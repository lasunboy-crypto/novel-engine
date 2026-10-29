/**
 * RunControlPage(P6-F-02 三按钮+状态卡 → v2 w1-s4 完整进度面板)。
 *
 * 路由 `/w/:workspaceId/run`(对齐 FRONTEND_SPEC §1 路由清单 + §4.10
 * "RunControlPage(运行控制)"全部 6 字面要点):
 *
 * 1. 当前流水线状态(运行中/暂停/空闲)         → `PipelineControlPanel` 顶部
 * 2. 启动控制:从某章开始 / 写 N 章 / 写到某章 / 写完整卷 → scope 4 档表单
 * 3. Writer Profile 临时覆盖(本次运行内有效)   → `WriterProfileSelector`
 * 4. 暂停 / 继续 / 终止 按钮                    → 状态驱动可点性
 * 5. 实时输出窗口(WS 推送)                    → `PipelineLiveOutput`
 * 6. 异常时高亮 + 跳转链接到对应章节人工介入页  → 同上(severity=warn/error)
 *
 * v2 w1-s4 升级(wave 1 后端能力 UI 化,plan §4 w1-s4 卡):
 *
 * 7. 失败策略选择器(stop/skip/retry 3 档)      → `PipelineControlPanel` 表单
 * 8. 逐章进度列表(完成/跳过/当前/待写 + 章状态徽章)→ `PipelineChapterProgressList`
 * 9. 孤儿章/成本护栏/中断横幅 + 直达/续跑/自愈   → `PipelineRunBanner`
 * 10. reset-stale 一键自愈(w1-s2 端点)          → `handleResetStale` + toast
 *
 * 页面级 onChapterFinished 回调:invalidate `["chapters", workspaceId]` 与
 * `["pipeline-status", workspaceId]`,让 ChaptersBoardPage / status 卡跨页面
 * 对账(P5 候选 4 task done 兜底的"pipeline 视角"半;AppShell 全局兜底另一半)。
 */

import { useEffect } from "react";
import { useParams } from "react-router-dom";
import { useQueryClient } from "@tanstack/react-query";

import { PipelineChapterProgressList } from "@/components/PipelineChapterProgressList";
import { PipelineControlPanel } from "@/components/PipelineControlPanel";
import { PipelineSkippedSummary } from "@/components/PipelineSkippedSummary";
import { PipelineLiveOutput } from "@/components/PipelineLiveOutput";
import { PipelineRunBanner } from "@/components/PipelineRunBanner";
import { showApiErrorToast, showSuccessToast } from "@/components/Toast";
import { useResetStaleChaptersMutation } from "@/api/chapters";
import {
  PIPELINE_STATUS_QUERY_KEY,
  usePausePipelineMutation,
  usePipelineStatusQuery,
  useResumePipelineMutation,
  useStartPipelineMutation,
  useStopPipelineMutation,
  type PipelineStartIn,
} from "@/api/pipeline";
import { useActiveWorkspaceStore } from "@/store/activeWorkspace";

