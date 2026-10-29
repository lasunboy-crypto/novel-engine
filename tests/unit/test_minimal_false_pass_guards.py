"""R9 v5 最小误放行修复的纯代码、防过修回归。"""

from __future__ import annotations

from dataclasses import replace
from datetime import UTC, datetime
import inspect
from pathlib import Path

import pytest

from src.core.agents.must_achieve_validator import CoverageEvidence, ValidationResult
from src.core.models.chapter_card import ChapterCard, ContinuityContext
from src.core.models.review import ConstraintResult, ReviewIssue, ReviewResult
from src.core.models.setting_contract import SettingContract
from src.core.orchestrator import chapter_orchestrator as chapter_orchestrator_module
from src.core.rules.minimal_false_pass_guards import (
    apply_objective_reviewer_guards,
    guard_must_fail_to_pass,
)
from src.core.rules.prose_hard_contract import (
    ExecutableConstraintPlan,
    compile_executable_constraint_plan,
)
from src.core.rules.prose_revision_contract import (
    ProseRevisionContract,
    ProseRevisionContractError,
    apply_revision_response,
    build_reviser_revision_contract,
    build_rewrite_revision_contract,
    find_localized_adjacent_duplicates,
)
from src.services.chapter_service import ChapterService

_REPO_ROOT = Path(__file__).resolve().parents[2]


def _card() -> ChapterCard:
    return ChapterCard(
        chapter_id="ch-0002",
        workspace_id="ws-generic",
        volume_id="v01",
        chapter_number=2,
        title="门廊",
        tension_level=7,
        word_count_min=1,
        word_count_max=5_000,
        plot_node_budget=3,
        must_achieve=["守门人明确交出通行凭证"],
        must_unresolved=["内门后的脚步来源"],
        forbidden=["本章打开内门"],
        pov_character="keeper",
        setting="封闭门廊",
        continuity_context=ContinuityContext(
            prev_chapter_tail="钥匙刚插入锁孔。",
            unfinished_actions=["继续核对门锁"],
            time_gap="立即续接",
            scene_continuation="same",
            pov_change=False,
            pov_from="keeper",
            pov_to="keeper",
        ),
        next_chapter_seed="门后传来脚步声",
        arc_phase="rising",
        is_arc_finale=False,
        related_plot_threads=[],
        related_characters=["keeper"],
    )


def _plan(*, stage: str = "rewrite") -> ExecutableConstraintPlan:
    return compile_executable_constraint_plan(
        chapter_card=_card(),
        stage=stage,  # type: ignore[arg-type]
        setting_contract=SettingContract(
            setting="封闭门廊",
            unique_rules="内门只会在铜印贴近锁面时亮起。",
            source_path="data/outline/world.json",
            source_hash="a" * 64,
        ),
    )


def _must_result(*, status: str, details: str) -> ConstraintResult:
    item = _plan().by_kind("must_achieve")[0]
    return item.as_constraint_result(status=status, details=details)  # type: ignore[arg-type]


def _review(
    *,
    version: int,
    verdict: str = "pass",
    issues: list[ReviewIssue] | None = None,
    constraint_results: list[ConstraintResult] | None = None,
) -> ReviewResult:
    is_hard_pass = not any(
        item.status == "fail" for item in (constraint_results or [])
    )
    return ReviewResult(
        review_id=f"review-{version}",
        chapter_id="ch-0002",
        version_reviewed=version,
        reviewed_at=datetime.now(UTC),
        reviewer_model="mock-reviewer",
        score_plot_progression=9.0,
        score_tension_match=9.0,
        score_prose_quality=9.0,
        score_consistency=9.0,
        score_hook_management=9.0,
        score_voice_consistency=9.0,
        score_overall=9.0,
        hard_constraints_passed=is_hard_pass,
        failed_constraints=(
            [] if is_hard_pass else ["must_achieve: 守门人明确交出通行凭证"]
        ),
        issues=list(issues or []),
        suggestions=[],
        verdict=verdict,  # type: ignore[arg-type]
        revise_targets=list(issues or []) if verdict == "revise" else [],
        constraint_results=list(constraint_results or []),
    )


