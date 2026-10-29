/**
 * OutlineWorkflowPanel(v1.0.x wave 6 s1+s4 合并):大纲生成 + 5 tab 预览 + commit
 * 完整工作流面板,从 NewBookPage 抽出供 NewBookPage / OutlinePage 共享。
 *
 * 真根因转向(2026-05-24,wave 6 s1 plan-kickoff 误诊纠正):
 * - 原 plan-kickoff(轮 174)误诊 outline:generate 端点未接 router → wave 6 s1
 *   实施期 Code grep 验真发现两端点 router 路径代码层完全对称,真根因是
 *   NewBookPage 创建按钮立即触发 generate 的**时序问题**(用户无机会在
 *   project.yaml 就位前阻止)。
 * - X1 方案 + 合并 s4 拍板:NewBookPage 瘦身只创建 + 跳 OutlinePage,
 *   OutlinePage 三档分支(未生成 / 已生成未 commit / 已 commit)各对应不同
 *   workflow stage。两个页面共享本组件管"生成 + 4 tab + commit"完整流程。
 *
 * 使用模式
 * ========
 *
 * - **NewBookPage**:创建工作区 + 资产上传后 navigate('/w/{id}/outline',
 *   {state: {formValues, assetsResult, autoStart: true}});NewBookPage 不再
 *   直接 render 本组件(瘦身后只剩 NewBookForm)
 * - **OutlinePage(not_started 状态)**:render 本组件 autoStart=false,
 *   formValues 来自 useLocation.state(无 state → 显示引导回 NewBookPage)
 * - **OutlinePage(generating 状态)**:render 本组件 autoStart=true(若
 *   useLocation.state 含 formValues + autoStart=true);或者复用 WS 订阅
 *   等待生成完成切 ready。刷新丢失 formValues 时,ready 态只从工作区元数据
 *   与当前卷规划恢复单段重生所需参数;卷间不一致则 fail closed
 * - **OutlinePage(committed 状态)**:本组件不渲染,由 OutlinePage 既有
 *   6 tab 编辑器接管
 */

import { useCallback, useEffect, useMemo, useRef, useState } from "react";
import { useQueryClient } from "@tanstack/react-query";

import { CancelTaskButton } from "@/components/CancelTaskButton";
import { ParallelTaskStatusList } from "@/components/ParallelTaskStatusList";
import {
  OutlineErrorDetail,
  type OutlineErrorLike,
} from "@/components/OutlineErrorDetail";
import { OutlineFailedCandidatePanel } from "@/components/OutlineFailedCandidatePanel";
import {
  OutlineTabContent,
  type RegenerateParams,
  type RegenerableOutlineSection,
} from "@/components/OutlineTabContent";
import { showApiErrorToast, showSuccessToast } from "@/components/Toast";
import { Button } from "@/components/ui/button";
import { isApiError } from "@/api/errors";
import type { AssetsImportOut } from "@/api/assets";
import type { NewBookFormSubmitPayload } from "@/components/NewBookForm";
import {
  OUTLINE_QUERY_KEY,
  useCommitOutlineMutation,
  useGenerateOutlineMutation,
  useOutlineQuery,
  useOutlineStatusQuery,
  useRegenerateOutlineSectionMutation,
  type OutlineCommitResult,
  type OutlineOut,
  type OutlineRegenerateSectionIn,
  type OutlineStatus,
} from "@/api/outline";
import { TASK_QUERY_KEY, useTaskInfoQuery, type TaskInfo } from "@/api/tasks";
import { useAsyncTaskState } from "@/hooks/useAsyncTaskState";
import {
  parseParallelTaskSnapshot,
  type ParallelTaskSnapshot,
} from "@/lib/parallelTaskStatus";
import { cn } from "@/lib/utils";

export type NewBookFormValues = NewBookFormSubmitPayload["values"];

/** 进度态机:idle 等用户点'生成大纲' / generating 提交中 / ready 成功 / error 任一步失败。
 *  J2.1(w4-s7):taskId 收归 useAsyncTaskState 单源,generating 不再内嵌 taskId;
 *  ready.taskId 仅供展示,在 done 帧转移时从 frame.data.task_id 取。 */
type SubmissionState =
  | { kind: "idle" }
  | {
      kind: "generating";
      stage: GenerateStage;
      assetsResult?: AssetsImportOut | null;
    }
  | {
      kind: "ready";
      taskId: string;
      assetsResult: AssetsImportOut | null;
    }
  | {
      kind: "error";
      message: string;
      /** 只有 task registry 已明确把本次任务置为 failed/cancelled 时才允许重试。
       *  POST 网络错误属于“服务端可能已经接受”的不确定态，必须硬停。 */
      retryAllowed: boolean;
      cancelled?: boolean;
      /** v2 W3.5 UI 缺口1:后端结构化错误(contradictions / violations / stage),
          供 OutlineErrorDetail 列清楚 + 高亮实体名。可空(超时等无结构化错误)。 */
      detail?: OutlineErrorLike;
    };

type GenerateStage =
  | "world"
  | "characters"
  | "plot"
  | "volume_plans"
  | "mini_plan"
  | "done";

const STAGE_LABELS: Record<GenerateStage, string> = {
  world: "世界观生成中",
  characters: "人设生成中",
  plot: "主线生成中",
  volume_plans: "卷规划生成中",
  mini_plan: "章级事件与 Beat 对齐中",
  done: "生成完成",
};

const OUTLINE_STAGES: GenerateStage[] = [
  "world",
  "characters",
  "plot",
  "volume_plans",
  "mini_plan",
];

/**
 * v2 W3.5 UI 缺口1:从已生成大纲(失败前 characters/volume_plans 已落盘)抽实体名
 * (角色名 + 卷 ID),供一致性冲突 / 结构闸违规文字红字高亮。query 未就位 → 空数组。
 */
function deriveEntityNames(outline: OutlineOut | undefined): string[] {
  if (!outline) return [];
  const names: string[] = [];
  for (const c of outline.characters?.characters ?? []) {
    if (c.name) names.push(c.name);
  }
  for (const v of outline.volume_plans?.volumes ?? []) {
    if (v.volume_id) names.push(v.volume_id);
  }
  return names;
}

