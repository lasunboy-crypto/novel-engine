"""Metrics 聚合服务(P4-B-04)。

``metrics_daily`` 表写入路径:在章节状态机进入 ``APPROVED`` 时由
``ChapterService._wrap_state_transition_with_db`` 闭包调本模块
``aggregate_daily_safely`` 一次性 upsert 当日工作区聚合行。

设计要点
--------

- **数据源:DB 反查 metrics_per_chapter**(P4-B-03 已落库)。本日已 APPROVED
  的所有章节 = ``approved_at`` 落在 ``[date 00:00, date+1 00:00)`` UTC 区间内
  的 metrics_per_chapter 行。每次 APPROVED 发生时全量重算当日聚合(成本极
  低 — SQLite 聚合查询单 ms 级,日内章节数通常 < 50)+ upsert 主键 ``date``。
  幂等且自然容忍重跑(同章 metrics_per_chapter 写入幂等 → 同日聚合也幂等)。
- **失败 swallow + structlog log**(对齐 P2-B-19 ``_extract_memories_safely``
  / P3-B-07 cost_tracker / P3-B-13 continuity refresh / P4-B-02
  ``persist_chapter_version_safely`` / ``persist_review_result_safely``
  / P4-B-03 ``write_per_chapter_safely`` 六先例)。聚合是辅助任务,落库
  失败不应阻塞主流水线。
- **workspace_id 透传纪律**(P3-B-14 沉淀):caller 显式传,不读
  ChapterCard / 资产文件字段;``metrics_daily`` 表 schema 无 ``workspace_id``
  字段(每工作区独立 db),本参数仅供 structlog 事件契约。
- **counts 字段语义**:``chapters_drafted`` = 当日有 metrics_per_chapter
  行 = 当日"完成至 APPROVED"章节数(非"启动 drafting"语义,以 spec
  ``metrics_daily`` 字段语义模糊为前提取保守语义);``chapters_approved`` =
  同 ``chapters_drafted``(P4 阶段 metrics_per_chapter 只在 APPROVED 时落,
  drafted/approved 1:1 对齐);``chapters_rewritten`` = ``rewrite_count >= 1``
  的章节数;``chapters_revised`` 同;``chapters_escalated`` = 当日落 0 行
  metrics_per_chapter 但状态机进入 HUMAN_INTERVENTION 的章节数(P4 阶段
  metrics_per_chapter 只在 APPROVED 时落,escalated 章节不会落 metrics_per_chapter
  → 无可靠数据源,P4 简化设 0,P5+ 加 escalated 落库后再补)。
- **per-model calls 字段** ``qwen_calls`` / ``claude_sonnet_calls`` /
  ``claude_opus_calls``:从 ``attempt_ledger`` 按真实 provider 物理调用分桶，
  包含所有 agent、失败请求和 transport retry。R5 前当日没有账本行时才回退
  ``metrics_per_chapter.writer_model/writer_calls``，保留历史指标。

字段映射(MetricsDailyORM ← 数据源)
------------------------------------

- ``date``                              ← caller 入参(UTC date)
- ``chapters_drafted``                  ← COUNT(metrics_per_chapter rows on date)
- ``chapters_approved``                 ← 同上
- ``chapters_rewritten``                ← COUNT(rewrite_count >= 1)
- ``chapters_revised``                  ← COUNT(revise_count >= 1)
- ``chapters_escalated``                ← 0(P4 简化,见上)
- ``avg_review_score``                  ← AVG(final_score_overall),无样本 → None
- ``avg_rewrites_per_chapter``          ← AVG(rewrite_count),无样本 → None
- ``avg_revises_per_chapter``           ← AVG(revise_count),无样本 → None
- ``total_input_tokens``                ← SUM(total_tokens) × 0(P4 metrics_per_chapter
                                          无 input/output 分桶,total_tokens 是合并值;
                                          P4 简化:total_input_tokens = SUM(total_tokens)
                                          作上界占位,P5+ metrics_per_chapter 加分桶字段
                                          后再切;total_output_tokens 同 0 占位)
                                          → 实施改为 SUM(total_tokens) → total_input_tokens,
                                          total_output_tokens = 0(spec 字面要分两字段,
                                          但当前 schema 无分桶源,只能取一边)
- ``total_output_tokens``               ← 0(同上)
- ``total_cost_usd``                    ← SUM(total_cost_usd)
- ``avg_chapter_latency_seconds``       ← AVG(total_latency_seconds),无样本 → None
- ``qwen_calls`` / claude_sonnet/opus   ← SUM(writer_calls) per model prefix bucket
"""

from __future__ import annotations

import datetime as _dt
from collections.abc import Callable

import structlog
from sqlalchemy import func, select
from sqlalchemy.orm import Session

from src.core.db.models import (
    AttemptLedgerORM,
    MetricsDailyORM,
    MetricsPerChapterORM,
)

_log = structlog.get_logger(__name__)

MetricsAggregatorSessionFactory = Callable[[], Session]
"""零参 session 工厂(``ChapterService`` 把"按 workspace_id 开 session"包成
零参再注入,与 P4-B-02 ``persist_*_safely`` / P4-B-03 ``write_per_chapter_safely``
helper 同形)。"""


