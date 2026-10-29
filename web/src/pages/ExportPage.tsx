/**
 * ExportPage(P6-F-03)。
 *
 * 路由 `/w/:workspaceId/export`(对齐 FRONTEND_SPEC §1 路由清单 + §4.14
 * "ExportPage(导出)"全部字面要点):
 *
 * 1. 格式 radio 选 md / chaptered_md / epub / docx(4 档)
 * 2. 范围 radio 选 full / volume / range + 字段自动显隐
 * 3. 选项 checkbox include_metadata / include_review_history
 * 4. "立即导出"按钮 → POST `/exports` 202 + `{task_id, export_id}`
 * 5. WS `task.{id}.progress` 阶段进度条(collecting / rendering / packaging /
 *    done)
 * 6. 历史导出列表(最近 10 次)+ 重新下载按钮 + 状态徽章
 * 7. 页面底部归档动作组(归档 / 清理 / 彻底删除 3 按钮 + 详细说明每个操作的影响)
 *
 * 页面 onTaskDone 回调:invalidate `["exports", workspaceId]`,让历史列表
 * 看到 status 从 running 翻 done + chapter_count / file_size_bytes 填充。
 *
 * 归档动作组用既有 4 lifecycle mutation:
 * - `useArchiveWorkspaceMutation`(可逆,二次 ConfirmDialog)
 * - `useCleanWorkspaceMutation`(清运行时数据,无需二次确认)
 * - `usePurgeWorkspaceMutation`(不可逆,PurgeConfirmDialog 要求输入 workspace_id)
 * - `useBackupWorkspaceMutation`(纯添加,无副作用)
 *
 * 4 个 mutation 已在 WorkspaceActionsMenu 用过,本页面只是另一个挂载点,
 * 用同样 toast / dialog 模式;不复用组件本身(那是为 dropdown 设计的,这里是
 * 整页底部按钮组,直接平铺 4 个按钮 + 内联 Dialog 更直观)。
 */

import { useEffect, useState } from "react";
import { useParams } from "react-router-dom";
import { useQueryClient } from "@tanstack/react-query";

import {
  useCreateExportMutation,
  EXPORTS_QUERY_KEY,
  type ExportCreateIn,
} from "@/api/exports";
import {
  useArchiveWorkspaceMutation,
  useBackupWorkspaceMutation,
  useCleanWorkspaceMutation,
  useDestroyWorkspaceMutation,
  usePurgeWorkspaceBackupsMutation,
  usePurgeWorkspaceMutation,
  useRestoreWorkspaceFromBackupMutation,
  useWorkspaceDetailQuery,
} from "@/api/workspaces";
import { ConfirmDialog } from "@/components/ConfirmDialog";
import { DestroyConfirmDialog } from "@/components/DestroyConfirmDialog";
import { ExportForm } from "@/components/ExportForm";
import { ExportHistoryList } from "@/components/ExportHistoryList";
import { PurgeConfirmDialog } from "@/components/PurgeConfirmDialog";
import { RestoreFromBackupDialog } from "@/components/RestoreFromBackupDialog";
import { showApiErrorToast, showSuccessToast } from "@/components/Toast";
import { Button } from "@/components/ui/button";
import { useTaskProgress } from "@/hooks/useTaskProgress";
import { useActiveWorkspaceStore } from "@/store/activeWorkspace";

/** WS task.{id}.progress 4 stage 字面对齐 P6-B-04 ExportService(self_marker)。 */
type ExportStage =
  | "collecting"
  | "rendering"
  | "packaging"
  | "done"
  | "failed";

const STAGE_LABELS: Record<ExportStage, string> = {
  collecting: "收集章节",
  rendering: "渲染格式",
  packaging: "打包 zip",
  done: "完成",
  failed: "失败",
};

// 进度 stage 顺序数组(用于 ProgressBar 计算"已完成 X / 总 Y"格)。
const STAGE_ORDER: ReadonlyArray<ExportStage> = [
  "collecting",
  "rendering",
  "packaging",
  "done",
];

