/**
 * OutlineTabContent(P5-F-03 落地)。
 *
 * 单 tab 容器:渲染对应 section 的只读结构化展示(`OutlineSectionView` 或
 * `MiniPlanTabContent`)+ 顶部"重新生成此项"按钮。按钮触发
 * `POST /outline:regenerate-section` body
 * `{section, premise, keep_others: true, genre, [volume_plans 段加 3 字段]}`。
 *
 * 成功后 mutation onSuccess 已 invalidate `OUTLINE_QUERY_KEY`,自动触发上游
 * `useOutlineQuery` 重拉并通过 props 流回最新 outline → 子视图重渲染。
 * 失败走 `showApiErrorToast`(对齐 NewBookPage 既有错误提示模式)。
 */

import { useQueryClient } from "@tanstack/react-query";

import {
  OUTLINE_QUERY_KEY,
  useRegenerateOutlineSectionMutation,
  type OutlineOut,
  type OutlineRegenerateSectionIn,
  type OutlineSection,
} from "@/api/outline";
import { CancelTaskButton } from "@/components/CancelTaskButton";
import { MiniPlanTabContent } from "@/components/MiniPlanTabContent";
import { OutlineSectionView } from "@/components/OutlineSectionView";
import { showApiErrorToast } from "@/components/Toast";
import { Button } from "@/components/ui/button";
import { useAsyncTaskState } from "@/hooks/useAsyncTaskState";

/** 重新生成单段所需的表单参数(NewBookPage 表单提交时透传)。 */
export interface RegenerateParams {
  premise: string;
  genre: string;
  total_volumes: number;
  chapters_per_volume: number;
  tension_template: OutlineRegenerateSectionIn["tension_template"];
  planning_mode?: OutlineRegenerateSectionIn["planning_mode"];
}

/** 确认前允许只读预览并单段重生的 5 段大纲。 */
export type RegenerableOutlineSection = OutlineSection | "mini_plan";

interface OutlineTabContentProps {
  workspaceId: string;
  outline: OutlineOut;
  section: RegenerableOutlineSection;
  regenerateParams: RegenerateParams;
  onTaskAccepted?: (taskId: string) => void;
  disabled?: boolean;
}

export function OutlineTabContent(props: OutlineTabContentProps) {
  const {
    workspaceId,
    outline,
    section,
    regenerateParams,
    onTaskAccepted,
    disabled,
  } = props;
  const regenerateMutation = useRegenerateOutlineSectionMutation();
  const queryClient = useQueryClient();

  // v1.0.x Commit B(2026-05-21):outline:regenerate-section 改 BackgroundTasks
  // 异步,真"正在重新生成"信号来自 task progress;mutation.isPending 仅瞬时启动期。
  // J2.1(w4-s7):taskId state + WS + timeout + 终态分发统一到 useAsyncTaskState。
  // 默认 timeout 文案(outline_section=5min)与原 handleRegenerateTimeout 一致,故省略 onTimeout。
  const invalidateOutline = () =>
    void queryClient.invalidateQueries({
      queryKey: OUTLINE_QUERY_KEY(workspaceId),
    });
  const task = useAsyncTaskState({
    kind: "outline_section",
    onSuccess: invalidateOutline,
    onFailed: (message) => {
      showApiErrorToast(new Error(message));
      invalidateOutline();
    },
  });
  const isRegenerating = regenerateMutation.isPending || task.isRunning;

  const handleRegenerate = async () => {
    const body: OutlineRegenerateSectionIn = {
      section,
      premise: regenerateParams.premise,
      keep_others: true,
      genre: regenerateParams.genre,
      ...(regenerateParams.planning_mode ? { planning_mode: regenerateParams.planning_mode } : {}),
      // world 同样依赖原书规模，不能回退到后端五卷三十章默认值。
      total_volumes:
        section === "volume_plans" || section === "world" ? regenerateParams.total_volumes : null,
      chapters_per_volume:
        section === "volume_plans" || section === "world"
          ? regenerateParams.chapters_per_volume
          : null,
      tension_template:
        section === "volume_plans" ? regenerateParams.tension_template : null,
    };
    try {
      const taskInfo = await regenerateMutation.mutateAsync({
        workspaceId,
        body,
      });
      // v1.0.x Commit B::regenerate-section 现返 TaskInfo,订阅 done 再 invalidate
      onTaskAccepted?.(taskInfo.task_id);
      task.start(taskInfo.task_id);
    } catch (err) {
      showApiErrorToast(err);
    }
  };

  return (
    <div className="space-y-3" data-testid={`outline-tab-${section}`}>
      <div className="flex items-center justify-between">
        <p className="text-xs text-muted-foreground">
          {isRegenerating
            ? "正在重新生成此项,其余段不变…"
            : "只读预览;不满意可点右侧按钮重新生成此项,或在底部返回修改输入。"}
        </p>
        <div className="flex items-center gap-2">
          <Button
            size="sm"
            variant="outline"
            onClick={handleRegenerate}
            disabled={disabled || isRegenerating}
            data-testid={`outline-regenerate-${section}`}
          >
            {isRegenerating ? "重新生成中…" : "重新生成此项"}
          </Button>
          {task.isRunning && (
            <CancelTaskButton
              onCancel={task.cancel}
              isCancelling={task.isCancelling}
              testId={`outline-regenerate-cancel-${section}`}
            />
          )}
        </div>
      </div>
      {section !== "mini_plan" && (
        <p className="text-xs text-muted-foreground">
          重做上游后旧材料仍保留。请核对后续内容，再重新生成章节小纲完成复核后提交。
        </p>
      )}
      {section === "mini_plan" ? (
        <MiniPlanTabContent
          miniPlan={outline.mini_plan ?? null}
          editing={false}
          onChange={() => undefined}
        />
      ) : (
        <OutlineSectionView outline={outline} section={section} />
      )}
    </div>
  );
}