/** 五段均已落盘才可从刷新/断线后的 server checkpoint 恢复 ready。 */
function hasCompleteOutline(outline: OutlineOut | undefined): boolean {
  return Boolean(
    outline?.world &&
      outline.characters &&
      outline.plot &&
      outline.volume_plans &&
      outline.mini_plan,
  );
}

/**
 * 刷新恢复必须同时绑定确切 task、终态和同一份大纲；仅凭“五段文件存在”
 * 可能把旧 checkpoint 误认成仍在覆盖它的新任务，提前开放 commit。
 */
function taskCompletedWithCurrentOutline(
  task: TaskInfo | undefined,
  outline: OutlineOut | undefined,
): boolean {
  if (
    !task ||
    task.status !== "done" ||
    !outline ||
    !task.result ||
    typeof task.result !== "object"
  ) {
    return false;
  }
  const result = task.result as Record<string, unknown>;
  let candidate: unknown;
  if (task.type === "outline_generate") {
    if (result.task_id !== task.task_id || !("outline" in result)) return false;
    candidate = result.outline;
  } else if (task.type === "outline_regenerate_section") {
    candidate = result;
  } else {
    return false;
  }
  return JSON.stringify(candidate) === JSON.stringify(outline);
}

function taskFailureDetail(task: TaskInfo | undefined): OutlineErrorLike | undefined {
  const raw = task?.error;
  if (!raw || typeof raw !== "object") return undefined;
  const code = typeof raw.code === "string" ? raw.code : undefined;
  const message = typeof raw.message === "string" ? raw.message : undefined;
  const details =
    raw.details && typeof raw.details === "object"
      ? (raw.details as Record<string, unknown>)
      : null;
  return { code, message, details };
}

function failedOutlineStage(
  detail: OutlineErrorLike | undefined,
): RegenerableOutlineSection | null {
  const stage = detail?.details?.stage;
  return stage === "world" ||
    stage === "characters" ||
    stage === "plot" ||
    stage === "volume_plans" ||
    stage === "mini_plan"
    ? stage
    : null;
}

/**
 * 刷新后 navigate state 会丢失,但单段重生不能猜参数。只在工作区前提/题材存在、
 * 当前卷规划非空,且每卷章数与张力模板完全一致时,从产品持久化事实恢复请求参数。
 */
function derivePersistedRegenerateParams(
  workspaceInputs: PersistedWorkspaceInputs | null,
  outline: OutlineOut | undefined,
): RegenerateParams | null {
  if (
    !workspaceInputs?.premise.trim() ||
    !workspaceInputs.genre.trim() ||
    !outline?.volume_plans
  ) {
    return null;
  }

  const scope = outline.world?.story_intent?.planning_scope;
  const volumes = outline.volume_plans.volumes;
  const firstVolume = volumes[0];
  if (
    !firstVolume ||
    !Number.isSafeInteger(firstVolume.chapter_count) ||
    firstVolume.chapter_count <= 0 ||
    volumes.some(
      (volume) =>
        (!scope && volume.chapter_count !== firstVolume.chapter_count) ||
        volume.tension_template !== firstVolume.tension_template,
    )
  ) {
    return null;
  }

  return {
    premise: workspaceInputs.premise,
    genre: workspaceInputs.genre,
    total_volumes: scope?.total_volumes ?? volumes.length,
    chapters_per_volume: scope?.chapters_per_volume ?? firstVolume.chapter_count,
    tension_template: firstVolume.tension_template,
    ...(outline.world?.story_intent?.planning_scope
      ? { planning_mode: outline.world.story_intent.planning_scope.mode } : {}),
  };
}

const STATUS_LABELS: Record<OutlineStatus, string> = {
  not_started: "未启动",
  generating: "生成中",
  committed: "已提交",
};

export interface OutlineWorkflowPanelProps {
  workspaceId: string;
  /** 表单数据:NewBookPage 通过 navigate state 透传(必传以触发 outline:generate);
   *  null 时 idle 状态下显示引导"请回 NewBookPage 续接"。 */
  formValues: NewBookFormValues | null;
  /** 刷新后用于安全恢复重生参数的持久化工作区字段;不用于首次生成。 */
  persistedWorkspaceInputs?: PersistedWorkspaceInputs | null;
  /** NewBookPage 上传资产后透传;OutlinePage 通常为 null。 */
  initialAssetsResult?: AssetsImportOut | null;
  /** true(NewBookPage 创建后跳来 + 用户期望立即生成):mount 时自动触发 generate;
   *  false(OutlinePage not_started 默认):idle 状态显示"生成大纲"按钮等用户主动点。 */
  autoStart: boolean;
  /** 点"进入工作区"(commit 后)/ 用户主动放弃(error 态) → 父组件可 navigate。 */
  onViewWorkspace: (workspaceId: string) => void;
  /** 可选:用户点"返回 NewBookPage 重提" / "重置表单" → 父组件 navigate 回 NewBookPage。 */
  onAbandon?: () => void;
  /** 当前 URL 持久化的确切 outline task_id；刷新恢复只认它。 */
  recoveryTaskId?: string | null;
  /** POST 收到 202 后立刻把 task_id 持久化到正常产品 URL。 */
  onTaskAccepted?: (taskId: string) => void;
}

export interface PersistedWorkspaceInputs {
  premise: string;
  genre: string;
}

