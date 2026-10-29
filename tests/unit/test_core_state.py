"""全书核心状态与主时间线的纯代码合同。

这里只验证无 IO 规则：保守时间换算、可变状态键、
Writer/Judge 共用胶囊和信息密度 fail-closed。
"""

from __future__ import annotations

import pytest

from src.core.models.core_state import (
    BookClockInterval,
    CoreStateContractError,
    CoreStateDensityError,
    CoreStateFact,
    CoreStateTransition,
    build_book_clock_constraint,
    make_core_state_projection,
    parse_book_time_gap,
    render_core_state_capsule,
    validate_core_state_transition_sources,
)


def _fact(index: int, *, value_size: int = 4) -> CoreStateFact:
    entity_id = f"承重物件{index}"
    return CoreStateFact(
        revision_id=f"rev-{index}",
        state_key=f"object:{entity_id}:surface",
        entity_type="object",
        entity_id=entity_id,
        attribute="surface",
        value="稳" * value_size,
        source_kind="approved_text",
        source_chapter_id="ch-0001",
        source_version_id=1,
        source_path=f"chapter_summaries/ch-0001.core[{index}]",
        source_text_hash="a" * 64,
        evidence_quote="逐字证据",
        valid_from_chapter=1,
    )


@pytest.mark.parametrize(
    ("raw", "minimum", "maximum"),
    [
        ("开篇", 0, 0),
        ("紧接上章", 0, 5),
        ("半天后", 720, 720),
        ("三日后", 4320, 4320),
        ("次日黄昏", 1440, 1440),
        ("次日白天，沙尘暴减弱后数小时", 1500, 2160),
        ("当日入夜", 360, 1080),
        ("一至两日后的深夜", 1440, 2880),
        ("两周后", 20160, 20160),
        ("三个月后", 129600, 129600),
    ],
)
def test_parse_book_time_gap_only_returns_bounded_mainline_intervals(
    raw: str, minimum: int, maximum: int
) -> None:
    assert parse_book_time_gap(raw) == BookClockInterval(
        minimum_minutes=minimum,
        maximum_minutes=maximum,
    )


@pytest.mark.parametrize(
    "raw",
    [
        "几天后",
        "一两天后",
        "梦里的三个月",
        "回忆中的三日后",
        "三天或五天后",
        "约三日后",
        "至少三日后",
        "三日后或更晚",
        "三个月前",
        "数日前",
        "新场景",
    ],
)
def test_parse_book_time_gap_keeps_ambiguous_or_non_mainline_text_soft(
    raw: str,
) -> None:
    assert parse_book_time_gap(raw) is None


def test_book_clock_accumulates_clear_gaps_but_unknown_gap_breaks_hard_chain() -> None:
    opening = build_book_clock_constraint(
        time_gap="开篇", previous_interval=None, is_first_chapter=True
    )
    assert opening.is_hard is True
    assert opening.current_interval == BookClockInterval(
        minimum_minutes=0, maximum_minutes=0
    )

    assert opening.current_interval is not None
    chapter_two = build_book_clock_constraint(
        time_gap="三日后",
        previous_interval=opening.current_interval,
        is_first_chapter=False,
    )
    assert chapter_two.current_interval == BookClockInterval(
        minimum_minutes=4320, maximum_minutes=4320
    )

    ambiguous = build_book_clock_constraint(
        time_gap="过了一阵",
        previous_interval=chapter_two.current_interval,
        is_first_chapter=False,
    )
    assert ambiguous.is_hard is False
    assert ambiguous.current_interval is None
    assert "含混" in ambiguous.soft_reason


def test_transition_has_stable_key_and_rejects_same_value() -> None:
    transition = CoreStateTransition(
        entity_type="object",
        entity_id="青铜镜",
        attribute="surface",
        from_value="无锈无氧化",
        to_value="暗绿色铜锈",
        reason="经过明示的化学腐蚀",
    )
    assert transition.state_key == "object:青铜镜:surface"

    with pytest.raises(ValueError, match="from_value.*to_value"):
        CoreStateTransition(
            entity_type="object",
            entity_id="青铜镜",
            attribute="surface",
            from_value="无锈",
            to_value="无锈",
            reason="无变化",
        )


def test_transition_source_contract_uses_current_projection_as_only_from_value() -> None:
    projection = make_core_state_projection(facts=[_fact(1)])
    exact = CoreStateTransition(
        entity_type="object",
        entity_id="承重物件1",
        attribute="surface",
        from_value="稳稳稳稳",
        to_value="已经破损",
        reason="本章上游明确安排破损",
    )
    validate_core_state_transition_sources(
        transitions=[exact],
        projection=projection,
    )

    invented = exact.model_copy(
        update={
            "entity_id": "TQ-07",
            "state_key": "object:TQ-07:surface",
            "from_value": "二级封存",
        }
    )
    with pytest.raises(CoreStateContractError, match="当前没有该事实"):
        validate_core_state_transition_sources(
            transitions=[invented],
            projection=projection,
        )

    stale = exact.model_copy(update={"from_value": "旧表面"})
    with pytest.raises(CoreStateContractError, match="from_value 已陈旧"):
        validate_core_state_transition_sources(
            transitions=[stale],
            projection=projection,
        )


def test_prompt_capsule_is_compact_and_does_not_require_restatement() -> None:
    projection = make_core_state_projection(
        facts=[_fact(1)],
        book_clock=BookClockInterval(minimum_minutes=1440, maximum_minutes=2880),
    )
    capsule = render_core_state_capsule(projection=projection)

    assert "不要求正文逐条复述" in capsule.text
    assert "object:承重物件1" in capsule.text
    assert "1天—2天" in capsule.text
    assert capsule.group_count == 2
    assert capsule.above_target is False


def test_prompt_capsule_fails_closed_instead_of_truncating_hard_facts() -> None:
    projection = make_core_state_projection(facts=[_fact(i) for i in range(13)])

    with pytest.raises(CoreStateDensityError, match="禁止静默截断"):
        render_core_state_capsule(projection=projection)


@pytest.mark.parametrize('attribute', ['possession', 'control', 'access'])
def test_relation_reaffirmation_requires_existing_exact_projected_owner(attribute):
    transition = CoreStateTransition(entity_type='object', entity_id='object-001',
        attribute=attribute, from_value='actor-001', to_value='actor-001', reason='本章继续由同一角色保管')
    fact = _fact(1).model_copy(update={'entity_id':'object-001',
        'state_key':f'object:object-001:{attribute}', 'attribute':attribute,'value':'actor-001'})
    validate_core_state_transition_sources(transitions=[transition],
        projection=make_core_state_projection(facts=[fact]))
    with pytest.raises(CoreStateContractError, match='当前没有该事实'):
        validate_core_state_transition_sources(transitions=[transition],
            projection=make_core_state_projection())
    with pytest.raises(CoreStateContractError, match='已陈旧'):
        validate_core_state_transition_sources(transitions=[transition],
            projection=make_core_state_projection(facts=[fact.model_copy(update={'value':'actor-002'})]))
    # First registration uses unknown/null, never an invented previous owner.
    validate_core_state_transition_sources(
        transitions=[transition.model_copy(update={'from_value':None})],
        projection=make_core_state_projection())
