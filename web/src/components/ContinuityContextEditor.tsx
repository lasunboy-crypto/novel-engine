/**
 * ContinuityContextEditor(P6-F-01)— 章节卡衔接桥嵌套对象编辑器。
 *
 * 对应 `ChapterCard.continuity_context: ContinuityContext`,7 字段嵌套:
 * `prev_chapter_tail` / `unfinished_actions` / `time_gap` /
 * `scene_continuation` / `pov_change` / `pov_from` / `pov_to`。
 *
 * P5 简版只读展示;P6 升级到可编辑,完整字段表单(对齐 plan §"P6-F-01" ①
 * 字面"continuity_context(嵌套对象编辑器)")。
 *
 * ## 设计取舍
 *
 * - **纯受控**:接 `value` + `onChange(next)` 回调,父组件持 draft 状态;
 *   不引入 react-hook-form,与 ChapterCardReviewPanel 既有 FieldText /
 *   FieldStringList 风格统一。
 * - **`pov_from` 可空**(首章对应 `null`):用 `<input>` + 空串映射 null。
 */

import type { ChapterCardOut } from "@/api/chapters";

type ContinuityContext = ChapterCardOut["continuity_context"];

const SCENE_CONTINUATION_OPTIONS: ReadonlyArray<{
  value: ContinuityContext["scene_continuation"];
  label: string;
}> = [
  { value: "same", label: "same(同场景)" },
  { value: "switch", label: "switch(切换场景)" },
  { value: "time_jump", label: "time_jump(时间跳跃)" },
];

interface ContinuityContextEditorProps {
  value: ContinuityContext;
  editable: boolean;
  onChange: (next: ContinuityContext) => void;
}

export function ContinuityContextEditor(props: ContinuityContextEditorProps) {
  const { value, editable, onChange } = props;

  const patch = (delta: Partial<ContinuityContext>) =>
    onChange({ ...value, ...delta });

  if (!editable) {
    return null;
  }
  return (
    <div
      data-testid="continuity-context-editor"
      className="space-y-3 rounded border border-amber-200 bg-amber-50 p-4"
    >
      <h4 className="text-xs font-semibold uppercase tracking-wide text-amber-800">
        衔接桥(continuity_context)编辑
      </h4>
      <div>
        <label
          htmlFor="cc-prev-tail"
          className="mb-1 block text-xs font-medium text-amber-900"
        >
          prev_chapter_tail(上一章末尾 500-800 字)
        </label>
        <textarea
          id="cc-prev-tail"
          rows={5}
          value={value.prev_chapter_tail}
          onChange={(e) => patch({ prev_chapter_tail: e.target.value })}
          className="w-full rounded border border-amber-100 bg-white px-2 py-1.5 text-xs focus-visible:outline-none focus-visible:ring-2 focus-visible:ring-amber-400"
        />
      </div>
      <div>
        <label
          htmlFor="cc-unfinished"
          className="mb-1 block text-xs font-medium text-amber-900"
        >
          unfinished_actions(上一章未完成动作;一行一条)
        </label>
        <textarea
          id="cc-unfinished"
          rows={Math.max(3, (value.unfinished_actions ?? []).length + 1)}
          value={(value.unfinished_actions ?? []).join("\n")}
          onChange={(e) => {
            const next = e.target.value
              .split("\n")
              .map((line) => line.trimEnd())
              .filter((line) => line.length > 0);
            patch({ unfinished_actions: next });
          }}
          className="w-full rounded border border-amber-100 bg-white px-2 py-1.5 text-xs focus-visible:outline-none focus-visible:ring-2 focus-visible:ring-amber-400"
        />
      </div>
      <div className="grid grid-cols-1 gap-3 md:grid-cols-2">
        <div>
          <label
            htmlFor="cc-time-gap"
            className="mb-1 block text-xs font-medium text-amber-900"
          >
            time_gap(时间间隔,如"立即续接")
          </label>
          <input
            id="cc-time-gap"
            type="text"
            value={value.time_gap}
            onChange={(e) => patch({ time_gap: e.target.value })}
            className="w-full rounded border border-amber-100 bg-white px-2 py-1.5 text-xs focus-visible:outline-none focus-visible:ring-2 focus-visible:ring-amber-400"
          />
        </div>
        <div>
          <label
            htmlFor="cc-scene"
            className="mb-1 block text-xs font-medium text-amber-900"
          >
            scene_continuation(场景延续模式)
          </label>
          <select
            id="cc-scene"
            value={value.scene_continuation}
            onChange={(e) =>
              patch({
                scene_continuation: e.target
                  .value as ContinuityContext["scene_continuation"],
              })
            }
            className="w-full rounded border border-amber-100 bg-white px-2 py-1.5 text-xs focus-visible:outline-none focus-visible:ring-2 focus-visible:ring-amber-400"
          >
            {SCENE_CONTINUATION_OPTIONS.map((o) => (
              <option key={o.value} value={o.value}>
                {o.label}
              </option>
            ))}
          </select>
        </div>
        <div className="flex items-center gap-2 pt-5">
          <input
            id="cc-pov-change"
            type="checkbox"
            checked={value.pov_change}
            onChange={(e) => patch({ pov_change: e.target.checked })}
            className="h-4 w-4 rounded border-amber-300"
          />
          <label
            htmlFor="cc-pov-change"
            className="text-xs font-medium text-amber-900"
          >
            pov_change(本章相对上一章是否切换视角)
          </label>
        </div>
        <div>
          <label
            htmlFor="cc-pov-from"
            className="mb-1 block text-xs font-medium text-amber-900"
          >
            pov_from(上一章视角角色,首章可空)
          </label>
          <input
            id="cc-pov-from"
            type="text"
            value={value.pov_from ?? ""}
            onChange={(e) => {
              const v = e.target.value.trim();
              patch({ pov_from: v.length === 0 ? null : v });
            }}
            placeholder="(首章留空 → null)"
            className="w-full rounded border border-amber-100 bg-white px-2 py-1.5 text-xs focus-visible:outline-none focus-visible:ring-2 focus-visible:ring-amber-400"
          />
        </div>
        <div>
          <label
            htmlFor="cc-pov-to"
            className="mb-1 block text-xs font-medium text-amber-900"
          >
            pov_to(本章视角角色)
          </label>
          <input
            id="cc-pov-to"
            type="text"
            value={value.pov_to}
            onChange={(e) => patch({ pov_to: e.target.value })}
            className="w-full rounded border border-amber-100 bg-white px-2 py-1.5 text-xs focus-visible:outline-none focus-visible:ring-2 focus-visible:ring-amber-400"
          />
        </div>
      </div>
    </div>
  );
}
