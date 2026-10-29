/**
 * 张力曲线图(P4-F-03,Recharts AreaChart 双系列)。
 *
 * 后端数据源 `GET /api/v1/workspaces/{id}/metrics/tension-curve`(P4-B-04),
 * 返回 TensionPoint 数组(每章一点):
 *   - target_tension_level(int 1-10,章节卡 tension_level)
 *   - actual_tension_estimate(float | null;**当前全 None**,P5+ Reviewer
 *     估计字段实装后才落库,见 OBSERVABILITY §3.1 + P4-B-04 留观项)
 *
 * 设计:
 *   - X 轴:`chapter_id`(章节序号字符串,如 `v1c001`)
 *   - Y 轴:0-10 固定范围,两系列同量纲
 *   - 两 Area 系列:target(emerald)+ actual(rose,空数据时仅渲 X 轴)
 *   - actual 全 None 时表内顶部加占位提示("待 P5+ Reviewer 实装")
 *
 * 状态分支:loading / error / empty / data 四态,对齐 MetricsDailyChart 套路。
 */

import {
  Area,
  AreaChart,
  CartesianGrid,
  Legend,
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
import type { TensionCurveOut, TensionPoint } from "@/api/metrics";

interface MetricsTensionCurveProps {
  data: TensionCurveOut | undefined;
  isLoading: boolean;
  error: Error | null;
}

interface TensionChartPoint {
  chapter_id: string;
  target_tension_level: number;
  actual_tension_estimate: number | null;
}

function toChartPoints(points: TensionPoint[]): TensionChartPoint[] {
  return points.map((p) => ({
    chapter_id: p.chapter_id,
    target_tension_level: p.target_tension_level,
    actual_tension_estimate: p.actual_tension_estimate ?? null,
  }));
}

export function MetricsTensionCurve({
  data,
  isLoading,
  error,
}: MetricsTensionCurveProps) {
  if (isLoading) {
    return (
      <Card data-testid="metrics-tension-curve-loading">
        <CardHeader className="pb-2">
          <CardTitle className="text-base">张力曲线</CardTitle>
          <CardDescription className="text-xs">target vs actual</CardDescription>
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
        data-testid="metrics-tension-curve-error"
      >
        <CardHeader>
          <CardTitle className="text-base text-rose-700">
            张力曲线加载失败
          </CardTitle>
          <CardDescription className="text-rose-600">
            {error.message}
          </CardDescription>
        </CardHeader>
      </Card>
    );
  }

  const points = toChartPoints(data?.points ?? []);

  if (points.length === 0) {
    return (
      <Card data-testid="metrics-tension-curve-empty">
        <CardHeader>
          <CardTitle className="text-base">张力曲线暂无数据</CardTitle>
          <CardDescription>
            该工作区当前没有 APPROVED 章节;每写一章并通过审稿后会出现一个点。
          </CardDescription>
        </CardHeader>
      </Card>
    );
  }

  const actualHasData = points.some(
    (p) => p.actual_tension_estimate !== null,
  );

  return (
    <Card data-testid="metrics-tension-curve-card">
      <CardHeader className="pb-2">
        <CardTitle className="text-base">张力曲线</CardTitle>
        <CardDescription className="text-xs">
          target(章节卡)vs actual(Reviewer 估计);Y 轴 1-10
        </CardDescription>
      </CardHeader>
      <CardContent>
        {!actualHasData ? (
          <p
            className="mb-2 rounded border border-dashed border-border bg-muted/40 px-3 py-1.5 text-xs text-muted-foreground"
            data-testid="metrics-tension-curve-actual-placeholder"
          >
            actual_tension_estimate 当前为空 — P5+ Reviewer 估计字段实装后此曲线渲染数据。
          </p>
        ) : null}
        <div className="h-64 w-full">
          <ResponsiveContainer width="100%" height="100%">
            <AreaChart
              data={points}
              margin={{ top: 8, right: 16, bottom: 8, left: 0 }}
            >
              <CartesianGrid strokeDasharray="3 3" className="stroke-muted" />
              <XAxis
                dataKey="chapter_id"
                tick={{ fontSize: 11 }}
                interval="preserveStartEnd"
                minTickGap={24}
              />
              <YAxis
                domain={[0, 10]}
                tick={{ fontSize: 11 }}
                width={36}
                allowDecimals={false}
              />
              <Tooltip
                contentStyle={{ fontSize: 12 }}
                formatter={(value, name) => {
                  if (typeof value === "number") {
                    return [value.toFixed(2), name as string];
                  }
                  return [value as string | number, name as string];
                }}
              />
              <Legend wrapperStyle={{ fontSize: 12 }} />
              <Area
                type="monotone"
                name="target"
                dataKey="target_tension_level"
                stroke="#10b981"
                fill="#10b981"
                fillOpacity={0.18}
                strokeWidth={2}
                isAnimationActive={false}
              />
              <Area
                type="monotone"
                name="actual"
                dataKey="actual_tension_estimate"
                stroke="#f43f5e"
                fill="#f43f5e"
                fillOpacity={0.18}
                strokeWidth={2}
                connectNulls={false}
                isAnimationActive={false}
              />
            </AreaChart>
          </ResponsiveContainer>
        </div>
      </CardContent>
    </Card>
  );
}
