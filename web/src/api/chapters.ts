/**
 * 章节 API hooks(P1-F-10 起,P1-F-12 扩单卡 / 版本系列,P1-F-14 扩 3 mutation)。
 *
 * 已含 hook:
 * - `useChapterListQuery(workspaceId)` → `GET /api/v1/chapters`(P1-B-25)
 * - `useChapterCardQuery(workspaceId, chapterId)` → `GET /chapters/{id}/card`
 * - `useChapterVersionsQuery(workspaceId, chapterId)` → `GET /chapters/{id}/versions`
 * - `useChapterVersionQuery(workspaceId, chapterId, versionId)` →
 *   `GET /chapters/{id}/versions/{version_id}`
 * - `useWriteChapterMutation()` → `POST /chapters/{id}:write`(P1-F-14)
 * - `useRewriteChapterMutation()` → `POST /chapters/{id}:rewrite`(P1-F-14)
 * - `useReviseChapterMutation()` → `POST /chapters/{id}:revise`(P1-F-14)
 *
 * ## X-Workspace-Id 注入
 *
 * 章节路由要求 `X-Workspace-Id` header(P1-B-19 决策 1 工作区路由 vs 章节路由
 * 的边界)。本模块通过 `apiGet(path, { workspaceId })` 选项注入,不引入 store
 * 直接耦合;调用方(组件)从 zustand 取 active id 并显式传入,保持 client
 * stateless(P1-F-03 设计取舍延伸)。
 *
 * ## queryKey 命名空间约定
 *
 * - 列表:`["chapters", workspaceId]`
 * - 单卡:`["chapter-card", workspaceId, chapterId]`
 * - 版本号列表:`["chapter-versions", workspaceId, chapterId]`
 * - 单版本正文:`["chapter-version", workspaceId, chapterId, versionId]`
 *
 * 选不同前缀(`chapters` vs `chapter-card` vs `chapter-versions` vs
 * `chapter-version`),让 P1-F-NN mutation invalidate 时按粒度分别命中:
 * `:write` / `:rewrite` / `:revise` 应 invalidate 列表 + 版本号列表 +
 * 单版本正文 +(P1-F-14 起补)单卡(因 ChapterCardOut 携带 latest_version_id /
 * has_final / version_count 等会随写作变更);`:put-card` 应只 invalidate
 * 单卡 + 列表(版本不变)。
 *
 * ## `enabled` 短路约定
 *
 * 所有 hook 在缺关键参(`workspaceId` / `chapterId` / `versionId` 任一空)时
 * `enabled=false`,query 不发,UI 状态保持 `isPending`。组件层做"参数齐全前
 * 不渲染数据栏"的条件分支(对齐 ChapterEditorPage 三栏分别处理 loading 套
 * 路,P1-F-12 决策 5)。
 */

import {
  useMutation,
  useQuery,
  useQueryClient,
} from "@tanstack/react-query";

import { apiGet, apiPost, apiPut } from "@/api/client";
import type { components } from "@/api/types";

export type ChapterListItem = components["schemas"]["ChapterListItem"];
export type ChapterCardOut = components["schemas"]["ChapterCardOut"];
export type ChapterVersionList = components["schemas"]["ChapterVersionList"];
export type ChapterVersionContent =
  components["schemas"]["ChapterVersionContent"];
export type ChapterWriteIn = components["schemas"]["ChapterWriteIn"];
export type ChapterRewriteIn = components["schemas"]["ChapterRewriteIn"];
export type ChapterReReviewIn = components["schemas"]["ChapterReReviewIn"];
export type ChapterReviseIn = components["schemas"]["ChapterReviseIn"];
export type ReviewIssueIn = components["schemas"]["ReviewIssueIn"];
export type TaskInfoChapterWrite =
  components["schemas"]["TaskInfoChapterWrite"];
export type TaskInfoChapterRevise =
  components["schemas"]["TaskInfoChapterRevise"];
// P6-F-06:rewind / force-pass / unblock / unblock-all 端点(P6-B-01 已落)。
export type ChapterRewindIn = components["schemas"]["ChapterRewindIn"];
export type ChapterRewindOut = components["schemas"]["ChapterRewindOut"];
export type ChapterForcePassIn = components["schemas"]["ChapterForcePassIn"];
export type ChapterForcePassOut = components["schemas"]["ChapterForcePassOut"];
export type ChapterUnblockIn = components["schemas"]["ChapterUnblockIn"];
export type ChapterUnblockOut = components["schemas"]["ChapterUnblockOut"];
export type WorkspaceUnblockAllIn =
  components["schemas"]["WorkspaceUnblockAllIn"];
export type WorkspaceUnblockAllOut =
  components["schemas"]["WorkspaceUnblockAllOut"];
export type WorkspaceResetStaleIn =
  components["schemas"]["WorkspaceResetStaleIn"];
