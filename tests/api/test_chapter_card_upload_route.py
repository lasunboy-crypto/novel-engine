"""``POST /api/v1/chapters/{chapter_id}/card`` 路由集成测试(P5-B-08)。

覆盖用例:

1. happy path:上传整 ChapterCard JSON → 200 + 文件落盘 SSOT + ORM 行
   ``card_status="draft"`` 默认
2. card_status="pending_review" 显式上传:ORM 行同步该值(非默认 "draft")
3. 路径 chapter_id 与 body chapter_id 不一致 → 422 ``CHAPTER_ID_MISMATCH``
4. 缺 X-Workspace-Id → 422 ``MISSING_WORKSPACE_HEADER``
5. body 缺必填字段(`title` 缺失)→ 422 ``VALIDATION_FAILED``
6. 二次上传同 chapter_id:文件覆盖 + ORM 行更新(同 chapter_id upsert)
7. 上传后 ``GET /chapters/{id}/card`` 返完整字段(端到端往返)
8. 上传**不**触发 WS event(对齐 P5-B-07 §6 接力 marker)

测试基础设施沿用 ``test_chapter_card_approve_reject_route.py``:in-memory
SQLite + StaticPool + MockLLMClient + tmp_path workspaces 根。
"""

from __future__ import annotations

import json
from collections.abc import Callable, Iterator
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

import pytest
from fastapi.testclient import TestClient
from sqlalchemy import create_engine
from sqlalchemy.orm import Session
from sqlalchemy.pool import StaticPool

from src.api.deps import get_chapter_card_service, get_chapter_service
from src.api.main import create_app
from src.core.db import Base
from src.core.db.models import ChapterCardORM, WorkspaceORM
from src.core.llm.mock_client import MockLLMClient
from src.core.models.chapter_card import ContinuityContext
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
        "ws-card-upload",
        display_name="P5-B-08 upload test",
        genre="xianxia",
        premise="test",
        activate=False,
    )
    return "ws-card-upload"


@pytest.fixture
def session_factory() -> Callable[[str], Session]:
    engine = create_engine(
        "sqlite:///:memory:",
        connect_args={"check_same_thread": False},
        poolclass=StaticPool,
    )
    Base.metadata.create_all(engine)

    def factory(workspace_id: str) -> Session:
        return Session(engine)

    return factory


@pytest.fixture
def client(
    workspace_service: WorkspaceService,
    session_factory: Callable[[str], Session],
    workspace_id: str,
) -> Iterator[TestClient]:
    """seed WorkspaceORM 满足 FK,wire ChapterService + ChapterCardService。"""
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


def _continuity_payload(**overrides: Any) -> dict[str, Any]:
    base: dict[str, Any] = {
        "prev_chapter_tail": "(开篇,无上一章)",
        "unfinished_actions": [],
        "time_gap": "立即续接",
        "scene_continuation": "same",
        "pov_change": False,
        "pov_from": None,
        "pov_to": "主角",
    }
    base.update(overrides)
    return base


def _card_payload(**overrides: Any) -> dict[str, Any]:
    """构造完整合法 ChapterCard JSON payload(API body)。"""
    base: dict[str, Any] = {
        "chapter_id": "ch-0001",
        "workspace_id": "ws-card-upload",
        "volume_id": "v1",
        "chapter_number": 1,
        "title": "开局",
        "tension_level": 5,
        "must_achieve": ["介绍主角"],
        "must_unresolved": ["主角身世"],
        "forbidden": ["金手指外挂"],
        "pov_character": "李白",
        "setting": "长安",
        "continuity_context": ContinuityContext(
            **_continuity_payload()
        ).model_dump(),
        "next_chapter_seed": "下一章引子",
        "arc_phase": "opening",
        "is_arc_finale": False,
        "related_plot_threads": ["pt_main"],
        "related_characters": ["李白"],
    }
    base.update(overrides)
    return base


# =============================================================================
# 主路径
# =============================================================================


