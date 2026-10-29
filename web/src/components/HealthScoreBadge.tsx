/**
 * HealthScoreBadge(P4-F-05):工作区记忆健康度徽标。
 *
 * 数据源:`useMetricsMemoryHealthQuery(workspaceId, limit=1)` 取 `items[0]`
 * 最新一条 `MemoryHealthOut`,展示 `severity` 4 档 + 3 类 issue 数量摘要。
 *
 * ## severity 4 档(对齐 OBSERVABILITY §3.1 / P4-B-09)
 *
 * - `ok` —— 绿
 * - `minor` —— 蓝
 * - `warning` —— 黄
 * - `critical` —— 红
 *
 * ## 空数据 / 错误 / 加载分支
 *
 * - 加载中:灰 "记忆健康度:加载中"
 * - 错误:红边 "加载失败"(简化版,不展开 ApiError code)
 * - 无记录:灰 "记忆健康度:暂无数据"(P4-B-09 调用 POST /memory/check 前是常态)
 * - 数据:按 severity 着色;鼠标 hover title 显示 issue 计数明细
 */

import { useMetricsMemoryHealthQuery } from "@/api/metrics";
import { formatTime } from "@/lib/format-metrics";

interface HealthScoreBadgeProps {
  workspaceId: string;
}

const SEVERITY_LABEL: Record<string, string> = {
  ok: "ok",
  minor: "minor",
  warning: "warning",
  critical: "critical",
};

const SEVERITY_BADGE_CLASS: Record<string, string> = {
  ok: "bg-emerald-100 text-emerald-700",
  minor: "bg-sky-100 text-sky-700",
  warning: "bg-amber-100 text-amber-700",
  critical: "bg-rose-100 text-rose-700",
};

export function HealthScoreBadge(props: HealthScoreBadgeProps) {
  const query = useMetricsMemoryHealthQuery(props.workspaceId, 1);

  if (query.isPending && query.fetchStatus !== "idle") {
    return (
      <span
        data-testid="health-score-badge"
        data-state="loading"
        className="inline-flex items-center gap-1 rounded border border-border bg-muted/50 px-2 py-1 text-xs text-muted-foreground"
      >
        记忆健康度:加载中…
      </span>
    );
  }

  if (query.error) {
    return (
      <span
        data-testid="health-score-badge"
        data-state="error"
        className="inline-flex items-center gap-1 rounded border border-destructive bg-destructive/10 px-2 py-1 text-xs text-destructive"
      >
        记忆健康度:加载失败
      </span>
    );
  }

  const latest = query.data?.items?.[0];
  if (!latest) {
    return (
      <span
        data-testid="health-score-badge"
        data-state="empty"
        className="inline-flex items-center gap-1 rounded border border-dashed border-border bg-muted/30 px-2 py-1 text-xs text-muted-foreground"
        title="尚未运行过 memory health check。POST /workspaces/{id}/memory/check 触发后会落记录。"
      >
        记忆健康度:暂无数据
      </span>
    );
  }

  const sev = latest.severity;
  const sevClass = sev in SEVERITY_BADGE_CLASS
    ? SEVERITY_BADGE_CLASS[sev]
    : "bg-zinc-100 text-zinc-600";
  const sevLabel = sev in SEVERITY_LABEL ? SEVERITY_LABEL[sev] : sev;

  const issueSummary = `stale_threads=${latest.stale_threads_count} · inactive_chars=${latest.inactive_characters_count} · conflicts=${latest.conflicts_count}`;
  const checkedAt = formatTime(latest.checked_at);

  return (
    <span
      data-testid="health-score-badge"
      data-state="data"
      data-severity={sev}
      className="inline-flex items-center gap-1 rounded border border-border bg-card px-2 py-1 text-xs"
      title={`${issueSummary}\n检查时间:${checkedAt}`}
    >
      <span className="text-muted-foreground">记忆健康度:</span>
      <span className={`rounded px-1.5 py-0.5 text-[10px] font-medium ${sevClass}`}>
        {sevLabel}
      </span>
    </span>
  );
}

