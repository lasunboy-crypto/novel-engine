"""MemoryExtractor agent(P2-B-09)。

职责:接收一章正文 + 章节卡上下文 → 拼成 system + user 两条消息 → 调注入
的 ``LLMClient`` → 解析 LLM 输出的 JSON → **同事务内**写回 SQLite(角色 /
伏笔 / 时间线 三类硬记忆,经由 ``memory/store.py`` CRUD)+ Chroma(四类
含 ``fact`` 一并)+ 返回结构化 ``ExtractionResult``。

为什么这次做
-------------
解锁 P2 向量记忆链路的"自动维护"段——P2-B-08 retriever 的输入
(``VectorStore`` 里的记忆)由本 agent 在每章写完后自动抽取并写回;
Writer(P2-B-10)拿到的"上一章相关记忆"就来自本路径的产出。

设计要点
========

1. **入参与 retriever 对偶**:``extract(content, card, *, llm,
   db_session, vector_store)``——``Session`` / ``VectorStore`` 由 caller
   注入,事务边界归 caller。本 agent 内部只 ``flush`` 不 ``commit``(沿
   用 ``memory/store.py`` 纪律);Chroma ``add`` 失败回滚由 caller(本任
   务范围内不做事务封装)。
2. **LLM 输出契约**:抽取 JSON 包含四类——``characters`` / ``plot_threads``
   / ``timeline`` / ``facts``。前三类**入 SQLite + Chroma 双写**(SQLite
   维护实体 ground truth,Chroma 维护向量检索);``facts`` 是 P2 阶段语义
   记忆桥,**只入 Chroma**(P3+ 视需要再加 ``facts`` 表)。
3. **SQLite 三类:get-or-update-or-create 语义**:LLM 给的 ``character_id``
   / ``thread_id`` / ``entry_id`` 在 SQLite 已存在时**全量更新**,不存在
   时**新建**——LLM 抽取本就是"角色已存在则更新属性,不存在则新建"的
   语义,避开 IntegrityError 路径。
4. **Chroma `id` 约定**:``f"{entity_type}-{entity_id}"`` 形态(例
   ``"character-lin-yan"``);Chroma ``add`` 同 ID 行为是 upsert,角色
   属性更新时向量自然覆盖,retriever top_K 不会塞满"林衍 v1 / v2 / v3"。
5. **Chroma `metadatas` 五字段约定**(P2-B-09 拍板):
   ``entity_type`` / ``entity_id`` / ``chapter_id`` / ``workspace_id`` /
   ``created_at``(ISO 8601 UTC)。下游 retriever 调用方可按 ``where``
   子句过滤。
6. **写入顺序:先 SQLite flush 再 Chroma add**——若 SQLite ``IntegrityError``
   等错误抛出,Chroma 还没动,caller rollback 干净;Chroma ``add`` 失败
   时 SQLite 还在 caller 事务里没 commit,caller rollback 即可。
7. **解析失败抛 ``MemoryExtractorError``**(带 ``raw_output`` /
   ``model_used``)+ structlog ``memory_extractor_parse_failed`` 事件;
   此时 SQLite / Chroma 一字未写,caller 看到错误回滚事务即可,无残留。
8. **agent 身份 marker(P1-B-22 协议)**:user message 末尾追加
   ``<!-- agent_role: extractor -->``。MockLLMClient 收到 extractor
   marker 当前抛 ``NotImplementedError``(P2-B-12+ 真接入时再扩);P2-B-09
   单测用独立 fake LLM client 直接返预设 JSON。
9. **不做什么**:① 跨章一致性校验(归 P2-B-11 Reviewer score_voice_consistency
   / P2-B-12 衔接桥);② 抽取产物的语义重写 / 去重(P3+);③ 抽取失败重试
   (归 caller / Orchestrator);④ 把 ``fact`` 落 SQLite(P2 不建 facts
   表,P3+ 视需要再加);⑤ 写 Chroma 失败时回滚已写的 SQLite(归 caller
   事务边界)。

边界
====

- ``content`` 为空字符串 / 空白:LLM 仍调用,LLM 应返四类空数组;若 LLM
  报错或返非合规 JSON 抛 ``MemoryExtractorError``。
- LLM 给的 ID 含非法字符(空格 / 中文标点 / 等)被 chroma collection
  正则约束拦下时,``vector_store.add`` 抛 ``ValueError`` 自然冒泡。
- ``timeline.time_order`` 不做单调校验(沿用 ``memory/store.py`` 决策)。
"""

from __future__ import annotations

import difflib
import hashlib
import json
import re
from dataclasses import dataclass
from datetime import UTC, datetime
from collections.abc import Callable, Sequence
from pathlib import Path
from typing import Any, Literal, get_args

import structlog
from pydantic import (
    BaseModel,
    ConfigDict,
    Field,
    ValidationError,
    model_validator,
)
from sqlalchemy.orm import Session

from src.config import settings
from src.core.agents._json_contract import loads_with_quote_repair
from src.core.agents._prompt import (
    append_role_marker,
    load_prompt_template,
    resolve_routing,
)
from src.core.agents._structured_output import prepare_strict_json_output
from src.core.agents.creative_delta import (
    build_assembly_artifact,
    build_evidence_source_catalog,
    CreativeDeltaAssemblyError,
    EvidenceSource,
    FrozenAuthority,
    render_evidence_source_catalog,
    resolve_evidence_source,
    uses_hy3_creative_delta,
)
from src.core.db.models import PlotThreadORM
from src.core.llm.base import LLMClient, Message
from src.core.memory.store import (
    create_character,
    create_plot_thread,
    create_timeline_entry,
    get_character,
    get_plot_thread,
    get_timeline_entry,
    list_characters_by_workspace,
    list_plot_threads_by_workspace,
    list_timeline_entries_by_workspace,
    update_character,
    update_plot_thread,
    update_timeline_entry,
)
from src.core.memory.vector import VectorStore
from src.core.models.character import (
    Character,
    CharacterStateChange,
    CharacterStateSnapshot,
    VoiceProfile,
    format_state_snapshot_lines,
    latest_state_snapshot_before,
)
from src.core.models.chapter_card import ChapterCard, chapter_seq_from_id
from src.core.models.plot_thread import PlotThread
from src.core.models.setting_contract import SettingContract
from src.core.models.timeline_entry import TimelineEntry
from src.core.routing.router import ModelRouter

_log = structlog.get_logger(__name__)

_EXTRACTOR_PROMPT_FILENAME = "memory_extractor.md"
_DELTA_EXTRACTOR_PROMPT_FILENAME = "memory_extractor_delta.md"

# Extractor 默认生成参数:抽取求稳,温度低;输出装一份多类 JSON,需中等长度。
_DEFAULT_TEMPERATURE = 0.2
_DEFAULT_MAX_TOKENS = 4000

# R9 临时伏笔不是“正文里任何未解释细节”。只有明确形成跨章读者承诺、且正文
# 有唯一逐字证据的高重要度候选才可进入 PlotThread。门槛只作用于 DB 中不存在的
# 新 ID；既有大纲/人工伏笔按原链路更新，绝不重新参加这套准入。
_EMERGENT_PLOT_THREAD_KINDS = frozenset(
    {
        "withheld_answer",
        "explicit_promise",
        "unresolved_consequence",
        "persistent_anomaly",
    }
)
_EMERGENT_EXPIRY_POLICIES = frozenset({"arc_end", "volume_end", "never"})
_EMERGENT_EVIDENCE_MIN_CHARS = 12
_EMERGENT_EVIDENCE_MAX_CHARS = 180
_EMERGENT_MAX_NEW_PER_CHAPTER = 1

# Chroma 写入 collection 名(对齐 P2-B-08 retriever ``DEFAULT_COLLECTION``)。
DEFAULT_COLLECTION = "memories"

# P7-B-05:名称相似度阈值。``difflib.SequenceMatcher`` ratio ∈ [0.0, 1.0],
# >= 阈值视为同人(LLM 偶发把 "林衍" 抽成 "林衍-2" / "林衍二" 等小幅变体的去重锚)。
# 常量留 module 级供 v1.1 真 LLM 实战时按反馈调校;0.85 是工程经验起点,中文双字
# 名小写改一字 ratio ≈ 0.5,改后缀 ratio ≈ 0.8-0.95,可拦截 LLM 改后缀类漂移
# 同时不误伤"林衍 / 林岩"等本就不同的人。
_NAME_SIMILARITY_THRESHOLD = 0.85

# 复用 P1-B-11 reviewer 的"代码块包裹"剥离正则——LLM 偶发把 JSON 套进
# ```json ... ``` 即使 prompt 禁止。
_CODE_FENCE_RE = re.compile(
    r"^\s*```(?:json)?\s*\n(?P<body>.*?)\n```\s*$",
    re.DOTALL,
)
# 末尾 HTML 注释剥离(防 LLM 复读 marker 到输出)。
_TRAILING_HTML_COMMENT_RE = re.compile(
    r"\s*<!--.*?-->\s*$",
    re.DOTALL,
)


class MemoryExtractorError(ValueError):
    """LLM 输出无法解析为合法抽取 JSON 时抛出。

    保留原始输出 + 实际模型名,便于 Orchestrator 重试决策与 P7 调优时回查
    根因(对齐 ``ReviewerParseError`` 形态)。
    """

    def __init__(self, message: str, *, raw_output: str, model_used: str) -> None:
        super().__init__(message)
        self.raw_output = raw_output
        self.model_used = model_used


class FactEntry(BaseModel):
    """语义记忆条目(P2 阶段只写 Chroma,不入 SQLite)。

    抽取自正文中"不属于角色 / 伏笔 / 时间线三类但值得检索"的关键事实——
    物品出现、规则设定、关键对话、地点细节等。retriever 调用方可按
    ``where={"entity_type": "fact"}`` 过滤拿到纯语义记忆。
    """

    fact_id: str = Field(..., description="稳定 ID,'事实简述-章节 ID' 形态")
    description: str = Field(..., description="事实描述(≤ 100 字)")
    involved_characters: list[str] = Field(
        default_factory=list, description="涉及角色 ID 列表"
    )


class ExtractedCharacter(BaseModel):
    """LLM 抽取的角色:不含 ``workspace_id`` / ``first_appearance_chapter`` /
    ``last_updated_chapter``(系统注入)。

    ``voice_profile`` 直接用 ``Character`` 声明的 ``VoiceProfile`` 子模型——
    LLM 按 prompt 契约必填 ``tone`` / ``sentence_style`` / ``self_address``
    + 3 可空集合;校验在 ``_parse_llm_payload`` 阶段前置完成,缺字段直接
    抛 ``MemoryExtractorError``,不污染 SQLite / Chroma。
    """

    character_id: str
    name: str
    archetype: str
    appearance: str
    personality: str
    background: str
    voice_profile: VoiceProfile
    goals_long_term: list[str] = Field(default_factory=list)
    goals_short_term: list[str] = Field(default_factory=list)
    aliases: list[str] = Field(default_factory=list)
    current_state: dict = Field(default_factory=dict)
    relationships: dict[str, str] = Field(default_factory=dict)
    abilities: list[str] = Field(default_factory=list)
    secrets: list[str] = Field(default_factory=list)


class ExtractedPlotThread(BaseModel):
    """LLM 抽取的伏笔:不含 ``workspace_id`` / ``created_at`` /
    ``last_updated_chapter``(系统注入)。"""

    thread_id: str
    title: str
    status: str  # 由 PlotThread Pydantic 校验枚举
    importance: int
    planted_chapter: str
    description: str
    planned_payoff: str | None = None
    actual_payoff: str | None = None
    related_characters: list[str] = Field(default_factory=list)
    evidence_quote: str | None = Field(
        default=None,
        description="仅正文中新建临时伏笔时必填的逐字证据；既有伏笔更新可空",
    )
    emergent_kind: str | None = Field(
        default=None,
        description=(
            "仅新临时伏笔使用:withheld_answer / explicit_promise / "
            "unresolved_consequence / persistent_anomaly"
        ),
    )
    expiry_policy: str | None = Field(
        default=None,
        description=(
            "仅新临时伏笔使用:arc_end / volume_end / never；"
            "既有大纲/人工伏笔更新可为 null"
        ),
    )


class ExtractedTimelineEntry(BaseModel):
    """LLM 抽取的时间线条目:不含 ``workspace_id`` /``chapter_id``(系统注入)。

    v1.1 wave 1 session 1(E2)引入 ``is_flashback`` 字段:LLM prompt
    (``prompts/memory_extractor.md``)引导 LLM 在抽取 flashback / 回忆 / 双时空
    穿越的另一时空 entry 时标 True;旧 prompt fixture 不带该字段时 Pydantic
    默认 False(向后兼容)。
    """

    entry_id: str
    description: str
    time_label: str
    time_order: int
    involved_characters: list[str] = Field(default_factory=list)
    is_flashback: bool = False


class ExtractedCharacterState(BaseModel):
    """LLM 抽取的角色截止本章状态快照(G1,v1.1 wave 4 session 1)。

    不含 ``as_of_chapter``(由 ``extract()`` 从 ``card.chapter_id`` 解析全局
    序注入)。LLM 在拿到"## 角色当前状态(截止上一章)"上下文 + 本章正文后,
    输出**截止本章的累积状态**(``injuries`` / ``acquired_items`` /
    ``current_relationships`` / ``physical_state``)+ 本章**增量事件**
    (``state_changes``)。折叠由 LLM 完成(它有上一章状态 + 本章正文),持久化
    端确定性可测。
    """

    character_id: str
    physical_state: str = ""
    injuries: list[str] = Field(default_factory=list)
    acquired_items: list[str] = Field(default_factory=list)
    current_relationships: dict[str, str] = Field(default_factory=dict)
    state_changes: list[CharacterStateChange] = Field(default_factory=list)
    #: 进阶等级(模块②,v1.1 wave 5 s10)。**仅 gated 时 LLM 才输出**(锚定增量:本章
    #: 明写突破/晋级/倒退才给新境界字符串,否则 ``None`` → 持久化端结转上一章)。OFF
    #: 时 prompt 不含该指令 → LLM 不输出 → 默认 ``None`` → 零回归。
    progression_rank: str | None = None


class ExtractedSceneAnchor(BaseModel):
    """LLM 抽取的本章双时空场景锚点(B2,v1.1 wave 6 session 3)。

    **章级**结构化字段(每章至多一条),治 B1 30 章长跑 bug B1/B2/B3(病房入梦→
    工厂醒 / 工厂密室→催眠舱瞬移 / 推演室→大桥):双时空(意识穿梭 / 梦境 / 副本)
    类设定里,角色"意识离开身体进入另一时空"时身体所在的现实位置(``enter_dream_location``,
    入梦点)、与"意识返回 / 醒来"时身体所在位置(``wake_location``,醒来点)应一致,
    除非正文交代了身体被移动(``has_transition_explanation``)。

    **多数章为空**(非双时空入梦 / 醒来章):三字段全空 / False → B2 比对自动 skip。
    旧 prompt fixture 不带该字段时 payload 默认 ``None``(向后兼容)。
    """

    #: 入梦点 = 意识离开身体进入另一时空时,身体所在的现实位置。空 = 本章无入梦。
    enter_dream_location: str = ""
    #: 醒来点 = 意识返回 / 醒来时,身体所在的位置。空 = 本章无醒来。
    wake_location: str = ""
    #: 正文是否交代了身体在两界之间被移动(转移 / 搬运 / 自行移动等)。
    has_transition_explanation: bool = False

    @property
    def is_empty(self) -> bool:
        return not (self.enter_dream_location.strip() or self.wake_location.strip())


class _ExtractorLLMPayload(BaseModel):
    """LLM 输出的中间模型(校验四类必须出现 + 各类元素结构)。

    四类入参字段对齐 ``prompts/memory_extractor.md`` 的 JSON 契约;
    ``characters`` / ``plot_threads`` / ``timeline`` 复用 ``Extracted*``
    "窄"中间模型(不含系统字段)——extract() 在写库前补全
    ``workspace_id`` / ``chapter_id`` / ``created_at`` 等。

    ``character_states``(G1,v1.1 wave 4 session 1)默认空列表——旧 prompt
    fixture 不带该字段时向后兼容(老测试 / 老缓存响应不回归)。

    ``scene_anchor``(B2,v1.1 wave 6 session 3)默认 ``None``——双时空入梦 / 醒来
    章才非空;旧 prompt fixture 不带该字段时向后兼容。
    """

    characters: list[ExtractedCharacter]
    plot_threads: list[ExtractedPlotThread]
    timeline: list[ExtractedTimelineEntry]
    facts: list[FactEntry]
    character_states: list[ExtractedCharacterState] = Field(default_factory=list)
    scene_anchor: ExtractedSceneAnchor | None = None


class _RelationshipEntryDelta(BaseModel):
    model_config = ConfigDict(extra="forbid")

    target: str = Field(..., min_length=1)
    value: str = Field(..., min_length=1)


_CharacterMemoryAction = Literal[
    "aliases_add", "aliases_remove",
    "goals_long_term_add", "goals_long_term_remove",
    "goals_short_term_add", "goals_short_term_remove",
    "current_state_upsert", "current_state_remove",
    "relationships_upsert", "relationships_remove",
    "abilities_add", "abilities_remove", "secrets_add", "secrets_remove",
]
_CharacterStateMemoryAction = Literal[
    "injuries_add", "injuries_remove",
    "acquired_items_add", "acquired_items_remove",
    "current_relationships_upsert", "current_relationships_remove",
]


class _MemoryOperation(BaseModel):
    """Shared shape validation; each response collection narrows its actions."""

    model_config = ConfigDict(extra="forbid")

    action: _CharacterMemoryAction | _CharacterStateMemoryAction
    reference: str | None = None
    value: str | None = None

    @model_validator(mode="after")
    def _validate_operation_shape(self) -> "_MemoryOperation":
        list_actions = {
            "aliases_add",
            "aliases_remove",
            "goals_long_term_add",
            "goals_long_term_remove",
            "goals_short_term_add",
            "goals_short_term_remove",
            "abilities_add",
            "abilities_remove",
            "secrets_add",
            "secrets_remove",
            "injuries_add",
            "injuries_remove",
            "acquired_items_add",
            "acquired_items_remove",
        }
        if self.action in list_actions:
            if self.reference is not None or not self.value:
                raise ValueError("list action 只能给非空 value，不得给 reference")
            return self
        if self.action.startswith("current_state_"):
            if (
                not self.reference
                or (self.action.endswith("_upsert") and self.value is None)
                or (self.action.endswith("_remove") and self.value is not None)
            ):
                raise ValueError(
                    "current_state action 必须给 key reference；upsert 给 value，remove 不给"
                )
            return self
        if (
            not self.reference
            or (self.action.endswith("_upsert") and not self.value)
            or (self.action.endswith("_remove") and self.value is not None)
        ):
            raise ValueError(
                "relationship action 必须给 character reference；upsert 给 value，remove 不给"
            )
        return self


