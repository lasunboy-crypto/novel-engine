/**
 * ChapterCardReviewPanel(P5-F-06 / P6-F-01 升级)— 章节卡审核态展示 + 编辑双态。
 *
 * 上层 ChapterCardReviewPage 控制 mode / draft state,本组件纯展示 + 受控
 * 表单(对齐 P5-F-05 OutlineSectionEditor 的"父组件控 mode + draft、子组件
 * 渲染表单"模式)。
 *
 * ## 字段分组渲染(P6 升级后 5 段)
 *
 * 1. **关键字段高亮区**(top,sky 浅蓝):对齐 FRONTEND_SPEC §4.8 字面 5 字段
 *    视觉强调 — `tension_level` / `must_achieve` / `must_unresolved` /
 *    `forbidden` / `continuity_context`。`continuity_context`
 *    单独拎到衔接桥块展示,故本段实际渲染剩 4 表单字段。
 * 2. **衔接桥块**(amber 浅黄):`prev_chapter_tail`(上一章末尾 500 字)+
 *    `unfinished_actions` 列表 + 4 元数据 cell;编辑态额外渲染
 *    `ContinuityContextEditor` 嵌套对象编辑器(7 字段)。
 * 3. **视角 / 场景 / 下章种子**:`pov_character` / `setting` /
 *    `next_chapter_seed` 3 字段。
 * 4. **完整字段编辑**(P6 新增,委托 `ChapterCardFullEditor`):`arc_phase` /
 *    `is_arc_finale` / `word_count_min/max` / `related_characters` /
 *    `related_plot_threads` / `force_profile`(P6-B-09)。
 * 5. **审稿阈值**(v1.0 hot-fix bug ⑤ 新增可编辑段):`review_threshold` /
 *    `revise_threshold` / `max_rewrites` / `max_revises` 4 字段。旧实现这 4
 *    字段在"系统只读字段"段不可改;hot-fix 起改为编辑态可改,死锁 / Qwen
 *    评分系统性偏低时,维护人可单章下调 review_threshold(例 ch-0001 tension=2
 *    下调到 5.5)。`review_threshold > revise_threshold` 由后端 Pydantic 校验。
 * 6. **系统只读字段**:`chapter_id` / `workspace_id` / `volume_id` /
 *    `chapter_number` / `title` / `plot_node_budget` / `card_status` /
 *    `card_approved_at`(4 阈值字段已迁出到第 5 段)。
 *
 * ## P5 vs P6 字段编辑面
 *
 * - P5 简版仅可编辑 7 字段(上面 1 段 4 + 3 段 3);其余只读。
 * - P6 升级完整字段编辑(P5 只读字段 → 编辑表单),加 `force_profile` 字段
 *   编辑(P6-B-09)+ `continuity_context` 嵌套编辑。"关键字段达成对比"块
 *   由父组件 ChapterCardReviewPage 直接挂载 `MustAchieveCheckList`,不在
 *   本组件内(对齐 plan §"P6-F-01" ④ 字面走 P6-B-11 端点)。
 */

import type { ChapterCardOut } from "@/api/chapters";
import { ChapterCardFullEditor } from "@/components/ChapterCardFullEditor";
import { ContinuityContextEditor } from "@/components/ContinuityContextEditor";
import { NarrativeContextView } from "@/components/NarrativeContextView";

export type EditableCardFields = Pick<
  ChapterCardOut,
  | "tension_level"
  | "must_achieve"
  | "must_unresolved"
  | "forbidden"
  | "pov_character"
  | "setting"
  | "next_chapter_seed"
>;

interface ChapterCardReviewPanelProps {
  card: ChapterCardOut;
  /** 编辑态时父组件提供的 draft;非编辑态忽略。 */
  draft: ChapterCardOut | null;
  isEditing: boolean;
  onDraftChange?: (next: ChapterCardOut) => void;
}

