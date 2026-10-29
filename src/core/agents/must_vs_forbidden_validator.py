"""must_achieve vs forbidden 可执行性判定 LLM-as-judge validator。

v1.1 audit G20 于 2026-05-27 引入二档互斥检查；R9 §2.115 复用同一个 Agent /
Provider 调用，把输出收紧为 ``conflict / ambiguous / no_conflict`` 三档；§2.120
进一步把 ``ambiguous`` 限定为 must 的必需执行路径不清，普通叙事选择不得触发。

职责:把 chapter_card.must_achieve(必达事件列表)与 chapter_card.forbidden(禁止
事件列表)之间是否存在语义互斥矛盾的判定,从 chapter_card_generator 解耦,
**1 章节卡 1 次综合 LLM 调用**(沿用 beat_alignment_validator 同型架构 — "1 次综合
判定"模式,而非"逐条 N×M 次"模式;自决项理由见 §设计要点 1)。

接口签名:::

    validate_must_vs_forbidden(
        *, must_achieve, forbidden,
        chapter_id, workspace_id=None,
        tension_level=None, setting=None,
        router=None, client=None, model=None,
        ...
    ) -> MustVsForbiddenValidationResult

调用方(chapter_card_generator.py `generate_chapter_card` 后置,
``_validate_cross_field_references`` + ``_detect_forbidden_style_pollution``
之后):章节卡生成成功 → 调本 agent → 若 conflict 或 ambiguous 抛
ChapterCardGeneratorError → caller(chapter_card_service)走既有卡片 retry 或 422。

设计要点
========

1. **1 章节卡 1 次综合 LLM 调用**(沿用 beat_alignment_validator 同型决策):
   - **1st 质量收益**:LLM 一次性看完 must_achieve 全列表 + forbidden 全列表,
     综合判定笛卡尔积全配对,比 N×M 次独立调用更全面
   - **3rd 工程便利**:每章节卡 +1 次 LLM 调用(成本 ~$0.001-0.005),
     30 章 = $0.03-0.15 USD,几乎不计

2. **沿用 cloud_qwen_max_thinking profile**(对齐 chapter_card_generator
   同型 thinking on profile;config.yaml ``agent_routing.must_vs_forbidden_validator``
   显式配置 thinking on)。

3. **空 must_achieve / 空 forbidden 跳过**:任一为空 → 跳过 LLM 调用直接
   ``passed=True``。无 must 或无 forbidden 时不可能产生互斥矛盾。

4. **LLM 失败 fail-open**(对齐 beat_alignment_validator 决策 4):LLM 调用抛
   异常或返非法 JSON → 整个 validator ``passed=True`` + log warn。避免 LLM
   瞬态故障导致章节卡生成失败。

5. **agent_role marker = ``must_vs_forbidden_validator``**:
   user message 末尾追加 ``<!-- agent_role: must_vs_forbidden_validator -->``
   marker 供 MockLLMClient 识别。**marker 不携带 chapter_id** — 与 beat_alignment_validator
   同型,mock 路径始终返 no_conflict JSON 让既有 chapter_card 生成 happy 路径不回归。

6. **失败语义**(caller retry 友好):直接互斥或缺少可执行桥梁都返
   ``passed=False`` + ``conflicts`` list,caller(chapter_card_generator)据此 raise
   ChapterCardGeneratorError(error msg 含 conflicts 字面 list 供 retry feedback)。
"""

from __future__ import annotations

import json
import re
from dataclasses import dataclass, field
from pathlib import Path
from typing import TYPE_CHECKING, Literal

import structlog
from pydantic import BaseModel, Field, ValidationError, model_validator

from src.config import settings
from src.core.models.narrative import UserNarrativeRequest
from src.core.agents.structured_handoff import HandoffError, handoff_user_text
from src.core.agents._json_contract import loads_with_quote_repair
from src.core.agents._prompt import (
    append_role_marker,
    load_prompt_template,
    resolve_routing,
)
from src.core.agents._structured_output import prepare_strict_json_output
from src.core.agents.creative_delta import uses_hy3_draft_v3
from src.core.llm.base import LLMClient, Message
from src.core.llm.local_json_contract import LocalJsonResponseViolation

if TYPE_CHECKING:
    from src.core.routing.router import ModelRouter

_log = structlog.get_logger(__name__)

