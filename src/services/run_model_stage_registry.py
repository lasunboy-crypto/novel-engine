"""Typed S2 stage registry, render ceilings, and reachable request DAG.

This module is the accounting bridge between the existing product call graph
and the single-model run manifest.  It does not schedule an Agent or create a
retry.  Counts are derived from the constants owned by the production stages;
the resulting graph is used only for preflight, cost reservation, and the
pre-request rendered-input lock.

The input unit deliberately matches :mod:`src.services.attempt_ledger`:
``64 + sum(UTF-8 bytes + 32 per Chat message)``.  A byte-level upper bound is
conservative when an exact provider tokenizer is unavailable, and—unlike a
provider context window—it is enforced on the actual rendered request before
network I/O.
"""

from __future__ import annotations

import hashlib
import json
from dataclasses import asdict, dataclass
from typing import Literal, Sequence, get_args

from src.core.llm.base import (
    MAX_PROVIDER_ATTEMPTS_PER_LOGICAL_CALL,
    Message,
)
from src.core.models.routing import AGENT_NAME
from src.core.models.run_model_manifest import RunModelTierName


StagePathKind = Literal["counted_success", "control", "excluded"]
ReachabilityRuleKind = Literal[
    "nth_attempt_succeeds",
    "bounded_budget",
    "mutually_exclusive",
    "terminal_stop",
    "success_only_downstream",
]

S2_ACTIVITY_CHAPTERS = 3
S2_ACTIVITY_VOLUMES = 1
V8_SANITY_PHYSICAL_ATTEMPTS = 261


@dataclass(frozen=True, slots=True)
class PromptRenderBound:
    """Runtime-enforced input ceiling plus its deterministic evidence."""

    tier: RunModelTierName
    max_accounting_input_tokens: int
    observed_fixture: str
    observed_render_chars: int
    observed_utf8_byte_upper_bound: int
    safety_margin_basis_points: int
    method: str = "64 + sum(len(content.encode('utf-8')) + 32)"

    def __post_init__(self) -> None:
        if self.max_accounting_input_tokens <= self.observed_utf8_byte_upper_bound:
            raise ValueError(f"{self.tier} render ceiling 没有保守余量")
        actual_margin = (
            self.max_accounting_input_tokens
            * 10_000
            // self.observed_utf8_byte_upper_bound
            - 10_000
        )
        if actual_margin < self.safety_margin_basis_points:
            raise ValueError(f"{self.tier} render ceiling 低于声明 margin")


# xhigh is measured from a complete deterministic one-volume/three-chapter
# outline assembly through mini-plan and its judges.  The medium/low worst
# fixtures are the frozen B0 full-memory and continuity renders; four UTF-8
# bytes per Unicode code point plus exact Chat framing is used before adding a
# further runtime margin.  The tests rebuild both evidence families from the
# real prompt assemblers.
PROMPT_RENDER_BOUNDS: dict[RunModelTierName, PromptRenderBound] = {
    "xhigh": PromptRenderBound(
        tier="xhigh",
        max_accounting_input_tokens=65_536,
        observed_fixture=(
            "s2-complete-one-volume-three-chapter-outline-assembler"
        ),
        observed_render_chars=25_710,
        observed_utf8_byte_upper_bound=45_948,
        safety_margin_basis_points=4_000,
    ),
    "medium": PromptRenderBound(
        tier="medium",
        max_accounting_input_tokens=49_152,
        observed_fixture="post-v8-b0-memory-extractor-frozen-render",
        observed_render_chars=9_928,
        observed_utf8_byte_upper_bound=9_928 * 4 + 128,
        safety_margin_basis_points=2_000,
    ),
    "low": PromptRenderBound(
        tier="low",
        max_accounting_input_tokens=32_768,
        observed_fixture="post-v8-b0-continuity-q5-frozen-render",
        observed_render_chars=6_624,
        observed_utf8_byte_upper_bound=6_624 * 4 + 128,
        safety_margin_basis_points=2_000,
    ),
}

# ``chapter_summary_extractor`` renders one JSONL authority row per exact
# evidence source.  At the production 4,500-character limit, its request shape
# can therefore be much denser than the other low-tier Agents.  T5R4C rebuilds
# this override from the real prompt/catalog/schema path with 2,250 source rows,
# maximum existing bounded metadata, and a further 20% byte-accounting margin.
# The tier default remains unchanged for every other low Agent.
AGENT_PROMPT_RENDER_BOUNDS: dict[str, PromptRenderBound] = {
    "chapter_summary_extractor": PromptRenderBound(
        tier="low",
        max_accounting_input_tokens=211_050,
        observed_fixture=(
            "post-v8-hy3-summary-agent-bound-v1:"
            "max_existing_bounded_fields__max_evidence_rows_han"
        ),
        observed_render_chars=159_061,
        observed_utf8_byte_upper_bound=175_875,
        safety_margin_basis_points=2_000,
    ),
}


