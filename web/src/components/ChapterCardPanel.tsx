/**
 * ChapterCardPanel(P1-F-12 / v1.1 acceptance 修复2 followup):章节卡左栏。
 *
 * 默认只读 JSON 预览(`JSON.stringify(card, null, 2)`)。
 *
 * ## must_achieve 编辑(v1.1 acceptance 修复2 followup,孤儿章人工解决)
 *
 * `canEditMustAchieve=true`(= 章节 `human_intervention` 状态)时,header 出现
 * 「编辑必达事件」按钮 → 进编辑态:**只编 must_achieve 列表**(加/删/改条目),
 * 其余字段仍只读。保存 = 原卡完整对象 + 覆盖 must_achieve → PUT card(完整覆盖,
 * 保留 card_status 等不被破坏,否则后续 :write gate 会拦)。
 *
 * **为何只编 must_achieve**:Fix D 写前预检检出的死结根因就在 must_achieve(卡上
 * 列了一条已被前文做完的必达事件 → 永不可达成)。删掉那条再点「开始写作」→ Fix D
 * 重新预检 → 卡干净放行。其余内容字段(must_unresolved / forbidden / pov 等)引擎
 * 自身有下游校验守着,非 Fix D 病根,不放进本编辑器(避免范围扩张 + 误改大纲透传
 * 字段 chapter_focus/arc_beats 导致卡与大纲分叉)。
 *
 * ## 错误码分支(P1-F-12 决策 5)
 *
 * - `CHAPTER_CARD_NOT_FOUND`(404)/ `CHAPTER_CARD_INVALID`(422)/ 其他 → 各自 hint。
 * 任一栏失败不阻塞其他栏。
 */

import { useCallback, useEffect, useMemo, useState } from "react";

import {
  useChapterCardQuery,
  useUpdateChapterCardMutation,
  type ChapterCardOut,
} from "@/api/chapters";
import { isApiError } from "@/api/errors";
import { Button } from "@/components/ui/button";
import { showApiErrorToast, showSuccessToast } from "@/components/Toast";

interface ChapterCardPanelProps {
  workspaceId: string | null | undefined;
  chapterId: string | null | undefined;
  /**
   * v1.1 acceptance 修复2 followup:章节 `human_intervention` 时为 true,解锁
   * 「编辑必达事件」。默认 false(正常章节只读,防误改卡导致与大纲分叉)。
   */
  canEditMustAchieve?: boolean;
}

export function ChapterCardPanel(props: ChapterCardPanelProps) {
  const query = useChapterCardQuery(props.workspaceId, props.chapterId);
  const json = useMemo(
    () => (query.data ? JSON.stringify(query.data, null, 2) : ""),
    [query.data],
  );
  const [editing, setEditing] = useState(false);

  return (
    <section
      data-testid="chapter-card-panel"
      className="flex h-full min-w-0 flex-col rounded border border-border bg-card"
    >
      <header className="flex items-center justify-between border-b border-border px-4 py-2">
        <h2 className="text-sm font-medium">章节卡(JSON)</h2>
        {props.canEditMustAchieve && query.data && !editing ? (
          <Button
            data-testid="card-edit-must-button"
            variant="outline"
            size="sm"
            onClick={() => setEditing(true)}
          >
            编辑必达事件
          </Button>
        ) : null}
      </header>
      {editing && query.data ? (
        <MustAchieveEditor
          workspaceId={props.workspaceId ?? ""}
          chapterId={props.chapterId ?? ""}
          card={query.data}
          onDone={() => setEditing(false)}
        />
      ) : (
        <PanelBody
          loading={query.isPending && query.fetchStatus !== "idle"}
          error={query.error}
          data={query.data}
          json={json}
          workspaceId={props.workspaceId ?? ""}
          chapterId={props.chapterId ?? ""}
        />
      )}
    </section>
  );
}

interface MustAchieveEditorProps {
  workspaceId: string;
  chapterId: string;
  card: ChapterCardOut;
  onDone: () => void;
}

/**
 * must_achieve 列表编辑器:加/删/改条目 → 保存(原卡 + 覆盖 must_achieve → PUT)。
 * 空条目保存时自动剔除(trim 后为空的不入列)。
 */
