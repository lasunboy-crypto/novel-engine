"""thread_closure LLM-as-judge validator(v1.1 wave 6 session 1,A4 核心)。

职责:**批处理**判定一组 long_thread(全书级伏笔)候选在结局区是否收口,治 B1
暴露的**大纲坑③(伏笔只设计"揭露"不设计"闭合")** —— thread 只挂到中段揭露、
结局章没挂闭合(B1 bug D4 界外真局埋了不收)。

**批处理(1 次 LLM 调用判全部候选)**:沿用 must_achieve_validator G9.3 范本
(v1.1 wave 2 session 6 从"逐条 N 次"改"批处理 1 次",−64% 调用)。结局区未挂线的
候选伏笔一次性喂 judge,既省 token 又避免 MockLLMClient 单实例 50 次调用护栏在
"outline + 章节卡"全流程测试里被顶爆。**候选为空 → 0 次 LLM 调用**(全部伏笔都在
结局区挂线 = 按计划收口,无需 judge)。

**三档 verdict(对齐 [docs/plan/v1_1_wave6_design.md](../../../docs/plan/v1_1_wave6_design.md)
§0 第一原则 + §1 A4 行 + bug 清单 C1 留白保护)**:

- ``closed``:伏笔在结局区有明确收口(揭晓 / 兑现 / 了结)。
- ``open_with_setup``:**有交代的开放结局**(作者明示留给希望 / 想象,且主线已闭合)。
  **合法** —— 对齐 bug 清单 C1(林晓妹妹"没救回"=科技总有办法 + 林晓愿意等 + 主线
  反派阴谋已摧毁)。判 ``open_with_setup`` 等同合格,**不报 warn**。
- ``dangling``:**埋了不收 / 无交代地断**(反复强调的悬念结局一句否认 / 直接消失)。
  唯一该标记的 WARN(caller 只 log,不 raise —— A4 WARN-only,护 §0 第一原则)。

**为何 WARN-only 不 HARD**:开放结局合法(C1),"有交代开放 vs 埋了不收"是文学判断,
LLM-judge 可能误判;§0 第一原则"宁可漏报也不误伤留白"。真正治本靠 mini_plan prompt
**上移**(要求每条 long_thread 设计收口);本 judge 是观测兜底。HARD 升级留 wave 6 末
约束预算实测后定(§4)。

接口签名::

    validate_thread_closure_batch(
        *, candidates, ending_events,
        workspace_id=None,
        router=None, client=None, model=None,
        ...
    ) -> ThreadClosureBatchResult

调用方(outline_builder ``_check_thread_closure`` 全书编排):筛"结局区未挂线"的候选
thread → 一次性调本 agent → dangling 累计 log warn。编排逻辑(候选筛选 / 结局区判定)
在 outline_builder,本模块只做批量 LLM-judge(对齐 beat_alignment_validator 职责切分)。

设计要点(沿用 must_achieve_validator / beat_alignment_validator 同型架构)
=========================================================================

1. **1 次批量 LLM 调用**:全部候选 thread + 结局区事件一次性看完综合判定。
2. **沿用 *_validator 路由**:agent_name ``"thread_closure_validator"`` →
   ``agent_routing.thread_closure_validator``(config.yaml,cloud_qwen_max_thinking);
   未显式配则 ``resolve_routing`` fallback 走 reviewer profile。
3. **LLM 失败 fail-open**:LLM 抛异常 / 非法 JSON / setup 失败 → 全部候选 ``closed``
   (fail-open 不产 dangling warn;§0 宁可漏报不误伤)。返回的 result 数与候选数不符时,
   按 index 对齐,缺的候选默认 ``closed``(不误产 dangling)。
4. **agent_role marker = ``thread_closure_validator``(无 chapter_id):MockLLMClient
   识别 marker 按 ``## 候选伏笔列表`` 编号数返对应数量 ``closed`` JSON。
"""

from __future__ import annotations

import json
import re
from dataclasses import dataclass, field
from pathlib import Path
from typing import Literal

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
from src.core.routing.router import ModelRouter

_log = structlog.get_logger(__name__)

_VALIDATOR_PROMPT_FILENAME = "thread_closure_validator_v1.md"

# 默认生成参数:综合判定对采样稳定性敏感,温度低;批量输出含 N 个 {verdict + str},
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

