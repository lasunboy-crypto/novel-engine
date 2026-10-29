"""R9 可执行 HARD 计划：编译、同源消费与无进展停止。"""

from __future__ import annotations

from datetime import UTC, datetime

import pytest

from src.core.models.chapter_card import ChapterCard, ContinuityContext
from src.core.models.character import Character, VoiceProfile
from src.core.models.core_state import (
    BookClockConstraint,
    BookClockInterval,
    CoreStateFact,
    CoreStateProjection,
    advance_book_time_projection,
    build_typed_book_clock_constraint,
    make_core_state_projection,
)
from src.core.models.review import (
    ConstraintResult,
    ConstraintViolation,
    ReviewResult,
    stable_constraint_id,
)
from src.core.models.setting_contract import SettingContract
from src.core.orchestrator.chapter_orchestrator import _repeated_hard_failure_ids
from src.core.rules.cross_chapter_voice_validator import (
    resolve_cross_chapter_voice_constraint_spec,
)
from src.core.rules.prose_hard_contract import (
    ExecutableConstraintPlanError,
    RewriteStyleConstraintSpec,
    compile_executable_constraint_plan,
    render_executable_constraint_plan,
)
from src.core.rules.validator_pipeline import build_deterministic_hard_violations
from src.core.rules.validators import ValidatorResult


def _card(**overrides: object) -> ChapterCard:
    payload: dict[str, object] = {
        "chapter_id": "ch-0004",
        "workspace_id": "ws-generic",
        "volume_id": "v01",
        "chapter_number": 4,
        "title": "第四章",
        "tension_level": 7,
        "word_count_min": 2500,
        "word_count_max": 4000,
        "plot_node_budget": 3,
        "must_achieve": ["辨认门上三枚旧刻痕", "决定由谁先进入"],
        "must_unresolved": ["刻痕由谁留下"],
        "forbidden": ["本章揭示幕后主使"],
        "pov_character": "char-a",
        "setting": "封闭门廊",
        "continuity_context": ContinuityContext(
            prev_chapter_tail="门轴刚刚发出一声轻响。",
            unfinished_actions=["继续推动石门"],
            time_gap="立即续接",
            scene_continuation="same",
            pov_change=False,
            pov_from="char-a",
            pov_to="char-a",
        ),
        "next_chapter_seed": "门后出现第二组脚印",
        "arc_phase": "rising",
        "is_arc_finale": False,
        "related_plot_threads": [],
        "related_characters": ["char-a"],
    }
    payload.update(overrides)
    return ChapterCard.model_validate(payload)


def _character() -> Character:
    return Character(
        character_id="char-a",
        workspace_id="ws-generic",
        name="甲",
        archetype="调查者",
        appearance="短发",
        personality="谨慎",
        background="旧城居民",
        voice_profile=VoiceProfile(
            tone="克制",
            sentence_style="短句",
            self_address="我",
            forbidden_phrases=["这也太离谱了"],
        ),
        goals_long_term=["查清旧城真相"],
        goals_short_term=["打开石门"],
    )


def _setting() -> SettingContract:
    return SettingContract(
        setting="旧城遗迹",
        unique_rules="石门只会对携带铜印的人开启；开门不会直接揭示幕后主使。",
        source_path="data/outline/world.json",
        source_hash="a" * 64,
    )


