"""验证 P5-B-01 Volume / Arc / TensionCurvePoint Pydantic 模型 + 4 张力模板。

覆盖维度:

- Volume / Arc / TensionCurvePoint 基本构造 + 默认值
- ``tension_template`` Literal 4 档枚举允许 / 非档抛 ``ValidationError``
- ``Arc.chapter_range`` 单调:``start < 1`` / ``end < start`` 抛
- ``Volume._validate_tension_curve_length``:非空时长度 == chapter_count
- ``Volume._validate_arcs_within_chapter_count``:Arc end > chapter_count 抛
- ``Volume._validate_unique_arc_ids``:重复 arc_id 抛
- Pydantic ``model_dump_json`` ↔ ``model_validate_json`` round-trip 无损
  (arcs / tension_curve 嵌套字段 JSON 序列化对齐 ORM JSON 列存储语义)
- ``apply_template`` 4 模板均能产出 ``chapter_count`` 长度的张力序列;
  每项 1-10 整数;``chapter_count == 1`` 返中点;非档模板名抛 ValueError
"""

from __future__ import annotations

import datetime as _dt
import json

import pytest
from pydantic import ValidationError

from src.core.models.tension_curve import (
    TENSION_TEMPLATES,
    apply_template,
)
from src.core.models.volume import (
    TENSION_TEMPLATE_VALUES,
    Arc,
    TensionCurvePoint,
    Volume,
)


def _now_utc() -> _dt.datetime:
    return _dt.datetime(2026, 5, 12, 10, 0, 0, tzinfo=_dt.timezone.utc)


def _minimal_volume(
    *,
    chapter_count: int = 30,
    template: str = "heros_journey",
    arcs: list[Arc] | None = None,
    tension_curve: list[TensionCurvePoint] | None = None,
) -> Volume:
    return Volume(
        volume_id="vol-001",
        workspace_id="ws-test",
        title="第一卷:启程",
        summary="主角踏上修炼之路",
        chapter_count=chapter_count,
        tension_template=template,  # type: ignore[arg-type]
        arcs=arcs or [],
        tension_curve=tension_curve or [],
        created_at=_now_utc(),
        last_updated_at=_now_utc(),
    )


class TestTensionCurvePoint:
    def test_valid_point(self) -> None:
        p = TensionCurvePoint(chapter_number=1, tension=5, is_arc_finale=False)
        assert p.chapter_number == 1
        assert p.tension == 5
        assert p.is_arc_finale is False

    @pytest.mark.parametrize("tension", [0, 11, -1, 100])
    def test_tension_out_of_range(self, tension: int) -> None:
        with pytest.raises(ValidationError):
            TensionCurvePoint(chapter_number=1, tension=tension)

    def test_chapter_number_must_be_positive(self) -> None:
        with pytest.raises(ValidationError):
            TensionCurvePoint(chapter_number=0, tension=5)


class TestArc:
    def test_valid_arc(self) -> None:
        arc = Arc(
            arc_id="arc-001",
            arc_type="主线",
            chapter_range=(1, 5),
            phase="opening",
            summary="主角起步",
        )
        assert arc.arc_id == "arc-001"
        assert arc.chapter_range == (1, 5)
        assert arc.phase == "opening"

    def test_chapter_range_end_less_than_start_raises(self) -> None:
        with pytest.raises(ValidationError):
            Arc(
                arc_id="arc-001",
                arc_type="主线",
                chapter_range=(5, 3),
                phase="opening",
                summary="bad range",
            )

    def test_chapter_range_start_lt_one_raises(self) -> None:
        with pytest.raises(ValidationError):
            Arc(
                arc_id="arc-001",
                arc_type="主线",
                chapter_range=(0, 5),
                phase="opening",
                summary="bad start",
            )

    @pytest.mark.parametrize(
        "phase",
        ["opening", "rising", "climax", "falling", "resolution"],
    )
    def test_phase_5_values_allowed(self, phase: str) -> None:
        arc = Arc(
            arc_id="arc-001",
            arc_type="主线",
            chapter_range=(1, 3),
            phase=phase,  # type: ignore[arg-type]
            summary="x",
        )
        assert arc.phase == phase

    def test_phase_outside_5_values_raises(self) -> None:
        with pytest.raises(ValidationError):
            Arc(
                arc_id="arc-001",
                arc_type="主线",
                chapter_range=(1, 3),
                phase="middle",  # type: ignore[arg-type]
                summary="x",
            )

    def test_arc_id_pattern_enforced(self) -> None:
        with pytest.raises(ValidationError):
            Arc(
                arc_id="arc 001",  # 含空格,违反 ID_PATTERN
                arc_type="主线",
                chapter_range=(1, 3),
                phase="opening",
                summary="x",
            )