export type WorkspaceResetStaleOut =
  components["schemas"]["WorkspaceResetStaleOut"];
// v1.1 acceptance 修复2:孤儿章人工解决流程(状态详情 + 人工编辑正文)。
export type ChapterStatusOut = components["schemas"]["ChapterStatusOut"];
export type ChapterContentUpdateIn =
  components["schemas"]["ChapterContentUpdateIn"];
export type ChapterContentUpdateOut =
  components["schemas"]["ChapterContentUpdateOut"];

export const CHAPTERS_QUERY_KEY = (workspaceId: string) =>
  ["chapters", workspaceId] as const;

export const CHAPTER_CARD_QUERY_KEY = (
  workspaceId: string,
  chapterId: string,
) => ["chapter-card", workspaceId, chapterId] as const;

export const CHAPTER_VERSIONS_QUERY_KEY = (
  workspaceId: string,
  chapterId: string,
) => ["chapter-versions", workspaceId, chapterId] as const;

export type ReviewSummaryEntry = components["schemas"]["ReviewSummaryEntry"];

// B-01 hot-fix(2026-05-19):新增持久化审稿历史查询(替代 task_registry LRU)
export const CHAPTER_REVIEW_HISTORY_QUERY_KEY = (
  workspaceId: string,
  chapterId: string,
) => ["chapter-review-history", workspaceId, chapterId] as const;

export const CHAPTER_VERSION_QUERY_KEY = (
  workspaceId: string,
  chapterId: string,
  versionId: number,
) => ["chapter-version", workspaceId, chapterId, versionId] as const;

// v1.1 w3-s7 H7:章节各版本风格指纹时序(漂移折线图数据源)
export const CHAPTER_STYLE_DRIFT_QUERY_KEY = (
  workspaceId: string,
  chapterId: string,
) => ["chapter-style-drift", workspaceId, chapterId] as const;

// v1.1 acceptance 修复2:章节状态详情(escalation_details 孤儿章裁决上下文)
export const CHAPTER_STATUS_QUERY_KEY = (
  workspaceId: string,
  chapterId: string,
) => ["chapter-status", workspaceId, chapterId] as const;

export type StyleDriftSeries = components["schemas"]["StyleDriftSeries"];
export type StyleFingerprintPoint =
  components["schemas"]["StyleFingerprintPoint"];

/**
 * 列出工作区内全部章节(对应 `GET /api/v1/chapters`)。
 *
 * `workspaceId` 为 null/undefined/空字符串时 `enabled=false`,query 不发——
 * 在工作区列表空 / WorkspaceDetailPage 直链 :id 不存在等场景安全短路。
 *
 * 排序与坏卡处置由后端 `chapter_service.list_chapters` 决定:按
 * `chapter_number` 升序,损坏 / schema 不符的章节卡 structlog warn 跳过返
 * 剩余合法卡(P1-B-25 决策 2)。前端不做二次排序。
 */
export function useChapterListQuery(workspaceId: string | null | undefined) {
  const enabled = typeof workspaceId === "string" && workspaceId.length > 0;
  return useQuery<ChapterListItem[]>({
    queryKey: CHAPTERS_QUERY_KEY(workspaceId ?? ""),
    queryFn: () =>
      apiGet<ChapterListItem[]>("/chapters", {
        workspaceId: workspaceId as string,
      }),
    enabled,
  });
}

/**
 * 读单张章节卡(对应 `GET /api/v1/chapters/{chapter_id}/card`)。
 *
 * 任一参为 null/undefined/空字符串 → `enabled=false`,query 不发。
 *
 * 错误码语义(后端 P1-B-19 决策 3 双轨):
 * - 文件不存在 → 404 `CHAPTER_CARD_NOT_FOUND`
 * - JSON 损坏 / schema 不符 → 422 `CHAPTER_CARD_INVALID`
 *
 * 调用方(ChapterCardPanel)按 `error.code` 区分提示文案,见 P1-F-12 决策 5。
 */
export function useChapterCardQuery(
  workspaceId: string | null | undefined,
  chapterId: string | null | undefined,
) {
  const enabled =
    typeof workspaceId === "string" &&
    workspaceId.length > 0 &&
    typeof chapterId === "string" &&
    chapterId.length > 0;
  return useQuery<ChapterCardOut>({
    queryKey: CHAPTER_CARD_QUERY_KEY(workspaceId ?? "", chapterId ?? ""),
    queryFn: () =>
      apiGet<ChapterCardOut>(
        `/chapters/${encodeURIComponent(chapterId as string)}/card`,
        { workspaceId: workspaceId as string },
      ),
    enabled,
  });
}

