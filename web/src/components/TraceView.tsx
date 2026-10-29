/**
 * Trace 视图(P4-F-04)。
 *
 * 后端入口 `GET /api/v1/logs/trace/{trace_or_request_id}`(P4-B-05 智能分流);
 * 渲染对齐 OBSERVABILITY §4.1 字面示例:**writer→reviewer→reviser 时序 +
 * 状态机箭头 + 总耗时 / 总成本**。
 *
 * ## 数据来源 + 视图布局
 *
 * 1. **元数据条**:trace_id(或 request_id)/ kind / workspace_id /
 *    chapter_id / 总耗时 / 总成本 / 总 tokens / 事件数。
 * 2. **状态机箭头摘要**:按时间序提取 `state_transition` 事件
 *    (from_state → to_state),用 → 连接;空列表时显示 "无状态切换"。
 * 3. **时序表**:每行一个事件(时间 / level / event / 关键 extra 字段),
 *    最长列优先 timestamp 升序;**reviewer / reviser / writer 阶段标识**
 *    通过 `logger` / `module` 字段着色:writer = sky / reviewer = amber /
 *    reviser = emerald / 其它 = muted。
 *
 * ## 总耗时 / 总成本计算
 *
 * - **总耗时**:`timestamp` 最大 - 最小(秒);≥60 时显示分钟+秒。
 * - **总成本**:遍历 `extra.cost_usd`(`llm_response` 等事件携带),累加。
 * - **总 tokens**:遍历 `extra.input_tokens` + `extra.output_tokens` 累加。
 *
 * ## 4 态分支(对齐 metrics 套路)
 *
 * loading / error / empty(items 为空)/ data。
 */

import { useMemo } from "react";

import {
  Card,
  CardContent,
  CardDescription,
  CardHeader,
  CardTitle,
} from "@/components/ui/card";
import {
  formatDurationSeconds,
  formatInt,
  formatUsd,
} from "@/lib/format-metrics";
import type { LogEntry, TraceViewOut } from "@/api/logs";
import { isApiError } from "@/api/errors";

interface TraceViewProps {
  data: TraceViewOut | undefined;
  isLoading: boolean;
  error: Error | null;
  /** 当前查询的原始 id(用于 loading / error / empty 文案展示)。 */
  traceOrRequestId: string;
}

interface TraceSummary {
  startedAt: string | null;
  endedAt: string | null;
  durationSeconds: number | null;
  totalCostUsd: number;
  totalInputTokens: number;
  totalOutputTokens: number;
  transitions: Array<{ from: string; to: string; at: string }>;
  rewriteCount: number;
  reviseCount: number;
}

function asNumber(value: unknown): number | null {
  if (typeof value === "number" && Number.isFinite(value)) return value;
  return null;
}

function asString(value: unknown): string | null {
  return typeof value === "string" ? value : null;
}

function summarizeTrace(items: readonly LogEntry[]): TraceSummary {
  if (items.length === 0) {
    return {
      startedAt: null,
      endedAt: null,
      durationSeconds: null,
      totalCostUsd: 0,
      totalInputTokens: 0,
      totalOutputTokens: 0,
      transitions: [],
      rewriteCount: 0,
      reviseCount: 0,
    };
  }
  const sorted = [...items].sort((a, b) =>
    a.timestamp < b.timestamp ? -1 : a.timestamp > b.timestamp ? 1 : 0,
  );
  const startedAt = sorted[0].timestamp;
  const endedAt = sorted[sorted.length - 1].timestamp;
  const durationMs =
    new Date(endedAt).getTime() - new Date(startedAt).getTime();
  let totalCostUsd = 0;
  let totalInputTokens = 0;
  let totalOutputTokens = 0;
  const transitions: TraceSummary["transitions"] = [];
  let rewriteCount = 0;
  let reviseCount = 0;
  for (const e of sorted) {
    const cost = asNumber(e.extra?.cost_usd);
    if (cost !== null) totalCostUsd += cost;
    const inTok = asNumber(e.extra?.input_tokens);
    if (inTok !== null) totalInputTokens += inTok;
    const outTok = asNumber(e.extra?.output_tokens);
    if (outTok !== null) totalOutputTokens += outTok;
    if (e.event === "state_transition") {
      const from = asString(e.extra?.from_state);
      const to = asString(e.extra?.to_state);
      if (from && to) {
        transitions.push({ from, to, at: e.timestamp });
        if (to === "REWRITING" || to === "DRAFTING") {
          if (from === "REVIEWING") rewriteCount += 1;
        }
        if (to === "REVISING") reviseCount += 1;
      }
    }
  }
  return {
    startedAt,
    endedAt,
    durationSeconds: Number.isFinite(durationMs) ? durationMs / 1000 : null,
    totalCostUsd,
    totalInputTokens,
    totalOutputTokens,
    transitions,
    rewriteCount,
    reviseCount,
  };
}

