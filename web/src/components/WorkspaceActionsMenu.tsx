/**
 * WorkspaceActionsMenu(P1-F-09 / P7-F-01 更新):工作区生命周期 5 动作菜单。
 *
 * 每个 WorkspaceRow 右侧一颗"…"按钮,点开 DropdownMenu 展开 5 项:
 * 归档 / 清理 / 备份 / 从备份恢复 / 彻底删除。各调对应 :archive / :clean /
 * :backup / :restore / :purge 端点。
 *
 * 二次确认策略(决策 2):
 * - **归档**:工作区从可见列表"消失"是 UX 副作用,弹简版 ConfirmDialog
 * - **从备份恢复**(P7-F-01 重写):弹 RestoreFromBackupDialog 选 backup
 *   + 输入 workspace_id 二次确认(原 P1 un-archive 模式已废)
 * - **彻底删除**:后端硬要求 + 物理删除不可逆,弹 PurgeConfirmDialog
 *   要求输入 workspace_id 字符串
 * - 清理 / 备份 — 直接执行 + toast(后端事实证据见
 *   `docs/impl/p1_f_09_workspaces_lifecycle.md`)
 *
 * 状态相关启用规则(决策 4 + P7-F-01 更新):
 * - :archive 在 status="archived" 时禁用(已归档,后端会 409
 *   `WORKSPACE_ALREADY_ARCHIVED`)
 * - **:restore 不再受 status 约束**(P7-F-01):BackupService.restore 是
 *   "从工作区内 backups/<id>.zip 恢复"语义,与 status="archived" 无关;
 *   archived 工作区从列表消失自然不出现菜单按钮,无需前端 disabled
 * - 其他 3 项全状态启用
 *
 * 5 个 mutation 内部消化(每行一组 hook),mutation 各自 invalidate
 * ['workspaces'] 触发列表 refetch;UI 副作用(toast + dialog state)
 * 在本组件内消化,WorkspacesPage 不感知(对齐 P1-F-08 决策 5
 * "mutation 副作用归属")。
 */

import * as React from "react";
import { MoreHorizontal } from "lucide-react";

import { ConfirmDialog } from "@/components/ConfirmDialog";
import { PurgeConfirmDialog } from "@/components/PurgeConfirmDialog";
import { RestoreFromBackupDialog } from "@/components/RestoreFromBackupDialog";
import { showApiErrorToast, showSuccessToast } from "@/components/Toast";
import { Button } from "@/components/ui/button";
import {
  DropdownMenu,
  DropdownMenuContent,
  DropdownMenuItem,
  DropdownMenuSeparator,
  DropdownMenuTrigger,
} from "@/components/ui/dropdown-menu";
import {
  useArchiveWorkspaceMutation,
  useBackupWorkspaceMutation,
  useCleanWorkspaceMutation,
  usePurgeWorkspaceMutation,
  useRestoreWorkspaceFromBackupMutation,
  type WorkspaceOut,
} from "@/api/workspaces";

export interface WorkspaceActionsMenuProps {
  workspace: WorkspaceOut;
}

