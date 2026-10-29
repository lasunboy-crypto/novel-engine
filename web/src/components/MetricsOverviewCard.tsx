/**
 * 指标概览卡片(P4-F-02)。
 *
 * 6 张 shadcn Card 横向排列,绑 OverviewOut 7 字段:
 *
 * | 卡片 | 主数值 | 副说明 |
 * |------|--------|--------|
 * | 累计 APPROVED 章节 | chapters_approved | 今日新增 today_chapters_approved |
 * | 累计 LLM 成本 | total_cost_usd | 今日 today_cost_usd |
 * | 平均评分 | avg_review_score | 阈值颜色档(对齐 PROJECT_SPEC §4.4 / VoiceCheckPanel) |
 * | 平均章节耗时 | avg_chapter_latency_seconds | 转分钟显示 |
 * | 累计 token | total_tokens | input + output 合计 |
 * | 当前工作区 | workspace_id | OverviewOut.workspace_id 回显 |
 *
 * 状态分支:loading / error / data 三态(对齐 ChaptersBoardPage 5 态分支精简版,
 * 因 query enabled=false 已由父级筛掉,本组件总有 workspace 上下文)。
 *
 * 颜色档(对齐 ReviewHistoryPanel VerdictBadge / VoiceCheckPanel 评分):
 * - score >= 8 → emerald(绿)
 * - score >= 6.5 → amber(琥珀)
 * - score < 6.5 → rose(红)
 * - score === null → muted(灰,语义"无样本")
 */

import {
  Card,
  CardContent,
  CardDescription,
  CardHeader,
  CardTitle,
} from "@/components/ui/card";
import type { OverviewOut } from "@/api/metrics";

interface MetricsOverviewCardProps {
  data: OverviewOut | undefined;
  isLoading: boolean;
  error: Error | null;
}

function formatUsd(value: number): string {
  return `$${value.toFixed(4)}`;
}

function formatInt(value: number): string {
  return value.toLocaleString("en-US");
}

function formatScore(value: number | null | undefined): string {
  return value === null || value === undefined ? "—" : value.toFixed(2);
}

function scoreColor(value: number | null | undefined): string {
  if (value === null || value === undefined) return "text-muted-foreground";
  if (value >= 8) return "text-emerald-600";
  if (value >= 6.5) return "text-amber-600";
  return "text-rose-600";
}

function formatLatencyMinutes(seconds: number | null | undefined): string {
  if (seconds === null || seconds === undefined) return "—";
  const minutes = seconds / 60;
  if (minutes >= 10) return `${minutes.toFixed(0)} min`;
  return `${minutes.toFixed(1)} min`;
}

interface StatCardProps {
  label: string;
  value: string;
  hint?: string;
  valueClassName?: string;
  testId: string;
}

function StatCard({ label, value, hint, valueClassName, testId }: StatCardProps) {
  return (
    <Card data-testid={testId}>
      <CardHeader className="pb-2">
        <CardDescription className="text-xs text-muted-foreground">
          {label}
        </CardDescription>
      </CardHeader>
      <CardContent>
        <CardTitle className={valueClassName ?? "text-2xl font-semibold"}>
          {value}
        </CardTitle>
        {hint ? (
          <p className="mt-1 text-xs text-muted-foreground">{hint}</p>
        ) : null}
      </CardContent>
    </Card>
  );
}

export function MetricsOverviewCard({
  data,
  isLoading,
  error,
}: MetricsOverviewCardProps) {
  if (isLoading) {
    return (
      <div
        className="grid grid-cols-2 gap-4 sm:grid-cols-3 lg:grid-cols-6"
        data-testid="metrics-overview-loading"
      >
        {Array.from({ length: 6 }).map((_, idx) => (
          <Card key={idx} className="animate-pulse">
            <CardHeader className="pb-2">
              <div className="h-3 w-20 rounded bg-muted" />
            </CardHeader>
            <CardContent>
              <div className="h-8 w-24 rounded bg-muted" />
            </CardContent>
          </Card>
        ))}
      </div>
    );
  }

  if (error) {
    return (
      <Card
        className="border-rose-300 bg-rose-50"
        data-testid="metrics-overview-error"
      >
        <CardHeader>
          <CardTitle className="text-base text-rose-700">
            概览加载失败
          </CardTitle>
          <CardDescription className="text-rose-600">
            {error.message}
          </CardDescription>
        </CardHeader>
      </Card>
    );
  }

  if (!data) {
    return (
      <Card data-testid="metrics-overview-empty">
        <CardHeader>
          <CardTitle className="text-base">暂无概览数据</CardTitle>
          <CardDescription>
            该工作区尚未有 APPROVED 章节;写完一章并通过审稿后会看到累计统计。
          </CardDescription>
        </CardHeader>
      </Card>
    );
  }

  return (
    <div
      className="grid grid-cols-2 gap-4 sm:grid-cols-3 lg:grid-cols-6"
      data-testid="metrics-overview-grid"
    >
      <StatCard
        testId="overview-card-chapters-approved"
        label="累计 APPROVED 章节"
        value={formatInt(data.chapters_approved)}
        hint={`今日新增 ${formatInt(data.today_chapters_approved)}`}
      />
      <StatCard
        testId="overview-card-total-cost"
        label="累计 LLM 成本"
        value={formatUsd(data.total_cost_usd)}
        hint={`今日 ${formatUsd(data.today_cost_usd)}`}
      />
      <StatCard
        testId="overview-card-avg-score"
        label="平均评分"
        value={formatScore(data.avg_review_score)}
        valueClassName={`text-2xl font-semibold ${scoreColor(data.avg_review_score)}`}
        hint={
          data.avg_review_score === null
            ? "无样本"
            : `阈值 ≥ 8 优 / ≥ 6.5 中 / < 6.5 弱`
        }
      />
      <StatCard
        testId="overview-card-avg-latency"
        label="平均章节耗时"
        value={formatLatencyMinutes(data.avg_chapter_latency_seconds)}
        hint="单章 write→approve 总耗时均值"
      />
      <StatCard
        testId="overview-card-total-tokens"
        label="累计 token"
        value={formatInt(data.total_tokens)}
        hint="LLM input + output 合计"
      />
      <StatCard
        testId="overview-card-workspace-id"
        label="当前工作区"
        value={data.workspace_id}
        valueClassName="text-base font-mono font-medium break-all"
      />
    </div>
  );
}
