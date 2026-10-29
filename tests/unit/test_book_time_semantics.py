"""C2 四类书内时间语义的纯代码冻结 oracle。

测试只使用仓库内常量，不打开任何真实 workspace，也不调用 Provider。
"""

from __future__ import annotations

import hashlib
from pathlib import Path

import pytest

from src.core.models.core_state import (
    BookClockConstraint,
    BookClockInterval,
    BookTimeApproximateQuantity,
    BookTimeNode,
    BookTimeProjection,
    advance_book_time_projection,
    build_typed_book_clock_constraint,
    compute_core_state_projection_hash,
    make_core_state_projection,
    render_core_state_capsule,
)


FROZEN_V8_TIME_GAPS = (
    "开篇",
    "次日夜间",
    "同夜稍后，约零点前后",
    "次日清晨",
    "数日后夜间，转运窗口确定并执行",
    "约一小时后，车队抵达临时营地",
    "数小时后，黎明前",
    "当夜，车队抵达县城方向临时实验室后数小时",
    "显影结束后数小时，天将亮未亮",
    "同一天白天，显影结束后数小时至午后",
    "同一天白天，天光渐白之后数小时",
    "当夜",
)

_ROOT = Path(__file__).resolve().parents[2]
_PROMPT_ARCHIVE_HASHES = {
    "continuity_validator_v1_pre_c2_typed_book_time.md": (
        "42dfe619efaba86eda62522ad9fcd78d44806e5c62dfcffc7e63ee4ec70d52bc"
    ),
    "setting_consistency_validator_v2_pre_c2_typed_book_time.md": (
        "1967b4a17480430cde9c05e01f4fc97f5ae3976298b57ade7aaec98075376579"
    ),
}


def _build_chain() -> tuple[list[BookClockConstraint], BookTimeProjection]:
    previous: BookTimeProjection | None = None
    constraints: list[BookClockConstraint] = []
    projection_hash = "0" * 64
    for index, time_gap in enumerate(FROZEN_V8_TIME_GAPS, start=1):
        constraint = build_typed_book_clock_constraint(
            chapter_id=f"ch-{index:04d}",
            time_gap=time_gap,
            previous_projection=previous,
            previous_projection_hash=projection_hash,
            is_first_chapter=index == 1,
        )
        constraints.append(constraint)
        previous = advance_book_time_projection(
            previous_projection=previous,
            constraint=constraint,
        )
        projection_hash = compute_core_state_projection_hash(
            facts=(),
            book_clock=(
                previous.current_node.absolute_interval
                if previous.current_node is not None
                else None
            ),
            book_time=previous,
            book_time_contract_version=1,
        )
    assert previous is not None
    return constraints, previous


def test_frozen_v8_twelve_chapter_oracle_stays_typed_without_minute_invention() -> None:
    constraints, projection = _build_chain()

    assert [item.kind for item in constraints] == [
        "exact",
        "ordered_relative",
        "ordered_relative",
        "ordered_relative",
        "ordered_relative",
        "ordered_relative",
        "ordered_relative",
        "ordered_relative",
        "ordered_relative",
        "ordered_relative",
        "ordered_relative",
        "ordered_relative",
    ]
    assert constraints[0].current_interval == BookClockInterval(
        minimum_minutes=0,
        maximum_minutes=0,
    )
    assert all(item.gap_interval is None for item in constraints[1:])
    assert all(item.current_interval is None for item in constraints[1:])
    assert [(item.relations, item.day_phase) for item in constraints] == [
        ((), None),
        (("next_narrative_day",), "night"),
        (("after", "same_night"), "around_midnight"),
        (("next_narrative_day",), "morning"),
        (("after",), "night"),
        (("after",), None),
        (("after",), "pre_dawn"),
        (("after",), "night"),
        (("after",), "pre_dawn"),
        (("after", "same_narrative_day"), "afternoon"),
        (("after", "same_narrative_day"), None),
        (("after", "same_narrative_day"), "night"),
    ]
    assert constraints[5].approximate_quantity == BookTimeApproximateQuantity(
        value=1,
        unit="hour",
    )
    assert constraints[11].relations == ("after", "same_narrative_day")
    assert constraints[11].day_phase == "night"

    # ordered_relative 从不清空最近 absolute anchor，且每章仍有主线节点。
    assert projection.latest_absolute_anchor is not None
    assert projection.latest_absolute_anchor.interval == BookClockInterval(
        minimum_minutes=0,
        maximum_minutes=0,
    )
    assert projection.latest_mainline_node_id == constraints[-1].node_id
    assert len(projection.active_relation_chain) == 11