/**
 * 列出章节所有版本号(对应 `GET /api/v1/chapters/{chapter_id}/versions`)。
 *
 * P1 阶段后端从文件系统推断,只返 `version_id` 数组(`v1.md` / `v2.md` /
 * ...,按 1..N 升序;P1-B-19 决策 6 字面)。无版本(章节未写作)→ 返空数组,
 * 不抛错。
 *
 * 调用方(ChapterContentPanel)选 `versions[versions.length - 1].version_id`
 * 作为"最新版"传给 `useChapterVersionQuery`,P1-F-12 决策 3。
 */
export function useChapterVersionsQuery(
  workspaceId: string | null | undefined,
  chapterId: string | null | undefined,
) {
  const enabled =
    typeof workspaceId === "string" &&
    workspaceId.length > 0 &&
    typeof chapterId === "string" &&
    chapterId.length > 0;
  return useQuery<ChapterVersionList>({
    queryKey: CHAPTER_VERSIONS_QUERY_KEY(workspaceId ?? "", chapterId ?? ""),
    queryFn: () =>
      apiGet<ChapterVersionList>(
        `/chapters/${encodeURIComponent(chapterId as string)}/versions`,
        { workspaceId: workspaceId as string },
      ),
    enabled,
  });
}

/**
 * 查章节各版本风格指纹时序(对应 `GET /api/v1/chapters/{id}/style-drift`,
 * v1.1 w3-s7 H7)。
 *
 * 后端读 `chapter_versions.style_fingerprint`(G5 落盘),按 version_id 升序返
 * 每版本 4 数值维度 + 主导人称。无 session_factory / 无版本行 → `points` 空;
 * 旧版本无指纹 → `has_fingerprint=false` + 维度 null。前端折线图据此展示
 * v1→vN 漂移趋势,缺指纹点用 `connectNulls` 跨过。
 */
export function useChapterStyleDriftQuery(
  workspaceId: string | null | undefined,
  chapterId: string | null | undefined,
) {
  const enabled =
    typeof workspaceId === "string" &&
    workspaceId.length > 0 &&
    typeof chapterId === "string" &&
    chapterId.length > 0;
  return useQuery<StyleDriftSeries>({
    queryKey: CHAPTER_STYLE_DRIFT_QUERY_KEY(workspaceId ?? "", chapterId ?? ""),
    queryFn: () =>
      apiGet<StyleDriftSeries>(
        `/chapters/${encodeURIComponent(chapterId as string)}/style-drift`,
        { workspaceId: workspaceId as string },
      ),
    enabled,
  });
}

/**
 * B-01 hot-fix(2026-05-19):查章节持久化审稿历史。
 *
 * 对应 `GET /api/v1/chapters/{chapter_id}/review-history`(从 review_results
 * 表查,跨服务器重启 / 页面刷新均稳定保留)。替代 v1 期前端走进程内
 * task_registry LRU 的丢失路径 — `ReviewHistoryPanel` 切到本 hook。
 *
 * 工作区 / 章节不存在 / 无审稿历史 → 后端返空列表 200(不抛 404),前端
 * 渲染"暂无审稿历史"hint。
 */
export function useChapterReviewHistoryQuery(
  workspaceId: string | null | undefined,
  chapterId: string | null | undefined,
) {
  const enabled =
    typeof workspaceId === "string" &&
    workspaceId.length > 0 &&
    typeof chapterId === "string" &&
    chapterId.length > 0;
  return useQuery<ReviewSummaryEntry[]>({
    queryKey: CHAPTER_REVIEW_HISTORY_QUERY_KEY(
      workspaceId ?? "",
      chapterId ?? "",
    ),
    queryFn: () =>
      apiGet<ReviewSummaryEntry[]>(
        `/chapters/${encodeURIComponent(chapterId as string)}/review-history`,
        { workspaceId: workspaceId as string },
      ),
    enabled,
  });
}

/**
 * 读单个章节版本正文(对应 `GET /api/v1/chapters/{chapter_id}/versions/{version_id}`)。
 *
 * `versionId` 为 null/undefined/0/负数 → `enabled=false`(后端 schema
 * `version_id ge=1`,客户端先短路);`workspaceId` / `chapterId` 缺亦短路。
 *
 * 错误:文件不存在 → 后端 ChapterVersioningError → 全局翻译表映射为 409
 * `CHAPTER_VERSION_CONFLICT`(P1-B-19 决策 6 字面)。
 */
export function useChapterVersionQuery(
  workspaceId: string | null | undefined,
  chapterId: string | null | undefined,
  versionId: number | null | undefined,
) {
  const enabled =
    typeof workspaceId === "string" &&
    workspaceId.length > 0 &&
    typeof chapterId === "string" &&
    chapterId.length > 0 &&
    typeof versionId === "number" &&
    Number.isInteger(versionId) &&
    versionId >= 1;
  return useQuery<ChapterVersionContent>({
    queryKey: CHAPTER_VERSION_QUERY_KEY(
      workspaceId ?? "",
      chapterId ?? "",
      versionId ?? 0,
    ),
    queryFn: () =>
      apiGet<ChapterVersionContent>(
        `/chapters/${encodeURIComponent(chapterId as string)}/versions/${
          versionId as number
        }`,
        { workspaceId: workspaceId as string },
      ),
    enabled,
  });
}

