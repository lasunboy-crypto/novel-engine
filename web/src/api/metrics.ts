/**
 * Metrics API hooks(P4-F-02 概览 + 每日趋势,P4-F-03 续接分章 + 张力 + 成本)。
 *
 * 后端入口:OBSERVABILITY §3.2 字面 7 工作区端点 + 1 系统级端点(P4-B-04):
 *
 * - `GET /api/v1/workspaces/{id}/metrics/overview`
 * - `GET /api/v1/workspaces/{id}/metrics/daily?since=&until=`
 * - `GET /api/v1/workspaces/{id}/metrics/per-chapter?order=`
 * - `GET /api/v1/workspaces/{id}/metrics/tension-curve`
 * - `GET /api/v1/workspaces/{id}/metrics/cost-breakdown`
 * - `GET /api/v1/workspaces/{id}/metrics/memory-health?limit=`
 * - `GET /api/v1/workspaces/{id}/metrics/style-audits?limit=`
 * - `GET /api/v1/system/metrics?history_days=`
 *
 * ## URL 形态(对齐 P3-F-02 config.ts)
 *
 * 嵌套子资源,workspace_id 走 URL path,**不需要 `X-Workspace-Id` header**
 * (P4-B-04 路由文件 docstring 字面拍板;对齐 P3-B-10 config 路由模式)。
 *
 * ## queryKey 命名空间
 *
 * 每端点独立前缀,避免 P4-F-03 / P4-F-05 引入 mutation 时 invalidate 误伤:
 *
 * - `["metrics_overview", workspaceId]`
 * - `["metrics_daily", workspaceId, since|null, until|null]`
 * - `["metrics_per_chapter", workspaceId, order]`
 * - `["metrics_tension_curve", workspaceId]`
 * - `["metrics_cost_breakdown", workspaceId]`
 * - `["metrics_memory_health", workspaceId, limit]`
 * - `["metrics_style_audits", workspaceId, limit]`
 * - `["system_metrics", historyDays]`
 *
 * ## `enabled` 短路
 *
 * `workspaceId` 缺失时 `enabled=false`(对齐 useCharacterListQuery / useWorkspaceConfigQuery
 * 套路);组件层做"无活动工作区时不渲染数据栏"分支。
 *
 * ## P4-F-02 只读
 *
 * 全部端点为 GET,只暴露 `useXxxQuery` hook;无 mutation。P5+ 配置可写时再扩。
 *
 * ## 后端空数据语义
 *
 * 工作区存在但 metrics 表为空 → 200 + 空数组 / 0 字段(P4-B-04 §回退 / 错误语义);
 * 调用方拿到的始终是合法 schema,**无需特判 404**。
 */

import { useQuery } from "@tanstack/react-query";

import { apiGet } from "@/api/client";
import type { components } from "@/api/types";

export type OverviewOut = components["schemas"]["OverviewOut"];
export type MetricsDailyOut = components["schemas"]["MetricsDailyOut"];
export type DailyListOut = components["schemas"]["DailyListOut"];
export type PerChapterOut = components["schemas"]["PerChapterOut"];
export type PerChapterListOut = components["schemas"]["PerChapterListOut"];
export type TensionPoint = components["schemas"]["TensionPoint"];
export type TensionCurveOut = components["schemas"]["TensionCurveOut"];
export type CostBucket = components["schemas"]["CostBucket"];
export type CostBreakdownOut = components["schemas"]["CostBreakdownOut"];
export type MemoryHealthOut = components["schemas"]["MemoryHealthOut"];
export type MemoryHealthListOut =
  components["schemas"]["MemoryHealthListOut"];
export type StyleAuditOut = components["schemas"]["StyleAuditOut"];
export type StyleAuditListOut = components["schemas"]["StyleAuditListOut"];
export type SystemMetricsDailyOut =
  components["schemas"]["SystemMetricsDailyOut"];
export type SystemMetricsOut = components["schemas"]["SystemMetricsOut"];

export type PerChapterOrder =
  | "approved_at_desc"
  | "approved_at_asc"
  | "chapter_id_asc";

export const METRICS_OVERVIEW_QUERY_KEY = (workspaceId: string) =>
  ["metrics_overview", workspaceId] as const;

export const METRICS_DAILY_QUERY_KEY = (
  workspaceId: string,
  since: string | null,
  until: string | null,
) => ["metrics_daily", workspaceId, since, until] as const;

export const METRICS_PER_CHAPTER_QUERY_KEY = (
  workspaceId: string,
  order: PerChapterOrder,
) => ["metrics_per_chapter", workspaceId, order] as const;

export const METRICS_TENSION_CURVE_QUERY_KEY = (workspaceId: string) =>
  ["metrics_tension_curve", workspaceId] as const;

export const METRICS_COST_BREAKDOWN_QUERY_KEY = (workspaceId: string) =>
  ["metrics_cost_breakdown", workspaceId] as const;

export const METRICS_MEMORY_HEALTH_QUERY_KEY = (
  workspaceId: string,
  limit: number,
) => ["metrics_memory_health", workspaceId, limit] as const;

export const METRICS_STYLE_AUDITS_QUERY_KEY = (
  workspaceId: string,
  limit: number,
) => ["metrics_style_audits", workspaceId, limit] as const;

export const SYSTEM_METRICS_QUERY_KEY = (historyDays: number) =>
  ["system_metrics", historyDays] as const;

function workspaceMetricsPath(workspaceId: string, suffix: string): string {
  return `/workspaces/${encodeURIComponent(workspaceId)}/metrics/${suffix}`;
}