class _CharacterMemoryOperation(_MemoryOperation):
    action: _CharacterMemoryAction


class _CharacterStateMemoryOperation(_MemoryOperation):
    action: _CharacterStateMemoryAction


class _VoiceProfileDelta(BaseModel):
    model_config = ConfigDict(extra="forbid")

    tone: str = Field(..., min_length=1)
    sentence_style: str = Field(..., min_length=1)
    self_address: str = Field(..., min_length=1)
    addresses_others: list[_RelationshipEntryDelta] = Field(...)
    catchphrases: list[str] = Field(...)
    forbidden_phrases: list[str] = Field(...)


class _NewCharacterDelta(BaseModel):
    model_config = ConfigDict(extra="forbid")

    name: str = Field(..., min_length=1)
    archetype: str = Field(..., min_length=1)
    appearance: str = Field(..., min_length=1)
    personality: str = Field(..., min_length=1)
    background: str = Field(..., min_length=1)
    voice_profile: _VoiceProfileDelta


class _CharacterMemoryDelta(BaseModel):
    model_config = ConfigDict(extra="forbid")

    existing_character_id: str | None = None
    new_character: _NewCharacterDelta | None = None
    evidence_source_id: str = Field(..., min_length=1)
    operations: list[_CharacterMemoryOperation] = Field(...)

    @model_validator(mode="after")
    def _existing_xor_new(self) -> "_CharacterMemoryDelta":
        if (self.existing_character_id is None) == (self.new_character is None):
            raise ValueError(
                "character delta 必须且只能给 existing_character_id/new_character 之一"
            )
        if self.existing_character_id is not None and not self.operations:
            raise ValueError("既有 character delta 至少需要一条实际 operation")
        return self


class _CharacterStateMemoryDelta(BaseModel):
    model_config = ConfigDict(extra="forbid")

    character: str = Field(..., min_length=1)
    evidence_source_id: str = Field(..., min_length=1)
    physical_state: str | None = None
    operations: list[_CharacterStateMemoryOperation] = Field(...)
    state_changes: list[CharacterStateChange] = Field(...)
    progression_rank: str | None = None

    @model_validator(mode="after")
    def _require_actual_change(self) -> "_CharacterStateMemoryDelta":
        if (
            self.physical_state is None
            and not self.operations
            and not self.state_changes
            and self.progression_rank is None
        ):
            raise ValueError("character state delta 不得是空操作")
        return self


class _NewPlotThreadDelta(BaseModel):
    model_config = ConfigDict(extra="forbid")

    title: str = Field(..., min_length=1)
    description: str = Field(..., min_length=1)
    status: Literal["planted", "developing", "resolved", "abandoned"]
    importance: int = Field(..., ge=1, le=5)
    planned_payoff: str | None = None
    emergent_kind: Literal[
        "withheld_answer",
        "explicit_promise",
        "unresolved_consequence",
        "persistent_anomaly",
    ]
    expiry_policy: Literal["arc_end", "volume_end", "never"]


class _PlotThreadMemoryDelta(BaseModel):
    model_config = ConfigDict(extra="forbid")

    existing_thread_id: str | None = None
    new_thread: _NewPlotThreadDelta | None = None
    evidence_source_id: str = Field(..., min_length=1)
    status: Literal["planted", "developing", "resolved", "abandoned"] | None = Field(
        None,
        description=(
            "既有伏笔必填本章结束时的非空状态；状态未变时沿用冻结权威值。"
            "新伏笔此处必须为 null，状态只填在 new_thread.status。"
        ),
    )
    importance: int | None = Field(
        None, ge=1, le=5,
        description="既有伏笔未改变重要度时为 null；新伏笔只填 new_thread.importance。",
    )
    development: str = Field(...)
    resolved_in_current_chapter: bool
    related_characters: list[str] = Field(...)

    @model_validator(mode="after")
    def _existing_xor_new(self) -> "_PlotThreadMemoryDelta":
        if (self.existing_thread_id is None) == (self.new_thread is None):
            raise ValueError(
                "plot thread delta 必须且只能给 existing_thread_id/new_thread 之一"
            )
        if self.existing_thread_id is not None and self.status is None:
            raise ValueError("既有 plot thread delta 必须给出 status")
        if self.new_thread is not None and (
            self.status is not None or self.importance is not None
        ):
            raise ValueError("新 plot thread 的 status/importance 只能写在 new_thread")
        return self


class _TimelineMemoryDelta(BaseModel):
    model_config = ConfigDict(extra="forbid")

    description: str = Field(..., min_length=1)
    time_label: str = Field(..., min_length=1)
    relative_order: int = Field(..., ge=1)
    involved_characters: list[str] = Field(...)
    is_flashback: bool
    evidence_source_id: str = Field(..., min_length=1)


class _FactMemoryDelta(BaseModel):
    model_config = ConfigDict(extra="forbid")

    description: str = Field(..., min_length=1, max_length=100)
    involved_characters: list[str] = Field(...)
    evidence_source_id: str = Field(..., min_length=1)


class _SceneAnchorMemoryDelta(BaseModel):
    model_config = ConfigDict(extra="forbid")

    enter_dream_location: str = Field(...)
    enter_evidence_source_id: str = Field(...)
    wake_location: str = Field(...)
    wake_evidence_source_id: str = Field(...)
    has_transition_explanation: bool

    @model_validator(mode="after")
    def _paired_sources(self) -> "_SceneAnchorMemoryDelta":
        if bool(self.enter_dream_location) != bool(self.enter_evidence_source_id):
            raise ValueError("enter_dream_location 与 source ID 必须同时为空或存在")
        if bool(self.wake_location) != bool(self.wake_evidence_source_id):
            raise ValueError("wake_location 与 source ID 必须同时为空或存在")
        return self


class _MemoryCreativeDeltaPayload(BaseModel):
    """Evidence-bound semantic changes; no system/canonical object replay."""

    model_config = ConfigDict(extra="forbid")

    characters: list[_CharacterMemoryDelta] = Field(...)
    plot_threads: list[_PlotThreadMemoryDelta] = Field(...)
    timeline: list[_TimelineMemoryDelta] = Field(...)
    facts: list[_FactMemoryDelta] = Field(...)
    character_states: list[_CharacterStateMemoryDelta] = Field(...)
    scene_anchor: _SceneAnchorMemoryDelta | None = None


_CHARACTER_REFERENCE_OPERATION_ACTIONS = frozenset(
    {
        "relationships_upsert",
        "relationships_remove",
        "current_relationships_upsert",
        "current_relationships_remove",
    }
)
_LOCAL_CHARACTER_REFERENCE_PREFIX = "@new:"
_LOCAL_CHARACTER_REFERENCE_PATTERN = re.compile(r"^@new:([1-9][0-9]*)$")


def _parse_local_character_reference(reference: str, *, path: str) -> int:
    """Parse one canonical ``@new:N`` new-character ordinal."""

    match = _LOCAL_CHARACTER_REFERENCE_PATTERN.fullmatch(reference)
    if match is None:
        raise CreativeDeltaAssemblyError("invalid_local_reference", path, reference)
    try:
        return int(match.group(1))
    except ValueError as exc:
        raise CreativeDeltaAssemblyError(
            "invalid_local_reference",
            path,
            "new_character_ordinal 超出可解析范围",
        ) from exc


def _required_schema_property(
    node: dict[str, Any], name: str, *, path: str
) -> dict[str, Any]:
    properties = node.get("properties")
    if not isinstance(properties, dict):
        raise CreativeDeltaAssemblyError(
            "authority_schema_drift", path, "properties 缺失"
        )
    child = properties.get(name)
    if not isinstance(child, dict):
        raise CreativeDeltaAssemblyError(
            "authority_schema_drift", f"{path}.{name}", "字段 schema 缺失"
        )
    return child


def _required_schema_items(node: dict[str, Any], *, path: str) -> dict[str, Any]:
    if node.get("type") != "array" or not isinstance(node.get("items"), dict):
        raise CreativeDeltaAssemblyError(
            "authority_schema_drift", path, "array items schema 缺失"
        )
    return node["items"]


def _bind_array_cardinality(
    node: dict[str, Any],
    *,
    path: str,
    max_items: int,
    unique_items: bool = False,
) -> dict[str, Any]:
    """Require one known array shape and attach its request-time bound."""

    items = _required_schema_items(node, path=path)
    if max_items < 0:
        raise CreativeDeltaAssemblyError(
            "authority_schema_drift", path, "maxItems 不得小于零"
        )
    node["maxItems"] = max_items
    if unique_items:
        node["uniqueItems"] = True
    return items


def _unbounded_array_schema_paths(
    node: Any,
    *,
    path: str = "memory",
) -> list[str]:
    """Return every response array whose request schema lacks ``maxItems``."""

    if not isinstance(node, dict):
        return []
    paths: list[str] = []
    node_type = node.get("type")
    is_array = node_type == "array" or (
        isinstance(node_type, list) and "array" in node_type
    )
    if is_array and not (
        isinstance(node.get("maxItems"), int)
        and not isinstance(node.get("maxItems"), bool)
        and node["maxItems"] >= 0
    ):
        paths.append(path)
    properties = node.get("properties")
    if isinstance(properties, dict):
        for name, child in properties.items():
            paths.extend(_unbounded_array_schema_paths(child, path=f"{path}.{name}"))
    items = node.get("items")
    if isinstance(items, dict):
        paths.extend(_unbounded_array_schema_paths(items, path=f"{path}[]"))
    for keyword in ("anyOf", "oneOf", "allOf", "prefixItems"):
        branches = node.get(keyword)
        if isinstance(branches, list):
            for index, branch in enumerate(branches):
                paths.extend(
                    _unbounded_array_schema_paths(
                        branch,
                        path=f"{path}.{keyword}[{index}]",
                    )
                )
    return paths


@dataclass(frozen=True, slots=True)
class MemoryCharacterAuthorityContract:
    """Bind every memory character reference to one frozen request catalog.

    Exact IDs frozen before dispatch remain the only authority in schema-bound
    character fields.  Relationship operations may additionally reference the
    Nth ``new_character`` in the same response through canonical ``@new:N``
    syntax.  The legal local-token set is derived from the complete parsed
    delta before deterministic assembly, so forward and backward references
    share one unambiguous contract without widening any other field.
    """

    existing_character_ids: tuple[str, ...]

    @classmethod
    def freeze(
        cls, *, characters: list[Character]
    ) -> "MemoryCharacterAuthorityContract":
        character_ids = [item.character_id for item in characters]
        if len(character_ids) != len(set(character_ids)):
            raise CreativeDeltaAssemblyError(
                "ambiguous_authority_source",
                "memory.authority.characters",
                "冻结人物 stable ID 重复",
            )
        reserved = [
            character_id
            for character_id in character_ids
            if character_id.startswith(_LOCAL_CHARACTER_REFERENCE_PREFIX)
        ]
        if reserved:
            raise CreativeDeltaAssemblyError(
                "ambiguous_authority_source",
                "memory.authority.characters",
                f"冻结人物 stable ID 占用 local-reference 保留前缀:{reserved}",
            )
        return cls(existing_character_ids=tuple(sorted(character_ids)))

    def transform_schema(self, schema: dict[str, Any]) -> None:
        """Apply Hy3-safe enums to every structurally distinct reference."""

        ids = list(self.existing_character_ids)
        characters = _required_schema_property(schema, "characters", path="memory")
        character_item = _required_schema_items(characters, path="memory.characters")
        existing_character_id = _required_schema_property(
            character_item,
            "existing_character_id",
            path="memory.characters[]",
        )
        existing_character_id["enum"] = [*ids, None]

        new_character = _required_schema_property(
            character_item, "new_character", path="memory.characters[]"
        )
        voice_profile = _required_schema_property(
            new_character,
            "voice_profile",
            path="memory.characters[].new_character",
        )
        addresses = _required_schema_property(
            voice_profile,
            "addresses_others",
            path="memory.characters[].new_character.voice_profile",
        )
        self._bind_reference_array(
            addresses,
            path=("memory.characters[].new_character.voice_profile.addresses_others"),
            target_property="target",
        )

        plot_threads = _required_schema_property(schema, "plot_threads", path="memory")
        plot_thread_item = _required_schema_items(
            plot_threads, path="memory.plot_threads"
        )
        self._bind_reference_array(
            _required_schema_property(
                plot_thread_item,
                "related_characters",
                path="memory.plot_threads[]",
            ),
            path="memory.plot_threads[].related_characters",
        )

        for collection_name in ("timeline", "facts"):
            collection = _required_schema_property(
                schema, collection_name, path="memory"
            )
            item = _required_schema_items(collection, path=f"memory.{collection_name}")
            self._bind_reference_array(
                _required_schema_property(
                    item,
                    "involved_characters",
                    path=f"memory.{collection_name}[]",
                ),
                path=f"memory.{collection_name}[].involved_characters",
            )

        character_states = _required_schema_property(
            schema, "character_states", path="memory"
        )
        state_item = _required_schema_items(
            character_states, path="memory.character_states"
        )
        if ids:
            _required_schema_property(
                state_item, "character", path="memory.character_states[]"
            )["enum"] = ids
        else:
            character_states["maxItems"] = 0

    def _bind_reference_array(
        self,
        array_schema: dict[str, Any],
        *,
        path: str,
        target_property: str | None = None,
    ) -> None:
        if not self.existing_character_ids:
            array_schema["maxItems"] = 0
            return
        item = _required_schema_items(array_schema, path=path)
        if target_property is not None:
            item = _required_schema_property(item, target_property, path=f"{path}[]")
        item["enum"] = list(self.existing_character_ids)

    def validate_delta(self, delta: _MemoryCreativeDeltaPayload) -> None:
        """Recheck all references, including action-dependent operation slots."""

        new_character_count = sum(
            item.new_character is not None for item in delta.characters
        )
        local_references = frozenset(
            f"{_LOCAL_CHARACTER_REFERENCE_PREFIX}{ordinal}"
            for ordinal in range(1, new_character_count + 1)
        )

        def require_known(reference: str, *, path: str) -> None:
            if reference not in self.existing_character_ids:
                raise CreativeDeltaAssemblyError(
                    "unknown_authority_reference", path, reference
                )

        for index, character in enumerate(delta.characters):
            path = f"memory.characters[{index}]"
            if character.existing_character_id is not None:
                require_known(
                    character.existing_character_id,
                    path=f"{path}.existing_character_id",
                )
            if character.new_character is not None:
                for target_index, entry in enumerate(
                    character.new_character.voice_profile.addresses_others
                ):
                    require_known(
                        entry.target,
                        path=(
                            f"{path}.new_character.voice_profile."
                            f"addresses_others[{target_index}].target"
                        ),
                    )
            self._validate_operation_references(
                character.operations,
                path=f"{path}.operations",
                local_references=local_references,
            )

        for index, plot_thread in enumerate(delta.plot_threads):
            for ref_index, reference in enumerate(plot_thread.related_characters):
                require_known(
                    reference,
                    path=(
                        f"memory.plot_threads[{index}].related_characters[{ref_index}]"
                    ),
                )
        for index, timeline_item in enumerate(delta.timeline):
            for ref_index, reference in enumerate(timeline_item.involved_characters):
                require_known(
                    reference,
                    path=(f"memory.timeline[{index}].involved_characters[{ref_index}]"),
                )
        for index, fact_item in enumerate(delta.facts):
            for ref_index, reference in enumerate(fact_item.involved_characters):
                require_known(
                    reference,
                    path=(f"memory.facts[{index}].involved_characters[{ref_index}]"),
                )
        for index, state in enumerate(delta.character_states):
            path = f"memory.character_states[{index}]"
            require_known(state.character, path=f"{path}.character")
            self._validate_operation_references(
                state.operations,
                path=f"{path}.operations",
                local_references=local_references,
            )

    def _validate_operation_references(
        self,
        operations: Sequence[_MemoryOperation],
        *,
        path: str,
        local_references: frozenset[str],
    ) -> None:
        for index, operation in enumerate(operations):
            reference = operation.reference
            if operation.action not in _CHARACTER_REFERENCE_OPERATION_ACTIONS:
                if reference is not None and reference.startswith(
                    _LOCAL_CHARACTER_REFERENCE_PREFIX
                ):
                    raise CreativeDeltaAssemblyError(
                        "unknown_authority_reference",
                        f"{path}[{index}].reference",
                        f"local reference 仅允许 relationship operation:{reference}",
                    )
                continue
            assert reference is not None
            if reference in self.existing_character_ids:
                continue
            reference_path = f"{path}[{index}].reference"
            if reference.startswith(_LOCAL_CHARACTER_REFERENCE_PREFIX):
                ordinal = _parse_local_character_reference(
                    reference,
                    path=reference_path,
                )
                if reference not in local_references:
                    raise CreativeDeltaAssemblyError(
                        "unknown_local_reference",
                        reference_path,
                        f"new_character_ordinal={ordinal}",
                    )
                continue
            if reference not in self.existing_character_ids:
                raise CreativeDeltaAssemblyError(
                    "unknown_authority_reference",
                    reference_path,
                    str(reference),
                )