function loggerColor(entry: LogEntry): string {
  const tag = `${entry.logger ?? ""} ${entry.module ?? ""}`.toLowerCase();
  if (tag.includes("writer")) return "text-sky-700";
  if (tag.includes("reviewer")) return "text-amber-700";
  if (tag.includes("reviser")) return "text-emerald-700";
  return "text-muted-foreground";
}

function levelBadge(level: string): string {
  const lv = level.toUpperCase();
  if (lv === "ERROR") return "bg-rose-100 text-rose-700";
  if (lv === "WARNING") return "bg-amber-100 text-amber-700";
  if (lv === "INFO") return "bg-sky-100 text-sky-700";
  if (lv === "DEBUG") return "bg-muted text-muted-foreground";
  return "bg-muted text-muted-foreground";
}

function compactExtra(entry: LogEntry): string {
  const e = entry.extra ?? {};
  const picks: string[] = [];
  if (entry.event === "state_transition") {
    const from = asString(e.from_state);
    const to = asString(e.to_state);
    if (from && to) picks.push(`${from} → ${to}`);
  }
  const model = asString(e.model) ?? asString(e.model_used);
  if (model) picks.push(`model=${model}`);
  const cost = asNumber(e.cost_usd);
  if (cost !== null) picks.push(`cost=${formatUsd(cost)}`);
  const latency = asNumber(e.latency_ms);
  if (latency !== null) picks.push(`${(latency / 1000).toFixed(2)} s`);
  const inTok = asNumber(e.input_tokens);
  const outTok = asNumber(e.output_tokens);
  if (inTok !== null || outTok !== null) {
    picks.push(`tokens=${inTok ?? 0} in / ${outTok ?? 0} out`);
  }
  const score = asNumber(e.score_overall);
  if (score !== null) picks.push(`score=${score.toFixed(2)}`);
  const verdict = asString(e.verdict);
  if (verdict) picks.push(`verdict=${verdict}`);
  return picks.join(" · ");
}

function formatError(error: unknown): string {
  if (isApiError(error)) {
    return `${error.code}: ${error.message}`;
  }
  if (error instanceof Error) return error.message;
  return "未知错误";
}