def _global_contract(source: str, *, source_version: int = 1) -> ProseRevisionContract:
    plan = _plan()
    must = plan.by_kind("must_achieve")[0]
    return build_rewrite_revision_contract(
        source_content=source,
        source_version_id=source_version,
        executable_constraint_plan=plan,
        must_fix=[must.as_constraint_result(status="fail", details="凭证尚未交出")],
        must_preserve=[],
        constraint_violations=[],
        other_failures=["must_achieve"],
        validator_failures=[],
    )


def _judge_yes(evidence: str) -> ValidationResult:
    return ValidationResult(
        passed=True,
        constraint_results=[_must_result(status="pass", details=evidence)],
        llm_calls_count=1,
    )


def test_unrelated_global_change_cannot_flip_prior_must_fail_to_pass() -> None:
    source = "守门人仍把凭证收在袖中。\n\n灯影像一条沉默的河。"
    candidate = "守门人仍把凭证收在袖中。\n\n灯影像一面静止的湖。"
    contract = _global_contract(source)
    prior = _review(
        version=1,
        verdict="rewrite",
        constraint_results=[_must_result(status="fail", details="凭证尚未交出")],
    )

    guarded = guard_must_fail_to_pass(
        result=_judge_yes("守门人仍把凭证收在袖中"),
        source_review=prior,
        candidate_text=candidate,
        candidate_version_id=2,
        revision_contract=contract,
        executable_constraint_plan=_plan(),
    )

    assert guarded.passed is False
    assert guarded.llm_calls_count == 1
    assert guarded.constraint_results[0].constraint_id == prior.constraint_results[0].constraint_id
    assert guarded.constraint_results[0].status == "fail"
    assert guarded.constraint_results[0].details == "凭证尚未交出"
    assert guarded.missing_items == ["守门人明确交出通行凭证"]


def test_exact_quote_inside_changed_paragraph_allows_must_transition() -> None:
    source = "守门人仍把凭证收在袖中。\n\n门外雨声未停。"
    candidate = "守门人取出通行凭证，郑重交到访客掌心。\n\n门外雨声未停。"
    contract = _global_contract(source)
    guarded = guard_must_fail_to_pass(
        result=_judge_yes("守门人取出通行凭证，郑重交到访客掌心"),
        source_review=_review(
            version=1,
            verdict="rewrite",
            constraint_results=[_must_result(status="fail", details="凭证尚未交出")],
        ),
        candidate_text=candidate,
        candidate_version_id=2,
        revision_contract=contract,
        executable_constraint_plan=_plan(),
    )

    assert guarded.passed is True
    assert guarded.constraint_results[0].status == "pass"


def test_deletion_repair_can_quote_surviving_text_in_changed_paragraph() -> None:
    source = "守门人交出通行凭证，却立刻把它夺回。　　门外雨声未停。"
    candidate = "守门人交出通行凭证。　　门外雨声未停。"
    contract = _global_contract(source)

    guarded = guard_must_fail_to_pass(
        result=_judge_yes("守门人交出通行凭证"),
        source_review=_review(
            version=1,
            verdict="rewrite",
            constraint_results=[
                _must_result(status="fail", details="凭证交出后又被夺回")
            ],
        ),
        candidate_text=candidate,
        candidate_version_id=2,
        revision_contract=contract,
        executable_constraint_plan=_plan(),
    )

    assert guarded.passed is True


