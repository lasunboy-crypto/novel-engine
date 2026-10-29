"""setting_consistency LLM-as-judge validator(v1.1 wave 6 session 3,B1 治本核心)。

职责:在 writer **写完正文后**(审稿期),把"本章正文"与"设定契约"(world
不变规则 + 出场角色固定身份)比对,**只报正文违反契约的点**(冲突才报警,没说
不报警 = 护留白)。治 B1 30 章长跑(luo-clean-v2)暴露的 A 类设定一致性 bug:

- **A1** 沈舟现实身份被写成"刑警"(大纲明确=心理医生,刑警是陆星野身份)
- **A2** 绣春刀 / 飞鱼服当实物出现在现实场景(违反"纯意识穿梭、肉体留现实"基准)
- **A3** 林晓妹妹肉体"在 vs 不在"自相矛盾
- **A4** 结案病历患者张冠李戴
- **A6** 双界机制"意识 vs 物理"全书摇摆

判据(对齐 [docs/plan/v1_1_wave6_design.md](../../../docs/plan/v1_1_wave6_design.md) §0
两原则 + §2 B1 行):本章正文是否与**已确立的设定契约**给出**互斥**答案 / 违反
硬规则。**护留白第一原则**:留白 / 开放 / 没交代 ≠ 违反;只有正文与契约**实质
冲突**才算 violation。

约束分层(§0 原则二)
====================

本 agent 是**下游兜底独立 LLM-judge**(旁移),writer 正文生成完全不加负担。
代码粗筛由 w6-s2 已落的 ``check_profession_title_blacklist``(职业称谓黑名单邻接
检测)承担,本 agent 专攻语义类违反(绣春刀穿帮 / 肉体在不在 / 病历张冠李戴 /
机制摇摆),两者互补。

R2 结构化分层
==============

LLM 只提出候选；代码必须逐条回查 ``world.unique_rules`` 原文与正文证据。只有
权威 contract、``world.unique_rules``、直接互斥、双 quote 可回查且无歧义的候选
才能成为 HARD。身份、动机、氛围、力量体系推断与文学留白一律 SOFT。

设计要点(沿用 mechanism_consistency_validator 同型架构,不发明新哲学)
==================================================================

1. **1 次综合 LLM 调用**:本章正文 + 契约一次性看完综合判定。
2. **沿用 *_validator 路由**:agent_name ``"setting_consistency_validator"`` →
   ``agent_routing.setting_consistency_validator``(config.yaml,cloud_qwen_max_thinking
   thinking on:设定冲突判定需深度斟酌,对齐 mechanism_consistency_validator 同型);
   未显式配则 ``resolve_routing`` fallback 走 reviewer profile。
3. **失败边界**:权威 contract 下传输/解析失败有界重试，耗尽后抛 typed technical
   error，不能沿用 pass；无权威 outline 的 legacy/import 路径保留显式降级。
4. **skip 边界**:契约为空(无 unique_rules 且无出场角色身份)/ 正文为空 → skip
   (无判定对象),不发起 LLM 调用。
5. **agent_role marker = ``setting_consistency_validator``(无 chapter_id):
   MockLLMClient 识别 marker 返默认"无违反"JSON,既有审稿 happy 路径不回归。
"""

from __future__ import annotations

import json
import re
from dataclasses import dataclass, field
from pathlib import Path
from typing import Literal, cast

import structlog
from pydantic import BaseModel, ConfigDict, Field, ValidationError, field_validator

from src.config import settings
from src.core.agents.structured_handoff import HandoffError, handoff_user_text
from src.core.agents._json_contract import loads_with_quote_repair
from src.core.agents._prompt import (
    append_role_marker,
    load_prompt_template,
    resolve_routing,
)
from src.core.agents._structured_output import prepare_strict_json_output
from src.core.agents.creative_delta import uses_experimental_pipeline
from src.core.llm.base import LLMClient, Message
from src.core.models.character import IdentityFacet
from src.core.models.core_state import CoreStateProjection, render_core_state_capsule
from src.core.models.review import (
    ConstraintResult,
    ConstraintViolation,
    stable_constraint_id,
)
from src.core.models.setting_contract import SettingContract
from src.core.routing.router import ModelRouter
from src.core.rules.prose_hard_contract import ExecutableConstraint

_log = structlog.get_logger(__name__)

_VALIDATOR_PROMPT_FILENAME = "setting_consistency_validator_v2.md"
_PREWRITING_VALIDATOR_PROMPT_FILENAME = "prewriting_semantic_validator_v6.md"
_FROZEN_PREWRITING_VALIDATOR_PROMPT_FILENAMES = (
    "prewriting_semantic_validator_v5.md",
    "prewriting_semantic_validator_v4.md",
    "prewriting_semantic_validator_v3.md",
    "prewriting_semantic_validator_v2.md",
    "prewriting_semantic_validator_v1.md",
)
SettingValidationScope = Literal[
    "chapter_text",
    "outline_plot",
    "volume_plans",
    "mini_plan",
    "chapter_card",
]
_ConstraintViolationDomain = Literal[
    "world.unique_rules",
    "world.setting",
    "world.power_system",
    "character.identity",
    "character.motivation",
    "character.voice",
    "character.profession",
    "chapter.internal",
    "chapter.continuity",
    "chapter.timeline",
    "chapter.style",
    "chapter.dedup",
    "chapter.ending",
]

