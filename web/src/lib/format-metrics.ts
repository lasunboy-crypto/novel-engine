/**
 * 集中 metrics / logs 相关格式化 helper(P4-acceptance-followup 抽取)。
 *
 * Rule of Three 触发来源:
 * - formatUsd:MetricsCostBreakdown / TraceView / MetricsPerChapterTable / MetricsDailyChart 4 处复制
 * - formatInt:MetricsCostBreakdown / TraceView / MetricsPerChapterTable 3 处复制
 * - formatScore / scoreColor / formatLatencyMinutes:MetricsPerChapterTable 单点(预留 P5+ 复用)
 * - formatDurationSeconds:TraceView 单点(预留 P5+ Trace 视图扩展复用)
 * - formatTime:HealthScoreBadge 单点(预留 LogsPage / 告警面板复用)
 *
 * P4-acceptance §6 接力 + 启动指令 §6 ⑥ 字面"统一抽 cost / token / latency / score 格式化函数到 lib"。
 */

/** USD 金额:固定 4 位小数,前置 $ 符号(对齐后端 OBSERVABILITY §2.2 "_usd 后缀 4 位")。 */
export function formatUsd(value: number): string {
  return `$${value.toFixed(4)}`;
}

/** 整数千分位:`1,234,567`(token 数 / 章数等)。 */
export function formatInt(value: number): string {
  return value.toLocaleString("en-US");
}

/** 评分:null / undefined → `—`,数字 → 2 位小数。 */
export function formatScore(value: number | null | undefined): string {
  return value === null || value === undefined ? "—" : value.toFixed(2);
}

/** 评分着色 class:≥8 绿,≥6.5 琥珀,<6.5 玫红;空值灰。 */
export function scoreColor(value: number | null | undefined): string {
  if (value === null || value === undefined) return "text-muted-foreground";
  if (value >= 8) return "text-emerald-600";
  if (value >= 6.5) return "text-amber-600";
  return "text-rose-600";
}

/** 秒 → 分钟:≥10 分钟取整,<10 分钟保留 1 位小数;空值 `—`。 */
export function formatLatencyMinutes(
  seconds: number | null | undefined,
): string {
  if (seconds === null || seconds === undefined) return "—";
  const minutes = seconds / 60;
  if (minutes >= 10) return `${minutes.toFixed(0)} min`;
  return `${minutes.toFixed(1)} min`;
}

/** 秒 → "1 min 23 s" / "0.5 s";空值 / 非法 `—`。Trace 视图章节时长用。 */
export function formatDurationSeconds(seconds: number | null): string {
  if (seconds === null || !Number.isFinite(seconds) || seconds < 0) return "—";
  if (seconds < 60) return `${seconds.toFixed(1)} s`;
  const minutes = Math.floor(seconds / 60);
  const remain = Math.round(seconds - minutes * 60);
  return `${minutes} min ${remain.toString().padStart(2, "0")} s`;
}

/** ISO 字符串 → `zh-CN` 本地化"YYYY/MM/DD HH:mm";非法 ISO 原样返回。 */
export function formatTime(iso: string): string {
  const d = new Date(iso);
  if (Number.isNaN(d.getTime())) return iso;
  return d.toLocaleString("zh-CN", {
    year: "numeric",
    month: "2-digit",
    day: "2-digit",
    hour: "2-digit",
    minute: "2-digit",
  });
}