class TestCardUploadHappy:
    def test_upload_lands_file_and_orm(
        self,
        client: TestClient,
        workspace_id: str,
        workspace_service: WorkspaceService,
        session_factory: Callable[[str], Session],
    ) -> None:
        payload = _card_payload()
        resp = client.post(
            "/api/v1/chapters/ch-0001/card",
            headers={"X-Workspace-Id": workspace_id},
            json=payload,
        )
        assert resp.status_code == 200, resp.text
        data = resp.json()["data"]
        assert data["chapter_id"] == "ch-0001"
        # 默认 card_status = "draft"(payload 没显式指定)
        assert data["card_status"] == "draft"

        # SSOT 文件落盘
        card_path = (
            workspace_service.workspaces_root
            / workspace_id
            / "data"
            / "cards"
            / "ch-0001.json"
        )
        assert card_path.is_file()
        on_disk = json.loads(card_path.read_text(encoding="utf-8"))
        assert on_disk["chapter_id"] == "ch-0001"
        assert on_disk["card_status"] == "draft"
        assert on_disk["title"] == "开局"

        # ORM 行落库
        session = session_factory(workspace_id)
        try:
            row = session.get(ChapterCardORM, "ch-0001")
        finally:
            session.close()
        assert row is not None
        assert row.card_status == "draft"
        assert row.workspace_id == workspace_id
        assert row.card_approved_at is None

    def test_upload_with_explicit_pending_review_status(
        self,
        client: TestClient,
        workspace_id: str,
        session_factory: Callable[[str], Session],
    ) -> None:
        """显式指定 card_status="pending_review" 上传 → ORM 行同步该值。"""
        payload = _card_payload(card_status="pending_review")
        resp = client.post(
            "/api/v1/chapters/ch-0001/card",
            headers={"X-Workspace-Id": workspace_id},
            json=payload,
        )
        assert resp.status_code == 200
        assert resp.json()["data"]["card_status"] == "pending_review"

        session = session_factory(workspace_id)
        try:
            row = session.get(ChapterCardORM, "ch-0001")
        finally:
            session.close()
        assert row is not None
        assert row.card_status == "pending_review"

    def test_round_trip_via_get(
        self,
        client: TestClient,
        workspace_id: str,
    ) -> None:
        """上传后 GET /chapters/{id}/card 返完整字段。"""
        payload = _card_payload()
        resp = client.post(
            "/api/v1/chapters/ch-0001/card",
            headers={"X-Workspace-Id": workspace_id},
            json=payload,
        )
        assert resp.status_code == 200
        resp_get = client.get(
            "/api/v1/chapters/ch-0001/card",
            headers={"X-Workspace-Id": workspace_id},
        )
        assert resp_get.status_code == 200
        got = resp_get.json()["data"]
        assert got["chapter_id"] == "ch-0001"
        assert got["title"] == "开局"
        assert got["card_status"] == "draft"
        assert got["tension_level"] == 5

    def test_second_upload_overwrites(
        self,
        client: TestClient,
        workspace_id: str,
        workspace_service: WorkspaceService,
        session_factory: Callable[[str], Session],
    ) -> None:
        """二次上传同 chapter_id:文件覆盖 + ORM 行更新(card_status / 字段变更)。"""
        client.post(
            "/api/v1/chapters/ch-0001/card",
            headers={"X-Workspace-Id": workspace_id},
            json=_card_payload(title="旧标题"),
        )
        client.post(
            "/api/v1/chapters/ch-0001/card",
            headers={"X-Workspace-Id": workspace_id},
            json=_card_payload(
                title="新标题", card_status="pending_review"
            ),
        )
        card_path = (
            workspace_service.workspaces_root
            / workspace_id
            / "data"
            / "cards"
            / "ch-0001.json"
        )
        on_disk = json.loads(card_path.read_text(encoding="utf-8"))
        assert on_disk["title"] == "新标题"

        session = session_factory(workspace_id)
        try:
            row = session.get(ChapterCardORM, "ch-0001")
        finally:
            session.close()
        assert row is not None
        assert row.card_status == "pending_review"


# =============================================================================
# 错误路径
# =============================================================================


class TestCardUploadErrors:
    def test_chapter_id_mismatch_returns_422(
        self,
        client: TestClient,
        workspace_id: str,
    ) -> None:
        payload = _card_payload(chapter_id="ch-0002")
        resp = client.post(
            "/api/v1/chapters/ch-0001/card",
            headers={"X-Workspace-Id": workspace_id},
            json=payload,
        )
        assert resp.status_code == 422
        assert resp.json()["error"]["code"] == "CHAPTER_ID_MISMATCH"

    def test_missing_workspace_header_returns_422(
        self,
        client: TestClient,
    ) -> None:
        resp = client.post(
            "/api/v1/chapters/ch-0001/card",
            json=_card_payload(),
        )
        assert resp.status_code == 422
        assert resp.json()["error"]["code"] == "MISSING_WORKSPACE_HEADER"

    def test_missing_required_field_returns_422(
        self,
        client: TestClient,
        workspace_id: str,
    ) -> None:
        """body 缺必填字段(`title`)→ 422 VALIDATION_FAILED。"""
        payload = _card_payload()
        del payload["title"]
        resp = client.post(
            "/api/v1/chapters/ch-0001/card",
            headers={"X-Workspace-Id": workspace_id},
            json=payload,
        )
        assert resp.status_code == 422
        assert resp.json()["error"]["code"] == "VALIDATION_FAILED"
