"""identity_coverage LLM-as-judge validator(v1.1 wave 6 session 2,A3 核心)。

职责:**批处理**判定一组**多情境身份角色**(``identity_facets`` ≥ 2)的每个身份
侧面在全书 mini_plan 事件中是否**至少被交代一次**,治 B1 暴露的 **bug A5**
(白露现实侧整章丢失 —— writer 全程只写她梦境侧,现实=诊所助理兼黑客 + 童年记忆
到结局都没揭晓)。

**批处理(1 次 LLM 调用判全部候选)**:沿用 thread_closure_validator / must_achieve
G9.3 范本(−64% 调用)。多情境身份角色一次性喂 judge,既省 token 又避免
MockLLMClient 单实例调用护栏在全流程测试里被顶爆。**候选为空 → 0 次 LLM 调用**
(全书无多情境身份角色 = 普通故事,无需 judge)。

**WARN-only(对齐 [docs/plan/v1_1_wave6_design.md](../../../docs/plan/v1_1_wave6_design.md)
§0 第一原则 + bug 清单 A5)**:某身份侧面在全书事件中未被交代 → ``uncovered_contexts``
非空 → caller 只 ``log warn`` **不 raise**。真正治本靠 mini_plan prompt **上移**
(要求每个多情境角色每侧分配 ≥ 1 章);本 judge 是观测兜底。**为何不 HARD**:
"某侧是否被交代"是文学语义判断,LLM-judge 可能误判(事件一句话可能隐含交代了
某侧);§0 第一原则"宁可漏报也不误伤"。HARD 升级留 wave 6 末约束预算实测后定。

接口签名::

    validate_identity_coverage_batch(
        *, candidates, all_events,
        workspace_id=None,
        router=None, client=None, model=None,
        ...
    ) -> IdentityCoverageBatchResult

调用方(outline_builder ``_check_identity_coverage`` 全书编排):从 CharactersOutput
筛 ``identity_facets`` ≥ 2 的角色 → 一次性调本 agent(全书 one_line_event 作上下文)
→ uncovered 累计 log warn。编排逻辑在 outline_builder,本模块只做批量 LLM-judge
(对齐 thread_closure_validator 职责切分)。

设计要点(沿用 thread_closure_validator 同型架构)
================================================

1. **1 次批量 LLM 调用**:全部候选角色 + 全书事件一次性看完综合判定。
2. **沿用 *_validator 路由**:agent_name ``"identity_coverage_validator"`` →
   ``agent_routing.identity_coverage_validator``(config.yaml,cloud_qwen_max_thinking);
   未显式配则 ``resolve_routing`` fallback 走 reviewer profile。
3. **LLM 失败 fail-open**:LLM 抛异常 / 非法 JSON / setup 失败 → 全部候选
   ``uncovered_contexts=[]``(fail-open 不产 warn;§0 宁可漏报不误伤)。返回的
   result 数与候选数不符时按 index 对齐,缺的候选默认空 uncovered(不误产 warn)。
4. **agent_role marker = ``identity_coverage_validator``(无 chapter_id):MockLLMClient
   识别 marker 按 ``## 候选角色`` 编号数返对应数量空 uncovered JSON。
"""

from __future__ import annotations

import json
import re
from dataclasses import dataclass, field
from pathlib import Path

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
from src.core.llm.base import LLMClient, Message
from src.core.models.character import IdentityFacet
from src.core.routing.router import ModelRouter

_log = structlog.get_logger(__name__)

_VALIDATOR_PROMPT_FILENAME = "identity_coverage_validator_v1.md"

# 默认生成参数:综合判定对采样稳定性敏感,温度低;批量输出含 N 个 {uncovered + str},
# token 上限适中。router 路径下由 profile 决定。
_DEFAULT_TEMPERATURE = 0.3
_DEFAULT_MAX_TOKENS = 1200

_CODE_FENCE_RE = re.compile(
    r"^\s*```(?:json)?\s*\n(?P<body>.*?)\n```\s*$",
    re.DOTALL,
)
_TRAILING_HTML_COMMENT_RE = re.compile(
    r"\s*<!--.*?-->\s*$",
    re.DOTALL,
)


@dataclass(frozen=True)
class IdentityCoverageCandidate:
    """单个多情境身份角色的输入(``identity_facets`` ≥ 2,待 judge 覆盖度)。"""

    character_id: str
    character_name: str
    facets: tuple[IdentityFacet, ...]


@dataclass(frozen=True)
class IdentityCoverageVerdictItem:
    """单个角色的覆盖度结论。"""

    character_id: str
    character_name: str
    #: 全书事件中**从未被交代**的身份情境标签 list(空 = 各侧都至少交代一次,合格)。
    uncovered_contexts: list[str] = field(default_factory=list)
    evidence: str = ""


