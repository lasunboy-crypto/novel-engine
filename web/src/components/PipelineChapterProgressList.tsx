/**
 * PipelineChapterProgressList(v2 w1-s4 Run 面板逐章进度列表)。
 *
 * 数据源 = `GET /pipeline/status` 的 chapter_ids / completed_chapter_ids /
 * skipped_chapter_ids / current_chapter(run 维度),join
 * `useChapterListQuery`(章维度:标题 + 8 档 status)——
 * RunControlPage 在 chapter_finished WS 事件时已 invalidate
 * `["chapters", workspaceId]`,列表随写作进度自动刷新。
 *
 * 每章一行:run 维度标记(✓ 已完成 / ⏭ 已跳过 / ▶ 当前章 / ○ 待写)+
 * 章号·标题 + `ChapterStateBadge`(章自身状态,复用 ChaptersBoard 习惯)+
 * 直达章节编辑页链接。
 *
 * run 维度标记优先级:completed > skipped > current > pending —— paused /
 * stopped 时 `current_chapter` = "最后处理的章节",可能已在 completed 集合,
 * 此时显示 ✓ 而非 ▶(避免"已完成的章还在写"的误读)。
 *
 * 无 active run(`chapter_ids=[]`)→ 返 null(零垂直占用,对齐
 * EscalationDetailsBanner 模式)。
 */

import { Link } from "react-router-dom";

import { ChapterStateBadge } from "@/components/ChapterStateBadge";
import { useChapterListQuery } from "@/api/chapters";
import type { PipelineStatusOut } from "@/api/pipeline";
import { cn } from "@/lib/utils";

type RunChapterMark = "completed" | "skipped" | "current" | "pending";

const MARK_META: Record<
  RunChapterMark,
  { symbol: string; label: string; className: string }
> = {
  completed: {
    symbol: "✓",
    label: "已完成",
    className: "bg-emerald-100 text-emerald-700",
  },
  skipped: {
    symbol: "⏭",
    label: "已跳过",
    className: "bg-orange-100 text-orange-700",
  },
  current: {
    symbol: "▶",
    label: "当前章",
    className: "bg-sky-100 text-sky-700",
  },
  pending: {
    symbol: "○",
    label: "待写",
    className: "bg-secondary text-muted-foreground",
  },
};

interface PipelineChapterProgressListProps {
  workspaceId: string;
  status: PipelineStatusOut | undefined;
}

export function PipelineChapterProgressList(
  props: PipelineChapterProgressListProps,
) {
  const { workspaceId, status } = props;
  const chapterListQuery = useChapterListQuery(workspaceId);

  const chapterIds = status?.chapter_ids ?? [];
  if (chapterIds.length === 0) {
    return null;
  }

  const completed = new Set(status?.completed_chapter_ids ?? []);
  const skipped = new Set(status?.skipped_chapter_ids ?? []);
  const current = status?.current_chapter ?? null;
  // 章维度信息(标题 + 8 档 status);列表加载中 / 失败时行内降级为只显示 ID。
  const chapterInfo = new Map(
    (chapterListQuery.data ?? []).map((ch) => [ch.chapter_id, ch]),
  );

  const markFor = (chapterId: string): RunChapterMark => {
    if (completed.has(chapterId)) return "completed";
    if (skipped.has(chapterId)) return "skipped";
    if (current === chapterId) return "current";
    return "pending";
  };

  return (
    <section
      data-testid="pipeline-chapter-progress"
      className="space-y-2 rounded border border-border bg-card p-4"
    >
      <h3 className="text-sm font-semibold uppercase tracking-wide text-muted-foreground">
        本次运行章节进度({chapterIds.length} 章)
      </h3>
      <ol className="max-h-80 space-y-1 overflow-y-auto">
        {chapterIds.map((chapterId) => {
          const mark = markFor(chapterId);
          const meta = MARK_META[mark];
          const info = chapterInfo.get(chapterId);
          return (
            <li
              key={chapterId}
              data-testid="pipeline-progress-row"
              data-run-mark={mark}
              className={cn(
                "flex items-center gap-2 rounded border border-border px-2 py-1.5 text-xs",
                mark === "current" && "border-sky-400 bg-sky-50/50",
              )}
            >
              <span
                className={cn(
                  "inline-flex h-5 w-14 items-center justify-center gap-1 rounded text-[10px] font-medium",
                  meta.className,
                )}
                title={meta.label}
              >
                {meta.symbol} {meta.label}
              </span>
              <Link
                to={`/chapters/${encodeURIComponent(chapterId)}`}
                className="flex-1 truncate hover:underline"
                data-testid="pipeline-progress-chapter-link"
              >
                {info ? (
                  <>
                    <span className="text-muted-foreground">
                      第 {info.chapter_number} 章
                    </span>{" "}
                    {info.title}
                  </>
                ) : (
                  <code>{chapterId}</code>
                )}
              </Link>
              <ChapterStateBadge status={info?.status ?? null} />
            </li>
          );
        })}
      </ol>
    </section>
  );
}
