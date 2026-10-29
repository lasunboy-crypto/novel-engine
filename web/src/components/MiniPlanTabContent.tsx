/**
 * MiniPlanTabContent — OutlinePage 第 5 tab(v1.0.x wave 4 session 5)。
 *
 * 展示 + 编辑 outline.mini_plan(全书章级事件分配,wave 4 session 1 引入)。
 * 表格视图:按 (volume_id, chapter_number) 排序,每行一章 — chapter_id /
 * volume_id / arc_id / one_line_event / linked_characters / linked_threads。
 *
 * **编辑态**:`one_line_event` 单字段 inline 编辑(其他字段只读;volume_id /
 * arc_id 等 ID 字段改动会破坏 outline 跨段引用完整性,留 v1.1 真实战反馈
 * 后给"高级编辑"入口)。改动后整段 patch 提交。
 */

import { useMemo } from "react";

import type { MiniPlanItem, MiniPlanOutput } from "@/api/outline";
import { NarrativeContextView } from "@/components/NarrativeContextView";

interface MiniPlanTabContentProps {
  miniPlan: MiniPlanOutput | null;
  editing: boolean;
  onChange: (next: MiniPlanOutput) => void;
}

export function MiniPlanTabContent(props: MiniPlanTabContentProps) {
  const { miniPlan, editing, onChange } = props;
  const items = useMemo<MiniPlanItem[]>(
    () =>
      miniPlan?.items
        ? [...miniPlan.items].sort((a, b) => {
            if (a.volume_id !== b.volume_id) {
              return a.volume_id.localeCompare(b.volume_id);
            }
            return a.chapter_number - b.chapter_number;
          })
        : [],
    [miniPlan],
  );

  if (!miniPlan || items.length === 0) {
    return (
      <div
        className="rounded border border-dashed border-border p-4 text-sm text-muted-foreground"
        data-testid="outline-section-mini-plan-empty"
      >
        本工作区尚未生成 mini_plan(章级事件分配,wave 4 session 1
        起 OutlineBuilder 自动产出)。旧工作区 outline 也可能为空,
        请重新跑 outline 生成或单段重生 mini_plan 段。
      </div>
    );
  }

  const handleEventChange = (idx: number, next: string) => {
    const nextItems = items.map((it, i) =>
      i === idx ? { ...it, one_line_event: next } : it,
    );
    onChange({ items: nextItems });
  };

  return (
    <div className="space-y-2" data-testid="outline-section-mini-plan">
      <p className="text-xs text-muted-foreground">
        每章一行:本章承担的事件(主语 + 动词 + 宾语三元素强制差异化,
        避免 LLM 反复抽 arc summary 同一组动作)。共 {items.length} 章。
      </p>
      <div className="overflow-x-auto rounded border border-border">
        <table
          className="w-full text-xs"
          data-testid="outline-section-mini-plan-table"
        >
          <thead className="bg-secondary/40">
            <tr>
              <th className="px-2 py-1.5 text-left font-medium">章节 ID</th>
              <th className="px-2 py-1.5 text-left font-medium">卷 ID</th>
              <th className="px-2 py-1.5 text-left font-medium">弧 ID</th>
              <th className="px-2 py-1.5 text-left font-medium">章号</th>
              <th className="px-2 py-1.5 text-left font-medium">
                one_line_event(本章事件)
              </th>
              <th className="px-2 py-1.5 text-left font-medium">关联角色</th>
              <th className="px-2 py-1.5 text-left font-medium">关联伏笔</th>
            </tr>
          </thead>
          <tbody>
            {items.map((it, idx) => (
              <tr
                key={it.chapter_id}
                className="border-t border-border/60 align-top"
              >
                <td className="px-2 py-1.5 font-mono">{it.chapter_id}</td>
                <td className="px-2 py-1.5 font-mono">{it.volume_id}</td>
                <td className="px-2 py-1.5 font-mono">{it.arc_id}</td>
                <td className="px-2 py-1.5">{it.chapter_number}</td>
                <td className="px-2 py-1.5">
                  {editing ? (
                    <textarea
                      value={it.one_line_event}
                      onChange={(e) => handleEventChange(idx, e.target.value)}
                      rows={2}
                      className="w-full min-w-[16rem] rounded border border-border bg-background px-2 py-1 text-xs"
                      data-testid={`outline-mini-plan-event-${it.chapter_id}`}
                    />
                  ) : (
                    <span className="whitespace-pre-wrap leading-relaxed">
                      {it.one_line_event}
                    </span>
                  )}
                </td>
                <td className="px-2 py-1.5 text-muted-foreground">
                  {it.linked_characters && it.linked_characters.length > 0
                    ? it.linked_characters.join(", ")
                    : "—"}
                </td>
                <td className="px-2 py-1.5 text-muted-foreground">
                  {it.linked_threads && it.linked_threads.length > 0
                    ? it.linked_threads.join(", ")
                    : "—"}
                </td>
              </tr>
            ))}
          </tbody>
        </table>
      </div>
      {items.filter((item) => item.narrative_context).map((item) => (
        <div key={item.chapter_id} className="space-y-1">
          <p className="text-sm font-medium">第 {item.chapter_number} 章阅读重点：{item.chapter_focus}</p>
          <NarrativeContextView context={item.narrative_context} />
        </div>
      ))}
      {editing && (
        <p className="text-xs text-muted-foreground">
          ⚠️ 修改 <code>one_line_event</code> 后,新批生成的章节卡 derive_context
          会用新事件;已生成的章节卡需要在 ChaptersBoardPage 手动 rewind +
          重新生成才会重新拉新事件。
        </p>
      )}
    </div>
  );
}
