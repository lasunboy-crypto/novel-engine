"""MemoryHealthChecker agent(P4-B-09)。

职责:接收 caller 预读的 Character / PlotThread / TimelineEntry 三类硬记忆 +
近 N 章 chapter_id 列表 → 拼成 system + user 两条消息 → 调注入的
``LLMClient`` → 解析 LLM 输出的 JSON → 返回 ``MemoryHealthReport``。

为什么这次做
-------------
P4-B-09 兑现 OBSERVABILITY §3.1 ``memory_health_history`` 表 + §3.3 阈值告
警表"记忆健康度 critical → 流水线自动暂停 + 强制人工介入"+ PROJECT_SPEC
§2.2 字面"MemoryHealthChecker:记忆库健康度审计 / Qwen / P4" + §8 字面
"记忆健康度 critical → 人工裁决"。LLM 判定 4 档 severity(ok / minor /
warning / critical),输出三类问题清单(stale_threads / inactive_characters
/ conflicts),caller 持久化 + critical 时联动 WS 推送
(``memory_health_critical`` 复用 P4-B-08 ``system_alerts_channel()`` helper)。

设计要点
========

1. **预读数据接口**(P4-B-07 沉淀,P4-B-08 接力):agent 函数接 caller 预读
   的 ``list[Character]`` / ``list[PlotThread]`` / ``list[TimelineEntry]`` /
   ``list[str]`` 近 N 章 chapter_id;**不依赖** SQLAlchemy session /
   ``MemoryStore`` CRUD。caller(``MemoryHealthService`` / 测试)自己 query
   再调本函数。理由对齐 P4-B-07 ``validator_pipeline`` / P4-B-08 ``cost_guard``
   / ``threshold_alerts``:① 单测无需 in-memory SQLite;② ``src.core.agents``
   不污染 caller 的事务边界;③ 同一笔判定可被多个 caller 复用
   (API 端点 / 定时巡检 / 章节流水线 hook)。

2. **workspace_id 透传纪律**(P3-B-14 沉淀):``memory_health_check`` event
   字段 ``workspace_id`` 由 caller 显式透传,本模块不读
   ``Character.workspace_id`` / ``PlotThread.workspace_id`` 自动猜——三类
   实体可能跨工作区复制(P5 OutlineBuilder / P7 模板复用场景),其字段会
   与"操作工作区"漂移。对齐 ``memory_extractor.extract(..., workspace_id=)``
   / ``validator_pipeline.run_validator_pipeline(..., workspace_id=)`` 模式。

3. **LLM 输出契约**:JSON 4 字段 — ``severity`` 4 档 Literal + ``summary``
   字符串(可读总结)+ ``stale_threads`` / ``inactive_characters`` /
   ``conflicts`` 三类 issue 列表(每条 dict 含 ``id`` / ``reason`` 字段)。
   解析失败抛 ``MemoryHealthCheckerError``(带 ``raw_output`` /
   ``model_used``)+ structlog ``memory_health_checker_parse_failed`` 事件;
   对齐 ``memory_extractor`` / ``reviewer`` 同模式。

4. **prompt 模板**:``prompts/memory_health_checker.md``,通过
   ``load_prompt_template`` 加载 + lru cache(沿用 P1-B-22 共享路径)。
   **不附加 agent role marker**:本 agent 不参与 MockLLMClient verdict 路由
   (健康度检查是工作区元数据维度,与"写一章"流水线解耦),mock 路径直接
   走独立 fake LLM client(类似 P2-B-09 MemoryExtractor 单测模式)。

5. **structlog ``memory_health_check`` 事件**:OBSERVABILITY §2.4 字面已
   定义(``workspace_id / severity / issues_count``)。本任务直接埋点,**不
   增加 v2_wishlist 待补数**(对齐 P4-B-06 ``validator_failed`` / ``passed``
   §2.4 字面已定义事件直接埋点先例)。

6. **不做什么**:① 跨章一致性"语义"校验(归 Reviewer voice_consistency
   / P4-B-07 hard validators);② 自动修复 stale_threads(归 caller / 人
   工);③ 落 SQLite ``memory_health_history``(归 service 层);④ WS
   推送(归 service 层);⑤ 真 LLM client 构造(归 caller / deps factory)。

边界
====

- 三类列表为空 + ``recent_chapter_ids`` 为空 → LLM 仍调用,LLM 应返
  ``severity=ok`` + 三空 issue 列表;若 LLM 报错或返非合规 JSON 抛
  ``MemoryHealthCheckerError``。
- LLM 给的 issue 条目 ``id`` 不一定真存在于预读列表(LLM 可能虚构);本
  agent 不做交叉校验,信任 LLM 输出。caller 渲染时若需要可二次校验。
"""

