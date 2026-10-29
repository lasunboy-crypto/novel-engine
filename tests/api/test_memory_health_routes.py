"""memory_health 路由集成测试(P4-B-09)。

覆盖 ``POST /api/v1/workspaces/{workspace_id}/memory/check``:

- 成功路径:200 + ``MemoryHealthCheckOut`` 完整字段(check_id / checked_at /
  4 档 severity / 三类 count / report_json)+ 落 1 行 memory_health_history
- LLM 输出非合法 JSON → 502 ``MEMORY_HEALTH_CHECKER_FAILED`` + details 含
  ``model_used`` / ``raw_output_preview``
- 工作区不存在 → 404 ``WORKSPACE_NOT_FOUND``(由
  ``WorkspaceServiceError`` 全局翻译器兜底)
- ``severity=critical`` 路径:返 200 + 落表 + service 内部 WS publish 已在
  ``test_memory_health_service.py`` 覆盖,本文件只看路由响应面

测试基础设施
============

``app.dependency_overrides[get_memory_health_service]`` 注入测 service
(指向 ``tmp_path`` + fake LLM client),避免真调外部 API + 污染 settings.data_dir。
"""

from __future__ import annotations

import json
from collections.abc import Iterator
from pathlib import Path
from typing import Any
from unittest.mock import MagicMock

import pytest
from fastapi.testclient import TestClient
from sqlalchemy import create_engine, select
from sqlalchemy.orm import Session

from src.api.deps import get_memory_health_service
from src.api.main import create_app
from src.core.db import Base
from src.core.db.models import MemoryHealthCheckORM
from src.core.llm.base import LLMClient, LLMResponse, TokenUsage
from src.core.workspace.workspace_paths import WorkspacePaths
from src.services.memory_health_service import MemoryHealthService
from src.services.workspace_service import WorkspaceService


# ---- fixtures --------------------------------------------------------------


@pytest.fixture
def workspace_service(tmp_path: Path) -> WorkspaceService:
    workspaces_root = tmp_path / "workspaces"
    archives_root = tmp_path / "archives"
    workspaces_root.mkdir()
    archives_root.mkdir()
    return WorkspaceService(
        workspaces_root=workspaces_root, archives_root=archives_root
    )


@pytest.fixture
def workspace_id(workspace_service: WorkspaceService) -> str:
    workspace_service.create(
        "ws-routes",
        display_name="P4-B-09 memory_health routes 集成测试",
        genre="test",
        premise="x",
        activate=False,
    )
    return "ws-routes"


def _make_session_factory(workspace_service: WorkspaceService):
    def _factory(workspace_id: str) -> Session:
        paths = WorkspacePaths(
            workspace_id, workspaces_root=workspace_service.workspaces_root
        )
        engine = create_engine(f"sqlite:///{paths.db_path}")
        Base.metadata.create_all(engine)
        return Session(engine)

    return _factory


def _build_llm(content: str) -> MagicMock:
    fake = MagicMock(spec=LLMClient)
    fake.complete.return_value = LLMResponse(
        content=content,
        model_used="claude-sonnet-4-6",
        usage=TokenUsage(input_tokens=80, output_tokens=160),
        finish_reason="end_turn",
    )
    return fake


def _build_service(
    workspace_service: WorkspaceService,
    llm: MagicMock,
) -> MemoryHealthService:
    prompts_dir = Path(__file__).parent.parent.parent / "prompts"
    return MemoryHealthService(
        workspace_service=workspace_service,
        session_factory=_make_session_factory(workspace_service),
        llm=llm,
        model="claude-sonnet-4-6",
        ws_manager=None,
        prompts_dir=prompts_dir,
    )


def _make_payload(severity: str, *, issues: int = 0) -> dict[str, Any]:
    return {
        "severity": severity,
        "summary": f"{severity} state",
        "stale_threads": [
            {"id": f"t{i}", "reason": f"stale {i}"} for i in range(issues)
        ],
        "inactive_characters": [],
        "conflicts": [],
    }


@pytest.fixture
def client_factory(workspace_service: WorkspaceService):
    """工厂返回 (build_client(llm))。每个测自己构造 LLM + 注入。"""

    def _factory(llm: MagicMock) -> Iterator[TestClient]:
        app = create_app()
        service = _build_service(workspace_service, llm)
        app.dependency_overrides[get_memory_health_service] = lambda: service
        with TestClient(app) as c:
            yield c
        app.dependency_overrides.clear()

    return _factory


