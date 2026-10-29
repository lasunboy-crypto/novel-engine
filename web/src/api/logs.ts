/**
 * Logs API hooks(P4-F-04 LogsPage 列表 + Trace 视图)。
 *
 * 后端入口(OBSERVABILITY §2.6 字面 + P4-B-05 impl):
 *
 * - `GET /api/v1/logs?workspace_id=&chapter_id=&trace_id=&request_id=&event=&level=&since=&until=&page=&page_size=`
 * - `GET /api/v1/logs/trace/{trace_or_request_id}`(智能分流 `req_` 前缀 → request_id)
 * - WS `/api/v1/logs/stream`(P4-F-04 范围内不接,留 P4-F-05)
 *
 * ## URL 形态(系统级,无 workspace 嵌套)
 *
 * 日志查询是跨工作区系统级资源(对齐 P4-B-05 路由位置选择),URL 不嵌
 * `/workspaces/{id}`;workspace 过滤通过 query 参数 `workspace_id=`。
 *
 * ## queryKey 命名空间
 *
 * - `["logs_list", filters]` — filters 是稳定字段顺序对象(序列化作为
 *   react-query 依赖,变更任一字段都重新请求)
 * - `["logs_trace", traceOrRequestId]`
 *
 * ## 空数据语义
 *
 * P4-B-05 决策 jsonl 写入接通留观,本任务下后端 logs 目录可能为空 →
 * 端点返 200 + `items: []`,前端走 empty 分支(不视为错误)。
 *
 * ## P4-F-04 只读
 *
 * 全部端点 GET,仅暴露 `useXxxQuery` hook;无 mutation。WS `/logs/stream`
 * 实时模式 P4-F-05 接。
 */

import { useQuery } from "@tanstack/react-query";

import { apiGet } from "@/api/client";
import type { components } from "@/api/types";

export type LogEntry = components["schemas"]["LogEntryOut"];
export type LogQueryOut = components["schemas"]["LogQueryOut"];
export type TraceViewOut = components["schemas"]["TraceViewOut"];

/**
 * `GET /logs` 查询筛选字段(对齐后端 `LogQueryFilters`)。
 *
 * 所有字段可选;空字符串视同未设置(URLSearchParams 不附加该 key)。
 * `since` / `until` ISO 8601 datetime(后端 FastAPI `_dt.datetime` 自动解析)。
 */
export interface LogsListFilters {
  workspace_id?: string | null;
  chapter_id?: string | null;
  trace_id?: string | null;
  request_id?: string | null;
  event?: string | null;
  level?: string | null;
  since?: string | null;
  until?: string | null;
  page?: number;
  page_size?: number;
}

export const LOGS_LIST_QUERY_KEY = (filters: LogsListFilters) =>
  [
    "logs_list",
    filters.workspace_id ?? null,
    filters.chapter_id ?? null,
    filters.trace_id ?? null,
    filters.request_id ?? null,
    filters.event ?? null,
    filters.level ?? null,
    filters.since ?? null,
    filters.until ?? null,
    filters.page ?? 1,
    filters.page_size ?? 50,
  ] as const;

export const LOGS_TRACE_QUERY_KEY = (traceOrRequestId: string) =>
  ["logs_trace", traceOrRequestId] as const;

function buildLogsListPath(filters: LogsListFilters): string {
  const qs = new URLSearchParams();
  const push = (key: string, value: string | number | null | undefined) => {
    if (value === null || value === undefined) return;
    const s = typeof value === "number" ? String(value) : value;
    if (s.length === 0) return;
    qs.set(key, s);
  };
  push("workspace_id", filters.workspace_id);
  push("chapter_id", filters.chapter_id);
  push("trace_id", filters.trace_id);
  push("request_id", filters.request_id);
  push("event", filters.event);
  push("level", filters.level);
  push("since", filters.since);
  push("until", filters.until);
  push("page", filters.page ?? 1);
  push("page_size", filters.page_size ?? 50);
  const query = qs.toString();
  return query ? `/logs?${query}` : "/logs";
}

/**
 * 日志查询 hook(分页 + 多字段筛选)。
 *
 * 后端按 `timestamp desc` 排序;空筛选返最新 page_size 条。
 */
export function useLogsListQuery(filters: LogsListFilters) {
  return useQuery<LogQueryOut>({
    queryKey: LOGS_LIST_QUERY_KEY(filters),
    queryFn: () => apiGet<LogQueryOut>(buildLogsListPath(filters)),
  });
}

/**
 * Trace 视图 hook(章节级 / API 级智能分流)。
 *
 * `traceOrRequestId` 为空时 `enabled=false`,避免 ` /logs/trace/` 404。
 */
export function useLogsTraceQuery(traceOrRequestId: string | null | undefined) {
  const enabled =
    typeof traceOrRequestId === "string" && traceOrRequestId.length > 0;
  return useQuery<TraceViewOut>({
    queryKey: LOGS_TRACE_QUERY_KEY(traceOrRequestId ?? ""),
    queryFn: () =>
      apiGet<TraceViewOut>(
        `/logs/trace/${encodeURIComponent(traceOrRequestId as string)}`,
      ),
    enabled,
  });
}
