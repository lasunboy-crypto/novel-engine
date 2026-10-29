"""正文局部修复保序合同的纯代码回归。"""

from __future__ import annotations

import json
from pathlib import Path
from unittest.mock import MagicMock

import pytest

from src.core.models.chapter_card import ChapterCard, ContinuityContext
from src.core.agents.writer import RewriteConstraintGuardrail, write_chapter
from src.core.chapter_versioning import ChapterVersioning
from src.core.llm.base import LLMClient, LLMResponse, TokenUsage
from src.core.models.review import (
    ConstraintViolation,
    ReviewIssue,
    stable_constraint_id,
)
from src.core.models.setting_contract import SettingContract
from src.core.orchestrator.chapter_orchestrator import (
    RewriteCheckpointContext,
    _PersistedRewriteReviewContext,
    _format_persisted_constraint_violation,
    _partition_mixed_above_max_rewrite,
    orchestrate_chapter,
)
from src.core.rules.prose_hard_contract import (
    ExecutableConstraintPlan,
    compile_executable_constraint_plan,
)
from src.core.rules.prose_revision_contract import (
    ProseRevisionContract,
    ProseRevisionContractError,
    ProseRevisionNoProgressError,
    apply_revision_response,
    build_reviser_revision_contract,
    build_rewrite_revision_contract,
    count_effective_content_rewrites,
    render_revision_contract,
)
from src.core.state_machine.states import ChapterState
from src.core.workspace.workspace_paths import WorkspacePaths


def _card(**overrides: object) -> ChapterCard:
    payload: dict[str, object] = {
        "chapter_id": "ch-0002",
        "workspace_id": "ws-generic",
        "volume_id": "v01",
        "chapter_number": 2,
        "title": "门廊",
        "tension_level": 7,
        "word_count_min": 100,
        "word_count_max": 500,
        "plot_node_budget": 3,
        "must_achieve": ["守门人确认门锁状态"],
        "must_unresolved": ["钥匙的来源"],
        "forbidden": ["本章打开内门"],
        "pov_character": "keeper",
        "setting": "封闭门廊",
        "continuity_context": ContinuityContext(
            prev_chapter_tail="钥匙刚插入锁孔。",
            unfinished_actions=["继续转动钥匙"],
            time_gap="立即续接",
            scene_continuation="same",
            pov_change=False,
            pov_from="keeper",
            pov_to="keeper",
        ),
        "next_chapter_seed": "门后传来脚步声",
        "arc_phase": "rising",
        "is_arc_finale": False,
        "related_plot_threads": [],
        "related_characters": ["keeper"],
    }
    payload.update(overrides)
    return ChapterCard.model_validate(payload)


def _setting() -> SettingContract:
    return SettingContract(
        setting="封闭门廊",
        unique_rules="内门只会在铜印贴近锁面时亮起。",
        source_path="data/outline/world.json",
        source_hash="a" * 64,
    )


def _plan(
    *, stage: str = "rewrite", card: ChapterCard | None = None
) -> ExecutableConstraintPlan:
    return compile_executable_constraint_plan(
        chapter_card=card or _card(),
        stage=stage,  # type: ignore[arg-type]
        setting_contract=_setting(),
    )


def _localized_response(
    contract: ProseRevisionContract,
    *,
    patches: list[dict[str, object]],
    source_version_id: int | None = None,
    source_sha256: str | None = None,
    plan_hash: str | None = None,
) -> str:
    return json.dumps(
        {
            "contract_version": "1",
            "mode": "localized",
            "source_version_id": source_version_id or contract.source_version_id,
            "source_sha256": source_sha256 or contract.source_sha256,
            "plan_hash": plan_hash or contract.plan_hash,
            "patches": patches,
        },
        ensure_ascii=False,
    )


def _continuity_contract(source: str) -> ProseRevisionContract:
    plan = _plan()
    continuity = plan.by_kind("continuity_unfinished_actions")[0]
    preserve = [
        plan.by_kind("must_achieve")[0].as_constraint_result(status="pass"),
        plan.by_kind("forbidden")[0].as_constraint_result(status="pass"),
        plan.by_kind("setting_unique_rules")[0].as_constraint_result(status="pass"),
    ]
    return build_rewrite_revision_contract(
        source_content=source,
        source_version_id=2,
        executable_constraint_plan=plan,
        must_fix=[continuity.as_constraint_result(status="fail")],
        must_preserve=preserve,
        constraint_violations=[],
        other_failures=["continuity"],
        validator_failures=[],
    )