_VALIDATOR_PROMPT_FILENAME = "must_vs_forbidden_validator_v3.md"
_FROZEN_VALIDATOR_PROMPT_FILENAMES = (
    "must_vs_forbidden_validator_v2.md",
    "must_vs_forbidden_validator_v1.md",
)

# 默认生成参数:综合判定对采样稳定性敏感,温度低且 token 上限适中(JSON 含
# conflicts list + evidence,约 200-800 token)。router 路径下由 profile 决定。
_DEFAULT_TEMPERATURE = 0.3
_DEFAULT_MAX_TOKENS = 800

# 匹配 ```json ... ``` 或 ``` ... ``` 包裹;沿用 beat_alignment_validator 同型剥离。
_CODE_FENCE_RE = re.compile(
    r"^\s*```(?:json)?\s*\n(?P<body>.*?)\n```\s*$",
    re.DOTALL,
)

# trailing HTML 注释剥离。
_TRAILING_HTML_COMMENT_RE = re.compile(
    r"\s*<!--.*?-->\s*$",
    re.DOTALL,
)

_MAX_EVIDENCE_LENGTH = 300


class MustVsForbiddenBoundaryError(ValueError):
    """新首稿的源卡边界未获得有效裁决；不能用 fail-open 冒充通过。"""


@dataclass(frozen=True)
class MustVsForbiddenConflict:
    """单条不可执行发现；保留旧类名以兼容既有内部 caller。"""

    must_index: int
    must_text: str
    forbidden_index: int
    forbidden_text: str
    reason: str
    classification: Literal["conflict", "ambiguous"] = "conflict"


@dataclass(frozen=True)
class MustVsForbiddenValidationResult:
    """validator 输出。

    - ``passed``:LLM ``verdict=no_conflict`` 时 True；另两档均为 False。
      空 must / 空 forbidden / LLM fail → True(skip / fail-open 语义)。
    - ``conflicts``:兼容旧字段名，承载 conflict/ambiguous findings；通过时 ``[]``。
    - ``evidence``:LLM 给出的简短判定依据(中文)。
    - ``llm_calls_count``:实际发起的 LLM 调用次数;空字段跳过 → 0;正常 → 1;
      fail-open(LLM 抛异常)→ 1。
    - ``skipped``:为 True 当且仅当 must / forbidden 任一为空走"跳过"分支。
    - ``fail_open``:为 True 当且仅当 LLM 调用抛异常导致整体 fail-open。
    """

    passed: bool
    verdict: Literal["conflict", "ambiguous", "no_conflict"] = "no_conflict"
    conflicts: list[MustVsForbiddenConflict] = field(default_factory=list)
    evidence: str = ""
    llm_calls_count: int = 0
    skipped: bool = False
    fail_open: bool = False


class _ConflictPayload(BaseModel):
    """LLM 输出中单条 conflict 字段。"""

    must_index: int = Field(..., ge=0)
    must_text: str = Field(..., min_length=1)
    forbidden_index: int = Field(..., ge=0)
    forbidden_text: str = Field(..., min_length=1)
    reason: str = Field(..., min_length=1, max_length=200)
    classification: Literal["conflict", "ambiguous"] = "conflict"


class _ValidatorLLMPayload(BaseModel):
    """LLM 输出的中间模型(校验 3 字段)。"""

    verdict: Literal["conflict", "ambiguous", "no_conflict"]
    conflicts: list[_ConflictPayload] = Field(default_factory=list)
    evidence: str = Field(..., min_length=1, max_length=_MAX_EVIDENCE_LENGTH)

    @model_validator(mode="after")
    def _validate_verdict_findings(self) -> _ValidatorLLMPayload:
        if self.verdict == "no_conflict" and self.conflicts:
            raise ValueError("no_conflict 不得携带 findings")
        if self.verdict != "no_conflict" and not self.conflicts:
            raise ValueError("conflict/ambiguous 必须携带至少一条 finding")
        classifications = {item.classification for item in self.conflicts}
        if self.verdict == "conflict" and "conflict" not in classifications:
            raise ValueError("conflict verdict 必须至少含一条 conflict finding")
        if self.verdict == "ambiguous" and classifications != {"ambiguous"}:
            raise ValueError("ambiguous verdict 只能包含 ambiguous findings")
        return self