def _core_card_and_projection() -> tuple[ChapterCard, CoreStateProjection]:
    fact = CoreStateFact(
        revision_id="csr-mirror",
        state_key="object:青铜镜:surface",
        entity_type="object",
        entity_id="青铜镜",
        attribute="surface",
        value="无锈无氧化",
        source_kind="approved_text",
        source_chapter_id="ch-0003",
        source_version_id=1,
        source_path="chapter_summaries/ch-0003.core_state_candidates",
        source_text_hash="c" * 64,
        evidence_quote="镜面无锈无氧化。",
        valid_from_chapter=3,
    )
    projection = make_core_state_projection(
        facts=[fact],
        book_clock=BookClockInterval(
            minimum_minutes=8 * 24 * 60,
            maximum_minutes=10 * 24 * 60,
        ),
    )
    clock = BookClockConstraint(
        time_gap="一日后",
        previous_interval=projection.book_clock,
        gap_interval=BookClockInterval(
            minimum_minutes=24 * 60,
            maximum_minutes=24 * 60,
        ),
        current_interval=BookClockInterval(
            minimum_minutes=9 * 24 * 60,
            maximum_minutes=11 * 24 * 60,
        ),
        is_hard=True,
    )
    return (
        _card(
            continuity_context=ContinuityContext(
                prev_chapter_tail="他把镜子收回木盒。",
                unfinished_actions=[],
                time_gap="一日后",
                scene_continuation="switch",
                pov_change=False,
                pov_from="char-a",
                pov_to="char-a",
            ),
            core_state_contract_version=1,
            core_state_projection_hash=projection.projection_hash,
            book_clock_constraint=clock,
        ),
        projection,
    )


def _review(
    *,
    result: ConstraintResult,
    version: int,
    violations: list[ConstraintViolation] | None = None,
    failed_constraints: list[str] | None = None,
) -> ReviewResult:
    return ReviewResult(
        review_id=f"rev-{version}",
        chapter_id="ch-0004",
        version_reviewed=version,
        reviewed_at=datetime.now(UTC),
        reviewer_model="mock",
        score_plot_progression=7,
        score_tension_match=7,
        score_prose_quality=7,
        score_consistency=7,
        score_hook_management=7,
        score_voice_consistency=7,
        score_overall=7,
        hard_constraints_passed=False,
        failed_constraints=failed_constraints or [f"must_achieve: {result.rule_text}"],
        issues=[],
        suggestions=[],
        verdict="rewrite",
        revise_targets=[],
        constraint_results=[result],
        constraint_violations=violations or [],
    )


def test_compiler_is_deterministic_and_renders_one_shared_hash() -> None:
    card = _card()
    kwargs = {
        "chapter_card": card,
        "stage": "rewrite",
        "setting_contract": _setting(),
        "character_records": [_character()],
        "expected_voice": "third_person",
        "rewrite_style": RewriteStyleConstraintSpec(
            paragraph_baseline=60,
            paragraph_threshold=0.5,
            paragraph_hard=True,
            sentence_baseline=22,
            sentence_threshold=0.5,
            sentence_hard=True,
        ),
    }

    first = compile_executable_constraint_plan(**kwargs)  # type: ignore[arg-type]
    second = compile_executable_constraint_plan(**kwargs)  # type: ignore[arg-type]

    assert first == second
    assert first.plan_hash == second.plan_hash
    assert all(item.severity == "hard" for item in first.constraints)
    assert {item.provenance for item in first.constraints} >= {
        "chapter_card",
        "setting_contract",
        "character_record",
        "chapter_version",
        "engine_rule",
    }
    assert {item.kind for item in first.constraints} >= {
        "word_count",
        "paragraph_mean_floor",
        "must_achieve",
        "must_unresolved",
        "forbidden",
        "continuity_unfinished_actions",
        "continuity_scene_same",
        "setting_unique_rules",
        "voice_forbidden",
        "cross_chapter_voice",
        "style_drift",
    }
    rendered = render_executable_constraint_plan(first)
    assert first.plan_hash in rendered
    assert "| severity | stages | provenance |" in rendered
    assert all(item.constraint_id in rendered for item in first.constraints)
    voice_rule = first.by_kind("cross_chapter_voice")[0].rule_text
    assert "pov_change 只记录跨章主视角角色变化" in voice_rule
    assert "pov_change，或" not in voice_rule


