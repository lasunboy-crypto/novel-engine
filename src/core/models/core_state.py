"""全书承重核心状态与主时间线书内时钟的数据合同。

本模块只做无 IO 的身份、时间区间、hash 与 Writer/Judge 共用胶囊计算。
数据库写入、ChapterSummary 候选准入和 Rewind 生命周期由
``src.services.core_state_service`` 统一负责。
"""

from __future__ import annotations

import hashlib
import json
import re
from collections.abc import Sequence
from typing import TYPE_CHECKING, Any, Literal

from pydantic import (
    BaseModel,
    ConfigDict,
    Field,
    ValidationInfo,
    model_serializer,
    model_validator,
)
from typing_extensions import NotRequired, Required, TypedDict

from src.core.models import ID_PATTERN

if TYPE_CHECKING:
    from src.core.models.drama import DramaticSourceBinding

CORE_STATE_TRANSITION_LIMIT = 2


CoreStateEntityType = Literal["object", "character"]
CoreStateRelationAttribute = Literal["possession", "control", "access"]
CoreStateWriteAttribute = Literal[
    "physical_form",
    "surface",
    "possession",
    "control",
    "access",
    "location",
    "integrity",
    "seal_status",
    "open_state",
    "identity",
    "life_status",
    "permanent_injury",
    "amnesia",
    "faction",
]
CoreStateAttribute = Literal[
    "physical_form",
    "surface",
    "holder",
    "possession",
    "control",
    "access",
    "location",
    "integrity",
    "seal_status",
    "open_state",
    "identity",
    "life_status",
    "permanent_injury",
    "amnesia",
    "faction",
]
CoreStateAdmissionStatus = Literal[
    "proposed",
    "admitted",
    "soft_ambiguous",
    "soft_not_core",
    "rejected_conflict",
    "rejected_invalid",
    "reaffirmed",
]
BookTimeKind = Literal[
    "exact",
    "bounded_interval",
    "ordered_relative",
    "genuinely_unanchored",
]
BookTimeRelationKind = Literal[
    "same_instant",
    "after",
    "same_narrative_day",
    "same_night",
    "next_narrative_day",
    "resume_anchor",
]
BookTimeDayPhase = Literal[
    "morning",
    "daytime",
    "afternoon",
    "night",
    "around_midnight",
    "pre_dawn",
]
BookTimeUnanchoredReason = Literal[
    "dream",
    "flashback",
    "inserted_memory",
    "non_mainline",
    "unspecified",
    "legacy_unknown",
]
BookTimeQuantityUnit = Literal["minute", "hour", "day", "week", "month", "year"]

OBJECT_CORE_ATTRIBUTES = frozenset(
    {
        "physical_form",
        "surface",
        "holder",
        "possession",
        "control",
        "access",
        "location",
        "integrity",
        "seal_status",
        "open_state",
    }
)
CHARACTER_CORE_ATTRIBUTES = frozenset(
    {"identity", "life_status", "permanent_injury", "amnesia", "faction"}
)
CORE_STATE_RELATION_ATTRIBUTES = frozenset({"possession", "control", "access"})

CORE_STATE_TARGET_GROUPS = 8
CORE_STATE_TARGET_CHARS = 1000
CORE_STATE_HARD_MAX_GROUPS = 12
CORE_STATE_HARD_MAX_CHARS = 1500


class CoreStateContractError(ValueError):
    """核心状态合同无法确定性建立或发生来源漂移。"""


class CoreStateDensityError(CoreStateContractError):
    """HARD 投影超过安全注意力预算；禁止静默截断。"""


def make_state_key(
    *, entity_type: CoreStateEntityType, entity_id: str, attribute: str
) -> str:
    """生成跨章节稳定状态键；保留人读身份而不是随机 UUID。"""

    normalized_id = " ".join(entity_id.strip().split())
    normalized_attribute = attribute.strip()
    if not normalized_id or not normalized_attribute:
        raise CoreStateContractError("核心状态 entity_id/attribute 不得为空")
    return f"{entity_type}:{normalized_id}:{normalized_attribute}"


class CoreStateTransition(BaseModel):
    """章节卡对当前状态的一次预授权 ``from → to`` 变化。

    它只是未来意图；只有同一章批准正文的 exact evidence 被确定性准入后，
    才会成为当前事实。
    """

    model_config = ConfigDict(extra="forbid")

    state_key: str = ""
    entity_type: CoreStateEntityType
    entity_id: str = Field(min_length=1, max_length=80)
    attribute: CoreStateAttribute
    from_value: str | None = Field(default=None, max_length=160)
    to_value: str = Field(min_length=1, max_length=160)
    reason: str = Field(min_length=1, max_length=240)

    @model_validator(mode="after")
    def _validate_identity_and_attribute(
        self, info: ValidationInfo
    ) -> CoreStateTransition:
        allowed = (
            OBJECT_CORE_ATTRIBUTES
            if self.entity_type == "object"
            else CHARACTER_CORE_ATTRIBUTES
        )
        if self.attribute not in allowed:
            raise ValueError(f"{self.entity_type} 不允许核心属性 {self.attribute!r}")
        context = info.context if isinstance(info.context, dict) else {}
        if self.attribute == "holder" and not context.get(
            "legacy_chapter_card_storage"
        ):
            raise ValueError(
                "object.holder 只允许 legacy 章节卡只读兼容；"
                "新合同必须使用 possession/control/access"
            )
        if self.attribute in CORE_STATE_RELATION_ATTRIBUTES:
            if self.to_value is None:
                raise ValueError(
                    f"关系属性 {self.attribute} 必须给出 to_value stable ID"
                )
            for field, value in (
                ("from_value", self.from_value),
                ("to_value", self.to_value),
            ):
                if value is not None and re.fullmatch(ID_PATTERN, value) is None:
                    raise ValueError(
                        f"关系属性 {self.attribute} 的 {field} 必须是原子 stable ID"
                    )
        expected = make_state_key(
            entity_type=self.entity_type,
            entity_id=self.entity_id,
            attribute=self.attribute,
        )
        if self.state_key and self.state_key != expected:
            raise ValueError("core state transition state_key 与实体身份不一致")
        object.__setattr__(self, "state_key", expected)
        if (
            self.from_value is not None
            and self.from_value.strip() == self.to_value.strip()
            and self.attribute not in CORE_STATE_RELATION_ATTRIBUTES
        ):
            raise ValueError("核心状态迁移 from_value 与 to_value 不得相同")
        return self


class CoreStateWriteTransition(CoreStateTransition):
    """新卡/新 Agent 输出使用的迁移 schema。

    持久化历史仍由 ``CoreStateTransition`` 读取 ``holder``；新 wire
    schema 不暴露该旧字段，从源头避免模型继续生产混合语义。
    """

    attribute: CoreStateWriteAttribute


class CoreStateCandidate(BaseModel):
    """ChapterSummaryExtractor 提出的候选；模型没有最终准入权。"""

    model_config = ConfigDict(extra="forbid")

    state_key: str = ""
    entity_type: CoreStateEntityType
    entity_id: str = Field(min_length=1, max_length=80)
    attribute: CoreStateAttribute
    value: str = Field(min_length=1, max_length=160)
    objective: bool
    long_lived: bool
    book_bearing: bool
    ambiguous: bool = False
    evidence_quote: str = Field(min_length=1)
    evidence_fingerprint: str = Field(pattern=r"^[0-9a-f]{64}$")
    admission_status: CoreStateAdmissionStatus = "proposed"
    admission_reason: str = ""

    @model_validator(mode="after")
    def _validate_identity_and_attribute(self) -> CoreStateCandidate:
        expected = make_state_key(
            entity_type=self.entity_type,
            entity_id=self.entity_id,
            attribute=self.attribute,
        )
        if self.state_key and self.state_key != expected:
            raise ValueError("core state candidate state_key 与实体身份不一致")
        object.__setattr__(self, "state_key", expected)
        allowed = (
            OBJECT_CORE_ATTRIBUTES
            if self.entity_type == "object"
            else CHARACTER_CORE_ATTRIBUTES
        )
        if self.attribute not in allowed:
            raise ValueError(f"{self.entity_type} 不允许核心属性 {self.attribute!r}")
        if (
            self.attribute in CORE_STATE_RELATION_ATTRIBUTES
            and re.fullmatch(ID_PATTERN, self.value) is None
        ):
            raise ValueError(
                f"关系属性 {self.attribute} 的 candidate value 必须是原子 stable ID"
            )
        return self