ThreadClosureVerdict = Literal["closed", "open_with_setup", "dangling"]


@dataclass(frozen=True)
class ThreadClosureCandidate:
    """单条候选伏笔的输入(结局区未挂线,待 judge)。"""

    thread_title: str
    thread_description: str
    last_hooked_chapter: str | None = None


@dataclass(frozen=True)
class ThreadClosureVerdictItem:
    """单条候选的 judge 结论。"""

    thread_title: str
    verdict: ThreadClosureVerdict
    evidence: str = ""


@dataclass(frozen=True)
class ThreadClosureBatchResult:
    """批量 validator 输出。

    - ``items``:逐候选结论(顺序与入参 ``candidates`` 对齐)。
    - ``llm_calls_count``:实际 LLM 调用次数;候选为空 → 0;正常 / fail-open → 1。
    - ``fail_open``:LLM / setup 异常导致 fail-open(全部 closed)时 True。
    """

    items: list[ThreadClosureVerdictItem] = field(default_factory=list)
    llm_calls_count: int = 0
    fail_open: bool = False

    @property
    def dangling_titles(self) -> list[str]:
        return [it.thread_title for it in self.items if it.verdict == "dangling"]


class _ThreadClosureItemPayload(BaseModel):
    """LLM 输出的单条结论(批量 results 数组元素)。"""

    index: int = Field(..., ge=1, description="对应候选列表 1-based 序号")
    verdict: ThreadClosureVerdict
    evidence: str = Field(..., min_length=1, max_length=400)


class _ThreadClosureBatchPayload(BaseModel):
    """LLM 输出的批量中间模型。"""

    results: list[_ThreadClosureItemPayload] = Field(default_factory=list)


def validate_thread_closure_batch(
    *,
    candidates: list[ThreadClosureCandidate],
    ending_events: list[str],
    workspace_id: str | None = None,
    router: ModelRouter | None = None,
    client: LLMClient | None = None,
    model: str | None = None,
    max_tokens: int = _DEFAULT_MAX_TOKENS,
    temperature: float = _DEFAULT_TEMPERATURE,
    prompts_dir: Path | None = None,
) -> ThreadClosureBatchResult:
    """1 次批量 LLM 调用,判定全部候选 thread 在结局区收口情况。

    :param candidates: 结局区未挂线的候选伏笔 list。**空 list → 直接返空结果,
        不调 LLM**(0 次调用)。
    :param ending_events: 结局区各章的 ``one_line_event`` 字面 list(给 judge 看是否收口)。
    :param workspace_id: 工作区 ID,落 structlog 字段(可选)。
    :param router: ``ModelRouter`` 实例;给定时按
        ``router.route("thread_closure_validator")`` 取 client + model + 生成参数。
    :param client: legacy 路径 LLM 客户端;``router=None`` 时必传。
    :param model: legacy 路径模型名;``router=None`` 时必传。
    :param max_tokens: legacy 路径 token 上限。
    :param temperature: legacy 路径温度。
    :param prompts_dir: 提示词模板根目录;``None`` 时取 ``settings.prompts_dir``。
    :return: ``ThreadClosureBatchResult``。
    """
    if not candidates:
        return ThreadClosureBatchResult(items=[], llm_calls_count=0)

    try:
        resolved_client, resolved_model, complete_kwargs = resolve_routing(
            agent_name="thread_closure_validator",
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
            output_model=_ThreadClosureBatchPayload,
            schema_name="thread_closure",
            complete_kwargs=complete_kwargs,
        )
    except Exception as exc:  # noqa: BLE001 — fail-open 包括 setup 阶段
        if isinstance(exc, HandoffError):
            raise
        _log.warning(
            "thread_closure_validator_setup_failed_open",
            workspace_id=workspace_id,
            candidate_count=len(candidates),
            error_type=type(exc).__name__,
            error_message=str(exc),
        )
        return _all_closed(candidates, llm_calls_count=0)

    user_text = _build_user_message(candidates=candidates, ending_events=ending_events)
    user_text = handoff_user_text(
        resolved_client, stage="thread_closure_validator",
        data={"candidates": candidates, "ending_events": ending_events}, original_task=user_text,
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
            "thread_closure_validator_failed_open",
            workspace_id=workspace_id,
            candidate_count=len(candidates),
            error_type=type(exc).__name__,
            error_message=str(exc),
        )
        return _all_closed(candidates, llm_calls_count=1)

    # 按 index 对齐 LLM 结论与候选;缺的候选默认 closed(不误产 dangling,护 §0)。
    verdict_by_index: dict[int, _ThreadClosureItemPayload] = {
        r.index: r for r in payload.results
    }
    items: list[ThreadClosureVerdictItem] = []
    for i, cand in enumerate(candidates, start=1):
        hit = verdict_by_index.get(i)
        if hit is None:
            items.append(
                ThreadClosureVerdictItem(
                    thread_title=cand.thread_title,
                    verdict="closed",
                    evidence="[fail-safe] LLM 未返回该候选结论,默认按 closed 处理",
                )
            )
        else:
            items.append(
                ThreadClosureVerdictItem(
                    thread_title=cand.thread_title,
                    verdict=hit.verdict,
                    evidence=hit.evidence,
                )
            )

    dangling = [it.thread_title for it in items if it.verdict == "dangling"]
    if dangling:
        _log.info(
            "thread_closure_validator_dangling",
            workspace_id=workspace_id,
            dangling_titles=dangling,
            candidate_count=len(candidates),
        )
    else:
        _log.debug(
            "thread_closure_validator_ok",
            workspace_id=workspace_id,
            candidate_count=len(candidates),
        )

    return ThreadClosureBatchResult(items=items, llm_calls_count=1, fail_open=False)


