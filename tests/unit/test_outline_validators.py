"""outline 级 [HARD] 校验单测(P5-B-04)。

覆盖 ``src/core/rules/outline_validators.py``:

- ``validate_arc_chapter_spans``:扫 ``volume_plans[*].arcs[*]`` 副本类
  段长 < 3 时返违规;= 3 / > 3 不返违规;arc_type 不含"副本"不返违规
  (即便段长 < 3);多卷多 arc 累计违规列表正确。
- ``OutlineValidationFailure`` Pydantic v2 model 字段约束。

对齐 PROJECT_SPEC §4.1 + p5_plan §8。
"""

from __future__ import annotations

import pytest

from src.core.models.outline import (
    VolumeArcPlan,
    VolumePlanItem,
    VolumePlansOutput,
)
from src.core.rules.outline_validators import (
    OutlineValidationFailure,
    validate_arc_chapter_spans,
)


def _make_volume(
    *,
    volume_id: str = "v1",
    chapter_count: int = 6,
    arcs: list[dict] | None = None,
    tension_curve: list[int] | None = None,
) -> VolumePlanItem:
    """构造 VolumePlanItem,arcs 默认 3 段连续不重叠,curve 长度对齐。"""
    if arcs is None:
        arcs = [
            {
                "arc_id": f"{volume_id}-arc-1",
                "arc_type": "主线",
                "chapter_range": [1, 2],
                "phase": "opening",
                "summary": "opening",
            },
            {
                "arc_id": f"{volume_id}-arc-2",
                "arc_type": "副本",
                "chapter_range": [3, 4],  # 段长 2 = 副本违规
                "phase": "rising",
                "summary": "副本段",
            },
            {
                "arc_id": f"{volume_id}-arc-3",
                "arc_type": "主线",
                "chapter_range": [5, chapter_count],
                "phase": "climax",
                "summary": "climax",
            },
        ]
    if tension_curve is None:
        tension_curve = [5] * chapter_count
    return VolumePlanItem(
        volume_id=volume_id,
        title=f"{volume_id} title",
        summary=f"{volume_id} summary",
        chapter_count=chapter_count,
        tension_template="heros_journey",
        arcs=[VolumeArcPlan.model_validate(a) for a in arcs],
        tension_curve=tension_curve,
    )


