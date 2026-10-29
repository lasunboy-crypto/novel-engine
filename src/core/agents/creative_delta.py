"""Hy3 creative/semantic delta wire contracts and deterministic assemblers.

The public product models remain the persistence and business-validation
authority.  This module only owns the experiment-internal wire shape used by
the exact TokenHub ``hy3`` manifest.  It deliberately contains no genre,
keyword, scene, plot, dialogue, or prose templates: model-authored creative
text is copied byte-for-byte, while the program only assigns stable IDs,
maps bounded references, copies authority, and validates the assembled object.
"""

from __future__ import annotations

from src.core.models.narrative import PhaseDevelopment, ReaderInformation
from src.core.rules.narrative_handoff import assemble_narrative_context
from src.core.models.narrative import NarrativeEventContract

import hashlib
import json
import re
from copy import deepcopy
from dataclasses import dataclass
from typing import Annotated, Any, Callable, Iterable, Literal, TypeVar

from pydantic import ValidateAs, BaseModel, ConfigDict, Field, model_validator

from src.core.llm.base import LLMClient
from src.core.models.character import IdentityFacet
from src.core.models.drama import (
    DramaticBeatContract,
    DramaticEventFields,
    DramaticFunction,
    DramaticInteractionMode,
    DramaticPromiseOutcome,
    LoadBearingCausalRole,
    LoadBearingObjectContract,
    VolumeDramaticPromise,
)
from src.core.models.outline import (
    MAX_INITIAL_CHARACTER_CORE_FACTS,
    ArcPhase,
    CharacterOutlineItem,
    CharacterRole,
    CharactersOutput,
    InitialCharacterCoreFact,
    MiniPlanItem,
    MiniPlanOutput,
    PlotLongThread,
    PlotOutput,
    PlotPhase,
    PlotTurningPoint,
    VolumeArcPlan,
    VolumeBeatPlan,
    VolumePlanItem,
    VolumePlansOutput,
    WorldOutput,
)
from src.core.models.volume import TensionTemplateName

HY3_CREATIVE_DELTA_CAPABILITY = "hy3_creative_delta_v1"
HY3_AUTHORITY_ASSEMBLY_CAPABILITY = "hy3_authority_assembly_v2"
HY3_DRAFT_V3_CAPABILITY = "hy3_writer_draft_v3"
HY3_DRAFT_V4_CAPABILITY = "hy3_writer_draft_v4"
HY3_DRAFT_V5_CAPABILITY = "hy3_writer_draft_v5"
EXPERIMENTAL_PIPELINE_CONTRACT = "creative_delta_v6"
CREATIVE_DELTA_CAPABILITY = "creative_delta_v6"
WRITER_DRAFT_CAPABILITY = "writer_draft_v6"
_SOURCE_BOUNDARY_RE = re.compile(
    r"(?:(?:[。！？!?；;]+|…{2,})[。！？!?；;…]*[”’」』）》】]*)|(?:\r?\n+)"
)

ProvenanceKind = Literal[
    "copied_authority",
    "deterministic_derived",
    "model_delta",
]


class CreativeDeltaAssemblyError(ValueError):
    """A delta cannot be joined to the frozen authority without guessing."""

    def __init__(self, code: str, field_path: str, message: str) -> None:
        self.code = code
        self.field_path = field_path
        super().__init__(f"{code}:{field_path}:{message}")


def canonical_json_bytes(value: Any) -> bytes:
    """Return the stable JSON representation used by all T2 hashes."""

    if isinstance(value, BaseModel):
        value = value.model_dump(mode="json")
    return json.dumps(
        value,
        ensure_ascii=False,
        sort_keys=True,
        separators=(",", ":"),
    ).encode("utf-8")


def canonical_sha256(value: Any) -> str:
    return hashlib.sha256(canonical_json_bytes(value)).hexdigest()


@dataclass(frozen=True, slots=True)
class FrozenAuthority:
    """Immutable hash binding between prompt render and assembly."""

    stage: str
    payload: Any
    source_hash: str

    @classmethod
    def freeze(cls, *, stage: str, payload: Any) -> "FrozenAuthority":
        normalized = (
            payload.model_dump(mode="json")
            if isinstance(payload, BaseModel)
            else payload
        )
        # JSON round-trip prevents a caller mutating the original dict/list after
        # the wire request has been prepared.
        snapshot = json.loads(canonical_json_bytes(normalized))
        return cls(
            stage=stage, payload=snapshot, source_hash=canonical_sha256(snapshot)
        )

    def assert_matches(self, current_payload: Any) -> None:
        current_hash = canonical_sha256(current_payload)
        if current_hash != self.source_hash:
            raise CreativeDeltaAssemblyError(
                "authority_source_drift",
                self.stage,
                f"frozen={self.source_hash},current={current_hash}",
            )


class FieldProvenance(BaseModel):
    """One assembled leaf and its exact source category."""

    model_config = ConfigDict(extra="forbid", frozen=True)

    field_path: str = Field(..., min_length=1)
    source_kind: ProvenanceKind
    source_path: str = Field(..., min_length=1)
    source_hash: str = Field(..., pattern=r"^[0-9a-f]{64}$")
    derivation: str | None = None


@dataclass(frozen=True, slots=True)
class EvidenceSource:
    """One exact immutable text slice selectable by a semantic delta."""

    source_id: str
    start: int
    end: int


def build_evidence_source_catalog(text: str) -> tuple[EvidenceSource, ...]:
    sources: list[EvidenceSource] = []

    def append_span(start: int, end: int) -> None:
        while start < end and text[start].isspace():
            start += 1
        while end > start and text[end - 1].isspace():
            end -= 1
        if start >= end or not any(char.isalnum() for char in text[start:end]):
            return
        sources.append(
            EvidenceSource(
                source_id=f"E{len(sources) + 1:04d}",
                start=start,
                end=end,
            )
        )

    cursor = 0
    for match in _SOURCE_BOUNDARY_RE.finditer(text):
        append_span(cursor, match.end())
        cursor = match.end()
    append_span(cursor, len(text))
    return tuple(sources)


def render_evidence_source_catalog(
    *, text: str, catalog: tuple[EvidenceSource, ...]
) -> str:
    return "\n".join(
        json.dumps(
            {"source_id": source.source_id, "text": text[source.start : source.end]},
            ensure_ascii=False,
        )
        for source in catalog
    )


def resolve_evidence_source(
    *, source_id: str, text: str, catalog: tuple[EvidenceSource, ...], field_path: str
) -> str:
    source = next((item for item in catalog if item.source_id == source_id), None)
    if source is None:
        raise CreativeDeltaAssemblyError(
            "unknown_evidence_source",
            field_path,
            source_id,
        )
    return text[source.start : source.end]


_T = TypeVar("_T", bound=BaseModel)


@dataclass(frozen=True, slots=True)
class AssemblyArtifact:
    """Non-persisted evidence for a deterministic delta join."""

    stage: str
    value: BaseModel
    authority_hash: str
    delta_hash: str
    provenance: tuple[FieldProvenance, ...]

    @property
    def full_leaf_count(self) -> int:
        return len(self.provenance)

    @property
    def model_delta_leaf_count(self) -> int:
        return sum(item.source_kind == "model_delta" for item in self.provenance)

    def assert_complete(self) -> None:
        expected = {
            path for path, _value in _iter_leaves(self.value.model_dump(mode="json"))
        }
        actual = [item.field_path for item in self.provenance]
        if len(actual) != len(set(actual)):
            raise CreativeDeltaAssemblyError(
                "duplicate_provenance",
                self.stage,
                "同一完整字段出现多条 provenance",
            )
        missing = sorted(expected - set(actual))
        extra = sorted(set(actual) - expected)
        if missing or extra:
            raise CreativeDeltaAssemblyError(
                "incomplete_provenance",
                self.stage,
                f"missing={missing},extra={extra}",
            )


def uses_hy3_creative_delta(client: LLMClient) -> bool:
    """兼容旧入口名：新管线能力独立于 Provider，历史 Hy3 能力仍可读。"""

    return (
        getattr(client, "creative_delta_capability", None)
        in {HY3_CREATIVE_DELTA_CAPABILITY, CREATIVE_DELTA_CAPABILITY}
    )


def uses_hy3_authority_assembly(client: LLMClient) -> bool:
    """True only for an exact-Hy3 activity opting into authority assembly v2."""

    return uses_hy3_creative_delta(client) and (
        getattr(client, "authority_assembly_capability", None)
        == HY3_AUTHORITY_ASSEMBLY_CAPABILITY
    )


def uses_hy3_draft_v3(client: LLMClient) -> bool:
    """v3 完整事实视图能力；v4/v5 继承，旧 v1/v2 不隐式升级。"""
    return uses_hy3_creative_delta(client) and (
        getattr(client, "authority_assembly_capability", None)
        in {HY3_DRAFT_V3_CAPABILITY, HY3_DRAFT_V4_CAPABILITY, HY3_DRAFT_V5_CAPABILITY,
            WRITER_DRAFT_CAPABILITY}
    )


def uses_hy3_draft_v4(client: LLMClient) -> bool:
    """v4/v5 启用场景设计交接；历史 v3 请求仍可逐字回放。"""
    return uses_hy3_creative_delta(client) and (
        getattr(client, "authority_assembly_capability", None)
        in {HY3_DRAFT_V4_CAPABILITY, HY3_DRAFT_V5_CAPABILITY, WRITER_DRAFT_CAPABILITY}
    )


def uses_hy3_draft_v5(client: LLMClient) -> bool:
    """v5 限定桥接排序职责，旧版本保持其原有展示。"""
    return uses_hy3_creative_delta(client) and (
        getattr(client, "authority_assembly_capability", None)
        in {HY3_DRAFT_V5_CAPABILITY, WRITER_DRAFT_CAPABILITY}
    )


def uses_experimental_pipeline(client: LLMClient) -> bool:
    """当前显式实验合同；不能仅靠 model/host 命中。"""
    return getattr(client, "creative_delta_capability", None) == CREATIVE_DELTA_CAPABILITY


def _iter_leaves(value: Any, path: str = "") -> list[tuple[str, Any]]:
    if isinstance(value, dict):
        if not value:
            return [(path, {})]
        leaves: list[tuple[str, Any]] = []
        for key, item in value.items():
            child = f"{path}.{key}" if path else str(key)
            leaves.extend(_iter_leaves(item, child))
        return leaves
    if isinstance(value, list):
        if not value:
            return [(path, [])]
        leaves = []
        for index, item in enumerate(value):
            leaves.extend(_iter_leaves(item, f"{path}[{index}]"))
        return leaves
    return [(path, value)]


_ProvenanceClassifier = Callable[[str, Any], tuple[ProvenanceKind, str, str | None]]


def _artifact(
    *,
    stage: str,
    value: BaseModel,
    authority: FrozenAuthority,
    delta: BaseModel,
    classifier: _ProvenanceClassifier,
) -> AssemblyArtifact:
    provenance: list[FieldProvenance] = []
    for path, leaf in _iter_leaves(value.model_dump(mode="json")):
        kind, source_path, derivation = classifier(path, leaf)
        provenance.append(
            FieldProvenance(
                field_path=path,
                source_kind=kind,
                source_path=source_path,
                source_hash=canonical_sha256(leaf),
                derivation=derivation,
            )
        )
    result = AssemblyArtifact(
        stage=stage,
        value=value,
        authority_hash=authority.source_hash,
        delta_hash=canonical_sha256(delta),
        provenance=tuple(provenance),
    )
    result.assert_complete()
    return result


def build_assembly_artifact(
    *,
    stage: str,
    value: BaseModel,
    authority: FrozenAuthority,
    delta: BaseModel,
    classifier: _ProvenanceClassifier,
) -> AssemblyArtifact:
    """Build exhaustive field-level evidence for an agent-local assembler."""

    return _artifact(
        stage=stage,
        value=value,
        authority=authority,
        delta=delta,
        classifier=classifier,
    )


def _require_unique(values: list[Any], *, path: str) -> None:
    if len(values) != len(set(values)):
        raise CreativeDeltaAssemblyError(
            "ambiguous_local_reference",
            path,
            "局部引用或键重复",
        )


def _require_known(value: str, allowed: set[str], *, path: str) -> None:
    if value not in allowed:
        raise CreativeDeltaAssemblyError(
            "unknown_authority_reference",
            path,
            f"{value!r} 不在冻结权威集合",
        )


def _stable_id(prefix: str, *ordinals: int) -> str:
    suffix = "-".join(f"{item:03d}" for item in ordinals)
    return f"{prefix}-{suffix}"


# ---------------------------------------------------------------------------
# Characters
# ---------------------------------------------------------------------------


