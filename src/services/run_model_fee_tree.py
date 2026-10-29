"""Reachable Run consumption and future loss-breaker suggestions.

Only the formal one-volume/three-chapter Run graph is priced.  Provider
context windows are never treated as product input: each stage uses the
runtime-enforced rendered Chat byte ceiling from
``run_model_stage_registry``.  Schema/business/transport retries, revision
budgets, early stop, human escalation, and approved-only summary/memory are
therefore reconstructed from one typed reachability graph.

``max_physical_requests`` and ``max_configured_rmb`` belong only to an
explicit ``selection_test`` experiment scope.  They are loss breakers, not
forecasts or completion guarantees.  A production Run may use the same model
identity lock, but it never reads or inherits these two values.
"""

from __future__ import annotations

from dataclasses import dataclass
from decimal import Decimal, ROUND_CEILING
from typing import Literal

from src.core.llm.base import MAX_PROVIDER_ATTEMPTS_PER_LOGICAL_CALL
from src.core.models.run_model_manifest import (
    RunModelManifest,
    RunModelTierName,
)
from src.services.run_model_stage_registry import (
    PROMPT_RENDER_BOUNDS,
    V8_SANITY_PHYSICAL_ATTEMPTS,
    S2RequestCorridorComponent,
    build_s2_selection_test_request_corridor,
    build_s2_reachability_dag,
    prompt_render_bound_for_agent,
)


LOSS_BREAKER_COST_MARGIN_MULTIPLIER = Decimal("1.10")
LOSS_BREAKER_COST_ROUNDING_RMB = Decimal("5")


@dataclass(frozen=True, slots=True)
class FeeTreeNode:
    stage: str
    agent: str
    tier: RunModelTierName
    logical_calls: int
    source: str

    @property
    def physical_attempts(self) -> int:
        return self.logical_calls * MAX_PROVIDER_ATTEMPTS_PER_LOGICAL_CALL


@dataclass(frozen=True, slots=True)
class FeeTreeCostNode:
    node: FeeTreeNode
    input_reserve_tokens: int
    output_reserve_tokens: int
    input_reserve_rmb: Decimal
    output_reserve_rmb: Decimal

    @property
    def total_reserve_rmb(self) -> Decimal:
        return self.input_reserve_rmb + self.output_reserve_rmb


@dataclass(frozen=True, slots=True)
class ReachableRunConsumption:
    """Maximum successful reachable Run path with terminal branches excluded."""

    manifest_id: str
    nodes: tuple[FeeTreeCostNode, ...]
    stage_registry_hash: str
    proven: Literal[True] = True
    classification: Literal["formal_run_reachable_conservative"] = (
        "formal_run_reachable_conservative"
    )
    sanity_v8_physical_attempts: int = V8_SANITY_PHYSICAL_ATTEMPTS
    proof_gaps: tuple[str, ...] = ()

    @property
    def logical_calls(self) -> int:
        return sum(item.node.logical_calls for item in self.nodes)

    @property
    def physical_attempts(self) -> int:
        return sum(item.node.physical_attempts for item in self.nodes)

    @property
    def input_reserve_tokens(self) -> int:
        return sum(item.input_reserve_tokens for item in self.nodes)

    @property
    def output_reserve_tokens(self) -> int:
        return sum(item.output_reserve_tokens for item in self.nodes)

    @property
    def exact_rmb_reserve(self) -> Decimal:
        return sum(
            (item.total_reserve_rmb for item in self.nodes),
            start=Decimal("0"),
        )

    @property
    def conservative_rmb(self) -> Decimal:
        return self.exact_rmb_reserve.quantize(
            Decimal("0.01"), rounding=ROUND_CEILING
        )


@dataclass(frozen=True, slots=True)
class SelectionTestLossBreakerCostComponent:
    component: S2RequestCorridorComponent
    input_reserve_tokens: int
    output_reserve_tokens: int
    conservative_rmb: Decimal


@dataclass(frozen=True, slots=True)
class SelectionTestLossBreakerProposal:
    """Future selection-test decision; either breaker stops that experiment."""

    manifest_id: str
    suggested_max_physical_requests: int
    nominal_physical_requests: int
    recovery_physical_requests: int
    components: tuple[SelectionTestLossBreakerCostComponent, ...]
    corridor_conservative_rmb: Decimal
    cost_margin_multiplier: Decimal
    suggested_max_configured_rmb: Decimal
    authorised: Literal[False] = False
    completion_guaranteed: Literal[False] = False
    classification: Literal["future_maintainer_approval_required"] = (
        "future_maintainer_approval_required"
    )


@dataclass(frozen=True, slots=True)
class ConfiguredSelectionTestLossBreakers:
    """Two experiment-only stops under an explicit ``selection_test`` scope."""

    manifest_id: str
    max_physical_requests: int
    max_configured_rmb: Decimal

    @property
    def authorised(self) -> bool:
        return self.max_physical_requests > 0 and self.max_configured_rmb > 0


