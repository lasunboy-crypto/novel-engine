/**
 * OutlineSectionEditor(P5-F-05 落地)。
 *
 * 4 段结构化字段表单编辑器,按 section 选对应子编辑器:
 *   - world      → WorldEditor(5 textarea)
 *   - characters → CharactersEditor(角色列表 + 卡片表单 + 增删)
 *   - plot       → PlotEditor(core_conflict + 3 列表 + character_arcs dict)
 *   - volume_plans → VolumePlansEditor(卷列表 + 卷内 arcs 编辑)
 *
 * 数据流:父组件持有 `draft: OutlineOut` 草稿态;子编辑器接 `value`(对应段)
 * + `onChange(next)`(整段替换)回调。**不**自己拉数据 / 写 mutation。
 *
 * **保持原始字段不丢**:每段编辑器对 schema 中可能存在的字段(relationships_brief
 * / linked_phases / tension_curve / character_arcs 部分项)做"原值透传不展示"
 * 而非展开编辑(P6 升级覆盖);避免 PUT 时丢段内字段。
 *
 * **新增项 ID 生成**:`new-<8hex>` 前缀,符合后端 `ID_PATTERN =
 * ^[a-zA-Z0-9_-]{1,64}$`(`src/core/models/__init__.py:12`)。
 */

import { Link } from "react-router-dom";

import type {
  CharacterOutlineItem,
  CharactersOutput,
  OutlineSection,
  PlotOutput,
  VolumeArcPlan,
  VolumePlanItem,
  VolumePlansOutput,
  WorldOutput,
} from "@/api/outline";
import { Button } from "@/components/ui/button";

const ROLE_OPTIONS: ReadonlyArray<{
  value: CharacterOutlineItem["role"];
  label: string;
}> = [
  { value: "protagonist", label: "主角" },
  { value: "deuteragonist", label: "次主角" },
  { value: "antagonist", label: "反派" },
  { value: "supporting", label: "配角" },
];

const ARC_PHASE_OPTIONS: ReadonlyArray<{
  value: VolumeArcPlan["phase"];
  label: string;
}> = [
  { value: "opening", label: "开场" },
  { value: "rising", label: "上升" },
  { value: "climax", label: "高潮" },
  { value: "falling", label: "回落" },
  { value: "resolution", label: "收束" },
];

const TENSION_TEMPLATE_OPTIONS: ReadonlyArray<{
  value: VolumePlanItem["tension_template"];
  label: string;
}> = [
  { value: "heros_journey", label: "英雄之旅" },
  { value: "steady_daily", label: "稳态日常" },
  { value: "strong_stimulation", label: "强刺激" },
  { value: "free", label: "自由" },
];

function newId(prefix: string): string {
  const rand = Math.random().toString(16).slice(2, 10).padEnd(8, "0");
  return `${prefix}-${rand}`;
}

interface OutlineSectionEditorProps {
  /** P6-F-06:候选 4 摊入 — 透传到 VolumePlansEditor 让卷卡可深链 VolumePlanPage。 */
  workspaceId: string;
  section: OutlineSection;
  world: WorldOutput | null;
  characters: CharactersOutput | null;
  plot: PlotOutput | null;
  volumePlans: VolumePlansOutput | null;
  onChangeWorld: (next: WorldOutput) => void;
  onChangeCharacters: (next: CharactersOutput) => void;
  onChangePlot: (next: PlotOutput) => void;
  onChangeVolumePlans: (next: VolumePlansOutput) => void;
}

export function OutlineSectionEditor(props: OutlineSectionEditorProps) {
  if (props.section === "world") {
    return (
      <WorldEditor value={props.world} onChange={props.onChangeWorld} />
    );
  }
  if (props.section === "characters") {
    return (
      <CharactersEditor
        value={props.characters}
        onChange={props.onChangeCharacters}
      />
    );
  }
  if (props.section === "plot") {
    return <PlotEditor value={props.plot} onChange={props.onChangePlot} />;
  }
  return (
    <VolumePlansEditor
      workspaceId={props.workspaceId}
      value={props.volumePlans}
      onChange={props.onChangeVolumePlans}
    />
  );
}

// ---- shared field controls ----