@pytest.mark.parametrize(
    ("candidate", "evidence"),
    [
        ("守门人交出凭证。\n\n门外雨声未停。", "守门人完成了交接动作"),
        ("守门人仍把凭证收在袖中。\n\n门外雨声变急。", "守门人仍把凭证收在袖中"),
        (
            "守门人交出凭证。\n\n墙上写着守门人交出凭证。",
            "守门人交出凭证",
        ),
    ],
    ids=["summary-not-in-text", "unchanged-area", "ambiguous-location"],
)
def test_invalid_must_yes_evidence_preserves_canonical_failure(
    candidate: str, evidence: str
) -> None:
    source = "守门人仍把凭证收在袖中。\n\n门外雨声未停。"
    prior = _review(
        version=1,
        verdict="rewrite",
        constraint_results=[_must_result(status="fail", details="凭证尚未交出")],
    )
    guarded = guard_must_fail_to_pass(
        result=_judge_yes(evidence),
        source_review=prior,
        candidate_text=candidate,
        candidate_version_id=2,
        revision_contract=_global_contract(source),
        executable_constraint_plan=_plan(),
    )

    assert guarded.passed is False
    assert guarded.constraint_results[0].details == "凭证尚未交出"


def test_invalid_transition_preserves_repeated_hard_failure_identity() -> None:
    from src.core.orchestrator.chapter_orchestrator import (
        _repeated_hard_failure_ids,
    )

    source = "守门人仍把凭证收在袖中。　　门外雨声未停。"
    prior = _review(
        version=1,
        verdict="rewrite",
        constraint_results=[_must_result(status="fail", details="凭证尚未交出")],
    )
    guarded = guard_must_fail_to_pass(
        result=_judge_yes("守门人完成了交接动作"),
        source_review=prior,
        candidate_text="守门人仍把凭证收在袖中。　　门外雨声变急。",
        candidate_version_id=2,
        revision_contract=_global_contract(source),
        executable_constraint_plan=_plan(),
    )
    current = _review(
        version=2,
        verdict="rewrite",
        constraint_results=guarded.constraint_results,
    )

    assert _repeated_hard_failure_ids(previous=prior, current=current) == [
        prior.constraint_results[0].constraint_id
    ]


def test_first_draft_must_pass_keeps_existing_semantics() -> None:
    result = _judge_yes("守门人取出通行凭证，郑重交到访客掌心")
    assert (
        guard_must_fail_to_pass(
            result=result,
            source_review=None,
            candidate_text="守门人取出通行凭证，郑重交到访客掌心。",
            candidate_version_id=1,
            revision_contract=None,
            executable_constraint_plan=_plan(),
        )
        is result
    )


@pytest.mark.parametrize(
    ("candidate_version", "plan"),
    [(3, _plan()), (2, _plan(stage="revise"))],
    ids=["version-drift", "plan-drift"],
)
def test_must_transition_identity_drift_fails_closed(
    candidate_version: int, plan: ExecutableConstraintPlan
) -> None:
    source = "守门人仍把凭证收在袖中。\n\n门外雨声未停。"
    with pytest.raises(ProseRevisionContractError):
        guard_must_fail_to_pass(
            result=_judge_yes("守门人交出凭证"),
            source_review=_review(
                version=1,
                verdict="rewrite",
                constraint_results=[_must_result(status="fail", details="凭证尚未交出")],
            ),
            candidate_text="守门人交出凭证。\n\n门外雨声未停。",
            candidate_version_id=candidate_version,
            revision_contract=_global_contract(source),
            executable_constraint_plan=plan,
        )


def test_same_must_semantics_with_drifted_stable_id_fails_closed() -> None:
    source = "守门人仍把凭证收在袖中。\n\n门外雨声未停。"
    original = _judge_yes("守门人交出凭证")
    result = ValidationResult(
        passed=original.passed,
        constraint_results=[
            original.constraint_results[0].model_copy(
                update={"constraint_id": "f" * 64}
            )
        ],
        llm_calls_count=original.llm_calls_count,
    )

    with pytest.raises(
        ProseRevisionContractError,
        match="同语义 constraint_id 发生漂移",
    ):
        guard_must_fail_to_pass(
            result=result,
            source_review=_review(
                version=1,
                verdict="rewrite",
                constraint_results=[
                    _must_result(status="fail", details="凭证尚未交出")
                ],
            ),
            candidate_text="守门人交出凭证。\n\n门外雨声未停。",
            candidate_version_id=2,
            revision_contract=_global_contract(source),
            executable_constraint_plan=_plan(),
        )


