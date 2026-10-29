/**
 * 异步任务进度订阅 hook(P5-F-06 抽出 — P5-F-03 §6 marker 第 2 处场景触发)。
 *
 * 订阅 `task.{taskId}.progress` WS 频道(对齐后端 `task_progress_channel`,
 * P5-B-04 OutlineService / P5-B-06 ChapterCardGenerator 等异步任务推送)。
 * 通用 `stage` / `done` / `error` / `batch_partial` 事件透传给回调,调用方按
 * `frame.event` 字符串分流。
 *
 * ## 抽出动机(P5-F-03 §6 第 2 处用户场景再抽)
 *
 * P5-F-02 NewBookPage `subscribeTaskProgress` 内联实现订阅;P5-F-06
 * ChapterCardReviewPage `:write` 触发后也需订阅 task progress 看新章节生成
 * 进度。两处场景共享:① taskId 变化时切订阅 ② unmount 自动取消 ③ event
 * 类型字符串透传。"don't design for hypothetical future requirements" — P5-F-03
 * 当时只 1 处不抽,P5-F-06 触发第 2 处场景后按 Rule of Two 抽。
 *
 * ## API 选择(自决项)
 *
 * - 回调式而非 reducer 式:与现有 `useChapterEvents` 同模式;调用方拿 frame
 *   后自管 state(setState / Zustand / TanStack Query invalidate 都行)。
 * - `taskId = null / undefined / 空字符串` → 不订阅(组件挂载早于 task_id
 *   ready 的场景安全)。
 * - 不维护 hook 内部 state:避免双源(WS event vs REST poll)与 React state
 *   生命周期错位;NewBookPage / ChapterCardReviewPage 各自管 submission state。
 */

import { useEffect, useRef } from "react";

import { getWsClient } from "@/ws/client";
import type { ServerPushFrame } from "@/ws/types";

/** 任务进度频道名:`task.{taskId}.progress`(对齐后端字面)。 */
function taskProgressChannel(taskId: string): string {
  return `task.${taskId}.progress`;
}

export interface UseTaskProgressOptions<TData = unknown> {
  /**
   * 收到任意事件帧时调用。listener 自行判断 `frame.event` 字符串
   * (常见:`stage` / `done` / `error` / `batch_partial` 等)。
   */
  onMessage?: (frame: ServerPushFrame<TData>) => void;
  /**
   * v1.0.x wave 6 s2.6c hotfix:超时 fallback 防御。
   *
   * 设为正整数后,订阅启动 ``timeoutMs`` 毫秒内未收到 ``done`` / ``failed``
   * 终态事件就触发 ``onTimeout`` 回调 — caller 通常用它 reset 自己的 taskId
   * state(让 button isLoading 信号复位)+ toast 提示用户检查后台状态。
   *
   * **设计动机**:wave 6 s2.6c 维护人 luo-cloud-test 实测发现一族"异步流水
   * 按钮"(``isLoading = mutation.isPending || taskId !== null``)因 WS
   * ``done`` event 丢失(后端没 publish / WS 断线 + reconnect 之间事件丢失 /
   * event name 不匹配等环境性问题)→ button 永远卡 "触发中…" / "重新生成中…"。
   * 代码层 audit 找不到 mutation invalidate 缺失 bug,真根因藏在 BackgroundTask
   * + WS 架构的"无 fallback 机制"层 — 本 option 是治标防御:即使 WS done 丢,
   * 也保证 UI 不卡死(taskId 60s 内必被 caller reset)。
   *
   * **不传 / undefined / 0** → 不启用 timeout(保留 wave 6 s2.6c 之前行为)。
   *
   * **caller 自管 done detection**:onMessage 收到 ``done`` / ``failed`` 时
   * caller 应同时调 onTimeout 的反面动作(reset taskId)+ 本 hook 内部不监听
   * frame.event(故意保持 hook 的"事件透传"语义不变)。
   *
   * @example
   * ```ts
   * useTaskProgress(taskId, {
   *   onMessage: (frame) => {
   *     if (frame.event === "done") { setTaskId(null); showToast("完成"); }
   *   },
   *   timeoutMs: 60_000,
   *   onTimeout: () => {
   *     setTaskId(null);
   *     showToast("任务超时,请检查后台状态");
   *   },
   * });
   * ```
   */
  timeoutMs?: number;
  /**
   * timeout 触发时调用(只调一次,触发后内部 timer 清空)。详 ``timeoutMs``。
   *
   * caller 通常在这里 reset 自己的 taskId state(让 button isLoading 复位)+
   * 显示 toast 告知用户任务可能已超时;后端实际状态以日志 / GET /tasks/{id}
   * 端点为准(本回调只防 UI 永久卡死,不试图推断真实任务状态)。
   */
  onTimeout?: () => void;
}

/**
 * 订阅异步任务进度。
 *
 * @param taskId 任务 ID;null/undefined/空字符串 → 不订阅
 * @param options 事件回调(`onMessage` 拿完整帧;调用方按 `event` 分流)
 *
 * ## B-06 hot-fix(2026-05-19):callback ref 防御
 *
 * v1.0 hot-fix 体检报告 §A-04 hypothesis 1 实证:调用方传未 memoize 的内联
 * onMessage 时,旧实现 ``useEffect`` 依赖项含 ``onMessage`` → 父组件每次 re-render
 * 触发 cleanup + 重新订阅 → WS sub/unsub 8ms 内三连抖动(同一 channel)→ 高频
 * stage event 叠加 React StrictMode 双 mount 下 listener 集合泄漏 + 主线程
 * setState 风暴占满 → "卡死 + 刷新无效"现象。
 *
 * 修复:内部用 ref 同步最新 callback,``useEffect`` 只依赖 ``[taskId]``,
 * callback ref 变化不再触发 cleanup/resubscribe。caller 可继续传内联函数
 * 也不会抖动 — 是健壮性兜底,B-05 同时 caller 侧补 useCallback 是双保险。
 */
export function useTaskProgress<TData = unknown>(
  taskId: string | null | undefined,
  options: UseTaskProgressOptions<TData> = {},
): void {
  const { onMessage, timeoutMs, onTimeout } = options;

  // 同步最新 onMessage / onTimeout 到 ref,避免它们进 useEffect deps 触发抖动
  const onMessageRef = useRef(onMessage);
  onMessageRef.current = onMessage;
  const onTimeoutRef = useRef(onTimeout);
  onTimeoutRef.current = onTimeout;

  useEffect(() => {
    if (typeof taskId !== "string" || taskId.length === 0) {
      return;
    }
    const client = getWsClient();
    const channel = taskProgressChannel(taskId);
    const unsubscribe = client.subscribe<TData>(channel, (frame) => {
      onMessageRef.current?.(frame);
    });

    // v1.0.x wave 6 s2.6c hotfix:timeout fallback(详 UseTaskProgressOptions
    // §timeoutMs docstring)。订阅启动起算,超时未到终态就调 onTimeout。
    let timerId: ReturnType<typeof setTimeout> | null = null;
    if (typeof timeoutMs === "number" && timeoutMs > 0) {
      timerId = setTimeout(() => {
        timerId = null;
        onTimeoutRef.current?.();
      }, timeoutMs);
    }

    return () => {
      unsubscribe();
      if (timerId !== null) {
        clearTimeout(timerId);
      }
    };
  }, [taskId, timeoutMs]);
}
