/**
 * LogsPage(P4-F-04 筛选 + 详情抽屉 + Trace 视图,P4-F-05 加实时模式)。
 *
 * 路由 `/logs`(列表 + 抽屉)与 `/logs/trace/:traceId`(Trace 视图)。
 *
 * ## 静态模式 vs 实时模式(P4-F-05)
 *
 * 顶部 toggle 切换两态:
 *
 * - **静态模式**(默认):走 `useLogsListQuery` 拉一次性快照,显式"应用筛选"
 *   按钮触发重拉;支持分页 + 抽屉。
 * - **实时模式**:`useLogsStream` 接后端 P4-B-05 独立 WS `/api/v1/logs/stream`
 *   单向 push(`LOG_STREAM_POLL_SECONDS=1.0` 心跳),append 新行 + 滚动锁定;
 *   暴露"暂停 / 恢复 / 清屏"3 按钮。实时模式期间筛选栏置灰不可应用(buffer
 *   是无过滤流;OBSERVABILITY §2.6 字面 client-side 过滤 P5+ 再加),应用按钮
 *   禁用并显示 hint。
 *
 * ## 筛选栏字段(对齐 FRONTEND_SPEC §4.12 + OBSERVABILITY §2.6 字面 8 query)
 *
 * - workspace_id / chapter_id / trace_id / request_id / event / level /
 *   since / until / page_size
 * - 提交模式:**显式应用按钮**(避免每次按键都触发请求);后端无 since/until
 *   端点级缓存,频繁请求会拖慢 jsonl 扫描
 *
 * ## 4 态分支(对齐 metrics 套路)
 *
 * loading / error / empty / data。Empty 在 jsonl 写入接通(P4-acceptance)前
 * 是常态,显示 "无日志条目" + 接入 marker 提示。
 *
 * ## 抽屉交互
 *
 * 点行 → 打开 LogDetailDrawer,展开完整 JSON;Drawer 内 trace_id 链接
 * 跳转到 `/logs/trace/{trace_id}`(关闭抽屉 + 触发路由)。
 */

import { useEffect, useMemo, useRef, useState } from "react";
import { Link, useParams } from "react-router-dom";

import { isApiError } from "@/api/errors";
import {
  useLogsListQuery,
  useLogsTraceQuery,
  type LogEntry,
  type LogsListFilters,
} from "@/api/logs";
import { Button } from "@/components/ui/button";
import { LogDetailDrawer } from "@/components/LogDetailDrawer";
import { TraceView } from "@/components/TraceView";
import { useLogsStream, type LogsStreamStatus } from "@/ws/logs-stream";

interface FormState {
  workspace_id: string;
  chapter_id: string;
  trace_id: string;
  request_id: string;
  event: string;
  level: string;
  since: string;
  until: string;
}

const EMPTY_FORM: FormState = {
  workspace_id: "",
  chapter_id: "",
  trace_id: "",
  request_id: "",
  event: "",
  level: "",
  since: "",
  until: "",
};

const DEFAULT_PAGE_SIZE = 50;

function formStateToFilters(form: FormState, page: number): LogsListFilters {
  const clean = (v: string): string | null => (v.trim() === "" ? null : v.trim());
  return {
    workspace_id: clean(form.workspace_id),
    chapter_id: clean(form.chapter_id),
    trace_id: clean(form.trace_id),
    request_id: clean(form.request_id),
    event: clean(form.event),
    level: clean(form.level),
    since: clean(form.since),
    until: clean(form.until),
    page,
    page_size: DEFAULT_PAGE_SIZE,
  };
}

export default function LogsPage() {
  const params = useParams<{ traceId?: string }>();
  if (params.traceId) {
    return <LogsTracePage traceId={params.traceId} />;
  }
  return <LogsListPage />;
}