def _setting_contract(source: str) -> ProseRevisionContract:
    plan = _plan()
    item = plan.by_kind("setting_unique_rules")[0]
    evidence = "内门在没有铜印时自行亮起"
    child_rule = "内门只会在铜印贴近锁面时亮起"
    violation = ConstraintViolation(
        constraint_id=stable_constraint_id(
            source_path=item.source_path,
            domain="world.unique_rules",
            rule_text=child_rule,
        ),
        domain="world.unique_rules",
        source_path=item.source_path,
        source_hash=item.source_hash,
        rule_quote=child_rule,
        evidence_quote=evidence,
        severity="hard",
    )
    continuity = plan.by_kind("continuity_unfinished_actions")[0]
    return build_rewrite_revision_contract(
        source_content=source,
        source_version_id=3,
        executable_constraint_plan=plan,
        must_fix=[item.as_constraint_result(status="fail", details=evidence)],
        must_preserve=[continuity.as_constraint_result(status="pass")],
        constraint_violations=[violation],
        other_failures=["setting"],
        validator_failures=[],
    )


def test_continuity_local_patch_mechanically_preserves_all_existing_prose() -> None:
    source = (
        "守门人确认门锁仍旧完好。\n\n"
        "内门只有铜印贴近时才亮起。\n\n"
        "他没有推开内门，只记下门后的脚步声。"
    )
    contract = _continuity_contract(source)

    assert contract.mode == "localized"
    target = contract.targets[0]
    raw = _localized_response(
        contract,
        patches=[
            {
                "target_ids": [target.target_id],
                "anchor_id": target.allowed_anchor_ids[0],
                "operation": "insert_before",
                "replacement": "他接着上一刻的动作，继续缓慢转动钥匙。",
            }
        ],
    )
    candidate = apply_revision_response(contract=contract, raw_content=raw)

    assert candidate.endswith(source)
    assert "内门只有铜印贴近时才亮起。" in candidate
    assert "守门人确认门锁仍旧完好。" in candidate
    assert "他没有推开内门" in candidate


def test_setting_local_patch_preserves_passed_unfinished_action_paragraph() -> None:
    unfinished = "他接着上一刻的动作，继续缓慢转动钥匙。"
    source = (
        f"{unfinished}\n\n"
        "内门在没有铜印时自行亮起，照亮整条走廊。\n\n"
        "守门人退回原位，没有打开内门。"
    )
    contract = _setting_contract(source)
    target = contract.targets[0]
    raw = _localized_response(
        contract,
        patches=[
            {
                "target_ids": [target.target_id],
                "anchor_id": target.allowed_anchor_ids[0],
                "operation": "replace",
                "replacement": "铜印贴近锁面后，内门才泛起微光。",
            }
        ],
    )

    candidate = apply_revision_response(contract=contract, raw_content=raw)

    assert candidate.startswith(unfinished + "\n\n")
    assert candidate.endswith("守门人退回原位，没有打开内门。")
    assert "没有铜印时自行亮起" not in candidate


@pytest.mark.parametrize(
    ("source_paragraph", "replacement"),
    [
        ("守门人确认门锁完好。", "守门人确认门锁完好。"),
        ("守门人确认门锁完好。", "守 门 人 确 认 门 锁 完 好。"),
        ("ＡＢＣ号门锁保持关闭。", "ABC号门锁保持关闭。"),
    ],
)
def test_localized_reviser_rejects_exact_whitespace_and_unicode_noop(
    source_paragraph: str,
    replacement: str,
) -> None:
    source = source_paragraph + "\n\n末段保持不变。"
    plan = _plan(stage="revise")
    issue = ReviewIssue(
        paragraph_anchor=source_paragraph,
        issue_type="prose",
        description="修复该段",
        suggestion="只改锚定段",
    )
    contract = build_reviser_revision_contract(
        source_content=source,
        source_version_id=5,
        executable_constraint_plan=plan,
        issues=[issue],
        validator_failures=[],
        constraint_results=[],
    )
    target = contract.targets[0]
    raw = _localized_response(
        contract,
        patches=[
            {
                "target_ids": [target.target_id],
                "anchor_id": target.allowed_anchor_ids[0],
                "operation": "replace",
                "replacement": replacement,
            }
        ],
    )

    with pytest.raises(ProseRevisionNoProgressError, match="实质变化"):
        apply_revision_response(contract=contract, raw_content=raw)


