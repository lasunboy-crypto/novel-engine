/**
 * PlotThreadsPage(P2-F-05 / P2-F-06):伏笔列表 + 详情 + 增删改入口。
 *
 * 路由:`/plot-threads`。列出当前活动工作区的全部伏笔。
 *
 * ## UI 结构
 *
 * - 顶栏:标题 + 活动工作区显示(沿用 CharactersPage 模板)+ "新增伏笔" 按钮
 *   (P2-F-06)
 * - 筛选条:`status` 4 档下拉(全部 / planted / developing / resolved /
 *   abandoned)+ `importance` 6 档下拉(全部 / 1-5)+ "重置筛选" 按钮
 * - 进度条:基于**当前筛选后**的列表统计 `actual_payoff != null` 数 / 总数
 * - 表格 7 列(P2-F-06 起加 actions):title / status / planted_chapter /
 *   planned_payoff / actual_payoff / importance / actions
 *
 * ## 状态徽标
 *
 * 4 档状态用 Tailwind 颜色区分(planted/slate, developing/amber,
 * resolved/emerald, abandoned/zinc)。
 *
 * ## 行点击 / actions(P2-F-06)
 *
 * - 行点击 → 打开 `<PlotThreadDetailDrawer>`(只读视图 + "切换编辑")
 * - 行 actions"编辑" → `<PlotThreadFormDialog mode="edit">`
 * - 行 actions"删除" → `<DeletePlotThreadConfirmDialog>` 客户端聚合
 *   `chapter_card.related_plot_threads` 显示引用数(>0 红色警示但仍允许删,
 *   沿用 P2-F-04 §2 自决项 4 套路)
 *
 * ## 筛选下推 / 客户端
 *
 * `status` / `importance` 透传给后端 `GET /threads?status=&importance=`,
 * 后端服务层 in-memory 过滤(P2-B-15 §2 决策 1)。queryKey 把过滤条件嵌进
 * 去,切换筛选不重取已访问过的组合(threads.ts 已实现)。
 */

import * as React from "react";
import { Link } from "react-router-dom";
import { useQueries } from "@tanstack/react-query";

import { CHAPTER_CARD_QUERY_KEY, useChapterListQuery } from "@/api/chapters";
import { apiGet } from "@/api/client";
import { isApiError } from "@/api/errors";
import {
  useDeleteThreadMutation,
  useThreadListQuery,
  type PlotThreadOut,
  type PlotThreadStatus,
} from "@/api/threads";
import type { components } from "@/api/types";
import {
  useWorkspacesQuery,
  type WorkspaceOut,
} from "@/api/workspaces";
import { ConfirmDialog } from "@/components/ConfirmDialog";
import { PlotThreadDetailDrawer } from "@/components/PlotThreadDetailDrawer";
import { PlotThreadFormDialog } from "@/components/PlotThreadFormDialog";
import { showApiErrorToast, showSuccessToast } from "@/components/Toast";
import { Button } from "@/components/ui/button";
import { useActiveWorkspace } from "@/store/activeWorkspace";

type ChapterCardOut = components["schemas"]["ChapterCardOut"];

const STATUS_OPTIONS: ReadonlyArray<{
  value: PlotThreadStatus | "all";
  label: string;
}> = [
  { value: "all", label: "全部状态" },
  { value: "planted", label: "埋下" },
  { value: "developing", label: "发展" },
  { value: "resolved", label: "已兑现" },
  { value: "abandoned", label: "已放弃" },
];

const IMPORTANCE_OPTIONS: ReadonlyArray<{
  value: number | "all";
  label: string;
}> = [
  { value: "all", label: "全部重要性" },
  { value: 1, label: "1(低)" },
  { value: 2, label: "2" },
  { value: 3, label: "3" },
  { value: 4, label: "4" },
  { value: 5, label: "5(高)" },
];

const STATUS_LABEL: Record<PlotThreadStatus, string> = {
  planted: "埋下",
  developing: "发展",
  resolved: "已兑现",
  abandoned: "已放弃",
};