export function ChapterCardReviewPanel(props: ChapterCardReviewPanelProps) {
  const display: ChapterCardOut = props.isEditing && props.draft
    ? props.draft
    : props.card;

  const updateDraft = (patch: Partial<ChapterCardOut>) => {
    if (!props.isEditing || !props.draft) return;
    props.onDraftChange?.({ ...props.draft, ...patch });
  };

  return (
    <div className="space-y-4" data-testid="card-review-panel">
      <NarrativeContextView context={display.narrative_context} />
      {/* 关键字段高亮区 */}
      <section className="rounded border border-sky-200 bg-sky-50 p-4">
        <h3 className="mb-3 text-sm font-semibold uppercase tracking-wide text-sky-800">
          关键字段
        </h3>
        <div className="grid grid-cols-1 gap-3 md:grid-cols-2">
          <FieldNumber
            label="tension_level(张力 1-10)"
            value={display.tension_level}
            min={1}
            max={10}
            editable={props.isEditing}
            onChange={(v) => updateDraft({ tension_level: v })}
          />
          <FieldStringList
            label="must_achieve(必达事件)"
            values={display.must_achieve}
            editable={props.isEditing}
            onChange={(v) => updateDraft({ must_achieve: v })}
            className="md:col-span-2"
          />
          <FieldStringList
            label="must_unresolved(必保未解悬念)"
            values={display.must_unresolved}
            editable={props.isEditing}
            onChange={(v) => updateDraft({ must_unresolved: v })}
            className="md:col-span-2"
          />
          <FieldStringList
            label="forbidden(禁止元素)"
            values={display.forbidden}
            editable={props.isEditing}
            onChange={(v) => updateDraft({ forbidden: v })}
            className="md:col-span-2"
          />
        </div>
      </section>

      {/* 衔接桥块(只读视图常驻;编辑态额外渲染嵌套编辑器) */}
      <ContinuityBridgeView
        context={display.continuity_context}
        povChange={display.continuity_context.pov_change}
      />

      {props.isEditing && (
        <ContinuityContextEditor
          value={display.continuity_context}
          editable
          onChange={(next) => updateDraft({ continuity_context: next })}
        />
      )}

      {/* 视角 / 场景 / 下章种子 */}
      <section className="rounded border border-border bg-card p-4">
        <h3 className="mb-3 text-sm font-semibold uppercase tracking-wide text-muted-foreground">
          视角 / 场景 / 下章种子
        </h3>
        <div className="grid grid-cols-1 gap-3">
          <FieldText
            label="pov_character(视角角色)"
            value={display.pov_character}
            editable={props.isEditing}
            onChange={(v) => updateDraft({ pov_character: v })}
          />
          <FieldText
            label="setting(场景设置)"
            value={display.setting}
            editable={props.isEditing}
            onChange={(v) => updateDraft({ setting: v })}
            multiline
          />
          <FieldText
            label="next_chapter_seed(给下一章的种子)"
            value={display.next_chapter_seed}
            editable={props.isEditing}
            onChange={(v) => updateDraft({ next_chapter_seed: v })}
            multiline
          />
        </div>
      </section>

      {/* 笔法投放(wave 5 s3 craft_focus;writer 规划期指引,只读) */}
      <CraftFocusView craftFocus={display.craft_focus} />

      {/* 完整字段编辑(P6 升级:arc / 字数 / 关联 / force_profile) */}
      <ChapterCardFullEditor
        card={display}
        editable={props.isEditing}
        onChange={(patch) => updateDraft(patch)}
      />

      {/* 审稿阈值(v1.0 hot-fix bug ⑤:从 ReadOnlyMeta 迁出,改可编辑) */}
      <ReviewThresholdSection
        card={display}
        editable={props.isEditing}
        onChange={(patch) => updateDraft(patch)}
      />

      {/* 系统只读字段 — ID / 标题 / plot_node_budget / 审核态 */}
      <ReadOnlyMeta card={display} />
    </div>
  );
}

/**
 * 笔法投放只读视图(v1.1 wave 7 / B 卓越性度量 UI 化)。
 *
 * 展示 ChapterCard.craft_focus(wave 5 s3 引入):卡生成期 LLM 据 must_achieve
 * 节拍标注"本章哪几处该深挖内心 / 给画面、其余克制留白"的 Writer 规划期指引。
 * ``default=""`` → 常为空(本章整体克制 / 旧卡),空态给明确提示不留白屏。
 * 与 reviewer 的画面感 / 内心戏度量(ExcellenceMetrics)配对:本字段是"投放
 * 意图",度量侧测"是否交付"。
 */
