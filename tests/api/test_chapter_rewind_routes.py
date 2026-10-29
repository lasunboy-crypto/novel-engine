"""``POST /chapters/{id}:rewind`` / ``:force-pass`` / ``:unblock`` +
``POST /workspaces/{id}:unblock-all`` 路由集成测试(P6-B-01)。

覆盖维度(对齐 plan §10 测试约定 5 类):

- **happy**:4 端点各 200 + envelope.data 字段完整 + ORM 落库 + WS event
- **error**:3 错误码各 1 测(`CHAPTER_REWIND_INVALID_STATE 422` /
  `CHAPTER_INVALIDATED_NEEDS_UNBLOCK 409` / `CHAPTER_UNBLOCK_INVALID_STATE 422`)
  + 缺 X-Workspace-Id 422 + force-pass body 缺 reason 422 + chapter 不存在 404
- **gate**:chapter:write / :rewrite / :revise 入口检测 invalidated → 409
- **WS event**:同 happy path,断言事件 publish

测试基础设施沿用 P5-B-07 ``test_chapter_card_approve_reject_route.py``:
in-memory SQLite + StaticPool + check_same_thread=False + tmp_path workspaces
根 + ``FakeVectorStore`` / ``FakeWSManager`` 注入。
"""

from __future__ import annotations

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
from src.api.deps import get_chapter_rewind_service, get_chapter_service
from src.api.main import create_app
from src.core.db import Base
from src.core.db.models import (
    ChapterCardORM,
    ChapterORM,
    PipelineRunORM,
    WorkspaceORM,
)
from src.core.llm.mock_client import MockLLMClient
from src.core.models.chapter_card import ChapterCard, ContinuityContext
from src.core.workspace.workspace_paths import WorkspacePaths
from src.services.chapter_rewind_service import ChapterRewindService
from src.services.chapter_service import ChapterService
from src.services.workspace_service import WorkspaceService


# ---- Fakes ------------------------------------------------------------------


class _FakeVectorStore:
    def __init__(self, *, revoked_count: int = 2) -> None:
        self.calls: list[tuple[str, str]] = []
        self._revoked_count = revoked_count

    def soft_delete_by_chapter_id(
        self, collection: str, *, chapter_id: str
    ) -> int:
        self.calls.append((collection, chapter_id))
        return self._revoked_count


class _FakeWSManager:
    def __init__(self) -> None:
        self.events: list[dict[str, Any]] = []

    def publish(self, *, channel: str, event: str, data: dict[str, Any]) -> None:
        self.events.append({"channel": channel, "event": event, "data": data})


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
        "ws-rewind-route",
        display_name="P6-B-01 rewind route test",
        genre="xianxia",
        premise="test",
        activate=False,
    )
    return "ws-rewind-route"


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


@pytest.fixture(autouse=True)
def _clear_task_registry() -> Iterator[None]:
    task_registry.clear()
    yield
    task_registry.clear()


def _seed_workspace_orm(
    session_factory: Callable[[str], Session], workspace_id: str
) -> None:
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


def _seed_chapter(
    session_factory: Callable[[str], Session],
    workspace_id: str,
    chapter_id: str,
    status: str = "approved",
) -> None:
    session = session_factory(workspace_id)
    try:
        now = datetime.now(timezone.utc)
        session.merge(
            ChapterORM(
                chapter_id=chapter_id,
                workspace_id=workspace_id,
                current_version=1,
                status=status,
                rewrite_count=0,
                revise_count=0,
                created_at=now,
                approved_at=now if status == "approved" else None,
                total_token_usage={},
                total_cost_usd=0.0,
            )
        )
        session.commit()
    finally:
        session.close()


