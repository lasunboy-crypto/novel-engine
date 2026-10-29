/**
 * PipelineLiveOutput(P6-F-02 RunControlPage 实时输出窗口)。
 *
 * 字面对齐 FRONTEND_SPEC §4.10:
 * - 实时输出窗口(WS 推送当前章节状态、正在调用什么模型、token 进度)
 * - 异常时高亮 + 跳转链接到对应章节人工介入页
 *
 * WS 双订阅(对齐 P6-B-02 marker §"P6-F-02 RunControlPage WS subscribe"):
 *
 * - `pipeline.events` 全局频道:接 6 类 event(``pipeline_started`` /
 *   ``pipeline_paused`` / ``pipeline_resumed`` / ``pipeline_stopped`` /
 *   ``chapter_started`` / ``chapter_finished``);event.data 含 `workspace_id`
 *   字段,**前端按当前工作区过滤**避免跨工作区事件混杂。
 * - `chapter.{current_chapter}.events` 局部频道:接当前正在写的章节
 *   state_transition(P2+ token 进度若有也接);**chapter_id 变化时自动切订阅**
 *   (`useChapterEvents` hook 内部 useEffect 处理)。
 *
 * ## 事件 → 输出窗口模型
 *
 * 内部维护 `events: TimelineEntry[]` 数组(最近 50 条循环):每条含
 * `{timestamp, severity, text}`;UI 渲染逆序(最新在顶部)。pipeline_paused
 * reason=`chapter_invalidated` 时 severity=warn + 加"跳转到 chapter
 * editor"链接;error_details 出现时 severity=error + 加跳转。
 *
 * ## task done 兜底
 *
 * 不在本组件做。AppShell 全局订阅 active task store 中所有 task_id 的 done
 * 事件 + 在 RunControlPage 层面 chapter_finished 时 invalidate
 * `["chapters", workspaceId]`。
 */

import { useCallback, useEffect, useState } from "react";
import { Link } from "react-router-dom";

import { useChapterEvents } from "@/hooks/useChapterEvents";
import { getWsClient } from "@/ws/client";
import { cn } from "@/lib/utils";

const PIPELINE_EVENTS_CHANNEL = "pipeline.events";
const MAX_TIMELINE_ENTRIES = 50;

type Severity = "info" | "success" | "warn" | "error";

interface TimelineEntry {
  /** 用于 React key + UI 显示。 */
  id: string;
  timestamp: string;
  severity: Severity;
  text: string;
  /** 与该条事件相关的章节 ID(若有,显示跳转链接)。 */
  chapterId?: string | null;
}

interface PipelineEventData {
  run_id?: string;
  workspace_id?: string;
  scope?: string;
  chapter_id?: string;
  chapter_ids?: string[];
  current_chapter?: string | null;
  completed_chapter_ids?: string[];
  completed_count?: number;
  total_count?: number;
  reason?: string;
  profile_override?: string | null;
}

interface PipelineLiveOutputProps {
  workspaceId: string;
  /** 当前正在写的章节(GET /status `current_chapter`);用于 chapter.events 订阅。 */
  currentChapterId: string | null;
  /** 接 `chapter_finished` 事件时回调,父组件用来 invalidate `chapters` query。 */
  onChapterFinished?: (chapterId: string) => void;
  /** 接任一 pipeline 生命周期事件时回调，父组件据此重新读取权威状态。 */
  onPipelineStatusChanged?: () => void;
}

