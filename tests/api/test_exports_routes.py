"""``POST /api/v1/workspaces/{id}/exports`` + ``GET /exports`` + ``GET
/exports/{id}/download`` 路由集成测试(P6-B-04)。

覆盖维度(对齐 plan §10 测试约定):

- **happy**:POST → 202 + task_id;BackgroundTasks 同步跑完 → GET 列表 + GET
  download 返 zip
- **error**:3 错误码各 1 测(``EXPORT_FORMAT_UNSUPPORTED 422`` /
  ``EXPORT_NOT_FOUND 404`` / ``EXPORT_SCOPE_INVALID 422``)+ Pydantic 422
- **gate**:scope=range 含 invalidated → 422
- **WS event**:happy path 断言 4 stage publish 到 ``task.{task_id}.progress``
"""

from __future__ import annotations

import io
import json
import zipfile
from collections.abc import Callable, Iterator
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

import pytest
from fastapi.testclient import TestClient
from sqlalchemy import create_engine
from sqlalchemy.orm import Session
from sqlalchemy.pool import StaticPool

from src.api import task_registry
from src.api.deps import get_export_service, get_workspace_service
from src.api.main import create_app
from src.core.db import Base
from src.core.db.models import ChapterORM, VolumeORM, WorkspaceORM
from src.services.export_service import ExportService
from src.services.workspace_service import WorkspaceService


class _FakeWSManager:
    def __init__(self) -> None:
        self.events: list[dict[str, Any]] = []

    def publish(self, *, channel: str, event: str, data: dict[str, Any]) -> None:
        self.events.append({"channel": channel, "event": event, "data": data})


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
        "ws-exp-rt",
        display_name="P6-B-04 export route test",
        genre="xianxia",
        premise="一句话设定",
        activate=False,
    )
    return "ws-exp-rt"


@pytest.fixture
def session_factory() -> Callable[[str], Session]:
    engine = create_engine(
        "sqlite:///:memory:",
        connect_args={"check_same_thread": False},
        poolclass=StaticPool,
    )
    Base.metadata.create_all(engine)

    def factory(_workspace_id: str) -> Session:
        return Session(engine)

    return factory


def _seed_db(
    session_factory: Callable[[str], Session],
    workspace_id: str,
    *,
    chapter_count: int = 2,
    invalidated_ids: list[str] | None = None,
) -> None:
    session = session_factory(workspace_id)
    try:
        now = datetime.now(timezone.utc)
        session.merge(
            WorkspaceORM(
                workspace_id=workspace_id,
                display_name="x",
                genre="x",
                premise="p",
                created_at=now,
                last_active_at=now,
                engine_version="0.1.0",
                status="active",
                total_chapters=chapter_count,
                completed_chapters=0,
                config_overrides={},
            )
        )
        session.merge(
            VolumeORM(
                volume_id="vol-1",
                workspace_id=workspace_id,
                title="V1",
                summary="s",
                chapter_count=chapter_count,
                tension_template="heros_journey",
                arcs_json=[],
                tension_curve_json=[],
                created_at=now,
                last_updated_at=now,
            )
        )
        for i in range(1, chapter_count + 1):
            cid = f"ch-{i:04d}"
            status_v = (
                "invalidated"
                if invalidated_ids and cid in invalidated_ids
                else "approved"
            )
            session.merge(
                ChapterORM(
                    chapter_id=cid,
                    workspace_id=workspace_id,
                    current_version=1,
                    status=status_v,
                    rewrite_count=0,
                    revise_count=0,
                    created_at=now,
                    approved_at=now if status_v == "approved" else None,
                    total_token_usage={},
                    total_cost_usd=0.0,
                )
            )
        session.commit()
    finally:
        session.close()


def _write_final(
    workspace_service: WorkspaceService,
    workspace_id: str,
    chapter_id: str,
    content: str,
) -> None:
    from src.core.workspace.workspace_paths import WorkspacePaths

    paths = WorkspacePaths(
        workspace_id, workspaces_root=workspace_service.workspaces_root
    )
    paths.chapter_dir(chapter_id).mkdir(parents=True, exist_ok=True)
    (paths.chapter_dir(chapter_id) / "final.md").write_text(
        content, encoding="utf-8"
    )