class BookClockInterval(BaseModel):
    """相对开篇的主时间线闭区间，单位为分钟。"""

    model_config = ConfigDict(extra="forbid", frozen=True)

    minimum_minutes: int = Field(ge=0)
    maximum_minutes: int = Field(ge=0)

    @model_validator(mode="after")
    def _validate_order(self) -> BookClockInterval:
        if self.minimum_minutes > self.maximum_minutes:
            raise ValueError("book clock minimum_minutes 不得大于 maximum_minutes")
        return self

    def add(self, other: BookClockInterval) -> BookClockInterval:
        return BookClockInterval(
            minimum_minutes=self.minimum_minutes + other.minimum_minutes,
            maximum_minutes=self.maximum_minutes + other.maximum_minutes,
        )


class BookTimeApproximateQuantity(BaseModel):
    """原文给出的近似量；只供展示/语义判断，绝不派生确定性 bounds。"""

    model_config = ConfigDict(extra="forbid", frozen=True)

    value: int = Field(gt=0)
    unit: BookTimeQuantityUnit


class BookTimeNode(BaseModel):
    """一章冻结的书内时间节点；自然语言仍是唯一展示 source。"""

    model_config = ConfigDict(extra="forbid", frozen=True)

    node_id: str = Field(pattern=r"^[0-9a-f]{64}$")
    chapter_id: str = Field(min_length=1)
    kind: BookTimeKind
    raw_time_gap: str = Field(min_length=1)
    anchor_node_id: str | None = Field(default=None, pattern=r"^[0-9a-f]{64}$")
    relations: tuple[BookTimeRelationKind, ...] = ()
    day_phase: BookTimeDayPhase | None = None
    approximate_quantity: BookTimeApproximateQuantity | None = None
    gap_interval: BookClockInterval | None = None
    absolute_interval: BookClockInterval | None = None
    unanchored_reason: BookTimeUnanchoredReason | None = None

    @model_validator(mode="after")
    def _validate_kind_shape(self) -> BookTimeNode:
        if len(self.relations) != len(set(self.relations)):
            raise ValueError("book time relations 不得重复")
        if self.relations != _canonical_relations(self.relations):
            raise ValueError("book time relations 必须使用 closed-set canonical 顺序")
        relation_set = set(self.relations)
        if {"same_instant", "after"} <= relation_set:
            raise ValueError("same_instant 与 after 关系互斥")
        if "next_narrative_day" in relation_set and relation_set.intersection(
            {"same_narrative_day", "same_night"}
        ):
            raise ValueError("next_narrative_day 不得与同日/同夜关系并存")
        if "resume_anchor" in relation_set and relation_set.intersection(
            {"same_instant", "next_narrative_day"}
        ):
            raise ValueError("resume_anchor 不得与同刻/次日关系并存")
        if self.kind == "genuinely_unanchored":
            if (
                self.unanchored_reason is None
                or self.anchor_node_id is not None
                or self.relations
                or self.gap_interval is not None
                or self.absolute_interval is not None
                or self.approximate_quantity is not None
            ):
                raise ValueError("genuinely_unanchored 节点字段形态不合法")
            return self
        if self.unanchored_reason is not None:
            raise ValueError("主线时间节点不得携带 unanchored_reason")
        if self.kind == "ordered_relative":
            if (
                not self.relations
                or self.anchor_node_id is None
                or self.gap_interval is not None
                or self.absolute_interval is not None
            ):
                raise ValueError("ordered_relative 必须有 anchor/relation 且不得伪造数值区间")
            return self
        if self.approximate_quantity is not None:
            raise ValueError("source-level approximate quantity 只属于 ordered_relative")
        if self.kind == "exact":
            if self.gap_interval is not None:
                if (
                    self.gap_interval.minimum_minutes
                    != self.gap_interval.maximum_minutes
                ):
                    raise ValueError("exact 明确数值时长必须是退化单点 interval")
            elif self.absolute_interval is not None:
                if (
                    self.anchor_node_id is not None
                    or self.relations
                    or self.absolute_interval.minimum_minutes
                    != self.absolute_interval.maximum_minutes
                ):
                    raise ValueError("exact 开篇钟面点只能建立单点 absolute origin")
            elif self.anchor_node_id is None or not self.relations:
                raise ValueError("exact 钟面点必须有 anchor 与 closed relation")
            return self
        if self.kind == "bounded_interval":
            if self.gap_interval is not None:
                if (
                    self.gap_interval.minimum_minutes
                    >= self.gap_interval.maximum_minutes
                ):
                    raise ValueError("bounded_interval 必须是原文明示的非退化闭区间")
            elif self.anchor_node_id is None or not self.relations:
                raise ValueError("bounded 钟面范围必须有 anchor 与 closed relation")
            return self
        raise ValueError(f"未知 book-time kind: {self.kind}")


class BookTimeAbsoluteAnchor(BaseModel):
    """最近一个能由 explicit numeric facts 确定的主线绝对锚。"""

    model_config = ConfigDict(extra="forbid", frozen=True)

    node_id: str = Field(pattern=r"^[0-9a-f]{64}$")
    interval: BookClockInterval


def compute_book_time_relation_chain_hash(
    *,
    latest_absolute_anchor: BookTimeAbsoluteAnchor | None,
    active_relation_chain: tuple[BookTimeNode, ...],
) -> str:
    payload = {
        "latest_absolute_anchor": (
            latest_absolute_anchor.model_dump(mode="json")
            if latest_absolute_anchor is not None
            else None
        ),
        "active_relation_chain": [
            node.model_dump(mode="json") for node in active_relation_chain
        ],
    }
    canonical = json.dumps(
        payload, ensure_ascii=False, sort_keys=True, separators=(",", ":")
    )
    return hashlib.sha256(canonical.encode("utf-8")).hexdigest()


class BookTimeProjection(BaseModel):
    """当前 typed 书内时间节点、最近绝对锚与未换算关系链。"""

    model_config = ConfigDict(extra="forbid", frozen=True)

    contract_version: Literal[0, 1] = 1
    current_node: BookTimeNode | None = None
    latest_mainline_node_id: str | None = Field(
        default=None, pattern=r"^[0-9a-f]{64}$"
    )
    latest_absolute_anchor: BookTimeAbsoluteAnchor | None = None
    active_relation_chain: tuple[BookTimeNode, ...] = ()
    relation_chain_hash: str = Field(pattern=r"^[0-9a-f]{64}$")

    @model_validator(mode="after")
    def _validate_chain(self) -> BookTimeProjection:
        expected = compute_book_time_relation_chain_hash(
            latest_absolute_anchor=self.latest_absolute_anchor,
            active_relation_chain=self.active_relation_chain,
        )
        if self.relation_chain_hash != expected:
            raise ValueError("book time relation_chain_hash 不匹配内容")
        if self.active_relation_chain and self.latest_absolute_anchor is None:
            raise ValueError("book time relation chain 缺少 absolute anchor")
        predecessor = (
            self.latest_absolute_anchor.node_id
            if self.latest_absolute_anchor is not None
            else None
        )
        for node in self.active_relation_chain:
            if node.kind == "genuinely_unanchored":
                raise ValueError("unanchored 节点不得进入 active relation chain")
            if node.anchor_node_id != predecessor:
                raise ValueError("book time relation chain anchor 不连续")
            predecessor = node.node_id
        expected_mainline = predecessor
        if self.latest_mainline_node_id != expected_mainline:
            raise ValueError("latest_mainline_node_id 与 relation chain head 不一致")
        if self.current_node is not None and self.current_node.kind != "genuinely_unanchored":
            if self.current_node.node_id != self.latest_mainline_node_id:
                raise ValueError("当前主线节点不是 relation chain head")
        return self


