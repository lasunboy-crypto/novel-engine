/**
 * ChaptersBoardPage(P1-F-11 列表;P4-F-05 升级为状态看板;P6-F-06 扩
 * `invalidated` 列 + unblock 按钮 + 顶部 unblock-all 按钮)。
 *
 * 路由:`/chapters`。列出当前活动工作区的所有章节,**P4-F-05 改为 grid
 * 看板视图**(对齐 FRONTEND_SPEC §4.7 字面"看板视图:列 = 状态、
 * 卡片 = 章节")。同时为每个 chapter 订阅 WS `chapter.{id}.events`,收到
 * `state_transition` 即 invalidate 章节列表 query 实时刷新卡片位置。
 *
 * ## 8 档列(P6-B-01 扩 `invalidated` 后对齐 ChapterStateBadge)
 *
 * drafting / reviewing / rewriting / revising / approved / published /
 * human_intervention / invalidated。后端若返 8 档外字符串(防御新档)→ 归
 * "其他"兜底桶,不丢失展示。
 *
 * ## P6-F-06:invalidated 章节恢复 UI
 *
 * - 每个 invalidated 章节卡片底部加"恢复(unblock)"小按钮 → `:unblock` 端点
 * - 顶部行加"全部恢复(unblock-all)"按钮(只在有 invalidated 章节时显)
 * - 二者均不弹二次确认(单卡 unblock 是低破坏性操作 — 不动 chroma / SQLite,
 *   只翻 status invalidated → drafting;批量 unblock-all 同理,且空状态幂等)
 *
 * ## 看板卡片字段
 *
 * P4-F-05 范围内卡片简化为 `第 N 章 · 标题 · chapter_id · 最新版本`(对齐
 * P1-F-11 既有列表字段子集),状态徽标隐式由所在列承载;点击 → `/chapters/{id}`
 * 进 ChapterEditorPage(P1-F-12)不变。
 *
 * ## 实时刷新(WS state_transition)
 *
 * 每个 chapter 挂一个 `<ChapterEventSubscriber>` 隐藏组件(useEffect 调
 * `useChapterEvents(chapterId, { onStateTransition })`),收到事件触发
 * `queryClient.invalidateQueries(CHAPTERS_QUERY_KEY(workspaceId))` 重拉列表。
 * 不在 hook 里做 setQueryData 优化(列表项还含 latest_version_id / has_final /
 * last_modified_at 等不在事件里的字段,refetch 是最简最稳的方案)。
 *
 * ## 活动工作区双路推算(沿用 P1-F-10 决策 5)
 *
 * 1. zustand `activeWorkspaceId` 命中 → 取该工作区
 * 2. store null → fallback 到 `useActiveWorkspace` 的 `last_active_at` max 推算
 */

import { Link } from "react-router-dom";
import { useQueryClient } from "@tanstack/react-query";

import {
  CHAPTERS_QUERY_KEY,
  useChapterListQuery,
  useUnblockAllChaptersMutation,
  useUnblockChapterMutation,
  type ChapterListItem,
} from "@/api/chapters";
import { isApiError } from "@/api/errors";
import {
  useWorkspacesQuery,
  type WorkspaceOut,
} from "@/api/workspaces";
import { Button } from "@/components/ui/button";
import { showApiErrorToast, showSuccessToast } from "@/components/Toast";
import { useActiveWorkspace } from "@/store/activeWorkspace";
import { useChapterEvents } from "@/hooks/useChapterEvents";

interface BoardColumn {
  status: string;
  label: string;
  /** 列头胶囊样式(对齐 ChapterStateBadge 颜色)。 */
  headerClass: string;
}

const BOARD_COLUMNS: readonly BoardColumn[] = [
  { status: "drafting", label: "起草中", headerClass: "bg-sky-100 text-sky-700" },
  { status: "reviewing", label: "审稿中", headerClass: "bg-amber-100 text-amber-700" },
  { status: "rewriting", label: "重写中", headerClass: "bg-indigo-100 text-indigo-700" },
  { status: "revising", label: "修订中", headerClass: "bg-violet-100 text-violet-700" },
  { status: "approved", label: "已通过", headerClass: "bg-emerald-100 text-emerald-700" },
  // B-04 hot-fix(2026-05-19):v1.0 体检报告 §B-04 — "published" 列纯占位,
  // 全代码库无 caller 触发 publish action,Export 不翻 chapter 状态。占位删
  // 避免 UI 误导用户以为有"发布"概念。后端 PUBLISHED state / "publish"
  // transition / "published" CHECK 字面留观推 v2(真正定义"发布"概念时启用),
  // 删后端要动 6+ 文件 + Alembic + 7-8 测试(超出 audit "30 分钟"估算)。
  { status: "human_intervention", label: "人工介入", headerClass: "bg-rose-100 text-rose-700" },
  // P6-B-01 / P6-F-06:rewind 上游章节后被标废的下游章节,需 :unblock 才能恢复 :write
  { status: "invalidated", label: "已失效", headerClass: "bg-orange-100 text-orange-700" },
];