def _all_closed(
    candidates: list[ThreadClosureCandidate], *, llm_calls_count: int
) -> ThreadClosureBatchResult:
    """fail-open:全部候选按 closed 返回(不产 dangling warn)。"""
    return ThreadClosureBatchResult(
        items=[
            ThreadClosureVerdictItem(
                thread_title=c.thread_title,
                verdict="closed",
                evidence="[fail-open] LLM / setup 异常,默认按 closed 处理",
            )
            for c in candidates
        ],
        llm_calls_count=llm_calls_count,
        fail_open=True,
    )


def _build_user_message(
    *,
    candidates: list[ThreadClosureCandidate],
    ending_events: list[str],
) -> str:
    """拼装 user message:候选伏笔列表 + 结局区事件 + agent_role marker。"""
    lines: list[str] = []
    for i, cand in enumerate(candidates, start=1):
        last_hooked = cand.last_hooked_chapter or "(全书从未挂线 / 孤儿伏笔)"
        lines.append(
            f"{i}. 标题:{cand.thread_title}\n"
            f"   描述 + 埋 / 收意图:{cand.thread_description}\n"
            f"   最后被挂线的章:{last_hooked}"
        )
    candidates_block = "\n".join(lines)
    events_block = (
        "\n".join(f"  - {e}" for e in ending_events) if ending_events else "  (空)"
    )
    body = (
        "## 候选伏笔列表(逐条判定其结局区收口情况)\n\n"
        f"{candidates_block}\n\n"
        "## 结局区各章事件(one_line_event)\n\n"
        f"{events_block}\n"
    )
    return append_role_marker(body, role="thread_closure_validator")


def _parse_llm_payload(*, raw_text: str, model_used: str) -> _ThreadClosureBatchPayload:
    """剥代码块 → ``json.loads`` → Pydantic 校验。失败抛 ``ValueError``。"""
    text = _strip_code_fence(raw_text).strip()
    try:
        data = loads_with_quote_repair(
            text,
            agent_name="thread_closure_validator",
            model_used=model_used,
        )
    except json.JSONDecodeError as exc:
        raise ValueError(
            f"thread_closure_validator LLM 输出非合法 JSON(model={model_used}): "
            f"{raw_text[:200]!r}"
        ) from exc
    try:
        return _ThreadClosureBatchPayload.model_validate(data)
    except ValidationError as exc:
        raise ValueError(
            f"thread_closure_validator LLM 输出 schema 不符(model={model_used}): "
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


__all__ = [
    "ThreadClosureCandidate",
    "ThreadClosureVerdictItem",
    "ThreadClosureBatchResult",
    "ThreadClosureVerdict",
    "validate_thread_closure_batch",
]