def test_localized_multi_target_requires_each_target_to_change() -> None:
    first = "第一处需要修复。"
    second = "第二处也需要修复。"
    source = f"{first}\n\n{second}\n\n末段保持不变。"
    plan = _plan(stage="revise")
    issues = [
        ReviewIssue(
            paragraph_anchor=paragraph,
            issue_type="prose",
            description=f"修复{index}",
            suggestion="只改对应锚点",
        )
        for index, paragraph in enumerate((first, second), start=1)
    ]
    contract = build_reviser_revision_contract(
        source_content=source,
        source_version_id=5,
        executable_constraint_plan=plan,
        issues=issues,
        validator_failures=[],
        constraint_results=[],
    )
    anchors = {anchor.anchor_id: anchor for anchor in contract.anchors}
    patches = []
    for index, target in enumerate(contract.targets):
        anchor_id = target.allowed_anchor_ids[0]
        patches.append(
            {
                "target_ids": [target.target_id],
                "anchor_id": anchor_id,
                "operation": "replace",
                "replacement": (
                    anchors[anchor_id].text
                    if index == 0
                    else "第二处已经发生有效变化。"
                ),
            }
        )

    with pytest.raises(ProseRevisionNoProgressError, match="每个授权 target"):
        apply_revision_response(
            contract=contract,
            raw_content=_localized_response(contract, patches=patches),
        )


@pytest.mark.parametrize(
    ("mutate", "message"),
    [
        ("missing_anchor", "未知 anchor"),
        ("duplicate_anchor", "重复修改"),
        ("wrong_version", "源版本"),
        ("wrong_source_hash", "source hash"),
        ("wrong_plan", "plan hash"),
        ("unauthorized_operation", "未授权"),
        ("non_target_anchor", "未授权"),
    ],
)
def test_bad_local_patch_fails_closed_before_candidate_persistence(
    mutate: str, message: str
) -> None:
    source = "守门人确认门锁完好。\n\n他没有打开内门。"
    contract = _continuity_contract(source)
    target = contract.targets[0]
    patch = {
        "target_ids": [target.target_id],
        "anchor_id": target.allowed_anchor_ids[0],
        "operation": "insert_before",
        "replacement": "他继续转动钥匙。",
    }
    patches = [patch]
    kwargs: dict[str, object] = {}
    if mutate == "missing_anchor":
        patch["anchor_id"] = "p9999-aaaaaaaaaaaaaaaa"
    elif mutate == "duplicate_anchor":
        patches = [patch, dict(patch)]
    elif mutate == "wrong_version":
        kwargs["source_version_id"] = contract.source_version_id + 1
    elif mutate == "wrong_source_hash":
        kwargs["source_sha256"] = "c" * 64
    elif mutate == "wrong_plan":
        kwargs["plan_hash"] = "b" * 64
    elif mutate == "unauthorized_operation":
        patch["operation"] = "replace"
    elif mutate == "non_target_anchor":
        patch["anchor_id"] = contract.anchors[1].anchor_id

    raw = _localized_response(contract, patches=patches, **kwargs)  # type: ignore[arg-type]
    with pytest.raises(ProseRevisionContractError, match=message):
        apply_revision_response(contract=contract, raw_content=raw)


def test_local_hard_without_unique_evidence_never_falls_back_to_global() -> None:
    source = "守门人检查外门。\n\n他记录了脚步声。"
    plan = _plan()
    setting = plan.by_kind("setting_unique_rules")[0]

    with pytest.raises(ProseRevisionContractError, match="缺少唯一正文证据锚点"):
        build_rewrite_revision_contract(
            source_content=source,
            source_version_id=2,
            executable_constraint_plan=plan,
            must_fix=[
                setting.as_constraint_result(
                    status="fail",
                    details="正文没有可逐字回查的证据",
                )
            ],
            must_preserve=[],
            constraint_violations=[],
            other_failures=["setting"],
            validator_failures=[],
        )


