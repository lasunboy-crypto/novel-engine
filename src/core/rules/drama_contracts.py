"""Outline 与 ChapterCard typed drama source 的零 Provider 对账。"""

from __future__ import annotations

import json
from pathlib import Path

from pydantic import ValidationError

from src.core.models.chapter_card import ChapterCard
from src.core.models.core_state import CORE_STATE_RELATION_ATTRIBUTES
from src.core.models.drama import (
    DramaticSourceBinding,
    build_dramatic_source_binding,
)
from src.core.models.outline import (
    CharactersOutput,
    MiniPlanItem,
    MiniPlanOutput,
    PlotOutput,
    VolumePlanItem,
    VolumePlansOutput,
)
from src.core.models.model_calibration import canonical_genre_identity
from src.core.rules.outline_contracts import (
    build_volume_plans_contract,
    format_volume_plans_contract_failures_for_retry,
    validate_volume_plans_contract,
)


class DramaticSourceContractError(ValueError):
    """上游或章节卡戏剧源缺失、漂移或传播不完整。"""


def outline_has_typed_drama(
    *,
    volume_plans: VolumePlansOutput,
    mini_plan: MiniPlanOutput | None = None,
) -> bool:
    """判断 outline 是否已选择 A1 typed contract，而不读取任何自然文本。"""

    if any(
        volume.dramatic_promise is not None
        or any(
            beat.dramatic_contract is not None
            for arc in volume.arcs
            for beat in arc.beats
        )
        for volume in volume_plans.volumes
    ):
        return True
    return mini_plan is not None and any(
        item.dramatic_contract is not None for item in mini_plan.items
    )


def raw_outline_declares_drama_schema(
    *,
    volume_plans: object,
    mini_plan: object,
) -> bool:
    """只查 JSON key，区分显式 A1 schema 与缺键 legacy；不读自然文本。"""

    if isinstance(volume_plans, dict):
        volumes = volume_plans.get("volumes")
        if isinstance(volumes, list):
            for volume in volumes:
                if not isinstance(volume, dict):
                    continue
                if "dramatic_promise" in volume:
                    return True
                arcs = volume.get("arcs")
                if not isinstance(arcs, list):
                    continue
                for arc in arcs:
                    if not isinstance(arc, dict):
                        continue
                    beats = arc.get("beats")
                    if isinstance(beats, list) and any(
                        isinstance(beat, dict) and "dramatic_contract" in beat
                        for beat in beats
                    ):
                        return True
    if isinstance(mini_plan, dict):
        items = mini_plan.get("items")
        if isinstance(items, list) and any(
            isinstance(item, dict) and "dramatic_contract" in item for item in items
        ):
            return True
    return False


def raw_outline_has_typed_drama(
    *,
    volume_plans: object,
    mini_plan: object,
) -> bool:
    """识别任一非空 typed 值；只检查 JSON 结构，不解析自然文本。"""

    if isinstance(volume_plans, dict):
        volumes = volume_plans.get("volumes")
        if isinstance(volumes, list):
            for volume in volumes:
                if not isinstance(volume, dict):
                    continue
                if volume.get("dramatic_promise") is not None:
                    return True
                arcs = volume.get("arcs")
                if not isinstance(arcs, list):
                    continue
                for arc in arcs:
                    if not isinstance(arc, dict):
                        continue
                    beats = arc.get("beats")
                    if isinstance(beats, list) and any(
                        isinstance(beat, dict)
                        and beat.get("dramatic_contract") is not None
                        for beat in beats
                    ):
                        return True
    if isinstance(mini_plan, dict):
        items = mini_plan.get("items")
        if isinstance(items, list) and any(
            isinstance(item, dict) and item.get("dramatic_contract") is not None
            for item in items
        ):
            return True
    return False


def drama_validation_required(
    *,
    volume_plans: VolumePlansOutput,
    mini_plan: MiniPlanOutput | None,
    raw_volume_plans: object,
    raw_mini_plan: object,
    genre: str,
) -> bool:
    """区分已启用 A1、新 mystery schema 与完全缺键的 legacy。"""

    return (
        outline_has_typed_drama(
            volume_plans=volume_plans,
            mini_plan=mini_plan,
        )
        or raw_outline_has_typed_drama(
            volume_plans=raw_volume_plans,
            mini_plan=raw_mini_plan,
        )
        or (
            canonical_genre_identity(genre) == "mystery"
            and raw_outline_declares_drama_schema(
                volume_plans=raw_volume_plans,
                mini_plan=raw_mini_plan,
            )
        )
    )


