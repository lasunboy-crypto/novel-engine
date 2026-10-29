/**
 * 每日趋势折线图(P4-F-02,Recharts LineChart)。
 *
 * 后端数据源 `GET /api/v1/workspaces/{id}/metrics/daily?since=&until=`
 * (P4-B-04),返回 metrics_daily 行数组(date asc)。
 *
 * 自决项:三系列独立堆叠 vs 共享 Y 轴
 * ----------------------------------
 *
 * `chapters_drafted` / `total_cost_usd` / `avg_review_score` 三系列量纲差异大:
 * 章数 ~0-10、成本 $0.00-$5.00、评分 0-10。共享同一 Y 轴会让小值贴底不可读。
 * 双 Y 轴(yAxisId="left"/"right")在 Recharts 内紧凑但可读性差,且 review
 * score 量纲又不同。本任务选**三个独立 LineChart 垂直堆叠**:
 *
 * - 各自独立 Y 轴 → 量纲清晰
 * - 共享 X 轴(date)+ 共享 ResponsiveContainer 宽度 → 视觉对齐
 * - P4-F-03 张力曲线(双折线 target vs actual,同 1-10 量纲)再练单图多线
 *
 * 状态分支:loading / error / empty / data 四态(空数组 = 工作区无 APPROVED 章节)。
 *
 * X 轴 tick:为避免 30 天 30 个 tick 拥挤,Recharts `interval` 自动取最大密度;
 * tickFormatter 截短日期为 `MM-DD`(month-day)。
 */

import {
  CartesianGrid,
  Line,
  LineChart,
  ResponsiveContainer,
  Tooltip,
  XAxis,
  YAxis,
} from "recharts";

import {
  Card,
  CardContent,
  CardDescription,
  CardHeader,
  CardTitle,
} from "@/components/ui/card";
import { formatUsd } from "@/lib/format-metrics";
import type { DailyListOut, MetricsDailyOut } from "@/api/metrics";

interface MetricsDailyChartProps {
  data: DailyListOut | undefined;
  isLoading: boolean;
  error: Error | null;
}

interface ChartPoint {
  date: string;
  dateShort: string;
  chapters_drafted: number;
  total_cost_usd: number;
  avg_review_score: number | null;
}

function toChartPoints(rows: MetricsDailyOut[]): ChartPoint[] {
  return rows.map((row) => ({
    date: row.date,
    dateShort: row.date.slice(5), // "2026-05-11" → "05-11"
    chapters_drafted: row.chapters_drafted,
    total_cost_usd: row.total_cost_usd,
    avg_review_score: row.avg_review_score ?? null,
  }));
}

interface SingleSeriesChartProps {
  points: ChartPoint[];
  dataKey: keyof Omit<ChartPoint, "date" | "dateShort">;
  title: string;
  description: string;
  color: string;
  yDomain?: [number | string, number | string];
  tooltipFormatter?: (value: number) => string;
  testId: string;
}

function SingleSeriesChart({
  points,
  dataKey,
  title,
  description,
  color,
  yDomain,
  tooltipFormatter,
  testId,
}: SingleSeriesChartProps) {
  return (
    <Card data-testid={testId}>
      <CardHeader className="pb-2">
        <CardTitle className="text-sm font-semibold">{title}</CardTitle>
        <CardDescription className="text-xs">{description}</CardDescription>
      </CardHeader>
      <CardContent>
        <div className="h-48 w-full">
          <ResponsiveContainer width="100%" height="100%">
            <LineChart
              data={points}
              margin={{ top: 8, right: 16, bottom: 8, left: 0 }}
            >
              <CartesianGrid strokeDasharray="3 3" className="stroke-muted" />
              <XAxis
                dataKey="dateShort"
                tick={{ fontSize: 11 }}
                interval="preserveStartEnd"
                minTickGap={18}
              />
              <YAxis
                tick={{ fontSize: 11 }}
                domain={yDomain}
                width={48}
                allowDecimals={false}
              />
              <Tooltip
                labelFormatter={(_label, payload) => {
                  const p = payload?.[0]?.payload as ChartPoint | undefined;
                  return p?.date ?? "";
                }}
                formatter={(value) => {
                  if (typeof value !== "number") {
                    return [value as string | number, title];
                  }
                  return [
                    tooltipFormatter ? tooltipFormatter(value) : String(value),
                    title,
                  ];
                }}
                contentStyle={{ fontSize: 12 }}
              />
              <Line
                type="monotone"
                dataKey={dataKey}
                stroke={color}
                strokeWidth={2}
                dot={{ r: 2 }}
                activeDot={{ r: 4 }}
                connectNulls={false}
                isAnimationActive={false}
              />
            </LineChart>
          </ResponsiveContainer>
        </div>
      </CardContent>
    </Card>
  );
}

export function MetricsDailyChart({
  data,
  isLoading,
  error,
}: MetricsDailyChartProps) {
  if (isLoading) {
    return (
      <div className="grid gap-4 lg:grid-cols-3" data-testid="metrics-daily-loading">
        {Array.from({ length: 3 }).map((_, idx) => (
          <Card key={idx} className="animate-pulse">
            <CardHeader className="pb-2">
              <div className="h-4 w-32 rounded bg-muted" />
            </CardHeader>
            <CardContent>
              <div className="h-48 w-full rounded bg-muted" />
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
        data-testid="metrics-daily-error"
      >
        <CardHeader>
          <CardTitle className="text-base text-rose-700">
            趋势数据加载失败
          </CardTitle>
          <CardDescription className="text-rose-600">
            {error.message}
          </CardDescription>
        </CardHeader>
      </Card>
    );
  }

  const points = toChartPoints(data?.items ?? []);

  if (points.length === 0) {
    return (
      <Card data-testid="metrics-daily-empty">
        <CardHeader>
          <CardTitle className="text-base">最近 30 天暂无每日数据</CardTitle>
          <CardDescription>
            该工作区当前时间窗口内没有 APPROVED 章节;写完一章后对应日期会出现数据点。
          </CardDescription>
        </CardHeader>
      </Card>
    );
  }

  return (
    <div className="grid gap-4 lg:grid-cols-3" data-testid="metrics-daily-grid">
      <SingleSeriesChart
        testId="daily-chart-chapters-drafted"
        points={points}
        dataKey="chapters_drafted"
        title="每日起草章数"
        description="chapters_drafted(进入 DRAFTING 起算)"
        color="#0ea5e9"
        yDomain={[0, "auto"]}
      />
      <SingleSeriesChart
        testId="daily-chart-total-cost"
        points={points}
        dataKey="total_cost_usd"
        title="每日 LLM 成本(USD)"
        description="total_cost_usd 当日合计"
        color="#10b981"
        yDomain={[0, "auto"]}
        tooltipFormatter={formatUsd}
      />
      <SingleSeriesChart
        testId="daily-chart-avg-score"
        points={points}
        dataKey="avg_review_score"
        title="每日平均评分"
        description="avg_review_score(无样本日为空)"
        color="#a855f7"
        yDomain={[0, 10]}
      />
    </div>
  );
}
