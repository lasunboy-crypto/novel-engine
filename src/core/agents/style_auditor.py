"""StyleAuditor agent —— 风格模板符合度审计(v1.1 wave 4 s5 / I2.2 落地)。

P4-B-10 留的 ``style_auditor_stub.py`` 纯函数占位升级为真 LLM agent
(candidates §I2 "StyleAuditor 落地 + 模板匹配度维度,检测章节产出 vs 模板
期望偏移")。

设计要点
========

- **对照源**:复用 I1 ``STYLE_TEMPLATES`` 10 维定性文案(修辞密度 / 对白比例 /
  调性等)作"模板期望",LLM 读章节正文片段 + 模板期望 → 输出 ``drift_score``
  (0-10,越高越偏离)+ 逐维 ``dimension_notes`` + ``summary``。
- **severity 确定性派生**:LLM **只产 drift_score**(避免 LLM 直接给档位时
  与分数自相矛盾),``severity`` 由 ``_severity_from_drift`` 按阈值映射
  (<2.5 ok / <5 minor / <7.5 warning / else critical)—— 对齐 reviewer
  "LLM 给分 + 代码判 verdict"模式,可单测、稳定。
- **无模板 / 无章节短路**:``style_template is None`` 或 ``chapter_excerpts``
  为空 → 不调 LLM,直接返 ``severity="ok"`` / ``drift_score=0.0`` +
  summary 说明"无基线可对照"。
- **fail-open**:setup / LLM / 解析失败 → 返 ``ok`` / drift=0 + summary 标注
  fail-open(审计是辅助任务,失败不阻断 API)。
- **独立审计,不入章节 verdict**:StyleAuditor 走独立端点 + 独立表,**不**参与
  chapter_orchestrator 的 reviewer/validator verdict 聚合 → 天然不碰 G8 治理。
- **structlog ``style_audit`` 事件**:字段 ``workspace_id`` / ``severity`` /
  ``drift_score`` / ``chapter_range_start`` / ``chapter_range_end`` /
  ``style_template``(沿用 stub 同事件名,供 LogsPage 对账)。
"""

from __future__ import annotations

import json
import re
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
from src.core.models.style_template import STYLE_TEMPLATES
from src.core.routing.router import ModelRouter

_log = structlog.get_logger(__name__)

_STYLE_AUDITOR_PROMPT_FILENAME = "style_auditor.md"

# 审计求稳:温度低;输出一份 JSON(drift_score + dimension_notes + summary)。
_DEFAULT_TEMPERATURE = 0.3
_DEFAULT_MAX_TOKENS = 1200

# 单段章节正文片段上限(字符),控审计 token;超出截断(取章首,最能体现风格基调)。
_EXCERPT_CHAR_LIMIT = 1500

StyleAuditSeverity = Literal["ok", "minor", "warning", "critical"]

# drift_score(0-10,越高越偏离模板期望)→ severity 4 档阈值。
_SEVERITY_THRESHOLDS: tuple[tuple[float, StyleAuditSeverity], ...] = (
    (2.5, "ok"),
    (5.0, "minor"),
    (7.5, "warning"),
)

# 匹配 ```json ... ``` 或 ``` ... ``` 包裹(沿用 reviewer / validator 同型剥离)。
_CODE_FENCE_RE = re.compile(
    r"^\s*```(?:json)?\s*\n(?P<body>.*?)\n```\s*$",
    re.DOTALL,
)
_TRAILING_HTML_COMMENT_RE = re.compile(r"\s*<!--.*?-->\s*$", re.DOTALL)


def _severity_from_drift(drift_score: float) -> StyleAuditSeverity:
    """drift_score(0-10)→ severity 4 档(确定性派生,可单测)。"""
    for threshold, severity in _SEVERITY_THRESHOLDS:
        if drift_score < threshold:
            return severity
    return "critical"


class StyleAuditReport(BaseModel):
    """``audit_style`` 出参(v1.1 w4-s5 / I2.2 真 agent 版)。

    与 stub 同形契约(``severity`` / ``drift_score`` / ``summary`` /
    ``model_used``)+ 新增 ``dimension_notes``;``severity`` 升正式 4 档
    ``Literal``,``drift_score`` 0-10。
    """

    severity: StyleAuditSeverity = Field(
        ..., description="模板偏移严重档(由 drift_score 阈值派生)"
    )
    drift_score: float | None = Field(
        None, ge=0.0, le=10.0, description="模板偏移度(0-10,越高越偏离)"
    )
    summary: str = Field(..., description="审计总结(自然语言)")
    model_used: str | None = Field(
        None, description="实际使用的模型名;短路 / fail-open 时为 None"
    )
    dimension_notes: list[str] = Field(
        default_factory=list, description="逐维偏移说明(LLM 产出,可空)"
    )