from __future__ import annotations

import json
import re
from pathlib import Path
from typing import Any, Literal

import structlog
from pydantic import BaseModel, Field, ValidationError

from src.config import settings
from src.core.agents._json_contract import loads_with_quote_repair
from src.core.agents._prompt import load_prompt_template, resolve_routing
from src.core.agents._structured_output import prepare_strict_json_output
from src.core.llm.base import LLMClient, Message
from src.core.models.character import Character
from src.core.models.plot_thread import PlotThread
from src.core.models.timeline_entry import TimelineEntry
from src.core.routing.router import ModelRouter

_log = structlog.get_logger(__name__)

_CHECKER_PROMPT_FILENAME = "memory_health_checker.md"

# 健康度检查求稳;低温 + 中等输出。
_DEFAULT_TEMPERATURE = 0.2
_DEFAULT_MAX_TOKENS = 2000

# 复用 P1-B-11 reviewer / P2-B-09 memory_extractor 的"代码块包裹"剥离正则。
_CODE_FENCE_RE = re.compile(
    r"^\s*```(?:json)?\s*\n(?P<body>.*?)\n```\s*$",
    re.DOTALL,
)
_TRAILING_HTML_COMMENT_RE = re.compile(
    r"\s*<!--.*?-->\s*$",
    re.DOTALL,
)


MemoryHealthSeverity = Literal["ok", "minor", "warning", "critical"]
"""OBSERVABILITY §3.1 字面 4 档 severity(与 ``MemoryHealthCheck`` Pydantic
模型 + ``MemoryHealthCheckORM`` CHECK 约束三处同源)。"""


class MemoryHealthCheckerError(ValueError):
    """LLM 输出无法解析为合法健康度报告时抛出。

    保留原始输出 + 实际模型名,便于 caller 重试决策与 P7 调优时回查根因
    (对齐 ``MemoryExtractorError`` / ``ReviewerParseError`` 形态)。
    """

    def __init__(self, message: str, *, raw_output: str, model_used: str) -> None:
        super().__init__(message)
        self.raw_output = raw_output
        self.model_used = model_used


class MemoryHealthIssue(BaseModel):
    """单条健康度问题(LLM 输出的 ``stale_threads`` / ``inactive_characters``
    / ``conflicts`` 列表元素结构)。

    ``id`` 字段:对应 ``thread_id`` / ``character_id`` / ``entry_id`` 或
    LLM 给的简短稳定 ID;``reason`` 字段:人类可读的问题描述。
    """

    id: str = Field(..., description="问题主体的稳定 ID(thread / character / entry)")
    reason: str = Field(..., description="问题描述(人类可读)")


