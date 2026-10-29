/**
 * WorkspaceDetailPage(P1-F-10):工作区主页/状态看板。
 *
 * 路由:`/workspaces/:id`。展示 4 张卡片:
 *
 * 1. 总章数:`GET /chapters` 返回的全部 `ChapterListItem` 数
 * 2. 已完成:`status === "approved"` 的章节数
 * 3. 进行中:总章数 − 已完成(`not_started` + `unknown` 合计;后端章节状态
 *    枚举见 `chapter_service.py:93`,任务模板"其他状态"指除 approved 外)
 * 4. 最近修改:`last_modified_at` 字典序最大的章节 ID(card 文件 mtime,
 *    P1-B-25 决策 3 近似)
 *
 * 数据来源:
 *
 * - `GET /api/v1/workspaces/{id}`(`useWorkspaceDetailQuery`):工作区元数据
 *   (display_name / status / 卷名等头部展示)
 * - `GET /api/v1/chapters`(`useChapterListQuery(id)`,X-Workspace-Id):
 *   章节列表用于看板分桶
 *
 * "查看所有章节"按钮:把当前 :id 写进 zustand store,然后 navigate
 * `/chapters`,让 ChaptersBoardPage(P1-F-11)读到对应工作区。这是 P1-F-10
 * 第一个真实使用 zustand 的入口(WorkspacesPage `*` 推算只是迁移)。
 */

import { useMemo } from "react";
import { Link, useNavigate, useParams } from "react-router-dom";

import { isApiError } from "@/api/errors";
import {
  useChapterListQuery,
  type ChapterListItem,
} from "@/api/chapters";
import { useWorkspaceDetailQuery } from "@/api/workspaces";
import { HealthScoreBadge } from "@/components/HealthScoreBadge";
import { Button } from "@/components/ui/button";
import { Card, CardContent, CardHeader, CardTitle } from "@/components/ui/card";
import { useActiveWorkspaceStore } from "@/store/activeWorkspace";
import { cn } from "@/lib/utils";

export default function WorkspaceDetailPage() {
  const { id } = useParams<{ id: string }>();
  const navigate = useNavigate();
  const setActiveWorkspaceId = useActiveWorkspaceStore(
    (s) => s.setActiveWorkspaceId,
  );

  const workspaceQuery = useWorkspaceDetailQuery(id);
  const chaptersQuery = useChapterListQuery(id);

  const stats = useMemo(
    () => computeStats(chaptersQuery.data),
    [chaptersQuery.data],
  );

  const handleGoToChapters = () => {
    if (id) {
      setActiveWorkspaceId(id);
    }
    navigate("/chapters");
  };

  if (!id) {
    return (
      <div className="rounded border border-destructive bg-destructive/10 p-6 text-sm text-destructive">
        URL 缺少工作区 ID 参数。
        <Link to="/workspaces" className="ml-2 underline">
          回到工作区列表
        </Link>
      </div>
    );
  }

  return (
    <div className="space-y-4">
      <header className="space-y-2">
        <p className="text-xs text-muted-foreground">
          <Link to="/workspaces" className="underline">
            ← 工作区列表
          </Link>
        </p>
        <div className="flex flex-wrap items-start justify-between gap-3">
          <WorkspaceHeader
            workspaceId={id}
            loading={workspaceQuery.isPending}
            error={workspaceQuery.error}
            workspace={workspaceQuery.data}
          />
          <div
            className="flex flex-wrap items-center gap-2"
            data-testid="workspace-detail-toolbar"
          >
            <HealthScoreBadge workspaceId={id} />
          </div>
        </div>
      </header>

      <StatsBoard
        loading={chaptersQuery.isPending && chaptersQuery.fetchStatus !== "idle"}
        error={chaptersQuery.error}
        stats={stats}
      />

      <div className="flex flex-wrap gap-2">
        <Button data-testid="go-to-chapters" onClick={handleGoToChapters}>
          查看所有章节
        </Button>
      </div>
    </div>
  );
}

interface WorkspaceHeaderProps {
  workspaceId: string;
  loading: boolean;
  error: unknown;
  workspace:
    | import("@/api/workspaces").WorkspaceOut
    | undefined;
}