const FALLBACK_COLUMN: BoardColumn = {
  status: "__other__",
  label: "其他",
  headerClass: "bg-zinc-100 text-zinc-600",
};

const KNOWN_STATUSES = new Set(BOARD_COLUMNS.map((c) => c.status));

export default function ChaptersBoardPage() {
  const workspacesQuery = useWorkspacesQuery();
  const activeWorkspace = useActiveWorkspace(workspacesQuery.data);
  const chaptersQuery = useChapterListQuery(activeWorkspace?.workspace_id);

  return (
    <div className="space-y-4">
      <header className="space-y-1">
        <h1 className="text-2xl font-semibold">章节看板</h1>
        <ChaptersHeaderHint
          workspacesLoading={workspacesQuery.isPending}
          workspacesError={workspacesQuery.error}
          activeWorkspace={activeWorkspace}
        />
      </header>

      <ChaptersContent
        workspacesLoading={workspacesQuery.isPending}
        workspacesError={workspacesQuery.error}
        workspacesEmpty={
          !workspacesQuery.isPending &&
          (workspacesQuery.data?.length ?? 0) === 0
        }
        activeWorkspace={activeWorkspace}
        chaptersLoading={
          chaptersQuery.isPending && chaptersQuery.fetchStatus !== "idle"
        }
        chaptersError={chaptersQuery.error}
        chapters={chaptersQuery.data ?? []}
      />
    </div>
  );
}

interface ChaptersHeaderHintProps {
  workspacesLoading: boolean;
  workspacesError: unknown;
  activeWorkspace: WorkspaceOut | null;
}