function LogsListPage() {
  const [form, setForm] = useState<FormState>(EMPTY_FORM);
  // 已应用的筛选条件;`form` 仅是 UI 草稿,真正驱动查询的是 `applied`
  const [applied, setApplied] = useState<FormState>(EMPTY_FORM);
  const [page, setPage] = useState<number>(1);
  const [selectedEntry, setSelectedEntry] = useState<LogEntry | null>(null);
  // P4-F-05:实时模式 toggle。开启时禁用筛选 apply,改走 WS buffer。
  const [isStreaming, setIsStreaming] = useState<boolean>(false);

  const filters = useMemo(
    () => formStateToFilters(applied, page),
    [applied, page],
  );
  // 静态模式才发起 REST 查询;实时模式期间不重复拉历史。
  const query = useLogsListQuery(filters);

  const stream = useLogsStream({ enabled: isStreaming });

  function updateField<K extends keyof FormState>(key: K, value: FormState[K]) {
    setForm((prev) => ({ ...prev, [key]: value }));
  }

  function handleApply() {
    setApplied(form);
    setPage(1);
  }

  function handleReset() {
    setForm(EMPTY_FORM);
    setApplied(EMPTY_FORM);
    setPage(1);
  }

  function handleToggleStreaming() {
    setIsStreaming((prev) => !prev);
  }

  const total = query.data?.total ?? 0;
  const pageSize = query.data?.page_size ?? DEFAULT_PAGE_SIZE;
  const totalPages = total > 0 ? Math.max(1, Math.ceil(total / pageSize)) : 1;

  return (
    <div className="space-y-6">
      <header className="space-y-2">
        <div className="flex flex-wrap items-center justify-between gap-3">
          <div className="space-y-1">
            <h1 className="text-2xl font-semibold">日志</h1>
            <p className="text-sm text-muted-foreground">
              OBSERVABILITY §2.6 字面 8 个筛选字段;jsonl 写入接通(P4-acceptance)前
              可能显示空列表。
            </p>
          </div>
          <div className="flex items-center gap-2">
            <span
              className="text-xs text-muted-foreground"
              data-testid="logs-stream-status"
            >
              {isStreaming
                ? `实时模式(${describeStreamStatus(stream.status)})`
                : "静态查询"}
            </span>
            <Button
              variant={isStreaming ? "default" : "outline"}
              onClick={handleToggleStreaming}
              data-testid="logs-stream-toggle"
            >
              {isStreaming ? "关闭实时模式" : "开启实时模式"}
            </Button>
          </div>
        </div>
      </header>

      <section
        data-testid="logs-filter-bar"
        className="rounded-lg border border-border bg-card p-4"
      >
        <div className="grid grid-cols-1 gap-3 sm:grid-cols-2 lg:grid-cols-4">
          <FilterField
            label="Workspace ID"
            value={form.workspace_id}
            onChange={(v) => updateField("workspace_id", v)}
            testId="logs-filter-workspace"
            placeholder="例:p3e2e-demo"
          />
          <FilterField
            label="Chapter ID"
            value={form.chapter_id}
            onChange={(v) => updateField("chapter_id", v)}
            testId="logs-filter-chapter"
            placeholder="例:v1c001"
          />
          <FilterField
            label="Trace ID"
            value={form.trace_id}
            onChange={(v) => updateField("trace_id", v)}
            testId="logs-filter-trace"
            placeholder="例:v1c001-rewrite0"
          />
          <FilterField
            label="Request ID"
            value={form.request_id}
            onChange={(v) => updateField("request_id", v)}
            testId="logs-filter-request"
            placeholder="例:req_abc123"
          />
          <FilterField
            label="Event"
            value={form.event}
            onChange={(v) => updateField("event", v)}
            testId="logs-filter-event"
            placeholder="例:chapter_draft_completed"
          />
          <FilterSelect
            label="Level"
            value={form.level}
            onChange={(v) => updateField("level", v)}
            testId="logs-filter-level"
            options={[
              { value: "", label: "全部" },
              { value: "DEBUG", label: "DEBUG" },
              { value: "INFO", label: "INFO" },
              { value: "WARNING", label: "WARNING" },
              { value: "ERROR", label: "ERROR" },
            ]}
          />
          <FilterField
            label="Since"
            value={form.since}
            onChange={(v) => updateField("since", v)}
            testId="logs-filter-since"
            type="datetime-local"
          />
          <FilterField
            label="Until"
            value={form.until}
            onChange={(v) => updateField("until", v)}
            testId="logs-filter-until"
            type="datetime-local"
          />
        </div>
        <div className="mt-4 flex items-center justify-between gap-2">
          <span className="text-xs text-muted-foreground">
            {isStreaming
              ? "实时模式下筛选不生效;buffer 是无过滤流(P5+ 加 client-side 过滤)"
              : ""}
          </span>
          <div className="flex items-center gap-2">
            <Button
              variant="outline"
              onClick={handleReset}
              data-testid="logs-filter-reset"
              disabled={isStreaming}
            >
              重置
            </Button>
            <Button
              onClick={handleApply}
              data-testid="logs-filter-apply"
              disabled={isStreaming}
            >
              应用筛选
            </Button>
          </div>
        </div>
      </section>

      {isStreaming ? (
        <LogsStreamBody
          stream={stream}
          onSelectEntry={(e) => setSelectedEntry(e)}
        />
      ) : (
        <LogsListBody
          query={query}
          onSelectEntry={(e) => setSelectedEntry(e)}
          page={page}
          totalPages={totalPages}
          total={total}
          onPrevPage={() => setPage((p) => Math.max(1, p - 1))}
          onNextPage={() => setPage((p) => Math.min(totalPages, p + 1))}
        />
      )}

      <LogDetailDrawer
        open={selectedEntry !== null}
        onOpenChange={(open) => {
          if (!open) setSelectedEntry(null);
        }}
        entry={selectedEntry}
      />
    </div>
  );
}