_XHIGH_AGENTS = frozenset(
    {
        "outline_builder",
        "mini_plan_generator",
        "mechanism_consistency_validator",
        "thread_closure_validator",
        "identity_coverage_validator",
        "volume_seam_replay_validator",
        "outline_consistency_validator",
    }
)
_LOW_AGENTS = frozenset(
    {
        "continuity",
        "reranker",
        "memory_health_checker",
        "chapter_summary_extractor",
        "continuity_validator",
        "scene_anchor_validator",
        "cross_chapter_dedup_validator",
        "ending_tension_validator",
    }
)
_ALL_AGENTS = frozenset(get_args(AGENT_NAME))
_MEDIUM_AGENTS = _ALL_AGENTS - _XHIGH_AGENTS - _LOW_AGENTS

RUN_MODEL_AGENT_TIER: dict[str, RunModelTierName] = {
    **{name: "xhigh" for name in _XHIGH_AGENTS},
    **{name: "medium" for name in _MEDIUM_AGENTS},
    **{name: "low" for name in _LOW_AGENTS},
}
if set(RUN_MODEL_AGENT_TIER) != _ALL_AGENTS:
    raise RuntimeError("run model stage registry 的 Agent tier 覆盖不完整")
for _agent_name, _agent_bound in AGENT_PROMPT_RENDER_BOUNDS.items():
    if _agent_name not in RUN_MODEL_AGENT_TIER:
        raise RuntimeError(f"agent-specific render bound 未登记:{_agent_name}")
    if _agent_bound.tier != RUN_MODEL_AGENT_TIER[_agent_name]:
        raise RuntimeError(f"agent-specific render bound tier 漂移:{_agent_name}")


def run_model_tier_for_agent(agent_name: str) -> RunModelTierName:
    """Return the only manifest tier authorised for a routed Agent."""

    try:
        return RUN_MODEL_AGENT_TIER[agent_name]
    except KeyError as exc:
        raise ValueError(f"未登记 agent {agent_name!r}") from exc


def prompt_render_bound_for_agent(agent_name: str) -> PromptRenderBound:
    tier = run_model_tier_for_agent(agent_name)
    return AGENT_PROMPT_RENDER_BOUNDS.get(agent_name, PROMPT_RENDER_BOUNDS[tier])


def conservative_rendered_input_tokens(messages: Sequence[Message]) -> int:
    """Return the shared byte-conservative Chat input accounting upper bound."""

    return 64 + sum(
        len(message.content.encode("utf-8")) + 32 for message in messages
    )


@dataclass(frozen=True, slots=True)
class ReachabilityRule:
    rule_id: str
    kind: ReachabilityRuleKind
    source: str
    counted_outcome: str
    alternatives: tuple[str, ...] = ()
    maximum: int | None = None


@dataclass(frozen=True, slots=True)
class ReachabilityStageNode:
    """One typed aggregate node in the S2 successful-path DAG."""

    stage_id: str
    agent_name: str | None
    tier: RunModelTierName | None
    logical_calls: int
    predecessors: tuple[str, ...]
    source: str
    rule_ids: tuple[str, ...] = ()
    path_kind: StagePathKind = "counted_success"
    exclusion_reason: str | None = None

    @property
    def physical_attempts(self) -> int:
        return self.logical_calls * MAX_PROVIDER_ATTEMPTS_PER_LOGICAL_CALL

    @property
    def render_input_tokens_per_attempt(self) -> int:
        if self.tier is None:
            return 0
        if self.agent_name is not None and self.agent_name != "smoke":
            return prompt_render_bound_for_agent(
                self.agent_name
            ).max_accounting_input_tokens
        return PROMPT_RENDER_BOUNDS[self.tier].max_accounting_input_tokens


