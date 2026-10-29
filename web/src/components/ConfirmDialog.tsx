/**
 * 危险操作二次确认对话框(P1-F-05)。
 *
 * 受控组件,基于 shadcn `Dialog` 原语 + `Button`。调用方控制 `open` 状态,
 * 点确认调用 `onConfirm`,点取消(或 ESC / 关闭按钮)调用 `onCancel`(可选)。
 *
 * 典型调用点(P1-F-09 起):
 *   const [confirmOpen, setConfirmOpen] = useState(false)
 *   <ConfirmDialog
 *     open={confirmOpen}
 *     onOpenChange={setConfirmOpen}
 *     title="确认彻底删除"
 *     description="此操作不可撤销,所有章节、版本、归档都会被删除。"
 *     confirmLabel="彻底删除"
 *     cancelLabel="取消"
 *     variant="destructive"
 *     onConfirm={() => purgeMutation.mutate(workspaceId)}
 *   />
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

export interface ConfirmDialogProps {
  /** 受控:dialog 是否打开。 */
  open: boolean;
  /** 受控:打开/关闭状态变更。Radix 触发 ESC / 点遮罩 / 点关闭按钮时调用。 */
  onOpenChange: (open: boolean) => void;
  /** dialog 顶部标题。 */
  title: React.ReactNode;
  /** dialog 描述,可省略(纯标题确认场景)。 */
  description?: React.ReactNode;
  /** 确认按钮文本,默认"确认"。 */
  confirmLabel?: string;
  /** 取消按钮文本,默认"取消"。 */
  cancelLabel?: string;
  /**
   * 确认按钮样式 variant:
   * - `default`(默认):普通确认场景
   * - `destructive`:红色,危险操作(purge / 强制覆盖等)
   */
  variant?: "default" | "destructive";
  /** 用户点确认时回调。Dialog 在调用后自动关闭。 */
  onConfirm: () => void;
  /** 用户点取消(或 ESC / 点遮罩)时回调,可选。 */
  onCancel?: () => void;
  /** 确认按钮 disabled 状态(用于 mutation pending),可选。 */
  isConfirmDisabled?: boolean;
}

export function ConfirmDialog({
  open,
  onOpenChange,
  title,
  description,
  confirmLabel = "确认",
  cancelLabel = "取消",
  variant = "default",
  onConfirm,
  onCancel,
  isConfirmDisabled,
}: ConfirmDialogProps) {
  // confirmedRef 标记本次关闭由"确认"触发,避免 onOpenChange 误触发 onCancel
  const confirmedRef = React.useRef(false);

  const handleCancel = () => {
    onOpenChange(false);
  };
  const handleConfirm = () => {
    confirmedRef.current = true;
    onConfirm();
    onOpenChange(false);
  };

  return (
    <Dialog
      open={open}
      onOpenChange={(next) => {
        if (!next) {
          if (confirmedRef.current) {
            confirmedRef.current = false;
          } else {
            onCancel?.();
          }
        }
        onOpenChange(next);
      }}
    >
      <DialogContent>
        <DialogHeader>
          <DialogTitle>{title}</DialogTitle>
          {description ? (
            <DialogDescription>{description}</DialogDescription>
          ) : null}
        </DialogHeader>
        <DialogFooter>
          <Button variant="outline" onClick={handleCancel}>
            {cancelLabel}
          </Button>
          <Button
            variant={variant === "destructive" ? "destructive" : "default"}
            onClick={handleConfirm}
            disabled={isConfirmDisabled}
          >
            {confirmLabel}
          </Button>
        </DialogFooter>
      </DialogContent>
    </Dialog>
  );
}
