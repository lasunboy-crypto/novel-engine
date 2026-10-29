"""Source coverage and clause aggregation; no external model calls."""
import json

import pytest

from src.core.agents.must_achieve_validator import (
    MustAchieveTechnicalError,
    _parse_coverage_batch,
    validate_must_achieve,
)
from src.core.llm.base import LLMClient, LLMResponse, TokenUsage


class CoverageClient(LLMClient):
    creative_delta_capability = "creative_delta_v6"

    def __init__(self, payloads):
        self.payloads = iter(payloads)
        self.calls = []
        self.schemas = []

    def prepare_strict_json_schema(self, *, schema, gen_params, **kwargs):
        self.schemas.append(schema)
        return gen_params

    def complete(self, messages, **kwargs):
        self.calls.append(messages)
        return LLMResponse(content=json.dumps(next(self.payloads), ensure_ascii=False),
                           model_used="deepseek-v4-flash", finish_reason="stop",
                           usage=TokenUsage(input_tokens=1, output_tokens=1))


def payload(first="yes", evidence="不批。"):
    return {"results": [{"item_index": 1, "checks": [
        {"requirement_part": "拒绝夜车，", "verdict": first, "evidence": evidence},
        {"requirement_part": "再暗中递图。", "verdict": "yes", "evidence": "地图放入了袋中。"},
    ]}]}


@pytest.mark.parametrize("first,evidence,passed", [
    ("yes", "不批。", True),
    ("no", "没有申请或拒绝夜车", False),
    ("uncertain", "只暗示难以成行", False),
])
def test_same_call_preserves_whole_requirement_and_aggregates_all_clauses(first, evidence, passed):
    client = CoverageClient([payload(first, evidence)])
    result = validate_must_achieve(
        chapter_text="他说：不批。\n另一个场面。\n地图放入了袋中。",
        must_achieve=["拒绝夜车，再暗中递图。"], chapter_id="ch-0002",
        client=client, model="deepseek-v4-flash", authoritative=True,
    )
    assert result.passed is passed
    assert len(client.calls) == 1
    assert "逐项覆盖" in client.calls[0][0].content
    assert "checks" in client.schemas[0]["properties"]["results"]["items"]["properties"]
    assert result.constraint_results[0].rule_text == "拒绝夜车，再暗中递图。"
    if not passed:
        assert result.missing_items == ["拒绝夜车，再暗中递图。"]


def test_omitted_subaction_is_schema_feedback_not_partial_yes():
    incomplete = payload()
    incomplete["results"][0]["checks"].pop(0)
    client = CoverageClient([incomplete, payload("no", "未拒绝夜車")])
    result = validate_must_achieve(
        chapter_text="地图放入了袋中。", must_achieve=["拒绝夜车，再暗中递图。"],
        chapter_id="ch-0002", client=client, model="deepseek-v4-flash", authoritative=True,
    )
    assert not result.passed
    assert len(client.calls) == 2
    assert "完整覆盖原要求" in client.calls[1][1].content


@pytest.mark.parametrize("damage", ["duplicate", "reordered", "paraphrase"])
def test_coverage_cannot_rename_or_drop_requirement(damage):
    value = payload()
    if damage == "duplicate":
        value["results"].append(value["results"][0])
    elif damage == "reordered":
        value["results"][0]["checks"].reverse()
    else:
        value["results"][0]["checks"][0]["requirement_part"] = "交付物资，"
    with pytest.raises(ValueError):
        _parse_coverage_batch(raw_text=json.dumps(value), model_used="test",
            must_achieve=["拒绝夜车，再暗中递图。"], chapter_text="不批。地图放入了袋中。")