@dataclass(frozen=True, slots=True)
class MemoryCardinalityAuthorityContract:
    """Bound every memory response list to frozen chapter authority.

    The evidence catalog is already split into the atomic text spans that a
    semantic delta may cite.  It therefore owns the capacity for authored
    change/event/value slots.  Existing character and plot-thread catalogs own
    the capacity for exact-reference lists; the one-new-thread allowance is
    the existing emergent-thread business contract.  No prose, event, action,
    or relationship value is supplied or rewritten here.
    """

    evidence_source_ids: tuple[str, ...]
    existing_character_ids: tuple[str, ...]
    existing_plot_thread_ids: tuple[str, ...]

    @classmethod
    def freeze(
        cls,
        *,
        source_catalog: tuple[EvidenceSource, ...],
        characters: list[Character],
        plot_threads: list[PlotThread],
    ) -> "MemoryCardinalityAuthorityContract":
        evidence_ids = tuple(item.source_id for item in source_catalog)
        character_ids = tuple(sorted(item.character_id for item in characters))
        thread_ids = tuple(sorted(item.thread_id for item in plot_threads))
        for ids, path in (
            (evidence_ids, "memory.authority.evidence_sources"),
            (character_ids, "memory.authority.characters"),
            (thread_ids, "memory.authority.plot_threads"),
        ):
            if len(ids) != len(set(ids)):
                raise CreativeDeltaAssemblyError(
                    "ambiguous_authority_source", path, "冻结 stable ID 重复"
                )
        return cls(
            evidence_source_ids=evidence_ids,
            existing_character_ids=character_ids,
            existing_plot_thread_ids=thread_ids,
        )

    @property
    def evidence_slot_count(self) -> int:
        return len(self.evidence_source_ids)

    @property
    def character_slot_count(self) -> int:
        return len(self.existing_character_ids)

    @property
    def plot_thread_slot_count(self) -> int:
        new_thread_slots = min(
            self.evidence_slot_count,
            _EMERGENT_MAX_NEW_PER_CHAPTER,
        )
        return len(self.existing_plot_thread_ids) + new_thread_slots

    def transform_schema(self, schema: dict[str, Any]) -> None:
        """附加可移植的十四数组基数边界。

        本形式为历史 package 重建保留标准 JSON Schema ``uniqueItems``。
        TokenHub Hy3 生产外发必须使用 :meth:`transform_hy3_wire_schema`；
        后者保留同一组上限，并由本地 authority validation 拒绝重复结果。
        """

        self._transform_schema(schema, include_unique_items=True)

    def transform_hy3_wire_schema(self, schema: dict[str, Any]) -> None:
        """附加全部基数边界，但不生成 Hy3 未实现的 ``uniqueItems``。"""

        self._transform_schema(schema, include_unique_items=False)

    def _transform_schema(
        self,
        schema: dict[str, Any],
        *,
        include_unique_items: bool,
    ) -> None:
        """按显式方言选择应用同一份 authority 基数边界。"""

        evidence_slots = self.evidence_slot_count
        character_slots = self.character_slot_count

        characters = _required_schema_property(schema, "characters", path="memory")
        character_item = _bind_array_cardinality(
            characters,
            path="memory.characters",
            max_items=character_slots + evidence_slots,
        )
        _bind_array_cardinality(
            _required_schema_property(
                character_item, "operations", path="memory.characters[]"
            ),
            path="memory.characters[].operations",
            max_items=evidence_slots,
            unique_items=include_unique_items,
        )
        new_character = _required_schema_property(
            character_item, "new_character", path="memory.characters[]"
        )
        voice_profile = _required_schema_property(
            new_character,
            "voice_profile",
            path="memory.characters[].new_character",
        )
        _bind_array_cardinality(
            _required_schema_property(
                voice_profile,
                "addresses_others",
                path="memory.characters[].new_character.voice_profile",
            ),
            path="memory.characters[].new_character.voice_profile.addresses_others",
            max_items=character_slots,
            unique_items=include_unique_items,
        )
        for field_name in ("catchphrases", "forbidden_phrases"):
            _bind_array_cardinality(
                _required_schema_property(
                    voice_profile,
                    field_name,
                    path="memory.characters[].new_character.voice_profile",
                ),
                path=(f"memory.characters[].new_character.voice_profile.{field_name}"),
                max_items=evidence_slots,
            )

        plot_threads = _required_schema_property(schema, "plot_threads", path="memory")
        plot_thread_item = _bind_array_cardinality(
            plot_threads,
            path="memory.plot_threads",
            max_items=self.plot_thread_slot_count,
        )
        _bind_array_cardinality(
            _required_schema_property(
                plot_thread_item,
                "related_characters",
                path="memory.plot_threads[]",
            ),
            path="memory.plot_threads[].related_characters",
            max_items=character_slots,
            unique_items=include_unique_items,
        )

        for collection_name in ("timeline", "facts"):
            collection = _required_schema_property(
                schema, collection_name, path="memory"
            )
            item = _bind_array_cardinality(
                collection,
                path=f"memory.{collection_name}",
                max_items=evidence_slots,
            )
            _bind_array_cardinality(
                _required_schema_property(
                    item,
                    "involved_characters",
                    path=f"memory.{collection_name}[]",
                ),
                path=f"memory.{collection_name}[].involved_characters",
                max_items=character_slots,
                unique_items=include_unique_items,
            )

        character_states = _required_schema_property(
            schema, "character_states", path="memory"
        )
        state_item = _bind_array_cardinality(
            character_states,
            path="memory.character_states",
            max_items=character_slots,
        )
        _bind_array_cardinality(
            _required_schema_property(
                state_item, "operations", path="memory.character_states[]"
            ),
            path="memory.character_states[].operations",
            max_items=evidence_slots,
            unique_items=include_unique_items,
        )
        _bind_array_cardinality(
            _required_schema_property(
                state_item, "state_changes", path="memory.character_states[]"
            ),
            path="memory.character_states[].state_changes",
            max_items=evidence_slots,
        )
        unbounded = _unbounded_array_schema_paths(schema)
        if unbounded:
            raise CreativeDeltaAssemblyError(
                "authority_schema_drift",
                "memory",
                f"未绑定 maxItems 的数组:{unbounded}",
            )

    @staticmethod
    def _require_at_most(values: list[Any], *, path: str, maximum: int) -> None:
        if len(values) > maximum:
            raise CreativeDeltaAssemblyError(
                "authority_cardinality_exceeded",
                path,
                f"actual={len(values)},max={maximum}",
            )

    def validate_delta(self, delta: _MemoryCreativeDeltaPayload) -> None:
        """Recheck the same dynamic bounds before deterministic assembly."""

        evidence_slots = self.evidence_slot_count
        character_slots = self.character_slot_count
        self._require_at_most(
            delta.characters,
            path="memory.characters",
            maximum=character_slots + evidence_slots,
        )
        _reject_duplicates(
            [
                item.existing_character_id
                for item in delta.characters
                if item.existing_character_id is not None
            ],
            path="memory.characters.existing_character_id",
        )
        new_character_count = sum(
            item.new_character is not None for item in delta.characters
        )
        self._require_at_most(
            [None] * new_character_count,
            path="memory.characters.new_character",
            maximum=evidence_slots,
        )
        for index, character in enumerate(delta.characters):
            path = f"memory.characters[{index}]"
            self._require_at_most(
                character.operations,
                path=f"{path}.operations",
                maximum=evidence_slots,
            )
            _reject_duplicates(
                [item.model_dump(mode="json") for item in character.operations],
                path=f"{path}.operations",
            )
            if character.new_character is None:
                continue
            voice = character.new_character.voice_profile
            self._require_at_most(
                voice.addresses_others,
                path=f"{path}.new_character.voice_profile.addresses_others",
                maximum=character_slots,
            )
            _reject_duplicates(
                [item.target for item in voice.addresses_others],
                path=f"{path}.new_character.voice_profile.addresses_others",
            )
            for field_name in ("catchphrases", "forbidden_phrases"):
                self._require_at_most(
                    getattr(voice, field_name),
                    path=f"{path}.new_character.voice_profile.{field_name}",
                    maximum=evidence_slots,
                )

        self._require_at_most(
            delta.plot_threads,
            path="memory.plot_threads",
            maximum=self.plot_thread_slot_count,
        )
        _reject_duplicates(
            [
                item.existing_thread_id
                for item in delta.plot_threads
                if item.existing_thread_id is not None
            ],
            path="memory.plot_threads.existing_thread_id",
        )
        new_thread_count = sum(
            item.new_thread is not None for item in delta.plot_threads
        )
        self._require_at_most(
            [None] * new_thread_count,
            path="memory.plot_threads.new_thread",
            maximum=min(evidence_slots, _EMERGENT_MAX_NEW_PER_CHAPTER),
        )
        for index, thread in enumerate(delta.plot_threads):
            self._require_at_most(
                thread.related_characters,
                path=f"memory.plot_threads[{index}].related_characters",
                maximum=character_slots,
            )
            _reject_duplicates(
                thread.related_characters,
                path=f"memory.plot_threads[{index}].related_characters",
            )

        for collection_name in ("timeline", "facts"):
            collection = getattr(delta, collection_name)
            self._require_at_most(
                collection,
                path=f"memory.{collection_name}",
                maximum=evidence_slots,
            )
            for index, item in enumerate(collection):
                self._require_at_most(
                    item.involved_characters,
                    path=f"memory.{collection_name}[{index}].involved_characters",
                    maximum=character_slots,
                )
                _reject_duplicates(
                    item.involved_characters,
                    path=f"memory.{collection_name}[{index}].involved_characters",
                )

        self._require_at_most(
            delta.character_states,
            path="memory.character_states",
            maximum=character_slots,
        )
        _reject_duplicates(
            [item.character for item in delta.character_states],
            path="memory.character_states.character",
        )
        for index, state in enumerate(delta.character_states):
            path = f"memory.character_states[{index}]"
            self._require_at_most(
                state.operations,
                path=f"{path}.operations",
                maximum=evidence_slots,
            )
            _reject_duplicates(
                [item.model_dump(mode="json") for item in state.operations],
                path=f"{path}.operations",
            )
            self._require_at_most(
                state.state_changes,
                path=f"{path}.state_changes",
                maximum=evidence_slots,
            )


@dataclass(frozen=True, slots=True)
class MemoryResponseAuthorityContract:
    """One transform/validator shared by production and successor runtimes."""

    characters: MemoryCharacterAuthorityContract
    cardinality: MemoryCardinalityAuthorityContract

    def transform_schema(self, schema: dict[str, Any]) -> None:
        self.characters.transform_schema(schema)
        self.cardinality.transform_schema(schema)

    def transform_hy3_wire_schema(self, schema: dict[str, Any]) -> None:
        """从同一 authority 构造经 Provider 实证可表达的 Hy3 wire 结构。"""

        self.characters.transform_schema(schema)
        self.cardinality.transform_hy3_wire_schema(schema)

    def validate_delta(self, delta: _MemoryCreativeDeltaPayload) -> None:
        self.characters.validate_delta(delta)
        self.cardinality.validate_delta(delta)


def build_memory_character_authority_contract(
    *, characters: list[Character]
) -> MemoryCharacterAuthorityContract:
    """Freeze the request-time character ID catalog used by schema and assembly."""

    return MemoryCharacterAuthorityContract.freeze(characters=characters)


def build_memory_response_authority_contract(
    *,
    characters: list[Character],
    plot_threads: list[PlotThread],
    source_catalog: tuple[EvidenceSource, ...],
) -> MemoryResponseAuthorityContract:
    """Freeze every request-time character and list-capacity authority."""

    return MemoryResponseAuthorityContract(
        characters=build_memory_character_authority_contract(characters=characters),
        cardinality=MemoryCardinalityAuthorityContract.freeze(
            source_catalog=source_catalog,
            characters=characters,
            plot_threads=plot_threads,
        ),
    )


def _memory_authority_payload(
    *,
    content: str,
    card: ChapterCard,
    workspace_id: str,
    characters: list[Character],
    plot_threads: list[PlotThread],
) -> dict[str, Any]:
    return {
        "source_text_hash": hashlib.sha256(content.encode("utf-8")).hexdigest(),
        "card": card.model_dump(mode="json"),
        "workspace_id": workspace_id,
        "characters": [
            item.model_dump(mode="json")
            for item in sorted(characters, key=lambda value: value.character_id)
        ],
        "plot_threads": [
            item.model_dump(mode="json")
            for item in sorted(plot_threads, key=lambda value: value.thread_id)
        ],
    }


def _reject_duplicates(values: list[Any], *, path: str) -> None:
    encoded = [
        json.dumps(value, ensure_ascii=False, sort_keys=True, default=str)
        for value in values
    ]
    if len(encoded) != len(set(encoded)):
        raise CreativeDeltaAssemblyError(
            "ambiguous_model_delta", path, "增量包含重复项"
        )


def _apply_list_delta(
    existing: list[str], additions: list[str], removals: list[str], *, path: str
) -> list[str]:
    _reject_duplicates(existing, path=f"{path}.authority")
    _reject_duplicates(additions, path=f"{path}.add")
    _reject_duplicates(removals, path=f"{path}.remove")
    overlap = set(additions) & set(removals)
    if overlap:
        raise CreativeDeltaAssemblyError(
            "authority_conflict", path, f"同值同时 add/remove:{sorted(overlap)}"
        )
    missing = [item for item in removals if item not in existing]
    if missing:
        raise CreativeDeltaAssemblyError(
            "authority_conflict", path, f"remove 不在权威当前值:{missing}"
        )
    result = [item for item in existing if item not in removals]
    result.extend(item for item in additions if item not in result)
    return result


def _stable_memory_id(kind: str, chapter_id: str, slot: int) -> str:
    return f"emergent-{kind}-{chapter_id}-{slot:03d}"


def _resolve_character_reference(
    reference: str,
    *,
    existing_ids: set[str],
    new_id_by_ordinal: dict[int, str] | None,
    path: str,
) -> str:
    if not reference.startswith(_LOCAL_CHARACTER_REFERENCE_PREFIX):
        if reference not in existing_ids:
            raise CreativeDeltaAssemblyError(
                "unknown_authority_reference",
                path,
                reference,
            )
        return reference
    if new_id_by_ordinal is None:
        raise CreativeDeltaAssemblyError(
            "unknown_authority_reference",
            path,
            f"local reference 仅允许 relationship operation:{reference}",
        )
    ordinal = _parse_local_character_reference(reference, path=path)
    mapped = new_id_by_ordinal.get(ordinal)
    if mapped is None:
        raise CreativeDeltaAssemblyError(
            "unknown_local_reference",
            path,
            f"new_character_ordinal={ordinal}",
        )
    return mapped


def _relationship_map(
    entries: list[_RelationshipEntryDelta],
    *,
    existing_ids: set[str],
    path: str,
) -> dict[str, str]:
    mapped: dict[str, str] = {}
    for index, entry in enumerate(entries):
        target = _resolve_character_reference(
            entry.target,
            existing_ids=existing_ids,
            new_id_by_ordinal=None,
            path=f"{path}[{index}].target",
        )
        if target in mapped:
            raise CreativeDeltaAssemblyError(
                "ambiguous_model_delta", path, f"重复 target={target}"
            )
        mapped[target] = entry.value
    return mapped


def _apply_mapping_delta(
    existing: dict[str, Any],
    updates: dict[str, Any],
    removals: list[str],
    *,
    path: str,
) -> dict[str, Any]:
    _reject_duplicates(removals, path=f"{path}.remove")
    overlap = set(updates) & set(removals)
    if overlap:
        raise CreativeDeltaAssemblyError(
            "authority_conflict", path, f"同 key 同时 update/remove:{sorted(overlap)}"
        )
    missing = [key for key in removals if key not in existing]
    if missing:
        raise CreativeDeltaAssemblyError(
            "authority_conflict", path, f"remove key 不在权威当前值:{missing}"
        )
    result = {key: value for key, value in existing.items() if key not in removals}
    result.update(updates)
    return result


def _group_memory_operations(
    operations: Sequence[_MemoryOperation],
    *,
    allowed_actions: set[str],
    path: str,
) -> dict[str, list[_MemoryOperation]]:
    _reject_duplicates(
        [item.model_dump(mode="json") for item in operations],
        path=path,
    )
    grouped: dict[str, list[_MemoryOperation]] = {}
    for index, item in enumerate(operations):
        if item.action not in allowed_actions:
            raise CreativeDeltaAssemblyError(
                "authority_conflict",
                f"{path}[{index}].action",
                f"{item.action} 不属于当前对象",
            )
        grouped.setdefault(item.action, []).append(item)
    return grouped


def _list_operation_delta(
    grouped: dict[str, list[_MemoryOperation]], field: str
) -> tuple[list[str], list[str]]:
    additions: list[str] = []
    removals: list[str] = []
    for item in grouped.get(f"{field}_add", []):
        assert item.value is not None
        additions.append(item.value)
    for item in grouped.get(f"{field}_remove", []):
        assert item.value is not None
        removals.append(item.value)
    return additions, removals


def _string_map_operation_delta(
    grouped: dict[str, list[_MemoryOperation]], field: str, *, path: str
) -> tuple[dict[str, str], list[str]]:
    updates: dict[str, str] = {}
    removals: list[str] = []
    for index, item in enumerate(grouped.get(f"{field}_upsert", [])):
        assert item.reference is not None
        if item.reference in updates:
            raise CreativeDeltaAssemblyError(
                "ambiguous_model_delta",
                f"{path}[{index}].reference",
                item.reference,
            )
        assert item.value is not None
        updates[item.reference] = item.value
    for item in grouped.get(f"{field}_remove", []):
        assert item.reference is not None
        removals.append(item.reference)
    return updates, removals


def _relationship_operation_delta(
    grouped: dict[str, list[_MemoryOperation]],
    field: str,
    *,
    existing_ids: set[str],
    new_id_by_ordinal: dict[int, str],
    path: str,
) -> tuple[dict[str, str], list[str]]:
    updates: dict[str, str] = {}
    removals: list[str] = []
    for index, item in enumerate(grouped.get(f"{field}_upsert", [])):
        assert item.reference is not None
        target = _resolve_character_reference(
            item.reference,
            existing_ids=existing_ids,
            new_id_by_ordinal=new_id_by_ordinal,
            path=f"{path}[{index}].reference",
        )
        if target in updates:
            raise CreativeDeltaAssemblyError(
                "ambiguous_model_delta", f"{path}[{index}].reference", target
            )
        assert item.value is not None
        updates[target] = item.value
    for index, item in enumerate(grouped.get(f"{field}_remove", [])):
        assert item.reference is not None
        removals.append(
            _resolve_character_reference(
                item.reference,
                existing_ids=existing_ids,
                new_id_by_ordinal=new_id_by_ordinal,
                path=f"{path}[{index}].reference",
            )
        )
    return updates, removals