function LabeledTextarea(props: {
  label: string;
  value: string;
  onChange: (next: string) => void;
  rows?: number;
  placeholder?: string;
}) {
  return (
    <label className="block space-y-1">
      <span className="text-xs font-semibold uppercase tracking-wide text-muted-foreground">
        {props.label}
      </span>
      <textarea
        value={props.value}
        onChange={(e) => props.onChange(e.target.value)}
        rows={props.rows ?? 3}
        placeholder={props.placeholder}
        className="w-full resize-y rounded border border-input bg-background px-2 py-1.5 text-sm leading-relaxed"
      />
    </label>
  );
}

function LabeledInput(props: {
  label: string;
  value: string;
  onChange: (next: string) => void;
  placeholder?: string;
  readOnly?: boolean;
}) {
  return (
    <label className="block space-y-1">
      <span className="text-xs font-semibold uppercase tracking-wide text-muted-foreground">
        {props.label}
      </span>
      <input
        type="text"
        value={props.value}
        onChange={(e) => props.onChange(e.target.value)}
        placeholder={props.placeholder}
        readOnly={props.readOnly}
        className="w-full rounded border border-input bg-background px-2 py-1 text-sm read-only:bg-secondary read-only:text-muted-foreground"
      />
    </label>
  );
}

function LabeledNumber(props: {
  label: string;
  value: number;
  onChange: (next: number) => void;
  min?: number;
  max?: number;
}) {
  return (
    <label className="block space-y-1">
      <span className="text-xs font-semibold uppercase tracking-wide text-muted-foreground">
        {props.label}
      </span>
      <input
        type="number"
        value={Number.isFinite(props.value) ? props.value : 0}
        onChange={(e) => {
          const next = Number.parseInt(e.target.value, 10);
          props.onChange(Number.isFinite(next) ? next : 0);
        }}
        min={props.min}
        max={props.max}
        className="w-full rounded border border-input bg-background px-2 py-1 text-sm"
      />
    </label>
  );
}

function LabeledSelect<T extends string>(props: {
  label: string;
  value: T;
  onChange: (next: T) => void;
  options: ReadonlyArray<{ value: T; label: string }>;
}) {
  return (
    <label className="block space-y-1">
      <span className="text-xs font-semibold uppercase tracking-wide text-muted-foreground">
        {props.label}
      </span>
      <select
        value={props.value}
        onChange={(e) => props.onChange(e.target.value as T)}
        className="w-full rounded border border-input bg-background px-2 py-1 text-sm"
      >
        {props.options.map((opt) => (
          <option key={opt.value} value={opt.value}>
            {opt.label}
          </option>
        ))}
      </select>
    </label>
  );
}

function EmptySectionHint(props: { hint: string; addLabel: string; onAdd: () => void }) {
  return (
    <div className="rounded border border-dashed border-border p-4 text-sm text-muted-foreground space-y-2">
      <p>{props.hint}</p>
      <Button size="sm" variant="outline" onClick={props.onAdd}>
        {props.addLabel}
      </Button>
    </div>
  );
}

// ---- WorldEditor ----

const EMPTY_WORLD: WorldOutput = {
  setting: "",
  power_system: "",
  factions: "",
  culture: "",
  unique_rules: "",
};

function WorldEditor(props: {
  value: WorldOutput | null;
  onChange: (next: WorldOutput) => void;
}) {
  const w = props.value ?? EMPTY_WORLD;
  const patch = (key: keyof WorldOutput, val: string) =>
    props.onChange({ ...w, [key]: val });

  return (
    <div className="space-y-3" data-testid="outline-editor-world">
      {props.value === null && (
        <p className="rounded border border-dashed border-border p-2 text-xs text-muted-foreground">
          世界观段尚未生成。下方填入字段并保存,后端将以 patch 写入。
        </p>
      )}
      <LabeledTextarea
        label="时代 / 地理设定"
        value={w.setting}
        onChange={(v) => patch("setting", v)}
      />
      <LabeledTextarea
        label="力量 / 技术体系"
        value={w.power_system}
        onChange={(v) => patch("power_system", v)}
        placeholder="非幻想类可留空字符串"
      />
      <LabeledTextarea
        label="主要势力"
        value={w.factions}
        onChange={(v) => patch("factions", v)}
      />
      <LabeledTextarea
        label="文化 / 经济 / 社会"
        value={w.culture}
        onChange={(v) => patch("culture", v)}
      />
      <LabeledTextarea
        label="本世界硬规则"
        value={w.unique_rules}
        onChange={(v) => patch("unique_rules", v)}
      />
    </div>
  );
}

// ---- CharactersEditor ----

