/**
 * RestoreFromBackupDialog(P7-F-01):工作区"从备份恢复"二次确认对话框。
 *
 * 决策 6=B 落地 — 替代 P1 期 `useRestoreWorkspaceMutation` 残破(P1 un-archive
 * 模式 vs P6-B-07 `BackupService.restore_from_backup` 新语义不兼容)。
 *
 * 行为:
 * - 受控 `open` / `onOpenChange`,关闭时重置内部 state(radio 选择 + confirm 输入)
 * - 挂载即 `useBackupsListQuery(workspaceId)` 拉备份列表;loading / error / empty
 *   3 态分别显占位 + restore 按钮 disable
 * - 用户选 1 行 backup(radio 单选,plan §自决项 8 拍板 — 对齐既有 archive/purge
 *   二次确认 dialog 简洁度,不引入 table / card 复杂度;backups 列表后端已按
 *   `created_at` 倒序 + 过滤 `pre_restore_*`)
 * - confirm 输入 = workspaceId(完整字符串)→ 启用 restore 按钮(对齐
 *   `PurgeConfirmDialog` 同模式 + 后端 RestoreIn body confirm 字段同校验)
 * - 点 restore → 调 `onConfirm({ backupId, confirm })`,父组件调 mutation;失败
 *   由父组件 toast,Dialog 不关 / 选择保留(对齐 PurgeConfirmDialog 同语义)
 *
 * 后端兜底:
 * - `confirm != workspace_id` → 422 `PurgeConfirmMismatch` 同模式(前端预校验
 *   已拦下,绕过预校验直接调也会被后端拦)
 * - `backup_id` 不存在 → 404 `BACKUP_NOT_FOUND`(理论上 dialog 拉的就是后端
 *   列表,不会触发,但仍由 toast 兜底)
 * - schema_version 不兼容 → 422 `RESTORE_VERSION_INCOMPATIBLE`(跨版本备份恢复
 *   场景,P6-B-08 数据迁移机制内场景)
 */

import * as React from "react";

import { useBackupsListQuery } from "@/api/workspaces";
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

export interface RestoreFromBackupDialogProps {
  open: boolean;
  onOpenChange: (open: boolean) => void;
  /** 待恢复的工作区 ID,作为 confirm 字段必须输入的二次确认字符串 */
  workspaceId: string;
  /** 工作区显示名,用于对话框标题/描述 */
  displayName: string;
  /** 用户点恢复时回调,调用方调 mutation;成功在调用方处置(关 Dialog + toast) */
  onConfirm: (payload: { backupId: string; confirm: string }) => void;
  /** mutation pending 时禁用恢复按钮 */
  isPending?: boolean;
}

function formatSize(bytes: number): string {
  if (bytes < 1024) return `${bytes} B`;
  const kb = bytes / 1024;
  if (kb < 1024) return `${kb.toFixed(1)} KB`;
  return `${(kb / 1024).toFixed(1)} MB`;
}

function formatCreatedAt(iso: string): string {
  // 后端给的是 ISO UTC,前端按本地时区显示更直观;月-日 + 时-分 简洁度对齐
  // archive/purge dialog 风格
  try {
    const date = new Date(iso);
    return date.toLocaleString("zh-CN", {
      month: "2-digit",
      day: "2-digit",
      hour: "2-digit",
      minute: "2-digit",
    });
  } catch {
    return iso;
  }
}

