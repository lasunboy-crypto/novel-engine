/**
 * v1.1 w3-s8 / F1 — PurgeConfirmDialog 参数化测试。
 *
 * 验证 P1-F-09 既有"输入 workspace_id 二次确认"逻辑在参数化后:
 * - 默认 props 保持"彻底删除工作区"用例字面(向后兼容,既有调用方不受影响)
 * - 自定义文案 + testIdPrefix(F1 清空备份复用)正确渲染 + 确认门控不变
 */

import { describe, it, expect, vi } from "vitest";
import { render, screen } from "@testing-library/react";
import userEvent from "@testing-library/user-event";

import { PurgeConfirmDialog } from "@/components/PurgeConfirmDialog";

describe("PurgeConfirmDialog 参数化(w3-s8 F1)", () => {
  it("默认 props 保持彻底删除工作区文案(向后兼容)", () => {
    render(
      <PurgeConfirmDialog
        open
        onOpenChange={() => {}}
        workspaceId="ws-1"
        displayName="测试书"
        onConfirm={() => {}}
      />,
    );
    expect(screen.getByText("确认彻底删除工作区")).toBeInTheDocument();
    expect(screen.getByTestId("purge-confirm-input")).toBeInTheDocument();
    expect(screen.getByTestId("purge-confirm-submit")).toHaveTextContent(
      "彻底删除",
    );
  });

  it("自定义清空备份文案 + testIdPrefix 渲染 + 确认门控生效", async () => {
    const user = userEvent.setup();
    const onConfirm = vi.fn();
    render(
      <PurgeConfirmDialog
        open
        onOpenChange={() => {}}
        workspaceId="ws-1"
        displayName="测试书"
        onConfirm={onConfirm}
        title="确认清空全部备份"
        confirmLabel="清空备份"
        pendingLabel="清空中…"
        testIdPrefix="purge-backups"
      />,
    );
    expect(screen.getByText("确认清空全部备份")).toBeInTheDocument();
    const submit = screen.getByTestId("purge-backups-confirm-submit");
    expect(submit).toHaveTextContent("清空备份");
    // 输入未匹配 → 禁用 + 不回调
    expect(submit).toBeDisabled();
    await user.click(submit);
    expect(onConfirm).not.toHaveBeenCalled();
    // 输入匹配 workspace_id → 启用 + 点确认回调
    await user.type(
      screen.getByTestId("purge-backups-confirm-input"),
      "ws-1",
    );
    expect(submit).toBeEnabled();
    await user.click(submit);
    expect(onConfirm).toHaveBeenCalledWith("ws-1");
  });
});