function CharactersEditor(props: {
  value: CharactersOutput | null;
  onChange: (next: CharactersOutput) => void;
}) {
  const items = props.value?.characters ?? [];

  const update = (idx: number, next: CharacterOutlineItem) => {
    const arr = items.slice();
    arr[idx] = next;
    props.onChange({ characters: arr });
  };
  const remove = (idx: number) => {
    const arr = items.slice();
    arr.splice(idx, 1);
    props.onChange({ characters: arr });
  };
  const add = () => {
    const next: CharacterOutlineItem = {
      character_id: newId("char"),
      name: "未命名角色",
      role: "supporting",
      archetype: "",
      appearance: "",
      personality: "",
      background: "",
      motivation: "",
      present_desire: "",
      personal_stakes: "",
      voice_hint: "",
      relationships_brief: {},
    };
    props.onChange({ characters: [...items, next] });
  };

  if (items.length === 0) {
    return (
      <EmptySectionHint
        hint="人设段为空 — 通常 OutlineBuilder 会产 4-8 个核心角色。"
        addLabel="添加角色"
        onAdd={add}
      />
    );
  }

  return (
    <div className="space-y-3" data-testid="outline-editor-characters">
      {items.map((c, idx) => (
        <CharacterCardEditor
          key={c.character_id}
          item={c}
          onChange={(next) => update(idx, next)}
          onRemove={() => remove(idx)}
        />
      ))}
      <Button size="sm" variant="outline" onClick={add}>
        ＋ 添加角色
      </Button>
    </div>
  );
}

function CharacterCardEditor(props: {
  item: CharacterOutlineItem;
  onChange: (next: CharacterOutlineItem) => void;
  onRemove: () => void;
}) {
  const c = props.item;
  const patch = <K extends keyof CharacterOutlineItem>(
    key: K,
    val: CharacterOutlineItem[K],
  ) => props.onChange({ ...c, [key]: val });

  const relsEntries = Object.entries(c.relationships_brief ?? {});

  return (
    <div className="rounded border border-border bg-secondary/30 p-3 space-y-2">
      <div className="flex items-start justify-between gap-2">
        <p className="text-xs text-muted-foreground">
          id: <code>{c.character_id}</code>
        </p>
        <Button
          size="sm"
          variant="ghost"
          onClick={props.onRemove}
          className="text-destructive hover:text-destructive"
        >
          删除
        </Button>
      </div>
      <div className="grid gap-2 sm:grid-cols-2">
        <LabeledInput
          label="名称"
          value={c.name}
          onChange={(v) => patch("name", v)}
        />
        <LabeledSelect
          label="角色定位"
          value={c.role}
          onChange={(v) => patch("role", v)}
          options={ROLE_OPTIONS}
        />
        <LabeledInput
          label="原型"
          value={c.archetype}
          onChange={(v) => patch("archetype", v)}
          placeholder="如 萌新剑修"
        />
        <LabeledInput
          label="说话风格 voice_hint"
          value={c.voice_hint}
          onChange={(v) => patch("voice_hint", v)}
        />
      </div>
      <LabeledTextarea
        label="外貌"
        value={c.appearance}
        onChange={(v) => patch("appearance", v)}
        rows={2}
      />
      <LabeledTextarea
        label="性格"
        value={c.personality}
        onChange={(v) => patch("personality", v)}
        rows={2}
      />
      <LabeledTextarea
        label="背景"
        value={c.background}
        onChange={(v) => patch("background", v)}
        rows={2}
      />
      <LabeledTextarea
        label="驱动力"
        value={c.motivation}
        onChange={(v) => patch("motivation", v)}
        rows={2}
      />
      <LabeledTextarea
        label="眼下想要什么"
        value={c.present_desire ?? ""}
        onChange={(v) => patch("present_desire", v)}
        rows={2}
      />
      <LabeledTextarea
        label="为何在乎这件事"
        value={c.personal_stakes ?? ""}
        onChange={(v) => patch("personal_stakes", v)}
        rows={2}
      />
      {relsEntries.length > 0 && (
        <div className="space-y-1">
          <p className="text-xs font-semibold uppercase tracking-wide text-muted-foreground">
            关系(只读,P6 升级支持编辑)
          </p>
          <ul className="space-y-0.5 rounded bg-background/50 p-2 text-xs">
            {relsEntries.map(([targetId, desc]) => (
              <li key={targetId}>
                → <code>{targetId}</code>:{desc}
              </li>
            ))}
          </ul>
        </div>
      )}
    </div>
  );
}