class BookClockIntervalWire(TypedDict):
    minimum_minutes: int
    maximum_minutes: int


class BookTimeApproximateQuantityWire(TypedDict):
    value: int
    unit: BookTimeQuantityUnit


class BookClockConstraintWire(TypedDict, total=False):
    """versioned serializer 的显式 OpenAPI 输出；v0 可省略所有 v1 字段。"""

    contract_version: NotRequired[Literal[0, 1]]
    time_gap: Required[str]
    previous_interval: Required[BookClockIntervalWire | None]
    gap_interval: Required[BookClockIntervalWire | None]
    current_interval: Required[BookClockIntervalWire | None]
    kind: NotRequired[BookTimeKind | None]
    node_id: NotRequired[str]
    node_chapter_id: NotRequired[str]
    anchor_node_id: NotRequired[str | None]
    relations: NotRequired[Sequence[BookTimeRelationKind]]
    day_phase: NotRequired[BookTimeDayPhase | None]
    approximate_quantity: NotRequired[BookTimeApproximateQuantityWire | None]
    unanchored_reason: NotRequired[BookTimeUnanchoredReason | None]
    relation_chain_hash: NotRequired[str]
    is_hard: Required[bool]
    soft_reason: Required[str]


class BookClockConstraint(BaseModel):
    """章节卡冻结的代码换算结果；Writer 与 continuity Q3 原样共用。"""

    model_config = ConfigDict(extra="forbid", frozen=True)

    contract_version: Literal[0, 1] = 0
    time_gap: str = Field(min_length=1)
    previous_interval: BookClockInterval | None = None
    gap_interval: BookClockInterval | None = None
    current_interval: BookClockInterval | None = None
    kind: BookTimeKind | None = None
    node_id: str = Field(default="", pattern=r"^(?:|[0-9a-f]{64})$")
    node_chapter_id: str = ""
    anchor_node_id: str | None = Field(default=None, pattern=r"^[0-9a-f]{64}$")
    relations: tuple[BookTimeRelationKind, ...] = ()
    day_phase: BookTimeDayPhase | None = None
    approximate_quantity: BookTimeApproximateQuantity | None = None
    unanchored_reason: BookTimeUnanchoredReason | None = None
    relation_chain_hash: str = Field(default="", pattern=r"^(?:|[0-9a-f]{64})$")
    is_hard: bool
    soft_reason: str = ""

    @model_validator(mode="after")
    def _validate_hard_shape(self) -> BookClockConstraint:
        if self.contract_version == 1:
            if (
                self.kind is None
                or not self.node_id
                or not self.node_chapter_id
                or not self.relation_chain_hash
            ):
                raise ValueError("book clock v1 缺少 typed kind/node/chain hash")
            BookTimeNode(
                node_id=self.node_id,
                chapter_id=self.node_chapter_id,
                kind=self.kind,
                raw_time_gap=self.time_gap,
                anchor_node_id=self.anchor_node_id,
                relations=self.relations,
                day_phase=self.day_phase,
                approximate_quantity=self.approximate_quantity,
                gap_interval=self.gap_interval,
                absolute_interval=self.current_interval,
                unanchored_reason=self.unanchored_reason,
            )
            expected_hard = self.kind != "genuinely_unanchored"
            if self.is_hard != expected_hard:
                raise ValueError("book clock v1 is_hard 与 typed kind 不一致")
            if self.is_hard and self.soft_reason:
                raise ValueError("HARD book clock 不得携带 soft_reason")
            if not self.is_hard and not self.soft_reason:
                raise ValueError("unanchored book clock 必须说明 SOFT 原因")
            return self
        complete = self.gap_interval is not None and self.current_interval is not None
        if self.is_hard != complete:
            raise ValueError("book clock is_hard 与可换算区间不一致")
        if self.is_hard and self.soft_reason:
            raise ValueError("HARD book clock 不得携带 soft_reason")
        if not self.is_hard and not self.soft_reason:
            raise ValueError("SOFT book clock 必须说明未硬化原因")
        return self

    @model_serializer(mode="wrap", return_type=BookClockConstraintWire)
    def _serialize_versioned(self, handler: Any) -> dict[str, Any]:
        data = handler(self)
        if self.contract_version == 0:
            for field in (
                "contract_version",
                "kind",
                "node_id",
                "node_chapter_id",
                "anchor_node_id",
                "relations",
                "day_phase",
                "approximate_quantity",
                "unanchored_reason",
                "relation_chain_hash",
            ):
                data.pop(field, None)
        return data


class CoreStateFact(BaseModel):
    """投影给下游的一条当前有效事实。"""

    model_config = ConfigDict(extra="forbid", frozen=True)

    revision_id: str = Field(min_length=1)
    state_key: str = Field(min_length=1)
    entity_type: CoreStateEntityType
    entity_id: str = Field(min_length=1)
    attribute: CoreStateAttribute
    value: str = Field(min_length=1)
    source_kind: Literal[
        "approved_text", "chapter_card", "manual_retcon", "outline_characters"
    ]
    source_chapter_id: str = Field(min_length=1)
    source_version_id: int = Field(ge=0)
    source_path: str = Field(min_length=1)
    source_text_hash: str = Field(pattern=r"^[0-9a-f]{64}$")
    evidence_quote: str = Field(min_length=1)
    valid_from_chapter: int = Field(ge=0)
    valid_to_chapter: int | None = Field(default=None, ge=0)

    @model_validator(mode="after")
    def _validate_relation_value(self) -> CoreStateFact:
        if (
            self.attribute in CORE_STATE_RELATION_ATTRIBUTES
            and re.fullmatch(ID_PATTERN, self.value) is None
        ):
            raise ValueError(
                f"关系属性 {self.attribute} 的 fact value 必须是原子 stable ID"
            )
        return self


class CoreStateProjection(BaseModel):
    """某张章节卡写作前看到的唯一当前状态投影。"""

    model_config = ConfigDict(extra="forbid", frozen=True)

    facts: tuple[CoreStateFact, ...] = ()
    book_clock: BookClockInterval | None = None
    book_time_contract_version: Literal[0, 1] = 0
    book_time: BookTimeProjection | None = None
    projection_hash: str = Field(pattern=r"^[0-9a-f]{64}$")

    @model_validator(mode="after")
    def _validate_hash_and_uniqueness(self) -> CoreStateProjection:
        keys = [item.state_key for item in self.facts]
        if len(keys) != len(set(keys)):
            raise ValueError("core state projection 存在重复 active state_key")
        if self.book_time_contract_version == 1 and (
            self.book_time is None or self.book_time.contract_version != 1
        ):
            raise ValueError("core state typed book_time v1 内容缺失")
        expected = compute_core_state_projection_hash(
            facts=self.facts,
            book_clock=self.book_clock,
            book_time=self.book_time,
            book_time_contract_version=self.book_time_contract_version,
        )
        if self.projection_hash != expected:
            raise ValueError("core state projection_hash 不匹配内容")
        return self


def canonical_core_state_value(value: str) -> str:
    """归一化状态值的无语义空白，供所有层使用同一把 from/to 判尺。"""

    return " ".join(value.strip().split())