export function OutlineWorkflowPanel(props: OutlineWorkflowPanelProps) {
  const {
    workspaceId,
    formValues,
    persistedWorkspaceInputs = null,
    initialAssetsResult = null,
    autoStart,
    onViewWorkspace,
    onAbandon,
    recoveryTaskId = null,
    onTaskAccepted,
  } = props;

  const [submission, setSubmission] = useState<SubmissionState>({
    kind: "idle",
  });
  const [beatParallelSnapshot, setBeatParallelSnapshot] =
    useState<ParallelTaskSnapshot | null>(null);
  const [checkpointRecoverySection, setCheckpointRecoverySection] =
    useState<"characters" | "plot" | "volume_plans" | "mini_plan" | null>(null);

  const generateMutation = useGenerateOutlineMutation();
  const checkpointRecoveryMutation = useRegenerateOutlineSectionMutation();
  const queryClient = useQueryClient();
  const statusQuery = useOutlineStatusQuery(workspaceId);
  // v2 W3.5 UI 缺口1:已生成段(失败前 world/characters/plot 已落盘)→ 取角色名
  // 供一致性冲突文字红字高亮(best-effort;query 失败 / 段未就位 → 空数组,降级纯文字)。
  const outlineForNamesQuery = useOutlineQuery(workspaceId);
  const entityNames = deriveEntityNames(outlineForNamesQuery.data);
  const recoveryTaskQuery = useTaskInfoQuery(recoveryTaskId);
  const persistedRecoveryParams = useMemo(() => derivePersistedRegenerateParams(
    persistedWorkspaceInputs, outlineForNamesQuery.data,
  ), [persistedWorkspaceInputs, outlineForNamesQuery.data]);
  const recoveryParams = formValues ?? persistedRecoveryParams;

  // WS 订阅 task.{task_id}.progress —— J2.1(w4-s7)收敛到 useAsyncTaskState
  // (kind=outline_generate → 40min 提醒线；提醒后继续保留 taskId/WS)。
  // generating 期接 stage(onMessage)/ done(onSuccess 转 ready)/ failed
  // (onFailed 转 error)/ cancelled(onCancelled 回 idle)/ timeout(只提醒不退订)。
  const outlineTask = useAsyncTaskState<{
    stage?: string;
    progress?: number;
    parallel_group?: string;
    volume_id?: string;
    max_concurrency?: number;
    tasks?: unknown[];
    error?: OutlineErrorLike;
    task_id?: string;
  }>({
    kind: "outline_generate",
    keepRunningOnTimeout: true,
    onMessage: (frame) => {
      if (frame.event !== "stage") return;
      const stage = frame.data?.stage;
      if (
        stage === "world" ||
        stage === "characters" ||
        stage === "plot" ||
        stage === "volume_plans" ||
        stage === "mini_plan"
      ) {
        setSubmission((prev) =>
          prev.kind === "generating" ? { ...prev, stage } : prev,
        );
      }
      const snapshot = parseParallelTaskSnapshot(
        frame.data,
        "mini_plan_beat_alignment",
      );
      if (snapshot) setBeatParallelSnapshot(snapshot);
    },
    onSuccess: (frame) => {
      setSubmission((prev) => {
        if (prev.kind !== "generating") return prev;
        return {
          kind: "ready",
          taskId: frame.data?.task_id ?? "",
          assetsResult: prev.assetsResult ?? null,
        };
      });
      showSuccessToast(
        "大纲生成完成",
        `工作区 ${workspaceId} 已生成,可继续编辑或提交`,
      );
    },
    onFailed: (message, frame) => {
      // v2 W3.5 UI 缺口1:捕获结构化错误(frame.data.error 含 code/details),
      // 供 OutlineErrorDetail 列冲突 + 高亮实体名;非结构化失败 detail 为空。
      const detail = frame.data?.error;
      setSubmission((prev) =>
        prev.kind === "generating"
          ? { kind: "error", message, detail, retryAllowed: true }
          : prev,
      );
      showApiErrorToast(new Error(message));
    },
    onCancelled: () => {
      // outline_status 在提交前一直是 generating；明确取消不能退回会永久隐藏按钮的 idle。
      setSubmission((prev) => prev.kind === "generating" ? {
        kind: "error", message: "大纲生成已取消，原结果保留。", retryAllowed: true,
        cancelled: true,
      } : prev);
      showSuccessToast("已取消大纲生成", "可重新触发生成");
    },
    onTimeout: () => {
      showApiErrorToast(
        new Error(
          "大纲生成已等待 40 分钟，后台任务仍在运行；可继续等待或取消，请勿重复触发",
        ),
      );
    },
  });
  const startOutlineTask = outlineTask.start;

  const checkpointRecoveryTask = useAsyncTaskState<{
    task_id?: string;
    error?: OutlineErrorLike;
  }>({
    kind: "outline_section",
    keepRunningOnTimeout: true,
    onSuccess: (frame) => {
      void queryClient.invalidateQueries({
        queryKey: OUTLINE_QUERY_KEY(workspaceId),
      });
      if (checkpointRecoverySection === "characters") {
        setCheckpointRecoverySection("plot");
        showSuccessToast("人物已从 checkpoint 恢复", "世界未重跑；请继续生成主线");
        return;
      }
      if (checkpointRecoverySection === "plot") {
        setCheckpointRecoverySection("volume_plans");
        showSuccessToast("主线已从 checkpoint 恢复", "世界与人物未重跑；请继续生成卷规划");
        return;
      }
      if (checkpointRecoverySection === "volume_plans") {
        setCheckpointRecoverySection("mini_plan");
        showSuccessToast(
          "卷规划已从 checkpoint 恢复",
          "上游三段未重跑；请继续生成章级事件",
        );
        return;
      }
      if (checkpointRecoverySection === "mini_plan") {
        setCheckpointRecoverySection(null);
        setSubmission({
          kind: "ready",
          taskId: frame.data?.task_id ?? "",
          assetsResult: initialAssetsResult,
        });
        showSuccessToast(
          "大纲 checkpoint 恢复完成",
          "五段均已落盘，可逐段审核后确认保存",
        );
      }
    },
    onFailed: (message, frame) => {
      const detail = frame.data?.error;
      setSubmission({ kind: "error", message, detail, retryAllowed: true });
      showApiErrorToast(new Error(message));
    },
    onTimeout: () => {
      showApiErrorToast(
        new Error("单段恢复任务仍在运行；请继续等待，禁止重复触发"),
      );
    },
  });

  /** 触发 outline:generate(自动 / 手动复用同一路径)。 */
  const runGenerate = useCallback(
    async (values: NewBookFormValues, assetsResult: AssetsImportOut | null) => {
      setBeatParallelSnapshot(null);
      setSubmission({
        kind: "generating",
        stage: "world",
        assetsResult,
      });
      try {
        const taskInfo = await generateMutation.mutateAsync({
          workspaceId,
          body: {
            premise: values.premise,
            total_volumes: values.total_volumes,
            chapters_per_volume: values.chapters_per_volume,
            tension_template: values.tension_template,
            genre: values.genre,
            ...(values.planning_mode ? { planning_mode: values.planning_mode } : {}),
          },
        });
        // taskId 收归 hook;submission 维持 generating(stage 走 WS 更新)。
        onTaskAccepted?.(taskInfo.task_id);
        startOutlineTask(taskInfo.task_id);
      } catch (err) {
        const message = isApiError(err)
          ? `${err.code}: ${err.message}`
          : err instanceof Error
            ? err.message
            : "未知错误";
        setSubmission({ kind: "error", message, retryAllowed: false });
        showApiErrorToast(err);
      }
    },
    [generateMutation, onTaskAccepted, workspaceId, startOutlineTask],
  );

  // autoStart effect:mount 时若 autoStart=true + formValues 有值 → 立即触发
  // generate。用 ref guard 避 React StrictMode 双触。
  const autoStartFiredRef = useRef(false);
  useEffect(() => {
    if (!autoStart || !formValues || autoStartFiredRef.current) return;
    autoStartFiredRef.current = true;
    void runGenerate(formValues, initialAssetsResult);
  }, [autoStart, formValues, initialAssetsResult, runGenerate]);

  const outlineStatus = statusQuery.data?.outline_status as
    | OutlineStatus
    | undefined;
  const completeOutline = hasCompleteOutline(outlineForNamesQuery.data);
  const recoveryTaskMatches = taskCompletedWithCurrentOutline(
    recoveryTaskQuery.data,
    outlineForNamesQuery.data,
  );
  const recoveryTaskActive = Boolean(
    recoveryTaskId &&
    recoveryTaskQuery.data?.type === "outline_regenerate_section" &&
    (recoveryTaskQuery.data.status === "pending" ||
      recoveryTaskQuery.data.status === "running"),
  );

  // URL 中的精确 task_id 在刷新后仍可只读跟踪终态；轮询不会再次提交生成。
  useEffect(() => {
    if (!recoveryTaskId || !recoveryTaskActive) return;
    const timer = window.setInterval(() => {
      void queryClient.invalidateQueries({ queryKey: TASK_QUERY_KEY(recoveryTaskId) });
    }, 2000);
    return () => window.clearInterval(timer);
  }, [queryClient, recoveryTaskActive, recoveryTaskId]);

  useEffect(() => {
    const task = recoveryTaskQuery.data;
    const cancelled = task?.status === "cancelled" &&
      task.type === "outline_generate" && task.task_id === recoveryTaskId;
    if (task?.status !== "failed" && !cancelled) return;
    const detail = taskFailureDetail(task);
    setSubmission((prev) =>
      prev.kind === "idle"
        ? {
            kind: "error",
            message: cancelled ? "大纲生成已取消，原结果保留。" : detail?.message ?? "大纲任务已明确失败",
            detail,
            retryAllowed: true,
            cancelled,
          }
        : prev,
    );
  }, [recoveryTaskQuery.data, recoveryTaskId]);

  useEffect(() => {
    const task = recoveryTaskQuery.data;
    const outline = outlineForNamesQuery.data;
    if (
      task?.status !== "done" ||
      task.type !== "outline_regenerate_section" ||
      !outline?.world ||
      !outline.characters ||
      outline.mini_plan ||
      !formValues
    ) {
      return;
    }
    const nextSection = !outline.plot ? "plot" : outline.volume_plans ? "mini_plan" : "volume_plans";
    if (nextSection !== "mini_plan" && (task.task_id !== recoveryTaskId ||
        !taskCompletedWithCurrentOutline(task, outline))) return;
    setCheckpointRecoverySection(nextSection);
    setSubmission((prev) =>
      prev.kind === "idle"
        ? {
            kind: "error",
            message: nextSection === "mini_plan" ? "卷规划 checkpoint 已完成，章级事件尚未生成"
              : nextSection === "plot" ? "人物 checkpoint 已完成，主线尚未生成"
              : "主线 checkpoint 已完成，卷规划尚未生成",
            detail: {
              code: "OUTLINE_BUILD_FAILED",
              message: "需从既有 checkpoint 继续下游生成",
              details: { stage: nextSection },
            },
            retryAllowed: true,
          }
        : prev,
    );
  }, [formValues, outlineForNamesQuery.data, recoveryTaskQuery.data, recoveryTaskId]);

  // 刷新、WS 断线或旧前端假超时后，后端可能已完成五段但仍按合同保持
  // outline_status=generating（直到 commit 才翻 committed）。从已审核的四段
  // checkpoint 单独生成小纲时，工作区也可能保持 not_started。只在 URL task_id、
  // task registry done 和当前五段内容三方一致时恢复 ready；不靠文件齐全猜测。
  useEffect(() => {
    if (
      (outlineStatus !== "generating" && outlineStatus !== "not_started") ||
      !completeOutline ||
      !recoveryTaskId ||
      !recoveryTaskMatches
    ) {
      return;
    }
    setSubmission((prev) =>
      prev.kind === "idle"
        ? {
            kind: "ready",
            taskId: recoveryTaskId,
            assetsResult: initialAssetsResult,
          }
        : prev,
    );
  }, [
    completeOutline,
    initialAssetsResult,
    outlineStatus,
    recoveryTaskId,
    recoveryTaskMatches,
  ]);

  const startCheckpointRecovery = useCallback(
    async (section: "characters" | "plot" | "volume_plans" | "mini_plan", usePersistedInputs = false) => {
      const params = usePersistedInputs ? persistedRecoveryParams : recoveryParams;
      if (!params) return;
      setCheckpointRecoverySection(section);
      const body: OutlineRegenerateSectionIn = {
        section,
        premise: params.premise,
        keep_others: true,
        genre: params.genre,
        ...(params.planning_mode ? { planning_mode: params.planning_mode } : {}),
        total_volumes:
          section === "volume_plans" ? params.total_volumes : null,
        chapters_per_volume:
          section === "volume_plans" ? params.chapters_per_volume : null,
        tension_template:
          section === "volume_plans" ? params.tension_template : null,
      };
      try {
        const taskInfo = await checkpointRecoveryMutation.mutateAsync({
          workspaceId,
          body,
        });
        onTaskAccepted?.(taskInfo.task_id);
        checkpointRecoveryTask.start(taskInfo.task_id);
      } catch (err) {
        const message = isApiError(err)
          ? `${err.code}: ${err.message}`
          : err instanceof Error
            ? err.message
            : "未知错误";
        setCheckpointRecoverySection(null);
        setSubmission({ kind: "error", message, retryAllowed: false });
        showApiErrorToast(err);
      }
    },
    [
      checkpointRecoveryMutation,
      checkpointRecoveryTask,
      persistedRecoveryParams,
      recoveryParams,
      onTaskAccepted,
      workspaceId,
    ],
  );

  // ---- 渲染分支 ----

  if (submission.kind === "idle") {
    if (checkpointRecoveryTask.isRunning || recoveryTaskActive) {
      return (
        <div className="rounded border border-border p-4 text-sm" data-testid="owp-staged-mini-plan-running">
          大纲单段任务正在生成，已绑定当前任务；请等待结果，不要重复触发。
        </div>
      );
    }
    const outline = outlineForNamesQuery.data;
    const canResumeMiniPlan =
      outlineStatus === "not_started" &&
      !recoveryTaskId &&
      Boolean(persistedRecoveryParams && outline?.world && outline.characters &&
        outline.plot && outline.volume_plans && !outline.mini_plan);
    return (
      <IdlePanel
        workspaceId={workspaceId}
        formValues={formValues}
        onGenerate={
          formValues && outlineStatus === "not_started" && !canResumeMiniPlan
            ? () => void runGenerate(formValues, initialAssetsResult)
            : undefined
        }
        onResumeMiniPlan={
          canResumeMiniPlan
            ? () => void startCheckpointRecovery("mini_plan", true)
            : undefined
        }
        outlineStatus={outlineStatus}
        onAbandon={onAbandon}
      />
    );
  }

  if (submission.kind === "error") {
    const outline = outlineForNamesQuery.data;
    const failedStage = failedOutlineStage(submission.detail);
    const rawCandidateId = submission.detail?.details?.candidate_id;
    const failedCandidateId = typeof rawCandidateId === "string"
      && /^candidate_[0-9a-f]{64}$/.test(rawCandidateId) ? rawCandidateId : null;
    // Older local JSON failures escaped without stage details. The exact
    // parse error plus a failed task and these three complete upstream objects
    // can use the existing volume recovery; never infer from arbitrary errors.
    const legacyLocalJsonFailure =
      submission.detail?.code === "INTERNAL_ERROR" &&
      submission.detail.message === "Expecting value: line 1 column 1 (char 0)";
    const legacyThreeChapterBound = submission.detail?.code === "INTERNAL_ERROR" &&
      submission.detail.message?.startsWith("已渲染 Prompt 超过活动阶段可执行上界") &&
      submission.detail.message.includes("agent=outline_builder,") &&
      submission.detail.message.includes("evidence=s2-complete-one-volume-three-chapter-outline-assembler");
    // 终审失败仍须通过原服务重审；完整四段仅允许省去重生，不能据此放行。
    const consistencyRecheck = submission.detail?.code === "OUTLINE_CONSISTENCY_CONFLICT" &&
      failedStage === "volume_plans";
    const safeCheckpointSection = failedCandidateId ? null :
      checkpointRecoverySection === "mini_plan" ||
      ((failedStage === "mini_plan" || legacyThreeChapterBound || consistencyRecheck) && recoveryParams && outline?.world &&
        outline.characters && outline.plot && outline.volume_plans && !outline.mini_plan)
        ? "mini_plan"
        : failedStage === "characters" && recoveryParams && outline?.world &&
            !outline.characters && !outline.plot && !outline.volume_plans && !outline.mini_plan
          ? "characters"
          : (failedStage === "plot" || checkpointRecoverySection === "plot") && recoveryParams && outline?.world && outline.characters &&
            !outline.plot && !outline.volume_plans && !outline.mini_plan
          ? "plot"
          : (failedStage === "volume_plans" || checkpointRecoverySection === "volume_plans" || legacyLocalJsonFailure) &&
            formValues &&
            outline?.world &&
            outline.characters &&
            outline.plot &&
            !outline.volume_plans &&
            !outline.mini_plan
          ? "volume_plans"
          : null;
    const checkpointRecoveryRunning =
      checkpointRecoveryMutation.isPending || checkpointRecoveryTask.isRunning;
    return (
      <div className="space-y-3">
          {submission.detail?.code && !submission.cancelled ? (
          <OutlineErrorDetail
            error={submission.detail}
            names={entityNames}
          />
        ) : (
          <div className="rounded border border-destructive bg-destructive/10 p-3 text-sm text-destructive">
            <p className="font-medium">{submission.cancelled ? "生成已取消" : "生成失败"}</p>
            <p className="mt-1 break-all">{submission.message}</p>
          </div>
        )}
        {failedCandidateId && <OutlineFailedCandidatePanel workspaceId={workspaceId} candidateId={failedCandidateId}
          onContinueMiniPlan={persistedRecoveryParams ? () => void startCheckpointRecovery("mini_plan", true) : undefined}
          continueDisabled={checkpointRecoveryRunning} />}
        <div className="flex flex-wrap gap-2">
          {safeCheckpointSection && submission.retryAllowed ? (
            <div
              className="space-y-2 rounded border border-amber-300 bg-amber-50 p-3 text-sm"
              data-testid="owp-safe-checkpoint-recovery"
            >
              <p className="text-amber-900">
                {consistencyRecheck ? "先重新审核现有大纲，通过后再生成" : "已确认上游 checkpoint 完整；本恢复只生成"}
                {safeCheckpointSection === "characters" ? "人物" : safeCheckpointSection === "plot" ? "主线" : safeCheckpointSection === "volume_plans"
                  ? "卷规划"
                  : "章级事件"}
                ，保留已完成的上游段。
              </p>
              <Button
                variant="default"
                size="sm"
                onClick={() => void startCheckpointRecovery(safeCheckpointSection)}
                disabled={checkpointRecoveryRunning}
                data-testid="owp-resume-checkpoint"
              >
                {checkpointRecoveryRunning
                  ? "恢复中…"
                  : safeCheckpointSection === "characters" ? "从 checkpoint 重生人物" : safeCheckpointSection === "plot" ? "从 checkpoint 重生主线" : safeCheckpointSection === "volume_plans"
                    ? "从 checkpoint 重生卷规划"
                    : consistencyRecheck ? "重新审核并继续章级事件" : "继续生成章级事件"}
              </Button>
            </div>
          ) : null}
          {formValues && submission.retryAllowed && !safeCheckpointSection && !failedCandidateId && (
            <Button
              variant="default"
              size="sm"
              onClick={() => void runGenerate(formValues, initialAssetsResult)}
              data-testid="owp-retry-generate"
            >
              重新生成大纲
            </Button>
          )}
          {!submission.retryAllowed && (
            <p
              className="max-w-2xl text-sm text-muted-foreground"
              data-testid="owp-ambiguous-request-stop"
            >
              本次提交是否已被服务端接受尚不明确。为防止重复 Worker 和重复付费，
              已停止自动重试；请先核对原任务状态。
            </p>
          )}
          {onAbandon && (
            <Button
              variant="outline"
              size="sm"
              onClick={onAbandon}
              data-testid="owp-abandon"
            >
              返回 NewBookPage
            </Button>
          )}
        </div>
      </div>
    );
  }

  if (submission.kind === "ready") {
    return (
      <ReadyPanel
        workspaceId={workspaceId}
        taskId={submission.taskId}
        assetsResult={submission.assetsResult}
        formValues={formValues}
        persistedWorkspaceInputs={persistedWorkspaceInputs}
        outlineStatus={outlineStatus}
        onTaskAccepted={onTaskAccepted}
        onAbandon={onAbandon}
        onViewWorkspace={onViewWorkspace}
      />
    );
  }

  // generating
  const currentStage = submission.stage;
  return (
    <div className="space-y-3">
      <div className="flex items-center justify-between gap-2">
        <StatusBadge
          kind="generating"
          outlineStatus={outlineStatus}
          workspaceId={workspaceId}
        />
        {outlineTask.isRunning && (
          <CancelTaskButton
            onCancel={outlineTask.cancel}
            isCancelling={outlineTask.isCancelling}
            testId="owp-generate-cancel"
          />
        )}
      </div>
      <ol className="space-y-1.5 text-sm" data-testid="owp-stage-list">
        {OUTLINE_STAGES.map((stage) => (
          <StageRow
            key={stage}
            label={STAGE_LABELS[stage]}
            state={stageStateFor(stage, currentStage)}
          />
        ))}
      </ol>
      {currentStage === "mini_plan" && beatParallelSnapshot && (
        <ParallelTaskStatusList
          title="本卷 Beat 对齐"
          snapshot={beatParallelSnapshot}
          testId="owp-beat-parallel-status"
        />
      )}
    </div>
  );
}

