/**
 * OutlineSectionView(P5-F-03 落地)。
 *
 * 只读结构化展示 outline 4 段产出(world / characters / plot / volume_plans),
 * 按后端 schema 字段渲染对应子视图。
 *
 * **不做编辑**:plan §P5-F-03 ② 字面 "markdown 编辑器" 与后端 schema 实情冲突
 * (OutlineOut 4 段是结构化 JSON 而非 markdown 字符串),类 A 处置维护人 2026-05-12
 * 拍 A — 按后端规格源走,本任务只读;真编辑留 P5-F-05 OutlinePage 做结构化字段
 * 表单(plan §P5-F-05 同步修正)。详 [docs/impl/p5_f_03_new_book_page_right.md] §4。
 *
 * 段空(段 = null / undefined)时显示 placeholder;有数据时调用对应子视图。
 */

import type {
  CharacterOutlineItem,
  CharactersOutput,
  OutlineOut,
  OutlineSection,
  PlotOutput,
  VolumeArcPlan,
  VolumePlanItem,
  VolumePlansOutput,
  WorldOutput,
} from "@/api/outline";

const ARC_PHASE_LABELS: Record<VolumeArcPlan["phase"], string> = {
  opening: "开场",
  rising: "上升",
  climax: "高潮",
  falling: "回落",
  resolution: "收束",
};

interface OutlineSectionViewProps {
  outline: OutlineOut;
  section: OutlineSection;
}

export function OutlineSectionView(props: OutlineSectionViewProps) {
  const { outline, section } = props;

  if (section === "world") {
    return <WorldView world={outline.world ?? null} />;
  }
  if (section === "characters") {
    return <CharactersView characters={outline.characters ?? null} />;
  }
  if (section === "plot") {
    return <PlotView plot={outline.plot ?? null} />;
  }
  return <VolumePlansView volumePlans={outline.volume_plans ?? null} />;
}

function EmptyPlaceholder(props: { hint: string }) {
  return (
    <div className="rounded border border-dashed border-border p-4 text-sm text-muted-foreground">
      {props.hint}
    </div>
  );
}

function FieldBlock(props: { label: string; value: string }) {
  return (
    <div className="space-y-1">
      <p className="text-xs font-semibold uppercase tracking-wide text-muted-foreground">
        {props.label}
      </p>
      <p className="whitespace-pre-wrap text-sm leading-relaxed">
        {props.value || <span className="text-muted-foreground">(空)</span>}
      </p>
    </div>
  );
}

// ---- world ----

function WorldView(props: { world: WorldOutput | null }) {
  if (!props.world) {
    return <EmptyPlaceholder hint="世界观段尚未生成,等待 OutlineBuilder 产出。" />;
  }
  const w = props.world;
  return (
    <div className="space-y-3" data-testid="outline-section-world">
      <FieldBlock label="时代 / 地理设定" value={w.setting} />
      <FieldBlock label="力量 / 技术体系" value={w.power_system} />
      <FieldBlock label="主要势力" value={w.factions} />
      <FieldBlock label="文化 / 经济 / 社会" value={w.culture} />
      <FieldBlock label="本世界硬规则" value={w.unique_rules} />
      {w.story_intent && (
        <div className="space-y-2 rounded border border-border p-3">
          <p className="text-sm font-semibold">故事定位</p>
          {w.story_intent.planning_scope && (
            <FieldBlock label="本次规划范围与参考篇幅" value={
              `${w.story_intent.planning_scope.mode === "opening_segment" ? "长篇开局" : "完整故事"}；` +
              `${w.story_intent.planning_scope.total_volumes}卷，每卷${w.story_intent.planning_scope.chapters_per_volume}章，` +
              `每章参考约${w.story_intent.planning_scope.reference_chapter_words}字（按经历自然收束）`
            } />
          )}
          {w.story_intent.scope_outcome && <FieldBlock label="本段预期成果" value={w.story_intent.scope_outcome} />}
          <FieldBlock label="阅读兴趣" value={w.story_intent.reader_interest} />
          <FieldBlock label="主角起点" value={w.story_intent.protagonist_start} />
          <FieldBlock label="个人愿望" value={w.story_intent.personal_drive} />
          <FieldBlock label="主要压力" value={w.story_intent.central_pressure} />
          <FieldBlock label="叙述调性" value={w.story_intent.narrative_tone} />
          <FieldBlock label="开篇需要认识什么" value={w.story_intent.opening_orientation} />
          {w.story_intent.progression_design && (
            <div className="space-y-2 border-t border-border pt-3">
              <p className="text-sm font-semibold">故事推进设计</p>
              <FieldBlock label="持续吸引读者的动力" value={w.story_intent.progression_design.story_engine} />
              <FieldBlock label="开篇如何进入故事" value={w.story_intent.progression_design.opening_development} />
              <FieldBlock label="阶段怎样向前发展" value={w.story_intent.progression_design.stage_progression} />
              <FieldBlock label="阶段收获与后续期待" value={w.story_intent.progression_design.payoff_rhythm} />
              <FieldBlock label="时间跨度与详略" value={w.story_intent.progression_design.time_and_detail} />
            </div>
          )}
        </div>
      )}
    </div>
  );
}