# 默认生成参数:综合判定对采样稳定性敏感,温度低;输出含 N 个 {rule + evidence},
# token 上限适中。router 路径下由 profile 决定。
_DEFAULT_TEMPERATURE = 0.3
_DEFAULT_MAX_TOKENS = 1000

# 本章正文判定窗口上限(自决项):设定违反可能出现在正文任意处(绣春刀中段出现 /
# 沈舟刑警随处),故比 continuity 的 1000 字大;但封顶控成本(多数章 1500-4000 字,
# 通常全量)。
_CHAPTER_MAX_CHARS = 6000
_PREWRITING_MAX_CHARS = 50_000
SETTING_CONSISTENCY_AUTHORITATIVE_MAX_ATTEMPTS = 2
_AUTHORITATIVE_MAX_ATTEMPTS = SETTING_CONSISTENCY_AUTHORITATIVE_MAX_ATTEMPTS
_LEGACY_SOURCE_PATH = "legacy/import"
_CHARACTER_SOURCE_PATH = "runtime/character_records"
_CHAPTER_SOURCE_PATH = "chapter_text"

_CODE_FENCE_RE = re.compile(
    r"^\s*```(?:json)?\s*\n(?P<body>.*?)\n```\s*$",
    re.DOTALL,
)
_TRAILING_HTML_COMMENT_RE = re.compile(
    r"\s*<!--.*?-->\s*$",
    re.DOTALL,
)
_NUMBERED_RULE_MARKER_RE = re.compile(
    r"(^|[。！？!?；;\n])\s*\d+[.、．]\s*",
    re.MULTILINE,
)
_RULE_UNIT_SPLIT_RE = re.compile(r"[。！？!?；;\n]+")

ValidationSource = Literal[
    "authoritative_world_contract",
    "authoritative_core_state",
    "authoritative_contracts",
    "legacy_import",
]


@dataclass(frozen=True)
class CharacterIdentityRef:
    """单个出场角色的固定身份契约(name + 多情境身份侧面)。"""

    character_name: str
    facets: tuple[IdentityFacet, ...] = ()


@dataclass(frozen=True)
class CharacterMotivationRef:
    """单个出场角色的核心动机契约(name + 已确立的核心目标 / 驱动力)。

    A5(v1.1 wave 6 session 4):即兴自洽校验用 —— 正文里 writer 即兴加的概念**深化**
    动机合法,**与已确立动机直接矛盾**(反派突然变好人 / 主角核心目标无故反转)才报。
    """

    character_name: str
    motivations: tuple[str, ...] = ()


@dataclass(frozen=True)
class SettingConsistencyResult:
    """validator 输出；``validation_source`` 明示权威或 legacy/import 降级。"""

    passed: bool
    violations: list[ConstraintViolation] = field(default_factory=list)
    constraint_results: list[ConstraintResult] = field(default_factory=list)
    llm_calls_count: int = 0
    skipped: bool = False
    fail_open: bool = False
    validation_source: ValidationSource = "legacy_import"
    resolved_recheck_constraint_ids: tuple[str, ...] = ()
    # 只阻止当前实验草稿自相矛盾，不提升世界普通字段的全书 HARD 级别。
    authoring_conflicts: list[ConstraintViolation] = field(default_factory=list)


def prewriting_blockers(result: SettingConsistencyResult) -> list[ConstraintViolation]:
    return [v for v in result.violations if v.severity == "hard"] + result.authoring_conflicts


def _authoring_conflicts(
    *, candidates: list[_ViolationItem], violations: list[ConstraintViolation],
    contract: SettingContract, text: str, enabled: bool,
) -> list[ConstraintViolation]:
    """仅接纳同次判断中无歧义、两端原文可回查的普通机制直接冲突。"""
    if not enabled or not contract.has_provenance:
        return []
    sources = {"world.setting": contract.setting, "world.power_system": contract.power_system}
    return [
        violation.model_copy(update={"ambiguity": None})
        for candidate, violation in zip(candidates, violations, strict=True)
        if candidate.domain in sources and candidate.directly_contradictory
        and not (candidate.ambiguity or "").strip()
        and candidate.rule_quote in sources[candidate.domain]
        and candidate.evidence_quote in text
    ]


class _ViolationItem(BaseModel):
    """LLM 输出候选；最终 severity 仍由代码资格校验决定。"""

    model_config = ConfigDict(extra="forbid")

    domain: Literal[
        "world.unique_rules",
        "world.setting",
        "world.power_system",
        "character.identity",
        "character.motivation",
        "chapter.internal",
        "core_state",
        "book_clock",
    ]
    rule_quote: str = Field(..., min_length=1, max_length=1000)
    evidence_quote: str = Field(..., min_length=1, max_length=400)
    severity: Literal["hard", "soft"]
    directly_contradictory: bool
    ambiguity: str | None = Field(default=None, max_length=200)

    @field_validator("rule_quote", "evidence_quote")
    @classmethod
    def _quote_must_not_be_blank(cls, value: str) -> str:
        stripped = value.strip()
        if not stripped:
            raise ValueError("quote 不能为空白")
        return stripped