def test_required_core_relation_reuses_call_and_original_hard_identity():
    from src.core.agents.must_achieve_validator import RequiredCoreStateEvidence
    from src.core.rules.prose_hard_contract import ExecutableConstraint
    contract = ExecutableConstraint(
        constraint_id="a" * 64, kind="core_state", provenance="core_state_ledger",
        source_path="chapter_cards/ch-0002.core_state_contract", source_hash="b" * 64,
        rule_text="原写前冻结核心状态合同", stages=("draft", "rewrite", "revise"),
        result_domain="setting",
    )
    value = payload()
    value["results"].append({"item_index": 2, "checks": [{
        "requirement_part": "确认箱子由甲授权开启。", "verdict": "uncertain",
        "evidence": "只有房门钥匙证据，未交代箱体授权。",
    }]})
    client = CoverageClient([value])
    result = validate_must_achieve(
        chapter_text="不批。地图放入了袋中。", must_achieve=["拒绝夜车，再暗中递图。"],
        chapter_id="ch-0002", client=client, model="deepseek-v4-flash", authoritative=True,
        required_core_state=RequiredCoreStateEvidence(
            constraint=contract, requirements=("确认箱子由甲授权开启。",), context="箱子=object-2，甲=char-1",
        ),
    )
    assert not result.passed
    assert len(client.calls) == 1
    assert "箱子=object-2" in client.calls[0][1].content
    assert result.missing_items == []
    assert result.constraint_results[0].status == "pass"
    relation = result.constraint_results[1]
    assert relation.status == "fail"
    assert relation.domain == "setting"
    assert relation.constraint_id == contract.constraint_id
    assert relation.source_hash == contract.source_hash
    assert relation.rule_text == contract.rule_text
    # A high-scoring Reviewer and a contradiction-only Setting PASS must not
    # erase the missing positive evidence or permit chapter approval.
    from types import SimpleNamespace
    from src.core.models.review import ReviewResult
    from src.core.orchestrator.chapter_orchestrator import (
        _merge_constraint_results,
        _merge_must_achieve,
    )
    setting_pass = contract.as_constraint_result(status="pass", details="没有直接矛盾")
    review = ReviewResult.model_construct(
        verdict="pass", hard_constraints_passed=True, failed_constraints=[],
        constraint_results=[setting_pass], score_overall=9.0,
    )
    blocked = _merge_must_achieve(
        review=review, result=result,
        chapter_card=SimpleNamespace(chapter_id="ch-0002"), workspace_id="offline",
    )
    blocked = _merge_constraint_results(blocked, [setting_pass])
    assert blocked.verdict == "rewrite" and not blocked.hard_constraints_passed
    assert f"setting: {contract.rule_text}" in blocked.failed_constraints
    assert next(item for item in blocked.constraint_results if item.constraint_id == contract.constraint_id).status == "fail"


def test_core_transition_reason_is_context_not_an_additional_action(monkeypatch):
    from types import SimpleNamespace
    from src.core.agents.must_achieve_validator import ValidationResult
    from src.core.orchestrator import chapter_orchestrator as module

    captured = {}
    def validate(**kwargs):
        captured.update(kwargs)
        return ValidationResult(passed=True)
    monkeypatch.setattr(module, "validate_must_achieve", validate)
    original_core_rule = object()
    plan = SimpleNamespace(
        assert_matches_chapter_card=lambda card: None,
        by_kind=lambda kind: [original_core_rule] if kind == "core_state" else [],
    )
    card = SimpleNamespace(
        chapter_id="ch-0001", must_achieve=[], dramatic_source=None,
        core_state_transitions=[SimpleNamespace(
            entity_id="object-1", entity_type="object", attribute="control",
            to_value="char-1", reason="为下一章运往北站作准备",
        )],
    )
    module._judge_must_achieve(
        chapter_text="甲保管箱体授权。", chapter_card=card, workspace_id="offline",
        router=None, client=None, model=None, executable_constraint_plan=plan,
        character_records=[SimpleNamespace(character_id="char-1", name="甲")],
    )
    evidence = captured["required_core_state"]
    assert evidence.constraint is original_core_rule
    assert evidence.requirements == ("本章正文明确证明object-1（object:object-1）的control为甲。",)
    assert "为下一章运往北站作准备" in evidence.context
    assert "不新增必须发生的动作" in evidence.context