// ---- characters ----

const ROLE_LABELS: Record<CharacterOutlineItem["role"], string> = {
  protagonist: "主角",
  deuteragonist: "次主角",
  antagonist: "反派",
  supporting: "配角",
};

function CharactersView(props: { characters: CharactersOutput | null }) {
  if (!props.characters) {
    return (
      <EmptyPlaceholder hint="人设段尚未生成,等待 OutlineBuilder 产出。" />
    );
  }
  const items = props.characters.characters ?? [];
  if (items.length === 0) {
    return <EmptyPlaceholder hint="人设段已生成但列表为空。" />;
  }
  return (
    <div className="space-y-3" data-testid="outline-section-characters">
      {items.length >= 8 && (
        <p role="status" className="text-sm text-muted-foreground">
          核心人物档案已达八人上限，自动补人已无空位；这不代表后续剧情引用的人物都已补齐。
        </p>
      )}
      {items.map((c) => (
        <CharacterCard key={c.character_id} character={c} />
      ))}
    </div>
  );
}

function CharacterCard(props: { character: CharacterOutlineItem }) {
  const c = props.character;
  const rels = c.relationships_brief ?? {};
  const relsEntries = Object.entries(rels);
  return (
    <div className="rounded border border-border bg-secondary/30 p-3 space-y-2">
      <div className="flex items-baseline justify-between gap-2">
        <div>
          <p className="text-sm font-semibold">
            {c.name}
            <span className="ml-2 rounded bg-secondary px-1.5 py-0.5 text-xs font-normal text-muted-foreground">
              {ROLE_LABELS[c.role]}
            </span>
          </p>
          <p className="text-xs text-muted-foreground">
            id: <code>{c.character_id}</code> · 原型: {c.archetype}
          </p>
        </div>
      </div>
      <FieldBlock label="外貌" value={c.appearance} />
      <FieldBlock label="性格" value={c.personality} />
      <FieldBlock label="背景" value={c.background} />
      <FieldBlock label="驱动力" value={c.motivation} />
      {c.present_desire && <FieldBlock label="眼前愿望" value={c.present_desire} />}
      {c.personal_stakes && <FieldBlock label="切身利害" value={c.personal_stakes} />}
      <FieldBlock label="说话风格" value={c.voice_hint} />
      {relsEntries.length > 0 && (
        <div className="space-y-1">
          <p className="text-xs font-semibold uppercase tracking-wide text-muted-foreground">
            关系
          </p>
          <ul className="space-y-0.5 text-sm">
            {relsEntries.map(([targetId, desc]) => (
              <li key={targetId}>
                → <code className="text-xs">{targetId}</code>:{desc}
              </li>
            ))}
          </ul>
        </div>
      )}
    </div>
  );
}

// ---- plot ----