function isWorkspaceIdValid(
  workspaceId: string | null | undefined,
): workspaceId is string {
  return typeof workspaceId === "string" && workspaceId.length > 0;
}

/**
 * 拉工作区 metrics 概览(累计 + 今日 7 字段,见 OverviewOut)。
 */
export function useMetricsOverviewQuery(
  workspaceId: string | null | undefined,
) {
  const enabled = isWorkspaceIdValid(workspaceId);
  return useQuery<OverviewOut>({
    queryKey: METRICS_OVERVIEW_QUERY_KEY(workspaceId ?? ""),
    queryFn: () =>
      apiGet<OverviewOut>(workspaceMetricsPath(workspaceId as string, "overview")),
    enabled,
  });
}

/**
 * 拉每日聚合范围(可选 since/until,ISO 8601 date `YYYY-MM-DD`,inclusive)。
 *
 * 后端按日期 asc 排序;range 端点都为 None 时返全部历史(无过滤)。P4-F-02
 * 默认调用方传"最近 30 天"窗口,P4-F-03 加 UI 时间范围选择器后再透传。
 */
export function useMetricsDailyQuery(
  workspaceId: string | null | undefined,
  since: string | null = null,
  until: string | null = null,
) {
  const enabled = isWorkspaceIdValid(workspaceId);
  return useQuery<DailyListOut>({
    queryKey: METRICS_DAILY_QUERY_KEY(workspaceId ?? "", since, until),
    queryFn: () => {
      const qs = new URLSearchParams();
      if (since) qs.set("since", since);
      if (until) qs.set("until", until);
      const suffix = qs.toString() ? `daily?${qs.toString()}` : "daily";
      return apiGet<DailyListOut>(
        workspaceMetricsPath(workspaceId as string, suffix),
      );
    },
    enabled,
  });
}

/**
 * 分章详细(P4-F-03 用)。`order` 默认 `approved_at_desc`(后端默认相同)。
 */
export function useMetricsPerChapterQuery(
  workspaceId: string | null | undefined,
  order: PerChapterOrder = "approved_at_desc",
) {
  const enabled = isWorkspaceIdValid(workspaceId);
  return useQuery<PerChapterListOut>({
    queryKey: METRICS_PER_CHAPTER_QUERY_KEY(workspaceId ?? "", order),
    queryFn: () =>
      apiGet<PerChapterListOut>(
        workspaceMetricsPath(
          workspaceId as string,
          `per-chapter?order=${order}`,
        ),
      ),
    enabled,
  });
}

/**
 * 张力曲线(target vs actual,P4-F-03 用)。
 */
export function useMetricsTensionCurveQuery(
  workspaceId: string | null | undefined,
) {
  const enabled = isWorkspaceIdValid(workspaceId);
  return useQuery<TensionCurveOut>({
    queryKey: METRICS_TENSION_CURVE_QUERY_KEY(workspaceId ?? ""),
    queryFn: () =>
      apiGet<TensionCurveOut>(
        workspaceMetricsPath(workspaceId as string, "tension-curve"),
      ),
    enabled,
  });
}

/**
 * 成本按 writer_model 分组(P4-F-03 用)。
 */
export function useMetricsCostBreakdownQuery(
  workspaceId: string | null | undefined,
) {
  const enabled = isWorkspaceIdValid(workspaceId);
  return useQuery<CostBreakdownOut>({
    queryKey: METRICS_COST_BREAKDOWN_QUERY_KEY(workspaceId ?? ""),
    queryFn: () =>
      apiGet<CostBreakdownOut>(
        workspaceMetricsPath(workspaceId as string, "cost-breakdown"),
      ),
    enabled,
  });
}

/**
 * 记忆健康度历史(P4-B-09 落库,P4-F-05 详细 UI;本任务范围内不渲染)。
 */
export function useMetricsMemoryHealthQuery(
  workspaceId: string | null | undefined,
  limit = 50,
) {
  const enabled = isWorkspaceIdValid(workspaceId);
  return useQuery<MemoryHealthListOut>({
    queryKey: METRICS_MEMORY_HEALTH_QUERY_KEY(workspaceId ?? "", limit),
    queryFn: () =>
      apiGet<MemoryHealthListOut>(
        workspaceMetricsPath(
          workspaceId as string,
          `memory-health?limit=${limit}`,
        ),
      ),
    enabled,
  });
}

/**
 * 风格漂移历史(P4-B-10 stub 落库,P4-F-05 详细 UI;本任务范围内不渲染)。
 */
export function useMetricsStyleAuditsQuery(
  workspaceId: string | null | undefined,
  limit = 50,
) {
  const enabled = isWorkspaceIdValid(workspaceId);
  return useQuery<StyleAuditListOut>({
    queryKey: METRICS_STYLE_AUDITS_QUERY_KEY(workspaceId ?? "", limit),
    queryFn: () =>
      apiGet<StyleAuditListOut>(
        workspaceMetricsPath(
          workspaceId as string,
          `style-audits?limit=${limit}`,
        ),
      ),
    enabled,
  });
}

/**
 * 系统级 metrics(跨工作区,今日 + 最近 N 天,P4-F-02 范围内不渲染但 hook 预留)。
 */
export function useSystemMetricsQuery(historyDays = 30) {
  return useQuery<SystemMetricsOut>({
    queryKey: SYSTEM_METRICS_QUERY_KEY(historyDays),
    queryFn: () =>
      apiGet<SystemMetricsOut>(`/system/metrics?history_days=${historyDays}`),
  });
}