class TestVolumeBasic:
    def test_minimal_volume_constructs(self) -> None:
        v = _minimal_volume()
        assert v.volume_id == "vol-001"
        assert v.workspace_id == "ws-test"
        assert v.chapter_count == 30
        assert v.tension_template == "heros_journey"
        assert v.arcs == []
        assert v.tension_curve == []

    def test_reserved_qwen38_selection_workspace_id_constructs(self) -> None:
        workspace_id = "post-v8-abc-qwen3.8-27b-selection-v1"

        volume = _minimal_volume().model_copy(update={"workspace_id": workspace_id})
        validated = Volume.model_validate(volume.model_dump())

        assert validated.workspace_id == workspace_id

    def test_empty_tension_curve_allowed(self) -> None:
        """初始 Volume 仅落 title / chapter_count,curve 待填充时为空允许。"""
        v = _minimal_volume(chapter_count=10)
        assert v.tension_curve == []

    def test_title_min_length_enforced(self) -> None:
        with pytest.raises(ValidationError):
            Volume(
                volume_id="vol-001",
                workspace_id="ws-test",
                title="",
                summary="x",
                chapter_count=10,
                tension_template="free",
                created_at=_now_utc(),
                last_updated_at=_now_utc(),
            )

    def test_volume_id_pattern_enforced(self) -> None:
        with pytest.raises(ValidationError):
            Volume(
                volume_id="vol 001",  # 含空格
                workspace_id="ws-test",
                title="t",
                summary="x",
                chapter_count=10,
                tension_template="free",
                created_at=_now_utc(),
                last_updated_at=_now_utc(),
            )


class TestVolumeTensionTemplate:
    @pytest.mark.parametrize("template", TENSION_TEMPLATE_VALUES)
    def test_each_allowed_template(self, template: str) -> None:
        v = _minimal_volume(template=template)
        assert v.tension_template == template

    @pytest.mark.parametrize(
        "bad", ["epic_quest", "slow_burn", "HEROS_JOURNEY", "", " free "]
    )
    def test_outside_4_values_raises(self, bad: str) -> None:
        """对齐 FRONTEND_SPEC §4.2 字面:大小写敏感 + 字面值精确匹配。"""
        with pytest.raises(ValidationError):
            _minimal_volume(template=bad)


class TestVolumeTensionCurveLength:
    def test_curve_length_eq_chapter_count_ok(self) -> None:
        curve = [
            TensionCurvePoint(chapter_number=i, tension=5)
            for i in range(1, 6)
        ]
        v = _minimal_volume(chapter_count=5, tension_curve=curve)
        assert len(v.tension_curve) == 5

    def test_curve_length_ne_chapter_count_raises(self) -> None:
        curve = [
            TensionCurvePoint(chapter_number=i, tension=5)
            for i in range(1, 4)
        ]
        with pytest.raises(ValidationError):
            _minimal_volume(chapter_count=5, tension_curve=curve)


class TestVolumeArcs:
    def test_arc_within_chapter_count_ok(self) -> None:
        arcs = [
            Arc(
                arc_id="arc-001",
                arc_type="主线",
                chapter_range=(1, 10),
                phase="opening",
                summary="a",
            ),
            Arc(
                arc_id="arc-002",
                arc_type="副本",
                chapter_range=(11, 15),
                phase="rising",
                summary="b",
            ),
        ]
        v = _minimal_volume(chapter_count=30, arcs=arcs)
        assert len(v.arcs) == 2

    def test_arc_end_exceeds_chapter_count_raises(self) -> None:
        arcs = [
            Arc(
                arc_id="arc-001",
                arc_type="主线",
                chapter_range=(1, 35),
                phase="opening",
                summary="a",
            ),
        ]
        with pytest.raises(ValidationError, match="chapter_range end"):
            _minimal_volume(chapter_count=30, arcs=arcs)

    def test_duplicate_arc_id_raises(self) -> None:
        arcs = [
            Arc(
                arc_id="arc-dup",
                arc_type="主线",
                chapter_range=(1, 5),
                phase="opening",
                summary="a",
            ),
            Arc(
                arc_id="arc-dup",
                arc_type="副本",
                chapter_range=(6, 10),
                phase="rising",
                summary="b",
            ),
        ]
        with pytest.raises(ValidationError, match="Duplicate Arc.arc_id"):
            _minimal_volume(chapter_count=30, arcs=arcs)