function describeStreamStatus(status: LogsStreamStatus): string {
  if (status === "open") return "已连接";
  if (status === "connecting") return "连接中…";
  if (status === "reconnecting") return "重连中…";
  if (status === "closed") return "已断开";
  return "未连接";
}

interface FilterFieldProps {
  label: string;
  value: string;
  onChange: (value: string) => void;
  testId: string;
  placeholder?: string;
  type?: string;
}

function FilterField(props: FilterFieldProps) {
  return (
    <div className="space-y-1">
      <label
        className="text-xs text-muted-foreground"
        htmlFor={props.testId}
      >
        {props.label}
      </label>
      <input
        id={props.testId}
        data-testid={props.testId}
        type={props.type ?? "text"}
        value={props.value}
        placeholder={props.placeholder}
        onChange={(e) => props.onChange(e.target.value)}
        className="h-9 w-full rounded-md border border-input bg-background px-3 py-1 text-sm shadow-sm focus-visible:outline-none focus-visible:ring-1 focus-visible:ring-ring"
      />
    </div>
  );
}

interface FilterSelectProps {
  label: string;
  value: string;
  onChange: (value: string) => void;
  testId: string;
  options: ReadonlyArray<{ value: string; label: string }>;
}

function FilterSelect(props: FilterSelectProps) {
  return (
    <div className="space-y-1">
      <label
        className="text-xs text-muted-foreground"
        htmlFor={props.testId}
      >
        {props.label}
      </label>
      <select
        id={props.testId}
        data-testid={props.testId}
        value={props.value}
        onChange={(e) => props.onChange(e.target.value)}
        className="h-9 w-full rounded-md border border-input bg-background px-3 py-1 text-sm shadow-sm focus-visible:outline-none focus-visible:ring-1 focus-visible:ring-ring"
      >
        {props.options.map((o) => (
          <option key={o.value} value={o.value}>
            {o.label}
          </option>
        ))}
      </select>
    </div>
  );
}

interface LogsListBodyProps {
  query: ReturnType<typeof useLogsListQuery>;
  onSelectEntry: (entry: LogEntry) => void;
  page: number;
  totalPages: number;
  total: number;
  onPrevPage: () => void;
  onNextPage: () => void;
}

