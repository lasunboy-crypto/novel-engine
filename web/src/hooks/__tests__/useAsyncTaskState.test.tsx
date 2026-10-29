/**
 * useAsyncTaskState 单测(v1.1 w4-s7 = J2.1)。
 *
 * 覆盖 plan §4 w4-s7 + candidates §J2.1 要求:
 * - WS done event → reset taskId + onSuccess
 * - WS failed event → reset + onFailed(不传走默认 showApiErrorToast)
 * - WS cancelled event(J1)→ reset + onCancelled
 * - 非终态 stage 帧 → onMessage 透传、不 reset
 * - timeout 触发 → reset + 默认超时 toast(或自定义 onTimeout)
 * - unsubscribe cleanup(unmount / taskId 变更时退订)
 * - cancel → 调 useCancelTaskMutation + isCancelling 过渡态
 * - start 携带 meta + meta 与 taskId 同生灭
 *
 * mock WS client(getWsClient)以程序化推送帧;mock useCancelTaskMutation /
 * Toast 避免真发请求 / 真弹 toast。
 */
import { act, renderHook } from "@testing-library/react";
import { afterEach, beforeEach, describe, expect, it, vi } from "vitest";

type Listener = (frame: unknown) => void;

/** 已注册的 (channel, listener);测试用它定位并推送帧。 */
let registered: { channel: string; listener: Listener }[] = [];
const unsubscribeSpy = vi.fn();
const subscribeSpy = vi.fn((channel: string, listener: Listener) => {
  registered.push({ channel, listener });
  return unsubscribeSpy;
});

vi.mock("@/ws/client", () => ({
  getWsClient: () => ({ subscribe: subscribeSpy }),
}));

const cancelMutate = vi.fn();
vi.mock("@/api/tasks", () => ({
  useCancelTaskMutation: () => ({ mutate: cancelMutate }),
}));

const showApiErrorToast = vi.fn();
const showSuccessToast = vi.fn();
vi.mock("@/components/Toast", () => ({
  showApiErrorToast: (...args: unknown[]) => showApiErrorToast(...args),
  showSuccessToast: (...args: unknown[]) => showSuccessToast(...args),
}));

import { useAsyncTaskState } from "@/hooks/useAsyncTaskState";

/** 往某 channel 推一帧(包 act 触发 React state 更新)。 */
function pushFrame(channel: string, frame: unknown) {
  const entry = registered.find((r) => r.channel === channel);
  if (!entry) throw new Error(`no listener for ${channel}`);
  act(() => entry.listener(frame));
}

beforeEach(() => {
  registered = [];
  subscribeSpy.mockClear();
  unsubscribeSpy.mockClear();
  cancelMutate.mockReset();
  showApiErrorToast.mockReset();
  showSuccessToast.mockReset();
});

afterEach(() => {
  vi.useRealTimers();
});

