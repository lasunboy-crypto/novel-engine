/**
 * 异步任务状态 wrapper hook(v1.1 w4-s7 = J2.1)。
 *
 * ## 抽出动机(Rule of Two 早已触发 — 6 处 call site)
 *
 * v1.0.x Commit B 起,长跑端点(:write / :rewrite / :revise / outline:generate /
 * outline:regenerate-section / 章节卡级联重生 / export)全部改 BackgroundTasks
 * 异步:mutation onSuccess 立即返回(task pending),真正"跑完"信号来自 WS
 * `task.{taskId}.progress` 频道的 `done` / `failed` / `cancelled` 事件。
 *
 * 这导致 6 处 caller(ChapterActionsToolbar / OutlineTabContent /
 * OutlineWorkflowPanel / ChapterCardReviewPage write+cascade / ExportPage)
 * 各自重复同一套样板:① 本地 `taskId` state ② `useTaskProgress` 订阅 ③ 终态
 * 事件检测后 reset taskId ④ s2.6c timeout fallback ⑤ 错误 / 超时 toast。本 hook
 * 把这套样板收敛成单点,caller 只声明 task 类型 + 业务回调。
 *
 * ## J1(w4-s6)协同 — cancelled 事件 + 取消按钮
 *
 * J1 给后端任务接了"尽力中断"取消机制 + `cancelled` WS 终态事件
 * (`src/api/routes/chapters.py` `_publish_task_event(id, "cancelled", ...)`)。
 * **迁移前 6 处 caller 的 onMessage 全部只判 done/failed、漏判 cancelled** →
 * 用户取消后后端发 cancelled,旧 caller 收不到、taskId 不 reset → 按钮卡死
 * (ExportPage 无 timeout 会永久卡)。本 hook 把 `cancelled` 纳入终态分发
 * 顺带根治这个 J1 引入的潜在 bug。`cancel()` 调 `useCancelTaskMutation`
 * (J1 已交付的 API client hook),取消按钮 UI 见 `CancelTaskButton`。
 *
 * ## 设计要点(自决项,impl 文档登记)
 *
 * - **复用 `useTaskProgress` 而非重造**:WS 订阅 + B-06 callback-ref 防抖 +
 *   s2.6c timeout fallback 都久经实测,本 hook 作为更高层 wrapper 委托它做
 *   订阅,只额外接管 taskId state + 终态分发 + cancel。
 * - **终态三态统一**:`done` → onSuccess;`failed` / `error` → onFailed
 *   (不传则默认 showApiErrorToast);`cancelled` → onCancelled。三者都自动
 *   reset taskId,caller 不必自己 reset。非终态帧(`stage` / `batch_partial`
 *   等)透传 onMessage(OutlineWorkflowPanel / ExportPage 用它追踪 stage)。
 * - **泛型 `TMeta`**:`start(taskId, meta?)` 可携带业务元数据(典型
 *   ChapterActionsToolbar 用 `kind` 区分按钮文案),与 taskId 同生同灭,避免
 *   caller 再开一份与 taskId 易失步的并行 state。
 * - **cancel 非瞬时语义**:`cancel()` 触发 mutation 后 **不立即 reset**(任务
 *   仍在跑,等后端 `cancelled` 事件才真 reset);期间 `isCancelling=true` 让
 *   取消按钮显示"取消中…"。mutation 自身 isPending 太短(立即返回 running
 *   TaskInfo),不足以做"取消中"信号,故用本地 `cancelRequested` 跨到终态。
 */

import { useCallback, useState } from "react";

import { useCancelTaskMutation } from "@/api/tasks";
import { showApiErrorToast } from "@/components/Toast";
import { useTaskProgress } from "@/hooks/useTaskProgress";
import type { ServerPushFrame } from "@/ws/types";

/**
 * 异步任务类型 — 决定默认 timeout(见 `DEFAULT_TIMEOUT_MS`)。
 *
 * - `chapter_write`:章节 :write / :rewrite / :revise + 章节卡写作(5min)
 * - `chapter_card`:单章卡生成(5min)
 * - `outline_generate`:整本大纲生成(40min,真实 qwen3.7-max 复杂大纲可超过 20min)
 * - `outline_section`:大纲单段重生(5min)
 * - `chapter_cascade`:章节卡级联重生(10min,批量 LLM 更宽松)
 * - `export`:导出 zip(无 timeout,走 ExportPage 自有 2s 轮询 fallback)
 */
