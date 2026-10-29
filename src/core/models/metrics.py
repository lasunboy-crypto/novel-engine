"""指标体系 5 张表的 Pydantic 领域模型(对应 OBSERVABILITY §3.1)。

P4-B-01 引入,5 张表对应 5 个 Pydantic 模型:

- ``MetricsDaily`` —— 工作区每日聚合(``metrics_daily`` 表)
- ``MetricsPerChapter`` —— 单章详细指标(``metrics_per_chapter`` 表)
- ``SystemMetricsDaily`` —— 系统级每日聚合(``system_metrics_daily`` 表)
- ``MemoryHealthCheck`` —— 记忆健康度检查历史(``memory_health_history`` 表)
- ``StyleAuditEntry`` —— 风格漂移审计历史(``style_audit_history`` 表)

设计要点:

1. 字段名严格对齐 OBSERVABILITY §3.1 字面 CREATE TABLE 列名;ORM 字段名同步
   对齐(便于 Pydantic ↔ ORM 互转)。
2. ``metrics_per_chapter`` 包含 P4-plan-kickoff 决策 5 拍板的 prompt cache
   2 字段(``cache_input_tokens`` / ``cache_hit_ratio``),默认 ``None``;
   仅当 Anthropic prompt cache 显式启用时由 CostTrackingClient 钩子写入。
3. **数值聚合字段的可空性**(自决项,impl 文档登记):spec §3.1 字面未标
   ``NOT NULL``,严格走会让全表 nullable;实际工程语义:计数字段 ``NOT NULL
   DEFAULT 0``(无样本时 0 比 NULL 更合理),平均值 / 平均分 / drift_score
   ``NULL``(无样本时确实未定义),时间戳 / 主键 / severity 等"写入即知"
   字段 ``NOT NULL``。
4. ``report_json`` 字段类型:spec 字面 TEXT,但项目模式以 ``sa.JSON`` 列存
   ``dict``(SQLAlchemy 自动序列化);名字保留 ``_json`` 后缀以保持与 spec
   字面一致。
5. ``severity`` 字段:``MemoryHealthCheck.severity`` 走 spec §3.1 字面 4 档
   ``ok|minor|warning|critical`` Literal 限定;``StyleAuditEntry.severity``
   P4-B-10 stub 时开放 ``str``("stub"),v1.1 w4-s5 / I2.2 StyleAuditor 真
   agent 落地后升级为同 4 档 ``Literal`` + ORM CHECK 约束(0018 迁移 fold
   旧 stub → ok)。
"""

import datetime as _dt
from typing import Any, Literal

from pydantic import BaseModel, Field


class MetricsDaily(BaseModel):
    """工作区每日聚合指标(``metrics_daily`` 表,OBSERVABILITY §3.1)。

    P4-B-04 metrics_aggregator 在每章 APPROVED 后增量更新当日 row;同一
    工作区每天一行,``date`` 主键避免重复。
    """

    date: _dt.date = Field(..., description="日期(ISO 8601 YYYY-MM-DD)")
    chapters_drafted: int = Field(0, description="当日 drafted 章节数")
    chapters_approved: int = Field(0, description="当日 approved 章节数")
    chapters_rewritten: int = Field(0, description="当日 rewrite 触发章节数")
    chapters_revised: int = Field(0, description="当日 revise 触发章节数")
    chapters_escalated: int = Field(0, description="当日升人工介入章节数")
    avg_review_score: float | None = Field(
        None, description="当日 review score_overall 平均值,无样本时 None"
    )
    avg_rewrites_per_chapter: float | None = Field(
        None, description="当日平均 rewrite 次数,无样本时 None"
    )
    avg_revises_per_chapter: float | None = Field(
        None, description="当日平均 revise 次数,无样本时 None"
    )
    total_input_tokens: int = Field(0, description="当日 LLM 输入 token 累加")
    total_output_tokens: int = Field(0, description="当日 LLM 输出 token 累加")
    total_cost_usd: float = Field(0.0, description="当日 LLM 成本累加 USD")
    avg_chapter_latency_seconds: float | None = Field(
        None, description="当日章节平均耗时秒,无样本时 None"
    )
    qwen_calls: int = Field(0, description="当日 Qwen 系列模型调用次数")
    claude_sonnet_calls: int = Field(
        0, description="当日 Claude Sonnet 系列模型调用次数"
    )
    claude_opus_calls: int = Field(
        0, description="当日 Claude Opus 系列模型调用次数"
    )


