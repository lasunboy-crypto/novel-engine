"""plot_thread 语义合并 LLM-as-judge agent(v1.1 wave 2 session 5 A3 治本,
2026-05-29 引入)。

职责:把 ``memory_extractor`` 抽出的本章 ``ExtractedPlotThread`` 列表与工作区
已有 ``PlotThread`` 表中的 thread 做语义合并判定 — **1 章 1 次综合 LLM 调用**
(沿用 ``must_vs_forbidden_validator`` 同型架构),识别本章新 thread 与既有
thread 语义重复 / 互为别名 / 同一主线不同表述时,返回合并决策让 caller
(``memory_extractor._persist_plot_threads``)走"改 thread_id 走 update 路径"
而非新建独立行,避免 luo-clean-v1 ch-1~10 累积 27 条 plot_thread 里 70% 冗余
(7 个语义簇,每簇 2-5 条相似 thread,如 thread-1-totem-secret /
zhi-wang-zhe-bai-lian-chuan-cheng / zhi-wang-zhe-mi-mi 全是织网者组织主线)。

接口签名:::

    merge_plot_threads_with_existing(
        *, extracted, existing,
        chapter_id, workspace_id=None,
        router=None, client=None, model=None,
        ...
    ) -> PlotThreadMergeResult

调用方(``memory_extractor._persist_plot_threads`` 入口前置):本章抽取完拿到
``extracted: list[ExtractedPlotThread]`` 后,先调本 agent 拿合并决策 → 对命中
合并的 extracted thread,改 ``thread_id = target_existing_id`` 让 caller 走
既有 update 路径(覆盖字段)而非新建。

设计要点
========

1. **1 章 1 次综合 LLM 调用**(沿用 must_vs_forbidden_validator / beat_alignment_validator
   同型):
   - **1st 质量收益**:LLM 一次性看完本章 N 条 extracted + 工作区 M 条
     existing(top-K 候选),综合判定每条 extracted 是否与某条 existing 是
     同一伏笔,比逐对 N×M 次独立调用更全面(综合上下文识别"织网者白莲传承"
     与"织网者地下互助会"是同一组织)
   - **3rd 工程便利**:每章 +1 次 LLM 调用(成本 ~$0.001-0.005),30 章
     ~$0.03-0.15 USD,几乎不计

2. **沿用 cloud_qwen_max_thinking profile**(对齐 LLM-as-judge 4 兄弟同型;
   ``config.yaml agent_routing.plot_thread_merger`` 显式 thinking on)。
   加入 ``LLM_AS_JUDGE_AGENTS`` 集合 hard guard,防止误切到 thinking-off
   profile 导致合并判定漂移。

3. **existing top-K 候选筛选**(防 token 爆炸):wave 4 30 章后 existing
   可能 50+ 条,LLM 输入 token 会爆。设计:caller 传入 ``existing`` 列表前
   先按 title 字面相似(``difflib.SequenceMatcher`` ratio > 0.3)或关键字
   jaccard 重叠筛 top-K(默认 K=10,可调)。其他 0 相似度的 existing 直接
   跳过,不进 LLM input。

4. **空输入跳过**:``extracted`` 为空 或 ``existing`` 为空 → 跳过 LLM 调用
   直接返 ``MergeResult(skipped=True, merge_decisions=[])``。无 extracted
   无新内容,无 existing 无重复目标。

5. **LLM 失败 fail-open**(对齐 LLM-as-judge 4 兄弟同型):LLM 抛异常 / 非法
   JSON / schema 不符 → 整个 merger 返 ``fail_open=True`` + ``merge_decisions=[]``
   + log warn。**fail-open 语义 = 不合并,保守路径**(让本章 extracted 全部
   新建独立行,避免错误合并导致数据丢失;后续 wave 4 末 backfill 脚本期可
   补救)。

6. **agent_role marker = ``plot_thread_merger``**:user message 末尾追加
   ``<!-- agent_role: plot_thread_merger -->``,marker 不携带 chapter_id —
   MockLLMClient 识别后默认返 ``decisions: []`` no-merge JSON 让 mock 路径
   不影响既有 persist 行为。

7. **失败语义 caller 友好**:LLM 返合并决策时,caller 据此改 ``thread_id``;
   ``confidence < 0.7`` 的低置信度合并决策 caller **可选忽略**(本 agent 不
   过滤,留 caller 策略;v1.1 默认接入全合并,wave 4 末实证后再调阈值)。
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
from src.core.agents._json_contract import loads_with_quote_repair
from src.core.agents._prompt import (
    append_role_marker,
    load_prompt_template,
    resolve_routing,
)
from src.core.agents._structured_output import prepare_strict_json_output
from src.core.llm.base import LLMClient, Message
from src.core.models.plot_thread import PlotThread

if TYPE_CHECKING:
    from src.core.routing.router import ModelRouter

_log = structlog.get_logger(__name__)

_MERGER_PROMPT_FILENAME = "plot_thread_merger_v1.md"

# 默认生成参数:合并判定对采样稳定性敏感,温度低 + token 上限适中(JSON 含
# decisions list + evidence,约 300-1500 token,依赖 extracted/existing 数量)。
_DEFAULT_TEMPERATURE = 0.3
_DEFAULT_MAX_TOKENS = 1500

# existing 筛 top-K 候选的默认值(防 token 爆炸)。
_DEFAULT_TOP_K_EXISTING = 10

# 匹配 ```json ... ``` 或 ``` ... ``` 包裹;沿用 must_vs_forbidden_validator 同型剥离。
_CODE_FENCE_RE = re.compile(
    r"^\s*```(?:json)?\s*\n(?P<body>.*?)\n```\s*$",
    re.DOTALL,
)

# trailing HTML 注释剥离。
_TRAILING_HTML_COMMENT_RE = re.compile(
    r"\s*<!--.*?-->\s*$",
    re.DOTALL,
)


@dataclass(frozen=True)
class PlotThreadMergeDecision:
    """单条合并决策。"""

    extracted_thread_id: str
    target_existing_thread_id: str
    confidence: float
    reason: str


@dataclass(frozen=True)
class PlotThreadMergeResult:
    """merger 输出。

    - ``merge_decisions``:LLM 判定的合并决策 list;无合并时 ``[]``。
    - ``evidence``:LLM 给出的简短判定依据(中文)。
    - ``llm_calls_count``:实际发起的 LLM 调用次数;空字段跳过 → 0;正常 → 1;
      fail-open(LLM 抛异常)→ 1。
    - ``skipped``:为 True 当且仅当 extracted / existing 任一为空走"跳过"分支。
    - ``fail_open``:为 True 当且仅当 LLM 调用抛异常导致整体 fail-open。
    """

    merge_decisions: list[PlotThreadMergeDecision] = field(default_factory=list)
    evidence: str = ""
    llm_calls_count: int = 0
    skipped: bool = False
    fail_open: bool = False


class _DecisionPayload(BaseModel):
    """LLM 输出中单条 decision 字段。"""

    extracted_thread_id: str = Field(..., min_length=1)
    target_existing_thread_id: str = Field(..., min_length=1)
    confidence: float = Field(..., ge=0.0, le=1.0)
    reason: str = Field(..., min_length=1, max_length=200)


class _MergerLLMPayload(BaseModel):
    """LLM 输出的中间模型(校验 3 字段)。"""

    verdict: Literal["merge", "no_merge"]
    decisions: list[_DecisionPayload] = Field(default_factory=list)
    evidence: str = Field(..., min_length=1, max_length=400)


def merge_plot_threads_with_existing(
    *,
    extracted: list[dict],
    existing: list[PlotThread],
    chapter_id: str,
    workspace_id: str | None = None,
    router: "ModelRouter | None" = None,
    client: LLMClient | None = None,
    model: str | None = None,
    max_tokens: int = _DEFAULT_MAX_TOKENS,
    temperature: float = _DEFAULT_TEMPERATURE,
    prompts_dir: Path | None = None,
) -> PlotThreadMergeResult:
    """1 章 1 次综合 LLM 调用,判定本章 extracted 是否与 existing 语义重复。

    :param extracted: 本章 LLM 抽出的 plot_thread 候选 list[dict],每条须含
        ``thread_id`` / ``title`` / ``description`` 三个 key(其他字段 caller
        可选)
    :param existing: 工作区已有 ``PlotThread`` 实体 list(由 caller 从 SQLite
        读出 + top-K 候选筛选后传入)
    :param chapter_id: 章节 ID,落 structlog 字段
    :param workspace_id: 工作区 ID,落 structlog 字段(可选)
    :param router: ``ModelRouter`` 实例;给定时按
        ``router.route("plot_thread_merger")`` 取 client + model + 生成参数
    :param client: legacy 路径 LLM 客户端;``router=None`` 时必传
    :param model: legacy 路径模型名;``router=None`` 时必传
    :param max_tokens: legacy 路径 token 上限
    :param temperature: legacy 路径温度
    :param prompts_dir: 提示词模板根目录;``None`` 时取 ``settings.prompts_dir``
    :return: ``PlotThreadMergeResult``
    """
    # 决策 4:extracted / existing 任一为空 → 跳过 + decisions=[]
    if not extracted or not existing:
        _log.debug(
            "plot_thread_merger_skipped",
            chapter_id=chapter_id,
            workspace_id=workspace_id,
            reason="empty_extracted_or_existing",
            extracted_count=len(extracted),
            existing_count=len(existing),
        )
        return PlotThreadMergeResult(
            llm_calls_count=0,
            skipped=True,
        )

    # 决策 5 扩:setup 阶段失败也走 fail-open。
    try:
        resolved_client, resolved_model, complete_kwargs = resolve_routing(
            agent_name="plot_thread_merger",
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
            schema_name="plot_thread_merger",
            complete_kwargs=complete_kwargs,
        )
    except Exception as exc:  # noqa: BLE001 — fail-open 包括 setup 阶段
        _log.warning(
            "plot_thread_merger_setup_failed_open",
            chapter_id=chapter_id,
            workspace_id=workspace_id,
            error_type=type(exc).__name__,
            error_message=str(exc),
        )
        return PlotThreadMergeResult(
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
    except Exception as exc:  # noqa: BLE001 — 决策 5:fail-open
        _log.warning(
            "plot_thread_merger_failed_open",
            chapter_id=chapter_id,
            workspace_id=workspace_id,
            error_type=type(exc).__name__,
            error_message=str(exc),
        )
        return PlotThreadMergeResult(
            llm_calls_count=1,
            fail_open=True,
        )

    decisions = [
        PlotThreadMergeDecision(
            extracted_thread_id=d.extracted_thread_id,
            target_existing_thread_id=d.target_existing_thread_id,
            confidence=d.confidence,
            reason=d.reason,
        )
        for d in payload.decisions
    ]

    # 校验:LLM 返决策的 thread_id 必须分别在 extracted / existing 集合中。
    extracted_ids = {item["thread_id"] for item in extracted}
    existing_ids = {th.thread_id for th in existing}
    valid_decisions = [
        d
        for d in decisions
        if d.extracted_thread_id in extracted_ids
        and d.target_existing_thread_id in existing_ids
    ]
    if len(valid_decisions) < len(decisions):
        _log.warning(
            "plot_thread_merger_invalid_ids_filtered",
            chapter_id=chapter_id,
            workspace_id=workspace_id,
            raw_count=len(decisions),
            valid_count=len(valid_decisions),
        )

    if not valid_decisions:
        _log.debug(
            "plot_thread_merger_no_merge",
            chapter_id=chapter_id,
            workspace_id=workspace_id,
            evidence=payload.evidence,
            extracted_count=len(extracted),
            existing_count=len(existing),
        )
    else:
        _log.info(
            "plot_thread_merger_merge_decided",
            chapter_id=chapter_id,
            workspace_id=workspace_id,
            decisions_count=len(valid_decisions),
            decisions=[
                {
                    "extracted": d.extracted_thread_id,
                    "target": d.target_existing_thread_id,
                    "confidence": d.confidence,
                }
                for d in valid_decisions
            ],
            evidence=payload.evidence,
        )

    return PlotThreadMergeResult(
        merge_decisions=valid_decisions,
        evidence=payload.evidence,
        llm_calls_count=1,
        skipped=False,
        fail_open=False,
    )


def select_top_k_existing_candidates(
    *,
    extracted: list[dict],
    existing: list[PlotThread],
    top_k: int = _DEFAULT_TOP_K_EXISTING,
) -> list[PlotThread]:
    """从 existing 中按字面相似度筛 top-K 候选(防 LLM token 爆炸)。

    策略:对每条 extracted,计算它与每条 existing 的 title 相似度
    (``difflib.SequenceMatcher`` ratio),取所有 extracted × existing 对中
    最大相似度 ≥ 0.3 的 existing,按最大相似度降序取前 K 条。

    :param extracted: 本章 LLM 抽出的 plot_thread 候选(含 ``title`` key)
    :param existing: 工作区全部 ``PlotThread``
    :param top_k: 返回上限,默认 10
    :return: 筛选后的 existing 子集(按相似度降序)
    """
    import difflib

    if not extracted or not existing:
        return []

    scored: list[tuple[float, PlotThread]] = []
    for th in existing:
        max_ratio = 0.0
        for item in extracted:
            extracted_title = item.get("title", "")
            ratio = difflib.SequenceMatcher(None, th.title, extracted_title).ratio()
            if ratio > max_ratio:
                max_ratio = ratio
        if max_ratio >= 0.3:
            scored.append((max_ratio, th))

    scored.sort(key=lambda x: x[0], reverse=True)
    return [th for _, th in scored[:top_k]]


def _build_user_message(
    *,
    extracted: list[dict],
    existing: list[PlotThread],
    chapter_id: str,
) -> str:
    """拼装 user message:extracted list + existing list + 上下文 + agent_role marker。"""
    extracted_lines = []
    for i, item in enumerate(extracted):
        extracted_lines.append(
            f"  [{i}] thread_id={item['thread_id']}\n"
            f"      title: {item['title']}\n"
            f"      description: {item.get('description', '')[:200]}"
        )
    extracted_block = "\n".join(extracted_lines)

    existing_lines = []
    for i, th in enumerate(existing):
        existing_lines.append(
            f"  [{i}] thread_id={th.thread_id}\n"
            f"      title: {th.title}\n"
            f"      description: {th.description[:200]}"
        )
    existing_block = "\n".join(existing_lines)

    body = (
        f"## 本章 (chapter_id={chapter_id}) 新抽取的 plot_thread 候选\n\n"
        f"{extracted_block}\n\n"
        "## 工作区已有 plot_thread(top-K 候选,按 title 相似度筛选)\n\n"
        f"{existing_block}\n"
    )
    return append_role_marker(body, role="plot_thread_merger")


def _parse_llm_payload(*, raw_text: str, model_used: str) -> _MergerLLMPayload:
    """剥代码块 → ``json.loads`` → Pydantic 校验。失败抛 ``ValueError``。"""
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
            agent_name="plot_thread_merger",
            model_used=model_used,
        )
    except json.JSONDecodeError as exc:
        raise ValueError(
            f"plot_thread_merger LLM 输出非合法 JSON (model={model_used}):"
            f"{exc.msg};raw={text[:200]!r}"
        ) from exc

    try:
        return _MergerLLMPayload.model_validate(data)
    except ValidationError as exc:
        raise ValueError(
            f"plot_thread_merger LLM 输出 schema 不符 (model={model_used}):"
            f"{exc};raw={text[:200]!r}"
        ) from exc
