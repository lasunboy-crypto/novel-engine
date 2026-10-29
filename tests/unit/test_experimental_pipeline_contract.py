"""同一实验管线在不同 Provider 下保留程序合同；全程 fake client。"""

from pathlib import Path
from typing import Literal

import pytest
import yaml
from pydantic import BaseModel

from src.core.agents._structured_output import prepare_strict_json_output
from src.core.agents.creative_delta import (
    CREATIVE_DELTA_CAPABILITY,
    EXPERIMENTAL_PIPELINE_CONTRACT,
    uses_hy3_creative_delta,
    uses_hy3_draft_v3,
    uses_hy3_draft_v4,
    uses_hy3_draft_v5,
)
from src.core.config_loader import load_run_model_manifest_catalog, load_writer_profiles_config
from src.core.llm.base import LLMClient, LLMResponse, Message, TokenUsage
from src.core.llm.local_json_contract import LOCAL_JSON_CONTRACT_PARAM
from src.core.routing.run_model_manifest_router import (
    RunModelManifestLockError,
    build_model_router,
)


class Decision(BaseModel):
    actor_id: Literal["actor-1", "actor-2"]
    steps: list[str]


class FakeClient(LLMClient):
    def __init__(self, model, base_url):
        self._model = model
        self._base_url = base_url
        self.content = '{"actor_id":"actor-1","steps":["模型自己设计的动作"]}'
        self.calls = []

    def complete(self, messages, **kwargs):
        self.calls.append((messages, kwargs))
        return LLMResponse(content=self.content, model_used=self._model, finish_reason="stop",
                           usage=TokenUsage(input_tokens=1, output_tokens=1))


def make_router(tmp_path, candidate="deepseek-v4-flash", pipeline=EXPERIMENTAL_PIPELINE_CONTRACT, structured_mode=None):
    raw = yaml.safe_load(Path("config.yaml").read_text())
    raw["run_model_manifest"]["active"] = candidate
    if candidate == "deepseek-v4-pro" or (candidate == "deepseek-v4-flash" and pipeline is not None):
        filename = "experimental_deepseek_pro_manifest.yaml" if candidate == "deepseek-v4-pro" else "experimental_deepseek_manifest.yaml"
        raw["run_model_manifest"]["manifests"][candidate] = yaml.safe_load(
            (Path("config") / filename).read_text())
    selected = raw["run_model_manifest"]["manifests"][candidate]
    selected["pipeline_contract"] = pipeline
    if structured_mode is not None:
        selected["structured_mode"] = structured_mode
    path = tmp_path / "config.yaml"
    path.write_text(yaml.safe_dump(raw, allow_unicode=True))
    clients = []

    def factory(*, model, base_url, provider):
        result = FakeClient(model, base_url)
        clients.append(result)
        return result

    return build_model_router(load_writer_profiles_config(path), config_path=path,
                              client_factory=factory), path, clients


@pytest.mark.parametrize("candidate", ["hy3", "deepseek-v4-flash", "deepseek-v4-pro", "qwen3.8-27b"])
def test_pipeline_selection_preserves_delta_stages_without_faking_provider(tmp_path, candidate):
    router, _, _ = make_router(tmp_path, candidate)
    target = router.route("writer")
    assert target.profile.model == candidate
    assert target.client.creative_delta_capability == CREATIVE_DELTA_CAPABILITY
    assert all(check(target.client) for check in (
        uses_hy3_creative_delta, uses_hy3_draft_v3, uses_hy3_draft_v4, uses_hy3_draft_v5))


def test_missing_pipeline_keeps_legacy_deepseek_and_serialized_contract(tmp_path):
    router, path, _ = make_router(tmp_path, pipeline=None)
    assert not uses_hy3_creative_delta(router.route("writer").client)
    manifest = load_run_model_manifest_catalog(path).active_manifest
    assert "pipeline_contract" not in manifest.model_dump(mode="json")