// ---- idle 态:autoStart=false + 用户主动点'生成大纲' ----

interface IdlePanelProps {
  workspaceId: string;
  formValues: NewBookFormValues | null;
  onGenerate?: () => void;
  onResumeMiniPlan?: () => void;
  onAbandon?: () => void;
  outlineStatus: OutlineStatus | undefined;
}

function IdlePanel(props: IdlePanelProps) {
  const { workspaceId, formValues, onGenerate, onResumeMiniPlan, onAbandon, outlineStatus } = props;
  return (
    <div
      className="space-y-3 rounded border border-dashed border-border p-4"
      data-testid="owp-idle-panel"
    >
      <div className="space-y-2 text-sm">
        <p className="font-medium">
          {outlineStatus === "generating"
            ? "正在核对大纲任务"
            : onResumeMiniPlan
              ? "四段大纲已就位，小纲待生成"
              : "大纲尚未生成"}
        </p>
        <p className="text-muted-foreground">
          工作区 <code>{workspaceId}</code>{" "}
          {outlineStatus === "generating"
            ? "后台仍标记为生成中；本页会核对已落盘 checkpoint，且不会重复触发 Provider。"
            : onResumeMiniPlan
              ? "将从现有四段继续，只生成章级事件；服务端会先执行现有上游关卡。"
              : formValues
                ? "已创建,大纲未生成。下方点'生成大纲'按钮触发 5 段串行生成。"
                : "已创建,大纲未生成。请回 NewBookPage 重新进入大纲流程(本页缺少表单参数)。"}
        </p>
        <details className="rounded border border-border bg-muted/30 p-2 text-xs">
          <summary className="cursor-pointer font-medium">
            切换 LLM provider 到云端?(可选)
          </summary>
          <div className="mt-2 space-y-1 text-muted-foreground">
            <p>
              生成前可在 <code>workspaces/{workspaceId}/project.yaml</code>
              写入 routing 覆盖,把 outline_builder 等切到云端 profile
              (如 cloud_qwen_max_thinking)。
            </p>
            <p>
              模板见 <code>docs/impl/v1_0_hotfix_post_wave5_h1_routing_completion.md</code>
              §6 步骤 3.2。yaml 写完后回本页点'生成大纲'按钮即可。
            </p>
          </div>
        </details>
      </div>
      <div className="flex flex-wrap gap-2">
        {onResumeMiniPlan && (
          <Button
            variant="default"
            size="sm"
            onClick={onResumeMiniPlan}
            data-testid="owp-resume-staged-mini-plan"
          >
            继续生成章级事件
          </Button>
        )}
        {onGenerate && (
          <Button
            variant="default"
            size="sm"
            onClick={onGenerate}
            data-testid="owp-generate-trigger"
          >
            生成大纲
          </Button>
        )}
        {onAbandon && (
          <Button
            variant="outline"
            size="sm"
            onClick={onAbandon}
            data-testid="owp-abandon"
          >
            返回 NewBookPage
          </Button>
        )}
      </div>
    </div>
  );
}

