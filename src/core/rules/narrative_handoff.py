"""纯程序叙事交接：核对来源和引用，不判断文学好坏。"""

from __future__ import annotations

from typing import TYPE_CHECKING

from src.core.models.narrative import CharacterMotive, NarrativeContext, NarrativeEventContract, ReaderInformation

if TYPE_CHECKING:
    from src.core.models.outline import MiniPlanItem, CharactersOutput, WorldOutput


def reader_source_catalog(world: WorldOutput, characters: CharactersOutput) -> dict[str, str]:
    """只提供可披露的已有世界与人物字段，不读取 secrets 或未来事件。"""
    result = {f"world.{key}": getattr(world, key) for key in (
        "setting", "power_system", "factions", "culture", "unique_rules",
    )}
    for character in characters.characters:
        for key in ("archetype", "appearance", "personality", "background", "motivation",
                    "present_desire", "personal_stakes"):
            value = getattr(character, key)
            if value:
                result[f"characters.{character.character_id}.{key}"] = value
    return result


def assemble_narrative_context(
    *, world: WorldOutput, characters: CharactersOutput,
    reader_information: list[ReaderInformation],
    event_contract: NarrativeEventContract | None = None,
) -> NarrativeContext | None:
    """定位与动机逐字继承，交代材料必须来自列出的原文。"""
    if world.story_intent is None:
        if reader_information or event_contract is not None:
            raise ValueError("旧世界没有故事定位，不能静默加入新叙事材料")
        return None
    catalog = reader_source_catalog(world, characters)
    seen: set[tuple[str, str]] = set()
    for fact in reader_information:
        if fact.source_path not in catalog or fact.text not in catalog[fact.source_path]:
            raise ValueError(f"读者材料来源不符：{fact.source_path}，须引用该字段原文")
        key = (fact.source_path, fact.text)
        if key in seen:
            raise ValueError("同一读者材料不能重复或同时交代与保留")
        seen.add(key)
    return NarrativeContext(
        version=2 if event_contract is not None else 1,
        event_contract=event_contract,
        story_intent=world.story_intent,
        reader_information=reader_information,
        character_motives={character.character_id: CharacterMotive(
            present_desire=character.present_desire,
            personal_stakes=character.personal_stakes,
        ) for character in characters.characters},
    )


def mini_plan_review_design(item: MiniPlanItem) -> dict[str, object]:
    """同次节拍核对看见真正将交给卡片的事实，不仅是剧情概览。"""
    data = item.model_dump(mode="json", include={
        "one_line_event", "chapter_focus", "arc_beats", "interaction_focus", "breakthrough_payoff",
    })
    if item.narrative_context is not None and item.narrative_context.event_contract is not None:
        data["event_contract"] = item.narrative_context.event_contract.model_dump(mode="json")
    return data