export function TraceView({
  data,
  isLoading,
  error,
  traceOrRequestId,
}: TraceViewProps) {
  const summary = useMemo(
    () => summarizeTrace(data?.items ?? []),
    [data?.items],
  );

  if (isLoading) {
    return (
      <Card data-testid="trace-view-loading">
        <CardHeader className="pb-2">
          <CardTitle className="text-base">Trace 视图</CardTitle>
          <CardDescription className="text-xs">
            加载中…(ID: {traceOrRequestId})
          </CardDescription>
        </CardHeader>
        <CardContent>
          <div className="h-32 w-full animate-pulse rounded bg-muted" />
        </CardContent>
      </Card>
    );
  }
  if (error) {
    return (
      <Card
        data-testid="trace-view-error"
        className="border-destructive bg-destructive/10"
      >
        <CardHeader className="pb-2">
          <CardTitle className="text-base text-destructive">
            Trace 加载失败
          </CardTitle>
          <CardDescription className="text-xs text-destructive">
            {formatError(error)}
          </CardDescription>
        </CardHeader>
      </Card>
    );
  }
  if (!data || data.items.length === 0) {
    return (
      <Card data-testid="trace-view-empty">
        <CardHeader className="pb-2">
          <CardTitle className="text-base">Trace 视图</CardTitle>
          <CardDescription className="text-xs">
            该 Trace 当前无日志条目(jsonl 写入接通 P4-acceptance 前可能为空)。
          </CardDescription>
        </CardHeader>
        <CardContent>
          <p className="text-sm text-muted-foreground">
            ID: <code className="font-mono">{traceOrRequestId}</code>
          </p>
        </CardContent>
      </Card>
    );
  }

  const sortedItems = [...data.items].sort((a, b) =>
    a.timestamp < b.timestamp ? -1 : a.timestamp > b.timestamp ? 1 : 0,
  );

  return (
    <div className="space-y-4" data-testid="trace-view-data">
      <Card>
        <CardHeader className="pb-2">
          <CardTitle className="text-base">
            Trace 视图({data.kind === "request_id" ? "API 级" : "章节级"})
          </CardTitle>
          <CardDescription className="text-xs">
            ID: <code className="font-mono">{data.id}</code>
          </CardDescription>
        </CardHeader>
        <CardContent>
          <dl className="grid grid-cols-2 gap-x-6 gap-y-1.5 text-sm sm:grid-cols-4">
            <SummaryItem
              label="总耗时"
              value={formatDurationSeconds(summary.durationSeconds)}
              testId="trace-summary-duration"
            />
            <SummaryItem
              label="总成本"
              value={formatUsd(summary.totalCostUsd)}
              testId="trace-summary-cost"
            />
            <SummaryItem
              label="总 Tokens"
              value={`${formatInt(summary.totalInputTokens)} in / ${formatInt(
                summary.totalOutputTokens,
              )} out`}
              testId="trace-summary-tokens"
            />
            <SummaryItem
              label="事件数"
              value={formatInt(data.total)}
              testId="trace-summary-count"
            />
            <SummaryItem
              label="重写次数"
              value={String(summary.rewriteCount)}
              testId="trace-summary-rewrite"
            />
            <SummaryItem
              label="修订次数"
              value={String(summary.reviseCount)}
              testId="trace-summary-revise"
            />
          </dl>
        </CardContent>
      </Card>

      <Card>
        <CardHeader className="pb-2">
          <CardTitle className="text-base">状态机箭头</CardTitle>
          <CardDescription className="text-xs">
            按时间序提取 `state_transition` 事件
          </CardDescription>
        </CardHeader>
        <CardContent>
          {summary.transitions.length === 0 ? (
            <p className="text-sm text-muted-foreground">无状态切换事件</p>
          ) : (
            <div
              data-testid="trace-state-arrows"
              className="flex flex-wrap items-center gap-x-1 gap-y-2 text-sm"
            >
              {summary.transitions.map((t, i) => (
                <span
                  key={`${t.at}-${i}`}
                  className="inline-flex items-center gap-1"
                >
                  {i === 0 ? (
                    <code className="font-mono text-xs">{t.from}</code>
                  ) : null}
                  <span className="text-muted-foreground">→</span>
                  <code className="font-mono text-xs">{t.to}</code>
                </span>
              ))}
            </div>
          )}
        </CardContent>
      </Card>

      <Card>
        <CardHeader className="pb-2">
          <CardTitle className="text-base">时序(共 {data.total} 条)</CardTitle>
          <CardDescription className="text-xs">
            writer = 蓝 / reviewer = 黄 / reviser = 绿,按 timestamp 升序
          </CardDescription>
        </CardHeader>
        <CardContent>
          <div className="overflow-x-auto">
            <table
              data-testid="trace-events-table"
              className="w-full text-left text-xs"
            >
              <thead>
                <tr className="border-b border-border text-muted-foreground">
                  <th className="px-2 py-1.5 font-normal">时间</th>
                  <th className="px-2 py-1.5 font-normal">Level</th>
                  <th className="px-2 py-1.5 font-normal">Logger</th>
                  <th className="px-2 py-1.5 font-normal">Event</th>
                  <th className="px-2 py-1.5 font-normal">摘要</th>
                </tr>
              </thead>
              <tbody>
                {sortedItems.map((entry, i) => (
                  <tr
                    key={`${entry.timestamp}-${entry.event}-${i}`}
                    className="border-b border-border/50 last:border-0"
                  >
                    <td className="px-2 py-1 font-mono">{entry.timestamp}</td>
                    <td className="px-2 py-1">
                      <span
                        className={`rounded px-1.5 py-0.5 text-[10px] font-medium ${levelBadge(
                          entry.level,
                        )}`}
                      >
                        {entry.level}
                      </span>
                    </td>
                    <td className={`px-2 py-1 font-mono ${loggerColor(entry)}`}>
                      {entry.logger}
                    </td>
                    <td className="px-2 py-1 font-mono">{entry.event}</td>
                    <td className="px-2 py-1 text-muted-foreground">
                      {compactExtra(entry)}
                    </td>
                  </tr>
                ))}
              </tbody>
            </table>
          </div>
        </CardContent>
      </Card>
    </div>
  );
}

interface SummaryItemProps {
  label: string;
  value: string;
  testId: string;
}

function SummaryItem({ label, value, testId }: SummaryItemProps) {
  return (
    <div className="contents">
      <dt className="text-muted-foreground">{label}</dt>
      <dd data-testid={testId} className="font-mono">
        {value}
      </dd>
    </div>
  );
}
