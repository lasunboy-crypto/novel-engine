"""RateLimitMiddleware 集成测试(P4-B-10)。

覆盖契约:

- 每 workspace 滑窗,默认 100 req/min — 测时用小 limit 触发
- 超限返 429 + ``RATE_LIMIT_EXCEEDED`` code + details 含 workspace_id /
  limit_per_minute / window_seconds
- 窗口外的请求被弹出(time 注入控制时间流)
- 不同 workspace 不共桶(X-Workspace-Id header / URL path 两种识别)
- 健康检查 / docs 跳过限流(``/api/v1/health`` / ``/openapi.json``)
- 超限触发 WS publish 到 ``system.alerts`` 频道(event=rate_limit_exceeded)
- WS publish 失败 swallow(429 响应仍返)
- 429 响应被 ``ApiMetricsMiddleware`` 看到 + 计入 system_metrics_daily.api_errors
- 匿名(无 workspace 上下文)请求归到匿名桶
"""

from __future__ import annotations

import datetime as _dt
from pathlib import Path
from unittest.mock import MagicMock

from fastapi import FastAPI
from fastapi.testclient import TestClient
from sqlalchemy import create_engine
from sqlalchemy.orm import Session
from structlog.testing import capture_logs

from src.api.errors import (
    ApiErrorTranslationMiddleware,
    register_exception_handlers,
)
from src.api.middleware import (
    ANONYMOUS_WORKSPACE_KEY,
    AccessLogMiddleware,
    ApiMetricsMiddleware,
    RateLimitMiddleware,
    RequestIDMiddleware,
)
from src.api.websocket.manager import WebSocketManager
from src.core.db import Base
from src.core.db.models import SystemMetricsDailyORM


# =============================================================================
# 公共 fixture / app builder
# =============================================================================


def _build_app(
    *,
    limit: int = 3,
    window: float = 60.0,
    now: callable | None = None,
    ws_manager: WebSocketManager | None = None,
    system_session_factory: callable | None = None,
) -> FastAPI:
    """构造一个最小 FastAPI app 挂上 RateLimitMiddleware,供测试 hit。"""
    app = FastAPI()

    @app.get("/api/v1/workspaces/{workspace_id}/dummy")
    async def _dummy(workspace_id: str) -> dict:
        return {"workspace_id": workspace_id}

    @app.get("/api/v1/dummy")
    async def _global_dummy() -> dict:
        return {"ok": True}

    @app.get("/api/v1/health")
    async def _health() -> dict:
        return {"ok": True}

    # 顺序对齐 main.py:Translation(最内)→ RateLimit → ApiMetrics →
    # AccessLog → RequestID。后注册的最外层。
    app.add_middleware(ApiErrorTranslationMiddleware)
    app.add_middleware(
        RateLimitMiddleware,
        limit_per_minute=limit,
        window_seconds=window,
        ws_manager=ws_manager,
        now=now,
    )
    app.add_middleware(
        ApiMetricsMiddleware, session_factory=system_session_factory
    )
    app.add_middleware(AccessLogMiddleware)
    app.add_middleware(RequestIDMiddleware)
    register_exception_handlers(app)
    return app


class _MutableClock:
    """注入 ``time`` 工厂以控制窗口滚动。"""

    def __init__(self, start: float = 0.0) -> None:
        self.now = start

    def __call__(self) -> float:
        return self.now

    def advance(self, seconds: float) -> None:
        self.now += seconds


# =============================================================================
# 限流核心契约
# =============================================================================