def validate_volume_mini_plan_dramatic_sources(
    *,
    items: list[MiniPlanItem],
    volume: VolumePlanItem,
    characters: CharactersOutput,
    plot: PlotOutput,
) -> None:
    """确保每个 typed beat 只由一章 mini-plan 原样承担。"""

    valid_character_ids = {
        character.character_id for character in characters.characters
    }
    valid_thread_ids = {thread.thread_id for thread in plot.long_threads}
    valid_turning_ids = {turning.turning_id for turning in plot.major_turning_points}
    dramatic_assignments: dict[str, int] = {}

    for item in items:
        if item.dramatic_contract is None:
            continue
        if item.volume_id != volume.volume_id:
            raise DramaticSourceContractError(
                "mini-plan dramatic_contract 指向错误 volume_id"
            )
        owning_arc = next(
            (arc for arc in volume.arcs if arc.arc_id == item.arc_id),
            None,
        )
        owning_beat = (
            next(
                (
                    beat
                    for beat in owning_arc.beats
                    if beat.chapter_range[0]
                    <= item.chapter_number
                    <= beat.chapter_range[1]
                ),
                None,
            )
            if owning_arc is not None
            else None
        )
        if owning_beat is None or owning_beat.dramatic_contract is None:
            raise DramaticSourceContractError(
                "mini-plan dramatic_contract 没有同章所属 beat typed source"
            )
        if item.dramatic_contract != owning_beat.dramatic_contract:
            raise DramaticSourceContractError(
                "mini-plan dramatic_contract 必须与所属 beat 逐字段一致"
            )

        drama = item.dramatic_contract
        unknown_characters = [
            character_id
            for character_id in (drama.actor_id, drama.counterforce_id)
            if character_id not in valid_character_ids
        ]
        if unknown_characters:
            raise DramaticSourceContractError(
                "mini-plan dramatic_contract 引用未知 actor/counterforce:"
                f"{unknown_characters}"
            )
        missing_characters = [
            character_id
            for character_id in (drama.actor_id, drama.counterforce_id)
            if character_id not in item.linked_characters
        ]
        if missing_characters:
            raise DramaticSourceContractError(
                "mini-plan dramatic_contract 的 actor_id/counterforce_id 必须进入 "
                f"linked_characters，缺少 {missing_characters}; "
                f"chapter={item.chapter_id}, beat={owning_beat.beat_id}, actual={item.linked_characters}"
            )
        if drama.thread_id is not None:
            if drama.thread_id not in valid_thread_ids:
                raise DramaticSourceContractError(
                    f"mini-plan dramatic_contract.thread_id 未知:{drama.thread_id}"
                )
            if drama.thread_id not in item.linked_threads:
                raise DramaticSourceContractError(
                    "mini-plan dramatic_contract.thread_id 必须进入 linked_threads; "
                    f"chapter={item.chapter_id}, beat={owning_beat.beat_id}, "
                    f"required={drama.thread_id}, actual={item.linked_threads}"
                )
        if drama.turning_id is not None and drama.turning_id not in valid_turning_ids:
            raise DramaticSourceContractError(
                f"mini-plan dramatic_contract.turning_id 未知:{drama.turning_id}"
            )
        if (
            drama.function == "finale"
            and item.chapter_number != owning_beat.chapter_range[1]
        ):
            raise DramaticSourceContractError(
                "function=finale 的 mini-plan 必须落在所属 beat 最后一章"
            )
        dramatic_assignments[owning_beat.beat_id] = (
            dramatic_assignments.get(owning_beat.beat_id, 0) + 1
        )

    for arc in volume.arcs:
        for beat in arc.beats:
            if beat.dramatic_contract is None:
                continue
            assigned = dramatic_assignments.get(beat.beat_id, 0)
            if assigned != 1:
                raise DramaticSourceContractError(
                    f"beat={beat.beat_id} 的 dramatic_contract 必须由恰好一章 "
                    f"mini-plan 承担，实际 {assigned}"
                )