function ChaptersHeaderHint(props: ChaptersHeaderHintProps) {
  if (props.workspacesLoading || props.workspacesError) {
    return null;
  }
  if (!props.activeWorkspace) {
    return (
      <p className="text-sm text-muted-foreground">
        当前没有活动工作区。
      </p>
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
      <span className="ml-2 text-xs">
        · 看板视图(WS state_transition 实时刷新)
      </span>
    </p>
  );
}

interface ChaptersContentProps {
  workspacesLoading: boolean;
  workspacesError: unknown;
  workspacesEmpty: boolean;
  activeWorkspace: WorkspaceOut | null;
  chaptersLoading: boolean;
  chaptersError: unknown;
  chapters: readonly ChapterListItem[];
}

function ChaptersContent(props: ChaptersContentProps) {
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
        data-testid="chapters-workspaces-error"
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

  if (props.chaptersLoading) {
    return (
      <div className="rounded border border-border p-6 text-sm text-muted-foreground">
        加载章节列表中…
      </div>
    );
  }

  if (props.chaptersError) {
    return (
      <div
        data-testid="chapters-list-error"
        className="rounded border border-destructive bg-destructive/10 p-6 text-sm text-destructive"
      >
        加载章节失败:{formatError(props.chaptersError)}
      </div>
    );
  }

  if (props.chapters.length === 0) {
    return (
      <div
        data-testid="chapters-empty"
        className="rounded border border-dashed border-border p-6 text-sm text-muted-foreground"
      >
        还没有章节卡。把 ChapterCard JSON 放到
        <code className="mx-1">
          workspaces/{props.activeWorkspace.workspace_id}
          /data/cards/&lt;chapter_id&gt;.json
        </code>
        ,刷新本页即可看到列表。P1 阶段前端无导入按钮,留待后续阶段。
      </div>
    );
  }

  return (
    <ChaptersBoard
      workspaceId={props.activeWorkspace.workspace_id}
      chapters={props.chapters}
    />
  );
}

interface ChaptersBoardProps {
  workspaceId: string;
  chapters: readonly ChapterListItem[];
}

function ChaptersBoard(props: ChaptersBoardProps) {
  const queryClient = useQueryClient();
  const unblockAllMutation = useUnblockAllChaptersMutation();
  const unblockMutation = useUnblockChapterMutation();
  // WS state_transition 实时刷新:任一章节 transition 都 invalidate 列表 query。
  function handleTransition() {
    queryClient.invalidateQueries({
      queryKey: CHAPTERS_QUERY_KEY(props.workspaceId),
    });
  }
  const workspaceId = props.workspaceId;

  const buckets = bucketByStatus(props.chapters);
  const fallbackBucket = buckets.get(FALLBACK_COLUMN.status) ?? [];
  const showFallbackColumn = fallbackBucket.length > 0;
  const columns: readonly BoardColumn[] = showFallbackColumn
    ? [...BOARD_COLUMNS, FALLBACK_COLUMN]
    : BOARD_COLUMNS;

  // P6-F-06:数 invalidated 章节,决定顶部 "全部恢复" 按钮显隐。
  const invalidatedCount = buckets.get("invalidated")?.length ?? 0;
  const hasInvalidated = invalidatedCount > 0;

  const handleUnblockAll = async () => {
    try {
      const result = await unblockAllMutation.mutateAsync({ workspaceId });
      showSuccessToast(
        "已批量恢复 invalidated 章节",
        `unblocked ${result.unblocked_count} 章` +
          (result.unblocked_chapter_ids.length > 0
            ? `:${result.unblocked_chapter_ids.join(", ")}`
            : ""),
      );
    } catch (err) {
      showApiErrorToast(err);
    }
  };

  const handleUnblockOne = async (chapterId: string) => {
    try {
      const result = await unblockMutation.mutateAsync({
        workspaceId,
        chapterId,
      });
      showSuccessToast(
        "已恢复章节",
        `${chapterId}: ${result.from_status} → ${result.to_status}`,
      );
    } catch (err) {
      showApiErrorToast(err);
    }
  };

  return (
    <>
      {/* 隐藏的事件订阅器:每章一个。useEffect 内部挂订阅,unmount 自动取消。 */}
      {props.chapters.map((c) => (
        <ChapterEventSubscriber
          key={`evt-${c.chapter_id}`}
          chapterId={c.chapter_id}
          onTransition={handleTransition}
        />
      ))}
      {hasInvalidated && (
        <div
          data-testid="chapters-board-unblock-all-bar"
          className="flex items-center justify-between rounded border border-orange-300 bg-orange-50 px-3 py-2 text-sm"
        >
          <p className="text-orange-800">
            当前有 <strong>{invalidatedCount}</strong> 个 invalidated 章节(上游
            rewind 触发),需 unblock 才能继续 :write / :rewrite。
          </p>
          <Button
            size="sm"
            variant="outline"
            onClick={handleUnblockAll}
            disabled={unblockAllMutation.isPending}
            data-testid="chapters-board-unblock-all-btn"
          >
            {unblockAllMutation.isPending ? "恢复中…" : "全部恢复(unblock-all)"}
          </Button>
        </div>
      )}
      <div
        data-testid="chapters-board"
        className="grid grid-cols-1 gap-3 md:grid-cols-2 lg:grid-cols-4 xl:grid-cols-7"
      >
        {columns.map((col) => {
          const items = buckets.get(col.status) ?? [];
          return (
            <BoardColumnView
              key={col.status}
              column={col}
              items={items}
              workspaceId={workspaceId}
              onUnblockOne={handleUnblockOne}
              isUnblockingChapterId={
                unblockMutation.isPending
                  ? unblockMutation.variables?.chapterId ?? null
                  : null
              }
            />
          );
        })}
      </div>
    </>
  );
}

interface BoardColumnViewProps {
  column: BoardColumn;
  items: readonly ChapterListItem[];
  workspaceId: string;
  /** P6-F-06:invalidated 列内卡片触发 :unblock 端点(父级 mutation 已 wired)。 */
  onUnblockOne: (chapterId: string) => void;
  /** P6-F-06:当前正在 unblock 的 chapter_id(用于禁用其按钮);null = 无。 */
  isUnblockingChapterId: string | null;
}

function BoardColumnView(props: BoardColumnViewProps) {
  return (
    <div
      data-testid="chapters-board-column"
      data-status={props.column.status}
      className="flex min-h-[8rem] flex-col rounded-lg border border-border bg-muted/30 p-2"
    >
      <div className="mb-2 flex items-center justify-between gap-2">
        <span
          className={`rounded px-1.5 py-0.5 text-xs font-medium ${props.column.headerClass}`}
        >
          {props.column.label}
        </span>
        <span
          className="text-xs text-muted-foreground tabular-nums"
          data-testid="chapters-board-column-count"
        >
          {props.items.length}
        </span>
      </div>
      <div className="flex-1 space-y-2">
        {props.items.length === 0 ? (
          <p className="text-center text-xs text-muted-foreground/70">—</p>
        ) : (
          props.items.map((chapter) => (
            <BoardChapterCard
              key={chapter.chapter_id}
              chapter={chapter}
              workspaceId={props.workspaceId}
              onUnblockOne={props.onUnblockOne}
              isUnblocking={
                props.isUnblockingChapterId === chapter.chapter_id
              }
            />
          ))
        )}
      </div>
    </div>
  );
}

interface BoardChapterCardProps {
  chapter: ChapterListItem;
  workspaceId: string;
  /** P6-F-06:invalidated 章节卡 unblock 按钮回调。 */
  onUnblockOne: (chapterId: string) => void;
  /** P6-F-06:本卡片 unblock mutation pending(只对 invalidated 卡有意义)。 */
  isUnblocking: boolean;
}

function BoardChapterCard(props: BoardChapterCardProps) {
  const { chapter, workspaceId } = props;
  // P5-F-06:加"审核章节卡"次要链接。`ChapterListItem` 无 `card_status` 字段
  // (类 A 处置:plan 字面"card_status != approved 时显" vs 工程实情 — 全显,
  // 不依赖 list 端点暴露 card_status;进 review 页后看实际 card_status 决定
  // 下一步动作)。
  const reviewHref = `/w/${encodeURIComponent(workspaceId)}/cards/${encodeURIComponent(chapter.chapter_id)}/review`;
  return (
    <div
      data-testid="chapter-row"
      data-chapter-id={chapter.chapter_id}
      data-status={chapter.status}
      className="rounded border border-border bg-card p-2 text-xs transition-colors hover:border-emerald-500/50 hover:bg-emerald-50/30"
    >
      <Link to={`/chapters/${encodeURIComponent(chapter.chapter_id)}`}>
        <div className="flex items-baseline gap-1">
          <span
            className="whitespace-nowrap font-medium text-muted-foreground tabular-nums"
            data-testid="chapter-number"
          >
            第 {chapter.chapter_number} 章
          </span>
          <span
            className="truncate font-medium text-foreground"
            data-testid="chapter-title"
            title={chapter.title}
          >
            {chapter.title}
          </span>
        </div>
        <p className="mt-1 text-[10px] text-muted-foreground">
          <span className="font-mono">{chapter.chapter_id}</span>
          <span className="mx-1">·</span>
          <span data-testid="chapter-latest-version">
            {formatLatestVersion(chapter)}
          </span>
        </p>
      </Link>
      <div className="mt-1.5 flex items-center justify-between gap-2 border-t border-border/60 pt-1">
        <Link
          to={reviewHref}
          data-testid="chapter-row-review-link"
          className="text-[10px] text-sky-700 underline-offset-2 hover:underline"
        >
          审核章节卡 →
        </Link>
        {chapter.status === "invalidated" && (
          <button
            type="button"
            onClick={(e) => {
              // 防止冒泡到外层 Link (chapter editor link)
              e.preventDefault();
              e.stopPropagation();
              props.onUnblockOne(chapter.chapter_id);
            }}
            disabled={props.isUnblocking}
            data-testid="chapter-row-unblock-btn"
            className="rounded border border-orange-300 bg-orange-50 px-1.5 py-0.5 text-[10px] font-medium text-orange-800 hover:bg-orange-100 disabled:opacity-50"
            title="恢复(unblock)— 翻回 drafting,可继续 :write / :rewrite"
          >
            {props.isUnblocking ? "恢复中…" : "恢复 ↻"}
          </button>
        )}
      </div>
    </div>
  );
}

interface ChapterEventSubscriberProps {
  chapterId: string;
  onTransition: () => void;
}

function ChapterEventSubscriber(props: ChapterEventSubscriberProps) {
  useChapterEvents(props.chapterId, {
    onStateTransition: props.onTransition,
  });
  return null;
}

function bucketByStatus(
  chapters: readonly ChapterListItem[],
): Map<string, ChapterListItem[]> {
  const map = new Map<string, ChapterListItem[]>();
  for (const c of chapters) {
    const key = KNOWN_STATUSES.has(c.status) ? c.status : FALLBACK_COLUMN.status;
    const arr = map.get(key);
    if (arr) {
      arr.push(c);
    } else {
      map.set(key, [c]);
    }
  }
  // 每列内部按 chapter_number 升序(后端已按章序排,这里再保一道,bucket 不打乱)。
  for (const [, arr] of map) {
    arr.sort((a, b) => a.chapter_number - b.chapter_number);
  }
  return map;
}

function formatLatestVersion(chapter: ChapterListItem): string {
  if (chapter.has_final && chapter.latest_version_id !== null) {
    return `final (v${chapter.latest_version_id})`;
  }
  if (chapter.has_final) {
    return "final";
  }
  if (chapter.latest_version_id !== null) {
    return `v${chapter.latest_version_id}`;
  }
  return "—";
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