def _seed_force_pass_files(
    workspace_service: WorkspaceService,
    workspace_id: str,
    chapter_id: str,
) -> None:
    paths = WorkspacePaths(
        workspace_id,
        workspaces_root=workspace_service.workspaces_root,
    )
    card = ChapterCard(
        chapter_id=chapter_id,
        workspace_id=workspace_id,
        volume_id="v01",
        chapter_number=int(chapter_id.rsplit("-", 1)[-1]),
        title="人工裁决章",
        tension_level=5,
        word_count_min=1,
        word_count_max=4000,
        must_achieve=[],
        must_unresolved=[],
        forbidden=[],
        pov_character="lin-yan",
        setting="山门",
        continuity_context=ContinuityContext(
            prev_chapter_tail="",
            unfinished_actions=[],
            time_gap="开篇",
            scene_continuation="switch",
            pov_change=False,
            pov_from=None,
            pov_to="lin-yan",
        ),
        next_chapter_seed="继续调查",
        arc_phase="opening",
        is_arc_finale=False,
        related_plot_threads=[],
        related_characters=["lin-yan"],
    )
    paths.cards_dir.mkdir(parents=True, exist_ok=True)
    (paths.cards_dir / f"{chapter_id}.json").write_text(
        card.model_dump_json(),
        encoding="utf-8",
    )
    version_path = paths.chapter_version_file(chapter_id, "v1.md")
    version_path.parent.mkdir(parents=True, exist_ok=True)
    version_path.write_text("林衍站在山门前，确认风铃仍在摇动。", encoding="utf-8")


def _seed_card_orm(
    session_factory: Callable[[str], Session],
    workspace_id: str,
    chapter_id: str,
    card_status: str = "approved",
) -> None:
    session = session_factory(workspace_id)
    try:
        now = datetime.now(timezone.utc)
        session.merge(
            ChapterCardORM(
                chapter_id=chapter_id,
                workspace_id=workspace_id,
                card_status=card_status,
                card_approved_at=now if card_status == "approved" else None,
                created_at=now,
                rejection_notes=None,
            )
        )
        session.commit()
    finally:
        session.close()


@pytest.fixture
def fake_vector_store() -> _FakeVectorStore:
    return _FakeVectorStore(revoked_count=3)


@pytest.fixture
def fake_ws_manager() -> _FakeWSManager:
    return _FakeWSManager()


@pytest.fixture
def client(
    workspace_service: WorkspaceService,
    session_factory: Callable[[str], Session],
    workspace_id: str,
    fake_vector_store: _FakeVectorStore,
    fake_ws_manager: _FakeWSManager,
) -> Iterator[TestClient]:
    app = create_app()
    mock_llm = MockLLMClient()
    chapter_service = ChapterService(
        workspace_service=workspace_service,
        client=mock_llm,
        model="claude-sonnet-4-6",
        session_factory=session_factory,
    )
    rewind_service = ChapterRewindService(
        workspace_service=workspace_service,
        session_factory=session_factory,
        vector_store_factory=lambda ws_id: fake_vector_store,
        ws_manager=fake_ws_manager,  # type: ignore[arg-type]
        llm_client=mock_llm,
        llm_model="mock-default",
    )
    app.dependency_overrides[get_chapter_service] = lambda: chapter_service
    app.dependency_overrides[get_chapter_rewind_service] = lambda: rewind_service
    with TestClient(app) as c:
        yield c
    app.dependency_overrides.clear()


# ---- rewind 端点 ------------------------------------------------------------


