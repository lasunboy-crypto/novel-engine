"""``POST /api/v1/pipeline:start`` / ``:pause`` / ``:resume`` / ``:stop`` +
``GET /api/v1/pipeline/status`` 路由集成测试(P6-B-02)。

覆盖维度(对齐 plan §10 测试约定 5 类):

- **happy**:5 端点各 1+ 测覆盖 envelope 字段完整 + 服务调用 + WS event
- **error**:3 错误码各 1 测(``PIPELINE_ALREADY_RUNNING 409`` /
  ``PIPELINE_NOT_RUNNING 409`` / ``PIPELINE_INVALID_SCOPE 422``)+ 缺
  X-Workspace-Id 422 + 缺 scope 字段 422
- **gate**:execute 撞 invalidated → 自动 pause(集成测,通过 fake chapter
  service 注入)
- **WS event**:同 happy path,断言 pipeline_started / chapter_started /
  chapter_finished / pipeline_stopped 4 类事件 publish 到 ``pipeline.events``
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

from src.api.deps import get_pipeline_service
from src.api.main import create_app
from src.core.db import Base
from src.core.db.models import (
    AttemptLedgerORM,
    ChapterCardORM,
    PipelineRunORM,
    VolumeORM,
    WorkspaceORM,
)
from src.services.chapter_rewind_service import (
    ChapterInvalidatedNeedsUnblockError,
)
from src.services.pipeline_service import PipelineService
from src.services.workspace_service import WorkspaceService


# ---- Fakes ------------------------------------------------------------------


class _FakeChapterService:
    def __init__(self, *, raise_on: dict[str, Exception] | None = None) -> None:
        self.calls: list[tuple[str, str, str | None]] = []
        self._raise_on = raise_on or {}

    def write(
        self,
        workspace_id: str,
        chapter_id: str,
        *,
        on_state_transition=None,
        profile_override: str | None = None,
    ) -> Any:
        self.calls.append((workspace_id, chapter_id, profile_override))
        if chapter_id in self._raise_on:
            raise self._raise_on[chapter_id]
        return None


class _FakeWSManager:
    def __init__(self) -> None:
        self.events: list[dict[str, Any]] = []

    def publish(self, *, channel: str, event: str, data: dict[str, Any]) -> None:
        self.events.append({"channel": channel, "event": event, "data": data})


# ---- Fixtures ---------------------------------------------------------------


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
        "ws-pipe-route",
        display_name="P6-B-02 pipeline route test",
        genre="xianxia",
        premise="test",
        activate=False,
    )
    return "ws-pipe-route"


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


def _seed_workspace_with_volume(
    session_factory: Callable[[str], Session],
    workspace_id: str,
    *,
    chapter_count: int = 3,
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
        session.commit()
    finally:
        session.close()


@pytest.fixture
def fake_chapter_service() -> _FakeChapterService:
    return _FakeChapterService()


@pytest.fixture
def fake_ws_manager() -> _FakeWSManager:
    return _FakeWSManager()


@pytest.fixture
def pipeline_service(
    workspace_service: WorkspaceService,
    session_factory: Callable[[str], Session],
    fake_chapter_service: _FakeChapterService,
    fake_ws_manager: _FakeWSManager,
) -> PipelineService:
    return PipelineService(
        chapter_service=fake_chapter_service,  # type: ignore[arg-type]
        workspace_service=workspace_service,
        session_factory=session_factory,
        ws_manager=fake_ws_manager,  # type: ignore[arg-type]
    )


@pytest.fixture
def client(
    pipeline_service: PipelineService,
) -> Iterator[TestClient]:
    app = create_app()
    app.dependency_overrides[get_pipeline_service] = lambda: pipeline_service
    with TestClient(app) as c:
        yield c
    app.dependency_overrides.clear()


# ---- :start happy + error -------------------------------------------------


class TestStartRoute:
    def test_start_range_returns_202_and_runs_all_chapters(
        self,
        client: TestClient,
        workspace_id: str,
        session_factory: Callable[[str], Session],
        fake_chapter_service: _FakeChapterService,
        fake_ws_manager: _FakeWSManager,
    ) -> None:
        _seed_workspace_with_volume(session_factory, workspace_id, chapter_count=3)
        resp = client.post(
            "/api/v1/pipeline:start",
            headers={"X-Workspace-Id": workspace_id},
            json={
                "scope": "range",
                "start_chapter_id": "ch-0001",
                "end_chapter_id": "ch-0003",
                "provider_call_cap": 96,
            },
        )
        assert resp.status_code == 202, resp.text
        data = resp.json()["data"]
        assert data["run_id"].startswith("run_")
        assert data["state"] == "running"
        assert data["scope"] == "range"
        assert data["chapter_ids"] == ["ch-0001", "ch-0002", "ch-0003"]
        assert data["provider_call_cap"] == 96

        # BackgroundTasks 在 TestClient 上下文内同步跑;响应回来后已完成
        assert [c[1] for c in fake_chapter_service.calls] == [
            "ch-0001",
            "ch-0002",
            "ch-0003",
        ]
        # 6 类 event 中 4 类 publish 实证(start / chapter_started × 3 /
        # chapter_finished × 3 / stopped[reason=completed])
        event_names = [e["event"] for e in fake_ws_manager.events]
        assert "pipeline_started" in event_names
        assert event_names.count("chapter_started") == 3
        assert event_names.count("chapter_finished") == 3
        assert "pipeline_stopped" in event_names

    def test_start_single_with_profile_override(
        self,
        client: TestClient,
        workspace_id: str,
        session_factory: Callable[[str], Session],
        fake_chapter_service: _FakeChapterService,
    ) -> None:
        _seed_workspace_with_volume(session_factory, workspace_id)
        resp = client.post(
            "/api/v1/pipeline:start",
            headers={"X-Workspace-Id": workspace_id},
            json={
                "scope": "single",
                "start_chapter_id": "ch-0001",
                "profile_override": "cloud_premium",
            },
        )
        assert resp.status_code == 202
        assert resp.json()["data"]["chapter_ids"] == ["ch-0001"]
        # write 被调用且带 profile_override
        assert fake_chapter_service.calls == [
            (workspace_id, "ch-0001", "cloud_premium")
        ]

    def test_start_volume_scope(
        self,
        client: TestClient,
        workspace_id: str,
        session_factory: Callable[[str], Session],
    ) -> None:
        _seed_workspace_with_volume(session_factory, workspace_id, chapter_count=2)
        resp = client.post(
            "/api/v1/pipeline:start",
            headers={"X-Workspace-Id": workspace_id},
            json={"scope": "volume", "volume_id": "vol-1"},
        )
        assert resp.status_code == 202
        assert resp.json()["data"]["chapter_ids"] == ["ch-0001", "ch-0002"]

    def test_start_already_running_returns_409(
        self,
        client: TestClient,
        workspace_id: str,
        session_factory: Callable[[str], Session],
        pipeline_service: PipelineService,
    ) -> None:
        _seed_workspace_with_volume(session_factory, workspace_id)
        # 先手动 start + pause(避免 BackgroundTasks 跑完清掉 active)
        pipeline_service.start(
            workspace_id,
            scope="single",
            start_chapter_id="ch-0001",
            end_chapter_id=None,
            volume_id=None,
            profile_override=None,
        )
        pipeline_service.pause(workspace_id)
        # 现在 active state=paused,二次 :start 应 409
        resp = client.post(
            "/api/v1/pipeline:start",
            headers={"X-Workspace-Id": workspace_id},
            json={"scope": "single", "start_chapter_id": "ch-0002"},
        )
        assert resp.status_code == 409
        err = resp.json()["error"]
        assert err["code"] == "PIPELINE_ALREADY_RUNNING"
        assert err["details"]["workspace_id"] == workspace_id
        assert err["details"]["active_state"] == "paused"

    def test_start_invalid_scope_range_without_end_returns_422(
        self,
        client: TestClient,
        workspace_id: str,
        session_factory: Callable[[str], Session],
    ) -> None:
        _seed_workspace_with_volume(session_factory, workspace_id)
        resp = client.post(
            "/api/v1/pipeline:start",
            headers={"X-Workspace-Id": workspace_id},
            json={"scope": "range", "start_chapter_id": "ch-0001"},
        )
        assert resp.status_code == 422
        err = resp.json()["error"]
        assert err["code"] == "PIPELINE_INVALID_SCOPE"
        assert err["details"]["scope"] == "range"

    def test_start_invalid_scope_volume_missing_id_returns_422(
        self,
        client: TestClient,
        workspace_id: str,
        session_factory: Callable[[str], Session],
    ) -> None:
        _seed_workspace_with_volume(session_factory, workspace_id)
        resp = client.post(
            "/api/v1/pipeline:start",
            headers={"X-Workspace-Id": workspace_id},
            json={"scope": "volume"},
        )
        assert resp.status_code == 422
        assert resp.json()["error"]["code"] == "PIPELINE_INVALID_SCOPE"

    def test_start_missing_workspace_header_returns_422(
        self, client: TestClient
    ) -> None:
        resp = client.post(
            "/api/v1/pipeline:start",
            json={"scope": "single", "start_chapter_id": "ch-0001"},
        )
        assert resp.status_code == 422
        assert resp.json()["error"]["code"] == "MISSING_WORKSPACE_HEADER"

    def test_start_unknown_scope_returns_422_validation(
        self, client: TestClient, workspace_id: str
    ) -> None:
        """Pydantic Literal 校验 — 未知 scope 值返 VALIDATION_FAILED 422。"""
        resp = client.post(
            "/api/v1/pipeline:start",
            headers={"X-Workspace-Id": workspace_id},
            json={"scope": "unknown_scope"},
        )
        assert resp.status_code == 422
        assert resp.json()["error"]["code"] == "VALIDATION_FAILED"


# ---- :pause / :resume / :stop happy + error -------------------------------


class TestPauseResumeStopRoute:
    def test_pause_happy_path_after_partial_run(
        self,
        client: TestClient,
        workspace_id: str,
        session_factory: Callable[[str], Session],
        pipeline_service: PipelineService,
        fake_ws_manager: _FakeWSManager,
    ) -> None:
        """先 service 直接 start(不经路由,跳过 BackgroundTasks),再 :pause 路由
        端点。"""
        _seed_workspace_with_volume(session_factory, workspace_id)
        pipeline_service.start(
            workspace_id,
            scope="single",
            start_chapter_id="ch-0001",
            end_chapter_id=None,
            volume_id=None,
            profile_override=None,
        )
        resp = client.post(
            "/api/v1/pipeline:pause",
            headers={"X-Workspace-Id": workspace_id},
            json={},
        )
        assert resp.status_code == 200, resp.text
        data = resp.json()["data"]
        assert data["state"] == "paused"
        assert data["pause_reason"] == "user_request"
        assert any(e["event"] == "pipeline_paused" for e in fake_ws_manager.events)

    def test_pause_without_active_returns_409(
        self, client: TestClient, workspace_id: str
    ) -> None:
        resp = client.post(
            "/api/v1/pipeline:pause",
            headers={"X-Workspace-Id": workspace_id},
            json={},
        )
        assert resp.status_code == 409
        err = resp.json()["error"]
        assert err["code"] == "PIPELINE_NOT_RUNNING"
        assert err["details"]["action"] == "pause"

    def test_resume_after_pause_continues_execution(
        self,
        client: TestClient,
        workspace_id: str,
        session_factory: Callable[[str], Session],
        pipeline_service: PipelineService,
        fake_chapter_service: _FakeChapterService,
        fake_ws_manager: _FakeWSManager,
        monkeypatch: pytest.MonkeyPatch,
    ) -> None:
        _seed_workspace_with_volume(session_factory, workspace_id, chapter_count=3)
        pipeline_service.start(
            workspace_id,
            scope="range",
            start_chapter_id="ch-0001",
            end_chapter_id="ch-0003",
            volume_id=None,
            profile_override=None,
        )
        pipeline_service.pause(workspace_id)
        execute_flags: list[bool] = []
        real_execute = pipeline_service.execute

        def _tracked_execute(
            tracked_workspace_id: str,
            tracked_run_id: str,
            *,
            explicit_resume: bool = False,
        ) -> None:
            execute_flags.append(explicit_resume)
            real_execute(
                tracked_workspace_id,
                tracked_run_id,
                explicit_resume=explicit_resume,
            )

        monkeypatch.setattr(pipeline_service, "execute", _tracked_execute)
        # :resume 触发 BackgroundTasks 重启 execute → 应跑完 3 章
        resp = client.post(
            "/api/v1/pipeline:resume",
            headers={"X-Workspace-Id": workspace_id},
            json={},
        )
        assert resp.status_code == 200, resp.text
        # 注意:resume 路由返回 snapshot 时 state=running(snapshot 在
        # BackgroundTasks 跑完前生成),但 TestClient 退出 with 块前
        # BackgroundTasks 已跑完,后续 status 调用应见 stopped。
        # 校验所有 3 章已被 write
        assert [c[1] for c in fake_chapter_service.calls] == [
            "ch-0001",
            "ch-0002",
            "ch-0003",
        ]
        assert execute_flags == [True]
        # WS event:pipeline_resumed 已 publish
        assert any(e["event"] == "pipeline_resumed" for e in fake_ws_manager.events)

    def test_resume_without_active_returns_409(
        self, client: TestClient, workspace_id: str
    ) -> None:
        resp = client.post(
            "/api/v1/pipeline:resume",
            headers={"X-Workspace-Id": workspace_id},
            json={},
        )
        assert resp.status_code == 409
        assert resp.json()["error"]["code"] == "PIPELINE_NOT_RUNNING"

    def test_resume_recoverable_continuity_error_restarts_same_run(
        self,
        client: TestClient,
        workspace_id: str,
        session_factory: Callable[[str], Session],
        pipeline_service: PipelineService,
        fake_chapter_service: _FakeChapterService,
    ) -> None:
        """特定 error 经既有 :resume 路由重启 BackgroundTask，不是假恢复。"""
        _seed_workspace_with_volume(session_factory, workspace_id, chapter_count=1)
        started = pipeline_service.start(
            workspace_id,
            scope="single",
            start_chapter_id="ch-0001",
            end_chapter_id=None,
            volume_id=None,
            profile_override=None,
        )
        session = session_factory(workspace_id)
        try:
            row = session.get(PipelineRunORM, started.run_id)
            assert row is not None
            row.state = "error"
            row.error_details = {
                "error_type": "ContinuityLedgerNotReadyError",
                "message": "quote invalid",
                "chapter_id": "ch-0001",
            }
            session.commit()
        finally:
            session.close()

        resp = client.post(
            "/api/v1/pipeline:resume",
            headers={"X-Workspace-Id": workspace_id},
            json={},
        )

        assert resp.status_code == 200, resp.text
        assert resp.json()["data"]["run_id"] == started.run_id
        assert [call[1] for call in fake_chapter_service.calls] == ["ch-0001"]
        final = pipeline_service.status(workspace_id)
        assert final.run_id == started.run_id
        assert final.state == "stopped"
        assert final.pause_reason == "completed"

    def test_resume_repaired_card_error_restarts_same_run(
        self,
        client: TestClient,
        workspace_service: WorkspaceService,
        workspace_id: str,
        session_factory: Callable[[str], Session],
        pipeline_service: PipelineService,
        fake_chapter_service: _FakeChapterService,
    ) -> None:
        """产品批准修复卡片后，:resume 重启原 run 而不新建 run。"""

        _seed_workspace_with_volume(session_factory, workspace_id, chapter_count=1)
        started = pipeline_service.start(
            workspace_id,
            scope="single",
            start_chapter_id="ch-0001",
            end_chapter_id=None,
            volume_id=None,
            profile_override=None,
        )
        now = datetime.now(timezone.utc)
        session = session_factory(workspace_id)
        try:
            row = session.get(PipelineRunORM, started.run_id)
            assert row is not None
            row.state = "error"
            row.current_chapter = "ch-0001"
            row.error_details = {
                "reason": "card_generation_failed",
                "message": "card contract ambiguous",
                "chapter_id": "ch-0001",
            }
            session.add(
                ChapterCardORM(
                    chapter_id="ch-0001",
                    workspace_id=workspace_id,
                    card_status="approved",
                    card_approved_at=now,
                    created_at=now,
                    rejection_notes=None,
                )
            )
            session.commit()
        finally:
            session.close()
        card_path = (
            workspace_service.workspaces_root
            / workspace_id
            / "data"
            / "cards"
            / "ch-0001.json"
        )
        card_path.write_text("{}", encoding="utf-8")

        response = client.post(
            "/api/v1/pipeline:resume",
            headers={"X-Workspace-Id": workspace_id},
            json={},
        )

        assert response.status_code == 200, response.text
        assert response.json()["data"]["run_id"] == started.run_id
        assert [call[1] for call in fake_chapter_service.calls] == ["ch-0001"]
        assert pipeline_service.status(workspace_id).run_id == started.run_id

    def test_second_resume_while_running_does_not_launch_duplicate_worker(
        self,
        client: TestClient,
        workspace_id: str,
        session_factory: Callable[[str], Session],
        pipeline_service: PipelineService,
        monkeypatch: pytest.MonkeyPatch,
    ) -> None:
        """running → running 幂等响应不得重复启动后台 execute。"""
        _seed_workspace_with_volume(session_factory, workspace_id, chapter_count=1)
        started = pipeline_service.start(
            workspace_id,
            scope="single",
            start_chapter_id="ch-0001",
            end_chapter_id=None,
            volume_id=None,
            profile_override=None,
        )
        execute_calls: list[tuple[str, str]] = []

        def _execute_spy(target_workspace_id: str, run_id: str) -> None:
            execute_calls.append((target_workspace_id, run_id))

        monkeypatch.setattr(pipeline_service, "execute", _execute_spy)

        def _status_must_not_run(_workspace_id: str):
            raise AssertionError("resume route 不得先 status，避免 TOCTOU")

        monkeypatch.setattr(pipeline_service, "status", _status_must_not_run)

        resp = client.post(
            "/api/v1/pipeline:resume",
            headers={"X-Workspace-Id": workspace_id},
            json={},
        )

        assert resp.status_code == 200, resp.text
        assert resp.json()["data"]["run_id"] == started.run_id
        assert resp.json()["data"]["state"] == "running"
        assert execute_calls == []

    def test_stop_happy_path(
        self,
        client: TestClient,
        workspace_id: str,
        session_factory: Callable[[str], Session],
        pipeline_service: PipelineService,
        fake_ws_manager: _FakeWSManager,
    ) -> None:
        _seed_workspace_with_volume(session_factory, workspace_id)
        pipeline_service.start(
            workspace_id,
            scope="single",
            start_chapter_id="ch-0001",
            end_chapter_id=None,
            volume_id=None,
            profile_override=None,
        )
        resp = client.post(
            "/api/v1/pipeline:stop",
            headers={"X-Workspace-Id": workspace_id},
            json={},
        )
        assert resp.status_code == 200, resp.text
        data = resp.json()["data"]
        assert data["state"] == "stopped"
        assert any(e["event"] == "pipeline_stopped" for e in fake_ws_manager.events)

    def test_stop_without_active_returns_409(
        self, client: TestClient, workspace_id: str
    ) -> None:
        resp = client.post(
            "/api/v1/pipeline:stop",
            headers={"X-Workspace-Id": workspace_id},
            json={},
        )
        assert resp.status_code == 409
        assert resp.json()["error"]["code"] == "PIPELINE_NOT_RUNNING"


# ---- GET /status ----------------------------------------------------------


class TestStatusRoute:
    def test_status_idle_when_no_run(
        self, client: TestClient, workspace_id: str
    ) -> None:
        resp = client.get(
            "/api/v1/pipeline/status",
            headers={"X-Workspace-Id": workspace_id},
        )
        assert resp.status_code == 200
        data = resp.json()["data"]
        assert data["state"] == "idle"
        assert data["run_id"] is None
        assert data["scope"] is None
        assert data["chapter_ids"] == []
        assert data["progress"] == 0.0
        assert data["attempt_usage_cost"] == {
            "usage_known_attempts": 0,
            "usage_unknown_attempts": 0,
            "legacy_unclassified_attempts": 0,
            "input_tokens_known": 0,
            "output_tokens_known": 0,
            "provider_request_id_count": 0,
            "response_status_counts": {},
            "configured_cost_basis_counts": {},
            "configured_estimates": [],
            "actual_billed": {
                "status": "not_recorded",
                "amount": None,
                "currency": None,
            },
        }

    def test_status_after_running_run(
        self,
        client: TestClient,
        workspace_id: str,
        session_factory: Callable[[str], Session],
        pipeline_service: PipelineService,
    ) -> None:
        _seed_workspace_with_volume(session_factory, workspace_id)
        started = pipeline_service.start(
            workspace_id,
            scope="single",
            start_chapter_id="ch-0001",
            end_chapter_id=None,
            volume_id=None,
            profile_override=None,
        )
        now = datetime.now(timezone.utc)
        with session_factory(workspace_id) as session:
            run = session.get(PipelineRunORM, started.run_id)
            assert run is not None
            run.provider_call_count = 2
            session.add_all(
                [
                    AttemptLedgerORM(
                        attempt_id="att-known",
                        workspace_id=workspace_id,
                        run_id=started.run_id,
                        task_id="task-1",
                        chapter_id="ch-0001",
                        attempt_kind="provider",
                        operation_kind="agent_call",
                        logical_call_id="call-1",
                        agent_name="writer",
                        provider="openai_compatible",
                        model="qwen3.8-max",
                        attempt_number=1,
                        attempt_cap=1000,
                        status="succeeded",
                        version_before=None,
                        version_after=None,
                        error_type=None,
                        created_at=now,
                        completed_at=now,
                        usage_status="known",
                        input_tokens=100,
                        output_tokens=20,
                        reserved_input_tokens=500,
                        reserved_output_tokens=100,
                        configured_cost_currency="RMB",
                        configured_input_cost_per_1k=0.012,
                        configured_output_cost_per_1k=0.036,
                        configured_cost_amount=0.00192,
                        configured_cost_basis="metered_usage",
                        provider_request_id="req-known",
                        response_status="completed",
                    ),
                    AttemptLedgerORM(
                        attempt_id="att-unknown",
                        workspace_id=workspace_id,
                        run_id=started.run_id,
                        task_id="task-1",
                        chapter_id="ch-0001",
                        attempt_kind="provider",
                        operation_kind="agent_call",
                        logical_call_id="call-2",
                        agent_name="reviewer",
                        provider="openai_compatible",
                        model="qwen3.8-max",
                        attempt_number=1,
                        attempt_cap=1000,
                        status="failed",
                        version_before=None,
                        version_after=None,
                        error_type="ProviderUsageMissingError",
                        created_at=now,
                        completed_at=now,
                        usage_status="unknown",
                        input_tokens=None,
                        output_tokens=None,
                        reserved_input_tokens=500,
                        reserved_output_tokens=100,
                        configured_cost_currency="RMB",
                        configured_input_cost_per_1k=0.012,
                        configured_output_cost_per_1k=0.036,
                        configured_cost_amount=0.0096,
                        configured_cost_basis="conservative_reserve",
                        provider_request_id=None,
                        response_status="usage_missing",
                    ),
                ]
            )
            session.commit()
        resp = client.get(
            "/api/v1/pipeline/status",
            headers={"X-Workspace-Id": workspace_id},
        )
        assert resp.status_code == 200
        data = resp.json()["data"]
        assert data["state"] == "running"
        assert data["run_id"].startswith("run_")
        assert data["chapter_ids"] == ["ch-0001"]
        assert data["provider_call_cap"] == 1000
        assert data["provider_call_count"] == 2
        assert data["transport_retry_count"] == 0
        assert data["pipeline_attempt_count"] == 0
        assert data["upstream_generation_attempt_count"] == 0
        usage_cost = data["attempt_usage_cost"]
        assert usage_cost["usage_known_attempts"] == 1
        assert usage_cost["usage_unknown_attempts"] == 1
        assert usage_cost["legacy_unclassified_attempts"] == 0
        assert usage_cost["input_tokens_known"] == 100
        assert usage_cost["output_tokens_known"] == 20
        assert usage_cost["provider_request_id_count"] == 1
        assert usage_cost["response_status_counts"] == {
            "completed": 1,
            "usage_missing": 1,
        }
        assert usage_cost["configured_cost_basis_counts"] == {
            "conservative_reserve": 1,
            "metered_usage": 1,
        }
        assert usage_cost["configured_estimates"] == [
            {"currency": "RMB", "amount": pytest.approx(0.01152)}
        ]
        assert usage_cost["actual_billed"] == {
            "status": "not_recorded",
            "amount": None,
            "currency": None,
        }


# ---- Invalidated gate 集成测 -----------------------------------------------


class TestInvalidatedGateIntegration:
    def test_pipeline_auto_pauses_when_chapter_invalidated(
        self,
        client: TestClient,
        workspace_id: str,
        workspace_service: WorkspaceService,
        session_factory: Callable[[str], Session],
        fake_ws_manager: _FakeWSManager,
    ) -> None:
        """plan §10 gate 类:pipeline 跑过程中 chapter:write 撞 invalidated
        gate → 自动 pause + reason=chapter_invalidated + 后续章节不跑。"""
        _seed_workspace_with_volume(session_factory, workspace_id, chapter_count=3)
        # 重新构造 PipelineService 注入 ch-0002 撞 invalidated
        fake_chapter = _FakeChapterService(
            raise_on={
                "ch-0002": ChapterInvalidatedNeedsUnblockError(
                    chapter_id="ch-0002", workspace_id=workspace_id
                )
            }
        )
        svc = PipelineService(
            chapter_service=fake_chapter,  # type: ignore[arg-type]
            workspace_service=workspace_service,
            session_factory=session_factory,
            ws_manager=fake_ws_manager,  # type: ignore[arg-type]
        )
        original_override = client.app.dependency_overrides[get_pipeline_service]
        client.app.dependency_overrides[get_pipeline_service] = lambda: svc
        try:
            resp = client.post(
                "/api/v1/pipeline:start",
                headers={"X-Workspace-Id": workspace_id},
                json={
                    "scope": "range",
                    "start_chapter_id": "ch-0001",
                    "end_chapter_id": "ch-0003",
                },
            )
            assert resp.status_code == 202
            # BackgroundTasks 已跑完;ch-0001 成功,ch-0002 撞 invalidated 后停
            # ch-0003 未跑
            assert [call[1] for call in fake_chapter.calls] == [
                "ch-0001",
                "ch-0002",
            ]

            # status 应翻 paused
            status_resp = client.get(
                "/api/v1/pipeline/status",
                headers={"X-Workspace-Id": workspace_id},
            )
            status_data = status_resp.json()["data"]
            assert status_data["state"] == "paused"
            assert status_data["pause_reason"] == "chapter_invalidated"
            assert status_data["completed_chapter_ids"] == ["ch-0001"]
        finally:
            client.app.dependency_overrides[get_pipeline_service] = original_override

        # WS event 应见 pipeline_paused + data.reason=chapter_invalidated
        paused = [e for e in fake_ws_manager.events if e["event"] == "pipeline_paused"]
        assert paused
        assert paused[-1]["data"]["reason"] == "chapter_invalidated"
        assert paused[-1]["data"]["chapter_id"] == "ch-0002"


# ---- v2 w1-s1:failure_policy 透传 + skipped 暴露 ---------------------------


class TestFailurePolicyRoutes:
    """v2 w1-s1::start 请求体 ``failure_policy`` 透传 + status 暴露
    ``failure_policy`` / ``skipped_chapter_ids``(防回退:接线断在路由层
    时 mock service 测照不出,本类直接打端点)。"""

    def test_start_passes_failure_policy_to_service_and_echoes(
        self,
        client: TestClient,
        workspace_id: str,
        session_factory: Callable[[str], Session],
    ) -> None:
        _seed_workspace_with_volume(session_factory, workspace_id, chapter_count=3)
        resp = client.post(
            "/api/v1/pipeline:start",
            headers={"X-Workspace-Id": workspace_id},
            json={
                "scope": "single",
                "start_chapter_id": "ch-0001",
                "failure_policy": "skip",
            },
        )
        assert resp.status_code == 202
        data = resp.json()["data"]
        assert data["failure_policy"] == "skip"

        status_resp = client.get(
            "/api/v1/pipeline/status",
            headers={"X-Workspace-Id": workspace_id},
        )
        status_data = status_resp.json()["data"]
        assert status_data["failure_policy"] == "skip"
        assert status_data["skipped_chapter_ids"] == []

    def test_start_default_failure_policy_is_stop(
        self,
        client: TestClient,
        workspace_id: str,
        session_factory: Callable[[str], Session],
    ) -> None:
        _seed_workspace_with_volume(session_factory, workspace_id, chapter_count=3)
        resp = client.post(
            "/api/v1/pipeline:start",
            headers={"X-Workspace-Id": workspace_id},
            json={"scope": "single", "start_chapter_id": "ch-0001"},
        )
        assert resp.status_code == 202
        assert resp.json()["data"]["failure_policy"] == "stop"

    def test_start_bogus_failure_policy_returns_422(
        self,
        client: TestClient,
        workspace_id: str,
        session_factory: Callable[[str], Session],
    ) -> None:
        _seed_workspace_with_volume(session_factory, workspace_id, chapter_count=3)
        resp = client.post(
            "/api/v1/pipeline:start",
            headers={"X-Workspace-Id": workspace_id},
            json={
                "scope": "single",
                "start_chapter_id": "ch-0001",
                "failure_policy": "bogus",
            },
        )
        assert resp.status_code == 422

    def test_idle_status_exposes_default_policy_fields(
        self,
        client: TestClient,
        workspace_id: str,
    ) -> None:
        resp = client.get(
            "/api/v1/pipeline/status",
            headers={"X-Workspace-Id": workspace_id},
        )
        data = resp.json()["data"]
        assert data["state"] == "idle"
        assert data["failure_policy"] == "stop"
        assert data["skipped_chapter_ids"] == []


# ---- :resume interrupted 断点续跑(v2 w1-s2) --------------------------------


class TestInterruptedResumeRoute:
    """interrupted run 经 ``:resume`` 端点续跑 — 路由层 BackgroundTasks
    重启条件(``prev_state in ("paused", "interrupted")``)防回退:只翻
    service 态不重启循环 = 假 resume(状态 running 但无人跑)。"""

    def test_resume_interrupted_restarts_execution(
        self,
        client: TestClient,
        workspace_id: str,
        session_factory: Callable[[str], Session],
        pipeline_service: PipelineService,
        fake_chapter_service: _FakeChapterService,
        fake_ws_manager: _FakeWSManager,
    ) -> None:
        _seed_workspace_with_volume(session_factory, workspace_id, chapter_count=3)
        snap = pipeline_service.start(
            workspace_id,
            scope="range",
            start_chapter_id="ch-0001",
            end_chapter_id="ch-0003",
            volume_id=None,
            profile_override=None,
        )
        # kill-restart 模拟:行残留 running → lifespan hook 翻 interrupted
        pipeline_service.mark_running_as_interrupted_on_startup([workspace_id])
        assert pipeline_service.status(workspace_id).state == "interrupted"

        # :resume 触发 BackgroundTasks 重启 execute → 应跑完 3 章
        resp = client.post(
            "/api/v1/pipeline:resume",
            headers={"X-Workspace-Id": workspace_id},
            json={},
        )
        assert resp.status_code == 200, resp.text
        assert [c[1] for c in fake_chapter_service.calls] == [
            "ch-0001",
            "ch-0002",
            "ch-0003",
        ]
        # 同 run 续跑(不是新 run)+ 自然完成
        final = pipeline_service.status(workspace_id)
        assert final.run_id == snap.run_id
        assert final.state == "stopped"
        assert final.pause_reason == "completed"
        assert any(e["event"] == "pipeline_resumed" for e in fake_ws_manager.events)
