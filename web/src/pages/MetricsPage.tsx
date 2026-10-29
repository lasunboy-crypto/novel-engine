/**
 * MetricsPage(P4-F-02 概览 / 趋势 + P4-F-03 分章 / 张力 / 成本 / 时间筛选)。
 *
 * 路由 `/metrics`。本任务(P4-F-03)在 P4-F-02 基础上:
 * 1. 用 UI 时间范围控件(7 天 / 30 天 / 全部 / 自定义)替换 P4-F-02 `useState`
 *    硬编码窗口
 * 2. 接入 `MetricsPerChapterTable`(分章指标表)
 * 3. 接入 `MetricsTensionCurve`(张力曲线 target vs actual)
 * 4. 接入 `MetricsCostBreakdown`(成本分布按 writer_model + 可选 cache 命中率)
 *
 * ## since/until 作用范围(类 A 冲突,本任务 impl §4 登记)
 *
 * 启动指令字面"setRange 同时透传给 daily / per-chapter / tension / cost
 * 4 个 query",但后端 P4-B-04 字面只 daily 端点接受 `?since=&until=` 查询
 * 参数(per-chapter / tension-curve / cost-breakdown 均无 since/until 参数)。
 * 按 CLAUDE.md 类 A 纪律走规格源 = **since/until 仅透传给 daily 端点**,
 * 其它 3 端点始终展示全量历史。P5+ 后端加端点级 since/until 后再升级为
 * "全局过滤"语义。
 *
 * UI 上时间控件位于 header 区域,daily 趋势 section 标题写"每日趋势(按选择
 * 窗口)";其它三个 section 标"全量历史(P5+ 加时间过滤)"提示。
 *
 * ## 5 态分支(沿用 P4-F-02)
 *
 * workspaces loading / error / empty / 无活动工作区 / data 全齐。data 分支
 * 内部各组件再各自 loading / error / empty / data 四态(由 react-query 驱动)。
 */

import { useMemo, useState } from "react";
import { Link } from "react-router-dom";

import { isApiError } from "@/api/errors";
import {
  useMetricsCostBreakdownQuery,
  useMetricsDailyQuery,
  useMetricsOverviewQuery,
  useMetricsPerChapterQuery,
  useMetricsTensionCurveQuery,
} from "@/api/metrics";
import { useWorkspacesQuery, type WorkspaceOut } from "@/api/workspaces";
import { MetricsCostBreakdown } from "@/components/MetricsCostBreakdown";
import { MetricsDailyChart } from "@/components/MetricsDailyChart";
import { MetricsOverviewCard } from "@/components/MetricsOverviewCard";
import { MetricsPerChapterTable } from "@/components/MetricsPerChapterTable";
import { MetricsTensionCurve } from "@/components/MetricsTensionCurve";
import {
  useActiveWorkspace,
  useActiveWorkspaceStore,
} from "@/store/activeWorkspace";

type RangePreset = "7d" | "30d" | "all" | "custom";

interface DateRange {
  since: string | null; // YYYY-MM-DD 或 null(表"全部")
  until: string | null;
}

function isoDateNDaysAgo(days: number): string {
  const d = new Date();
  d.setDate(d.getDate() - days);
  return d.toISOString().slice(0, 10);
}

function isoDateToday(): string {
  return new Date().toISOString().slice(0, 10);
}

function rangeForPreset(preset: Exclude<RangePreset, "custom">): DateRange {
  if (preset === "all") {
    return { since: null, until: null };
  }
  const days = preset === "7d" ? 7 : 30;
  return { since: isoDateNDaysAgo(days), until: isoDateToday() };
}

function rangeLabel(preset: RangePreset, range: DateRange): string {
  if (preset === "all") return "全部历史";
  if (preset === "7d") return `最近 7 天(${range.since} → ${range.until})`;
  if (preset === "30d") return `最近 30 天(${range.since} → ${range.until})`;
  return `自定义(${range.since ?? "—"} → ${range.until ?? "—"})`;
}

