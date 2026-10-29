/**
 * AlertBadge(P4-F-05):订阅 WS `system.alerts` 频道,显示告警红点 + 累计计数。
 *
 * 触发事件(对齐 API_CONTRACT §5.3 + P4-B-08 / P4-B-09 / P4-B-10):
 *
 * - `cost_guard_triggered` —— 单日 / 单工作区 / 月度成本超限
 * - `threshold_alert_triggered` —— 8 条阈值告警(rewrite/score/tension/...)
 * - `memory_health_critical` —— 记忆健康度 critical 等级
 * - `rate_limit_exceeded` —— API 限流触发(P4-B-10)
 *
 * ## 累计 vs 当前
 *
 * - `total`:从组件 mount 起累计接收条数(红点上的 N)
 * - `recent`:最近 N(本组件 cap 20)条 alerts,点击徽标展开下拉列表
 * - "清除"按钮把两者归 0(本地视图层操作,不与后端交互)
 *
 * ## 跨工作区接收
 *
 * `system.alerts` 是系统级频道,接所有工作区告警。组件不主动按 workspace_id
 * 过滤(WorkspaceDetailPage 内嵌时 P5+ 可加 `filterWorkspaceId` prop)。
 */

import { useEffect, useRef, useState } from "react";

import { getWsClient } from "@/ws/client";
import type { ServerPushFrame } from "@/ws/types";

interface AlertRecord {
  /** WS 帧自带的 ISO 8601 时间戳。 */
  timestamp: string;
  /** 事件名(cost_guard_triggered / memory_health_critical / threshold_alert_triggered / rate_limit_exceeded)。 */
  event: string;
  /** 原始 data(展开行渲染用)。 */
  data: Record<string, unknown>;
}

const RECENT_CAP = 20;

export function AlertBadge() {
  const [recent, setRecent] = useState<readonly AlertRecord[]>([]);
  const [total, setTotal] = useState<number>(0);
  const [open, setOpen] = useState<boolean>(false);
  const buttonRef = useRef<HTMLButtonElement | null>(null);

  useEffect(() => {
    const client = getWsClient();
    const unsubscribe = client.subscribe<Record<string, unknown>>(
      "system.alerts",
      (frame: ServerPushFrame<Record<string, unknown>>) => {
        const record: AlertRecord = {
          timestamp: frame.timestamp,
          event: frame.event,
          data: frame.data ?? {},
        };
        setRecent((prev) => {
          const next = prev.concat([record]);
          return next.length > RECENT_CAP
            ? next.slice(next.length - RECENT_CAP)
            : next;
        });
        setTotal((n) => n + 1);
      },
    );
    return unsubscribe;
  }, []);

  function handleToggle() {
    setOpen((o) => !o);
  }

  function handleClear() {
    setRecent([]);
    setTotal(0);
    setOpen(false);
  }

  return (
    <div className="relative inline-block" data-testid="alert-badge">
      <button
        ref={buttonRef}
        type="button"
        onClick={handleToggle}
        data-testid="alert-badge-button"
        className="relative inline-flex h-9 items-center gap-1 rounded-md border border-input bg-background px-3 text-sm shadow-sm hover:bg-accent"
      >
        <span aria-hidden="true">🔔</span>
        <span>告警</span>
        {total > 0 && (
          <span
            data-testid="alert-badge-count"
            className="ml-1 inline-flex h-4 min-w-[1rem] items-center justify-center rounded-full bg-rose-500 px-1 text-[10px] font-medium text-white"
          >
            {total > 99 ? "99+" : total}
          </span>
        )}
      </button>
      {open && (
        <div
          data-testid="alert-badge-popover"
          className="absolute right-0 z-20 mt-1 w-80 rounded-md border border-border bg-popover p-2 shadow-md"
        >
          <div className="mb-2 flex items-center justify-between">
            <p className="text-xs font-medium">
              最近告警(累计 {total} 条,显示最近 {recent.length})
            </p>
            <button
              type="button"
              onClick={handleClear}
              data-testid="alert-badge-clear"
              className="text-xs text-muted-foreground underline"
            >
              清除
            </button>
          </div>
          {recent.length === 0 ? (
            <p
              data-testid="alert-badge-empty"
              className="rounded border border-dashed border-border p-3 text-center text-xs text-muted-foreground"
            >
              暂无告警
            </p>
          ) : (
            <ul className="space-y-1 max-h-72 overflow-y-auto">
              {recent
                .slice()
                .reverse()
                .map((r, i) => (
                  <li
                    key={`${r.timestamp}-${i}`}
                    data-testid="alert-badge-item"
                    className="rounded bg-muted/40 p-2 text-xs"
                  >
                    <div className="flex items-center justify-between gap-2">
                      <span
                        className={`rounded px-1.5 py-0.5 text-[10px] font-medium ${eventBadgeClass(
                          r.event,
                        )}`}
                      >
                        {r.event}
                      </span>
                      <span className="text-[10px] text-muted-foreground font-mono">
                        {formatTime(r.timestamp)}
                      </span>
                    </div>
                    <p className="mt-1 break-all font-mono text-[10px] text-muted-foreground">
                      {summarizeData(r.event, r.data)}
                    </p>
                  </li>
                ))}
            </ul>
          )}
        </div>
      )}
    </div>
  );
}