def test_pipeline_change_invalidates_same_model_before_any_call(tmp_path):
    router, path, clients = make_router(tmp_path)
    target = router.route("writer")
    raw = yaml.safe_load(path.read_text())
    raw["run_model_manifest"]["manifests"]["deepseek-v4-flash"].pop("pipeline_contract")
    path.write_text(yaml.safe_dump(raw))
    with pytest.raises(RunModelManifestLockError):
        target.client.complete([Message(role="user", content="写作")])
    assert clients[0].calls == []


def prepared(target):
    def transform(schema):
        schema["properties"]["actor_id"]["enum"] = ["actor-1"]
        schema["properties"]["steps"]["minItems"] = 1
        schema["properties"]["steps"]["maxItems"] = 2
    return prepare_strict_json_output(client=target.client, model=target.profile.model,
                                     output_model=Decision, schema_name="test_decision",
                                     complete_kwargs=target.complete_kwargs, transform=transform)


def test_json_object_sees_full_dynamic_schema_and_same_accounting_render(tmp_path):
    router, _, clients = make_router(tmp_path, structured_mode="json_object_local_schema")
    target = router.route("chapter_card_generator")
    params = prepared(target)
    messages = [Message(role="system", content="创作"), Message(role="user", content="已有材料")]
    charged = target.client.messages_for_cost_reserve(messages=messages, gen_params=params)
    response = target.client.complete(messages, **params)
    actual, wire = clients[0].calls[0]
    assert actual == charged
    assert '"actor_id":{"enum":["actor-1"]' in actual[-1].content
    assert '"maxItems":2' in actual[-1].content
    assert wire["response_format"] == {"type": "json_object"}
    assert LOCAL_JSON_CONTRACT_PARAM not in wire
    assert "json_schema" not in wire["response_format"]
    assert response.content == clients[0].content


@pytest.mark.parametrize("content", [
    '{"actor_id":"actor-2","steps":["动作"]}',
    '{"actor_id":"actor-1","steps":[]}',
    '{"actor_id":"actor-1","steps":["a","b","c"]}',
    '{"actor_id":"actor-1","steps":["a"],"invented":true}',
    '正文而非 JSON',
])
def test_invalid_delta_is_rejected_locally_without_repair_or_second_request(tmp_path, content):
    router, _, clients = make_router(tmp_path)
    target = router.route("chapter_card_generator")
    clients[0].content = content
    with pytest.raises(ValueError):
        target.client.complete([Message(role="user", content="材料")], **prepared(target))
    assert len(clients[0].calls) == 1


@pytest.mark.parametrize("candidate,input_price,output_price", [
    ("deepseek-v4-flash", 0.003, 0.009), ("deepseek-v4-pro", 0.009, 0.027),
])
def test_deepseek_all_registered_roles_keep_model_pipeline_and_explicit_prose_policy(tmp_path, candidate, input_price, output_price):
    from src.services.run_model_stage_registry import RUN_MODEL_AGENT_TIER
    router, _, clients = make_router(tmp_path, candidate)
    for role in RUN_MODEL_AGENT_TIER:
        target = router.route(role)
        assert target.profile.model == candidate, role
        assert uses_hy3_creative_delta(target.client), role
        params = target.complete_kwargs
        assert "max_tokens" in params and "max_completion_tokens" not in params
        if role in {"writer", "reviser"}:
            assert params["thinking"] == {"type": "disabled"}
            assert "reasoning_effort" not in params
            assert params["temperature"] == 0.8
        elif candidate == "deepseek-v4-flash" and RUN_MODEL_AGENT_TIER[role] == "medium":
            assert params["thinking"] == {"type": "disabled"}
            assert "reasoning_effort" not in params
            assert params["temperature"] == 0.4
        else:
            assert params["thinking"] == {"type": "enabled"}
            assert "temperature" not in params
        assert target.profile.cost_per_1k_input == input_price
        assert target.profile.cost_per_1k_output == output_price
    assert all(client.calls == [] for client in clients)