class CharacterRelationshipDelta(BaseModel):
    model_config = ConfigDict(extra="forbid")

    target_slot: int = Field(..., ge=1, le=8)
    description: str = Field(..., min_length=1)


class InitialCoreFactDelta(BaseModel):
    model_config = ConfigDict(extra="forbid")

    attribute: Literal[
        "identity", "life_status", "permanent_injury", "amnesia", "faction"
    ]
    value: str = Field(..., min_length=1, max_length=160)
    source_field: Literal["archetype", "background", "identity_facets"]


class CharacterCreativeDelta(BaseModel):
    model_config = ConfigDict(extra="forbid")

    name: str = Field(..., min_length=1)
    role: CharacterRole
    archetype: str = Field(..., min_length=1)
    appearance: str = Field(..., min_length=1)
    personality: str = Field(..., min_length=1)
    background: str = Field(..., min_length=1)
    motivation: str = Field(..., min_length=1)
    present_desire: str = ""
    personal_stakes: str = ""
    voice_hint: str = Field(..., min_length=1)
    identity_facets: list[IdentityFacet] = Field(...)
    initial_core_facts: list[InitialCoreFactDelta] = Field(..., max_length=2)
    relationships: list[CharacterRelationshipDelta] = Field(...)


class CharactersCreativeDelta(BaseModel):
    model_config = ConfigDict(extra="forbid")

    characters: list[CharacterCreativeDelta] = Field(
        ..., min_length=4, max_length=8,
        description=(
            f"核心人设 4–8 人；所有角色 initial_core_facts 合计最多 "
            f"{MAX_INITIAL_CHARACTER_CORE_FACTS} 条，每人最多 2 条。"
            "由模型选取真正承重的开篇事实；其余人物设定保留在原叙事字段。"
        ),
    )


def assemble_characters_delta(
    *,
    delta: CharactersCreativeDelta,
    authority: FrozenAuthority,
    current_authority: Any,
) -> AssemblyArtifact:
    authority.assert_matches(current_authority)
    total_initial_facts = sum(len(item.initial_core_facts) for item in delta.characters)
    if total_initial_facts > MAX_INITIAL_CHARACTER_CORE_FACTS:
        raise CreativeDeltaAssemblyError(
            "initial_core_fact_budget_exceeded", "characters.initial_core_facts",
            f"全书 initial_core_facts 当前 {total_initial_facts} 条，合计最多 "
            f"{MAX_INITIAL_CHARACTER_CORE_FACTS} 条，每人最多 2 条。"
            "请自行重选真正承重的初始事实；保留全部人物、背景与关系设计，"
            "不删除叙事字段里的事实，未登记的普通设定仍留在原字段。",
        )
    ids = [_stable_id("char", index) for index in range(1, len(delta.characters) + 1)]
    items: list[CharacterOutlineItem] = []
    for index, item in enumerate(delta.characters, start=1):
        relationship_slots = [rel.target_slot for rel in item.relationships]
        _require_unique(
            relationship_slots,
            path=f"characters[{index - 1}].relationships",
        )
        relationships: dict[str, str] = {}
        for rel_index, relationship in enumerate(item.relationships):
            if relationship.target_slot > len(ids):
                raise CreativeDeltaAssemblyError(
                    "unknown_local_reference",
                    f"characters[{index - 1}].relationships[{rel_index}].target_slot",
                    "目标 slot 超出本次角色数组",
                )
            if relationship.target_slot == index:
                raise CreativeDeltaAssemblyError(
                    "authority_conflict",
                    f"characters[{index - 1}].relationships[{rel_index}].target_slot",
                    "角色关系不得指向自身",
                )
            relationships[ids[relationship.target_slot - 1]] = relationship.description

        source_texts = {
            "archetype": item.archetype,
            "background": item.background,
            "identity_facets": "\n".join(
                f"{facet.context}:{facet.identity}" for facet in item.identity_facets
            ),
        }
        facts: list[InitialCharacterCoreFact] = []
        seen_fact_attributes: set[str] = set()
        for fact_index, fact in enumerate(item.initial_core_facts):
            if fact.attribute in seen_fact_attributes:
                raise CreativeDeltaAssemblyError(
                    "duplicate_initial_core_fact_attribute",
                    f"characters[{index - 1}].initial_core_facts[{fact_index}].attribute",
                    f"同一角色的 {fact.attribute!r} 只能登记一条初始事实。"
                    "双重身份可保留在 identity_facets 等叙事字段；"
                    "请由模型修正初始事实登记，不删除人物设定。",
                )
            seen_fact_attributes.add(fact.attribute)
            source = source_texts[fact.source_field]
            if fact.value not in source:
                raise CreativeDeltaAssemblyError(
                    "ungrounded_model_delta",
                    f"characters[{index - 1}].initial_core_facts[{fact_index}].value",
                    f"所选值 {fact.value!r} 未逐字出现在该角色的 {fact.source_field}。"
                    "请从该角色所指字段复制原文；不要替换来源或新增事实凑匹配。"
                    "只登记本人对应属性的开篇状态；没有合适项则用空数组，保留叙事内容。",
                )
            facts.append(
                InitialCharacterCoreFact(
                    attribute=fact.attribute,
                    value=fact.value,
                    source_field=fact.source_field,
                    evidence_quote=fact.value,
                )
            )
        items.append(
            CharacterOutlineItem(
                character_id=ids[index - 1],
                name=item.name,
                role=item.role,
                archetype=item.archetype,
                appearance=item.appearance,
                personality=item.personality,
                background=item.background,
                motivation=item.motivation,
                present_desire=item.present_desire,
                personal_stakes=item.personal_stakes,
                voice_hint=item.voice_hint,
                identity_facets=list(item.identity_facets),
                initial_core_facts=facts,
                relationships_brief=relationships,
            )
        )
    value = CharactersOutput(characters=items)

    def classify(path: str, _leaf: Any) -> tuple[ProvenanceKind, str, str | None]:
        if path.endswith(".character_id"):
            return "deterministic_derived", path, "ordinal_to_stable_id"
        if path.endswith(".evidence_quote"):
            return "deterministic_derived", path, "exact_substring_selection"
        return "model_delta", f"characters_delta.{path}", None

    return _artifact(
        stage="characters",
        value=value,
        authority=authority,
        delta=delta,
        classifier=classify,
    )


# ---------------------------------------------------------------------------
# Plot
# ---------------------------------------------------------------------------


class PlotPhaseDelta(BaseModel):
    model_config = ConfigDict(extra="forbid")

    title: str = Field(..., min_length=1)
    summary: str = Field(..., min_length=1)


class PlotTurningDelta(BaseModel):
    model_config = ConfigDict(extra="forbid")

    title: str = Field(..., min_length=1)
    description: str = Field(..., min_length=1)


class PlotThreadDelta(BaseModel):
    model_config = ConfigDict(extra="forbid")

    title: str = Field(..., min_length=1)
    description: str = Field(..., min_length=1)


class CharacterArcDelta(BaseModel):
    model_config = ConfigDict(extra="forbid")

    character_id: str = Field(..., min_length=1)
    description: str = Field(..., min_length=1)


class LoadBearingObjectDelta(BaseModel):
    model_config = ConfigDict(extra="forbid")

    label: str = Field(..., min_length=1, max_length=80)
    causal_role: LoadBearingCausalRole
    causal_requirement: str = Field(..., min_length=1, max_length=300)
    relation_attributes: list[Literal["possession", "control", "access"]] = Field(
        ..., min_length=1, max_length=3
    )
    linked_turning_slot: int | None = Field(None, ge=1, le=6)
    linked_thread_slot: int | None = Field(None, ge=1, le=6)


class PlotCreativeDelta(BaseModel):
    model_config = ConfigDict(extra="forbid")

    core_conflict: str = Field(..., min_length=1)
    main_phases: list[PlotPhaseDelta] = Field(..., min_length=4, max_length=7)
    major_turning_points: list[PlotTurningDelta] = Field(
        ..., min_length=3, max_length=6
    )
    long_threads: list[PlotThreadDelta] = Field(..., min_length=3, max_length=6)
    character_arcs: list[CharacterArcDelta] = Field(...)
    load_bearing_objects: list[LoadBearingObjectDelta] = Field(..., max_length=12)


class ScopedPlotPhaseDelta(PlotPhaseDelta):
    development: PhaseDevelopment


class ScopedPlotCreativeDelta(BaseModel):
    """Separate response schema keeps the legacy contract and field types unchanged."""

    model_config = ConfigDict(extra="forbid")
    core_conflict: str = Field(..., min_length=1)
    main_phases: list[ScopedPlotPhaseDelta] = Field(..., min_length=4, max_length=7)
    major_turning_points: list[PlotTurningDelta] = Field(..., min_length=3, max_length=6)
    long_threads: list[PlotThreadDelta] = Field(..., min_length=3, max_length=6)
    character_arcs: list[CharacterArcDelta] = Field(...)
    load_bearing_objects: list[LoadBearingObjectDelta] = Field(..., max_length=12)

    @model_validator(mode="after")
    def coherent_space_reference(self) -> ScopedPlotCreativeDelta:
        if sum(phase.development.space_share for phase in self.main_phases) != 100:
            raise ValueError("阶段参考篇幅占比须合计100；仅校验规划资料，不是正文配额")
        return self


def assemble_plot_delta(
    *,
    delta: PlotCreativeDelta | ScopedPlotCreativeDelta,
    characters: CharactersOutput,
    authority: FrozenAuthority,
    current_authority: Any,
) -> AssemblyArtifact:
    authority.assert_matches(current_authority)
    phase_ids = [_stable_id("phase", i) for i in range(1, len(delta.main_phases) + 1)]
    turning_ids = [
        _stable_id("turn", i) for i in range(1, len(delta.major_turning_points) + 1)
    ]
    thread_ids = [
        _stable_id("thread", i) for i in range(1, len(delta.long_threads) + 1)
    ]
    character_ids = {item.character_id for item in characters.characters}
    arc_ids = [item.character_id for item in delta.character_arcs]
    _require_unique(arc_ids, path="plot.character_arcs")
    for index, character_id in enumerate(arc_ids):
        _require_known(
            character_id,
            character_ids,
            path=f"plot.character_arcs[{index}].character_id",
        )

    objects: list[LoadBearingObjectContract] = []
    for index, item in enumerate(delta.load_bearing_objects, start=1):
        linked_turning_id = None
        linked_thread_id = None
        if item.linked_turning_slot is not None:
            if item.linked_turning_slot > len(turning_ids):
                raise CreativeDeltaAssemblyError(
                    "unknown_local_reference",
                    f"plot.load_bearing_objects[{index - 1}].linked_turning_slot",
                    "turning slot 超出数组",
                )
            linked_turning_id = turning_ids[item.linked_turning_slot - 1]
        if item.linked_thread_slot is not None:
            if item.linked_thread_slot > len(thread_ids):
                raise CreativeDeltaAssemblyError(
                    "unknown_local_reference",
                    f"plot.load_bearing_objects[{index - 1}].linked_thread_slot",
                    "thread slot 超出数组",
                )
            linked_thread_id = thread_ids[item.linked_thread_slot - 1]
        if linked_turning_id is None and linked_thread_id is None:
            raise CreativeDeltaAssemblyError(
                "missing_creative_reference",
                f"plot.load_bearing_objects[{index - 1}]",
                "承重物至少选择一个 turning/thread 来源",
            )
        objects.append(
            LoadBearingObjectContract(
                object_id=_stable_id("object", index),
                label=item.label,
                causal_role=item.causal_role,
                causal_requirement=item.causal_requirement,
                relation_attributes=list(item.relation_attributes),
                linked_turning_id=linked_turning_id,
                linked_thread_id=linked_thread_id,
            )
        )

    value = PlotOutput(
        core_conflict=delta.core_conflict,
        main_phases=[
            PlotPhase(phase_id=phase_ids[index], title=item.title, summary=item.summary,
                      development=getattr(item, "development", None))
            for index, item in enumerate(delta.main_phases)
        ],
        major_turning_points=[
            PlotTurningPoint(
                turning_id=turning_ids[index],
                title=item.title,
                description=item.description,
            )
            for index, item in enumerate(delta.major_turning_points)
        ],
        long_threads=[
            PlotLongThread(
                thread_id=thread_ids[index],
                title=item.title,
                description=item.description,
            )
            for index, item in enumerate(delta.long_threads)
        ],
        character_arcs={
            item.character_id: item.description for item in delta.character_arcs
        },
        load_bearing_objects=objects,
    )

    def classify(path: str, _leaf: Any) -> tuple[ProvenanceKind, str, str | None]:
        if re.search(r"\.(?:phase_id|turning_id|thread_id|object_id)$", path):
            # linked_* IDs are mappings of model-selected local slots; object and
            # structural IDs are ordinal derivations.  Both are purely mechanical.
            return "deterministic_derived", path, "local_reference_to_stable_id"
        return "model_delta", f"plot_delta.{path}", None

    return _artifact(
        stage="plot",
        value=value,
        authority=authority,
        delta=delta,
        classifier=classify,
    )


