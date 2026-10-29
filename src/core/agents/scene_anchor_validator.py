"""scene_anchor LLM-as-judge validator(v1.1 wave 6 session 3,B2 复核核心)。

职责:当代码粗筛标记"本章醒来点 ≠ 最近一次入梦点 且无位移交代"后,**复核**这到底是
真硬跳(肉体瞬移,bug)还是剧情自然差异 / 正文其实有隐含交代(护留白)。治 B1 30 章
长跑 bug B1/B2/B3(病房入梦→工厂醒 / 工厂密室→催眠舱瞬移 / 推演室→大桥指挥车)。

约束分层(对齐 [docs/plan/v1_1_wave6_design.md](../../../docs/plan/v1_1_wave6_design.md)
§2 B2 行)
==========

- **代码粗筛(gating)**:``_check_scene_anchor_continuity_safely``(orchestrator)按
  ``chapter_id`` 升序查最近一次入梦点,与本章醒来点做规范化字符串比对,不一致且
  ``has_transition_explanation`` 为 False → 候选 → 调本 agent 复核。
- **LLM 复核(本 agent)**:读入梦点 / 醒来点 / 本章正文片段,判 ``natural``(自然
  过渡 / 正文有隐含交代,护留白)/ ``teleport``(真肉体瞬移,无任何位移交代)/
  ``uncertain``。只有 ``teleport`` → caller WARN log。

全 WARN-only(对齐 wave 6 全 WARN 默认)
========================================

caller 对 ``teleport`` 只 ``log warn``,**不阻塞 / 不 rewrite**(extract 已在
APPROVED 后,纯观测)。**为何不 HARD**:"瞬移 vs 自然过渡"是文学语义判断,
LLM 可能误判(§0 第一原则"宁可漏报也不误伤")。HARD 升级留 wave 6 末实测。

设计要点(沿用 continuity_validator 同型单 verdict 架构)
======================================================

1. **1 次 LLM 调用**:入梦点 + 醒来点 + 本章正文片段一次性看完判定。
2. **沿用 *_validator 路由**:agent_name ``"scene_anchor_validator"`` →
   ``agent_routing.scene_anchor_validator``(config.yaml,cloud_qwen_max_thinking)。
3. **LLM 失败 fail-open**:LLM 抛异常 / 返非法 JSON / setup 失败 → ``verdict=natural``
   + log warn(§0 第一原则:宁可漏报,不误伤)。
4. **agent_role marker = ``scene_anchor_validator``(无 chapter_id):MockLLMClient
   识别 marker 返 ``natural`` 默认 JSON。
"""

from __future__ import annotations

import json
import re
from dataclasses import dataclass
from pathlib import Path
from typing import Literal

import structlog
from pydantic import BaseModel, Field, ValidationError

from src.config import settings
from src.core.agents._json_contract import loads_with_quote_repair
from src.core.agents._prompt import (
    append_role_marker,
    load_prompt_template,
    resolve_routing,
)
from src.core.agents._structured_output import prepare_strict_json_output
from src.core.llm.base import LLMClient, Message
from src.core.routing.router import ModelRouter

_log = structlog.get_logger(__name__)

_VALIDATOR_PROMPT_FILENAME = "scene_anchor_validator_v1.md"

_DEFAULT_TEMPERATURE = 0.3
_DEFAULT_MAX_TOKENS = 300

# 本章正文判定窗口(自决项):入梦点在上一锚点章,醒来点在本章开头,故取本章前段。
_CHAPTER_HEAD_CHARS = 1500

_CODE_FENCE_RE = re.compile(
    r"^\s*```(?:json)?\s*\n(?P<body>.*?)\n```\s*$",
    re.DOTALL,
)
_TRAILING_HTML_COMMENT_RE = re.compile(
    r"\s*<!--.*?-->\s*$",
    re.DOTALL,
)

_Verdict = Literal["natural", "teleport", "uncertain"]


@dataclass(frozen=True)
class SceneAnchorResult:
    """validator 输出。

    - ``verdict``:``natural``(自然过渡 / 有交代,合格)/ ``teleport``(真硬跳,
      caller WARN)/ ``uncertain``(视作 teleport 处置,对齐 continuity uncertain=fail)。
    - ``is_teleport``:``verdict in {"teleport", "uncertain"}`` —— caller 据此 WARN。
    - ``evidence``:LLM 判定依据。
    - ``llm_calls_count``:实际 LLM 调用次数;正常 → 1;fail-open → 0 或 1。
    - ``fail_open``:LLM / setup 异常导致 fail-open(verdict=natural)时 True。
    """

    verdict: _Verdict
    evidence: str = ""
    llm_calls_count: int = 0
    fail_open: bool = False

    @property
    def is_teleport(self) -> bool:
        return self.verdict in {"teleport", "uncertain"}