def test_source_content_version_and_plan_drift_fail_closed() -> None:
    source = "守门人确认门锁完好。\n\n他没有打开内门。"
    contract = _continuity_contract(source)
    plan = _plan()

    with pytest.raises(ProseRevisionContractError, match="source_version_id"):
        contract.assert_compatible(
            source_content=source,
            source_version_id=9,
            executable_constraint_plan=plan,
        )
    with pytest.raises(ProseRevisionContractError, match="source_content"):
        contract.assert_compatible(
            source_content=source + "漂移",
            source_version_id=2,
            executable_constraint_plan=plan,
        )
    changed_plan = _plan(card=_card(word_count_max=600))
    with pytest.raises(ProseRevisionContractError, match="Plan"):
        contract.assert_compatible(
            source_content=source,
            source_version_id=2,
            executable_constraint_plan=changed_plan,
        )


def test_multiple_local_patches_are_order_independent() -> None:
    source = (
        "守门人确认门锁完好。\n\n"
        "内门在没有铜印时自行亮起。\n\n"
        "他没有打开内门。"
    )
    plan = _plan()
    continuity = plan.by_kind("continuity_unfinished_actions")[0]
    setting = plan.by_kind("setting_unique_rules")[0]
    evidence = "内门在没有铜印时自行亮起"
    child_rule = "内门只会在铜印贴近锁面时亮起"
    violation = ConstraintViolation(
        constraint_id=stable_constraint_id(
            source_path=setting.source_path,
            domain="world.unique_rules",
            rule_text=child_rule,
        ),
        domain="world.unique_rules",
        source_path=setting.source_path,
        source_hash=setting.source_hash,
        rule_quote=child_rule,
        evidence_quote=evidence,
        severity="hard",
    )
    contract = build_rewrite_revision_contract(
        source_content=source,
        source_version_id=4,
        executable_constraint_plan=plan,
        must_fix=[
            continuity.as_constraint_result(status="fail"),
            setting.as_constraint_result(status="fail", details=evidence),
        ],
        must_preserve=[],
        constraint_violations=[violation],
        other_failures=["continuity", "setting"],
        validator_failures=[],
    )
    by_kind = {
        plan.result_constraint(target.target_id).kind: target
        for target in contract.targets
    }
    continuity_target = by_kind["continuity_unfinished_actions"]
    setting_target = by_kind["setting_unique_rules"]
    patches = [
        {
            "target_ids": [continuity_target.target_id],
            "anchor_id": continuity_target.allowed_anchor_ids[0],
            "operation": "insert_before",
            "replacement": "他继续转动钥匙。",
        },
        {
            "target_ids": [setting_target.target_id],
            "anchor_id": setting_target.allowed_anchor_ids[0],
            "operation": "replace",
            "replacement": "铜印贴近后，内门才亮起。",
        },
    ]

    forward = apply_revision_response(
        contract=contract,
        raw_content=_localized_response(contract, patches=patches),
    )
    reverse = apply_revision_response(
        contract=contract,
        raw_content=_localized_response(contract, patches=list(reversed(patches))),
    )

    assert forward == reverse


def test_word_count_failure_uses_explicit_global_mode_and_preserves_pass_ids() -> None:
    source = "守门人确认门锁完好。\n\n他没有打开内门。"
    plan = _plan()
    word_count = plan.by_kind("word_count")[0]
    must = plan.by_kind("must_achieve")[0]
    contract = build_rewrite_revision_contract(
        source_content=source,
        source_version_id=2,
        executable_constraint_plan=plan,
        must_fix=[],
        must_preserve=[must.as_constraint_result(status="pass")],
        constraint_violations=[],
        other_failures=["word_count"],
        validator_failures=[{"name": "word_count", "details": {}}],
    )

    assert word_count.kind == "word_count"
    assert contract.mode == "global"
    assert contract.preserve_constraint_ids == (must.constraint_id,)
    revised = source + "\n\n他又核对了一遍锁面。"
    assert apply_revision_response(contract=contract, raw_content=revised) == revised