export function CraftFocusView(props: { craftFocus: string }) {
  const text = (props.craftFocus ?? "").trim();
  return (
    <section
      data-testid="craft-focus-view"
      className="rounded border border-teal-200 bg-teal-50 p-4"
    >
      <h3 className="mb-1 text-sm font-semibold uppercase tracking-wide text-teal-800">
        笔法投放(writer 指引)
      </h3>
      <p className="mb-2 text-[11px] leading-relaxed text-teal-700">
        本章 Writer 规划期的"哪几处深挖内心 / 给画面、其余克制留白"指引
        (wave 5 craft_focus)。空 = 本章整体克制,无特别投放。
      </p>
      {text ? (
        <p
          data-testid="craft-focus-text"
          className="whitespace-pre-wrap break-words text-xs text-foreground"
        >
          {text}
        </p>
      ) : (
        <p
          data-testid="craft-focus-empty"
          className="text-xs text-muted-foreground"
        >
          (空:本章无特别笔法投放)
        </p>
      )}
    </section>
  );
}

interface ReviewThresholdSectionProps {
  card: ChapterCardOut;
  editable: boolean;
  onChange: (patch: Partial<ChapterCardOut>) => void;
}

/**
 * 审稿阈值编辑段(v1.0 hot-fix bug ⑤,2026-05-19)。
 *
 * 旧实现:`review_threshold` / `revise_threshold` / `max_rewrites` / `max_revises`
 * 4 字段挂在 `ReadOnlyMeta` 段不可改 → Qwen reviewer 评分系统性偏低时,维护人
 * 无法单章下调阈值,需手改 SSOT JSON。本段把 4 字段迁出到可编辑区。
 *
 * 校验:`review_threshold > revise_threshold` 由后端 Pydantic 校验
 * (`ChapterCard.revise_threshold < review_threshold`),前端不重复校验;
 * 用户输入越界时保存动作走 422 提示。`max_rewrites` / `max_revises` 后端
 * 限 `ge=0`,本组件 input min=0。
 */
function ReviewThresholdSection(props: ReviewThresholdSectionProps) {
  const { card, editable } = props;
  return (
    <section className="rounded border border-violet-200 bg-violet-50 p-4">
      <h3 className="mb-3 text-sm font-semibold uppercase tracking-wide text-violet-800">
        审稿阈值(单章覆盖)
      </h3>
      <div className="grid grid-cols-1 gap-3 md:grid-cols-2">
        <FieldNumberFloat
          label="review_threshold(通过阈值,score ≥ 此值 → pass)"
          value={card.review_threshold}
          min={0}
          max={10}
          step={0.1}
          editable={editable}
          onChange={(v) => props.onChange({ review_threshold: v })}
        />
        <FieldNumberFloat
          label="revise_threshold(revise 下界,score < 此值 → rewrite)"
          value={card.revise_threshold}
          min={0}
          max={10}
          step={0.1}
          editable={editable}
          onChange={(v) => props.onChange({ revise_threshold: v })}
        />
        <FieldNumber
          label="max_rewrites(重写次数上限)"
          value={card.max_rewrites}
          min={0}
          max={20}
          editable={editable}
          onChange={(v) => props.onChange({ max_rewrites: v })}
        />
        <FieldNumber
          label="max_revises(修订次数上限)"
          value={card.max_revises}
          min={0}
          max={20}
          editable={editable}
          onChange={(v) => props.onChange({ max_revises: v })}
        />
      </div>
      {editable && (
        <p className="mt-3 text-[11px] leading-relaxed text-violet-900/80">
          约束:<code>review_threshold</code> 必须严格大于{" "}
          <code>revise_threshold</code>(verdict 三档非空区间);越界保存时后端
          返 422。tension-aware 默认值:tension 1-3 = 6.0/5.0,4-6 = 6.5/5.5,
          7-8 = 7.0/6.0,9-10 = 7.5/6.5(章节卡生成时按本章 tension_level 注入)。
        </p>
      )}
    </section>
  );
}

interface FieldNumberFloatProps {
  label: string;
  value: number;
  min: number;
  max: number;
  step: number;
  editable: boolean;
  onChange: (v: number) => void;
}

function FieldNumberFloat(props: FieldNumberFloatProps) {
  const id = useFieldId(props.label);
  if (!props.editable) {
    return (
      <ReadOnlyCell
        label={props.label}
        value={props.value.toFixed(2)}
      />
    );
  }
  return (
    <div>
      <label
        htmlFor={id}
        className="mb-1 block text-xs font-medium text-muted-foreground"
      >
        {props.label}
      </label>
      <input
        id={id}
        type="number"
        min={props.min}
        max={props.max}
        step={props.step}
        value={props.value}
        onChange={(e) => {
          const next = Number(e.target.value);
          if (Number.isFinite(next)) props.onChange(next);
        }}
        className="w-32 rounded border border-input bg-background px-2 py-1.5 text-sm tabular-nums focus-visible:outline-none focus-visible:ring-2 focus-visible:ring-ring"
      />
    </div>
  );
}