def _assemble_memory_delta(
    *,
    delta: _MemoryCreativeDeltaPayload,
    content: str,
    card: ChapterCard,
    workspace_id: str,
    existing_characters: list[Character],
    existing_plot_threads: list[PlotThread],
    source_catalog: tuple[EvidenceSource, ...],
    authority: FrozenAuthority,
    current_authority: dict[str, Any],
) -> tuple[_ExtractorLLMPayload, Any]:
    authority.assert_matches(current_authority)
    character_by_id = {item.character_id: item for item in existing_characters}
    thread_by_id = {item.thread_id: item for item in existing_plot_threads}
    existing_ids = set(character_by_id)

    new_id_by_slot: dict[int, str] = {}
    new_id_by_ordinal: dict[int, str] = {}
    new_character_ordinal = 0
    claimed_existing: list[str] = []
    for slot, character_delta in enumerate(delta.characters, start=1):
        if character_delta.existing_character_id is not None:
            if character_delta.existing_character_id not in character_by_id:
                raise CreativeDeltaAssemblyError(
                    "unknown_authority_reference",
                    f"memory.characters[{slot - 1}].existing_character_id",
                    character_delta.existing_character_id,
                )
            claimed_existing.append(character_delta.existing_character_id)
        else:
            new_character_ordinal += 1
            new_id = _stable_memory_id("char", card.chapter_id, slot)
            if new_id in existing_ids:
                raise CreativeDeltaAssemblyError(
                    "stable_id_collision",
                    f"memory.characters[{slot - 1}]",
                    new_id,
                )
            new_id_by_slot[slot] = new_id
            new_id_by_ordinal[new_character_ordinal] = new_id
    _reject_duplicates(claimed_existing, path="memory.characters.existing_character_id")

    def evidence(source_id: str, path: str) -> str:
        return resolve_evidence_source(
            source_id=source_id,
            text=content,
            catalog=source_catalog,
            field_path=path,
        )

    extracted_characters: list[ExtractedCharacter] = []
    for slot, character_delta in enumerate(delta.characters, start=1):
        path = f"memory.characters[{slot - 1}]"
        evidence(character_delta.evidence_source_id, f"{path}.evidence_source_id")
        operations = _group_memory_operations(
            character_delta.operations,
            allowed_actions=set(get_args(_CharacterMemoryAction)),
            path=f"{path}.operations",
        )
        if character_delta.existing_character_id is not None:
            existing_character = character_by_id[character_delta.existing_character_id]
            character_id = existing_character.character_id
            name = existing_character.name
            archetype = existing_character.archetype
            appearance = existing_character.appearance
            personality = existing_character.personality
            background = existing_character.background
            voice_profile = existing_character.voice_profile
            aliases_base = list(existing_character.aliases)
            goals_long_term_base = list(existing_character.goals_long_term)
            goals_short_term_base = list(existing_character.goals_short_term)
            current_state_base = dict(existing_character.current_state)
            relationships_base = dict(existing_character.relationships)
            abilities_base = list(existing_character.abilities)
            secrets_base = list(existing_character.secrets)
        else:
            assert character_delta.new_character is not None
            new = character_delta.new_character
            character_id = new_id_by_slot[slot]
            name = new.name
            archetype = new.archetype
            appearance = new.appearance
            personality = new.personality
            background = new.background
            addresses = _relationship_map(
                new.voice_profile.addresses_others,
                existing_ids=existing_ids,
                path=f"{path}.new_character.voice_profile.addresses_others",
            )
            voice_profile = VoiceProfile(
                tone=new.voice_profile.tone,
                sentence_style=new.voice_profile.sentence_style,
                self_address=new.voice_profile.self_address,
                addresses_others=addresses,
                catchphrases=list(new.voice_profile.catchphrases),
                forbidden_phrases=list(new.voice_profile.forbidden_phrases),
            )
            aliases_base = []
            goals_long_term_base = []
            goals_short_term_base = []
            current_state_base = {}
            relationships_base = {}
            abilities_base = []
            secrets_base = []

        aliases_add, aliases_remove = _list_operation_delta(operations, "aliases")
        aliases = _apply_list_delta(
            aliases_base,
            aliases_add,
            aliases_remove,
            path=f"{path}.aliases",
        )
        long_add, long_remove = _list_operation_delta(operations, "goals_long_term")
        goals_long_term = _apply_list_delta(
            goals_long_term_base,
            long_add,
            long_remove,
            path=f"{path}.goals_long_term",
        )
        short_add, short_remove = _list_operation_delta(operations, "goals_short_term")
        goals_short_term = _apply_list_delta(
            goals_short_term_base,
            short_add,
            short_remove,
            path=f"{path}.goals_short_term",
        )
        current_state_updates, current_state_removals = _string_map_operation_delta(
            operations,
            "current_state",
            path=f"{path}.operations.current_state",
        )
        current_state = _apply_mapping_delta(
            current_state_base,
            current_state_updates,
            current_state_removals,
            path=f"{path}.current_state",
        )
        relationship_updates, relationship_removals = _relationship_operation_delta(
            operations,
            "relationships",
            existing_ids=existing_ids,
            new_id_by_ordinal=new_id_by_ordinal,
            path=f"{path}.operations.relationships",
        )
        relationships = _apply_mapping_delta(
            relationships_base,
            relationship_updates,
            relationship_removals,
            path=f"{path}.relationships",
        )
        abilities_add, abilities_remove = _list_operation_delta(operations, "abilities")
        abilities = _apply_list_delta(
            abilities_base,
            abilities_add,
            abilities_remove,
            path=f"{path}.abilities",
        )
        secrets_add, secrets_remove = _list_operation_delta(operations, "secrets")
        secrets = _apply_list_delta(
            secrets_base,
            secrets_add,
            secrets_remove,
            path=f"{path}.secrets",
        )
        extracted_characters.append(
            ExtractedCharacter(
                character_id=character_id,
                name=name,
                archetype=archetype,
                appearance=appearance,
                personality=personality,
                background=background,
                voice_profile=voice_profile,
                goals_long_term=goals_long_term,
                goals_short_term=goals_short_term,
                aliases=aliases,
                current_state=current_state,
                relationships=relationships,
                abilities=abilities,
                secrets=secrets,
            )
        )

    extracted_states: list[ExtractedCharacterState] = []
    chapter_seq = chapter_seq_from_id(card.chapter_id)
    for index, state_delta in enumerate(delta.character_states):
        path = f"memory.character_states[{index}]"
        evidence(state_delta.evidence_source_id, f"{path}.evidence_source_id")
        operations = _group_memory_operations(
            state_delta.operations,
            allowed_actions=set(get_args(_CharacterStateMemoryAction)),
            path=f"{path}.operations",
        )
        character_id = _resolve_character_reference(
            state_delta.character,
            existing_ids=existing_ids,
            new_id_by_ordinal=None,
            path=f"{path}.character",
        )
        state_character = character_by_id.get(character_id)
        prior = None
        if state_character is not None and chapter_seq is not None:
            prior = latest_state_snapshot_before(
                state_character.state_timeline, chapter_seq
            )
        injuries_add, injuries_remove = _list_operation_delta(operations, "injuries")
        injuries = _apply_list_delta(
            list(prior.injuries) if prior else [],
            injuries_add,
            injuries_remove,
            path=f"{path}.injuries",
        )
        items_add, items_remove = _list_operation_delta(operations, "acquired_items")
        acquired_items = _apply_list_delta(
            list(prior.acquired_items) if prior else [],
            items_add,
            items_remove,
            path=f"{path}.acquired_items",
        )
        relationship_updates, relationship_removals = _relationship_operation_delta(
            operations,
            "current_relationships",
            existing_ids=existing_ids,
            new_id_by_ordinal=new_id_by_ordinal,
            path=f"{path}.operations.current_relationships",
        )
        relationships = _apply_mapping_delta(
            dict(prior.current_relationships) if prior else {},
            relationship_updates,
            relationship_removals,
            path=f"{path}.current_relationships",
        )
        extracted_states.append(
            ExtractedCharacterState(
                character_id=character_id,
                physical_state=(
                    state_delta.physical_state
                    if state_delta.physical_state is not None
                    else (prior.physical_state if prior else "")
                ),
                injuries=injuries,
                acquired_items=acquired_items,
                current_relationships=relationships,
                state_changes=list(state_delta.state_changes),
                progression_rank=state_delta.progression_rank,
            )
        )

    claimed_threads: list[str] = []
    extracted_threads: list[ExtractedPlotThread] = []
    for index, thread_delta in enumerate(delta.plot_threads, start=1):
        path = f"memory.plot_threads[{index - 1}]"
        quote = evidence(thread_delta.evidence_source_id, f"{path}.evidence_source_id")
        related_characters = [
            _resolve_character_reference(
                reference,
                existing_ids=existing_ids,
                new_id_by_ordinal=None,
                path=f"{path}.related_characters[{ref_index}]",
            )
            for ref_index, reference in enumerate(thread_delta.related_characters)
        ]
        _reject_duplicates(related_characters, path=f"{path}.related_characters")
        if thread_delta.existing_thread_id is not None:
            existing_thread = thread_by_id.get(thread_delta.existing_thread_id)
            if existing_thread is None:
                raise CreativeDeltaAssemblyError(
                    "unknown_authority_reference",
                    f"{path}.existing_thread_id",
                    thread_delta.existing_thread_id,
                )
            claimed_threads.append(existing_thread.thread_id)
            assert thread_delta.status is not None
            description = existing_thread.description
            if thread_delta.development:
                description = f"{description}\n{thread_delta.development}"
            actual_payoff = (
                card.chapter_id
                if thread_delta.resolved_in_current_chapter
                else existing_thread.actual_payoff
            )
            if thread_delta.status == "resolved" and actual_payoff is None:
                raise CreativeDeltaAssemblyError(
                    "authority_conflict",
                    f"{path}.resolved_in_current_chapter",
                    "status=resolved 必须有当前或既有 actual payoff",
                )
            extracted_threads.append(
                ExtractedPlotThread(
                    thread_id=existing_thread.thread_id,
                    title=existing_thread.title,
                    status=thread_delta.status,
                    importance=thread_delta.importance or existing_thread.importance,
                    planted_chapter=existing_thread.planted_chapter or card.chapter_id,
                    description=description,
                    planned_payoff=existing_thread.planned_payoff,
                    actual_payoff=actual_payoff,
                    related_characters=related_characters,
                    evidence_quote=quote,
                )
            )
        else:
            assert thread_delta.new_thread is not None
            new = thread_delta.new_thread
            if thread_delta.development:
                raise CreativeDeltaAssemblyError(
                    "ambiguous_model_delta",
                    f"{path}.development",
                    "新伏笔 description 只能写在 new_thread",
                )
            thread_id = _stable_memory_id("thread", card.chapter_id, index)
            if thread_id in thread_by_id:
                raise CreativeDeltaAssemblyError("stable_id_collision", path, thread_id)
            actual_payoff = (
                card.chapter_id if thread_delta.resolved_in_current_chapter else None
            )
            if new.status == "resolved" and actual_payoff is None:
                raise CreativeDeltaAssemblyError(
                    "authority_conflict",
                    f"{path}.resolved_in_current_chapter",
                    "新伏笔 status=resolved 必须显式标当前兑现",
                )
            extracted_threads.append(
                ExtractedPlotThread(
                    thread_id=thread_id,
                    title=new.title,
                    status=new.status,
                    importance=new.importance,
                    planted_chapter=card.chapter_id,
                    description=new.description,
                    planned_payoff=new.planned_payoff,
                    actual_payoff=actual_payoff,
                    related_characters=related_characters,
                    evidence_quote=quote,
                    emergent_kind=new.emergent_kind,
                    expiry_policy=new.expiry_policy,
                )
            )
    _reject_duplicates(claimed_threads, path="memory.plot_threads.existing_thread_id")

    extracted_timeline: list[ExtractedTimelineEntry] = []
    for index, timeline_delta in enumerate(delta.timeline, start=1):
        path = f"memory.timeline[{index - 1}]"
        evidence(timeline_delta.evidence_source_id, f"{path}.evidence_source_id")
        involved = [
            _resolve_character_reference(
                reference,
                existing_ids=existing_ids,
                new_id_by_ordinal=None,
                path=f"{path}.involved_characters[{ref_index}]",
            )
            for ref_index, reference in enumerate(timeline_delta.involved_characters)
        ]
        _reject_duplicates(involved, path=f"{path}.involved_characters")
        extracted_timeline.append(
            ExtractedTimelineEntry(
                entry_id=_stable_memory_id("timeline", card.chapter_id, index),
                description=timeline_delta.description,
                time_label=timeline_delta.time_label,
                time_order=timeline_delta.relative_order,
                involved_characters=involved,
                is_flashback=timeline_delta.is_flashback,
            )
        )

    extracted_facts: list[FactEntry] = []
    for index, fact_delta in enumerate(delta.facts, start=1):
        path = f"memory.facts[{index - 1}]"
        evidence(fact_delta.evidence_source_id, f"{path}.evidence_source_id")
        involved = [
            _resolve_character_reference(
                reference,
                existing_ids=existing_ids,
                new_id_by_ordinal=None,
                path=f"{path}.involved_characters[{ref_index}]",
            )
            for ref_index, reference in enumerate(fact_delta.involved_characters)
        ]
        _reject_duplicates(involved, path=f"{path}.involved_characters")
        extracted_facts.append(
            FactEntry(
                fact_id=_stable_memory_id("fact", card.chapter_id, index),
                description=fact_delta.description,
                involved_characters=involved,
            )
        )

    scene_anchor = None
    if delta.scene_anchor is not None:
        scene = delta.scene_anchor
        if scene.enter_evidence_source_id:
            evidence(
                scene.enter_evidence_source_id,
                "memory.scene_anchor.enter_evidence_source_id",
            )
        if scene.wake_evidence_source_id:
            evidence(
                scene.wake_evidence_source_id,
                "memory.scene_anchor.wake_evidence_source_id",
            )
        scene_anchor = ExtractedSceneAnchor(
            enter_dream_location=scene.enter_dream_location,
            wake_location=scene.wake_location,
            has_transition_explanation=scene.has_transition_explanation,
        )

    payload = _ExtractorLLMPayload(
        characters=extracted_characters,
        plot_threads=extracted_threads,
        timeline=extracted_timeline,
        facts=extracted_facts,
        character_states=extracted_states,
        scene_anchor=scene_anchor,
    )

    def classify(
        path: str, _leaf: Any
    ) -> tuple[
        Literal["copied_authority", "deterministic_derived", "model_delta"],
        str,
        str | None,
    ]:
        if path.endswith(".evidence_quote"):
            return "deterministic_derived", path, "source_id_exact_lookup"
        match = re.match(r"characters\[(\d+)\]\.(.+)", path)
        if match is not None:
            index = int(match.group(1))
            character_delta = delta.characters[index]
            rest = match.group(2)
            existing = character_delta.existing_character_id is not None
            if rest == "character_id":
                if existing:
                    return (
                        "copied_authority",
                        f"character_store.{character_delta.existing_character_id}.character_id",
                        "exact_copy",
                    )
                return "deterministic_derived", path, "new_slot_to_stable_id"
            canonical_roots = {
                "name",
                "archetype",
                "appearance",
                "personality",
                "background",
                "voice_profile",
            }
            root = rest.split(".", 1)[0].split("[", 1)[0]
            if existing and root in canonical_roots:
                return "copied_authority", f"character_store.{path}", "exact_copy"
            list_roots = {
                "aliases",
                "goals_long_term",
                "goals_short_term",
                "abilities",
                "secrets",
            }
            if root in list_roots:
                additions = {
                    operation.value
                    for operation in character_delta.operations
                    if operation.action == f"{root}_add"
                }
                if rest == root and isinstance(_leaf, list):
                    if existing and not additions:
                        return (
                            "copied_authority",
                            f"character_store.{path}",
                            "exact_copy",
                        )
                    return "deterministic_derived", path, "exact_list_delta_merge"
                if not isinstance(_leaf, (list, dict)) and _leaf in additions:
                    return "model_delta", f"memory_delta.{path}", None
                if existing and not isinstance(_leaf, (list, dict)):
                    return "copied_authority", f"character_store.{path}", "exact_copy"
                return "deterministic_derived", path, "exact_list_delta_merge"
            if root == "current_state":
                key = rest.removeprefix(f"{root}.")
                if any(
                    operation.action == f"{root}_upsert" and operation.reference == key
                    for operation in character_delta.operations
                ):
                    return "model_delta", f"memory_delta.{path}", None
                if existing and not isinstance(_leaf, (list, dict)):
                    return "copied_authority", f"character_store.{path}", "exact_copy"
                return "deterministic_derived", path, "exact_mapping_delta_merge"
            if root == "relationships":
                key = rest.removeprefix("relationships.")
                if any(
                    operation.action == "relationships_upsert"
                    and operation.reference is not None
                    and _resolve_character_reference(
                        operation.reference,
                        existing_ids=existing_ids,
                        new_id_by_ordinal=new_id_by_ordinal,
                        path=f"memory_delta.{path}",
                    )
                    == key
                    for operation in character_delta.operations
                ):
                    return "model_delta", f"memory_delta.{path}", None
                if existing and not isinstance(_leaf, (list, dict)):
                    return "copied_authority", f"character_store.{path}", "exact_copy"
                return "deterministic_derived", path, "exact_mapping_delta_merge"
            return "model_delta", f"memory_delta.{path}", None

        state_match = re.match(r"character_states\[(\d+)\]\.(.+)", path)
        if state_match is not None:
            index = int(state_match.group(1))
            state_delta = delta.character_states[index]
            rest = state_match.group(2)
            if rest == "character_id":
                return (
                    "copied_authority",
                    f"character_store.{state_delta.character}",
                    "exact_copy",
                )
            if rest == "physical_state" and state_delta.physical_state is None:
                return "copied_authority", f"character_store.{path}", "latest_snapshot"
            root = rest.split(".", 1)[0].split("[", 1)[0]
            action_root = (
                "current_relationships" if root == "current_relationships" else root
            )
            if root in {"injuries", "acquired_items"}:
                additions = {
                    operation.value
                    for operation in state_delta.operations
                    if operation.action == f"{action_root}_add"
                }
                if rest == root and isinstance(_leaf, list):
                    if not additions:
                        return (
                            "copied_authority",
                            f"character_store.{path}",
                            "latest_snapshot",
                        )
                    return "deterministic_derived", path, "exact_list_delta_merge"
                if not isinstance(_leaf, (list, dict)) and _leaf in additions:
                    return "model_delta", f"memory_delta.{path}", None
                if not isinstance(_leaf, (list, dict)):
                    return (
                        "copied_authority",
                        f"character_store.{path}",
                        "latest_snapshot",
                    )
                return "deterministic_derived", path, "exact_list_delta_merge"
            if root == "current_relationships":
                key = rest.removeprefix("current_relationships.")
                if any(
                    operation.action == "current_relationships_upsert"
                    and operation.reference is not None
                    and _resolve_character_reference(
                        operation.reference,
                        existing_ids=existing_ids,
                        new_id_by_ordinal=new_id_by_ordinal,
                        path=f"memory_delta.{path}",
                    )
                    == key
                    for operation in state_delta.operations
                ):
                    return "model_delta", f"memory_delta.{path}", None
                if not isinstance(_leaf, (list, dict)):
                    return (
                        "copied_authority",
                        f"character_store.{path}",
                        "latest_snapshot",
                    )
                return "deterministic_derived", path, "exact_mapping_delta_merge"
            return "model_delta", f"memory_delta.{path}", None

        thread_match = re.match(r"plot_threads\[(\d+)\]\.(.+)", path)
        if thread_match is not None:
            index = int(thread_match.group(1))
            thread_delta = delta.plot_threads[index]
            rest = thread_match.group(2)
            existing = thread_delta.existing_thread_id is not None
            if rest == "thread_id":
                if existing:
                    return "copied_authority", f"plot_thread_store.{path}", "exact_copy"
                return "deterministic_derived", path, "new_slot_to_stable_id"
            if existing and rest in {"title", "planned_payoff", "planted_chapter"}:
                return "copied_authority", f"plot_thread_store.{path}", "exact_copy"
            if existing and rest == "importance" and thread_delta.importance is None:
                return "copied_authority", f"plot_thread_store.{path}", "exact_copy"
            if existing and rest == "description":
                return (
                    "deterministic_derived",
                    path,
                    "authority_plus_delta_exact_concat",
                )
            if (
                existing
                and rest == "actual_payoff"
                and not thread_delta.resolved_in_current_chapter
            ):
                return "copied_authority", f"plot_thread_store.{path}", "exact_copy"
            if rest in {"planted_chapter", "actual_payoff"}:
                return "deterministic_derived", path, "chapter_identity_mapping"
            return "model_delta", f"memory_delta.{path}", None

        if re.match(r"timeline\[\d+\]\.entry_id$", path):
            return "deterministic_derived", path, "chapter_ordinal_to_stable_id"
        if re.match(r"facts\[\d+\]\.fact_id$", path):
            return "deterministic_derived", path, "chapter_ordinal_to_stable_id"
        return "model_delta", f"memory_delta.{path}", None

    artifact = build_assembly_artifact(
        stage="memory",
        value=payload,
        authority=authority,
        delta=delta,
        classifier=classify,
    )
    return payload, artifact