export function PipelineLiveOutput(props: PipelineLiveOutputProps) {
  const {
    workspaceId,
    currentChapterId,
    onChapterFinished,
    onPipelineStatusChanged,
  } = props;
  const [entries, setEntries] = useState<TimelineEntry[]>([]);

  const appendEntry = useCallback((entry: Omit<TimelineEntry, "id">) => {
    setEntries((prev) => {
      const id = `${Date.now()}-${prev.length}-${Math.random().toString(36).slice(2, 8)}`;
      const next = [{ ...entry, id }, ...prev];
      // 保留最新 MAX_TIMELINE_ENTRIES 条
      return next.slice(0, MAX_TIMELINE_ENTRIES);
    });
  }, []);

  // 订阅 pipeline.events 全局频道(按 workspace_id 过滤)。
  useEffect(() => {
    if (!workspaceId) return;
    const client = getWsClient();
    const unsubscribe = client.subscribe<PipelineEventData>(
      PIPELINE_EVENTS_CHANNEL,
      (frame) => {
        const data = frame.data ?? {};
        if (data.workspace_id && data.workspace_id !== workspaceId) {
          // 跨工作区事件 — 静默忽略
          return;
        }
        const { event, timestamp } = frame;
        const entry = pipelineEventToEntry(event, data, timestamp);
        if (entry) appendEntry(entry);
        // 通知父组件触发 query invalidate
        if (event === "chapter_finished" && data.chapter_id) {
          onChapterFinished?.(data.chapter_id);
        }
        if (
          event === "pipeline_started" ||
          event === "pipeline_paused" ||
          event === "pipeline_resumed" ||
          event === "pipeline_stopped"
        ) {
          onPipelineStatusChanged?.();
        }
      },
    );
    return unsubscribe;
  }, [
    workspaceId,
    appendEntry,
    onChapterFinished,
    onPipelineStatusChanged,
  ]);

  // 订阅当前章节 state_transition(局部频道)。
  useChapterEvents(currentChapterId ?? undefined, {
    onStateTransition: (transition) => {
      appendEntry({
        timestamp: new Date().toISOString(),
        severity: transition.to === "passed" ? "success" : "info",
        text: `章节 ${currentChapterId} 状态:${transition.from} → ${transition.to}`,
        chapterId: currentChapterId ?? undefined,
      });
    },
  });

  const handleClear = () => setEntries([]);

  return (
    <section
      data-testid="pipeline-live-output"
      className="space-y-2 rounded border border-border bg-card p-4"
    >
      <div className="flex items-center justify-between">
        <h3 className="text-sm font-semibold uppercase tracking-wide text-muted-foreground">
          实时输出
        </h3>
        <button
          type="button"
          onClick={handleClear}
          className="text-xs text-muted-foreground hover:text-foreground"
          data-testid="pipeline-output-clear"
        >
          清空
        </button>
      </div>
      {entries.length === 0 ? (
        <p className="rounded border border-dashed border-border p-3 text-xs text-muted-foreground">
          等待 WS 推送(`pipeline.events` + 当前章节
          `chapter.&#123;id&#125;.events`)。点"开始"后会看到 pipeline_started
          → chapter_started → 状态翻转 → chapter_finished 序列。
        </p>
      ) : (
        <ol
          data-testid="pipeline-output-list"
          className="max-h-64 space-y-1 overflow-y-auto"
        >
          {entries.map((e) => (
            <TimelineRow key={e.id} entry={e} workspaceId={workspaceId} />
          ))}
        </ol>
      )}
    </section>
  );
}