class _RequiredRecheckItem(BaseModel):
    """章节卡修复后对既有阻断身份的逐项复核。"""

    model_config = ConfigDict(extra="forbid")

    constraint_id: str = Field(..., min_length=64, max_length=64)
    status: Literal["resolved", "still_conflict", "ambiguous"]
    resolution_basis: Literal[
        "trigger_removed",
        "rule_requirement_satisfied",
        "conflicting_prohibition_removed",
        "unresolved",
    ]
    evidence_quotes: list[str] = Field(..., min_length=1, max_length=3)
    reason: str = Field(..., min_length=1, max_length=500)

    @field_validator("evidence_quotes")
    @classmethod
    def _recheck_quotes_must_not_be_blank(cls, values: list[str]) -> list[str]:
        stripped = [value.strip() for value in values]
        if any(not value for value in stripped):
            raise ValueError("recheck evidence_quote 不能为空白")
        return stripped


class _SettingLLMPayload(BaseModel):
    """LLM 输出的中间模型；定向修复时还须逐项返回 ``rechecks``。"""

    model_config = ConfigDict(extra="forbid")

    violations: list[_ViolationItem] = Field(default_factory=list)
    rechecks: list[_RequiredRecheckItem] = Field(default_factory=list)


@dataclass(frozen=True)
class SettingConstraintRecheck:
    """代码复核后的定向修复结论；不持久化，不改变原约束身份。"""

    constraint_id: str
    status: Literal["resolved", "still_conflict", "ambiguous"]
    resolution_basis: Literal[
        "trigger_removed",
        "rule_requirement_satisfied",
        "conflicting_prohibition_removed",
        "unresolved",
    ]
    evidence_quotes: tuple[str, ...]
    reason: str


class SettingConsistencyTechnicalError(RuntimeError):
    """权威 setting judge setup/传输/解析失败且不能安全降级。"""

    def __init__(self, *, stage: str, attempts: int, cause: Exception) -> None:
        self.stage = stage
        self.attempts = attempts
        self.cause_type = type(cause).__name__
        super().__init__(
            "权威设定一致性校验技术失败:"
            f"stage={stage},attempts={attempts},cause={self.cause_type}"
        )


