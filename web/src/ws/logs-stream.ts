/**
 * Logs Stream WS hook(P4-F-05 LogsPage 实时模式)。
 *
 * 接后端 P4-B-05 独立 WS endpoint `/api/v1/logs/stream`(**不是** `/api/v1/ws`
 * 主多路复用频道);握手成功后服务端按 `LOG_STREAM_POLL_SECONDS=1.0` 周期单向
 * push 新增 `LogEntryOut` 字段格式的 JSON 行。客户端无需发任何帧(后端 loop
 * 不读 client message)。
 *
 * 设计选择(P4-F-05 自决项):
 *
 * - **独立原生 `WebSocket` 而非复用 `getWsClient()` 单例**:`/logs/stream` 是
 *   独立 path 的 WS endpoint(P4-B-05 `@router.websocket("/logs/stream")`),
 *   协议不带 `{channel, event, data}` 信封,直接 push raw `LogEntryOut` 字段;
 *   单例 client 的频道分发不适用。
 * - **buffer 上限 500 条**:实时模式长跑不限制会导致前端 OOM。soft cap 500,
 *   超出后从头丢老的(老的可用 REST `/logs?...` 重新拉)。
 * - **pause / resume / clear 三态**:`isPaused=true` 时 onmessage 把新行写入
 *   `pendingRef`(不动 buffer state,不重渲);resume 后一次性 flush。`clear`
 *   清 buffer + pendingRef。
 * - **自动重连**:与 `ws/client.ts` 同款指数退避序列 1s→2s→4s→8s→16s→30s,
 *   主动 close 不重连。
 *
 * 状态机
 * ------
 *
 * - `idle`:未启动(组件还没调 `start()`)
 * - `connecting`:WS 建立中(首次或重连)
 * - `open`:连接 ready,正在接收
 * - `closed`:主动 close,不重连
 * - `reconnecting`:等下次退避重连
 *
 * 与 P4-F-05 LogsPage 集成
 * --------------------------
 *
 * `useLogsStream({ enabled })` 在 `enabled=true` 时自动 connect,`false` 时
 * close;返回 `{ buffer, isPaused, status, pause, resume, clear, droppedCount }`。
 */

import { useCallback, useEffect, useRef, useState } from "react";

import { getApiBaseURL } from "@/api/client";
import type { LogEntry } from "@/api/logs";

export type LogsStreamStatus =
  | "idle"
  | "connecting"
  | "open"
  | "closed"
  | "reconnecting";

const RECONNECT_BACKOFF_MS = [1000, 2000, 4000, 8000, 16000, 30000] as const;
const NORMAL_CLOSE_CODE = 1000;
const BUFFER_SOFT_CAP = 500;