export default function MetricsPage() {
  const workspacesQuery = useWorkspacesQuery();
  const activeWorkspace = useActiveWorkspace(workspacesQuery.data);
  const setActiveWorkspaceId = useActiveWorkspaceStore(
    (s) => s.setActiveWorkspaceId,
  );
  const workspaceId = activeWorkspace?.workspace_id ?? null;

  const [preset, setPreset] = useState<RangePreset>("30d");
  const [range, setRange] = useState<DateRange>(() => rangeForPreset("30d"));

  const overviewQuery = useMetricsOverviewQuery(workspaceId);
  // daily 端点级 since/until 透传(P4-B-04 字面唯一支持 since/until 的端点)。
  const dailyQuery = useMetricsDailyQuery(workspaceId, range.since, range.until);
  // 其它 3 端点不接受 since/until(后端规格源,本任务 impl §4 类 A 登记)。
  const perChapterQuery = useMetricsPerChapterQuery(workspaceId);
  const tensionCurveQuery = useMetricsTensionCurveQuery(workspaceId);
  const costBreakdownQuery = useMetricsCostBreakdownQuery(workspaceId);

  const activeWorkspaces = useMemo(
    () =>
      (workspacesQuery.data ?? []).filter((w) => w.status !== "archived"),
    [workspacesQuery.data],
  );

  function handlePresetChange(next: RangePreset) {
    setPreset(next);
    if (next !== "custom") {
      setRange(rangeForPreset(next));
    }
    // custom 保留当前 range,等用户改 input
  }

  function handleCustomSinceChange(value: string) {
    setRange((prev) => ({ since: value || null, until: prev.until }));
  }

  function handleCustomUntilChange(value: string) {
    setRange((prev) => ({ since: prev.since, until: value || null }));
  }

  return (
    <div className="space-y-6">
      <header className="space-y-1">
        <h1 className="text-2xl font-semibold">指标</h1>
        <MetricsHeaderHint
          workspacesLoading={workspacesQuery.isPending}
          workspacesError={workspacesQuery.error}
          activeWorkspace={activeWorkspace}
          rangeLabel={rangeLabel(preset, range)}
        />
      </header>

      {!workspacesQuery.isPending && !workspacesQuery.error
        ? (
          <div className="flex flex-wrap items-center gap-6">
            <WorkspaceSwitcher
              workspaces={activeWorkspaces}
              activeWorkspaceId={workspaceId}
              onChange={setActiveWorkspaceId}
            />
            <RangePicker
              preset={preset}
              range={range}
              onPresetChange={handlePresetChange}
              onCustomSinceChange={handleCustomSinceChange}
              onCustomUntilChange={handleCustomUntilChange}
            />
          </div>
        )
        : null}

      <MetricsBody
        workspacesLoading={workspacesQuery.isPending}
        workspacesError={workspacesQuery.error}
        workspacesEmpty={
          !workspacesQuery.isPending && activeWorkspaces.length === 0
        }
        activeWorkspace={activeWorkspace}
        overviewData={overviewQuery.data}
        overviewLoading={
          overviewQuery.isPending && overviewQuery.fetchStatus !== "idle"
        }
        overviewError={overviewQuery.error}
        dailyData={dailyQuery.data}
        dailyLoading={
          dailyQuery.isPending && dailyQuery.fetchStatus !== "idle"
        }
        dailyError={dailyQuery.error}
        perChapterData={perChapterQuery.data}
        perChapterLoading={
          perChapterQuery.isPending && perChapterQuery.fetchStatus !== "idle"
        }
        perChapterError={perChapterQuery.error}
        tensionCurveData={tensionCurveQuery.data}
        tensionCurveLoading={
          tensionCurveQuery.isPending && tensionCurveQuery.fetchStatus !== "idle"
        }
        tensionCurveError={tensionCurveQuery.error}
        costBreakdownData={costBreakdownQuery.data}
        costBreakdownLoading={
          costBreakdownQuery.isPending &&
          costBreakdownQuery.fetchStatus !== "idle"
        }
        costBreakdownError={costBreakdownQuery.error}
      />
    </div>
  );
}