@pytest.mark.parametrize(
    "candidate",
    [
        "守门人确认门锁完好。\n\n他没有打开内门。",
        "守门人确认门锁完好。\n \n\n他没有打开内门。  ",
    ],
)
def test_global_revision_rejects_content_equivalent_response(candidate: str) -> None:
    source = "守门人确认门锁完好。\n\n他没有打开内门。"
    plan = _plan()
    contract = build_rewrite_revision_contract(
        source_content=source,
        source_version_id=2,
        executable_constraint_plan=plan,
        must_fix=[],
        must_preserve=[],
        constraint_violations=[],
        other_failures=["word_count"],
        validator_failures=[{"name": "word_count", "details": {}}],
    )

    with pytest.raises(ProseRevisionNoProgressError, match="无实质变化"):
        apply_revision_response(contract=contract, raw_content=candidate)

    rendered = render_revision_contract(contract)
    assert "原样返回或只改空格、换行" in rendered
    assert "不是可直接照抄的答案" in rendered


def test_effective_rewrite_budget_excludes_provider_noop_but_keeps_call_evidence() -> None:
    source = "第一稿。"
    rewritten = "第一稿。\n\n真正补上关键事件。"
    versions = [
        ("draft", "qwen3.7-max", source),
        ("rewrite", "qwen3.7-max", rewritten),
        ("rewrite", "qwen3.7-max", rewritten + "\n\n"),
        ("rewrite", "local:paragraph_reflow", rewritten + "\n\n第二段。"),
    ]

    assert len(versions) == 4  # 物理/历史版本证据仍完整保留
    assert (
        count_effective_content_rewrites(
            versions,
            excluded_model_used="local:paragraph_reflow",
        )
        == 1
    )


def test_mixed_above_max_and_semantic_hard_are_staged_semantic_first() -> None:
    source = "冗余" * 300
    plan = _plan()
    word_count = plan.by_kind("word_count")[0]
    must = plan.by_kind("must_achieve")[0]
    word_count_violation = ConstraintViolation(
        constraint_id=word_count.constraint_id,
        domain="chapter.internal",
        source_path=word_count.source_path,
        source_hash=word_count.source_hash,
        rule_quote=word_count.rule_text,
        evidence_quote=(
            '{"actual_word_count":600,"reason":"above_max",'
            '"word_count_max":500,"word_count_min":100}'
        ),
        severity="hard",
        source_chapter_id="ch-0002",
        source_final_version=3,
    )
    guardrail = RewriteConstraintGuardrail(
        must_fix=[
            must.as_constraint_result(status="fail", details="关键事件没有发生"),
        ],
        constraint_violations=[word_count_violation],
        other_failures=[
            "word_count",
            _format_persisted_constraint_violation(word_count_violation),
            "must_achieve",
        ],
    )
    failures = [
        {
            "name": "word_count",
            "details": {
                "reason": "above_max",
                "actual_word_count": len(source),
                "word_count_min": 100,
                "word_count_max": 500,
            },
        }
    ]

    writer_failures, writer_guardrail, staged = (
        _partition_mixed_above_max_rewrite(
            source_content=source,
            validator_failures=failures,
            guardrail=guardrail,
            executable_constraint_plan=plan,
        )
    )

    assert staged is True
    assert writer_failures is None
    assert [item.constraint_id for item in writer_guardrail.must_fix] == [
        must.constraint_id
    ]
    assert writer_guardrail.other_failures == ["must_achieve"]
    assert writer_guardrail.constraint_violations == []
    contract = build_rewrite_revision_contract(
        source_content=source,
        source_version_id=3,
        executable_constraint_plan=plan,
        must_fix=writer_guardrail.must_fix,
        must_preserve=writer_guardrail.must_preserve,
        constraint_violations=writer_guardrail.constraint_violations,
        other_failures=writer_guardrail.other_failures,
        validator_failures=writer_failures or (),
    )
    assert contract.mode == "global"
    assert any("must_achieve" in reason for reason in contract.global_reasons)
    assert not any("word_count" in reason for reason in contract.global_reasons)


