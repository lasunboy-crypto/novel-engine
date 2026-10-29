/**
 * ChapterEditorPage(P1-F-12 实质化 / P1-F-13 版本切换 / P1-F-14 工具栏 /
 * P1-F-15 实时进度):章节编辑器三栏布局 + 顶部工具栏 + 状态徽标 + WS 订阅。
 *
 * 路由:`/chapters/:id`。本任务把 P1-F-04 占位实质化为三栏 + P1-F-14 工具栏:
 *
 * - **顶部工具栏**:`<ChapterActionsToolbar>`(P1-F-14 加,3 按钮触发
 *   :write / :rewrite / :revise)
 * - **左栏**:章节卡(只读 JSON 预览,`<ChapterCardPanel>`)
 * - **中栏**:章节正文(选最新版 + react-markdown 渲染,`<ChapterContentPanel>`)
 * - **右栏**:审稿历史(P1-F-13 实质化 + P1-F-14 受控选中,`<ReviewHistoryPanel>`)
 *
 * 三栏宽比 1 : 2 : 1(中栏正文是主角,P1-F-12 决策 1)。小屏(< lg)堆叠成单列。
 *
 * ## P1-F-15 实时进度(WS 订阅 state_transition)
 *
 * 进入页时通过 `useChapterEvents(chapterId, { onStateTransition })` 订阅
 * `chapter.{chapterId}.events` 频道。收到 `state_transition` 事件:
 *
 * 1. **更新 `lastChapterState`**:`<ChapterStateBadge>` 在 Header 实时刷新(P2-F-01 合并 LiveChapterStateBadge 后统一组件)
 * 2. **弹 toast**(default variant,非 destructive):标题"章节状态变化",
 *    描述 `from → to`(原样 ChapterState.value snake_case 英文,自决 1)
 * 3. **invalidate 子集 query**:`chapter-card` / `chapter-versions` /
 *    `chapter-version`(中栏正文 + 版本下拉自动刷到新最新版,自决 3);**不**
 *    invalidate `chapters` 列表(本页不渲染列表)
 *
 * 初始 `lastChapterState=null`,徽标显示"等待事件"灰兜底(P1-F-15 待审 1
 * 拍 A:不拉 `GET /chapters/{id}/status` baseline,因 P1 时 status API 返
 * 3 档与 WS 7 档枚举空间不同。**P2-B-17 已让 status API + list 端点返 7 档**,
 * 但本页是否拉 baseline 留给后续任务评估,本任务范围只做组件合并)。
 *
 * ## P1-F-14 选中状态归属(待审 1 拍 A)
 *
 * `selectedReviewIndex` 由本组件 `useState` 持有(待审 1 拍 A——React 共享
 * 状态标准模式,不引 zustand)。`ReviewHistoryPanel` 受控接收 + setter 上抛
 * toggle;`ChapterActionsToolbar` 接收派生的 `selectedReview`(从父级
 * `reviewSummary` 数组按 index 取,父级 `reviewSummary` 由 ReviewHistoryPanel
 * `onSummaryChange` hoist)。
 *
 * ## active workspace 推算(沿用 P1-F-11 套路,P1-F-12 决策 7)
 *
 * 章节路由要 `X-Workspace-Id` header,但路由参只给了 `:id`(chapter_id),
 * workspace_id 需推算:
 *
 * 1. zustand `activeWorkspaceId` 命中(WorkspaceDetailPage / ChaptersBoardPage
 *    上一跳已写入)→ 取该工作区
 * 2. store null(用户直链 `/chapters/<id>` 绕过列表)→ fallback
 *    `last_active_at` max(`useActiveWorkspace` hook 内部已合两路)
 *
 * 推算失败(无活动工作区)→ 整页提示 + Link 回 `/workspaces`,不渲染三栏。
 *
 * ## 5 态分支(整页层 + 三栏内层)
 *
 * 整页层(workspaces 数据):
 * - workspaces loading / error / empty / activeWorkspace null → 整页提示
 *
 * 三栏内层(各栏独立处理,任一栏失败不阻塞其他栏,P1-F-12 决策 5):
 * - 章节卡:loading / 404 / 422 / 其他 error / 渲染
 * - 中栏:versions loading / error / empty("尚未写作")/ version content
 *   loading / error / 渲染 markdown
 * - 右栏:占位 hint(P1-F-12 范围内不消费 API)
 */

