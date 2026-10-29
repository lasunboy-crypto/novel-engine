"""must_feasibility LLM-as-judge validator(v1.1 acceptance 修复2 / Fix D,2026-06-10)。

职责:在 ``orchestrate_chapter`` **写前**(writer 动笔之前)逐条判定本章
``ChapterCard.must_achieve`` 在已写正文事实下是否仍然可达成 —— 某条 must 已被
**前文消费**(同一事件在前章已发生并解决完毕)时,本章正文永不可达成 → 写后
``must_achieve_validator`` 会 4/4 正确拒绝 → rewrite budget 耗尽 → 整章
``human_intervention`` 报废。写前识别可省整轮浪费(4 版 writer + reviewer +
validators),提前升级人工裁决。

现场(luo-accept-v1 ch-0007,2026-06-10):must"强行压制副人格救下流浪汉"已被
ch-0006 消费(arc_beats 已含解决"掰开手指+砸头")→ 4 版重写全败 must_achieve
→ budget 耗尽。修复1 Fix B/C 在 outline 期拦截卷界重演(主要根因),本 validator
是**写作期最后一道网**(兜 Fix C 粗筛漏过的改写式重演 + 非卷界的一般性消费)。

与写后 ``must_achieve_validator`` 的判定方向刻意相反
====================================================

- 写后:验收已写正文,``uncertain`` **视作 missing**(宁严 — 已花成本,放水
  即质量事故);
- 写前(本 validator):还没动笔,``uncertain`` / 低置信 **放行写作**(宁宽 —
  误杀健康章 = 把正常写作流错误送进人工介入;写后还有兜底)。

仅 ``verdict=="consumed"`` 且 ``confidence >= MUST_INFEASIBLE_CONFIDENCE_THRESHOLD``
才产出 finding(检出后 orchestrator 升 ``human_intervention``,
``escalation_reason="must_infeasible_pre_write"``,属 §0 判据"真矛盾才 hard")。

fail-open 语义(对齐 volume_seam_replay_validator):LLM / setup / 解析异常、
LLM 输出条数与输入不齐 → ``passed=True`` + ``fail_open=True``,放行写作不阻断
(judge 挂了不应让用户写不了章)。上下文全空(无前章卡 + 无前章摘要,典型 =
全书首章)→ skip,零 LLM 调用。

路由:agent_name ``"must_feasibility_validator"`` → 已注册
``LLM_AS_JUDGE_AGENTS``(thinking-on hard guard 保护,"同一事件 vs 后果延续 vs
可转述达成"语义区分需深度斟酌);config.yaml
``agent_routing.must_feasibility_validator: cloud_qwen_max_thinking``。
"""

from __future__ import annotations

import re
from dataclasses import dataclass, field
from pathlib import Path
from typing import TYPE_CHECKING, Final, Literal

import structlog
from pydantic import BaseModel, Field

from src.config import settings
from src.core.agents._json_contract import loads_with_quote_repair
from src.core.agents._prompt import (
    append_role_marker,
    load_prompt_template,
    resolve_routing,
)
from src.core.agents._structured_output import prepare_strict_json_output
from src.core.llm.base import LLMClient, Message
from src.core.models.review import ConstraintResult, stable_constraint_id
from src.core.routing.router import ModelRouter

if TYPE_CHECKING:
    from src.core.models.chapter_card import ChapterCard

_log = structlog.get_logger(__name__)

_VALIDATOR_PROMPT_FILENAME = "must_feasibility_validator_v1.md"

# 默认生成参数:N 条 must 批处理输出 JSON items,token 上限给宽;语义判定对采样
# 稳定性敏感,温度低。router 路径下由 profile 决定。
_DEFAULT_TEMPERATURE = 0.3
_DEFAULT_MAX_TOKENS = 2000

#: 高置信门槛:LLM 判 ``consumed`` 且 confidence ≥ 本值才算 infeasible finding。
#: 写前误杀的代价是"健康章被错误送进人工介入"(维护人需手动恢复),写后兜底
#: (must_achieve_validator)仍在 → 阈值取高、宁漏不误杀。
MUST_INFEASIBLE_CONFIDENCE_THRESHOLD: Final[float] = 0.8

