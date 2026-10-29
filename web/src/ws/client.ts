/**
 * WebSocket 客户端封装(P1-F-06)。
 *
 * 职责
 * ====
 *
 * - 用浏览器原生 `WebSocket` 连后端 `ws://.../api/v1/ws`(从
 *   `getApiBaseURL()` 派生,自动 `http→ws / https→wss`)
 * - 收到服务端 `{type:"ping"}` 立即回 `{type:"pong"}`(对齐 API_CONTRACT §5.4
 *   + P1-B-20 真实协议方向:**服务端发 ping,客户端只 pong**——NEXT_STEPS /
 *   p1_plan §P1-F-06 字面"客户端发 ping"已过时,以 spec + 后端实现为准)
 * - 按 `channel` 注册 / 注销 listener;收到服务端推送帧
 *   `{channel, event, timestamp, data}` 时分发到该 channel 下所有 listener
 * - **断线指数退避重连**:1s → 2s → 4s → 8s → 16s → 30s 封顶;主动 `close()`
 *   或 close code=1000(正常关闭)不重连
 * - 重连成功后**自动重新发 subscribe**(服务端不持久化订阅,P1-B-20 决策 5)
 * - 暴露连接状态机(`connecting / open / closed / reconnecting`)供 UI 订阅
 *
 * 模块级单例
 * ==========
 *
 * 暴露 `getWsClient()` 返回模块级单例。P1 单连接前端模型——所有页面 / hook
 * 共享一条 WS 连接,各自按 channel 订阅。模块卸载时 client 不自动 close,
 * 由 React lifecycle(unmount)负责取消 listener;空 channel 不触发 unsubscribe
 * (本期简化,P2+ 加 GC)。
 *
 * P1 不实现
 * =========
 *
 * - `?token=xxx` 认证(P1 默认 `API_AUTH_ENABLED=false`)
 * - 订阅持久化到 localStorage(内存订阅集合即可,重连重发)
 * - 多事件型频道(`pipeline.events` / `system.alerts` / `task.{id}.progress`),P2+
 * - 前端单测(P1-F 期无测试基建,典型 typecheck + build + Preview 程序化验)
 */

import { getApiBaseURL } from "@/api/client";
import type {
  ChannelListener,
  ClientFrame,
  ServerFrame,
  ServerPushFrame,
  WsConnectionState,
} from "@/ws/types";

/** 重连退避序列(ms),最末值为封顶值,达到后保持。 */
const RECONNECT_BACKOFF_MS = [1000, 2000, 4000, 8000, 16000, 30000] as const;

/** WebSocket close code 1000 = "Normal Closure"(RFC 6455);不重连。 */
const NORMAL_CLOSE_CODE = 1000;

type StateListener = (state: WsConnectionState) => void;