interface ExportProgressState {
  taskId: string;
  exportId: string;
  stage: ExportStage | null;
  error: string | null;
}

export default function ExportPage() {
  const { workspaceId } = useParams<{ workspaceId: string }>();
  const queryClient = useQueryClient();
  const setActiveWorkspaceId = useActiveWorkspaceStore(
    (s) => s.setActiveWorkspaceId,
  );

  useEffect(() => {
    if (workspaceId) setActiveWorkspaceId(workspaceId);
  }, [workspaceId, setActiveWorkspaceId]);

  const detailQuery = useWorkspaceDetailQuery(workspaceId);
  const createMutation = useCreateExportMutation();

  // 当前正在进行的导出任务 — 只跟踪 1 个(单工作区单工作流,串行)。
  const [progress, setProgress] = useState<ExportProgressState | null>(null);

  // v1.0.x reviewer-resilience(2026-05-20):问题5 修法 — 改用本地 state
  // 控制"提交中..."文案 / 按钮 disabled。B2-05 的 createMutation.reset() 路径
  // 在 React 18 dev StrictMode 双 mount 下偶发不翻 isPending,导致按钮卡死
  // (luo2 实测命中)。本地 state 完全脱离 react-query 内部状态机时序,
  // handleSubmit 入口 set true、onSuccess/onError 末尾 set false,刚性可控。
  const [isSubmitting, setIsSubmitting] = useState(false);

  // v1.0.x B2-05:WS done event 时序竞态兜底 polling — 在 progress.taskId
  // 出现且 stage 未 done/failed 时,每 2s 主动 invalidate 历史列表 query,
  // 让"运行中"能 ≤ 2s 翻"已完成"(而不是必须刷新页面)。done/failed 时
  // 清 interval。WS event 正常到达时 onMessage 内的 invalidate 仍正常工作,
  // 此 polling 是 fail-safe 不替代 WS。
  useEffect(() => {
    if (!workspaceId) return;
    const taskId = progress?.taskId;
    const stage = progress?.stage;
    if (!taskId || stage === "done" || stage === "failed") return;
    const intervalId = setInterval(() => {
      void queryClient.invalidateQueries({
        queryKey: EXPORTS_QUERY_KEY(workspaceId),
      });
    }, 2000);
    return () => clearInterval(intervalId);
  }, [workspaceId, progress?.taskId, progress?.stage, queryClient]);

  // 6 lifecycle mutation(归档动作组,P7-F-01 起含 restore-from-backup,
  // P7-B-07 起含 destroy 三方全删)
  const archiveMutation = useArchiveWorkspaceMutation();
  const cleanMutation = useCleanWorkspaceMutation();
  const purgeMutation = usePurgeWorkspaceMutation();
  const backupMutation = useBackupWorkspaceMutation();
  const restoreMutation = useRestoreWorkspaceFromBackupMutation();
  const destroyMutation = useDestroyWorkspaceMutation();
  const purgeBackupsMutation = usePurgeWorkspaceBackupsMutation();
  const [archiveOpen, setArchiveOpen] = useState(false);
  const [purgeOpen, setPurgeOpen] = useState(false);
  const [restoreOpen, setRestoreOpen] = useState(false);
  const [destroyOpen, setDestroyOpen] = useState(false);
  const [purgeBackupsOpen, setPurgeBackupsOpen] = useState(false);

  // 订阅 task.{id}.progress(taskId 为 null 时 hook 内部短路)
  useTaskProgress<{ stage?: ExportStage; error?: string }>(progress?.taskId, {
    onMessage: (frame) => {
      const data = frame.data ?? {};
      const stage = (data.stage as ExportStage | undefined) ?? null;
      const err = (data.error as string | undefined) ?? null;

      if (frame.event === "done") {
        setProgress((prev) =>
          prev ? { ...prev, stage: "done", error: null } : prev,
        );
        showSuccessToast("导出完成", "可在历史列表点击「重新下载」获取 zip。");
        void queryClient.invalidateQueries({
          queryKey: EXPORTS_QUERY_KEY(workspaceId ?? ""),
        });
        return;
      }
      if (frame.event === "error" || frame.event === "failed") {
        setProgress((prev) =>
          prev ? { ...prev, stage: "failed", error: err } : prev,
        );
        void queryClient.invalidateQueries({
          queryKey: EXPORTS_QUERY_KEY(workspaceId ?? ""),
        });
        return;
      }
      // stage 增量(collecting / rendering / packaging)
      if (stage) {
        setProgress((prev) =>
          prev ? { ...prev, stage, error: null } : prev,
        );
      }
    },
  });

  if (!workspaceId) {
    return (
      <div className="rounded border border-destructive bg-destructive/10 p-4 text-sm text-destructive">
        路由参数 workspaceId 缺失。请从工作区列表进入导出页。
      </div>
    );
  }

  const workspace = detailQuery.data;
  const wsName = workspace?.display_name ?? workspaceId;
  const wsStatus = workspace?.status;

  // v1.0.x reviewer-resilience(2026-05-20):问题5 — 改用本地 isSubmitting
  // state(详 useState 声明处注释)。B2-05 引入的 createMutation.reset()
  // 路径删除,本地 state 入口 set true、出口 set false 刚性翻新。
  // ② polling fallback useEffect 见下方保留 — 防 WS done event 因订阅时序
  // 竞态(后端 BackgroundTasks 几秒内跑完 publish stage event,前端
  // useTaskProgress useEffect subscribe 慢一拍错过)导致历史列表"运行中"不翻
  // "已完成"。
  const handleSubmit = (body: ExportCreateIn) => {
    setProgress(null);
    setIsSubmitting(true);
    createMutation.mutate(
      { workspaceId, body },
      {
        onSuccess: (result) => {
          setProgress({
            taskId: result.task_id,
            exportId: result.export_id,
            stage: null,
            error: null,
          });
          showSuccessToast(
            "导出任务已提交",
            `task_id: ${result.task_id} · export_id: ${result.export_id}`,
          );
          void queryClient.invalidateQueries({
            queryKey: EXPORTS_QUERY_KEY(workspaceId),
          });
          setIsSubmitting(false);
        },
        onError: (err) => {
          showApiErrorToast(err);
          setIsSubmitting(false);
        },
      },
    );
  };

  const handleArchive = () => {
    archiveMutation.mutate(workspaceId, {
      onSuccess: (result) => {
        showSuccessToast(
          "已归档工作区",
          `${wsName} → ${result.archive_path}`,
        );
        setArchiveOpen(false);
      },
      onError: (err) => showApiErrorToast(err),
    });
  };

  const handleClean = () => {
    cleanMutation.mutate(workspaceId, {
      onSuccess: () => {
        showSuccessToast(
          "已清理运行时数据",
          `${wsName}(${workspaceId})的 chroma / prompts archives / logs 已清空`,
        );
      },
      onError: (err) => showApiErrorToast(err),
    });
  };

  const handlePurge = (confirm: string) => {
    purgeMutation.mutate(
      { workspaceId, confirm },
      {
        onSuccess: () => {
          showSuccessToast(
            "已彻底删除工作区",
            `${wsName}(${workspaceId})`,
          );
          setPurgeOpen(false);
        },
        onError: (err) => showApiErrorToast(err),
      },
    );
  };

  const handlePurgeBackups = (confirm: string) => {
    purgeBackupsMutation.mutate(
      { workspaceId, confirm },
      {
        onSuccess: (result) => {
          const freedKb = (result.freed_bytes / 1024).toFixed(1);
          showSuccessToast(
            "已清空全部备份",
            `删除 ${result.deleted_count} 份 · 释放 ${freedKb} KB`,
          );
          setPurgeBackupsOpen(false);
        },
        onError: (err) => showApiErrorToast(err),
      },
    );
  };

  const handleBackup = () => {
    backupMutation.mutate(workspaceId, {
      onSuccess: (result) => {
        const sizeKb = (result.size_bytes / 1024).toFixed(1);
        showSuccessToast(
          "已手动备份快照",
          `${result.backup_id}(${sizeKb} KB · schema ${result.schema_version})`,
        );
      },
      onError: (err) => showApiErrorToast(err),
    });
  };

  const handleRestoreFromBackup = (payload: {
    backupId: string;
    confirm: string;
  }) => {
    restoreMutation.mutate(
      { workspaceId, backupId: payload.backupId, confirm: payload.confirm },
      {
        onSuccess: (result) => {
          showSuccessToast(
            "已从备份恢复",
            `${wsName}(${result.workspace_id}) ← ${result.backup_id}(原状态备份 ${result.pre_restore_backup_id})`,
          );
          setRestoreOpen(false);
        },
        onError: (err) => showApiErrorToast(err),
      },
    );
  };

  const handleDestroy = (confirm: string) => {
    destroyMutation.mutate(
      { workspaceId, confirm },
      {
        onSuccess: (result) => {
          showSuccessToast(
            "已完全销毁工作区",
            `${wsName}(${result.workspace_id}) — 已删除 ${result.deleted_paths.length} 项`,
          );
          setDestroyOpen(false);
        },
        onError: (err) => showApiErrorToast(err),
      },
    );
  };

  return (
    <div className="space-y-6" data-testid="export-page">
      <header className="space-y-1">
        <h1 className="text-2xl font-semibold">导出</h1>
        <p className="text-sm text-muted-foreground">
          导出工作区 <code>{workspaceId}</code> 为 Markdown / EPUB / Word 等
          可读格式;任务在后台异步运行,完成后在历史列表可下载。
        </p>
      </header>

      <section className="grid grid-cols-1 gap-6 lg:grid-cols-2">
        <div className="space-y-4">
          <h2 className="text-lg font-medium">新建导出</h2>
          <ExportForm
            workspaceId={workspaceId}
            onSubmit={handleSubmit}
            submitting={isSubmitting}
          />
          {progress && (
            <ProgressPanel
              progress={progress}
              onDismiss={() => setProgress(null)}
            />
          )}
        </div>
        <div className="space-y-4">
          <div className="flex items-baseline justify-between">
            <h2 className="text-lg font-medium">历史导出(最近 10 次)</h2>
            <button
              type="button"
              onClick={() =>
                queryClient.invalidateQueries({
                  queryKey: EXPORTS_QUERY_KEY(workspaceId),
                })
              }
              className="text-xs text-muted-foreground underline-offset-2 hover:underline"
              data-testid="export-refresh-history"
            >
              刷新
            </button>
          </div>
          <ExportHistoryList workspaceId={workspaceId} />
        </div>
      </section>

      {/* 页面底部归档动作组(对齐 FRONTEND_SPEC §4.14 字面 3 按钮 + 详细说明) */}
      <section
        className="space-y-3 border-t border-border pt-6"
        data-testid="lifecycle-actions"
      >
        <h2 className="text-lg font-medium">工作区归档与清理</h2>
        <p className="text-sm text-muted-foreground">
          以下操作影响工作区可见性与磁盘数据,谨慎使用。备份为纯添加操作,
          可通过「从备份恢复」选择历史快照覆盖当前工作区;清理只删运行时
          副产物;归档把工作区打包到项目级 archives 并从可见列表消失;
          彻底删除不可逆但保留备份;完全销毁三方全删完全不可逆(适用写完
          一本书清磁盘场景),后两者均需输入工作区 ID 二次确认。
        </p>

        <div className="grid grid-cols-1 gap-3 md:grid-cols-2">
          <ActionCard
            title="立即手动备份"
            description="把当前工作区全量打包到 backups/manual_<ts>.zip(db + outline + cards + volumes + assets + chapters + meta.json),不影响原目录。可在备份列表回滚。"
            buttonLabel={backupMutation.isPending ? "备份中…" : "立即备份"}
            onClick={handleBackup}
            disabled={backupMutation.isPending}
            testId="lifecycle-backup"
          />
          <ActionCard
            title="从备份恢复"
            description="从工作区内 backups/<id>.zip 选择历史快照覆盖当前工作区(自动 + 手动备份,按时间倒序)。恢复前会自动把当前状态打包为 pre_restore_<ts>.zip 备份。要求输入工作区 ID 二次确认。"
            buttonLabel="从备份恢复…"
            onClick={() => setRestoreOpen(true)}
            disabled={restoreMutation.isPending}
            testId="lifecycle-restore"
          />
          <ActionCard
            title="清空全部备份"
            description="删除 backups/ 下全部备份 zip(自动 + 手动 + pre_restore),释放磁盘。自动备份只保留最近 30 份,手动备份永不自动清 —— 本操作一键全清。不影响正文 / 大纲 / 人设 / db。清空后无法 restore 历史快照,要求输入工作区 ID 二次确认。"
            buttonLabel="清空备份…"
            onClick={() => setPurgeBackupsOpen(true)}
            disabled={purgeBackupsMutation.isPending}
            variant="destructive"
            testId="lifecycle-purge-backups"
          />
          <ActionCard
            title="归档工作区"
            description="工作区打包到项目级 archives/<id>.tar.gz 并从可见列表消失;不可在前端列表二次操作,故建议归档前先确认不会立即用。"
            buttonLabel={archiveMutation.isPending ? "归档中…" : "归档…"}
            onClick={() => setArchiveOpen(true)}
            disabled={archiveMutation.isPending || wsStatus === "archived"}
            disabledHint={
              wsStatus === "archived" ? "工作区已是归档状态" : undefined
            }
            testId="lifecycle-archive"
          />
          <ActionCard
            title="清理运行时数据"
            description="删除 data/chroma/、工作区内 archives/(prompts 归档)、logs/ 等运行时副产物;保留正文 / 大纲 / 人设 / SQLite db / backups/。语义是清副产物,不影响主要数据。"
            buttonLabel={cleanMutation.isPending ? "清理中…" : "清理副产物"}
            onClick={handleClean}
            disabled={cleanMutation.isPending}
            testId="lifecycle-clean"
          />
          <ActionCard
            title="彻底删除工作区"
            description="物理删除工作区目录,所有章节、版本、运行时数据一并销毁;不动顶级 archives 与工作区内 backups(可作历史回滚)。此操作不可撤销,要求输入工作区 ID 二次确认。"
            buttonLabel="彻底删除…"
            onClick={() => setPurgeOpen(true)}
            disabled={purgeMutation.isPending}
            variant="destructive"
            testId="lifecycle-purge"
          />
          <ActionCard
            title="完全销毁工作区(全删)"
            description="物理删除工作区目录 + 工作区内 backups/ + 顶级 archives/<id>.tar.gz,三方全清磁盘。所有章节、备份、归档一并销毁,完全不可逆(对比 purge 仅删工作区目录保留 backups)。要求输入工作区 ID 二次确认。适用场景:写完一本书后清磁盘空间。"
            buttonLabel="完全销毁…"
            onClick={() => setDestroyOpen(true)}
            disabled={destroyMutation.isPending}
            variant="destructive"
            testId="lifecycle-destroy"
          />
        </div>
      </section>

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
        workspaceId={workspaceId}
        displayName={wsName}
        isPending={purgeMutation.isPending}
        onConfirm={handlePurge}
      />

      <PurgeConfirmDialog
        open={purgeBackupsOpen}
        onOpenChange={setPurgeBackupsOpen}
        workspaceId={workspaceId}
        displayName={wsName}
        isPending={purgeBackupsMutation.isPending}
        onConfirm={handlePurgeBackups}
        title="确认清空全部备份"
        description="此操作不可撤销。工作区 backups/ 下全部备份 zip(自动 + 手动 + pre_restore)将被物理删除,正文 / 大纲 / 人设 / db 不动。清空后无法 restore 历史快照。"
        confirmLabel="清空备份"
        pendingLabel="清空中…"
        testIdPrefix="purge-backups"
      />

      <RestoreFromBackupDialog
        open={restoreOpen}
        onOpenChange={setRestoreOpen}
        workspaceId={workspaceId}
        displayName={wsName}
        isPending={restoreMutation.isPending}
        onConfirm={handleRestoreFromBackup}
      />

      <DestroyConfirmDialog
        open={destroyOpen}
        onOpenChange={setDestroyOpen}
        workspaceId={workspaceId}
        displayName={wsName}
        isPending={destroyMutation.isPending}
        onConfirm={handleDestroy}
      />
    </div>
  );
}