const STATUS_BADGE_CLASS: Record<PlotThreadStatus, string> = {
  planted: "bg-slate-100 text-slate-700 ring-1 ring-slate-200",
  developing: "bg-amber-100 text-amber-700 ring-1 ring-amber-200",
  resolved: "bg-emerald-100 text-emerald-700 ring-1 ring-emerald-200",
  abandoned: "bg-zinc-200 text-zinc-600 ring-1 ring-zinc-300",
};

export default function PlotThreadsPage() {
  const workspacesQuery = useWorkspacesQuery();
  const activeWorkspace = useActiveWorkspace(workspacesQuery.data);
  const workspaceId = activeWorkspace?.workspace_id ?? null;

  const [statusFilter, setStatusFilter] = React.useState<
    PlotThreadStatus | "all"
  >("all");
  const [importanceFilter, setImportanceFilter] = React.useState<
    number | "all"
  >("all");

  const filter = React.useMemo(
    () => ({
      status: statusFilter === "all" ? null : statusFilter,
      importance: importanceFilter === "all" ? null : importanceFilter,
    }),
    [statusFilter, importanceFilter],
  );

  const threadsQuery = useThreadListQuery(workspaceId, filter);

  const [selectedThreadId, setSelectedThreadId] = React.useState<string | null>(
    null,
  );
  const [drawerOpen, setDrawerOpen] = React.useState(false);

  const [createOpen, setCreateOpen] = React.useState(false);
  const [editTarget, setEditTarget] = React.useState<PlotThreadOut | null>(
    null,
  );
  const [deleteTarget, setDeleteTarget] = React.useState<PlotThreadOut | null>(
    null,
  );

  const handleResetFilters = () => {
    setStatusFilter("all");
    setImportanceFilter("all");
  };

  const filtersActive = statusFilter !== "all" || importanceFilter !== "all";

  const handleRowClick = (threadId: string) => {
    setSelectedThreadId(threadId);
    setDrawerOpen(true);
  };

  const handleEditClick = (thread: PlotThreadOut) => {
    setEditTarget(thread);
  };

  const handleDeleteClick = (thread: PlotThreadOut) => {
    setDeleteTarget(thread);
  };

  const handleEditFromDrawer = (thread: PlotThreadOut) => {
    setDrawerOpen(false);
    setEditTarget(thread);
  };

  const onDeleteConfirmed = () => {
    if (deleteTarget && selectedThreadId === deleteTarget.thread_id) {
      setDrawerOpen(false);
      setSelectedThreadId(null);
    }
  };

  return (
    <div className="space-y-4">
      <header className="flex items-start justify-between gap-4">
        <div className="space-y-1">
          <h1 className="text-2xl font-semibold">伏笔</h1>
          <PlotThreadsHeaderHint
            workspacesLoading={workspacesQuery.isPending}
            workspacesError={workspacesQuery.error}
            activeWorkspace={activeWorkspace}
          />
        </div>
        <Button
          onClick={() => setCreateOpen(true)}
          disabled={!workspaceId}
          data-testid="plot-thread-create-button"
        >
          新增伏笔
        </Button>
      </header>

      {workspaceId && (
        <FilterBar
          statusFilter={statusFilter}
          importanceFilter={importanceFilter}
          onStatusChange={setStatusFilter}
          onImportanceChange={setImportanceFilter}
          onReset={handleResetFilters}
          filtersActive={filtersActive}
        />
      )}

      {workspaceId && threadsQuery.data && (
        <PayoffProgress threads={threadsQuery.data} />
      )}

      <PlotThreadsContent
        workspacesLoading={workspacesQuery.isPending}
        workspacesError={workspacesQuery.error}
        workspacesEmpty={
          !workspacesQuery.isPending &&
          (workspacesQuery.data?.length ?? 0) === 0
        }
        activeWorkspace={activeWorkspace}
        threadsLoading={
          threadsQuery.isPending && threadsQuery.fetchStatus !== "idle"
        }
        threadsError={threadsQuery.error}
        threads={threadsQuery.data ?? []}
        filtersActive={filtersActive}
        onRowClick={handleRowClick}
        onEditClick={handleEditClick}
        onDeleteClick={handleDeleteClick}
      />

      <PlotThreadDetailDrawer
        open={drawerOpen}
        onOpenChange={setDrawerOpen}
        workspaceId={workspaceId}
        threadId={selectedThreadId}
        onEditRequest={handleEditFromDrawer}
      />

      {workspaceId && createOpen && (
        <PlotThreadFormDialog
          open={createOpen}
          onOpenChange={setCreateOpen}
          workspaceId={workspaceId}
          mode="create"
        />
      )}

      {workspaceId && editTarget && (
        <PlotThreadFormDialog
          open={editTarget !== null}
          onOpenChange={(next) => {
            if (!next) setEditTarget(null);
          }}
          workspaceId={workspaceId}
          mode="edit"
          thread={editTarget}
        />
      )}

      {workspaceId && deleteTarget && (
        <DeletePlotThreadConfirmDialog
          workspaceId={workspaceId}
          target={deleteTarget}
          onClose={() => setDeleteTarget(null)}
          onDeleted={onDeleteConfirmed}
        />
      )}
    </div>
  );
}

