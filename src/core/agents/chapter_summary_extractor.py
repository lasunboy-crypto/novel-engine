"""章节事实摘要与 W3.5-R4 同步连续性小账本。

``extract_chapter_summary`` 和
``extract_and_persist_chapter_summary_safely`` 保留给历史回填/兼容调用，
仍是 fail-open。生产批准入口必须调用
``extract_and_persist_continuity_ledger``：它要求有效 final/version/card，
逐条验证结构事实的原文 quote 与 fingerprint，复算来源及 ledger hash，并在
同一同步链上写 ``validated_at``。只有该严格入口成功后，服务层才可设置
``ready_for_next_chapter_at``；向量和完整记忆继续是 ready 之后的 fail-open
增强层。
"""

from __future__ import annotations

import json
import re
from collections.abc import Callable
from dataclasses import dataclass
from datetime import UTC, datetime
from pathlib import Path
from typing import Any, Literal, cast

import structlog
from pydantic import BaseModel, ConfigDict, Field, ValidationError

from src.config import settings
from src.core.agents._json_contract import loads_with_quote_repair
from src.core.agents._prompt import (
    append_role_marker,
    load_prompt_template,
    resolve_routing,
)
from src.core.agents._retry_feedback import (
    SchemaRetryExhausted,
    run_with_schema_retry,
)
from src.core.agents._structured_output import prepare_strict_json_output
from src.core.agents.creative_delta import uses_hy3_creative_delta
from src.core.llm.authority_bound_selection import (
    AuthorityBoundSelectionError,
    AuthorityCatalog,
    AuthorityCatalogEntry,
    AuthorityReferenceBinding,
    AuthoritySelectionContract,
    AuthoritySnapshot,
)
from src.core.llm.base import LLMClient, LLMResponse, Message
from src.core.models.chapter_card import ChapterCard
from src.core.models.continuity_ledger import (
    ContinuityEvidenceRecord,
    ContinuityLedgerProvenance,
    ProvenanceBoundContinuityCapsule,
    compute_continuity_ledger_hash,
    make_evidence_record,
    raw_final_tail,
    sha256_text,
    validate_evidence_records,
    validate_frozen_provenance,
)
from src.core.models.core_state import (
    CORE_STATE_RELATION_ATTRIBUTES,
    CORE_STATE_RELATION_EVIDENCE_GUIDANCE,
    CoreStateCandidate,
    make_state_key,
)
from src.core.routing.router import ModelRouter

_log = structlog.get_logger(__name__)

# R4 ready hard-watermark path owns this bounded schema/business retry limit.
CONTINUITY_LEDGER_SCHEMA_MAX_ATTEMPTS = 3

_EXTRACTOR_PROMPT_FILENAME = "chapter_summary_extractor_v2.md"
_SOURCE_REF_PROMPT_FILENAME = "chapter_summary_extractor_v3.md"

# v1.1 wave 3 s6(E1):章节摘要入 Chroma 的 collection 名,对齐
# ``memory_extractor.DEFAULT_COLLECTION`` / ``retriever.DEFAULT_COLLECTION``
# 统一 "memories"。摘要以 entity_type="chapter_summary" 与既有四类(character /
# plot_thread / timeline / fact)共存同一 collection,按 entity_type 过滤检索互不干扰。
DEFAULT_COLLECTION = "memories"

# 默认生成参数:摘要任务温度稍高允许重组表达,但仍偏稳;输出 ~500-800 字 JSON。
# router 路径下由 profile 决定(local_qwen_32k:temp 0.5 / num_predict 2048),
# legacy 路径保留本默认值(单测兜底)。
_DEFAULT_TEMPERATURE = 0.5
_DEFAULT_MAX_TOKENS = 2000

# 匹配 ```json ... ``` 或 ``` ... ``` 包裹;沿用既有 LLM agent 同型剥离。
_CODE_FENCE_RE = re.compile(
    r"^\s*```(?:json)?\s*\n(?P<body>.*?)\n```\s*$",
    re.DOTALL,
)
_TRAILING_HTML_COMMENT_RE = re.compile(
    r"\s*<!--.*?-->\s*$",
    re.DOTALL,
)
_SOURCE_BOUNDARY_RE = re.compile(
    r"(?:(?:[。！？!?；;]+|…{2,})[。！？!?；;…]*[”’」』）》】]*)|(?:\r?\n+)"
)
ExtractionTrigger = Literal["reviewer_pass", "force_pass"]
"""``ChapterSummaryORM.extraction_trigger`` 允许值(对齐
``src/core/db/models.py:CHAPTER_SUMMARY_TRIGGER_VALUES``)。"""


class ContinuityLedgerValidationError(ValueError):
    """R4 同步小账本抽取、持久化或来源校验失败。"""


@dataclass(frozen=True, slots=True)
class _EvidenceSource:
    """严格 R4 路径中的一段确定性原文切片。"""

    source_id: str
    start: int
    end: int


class _KeyEventPayload(BaseModel):
    """LLM 输出 ``key_events`` 单项的中间模型。"""

    description: str = Field(..., min_length=1, max_length=80)
    actors: list[str] = Field(default_factory=list)
    location: str = Field(default="")
    evidence_quote: str = Field(..., min_length=1)


class _CharacterInteractionPayload(BaseModel):
    """LLM 输出 ``character_interactions`` 单项的中间模型。"""

    char_a: str = Field(..., min_length=1)
    char_b: str = Field(..., min_length=1)
    dynamic: str = Field(..., min_length=1, max_length=60)
    evidence_quote: str = Field(..., min_length=1)


class _UnfinishedActionPayload(BaseModel):
    """LLM 输出的一条未完动作及其即时性/原文证据。"""

    description: str = Field(..., min_length=1)
    is_immediate: bool = False
    evidence_quote: str = Field(..., min_length=1)


class _CharacterStatePayload(BaseModel):
    """角色在本章结尾的可观察状态。"""

    character_id: str = Field(..., min_length=1)
    state: str = Field(..., min_length=1)
    evidence_quote: str = Field(..., min_length=1)


class _EstablishedFactPayload(BaseModel):
    """本章已确定、后文不得无故推翻的事实。"""

    fact: str = Field(..., min_length=1)
    evidence_quote: str = Field(..., min_length=1)


class _RevealedMechanicPayload(BaseModel):
    """本章已明确揭示的机制边界。"""

    mechanic: str = Field(..., min_length=1)
    evidence_quote: str = Field(..., min_length=1)