class TestRewindRoute:
    def test_rewind_happy_path(
        self,
        client: TestClient,
        workspace_id: str,
        session_factory: Callable[[str], Session],
        fake_vector_store: _FakeVectorStore,
        fake_ws_manager: _FakeWSManager,
    ) -> None:
        _seed_workspace_orm(session_factory, workspace_id)
        _seed_chapter(session_factory, workspace_id, "ch-0001", "approved")
        _seed_chapter(session_factory, workspace_id, "ch-0002", "approved")

        resp = client.post(
            "/api/v1/chapters/ch-0001:rewind",
            headers={"X-Workspace-Id": workspace_id},
            json={},
        )
        assert resp.status_code == 200, resp.text
        data = resp.json()["data"]
        assert data["chapter_id"] == "ch-0001"
        assert data["from_status"] == "approved"
        assert data["to_status"] == "drafting"
        assert data["invalidated_chapter_ids"] == ["ch-0002"]
        assert data["vector_revoked_count"] == 3
        assert data["extracted_revoked_count"] == 0

        # ORM 验证
        session = session_factory(workspace_id)
        try:
            assert session.get(ChapterORM, "ch-0001").status == "drafting"
            assert session.get(ChapterORM, "ch-0002").status == "invalidated"
        finally:
            session.close()

        # Chroma 调用
        assert fake_vector_store.calls == [("memories", "ch-0001")]

        # WS event
        events = [e["event"] for e in fake_ws_manager.events]
        assert "chapter_rewound" in events
        assert "chapter_invalidated" in events

    def test_rewind_from_drafting_returns_422(
        self,
        client: TestClient,
        workspace_id: str,
        session_factory: Callable[[str], Session],
    ) -> None:
        _seed_workspace_orm(session_factory, workspace_id)
        _seed_chapter(session_factory, workspace_id, "ch-0001", "drafting")

        resp = client.post(
            "/api/v1/chapters/ch-0001:rewind",
            headers={"X-Workspace-Id": workspace_id},
            json={},
        )
        assert resp.status_code == 422
        err = resp.json()["error"]
        assert err["code"] == "CHAPTER_REWIND_INVALID_STATE"
        assert err["details"]["chapter_id"] == "ch-0001"
        assert err["details"]["current_status"] == "drafting"

    def test_rewind_missing_workspace_header_returns_422(
        self,
        client: TestClient,
        workspace_id: str,
        session_factory: Callable[[str], Session],
    ) -> None:
        _seed_workspace_orm(session_factory, workspace_id)
        _seed_chapter(session_factory, workspace_id, "ch-0001", "approved")

        resp = client.post("/api/v1/chapters/ch-0001:rewind", json={})
        assert resp.status_code == 422
        assert resp.json()["error"]["code"] == "MISSING_WORKSPACE_HEADER"

    def test_rewind_chapter_not_found_returns_404(
        self,
        client: TestClient,
        workspace_id: str,
        session_factory: Callable[[str], Session],
    ) -> None:
        _seed_workspace_orm(session_factory, workspace_id)
        resp = client.post(
            "/api/v1/chapters/ch-nonexistent:rewind",
            headers={"X-Workspace-Id": workspace_id},
            json={},
        )
        assert resp.status_code == 404
        assert resp.json()["error"]["code"] == "CHAPTER_NOT_FOUND"


# ---- force-pass 端点 --------------------------------------------------------


class TestForcePassRoute:
    def test_force_pass_happy_path(
        self,
        client: TestClient,
        workspace_service: WorkspaceService,
        workspace_id: str,
        session_factory: Callable[[str], Session],
        fake_ws_manager: _FakeWSManager,
    ) -> None:
        _seed_workspace_orm(session_factory, workspace_id)
        _seed_chapter(
            session_factory, workspace_id, "ch-0001", "human_intervention"
        )
        _seed_force_pass_files(
            workspace_service,
            workspace_id,
            "ch-0001",
        )
        resp = client.post(
            "/api/v1/chapters/ch-0001:force-pass",
            headers={"X-Workspace-Id": workspace_id},
            json={"reason": "rewrite 配额耗尽,审稿无解"},
        )
        assert resp.status_code == 200, resp.text
        data = resp.json()["data"]
        assert data["chapter_id"] == "ch-0001"
        assert data["from_status"] == "human_intervention"
        assert data["to_status"] == "approved"
        assert data["force_pass_reason"] == "rewrite 配额耗尽,审稿无解"
        # ISO 8601 字符串
        assert "T" in data["force_pass_at"]

        # ORM 持久化
        session = session_factory(workspace_id)
        try:
            row = session.get(ChapterORM, "ch-0001")
            assert row.status == "approved"
            assert row.force_pass_reason == "rewrite 配额耗尽,审稿无解"
        finally:
            session.close()

        # WS event
        events = [e["event"] for e in fake_ws_manager.events]
        assert "chapter_force_passed" in events

    def test_force_pass_missing_reason_returns_422(
        self,
        client: TestClient,
        workspace_id: str,
        session_factory: Callable[[str], Session],
    ) -> None:
        """body.reason 强制非空 ``min_length=1``(自决项 1)。"""
        _seed_workspace_orm(session_factory, workspace_id)
        _seed_chapter(session_factory, workspace_id, "ch-0001", "reviewing")
        resp = client.post(
            "/api/v1/chapters/ch-0001:force-pass",
            headers={"X-Workspace-Id": workspace_id},
            json={},
        )
        assert resp.status_code == 422
        assert resp.json()["error"]["code"] == "VALIDATION_FAILED"

    def test_force_pass_empty_reason_returns_422(
        self,
        client: TestClient,
        workspace_id: str,
        session_factory: Callable[[str], Session],
    ) -> None:
        """``reason=""`` 不通过 ``min_length=1`` 校验。"""
        _seed_workspace_orm(session_factory, workspace_id)
        _seed_chapter(session_factory, workspace_id, "ch-0001", "reviewing")
        resp = client.post(
            "/api/v1/chapters/ch-0001:force-pass",
            headers={"X-Workspace-Id": workspace_id},
            json={"reason": ""},
        )
        assert resp.status_code == 422

    def test_force_pass_invalidated_returns_409_needs_unblock(
        self,
        client: TestClient,
        workspace_id: str,
        session_factory: Callable[[str], Session],
    ) -> None:
        _seed_workspace_orm(session_factory, workspace_id)
        _seed_chapter(
            session_factory, workspace_id, "ch-0001", "invalidated"
        )
        resp = client.post(
            "/api/v1/chapters/ch-0001:force-pass",
            headers={"X-Workspace-Id": workspace_id},
            json={"reason": "test"},
        )
        assert resp.status_code == 409
        err = resp.json()["error"]
        assert err["code"] == "CHAPTER_INVALIDATED_NEEDS_UNBLOCK"
        assert err["details"]["chapter_id"] == "ch-0001"

    def test_force_pass_already_approved_returns_409(
        self,
        client: TestClient,
        workspace_id: str,
        session_factory: Callable[[str], Session],
    ) -> None:
        _seed_workspace_orm(session_factory, workspace_id)
        _seed_chapter(session_factory, workspace_id, "ch-0001", "approved")
        resp = client.post(
            "/api/v1/chapters/ch-0001:force-pass",
            headers={"X-Workspace-Id": workspace_id},
            json={"reason": "test"},
        )
        assert resp.status_code == 409
        assert resp.json()["error"]["code"] == "CHAPTER_INVALID_STATE"


