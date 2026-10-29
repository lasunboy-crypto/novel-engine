/**
 * WorkspacesPage(P1-F-07 列表+切换 / P1-F-08 创建表单 / P1-F-09 生命周期动作)。
 *
 * - 列表来源:`useWorkspacesQuery()`(`GET /api/v1/workspaces`)
 * - 切换:`useSwitchWorkspaceMutation()`(`POST /workspaces/{id}:switch`)
 * - 创建:`CreateWorkspaceDialog` + `useCreateWorkspaceMutation`(P1-F-08)
 * - 生命周期 5 动作:每行右侧 `WorkspaceActionsMenu`(P1-F-09)挂 archive /
 *   clean / backup / restore / purge 5 mutation,各自 invalidate 触发列表 refetch
 * - 当前活动工作区:`useActiveWorkspace(workspaces)`(P1-F-10 起 zustand
 *   store + selector,见 `web/src/store/activeWorkspace.ts`)
 *
 * 路由名 `:switch` 而非 `:activate`(NEXT_STEPS / p1_plan 字面已过时,
 * 详见 `web/src/api/workspaces.ts` 模块顶部说明)。
 */

import { useState } from "react";
import { useNavigate } from "react-router-dom";

import { CreateWorkspaceDialog } from "@/components/CreateWorkspaceDialog";
import { showApiErrorToast, showSuccessToast } from "@/components/Toast";
import { Button } from "@/components/ui/button";
import { WorkspaceActionsMenu } from "@/components/WorkspaceActionsMenu";
import { isApiError } from "@/api/errors";
import {
  useSwitchWorkspaceMutation,
  useWorkspacesQuery,
  type WorkspaceOut,
} from "@/api/workspaces";
import {
  useActiveWorkspace,
  useActiveWorkspaceStore,
} from "@/store/activeWorkspace";
import { cn } from "@/lib/utils";

const STATUS_LABEL: Record<WorkspaceOut["status"], string> = {
  active: "活动",
  archived: "已归档",
  completed: "已完结",
};

const STATUS_BADGE_CLASS: Record<WorkspaceOut["status"], string> = {
  active: "bg-emerald-100 text-emerald-700",
  archived: "bg-zinc-100 text-zinc-600",
  completed: "bg-sky-100 text-sky-700",
};

export default function WorkspacesPage() {
  const navigate = useNavigate();
  const query = useWorkspacesQuery();
  const switchMutation = useSwitchWorkspaceMutation();
  const setActiveWorkspaceId = useActiveWorkspaceStore(
    (s) => s.setActiveWorkspaceId,
  );
  const [createOpen, setCreateOpen] = useState(false);

  const activeWorkspace = useActiveWorkspace(query.data);

  // v1.0.x B2-02:切换工作区 onSuccess 同步 zustand store,让 AppShell 顶部
  // nav("大纲 / 运行控制 / 导出"动态链接)在用户离开 /workspaces 列表后仍能
  // 拼出正确的 /w/:workspaceId/* href(否则 storeId 一直 null,所有动态链接
  // fallback 到 /workspaces)。对齐 WorkspaceDetailPage "查看所有章节" 模式。
  const handleSwitch = (workspace: WorkspaceOut) => {
    switchMutation.mutate(workspace.workspace_id, {
      onSuccess: (next) => {
        setActiveWorkspaceId(next.workspace_id);
        showSuccessToast(
          "已切换活动工作区",
          `${next.display_name} (${next.workspace_id})`,
        );
      },
      onError: (error) => {
        showApiErrorToast(error);
      },
    });
  };

  return (
    <div className="space-y-4">
      <header className="flex flex-wrap items-start justify-between gap-3">
        <div className="space-y-1">
          <h1 className="text-2xl font-semibold">工作区</h1>
          <p className="text-sm text-muted-foreground">
            列表来自 <code>GET /api/v1/workspaces</code>;点"切换到此工作区"调
            <code> :switch</code> 端点,带{" "}
            <span className="font-semibold">*</span> 的为当前活动工作区。
          </p>
        </div>
        <div className="flex flex-wrap items-center gap-2">
          <Button
            data-testid="open-new-book"
            onClick={() => navigate("/workspaces/new")}
          >
            新书启动
          </Button>
          <Button
            data-testid="open-create-dialog"
            variant="outline"
            onClick={() => setCreateOpen(true)}
          >
            快速创建(无大纲)
          </Button>
        </div>
      </header>

      <WorkspacesContent
        loading={query.isPending}
        error={query.error}
        workspaces={query.data ?? []}
        activeWorkspaceId={activeWorkspace?.workspace_id ?? null}
        switchingWorkspaceId={
          switchMutation.isPending ? switchMutation.variables ?? null : null
        }
        onSwitch={handleSwitch}
      />

      <CreateWorkspaceDialog open={createOpen} onOpenChange={setCreateOpen} />
    </div>
  );
}

