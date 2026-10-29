/**
 * useToast 自管理 auto-dismiss 防回退测(v1.1 wave 7,§7)。
 *
 * 钉住:Radix 内部 timer 被 `duration={Infinity}` 关掉后,store 级
 * `TOAST_AUTO_DISMISS_MS` 计时器接管 —— toast 到点自动 dismiss(open→false),
 * 而非永远停在屏上。防后续改动误删 store 计时器又退回"toast 不消失"老坑。
 */

import { describe, it, expect, vi, beforeEach, afterEach } from "vitest";
import { renderHook, act } from "@testing-library/react";

import { useToast } from "@/hooks/use-toast";

describe("useToast 自动消失(v1.1 wave 7)", () => {
  beforeEach(() => {
    vi.useFakeTimers();
  });
  afterEach(() => {
    vi.runOnlyPendingTimers();
    vi.useRealTimers();
  });

  it("toast 到点(4s)自动 dismiss → open 翻 false", () => {
    const { result } = renderHook(() => useToast());
    act(() => {
      result.current.toast({ title: "自动消失测试" });
    });
    // 刚弹出:open=true
    expect(result.current.toasts[0]?.open).toBe(true);
    // 推进到自动消失时长
    act(() => {
      vi.advanceTimersByTime(4000);
    });
    // store 计时器主动 dismiss → controlled open 翻 false(Radix 正常退场)
    expect(result.current.toasts[0]?.open).toBe(false);
  });

  it("时长内不提前消失(3s 仍在显示)", () => {
    const { result } = renderHook(() => useToast());
    act(() => {
      result.current.toast({ title: "未到点测试" });
    });
    act(() => {
      vi.advanceTimersByTime(3000);
    });
    expect(result.current.toasts[0]?.open).toBe(true);
  });
});
