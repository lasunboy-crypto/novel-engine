import { useEffect, useState } from "react";
import { useQuery, useQueryClient } from "@tanstack/react-query";
import { apiGet } from "@/api/client";
import { actAuthoring, authoringKey, saveAuthoring, type AuthoringActionIn, type AuthoringView, type AuthoringSaveIn, type AuthoringPreferences } from "@/api/authoring";
import { useCancelTaskMutation, type TaskInfo } from "@/api/tasks";
import { Button } from "@/components/ui/button";
import { OutlineSectionView } from "@/components/OutlineSectionView";
import { STYLE_TEMPLATE_OPTIONS, TENSION_TEMPLATE_OPTIONS } from "@/schemas/new_book";
import { WORKSPACES_QUERY_KEY } from "@/api/workspaces";

const fieldClass = "rounded border bg-background p-2";
export function AuthoringPanel({ workspaceId, view }: { workspaceId: string; view: AuthoringView }) {
  const draft = view.draft!;
  const queryClient = useQueryClient();
  const [taskId, setTaskId] = useState(view.active_task_id);
  const [busy, setBusy] = useState(false);
  const [error, setError] = useState("");
  const [preferences, setPreferences] = useState<AuthoringPreferences>(draft.preferences!);
  const [selections, setSelections] = useState<NonNullable<AuthoringSaveIn["selections"]>>({});
  const [names, setNames] = useState<Record<string, string>>({});
  const [custom, setCustom] = useState<NonNullable<AuthoringSaveIn["custom_sections"]>>({});
  const [direction, setDirection] = useState("");
  const [restoreRevision, setRestoreRevision] = useState("");
  const task = useQuery({ queryKey: ["task", taskId], enabled: !!taskId,
    queryFn: () => apiGet<TaskInfo>(`/tasks/${taskId}`),
    refetchInterval: (query) => ["pending", "running"].includes(query.state.data?.status ?? "pending") ? 1500 : false,
  });
  const running = !!taskId && (!task.data || ["pending", "running"].includes(task.data.status));
  const disabled = busy || running;
  const cancel = useCancelTaskMutation();
  useEffect(() => {
    if (task.data && !["pending", "running"].includes(task.data.status)) {
      void queryClient.invalidateQueries({ queryKey: authoringKey(workspaceId) });
      void queryClient.invalidateQueries({ queryKey: WORKSPACES_QUERY_KEY });
    }
  }, [task.data?.status, task.data?.task_id, queryClient, workspaceId]); // eslint-disable-line react-hooks/exhaustive-deps
  const dirty = JSON.stringify(preferences) !== JSON.stringify(draft.preferences) || Object.keys(selections).length > 0 || Object.keys(names).length > 0 || Object.keys(custom).length > 0;
  const save = async () => {
    if (!dirty) return draft;
    const result = await saveAuthoring(workspaceId, { expected_revision: draft.revision, preferences, selections, names, custom_sections: custom });
    return result.draft!;
  };
  const run = async (action?: AuthoringActionIn["action"]) => {
    setBusy(true); setError("");
    try {
      const current = await save();
      if (action) {
        const job = await actAuthoring(workspaceId, { expected_revision: current.revision, action, direction,
          restore_revision: restoreRevision ? Number(restoreRevision) : undefined });
        setTaskId(job.task_id);
      }
      await queryClient.invalidateQueries({ queryKey: authoringKey(workspaceId) });
    } catch (e) { setError(e instanceof Error ? e.message : "操作失败，请刷新后继续"); }
    finally { setBusy(false); }
  };
  const pref = <K extends keyof AuthoringPreferences>(key: K, value: AuthoringPreferences[K]) => setPreferences({ ...preferences, [key]: value });
  const render = (text: string) => text.replace(/\[\[([^\]]+)\]\]/g, (_, id: string) => names[id] ?? draft.proposal?.entities.find((e) => e.entity_id === id)?.name ?? id);
  return <section className="space-y-6 rounded border p-5">
    <header className="space-y-2"><h2 className="text-xl font-semibold">小说规划 · 第 {draft.revision} 版</h2>
      <p>{draft.premise}</p><p className="text-sm text-muted-foreground">选择与改名只保存，不调用模型。可以一次改多项，再点击整理。已确认后才进入全书大纲。</p>
    </header>
    <fieldset disabled={disabled} className="space-y-5">
      {draft.proposal && <><h3 className="font-semibold">{draft.proposal.title}</h3>
        {draft.proposal.sections.map((section) => {
          const selected = selections[section.key] ?? section.selected;
          return <section key={section.key} className="space-y-2 border-b pb-4">
            <label className="font-medium">{section.title} <select className={fieldClass} aria-label={section.title} value={selected}
              onChange={(e) => setSelections({ ...selections, [section.key]: e.target.value })}>
              {section.options.map((o) => <option key={o.option_id} value={o.option_id}>{o.label}</option>)}
            </select></label>
            <p className="whitespace-pre-wrap leading-relaxed">{render(custom[section.key] ?? section.options.find((o) => o.option_id === selected)?.content ?? "")}</p>
            <details><summary className="cursor-pointer text-sm text-muted-foreground">自定义这部分（可选）</summary>
              <textarea aria-label={`自定义${section.title}`} className={`${fieldClass} w-full`} rows={3} value={custom[section.key] ?? ""}
                onChange={(e) => { const next = { ...custom }; if (e.target.value.trim()) next[section.key] = e.target.value; else delete next[section.key]; setCustom(next); }} />
            </details>
          </section>;
        })}
        <div className="space-y-3"><h3 className="font-medium">人物、组织和道具名称</h3>
          {draft.proposal.entities.map((entity) => <label className="block" key={entity.entity_id}>{entity.name}
            <select aria-label={`${entity.name}的名称`} className={`${fieldClass} ml-3`} value={names[entity.entity_id] ?? entity.name}
              onChange={(e) => setNames({ ...names, [entity.entity_id]: e.target.value })}>
              {[...new Set([entity.name, ...(entity.alternatives ?? []), names[entity.entity_id]].filter(Boolean))].map((name) => <option key={name}>{name}</option>)}
            </select>
            <details className="text-sm"><summary>自己命名（可选）</summary><input aria-label={`自定义${entity.name}名称`} className={fieldClass}
              value={names[entity.entity_id] ?? entity.name} onChange={(e) => setNames({ ...names, [entity.entity_id]: e.target.value })} /></details>
          </label>)}
        </div></>}
      <div className="flex flex-wrap gap-4">
        <label>风格模板 <select className={fieldClass} aria-label="风格模板" value={preferences.style_template ?? ""} onChange={(e) => pref("style_template", (e.target.value || null) as AuthoringPreferences["style_template"])}>
          <option value="">只用提案中的风格方向</option>{STYLE_TEMPLATE_OPTIONS.map((o) => <option key={o.value} value={o.value}>{o.label}</option>)}</select></label>
        <label>张力走向 <select className={fieldClass} aria-label="张力走向" value={preferences.tension_template} onChange={(e) => pref("tension_template", e.target.value as AuthoringPreferences["tension_template"])}>
          {TENSION_TEMPLATE_OPTIONS.map((o) => <option key={o.value} value={o.value}>{o.label}</option>)}</select></label>
        <label>全书卷数 <select aria-label="全书卷数" className={fieldClass} value={preferences.total_volumes}
          onChange={(e) => setPreferences({ ...preferences, total_volumes: Number(e.target.value), volume_chapters: [] })}>
          {Array.from({ length: 20 }, (_, i) => i + 1).map((n) => <option key={n}>{n}</option>)}</select></label>
        <label>每卷参考章数 <select aria-label="每卷参考章数" className={fieldClass} value={preferences.chapters_per_volume}
          onChange={(e) => setPreferences({ ...preferences, chapters_per_volume: Number(e.target.value), volume_chapters: [] })}>
          {[...new Set([12, 20, 30, 40, 60, 100, preferences.chapters_per_volume])].map((n) => <option key={n}>{n}</option>)}</select></label>
        <label><input type="checkbox" checked={preferences.fixed_chapter_counts} onChange={(e) => pref("fixed_chapter_counts", e.target.checked)} /> 固定各卷章数</label>
      </div>
      <details><summary>分别选择各卷章数</summary><div className="flex flex-wrap gap-3">{Array.from({ length: preferences.total_volumes }, (_, i) => {
        const counts = preferences.volume_chapters?.length ? preferences.volume_chapters : Array(preferences.total_volumes).fill(preferences.chapters_per_volume) as number[];
        return <label key={i}>第{i + 1}卷 <select aria-label={`第${i + 1}卷章数`} className={fieldClass} value={counts[i]} onChange={(e) => { const next = [...counts]; next[i] = Number(e.target.value); pref("volume_chapters", next); }}>
          {Array.from({ length: 199 }, (_, j) => j + 2).map((n) => <option key={n}>{n}</option>)}</select></label>;
      })}</div></details>
      <div className="flex flex-wrap gap-4">
        <label>开篇铺垫 <select className={fieldClass} aria-label="开篇铺垫" value={preferences.opening} onChange={(e) => pref("opening", e.target.value as AuthoringPreferences["opening"])}>
          <option value="direct">直接入局 · 第1章</option><option value="brief">短铺垫 · 前4章内入局</option><option value="extended">较充分 · 前7章内入局</option><option value="suggested">由提案安排</option></select></label>
        <label>人称 <select className={fieldClass} value={preferences.voice} onChange={(e) => pref("voice", e.target.value as AuthoringPreferences["voice"])}>
          <option value="third_person">第三人称</option><option value="first_person">第一人称</option><option value="free">不固定</option></select></label>
        <label>爽感 <select className={fieldClass} value={preferences.payoff} onChange={(e) => pref("payoff", e.target.value as AuthoringPreferences["payoff"])}>
          <option value="natural">自然展开</option><option value="balanced">均衡</option><option value="strong">更强的收获与张力</option></select></label>
        <label>感情线 <select className={fieldClass} value={preferences.romance} onChange={(e) => pref("romance", e.target.value as AuthoringPreferences["romance"])}>
          <option value="suggested">采用提案安排</option><option value="none">不设爱情主线</option><option value="light">轻感情线</option><option value="important">重要感情线</option></select></label>
        <label>每章参考字数 <select className={fieldClass} value={preferences.reference_chapter_words} onChange={(e) => pref("reference_chapter_words", Number(e.target.value))}>
          {[1500, 2000, 2500, 3000, 4000].map((n) => <option key={n}>{n}</option>)}</select></label>
      </div>
      <p className="text-sm text-muted-foreground">“入局”指真正开始本书的主要经历，不能用听闻、报名或决定出发代替。字数是创作参考，不会触发拒稿或凑字数重写。</p>
      {view.contract && <div><h3 className="font-medium">开局节点的具体含义</h3>{view.contract.milestones.map((m) => <p key={m.milestone_id}>{m.event}</p>)}</div>}
      <details><summary>额外阅读偏好（可选）</summary><textarea className={`${fieldClass} w-full`} value={preferences.reading_reference} onChange={(e) => pref("reading_reference", e.target.value)} /></details>
      <div className="flex flex-wrap gap-3"><Button variant="outline" disabled={!dirty} onClick={() => void run()}>保存选择 · 不调用模型</Button>
        {!draft.proposal ? <Button onClick={() => void run("propose")}>生成故事提案</Button> : <Button onClick={() => void run("prepare")}>整理完整规划</Button>}
      </div>
      {draft.proposal && <details><summary>主线或其他部分不满意，批量调整提案</summary>
        <textarea className={`${fieldClass} w-full`} aria-label="调整方向" placeholder="可选：说明哪部分不满意。留空则按当前选择统一整理提案。" value={direction} onChange={(e) => setDirection(e.target.value)} />
        <Button variant="outline" onClick={() => void run("revise")}>调整当前提案</Button></details>}
      {!!view.history?.length && <details><summary>规划历史</summary><select aria-label="历史版本" className={fieldClass} value={restoreRevision} onChange={(e) => setRestoreRevision(e.target.value)}>
        <option value="">选择版本</option>{view.history.map((n) => <option key={n} value={n}>第{n}版</option>)}</select>
        <Button disabled={!restoreRevision || dirty} variant="outline" onClick={() => void run("restore")}>恢复为新草稿</Button></details>}
    </fieldset>
    {disabled && <p role="status">{running ? "规划任务进行中，已完成步骤会保存。" : "保存中…"}</p>}
    {running && taskId && <Button variant="outline" disabled={cancel.isPending} onClick={() => cancel.mutate(taskId)}>暂停任务</Button>}
    {(error || (task.data && ["failed", "interrupted", "cancelled"].includes(task.data.status))) && <p role="alert" className="text-destructive">{error || String(task.data?.error?.message ?? "任务已停止；点击原步骤继续，成功阶段会复用。")}</p>}
    {view.world && view.characters && view.plot && <div className="space-y-4">
      <h3 className="font-semibold">完整规划预览</h3>{(["world", "characters", "plot"] as const).map((section) => <details key={section} open={section === "plot"}>
        <summary>{{ world: "世界设定", characters: "人物与关系", plot: "全书主线" }[section]}</summary>
        <OutlineSectionView section={section} outline={{ world: view.world, characters: view.characters, plot: view.plot, volume_plans: null, mini_plan: null }} />
      </details>)}
      {draft.status === "prepared" && <Button disabled={disabled || dirty} onClick={() => void run("confirm")}>确认这份规划 · 不调用模型</Button>}
    </div>}
    {draft.status === "confirmed" && <p className="font-medium">规划已确认，下方可生成全部卷规划与章节小纲。修改选择后须重新整理和确认。</p>}
  </section>;
}