def test_core_state_and_book_clock_compile_once_for_writer_and_judges() -> None:
    card, projection = _core_card_and_projection()
    plan = compile_executable_constraint_plan(
        chapter_card=card,
        stage="draft",
        core_state_projection=projection,
    )

    core = plan.by_kind("core_state")
    clock = plan.by_kind("book_clock")
    assert len(core) == 1
    assert len(clock) == 1
    assert core[0].source_hash == projection.projection_hash
    assert clock[0].source_hash == projection.projection_hash
    assert projection.projection_hash in core[0].rule_text
    assert "12960—15840 分钟" in clock[0].rule_text
    plan.assert_matches_core_state(
        chapter_card=card,
        projection=projection,
    )

    drifted = make_core_state_projection(
        facts=[],
        book_clock=BookClockInterval(minimum_minutes=0, maximum_minutes=0),
    )
    with pytest.raises(ExecutableConstraintPlanError, match="projection_hash"):
        plan.assert_matches_core_state(chapter_card=card, projection=drifted)


def test_typed_approximate_clock_keeps_natural_relation_without_numeric_bounds() -> None:
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
    clock = build_typed_book_clock_constraint(
        chapter_id="ch-0004",
        time_gap="同夜稍后，约一小时后",
        previous_projection=opening_time,
        previous_projection_hash=projection.projection_hash,
        is_first_chapter=False,
    )
    card = _card(
        continuity_context=ContinuityContext(
            prev_chapter_tail="他们仍守在当夜的门廊。",
            unfinished_actions=[],
            time_gap="同夜稍后，约一小时后",
            scene_continuation="switch",
            pov_change=False,
            pov_from="char-a",
            pov_to="char-a",
        ),
        core_state_contract_version=1,
        core_state_projection_hash=projection.projection_hash,
        book_clock_constraint=clock,
    )

    plan = compile_executable_constraint_plan(
        chapter_card=card,
        stage="draft",
        core_state_projection=projection,
    )

    frozen = plan.by_kind("book_clock")[0]
    parameters = {item.name: item.value for item in frozen.parameters}
    assert clock.kind == "ordered_relative"
    assert clock.gap_interval is None
    assert clock.current_interval is None
    assert parameters["kind"] == "ordered_relative"
    assert parameters["relations"] == ("after", "same_night")
    assert parameters["source_approximate_quantity"] == "1 hour"
    assert "explicit_minimum_minutes" not in parameters
    assert "explicit_maximum_minutes" not in parameters
    assert "同夜稍后，约一小时后" in frozen.rule_text
    assert "不生成分钟上下界" in frozen.rule_text
    assert "距开篇" not in frozen.rule_text
    assert "ordered_relative" not in frozen.rule_text
    assert "same_night" not in frozen.rule_text
    assert "180" not in frozen.rule_text
    plan.assert_matches_core_state(chapter_card=card, projection=projection)


@pytest.mark.parametrize(
    ("time_gap", "kind", "minimum", "maximum"),
    [
        ("三小时后", "exact", 180, 180),
        ("两到三天后", "bounded_interval", 2880, 4320),
    ],
)
def test_typed_explicit_numeric_facts_stay_in_machine_parameters_only(
    time_gap: str,
    kind: str,
    minimum: int,
    maximum: int,
) -> None:
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
    clock = build_typed_book_clock_constraint(
        chapter_id="ch-0004",
        time_gap=time_gap,
        previous_projection=opening_time,
        previous_projection_hash=projection.projection_hash,
        is_first_chapter=False,
    )
    card = _card(
        continuity_context=ContinuityContext(
            prev_chapter_tail="他们正在等待。",
            unfinished_actions=[],
            time_gap=time_gap,
            scene_continuation="switch",
            pov_change=False,
            pov_from="char-a",
            pov_to="char-a",
        ),
        core_state_contract_version=1,
        core_state_projection_hash=projection.projection_hash,
        book_clock_constraint=clock,
    )

    plan = compile_executable_constraint_plan(
        chapter_card=card,
        stage="draft",
        core_state_projection=projection,
    )

    frozen = plan.by_kind("book_clock")[0]
    parameters = {item.name: item.value for item in frozen.parameters}
    assert parameters["kind"] == kind
    assert parameters["explicit_minimum_minutes"] == minimum
    assert parameters["explicit_maximum_minutes"] == maximum
    assert time_gap in frozen.rule_text
    assert str(minimum) not in frozen.rule_text
    assert str(maximum) not in frozen.rule_text
    assert "距开篇" not in frozen.rule_text


