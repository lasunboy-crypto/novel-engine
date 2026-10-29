/**
 * ChapterSummary API hooks(v1.0.x wave 4 session 5)。
 *
 * 端点(2 个,workspace_id 走 URL path 对齐 outline.ts):
 *
 * - GET `/workspaces/{id}/chapter-summaries` — 列出所有摘要(chapter_id ASC)
 * - PUT `/workspaces/{id}/chapter-summaries/{chapter_id}` — PATCH 6 字段
 *
 * 用途:前端 OutlinePage 第 6 tab(`ChapterSummaryTabContent`)展示 + 编辑
 * LLM 抽取的章节摘要。摘要 LLM 抽取在后端 ChapterService / ChapterRewindService
 * 的 reviewer_pass / force_pass hook 内自动触发,本 API 只暴露用户事后编辑入口。
 */

import { useMutation, useQuery, useQueryClient } from "@tanstack/react-query";

import { apiGet, apiPut } from "@/api/client";
import type { components } from "@/api/types";

export type ChapterSummaryOut = components["schemas"]["ChapterSummaryOut"];
export type ChapterSummaryUpdateIn =
  components["schemas"]["ChapterSummaryUpdateIn"];

export const CHAPTER_SUMMARIES_QUERY_KEY = (workspaceId: string) =>
  ["chapter-summaries", workspaceId] as const;

/**
 * 列出工作区下所有章节摘要(chapter_id ASC)。空 workspaceId 时禁用。
 */
export function useChapterSummariesQuery(workspaceId: string | undefined) {
  return useQuery<ChapterSummaryOut[]>({
    queryKey: CHAPTER_SUMMARIES_QUERY_KEY(workspaceId ?? ""),
    queryFn: () =>
      apiGet<ChapterSummaryOut[]>(
        `/workspaces/${encodeURIComponent(workspaceId ?? "")}/chapter-summaries`,
      ),
    enabled: typeof workspaceId === "string" && workspaceId.length > 0,
  });
}

/**
 * 编辑单条章节摘要(PATCH 语义,只更新非 null 字段)。成功后 invalidate
 * `CHAPTER_SUMMARIES_QUERY_KEY` 让列表重 fetch。
 */
export function useUpdateChapterSummaryMutation() {
  const queryClient = useQueryClient();
  return useMutation<
    ChapterSummaryOut,
    Error,
    {
      workspaceId: string;
      chapterId: string;
      body: ChapterSummaryUpdateIn;
    }
  >({
    mutationFn: ({ workspaceId, chapterId, body }) =>
      apiPut<ChapterSummaryOut, ChapterSummaryUpdateIn>(
        `/workspaces/${encodeURIComponent(workspaceId)}/chapter-summaries/${encodeURIComponent(chapterId)}`,
        body,
      ),
    onSuccess: (_data, variables) => {
      void queryClient.invalidateQueries({
        queryKey: CHAPTER_SUMMARIES_QUERY_KEY(variables.workspaceId),
      });
    },
  });
}