import { useCallback, useEffect, useState } from "react";
import { useQueryClient } from "@tanstack/react-query";
import { Link, useNavigate, useParams } from "react-router-dom";

import {
  invalidateChapterArtifacts,
  useChapterCardQuery,
  useChapterStatusQuery,
  useForcePassChapterMutation,
  useIgnoreFeasibilityMutation,
  useReReviewChapterMutation,
  useRewindChapterMutation,
  useUpdateChapterContentMutation,
} from "@/api/chapters";
import { isApiError } from "@/api/errors";
import type { ReviewSummaryEntry } from "@/api/tasks";
import {
  useWorkspacesQuery,
  type WorkspaceOut,
} from "@/api/workspaces";
import { Button } from "@/components/ui/button";
import { ChapterActionsToolbar } from "@/components/ChapterActionsToolbar";
import { ChapterCardPanel } from "@/components/ChapterCardPanel";
import { ChapterContentPanel } from "@/components/ChapterContentPanel";
import { ChapterStateBadge } from "@/components/ChapterStateBadge";
import { EscalationDetailsBanner } from "@/components/EscalationDetailsBanner";
import { ForcePassConfirmDialog } from "@/components/ForcePassConfirmDialog";
import { RewindConfirmDialog } from "@/components/RewindConfirmDialog";
import { ReviewHistoryPanel } from "@/components/ReviewHistoryPanel";
import { StyleDriftPanel } from "@/components/StyleDriftPanel";
import { VoiceCheckPanel } from "@/components/VoiceCheckPanel";
import { showApiErrorToast, showSuccessToast } from "@/components/Toast";
import { useChapterEvents } from "@/hooks/useChapterEvents";
import { useActiveWorkspace } from "@/store/activeWorkspace";
import type { StateTransitionData } from "@/ws/types";

