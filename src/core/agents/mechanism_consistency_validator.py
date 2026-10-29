"""mechanism_consistency LLM-as-judge validator(v1.1 wave 6 session 1,A1 治本核心)。

职责:在 outline **生成期**对 world 段做"机制具体化"自检,把 B1 暴露的**大纲坑①
(机制高概念留白)**根因前置到大纲层拦截 —— 高概念词(认知融合 / 共振 / 意识实验体
肉体状态等)只写了词、没写**可执行规则**,writer 即兴 → 抽象 / 矛盾(B1 bug A2 绣春刀
穿帮 / A3 肉体在 vs 不在 / A6 双界机制全书摇摆)。

判据两类(对齐 [docs/plan/v1_1_wave6_design.md](../../../docs/plan/v1_1_wave6_design.md) §0
两原则 + §1 A1 行):

1. **真矛盾**(contradictions,HARD):world 内部给两个**互斥**答案(如"纯意识穿梭、
   肉体留现实" ↔ 同时说"肉体可跨界搬运")。对齐 §0 通用判据"只对真崩坏 / 真矛盾做
   hard" —— caller 把非空 contradictions 转 ``ValueError`` 触发 build_world
   retry-with-feedback,让 LLM 自行消除互斥。
2. **高概念缺可执行规则**(vague_mechanisms,WARN):高概念词被当成承重设定用,却只
   有词、没有可执行规则(明确边界 / 触发条件 / 后果)。对齐 §0"可能误伤留白的维度
   降 warn-only" —— **只 structlog 标记不 raise**(留白合法,§0 第一原则:护 C3
   共振机制不必填满物理细节,读者脑补即可)。

接口签名::

    validate_mechanism_consistency(
        *, world, premise="", genre="",
        workspace_id=None,
        router=None, client=None, model=None,
        ...
    ) -> MechanismConsistencyResult

调用方(outline_builder.build_world 的 post_validate callback):每次 world 段生成 →
调本 agent → contradictions 非空抛 ValueError(retry-with-feedback 自动 retry);
vague_mechanisms 非空只 log warn 不 raise。

设计要点(沿用 beat_alignment_validator 同型架构,不发明新哲学)
================================================================

1. **1 次综合 LLM 调用**:world 5 字段一次性看完综合判定,比逐字段独立更全面。
2. **沿用 *_validator 路由**:agent_name ``"mechanism_consistency_validator"`` →
   ``agent_routing.mechanism_consistency_validator``(config.yaml,cloud_qwen_max_thinking
   thinking on:机制矛盾需深度斟酌,对齐 beat_alignment_validator / must_vs_forbidden_validator
   同型);未显式配则 ``resolve_routing`` fallback 走 reviewer profile。
3. **LLM 失败 fail-open**(对齐 beat_alignment_validator 决策 4):LLM 抛异常 / 返非法
   JSON / setup 阶段失败(prompt 缺失)→ ``passed=True`` + log warn。避免 LLM 瞬态
   故障让 world 段全卷返工(§0 第一原则:宁可漏报,不误伤)。
4. **agent_role marker = ``mechanism_consistency_validator``(无 chapter_id):
   MockLLMClient 识别 marker 返默认"无矛盾"JSON,既有 outline 生成 happy 路径不回归。
5. **失败语义**(retry-with-feedback 友好):``format_mechanism_failure_for_retry``
   把 contradictions 字面 list 格式化进 error msg,LLM retry 时直接读到"哪两条互斥",
   再生成 world 时主动消除。
"""

from __future__ import annotations

import json
import re
from dataclasses import dataclass, field
from pathlib import Path
from typing import TYPE_CHECKING

import structlog
from pydantic import BaseModel, Field, ValidationError