def test_revision_contract_source_hash_drift_fails_closed() -> None:
    source = "守门人仍把凭证收在袖中。　　门外雨声未停。"
    contract = _global_contract(source)

    with pytest.raises(ProseRevisionContractError):
        contract.assert_compatible(
            source_content=source + "额外漂移。",
            source_version_id=contract.source_version_id,
            executable_constraint_plan=_plan(),
        )


def _localized_contract(source: str, *, target_anchor_text: str) -> ProseRevisionContract:
    plan = _plan(stage="revise")
    issue = ReviewIssue(
        paragraph_anchor=target_anchor_text[:30],
        issue_type="prose",
        description="补清楚过桥动作",
        suggestion="只改该段",
    )
    return build_reviser_revision_contract(
        source_content=source,
        source_version_id=1,
        executable_constraint_plan=plan,
        issues=[issue],
        validator_failures=[],
        constraint_results=[],
    )


def _localized_candidate(
    contract: ProseRevisionContract, *, replacement: str
) -> str:
    target = contract.targets[0]
    return apply_revision_response(
        contract=contract,
        raw_content=(
            "{"
            '"contract_version":"1","mode":"localized",'
            f'"source_version_id":{contract.source_version_id},'
            f'"source_sha256":"{contract.source_sha256}",'
            f'"plan_hash":"{contract.plan_hash}",'
            '"patches":[{'
            f'"target_ids":["{target.target_id}"],'
            f'"anchor_id":"{target.allowed_anchor_ids[0]}",'
            '"operation":"replace",'
            f'"replacement":"{replacement}"'
            "}]}"
        ),
    )


def _long(seed: str, *, repeats: int = 12) -> str:
    return (seed * repeats)[:260]


def test_localized_exact_adjacent_long_duplicate_forces_existing_revise_path() -> None:
    kept = _long("守门人逐项核对刻痕与封蜡，确认所有编号都对应原始登记。")
    target = _long("访客站在门边等待，手里没有触碰任何机关。")
    source = f"{kept}\n\n{target}\n\n末段保持不变。"
    contract = _localized_contract(source, target_anchor_text=target)
    candidate = _localized_candidate(contract, replacement=kept)

    findings = find_localized_adjacent_duplicates(
        contract=contract, candidate_content=candidate
    )
    guarded = apply_objective_reviewer_guards(
        review=_review(version=2),
        chapter_text=candidate,
        revision_contract=contract,
    )

    assert [item.match_kind for item in findings] == ["exact"]
    assert guarded.verdict == "revise"
    assert guarded.revise_targets[0].issue_type == "adjacent_duplicate"
    # 机器锚点可由下一轮现有 Reviser 合同唯一消费，不新增字段。
    followup = build_reviser_revision_contract(
        source_content=candidate,
        source_version_id=2,
        executable_constraint_plan=_plan(stage="revise"),
        issues=guarded.revise_targets,
        validator_failures=[],
        constraint_results=[],
    )
    assert followup.mode == "localized"


def test_localized_contained_adjacent_long_duplicate_forces_revise() -> None:
    kept = _long("守门人逐项核对刻痕与封蜡，确认所有编号都对应原始登记。")
    target = _long("访客站在门边等待，手里没有触碰任何机关。")
    source = f"{kept}\n\n{target}\n\n末段保持不变。"
    contract = _localized_contract(source, target_anchor_text=target)
    candidate = _localized_candidate(
        contract,
        replacement="值守记录补充如下。" + kept + "核验至此结束。",
    )

    findings = find_localized_adjacent_duplicates(
        contract=contract, candidate_content=candidate
    )
    guarded = apply_objective_reviewer_guards(
        review=_review(version=2),
        chapter_text=candidate,
        revision_contract=contract,
    )

    assert [item.match_kind for item in findings] == ["contained"]
    assert guarded.verdict == "revise"


