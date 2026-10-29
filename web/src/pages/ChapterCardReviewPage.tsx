/**
 * ChapterCardReviewPage(P5-F-06 + P6-F-01 升级)— 章节卡审核 + 上传 JSON +
 * 编辑后重写 + 完整字段编辑 + 关键字段达成对比 + retry-references。
 *
 * 路由:`/w/:workspaceId/cards/:chapterId/review`(对齐 FRONTEND_SPEC §4.8)。
 *
 * ## 功能点(P5 基线 + P6-F-01 升级)
 *
 * 1. 章节卡完整内容展示(只读 + 进入编辑模式)+ 关键字段高亮 6 项
 *    (`tension_level` / `must_achieve` / `must_unresolved` / `forbidden` /
 *    `continuity_context`) — `continuity_context` 单独
 *    衔接桥块
 * 2. 衔接桥块(上一章末尾 500 字 + unfinished_actions 列表)+ 编辑态嵌套对象
 *    编辑器(`ContinuityContextEditor`,7 字段)
 * 3. 完整字段编辑(P6 升级,委托 `ChapterCardFullEditor`):`arc_phase` /
 *    `is_arc_finale` / `word_count_min/max` / `related_characters` /
 *    `related_plot_threads` / `force_profile`(P6-B-09)
 * 4. 操作按钮:批准 / 退回(+ rejection_notes textarea)/ 编辑 /
 *    强制 profile(WriterProfileSelector 单次覆盖)/ 重试引用合法性
 *    (P6-B-10,reject 状态显)
 * 5. 上传 ChapterCard JSON 按钮(P5 P1 验收 2)
 * 6. 编辑后"重新写作"按钮 → POST /chapters/{id}:write(P5 P1 验收 6)
 * 7. 关键字段达成对比块(P6-B-11,`MustAchieveCheckList` 接 GET /chapters/
 *    {id}/validators/latest;404 `VALIDATOR_REPORT_NOT_FOUND` 时显示
 *    "先 :write 写出正文"提示)
 * 8. ChapterStateBadge + CardStatusBadge 双显;UnsavedChangesGuard 接入
 *
 * ## 编辑保存语义
 *
 * 走 `useUpdateChapterCardMutation`(PUT,既有):整张卡覆盖,后端 P1-B-19
 * 已建 PUT 端点支持完整覆盖。**不**走 P5-B-08 POST(POST 同步落 ORM,
 * 语义是"上传新卡"而非"编辑保存")。
 *
 * ## retry-references 流程
 *
 * `card_status === "rejected"` 时显示"重试引用合法性"按钮(P6-B-10 端点)。
 * 走 LLM 重新生成章节卡 + 严格 ID 约束;新生卡 `card_status="draft"` 覆盖
 * 既有卡。仍失败返 422 `REFERENCE_VALIDATION_FAILED`,详情(unknown ids
 * 列表)由 toast 显示。
 *
 * ## 重新写作触发
 *
 * `card_status === "approved"` 时显示"重新写作"按钮。`writeTaskId` 走
 * `useTaskProgress` WS 订阅占位 toast(详细进度展示留后续迭代)。
 */

import { useMemo, useState } from "react";
import { Link, useNavigate, useParams } from "react-router-dom";

import {
  useChapterCardQuery,
  useChapterListQuery,
  useUpdateChapterCardMutation,
  useWriteChapterMutation,
  type ChapterCardOut,
} from "@/api/chapters";
import {
  useApproveCardMutation,
  useGenerateCardMutation,
  useRejectCardMutation,
  useRetryReferencesMutation,
} from "@/api/chapter_cards";
import { isApiError } from "@/api/errors";
import { useVolumesQuery } from "@/api/volumes";
import { useChapterValidatorReportQuery } from "@/api/validators";
import { CancelTaskButton } from "@/components/CancelTaskButton";
import { CardRetryCascadeConfirmDialog } from "@/components/CardRetryCascadeConfirmDialog";
import { CardStatusBadge } from "@/components/CardStatusBadge";
import { ChapterCardReviewPanel } from "@/components/ChapterCardReviewPanel";
import { ChapterCardUploader } from "@/components/ChapterCardUploader";
import { CoreStateAuditPanel } from "@/components/CoreStateAuditPanel";
import { ChapterStateBadge } from "@/components/ChapterStateBadge";
import GenerationParamsPanel from "@/components/GenerationParamsPanel";
import { MustAchieveCheckList } from "@/components/MustAchieveCheckList";
import { ParallelTaskStatusList } from "@/components/ParallelTaskStatusList";
import { Button } from "@/components/ui/button";
import { showApiErrorToast, showSuccessToast } from "@/components/Toast";
import {
  parseParallelTaskSnapshot,
  type ParallelTaskSnapshot,
} from "@/lib/parallelTaskStatus";
import { WriterProfileSelector } from "@/components/WriterProfileSelector";
import { useAsyncTaskState } from "@/hooks/useAsyncTaskState";
import { useUnsavedChanges } from "@/hooks/useUnsavedChanges";
import {
  resolveChapterLocation,
  type ChapterLocation,
} from "@/lib/chapterLocation";

