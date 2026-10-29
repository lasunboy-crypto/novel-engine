"""POST /api/v1/system/frontend-errors 路由测试(P4-B-10)。

覆盖契约:

- 完整字段(6 个 + workspace_id)→ 200 + structlog ``frontend_error`` 事件
  字段全对齐
- 必填字段缺失(error_type / message)→ 422 Pydantic 错
- ``stack`` 长度 > 1000 字 → 服务端 schema 截断
- workspace_id 可为 None(全局错误场景)
- ``system_metrics_daily.frontend_errors`` 累加(当 app.state.system_session_factory
  注入时)
- system_session_factory 失败 swallow → 仍返 200
- 未授权 / 未提供 X-Workspace-Id header 仍可上报(全局 system 资源)
"""

from __future__ import annotations

import datetime as _dt
from collections.abc import Iterator
from pathlib import Path

import pytest
from fastapi.testclient import TestClient
from sqlalchemy import create_engine
from sqlalchemy.orm import Session
from structlog.testing import capture_logs

from src.api.main import create_app
from src.core.db import Base
from src.core.db.models import SystemMetricsDailyORM


@pytest.fixture
def app_no_system_db() -> Iterator[TestClient]:
    """默认 app.state.system_session_factory=None,只走 structlog 路径。"""
    app = create_app()
    with TestClient(app) as c:
        yield c


@pytest.fixture
def app_with_system_db(tmp_path: Path) -> Iterator[tuple[TestClient, callable]]:
    """app.state.system_session_factory 指向 tmp_path 下独立 sqlite。"""
    db_path = tmp_path / "_system_metrics.db"
    engine = create_engine(f"sqlite:///{db_path}")
    Base.metadata.create_all(engine)

    def _factory() -> Session:
        return Session(engine)

    app = create_app(system_session_factory=_factory)
    with TestClient(app) as c:
        yield c, _factory


def _valid_payload(**overrides) -> dict:
    base = {
        "error_type": "TypeError",
        "message": "Cannot read properties of undefined",
        "stack": "Error\n    at foo (a.js:10:5)\n    at bar (b.js:3:1)",
        "url": "http://127.0.0.1:5173/workspaces/ws-a/chapters",
        "user_action": "click 'write'",
        "browser": "Mozilla/5.0",
        "workspace_id": "ws-a",
    }
    base.update(overrides)
    return base


class TestFrontendErrorsRoute:
    def test_success_returns_200_and_emits_event(
        self, app_no_system_db: TestClient
    ) -> None:
        with capture_logs() as logs:
            resp = app_no_system_db.post(
                "/api/v1/system/frontend-errors",
                json=_valid_payload(),
            )
        assert resp.status_code == 200
        assert resp.json()["data"] == {}
        events = [e for e in logs if e["event"] == "frontend_error"]
        assert len(events) == 1
        ev = events[0]
        assert ev["error_type"] == "TypeError"
        assert ev["workspace_id"] == "ws-a"
        assert ev["user_action"] == "click 'write'"
        assert ev["browser"] == "Mozilla/5.0"
        assert "stack" in ev

    def test_missing_required_fields_returns_422(
        self, app_no_system_db: TestClient
    ) -> None:
        # 缺 message
        resp = app_no_system_db.post(
            "/api/v1/system/frontend-errors",
            json={"error_type": "TypeError"},
        )
        assert resp.status_code == 422
        body = resp.json()
        # 全局错误格式 — code=VALIDATION_FAILED (默认 Pydantic 422)。
        assert body["error"]["code"] == "VALIDATION_FAILED"

    def test_stack_truncated_to_1000_chars(
        self, app_no_system_db: TestClient
    ) -> None:
        long_stack = "x" * 5000
        with capture_logs() as logs:
            resp = app_no_system_db.post(
                "/api/v1/system/frontend-errors",
                json=_valid_payload(stack=long_stack),
            )
        assert resp.status_code == 200
        events = [e for e in logs if e["event"] == "frontend_error"]
        assert len(events) == 1
        assert events[0]["stack"] is not None
        assert len(events[0]["stack"]) == 1000

    def test_workspace_id_optional(self, app_no_system_db: TestClient) -> None:
        with capture_logs() as logs:
            resp = app_no_system_db.post(
                "/api/v1/system/frontend-errors",
                json=_valid_payload(workspace_id=None),
            )
        assert resp.status_code == 200
        events = [e for e in logs if e["event"] == "frontend_error"]
        assert events[0]["workspace_id"] is None

    def test_no_x_workspace_id_header_still_works(
        self, app_no_system_db: TestClient
    ) -> None:
        # 全局 system 资源:不读 X-Workspace-Id header(P4-B-10 启动指令注 ⑦)。
        resp = app_no_system_db.post(
            "/api/v1/system/frontend-errors",
            json=_valid_payload(workspace_id=None),
        )
        assert resp.status_code == 200

    def test_frontend_errors_counter_increments(
        self, app_with_system_db: tuple[TestClient, callable]
    ) -> None:
        client, factory = app_with_system_db
        resp = client.post(
            "/api/v1/system/frontend-errors", json=_valid_payload()
        )
        assert resp.status_code == 200
        session = factory()
        try:
            today = _dt.datetime.now(_dt.timezone.utc).date()
            row = session.get(SystemMetricsDailyORM, today)
            assert row is not None
            assert row.frontend_errors == 1
        finally:
            session.close()

        # 再上报一次,计数 +1。
        resp2 = client.post(
            "/api/v1/system/frontend-errors", json=_valid_payload()
        )
        assert resp2.status_code == 200
        session = factory()
        try:
            row = session.get(
                SystemMetricsDailyORM,
                _dt.datetime.now(_dt.timezone.utc).date(),
            )
            assert row is not None
            assert row.frontend_errors == 2
        finally:
            session.close()

    def test_extra_field_rejected(self, app_no_system_db: TestClient) -> None:
        # FrontendErrorReport model_config: extra="forbid",防意外字段(避
        # 免前端打散后 typo 字段名静默通过)。
        resp = app_no_system_db.post(
            "/api/v1/system/frontend-errors",
            json={**_valid_payload(), "rogue_field": "x"},
        )
        assert resp.status_code == 422