function pipelineEventToEntry(
  event: string,
  data: PipelineEventData,
  timestamp: string,
): Omit<TimelineEntry, "id"> | null {
  switch (event) {
    case "pipeline_started":
      return {
        timestamp,
        severity: "info",
        text: `流水线已启动:scope=${data.scope ?? "?"},共 ${data.chapter_ids?.length ?? 0} 章${
          data.profile_override ? `,profile=${data.profile_override}` : ""
        }`,
      };
    case "pipeline_paused": {
      // v2 w1-s4:孤儿章 / 驳回卡 / 成本护栏停跑(w1-s1/s3)与 invalidated
      // 同级 warn 高亮 + 章节跳转(cost_guard 是全局态,无章节链接)。
      const warnReasons = [
        "chapter_invalidated",
        "chapter_escalated",
        "card_rejected",
        "continuity_ledger_not_ready",
        "continuity_checkpoint_corruption",
        "memory_enrichment_not_ready",
        "memory_enrichment_failed",
        "chapter_operation_safety_block",
        "cost_guard_exceeded",
        "provider_call_cap_exceeded",
      ];
      const isWarn = warnReasons.includes(data.reason ?? "");
      const hasChapterLink =
        isWarn &&
        data.reason !== "cost_guard_exceeded" &&
        data.reason !== "provider_call_cap_exceeded";
      return {
        timestamp,
        severity: isWarn ? "warn" : "info",
        text: `流水线已暂停${data.reason ? `(reason=${data.reason})` : ""}${
          data.current_chapter ? `,当前章节 ${data.current_chapter}` : ""
        }`,
        chapterId: hasChapterLink
          ? data.chapter_id ?? data.current_chapter ?? null
          : null,
      };
    }
    case "pipeline_resumed":
      return {
        timestamp,
        severity: "info",
        text: `流水线已恢复${
          data.current_chapter ? `,当前章节 ${data.current_chapter}` : ""
        }`,
      };
    case "pipeline_stopped":
      return {
        timestamp,
        severity:
          data.reason === "completed" ? "success" : "info",
        text: `流水线已${
          data.reason === "completed" ? "完成" : "终止"
        }${data.reason ? `(reason=${data.reason})` : ""},已完成 ${
          data.completed_chapter_ids?.length ?? 0
        } 章`,
      };
    case "chapter_started":
      return {
        timestamp,
        severity: "info",
        text: `开始写作章节 ${data.chapter_id ?? "?"}`,
        chapterId: data.chapter_id,
      };
    case "chapter_finished":
      return {
        timestamp,
        severity: "success",
        text: `章节 ${data.chapter_id ?? "?"} 写作完成(${
          data.completed_count ?? 0
        }/${data.total_count ?? 0})`,
        chapterId: data.chapter_id,
      };
    default:
      // 未识别事件:静默忽略(前向兼容,后端 P7+ 可能加新 event)
      return null;
  }
}

interface TimelineRowProps {
  entry: TimelineEntry;
  workspaceId: string;
}

function TimelineRow(props: TimelineRowProps) {
  const { entry, workspaceId } = props;
  const tone = severityClass(entry.severity);
  // 章节人工介入页跳转(对齐 FRONTEND_SPEC §4.10 字面"异常时高亮 + 跳转链接")
  const chapterEditorPath = entry.chapterId
    ? `/chapters/${encodeURIComponent(entry.chapterId)}`
    : null;
  const cardReviewPath =
    entry.chapterId && workspaceId
      ? `/w/${encodeURIComponent(workspaceId)}/cards/${encodeURIComponent(
          entry.chapterId,
        )}/review`
      : null;
  return (
    <li
      data-testid="pipeline-output-row"
      className={cn(
        "flex flex-col gap-0.5 rounded border px-2 py-1.5 text-xs",
        tone,
      )}
    >
      <div className="flex items-start gap-2">
        <span className="text-[10px] tabular-nums text-muted-foreground">
          {formatTimestamp(entry.timestamp)}
        </span>
        <span className="flex-1 break-words">{entry.text}</span>
      </div>
      {(entry.severity === "warn" || entry.severity === "error") &&
        chapterEditorPath && (
          <div className="flex flex-wrap gap-2 pl-12 text-[10px]">
            <Link
              to={chapterEditorPath}
              className="underline hover:text-foreground"
              data-testid="pipeline-jump-editor"
            >
              → 打开章节编辑器
            </Link>
            {cardReviewPath && (
              <Link
                to={cardReviewPath}
                className="underline hover:text-foreground"
                data-testid="pipeline-jump-card-review"
              >
                → 打开章节卡审核
              </Link>
            )}
          </div>
        )}
    </li>
  );
}

function severityClass(severity: Severity): string {
  switch (severity) {
    case "success":
      return "border-emerald-200 bg-emerald-50 text-emerald-900";
    case "warn":
      return "border-amber-300 bg-amber-50 text-amber-900";
    case "error":
      return "border-destructive bg-destructive/10 text-destructive";
    case "info":
    default:
      return "border-border bg-background text-foreground";
  }
}

function formatTimestamp(iso: string): string {
  // ISO 8601 Z → HH:MM:SS(本地时区)
  try {
    const d = new Date(iso);
    if (Number.isNaN(d.getTime())) return iso;
    const hh = String(d.getHours()).padStart(2, "0");
    const mm = String(d.getMinutes()).padStart(2, "0");
    const ss = String(d.getSeconds()).padStart(2, "0");
    return `${hh}:${mm}:${ss}`;
  } catch {
    return iso;
  }
}