class TestRateLimitMiddleware:
    def test_first_n_requests_pass(self) -> None:
        clock = _MutableClock()
        app = _build_app(limit=3, window=60.0, now=clock)
        with TestClient(app) as client:
            for _ in range(3):
                resp = client.get("/api/v1/workspaces/ws-a/dummy")
                assert resp.status_code == 200

    def test_over_limit_returns_429(self) -> None:
        clock = _MutableClock()
        app = _build_app(limit=2, window=60.0, now=clock)
        with TestClient(app) as client:
            client.get("/api/v1/workspaces/ws-a/dummy")
            client.get("/api/v1/workspaces/ws-a/dummy")
            resp = client.get("/api/v1/workspaces/ws-a/dummy")
            assert resp.status_code == 429
            body = resp.json()
            assert body["error"]["code"] == "RATE_LIMIT_EXCEEDED"
            details = body["error"]["details"]
            assert details["workspace_id"] == "ws-a"
            assert details["limit_per_minute"] == 2

    def test_window_rollover_allows_new_request(self) -> None:
        clock = _MutableClock(start=0.0)
        app = _build_app(limit=2, window=60.0, now=clock)
        with TestClient(app) as client:
            client.get("/api/v1/workspaces/ws-a/dummy")
            client.get("/api/v1/workspaces/ws-a/dummy")
            # 满了
            assert (
                client.get("/api/v1/workspaces/ws-a/dummy").status_code == 429
            )
            # 时钟前进 61 秒(超出 60 秒窗口),前两条时间戳被弹出
            clock.advance(61.0)
            assert (
                client.get("/api/v1/workspaces/ws-a/dummy").status_code == 200
            )

    def test_per_workspace_bucket_isolation_via_url_path(self) -> None:
        clock = _MutableClock()
        app = _build_app(limit=2, window=60.0, now=clock)
        with TestClient(app) as client:
            client.get("/api/v1/workspaces/ws-a/dummy")
            client.get("/api/v1/workspaces/ws-a/dummy")
            # ws-a 满了
            assert (
                client.get("/api/v1/workspaces/ws-a/dummy").status_code == 429
            )
            # ws-b 仍空
            assert (
                client.get("/api/v1/workspaces/ws-b/dummy").status_code == 200
            )

    def test_per_workspace_bucket_isolation_via_header(self) -> None:
        clock = _MutableClock()
        app = _build_app(limit=2, window=60.0, now=clock)
        with TestClient(app) as client:
            client.get(
                "/api/v1/dummy", headers={"X-Workspace-Id": "ws-h-a"}
            )
            client.get(
                "/api/v1/dummy", headers={"X-Workspace-Id": "ws-h-a"}
            )
            assert (
                client.get(
                    "/api/v1/dummy", headers={"X-Workspace-Id": "ws-h-a"}
                ).status_code
                == 429
            )
            # 不同 header 值不共桶
            assert (
                client.get(
                    "/api/v1/dummy", headers={"X-Workspace-Id": "ws-h-b"}
                ).status_code
                == 200
            )

    def test_health_path_is_skipped(self) -> None:
        clock = _MutableClock()
        app = _build_app(limit=1, window=60.0, now=clock)
        with TestClient(app) as client:
            # 健康检查可疯狂 hit 不被限。
            for _ in range(10):
                resp = client.get("/api/v1/health")
                assert resp.status_code == 200

    def test_anonymous_bucket_for_no_workspace_context(self) -> None:
        clock = _MutableClock()
        app = _build_app(limit=2, window=60.0, now=clock)
        with TestClient(app) as client:
            # 无 workspace 上下文 → 匿名桶
            client.get("/api/v1/dummy")
            client.get("/api/v1/dummy")
            assert client.get("/api/v1/dummy").status_code == 429
            # 加 header 可绕过(走独立 ws-x 桶)
            assert (
                client.get(
                    "/api/v1/dummy", headers={"X-Workspace-Id": "ws-x"}
                ).status_code
                == 200
            )

    def test_rate_limit_emits_ws_publish(self) -> None:
        clock = _MutableClock()
        ws_manager = MagicMock(spec=WebSocketManager)
        app = _build_app(
            limit=1, window=60.0, now=clock, ws_manager=ws_manager
        )
        with TestClient(app) as client:
            client.get("/api/v1/workspaces/ws-a/dummy")
            client.get("/api/v1/workspaces/ws-a/dummy")  # 触发 429
        # 至少 1 次 publish 调用
        ws_manager.publish.assert_called_once()
        call = ws_manager.publish.call_args
        assert call.kwargs["channel"] == "system.alerts"
        assert call.kwargs["event"] == "rate_limit_exceeded"
        data = call.kwargs["data"]
        assert data["workspace_id"] == "ws-a"
        assert data["limit_per_minute"] == 1
        assert data["path"] == "/api/v1/workspaces/ws-a/dummy"

    def test_ws_publish_failure_swallowed(self) -> None:
        clock = _MutableClock()
        ws_manager = MagicMock(spec=WebSocketManager)
        ws_manager.publish.side_effect = RuntimeError("ws boom")
        app = _build_app(
            limit=1, window=60.0, now=clock, ws_manager=ws_manager
        )
        with TestClient(app) as client:
            with capture_logs() as logs:
                client.get("/api/v1/workspaces/ws-a/dummy")
                resp = client.get("/api/v1/workspaces/ws-a/dummy")
        # 429 仍返(WS 失败不阻塞响应)
        assert resp.status_code == 429
        # structlog 落 rate_limit_notify_failed
        events = [
            e for e in logs if e["event"] == "rate_limit_notify_failed"
        ]
        assert len(events) >= 1
        assert events[0]["error_type"] == "RuntimeError"

    def test_429_counted_as_api_error_in_system_metrics(
        self, tmp_path: Path
    ) -> None:
        # 启动指令注 ⑧:429 响应也算入 system_metrics_daily.api_errors
        # (因 RateLimit 挂在 ApiMetrics 内层,429 经过 ApiMetrics 时
        # status_code>=400 → api_errors+=1)。
        db_path = tmp_path / "_system_metrics.db"
        engine = create_engine(f"sqlite:///{db_path}")
        Base.metadata.create_all(engine)

        def _factory() -> Session:
            return Session(engine)

        clock = _MutableClock()
        app = _build_app(
            limit=1,
            window=60.0,
            now=clock,
            system_session_factory=_factory,
        )
        with TestClient(app) as client:
            client.get("/api/v1/workspaces/ws-a/dummy")  # 200
            client.get("/api/v1/workspaces/ws-a/dummy")  # 429

        session = _factory()
        try:
            today = _dt.datetime.now(_dt.timezone.utc).date()
            row = session.get(SystemMetricsDailyORM, today)
            assert row is not None
            assert row.api_requests == 2
            assert row.api_errors == 1  # 一次 429
        finally:
            session.close()


# =============================================================================
# 工具函数 / 常量
# =============================================================================


class TestResolveWorkspaceKey:
    def test_anonymous_constant_exists(self) -> None:
        assert ANONYMOUS_WORKSPACE_KEY == "__anonymous__"