describe("useAsyncTaskState", () => {
  it("start 登记 taskId/meta + 订阅 task.{id}.progress 频道", () => {
    const { result } = renderHook(() =>
      useAsyncTaskState<unknown, "write">({ kind: "chapter_write" }),
    );

    expect(result.current.isRunning).toBe(false);
    expect(result.current.taskId).toBeNull();

    act(() => result.current.start("task_1", "write"));

    expect(result.current.isRunning).toBe(true);
    expect(result.current.taskId).toBe("task_1");
    expect(result.current.meta).toBe("write");
    expect(subscribeSpy).toHaveBeenCalledWith(
      "task.task_1.progress",
      expect.any(Function),
    );
  });

  it("WS done 事件 → reset taskId + 调 onSuccess", () => {
    const onSuccess = vi.fn();
    const { result } = renderHook(() =>
      useAsyncTaskState({ kind: "chapter_write", onSuccess }),
    );
    act(() => result.current.start("task_done"));

    pushFrame("task.task_done.progress", {
      event: "done",
      data: { task_id: "task_done", result: {} },
    });

    expect(result.current.isRunning).toBe(false);
    expect(result.current.taskId).toBeNull();
    expect(onSuccess).toHaveBeenCalledTimes(1);
  });

  it("WS failed 事件 → reset + onFailed 拿到错误信息", () => {
    const onFailed = vi.fn();
    const { result } = renderHook(() =>
      useAsyncTaskState<{ error?: { message?: string } }>({
        kind: "chapter_write",
        onFailed,
      }),
    );
    act(() => result.current.start("task_fail"));

    pushFrame("task.task_fail.progress", {
      event: "failed",
      data: { error: { message: "boom" } },
    });

    expect(result.current.isRunning).toBe(false);
    expect(onFailed).toHaveBeenCalledWith("boom", expect.anything());
    // 传了 onFailed → 不走默认 toast
    expect(showApiErrorToast).not.toHaveBeenCalled();
  });

  it("WS failed 事件不传 onFailed → 默认 showApiErrorToast(用 kind 标签兜底)", () => {
    const { result } = renderHook(() =>
      useAsyncTaskState({ kind: "outline_section" }),
    );
    act(() => result.current.start("task_fail2"));

    pushFrame("task.task_fail2.progress", { event: "failed", data: {} });

    expect(result.current.isRunning).toBe(false);
    expect(showApiErrorToast).toHaveBeenCalledTimes(1);
    const errArg = showApiErrorToast.mock.calls[0][0] as Error;
    expect(errArg.message).toContain("单段重生失败");
  });

  it("WS cancelled 事件(J1)→ reset + 调 onCancelled", () => {
    const onCancelled = vi.fn();
    const { result } = renderHook(() =>
      useAsyncTaskState({ kind: "chapter_cascade", onCancelled }),
    );
    act(() => result.current.start("task_c"));

    pushFrame("task.task_c.progress", {
      event: "cancelled",
      data: { task_id: "task_c" },
    });

    expect(result.current.isRunning).toBe(false);
    expect(onCancelled).toHaveBeenCalledTimes(1);
  });

  it("非终态 stage 帧 → onMessage 透传、不 reset", () => {
    const onMessage = vi.fn();
    const onSuccess = vi.fn();
    const { result } = renderHook(() =>
      useAsyncTaskState({ kind: "outline_generate", onMessage, onSuccess }),
    );
    act(() => result.current.start("task_stage"));

    pushFrame("task.task_stage.progress", {
      event: "stage",
      data: { stage: "world" },
    });

    expect(result.current.isRunning).toBe(true); // 仍在跑
    expect(onMessage).toHaveBeenCalledTimes(1);
    expect(onSuccess).not.toHaveBeenCalled();
  });

  it("timeout 触发 → reset + 默认超时 toast", () => {
    vi.useFakeTimers();
    const { result } = renderHook(() =>
      // customTimeoutMs 显式小值,避免依赖 5min 默认
      useAsyncTaskState({ kind: "chapter_write", customTimeoutMs: 1000 }),
    );
    act(() => result.current.start("task_timeout"));

    act(() => {
      vi.advanceTimersByTime(1000);
    });

    expect(result.current.isRunning).toBe(false);
    expect(showApiErrorToast).toHaveBeenCalledTimes(1);
  });

  it("timeout 触发 → 传了 onTimeout 则调它、不走默认 toast", () => {
    vi.useFakeTimers();
    const onTimeout = vi.fn();
    const { result } = renderHook(() =>
      useAsyncTaskState({
        kind: "chapter_write",
        customTimeoutMs: 500,
        onTimeout,
      }),
    );
    act(() => result.current.start("task_t2"));

    act(() => {
      vi.advanceTimersByTime(500);
    });

    expect(result.current.isRunning).toBe(false);
    expect(onTimeout).toHaveBeenCalledTimes(1);
    expect(showApiErrorToast).not.toHaveBeenCalled();
  });

  it("customTimeoutMs=0 → 不启用 timeout", () => {
    vi.useFakeTimers();
    const { result } = renderHook(() =>
      useAsyncTaskState({ kind: "export", customTimeoutMs: 0 }),
    );
    act(() => result.current.start("task_no_timeout"));

    act(() => {
      vi.advanceTimersByTime(60_000);
    });

    // 无 timeout → 仍在跑、不弹 toast
    expect(result.current.isRunning).toBe(true);
    expect(showApiErrorToast).not.toHaveBeenCalled();
  });

  it("outline 长跑提醒后保留 taskId/WS，后续 done 仍正常收口", () => {
    vi.useFakeTimers();
    const onTimeout = vi.fn();
    const onSuccess = vi.fn();
    const { result } = renderHook(() =>
      useAsyncTaskState({
        kind: "outline_generate",
        keepRunningOnTimeout: true,
        onTimeout,
        onSuccess,
      }),
    );
    act(() => result.current.start("task_outline_long"));

    act(() => {
      vi.advanceTimersByTime(1_200_000);
    });
    expect(result.current.isRunning).toBe(true);
    expect(result.current.taskId).toBe("task_outline_long");
    expect(onTimeout).not.toHaveBeenCalled();

    act(() => {
      vi.advanceTimersByTime(1_200_000);
    });
    expect(onTimeout).toHaveBeenCalledTimes(1);
    expect(result.current.isRunning).toBe(true);
    expect(result.current.taskId).toBe("task_outline_long");

    pushFrame("task.task_outline_long.progress", {
      event: "done",
      data: { task_id: "task_outline_long" },
    });
    expect(result.current.isRunning).toBe(false);
    expect(result.current.taskId).toBeNull();
    expect(onSuccess).toHaveBeenCalledTimes(1);
  });

  it("unmount → 退订 WS(unsubscribe cleanup)", () => {
    const { result, unmount } = renderHook(() =>
      useAsyncTaskState({ kind: "chapter_write" }),
    );
    act(() => result.current.start("task_unmount"));
    expect(subscribeSpy).toHaveBeenCalledTimes(1);

    unmount();

    expect(unsubscribeSpy).toHaveBeenCalledTimes(1);
  });

  it("终态后再 start 新 taskId → 旧订阅退订 + 订阅新频道", () => {
    const { result } = renderHook(() =>
      useAsyncTaskState({ kind: "chapter_write" }),
    );
    act(() => result.current.start("task_a"));
    pushFrame("task.task_a.progress", { event: "done", data: {} });
    // done 后 taskId 变 null → 旧订阅退订
    expect(unsubscribeSpy).toHaveBeenCalledTimes(1);

    act(() => result.current.start("task_b"));
    expect(subscribeSpy).toHaveBeenLastCalledWith(
      "task.task_b.progress",
      expect.any(Function),
    );
  });

  it("cancel → 调 useCancelTaskMutation(taskId) + isCancelling 翻 true", () => {
    const { result } = renderHook(() =>
      useAsyncTaskState({ kind: "chapter_write" }),
    );
    act(() => result.current.start("task_cancel"));

    act(() => result.current.cancel());

    expect(cancelMutate).toHaveBeenCalledWith(
      "task_cancel",
      expect.objectContaining({ onError: expect.any(Function) }),
    );
    expect(result.current.isCancelling).toBe(true);
    // 任务仍在跑(尽力中断、非瞬时)
    expect(result.current.isRunning).toBe(true);
  });

  it("cancel 后收到 cancelled 终态 → isCancelling 复位 + 任务结束", () => {
    const { result } = renderHook(() =>
      useAsyncTaskState({ kind: "chapter_write" }),
    );
    act(() => result.current.start("task_cancel2"));
    act(() => result.current.cancel());
    expect(result.current.isCancelling).toBe(true);

    pushFrame("task.task_cancel2.progress", {
      event: "cancelled",
      data: { task_id: "task_cancel2" },
    });

    expect(result.current.isRunning).toBe(false);
    expect(result.current.isCancelling).toBe(false);
  });

  it("无 taskId 时 cancel 静默 no-op", () => {
    const { result } = renderHook(() =>
      useAsyncTaskState({ kind: "chapter_write" }),
    );
    act(() => result.current.cancel());
    expect(cancelMutate).not.toHaveBeenCalled();
    expect(result.current.isCancelling).toBe(false);
  });
});