/**
 * P1-F-14:`:write` / `:rewrite` / `:revise` mutation 共享的 invalidate 套路。
 *
 * 三条 mutation 跑完都可能影响:
 * - `["chapters", workspaceId]` 列表(`status` / `latest_version_id` /
 *   `version_count` 全可能改)
 * - `["chapter-card", ws, ch]` 单卡(同上,ChapterCardOut 携带 status 段)
 * - `["chapter-versions", ws, ch]` 版本号列表(:write / :rewrite / :revise
 *   都新增 vN.md)
 * - `["chapter-version", ws, ch, ...]` 单版本正文(:write / :rewrite 写多版,
 *   :revise 写一版;按 partial match invalidate 全部正文 query 让中栏自动
 *   refetch 最新版)
 *
 * 不 invalidate `["task", taskId]`:每次 mutation 写新 task_id 入 store,旧
 * key 自然不复用(`useTaskInfoQuery` 重新订阅新 key 即触发新请求)。
 *
 * P1-F-15 起 ChapterEditorPage 收到 WS `state_transition` 事件时也复用本函数
 * 的子集(不 invalidate 列表,因编辑器页不渲染列表),通过 `options.skipList`
 * 控制。
 */
export function invalidateChapterArtifacts(
  queryClient: ReturnType<typeof useQueryClient>,
  workspaceId: string,
  chapterId: string,
  options: { skipList?: boolean } = {},
): void {
  if (!options.skipList) {
    void queryClient.invalidateQueries({
      queryKey: ["chapters", workspaceId],
    });
  }
  void queryClient.invalidateQueries({
    queryKey: CHAPTER_CARD_QUERY_KEY(workspaceId, chapterId),
  });
  void queryClient.invalidateQueries({
    queryKey: CHAPTER_VERSIONS_QUERY_KEY(workspaceId, chapterId),
  });
  void queryClient.invalidateQueries({
    queryKey: ["chapter-version", workspaceId, chapterId],
  });
  // v1.1 w3-s7 H7:新版本写入后风格指纹时序也变,刷新漂移折线图。
  void queryClient.invalidateQueries({
    queryKey: CHAPTER_STYLE_DRIFT_QUERY_KEY(workspaceId, chapterId),
  });
  // v2 W3.5 UI 缺口3:状态 + escalation_details 也随每次 transition / mutation 变,
  // 刷新 chapter-status 让 EscalationDetailsBanner 实时反映(重审通过→横幅消失 /
  // 不通过→换新详情;原 ignore-feasibility 路径也曾有此 staleness)。
  void queryClient.invalidateQueries({
    queryKey: CHAPTER_STATUS_QUERY_KEY(workspaceId, chapterId),
  });
}

interface ChapterMutationContext {
  workspaceId: string;
  chapterId: string;
}

interface UpdateChapterCardArgs extends ChapterMutationContext {
  /** 整张 ChapterCard(P1-B-19 PUT /card 完整覆盖语义,不允许 PATCH 单字段)。 */
  card: ChapterCardOut;
}

/**
 * 更新章节卡(对应 `PUT /api/v1/chapters/{id}/card`,P3-F-04 引入)。
 *
 * 后端 P1-B-19 已支持完整覆盖写入,前端 P3-F-04 起首次接通。Body 必须含
 * `chapter_id` 字段且与 URL 参数一致(否则后端 422 `CHAPTER_ID_MISMATCH`)。
 *
 * onSuccess 同 :write / :rewrite / :revise:invalidate `chapter-card` /
 * `chapter-versions` / 列表 query,让中栏正文 + 左栏卡 + 章节列表统一刷新。
 *
 * 错误码:`CHAPTER_ID_MISMATCH` (422) / `CHAPTER_INVALID_STATE` (409,后端 P3+
 * 可能加禁止改卡的状态守卫;P1-P2 不抛此码)/ Pydantic 校验失败 → 422。
 */
export function useUpdateChapterCardMutation() {
  const queryClient = useQueryClient();
  return useMutation<ChapterCardOut, Error, UpdateChapterCardArgs>({
    mutationFn: ({ workspaceId, chapterId, card }) =>
      apiPut<ChapterCardOut, ChapterCardOut>(
        `/chapters/${encodeURIComponent(chapterId)}/card`,
        card,
        { workspaceId },
      ),
    onSuccess: (_data, { workspaceId, chapterId }) => {
      // 改卡只影响章节卡 + 列表(card.title / chapter_number / state 段都可能进
      // 列表行)。版本 / 版本正文不受影响,但用 invalidateChapterArtifacts 全
      // invalidate 也无副作用,沿用 :write 套路保持一致。
      invalidateChapterArtifacts(queryClient, workspaceId, chapterId);
    },
  });
}

