/**
 * Validator 报告 API hook(P6-F-01,P6-B-11 端点接通)。
 *
 * 已含 hook:
 * - `useChapterValidatorReportQuery(workspaceId, chapterId)` →
 *   `GET /api/v1/chapters/{id}/validators/latest`(P6-B-11)
 *
 * ## 端点契约
 *
 * 端点现跑现产 [HARD] validator pipeline 报告(P4-B-06 / P4-B-07 落地的
 * 4+6 个 validator),~ms 级开销。前置:章节卡必存在(否则 404
 * `CHAPTER_CARD_NOT_FOUND`)+ `final.md` 必存在(否则 404
 * `VALIDATOR_REPORT_NOT_FOUND`,前端据此提示用户先 `:write`)。
 *
 * ## queryKey 命名空间约定
 *
 * `["chapter-validator-report", workspaceId, chapterId]` — 与 `chapter-card` /
 * `chapter-versions` 平行,不复用 `invalidateChapterArtifacts`(write / rewrite
 * 等动作完成后,UI 上的 ChapterCardReviewPage 一般已不是当前页,本 query 自动
 * stale 时重拉即可)。
 *
 * ## 404 短路
 *
 * `VALIDATOR_REPORT_NOT_FOUND` 是常态分支(用户刚审完章节卡尚未触发 write),
 * 不应当作 hard error。`retry: false`(避免重复打后端),UI 层根据 `error` 的
 * ApiError.code 区分"无报告"(显示"先 :write")vs "其他错误"(显示错误信息)。
 */

import { useQuery } from "@tanstack/react-query";

import { apiGet } from "@/api/client";
import type { components } from "@/api/types";

export type ValidatorReportOut = components["schemas"]["ValidatorReportOut"];
export type ValidatorResultOut = components["schemas"]["ValidatorResultOut"];

export const CHAPTER_VALIDATOR_REPORT_QUERY_KEY = (
  workspaceId: string,
  chapterId: string,
) => ["chapter-validator-report", workspaceId, chapterId] as const;

/**
 * 拉取章节最近一次 validator 报告(P6-B-11)。
 *
 * `workspaceId` / `chapterId` 任一缺时 `enabled=false`,query 不发。
 *
 * 错误码处理:`VALIDATOR_REPORT_NOT_FOUND` (404) 表示"章节卡有 + final.md
 * 没有",UI 应显示"先 :write 写出正文"提示,不是真错误;`CHAPTER_CARD_NOT_FOUND`
 * (404) 是章节卡都没有,理论上 ChapterCardReviewPage 上层 cardQuery 会先报错,
 * 本 hook 不应被启用。
 */
export function useChapterValidatorReportQuery(
  workspaceId: string | undefined,
  chapterId: string | undefined,
) {
  return useQuery<ValidatorReportOut>({
    queryKey: CHAPTER_VALIDATOR_REPORT_QUERY_KEY(
      workspaceId ?? "",
      chapterId ?? "",
    ),
    queryFn: () =>
      apiGet<ValidatorReportOut>(
        `/chapters/${encodeURIComponent(chapterId!)}/validators/latest`,
        { workspaceId },
      ),
    enabled: Boolean(workspaceId && chapterId),
    retry: false,
  });
}
