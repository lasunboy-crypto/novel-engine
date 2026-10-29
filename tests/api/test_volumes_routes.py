"""Volumes 路由集成测试(P5-B-09)。

覆盖 5 个端点 happy path + 关键错误码:

- ``GET /workspaces/{id}/volumes``:200 + 列表(确定性顺序)
- ``GET /workspaces/{id}/volumes/{vid}``:200 + 单卷 / 404 ``VOLUME_NOT_FOUND``
- ``PUT /workspaces/{id}/volumes/{vid}``:200 + patch 后字段已更新 / 422
  ``WORKSPACE_INVALID``(``tension_curve`` 长度 vs ``chapter_count`` 不一致)
- ``POST /workspaces/{id}/volumes/{vid}:apply-template``:200 + 张力曲线已填
  + 模板字段同步 + arcs_json 不动 / 404 卷不存在
- ``GET /workspaces/{id}/outline:status``:200 + 当前 status / ORM 行不存在 → not_started

测试基础设施
============

工作区物理目录通过 tmp_path + ``WorkspaceService.create()`` 建立;
``VolumeORM`` 行通过 fixture seed 直接 SQL insert(对齐 P5-B-09 测试纯
CRUD 性质,不依赖 outline:generate / commit 链路 — 后者在
``test_outline_routes.py`` 已覆盖)。
"""

from __future__ import annotations

from collections.abc import Callable, Iterator
from datetime import UTC, datetime
from pathlib import Path

import pytest
from fastapi.testclient import TestClient
from sqlalchemy import create_engine
from sqlalchemy.orm import Session

from src.api.deps import get_volume_service
from src.api.main import create_app
from src.core.db import Base
from src.core.db.models import VolumeORM, WorkspaceORM
from src.core.workspace.workspace_paths import WorkspacePaths
from src.services.volume_service import VolumeService
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
        "ws-volumes-api",
        display_name="P5-B-09 volumes routes test",
        genre="test",
        premise="x",
        activate=False,
    )
    return "ws-volumes-api"


def _make_session_factory(
    workspace_service: WorkspaceService,
) -> Callable[[str], Session]:
    def _factory(workspace_id: str) -> Session:
        paths = WorkspacePaths(
            workspace_id, workspaces_root=workspace_service.workspaces_root
        )
        engine = create_engine(f"sqlite:///{paths.db_path}")
        Base.metadata.create_all(engine)
        return Session(engine)

    return _factory


def _seed_volume(
    session_factory: Callable[[str], Session],
    workspace_id: str,
    *,
    volume_id: str,
    title: str = "卷一·初临",
    chapter_count: int = 9,
    arcs_json: list[dict] | None = None,
    tension_curve_json: list[dict] | None = None,
    tension_template: str = "heros_journey",
) -> None:
    """直接 SQL insert 一个 VolumeORM 行(避免依赖 outline:generate 链路)。"""
    now = datetime.now(UTC)
    session = session_factory(workspace_id)
    try:
        # ensure WorkspaceORM 行存在(get_outline_status 测试需要)
        if session.get(WorkspaceORM, workspace_id) is None:
            session.add(
                WorkspaceORM(
                    workspace_id=workspace_id,
                    display_name="P5-B-09 test",
                    genre="test",
                    premise="x",
                    status="active",
                    created_at=now,
                    last_active_at=now,
                    engine_version="0.1.0",
                    outline_status="not_started",
                )
            )
        session.add(
            VolumeORM(
                volume_id=volume_id,
                workspace_id=workspace_id,
                title=title,
                summary=f"summary of {title}",
                chapter_count=chapter_count,
                tension_template=tension_template,
                arcs_json=arcs_json or [],
                tension_curve_json=tension_curve_json or [],
                created_at=now,
                last_updated_at=now,
            )
        )
        session.commit()
    finally:
        session.close()


@pytest.fixture
def session_factory(
    workspace_service: WorkspaceService,
) -> Callable[[str], Session]:
    return _make_session_factory(workspace_service)


@pytest.fixture
def client(
    workspace_service: WorkspaceService,
    session_factory: Callable[[str], Session],
) -> Iterator[TestClient]:
    app = create_app()
    service = VolumeService(
        workspace_service=workspace_service,
        session_factory=session_factory,
    )
    app.dependency_overrides[get_volume_service] = lambda: service
    with TestClient(app) as c:
        yield c
    app.dependency_overrides.clear()


# =============================================================================
# GET /volumes(列表)
# =============================================================================


