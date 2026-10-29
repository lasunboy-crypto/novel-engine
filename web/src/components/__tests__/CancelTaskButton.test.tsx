/**
 * CancelTaskButton 单测(v1.1 w4-s7 = J2.1)。
 *
 * 验证:① 默认渲染"取消"+ 点击调 onCancel ② isCancelling 时禁用 + 文案"取消中…"
 * + 点击不再触发 onCancel。
 */
import { fireEvent, render, screen } from "@testing-library/react";
import { describe, expect, it, vi } from "vitest";

import { CancelTaskButton } from "@/components/CancelTaskButton";

describe("CancelTaskButton", () => {
  it("默认渲染'取消'并在点击时调 onCancel", () => {
    const onCancel = vi.fn();
    render(<CancelTaskButton onCancel={onCancel} isCancelling={false} />);

    const btn = screen.getByTestId("cancel-task");
    expect(btn).toHaveTextContent("取消");
    expect(btn).not.toBeDisabled();

    fireEvent.click(btn);
    expect(onCancel).toHaveBeenCalledTimes(1);
  });

  it("isCancelling 时禁用 + 文案'取消中…' + 点击不触发 onCancel", () => {
    const onCancel = vi.fn();
    render(<CancelTaskButton onCancel={onCancel} isCancelling={true} />);

    const btn = screen.getByTestId("cancel-task");
    expect(btn).toHaveTextContent("取消中…");
    expect(btn).toBeDisabled();

    fireEvent.click(btn);
    expect(onCancel).not.toHaveBeenCalled();
  });

  it("testId 可自定义", () => {
    render(
      <CancelTaskButton
        onCancel={vi.fn()}
        isCancelling={false}
        testId="write-task-cancel"
      />,
    );
    expect(screen.getByTestId("write-task-cancel")).toBeInTheDocument();
  });
});