export default function ChapterCardReviewPage() {
  const { workspaceId, chapterId } = useParams<{
    workspaceId: string;
    chapterId: string;
  }>();
  const navigate = useNavigate();

  if (!workspaceId || !chapterId) {
    return (
      <div className="rounded border border-destructive bg-destructive/10 p-6 text-sm text-destructive">
        URL 参数缺失:需要 workspaceId 与 chapterId。
      </div>
    );
  }

  return (
    <ReviewPageInner workspaceId={workspaceId} chapterId={chapterId}>
      <Link
        to="/chapters"
        className="text-xs text-muted-foreground underline-offset-2 hover:text-foreground hover:underline"
        data-testid="review-back-link"
      >
        ← 返回章节看板
      </Link>
      <span
        className="ml-3 text-xs text-muted-foreground"
        data-testid="review-route-context"
      >
        工作区 <code className="font-mono">{workspaceId}</code> · 章节{" "}
        <code className="font-mono">{chapterId}</code>
      </span>
      <div className="ml-auto flex items-center gap-3">
        <GenerationParamsPanel workspaceId={workspaceId} />
        <button
          type="button"
          onClick={() => navigate(-1)}
          className="text-xs text-muted-foreground underline-offset-2 hover:text-foreground hover:underline"
          data-testid="review-back-history"
        >
          上一页
        </button>
      </div>
    </ReviewPageInner>
  );
}

interface ReviewPageInnerProps {
  workspaceId: string;
  chapterId: string;
  children: React.ReactNode;
}