def validate_core_state_transition_sources(
    *,
    transitions: list[CoreStateTransition] | tuple[CoreStateTransition, ...],
    projection: CoreStateProjection,
    dramatic_source: DramaticSourceBinding | None = None,
    related_character_ids: Sequence[str] = (),
) -> None:
    """在任何付费 Judge 前验证 ``from_value`` 确实来自当前投影。

    本函数无 IO；ChapterCardGenerator、批准入口与 Writer 前复核共用。没有
    当前事实时只能用 ``from_value=null`` 建立初始状态，禁止卡片根据摘要或
    正文软记忆猜造旧值。
    """

    current = {fact.state_key: fact for fact in projection.facts}
    current_object_ids = {
        fact.entity_id for fact in projection.facts if fact.entity_type == "object"
    }
    new_object_ids = {
        transition.entity_id
        for transition in transitions
        if transition.entity_type == "object"
        and transition.attribute in CORE_STATE_RELATION_ATTRIBUTES
        and transition.entity_id not in current_object_ids
    }
    if len(new_object_ids) > 1:
        from src.core.models.drama import DramaticSourceBinding

        source = None
        if dramatic_source is not None:
            try:
                source = DramaticSourceBinding.model_validate(dramatic_source.model_dump(mode="json"))
            except ValueError as exc:
                raise CoreStateContractError("双物件登记的 typed 来源或 hash 无效") from exc
        catalog = {item.object_id: item for item in source.beat_contract.load_bearing_objects} if source else {}
        if (len(new_object_ids) != 2 or set(catalog) != new_object_ids
                or len(transitions) > CORE_STATE_TRANSITION_LIMIT):
            raise CoreStateContractError(
                "每章默认最多引入 1 个新承重物件；仅允许本章来源验真的两个 typed 物件例外；"
                f"本章声明了 {sorted(new_object_ids)}"
            )
        for transition in transitions:
            contract = catalog.get(transition.entity_id)
            if (contract is None or transition.entity_type != "object"
                    or transition.attribute not in contract.relation_attributes
                    or transition.to_value not in related_character_ids):
                raise CoreStateContractError("双物件登记的身份、关系属性或目标未由本章来源授权")
    for transition in transitions:
        if transition.attribute == "holder":
            raise CoreStateContractError(
                "object.holder 只允许 legacy 读取；新迁移必须拆为 "
                "possession/control/access"
            )
        active = current.get(transition.state_key)
        if active is None:
            if transition.from_value is not None:
                raise CoreStateContractError(
                    f"迁移 {transition.state_key} 声明 from_value，但当前没有该事实"
                )
            continue
        if transition.from_value is None:
            raise CoreStateContractError(
                f"迁移 {transition.state_key} 缺少当前 from_value={active.value!r}"
            )
        if canonical_core_state_value(
            transition.from_value
        ) != canonical_core_state_value(active.value):
            raise CoreStateContractError(
                f"迁移 {transition.state_key} from_value 已陈旧: "
                f"card={transition.from_value!r} current={active.value!r}"
            )


class CoreStatePromptCapsule(BaseModel):
    """同一份投影的紧凑 prompt 表面与密度观测。"""

    model_config = ConfigDict(extra="forbid", frozen=True)

    text: str
    group_count: int = Field(ge=0)
    char_count: int = Field(ge=0)
    above_target: bool


_CN_DIGITS = {
    "零": 0,
    "〇": 0,
    "一": 1,
    "二": 2,
    "两": 2,
    "三": 3,
    "四": 4,
    "五": 5,
    "六": 6,
    "七": 7,
    "八": 8,
    "九": 9,
}


def _parse_cn_number(raw: str) -> int | None:
    text = raw.strip()
    if text.isdigit():
        return int(text)
    if text in _CN_DIGITS:
        return _CN_DIGITS[text]
    if text == "十":
        return 10
    if "十" in text and len(text) <= 3:
        left, right = text.split("十", 1)
        tens = _CN_DIGITS.get(left, 1) if left else 1
        ones = _CN_DIGITS.get(right, 0) if right else 0
        return tens * 10 + ones
    return None


_EXACT_GAP_RE = re.compile(
    r"(?P<number>\d+|[零〇一二两三四五六七八九十]{1,3})\s*"
    r"(?P<unit>分钟|分|小时|时|天|日|周|星期|个月|月|年)"
)
_RANGE_GAP_RE = re.compile(
    r"(?P<minimum>\d+|[零〇一二两三四五六七八九十]{1,3})\s*"
    r"(?:至|到|—|-)\s*"
    r"(?P<maximum>\d+|[零〇一二两三四五六七八九十]{1,3})\s*"
    r"(?P<unit>分钟|分|小时|时|天|日|周|星期|个月|月|年)"
)

_GAP_UNIT_MINUTES = {
    "分钟": 1,
    "分": 1,
    "小时": 60,
    "时": 60,
    "天": 24 * 60,
    "日": 24 * 60,
    "周": 7 * 24 * 60,
    "星期": 7 * 24 * 60,
    "个月": 30 * 24 * 60,
    "月": 30 * 24 * 60,
    "年": 365 * 24 * 60,
}

_BOOK_TIME_RELATION_ORDER: dict[BookTimeRelationKind, int] = {
    "same_instant": 0,
    "after": 1,
    "resume_anchor": 2,
    "same_narrative_day": 3,
    "same_night": 4,
    "next_narrative_day": 5,
}
_BOOK_TIME_UNIT: dict[str, BookTimeQuantityUnit] = {
    "分钟": "minute",
    "分": "minute",
    "小时": "hour",
    "时": "hour",
    "天": "day",
    "日": "day",
    "周": "week",
    "星期": "week",
    "个月": "month",
    "月": "month",
    "年": "year",
}
_APPROXIMATE_QUANTITY_RE = re.compile(
    r"(?:大约|约|将近)\s*"
    r"(?P<number>\d+|[零〇一二两三四五六七八九十]{1,3})\s*"
    r"(?P<unit>分钟|分|小时|时|天|日|周|星期|个月|月|年)"
)
_EXPLICIT_CLOCK_POINT_RE = re.compile(
    r"(?:凌晨|清晨|早晨|上午|中午|午后|下午|傍晚|夜间|夜里|深夜|午夜)?\s*"
    r"(?:\d{1,2}|[零〇一二两三四五六七八九十]{1,3})\s*(?:点|时)"
    r"(?:\s*(?:\d{1,2}|[零〇一二两三四五六七八九十]{1,3})\s*分)?"
    r"(?:整|正)?"
)
_EXPLICIT_CLOCK_RANGE_RE = re.compile(
    r"(?:\d{1,2}|[零〇一二两三四五六七八九十]{1,3})\s*(?:点|时)\s*"
    r"(?:至|到|—|-)\s*"
    r"(?:\d{1,2}|[零〇一二两三四五六七八九十]{1,3})\s*(?:点|时)"
)


def _canonical_relations(
    relations: Sequence[BookTimeRelationKind],
) -> tuple[BookTimeRelationKind, ...]:
    return tuple(sorted(set(relations), key=_BOOK_TIME_RELATION_ORDER.__getitem__))


def _book_time_day_phase(text: str) -> BookTimeDayPhase | None:
    if any(marker in text for marker in ("零点", "午夜")):
        return "around_midnight"
    if any(marker in text for marker in ("黎明前", "天将亮未亮", "天光渐白", "凌晨")):
        return "pre_dawn"
    if any(marker in text for marker in ("清晨", "早晨", "晨间", "上午")):
        return "morning"
    if any(marker in text for marker in ("午后", "下午")):
        return "afternoon"
    if any(
        marker in text
        for marker in ("夜间", "深夜", "午夜", "傍晚", "当夜", "同夜", "入夜", "夜里")
    ):
        return "night"
    if any(marker in text for marker in ("白天", "日间", "中午")):
        return "daytime"
    return None


def _book_time_unanchored_reason(text: str) -> BookTimeUnanchoredReason | None:
    if any(marker in text for marker in ("梦中", "梦里", "梦境", "幻梦")):
        return "dream"
    if any(marker in text for marker in ("回忆", "倒叙", "闪回")):
        return "flashback"
    if "插叙" in text:
        return "inserted_memory"
    if any(marker in text for marker in ("幻境", "幻觉")):
        return "non_mainline"
    if re.search(
        r"(?:分钟|分|小时|时|天|日|周|星期|个月|月|年|片刻|数日|数周|数月)\s*前",
        text,
    ):
        return "non_mainline"
    return None