class TestListVolumes:
    def test_empty_workspace_returns_empty_list(
        self, client: TestClient, workspace_id: str
    ) -> None:
        resp = client.get(f"/api/v1/workspaces/{workspace_id}/volumes")
        assert resp.status_code == 200
        body = resp.json()
        assert body["data"]["volumes"] == []

    def test_returns_volumes_in_order(
        self,
        client: TestClient,
        workspace_id: str,
        session_factory: Callable[[str], Session],
    ) -> None:
        _seed_volume(session_factory, workspace_id, volume_id="vol-2")
        _seed_volume(session_factory, workspace_id, volume_id="vol-1")
        _seed_volume(session_factory, workspace_id, volume_id="vol-3")

        resp = client.get(f"/api/v1/workspaces/{workspace_id}/volumes")
        assert resp.status_code == 200
        ids = [v["volume_id"] for v in resp.json()["data"]["volumes"]]
        # 按 volume_id 升序
        assert ids == ["vol-1", "vol-2", "vol-3"]

    def test_workspace_not_found_returns_422(self, client: TestClient) -> None:
        resp = client.get("/api/v1/workspaces/no-such-ws/volumes")
        assert resp.status_code == 422
        assert resp.json()["error"]["code"] == "WORKSPACE_INVALID"


# =============================================================================
# GET /volumes/{vid}(单卷)
# =============================================================================


class TestGetVolume:
    def test_happy_path(
        self,
        client: TestClient,
        workspace_id: str,
        session_factory: Callable[[str], Session],
    ) -> None:
        _seed_volume(
            session_factory,
            workspace_id,
            volume_id="vol-x",
            title="测试卷",
            chapter_count=10,
        )
        resp = client.get(
            f"/api/v1/workspaces/{workspace_id}/volumes/vol-x"
        )
        assert resp.status_code == 200
        data = resp.json()["data"]
        assert data["volume_id"] == "vol-x"
        assert data["workspace_id"] == workspace_id
        assert data["title"] == "测试卷"
        assert data["chapter_count"] == 10
        assert data["tension_template"] == "heros_journey"
        assert data["arcs"] == []
        assert data["tension_curve"] == []

    def test_not_found_returns_404(
        self, client: TestClient, workspace_id: str
    ) -> None:
        resp = client.get(
            f"/api/v1/workspaces/{workspace_id}/volumes/no-such-vol"
        )
        assert resp.status_code == 404
        assert resp.json()["error"]["code"] == "VOLUME_NOT_FOUND"

    def test_cross_workspace_volume_returns_404(
        self,
        client: TestClient,
        workspace_id: str,
        workspace_service: WorkspaceService,
        session_factory: Callable[[str], Session],
    ) -> None:
        # 在另一个工作区建卷,本工作区查应返 404(跨工作区视为不存在)
        workspace_service.create(
            "ws-other",
            display_name="other",
            genre="t",
            premise="x",
            activate=False,
        )
        _seed_volume(session_factory, "ws-other", volume_id="vol-other")
        resp = client.get(
            f"/api/v1/workspaces/{workspace_id}/volumes/vol-other"
        )
        assert resp.status_code == 404
        assert resp.json()["error"]["code"] == "VOLUME_NOT_FOUND"


# =============================================================================
# PUT /volumes/{vid}(patch)
# =============================================================================