def aggregate_daily_safely(
    session_factory: MetricsAggregatorSessionFactory | None,
    *,
    workspace_id: str,
    date: _dt.date,
) -> None:
    """全量重算 ``date`` 当日工作区 metrics_daily 行,失败 swallow + structlog log。

    ``session_factory=None`` → no-op(P1 兼容路径,无 DB 写)。给定时打开短
    session、聚合 ``metrics_per_chapter`` 当日所有行、upsert ``metrics_daily``
    主键 ``date`` 行、commit、close。事务边界全在本函数内,**不冒泡**——主
    流水线已 APPROVED,聚合持久化是辅助任务,失败不应回滚状态机(对齐
    P2-B-19 / P3-B-07 / P3-B-13 / P4-B-02 / P4-B-03 六先例)。

    幂等:每次调用全量重算,重复触发不会双计数(metrics_per_chapter 主键
    ``chapter_id`` 已保证不重复)。

    本日无样本:仍 upsert 一行 ``date`` PK 行,counts=0、avg=None;前端
    overview 端点拉到也能正确显示"今天还没有完成的章节"。
    """
    if session_factory is None:
        return
    try:
        session = session_factory()
    except Exception as exc:  # noqa: BLE001 — swallow per P2-B-19 / P4-B-02 / P4-B-03 先例
        _log.error(
            "metrics_daily_aggregate_failed",
            workspace_id=workspace_id,
            date=date.isoformat(),
            stage="open_session",
            error_type=type(exc).__name__,
            error_message=str(exc),
        )
        return
    try:
        day_start = _dt.datetime.combine(date, _dt.time.min, tzinfo=_dt.timezone.utc)
        day_end = day_start + _dt.timedelta(days=1)

        rows = list(
            session.execute(
                select(MetricsPerChapterORM).where(
                    MetricsPerChapterORM.approved_at >= day_start,
                    MetricsPerChapterORM.approved_at < day_end,
                )
            ).scalars()
        )

        chapters_drafted = len(rows)
        chapters_approved = chapters_drafted
        chapters_rewritten = sum(1 for r in rows if r.rewrite_count >= 1)
        chapters_revised = sum(1 for r in rows if r.revise_count >= 1)
        chapters_escalated = 0

        if chapters_drafted > 0:
            avg_review_score = sum(r.final_score_overall for r in rows) / chapters_drafted
            avg_rewrites_per_chapter = (
                sum(r.rewrite_count for r in rows) / chapters_drafted
            )
            avg_revises_per_chapter = (
                sum(r.revise_count for r in rows) / chapters_drafted
            )
            avg_chapter_latency_seconds = (
                sum(r.total_latency_seconds for r in rows) / chapters_drafted
            )
            total_input_tokens = sum(r.total_tokens for r in rows)
            total_cost_usd = sum(r.total_cost_usd for r in rows)
        else:
            avg_review_score = None
            avg_rewrites_per_chapter = None
            avg_revises_per_chapter = None
            avg_chapter_latency_seconds = None
            total_input_tokens = 0
            total_cost_usd = 0.0
        total_output_tokens = 0

        provider_models = list(
            session.execute(
                select(AttemptLedgerORM.model).where(
                    AttemptLedgerORM.workspace_id == workspace_id,
                    AttemptLedgerORM.attempt_kind == "provider",
                    AttemptLedgerORM.created_at >= day_start,
                    AttemptLedgerORM.created_at < day_end,
                )
            ).scalars()
        )
        if provider_models:
            qwen_calls = sum(
                1 for model in provider_models if (model or "").lower().startswith("qwen")
            )
            claude_sonnet_calls = sum(
                1
                for model in provider_models
                if (model or "").lower().startswith("claude-sonnet")
            )
            claude_opus_calls = sum(
                1
                for model in provider_models
                if (model or "").lower().startswith("claude-opus")
            )
        else:
            qwen_calls = 0
            claude_sonnet_calls = 0
            claude_opus_calls = 0
            for r in rows:
                writer_model = (r.writer_model or "").lower()
                calls = int(r.writer_calls or 0)
                if writer_model.startswith("qwen"):
                    qwen_calls += calls
                elif writer_model.startswith("claude-sonnet"):
                    claude_sonnet_calls += calls
                elif writer_model.startswith("claude-opus"):
                    claude_opus_calls += calls

        existing = session.get(MetricsDailyORM, date)
        if existing is None:
            row = MetricsDailyORM(
                date=date,
                chapters_drafted=chapters_drafted,
                chapters_approved=chapters_approved,
                chapters_rewritten=chapters_rewritten,
                chapters_revised=chapters_revised,
                chapters_escalated=chapters_escalated,
                avg_review_score=avg_review_score,
                avg_rewrites_per_chapter=avg_rewrites_per_chapter,
                avg_revises_per_chapter=avg_revises_per_chapter,
                total_input_tokens=total_input_tokens,
                total_output_tokens=total_output_tokens,
                total_cost_usd=float(total_cost_usd),
                avg_chapter_latency_seconds=avg_chapter_latency_seconds,
                qwen_calls=qwen_calls,
                claude_sonnet_calls=claude_sonnet_calls,
                claude_opus_calls=claude_opus_calls,
            )
            session.add(row)
        else:
            existing.chapters_drafted = chapters_drafted
            existing.chapters_approved = chapters_approved
            existing.chapters_rewritten = chapters_rewritten
            existing.chapters_revised = chapters_revised
            existing.chapters_escalated = chapters_escalated
            existing.avg_review_score = avg_review_score
            existing.avg_rewrites_per_chapter = avg_rewrites_per_chapter
            existing.avg_revises_per_chapter = avg_revises_per_chapter
            existing.total_input_tokens = total_input_tokens
            existing.total_output_tokens = total_output_tokens
            existing.total_cost_usd = float(total_cost_usd)
            existing.avg_chapter_latency_seconds = avg_chapter_latency_seconds
            existing.qwen_calls = qwen_calls
            existing.claude_sonnet_calls = claude_sonnet_calls
            existing.claude_opus_calls = claude_opus_calls

        session.commit()
        _log.info(
            "metrics_daily_aggregated",
            workspace_id=workspace_id,
            date=date.isoformat(),
            chapters_drafted=chapters_drafted,
            chapters_approved=chapters_approved,
            chapters_rewritten=chapters_rewritten,
            chapters_revised=chapters_revised,
            avg_review_score=avg_review_score,
            total_cost_usd=float(total_cost_usd),
        )
    except Exception as exc:  # noqa: BLE001 — swallow per P2-B-19 / P4-B-02 / P4-B-03 先例
        session.rollback()
        _log.error(
            "metrics_daily_aggregate_failed",
            workspace_id=workspace_id,
            date=date.isoformat(),
            stage="commit",
            error_type=type(exc).__name__,
            error_message=str(exc),
        )
    finally:
        session.close()