function ReviewPageInner(props: ReviewPageInnerProps) {
  const { workspaceId, chapterId } = props;
  const cardQuery = useChapterCardQuery(workspaceId, chapterId);

  const [isEditing, setIsEditing] = useState(false);
  const [draft, setDraft] = useState<ChapterCardOut | null>(null);
  const [rejectionNotes, setRejectionNotes] = useState("");
  const [profileOverride, setProfileOverride] = useState<string | null>(null);
  const [cardParallelSnapshot, setCardParallelSnapshot] =
    useState<ParallelTaskSnapshot | null>(null);
  // v1.0.x wave 5 session 4:retry-references cascade 二次确认 + task progress
  const [cascadeConfirmOpen, setCascadeConfirmOpen] = useState(false);
  // 估算 cascade 章数:chapter list 过滤 chapter_id > 当前(字典序,对齐后端
  // _collect_subsequent_chapter_card_ids 同型字符串比较口径)
  const chapterListQuery = useChapterListQuery(workspaceId);
  const estimatedCascadeCount = useMemo(() => {
    if (!chapterListQuery.data) return null;
    return chapterListQuery.data.filter((c) => c.chapter_id > chapterId).length;
  }, [chapterListQuery.data, chapterId]);

  const updateMutation = useUpdateChapterCardMutation();
  const approveMutation = useApproveCardMutation();
  const rejectMutation = useRejectCardMutation();
  const writeMutation = useWriteChapterMutation();
  const retryRefMutation = useRetryReferencesMutation();
  const generateCardMutation = useGenerateCardMutation();
  const volumesQuery = useVolumesQuery(workspaceId);
  const validatorReportQuery = useChapterValidatorReportQuery(
    workspaceId,
    chapterId,
  );

  const missingCardLocation = useMemo(
    () => resolveChapterLocation(chapterId, volumesQuery.data?.volumes ?? []),
    [chapterId, volumesQuery.data],
  );
  const isMissingCard =
    isApiError(cardQuery.error) &&
    cardQuery.error.code === "CHAPTER_CARD_NOT_FOUND";

  const generateCardTask = useAsyncTaskState<{
    task_id?: string;
    parallel_group?: string;
    max_concurrency?: number;
    tasks?: unknown[];
    error?: { message?: string };
  }>({
    kind: "chapter_card",
    onMessage: (frame) => {
      if (frame.event !== "stage") return;
      const snapshot = parseParallelTaskSnapshot(
        frame.data,
        "chapter_card_semantic_gates",
      );
      if (snapshot) setCardParallelSnapshot(snapshot);
    },
    onSuccess: () => {
      void cardQuery.refetch();
      showSuccessToast("章节卡生成完成", `${chapterId} 已生成，等待人工审核`);
    },
  });

  // dirty 判定:进入编辑模式 + draft 与服务端 card 不同。
  const isDirty = useMemo(() => {
    if (!isEditing || !draft || !cardQuery.data) return false;
    return JSON.stringify(draft) !== JSON.stringify(cardQuery.data);
  }, [isEditing, draft, cardQuery.data]);
  useUnsavedChanges(isDirty);

  // 写作进度订阅 — J2.1(w4-s7)收敛到 useAsyncTaskState:taskId + WS +
  // timeout(chapter_write=5min,默认文案与原 handleWriteTimeout 一致)+ done/
  // failed/cancelled 终态分发。toast 里的 task_id 改读 done 帧 data.task_id
  // (hook 已 reset 自身 taskId)。
  const writeTask = useAsyncTaskState<{
    task_id?: string;
    stage?: string;
    error?: { message?: string };
  }>({
    kind: "chapter_write",
    onSuccess: (frame) => {
      showSuccessToast(
        "写作完成",
        `task_id=${frame.data?.task_id ?? "—"};回到看板查看新版本`,
      );
    },
    // onFailed 不传 → 默认 showApiErrorToast(错误信息 ?? "章节任务失败")。
  });

  const enterEditMode = () => {
    if (!cardQuery.data) return;
    setDraft(structuredClone(cardQuery.data));
    setIsEditing(true);
  };
  const cancelEdit = () => {
    setDraft(null);
    setIsEditing(false);
  };
  const handleSaveEdits = async () => {
    if (!draft) return;
    try {
      await updateMutation.mutateAsync({
        workspaceId,
        chapterId,
        card: draft,
      });
      showSuccessToast("章节卡已保存");
      setIsEditing(false);
      setDraft(null);
    } catch (err) {
      showApiErrorToast(err);
    }
  };
  const handleApprove = async () => {
    try {
      const result = await approveMutation.mutateAsync({
        workspaceId,
        chapterId,
      });
      showSuccessToast(
        "章节卡已批准",
        `${result.from_status} → ${result.to_status}`,
      );
    } catch (err) {
      showApiErrorToast(err);
    }
  };
  const handleReject = async () => {
    try {
      const result = await rejectMutation.mutateAsync({
        workspaceId,
        chapterId,
        rejectionNotes: rejectionNotes.trim() || null,
      });
      showSuccessToast(
        "章节卡已退回",
        `${result.from_status} → ${result.to_status}`,
      );
      setRejectionNotes("");
    } catch (err) {
      showApiErrorToast(err);
    }
  };
  const handleWrite = async () => {
    try {
      const task = await writeMutation.mutateAsync({
        workspaceId,
        chapterId,
        profileOverride,
      });
      writeTask.start(task.task_id);
      showSuccessToast(
        "写作任务已触发",
        `task_id=${task.task_id};进度走 WS 更新`,
      );
    } catch (err) {
      showApiErrorToast(err);
    }
  };
  const handleGenerateMissingCard = async () => {
    if (!missingCardLocation) return;
    setCardParallelSnapshot(null);
    try {
      const task = await generateCardMutation.mutateAsync({
        workspaceId,
        chapterId,
        volumeId: missingCardLocation.volumeId,
        chapterNumber: missingCardLocation.chapterNumber,
      });
      generateCardTask.start(task.task_id);
      showSuccessToast(
        "章节卡生成任务已触发",
        `task_id=${task.task_id};完成后仍需人工审核批准`,
      );
    } catch (err) {
      showApiErrorToast(err);
    }
  };
  // v1.0.x wave 5 session 4:retry-references 改两步流程 — 先开级联二次
  // 确认对话框,用户确认后才真正触发 mutation。对齐 plan §s4 k 件字面
  // "二次确认对话框,对照 P6-F-06 RewindConfirmDialog 同型"。
  const handleRetryReferences = () => {
    if (!cardQuery.data) return;
    setCascadeConfirmOpen(true);
  };
  const handleConfirmedRetryReferences = async () => {
    const card = cardQuery.data;
    if (!card) return;
    setCascadeConfirmOpen(false);
    try {
      const result = await retryRefMutation.mutateAsync({
        workspaceId,
        chapterId,
        volumeId: card.volume_id,
        chapterNumber: card.chapter_number,
      });
      const cascadeMsg = result.invalidated_chapter_ids.length
        ? `;级联 ${result.invalidated_chapter_ids.length} 章节卡后台重生中`
        : "(无后续章节卡 — 末章,无级联)";
      showSuccessToast(
        "已重试引用合法性",
        `章节卡已 LLM 重写;card_status=${result.card.card_status}${cascadeMsg}`,
      );
      if (result.cascade_regenerate_task_id) {
        cascadeTask.start(result.cascade_regenerate_task_id);
      }
    } catch (err) {
      showApiErrorToast(err);
    }
  };
  // v1.0.x wave 5 session 4:cascade 进度订阅(对照 write 同型)。
  // J2.1(w4-s7):收敛到 useAsyncTaskState(chapter_cascade=10min,默认超时
  // 文案与原 handleCascadeTimeout 一致)。
  const cascadeTask = useAsyncTaskState<{
    task_id?: string;
    stage?: string;
    error?: { message?: string };
  }>({
    kind: "chapter_cascade",
    onSuccess: (frame) => {
      showSuccessToast(
        "章节卡级联重生完成",
        `task_id=${frame.data?.task_id ?? "—"};所有后续卡已基于新上下文重生`,
      );
    },
    // onFailed 不传 → 默认 showApiErrorToast(错误信息 ?? "章节卡级联重生失败")。
  });

  return (
    <div className="space-y-4">
      <header className="flex flex-wrap items-center gap-3 border-b border-border pb-2">
        {props.children}
      </header>

      <CardBody
        cardQuery={cardQuery}
        isEditing={isEditing}
        draft={draft}
        onDraftChange={setDraft}
      />

      {isMissingCard && (
        <MissingCardRecovery
          chapterId={chapterId}
          location={missingCardLocation}
          volumesLoading={volumesQuery.isPending}
          volumesError={volumesQuery.error}
          isGenerating={
            generateCardMutation.isPending || generateCardTask.isRunning
          }
          isCancelling={generateCardTask.isCancelling}
          taskId={generateCardTask.taskId}
          parallelSnapshot={cardParallelSnapshot}
          onGenerate={handleGenerateMissingCard}
          onCancel={generateCardTask.cancel}
        />
      )}

      {cardQuery.data && (
        <>
          <BadgesBar card={cardQuery.data} />

          <ActionsToolbar
            workspaceId={workspaceId}
            chapterId={chapterId}
            card={cardQuery.data}
            isEditing={isEditing}
            isDirty={isDirty}
            isSavingEdits={updateMutation.isPending}
            isApproving={approveMutation.isPending}
            isRejecting={rejectMutation.isPending}
            // v1.0.x Commit B::write 改异步:mutation pending 仅启动期,
            // 真"写作中"信号由 writeTask.isRunning(WS task progress 监听 done 清空)。
            isWriting={writeMutation.isPending || writeTask.isRunning}
            isRetryingRefs={retryRefMutation.isPending}
            profileOverride={profileOverride}
            onProfileOverrideChange={setProfileOverride}
            rejectionNotes={rejectionNotes}
            onRejectionNotesChange={setRejectionNotes}
            onEnterEdit={enterEditMode}
            onCancelEdit={cancelEdit}
            onSaveEdits={handleSaveEdits}
            onApprove={handleApprove}
            onReject={handleReject}
            onWrite={handleWrite}
            onRetryReferences={handleRetryReferences}
            onUploaded={() => {
              setIsEditing(false);
              setDraft(null);
            }}
          />

          {writeTask.isRunning && (
            <div
              data-testid="write-task-banner"
              className="flex items-center gap-2 rounded border border-sky-200 bg-sky-50 p-2 text-xs text-sky-800"
            >
              <span>
                写作任务正在执行 · task_id=
                <code className="break-all">{writeTask.taskId}</code>
              </span>
              <CancelTaskButton
                onCancel={writeTask.cancel}
                isCancelling={writeTask.isCancelling}
                testId="write-task-cancel"
                className="ml-auto"
              />
            </div>
          )}

          {cascadeTask.isRunning && (
            <div
              data-testid="cascade-task-banner"
              className="flex items-center gap-2 rounded border border-amber-200 bg-amber-50 p-2 text-xs text-amber-800"
            >
              <span>
                章节卡级联重生进行中 · task_id=
                <code className="break-all">{cascadeTask.taskId}</code>
                <span className="ml-2 text-muted-foreground">
                  (后续章节卡基于更新后的上下文重生,完成后 toast 提示)
                </span>
              </span>
              <CancelTaskButton
                onCancel={cascadeTask.cancel}
                isCancelling={cascadeTask.isCancelling}
                testId="cascade-task-cancel"
                className="ml-auto shrink-0"
              />
            </div>
          )}

          <CardRetryCascadeConfirmDialog
            open={cascadeConfirmOpen}
            onOpenChange={setCascadeConfirmOpen}
            chapterId={chapterId}
            estimatedCascadeCount={estimatedCascadeCount}
            onConfirm={handleConfirmedRetryReferences}
            onCancel={() => setCascadeConfirmOpen(false)}
            isConfirmDisabled={retryRefMutation.isPending}
          />

          {!isEditing && (
            <>
              <MustAchieveCheckList
                cardMustAchieve={cardQuery.data.must_achieve}
                cardMustUnresolved={cardQuery.data.must_unresolved}
                cardForbidden={cardQuery.data.forbidden}
                cardRelatedCharacters={cardQuery.data.related_characters}
                report={validatorReportQuery.data}
                isLoading={validatorReportQuery.isPending}
                error={validatorReportQuery.error}
              />
              <CoreStateAuditPanel
                workspaceId={workspaceId}
                card={cardQuery.data}
              />
            </>
          )}
        </>
      )}
    </div>
  );
}