def validate_outline_dramatic_sources(
    *,
    characters: CharactersOutput,
    plot: PlotOutput,
    volume_plans: VolumePlansOutput,
    mini_plan: MiniPlanOutput,
    genre: str,
) -> None:
    """对生成、导入与 commit 共用的完整 A1 source chain 做确定性校验。"""

    valid_turning_ids = {
        turning.turning_id for turning in plot.major_turning_points
    }
    valid_thread_ids = {thread.thread_id for thread in plot.long_threads}
    reserved_ids = {
        *(character.character_id for character in characters.characters),
        *(phase.phase_id for phase in plot.main_phases),
        *valid_turning_ids,
        *valid_thread_ids,
    }
    for item in plot.load_bearing_objects:
        if item.object_id in reserved_ids:
            raise DramaticSourceContractError(
                f"load_bearing_object.object_id 与既有稳定 ID 冲突:{item.object_id}"
            )
        if (
            item.linked_turning_id is not None
            and item.linked_turning_id not in valid_turning_ids
        ):
            raise DramaticSourceContractError(
                "load_bearing_object 引用未知 linked_turning_id:"
                f"{item.object_id}/{item.linked_turning_id}"
            )
        if (
            item.linked_thread_id is not None
            and item.linked_thread_id not in valid_thread_ids
        ):
            raise DramaticSourceContractError(
                "load_bearing_object 引用未知 linked_thread_id:"
                f"{item.object_id}/{item.linked_thread_id}"
            )

    if (
        not outline_has_typed_drama(
            volume_plans=volume_plans,
            mini_plan=mini_plan,
        )
        and canonical_genre_identity(genre) != "mystery"
    ):
        return

    chapters_per_volume = volume_plans.volumes[0].chapter_count
    contract = build_volume_plans_contract(
        characters=characters,
        plot=plot,
        total_volumes=len(volume_plans.volumes),
        chapters_per_volume=chapters_per_volume,
        genre=genre,
    )
    drama_failures = [
        failure
        for failure in validate_volume_plans_contract(volume_plans, contract)
        if failure.rule.startswith("dramatic_")
    ]
    if drama_failures:
        raise DramaticSourceContractError(
            format_volume_plans_contract_failures_for_retry(drama_failures)
        )

    volume_ids = {volume.volume_id for volume in volume_plans.volumes}
    unknown_volume_items = [
        item.chapter_id
        for item in mini_plan.items
        if item.dramatic_contract is not None and item.volume_id not in volume_ids
    ]
    if unknown_volume_items:
        raise DramaticSourceContractError(
            f"mini-plan typed drama 引用未知 volume_id，章节:{unknown_volume_items}"
        )

    for volume in volume_plans.volumes:
        validate_volume_mini_plan_dramatic_sources(
            items=[
                item for item in mini_plan.items if item.volume_id == volume.volume_id
            ],
            volume=volume,
            characters=characters,
            plot=plot,
        )


def validate_card_dramatic_source(
    *,
    card: ChapterCard,
    volume_plans: VolumePlansOutput,
    mini_plan: MiniPlanOutput,
) -> DramaticSourceBinding | None:
    """在审批/Writer 前把 Card 与当前 volume/beat/mini-plan 逐字段对账。"""

    has_relation_transition = any(
        transition.attribute in CORE_STATE_RELATION_ATTRIBUTES
        for transition in card.core_state_transitions
    )
    volume = next(
        (item for item in volume_plans.volumes if item.volume_id == card.volume_id),
        None,
    )
    mini = next(
        (item for item in mini_plan.items if item.chapter_id == card.chapter_id),
        None,
    )
    if volume is None or mini is None:
        if card.dramatic_source is not None or has_relation_transition:
            raise DramaticSourceContractError(
                "ChapterCard 有 dramatic_source/relation transition，"
                "但当前 volume/mini-plan typed source 缺失"
            )
        return None
    if mini.volume_id != card.volume_id or mini.chapter_number != card.chapter_number:
        raise DramaticSourceContractError("ChapterCard 与 mini-plan 章节身份漂移")

    if mini.dramatic_contract is None:
        if card.dramatic_source is not None or has_relation_transition:
            raise DramaticSourceContractError(
                "ChapterCard dramatic_source/relation transition "
                "与当前普通/legacy mini-plan 漂移"
            )
        return None

    arc = next((item for item in volume.arcs if item.arc_id == mini.arc_id), None)
    beat = (
        next(
            (
                item
                for item in arc.beats
                if item.chapter_range[0] <= mini.chapter_number <= item.chapter_range[1]
            ),
            None,
        )
        if arc is not None
        else None
    )
    if (
        volume.dramatic_promise is None
        or beat is None
        or beat.dramatic_contract is None
    ):
        raise DramaticSourceContractError(
            "mini-plan dramatic_contract 缺少同源 volume promise 或 beat contract"
        )
    try:
        expected = build_dramatic_source_binding(
            volume_id=volume.volume_id,
            beat_id=beat.beat_id,
            promise=volume.dramatic_promise,
            beat_contract=beat.dramatic_contract,
            mini_plan_contract=mini.dramatic_contract,
        )
    except ValueError as exc:
        raise DramaticSourceContractError(
            f"volume/beat/mini-plan dramatic source 不同源:{exc}"
        ) from exc
    if card.dramatic_source != expected:
        raise DramaticSourceContractError(
            "ChapterCard dramatic_source 与当前 outline typed source 漂移"
        )

    missing_requirements = [
        text
        for text in expected.beat_contract.natural_requirements()
        if text not in card.must_achieve
    ]
    if missing_requirements:
        raise DramaticSourceContractError(
            f"ChapterCard.must_achieve 缺少 typed 戏剧自然要求:{missing_requirements}"
        )
    missing_characters = [
        character_id
        for character_id in (
            expected.beat_contract.actor_id,
            expected.beat_contract.counterforce_id,
        )
        if character_id not in card.related_characters
    ]
    if missing_characters:
        raise DramaticSourceContractError(
            f"ChapterCard.related_characters 缺少 actor/counterforce:{missing_characters}"
        )
    thread_id = expected.beat_contract.thread_id
    if thread_id is not None and thread_id not in card.related_plot_threads:
        raise DramaticSourceContractError(
            f"ChapterCard.related_plot_threads 缺少 dramatic thread_id:{thread_id}"
        )
    object_catalog = {
        item.object_id: item for item in expected.beat_contract.load_bearing_objects
    }
    transitioned_relation_object_ids: set[str] = set()
    for transition in card.core_state_transitions:
        if transition.attribute not in CORE_STATE_RELATION_ATTRIBUTES:
            continue
        if transition.entity_type != "object":
            raise DramaticSourceContractError(
                "possession/control/access 只允许 object relation transition"
            )
        object_contract = object_catalog.get(transition.entity_id)
        if object_contract is None:
            raise DramaticSourceContractError(
                "ChapterCard relation transition 引用未授权承重物件 "
                f"target={transition.entity_id}"
            )
        if transition.attribute not in object_contract.relation_attributes:
            raise DramaticSourceContractError(
                "ChapterCard relation attribute 未由承重物件合同授权:"
                f"{transition.entity_id}/{transition.attribute}"
            )
        transitioned_relation_object_ids.add(transition.entity_id)
        if (
            transition.to_value is not None
            and transition.to_value not in card.related_characters
        ):
            raise DramaticSourceContractError(
                "ChapterCard relation target principal 必须是 "
                f"related_characters 稳定 ID:{transition.to_value}"
            )
    if card.core_state_contract_version == 1:
        missing_relation_objects = sorted(
            set(object_catalog) - transitioned_relation_object_ids
        )
        if missing_relation_objects:
            raise DramaticSourceContractError(
                "ChapterCard 缺少承重物件在本 beat 必须冻结的 "
                "possession/control/access transition:"
                f"{missing_relation_objects}"
            )
    return expected


