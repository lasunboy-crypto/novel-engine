/**
 * ChapterCardFullEditor(P6-F-01)— 章节卡完整字段编辑器,接 P5 简版。
 *
 * P5-F-06 简版 8 字段(`tension_level` / `must_achieve` / `must_unresolved` /
 * `forbidden` / `pov_character` / `setting` / `next_chapter_seed`)由
 * ChapterCardReviewPanel 内嵌的 FieldText / FieldNumber / FieldStringList
 * 控件渲染,本组件**只补 P6 升级新增的 6 字段**:
 *
 * - `arc_phase`(Literal 5 档,enum)
 * - `target_word_count_range`(对齐 `word_count_min` / `word_count_max` 双数值)
 * - `is_arc_finale`(boolean)
 * - `related_characters`(string[])
 * - `related_plot_threads`(string[])
 * - `force_profile`(P6-B-09 ForceProfile Literal 4 档 | null)
 *
 * **`continuity_context` 嵌套对象编辑**走独立 `ContinuityContextEditor`
 * 组件(plan §"P6-F-01" ① 字面"continuity_context 嵌套对象编辑器")。
 *
 * ## 设计取舍
 *
 * - **纯受控**:接 `value: ChapterCardOut` + `onChange(patch)` 回调,父组件
 *   ChapterCardReviewPanel 持 draft 状态。
 * - **`force_profile = null` 用空字符串**表示"沿用默认路由"(下拉首项),
 *   语义对齐 WriterProfileSelector(`profile_override = null` 同模式)。
 * - **不引入校验**:`word_count_min < word_count_max` / `pov_character` 在
 *   `related_characters` 列表内等跨字段约束留后端 422 兜底,前端编辑期不挡
 *   (对齐 P5-F-06 字面"editor 允许中间态");后端 Pydantic 校验失败时
 *   showApiErrorToast 显示 details。
 */

import type { ChapterCardOut } from "@/api/chapters";

type ArcPhase = ChapterCardOut["arc_phase"];
type ForceProfile = NonNullable<ChapterCardOut["force_profile"]>;

const ARC_PHASE_OPTIONS: ReadonlyArray<{
  value: ArcPhase;
  label: string;
}> = [
  { value: "opening", label: "opening(开场)" },
  { value: "rising", label: "rising(上升)" },
  { value: "climax", label: "climax(高潮)" },
  { value: "falling", label: "falling(下落)" },
  { value: "resolution", label: "resolution(收束)" },
];

const FORCE_PROFILE_OPTIONS: ReadonlyArray<{
  value: ForceProfile | "";
  label: string;
}> = [
  { value: "", label: "(沿用默认路由 / 不强制)" },
  { value: "cloud_quality", label: "cloud_quality(Claude Sonnet 4 主力)" },
  { value: "cloud_premium", label: "cloud_premium(Claude Opus 4 高质量)" },
  { value: "local_qwen_32k", label: "local_qwen_32k(本地 Qwen 主力)" },
  { value: "local_qwen_safe", label: "local_qwen_safe(本地 Qwen 安全)" },
];

interface ChapterCardFullEditorProps {
  card: ChapterCardOut;
  editable: boolean;
  onChange: (patch: Partial<ChapterCardOut>) => void;
}

