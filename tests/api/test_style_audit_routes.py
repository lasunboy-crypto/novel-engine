"""style_audit 路由集成测试(P4-B-10;v1.1 w4-s5 / I2.2 StyleAuditor 落地后更新)。

覆盖 ``POST /api/v1/workspaces/{workspace_id}/style/audit``:

- 成功路径(无请求体,工作区未选模板)→ 200 + StyleAuditor 短路报告
  (severity='ok' / drift_score=0.0 / report_json.summary='工作区未选写作风格模板...')
- 成功路径(带 chapter_range_start + _end)→ 200 + 字段透传 + 落表
- 工作区不存在 → 404 / 422(由 WorkspaceServiceError / MetaFileError 翻译)
- chapter_range_start 不存在 → 404 CHAPTER_NOT_FOUND
- 空请求体 / 缺字段也合法(两字段都 Optional)

注:测试工作区均未选 style_template → StyleAuditor 短路返 ok(不调 LLM)。
"""

from __future__ import annotations

from collections.abc import Iterator
from datetime import UTC, datetime
from pathlib import Path

import pytest
from fastapi.testclient import TestClient
from sqlalchemy import create_engine, select
from sqlalchemy.orm import Session

from src.api.deps import get_style_audit_service
from src.api.main import create_app
from src.core.db import Base
from src.core.db.models import ChapterORM, StyleAuditEntryORM, WorkspaceORM
from src.core.workspace.workspace_paths import WorkspacePaths
from src.services.style_audit_service import StyleAuditService
from src.services.workspace_service import WorkspaceService


@pytest.fixture
def workspace_service(tmp_path: Path) -> WorkspaceService:
    workspaces_root = tmp_path / "workspaces"
    archives_root = tmp_path / "archives"
    workspaces_root.mkdir()
    archives_root.mkdir()
    return WorkspaceService(
        workspaces_root=workspaces_root, archives_root=archives_root
    )


def _make_session_factory(workspace_service: WorkspaceService):
    def _factory(workspace_id: str) -> Session:
        paths = WorkspacePaths(
            workspace_id, workspaces_root=workspace_service.workspaces_root
        )
        engine = create_engine(f"sqlite:///{paths.db_path}")
        Base.metadata.create_all(engine)
        return Session(engine)

    return _factory


@pytest.fixture
def workspace_id(workspace_service: WorkspaceService) -> str:
    workspace_service.create(
        "ws-styaud",
        display_name="P4-B-10 style audit 路由测试",
        genre="test",
        premise="x",
        activate=False,
    )
    return "ws-styaud"


def _seed_chapter(
    workspace_service: WorkspaceService, workspace_id: str, chapter_id: str
) -> None:
    factory = _make_session_factory(workspace_service)
    session = factory(workspace_id)
    try:
        if (
            session.execute(
                select(WorkspaceORM).where(
                    WorkspaceORM.workspace_id == workspace_id
                )
            ).scalars().first()
            is None
        ):
            session.add(
                WorkspaceORM(
                    workspace_id=workspace_id,
                    display_name="seed",
                    genre="test",
                    premise="x",
                    created_at=datetime.now(UTC),
                    last_active_at=datetime.now(UTC),
                    engine_version="0.1",
                    status="active",
                )
            )
        session.add(
            ChapterORM(
                chapter_id=chapter_id,
                workspace_id=workspace_id,
                current_version=1,
                status="drafting",
                created_at=datetime.now(UTC),
            )
        )
        session.commit()
    finally:
        session.close()


@pytest.fixture
def client_factory(workspace_service: WorkspaceService):
    """工厂返回 TestClient,override style_audit service 指向 tmp_path。"""

    def _factory() -> Iterator[TestClient]:
        app = create_app()
        service = StyleAuditService(
            workspace_service=workspace_service,
            session_factory=_make_session_factory(workspace_service),
        )
        app.dependency_overrides[get_style_audit_service] = lambda: service
        with TestClient(app) as c:
            yield c
        app.dependency_overrides.clear()

    return _factory


class TestStyleAuditRoute:
    def test_success_returns_ok_report_no_template(
        self,
        workspace_service: WorkspaceService,
        workspace_id: str,
        client_factory,
    ) -> None:
        # 工作区未选模板 → StyleAuditor 短路返 ok / drift=0(不调 LLM)。
        for client in client_factory():
            resp = client.post(
                f"/api/v1/workspaces/{workspace_id}/style/audit",
                json={},
            )
        assert resp.status_code == 200
        body = resp.json()
        data = body["data"]
        assert data["severity"] == "ok"
        assert data["drift_score"] == 0.0
        assert data["audit_id"].startswith("sty-")
        assert "未选写作风格模板" in data["report_json"]["summary"]
        # 行落表
        session = _make_session_factory(workspace_service)(workspace_id)
        try:
            rows = session.execute(select(StyleAuditEntryORM)).scalars().all()
            assert len(rows) == 1
            assert rows[0].severity == "ok"
        finally:
            session.close()

    def test_success_with_chapter_range(
        self,
        workspace_service: WorkspaceService,
        workspace_id: str,
        client_factory,
    ) -> None:
        _seed_chapter(workspace_service, workspace_id, "ch-0001")
        _seed_chapter(workspace_service, workspace_id, "ch-0002")
        for client in client_factory():
            resp = client.post(
                f"/api/v1/workspaces/{workspace_id}/style/audit",
                json={
                    "chapter_range_start": "ch-0001",
                    "chapter_range_end": "ch-0002",
                },
            )
        assert resp.status_code == 200
        data = resp.json()["data"]
        assert data["chapter_range_start"] == "ch-0001"
        assert data["chapter_range_end"] == "ch-0002"

    def test_workspace_not_found_returns_404_or_422(
        self,
        client_factory,
    ) -> None:
        for client in client_factory():
            resp = client.post(
                "/api/v1/workspaces/ws-not-exist/style/audit",
                json={},
            )
        assert resp.status_code in (404, 422)
        body = resp.json()
        assert body["error"]["code"] in (
            "WORKSPACE_NOT_FOUND",
            "WORKSPACE_INVALID",
        )

    def test_chapter_not_found_returns_404(
        self,
        workspace_service: WorkspaceService,
        workspace_id: str,
        client_factory,
    ) -> None:
        _seed_chapter(workspace_service, workspace_id, "ch-0001")
        for client in client_factory():
            resp = client.post(
                f"/api/v1/workspaces/{workspace_id}/style/audit",
                json={
                    "chapter_range_start": "ch-9999",
                    "chapter_range_end": "ch-0001",
                },
            )
        assert resp.status_code == 404
        body = resp.json()
        assert body["error"]["code"] == "CHAPTER_NOT_FOUND"

    def test_empty_request_body_is_allowed(
        self,
        workspace_service: WorkspaceService,
        workspace_id: str,
        client_factory,
    ) -> None:
        # 不传 body → payload 默认 None → 路由内 fallback 空 StyleAuditRequest()
        for client in client_factory():
            resp = client.post(
                f"/api/v1/workspaces/{workspace_id}/style/audit"
            )
        assert resp.status_code == 200
        assert resp.json()["data"]["severity"] == "ok"
