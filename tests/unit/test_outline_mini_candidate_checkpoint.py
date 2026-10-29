"""Interrupted validation reuses generation but never inherits a passing verdict."""
import json

import pytest
from pydantic import BaseModel

from src.core.agents._generation_checkpoint import GenerationCheckpointError
from src.core.agents.outline_builder import _run_stage
from src.core.agents.beat_alignment_validator import BeatAlignmentBatchTechnicalError
from src.core.llm.base import LLMClient, LLMResponse, TokenUsage
from src.services.outline_mini_candidate_store import OutlineMiniCandidateStore


class Candidate(BaseModel):
    text: str


class Client(LLMClient):
    def __init__(self):
        self.calls = []

    def complete(self, messages, **params):
        self.calls.append(messages)
        return LLMResponse(content=json.dumps({"text": f"candidate-{len(self.calls)}"}),
            model_used="mock", usage=TokenUsage(input_tokens=10, output_tokens=20))


def run(client, store, check, **overrides):
    return _run_stage(**{ "stage": "mini_plan", "system_text": "plan", "user_text": "source",
        "output_model": Candidate, "router": None, "client": client, "model": "mock",
        "max_tokens": 393216, "temperature": 0.7, "candidate_checkpoint": store,
        "post_validate": check, "non_retryable_exceptions": (BeatAlignmentBatchTechnicalError,),
        **overrides })


def test_technical_interruption_reuses_generation_and_rechecks_every_gate(tmp_path):
    client, store = Client(), OutlineMiniCandidateStore(tmp_path)
    seen = []

    def interrupted(candidate):
        seen.append(candidate.text)
        raise BeatAlignmentBatchTechnicalError("provider 402")

    with pytest.raises(BeatAlignmentBatchTechnicalError):
        run(client, store, interrupted)
    result = run(client, OutlineMiniCandidateStore(tmp_path), lambda candidate: seen.append(candidate.text))
    assert result.text == "candidate-1"
    assert seen == ["candidate-1", "candidate-1"]
    assert len(client.calls) == 1
    assert not (tmp_path / "mini_plan.json").exists()


def test_semantic_failure_cannot_be_reused_as_pass_and_keeps_normal_feedback(tmp_path):
    client, store = Client(), OutlineMiniCandidateStore(tmp_path)

    def check(candidate):
        if candidate.text == "candidate-1":
            raise ValueError("source action missing")

    assert run(client, store, check).text == "candidate-2"
    assert len(client.calls) == 2
    assert "source action missing" in client.calls[1][1].content
    assert run(client, store, check).text == "candidate-2"
    assert len(client.calls) == 2
    # Both paid response evidence remains, only the last one is eligible for rechecking.
    responses = [json.loads(path.read_text()) for path in tmp_path.glob("*.json")]
    assert {r["content"] for r in responses if "content" in r} == {
        '{"text": "candidate-1"}', '{"text": "candidate-2"}'}


@pytest.mark.parametrize("change", [{"user_text": "changed source"},
    {"system_text": "changed template"}, {"max_tokens": 65536}, {"model": "different"}])
def test_source_prompt_and_generation_configuration_drift_never_reuse(tmp_path, change):
    client, store = Client(), OutlineMiniCandidateStore(tmp_path)
    run(client, store, lambda _: None)
    assert run(client, store, lambda _: None, **change).text == "candidate-2"
    assert len(client.calls) == 2


def test_corrupted_response_stops_before_another_paid_request(tmp_path):
    client, store = Client(), OutlineMiniCandidateStore(tmp_path)
    run(client, store, lambda _: None)
    for path in tmp_path.glob("*.json"):
        data = json.loads(path.read_text())
        if "content" in data:
            path.write_text(json.dumps({**data, "content": '{"text":"tampered"}'}))
    with pytest.raises(GenerationCheckpointError, match="损坏"):
        run(client, store, lambda _: None)
    assert len(client.calls) == 1


def test_checkpoint_write_error_cannot_trigger_paid_schema_retry(tmp_path, monkeypatch):
    client, store = Client(), OutlineMiniCandidateStore(tmp_path)

    def broken(*_):
        raise GenerationCheckpointError("disk failure")

    monkeypatch.setattr(store, "store", broken)
    with pytest.raises(GenerationCheckpointError):
        run(client, store, lambda _: None)
    assert len(client.calls) == 1


def test_experimental_local_json_contract_is_fingerprinted_without_type_serialization(tmp_path):
    from src.core.llm.local_json_contract import LOCAL_JSON_CONTRACT_PARAM, LocalJsonContract

    class ContractClient(Client):
        def prepare_strict_json_schema(self, *, model, schema_name, schema, gen_params):
            return {**gen_params, LOCAL_JSON_CONTRACT_PARAM:
                LocalJsonContract.build(schema_name, schema)}

    client, store = ContractClient(), OutlineMiniCandidateStore(tmp_path)
    assert run(client, store, lambda _: None).text == "candidate-1"
    assert run(client, store, lambda _: None).text == "candidate-1"
    assert len(client.calls) == 1