class MetricsPerChapter(BaseModel):
    """单章详细指标(``metrics_per_chapter`` 表,OBSERVABILITY §3.1)。

    P4-B-03 ``_on_state_transition(to_state=APPROVED)`` 钩子一次性落库,失败
    swallow + structlog error(对齐 P2-B-19 / P3-B-07 模式)。

    ``cache_input_tokens`` / ``cache_hit_ratio`` 是 P4-plan-kickoff 决策 5
    拍板的 prompt cache 2 字段,默认 ``None``;CostTrackingClient 钩子读
    Claude SDK ``response.usage.cache_creation_input_tokens`` /
    ``cache_read_input_tokens`` 字段时填入,默认 disable。
    """

    chapter_id: str = Field(..., description="章节唯一 ID(主键)")
    drafted_at: _dt.datetime = Field(
        ..., description="章节 drafting 启动时间(UTC)"
    )
    approved_at: _dt.datetime = Field(..., description="章节 APPROVED 时间(UTC)")
    total_latency_seconds: float = Field(
        ..., description="drafted_at → approved_at 总耗时秒"
    )
    rewrite_count: int = Field(0, description="本章 rewrite 触发次数")
    revise_count: int = Field(0, description="本章 revise 触发次数")
    final_score_overall: float = Field(
        ..., description="终版 review score_overall(approved 时锁定)"
    )
    final_score_plot_progression: float = Field(
        ..., description="终版 review score_plot_progression"
    )
    final_score_tension_match: float = Field(
        ..., description="终版 review score_tension_match"
    )
    final_score_prose_quality: float = Field(
        ..., description="终版 review score_prose_quality"
    )
    final_score_consistency: float = Field(
        ..., description="终版 review score_consistency"
    )
    final_score_voice_consistency: float = Field(
        ..., description="终版 review score_voice_consistency(P2-B-11)"
    )
    final_score_hook_management: float = Field(
        ..., description="终版 review score_hook_management"
    )
    word_count: int = Field(..., description="终版章节字数")
    target_word_count_min: int = Field(
        ..., description="章节卡 target_word_count.min"
    )
    target_word_count_max: int = Field(
        ..., description="章节卡 target_word_count.max"
    )
    target_tension_level: int = Field(
        ..., description="章节卡 target_tension_level(1-10)"
    )
    actual_tension_estimate: float | None = Field(
        None, description="Reviewer 估计的实际张力(1-10),未估时 None"
    )
    total_tokens: int = Field(0, description="本章 LLM 总 token(input+output)")
    total_cost_usd: float = Field(0.0, description="本章 LLM 总成本 USD")
    writer_profile: str = Field(
        ..., description="本章 Writer 实际使用的 profile id(P3-B-01)"
    )
    writer_model: str = Field(..., description="本章 Writer 实际使用的模型名")
    writer_calls: int = Field(0, description="Writer 调用次数(含 rewrite)")
    reviewer_calls: int = Field(0, description="Reviewer 调用次数(含 rewrite)")
    reviser_calls: int = Field(0, description="Reviser 调用次数")
    cache_input_tokens: int | None = Field(
        None,
        description=(
            "Anthropic prompt cache 命中输入 token(决策 5,默认 disable);"
            "未启用时 None"
        ),
    )
    cache_hit_ratio: float | None = Field(
        None,
        description=(
            "Anthropic prompt cache 命中率 = cache_input_tokens / "
            "total_input_tokens(决策 5,默认 disable);未启用时 None"
        ),
    )