function MustAchieveEditor(props: MustAchieveEditorProps) {
  const mutation = useUpdateChapterCardMutation();
  const [items, setItems] = useState<string[]>(
    () => [...(props.card.must_achieve ?? [])],
  );
  // 卡切换(chapterId 变)时重置草稿,避免串台。
  useEffect(() => {
    setItems([...(props.card.must_achieve ?? [])]);
  }, [props.card]);

  const updateItem = useCallback((i: number, value: string) => {
    setItems((prev) => prev.map((v, idx) => (idx === i ? value : v)));
  }, []);
  const removeItem = useCallback((i: number) => {
    setItems((prev) => prev.filter((_, idx) => idx !== i));
  }, []);
  const addItem = useCallback(() => {
    setItems((prev) => [...prev, ""]);
  }, []);

  const handleSave = useCallback(async () => {
    const cleaned = items.map((s) => s.trim()).filter((s) => s.length > 0);
    try {
      await mutation.mutateAsync({
        workspaceId: props.workspaceId,
        chapterId: props.chapterId,
        // 原卡完整对象 + 覆盖 must_achieve(保留 card_status / 大纲透传字段等)。
        card: { ...props.card, must_achieve: cleaned },
      });
      showSuccessToast(
        "必达事件已保存",
        `${props.chapterId}: 共 ${cleaned.length} 条。删掉已被前文做完的那条后,` +
          `点工具栏「开始写作」重新生成正文(写前预检会重新核对)。`,
      );
      props.onDone();
    } catch (err) {
      showApiErrorToast(err);
      // 不退出编辑态,保留草稿供修正(典型:forbidden 风格污染类 422 不影响 must,
      // 但 PUT 整卡校验失败时让用户看到错误后重试)。
    }
  }, [items, mutation, props]);

  return (
    <div
      data-testid="card-must-achieve-editor"
      className="flex flex-1 flex-col gap-3 overflow-auto p-4"
    >
      <p className="text-xs text-muted-foreground">
        只编辑「必达事件 must_achieve」。删除已被前一章做完的那条,其余字段不变。
      </p>
      <ul className="space-y-2">
        {items.map((item, i) => (
          <li key={i} className="flex items-start gap-2">
            <textarea
              data-testid={`must-achieve-input-${i}`}
              className="min-h-[3rem] flex-1 resize-y rounded border border-border bg-background p-2 text-sm"
              value={item}
              disabled={mutation.isPending}
              onChange={(e) => updateItem(i, e.target.value)}
            />
            <Button
              data-testid={`must-achieve-remove-${i}`}
              variant="ghost"
              size="sm"
              disabled={mutation.isPending}
              onClick={() => removeItem(i)}
              aria-label={`删除第 ${i + 1} 条`}
            >
              ✕
            </Button>
          </li>
        ))}
      </ul>
      <Button
        data-testid="must-achieve-add"
        variant="outline"
        size="sm"
        disabled={mutation.isPending}
        onClick={addItem}
        className="self-start"
      >
        + 加一条
      </Button>
      <div className="mt-auto flex items-center gap-2 pt-2">
        <Button
          data-testid="must-achieve-save"
          size="sm"
          disabled={mutation.isPending}
          onClick={handleSave}
        >
          {mutation.isPending ? "保存中…" : "保存必达事件"}
        </Button>
        <Button
          data-testid="must-achieve-cancel"
          variant="ghost"
          size="sm"
          disabled={mutation.isPending}
          onClick={props.onDone}
        >
          取消
        </Button>
      </div>
    </div>
  );
}

interface PanelBodyProps {
  loading: boolean;
  error: unknown;
  data: ChapterCardOut | undefined;
  json: string;
  workspaceId: string;
  chapterId: string;
}

function PanelBody(props: PanelBodyProps) {
  if (props.loading) {
    return (
      <div className="p-4 text-sm text-muted-foreground">加载章节卡中…</div>
    );
  }
  if (props.error) {
    return (
      <ErrorHint
        error={props.error}
        workspaceId={props.workspaceId}
        chapterId={props.chapterId}
      />
    );
  }
  if (!props.data) {
    return (
      <div className="p-4 text-sm text-muted-foreground">没有章节卡数据。</div>
    );
  }
  return (
    <pre
      data-testid="chapter-card-json"
      className="m-0 flex-1 overflow-auto whitespace-pre-wrap break-words p-4 font-mono text-xs leading-relaxed text-foreground"
    >
      {props.json}
    </pre>
  );
}

interface ErrorHintProps {
  error: unknown;
  workspaceId: string;
  chapterId: string;
}

function ErrorHint(props: ErrorHintProps) {
  const apiErr = isApiError(props.error) ? props.error : null;
  if (apiErr?.code === "CHAPTER_CARD_NOT_FOUND") {
    return (
      <div
        data-testid="chapter-card-not-found"
        className="space-y-2 p-4 text-sm text-muted-foreground"
      >
        <p>未找到章节卡 {props.chapterId || "<chapter_id>"}。</p>
        <p>
          请确认 chapter_id 拼写正确,或把 ChapterCard JSON 放到
          <code className="mx-1 break-all">
            workspaces/{props.workspaceId || "<id>"}/data/cards/
            {props.chapterId || "<chapter_id>"}.json
          </code>
          后刷新本页。
        </p>
      </div>
    );
  }
  if (apiErr?.code === "CHAPTER_CARD_INVALID") {
    return (
      <div
        data-testid="chapter-card-invalid"
        className="space-y-2 p-4 text-sm text-destructive"
      >
        <p>章节卡 JSON 损坏或不符合 schema:</p>
        <p className="font-mono text-xs">{apiErr.message}</p>
      </div>
    );
  }
  const message = apiErr
    ? `${apiErr.code}: ${apiErr.message}`
    : props.error instanceof Error
      ? props.error.message
      : "未知错误";
  return (
    <div
      data-testid="chapter-card-error"
      className="p-4 text-sm text-destructive"
    >
      加载章节卡失败:{message}
    </div>
  );
}