function LogsListBody(props: LogsListBodyProps) {
  const { query, onSelectEntry } = props;
  if (query.isPending && query.fetchStatus !== "idle") {
    return (
      <div
        data-testid="logs-list-loading"
        className="rounded border border-border p-6 text-sm text-muted-foreground"
      >
        加载中…
      </div>
    );
  }
  if (query.error) {
    return (
      <div
        data-testid="logs-list-error"
        className="rounded border border-destructive bg-destructive/10 p-6 text-sm text-destructive"
      >
        加载日志失败:{formatError(query.error)}
      </div>
    );
  }
  const items = query.data?.items ?? [];
  if (items.length === 0) {
    return (
      <div
        data-testid="logs-list-empty"
        className="rounded border border-dashed border-border p-6 text-sm text-muted-foreground"
      >
        无符合筛选条件的日志条目。
        <span className="ml-1 text-xs">
          (jsonl 写入接通 P4-acceptance 真 LLM 重跑时落地,见 P4-B-05 §6 留观)
        </span>
      </div>
    );
  }
  return (
    <div className="space-y-3">
      <div className="overflow-x-auto rounded border border-border">
        <table
          data-testid="logs-list-table"
          className="w-full text-left text-xs"
        >
          <thead className="bg-muted/50">
            <tr className="border-b border-border text-muted-foreground">
              <th className="px-2 py-2 font-normal">时间</th>
              <th className="px-2 py-2 font-normal">Level</th>
              <th className="px-2 py-2 font-normal">Event</th>
              <th className="px-2 py-2 font-normal">Workspace</th>
              <th className="px-2 py-2 font-normal">Chapter</th>
              <th className="px-2 py-2 font-normal">Trace</th>
              <th className="px-2 py-2 font-normal">Logger</th>
            </tr>
          </thead>
          <tbody>
            {items.map((entry, i) => (
              <tr
                key={`${entry.timestamp}-${entry.event}-${i}`}
                data-testid="logs-list-row"
                className="cursor-pointer border-b border-border/50 transition-colors last:border-0 hover:bg-muted/40"
                onClick={() => onSelectEntry(entry)}
              >
                <td className="px-2 py-1 font-mono">{entry.timestamp}</td>
                <td className="px-2 py-1">
                  <span
                    className={`rounded px-1.5 py-0.5 text-[10px] font-medium ${levelBadge(
                      entry.level,
                    )}`}
                  >
                    {entry.level}
                  </span>
                </td>
                <td className="px-2 py-1 font-mono">{entry.event}</td>
                <td className="px-2 py-1 font-mono">
                  {entry.workspace_id ?? "—"}
                </td>
                <td className="px-2 py-1 font-mono">
                  {entry.chapter_id ?? "—"}
                </td>
                <td className="px-2 py-1 font-mono">
                  {entry.trace_id ? (
                    <Link
                      to={`/logs/trace/${encodeURIComponent(entry.trace_id)}`}
                      className="underline"
                      onClick={(e) => e.stopPropagation()}
                      data-testid="logs-list-trace-link"
                    >
                      {entry.trace_id}
                    </Link>
                  ) : (
                    "—"
                  )}
                </td>
                <td className="px-2 py-1 font-mono">{entry.logger}</td>
              </tr>
            ))}
          </tbody>
        </table>
      </div>
      <div className="flex items-center justify-between text-sm text-muted-foreground">
        <span data-testid="logs-list-total">
          共 {props.total} 条 · 第 {props.page} / {props.totalPages} 页
        </span>
        <div className="flex items-center gap-2">
          <Button
            variant="outline"
            size="sm"
            onClick={props.onPrevPage}
            disabled={props.page <= 1}
            data-testid="logs-list-prev-page"
          >
            上一页
          </Button>
          <Button
            variant="outline"
            size="sm"
            onClick={props.onNextPage}
            disabled={props.page >= props.totalPages}
            data-testid="logs-list-next-page"
          >
            下一页
          </Button>
        </div>
      </div>
    </div>
  );
}

interface LogsTracePageProps {
  traceId: string;
}

function LogsTracePage({ traceId }: LogsTracePageProps) {
  const query = useLogsTraceQuery(traceId);
  return (
    <div className="space-y-6">
      <header className="space-y-1">
        <div className="flex items-center justify-between">
          <h1 className="text-2xl font-semibold">Trace 视图</h1>
          <Link
            to="/logs"
            className="text-sm underline"
            data-testid="logs-trace-back"
          >
            ← 返回日志列表
          </Link>
        </div>
        <p className="text-sm text-muted-foreground">
          ID: <code className="font-mono">{traceId}</code>
        </p>
      </header>
      <TraceView
        data={query.data}
        isLoading={query.isPending && query.fetchStatus !== "idle"}
        error={query.error}
        traceOrRequestId={traceId}
      />
    </div>
  );
}

interface LogsStreamBodyProps {
  stream: ReturnType<typeof useLogsStream>;
  onSelectEntry: (entry: LogEntry) => void;
}