# ---------------------------------------------------------------------------
# Volume build/refine
# ---------------------------------------------------------------------------


class DramaticBeatDelta(DramaticEventFields):
    model_config = ConfigDict(extra="forbid")

    function: DramaticFunction
    actor_id: str = Field(..., min_length=1)
    counterforce_id: str = Field(..., min_length=1)
    interaction_mode: DramaticInteractionMode
    thread_id: str | None = None
    turning_id: str | None = None
    actor_goal: str = Field(..., min_length=1, max_length=300)
    counteraction: str = Field(..., min_length=1, max_length=300)
    choice: str = Field(..., min_length=1, max_length=300)
    consequence: str = Field(..., min_length=1, max_length=300)
    load_bearing_object_ids: list[str] = Field(..., max_length=2)


class VolumePromiseDelta(BaseModel):
    model_config = ConfigDict(extra="forbid")

    primary_thread_id: str | None = None
    primary_turning_id: str | None = None
    actor_id: str = Field(..., min_length=1)
    counterforce_id: str = Field(..., min_length=1)
    outcome: DramaticPromiseOutcome
    deferred_to_volume_id: str | None = None
    deferred_to_thread_id: str | None = None

    @model_validator(mode="after")
    def _validate_outcome_targets(self) -> VolumePromiseDelta:
        """Keep the delta outcome/target pair identical to the product contract."""

        targets = (self.deferred_to_volume_id, self.deferred_to_thread_id)
        if self.outcome == "resolved" and any(value is not None for value in targets):
            raise ValueError("resolved 不得携带 deferred target")
        if self.outcome == "explicitly_deferred" and any(
            value is None for value in targets
        ):
            raise ValueError(
                "explicitly_deferred 必须同时给出后续 volume_id 与既有 thread_id"
            )
        return self


class VolumeBeatCreativeDelta(BaseModel):
    model_config = ConfigDict(extra="forbid")

    chapter_range: tuple[int, int]
    summary: str = Field(..., min_length=1)
    must_advance: list[str] = Field(...)
    forbidden_advance: list[str] = Field(...)
    dramatic_contract: DramaticBeatDelta | None = None


class VolumeArcCreativeDelta(BaseModel):
    model_config = ConfigDict(extra="forbid")

    arc_type: str = Field(..., min_length=1)
    chapter_range: tuple[int, int]
    phase: ArcPhase
    summary: str = Field(..., min_length=1)
    beats: list[VolumeBeatCreativeDelta] = Field(...)


class VolumeCreativeDelta(BaseModel):
    model_config = ConfigDict(extra="forbid")

    title: str = Field(..., min_length=1)
    summary: str = Field(..., min_length=1)
    linked_phases: list[str] = Field(...)
    linked_turning_points: list[str] = Field(...)
    linked_threads: list[str] = Field(...)
    dramatic_promise: VolumePromiseDelta | None = None
    arcs: list[VolumeArcCreativeDelta] = Field(..., min_length=2, max_length=4)
    tension_curve: list[int] = Field(...)


class VolumePlansCreativeDelta(BaseModel):
    model_config = ConfigDict(extra="forbid")

    volumes: list[VolumeCreativeDelta] = Field(..., min_length=1)


# ``VolumePlansCreativeDelta`` above is the immutable T5R1—T5R3 wire model.
# Production exact-Hy3 uses the v4 shape below.  The split is intentional: old
# request/schema hashes remain reproducible while the live route no longer asks
# the model to remember aggregate mechanical obligations from prose alone.


class VolumeBeatCreativeDeltaV4(BaseModel):
    model_config = ConfigDict(extra="forbid")

    chapter_range: tuple[int, int]
    summary: str = Field(..., min_length=1)
    must_advance: list[str] = Field(...)
    forbidden_advance: list[str] = Field(...)


class VolumeArcCreativeDeltaV4(BaseModel):
    model_config = ConfigDict(extra="forbid")

    arc_type: str = Field(..., min_length=1)
    chapter_range: tuple[int, int]
    phase: ArcPhase
    summary: str = Field(..., min_length=1)
    beats: list[VolumeBeatCreativeDeltaV4] = Field(..., min_length=1, max_length=4)


class VolumeDramaticMomentDeltaV4(DramaticEventFields):
    """One optional non-finale creative contract targeting a local beat slot."""

    model_config = ConfigDict(extra="forbid")

    arc_slot: int = Field(..., ge=1, le=4)
    beat_slot: int = Field(..., ge=1, le=4)
    function: Literal["confrontation", "turning", "emotional_peak"]
    actor_id: str = Field(..., min_length=1)
    counterforce_id: str = Field(..., min_length=1)
    interaction_mode: DramaticInteractionMode
    thread_id: str | None = None
    turning_id: str | None = None
    actor_goal: str = Field(..., min_length=1, max_length=300)
    counteraction: str = Field(..., min_length=1, max_length=300)
    actor_choice: str = Field(..., min_length=1, max_length=300)
    consequence: str = Field(..., min_length=1, max_length=300)


class VolumeFinaleCreativeDeltaV4(DramaticEventFields):
    """Creative finale content; identity is copied from the volume promise."""

    model_config = ConfigDict(extra="forbid")

    interaction_mode: DramaticInteractionMode
    actor_goal: str = Field(..., min_length=1, max_length=300)
    counteraction: str = Field(..., min_length=1, max_length=300)
    actor_choice: str = Field(..., min_length=1, max_length=300)
    consequence: str = Field(..., min_length=1, max_length=300)


class VolumeCreativeDeltaV4(BaseModel):
    model_config = ConfigDict(extra="forbid")

    title: str = Field(..., min_length=1)
    summary: str = Field(..., min_length=1)
    linked_phases: list[str] = Field(...)
    linked_turning_points: list[str] = Field(...)
    linked_threads: list[str] = Field(...)
    dramatic_promise: VolumePromiseDelta | None = None
    arcs: list[VolumeArcCreativeDeltaV4] = Field(..., min_length=2, max_length=4)
    dramatic_moments: list[VolumeDramaticMomentDeltaV4] = Field(..., max_length=16)
    finale_contract: VolumeFinaleCreativeDeltaV4 | None = None
    tension_curve: list[int] = Field(...)


class FactualDramaticMomentDelta(VolumeDramaticMomentDeltaV4):
    contract_version: Literal[2] = 2
    required_events: list[str] = Field(min_length=1)


class FactualFinaleDelta(VolumeFinaleCreativeDeltaV4):
    contract_version: Literal[2] = 2
    required_events: list[str] = Field(min_length=1)


class FactualVolumeDelta(VolumeCreativeDeltaV4):
    dramatic_moments: list[Annotated[VolumeDramaticMomentDeltaV4,
        ValidateAs(FactualDramaticMomentDelta, lambda value: value)]] = Field(max_length=16)
    finale_contract: FactualFinaleDelta | None = None


class VolumeObjectPlacementDeltaV4(BaseModel):
    """Select where one frozen plot object participates; never rewrite it."""

    model_config = ConfigDict(extra="forbid")

    object_id: str = Field(..., min_length=1)
    volume_slot: int = Field(..., ge=1)
    target_kind: Literal["finale", "dramatic_moment"]
    dramatic_moment_slot: int | None = Field(None, ge=1, le=16)

    @model_validator(mode="after")
    def _validate_target_shape(self) -> VolumeObjectPlacementDeltaV4:
        if self.target_kind == "finale" and self.dramatic_moment_slot is not None:
            raise ValueError("finale placement 的 dramatic_moment_slot 必须为 null")
        if self.target_kind == "dramatic_moment" and self.dramatic_moment_slot is None:
            raise ValueError("dramatic_moment placement 必须给出目标 slot")
        return self


class VolumePlansCreativeDeltaV4(BaseModel):
    """Required-slot Hy3 volume delta without product-model persistence changes."""

    model_config = ConfigDict(extra="forbid")

    volumes: list[VolumeCreativeDeltaV4] = Field(..., min_length=1)
    object_placements: list[VolumeObjectPlacementDeltaV4] = Field(..., max_length=12)


class FactualVolumePlansDelta(VolumePlansCreativeDeltaV4):
    volumes: list[Annotated[VolumeCreativeDeltaV4,
        ValidateAs(FactualVolumeDelta, lambda value: value)]] = Field(min_length=1)


def build_volume_plans_delta_schema_transform(
    *,
    total_volumes: int,
    allowed_phase_ids: Iterable[str],
    allowed_turning_ids: Iterable[str],
    allowed_thread_ids: Iterable[str],
    allowed_character_ids: Iterable[str],
    allowed_object_ids: Iterable[str],
    require_dramatic_promise: bool,
    bind_nested_authority_refs: bool = True,
) -> Callable[[dict[str, Any]], None]:
    """Bind the Hy3 volume-delta wire schema to frozen outline authority.

    The transform removes the exact T5R1 contradiction: a one-volume plan can
    no longer emit ``explicitly_deferred`` even though no later volume exists.
    Multi-volume schemas expose only later generated volume IDs.  The current
    contract also binds every nested dramatic-contract authority reference;
    ``bind_nested_authority_refs=False`` exists only to reproduce the frozen
    T5R2 request/schema bytes.  Cross-field outcome semantics remain duplicated
    in :class:`VolumePromiseDelta` so a Provider or dialect that weakens a
    nullable field still fails locally.
    """

    if total_volumes < 1:
        raise ValueError("total_volumes must be >= 1")
    phase_ids = sorted(set(allowed_phase_ids))
    turning_ids = sorted(set(allowed_turning_ids))
    thread_ids = sorted(set(allowed_thread_ids))
    character_ids = sorted(set(allowed_character_ids))
    object_ids = sorted(set(allowed_object_ids))
    volume_ids = [_stable_id("vol", index) for index in range(1, total_volumes + 1)]

    def _set_string_enum(field_schema: Any, values: list[str]) -> None:
        if not isinstance(field_schema, dict) or not values:
            return
        field_schema["type"] = "string"
        field_schema["enum"] = values

    def _set_nullable_enum(field_schema: Any, values: list[str]) -> None:
        if not isinstance(field_schema, dict):
            return
        field_schema["type"] = ["string", "null"]
        field_schema["enum"] = [*values, None]

    def _set_null_only(field_schema: Any) -> None:
        if not isinstance(field_schema, dict):
            return
        field_schema["type"] = "null"
        field_schema["enum"] = [None]

    def _set_array_item_enum(field_schema: Any, values: list[str]) -> None:
        if not isinstance(field_schema, dict):
            raise ValueError("volume delta authority array schema missing")
        items_schema = field_schema.get("items")
        if not isinstance(items_schema, dict):
            raise ValueError("volume delta authority array items schema missing")
        if values:
            _set_string_enum(items_schema, values)
        else:
            field_schema["maxItems"] = 0

    def transform(schema: dict[str, Any]) -> None:
        if bind_nested_authority_refs and not character_ids:
            # The closure is constructed for every model route, but only exact
            # Hy3 invokes it.  Keep legacy/mock full-object paths untouched
            # while failing before an exact-Hy3 Provider request can be formed.
            raise ValueError("volume delta 至少需要一个冻结 character ID")
        try:
            volumes_schema = schema["properties"]["volumes"]
            base_volume_schema = volumes_schema["items"]
        except (KeyError, TypeError) as exc:
            raise ValueError(
                "volume delta schema shape drifted before transform"
            ) from exc
        if not isinstance(volumes_schema, dict) or not isinstance(
            base_volume_schema, dict
        ):
            raise ValueError("volume delta volumes/items schema must be objects")

        volumes_schema["minItems"] = total_volumes
        volumes_schema["maxItems"] = total_volumes
        prefix_items: list[dict[str, Any]] = []
        for volume_offset in range(total_volumes):
            volume_schema = deepcopy(base_volume_schema)
            properties = volume_schema.get("properties")
            if not isinstance(properties, dict):
                raise ValueError("volume delta item properties missing")

            for field_name, allowed in (
                ("linked_phases", phase_ids),
                ("linked_turning_points", turning_ids),
                ("linked_threads", thread_ids),
            ):
                field_schema = properties.get(field_name)
                if bind_nested_authority_refs:
                    _set_array_item_enum(field_schema, allowed)
                elif isinstance(field_schema, dict):
                    # Historical T5R2 byte reproduction: empty catalogs were
                    # not collapsed to maxItems=0 in that activity.
                    _set_string_enum(field_schema.get("items"), allowed)

            promise_schema = properties.get("dramatic_promise")
            if not isinstance(promise_schema, dict):
                raise ValueError("volume delta dramatic_promise schema missing")
            promise_properties = promise_schema.get("properties")
            if not isinstance(promise_properties, dict):
                raise ValueError("volume delta promise properties missing")

            _set_nullable_enum(promise_properties.get("primary_thread_id"), thread_ids)
            _set_nullable_enum(
                promise_properties.get("primary_turning_id"), turning_ids
            )
            _set_string_enum(promise_properties.get("actor_id"), character_ids)
            _set_string_enum(promise_properties.get("counterforce_id"), character_ids)

            outcome_schema = promise_properties.get("outcome")
            if not isinstance(outcome_schema, dict):
                raise ValueError("volume delta promise outcome schema missing")
            is_final_volume = volume_offset == total_volumes - 1
            if is_final_volume:
                outcome_schema["enum"] = ["resolved"]
                _set_null_only(promise_properties.get("deferred_to_volume_id"))
                _set_null_only(promise_properties.get("deferred_to_thread_id"))
            else:
                outcome_schema["enum"] = ["resolved", "explicitly_deferred"]
                _set_nullable_enum(
                    promise_properties.get("deferred_to_volume_id"),
                    volume_ids[volume_offset + 1 :],
                )
                _set_nullable_enum(
                    promise_properties.get("deferred_to_thread_id"), thread_ids
                )

            promise_required = list(promise_schema.get("required", []))
            for field_name in (
                "deferred_to_volume_id",
                "deferred_to_thread_id",
            ):
                if field_name not in promise_required:
                    promise_required.append(field_name)
            promise_schema["required"] = promise_required
            if require_dramatic_promise:
                promise_schema["type"] = "object"
                volume_required = list(volume_schema.get("required", []))
                if "dramatic_promise" not in volume_required:
                    volume_required.append("dramatic_promise")
                volume_schema["required"] = volume_required

            if bind_nested_authority_refs:
                arcs_schema = properties.get("arcs")
                if not isinstance(arcs_schema, dict):
                    raise ValueError("volume delta arcs schema missing")
                arc_schema = arcs_schema.get("items")
                if not isinstance(arc_schema, dict):
                    raise ValueError("volume delta arc item schema missing")
                arc_properties = arc_schema.get("properties")
                if not isinstance(arc_properties, dict):
                    raise ValueError("volume delta arc properties missing")
                beats_schema = arc_properties.get("beats")
                if not isinstance(beats_schema, dict):
                    raise ValueError("volume delta beats schema missing")
                beat_schema = beats_schema.get("items")
                if not isinstance(beat_schema, dict):
                    raise ValueError("volume delta beat item schema missing")
                beat_properties = beat_schema.get("properties")
                if not isinstance(beat_properties, dict):
                    raise ValueError("volume delta beat properties missing")
                dramatic_schema = beat_properties.get("dramatic_contract")
                if not isinstance(dramatic_schema, dict):
                    raise ValueError("volume delta beat dramatic schema missing")
                dramatic_properties = dramatic_schema.get("properties")
                if not isinstance(dramatic_properties, dict):
                    raise ValueError("volume delta beat dramatic properties missing")

                _set_string_enum(dramatic_properties.get("actor_id"), character_ids)
                _set_string_enum(
                    dramatic_properties.get("counterforce_id"), character_ids
                )
                _set_nullable_enum(dramatic_properties.get("thread_id"), thread_ids)
                _set_nullable_enum(dramatic_properties.get("turning_id"), turning_ids)
                _set_array_item_enum(
                    dramatic_properties.get("load_bearing_object_ids"),
                    object_ids,
                )
            prefix_items.append(volume_schema)

        volumes_schema["prefixItems"] = prefix_items

    return transform