def validate_setting_consistency(
    *,
    chapter_text: str,
    contract: SettingContract,
    character_identities: list[CharacterIdentityRef] | None = None,
    character_motivations: list[CharacterMotivationRef] | None = None,
    chapter_id: str,
    workspace_id: str | None = None,
    router: ModelRouter | None = None,
    client: LLMClient | None = None,
    model: str | None = None,
    max_tokens: int = _DEFAULT_MAX_TOKENS,
    temperature: float = _DEFAULT_TEMPERATURE,
    prompts_dir: Path | None = None,
    validation_scope: SettingValidationScope = "chapter_text",
    core_state_projection: CoreStateProjection | None = None,
    core_state_constraint: ExecutableConstraint | None = None,
    book_clock_constraint: ExecutableConstraint | None = None,
    required_rechecks: list[ConstraintViolation] | None = None,
) -> SettingConsistencyResult:
    """1 次综合 LLM 调用,判定本章正文是否违反设定 / 身份 / 动机契约(冲突才报,护留白)。

    :param chapter_text: 本章正文(取前 ``_CHAPTER_MAX_CHARS`` 字判定)。
    :param contract: ``SettingContract``(world 不变规则)。
    :param character_identities: 出场角色固定身份 list(只含有 ``identity_facets``
        的多情境角色);``None`` / 空 → 不按身份判定。
    :param character_motivations: 出场角色核心动机 list(A5 即兴自洽,v1.1 w6-s4);
        ``None`` / 空 → 不按动机判定。
    :param chapter_id: 章节 ID,落 structlog 字段。
    :param workspace_id: 工作区 ID,落 structlog 字段(可选)。
    :param router: ``ModelRouter`` 实例;给定时按
        ``router.route("setting_consistency_validator")`` 取 client + model + 生成参数。
    :param client: legacy 路径 LLM 客户端;``router=None`` 时必传。
    :param model: legacy 路径模型名;``router=None`` 时必传。
    :param max_tokens: legacy 路径 token 上限。
    :param temperature: legacy 路径温度。
    :param prompts_dir: 提示词模板根目录;``None`` 时取 ``settings.prompts_dir``。
    :param validation_scope: ``chapter_text`` 沿用 R2 正文判定；``outline_plot`` /
        ``volume_plans`` / ``mini_plan`` / ``chapter_card`` 使用写前语义 prompt，
        但仍复用同一 ``ConstraintViolation`` 与 HARD 资格函数。
    :return: ``SettingConsistencyResult``。
    """
    identities = character_identities or []
    motivations = character_motivations or []
    recheck_requirements = required_rechecks or []
    if recheck_requirements and validation_scope != "chapter_card":
        raise SettingConsistencyTechnicalError(
            stage="setup",
            attempts=0,
            cause=ValueError("定向冲突复核只允许用于 chapter_card 写前合同"),
        )
    if recheck_requirements and not contract.has_provenance:
        raise SettingConsistencyTechnicalError(
            stage="setup",
            attempts=0,
            cause=ValueError("定向冲突复核必须绑定权威 world provenance"),
        )
    if (core_state_projection is None) != (core_state_constraint is None):
        raise SettingConsistencyTechnicalError(
            stage="setup",
            attempts=0,
            cause=ValueError("core_state projection/constraint 必须成对提供"),
        )
    if book_clock_constraint is not None and core_state_projection is None:
        raise SettingConsistencyTechnicalError(
            stage="setup",
            attempts=0,
            cause=ValueError("book_clock constraint 必须绑定 core_state projection"),
        )
    existing_call_has_subject = bool(
        not contract.is_empty or identities or motivations
    )
    if (
        (core_state_constraint is not None or book_clock_constraint is not None)
        and chapter_text.strip()
        and not existing_call_has_subject
    ):
        raise SettingConsistencyTechnicalError(
            stage="setup",
            attempts=0,
            cause=ValueError(
                "核心状态/时钟不能为原本会 skip 的 Setting Judge 新增 Provider 调用；"
                "请关闭本工作区开关或先建立权威 world/角色判定输入"
            ),
        )
    authoritative = (
        contract.has_provenance
        or core_state_constraint is not None
        or book_clock_constraint is not None
    )
    if contract.has_provenance and core_state_constraint is not None:
        validation_source: ValidationSource = "authoritative_contracts"
    elif contract.has_provenance:
        validation_source = "authoritative_world_contract"
    elif core_state_constraint is not None:
        validation_source = "authoritative_core_state"
    else:
        validation_source = "legacy_import"

    # skip 边界:契约 + 出场角色身份 + 角色动机都为空(无判定对象)/ 正文为空。
    if (
        contract.is_empty
        and not identities
        and not motivations
        and core_state_constraint is None
    ) or not chapter_text.strip():
        _log.debug(
            "setting_consistency_validator_skipped",
            chapter_id=chapter_id,
            workspace_id=workspace_id,
            reason=(
                "empty_chapter_text" if not chapter_text.strip() else "no_contract"
            ),
            validation_source=validation_source,
            failure_layer=validation_scope,
        )
        return SettingConsistencyResult(
            passed=True,
            skipped=True,
            llm_calls_count=0,
            validation_source=validation_source,
        )

    # setup 阶段失败:权威 contract 不能降级为 pass；legacy/import 显式 fail-open。
    try:
        resolved_client, resolved_model, complete_kwargs = resolve_routing(
            agent_name="setting_consistency_validator",
            router=router,
            client=client,
            model=model,
            legacy_kwargs={"max_tokens": max_tokens, "temperature": temperature},
        )
        template_root = (
            prompts_dir if prompts_dir is not None else settings.prompts_dir
        ).resolve()
        prompt_path = template_root / (
            _VALIDATOR_PROMPT_FILENAME
            if validation_scope == "chapter_text"
            else _PREWRITING_VALIDATOR_PROMPT_FILENAME
        )
        # 显式 prompts_dir 可能来自冻结活动快照。只有该路径缺少当前 v6 时，
        # 才按新到旧读取当时的 v5/v4/v3/v2/v1；生产默认目录缺 v6 必须继续按
        # authoritative setup failure 停止，不能静默退回旧判尺。
        if (
            validation_scope != "chapter_text"
            and prompts_dir is not None
            and not prompt_path.is_file()
        ):
            for frozen_filename in _FROZEN_PREWRITING_VALIDATOR_PROMPT_FILENAMES:
                frozen_path = template_root / frozen_filename
                if frozen_path.is_file():
                    prompt_path = frozen_path
                    break
        system_text = load_prompt_template(prompt_path)
        complete_kwargs = prepare_strict_json_output(
            client=resolved_client,
            model=resolved_model,
            output_model=_SettingLLMPayload,
            schema_name="setting_consistency",
            complete_kwargs=complete_kwargs,
        )
    except Exception as exc:  # noqa: BLE001 — 权威/legacy 在此分流
        if isinstance(exc, HandoffError):
            raise
        if authoritative:
            _log.error(
                "setting_consistency_validator_authoritative_setup_failed",
                chapter_id=chapter_id,
                workspace_id=workspace_id,
                error_type=type(exc).__name__,
                contract_hash=contract.source_hash,
                attempt=0,
                failure_layer=validation_scope,
            )
            raise SettingConsistencyTechnicalError(
                stage="setup", attempts=0, cause=exc
            ) from exc
        _log.warning(
            "setting_consistency_validator_legacy_setup_failed_open",
            chapter_id=chapter_id,
            workspace_id=workspace_id,
            error_type=type(exc).__name__,
            validation_source=validation_source,
            contract_hash=contract.source_hash,
            attempt=0,
            failure_layer=validation_scope,
        )
        return SettingConsistencyResult(
            passed=True,
            fail_open=True,
            llm_calls_count=0,
            validation_source=validation_source,
        )

    if not authoritative:
        _log.warning(
            "setting_consistency_validator_legacy_degraded",
            chapter_id=chapter_id,
            workspace_id=workspace_id,
            validation_source=validation_source,
            failure_layer=validation_scope,
        )

    max_chars = (
        _CHAPTER_MAX_CHARS
        if validation_scope == "chapter_text"
        else _PREWRITING_MAX_CHARS
    )
    checked_chapter_text = chapter_text[:max_chars]
    user_text = _build_user_message(
        chapter_text=checked_chapter_text,
        contract=contract,
        identities=identities,
        motivations=motivations,
        validation_scope=validation_scope,
        core_state_projection=core_state_projection,
        core_state_constraint=core_state_constraint,
        book_clock_constraint=book_clock_constraint,
        required_rechecks=recheck_requirements,
    )

    user_text = handoff_user_text(
        resolved_client, stage="setting_consistency_validator",
        data={"chapter_text": checked_chapter_text, "contract": contract, "identities": identities, "motivations": motivations, "validation_scope": validation_scope, "core_state_projection": core_state_projection, "core_state_constraint": core_state_constraint, "book_clock_constraint": book_clock_constraint, "required_rechecks": recheck_requirements}, original_task=user_text,
    )
    max_attempts = _AUTHORITATIVE_MAX_ATTEMPTS if authoritative else 1
    payload: _SettingLLMPayload | None = None
    materialized_rechecks: list[SettingConstraintRecheck] = []
    for attempt in range(1, max_attempts + 1):
        try:
            response = resolved_client.complete(
                messages=[
                    Message(role="system", content=system_text),
                    Message(role="user", content=user_text),
                ],
                model=resolved_model,
                **complete_kwargs,
            )
            payload = _parse_llm_payload(
                raw_text=response.content, model_used=response.model_used
            )
            materialized_rechecks = _materialize_required_rechecks(
                payload=payload,
                required_rechecks=recheck_requirements,
                checked_chapter_text=checked_chapter_text,
            )
            break
        except Exception as exc:  # noqa: BLE001 — 有界技术重试/legacy 分流
            if isinstance(exc, HandoffError):
                raise
            _log.warning(
                "setting_consistency_validator_attempt_failed",
                chapter_id=chapter_id,
                workspace_id=workspace_id,
                attempt=attempt,
                max_attempts=max_attempts,
                error_type=type(exc).__name__,
                validation_source=validation_source,
                contract_hash=contract.source_hash,
                failure_layer=validation_scope,
            )
            if attempt < max_attempts:
                continue
            if authoritative:
                raise SettingConsistencyTechnicalError(
                    stage="transport_or_parse",
                    attempts=attempt,
                    cause=exc,
                ) from exc
            return SettingConsistencyResult(
                passed=True,
                fail_open=True,
                llm_calls_count=attempt,
                validation_source=validation_source,
            )

    assert payload is not None  # loop 只会成功赋值、return 或 raise
    violations = [
        _materialize_constraint_violation(
            candidate=candidate,
            contract=contract,
            checked_chapter_text=checked_chapter_text,
            core_state_capsule=(
                render_core_state_capsule(
                    projection=core_state_projection,
                ).text
                + (
                    "\n" + core_state_constraint.rule_text
                    if core_state_constraint is not None
                    else ""
                )
                + (
                    "\n" + book_clock_constraint.rule_text
                    if book_clock_constraint is not None
                    else ""
                )
                if core_state_projection is not None
                else ""
            ),
            core_state_constraint=core_state_constraint,
            book_clock_constraint=book_clock_constraint,
        )
        for candidate in payload.violations
    ]
    authoring_conflicts = _authoring_conflicts(
        candidates=payload.violations, violations=violations, contract=contract,
        text=checked_chapter_text,
        enabled=(validation_scope != "chapter_text" and uses_experimental_pipeline(resolved_client)),
    )
    required_by_id = {
        violation.constraint_id: violation for violation in recheck_requirements
    }
    for recheck in materialized_rechecks:
        if recheck.status == "resolved":
            continue
        prior = required_by_id[recheck.constraint_id]
        violations.append(
            prior.model_copy(
                update={
                    "evidence_quote": recheck.evidence_quotes[0],
                    "severity": (
                        prior.severity
                        if recheck.status == "still_conflict"
                        else "soft"
                    ),
                    "ambiguity": recheck.reason,
                }
            )
        )
    if materialized_rechecks:
        _log.info(
            "setting_consistency_required_rechecks_completed",
            chapter_id=chapter_id,
            workspace_id=workspace_id,
            required_constraint_ids=[
                violation.constraint_id for violation in recheck_requirements
            ],
            resolved_constraint_ids=[
                item.constraint_id
                for item in materialized_rechecks
                if item.status == "resolved"
            ],
            unresolved_constraint_ids=[
                item.constraint_id
                for item in materialized_rechecks
                if item.status != "resolved"
            ],
            failure_layer=validation_scope,
        )
    hard_violations = [
        violation for violation in violations if violation.severity == "hard"
    ]
    constraint_results: list[ConstraintResult] = []
    if contract.has_provenance and contract.unique_rules.strip():
        source_path = contract.source_path
        rule_text = contract.unique_rules.strip()
        hard_world_violations = [
            violation
            for violation in hard_violations
            if violation.domain == "world.unique_rules"
        ]
        constraint_results.append(
            ConstraintResult(
                constraint_id=stable_constraint_id(
                    source_path=source_path,
                    domain="setting",
                    rule_text=rule_text,
                ),
                domain="setting",
                source_path=source_path,
                source_hash=contract.source_hash,
                rule_text=rule_text,
                status="fail" if hard_world_violations else "pass",
                details="；".join(
                    violation.evidence_quote for violation in hard_world_violations
                ),
            )
        )
    if core_state_constraint is not None:
        hard_core_violations = [
            violation
            for violation in hard_violations
            if violation.constraint_id == core_state_constraint.constraint_id
        ]
        constraint_results.append(
            core_state_constraint.as_constraint_result(
                status="fail" if hard_core_violations else "pass",
                details="；".join(
                    violation.evidence_quote for violation in hard_core_violations
                ),
            )
        )
    if book_clock_constraint is not None:
        hard_clock_violations = [
            violation
            for violation in hard_violations
            if violation.constraint_id == book_clock_constraint.constraint_id
        ]
        constraint_results.append(
            book_clock_constraint.as_constraint_result(
                status="fail" if hard_clock_violations else "pass",
                details="；".join(
                    violation.evidence_quote for violation in hard_clock_violations
                ),
            )
        )
    passed = not violations
    if passed:
        _log.debug(
            "setting_consistency_validator_passed",
            chapter_id=chapter_id,
            workspace_id=workspace_id,
            validation_source=validation_source,
            contract_hash=contract.source_hash,
            attempt=attempt,
            failure_layer=validation_scope,
        )
    else:
        _log.info(
            "setting_consistency_validator_violations",
            chapter_id=chapter_id,
            workspace_id=workspace_id,
            violation_count=len(violations),
            hard_count=sum(v.severity == "hard" for v in violations),
            violation_ids=[v.constraint_id for v in violations],
            validation_source=validation_source,
            contract_hash=contract.source_hash,
            attempt=attempt,
            failure_layer=validation_scope,
        )

    return SettingConsistencyResult(
        passed=passed,
        violations=violations,
        authoring_conflicts=authoring_conflicts,
        constraint_results=constraint_results,
        llm_calls_count=attempt,
        fail_open=False,
        validation_source=validation_source,
        resolved_recheck_constraint_ids=tuple(
            item.constraint_id
            for item in materialized_rechecks
            if item.status == "resolved"
        ),
    )


