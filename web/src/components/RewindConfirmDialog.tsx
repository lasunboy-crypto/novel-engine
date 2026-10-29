/**
 * RewindConfirmDialog(P6-F-06):章节 rewind 二次确认对话框。
 *
 * 对齐 FRONTEND_SPEC §5.4 字面"破坏性操作要 user 显式留痕" + plan §P6-F-06
 * 字面"显示'将回退 ch-NNNN + 标废后续 X 章 + 记忆库 vectors 撤销'"。
 *
 * 本组件是 `ConfirmDialog` 的薄包装,封装 rewind 专属文案 + 章节号显示。
 *
 * ## 为何不显示后续章节具体数量
 *
 * 后端 `:rewind` 端点在 service 层动态计算"chapter_id > target_id"列表,
 * 前端在调用前无法精确知道几章会被 invalidated(需要拉章节列表 + 客户端
 * 模拟过滤,徒增复杂度且与后端定义可能漂移)。所以二次确认对话框只展示
 * 通用文案"标废所有后续章节",rewind 完成后 toast 显示实际数字。
 */

import { ConfirmDialog } from "@/components/ConfirmDialog";

export interface RewindConfirmDialogProps {
  open: boolean;
  onOpenChange: (open: boolean) => void;
  /** 当前要 rewind 的章节(显示给用户)。 */
  chapterId: string;
  /** 当前章节标题(可选,显示更友好)。 */
  chapterTitle?: string | null;
  /** 用户点确认时回调;mutation pending 期间禁用按钮。 */
  onConfirm: () => void;
  /** 用户点取消时回调,可选。 */
  onCancel?: () => void;
  /** mutation pending 期间禁用确认按钮。 */
  isConfirmDisabled?: boolean;
}

export function RewindConfirmDialog(props: RewindConfirmDialogProps) {
  const titleText = props.chapterTitle
    ? `第 ${props.chapterTitle}`
    : props.chapterId;

  return (
    <ConfirmDialog
      open={props.open}
      onOpenChange={props.onOpenChange}
      title="确认回退章节"
      description={
        <span data-testid="rewind-confirm-description">
          将回退 <strong className="font-mono">{props.chapterId}</strong>
          {props.chapterTitle ? (
            <>
              {" "}
              ({titleText})
            </>
          ) : null}
          {" "}到起草状态,并:
          <ul className="ml-4 mt-2 list-disc space-y-1 text-xs">
            <li>标废所有后续章节(状态翻 invalidated,需 unblock 才能继续写)</li>
            <li>记忆库 vectors 软删(metadata.revoked=true)</li>
            <li>SQLite extracted_*(角色 / 伏笔 / 时间线)对应行 revoked_at 翻 now</li>
            <li>章节版本历史保留(chapter_versions 表不删)</li>
          </ul>
          <span className="mt-2 block text-xs text-muted-foreground">
            后续章节可通过"全部恢复"或单章"恢复(unblock)"翻回 drafting。
          </span>
        </span>
      }
      confirmLabel="确认回退"
      cancelLabel="取消"
      variant="destructive"
      onConfirm={props.onConfirm}
      onCancel={props.onCancel}
      isConfirmDisabled={props.isConfirmDisabled}
    />
  );
}