class _SceneAnchorLLMPayload(BaseModel):
    """LLM 输出的中间模型。"""

    verdict: _Verdict
    evidence: str = Field(..., min_length=1, max_length=200)


def validate_scene_anchor(
    *,
    enter_location: str,
    wake_location: str,
    chapter_text: str,
    chapter_id: str,
    enter_chapter_id: str = "",
    workspace_id: str | None = None,
    router: ModelRouter | None = None,
    client: LLMClient | None = None,
    model: str | None = None,
    max_tokens: int = _DEFAULT_MAX_TOKENS,
    temperature: float = _DEFAULT_TEMPERATURE,
    prompts_dir: Path | None = None,
) -> SceneAnchorResult:
    """1 次 LLM 调用,复核醒来点≠入梦点是否真硬跳(肉体瞬移)。

    :param enter_location: 最近一次入梦点(身体进入另一时空时所在的现实位置)。
    :param wake_location: 本章醒来点(意识返回时身体所在位置)。
    :param chapter_text: 本章正文(取前 ``_CHAPTER_HEAD_CHARS`` 字)。
    :param chapter_id: 本章 ID,落 structlog。
    :param enter_chapter_id: 入梦点所在章 ID(给 judge 作上下文,可选)。
    :param workspace_id: 工作区 ID,落 structlog(可选)。
    :return: ``SceneAnchorResult``。
    """
    try:
        resolved_client, resolved_model, complete_kwargs = resolve_routing(
            agent_name="scene_anchor_validator",
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
            output_model=_SceneAnchorLLMPayload,
            schema_name="scene_anchor",
            complete_kwargs=complete_kwargs,
        )
    except Exception as exc:  # noqa: BLE001 — fail-open 包括 setup 阶段
        _log.warning(
            "scene_anchor_validator_setup_failed_open",
            chapter_id=chapter_id,
            workspace_id=workspace_id,
            error_type=type(exc).__name__,
            error_message=str(exc),
        )
        return SceneAnchorResult(verdict="natural", fail_open=True, llm_calls_count=0)

    user_text = _build_user_message(
        enter_location=enter_location,
        wake_location=wake_location,
        chapter_head=chapter_text[:_CHAPTER_HEAD_CHARS],
        enter_chapter_id=enter_chapter_id,
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
        _log.warning(
            "scene_anchor_validator_failed_open",
            chapter_id=chapter_id,
            workspace_id=workspace_id,
            error_type=type(exc).__name__,
            error_message=str(exc),
        )
        return SceneAnchorResult(verdict="natural", fail_open=True, llm_calls_count=1)

    result = SceneAnchorResult(
        verdict=payload.verdict,
        evidence=payload.evidence,
        llm_calls_count=1,
        fail_open=False,
    )
    if result.is_teleport:
        _log.info(
            "scene_anchor_validator_teleport",
            chapter_id=chapter_id,
            workspace_id=workspace_id,
            enter_location=enter_location,
            wake_location=wake_location,
            verdict=payload.verdict,
            evidence=payload.evidence,
        )
    else:
        _log.debug(
            "scene_anchor_validator_natural",
            chapter_id=chapter_id,
            workspace_id=workspace_id,
            evidence=payload.evidence,
        )
    return result


def _build_user_message(
    *,
    enter_location: str,
    wake_location: str,
    chapter_head: str,
    enter_chapter_id: str,
) -> str:
    """拼装 user message:入梦点 / 醒来点 / 本章开头 + agent_role marker。"""
    enter_ctx = f"(记录于 {enter_chapter_id})" if enter_chapter_id else ""
    body = (
        "## 待复核的场景锚点\n\n"
        f"- 最近一次入梦点(身体进入另一时空时的现实位置){enter_ctx}:{enter_location}\n"
        f"- 本章醒来点(意识返回时身体所在位置):{wake_location}\n\n"
        "## 本章开头\n\n"
        f"{chapter_head}\n"
    )
    return append_role_marker(body, role="scene_anchor_validator")


def _parse_llm_payload(*, raw_text: str, model_used: str) -> _SceneAnchorLLMPayload:
    """剥代码块 → ``json.loads`` → Pydantic 校验。失败抛 ``ValueError``。"""
    text = _strip_code_fence(raw_text).strip()
    try:
        data = loads_with_quote_repair(
            text,
            agent_name="scene_anchor_validator",
            model_used=model_used,
        )
    except json.JSONDecodeError as exc:
        raise ValueError(
            f"scene_anchor_validator LLM 输出非合法 JSON(model={model_used}): "
            f"{raw_text[:200]!r}"
        ) from exc
    try:
        return _SceneAnchorLLMPayload.model_validate(data)
    except ValidationError as exc:
        raise ValueError(
            f"scene_anchor_validator LLM 输出 schema 不符(model={model_used}): "
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
    "SceneAnchorResult",
    "validate_scene_anchor",
]
