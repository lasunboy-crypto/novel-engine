"""结构化 Agent 的 strict JSON Schema 组装回归。"""

from __future__ import annotations

import json
from typing import Any

import pytest
from pydantic import BaseModel

from src.core.agents import (
    beat_alignment_validator,
    chapter_card_generator,
    chapter_summary_extractor,
    character_backfill,
    character_disambiguator,
    continuity_validator,
    cross_chapter_dedup_validator,
    ending_tension_validator,
    forbidden_crossing_validator,
    identity_coverage_validator,
    mechanism_consistency_validator,
    memory_extractor,
    memory_health_checker,
    must_achieve_validator,
    must_feasibility_validator,
    must_vs_forbidden_validator,
    outline_builder,
    outline_consistency_validator,
    plot_thread_merger,
    reviewer,
    scene_anchor_validator,
    setting_consistency_validator,
    style_auditor,
    thread_closure_validator,
    timeline_entry_merger,
    volume_seam_replay_validator,
)
from src.core.agents._structured_output import strict_schema_from_model
from src.core.agents.creative_delta import (
    CharactersCreativeDelta,
    PlotCreativeDelta,
    SingleVolumeMiniPlanDelta,
    SingleVolumeRefineDelta,
    VolumePlansCreativeDelta,
)
from src.core.llm.strict_json_schema import (
    Hy3SchemaUnsupportedError,
    compile_hy3_chat_json_schema,
)
from src.core.models.outline import (
    CharactersOutput,
    MiniPlanOutput,
    PlotOutput,
    VolumePlansOutput,
    WorldOutput,
)
from src.core.rules.prose_revision_contract import LocalizedRevisionResponse


class _NestedItem(BaseModel):
    value: str


class _NestedPayload(BaseModel):
    item: _NestedItem
    labels: dict[str, str]
    optional_note: str | None = None


def _walk_objects(node: Any) -> list[dict[str, Any]]:
    found: list[dict[str, Any]] = []
    if isinstance(node, dict):
        if node.get("type") == "object":
            found.append(node)
        for value in node.values():
            found.extend(_walk_objects(value))
    elif isinstance(node, list):
        for value in node:
            found.extend(_walk_objects(value))
    return found


def test_schema_resolves_refs_and_closes_fixed_objects() -> None:
    schema = strict_schema_from_model(_NestedPayload)
    encoded = json.dumps(schema, ensure_ascii=False)

    assert '"$ref"' not in encoded
    assert '"$defs"' not in encoded
    assert schema["additionalProperties"] is False
    assert schema["properties"]["item"]["additionalProperties"] is False
    assert schema["properties"]["labels"]["additionalProperties"] == {"type": "string"}
    assert schema["properties"]["optional_note"]["type"] == ["string", "null"]


def test_all_production_structured_payloads_can_build_closed_schema() -> None:
    output_models = [
        WorldOutput,
        CharactersOutput,
        CharactersCreativeDelta,
        PlotOutput,
        PlotCreativeDelta,
        VolumePlansOutput,
        VolumePlansCreativeDelta,
        SingleVolumeRefineDelta,
        MiniPlanOutput,
        SingleVolumeMiniPlanDelta,
        outline_builder._SingleVolumeMiniPlanOutput,
        LocalizedRevisionResponse,
        character_backfill.BackfillPayload,
        chapter_card_generator._ChapterCardDecisionPayload,
        chapter_card_generator._ChapterCardCreativeDelta,
        reviewer._ReviewerLLMPayload,
        beat_alignment_validator._ValidatorLLMPayload,
        continuity_validator._ValidatorLLMPayload,
        cross_chapter_dedup_validator._DedupBatchPayload,
        ending_tension_validator._EndingTensionPayload,
        forbidden_crossing_validator._ForbiddenLLMPayload,
        identity_coverage_validator._IdentityCoverageBatchPayload,
        mechanism_consistency_validator._MechanismLLMPayload,
        must_achieve_validator._ValidatorBatchPayload,
        must_feasibility_validator._FeasibilityLLMPayload,
        must_vs_forbidden_validator._ValidatorLLMPayload,
        outline_consistency_validator._OutlineConsistencyLLMPayload,
        scene_anchor_validator._SceneAnchorLLMPayload,
        setting_consistency_validator._SettingLLMPayload,
        style_auditor._StyleAuditorLLMPayload,
        thread_closure_validator._ThreadClosureBatchPayload,
        volume_seam_replay_validator._SeamLLMPayload,
        chapter_summary_extractor._ExtractorLLMPayload,
        chapter_summary_extractor._ExtractorSourceRefPayload,
        memory_extractor._ExtractorLLMPayload,
        memory_extractor._MemoryCreativeDeltaPayload,
        plot_thread_merger._MergerLLMPayload,
        character_disambiguator._DisambiguatorLLMPayload,
        timeline_entry_merger._MergerLLMPayload,
        memory_health_checker._CheckerLLMPayload,
    ]

    for output_model in output_models:
        schema = strict_schema_from_model(output_model)
        encoded = json.dumps(schema, ensure_ascii=False)
        assert '"$ref"' not in encoded, output_model.__name__
        assert '"$defs"' not in encoded, output_model.__name__
        assert '"anyOf"' not in encoded, output_model.__name__
        assert schema["type"] == "object", output_model.__name__
        for object_schema in _walk_objects(schema):
            assert "additionalProperties" in object_schema, output_model.__name__