def test_card_setting_and_character_drift_fail_closed() -> None:
    plan = compile_executable_constraint_plan(
        chapter_card=_card(),
        stage="draft",
        setting_contract=_setting(),
        character_records=[_character()],
    )

    with pytest.raises(ExecutableConstraintPlanError, match="must_achieve"):
        plan.assert_matches_chapter_card(_card(must_achieve=["改成另一件必达事件"]))
    with pytest.raises(ExecutableConstraintPlanError, match="SettingContract"):
        plan.assert_matches_setting_contract(
            _setting().model_copy(update={"unique_rules": "另一套世界规则"})
        )
    changed_character = _character().model_copy(
        update={
            "voice_profile": _character().voice_profile.model_copy(
                update={"forbidden_phrases": ["另一句禁语"]}
            )
        }
    )
    with pytest.raises(ExecutableConstraintPlanError, match="voice_forbidden"):
        plan.assert_matches_character_records(
            chapter_card=_card(), character_records=[changed_character]
        )


def test_voice_and_style_judges_cannot_reparse_another_hard_contract() -> None:
    style = RewriteStyleConstraintSpec(
        paragraph_baseline=60,
        paragraph_threshold=0.5,
        paragraph_hard=True,
        sentence_baseline=22,
        sentence_threshold=0.5,
        sentence_hard=True,
    )
    voice = resolve_cross_chapter_voice_constraint_spec(
        baseline_fingerprint={
            "total_chars": 3000.0,
            "pronoun_freq_他": 0.02,
            "pronoun_freq_我": 0.001,
        },
        baseline_chapter_id="ch-0003",
        workspace_default_voice="third_person",
        chapter_card=_card(),
    )
    changed_pov_card = _card(
        continuity_context=_card().continuity_context.model_copy(
            update={
                "pov_change": True,
                "pov_from": "char-b",
                "pov_to": "char-a",
            }
        )
    )
    changed_pov_voice = resolve_cross_chapter_voice_constraint_spec(
        baseline_fingerprint={
            "total_chars": 3000.0,
            "pronoun_freq_他": 0.02,
            "pronoun_freq_我": 0.001,
        },
        baseline_chapter_id="ch-0003",
        workspace_default_voice="third_person",
        chapter_card=changed_pov_card,
    )
    assert changed_pov_voice.hard_enabled is True
    plan = compile_executable_constraint_plan(
        chapter_card=_card(),
        stage="rewrite",
        cross_chapter_voice=voice,
        rewrite_style=style,
    )
    plan.assert_matches_cross_chapter_voice(voice)
    plan.assert_matches_rewrite_style(style)
    plan.assert_style_failure_dimensions(
        {"paragraph_length_mean", "sentence_length_mean"}
    )

    with pytest.raises(ExecutableConstraintPlanError, match="G10/G12"):
        plan.assert_matches_cross_chapter_voice(
            resolve_cross_chapter_voice_constraint_spec(
                baseline_fingerprint={
                    "total_chars": 3000.0,
                    "pronoun_freq_他": 0.001,
                    "pronoun_freq_我": 0.02,
                },
                baseline_chapter_id="ch-0003",
                workspace_default_voice="third_person",
                chapter_card=_card(),
            )
        )
    with pytest.raises(ExecutableConstraintPlanError, match="G17/G5"):
        plan.assert_matches_rewrite_style(
            style.model_copy(update={"sentence_hard": False})
        )
    with pytest.raises(ExecutableConstraintPlanError, match="计划外 HARD 维度"):
        plan.assert_style_failure_dimensions({"simile_density"})

    default_voice = resolve_cross_chapter_voice_constraint_spec(
        baseline_fingerprint=None,
        baseline_chapter_id=None,
        workspace_default_voice="third_person",
        chapter_card=_card(),
    )
    default_plan = compile_executable_constraint_plan(
        chapter_card=_card(),
        stage="draft",
        cross_chapter_voice=default_voice,
    )
    assert default_plan.by_kind("cross_chapter_voice")[0].provenance == (
        "workspace_config"
    )