def _source_approximate_quantity(
    text: str,
) -> BookTimeApproximateQuantity | None:
    match = _APPROXIMATE_QUANTITY_RE.search(text)
    if match is None:
        return None
    number = _parse_cn_number(match.group("number"))
    if number is None or number <= 0:
        return None
    return BookTimeApproximateQuantity(
        value=number,
        unit=_BOOK_TIME_UNIT[match.group("unit")],
    )


def _is_explicit_duration_match(text: str, match: re.Match[str]) -> bool:
    """拒绝把“下午三时”等钟面点误投影成距上一节点三小时。"""

    stripped = text.strip(" ，。；：、\t\r\n")
    matched = match.group(0).strip()
    if stripped == matched:
        return True
    before = text[: match.start()]
    after = text[match.end() :]
    return bool(
        re.search(r"(?:经过|过了|相隔|间隔|持续|历时)\s*$", before)
        or re.match(r"\s*(?:后|之后|以后|过后)", after)
    )


def _relations_from_natural_time(
    text: str,
    *,
    phase: BookTimeDayPhase | None,
) -> tuple[BookTimeRelationKind, ...] | None:
    relations: list[BookTimeRelationKind] = []
    if any(marker in text for marker in ("次日", "翌日")):
        relations.append("next_narrative_day")
    elif any(marker in text for marker in ("同夜", "同一夜")):
        if any(marker in text for marker in ("后", "稍后", "之后", "过了")):
            relations.append("after")
        relations.append("same_night")
    elif "当夜" in text:
        relations.append("after")
        # “当夜，某事件后数小时”同时含一个章内局部锚；不能把局部锚的
        # “后数小时”反推成相对上一章的 SAME_NARRATIVE_DAY。单纯“当夜”
        # 或“当夜稍后”才冻结同日关系。
        if re.search(
            r"当夜[，,].*(?:后|之后)\s*"
            r"(?:数|\d|[零〇一二两三四五六七八九十])"
            r"(?:分钟|分|小时|时|天|日)",
            text,
        ) is None:
            relations.append("same_narrative_day")
    elif any(marker in text for marker in ("同一天", "同日", "当日", "当天")):
        if any(marker in text for marker in ("后", "稍后", "之后", "过了", "数")):
            relations.append("after")
        relations.append("same_narrative_day")
    elif any(marker in text for marker in ("同一时刻", "立即", "紧接", "数秒")):
        relations.append("same_instant")
    elif any(marker in text for marker in ("后", "稍后", "之后", "后来", "过了一阵", "片刻")):
        relations.append("after")
    elif phase is not None:
        relations.append("same_narrative_day")
    else:
        return None
    return _canonical_relations(relations)


def _fuzzy_gap_interval(text: str) -> BookClockInterval | None:
    """解析一段文字中的窄口径模糊时长；不处理“次日”日历锚。"""

    if "片刻" in text:
        return BookClockInterval(minimum_minutes=1, maximum_minutes=15)
    if "数分钟" in text:
        return BookClockInterval(minimum_minutes=2, maximum_minutes=30)
    if "数小时" in text:
        return BookClockInterval(minimum_minutes=60, maximum_minutes=12 * 60)
    if "半天" in text:
        return BookClockInterval(minimum_minutes=12 * 60, maximum_minutes=12 * 60)
    if "数日" in text:
        return BookClockInterval(
            minimum_minutes=2 * 24 * 60,
            maximum_minutes=7 * 24 * 60,
        )
    if "数周" in text:
        return BookClockInterval(
            minimum_minutes=2 * 7 * 24 * 60,
            maximum_minutes=6 * 7 * 24 * 60,
        )
    if "数月" in text:
        return BookClockInterval(
            minimum_minutes=2 * 30 * 24 * 60,
            maximum_minutes=6 * 30 * 24 * 60,
        )
    return None


def parse_book_time_gap(time_gap: str) -> BookClockInterval | None:
    """保守解析主时间线间隔；无法唯一换算时返回 ``None`` 而非猜测。"""

    normalized = " ".join(time_gap.strip().split())
    if not normalized:
        return None
    if any(marker in normalized for marker in ("梦", "回忆", "倒叙", "插叙", "幻境")):
        return None
    # 这些修饰词会把单个数字变成开放区间或近似值。即使正则能抓到
    # “三日”，也不能据此伪造一个精确 HARD 时钟。
    if any(
        marker in normalized
        for marker in (
            "大约",
            "约",
            "将近",
            "至少",
            "至多",
            "不超过",
            "不少于",
            "以上",
            "以内",
            "左右",
            "前后",
            "或更晚",
        )
    ):
        return None
    # “三个月前 / 数日前”是对过去事件的回指，不是本章相对上一章向前推进的
    # time_gap；宁可保持 SOFT，也不能把它错误累计成正向时长。
    if re.search(
        r"(?:分钟|分|小时|时|天|日|周|星期|个月|月|年|片刻|数日|数周|数月)\s*前",
        normalized,
    ):
        return None
    if normalized in {"开篇", "首章", "无(本章为首章)", "无（本章为首章）"}:
        return BookClockInterval(minimum_minutes=0, maximum_minutes=0)
    if any(marker in normalized for marker in ("立即", "紧接", "同一时刻", "数秒")):
        return BookClockInterval(minimum_minutes=0, maximum_minutes=5)

    # 章节卡的“次日”是主线计划锚，按一个叙事日累计；
    # 如果同句还有“数小时后”，再叠加该有界区间。这不从
    # 正文自由推理，只换算上游已明示的 time_gap。
    next_day = "次日" in normalized or "翌日" in normalized
    if next_day:
        base = BookClockInterval(
            minimum_minutes=24 * 60,
            maximum_minutes=24 * 60,
        )
        extra = _fuzzy_gap_interval(normalized)
        return base.add(extra) if extra is not None else base

    range_match = _RANGE_GAP_RE.search(normalized)
    if range_match is not None:
        # 范围句只允许一个时长表达，避免把“一至两日，
        # 又过三小时”猜成加法。
        if len(_RANGE_GAP_RE.findall(normalized)) != 1:
            return None
        minimum = _parse_cn_number(range_match.group("minimum"))
        maximum = _parse_cn_number(range_match.group("maximum"))
        if minimum is None or maximum is None or minimum > maximum:
            return None
        multiplier = _GAP_UNIT_MINUTES[range_match.group("unit")]
        return BookClockInterval(
            minimum_minutes=minimum * multiplier,
            maximum_minutes=maximum * multiplier,
        )

    fuzzy = _fuzzy_gap_interval(normalized)
    if fuzzy is not None:
        return fuzzy

    if any(marker in normalized for marker in ("当日入夜", "当天入夜", "同日入夜")):
        return BookClockInterval(
            minimum_minutes=6 * 60,
            maximum_minutes=18 * 60,
        )
    if any(marker in normalized for marker in ("当日", "当天", "同日", "同一夜")):
        return BookClockInterval(
            minimum_minutes=0,
            maximum_minutes=24 * 60,
        )

    matches = list(_EXACT_GAP_RE.finditer(normalized))
    if len(matches) != 1:
        return None
    match = matches[0]
    number = _parse_cn_number(match.group("number"))
    if number is None or number < 0:
        return None
    minutes = number * _GAP_UNIT_MINUTES[match.group("unit")]
    return BookClockInterval(minimum_minutes=minutes, maximum_minutes=minutes)