interface MetricsHeaderHintProps {
  workspacesLoading: boolean;
  workspacesError: unknown;
  activeWorkspace: WorkspaceOut | null;
  rangeLabel: string;
}

function MetricsHeaderHint(props: MetricsHeaderHintProps) {
  if (props.workspacesLoading || props.workspacesError) {
    return null;
  }
  if (!props.activeWorkspace) {
    return (
      <p className="text-sm text-muted-foreground">当前没有活动工作区。</p>
    );
  }
  return (
    <p className="text-sm text-muted-foreground">
      当前活动工作区:
      <span
        className="ml-1 font-medium text-foreground"
        data-testid="metrics-active-workspace-display-name"
      >
        {props.activeWorkspace.display_name}
      </span>
      <span className="ml-1 text-xs">
        ({props.activeWorkspace.workspace_id})
      </span>
      <span className="ml-2 text-xs">每日趋势窗口:{props.rangeLabel}</span>
    </p>
  );
}

interface WorkspaceSwitcherProps {
  workspaces: readonly WorkspaceOut[];
  activeWorkspaceId: string | null;
  onChange: (id: string | null) => void;
}

function WorkspaceSwitcher(props: WorkspaceSwitcherProps) {
  if (props.workspaces.length === 0) {
    return null;
  }
  return (
    <div className="flex items-center gap-3 text-sm">
      <label
        htmlFor="metrics-workspace-switcher"
        className="text-muted-foreground"
      >
        切换工作区:
      </label>
      <select
        id="metrics-workspace-switcher"
        data-testid="metrics-workspace-switcher"
        value={props.activeWorkspaceId ?? ""}
        onChange={(e) => props.onChange(e.target.value || null)}
        className="h-9 min-w-60 rounded-md border border-input bg-background px-3 py-1 text-sm shadow-sm focus-visible:outline-none focus-visible:ring-1 focus-visible:ring-ring"
      >
        {props.workspaces.map((w) => (
          <option key={w.workspace_id} value={w.workspace_id}>
            {w.display_name}({w.workspace_id})
          </option>
        ))}
      </select>
    </div>
  );
}

interface RangePickerProps {
  preset: RangePreset;
  range: DateRange;
  onPresetChange: (preset: RangePreset) => void;
  onCustomSinceChange: (value: string) => void;
  onCustomUntilChange: (value: string) => void;
}

function RangePicker(props: RangePickerProps) {
  return (
    <div className="flex flex-wrap items-center gap-3 text-sm">
      <label
        htmlFor="metrics-range-picker"
        className="text-muted-foreground"
      >
        时间范围:
      </label>
      <select
        id="metrics-range-picker"
        data-testid="metrics-range-picker"
        value={props.preset}
        onChange={(e) => props.onPresetChange(e.target.value as RangePreset)}
        className="h-9 rounded-md border border-input bg-background px-3 py-1 text-sm shadow-sm focus-visible:outline-none focus-visible:ring-1 focus-visible:ring-ring"
      >
        <option value="7d">最近 7 天</option>
        <option value="30d">最近 30 天</option>
        <option value="all">全部历史</option>
        <option value="custom">自定义</option>
      </select>
      {props.preset === "custom" ? (
        <div className="flex items-center gap-2">
          <input
            type="date"
            data-testid="metrics-range-custom-since"
            value={props.range.since ?? ""}
            onChange={(e) => props.onCustomSinceChange(e.target.value)}
            className="h-9 rounded-md border border-input bg-background px-2 py-1 text-sm shadow-sm focus-visible:outline-none focus-visible:ring-1 focus-visible:ring-ring"
          />
          <span className="text-muted-foreground">→</span>
          <input
            type="date"
            data-testid="metrics-range-custom-until"
            value={props.range.until ?? ""}
            onChange={(e) => props.onCustomUntilChange(e.target.value)}
            className="h-9 rounded-md border border-input bg-background px-2 py-1 text-sm shadow-sm focus-visible:outline-none focus-visible:ring-1 focus-visible:ring-ring"
          />
        </div>
      ) : null}
    </div>
  );
}

