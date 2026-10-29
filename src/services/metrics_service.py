"""Metrics 读端点服务(P4-B-04)。

把 P4-B-01 metrics 5 表的查询打包成业务语义明确的 service,供 ``api/routes/metrics.py``
7 + 1 个 GET 端点调用。**只读**——所有写入路径走 ``metrics_writer`` /
``metrics_aggregator`` / ``system_metrics_writer``。

设计要点
========

- session 由工厂注入(``Callable[[str], Session]``,与 character/thread/timeline
  service 同模板),每方法调用打开短 session、读、close;不持有连接。
- 跨工作区禁止:metrics_per_chapter 表没有 ``workspace_id`` 字段(每工作区
  独立 db),session 工厂已按 workspace_id 分流,语义自然隔离。
- 错误统一抛 ``MetricsServiceError``,API 层翻译为 ``METRICS_*`` 错误码。
  当前仅在 workspace 不存在时由上游 ``WorkspaceServiceError`` 冒泡(P1-B-19
  错误体系自动翻译为 ``WORKSPACE_NOT_FOUND``)。
"""

from __future__ import annotations

import datetime as _dt
from collections.abc import Callable
from typing import Literal

from sqlalchemy import desc, func, select
from sqlalchemy.orm import Session

from src.core.db.models import (
    MemoryHealthCheckORM,
    MetricsDailyORM,
    MetricsPerChapterORM,
    StyleAuditEntryORM,
    SystemMetricsDailyORM,
)
from src.services.metrics_aggregator import (
    compute_overview,
    list_aggregated_rows,
)
from src.services.workspace_service import WorkspaceService

SessionFactory = Callable[[str], Session]
"""按 ``workspace_id`` 构造 SQLite ``Session`` 的工厂(对齐 character_service /
thread_service 既有模板)。"""

SystemSessionFactory = Callable[[], Session]
"""零参 system db 工厂(``/api/v1/system/metrics`` 端点用,不依赖任何 workspace)。"""

PerChapterOrder = Literal["approved_at_asc", "approved_at_desc", "chapter_id_asc"]


class MetricsServiceError(Exception):
    """metrics service 操作失败基类(预留;当前仅 workspace 不存在透传)。"""