from src.config import settings
from src.core.agents.structured_handoff import HandoffError, handoff_user_text
from src.core.agents._json_contract import loads_with_quote_repair
from src.core.agents._prompt import (
    append_role_marker,
    load_prompt_template,
    resolve_routing,
)
from src.core.agents._structured_output import prepare_strict_json_output
from src.core.agents.creative_delta import EXPERIMENTAL_PIPELINE_CONTRACT, uses_experimental_pipeline
from src.core.llm.base import LLMClient, Message
from src.core.routing.router import ModelRouter

if TYPE_CHECKING:
    from src.core.models.outline import WorldOutput

_log = structlog.get_logger(__name__)

_VALIDATOR_PROMPT_FILENAME = "mechanism_consistency_validator_v1.md"

# 默认生成参数:综合判定对采样稳定性敏感,温度低;输出 JSON 含 2 个 list[str] + str,
# token 上限适中。router 路径下由 profile 决定。
_DEFAULT_TEMPERATURE = 0.3
_DEFAULT_MAX_TOKENS = 800

# 匹配 ```json ... ``` 或 ``` ... ``` 包裹(沿用 beat_alignment_validator 同型剥离)。
_CODE_FENCE_RE = re.compile(
    r"^\s*```(?:json)?\s*\n(?P<body>.*?)\n```\s*$",
    re.DOTALL,
)

# trailing HTML 注释剥离(沿用 beat_alignment_validator 同型)。
_TRAILING_HTML_COMMENT_RE = re.compile(
    r"\s*<!--.*?-->\s*$",
    re.DOTALL,
)


@dataclass(frozen=True)
class MechanismConsistencyResult:
    """validator 输出。

    - ``passed``:``contradictions`` 为空时 True;非空时 False(HARD 触发 retry)。
      实验 LLM / setup 失败抛出技术异常；旧路线保留 True(fail-open)。
    - ``contradictions``:LLM 判定 world 内互斥的机制规则对(自然语言,每条描述一处
      互斥)。``passed=False`` 的主要原因在这里。
    - ``vague_mechanisms``:LLM 判定"高概念词当承重设定用但缺可执行规则"的条目
      (WARN 标记,**不**影响 ``passed``;留白合法)。
    - ``evidence``:LLM 给出的简短判定依据(中文)。
    - ``llm_calls_count``:实际发起的 LLM 调用次数;正常 → 1;fail-open(LLM /
      setup 异常)→ 0 或 1。
    - ``fail_open``:为 True 当且仅当 LLM 调用 / setup 抛异常导致整体 fail-open
      (passed=True)。
    """

    passed: bool
    contradictions: list[str] = field(default_factory=list)
    vague_mechanisms: list[str] = field(default_factory=list)
    evidence: str = ""
    llm_calls_count: int = 0
    fail_open: bool = False


class _MechanismLLMPayload(BaseModel):
    """LLM 输出的中间模型(校验 3 字段)。"""

    contradictions: list[str] = Field(default_factory=list)
    vague_mechanisms: list[str] = Field(default_factory=list)
    evidence: str = Field(..., min_length=1, max_length=400)


class MechanismConsistencyTechnicalError(RuntimeError):
    """实验机制审核未取得有效裁决，禁止重生世界掩盖技术错误。"""


