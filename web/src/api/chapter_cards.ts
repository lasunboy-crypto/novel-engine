/**
 * 章节卡审核流 API hooks(P5-F-06,P5-B-07 / P5-B-08 端点接通)。
 *
 * 已含 hook:
 * - `useApproveCardMutation()` → `POST /chapters/{id}/card:approve`(P5-B-07)
 * - `useRejectCardMutation()` → `POST /chapters/{id}/card:reject`(P5-B-07)
 * - `useUploadCardMutation()` → `POST /chapters/{id}/card`(P5-B-08,整 JSON
 *   上传,与既有 PUT 互补 — POST 同步 ORM 行,PUT 只写文件)
 *
 * **复用既有 hook**(不在本文件重造):
 * - `useChapterCardQuery` / `useUpdateChapterCardMutation` 在 `api/chapters.ts`
 *
 * ## 命名空间
 *
 * 模块文件名 `chapter_cards.ts` 而非合并入 `chapters.ts`,与 P5-B-07/P5-B-08
 * 后端 `chapter_card_service.py` 同语义边界(审核 / 上传是 card 资源的状态机
 * 操作,不是 chapter 主体行为);后续 P6+ 加 `card:force-review` 等动作时
 * 在本文件直接扩,不污染 chapters.ts。
 *
 * ## invalidate 策略
 *
 * approve / reject 后单卡 + 列表都可能改(`card_status` 字段进 ChapterCardOut
 * + ChaptersBoardPage 未来若引入 card_status 过滤需重拉列表);沿用
 * `invalidateChapterArtifacts` 全量 invalidate,代价小、保守稳。upload 触发
 * 同样的 invalidate(整张卡覆盖)。
 */

import { useMutation, useQueryClient } from "@tanstack/react-query";

import { apiPost } from "@/api/client";
import {
  invalidateChapterArtifacts,
  type ChapterCardOut,
} from "@/api/chapters";
import type { components } from "@/api/types";

export type ChapterCardApproveIn =
  components["schemas"]["ChapterCardApproveIn"];
export type ChapterCardGenerateIn =
  components["schemas"]["ChapterCardGenerateIn"];
export type ChapterCardRejectIn = components["schemas"]["ChapterCardRejectIn"];
export type ChapterCardStatusOut =
  components["schemas"]["ChapterCardStatusOut"];
export type ChapterCardRetryReferencesIn =
  components["schemas"]["ChapterCardRetryReferencesIn"];
// v1.0.x wave 5 session 4:retry_references 响应加 cascade 字段
export type ChapterCardRetryReferencesOut =
  components["schemas"]["ChapterCardRetryReferencesOut"];
export type TaskInfo = components["schemas"]["TaskInfo"];
interface CardActionArgs {
  workspaceId: string;
  chapterId: string;
}

interface GenerateCardArgs extends CardActionArgs {
  volumeId: string;
  chapterNumber: number;
}

/**
 * 生成缺失的单章卡(对应 ``POST /chapters/{id}/card:generate``)。
 *
 * 正式线性 Run 在上一章富记忆完成后通常自动走同一后端能力；若 Provider
 * 返回不可解析 JSON，run 会保留原现场并要求从章节卡审核页修复。该 mutation
 * 只负责启动既有异步任务，caller 必须等待 task WS 终态后再重读卡片。
 */
export function useGenerateCardMutation() {
  return useMutation<TaskInfo, Error, GenerateCardArgs>({
    mutationFn: ({ workspaceId, chapterId, volumeId, chapterNumber }) =>
      apiPost<TaskInfo, ChapterCardGenerateIn>(
        `/chapters/${encodeURIComponent(chapterId)}/card:generate`,
        { volume_id: volumeId, chapter_number: chapterNumber },
        { workspaceId },
      ),
  });
}

interface RejectCardArgs extends CardActionArgs {
  /** 可选退回原因(后端持久化到 ChapterCardORM.rejection_notes)。 */
  rejectionNotes?: string | null;
}

interface UploadCardArgs extends CardActionArgs {
  /** 整张 ChapterCard JSON(`chapter_id` 必须与 URL 一致,否则 422)。 */
  card: ChapterCardOut;
}

interface RetryReferencesArgs extends CardActionArgs {
  /** 章节卡所属卷 ID(P6-B-10 服务层定位章节卡 SSOT 文件)。 */
  volumeId: string;
  /** 卷内章序号(从 1 起;服务层与 chapterId 联合定位)。 */
  chapterNumber: number;
}

/**
 * 审批通过(对应 `POST /api/v1/chapters/{id}/card:approve`)。
 *
 * 后端 P5-B-07:`card_status` 从 `draft` / `pending_review` / `rejected` 翻
 * `approved`,记 `card_approved_at`,清除上次 `rejection_notes`;从 `approved`
 * 再 approve 抛 409 `CARD_INVALID_STATE_TRANSITION`。同步 patch SSOT 文件 +
 * publish WS event 到 `chapter.{id}.card.events`(本前端不直接订阅该频道,
 * 后端响应到达后 mutation onSuccess 走 query invalidate 同步,避免双源)。
 */
export function useApproveCardMutation() {
  const queryClient = useQueryClient();
  return useMutation<ChapterCardStatusOut, Error, CardActionArgs>({
    mutationFn: ({ workspaceId, chapterId }) =>
      apiPost<ChapterCardStatusOut, ChapterCardApproveIn>(
        `/chapters/${encodeURIComponent(chapterId)}/card:approve`,
        {},
        { workspaceId },
      ),
    onSuccess: (_data, { workspaceId, chapterId }) => {
      invalidateChapterArtifacts(queryClient, workspaceId, chapterId);
    },
  });
}

