"""timeline_entry 语义合并 LLM-as-judge agent(v1.1 wave 2 session 5 A3 续,
2026-05-29 引入)。

职责:把 ``memory_extractor`` 抽出的本章 ``ExtractedTimelineEntry`` 列表与
工作区已有 ``TimelineEntry`` 表中的条目做语义合并判定 — **1 章 1 次综合 LLM
调用**(沿用 ``plot_thread_merger`` / LLM-as-judge 同型架构),识别本章新抽
timeline_entry 与既有 entry 描述同一事件不同表述(典型:同事件多视角 / 同
事件不同章节回顾),返回合并决策让 caller(``memory_extractor._persist_timeline``)
走"改 entry_id 走 update 路径"而非新建独立行。

为什么需要(预防性兜底,目前未观察到真实例):

luo-clean-v1 / luo-cloud-test 当前 audit 未观察到 timeline_entry 重复登记
(LLM 抽 entry_id 习惯 ``chapter_id-event-N`` 形态字面稳定不漂移),但 wave 4
30 章长跑场景跨章节回忆 / 倒叙片段重复描述同事件时,LLM 可能给同一事件
生成不同 entry_id → 新建独立行 → reviewer ``check_timeline_monotonic``
失败可能性升 + writer 检索 timeline 混乱。本 agent 作为预防性 LLM 兜底防御。

接口签名:::

    merge_timeline_entries_with_existing(
        *, extracted, existing,
        chapter_id, workspace_id=None,
        router=None, client=None, model=None,
        ...
    ) -> TimelineEntryMergeResult

调用方(``memory_extractor._persist_timeline`` 入口前置):本章抽取完拿到
``extracted: list[ExtractedTimelineEntry]`` 后,先调本 agent 拿合并决策 →
对命中合并的 extracted item,改 ``entry_id = target_existing_id`` 让 caller
自然走 get_timeline_entry hit + update 路径(覆盖字段)。

设计要点
========

1. **1 章 1 次综合 LLM 调用**(沿用 plot_thread_merger / LLM-as-judge 4 兄弟同型):
   - **1st 质量收益**:LLM 一次性综合判定 N × M 配对,识别"同事件不同视角 /
     同事件不同章节回顾 / 同事件不同时间标签描述"等场景
   - **3rd 工程便利**:每章 +1 次 LLM 调用,成本几乎不计

2. **沿用 cloud_qwen_max_thinking profile + 加入 LLM_AS_JUDGE_AGENTS 集合**:
   合并判定准确度依赖 thinking on 深度斟酌时间 / 事件语义匹配。

3. **existing top-K 候选筛选**(防 token 爆炸):caller 按 description 字面
   相似(``difflib.SequenceMatcher`` ratio > 0.3)或 time_label 相似筛 top-K
   (默认 K=10)。timeline 不需 character_disambiguator 的描述性指代兜底
   (entry 没有"指代"概念)。

4. **空输入跳过**:``extracted`` 为空 或 ``existing`` 为空 → 跳过 LLM 调用
   直接返 ``MergeResult(skipped=True, merge_decisions=[])``。

5. **LLM 失败 fail-open**(对齐 LLM-as-judge 同型):LLM 抛异常 / 非法 JSON /
   schema 不符 → 整个 merger 返 ``fail_open=True`` + ``merge_decisions=[]`` +
   log warn。**fail-open 语义 = 不合并,保守路径**(让本章 extracted 全部新建
   独立行,避免错误合并导致时间线乱)。

6. **agent_role marker = ``timeline_entry_merger``**:user message 末尾追加
   ``<!-- agent_role: timeline_entry_merger -->``,marker 不携带 chapter_id —
   MockLLMClient 识别后默认返 ``decisions: []`` no-merge JSON 让 mock 路径
   不影响既有 persist 行为。
"""

from __future__ import annotations

import json
import re
from dataclasses import dataclass, field
from pathlib import Path
from typing import TYPE_CHECKING, Literal

import structlog
from pydantic import BaseModel, Field, ValidationError

from src.config import settings
from src.core.agents._json_contract import (
    _repair_unescaped_inner_quotes as _shared_repair_unescaped_inner_quotes,
    loads_with_quote_repair,
)
from src.core.agents._prompt import (
    append_role_marker,
    load_prompt_template,
    resolve_routing,
)
from src.core.agents._structured_output import prepare_strict_json_output
from src.core.llm.base import LLMClient, Message
from src.core.models.timeline_entry import TimelineEntry

if TYPE_CHECKING:
    from src.core.routing.router import ModelRouter

_log = structlog.get_logger(__name__)

_MERGER_PROMPT_FILENAME = "timeline_entry_merger_v1.md"

_DEFAULT_TEMPERATURE = 0.3
_DEFAULT_MAX_TOKENS = 1500

_DEFAULT_TOP_K_EXISTING = 10

_CODE_FENCE_RE = re.compile(
    r"^\s*```(?:json)?\s*\n(?P<body>.*?)\n```\s*$",
    re.DOTALL,
)

_TRAILING_HTML_COMMENT_RE = re.compile(
    r"\s*<!--.*?-->\s*$",
    re.DOTALL,
)