class TestArcTooShortRule:
    def test_副本_2_chars_returns_one_failure(self) -> None:
        # 默认 6 章 + 中段 [3,4] 副本 = 段长 2 → 1 条违规
        vol = _make_volume()
        failures = validate_arc_chapter_spans([vol])
        assert len(failures) == 1
        assert failures[0].rule == "arc_too_short"
        assert failures[0].volume_id == "v1"
        assert failures[0].arc_id == "v1-arc-2"
        assert failures[0].arc_type == "副本"
        assert failures[0].chapter_range == (3, 4)
        assert failures[0].span_length == 2

    def test_副本_3_chars_passes(self) -> None:
        # 副本段长 3 → 不违规
        vol = _make_volume(
            chapter_count=9,
            arcs=[
                {
                    "arc_id": "v1-arc-1",
                    "arc_type": "主线",
                    "chapter_range": [1, 3],
                    "phase": "opening",
                    "summary": "x",
                },
                {
                    "arc_id": "v1-arc-2",
                    "arc_type": "副本",
                    "chapter_range": [4, 6],  # 段长 3 ✅
                    "phase": "rising",
                    "summary": "x",
                },
                {
                    "arc_id": "v1-arc-3",
                    "arc_type": "主线",
                    "chapter_range": [7, 9],
                    "phase": "climax",
                    "summary": "x",
                },
            ],
            tension_curve=[5] * 9,
        )
        failures = validate_arc_chapter_spans([vol])
        assert failures == []

    def test_副本_5_chars_passes(self) -> None:
        vol = _make_volume(
            chapter_count=10,
            arcs=[
                {
                    "arc_id": "v1-arc-1",
                    "arc_type": "主线",
                    "chapter_range": [1, 3],
                    "phase": "opening",
                    "summary": "x",
                },
                {
                    "arc_id": "v1-arc-2",
                    "arc_type": "副本",
                    "chapter_range": [4, 8],  # 段长 5 ✅
                    "phase": "rising",
                    "summary": "x",
                },
                {
                    "arc_id": "v1-arc-3",
                    "arc_type": "主线",
                    "chapter_range": [9, 10],
                    "phase": "climax",
                    "summary": "x",
                },
            ],
            tension_curve=[5] * 10,
        )
        failures = validate_arc_chapter_spans([vol])
        assert failures == []

    def test_main_arc_2_chars_passes(self) -> None:
        """主线段长 2 — 不触发副本规则。"""
        vol = _make_volume(
            chapter_count=6,
            arcs=[
                {
                    "arc_id": "v1-arc-1",
                    "arc_type": "主线",  # 主线不受规则约束
                    "chapter_range": [1, 2],
                    "phase": "opening",
                    "summary": "x",
                },
                {
                    "arc_id": "v1-arc-2",
                    "arc_type": "主线",
                    "chapter_range": [3, 4],
                    "phase": "rising",
                    "summary": "x",
                },
                {
                    "arc_id": "v1-arc-3",
                    "arc_type": "主线",
                    "chapter_range": [5, 6],
                    "phase": "climax",
                    "summary": "x",
                },
            ],
            tension_curve=[5] * 6,
        )
        failures = validate_arc_chapter_spans([vol])
        assert failures == []

    def test_substring_副本_in_arc_type(self) -> None:
        """arc_type 含"副本"子串(例如"幻境副本") — 仍命中规则。"""
        vol = _make_volume(
            chapter_count=6,
            arcs=[
                {
                    "arc_id": "v1-arc-1",
                    "arc_type": "主线",
                    "chapter_range": [1, 2],
                    "phase": "opening",
                    "summary": "x",
                },
                {
                    "arc_id": "v1-arc-2",
                    "arc_type": "幻境副本",  # 含"副本"子串
                    "chapter_range": [3, 4],
                    "phase": "rising",
                    "summary": "x",
                },
                {
                    "arc_id": "v1-arc-3",
                    "arc_type": "主线",
                    "chapter_range": [5, 6],
                    "phase": "climax",
                    "summary": "x",
                },
            ],
            tension_curve=[5] * 6,
        )
        failures = validate_arc_chapter_spans([vol])
        assert len(failures) == 1
        assert failures[0].arc_type == "幻境副本"

    def test_multiple_volumes_accumulate(self) -> None:
        v1 = _make_volume(volume_id="v1")  # 1 违规
        v2 = _make_volume(volume_id="v2")  # 1 违规
        failures = validate_arc_chapter_spans([v1, v2])
        assert len(failures) == 2
        assert {f.volume_id for f in failures} == {"v1", "v2"}

    def test_empty_volumes_list(self) -> None:
        assert validate_arc_chapter_spans([]) == []

    def test_volume_plans_output_integration(self) -> None:
        """通过 VolumePlansOutput 接入(集成 - 包外层 model wrap)。"""
        plans = VolumePlansOutput(volumes=[_make_volume(volume_id="v1")])
        failures = validate_arc_chapter_spans(plans.volumes)
        assert len(failures) == 1


class TestOutlineValidationFailureModel:
    def test_required_fields(self) -> None:
        failure = OutlineValidationFailure(
            rule="arc_too_short",
            volume_id="v1",
            arc_id="v1-arc-2",
            arc_type="副本",
            chapter_range=(3, 4),
            span_length=2,
        )
        assert failure.rule == "arc_too_short"
        assert failure.span_length == 2

    def test_span_length_must_be_positive(self) -> None:
        with pytest.raises(Exception):
            OutlineValidationFailure(
                rule="arc_too_short",
                volume_id="v1",
                arc_id="x",
                arc_type="副本",
                chapter_range=(1, 1),
                span_length=0,
            )

    def test_model_dump_json_serializable(self) -> None:
        failure = OutlineValidationFailure(
            rule="arc_too_short",
            volume_id="v1",
            arc_id="x",
            arc_type="副本",
            chapter_range=(3, 4),
            span_length=2,
        )
        dumped = failure.model_dump(mode="json")
        assert dumped["chapter_range"] == [3, 4]
        assert dumped["span_length"] == 2
