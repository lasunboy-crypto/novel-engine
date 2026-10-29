/**
 * ChapterSummaryTabContent — OutlinePage 第 6 tab(v1.0.x wave 4 session 5)。
 *
 * 展示 + 编辑 chapter_summaries 表(LLM 在每章 approved / force_pass 后自动抽取
 * 落库;wave 4 session 3a 引入)。本组件是用户事后查看 + 编辑入口,不调 LLM
 * 重新抽取(重新抽取入口在 backfill 脚本 / v1.1 加按钮)。
 *
 * 列表 + 行内编辑模式:每章一卡,展开后可改 4 字段(summary_text /
 * pov_character / scene_setting / unfinished_actions 用换行分隔编辑);
 * key_events / character_interactions 是结构化 JSON 列表,只读显示,留 v1.1
 * 加结构化表单。
 */

import { useEffect, useState } from "react";

import {
  useChapterSummariesQuery,
  useUpdateChapterSummaryMutation,
  type ChapterSummaryOut,
  type ChapterSummaryUpdateIn,
} from "@/api/chapter_summary";
import { isApiError } from "@/api/errors";
import { showApiErrorToast, showSuccessToast } from "@/components/Toast";
import { Button } from "@/components/ui/button";

interface ChapterSummaryTabContentProps {
  workspaceId: string;
}

export function ChapterSummaryTabContent(
  props: ChapterSummaryTabContentProps,
) {
  const { workspaceId } = props;
  const query = useChapterSummariesQuery(workspaceId);

  if (query.isPending) {
    return (
      <div className="rounded border border-border p-3 text-sm text-muted-foreground">
        加载章节摘要…
      </div>
    );
  }
  if (query.error) {
    const message = isApiError(query.error)
      ? `${query.error.code}: ${query.error.message}`
      : query.error instanceof Error
        ? query.error.message
        : "未知错误";
    return (
      <div className="rounded border border-destructive bg-destructive/10 p-3 text-sm text-destructive">
        加载章节摘要失败:{message}
      </div>
    );
  }

  const summaries = query.data ?? [];
  if (summaries.length === 0) {
    return (
      <div
        className="rounded border border-dashed border-border p-4 text-sm text-muted-foreground"
        data-testid="chapter-summary-empty"
      >
        本工作区尚无章节摘要。摘要在每章 approved / force_pass 时由 LLM
        自动抽取(wave 4 session 3a 起);旧工作区可在主仓库根目录跑
        <code className="mx-1">uv run python scripts/backfill_chapter_summaries.py --apply</code>
        批量回填。
      </div>
    );
  }

  return (
    <div className="space-y-2" data-testid="chapter-summary-list">
      <p className="text-xs text-muted-foreground">
        共 {summaries.length} 条摘要(按 chapter_id 升序)。点击"编辑"逐条
        修改;摘要不影响已写章节,只供下游 ChapterCardGenerator
        derive_context 注入前 N 章长程上下文用。
      </p>
      <ul className="space-y-2">
        {summaries.map((s) => (
          <ChapterSummaryCard
            key={s.chapter_id}
            workspaceId={workspaceId}
            summary={s}
          />
        ))}
      </ul>
    </div>
  );
}

interface ChapterSummaryCardProps {
  workspaceId: string;
  summary: ChapterSummaryOut;
}