interface MissingCardRecoveryProps {
  chapterId: string;
  location: ChapterLocation | null;
  volumesLoading: boolean;
  volumesError: unknown;
  isGenerating: boolean;
  isCancelling: boolean;
  taskId: string | null;
  parallelSnapshot: ParallelTaskSnapshot | null;
  onGenerate: () => void;
  onCancel: () => void;
}

function MissingCardRecovery(props: MissingCardRecoveryProps) {
  const locationUnavailable =
    !props.volumesLoading && !props.volumesError && !props.location;
  return (
    <section
      data-testid="missing-card-recovery"
      className="space-y-3 rounded border border-amber-200 bg-amber-50 p-4 text-sm text-amber-900"
    >
      <div>
        <p className="font-medium">本章卡尚未落盘</p>
        <p className="mt-1 text-xs leading-relaxed text-amber-800">
          可从正式产品路径重新生成 {props.chapterId}；生成完成后仍需逐项审核并批准，
          不会自动开始正文或替换 run。
        </p>
      </div>
      {props.location && (
        <p className="text-xs">
          生成位置：<code>{props.location.volumeId}</code> · 卷内第 {props.location.chapterNumber} 章
        </p>
      )}
      {Boolean(props.volumesError) && (
        <p className="text-xs text-destructive">卷数据加载失败，暂不能安全生成。</p>
      )}
      {locationUnavailable && (
        <p className="text-xs text-destructive">
          无法从已提交卷数据定位本章，已禁止猜测 volume_id 或章号。
        </p>
      )}
      {props.parallelSnapshot && (
        <ParallelTaskStatusList
          title="章节卡写前双 Judge"
          snapshot={props.parallelSnapshot}
          testId="missing-card-parallel-status"
        />
      )}
      <div className="flex items-center gap-2">
        <Button
          size="sm"
          onClick={props.onGenerate}
          disabled={props.volumesLoading || !props.location || props.isGenerating}
          data-testid="missing-card-generate-btn"
        >
          {props.isGenerating ? "生成中…" : "重新生成本章卡"}
        </Button>
        {props.taskId && (
          <>
            <span className="text-xs text-muted-foreground">
              task_id=<code className="break-all">{props.taskId}</code>
            </span>
            <CancelTaskButton
              onCancel={props.onCancel}
              isCancelling={props.isCancelling}
              testId="missing-card-task-cancel"
            />
          </>
        )}
      </div>
    </section>
  );
}

