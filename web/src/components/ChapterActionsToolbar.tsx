/**
 * ChapterActionsToolbar(P1-F-14):章节工具栏 3 按钮 + P3-F-03 起内嵌
 * WriterProfileSelector。
 *
 * 页头下方、三栏 grid 上方的水平工具栏:
 *
 * - **开始写作** → `:write`(后端跑完整闭环 write → review → revise/rewrite
 *   → ... → pass / 升人工)。P3-B-11 起 body 携带 `profile_override`
 *   (单次写作覆盖,`null` = 走 routing 默认)。
 * - **重写** → `:rewrite`(强制以重写方式再跑一次完整闭环)。body 含
 *   `prior_rewrite_count` + P3-B-11 起 `profile_override`。
 * - **修订** → `:revise`(基于选中的 review 条目 revise_targets 调 Reviser
 *   落新版本)。需父组件传入 `selectedReview`(某条 ReviewSummaryEntry);
 *   未选中时按钮禁用 + `title` 提示。**P3-F-03 决策 3:revise 不接
 *   profile_override**(走 Reviser 单独路径,典型场景无切换需求,留 P6)。
 *
 * P3-F-03 把 `WriterProfileSelector` 内嵌到本工具栏右侧(同 flex 容器,与
 * 操作按钮同行)——节省垂直空间,且语义上"选择 profile + 触发写作"是同一个
 * 决策动作。Selector 受控,父组件 ChapterEditorPage 持有 state(:write /
 * :rewrite 共享同一选择)。
 *
 * ## 待审决策落地
 *
 * - 待审 1 拍 A:`selectedReview` 由父组件 `ChapterEditorPage` 持有,通过
 *   props 传入;工具栏纯渲染,不引入 zustand
 * - 待审 2 拍 A:任一选中条目都可点(空 revise_targets 后端兜底,前端不替
 *   后端做校验)—— 仅以"selectedReview != null"作启用判定
 * - 待审 3 拍 A:`source_version_id` 显式传 `selectedReview.version_reviewed`
 *
 * ## task_id 写入路径
 *
 * 3 个 mutation onSuccess 都调 `setChapterTaskId(chapterId, task_id)` 写入
 * P1-F-13 留下的 zustand store。`ReviewHistoryPanel` 订阅 `useChapterTaskId`
 * 自动激活 review_summary 列表;P1-F-15 订阅同 store 触发 WS 进度。
 *
 * ## 错误处理
 *
 * 失败统一走 `showApiErrorToast`,按错误码翻译(`CHAPTER_INVALID_STATE` /
 * `CHAPTER_CARD_NOT_FOUND` 等)。成功反馈 `showSuccessToast` 带 task_id 后
 * 8 位 + 任务类型(对账用)。
 */

import { useQueryClient } from "@tanstack/react-query";

import {
  CHAPTERS_QUERY_KEY,
  invalidateChapterArtifacts,
  useReviseChapterMutation,
  useRewriteChapterMutation,
  useWriteChapterMutation,
  type ReviewIssueIn,
} from "@/api/chapters";
import { CHAPTER_REVIEW_HISTORY_QUERY_KEY } from "@/api/chapters";
import { CancelTaskButton } from "@/components/CancelTaskButton";
import { showApiErrorToast, showSuccessToast } from "@/components/Toast";
import { Button } from "@/components/ui/button";
import { WriterProfileSelector } from "@/components/WriterProfileSelector";
import { useAsyncTaskState } from "@/hooks/useAsyncTaskState";
import { useActiveTaskStore } from "@/store/activeTask";
import type { ReviewSummaryEntry } from "@/api/tasks";

interface ChapterActionsToolbarProps {
  workspaceId: string;
  chapterId: string;
  /** 当前 review_summary(最近一次 :write/:rewrite 任务的产出),用于推算 priorRewriteCount。null = 没有任务历史(首次写作)。 */
  reviewSummary: ReviewSummaryEntry[] | null;
  /** ReviewHistoryPanel 中选中的某条 review 条目;null = 未选中。 */
  selectedReview: ReviewSummaryEntry | null;
  /** P3-F-03:本次 :write / :rewrite 的 profile 覆盖;null = 走 RoutingConfig 默认。 */
  profileOverride: string | null;
  /** P3-F-03:profile 覆盖 onChange callback,父组件持 state。 */
  onProfileOverrideChange: (value: string | null) => void;
}