interface PlotThreadsHeaderHintProps {
  workspacesLoading: boolean;
  workspacesError: unknown;
  activeWorkspace: WorkspaceOut | null;
}

function PlotThreadsHeaderHint(props: PlotThreadsHeaderHintProps) {
  if (props.workspacesLoading || props.workspacesError) {
    return null;
  }
  if (!props.activeWorkspace) {
    return (
      <p className="text-sm text-muted-foreground">当前没有活动工作区。</p>
    );
  }
  return (
    <p className="text-sm text-muted-foreground">
      当前活动工作区:
      <span
        className="ml-1 font-medium text-foreground"
        data-testid="active-workspace-display-name"
      >
        {props.activeWorkspace.display_name}
      </span>
      <span className="ml-1 text-xs">
        ({props.activeWorkspace.workspace_id})
      </span>
    </p>
  );
}

interface FilterBarProps {
  statusFilter: PlotThreadStatus | "all";
  importanceFilter: number | "all";
  onStatusChange: (next: PlotThreadStatus | "all") => void;
  onImportanceChange: (next: number | "all") => void;
  onReset: () => void;
  filtersActive: boolean;
}

function FilterBar(props: FilterBarProps) {
  return (
    <div
      className="flex flex-wrap items-center gap-3 rounded border border-border bg-muted/20 p-3"
      data-testid="plot-threads-filter-bar"
    >
      <label className="flex items-center gap-1 text-sm">
        <span className="text-muted-foreground">状态</span>
        <select
          data-testid="plot-thread-status-filter"
          className="rounded border border-border bg-background px-2 py-1 text-sm focus:outline-none focus:ring-1 focus:ring-ring"
          value={props.statusFilter}
          onChange={(event) =>
            props.onStatusChange(
              event.target.value as PlotThreadStatus | "all",
            )
          }
        >
          {STATUS_OPTIONS.map((opt) => (
            <option key={opt.value} value={opt.value}>
              {opt.label}
            </option>
          ))}
        </select>
      </label>

      <label className="flex items-center gap-1 text-sm">
        <span className="text-muted-foreground">重要性</span>
        <select
          data-testid="plot-thread-importance-filter"
          className="rounded border border-border bg-background px-2 py-1 text-sm focus:outline-none focus:ring-1 focus:ring-ring"
          value={String(props.importanceFilter)}
          onChange={(event) => {
            const raw = event.target.value;
            if (raw === "all") {
              props.onImportanceChange("all");
            } else {
              const next = Number.parseInt(raw, 10);
              if (Number.isInteger(next) && next >= 1 && next <= 5) {
                props.onImportanceChange(next);
              }
            }
          }}
        >
          {IMPORTANCE_OPTIONS.map((opt) => (
            <option key={opt.value} value={String(opt.value)}>
              {opt.label}
            </option>
          ))}
        </select>
      </label>

      {props.filtersActive && (
        <button
          type="button"
          data-testid="plot-thread-reset-filter"
          onClick={props.onReset}
          className="text-sm text-muted-foreground underline-offset-2 hover:text-foreground hover:underline"
        >
          重置筛选
        </button>
      )}
    </div>
  );
}