def build_book_clock_constraint(
    *,
    time_gap: str,
    previous_interval: BookClockInterval | None,
    is_first_chapter: bool,
) -> BookClockConstraint:
    """从卡片 ``time_gap`` 和上一章已批准时钟得到唯一冻结结果。"""

    gap = parse_book_time_gap(time_gap)
    if is_first_chapter:
        if gap is None or gap.minimum_minutes != 0 or gap.maximum_minutes != 0:
            return BookClockConstraint(
                time_gap=time_gap or "(空)",
                previous_interval=None,
                gap_interval=None,
                current_interval=None,
                is_hard=False,
                soft_reason="全书首章 time_gap 不是明确开篇字面",
            )
        return BookClockConstraint(
            time_gap=time_gap,
            previous_interval=None,
            gap_interval=gap,
            current_interval=gap,
            is_hard=True,
        )
    if previous_interval is None:
        return BookClockConstraint(
            time_gap=time_gap or "(空)",
            previous_interval=None,
            gap_interval=None,
            current_interval=None,
            is_hard=False,
            soft_reason="上一章没有已提交的书内时钟",
        )
    if gap is None:
        return BookClockConstraint(
            time_gap=time_gap or "(空)",
            previous_interval=previous_interval,
            gap_interval=None,
            current_interval=None,
            is_hard=False,
            soft_reason="time_gap 含混或属于非主时间线，未自动换算",
        )
    return BookClockConstraint(
        time_gap=time_gap,
        previous_interval=previous_interval,
        gap_interval=gap,
        current_interval=previous_interval.add(gap),
        is_hard=True,
    )


def _parse_typed_book_time_gap(
    time_gap: str,
) -> tuple[
    BookTimeKind,
    BookClockInterval | None,
    tuple[BookTimeRelationKind, ...],
    BookTimeDayPhase | None,
    BookTimeApproximateQuantity | None,
    BookTimeUnanchoredReason | None,
]:
    """把上游自然语言冻结为四类 typed facts；不从近似词发明 bounds。"""

    normalized = " ".join(time_gap.strip().split())
    if not normalized:
        return ("genuinely_unanchored", None, (), None, None, "unspecified")
    phase = _book_time_day_phase(normalized)
    # 末尾是“天光渐白之后数小时”一类表达时，phase 描述的是 elapsed
    # quantity 的起点，不是本章当前节点；不把 anchor phase 误冻为 current phase。
    if re.search(
        r"(?:黎明前|天将亮未亮|天光渐白|凌晨|清晨|早晨|晨间|上午|"
        r"中午|午后|下午|傍晚|夜间|深夜|午夜|当夜|同夜|入夜|夜里|"
        r"白天|日间)(?:之后|后)\s*"
        r"(?:数|\d|[零〇一二两三四五六七八九十])"
        r"(?:分钟|分|小时|时|天|日)\s*$",
        normalized,
    ):
        phase = None
    if any(marker in normalized for marker in ("回到当夜", "回到同夜", "重返当夜", "返回当夜")):
        return (
            "ordered_relative",
            None,
            _canonical_relations(("resume_anchor", "same_night")),
            "night",
            None,
            None,
        )
    unanchored_reason = _book_time_unanchored_reason(normalized)
    if unanchored_reason is not None:
        return (
            "genuinely_unanchored",
            None,
            (),
            None,
            None,
            unanchored_reason,
        )
    if normalized in {"开篇", "首章", "无(本章为首章)", "无（本章为首章）"}:
        return (
            "exact",
            BookClockInterval(minimum_minutes=0, maximum_minutes=0),
            (),
            None,
            None,
            None,
        )

    approximate = _source_approximate_quantity(normalized)
    approximate_or_open = any(
        marker in normalized
        for marker in (
            "大约",
            "约",
            "将近",
            "至少",
            "至多",
            "不超过",
            "不少于",
            "以上",
            "以内",
            "左右",
            "前后",
            "或更晚",
            "数分钟",
            "数小时",
            "数日",
            "数周",
            "数月",
            "几天",
            "一两天",
        )
    )

    if not approximate_or_open:
        range_matches = list(_RANGE_GAP_RE.finditer(normalized))
        if len(range_matches) == 1:
            match = range_matches[0]
            minimum = _parse_cn_number(match.group("minimum"))
            maximum = _parse_cn_number(match.group("maximum"))
            if (
                minimum is not None
                and maximum is not None
                and minimum < maximum
                and _is_explicit_duration_match(normalized, match)
            ):
                multiplier = _GAP_UNIT_MINUTES[match.group("unit")]
                return (
                    "bounded_interval",
                    BookClockInterval(
                        minimum_minutes=minimum * multiplier,
                        maximum_minutes=maximum * multiplier,
                    ),
                    ("after",),
                    phase,
                    None,
                    None,
                )
        if "半天" in normalized:
            return (
                "exact",
                BookClockInterval(minimum_minutes=12 * 60, maximum_minutes=12 * 60),
                ("after",),
                phase,
                None,
                None,
            )
        exact_matches = list(_EXACT_GAP_RE.finditer(normalized))
        if len(exact_matches) == 1:
            match = exact_matches[0]
            number = _parse_cn_number(match.group("number"))
            if (
                number is not None
                and number >= 0
                and _is_explicit_duration_match(normalized, match)
            ):
                minutes = number * _GAP_UNIT_MINUTES[match.group("unit")]
                return (
                    "exact",
                    BookClockInterval(
                        minimum_minutes=minutes,
                        maximum_minutes=minutes,
                    ),
                    ("after",),
                    phase,
                    None,
                    None,
                )

        natural_relations = _relations_from_natural_time(normalized, phase=phase)
        if natural_relations is not None and (
            _EXPLICIT_CLOCK_RANGE_RE.search(normalized) is not None
            or (
                len(range_matches) == 1
                and phase is not None
                and not _is_explicit_duration_match(normalized, range_matches[0])
            )
        ):
            return (
                "bounded_interval",
                None,
                natural_relations,
                phase,
                None,
                None,
            )
        if (
            natural_relations is not None
            and _EXPLICIT_CLOCK_POINT_RE.search(normalized) is not None
        ):
            return (
                "exact",
                None,
                natural_relations,
                phase,
                None,
                None,
            )

    natural_relations = _relations_from_natural_time(normalized, phase=phase)
    if natural_relations is None:
        return (
            "genuinely_unanchored",
            None,
            (),
            None,
            None,
            "unspecified",
        )
    return (
        "ordered_relative",
        None,
        natural_relations,
        phase,
        approximate,
        None,
    )


def _book_time_node_id(
    *,
    chapter_id: str,
    time_gap: str,
    previous_projection_hash: str,
    kind: BookTimeKind,
    gap_interval: BookClockInterval | None,
    anchor_node_id: str | None,
    relations: tuple[BookTimeRelationKind, ...],
    day_phase: BookTimeDayPhase | None,
    approximate_quantity: BookTimeApproximateQuantity | None,
    unanchored_reason: BookTimeUnanchoredReason | None,
) -> str:
    payload = {
        "contract_version": 1,
        "chapter_id": chapter_id,
        "time_gap": " ".join(time_gap.strip().split()),
        "previous_projection_hash": previous_projection_hash,
        "kind": kind,
        "gap_interval": (
            gap_interval.model_dump(mode="json") if gap_interval is not None else None
        ),
        "anchor_node_id": anchor_node_id,
        "relations": relations,
        "day_phase": day_phase,
        "approximate_quantity": (
            approximate_quantity.model_dump(mode="json")
            if approximate_quantity is not None
            else None
        ),
        "unanchored_reason": unanchored_reason,
    }
    canonical = json.dumps(
        payload, ensure_ascii=False, sort_keys=True, separators=(",", ":")
    )
    return hashlib.sha256(canonical.encode("utf-8")).hexdigest()