interface CardBodyProps {
  cardQuery: ReturnType<typeof useChapterCardQuery>;
  isEditing: boolean;
  draft: ChapterCardOut | null;
  onDraftChange: (next: ChapterCardOut) => void;
}

function CardBody(props: CardBodyProps) {
  if (props.cardQuery.isPending) {
    return (
      <p className="rounded border border-border p-6 text-sm text-muted-foreground">
        加载章节卡中…
      </p>
    );
  }
  if (props.cardQuery.error) {
    return <CardErrorView error={props.cardQuery.error} />;
  }
  if (!props.cardQuery.data) {
    return (
      <p className="rounded border border-dashed border-border p-6 text-sm text-muted-foreground">
        没有章节卡数据。
      </p>
    );
  }
  return (
    <ChapterCardReviewPanel
      card={props.cardQuery.data}
      isEditing={props.isEditing}
      draft={props.draft}
      onDraftChange={props.onDraftChange}
    />
  );
}

function CardErrorView(props: { error: unknown }) {
  const apiErr = isApiError(props.error) ? props.error : null;
  const message = apiErr
    ? `${apiErr.code}: ${apiErr.message}`
    : props.error instanceof Error
      ? props.error.message
      : "未知错误";
  return (
    <div
      data-testid="card-load-error"
      className="rounded border border-destructive bg-destructive/10 p-4 text-sm text-destructive"
    >
      加载章节卡失败:{message}
    </div>
  );
}

