/**
 * PipelineSkippedSummary(v2 W3.5 UI 缺口4):Run 视图"本次跳过 N 章待处理"汇总条。
 *
 * 背景:failure_policy=skip 时,生成中失败的章被静默跳过(`skipped_chapter_ids`)。
 * 逐章进度列表(PipelineChapterProgressList)虽含跳过标记,但混在全部章里易被忽略 →
 * 本组件把跳过的章**单独、显眼**列成待办,每条链到该章编辑页走缺口3 章级闭环
 * (改正文→提交后重审 / 强制通过)。
 *
 * 无跳过章(`skipped_chapter_ids` 空)→ 返 null(零垂直占用,对齐
 * EscalationDetailsBanner / PipelineChapterProgressList 模式)。
 */

import { Link } from "react-router-dom";

import { ChapterStateBadge } from "@/components/ChapterStateBadge";
import { useChapterListQuery } from "@/api/chapters";
import type { PipelineStatusOut } from "@/api/pipeline";

interface PipelineSkippedSummaryProps {
  workspaceId: string;
  status: PipelineStatusOut | undefined;
}

export function PipelineSkippedSummary(props: PipelineSkippedSummaryProps) {
  const { workspaceId, status } = props;
  const chapterListQuery = useChapterListQuery(workspaceId);

  const skippedIds = status?.skipped_chapter_ids ?? [];
  if (skippedIds.length === 0) {
    return null;
  }

  const chapterInfo = new Map(
    (chapterListQuery.data ?? []).map((ch) => [ch.chapter_id, ch]),
  );

  return (
    <section
      data-testid="pipeline-skipped-summary"
      className="space-y-2 rounded border border-orange-500/50 bg-orange-500/10 p-4 text-sm"
    >
      <div className="flex items-center gap-2 font-medium text-orange-700 dark:text-orange-400">
        <span>⏭ 本次跳过 {skippedIds.length} 章待处理</span>
      </div>
      <p className="text-xs text-muted-foreground">
        这些章在生成中失败、按失败策略被跳过。点进每章修正(改正文→提交后重审)或
        强制通过后,本书才算完整。
      </p>
      <ul className="space-y-1" data-testid="pipeline-skipped-list">
        {skippedIds.map((chapterId) => {
          const info = chapterInfo.get(chapterId);
          return (
            <li
              key={chapterId}
              data-testid="pipeline-skipped-row"
              className="flex items-center gap-2 rounded border border-border bg-card px-2 py-1.5 text-xs"
            >
              <Link
                to={`/chapters/${encodeURIComponent(chapterId)}`}
                className="flex-1 truncate font-medium hover:underline"
                data-testid="pipeline-skipped-chapter-link"
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
              <span className="text-orange-600 dark:text-orange-400">
                去处理 →
              </span>
            </li>
          );
        })}
      </ul>
    </section>
  );
}