def _materialize_required_rechecks(
    *,
    payload: _SettingLLMPayload,
    required_rechecks: list[ConstraintViolation],
    checked_chapter_text: str,
) -> list[SettingConstraintRecheck]:
    """校验修复后正向闭环；缺项、错 ID 或不可回查证据均触发技术重试。"""

    if not required_rechecks:
        if payload.rechecks:
            raise ValueError("没有定向复核任务时不得虚构 rechecks")
        return []

    required_by_id = {
        violation.constraint_id: violation for violation in required_rechecks
    }
    if len(required_by_id) != len(required_rechecks):
        raise ValueError("required_rechecks constraint_id 重复")

    payload_by_id: dict[str, _RequiredRecheckItem] = {}
    for item in payload.rechecks:
        if item.constraint_id in payload_by_id:
            raise ValueError("rechecks constraint_id 重复")
        payload_by_id[item.constraint_id] = item
    if set(payload_by_id) != set(required_by_id):
        raise ValueError(
            "rechecks 必须逐项覆盖且只能覆盖 required constraint_id"
        )

    results: list[SettingConstraintRecheck] = []
    for required in required_rechecks:
        item = payload_by_id[required.constraint_id]
        if item.status == "resolved" and item.resolution_basis == "unresolved":
            raise ValueError("resolved recheck 不能使用 unresolved basis")
        if item.status != "resolved" and item.resolution_basis != "unresolved":
            raise ValueError("未解决 recheck 必须使用 unresolved basis")
        if any(quote not in checked_chapter_text for quote in item.evidence_quotes):
            raise ValueError("recheck evidence_quotes 无法在当前章节卡合同中回查")
        results.append(
            SettingConstraintRecheck(
                constraint_id=item.constraint_id,
                status=item.status,
                resolution_basis=item.resolution_basis,
                evidence_quotes=tuple(item.evidence_quotes),
                reason=item.reason.strip(),
            )
        )
    return results