def _advance_book_time_node(
    *,
    previous_projection: BookTimeProjection | None,
    node: BookTimeNode,
) -> BookTimeProjection:
    if previous_projection is None:
        latest_anchor = (
            BookTimeAbsoluteAnchor(node_id=node.node_id, interval=node.absolute_interval)
            if node.absolute_interval is not None
            else None
        )
        chain: tuple[BookTimeNode, ...] = ()
        latest_mainline = node.node_id if node.kind != "genuinely_unanchored" else None
    elif node.kind == "genuinely_unanchored":
        latest_anchor = previous_projection.latest_absolute_anchor
        chain = previous_projection.active_relation_chain
        latest_mainline = previous_projection.latest_mainline_node_id
    elif node.absolute_interval is not None:
        latest_anchor = BookTimeAbsoluteAnchor(
            node_id=node.node_id,
            interval=node.absolute_interval,
        )
        chain = ()
        latest_mainline = node.node_id
    else:
        latest_anchor = previous_projection.latest_absolute_anchor
        chain = (*previous_projection.active_relation_chain, node)
        latest_mainline = node.node_id
    return BookTimeProjection(
        contract_version=1,
        current_node=node,
        latest_mainline_node_id=latest_mainline,
        latest_absolute_anchor=latest_anchor,
        active_relation_chain=chain,
        relation_chain_hash=compute_book_time_relation_chain_hash(
            latest_absolute_anchor=latest_anchor,
            active_relation_chain=chain,
        ),
    )


def build_typed_book_clock_constraint(
    *,
    chapter_id: str,
    time_gap: str,
    previous_projection: BookTimeProjection | None,
    previous_projection_hash: str,
    is_first_chapter: bool,
) -> BookClockConstraint:
    """冻结 C2 v1 节点；近似时间永不被换成 deterministic min/max。"""

    raw_time_gap = time_gap.strip() or "(空)"
    (
        kind,
        gap_interval,
        relations,
        day_phase,
        approximate_quantity,
        unanchored_reason,
    ) = _parse_typed_book_time_gap(time_gap)
    anchor_node_id = (
        previous_projection.latest_mainline_node_id
        if previous_projection is not None
        else None
    )
    if is_first_chapter:
        opening_interval = BookClockInterval(minimum_minutes=0, maximum_minutes=0)
        if kind == "exact" and (
            gap_interval == opening_interval or gap_interval is None
        ):
            relations = ()
            anchor_node_id = None
        else:
            kind = "genuinely_unanchored"
            gap_interval = None
            relations = ()
            day_phase = None
            approximate_quantity = None
            unanchored_reason = "unspecified"
            anchor_node_id = None
    elif kind != "genuinely_unanchored" and not is_first_chapter:
        if anchor_node_id is None:
            kind = "genuinely_unanchored"
            gap_interval = None
            relations = ()
            day_phase = None
            approximate_quantity = None
            unanchored_reason = "legacy_unknown"
        elif kind in {"exact", "bounded_interval"} and gap_interval is not None:
            relations = ("after",)
    if kind == "genuinely_unanchored":
        anchor_node_id = None

    previous_interval = (
        previous_projection.current_node.absolute_interval
        if previous_projection is not None
        and previous_projection.current_node is not None
        else None
    )
    current_interval: BookClockInterval | None = None
    can_derive_absolute = bool(
        gap_interval is not None
        and previous_projection is not None
        and previous_interval is not None
        and previous_projection.latest_absolute_anchor is not None
        and previous_projection.current_node is not None
        and previous_projection.current_node.node_id
        == previous_projection.latest_mainline_node_id
        and not previous_projection.active_relation_chain
    )
    if is_first_chapter and kind == "exact":
        current_interval = gap_interval or BookClockInterval(
            minimum_minutes=0,
            maximum_minutes=0,
        )
    elif can_derive_absolute:
        assert previous_interval is not None and gap_interval is not None
        current_interval = previous_interval.add(gap_interval)

    node_id = _book_time_node_id(
        chapter_id=chapter_id,
        time_gap=raw_time_gap,
        previous_projection_hash=previous_projection_hash,
        kind=kind,
        gap_interval=gap_interval,
        anchor_node_id=anchor_node_id,
        relations=relations,
        day_phase=day_phase,
        approximate_quantity=approximate_quantity,
        unanchored_reason=unanchored_reason,
    )
    node = BookTimeNode(
        node_id=node_id,
        chapter_id=chapter_id,
        kind=kind,
        raw_time_gap=raw_time_gap,
        anchor_node_id=anchor_node_id,
        relations=relations,
        day_phase=day_phase,
        approximate_quantity=approximate_quantity,
        gap_interval=gap_interval,
        absolute_interval=current_interval,
        unanchored_reason=unanchored_reason,
    )
    projected = _advance_book_time_node(
        previous_projection=previous_projection,
        node=node,
    )
    return BookClockConstraint(
        contract_version=1,
        time_gap=raw_time_gap,
        previous_interval=previous_interval,
        gap_interval=gap_interval,
        current_interval=current_interval,
        kind=kind,
        node_id=node_id,
        node_chapter_id=chapter_id,
        anchor_node_id=anchor_node_id,
        relations=relations,
        day_phase=day_phase,
        approximate_quantity=approximate_quantity,
        unanchored_reason=unanchored_reason,
        relation_chain_hash=projected.relation_chain_hash,
        is_hard=kind != "genuinely_unanchored",
        soft_reason=(
            "time_gap 属于 genuinely_unanchored；保留最近主线锚与关系链"
            if kind == "genuinely_unanchored"
            else ""
        ),
    )


def advance_book_time_projection(
    *,
    previous_projection: BookTimeProjection | None,
    constraint: BookClockConstraint,
) -> BookTimeProjection:
    """提交已冻结 v1 约束；任何 node/chain drift 都在持久化前失败。"""

    if constraint.contract_version != 1 or constraint.kind is None:
        raise CoreStateContractError("advance_book_time_projection 只接受 clock v1")
    node = BookTimeNode(
        node_id=constraint.node_id,
        chapter_id=constraint.node_chapter_id,
        kind=constraint.kind,
        raw_time_gap=constraint.time_gap,
        anchor_node_id=constraint.anchor_node_id,
        relations=constraint.relations,
        day_phase=constraint.day_phase,
        approximate_quantity=constraint.approximate_quantity,
        gap_interval=constraint.gap_interval,
        absolute_interval=constraint.current_interval,
        unanchored_reason=constraint.unanchored_reason,
    )
    projected = _advance_book_time_node(
        previous_projection=previous_projection,
        node=node,
    )
    if projected.relation_chain_hash != constraint.relation_chain_hash:
        raise CoreStateContractError("book time relation chain 已漂移")
    return projected


def compute_core_state_projection_hash(
    *,
    facts: tuple[CoreStateFact, ...],
    book_clock: BookClockInterval | None,
    book_time: BookTimeProjection | None = None,
    book_time_contract_version: Literal[0, 1] = 0,
) -> str:
    """计算当前事实 hash；v0 payload 字节保持原样，v1 才加入 typed time。"""

    payload: dict[str, Any] = {
        "facts": [
            item.model_dump(mode="json")
            for item in sorted(facts, key=lambda fact: fact.state_key)
        ],
        "book_clock": (
            book_clock.model_dump(mode="json") if book_clock is not None else None
        ),
    }
    if book_time_contract_version == 1:
        if book_time is None or book_time.contract_version != 1:
            raise CoreStateContractError("book time v1 projection 缺少 typed 内容")
        payload["book_time_contract_version"] = 1
        payload["book_time"] = book_time.model_dump(mode="json")
    canonical = json.dumps(
        payload, ensure_ascii=False, sort_keys=True, separators=(",", ":")
    )
    return hashlib.sha256(canonical.encode("utf-8")).hexdigest()


def make_core_state_projection(
    *,
    facts: list[CoreStateFact] | tuple[CoreStateFact, ...] = (),
    book_clock: BookClockInterval | None = None,
    book_time: BookTimeProjection | None = None,
    book_time_contract_version: Literal[0, 1] = 0,
) -> CoreStateProjection:
    frozen = tuple(sorted(facts, key=lambda item: item.state_key))
    return CoreStateProjection(
        facts=frozen,
        book_clock=book_clock,
        book_time=book_time,
        book_time_contract_version=book_time_contract_version,
        projection_hash=compute_core_state_projection_hash(
            facts=frozen,
            book_clock=book_clock,
            book_time=book_time,
            book_time_contract_version=book_time_contract_version,
        ),
    )