def test_yes_quote_typo_uses_existing_technical_retry_without_false_missing():
    client = CoverageClient([payload(evidence="不批了。"), payload()])
    result = validate_must_achieve(
        chapter_text="他说：不批。地图放入了袋中。",
        must_achieve=["拒绝夜车，再暗中递图。"], chapter_id="ch-0002",
        client=client, model="deepseek-v4-flash", authoritative=True,
    )
    assert result.passed and result.missing_items == []
    assert result.llm_calls_count == 2 and len(client.calls) == 2
    assert "连续逐字原文" in client.calls[1][1].content
    proofs = result.coverage_evidence[result.constraint_results[0].constraint_id]
    assert [proof.requirement_part for proof in proofs] == ["拒绝夜车，", "再暗中递图。"]
    assert [proof.evidence for proof in proofs] == ["不批。", "地图放入了袋中。"]
    # Temporary proof metadata is not a new model field or wire requirement.
    assert set(client.schemas[0]["properties"]["results"]["items"]["properties"]) == {"item_index", "checks"}


def test_repeated_invalid_yes_is_technical_stop_not_a_content_failure():
    client = CoverageClient([payload(evidence="不批了。"), payload(evidence="不批了。")])
    with pytest.raises(MustAchieveTechnicalError) as raised:
        validate_must_achieve(
            chapter_text="不批。地图放入了袋中。",
            must_achieve=["拒绝夜车，再暗中递图。"], chapter_id="ch-0002",
            client=client, model="deepseek-v4-flash", authoritative=True,
        )
    assert raised.value.stage == "parse_or_schema"
    assert raised.value.attempts == 2 and len(client.calls) == 2


def test_real_missing_clause_still_fails_without_evidence_shopping():
    value = payload("no", "没有请求或拒绝的动作。")
    value["results"][0]["checks"][1]["evidence"] = "地图放了入了袋中。"
    client = CoverageClient([value])
    result = validate_must_achieve(
        chapter_text="地图放入了袋中。",
        must_achieve=["拒绝夜车，再暗中递图。"], chapter_id="ch-0002",
        client=client, model="deepseek-v4-flash", authoritative=True,
    )
    assert not result.passed and len(client.calls) == 1
    assert result.coverage_evidence == {}
    assert result.constraint_results[0].details.startswith("未完成分项[拒绝夜车，]")


def test_active_coverage_prompt_distinguishes_background_and_negative_states():
    client = CoverageClient([payload()])
    result = validate_must_achieve(
        chapter_text="不批。地图放入了袋中。",
        must_achieve=["拒绝夜车，再暗中递图。"], chapter_id="ch-0002",
        client=client, model="deepseek-v4-flash", authoritative=True,
    )
    assert result.passed and len(client.calls) == 1
    system = client.calls[0][0].content
    assert "判定 v6" in system
    assert "不凭空假设画外另有获知途径" in system
    assert "明确相反的季节、地点、身份仍应判 no" in system
    assert "正文出现泄密、告知、角色运用秘密行动" in system
    assert "yes 的 evidence 必须是本章正文中一段连续逐字原文" in system


@pytest.mark.parametrize('always_empty', [False, True])
def test_local_wire_empty_answer_uses_only_existing_schema_feedback_attempt(always_empty):
    from src.core.llm.local_json_contract import LocalJsonContract, LocalJsonResponseViolation

    class Client(CoverageClient):
        def prepare_strict_json_schema(self, *, schema_name, schema, gen_params, **kwargs):
            self.contract = LocalJsonContract.build(schema_name, schema)
            return super().prepare_strict_json_schema(schema=schema, gen_params=gen_params)

        def complete(self, messages, **kwargs):
            if not self.calls or always_empty:
                self.calls.append(messages)
                response = LLMResponse(content='', model_used='deepseek-v4-flash',
                                       finish_reason='stop', usage=TokenUsage(input_tokens=1, output_tokens=1))
                raise LocalJsonResponseViolation(response, self.contract, ValueError('empty JSON'))
            return super().complete(messages, **kwargs)

    client = Client([payload()])
    kwargs = dict(chapter_text='不批。地图放入了袋中。', must_achieve=['拒绝夜车，再暗中递图。'],
                  chapter_id='ch-0002', client=client, model='deepseek-v4-flash', authoritative=True)
    if always_empty:
        with pytest.raises(MustAchieveTechnicalError) as error:
            validate_must_achieve(**kwargs)
        assert error.value.stage == 'parse_or_schema' and error.value.attempts == 2
    else:
        assert validate_must_achieve(**kwargs).passed
    assert len(client.calls) == 2
    assert 'empty JSON' in client.calls[1][-1].content