def _materialize_constraint_violation(
    *,
    candidate: _ViolationItem,
    contract: SettingContract,
    checked_chapter_text: str,
    core_state_capsule: str = "",
    core_state_constraint: ExecutableConstraint | None = None,
    book_clock_constraint: ExecutableConstraint | None = None,
) -> ConstraintViolation:
    """把 LLM 候选转成可审计结构；HARD 资格只由本函数确定。"""
    is_core_state = candidate.domain == "core_state"
    is_book_clock = candidate.domain == "book_clock"
    matched_constraint = (
        core_state_constraint
        if is_core_state
        else book_clock_constraint
        if is_book_clock
        else None
    )
    if matched_constraint is not None:
        source_path = matched_constraint.source_path
        source_hash = matched_constraint.source_hash
    else:
        source_path, source_hash = _constraint_source(
            domain=candidate.domain,
            contract=contract,
        )
    rule_recoverable = (
        candidate.rule_quote in core_state_capsule
        if is_core_state or is_book_clock
        else candidate.rule_quote in contract.unique_rules
    )
    evidence_recoverable = candidate.evidence_quote in checked_chapter_text
    ambiguity_parts = (
        [candidate.ambiguity.strip()]
        if candidate.ambiguity and candidate.ambiguity.strip()
        else []
    )
    if candidate.domain not in {"world.unique_rules", "core_state", "book_clock"}:
        ambiguity_parts.append("domain_not_world_unique_rules")
    if not contract.has_provenance and not (is_core_state or is_book_clock):
        ambiguity_parts.append("legacy_source_without_provenance")
    if is_core_state and core_state_constraint is None:
        ambiguity_parts.append("core_state_constraint_missing")
    if is_book_clock and book_clock_constraint is None:
        ambiguity_parts.append("book_clock_constraint_missing")
    if not rule_recoverable:
        ambiguity_parts.append("rule_quote_not_found_in_source")
    if not evidence_recoverable:
        ambiguity_parts.append("evidence_quote_not_found_in_chapter")
    if not candidate.directly_contradictory:
        ambiguity_parts.append("not_directly_contradictory")

    canonical_rule_quote = candidate.rule_quote
    if candidate.domain == "world.unique_rules" and rule_recoverable:
        canonical_rule_quote, identity_ambiguity = _canonical_world_rule_quote(
            unique_rules=contract.unique_rules,
            rule_quote=candidate.rule_quote,
        )
        if identity_ambiguity is not None:
            ambiguity_parts.append(identity_ambiguity)

    hard_eligible = (
        candidate.severity == "hard"
        and (
            (candidate.domain == "world.unique_rules" and contract.has_provenance)
            or matched_constraint is not None
        )
        and rule_recoverable
        and evidence_recoverable
        and candidate.directly_contradictory
        and not ambiguity_parts
    )
    if matched_constraint is not None:
        constraint_id = matched_constraint.constraint_id
        canonical_rule_quote = matched_constraint.rule_text
    else:
        constraint_id = stable_constraint_id(
            source_path=source_path,
            domain=candidate.domain,
            rule_text=canonical_rule_quote,
        )
    violation_domain = cast(
        _ConstraintViolationDomain,
        "chapter.timeline" if is_core_state or is_book_clock else candidate.domain,
    )
    return ConstraintViolation(
        constraint_id=constraint_id,
        domain=violation_domain,
        source_path=source_path,
        source_hash=source_hash,
        rule_quote=canonical_rule_quote,
        evidence_quote=candidate.evidence_quote,
        severity="hard" if hard_eligible else "soft",
        ambiguity=";".join(dict.fromkeys(ambiguity_parts)) or None,
        suggested_action=(
            "删除或改写该正文证据表达的冲突命题，使其语义与权威规则一致；"
            "不得只换同义词、补充因果或强化原冲突结论。"
            if hard_eligible
            else None
        ),
        explanation=(
            (
                (
                    "批准正文形成的当前核心状态只能通过章节卡预授权迁移；"
                    "富记忆、对白、梦境、回忆和猜测不得覆盖。"
                    if is_core_state
                    else "主时间线必须落在冻结书内时钟区间；对白、梦境、回忆、"
                    "倒叙和插叙不改写主时间线。"
                )
                if is_core_state or is_book_clock
                else "world.unique_rules 是最高优先级事实；章节卡、角色状态、"
                "检索记忆和源正文中的冲突派生事实均不得覆盖它。"
            )
            if hard_eligible
            else None
        ),
    )