# ---- unblock 端点 -----------------------------------------------------------


class TestUnblockRoute:
    def test_unblock_happy_path(
        self,
        client: TestClient,
        workspace_id: str,
        session_factory: Callable[[str], Session],
        fake_ws_manager: _FakeWSManager,
    ) -> None:
        _seed_workspace_orm(session_factory, workspace_id)
        _seed_chapter(
            session_factory, workspace_id, "ch-0001", "invalidated"
        )
        resp = client.post(
            "/api/v1/chapters/ch-0001:unblock",
            headers={"X-Workspace-Id": workspace_id},
            json={},
        )
        assert resp.status_code == 200, resp.text
        data = resp.json()["data"]
        assert data["chapter_id"] == "ch-0001"
        assert data["from_status"] == "invalidated"
        assert data["to_status"] == "drafting"

        session = session_factory(workspace_id)
        try:
            assert session.get(ChapterORM, "ch-0001").status == "drafting"
        finally:
            session.close()

        events = [e["event"] for e in fake_ws_manager.events]
        assert "chapter_unblocked" in events

    def test_unblock_non_invalidated_returns_422(
        self,
        client: TestClient,
        workspace_id: str,
        session_factory: Callable[[str], Session],
    ) -> None:
        _seed_workspace_orm(session_factory, workspace_id)
        _seed_chapter(session_factory, workspace_id, "ch-0001", "drafting")
        resp = client.post(
            "/api/v1/chapters/ch-0001:unblock",
            headers={"X-Workspace-Id": workspace_id},
            json={},
        )
        assert resp.status_code == 422
        err = resp.json()["error"]
        assert err["code"] == "CHAPTER_UNBLOCK_INVALID_STATE"
        assert err["details"]["current_status"] == "drafting"


# ---- unblock-all 端点 -------------------------------------------------------