def build_volume_plans_delta_v4_schema_transform(
    *,
    total_volumes: int,
    allowed_phase_ids: Iterable[str],
    allowed_turning_ids: Iterable[str],
    allowed_thread_ids: Iterable[str],
    allowed_character_ids: Iterable[str],
    allowed_object_ids: Iterable[str],
    require_dramatic_promise: bool,
    selection_short_arc: bool,
) -> Callable[[dict[str, Any]], None]:
    """Freeze T5R4A mechanical slots in the exact-Hy3 wire schema.

    The model still chooses arc/beat ranges and creative content.  The schema
    only makes the existing product shape unavoidable: normal arcs expose
    2—4 beat slots (selection exposes one), a required drama volume exposes one
    finale-content slot, and every frozen plot object exposes one placement row.
    """

    if total_volumes < 1:
        raise ValueError("total_volumes must be >= 1")
    phase_ids = sorted(set(allowed_phase_ids))
    turning_ids = sorted(set(allowed_turning_ids))
    thread_ids = sorted(set(allowed_thread_ids))
    character_ids = sorted(set(allowed_character_ids))
    object_ids = sorted(set(allowed_object_ids))
    volume_ids = [_stable_id("vol", index) for index in range(1, total_volumes + 1)]

    def _set_string_enum(field_schema: Any, values: list[str]) -> None:
        if not isinstance(field_schema, dict):
            raise ValueError("volume delta v4 authority field schema missing")
        if not values:
            raise ValueError("volume delta v4 required authority enum is empty")
        field_schema["type"] = "string"
        field_schema["enum"] = values

    def _set_nullable_enum(field_schema: Any, values: list[str]) -> None:
        if not isinstance(field_schema, dict):
            raise ValueError("volume delta v4 nullable authority schema missing")
        field_schema["type"] = ["string", "null"]
        field_schema["enum"] = [*values, None]

    def _set_null_only(field_schema: Any) -> None:
        if not isinstance(field_schema, dict):
            raise ValueError("volume delta v4 null authority schema missing")
        field_schema["type"] = "null"
        field_schema["enum"] = [None]

    def _set_array_item_enum(field_schema: Any, values: list[str]) -> None:
        if not isinstance(field_schema, dict):
            raise ValueError("volume delta v4 authority array schema missing")
        items_schema = field_schema.get("items")
        if not isinstance(items_schema, dict):
            raise ValueError("volume delta v4 authority array items missing")
        if values:
            _set_string_enum(items_schema, values)
        else:
            field_schema["maxItems"] = 0

    def _bind_dramatic_authority(properties: Any) -> None:
        if not isinstance(properties, dict):
            raise ValueError("volume delta v4 dramatic properties missing")
        _set_string_enum(properties.get("actor_id"), character_ids)
        _set_string_enum(properties.get("counterforce_id"), character_ids)
        _set_nullable_enum(properties.get("thread_id"), thread_ids)
        _set_nullable_enum(properties.get("turning_id"), turning_ids)

    def transform(schema: dict[str, Any]) -> None:
        # The closure is constructed for every provider route, but only exact
        # Hy3 invokes it.  Preserve legacy/mock full-object paths while failing
        # before an exact-Hy3 request can be formed without character authority.
        if not character_ids:
            raise ValueError("volume delta v4 至少需要一个冻结 character ID")
        try:
            root_properties = schema["properties"]
            volumes_schema = root_properties["volumes"]
            base_volume_schema = volumes_schema["items"]
            placements_schema = root_properties["object_placements"]
            base_placement_schema = placements_schema["items"]
        except (KeyError, TypeError) as exc:
            raise ValueError(
                "volume delta v4 schema shape drifted before transform"
            ) from exc
        if not all(
            isinstance(value, dict)
            for value in (
                root_properties,
                volumes_schema,
                base_volume_schema,
                placements_schema,
                base_placement_schema,
            )
        ):
            raise ValueError("volume delta v4 root collections must be objects")

        volumes_schema["minItems"] = total_volumes
        volumes_schema["maxItems"] = total_volumes
        volume_prefix_items: list[dict[str, Any]] = []
        for volume_offset in range(total_volumes):
            volume_schema = deepcopy(base_volume_schema)
            properties = volume_schema.get("properties")
            if not isinstance(properties, dict):
                raise ValueError("volume delta v4 item properties missing")

            for field_name, allowed in (
                ("linked_phases", phase_ids),
                ("linked_turning_points", turning_ids),
                ("linked_threads", thread_ids),
            ):
                _set_array_item_enum(properties.get(field_name), allowed)

            promise_schema = properties.get("dramatic_promise")
            if not isinstance(promise_schema, dict):
                raise ValueError("volume delta v4 dramatic_promise schema missing")
            promise_properties = promise_schema.get("properties")
            if not isinstance(promise_properties, dict):
                raise ValueError("volume delta v4 promise properties missing")
            _set_nullable_enum(promise_properties.get("primary_thread_id"), thread_ids)
            _set_nullable_enum(
                promise_properties.get("primary_turning_id"), turning_ids
            )
            _set_string_enum(promise_properties.get("actor_id"), character_ids)
            _set_string_enum(promise_properties.get("counterforce_id"), character_ids)
            outcome_schema = promise_properties.get("outcome")
            if not isinstance(outcome_schema, dict):
                raise ValueError("volume delta v4 promise outcome schema missing")
            is_final_volume = volume_offset == total_volumes - 1
            if is_final_volume:
                outcome_schema["enum"] = ["resolved"]
                _set_null_only(promise_properties.get("deferred_to_volume_id"))
                _set_null_only(promise_properties.get("deferred_to_thread_id"))
            else:
                outcome_schema["enum"] = ["resolved", "explicitly_deferred"]
                _set_nullable_enum(
                    promise_properties.get("deferred_to_volume_id"),
                    volume_ids[volume_offset + 1 :],
                )
                _set_nullable_enum(
                    promise_properties.get("deferred_to_thread_id"), thread_ids
                )
            promise_required = list(promise_schema.get("required", []))
            for field_name in (
                "deferred_to_volume_id",
                "deferred_to_thread_id",
            ):
                if field_name not in promise_required:
                    promise_required.append(field_name)
            promise_schema["required"] = promise_required

            arcs_schema = properties.get("arcs")
            if not isinstance(arcs_schema, dict):
                raise ValueError("volume delta v4 arcs schema missing")
            arcs_schema["minItems"] = 2
            arcs_schema["maxItems"] = 4
            arc_schema = arcs_schema.get("items")
            arc_properties = (
                arc_schema.get("properties") if isinstance(arc_schema, dict) else None
            )
            if not isinstance(arc_properties, dict):
                raise ValueError("volume delta v4 arc properties missing")
            beats_schema = arc_properties.get("beats")
            if not isinstance(beats_schema, dict):
                raise ValueError("volume delta v4 beats schema missing")
            beats_schema["minItems"] = 1 if selection_short_arc else 2
            beats_schema["maxItems"] = 1 if selection_short_arc else 4

            moments_schema = properties.get("dramatic_moments")
            if not isinstance(moments_schema, dict):
                raise ValueError("volume delta v4 dramatic_moments schema missing")
            moments_schema["maxItems"] = 16
            moment_schema = moments_schema.get("items")
            moment_properties = (
                moment_schema.get("properties")
                if isinstance(moment_schema, dict)
                else None
            )
            _bind_dramatic_authority(moment_properties)

            finale_schema = properties.get("finale_contract")
            if not isinstance(finale_schema, dict) or not isinstance(
                finale_schema.get("properties"), dict
            ):
                raise ValueError("volume delta v4 finale schema missing")

            if require_dramatic_promise:
                promise_schema["type"] = "object"
                finale_schema["type"] = "object"
                volume_required = list(volume_schema.get("required", []))
                for field_name in ("dramatic_promise", "finale_contract"):
                    if field_name not in volume_required:
                        volume_required.append(field_name)
                volume_schema["required"] = volume_required

            volume_prefix_items.append(volume_schema)
        volumes_schema["prefixItems"] = volume_prefix_items

        placements_schema["minItems"] = len(object_ids)
        placements_schema["maxItems"] = len(object_ids)
        placement_prefix_items: list[dict[str, Any]] = []
        for object_id in object_ids:
            placement_schema = deepcopy(base_placement_schema)
            placement_properties = placement_schema.get("properties")
            if not isinstance(placement_properties, dict):
                raise ValueError("volume delta v4 placement properties missing")
            object_schema = placement_properties.get("object_id")
            if not isinstance(object_schema, dict):
                raise ValueError("volume delta v4 placement object_id missing")
            object_schema["type"] = "string"
            object_schema["enum"] = [object_id]
            volume_slot_schema = placement_properties.get("volume_slot")
            if not isinstance(volume_slot_schema, dict):
                raise ValueError("volume delta v4 placement volume_slot missing")
            volume_slot_schema["type"] = "integer"
            volume_slot_schema["enum"] = list(range(1, total_volumes + 1))
            placement_prefix_items.append(placement_schema)
        placements_schema["prefixItems"] = placement_prefix_items

    return transform