class MemoryHealthReport(BaseModel):
    """``check_memory_health`` 出参 — LLM 判定的完整报告。

    ``severity`` 4 档对齐 OBSERVABILITY §3.1 字面 ``memory_health_history.severity``
    CHECK 约束 + Pydantic ``MemoryHealthCheck.severity`` Literal。
    """

    severity: MemoryHealthSeverity = Field(..., description="健康度严重档(4 档)")
    summary: str = Field(..., description="总结性描述(人类可读)")
    stale_threads: list[MemoryHealthIssue] = Field(
        default_factory=list,
        description="陈旧伏笔(active 但近 N 章未提及)",
    )
    inactive_characters: list[MemoryHealthIssue] = Field(
        default_factory=list,
        description="不活跃角色(近 N 章未出场)",
    )
    conflicts: list[MemoryHealthIssue] = Field(
        default_factory=list,
        description="冲突(timeline 矛盾或 character 字段矛盾)",
    )
    model_used: str = Field(..., description="实际使用的模型名(来自 LLM 响应)")

    @property
    def issues_count(self) -> int:
        """三类 issue 数量合计(供 structlog ``memory_health_check`` 事件
        ``issues_count`` 字段 + ``MemoryHealthCheckORM.{stale_threads,inactive_characters,conflicts}_count``
        之和,方便对账)。"""
        return (
            len(self.stale_threads)
            + len(self.inactive_characters)
            + len(self.conflicts)
        )


class _CheckerLLMPayload(BaseModel):
    """LLM 输出的中间模型(校验四字段必须出现 + 三类列表元素结构)。

    与 ``MemoryHealthReport`` 同形,但不含 ``model_used``(由本函数填入,
    不由 LLM 输出)。
    """

    severity: MemoryHealthSeverity
    summary: str
    stale_threads: list[MemoryHealthIssue]
    inactive_characters: list[MemoryHealthIssue]
    conflicts: list[MemoryHealthIssue]