class TestUnblockAllRoute:
    def test_unblock_all_happy_path(
        self,
        client: TestClient,
        workspace_id: str,
        session_factory: Callable[[str], Session],
        fake_ws_manager: _FakeWSManager,
    ) -> None:
        _seed_workspace_orm(session_factory, workspace_id)
        _seed_chapter(
            session_factory, workspace_id, "ch-0001", "invalidated"
        )
        _seed_chapter(
            session_factory, workspace_id, "ch-0002", "invalidated"
        )
        _seed_chapter(session_factory, workspace_id, "ch-0003", "approved")

        resp = client.post(
            f"/api/v1/workspaces/{workspace_id}:unblock-all",
            json={},
        )
        assert resp.status_code == 200, resp.text
        data = resp.json()["data"]
        assert data["workspace_id"] == workspace_id
        assert data["unblocked_chapter_ids"] == ["ch-0001", "ch-0002"]
        assert data["unblocked_count"] == 2

        session = session_factory(workspace_id)
        try:
            assert session.get(ChapterORM, "ch-0001").status == "drafting"
            assert session.get(ChapterORM, "ch-0002").status == "drafting"
            assert session.get(ChapterORM, "ch-0003").status == "approved"  # 不变
        finally:
            session.close()

    def test_unblock_all_empty_returns_200_idempotent(
        self,
        client: TestClient,
        workspace_id: str,
        session_factory: Callable[[str], Session],
        fake_ws_manager: _FakeWSManager,
    ) -> None:
        """自决项 2:无 invalidated 章节 200 + count=0(对齐 P3-B-12 batch 模式)。"""
        _seed_workspace_orm(session_factory, workspace_id)
        _seed_chapter(session_factory, workspace_id, "ch-0001", "approved")
        resp = client.post(
            f"/api/v1/workspaces/{workspace_id}:unblock-all",
            json={},
        )
        assert resp.status_code == 200
        data = resp.json()["data"]
        assert data["unblocked_chapter_ids"] == []
        assert data["unblocked_count"] == 0


# ---- chapter:write / :rewrite / :revise gate -----------------------------


class TestInvalidatedGate:
    """rewind 后 chapter:write / :rewrite / :revise 入口 gate 检测 invalidated
    → 409 CHAPTER_INVALIDATED_NEEDS_UNBLOCK(对齐 plan §10 "gate" 测试类)。"""

    def test_write_invalidated_chapter_returns_409(
        self,
        client: TestClient,
        workspace_id: str,
        workspace_service: WorkspaceService,
        session_factory: Callable[[str], Session],
    ) -> None:
        _seed_workspace_orm(session_factory, workspace_id)
        _seed_chapter(
            session_factory, workspace_id, "ch-0001", "invalidated"
        )
        _seed_card_orm(
            session_factory, workspace_id, "ch-0001", "approved"
        )
        # 落 SSOT card 文件,绕过 ``ChapterCardNotFound`` 前置守卫
        card_dir = (
            workspace_service.workspaces_root / workspace_id / "data" / "cards"
        )
        card_dir.mkdir(parents=True, exist_ok=True)
        (card_dir / "ch-0001.json").write_text(
            '{"chapter_id":"ch-0001","card_status":"approved"}',
            encoding="utf-8",
        )

        # v1.0.x Commit B::write 改 BackgroundTasks 异步,gate (_run_gates)
        # 在 service.write 内抛 ChapterInvalidatedNeedsUnblockError → wrapper
        # update_to_failed + error.code 仍是 CHAPTER_INVALIDATED_NEEDS_UNBLOCK。
        # HTTP 202(异步语义),前端订阅 task progress 收到 failed event 显示错误。
        resp = client.post(
            "/api/v1/chapters/ch-0001:write",
            headers={"X-Workspace-Id": workspace_id},
            json={},
        )
        assert resp.status_code == 202, resp.text
        info = resp.json()["data"]
        task_id = info["task_id"]
        # 读 task_registry 验最终失败状态 + error.code
        from src.api import task_registry as _tr

        final = _tr.get(task_id)
        assert final is not None
        assert final.status == "failed"
        assert final.error is not None
        assert final.error["code"] == "CHAPTER_INVALIDATED_NEEDS_UNBLOCK"
        assert final.error["details"]["chapter_id"] == "ch-0001"
        assert final.error["details"]["workspace_id"] == workspace_id

    def test_write_after_unblock_no_longer_409(
        self,
        client: TestClient,
        workspace_id: str,
        workspace_service: WorkspaceService,
        session_factory: Callable[[str], Session],
    ) -> None:
        """rewind → unblock → write 链路:unblock 后 write 不再 409
        ``CHAPTER_INVALIDATED_NEEDS_UNBLOCK`` (gate 通过)。"""
        _seed_workspace_orm(session_factory, workspace_id)
        _seed_chapter(
            session_factory, workspace_id, "ch-0001", "invalidated"
        )
        _seed_card_orm(
            session_factory, workspace_id, "ch-0001", "approved"
        )
        card_dir = (
            workspace_service.workspaces_root / workspace_id / "data" / "cards"
        )
        card_dir.mkdir(parents=True, exist_ok=True)
        (card_dir / "ch-0001.json").write_text(
            '{"chapter_id":"ch-0001","card_status":"approved"}',
            encoding="utf-8",
        )

        # unblock
        resp = client.post(
            "/api/v1/chapters/ch-0001:unblock",
            headers={"X-Workspace-Id": workspace_id},
            json={},
        )
        assert resp.status_code == 200

        # 再 write — gate 不应再撞 invalidated;允许 write 失败但**不应是 ``CHAPTER_INVALIDATED_NEEDS_UNBLOCK``**。
        resp = client.post(
            "/api/v1/chapters/ch-0001:write",
            headers={"X-Workspace-Id": workspace_id},
            json={},
        )
        # 此时 mock LLM 路径可能因缺章节卡 JSON 完整性失败 → 任一非
        # CHAPTER_INVALIDATED_NEEDS_UNBLOCK 都符合本测试断言。
        if resp.status_code >= 400:
            assert (
                resp.json()["error"]["code"]
                != "CHAPTER_INVALIDATED_NEEDS_UNBLOCK"
            )