def load_and_validate_card_dramatic_source(
    *,
    card: ChapterCard,
    outline_dir: Path,
    genre: str = "",
) -> DramaticSourceBinding | None:
    """从当前 outline SSOT 加载并执行同一份 Card source 对账。"""

    volume_path = outline_dir / "volume_plans.json"
    mini_path = outline_dir / "mini_plan.json"
    if not volume_path.is_file() or not mini_path.is_file():
        if card.dramatic_source is not None:
            raise DramaticSourceContractError(
                "ChapterCard 有 dramatic_source，但当前 outline source 文件缺失"
            )
        return None
    try:
        raw_volume_plans = json.loads(volume_path.read_text(encoding="utf-8"))
        raw_mini_plan = json.loads(mini_path.read_text(encoding="utf-8"))
        volume_plans = VolumePlansOutput.model_validate(raw_volume_plans)
        mini_plan = MiniPlanOutput.model_validate(raw_mini_plan)
    except (OSError, json.JSONDecodeError, ValidationError) as exc:
        raise DramaticSourceContractError(
            f"无法读取当前 outline dramatic source:{exc}"
        ) from exc
    if drama_validation_required(
        volume_plans=volume_plans,
        mini_plan=mini_plan,
        raw_volume_plans=raw_volume_plans,
        raw_mini_plan=raw_mini_plan,
        genre=genre,
    ):
        characters_path = outline_dir / "characters.json"
        plot_path = outline_dir / "plot.json"
        try:
            characters = CharactersOutput.model_validate(
                json.loads(characters_path.read_text(encoding="utf-8"))
            )
            plot = PlotOutput.model_validate(
                json.loads(plot_path.read_text(encoding="utf-8"))
            )
        except (OSError, json.JSONDecodeError, ValidationError) as exc:
            raise DramaticSourceContractError(
                f"无法读取当前 outline dramatic identity source:{exc}"
            ) from exc
        validate_outline_dramatic_sources(
            characters=characters,
            plot=plot,
            volume_plans=volume_plans,
            mini_plan=mini_plan,
            genre=genre,
        )
    return validate_card_dramatic_source(
        card=card,
        volume_plans=volume_plans,
        mini_plan=mini_plan,
    )


__all__ = [
    "DramaticSourceContractError",
    "drama_validation_required",
    "load_and_validate_card_dramatic_source",
    "outline_has_typed_drama",
    "raw_outline_declares_drama_schema",
    "raw_outline_has_typed_drama",
    "validate_card_dramatic_source",
    "validate_outline_dramatic_sources",
    "validate_volume_mini_plan_dramatic_sources",
]