def _write_card(
    workspace_service: WorkspaceService,
    workspace_id: str,
    chapter_id: str,
    *,
    title: str,
    chapter_number: int,
) -> None:
    from src.core.workspace.workspace_paths import WorkspacePaths

    paths = WorkspacePaths(
        workspace_id, workspaces_root=workspace_service.workspaces_root
    )
    paths.cards_dir.mkdir(parents=True, exist_ok=True)
    (paths.cards_dir / f"{chapter_id}.json").write_text(
        json.dumps(
            {
                "chapter_id": chapter_id,
                "title": title,
                "chapter_number": chapter_number,
            },
            ensure_ascii=False,
        ),
        encoding="utf-8",
    )


@pytest.fixture
def fake_ws_manager() -> _FakeWSManager:
    return _FakeWSManager()


@pytest.fixture
def export_service(
    workspace_service: WorkspaceService,
    session_factory: Callable[[str], Session],
    fake_ws_manager: _FakeWSManager,
) -> ExportService:
    return ExportService(
        workspace_service=workspace_service,
        session_factory=session_factory,
        ws_manager=fake_ws_manager,  # type: ignore[arg-type]
        engine_version="0.1.0",
    )


@pytest.fixture
def client(
    export_service: ExportService,
    workspace_service: WorkspaceService,
) -> Iterator[TestClient]:
    app = create_app()
    app.dependency_overrides[get_export_service] = lambda: export_service
    app.dependency_overrides[get_workspace_service] = lambda: workspace_service
    with TestClient(app) as c:
        yield c
    app.dependency_overrides.clear()


@pytest.fixture(autouse=True)
def clear_registry() -> Iterator[None]:
    task_registry.clear()
    yield
    task_registry.clear()


# ---- happy path -------------------------------------------------------------


class TestCreateHappy:
    def test_post_md_full_returns_202_and_runs_background(
        self,
        client: TestClient,
        workspace_id: str,
        workspace_service: WorkspaceService,
        session_factory: Callable[[str], Session],
        fake_ws_manager: _FakeWSManager,
    ) -> None:
        _seed_db(session_factory, workspace_id, chapter_count=2)
        _write_final(workspace_service, workspace_id, "ch-0001", "正文 A")
        _write_final(workspace_service, workspace_id, "ch-0002", "正文 B")
        _write_card(
            workspace_service,
            workspace_id,
            "ch-0001",
            title="启程",
            chapter_number=1,
        )
        _write_card(
            workspace_service,
            workspace_id,
            "ch-0002",
            title="进阶",
            chapter_number=2,
        )

        resp = client.post(
            f"/api/v1/workspaces/{workspace_id}/exports",
            json={
                "format": "md",
                "scope": "full",
                "include_metadata": True,
                "include_review_history": False,
            },
        )
        assert resp.status_code == 202, resp.text
        data = resp.json()["data"]
        assert data["task_id"].startswith("task_")
        assert data["export_id"].startswith("exp_")
        assert data["status"] == "pending"
        assert data["format"] == "md"

        export_id = data["export_id"]
        # BackgroundTasks 在 TestClient with 块退出前同步跑完
        # 验列表
        resp_list = client.get(f"/api/v1/workspaces/{workspace_id}/exports")
        assert resp_list.status_code == 200
        items = resp_list.json()["data"]["items"]
        assert len(items) == 1
        assert items[0]["export_id"] == export_id
        assert items[0]["status"] == "done"
        assert items[0]["chapter_count"] == 2

        # 下载 zip
        resp_dl = client.get(
            f"/api/v1/workspaces/{workspace_id}/exports/{export_id}/download"
        )
        assert resp_dl.status_code == 200
        assert resp_dl.headers["content-type"] == "application/zip"
        with zipfile.ZipFile(io.BytesIO(resp_dl.content)) as zf:
            names = zf.namelist()
            assert "book.md" in names
            assert "meta.json" in names
            book = zf.read("book.md").decode("utf-8")
            assert "正文 A" in book
            assert "正文 B" in book

        # WS event:4 stage
        stages = [e["data"]["stage"] for e in fake_ws_manager.events]
        assert stages == ["collecting", "rendering", "packaging", "done"]

    def test_post_chaptered_md_volume_scope(
        self,
        client: TestClient,
        workspace_id: str,
        workspace_service: WorkspaceService,
        session_factory: Callable[[str], Session],
    ) -> None:
        _seed_db(session_factory, workspace_id, chapter_count=2)
        _write_final(workspace_service, workspace_id, "ch-0001", "正文 A")
        _write_final(workspace_service, workspace_id, "ch-0002", "正文 B")
        _write_card(
            workspace_service,
            workspace_id,
            "ch-0001",
            title="一",
            chapter_number=1,
        )
        _write_card(
            workspace_service,
            workspace_id,
            "ch-0002",
            title="二",
            chapter_number=2,
        )
        resp = client.post(
            f"/api/v1/workspaces/{workspace_id}/exports",
            json={
                "format": "chaptered_md",
                "scope": "volume",
                "volume_id": "vol-1",
            },
        )
        assert resp.status_code == 202
        export_id = resp.json()["data"]["export_id"]
        resp_dl = client.get(
            f"/api/v1/workspaces/{workspace_id}/exports/{export_id}/download"
        )
        assert resp_dl.status_code == 200
        with zipfile.ZipFile(io.BytesIO(resp_dl.content)) as zf:
            names = zf.namelist()
            assert "index.md" in names
            assert "volume-1/chapter-0001.md" in names
            assert "volume-1/chapter-0002.md" in names