class _StyleAuditorLLMPayload(BaseModel):
    """LLM 输出中间模型(只校验 LLM 该产出的字段)。

    ``severity`` 不让 LLM 给(代码按 ``drift_score`` 阈值派生);LLM 只产
    ``drift_score`` + ``summary`` + 可选 ``dimension_notes``。
    """

    drift_score: float = Field(ge=0.0, le=10.0)
    summary: str
    dimension_notes: list[str] = Field(default_factory=list)


def audit_style(
    *,
    workspace_id: str,
    style_template: str | None = None,
    chapter_excerpts: list[str] | None = None,
    chapter_range_start: str | None = None,
    chapter_range_end: str | None = None,
    router: ModelRouter | None = None,
    client: LLMClient | None = None,
    model: str | None = None,
    max_tokens: int = _DEFAULT_MAX_TOKENS,
    temperature: float = _DEFAULT_TEMPERATURE,
    prompts_dir: Path | None = None,
) -> StyleAuditReport:
    """审计一段章节范围的产出与所选写作风格模板的偏移度(v1.1 w4-s5 / I2.2)。

    :param workspace_id: 当前工作区 ID(透传 structlog 事件 + caller 落表同源)。
    :param style_template: 工作区所选风格模板枚举值;``None`` → 短路返 ok。
    :param chapter_excerpts: 待审章节正文片段列表(caller 预读 final 正文,
        本函数不依赖 SQLAlchemy session);空 → 短路返 ok。
    :param chapter_range_start: 审计起点 chapter_id(可选,记录用)。
    :param chapter_range_end: 审计终点 chapter_id(可选,记录用)。
    :param router: ``ModelRouter``;给定时按 ``router.route("style_auditor")``
        取 client + model + 生成参数。``None`` 时走 legacy client/model 路径。
    :param client: legacy 路径 LLM 客户端;``router=None`` 时必传。
    :param model: legacy 路径模型名;``router=None`` 时必传。
    :param max_tokens: legacy 路径输出 token 上限。
    :param temperature: legacy 路径采样温度。
    :param prompts_dir: 提示词模板根目录;``None`` 时取 ``settings.prompts_dir``。
    :return: ``StyleAuditReport``。
    """
    excerpts = [e for e in (chapter_excerpts or []) if e and e.strip()]
    template_meta = (
        STYLE_TEMPLATES.get(style_template) if style_template is not None else None
    )

    # 短路:未选模板 / 模板非法 / 无可审章节 → 不调 LLM,返 ok 基线。
    if template_meta is None or not excerpts:
        reason = (
            "工作区未选写作风格模板,无基线可对照"
            if template_meta is None
            else "无可审章节正文,无偏移基线"
        )
        report = StyleAuditReport(
            severity="ok",
            drift_score=0.0,
            summary=reason,
            model_used=None,
            dimension_notes=[],
        )
        _log.info(
            "style_audit",
            workspace_id=workspace_id,
            severity=report.severity,
            drift_score=report.drift_score,
            chapter_range_start=chapter_range_start,
            chapter_range_end=chapter_range_end,
            style_template=style_template,
            short_circuit=True,
        )
        return report

    # setup(路由 + prompt 加载)失败 → fail-open。
    try:
        resolved_client, resolved_model, complete_kwargs = resolve_routing(
            agent_name="style_auditor",
            router=router,
            client=client,
            model=model,
            legacy_kwargs={"max_tokens": max_tokens, "temperature": temperature},
        )
        template_root = (
            prompts_dir if prompts_dir is not None else settings.prompts_dir
        ).resolve()
        system_text = load_prompt_template(
            template_root / _STYLE_AUDITOR_PROMPT_FILENAME
        )
        complete_kwargs = prepare_strict_json_output(
            client=resolved_client,
            model=resolved_model,
            output_model=_StyleAuditorLLMPayload,
            schema_name="style_auditor",
            complete_kwargs=complete_kwargs,
        )
    except Exception as exc:  # noqa: BLE001 — fail-open 含 setup 阶段
        return _fail_open_report(
            workspace_id=workspace_id,
            style_template=style_template,
            chapter_range_start=chapter_range_start,
            chapter_range_end=chapter_range_end,
            stage="setup",
            exc=exc,
        )

    user_text = _build_user_message(
        template_meta=template_meta,
        excerpts=excerpts,
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
    except Exception as exc:  # noqa: BLE001 — fail-open(LLM / 解析失败)
        return _fail_open_report(
            workspace_id=workspace_id,
            style_template=style_template,
            chapter_range_start=chapter_range_start,
            chapter_range_end=chapter_range_end,
            stage="llm",
            exc=exc,
        )

    severity = _severity_from_drift(payload.drift_score)
    report = StyleAuditReport(
        severity=severity,
        drift_score=payload.drift_score,
        summary=payload.summary,
        model_used=response.model_used,
        dimension_notes=list(payload.dimension_notes),
    )
    _log.info(
        "style_audit",
        workspace_id=workspace_id,
        severity=report.severity,
        drift_score=round(payload.drift_score, 4),
        chapter_range_start=chapter_range_start,
        chapter_range_end=chapter_range_end,
        style_template=style_template,
        model_used=response.model_used,
    )
    return report


def _fail_open_report(
    *,
    workspace_id: str,
    style_template: str | None,
    chapter_range_start: str | None,
    chapter_range_end: str | None,
    stage: str,
    exc: Exception,
) -> StyleAuditReport:
    """setup / LLM 失败时的 fail-open 报告(severity=ok / drift=0 + 警告日志)。"""
    _log.warning(
        "style_audit_failed_open",
        workspace_id=workspace_id,
        style_template=style_template,
        chapter_range_start=chapter_range_start,
        chapter_range_end=chapter_range_end,
        stage=stage,
        error_type=type(exc).__name__,
        error_message=str(exc),
    )
    return StyleAuditReport(
        severity="ok",
        drift_score=0.0,
        summary=f"风格审计失败兜底(fail-open,阶段={stage}):{type(exc).__name__}",
        model_used=None,
        dimension_notes=[],
    )


def _build_user_message(*, template_meta: object, excerpts: list[str]) -> str:
    """拼装 user message:模板期望 10 维 + 章节正文片段 + agent_role marker。"""
    d = template_meta.dimensions  # type: ignore[attr-defined]
    label = template_meta.label  # type: ignore[attr-defined]
    description = template_meta.description  # type: ignore[attr-defined]
    expectation_block = (
        "## 模板期望(TEMPLATE STYLE EXPECTATION)\n\n"
        f"本工作区选定写作风格模板:**{label}**({description})\n\n"
        f"- ① 题材调性:{d.genre_tone}\n"
        f"- ② 叙事密度:{d.narrative_density}\n"
        f"- ③ 修辞密度:{d.rhetoric_density}\n"
        f"- ④ 对白比例:{d.dialogue_ratio}\n"
        f"- ⑤ POV 切换:{d.pov_switching}\n"
        f"- ⑥ 金手指节奏:{d.power_pacing}\n"
        f"- ⑦ 世界观密度:{d.worldbuilding_density}\n"
        f"- ⑧ 情感色调:{d.emotional_tone}\n"
        f"- ⑨ 章节长度倾向:{d.chapter_length}\n"
        f"- ⑩ 事件密度倾向:{d.event_density}"
    )
    excerpt_blocks: list[str] = []
    for idx, text in enumerate(excerpts, start=1):
        clipped = text[:_EXCERPT_CHAR_LIMIT]
        suffix = "\n……(已截断)" if len(text) > _EXCERPT_CHAR_LIMIT else ""
        excerpt_blocks.append(f"### 片段 {idx}\n\n{clipped}{suffix}")
    excerpts_block = "## 待审章节正文片段\n\n" + "\n\n".join(excerpt_blocks)
    body = f"{expectation_block}\n\n{excerpts_block}"
    return append_role_marker(body, role="style_auditor")


def _parse_llm_payload(*, raw_text: str, model_used: str) -> _StyleAuditorLLMPayload:
    """剥代码块 → ``json.loads`` → Pydantic 校验。失败抛 ``ValueError``。"""
    text = _strip_code_fence(raw_text).strip()
    try:
        data = loads_with_quote_repair(
            text,
            agent_name="style_auditor",
            model_used=model_used,
        )
    except json.JSONDecodeError as exc:
        raise ValueError(
            f"style_auditor LLM 输出非合法 JSON(model={model_used}): {raw_text[:200]!r}"
        ) from exc
    try:
        return _StyleAuditorLLMPayload.model_validate(data)
    except ValidationError as exc:
        raise ValueError(
            f"style_auditor LLM 输出 schema 不符(model={model_used}): {exc.errors()}"
        ) from exc


def _strip_code_fence(text: str) -> str:
    """剥 trailing HTML 注释 + ``` / ```json 包裹(沿用 reviewer / validator 同型)。"""
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
    "StyleAuditReport",
    "StyleAuditSeverity",
    "audit_style",
]