export default function ChapterEditorPage() {
  const { id: chapterId } = useParams<{ id: string }>();
  const workspacesQuery = useWorkspacesQuery();
  const activeWorkspace = useActiveWorkspace(workspacesQuery.data);
  const queryClient = useQueryClient();
  const navigate = useNavigate();

  // P1-F-14:选中的审稿条目 index(供 ChapterActionsToolbar 修订按钮派生
  // selectedReview)。null = 未选中(修订按钮禁用)。
  const [selectedReviewIndex, setSelectedReviewIndex] = useState<number | null>(
    null,
  );
  // P1-F-14:从 ReviewHistoryPanel hoist 上来的 review_summary 数组。null =
  // 未触发任务 / 任务无摘要;非 null = 当前 task 的 review_summary 列表。
  // ChapterActionsToolbar 用它推算 priorRewriteCount,父级用它派生
  // selectedReview。
  const [reviewSummary, setReviewSummary] = useState<
    ReviewSummaryEntry[] | null
  >(null);

  // P1-F-15:最近一次 WS state_transition 事件的 to(P6-B-01 起 8 档 ChapterState.value
  // snake_case)。null = 进入页后尚未观测到任何 transition;由 ChapterStateBadge
  // 渲染"等待事件"灰兜底(待审 1 拍 A,不拉 GET /chapters/{id}/status baseline;
  // P2-F-01 合并 LiveChapterStateBadge 后统一徽标组件)。
  const [lastChapterState, setLastChapterState] = useState<string | null>(null);

  // P6-F-06:rewind / force-pass 二次确认对话框 open 受控 state。
  const [rewindDialogOpen, setRewindDialogOpen] = useState(false);
  const [forcePassDialogOpen, setForcePassDialogOpen] = useState(false);
  const rewindMutation = useRewindChapterMutation();
  const forcePassMutation = useForcePassChapterMutation();

  // P3-F-03:本次写作 profile 覆盖。null = "auto(走 RoutingConfig 规则)";
  // 字符串 = 强制指定 profile_id。:write 与 :rewrite 共享同一选择,触发后
  // 后端 P3-B-11 wrap _ProfileOverrideRouter 让所有 agent 走该 profile。
  // **不持久化**——切章 / 刷新页面后回到 null(对齐 P3-plan §F-2 字面)。
  const [displayedWordCount, setDisplayedWordCount] = useState<number | null>(null);
  const [profileOverride, setProfileOverride] = useState<string | null>(null);

  // 章节切换 / 任务切换 → reset 选中(避免越界 / 错配)。
  useEffect(() => {
    setSelectedReviewIndex(null);
  }, [chapterId, reviewSummary]);

  // P1-F-15:章节切换时 reset 实时状态(切到不同章节时,前一章节的状态徽标
  // 不应保留)。空依赖只有 chapterId,不依赖 lastChapterState 避免自身 setter
  // 重触发。
  useEffect(() => {
    setLastChapterState(null);
  }, [chapterId]);

  // P3-F-03:章节切换时 reset profile 覆盖。换章节后保留前一章节的 profile
  // 选择会让用户困惑(不同章节可能想用不同 profile),回到 auto 默认。
  useEffect(() => {
    setProfileOverride(null);
  }, [chapterId]);

  // P1-F-15:WS state_transition 处理器。useCallback 稳定引用,避免
  // useChapterEvents 内 useEffect 因 onStateTransition 变化反复 resub。依赖
  // chapterId / activeWorkspace.workspace_id / queryClient(均稳定)。
  const handleStateTransition = useCallback(
    (data: StateTransitionData) => {
      setLastChapterState(data.to);
      // 状态变化即触发 toast(default variant,非 destructive,自决 5)。
      // 多次 transition 顶替前一条(shadcn TOAST_LIMIT=1 自然行为,自决 2)。
      // 文案 from→to 用原样 ChapterState.value snake_case,与后端日志直读对应
      // (自决 1)。
      showSuccessToast(
        "章节状态变化",
        `${data.from} → ${data.to}`,
      );
      // invalidate 子集:本页用到的 chapter-card / chapter-versions /
      // chapter-version,不 invalidate `chapters` 列表(本页不渲染,自决 3)。
      // 与 P1-F-14 mutation 的 invalidateChapterArtifacts 共享实现,通过
      // skipList=true 取子集。
      if (activeWorkspace && chapterId) {
        invalidateChapterArtifacts(
          queryClient,
          activeWorkspace.workspace_id,
          chapterId,
          { skipList: true },
        );
      }
    },
    [activeWorkspace, chapterId, queryClient],
  );

  // P1-F-15:订阅 chapter.{id}.events;chapterId 变化时自动取消旧订阅 + 订阅新
  // channel(useChapterEvents 内部 useEffect 依赖 chapterId)。chapterId
  // undefined 时 hook 内部短路不订阅。
  useChapterEvents(chapterId, { onStateTransition: handleStateTransition });

  // P6-F-06:rewind 触发 → 二次确认 → mutation → 成功 toast(显示
  // invalidated_chapter_ids 数量)+ 跳 ChaptersBoardPage(任务模板字面 ③)。
  const handleRewindConfirm = useCallback(async () => {
    if (!activeWorkspace || !chapterId) return;
    try {
      const result = await rewindMutation.mutateAsync({
        workspaceId: activeWorkspace.workspace_id,
        chapterId,
      });
      showSuccessToast(
        "章节已回退",
        `${chapterId}: ${result.from_status} → ${result.to_status};` +
          `标废后续 ${result.invalidated_chapter_ids.length} 章 / ` +
          `软删 ${result.vector_revoked_count} vectors / ` +
          `extracted ${result.extracted_revoked_count} 行`,
      );
      navigate("/chapters");
    } catch (err) {
      showApiErrorToast(err);
    }
  }, [activeWorkspace, chapterId, navigate, rewindMutation]);

  // P6-F-06:force-pass 触发 → 二次确认(收集 reason)→ mutation → 成功 toast。
  // 不跳板(force-pass 把章节翻 approved,用户仍在编辑页查看就好)。
  const handleForcePassConfirm = useCallback(
    async (reason: string) => {
      if (!activeWorkspace || !chapterId) return;
      try {
        const result = await forcePassMutation.mutateAsync({
          workspaceId: activeWorkspace.workspace_id,
          chapterId,
          reason,
        });
        showSuccessToast(
          "已强制通过审稿",
          `${chapterId}: ${result.from_status} → ${result.to_status}`,
        );
      } catch (err) {
        showApiErrorToast(err);
      }
    },
    [activeWorkspace, chapterId, forcePassMutation],
  );

  if (!chapterId) {
    return (
      <div className="rounded border border-destructive bg-destructive/10 p-6 text-sm text-destructive">
        URL 缺少 chapter_id 参数。
        <Link to="/chapters" className="ml-2 underline">
          回到章节列表
        </Link>
      </div>
    );
  }

  // 派生:从父级 reviewSummary 按 selectedReviewIndex 取 selectedReview。
  // index 越界(reviewSummary 长度变化但 index 未及时 reset)→ null。
  const selectedReview =
    reviewSummary &&
    selectedReviewIndex !== null &&
    selectedReviewIndex >= 0 &&
    selectedReviewIndex < reviewSummary.length
      ? reviewSummary[selectedReviewIndex]
      : null;

  return (
    <div className="space-y-4">
      <Header
        chapterId={chapterId}
        displayedWordCount={displayedWordCount}
        workspacesLoading={workspacesQuery.isPending}
        workspacesError={workspacesQuery.error}
        activeWorkspace={activeWorkspace}
        lastChapterState={lastChapterState}
        onRewindClick={() => setRewindDialogOpen(true)}
        onForcePassClick={() => setForcePassDialogOpen(true)}
        isRewindPending={rewindMutation.isPending}
        isForcePassPending={forcePassMutation.isPending}
      />
      <BodyOrPrecondition
        chapterId={chapterId}
        workspacesLoading={workspacesQuery.isPending}
        workspacesError={workspacesQuery.error}
        workspacesEmpty={
          !workspacesQuery.isPending &&
          (workspacesQuery.data?.length ?? 0) === 0
        }
        activeWorkspace={activeWorkspace}
        selectedReviewIndex={selectedReviewIndex}
        onSelectReviewIndex={setSelectedReviewIndex}
        reviewSummary={reviewSummary}
        onSummaryChange={setReviewSummary}
        selectedReview={selectedReview}
        profileOverride={profileOverride}
        onProfileOverrideChange={setProfileOverride}
        onWordCountChange={setDisplayedWordCount}
      />
      <RewindConfirmDialog
        open={rewindDialogOpen}
        onOpenChange={setRewindDialogOpen}
        chapterId={chapterId}
        onConfirm={handleRewindConfirm}
        isConfirmDisabled={rewindMutation.isPending}
      />
      <ForcePassConfirmDialog
        open={forcePassDialogOpen}
        onOpenChange={setForcePassDialogOpen}
        chapterId={chapterId}
        onConfirm={handleForcePassConfirm}
        isConfirmDisabled={forcePassMutation.isPending}
      />
    </div>
  );
}