interface PayoffProgressProps {
  threads: readonly PlotThreadOut[];
}

function PayoffProgress(props: PayoffProgressProps) {
  const total = props.threads.length;
  const paidOff = props.threads.filter(
    (t) => t.actual_payoff !== null && t.actual_payoff !== undefined,
  ).length;
  const pending = total - paidOff;
  const ratio = total === 0 ? 0 : Math.round((paidOff / total) * 100);

  return (
    <div
      data-testid="plot-thread-progress"
      className="rounded border border-border bg-background p-3"
    >
      <div className="flex items-baseline justify-between text-sm">
        <span className="font-medium">兑现进度</span>
        <span className="text-muted-foreground">
          已兑现{" "}
          <span
            className="font-semibold text-foreground"
            data-testid="plot-thread-progress-paid"
          >
            {paidOff}
          </span>
          {" / 未兑现 "}
          <span
            className="font-semibold text-foreground"
            data-testid="plot-thread-progress-pending"
          >
            {pending}
          </span>
          {" / 总计 "}
          <span
            className="font-semibold text-foreground"
            data-testid="plot-thread-progress-total"
          >
            {total}
          </span>
          {total > 0 && (
            <span className="ml-2" data-testid="plot-thread-progress-ratio">
              ({ratio}%)
            </span>
          )}
        </span>
      </div>
      <div className="mt-2 h-2 overflow-hidden rounded-full bg-muted">
        <div
          className="h-full bg-emerald-500 transition-[width]"
          style={{ width: `${ratio}%` }}
        />
      </div>
    </div>
  );
}

interface PlotThreadsContentProps {
  workspacesLoading: boolean;
  workspacesError: unknown;
  workspacesEmpty: boolean;
  activeWorkspace: WorkspaceOut | null;
  threadsLoading: boolean;
  threadsError: unknown;
  threads: readonly PlotThreadOut[];
  filtersActive: boolean;
  onRowClick: (threadId: string) => void;
  onEditClick: (thread: PlotThreadOut) => void;
  onDeleteClick: (thread: PlotThreadOut) => void;
}

function PlotThreadsContent(props: PlotThreadsContentProps) {
  if (props.workspacesLoading) {
    return (
      <div className="rounded border border-border p-6 text-sm text-muted-foreground">
        加载工作区列表中…
      </div>
    );
  }
  if (props.workspacesError) {
    return (
      <div
        data-testid="plot-threads-workspaces-error"
        className="rounded border border-destructive bg-destructive/10 p-6 text-sm text-destructive"
      >
        加载工作区失败:{formatError(props.workspacesError)}
      </div>
    );
  }
  if (props.workspacesEmpty) {
    return (
      <div className="rounded border border-dashed border-border p-6 text-sm text-muted-foreground">
        还没有工作区。请先到
        <Link to="/workspaces" className="mx-1 underline">
          工作区列表
        </Link>
        创建一个。
      </div>
    );
  }
  if (!props.activeWorkspace) {
    return (
      <div className="rounded border border-dashed border-border p-6 text-sm text-muted-foreground">
        当前没有活动工作区。请到
        <Link to="/workspaces" className="mx-1 underline">
          工作区列表
        </Link>
        切换到一个工作区。
      </div>
    );
  }
  if (props.threadsLoading) {
    return (
      <div className="rounded border border-border p-6 text-sm text-muted-foreground">
        加载伏笔列表中…
      </div>
    );
  }
  if (props.threadsError) {
    return (
      <div
        data-testid="plot-threads-list-error"
        className="rounded border border-destructive bg-destructive/10 p-6 text-sm text-destructive"
      >
        加载伏笔失败:{formatError(props.threadsError)}
      </div>
    );
  }
  if (props.threads.length === 0) {
    return (
      <div
        data-testid="plot-threads-empty"
        className="rounded border border-dashed border-border p-6 text-sm text-muted-foreground"
      >
        {props.filtersActive
          ? "当前筛选条件下没有伏笔。试试重置筛选。"
          : "还没有伏笔。点页顶“新增伏笔”创建,或用 novel workspace import-assets CLI 批量导入(P2-B-13)。"}
      </div>
    );
  }

  return (
    <div className="overflow-hidden rounded border border-border">
      <table className="w-full text-sm" data-testid="plot-threads-table">
        <thead className="bg-muted/40 text-left text-xs uppercase tracking-wide text-muted-foreground">
          <tr>
            <th className="px-4 py-2 font-medium">标题</th>
            <th className="px-4 py-2 font-medium">状态</th>
            <th className="px-4 py-2 font-medium">埋伏章</th>
            <th className="px-4 py-2 font-medium">计划兑现章</th>
            <th className="px-4 py-2 font-medium">实际兑现章</th>
            <th className="px-4 py-2 font-medium text-right">重要性</th>
            <th className="px-4 py-2 font-medium text-right">操作</th>
          </tr>
        </thead>
        <tbody>
          {props.threads.map((thread) => (
            <PlotThreadRow
              key={thread.thread_id}
              thread={thread}
              onClick={props.onRowClick}
              onEdit={props.onEditClick}
              onDelete={props.onDeleteClick}
            />
          ))}
        </tbody>
      </table>
    </div>
  );
}