def test_mixed_semantic_rewrite_defers_proactive_trim_until_full_review(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    card = _card(word_count_min=100, word_count_max=500)
    source = "冗余" * 300
    paths = WorkspacePaths("ws-generic", workspaces_root=tmp_path / "workspaces")
    paths.root.mkdir(parents=True)
    version = ChapterVersioning(paths).write_version(
        card.chapter_id,
        source,
        creation_type="draft",
        model_used="mock-model",
    )
    plan = compile_executable_constraint_plan(
        chapter_card=card,
        stage="rewrite",
    )
    word_count = plan.by_kind("word_count")[0]
    must = plan.by_kind("must_achieve")[0]
    violation = ConstraintViolation(
        constraint_id=word_count.constraint_id,
        domain="chapter.internal",
        source_path=word_count.source_path,
        source_hash=word_count.source_hash,
        rule_quote=word_count.rule_text,
        evidence_quote=(
            '{"actual_word_count":600,"reason":"above_max",'
            '"word_count_max":500,"word_count_min":100}'
        ),
        severity="hard",
        source_chapter_id=card.chapter_id,
        source_final_version=version.version_id,
    )
    checkpoint = RewriteCheckpointContext(
        version_id=version.version_id,
        source_content=source,
        source_style_fingerprint=version.style_fingerprint or {},
        v1_style_fingerprint=version.style_fingerprint or {},
        review=_PersistedRewriteReviewContext(
            constraint_results=[
                must.as_constraint_result(
                    status="fail", details="关键事件没有发生"
                )
            ],
            constraint_violations=[violation],
            failed_constraints=["word_count", "must_achieve"],
            hard_constraints_passed=False,
            verdict="rewrite",
            score_overall=8.0,
        ),
    )
    monkeypatch.setattr(
        "src.core.orchestrator.chapter_orchestrator.load_rewrite_checkpoint_context",
        lambda *args, **kwargs: checkpoint,
    )
    trim = MagicMock()
    monkeypatch.setattr(
        "src.core.orchestrator.chapter_orchestrator.trim_to_word_count",
        trim,
    )
    client = MagicMock(spec=LLMClient)
    client.complete.side_effect = [
        LLMResponse(
            content="关键事件已经发生。" + "正文" * 300,
            model_used="mock-model",
            usage=TokenUsage(input_tokens=10, output_tokens=10),
            finish_reason="stop",
        ),
        RuntimeError("stop-after-writer"),
    ]

    with pytest.raises(RuntimeError, match="stop-after-writer"):
        orchestrate_chapter(
            chapter_card=card,
            paths=paths,
            memory_section="",
            workspace_id=card.workspace_id,
            client=client,
            model="mock-model",
            initial_state=ChapterState.REWRITING,
            initial_rewrite_count=1,
            initial_last_version_id=version.version_id,
            rewrite_checkpoint_context=checkpoint,
            word_count_trim_enabled=True,
        )

    trim.assert_not_called()
    assert ChapterVersioning(paths).next_version_id(card.chapter_id) == 3


def test_reviser_uses_same_localized_contract_and_exact_issue_anchor() -> None:
    source = "第一段保持不变。\n\n第二段节奏拖沓，需要压缩。\n\n末段保持不变。"
    plan = _plan(stage="revise")
    contract = build_reviser_revision_contract(
        source_content=source,
        source_version_id=5,
        executable_constraint_plan=plan,
        issues=[
            ReviewIssue(
                paragraph_anchor="第二段节奏拖沓",
                issue_type="pacing",
                description="段落节奏过慢",
                suggestion="压缩为一个动作",
            )
        ],
        validator_failures=[],
        constraint_results=[],
    )
    target = contract.targets[0]
    raw = _localized_response(
        contract,
        patches=[
            {
                "target_ids": [target.target_id],
                "anchor_id": target.allowed_anchor_ids[0],
                "operation": "replace",
                "replacement": "第二段只保留一个动作。",
            }
        ],
    )

    candidate = apply_revision_response(contract=contract, raw_content=raw)

    assert candidate.startswith("第一段保持不变。\n\n")
    assert candidate.endswith("\n\n末段保持不变。")


def test_ambiguous_or_missing_local_anchor_fails_before_provider() -> None:
    plan = _plan(stage="revise")
    issue = ReviewIssue(
        paragraph_anchor="重复锚点",
        issue_type="pacing",
        description="压缩",
        suggestion="只留动作",
    )
    with pytest.raises(ProseRevisionContractError, match="未唯一命中"):
        build_reviser_revision_contract(
            source_content="重复锚点在这里。\n\n重复锚点也在这里。",
            source_version_id=2,
            executable_constraint_plan=plan,
            issues=[issue],
            validator_failures=[],
            constraint_results=[],
        )


def test_localized_writer_rewrite_uses_exactly_one_provider_call(
    tmp_path: Path,
) -> None:
    source = "守门人确认门锁完好。\n\n他没有打开内门。"
    contract = _continuity_contract(source)
    target = contract.targets[0]
    raw = _localized_response(
        contract,
        patches=[
            {
                "target_ids": [target.target_id],
                "anchor_id": target.allowed_anchor_ids[0],
                "operation": "insert_before",
                "replacement": "他继续转动钥匙。",
            }
        ],
    )
    client = MagicMock(spec=LLMClient)
    captured_schema: dict[str, object] = {}

    def prepare_schema(**kwargs: object) -> dict[str, object]:
        captured_schema.update(kwargs)
        gen_params = kwargs["gen_params"]
        assert isinstance(gen_params, dict)
        return {**gen_params, "strict_schema_sentinel": True}

    client.prepare_strict_json_schema.side_effect = prepare_schema
    client.complete.return_value = LLMResponse(
        content=raw,
        model_used="mock-model",
        usage=TokenUsage(input_tokens=10, output_tokens=10),
        finish_reason="stop",
    )
    prompts_dir = tmp_path / "prompts"
    prompts_dir.mkdir()
    (prompts_dir / "writer.md").write_text("GENERIC WRITER", encoding="utf-8")

    output = write_chapter(
        chapter_card=_card(),
        memory_section="",
        client=client,
        model="mock-model",
        prompts_dir=prompts_dir,
        creation_type="rewrite",
        executable_constraint_plan=_plan(),
        revision_contract=contract,
    )

    assert client.complete.call_count == 1
    assert client.complete.call_args.kwargs["strict_schema_sentinel"] is True
    assert captured_schema["schema_name"] == "localized_revision"
    assert output.content.endswith(source)
    user_message = client.complete.call_args.kwargs["messages"][1].content
    assert "LOCALIZED PATCH" in user_message


def test_checkpoint_resume_routes_legacy_parallel_card_to_single_writer(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    legacy_payload = _card(word_count_min=1).model_dump(mode="json")
    legacy_payload["draft_strategy"] = "parallel_2"
    card = ChapterCard.model_validate_storage(legacy_payload)
    source = "守门人确认门锁完好。\n\n他没有打开内门。"
    paths = WorkspacePaths("ws-generic", workspaces_root=tmp_path / "workspaces")
    paths.root.mkdir(parents=True)
    versioning = ChapterVersioning(paths)
    version = versioning.write_version(
        card.chapter_id,
        source,
        creation_type="draft",
        model_used="mock-model",
    )
    plan = _plan(card=card)
    continuity = plan.by_kind("continuity_unfinished_actions")[0]
    must = plan.by_kind("must_achieve")[0]
    checkpoint = RewriteCheckpointContext(
        version_id=version.version_id,
        source_content=source,
        source_style_fingerprint=version.style_fingerprint or {},
        v1_style_fingerprint=version.style_fingerprint or {},
        review=_PersistedRewriteReviewContext(
            constraint_results=[
                continuity.as_constraint_result(status="fail"),
                must.as_constraint_result(status="pass"),
            ],
            failed_constraints=["continuity"],
            hard_constraints_passed=False,
            verdict="rewrite",
            score_overall=6.0,
        ),
    )
    monkeypatch.setattr(
        "src.core.orchestrator.chapter_orchestrator.load_rewrite_checkpoint_context",
        lambda *args, **kwargs: checkpoint,
    )
    captured: dict[str, object] = {}

    class _StopAfterContract(Exception):
        pass

    def _capture_writer(**kwargs: object):
        captured.update(kwargs)
        raise _StopAfterContract

    monkeypatch.setattr(
        "src.core.orchestrator.chapter_orchestrator.write_chapter",
        _capture_writer,
    )
    client = MagicMock(spec=LLMClient)

    with pytest.raises(_StopAfterContract):
        orchestrate_chapter(
            chapter_card=card,
            paths=paths,
            memory_section="",
            workspace_id=card.workspace_id,
            client=client,
            model="mock-model",
            initial_state=ChapterState.REWRITING,
            initial_rewrite_count=1,
            initial_last_version_id=version.version_id,
            rewrite_checkpoint_context=checkpoint,
        )

    routed = captured["revision_contract"]
    assert isinstance(routed, ProseRevisionContract)
    assert routed.mode == "localized"
    assert routed.source_version_id == version.version_id
    assert routed.preserve_constraint_ids == (must.constraint_id,)
    client.complete.assert_not_called()