class _CoreStateCandidatePayload(BaseModel):
    """模型只提名承重状态，最终是否 HARD 由确定性服务判定。"""

    entity_type: Literal["object", "character"]
    entity_id: str = Field(..., min_length=1, max_length=80)
    attribute: Literal[
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
    value: str = Field(..., min_length=1, max_length=160)
    objective: bool
    long_lived: bool
    book_bearing: bool
    ambiguous: bool = False
    evidence_quote: str = Field(..., min_length=1)


class _ExtractorLLMPayload(BaseModel):
    """LLM 输出的 R4 中间模型(结构事实必须逐条带原文 quote)。

    对齐 ``prompts/chapter_summary_extractor_v2.md``；fingerprint 不信任 LLM，
    由代码在确认 quote 可定位后计算。
    """

    summary_text: str = Field(..., min_length=1, max_length=2000)
    pov_character: str = Field(default="")
    pov_evidence_quote: str = Field(default="")
    scene_setting: str = Field(default="")
    scene_setting_evidence_quote: str = Field(default="")
    key_events: list[_KeyEventPayload] = Field(default_factory=list)
    character_interactions: list[_CharacterInteractionPayload] = Field(
        default_factory=list
    )
    unfinished_actions: list[_UnfinishedActionPayload] = Field(default_factory=list)
    character_states: list[_CharacterStatePayload] = Field(default_factory=list)
    established_facts: list[_EstablishedFactPayload] = Field(default_factory=list)
    revealed_mechanics: list[_RevealedMechanicPayload] = Field(default_factory=list)
    core_state_candidates: list[_CoreStateCandidatePayload] = Field(
        default_factory=list
    )


class _SourceRefPayloadBase(BaseModel):
    """严格 source ID wire schema 不允许悄悄吞掉 quote/offset 等额外字段。"""

    model_config = ConfigDict(extra="forbid")


class _KeyEventSourceRefPayload(_SourceRefPayloadBase):
    """严格 R4 路径的关键事件候选，只允许引用 source ID。"""

    description: str = Field(..., min_length=1, max_length=80)
    actors: list[str] = Field(default_factory=list)
    location: str = Field(default="")
    evidence_source_id: str = Field(..., min_length=1)


class _CharacterInteractionSourceRefPayload(_SourceRefPayloadBase):
    """严格 R4 路径的人物互动候选，只允许引用 source ID。"""

    char_a: str = Field(..., min_length=1)
    char_b: str = Field(..., min_length=1)
    dynamic: str = Field(..., min_length=1, max_length=60)
    evidence_source_id: str = Field(..., min_length=1)


class _UnfinishedActionSourceRefPayload(_SourceRefPayloadBase):
    """严格 R4 路径的未完动作候选，只允许引用 source ID。"""

    description: str = Field(..., min_length=1)
    is_immediate: bool = False
    evidence_source_id: str = Field(..., min_length=1)


class _CharacterStateSourceRefPayload(_SourceRefPayloadBase):
    """严格 R4 路径的角色状态候选，只允许引用 source ID。"""

    character_id: str = Field(..., min_length=1)
    state: str = Field(..., min_length=1)
    evidence_source_id: str = Field(..., min_length=1)


class _EstablishedFactSourceRefPayload(_SourceRefPayloadBase):
    """严格 R4 路径的既定事实候选，只允许引用 source ID。"""

    fact: str = Field(..., min_length=1)
    evidence_source_id: str = Field(..., min_length=1)


class _RevealedMechanicSourceRefPayload(_SourceRefPayloadBase):
    """严格 R4 路径的机制候选，只允许引用 source ID。"""

    mechanic: str = Field(..., min_length=1)
    evidence_source_id: str = Field(..., min_length=1)


class _CoreStateCandidateSourceRefPayload(_SourceRefPayloadBase):
    """严格 source ID 协议下的核心状态候选。"""

    entity_type: Literal["object", "character"]
    entity_id: str = Field(..., min_length=1, max_length=80)
    attribute: Literal[
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
    value: str = Field(..., min_length=1, max_length=160)
    objective: bool
    long_lived: bool
    book_bearing: bool
    ambiguous: bool = False
    evidence_source_id: str = Field(..., min_length=1)


class _ExtractorSourceRefPayload(_SourceRefPayloadBase):
    """严格 R4 Provider 输出：事实由模型抽取，逐字证据由 source ID 锚定。"""

    summary_text: str = Field(..., min_length=1, max_length=2000)
    pov_character: str = Field(...)
    pov_evidence_source_id: str = Field(...)
    scene_setting: str = Field(...)
    scene_setting_evidence_source_id: str = Field(...)
    key_events: list[_KeyEventSourceRefPayload] = Field(...)
    character_interactions: list[_CharacterInteractionSourceRefPayload] = Field(...)
    unfinished_actions: list[_UnfinishedActionSourceRefPayload] = Field(...)
    character_states: list[_CharacterStateSourceRefPayload] = Field(...)
    established_facts: list[_EstablishedFactSourceRefPayload] = Field(...)
    revealed_mechanics: list[_RevealedMechanicSourceRefPayload] = Field(...)
    core_state_candidates: list[_CoreStateCandidateSourceRefPayload] = Field(
        default_factory=list
    )


_SUMMARY_AUTHORITY_BINDINGS = (
    AuthorityReferenceBinding(
        field_path="pov_character",
        catalogs=("characters",),
        allow_empty_value=True,
    ),
    AuthorityReferenceBinding(
        field_path="pov_evidence_source_id",
        catalogs=("evidence_sources",),
        allow_empty_value=True,
    ),
    AuthorityReferenceBinding(
        field_path="scene_setting_evidence_source_id",
        catalogs=("evidence_sources",),
        allow_empty_value=True,
    ),
    AuthorityReferenceBinding(
        field_path="key_events[].actors[]",
        catalogs=("characters",),
    ),
    AuthorityReferenceBinding(
        field_path="key_events[].evidence_source_id",
        catalogs=("evidence_sources",),
    ),
    AuthorityReferenceBinding(
        field_path="character_interactions[].char_a",
        catalogs=("characters",),
    ),
    AuthorityReferenceBinding(
        field_path="character_interactions[].char_b",
        catalogs=("characters",),
    ),
    AuthorityReferenceBinding(
        field_path="character_interactions[].evidence_source_id",
        catalogs=("evidence_sources",),
    ),
    AuthorityReferenceBinding(
        field_path="unfinished_actions[].evidence_source_id",
        catalogs=("evidence_sources",),
    ),
    AuthorityReferenceBinding(
        field_path="character_states[].character_id",
        catalogs=("characters",),
    ),
    AuthorityReferenceBinding(
        field_path="character_states[].evidence_source_id",
        catalogs=("evidence_sources",),
    ),
    AuthorityReferenceBinding(
        field_path="established_facts[].evidence_source_id",
        catalogs=("evidence_sources",),
    ),
    AuthorityReferenceBinding(
        field_path="revealed_mechanics[].evidence_source_id",
        catalogs=("evidence_sources",),
    ),
    AuthorityReferenceBinding(
        field_path="core_state_candidates[].entity_id",
        catalogs=("characters", "objects"),
    ),
    AuthorityReferenceBinding(
        field_path="core_state_candidates[].evidence_source_id",
        catalogs=("evidence_sources",),
    ),
)


class ChapterSummary(BaseModel):
    """单章摘要 — caller(hook + backfill)用于落 ``ChapterSummaryORM``。

    与 ``ChapterSummaryORM`` 字段一一对应,但 Pydantic 层多 ``extracted_at``
    时间戳(ORM 层用 ``created_at`` / ``updated_at`` 两字段)。

    ``pov_character`` / ``scene_setting`` Python 层用 ``None`` 表示"无明确视
    角 / 场景"(LLM 输出空字符串时由 ``_payload_to_summary`` 转 None);ORM 列
    ``nullable=True`` 与之对齐。
    """

    chapter_id: str
    workspace_id: str
    summary_text: str
    pov_character: str | None = None
    scene_setting: str | None = None
    key_events: list[dict] = Field(default_factory=list)
    character_interactions: list[dict] = Field(default_factory=list)
    unfinished_actions: list[str] = Field(default_factory=list)
    character_states: list[dict] = Field(default_factory=list)
    established_facts: list[str] = Field(default_factory=list)
    revealed_mechanics: list[str] = Field(default_factory=list)
    core_state_candidates: list[dict] = Field(default_factory=list)
    raw_final_tail: str = ""
    source_version_id: int = 0
    source_text_hash: str = ""
    evidence_records: list[dict] = Field(default_factory=list)
    ledger_hash: str = ""
    validated_at: datetime | None = None
    extraction_trigger: ExtractionTrigger
    model_used: str
    extracted_at: datetime


def _core_candidates_for_ledger_hash(candidates: list[dict]) -> list[dict]:
    """只把模型提出时的不可变语义纳入 ledger hash。

    admission_status/reason 是批准后纯代码写回的裁决结果，不能反过来改变
    ChapterSummary 的来源身份。
    """

    return [
        CoreStateCandidate.model_validate(item).model_dump(
            mode="json", exclude={"admission_status", "admission_reason"}
        )
        for item in candidates
    ]


def extract_chapter_summary(
    *,
    chapter_text: str,
    card: ChapterCard,
    chapter_id: str,
    workspace_id: str,
    extraction_trigger: ExtractionTrigger,
    router: ModelRouter | None = None,
    client: LLMClient | None = None,
    model: str | None = None,
    max_tokens: int = _DEFAULT_MAX_TOKENS,
    temperature: float = _DEFAULT_TEMPERATURE,
    prompts_dir: Path | None = None,
    source_version_id: int | None = None,
    schema_retry_max_attempts: int = 1,
    character_names: dict[str, str] | None = None,
    current_core_states: dict[str, str] | None = None,
    use_evidence_source_catalog: bool = False,
    non_retryable_exceptions: tuple[type[Exception], ...] = (),
) -> ChapterSummary | None:
    """从 ``final.md`` 全文抽取章节摘要(LLM 路径)。

    失败 fail-open 返 ``None``(不阻塞 caller 主流程,下游 chapter_card
    derive_context 读不到该章摘要时自然降级到会话 2 的 ChapterCard SSOT 字段
    映射)。

    :param chapter_text: 章节正文(由 caller 从 ``ChapterVersion.final.md``
        读出)。
    :param card: 章节卡(给 LLM 提供 chapter_number / title / pov_character /
        setting / related_characters / related_plot_threads 元数据锚)。
    :param chapter_id: 章节 ID(注入 ChapterSummary + structlog 字段)。
    :param workspace_id: 工作区 ID(注入 ChapterSummary + structlog 字段;
        P3-B-14 透传纪律,与 ``card.workspace_id`` 解耦避免模板复用漂移)。
    :param extraction_trigger: ``reviewer_pass`` 或 ``force_pass``(由 caller
        显式注入,本 agent 不感知触发来源)。
    :param router: ``ModelRouter`` 实例;给定时按
        ``router.route("chapter_summary_extractor")`` 取 client + model + 生成
        参数。
    :param client: legacy 路径 LLM 客户端;``router=None`` 时必传。
    :param model: legacy 路径模型名;``router=None`` 时必传。
    :param max_tokens: legacy 路径 token 上限(``router`` 路径由 profile 决定)。
    :param temperature: legacy 路径温度(``router`` 路径由 profile 决定)。
    :param prompts_dir: 提示词模板根目录;``None`` 时取 ``settings.prompts_dir``。
    :param source_version_id: R4 同步小账本对应的最终正文版本；legacy/回填可空，
        但空值产物没有资格通过 ready 硬水位。
    :param schema_retry_max_attempts: JSON/schema/逐字证据校验的最大尝试次数。
        legacy fail-open 调用保持 1；R4 ready 硬水位入口显式使用 3。
    :param use_evidence_source_catalog: 严格 R4 ready 路径为 True。Provider 只
        返回确定性原文目录 ID，代码再从同一正文快照按 offset 取回逐字证据；
        legacy 路径保持 False 和 v2 quote schema。
    :param non_retryable_exceptions: strict caller 要求原样上抛的异常类型；
        默认空 tuple 保持 legacy transport fail-open。
    :returns: ``ChapterSummary`` 或 ``None``(fail-open)。
    """
    if not chapter_text or not chapter_text.strip():
        _log.warning(
            "chapter_summary_extractor_empty_text",
            chapter_id=chapter_id,
            workspace_id=workspace_id,
        )
        return None

    character_names = dict(character_names or {})
    exact_client_before_routing = client is not None and uses_hy3_creative_delta(client)
    source_catalog: tuple[_EvidenceSource, ...] = ()
    authority_selection: AuthoritySelectionContract | None = None
    if exact_client_before_routing:
        source_catalog = _build_evidence_source_catalog(chapter_text)
        authority_selection = _chapter_summary_authority_contract(
            card=card,
            chapter_text=chapter_text,
            source_catalog=source_catalog,
            character_names=character_names,
        )

    # setup 阶段失败也走 fail-open(prompt 文件缺失 / 路由错配 / 等)
    resolved_is_exact_hy3 = False
    try:
        resolved_client, resolved_model, complete_kwargs = resolve_routing(
            agent_name="chapter_summary_extractor",
            router=router,
            client=client,
            model=model,
            legacy_kwargs={"max_tokens": max_tokens, "temperature": temperature},
        )
        # T2: exact Hy3 always uses the already-proven source-ID delta path.
        # Legacy callers/models retain their explicit flag and v2 quote wire.
        use_evidence_source_catalog = (
            use_evidence_source_catalog
            or uses_hy3_creative_delta(resolved_client)
        )
        resolved_is_exact_hy3 = uses_hy3_creative_delta(resolved_client)
        if use_evidence_source_catalog:
            source_catalog = _build_evidence_source_catalog(chapter_text)
            if resolved_is_exact_hy3 and authority_selection is None:
                authority_selection = _chapter_summary_authority_contract(
                    card=card,
                    chapter_text=chapter_text,
                    source_catalog=source_catalog,
                    character_names=character_names,
                )
        template_root = (
            prompts_dir if prompts_dir is not None else settings.prompts_dir
        ).resolve()
        prompt_filename = (
            _SOURCE_REF_PROMPT_FILENAME
            if use_evidence_source_catalog
            else _EXTRACTOR_PROMPT_FILENAME
        )
        system_text = load_prompt_template(template_root / prompt_filename)
        if use_evidence_source_catalog:
            if resolved_is_exact_hy3 and authority_selection is None:
                raise AuthorityBoundSelectionError(
                    "summary source-ref schema 缺少 frozen authority selection"
                )
            complete_kwargs = prepare_strict_json_output(
                client=resolved_client,
                model=resolved_model,
                output_model=_ExtractorSourceRefPayload,
                schema_name="chapter_summary",
                complete_kwargs=complete_kwargs,
                transform=(
                    authority_selection.transform_schema
                    if authority_selection is not None
                    else None
                ),
            )
        else:
            complete_kwargs = prepare_strict_json_output(
                client=resolved_client,
                model=resolved_model,
                output_model=_ExtractorLLMPayload,
                schema_name="chapter_summary",
                complete_kwargs=complete_kwargs,
            )
    except Exception as exc:  # noqa: BLE001 — fail-open 包括 setup 阶段
        if exact_client_before_routing or resolved_is_exact_hy3:
            raise
        _log.warning(
            "chapter_summary_extractor_setup_failed_open",
            chapter_id=chapter_id,
            workspace_id=workspace_id,
            error_type=type(exc).__name__,
            error_message=str(exc),
        )
        return None

    user_text = _build_user_message(
        card=card,
        chapter_text=chapter_text,
        chapter_id=chapter_id,
        workspace_id=workspace_id,
        source_catalog=source_catalog,
        use_evidence_source_catalog=use_evidence_source_catalog,
        authority_selection=authority_selection,
        character_names=character_names,
        current_core_states=current_core_states,
    )

    def _llm_call(messages: list[Message]) -> LLMResponse:
        return resolved_client.complete(
            messages=messages,
            model=resolved_model,
            **complete_kwargs,
        )

    validated_summary: ChapterSummary | None = None

    def _validate_exact_evidence(payload: _ExtractorLLMPayload) -> None:
        """逐条执行既有 exact quote/fingerprint 构造，失败交回模型修正。"""

        nonlocal validated_summary
        try:
            _validate_core_state_candidate_identities(payload=payload, card=card)
            validated_summary = _payload_to_summary(
                payload=payload,
                chapter_id=chapter_id,
                workspace_id=workspace_id,
                extraction_trigger=extraction_trigger,
                model_used=resolved_model,
                chapter_text=chapter_text,
                source_version_id=source_version_id,
            )
        except ValueError as exc:
            raise ValueError(
                f"{exc}。只能从本次章节正文逐字复制连续原文；"
                "禁止模糊匹配、改写、拼接或猜测证据。"
                "正文没有能证明该事实的原句时，请删除对应数组项；"
                "POV 或场景无证据时，请将事实字段与 quote 一起置空。"
            ) from exc

    def _validate_source_refs(payload: _ExtractorSourceRefPayload) -> None:
        """把 exact source ID 映射回同一正文快照，再走既有 R4 校验。"""

        nonlocal validated_summary
        try:
            if authority_selection is not None:
                authority_selection.assert_matches(
                    _chapter_summary_authority_contract(
                        card=card,
                        chapter_text=chapter_text,
                        source_catalog=_build_evidence_source_catalog(chapter_text),
                        character_names=character_names,
                    )
                )
                _validate_summary_authority_references(
                    payload=payload,
                    card=card,
                    contract=authority_selection,
                )
            quote_payload = _source_ref_payload_to_quote_payload(
                payload=payload,
                chapter_text=chapter_text,
                source_catalog=source_catalog,
            )
            _validate_core_state_candidate_identities(
                payload=quote_payload,
                card=card,
            )
            validated_summary = _payload_to_summary(
                payload=quote_payload,
                chapter_id=chapter_id,
                workspace_id=workspace_id,
                extraction_trigger=extraction_trigger,
                model_used=resolved_model,
                chapter_text=chapter_text,
                source_version_id=source_version_id,
            )
        except AuthorityBoundSelectionError:
            # Canonical ID selection is fail-closed and never converted into
            # model feedback/retry.  The same frozen request already supplied
            # the complete legal catalog.
            raise
        except ValueError as exc:
            raise ValueError(
                f"{exc}。evidence_source_id 只能逐字使用本次原文目录中已有的 ID；"
                "禁止大小写修复、模糊匹配、改写、拼接、offset 或猜测。"
                "正文没有单段证据时，请删除对应数组项；POV 或场景无证据时，"
                "请将事实字段与 source ID 一起置空。"
            ) from exc

    try:
        if use_evidence_source_catalog:
            _source_ref_payload, _attempts, model_used = run_with_schema_retry(
                agent_name="chapter_summary_extractor",
                stage="summary_source_ref_payload",
                system_text=system_text,
                initial_user_text=user_text,
                output_model=_ExtractorSourceRefPayload,
                llm_call=_llm_call,
                strip_format_noise=_strip_format_noise,
                post_validate=_validate_source_refs,
                max_attempts=schema_retry_max_attempts,
                non_retryable_exceptions=(
                    (
                        AuthorityBoundSelectionError,
                        *non_retryable_exceptions,
                    )
                    if authority_selection is not None
                    else non_retryable_exceptions
                ),
            )
        else:
            _quote_payload, _attempts, model_used = run_with_schema_retry(
                agent_name="chapter_summary_extractor",
                stage="summary_payload",
                system_text=system_text,
                initial_user_text=user_text,
                output_model=_ExtractorLLMPayload,
                llm_call=_llm_call,
                strip_format_noise=_strip_format_noise,
                post_validate=_validate_exact_evidence,
                max_attempts=schema_retry_max_attempts,
            )
    except SchemaRetryExhausted as exc:
        _log.warning(
            "chapter_summary_extractor_schema_retry_exhausted",
            chapter_id=chapter_id,
            workspace_id=workspace_id,
            attempts=exc.attempts,
            error_type=type(exc.root_exc).__name__,
            error_message=str(exc.root_exc),
        )
        if uses_hy3_creative_delta(resolved_client):
            raise
        return None
    except Exception as exc:  # noqa: BLE001 — LLM transport 仍 fail-open
        if uses_hy3_creative_delta(resolved_client) or isinstance(
            exc, non_retryable_exceptions
        ):
            raise
        _log.warning(
            "chapter_summary_extractor_llm_failed_open",
            chapter_id=chapter_id,
            workspace_id=workspace_id,
            error_type=type(exc).__name__,
            error_message=str(exc),
        )
        return None

    # post_validate 成功才可能返回；保留防御性 fail-open，避免未来 helper
    # 契约变化让未经 exact quote 校验的 payload 落账。
    if validated_summary is None:
        _log.warning(
            "chapter_summary_evidence_validation_failed_open",
            chapter_id=chapter_id,
            workspace_id=workspace_id,
            error_type="MissingValidatedSummary",
            error_message="schema retry 返回但 exact evidence 结果缺失",
        )
        if uses_hy3_creative_delta(resolved_client):
            raise RuntimeError(
                "exact Hy3 summary delta 缺少已验证的确定性组装结果"
            )
        return None
    summary = validated_summary.model_copy(update={"model_used": model_used})

    _log.info(
        "chapter_summary_extractor_completed",
        chapter_id=chapter_id,
        workspace_id=workspace_id,
        extraction_trigger=extraction_trigger,
        summary_length=len(summary.summary_text),
        key_events_count=len(summary.key_events),
        interactions_count=len(summary.character_interactions),
        unfinished_count=len(summary.unfinished_actions),
        model_used=model_used,
        evidence_wire=(
            "source_ref_delta" if use_evidence_source_catalog else "quote_payload"
        ),
    )
    return summary


def _build_evidence_source_catalog(
    chapter_text: str,
) -> tuple[_EvidenceSource, ...]:
    """按原始 offset 确定性切句，生成大小写敏感、不可猜测的稳定 source ID。"""

    sources: list[_EvidenceSource] = []

    def _append_span(start: int, end: int) -> None:
        while start < end and chapter_text[start].isspace():
            start += 1
        while end > start and chapter_text[end - 1].isspace():
            end -= 1
        if start >= end:
            return
        if not any(char.isalnum() for char in chapter_text[start:end]):
            return
        sources.append(
            _EvidenceSource(
                source_id=f"E{len(sources) + 1:04d}",
                start=start,
                end=end,
            )
        )

    cursor = 0
    for match in _SOURCE_BOUNDARY_RE.finditer(chapter_text):
        _append_span(cursor, match.end())
        cursor = match.end()
    _append_span(cursor, len(chapter_text))
    return tuple(sources)


def _render_evidence_source_catalog(
    *,
    chapter_text: str,
    source_catalog: tuple[_EvidenceSource, ...],
) -> str:
    """把原序 exact slice 渲染为 JSONL；不让 Provider 计算 offset。"""

    return "\n".join(
        json.dumps(
            {
                "source_id": source.source_id,
                "text": chapter_text[source.start : source.end],
            },
            ensure_ascii=False,
        )
        for source in source_catalog
    )


def _source_ref_payload_to_quote_payload(
    *,
    payload: _ExtractorSourceRefPayload,
    chapter_text: str,
    source_catalog: tuple[_EvidenceSource, ...],
) -> _ExtractorLLMPayload:
    """exact lookup source ID，再把原文切片装回既有 quote payload。"""

    sources_by_id = {source.source_id: source for source in source_catalog}
    unknown_refs: list[str] = []

    def _quote(source_id: str, field: str, index: int) -> str:
        source = sources_by_id.get(source_id)
        if source is None:
            unknown_refs.append(f"{field}[{index}]={source_id!r}")
            return ""
        return chapter_text[source.start : source.end]

    scalar_pairs = (
        (
            "pov_character",
            payload.pov_character,
            payload.pov_evidence_source_id,
        ),
        (
            "scene_setting",
            payload.scene_setting,
            payload.scene_setting_evidence_source_id,
        ),
    )
    for field, fact, source_id in scalar_pairs:
        if bool(fact) != bool(source_id):
            unknown_refs.append(
                f"{field}[0] 事实字段与 evidence_source_id 必须同时为空或同时存在"
            )

    data = payload.model_dump()
    data["pov_evidence_quote"] = (
        _quote(payload.pov_evidence_source_id, "pov_character", 0)
        if payload.pov_evidence_source_id
        else ""
    )
    data["scene_setting_evidence_quote"] = (
        _quote(payload.scene_setting_evidence_source_id, "scene_setting", 0)
        if payload.scene_setting_evidence_source_id
        else ""
    )
    data.pop("pov_evidence_source_id")
    data.pop("scene_setting_evidence_source_id")

    for field in (
        "key_events",
        "character_interactions",
        "unfinished_actions",
        "character_states",
        "established_facts",
        "revealed_mechanics",
        "core_state_candidates",
    ):
        converted: list[dict] = []
        for index, item in enumerate(data[field]):
            source_id = item.pop("evidence_source_id")
            item["evidence_quote"] = _quote(source_id, field, index)
            converted.append(item)
        data[field] = converted

    if unknown_refs:
        raise ValueError("无效 evidence_source_id: " + "; ".join(unknown_refs))
    return _ExtractorLLMPayload.model_validate(data)


def _build_user_message(
    *,
    card: ChapterCard,
    chapter_text: str,
    chapter_id: str,
    workspace_id: str,
    source_catalog: tuple[_EvidenceSource, ...] = (),
    use_evidence_source_catalog: bool = False,
    authority_selection: AuthoritySelectionContract | None = None,
    character_names: dict[str, str] | None = None,
    current_core_states: dict[str, str] | None = None,
) -> str:
    """user message:章节卡核心字段 + 正文 + marker。

    包含 ``chapter_id`` / ``workspace_id`` / ``chapter_number`` / 标题 / 视角 /
    场景 / 出场角色 / 关联伏笔,让 LLM 抽取时知道章节卡层面的"计划",输出与
    之对齐的"实际落地"摘要。

    末尾追加 marker(P1-B-22 协议),MockLLMClient 据此返合规 fixture。
    """
    character_names = dict(character_names or {})
    identity_block = "\n".join(
        f"  - {identifier}: {name}"
        for identifier, name in sorted(character_names.items())
    ) or "  - (未提供姓名映射；不得按出场顺序重编 ID)"
    related_characters_block = (
        ", ".join(card.related_characters) if card.related_characters else "(无)"
    )
    related_threads_block = (
        ", ".join(card.related_plot_threads) if card.related_plot_threads else "(无)"
    )
    planned_core_transitions = (
        "\n".join(
            "  - "
            f"{item.state_key}: {item.from_value or '(未建立)'} → {item.to_value}"
            for item in card.core_state_transitions
        )
        if card.core_state_transitions
        else "  - (无)"
    )
    must_achieve_block = (
        "\n".join(f"  - {item}" for item in card.must_achieve) or "  - (无)"
    )
    forbidden_block = (
        "\n".join(f"  - {item}" for item in card.forbidden) or "  - (无)"
    )
    legal_object_ids = _stable_object_ids_from_card(card)
    legal_object_id_block = (
        "、".join(legal_object_ids) if legal_object_ids else "(未声明代码型物件 ID)"
    )
    context_block = (
        "## 章节元数据\n\n"
        f"- 章节 ID(chapter_id):{chapter_id}\n"
        f"- 工作区 ID(workspace_id):{workspace_id}\n"
        f"- 卷内章序号(chapter_number):{card.chapter_number}\n"
        f"- 标题:{card.title}\n"
        f"- 视角角色(pov_character):{card.pov_character}\n"
        f"- 场景设置(setting):{card.setting}\n"
        f"- 出场角色(related_characters):{related_characters_block}\n"
        f"- 权威角色 ID 与姓名（全文所有角色引用共用，禁止重新编号）:\n{identity_block}\n"
        f"- 关联伏笔(related_plot_threads):{related_threads_block}\n"
        "- 本章必达(must_achieve):\n"
        f"{must_achieve_block}\n"
        "- 本章禁止(forbidden):\n"
        f"{forbidden_block}\n"
        f"- 下一章种子(next_chapter_seed):{card.next_chapter_seed}\n"
        f"- 章节卡中的合法代码型物件稳定 ID:{legal_object_id_block}\n"
        "  object 核心状态候选只能逐字使用上述 typed/transition "
        "ID；不得从 title/setting/must/forbidden/seed 或正文名词重建新身份。\n"
        "  possession=物理持有，control=决策/授权控制，access=当前可操作"
        "目标；三义必须分开，value 只写角色 stable ID。holder 为历史读取"
        "字段，新候选禁止。\n"
        "- 本章获准核心状态变化、首次登记或关系再次确认（只作证据查找目标）:\n"
        "  from_value 为空表示尚未登记，不表示正文必须先发生移交；from=to "
        "表示关系确认。请逐项检查正文是否明确支持当前关系，支持时也应提名 "
        "core_state_candidates，并引用原文证据；不得因没有状态变化而省略。"
        "正文不支持时保持缺失，不得把卡片计划当成已发生事实或编造证据。\n"
        f"{planned_core_transitions}\n"
        + CORE_STATE_RELATION_EVIDENCE_GUIDANCE + "\n"
    )
    if current_core_states is not None:
        context_block += (
            "\n## 本章开始前已登记的核心状态\n\n"
            + json.dumps(current_core_states, ensure_ascii=False, sort_keys=True)
            + "\n这些是只读现状，不是待输出清单或正文证据。旧伤的日常症状、"
            "身份的另一种说法等，只记入普通 character_states/established_facts，"
            "没有真实状态变化时不必再次提名 core_state_candidates。若正文只是确认"
            "已有值，可逐字复用其 value，不要用扩写或换词制造状态变化。只有本章"
            "确实发生变化才提名不同值；没有卡片许可仍须如实记录真实变化，不能"
            "抄当前值、丢弃冲突或把旧伤描述当作新伤来规避检查。"
            "卡片显式列出的迁移/关系确认仍须逐项找正文证据；所有证据只取本章正文。\n"
        )
    if use_evidence_source_catalog:
        if authority_selection is not None:
            text_block = (
                authority_selection.render_prompt_catalog()
                + "\n\n- evidence_sources 按 final.md 原序覆盖非空正文；只返回 "
                "stable_id，不要抄写 semantic，不要输出 offset。\n"
                "- core_state_candidates[].entity_type 必须与同名 catalog 配对；"
                "关系属性 value 只能使用 characters stable_id。"
            )
        else:
            rendered_catalog = _render_evidence_source_catalog(
                chapter_text=chapter_text,
                source_catalog=source_catalog,
            )
            text_block = (
                "## 章节正文证据目录\n\n"
                "下列条目按 final.md 原序覆盖全部非空正文。只能返回 source_id；"
                "不要抄写 text，不要输出 offset。\n\n"
                + (rendered_catalog or "(无可引用的非空原文片段)")
            )
    else:
        text_block = "## 章节正文\n\n" + chapter_text
    return append_role_marker(
        f"{context_block}\n\n{text_block}",
        role="chapter_summary_extractor",
    )


def _stable_object_ids_from_card(card: ChapterCard) -> list[str]:
    """从 typed 承重源和显式迁移取可复用物件 ID。

    不读 title/setting/must/正文中的高频名词，因而普通杯子、沙粒与
    工具不会因出现次数升为稳定身份。
    """

    values = {
        item.entity_id
        for item in card.core_state_transitions
        if item.entity_type == "object"
    }
    if card.dramatic_source is not None:
        values.update(
            item.object_id
            for item in card.dramatic_source.beat_contract.load_bearing_objects
        )
    return sorted(values)


def _chapter_summary_authority_contract(
    *,
    card: ChapterCard,
    chapter_text: str,
    source_catalog: tuple[_EvidenceSource, ...],
    character_names: dict[str, str] | None = None,
) -> AuthoritySelectionContract:
    """Freeze legal people/entity/source IDs for the source-ref delta."""

    character_ids = sorted({card.pov_character, *card.related_characters})
    character_entries = [
        AuthorityCatalogEntry(
            stable_id=character_id,
            semantic=(
                f"name={(character_names or {}).get(character_id, '未提供')}; "
                + (
                    "approved ChapterCard POV and related character"
                    if character_id == card.pov_character
                    else "approved ChapterCard related character"
                )
            ),
        )
        for character_id in character_ids
    ]

    object_semantics: dict[str, dict[str, Any]] = {
        object_id: {} for object_id in _stable_object_ids_from_card(card)
    }
    if card.dramatic_source is not None:
        for item in card.dramatic_source.beat_contract.load_bearing_objects:
            object_semantics.setdefault(item.object_id, {})[
                "load_bearing_contract"
            ] = {
                "label": item.label,
                "causal_role": item.causal_role,
                "causal_requirement": item.causal_requirement,
                "relation_attributes": item.relation_attributes,
            }
    for transition in card.core_state_transitions:
        if transition.entity_type != "object":
            continue
        object_semantics.setdefault(transition.entity_id, {}).setdefault(
            "approved_transitions", []
        ).append(
            {
                "attribute": transition.attribute,
                "from_value": transition.from_value,
                "to_value": transition.to_value,
            }
        )
    object_entries = [
        AuthorityCatalogEntry(
            stable_id=object_id,
            semantic=json.dumps(
                semantic or {"source": "approved ChapterCard typed object"},
                ensure_ascii=False,
                sort_keys=True,
                separators=(",", ":"),
            ),
        )
        for object_id, semantic in object_semantics.items()
    ]
    source_entries = [
        AuthorityCatalogEntry(
            stable_id=source.source_id,
            semantic=chapter_text[source.start : source.end],
        )
        for source in source_catalog
    ]
    snapshot = AuthoritySnapshot.freeze(
        stage="chapter_summary_source_ref_delta",
        catalogs=(
            AuthorityCatalog.freeze(name="characters", entries=character_entries),
            AuthorityCatalog.freeze(
                name="evidence_sources", entries=source_entries
            ),
            AuthorityCatalog.freeze(name="objects", entries=object_entries),
        ),
        required_catalogs={"characters", "evidence_sources"},
    )
    return AuthoritySelectionContract(
        snapshot=snapshot,
        bindings=_SUMMARY_AUTHORITY_BINDINGS,
    )


def _validate_summary_authority_references(
    *,
    payload: _ExtractorSourceRefPayload,
    card: ChapterCard,
    contract: AuthoritySelectionContract,
) -> None:
    """Keep type pairing and conditional relation IDs outside wire dialect."""

    contract.validate_payload(payload.model_dump(mode="json"))
    character_ids = set(contract.catalog_ids("characters"))
    object_ids = set(contract.catalog_ids("objects"))
    invalid: list[str] = []
    if payload.pov_character and payload.pov_character != card.pov_character:
        invalid.append(
            "pov_character must be empty or equal approved ChapterCard POV:"
            f"{payload.pov_character!r}"
        )
    for index, candidate in enumerate(payload.core_state_candidates):
        allowed_entity_ids = (
            character_ids if candidate.entity_type == "character" else object_ids
        )
        if candidate.entity_id not in allowed_entity_ids:
            invalid.append(
                f"core_state_candidates[{index}].entity_type/entity_id="
                f"{candidate.entity_type}/{candidate.entity_id}"
            )
        if (
            candidate.attribute in CORE_STATE_RELATION_ATTRIBUTES
            and candidate.value not in character_ids
        ):
            invalid.append(
                f"core_state_candidates[{index}].value={candidate.value!r}"
            )
    if invalid:
        raise AuthorityBoundSelectionError(
            "ChapterSummary canonical reference type pairing failed: "
            + "; ".join(invalid)
        )


def _validate_core_state_candidate_identities(
    *,
    payload: _ExtractorLLMPayload,
    card: ChapterCard,
) -> None:
    """候选只能复用 typed 物件；关系必须精确命中获准 from→to。"""

    transition_by_key = {
        item.state_key: item for item in card.core_state_transitions
    }
    legal_code_ids = _stable_object_ids_from_card(card)
    invalid: list[str] = []
    invalid_relations: list[str] = []
    for candidate in payload.core_state_candidates:
        if candidate.entity_type != "object":
            continue
        if candidate.entity_id not in legal_code_ids:
            invalid.append(candidate.entity_id)
            continue
        if candidate.attribute in {"possession", "control", "access"}:
            state_key = make_state_key(
                entity_type=candidate.entity_type,
                entity_id=candidate.entity_id,
                attribute=candidate.attribute,
            )
            transition = transition_by_key.get(state_key)
            if transition is None or transition.to_value != candidate.value:
                invalid_relations.append(f"{state_key}={candidate.value}")
    if invalid:
        legal = "、".join(legal_code_ids) if legal_code_ids else "(无代码型 ID)"
        raise ValueError(
            "object 核心状态候选使用了章节卡未声明的 typed 物件:"
            f"{invalid}；合法稳定 ID={legal}。不得从自然文本重建别名"
        )
    if invalid_relations:
        raise ValueError(
            "object relation 候选与章节卡获准 state_key/to_value 不一致:"
            f"{invalid_relations}"
        )


def _parse_llm_payload(
    *,
    raw_text: str,
    model_used: str,
    chapter_id: str,
) -> _ExtractorLLMPayload:
    """剥代码块 / trailing 注释 → ``json.loads`` → Pydantic 校验。

    任一失败抛 ``ValueError``;caller 在 ``extract_chapter_summary`` except 兜
    底统一 fail-open 返 None。
    """
    text = _strip_format_noise(raw_text).strip()
    try:
        data = loads_with_quote_repair(
            text,
            agent_name="chapter_summary_extractor",
            model_used=model_used,
        )
    except json.JSONDecodeError as exc:
        raise ValueError(
            f"chapter_summary_extractor LLM 输出非合法 JSON"
            f"(model={model_used} chapter={chapter_id}): {raw_text[:200]!r}"
        ) from exc
    try:
        return _ExtractorLLMPayload.model_validate(data)
    except ValidationError as exc:
        raise ValueError(
            f"chapter_summary_extractor LLM 输出 schema 不符"
            f"(model={model_used} chapter={chapter_id}): {exc.errors()}"
        ) from exc


def _strip_format_noise(text: str) -> str:
    """剥 trailing HTML 注释 + ``` / ```json 包裹(沿用 reviewer / memory_extractor 同型)。"""
    while True:
        stripped = _TRAILING_HTML_COMMENT_RE.sub("", text)
        if stripped == text:
            break
        text = stripped
    match = _CODE_FENCE_RE.match(text)
    if match is not None:
        return match.group("body")
    return text


def _payload_to_summary(
    *,
    payload: _ExtractorLLMPayload,
    chapter_id: str,
    workspace_id: str,
    extraction_trigger: ExtractionTrigger,
    model_used: str,
    chapter_text: str,
    source_version_id: int | None,
) -> ChapterSummary:
    """把 LLM 输出转成带逐条原文证据和可复算 hash 的 R4 小账本。"""

    records: list[ContinuityEvidenceRecord] = []

    def _record(
        *,
        field: str,
        index: int,
        fact_text: str,
        quote: str,
        is_immediate: bool = False,
    ) -> ContinuityEvidenceRecord:
        record = make_evidence_record(
            field=field,
            index=index,
            fact_text=fact_text,
            evidence_quote=quote,
            chapter_text=chapter_text,
            is_immediate=is_immediate,
        )
        records.append(record)
        return record

    pov_character = payload.pov_character or None
    scene_setting = payload.scene_setting or None
    if pov_character is not None:
        _record(
            field="pov_character",
            index=0,
            fact_text=pov_character,
            quote=payload.pov_evidence_quote,
        )
    if scene_setting is not None:
        _record(
            field="scene_setting",
            index=0,
            fact_text=scene_setting,
            quote=payload.scene_setting_evidence_quote,
        )

    key_events: list[dict] = []
    for index, event in enumerate(payload.key_events):
        evidence = _record(
            field="key_events",
            index=index,
            fact_text=event.description,
            quote=event.evidence_quote,
        )
        data = event.model_dump()
        data["evidence_fingerprint"] = evidence.evidence_fingerprint
        key_events.append(data)

    character_interactions: list[dict] = []
    for index, interaction in enumerate(payload.character_interactions):
        fact_text = f"{interaction.char_a}<→>{interaction.char_b}:{interaction.dynamic}"
        evidence = _record(
            field="character_interactions",
            index=index,
            fact_text=fact_text,
            quote=interaction.evidence_quote,
        )
        data = interaction.model_dump()
        data["evidence_fingerprint"] = evidence.evidence_fingerprint
        character_interactions.append(data)

    unfinished_actions: list[str] = []
    for index, action in enumerate(payload.unfinished_actions):
        _record(
            field="unfinished_actions",
            index=index,
            fact_text=action.description,
            quote=action.evidence_quote,
            is_immediate=action.is_immediate,
        )
        unfinished_actions.append(action.description)

    character_states: list[dict] = []
    for index, state in enumerate(payload.character_states):
        fact_text = f"{state.character_id}:{state.state}"
        evidence = _record(
            field="character_states",
            index=index,
            fact_text=fact_text,
            quote=state.evidence_quote,
        )
        data = state.model_dump()
        data["evidence_fingerprint"] = evidence.evidence_fingerprint
        character_states.append(data)

    established_facts: list[str] = []
    for index, fact in enumerate(payload.established_facts):
        _record(
            field="established_facts",
            index=index,
            fact_text=fact.fact,
            quote=fact.evidence_quote,
        )
        established_facts.append(fact.fact)

    revealed_mechanics: list[str] = []
    for index, mechanic in enumerate(payload.revealed_mechanics):
        _record(
            field="revealed_mechanics",
            index=index,
            fact_text=mechanic.mechanic,
            quote=mechanic.evidence_quote,
        )
        revealed_mechanics.append(mechanic.mechanic)

    core_state_candidates: list[dict] = []
    for index, candidate in enumerate(payload.core_state_candidates):
        state_key = make_state_key(
            entity_type=candidate.entity_type,
            entity_id=candidate.entity_id,
            attribute=candidate.attribute,
        )
        fact_text = f"{state_key}={candidate.value}"
        evidence = _record(
            field="core_state_candidates",
            index=index,
            fact_text=fact_text,
            quote=candidate.evidence_quote,
        )
        core_candidate = CoreStateCandidate(
            state_key=state_key,
            entity_type=candidate.entity_type,
            entity_id=candidate.entity_id,
            attribute=candidate.attribute,
            value=candidate.value,
            objective=candidate.objective,
            long_lived=candidate.long_lived,
            book_bearing=candidate.book_bearing,
            ambiguous=candidate.ambiguous,
            evidence_quote=candidate.evidence_quote,
            evidence_fingerprint=evidence.evidence_fingerprint,
        )
        core_state_candidates.append(core_candidate.model_dump(mode="json"))

    version_id = source_version_id or 0
    if version_id < 0:
        raise ValueError("source_version_id 不能为负数")
    source_hash = sha256_text(chapter_text)
    final_tail = raw_final_tail(chapter_text)
    ledger_hash = compute_continuity_ledger_hash(
        chapter_id=chapter_id,
        workspace_id=workspace_id,
        summary_text=payload.summary_text,
        pov_character=pov_character,
        scene_setting=scene_setting,
        key_events=key_events,
        character_interactions=character_interactions,
        unfinished_actions=unfinished_actions,
        character_states=character_states,
        established_facts=established_facts,
        revealed_mechanics=revealed_mechanics,
        core_state_candidates=_core_candidates_for_ledger_hash(
            core_state_candidates
        ),
        raw_tail=final_tail,
        source_version_id=version_id,
        source_text_hash=source_hash,
        evidence_records=records,
    )
    now = datetime.now(UTC)
    return ChapterSummary(
        chapter_id=chapter_id,
        workspace_id=workspace_id,
        summary_text=payload.summary_text,
        pov_character=pov_character,
        scene_setting=scene_setting,
        key_events=key_events,
        character_interactions=character_interactions,
        unfinished_actions=unfinished_actions,
        character_states=character_states,
        established_facts=established_facts,
        revealed_mechanics=revealed_mechanics,
        core_state_candidates=core_state_candidates,
        raw_final_tail=final_tail,
        source_version_id=version_id,
        source_text_hash=source_hash,
        evidence_records=[record.model_dump(mode="json") for record in records],
        ledger_hash=ledger_hash,
        validated_at=None,
        extraction_trigger=extraction_trigger,
        model_used=model_used,
        extracted_at=now,
    )


def extract_and_persist_chapter_summary_safely(
    *,
    workspace_id: str,
    chapter_id: str,
    trigger: ExtractionTrigger,
    paths: object,
    session_factory: object,
    llm_client: LLMClient | None = None,
    llm_model: str | None = None,
    router: ModelRouter | None = None,
    prompts_dir: Path | None = None,
    vector_store: object | None = None,
    source_version_id: int | None = None,
    schema_retry_max_attempts: int = 1,
    use_evidence_source_catalog: bool = False,
    chapter_text_snapshot: str | None = None,
    chapter_card_snapshot: ChapterCard | None = None,
    non_retryable_exceptions: tuple[type[Exception], ...] = (),
) -> bool:
    """完整 hook 调用链:读 final.md + 读 ChapterCard → 调 LLM 抽取 → 落 ORM(+ Chroma)。

    供 ``ChapterService._combined``(reviewer_pass 路径)与
    ``ChapterRewindService.force_pass``(force_pass 路径)调用。**全程 fail-open
    + structlog**,任一步失败不抛(对齐 A2-03 ``sync_completed_chapters_to_meta``
    + force_pass mark_final swallow 同型),**不阻塞章节 approved 主流程**。
    W3.5-C 起返回 bool:``True`` 表示摘要 SQLite 落账成功且向量写入未报错,
    ``False`` 表示跳过或失败;旧调用方不看返回值,fail-open 行为不变。

    职责:
    1. 用 ``paths`` 拼 final.md 路径,读出正文(失败 swallow + warn 返)。
    2. 用 ``paths`` 拼 card JSON 路径,读出 ChapterCard(失败 swallow + warn 返)。
    3. 调 ``extract_chapter_summary`` LLM(返 None 即 fail-open,跳过落 ORM)。
    4. 用 ``session_factory(workspace_id)`` 开 session,upsert ``ChapterSummaryORM``
       行(已存在则 update 字段;commit/rollback 边界在本函数内)。
    5. **v1.1 wave 3 s6(E1)**:``vector_store`` 给定时,把本章摘要写进 Chroma
       ``"memories"`` collection(``entity_type="chapter_summary"``),供 Writer
       检索"相关历史章节摘要"维度。**独立 try/except fail-open** —— SQLite 已
       commit,Chroma 写失败只 warn 不回滚(向量库是检索增强,非真相源)。
    6. 任一步异常 → 不抛,只 log warn。

    :param workspace_id: 工作区 ID。
    :param chapter_id: 章节 ID。
    :param trigger: ``reviewer_pass`` / ``force_pass``。
    :param paths: ``WorkspacePaths`` 实例(签名 ``object`` 避顶层 import 循环,
        实际类型由调用方传入)。
    :param session_factory: ``Callable[[workspace_id], Session]`` factory(签名
        ``object`` 同理)。``None`` → 静默跳过(单测 / CLI legacy 兼容)。
    :param llm_client: LLM 客户端(legacy 路径必传;router 路径忽略)。``None``
        且 router 也 None → 静默跳过(LLM 未注入 = hook disable)。
    :param llm_model: LLM 模型名(legacy 路径必传)。
    :param router: ``ModelRouter``(给定时优先于 legacy)。
    :param prompts_dir: 模板目录;``None`` 取 ``settings.prompts_dir``。
    :param vector_store: v1.1 wave 3 s6(E1)加。``VectorStore`` 实例(签名
        ``object`` 避顶层 import 循环);``None`` → 跳过 Chroma 写(仅落 SQLite,
        向后兼容单测 / CLI legacy 路径)。生产路径由 ``ChapterService`` /
        ``ChapterRewindService`` 透传 ``vector_store_factory(workspace_id)``。
    :param source_version_id: R4 同步路径传最终正文版本；legacy/force-pass 可空，
        但空值账本没有 ready 资格。
    :param use_evidence_source_catalog: 严格 R4 路径启用 v3 source ID 协议；
        legacy hook 保持 False。
    :param chapter_text_snapshot: 可选的已校验正文快照。给定时 Provider 输入只用
        此快照，不在调用前再次读取 ``final.md``；默认 ``None`` 保持 legacy
        路径原有读文件行为。
    :param chapter_card_snapshot: 可选的已校验章节卡快照。给定时 Provider 输入只
        用此对象，不在调用前再次读取 card JSON；默认 ``None`` 保持 legacy 行为。
    """
    if session_factory is None:
        _log.debug(
            "chapter_summary_hook_skipped",
            reason="session_factory_none",
            chapter_id=chapter_id,
            workspace_id=workspace_id,
        )
        return False
    if router is None and (llm_client is None or llm_model is None):
        _log.debug(
            "chapter_summary_hook_skipped",
            reason="llm_not_injected",
            chapter_id=chapter_id,
            workspace_id=workspace_id,
        )
        return False

    # 1) 读 final.md + ChapterCard(失败 swallow + warn)
    try:
        # 局部 import 避顶层循环:agents 不依赖 services / workspace_paths
        from src.core.models.chapter_card import ChapterCard as _ChapterCard

        if chapter_text_snapshot is None:
            final_md_path = paths.chapter_version_file(  # type: ignore[attr-defined]
                chapter_id, "final.md"
            )
            if not final_md_path.is_file():
                _log.warning(
                    "chapter_summary_hook_no_final_md",
                    chapter_id=chapter_id,
                    workspace_id=workspace_id,
                    final_md_path=str(final_md_path),
                )
                return False
            chapter_text = final_md_path.read_text(encoding="utf-8")
        else:
            chapter_text = chapter_text_snapshot

        if chapter_card_snapshot is None:
            card_path = (  # type: ignore[attr-defined]
                paths.cards_dir / f"{chapter_id}.json"
            )
            if not card_path.is_file():
                _log.warning(
                    "chapter_summary_hook_no_card",
                    chapter_id=chapter_id,
                    workspace_id=workspace_id,
                    card_path=str(card_path),
                )
                return False
            card = _ChapterCard.model_validate_json_storage(
                card_path.read_text(encoding="utf-8")
            )
        else:
            card = chapter_card_snapshot
        from sqlalchemy import select
        from src.core.db.models import CharacterORM

        identity_session = session_factory(workspace_id)  # type: ignore[operator]
        try:
            character_names = dict(identity_session.execute(
                select(CharacterORM.character_id, CharacterORM.name).where(
                    CharacterORM.workspace_id == workspace_id,
                    CharacterORM.character_id.in_(
                        {card.pov_character, *card.related_characters}
                    ),
                )
            ).all())
        finally:
            identity_session.close()
        current_core_states = None
        if card.core_state_contract_version == 1:
            from src.services.core_state_service import CoreStateService
            from src.core.models.chapter_card import chapter_seq_from_id
            from sqlalchemy.orm import Session

            projection = CoreStateService(
                session_factory=cast(Callable[[str], Session], session_factory)
            ).load_projection(
                workspace_id, before_chapter=chapter_seq_from_id(chapter_id)
            )
            if projection.projection_hash != card.core_state_projection_hash:
                raise ValueError("摘要输入的核心状态与批准卡投影不一致")
            current_core_states = {
                fact.state_key: fact.value for fact in projection.facts
            }
    except Exception as exc:  # noqa: BLE001
        _log.warning(
            "chapter_summary_hook_read_failed",
            chapter_id=chapter_id,
            workspace_id=workspace_id,
            error_type=type(exc).__name__,
            error_message=str(exc),
        )
        return False

    # 2) 调 LLM 抽取(返 None 即 fail-open,跳过落 ORM)
    summary = extract_chapter_summary(
        chapter_text=chapter_text,
        card=card,
        chapter_id=chapter_id,
        workspace_id=workspace_id,
        extraction_trigger=trigger,
        router=router,
        client=llm_client,
        model=llm_model,
        prompts_dir=prompts_dir,
        source_version_id=source_version_id,
        schema_retry_max_attempts=schema_retry_max_attempts,
        character_names=character_names,
        current_core_states=current_core_states,
        use_evidence_source_catalog=use_evidence_source_catalog,
        non_retryable_exceptions=non_retryable_exceptions,
    )
    if summary is None:
        # extract_chapter_summary 内部已 log warn
        return False

    # 3) 落 ORM(upsert ChapterSummaryORM 行)
    try:
        # 局部 import 避顶层循环:agents 不应顶层依赖 db.models
        from src.core.db.models import ChapterSummaryORM

        session = session_factory(workspace_id)  # type: ignore[operator]
        try:
            existing = session.get(ChapterSummaryORM, chapter_id)
            now = datetime.now(UTC)
            if existing is None:
                session.add(
                    ChapterSummaryORM(
                        chapter_id=summary.chapter_id,
                        workspace_id=summary.workspace_id,
                        summary_text=summary.summary_text,
                        pov_character=summary.pov_character,
                        scene_setting=summary.scene_setting,
                        key_events=summary.key_events,
                        character_interactions=summary.character_interactions,
                        unfinished_actions=summary.unfinished_actions,
                        character_states=summary.character_states,
                        established_facts=summary.established_facts,
                        revealed_mechanics=summary.revealed_mechanics,
                        core_state_candidates=summary.core_state_candidates,
                        raw_final_tail=summary.raw_final_tail,
                        source_version_id=summary.source_version_id,
                        source_text_hash=summary.source_text_hash,
                        evidence_records=summary.evidence_records,
                        ledger_hash=summary.ledger_hash,
                        validated_at=summary.validated_at,
                        extraction_trigger=summary.extraction_trigger,
                        model_used=summary.model_used,
                        created_at=now,
                        updated_at=now,
                    )
                )
            else:
                # 已存在(rewind 后重写 / 多次 force_pass) → update 全字段 +
                # updated_at,保留 created_at(首次抽取时间)
                existing.summary_text = summary.summary_text
                existing.pov_character = summary.pov_character
                existing.scene_setting = summary.scene_setting
                existing.key_events = summary.key_events
                existing.character_interactions = summary.character_interactions
                existing.unfinished_actions = summary.unfinished_actions
                existing.character_states = summary.character_states
                existing.established_facts = summary.established_facts
                existing.revealed_mechanics = summary.revealed_mechanics
                existing.core_state_candidates = summary.core_state_candidates
                existing.raw_final_tail = summary.raw_final_tail
                existing.source_version_id = summary.source_version_id
                existing.source_text_hash = summary.source_text_hash
                existing.evidence_records = summary.evidence_records
                existing.ledger_hash = summary.ledger_hash
                existing.validated_at = summary.validated_at
                existing.extraction_trigger = summary.extraction_trigger
                existing.model_used = summary.model_used
                existing.updated_at = now
            session.commit()
        except Exception:
            session.rollback()
            raise
        finally:
            session.close()
        _log.info(
            "chapter_summary_hook_persisted",
            chapter_id=chapter_id,
            workspace_id=workspace_id,
            trigger=trigger,
            summary_length=len(summary.summary_text),
        )
    except Exception as exc:  # noqa: BLE001
        _log.warning(
            "chapter_summary_hook_persist_failed",
            chapter_id=chapter_id,
            workspace_id=workspace_id,
            error_type=type(exc).__name__,
            error_message=str(exc),
        )
        return False

    # 4) v1.1 wave 3 s6(E1):落 Chroma —— 摘要入 "memories" collection
    #    (entity_type="chapter_summary"),供 Writer 检索"相关历史章节摘要"维度。
    #    独立 fail-open:SQLite 已 commit(真相源已落),Chroma 写失败只 warn。
    #    只有 SQLite 落库成功(未走上面 return)才到这里。
    if vector_store is not None:
        return _persist_summary_to_vector_store(
            vector_store=vector_store,
            summary=summary,
            chapter_id=chapter_id,
            workspace_id=workspace_id,
        )
    return True


def _summary_from_orm(row: object) -> ChapterSummary:
    """把 ``ChapterSummaryORM`` 行还原成 R4 Pydantic 小账本。"""

    return ChapterSummary(
        chapter_id=row.chapter_id,  # type: ignore[attr-defined]
        workspace_id=row.workspace_id,  # type: ignore[attr-defined]
        summary_text=row.summary_text,  # type: ignore[attr-defined]
        pov_character=row.pov_character,  # type: ignore[attr-defined]
        scene_setting=row.scene_setting,  # type: ignore[attr-defined]
        key_events=list(row.key_events or []),  # type: ignore[attr-defined]
        character_interactions=list(  # type: ignore[attr-defined]
            row.character_interactions or []
        ),
        unfinished_actions=list(row.unfinished_actions or []),  # type: ignore[attr-defined]
        character_states=list(row.character_states or []),  # type: ignore[attr-defined]
        established_facts=list(row.established_facts or []),  # type: ignore[attr-defined]
        revealed_mechanics=list(row.revealed_mechanics or []),  # type: ignore[attr-defined]
        core_state_candidates=list(  # type: ignore[attr-defined]
            getattr(row, "core_state_candidates", None) or []
        ),
        raw_final_tail=row.raw_final_tail,  # type: ignore[attr-defined]
        source_version_id=row.source_version_id,  # type: ignore[attr-defined]
        source_text_hash=row.source_text_hash,  # type: ignore[attr-defined]
        evidence_records=list(row.evidence_records or []),  # type: ignore[attr-defined]
        ledger_hash=row.ledger_hash,  # type: ignore[attr-defined]
        validated_at=row.validated_at,  # type: ignore[attr-defined]
        extraction_trigger=row.extraction_trigger,  # type: ignore[attr-defined]
        model_used=row.model_used,  # type: ignore[attr-defined]
        extracted_at=row.created_at,  # type: ignore[attr-defined]
    )


def _expected_evidence_facts(
    summary: ChapterSummary,
) -> dict[tuple[str, int], str]:
    """由结构字段重建证据覆盖集合，防止只写 quote 不对应事实。"""

    expected: dict[tuple[str, int], str] = {}
    if summary.pov_character:
        expected[("pov_character", 0)] = summary.pov_character
    if summary.scene_setting:
        expected[("scene_setting", 0)] = summary.scene_setting
    for index, event in enumerate(summary.key_events):
        expected[("key_events", index)] = str(event.get("description", ""))
    for index, interaction in enumerate(summary.character_interactions):
        expected[("character_interactions", index)] = (
            f"{interaction.get('char_a', '')}<→>{interaction.get('char_b', '')}:"
            f"{interaction.get('dynamic', '')}"
        )
    for index, action in enumerate(summary.unfinished_actions):
        expected[("unfinished_actions", index)] = action
    for index, state in enumerate(summary.character_states):
        expected[("character_states", index)] = (
            f"{state.get('character_id', '')}:{state.get('state', '')}"
        )
    for index, fact in enumerate(summary.established_facts):
        expected[("established_facts", index)] = fact
    for index, mechanic in enumerate(summary.revealed_mechanics):
        expected[("revealed_mechanics", index)] = mechanic
    for index, raw in enumerate(summary.core_state_candidates):
        candidate = CoreStateCandidate.model_validate(raw)
        expected[("core_state_candidates", index)] = (
            f"{candidate.state_key}={candidate.value}"
        )
    if any(not text for text in expected.values()):
        raise ContinuityLedgerValidationError("连续性小账本存在空结构事实")
    return expected


def _validate_summary_against_sources(
    *,
    summary: ChapterSummary,
    chapter_text: str,
    version_text: str,
    card: ChapterCard,
    workspace_id: str,
    chapter_id: str,
    expected_version_id: int | None,
    require_validated_at: bool,
) -> None:
    """校验来源 version/hash、card 身份、逐条 quote 与可复算 ledger hash。"""

    if summary.chapter_id != chapter_id or summary.workspace_id != workspace_id:
        raise ContinuityLedgerValidationError(
            "连续性小账本 chapter/workspace 身份不匹配"
        )
    if card.chapter_id != chapter_id or card.workspace_id != workspace_id:
        raise ContinuityLedgerValidationError("章节卡 chapter/workspace 身份不匹配")
    if (
        summary.pov_character is not None
        and summary.pov_character != card.pov_character
    ):
        raise ContinuityLedgerValidationError("小账本 POV 与章节卡不一致")
    if summary.source_version_id < 1:
        raise ContinuityLedgerValidationError("连续性小账本缺少有效 source_version_id")
    if (
        expected_version_id is not None
        and summary.source_version_id != expected_version_id
    ):
        raise ContinuityLedgerValidationError("连续性小账本 source_version_id 已陈旧")
    if chapter_text != version_text:
        raise ContinuityLedgerValidationError("final.md 与来源版本正文不一致")
    if summary.source_text_hash != sha256_text(chapter_text):
        raise ContinuityLedgerValidationError("连续性小账本 source_text_hash 已陈旧")
    if summary.raw_final_tail != raw_final_tail(chapter_text):
        raise ContinuityLedgerValidationError("连续性小账本 raw_final_tail 不匹配")
    if require_validated_at and summary.validated_at is None:
        raise ContinuityLedgerValidationError("连续性小账本尚未完成硬水位校验")

    try:
        evidence_records = [
            ContinuityEvidenceRecord.model_validate(item)
            for item in summary.evidence_records
        ]
        validate_evidence_records(
            records=evidence_records,
            chapter_text=chapter_text,
            expected_facts=_expected_evidence_facts(summary),
        )
    except ValueError as exc:
        raise ContinuityLedgerValidationError(str(exc)) from exc

    expected_hash = compute_continuity_ledger_hash(
        chapter_id=summary.chapter_id,
        workspace_id=summary.workspace_id,
        summary_text=summary.summary_text,
        pov_character=summary.pov_character,
        scene_setting=summary.scene_setting,
        key_events=summary.key_events,
        character_interactions=summary.character_interactions,
        unfinished_actions=summary.unfinished_actions,
        character_states=summary.character_states,
        established_facts=summary.established_facts,
        revealed_mechanics=summary.revealed_mechanics,
        core_state_candidates=_core_candidates_for_ledger_hash(
            summary.core_state_candidates
        ),
        raw_tail=summary.raw_final_tail,
        source_version_id=summary.source_version_id,
        source_text_hash=summary.source_text_hash,
        evidence_records=evidence_records,
    )
    if summary.ledger_hash != expected_hash:
        # 0031 以前的 ledger hash 没有 core_state_candidates 键；迁移默认 []
        # 时继续按旧算法验真，绝不把旧 workspace 伪装成已启用新合同。
        legacy_hash = (
            compute_continuity_ledger_hash(
                chapter_id=summary.chapter_id,
                workspace_id=summary.workspace_id,
                summary_text=summary.summary_text,
                pov_character=summary.pov_character,
                scene_setting=summary.scene_setting,
                key_events=summary.key_events,
                character_interactions=summary.character_interactions,
                unfinished_actions=summary.unfinished_actions,
                character_states=summary.character_states,
                established_facts=summary.established_facts,
                revealed_mechanics=summary.revealed_mechanics,
                raw_tail=summary.raw_final_tail,
                source_version_id=summary.source_version_id,
                source_text_hash=summary.source_text_hash,
                evidence_records=evidence_records,
                core_state_candidates=None,
            )
            if not summary.core_state_candidates
            else ""
        )
        if summary.ledger_hash != legacy_hash:
            raise ContinuityLedgerValidationError("连续性小账本 ledger_hash 不可复算")


def _load_and_validate_continuity_ledger(
    *,
    workspace_id: str,
    chapter_id: str,
    paths: object,
    session_factory: object,
    expected_version_id: int | None,
    require_validated_at: bool,
) -> ChapterSummary:
    """从 SQLite 加载并对当前 final/version/card 做全量确定性复核。"""

    if session_factory is None:
        raise ContinuityLedgerValidationError("连续性小账本缺少 session_factory")
    try:
        final_path = paths.chapter_version_file(  # type: ignore[attr-defined]
            chapter_id, "final.md"
        )
        chapter_text = final_path.read_text(encoding="utf-8")
        card_path = paths.cards_dir / f"{chapter_id}.json"  # type: ignore[attr-defined]
        card = ChapterCard.model_validate_json_storage(
            card_path.read_text(encoding="utf-8")
        )
    except Exception as exc:
        raise ContinuityLedgerValidationError(
            f"读取连续性小账本来源失败: {type(exc).__name__}: {exc}"
        ) from exc

    from src.core.db.models import ChapterSummaryORM

    session = session_factory(workspace_id)  # type: ignore[operator]
    try:
        row = session.get(ChapterSummaryORM, chapter_id)
        if row is None:
            raise ContinuityLedgerValidationError(
                f"连续性小账本不存在: chapter_id={chapter_id}"
            )
        summary = _summary_from_orm(row)
    finally:
        session.close()

    try:
        version_path = paths.chapter_version_file(  # type: ignore[attr-defined]
            chapter_id, f"v{summary.source_version_id}.md"
        )
        version_text = version_path.read_text(encoding="utf-8")
    except Exception as exc:
        raise ContinuityLedgerValidationError(
            f"读取连续性来源版本失败: {type(exc).__name__}: {exc}"
        ) from exc

    _validate_summary_against_sources(
        summary=summary,
        chapter_text=chapter_text,
        version_text=version_text,
        card=card,
        workspace_id=workspace_id,
        chapter_id=chapter_id,
        expected_version_id=expected_version_id,
        require_validated_at=require_validated_at,
    )
    return summary


def extract_and_persist_continuity_ledger(
    *,
    workspace_id: str,
    chapter_id: str,
    source_version_id: int,
    paths: object,
    session_factory: object,
    llm_client: LLMClient | None = None,
    llm_model: str | None = None,
    router: ModelRouter | None = None,
    prompts_dir: Path | None = None,
    trigger: ExtractionTrigger = "reviewer_pass",
    chapter_text_snapshot: str | None = None,
    chapter_card_snapshot: ChapterCard | None = None,
    non_retryable_exceptions: tuple[type[Exception], ...] = (),
) -> ChapterSummary:
    """同步抽取、落库并验证 R4 小账本；失败必须向主流程抛出。

    ``trigger`` 只记录批准来源；``reviewer_pass`` 与 ``force_pass`` 共享完全
    相同的 final/version/card/quote/hash 硬水位。``chapter_text_snapshot``
    给 checkpoint recovery 使用：Provider 只接收调用前已与 DB、vN、final
    三方核对过的正文，避免校验后文件再变更造成 TOCTOU。
    """

    ok = extract_and_persist_chapter_summary_safely(
        workspace_id=workspace_id,
        chapter_id=chapter_id,
        trigger=trigger,
        paths=paths,
        session_factory=session_factory,
        llm_client=llm_client,
        llm_model=llm_model,
        router=router,
        prompts_dir=prompts_dir,
        vector_store=None,
        source_version_id=source_version_id,
        schema_retry_max_attempts=CONTINUITY_LEDGER_SCHEMA_MAX_ATTEMPTS,
        use_evidence_source_catalog=True,
        chapter_text_snapshot=chapter_text_snapshot,
        chapter_card_snapshot=chapter_card_snapshot,
        non_retryable_exceptions=non_retryable_exceptions,
    )
    if not ok:
        raise ContinuityLedgerValidationError("连续性小账本抽取或持久化失败")

    summary = _load_and_validate_continuity_ledger(
        workspace_id=workspace_id,
        chapter_id=chapter_id,
        paths=paths,
        session_factory=session_factory,
        expected_version_id=source_version_id,
        require_validated_at=False,
    )

    from src.core.db.models import ChapterSummaryORM

    validated_at = datetime.now(UTC)
    session = session_factory(workspace_id)  # type: ignore[operator]
    try:
        row = session.get(ChapterSummaryORM, chapter_id)
        if row is None:
            raise ContinuityLedgerValidationError("连续性小账本校验后行丢失")
        row.validated_at = validated_at
        session.commit()
    except Exception as exc:
        session.rollback()
        if isinstance(exc, ContinuityLedgerValidationError):
            raise
        raise ContinuityLedgerValidationError(
            f"连续性小账本 validated_at 持久化失败: {type(exc).__name__}: {exc}"
        ) from exc
    finally:
        session.close()
    return summary.model_copy(update={"validated_at": validated_at})


def load_validated_continuity_ledger(
    *,
    workspace_id: str,
    chapter_id: str,
    paths: object,
    session_factory: object,
    expected_version_id: int | None = None,
) -> ChapterSummary:
    """加载已 ready 资格的小账本，并对当前来源重新复核。"""

    return _load_and_validate_continuity_ledger(
        workspace_id=workspace_id,
        chapter_id=chapter_id,
        paths=paths,
        session_factory=session_factory,
        expected_version_id=expected_version_id,
        require_validated_at=True,
    )


def continuity_ledger_provenance(
    summary: ChapterSummary,
) -> ContinuityLedgerProvenance:
    """从已验证小账本构造供下一张卡冻结的 provenance。"""

    if summary.validated_at is None:
        raise ContinuityLedgerValidationError("未验证小账本不能生成 provenance")
    return ContinuityLedgerProvenance(
        chapter_id=summary.chapter_id,
        source_version_id=summary.source_version_id,
        source_text_hash=summary.source_text_hash,
        ledger_hash=summary.ledger_hash,
    )


def build_provenance_bound_continuity_capsule(
    summary: ChapterSummary,
    *,
    frozen_provenance: ContinuityLedgerProvenance | None,
) -> ProvenanceBoundContinuityCapsule:
    """从已验证 ledger 构造本次调用专用 capsule。

    这里故意不做 IO。生产调用方先用 ``load_validated_continuity_ledger``
    对 final/version/card/SQLite 完整验真；本 helper 再复算结构 coverage、quote
    fingerprint 与 ledger hash，保证离线 fixture 或误传对象不能开启短 Prompt。
    任一项不可证都抛 ``ContinuityLedgerValidationError``，调用方必须在首次
    Provider 请求前选择原完整 Prompt，禁止短版失败后 retry。
    """

    if summary.validated_at is None:
        raise ContinuityLedgerValidationError("未验证小账本不能构造 continuity capsule")
    current = continuity_ledger_provenance(summary)
    try:
        validate_frozen_provenance(
            frozen=frozen_provenance,
            current=current,
        )
        evidence_records = tuple(
            ContinuityEvidenceRecord.model_validate(item)
            for item in summary.evidence_records
        )
        expected_facts = _expected_evidence_facts(summary)
        actual_by_key: dict[tuple[str, int], ContinuityEvidenceRecord] = {}
        for record in evidence_records:
            key = (record.field, record.index)
            if key in actual_by_key:
                raise ValueError(
                    f"连续性证据重复: {record.field}[{record.index}]"
                )
            actual_by_key[key] = record
            if record.evidence_fingerprint != sha256_text(record.evidence_quote):
                raise ValueError(
                    f"{record.field}[{record.index}] evidence_fingerprint 不匹配"
                )
        if set(actual_by_key) != set(expected_facts):
            missing = sorted(set(expected_facts) - set(actual_by_key))
            extra = sorted(set(actual_by_key) - set(expected_facts))
            raise ValueError(
                f"连续性证据覆盖不完整: missing={missing} extra={extra}"
            )
        for key, fact_text in expected_facts.items():
            if actual_by_key[key].fact_text != fact_text:
                raise ValueError(
                    f"{key[0]}[{key[1]}] fact_text 与结构字段不一致"
                )
    except ValueError as exc:
        raise ContinuityLedgerValidationError(str(exc)) from exc

    def _compute_hash(
        core_state_candidates: list[dict[str, Any]] | None,
    ) -> str:
        return compute_continuity_ledger_hash(
            chapter_id=summary.chapter_id,
            workspace_id=summary.workspace_id,
            summary_text=summary.summary_text,
            pov_character=summary.pov_character,
            scene_setting=summary.scene_setting,
            key_events=summary.key_events,
            character_interactions=summary.character_interactions,
            unfinished_actions=summary.unfinished_actions,
            character_states=summary.character_states,
            established_facts=summary.established_facts,
            revealed_mechanics=summary.revealed_mechanics,
            raw_tail=summary.raw_final_tail,
            source_version_id=summary.source_version_id,
            source_text_hash=summary.source_text_hash,
            evidence_records=list(evidence_records),
            core_state_candidates=core_state_candidates,
        )

    expected_hash = _compute_hash(
        _core_candidates_for_ledger_hash(summary.core_state_candidates)
    )
    legacy_hash = (
        _compute_hash(None)
        if not summary.core_state_candidates
        else ""
    )
    if summary.ledger_hash == legacy_hash and legacy_hash != expected_hash:
        # 历史 ledger 仍由 load/validate 路径兼容读取，但 B0 的短版选择不把
        # “缺少 current hash shape”伪装成逐字段可证。旧现场一律完整 Prompt/Q4。
        raise ContinuityLedgerValidationError(
            "legacy 连续性小账本不启用 Provider-0 短版"
        )
    if summary.ledger_hash != expected_hash:
        raise ContinuityLedgerValidationError("连续性小账本 ledger_hash 不可复算")

    def _canonical(value: object) -> str:
        return json.dumps(
            value,
            ensure_ascii=False,
            sort_keys=True,
            separators=(",", ":"),
        )

    capsule_payload = {
        "provenance": current.model_dump(mode="json"),
        "summary_text": summary.summary_text,
        "pov_character": summary.pov_character,
        "scene_setting": summary.scene_setting,
        "raw_final_tail": summary.raw_final_tail,
        "unfinished_actions": summary.unfinished_actions,
        "key_events": summary.key_events,
        "character_interactions": summary.character_interactions,
        "character_states": summary.character_states,
        "established_facts": summary.established_facts,
        "revealed_mechanics": summary.revealed_mechanics,
        "core_state_candidates": summary.core_state_candidates,
        "evidence_records": [
            record.model_dump(mode="json") for record in evidence_records
        ],
    }
    return ProvenanceBoundContinuityCapsule(
        provenance=current,
        summary_text=summary.summary_text,
        pov_character=summary.pov_character,
        scene_setting=summary.scene_setting,
        raw_final_tail=summary.raw_final_tail,
        unfinished_actions=tuple(summary.unfinished_actions),
        key_events_json=_canonical(summary.key_events),
        character_interactions_json=_canonical(summary.character_interactions),
        character_states_json=_canonical(summary.character_states),
        established_facts_json=_canonical(summary.established_facts),
        revealed_mechanics_json=_canonical(summary.revealed_mechanics),
        core_state_candidates_json=_canonical(summary.core_state_candidates),
        evidence_records=evidence_records,
        capsule_hash=sha256_text(_canonical(capsule_payload)),
    )


def persist_continuity_ledger_vector_safely(
    *,
    workspace_id: str,
    chapter_id: str,
    paths: object,
    session_factory: object,
    vector_store: object,
    on_vector_write_started: Callable[[], None] | None = None,
) -> bool:
    """后台只把已验证 SQLite 小账本写向量，不重新调用摘要 LLM。"""

    try:
        summary = load_validated_continuity_ledger(
            workspace_id=workspace_id,
            chapter_id=chapter_id,
            paths=paths,
            session_factory=session_factory,
        )
    except ContinuityLedgerValidationError as exc:
        _log.warning(
            "continuity_ledger_vector_skipped_invalid",
            workspace_id=workspace_id,
            chapter_id=chapter_id,
            error_message=str(exc),
        )
        return False
    return _persist_summary_to_vector_store(
        vector_store=vector_store,
        summary=summary,
        chapter_id=chapter_id,
        workspace_id=workspace_id,
        on_vector_write_started=on_vector_write_started,
    )


def _compose_chapter_summary_doc(summary: ChapterSummary) -> str:
    """v1.1 wave 3 s6(E1):拼章节摘要的 Chroma document 文本。

    以 ``summary_text`` 为主体,追加 ``key_events`` 各条 description 增强语义召回
    (Writer 按"主线 + 出场角色 + 关联伏笔"查询时,key_events 描述能提升相关章节
    摘要的命中率)。对齐 ``memory_extractor._compose_*_doc`` 的 " | " 拼接风格。
    """
    parts: list[str] = [summary.summary_text]
    for ev in summary.key_events:
        desc = ev.get("description", "") if isinstance(ev, dict) else ""
        if desc:
            parts.append(desc)
    return " | ".join(parts)


def _persist_summary_to_vector_store(
    *,
    vector_store: object,
    summary: ChapterSummary,
    chapter_id: str,
    workspace_id: str,
    on_vector_write_started: Callable[[], None] | None = None,
) -> bool:
    """v1.1 wave 3 s6(E1):把单章摘要写进 Chroma ``"memories"`` collection。

    - Chroma id = ``f"chapter_summary-{chapter_id}"``;同 ID upsert,rewind 后重抽
      自然覆盖,不堆叠多版本。
    - metadata 五字段对齐 ``memory_extractor._make_metadata`` 约定
      (``entity_type`` / ``entity_id`` / ``chapter_id`` / ``workspace_id`` /
      ``created_at``),让 retriever 可按 ``where={"entity_type": "chapter_summary"}``
      过滤,也让 ``VectorStore.soft_delete_by_chapter_id``(rewind 撤回)按
      ``chapter_id`` 一并软删本章摘要向量。
    - **fail-open**:任一异常只 log warn 不抛(SQLite 真相源已 commit;向量库是
      检索增强层,写失败不影响章节主流程,下次该章重抽时再补写)。
    """
    try:
        # 局部 import 避顶层循环 + 避免 ``object`` 形参的静态类型噪声
        from src.core.memory.vector import VectorStore

        if not isinstance(vector_store, VectorStore):
            return True
        created_at_iso = datetime.now(UTC).isoformat()
        if on_vector_write_started is not None:
            on_vector_write_started()
        vector_store.add(
            DEFAULT_COLLECTION,
            ids=[f"chapter_summary-{chapter_id}"],
            documents=[_compose_chapter_summary_doc(summary)],
            metadatas=[
                {
                    "entity_type": "chapter_summary",
                    "entity_id": chapter_id,
                    "chapter_id": chapter_id,
                    "workspace_id": workspace_id,
                    "created_at": created_at_iso,
                }
            ],
        )
        _log.info(
            "chapter_summary_vector_persisted",
            chapter_id=chapter_id,
            workspace_id=workspace_id,
        )
        return True
    except Exception as exc:  # noqa: BLE001 — fail-open,向量库是检索增强层
        _log.warning(
            "chapter_summary_vector_persist_failed",
            chapter_id=chapter_id,
            workspace_id=workspace_id,
            error_type=type(exc).__name__,
            error_message=str(exc),
        )
        return False


__all__ = [
    "ChapterSummary",
    "ContinuityLedgerValidationError",
    "ExtractionTrigger",
    "build_provenance_bound_continuity_capsule",
    "continuity_ledger_provenance",
    "extract_and_persist_continuity_ledger",
    "extract_and_persist_chapter_summary_safely",
    "extract_chapter_summary",
    "load_validated_continuity_ledger",
    "persist_continuity_ledger_vector_safely",
]