interface MetricsBodyProps {
  workspacesLoading: boolean;
  workspacesError: unknown;
  workspacesEmpty: boolean;
  activeWorkspace: WorkspaceOut | null;
  overviewData: ReturnType<typeof useMetricsOverviewQuery>["data"];
  overviewLoading: boolean;
  overviewError: Error | null;
  dailyData: ReturnType<typeof useMetricsDailyQuery>["data"];
  dailyLoading: boolean;
  dailyError: Error | null;
  perChapterData: ReturnType<typeof useMetricsPerChapterQuery>["data"];
  perChapterLoading: boolean;
  perChapterError: Error | null;
  tensionCurveData: ReturnType<typeof useMetricsTensionCurveQuery>["data"];
  tensionCurveLoading: boolean;
  tensionCurveError: Error | null;
  costBreakdownData: ReturnType<typeof useMetricsCostBreakdownQuery>["data"];
  costBreakdownLoading: boolean;
  costBreakdownError: Error | null;
}

function MetricsBody(props: MetricsBodyProps) {
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
        data-testid="metrics-workspaces-error"
        className="rounded border border-destructive bg-destructive/10 p-6 text-sm text-destructive"
      >
        加载工作区失败:{formatError(props.workspacesError)}
      </div>
    );
  }
  if (props.workspacesEmpty) {
    return (
      <div className="rounded border border-dashed border-border p-6 text-sm text-muted-foreground">
        还没有可用的工作区。请先到
        <Link to="/workspaces" className="mx-1 underline">
          工作区列表
        </Link>
        创建一个(归档工作区不在此页列出)。
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

  return (
    <div className="space-y-6">
      <section>
        <MetricsOverviewCard
          data={props.overviewData}
          isLoading={props.overviewLoading}
          error={props.overviewError}
        />
      </section>
      <section>
        <h2 className="mb-3 text-lg font-semibold">每日趋势(按选择窗口)</h2>
        <MetricsDailyChart
          data={props.dailyData}
          isLoading={props.dailyLoading}
          error={props.dailyError}
        />
      </section>
      <section>
        <h2 className="mb-3 text-lg font-semibold">
          分章指标
          <span className="ml-2 text-xs font-normal text-muted-foreground">
            (全量历史;P5+ 后端加端点级 since/until 后启用全局时间过滤)
          </span>
        </h2>
        <MetricsPerChapterTable
          data={props.perChapterData}
          isLoading={props.perChapterLoading}
          error={props.perChapterError}
        />
      </section>
      <section>
        <h2 className="mb-3 text-lg font-semibold">
          张力曲线
          <span className="ml-2 text-xs font-normal text-muted-foreground">
            (全量历史)
          </span>
        </h2>
        <MetricsTensionCurve
          data={props.tensionCurveData}
          isLoading={props.tensionCurveLoading}
          error={props.tensionCurveError}
        />
      </section>
      <section>
        <h2 className="mb-3 text-lg font-semibold">
          成本分布
          <span className="ml-2 text-xs font-normal text-muted-foreground">
            (全量历史)
          </span>
        </h2>
        <MetricsCostBreakdown
          data={props.costBreakdownData}
          isLoading={props.costBreakdownLoading}
          error={props.costBreakdownError}
          perChapterData={props.perChapterData}
        />
      </section>
    </div>
  );
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
