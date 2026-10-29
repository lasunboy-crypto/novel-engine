"""P4-B-04:system_metrics_daily 写入 helper 单元测试。

覆盖契约:

- record_api_request_safely(None, ...) → no-op
- 首次调用 → 创建当日行,api_requests=1,api_errors 看 status_code
- 二次调用 → 增量更新,api_requests +=1,avg_api_latency_ms 增量平均
- status_code >= 400 → api_errors 增量
- session_factory 抛 → swallow + structlog log
- commit 抛 → swallow + structlog log + rollback
"""

from __future__ import annotations

import datetime as _dt

import pytest
from sqlalchemy import create_engine
from sqlalchemy.orm import Session
from structlog.testing import capture_logs

from src.core.db import Base
from src.core.db.models import SystemMetricsDailyORM
from src.services.system_metrics_writer import record_api_request_safely


@pytest.fixture
def in_memory_engine():
    engine = create_engine("sqlite:///:memory:")
    Base.metadata.create_all(engine)
    yield engine


@pytest.fixture
def factory(in_memory_engine):
    def _factory() -> Session:
        return Session(in_memory_engine)

    return _factory


def test_no_op_when_factory_none() -> None:
    record_api_request_safely(None, status_code=200, latency_ms=12.3)


def test_first_call_creates_row(factory, in_memory_engine) -> None:
    today = _dt.date(2026, 5, 10)
    record_api_request_safely(
        factory, status_code=200, latency_ms=12.3, today=today
    )
    with Session(in_memory_engine) as s:
        row = s.get(SystemMetricsDailyORM, today)
    assert row is not None
    assert row.api_requests == 1
    assert row.api_errors == 0
    assert row.avg_api_latency_ms == pytest.approx(12.3)


def test_first_error_call_creates_error_row(factory, in_memory_engine) -> None:
    today = _dt.date(2026, 5, 10)
    record_api_request_safely(
        factory, status_code=500, latency_ms=200.0, today=today
    )
    with Session(in_memory_engine) as s:
        row = s.get(SystemMetricsDailyORM, today)
    assert row is not None
    assert row.api_requests == 1
    assert row.api_errors == 1
    assert row.avg_api_latency_ms == pytest.approx(200.0)


def test_incremental_avg_latency(factory, in_memory_engine) -> None:
    today = _dt.date(2026, 5, 10)
    record_api_request_safely(
        factory, status_code=200, latency_ms=10.0, today=today
    )
    record_api_request_safely(
        factory, status_code=200, latency_ms=20.0, today=today
    )
    record_api_request_safely(
        factory, status_code=200, latency_ms=30.0, today=today
    )
    with Session(in_memory_engine) as s:
        row = s.get(SystemMetricsDailyORM, today)
    assert row is not None
    assert row.api_requests == 3
    assert row.avg_api_latency_ms == pytest.approx(20.0)


def test_status_4xx_counts_as_error(factory, in_memory_engine) -> None:
    today = _dt.date(2026, 5, 10)
    record_api_request_safely(
        factory, status_code=200, latency_ms=10.0, today=today
    )
    record_api_request_safely(
        factory, status_code=404, latency_ms=10.0, today=today
    )
    record_api_request_safely(
        factory, status_code=422, latency_ms=10.0, today=today
    )
    record_api_request_safely(
        factory, status_code=500, latency_ms=10.0, today=today
    )
    with Session(in_memory_engine) as s:
        row = s.get(SystemMetricsDailyORM, today)
    assert row is not None
    assert row.api_requests == 4
    assert row.api_errors == 3


def test_swallows_factory_exception() -> None:
    def _broken_factory() -> Session:
        raise RuntimeError("[test] 故意挂")

    with capture_logs() as logs:
        record_api_request_safely(
            _broken_factory, status_code=200, latency_ms=10.0
        )
    events = [
        log for log in logs if log.get("event") == "system_metrics_record_failed"
    ]
    assert len(events) == 1
    assert events[0]["stage"] == "open_session"


def test_swallows_commit_exception(in_memory_engine) -> None:
    """commit 抛 → swallow + log。"""
    from sqlalchemy import event

    def _factory() -> Session:
        s = Session(in_memory_engine)

        @event.listens_for(s, "before_commit")
        def _fail(_session: Session) -> None:
            raise RuntimeError("[test] commit 故意失败")

        return s

    with capture_logs() as logs:
        record_api_request_safely(
            _factory, status_code=200, latency_ms=10.0
        )
    events = [
        log for log in logs if log.get("event") == "system_metrics_record_failed"
    ]
    assert len(events) == 1
    assert events[0]["stage"] == "commit"