// ---- PlotEditor ----

const EMPTY_PLOT: PlotOutput = {
  core_conflict: "",
  main_phases: [],
  major_turning_points: [],
  long_threads: [],
  character_arcs: {},
};

function PlotEditor(props: {
  value: PlotOutput | null;
  onChange: (next: PlotOutput) => void;
}) {
  const p = props.value ?? EMPTY_PLOT;
  const patch = <K extends keyof PlotOutput>(key: K, val: PlotOutput[K]) =>
    props.onChange({ ...p, [key]: val });

  const archEntries = Object.entries(p.character_arcs ?? {});

  return (
    <div className="space-y-3" data-testid="outline-editor-plot">
      {props.value === null && (
        <p className="rounded border border-dashed border-border p-2 text-xs text-muted-foreground">
          主线段尚未生成。
        </p>
      )}
      <LabeledTextarea
        label="核心冲突"
        value={p.core_conflict}
        onChange={(v) => patch("core_conflict", v)}
        rows={2}
      />

      <PlotItemList
        label="主线阶段"
        items={p.main_phases}
        getId={(it) => it.phase_id}
        getTitle={(it) => it.title}
        getDesc={(it) => it.summary}
        onChange={(arr) => patch("main_phases", arr)}
        onAdd={() => ({
          phase_id: newId("phase"),
          title: "新阶段",
          summary: "",
        })}
        descLabel="阶段概要"
      />

      <PlotItemList
        label="关键转折"
        items={p.major_turning_points}
        getId={(it) => it.turning_id}
        getTitle={(it) => it.title}
        getDesc={(it) => it.description}
        onChange={(arr) => patch("major_turning_points", arr)}
        onAdd={() => ({
          turning_id: newId("turn"),
          title: "新转折",
          description: "",
        })}
        descLabel="转折描述"
      />

      <PlotItemList
        label="长伏笔"
        items={p.long_threads}
        getId={(it) => it.thread_id}
        getTitle={(it) => it.title}
        getDesc={(it) => it.description}
        onChange={(arr) => patch("long_threads", arr)}
        onAdd={() => ({
          thread_id: newId("thread"),
          title: "新伏笔",
          description: "",
        })}
        descLabel="伏笔描述 + 埋 / 收意图"
      />

      {archEntries.length > 0 && (
        <div className="space-y-1">
          <p className="text-xs font-semibold uppercase tracking-wide text-muted-foreground">
            角色弧光(只读,P6 升级支持编辑)
          </p>
          <ul className="space-y-0.5 rounded bg-background/50 p-2 text-xs">
            {archEntries.map(([charId, desc]) => (
              <li key={charId}>
                <code>{charId}</code>:{desc}
              </li>
            ))}
          </ul>
        </div>
      )}
    </div>
  );
}

interface PlotItem {
  [key: string]: unknown;
}

function PlotItemList<T extends PlotItem>(props: {
  label: string;
  items: T[];
  getId: (it: T) => string;
  getTitle: (it: T) => string;
  getDesc: (it: T) => string;
  onChange: (next: T[]) => void;
  onAdd: () => T;
  descLabel: string;
}) {
  const { items, getId, getTitle, getDesc } = props;

  const update = (idx: number, partial: Partial<T>) => {
    const arr = items.slice();
    arr[idx] = { ...arr[idx], ...partial };
    props.onChange(arr);
  };
  const remove = (idx: number) => {
    const arr = items.slice();
    arr.splice(idx, 1);
    props.onChange(arr);
  };
  const add = () => {
    props.onChange([...items, props.onAdd()]);
  };

  const titleField = (it: T): keyof T =>
    ("title" in it ? "title" : Object.keys(it)[1]) as keyof T;
  const descField = (it: T): keyof T =>
    ("summary" in it
      ? "summary"
      : "description" in it
        ? "description"
        : Object.keys(it)[2]) as keyof T;

  return (
    <div className="space-y-2">
      <p className="text-xs font-semibold uppercase tracking-wide text-muted-foreground">
        {props.label}({items.length})
      </p>
      {items.map((it, idx) => (
        <div
          key={getId(it)}
          className="rounded border border-border/70 bg-background p-2 space-y-1.5"
        >
          <div className="flex items-start justify-between gap-2">
            <p className="text-xs text-muted-foreground">
              id: <code>{getId(it)}</code>
            </p>
            <Button
              size="sm"
              variant="ghost"
              onClick={() => remove(idx)}
              className="text-destructive hover:text-destructive"
            >
              删除
            </Button>
          </div>
          <LabeledInput
            label="标题"
            value={getTitle(it)}
            onChange={(v) =>
              update(idx, { [titleField(it)]: v } as Partial<T>)
            }
          />
          <LabeledTextarea
            label={props.descLabel}
            value={getDesc(it)}
            onChange={(v) =>
              update(idx, { [descField(it)]: v } as Partial<T>)
            }
            rows={2}
          />
        </div>
      ))}
      <Button size="sm" variant="outline" onClick={add}>
        ＋ 添加 {props.label}
      </Button>
    </div>
  );
}