interface ContinuityBridgeViewProps {
  context: ChapterCardOut["continuity_context"];
  povChange: boolean;
}

function ContinuityBridgeView(props: ContinuityBridgeViewProps) {
  const { context, povChange } = props;
  const tail = context.prev_chapter_tail || "";
  const unfinished = context.unfinished_actions ?? [];
  return (
    <section
      data-testid="continuity-bridge"
      className="rounded border border-amber-200 bg-amber-50 p-4"
    >
      <h3 className="mb-1 text-sm font-semibold uppercase tracking-wide text-amber-800">
        衔接桥(上一章末尾 + 未完成动作)
      </h3>
      {/* 线性正式 Run 中，非首章卡在上一章富记忆 succeeded 后才生成，正常应
          直接显示上一章 final.md 尾文。占位只兼容首章、旧工作区或人工旁路。 */}
      <p className="mb-3 text-[11px] leading-relaxed text-amber-700">
        首章显示“无上一章”；正式 Run 的后续卡应直接带上一章真实尾文。若非首章仍
        显示占位，通常是旧工作区或人工旁路上下文不完整。
      </p>
      <div className="space-y-3">
        <div>
          <p className="mb-1 text-xs font-medium text-amber-900">
            上一章末尾 500 字
          </p>
          {tail ? (
            <pre className="m-0 whitespace-pre-wrap break-words rounded border border-amber-100 bg-white p-2 font-sans text-xs leading-relaxed text-foreground">
              {tail}
            </pre>
          ) : (
            <p className="text-xs text-muted-foreground">(首章或无衔接段)</p>
          )}
        </div>
        <div>
          <p className="mb-1 text-xs font-medium text-amber-900">
            上一章未完成动作 ({unfinished.length})
          </p>
          {unfinished.length === 0 ? (
            <p className="text-xs text-muted-foreground">(无)</p>
          ) : (
            <ul className="list-disc space-y-0.5 pl-5 text-xs text-foreground">
              {unfinished.map((a, i) => (
                <li key={i}>{a}</li>
              ))}
            </ul>
          )}
        </div>
        <div className="grid grid-cols-2 gap-3 border-t border-amber-100 pt-3 text-xs md:grid-cols-4">
          <ReadOnlyCell label="time_gap" value={context.time_gap || "—"} />
          <ReadOnlyCell
            label="scene_continuation"
            value={context.scene_continuation}
          />
          <ReadOnlyCell
            label="pov_change"
            value={povChange ? "是" : "否"}
          />
          <ReadOnlyCell
            label="pov_from → pov_to"
            value={`${context.pov_from ?? "—"} → ${context.pov_to}`}
          />
        </div>
      </div>
    </section>
  );
}

function ReadOnlyMeta(props: { card: ChapterCardOut }) {
  const { card } = props;
  return (
    <section className="rounded border border-border bg-card p-4">
      <h3 className="mb-3 text-sm font-semibold uppercase tracking-wide text-muted-foreground">
        系统字段(始终只读)
      </h3>
      <div className="grid grid-cols-2 gap-x-4 gap-y-2 text-xs md:grid-cols-4">
        <ReadOnlyCell label="chapter_id" value={card.chapter_id} mono />
        <ReadOnlyCell label="workspace_id" value={card.workspace_id} mono />
        <ReadOnlyCell label="volume_id" value={card.volume_id} mono />
        <ReadOnlyCell
          label="chapter_number"
          value={String(card.chapter_number)}
        />
        <ReadOnlyCell
          label="title"
          value={card.title}
          className="col-span-2"
        />
        <ReadOnlyCell
          label="plot_node_budget"
          value={String(card.plot_node_budget)}
        />
        {/* v1.0 hot-fix bug ⑤:review_threshold / revise_threshold /
            max_rewrites / max_revises 4 字段已迁出到 ReviewThresholdSection
            可编辑段(组件上方)。 */}
        <ReadOnlyCell label="card_status" value={card.card_status} />
        <ReadOnlyCell
          label="card_approved_at"
          value={card.card_approved_at ?? "—"}
        />
      </div>
    </section>
  );
}