export function ChapterActionsToolbar(props: ChapterActionsToolbarProps) {
  const writeMutation = useWriteChapterMutation();
  const rewriteMutation = useRewriteChapterMutation();
  const reviseMutation = useReviseChapterMutation();
  const setChapterTaskId = useActiveTaskStore((s) => s.setChapterTaskId);
  const queryClient = useQueryClient();

  // v1.0.x Commit B(2026-05-21):3 个长跑端点(:write / :rewrite / :revise)
  // 后端改 BackgroundTasks 异步,mutation onSuccess 立即返(task pending),
  // 真正闭环跑完的信号来自 WS task.{task_id}.progress 频道的 done/failed event。
  // J2.1(w4-s7):taskId + WS + timeout + 终态统一到 useAsyncTaskState。kind
  // 区分(write/rewrite/revise)用 hook 的 meta 承载,供按钮文案 + 与 taskId 同生灭。
  // 默认 timeout 文案(chapter_write=5min)与原 handleRunningTimeout 一致。
  type RunningKind = "write" | "rewrite" | "revise";
  // done/failed 都二次 invalidate 拿最终 review_summary / final_path。
  const invalidateAfterRun = () => {
    invalidateChapterArtifacts(queryClient, props.workspaceId, props.chapterId);
    void queryClient.invalidateQueries({
      queryKey: CHAPTERS_QUERY_KEY(props.workspaceId),
    });
    void queryClient.invalidateQueries({
      queryKey: CHAPTER_REVIEW_HISTORY_QUERY_KEY(
        props.workspaceId,
        props.chapterId,
      ),
    });
  };
  const task = useAsyncTaskState<unknown, RunningKind>({
    kind: "chapter_write",
    onSuccess: () => {
      showSuccessToast("任务完成", "可在右栏审稿历史查看最新结果");
      invalidateAfterRun();
    },
    onFailed: (message) => {
      showApiErrorToast(new Error(message));
      invalidateAfterRun();
    },
  });

  // 当前已用重写次数 = review_summary 里 verdict="rewrite" 条目数(P1 估算,
  // P2 后端落 chapters 表后改读后端字段,本字段会 deprecate)
  const priorRewriteCount = (props.reviewSummary ?? []).filter(
    (e) => e.verdict === "rewrite",
  ).length;

  const anyPending =
    writeMutation.isPending ||
    rewriteMutation.isPending ||
    reviseMutation.isPending ||
    task.isRunning;

  const handleWrite = () => {
    writeMutation.mutate(
      {
        workspaceId: props.workspaceId,
        chapterId: props.chapterId,
        profileOverride: props.profileOverride,
      },
      {
        onSuccess: (taskInfo) => {
          setChapterTaskId(props.chapterId, taskInfo.task_id);
          task.start(taskInfo.task_id, "write");
          showSuccessToast(
            "写作任务已启动",
            `task=${taskInfo.task_id.slice(-8)} · 后台跑闭环,完成后自动刷新`,
          );
        },
        onError: (error) => {
          showApiErrorToast(error);
        },
      },
    );
  };

  const handleRewrite = () => {
    rewriteMutation.mutate(
      {
        workspaceId: props.workspaceId,
        chapterId: props.chapterId,
        priorRewriteCount,
        profileOverride: props.profileOverride,
      },
      {
        onSuccess: (taskInfo) => {
          setChapterTaskId(props.chapterId, taskInfo.task_id);
          task.start(taskInfo.task_id, "rewrite");
          showSuccessToast(
            "重写任务已启动",
            `task=${taskInfo.task_id.slice(-8)} · prior_rewrite_count=${priorRewriteCount}`,
          );
        },
        onError: (error) => {
          showApiErrorToast(error);
        },
      },
    );
  };

  const handleRevise = () => {
    if (!props.selectedReview) return;
    const issues: ReviewIssueIn[] = props.selectedReview.revise_targets.map(
      (issue) => ({
        paragraph_anchor: issue.paragraph_anchor,
        issue_type: issue.issue_type,
        description: issue.description,
        suggestion: issue.suggestion,
      }),
    );
    reviseMutation.mutate(
      {
        workspaceId: props.workspaceId,
        chapterId: props.chapterId,
        issues,
        sourceVersionId: props.selectedReview.version_reviewed,
      },
      {
        onSuccess: (taskInfo) => {
          setChapterTaskId(props.chapterId, taskInfo.task_id);
          task.start(taskInfo.task_id, "revise");
          showSuccessToast(
            "修订任务已启动",
            `task=${taskInfo.task_id.slice(-8)} · ${issues.length} 条 issue · source v${props.selectedReview?.version_reviewed}`,
          );
        },
        onError: (error) => {
          showApiErrorToast(error);
        },
      },
    );
  };

  const reviseTitle = props.selectedReview
    ? `基于第 ${props.selectedReview.version_reviewed} 版的审稿条目(${props.selectedReview.revise_targets.length} 条 issue)修订`
    : "请先在右栏「审稿历史」选中一条审稿条目";

  return (
    <div
      data-testid="chapter-actions-toolbar"
      className="flex flex-wrap items-center gap-2 rounded border border-border bg-card px-3 py-2"
    >
      <span className="text-xs font-medium text-muted-foreground">操作</span>
      <Button
        data-testid="action-write"
        size="sm"
        onClick={handleWrite}
        disabled={anyPending}
      >
        {writeMutation.isPending
          ? "启动中…"
          : task.meta === "write"
            ? "写作中…"
            : "开始写作"}
      </Button>
      <Button
        data-testid="action-rewrite"
        size="sm"
        variant="secondary"
        onClick={handleRewrite}
        disabled={anyPending}
        title={`已重写 ${priorRewriteCount} 次`}
      >
        {rewriteMutation.isPending
          ? "启动中…"
          : task.meta === "rewrite"
            ? "重写中…"
            : "重写"}
      </Button>
      <Button
        data-testid="action-revise"
        size="sm"
        variant="secondary"
        onClick={handleRevise}
        disabled={anyPending || !props.selectedReview}
        title={reviseTitle}
      >
        {reviseMutation.isPending
          ? "启动中…"
          : task.meta === "revise"
            ? "修订中…"
            : "修订"}
      </Button>
      {task.isRunning && (
        <CancelTaskButton
          onCancel={task.cancel}
          isCancelling={task.isCancelling}
          testId="chapter-action-cancel"
        />
      )}
      <WriterProfileSelector
        workspaceId={props.workspaceId}
        value={props.profileOverride}
        onChange={props.onProfileOverrideChange}
        disabled={anyPending}
      />
    </div>
  );
}
