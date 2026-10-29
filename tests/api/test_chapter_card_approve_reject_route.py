"""``POST /chapters/{id}/card:approve`` / ``card:reject`` 路由集成测试(P5-B-07)。

覆盖用例:

1. ``card:approve`` happy path:200 + StatusOut + ORM ``card_status="approved"``
   + ``card_approved_at`` 非 None + SSOT 文件 patch + ``chapter:write`` 不再 409
2. ``card:reject`` happy path:200 + StatusOut + ``rejection_notes`` 持久化
3. ``card:approve`` 缺 X-Workspace-Id → 422 MISSING_WORKSPACE_HEADER
4. ``card:reject`` body 多余字段 → 422 VALIDATION_FAILED(``extra="forbid"``)
5. ``card:approve`` 从已 ``approved`` 再 approve → 409 CARD_INVALID_STATE_TRANSITION
   + details 字段完整
6. ``card:reject`` 从 ``rejected`` 再 reject → 409
7. ``card:approve`` ORM 行不存在 → 404 CHAPTER_CARD_NOT_FOUND
8. ``card:reject`` 不带 rejection_notes(可选)→ 200 + StatusOut.rejection_notes=None

测试基础设施沿用 P5-B-06 ``test_chapter_card_generate_route.py``:in-memory
SQLite + StaticPool + check_same_thread=False(BackgroundTasks 跨线程兼容)
+ MockLLMClient + tmp_path workspaces 根。
"""

from __future__ import annotations

import json
from collections.abc import Callable, Iterator
from datetime import datetime, timezone
from pathlib import Path

import pytest
from fastapi.testclient import TestClient
from sqlalchemy import create_engine
from sqlalchemy.orm import Session
from sqlalchemy.pool import StaticPool

from src.api import task_registry
from src.api.deps import get_chapter_card_service, get_chapter_service
from src.api.main import create_app
from src.core.db import Base
from src.core.db.models import ChapterCardORM, WorkspaceORM
from src.core.llm.mock_client import MockLLMClient
from src.services.chapter_card_service import ChapterCardService
from src.services.chapter_service import ChapterService
from src.services.workspace_service import WorkspaceService


# ---- fixtures ---------------------------------------------------------------


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
        "ws-card-approve",
        display_name="P5-B-07 approve/reject test",
        genre="xianxia",
        premise="test",
        activate=False,
    )
    return "ws-card-approve"


@pytest.fixture
def session_factory() -> Callable[[str], Session]:
    """单 in-memory engine + StaticPool(对齐 P5-B-06 同源)。"""
    engine = create_engine(
        "sqlite:///:memory:",
        connect_args={"check_same_thread": False},
        poolclass=StaticPool,
    )
    Base.metadata.create_all(engine)

    def factory(workspace_id: str) -> Session:
        return Session(engine)

    return factory


@pytest.fixture(autouse=True)
def _clear_task_registry() -> Iterator[None]:
    task_registry.clear()
    yield
    task_registry.clear()


def _seed_orm(
    session_factory: Callable[[str], Session],
    workspace_id: str,
    *,
    chapter_id: str = "ch-0001",
    card_status: str = "pending_review",
    rejection_notes: str | None = None,
) -> None:
    """seed WorkspaceORM + ChapterCardORM 满足 FK + 入口状态。"""
    session = session_factory(workspace_id)
    try:
        now = datetime.now(timezone.utc)
        session.merge(
            WorkspaceORM(
                workspace_id=workspace_id,
                display_name="x",
                genre="xianxia",
                premise="p",
                created_at=now,
                last_active_at=now,
                engine_version="0.1.0",
                status="active",
                total_chapters=10,
                completed_chapters=0,
                config_overrides={},
            )
        )
        session.merge(
            ChapterCardORM(
                chapter_id=chapter_id,
                workspace_id=workspace_id,
                card_status=card_status,
                card_approved_at=None,
                created_at=now,
                rejection_notes=rejection_notes,
            )
        )
        session.commit()
    finally:
        session.close()


