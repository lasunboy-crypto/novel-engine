"""Writer 前可恢复的上游戏剧合同。

这些模型只承载显式字段与既有稳定 ID。自然文本仍保持可读，不使用前缀、
opaque token 或从文本反向解析机器状态。
"""

from __future__ import annotations

import hashlib
import json
from typing import Any, Literal

from pydantic import BaseModel, ConfigDict, Field, model_validator

from src.core.models import ID_PATTERN
from src.core.models.core_state import CoreStateRelationAttribute

DramaticFunction = Literal["confrontation", "turning", "emotional_peak", "finale"]
DramaticInteractionMode = Literal["direct", "indirect"]
DramaticPromiseOutcome = Literal["resolved", "explicitly_deferred"]
LoadBearingCausalRole = Literal[
    "enter",
    "open",
    "close",
    "escape",
    "irreversible_action",
]


class LoadBearingObjectContract(BaseModel):
    """plot 选出的承重物件稳定身份与可追踪关系。

    ``label`` / ``causal_requirement`` 只供人和 Writer 理解；机器身份只认
    ``object_id`` 与 typed turning/thread/relationship 字段。
    """

    model_config = ConfigDict(extra="forbid")

    object_id: str = Field(..., pattern=ID_PATTERN)
    label: str = Field(..., min_length=1, max_length=80)
    causal_role: LoadBearingCausalRole
    causal_requirement: str = Field(..., min_length=1, max_length=300)
    relation_attributes: list[CoreStateRelationAttribute] = Field(
        ..., min_length=1, max_length=3
    )
    linked_turning_id: str | None = Field(None, pattern=ID_PATTERN)
    linked_thread_id: str | None = Field(None, pattern=ID_PATTERN)

    @model_validator(mode="after")
    def _validate_identity_and_relations(self) -> "LoadBearingObjectContract":
        if self.linked_turning_id is None and self.linked_thread_id is None:
            raise ValueError(
                "承重物件至少绑定一个既有 linked_turning_id 或 linked_thread_id"
            )
        if len(self.relation_attributes) != len(set(self.relation_attributes)):
            raise ValueError("承重物件 relation_attributes 不得重复")
        return self


class VolumeDramaticPromise(BaseModel):
    """单卷唯一主戏剧承诺的身份与卷末裁决。"""

    model_config = ConfigDict(extra="forbid")

    primary_thread_id: str | None = Field(None, pattern=ID_PATTERN)
    primary_turning_id: str | None = Field(None, pattern=ID_PATTERN)
    actor_id: str = Field(..., pattern=ID_PATTERN)
    counterforce_id: str = Field(..., pattern=ID_PATTERN)
    outcome: DramaticPromiseOutcome
    deferred_to_volume_id: str | None = Field(None, pattern=ID_PATTERN)
    deferred_to_thread_id: str | None = Field(None, pattern=ID_PATTERN)

    @model_validator(mode="after")
    def _validate_identity_and_outcome(self) -> "VolumeDramaticPromise":
        if self.primary_thread_id is None and self.primary_turning_id is None:
            raise ValueError(
                "戏剧承诺至少绑定一个既有 primary_thread_id 或 primary_turning_id"
            )
        if self.actor_id == self.counterforce_id:
            raise ValueError("戏剧承诺 actor_id 与 counterforce_id 必须不同")
        deferred = (
            self.deferred_to_volume_id,
            self.deferred_to_thread_id,
        )
        if self.outcome == "resolved" and any(deferred):
            raise ValueError("outcome=resolved 时不得携带 deferred target")
        if self.outcome == "explicitly_deferred" and not all(deferred):
            raise ValueError(
                "outcome=explicitly_deferred 时必须同时给出目标 volume_id/thread_id"
            )
        return self


class DramaticEventFields(BaseModel):
    """v1保留原四项必达；v2显式事实事件与文学意图分开。"""
    contract_version: Literal[1, 2] = Field(1, exclude_if=lambda value: value == 1)
    required_events: list[str] | None = Field(None, exclude_if=lambda value: value is None)

    @model_validator(mode="after")
    def validate_event_version(self) -> "DramaticEventFields":
        if self.contract_version == 2:
            if not self.required_events or any(not text.strip() for text in self.required_events):
                raise ValueError("戏剧合同 v2 缺少明确事实事件")
            if len(set(self.required_events)) != len(self.required_events):
                raise ValueError("戏剧事实事件不得重复")
        elif self.required_events is not None:
            raise ValueError("旧戏剧合同不能静默加入事实事件")
        return self