def test_high_overlap_duplicate_needs_reviewer_signal_and_unique_anchor() -> None:
    kept = "唯一值守开篇标记：" + _long(
        "风扇低鸣隔开门外雨声，守门人坐在冷光下逐项登记。"
    )
    target = _long("访客站在门边等待，手里没有触碰任何机关。")
    expanded = "他接过值守记录。" + kept.replace("守门人", "他", 1)
    source = f"{kept}\n\n{target}\n\n末段保持不变。"
    contract = _localized_contract(source, target_anchor_text=target)
    candidate = _localized_candidate(contract, replacement=expanded)

    assert apply_objective_reviewer_guards(
        review=_review(version=2),
        chapter_text=candidate,
        revision_contract=contract,
    ).verdict == "pass"

    bad_anchor_issue = ReviewIssue(
        paragraph_anchor="不存在的锚点",
        issue_type="prose",
        description="本段与上一段高度重复，形成复读。",
        suggestion="删除重复部分。",
    )
    assert apply_objective_reviewer_guards(
        review=_review(version=2, issues=[bad_anchor_issue]),
        chapter_text=candidate,
        revision_contract=contract,
    ).verdict == "pass"

    issue = bad_anchor_issue.model_copy(
        update={"paragraph_anchor": kept[:30]}
    )
    guarded = apply_objective_reviewer_guards(
        review=_review(version=2, issues=[issue]),
        chapter_text=candidate,
        revision_contract=contract,
    )
    assert guarded.verdict == "revise"
    assert guarded.revise_targets[0].issue_type == "adjacent_duplicate"


def test_meta_narration_double_signal_forces_revise_but_subjective_issue_does_not() -> None:
    text = (
        "雨水沿门框往下淌。\n\n"
        "守门人在上一章就确认了封蜡编号，现在只需核对签名。"
    )
    meta = ReviewIssue(
        paragraph_anchor="守门人在上一章就确认了封蜡编号",
        issue_type="prose",
        description="出现‘上一章’这种元叙事词，打破第四面墙并破坏沉浸感。",
        suggestion="改成‘守门人此前就确认了’。",
    )
    ordinary = ReviewIssue(
        paragraph_anchor="雨水沿门框往下淌",
        issue_type="prose",
        description="雨景可以更有诗意。",
        suggestion="增加一个比喻。",
    )

    guarded = apply_objective_reviewer_guards(
        review=_review(version=1, issues=[meta, ordinary]),
        chapter_text=text,
        revision_contract=None,
    )

    assert guarded.verdict == "revise"
    assert guarded.revise_targets == [meta]


@pytest.mark.parametrize(
    "text",
    [
        "守门人说：“你在上一章里写错了我的名字。”",
        "《值守档案·上一章：旧门登记》",
        "他翻开书中书，标题正是《上一章》。",
        "编辑与作者讨论上一章的章节结构。",
    ],
    ids=["dialogue", "archive-title", "book-within-book", "real-discussion"],
)
def test_meta_term_in_protected_context_does_not_block(text: str) -> None:
    issue = ReviewIssue(
        paragraph_anchor=text[:30],
        issue_type="prose",
        description="‘上一章’像元叙事，会破坏沉浸感。",
        suggestion="删除。",
    )
    guarded = apply_objective_reviewer_guards(
        review=_review(version=1, issues=[issue]),
        chapter_text=text,
        revision_contract=None,
    )
    assert guarded.verdict == "pass"
    assert guarded.revise_targets == []