export type AsyncTaskKind =
  | "chapter_write"
  | "chapter_card"
  | "outline_generate"
  | "outline_section"
  | "chapter_cascade"
  | "export";

/**
 * 各 task 类型默认 timeout(ms)。除 outline_generate 外沿用 v1.0.x wave 6
 * s2.6c 实测值；outline 在 R8 实证中正常运行 25m21s，故提醒线升到 40min。
 * `0` = 关闭 timeout(export 走轮询)。
 */
const DEFAULT_TIMEOUT_MS: Record<AsyncTaskKind, number> = {
  chapter_write: 300_000,
  chapter_card: 300_000,
  outline_generate: 2_400_000,
  outline_section: 300_000,
  chapter_cascade: 600_000,
  export: 0,
};

/** 各 task 类型的中文标签(默认超时 toast 用)。 */
const KIND_LABEL: Record<AsyncTaskKind, string> = {
  chapter_write: "章节任务",
  chapter_card: "章节卡生成",
  outline_generate: "大纲生成",
  outline_section: "单段重生",
  chapter_cascade: "章节卡级联重生",
  export: "导出",
};

/** 从终态帧里尽量抽出错误信息(兼容 `error: {message}` 对象与 `error: string`)。 */
function extractErrorMessage<TData>(frame: ServerPushFrame<TData>): string | null {
  const err = (frame.data as { error?: unknown } | undefined)?.error;
  if (typeof err === "string") return err;
  if (err && typeof err === "object" && "message" in err) {
    const message = (err as { message?: unknown }).message;
    if (typeof message === "string") return message;
  }
  return null;
}

export interface UseAsyncTaskStateOptions<TData = unknown> {
  /** task 类型,决定默认 timeout + 默认超时 toast 文案。 */
  kind: AsyncTaskKind;
  /** `done` 终态:caller 通常在此 invalidate query + 成功 toast。hook 已自动 reset。 */
  onSuccess?: (frame: ServerPushFrame<TData>) => void;
  /**
   * `failed` / `error` 终态:caller 自定义错误处理(更新自己的 state 机等)。
   * 不传则默认 `showApiErrorToast(抽出的错误信息 ?? "<kind>失败")`。hook 已自动 reset。
   */
  onFailed?: (message: string, frame: ServerPushFrame<TData>) => void;
  /** `cancelled` 终态(J1 取消机制):caller 可选提示。hook 已自动 reset。 */
  onCancelled?: (frame: ServerPushFrame<TData>) => void;
  /** 非终态帧(`stage` / `batch_partial` 等)透传,caller 追踪进度用。 */
  onMessage?: (frame: ServerPushFrame<TData>) => void;
  /**
   * timeout fallback:不传则默认 reset + 通用超时 toast(用 kind 中文标签)。
   * 需要更新自己 state 机的 caller(OutlineWorkflowPanel / ExportPage)传自定义。
   */
  onTimeout?: () => void;
  /** 覆盖 kind 默认 timeout(ms);`0` = 关闭。不传走 `DEFAULT_TIMEOUT_MS[kind]`。 */
  customTimeoutMs?: number;
  /**
   * timeout 只表示前端等待过久时,是否继续保留 taskId 与 WS 订阅。
   * 默认 false,维持历史调用方“超时即释放 UI”的行为；长跑任务可设 true,
   * 避免把前端计时到点误报为后端失败并诱发重复请求。
   */
  keepRunningOnTimeout?: boolean;
}

export interface UseAsyncTaskStateReturn<TMeta = undefined> {
  /** 当前运行中任务 ID;null = 无任务。 */
  taskId: string | null;
  /** `start` 时携带的业务元数据;null = 无任务或未传。 */
  meta: TMeta | null;
  /** taskId !== null。caller 通常 `mutation.isPending || isRunning` 组合做按钮禁用。 */
  isRunning: boolean;
  /** 触发后端任务成功(拿到 task_id)后调,登记任务 + 可选 meta。 */
  start: (taskId: string, meta?: TMeta) => void;
  /** 手动清空任务状态(state 机 caller 在自定义回调里需要时用)。 */
  reset: () => void;
  /** 取消正在跑的任务(调 `useCancelTaskMutation`);非瞬时,等 cancelled 事件真 reset。 */
  cancel: () => void;
  /** 已发出取消请求、尚未收到终态(取消按钮显示"取消中…"用)。 */
  isCancelling: boolean;
}