# ---- error codes ------------------------------------------------------------


class TestErrors:
    def test_unknown_format_returns_422_pydantic_validation(
        self,
        client: TestClient,
        workspace_id: str,
        session_factory: Callable[[str], Session],
    ) -> None:
        """P6-B-06 起 4 档 format 全部支持;路由层 ``ExportFormat`` Literal
        对未知 format(如 ``"pdf"``)返 Pydantic 422 ``VALIDATION_FAILED``,
        而非 ``EXPORT_FORMAT_UNSUPPORTED``(service 层 unsupported 路径已
        无法从路由层触达,只对 service 层直接调用兜底,见
        ``test_export_service.py:test_unknown_format_returns_unsupported``)。"""
        _seed_db(session_factory, workspace_id)
        resp = client.post(
            f"/api/v1/workspaces/{workspace_id}/exports",
            json={"format": "pdf", "scope": "full"},
        )
        assert resp.status_code == 422
        err = resp.json()["error"]
        assert err["code"] == "VALIDATION_FAILED"

    def test_unknown_export_id_download_returns_404(
        self, client: TestClient, workspace_id: str
    ) -> None:
        resp = client.get(
            f"/api/v1/workspaces/{workspace_id}/exports/exp_missing/download"
        )
        assert resp.status_code == 404
        err = resp.json()["error"]
        assert err["code"] == "EXPORT_NOT_FOUND"
        assert err["details"]["export_id"] == "exp_missing"

    def test_scope_range_missing_end_returns_422_invalid(
        self,
        client: TestClient,
        workspace_id: str,
        session_factory: Callable[[str], Session],
    ) -> None:
        _seed_db(session_factory, workspace_id, chapter_count=2)
        resp = client.post(
            f"/api/v1/workspaces/{workspace_id}/exports",
            json={
                "format": "md",
                "scope": "range",
                "start_chapter_id": "ch-0001",
            },
        )
        assert resp.status_code == 422
        err = resp.json()["error"]
        assert err["code"] == "EXPORT_SCOPE_INVALID"
        assert err["details"]["scope"] == "range"

    def test_unknown_scope_returns_422_pydantic(
        self,
        client: TestClient,
        workspace_id: str,
        session_factory: Callable[[str], Session],
    ) -> None:
        _seed_db(session_factory, workspace_id)
        resp = client.post(
            f"/api/v1/workspaces/{workspace_id}/exports",
            json={"format": "md", "scope": "unknown_scope"},
        )
        # Pydantic Literal 校验拒绝
        assert resp.status_code == 422
        assert resp.json()["error"]["code"] == "VALIDATION_FAILED"


# ---- gate:scope=range 含 invalidated ---------------------------------------


class TestInvalidatedGate:
    def test_range_with_invalidated_inside_returns_422_invalid(
        self,
        client: TestClient,
        workspace_id: str,
        session_factory: Callable[[str], Session],
    ) -> None:
        _seed_db(
            session_factory,
            workspace_id,
            chapter_count=3,
            invalidated_ids=["ch-0002"],
        )
        resp = client.post(
            f"/api/v1/workspaces/{workspace_id}/exports",
            json={
                "format": "md",
                "scope": "range",
                "start_chapter_id": "ch-0001",
                "end_chapter_id": "ch-0003",
            },
        )
        assert resp.status_code == 422
        err = resp.json()["error"]
        assert err["code"] == "EXPORT_SCOPE_INVALID"
        assert err["details"]["invalidated_chapter_ids"] == ["ch-0002"]