def test_pronoun_frequency_only_change_does_not_drift_hard_plan_identity() -> None:
    """Session D：代词倍率是 WARN，不能再改变 HARD plan hash。"""

    baseline = resolve_cross_chapter_voice_constraint_spec(
        baseline_fingerprint={
            "total_chars": 3000.0,
            "pronoun_freq_他": 0.02,
            "pronoun_freq_我": 0.001,
        },
        baseline_chapter_id="ch-0003",
        workspace_default_voice="third_person",
        chapter_card=_card(),
    )
    changed_frequency = resolve_cross_chapter_voice_constraint_spec(
        baseline_fingerprint={
            "total_chars": 3000.0,
            "pronoun_freq_他": 0.04,
            "pronoun_freq_我": 0.003,
        },
        baseline_chapter_id="ch-0003",
        workspace_default_voice="third_person",
        chapter_card=_card(),
    )
    assert changed_frequency == baseline
    assert {parameter.name for parameter in baseline.parameters} == {
        "baseline_dominant_pov",
        "workspace_default_voice",
    }


def test_judge_cannot_introduce_unplanned_hard_identity() -> None:
    plan = compile_executable_constraint_plan(
        chapter_card=_card(),
        stage="draft",
    )
    planned = plan.by_kind("must_achieve")[0]
    valid = planned.as_constraint_result(status="fail", details="正文没有辨认刻痕")
    plan.assert_review_uses_only_planned_hard(_review(result=valid, version=1))

    unknown = ConstraintResult(
        constraint_id=stable_constraint_id(
            source_path="runtime.judge",
            domain="must_achieve",
            rule_text="Judge 临时新增的要求",
        ),
        domain="must_achieve",
        source_path="runtime.judge",
        rule_text="Judge 临时新增的要求",
        status="fail",
        details="临时证据",
    )
    with pytest.raises(ExecutableConstraintPlanError, match="未在计划中声明"):
        plan.assert_review_uses_only_planned_hard(
            _review(
                result=unknown,
                version=2,
                failed_constraints=["must_achieve: Judge 临时新增的要求"],
            )
        )


def test_setting_hard_child_must_quote_the_planned_authority() -> None:
    setting = _setting()
    plan = compile_executable_constraint_plan(
        chapter_card=_card(),
        stage="draft",
        setting_contract=setting,
    )
    parent = plan.by_kind("setting_unique_rules")[0].as_constraint_result(
        status="fail", details="石门无铜印却开启"
    )
    child_rule = "石门只会对携带铜印的人开启"
    child = ConstraintViolation(
        constraint_id=stable_constraint_id(
            source_path=setting.source_path,
            domain="world.unique_rules",
            rule_text=child_rule,
        ),
        domain="world.unique_rules",
        source_path=setting.source_path,
        source_hash=setting.source_hash,
        rule_quote=child_rule,
        evidence_quote="他没有铜印，石门仍然打开了。",
        severity="hard",
    )
    review = _review(
        result=parent,
        version=1,
        violations=[child],
        failed_constraints=[f"setting:{child.constraint_id}:直接冲突"],
    )
    plan.assert_review_uses_only_planned_hard(review)

    unplanned = child.model_copy(update={"rule_quote": "来源里不存在的规则"})
    with pytest.raises(ExecutableConstraintPlanError, match="无法回指"):
        plan.assert_review_uses_only_planned_hard(
            _review(
                result=parent,
                version=2,
                violations=[unplanned],
                failed_constraints=[f"setting:{unplanned.constraint_id}:直接冲突"],
            )
        )