class SystemMetricsDaily(BaseModel):
    """系统级每日聚合(``system_metrics_daily`` 表,OBSERVABILITY §3.1)。

    跨工作区聚合;``date`` 主键。P4-B-04 由 API 中间件级别聚合,与 §2.4
    ``api_request`` / ``api_error`` / ``ws_*`` / ``frontend_error`` /
    ``pipeline_*`` 事件耦合。
    """

    date: _dt.date = Field(..., description="日期(ISO 8601 YYYY-MM-DD)")
    api_requests: int = Field(0, description="当日 API 请求总数")
    api_errors: int = Field(0, description="当日 API 错误总数(4xx/5xx)")
    avg_api_latency_ms: float | None = Field(
        None, description="当日 API 平均耗时毫秒,无样本时 None"
    )
    ws_connections: int = Field(0, description="当日 WebSocket 连接建立次数")
    frontend_errors: int = Field(
        0, description="当日前端上报错误数(POST /system/frontend-errors)"
    )
    pipeline_runs: int = Field(0, description="当日章节流水线运行次数")


class MemoryHealthCheck(BaseModel):
    """记忆健康度检查历史(``memory_health_history`` 表,OBSERVABILITY §3.1)。

    P4-B-09 MemoryHealthChecker agent 触发后落一行;``severity=critical`` 时
    流水线自动暂停 + WS 推送(P4-B-08 阈值告警联动)。
    """

    check_id: str = Field(..., description="检查唯一 ID(主键)")
    checked_at: _dt.datetime = Field(..., description="检查时间(UTC)")
    severity: Literal["ok", "minor", "warning", "critical"] = Field(
        ..., description="健康度严重档"
    )
    stale_threads_count: int = Field(
        0, description="陈旧伏笔数(active 但近 N 章未提及)"
    )
    inactive_characters_count: int = Field(
        0, description="不活跃角色数(近 N 章未出场)"
    )
    conflicts_count: int = Field(
        0, description="冲突数(timeline 矛盾或 character 字段矛盾)"
    )
    report_json: dict[str, Any] = Field(
        default_factory=dict,
        description="完整检查报告(列出具体 stale_threads / conflicts 等)",
    )


class StyleAuditEntry(BaseModel):
    """风格漂移审计历史(``style_audit_history`` 表,OBSERVABILITY §3.1)。

    P4-B-10 StyleAuditor agent 接口预留 stub(severity="stub");v1.1 wave 4
    s5 / I2.2 StyleAuditor 真 agent 落地后复用同表 schema,severity 升级为正式
    4 档 ``Literal``,``drift_score`` 0-10(模板偏移度,越高越偏离模板期望)。
    ORM ``severity`` 字段同步加 CHECK 约束(SSOT ``STYLE_AUDIT_SEVERITY_VALUES``)。
    """

    audit_id: str = Field(..., description="审计唯一 ID(主键)")
    audited_at: _dt.datetime = Field(..., description="审计时间(UTC)")
    chapter_range_start: str | None = Field(
        None, description="审计章节范围起点 chapter_id(可选)"
    )
    chapter_range_end: str | None = Field(
        None, description="审计章节范围终点 chapter_id(可选)"
    )
    drift_score: float | None = Field(
        None,
        ge=0.0,
        le=10.0,
        description=(
            "模板偏移度(0-10,越高越偏离模板期望);工作区未选模板 / 无可审"
            "章节时为 0.0(无基线可对照)"
        ),
    )
    severity: Literal["ok", "minor", "warning", "critical"] = Field(
        ...,
        description=(
            "模板偏移严重档(v1.1 w4-s5 / I2.2 由 drift_score 阈值确定性派生:"
            "<2.5 ok / <5 minor / <7.5 warning / else critical)"
        ),
    )
    report_json: dict[str, Any] = Field(
        default_factory=dict,
        description="完整审计报告(含 summary / model_used / dimension_notes)",
    )