function eventBadgeClass(event: string): string {
  if (event === "cost_guard_triggered") return "bg-rose-100 text-rose-700";
  if (event === "memory_health_critical") return "bg-rose-100 text-rose-700";
  if (event === "threshold_alert_triggered") return "bg-amber-100 text-amber-700";
  if (event === "rate_limit_exceeded") return "bg-amber-100 text-amber-700";
  return "bg-zinc-100 text-zinc-700";
}

function summarizeData(event: string, data: Record<string, unknown>): string {
  // 各事件主字段摘要(避免下拉里渲染整段 JSON);未知 event 退回 stringify。
  if (event === "cost_guard_triggered") {
    const cur = data["current_usd"];
    const lim = data["limit_usd"];
    const ws = data["workspace_id"];
    return `current=${formatScalar(cur)} / limit=${formatScalar(lim)}${
      ws ? ` · ws=${formatScalar(ws)}` : ""
    }`;
  }
  if (event === "memory_health_critical") {
    const ws = data["workspace_id"];
    const summary = data["summary"];
    const issues = data["issues_count"];
    return `${ws ? `ws=${formatScalar(ws)} · ` : ""}issues=${formatScalar(
      issues,
    )}${summary ? ` · ${formatScalar(summary)}` : ""}`;
  }
  if (event === "threshold_alert_triggered") {
    const code = data["code"];
    const sev = data["severity"];
    const ws = data["workspace_id"];
    return `${formatScalar(code)} · ${formatScalar(sev)}${
      ws ? ` · ws=${formatScalar(ws)}` : ""
    }`;
  }
  if (event === "rate_limit_exceeded") {
    const ws = data["workspace_id"];
    const method = data["method"];
    const path = data["path"];
    return `${formatScalar(method)} ${formatScalar(path)}${
      ws ? ` · ws=${formatScalar(ws)}` : ""
    }`;
  }
  try {
    return JSON.stringify(data);
  } catch {
    return "(无法序列化)";
  }
}

function formatScalar(v: unknown): string {
  if (v === null || v === undefined) return "—";
  if (typeof v === "string") return v;
  if (typeof v === "number" || typeof v === "boolean") return String(v);
  try {
    return JSON.stringify(v);
  } catch {
    return "(?)";
  }
}

function formatTime(iso: string): string {
  const d = new Date(iso);
  if (Number.isNaN(d.getTime())) return iso;
  return d.toLocaleTimeString("zh-CN", { hour12: false });
}