interface HeaderProps {
  chapterId: string;
  displayedWordCount: number | null;
  workspacesLoading: boolean;
  workspacesError: unknown;
  activeWorkspace: WorkspaceOut | null;
  /** P1-F-15:最近一次 WS state_transition 的 to;null = 尚未观测到事件。 */
  lastChapterState: string | null;
  /** P6-F-06:右上角"回退章节"按钮点击;父级弹 RewindConfirmDialog。 */
  onRewindClick: () => void;
  /** P6-F-06:右上角"强制通过"按钮点击;父级弹 ForcePassConfirmDialog。 */
  onForcePassClick: () => void;
  /** P6-F-06:rewind mutation pending(按钮显"回退中..." + disabled)。 */
  isRewindPending: boolean;
  /** P6-F-06:force-pass mutation pending(按钮显"强制通过中..." + disabled)。 */
  isForcePassPending: boolean;
}

function Header(props: HeaderProps) {
  const cardQuery = useChapterCardQuery(
    props.activeWorkspace?.workspace_id,
    props.chapterId,
  );
  const card = cardQuery.data;

  return (
    <header className="space-y-1">
      <p className="text-xs text-muted-foreground">
        <Link to="/chapters" className="underline">
          ← 章节列表
        </Link>
      </p>
      <div
        className="flex flex-wrap items-baseline gap-x-2 gap-y-1"
        data-testid="chapter-editor-header"
      >
        <h1 className="whitespace-nowrap text-2xl font-semibold">
          {card ? (
            <>
              <span className="text-base font-medium text-muted-foreground tabular-nums">
                第 {card.chapter_number} 章
              </span>{" "}
              <span data-testid="chapter-editor-title">{card.title}</span>
              {props.displayedWordCount !== null && props.displayedWordCount >= card.word_count_max * 2 && (
                <span className="ml-2 text-sm font-normal text-muted-foreground" data-testid="chapter-length-note">
                  〔约 {props.displayedWordCount} 字符，篇幅较长，可考虑人工分章〕
                </span>
              )}
            </>
          ) : (
            <span data-testid="chapter-editor-title-fallback">
              {props.chapterId}
            </span>
          )}
        </h1>
        <span
          className="whitespace-nowrap text-xs text-muted-foreground"
          data-testid="chapter-editor-id"
        >
          ({props.chapterId})
        </span>
        {card ? (
          <span
            className="whitespace-nowrap text-xs text-muted-foreground"
            data-testid="chapter-editor-volume-id"
          >
            卷 {card.volume_id}
          </span>
        ) : null}
        {/* P1-F-15 起 / P2-F-01 合并:实时状态徽标。从 WS state_transition 事件 `to` 字段刷新。 */}
        <ChapterStateBadge status={props.lastChapterState} />
        {/* P6-F-06:右上角"回退章节" + "强制通过"按钮(人工介入)。
            workspace 未推算出 → 禁用(避免无 workspace_id mutation 401)。 */}
        <div className="ml-auto flex items-center gap-2">
          <Button
            variant="outline"
            size="sm"
            onClick={props.onRewindClick}
            disabled={!props.activeWorkspace || props.isRewindPending}
            data-testid="chapter-rewind-btn"
            title="回退当前章节到 drafting 状态,标废后续章节,记忆库 vectors 软删"
          >
            {props.isRewindPending ? "回退中…" : "回退章节"}
          </Button>
          <Button
            variant="outline"
            size="sm"
            onClick={props.onForcePassClick}
            disabled={!props.activeWorkspace || props.isForcePassPending}
            data-testid="chapter-force-pass-btn"
            title="跳过 Reviewer 直接翻 approved,需留下理由"
          >
            {props.isForcePassPending ? "强制通过中…" : "强制通过"}
          </Button>
        </div>
      </div>
      {!props.workspacesLoading &&
        !props.workspacesError &&
        props.activeWorkspace && (
          <p className="text-xs text-muted-foreground">
            当前活动工作区:
            <span className="ml-1 font-medium text-foreground">
              {props.activeWorkspace.display_name}
            </span>
            <span className="ml-1">
              ({props.activeWorkspace.workspace_id})
            </span>
          </p>
        )}
    </header>
  );
}

