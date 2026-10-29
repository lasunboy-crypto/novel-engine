"""系统级 metrics 写入服务(P4-B-04)。

``system_metrics_daily`` 表写入路径:由 ``ApiMetricsMiddleware`` 在每个 HTTP
请求结束时调 ``record_api_request_safely``,upsert 当日跨工作区聚合行。

设计要点
--------

- **每个请求 write-through**:简单、易测、不持有内存状态。SQLite 单连接
  upsert 在 < 1ms 量级,本机调试 throughput 可接受;P5+ 视压力再加 in-process
  buffer + periodic flush 优化(本任务不做)。
- **失败 swallow + structlog log**(对齐 P2-B-19 / P3-B-07 / P3-B-13 / P4-B-02
  / P4-B-03 / P4-B-04 ``aggregate_daily_safely`` 七先例)。系统 metrics 是
  辅助任务,落库失败不应阻塞 API 响应。
- **avg_api_latency_ms 增量更新**:用 SQL 累加器 ``avg_new = (avg_old × N_old
  + latency_new) / (N_old + 1)`` 在 Python 侧计算后写回(SQLite 没原子
  upsert+computation 语法,session 内读改写在短事务内串行安全;并发 worker
  下可能小漂移,P4 简化范畴)。
- **status_code 区分**:``status_code >= 400`` 计入 ``api_errors``;
  ``api_requests`` 永远 +1(无论成功失败),与 OBSERVABILITY §3.1 字段语义
  对齐。
"""

from __future__ import annotations

import datetime as _dt
from collections.abc import Callable

import structlog
from sqlalchemy.orm import Session

from src.core.db.models import SystemMetricsDailyORM

_log = structlog.get_logger(__name__)

SystemMetricsSessionFactory = Callable[[], Session]


def record_api_request_safely(
    session_factory: SystemMetricsSessionFactory | None,
    *,
    status_code: int,
    latency_ms: float,
    today: _dt.date | None = None,
) -> None:
    """记一次 API 请求到 ``system_metrics_daily.<today>`` 行。

    ``session_factory=None`` → no-op(测试 / 兼容路径)。``today=None`` 则取
    UTC ``today()`` (与 caller 显式传相同语义,只是少传一参)。

    upsert 语义:行不存在则 insert(``api_requests=1``,``api_errors`` 看
    status_code,``avg_api_latency_ms=latency_ms``);行已存在则在原值上
    incremental update(``api_requests += 1``,``avg_api_latency_ms`` 用
    增量平均公式)。

    失败 swallow + structlog log(对齐 P4-B-03 / P4-B-04 ``*_safely`` 模式)。
    """
    if session_factory is None:
        return
    if today is None:
        today = _dt.datetime.now(_dt.timezone.utc).date()
    is_error = status_code >= 400
    try:
        session = session_factory()
    except Exception as exc:  # noqa: BLE001 — swallow per *_safely 七先例
        _log.error(
            "system_metrics_record_failed",
            stage="open_session",
            error_type=type(exc).__name__,
            error_message=str(exc),
        )
        return
    try:
        existing = session.get(SystemMetricsDailyORM, today)
        if existing is None:
            session.add(
                SystemMetricsDailyORM(
                    date=today,
                    api_requests=1,
                    api_errors=1 if is_error else 0,
                    avg_api_latency_ms=latency_ms,
                    ws_connections=0,
                    frontend_errors=0,
                    pipeline_runs=0,
                )
            )
        else:
            n_old = existing.api_requests
            avg_old = existing.avg_api_latency_ms
            if avg_old is None or n_old <= 0:
                avg_new = latency_ms
            else:
                avg_new = (avg_old * n_old + latency_ms) / (n_old + 1)
            existing.api_requests = n_old + 1
            if is_error:
                existing.api_errors = existing.api_errors + 1
            existing.avg_api_latency_ms = avg_new
        session.commit()
    except Exception as exc:  # noqa: BLE001 — swallow per *_safely 七先例
        session.rollback()
        _log.error(
            "system_metrics_record_failed",
            stage="commit",
            error_type=type(exc).__name__,
            error_message=str(exc),
        )
    finally:
        session.close()


def record_frontend_error_safely(
    session_factory: SystemMetricsSessionFactory | None,
    *,
    today: _dt.date | None = None,
) -> None:
    """记一次前端错误上报到 ``system_metrics_daily.<today>.frontend_errors``。

    由 ``POST /api/v1/system/frontend-errors`` 路由(P4-B-10)在落
    ``frontend_error`` structlog 事件之后调用。``session_factory=None`` →
    no-op(测试 / 兼容路径)。

    upsert 语义:行不存在则 insert(``api_requests=0``,``frontend_errors=1``);
    行已存在则 ``frontend_errors += 1``。其他字段不动(`api_requests` /
    `api_errors` / `avg_api_latency_ms` 由 ``record_api_request_safely``
    管,各自负责一栏)。

    失败 swallow + structlog log(对齐 ``record_api_request_safely`` 同
    *_safely 七先例)。
    """
    if session_factory is None:
        return
    if today is None:
        today = _dt.datetime.now(_dt.timezone.utc).date()
    try:
        session = session_factory()
    except Exception as exc:  # noqa: BLE001 — swallow per *_safely 七先例
        _log.error(
            "system_metrics_record_failed",
            stage="open_session",
            field="frontend_errors",
            error_type=type(exc).__name__,
            error_message=str(exc),
        )
        return
    try:
        existing = session.get(SystemMetricsDailyORM, today)
        if existing is None:
            session.add(
                SystemMetricsDailyORM(
                    date=today,
                    api_requests=0,
                    api_errors=0,
                    avg_api_latency_ms=0.0,
                    ws_connections=0,
                    frontend_errors=1,
                    pipeline_runs=0,
                )
            )
        else:
            existing.frontend_errors = existing.frontend_errors + 1
        session.commit()
    except Exception as exc:  # noqa: BLE001 — swallow per *_safely 七先例
        session.rollback()
        _log.error(
            "system_metrics_record_failed",
            stage="commit",
            field="frontend_errors",
            error_type=type(exc).__name__,
            error_message=str(exc),
        )
    finally:
        session.close()