class ExtractionResult(BaseModel):
    """``extract()`` 出参。

    汇总四类各落库 / 落 Chroma 后的最终 Pydantic 实体(已经补好系统字段);
    ``facts`` 不入 SQLite 故只透传 LLM 抽取的 ``FactEntry`` 原值。

    ``count_by_type`` 字段便于 caller 写日志 / 触发下一步业务(例如"本
    章抽取了 N 条记忆"通知)。
    """

    chapter_id: str
    workspace_id: str
    characters: list[Character]
    plot_threads: list[PlotThread]
    timeline: list[TimelineEntry]
    facts: list[FactEntry]
    state_snapshots: list[CharacterStateSnapshot] = Field(default_factory=list)
    #: B2(v1.1 wave 6 session 3):本章双时空场景锚点(入梦点 / 醒来点);非双时空
    #: 章为 ``None``。caller(``_extract_memories_safely``)落 ChapterORM.scene_anchor
    #: + 跨章比对醒来点≠入梦点。
    scene_anchor: ExtractedSceneAnchor | None = None
    model_used: str

    @property
    def count_by_type(self) -> dict[str, int]:
        return {
            "character": len(self.characters),
            "plot_thread": len(self.plot_threads),
            "timeline": len(self.timeline),
            "fact": len(self.facts),
            "state_snapshot": len(self.state_snapshots),
        }


def extract(
    content: str,
    card: ChapterCard,
    *,
    workspace_id: str,
    db_session: Session,
    vector_store: VectorStore,
    router: ModelRouter | None = None,
    llm: LLMClient | None = None,
    model: str | None = "claude-sonnet-4-6",
    max_tokens: int = _DEFAULT_MAX_TOKENS,
    temperature: float = _DEFAULT_TEMPERATURE,
    prompts_dir: Path | None = None,
    setting_contract: SettingContract | None = None,
    progression_consistency_enabled: bool = False,
    progression_label: str = "境界",
    on_vector_write_started: Callable[[], None] | None = None,
) -> ExtractionResult:
    """从一章正文抽取增量记忆并落库。

    流程:
    1. 加载 ``prompts/memory_extractor.md`` system 提示词 + 拼装 user
       消息(章节卡上下文 + 正文 + extractor marker)。
    2. 调注入的 ``llm.complete(...)`` 拿到 LLM 响应。
    3. 剥代码块 / trailing 注释 → ``json.loads`` → ``_ExtractorLLMPayload``
       校验。任一失败抛 ``MemoryExtractorError``。
    4. 三类硬记忆按 get-or-update-or-create 写入 SQLite(``flush()`` 不
       ``commit()``)。
    5. 四类(含 facts)按"先 SQLite 后 Chroma"顺序批量调 ``vector_store.add``,
       Chroma id = ``f"{entity_type}-{entity_id}"``,metadata 五字段。
    6. 落 ``memory_extracted`` structlog 事件,返回 ``ExtractionResult``。

    :param content: 章节正文(由 caller 从 ``ChapterVersion.final.md`` 等读出)。
    :param card: 章节卡;Extractor 用 ``card.chapter_id`` /
        ``card.related_characters`` 等。**注意**:卡片字段 ``card.workspace_id``
        在 P3-B-14 起**不再**用于 ``memory_extracted`` 事件 / ``ExtractionResult``
        字段——卡片是模板化资产,可能跨工作区复制(P5 OutlineBuilder / P7 模板
        复用预期场景),其 ``workspace_id`` 字段会与"操作工作区"漂移;改由
        ``workspace_id`` 入参显式注入操作工作区上下文(对齐其他 agent / event
        读 chapter_service 透传的语义)。
    :param workspace_id: P3-B-14 加。当前操作工作区 ID,作为 ``memory_extracted``
        事件字段 + ``ExtractionResult.workspace_id`` 字段的真理源。``ChapterService``
        透传 → ``orchestrate_chapter`` → ``_extract_memories_safely`` → 本函数。
        与 ``card.workspace_id`` 解耦,卡片 JSON 字段漂移时事件仍正确(P3-acceptance
        观察 1 字面修复 — fixture/p2e2e/cards/*.json 的 ``workspace_id: p2e2e-demo``
        在 p3e2e-demo 工作区下复用时不再串号)。
    :param db_session: SQLAlchemy ``Session``,事务边界归 caller。
    :param vector_store: 注入的 ``VectorStore``(已绑定 workspace_id +
        embedding_fn + persist_dir)。
    :param router: P3-B-06 主路径。``ModelRouter`` 实例;给定时按
        ``router.route("memory_extractor")`` 取 client + model + 生成参数。
    :param llm: legacy 路径 LLM 客户端;``router=None`` 时必传。
    :param model: legacy 路径模型名;``router=None`` 时必传。实际
        ``model_used`` 取响应里的字段。
    :param max_tokens: legacy 路径输出 token 上限,默认 4000;``router`` 路径
        下由 profile 决定。
    :param temperature: legacy 路径采样温度,默认 0.2;``router`` 路径下由
        profile 决定。
    :param prompts_dir: 提示词模板根目录;``None`` 时取 ``settings.prompts_dir``。
    :raises MemoryExtractorError: LLM 输出非合法 JSON / 缺字段 / 字段类型错。
    :raises TypeError: ``router=None`` 且 ``llm`` / ``model`` 任一缺失。
    :returns: ``ExtractionResult``——四类已落库 / 落 Chroma 后的实体。
    """
    resolved_client, resolved_model, complete_kwargs = resolve_routing(
        agent_name="memory_extractor",
        router=router,
        client=llm,
        model=model,
        legacy_kwargs={"max_tokens": max_tokens, "temperature": temperature},
    )
    use_creative_delta = uses_hy3_creative_delta(resolved_client)
    existing_characters_snapshot: list[Character] = []
    existing_plot_threads_snapshot: list[PlotThread] = []
    authority_payload: dict[str, Any] = {}
    frozen_authority: FrozenAuthority | None = None
    response_authority: MemoryResponseAuthorityContract | None = None
    source_catalog: tuple[EvidenceSource, ...] = ()
    if use_creative_delta:
        existing_characters_snapshot = list_characters_by_workspace(
            db_session, workspace_id
        )
        existing_plot_threads_snapshot = list_plot_threads_by_workspace(
            db_session, workspace_id
        )
        authority_payload = _memory_authority_payload(
            content=content,
            card=card,
            workspace_id=workspace_id,
            characters=existing_characters_snapshot,
            plot_threads=existing_plot_threads_snapshot,
        )
        frozen_authority = FrozenAuthority.freeze(
            stage="memory",
            payload=authority_payload,
        )
        source_catalog = build_evidence_source_catalog(content)
        response_authority = build_memory_response_authority_contract(
            characters=existing_characters_snapshot,
            plot_threads=existing_plot_threads_snapshot,
            source_catalog=source_catalog,
        )
    wire_model = (
        _MemoryCreativeDeltaPayload if use_creative_delta else _ExtractorLLMPayload
    )
    extractor_complete_kwargs = prepare_strict_json_output(
        client=resolved_client,
        model=resolved_model,
        output_model=wire_model,
        schema_name="memory_extractor",
        complete_kwargs=complete_kwargs,
        transform=(
            response_authority.transform_hy3_wire_schema
            if response_authority is not None
            else None
        ),
    )

    template_root = (
        prompts_dir if prompts_dir is not None else settings.prompts_dir
    ).resolve()
    prompt_filename = (
        _DELTA_EXTRACTOR_PROMPT_FILENAME
        if use_creative_delta
        else _EXTRACTOR_PROMPT_FILENAME
    )
    system_text = load_prompt_template(template_root / prompt_filename)
    # G1(v1.1 wave 4 session 1):注入出场角色截止上一章的累积状态,让 LLM 抽
    # character_states 时基于历史状态折叠(防"断臂复活")。
    # 模块②(v1.1 w5-s10):gated 时 prior_state_block 额外含"当前境界"(锚定增量),
    # 且 user message 追加进阶等级抽取指令。OFF 时两者均不变 → 逐字节零回归。
    prior_state_block = _render_prior_state_block(
        session=db_session,
        card=card,
        progression_enabled=progression_consistency_enabled,
        progression_label=progression_label,
    )
    # 闭环自校(模块②):persist 前捕获各出场角色 prior 进阶等级,persist 后与抽取值
    # 比对(注入↔写出),仅 gated 时执行。
    prior_ranks = (
        _capture_prior_progression_ranks(session=db_session, card=card)
        if progression_consistency_enabled
        else {}
    )
    related_plot_thread_block = _render_related_plot_thread_block(
        session=db_session,
        card=card,
        workspace_id=workspace_id,
    )
    user_text = _build_user_message(
        card=card,
        content=content,
        workspace_id=workspace_id,
        prior_state_block=prior_state_block,
        related_plot_thread_block=related_plot_thread_block,
        setting_contract=setting_contract,
        progression_enabled=progression_consistency_enabled,
        progression_label=progression_label,
        evidence_source_catalog=(
            render_evidence_source_catalog(text=content, catalog=source_catalog)
            if use_creative_delta
            else None
        ),
        delta_authority=(
            {
                "authority_hash": frozen_authority.source_hash,
                "characters": authority_payload["characters"],
                "plot_threads": authority_payload["plot_threads"],
            }
            if use_creative_delta and frozen_authority is not None
            else None
        ),
    )

    response = resolved_client.complete(
        messages=[
            Message(role="system", content=system_text),
            Message(role="user", content=user_text),
        ],
        model=resolved_model,
        **extractor_complete_kwargs,
    )

    if use_creative_delta:
        assert frozen_authority is not None
        assert response_authority is not None
        try:
            raw_delta = json.loads(response.content)
            delta = _MemoryCreativeDeltaPayload.model_validate(raw_delta)
            response_authority.validate_delta(delta)
            current_characters = list_characters_by_workspace(db_session, workspace_id)
            current_plot_threads = list_plot_threads_by_workspace(
                db_session, workspace_id
            )
            current_authority = _memory_authority_payload(
                content=content,
                card=card,
                workspace_id=workspace_id,
                characters=current_characters,
                plot_threads=current_plot_threads,
            )
            payload, assembly_artifact = _assemble_memory_delta(
                delta=delta,
                content=content,
                card=card,
                workspace_id=workspace_id,
                existing_characters=existing_characters_snapshot,
                existing_plot_threads=existing_plot_threads_snapshot,
                source_catalog=source_catalog,
                authority=frozen_authority,
                current_authority=current_authority,
            )
        except (
            json.JSONDecodeError,
            ValidationError,
            CreativeDeltaAssemblyError,
        ) as exc:
            _log.error(
                "memory_extractor_delta_failed",
                chapter_id=card.chapter_id,
                model_used=response.model_used,
                error_type=type(exc).__name__,
                error=str(exc),
            )
            raise MemoryExtractorError(
                "MemoryExtractor creative delta 严格解析或确定性组装失败",
                raw_output=response.content,
                model_used=response.model_used,
            ) from exc
        _log.info(
            "memory_extractor_delta_assembled",
            chapter_id=card.chapter_id,
            authority_hash=assembly_artifact.authority_hash,
            delta_hash=assembly_artifact.delta_hash,
            full_leaf_count=assembly_artifact.full_leaf_count,
            model_delta_leaf_count=assembly_artifact.model_delta_leaf_count,
        )
    else:
        payload = _parse_llm_payload(
            raw_text=response.content,
            model_used=response.model_used,
            chapter_id=card.chapter_id,
        )

    # 1) SQLite 三类硬记忆:get-or-update-or-create。flush 不 commit。
    #
    # === Y1 缺陷 C 治本(v1.1 w4 B1,2026-06-03):extract 写事务不跨 LLM ===
    # 原实现"merge LLM 与 persist 交错"(disambiguation→写 character→plot_merge
    # →写 thread→timeline_merge→写 timeline):首次 _persist 即让本 session 获 WAL
    # 写锁,之后 plot_thread_merger / timeline_entry_merger 的 LLM 调用(thinking
    # 各 ~20s)在持写锁状态下进行 → 写锁被占 40s+ → 并发的 cost 写(独立短 session)
    # busy_timeout 等满仍反复抢不到锁(B1 长跑 ch-15~18 逐章 cost_tracker_recorder
    # _failed,retry 全失败)。详见 ``cost_tracker`` 模块注释 + digest Y1 缺陷 C。
    # 治本 = 两段式:Phase 1 先把全部 merge LLM 跑完(只读 existing + in_place 改
    # payload,不写 SQLite),Phase 2 再集中 persist(纯 SQLite 写,无 LLM)→ 写锁
    # 持有从 ~40s 降到几十 ms。三个 merge 各读不同表(character / plot_thread /
    # timeline),互不依赖对方的 persist 写,可安全前置。

    # --- Phase 1:全部语义合并 LLM 兜底(只读 existing 对比 + in_place 改 payload)---

    # A3 续(v1.1 wave 2 session 5,2026-05-29):character 语义消歧 LLM 兜底。
    # 在 _persist_characters 前调 character_disambiguator LLM-as-judge 判定本章
    # extracted 与工作区 existing 是否同一人不同表述(描述性指代 vs 真名披露 /
    # 别名等)→ 命中改 character_id 让 caller 走既有 update 路径。P7-B-05 规则版
    # 0.85 相似度规则作第二道防御,LLM 漏抓的"林衍 / 林衍-2"类字面漂移场景规则
    # 版仍能兜底。
    _apply_character_disambiguation_in_place(
        extracted=payload.characters,
        content=content,
        session=db_session,
        workspace_id=workspace_id,
        chapter_id=card.chapter_id,
        router=router,
        llm=llm,
        model=model,
        max_tokens=max_tokens,
        temperature=temperature,
        prompts_dir=prompts_dir,
    )

    # R9 临时伏笔高门槛必须放在语义 merger **之前**。否则一条只有气氛、道具
    # 或普通未完动作的低质量新候选，可能先被 merger 映射到大纲主伏笔并覆盖其
    # 状态/描述，等于绕过准入。精确命中既有 ID 的更新不参加门槛，保持大纲
    # 伏笔原推进链不变。
    payload.plot_threads = _filter_new_emergent_plot_threads_before_merge(
        extracted=payload.plot_threads,
        content=content,
        session=db_session,
        workspace_id=workspace_id,
        chapter_id=card.chapter_id,
        known_upstream_thread_ids=set(card.related_plot_threads),
    )

    # A3 治本(v1.1 wave 2 session 5):plot_thread 语义合并 LLM 兜底。
    _apply_plot_thread_merge_in_place(
        extracted=payload.plot_threads,
        session=db_session,
        workspace_id=workspace_id,
        chapter_id=card.chapter_id,
        router=router,
        llm=llm,
        model=model,
        max_tokens=max_tokens,
        temperature=temperature,
        prompts_dir=prompts_dir,
    )

    # merger 后真正仍为新 ID 的候选每章最多落一条；被合并到既有大纲/人工伏笔
    # 的更新不计入临时配额，也不会被截断。
    payload.plot_threads = _cap_new_emergent_plot_threads_after_merge(
        extracted=payload.plot_threads,
        session=db_session,
        workspace_id=workspace_id,
        chapter_id=card.chapter_id,
        known_upstream_thread_ids=set(card.related_plot_threads),
    )

    # A3 续(v1.1 wave 2 session 5,2026-05-29):timeline_entry 语义合并 LLM 兜底。
    # 预防性兜底 — 治 wave 4 30 章长跑跨章节回忆 / 倒叙场景同事件重复登记。
    _apply_timeline_entry_merge_in_place(
        extracted=payload.timeline,
        session=db_session,
        workspace_id=workspace_id,
        chapter_id=card.chapter_id,
        router=router,
        llm=llm,
        model=model,
        max_tokens=max_tokens,
        temperature=temperature,
        prompts_dir=prompts_dir,
    )

    # --- Phase 1→2 隔断:结束 Phase 1 只读事务,丢弃其读快照 ---
    # Phase 1 仅含读(_render_prior_state_block + 三个 merge 的 existing 对比)+
    # in_place 改 payload,无任何 SQLite 写,rollback 无数据损失。**为何必需**:
    # Phase 1 的 merge LLM 调用期间,并发 cost 写(每个 merger LLM 返回即触发)会
    # 改 chapters 表 → 本 session 的读快照过期;若不隔断,Phase 2 首次写会从过期
    # 读快照"升级写"撞 SQLITE_BUSY_SNAPSHOT(把锁冲突从 cost 转嫁给 extract 自己)。
    # rollback 后 Phase 2 取最新快照开干净写事务,规避升级冲突。
    db_session.rollback()

    # --- Phase 2:全部硬记忆 persist(纯 SQLite 写,无 LLM;flush 不 commit)---

    saved_characters = _persist_characters(
        db_session, payload.characters, card=card, workspace_id=workspace_id
    )

    # G1(v1.1 wave 4 session 1):character_states 折叠落 state_timeline。
    # 放在 _persist_characters 之后 — 本章新登场角色已落库,append snapshot 不漏。
    # 模块②(w5-s10):gated 时 progression_rank 结转(LLM 没明写变化则沿用上一章)。
    saved_state_snapshots = _persist_character_states(
        db_session,
        payload.character_states,
        card=card,
        progression_enabled=progression_consistency_enabled,
        prior_ranks=prior_ranks if progression_consistency_enabled else None,
    )

    # 模块②(w5-s10)闭环自校:注入↔写出比对 + 候选章交代判定 → 结构化 WARN。
    # 仅 gated 时执行;纯观测(WARN 安全网,不阻断主流程)。
    if progression_consistency_enabled:
        _check_progression_consistency(
            extracted=payload.character_states,
            prior_ranks=prior_ranks,
            content=content,
            card=card,
            workspace_id=workspace_id,
            client=resolved_client,
            model=resolved_model,
            complete_kwargs=complete_kwargs,
            label=progression_label,
            template_root=template_root,
        )

    saved_threads = _persist_plot_threads(
        db_session, payload.plot_threads, card=card, workspace_id=workspace_id
    )

    saved_timeline = _persist_timeline(
        db_session, payload.timeline, card=card, workspace_id=workspace_id
    )

    # Phase 3:Chroma 四类合并写。先 SQLite 后 Chroma 顺序保证 caller rollback 干净。
    _persist_to_vector_store(
        vector_store=vector_store,
        characters=saved_characters,
        plot_threads=saved_threads,
        timeline=saved_timeline,
        facts=payload.facts,
        card=card,
        workspace_id=workspace_id,
        on_vector_write_started=on_vector_write_started,
    )

    _log.info(
        "memory_extracted",
        chapter_id=card.chapter_id,
        workspace_id=workspace_id,
        count_by_type={
            "character": len(saved_characters),
            "plot_thread": len(saved_threads),
            "timeline": len(saved_timeline),
            "fact": len(payload.facts),
            "state_snapshot": len(saved_state_snapshots),
        },
        model_used=response.model_used,
    )

    return ExtractionResult(
        chapter_id=card.chapter_id,
        workspace_id=workspace_id,
        characters=saved_characters,
        plot_threads=saved_threads,
        timeline=saved_timeline,
        facts=list(payload.facts),
        state_snapshots=saved_state_snapshots,
        # B2(v1.1 wave 6 session 3):透传本章场景锚点(可能 None);caller 落
        # ChapterORM.scene_anchor + 跨章比对。extract 本身不持久化(纯返回),
        # 持久化 / 比对 / LLM 复核在 extract 写事务之外(护 Y1-C 不变量)。
        scene_anchor=payload.scene_anchor,
        model_used=response.model_used,
    )


