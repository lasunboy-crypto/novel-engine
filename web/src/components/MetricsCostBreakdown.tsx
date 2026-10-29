/**
 * 成本分布饼图(P4-F-03,Recharts PieChart)。
 *
 * 后端数据源:
 *   - `GET /api/v1/workspaces/{id}/metrics/cost-breakdown`(P4-B-04)→ buckets
 *     按 `writer_model` 维度分组({ writer_model, chapters_count,
 *     total_cost_usd, total_tokens })
 *   - 可选 `metrics_per_chapter` 数据透传:用于聚合 Anthropic prompt cache
 *     命中率 `cache_hit_ratio`(per-chapter 字段);默认无数据时显示"未启用"
 *
 * `writer_profile == writer_model`(P4-B-03 类 A fallback,P5+ ChapterVersionORM
 * 落 `writer_profile_id` 字段后切真 profile_id)。
 *
 * 状态分支:loading / error / empty / data 四态,对齐 MetricsDailyChart 套路。
 */

import { useMemo } from "react";
import {
  Cell,
  Legend,
  Pie,
  PieChart,
  ResponsiveContainer,
  Tooltip,
} from "recharts";

import {
  Card,
  CardContent,
  CardDescription,
  CardHeader,
  CardTitle,
} from "@/components/ui/card";
import { formatInt, formatUsd } from "@/lib/format-metrics";
import type {
  CostBreakdownOut,
  CostBucket,
  PerChapterListOut,
} from "@/api/metrics";

// 与 OverviewCard / DailyChart / TensionCurve 各 emerald/amber/rose/sky 配色对齐;
// 7 色覆盖 P4 默认 writer_model 集(qwen-novel / claude-sonnet-4-6 / claude-opus-4-7
// + Anthropic / OpenAI 兼容 P3 备选 4)。
const SLICE_COLORS = [
  "#0ea5e9", // sky
  "#10b981", // emerald
  "#a855f7", // purple
  "#f59e0b", // amber
  "#f43f5e", // rose
  "#6366f1", // indigo
  "#14b8a6", // teal
];

interface MetricsCostBreakdownProps {
  data: CostBreakdownOut | undefined;
  isLoading: boolean;
  error: Error | null;
  /**
   * 可选 per-chapter 数据(MetricsPage 父级统一拉),用于聚合 prompt cache
   * 命中率。本组件不直接调用 hook,保持 prop-driven 数据流(对齐
   * MetricsOverviewCard / MetricsDailyChart 模式)。
   */
  perChapterData?: PerChapterListOut | undefined;
}

interface CacheHitSummary {
  /** 至少一行 cache_hit_ratio 非 null。 */
  hasData: boolean;
  /** 加权平均(按 chapters 数);无数据时为 null。 */
  avgRatio: number | null;
  /** 参与统计的章节数(cache_hit_ratio !== null 的样本数)。 */
  sampleCount: number;
}

function summarizeCacheHit(
  perChapterData: PerChapterListOut | undefined,
): CacheHitSummary {
  const rows = perChapterData?.items ?? [];
  const samples = rows.filter(
    (r) => r.cache_hit_ratio !== null && r.cache_hit_ratio !== undefined,
  );
  if (samples.length === 0) {
    return { hasData: false, avgRatio: null, sampleCount: 0 };
  }
  const sum = samples.reduce(
    (acc, r) => acc + (r.cache_hit_ratio as number),
    0,
  );
  return {
    hasData: true,
    avgRatio: sum / samples.length,
    sampleCount: samples.length,
  };
}

interface PieSliceProps {
  name: string;
  value: number;
  chapters_count: number;
  total_tokens: number;
}

function toPieData(buckets: CostBucket[]): PieSliceProps[] {
  return buckets.map((b) => ({
    name: b.writer_model,
    value: b.total_cost_usd,
    chapters_count: b.chapters_count,
    total_tokens: b.total_tokens,
  }));
}

