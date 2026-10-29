"""ending_tension 终局张力曲线 validator(v1.1 wave 6 session 4,B6)。

职责:**仅全书终章**触发,判定结局是否出现"**与前文张力断裂**"——典型 bug D5:
高潮后旁白**强行宣告岁月静好**,张力骤跌且无铺垫(luo-clean-v2 ch-30)。**关键护
留白**:抓的是"**断裂 / 强行**",**不是"平静收尾"本身**(自然的、有铺垫的平静
收束是成熟手法,绝不报)。

判据(对齐 [docs/plan/v1_1_wave6_design.md](../../../docs/plan/v1_1_wave6_design.md)
§2 B6 行 + §0 第一原则)
================================================================

- ``earned``:结局的情绪落点(无论平静还是高昂)**有铺垫、是前文张力的自然收束** →
  合格,**不报**(护留白:平静收尾合法)。
- ``abrupt``:结局**强行**拉平张力 / 旁白生硬宣告平安,与前文高潮**断裂**且无过渡 →
  唯一该标记的 WARN。

**为何 WARN-only 不 HARD**:"平静是自然收束还是强行断裂"是文学判断,LLM-judge 可能
误把有意的余韵 / 留白当断裂(§0 第一原则"宁可漏报也不误伤")。HARD 升级留 wave 6 末
约束预算实测后定。

全 SOFT / WARN-only + 仅终章
===========================

caller(``chapter_orchestrator._apply_ending_tension_validator``)只在**全书最后一章**
调用,abrupt 时把 evidence 合并进 ``review.suggestions``,**不覆盖 verdict**。非终章
不调用(0 成本)。设计要点沿用 scene_anchor_validator 同型(单 verdict + evidence)。
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

_VALIDATOR_PROMPT_FILENAME = "ending_tension_validator_v1.md"

_DEFAULT_TEMPERATURE = 0.3
_DEFAULT_MAX_TOKENS = 800

# 终章正文判定窗口:结局张力落点通常在章末,取尾部 + 头部能覆盖(取前后各一段)。
# 简化取前 _CHAPTER_MAX_CHARS 字(终章 word_count 通常 2000-4000,多数全量)。
_CHAPTER_MAX_CHARS = 6000

_CODE_FENCE_RE = re.compile(
    r"^\s*```(?:json)?\s*\n(?P<body>.*?)\n```\s*$",
    re.DOTALL,
)
_TRAILING_HTML_COMMENT_RE = re.compile(
    r"\s*<!--.*?-->\s*$",
    re.DOTALL,
)

EndingTensionVerdict = Literal["earned", "abrupt"]


@dataclass(frozen=True)
class EndingTensionResult:
    """validator 输出。

    - ``passed``:``verdict == "earned"`` 时 True。LLM / setup 失败 / skip → True(fail-open)。
    - ``verdict``:``earned``(自然收束)/ ``abrupt``(强行断裂,WARN)。
    - ``evidence``:判定依据(人类可读)。
    - ``llm_calls_count`` / ``skipped`` / ``fail_open``:同其它 wave 6 validator 语义。
    """

    passed: bool
    verdict: EndingTensionVerdict = "earned"
    evidence: str = ""
    llm_calls_count: int = 0
    skipped: bool = False
    fail_open: bool = False


class _EndingTensionPayload(BaseModel):
    """LLM 输出中间模型。"""

    verdict: EndingTensionVerdict
    evidence: str = Field(..., min_length=1, max_length=300)


def validate_ending_tension(
    *,
    chapter_text: str,
    tension_level: int,
    chapter_id: str,
    workspace_id: str | None = None,
    router: ModelRouter | None = None,
    client: LLMClient | None = None,
    model: str | None = None,
    max_tokens: int = _DEFAULT_MAX_TOKENS,
    temperature: float = _DEFAULT_TEMPERATURE,
    prompts_dir: Path | None = None,
) -> EndingTensionResult:
    """1 次 LLM 调用,判定终章结局是否与前文张力断裂(强行岁月静好)。

    :param chapter_text: 终章正文(取前 ``_CHAPTER_MAX_CHARS`` 字)。
    :param tension_level: 终章章节卡的预期张力等级(1-10),供 judge 参照。
    :param chapter_id: 章节 ID,落 structlog。
    :return: ``EndingTensionResult``。
    """
    if not chapter_text.strip():
        return EndingTensionResult(passed=True, skipped=True, llm_calls_count=0)

    try:
        resolved_client, resolved_model, complete_kwargs = resolve_routing(
            agent_name="ending_tension_validator",
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
            output_model=_EndingTensionPayload,
            schema_name="ending_tension",
            complete_kwargs=complete_kwargs,
        )
    except Exception as exc:  # noqa: BLE001 — fail-open 包括 setup 阶段
        _log.warning(
            "ending_tension_validator_setup_failed_open",
            chapter_id=chapter_id,
            workspace_id=workspace_id,
            error_type=type(exc).__name__,
            error_message=str(exc),
        )
        return EndingTensionResult(passed=True, fail_open=True, llm_calls_count=0)

    user_text = _build_user_message(
        chapter_text=chapter_text[:_CHAPTER_MAX_CHARS],
        tension_level=tension_level,
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
            "ending_tension_validator_failed_open",
            chapter_id=chapter_id,
            workspace_id=workspace_id,
            error_type=type(exc).__name__,
            error_message=str(exc),
        )
        return EndingTensionResult(passed=True, fail_open=True, llm_calls_count=1)

    passed = payload.verdict == "earned"
    if passed:
        _log.debug(
            "ending_tension_validator_earned",
            chapter_id=chapter_id,
            workspace_id=workspace_id,
        )
    else:
        _log.info(
            "ending_tension_validator_abrupt",
            chapter_id=chapter_id,
            workspace_id=workspace_id,
            evidence=payload.evidence,
        )
    return EndingTensionResult(
        passed=passed,
        verdict=payload.verdict,
        evidence=payload.evidence,
        llm_calls_count=1,
        fail_open=False,
    )


def _build_user_message(*, chapter_text: str, tension_level: int) -> str:
    body = (
        "## 这是全书最后一章(终章)\n\n"
        f"章节卡预期张力等级(1-10):{tension_level}\n\n"
        "判定结局的情绪落点是**前文张力的自然收束(earned)**,还是**强行拉平 / "
        "旁白生硬宣告岁月静好、与前文断裂(abrupt)**。**平静收尾本身不是问题**,"
        "只有**强行 / 无铺垫的断裂**才报。\n\n"
        "## 终章正文\n\n"
        f"{chapter_text}\n"
    )
    return append_role_marker(body, role="ending_tension_validator")


def _parse_llm_payload(*, raw_text: str, model_used: str) -> _EndingTensionPayload:
    text = _strip_code_fence(raw_text).strip()
    try:
        data = loads_with_quote_repair(
            text,
            agent_name="ending_tension_validator",
            model_used=model_used,
        )
    except json.JSONDecodeError as exc:
        raise ValueError(
            f"ending_tension_validator LLM 输出非合法 JSON(model={model_used}): "
            f"{raw_text[:200]!r}"
        ) from exc
    try:
        return _EndingTensionPayload.model_validate(data)
    except ValidationError as exc:
        raise ValueError(
            f"ending_tension_validator LLM 输出 schema 不符(model={model_used}): "
            f"{exc.errors()}"
        ) from exc


def _strip_code_fence(text: str) -> str:
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
    "EndingTensionVerdict",
    "EndingTensionResult",
    "validate_ending_tension",
]
