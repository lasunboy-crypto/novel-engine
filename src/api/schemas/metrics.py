"""Metrics API 表面 schema(P4-B-04)。

对应 OBSERVABILITY §3.2 字面 7 个工作区端点 + 1 个系统级端点的请求/响应
形态。所有 schema 严格 ``forbid extra``(对齐 P1-B-17 既有契约纪律)。

字段命名严格按 ``snake_case``(CLAUDE.md §字段命名约定)。
"""

from __future__ import annotations

import datetime as _dt

from pydantic import BaseModel, ConfigDict, Field


class OverviewOut(BaseModel):
    """``GET /metrics/overview`` 响应 ``data``。

    工作区累计概览:从 metrics_per_chapter 聚合(累计 APPROVED 章节数 +
    成本累加 + 平均评分等)+ 当日 metrics_daily 局部摘要(daily delta)。
    """

    model_config = ConfigDict(extra="forbid")

    workspace_id: str
    chapters_approved: int = Field(0, description="累计 APPROVED 章节数")
    total_cost_usd: float = Field(0.0, description="累计 LLM 成本 USD")
    total_tokens: int = Field(0, description="累计 LLM token(input+output)")
    avg_review_score: float | None = Field(
        None, description="累计 review final_score_overall 平均;无样本时 None"
    )
    avg_chapter_latency_seconds: float | None = Field(
        None,
        description="累计 metrics_per_chapter total_latency_seconds 平均;无样本时 None",
    )
    today_chapters_approved: int = Field(
        0, description="今日已 APPROVED 章节数(metrics_daily 当日行)"
    )
    today_cost_usd: float = Field(0.0, description="今日 LLM 成本 USD")


class MetricsDailyOut(BaseModel):
    """``GET /metrics/daily`` 单元素 + ``per-chapter`` / 其他端点共用的
    metrics_daily 行响应形态(对齐 OBSERVABILITY §3.1 ``metrics_daily`` 字段)。"""

    model_config = ConfigDict(extra="forbid")

    date: _dt.date
    chapters_drafted: int
    chapters_approved: int
    chapters_rewritten: int
    chapters_revised: int
    chapters_escalated: int
    avg_review_score: float | None = None
    avg_rewrites_per_chapter: float | None = None
    avg_revises_per_chapter: float | None = None
    total_input_tokens: int
    total_output_tokens: int
    total_cost_usd: float
    avg_chapter_latency_seconds: float | None = None
    qwen_calls: int = Field(
        description=(
            "writer 调用数(draft+rewrite version 计数),**非全 agent 计数**"
            "(reviewer/summary/memory 等不在内)— 字段名历史遗留,语义见 "
            "v2 w2-s1 标定报告 §7.1(11 章 × ~1.4 次为正常量级)"
        )
    )
    claude_sonnet_calls: int
    claude_opus_calls: int


class DailyListOut(BaseModel):
    """``GET /metrics/daily?since=&until=`` 响应 ``data``:按日期 asc 数组。"""

    model_config = ConfigDict(extra="forbid")

    items: list[MetricsDailyOut]


class PerChapterOut(BaseModel):
    """``GET /metrics/per-chapter`` 单元素响应形态(对齐 OBSERVABILITY §3.1
    ``metrics_per_chapter`` 字段)。"""

    model_config = ConfigDict(extra="forbid")

    chapter_id: str
    drafted_at: _dt.datetime
    approved_at: _dt.datetime
    total_latency_seconds: float
    rewrite_count: int
    revise_count: int
    final_score_overall: float
    final_score_plot_progression: float
    final_score_tension_match: float
    final_score_prose_quality: float
    final_score_consistency: float
    final_score_voice_consistency: float
    final_score_hook_management: float
    word_count: int
    target_word_count_min: int
    target_word_count_max: int
    target_tension_level: int
    actual_tension_estimate: float | None = None
    total_tokens: int
    total_cost_usd: float
    writer_profile: str
    writer_model: str
    writer_calls: int
    reviewer_calls: int
    reviser_calls: int
    cache_input_tokens: int | None = None
    cache_hit_ratio: float | None = None


class PerChapterListOut(BaseModel):
    """``GET /metrics/per-chapter?volume=&order=`` 响应 ``data``。"""

    model_config = ConfigDict(extra="forbid")

    items: list[PerChapterOut]


class TensionPoint(BaseModel):
    """``/metrics/tension-curve`` 单点(章节 → target vs actual)。"""

    model_config = ConfigDict(extra="forbid")

    chapter_id: str
    target_tension_level: int = Field(..., description="章节卡 tension_level(1-10)")
    actual_tension_estimate: float | None = Field(
        None, description="Reviewer 估计实际张力(1-10);未估时 None"
    )


class TensionCurveOut(BaseModel):
    """``GET /metrics/tension-curve`` 响应 ``data``。"""

    model_config = ConfigDict(extra="forbid")

    points: list[TensionPoint]


class CostBucket(BaseModel):
    """``/metrics/cost-breakdown`` 按模型分组。"""

    model_config = ConfigDict(extra="forbid")

    writer_model: str
    chapters_count: int
    total_cost_usd: float
    total_tokens: int


class CostBreakdownOut(BaseModel):
    """``GET /metrics/cost-breakdown`` 响应 ``data``。"""

    model_config = ConfigDict(extra="forbid")

    buckets: list[CostBucket]


class MemoryHealthOut(BaseModel):
    """``GET /metrics/memory-health`` 单元素(对齐 OBSERVABILITY §3.1
    ``memory_health_history``)。"""

    model_config = ConfigDict(extra="forbid")

    check_id: str
    checked_at: _dt.datetime
    severity: str
    stale_threads_count: int
    inactive_characters_count: int
    conflicts_count: int


class MemoryHealthListOut(BaseModel):
    """``GET /metrics/memory-health`` 响应 ``data``(checked_at desc)。"""

    model_config = ConfigDict(extra="forbid")

    items: list[MemoryHealthOut]


class StyleAuditOut(BaseModel):
    """``GET /metrics/style-audits`` 单元素。"""

    model_config = ConfigDict(extra="forbid")

    audit_id: str
    audited_at: _dt.datetime
    chapter_range_start: str | None = None
    chapter_range_end: str | None = None
    drift_score: float | None = None
    severity: str


class StyleAuditListOut(BaseModel):
    """``GET /metrics/style-audits`` 响应 ``data``(audited_at desc)。"""

    model_config = ConfigDict(extra="forbid")

    items: list[StyleAuditOut]


class SystemMetricsDailyOut(BaseModel):
    """``system_metrics_daily`` 行序列化形态(对齐 OBSERVABILITY §3.1)。"""

    model_config = ConfigDict(extra="forbid")

    date: _dt.date
    api_requests: int
    api_errors: int
    avg_api_latency_ms: float | None = None
    ws_connections: int
    frontend_errors: int
    pipeline_runs: int


class SystemMetricsOut(BaseModel):
    """``GET /api/v1/system/metrics`` 响应 ``data``。

    P4-B-04 由 API 中间件级别聚合(``ApiMetricsMiddleware`` 写入 system db
    ``system_metrics_daily.<today>`` 行);跨工作区,单进程内全局。
    """

    model_config = ConfigDict(extra="forbid")

    today: SystemMetricsDailyOut
    history: list[SystemMetricsDailyOut]