def build_reachable_run_consumption(
    manifest: RunModelManifest,
) -> ReachableRunConsumption:
    """Price every stage on the maximum successful formal Run path."""

    dag = build_s2_reachability_dag()
    million = Decimal(1_000_000)
    cost_nodes: list[FeeTreeCostNode] = []
    for stage in dag.counted_nodes:
        assert stage.tier is not None
        tier = manifest.tiers[stage.tier]
        node = FeeTreeNode(
            stage=stage.stage_id,
            agent=stage.agent_name or "control",
            tier=stage.tier,
            logical_calls=stage.logical_calls,
            source=(
                f"{stage.source}; reachability="
                f"{','.join(stage.rule_ids) or 'direct'}"
            ),
        )
        input_tokens = (
            node.physical_attempts * stage.render_input_tokens_per_attempt
        )
        output_tokens = node.physical_attempts * tier.max_completion_tokens
        cost_nodes.append(
            FeeTreeCostNode(
                node=node,
                input_reserve_tokens=input_tokens,
                output_reserve_tokens=output_tokens,
                input_reserve_rmb=(
                    Decimal(input_tokens)
                    * manifest.input_price_rmb_per_million
                    / million
                ),
                output_reserve_rmb=(
                    Decimal(output_tokens)
                    * manifest.output_price_rmb_per_million
                    / million
                ),
            )
        )

    return ReachableRunConsumption(
        manifest_id=manifest.manifest_id,
        nodes=tuple(cost_nodes),
        stage_registry_hash=dag.registry_hash(),
    )


def _ceil_to_increment(value: Decimal, increment: Decimal) -> Decimal:
    units = (value / increment).quantize(Decimal("1"), rounding=ROUND_CEILING)
    return units * increment


def _price_corridor_component(
    manifest: RunModelManifest,
    component: S2RequestCorridorComponent,
) -> SelectionTestLossBreakerCostComponent:
    tier = manifest.tiers[component.tier]
    physical = component.physical_attempts
    render_bound = (
        prompt_render_bound_for_agent(component.agent_name)
        if component.agent_name is not None
        else PROMPT_RENDER_BOUNDS[component.tier]
    )
    input_tokens = (
        physical
        * render_bound.max_accounting_input_tokens
    )
    output_tokens = physical * tier.max_completion_tokens
    million = Decimal(1_000_000)
    cost = (
        Decimal(input_tokens)
        * manifest.input_price_rmb_per_million
        / million
        + Decimal(output_tokens)
        * manifest.output_price_rmb_per_million
        / million
    )
    return SelectionTestLossBreakerCostComponent(
        component=component,
        input_reserve_tokens=input_tokens,
        output_reserve_tokens=output_tokens,
        conservative_rmb=cost,
    )


def propose_selection_test_loss_breakers(
    manifest: RunModelManifest,
) -> SelectionTestLossBreakerProposal:
    """Suggest two stop values from the typed nominal + recovery corridor.

    The recovery pool is fungible: outline/card/schema retries reduce the
    revision runway.  Every logical call is priced as three physical attempts
    with a full runtime render ceiling and full configured completion ceiling.
    A further 10% monetary margin is rounded upward to ¥5.  This is a maximum
    loss preference for future approval, not expected spend or assurance that
    the Run will finish.
    """

    corridor = build_s2_selection_test_request_corridor()
    priced = tuple(
        _price_corridor_component(manifest, component)
        for component in (
            *corridor.nominal_components,
            *corridor.recovery_components,
        )
    )
    corridor_cost = sum(
        (item.conservative_rmb for item in priced),
        start=Decimal("0"),
    )
    suggested_rmb = _ceil_to_increment(
        corridor_cost * LOSS_BREAKER_COST_MARGIN_MULTIPLIER,
        LOSS_BREAKER_COST_ROUNDING_RMB,
    )
    return SelectionTestLossBreakerProposal(
        manifest_id=manifest.manifest_id,
        suggested_max_physical_requests=(
            corridor.suggested_max_physical_requests
        ),
        nominal_physical_requests=corridor.nominal_physical_attempts,
        recovery_physical_requests=corridor.recovery_physical_attempts,
        components=priced,
        corridor_conservative_rmb=corridor_cost,
        cost_margin_multiplier=LOSS_BREAKER_COST_MARGIN_MULTIPLIER,
        suggested_max_configured_rmb=suggested_rmb,
    )


def configured_selection_test_loss_breakers(
    manifest: RunModelManifest,
) -> ConfiguredSelectionTestLossBreakers:
    """Read only the explicit ``selection_test`` experiment scope."""

    return ConfiguredSelectionTestLossBreakers(
        manifest_id=manifest.manifest_id,
        max_physical_requests=manifest.selection_test.max_physical_requests,
        max_configured_rmb=manifest.selection_test.max_configured_rmb,
    )


__all__ = [
    "ConfiguredSelectionTestLossBreakers",
    "FeeTreeCostNode",
    "FeeTreeNode",
    "SelectionTestLossBreakerCostComponent",
    "SelectionTestLossBreakerProposal",
    "ReachableRunConsumption",
    "build_reachable_run_consumption",
    "configured_selection_test_loss_breakers",
    "propose_selection_test_loss_breakers",
]