@dataclass(frozen=True)
class TimelineEntryMergeDecision:
    """单条合并决策。"""

    extracted_entry_id: str
    target_existing_entry_id: str
    confidence: float
    reason: str


@dataclass(frozen=True)
class TimelineEntryMergeResult:
    """merger 输出。"""

    merge_decisions: list[TimelineEntryMergeDecision] = field(default_factory=list)
    evidence: str = ""
    llm_calls_count: int = 0
    skipped: bool = False
    fail_open: bool = False


class _DecisionPayload(BaseModel):
    """LLM 输出中单条 decision 字段。"""

    extracted_entry_id: str = Field(..., min_length=1)
    target_existing_entry_id: str = Field(..., min_length=1)
    confidence: float = Field(..., ge=0.0, le=1.0)
    reason: str = Field(..., min_length=1, max_length=200)


class _MergerLLMPayload(BaseModel):
    """LLM 输出的中间模型(校验 3 字段)。"""

    verdict: Literal["merge", "no_merge"]
    decisions: list[_DecisionPayload] = Field(default_factory=list)
    evidence: str = Field(..., min_length=1, max_length=400)


def merge_timeline_entries_with_existing(
    *,
    extracted: list[dict],
    existing: list[TimelineEntry],
    chapter_id: str,
    workspace_id: str | None = None,
    router: "ModelRouter | None" = None,
    client: LLMClient | None = None,
    model: str | None = None,
    max_tokens: int = _DEFAULT_MAX_TOKENS,
    temperature: float = _DEFAULT_TEMPERATURE,
    prompts_dir: Path | None = None,
) -> TimelineEntryMergeResult:
    """1 章 1 次综合 LLM 调用,判定本章 extracted 是否与 existing 是同事件。

    :param extracted: 本章 LLM 抽出的 timeline_entry 候选 list[dict],每条须含
        ``entry_id`` / ``description`` / ``time_label`` 三个 key
    :param existing: 工作区已有 ``TimelineEntry`` 实体 list(由 caller 从
        SQLite 读出 + top-K 候选筛选后传入)
    :param chapter_id: 章节 ID,落 structlog 字段
    :param workspace_id: 工作区 ID,落 structlog 字段(可选)
    :param router: ``ModelRouter`` 实例;给定时按
        ``router.route("timeline_entry_merger")`` 取 client + model + 生成参数
    :param client: legacy 路径 LLM 客户端;``router=None`` 时必传
    :param model: legacy 路径模型名;``router=None`` 时必传
    :param max_tokens: legacy 路径 token 上限
    :param temperature: legacy 路径温度
    :param prompts_dir: 提示词模板根目录;``None`` 时取 ``settings.prompts_dir``
    :return: ``TimelineEntryMergeResult``
    """
    if not extracted or not existing:
        _log.debug(
            "timeline_entry_merger_skipped",
            chapter_id=chapter_id,
            workspace_id=workspace_id,
            reason="empty_extracted_or_existing",
            extracted_count=len(extracted),
            existing_count=len(existing),
        )
        return TimelineEntryMergeResult(
            llm_calls_count=0,
            skipped=True,
        )

    try:
        resolved_client, resolved_model, complete_kwargs = resolve_routing(
            agent_name="timeline_entry_merger",
            router=router,
            client=client,
            model=model,
            legacy_kwargs={"max_tokens": max_tokens, "temperature": temperature},
        )
        template_root = (
            prompts_dir if prompts_dir is not None else settings.prompts_dir
        ).resolve()
        system_text = load_prompt_template(template_root / _MERGER_PROMPT_FILENAME)
        complete_kwargs = prepare_strict_json_output(
            client=resolved_client,
            model=resolved_model,
            output_model=_MergerLLMPayload,
            schema_name="timeline_entry_merger",
            complete_kwargs=complete_kwargs,
        )
    except Exception as exc:  # noqa: BLE001 — fail-open 包括 setup 阶段
        _log.warning(
            "timeline_entry_merger_setup_failed_open",
            chapter_id=chapter_id,
            workspace_id=workspace_id,
            error_type=type(exc).__name__,
            error_message=str(exc),
        )
        return TimelineEntryMergeResult(
            llm_calls_count=0,
            fail_open=True,
        )

    user_text = _build_user_message(
        extracted=extracted,
        existing=existing,
        chapter_id=chapter_id,
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
            "timeline_entry_merger_failed_open",
            chapter_id=chapter_id,
            workspace_id=workspace_id,
            error_type=type(exc).__name__,
            error_message=str(exc),
        )
        return TimelineEntryMergeResult(
            llm_calls_count=1,
            fail_open=True,
        )

    decisions = [
        TimelineEntryMergeDecision(
            extracted_entry_id=d.extracted_entry_id,
            target_existing_entry_id=d.target_existing_entry_id,
            confidence=d.confidence,
            reason=d.reason,
        )
        for d in payload.decisions
    ]

    extracted_ids = {item["entry_id"] for item in extracted}
    existing_ids = {te.entry_id for te in existing}
    valid_decisions = [
        d
        for d in decisions
        if d.extracted_entry_id in extracted_ids
        and d.target_existing_entry_id in existing_ids
    ]
    if len(valid_decisions) < len(decisions):
        _log.warning(
            "timeline_entry_merger_invalid_ids_filtered",
            chapter_id=chapter_id,
            workspace_id=workspace_id,
            raw_count=len(decisions),
            valid_count=len(valid_decisions),
        )

    if not valid_decisions:
        _log.debug(
            "timeline_entry_merger_no_merge",
            chapter_id=chapter_id,
            workspace_id=workspace_id,
            evidence=payload.evidence,
            extracted_count=len(extracted),
            existing_count=len(existing),
        )
    else:
        _log.info(
            "timeline_entry_merger_merge_decided",
            chapter_id=chapter_id,
            workspace_id=workspace_id,
            decisions_count=len(valid_decisions),
            decisions=[
                {
                    "extracted": d.extracted_entry_id,
                    "target": d.target_existing_entry_id,
                    "confidence": d.confidence,
                }
                for d in valid_decisions
            ],
            evidence=payload.evidence,
        )

    return TimelineEntryMergeResult(
        merge_decisions=valid_decisions,
        evidence=payload.evidence,
        llm_calls_count=1,
        skipped=False,
        fail_open=False,
    )