interface WorkspacesContentProps {
  loading: boolean;
  error: unknown;
  workspaces: WorkspaceOut[];
  activeWorkspaceId: string | null;
  switchingWorkspaceId: string | null;
  onSwitch: (workspace: WorkspaceOut) => void;
}

function WorkspacesContent(props: WorkspacesContentProps) {
  if (props.loading) {
    return (
      <div className="rounded border border-border p-6 text-sm text-muted-foreground">
        加载工作区列表中…
      </div>
    );
  }

  if (props.error) {
    const message = isApiError(props.error)
      ? `${props.error.code}: ${props.error.message}`
      : props.error instanceof Error
        ? props.error.message
        : "未知错误";
    return (
      <div className="rounded border border-destructive bg-destructive/10 p-6 text-sm text-destructive">
        加载失败:{message}
      </div>
    );
  }

  if (props.workspaces.length === 0) {
    return (
      <div className="rounded border border-dashed border-border p-6 text-sm text-muted-foreground">
        还没有工作区。点右上角"新建工作区"创建第一个,或用 CLI:
        <code className="ml-1">
          uv run novel workspace create demo --premise "..." --genre scifi
        </code>
      </div>
    );
  }

  return (
    <ul className="space-y-2">
      {props.workspaces.map((workspace) => (
        <WorkspaceRow
          key={workspace.workspace_id}
          workspace={workspace}
          isActive={workspace.workspace_id === props.activeWorkspaceId}
          isSwitching={workspace.workspace_id === props.switchingWorkspaceId}
          onSwitch={() => props.onSwitch(workspace)}
        />
      ))}
    </ul>
  );
}

interface WorkspaceRowProps {
  workspace: WorkspaceOut;
  isActive: boolean;
  isSwitching: boolean;
  onSwitch: () => void;
}

function WorkspaceRow(props: WorkspaceRowProps) {
  const { workspace, isActive, isSwitching, onSwitch } = props;
  const isArchived = workspace.status === "archived";
  return (
    <li
      data-testid="workspace-row"
      data-workspace-id={workspace.workspace_id}
      data-active={isActive ? "true" : "false"}
      data-status={workspace.status}
      className={cn(
        "flex items-center justify-between gap-4 rounded border border-border bg-card p-4",
        isActive && "border-emerald-500/50 bg-emerald-50/40",
        isArchived && "opacity-60",
      )}
    >
      <div className="min-w-0 flex-1 space-y-1">
        <div className="flex flex-wrap items-center gap-x-2 gap-y-1">
          {isActive && (
            <span
              aria-label="当前活动工作区"
              className="text-base font-semibold text-emerald-600"
            >
              *
            </span>
          )}
          <span className="whitespace-nowrap text-base font-medium">
            {workspace.display_name}
          </span>
          <span className="whitespace-nowrap text-xs text-muted-foreground">
            ({workspace.workspace_id})
          </span>
          <span
            className={cn(
              "whitespace-nowrap rounded px-1.5 py-0.5 text-xs font-medium",
              STATUS_BADGE_CLASS[workspace.status],
            )}
          >
            {STATUS_LABEL[workspace.status]}
          </span>
        </div>
        <p className="line-clamp-2 text-sm text-muted-foreground">
          {workspace.genre} · {workspace.premise}
        </p>
        <p className="text-xs text-muted-foreground">
          上次活动 {formatDateTime(workspace.last_active_at)} · 已完成{" "}
          {workspace.completed_chapters}/{workspace.total_chapters} 章
        </p>
      </div>
      <div className="flex shrink-0 items-center gap-1">
        <Button
          variant={isActive ? "secondary" : "default"}
          size="sm"
          disabled={isActive || isSwitching || isArchived}
          onClick={onSwitch}
        >
          {isActive ? "已是活动" : isSwitching ? "切换中…" : "切换到此工作区"}
        </Button>
        <WorkspaceActionsMenu workspace={workspace} />
      </div>
    </li>
  );
}

function formatDateTime(iso: string): string {
  const date = new Date(iso);
  if (Number.isNaN(date.getTime())) return iso;
  return date.toLocaleString("zh-CN", {
    year: "numeric",
    month: "2-digit",
    day: "2-digit",
    hour: "2-digit",
    minute: "2-digit",
  });
}