interface WriteChapterArgs extends ChapterMutationContext {
  /** P3-F-03 加:本次写作 profile 覆盖;`null`/`undefined` = 走 RoutingConfig 默认。 */
  profileOverride?: string | null;
}

/**
 * 触发完整章节闭环(对应 `POST /api/v1/chapters/{id}:write`)。
 *
 * P1-P2 阶段 `:write` body 为空;**P3-B-11 起 body 可选携带 `profile_override`
 * 字段**(WriterProfileSelector 单次写作覆盖)。`profileOverride=null`/
 * `undefined` 时仍发送 `{}` 空对象等价 body(后端 `ChapterWriteIn()` 默认
 * 实例,字段 default=None,语义等同 P1-P2 空 body)。
 *
 * 调用方(`ChapterActionsToolbar`)在 onSuccess 内:
 * 1. `setChapterTaskId(chapterId, task_id)` 写 zustand(供 ReviewHistoryPanel
 *    自动激活 + 供 P1-F-15 订阅 WS 进度)
 * 2. `showSuccessToast` 反馈
 * 失败由调用方 `showApiErrorToast` 翻译;典型错误 `CHAPTER_INVALID_STATE`(状态
 * 机不允许)/ `CHAPTER_CARD_NOT_FOUND` / `CHAPTER_INVALID_PROFILE`(P3-B-11
 * 加,profile_override 引用未声明 profile)/ 5xx LLM 失败。
 */
export function useWriteChapterMutation() {
  const queryClient = useQueryClient();
  return useMutation<TaskInfoChapterWrite, Error, WriteChapterArgs>({
    mutationFn: ({ workspaceId, chapterId, profileOverride }) =>
      apiPost<TaskInfoChapterWrite, ChapterWriteIn>(
        `/chapters/${encodeURIComponent(chapterId)}:write`,
        { profile_override: profileOverride ?? null },
        { workspaceId },
      ),
    onSuccess: (_data, { workspaceId, chapterId }) => {
      invalidateChapterArtifacts(queryClient, workspaceId, chapterId);
    },
  });
}

/**
 * v2 Fix 2:人工确认 Fix D 误判后,忽略写前可行性预检并重写
 * (对应 `POST /api/v1/chapters/{id}:ignore-feasibility`)。
 *
 * 仅 `human_intervention`(写前预检升人工的孤儿章)可调;其余 status 返 409
 * `CHAPTER_INVALID_STATE`。章节带完整 must_achieve 写作,写后 must_achieve
 * validator 仍兜底验真(非无脑放过)。立即返 202 + TaskInfo,前端订阅
 * `task.{id}.progress` 接 done/failed。
 */
export function useIgnoreFeasibilityMutation() {
  const queryClient = useQueryClient();
  return useMutation<TaskInfoChapterWrite, Error, WriteChapterArgs>({
    mutationFn: ({ workspaceId, chapterId, profileOverride }) =>
      apiPost<TaskInfoChapterWrite, ChapterWriteIn>(
        `/chapters/${encodeURIComponent(chapterId)}:ignore-feasibility`,
        { profile_override: profileOverride ?? null },
        { workspaceId },
      ),
    onSuccess: (_data, { workspaceId, chapterId }) => {
      invalidateChapterArtifacts(queryClient, workspaceId, chapterId);
    },
  });
}

/**
 * v2 W3.5 UI 缺口3:人工改稿后重审(对应 `POST /api/v1/chapters/{id}:re-review`)。
 *
 * 仅 `human_intervention`(孤儿章)可调;其余 status → 422
 * `CHAPTER_RE_REVIEW_INVALID_STATE`。对最新版本(= 人工改稿版本)单次重跑写后
 * 审校:**通过**→ APPROVED(自动 mark final + 摘要 + 记忆);**不通过**→ 回
 * human_intervention + `escalation_reason=re_review_failed` + 详情(前端刷新
 * status 拿)。立即返 202 + TaskInfo,前端订阅 `task.{id}.progress` 接 done/failed。
 */
export function useReReviewChapterMutation() {
  const queryClient = useQueryClient();
  return useMutation<TaskInfoChapterWrite, Error, WriteChapterArgs>({
    mutationFn: ({ workspaceId, chapterId, profileOverride }) =>
      apiPost<TaskInfoChapterWrite, ChapterReReviewIn>(
        `/chapters/${encodeURIComponent(chapterId)}:re-review`,
        { profile_override: profileOverride ?? null },
        { workspaceId },
      ),
    onSuccess: (_data, { workspaceId, chapterId }) => {
      invalidateChapterArtifacts(queryClient, workspaceId, chapterId);
    },
  });
}