@pytest.mark.parametrize(
    ("time_gap", "kind", "gap"),
    [
        ("三小时后", "exact", BookClockInterval(minimum_minutes=180, maximum_minutes=180)),
        (
            "两到三天后",
            "bounded_interval",
            BookClockInterval(minimum_minutes=2880, maximum_minutes=4320),
        ),
        ("约一小时后", "ordered_relative", None),
        ("数日后", "ordered_relative", None),
        ("数小时后", "ordered_relative", None),
        ("下午三时", "exact", None),
        ("下午两到三时", "bounded_interval", None),
    ],
)
def test_only_explicit_numeric_facts_create_intervals(
    time_gap: str,
    kind: str,
    gap: BookClockInterval | None,
) -> None:
    opening = build_typed_book_clock_constraint(
        chapter_id="ch-0001",
        time_gap="开篇",
        previous_projection=None,
        previous_projection_hash="0" * 64,
        is_first_chapter=True,
    )
    projection = advance_book_time_projection(
        previous_projection=None,
        constraint=opening,
    )
    constraint = build_typed_book_clock_constraint(
        chapter_id="ch-0002",
        time_gap=time_gap,
        previous_projection=projection,
        previous_projection_hash="1" * 64,
        is_first_chapter=False,
    )
    assert constraint.kind == kind
    assert constraint.gap_interval == gap


def test_writer_capsule_keeps_explicit_duration_in_source_language_only() -> None:
    opening = build_typed_book_clock_constraint(
        chapter_id="ch-0001",
        time_gap="开篇",
        previous_projection=None,
        previous_projection_hash="0" * 64,
        is_first_chapter=True,
    )
    opening_time = advance_book_time_projection(
        previous_projection=None,
        constraint=opening,
    )
    projection = make_core_state_projection(
        book_clock=opening.current_interval,
        book_time=opening_time,
        book_time_contract_version=1,
    )
    exact = build_typed_book_clock_constraint(
        chapter_id="ch-0002",
        time_gap="三小时后",
        previous_projection=opening_time,
        previous_projection_hash=projection.projection_hash,
        is_first_chapter=False,
    )

    capsule = render_core_state_capsule(
        projection=projection,
        book_clock_constraint=exact,
    ).text

    assert "原始关系=三小时后" in capsule
    assert "晚于上一主线节点" in capsule
    assert "180分钟" not in capsule
    assert "exact" not in capsule
    assert "距开篇" not in capsule
    assert "时间戳" in capsule


def test_unanchored_keeps_anchor_and_resume_returns_to_frozen_mainline_node() -> None:
    opening = build_typed_book_clock_constraint(
        chapter_id="ch-0001",
        time_gap="开篇",
        previous_projection=None,
        previous_projection_hash="0" * 64,
        is_first_chapter=True,
    )
    mainline = advance_book_time_projection(
        previous_projection=None,
        constraint=opening,
    )
    dream = build_typed_book_clock_constraint(
        chapter_id="ch-0002",
        time_gap="梦中不知过了多久",
        previous_projection=mainline,
        previous_projection_hash="1" * 64,
        is_first_chapter=False,
    )
    assert dream.kind == "genuinely_unanchored"
    assert dream.unanchored_reason == "dream"
    assert dream.is_hard is False
    unanchored = advance_book_time_projection(
        previous_projection=mainline,
        constraint=dream,
    )
    assert unanchored.latest_mainline_node_id == opening.node_id
    assert unanchored.latest_absolute_anchor == mainline.latest_absolute_anchor
    assert unanchored.active_relation_chain == mainline.active_relation_chain

    resumed = build_typed_book_clock_constraint(
        chapter_id="ch-0003",
        time_gap="回到当夜",
        previous_projection=unanchored,
        previous_projection_hash="2" * 64,
        is_first_chapter=False,
    )
    assert resumed.kind == "ordered_relative"
    assert resumed.anchor_node_id == opening.node_id
    assert resumed.relations == ("resume_anchor", "same_night")