#: 写前预检注入的近章摘要条数上限(orchestrator 预读;"已被消费"几乎总发生在
#: 紧邻前章〔卷界重演场景〕,更早的消费由摘要覆盖,3 条够用且控 token)。
RECENT_SUMMARIES_FOR_PRECHECK: Final[int] = 3

# 匹配 ```json ... ``` 或 ``` ... ``` 包裹(沿用 volume_seam_replay 同型剥离)。
_CODE_FENCE_RE = re.compile(
    r"^\s*```(?:json)?\s*\n(?P<body>.*?)\n```\s*$",
    re.DOTALL,
)


@dataclass(frozen=True)
class MustFeasibilityFinding:
    """单条高置信不可达 must(verdict=consumed 且过置信门槛才产出)。"""

    must_text: str
    confidence: float
    evidence: str
    #: 给人工编辑的处置建议(prompt 要求 consumed 时必给;孤儿章 UI 展示)。
    suggestion: str


@dataclass(frozen=True)
class MustFeasibilityResult:
    """validator 输出。

    - ``passed``:``findings`` 为空时 True(含 skip 与全部 fail-open 的情况)。
    - ``findings``:高置信不可达 must 列表。
    - ``items_total``:输入 must_achieve 条数。
    - ``llm_calls_count``:实际 LLM 调用次数(0 或 1,批处理单次)。
    - ``skipped``:True 当且仅当 must 为空或上下文全空(零 LLM)。
    - ``fail_open``:LLM / setup / 解析异常放行。
    """

    passed: bool
    findings: list[MustFeasibilityFinding] = field(default_factory=list)
    constraint_results: list[ConstraintResult] = field(default_factory=list)
    items_total: int = 0
    llm_calls_count: int = 0
    skipped: bool = False
    fail_open: bool = False


class _FeasibilityItemPayload(BaseModel):
    """LLM 输出单条的中间模型(校验 5 字段)。"""

    must: str
    verdict: Literal["feasible", "consumed", "uncertain"]
    confidence: float = Field(..., ge=0.0, le=1.0)
    evidence: str = Field(..., max_length=600)
    suggestion: str = Field("", max_length=400)


class _FeasibilityLLMPayload(BaseModel):
    items: list[_FeasibilityItemPayload]


def _render_prev_card_context(prev_card: ChapterCard) -> str:
    """渲染前章卡的"已完结收束"上下文(对齐 volume_seam_replay prev 侧素材)。"""
    resolved = (
        ";".join(prev_card.arc_beats.values()) if prev_card.arc_beats else "(无标注)"
    )
    musts = ";".join(prev_card.must_achieve) if prev_card.must_achieve else "(无)"
    return (
        f"### 前一章计划({prev_card.chapter_id})\n"
        f"- 前章必达事件(已写入正文):{musts}\n"
        f"- 前章内已完结的收束:{resolved}\n"
    )


def _build_user_message(
    *,
    must_achieve: list[str],
    chapter_id: str,
    prev_card_context: str | None,
    recent_summaries: list[tuple[str, str, list[str]]],
) -> str:
    parts: list[str] = ["## 前文已写事实\n"]
    if prev_card_context:
        parts.append(prev_card_context)
    if recent_summaries:
        parts.append("### 近章正文摘要(已写定稿的事实)")
        # 每条:summary_text 一行 + 该章 unfinished_actions 缩进一行(w2-s3 补线
        # —— 摘要可能把"结局未知 / 表面收束"压成完成态,unfinished_actions 是抽取端
        # 独立兜住的悬念信号;判定时 must 命中前章未完结动作 = 该事件明确未收尾,
        # 本章延续 / 收尾它属合法,不应误报 consumed)。
        for cid, summary, unfinished in recent_summaries:
            parts.append(f"- {cid}:{summary}")
            if unfinished:
                joined = "；".join(unfinished)
                parts.append(
                    f"  - 该章未完结动作(尚未收尾,本章 must 延续 / 收尾它属合法、"
                    f"非重演):{joined}"
                )
    parts.append(f"\n## 待判定:本章({chapter_id})must_achieve\n")
    for i, must in enumerate(must_achieve, start=1):
        parts.append(f"{i}. {must}")
    parts.append(
        "\n请按系统提示词标准逐条判定可达成性,输出严格 JSON,"
        f"items 恰好 {len(must_achieve)} 条、顺序一致。"
    )
    return append_role_marker("\n".join(parts), role="must_feasibility_validator")