@dataclass(frozen=True, slots=True)
class S2ReachabilityDAG:
    """Machine-checkable aggregate DAG for one future three-chapter arm."""

    nodes: tuple[ReachabilityStageNode, ...]
    rules: tuple[ReachabilityRule, ...]
    chapters: int
    volumes: int
    sanity_v8_physical_attempts: int = V8_SANITY_PHYSICAL_ATTEMPTS

    def __post_init__(self) -> None:
        rule_ids = {rule.rule_id for rule in self.rules}
        if len(rule_ids) != len(self.rules):
            raise ValueError("reachability rule_id 重复")
        seen: set[str] = set()
        covered_agents: set[str] = set()
        for node in self.nodes:
            if node.stage_id in seen:
                raise ValueError(f"stage_id 重复: {node.stage_id}")
            unknown_predecessors = set(node.predecessors) - seen
            if unknown_predecessors:
                raise ValueError(
                    f"{node.stage_id} 不是拓扑顺序或 predecessor 不存在: "
                    f"{sorted(unknown_predecessors)}"
                )
            unknown_rules = set(node.rule_ids) - rule_ids
            if unknown_rules:
                raise ValueError(
                    f"{node.stage_id} 引用未知 reachability rule: "
                    f"{sorted(unknown_rules)}"
                )
            if node.agent_name is not None and node.agent_name != "smoke":
                covered_agents.add(node.agent_name)
                expected_tier = run_model_tier_for_agent(node.agent_name)
                if node.tier != expected_tier:
                    raise ValueError(
                        f"{node.stage_id} tier 漂移: {node.tier} != {expected_tier}"
                    )
            if node.path_kind == "counted_success":
                if node.logical_calls < 1 or node.tier is None:
                    raise ValueError(f"{node.stage_id} counted node 无 Provider 调用")
            elif node.logical_calls != 0:
                raise ValueError(f"{node.stage_id} control/excluded node 不得计费")
            if node.path_kind == "excluded" and not node.exclusion_reason:
                raise ValueError(f"{node.stage_id} 缺少排除原因")
            seen.add(node.stage_id)
        if covered_agents != _ALL_AGENTS:
            raise ValueError(
                "S2 stage registry 未覆盖全部路由 Agent: "
                f"missing={sorted(_ALL_AGENTS - covered_agents)}"
            )

    @property
    def counted_nodes(self) -> tuple[ReachabilityStageNode, ...]:
        return tuple(
            node for node in self.nodes if node.path_kind == "counted_success"
        )

    @property
    def logical_calls(self) -> int:
        return sum(node.logical_calls for node in self.counted_nodes)

    @property
    def physical_attempts(self) -> int:
        return sum(node.physical_attempts for node in self.counted_nodes)

    @property
    def logical_calls_by_tier(self) -> dict[RunModelTierName, int]:
        totals: dict[RunModelTierName, int] = {"low": 0, "medium": 0, "xhigh": 0}
        for node in self.counted_nodes:
            assert node.tier is not None
            totals[node.tier] += node.logical_calls
        return totals

    def registry_hash(self) -> str:
        payload = {
            "chapters": self.chapters,
            "volumes": self.volumes,
            "nodes": [asdict(node) for node in self.nodes],
            "rules": [asdict(rule) for rule in self.rules],
            "render_bounds": {
                tier: asdict(bound) for tier, bound in PROMPT_RENDER_BOUNDS.items()
            },
            "agent_render_bounds": {
                agent: asdict(bound)
                for agent, bound in AGENT_PROMPT_RENDER_BOUNDS.items()
            },
        }
        encoded = json.dumps(
            payload,
            ensure_ascii=False,
            sort_keys=True,
            separators=(",", ":"),
        ).encode("utf-8")
        return hashlib.sha256(encoded).hexdigest()


@dataclass(frozen=True, slots=True)
class S2RequestCorridorComponent:
    """One reconstructable term in the future selection-test stop corridor."""

    component_id: str
    tier: RunModelTierName
    logical_calls: int
    source: str
    agent_name: str | None = None

    def __post_init__(self) -> None:
        if self.agent_name is not None:
            expected_tier = run_model_tier_for_agent(self.agent_name)
            if self.tier != expected_tier:
                raise ValueError(
                    f"{self.component_id} tier 漂移: {self.tier} != {expected_tier}"
                )

    @property
    def physical_attempts(self) -> int:
        return self.logical_calls * MAX_PROVIDER_ATTEMPTS_PER_LOGICAL_CALL


@dataclass(frozen=True, slots=True)
class S2SelectionTestRequestCorridor:
    """Future selection-experiment stop corridor; never a production cap."""

    nominal_components: tuple[S2RequestCorridorComponent, ...]
    recovery_components: tuple[S2RequestCorridorComponent, ...]
    nominal_logical_calls: int
    nominal_physical_attempts: int
    recovery_logical_calls: int
    recovery_physical_attempts: int
    suggested_max_physical_requests: int
    recovery_pool_is_fungible: bool = True

    def __post_init__(self) -> None:
        if sum(item.logical_calls for item in self.nominal_components) != (
            self.nominal_logical_calls
        ):
            raise ValueError("selection-test nominal component 不守恒")
        if sum(item.logical_calls for item in self.recovery_components) != (
            self.recovery_logical_calls
        ):
            raise ValueError("selection-test recovery component 不守恒")
        expected_physical = (
            self.nominal_logical_calls + self.recovery_logical_calls
        ) * MAX_PROVIDER_ATTEMPTS_PER_LOGICAL_CALL
        if self.suggested_max_physical_requests != expected_physical:
            raise ValueError("selection-test request proposal 未由 component 重建")