def test_all_deterministic_failures_bind_to_exact_planned_hard_identity() -> None:
    """字数/悬念/禁语/人称/风格/段均均不得只留下无身份的展示字符串。"""

    plan = compile_executable_constraint_plan(
        chapter_card=_card(),
        stage="rewrite",
        character_records=[_character()],
        expected_voice="third_person",
        rewrite_style=RewriteStyleConstraintSpec(
            paragraph_baseline=60,
            paragraph_threshold=0.5,
            paragraph_hard=True,
            sentence_baseline=22,
            sentence_threshold=0.5,
            sentence_hard=True,
        ),
    )
    violations = build_deterministic_hard_violations(
        results=[
            ValidatorResult(
                name="word_count",
                passed=False,
                details={
                    "actual_word_count": 4100,
                    "word_count_min": 2500,
                    "word_count_max": 4000,
                    "reason": "above_max",
                },
            ),
            ValidatorResult(
                name="must_unresolved",
                passed=False,
                details={"items_resolved": ["刻痕由谁留下"]},
            ),
            ValidatorResult(
                name="voice_forbidden",
                passed=False,
                details={
                    "attributable_hits": [
                        {
                            "character_id": "char-a",
                            "name": "甲",
                            "phrase": "这也太离谱了",
                        }
                    ]
                },
            ),
            ValidatorResult(
                name="cross_chapter_voice",
                passed=False,
                details={"failures": [{"type": "dominant_pov_flipped"}]},
            ),
            ValidatorResult(
                name="style_drift",
                passed=False,
                details={
                    "drifted_dimensions": [
                        {
                            "dimension": "sentence_length_mean",
                            "baseline_value": 22.0,
                            "current_value": 34.0,
                            "relative_drift": 0.545,
                            "threshold": 0.5,
                        }
                    ]
                },
            ),
        ],
        executable_constraint_plan=plan,
        current_version_id=2,
    )
    paragraph = plan.by_kind("paragraph_mean_floor")[0].as_hard_violation(
        evidence_quote='{"actual_paragraph_length_mean":15.0,"minimum":30.0}',
        source_chapter_id="ch-0004",
        source_final_version=2,
    )
    violations.append(paragraph)

    planned_kinds = {
        plan.hard_violation_constraint(item).kind for item in violations
    }
    assert planned_kinds == {
        "word_count",
        "paragraph_mean_floor",
        "must_unresolved",
        "voice_forbidden",
        "cross_chapter_voice",
        "style_drift",
    }
    assert all(item.evidence_quote for item in violations)
    assert all(item.source_final_version == 2 for item in violations)

    drifted = violations[0].model_copy(update={"source_path": "runtime.changed"})
    with pytest.raises(ExecutableConstraintPlanError, match="身份不一致"):
        plan.hard_violation_constraint(drifted)


def test_same_hard_id_and_same_evidence_escalates_but_changed_evidence_does_not() -> (
    None
):
    plan = compile_executable_constraint_plan(chapter_card=_card(), stage="rewrite")
    planned = plan.by_kind("must_achieve")[0]
    first_result = planned.as_constraint_result(
        status="fail", details="只写了看门，没有辨认三枚刻痕"
    )
    repeated_result = planned.as_constraint_result(
        status="fail", details="只写了看门，没有辨认三枚刻痕"
    )
    changed_result = planned.as_constraint_result(
        status="fail", details="辨认了两枚，第三枚仍缺失"
    )

    assert _repeated_hard_failure_ids(
        previous=_review(result=first_result, version=1),
        current=_review(result=repeated_result, version=2),
    ) == [planned.constraint_id]
    assert (
        _repeated_hard_failure_ids(
            previous=_review(result=first_result, version=1),
            current=_review(result=changed_result, version=2),
        )
        == []
    )
    assert (
        _repeated_hard_failure_ids(
            previous=_review(result=first_result, version=1),
            current=_review(result=repeated_result, version=3),
        )
        == []
    )
    assert (
        _repeated_hard_failure_ids(
            previous=_review(result=first_result, version=2),
            current=_review(result=repeated_result, version=2),
        )
        == []
    )