interface BodyOrPreconditionProps {
  chapterId: string;
  workspacesLoading: boolean;
  workspacesError: unknown;
  workspacesEmpty: boolean;
  activeWorkspace: WorkspaceOut | null;
  selectedReviewIndex: number | null;
  onSelectReviewIndex: (index: number | null) => void;
  reviewSummary: ReviewSummaryEntry[] | null;
  onSummaryChange: (summary: ReviewSummaryEntry[] | null) => void;
  selectedReview: ReviewSummaryEntry | null;
  /** P3-F-03:本次写作 profile 覆盖,父级持 state。 */
  profileOverride: string | null;
  onProfileOverrideChange: (value: string | null) => void;
  onWordCountChange: (count: number | null) => void;
}

function BodyOrPrecondition(props: BodyOrPreconditionProps) {
  if (props.workspacesLoading) {
    return (
      <div className="rounded border border-border p-6 text-sm text-muted-foreground">
        加载工作区列表中…
      </div>
    );
  }
  if (props.workspacesError) {
    const message = isApiError(props.workspacesError)
      ? `${props.workspacesError.code}: ${props.workspacesError.message}`
      : props.workspacesError instanceof Error
        ? props.workspacesError.message
        : "未知错误";
    return (
      <div
        data-testid="chapter-editor-workspaces-error"
        className="rounded border border-destructive bg-destructive/10 p-6 text-sm text-destructive"
      >
        加载工作区失败:{message}
      </div>
    );
  }
  if (props.workspacesEmpty) {
    return (
      <div className="rounded border border-dashed border-border p-6 text-sm text-muted-foreground">
        还没有工作区。请先到
        <Link to="/workspaces" className="mx-1 underline">
          工作区列表
        </Link>
        创建一个。
      </div>
    );
  }
  if (!props.activeWorkspace) {
    return (
      <div className="rounded border border-dashed border-border p-6 text-sm text-muted-foreground">
        当前没有活动工作区。请到
        <Link to="/workspaces" className="mx-1 underline">
          工作区列表
        </Link>
        切换到一个工作区。
      </div>
    );
  }
  return (
    <ChapterEditorBody
      workspaceId={props.activeWorkspace.workspace_id}
      chapterId={props.chapterId}
      selectedReviewIndex={props.selectedReviewIndex}
      onSelectReviewIndex={props.onSelectReviewIndex}
      reviewSummary={props.reviewSummary}
      onSummaryChange={props.onSummaryChange}
      selectedReview={props.selectedReview}
      profileOverride={props.profileOverride}
      onProfileOverrideChange={props.onProfileOverrideChange}
      onWordCountChange={props.onWordCountChange}
    />
  );
}