function BadgesBar(props: { card: ChapterCardOut }) {
  return (
    <div
      data-testid="card-badges-bar"
      className="flex flex-wrap items-center gap-2 text-xs text-muted-foreground"
    >
      <span>第 {props.card.chapter_number} 章 · </span>
      <span className="font-medium text-foreground">{props.card.title}</span>
      <span className="ml-2 inline-flex items-center gap-1">
        <span className="text-[10px] uppercase tracking-wide">card</span>
        <CardStatusBadge status={props.card.card_status} />
      </span>
      <span className="inline-flex items-center gap-1">
        <span className="text-[10px] uppercase tracking-wide">chapter</span>
        <ChapterStateBadge status={null} />
      </span>
      {props.card.auto_retry_status === "exhausted" && (
        <span
          data-testid="auto-retry-exhausted-badge"
          title="多卡级联恢复时自动避免雷同 3 次仍超阈值,已保留首次卡;建议人工编辑或手动点'重试引用合法性'再生成"
          className="ml-2 inline-flex items-center gap-1 rounded border border-destructive bg-destructive/10 px-2 py-0.5 font-medium text-destructive"
        >
          <span aria-hidden>⚠</span>
          自动避雷同 3 次失败
        </span>
      )}
      {props.card.card_approved_at && (
        <span className="ml-2 text-[10px]">
          approved_at:{" "}
          <code className="font-mono">{props.card.card_approved_at}</code>
        </span>
      )}
    </div>
  );
}

interface ActionsToolbarProps {
  workspaceId: string;
  chapterId: string;
  card: ChapterCardOut;
  isEditing: boolean;
  isDirty: boolean;
  isSavingEdits: boolean;
  isApproving: boolean;
  isRejecting: boolean;
  isWriting: boolean;
  isRetryingRefs: boolean;
  profileOverride: string | null;
  onProfileOverrideChange: (v: string | null) => void;
  rejectionNotes: string;
  onRejectionNotesChange: (v: string) => void;
  onEnterEdit: () => void;
  onCancelEdit: () => void;
  onSaveEdits: () => void;
  onApprove: () => void;
  onReject: () => void;
  onWrite: () => void;
  onRetryReferences: () => void;
  onUploaded: () => void;
}