class TestVolumeJsonRoundTrip:
    """Pydantic ``model_dump_json`` ↔ ``model_validate_json`` 无损 round-trip。

    对齐 ORM ``arcs_json`` / ``tension_curve_json`` JSON 列存储语义:Pydantic
    层产 JSON 字符串落库,回读时反序列化为同结构 dict。
    """

    def test_full_volume_json_round_trip(self) -> None:
        arcs = [
            Arc(
                arc_id="arc-001",
                arc_type="主线",
                chapter_range=(1, 10),
                phase="opening",
                summary="主角起步",
            ),
            Arc(
                arc_id="arc-002",
                arc_type="副本",
                chapter_range=(11, 14),
                phase="rising",
                summary="第一个副本",
            ),
        ]
        curve = [
            TensionCurvePoint(
                chapter_number=i, tension=(i % 10) + 1, is_arc_finale=(i == 14)
            )
            for i in range(1, 15)
        ]
        original = _minimal_volume(chapter_count=14, arcs=arcs, tension_curve=curve)

        serialized = original.model_dump_json()
        # 二次反序列化等同
        restored = Volume.model_validate_json(serialized)

        assert restored.volume_id == original.volume_id
        assert restored.chapter_count == original.chapter_count
        assert len(restored.arcs) == 2
        assert restored.arcs[1].chapter_range == (11, 14)
        assert len(restored.tension_curve) == 14
        assert restored.tension_curve[13].is_arc_finale is True
        # 完整 JSON 字符串幂等(再 dump 还原)
        assert restored.model_dump_json() == serialized

    def test_arcs_json_dict_round_trip(self) -> None:
        """``model_dump(mode='json')`` 产 dict,等效 ORM ``arcs_json`` 列存储。

        模拟 ORM 写入路径:Pydantic → dict(JSON-safe)→ SQLAlchemy JSON 列。
        """
        arc = Arc(
            arc_id="arc-001",
            arc_type="主线",
            chapter_range=(1, 5),
            phase="opening",
            summary="a",
        )
        as_dict = arc.model_dump(mode="json")
        # tuple → list(JSON 没 tuple),其余字段字面保留
        assert as_dict["arc_id"] == "arc-001"
        assert as_dict["chapter_range"] == [1, 5]
        # JSON 字符串可还原
        s = json.dumps(as_dict)
        recovered = Arc.model_validate(json.loads(s))
        assert recovered.arc_id == arc.arc_id
        assert recovered.chapter_range == (1, 5)


class TestApplyTensionTemplate:
    @pytest.mark.parametrize("template", TENSION_TEMPLATE_VALUES)
    def test_template_returns_correct_length(self, template: str) -> None:
        for cc in [1, 5, 10, 30, 100]:
            series = apply_template(template, cc)  # type: ignore[arg-type]
            assert len(series) == cc
            assert all(1 <= t <= 10 for t in series)
            assert all(isinstance(t, int) for t in series)

    def test_chapter_count_one_returns_template_midpoint(self) -> None:
        for template in TENSION_TEMPLATE_VALUES:
            series = apply_template(template, 1)  # type: ignore[arg-type]
            samples = TENSION_TEMPLATES[template]  # type: ignore[index]
            assert series[0] == max(1, min(10, samples[len(samples) // 2]))

    def test_unknown_template_raises(self) -> None:
        with pytest.raises(ValueError, match="Unknown tension template"):
            apply_template("epic_quest", 10)  # type: ignore[arg-type]

    def test_chapter_count_zero_raises(self) -> None:
        with pytest.raises(ValueError, match="chapter_count must be >= 1"):
            apply_template("free", 0)

    def test_heros_journey_endpoints_align_with_template(self) -> None:
        """N=11 时端点应严格落在模板首末。"""
        series = apply_template("heros_journey", 11)
        samples = TENSION_TEMPLATES["heros_journey"]
        assert series[0] == samples[0]
        assert series[-1] == samples[-1]

    def test_free_template_returns_flat_five(self) -> None:
        series = apply_template("free", 30)
        assert all(t == 5 for t in series)

    def test_template_dict_has_4_entries(self) -> None:
        assert set(TENSION_TEMPLATES.keys()) == set(TENSION_TEMPLATE_VALUES)