def _canonical_world_rule_quote(
    *,
    unique_rules: str,
    rule_quote: str,
) -> tuple[str, str | None]:
    """把任意合法截句归一到唯一权威规则单元，稳定同一规则的 HARD 身份。

    Provider 仍须逐字引用 ``unique_rules``；本函数只在 quote 已可回查后运行。
    编号规则优先按编号切分，普通文本按句号/分号/换行切分。若同一截句同时
    命中多个规则单元，不能猜归属，保留原 quote 并把候选降为 SOFT。
    """

    units = _authoritative_world_rule_units(unique_rules)
    matches = [unit for unit in units if rule_quote in unit]
    if len(matches) == 1:
        return matches[0], None
    if len(matches) > 1:
        return rule_quote, "rule_quote_matches_multiple_authoritative_units"
    # 跨越多个规则单元的长 quote 或整段 quote 仍是可回查原文；此时不擅自
    # 截断，只沿用原文本身作为稳定身份。
    return rule_quote, None


def _authoritative_world_rule_units(unique_rules: str) -> list[str]:
    """按原文边界提取权威规则单元，不改写任何字词。"""

    numbered_markers = list(_NUMBERED_RULE_MARKER_RE.finditer(unique_rules))
    if numbered_markers:
        units = []
        for index, marker in enumerate(numbered_markers):
            end = (
                numbered_markers[index + 1].start()
                if index + 1 < len(numbered_markers)
                else len(unique_rules)
            )
            unit = unique_rules[marker.end() : end].strip().rstrip("。！？!?；;")
            if unit:
                units.append(unit)
        if units:
            return units

    return [
        unit.strip() for unit in _RULE_UNIT_SPLIT_RE.split(unique_rules) if unit.strip()
    ]


def _constraint_source(
    *,
    domain: str,
    contract: SettingContract,
) -> tuple[str, str]:
    """返回稳定来源；legacy/import 不伪造不存在的 hash。"""
    if domain.startswith("world."):
        if contract.has_provenance:
            return contract.source_path, contract.source_hash
        return _LEGACY_SOURCE_PATH, ""
    if domain.startswith("character."):
        return _CHARACTER_SOURCE_PATH, ""
    return _CHAPTER_SOURCE_PATH, ""