def _format_minutes(interval: BookClockInterval) -> str:
    def one(value: int) -> str:
        if value % (24 * 60) == 0:
            return f"{value // (24 * 60)}天"
        if value % 60 == 0:
            return f"{value // 60}小时"
        return f"{value}分钟"

    if interval.minimum_minutes == interval.maximum_minutes:
        return one(interval.minimum_minutes)
    return f"{one(interval.minimum_minutes)}—{one(interval.maximum_minutes)}"


_BOOK_TIME_RELATION_LABELS: dict[BookTimeRelationKind, str] = {
    "same_instant": "同一时刻",
    "after": "晚于上一主线节点",
    "same_narrative_day": "同一叙事日",
    "same_night": "同一叙事夜",
    "next_narrative_day": "下一叙事日",
    "resume_anchor": "恢复到最近明确主线节点",
}
_BOOK_TIME_PHASE_LABELS: dict[BookTimeDayPhase, str] = {
    "morning": "清晨/早晨",
    "daytime": "白天",
    "afternoon": "午后",
    "night": "夜间",
    "around_midnight": "零点前后",
    "pre_dawn": "黎明前",
}


def render_typed_book_time_constraint(clock: BookClockConstraint) -> str:
    """给 Writer/Judge/UI 文案消费者的自然语言关系，不暴露机器分钟投影。"""

    if clock.contract_version != 1:
        raise CoreStateContractError("typed book-time 展示只接受 clock contract v1")
    parts = [f"原始关系={clock.time_gap}"]
    if clock.relations:
        parts.append(
            "关系=" + "、".join(_BOOK_TIME_RELATION_LABELS[item] for item in clock.relations)
        )
    if clock.day_phase is not None:
        parts.append("时段=" + _BOOK_TIME_PHASE_LABELS[clock.day_phase])
    if clock.approximate_quantity is not None:
        parts.append("近似量只作语义提示，不生成分钟上下界")
    if clock.kind == "genuinely_unanchored":
        parts.append("本章不建立主线锚；最近主线锚与既有关系链保持")
    return "；".join(parts)


CORE_STATE_RELATION_EVIDENCE_GUIDANCE = (
    "物件关系证据按同一具体对象判断：possession 是物理持有，control 是决定授权，"
    "access 是当前可操作权限；三者不能互推。持有房门钥匙不自动证明房内物件的"
    "独占控制或操作权，容器与内容物也不是同一对象。首次登记及关系再次确认"
    "不要求虚构换手，但同样需要正文明确支持当前关系；仅卡片计划不是正文证据。"
    "长期照料、熟悉或先发现不等于物理持有或授权控制；未来才会带走的对象不能提前登记持有。"
    "全书 relation_attributes 是可追踪属性目录，本章只登记有当前依据的那一义。"
    "暂不明确的关系保留未知；不得让人物为了登记关系说归属宣言。"
)


def render_core_state_capsule(
    *,
    projection: CoreStateProjection,
    transitions: Sequence[CoreStateTransition] = (),
    book_clock_constraint: BookClockConstraint | None = None,
) -> CoreStatePromptCapsule:
    """生成 Writer/Judge 共用短胶囊；HARD 超预算时显式失败。"""

    groups: list[str] = []
    by_entity: dict[tuple[str, str], list[CoreStateFact]] = {}
    for fact in projection.facts:
        by_entity.setdefault((fact.entity_type, fact.entity_id), []).append(fact)
    for (entity_type, entity_id), facts in sorted(by_entity.items()):
        body = "；".join(f"{fact.attribute}={fact.value}" for fact in facts)
        groups.append(f"- {entity_type}:{entity_id}｜当前有效：{body}")
    if (
        book_clock_constraint is not None
        and book_clock_constraint.contract_version == 1
    ):
        groups.append(
            "- book_time｜" + render_typed_book_time_constraint(book_clock_constraint)
        )
    elif book_clock_constraint is not None and book_clock_constraint.is_hard:
        assert book_clock_constraint.current_interval is not None
        groups.append(
            "- book_clock｜本章主时间线距开篇："
            + _format_minutes(book_clock_constraint.current_interval)
            + f"（time_gap={book_clock_constraint.time_gap}）"
        )
    elif (
        projection.book_time_contract_version == 1
        and projection.book_time is not None
        and projection.book_time.current_node is not None
    ):
        node = projection.book_time.current_node
        groups.append(f"- book_time｜最近冻结关系={node.raw_time_gap}")
    elif projection.book_clock is not None:
        groups.append(
            "- book_clock｜上一章末主时间线距开篇："
            + _format_minutes(projection.book_clock)
        )
    for transition in transitions:
        groups.append(
            f"- 本章获准变化：{transition.state_key}｜"
            f"{transition.from_value or '(未建立)'} → {transition.to_value}｜"
            f"理由：{transition.reason}"
        )

    header = (
        "## 全书核心状态与书内时钟（HARD，共用判尺）\n\n"
        "以下只约束客观连续性；不要求正文逐条复述。未列入“本章获准变化”的当前值"
        "必须保持；获准变化也必须在正文中有明确因果与可定位证据。对白、梦境、回忆、"
        "插叙不自动改写主时间线。书内时间只按自然语言关系写作，不要求累计分钟或时间戳。\n\n"
    )
    if transitions or any(fact.attribute in CORE_STATE_RELATION_ATTRIBUTES for fact in projection.facts):
        header += CORE_STATE_RELATION_EVIDENCE_GUIDANCE + "\n\n"
    text = header + ("\n".join(groups) if groups else "- （当前无已建立核心事实）")
    group_count = len(groups)
    char_count = len(text)
    if (
        group_count > CORE_STATE_HARD_MAX_GROUPS
        or char_count > CORE_STATE_HARD_MAX_CHARS
    ):
        raise CoreStateDensityError(
            "核心状态 HARD 投影超过安全注意力预算，禁止静默截断："
            f"groups={group_count}/{CORE_STATE_HARD_MAX_GROUPS}, "
            f"chars={char_count}/{CORE_STATE_HARD_MAX_CHARS}"
        )
    return CoreStatePromptCapsule(
        text=text,
        group_count=group_count,
        char_count=char_count,
        above_target=(
            group_count > CORE_STATE_TARGET_GROUPS
            or char_count > CORE_STATE_TARGET_CHARS
        ),
    )


__all__ = [
    "BookClockConstraint",
    "BookClockInterval",
    "BookTimeAbsoluteAnchor",
    "BookTimeApproximateQuantity",
    "BookTimeDayPhase",
    "BookTimeKind",
    "BookTimeNode",
    "BookTimeProjection",
    "BookTimeRelationKind",
    "BookTimeUnanchoredReason",
    "CHARACTER_CORE_ATTRIBUTES",
    "CORE_STATE_HARD_MAX_CHARS",
    "CORE_STATE_HARD_MAX_GROUPS",
    "CORE_STATE_TARGET_CHARS",
    "CORE_STATE_TARGET_GROUPS",
    "CoreStateCandidate",
    "CoreStateContractError",
    "CoreStateDensityError",
    "CoreStateFact",
    "CoreStateProjection",
    "CoreStatePromptCapsule",
    "CoreStateRelationAttribute",
    "CoreStateTransition",
    "CoreStateWriteAttribute",
    "CoreStateWriteTransition",
    "CORE_STATE_RELATION_ATTRIBUTES",
    "OBJECT_CORE_ATTRIBUTES",
    "advance_book_time_projection",
    "build_book_clock_constraint",
    "build_typed_book_clock_constraint",
    "canonical_core_state_value",
    "compute_book_time_relation_chain_hash",
    "compute_core_state_projection_hash",
    "make_core_state_projection",
    "make_state_key",
    "parse_book_time_gap",
    "render_core_state_capsule",
    "render_typed_book_time_constraint",
    "validate_core_state_transition_sources",
]
