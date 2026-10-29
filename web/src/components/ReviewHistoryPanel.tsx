/**
 * ReviewHistoryPanel(B-01 hot-fix 2026-05-19 重写)
 *
 * v1.0 体检报告 §B-01 修法:从 ``review_results`` 表查持久化历史(替代
 * v1 期前端走 zustand `useChapterTaskId` + `useTaskInfoQuery` 链路 ——
 * 后者依赖进程内 task_registry LRU,服务器重启 / 页面刷新都会丢)。
 *
 * 数据流(本期)
 * ==============
 *
 * 1. `useChapterReviewHistoryQuery(workspaceId, chapterId)` 调
 *    `GET /chapters/{chapter_id}/review-history`,后端按 `reviewed_at`
 *    升序返 `ReviewSummaryEntry[]`(对齐 task_registry 模式但走 SQLite 真源)
 * 2. **空数组**(工作区 / 章节不存在 / 无审稿记录)→ 显示"暂无审稿历史"hint
 *    + 修订文案说明"`:write` / `:rewrite` 跑完会持久化到 review_results 表
 *    (服务器重启不丢)"
 * 3. **非空** → 渲染条目列表,按 ReviewSummaryEntry 字段走 verdict 颜色档
 *    + revise_targets 内嵌列表(与原实现一致)
 * 4. error → 通用红色 hint(无 TASK_NOT_FOUND 特殊路径 — API 永远 200 返
 *    空列表,无任务过期语义)
 *
 * 选中状态 / 上抛 summary 给父级
 * ==================================
 *
 * 与 P1-F-14 设计一致:`selectedIndex` / `onSelectIndex` / `onSummaryChange`
 * 三 prop 由父级 (ChapterEditorPage) 持有,选中变化触发 ChapterActionsToolbar
 * "修订"按钮启用 / 入参映射。本组件无变化。
 */

import { useEffect } from "react";

import {
  useChapterReviewHistoryQuery,
  type ReviewSummaryEntry,
} from "@/api/chapters";
import { isApiError } from "@/api/errors";
import { ExcellenceMetrics } from "@/components/ExcellenceMetrics";
import type { components } from "@/api/types";

type ReviewIssueOut = components["schemas"]["ReviewIssueOut"];

interface ReviewHistoryPanelProps {
  workspaceId: string | null | undefined;
  chapterId: string | null | undefined;
  /** 父级持有的选中 review_summary 索引;null = 未选中 */
  selectedIndex: number | null;
  /** 父级 setter,接收 index(再次点同一条传 null toggle off) */
  onSelectIndex: (index: number | null) => void;
  /** 上抛当前 review_summary 数组(供 ChapterActionsToolbar 推算 priorRewriteCount);无审稿时传 null */
  onSummaryChange: (summary: ReviewSummaryEntry[] | null) => void;
}

export function ReviewHistoryPanel(props: ReviewHistoryPanelProps) {
  const query = useChapterReviewHistoryQuery(props.workspaceId, props.chapterId);

  return (
    <section
      data-testid="review-history-panel"
      className="flex h-full min-w-0 flex-col rounded border border-border bg-card"
    >
      <header className="border-b border-border px-4 py-2">
        <h2 className="text-sm font-medium">审稿历史</h2>
      </header>
      <PanelBody
        loading={query.isPending && query.fetchStatus !== "idle"}
        error={query.error}
        summary={query.data ?? null}
        selectedIndex={props.selectedIndex}
        onSelectIndex={props.onSelectIndex}
        onSummaryChange={props.onSummaryChange}
      />
    </section>
  );
}

interface PanelBodyProps {
  loading: boolean;
  error: unknown;
  summary: ReviewSummaryEntry[] | null;
  selectedIndex: number | null;
  onSelectIndex: (index: number | null) => void;
  onSummaryChange: (summary: ReviewSummaryEntry[] | null) => void;
}

function PanelBody(props: PanelBodyProps) {
  // hoist summary 给父级。空数组(后端无 review)→ 传 null,沿用 onSummaryChange
  // 既有契约;TanStack Query 同 key 缓存 → 数组引用稳定,useEffect 不抖动。
  const summaryForParent: ReviewSummaryEntry[] | null =
    props.summary && props.summary.length > 0 ? props.summary : null;
  useEffect(() => {
    props.onSummaryChange(summaryForParent);
    // eslint-disable-next-line react-hooks/exhaustive-deps
  }, [summaryForParent]);

  if (props.loading) {
    return (
      <div className="p-4 text-sm text-muted-foreground">加载审稿历史中…</div>
    );
  }
  if (props.error) {
    return <ErrorHint error={props.error} />;
  }
  if (!props.summary || props.summary.length === 0) {
    return (
      <div
        data-testid="review-history-empty"
        className="space-y-2 p-4 text-sm text-muted-foreground"
      >
        <p>暂无审稿历史。</p>
        <p className="text-xs">
          点击页顶工具栏的「开始写作」/「重写」触发任务,完成后
          <code className="mx-1">review_results</code>表持久化结果会显示在此。
          B-01 hot-fix 起,审稿历史跨服务器重启 / 页面刷新均稳定保留(替代
          v1 期 task_registry LRU 字典)。
        </p>
      </div>
    );
  }

  return (
    <ul
      data-testid="review-history-list"
      className="flex-1 space-y-3 overflow-auto p-4"
    >
      {props.summary.map((entry, index) => (
        <ReviewSummaryItem
          key={`${entry.version_reviewed}-${index}`}
          entry={entry}
          index={index}
          selected={props.selectedIndex === index}
          onSelect={() =>
            props.onSelectIndex(
              props.selectedIndex === index ? null : index,
            )
          }
        />
      ))}
    </ul>
  );
}