def build_volume_plans_delta_v5_schema_transform(
    *,
    total_volumes: int,
    chapters_per_volume: int,
    allowed_phase_ids: Iterable[str],
    allowed_turning_ids: Iterable[str],
    allowed_thread_ids: Iterable[str],
    allowed_character_ids: Iterable[str],
    allowed_object_ids: Iterable[str],
    require_dramatic_promise: bool,
    selection_short_arc: bool,
) -> Callable[[dict[str, Any]], None]:
    """Add chapter-count feasibility to the v4 required-slot wire shape.

    The program closes arithmetic choices that cannot produce non-overlapping
    chapter ranges.  It does not choose an arc type, phase, summary, dramatic
    action, must/forbidden item, or prose.  Normal volumes with more than the
    uniquely constrained four-chapter minimum keep model-owned range choices,
    but impossible arc counts are removed from the wire schema.
    """

    if chapters_per_volume < 1:
        raise ValueError("chapters_per_volume must be >= 1")
    if selection_short_arc:
        if chapters_per_volume != 3:
            raise ValueError(
                "selection_short_arc chapters_per_volume must be exactly 3"
            )
        minimum_beats_per_arc = 1
    else:
        minimum_beats_per_arc = 2
    feasible_arc_max = min(4, chapters_per_volume // minimum_beats_per_arc)
    if feasible_arc_max < 2:
        raise ValueError(
            "chapters_per_volume cannot fit two non-overlapping arcs with "
            f"{minimum_beats_per_arc} beat(s) each"
        )

    base_transform = build_volume_plans_delta_v4_schema_transform(
        total_volumes=total_volumes,
        allowed_phase_ids=allowed_phase_ids,
        allowed_turning_ids=allowed_turning_ids,
        allowed_thread_ids=allowed_thread_ids,
        allowed_character_ids=allowed_character_ids,
        allowed_object_ids=allowed_object_ids,
        require_dramatic_promise=require_dramatic_promise,
        selection_short_arc=selection_short_arc,
    )

    def _freeze_range(field_schema: Any, chapter_range: tuple[int, int]) -> None:
        if not isinstance(field_schema, dict):
            raise ValueError("volume delta v5 chapter_range schema missing")
        prefix_items = field_schema.get("prefixItems")
        if prefix_items is None:
            # The legacy narrow schema intentionally drops tuple annotations;
            # the exact-Hy3 compiler retains them.  Rebuild the same two
            # integer positions so both capability paths see one contract.
            prefix_items = [{"type": "integer"}, {"type": "integer"}]
            field_schema["prefixItems"] = prefix_items
        if not isinstance(prefix_items, list) or len(prefix_items) != 2:
            raise ValueError("volume delta v5 chapter_range tuple shape drifted")
        field_schema["minItems"] = 2
        field_schema["maxItems"] = 2
        for item_schema, value in zip(prefix_items, chapter_range, strict=True):
            if not isinstance(item_schema, dict):
                raise ValueError("volume delta v5 chapter_range item missing")
            item_schema["type"] = "integer"
            item_schema["enum"] = [value]

    def _freeze_string(field_schema: Any, value: str) -> None:
        if not isinstance(field_schema, dict):
            raise ValueError("volume delta v5 exact string schema missing")
        field_schema["type"] = "string"
        field_schema["enum"] = [value]

    def _freeze_geometry(
        *,
        arcs_schema: dict[str, Any],
        arc_ranges: tuple[tuple[int, int], ...],
        beat_ranges: tuple[tuple[tuple[int, int], ...], ...],
        arc_types: tuple[str, ...] | None = None,
        phases: tuple[str, ...] | None = None,
    ) -> None:
        base_arc_schema = arcs_schema.get("items")
        if not isinstance(base_arc_schema, dict):
            raise ValueError("volume delta v5 arc item schema missing")
        if len(arc_ranges) != len(beat_ranges):
            raise ValueError("volume delta v5 geometry length drift")
        if arc_types is not None and len(arc_types) != len(arc_ranges):
            raise ValueError("volume delta v5 arc type geometry drift")
        if phases is not None and len(phases) != len(arc_ranges):
            raise ValueError("volume delta v5 phase geometry drift")

        arc_prefix_items: list[dict[str, Any]] = []
        for index, (arc_range, arc_beat_ranges) in enumerate(
            zip(arc_ranges, beat_ranges, strict=True)
        ):
            arc_schema = deepcopy(base_arc_schema)
            arc_properties = arc_schema.get("properties")
            if not isinstance(arc_properties, dict):
                raise ValueError("volume delta v5 arc properties missing")
            _freeze_range(arc_properties.get("chapter_range"), arc_range)
            if arc_types is not None:
                _freeze_string(arc_properties.get("arc_type"), arc_types[index])
            if phases is not None:
                _freeze_string(arc_properties.get("phase"), phases[index])

            beats_schema = arc_properties.get("beats")
            if not isinstance(beats_schema, dict):
                raise ValueError("volume delta v5 beats schema missing")
            base_beat_schema = beats_schema.get("items")
            if not isinstance(base_beat_schema, dict):
                raise ValueError("volume delta v5 beat item schema missing")
            beat_prefix_items: list[dict[str, Any]] = []
            for beat_range in arc_beat_ranges:
                beat_schema = deepcopy(base_beat_schema)
                beat_properties = beat_schema.get("properties")
                if not isinstance(beat_properties, dict):
                    raise ValueError("volume delta v5 beat properties missing")
                _freeze_range(beat_properties.get("chapter_range"), beat_range)
                beat_prefix_items.append(beat_schema)
            beats_schema["minItems"] = len(beat_prefix_items)
            beats_schema["maxItems"] = len(beat_prefix_items)
            beats_schema["prefixItems"] = beat_prefix_items
            arc_prefix_items.append(arc_schema)

        arcs_schema["minItems"] = len(arc_prefix_items)
        arcs_schema["maxItems"] = len(arc_prefix_items)
        arcs_schema["prefixItems"] = arc_prefix_items

    def transform(schema: dict[str, Any]) -> None:
        base_transform(schema)
        try:
            volume_items = schema["properties"]["volumes"]["prefixItems"]
        except (KeyError, TypeError) as exc:
            raise ValueError("volume delta v5 volume prefix shape drifted") from exc
        if not isinstance(volume_items, list) or len(volume_items) != total_volumes:
            raise ValueError("volume delta v5 volume count shape drifted")

        for volume_schema in volume_items:
            properties = (
                volume_schema.get("properties")
                if isinstance(volume_schema, dict)
                else None
            )
            arcs_schema = (
                properties.get("arcs") if isinstance(properties, dict) else None
            )
            if not isinstance(arcs_schema, dict):
                raise ValueError("volume delta v5 arcs schema missing")
            arcs_schema["maxItems"] = feasible_arc_max

            if selection_short_arc:
                _freeze_geometry(
                    arcs_schema=arcs_schema,
                    arc_ranges=((1, 1), (2, 2), (3, 3)),
                    beat_ranges=(((1, 1),), ((2, 2),), ((3, 3),)),
                    arc_types=("起因", "升级", "高潮/交代"),
                    phases=("opening", "rising", "climax"),
                )
            elif chapters_per_volume == 4:
                _freeze_geometry(
                    arcs_schema=arcs_schema,
                    arc_ranges=((1, 2), (3, 4)),
                    beat_ranges=(
                        ((1, 1), (2, 2)),
                        ((3, 3), (4, 4)),
                    ),
                )

    return transform


def _map_dramatic_contract(
    item: DramaticBeatDelta,
    *,
    characters: set[str],
    thread_ids: set[str],
    turning_ids: set[str],
    object_by_id: dict[str, LoadBearingObjectContract],
    path: str,
) -> DramaticBeatContract:
    for field, value, allowed in (
        ("actor_id", item.actor_id, characters),
        ("counterforce_id", item.counterforce_id, characters),
    ):
        _require_known(value, allowed, path=f"{path}.{field}")
    if item.thread_id is not None:
        _require_known(item.thread_id, thread_ids, path=f"{path}.thread_id")
    if item.turning_id is not None:
        _require_known(item.turning_id, turning_ids, path=f"{path}.turning_id")
    _require_unique(
        item.load_bearing_object_ids, path=f"{path}.load_bearing_object_ids"
    )
    objects: list[LoadBearingObjectContract] = []
    for index, object_id in enumerate(item.load_bearing_object_ids):
        if object_id not in object_by_id:
            raise CreativeDeltaAssemblyError(
                "unknown_authority_reference",
                f"{path}.load_bearing_object_ids[{index}]",
                object_id,
            )
        objects.append(object_by_id[object_id])
    return DramaticBeatContract(
        function=item.function,
        actor_id=item.actor_id,
        counterforce_id=item.counterforce_id,
        interaction_mode=item.interaction_mode,
        thread_id=item.thread_id,
        turning_id=item.turning_id,
        actor_goal=item.actor_goal,
        counteraction=item.counteraction,
        choice=item.choice,
        consequence=item.consequence,
        contract_version=item.contract_version, required_events=item.required_events,
        load_bearing_objects=objects,
    )


def assemble_volume_plans_delta(
    *,
    delta: VolumePlansCreativeDelta,
    characters: CharactersOutput,
    plot: PlotOutput,
    total_volumes: int,
    chapters_per_volume: int,
    tension_template: TensionTemplateName,
    authority: FrozenAuthority,
    current_authority: Any,
) -> AssemblyArtifact:
    authority.assert_matches(current_authority)
    if len(delta.volumes) != total_volumes:
        raise CreativeDeltaAssemblyError(
            "authority_conflict",
            "volume_plans.volumes",
            f"delta={len(delta.volumes)},authority={total_volumes}",
        )
    character_ids = {item.character_id for item in characters.characters}
    phase_ids = {item.phase_id for item in plot.main_phases}
    turning_ids = {item.turning_id for item in plot.major_turning_points}
    thread_ids = {item.thread_id for item in plot.long_threads}
    object_by_id = {item.object_id: item for item in plot.load_bearing_objects}
    volume_ids = [_stable_id("vol", index) for index in range(1, total_volumes + 1)]
    volumes: list[VolumePlanItem] = []
    for volume_index, item in enumerate(delta.volumes, start=1):
        path = f"volume_plans.volumes[{volume_index - 1}]"
        for field_name, values, allowed in (
            ("linked_phases", item.linked_phases, phase_ids),
            ("linked_turning_points", item.linked_turning_points, turning_ids),
            ("linked_threads", item.linked_threads, thread_ids),
        ):
            _require_unique(values, path=f"{path}.{field_name}")
            for ref_index, value in enumerate(values):
                _require_known(value, allowed, path=f"{path}.{field_name}[{ref_index}]")
        if len(item.tension_curve) != chapters_per_volume:
            raise CreativeDeltaAssemblyError(
                "authority_conflict",
                f"{path}.tension_curve",
                "长度必须逐字匹配冻结 chapter_count，禁止 pad/trim",
            )
        if any(
            not isinstance(value, int) or not 1 <= value <= 10
            for value in item.tension_curve
        ):
            raise CreativeDeltaAssemblyError(
                "invalid_model_delta",
                f"{path}.tension_curve",
                "张力必须为 1..10 整数，禁止 clamp",
            )
        promise = None
        if item.dramatic_promise is not None:
            promise_data = item.dramatic_promise.model_dump(mode="json")
            for field_name, allowed in (
                ("primary_thread_id", thread_ids),
                ("deferred_to_thread_id", thread_ids),
                ("primary_turning_id", turning_ids),
                ("actor_id", character_ids),
                ("counterforce_id", character_ids),
            ):
                value = promise_data[field_name]
                if value is not None:
                    _require_known(
                        value, allowed, path=f"{path}.dramatic_promise.{field_name}"
                    )
            if promise_data["deferred_to_volume_id"] is not None:
                _require_known(
                    promise_data["deferred_to_volume_id"],
                    set(volume_ids),
                    path=f"{path}.dramatic_promise.deferred_to_volume_id",
                )
            promise = VolumeDramaticPromise.model_validate(promise_data)

        arcs: list[VolumeArcPlan] = []
        for arc_index, arc in enumerate(item.arcs, start=1):
            beats: list[VolumeBeatPlan] = []
            for beat_index, beat in enumerate(arc.beats, start=1):
                drama = (
                    _map_dramatic_contract(
                        beat.dramatic_contract,
                        characters=character_ids,
                        thread_ids=thread_ids,
                        turning_ids=turning_ids,
                        object_by_id=object_by_id,
                        path=(
                            f"{path}.arcs[{arc_index - 1}].beats[{beat_index - 1}]"
                            ".dramatic_contract"
                        ),
                    )
                    if beat.dramatic_contract is not None
                    else None
                )
                beats.append(
                    VolumeBeatPlan(
                        beat_id=_stable_id("beat", volume_index, arc_index, beat_index),
                        chapter_range=beat.chapter_range,
                        summary=beat.summary,
                        must_advance=list(beat.must_advance),
                        forbidden_advance=list(beat.forbidden_advance),
                        dramatic_contract=drama,
                    )
                )
            arcs.append(
                VolumeArcPlan(
                    arc_id=_stable_id("arc", volume_index, arc_index),
                    arc_type=arc.arc_type,
                    chapter_range=arc.chapter_range,
                    phase=arc.phase,
                    summary=arc.summary,
                    beats=beats,
                )
            )
        volumes.append(
            VolumePlanItem(
                volume_id=volume_ids[volume_index - 1],
                title=item.title,
                summary=item.summary,
                chapter_count=chapters_per_volume,
                tension_template=tension_template,
                linked_phases=list(item.linked_phases),
                linked_turning_points=list(item.linked_turning_points),
                linked_threads=list(item.linked_threads),
                dramatic_promise=promise,
                arcs=arcs,
                tension_curve=list(item.tension_curve),
            )
        )
    assembled_output = VolumePlansOutput(volumes=volumes)

    def classify(path: str, _leaf: Any) -> tuple[ProvenanceKind, str, str | None]:
        if path.endswith((".volume_id", ".chapter_count", ".tension_template")):
            return "copied_authority", path, "new_book_exact_copy"
        if path.endswith((".arc_id", ".beat_id")):
            return "deterministic_derived", path, "ordinal_to_stable_id"
        if ".load_bearing_objects[" in path:
            return "copied_authority", path, "plot_object_exact_copy"
        return "model_delta", f"volume_delta.{path}", None

    return _artifact(
        stage="volume_plans",
        value=assembled_output,
        authority=authority,
        delta=delta,
        classifier=classify,
    )


_VolumeBeatSlotV4 = tuple[int, int, int]


@dataclass(frozen=True, slots=True)
class _PendingDramaticContractV4:
    function: DramaticFunction
    actor_id: str
    counterforce_id: str
    interaction_mode: DramaticInteractionMode
    thread_id: str | None
    turning_id: str | None
    actor_goal: str
    counteraction: str
    actor_choice: str
    consequence: str
    path: str
    contract_version: Literal[1, 2]
    required_events: list[str] | None


def _validate_pending_dramatic_v4(
    pending: _PendingDramaticContractV4,
    *,
    character_ids: set[str],
    thread_ids: set[str],
    turning_ids: set[str],
) -> None:
    for field_name, value, allowed in (
        ("actor_id", pending.actor_id, character_ids),
        ("counterforce_id", pending.counterforce_id, character_ids),
    ):
        _require_known(value, allowed, path=f"{pending.path}.{field_name}")
    if pending.actor_id == pending.counterforce_id:
        raise CreativeDeltaAssemblyError(
            "invalid_model_delta",
            pending.path,
            "actor_id 与 counterforce_id 必须不同",
        )
    if pending.thread_id is not None:
        _require_known(pending.thread_id, thread_ids, path=f"{pending.path}.thread_id")
    if pending.turning_id is not None:
        _require_known(
            pending.turning_id, turning_ids, path=f"{pending.path}.turning_id"
        )
    if pending.thread_id is None and pending.turning_id is None:
        raise CreativeDeltaAssemblyError(
            "missing_creative_reference",
            pending.path,
            "typed dramatic slot 至少选择一个 thread/turning 来源",
        )


def assemble_volume_plans_delta_v4(
    *,
    delta: VolumePlansCreativeDeltaV4,
    characters: CharactersOutput,
    plot: PlotOutput,
    total_volumes: int,
    chapters_per_volume: int,
    tension_template: TensionTemplateName,
    selection_short_arc: bool,
    authority: FrozenAuthority,
    current_authority: Any,
) -> AssemblyArtifact:
    """Join required mechanical slots with model-authored volume creativity.

    This assembler never invents a beat, range, scene, summary, must, or
    dramatic sentence.  It generates stable IDs, locates the unique terminal
    beat selected by the model's ranges, copies finale identity from the
    model-authored promise, and places each frozen plot object at the model's
    explicit local target.
    """

    authority.assert_matches(current_authority)
    if len(delta.volumes) != total_volumes:
        raise CreativeDeltaAssemblyError(
            "authority_conflict",
            "volume_plans_v4.volumes",
            f"delta={len(delta.volumes)},authority={total_volumes}",
        )

    character_ids = {item.character_id for item in characters.characters}
    phase_ids = {item.phase_id for item in plot.main_phases}
    turning_ids = {item.turning_id for item in plot.major_turning_points}
    thread_ids = {item.thread_id for item in plot.long_threads}
    object_by_id = {item.object_id: item for item in plot.load_bearing_objects}
    volume_ids = [_stable_id("vol", index) for index in range(1, total_volumes + 1)]
    expected_beat_min = 1 if selection_short_arc else 2
    expected_beat_max = 1 if selection_short_arc else 4

    promises: list[VolumeDramaticPromise | None] = []
    pending_by_beat: dict[_VolumeBeatSlotV4, _PendingDramaticContractV4] = {}
    finale_target_by_volume: dict[int, _VolumeBeatSlotV4] = {}
    moment_target_by_slot: dict[tuple[int, int], _VolumeBeatSlotV4] = {}

    for volume_index, item in enumerate(delta.volumes, start=1):
        path = f"volume_plans_v4.volumes[{volume_index - 1}]"
        for field_name, values, allowed in (
            ("linked_phases", item.linked_phases, phase_ids),
            ("linked_turning_points", item.linked_turning_points, turning_ids),
            ("linked_threads", item.linked_threads, thread_ids),
        ):
            _require_unique(values, path=f"{path}.{field_name}")
            for ref_index, value in enumerate(values):
                _require_known(
                    value,
                    allowed,
                    path=f"{path}.{field_name}[{ref_index}]",
                )

        if len(item.tension_curve) != chapters_per_volume:
            raise CreativeDeltaAssemblyError(
                "authority_conflict",
                f"{path}.tension_curve",
                "长度必须逐字匹配冻结 chapter_count，禁止 pad/trim",
            )
        if any(
            not isinstance(value, int) or not 1 <= value <= 10
            for value in item.tension_curve
        ):
            raise CreativeDeltaAssemblyError(
                "invalid_model_delta",
                f"{path}.tension_curve",
                "张力必须为 1..10 整数，禁止 clamp",
            )

        promise = None
        if item.dramatic_promise is not None:
            promise_data = item.dramatic_promise.model_dump(mode="json")
            for field_name, allowed in (
                ("primary_thread_id", thread_ids),
                ("deferred_to_thread_id", thread_ids),
                ("primary_turning_id", turning_ids),
                ("actor_id", character_ids),
                ("counterforce_id", character_ids),
            ):
                value = promise_data[field_name]
                if value is not None:
                    _require_known(
                        value,
                        allowed,
                        path=f"{path}.dramatic_promise.{field_name}",
                    )
            if promise_data["deferred_to_volume_id"] is not None:
                _require_known(
                    promise_data["deferred_to_volume_id"],
                    set(volume_ids),
                    path=f"{path}.dramatic_promise.deferred_to_volume_id",
                )
            promise = VolumeDramaticPromise.model_validate(promise_data)
        promises.append(promise)

        terminal_targets: list[_VolumeBeatSlotV4] = []
        beat_slots: set[_VolumeBeatSlotV4] = set()
        for arc_index, arc in enumerate(item.arcs, start=1):
            if not expected_beat_min <= len(arc.beats) <= expected_beat_max:
                raise CreativeDeltaAssemblyError(
                    "invalid_model_delta",
                    f"{path}.arcs[{arc_index - 1}].beats",
                    (
                        f"selection_short_arc={selection_short_arc} 要求 "
                        f"{expected_beat_min}-{expected_beat_max} beats，"
                        f"实际 {len(arc.beats)}"
                    ),
                )
            for beat_index, beat in enumerate(arc.beats, start=1):
                key = (volume_index, arc_index, beat_index)
                beat_slots.add(key)
                if beat.chapter_range[1] == chapters_per_volume:
                    terminal_targets.append(key)

        if (promise is None) != (item.finale_contract is None):
            raise CreativeDeltaAssemblyError(
                "missing_creative_reference",
                f"{path}.finale_contract",
                "dramatic_promise 与 finale_contract 必须成对出现",
            )
        if item.dramatic_moments and promise is None:
            raise CreativeDeltaAssemblyError(
                "missing_creative_reference",
                f"{path}.dramatic_promise",
                "存在 typed dramatic moment 时必须声明卷承诺与 finale",
            )

        if item.finale_contract is not None:
            if len(terminal_targets) != 1:
                raise CreativeDeltaAssemblyError(
                    "ambiguous_local_reference",
                    f"{path}.finale_contract",
                    (
                        "模型 chapter_range 必须提供唯一结束于冻结卷末的 beat；"
                        f"实际 {len(terminal_targets)}"
                    ),
                )
            assert promise is not None
            finale = item.finale_contract
            finale_target = terminal_targets[0]
            pending = _PendingDramaticContractV4(
                function="finale",
                actor_id=promise.actor_id,
                counterforce_id=promise.counterforce_id,
                interaction_mode=finale.interaction_mode,
                thread_id=promise.primary_thread_id,
                turning_id=promise.primary_turning_id,
                actor_goal=finale.actor_goal,
                counteraction=finale.counteraction,
                actor_choice=finale.actor_choice,
                consequence=finale.consequence,
                contract_version=finale.contract_version, required_events=finale.required_events,
                path=f"{path}.finale_contract",
            )
            _validate_pending_dramatic_v4(
                pending,
                character_ids=character_ids,
                thread_ids=thread_ids,
                turning_ids=turning_ids,
            )
            pending_by_beat[finale_target] = pending
            finale_target_by_volume[volume_index] = finale_target

        for moment_index, moment in enumerate(item.dramatic_moments, start=1):
            target = (volume_index, moment.arc_slot, moment.beat_slot)
            moment_path = f"{path}.dramatic_moments[{moment_index - 1}]"
            if target not in beat_slots:
                raise CreativeDeltaAssemblyError(
                    "unknown_local_reference",
                    moment_path,
                    "arc_slot/beat_slot 超出模型本次给出的 beats",
                )
            if target in pending_by_beat:
                raise CreativeDeltaAssemblyError(
                    "ambiguous_local_reference",
                    moment_path,
                    "一个 beat 只能承载一个 typed dramatic contract",
                )
            pending = _PendingDramaticContractV4(
                function=moment.function,
                actor_id=moment.actor_id,
                counterforce_id=moment.counterforce_id,
                interaction_mode=moment.interaction_mode,
                thread_id=moment.thread_id,
                turning_id=moment.turning_id,
                actor_goal=moment.actor_goal,
                counteraction=moment.counteraction,
                actor_choice=moment.actor_choice,
                consequence=moment.consequence,
                contract_version=moment.contract_version, required_events=moment.required_events,
                path=moment_path,
            )
            _validate_pending_dramatic_v4(
                pending,
                character_ids=character_ids,
                thread_ids=thread_ids,
                turning_ids=turning_ids,
            )
            pending_by_beat[target] = pending
            moment_target_by_slot[(volume_index, moment_index)] = target

    expected_object_ids = sorted(object_by_id)
    actual_object_ids = [item.object_id for item in delta.object_placements]
    if actual_object_ids != expected_object_ids:
        raise CreativeDeltaAssemblyError(
            "authority_conflict",
            "volume_plans_v4.object_placements",
            "必须按冻结 catalog 顺序逐对象提供且不得遗漏/重复",
        )

    objects_by_beat: dict[_VolumeBeatSlotV4, list[LoadBearingObjectContract]] = {}
    for placement_index, placement in enumerate(delta.object_placements):
        placement_path = f"volume_plans_v4.object_placements[{placement_index}]"
        source_object = object_by_id[placement.object_id]
        placement_target: _VolumeBeatSlotV4 | None
        if placement.target_kind == "finale":
            placement_target = finale_target_by_volume.get(placement.volume_slot)
        else:
            assert placement.dramatic_moment_slot is not None
            placement_target = moment_target_by_slot.get(
                (placement.volume_slot, placement.dramatic_moment_slot)
            )
        if placement_target is None:
            raise CreativeDeltaAssemblyError(
                "unknown_local_reference",
                placement_path,
                "placement 指向不存在的 finale/dramatic_moment slot",
            )
        pending = pending_by_beat[placement_target]
        same_source = (
            source_object.linked_turning_id is not None
            and source_object.linked_turning_id == pending.turning_id
        ) or (
            source_object.linked_thread_id is not None
            and source_object.linked_thread_id == pending.thread_id
        )
        if not same_source:
            raise CreativeDeltaAssemblyError(
                "object_source_mismatch",
                placement_path,
                (
                    f"{placement.object_id} 与目标 dramatic slot 的 "
                    "turning/thread 不同源"
                ),
            )
        target_objects = objects_by_beat.setdefault(placement_target, [])
        if len(target_objects) >= 2:
            raise CreativeDeltaAssemblyError(
                "invalid_model_delta",
                placement_path,
                "同一 dramatic slot 最多承载 2 个 plot object",
            )
        target_objects.append(source_object)

    dramatic_contract_by_beat: dict[_VolumeBeatSlotV4, DramaticBeatContract] = {}
    for target, pending in pending_by_beat.items():
        dramatic_contract_by_beat[target] = DramaticBeatContract(
            function=pending.function,
            actor_id=pending.actor_id,
            counterforce_id=pending.counterforce_id,
            interaction_mode=pending.interaction_mode,
            thread_id=pending.thread_id,
            turning_id=pending.turning_id,
            actor_goal=pending.actor_goal,
            counteraction=pending.counteraction,
            choice=pending.actor_choice,
            consequence=pending.consequence,
            contract_version=pending.contract_version, required_events=pending.required_events,
            load_bearing_objects=objects_by_beat.get(target, []),
        )

    volumes: list[VolumePlanItem] = []
    for volume_index, item in enumerate(delta.volumes, start=1):
        arcs: list[VolumeArcPlan] = []
        for arc_index, arc in enumerate(item.arcs, start=1):
            beats = [
                VolumeBeatPlan(
                    beat_id=_stable_id("beat", volume_index, arc_index, beat_index),
                    chapter_range=beat.chapter_range,
                    summary=beat.summary,
                    must_advance=list(beat.must_advance),
                    forbidden_advance=list(beat.forbidden_advance),
                    dramatic_contract=dramatic_contract_by_beat.get(
                        (volume_index, arc_index, beat_index)
                    ),
                )
                for beat_index, beat in enumerate(arc.beats, start=1)
            ]
            arcs.append(
                VolumeArcPlan(
                    arc_id=_stable_id("arc", volume_index, arc_index),
                    arc_type=arc.arc_type,
                    chapter_range=arc.chapter_range,
                    phase=arc.phase,
                    summary=arc.summary,
                    beats=beats,
                )
            )
        volumes.append(
            VolumePlanItem(
                volume_id=volume_ids[volume_index - 1],
                title=item.title,
                summary=item.summary,
                chapter_count=chapters_per_volume,
                tension_template=tension_template,
                linked_phases=list(item.linked_phases),
                linked_turning_points=list(item.linked_turning_points),
                linked_threads=list(item.linked_threads),
                dramatic_promise=promises[volume_index - 1],
                arcs=arcs,
                tension_curve=list(item.tension_curve),
            )
        )

    assembled_output = VolumePlansOutput(volumes=volumes)

    def classify(path: str, leaf: Any) -> tuple[ProvenanceKind, str, str | None]:
        if path.endswith((".volume_id", ".chapter_count", ".tension_template")):
            return "copied_authority", path, "new_book_exact_copy"
        if path.endswith((".arc_id", ".beat_id")):
            return "deterministic_derived", path, "ordinal_to_stable_id"
        if path.endswith(".dramatic_contract.function") and leaf == "finale":
            return "deterministic_derived", path, "required_terminal_finale_slot"
        if ".load_bearing_objects[" in path:
            return "copied_authority", path, "plot_object_exact_copy"
        if path.endswith(".dramatic_contract.choice"):
            return "model_delta", f"volume_delta_v4.{path}", "actor_choice_to_choice"
        return "model_delta", f"volume_delta_v4.{path}", None

    return _artifact(
        stage="volume_plans",
        value=assembled_output,
        authority=authority,
        delta=delta,
        classifier=classify,
    )


class VolumeBeatRangeDelta(BaseModel):
    model_config = ConfigDict(extra="forbid")

    chapter_range: tuple[int, int]


class VolumeArcRangeDelta(BaseModel):
    model_config = ConfigDict(extra="forbid")

    chapter_range: tuple[int, int]
    beats: list[VolumeBeatRangeDelta] = Field(...)


class VolumeRefineDelta(BaseModel):
    model_config = ConfigDict(extra="forbid")

    chapter_count: int = Field(..., ge=1)
    tension_curve: list[int] = Field(...)
    arcs: list[VolumeArcRangeDelta] = Field(...)


class SingleVolumeRefineDelta(BaseModel):
    model_config = ConfigDict(extra="forbid")

    volume: VolumeRefineDelta


def assemble_volume_refine_delta(
    *,
    delta: SingleVolumeRefineDelta,
    original: VolumePlanItem,
    authority: FrozenAuthority,
    current_authority: Any,
) -> AssemblyArtifact:
    authority.assert_matches(current_authority)
    item = delta.volume
    if len(item.arcs) != len(original.arcs):
        raise CreativeDeltaAssemblyError(
            "authority_conflict",
            "volume_refine.volume.arcs",
            "refine 不得增删 arc",
        )
    if len(item.tension_curve) != item.chapter_count:
        raise CreativeDeltaAssemblyError(
            "invalid_model_delta",
            "volume_refine.volume.tension_curve",
            "长度必须等于 delta chapter_count，禁止 pad/trim",
        )
    if any(
        not isinstance(value, int) or not 1 <= value <= 10
        for value in item.tension_curve
    ):
        raise CreativeDeltaAssemblyError(
            "invalid_model_delta",
            "volume_refine.volume.tension_curve",
            "张力必须为 1..10 整数，禁止 clamp",
        )
    arcs: list[VolumeArcPlan] = []
    for arc_index, (arc_delta, arc) in enumerate(
        zip(item.arcs, original.arcs, strict=True)
    ):
        if len(arc_delta.beats) != len(arc.beats):
            raise CreativeDeltaAssemblyError(
                "authority_conflict",
                f"volume_refine.volume.arcs[{arc_index}].beats",
                "refine 不得增删 beat",
            )
        beats = [
            beat.model_copy(update={"chapter_range": beat_delta.chapter_range})
            for beat_delta, beat in zip(arc_delta.beats, arc.beats, strict=True)
        ]
        arcs.append(
            arc.model_copy(
                update={"chapter_range": arc_delta.chapter_range, "beats": beats}
            )
        )
    refined = original.model_copy(
        update={
            "chapter_count": item.chapter_count,
            "tension_curve": list(item.tension_curve),
            "arcs": arcs,
        }
    )
    # model_copy does not rerun nested business validators.
    value = VolumePlansOutput.model_validate(
        {"volumes": [refined.model_dump(mode="json")]}
    )

    def classify(path: str, _leaf: Any) -> tuple[ProvenanceKind, str, str | None]:
        if path.endswith(".chapter_count") or path.endswith(".tension_curve"):
            return "model_delta", f"volume_refine_delta.{path}", None
        if path.endswith(".chapter_range"):
            return "model_delta", f"volume_refine_delta.{path}", None
        return "copied_authority", f"original.{path}", "exact_copy"

    return _artifact(
        stage="volume_refine",
        value=value,
        authority=authority,
        delta=delta,
        classifier=classify,
    )


# ---------------------------------------------------------------------------
# Mini-plan
# ---------------------------------------------------------------------------


class CharacterArcBeatDelta(BaseModel):
    model_config = ConfigDict(extra="forbid")

    character_id: str = Field(..., min_length=1)
    beat: str = Field(..., min_length=1)


class MiniPlanItemDelta(BaseModel):
    model_config = ConfigDict(extra="forbid")

    @model_validator(mode="before")
    @classmethod
    def _require_explicit_dramatic_beat_id(cls, data: Any) -> Any:
        if isinstance(data, dict) and "dramatic_beat_id" not in data:
            raise ValueError("dramatic_beat_id 必须显式出现，普通章使用 null")
        return data

    one_line_event: str = Field(..., min_length=1)
    info_increment: str = Field(...)
    linked_characters: list[str] = Field(...)
    linked_threads: list[str] = Field(...)
    callback_anchors: list[str] = Field(...)
    chapter_focus: str = Field(...)
    reader_information: list[ReaderInformation] = Field(default_factory=list)
    arc_beats: list[CharacterArcBeatDelta] = Field(...)
    interaction_focus: str = Field(...)
    breakthrough_payoff: str = Field(...)
    dramatic_beat_id: str | None = None


class NarrativeMiniPlanItemDelta(MiniPlanItemDelta):
    event_contract: NarrativeEventContract


class NarrativeSingleVolumeMiniPlanDelta(BaseModel):
    model_config = ConfigDict(extra="forbid")
    items: list[NarrativeMiniPlanItemDelta] = Field(..., min_length=1)


class SingleVolumeMiniPlanDelta(BaseModel):
    model_config = ConfigDict(extra="forbid")

    items: list[MiniPlanItemDelta] = Field(..., min_length=1)


def _mini_plan_dramatic_authority_slots(
    volume: VolumePlanItem,
) -> tuple[tuple[str | None, bool], ...]:
    """Resolve one unambiguous typed-beat authority slot per chapter.

    The tuple value is ``(exact_beat_id, allow_null)``.  Chapters without a
    typed dramatic contract have ``(None, True)``; a single-chapter typed beat
    is mandatory; a multi-chapter typed beat is nullable at each covered
    position because the existing product validator remains responsible for
    enforcing exactly one assignment across the full range.
    """

    chapter_count = volume.chapter_count
    if chapter_count < 1:
        raise CreativeDeltaAssemblyError(
            "missing_authority_mapping",
            "mini_plan.items",
            "volume.chapter_count 必须大于零",
        )

    arc_ids_by_chapter: list[list[str]] = [
        [] for _chapter_number in range(chapter_count)
    ]
    typed_beats_by_chapter: list[list[VolumeBeatPlan]] = [
        [] for _chapter_number in range(chapter_count)
    ]
    seen_beat_ids: set[str] = set()
    for arc in volume.arcs:
        arc_start, arc_end = arc.chapter_range
        if arc_start < 1 or arc_end > chapter_count or arc_end < arc_start:
            raise CreativeDeltaAssemblyError(
                "authority_conflict",
                f"volume.arcs[{arc.arc_id}].chapter_range",
                f"range={arc.chapter_range},chapter_count={chapter_count}",
            )
        for chapter_number in range(arc_start, arc_end + 1):
            arc_ids_by_chapter[chapter_number - 1].append(arc.arc_id)

        for beat in arc.beats:
            if beat.beat_id in seen_beat_ids:
                raise CreativeDeltaAssemblyError(
                    "ambiguous_authority",
                    "volume.beats",
                    f"重复 beat_id:{beat.beat_id}",
                )
            seen_beat_ids.add(beat.beat_id)
            beat_start, beat_end = beat.chapter_range
            if beat_start < arc_start or beat_end > arc_end or beat_end < beat_start:
                raise CreativeDeltaAssemblyError(
                    "authority_conflict",
                    f"volume.beats[{beat.beat_id}].chapter_range",
                    f"range={beat.chapter_range},arc_range={arc.chapter_range}",
                )
            if beat.dramatic_contract is None:
                continue
            for chapter_number in range(beat_start, beat_end + 1):
                typed_beats_by_chapter[chapter_number - 1].append(beat)

    slots: list[tuple[str | None, bool]] = []
    for chapter_number, (arc_ids, typed_beats) in enumerate(
        zip(arc_ids_by_chapter, typed_beats_by_chapter, strict=True),
        start=1,
    ):
        if len(arc_ids) != 1:
            code = "missing_authority_mapping" if not arc_ids else "ambiguous_authority"
            raise CreativeDeltaAssemblyError(
                code,
                f"mini_plan.items[{chapter_number - 1}].arc_id",
                f"chapter={chapter_number},matching_arcs={arc_ids}",
            )
        if len(typed_beats) > 1:
            raise CreativeDeltaAssemblyError(
                "ambiguous_authority",
                f"mini_plan.items[{chapter_number - 1}].dramatic_beat_id",
                "同章命中多个 typed dramatic beat:"
                f"{[beat.beat_id for beat in typed_beats]}",
            )
        if not typed_beats:
            slots.append((None, True))
            continue
        beat = typed_beats[0]
        slots.append(
            (
                beat.beat_id,
                beat.chapter_range[0] != beat.chapter_range[1],
            )
        )
    return tuple(slots)


def mini_plan_reference_brief(volume: VolumePlanItem) -> str:
    """Render required joins outside schema annotations (compiler drops those)."""
    lines = [
        "## 所选戏剧任务的必需关联",
        "以下均为上游已有引用。选择对应 dramatic_beat_id 时，linked_characters 必须包含所列人物，linked_threads 必须包含所列伏笔；可以再选其他合法引用。间接对抗也必须关联 counterforce，关联不等于要求其本人出场。",
        "callback_anchors 只允许已有 thread ID，没有回响则填 []。",
    ]
    for arc in volume.arcs:
        for beat in arc.beats:
            drama = beat.dramatic_contract
            if drama is None:
                continue
            lines.append(
                f"- 章 {beat.chapter_range[0]}–{beat.chapter_range[1]} / {beat.beat_id}: "
                f"linked_characters 至少 [{drama.actor_id}, {drama.counterforce_id}]; "
                f"linked_threads 至少 [{drama.thread_id or ''}]。"
            )
    return "\n".join(lines)


def build_mini_plan_delta_schema_transform(
    *,
    volume: VolumePlanItem,
    allowed_character_ids: list[str] | None = None,
    allowed_thread_ids: list[str] | None = None,
    reader_sources: list[str] | None = None,
) -> Callable[[dict[str, Any]], None]:
    """Bind the exact-Hy3 mini-plan wire schema to chapter authority.

    Only array cardinality, positional chapter slots, and the legal typed beat
    ID (or ``null``) are frozen.  Every event, information increment, action,
    interaction, character-arc beat, focus, payoff, and other creative string
    remains model-authored.
    """

    chapter_count = volume.chapter_count
    authority_slots = _mini_plan_dramatic_authority_slots(volume)

    def transform(schema: dict[str, Any]) -> None:
        try:
            items_schema = schema["properties"]["items"]
            base_item_schema = items_schema["items"]
        except (KeyError, TypeError) as exc:
            raise ValueError(
                "mini-plan delta schema shape drifted before transform"
            ) from exc
        if not isinstance(items_schema, dict) or not isinstance(base_item_schema, dict):
            raise ValueError("mini-plan delta items schema must be objects")

        items_schema["minItems"] = chapter_count
        items_schema["maxItems"] = chapter_count
        prefix_items: list[dict[str, Any]] = []
        for chapter_number, (beat_id, allow_null) in enumerate(
            authority_slots,
            start=1,
        ):
            item_schema = deepcopy(base_item_schema)
            properties = item_schema.get("properties")
            if not isinstance(properties, dict):
                raise ValueError(
                    f"mini-plan delta item {chapter_number} properties missing"
                )
            if reader_sources is not None:
                properties["chapter_focus"]["minLength"] = 1
                properties["reader_information"]["items"]["properties"]["source_path"]["enum"] = reader_sources
            # v6 exposes the same reference catalogs that assembly validates.
            # None preserves historical wire schemas; an empty catalog allows
            # only an empty array, never arbitrary model-invented identifiers.
            for field_name, allowed in (
                ("linked_characters", allowed_character_ids),
                ("linked_threads", allowed_thread_ids),
                ("callback_anchors", allowed_thread_ids),
            ):
                if allowed is None:
                    continue
                field_schema = properties[field_name]
                if allowed:
                    field_schema["items"]["enum"] = sorted(set(allowed))
                else:
                    field_schema["maxItems"] = 0
            if allowed_character_ids is not None:
                arc_schema = properties["arc_beats"]
                if allowed_character_ids:
                    arc_schema["items"]["properties"]["character_id"]["enum"] = sorted(set(allowed_character_ids))
                else:
                    arc_schema["maxItems"] = 0
            beat_schema = properties.get("dramatic_beat_id")
            if not isinstance(beat_schema, dict):
                raise ValueError(
                    f"mini-plan delta item {chapter_number} dramatic_beat_id missing"
                )

            required = list(item_schema.get("required", []))
            if "dramatic_beat_id" not in required:
                required.append("dramatic_beat_id")
            item_schema["required"] = required

            if beat_id is None:
                beat_schema["type"] = "null"
                beat_schema["enum"] = [None]
            elif allow_null:
                beat_schema["type"] = ["string", "null"]
                beat_schema["enum"] = [beat_id, None]
            else:
                beat_schema["type"] = "string"
                beat_schema["enum"] = [beat_id]
            prefix_items.append(item_schema)

        items_schema["prefixItems"] = prefix_items

    return transform


def assemble_mini_plan_delta(
    *,
    delta: SingleVolumeMiniPlanDelta | NarrativeSingleVolumeMiniPlanDelta,
    volume: VolumePlanItem,
    chapter_id_map: dict[tuple[str, int], str],
    characters: CharactersOutput,
    plot: PlotOutput,
    authority: FrozenAuthority,
    current_authority: Any,
    world: WorldOutput | None = None,
) -> AssemblyArtifact:
    authority.assert_matches(current_authority)
    _mini_plan_dramatic_authority_slots(volume)
    if len(delta.items) != volume.chapter_count:
        raise CreativeDeltaAssemblyError(
            "authority_conflict",
            "mini_plan.items",
            f"delta={len(delta.items)},authority={volume.chapter_count}",
        )
    character_ids = {item.character_id for item in characters.characters}
    thread_ids = {item.thread_id for item in plot.long_threads}
    arc_for_chapter: dict[int, VolumeArcPlan] = {}
    beat_by_id: dict[str, VolumeBeatPlan] = {}
    for authority_arc in volume.arcs:
        for chapter_number in range(
            authority_arc.chapter_range[0], authority_arc.chapter_range[1] + 1
        ):
            if chapter_number in arc_for_chapter:
                raise CreativeDeltaAssemblyError(
                    "ambiguous_authority",
                    f"volume.arcs.chapter_range[{chapter_number}]",
                    "同一章命中多个 arc",
                )
            arc_for_chapter[chapter_number] = authority_arc
        for authority_beat in authority_arc.beats:
            if authority_beat.beat_id in beat_by_id:
                raise CreativeDeltaAssemblyError(
                    "ambiguous_authority", "volume.beats", authority_beat.beat_id
                )
            beat_by_id[authority_beat.beat_id] = authority_beat

    items: list[MiniPlanItem] = []
    for index, item in enumerate(delta.items, start=1):
        path = f"mini_plan.items[{index - 1}]"
        if world is not None and world.story_intent is not None and not item.chapter_focus.strip():
            raise CreativeDeltaAssemblyError("missing_narrative_focus", f"{path}.chapter_focus", "新小纲每章需要简短阅读重点")
        chapter_arc = arc_for_chapter.get(index)
        if chapter_arc is None:
            raise CreativeDeltaAssemblyError(
                "missing_authority_mapping",
                f"{path}.arc_id",
                "卷规划未覆盖本章",
            )
        map_key = (volume.volume_id, index)
        if map_key not in chapter_id_map:
            raise CreativeDeltaAssemblyError(
                "missing_authority_mapping", f"{path}.chapter_id", str(map_key)
            )
        for field_name, values, allowed in (
            ("linked_characters", item.linked_characters, character_ids),
            ("linked_threads", item.linked_threads, thread_ids),
            ("callback_anchors", item.callback_anchors, thread_ids),
        ):
            _require_unique(values, path=f"{path}.{field_name}")
            for ref_index, value in enumerate(values):
                _require_known(value, allowed, path=f"{path}.{field_name}[{ref_index}]")
        arc_beat_ids = [entry.character_id for entry in item.arc_beats]
        _require_unique(arc_beat_ids, path=f"{path}.arc_beats")
        for ref_index, value in enumerate(arc_beat_ids):
            _require_known(value, character_ids, path=f"{path}.arc_beats[{ref_index}]")

        dramatic_contract = None
        if item.dramatic_beat_id is not None:
            selected_beat = beat_by_id.get(item.dramatic_beat_id)
            if selected_beat is None:
                raise CreativeDeltaAssemblyError(
                    "unknown_authority_reference",
                    f"{path}.dramatic_beat_id",
                    item.dramatic_beat_id,
                )
            if (
                not selected_beat.chapter_range[0]
                <= index
                <= selected_beat.chapter_range[1]
            ):
                raise CreativeDeltaAssemblyError(
                    "authority_conflict",
                    f"{path}.dramatic_beat_id",
                    "所选 beat 不覆盖本章",
                )
            if selected_beat.dramatic_contract is None:
                raise CreativeDeltaAssemblyError(
                    "missing_authority_mapping",
                    f"{path}.dramatic_beat_id",
                    "所选 beat 没有 typed dramatic contract",
                )
            dramatic_contract = selected_beat.dramatic_contract

        if isinstance(item, NarrativeMiniPlanItemDelta) and not item.event_contract.required_events and dramatic_contract is None:
            raise CreativeDeltaAssemblyError("missing_chapter_event", path, "普通章需要明确的事件或结果，不能由出卡时临时补造")
        items.append(
            MiniPlanItem(
                chapter_id=chapter_id_map[map_key],
                chapter_number=index,
                volume_id=volume.volume_id,
                arc_id=chapter_arc.arc_id,
                one_line_event=item.one_line_event,
                narrative_context=(assemble_narrative_context(
                    world=world, characters=characters,
                    reader_information=item.reader_information,
                    event_contract=(item.event_contract if isinstance(item, NarrativeMiniPlanItemDelta) else None),
                ) if world is not None else None),
                info_increment=item.info_increment,
                linked_characters=list(item.linked_characters),
                linked_threads=list(item.linked_threads),
                callback_anchors=list(item.callback_anchors),
                chapter_focus=item.chapter_focus,
                arc_beats={entry.character_id: entry.beat for entry in item.arc_beats},
                interaction_focus=item.interaction_focus,
                dramatic_contract=dramatic_contract,
                breakthrough_payoff=item.breakthrough_payoff,
            )
        )
    assembled_output = MiniPlanOutput(items=items)

    def classify(path: str, _leaf: Any) -> tuple[ProvenanceKind, str, str | None]:
        if path.endswith((".chapter_id", ".chapter_number", ".volume_id", ".arc_id")):
            return "copied_authority", path, "range_or_map_exact_copy"
        if ".dramatic_contract." in path:
            return "copied_authority", path, "beat_contract_exact_copy"
        if ".narrative_context.story_intent." in path or ".narrative_context.character_motives." in path:
            return "copied_authority", path, "narrative_source_exact_copy"
        return "model_delta", f"mini_plan_delta.{path}", None

    return _artifact(
        stage="mini_plan",
        value=assembled_output,
        authority=authority,
        delta=delta,
        classifier=classify,
    )


def count_schema_properties(model: type[BaseModel]) -> int:
    """Count every object property in a Pydantic JSON schema once."""

    schema = model.model_json_schema()
    seen: set[int] = set()

    def walk(node: Any) -> int:
        if isinstance(node, dict):
            identity = id(node)
            if identity in seen:
                return 0
            seen.add(identity)
            properties = node.get("properties")
            count = len(properties) if isinstance(properties, dict) else 0
            return count + sum(walk(value) for value in node.values())
        if isinstance(node, list):
            return sum(walk(value) for value in node)
        return 0

    return walk(schema)


def rendered_structured_input_bytes(
    *, system_text: str, user_text: str, output_model: type[BaseModel]
) -> int:
    """Frozen offline comparison metric: rendered messages plus wire schema."""

    return (
        len(system_text.encode("utf-8"))
        + len(user_text.encode("utf-8"))
        + len(canonical_json_bytes(output_model.model_json_schema()))
    )


__all__ = [
    "assemble_characters_delta",
    "assemble_mini_plan_delta",
    "assemble_plot_delta",
    "assemble_volume_plans_delta",
    "assemble_volume_plans_delta_v4",
    "assemble_volume_refine_delta",
    "AssemblyArtifact",
    "build_assembly_artifact",
    "build_evidence_source_catalog",
    "build_mini_plan_delta_schema_transform",
    "canonical_json_bytes",
    "canonical_sha256",
    "CharacterArcBeatDelta",
    "CharactersCreativeDelta",
    "count_schema_properties",
    "CreativeDeltaAssemblyError",
    "EvidenceSource",
    "FieldProvenance",
    "FrozenAuthority",
    "HY3_AUTHORITY_ASSEMBLY_CAPABILITY",
    "HY3_CREATIVE_DELTA_CAPABILITY",
    "MiniPlanItemDelta",
    "PlotCreativeDelta",
    "build_volume_plans_delta_v4_schema_transform",
    "build_volume_plans_delta_v5_schema_transform",
    "rendered_structured_input_bytes",
    "render_evidence_source_catalog",
    "resolve_evidence_source",
    "SingleVolumeMiniPlanDelta",
    "SingleVolumeRefineDelta",
    "uses_hy3_creative_delta",
    "uses_hy3_authority_assembly",
    "VolumePlansCreativeDelta",
    "VolumePlansCreativeDeltaV4",
]