function WorkspaceHeader(props: WorkspaceHeaderProps) {
  if (props.loading) {
    return (
      <div className="space-y-1">
        <h1 className="text-2xl font-semibold">工作区详情</h1>
        <p className="text-sm text-muted-foreground">
          加载工作区 {props.workspaceId} …
        </p>
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
      <div className="space-y-1">
        <h1 className="text-2xl font-semibold">工作区详情</h1>
        <div className="rounded border border-destructive bg-destructive/10 p-3 text-sm text-destructive">
          加载工作区失败:{message}
        </div>
      </div>
    );
  }
  const ws = props.workspace;
  if (!ws) {
    return (
      <div className="space-y-1">
        <h1 className="text-2xl font-semibold">工作区详情</h1>
        <p className="text-sm text-muted-foreground">未找到工作区。</p>
      </div>
    );
  }
  return (
    <div className="space-y-1">
      <div className="flex flex-wrap items-center gap-x-2 gap-y-1">
        <h1
          className="whitespace-nowrap text-2xl font-semibold"
          data-testid="workspace-display-name"
        >
          {ws.display_name}
        </h1>
        <span className="whitespace-nowrap text-sm text-muted-foreground">
          ({ws.workspace_id})
        </span>
        <span
          className={cn(
            "whitespace-nowrap rounded px-1.5 py-0.5 text-xs font-medium",
            STATUS_BADGE_CLASS[ws.status],
          )}
        >
          {STATUS_LABEL[ws.status]}
        </span>
      </div>
      <p className="text-sm text-muted-foreground">
        {ws.genre} · {ws.premise}
      </p>
    </div>
  );
}

const STATUS_LABEL: Record<
  import("@/api/workspaces").WorkspaceOut["status"],
  string
> = {
  active: "活动",
  archived: "已归档",
  completed: "已完结",
};

const STATUS_BADGE_CLASS: Record<
  import("@/api/workspaces").WorkspaceOut["status"],
  string
> = {
  active: "bg-emerald-100 text-emerald-700",
  archived: "bg-zinc-100 text-zinc-600",
  completed: "bg-sky-100 text-sky-700",
};

interface ChapterStats {
  total: number;
  approved: number;
  inProgress: number;
  lastModifiedChapterId: string | null;
  lastModifiedAt: string | null;
}

function computeStats(
  chapters: readonly ChapterListItem[] | undefined,
): ChapterStats {
  if (!chapters || chapters.length === 0) {
    return {
      total: 0,
      approved: 0,
      inProgress: 0,
      lastModifiedChapterId: null,
      lastModifiedAt: null,
    };
  }
  let approved = 0;
  let latest: ChapterListItem = chapters[0];
  for (const ch of chapters) {
    if (ch.status === "approved") approved += 1;
    if (ch.last_modified_at > latest.last_modified_at) {
      latest = ch;
    }
  }
  return {
    total: chapters.length,
    approved,
    inProgress: chapters.length - approved,
    lastModifiedChapterId: latest.chapter_id,
    lastModifiedAt: latest.last_modified_at,
  };
}

interface StatsBoardProps {
  loading: boolean;
  error: unknown;
  stats: ChapterStats;
}

function StatsBoard(props: StatsBoardProps) {
  if (props.loading) {
    return (
      <div className="rounded border border-border p-6 text-sm text-muted-foreground">
        加载章节列表中…
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
      <div className="rounded border border-destructive bg-destructive/10 p-3 text-sm text-destructive">
        加载章节失败:{message}
      </div>
    );
  }
  const lastModifiedDisplay = props.stats.lastModifiedChapterId
    ? `${props.stats.lastModifiedChapterId}${
        props.stats.lastModifiedAt
          ? ` · ${formatDateTime(props.stats.lastModifiedAt)}`
          : ""
      }`
    : "—";
  return (
    <div
      data-testid="workspace-stats-board"
      className="grid grid-cols-1 gap-3 sm:grid-cols-2 lg:grid-cols-4"
    >
      <StatCard
        testid="stat-total"
        label="总章数"
        value={props.stats.total.toString()}
      />
      <StatCard
        testid="stat-approved"
        label="已完成"
        value={props.stats.approved.toString()}
        emphasis="approved"
      />
      <StatCard
        testid="stat-in-progress"
        label="进行中"
        value={props.stats.inProgress.toString()}
        hint="not_started + unknown"
      />
      <StatCard
        testid="stat-last-modified"
        label="最近修改章节"
        value={lastModifiedDisplay}
        valueClassName="text-base font-medium"
      />
    </div>
  );
}

interface StatCardProps {
  testid: string;
  label: string;
  value: string;
  hint?: string;
  emphasis?: "approved";
  valueClassName?: string;
}

function StatCard(props: StatCardProps) {
  return (
    <Card data-testid={props.testid}>
      <CardHeader className="space-y-0 p-4 pb-2">
        <CardTitle className="text-sm font-medium text-muted-foreground">
          {props.label}
        </CardTitle>
      </CardHeader>
      <CardContent className="p-4 pt-0">
        <div
          className={cn(
            "tabular-nums",
            props.valueClassName ?? "text-2xl font-semibold",
            props.emphasis === "approved" && "text-emerald-600",
          )}
        >
          {props.value}
        </div>
        {props.hint && (
          <p className="mt-1 text-xs text-muted-foreground">{props.hint}</p>
        )}
      </CardContent>
    </Card>
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