class MetricsService:
    """工作区 metrics 读 service。

    所有方法以 ``workspace_id`` 起头(URL path 参数),内部通过 session 工厂
    打开当前工作区 SQLite 短 session 完成读;返回纯 dict / list[dict] 形式
    (供路由层 ``Out`` schema 序列化),不返 ORM 对象避开"路由直接读 ORM"
    的层间耦合。

    ``WorkspaceService`` 注入仅用于校验 workspace 存在性(``info()`` 不存在
    抛 ``WorkspaceServiceError``,API 层翻译为 ``WORKSPACE_NOT_FOUND``);系统级
    端点 ``/system/metrics`` 不走 workspace 路径,在路由层独立处理。
    """

    def __init__(
        self,
        *,
        workspace_service: WorkspaceService,
        session_factory: SessionFactory,
        system_session_factory: SystemSessionFactory | None = None,
    ) -> None:
        self._workspace_service = workspace_service
        self._session_factory = session_factory
        self._system_session_factory = system_session_factory

    def get_overview(self, workspace_id: str) -> dict:
        """累计概览 + 今日 delta。"""
        self._workspace_service.info(workspace_id)
        with self._session_factory(workspace_id) as session:
            cumulative = compute_overview(session)
            today = _dt.datetime.now(_dt.timezone.utc).date()
            today_row = session.get(MetricsDailyORM, today)
            today_chapters_approved = (
                today_row.chapters_approved if today_row is not None else 0
            )
            today_cost_usd = (
                float(today_row.total_cost_usd) if today_row is not None else 0.0
            )
        return {
            "workspace_id": workspace_id,
            **cumulative,
            "today_chapters_approved": today_chapters_approved,
            "today_cost_usd": today_cost_usd,
        }

    def list_daily(
        self,
        workspace_id: str,
        *,
        since: _dt.date | None = None,
        until: _dt.date | None = None,
    ) -> list[dict]:
        """按日期 asc 列出 metrics_daily 行(范围可选)。"""
        self._workspace_service.info(workspace_id)
        with self._session_factory(workspace_id) as session:
            rows = list_aggregated_rows(session, since=since, until=until)
            return [_metrics_daily_to_dict(r) for r in rows]

    def list_per_chapter(
        self,
        workspace_id: str,
        *,
        order: PerChapterOrder = "approved_at_desc",
    ) -> list[dict]:
        """列出 metrics_per_chapter 全部行。

        **P4 简化**:``volume=`` 过滤未实现(metrics_per_chapter 无 volume_id
        字段;P5+ 加 ``chapter_id → volume_id`` 反查后再实现)。``order``
        支持 3 档:``approved_at_desc`` / ``approved_at_asc`` / ``chapter_id_asc``。
        """
        self._workspace_service.info(workspace_id)
        with self._session_factory(workspace_id) as session:
            stmt = select(MetricsPerChapterORM)
            if order == "approved_at_desc":
                stmt = stmt.order_by(desc(MetricsPerChapterORM.approved_at))
            elif order == "approved_at_asc":
                stmt = stmt.order_by(MetricsPerChapterORM.approved_at.asc())
            else:
                stmt = stmt.order_by(MetricsPerChapterORM.chapter_id.asc())
            rows = list(session.execute(stmt).scalars())
            return [_metrics_per_chapter_to_dict(r) for r in rows]

    def get_tension_curve(self, workspace_id: str) -> list[dict]:
        """返每章 (target_tension_level, actual_tension_estimate),按 approved_at asc。"""
        self._workspace_service.info(workspace_id)
        with self._session_factory(workspace_id) as session:
            stmt = (
                select(
                    MetricsPerChapterORM.chapter_id,
                    MetricsPerChapterORM.target_tension_level,
                    MetricsPerChapterORM.actual_tension_estimate,
                )
                .order_by(MetricsPerChapterORM.approved_at.asc())
            )
            return [
                {
                    "chapter_id": chapter_id,
                    "target_tension_level": target_tension_level,
                    "actual_tension_estimate": actual_tension_estimate,
                }
                for chapter_id, target_tension_level, actual_tension_estimate in (
                    session.execute(stmt).all()
                )
            ]

    def get_cost_breakdown(self, workspace_id: str) -> list[dict]:
        """按 writer_model 分组 SUM(total_cost_usd) / SUM(total_tokens) / COUNT(*)。"""
        self._workspace_service.info(workspace_id)
        with self._session_factory(workspace_id) as session:
            stmt = (
                select(
                    MetricsPerChapterORM.writer_model,
                    func.count().label("chapters_count"),
                    func.coalesce(
                        func.sum(MetricsPerChapterORM.total_cost_usd), 0.0
                    ).label("total_cost_usd"),
                    func.coalesce(
                        func.sum(MetricsPerChapterORM.total_tokens), 0
                    ).label("total_tokens"),
                )
                .group_by(MetricsPerChapterORM.writer_model)
                .order_by(MetricsPerChapterORM.writer_model.asc())
            )
            return [
                {
                    "writer_model": writer_model,
                    "chapters_count": int(chapters_count),
                    "total_cost_usd": float(total_cost_usd),
                    "total_tokens": int(total_tokens),
                }
                for writer_model, chapters_count, total_cost_usd, total_tokens in (
                    session.execute(stmt).all()
                )
            ]

    def list_memory_health(
        self, workspace_id: str, *, limit: int = 50
    ) -> list[dict]:
        """memory_health_history 按 ``checked_at desc`` 列出。"""
        self._workspace_service.info(workspace_id)
        with self._session_factory(workspace_id) as session:
            stmt = (
                select(MemoryHealthCheckORM)
                .order_by(desc(MemoryHealthCheckORM.checked_at))
                .limit(limit)
            )
            rows = list(session.execute(stmt).scalars())
            return [_memory_health_to_dict(r) for r in rows]

    def list_style_audits(
        self, workspace_id: str, *, limit: int = 50
    ) -> list[dict]:
        """style_audit_history 按 ``audited_at desc`` 列出。"""
        self._workspace_service.info(workspace_id)
        with self._session_factory(workspace_id) as session:
            stmt = (
                select(StyleAuditEntryORM)
                .order_by(desc(StyleAuditEntryORM.audited_at))
                .limit(limit)
            )
            rows = list(session.execute(stmt).scalars())
            return [_style_audit_to_dict(r) for r in rows]

    def get_system_metrics(self, *, history_days: int = 30) -> dict:
        """系统级 metrics:今日 + 最近 N 天历史(无工作区,跨进程聚合)。

        ``system_session_factory`` 未注入时返默认空行 + 空历史(测试 / P1
        兼容路径)。
        """
        today = _dt.datetime.now(_dt.timezone.utc).date()
        if self._system_session_factory is None:
            return {
                "today": _empty_system_metrics(today),
                "history": [],
            }
        with self._system_session_factory() as session:
            today_row = session.get(SystemMetricsDailyORM, today)
            today_payload = (
                _system_metrics_to_dict(today_row)
                if today_row is not None
                else _empty_system_metrics(today)
            )
            since = today - _dt.timedelta(days=history_days)
            stmt = (
                select(SystemMetricsDailyORM)
                .where(SystemMetricsDailyORM.date >= since)
                .order_by(SystemMetricsDailyORM.date.asc())
            )
            history = [
                _system_metrics_to_dict(r)
                for r in session.execute(stmt).scalars()
            ]
        return {
            "today": today_payload,
            "history": history,
        }