def validate_mechanism_consistency(
    *,
    world: WorldOutput,
    premise: str = "",
    genre: str = "",
    workspace_id: str | None = None,
    router: ModelRouter | None = None,
    client: LLMClient | None = None,
    model: str | None = None,
    max_tokens: int = _DEFAULT_MAX_TOKENS,
    temperature: float = _DEFAULT_TEMPERATURE,
    prompts_dir: Path | None = None,
) -> MechanismConsistencyResult:
    """1 次综合 LLM 调用,判定 world 段机制是否自洽 + 高概念词是否配可执行规则。

    :param world: ``WorldOutput`` 第 1 段产出(5 字段)。
    :param premise: 用户一句话设定(可选,给 judge 作背景)。
    :param genre: 题材(可选)。
    :param workspace_id: 工作区 ID,落 structlog 字段(可选)。
    :param router: ``ModelRouter`` 实例;给定时按
        ``router.route("mechanism_consistency_validator")`` 取 client + model +
        生成参数。``None`` 时走 legacy 路径。
    :param client: legacy 路径 LLM 客户端;``router=None`` 时必传。
    :param model: legacy 路径模型名;``router=None`` 时必传。
    :param max_tokens: legacy 路径 token 上限。
    :param temperature: legacy 路径温度。
    :param prompts_dir: 提示词模板根目录;``None`` 时取 ``settings.prompts_dir``。
    :return: ``MechanismConsistencyResult``。
    """
    experimental = (
        getattr(router, "pipeline_contract", None) == EXPERIMENTAL_PIPELINE_CONTRACT
        or (client is not None and uses_experimental_pipeline(client))
    )
    # 实验审核失败停止；旧路线保留历史兼容。
    try:
        resolved_client, resolved_model, complete_kwargs = resolve_routing(
            agent_name="mechanism_consistency_validator",
            router=router,
            client=client,
            model=model,
            legacy_kwargs={"max_tokens": max_tokens, "temperature": temperature},
        )
        experimental = experimental or uses_experimental_pipeline(resolved_client)
        template_root = (
            prompts_dir if prompts_dir is not None else settings.prompts_dir
        ).resolve()
        system_text = load_prompt_template(template_root / _VALIDATOR_PROMPT_FILENAME)
        complete_kwargs = prepare_strict_json_output(
            client=resolved_client,
            model=resolved_model,
            output_model=_MechanismLLMPayload,
            schema_name="mechanism_consistency",
            complete_kwargs=complete_kwargs,
        )
    except Exception as exc:  # noqa: BLE001 — 旧路线兼容
        if isinstance(exc, HandoffError):
            raise
        if experimental:
            raise MechanismConsistencyTechnicalError("世界机制审核准备失败，尚无有效裁决") from exc
        _log.warning(
            "mechanism_consistency_validator_setup_failed_open",
            workspace_id=workspace_id,
            error_type=type(exc).__name__,
            error_message=str(exc),
        )
        return MechanismConsistencyResult(
            passed=True,
            llm_calls_count=0,
            fail_open=True,
        )

    user_text = _build_user_message(world=world, premise=premise, genre=genre)
    user_text = handoff_user_text(
        resolved_client, stage="mechanism_consistency_validator",
        data={"world": world, "premise": premise, "genre": genre}, original_task=user_text,
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
            raw_text=response.content, model_used=response.model_used,
            require_explicit_verdict=experimental
        )
    except Exception as exc:  # noqa: BLE001 — 旧路线兼容
        if isinstance(exc, HandoffError):
            raise
        if experimental:
            raise MechanismConsistencyTechnicalError("世界机制审核未取得有效裁决，停止后续生成") from exc
        _log.warning(
            "mechanism_consistency_validator_failed_open",
            workspace_id=workspace_id,
            error_type=type(exc).__name__,
            error_message=str(exc),
        )
        return MechanismConsistencyResult(
            passed=True,
            llm_calls_count=1,
            fail_open=True,
        )

    passed = len(payload.contradictions) == 0
    if passed:
        _log.debug(
            "mechanism_consistency_validator_passed",
            workspace_id=workspace_id,
            vague_count=len(payload.vague_mechanisms),
            evidence=payload.evidence,
        )
    else:
        _log.info(
            "mechanism_consistency_validator_failed",
            workspace_id=workspace_id,
            contradictions=payload.contradictions,
            vague_mechanisms=payload.vague_mechanisms,
            evidence=payload.evidence,
        )
    # vague_mechanisms 是 WARN-only 标记(不影响 passed);非空时单独 log warn,
    # 让"高概念缺可执行规则"在 OBSERVABILITY 可见(§0:warn-only 标记,护留白)。
    if payload.vague_mechanisms:
        _log.warning(
            "mechanism_consistency_vague_mechanisms",
            workspace_id=workspace_id,
            vague_mechanisms=payload.vague_mechanisms,
            evidence=payload.evidence,
        )

    return MechanismConsistencyResult(
        passed=passed,
        contradictions=payload.contradictions,
        vague_mechanisms=payload.vague_mechanisms,
        evidence=payload.evidence,
        llm_calls_count=1,
        fail_open=False,
    )