interface PlotThreadRowProps {
  thread: PlotThreadOut;
  onClick: (threadId: string) => void;
  onEdit: (thread: PlotThreadOut) => void;
  onDelete: (thread: PlotThreadOut) => void;
}

function PlotThreadRow(props: PlotThreadRowProps) {
  const { thread, onClick, onEdit, onDelete } = props;
  const handleKeyDown = (event: React.KeyboardEvent<HTMLTableRowElement>) => {
    if (event.key === "Enter" || event.key === " ") {
      event.preventDefault();
      onClick(thread.thread_id);
    }
  };
  return (
    <tr
      data-testid="plot-thread-row"
      data-thread-id={thread.thread_id}
      tabIndex={0}
      role="button"
      onClick={() => onClick(thread.thread_id)}
      onKeyDown={handleKeyDown}
      className="cursor-pointer border-t border-border transition-colors hover:bg-emerald-50/30 focus:outline-none focus:ring-2 focus:ring-ring"
    >
      <td className="px-4 py-2">
        <div className="font-medium" data-testid="plot-thread-title">
          {thread.title}
        </div>
        <div className="text-xs text-muted-foreground">{thread.thread_id}</div>
      </td>
      <td className="px-4 py-2" data-testid="plot-thread-status">
        <span
          className={`inline-flex items-center rounded px-2 py-0.5 text-xs font-medium ${STATUS_BADGE_CLASS[thread.status]}`}
        >
          {STATUS_LABEL[thread.status]}
        </span>
      </td>
      <td
        className="px-4 py-2 text-muted-foreground"
        data-testid="plot-thread-planted-chapter"
      >
        {thread.planted_chapter || "—"}
      </td>
      <td
        className="px-4 py-2 text-muted-foreground"
        data-testid="plot-thread-planned-payoff"
      >
        {thread.planned_payoff ?? "—"}
      </td>
      <td
        className="px-4 py-2 text-muted-foreground"
        data-testid="plot-thread-actual-payoff"
      >
        {thread.actual_payoff ?? "—"}
      </td>
      <td
        className="px-4 py-2 text-right font-medium"
        data-testid="plot-thread-importance"
      >
        {thread.importance}
      </td>
      <td className="px-4 py-2 text-right">
        <div className="inline-flex gap-2">
          <Button
            type="button"
            variant="outline"
            size="sm"
            data-testid="plot-thread-edit"
            onClick={(event) => {
              event.stopPropagation();
              onEdit(thread);
            }}
          >
            编辑
          </Button>
          <Button
            type="button"
            variant="outline"
            size="sm"
            data-testid="plot-thread-delete"
            onClick={(event) => {
              event.stopPropagation();
              onDelete(thread);
            }}
          >
            删除
          </Button>
        </div>
      </td>
    </tr>
  );
}