// ---- VolumePlansEditor ----

function VolumePlansEditor(props: {
  /** P6-F-06:候选 4 摊入 — 用于卷卡 "打开 VolumePlanPage →" 深链。 */
  workspaceId: string;
  value: VolumePlansOutput | null;
  onChange: (next: VolumePlansOutput) => void;
}) {
  const volumes = props.value?.volumes ?? [];

  const update = (idx: number, next: VolumePlanItem) => {
    const arr = volumes.slice();
    arr[idx] = next;
    props.onChange({ volumes: arr });
  };
  const remove = (idx: number) => {
    const arr = volumes.slice();
    arr.splice(idx, 1);
    props.onChange({ volumes: arr });
  };
  const add = () => {
    const next: VolumePlanItem = {
      volume_id: newId("vol"),
      title: "新卷",
      summary: "",
      chapter_count: 1,
      tension_template: "free",
      arcs: [],
      tension_curve: [5],
      linked_phases: [],
      linked_turning_points: [],
      linked_threads: [],
    };
    props.onChange({ volumes: [...volumes, next] });
  };

  if (volumes.length === 0) {
    return (
      <EmptySectionHint
        hint="卷规划段为空。"
        addLabel="添加卷"
        onAdd={add}
      />
    );
  }

  return (
    <div className="space-y-3" data-testid="outline-editor-volume-plans">
      {volumes.map((v, idx) => (
        <VolumeCardEditor
          key={v.volume_id}
          workspaceId={props.workspaceId}
          item={v}
          onChange={(next) => update(idx, next)}
          onRemove={() => remove(idx)}
        />
      ))}
      <Button size="sm" variant="outline" onClick={add}>
        ＋ 添加卷
      </Button>
      <p className="rounded bg-secondary/30 p-2 text-xs text-muted-foreground">
        提示:章数 / 张力曲线在每张卷卡右上角的"打开 VolumePlanPage →"链接
        进入编辑更直观;本页改章数会**同步重置 tension_curve 长度**,确保
        len(tension_curve) == chapter_count 不变式。
      </p>
    </div>
  );
}