interface RewriteChapterArgs extends ChapterMutationContext {
  priorRewriteCount: number;
  /** P3-F-03 加:本次重写 profile 覆盖;`null`/`undefined` = 走默认 routing。 */
  profileOverride?: string | null;
}

/**
 * 触发强制重写(对应 `POST /api/v1/chapters/{id}:rewrite`)。
 *
 * `prior_rewrite_count` 由调用方提供(P1 ChapterService 没 chapters 表,后端
 * 自己查不到"已用了几次重写")。前端从 P1-F-13 review_summary 统计 `verdict ==
 * "rewrite"` 条目数推算,P2 后端落 chapters 表后该字段 deprecate。
 *
 * 错误码同 :write,另加后端 `MAX_REWRITES_EXCEEDED` 类(后端 P1 简版未必抛
 * 该细分错,失败走 `CHAPTER_INVALID_STATE` 兜底,`showApiErrorToast` 自然
 * 翻译)。
 */
export function useRewriteChapterMutation() {
  const queryClient = useQueryClient();
  return useMutation<TaskInfoChapterWrite, Error, RewriteChapterArgs>({
    mutationFn: ({
      workspaceId,
      chapterId,
      priorRewriteCount,
      profileOverride,
    }) =>
      apiPost<TaskInfoChapterWrite, ChapterRewriteIn>(
        `/chapters/${encodeURIComponent(chapterId)}:rewrite`,
        {
          prior_rewrite_count: priorRewriteCount,
          profile_override: profileOverride ?? null,
        },
        { workspaceId },
      ),
    onSuccess: (_data, { workspaceId, chapterId }) => {
      invalidateChapterArtifacts(queryClient, workspaceId, chapterId);
    },
  });
}

interface ReviseChapterArgs extends ChapterMutationContext {
  issues: ReviewIssueIn[];
  sourceVersionId?: number | null;
}

/**
 * 触发局部修订(对应 `POST /api/v1/chapters/{id}:revise`)。
 *
 * `issues` 透传自调用方在 ReviewHistoryPanel 选中的 review 条目的
 * `revise_targets`。`ReviewIssueOut → ReviewIssueIn` 字段一致(后端 schema
 * `ReviewIssueOut(ReviewIssueIn)` 是空子类),前端可直接传输。
 *
 * `sourceVersionId` 透传自被审稿的版本号(`entry.version_reviewed`,P1-F-14
 * 待审 3 拍 A)。后端字面"留空时取最新版本号",但前端按"基于该次审稿对那一
 * 版做修订"语义显式传。
 *
 * `:revise` 不走 Orchestrator,只产出新版本元数据(`ChapterVersionMeta`
 * 不含正文),`TaskInfoChapterRevise.result` 不含 review_summary。
 */
export function useReviseChapterMutation() {
  const queryClient = useQueryClient();
  return useMutation<TaskInfoChapterRevise, Error, ReviseChapterArgs>({
    mutationFn: ({ workspaceId, chapterId, issues, sourceVersionId }) =>
      apiPost<TaskInfoChapterRevise, ChapterReviseIn>(
        `/chapters/${encodeURIComponent(chapterId)}:revise`,
        {
          issues,
          source_version_id: sourceVersionId ?? null,
        },
        { workspaceId },
      ),
    onSuccess: (_data, { workspaceId, chapterId }) => {
      invalidateChapterArtifacts(queryClient, workspaceId, chapterId);
    },
  });
}

/**
 * P6-F-06:回退章节(对应 `POST /api/v1/chapters/{id}:rewind`,P6-B-01)。
 *
 * 后端语义(对齐 ChapterRewindOut):
 * - 目标章节 `chapters.status` 翻 `drafting`
 * - `chapter_id > target_id` 的后续章节翻 `invalidated`
 * - Chroma 向量库 `metadata.chapter_id == target_id` 软删(revoked=true)
 * - SQLite extracted_* 三表对应章节行 `revoked_at` 翻 now
 *
 * 源 status 必须在 `reviewing/rewriting/revising/approved/published/
 * human_intervention` 内,否则 422 `CHAPTER_REWIND_INVALID_STATE`。
 *
 * onSuccess 同 :write 套路 invalidate `chapter-card` / `chapter-versions` /
 * 列表;额外因为后续章节状态翻 invalidated,列表 query 重拉时多档刷新。
 */
