"""首稿的只读事实视图：合并逐字相同的来源，不推断场景数或字数配额。"""

from __future__ import annotations

from dataclasses import dataclass
import re

from src.core.models.chapter_card import ChapterCard
from src.core.rules.prose_hard_contract import ExecutableConstraintPlan


@dataclass(frozen=True)
class DraftFact:
    """一段原文及其全部来源；来源只用于审计，不构成新的剧情要求。"""

    text: str
    labels: tuple[str, ...]
    sources: tuple[str, ...]
    hard_ids: tuple[str, ...]


@dataclass(frozen=True)
class WriterDraftView:
    """同一冻结计划的模型展示与覆盖证据，不产生新的持久化合同。"""

    plan_hash: str
    facts: tuple[DraftFact, ...]

    def assert_covers(self, plan: ExecutableConstraintPlan) -> None:
        """ID、级别所属计划及原文必须一一绑定，禁止只检查文本命中。"""
        if self.plan_hash != plan.plan_hash:
            raise ValueError("Writer 首稿 plan hash 漂移")
        actual: dict[str, str] = {}
        for fact in self.facts:
            for identity in fact.hard_ids:
                if identity in actual:
                    raise ValueError("Writer 首稿 HARD 重复绑定")
                actual[identity] = fact.text
        expected = {item.constraint_id: item.rule_text for item in plan.constraints}
        if actual != expected:
            raise ValueError("Writer 首稿 HARD 原文/身份覆盖漂移")
        for item in plan.constraints:
            owner = next(
                fact for fact in self.facts if item.constraint_id in fact.hard_ids
            )
            if item.source_path not in owner.sources:
                raise ValueError("Writer 首稿 HARD 来源绑定漂移")

    def render(self, *, include_audit_ids: bool = True) -> str:
        """展示全部原文；自然分组不改变内部身份、事实或必须遵守的级别。"""
        if not include_audit_ids:
            groups: dict[str, list[str]] = {
                "必须发生的事件与结果": [],
                "人物变化与互动方向": [],
                "必须保留的悬念": [],
                "本章禁止发生的事": [],
                "必须遵守的条件": [],
            }
            for fact in self.facts:
                if "禁止发生" in fact.labels:
                    group = "本章禁止发生的事"
                elif "保持未解决" in fact.labels:
                    group = "必须保留的悬念"
                elif "须兑现的行动与结果" in fact.labels:
                    group = "必须发生的事件与结果"
                elif fact.hard_ids:
                    group = "必须遵守的条件"
                else:
                    group = "人物变化与互动方向"
                groups[group].append(fact.text)
            return "\n\n".join([
                "## 本章写作材料",
                "事件、保留的悬念、禁区和明确条件必须遵守；人物互动方向用于指导演出。"
                "以下材料可以在同一场景中交织，不是独立场次或段落配额。"
                "原文明示的前置条件、先后动作和因果关系必须成立。",
                *(f"### {name}\n\n" + "\n\n".join(texts)
                  for name, texts in groups.items() if texts),
            ])
        lines = [
            "## 本章事实与要求",
            f"plan_hash: {self.plan_hash}",
            "以下要求可以在同一场景中自然交织，不代表独立场次或段落配额。"
            "原文明示的前置条件、先后动作和因果关系必须成立。",
        ]
        for fact in self.facts:
            identity = (
                " / ".join(fact.labels) + "；HARD " + ", ".join(fact.hard_ids)
                if fact.hard_ids
                else " / ".join(fact.labels)
            )
            lines.append(f"[{identity}]\n{fact.text}")
        return "\n\n".join(lines)


def build_writer_draft_view(
    card: ChapterCard, plan: ExecutableConstraintPlan
) -> WriterDraftView:
    """保留每个来源，只有逐字相同的整段事实才共享一个自然文本所有者。"""
    plan.assert_matches_chapter_card(card)
    if plan.stage != "draft":
        raise ValueError("首稿事实视图只能消费 draft 计划")
    facts: dict[str, DraftFact] = {}

    def add(text: str, label: str, source: str, hard_id: str | None = None) -> None:
        if not text.strip():
            return
        previous = facts.get(text, DraftFact(text, (), (), ()))
        facts[text] = DraftFact(
            text=text,
            labels=tuple(dict.fromkeys((*previous.labels, label))),
            sources=tuple(dict.fromkeys((*previous.sources, source))),
            hard_ids=tuple(
                dict.fromkeys((*previous.hard_ids, *((hard_id,) if hard_id else ())))
            ),
        )

    for index, text in enumerate(card.must_achieve):
        add(text, "须兑现的行动与结果", f"must_achieve[{index}]")
    if card.dramatic_source is not None:
        for field, label in (
            ("actor_goal", "目标"),
            ("counteraction", "反作用"),
            ("choice", "选择"),
            ("consequence", "后果"),
        ):
            add(
                getattr(card.dramatic_source.beat_contract, field),
                label,
                f"dramatic_source.beat_contract.{field}",
            )
    for character_id, text in card.arc_beats.items():
        add(text, f"人物变化：{character_id}", f"arc_beats[{character_id}]")
    for index, text in enumerate(card.must_unresolved):
        add(text, "保持未解决", f"must_unresolved[{index}]")
    for index, text in enumerate(card.forbidden):
        add(text, "禁止发生", f"forbidden[{index}]")
    for item in plan.constraints:
        label = (
            "正文要求"
            if item.rule_text not in facts
            else facts[item.rule_text].labels[0]
        )
        add(item.rule_text, label, item.source_path, item.constraint_id)
    view = WriterDraftView(plan.plan_hash, tuple(facts.values()))
    view.assert_covers(plan)
    return view


def draft_instruction_leakage(
    content: str, plan: ExecutableConstraintPlan
) -> tuple[str, ...]:
    """只识别明确内部标记或强组合；引号内的书中书/对白不作叙述层泄漏。"""
    narration = re.sub(r'"[^"\n]*"|“[^”]*”|「[^」]*」|『[^』]*』', "", content)
    reasons: list[str] = []
    if any(
        identity in narration
        for identity in (
            plan.plan_hash,
            *(item.constraint_id for item in plan.constraints),
        )
    ):
        reasons.append("internal_constraint_identity")
    if "留待下一章边界之外" in narration or any(
        "本章只写到" in paragraph and "下一章" in paragraph
        for paragraph in re.split(r"\n\s*\n", narration)
    ):
        reasons.append("narrative_boundary_instruction")
    return tuple(reasons)