/**
 * 退回修改(对应 `POST /api/v1/chapters/{id}/card:reject`)。
 *
 * 后端 P5-B-07:`card_status` 从 `draft` / `pending_review` / `approved` 翻
 * `rejected`;`rejection_notes` 写入 ORM(可选)。从 `rejected` 再 reject 抛
 * 409 `CARD_INVALID_STATE_TRANSITION`。
 */
export function useRejectCardMutation() {
  const queryClient = useQueryClient();
  return useMutation<ChapterCardStatusOut, Error, RejectCardArgs>({
    mutationFn: ({ workspaceId, chapterId, rejectionNotes }) =>
      apiPost<ChapterCardStatusOut, ChapterCardRejectIn>(
        `/chapters/${encodeURIComponent(chapterId)}/card:reject`,
        { rejection_notes: rejectionNotes ?? null },
        { workspaceId },
      ),
    onSuccess: (_data, { workspaceId, chapterId }) => {
      invalidateChapterArtifacts(queryClient, workspaceId, chapterId);
    },
  });
}

/**
 * 上传整张 ChapterCard JSON(对应 `POST /api/v1/chapters/{id}/card`,P5-B-08)。
 *
 * 与 `useUpdateChapterCardMutation`(PUT)互补:**POST 同步落 SSOT 文件 + ORM
 * 行,PUT 仅写文件**。前端"用户从本地选 JSON 文件上传"场景走 POST,语义最贴
 * 切创建/覆盖;"在编辑器里改完保存"场景走 PUT(保留既有路径)。
 *
 * 错误码:`CHAPTER_ID_MISMATCH (422)` — body 内 `chapter_id` 与 URL 不一致;
 * Pydantic 校验失败 → 422 `VALIDATION_FAILED`;`MISSING_WORKSPACE_HEADER` 由
 * client 层注入 header 保证不发生。
 */
export function useUploadCardMutation() {
  const queryClient = useQueryClient();
  return useMutation<ChapterCardOut, Error, UploadCardArgs>({
    mutationFn: ({ workspaceId, chapterId, card }) =>
      apiPost<ChapterCardOut, ChapterCardOut>(
        `/chapters/${encodeURIComponent(chapterId)}/card`,
        card,
        { workspaceId },
      ),
    onSuccess: (_data, { workspaceId, chapterId }) => {
      invalidateChapterArtifacts(queryClient, workspaceId, chapterId);
    },
  });
}

/**
 * 重试章节卡引用合法性(对应 `POST /api/v1/chapters/{id}:retry-references`,
 * P6-B-10 → v1.0.x wave 5 session 4)。
 *
 * 服务层走 `ChapterCardService.retry_references`:LLM 重新生成章节卡,
 * agent prompt 顶部注入"已知 ID 列表"banner + `character_voices` /
 * `plot_threads` 过滤到只剩工作区 SQLite 表实有 ID。**单次重试**(用户已点
 * 显式按钮 = 一次触发),仍失败返 422 `REFERENCE_VALIDATION_FAILED`,
 * `details` 含 `unknown_character_ids` / `unknown_thread_ids`(供 UI 引导
 * 用户去 character / thread CRUD 页修复)。
 *
 * 新生卡默认 `card_status="draft"` 覆盖既有卡;前端按需调 `card:approve`
 * 流转。
 *
 * **v1.0.x wave 5 session 4 升级**:响应改 `ChapterCardRetryReferencesOut`,
 * 含 `card` + `invalidated_chapter_ids` + `cascade_regenerate_task_id`。
 * 后端注入同卷其他 approved 章 must_achieve 作 forbidden_phrases 防同质化,
 * 并自动注册 BackgroundTask 跑后续章节卡级联重生 — caller 拿
 * `cascade_regenerate_task_id` 订阅 `task.{id}.progress` 频道接进度。
 * onSuccess 时遍历 `invalidated_chapter_ids` 逐个 invalidate(对照
 * `useRewindMutation` 同型),让 UI 同步刷新被级联章节的卡 query。
 */
export function useRetryReferencesMutation() {
  const queryClient = useQueryClient();
  return useMutation<ChapterCardRetryReferencesOut, Error, RetryReferencesArgs>({
    mutationFn: ({ workspaceId, chapterId, volumeId, chapterNumber }) =>
      apiPost<ChapterCardRetryReferencesOut, ChapterCardRetryReferencesIn>(
        `/chapters/${encodeURIComponent(chapterId)}:retry-references`,
        { volume_id: volumeId, chapter_number: chapterNumber },
        { workspaceId },
      ),
    onSuccess: (data, { workspaceId, chapterId }) => {
      invalidateChapterArtifacts(queryClient, workspaceId, chapterId);
      // v1.0.x wave 5 session 4:cascade 失效章节也一并 invalidate(对照
      // useRewindMutation 同模式)。cards/ORM 在 BackgroundTask 跑完前是
      // 旧值,但 UI 在 task.done 后再 manual refetch 也行;本 invalidate
      // 即时让 ChaptersBoardPage 列表 query 失效,使浏览器一旦跳回看板自
      // 然重新拉。
      for (const invalidatedId of data.invalidated_chapter_ids) {
        invalidateChapterArtifacts(queryClient, workspaceId, invalidatedId);
      }
    },
  });
}