def test_prose_policy_is_part_of_frozen_lock(tmp_path):
    router, path, clients = make_router(tmp_path, "deepseek-v4-pro")
    target = router.route("writer")
    raw = yaml.safe_load(path.read_text())
    raw["run_model_manifest"]["manifests"]["deepseek-v4-pro"]["prose_generation"]["temperature"] = 0.9
    path.write_text(yaml.safe_dump(raw))
    with pytest.raises(RunModelManifestLockError):
        target.client.complete([Message(role="user", content="写作")])
    assert clients[0].calls == []


def test_old_pro_calibration_is_not_reused_for_new_writer_contract(tmp_path):
    from src.core.config_loader import load_model_calibration
    router, path, clients = make_router(tmp_path, "deepseek-v4-pro")
    assert router.select_profile("writer").model == "deepseek-v4-pro"
    assert "deepseek-v4-pro" not in load_model_calibration(path)
    assert all(client.calls == [] for client in clients)


def test_matching_calibration_binding_survives_price_changes_but_not_writer_changes(tmp_path):
    from src.core.config_loader import load_model_calibration
    from src.core.models.calibration_binding import writer_calibration_contract_hash
    from src.core.models.run_model_manifest import RunModelManifest
    _, path, _ = make_router(tmp_path, "deepseek-v4-pro")
    raw = yaml.safe_load(path.read_text())
    selected = raw["run_model_manifest"]["manifests"]["deepseek-v4-pro"]
    raw["model_calibration"]["deepseek-v4-pro"]["writer_contract_hash"] = writer_calibration_contract_hash(
        RunModelManifest.model_validate(selected))
    path.write_text(yaml.safe_dump(raw))
    assert "deepseek-v4-pro" in load_model_calibration(path)
    selected["output_price_rmb_per_million"] = 28
    path.write_text(yaml.safe_dump(raw))
    assert "deepseek-v4-pro" in load_model_calibration(path)
    selected["prose_generation"]["temperature"] = 0.9
    path.write_text(yaml.safe_dump(raw))
    assert "deepseek-v4-pro" not in load_model_calibration(path)


def test_prompt_json_preserves_same_instruction_and_rejects_invalid_wire_data(tmp_path):
    from src.core.llm.local_json_contract import LocalJsonResponseViolation

    router, path, _ = make_router(tmp_path, structured_mode="json_object_local_schema")
    original = router.route("outline_builder")
    original_params = prepared(original)
    messages = [Message(role="system", content="Return JSON"), Message(role="user", content="Decide")]
    original_render = original.client.messages_for_cost_reserve(messages, original_params)
    raw = yaml.safe_load(path.read_text())
    raw["run_model_manifest"]["manifests"]["deepseek-v4-flash"]["structured_mode"] = "prompt_json_local_schema"
    path.write_text(yaml.safe_dump(raw))
    clients = []
    def factory(*, model, base_url, provider):
        client = FakeClient(model, base_url)
        clients.append(client)
        return client
    router = build_model_router(load_writer_profiles_config(path), config_path=path, client_factory=factory)
    target = router.route("outline_builder")
    params = prepared(target)
    assert target.client.messages_for_cost_reserve(messages, params) == original_render
    target.client.complete(messages, **params)
    assert "response_format" not in clients[0].calls[0][1]
    assert clients[0].calls[0][1]["thinking"] == {"type": "enabled"}
    clients[0].content = '{"actor_id":"actor-2","steps":["invalid identity"]}'
    with pytest.raises(LocalJsonResponseViolation):
        target.client.complete(messages, **params)
    assert params[LOCAL_JSON_CONTRACT_PARAM].schema_hash == original_params[LOCAL_JSON_CONTRACT_PARAM].schema_hash