def get_aggregated_row(
    session: Session, date: _dt.date
) -> MetricsDailyORM | None:
    """单行读取(供 metrics API ``/daily`` 端点 + 单元测试)。

    主键查找,nullable;不暴露在 ``aggregate_daily_safely`` 失败容错语义之
    下,纯 read-side helper。
    """
    return session.get(MetricsDailyORM, date)


def list_aggregated_rows(
    session: Session,
    *,
    since: _dt.date | None = None,
    until: _dt.date | None = None,
) -> list[MetricsDailyORM]:
    """范围读取(供 metrics API ``/daily?since=&until=`` 端点)。

    ``since`` / ``until`` 都是 inclusive(``BETWEEN``);任一为 None 时退化
    为单边或全集。结果按 ``date`` asc 排序。
    """
    stmt = select(MetricsDailyORM)
    if since is not None:
        stmt = stmt.where(MetricsDailyORM.date >= since)
    if until is not None:
        stmt = stmt.where(MetricsDailyORM.date <= until)
    stmt = stmt.order_by(MetricsDailyORM.date.asc())
    return list(session.execute(stmt).scalars())


def compute_overview(session: Session) -> dict[str, float | int | None]:
    """算工作区累计概览(供 metrics API ``/overview`` 端点)。

    返回:
    - ``chapters_approved``:metrics_per_chapter 总行数(累计 APPROVED 章节)
    - ``total_cost_usd``:metrics_per_chapter total_cost_usd 累加
    - ``total_tokens``:metrics_per_chapter total_tokens 累加
    - ``avg_review_score``:metrics_per_chapter final_score_overall 平均
      (无样本 → None)
    - ``avg_chapter_latency_seconds``:total_latency_seconds 平均
      (无样本 → None)
    """
    chapters_approved = (
        session.scalar(
            select(func.count()).select_from(MetricsPerChapterORM)
        )
        or 0
    )
    if chapters_approved == 0:
        return {
            "chapters_approved": 0,
            "total_cost_usd": 0.0,
            "total_tokens": 0,
            "avg_review_score": None,
            "avg_chapter_latency_seconds": None,
        }
    total_cost_usd = float(
        session.scalar(
            select(func.coalesce(func.sum(MetricsPerChapterORM.total_cost_usd), 0.0))
        )
        or 0.0
    )
    total_tokens = int(
        session.scalar(
            select(func.coalesce(func.sum(MetricsPerChapterORM.total_tokens), 0))
        )
        or 0
    )
    avg_review_score = session.scalar(
        select(func.avg(MetricsPerChapterORM.final_score_overall))
    )
    avg_chapter_latency_seconds = session.scalar(
        select(func.avg(MetricsPerChapterORM.total_latency_seconds))
    )
    return {
        "chapters_approved": int(chapters_approved),
        "total_cost_usd": total_cost_usd,
        "total_tokens": total_tokens,
        "avg_review_score": (
            float(avg_review_score) if avg_review_score is not None else None
        ),
        "avg_chapter_latency_seconds": (
            float(avg_chapter_latency_seconds)
            if avg_chapter_latency_seconds is not None
            else None
        ),
    }