export function useRewindChapterMutation() {
  const queryClient = useQueryClient();
  return useMutation<ChapterRewindOut, Error, ChapterMutationContext>({
    mutationFn: ({ workspaceId, chapterId }) =>
      apiPost<ChapterRewindOut, ChapterRewindIn>(
        `/chapters/${encodeURIComponent(chapterId)}:rewind`,
        {},
        { workspaceId },
      ),
    // v1.0.x B2-04:全局 staleTime=30s(main.tsx:18) → 仅 invalidate 单卡
    // ["chapter-card", ws, target_id] 不够。rewind 翻 chapter_id > target_id
    // 的后续章节 status → invalidated,这批下游章节单卡 cache 在 30s 内仍
    // fresh,用户跳板后点进下游章节看到陈旧 approved → 实战感知"按钮点击后
    // 状态卡顿不更新"。修法:遍历 `invalidated_chapter_ids` 逐个 invalidate
    // 下游单卡 / versions / version;列表 invalidate 由 target chapterId 走
    // `invalidateChapterArtifacts` 触发(skipList=true 避 N 次列表 refetch)。
    onSuccess: (data, { workspaceId, chapterId }) => {
      invalidateChapterArtifacts(queryClient, workspaceId, chapterId);
      for (const invalidatedId of data.invalidated_chapter_ids) {
        invalidateChapterArtifacts(queryClient, workspaceId, invalidatedId, {
          skipList: true,
        });
      }
    },
  });
}

interface ForcePassChapterArgs extends ChapterMutationContext {
  /** force-pass 触发理由(必填,后端 min_length=1)。 */
  reason: string;
}

/**
 * P6-F-06:强制审稿通过(对应 `POST /api/v1/chapters/{id}:force-pass`,P6-B-01)。
 *
 * `reason` 强制非空(后端 Pydantic `min_length=1`,自决项 1)— 前端 textarea
 * 收集 + 调用前 trim 校验。源 status 在 `drafting/reviewing/rewriting/
 * revising/human_intervention` 内才能 force-pass;`approved`/`published`
 * 返 409 `CHAPTER_INVALID_STATE`;`invalidated` 返 409
 * `CHAPTER_INVALIDATED_NEEDS_UNBLOCK`(先 unblock)。
 */
/**
 * v1.1 acceptance 修复2:读章节状态详情(对应 `GET /api/v1/chapters/{id}/status`)。
 *
 * 孤儿章人工裁决场景:`escalation_details` 字段含 Fix D 写前预检 findings
 * (哪条 must 不可达 / judge 证据 / 处置建议),ChapterEditorPage 在
 * `human_intervention` 状态展示。
 */
export function useChapterStatusQuery(
  workspaceId: string | null | undefined,
  chapterId: string | null | undefined,
) {
  const enabled =
    typeof workspaceId === "string" &&
    workspaceId.length > 0 &&
    typeof chapterId === "string" &&
    chapterId.length > 0;
  return useQuery<ChapterStatusOut>({
    queryKey: CHAPTER_STATUS_QUERY_KEY(workspaceId ?? "", chapterId ?? ""),
    queryFn: () =>
      apiGet<ChapterStatusOut>(
        `/chapters/${encodeURIComponent(chapterId as string)}/status`,
        { workspaceId: workspaceId as string },
      ),
    enabled,
  });
}

interface UpdateChapterContentArgs {
  workspaceId: string;
  chapterId: string;
  content: string;
}

/**
 * v1.1 acceptance 修复2:人工编辑章节正文(对应 `PUT /api/v1/chapters/{id}/content`)。
 *
 * 孤儿章人工解决流程第一步:落新版本(`manual_edit`)+ 切 final 指针;批准走
 * 既有 `:force-pass`(批准后后端自动补完整记忆抽取)。仅 `human_intervention`
 * 状态可调,其余 409 `CHAPTER_INVALID_STATE`。
 */
export function useUpdateChapterContentMutation() {
  const queryClient = useQueryClient();
  return useMutation<ChapterContentUpdateOut, Error, UpdateChapterContentArgs>({
    mutationFn: ({ workspaceId, chapterId, content }) =>
      apiPut<ChapterContentUpdateOut, ChapterContentUpdateIn>(
        `/chapters/${encodeURIComponent(chapterId)}/content`,
        { content },
        { workspaceId },
      ),
    onSuccess: (_data, { workspaceId, chapterId }) => {
      invalidateChapterArtifacts(queryClient, workspaceId, chapterId);
      void queryClient.invalidateQueries({
        queryKey: CHAPTER_STATUS_QUERY_KEY(workspaceId, chapterId),
      });
    },
  });
}

export function useForcePassChapterMutation() {
  const queryClient = useQueryClient();
  return useMutation<ChapterForcePassOut, Error, ForcePassChapterArgs>({
    mutationFn: ({ workspaceId, chapterId, reason }) =>
      apiPost<ChapterForcePassOut, ChapterForcePassIn>(
        `/chapters/${encodeURIComponent(chapterId)}:force-pass`,
        { reason },
        { workspaceId },
      ),
    onSuccess: (_data, { workspaceId, chapterId }) => {
      invalidateChapterArtifacts(queryClient, workspaceId, chapterId);
    },
  });
}

