/**
 * PurgeConfirmDialog(P1-F-09):工作区物理删除二次确认对话框。
 *
 * 不复用 P1-F-05 ConfirmDialog——后者是 yes/no 受控简单组件,塞 input
 * 进去会污染其 API(P1-F-08 决策 3 字面"职责隔离"原则同处置)。这里专门
 * 处理"输入 workspace_id 字符串校验后才启用确认按钮"的 P1 唯一用例,与
 * NEXT_STEPS / p1_plan 顶部字面"必弹 ConfirmDialog(P1-F-05 已建)"
 * 冲突时按工程职责隔离判断走(impl 文档登记)。
 *
 * 行为:
 * - 受控 `open` / `onOpenChange`
 * - 用户每次打开 → input 重置为空(`handleOpenChange` 关闭路径触发,
 *   不用 useEffect,避 P1-F-08 沉淀 2 死循环坑)
 * - 输入 trim 后等于 `workspaceId` → 启用确认按钮(前端预校验,即时反馈)
 * - 点确认 → 调 `onConfirm(input)`,父组件调 mutation;失败由父组件 toast,
 *   **Dialog 不关 / 输入保留**(让用户改字段重试,语义对齐 P1-F-08 决策 5)
 *
 * 后端校验仍是真实 source of truth:绕过前端预校验直接调 mutation 也会被
 * 后端 422 `PURGE_CONFIRM_MISMATCH` 拦下走 toast。
 */

import * as React from "react";

import { Button } from "@/components/ui/button";
import {
  Dialog,
  DialogContent,
  DialogDescription,
  DialogFooter,
  DialogHeader,
  DialogTitle,
} from "@/components/ui/dialog";
import { Input } from "@/components/ui/input";

export interface PurgeConfirmDialogProps {
  open: boolean;
  onOpenChange: (open: boolean) => void;
  /** 待删除的工作区 ID,作为用户必须输入的二次确认字符串 */
  workspaceId: string;
  /** 工作区显示名,用于对话框标题/描述 */
  displayName: string;
  /** 用户点确认时回调,调用方调 mutation;成功在调用方处置(关 Dialog + toast) */
  onConfirm: (confirm: string) => void;
  /** mutation pending 时禁用确认按钮 */
  isPending?: boolean;
  /**
   * 文案 / testId 覆盖(v1.1 w3-s8 F1)。默认是"彻底删除工作区"用例;
   * F1 清空备份复用同款"输入 workspace_id 二次确认"逻辑,只换文案 + testId
   * 前缀(向后兼容 — 不传则保持 P1-F-09 既有行为,既有调用方 / 测试不受影响)。
   */
  title?: string;
  description?: React.ReactNode;
  confirmLabel?: string;
  pendingLabel?: string;
  testIdPrefix?: string;
}

export function PurgeConfirmDialog({
  open,
  onOpenChange,
  workspaceId,
  displayName,
  onConfirm,
  isPending,
  title = "确认彻底删除工作区",
  description = "此操作不可撤销。工作区目录将被物理删除,所有章节、版本、运行时数据(顶级 archives 与工作区内 backups 不动)。",
  confirmLabel = "彻底删除",
  pendingLabel = "删除中…",
  testIdPrefix = "purge",
}: PurgeConfirmDialogProps) {
  const [input, setInput] = React.useState("");

  const handleOpenChange = (next: boolean) => {
    if (!next) setInput("");
    onOpenChange(next);
  };

  const isMatched = input.trim() === workspaceId;
  const canConfirm = isMatched && !isPending;

  const handleConfirm = () => {
    if (!canConfirm) return;
    onConfirm(input.trim());
  };

  return (
    <Dialog open={open} onOpenChange={handleOpenChange}>
      <DialogContent className="sm:max-w-md">
        <DialogHeader>
          <DialogTitle>{title}</DialogTitle>
          <DialogDescription>{description}</DialogDescription>
        </DialogHeader>
        <div className="space-y-2">
          <p className="text-sm text-muted-foreground">
            待删除:<span className="font-medium text-foreground">{displayName}</span>
            <span className="ml-1 text-xs">({workspaceId})</span>
          </p>
          <label className="block space-y-1 text-sm">
            <span className="text-muted-foreground">
              请输入工作区 ID
              <code className="mx-1 rounded bg-muted px-1 py-0.5 text-xs">
                {workspaceId}
              </code>
              以确认:
            </span>
            <Input
              data-testid={`${testIdPrefix}-confirm-input`}
              autoFocus
              value={input}
              onChange={(e) => setInput(e.target.value)}
              placeholder={workspaceId}
              autoComplete="off"
              spellCheck={false}
            />
            {input.length > 0 && !isMatched && (
              <p
                role="alert"
                data-testid={`${testIdPrefix}-confirm-mismatch-hint`}
                className="text-xs text-destructive"
              >
                输入与工作区 ID 不一致
              </p>
            )}
          </label>
        </div>
        <DialogFooter>
          <Button variant="outline" onClick={() => handleOpenChange(false)}>
            取消
          </Button>
          <Button
            data-testid={`${testIdPrefix}-confirm-submit`}
            variant="destructive"
            disabled={!canConfirm}
            onClick={handleConfirm}
          >
            {isPending ? pendingLabel : confirmLabel}
          </Button>
        </DialogFooter>
      </DialogContent>
    </Dialog>
  );
}