def select_top_k_existing_candidates(
    *,
    extracted: list[dict],
    existing: list[TimelineEntry],
    top_k: int = _DEFAULT_TOP_K_EXISTING,
) -> list[TimelineEntry]:
    """从 existing 中按 description 字面相似度筛 top-K 候选(防 LLM token 爆炸)。

    策略:对每条 extracted,计算它与每条 existing 的 description 相似度
    (``difflib.SequenceMatcher`` ratio),取所有 extracted × existing 对中
    最大相似度 ≥ 0.3 的 existing,按最大相似度降序取前 K 条。
    """
    import difflib

    if not extracted or not existing:
        return []

    scored: list[tuple[float, TimelineEntry]] = []
    for te in existing:
        max_ratio = 0.0
        for item in extracted:
            extracted_desc = item.get("description", "")
            ratio = difflib.SequenceMatcher(
                None, te.description, extracted_desc
            ).ratio()
            if ratio > max_ratio:
                max_ratio = ratio
        if max_ratio >= 0.3:
            scored.append((max_ratio, te))

    scored.sort(key=lambda x: x[0], reverse=True)
    return [te for _, te in scored[:top_k]]


def _build_user_message(
    *,
    extracted: list[dict],
    existing: list[TimelineEntry],
    chapter_id: str,
) -> str:
    """拼装 user message:extracted list + existing list + 上下文 + agent_role marker。"""
    extracted_lines = []
    for i, item in enumerate(extracted):
        extracted_lines.append(
            f"  [{i}] entry_id={item['entry_id']}\n"
            f"      time_label: {item.get('time_label', '')}\n"
            f"      description: {item.get('description', '')[:200]}"
        )
    extracted_block = "\n".join(extracted_lines)

    existing_lines = []
    for i, te in enumerate(existing):
        existing_lines.append(
            f"  [{i}] entry_id={te.entry_id}\n"
            f"      time_label: {te.time_label}\n"
            f"      chapter_id: {te.chapter_id}\n"
            f"      is_flashback: {te.is_flashback}\n"
            f"      description: {te.description[:200]}"
        )
    existing_block = "\n".join(existing_lines)

    body = (
        f"## 本章 (chapter_id={chapter_id}) 新抽取的 timeline_entry 候选\n\n"
        f"{extracted_block}\n\n"
        "## 工作区已有 timeline_entry(top-K 候选,按 description 相似度筛选)\n\n"
        f"{existing_block}\n"
    )
    return append_role_marker(body, role="timeline_entry_merger")


def _repair_unescaped_inner_quotes(text: str) -> str:
    """保留既有测试/诊断入口，实际实现统一到共享 JSON 合同。"""

    return _shared_repair_unescaped_inner_quotes(text)


def _parse_llm_payload(*, raw_text: str, model_used: str) -> _MergerLLMPayload:
    """剥代码块 → ``json.loads``(失败补救裸引号一次)→ Pydantic 校验。失败抛 ``ValueError``。"""
    text = raw_text.strip()
    while True:
        stripped = _TRAILING_HTML_COMMENT_RE.sub("", text)
        if stripped == text:
            break
        text = stripped
    match = _CODE_FENCE_RE.match(text)
    if match is not None:
        text = match.group("body")

    try:
        data = loads_with_quote_repair(
            text,
            agent_name="timeline_entry_merger",
            model_used=model_used,
        )
    except json.JSONDecodeError as exc:
        raise ValueError(
            f"timeline_entry_merger LLM 输出非合法 JSON (model={model_used}):"
            f"{exc.msg};raw={text[:200]!r}"
        ) from exc

    try:
        return _MergerLLMPayload.model_validate(data)
    except ValidationError as exc:
        raise ValueError(
            f"timeline_entry_merger LLM 输出 schema 不符 (model={model_used}):"
            f"{exc};raw={text[:200]!r}"
        ) from exc
