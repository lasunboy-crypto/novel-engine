/**
 * Export API hooks(P6-F-03)。
 *
 * 端点对账(P6-B-04 / B-05 / B-06 全已落,见 web/src/api/types.ts):
 *
 * - POST `/api/v1/workspaces/{id}/exports` → `ExportCreateIn` body + 202
 *   `ExportCreateOut`(`{task_id, export_id, status="pending"}`)。前端订阅
 *   WS `task.{task_id}.progress` 接 4 stage(collecting / rendering /
 *   packaging / done)。
 * - GET `/api/v1/workspaces/{id}/exports` → `ExportListOut`(最近 10 条
 *   倒序,含 pending / running / done / failed 全状态)。
 * - GET `/api/v1/workspaces/{id}/exports/{export_id}/download` → 浏览器
 *   走原生 `<a href>` 触发文件下载;`status=done` 时可达,其他状态 →
 *   404 `EXPORT_NOT_FOUND`。
 *
 * 错误码(对齐 P6-B-04):
 * - 422 `EXPORT_FORMAT_UNSUPPORTED`:format 4 档外(理论 Pydantic 已拦)
 * - 422 `EXPORT_SCOPE_INVALID`:scope=volume 缺 volume_id / scope=range
 *   缺 start_chapter_id 或 end_chapter_id / range 含 invalidated 章节
 * - 404 `EXPORT_NOT_FOUND`:export_id 不存在或任务未完成
 *
 * ## queryKey 命名空间
 *
 * - 列表:`["exports", workspaceId]`
 *
 * mutation onSuccess + WS task done event 两个时机都触发 invalidate,让
 * ExportHistoryList 顶部"刷新"按钮无副作用复用同 key。
 */

import { useMutation, useQuery, useQueryClient } from "@tanstack/react-query";

import { apiGet, apiPost, getApiBaseURL } from "@/api/client";
import type { components } from "@/api/types";

export type ExportCreateIn = components["schemas"]["ExportCreateIn"];
export type ExportCreateOut = components["schemas"]["ExportCreateOut"];
export type ExportItemOut = components["schemas"]["ExportItemOut"];
export type ExportListOut = components["schemas"]["ExportListOut"];
export type ExportFormat = ExportCreateIn["format"];
export type ExportScope = ExportCreateIn["scope"];
export type ExportStatus = ExportItemOut["status"];

export const EXPORTS_QUERY_KEY = (workspaceId: string) =>
  ["exports", workspaceId] as const;

/** 4 档格式 + 中文显示标签(对齐 plan §P6-F-03 字面)。 */
export const EXPORT_FORMAT_OPTIONS: ReadonlyArray<{
  value: ExportFormat;
  label: string;
  hint: string;
}> = [
  {
    value: "md",
    label: "Markdown(单文件)",
    hint: "全书合并成一个 book.md",
  },
  {
    value: "chaptered_md",
    label: "Markdown(分章)",
    hint: "按卷 / 章拆分文件 + index.md",
  },
  {
    value: "epub",
    label: "EPUB",
    hint: "电子书标准格式,可用 Apple Books / calibre 打开",
  },
  {
    value: "docx",
    label: "Word(.docx)",
    hint: "微软 Word / WPS / LibreOffice 可打开",
  },
];

/** 3 档范围 + 中文显示标签。 */
export const EXPORT_SCOPE_OPTIONS: ReadonlyArray<{
  value: ExportScope;
  label: string;
  hint: string;
}> = [
  { value: "full", label: "全书", hint: "导出全部已写章节(跳过 invalidated)" },
  { value: "volume", label: "整卷", hint: "导出指定卷的全部章节" },
  {
    value: "range",
    label: "区间",
    hint: "从 start 到 end(含);区间含 invalidated 章节会被拒",
  },
];

/** 4 档任务状态 + 中文标签 + 配色。 */
export const EXPORT_STATUS_LABELS: Record<
  ExportStatus,
  { label: string; tone: "neutral" | "info" | "success" | "danger" }
> = {
  pending: { label: "排队中", tone: "neutral" },
  running: { label: "运行中", tone: "info" },
  done: { label: "已完成", tone: "success" },
  failed: { label: "失败", tone: "danger" },
};

/**
 * 列出工作区最近 10 次导出(对应 `GET /workspaces/{id}/exports`)。
 *
 * 空 workspaceId 时 `enabled=false`。后端按 created_at 倒序返,前端不再排序。
 */
export function useExportsListQuery(workspaceId: string | null | undefined) {
  const enabled = typeof workspaceId === "string" && workspaceId.length > 0;
  return useQuery<ExportListOut>({
    queryKey: EXPORTS_QUERY_KEY(workspaceId ?? ""),
    queryFn: () =>
      apiGet<ExportListOut>(
        `/workspaces/${encodeURIComponent(workspaceId ?? "")}/exports`,
      ),
    enabled,
  });
}

interface CreateExportArgs {
  workspaceId: string;
  body: ExportCreateIn;
}

/**
 * 启动新导出任务(对应 `POST /workspaces/{id}/exports`)。
 *
 * 后端返 202 + `{task_id, export_id, status: "pending"}`;前端拿 task_id 订阅
 * WS `task.{id}.progress` 看 4 stage 增量。onSuccess 后立刻 invalidate
 * `["exports", workspaceId]` 让历史列表显示新行(status=pending)。
 */
export function useCreateExportMutation() {
  const queryClient = useQueryClient();
  return useMutation<ExportCreateOut, Error, CreateExportArgs>({
    mutationFn: ({ workspaceId, body }) =>
      apiPost<ExportCreateOut, ExportCreateIn>(
        `/workspaces/${encodeURIComponent(workspaceId)}/exports`,
        body,
      ),
    onSuccess: (_data, variables) => {
      void queryClient.invalidateQueries({
        queryKey: EXPORTS_QUERY_KEY(variables.workspaceId),
      });
    },
  });
}

/**
 * 拼下载链接,直接挂 `<a href>` 触发浏览器原生下载。
 *
 * 后端 FileResponse + Content-Disposition: attachment + filename={ws}-{exp}.zip
 * (对齐 P6-B-04 impl §自决项 10)。**不走 fetch + blob**,因为:
 * ① 走原生 <a> 浏览器自动用 filename 头取名 + 进度条;
 * ② 走 blob 需把整个 zip 读进内存,大体量(>50MB)会卡;
 * ③ 错误状态(404 等)交给浏览器原生新窗口处理足够。
 */
export function buildExportDownloadUrl(
  workspaceId: string,
  exportId: string,
): string {
  return `${getApiBaseURL()}/workspaces/${encodeURIComponent(
    workspaceId,
  )}/exports/${encodeURIComponent(exportId)}/download`;
}