class TestUpdateVolume:
    def test_patch_title_only(
        self,
        client: TestClient,
        workspace_id: str,
        session_factory: Callable[[str], Session],
    ) -> None:
        _seed_volume(session_factory, workspace_id, volume_id="vol-p")
        resp = client.put(
            f"/api/v1/workspaces/{workspace_id}/volumes/vol-p",
            json={"title": "新标题"},
        )
        assert resp.status_code == 200
        assert resp.json()["data"]["title"] == "新标题"

        # round-trip:再 GET 返新值
        resp2 = client.get(
            f"/api/v1/workspaces/{workspace_id}/volumes/vol-p"
        )
        assert resp2.json()["data"]["title"] == "新标题"

    def test_patch_summary_and_template(
        self,
        client: TestClient,
        workspace_id: str,
        session_factory: Callable[[str], Session],
    ) -> None:
        _seed_volume(session_factory, workspace_id, volume_id="vol-st")
        resp = client.put(
            f"/api/v1/workspaces/{workspace_id}/volumes/vol-st",
            json={
                "summary": "改后概要",
                "tension_template": "steady_daily",
            },
        )
        assert resp.status_code == 200
        data = resp.json()["data"]
        assert data["summary"] == "改后概要"
        assert data["tension_template"] == "steady_daily"

    def test_patch_arcs_full_replace(
        self,
        client: TestClient,
        workspace_id: str,
        session_factory: Callable[[str], Session],
    ) -> None:
        _seed_volume(session_factory, workspace_id, volume_id="vol-a", chapter_count=12)
        new_arcs = [
            {
                "arc_id": "arc-1",
                "arc_type": "主线",
                "chapter_range": [1, 6],
                "phase": "rising",
                "summary": "弧 1 概要",
            },
            {
                "arc_id": "arc-2",
                "arc_type": "副本",
                "chapter_range": [7, 12],
                "phase": "climax",
                "summary": "弧 2 概要",
            },
        ]
        resp = client.put(
            f"/api/v1/workspaces/{workspace_id}/volumes/vol-a",
            json={"arcs": new_arcs},
        )
        assert resp.status_code == 200
        arcs = resp.json()["data"]["arcs"]
        assert len(arcs) == 2
        assert arcs[0]["arc_id"] == "arc-1"
        assert arcs[1]["chapter_range"] == [7, 12]

    def test_patch_chapter_count_without_curve_resize_returns_422(
        self,
        client: TestClient,
        workspace_id: str,
        session_factory: Callable[[str], Session],
    ) -> None:
        # seed:chapter_count=9 + tension_curve 长度 9
        curve_9 = [
            {"chapter_number": i + 1, "tension": 5, "is_arc_finale": False}
            for i in range(9)
        ]
        _seed_volume(
            session_factory,
            workspace_id,
            volume_id="vol-c",
            chapter_count=9,
            tension_curve_json=curve_9,
        )
        # patch chapter_count → 12 不动 curve(长度仍 9)→ 校验失败
        resp = client.put(
            f"/api/v1/workspaces/{workspace_id}/volumes/vol-c",
            json={"chapter_count": 12},
        )
        assert resp.status_code == 422
        assert resp.json()["error"]["code"] == "WORKSPACE_INVALID"

    def test_patch_chapter_count_with_curve_resize_succeeds(
        self,
        client: TestClient,
        workspace_id: str,
        session_factory: Callable[[str], Session],
    ) -> None:
        curve_9 = [
            {"chapter_number": i + 1, "tension": 5, "is_arc_finale": False}
            for i in range(9)
        ]
        _seed_volume(
            session_factory,
            workspace_id,
            volume_id="vol-c2",
            chapter_count=9,
            tension_curve_json=curve_9,
        )
        new_curve_12 = [
            {"chapter_number": i + 1, "tension": 6, "is_arc_finale": False}
            for i in range(12)
        ]
        resp = client.put(
            f"/api/v1/workspaces/{workspace_id}/volumes/vol-c2",
            json={"chapter_count": 12, "tension_curve": new_curve_12},
        )
        assert resp.status_code == 200
        data = resp.json()["data"]
        assert data["chapter_count"] == 12
        assert len(data["tension_curve"]) == 12

    def test_extra_field_rejected(
        self,
        client: TestClient,
        workspace_id: str,
        session_factory: Callable[[str], Session],
    ) -> None:
        _seed_volume(session_factory, workspace_id, volume_id="vol-e")
        resp = client.put(
            f"/api/v1/workspaces/{workspace_id}/volumes/vol-e",
            json={"title": "ok", "unknown_field": "blocked"},
        )
        assert resp.status_code == 422  # extra="forbid"

    def test_volume_not_found_returns_404(
        self, client: TestClient, workspace_id: str
    ) -> None:
        resp = client.put(
            f"/api/v1/workspaces/{workspace_id}/volumes/no-such",
            json={"title": "x"},
        )
        assert resp.status_code == 404
        assert resp.json()["error"]["code"] == "VOLUME_NOT_FOUND"


# =============================================================================
# POST /volumes/{vid}:apply-template
# =============================================================================