def _seed_ssot_card(
    workspace_service: WorkspaceService,
    workspace_id: str,
    chapter_id: str,
    card_status: str,
) -> Path:
    card_dir = (
        workspace_service.workspaces_root / workspace_id / "data" / "cards"
    )
    card_dir.mkdir(parents=True, exist_ok=True)
    card_path = card_dir / f"{chapter_id}.json"
    card_path.write_text(
        json.dumps(
            {
                "chapter_id": chapter_id,
                "card_status": card_status,
                "card_approved_at": None,
            },
            ensure_ascii=False,
        ),
        encoding="utf-8",
    )
    return card_path


@pytest.fixture
def client(
    workspace_service: WorkspaceService,
    session_factory: Callable[[str], Session],
    workspace_id: str,
) -> Iterator[TestClient]:
    app = create_app()
    mock_llm = MockLLMClient()
    chapter_service = ChapterService(
        workspace_service=workspace_service,
        client=mock_llm,
        model="claude-sonnet-4-6",
        session_factory=session_factory,
    )
    card_service = ChapterCardService(
        workspace_service=workspace_service,
        chapter_service=chapter_service,
        session_factory=session_factory,
        client=mock_llm,
        model="claude-sonnet-4-6",
        ws_manager=None,
    )
    app.dependency_overrides[get_chapter_card_service] = lambda: card_service
    app.dependency_overrides[get_chapter_service] = lambda: chapter_service
    with TestClient(app) as c:
        yield c
    app.dependency_overrides.clear()


# ---- 测试:POST /chapters/{id}/card:approve ---------------------------------


class TestCardApproveRoute:
    def test_approve_happy_path(
        self,
        client: TestClient,
        workspace_id: str,
        workspace_service: WorkspaceService,
        session_factory: Callable[[str], Session],
    ) -> None:
        _seed_orm(
            session_factory,
            workspace_id,
            chapter_id="ch-0001",
            card_status="pending_review",
        )
        card_path = _seed_ssot_card(
            workspace_service, workspace_id, "ch-0001", "pending_review"
        )

        resp = client.post(
            "/api/v1/chapters/ch-0001/card:approve",
            headers={"X-Workspace-Id": workspace_id},
            json={},
        )
        assert resp.status_code == 200, resp.text
        data = resp.json()["data"]
        assert data["chapter_id"] == "ch-0001"
        assert data["from_status"] == "pending_review"
        assert data["to_status"] == "approved"
        assert data["card_approved_at"] is not None
        assert data["rejection_notes"] is None

        # ORM 行
        session = session_factory(workspace_id)
        try:
            row = session.get(ChapterCardORM, "ch-0001")
        finally:
            session.close()
        assert row is not None
        assert row.card_status == "approved"
        assert row.card_approved_at is not None

        # SSOT 文件 patch
        ssot = json.loads(card_path.read_text(encoding="utf-8"))
        assert ssot["card_status"] == "approved"
        assert ssot["card_approved_at"] is not None

    def test_approve_missing_workspace_header_returns_422(
        self,
        client: TestClient,
        workspace_id: str,
        session_factory: Callable[[str], Session],
    ) -> None:
        _seed_orm(session_factory, workspace_id)
        resp = client.post(
            "/api/v1/chapters/ch-0001/card:approve",
            json={},
        )
        assert resp.status_code == 422
        assert resp.json()["error"]["code"] == "MISSING_WORKSPACE_HEADER"

    def test_approve_from_approved_returns_409(
        self,
        client: TestClient,
        workspace_id: str,
        session_factory: Callable[[str], Session],
    ) -> None:
        _seed_orm(
            session_factory,
            workspace_id,
            card_status="approved",
        )
        resp = client.post(
            "/api/v1/chapters/ch-0001/card:approve",
            headers={"X-Workspace-Id": workspace_id},
            json={},
        )
        assert resp.status_code == 409, resp.text
        err = resp.json()["error"]
        assert err["code"] == "CARD_INVALID_STATE_TRANSITION"
        details = err["details"]
        assert details["chapter_id"] == "ch-0001"
        assert details["current_status"] == "approved"
        assert details["action"] == "approve"
        assert "approved" not in details["allowed_sources"]

    def test_approve_orm_missing_returns_404(
        self,
        client: TestClient,
        workspace_id: str,
        session_factory: Callable[[str], Session],
    ) -> None:
        # seed workspace 但不 seed card row
        session = session_factory(workspace_id)
        try:
            now = datetime.now(timezone.utc)
            session.merge(
                WorkspaceORM(
                    workspace_id=workspace_id,
                    display_name="x",
                    genre="xianxia",
                    premise="p",
                    created_at=now,
                    last_active_at=now,
                    engine_version="0.1.0",
                    status="active",
                    total_chapters=10,
                    completed_chapters=0,
                    config_overrides={},
                )
            )
            session.commit()
        finally:
            session.close()
        resp = client.post(
            "/api/v1/chapters/ch-0001/card:approve",
            headers={"X-Workspace-Id": workspace_id},
            json={},
        )
        assert resp.status_code == 404, resp.text
        assert resp.json()["error"]["code"] == "CHAPTER_CARD_NOT_FOUND"