def _build_user_message(*, world: WorldOutput, premise: str, genre: str) -> str:
    """拼装 user message:world 5 字段 + 背景 + agent_role marker。"""
    premise_text = premise.strip() if premise.strip() else "(未提供)"
    genre_text = genre.strip() if genre.strip() else "(未指定)"
    body = (
        "## 背景\n\n"
        f"- 一句话设定(premise):{premise_text}\n"
        f"- 题材(genre):{genre_text}\n\n"
        "## 待审世界观(world)\n\n"
        f"- setting:{world.setting}\n"
        f"- power_system:{world.power_system}\n"
        f"- factions:{world.factions}\n"
        f"- culture:{world.culture}\n"
        f"- unique_rules:{world.unique_rules}\n"
    )
    return append_role_marker(body, role="mechanism_consistency_validator")


def _parse_llm_payload(*, raw_text: str, model_used: str, require_explicit_verdict: bool = False) -> _MechanismLLMPayload:
    """剥代码块 → ``json.loads`` → Pydantic 校验。失败抛 ``ValueError``。"""
    text = _strip_code_fence(raw_text).strip()
    try:
        data = loads_with_quote_repair(
            text,
            agent_name="mechanism_consistency_validator",
            model_used=model_used,
        )
    except json.JSONDecodeError as exc:
        raise ValueError(
            f"mechanism_consistency_validator LLM 输出非合法 JSON(model={model_used}): "
            f"{raw_text[:200]!r}"
        ) from exc
    if require_explicit_verdict and (
        not isinstance(data, dict)
        or not {"contradictions", "vague_mechanisms"}.issubset(data)
    ):
        raise ValueError("机制审核必须显式返回两项裁决数组")
    try:
        return _MechanismLLMPayload.model_validate(data)
    except ValidationError as exc:
        raise ValueError(
            f"mechanism_consistency_validator LLM 输出 schema 不符(model={model_used}): "
            f"{exc.errors()}"
        ) from exc


def _strip_code_fence(text: str) -> str:
    """剥 trailing HTML 注释 + ``` / ```json 包裹(沿用 beat_alignment_validator 同型)。"""
    while True:
        stripped = _TRAILING_HTML_COMMENT_RE.sub("", text)
        if stripped == text:
            break
        text = stripped
    match = _CODE_FENCE_RE.match(text)
    if match is not None:
        return match.group("body")
    return text


def format_mechanism_failure_for_retry(*, result: MechanismConsistencyResult) -> str:
    """格式化 validator 失败信息,供 build_world retry-with-feedback 反馈给 LLM。

    设计:让 LLM 在 retry 时能从 error msg 直接读出"哪几处机制规则互斥",
    再生成 world 时主动消除互斥(给一个确定的可执行规则,而非两个互斥答案)。
    """
    parts = ["world 段机制一致性自检失败 —— 存在互斥的机制设定(必须消除):"]
    for i, c in enumerate(result.contradictions, start=1):
        parts.append(f"  {i}. {c}")
    parts.append(
        "  请修订 unique_rules / power_system / setting,让每条承重机制只给"
        "**一个确定的可执行规则**(留白可以,但不能给两个互斥答案)。"
    )
    if result.evidence:
        parts.append(f"  - LLM 判定依据:{result.evidence}")
    return "\n".join(parts)


__all__ = [
    "MechanismConsistencyResult",
    "validate_mechanism_consistency",
    "format_mechanism_failure_for_retry",
]