def test_typed_node_shapes_and_chain_hash_fail_closed() -> None:
    with pytest.raises(ValueError, match="ordered_relative"):
        BookTimeNode(
            node_id="a" * 64,
            chapter_id="ch-0002",
            kind="ordered_relative",
            raw_time_gap="稍后",
            relations=("after",),
        )
    with pytest.raises(ValueError, match="bounded_interval"):
        BookTimeNode(
            node_id="b" * 64,
            chapter_id="ch-0002",
            kind="bounded_interval",
            raw_time_gap="两到两天后",
            gap_interval=BookClockInterval(
                minimum_minutes=2880,
                maximum_minutes=2880,
            ),
        )
    with pytest.raises(ValueError, match="genuinely_unanchored"):
        BookTimeNode(
            node_id="c" * 64,
            chapter_id="ch-0002",
            kind="genuinely_unanchored",
            raw_time_gap="梦中不知多久",
            relations=("after",),
            unanchored_reason="dream",
        )
    with pytest.raises(ValueError, match="next_narrative_day"):
        BookTimeNode(
            node_id="d" * 64,
            chapter_id="ch-0002",
            kind="ordered_relative",
            raw_time_gap="同夜又说是次日",
            anchor_node_id="a" * 64,
            relations=("same_night", "next_narrative_day"),
        )

    opening = build_typed_book_clock_constraint(
        chapter_id="ch-0001",
        time_gap="开篇",
        previous_projection=None,
        previous_projection_hash="0" * 64,
        is_first_chapter=True,
    )
    projection = advance_book_time_projection(
        previous_projection=None,
        constraint=opening,
    )
    with pytest.raises(ValueError, match="relation_chain_hash"):
        BookTimeProjection(
            contract_version=1,
            current_node=projection.current_node,
            latest_mainline_node_id=projection.latest_mainline_node_id,
            latest_absolute_anchor=projection.latest_absolute_anchor,
            active_relation_chain=projection.active_relation_chain,
            relation_chain_hash="f" * 64,
        )


def test_typed_node_id_is_deterministic_and_binds_previous_projection_hash() -> None:
    kwargs = {
        "chapter_id": "ch-0001",
        "time_gap": "开篇",
        "previous_projection": None,
        "is_first_chapter": True,
    }
    first = build_typed_book_clock_constraint(
        previous_projection_hash="0" * 64,
        **kwargs,
    )
    replay = build_typed_book_clock_constraint(
        previous_projection_hash="0" * 64,
        **kwargs,
    )
    drifted = build_typed_book_clock_constraint(
        previous_projection_hash="1" * 64,
        **kwargs,
    )
    assert first.node_id == replay.node_id
    assert first.relation_chain_hash == replay.relation_chain_hash
    assert drifted.node_id != first.node_id


def test_legacy_v0_projection_hash_bytes_do_not_change() -> None:
    assert compute_core_state_projection_hash(facts=(), book_clock=None) == (
        "0a17980e6ea5782c70ff33092fecde0314c517f06d85387fb8aa2b77350375e0"
    )
    legacy = BookClockConstraint(
        time_gap="过了一阵",
        previous_interval=None,
        gap_interval=None,
        current_interval=None,
        is_hard=False,
        soft_reason="legacy unknown",
    )
    assert "contract_version" not in legacy.model_dump(mode="json")
    assert "kind" not in legacy.model_dump(mode="json")


def test_book_clock_serialization_schema_keeps_additive_typed_fields() -> None:
    schema = BookClockConstraint.model_json_schema(mode="serialization")
    wire = schema["$defs"]["BookClockConstraintWire"]
    assert {"contract_version", "kind", "relations", "approximate_quantity"} <= set(
        wire["properties"]
    )


def test_pre_c2_prompt_archives_are_byte_frozen_and_active_prompts_advance() -> None:
    for archive_name, expected_hash in _PROMPT_ARCHIVE_HASHES.items():
        archive_path = _ROOT / "prompts" / "archive" / archive_name
        active_name = archive_name.split("_pre_c2_typed_book_time", maxsplit=1)[0]
        active_path = _ROOT / "prompts" / f"{active_name}.md"
        archive_bytes = archive_path.read_bytes()
        assert hashlib.sha256(archive_bytes).hexdigest() == expected_hash
        assert active_path.read_bytes() != archive_bytes