function ActionsToolbar(props: ActionsToolbarProps) {
  const { card, isEditing } = props;
  const isApproved = card.card_status === "approved";
  const isRejected = card.card_status === "rejected";
  // wave 6 s2:多卡级联恢复自动 retry 3 次失败 → exhausted,允许手动 retry 接力
  // (与 rejected 同型,但语义独立:exhausted = 系统判定,rejected = 用户判定)
  const isAutoRetryExhausted = card.auto_retry_status === "exhausted";
  const canRetryReferences =
    (isRejected || isAutoRetryExhausted) && !isEditing;
  return (
    <section
      data-testid="card-actions-toolbar"
      className="space-y-3 rounded border border-border bg-card p-4"
    >
      <div className="flex flex-wrap items-center gap-2">
        {!isEditing ? (
          <>
            <Button
              size="sm"
              onClick={props.onApprove}
              disabled={isApproved || props.isApproving}
              data-testid="card-approve-btn"
            >
              {props.isApproving ? "批准中…" : "批准"}
            </Button>
            <Button
              size="sm"
              variant="outline"
              onClick={props.onReject}
              disabled={isRejected || props.isRejecting}
              data-testid="card-reject-btn"
            >
              {props.isRejecting ? "退回中…" : "退回修改"}
            </Button>
            <Button
              size="sm"
              variant="outline"
              onClick={props.onEnterEdit}
              data-testid="card-enter-edit-btn"
            >
              编辑
            </Button>
          </>
        ) : (
          <>
            <Button
              size="sm"
              onClick={props.onSaveEdits}
              disabled={!props.isDirty || props.isSavingEdits}
              data-testid="card-save-edits-btn"
            >
              {props.isSavingEdits ? "保存中…" : "保存修改"}
            </Button>
            <Button
              size="sm"
              variant="outline"
              onClick={props.onCancelEdit}
              disabled={props.isSavingEdits}
              data-testid="card-cancel-edit-btn"
            >
              取消编辑
            </Button>
          </>
        )}
        <span className="mx-2 hidden h-4 w-px bg-border md:inline-block" />
        <ChapterCardUploader
          workspaceId={props.workspaceId}
          chapterId={props.chapterId}
          onUploaded={props.onUploaded}
        />
        {canRetryReferences && (
          <Button
            size="sm"
            variant="outline"
            onClick={props.onRetryReferences}
            disabled={props.isRetryingRefs}
            data-testid="card-retry-references-btn"
          >
            {props.isRetryingRefs
              ? "重试中…"
              : isAutoRetryExhausted && !isRejected
                ? "重试避免雷同"
                : "重试引用合法性"}
          </Button>
        )}
        {isApproved && !isEditing && (
          <Button
            size="sm"
            variant="default"
            onClick={props.onWrite}
            disabled={props.isWriting}
            data-testid="card-write-btn"
            className="ml-auto"
          >
            {props.isWriting ? "触发中…" : "重新写作"}
          </Button>
        )}
      </div>

      {!isEditing && !isRejected && (
        <div className="space-y-1">
          <label
            htmlFor="rejection-notes"
            className="block text-xs font-medium text-muted-foreground"
          >
            退回原因(可选,持久化到 ChapterCardORM.rejection_notes)
          </label>
          <textarea
            id="rejection-notes"
            value={props.rejectionNotes}
            onChange={(e) => props.onRejectionNotesChange(e.target.value)}
            rows={2}
            placeholder="例:tension_level 偏低,需提升到 8 以上"
            className="w-full rounded border border-input bg-background px-2 py-1.5 text-sm focus-visible:outline-none focus-visible:ring-2 focus-visible:ring-ring"
            data-testid="rejection-notes-textarea"
          />
        </div>
      )}

      {isApproved && !isEditing && (
        <WriterProfileSelector
          workspaceId={props.workspaceId}
          value={props.profileOverride}
          onChange={props.onProfileOverrideChange}
          disabled={props.isWriting}
        />
      )}

      {isEditing && (
        <p
          data-testid="dirty-hint"
          className="text-xs text-muted-foreground"
        >
          {props.isDirty
            ? "已修改章节卡,点保存或取消;离开会被拦截"
            : "未做修改;可直接取消"}
        </p>
      )}
    </section>
  );
}