def test_short_loop_and_non_adjacent_theme_recurrence_do_not_block() -> None:
    short = "滴答。"
    target = _long("访客站在门边等待，手里没有触碰任何机关。")
    distant = _long("守门人逐项核对刻痕与封蜡，确认所有编号都对应原始登记。")
    source = f"{short}\n\n{target}\n\n中间段保持不同。\n\n{distant}"
    contract = _localized_contract(source, target_anchor_text=target)
    candidate = _localized_candidate(contract, replacement=short)

    assert find_localized_adjacent_duplicates(
        contract=contract, candidate_content=candidate
    ) == []
    assert apply_objective_reviewer_guards(
        review=_review(version=2),
        chapter_text=candidate,
        revision_contract=contract,
    ).verdict == "pass"


def test_large_repeated_unchanged_suffix_uses_bounded_paragraph_diff() -> None:
    kept = _long("守门人逐项核对刻痕与封蜡，确认编号对应登记。")
    target = _long("访客站在门边等待，手里没有触碰任何机关。")
    repeated = _long("雨水沿着排水槽持续流向院外，远处钟声保持平稳。")
    source = kept + "\n\n" + target + "\n\n" + "\n\n".join(
        [repeated] * 8_000
    )
    contract = _localized_contract(source, target_anchor_text=target)
    candidate = _localized_candidate(
        contract,
        replacement=kept,
    )

    findings = find_localized_adjacent_duplicates(
        contract=contract,
        candidate_content=candidate,
    )
    assert [item.match_kind for item in findings] == ["exact"]


@pytest.mark.parametrize(
    "surface",
    ["main", "checkpoint_resume", "explicit_product_revise"],
)
def test_all_revision_surfaces_use_the_same_pure_guard(surface: str) -> None:
    if surface == "main":
        source_code = inspect.getsource(
            chapter_orchestrator_module._run_post_review_llm_gates
        )
        serial_source = inspect.getsource(
            chapter_orchestrator_module._apply_must_achieve_validator
        )
        assert source_code.count("guard_must_fail_to_pass(") == 1
        assert serial_source.count("guard_must_fail_to_pass(") == 1
    elif surface == "checkpoint_resume":
        source_code = inspect.getsource(
            chapter_orchestrator_module.orchestrate_chapter
        )
        assert "active_rewrite_checkpoint.review_result" in source_code
        assert "source_review=_source_review_for_revision(" in source_code
    else:
        source_code = inspect.getsource(ChapterService.revise)
        detector_position = source_code.index(
            "find_localized_adjacent_duplicates("
        )
        persistence_position = source_code.index("versioning.write_version(")
        assert detector_position < persistence_position

    source = "守门人仍把凭证收在袖中。　　门外雨声未停。"
    guarded = guard_must_fail_to_pass(
        result=_judge_yes("守门人完成了交接动作"),
        source_review=_review(
            version=1,
            verdict="rewrite",
            constraint_results=[_must_result(status="fail", details="凭证尚未交出")],
        ),
        candidate_text="守门人仍把凭证收在袖中。　　门外雨声变急。",
        candidate_version_id=2,
        revision_contract=_global_contract(source),
        executable_constraint_plan=_plan(),
    )

    assert guarded.passed is False


def test_new_guards_have_no_llm_dispatch_surface() -> None:
    from src.core.rules import minimal_false_pass_guards

    source = inspect.getsource(minimal_false_pass_guards)
    assert ".complete(" not in source
    assert "agent_role" not in source
    assert "ModelRouter" not in source
    assert "LLMClient" not in source

    orchestrator_source = inspect.getsource(chapter_orchestrator_module)
    assert orchestrator_source.count("guard_must_fail_to_pass(") == 2
    assert orchestrator_source.count("apply_objective_reviewer_guards(") == 1


