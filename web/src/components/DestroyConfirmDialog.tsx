/**
 * DestroyConfirmDialog(P7-B-07):工作区完全销毁(三方全删)二次确认对话框。
 *
 * 沿用 PurgeConfirmDialog 同模式(P1-F-09 沉淀 — 不复用 ConfirmDialog,因
 * 需要输入框 + 字符串校验,与 yes/no 受控组件职责隔离)。
 *
 * 与 PurgeConfirmDialog 区别:
 * - 文案明确告知"三方全删"(workspace 目录 + 工作区内 backups + 顶级 archive),
 *   对比 purge 仅删工作区目录保留 backups + 顶级 archive
 * - 红色警告条强调"完全不可逆(无任何 restore 路径)"
 * - 标题/按钮文案改"完全销毁"区别于 purge 的"彻底删除"(避免视觉混淆)
 *
 * 行为(对齐 PurgeConfirmDialog):
 * - 受控 `open` / `onOpenChange`
 * - 关闭后 input 重置为空(关闭路径触发,不用 useEffect)
 * - 输入 trim 后等于 `workspaceId` → 启用确认按钮(前端预校验)
 * - 点确认 → 调 `onConfirm(input)`,父组件调 mutation;失败由父组件 toast,
 *   Dialog 不关 / 输入保留(让用户改字段重试)
 *
 * 后端校验仍是 SSOT:绕过前端预校验直接调 mutation 会被后端 422
 * `WORKSPACE_DESTROY_CONFIRM_INVALID` 拦下走 toast。
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

export interface DestroyConfirmDialogProps {
  open: boolean;
  onOpenChange: (open: boolean) => void;
  /** 待销毁的工作区 ID,作为用户必须输入的二次确认字符串 */
  workspaceId: string;
  /** 工作区显示名,用于对话框标题/描述 */
  displayName: string;
  /** 用户点确认时回调,调用方调 mutation;成功在调用方处置(关 Dialog + toast) */
  onConfirm: (confirm: string) => void;
  /** mutation pending 时禁用确认按钮 */
  isPending?: boolean;
}

export function DestroyConfirmDialog({
  open,
  onOpenChange,
  workspaceId,
  displayName,
  onConfirm,
  isPending,
}: DestroyConfirmDialogProps) {
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
          <DialogTitle>完全销毁工作区(三方全删)</DialogTitle>
          <DialogDescription>
            将物理删除:① 工作区目录(含工作区内 backups/、data/novel.db、
            chroma/ 等全部数据)② 顶级 archives/&lt;id&gt;.tar.gz(若曾归档过)。
          </DialogDescription>
        </DialogHeader>
        <div
          role="alert"
          data-testid="destroy-warning"
          className="rounded border border-destructive bg-destructive/10 px-3 py-2 text-sm text-destructive"
        >
          完全不可逆 — 无任何 restore 路径。请确保已导出 epub / docx 等最终
          成品到工作区外后再继续。
        </div>
        <div className="space-y-2">
          <p className="text-sm text-muted-foreground">
            待销毁:
            <span className="font-medium text-foreground">{displayName}</span>
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
              data-testid="destroy-confirm-input"
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
                data-testid="destroy-confirm-mismatch-hint"
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
            data-testid="destroy-confirm-submit"
            variant="destructive"
            disabled={!canConfirm}
            onClick={handleConfirm}
          >
            {isPending ? "销毁中…" : "完全销毁"}
          </Button>
        </DialogFooter>
      </DialogContent>
    </Dialog>
  );
}