/**
 * P6-F-06:unblock 单章(对应 `POST /api/v1/chapters/{id}:unblock`,P6-B-01)。
 *
 * 把 `chapters.status` 从 `invalidated` 翻回 `drafting`;非 `invalidated`
 * 源返 422 `CHAPTER_UNBLOCK_INVALID_STATE`。批量走 `unblock-all` 端点
 * (`useUnblockAllChaptersMutation`)。
 */
export function useUnblockChapterMutation() {
  const queryClient = useQueryClient();
  return useMutation<ChapterUnblockOut, Error, ChapterMutationContext>({
    mutationFn: ({ workspaceId, chapterId }) =>
      apiPost<ChapterUnblockOut, ChapterUnblockIn>(
        `/chapters/${encodeURIComponent(chapterId)}:unblock`,
        {},
        { workspaceId },
      ),
    onSuccess: (_data, { workspaceId, chapterId }) => {
      invalidateChapterArtifacts(queryClient, workspaceId, chapterId);
    },
  });
}

interface UnblockAllChaptersArgs {
  workspaceId: string;
}

/**
 * P6-F-06:批量 unblock workspace 内所有 invalidated 章节(对应
 * `POST /api/v1/workspaces/{workspace_id}:unblock-all`,P6-B-01)。
 *
 * **注意路径区别**:本端点是 workspace 子资源(workspace_id 在 path),不需要
 * X-Workspace-Id header(`apiPost` 第三参 `workspaceId` 留空)。空状态(无
 * invalidated 章节)→ 200 + count=0 幂等。
 */
export function useUnblockAllChaptersMutation() {
  const queryClient = useQueryClient();
  return useMutation<WorkspaceUnblockAllOut, Error, UnblockAllChaptersArgs>({
    mutationFn: ({ workspaceId }) =>
      apiPost<WorkspaceUnblockAllOut, WorkspaceUnblockAllIn>(
        `/workspaces/${encodeURIComponent(workspaceId)}:unblock-all`,
        {},
      ),
    // v1.0.x B2-04:全局 staleTime=30s(main.tsx:18) → 只 invalidate 列表不够。
    // unblock-all 翻多章节 status invalidated → drafting,这批章节单卡 cache
    // 在 30s 内仍 fresh,用户点进任一章节看到陈旧 invalidated → 实战感知
    // "按钮点击后状态卡顿不更新"。修法:遍历 `unblocked_chapter_ids` 逐个
    // invalidate 单卡(skipList=true 避 N 次列表 refetch,列表已在前面统一
    // invalidate)。
    onSuccess: (data, { workspaceId }) => {
      void queryClient.invalidateQueries({
        queryKey: CHAPTERS_QUERY_KEY(workspaceId),
      });
      for (const unblockedId of data.unblocked_chapter_ids) {
        invalidateChapterArtifacts(queryClient, workspaceId, unblockedId, {
          skipList: true,
        });
      }
    },
  });
}

interface ResetStaleChaptersArgs {
  workspaceId: string;
}

/**
 * v2 w1-s4:批量重置 workspace 内滞留非终态的中断章(对应
 * `POST /api/v1/workspaces/{workspace_id}:reset-stale`,w1-s2 章自愈)。
 *
 * 把进程中断时停在 reviewing / rewriting / revising 的章翻回 drafting;
 * human_intervention / invalidated 语义"等人"不在自愈范围。有 running
 * pipeline / 活跃后台任务时后端跳过扫描(200 + `skipped_reason` 非空)。
 * 空扫描 → 200 + count=0 幂等(对齐 unblock-all 模式,workspace 子资源
 * 路径,不需要 X-Workspace-Id header)。
 */
export function useResetStaleChaptersMutation() {
  const queryClient = useQueryClient();
  return useMutation<WorkspaceResetStaleOut, Error, ResetStaleChaptersArgs>({
    mutationFn: ({ workspaceId }) =>
      apiPost<WorkspaceResetStaleOut, WorkspaceResetStaleIn>(
        `/workspaces/${encodeURIComponent(workspaceId)}:reset-stale`,
        {},
      ),
    // 对齐 unblock-all 的 B2-04 修法:列表 invalidate + 被重置章逐个
    // invalidate 单卡(30s staleTime 下避免点进章节看到陈旧 status)。
    onSuccess: (data, { workspaceId }) => {
      void queryClient.invalidateQueries({
        queryKey: CHAPTERS_QUERY_KEY(workspaceId),
      });
      for (const resetId of data.reset_chapter_ids) {
        invalidateChapterArtifacts(queryClient, workspaceId, resetId, {
          skipList: true,
        });
      }
    },
  });
}