function ReadOnlyCell(props: {
  label: string;
  value: string;
  mono?: boolean;
  className?: string;
}) {
  return (
    <div className={props.className}>
      <p className="text-[10px] uppercase tracking-wide text-muted-foreground">
        {props.label}
      </p>
      <p
        className={
          props.mono
            ? "break-all font-mono text-xs"
            : "break-words text-xs text-foreground"
        }
      >
        {props.value}
      </p>
    </div>
  );
}

// ---- 可编辑字段控件 ----

interface FieldTextProps {
  label: string;
  value: string;
  editable: boolean;
  onChange: (v: string) => void;
  multiline?: boolean;
}

function FieldText(props: FieldTextProps) {
  const id = useFieldId(props.label);
  if (!props.editable) {
    return (
      <ReadOnlyCell label={props.label} value={props.value || "—"} />
    );
  }
  return (
    <div>
      <label
        htmlFor={id}
        className="mb-1 block text-xs font-medium text-muted-foreground"
      >
        {props.label}
      </label>
      {props.multiline ? (
        <textarea
          id={id}
          rows={3}
          value={props.value}
          onChange={(e) => props.onChange(e.target.value)}
          className="w-full rounded border border-input bg-background px-2 py-1.5 text-sm focus-visible:outline-none focus-visible:ring-2 focus-visible:ring-ring"
        />
      ) : (
        <input
          id={id}
          type="text"
          value={props.value}
          onChange={(e) => props.onChange(e.target.value)}
          className="w-full rounded border border-input bg-background px-2 py-1.5 text-sm focus-visible:outline-none focus-visible:ring-2 focus-visible:ring-ring"
        />
      )}
    </div>
  );
}

interface FieldNumberProps {
  label: string;
  value: number;
  min: number;
  max: number;
  editable: boolean;
  onChange: (v: number) => void;
}

function FieldNumber(props: FieldNumberProps) {
  const id = useFieldId(props.label);
  if (!props.editable) {
    return <ReadOnlyCell label={props.label} value={String(props.value)} />;
  }
  return (
    <div>
      <label
        htmlFor={id}
        className="mb-1 block text-xs font-medium text-muted-foreground"
      >
        {props.label}
      </label>
      <input
        id={id}
        type="number"
        min={props.min}
        max={props.max}
        value={props.value}
        onChange={(e) => {
          const next = Number(e.target.value);
          if (Number.isFinite(next)) props.onChange(next);
        }}
        className="w-24 rounded border border-input bg-background px-2 py-1.5 text-sm tabular-nums focus-visible:outline-none focus-visible:ring-2 focus-visible:ring-ring"
      />
    </div>
  );
}

interface FieldStringListProps {
  label: string;
  values: string[];
  editable: boolean;
  onChange: (v: string[]) => void;
  className?: string;
}

function FieldStringList(props: FieldStringListProps) {
  if (!props.editable) {
    return (
      <div className={props.className}>
        <p className="text-[10px] uppercase tracking-wide text-muted-foreground">
          {props.label}
        </p>
        {props.values.length === 0 ? (
          <p className="text-xs text-muted-foreground">(空)</p>
        ) : (
          <ul className="list-disc space-y-0.5 pl-5 text-xs text-foreground">
            {props.values.map((v, i) => (
              <li key={i}>{v}</li>
            ))}
          </ul>
        )}
      </div>
    );
  }
  // 编辑态:一行一条,自动补 / 删空行。textarea 每行 = 一个 item。
  const text = props.values.join("\n");
  return (
    <div className={props.className}>
      <label className="mb-1 block text-xs font-medium text-muted-foreground">
        {props.label}{" "}
        <span className="text-[10px] text-muted-foreground/70">
          (一行一条;空行自动忽略)
        </span>
      </label>
      <textarea
        rows={Math.max(3, props.values.length + 1)}
        value={text}
        onChange={(e) => {
          const next = e.target.value
            .split("\n")
            .map((line) => line.trimEnd())
            .filter((line) => line.length > 0);
          props.onChange(next);
        }}
        className="w-full rounded border border-input bg-background px-2 py-1.5 text-sm focus-visible:outline-none focus-visible:ring-2 focus-visible:ring-ring"
      />
    </div>
  );
}

// 简版 id 生成器(避免引入 useId 兼容问题;React 18 useId 也行但更重)。
function useFieldId(label: string): string {
  return `card-field-${label.replace(/[^a-zA-Z0-9]+/g, "-").toLowerCase()}`;
}
