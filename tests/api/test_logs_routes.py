"""logs 路由集成测试(P4-B-05)。

覆盖 OBSERVABILITY §2.6 字面端点契约:

- ``GET /api/v1/logs?...``  分页查询 + 多字段过滤
- ``GET /api/v1/logs/trace/{trace_id}``    Trace 视图(智能分流)
- ``GET /api/v1/logs/trace/{request_id}``  同上,按 ``req_`` 前缀分流
- WS  ``/api/v1/logs/stream``              实时日志流

测试基础设施:``app.dependency_overrides[get_logs_service] = ...``,fixture
工作区指向 ``tmp_path``,种子 jsonl 行直接写到 ``tmp_path/system_logs/`` 与
``tmp_path/<workspace_id>/logs/`` 模拟生产 jsonl 落盘形态。
"""

from __future__ import annotations

import json
from collections.abc import Iterator
from pathlib import Path

import pytest
from fastapi.testclient import TestClient

from src.api.main import create_app
from src.api.routes.logs import get_logs_service
from src.services.logs_service import LogsService


# ---- fixtures --------------------------------------------------------------


def _write_jsonl(path: Path, rows: list[dict]) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    with path.open("w", encoding="utf-8") as fh:
        for r in rows:
            fh.write(json.dumps(r) + "\n")


@pytest.fixture
def seeded_data_dir(tmp_path: Path) -> Path:
    """种子两份 jsonl:系统级 + 工作区级。"""
    sys_rows = [
        {
            "timestamp": "2026-05-10T10:00:00.123Z",
            "level": "INFO",
            "logger": "src.api.middleware",
            "event": "api_request",
            "request_id": "req_abc12300beef",
            "method": "GET",
            "path": "/api/v1/health",
            "status_code": 200,
            "latency_ms": 5.2,
        },
        {
            "timestamp": "2026-05-10T10:00:01.456Z",
            "level": "WARNING",
            "logger": "src.api.middleware",
            "event": "api_request",
            "request_id": "req_abc12300beef",
            "method": "GET",
            "path": "/api/v1/missing",
            "status_code": 404,
            "latency_ms": 3.1,
        },
        {
            "timestamp": "2026-05-10T11:00:00.000Z",
            "level": "INFO",
            "logger": "src.api.main",
            "event": "system_startup",
            "version": "0.1.0",
        },
    ]
    ws_rows = [
        {
            "timestamp": "2026-05-10T10:30:00.000Z",
            "level": "INFO",
            "logger": "src.core.agents.writer",
            "event": "chapter_draft_completed",
            "trace_id": "ch-0001-write0",
            "workspace_id": "ws-logs-demo",
            "chapter_id": "ch-0001",
            "word_count": 4203,
            "model_used": "mock",
        },
        {
            "timestamp": "2026-05-10T10:30:05.000Z",
            "level": "INFO",
            "logger": "src.core.agents.reviewer",
            "event": "review_completed",
            "trace_id": "ch-0001-write0",
            "workspace_id": "ws-logs-demo",
            "chapter_id": "ch-0001",
            "score_overall": 8.4,
            "verdict": "pass",
        },
    ]
    _write_jsonl(tmp_path / "system_logs" / "2026-05-10.jsonl", sys_rows)
    _write_jsonl(
        tmp_path / "ws-logs-demo" / "logs" / "2026-05-10.jsonl", ws_rows
    )
    return tmp_path


@pytest.fixture
def client(seeded_data_dir: Path) -> Iterator[TestClient]:
    app = create_app()
    app.dependency_overrides[get_logs_service] = lambda: LogsService(
        data_dir=seeded_data_dir
    )
    with TestClient(app) as c:
        yield c


# ---- GET /logs ------------------------------------------------------------