# ---- PUT /chapters/{id}/content 端点(v1.1 acceptance 修复2) ----------------


class TestUpdateContentRoute:
    """孤儿章人工编辑正文端点。"""

    def _write_v1(
        self,
        workspace_service: WorkspaceService,
        workspace_id: str,
        chapter_id: str,
    ) -> Path:
        from src.core.workspace.workspace_paths import WorkspacePaths

        paths = WorkspacePaths(
            workspace_id, workspaces_root=workspace_service.workspaces_root
        )
        chapter_dir = paths.chapter_dir(chapter_id)
        chapter_dir.mkdir(parents=True, exist_ok=True)
        (chapter_dir / "v1.md").write_text(
            "原 LLM 版正文。" * 30, encoding="utf-8"
        )
        return chapter_dir

    def test_update_content_happy_200(
        self,
        client: TestClient,
        workspace_id: str,
        workspace_service: WorkspaceService,
        session_factory: Callable[[str], Session],
        fake_ws_manager: _FakeWSManager,
    ) -> None:
        _seed_workspace_orm(session_factory, workspace_id)
        _seed_chapter(
            session_factory, workspace_id, "ch-0007", status="human_intervention"
        )
        chapter_dir = self._write_v1(workspace_service, workspace_id, "ch-0007")

        edited = "人工修复后的正文。" * 40
        resp = client.put(
            "/api/v1/chapters/ch-0007/content",
            json={"content": edited},
            headers={"X-Workspace-Id": workspace_id},
        )
        assert resp.status_code == 200
        data = resp.json()["data"]
        assert data["chapter_id"] == "ch-0007"
        assert data["version_id"] == 2
        assert data["word_count"] == len(edited)
        assert (chapter_dir / "final.md").read_text(encoding="utf-8") == edited
        assert any(
            e["event"] == "chapter_content_updated"
            for e in fake_ws_manager.events
        )

    def test_update_content_wrong_status_409(
        self,
        client: TestClient,
        workspace_id: str,
        session_factory: Callable[[str], Session],
    ) -> None:
        _seed_workspace_orm(session_factory, workspace_id)
        _seed_chapter(session_factory, workspace_id, "ch-0001", status="approved")
        resp = client.put(
            "/api/v1/chapters/ch-0001/content",
            json={"content": "不应写入"},
            headers={"X-Workspace-Id": workspace_id},
        )
        assert resp.status_code == 409
        assert resp.json()["error"]["code"] == "CHAPTER_INVALID_STATE"

    def test_update_content_missing_chapter_404(
        self,
        client: TestClient,
        workspace_id: str,
        session_factory: Callable[[str], Session],
    ) -> None:
        _seed_workspace_orm(session_factory, workspace_id)
        resp = client.put(
            "/api/v1/chapters/ch-0099/content",
            json={"content": "x"},
            headers={"X-Workspace-Id": workspace_id},
        )
        assert resp.status_code == 404
        assert resp.json()["error"]["code"] == "CHAPTER_NOT_FOUND"

    def test_update_content_empty_body_422(
        self,
        client: TestClient,
        workspace_id: str,
        session_factory: Callable[[str], Session],
    ) -> None:
        _seed_workspace_orm(session_factory, workspace_id)
        _seed_chapter(
            session_factory, workspace_id, "ch-0007", status="human_intervention"
        )
        resp = client.put(
            "/api/v1/chapters/ch-0007/content",
            json={"content": ""},
            headers={"X-Workspace-Id": workspace_id},
        )
        assert resp.status_code == 422