def _build_progression_extract_instruction(label: str) -> str:
    """模块②(w5-s10)gated 进阶等级抽取指令(锚定增量 + 只认明写)。

    作为 user message 的**条件块**追加(非改静态 prompt 文件)→ OFF 时 prompt
    逐字节不变 = 零回归。指令让 LLM 在 ``character_states`` 各元素**额外**输出
    ``progression_rank`` 字段:仅当本章正文**明写**该角色{label}发生突破/晋级/
    倒退时给新值,否则 ``null``(系统结转上一章,不脑补)。
    """
    return (
        f"\n\n## 额外抽取:角色进阶{label}(模块②,仅本工作区启用)\n\n"
        f"除上述四类,请在 `character_states` 每个出场角色元素里**额外**输出一个 "
        f"`progression_rank` 字段(字符串),记其**截止本章的当前{label}**"
        f"(如仙侠'炼气六层巅峰'/'金丹',战斗'战力8200'/'黄金段位'):\n"
        f"- **锚定上一章**:参考上方'角色当前状态'段里的'当前{label}'作起点。\n"
        f"- **只认明写**:仅当本章正文**明确写了**该角色{label}变化(突破/晋级 升,"
        f"或突破失败/被打落/被废/封印/重伤 等**降**)才给新值;\n"
        f"- **没明写就给 `null`**(不要凭气氛脑补;系统会自动沿用上一章{label})。\n"
        f"- 方向无关:升、降都如实记(降也要记,后续由系统判这次降有没有剧情交代)。\n"
    )


def _build_user_message(
    *,
    card: ChapterCard,
    content: str,
    workspace_id: str,
    prior_state_block: str = "",
    related_plot_thread_block: str = "",
    setting_contract: SettingContract | None = None,
    progression_enabled: bool = False,
    progression_label: str = "境界",
    evidence_source_catalog: str | None = None,
    delta_authority: dict[str, Any] | None = None,
) -> str:
    """user 消息:权威设定 + 章节卡核心字段 + (G1)角色当前状态 + 正文。

    包含 ``chapter_id`` / ``workspace_id`` / ``chapter_number`` / 视角 /
    场景 / 出场角色,让 LLM 抽取时知道"哪些角色已是出场角色"——避免把已
    存在角色再当新角色抽。

    ``prior_state_block``(G1,v1.1 wave 4 session 1):出场角色截止上一章的
    累积状态快照(由 ``extract()`` 从各角色 ``state_timeline`` 渲染)。让 LLM
    抽 ``character_states`` 时基于"上一章状态 + 本章正文"折叠出截止本章的累积
    状态(断臂的角色不会因为本章没提到就"痊愈")。空串时省略该段。

    末尾追加 P1-B-22 协议的 extractor marker(MockLLMClient 当前抛
    ``NotImplementedError``,P2-B-09 单测用独立 fake LLM client)。

    P3-B-14:``workspace_id`` 显式注入,**不再**读 ``card.workspace_id``——
    卡片是模板化资产,工作区字段可能与"操作工作区"漂移。

    R9 v5:``character_states`` 是正文抽取出的派生记忆，不得覆盖权威
    ``world.unique_rules``。把同一 ``SettingContract`` 注入既有 extractor
    调用，不新增 Agent 或 Provider 请求；若旧快照与契约冲突，抽取结果必须按
    契约纠正冲突字段，而不是把错误继续累积到下一章。
    """
    related_characters_block = (
        ", ".join(card.related_characters)
        if card.related_characters
        else "(无,本章可能新登场角色)"
    )
    related_threads_block = (
        ", ".join(card.related_plot_threads) if card.related_plot_threads else "(无)"
    )
    context_block = (
        "## 章节上下文\n\n"
        f"- 章节 ID(chapter_id):{card.chapter_id}\n"
        f"- 工作区 ID(workspace_id):{workspace_id}\n"
        f"- 卷内章序号(chapter_number):{card.chapter_number}\n"
        f"- 标题:{card.title}\n"
        f"- 视角角色(pov_character):{card.pov_character}\n"
        f"- 场景设置(setting):{card.setting}\n"
        f"- 出场角色(related_characters):{related_characters_block}\n"
        f"- 关联伏笔(related_plot_threads):{related_threads_block}\n"
    )
    if evidence_source_catalog is None:
        text_block = "## 待抽取正文\n\n" + content
    else:
        text_block = (
            "## 待抽取正文证据目录\n\n"
            "每行是程序冻结的 source_id 与逐字正文片段。语义增量只能引用这些 "
            "source_id；不得复述证据作为身份。\n\n" + evidence_source_catalog
        )
    if delta_authority is not None:
        authority_json = json.dumps(
            delta_authority,
            ensure_ascii=False,
            sort_keys=True,
            separators=(",", ":"),
        )
        context_block = (
            context_block + "\n## 程序冻结的当前权威对象\n\n" + authority_json + "\n"
        )
    optional_context = "\n\n".join(
        block for block in (prior_state_block, related_plot_thread_block) if block
    )
    body = (
        f"{context_block}\n\n{optional_context}\n\n{text_block}"
        if optional_context
        else f"{context_block}\n\n{text_block}"
    )
    if setting_contract is not None and not setting_contract.is_empty:
        authority_block = (
            setting_contract.render_for_prompt()
            + "\n\n## 富记忆权威优先级\n\n"
            + "`character_states` 是从已批准正文与历史快照抽出的派生记忆，"
            + "**不得覆盖上方权威世界契约**。若历史状态、章节卡措辞或正文比喻"
            + "把规则的范围、时长、对象或效果扩大，与 `world.unique_rules` 冲突，"
            + "必须丢弃该冲突派生事实并按权威规则输出；只记录角色本章末实际仍处于"
            + "何种状态，不得把‘本章尚未恢复’擅自升级为‘永久／不可逆’。\n"
        )
        body = f"{authority_block}\n\n{body}"
    # 模块②(w5-s10):gated 时追加进阶等级抽取指令(条件块,OFF 时 body 逐字节不变)。
    if progression_enabled:
        body = body + _build_progression_extract_instruction(progression_label)
    return append_role_marker(body, role="extractor")


def _render_related_plot_thread_block(
    *,
    session: Session,
    card: ChapterCard,
    workspace_id: str,
) -> str:
    """把本章卡已关联伏笔的稳定 ID 与当前状态明确给同一次 Extractor。

    这是现有调用内的 SQL 精确读取，不增加 Agent/Provider 请求。目的不是把所有
    伏笔塞给模型，而是让本章确实推进/收口的伏笔使用原 ID 更新；尤其临时伏笔
    收口后不能另造一个 ``resolved`` ID，导致原条目永远悬空。
    """
    blocks: list[str] = []
    for thread_id in dict.fromkeys(card.related_plot_threads):
        row = session.get(PlotThreadORM, thread_id)
        if row is None or row.workspace_id != workspace_id:
            continue
        origin = getattr(row, "origin", "outline")
        blocks.append(
            f"- {row.thread_id} | {row.title} | status={row.status} | "
            f"origin={origin}\n  desc:{row.description}"
        )
    if not blocks:
        return ""
    return (
        "## 本章已关联伏笔的稳定身份（状态推进/收口必须复用原 thread_id）\n\n"
        + "\n".join(blocks)
    )


def _render_prior_state_block(
    *,
    session: Session,
    card: ChapterCard,
    progression_enabled: bool = False,
    progression_label: str = "境界",
) -> str:
    """G1:渲染出场角色截止上一章的累积状态,供 LLM 抽 ``character_states`` 时折叠。

    遍历 ``pov_character`` + ``related_characters``,各取 ``state_timeline`` 中
    ``as_of_chapter < 本章全局序`` 的最新一条快照渲染。无任何历史状态时返回空串
    (``_build_user_message`` 据此省略该段;首章 / 角色首次出场场景)。

    模块②(w5-s10):``progression_enabled`` 时,额外渲染该角色"当前{label}"
    (锚定增量——让 LLM 在已知起点上判本章有无突破/倒退)。OFF 时不渲染 → 与 G1
    逐字节一致(零回归);**不动** ``format_state_snapshot_lines``(保 G1 共享渲染)。
    """
    current_seq = chapter_seq_from_id(card.chapter_id)
    if current_seq is None:
        return ""

    char_ids: list[str] = []
    if card.pov_character:
        char_ids.append(card.pov_character)
    for cid in card.related_characters:
        if cid not in char_ids:
            char_ids.append(cid)

    rendered: list[str] = []
    for cid in char_ids:
        character = get_character(session, cid)
        if character is None:
            continue
        snapshot = latest_state_snapshot_before(character.state_timeline, current_seq)
        if snapshot is None:
            continue
        lines = format_state_snapshot_lines(snapshot)
        if progression_enabled and snapshot.progression_rank:
            lines.append(f"当前{progression_label}:{snapshot.progression_rank}")
        if not lines:
            continue
        body = "\n".join(f"  - {line}" for line in lines)
        rendered.append(f"- {character.name}({cid}):\n{body}")

    if not rendered:
        return ""
    header = (
        "## 角色当前状态(截止上一章,CHARACTER STATE AS OF PREVIOUS CHAPTER)\n\n"
        "以下是出场角色截止上一章的累积状态。抽 `character_states` 时,**在此基础上**"
        "叠加本章正文带来的变化,输出**截止本章的累积状态**——上一章已断的臂不会"
        "因为本章没提到就痊愈,已获得的物品不会凭空消失。\n\n"
    )
    return header + "\n".join(rendered)


def _persist_character_states(
    session: Session,
    extracted: list[ExtractedCharacterState],
    *,
    card: ChapterCard,
    progression_enabled: bool = False,
    prior_ranks: dict[str, str | None] | None = None,
) -> list[CharacterStateSnapshot]:
    """G1:把 LLM 抽取的 ``character_states`` 折叠落各角色 ``state_timeline``。

    每条 → 一条 ``CharacterStateSnapshot``(``as_of_chapter`` = 本章全局序),
    append 到该角色 ``state_timeline``;同 ``as_of_chapter`` 已存在则**替换**
    (rewind / 章节回写 / 重抽幂等)。角色不存在(``character_states`` 引用了
    未落库角色)则跳过该条并 warn log,不阻断主流程。

    :return: 本次实际落库的 snapshot 列表(供 ``ExtractionResult`` 观测)。
    """
    seq = chapter_seq_from_id(card.chapter_id)
    if seq is None:
        if extracted:
            _log.warning(
                "memory_extractor_state_skip_bad_chapter_id",
                chapter_id=card.chapter_id,
                count=len(extracted),
            )
        return []

    saved: list[CharacterStateSnapshot] = []
    for item in extracted:
        character = get_character(session, item.character_id)
        if character is None:
            _log.warning(
                "memory_extractor_state_skip_unknown_character",
                chapter_id=card.chapter_id,
                character_id=item.character_id,
            )
            continue
        # 模块②(w5-s10)进阶等级结转:LLM 明写变化(progression_rank 非空)则用新值;
        # 否则沿用上一章最新快照的 progression_rank(锚定增量,不丢/不脑补)。OFF 时
        # progression_enabled=False → 恒 None → 与 G1 逐字节一致(零回归)。
        progression_rank: str | None = None
        if progression_enabled:
            extracted_rank = (item.progression_rank or "").strip()
            if extracted_rank:
                progression_rank = extracted_rank
            elif prior_ranks is not None and item.character_id in prior_ranks:
                # 结转用 extract() persist 前捕获的 prior(robust:_persist_characters
                # 的 update 可能重置 state_timeline,故不在此处重新 lookup)。
                progression_rank = prior_ranks.get(item.character_id)
            else:
                prior = latest_state_snapshot_before(character.state_timeline, seq)
                progression_rank = prior.progression_rank if prior else None
        existing_same_chapter = next(
            (state for state in character.state_timeline if state.as_of_chapter == seq),
            None,
        )
        protected = (
            existing_same_chapter
            if existing_same_chapter is not None
            and (
                existing_same_chapter.authority == "approved_text"
                or bool(existing_same_chapter.core_facts)
            )
            else None
        )
        if protected is None:
            # 核心事实是跨章持续状态。即便本章没有新的核心候选，同一次既有
            # rich-memory 落新快照时也必须从此前最近的核心快照结转；否则
            # CoreStateService 读取“最新快照”会让死亡/断肢在下一章凭空消失。
            protected = max(
                (
                    state
                    for state in character.state_timeline
                    if state.as_of_chapter < seq and state.core_facts
                ),
                key=lambda state: state.as_of_chapter,
                default=None,
            )
        snapshot = CharacterStateSnapshot(
            as_of_chapter=seq,
            physical_state=item.physical_state,
            injuries=list(item.injuries),
            acquired_items=list(item.acquired_items),
            current_relationships=dict(item.current_relationships),
            state_changes=list(item.state_changes),
            progression_rank=progression_rank,
            authority=(protected.authority if protected else "rich_memory"),
            source_chapter_id=(protected.source_chapter_id if protected else None),
            source_version_id=(protected.source_version_id if protected else None),
            source_text_hash=(protected.source_text_hash if protected else ""),
            evidence_quotes=(list(protected.evidence_quotes) if protected else []),
            core_facts=(list(protected.core_facts) if protected else []),
        )
        timeline = [s for s in character.state_timeline if s.as_of_chapter != seq]
        timeline.append(snapshot)
        timeline.sort(key=lambda s: s.as_of_chapter)
        updated = character.model_copy(update={"state_timeline": timeline})
        update_character(session, updated)
        saved.append(snapshot)
    return saved


def _capture_prior_progression_ranks(
    *, session: Session, card: ChapterCard
) -> dict[str, str | None]:
    """模块②(w5-s10)闭环自校:persist 前捕获各出场角色"截止上一章"进阶等级。

    = 写本章时**注入给 writer 的那个等级**(latest snapshot before 本章全局序)。
    persist 后与抽取器从正文读出的等级比对(注入↔写出)。返回 cid → prior rank。
    """
    current_seq = chapter_seq_from_id(card.chapter_id)
    if current_seq is None:
        return {}
    char_ids: list[str] = []
    if card.pov_character:
        char_ids.append(card.pov_character)
    for cid in card.related_characters:
        if cid not in char_ids:
            char_ids.append(cid)
    out: dict[str, str | None] = {}
    for cid in char_ids:
        character = get_character(session, cid)
        if character is None:
            continue
        snap = latest_state_snapshot_before(character.state_timeline, current_seq)
        out[cid] = snap.progression_rank if snap else None
    return out