def _build_user_message(
    *,
    chapter_text: str,
    contract: SettingContract,
    identities: list[CharacterIdentityRef],
    motivations: list[CharacterMotivationRef],
    validation_scope: SettingValidationScope = "chapter_text",
    core_state_projection: CoreStateProjection | None = None,
    core_state_constraint: ExecutableConstraint | None = None,
    book_clock_constraint: ExecutableConstraint | None = None,
    required_rechecks: list[ConstraintViolation] | None = None,
) -> str:
    """拼装 user message:设定契约 + 出场角色身份 + 角色核心动机 + 本章正文 + marker。"""
    if identities:
        id_lines = []
        for identity_ref in identities:
            facet_block = "; ".join(
                f"{facet.context}={facet.identity}" for facet in identity_ref.facets
            )
            id_lines.append(f"- {identity_ref.character_name}:{facet_block}")
        identities_block = "\n".join(id_lines)
    else:
        identities_block = "(无多情境身份角色)"

    if motivations:
        mot_lines = []
        for motivation_ref in motivations:
            if not motivation_ref.motivations:
                continue
            mot_block = "; ".join(motivation_ref.motivations)
            mot_lines.append(f"- {motivation_ref.character_name}:{mot_block}")
        motivations_block = "\n".join(mot_lines) if mot_lines else "(无)"
    else:
        motivations_block = "(无已确立动机角色)"

    if core_state_projection is not None and core_state_constraint is not None:
        clock_rule = (
            "\n- 本章书内时钟 HARD：" + book_clock_constraint.rule_text
            if book_clock_constraint is not None
            else ""
        )
        core_state_block = (
            "## 全书核心状态（与 Writer 共用的 HARD 投影）\n\n"
            + render_core_state_capsule(projection=core_state_projection).text
            + "\n\n- 本轮统一 HARD 通过条件："
            + core_state_constraint.rule_text
            + clock_rule
            + "\n- 只有正文给出与当前值或获准变化直接互斥的客观事实，才报 "
            "`domain=core_state`；只有正文直接违反 frozen exact/bounds/closed relation/"
            "day phase 或真正主线倒序才报 `domain=book_clock`。近似量没有派生 bounds，"
            "未写分钟或无法证明矛盾不得 HARD。对白、梦境、回忆、倒叙、插叙、猜测、"
            "短暂表象和未提及不算。\n\n"
        )
    else:
        core_state_block = ""

    content_label = {
        "chapter_text": "本章正文",
        "outline_plot": "当前主线 plot",
        "volume_plans": "当前卷规划 volume_plans",
        "mini_plan": "当前卷 mini plan",
        "chapter_card": "当前章节卡合同",
    }[validation_scope]
    completeness_block = (
        "## 判定完整性（同一内容内的正确复述不能抵消冲突句）\n\n"
        "逐项检查规则的触发条件、作用对象、数量范围、持续时间和效果强度。"
        "若权威规则限定为短暂／临时，内容却把同一原因写成永久／不可逆，"
        "这是直接互斥；即使内容别处也逐字复述了正确规则，冲突句仍须单独报告，"
        "不得用一正一误相互抵消。反之，角色在当前章节结束时仍处于某状态，只说明"
        "当前时点，不能据此推断该状态永久。\n\n"
    )
    if required_rechecks:
        recheck_items = []
        for violation in required_rechecks:
            recheck_items.append(
                "\n".join(
                    [
                        f"- constraint_id:{violation.constraint_id}",
                        f"  rule_quote:「{violation.rule_quote}」",
                        f"  previous_evidence_quote:「{violation.evidence_quote}」",
                        f"  previous_ambiguity:{violation.ambiguity or 'null'}",
                    ]
                )
            )
        recheck_block = (
            "## 定向修复后必须逐项复核（fail closed）\n\n"
            "下列身份来自上一候选的真实阻断。本次必须在同一 JSON 的 `rechecks` "
            "中逐项返回，ID 不得缺失、增加或改写。`resolved` 只能由当前卡合同及其"
            "批准连续性证据中的逐字引文正向证明：要么触发条件已移除，要么规则要求"
            "已满足，要么冲突禁令已删除。仅改写措辞、仅否定并列触发条件中的其他"
            "分支、或仍缺关键事实，都只能是 `still_conflict/ambiguous`。\n\n"
            + "\n\n".join(recheck_items)
            + "\n\n"
        )
    else:
        recheck_block = ""
    body = (
        f"## 设定契约({content_label}不得与之冲突;留白 / 没交代 ≠ 冲突)\n\n"
        f"{contract.render_for_prompt()}\n\n"
        f"{core_state_block}"
        f"{completeness_block}"
        "### 出场角色固定身份(同一角色各情境身份不得写串)\n\n"
        f"{identities_block}\n\n"
        "### 出场角色核心动机(A5:即兴深化合法,与已确立动机直接矛盾才报)\n\n"
        f"{motivations_block}\n\n"
        f"{recheck_block}"
        f"## {content_label}\n\n"
        f"{chapter_text}\n"
    )
    return append_role_marker(body, role="setting_consistency_validator")


def _parse_llm_payload(*, raw_text: str, model_used: str) -> _SettingLLMPayload:
    """剥代码块 → ``json.loads`` → Pydantic 校验。失败抛 ``ValueError``。"""
    text = _strip_code_fence(raw_text).strip()
    try:
        data = loads_with_quote_repair(
            text,
            agent_name="setting_consistency_validator",
            model_used=model_used,
        )
    except json.JSONDecodeError as exc:
        raise ValueError(
            f"setting_consistency_validator LLM 输出非合法 JSON(model={model_used})"
        ) from exc
    try:
        return _SettingLLMPayload.model_validate(data)
    except ValidationError as exc:
        raise ValueError(
            f"setting_consistency_validator LLM 输出 schema 不符(model={model_used}): "
            f"{exc.errors()}"
        ) from exc


def _strip_code_fence(text: str) -> str:
    """剥 trailing HTML 注释 + ``` / ```json 包裹(沿用同型清理)。"""
    while True:
        stripped = _TRAILING_HTML_COMMENT_RE.sub("", text)
        if stripped == text:
            break
        text = stripped
    match = _CODE_FENCE_RE.match(text)
    if match is not None:
        return match.group("body")
    return text


__all__ = [
    "SettingContract",  # 兼容旧 import；定义已迁到 shared core model
    "ConstraintViolation",
    "CharacterIdentityRef",
    "CharacterMotivationRef",
    "SettingConsistencyResult",
    "SettingConstraintRecheck",
    "SettingConsistencyTechnicalError",
    "SettingValidationScope",
    "validate_setting_consistency",
]