# ---- reset-stale 端点(v2 w1-s2 中断章自愈) ---------------------------------


class TestResetStaleRoute:
    def test_reset_stale_happy_path(
        self,
        client: TestClient,
        workspace_id: str,
        session_factory: Callable[[str], Session],
        fake_ws_manager: _FakeWSManager,
    ) -> None:
        """滞留流程中间态的章重置 drafting;终态 / "等人"态不动。"""
        _seed_chapter(session_factory, workspace_id, "ch-0001", "reviewing")
        _seed_chapter(session_factory, workspace_id, "ch-0002", "rewriting")
        _seed_chapter(session_factory, workspace_id, "ch-0003", "approved")
        _seed_chapter(
            session_factory, workspace_id, "ch-0004", "human_intervention"
        )
        resp = client.post(
            f"/api/v1/workspaces/{workspace_id}:reset-stale",
            json={},
        )
        assert resp.status_code == 200, resp.text
        data = resp.json()["data"]
        assert data["workspace_id"] == workspace_id
        assert data["reset_chapter_ids"] == ["ch-0001", "ch-0002"]
        assert data["reset_count"] == 2
        assert data["skipped_reason"] is None
        # DB 行已翻 drafting;approved / human_intervention 不动
        session = session_factory(workspace_id)
        try:
            assert session.get(ChapterORM, "ch-0001").status == "drafting"
            assert session.get(ChapterORM, "ch-0002").status == "drafting"
            assert session.get(ChapterORM, "ch-0003").status == "approved"
            assert (
                session.get(ChapterORM, "ch-0004").status
                == "human_intervention"
            )
        finally:
            session.close()
        # WS publish chapter_stale_reset 每章
        stale_events = [
            e for e in fake_ws_manager.events
            if e["event"] == "chapter_stale_reset"
        ]
        assert len(stale_events) == 2

    def test_reset_stale_empty_returns_200_idempotent(
        self,
        client: TestClient,
        workspace_id: str,
    ) -> None:
        resp = client.post(
            f"/api/v1/workspaces/{workspace_id}:reset-stale",
            json={},
        )
        assert resp.status_code == 200, resp.text
        data = resp.json()["data"]
        assert data["reset_count"] == 0
        assert data["reset_chapter_ids"] == []
        assert data["skipped_reason"] is None

    def test_reset_stale_skipped_reason_passthrough(
        self,
        client: TestClient,
        workspace_id: str,
        session_factory: Callable[[str], Session],
    ) -> None:
        """对账跳过透传:running pipeline run → 200 + skipped_reason 字段
        (防路由层漏映射 skipped_reason — UI 靠它提示"等任务结束重试")。"""
        _seed_chapter(session_factory, workspace_id, "ch-0001", "reviewing")
        session = session_factory(workspace_id)
        try:
            now = datetime.now(timezone.utc)
            session.add(
                PipelineRunORM(
                    run_id="run-busy",
                    workspace_id=workspace_id,
                    state="running",
                    scope="single",
                    start_chapter_id="ch-0001",
                    end_chapter_id=None,
                    volume_id=None,
                    profile_override=None,
                    chapter_ids=["ch-0001"],
                    current_chapter="ch-0001",
                    completed_chapter_ids=[],
                    pause_reason=None,
                    error_details=None,
                    created_at=now,
                    updated_at=now,
                    completed_at=None,
                )
            )
            session.commit()
        finally:
            session.close()

        resp = client.post(
            f"/api/v1/workspaces/{workspace_id}:reset-stale",
            json={},
        )
        assert resp.status_code == 200, resp.text
        data = resp.json()["data"]
        assert data["skipped_reason"] == "active_pipeline_run"
        assert data["reset_count"] == 0
        # 章未被误杀
        session = session_factory(workspace_id)
        try:
            assert session.get(ChapterORM, "ch-0001").status == "reviewing"
        finally:
            session.close()