function deriveLogsStreamURL(httpBase: string): string {
  const wsScheme = httpBase.startsWith("https://") ? "wss://" : "ws://";
  const hostAndPath = httpBase.replace(/^https?:\/\//, "");
  return `${wsScheme}${hostAndPath}/logs/stream`;
}

interface UseLogsStreamOptions {
  /** `true` 时启动 WS;`false` 时主动关闭(切回静态模式)。 */
  enabled: boolean;
}

export interface UseLogsStreamResult {
  /** 已接收的日志条目(最新在末尾,append-only;cap 500)。 */
  buffer: readonly LogEntry[];
  /** 暂停时是否累积新行(仅供 UI 显示"暂停中(累积 N 条)")。 */
  isPaused: boolean;
  /** 暂停期间累积的待 flush 条数。 */
  pendingCount: number;
  /** 因 buffer cap 已被丢弃的旧条数(诊断用)。 */
  droppedCount: number;
  /** 连接状态(LogsPage 显示)。 */
  status: LogsStreamStatus;
  /** 暂停(新行进 pending 队列,不更新 buffer state)。 */
  pause: () => void;
  /** 恢复:flush pending 后转 active。 */
  resume: () => void;
  /** 清屏(清 buffer + pending + dropped 计数器)。 */
  clear: () => void;
}

export function useLogsStream(
  options: UseLogsStreamOptions,
): UseLogsStreamResult {
  const { enabled } = options;

  const [buffer, setBuffer] = useState<readonly LogEntry[]>([]);
  const [isPaused, setIsPaused] = useState<boolean>(false);
  const [pendingCount, setPendingCount] = useState<number>(0);
  const [droppedCount, setDroppedCount] = useState<number>(0);
  const [status, setStatus] = useState<LogsStreamStatus>("idle");

  // 暂停期间的 pending 队列(不入 state,避免暂停期间频繁重渲)。
  const pendingRef = useRef<LogEntry[]>([]);
  const isPausedRef = useRef<boolean>(false);
  const wsRef = useRef<WebSocket | null>(null);
  const reconnectAttemptRef = useRef<number>(0);
  const reconnectTimerRef = useRef<ReturnType<typeof setTimeout> | null>(null);
  const manualCloseRef = useRef<boolean>(false);

  const appendToBuffer = useCallback((row: LogEntry) => {
    setBuffer((prev) => {
      const next = prev.length >= BUFFER_SOFT_CAP
        ? prev.slice(prev.length - BUFFER_SOFT_CAP + 1)
        : prev.slice();
      if (prev.length >= BUFFER_SOFT_CAP) {
        // 刚才切了头部一条,记入 dropped 计数。
        setDroppedCount((d) => d + 1);
      }
      next.push(row);
      return next;
    });
  }, []);

  const flushPending = useCallback(() => {
    const queued = pendingRef.current;
    if (queued.length === 0) {
      setPendingCount(0);
      return;
    }
    pendingRef.current = [];
    setPendingCount(0);
    setBuffer((prev) => {
      const combined = prev.concat(queued);
      const overflow = combined.length - BUFFER_SOFT_CAP;
      if (overflow > 0) {
        setDroppedCount((d) => d + overflow);
        return combined.slice(overflow);
      }
      return combined;
    });
  }, []);

  const pause = useCallback(() => {
    isPausedRef.current = true;
    setIsPaused(true);
  }, []);

  const resume = useCallback(() => {
    isPausedRef.current = false;
    setIsPaused(false);
    flushPending();
  }, [flushPending]);

  const clear = useCallback(() => {
    pendingRef.current = [];
    setBuffer([]);
    setPendingCount(0);
    setDroppedCount(0);
  }, []);

  // ---- WS 生命周期 -----------------------------------------------------------

  useEffect(() => {
    if (!enabled) {
      // 切静态模式 → 主动 close + 状态归位。
      manualCloseRef.current = true;
      if (reconnectTimerRef.current !== null) {
        clearTimeout(reconnectTimerRef.current);
        reconnectTimerRef.current = null;
      }
      if (wsRef.current !== null) {
        try {
          wsRef.current.close(NORMAL_CLOSE_CODE);
        } catch {
          /* 已关 / 关中,吞 */
        }
        wsRef.current = null;
      }
      reconnectAttemptRef.current = 0;
      setStatus("idle");
      return;
    }

    manualCloseRef.current = false;

    const url = deriveLogsStreamURL(getApiBaseURL());

    function openSocket() {
      setStatus("connecting");
      let ws: WebSocket;
      try {
        ws = new WebSocket(url);
      } catch {
        scheduleReconnect();
        return;
      }
      wsRef.current = ws;
      ws.onopen = () => {
        reconnectAttemptRef.current = 0;
        setStatus("open");
      };
      ws.onmessage = (event) => {
        let row: LogEntry;
        try {
          row = JSON.parse(event.data as string) as LogEntry;
        } catch {
          return;
        }
        if (isPausedRef.current) {
          pendingRef.current.push(row);
          setPendingCount(pendingRef.current.length);
          return;
        }
        appendToBuffer(row);
      };
      ws.onclose = (event) => {
        wsRef.current = null;
        if (manualCloseRef.current || event.code === NORMAL_CLOSE_CODE) {
          setStatus("closed");
          return;
        }
        scheduleReconnect();
      };
      ws.onerror = () => {
        // onclose 一定跟着,统一在那里处理。
      };
    }

    function scheduleReconnect() {
      if (reconnectTimerRef.current !== null) return;
      const idx = Math.min(
        reconnectAttemptRef.current,
        RECONNECT_BACKOFF_MS.length - 1,
      );
      const delay = RECONNECT_BACKOFF_MS[idx];
      reconnectAttemptRef.current += 1;
      setStatus("reconnecting");
      reconnectTimerRef.current = setTimeout(() => {
        reconnectTimerRef.current = null;
        if (!manualCloseRef.current) openSocket();
      }, delay);
    }

    openSocket();

    return () => {
      manualCloseRef.current = true;
      if (reconnectTimerRef.current !== null) {
        clearTimeout(reconnectTimerRef.current);
        reconnectTimerRef.current = null;
      }
      if (wsRef.current !== null) {
        try {
          wsRef.current.close(NORMAL_CLOSE_CODE);
        } catch {
          /* 同上 */
        }
        wsRef.current = null;
      }
    };
  }, [enabled, appendToBuffer]);

  return {
    buffer,
    isPaused,
    pendingCount,
    droppedCount,
    status,
    pause,
    resume,
    clear,
  };
}