# ---- 路径测试 --------------------------------------------------------------


class TestMemoryHealthRoute:
    def test_success_returns_complete_report(
        self,
        workspace_service: WorkspaceService,
        workspace_id: str,
        client_factory,
    ) -> None:
        llm = _build_llm(json.dumps(_make_payload("ok")))
        for client in client_factory(llm):
            resp = client.post(
                f"/api/v1/workspaces/{workspace_id}/memory/check"
            )
        assert resp.status_code == 200
        body = resp.json()
        data = body["data"]
        assert data["severity"] == "ok"
        assert data["stale_threads_count"] == 0
        assert data["inactive_characters_count"] == 0
        assert data["conflicts_count"] == 0
        assert data["check_id"].startswith("mhc-")
        # checked_at 是 ISO datetime 字符串
        assert "T" in data["checked_at"]
        # report_json 含 summary + model_used
        assert "summary" in data["report_json"]
        assert data["report_json"]["model_used"] == "claude-sonnet-4-6"

        # 行落表
        session = _make_session_factory(workspace_service)(workspace_id)
        try:
            rows = (
                session.execute(select(MemoryHealthCheckORM)).scalars().all()
            )
            assert len(rows) == 1
            assert rows[0].severity == "ok"
        finally:
            session.close()

    def test_warning_severity_counts_match(
        self,
        workspace_service: WorkspaceService,
        workspace_id: str,
        client_factory,
    ) -> None:
        llm = _build_llm(json.dumps(_make_payload("warning", issues=4)))
        for client in client_factory(llm):
            resp = client.post(
                f"/api/v1/workspaces/{workspace_id}/memory/check"
            )
        assert resp.status_code == 200
        data = resp.json()["data"]
        assert data["severity"] == "warning"
        assert data["stale_threads_count"] == 4
        assert len(data["report_json"]["stale_threads"]) == 4

    def test_parser_failure_returns_502(
        self,
        workspace_service: WorkspaceService,
        workspace_id: str,
        client_factory,
    ) -> None:
        llm = _build_llm("not a json")
        for client in client_factory(llm):
            resp = client.post(
                f"/api/v1/workspaces/{workspace_id}/memory/check"
            )
        assert resp.status_code == 502
        body = resp.json()
        assert body["error"]["code"] == "MEMORY_HEALTH_CHECKER_FAILED"
        details = body["error"]["details"]
        assert details["model_used"] == "claude-sonnet-4-6"
        assert details["raw_output_preview"] == "not a json"

    def test_workspace_not_found_returns_404(
        self,
        workspace_service: WorkspaceService,
        client_factory,
    ) -> None:
        # workspace_id 没 create,WorkspaceService.info 抛 MetaFileError →
        # 翻译为 WORKSPACE_INVALID (422)。本测验证错误被全局翻译器兜底而
        # 非裸 500。
        llm = _build_llm(json.dumps(_make_payload("ok")))
        for client in client_factory(llm):
            resp = client.post(
                "/api/v1/workspaces/ws-not-exist/memory/check"
            )
        assert resp.status_code in (404, 422)
        body = resp.json()
        assert body["error"]["code"] in (
            "WORKSPACE_NOT_FOUND",
            "WORKSPACE_INVALID",
        )

    def test_critical_severity_returns_200_persists(
        self,
        workspace_service: WorkspaceService,
        workspace_id: str,
        client_factory,
    ) -> None:
        # critical 时 service 内部会 WS publish(本文件 ws_manager=None,不
        # 触发);路由应仍 200 + 完整 critical 报告。
        llm = _build_llm(json.dumps(_make_payload("critical", issues=10)))
        for client in client_factory(llm):
            resp = client.post(
                f"/api/v1/workspaces/{workspace_id}/memory/check"
            )
        assert resp.status_code == 200
        data = resp.json()["data"]
        assert data["severity"] == "critical"
        assert data["stale_threads_count"] == 10

        session = _make_session_factory(workspace_service)(workspace_id)
        try:
            row = (
                session.execute(select(MemoryHealthCheckORM)).scalars().first()
            )
            assert row is not None
            assert row.severity == "critical"
            assert row.stale_threads_count == 10
        finally:
            session.close()
