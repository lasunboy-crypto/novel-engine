/**
 * 分章指标表(P4-F-03)。
 *
 * 后端数据源 `GET /api/v1/workspaces/{id}/metrics/per-chapter?order=`
 * (P4-B-04),返回 metrics_per_chapter 行数组(默认 approved_at desc)。
 *
 * 列(启动指令字面):
 *   - 章节 ID(chapter_id)
 *   - 字数(word_count)
 *   - 评分(final_score_overall;阈值颜色档同 OverviewCard)
 *   - 成本(total_cost_usd)
 *   - Token(total_tokens)
 *   - 耗时(total_latency_seconds → minutes)
 *   - Writer 模型(writer_model;P5+ ChapterVersionORM 透传 writer_profile_id 后切真 profile)
 *
 * P4 不实现:`volume=` 卷筛选(metrics_per_chapter schema 无 `volume_id` 字段,
 * P4-B-04 决策 4 字面"P5+ 才实现")。本表暂用 order 默认 `approved_at_desc`,
 * 不暴露排序 UI(P4 简化;ChaptersBoardPage 已可看章序,这里聚焦 metrics)。
 *
 * 状态分支:loading / error / empty / data 四态,对齐 MetricsDailyChart 套路。
 */

import {
  Card,
  CardContent,
  CardDescription,
  CardHeader,
  CardTitle,
} from "@/components/ui/card";
import {
  formatInt,
  formatLatencyMinutes,
  formatScore,
  formatUsd,
  scoreColor,
} from "@/lib/format-metrics";
import type { PerChapterListOut, PerChapterOut } from "@/api/metrics";

interface MetricsPerChapterTableProps {
  data: PerChapterListOut | undefined;
  isLoading: boolean;
  error: Error | null;
}

export function MetricsPerChapterTable({
  data,
  isLoading,
  error,
}: MetricsPerChapterTableProps) {
  if (isLoading) {
    return (
      <Card data-testid="metrics-per-chapter-loading">
        <CardHeader className="pb-2">
          <CardTitle className="text-base">分章指标</CardTitle>
          <CardDescription className="text-xs">
            metrics_per_chapter(approved_at desc)
          </CardDescription>
        </CardHeader>
        <CardContent>
          <div className="h-32 w-full animate-pulse rounded bg-muted" />
        </CardContent>
      </Card>
    );
  }

  if (error) {
    return (
      <Card
        className="border-rose-300 bg-rose-50"
        data-testid="metrics-per-chapter-error"
      >
        <CardHeader>
          <CardTitle className="text-base text-rose-700">
            分章指标加载失败
          </CardTitle>
          <CardDescription className="text-rose-600">
            {error.message}
          </CardDescription>
        </CardHeader>
      </Card>
    );
  }

  const rows = data?.items ?? [];

  if (rows.length === 0) {
    return (
      <Card data-testid="metrics-per-chapter-empty">
        <CardHeader>
          <CardTitle className="text-base">暂无分章指标</CardTitle>
          <CardDescription>
            该工作区当前还没有 APPROVED 章节(metrics_per_chapter 表为空)。
          </CardDescription>
        </CardHeader>
      </Card>
    );
  }

  return (
    <Card data-testid="metrics-per-chapter-card">
      <CardHeader className="pb-2">
        <CardTitle className="text-base">分章指标</CardTitle>
        <CardDescription className="text-xs">
          metrics_per_chapter(approved_at desc;共 {rows.length} 行)
        </CardDescription>
      </CardHeader>
      <CardContent>
        <div className="overflow-hidden rounded border border-border">
          <table
            className="w-full text-sm"
            data-testid="metrics-per-chapter-table"
          >
            <thead className="bg-muted/40 text-left text-xs uppercase tracking-wide text-muted-foreground">
              <tr>
                <th className="px-3 py-2 font-medium">章节 ID</th>
                <th className="px-3 py-2 font-medium text-right">字数</th>
                <th className="px-3 py-2 font-medium text-right">评分</th>
                <th className="px-3 py-2 font-medium text-right">成本</th>
                <th className="px-3 py-2 font-medium text-right">Token</th>
                <th className="px-3 py-2 font-medium text-right">耗时</th>
                <th className="px-3 py-2 font-medium">Writer 模型</th>
              </tr>
            </thead>
            <tbody>
              {rows.map((row) => (
                <PerChapterRow key={row.chapter_id} row={row} />
              ))}
            </tbody>
          </table>
        </div>
      </CardContent>
    </Card>
  );
}

interface PerChapterRowProps {
  row: PerChapterOut;
}

function PerChapterRow({ row }: PerChapterRowProps) {
  return (
    <tr
      className="border-t border-border hover:bg-muted/20"
      data-testid={`per-chapter-row-${row.chapter_id}`}
    >
      <td className="px-3 py-2 font-mono text-xs">{row.chapter_id}</td>
      <td className="px-3 py-2 text-right">{formatInt(row.word_count)}</td>
      <td
        className={`px-3 py-2 text-right font-semibold ${scoreColor(row.final_score_overall)}`}
      >
        {formatScore(row.final_score_overall)}
      </td>
      <td className="px-3 py-2 text-right">
        {formatUsd(row.total_cost_usd)}
      </td>
      <td className="px-3 py-2 text-right">{formatInt(row.total_tokens)}</td>
      <td className="px-3 py-2 text-right">
        {formatLatencyMinutes(row.total_latency_seconds)}
      </td>
      <td className="px-3 py-2 font-mono text-xs text-muted-foreground">
        {row.writer_model}
      </td>
    </tr>
  );
}