def validate_must_vs_forbidden(
    *,
    must_achieve: list[str],
    forbidden: list[str],
    chapter_id: str,
    workspace_id: str | None = None,
    tension_level: int | None = None,
    setting: str | None = None,
    craft_focus: str | None = None,
    next_chapter_seed: str | None = None,
    user_request: UserNarrativeRequest | None = None,
    router: "ModelRouter | None" = None,
    client: LLMClient | None = None,
    model: str | None = None,
    max_tokens: int = _DEFAULT_MAX_TOKENS,
    temperature: float = _DEFAULT_TEMPERATURE,
    prompts_dir: Path | None = None,
) -> MustVsForbiddenValidationResult:
    """1 章节卡 1 次综合调用，判定 must/forbidden 是否可无猜测执行。

    :param must_achieve: 章节卡 ``must_achieve`` 字面(2-4 条)
    :param forbidden: 章节卡 ``forbidden`` 字面(2-4 条)
    :param chapter_id: 章节 ID,落 structlog 字段
    :param workspace_id: 工作区 ID,落 structlog 字段(可选)
    :param tension_level: 张力 1-10(供 LLM 上下文)
    :param setting: 场景设置(供 LLM 上下文)
    :param router: ``ModelRouter`` 实例;给定时按 ``router.route("must_vs_forbidden_validator")``
        取 client + model + 生成参数
    :param client: legacy 路径 LLM 客户端;``router=None`` 时必传
    :param model: legacy 路径模型名;``router=None`` 时必传
    :param max_tokens: legacy 路径 token 上限
    :param temperature: legacy 路径温度
    :param prompts_dir: 提示词模板根目录;``None`` 时取 ``settings.prompts_dir``
    :return: ``MustVsForbiddenValidationResult``
    """
    if user_request is not None:
        user_request = UserNarrativeRequest.model_validate(user_request.model_dump(mode="json"))

    # 决策 3:must / forbidden 任一为空 → 跳过 + passed=True
    if not must_achieve or not forbidden:
        _log.debug(
            "must_vs_forbidden_validator_skipped",
            chapter_id=chapter_id,
            workspace_id=workspace_id,
            reason="empty_must_or_forbidden",
            must_count=len(must_achieve),
            forbidden_count=len(forbidden),
        )
        return MustVsForbiddenValidationResult(
            passed=True,
            verdict="no_conflict",
            llm_calls_count=0,
            skipped=True,
        )

    # 历史合同保留原 fail-open；v3 seed 检查无法验真时停止出卡。
    boundary_check = bool(next_chapter_seed) and (
        (client is not None and uses_hy3_draft_v3(client))
        or getattr(getattr(router, "run_model_manifest_binding", None), "manifest_id", None)
        == "hy3"
    )
    seed_index: int | None = None
    try:
        resolved_client, resolved_model, complete_kwargs = resolve_routing(
            agent_name="must_vs_forbidden_validator",
            router=router,
            client=client,
            model=model,
            legacy_kwargs={"max_tokens": max_tokens, "temperature": temperature},
        )
        boundary_check = boundary_check or (
            bool(next_chapter_seed) and uses_hy3_draft_v3(resolved_client)
        )
        template_root = (
            prompts_dir if prompts_dir is not None else settings.prompts_dir
        ).resolve()
        prompt_path = template_root / _VALIDATOR_PROMPT_FILENAME
        # 已冻结的历史活动会把当时 prompt 复制到活动目录，并显式传入
        # ``prompts_dir``。只为这种显式快照按新到旧保留 v2/v1 读取兼容；生产
        # 默认目录若缺少 v3 必须继续 setup fail-open 留痕，不能悄悄退回旧判尺。
        if prompts_dir is not None and not prompt_path.is_file():
            for frozen_filename in _FROZEN_VALIDATOR_PROMPT_FILENAMES:
                frozen_path = template_root / frozen_filename
                if frozen_path.is_file():
                    prompt_path = frozen_path
                    break
        system_text = load_prompt_template(prompt_path)
        # 复用原索引协议，末项只用于卡片可执行性检查，不写回正文 must/HARD。
        if boundary_check:
            assert next_chapter_seed is not None
            seed_index = len(must_achieve)
            must_achieve = [*must_achieve, next_chapter_seed]
            system_text += "\n\n" + load_prompt_template(
                template_root / ("must_vs_forbidden_boundary_v2.md" if user_request is not None
                                 else "must_vs_forbidden_boundary_v1.md")
            )
        if user_request is not None and not boundary_check:
            system_text += "\n\n" + load_prompt_template(template_root / "must_vs_forbidden_boundary_v2.md")
        complete_kwargs = prepare_strict_json_output(
            client=resolved_client,
            model=resolved_model,
            output_model=_ValidatorLLMPayload,
            schema_name="must_vs_forbidden",
            complete_kwargs=complete_kwargs,
        )
    except Exception as exc:  # noqa: BLE001 — fail-open 包括 setup 阶段
        if isinstance(exc, HandoffError):
            raise
        if boundary_check:
            raise MustVsForbiddenBoundaryError("seed 边界检查准备失败") from exc
        _log.warning(
            "must_vs_forbidden_validator_setup_failed_open",
            chapter_id=chapter_id,
            workspace_id=workspace_id,
            error_type=type(exc).__name__,
            error_message=str(exc),
        )
        return MustVsForbiddenValidationResult(
            passed=True,
            verdict="no_conflict",
            llm_calls_count=0,
            fail_open=True,
        )

    user_text = _build_user_message(
        must_achieve=must_achieve,
        forbidden=forbidden,
        chapter_id=chapter_id,
        tension_level=tension_level,
        setting=setting,
        craft_focus=craft_focus,
    )

    if user_request is not None:
        user_text += "\n\n用户原始前提与阅读要求（文学方向，不逐条要求证据）：\n" + user_request.premise

    if seed_index is not None:
        user_text += (
            f"\n\n检查列表索引 {seed_index} 的来源为 next_chapter_seed；"
            "其前面的条目索引与原 must_achieve 相同。"
        )
    user_text = handoff_user_text(
        resolved_client, stage="must_vs_forbidden_validator",
        data={"must_achieve": must_achieve, "forbidden": forbidden, "chapter_id": chapter_id, "tension_level": tension_level, "setting": setting, "craft_focus": craft_focus, "next_chapter_seed_index": seed_index, **({"user_request": user_request} if user_request is not None else {})}, original_task=user_text,
    )
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
        _validate_payload_references(
            payload=payload,
            must_achieve=must_achieve,
            forbidden=forbidden,
        )
    except Exception as exc:  # noqa: BLE001 — 决策 4:fail-open
        if isinstance(exc, HandoffError):
            raise
        if boundary_check:
            if isinstance(exc, LocalJsonResponseViolation) and not exc.response.content.strip():
                raise MustVsForbiddenBoundaryError("seed 边界审核返回空 content，未形成可用裁决") from exc
            raise MustVsForbiddenBoundaryError("seed 边界裁决或原文索引无效") from exc
        _log.warning(
            "must_vs_forbidden_validator_failed_open",
            chapter_id=chapter_id,
            workspace_id=workspace_id,
            error_type=type(exc).__name__,
            error_message=str(exc),
        )
        return MustVsForbiddenValidationResult(
            passed=True,
            verdict="no_conflict",
            llm_calls_count=1,
            fail_open=True,
        )

    passed = payload.verdict == "no_conflict"
    conflicts = [
        MustVsForbiddenConflict(
            must_index=c.must_index,
            must_text=c.must_text,
            forbidden_index=c.forbidden_index,
            forbidden_text=c.forbidden_text,
            reason=c.reason,
            classification=c.classification,
        )
        for c in payload.conflicts
    ]

    if passed:
        _log.debug(
            "must_vs_forbidden_validator_passed",
            chapter_id=chapter_id,
            workspace_id=workspace_id,
            evidence=payload.evidence,
        )
    else:
        _log.info(
            "must_vs_forbidden_validator_failed",
            chapter_id=chapter_id,
            workspace_id=workspace_id,
            conflicts_count=len(conflicts),
            verdict=payload.verdict,
            conflicts=[
                {
                    "must_index": c.must_index,
                    "forbidden_index": c.forbidden_index,
                    "reason": c.reason,
                }
                for c in conflicts
            ],
            evidence=payload.evidence,
            attempt=1,
            failure_layer="chapter_card_contract",
        )

    return MustVsForbiddenValidationResult(
        passed=passed,
        verdict=payload.verdict,
        conflicts=conflicts,
        evidence=payload.evidence,
        llm_calls_count=1,
        skipped=False,
        fail_open=False,
    )