/** 把 `http(s)://host/api/v1` 派生成 `ws(s)://host/api/v1/ws`。 */
function deriveWsURL(httpBase: string): string {
  const wsScheme = httpBase.startsWith("https://") ? "wss://" : "ws://";
  const hostAndPath = httpBase.replace(/^https?:\/\//, "");
  return `${wsScheme}${hostAndPath}/ws`;
}

export class WebSocketClient {
  private url: string;
  private ws: WebSocket | null = null;
  private state: WsConnectionState = "closed";

  /** 频道 → listener 集合(用 Set 防同 listener 重复注册)。 */
  private channelListeners: Map<string, Set<ChannelListener>> = new Map();

  /** 状态变化订阅者(UI / 调试用)。 */
  private stateListeners: Set<StateListener> = new Set();

  /** 当前重连尝试次数(用于查 `RECONNECT_BACKOFF_MS`)。 */
  private reconnectAttempt = 0;

  /** 当前等待中的重连定时器,主动 close 时清掉。 */
  private reconnectTimer: ReturnType<typeof setTimeout> | null = null;

  /** 用户主动 close 标记;用来阻断 onclose 触发的自动重连。 */
  private manualClose = false;

  constructor(url?: string) {
    this.url = url ?? deriveWsURL(getApiBaseURL());
  }

  // ---- 公开 API ----------------------------------------------------------

  /** 启动连接。已开 / 连接中重复调直接返回。 */
  connect(): void {
    if (this.ws !== null) {
      return;
    }
    this.manualClose = false;
    this.openSocket();
  }

  /** 主动关闭。不会触发自动重连;`reconnectTimer` 一并清掉。 */
  close(): void {
    this.manualClose = true;
    if (this.reconnectTimer !== null) {
      clearTimeout(this.reconnectTimer);
      this.reconnectTimer = null;
    }
    if (this.ws !== null) {
      try {
        this.ws.close(NORMAL_CLOSE_CODE);
      } catch {
        /* 已关 / 关中,吞掉 */
      }
      this.ws = null;
    }
    this.setState("closed");
  }

  /** 当前连接状态快照。 */
  getState(): WsConnectionState {
    return this.state;
  }

  /** 已订阅频道集合(诊断用)。 */
  subscribedChannels(): string[] {
    return Array.from(this.channelListeners.keys()).sort();
  }

  /**
   * 订阅 channel,返回取消函数。
   *
   * 第一次往一个 channel 注册 listener 时,client 会发 `subscribe` 帧到服务端
   * (若 socket 未 open,只把 listener 记下来,等 onopen 时统一刷一遍订阅);
   * 重复注册同一 channel 不重复发 subscribe。listener 用 Set 去重(同函数引用
   * 多次 subscribe 只算一次)。
   */
  subscribe<TData = unknown>(
    channel: string,
    listener: ChannelListener<TData>,
  ): () => void {
    const generic = listener as ChannelListener;
    const existing = this.channelListeners.get(channel);
    const isFirstListenerForChannel = existing === undefined;
    if (existing === undefined) {
      this.channelListeners.set(channel, new Set([generic]));
    } else {
      existing.add(generic);
    }
    if (isFirstListenerForChannel && this.state === "open") {
      this.sendFrame({ type: "subscribe", channels: [channel] });
    }
    return () => this.unsubscribeListener(channel, generic);
  }

  /** 订阅连接状态变化,返回取消函数。 */
  subscribeState(listener: StateListener): () => void {
    this.stateListeners.add(listener);
    return () => {
      this.stateListeners.delete(listener);
    };
  }

  // ---- 内部:连接生命周期 ------------------------------------------------

  /** 真正建 WebSocket。 */
  private openSocket(): void {
    this.setState(this.reconnectAttempt > 0 ? "connecting" : "connecting");
    let ws: WebSocket;
    try {
      ws = new WebSocket(this.url);
    } catch {
      // URL 非法 / 浏览器拒绝:走重连退避。
      this.scheduleReconnect();
      return;
    }
    this.ws = ws;
    ws.onopen = () => this.handleOpen();
    ws.onmessage = (e) => this.handleMessage(e);
    ws.onclose = (e) => this.handleClose(e);
    ws.onerror = () => {
      // 不在 onerror 处理重连——onclose 一定会跟着触发,统一在那里处理。
    };
  }

  private handleOpen(): void {
    this.reconnectAttempt = 0;
    this.setState("open");
    // 重连 / 首次连接成功后,把所有内存里已注册 channel 一次性发 subscribe。
    const channels = Array.from(this.channelListeners.keys());
    if (channels.length > 0) {
      this.sendFrame({ type: "subscribe", channels });
    }
  }

  private handleMessage(event: MessageEvent): void {
    let parsed: ServerFrame;
    try {
      parsed = JSON.parse(event.data as string) as ServerFrame;
    } catch {
      // 非 JSON 帧静默忽略(P1 简版,前向兼容更好)。
      return;
    }
    if ((parsed as { type?: string }).type === "ping") {
      this.sendFrame({ type: "pong" });
      return;
    }
    const push = parsed as ServerPushFrame;
    if (typeof push.channel !== "string" || typeof push.event !== "string") {
      // 非 ping 也非合法 push 帧,忽略。
      return;
    }
    const listeners = this.channelListeners.get(push.channel);
    if (listeners === undefined || listeners.size === 0) {
      return;
    }
    for (const fn of listeners) {
      try {
        fn(push);
      } catch {
        // listener 内部抛错不应影响其他 listener;P1 不打日志(浏览器控制台可见)。
      }
    }
  }

  private handleClose(event: CloseEvent): void {
    this.ws = null;
    if (this.manualClose || event.code === NORMAL_CLOSE_CODE) {
      this.setState("closed");
      return;
    }
    this.scheduleReconnect();
  }

  private scheduleReconnect(): void {
    if (this.reconnectTimer !== null) {
      return;
    }
    const idx = Math.min(
      this.reconnectAttempt,
      RECONNECT_BACKOFF_MS.length - 1,
    );
    const delay = RECONNECT_BACKOFF_MS[idx];
    this.reconnectAttempt += 1;
    this.setState("reconnecting");
    this.reconnectTimer = setTimeout(() => {
      this.reconnectTimer = null;
      this.openSocket();
    }, delay);
  }

  // ---- 内部:帧发送 / 订阅维护 ------------------------------------------

  private sendFrame(frame: ClientFrame): void {
    if (this.ws === null || this.ws.readyState !== WebSocket.OPEN) {
      return;
    }
    try {
      this.ws.send(JSON.stringify(frame));
    } catch {
      // send 失败常见于 socket 刚进入关闭态;onclose 会接管,这里吞掉。
    }
  }

  private unsubscribeListener(
    channel: string,
    listener: ChannelListener,
  ): void {
    const set = this.channelListeners.get(channel);
    if (set === undefined) {
      return;
    }
    set.delete(listener);
    if (set.size === 0) {
      this.channelListeners.delete(channel);
      if (this.state === "open") {
        this.sendFrame({ type: "unsubscribe", channels: [channel] });
      }
    }
  }

  private setState(next: WsConnectionState): void {
    if (this.state === next) {
      return;
    }
    this.state = next;
    for (const fn of this.stateListeners) {
      try {
        fn(next);
      } catch {
        /* 同 listener:吞掉避免污染其他订阅 */
      }
    }
  }
}

// ---- 模块级单例 -----------------------------------------------------------

let singleton: WebSocketClient | null = null;

/**
 * 获取模块级单例 WS 客户端;首次调用时 lazy 创建并 `connect()`。
 *
 * 设计理由(对齐计划"P1 单连接前端模型"):整个 SPA 共享一条 WS 连接,
 * page / hook 各自按 channel 订阅;避免每页面起独立 socket 浪费连接。
 */
export function getWsClient(): WebSocketClient {
  if (singleton === null) {
    singleton = new WebSocketClient();
    singleton.connect();
  }
  return singleton;
}

/**
 * 测试 / 验收用:重置单例(把现有连接 close,下次 `getWsClient` 重建)。
 *
 * 产品代码不调。Claude Preview 程序化验收切场景时用。
 */
export function __resetWsClientForTesting(): void {
  if (singleton !== null) {
    singleton.close();
    singleton = null;
  }
}