@dataclass(frozen=True)
class IdentityCoverageBatchResult:
    """批量 validator 输出。

    - ``items``:逐候选结论(顺序与入参 ``candidates`` 对齐)。
    - ``llm_calls_count``:实际 LLM 调用次数;候选为空 → 0;正常 / fail-open → 1。
    - ``fail_open``:LLM / setup 异常导致 fail-open(全部空 uncovered)时 True。
    """

    items: list[IdentityCoverageVerdictItem] = field(default_factory=list)
    llm_calls_count: int = 0
    fail_open: bool = False

    @property
    def gaps(self) -> list[tuple[str, list[str]]]:
        """有未覆盖侧面的角色 → [(character_name, uncovered_contexts), ...]。"""
        return [
            (it.character_name, it.uncovered_contexts)
            for it in self.items
            if it.uncovered_contexts
        ]


class _IdentityCoverageItemPayload(BaseModel):
    """LLM 输出的单条结论(批量 results 数组元素)。"""

    index: int = Field(..., ge=1, description="对应候选角色列表 1-based 序号")
    uncovered_contexts: list[str] = Field(default_factory=list)
    evidence: str = Field(..., min_length=1, max_length=400)


class _IdentityCoverageBatchPayload(BaseModel):
    """LLM 输出的批量中间模型。"""

    results: list[_IdentityCoverageItemPayload] = Field(default_factory=list)