function LogsStreamBody(props: LogsStreamBodyProps) {
  const { stream, onSelectEntry } = props;
  const scrollRef = useRef<HTMLDivElement | null>(null);
  // 滚动锁定:每次 buffer 增长把容器滚到底部(暂停时不动,因为 buffer 不变)。
  useEffect(() => {
    const node = scrollRef.current;
    if (node) {
      node.scrollTop = node.scrollHeight;
    }
  }, [stream.buffer]);

  const items = stream.buffer;

  return (
    <div className="space-y-3" data-testid="logs-stream-body">
      <div className="flex items-center justify-between gap-2 rounded border border-border bg-card p-3 text-xs">
        <div className="flex items-center gap-3 text-muted-foreground">
          <span data-testid="logs-stream-count">已收 {items.length} 条</span>
          {stream.isPaused && (
            <span
              data-testid="logs-stream-pending"
              className="rounded bg-amber-100 px-1.5 py-0.5 text-amber-700"
            >
              暂停中(累积 {stream.pendingCount} 条)
            </span>
          )}
          {stream.droppedCount > 0 && (
            <span
              data-testid="logs-stream-dropped"
              className="rounded bg-rose-100 px-1.5 py-0.5 text-rose-700"
            >
              超出 buffer 已丢弃 {stream.droppedCount} 条
            </span>
          )}
        </div>
        <div className="flex items-center gap-2">
          {stream.isPaused ? (
            <Button
              size="sm"
              variant="outline"
              onClick={stream.resume}
              data-testid="logs-stream-resume"
            >
              恢复
            </Button>
          ) : (
            <Button
              size="sm"
              variant="outline"
              onClick={stream.pause}
              data-testid="logs-stream-pause"
            >
              暂停
            </Button>
          )}
          <Button
            size="sm"
            variant="outline"
            onClick={stream.clear}
            data-testid="logs-stream-clear"
          >
            清屏
          </Button>
        </div>
      </div>

      {items.length === 0 ? (
        <div
          data-testid="logs-stream-empty"
          className="rounded border border-dashed border-border p-6 text-sm text-muted-foreground"
        >
          {stream.status === "open"
            ? "已连接,等待新日志(jsonl 写入接通 P4-acceptance 后会持续 append)…"
            : `当前连接状态:${describeStreamStatus(stream.status)}`}
        </div>
      ) : (
        <div
          ref={scrollRef}
          className="max-h-[60vh] overflow-y-auto rounded border border-border"
          data-testid="logs-stream-scroll"
        >
          <table
            data-testid="logs-stream-table"
            className="w-full text-left text-xs"
          >
            <thead className="sticky top-0 bg-muted/80 backdrop-blur">
              <tr className="border-b border-border text-muted-foreground">
                <th className="px-2 py-2 font-normal">时间</th>
                <th className="px-2 py-2 font-normal">Level</th>
                <th className="px-2 py-2 font-normal">Event</th>
                <th className="px-2 py-2 font-normal">Workspace</th>
                <th className="px-2 py-2 font-normal">Chapter</th>
                <th className="px-2 py-2 font-normal">Trace</th>
                <th className="px-2 py-2 font-normal">Logger</th>
              </tr>
            </thead>
            <tbody>
              {items.map((entry, i) => (
                <tr
                  key={`${entry.timestamp}-${entry.event}-${i}`}
                  data-testid="logs-stream-row"
                  className="cursor-pointer border-b border-border/50 transition-colors last:border-0 hover:bg-muted/40"
                  onClick={() => onSelectEntry(entry)}
                >
                  <td className="px-2 py-1 font-mono">{entry.timestamp}</td>
                  <td className="px-2 py-1">
                    <span
                      className={`rounded px-1.5 py-0.5 text-[10px] font-medium ${levelBadge(
                        entry.level,
                      )}`}
                    >
                      {entry.level}
                    </span>
                  </td>
                  <td className="px-2 py-1 font-mono">{entry.event}</td>
                  <td className="px-2 py-1 font-mono">
                    {entry.workspace_id ?? "—"}
                  </td>
                  <td className="px-2 py-1 font-mono">
                    {entry.chapter_id ?? "—"}
                  </td>
                  <td className="px-2 py-1 font-mono">
                    {entry.trace_id ? (
                      <Link
                        to={`/logs/trace/${encodeURIComponent(entry.trace_id)}`}
                        className="underline"
                        onClick={(e) => e.stopPropagation()}
                        data-testid="logs-stream-trace-link"
                      >
                        {entry.trace_id}
                      </Link>
                    ) : (
                      "—"
                    )}
                  </td>
                  <td className="px-2 py-1 font-mono">{entry.logger}</td>
                </tr>
              ))}
            </tbody>
          </table>
        </div>
      )}
    </div>
  );
}

function levelBadge(level: string): string {
  const lv = level.toUpperCase();
  if (lv === "ERROR") return "bg-rose-100 text-rose-700";
  if (lv === "WARNING") return "bg-amber-100 text-amber-700";
  if (lv === "INFO") return "bg-sky-100 text-sky-700";
  if (lv === "DEBUG") return "bg-muted text-muted-foreground";
  return "bg-muted text-muted-foreground";
}

function formatError(error: unknown): string {
  if (isApiError(error)) {
    return `${error.code}: ${error.message}`;
  }
  if (error instanceof Error) return error.message;
  return "未知错误";
}