def _build_user_message(
    *,
    must_achieve: list[str],
    forbidden: list[str],
    chapter_id: str,
    tension_level: int | None,
    setting: str | None,
    craft_focus: str | None = None,
) -> str:
    """拼装 user message:must_achieve list + forbidden list + 上下文 + agent_role marker。"""
    must_block = "\n".join(f"  - {m}" for m in must_achieve)
    forbidden_block = "\n".join(f"  - {f}" for f in forbidden)
    context_lines = [f"- chapter_id: {chapter_id}"]
    if tension_level is not None:
        context_lines.append(f"- tension_level: {tension_level}/10")
    if setting:
        context_lines.append(f"- setting: {setting}")
    if craft_focus:
        context_lines.append(
            f"- craft_focus（本章过程设计原文，供判断可执行性；文学建议不升级为必达）: {craft_focus}"
        )
    context_block = "\n".join(context_lines)

    body = (
        "## must_achieve 列表\n\n"
        f"{must_block}\n\n"
        "## forbidden 列表\n\n"
        f"{forbidden_block}\n\n"
        "## 上下文\n\n"
        f"{context_block}\n"
    )
    return append_role_marker(body, role="must_vs_forbidden_validator")


def _validate_payload_references(
    *,
    payload: _ValidatorLLMPayload,
    must_achieve: list[str],
    forbidden: list[str],
) -> None:
    """拒绝 Judge 用无效索引或改写后的规则文本制造卡片缺陷。"""

    for finding in payload.conflicts:
        if finding.must_index >= len(must_achieve):
            raise ValueError("must_vs_forbidden finding.must_index 越界")
        if finding.forbidden_index >= len(forbidden):
            raise ValueError("must_vs_forbidden finding.forbidden_index 越界")
        if finding.must_text != must_achieve[finding.must_index]:
            raise ValueError("must_vs_forbidden finding.must_text 未逐字回指输入")
        if finding.forbidden_text != forbidden[finding.forbidden_index]:
            raise ValueError("must_vs_forbidden finding.forbidden_text 未逐字回指输入")