def validate_identity_coverage_batch(
    *,
    candidates: list[IdentityCoverageCandidate],
    all_events: list[str],
    workspace_id: str | None = None,
    router: ModelRouter | None = None,
    client: LLMClient | None = None,
    model: str | None = None,
    max_tokens: int = _DEFAULT_MAX_TOKENS,
    temperature: float = _DEFAULT_TEMPERATURE,
    prompts_dir: Path | None = None,
) -> IdentityCoverageBatchResult:
    """1 次批量 LLM 调用,判定全部候选角色各身份侧面的全书覆盖度。

    :param candidates: 多情境身份角色 list(``identity_facets`` ≥ 2)。**空 list →
        直接返空结果,不调 LLM**(0 次调用)。
    :param all_events: 全书各章 ``one_line_event`` 字面 list(给 judge 看哪些侧面被交代)。
    :param workspace_id: 工作区 ID,落 structlog 字段(可选)。
    :param router: ``ModelRouter`` 实例;给定时按
        ``router.route("identity_coverage_validator")`` 取 client + model + 生成参数。
    :param client: legacy 路径 LLM 客户端;``router=None`` 时必传。
    :param model: legacy 路径模型名;``router=None`` 时必传。
    :param max_tokens: legacy 路径 token 上限。
    :param temperature: legacy 路径温度。
    :param prompts_dir: 提示词模板根目录;``None`` 时取 ``settings.prompts_dir``。
    :return: ``IdentityCoverageBatchResult``。
    """
    if not candidates:
        return IdentityCoverageBatchResult(items=[], llm_calls_count=0)

    try:
        resolved_client, resolved_model, complete_kwargs = resolve_routing(
            agent_name="identity_coverage_validator",
            router=router,
            client=client,
            model=model,
            legacy_kwargs={"max_tokens": max_tokens, "temperature": temperature},
        )
        template_root = (
            prompts_dir if prompts_dir is not None else settings.prompts_dir
        ).resolve()
        system_text = load_prompt_template(template_root / _VALIDATOR_PROMPT_FILENAME)
        complete_kwargs = prepare_strict_json_output(
            client=resolved_client,
            model=resolved_model,
            output_model=_IdentityCoverageBatchPayload,
            schema_name="identity_coverage",
            complete_kwargs=complete_kwargs,
        )
    except Exception as exc:  # noqa: BLE001 — fail-open 包括 setup 阶段
        if isinstance(exc, HandoffError):
            raise
        _log.warning(
            "identity_coverage_validator_setup_failed_open",
            workspace_id=workspace_id,
            candidate_count=len(candidates),
            error_type=type(exc).__name__,
            error_message=str(exc),
        )
        return _all_covered(candidates, llm_calls_count=0)

    user_text = _build_user_message(candidates=candidates, all_events=all_events)
    user_text = handoff_user_text(
        resolved_client, stage="identity_coverage_validator",
        data={"candidates": candidates, "all_events": all_events}, original_task=user_text,
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
    except Exception as exc:  # noqa: BLE001 — fail-open
        if isinstance(exc, HandoffError):
            raise
        _log.warning(
            "identity_coverage_validator_failed_open",
            workspace_id=workspace_id,
            candidate_count=len(candidates),
            error_type=type(exc).__name__,
            error_message=str(exc),
        )
        return _all_covered(candidates, llm_calls_count=1)

    # 按 index 对齐 LLM 结论与候选;缺的候选默认空 uncovered(不误产 warn,护 §0)。
    # 同时把 LLM 返回的 uncovered_contexts 收敛到候选真实拥有的 context 集合内
    # (防 LLM 编造不存在的情境标签)。
    verdict_by_index: dict[int, _IdentityCoverageItemPayload] = {
        r.index: r for r in payload.results
    }
    items: list[IdentityCoverageVerdictItem] = []
    for i, cand in enumerate(candidates, start=1):
        valid_contexts = {f.context for f in cand.facets}
        hit = verdict_by_index.get(i)
        if hit is None:
            items.append(
                IdentityCoverageVerdictItem(
                    character_id=cand.character_id,
                    character_name=cand.character_name,
                    uncovered_contexts=[],
                    evidence="[fail-safe] LLM 未返回该候选结论,默认按全覆盖处理",
                )
            )
        else:
            uncovered = [c for c in hit.uncovered_contexts if c in valid_contexts]
            items.append(
                IdentityCoverageVerdictItem(
                    character_id=cand.character_id,
                    character_name=cand.character_name,
                    uncovered_contexts=uncovered,
                    evidence=hit.evidence,
                )
            )

    gaps = [
        (it.character_name, it.uncovered_contexts)
        for it in items
        if it.uncovered_contexts
    ]
    if gaps:
        _log.info(
            "identity_coverage_validator_gaps",
            workspace_id=workspace_id,
            gaps=gaps,
            candidate_count=len(candidates),
        )
    else:
        _log.debug(
            "identity_coverage_validator_ok",
            workspace_id=workspace_id,
            candidate_count=len(candidates),
        )

    return IdentityCoverageBatchResult(items=items, llm_calls_count=1, fail_open=False)


def _all_covered(
    candidates: list[IdentityCoverageCandidate], *, llm_calls_count: int
) -> IdentityCoverageBatchResult:
    """fail-open:全部候选按全覆盖返回(不产 uncovered warn)。"""
    return IdentityCoverageBatchResult(
        items=[
            IdentityCoverageVerdictItem(
                character_id=c.character_id,
                character_name=c.character_name,
                uncovered_contexts=[],
                evidence="[fail-open] LLM / setup 异常,默认按全覆盖处理",
            )
            for c in candidates
        ],
        llm_calls_count=llm_calls_count,
        fail_open=True,
    )


def _build_user_message(
    *,
    candidates: list[IdentityCoverageCandidate],
    all_events: list[str],
) -> str:
    """拼装 user message:候选角色(含各身份侧面)+ 全书事件 + agent_role marker。"""
    lines: list[str] = []
    for i, cand in enumerate(candidates, start=1):
        facet_block = "; ".join(f"{f.context}={f.identity}" for f in cand.facets)
        lines.append(
            f"{i}. 角色:{cand.character_name}({cand.character_id})\n"
            f"   身份侧面(每一侧都应在全书至少被交代一次):{facet_block}"
        )
    candidates_block = "\n".join(lines)
    events_block = (
        "\n".join(f"  {idx}. {e}" for idx, e in enumerate(all_events, start=1))
        if all_events
        else "  (空)"
    )
    body = (
        "## 候选角色(逐个判定其各身份侧面的全书覆盖度)\n\n"
        f"{candidates_block}\n\n"
        "## 全书各章事件(one_line_event,按章序)\n\n"
        f"{events_block}\n"
    )
    return append_role_marker(body, role="identity_coverage_validator")


def _parse_llm_payload(
    *, raw_text: str, model_used: str
) -> _IdentityCoverageBatchPayload:
    """剥代码块 → ``json.loads`` → Pydantic 校验。失败抛 ``ValueError``。"""
    text = _strip_code_fence(raw_text).strip()
    try:
        data = loads_with_quote_repair(
            text,
            agent_name="identity_coverage_validator",
            model_used=model_used,
        )
    except json.JSONDecodeError as exc:
        raise ValueError(
            f"identity_coverage_validator LLM 输出非合法 JSON(model={model_used}): "
            f"{raw_text[:200]!r}"
        ) from exc
    try:
        return _IdentityCoverageBatchPayload.model_validate(data)
    except ValidationError as exc:
        raise ValueError(
            f"identity_coverage_validator LLM 输出 schema 不符(model={model_used}): "
            f"{exc.errors()}"
        ) from exc


def _strip_code_fence(text: str) -> str:
    """剥 trailing HTML 注释 + ``` / ```json 包裹(沿用 thread_closure_validator 同型)。"""
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
    "IdentityCoverageCandidate",
    "IdentityCoverageVerdictItem",
    "IdentityCoverageBatchResult",
    "validate_identity_coverage_batch",
]