interface ChapterEditorBodyProps {
  workspaceId: string;
  chapterId: string;
  selectedReviewIndex: number | null;
  onSelectReviewIndex: (index: number | null) => void;
  reviewSummary: ReviewSummaryEntry[] | null;
  onSummaryChange: (summary: ReviewSummaryEntry[] | null) => void;
  selectedReview: ReviewSummaryEntry | null;
  profileOverride: string | null;
  onProfileOverrideChange: (value: string | null) => void;
  onWordCountChange: (count: number | null) => void;
}

function ChapterEditorBody(props: ChapterEditorBodyProps) {
  // v1.1 acceptance 修复2:孤儿章人工解决流程。status 查询拿 escalation_details
  // (Fix D 写前预检 findings) + 状态;human_intervention 时解锁正文编辑。
  const statusQuery = useChapterStatusQuery(props.workspaceId, props.chapterId);
  const updateContentMutation = useUpdateChapterContentMutation();
  const ignoreFeasibilityMutation = useIgnoreFeasibilityMutation();
  const reReviewMutation = useReReviewChapterMutation();
  const canManualEdit = statusQuery.data?.status === "human_intervention";

  // v2 Fix 2:人工确认 Fix D 写前预检误判 → 忽略预检并带完整 must 重写。
  const handleIgnoreFeasibility = useCallback(async () => {
    try {
      await ignoreFeasibilityMutation.mutateAsync({
        workspaceId: props.workspaceId,
        chapterId: props.chapterId,
      });
      showSuccessToast(
        "已忽略写前预检并重写",
        `${props.chapterId}:带完整必达事件重新写作,写后审校仍会兜底验真。`,
      );
    } catch (err) {
      showApiErrorToast(err);
    }
  }, [ignoreFeasibilityMutation, props.workspaceId, props.chapterId]);

  // v2 W3.5 UI 缺口3:人工改正文后「提交后重审」→ 重跑写后审校。结果(通过完结 /
  // 不通过回人工 + 新详情)由后台任务跑出,WS state_transition 刷 status 后横幅
  // 自动更新;此处只提交 + 提示已开始。
  const handleReReview = useCallback(async () => {
    try {
      await reReviewMutation.mutateAsync({
        workspaceId: props.workspaceId,
        chapterId: props.chapterId,
      });
      showSuccessToast(
        "已提交重审",
        `${props.chapterId}:正在对最新正文重跑写后审校,通过则完结、不通过回人工(下方稍后刷新)。`,
      );
    } catch (err) {
      showApiErrorToast(err);
    }
  }, [reReviewMutation, props.workspaceId, props.chapterId]);

  const handleSaveContent = useCallback(
    async (content: string) => {
      const out = await updateContentMutation.mutateAsync({
        workspaceId: props.workspaceId,
        chapterId: props.chapterId,
        content,
      });
      showSuccessToast(
        "已保存为新版本",
        `${props.chapterId}: v${out.version_id}(${out.word_count} 字符)。` +
          `用右上角「强制通过」批准。`,
      );
      return { version_id: out.version_id };
    },
    [updateContentMutation, props.workspaceId, props.chapterId],
  );

  const handleSaveWithToast = useCallback(
    (content: string) =>
      handleSaveContent(content).catch((err) => {
        showApiErrorToast(err);
        throw err; // 让面板 .then 不执行退出编辑,保留草稿供重试
      }),
    [handleSaveContent],
  );

  return (
    <>
      <ChapterActionsToolbar
        workspaceId={props.workspaceId}
        chapterId={props.chapterId}
        reviewSummary={props.reviewSummary}
        selectedReview={props.selectedReview}
        profileOverride={props.profileOverride}
        onProfileOverrideChange={props.onProfileOverrideChange}
      />
      {/* v1.1 acceptance 修复2:孤儿章人工介入横幅(human_intervention +
          escalation_details 非空才渲染,展示哪条 must 不可达 + 处置建议)。 */}
      <EscalationDetailsBanner
        status={statusQuery.data}
        onReReview={handleReReview}
        isReReviewing={reReviewMutation.isPending}
        onIgnoreFeasibility={handleIgnoreFeasibility}
        isIgnoringFeasibility={ignoreFeasibilityMutation.isPending}
      />
      <div
        data-testid="chapter-editor-grid"
        className="grid grid-cols-1 gap-4 lg:grid-cols-[minmax(0,1fr)_minmax(0,2fr)_minmax(0,1fr)]"
      >
        <ChapterCardPanel
          workspaceId={props.workspaceId}
          chapterId={props.chapterId}
          canEditMustAchieve={canManualEdit}
        />
        <ChapterContentPanel
          workspaceId={props.workspaceId}
          chapterId={props.chapterId}
          canManualEdit={canManualEdit}
          onWordCountChange={props.onWordCountChange}
          onSaveContent={handleSaveWithToast}
          isSavePending={updateContentMutation.isPending}
        />
        <div className="flex min-w-0 flex-col gap-4">
          <VoiceCheckPanel
            workspaceId={props.workspaceId}
            chapterId={props.chapterId}
            reviewSummary={props.reviewSummary}
          />
          <ReviewHistoryPanel
            workspaceId={props.workspaceId}
            chapterId={props.chapterId}
            selectedIndex={props.selectedReviewIndex}
            onSelectIndex={props.onSelectReviewIndex}
            onSummaryChange={props.onSummaryChange}
          />
        </div>
      </div>
      {/* v1.1 w3-s7 H7:风格指纹漂移折线图(整宽,折线需横向空间展示 v1→vN
          趋势)。放三栏 grid 下方,与"章节多版本对比"语义贴近。 */}
      <StyleDriftPanel
        workspaceId={props.workspaceId}
        chapterId={props.chapterId}
      />
    </>
  );
}