def _normalize_explanatory_fields(data: object, *, model_used: str) -> object:
    """只收敛不参与裁决的说明文本，不改 verdict / findings / 回指字段。"""
    if not isinstance(data, dict):
        return data
    evidence = data.get("evidence")
    if not isinstance(evidence, str) or len(evidence) <= _MAX_EVIDENCE_LENGTH:
        return data
    normalized = dict(data)
    normalized["evidence"] = evidence[:_MAX_EVIDENCE_LENGTH]
    _log.warning(
        "must_vs_forbidden_validator_evidence_truncated",
        model_used=model_used,
        original_length=len(evidence),
        retained_length=_MAX_EVIDENCE_LENGTH,
        note="只截断说明文本，不改 verdict/conflicts/输入回指",
    )
    return normalized


def _parse_llm_payload(*, raw_text: str, model_used: str) -> _ValidatorLLMPayload:
    """剥代码块 → 严格 JSON（裸引号补救一次）→ Pydantic 校验。"""
    # 1. 剥 trailing HTML 注释(可能多个)
    text = raw_text.strip()
    while True:
        stripped = _TRAILING_HTML_COMMENT_RE.sub("", text)
        if stripped == text:
            break
        text = stripped
    # 2. 剥代码块包裹
    match = _CODE_FENCE_RE.match(text)
    if match is not None:
        text = match.group("body")

    try:
        data = loads_with_quote_repair(
            text,
            agent_name="must_vs_forbidden_validator",
            model_used=model_used,
        )
    except json.JSONDecodeError as exc:
        raise ValueError(
            "must_vs_forbidden_validator LLM 输出非合法 JSON "
            f"(model={model_used}):{exc.msg};raw={text[:200]!r}"
        ) from exc

    data = _normalize_explanatory_fields(data, model_used=model_used)

    try:
        return _ValidatorLLMPayload.model_validate(data)
    except ValidationError as exc:
        raise ValueError(
            f"must_vs_forbidden_validator LLM 输出 schema 不符 (model={model_used}):"
            f"{exc};raw={text[:200]!r}"
        ) from exc