class TestApplyTemplate:
    def test_heros_journey_fills_curve(
        self,
        client: TestClient,
        workspace_id: str,
        session_factory: Callable[[str], Session],
    ) -> None:
        _seed_volume(
            session_factory,
            workspace_id,
            volume_id="vol-t",
            chapter_count=11,
            tension_template="free",
        )
        resp = client.post(
            f"/api/v1/workspaces/{workspace_id}/volumes/vol-t:apply-template",
            json={"template": "heros_journey"},
        )
        assert resp.status_code == 200
        data = resp.json()["data"]
        assert data["volume_id"] == "vol-t"
        assert data["template"] == "heros_journey"
        curve = data["tension_curve"]
        assert len(curve) == 11
        # heros_journey 11 采样点字面 (3, 4, 5, 6, 5, 6, 7, 7, 8, 9, 6) — chapter_count=11
        # 时插值 1:1 命中(端点对齐)
        tensions = [pt["tension"] for pt in curve]
        assert tensions == [3, 4, 5, 6, 5, 6, 7, 7, 8, 9, 6]

    def test_apply_does_not_touch_arcs(
        self,
        client: TestClient,
        workspace_id: str,
        session_factory: Callable[[str], Session],
    ) -> None:
        existing_arcs = [
            {
                "arc_id": "arc-keep",
                "arc_type": "主线",
                "chapter_range": [1, 5],
                "phase": "rising",
                "summary": "应被保留",
            },
        ]
        _seed_volume(
            session_factory,
            workspace_id,
            volume_id="vol-arc",
            chapter_count=11,
            arcs_json=existing_arcs,
        )
        resp = client.post(
            f"/api/v1/workspaces/{workspace_id}/volumes/vol-arc:apply-template",
            json={"template": "steady_daily"},
        )
        assert resp.status_code == 200

        # 确认 arcs_json 未被覆盖 — 再 GET 检查
        resp2 = client.get(
            f"/api/v1/workspaces/{workspace_id}/volumes/vol-arc"
        )
        arcs = resp2.json()["data"]["arcs"]
        assert len(arcs) == 1
        assert arcs[0]["arc_id"] == "arc-keep"

    def test_apply_syncs_template_field(
        self,
        client: TestClient,
        workspace_id: str,
        session_factory: Callable[[str], Session],
    ) -> None:
        _seed_volume(
            session_factory,
            workspace_id,
            volume_id="vol-sync",
            chapter_count=11,
            tension_template="free",
        )
        client.post(
            f"/api/v1/workspaces/{workspace_id}/volumes/vol-sync:apply-template",
            json={"template": "strong_stimulation"},
        )
        # 再 GET 看 tension_template 字段已落
        resp = client.get(
            f"/api/v1/workspaces/{workspace_id}/volumes/vol-sync"
        )
        assert resp.json()["data"]["tension_template"] == "strong_stimulation"

    def test_is_arc_finale_marked_by_arc_ends(
        self,
        client: TestClient,
        workspace_id: str,
        session_factory: Callable[[str], Session],
    ) -> None:
        # arcs 末位 5 / 11
        arcs = [
            {
                "arc_id": "arc-1",
                "arc_type": "主线",
                "chapter_range": [1, 5],
                "phase": "rising",
                "summary": "x",
            },
            {
                "arc_id": "arc-2",
                "arc_type": "副本",
                "chapter_range": [6, 11],
                "phase": "climax",
                "summary": "y",
            },
        ]
        _seed_volume(
            session_factory,
            workspace_id,
            volume_id="vol-finale",
            chapter_count=11,
            arcs_json=arcs,
        )
        resp = client.post(
            f"/api/v1/workspaces/{workspace_id}/volumes/vol-finale:apply-template",
            json={"template": "free"},
        )
        curve = resp.json()["data"]["tension_curve"]
        finale_chapters = {
            pt["chapter_number"] for pt in curve if pt["is_arc_finale"]
        }
        assert finale_chapters == {5, 11}

    def test_invalid_template_rejected(
        self,
        client: TestClient,
        workspace_id: str,
        session_factory: Callable[[str], Session],
    ) -> None:
        _seed_volume(session_factory, workspace_id, volume_id="vol-bad")
        resp = client.post(
            f"/api/v1/workspaces/{workspace_id}/volumes/vol-bad:apply-template",
            json={"template": "nonexistent"},
        )
        assert resp.status_code == 422  # Literal 校验

    def test_volume_not_found_returns_404(
        self, client: TestClient, workspace_id: str
    ) -> None:
        resp = client.post(
            f"/api/v1/workspaces/{workspace_id}/volumes/no-such:apply-template",
            json={"template": "heros_journey"},
        )
        assert resp.status_code == 404
        assert resp.json()["error"]["code"] == "VOLUME_NOT_FOUND"


# =============================================================================
# GET /outline:status
# =============================================================================


class TestOutlineStatus:
    def test_returns_default_when_no_orm_row(
        self, client: TestClient, workspace_id: str
    ) -> None:
        resp = client.get(
            f"/api/v1/workspaces/{workspace_id}/outline:status"
        )
        assert resp.status_code == 200
        data = resp.json()["data"]
        assert data["workspace_id"] == workspace_id
        assert data["outline_status"] == "not_started"

    def test_returns_seeded_status(
        self,
        client: TestClient,
        workspace_id: str,
        session_factory: Callable[[str], Session],
    ) -> None:
        # seed WorkspaceORM 行 + outline_status='committed'
        session = session_factory(workspace_id)
        try:
            now = datetime.now(UTC)
            session.add(
                WorkspaceORM(
                    workspace_id=workspace_id,
                    display_name="x",
                    genre="t",
                    premise="x",
                    status="active",
                    created_at=now,
                    last_active_at=now,
                    engine_version="0.1.0",
                    outline_status="committed",
                )
            )
            session.commit()
        finally:
            session.close()

        resp = client.get(
            f"/api/v1/workspaces/{workspace_id}/outline:status"
        )
        assert resp.json()["data"]["outline_status"] == "committed"

    def test_workspace_not_found_returns_422(self, client: TestClient) -> None:
        resp = client.get("/api/v1/workspaces/no-such-ws/outline:status")
        assert resp.status_code == 422
        assert resp.json()["error"]["code"] == "WORKSPACE_INVALID"
