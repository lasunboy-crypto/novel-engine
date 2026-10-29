/**
 * CardRetryCascadeConfirmDialog(v1.0.x wave 5 session 4):章节卡 retry-references
 * 二次确认对话框(plan §s4 k 件 字面"修改本章节卡后续 N 章需要重生,确认继续?")。
 *
 * 对照 P6-F-06 `RewindConfirmDialog` 同型 — 薄包装 `ConfirmDialog`,封装 retry
 * 专属文案 + 级联章节数预览(基于前端本地 chapter list 的章 id 字典序 > 当前章
 * 估算,精确数值在 mutation 完成后通过 `invalidated_chapter_ids` 显示)。
 *
 * ## 为何前置二次确认(对话框打开时不知精确 N)
 *
 * 后端 `retry_references` 服务层在事务内动态查 `ChapterCardORM where chapter_id
 * > target`,前端在 POST 之前无法 100% 精确预判;但 ChaptersBoardPage 已有
 * 本地 chapter list cache(`useChapterListQuery`),用其过滤本章后续即可估算
 * 与后端口径一致的章数(都是字典序比较,ch-NNNN 4 位定宽)。
 *
 * 估算章数为 0 时仍展示对话框(用户可能从未生成 cascade 章节列表 → 仍走 retry,
 * mutation 完成后 toast 显示真实 invalidated 数。
 */

import { ConfirmDialog } from "@/components/ConfirmDialog";

export interface CardRetryCascadeConfirmDialogProps {
  open: boolean;
  onOpenChange: (open: boolean) => void;
  /** 当前要 retry 的章节 ID(显示给用户)。 */
  chapterId: string;
  /** 本地估算 — 后续将级联重生的章节数(本 ChaptersBoard chapter list 过滤
   *  chapter_id > target 的章数;未知时用 `null` 展示通用文案)。 */
  estimatedCascadeCount: number | null;
  /** 用户点确认时回调;mutation pending 期间禁用按钮。 */
  onConfirm: () => void;
  /** 用户点取消时回调,可选。 */
  onCancel?: () => void;
  /** mutation pending 期间禁用确认按钮。 */
  isConfirmDisabled?: boolean;
}

export function CardRetryCascadeConfirmDialog(
  props: CardRetryCascadeConfirmDialogProps,
) {
  const cascadeLabel =
    props.estimatedCascadeCount === null
      ? "本章后续所有已生成章节卡"
      : props.estimatedCascadeCount === 0
        ? "(无后续章节卡 — 末章场景,仅本章重试)"
        : `本章后续约 ${props.estimatedCascadeCount} 章节卡`;

  return (
    <ConfirmDialog
      open={props.open}
      onOpenChange={props.onOpenChange}
      title="确认重试章节卡引用"
      description={
        <span data-testid="card-retry-cascade-confirm-description">
          即将重试 <strong className="font-mono">{props.chapterId}</strong>{" "}
          章节卡的引用合法性(LLM 重新生成 + 注入同卷其他已审章节 must_achieve
          作 forbidden_phrases 防同质化),并自动级联失效后续章节卡:
          <ul className="ml-4 mt-2 list-disc space-y-1 text-xs">
            <li>
              <strong>本章卡</strong>:LLM 重新生成,新卡覆盖旧 SSOT 文件 + ORM
              翻 <code>card_status='draft'</code>
            </li>
            <li>
              <strong>{cascadeLabel}</strong>
              :后台批量重生(基于更新后的上下文派生新内容),
              <code>card_status='draft'</code>
            </li>
            <li>章节正文(已 approved 的 ChapterORM 行)不动 — 用户需走 rewind 流程</li>
          </ul>
          <span className="mt-2 block text-xs text-muted-foreground">
            cascade 重生在后台跑,完成前可继续浏览其他页面;响应里
            <code>cascade_regenerate_task_id</code> 可订阅 WS task progress 看进度。
          </span>
        </span>
      }
      confirmLabel="确认重试 + 级联"
      cancelLabel="取消"
      variant="destructive"
      onConfirm={props.onConfirm}
      onCancel={props.onCancel}
      isConfirmDisabled={props.isConfirmDisabled}
    />
  );
}
