"""G2(v1.1 wave 4 session 2):mini_plan ``callback_anchors`` 校验单测。

``_validate_mini_plan_volume_refs`` 对 ``callback_anchors`` 做与 ``linked_threads``
同型引用完整性校验(必须命中 outline ``plot.long_threads`` 内 thread_id)。
"""

from __future__ import annotations

import pytest

from src.core.agents.outline_builder import (
    OutlineBuilderError,
    _validate_mini_plan_volume_refs,
)
from src.core.models.outline import (
    CharacterOutlineItem,
    CharactersOutput,
    MiniPlanItem,
    PlotLongThread,
    PlotOutput,
    PlotPhase,
    PlotTurningPoint,
    VolumeArcPlan,
    VolumeBeatPlan,
    VolumePlanItem,
)


def _make_characters() -> CharactersOutput:
    # CharactersOutput 要求 4-8 个角色(min_length=4)。
    def _c(cid: str, role: str) -> CharacterOutlineItem:
        return CharacterOutlineItem(
            character_id=cid,
            name=cid,
            role=role,
            archetype="原型",
            appearance="外貌",
            personality="性格",
            background="背景",
            motivation="动机",
            voice_hint="简洁",
        )

    return CharactersOutput(
        characters=[
            _c("char-hero", "protagonist"),
            _c("char-second", "deuteragonist"),
            _c("char-villain", "antagonist"),
            _c("char-support", "supporting"),
        ]
    )


def _make_plot() -> PlotOutput:
    return PlotOutput(
        core_conflict="正邪对抗",
        main_phases=[
            PlotPhase(phase_id="phase-1", title="开端", summary="觉醒"),
            PlotPhase(phase_id="phase-2", title="发展", summary="历练"),
            PlotPhase(phase_id="phase-3", title="高潮", summary="决战"),
            PlotPhase(phase_id="phase-4", title="结局", summary="新生"),
        ],
        major_turning_points=[
            PlotTurningPoint(turning_id="tp-1", title="转折一", description="入门"),
            PlotTurningPoint(turning_id="tp-2", title="转折二", description="背叛"),
            PlotTurningPoint(turning_id="tp-3", title="转折三", description="觉悟"),
        ],
        long_threads=[
            PlotLongThread(thread_id="thread-1", title="身世", description="身世之谜"),
            PlotLongThread(thread_id="thread-2", title="宝物", description="神剑下落"),
            PlotLongThread(thread_id="thread-3", title="宿敌", description="宿敌身份"),
        ],
        character_arcs={"char-hero": "从少年到大侠"},
    )


def _make_volume() -> VolumePlanItem:
    return VolumePlanItem(
        volume_id="v1",
        title="第一卷",
        summary="开篇",
        chapter_count=4,
        tension_template="heros_journey",
        linked_threads=["thread-1"],
        arcs=[
            VolumeArcPlan(
                arc_id="v1-arc-1",
                arc_type="主线",
                chapter_range=(1, 2),
                phase="opening",
                summary="开端",
                beats=[
                    VolumeBeatPlan(
                        beat_id="v1-arc-1-beat-1",
                        chapter_range=(1, 2),
                        summary="起始 beat",
                        must_advance=["主角登场"],
                        forbidden_advance=["反派死亡"],
                    ),
                ],
            ),
            VolumeArcPlan(
                arc_id="v1-arc-2",
                arc_type="主线",
                chapter_range=(3, 4),
                phase="rising",
                summary="发展",
                beats=[
                    VolumeBeatPlan(
                        beat_id="v1-arc-2-beat-1",
                        chapter_range=(3, 4),
                        summary="推进 beat",
                        must_advance=["历练"],
                        forbidden_advance=["决战"],
                    ),
                ],
            ),
        ],
        tension_curve=[3, 5, 6, 7],
    )


def _make_chapter_id_map() -> dict[tuple[str, int], str]:
    return {
        ("v1", 1): "ch-0001",
        ("v1", 2): "ch-0002",
        ("v1", 3): "ch-0003",
        ("v1", 4): "ch-0004",
    }


def _make_item(*, callback_anchors: list[str]) -> MiniPlanItem:
    return MiniPlanItem(
        chapter_id="ch-0001",
        chapter_number=1,
        volume_id="v1",
        arc_id="v1-arc-1",
        one_line_event="主角登场,初遇神剑。",
        linked_threads=["thread-1"],
        callback_anchors=callback_anchors,
    )


def _validate(item: MiniPlanItem) -> None:
    _validate_mini_plan_volume_refs(
        items=[item],
        volume=_make_volume(),
        characters=_make_characters(),
        plot=_make_plot(),
        chapter_id_map=_make_chapter_id_map(),
    )


def test_callback_anchors_unknown_id_raises() -> None:
    with pytest.raises(OutlineBuilderError, match="callback_anchors"):
        _validate(_make_item(callback_anchors=["thread-DOES-NOT-EXIST"]))


def test_callback_anchors_valid_passes() -> None:
    _validate(_make_item(callback_anchors=["thread-2"]))


def test_callback_anchors_empty_passes() -> None:
    _validate(_make_item(callback_anchors=[]))


def test_miniplan_item_callback_anchors_defaults_empty() -> None:
    item = MiniPlanItem(
        chapter_id="ch-0001",
        chapter_number=1,
        volume_id="v1",
        arc_id="v1-arc-1",
        one_line_event="主角登场。",
    )
    assert item.callback_anchors == []