// ---- ready 态:5 tab 容器 + 重新生成 + 确认提交 ----

const TAB_LABELS: Record<RegenerableOutlineSection, string> = {
  world: "世界观",
  characters: "核心人设",
  plot: "主线",
  volume_plans: "卷规划",
  mini_plan: "章级事件",
};
const TAB_ORDER: RegenerableOutlineSection[] = [
  "world",
  "characters",
  "plot",
  "volume_plans",
  "mini_plan",
];

interface ReadyPanelProps {
  workspaceId: string;
  taskId: string;
  assetsResult: AssetsImportOut | null;
  formValues: NewBookFormValues | null;
  persistedWorkspaceInputs: PersistedWorkspaceInputs | null;
  outlineStatus: OutlineStatus | undefined;
  onTaskAccepted?: (taskId: string) => void;
  onAbandon?: () => void;
  onViewWorkspace: (workspaceId: string) => void;
}

function ReadyPanel(props: ReadyPanelProps) {
  const {
    workspaceId,
    taskId,
    assetsResult,
    formValues,
    persistedWorkspaceInputs,
    outlineStatus,
    onTaskAccepted,
    onAbandon,
    onViewWorkspace,
  } = props;

  const [activeTab, setActiveTab] =
    useState<RegenerableOutlineSection>("world");
  const outlineQuery = useOutlineQuery(workspaceId);
  const commitMutation = useCommitOutlineMutation();
  const queryClient = useQueryClient();

  // v2 W3.5 UI 缺口2:确认期结构闸违规(arc 太短 / 卷界重演)= 同步 422 ApiError。
  // 捕获结构化错误供 OutlineErrorDetail 列清楚(原被 showApiErrorToast 吞 details)。
  const [commitError, setCommitError] = useState<OutlineErrorLike | null>(null);
  const entityNames = useMemo(
    () => deriveEntityNames(outlineQuery.data),
    [outlineQuery.data],
  );

  // 重做卷规划(缺口2):arc 太短兜底 = 调大每卷章数(参数变更,非手改内容)。
  const regenerateSectionMutation = useRegenerateOutlineSectionMutation();
  const redoTask = useAsyncTaskState({
    kind: "outline_section",
    onSuccess: () => {
      void queryClient.invalidateQueries({
        queryKey: OUTLINE_QUERY_KEY(workspaceId),
      });
      setCommitError(null);
      showSuccessToast("已重做卷规划", "请重新点「确认并保存」再次过结构闸");
    },
    onFailed: (message) => showApiErrorToast(new Error(message)),
  });

  const regenerateParams: RegenerateParams | null = useMemo(() => {
    if (formValues) {
      return {
        premise: formValues.premise,
        genre: formValues.genre,
        ...(formValues.planning_mode ? { planning_mode: formValues.planning_mode } : {}),
        total_volumes: formValues.total_volumes,
        chapters_per_volume: formValues.chapters_per_volume,
        tension_template: formValues.tension_template,
      };
    }
    return derivePersistedRegenerateParams(
      persistedWorkspaceInputs,
      outlineQuery.data,
    );
  }, [formValues, outlineQuery.data, persistedWorkspaceInputs]);

  // arc 太短重做:默认把每卷章数 +1(避免同参数死循环);min = 当前 +1。
  const baseChapters = regenerateParams?.chapters_per_volume ?? 0;
  const [redoChapters, setRedoChapters] = useState<number>(baseChapters + 1);
  useEffect(() => {
    setRedoChapters(baseChapters + 1);
  }, [baseChapters]);

  const handleCommit = async () => {
    setCommitError(null);
    try {
      const result: OutlineCommitResult =
        await commitMutation.mutateAsync(workspaceId);
      showSuccessToast(
        "大纲已提交",
        `${result.volumes_count} 卷已落库,首卡正在异步生成`,
      );
    } catch (err) {
      // 结构闸违规(OUTLINE_ARC_TOO_SHORT / OUTLINE_VOLUME_SEAM_REPLAY 等)→
      // 捕获结构化错误列清楚;非 ApiError 走 toast 兜底。
      if (isApiError(err)) {
        setCommitError({
          code: err.code,
          message: err.message,
          details: err.details,
        });
      } else {
        showApiErrorToast(err);
      }
    }
  };

  // 重做卷规划:必须有原表单或严格从持久化事实恢复的 regenerateParams。
  // chaptersOverride 给定 = arc 太短调大章数;否则沿用当前安全参数。
  const handleRedoVolumePlans = async (chaptersOverride?: number) => {
    if (!regenerateParams) return;
    const body: OutlineRegenerateSectionIn = {
      section: "volume_plans",
      premise: regenerateParams.premise,
      keep_others: true,
      genre: regenerateParams.genre,
      ...(regenerateParams.planning_mode ? { planning_mode: regenerateParams.planning_mode } : {}),
      total_volumes: regenerateParams.total_volumes,
      chapters_per_volume:
        chaptersOverride ?? regenerateParams.chapters_per_volume,
      tension_template: regenerateParams.tension_template,
    };
    try {
      const taskInfo = await regenerateSectionMutation.mutateAsync({
        workspaceId,
        body,
      });
      onTaskAccepted?.(taskInfo.task_id);
      redoTask.start(taskInfo.task_id);
    } catch (err) {
      showApiErrorToast(err);
    }
  };

  const committed = commitMutation.isSuccess && commitMutation.data;
  const isRedoing =
    regenerateSectionMutation.isPending || redoTask.isRunning;

  return (
    <div className="space-y-3">
      <StatusBadge
        kind="ready"
        outlineStatus={outlineStatus}
        workspaceId={workspaceId}
      />
      {assetsResult && <AssetsCountSummary result={assetsResult} />}

      <div
        role="tablist"
        className="flex gap-1 border-b border-border"
        data-testid="owp-outline-tabs"
      >
        {TAB_ORDER.map((section) => (
          <button
            key={section}
            role="tab"
            aria-selected={activeTab === section}
            onClick={() => setActiveTab(section)}
            data-testid={`owp-tab-${section}`}
            className={cn(
              "px-3 py-1.5 text-sm transition-colors -mb-px border-b-2",
              activeTab === section
                ? "border-sky-500 text-sky-700 font-medium"
                : "border-transparent text-muted-foreground hover:text-foreground",
            )}
          >
            {TAB_LABELS[section]}
          </button>
        ))}
      </div>

      <p
        className="text-xs text-amber-700"
        data-testid="owp-regeneration-order-hint"
      >
        上游单段重生不会自动改写下游；若修改主线，请依次重生“卷规划 →
        章级事件”后再确认保存。
      </p>

      {outlineQuery.isLoading && (
        <p className="text-sm text-muted-foreground">加载 outline 5 段…</p>
      )}
      {outlineQuery.isError && (
        <p className="text-sm text-destructive">
          读取 outline 失败:{(outlineQuery.error as Error).message}
        </p>
      )}
      {outlineQuery.data && regenerateParams && (
        <OutlineTabContent
          workspaceId={workspaceId}
          outline={outlineQuery.data}
          section={activeTab}
          regenerateParams={regenerateParams}
          onTaskAccepted={onTaskAccepted}
          disabled={Boolean(committed) || commitMutation.isPending}
        />
      )}
      {outlineQuery.data && !regenerateParams && (
        <p
          className="text-xs text-muted-foreground"
          data-testid="owp-regeneration-params-missing"
        >
          工作区或卷规划中的持久化重生参数缺失或不一致,单段“重新生成”已安全禁用。
          本页仍可只读预览和提交;请先核对工作区前提/题材及各卷章数/张力模板,
          避免用猜测参数发出付费请求。
        </p>
      )}

      {committed ? (
        <div className="space-y-2 border-t border-border pt-3">
          <div className="rounded bg-emerald-50 p-2 text-sm text-emerald-700">
            <p className="font-medium">✓ 已提交</p>
            <p className="mt-0.5 text-xs">
              {commitMutation.data?.volumes_count} 卷已落库;
              {commitMutation.data?.first_card_task_id && (
                <>
                  首卡任务 id:
                  <code className="ml-1 break-all text-xs">
                    {commitMutation.data.first_card_task_id}
                  </code>
                </>
              )}
            </p>
          </div>
          <Button
            size="sm"
            onClick={() => onViewWorkspace(workspaceId)}
            data-testid="owp-view-workspace"
          >
            进入工作区
          </Button>
        </div>
      ) : (
        <div className="space-y-3 border-t border-border pt-3">
          {/* v2 W3.5 UI 缺口2:确认期结构闸违规列清楚 + 可调章数重做。 */}
          {commitError ? (
            <div className="space-y-2">
              <OutlineErrorDetail error={commitError} names={entityNames} />
              {regenerateParams &&
              commitError.code === "OUTLINE_ARC_TOO_SHORT" ? (
                <div
                  data-testid="owp-arc-too-short-redo"
                  className="flex flex-wrap items-center gap-2 rounded border border-border bg-card p-2 text-xs"
                >
                  <span className="text-muted-foreground">
                    副本弧需 ≥ 3 章;调大每卷章数后重做卷规划(当前
                    {baseChapters}):
                  </span>
                  <input
                    type="number"
                    min={baseChapters + 1}
                    value={redoChapters}
                    onChange={(e) =>
                      setRedoChapters(Number(e.target.value) || baseChapters + 1)
                    }
                    disabled={isRedoing}
                    data-testid="owp-redo-chapters-input"
                    className="w-20 rounded border border-border bg-background px-2 py-1"
                  />
                  <Button
                    size="sm"
                    onClick={() => void handleRedoVolumePlans(redoChapters)}
                    disabled={isRedoing || redoChapters <= baseChapters}
                    data-testid="owp-redo-arc"
                  >
                    {isRedoing ? "重做中…" : "调大章数并重做卷规划"}
                  </Button>
                </div>
              ) : null}
              {regenerateParams &&
              commitError.code === "OUTLINE_VOLUME_SEAM_REPLAY" ? (
                <Button
                  size="sm"
                  variant="outline"
                  onClick={() => void handleRedoVolumePlans()}
                  disabled={isRedoing}
                  data-testid="owp-redo-seam"
                >
                  {isRedoing ? "重做中…" : "重新生成卷规划"}
                </Button>
              ) : null}
            </div>
          ) : null}
          <div className="flex flex-wrap gap-2">
          <Button
            size="sm"
            onClick={handleCommit}
            disabled={commitMutation.isPending || outlineQuery.isLoading}
            data-testid="owp-commit-outline"
          >
            {commitMutation.isPending ? "提交中…" : "确认并保存"}
          </Button>
          {onAbandon && (
            <Button
              size="sm"
              variant="outline"
              onClick={onAbandon}
              disabled={commitMutation.isPending}
              data-testid="owp-return-to-input"
            >
              返回 NewBookPage
            </Button>
          )}
          <p className="ml-auto text-xs text-muted-foreground self-center">
            task_id:
            <code className="ml-1 break-all">{taskId}</code>
          </p>
          </div>
        </div>
      )}
    </div>
  );
}