#: 模块②(w5-s10)stage-2 交代判定 judge 提示词(inline:聚焦单判断、自决不另起
#: 文件;非 gating verdict 故不进 LLM_AS_JUDGE_AGENTS,WARN 安全网用 extractor client)。
_PROGRESSION_JUDGE_PROMPT = (
    "你是修真/玄幻小说的设定一致性审校。给你一个角色本章发生的进阶等级变化,以及"
    "本章正文。判断:**本章正文有没有对这次等级变化给出因果交代?**\n\n"
    "判定原则(务必遵守):\n"
    "- **交代种类完全开放**:突破升级、突破失败倒退、被敌人打落境界、被废修为、封印、"
    "重伤、自爆反噬、夺舍、中毒、秘境规则压制……**任意一种剧情理由都算有交代 → 合法**。\n"
    "- **越阶击败/越级战是本类型的爽点,不是问题**:主角以低境界击败高境界敌人,只要"
    "本章有方法/代价/天赋/宝物的交代,就算合法。\n"
    "- 只有**完全凭空、前后无任何因果交代**地变(尤其无故倒退、数值自相矛盾)才算"
    "**未交代**。拿不准时一律算**已交代**(宁可漏报不误伤)。\n\n"
    '只输出一行 JSON,无其他文字:{"justified": true/false, "reason": "≤30字"}'
)


def _check_progression_consistency(
    *,
    extracted: list[ExtractedCharacterState],
    prior_ranks: dict[str, str | None],
    content: str,
    card: ChapterCard,
    workspace_id: str,
    client: LLMClient,
    model: str,
    complete_kwargs: dict[str, Any],
    label: str,
    template_root: Path,
) -> None:
    """模块②(w5-s10)闭环自校 + 两段校验(WARN 安全网,不阻断主流程)。

    第 1 段(确定性、零 LLM):对每个出场角色,抽取器从正文读出的等级(``item.
    progression_rank``,明写才非空)若与 prior(注入给 writer 的等级)**不同** →
    候选异常(注入↔写出对不上)。相同 / 抽取器没明写(None)→ 跳过,0 介入。

    第 2 段(LLM judge,仅候选章):判本章对这次变化**有没有因果交代**(种类开放,
    越阶/越级战不算问题)。有交代 → info log(合法,含升级);凭空无交代 → WARN
    (疑似写串/莫名倒退,surface 供修)。**fail-open**:judge 出错/不可解析 → debug
    log 不 WARN(WARN 安全网不制造假警报)。
    """
    for item in extracted:
        ext_rank = (item.progression_rank or "").strip()
        if not ext_rank:
            continue  # 抽取器没明写变化 → 结转,无注入↔写出偏差
        prior = (prior_ranks.get(item.character_id) or "").strip()
        if ext_rank == prior:
            continue  # 注入 == 写出 → 一致,0 介入(最常见)
        # 候选:注入(prior)↔写出(ext_rank)不一致 → stage-2 判交代
        justified = True
        reason = ""
        try:
            user = (
                f"角色 ID:{item.character_id}\n"
                f"上一章{label}(注入给写手的起点):{prior or '(本章首次出现/无)'}\n"
                f"本章正文读出的{label}:{ext_rank}\n\n"
                f"## 本章正文\n\n{content}"
            )
            resp = client.complete(
                messages=[
                    Message(role="system", content=_PROGRESSION_JUDGE_PROMPT),
                    Message(
                        role="user", content=append_role_marker(user, role="extractor")
                    ),
                ],
                model=model,
                **complete_kwargs,
            )
            verdict = _safe_parse_justified(resp.content)
            if verdict is not None:
                justified, reason = verdict
        except Exception as exc:  # noqa: BLE001 — fail-open:judge 错不制造假警报
            _log.debug(
                "progression_consistency_judge_failed",
                chapter_id=card.chapter_id,
                character_id=item.character_id,
                error=str(exc),
            )
            continue
        if justified:
            _log.info(
                "progression_change_justified",
                chapter_id=card.chapter_id,
                workspace_id=workspace_id,
                character_id=item.character_id,
                prior_rank=prior or None,
                new_rank=ext_rank,
                reason=reason,
            )
        else:
            _log.warning(
                "progression_consistency_warn",
                chapter_id=card.chapter_id,
                workspace_id=workspace_id,
                character_id=item.character_id,
                prior_rank=prior or None,
                new_rank=ext_rank,
                reason=reason,
                hint="进阶等级凭空变化(无因果交代),疑似写串/莫名倒退,请人工核查",
            )


def _safe_parse_justified(raw: str) -> tuple[bool, str] | None:
    """从 judge 响应宽松解析 ``{"justified": bool, "reason": str}``;失败返回 None。"""
    import re as _re

    m = _re.search(r"\{.*\}", raw, _re.DOTALL)
    if not m:
        return None
    try:
        obj = json.loads(m.group(0))
    except (ValueError, TypeError):
        return None
    if not isinstance(obj, dict) or "justified" not in obj:
        return None
    return bool(obj["justified"]), str(obj.get("reason", ""))[:60]


def _parse_llm_payload(
    *,
    raw_text: str,
    model_used: str,
    chapter_id: str,
) -> _ExtractorLLMPayload:
    """剥代码块 + trailing → ``json.loads`` → Pydantic 校验。任一失败抛
    ``MemoryExtractorError`` 并落 ``memory_extractor_parse_failed`` 事件。
    """
    text = _strip_format_noise(raw_text).strip()

    try:
        data = loads_with_quote_repair(
            text,
            agent_name="memory_extractor",
            model_used=model_used,
        )
    except json.JSONDecodeError as exc:
        _log.error(
            "memory_extractor_parse_failed",
            reason="invalid_json",
            raw_output=raw_text,
            model_used=model_used,
            chapter_id=chapter_id,
            json_error=str(exc),
        )
        raise MemoryExtractorError(
            "MemoryExtractor LLM 输出非合法 JSON",
            raw_output=raw_text,
            model_used=model_used,
        ) from exc

    try:
        return _ExtractorLLMPayload.model_validate(data)
    except ValidationError as exc:
        _log.error(
            "memory_extractor_parse_failed",
            reason="schema_mismatch",
            raw_output=raw_text,
            model_used=model_used,
            chapter_id=chapter_id,
            validation_errors=exc.errors(),
        )
        raise MemoryExtractorError(
            "MemoryExtractor LLM 输出 JSON 结构与 schema 不符",
            raw_output=raw_text,
            model_used=model_used,
        ) from exc


def _strip_format_noise(text: str) -> str:
    """剥 trailing HTML 注释 + ``` / ```json 包裹(沿用 P1-B-11 reviewer 路径)。

    顺序与 ``reviewer._strip_code_fence`` 同源:**先剥 trailing**(可能多
    个),**再剥 fence**——若先剥 fence,fence 正则要求整字符串末尾为
    ``` ,trailing 注释会让 fence 不匹配。
    """
    while True:
        stripped = _TRAILING_HTML_COMMENT_RE.sub("", text)
        if stripped == text:
            break
        text = stripped
    match = _CODE_FENCE_RE.match(text)
    if match is not None:
        return match.group("body")
    return text


# ---- SQLite 写入(get-or-update-or-create)-----------------------------------


def _find_similar_character(
    session: Session, workspace_id: str, target_name: str
) -> Character | None:
    """P7-B-05:扫工作区角色,返第一个 name 相似度 ≥ ``_NAME_SIMILARITY_THRESHOLD``
    的角色;无命中返 ``None``。

    用于 LLM 抽取已存在角色但 ``character_id`` 漂移的去重(典型场景:
    抽 "林衍" 时 LLM 给了 ``character_id="lin-yan-2"`` 而库里已是
    ``"lin-yan"``)。命中后由 ``_persist_characters`` 改走 update 路径合并到既有
    行,避免角色实体冗余。

    遍历顺序按 ``character_id`` 升序(``list_characters_by_workspace`` 已排),
    首个命中即返(短路 — 名字精确一致或相似度 1.0 的会最先命中)。

    工作区角色数典型 N ≤ 30,O(N) 扫 + 单次 SQLAlchemy 查询代价可接受;后续若
    出现 N > 200 工作区需提速,可考虑加 LRU 缓存(留 v1.1)。
    """
    for char in list_characters_by_workspace(session, workspace_id):
        ratio = difflib.SequenceMatcher(None, char.name, target_name).ratio()
        if ratio >= _NAME_SIMILARITY_THRESHOLD:
            return char
    return None


def _merge_voice_profile(
    existing: VoiceProfile, incoming: VoiceProfile
) -> VoiceProfile:
    """P7-B-05:``voice_profile`` 6 字段级合并 — incoming 非空覆盖,为空保留 existing。

    对齐 ``v2_wishlist`` mock LLM 优化项段修法 2 字面:LLM 重抽同一角色时易把
    已知的 ``forbidden_phrases`` / ``catchphrases`` / ``addresses_others`` 等
    集合字段产为空(LLM 关注度衰减),字段级合并避免"已积累的角色声音规范在
    每次抽取后被空覆盖"。

    str 字段三件套(tone / sentence_style / self_address):空串视为缺失保留旧值;
    list / dict 字段三件套:空集合视为缺失保留旧值。
    """
    return VoiceProfile(
        tone=incoming.tone if incoming.tone else existing.tone,
        sentence_style=(
            incoming.sentence_style
            if incoming.sentence_style
            else existing.sentence_style
        ),
        self_address=(
            incoming.self_address if incoming.self_address else existing.self_address
        ),
        addresses_others=(
            dict(incoming.addresses_others)
            if incoming.addresses_others
            else dict(existing.addresses_others)
        ),
        catchphrases=(
            list(incoming.catchphrases)
            if incoming.catchphrases
            else list(existing.catchphrases)
        ),
        forbidden_phrases=(
            list(incoming.forbidden_phrases)
            if incoming.forbidden_phrases
            else list(existing.forbidden_phrases)
        ),
    )


def _earliest_chapter_id(*chapter_ids: str | None) -> str | None:
    """P7-B-05:取最早(字典序最小)的非空 chapter_id;全空返 ``None``。

    依赖项目章节 ID 命名约定 ``ch-NNNN`` 4 位补零 — 字典序与自然章节序一致
    (``ch-0003`` < ``ch-0006`` < ``ch-0010`` < ``ch-0099`` < ``ch-0100``)。
    用于 ``first_appearance_chapter`` 早优先合并(章节回写 / rewind 等场景下
    既有"首次出场"字段不被新章节 ID 覆盖)。
    """
    non_none = [cid for cid in chapter_ids if cid]
    return min(non_none) if non_none else None


def _persist_characters(
    session: Session,
    extracted: list[ExtractedCharacter],
    *,
    card: ChapterCard,
    workspace_id: str,
) -> list[Character]:
    """三类硬记忆写法对齐:get → 存在则 update / 不存在则 create。

    系统字段补全:``workspace_id`` 由 caller 显式注入(P3-B-14:**不再**读
    ``card.workspace_id``,卡片字段与操作工作区可能漂移);
    ``first_appearance_chapter`` create 时填本章 ID,update 时按"早优先"合并;
    ``last_updated_chapter`` 每次写入都更新为本章 ID。

    P7-B-05 合并规则增强(对齐 v2_wishlist mock LLM 优化项段修法 2):
    1. **名称相似度去重**:``character_id`` 找不到时,按 name 相似度
       (``difflib.SequenceMatcher`` ratio ≥ 0.85)在工作区扫一遍,命中
       视为同人 → 重写为既有 ``character_id`` 走 update 路径(LLM 改后缀
       漂移类场景的实体合并)。
    2. **first_appearance 早优先**:已有 + 本章 chapter_id 字典序取小者
       (rewind / 章节回写场景下旧首次出场不被新章节覆盖)。
    3. **voice_profile 字段级合并**:6 字段 incoming 非空才覆盖,为空保留
       既有值(LLM 重抽时空覆盖防御)。
    """
    saved: list[Character] = []
    for item in extracted:
        existing = get_character(session, item.character_id)
        effective_character_id = item.character_id

        # P7-B-05:character_id 找不到 → 按 name 相似度匹配工作区已有角色。
        if existing is None:
            similar = _find_similar_character(session, workspace_id, item.name)
            if similar is not None:
                existing = similar
                effective_character_id = similar.character_id

        new_character = Character(
            character_id=effective_character_id,
            workspace_id=workspace_id,
            name=item.name,
            aliases=list(item.aliases),
            archetype=item.archetype,
            appearance=item.appearance,
            personality=item.personality,
            background=item.background,
            voice_profile=item.voice_profile,
            goals_long_term=list(item.goals_long_term),
            goals_short_term=list(item.goals_short_term),
            current_state=dict(item.current_state),
            relationships=dict(item.relationships),
            abilities=list(item.abilities),
            secrets=list(item.secrets),
            first_appearance_chapter=card.chapter_id,
            last_updated_chapter=card.chapter_id,
        )
        if existing is None:
            saved.append(create_character(session, new_character))
        else:
            # P7-B-05:first_appearance 早优先 + voice_profile 字段级合并。
            merged_first_appearance = _earliest_chapter_id(
                existing.first_appearance_chapter, card.chapter_id
            )
            merged_voice = _merge_voice_profile(
                existing.voice_profile, item.voice_profile
            )
            # POST-v6 Session 3/C:rich-memory 仍可刷新普通派生状态，但不能
            # 擦掉同步核心提交刚写入的 approved_text 快照或 core.* 当前值。
            # update_character 是全量覆盖，因此必须显式保留两类权威字段；
            # 后面的 _persist_character_states 只合并同章非核心维度。
            merged_current_state = dict(item.current_state)
            merged_current_state.update(
                {
                    key: value
                    for key, value in existing.current_state.items()
                    if str(key).startswith("core.")
                }
            )
            new_character = new_character.model_copy(
                update={
                    "first_appearance_chapter": merged_first_appearance,
                    "voice_profile": merged_voice,
                    "current_state": merged_current_state,
                    "state_timeline": list(existing.state_timeline),
                }
            )
            saved.append(update_character(session, new_character))
    return saved


def _apply_character_disambiguation_in_place(
    *,
    extracted: list[ExtractedCharacter],
    content: str,
    session: Session,
    workspace_id: str,
    chapter_id: str,
    router: ModelRouter | None,
    llm: LLMClient | None,
    model: str | None,
    max_tokens: int,
    temperature: float,
    prompts_dir: Path | None,
) -> None:
    """A3 续(v1.1 wave 2 session 5):character 语义消歧 LLM-as-judge 兜底。

    流程:
    1. 从工作区读出 existing characters
    2. 双轨筛 top-K 候选(字面相似度 + 描述性指代兜底,防 LLM token 爆炸)
    3. 调 ``character_disambiguator`` LLM agent 综合判定 extracted × existing
       同一人不同表述对(描述性指代 vs 真名披露 / 别名 / 等)
    4. 对命中合并的 extracted item,**就地改 character_id = target_existing_id**,
       让下游 ``_persist_characters`` 自然走 get_character hit + update 路径

    fail-open / skip 路径不修改 extracted,P7-B-05 规则版 ``_find_similar_character``
    继续作第二道防御。
    """
    if not extracted:
        return

    existing = list_characters_by_workspace(session, workspace_id)
    if not existing:
        return

    from src.core.agents.character_disambiguator import (
        disambiguate_characters_with_existing,
        select_top_k_existing_candidates,
    )

    extracted_dicts = [
        {
            "character_id": it.character_id,
            "name": it.name,
            "description": (f"{it.archetype} | {it.appearance} | {it.personality}"),
        }
        for it in extracted
    ]

    top_k_existing = select_top_k_existing_candidates(
        extracted=extracted_dicts, existing=existing
    )
    if not top_k_existing:
        return

    # 章节正文前 1500 字符片段(供 LLM 判定真名披露场景)
    content_snippet = content[:1500] if content else ""

    result = disambiguate_characters_with_existing(
        extracted=extracted_dicts,
        existing=top_k_existing,
        chapter_id=chapter_id,
        chapter_content_snippet=content_snippet,
        workspace_id=workspace_id,
        router=router,
        client=llm,
        model=model,
        max_tokens=max_tokens,
        temperature=temperature,
        prompts_dir=prompts_dir,
    )

    if result.skipped or result.fail_open or not result.disambiguation_decisions:
        return

    id_remap = {
        d.extracted_character_id: d.target_existing_character_id
        for d in result.disambiguation_decisions
    }
    for item in extracted:
        if item.character_id in id_remap:
            item.character_id = id_remap[item.character_id]


def _apply_timeline_entry_merge_in_place(
    *,
    extracted: list[ExtractedTimelineEntry],
    session: Session,
    workspace_id: str,
    chapter_id: str,
    router: ModelRouter | None,
    llm: LLMClient | None,
    model: str | None,
    max_tokens: int,
    temperature: float,
    prompts_dir: Path | None,
) -> None:
    """A3 续(v1.1 wave 2 session 5):timeline_entry 语义合并 LLM-as-judge 兜底。

    流程:
    1. 从工作区读出 existing timeline_entries
    2. 按 description 字面相似度筛 top-K 候选(防 LLM token 爆炸)
    3. 调 ``timeline_entry_merger`` LLM agent 综合判定 extracted × existing
       同事件不同表述对(跨章节回忆 / 倒叙 / 同事件不同视角等)
    4. 对命中合并的 extracted item,**就地改 entry_id = target_existing_id**,
       让下游 ``_persist_timeline`` 自然走 get_timeline_entry hit + update 路径

    fail-open / skip 路径不修改 extracted。
    """
    if not extracted:
        return

    existing = list_timeline_entries_by_workspace(session, workspace_id)
    if not existing:
        return

    from src.core.agents.timeline_entry_merger import (
        merge_timeline_entries_with_existing,
        select_top_k_existing_candidates,
    )

    extracted_dicts = [
        {
            "entry_id": it.entry_id,
            "description": it.description,
            "time_label": it.time_label,
        }
        for it in extracted
    ]

    top_k_existing = select_top_k_existing_candidates(
        extracted=extracted_dicts, existing=existing
    )
    if not top_k_existing:
        return

    result = merge_timeline_entries_with_existing(
        extracted=extracted_dicts,
        existing=top_k_existing,
        chapter_id=chapter_id,
        workspace_id=workspace_id,
        router=router,
        client=llm,
        model=model,
        max_tokens=max_tokens,
        temperature=temperature,
        prompts_dir=prompts_dir,
    )

    if result.skipped or result.fail_open or not result.merge_decisions:
        return

    id_remap = {
        d.extracted_entry_id: d.target_existing_entry_id for d in result.merge_decisions
    }
    for item in extracted:
        if item.entry_id in id_remap:
            item.entry_id = id_remap[item.entry_id]


