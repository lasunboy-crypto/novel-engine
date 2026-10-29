/**
 * ForcePassConfirmDialog(P6-F-06):章节 force-pass 二次确认对话框。
 *
 * 对齐 FRONTEND_SPEC §5.4 字面"破坏性操作要 user 显式留痕" + 后端
 * `ChapterForcePassIn.reason` Pydantic `min_length=1` 强制非空(P6-B-01
 * 自决项 1)— force-pass 跳过 Reviewer 是显著决策点,留理由便于事后追溯。
 *
 * 本组件 **不复用** ConfirmDialog 是因为 force-pass 需要 reason textarea
 * 字段(ConfirmDialog 只有 title/description/确认按钮,无表单字段);自决
 * 选择内部用 shadcn Dialog 原语直接组装,与 RewindConfirmDialog 走 different
 * 路径但语义对称。
 *
 * ## reason 校验
 *
 * `reason.trim().length === 0` 时 confirm 按钮 disabled;后端 Pydantic
 * `min_length=1` 也会兜底校验,但前端先 disable 提供更好 UX。
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

export interface ForcePassConfirmDialogProps {
  open: boolean;
  onOpenChange: (open: boolean) => void;
  /** 当前要 force-pass 的章节(显示给用户)。 */
  chapterId: string;
  /** 当前章节标题(可选,显示更友好)。 */
  chapterTitle?: string | null;
  /**
   * 用户点确认时回调,传入 `reason`(trim 后);mutation 由调用方触发。
   */
  onConfirm: (reason: string) => void;
  /** 用户点取消时回调,可选。 */
  onCancel?: () => void;
  /** mutation pending 期间禁用确认按钮 + textarea。 */
  isConfirmDisabled?: boolean;
}

export function ForcePassConfirmDialog(props: ForcePassConfirmDialogProps) {
  const [reason, setReason] = React.useState("");
  const confirmedRef = React.useRef(false);

  // Dialog 关闭时(任何路径)清空 reason,避免下次打开残留旧值。
  React.useEffect(() => {
    if (!props.open) {
      setReason("");
      confirmedRef.current = false;
    }
  }, [props.open]);

  const trimmed = reason.trim();
  const reasonInvalid = trimmed.length === 0;

  const handleConfirm = () => {
    if (reasonInvalid) return;
    confirmedRef.current = true;
    props.onConfirm(trimmed);
    props.onOpenChange(false);
  };

  const handleCancel = () => {
    props.onOpenChange(false);
  };

  return (
    <Dialog
      open={props.open}
      onOpenChange={(next) => {
        if (!next) {
          if (confirmedRef.current) {
            confirmedRef.current = false;
          } else {
            props.onCancel?.();
          }
        }
        props.onOpenChange(next);
      }}
    >
      <DialogContent>
        <DialogHeader>
          <DialogTitle>确认强制通过审稿</DialogTitle>
          <DialogDescription>
            <span data-testid="force-pass-confirm-description">
              将跳过 Reviewer,把{" "}
              <strong className="font-mono">{props.chapterId}</strong>
              {props.chapterTitle ? <> ({props.chapterTitle})</> : null} 直接
              翻为 <strong>approved</strong>。此操作绕过质量审核,需留下显式
              理由以便事后追溯(写入 chapters.force_pass_reason)。
            </span>
          </DialogDescription>
        </DialogHeader>
        <div className="space-y-2">
          <label
            htmlFor="force-pass-reason"
            className="block text-xs font-medium text-muted-foreground"
          >
            理由(必填,持久化到 ChapterORM.force_pass_reason)
          </label>
          <textarea
            id="force-pass-reason"
            value={reason}
            onChange={(e) => setReason(e.target.value)}
            rows={3}
            placeholder="例:Reviewer 误判 tension_level,人工核查后判定通过"
            className="w-full rounded border border-input bg-background px-2 py-1.5 text-sm focus-visible:outline-none focus-visible:ring-2 focus-visible:ring-ring"
            data-testid="force-pass-reason-textarea"
            disabled={props.isConfirmDisabled}
          />
          {reasonInvalid && (
            <p
              className="text-xs text-destructive"
              data-testid="force-pass-reason-hint"
            >
              理由不能为空(去除首尾空白后至少 1 字符)。
            </p>
          )}
        </div>
        <DialogFooter>
          <Button variant="outline" onClick={handleCancel}>
            取消
          </Button>
          <Button
            variant="destructive"
            onClick={handleConfirm}
            disabled={reasonInvalid || props.isConfirmDisabled}
            data-testid="force-pass-confirm-btn"
          >
            确认强制通过
          </Button>
        </DialogFooter>
      </DialogContent>
    </Dialog>
  );
}