class TestListLogs:
    """``GET /api/v1/logs?...``"""

    def test_unfiltered_returns_all(self, client: TestClient) -> None:
        resp = client.get("/api/v1/logs")
        assert resp.status_code == 200
        data = resp.json()["data"]
        # 系统级 3 + 工作区 2 = 5 行
        assert data["total"] == 5
        assert data["page"] == 1
        assert data["page_size"] == 50
        assert len(data["items"]) == 5
        # timestamp 升序
        timestamps = [it["timestamp"] for it in data["items"]]
        assert timestamps == sorted(timestamps)

    def test_filter_by_workspace_id(self, client: TestClient) -> None:
        resp = client.get(
            "/api/v1/logs", params={"workspace_id": "ws-logs-demo"}
        )
        assert resp.status_code == 200
        data = resp.json()["data"]
        assert data["total"] == 2
        for item in data["items"]:
            assert item["workspace_id"] == "ws-logs-demo"

    def test_filter_by_event(self, client: TestClient) -> None:
        resp = client.get("/api/v1/logs", params={"event": "api_request"})
        data = resp.json()["data"]
        assert data["total"] == 2
        for item in data["items"]:
            assert item["event"] == "api_request"

    def test_filter_by_level_case_insensitive(self, client: TestClient) -> None:
        resp = client.get("/api/v1/logs", params={"level": "warning"})
        data = resp.json()["data"]
        assert data["total"] == 1
        assert data["items"][0]["level"] == "WARNING"

    def test_filter_by_chapter_id(self, client: TestClient) -> None:
        resp = client.get("/api/v1/logs", params={"chapter_id": "ch-0001"})
        data = resp.json()["data"]
        assert data["total"] == 2
        for item in data["items"]:
            assert item["chapter_id"] == "ch-0001"

    def test_filter_by_since_until(self, client: TestClient) -> None:
        resp = client.get(
            "/api/v1/logs",
            params={
                "since": "2026-05-10T10:30:00Z",
                "until": "2026-05-10T10:59:59Z",
            },
        )
        data = resp.json()["data"]
        assert data["total"] == 2
        for item in data["items"]:
            ts = item["timestamp"]
            assert "2026-05-10T10:30" in ts or "2026-05-10T10:30:05" in ts

    def test_pagination(self, client: TestClient) -> None:
        resp = client.get(
            "/api/v1/logs", params={"page": 2, "page_size": 2}
        )
        data = resp.json()["data"]
        assert data["page"] == 2
        assert data["page_size"] == 2
        assert data["total"] == 5
        assert len(data["items"]) == 2

    def test_extra_fields_collected_into_extra(self, client: TestClient) -> None:
        resp = client.get("/api/v1/logs", params={"event": "system_startup"})
        item = resp.json()["data"]["items"][0]
        # ``version`` 是动态字段,应该被收进 extra
        assert item["extra"]["version"] == "0.1.0"
        # 核心字段保留
        assert item["event"] == "system_startup"
        assert item["level"] == "INFO"

    def test_no_logs_dir_returns_empty(self, tmp_path: Path) -> None:
        """data_dir 为空(无任何 jsonl)→ 200 + 空数组,不报错。"""
        app = create_app()
        empty_dir = tmp_path / "empty"
        empty_dir.mkdir()
        app.dependency_overrides[get_logs_service] = lambda: LogsService(
            data_dir=empty_dir
        )
        with TestClient(app) as client:
            resp = client.get("/api/v1/logs")
            assert resp.status_code == 200
            data = resp.json()["data"]
            assert data["total"] == 0
            assert data["items"] == []


# ---- GET /logs/trace/{id} -------------------------------------------------


class TestTraceView:
    """``GET /api/v1/logs/trace/{id}`` 智能分流。"""

    def test_trace_id_returns_chapter_trace(self, client: TestClient) -> None:
        resp = client.get("/api/v1/logs/trace/ch-0001-write0")
        assert resp.status_code == 200
        data = resp.json()["data"]
        assert data["kind"] == "trace_id"
        assert data["id"] == "ch-0001-write0"
        assert data["total"] == 2
        events = [it["event"] for it in data["items"]]
        assert events == ["chapter_draft_completed", "review_completed"]

    def test_request_id_returns_api_trace(self, client: TestClient) -> None:
        resp = client.get("/api/v1/logs/trace/req_abc12300beef")
        assert resp.status_code == 200
        data = resp.json()["data"]
        assert data["kind"] == "request_id"
        assert data["id"] == "req_abc12300beef"
        assert data["total"] == 2
        for item in data["items"]:
            assert item["request_id"] == "req_abc12300beef"

    def test_unknown_id_returns_empty(self, client: TestClient) -> None:
        resp = client.get("/api/v1/logs/trace/ch-9999-write0")
        assert resp.status_code == 200
        data = resp.json()["data"]
        assert data["kind"] == "trace_id"
        assert data["total"] == 0
        assert data["items"] == []