export function MetricsCostBreakdown({
  data,
  isLoading,
  error,
  perChapterData,
}: MetricsCostBreakdownProps) {
  const cacheSummary = useMemo(
    () => summarizeCacheHit(perChapterData),
    [perChapterData],
  );

  if (isLoading) {
    return (
      <Card data-testid="metrics-cost-breakdown-loading">
        <CardHeader className="pb-2">
          <CardTitle className="text-base">成本分布</CardTitle>
          <CardDescription className="text-xs">
            按 writer_model 分组
          </CardDescription>
        </CardHeader>
        <CardContent>
          <div className="h-56 w-full animate-pulse rounded bg-muted" />
        </CardContent>
      </Card>
    );
  }

  if (error) {
    return (
      <Card
        className="border-rose-300 bg-rose-50"
        data-testid="metrics-cost-breakdown-error"
      >
        <CardHeader>
          <CardTitle className="text-base text-rose-700">
            成本分布加载失败
          </CardTitle>
          <CardDescription className="text-rose-600">
            {error.message}
          </CardDescription>
        </CardHeader>
      </Card>
    );
  }

  const buckets = data?.buckets ?? [];
  const pieData = toPieData(buckets);
  const totalCost = pieData.reduce((acc, p) => acc + p.value, 0);

  if (pieData.length === 0 || totalCost === 0) {
    return (
      <Card data-testid="metrics-cost-breakdown-empty">
        <CardHeader>
          <CardTitle className="text-base">成本分布暂无数据</CardTitle>
          <CardDescription>
            该工作区当前没有 APPROVED 章节,或 LLM 成本为 0;写完一章后会按
            writer_model 维度分组。
          </CardDescription>
        </CardHeader>
        <CardContent>
          <CachePlaceholder summary={cacheSummary} />
        </CardContent>
      </Card>
    );
  }

  return (
    <Card data-testid="metrics-cost-breakdown-card">
      <CardHeader className="pb-2">
        <CardTitle className="text-base">成本分布</CardTitle>
        <CardDescription className="text-xs">
          按 writer_model 分组(共 {pieData.length} 个模型,合计 {formatUsd(totalCost)})
        </CardDescription>
      </CardHeader>
      <CardContent>
        <div className="h-64 w-full">
          <ResponsiveContainer width="100%" height="100%">
            <PieChart>
              <Pie
                data={pieData}
                dataKey="value"
                nameKey="name"
                cx="50%"
                cy="50%"
                outerRadius={80}
                isAnimationActive={false}
                label={(entry: { name?: string }) => entry.name ?? ""}
              >
                {pieData.map((_, idx) => (
                  <Cell
                    key={idx}
                    fill={SLICE_COLORS[idx % SLICE_COLORS.length]}
                  />
                ))}
              </Pie>
              <Tooltip
                contentStyle={{ fontSize: 12 }}
                formatter={(value, _name, item) => {
                  const p = (item?.payload ?? null) as
                    | PieSliceProps
                    | null;
                  if (typeof value !== "number" || !p) {
                    return [String(value), p?.name ?? ""];
                  }
                  return [
                    `${formatUsd(value)}(${p.chapters_count} 章 / ${formatInt(p.total_tokens)} token)`,
                    p.name,
                  ];
                }}
              />
              <Legend wrapperStyle={{ fontSize: 12 }} />
            </PieChart>
          </ResponsiveContainer>
        </div>
        <CachePlaceholder summary={cacheSummary} />
      </CardContent>
    </Card>
  );
}

interface CachePlaceholderProps {
  summary: CacheHitSummary;
}

function CachePlaceholder({ summary }: CachePlaceholderProps) {
  if (!summary.hasData) {
    return (
      <p
        className="mt-3 rounded border border-dashed border-border bg-muted/40 px-3 py-1.5 text-xs text-muted-foreground"
        data-testid="metrics-cost-cache-placeholder"
      >
        Anthropic prompt cache 命中率:未启用(尚无 cache_hit_ratio 样本)。
      </p>
    );
  }
  return (
    <p
      className="mt-3 rounded border border-border bg-muted/40 px-3 py-1.5 text-xs text-muted-foreground"
      data-testid="metrics-cost-cache-hit"
    >
      Anthropic prompt cache 命中率:
      <span className="ml-1 font-semibold text-foreground">
        {((summary.avgRatio as number) * 100).toFixed(1)}%
      </span>
      <span className="ml-1">(基于 {summary.sampleCount} 章样本均值)</span>
    </p>
  );
}