class DramaticBeatContract(DramaticEventFields):
    """只供高价值 beat/mini-plan 使用的可执行戏剧单元。"""

    model_config = ConfigDict(extra="forbid")

    function: DramaticFunction
    actor_id: str = Field(..., pattern=ID_PATTERN)
    counterforce_id: str = Field(..., pattern=ID_PATTERN)
    interaction_mode: DramaticInteractionMode
    thread_id: str | None = Field(None, pattern=ID_PATTERN)
    turning_id: str | None = Field(None, pattern=ID_PATTERN)
    actor_goal: str = Field(..., min_length=1, max_length=300)
    counteraction: str = Field(..., min_length=1, max_length=300)
    choice: str = Field(..., min_length=1, max_length=300)
    consequence: str = Field(..., min_length=1, max_length=300)
    load_bearing_objects: list[LoadBearingObjectContract] = Field(
        default_factory=list,
        max_length=2,
        description=(
            "从 plot catalog 逐对象复制的承重物件；普通 beat 为空。"
        ),
    )

    @model_validator(mode="after")
    def _validate_identity(self) -> "DramaticBeatContract":
        if self.actor_id == self.counterforce_id:
            raise ValueError("戏剧 beat actor_id 与 counterforce_id 必须不同")
        if self.thread_id is None and self.turning_id is None:
            raise ValueError("戏剧 beat 至少绑定一个既有 thread_id 或 turning_id")
        object_ids = [item.object_id for item in self.load_bearing_objects]
        if len(object_ids) != len(set(object_ids)):
            raise ValueError("同一戏剧 beat 不得重复引用承重物件 object_id")
        for item in self.load_bearing_objects:
            linked = (
                self.turning_id is not None
                and item.linked_turning_id == self.turning_id
            ) or (
                self.thread_id is not None
                and item.linked_thread_id == self.thread_id
            )
            if not linked:
                raise ValueError(
                    f"承重物件 {item.object_id} 未与当前 beat 的 turning/thread 同源"
                )
        return self

    def natural_requirements(self) -> tuple[str, ...]:
        """只有事实进入必达；旧书保留原四项，不改历史来源哈希。"""
        if self.contract_version == 2:
            return tuple(self.required_events or ())
        return (
            self.actor_goal,
            self.counteraction,
            self.choice,
            self.consequence,
        )


def _binding_payload(
    *,
    volume_id: str,
    beat_id: str,
    promise: VolumeDramaticPromise,
    beat_contract: DramaticBeatContract,
) -> dict[str, Any]:
    return {
        "volume_id": volume_id,
        "beat_id": beat_id,
        "promise": promise.model_dump(mode="json"),
        "beat_contract": beat_contract.model_dump(mode="json"),
    }


def dramatic_source_hash(
    *,
    volume_id: str,
    beat_id: str,
    promise: VolumeDramaticPromise,
    beat_contract: DramaticBeatContract,
) -> str:
    """对完整 typed source 做 canonical SHA-256。"""

    encoded = json.dumps(
        _binding_payload(
            volume_id=volume_id,
            beat_id=beat_id,
            promise=promise,
            beat_contract=beat_contract,
        ),
        ensure_ascii=False,
        sort_keys=True,
        separators=(",", ":"),
    ).encode("utf-8")
    return hashlib.sha256(encoded).hexdigest()


class DramaticSourceBinding(BaseModel):
    """ChapterCard 持久化的上游源快照与完整性 hash。"""

    model_config = ConfigDict(extra="forbid")

    volume_id: str = Field(..., pattern=ID_PATTERN)
    beat_id: str = Field(..., pattern=ID_PATTERN)
    promise: VolumeDramaticPromise
    beat_contract: DramaticBeatContract
    source_hash: str = Field(..., pattern=r"^[0-9a-f]{64}$")

    @model_validator(mode="after")
    def _validate_source_hash(self) -> "DramaticSourceBinding":
        expected = dramatic_source_hash(
            volume_id=self.volume_id,
            beat_id=self.beat_id,
            promise=self.promise,
            beat_contract=self.beat_contract,
        )
        if self.source_hash != expected:
            raise ValueError("dramatic source_hash 与 typed source 不匹配")
        return self


def build_dramatic_source_binding(
    *,
    volume_id: str,
    beat_id: str,
    promise: VolumeDramaticPromise,
    beat_contract: DramaticBeatContract,
    mini_plan_contract: DramaticBeatContract,
) -> DramaticSourceBinding:
    """只在 beat 与 mini-plan 完全同源时建立 ChapterCard binding。"""

    if beat_contract != mini_plan_contract:
        raise ValueError("mini-plan dramatic_contract 必须与所属 beat 完全一致")
    return DramaticSourceBinding(
        volume_id=volume_id,
        beat_id=beat_id,
        promise=promise,
        beat_contract=beat_contract,
        source_hash=dramatic_source_hash(
            volume_id=volume_id,
            beat_id=beat_id,
            promise=promise,
            beat_contract=beat_contract,
        ),
    )


__all__ = [
    "build_dramatic_source_binding",
    "dramatic_source_hash",
    "DramaticBeatContract",
    "DramaticFunction",
    "DramaticInteractionMode",
    "DramaticPromiseOutcome",
    "DramaticSourceBinding",
    "LoadBearingCausalRole",
    "LoadBearingObjectContract",
    "VolumeDramaticPromise",
]