interface DeletePlotThreadConfirmDialogProps {
  workspaceId: string;
  target: PlotThreadOut;
  onClose: () => void;
  onDeleted: () => void;
}

/**
 * 删除前确认对话框。打开瞬间触发 `useQueries` 批量拿全工作区的 ChapterCard,
 * 统计 `related_plot_threads` 含目标 thread_id 的章节数量(P1-B-19 决策 4 字段
 * 已存在)。引用计数策略沿用 P2-F-04 §2 自决项 4。
 */
function DeletePlotThreadConfirmDialog(
  props: DeletePlotThreadConfirmDialogProps,
) {
  const { workspaceId, target, onClose, onDeleted } = props;
  const chaptersQuery = useChapterListQuery(workspaceId);
  const chapterIds = React.useMemo(
    () => (chaptersQuery.data ?? []).map((c) => c.chapter_id),
    [chaptersQuery.data],
  );

  const cardsQueries = useQueries({
    queries: chapterIds.map((chapterId) => ({
      queryKey: CHAPTER_CARD_QUERY_KEY(workspaceId, chapterId),
      queryFn: () =>
        apiGet<ChapterCardOut>(
          `/chapters/${encodeURIComponent(chapterId)}/card`,
          { workspaceId },
        ),
      enabled: chapterIds.length > 0,
    })),
  });

  const isCounting =
    chaptersQuery.isPending ||
    cardsQueries.some((q) => q.isPending && q.fetchStatus !== "idle");

  const referencingChapters = React.useMemo(() => {
    const out: string[] = [];
    cardsQueries.forEach((q) => {
      const card = q.data;
      if (card && card.related_plot_threads.includes(target.thread_id)) {
        out.push(card.chapter_id);
      }
    });
    return out;
  }, [cardsQueries, target.thread_id]);

  const deleteMutation = useDeleteThreadMutation();

  const handleConfirm = () => {
    deleteMutation.mutate(
      { workspaceId, threadId: target.thread_id },
      {
        onSuccess: () => {
          showSuccessToast(
            "已删除",
            `伏笔 ${target.title}(${target.thread_id})已删除`,
          );
          onDeleted();
          onClose();
        },
        onError: (err) => showApiErrorToast(err),
      },
    );
  };

  const description = (
    <span data-testid="plot-thread-delete-reference-count">
      即将删除伏笔 <strong>{target.title}</strong>(
      <code>{target.thread_id}</code>)。
      {isCounting ? (
        <span className="block pt-2 text-muted-foreground">统计引用中…</span>
      ) : referencingChapters.length === 0 ? (
        <span className="block pt-2 text-muted-foreground">
          该伏笔暂未被任何章节引用,删除安全。
        </span>
      ) : (
        <span className="block pt-2 font-medium text-destructive">
          ⚠ 该伏笔被 {referencingChapters.length} 章引用(
          {referencingChapters.slice(0, 3).join("、")}
          {referencingChapters.length > 3 ? " 等" : ""}
          )。删除后这些章节卡的 related_plot_threads 字段将留下悬空 ID。
        </span>
      )}
    </span>
  );

  return (
    <ConfirmDialog
      open
      onOpenChange={(next) => {
        if (!next) onClose();
      }}
      title="确认删除伏笔"
      description={description}
      confirmLabel={deleteMutation.isPending ? "删除中…" : "删除"}
      cancelLabel="取消"
      variant="destructive"
      onConfirm={handleConfirm}
      isConfirmDisabled={deleteMutation.isPending}
    />
  );
}

function formatError(error: unknown): string {
  if (isApiError(error)) {
    return `${error.code}: ${error.message}`;
  }
  if (error instanceof Error) {
    return error.message;
  }
  return "未知错误";
}