interface ReviewSummaryItemProps {
  entry: ReviewSummaryEntry;
  index: number;
  selected: boolean;
  onSelect: () => void;
}

function ReviewSummaryItem(props: ReviewSummaryItemProps) {
  const { entry } = props;
  const stateClass = props.selected
    ? "border-primary bg-primary/5 ring-1 ring-primary"
    : "border-border bg-background hover:bg-muted/50";
  return (
    <li>
      <button
        type="button"
        data-testid="review-summary-item"
        data-selected={props.selected ? "true" : "false"}
        aria-pressed={props.selected}
        onClick={props.onSelect}
        className={`w-full space-y-2 rounded border p-3 text-left text-xs transition-colors ${stateClass}`}
      >
        <div className="flex flex-wrap items-baseline gap-x-2 gap-y-1">
          <span className="font-medium text-foreground">
            第 {props.index + 1} 次审稿
          </span>
          <span className="tabular-nums text-muted-foreground">
            v{entry.version_reviewed}
          </span>
          <VerdictBadge verdict={entry.verdict} />
          <span className="tabular-nums text-muted-foreground">
            score {entry.score_overall.toFixed(2)}
          </span>
          {entry.revise_targets.length > 0 ? (
            <span className="tabular-nums text-muted-foreground">
              · {entry.revise_targets.length} 条 issue
            </span>
          ) : null}
        </div>
        <ExcellenceMetrics review={entry} />
        {(entry.hard_constraint_regressions ?? []).length > 0 ? (
          <div
            data-testid="review-hard-regressions"
            className="space-y-0.5 border-l-2 border-rose-300 pl-3 text-[10px] text-rose-700"
          >
            <span>重写破坏了已通过约束：</span>
            {(entry.hard_constraint_regressions ?? []).map((constraintId) => (
              <code key={constraintId} className="block break-all">
                {constraintId}
              </code>
            ))}
          </div>
        ) : null}
        {entry.revise_targets.length > 0 ? (
          <ul
            data-testid="review-revise-targets"
            className="space-y-1 border-l-2 border-border pl-3"
          >
            {entry.revise_targets.map((issue, idx) => (
              <ReviseTargetItem key={idx} issue={issue} />
            ))}
          </ul>
        ) : null}
      </button>
    </li>
  );
}

const VERDICT_BADGE_CLASS: Record<ReviewSummaryEntry["verdict"], string> = {
  pass: "bg-emerald-100 text-emerald-700",
  revise: "bg-amber-100 text-amber-700",
  rewrite: "bg-rose-100 text-rose-700",
  escalate_to_human: "bg-rose-100 text-rose-700",
};

const VERDICT_LABEL: Record<ReviewSummaryEntry["verdict"], string> = {
  pass: "通过",
  revise: "修订",
  rewrite: "重写",
  escalate_to_human: "升人工",
};

function VerdictBadge(props: { verdict: ReviewSummaryEntry["verdict"] }) {
  return (
    <span
      data-testid="review-verdict-badge"
      className={`rounded px-1.5 py-0.5 text-xs font-medium ${VERDICT_BADGE_CLASS[props.verdict]}`}
    >
      {VERDICT_LABEL[props.verdict]}
    </span>
  );
}

function ReviseTargetItem(props: { issue: ReviewIssueOut }) {
  const { issue } = props;
  return (
    <li className="space-y-0.5">
      <div className="flex flex-wrap items-baseline gap-x-2">
        <span className="font-medium text-foreground">{issue.issue_type}</span>
        <span className="text-muted-foreground">
          “{issue.paragraph_anchor}”
        </span>
      </div>
      <p className="text-muted-foreground">{issue.description}</p>
      {issue.suggestion ? (
        <p className="text-muted-foreground">建议:{issue.suggestion}</p>
      ) : null}
    </li>
  );
}

function ErrorHint(props: { error: unknown }) {
  const apiErr = isApiError(props.error) ? props.error : null;
  const message = apiErr
    ? `${apiErr.code}: ${apiErr.message}`
    : props.error instanceof Error
      ? props.error.message
      : "未知错误";
  return (
    <div
      data-testid="review-history-error"
      className="p-4 text-sm text-destructive"
    >
      加载审稿历史失败:{message}
    </div>
  );
}