/**
 * 管理单个异步任务的完整生命周期(WS 订阅 + 终态分发 + timeout + 取消)。
 *
 * @example
 * ```ts
 * const task = useAsyncTaskState({
 *   kind: "chapter_write",
 *   onSuccess: () => { invalidate(...); showSuccessToast("完成"); },
 * });
 * // 触发:
 * writeMutation.mutate(..., { onSuccess: (t) => task.start(t.task_id) });
 * // 渲染:
 * <Button disabled={writeMutation.isPending || task.isRunning}>...</Button>
 * {task.isRunning && <CancelTaskButton onCancel={task.cancel} isCancelling={task.isCancelling} />}
 * ```
 */
export function useAsyncTaskState<TData = unknown, TMeta = undefined>(
  options: UseAsyncTaskStateOptions<TData>,
): UseAsyncTaskStateReturn<TMeta> {
  const {
    kind,
    onSuccess,
    onFailed,
    onCancelled,
    onMessage,
    onTimeout,
    customTimeoutMs,
    keepRunningOnTimeout = false,
  } = options;

  const [state, setState] = useState<{ taskId: string; meta: TMeta | null } | null>(
    null,
  );
  const [cancelRequested, setCancelRequested] = useState(false);
  const cancelMutation = useCancelTaskMutation();

  const taskId = state?.taskId ?? null;
  const meta = state?.meta ?? null;
  const isRunning = state !== null;

  const finish = useCallback(() => {
    setState(null);
    setCancelRequested(false);
  }, []);

  const start = useCallback((nextTaskId: string, nextMeta?: TMeta) => {
    setState({ taskId: nextTaskId, meta: nextMeta ?? null });
    setCancelRequested(false);
  }, []);

  const cancel = useCallback(() => {
    if (!taskId) return;
    setCancelRequested(true);
    cancelMutation.mutate(taskId, {
      onError: (err) => {
        // 取消请求本身失败(典型 404 任务已终态被淘汰):清"取消中"+ 提示。
        setCancelRequested(false);
        showApiErrorToast(err);
      },
    });
  }, [taskId, cancelMutation]);

  const resolvedTimeout = customTimeoutMs ?? DEFAULT_TIMEOUT_MS[kind];

  const handleMessage = useCallback(
    (frame: ServerPushFrame<TData>) => {
      switch (frame.event) {
        case "done":
          finish();
          onSuccess?.(frame);
          return;
        case "failed":
        case "error": {
          finish();
          const message = extractErrorMessage(frame) ?? `${KIND_LABEL[kind]}失败`;
          if (onFailed) onFailed(message, frame);
          else showApiErrorToast(new Error(message));
          return;
        }
        case "cancelled":
          finish();
          onCancelled?.(frame);
          return;
        default:
          // 非终态(stage / batch_partial / running ...)透传
          onMessage?.(frame);
      }
    },
    [finish, kind, onSuccess, onFailed, onCancelled, onMessage],
  );

  const handleTimeout = useCallback(() => {
    if (!keepRunningOnTimeout) {
      finish();
    }
    if (onTimeout) {
      onTimeout();
      return;
    }
    const minutes = Math.round(resolvedTimeout / 60_000);
    showApiErrorToast(
      new Error(
        `${KIND_LABEL[kind]} ${minutes} 分钟未收到完成信号 — 请检查后台日志或刷新页面`,
      ),
    );
  }, [finish, keepRunningOnTimeout, kind, onTimeout, resolvedTimeout]);

  useTaskProgress<TData>(taskId, {
    onMessage: handleMessage,
    timeoutMs: resolvedTimeout,
    onTimeout: handleTimeout,
  });

  return { taskId, meta, isRunning, start, reset: finish, cancel, isCancelling: cancelRequested };
}