// ---- 辅助 ----

interface StatusBadgeProps {
  kind: "generating" | "ready";
  outlineStatus: OutlineStatus | undefined;
  workspaceId: string;
}

function StatusBadge(props: StatusBadgeProps) {
  const { kind, outlineStatus, workspaceId } = props;
  const headline = kind === "ready" ? "已生成 4 段大纲" : "生成中…";
  return (
    <div
      data-testid="owp-status-badge"
      className={cn(
        "rounded p-2 text-sm",
        kind === "ready"
          ? "bg-emerald-50 text-emerald-700"
          : "bg-sky-50 text-sky-700",
      )}
    >
      <p className="font-medium">{headline}</p>
      <p className="mt-0.5 text-xs">
        工作区:<code>{workspaceId}</code>
        {outlineStatus && (
          <span className="ml-2">
            outline 状态:
            <code data-testid="owp-outline-status">
              {STATUS_LABELS[outlineStatus]}
            </code>
          </span>
        )}
      </p>
    </div>
  );
}

interface StageRowProps {
  label: string;
  state: "pending" | "active" | "done";
}

function StageRow(props: StageRowProps) {
  const { label, state } = props;
  const icon = state === "done" ? "✓" : state === "active" ? "●" : "○";
  const cls =
    state === "done"
      ? "text-emerald-600"
      : state === "active"
        ? "text-sky-600 font-medium"
        : "text-muted-foreground";
  return (
    <li className={cn("flex items-center gap-2", cls)}>
      <span aria-hidden="true">{icon}</span>
      <span>{label}</span>
    </li>
  );
}

function stageStateFor(
  stage: GenerateStage,
  current: GenerateStage,
): "pending" | "active" | "done" {
  const order: GenerateStage[] = [
    "world",
    "characters",
    "plot",
    "volume_plans",
    "done",
  ];
  const stageIdx = order.indexOf(stage);
  const currentIdx = order.indexOf(current);
  if (currentIdx === order.length - 1) return "done";
  if (stageIdx < currentIdx) return "done";
  if (stageIdx === currentIdx) return "active";
  return "pending";
}

function AssetsCountSummary(props: { result: AssetsImportOut }) {
  const counts = props.result.counts ?? {};
  const characterCount = counts["character"] ?? 0;
  const factCount = counts["fact"] ?? 0;
  return (
    <div
      data-testid="owp-assets-summary"
      className="rounded bg-secondary/50 p-2 text-xs"
    >
      <p className="font-medium">资产导入</p>
      <p className="mt-0.5">
        角色 {characterCount} 个 · 世界设定 {factCount} 条
      </p>
    </div>
  );
}