export function WorkspaceActionsMenu({ workspace }: WorkspaceActionsMenuProps) {
  const [archiveOpen, setArchiveOpen] = React.useState(false);
  const [purgeOpen, setPurgeOpen] = React.useState(false);
  const [restoreOpen, setRestoreOpen] = React.useState(false);

  const archiveMutation = useArchiveWorkspaceMutation();
  const cleanMutation = useCleanWorkspaceMutation();
  const purgeMutation = usePurgeWorkspaceMutation();
  const backupMutation = useBackupWorkspaceMutation();
  const restoreMutation = useRestoreWorkspaceFromBackupMutation();

  const isArchived = workspace.status === "archived";
  const wsId = workspace.workspace_id;
  const wsName = workspace.display_name;

  const handleArchive = () => {
    archiveMutation.mutate(wsId, {
      onSuccess: (result) => {
        showSuccessToast(
          "已归档工作区",
          `${wsName} → ${result.archive_path}`,
        );
        setArchiveOpen(false);
      },
      onError: (error) => {
        showApiErrorToast(error);
      },
    });
  };

  const handleClean = () => {
    cleanMutation.mutate(wsId, {
      onSuccess: () => {
        showSuccessToast(
          "已清理运行时数据",
          `${wsName} (${wsId}) 的 chroma / prompts archives / logs 已清空`,
        );
      },
      onError: (error) => {
        showApiErrorToast(error);
      },
    });
  };

  const handlePurge = (confirm: string) => {
    purgeMutation.mutate(
      { workspaceId: wsId, confirm },
      {
        onSuccess: () => {
          showSuccessToast("已彻底删除工作区", `${wsName} (${wsId})`);
          setPurgeOpen(false);
        },
        onError: (error) => {
          showApiErrorToast(error);
        },
      },
    );
  };

  const handleBackup = () => {
    backupMutation.mutate(wsId, {
      onSuccess: (result) => {
        // P6-F-03 字段重命名完整接入(对齐 P6-B-07 `BackupInfoOut` 新字段集):
        // 显示 backup_id + 大小 + schema_version,让维护人在 toast 上对账
        // 是否落到了预期的 backups/<id>.zip 文件。
        const sizeKb = (result.size_bytes / 1024).toFixed(1);
        showSuccessToast(
          "已备份快照",
          `${result.backup_id}(${sizeKb} KB · schema ${result.schema_version})`,
        );
      },
      onError: (error) => {
        showApiErrorToast(error);
      },
    });
  };

  const handleRestoreFromBackup = (payload: {
    backupId: string;
    confirm: string;
  }) => {
    restoreMutation.mutate(
      {
        workspaceId: wsId,
        backupId: payload.backupId,
        confirm: payload.confirm,
      },
      {
        onSuccess: (result) => {
          showSuccessToast(
            "已从备份恢复",
            `${wsName} (${result.workspace_id}) ← ${result.backup_id}(原状态备份 ${result.pre_restore_backup_id})`,
          );
          setRestoreOpen(false);
        },
        onError: (error) => {
          showApiErrorToast(error);
        },
      },
    );
  };

  return (
    <>
      <DropdownMenu>
        <DropdownMenuTrigger asChild>
          <Button
            data-testid="workspace-actions-trigger"
            variant="ghost"
            size="icon"
            aria-label={`${wsName} 操作菜单`}
          >
            <MoreHorizontal className="h-4 w-4" />
          </Button>
        </DropdownMenuTrigger>
        <DropdownMenuContent align="end" className="w-44">
          <DropdownMenuItem
            data-testid="action-archive"
            disabled={isArchived || archiveMutation.isPending}
            onSelect={() => setArchiveOpen(true)}
          >
            归档
          </DropdownMenuItem>
          <DropdownMenuItem
            data-testid="action-clean"
            disabled={cleanMutation.isPending}
            onSelect={handleClean}
          >
            清理运行时数据
          </DropdownMenuItem>
          <DropdownMenuItem
            data-testid="action-backup"
            disabled={backupMutation.isPending}
            onSelect={handleBackup}
          >
            立即备份
          </DropdownMenuItem>
          <DropdownMenuItem
            data-testid="action-restore"
            disabled={restoreMutation.isPending}
            onSelect={() => setRestoreOpen(true)}
          >
            从备份恢复…
          </DropdownMenuItem>
          <DropdownMenuSeparator />
          <DropdownMenuItem
            data-testid="action-purge"
            variant="destructive"
            onSelect={() => setPurgeOpen(true)}
          >
            彻底删除…
          </DropdownMenuItem>
        </DropdownMenuContent>
      </DropdownMenu>

      <ConfirmDialog
        open={archiveOpen}
        onOpenChange={setArchiveOpen}
        title={`归档工作区 ${wsName}?`}
        description="工作区将打包到项目级 archives/<id>.tar.gz 并从可见列表消失。"
        confirmLabel="归档"
        cancelLabel="取消"
        variant="default"
        isConfirmDisabled={archiveMutation.isPending}
        onConfirm={handleArchive}
      />

      <PurgeConfirmDialog
        open={purgeOpen}
        onOpenChange={setPurgeOpen}
        workspaceId={wsId}
        displayName={wsName}
        isPending={purgeMutation.isPending}
        onConfirm={handlePurge}
      />

      <RestoreFromBackupDialog
        open={restoreOpen}
        onOpenChange={setRestoreOpen}
        workspaceId={wsId}
        displayName={wsName}
        isPending={restoreMutation.isPending}
        onConfirm={handleRestoreFromBackup}
      />
    </>
  );
}