def check_memory_health(
    *,
    workspace_id: str,
    characters: list[Character],
    plot_threads: list[PlotThread],
    timeline: list[TimelineEntry],
    recent_chapter_ids: list[str],
    router: ModelRouter | None = None,
    llm: LLMClient | None = None,
    model: str | None = "claude-sonnet-4-6",
    max_tokens: int = _DEFAULT_MAX_TOKENS,
    temperature: float = _DEFAULT_TEMPERATURE,
    prompts_dir: Path | None = None,
) -> MemoryHealthReport:
    """对一个工作区的记忆库做健康度审计。

    流程:
    1. 加载 ``prompts/memory_health_checker.md`` system 提示词 + 拼装 user
       消息(三类列表 + 近 N 章 chapter_id)。
    2. 调注入的 ``llm.complete(...)`` 拿到 LLM 响应。
    3. 剥代码块 / trailing 注释 → ``json.loads`` → ``_CheckerLLMPayload``
       校验。任一失败抛 ``MemoryHealthCheckerError``。
    4. 落 ``memory_health_check`` structlog 事件(§2.4 字面字段
       ``workspace_id`` / ``severity`` / ``issues_count``)+ 扩 ``model_used``
       便于 LogsPage trace。
    5. 返回 ``MemoryHealthReport``。

    :param workspace_id: 当前操作工作区 ID;**caller 显式透传**(P3-B-14
        沉淀,不读三类实体的 workspace_id 字段),供 structlog 事件 +
        caller 落表 + WS payload 同源。
    :param characters: caller 预读的角色列表(``list_characters_by_workspace``
        产出)。
    :param plot_threads: caller 预读的伏笔列表
        (``list_plot_threads_by_workspace`` 产出)。
    :param timeline: caller 预读的时间线条目列表
        (``list_timeline_entries_by_workspace`` 产出)。
    :param recent_chapter_ids: caller 预读的近 N 章 chapter_id(按
        ``ChapterORM.created_at`` desc 取),供 LLM 判定"stale""inactive"
        维度;空列表表达"工作区刚创建无写章"。
    :param router: P3-B-06 主路径。``ModelRouter`` 实例;给定时按
        ``router.route("memory_health_checker")`` 取 client + model + 生成参数。
    :param llm: legacy 路径 LLM 客户端;``router=None`` 时必传。
    :param model: legacy 路径模型名;``router=None`` 时必传。实际
        ``model_used`` 取响应里的字段。
    :param max_tokens: legacy 路径输出 token 上限,默认 2000;``router``
        路径下由 profile 决定。
    :param temperature: legacy 路径采样温度,默认 0.2;``router`` 路径下由
        profile 决定。
    :param prompts_dir: 提示词模板根目录;``None`` 时取
        ``settings.prompts_dir``。
    :raises MemoryHealthCheckerError: LLM 输出非合法 JSON / 缺字段 / 字段
        类型错。
    :raises TypeError: ``router=None`` 且 ``llm`` / ``model`` 任一缺失。
    :returns: ``MemoryHealthReport``——4 档 severity + 三类 issue 列表 +
        ``model_used``。
    """
    resolved_client, resolved_model, complete_kwargs = resolve_routing(
        agent_name="memory_health_checker",
        router=router,
        client=llm,
        model=model,
        legacy_kwargs={"max_tokens": max_tokens, "temperature": temperature},
    )
    complete_kwargs = prepare_strict_json_output(
        client=resolved_client,
        model=resolved_model,
        output_model=_CheckerLLMPayload,
        schema_name="memory_health_checker",
        complete_kwargs=complete_kwargs,
    )

    template_root = (
        prompts_dir if prompts_dir is not None else settings.prompts_dir
    ).resolve()
    system_text = load_prompt_template(template_root / _CHECKER_PROMPT_FILENAME)
    user_text = _build_user_message(
        workspace_id=workspace_id,
        characters=characters,
        plot_threads=plot_threads,
        timeline=timeline,
        recent_chapter_ids=recent_chapter_ids,
    )

    response = resolved_client.complete(
        messages=[
            Message(role="system", content=system_text),
            Message(role="user", content=user_text),
        ],
        model=resolved_model,
        **complete_kwargs,
    )

    payload = _parse_llm_payload(
        raw_text=response.content,
        model_used=response.model_used,
        workspace_id=workspace_id,
    )

    report = MemoryHealthReport(
        severity=payload.severity,
        summary=payload.summary,
        stale_threads=list(payload.stale_threads),
        inactive_characters=list(payload.inactive_characters),
        conflicts=list(payload.conflicts),
        model_used=response.model_used,
    )

    _log.info(
        "memory_health_check",
        workspace_id=workspace_id,
        severity=report.severity,
        issues_count=report.issues_count,
        stale_threads_count=len(report.stale_threads),
        inactive_characters_count=len(report.inactive_characters),
        conflicts_count=len(report.conflicts),
        model_used=response.model_used,
    )

    return report


def _build_user_message(
    *,
    workspace_id: str,
    characters: list[Character],
    plot_threads: list[PlotThread],
    timeline: list[TimelineEntry],
    recent_chapter_ids: list[str],
) -> str:
    """user 消息:三类列表序列化为人类可读 + LLM 可消化文本。

    设计:三类列表各成 ``## 字段名`` section,空列表显式标注"(无)"。
    timeline 按 ``time_order`` 升序展示(caller 已用
    ``list_timeline_entries_by_workspace`` 返出按 ``time_order`` 升序);
    plot_threads 仅展示 ``thread_id`` / ``title`` / ``status`` /
    ``importance`` / ``planted_chapter`` / ``last_updated_chapter`` 关键字
    段,full description 过长不传(token 优化);characters 仅展示
    ``character_id`` / ``name`` / ``current_state`` / ``last_updated_chapter``
    关键字段。
    """
    chars_block = _render_characters_block(characters)
    threads_block = _render_plot_threads_block(plot_threads)
    timeline_block = _render_timeline_block(timeline)
    recent_chapters_block = (
        ", ".join(recent_chapter_ids) if recent_chapter_ids else "(无,工作区尚无写章)"
    )

    return (
        "## 操作工作区\n\n"
        f"- 工作区 ID(workspace_id):{workspace_id}\n"
        f"- 近期章节列表(recent_chapter_ids,按 created_at desc):"
        f"{recent_chapters_block}\n\n"
        "## 角色清单(characters)\n\n"
        f"{chars_block}\n\n"
        "## 伏笔清单(plot_threads)\n\n"
        f"{threads_block}\n\n"
        "## 时间线条目(timeline,按 time_order 升序)\n\n"
        f"{timeline_block}\n"
    )