export default function RunControlPage() {
  const { workspaceId } = useParams<{ workspaceId: string }>();
  const queryClient = useQueryClient();
  const setActiveWorkspaceId = useActiveWorkspaceStore(
    (s) => s.setActiveWorkspaceId,
  );

  // 进入页面时同步 activeWorkspace store(AppShell 顶部 nav / 全局兜底用到)。
  useEffect(() => {
    if (workspaceId) {
      setActiveWorkspaceId(workspaceId);
    }
  }, [workspaceId, setActiveWorkspaceId]);

  const statusQuery = usePipelineStatusQuery(workspaceId);
  const startMutation = useStartPipelineMutation();
  const pauseMutation = usePausePipelineMutation();
  const resumeMutation = useResumePipelineMutation();
  const stopMutation = useStopPipelineMutation();
  const resetStaleMutation = useResetStaleChaptersMutation();

  const busy =
    startMutation.isPending ||
    pauseMutation.isPending ||
    resumeMutation.isPending ||
    stopMutation.isPending;

  if (!workspaceId) {
    return (
      <div className="rounded border border-destructive bg-destructive/10 p-4 text-sm text-destructive">
        路由参数 workspaceId 缺失。请从工作区列表进入运行控制页。
      </div>
    );
  }

  const handleStart = async (body: PipelineStartIn) => {
    try {
      const result = await startMutation.mutateAsync({ workspaceId, body });
      showSuccessToast(
        "流水线已启动",
        `run_id: ${result.run_id} · 共 ${result.chapter_ids.length} 章`,
      );
    } catch (err) {
      showApiErrorToast(err);
    }
  };

  const handlePause = async () => {
    try {
      await pauseMutation.mutateAsync({ workspaceId });
      showSuccessToast("已请求暂停", "下一章开始前会停止");
    } catch (err) {
      showApiErrorToast(err);
    }
  };

  const handleResume = async () => {
    try {
      await resumeMutation.mutateAsync({ workspaceId });
      showSuccessToast("已请求继续", "后台循环已重启");
    } catch (err) {
      showApiErrorToast(err);
    }
  };

  const handleStop = async () => {
    try {
      await stopMutation.mutateAsync({ workspaceId });
      showSuccessToast("已请求终止", "流水线 state 标 stopped");
    } catch (err) {
      showApiErrorToast(err);
    }
  };

  // v2 w1-s4:reset-stale 一键自愈(w1-s2 端点)。skipped_reason 非空 =
  // 后端防误杀对账跳过扫描(有 running run / 活跃任务),提示稍后重试。
  const handleResetStale = async () => {
    try {
      const result = await resetStaleMutation.mutateAsync({ workspaceId });
      if (result.skipped_reason) {
        showSuccessToast(
          "本次未扫描",
          `原因:${result.skipped_reason}(有运行中的任务,等它结束后重试)`,
        );
      } else if (result.reset_count === 0) {
        showSuccessToast("无需自愈", "没有滞留在中间态的章节");
      } else {
        showSuccessToast(
          `已重置 ${result.reset_count} 章`,
          `翻回起草态:${result.reset_chapter_ids.join(", ")}`,
        );
      }
    } catch (err) {
      showApiErrorToast(err);
    }
  };

  // chapter_finished WS event 来时:invalidate chapters 列表 + pipeline-status,
  // 让 ChaptersBoardPage 跨页面看到状态翻 passed + 进度条更新。
  // (PipelineLiveOutput 回调签名带 chapterId 但本页只 invalidate 不用 id,
  // TS 允许 less-arity 函数兼容,B-08 hot-fix 删占位避免 lint unused-vars。)
  const handleChapterFinished = () => {
    void queryClient.invalidateQueries({
      queryKey: ["chapters", workspaceId],
    });
    void queryClient.invalidateQueries({
      queryKey: PIPELINE_STATUS_QUERY_KEY(workspaceId),
    });
  };

  // 任一生命周期 WS event 来时都重读权威状态。尤其 Continue 可能在
  // resumed 后立即因安全闸 paused；只刷新 stopped 会把页面卡在假 running。
  const handlePipelineStatusChanged = () => {
    void queryClient.invalidateQueries({
      queryKey: PIPELINE_STATUS_QUERY_KEY(workspaceId),
    });
  };

  const currentChapter = statusQuery.data?.current_chapter ?? null;

  return (
    <div className="space-y-4" data-testid="run-control-page">
      <header className="space-y-1">
        <h1 className="text-2xl font-semibold">运行控制</h1>
        <p className="text-sm text-muted-foreground">
          管理工作区 <code>{workspaceId}</code> 的流水线:启动 scope 4 档(单章
          / 区间 / 整卷 / 直到)+ 暂停 / 继续 / 终止;实时输出窗口接 WS
          <code> pipeline.events</code> 与当前章节
          <code> chapter.&#123;id&#125;.events</code> 双频道。
        </p>
      </header>

      <PipelineRunBanner
        status={statusQuery.data}
        onResume={() => {
          void handleResume();
        }}
        onResetStale={() => {
          void handleResetStale();
        }}
        busy={busy}
        isResettingStale={resetStaleMutation.isPending}
      />

      <div className="grid grid-cols-1 gap-4 lg:grid-cols-2">
        <PipelineControlPanel
          workspaceId={workspaceId}
          status={statusQuery.data}
          statusLoading={statusQuery.isLoading}
          statusError={statusQuery.error as Error | null}
          onStart={handleStart}
          onPause={handlePause}
          onResume={handleResume}
          onStop={handleStop}
          busy={busy}
        />
        <PipelineLiveOutput
          workspaceId={workspaceId}
          currentChapterId={currentChapter}
          onChapterFinished={handleChapterFinished}
          onPipelineStatusChanged={handlePipelineStatusChanged}
        />
      </div>

      <PipelineSkippedSummary
        workspaceId={workspaceId}
        status={statusQuery.data}
      />

      <PipelineChapterProgressList
        workspaceId={workspaceId}
        status={statusQuery.data}
      />
    </div>
  );
}