function ChapterSummaryCard(props: ChapterSummaryCardProps) {
  const { workspaceId, summary } = props;
  const [editing, setEditing] = useState(false);
  const updateMutation = useUpdateChapterSummaryMutation();

  // 本地草稿:editing=true 时初始化为当前服务端值
  const [draft, setDraft] = useState<{
    summary_text: string;
    pov_character: string;
    scene_setting: string;
    unfinished_actions: string;
  }>(() => ({
    summary_text: summary.summary_text,
    pov_character: summary.pov_character ?? "",
    scene_setting: summary.scene_setting ?? "",
    unfinished_actions: (summary.unfinished_actions ?? []).join("\n"),
  }));

  // server-side 数据变化时重置草稿(除非正在编辑)
  useEffect(() => {
    if (!editing) {
      setDraft({
        summary_text: summary.summary_text,
        pov_character: summary.pov_character ?? "",
        scene_setting: summary.scene_setting ?? "",
        unfinished_actions: (summary.unfinished_actions ?? []).join("\n"),
      });
    }
  }, [editing, summary]);

  const handleSave = () => {
    const body: ChapterSummaryUpdateIn = {};
    if (draft.summary_text !== summary.summary_text) {
      body.summary_text = draft.summary_text;
    }
    if (draft.pov_character !== (summary.pov_character ?? "")) {
      body.pov_character = draft.pov_character;
    }
    if (draft.scene_setting !== (summary.scene_setting ?? "")) {
      body.scene_setting = draft.scene_setting;
    }
    const newUnfinished = draft.unfinished_actions
      .split("\n")
      .map((s) => s.trim())
      .filter((s) => s.length > 0);
    const oldUnfinished = summary.unfinished_actions ?? [];
    if (JSON.stringify(newUnfinished) !== JSON.stringify(oldUnfinished)) {
      body.unfinished_actions = newUnfinished;
    }

    if (Object.keys(body).length === 0) {
      setEditing(false);
      return;
    }

    updateMutation.mutate(
      { workspaceId, chapterId: summary.chapter_id, body },
      {
        onSuccess: () => {
          showSuccessToast(`章节摘要已保存(${summary.chapter_id})`);
          setEditing(false);
        },
        onError: (err) => showApiErrorToast(err),
      },
    );
  };

  return (
    <li
      className="rounded border border-border bg-secondary/30 p-3 space-y-2"
      data-testid={`chapter-summary-card-${summary.chapter_id}`}
    >
      <div className="flex flex-wrap items-baseline justify-between gap-2">
        <div>
          <p className="text-sm font-semibold">
            {summary.chapter_id}
            <span className="ml-2 rounded bg-secondary px-1.5 py-0.5 text-xs font-normal text-muted-foreground">
              {summary.extraction_trigger}
            </span>
          </p>
          <p className="text-xs text-muted-foreground">
            模型: <code>{summary.model_used}</code> · 抽取时间:{" "}
            {new Date(summary.extracted_at).toLocaleString("zh-CN")}
          </p>
        </div>
        <div className="flex gap-1">
          {!editing ? (
            <Button
              size="sm"
              variant="outline"
              onClick={() => setEditing(true)}
              data-testid={`chapter-summary-edit-${summary.chapter_id}`}
            >
              编辑
            </Button>
          ) : (
            <>
              <Button
                size="sm"
                onClick={handleSave}
                disabled={updateMutation.isPending}
                data-testid={`chapter-summary-save-${summary.chapter_id}`}
              >
                {updateMutation.isPending ? "保存中…" : "保存"}
              </Button>
              <Button
                size="sm"
                variant="outline"
                onClick={() => setEditing(false)}
                disabled={updateMutation.isPending}
              >
                取消
              </Button>
            </>
          )}
        </div>
      </div>

      <div className="space-y-2 text-xs">
        <SummaryField
          label="摘要正文"
          editing={editing}
          value={draft.summary_text}
          onChange={(v) => setDraft((d) => ({ ...d, summary_text: v }))}
          rawValue={summary.summary_text}
          rows={4}
        />
        <SummaryField
          label="主视角角色"
          editing={editing}
          value={draft.pov_character}
          onChange={(v) => setDraft((d) => ({ ...d, pov_character: v }))}
          rawValue={summary.pov_character ?? "—"}
          rows={1}
        />
        <SummaryField
          label="主场景"
          editing={editing}
          value={draft.scene_setting}
          onChange={(v) => setDraft((d) => ({ ...d, scene_setting: v }))}
          rawValue={summary.scene_setting ?? "—"}
          rows={2}
        />
        <SummaryField
          label="未完动作(每行一条)"
          editing={editing}
          value={draft.unfinished_actions}
          onChange={(v) => setDraft((d) => ({ ...d, unfinished_actions: v }))}
          rawValue={
            (summary.unfinished_actions ?? []).length > 0
              ? (summary.unfinished_actions ?? []).join("\n")
              : "—"
          }
          rows={3}
        />
        <ReadOnlyJson label="关键事件(只读)" value={summary.key_events} />
        <ReadOnlyJson
          label="角色互动(只读)"
          value={summary.character_interactions}
        />
      </div>
    </li>
  );
}

function SummaryField(props: {
  label: string;
  editing: boolean;
  value: string;
  onChange: (next: string) => void;
  rawValue: string;
  rows: number;
}) {
  return (
    <div className="space-y-1">
      <p className="text-xs font-semibold uppercase tracking-wide text-muted-foreground">
        {props.label}
      </p>
      {props.editing ? (
        <textarea
          value={props.value}
          onChange={(e) => props.onChange(e.target.value)}
          rows={props.rows}
          className="w-full rounded border border-border bg-background px-2 py-1 text-xs"
        />
      ) : (
        <p className="whitespace-pre-wrap text-sm leading-relaxed">
          {props.rawValue}
        </p>
      )}
    </div>
  );
}

function ReadOnlyJson(props: { label: string; value: unknown }) {
  if (!Array.isArray(props.value) || props.value.length === 0) {
    return (
      <div className="space-y-1">
        <p className="text-xs font-semibold uppercase tracking-wide text-muted-foreground">
          {props.label}
        </p>
        <p className="text-xs text-muted-foreground">(无)</p>
      </div>
    );
  }
  return (
    <div className="space-y-1">
      <p className="text-xs font-semibold uppercase tracking-wide text-muted-foreground">
        {props.label}
      </p>
      <pre className="overflow-x-auto rounded bg-background p-2 text-[10px] leading-relaxed">
        {JSON.stringify(props.value, null, 2)}
      </pre>
    </div>
  );
}