def _emergent_plot_thread_rejection_reasons(
    item: ExtractedPlotThread,
    *,
    content: str,
    chapter_id: str,
) -> list[str]:
    """返回正文中新建临时伏笔未达到高门槛的确定性原因。"""
    reasons: list[str] = []
    evidence = (item.evidence_quote or "").strip()
    if item.status not in {"planted", "developing"}:
        reasons.append("status_not_open")
    if item.importance < 4:
        reasons.append("importance_below_4")
    if item.planted_chapter != chapter_id:
        reasons.append("planted_chapter_not_current")
    if item.planned_payoff is not None:
        reasons.append("planned_payoff_must_be_null")
    if item.actual_payoff is not None:
        reasons.append("actual_payoff_must_be_null")
    if item.emergent_kind not in _EMERGENT_PLOT_THREAD_KINDS:
        reasons.append("emergent_kind_invalid_or_missing")
    if item.expiry_policy not in _EMERGENT_EXPIRY_POLICIES:
        reasons.append("expiry_policy_invalid")
    if item.expiry_policy == "never" and item.importance != 5:
        reasons.append("never_requires_importance_5")
    if len(item.description.strip()) < 16:
        reasons.append("description_too_short")
    if not (
        _EMERGENT_EVIDENCE_MIN_CHARS <= len(evidence) <= _EMERGENT_EVIDENCE_MAX_CHARS
    ):
        reasons.append("evidence_length_out_of_range")
    elif content.count(evidence) != 1:
        reasons.append("evidence_not_unique_exact_substring")
    return reasons


def _filter_new_emergent_plot_threads_before_merge(
    *,
    extracted: list[ExtractedPlotThread],
    content: str,
    session: Session,
    workspace_id: str,
    chapter_id: str,
    known_upstream_thread_ids: set[str] | None = None,
) -> list[ExtractedPlotThread]:
    """只拦 DB 与章节卡上游都不存在的新 ID；既有主伏笔更新原样直通。

    gate 放在语义 merger 前，防低质量新候选借“相似”覆盖大纲伏笔。被拒候选只
    从 plot_threads 数组丢弃；同一次抽取的 facts/timeline/角色记忆不受影响。
    """
    if not extracted:
        return []
    existing_ids = {
        thread.thread_id
        for thread in list_plot_threads_by_workspace(session, workspace_id)
    }
    existing_ids.update(known_upstream_thread_ids or set())
    admitted: list[ExtractedPlotThread] = []
    for item in extracted:
        if item.thread_id in existing_ids:
            admitted.append(item)
            continue
        reasons = _emergent_plot_thread_rejection_reasons(
            item,
            content=content,
            chapter_id=chapter_id,
        )
        if reasons:
            _log.info(
                "emergent_plot_thread_rejected",
                workspace_id=workspace_id,
                chapter_id=chapter_id,
                thread_id=item.thread_id,
                reasons=reasons,
            )
            continue
        item.evidence_quote = (item.evidence_quote or "").strip()
        admitted.append(item)
    return admitted


def _cap_new_emergent_plot_threads_after_merge(
    *,
    extracted: list[ExtractedPlotThread],
    session: Session,
    workspace_id: str,
    chapter_id: str,
    known_upstream_thread_ids: set[str] | None = None,
) -> list[ExtractedPlotThread]:
    """merger 后每章最多保留一条真正新临时伏笔，既有更新不计配额。"""
    if not extracted:
        return []
    existing_ids = {
        thread.thread_id
        for thread in list_plot_threads_by_workspace(session, workspace_id)
    }
    existing_ids.update(known_upstream_thread_ids or set())
    new_candidates = [item for item in extracted if item.thread_id not in existing_ids]
    if len(new_candidates) <= _EMERGENT_MAX_NEW_PER_CHAPTER:
        return extracted

    selected = sorted(
        new_candidates,
        key=lambda item: (
            -item.importance,
            -len(item.evidence_quote or ""),
            item.thread_id,
        ),
    )[:_EMERGENT_MAX_NEW_PER_CHAPTER]
    selected_object_ids = {id(item) for item in selected}
    result = [
        item
        for item in extracted
        if item.thread_id in existing_ids or id(item) in selected_object_ids
    ]
    _log.info(
        "emergent_plot_threads_capped",
        workspace_id=workspace_id,
        chapter_id=chapter_id,
        candidate_count=len(new_candidates),
        admitted_thread_ids=[item.thread_id for item in selected],
        rejected_thread_ids=[
            item.thread_id
            for item in new_candidates
            if id(item) not in selected_object_ids
        ],
        max_new=_EMERGENT_MAX_NEW_PER_CHAPTER,
    )
    return result


def _apply_plot_thread_merge_in_place(
    *,
    extracted: list[ExtractedPlotThread],
    session: Session,
    workspace_id: str,
    chapter_id: str,
    router: ModelRouter | None,
    llm: LLMClient | None,
    model: str | None,
    max_tokens: int,
    temperature: float,
    prompts_dir: Path | None,
) -> None:
    """A3 治本(v1.1 wave 2 session 5):plot_thread 语义合并 LLM-as-judge 兜底。

    流程:
    1. 从工作区读出 existing plot_threads
    2. 按 title 字面相似度筛 top-K 候选(防 LLM token 爆炸)
    3. 调 ``plot_thread_merger`` LLM agent 综合判定 extracted × existing 重复对
    4. 对命中合并的 extracted item,**就地改 thread_id = target_existing_id**,
       让下游 ``_persist_plot_threads`` 自然走既有 update 路径(覆盖字段)
       而非新建独立行

    fail-open / skip 路径不修改 extracted,保证 caller 行为不回归。
    """
    if not extracted:
        return

    existing = list_plot_threads_by_workspace(session, workspace_id)
    if not existing:
        return

    # 延迟 import 避免循环依赖(plot_thread_merger 不引用 memory_extractor)
    from src.core.agents.plot_thread_merger import (
        merge_plot_threads_with_existing,
        select_top_k_existing_candidates,
    )

    extracted_dicts = [
        {
            "thread_id": it.thread_id,
            "title": it.title,
            "description": it.description,
        }
        for it in extracted
    ]

    top_k_existing = select_top_k_existing_candidates(
        extracted=extracted_dicts, existing=existing
    )
    if not top_k_existing:
        return

    result = merge_plot_threads_with_existing(
        extracted=extracted_dicts,
        existing=top_k_existing,
        chapter_id=chapter_id,
        workspace_id=workspace_id,
        router=router,
        client=llm,
        model=model,
        max_tokens=max_tokens,
        temperature=temperature,
        prompts_dir=prompts_dir,
    )

    if result.skipped or result.fail_open or not result.merge_decisions:
        return

    # 应用合并决策:就地改 extracted item 的 thread_id 为 target_existing_id
    id_remap = {
        d.extracted_thread_id: d.target_existing_thread_id
        for d in result.merge_decisions
    }
    for item in extracted:
        if item.thread_id in id_remap:
            item.thread_id = id_remap[item.thread_id]


def _persist_plot_threads(
    session: Session,
    extracted: list[ExtractedPlotThread],
    *,
    card: ChapterCard,
    workspace_id: str,
) -> list[PlotThread]:
    saved: list[PlotThread] = []
    now = datetime.now(UTC)
    # 历史内部调用/回放 fixture 只提供 chapter_id；正式 ChapterCard 始终有该字段。
    # 缺失时按“不是上游已规划 ID”处理，保持 helper 向后兼容。
    upstream_thread_ids = set(getattr(card, "related_plot_threads", []))
    for item in extracted:
        existing = get_plot_thread(session, item.thread_id)
        if existing is None:
            new_thread = PlotThread(
                thread_id=item.thread_id,
                workspace_id=workspace_id,
                title=item.title,
                status=item.status,  # type: ignore[arg-type]
                importance=item.importance,
                planted_chapter=item.planted_chapter,
                planned_payoff=item.planned_payoff,
                actual_payoff=item.actual_payoff,
                description=item.description,
                related_characters=list(item.related_characters),
                created_at=now,
                last_updated_chapter=card.chapter_id,
            )
            origin = "outline" if item.thread_id in upstream_thread_ids else "emergent"
            saved.append(
                create_plot_thread(
                    session,
                    new_thread,
                    origin=origin,
                    expiry_policy=(
                        item.expiry_policy
                        if origin == "emergent" and item.expiry_policy is not None
                        else "never"
                    ),
                    source_evidence=(
                        item.evidence_quote if origin == "emergent" else None
                    ),
                )
            )
        else:
            updated = existing.model_copy(
                update={
                    "title": item.title,
                    "status": item.status,
                    "importance": item.importance,
                    # 早优先合并(对齐 _persist_characters first_appearance_chapter):
                    # planted_chapter = 首次埋设章,语义上不可变,update 时**不被后
                    # 续章覆盖**。否则长伏笔(ch-2 引入、ch-7 才落库)的 planted_chapter
                    # 被覆盖成 ch-7 → rewind ch-7 按 planted==ch-7 误 revoke 该长伏笔 →
                    # 重跑 reference gate 判 unknown 死锁(luo-clean-v2 ch-7 rewind 现场,
                    # v1.1 w4 b1 rewind 链路缺陷治本)。
                    "planted_chapter": _earliest_chapter_id(
                        existing.planted_chapter, item.planted_chapter
                    )
                    or item.planted_chapter,
                    # Extractor 只观察已写正文，不得用 null 抹掉大纲/人工预排的
                    # 收口章；有明确新值时仍沿用既有更新能力。
                    "planned_payoff": (
                        item.planned_payoff
                        if item.planned_payoff is not None
                        else existing.planned_payoff
                    ),
                    "actual_payoff": item.actual_payoff,
                    "description": item.description,
                    "related_characters": list(item.related_characters),
                    "last_updated_chapter": card.chapter_id,
                }
            )
            saved.append(update_plot_thread(session, updated))
    return saved


def _persist_timeline(
    session: Session,
    extracted: list[ExtractedTimelineEntry],
    *,
    card: ChapterCard,
    workspace_id: str,
) -> list[TimelineEntry]:
    """落库 timeline 条目,**用全局章序硬注入 time_order 基数**(S3 治本,v1.1 w4 b1)。

    **根因**:prompt 曾引导 LLM 用 ``卷内 chapter_number * 100 + 章内序`` 赋
    time_order,而 chapter_number 跨卷重置(``chapter_card.py:13`` 契约:不能作
    时序锚)→ 卷二第 1 章(基数 1→1xx)与卷一第 1 章(同 1xx)碰撞 → ``timeline_
    monotonic`` 确定性多卷阻塞(ch-8 现场,S2 同源根因)。

    **治本**:落库时不信任 LLM 给的基数,改由代码用全局章序 ``chapter_seq_from_id``
    (``ch-0007`` → 7)定基数;保留 LLM 对**章内**事件的相对先后(按其 time_order
    稳定排序取 rank)。结果 ``time_order = 全局章序*100 + 章内 rank`` → 不同章号段
    落不重叠、全局严格单调,碰撞在数学上不可能(不依赖 LLM 守约定)。
    """
    saved: list[TimelineEntry] = []
    seq = chapter_seq_from_id(card.chapter_id)
    base: int | None = seq * 100 if seq is not None else None
    if base is None:
        # chapter_id 非 ch-NNNN 模式(理论不出现):保留 LLM 原值,记 warn 留痕。
        _log.warning(
            "timeline_time_order_base_fallback",
            chapter_id=card.chapter_id,
            workspace_id=workspace_id,
            note="chapter_id 非 ch-NNNN,无法取全局章序,保留 LLM 原始 time_order",
        )
    # 按 LLM 给的 time_order 章内稳定排序 → 章内 rank(1-based);平手保原顺序。
    rank_by_index: dict[int, int] = {
        orig_idx: rank
        for rank, (orig_idx, _item) in enumerate(
            sorted(enumerate(extracted), key=lambda p: p[1].time_order), start=1
        )
    }
    for idx, item in enumerate(extracted):
        time_order = base + rank_by_index[idx] if base is not None else item.time_order
        new_entry = TimelineEntry(
            entry_id=item.entry_id,
            workspace_id=workspace_id,
            chapter_id=card.chapter_id,
            description=item.description,
            time_label=item.time_label,
            time_order=time_order,
            involved_characters=list(item.involved_characters),
            is_flashback=item.is_flashback,
        )
        existing = get_timeline_entry(session, item.entry_id)
        if existing is None:
            saved.append(create_timeline_entry(session, new_entry))
        else:
            saved.append(update_timeline_entry(session, new_entry))
    return saved


# ---- Chroma 写入 -----------------------------------------------------------


def _persist_to_vector_store(
    *,
    vector_store: VectorStore,
    characters: list[Character],
    plot_threads: list[PlotThread],
    timeline: list[TimelineEntry],
    facts: list[FactEntry],
    card: ChapterCard,
    workspace_id: str,
    on_vector_write_started: Callable[[], None] | None = None,
) -> None:
    """四类合并批量写 Chroma。

    Chroma id = ``f"{entity_type}-{entity_id}"``;同 ID 行为是 upsert,
    天然实现"角色属性更新时向量覆盖"。
    metadata 五字段(``entity_type`` / ``entity_id`` / ``chapter_id`` /
    ``workspace_id`` / ``created_at``)。
    document 文本约定:角色用 ``name + archetype + personality + background``
    拼接(让 retriever 命中"林衍 沉静内敛 寒门出身"等查询);伏笔用
    ``title + description``;时间线用 ``description + time_label``;fact 用
    ``description``。
    """
    ids: list[str] = []
    documents: list[str] = []
    metadatas: list[dict] = []
    created_at_iso = datetime.now(UTC).isoformat()

    for ch in characters:
        ids.append(f"character-{ch.character_id}")
        documents.append(_compose_character_doc(ch))
        metadatas.append(
            _make_metadata(
                entity_type="character",
                entity_id=ch.character_id,
                card=card,
                workspace_id=workspace_id,
                created_at=created_at_iso,
            )
        )
    for th in plot_threads:
        ids.append(f"plot_thread-{th.thread_id}")
        documents.append(_compose_plot_thread_doc(th))
        metadatas.append(
            _make_metadata(
                entity_type="plot_thread",
                entity_id=th.thread_id,
                card=card,
                workspace_id=workspace_id,
                created_at=created_at_iso,
            )
        )
    for tl in timeline:
        ids.append(f"timeline-{tl.entry_id}")
        documents.append(_compose_timeline_doc(tl))
        metadatas.append(
            _make_metadata(
                entity_type="timeline",
                entity_id=tl.entry_id,
                card=card,
                workspace_id=workspace_id,
                created_at=created_at_iso,
            )
        )
    for fa in facts:
        ids.append(f"fact-{fa.fact_id}")
        documents.append(fa.description)
        metadatas.append(
            _make_metadata(
                entity_type="fact",
                entity_id=fa.fact_id,
                card=card,
                workspace_id=workspace_id,
                created_at=created_at_iso,
            )
        )

    # 单批 ids 去重(v1.1 w4 b1):防 LLM extract 重复输出同 entity_id(同一次抽取
    # 里两条 thread-3)→ ids 单批重复 → chroma upsert 抛 DuplicateIDError(单次调用
    # 要求 ids 唯一,luo-clean-v2 ch-7 重跑现场)。同 id 保留最后一条(最新值)。
    if ids:
        deduped: dict[str, tuple[str, dict]] = {}
        for _id, _doc, _meta in zip(ids, documents, metadatas):
            deduped[_id] = (_doc, _meta)
        ids = list(deduped.keys())
        documents = [deduped[_id][0] for _id in ids]
        metadatas = [deduped[_id][1] for _id in ids]

    if not ids:
        return
    if on_vector_write_started is not None:
        on_vector_write_started()
    vector_store.add(
        DEFAULT_COLLECTION,
        ids=ids,
        documents=documents,
        metadatas=metadatas,
    )


def _make_metadata(
    *,
    entity_type: str,
    entity_id: str,
    card: ChapterCard,
    workspace_id: str,
    created_at: str,
) -> dict:
    """metadata(P2-B-09 拍板五字段;P3-B-14:``workspace_id`` 由 caller 显式注入;
    G1 v1.1 wave 4 session 1 加 ``chapter_seq`` 第六字段供 retriever 时序过滤)。

    ``chapter_seq``:从 ``chapter_id`` 解析的**全局**章序整数(``ch-0005`` → 5),
    供 retriever ``before_chapter_seq`` 用 Chroma ``$lt`` 数值范围过滤——Chroma
    range 操作符只对数值生效,字符串 ``chapter_id`` 无法做 ``$lt``。
    ``chapter_id`` 非 ``ch-NNNN`` 模式时省略该字段(Chroma 不接受 ``None`` 值;
    此类条目不参与时序过滤)。
    """
    metadata: dict = {
        "entity_type": entity_type,
        "entity_id": entity_id,
        "chapter_id": card.chapter_id,
        "workspace_id": workspace_id,
        "created_at": created_at,
    }
    seq = chapter_seq_from_id(card.chapter_id)
    if seq is not None:
        metadata["chapter_seq"] = seq
    return metadata


def _compose_character_doc(ch: Character) -> str:
    """角色向量 document 文本:让 retriever 在按"姓名 / 性格 / 背景"等
    多关键词查询时都能命中。"""
    parts = [ch.name]
    if ch.archetype:
        parts.append(ch.archetype)
    if ch.personality:
        parts.append(ch.personality)
    if ch.background:
        parts.append(ch.background)
    return " | ".join(parts)


def _compose_plot_thread_doc(th: PlotThread) -> str:
    return f"{th.title} | {th.description}"


def _compose_timeline_doc(tl: TimelineEntry) -> str:
    return f"{tl.description} | {tl.time_label}"