def _render_characters_block(characters: list[Character]) -> str:
    if not characters:
        return "(无)"
    lines: list[str] = []
    for c in characters:
        state_str = (
            ", ".join(f"{k}={v}" for k, v in sorted(c.current_state.items())) or "(空)"
        )
        lines.append(
            f"- character_id={c.character_id} | name={c.name} | "
            f"current_state={{{state_str}}} | "
            f"last_updated_chapter={c.last_updated_chapter or '(未填)'}"
        )
    return "\n".join(lines)


def _render_plot_threads_block(plot_threads: list[PlotThread]) -> str:
    if not plot_threads:
        return "(无)"
    lines: list[str] = []
    for t in plot_threads:
        lines.append(
            f"- thread_id={t.thread_id} | title={t.title} | "
            f"status={t.status} | importance={t.importance} | "
            f"planted_chapter={t.planted_chapter} | "
            f"last_updated_chapter={t.last_updated_chapter or '(未更新)'}"
        )
    return "\n".join(lines)


def _render_timeline_block(timeline: list[TimelineEntry]) -> str:
    if not timeline:
        return "(无)"
    lines: list[str] = []
    for e in timeline:
        chars = ", ".join(e.involved_characters) or "(无)"
        lines.append(
            f"- entry_id={e.entry_id} | time_order={e.time_order} | "
            f"time_label={e.time_label} | chapter_id={e.chapter_id} | "
            f"involved={chars} | description={e.description}"
        )
    return "\n".join(lines)


def _parse_llm_payload(
    *,
    raw_text: str,
    model_used: str,
    workspace_id: str,
) -> _CheckerLLMPayload:
    """剥代码块 + trailing → ``json.loads`` → Pydantic 校验。

    任一失败抛 ``MemoryHealthCheckerError`` 并落
    ``memory_health_checker_parse_failed`` 事件。
    """
    text = _strip_format_noise(raw_text).strip()

    try:
        data: Any = loads_with_quote_repair(
            text,
            agent_name="memory_health_checker",
            model_used=model_used,
        )
    except json.JSONDecodeError as exc:
        _log.error(
            "memory_health_checker_parse_failed",
            reason="invalid_json",
            raw_output=raw_text,
            model_used=model_used,
            workspace_id=workspace_id,
            json_error=str(exc),
        )
        raise MemoryHealthCheckerError(
            "MemoryHealthChecker LLM 输出非合法 JSON",
            raw_output=raw_text,
            model_used=model_used,
        ) from exc

    try:
        return _CheckerLLMPayload.model_validate(data)
    except ValidationError as exc:
        _log.error(
            "memory_health_checker_parse_failed",
            reason="schema_mismatch",
            raw_output=raw_text,
            model_used=model_used,
            workspace_id=workspace_id,
            validation_errors=exc.errors(),
        )
        raise MemoryHealthCheckerError(
            "MemoryHealthChecker LLM 输出 JSON 结构与 schema 不符",
            raw_output=raw_text,
            model_used=model_used,
        ) from exc


def _strip_format_noise(text: str) -> str:
    """剥 trailing HTML 注释 + ``` / ```json 包裹(沿用 P2-B-09
    ``memory_extractor._strip_format_noise`` 路径)。

    顺序:**先剥 trailing**(可能多个),**再剥 fence**。
    """
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
    "MemoryHealthCheckerError",
    "MemoryHealthIssue",
    "MemoryHealthReport",
    "MemoryHealthSeverity",
    "check_memory_health",
]