def build_s2_selection_test_request_corridor() -> S2SelectionTestRequestCorridor:
    """Derive a future selection-test stop line from the frozen Run graph.

    The nominal path uses one successful generation per ordinary stage, one
    proactive trim allowance per chapter, the authoritative post-review
    maxima, and one summary/memory pass.  The recovery pool is *derived*, not
    hand-entered: it prices one complete additional rewrite round per chapter,
    including the existing GLOBAL no-progress retry, both word-count trim
    attempts, all Reviewer parse attempts, and the full post-review gate set.

    The pool is fungible: an outline/card/schema retry consumes the same pool
    and therefore reduces the remaining revision runway.  It is deliberately
    not a historical average or a completion guarantee.  Hitting it means
    stop—not raise or borrow from another arm.
    """

    from src.core.agents.word_count_trim import DEFAULT_MAX_ATTEMPTS
    from src.core.orchestrator.chapter_orchestrator import (
        GLOBAL_REWRITE_NO_PROGRESS_MAX_RETRIES,
        POST_REVIEW_GATE_LOGICAL_CALL_MAXIMA,
        REVIEWER_MAX_ATTEMPTS,
    )

    chapters = S2_ACTIVITY_CHAPTERS
    post_review_medium = chapters * (
        POST_REVIEW_GATE_LOGICAL_CALL_MAXIMA["must_achieve"]
        + POST_REVIEW_GATE_LOGICAL_CALL_MAXIMA["forbidden_crossing"]
        + POST_REVIEW_GATE_LOGICAL_CALL_MAXIMA["setting"]
    )
    post_review_low = (
        (chapters - 1)
        * (
            POST_REVIEW_GATE_LOGICAL_CALL_MAXIMA["continuity"]
            + POST_REVIEW_GATE_LOGICAL_CALL_MAXIMA["dedup"]
        )
        + POST_REVIEW_GATE_LOGICAL_CALL_MAXIMA["ending"]
    )
    nominal_components = (
        S2RequestCorridorComponent(
            "protocol_smoke_json_object",
            "low",
            1,
            "one minimal Chat JSON Object protocol smoke before the arm",
        ),
        S2RequestCorridorComponent(
            "protocol_smoke_thinking_usage",
            "medium",
            1,
            "one thinking/SSE usage protocol smoke before the arm",
        ),
        S2RequestCorridorComponent(
            "outline_clean_xhigh",
            "xhigh",
            12,
            "twelve xhigh outline/mini-plan stages on the clean Run path",
        ),
        S2RequestCorridorComponent(
            "outline_clean_medium",
            "medium",
            3 + chapters,
            "three setting stages plus one beat Judge per chapter",
        ),
        S2RequestCorridorComponent(
            "card_clean",
            "medium",
            chapters * 3,
            "generator + setting + must/forbidden per chapter",
        ),
        S2RequestCorridorComponent(
            "prewrite",
            "medium",
            chapters - 1,
            "must feasibility when a prior chapter exists",
        ),
        S2RequestCorridorComponent(
            "writer_and_one_trim",
            "medium",
            chapters * 2,
            "one Writer plus one proactive trim allowance per chapter",
        ),
        S2RequestCorridorComponent(
            "reviewer_clean",
            "medium",
            chapters,
            "one parsed Reviewer result per chapter",
        ),
        S2RequestCorridorComponent(
            "post_review_authoritative_medium",
            "medium",
            post_review_medium,
            "must/forbidden/setting maxima on the initial versions",
        ),
        S2RequestCorridorComponent(
            "post_review_authoritative_low",
            "low",
            post_review_low,
            "continuity/dedup/final-ending maxima on the initial versions",
        ),
        S2RequestCorridorComponent(
            "summary",
            "low",
            chapters,
            "one successful summary call per approved chapter",
            agent_name="chapter_summary_extractor",
        ),
        S2RequestCorridorComponent(
            "rich_memory_medium",
            "medium",
            chapters * 3 + (chapters - 1),
            "extract/disambiguate/plot and later-chapter timeline merges",
        ),
        S2RequestCorridorComponent(
            "rich_memory_low",
            "low",
            chapters - 1,
            "later-chapter scene-anchor checks",
        ),
        S2RequestCorridorComponent(
            "rich_memory_xhigh",
            "xhigh",
            1,
            "final plot-thread payoff observation",
        ),
    )
    nominal_logical = sum(item.logical_calls for item in nominal_components)
    nominal_physical = (
        nominal_logical * MAX_PROVIDER_ATTEMPTS_PER_LOGICAL_CALL
    )
    recovery_components = (
        S2RequestCorridorComponent(
            "one_rewrite_per_chapter",
            "medium",
            chapters,
            "one additional Writer rewrite per chapter",
        ),
        S2RequestCorridorComponent(
            "global_no_progress",
            "medium",
            chapters * GLOBAL_REWRITE_NO_PROGRESS_MAX_RETRIES,
            "GLOBAL_REWRITE_NO_PROGRESS_MAX_RETRIES per rewrite",
        ),
        S2RequestCorridorComponent(
            "rewrite_word_count_trim",
            "medium",
            chapters * DEFAULT_MAX_ATTEMPTS,
            "word_count_trim.DEFAULT_MAX_ATTEMPTS per rewrite",
        ),
        S2RequestCorridorComponent(
            "rewrite_reviewer_parse",
            "medium",
            chapters * REVIEWER_MAX_ATTEMPTS,
            "REVIEWER_MAX_ATTEMPTS per rewritten version",
        ),
        S2RequestCorridorComponent(
            "rewrite_post_review_medium",
            "medium",
            post_review_medium,
            "must/forbidden/setting gates on all rewritten versions",
        ),
        S2RequestCorridorComponent(
            "rewrite_post_review_low",
            "low",
            post_review_low,
            "continuity/dedup/final-ending gates on all rewritten versions",
        ),
    )
    recovery_logical = sum(item.logical_calls for item in recovery_components)
    if recovery_logical <= 0:
        raise ValueError("selection-test proposal 没有正的 recovery pool")
    recovery_physical = (
        recovery_logical * MAX_PROVIDER_ATTEMPTS_PER_LOGICAL_CALL
    )
    return S2SelectionTestRequestCorridor(
        nominal_components=nominal_components,
        recovery_components=recovery_components,
        nominal_logical_calls=nominal_logical,
        nominal_physical_attempts=nominal_physical,
        recovery_logical_calls=recovery_logical,
        recovery_physical_attempts=recovery_physical,
        suggested_max_physical_requests=(nominal_logical + recovery_logical)
        * MAX_PROVIDER_ATTEMPTS_PER_LOGICAL_CALL,
    )