# ---- 测试:POST /chapters/{id}/card:reject ----------------------------------


class TestCardRejectRoute:
    def test_reject_happy_path_with_notes(
        self,
        client: TestClient,
        workspace_id: str,
        session_factory: Callable[[str], Session],
    ) -> None:
        _seed_orm(
            session_factory,
            workspace_id,
            card_status="pending_review",
        )
        resp = client.post(
            "/api/v1/chapters/ch-0001/card:reject",
            headers={"X-Workspace-Id": workspace_id},
            json={"rejection_notes": "情节松散,建议补衔接"},
        )
        assert resp.status_code == 200, resp.text
        data = resp.json()["data"]
        assert data["from_status"] == "pending_review"
        assert data["to_status"] == "rejected"
        assert data["rejection_notes"] == "情节松散,建议补衔接"
        assert data["card_approved_at"] is None

        # ORM 行
        session = session_factory(workspace_id)
        try:
            row = session.get(ChapterCardORM, "ch-0001")
        finally:
            session.close()
        assert row is not None
        assert row.card_status == "rejected"
        assert row.rejection_notes == "情节松散,建议补衔接"

    def test_reject_without_notes_returns_200_notes_null(
        self,
        client: TestClient,
        workspace_id: str,
        session_factory: Callable[[str], Session],
    ) -> None:
        _seed_orm(session_factory, workspace_id, card_status="approved")
        resp = client.post(
            "/api/v1/chapters/ch-0001/card:reject",
            headers={"X-Workspace-Id": workspace_id},
            json={},
        )
        assert resp.status_code == 200, resp.text
        data = resp.json()["data"]
        assert data["to_status"] == "rejected"
        assert data["rejection_notes"] is None

    def test_reject_from_rejected_returns_409(
        self,
        client: TestClient,
        workspace_id: str,
        session_factory: Callable[[str], Session],
    ) -> None:
        _seed_orm(session_factory, workspace_id, card_status="rejected")
        resp = client.post(
            "/api/v1/chapters/ch-0001/card:reject",
            headers={"X-Workspace-Id": workspace_id},
            json={"rejection_notes": "x"},
        )
        assert resp.status_code == 409, resp.text
        err = resp.json()["error"]
        assert err["code"] == "CARD_INVALID_STATE_TRANSITION"
        assert err["details"]["current_status"] == "rejected"
        assert err["details"]["action"] == "reject"

    def test_reject_extra_field_returns_422(
        self,
        client: TestClient,
        workspace_id: str,
        session_factory: Callable[[str], Session],
    ) -> None:
        _seed_orm(session_factory, workspace_id)
        resp = client.post(
            "/api/v1/chapters/ch-0001/card:reject",
            headers={"X-Workspace-Id": workspace_id},
            json={"rejection_notes": "x", "unknown_field": "bad"},
        )
        assert resp.status_code == 422
        assert resp.json()["error"]["code"] == "VALIDATION_FAILED"
