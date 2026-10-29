import type { components } from "@/api/types";

const PARTICIPATION = {
  present: "当场参与", recalled: "回忆中出现", mentioned: "仅被提及", restricted: "本章不出场",
};
const INFORMATION = { introduce: "本章交代素材", background: "可用背景", defer: "暂缓披露" };

export function NarrativeContextView({ context }: {
  context: components["schemas"]["NarrativeContext"] | null | undefined;
}) {
  if (!context) return null;
  return (
    <section className="space-y-2 rounded border border-border p-3" data-testid="narrative-context">
      {context.event_contract && <div className="space-y-1 text-sm" data-testid="narrative-event-contract">
        <h3 className="font-semibold">小纲事实来源</h3>
        <p className="text-xs text-muted-foreground">以下事实由章节卡继承；不规定台词、心理或动作写法。</p>
        {([
          ["必要事件与结果", context.event_contract.required_events],
          ["章末未决问题", context.event_contract.unresolved_questions],
          ["禁止发生的结果", context.event_contract.forbidden_results],
        ] as const).map(([label, values]) => values.length > 0 && (
          <div key={label}><span className="font-medium">{label}：</span>
            <ul className="list-inside list-disc">{values.map((value) => <li key={value}>{value}</li>)}</ul>
          </div>
        ))}
      </div>}
      <h3 className="text-sm font-semibold">叙事材料</h3>
      <p className="text-xs text-muted-foreground">供写作选择和自然交代，参考材料不要求逐条写进正文。</p>
      {(context.participation ?? []).length > 0 && (
        <ul className="text-sm">
          {(context.participation ?? []).map((person) => (
            <li key={person.character_id}>{person.character_id}：{PARTICIPATION[person.mode]}</li>
          ))}
        </ul>
      )}
      {context.reader_information?.map((fact, index) => (
        <p key={`${fact.source_path}-${index}`} className="whitespace-pre-wrap text-sm">
          <span className="font-medium">{INFORMATION[fact.use]}：</span>{fact.text}
        </p>
      ))}
    </section>
  );
}