def _parse_llm_payload(
    raw_text: str,
    *,
    model_used: str,
) -> _FeasibilityLLMPayload:
    text = raw_text.strip()
    fence = _CODE_FENCE_RE.match(text)
    if fence:
        text = fence.group("body").strip()
    return _FeasibilityLLMPayload.model_validate(
        loads_with_quote_repair(
            text,
            agent_name="must_feasibility_validator",
            model_used=model_used,
        )
    )


def validate_must_feasibility(
    *,
    must_achieve: list[str],
    chapter_id: str,
    prev_chapter_card: ChapterCard | None = None,
    recent_summaries: list[tuple[str, str, list[str]]] | None = None,
    workspace_id: str | None = None,
    router: ModelRouter | None = None,
    client: LLMClient | None = None,
    model: str | None = None,
    max_tokens: int = _DEFAULT_MAX_TOKENS,
    temperature: float = _DEFAULT_TEMPERATURE,
    prompts_dir: Path | None = None,
) -> MustFeasibilityResult:
    """写前逐条判定 must_achieve 可达成性(批处理单次 LLM,fail-open)。

    :param must_achieve: 本章 ``ChapterCard.must_achieve``;空 → skip 零 LLM。
    :param chapter_id: 本章 ID(日志 + prompt 标识)。
    :param prev_chapter_card: 前一章卡(orchestrator 已为 continuity 预读复用);
        其 ``arc_beats``(已完结收束)是"已被消费"的最强证据。``None`` 容忍。
    :param recent_summaries: 近章 ``(chapter_id, summary_text, unfinished_actions)``
        三元组列表(caller 预读,建议 ``RECENT_SUMMARIES_FOR_PRECHECK`` 条);
        ``None``/空 容忍。``unfinished_actions``(w2-s3 补线)是抽取端独立兜住的
        悬念信号 —— 摘要可能把"结局未知 / 表面收束"压成完成态,该字段保留"事未完"
        的事实、渲染进 prompt,收窄"延续 / 收尾前文悬念"被误判 consumed 的面。
        与 ``prev_chapter_card`` **同时**为空(典型 = 全书首章)→ skip 零 LLM。
    :param workspace_id: 工作区 ID,落 structlog 字段(可选)。
    :param router: ``ModelRouter``;给定时按
        ``router.route("must_feasibility_validator")`` 取 client + 生成参数。
    :param client: legacy 路径 LLM 客户端;``router=None`` 时必传。
    :param model: legacy 路径模型名。
    :param max_tokens: legacy 路径 token 上限。
    :param temperature: legacy 路径温度。
    :param prompts_dir: 提示词模板根目录;``None`` 取 ``settings.prompts_dir``。
    :return: ``MustFeasibilityResult``。
    """
    if not must_achieve:
        return MustFeasibilityResult(passed=True, skipped=True)

    summaries = recent_summaries or []
    if prev_chapter_card is None and not summaries:
        # 全书首章 / 上下文不可得:无"前文事实"可对照,预检无意义,零成本跳过。
        return MustFeasibilityResult(
            passed=True, skipped=True, items_total=len(must_achieve)
        )

    try:
        resolved_client, resolved_model, complete_kwargs = resolve_routing(
            agent_name="must_feasibility_validator",
            router=router,
            client=client,
            model=model,
            legacy_kwargs={
                "max_tokens": max_tokens,
                "temperature": temperature,
            },
        )
        template_root = (
            prompts_dir if prompts_dir is not None else settings.prompts_dir
        ).resolve()
        system_text = load_prompt_template(template_root / _VALIDATOR_PROMPT_FILENAME)
        complete_kwargs = prepare_strict_json_output(
            client=resolved_client,
            model=resolved_model,
            output_model=_FeasibilityLLMPayload,
            schema_name="must_feasibility",
            complete_kwargs=complete_kwargs,
        )
    except Exception as exc:  # noqa: BLE001 — fail-open 包括 setup 阶段
        _log.warning(
            "must_feasibility_setup_failed_open",
            workspace_id=workspace_id,
            chapter_id=chapter_id,
            error_type=type(exc).__name__,
            error_message=str(exc)[:200],
        )
        return MustFeasibilityResult(
            passed=True, items_total=len(must_achieve), fail_open=True
        )

    prev_card_context = (
        _render_prev_card_context(prev_chapter_card)
        if prev_chapter_card is not None
        else None
    )
    try:
        response = resolved_client.complete(
            messages=[
                Message(role="system", content=system_text),
                Message(
                    role="user",
                    content=_build_user_message(
                        must_achieve=must_achieve,
                        chapter_id=chapter_id,
                        prev_card_context=prev_card_context,
                        recent_summaries=summaries,
                    ),
                ),
            ],
            model=resolved_model,
            **complete_kwargs,
        )
        payload = _parse_llm_payload(
            response.content,
            model_used=response.model_used,
        )
    except Exception as exc:  # noqa: BLE001 — fail-open(judge 挂不阻断写作)
        _log.warning(
            "must_feasibility_failed_open",
            workspace_id=workspace_id,
            chapter_id=chapter_id,
            error_type=type(exc).__name__,
            error_message=str(exc)[:200],
        )
        return MustFeasibilityResult(
            passed=True,
            items_total=len(must_achieve),
            llm_calls_count=1,
            fail_open=True,
        )

    if len(payload.items) != len(must_achieve):
        # 条数不齐 = LLM 输出不可信,整体 fail-open(对齐"宁漏不误杀")。
        _log.warning(
            "must_feasibility_items_mismatch_failed_open",
            workspace_id=workspace_id,
            chapter_id=chapter_id,
            expected=len(must_achieve),
            got=len(payload.items),
        )
        return MustFeasibilityResult(
            passed=True,
            items_total=len(must_achieve),
            llm_calls_count=1,
            fail_open=True,
        )

    findings: list[MustFeasibilityFinding] = []
    constraint_results: list[ConstraintResult] = []
    source_path = f"chapter_cards/{chapter_id}.must_achieve"
    for index, item in enumerate(payload.items):
        _log.info(
            "must_feasibility_judged",
            workspace_id=workspace_id,
            chapter_id=chapter_id,
            must=item.must[:80],
            verdict=item.verdict,
            confidence=item.confidence,
        )
        is_infeasible = (
            item.verdict == "consumed"
            and item.confidence >= MUST_INFEASIBLE_CONFIDENCE_THRESHOLD
        )
        authoritative_rule = must_achieve[index]
        constraint_results.append(
            ConstraintResult(
                constraint_id=stable_constraint_id(
                    source_path=source_path,
                    domain="must_achieve",
                    rule_text=authoritative_rule,
                ),
                domain="must_achieve",
                source_path=source_path,
                rule_text=authoritative_rule,
                status="fail" if is_infeasible else "pass",
                details=item.evidence,
            )
        )
        if is_infeasible:
            findings.append(
                MustFeasibilityFinding(
                    must_text=authoritative_rule,
                    confidence=item.confidence,
                    evidence=item.evidence,
                    suggestion=item.suggestion,
                )
            )

    return MustFeasibilityResult(
        passed=not findings,
        findings=findings,
        constraint_results=constraint_results,
        items_total=len(must_achieve),
        llm_calls_count=1,
    )


__all__ = [
    "MUST_INFEASIBLE_CONFIDENCE_THRESHOLD",
    "RECENT_SUMMARIES_FOR_PRECHECK",
    "MustFeasibilityFinding",
    "MustFeasibilityResult",
    "validate_must_feasibility",
]