function PlotView(props: { plot: PlotOutput | null }) {
  if (!props.plot) {
    return <EmptyPlaceholder hint="主线段尚未生成,等待 OutlineBuilder 产出。" />;
  }
  const p = props.plot;
  const arcs = Object.entries(p.character_arcs ?? {});
  return (
    <div className="space-y-3" data-testid="outline-section-plot">
      <FieldBlock label="核心冲突" value={p.core_conflict} />
      <ListBlock
        label={`主线阶段(${p.main_phases.length})`}
        items={p.main_phases.map((ph) => ({
          id: ph.phase_id,
          title: ph.title,
          desc: ph.development
            ? `${ph.summary}\n\n实际经历：${ph.development.experience}\n阶段变化：${ph.development.resulting_change}\n参考篇幅占比：${ph.development.space_share}%（规划参考）\n可概述过程：${ph.development.summary_processes || "无"}`
            : ph.summary,
        }))}
      />
      <ListBlock
        label={`关键转折(${p.major_turning_points.length})`}
        items={p.major_turning_points.map((tp) => ({
          id: tp.turning_id,
          title: tp.title,
          desc: tp.description,
        }))}
      />
      <ListBlock
        label={`长伏笔(${p.long_threads.length})`}
        items={p.long_threads.map((lt) => ({
          id: lt.thread_id,
          title: lt.title,
          desc: lt.description,
        }))}
      />
      {arcs.length > 0 && (
        <div className="space-y-1">
          <p className="text-xs font-semibold uppercase tracking-wide text-muted-foreground">
            角色弧光
          </p>
          <ul className="space-y-1 text-sm">
            {arcs.map(([charId, desc]) => (
              <li key={charId}>
                <code className="text-xs">{charId}</code>:{desc}
              </li>
            ))}
          </ul>
        </div>
      )}
    </div>
  );
}

interface ListItem {
  id: string;
  title: string;
  desc: string;
}

function ListBlock(props: { label: string; items: ListItem[] }) {
  if (props.items.length === 0) return null;
  return (
    <div className="space-y-1">
      <p className="text-xs font-semibold uppercase tracking-wide text-muted-foreground">
        {props.label}
      </p>
      <ul className="space-y-1.5 text-sm">
        {props.items.map((item) => (
          <li
            key={item.id}
            className="rounded border border-border/60 bg-background p-2"
          >
            <p className="font-medium">
              {item.title}
              <code className="ml-2 text-xs text-muted-foreground">
                {item.id}
              </code>
            </p>
            <p className="mt-0.5 whitespace-pre-wrap text-muted-foreground">
              {item.desc}
            </p>
          </li>
        ))}
      </ul>
    </div>
  );
}

// ---- volume_plans ----

const TENSION_TEMPLATE_LABELS: Record<
  VolumePlanItem["tension_template"],
  string
> = {
  heros_journey: "英雄之旅",
  steady_daily: "稳态日常",
  strong_stimulation: "强刺激",
  free: "自由",
};

function VolumePlansView(props: { volumePlans: VolumePlansOutput | null }) {
  if (!props.volumePlans) {
    return (
      <EmptyPlaceholder hint="卷规划段尚未生成,等待 OutlineBuilder 产出。" />
    );
  }
  const volumes = props.volumePlans.volumes ?? [];
  if (volumes.length === 0) {
    return <EmptyPlaceholder hint="卷规划段已生成但卷列表为空。" />;
  }
  return (
    <div className="space-y-3" data-testid="outline-section-volume-plans">
      {volumes.map((v) => (
        <VolumePlanCard key={v.volume_id} volume={v} />
      ))}
    </div>
  );
}

function VolumePlanCard(props: { volume: VolumePlanItem }) {
  const v = props.volume;
  return (
    <div className="rounded border border-border bg-secondary/30 p-3 space-y-2">
      <div>
        <p className="text-sm font-semibold">
          {v.title}
          <span className="ml-2 text-xs font-normal text-muted-foreground">
            {v.chapter_count} 章 · {TENSION_TEMPLATE_LABELS[v.tension_template]}
          </span>
        </p>
        <p className="text-xs text-muted-foreground">
          id: <code>{v.volume_id}</code>
        </p>
      </div>
      <FieldBlock label="本卷在主线中的位置" value={v.summary} />
      {v.arcs && v.arcs.length > 0 && (
        <div className="space-y-1">
          <p className="text-xs font-semibold uppercase tracking-wide text-muted-foreground">
            卷内弧({v.arcs.length})
          </p>
          <ul className="space-y-1 text-sm">
            {v.arcs.map((arc) => (
              <li key={arc.arc_id}>
                <span className="font-medium">{arc.arc_type}</span>{" "}
                <span className="text-xs text-muted-foreground">
                  ch {arc.chapter_range[0]}-{arc.chapter_range[1]} ·{" "}
                  {ARC_PHASE_LABELS[arc.phase]}
                </span>
                <p className="mt-0.5 text-muted-foreground">{arc.summary}</p>
              </li>
            ))}
          </ul>
        </div>
      )}
      <div className="space-y-1">
        <p className="text-xs font-semibold uppercase tracking-wide text-muted-foreground">
          张力曲线
        </p>
        <p className="font-mono text-xs text-muted-foreground">
          [{v.tension_curve.join(", ")}]
        </p>
      </div>
    </div>
  );
}