def test_must_prompt_v2_is_exactly_archived_and_v3_increment_is_bounded() -> None:
    active_v2 = (_REPO_ROOT / "prompts/must_achieve_validator_v2.md").read_text()
    archived_v2 = (
        _REPO_ROOT / "prompts/archive/must_achieve_validator_v2.md"
    ).read_text()
    active_v3 = (_REPO_ROOT / "prompts/must_achieve_validator_v3.md").read_text()

    assert archived_v2 == active_v2
    assert "连续逐字引文" in active_v3
    assert "不得概括、推断、解释" in active_v3
    assert len(active_v3) - len(active_v2) == 98
    assert len(active_v3) - len(active_v2) <= 128


@pytest.mark.parametrize("failed_part", ["守门人明确交出", "明确交出"])
def test_compound_must_keeps_repaired_clause_even_when_last_proof_unchanged(failed_part):
    source = "守门人把凭证收在袖中。\n\n通行凭证上盖着铜印。"
    candidate = "守门人取出通行凭证，交到访客手里。\n\n通行凭证上盖着铜印。"
    result = _judge_yes("通行凭证上盖着铜印。")
    constraint_id = result.constraint_results[0].constraint_id
    result = replace(result, coverage_evidence={constraint_id: (
        CoverageEvidence("守门人明确交出", "守门人取出通行凭证，交到访客手里。"),
        CoverageEvidence("通行凭证", "通行凭证上盖着铜印。"),
    )})
    guarded = guard_must_fail_to_pass(
        result=result,
        source_review=_review(version=1, verdict="rewrite", constraint_results=[
            _must_result(status="fail", details=f"未完成分项[{failed_part}]: 凭证尚未交出"),
        ]),
        candidate_text=candidate, candidate_version_id=2,
        revision_contract=_global_contract(source), executable_constraint_plan=_plan(),
    )
    assert guarded.passed and guarded.missing_items == []
    assert guarded.llm_calls_count == 1
    assert guarded.constraint_results[0].details == "守门人取出通行凭证，交到访客手里。"


def test_compound_must_cannot_use_a_changed_unrelated_clause_to_erase_failure():
    source = "守门人把凭证收在袖中。\n\n通行凭证上盖着铜印。"
    candidate = "守门人把凭证收在袖中。\n\n通行凭证上盖着红印。"
    result = _judge_yes("通行凭证上盖着红印。")
    constraint_id = result.constraint_results[0].constraint_id
    result = replace(result, coverage_evidence={constraint_id: (
        CoverageEvidence("守门人明确交出", "守门人把凭证收在袖中。"),
        CoverageEvidence("通行凭证", "通行凭证上盖着红印。"),
    )})
    previous = _must_result(status="fail", details="未完成分项[守门人明确交出]: 凭证尚未交出")
    guarded = guard_must_fail_to_pass(
        result=result, source_review=_review(version=1, verdict="rewrite", constraint_results=[previous]),
        candidate_text=candidate, candidate_version_id=2,
        revision_contract=_global_contract(source), executable_constraint_plan=_plan(),
    )
    assert not guarded.passed and guarded.llm_calls_count == 1
    assert guarded.constraint_results[0].details == previous.details


def test_compound_proof_metadata_drift_fails_closed():
    source = "守门人把凭证收在袖中。"
    candidate = "守门人交出通行凭证。"
    result = _judge_yes(candidate)
    constraint_id = result.constraint_results[0].constraint_id
    result = replace(result, coverage_evidence={constraint_id: (
        CoverageEvidence("被偷偷改写的要求", candidate),
    )})
    with pytest.raises(ProseRevisionContractError, match="分项原文证据发生漂移"):
        guard_must_fail_to_pass(
            result=result, source_review=_review(version=1, verdict="rewrite", constraint_results=[
                _must_result(status="fail", details="未完成分项[守门人明确交出]: 凭证尚未交出"),
            ]), candidate_text=candidate, candidate_version_id=2,
            revision_contract=_global_contract(source), executable_constraint_plan=_plan(),
        )