function VolumeCardEditor(props: {
  /** P6-F-06:用于卷卡右上角 "打开 VolumePlanPage →" 深链 href。 */
  workspaceId: string;
  item: VolumePlanItem;
  onChange: (next: VolumePlanItem) => void;
  onRemove: () => void;
}) {
  const v = props.item;
  const patch = <K extends keyof VolumePlanItem>(
    key: K,
    val: VolumePlanItem[K],
  ) => props.onChange({ ...v, [key]: val });

  const handleChapterCountChange = (next: number) => {
    const clamped = Math.max(1, Math.min(200, next));
    const curve = v.tension_curve ?? [];
    let nextCurve: number[];
    if (curve.length === clamped) {
      nextCurve = curve;
    } else if (curve.length > clamped) {
      nextCurve = curve.slice(0, clamped);
    } else {
      const tail = curve.length > 0 ? curve[curve.length - 1] : 5;
      nextCurve = [...curve];
      while (nextCurve.length < clamped) nextCurve.push(tail);
    }
    props.onChange({
      ...v,
      chapter_count: clamped,
      tension_curve: nextCurve,
    });
  };

  const updateArcs = (next: VolumeArcPlan[]) =>
    props.onChange({ ...v, arcs: next });

  // P6-F-06:候选 4 摊入 — 卷卡右上角 "打开 VolumePlanPage →" 深链。
  // 新建卷(volume_id 前缀 `vol-` 等本地占位)在保存前不可访问 VolumePlanPage,
  // 此时不渲染链接(避免 404)。
  const isPersistedVolume = !v.volume_id.startsWith("vol-");
  const volumePlanHref = `/w/${encodeURIComponent(props.workspaceId)}/volumes/${encodeURIComponent(v.volume_id)}`;

  return (
    <div className="rounded border border-border bg-secondary/30 p-3 space-y-2">
      <div className="flex items-start justify-between gap-2">
        <p className="text-xs text-muted-foreground">
          id: <code>{v.volume_id}</code>
        </p>
        <div className="flex items-center gap-2">
          {isPersistedVolume && (
            <Link
              to={volumePlanHref}
              data-testid="volume-card-open-volume-plan-link"
              className="text-xs text-sky-700 underline-offset-2 hover:underline"
              title={`打开 ${v.volume_id} 的 VolumePlanPage(章数 + 张力曲线编辑)`}
            >
              打开 VolumePlanPage →
            </Link>
          )}
          <Button
            size="sm"
            variant="ghost"
            onClick={props.onRemove}
            className="text-destructive hover:text-destructive"
          >
            删除卷
          </Button>
        </div>
      </div>
      <div className="grid gap-2 sm:grid-cols-2">
        <LabeledInput
          label="卷标题"
          value={v.title}
          onChange={(val) => patch("title", val)}
        />
        <LabeledNumber
          label="章数 chapter_count"
          value={v.chapter_count}
          onChange={handleChapterCountChange}
          min={1}
          max={200}
        />
      </div>
      <LabeledSelect
        label="张力曲线模板"
        value={v.tension_template}
        onChange={(val) => patch("tension_template", val)}
        options={TENSION_TEMPLATE_OPTIONS}
      />
      <LabeledTextarea
        label="本卷在主线中的位置"
        value={v.summary}
        onChange={(val) => patch("summary", val)}
        rows={2}
      />
      <ArcsEditor arcs={v.arcs ?? []} onChange={updateArcs} />
    </div>
  );
}

function ArcsEditor(props: {
  arcs: VolumeArcPlan[];
  onChange: (next: VolumeArcPlan[]) => void;
}) {
  const update = (idx: number, partial: Partial<VolumeArcPlan>) => {
    const arr = props.arcs.slice();
    arr[idx] = { ...arr[idx], ...partial };
    props.onChange(arr);
  };
  const remove = (idx: number) => {
    const arr = props.arcs.slice();
    arr.splice(idx, 1);
    props.onChange(arr);
  };
  const add = () => {
    const next: VolumeArcPlan = {
      arc_id: newId("arc"),
      arc_type: "主线",
      chapter_range: [1, 1],
      phase: "opening",
      summary: "",
    };
    props.onChange([...props.arcs, next]);
  };

  return (
    <div className="space-y-1.5">
      <p className="text-xs font-semibold uppercase tracking-wide text-muted-foreground">
        卷内弧({props.arcs.length})
      </p>
      {props.arcs.map((arc, idx) => (
        <div
          key={arc.arc_id}
          className="rounded border border-border/70 bg-background p-2 space-y-1"
        >
          <div className="flex items-start justify-between gap-2">
            <p className="text-xs text-muted-foreground">
              id: <code>{arc.arc_id}</code>
            </p>
            <Button
              size="sm"
              variant="ghost"
              onClick={() => remove(idx)}
              className="text-destructive hover:text-destructive"
            >
              删除
            </Button>
          </div>
          <div className="grid gap-1.5 sm:grid-cols-2">
            <LabeledInput
              label="弧类型 arc_type"
              value={arc.arc_type}
              onChange={(v) => update(idx, { arc_type: v })}
              placeholder="主线 / 副本 / 支线"
            />
            <LabeledSelect
              label="阶段 phase"
              value={arc.phase}
              onChange={(v) => update(idx, { phase: v })}
              options={ARC_PHASE_OPTIONS}
            />
            <LabeledNumber
              label="起始章号"
              value={arc.chapter_range[0]}
              onChange={(v) =>
                update(idx, {
                  chapter_range: [v, arc.chapter_range[1]],
                })
              }
              min={1}
            />
            <LabeledNumber
              label="结束章号"
              value={arc.chapter_range[1]}
              onChange={(v) =>
                update(idx, {
                  chapter_range: [arc.chapter_range[0], v],
                })
              }
              min={1}
            />
          </div>
          <LabeledTextarea
            label="弧概要"
            value={arc.summary}
            onChange={(v) => update(idx, { summary: v })}
            rows={2}
          />
        </div>
      ))}
      <Button size="sm" variant="outline" onClick={add}>
        ＋ 添加弧
      </Button>
    </div>
  );
}