# ---- 内部 ORM → dict 序列化 helper -----------------------------------------


def _metrics_daily_to_dict(row: MetricsDailyORM) -> dict:
    return {
        "date": row.date,
        "chapters_drafted": row.chapters_drafted,
        "chapters_approved": row.chapters_approved,
        "chapters_rewritten": row.chapters_rewritten,
        "chapters_revised": row.chapters_revised,
        "chapters_escalated": row.chapters_escalated,
        "avg_review_score": row.avg_review_score,
        "avg_rewrites_per_chapter": row.avg_rewrites_per_chapter,
        "avg_revises_per_chapter": row.avg_revises_per_chapter,
        "total_input_tokens": row.total_input_tokens,
        "total_output_tokens": row.total_output_tokens,
        "total_cost_usd": float(row.total_cost_usd),
        "avg_chapter_latency_seconds": row.avg_chapter_latency_seconds,
        "qwen_calls": row.qwen_calls,
        "claude_sonnet_calls": row.claude_sonnet_calls,
        "claude_opus_calls": row.claude_opus_calls,
    }


def _metrics_per_chapter_to_dict(row: MetricsPerChapterORM) -> dict:
    return {
        "chapter_id": row.chapter_id,
        "drafted_at": row.drafted_at,
        "approved_at": row.approved_at,
        "total_latency_seconds": row.total_latency_seconds,
        "rewrite_count": row.rewrite_count,
        "revise_count": row.revise_count,
        "final_score_overall": row.final_score_overall,
        "final_score_plot_progression": row.final_score_plot_progression,
        "final_score_tension_match": row.final_score_tension_match,
        "final_score_prose_quality": row.final_score_prose_quality,
        "final_score_consistency": row.final_score_consistency,
        "final_score_voice_consistency": row.final_score_voice_consistency,
        "final_score_hook_management": row.final_score_hook_management,
        "word_count": row.word_count,
        "target_word_count_min": row.target_word_count_min,
        "target_word_count_max": row.target_word_count_max,
        "target_tension_level": row.target_tension_level,
        "actual_tension_estimate": row.actual_tension_estimate,
        "total_tokens": row.total_tokens,
        "total_cost_usd": float(row.total_cost_usd),
        "writer_profile": row.writer_profile,
        "writer_model": row.writer_model,
        "writer_calls": row.writer_calls,
        "reviewer_calls": row.reviewer_calls,
        "reviser_calls": row.reviser_calls,
        "cache_input_tokens": row.cache_input_tokens,
        "cache_hit_ratio": row.cache_hit_ratio,
    }


def _memory_health_to_dict(row: MemoryHealthCheckORM) -> dict:
    return {
        "check_id": row.check_id,
        "checked_at": row.checked_at,
        "severity": row.severity,
        "stale_threads_count": row.stale_threads_count,
        "inactive_characters_count": row.inactive_characters_count,
        "conflicts_count": row.conflicts_count,
    }


def _style_audit_to_dict(row: StyleAuditEntryORM) -> dict:
    return {
        "audit_id": row.audit_id,
        "audited_at": row.audited_at,
        "chapter_range_start": row.chapter_range_start,
        "chapter_range_end": row.chapter_range_end,
        "drift_score": row.drift_score,
        "severity": row.severity,
    }


def _system_metrics_to_dict(row: SystemMetricsDailyORM) -> dict:
    return {
        "date": row.date,
        "api_requests": row.api_requests,
        "api_errors": row.api_errors,
        "avg_api_latency_ms": row.avg_api_latency_ms,
        "ws_connections": row.ws_connections,
        "frontend_errors": row.frontend_errors,
        "pipeline_runs": row.pipeline_runs,
    }


def _empty_system_metrics(today: _dt.date) -> dict:
    return {
        "date": today,
        "api_requests": 0,
        "api_errors": 0,
        "avg_api_latency_ms": None,
        "ws_connections": 0,
        "frontend_errors": 0,
        "pipeline_runs": 0,
    }