def test_all_production_structured_payloads_compile_for_exact_hy3_dialect() -> None:
    output_models = [
        WorldOutput,
        CharactersOutput,
        CharactersCreativeDelta,
        PlotOutput,
        PlotCreativeDelta,
        VolumePlansOutput,
        VolumePlansCreativeDelta,
        SingleVolumeRefineDelta,
        MiniPlanOutput,
        SingleVolumeMiniPlanDelta,
        outline_builder._SingleVolumeMiniPlanOutput,
        LocalizedRevisionResponse,
        character_backfill.BackfillPayload,
        chapter_card_generator._ChapterCardDecisionPayload,
        chapter_card_generator._ChapterCardCreativeDelta,
        reviewer._ReviewerLLMPayload,
        beat_alignment_validator._ValidatorLLMPayload,
        continuity_validator._ValidatorLLMPayload,
        cross_chapter_dedup_validator._DedupBatchPayload,
        ending_tension_validator._EndingTensionPayload,
        forbidden_crossing_validator._ForbiddenLLMPayload,
        identity_coverage_validator._IdentityCoverageBatchPayload,
        mechanism_consistency_validator._MechanismLLMPayload,
        must_achieve_validator._ValidatorBatchPayload,
        must_feasibility_validator._FeasibilityLLMPayload,
        must_vs_forbidden_validator._ValidatorLLMPayload,
        outline_consistency_validator._OutlineConsistencyLLMPayload,
        scene_anchor_validator._SceneAnchorLLMPayload,
        setting_consistency_validator._SettingLLMPayload,
        style_auditor._StyleAuditorLLMPayload,
        thread_closure_validator._ThreadClosureBatchPayload,
        volume_seam_replay_validator._SeamLLMPayload,
        chapter_summary_extractor._ExtractorLLMPayload,
        chapter_summary_extractor._ExtractorSourceRefPayload,
        memory_extractor._ExtractorLLMPayload,
        memory_extractor._MemoryCreativeDeltaPayload,
        plot_thread_merger._MergerLLMPayload,
        character_disambiguator._DisambiguatorLLMPayload,
        timeline_entry_merger._MergerLLMPayload,
        memory_health_checker._CheckerLLMPayload,
    ]

    unsupported: dict[str, list[str]] = {}
    for output_model in output_models:
        try:
            compiled = compile_hy3_chat_json_schema(output_model.model_json_schema())
        except Hy3SchemaUnsupportedError as exc:
            unsupported[output_model.__name__] = [
                f"{issue.path}:{issue.keyword}:{issue.reason}" for issue in exc.issues
            ]
            continue
        assert compiled.schema["type"] == "object", output_model.__name__
        assert (
            compiled.canonical_hash
            == compile_hy3_chat_json_schema(
                output_model.model_json_schema()
            ).canonical_hash
        )
        for object_schema in _walk_objects(compiled.schema):
            if "properties" in object_schema:
                assert object_schema["additionalProperties"] is False
            else:
                assert object_schema["additionalProperties"] is not None

    assert unsupported == {}, unsupported


def test_hy3_compiler_reports_losses_and_fails_closed_on_shape_loss() -> None:
    compiled = compile_hy3_chat_json_schema(_NestedPayload.model_json_schema())

    assert (
        compiled.canonical_hash
        == compile_hy3_chat_json_schema(
            _NestedPayload.model_json_schema()
        ).canonical_hash
    )
    assert any(
        issue.action == "dropped" and issue.keyword == "title"
        for issue in compiled.issues
    )
    assert any(
        issue.action == "rewritten" and issue.keyword == "additionalProperties"
        for issue in compiled.issues
    )

    broken = {
        "type": "object",
        "properties": {"known": {"type": "string"}},
        "required": ["missing"],
    }
    with pytest.raises(Hy3SchemaUnsupportedError) as exc_info:
        compile_hy3_chat_json_schema(broken)
    assert exc_info.value.issues[0].weakens_shape is True
    assert "required 引用未知" in exc_info.value.issues[0].reason


def test_hy3_compiler_refuses_ref_sibling_required_override() -> None:
    schema = {
        "$defs": {
            "Fixed": {
                "type": "object",
                "properties": {"value": {"type": "string"}},
                "required": ["value"],
            }
        },
        "type": "object",
        "properties": {
            "nested": {
                "$ref": "#/$defs/Fixed",
                "required": [],
            }
        },
        "required": ["nested"],
    }

    with pytest.raises(Hy3SchemaUnsupportedError) as exc_info:
        compile_hy3_chat_json_schema(schema)

    assert any(
        issue.keyword == "required"
        and issue.weakens_shape is True
        and "$ref" in issue.reason
        for issue in exc_info.value.issues
    )


def test_hy3_compiler_closes_explicitly_open_fixed_object_without_duplicate_loss() -> (
    None
):
    schema = {
        "type": "object",
        "properties": {
            "nested": {
                "anyOf": [
                    {
                        "type": "object",
                        "properties": {"value": {"type": "string"}},
                        "required": ["value"],
                        "additionalProperties": True,
                    },
                    {"type": "null"},
                ]
            }
        },
        "required": ["nested"],
    }

    compiled = compile_hy3_chat_json_schema(schema)

    nested = compiled.schema["properties"]["nested"]
    assert nested["additionalProperties"] is False
    rewrites = [
        (issue.path, issue.keyword, issue.reason)
        for issue in compiled.issues
        if issue.action == "rewritten"
    ]
    assert len(rewrites) == len(set(rewrites))