interface ProgressPanelProps {
  progress: ExportProgressState;
  onDismiss: () => void;
}

function ProgressPanel({ progress, onDismiss }: ProgressPanelProps) {
  const stage = progress.stage;
  const isDone = stage === "done";
  const isFailed = stage === "failed";
  const index = stage && stage !== "failed" ? STAGE_ORDER.indexOf(stage) : -1;
  const total = STAGE_ORDER.length;
  const fillRatio = isFailed ? 1 : Math.max(0, (index + 1)) / total;
  return (
    <div
      className="space-y-2 rounded border border-border bg-muted/40 p-3"
      data-testid="export-progress"
    >
      <div className="flex items-baseline justify-between">
        <span className="text-sm font-medium">
          当前任务:<code>{progress.taskId}</code>
        </span>
        <button
          type="button"
          onClick={onDismiss}
          className="text-xs text-muted-foreground underline-offset-2 hover:underline"
        >
          关闭
        </button>
      </div>
      <div
        className="h-2 w-full overflow-hidden rounded bg-muted"
        aria-label="导出进度"
      >
        <div
          className={`h-full transition-all ${
            isFailed
              ? "bg-destructive"
              : isDone
                ? "bg-green-500"
                : "bg-primary"
          }`}
          style={{ width: `${Math.round(fillRatio * 100)}%` }}
        />
      </div>
      <div className="flex items-center gap-2 text-xs">
        <span data-testid="export-progress-stage">
          阶段:
          {stage ? STAGE_LABELS[stage] : "等待 WS 第一帧…"}
        </span>
        {!isDone && !isFailed && stage && (
          <span className="text-muted-foreground">
            {index + 1}/{total}
          </span>
        )}
      </div>
      {progress.error && (
        <p
          role="alert"
          className="rounded border border-destructive bg-destructive/10 px-2 py-1 text-xs text-destructive"
          data-testid="export-progress-error"
        >
          {progress.error}
        </p>
      )}
    </div>
  );
}

interface ActionCardProps {
  title: string;
  description: string;
  buttonLabel: string;
  onClick: () => void;
  disabled?: boolean;
  disabledHint?: string;
  variant?: "default" | "destructive";
  testId: string;
}

function ActionCard({
  title,
  description,
  buttonLabel,
  onClick,
  disabled,
  disabledHint,
  variant = "default",
  testId,
}: ActionCardProps) {
  return (
    <div className="space-y-2 rounded border border-border p-3">
      <h3 className="text-sm font-medium">{title}</h3>
      <p className="text-xs text-muted-foreground">{description}</p>
      <Button
        type="button"
        variant={variant}
        size="sm"
        onClick={onClick}
        disabled={disabled}
        title={disabled ? disabledHint : undefined}
        data-testid={testId}
      >
        {buttonLabel}
      </Button>
    </div>
  );
}