export function ChapterCardFullEditor(props: ChapterCardFullEditorProps) {
  const { card, editable, onChange } = props;

  if (!editable) {
    return <FullEditorReadView card={card} />;
  }
  return (
    <section
      data-testid="chapter-card-full-editor"
      className="space-y-3 rounded border border-border bg-card p-4"
    >
      <h3 className="text-sm font-semibold uppercase tracking-wide text-muted-foreground">
        完整字段编辑(P6 升级:arc / 字数 / 关联 / force_profile)
      </h3>
      <div className="grid grid-cols-1 gap-3 md:grid-cols-2">
        <div>
          <label
            htmlFor="full-arc-phase"
            className="mb-1 block text-xs font-medium text-muted-foreground"
          >
            arc_phase(弧线阶段)
          </label>
          <select
            id="full-arc-phase"
            value={card.arc_phase}
            onChange={(e) =>
              onChange({ arc_phase: e.target.value as ArcPhase })
            }
            className="w-full rounded border border-input bg-background px-2 py-1.5 text-sm focus-visible:outline-none focus-visible:ring-2 focus-visible:ring-ring"
          >
            {ARC_PHASE_OPTIONS.map((o) => (
              <option key={o.value} value={o.value}>
                {o.label}
              </option>
            ))}
          </select>
        </div>
        <div className="flex items-center gap-2 pt-5">
          <input
            id="full-is-arc-finale"
            type="checkbox"
            checked={card.is_arc_finale}
            onChange={(e) => onChange({ is_arc_finale: e.target.checked })}
            className="h-4 w-4 rounded border-input"
          />
          <label
            htmlFor="full-is-arc-finale"
            className="text-xs font-medium text-muted-foreground"
          >
            is_arc_finale(本章是否为弧线终章)
          </label>
        </div>
        <div>
          <label
            htmlFor="full-wcmin"
            className="mb-1 block text-xs font-medium text-muted-foreground"
          >
            {card.word_count_policy_version === 2 ? "目标字数（约，不设下限）" : "word_count_min(字数下限)"}
          </label>
          <input
            id="full-wcmin"
            type="number"
            min={1}
            value={card.word_count_min}
            onChange={(e) => {
              const next = Number(e.target.value);
              if (Number.isFinite(next))
                onChange({ word_count_min: Math.max(1, next),
                  ...(card.word_count_policy_version === 2 ? { word_count_max: Math.max(1, next) } : {}) });
            }}
            className="w-32 rounded border border-input bg-background px-2 py-1.5 text-sm tabular-nums focus-visible:outline-none focus-visible:ring-2 focus-visible:ring-ring"
          />
        </div>
        {card.word_count_policy_version !== 2 && <div>
          <label
            htmlFor="full-wcmax"
            className="mb-1 block text-xs font-medium text-muted-foreground"
          >
            word_count_max(字数上限)
          </label>
          <input
            id="full-wcmax"
            type="number"
            min={1}
            value={card.word_count_max}
            onChange={(e) => {
              const next = Number(e.target.value);
              if (Number.isFinite(next))
                onChange({ word_count_max: Math.max(1, next) });
            }}
            className="w-32 rounded border border-input bg-background px-2 py-1.5 text-sm tabular-nums focus-visible:outline-none focus-visible:ring-2 focus-visible:ring-ring"
          />
        </div>}
        <StringListField
          id="full-related-chars"
          label="related_characters(出场角色 ID,一行一条)"
          values={card.related_characters}
          onChange={(next) => onChange({ related_characters: next })}
          className="md:col-span-2"
        />
        <StringListField
          id="full-related-threads"
          label="related_plot_threads(关联剧情线 ID,一行一条)"
          values={card.related_plot_threads}
          onChange={(next) => onChange({ related_plot_threads: next })}
          className="md:col-span-2"
        />
        <div className="md:col-span-2">
          <label
            htmlFor="full-force-profile"
            className="mb-1 block text-xs font-medium text-muted-foreground"
          >
            force_profile(强制本章走指定 Writer profile;P6-B-09)
          </label>
          <select
            id="full-force-profile"
            value={card.force_profile ?? ""}
            onChange={(e) => {
              const v = e.target.value;
              onChange({
                force_profile: v === "" ? null : (v as ForceProfile),
              });
            }}
            className="rounded border border-input bg-background px-2 py-1.5 text-sm focus-visible:outline-none focus-visible:ring-2 focus-visible:ring-ring"
            data-testid="force-profile-select"
          >
            {FORCE_PROFILE_OPTIONS.map((o) => (
              <option key={o.value} value={o.value}>
                {o.label}
              </option>
            ))}
          </select>
          <p className="mt-1 text-[10px] text-muted-foreground">
            越界(profile 在 Literal 4 档内但 ModelRouter 未注册)→ write/rewrite
            返 422 WRITER_PROFILE_NOT_FOUND;此处先按 plan 字面 4 档下拉避免误输入。
          </p>
        </div>
      </div>
    </section>
  );
}

interface StringListFieldProps {
  id: string;
  label: string;
  values: string[];
  onChange: (next: string[]) => void;
  className?: string;
}

function StringListField(props: StringListFieldProps) {
  return (
    <div className={props.className}>
      <label
        htmlFor={props.id}
        className="mb-1 block text-xs font-medium text-muted-foreground"
      >
        {props.label}
      </label>
      <textarea
        id={props.id}
        rows={Math.max(3, props.values.length + 1)}
        value={props.values.join("\n")}
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

function FullEditorReadView(props: { card: ChapterCardOut }) {
  const { card } = props;
  return (
    <section
      data-testid="chapter-card-full-readview"
      className="rounded border border-border bg-card p-4"
    >
      <h3 className="mb-3 text-sm font-semibold uppercase tracking-wide text-muted-foreground">
        完整字段(P6 升级:arc / 字数 / 关联 / force_profile)
      </h3>
      <div className="grid grid-cols-2 gap-x-4 gap-y-2 text-xs md:grid-cols-4">
        <Cell label="arc_phase" value={card.arc_phase} />
        <Cell label="is_arc_finale" value={card.is_arc_finale ? "是" : "否"} />
        <Cell
          label="word_count"
          value={card.word_count_policy_version === 2 ? `约 ${card.word_count_min} 字（不设下限）` : `${card.word_count_min}–${card.word_count_max}`}
        />
        <Cell
          label="force_profile"
          value={card.force_profile ?? "(沿用默认)"}
        />
        <Cell
          label="related_characters"
          value={card.related_characters.join(", ") || "—"}
          className="col-span-2 md:col-span-4"
        />
        <Cell
          label="related_plot_threads"
          value={card.related_plot_threads.join(", ") || "—"}
          className="col-span-2 md:col-span-4"
        />
      </div>
    </section>
  );
}

function Cell(props: { label: string; value: string; className?: string }) {
  return (
    <div className={props.className}>
      <p className="text-[10px] uppercase tracking-wide text-muted-foreground">
        {props.label}
      </p>
      <p className="break-words text-xs text-foreground">{props.value}</p>
    </div>
  );
}
