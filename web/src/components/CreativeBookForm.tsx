import { useState } from "react";
import { GENRE_OPTIONS } from "@/schemas/new_book";
import type { NewBookFormProps } from "@/components/NewBookForm";
import { Button } from "@/components/ui/button";

export function CreativeBookForm({ onSubmit, disabled, submitButtonLabel }: NewBookFormProps) {
  const [premise, setPremise] = useState("");
  const [genre, setGenre] = useState("xianxia");
  return <form className="space-y-4" onSubmit={(event) => {
    event.preventDefault();
    if (!premise.trim()) return;
    onSubmit({ values: { workspace_id: `book-${crypto.randomUUID().slice(0, 12)}`,
      display_name: "新故事", premise: premise.trim(), genre,
      total_volumes: 5, chapters_per_volume: 30, planning_mode: "whole_book",
      default_voice: "third_person", tension_template: "heros_journey",
      style_template: genre === "xianxia" ? "xianxia_growth" : "" },
      world_md_content: null, characters_md_content: null });
  }}>
    <label className="block space-y-2">最初的创意
      <textarea aria-label="最初的创意" required rows={6} value={premise} disabled={disabled}
        onChange={(event) => setPremise(event.target.value)} className="w-full rounded border bg-background p-3"
        placeholder="一句话或一段话就可以。其余内容接下来通过选择调整。" />
    </label>
    <label className="block space-y-2">题材
      <select aria-label="题材" className="ml-3 rounded border bg-background p-2" value={genre} disabled={disabled} onChange={(e) => setGenre(e.target.value)}>
        {GENRE_OPTIONS.map((o) => <option key={o.value} value={o.value}>{o.label}</option>)}
      </select>
    </label>
    <p className="text-sm text-muted-foreground">创建后先查看故事提案，再选择人物、主线、风格和全书规模。确认规划后才生成各卷与章节。</p>
    <Button type="submit" disabled={disabled || !premise.trim()}>{submitButtonLabel}</Button>
  </form>;
}