export function RestoreFromBackupDialog({
  open,
  onOpenChange,
  workspaceId,
  displayName,
  onConfirm,
  isPending,
}: RestoreFromBackupDialogProps) {
  const [selectedBackupId, setSelectedBackupId] = React.useState<string | null>(
    null,
  );
  const [confirmInput, setConfirmInput] = React.useState("");

  const backupsQuery = useBackupsListQuery(open ? workspaceId : undefined);
  const backups = backupsQuery.data?.backups ?? [];

  const handleOpenChange = (next: boolean) => {
    if (!next) {
      // 关闭时重置 — 不用 useEffect,避 P1-F-08 沉淀 2 死循环坑
      setSelectedBackupId(null);
      setConfirmInput("");
    }
    onOpenChange(next);
  };

  const isConfirmMatched = confirmInput.trim() === workspaceId;
  const canSubmit =
    selectedBackupId !== null && isConfirmMatched && !isPending;

  const handleSubmit = () => {
    if (!canSubmit || selectedBackupId === null) return;
    onConfirm({ backupId: selectedBackupId, confirm: confirmInput.trim() });
  };

  return (
    <Dialog open={open} onOpenChange={handleOpenChange}>
      <DialogContent className="sm:max-w-lg">
        <DialogHeader>
          <DialogTitle>从备份恢复工作区</DialogTitle>
          <DialogDescription>
            选择一份历史备份覆盖当前工作区。恢复前会自动把当前状态打包为
            <code className="mx-1 rounded bg-muted px-1 py-0.5 text-xs">
              pre_restore_&lt;ts&gt;.zip
            </code>
            备份(可在备份列表中找到)。此操作覆盖现有正文 / 大纲 / 卡片 / 卷规划。
          </DialogDescription>
        </DialogHeader>

        <div className="space-y-3">
          <p className="text-sm text-muted-foreground">
            目标工作区:
            <span className="ml-1 font-medium text-foreground">
              {displayName}
            </span>
            <span className="ml-1 text-xs">({workspaceId})</span>
          </p>

          {/* 备份列表区 */}
          <div
            data-testid="restore-backups-list"
            className="max-h-60 overflow-y-auto rounded border border-border"
          >
            {backupsQuery.isPending ? (
              <p className="p-3 text-sm text-muted-foreground">
                加载备份列表…
              </p>
            ) : backupsQuery.isError ? (
              <p
                data-testid="restore-backups-error"
                className="p-3 text-sm text-destructive"
              >
                备份列表加载失败,请关闭重试。
              </p>
            ) : backups.length === 0 ? (
              <p
                data-testid="restore-backups-empty"
                className="p-3 text-sm text-muted-foreground"
              >
                暂无可用备份。先在备份列表手动创建一份,或等待章节 approved
                后自动备份生成。
              </p>
            ) : (
              <ul className="divide-y divide-border">
                {backups.map((backup) => {
                  const isSelected = selectedBackupId === backup.backup_id;
                  return (
                    <li key={backup.backup_id}>
                      <label
                        data-testid={`restore-backup-${backup.backup_id}`}
                        className="flex cursor-pointer items-start gap-3 p-3 hover:bg-muted/50"
                      >
                        <input
                          type="radio"
                          name="restore-backup"
                          className="mt-1"
                          checked={isSelected}
                          onChange={() =>
                            setSelectedBackupId(backup.backup_id)
                          }
                        />
                        <div className="min-w-0 flex-1 space-y-1">
                          <div className="flex items-baseline gap-2 text-sm">
                            <span className="font-medium text-foreground">
                              {backup.backup_id}
                            </span>
                            <span className="text-xs text-muted-foreground">
                              {backup.backup_type === "auto" ? "自动" : "手动"}
                            </span>
                          </div>
                          <div className="text-xs text-muted-foreground">
                            {formatCreatedAt(backup.created_at)} ·{" "}
                            {formatSize(backup.size_bytes)} · schema{" "}
                            {backup.schema_version}
                            {backup.chapter_id ? (
                              <span className="ml-1">
                                · 章节 {backup.chapter_id}
                              </span>
                            ) : null}
                          </div>
                        </div>
                      </label>
                    </li>
                  );
                })}
              </ul>
            )}
          </div>

          {/* 二次确认输入 */}
          <label className="block space-y-1 text-sm">
            <span className="text-muted-foreground">
              请输入工作区 ID
              <code className="mx-1 rounded bg-muted px-1 py-0.5 text-xs">
                {workspaceId}
              </code>
              以确认覆盖:
            </span>
            <Input
              data-testid="restore-confirm-input"
              autoFocus
              value={confirmInput}
              onChange={(e) => setConfirmInput(e.target.value)}
              placeholder={workspaceId}
              autoComplete="off"
              spellCheck={false}
            />
            {confirmInput.length > 0 && !isConfirmMatched && (
              <p
                role="alert"
                data-testid="restore-confirm-mismatch-hint"
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
            data-testid="restore-confirm-submit"
            variant="default"
            disabled={!canSubmit}
            onClick={handleSubmit}
          >
            {isPending ? "恢复中…" : "从备份恢复"}
          </Button>
        </DialogFooter>
      </DialogContent>
    </Dialog>
  );
}