def _node(
    stage_id: str,
    agent_name: str,
    logical_calls: int,
    predecessors: tuple[str, ...],
    source: str,
    *rule_ids: str,
) -> ReachabilityStageNode:
    tier: RunModelTierName = (
        "low" if agent_name == "smoke" else run_model_tier_for_agent(agent_name)
    )
    return ReachabilityStageNode(
        stage_id=stage_id,
        agent_name=agent_name,
        tier=tier,
        logical_calls=logical_calls,
        predecessors=predecessors,
        source=source,
        rule_ids=tuple(rule_ids),
    )


def build_s2_reachability_dag(
    *,
    chapters: int = S2_ACTIVITY_CHAPTERS,
    volumes: int = S2_ACTIVITY_VOLUMES,
) -> S2ReachabilityDAG:
    """Build the maximum successful path without summing terminal branches.

    Each schema/business retry is represented as “the Nth attempt succeeds”.
    A transport call similarly reserves three physical attempts but can only
    continue when the final attempt succeeds.  Human escalation, a second
    no-progress result, and retry exhaustion are terminal alternatives, so
    downstream summary/memory is never added to those failure paths.
    """

    if chapters != S2_ACTIVITY_CHAPTERS or volumes != S2_ACTIVITY_VOLUMES:
        raise ValueError("当前冻结 S2 activity 必须精确为 1 卷 3 章")

    # Local imports avoid adding heavyweight Agent/service import cycles to the
    # runtime router.  These values remain owned and used by product code.
    from src.config import settings
    from src.core.agents.chapter_summary_extractor import (
        CONTINUITY_LEDGER_SCHEMA_MAX_ATTEMPTS,
    )
    from src.core.agents.setting_consistency_validator import (
        SETTING_CONSISTENCY_AUTHORITATIVE_MAX_ATTEMPTS,
    )
    from src.core.agents.word_count_trim import DEFAULT_MAX_ATTEMPTS
    from src.core.models.chapter_card import ChapterCard
    from src.core.orchestrator.chapter_orchestrator import (
        GLOBAL_REWRITE_NO_PROGRESS_MAX_RETRIES,
        POST_REVIEW_GATE_LOGICAL_CALL_MAXIMA,
        REVIEWER_MAX_ATTEMPTS,
    )
    from src.services.chapter_card_service import (
        CHAPTER_CARD_REFERENCE_RETRY_MAX_RETRIES,
        CHAPTER_CARD_SHARED_CONTRACT_REPAIR_MAX_RETRIES,
    )

    outline_attempts = settings.outline_builder_max_retry_attempts
    rewrite_budget = int(ChapterCard.model_fields["max_rewrites"].default)
    revise_budget = int(ChapterCard.model_fields["max_revises"].default)
    versions_per_chapter = 1 + rewrite_budget + revise_budget
    base_writer_calls_per_chapter = 1 + rewrite_budget
    volume_rounds = 3  # initial + consistency retry + arc-span retry
    card_candidate_calls = (
        1
        + CHAPTER_CARD_SHARED_CONTRACT_REPAIR_MAX_RETRIES
        + CHAPTER_CARD_REFERENCE_RETRY_MAX_RETRIES
    )

    rules = (
        ReachabilityRule(
            "transport-third-success",
            "nth_attempt_succeeds",
            "src/core/llm/base.py:MAX_PROVIDER_ATTEMPTS_PER_LOGICAL_CALL",
            "third physical attempt succeeds and control may continue",
            ("earlier physical success", "third failure stops"),
            MAX_PROVIDER_ATTEMPTS_PER_LOGICAL_CALL,
        ),
        ReachabilityRule(
            "schema-last-success",
            "nth_attempt_succeeds",
            "existing stage schema/business retry owners",
            "last bounded logical attempt succeeds",
            ("earlier success", "retry exhaustion stops"),
            outline_attempts,
        ),
        ReachabilityRule(
            "card-reference-contract",
            "bounded_budget",
            "ChapterCardService shared contract repair + reference retry",
            "initial full candidate, one repair candidate, strict reference candidate",
            ("any exhausted branch stops",),
            card_candidate_calls,
        ),
        ReachabilityRule(
            "chapter-revision-budget",
            "bounded_budget",
            "ChapterCard.max_revises/max_rewrites + GLOBAL no-progress owner",
            "draft + all revise + all rewrite budgets, final review passes",
            ("budget exhaustion escalates", "earlier approval"),
            versions_per_chapter,
        ),
        ReachabilityRule(
            "paragraph-reflow-exclusive",
            "mutually_exclusive",
            "chapter_orchestrator mechanical_reflow_only branch",
            "model revision budget is the higher-cost branch",
            ("pure-code paragraph reflow", "model revise/rewrite"),
            1,
        ),
        ReachabilityRule(
            "human-escalation-terminal",
            "terminal_stop",
            "chapter state machine HUMAN_INTERVENTION",
            "successful path continues only while budget remains",
            ("human escalation stops before summary/memory",),
        ),
        ReachabilityRule(
            "approved-downstream-only",
            "success_only_downstream",
            "chapter_orchestrator APPROVED branch",
            "summary/memory/mergers run only after approval",
            ("failure/human escalation has no downstream calls",),
        ),
    )

    nodes: list[ReachabilityStageNode] = [
        ReachabilityStageNode(
            "start",
            None,
            None,
            0,
            (),
            "future S2 arm start",
            path_kind="control",
        ),
        ReachabilityStageNode(
            "selection.protocol_smoke_json_object.excluded",
            "smoke",
            "low",
            0,
            ("start",),
            "S2-1 minimal Chat JSON Object smoke",
            path_kind="excluded",
            exclusion_reason=(
                "selection-test smoke is separately authorised and is not a "
                "formal production Run stage"
            ),
        ),
        ReachabilityStageNode(
            "selection.protocol_smoke_thinking_usage.excluded",
            "smoke",
            "medium",
            0,
            ("selection.protocol_smoke_json_object.excluded",),
            "S2-1 thinking/SSE usage smoke",
            path_kind="excluded",
            exclusion_reason=(
                "selection-test smoke is separately authorised and is not a "
                "formal production Run stage"
            ),
        ),
        _node(
            "outline.world",
            "outline_builder",
            outline_attempts,
            ("selection.protocol_smoke_thinking_usage.excluded",),
            "OutlineBuilder world schema/business attempts",
            "schema-last-success",
        ),
        _node(
            "outline.world_mechanism",
            "mechanism_consistency_validator",
            outline_attempts,
            ("outline.world",),
            "one mechanism Judge per reachable world candidate",
            "schema-last-success",
        ),
        _node(
            "outline.characters",
            "outline_builder",
            outline_attempts,
            ("outline.world_mechanism",),
            "OutlineBuilder characters attempts",
            "schema-last-success",
        ),
        _node(
            "outline.plot",
            "outline_builder",
            outline_attempts,
            ("outline.characters",),
            "OutlineBuilder plot attempts",
            "schema-last-success",
        ),
        _node(
            "outline.plot_setting",
            "setting_consistency_validator",
            outline_attempts * SETTING_CONSISTENCY_AUTHORITATIVE_MAX_ATTEMPTS,
            ("outline.plot",),
            "authoritative setting attempts per reachable plot candidate",
            "schema-last-success",
        ),
        _node(
            "outline.character_backfill_after_plot",
            "outline_builder",
            outline_attempts,
            ("outline.plot_setting",),
            "existing character-backfill helper uses outline route",
            "schema-last-success",
        ),
        _node(
            "outline.volume_build",
            "outline_builder",
            volume_rounds * outline_attempts,
            ("outline.character_backfill_after_plot",),
            "initial + consistency retry + arc-span retry volume rounds",
            "schema-last-success",
        ),
        _node(
            "outline.volume_setting",
            "setting_consistency_validator",
            volume_rounds
            * outline_attempts
            * SETTING_CONSISTENCY_AUTHORITATIVE_MAX_ATTEMPTS,
            ("outline.volume_build",),
            "authoritative setting attempts per volume candidate",
            "schema-last-success",
        ),
        _node(
            "outline.volume_refine",
            "outline_builder",
            volume_rounds * outline_attempts,
            ("outline.volume_setting",),
            "length-aware refine in every reachable volume round",
            "schema-last-success",
        ),
        _node(
            "outline.character_backfill_after_volume",
            "outline_builder",
            volume_rounds * outline_attempts,
            ("outline.volume_refine",),
            "volume-aware character backfill in every round",
            "schema-last-success",
        ),
        _node(
            "outline.consistency",
            "outline_consistency_validator",
            5,
            ("outline.character_backfill_after_volume",),
            "initial substantive failure + two later typo/autofix revalidation pairs",
            "schema-last-success",
        ),
        _node(
            "outline.mini_plan",
            "mini_plan_generator",
            volumes * outline_attempts,
            ("outline.consistency",),
            "one-volume mini-plan schema/business attempts",
            "schema-last-success",
        ),
        _node(
            "outline.mini_beat_alignment",
            "beat_alignment_validator",
            chapters * outline_attempts,
            ("outline.mini_plan",),
            "one beat Judge per chapter for every reachable mini-plan candidate",
            "schema-last-success",
        ),
        _node(
            "outline.mini_setting",
            "setting_consistency_validator",
            volumes
            * outline_attempts
            * SETTING_CONSISTENCY_AUTHORITATIVE_MAX_ATTEMPTS,
            ("outline.mini_beat_alignment",),
            "authoritative mini-plan setting retry",
            "schema-last-success",
        ),
        _node(
            "outline.thread_closure",
            "thread_closure_validator",
            1,
            ("outline.mini_setting",),
            "outline commit thread-closure gate",
            "transport-third-success",
        ),
        _node(
            "outline.identity_coverage",
            "identity_coverage_validator",
            1,
            ("outline.thread_closure",),
            "outline commit identity gate",
            "transport-third-success",
        ),
        ReachabilityStageNode(
            "outline.volume_seam.excluded",
            "volume_seam_replay_validator",
            "xhigh",
            0,
            ("outline.identity_coverage",),
            "volume seam requires two adjacent volumes",
            path_kind="excluded",
            exclusion_reason="frozen S2 arm contains exactly one volume",
        ),
        _node(
            "card.generate",
            "chapter_card_generator",
            chapters * card_candidate_calls,
            ("outline.volume_seam.excluded",),
            "initial + shared repair + strict reference candidates",
            "card-reference-contract",
        ),
        _node(
            "card.setting",
            "setting_consistency_validator",
            chapters
            * card_candidate_calls
            * SETTING_CONSISTENCY_AUTHORITATIVE_MAX_ATTEMPTS,
            ("card.generate",),
            "all initial/contract-repair/reference candidates rerun the full Setting Judge",
            "card-reference-contract",
        ),
        _node(
            "card.must_vs_forbidden",
            "must_vs_forbidden_validator",
            chapters * card_candidate_calls,
            ("card.generate",),
            "one must/forbidden Judge per initial/repair/reference candidate",
            "card-reference-contract",
        ),
        ReachabilityStageNode(
            "card.semantic_join",
            None,
            None,
            0,
            ("card.setting", "card.must_vs_forbidden"),
            "parallel card Judges fully drain before fixed-order merge",
            path_kind="control",
        ),
        _node(
            "chapter.must_feasibility_precheck",
            "must_feasibility_validator",
            max(chapters - 1, 0),
            ("card.semantic_join",),
            "fresh first chapter skips with no previous summary; later chapters call once",
            "human-escalation-terminal",
        ),
        _node(
            "chapter.writer.base",
            "writer",
            chapters * base_writer_calls_per_chapter,
            ("chapter.must_feasibility_precheck",),
            "initial draft plus ChapterCard.max_rewrites",
            "chapter-revision-budget",
        ),
        _node(
            "chapter.writer.global_no_progress_retry",
            "writer",
            chapters
            * rewrite_budget
            * GLOBAL_REWRITE_NO_PROGRESS_MAX_RETRIES,
            ("chapter.writer.base",),
            "one non-materialising same-source retry per GLOBAL rewrite",
            "chapter-revision-budget",
        ),
        _node(
            "chapter.writer.word_count_trim",
            "writer",
            chapters * base_writer_calls_per_chapter * DEFAULT_MAX_ATTEMPTS,
            ("chapter.writer.global_no_progress_retry",),
            "trim reuses writer route and may fail twice before normal review",
            "chapter-revision-budget",
        ),
        _node(
            "chapter.reviser",
            "reviser",
            chapters * revise_budget,
            ("chapter.writer.word_count_trim",),
            "ChapterCard.max_revises",
            "chapter-revision-budget",
        ),
        _node(
            "chapter.reviewer",
            "reviewer",
            chapters * versions_per_chapter * REVIEWER_MAX_ATTEMPTS,
            ("chapter.reviser",),
            "last parse attempt succeeds for every materialised version",
            "schema-last-success",
            "chapter-revision-budget",
        ),
        _node(
            "chapter.must_gate",
            "must_achieve_validator",
            chapters
            * versions_per_chapter
            * POST_REVIEW_GATE_LOGICAL_CALL_MAXIMA["must_achieve"],
            ("chapter.reviewer",),
            "authoritative must schema feedback retry",
            "schema-last-success",
        ),
        _node(
            "chapter.continuity_gate",
            "continuity_validator",
            max(chapters - 1, 0)
            * versions_per_chapter
            * POST_REVIEW_GATE_LOGICAL_CALL_MAXIMA["continuity"],
            ("chapter.reviewer",),
            "Q1-Q5 only when a previous chapter exists",
            "schema-last-success",
        ),
        _node(
            "chapter.forbidden_gate",
            "forbidden_crossing_validator",
            chapters
            * versions_per_chapter
            * POST_REVIEW_GATE_LOGICAL_CALL_MAXIMA["forbidden_crossing"],
            ("chapter.reviewer",),
            "one forbidden Judge per materialised version",
            "schema-last-success",
        ),
        _node(
            "chapter.setting_gate",
            "setting_consistency_validator",
            chapters
            * versions_per_chapter
            * POST_REVIEW_GATE_LOGICAL_CALL_MAXIMA["setting"],
            ("chapter.reviewer",),
            "authoritative setting technical retry per materialised version",
            "schema-last-success",
        ),
        _node(
            "chapter.dedup_gate",
            "cross_chapter_dedup_validator",
            max(chapters - 1, 0)
            * versions_per_chapter
            * POST_REVIEW_GATE_LOGICAL_CALL_MAXIMA["dedup"],
            ("chapter.reviewer",),
            "dedup requires previous approved body",
            "schema-last-success",
        ),
        _node(
            "chapter.ending_gate",
            "ending_tension_validator",
            versions_per_chapter
            * POST_REVIEW_GATE_LOGICAL_CALL_MAXIMA["ending"],
            ("chapter.reviewer",),
            "only the final chapter runs ending tension",
            "schema-last-success",
        ),
        ReachabilityStageNode(
            "chapter.post_review_join",
            None,
            None,
            0,
            (
                "chapter.must_gate",
                "chapter.continuity_gate",
                "chapter.forbidden_gate",
                "chapter.setting_gate",
                "chapter.dedup_gate",
                "chapter.ending_gate",
            ),
            "all started post-review futures drain; fixed chain order merges",
            path_kind="control",
        ),
        ReachabilityStageNode(
            "chapter.mechanical_reflow.excluded",
            None,
            None,
            0,
            ("chapter.post_review_join",),
            "sole paragraph-mean failure uses pure code and then re-review",
            ("paragraph-reflow-exclusive",),
            path_kind="excluded",
            exclusion_reason="mutually exclusive lower-cost branch",
        ),
        ReachabilityStageNode(
            "chapter.human_escalation.terminal",
            None,
            None,
            0,
            ("chapter.post_review_join",),
            "retry/budget exhaustion terminates without summary or memory",
            ("human-escalation-terminal",),
            path_kind="excluded",
            exclusion_reason="terminal branch is not combinable with successful downstream",
        ),
        ReachabilityStageNode(
            "chapter.approved",
            None,
            None,
            0,
            ("chapter.post_review_join",),
            "only the successful branch reaches approved",
            ("approved-downstream-only",),
            path_kind="control",
        ),
        _node(
            "memory.summary",
            "chapter_summary_extractor",
            chapters * CONTINUITY_LEDGER_SCHEMA_MAX_ATTEMPTS,
            ("chapter.approved",),
            "R4 strict continuity-ledger schema/business retry",
            "schema-last-success",
            "approved-downstream-only",
        ),
        _node(
            "memory.extract",
            "memory_extractor",
            chapters,
            ("memory.summary",),
            "one rich-memory extraction per approved chapter",
            "approved-downstream-only",
        ),
        _node(
            "memory.character_disambiguation",
            "character_disambiguator",
            chapters,
            ("memory.extract",),
            "one batched disambiguation per approved extraction",
            "approved-downstream-only",
        ),
        _node(
            "memory.plot_merge",
            "plot_thread_merger",
            chapters,
            ("memory.character_disambiguation",),
            "one batched plot merge per approved extraction",
            "approved-downstream-only",
        ),
        _node(
            "memory.timeline_merge",
            "timeline_entry_merger",
            max(chapters - 1, 0),
            ("memory.plot_merge",),
            "first chapter has no prior timeline candidate to merge",
            "approved-downstream-only",
        ),
        _node(
            "memory.scene_anchor",
            "scene_anchor_validator",
            max(chapters - 1, 0),
            ("memory.timeline_merge",),
            "scene-anchor comparison requires an earlier chapter",
            "approved-downstream-only",
        ),
        _node(
            "memory.final_plot_payoff",
            "thread_closure_validator",
            1,
            ("memory.scene_anchor",),
            "final chapter plot-thread payoff observation",
            "approved-downstream-only",
        ),
    ]

    # Registered but not invoked by the sequential NewBook→run graph.
    excluded = (
        (
            "chapter.reviewer_escalation.excluded",
            "reviewer_escalation",
            "the frozen S2 config has no explicit reviewer_escalation route",
        ),
        (
            "chapter.style_audit.excluded",
            "style_auditor",
            "style audit is a separate API operation, not sequential run control flow",
        ),
        (
            "chapter.reranker.excluded",
            "reranker",
            "writer main path explicitly does not invoke the specialist reranker",
        ),
        (
            "chapter.continuity_extract.excluded",
            "continuity",
            "legacy continuity enhancement is not invoked by the strict ledger run path",
        ),
        (
            "memory.health.excluded",
            "memory_health_checker",
            "memory health is a separate inspection endpoint",
        ),
    )
    for stage_id, agent_name, reason in excluded:
        nodes.append(
            ReachabilityStageNode(
                stage_id,
                agent_name,
                run_model_tier_for_agent(agent_name),
                0,
                ("memory.final_plot_payoff",),
                reason,
                path_kind="excluded",
                exclusion_reason=reason,
            )
        )
    nodes.append(
        ReachabilityStageNode(
            "completed",
            None,
            None,
            0,
            ("memory.final_plot_payoff",),
            "all three approved chapters and enrichment complete",
            path_kind="control",
        )
    )

    return S2ReachabilityDAG(
        nodes=tuple(nodes),
        rules=rules,
        chapters=chapters,
        volumes=volumes,
    )


__all__ = [
    "AGENT_PROMPT_RENDER_BOUNDS",
    "PROMPT_RENDER_BOUNDS",
    "RUN_MODEL_AGENT_TIER",
    "PromptRenderBound",
    "ReachabilityRule",
    "ReachabilityStageNode",
    "S2ReachabilityDAG",
    "S2SelectionTestRequestCorridor",
    "S2RequestCorridorComponent",
    "S2_ACTIVITY_CHAPTERS",
    "S2_ACTIVITY_VOLUMES",
    "V8_SANITY_PHYSICAL_ATTEMPTS",
    "build_s2_selection_test_request_corridor",
    "build_s2_reachability_dag",
    "conservative_rendered_input_tokens",
    "prompt_render_bound_for_agent",
    "run_model_tier_for_agent",
]
