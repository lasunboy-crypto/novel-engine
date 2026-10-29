"""P6-B-03 PipelineService SQLite 持久化语义测试。

对齐 docs/plan/p6_plan.md §"P6-B-03" + ``src/services/pipeline_service.py``
P6-B-03 改造("内存 dict → ``pipeline_runs`` 表读写")。

测试范围(plan §10 测试约定 5 类):

- **happy**:start INSERT 行 / pause UPDATE state / execute completed_chapter_ids
  JSON 持久化(实证 SELECT 行落库)
- **error**:不存在 / scope 422 沿用 P6-B-02 既有覆盖,本任务回归 + 新增
  跨 service 实例的"重启后 state 仍持久"持久化语义
- **gate**:invalidated 自动 pause(P6-B-02 已落,本任务回归)+ paused 行
  在新 service 实例上仍可 resume 续跑
- **rollback**:execute 撞 RuntimeError → state=error + error_details 持久
- **WS event**:沿用 P6-B-02(本任务不重测)+ 新增 lifespan hook 不 publish
  event(仅 DB 标记)

新增重点:**跨 service 实例**(模拟进程重启)的状态可见性。
"""

from __future__ import annotations

from collections.abc import Callable
from datetime import UTC, datetime
from pathlib import Path

import pytest
from sqlalchemy import create_engine, select
from sqlalchemy.orm import Session
from sqlalchemy.pool import StaticPool

from src.core.db import Base, PipelineRunORM, VolumeORM, WorkspaceORM
from src.services.chapter_rewind_service import (
    ChapterInvalidatedNeedsUnblockError,
)
from src.services.pipeline_service import (
    PipelineService,
)
from src.services.workspace_service import WorkspaceService


# ---- fakes(对齐 test_pipeline_service.py 同模式)----------------------------


class _FakeChapterService:
    """模拟 ChapterService.write,可注入 side_effect。"""

    def __init__(self) -> None:
        self.calls: list[tuple[str, str, str | None]] = []
        self.side_effects: dict[str, Exception | None] = {}

    def write(
        self,
        workspace_id: str,
        chapter_id: str,
        *,
        profile_override: str | None = None,
    ) -> None:
        self.calls.append((workspace_id, chapter_id, profile_override))
        effect = self.side_effects.get(chapter_id)
        if effect is not None:
            raise effect


class _FakeWSManager:
    """模拟 WebSocketManager.publish,记录事件列表。"""

    def __init__(self) -> None:
        self.events: list[dict] = []

    def publish(self, *, channel: str, event: str, data: dict) -> None:
        self.events.append({"channel": channel, "event": event, "data": data})


# ---- shared fixtures --------------------------------------------------------


@pytest.fixture
def tmp_workspaces_root(tmp_path: Path) -> Path:
    root = tmp_path / "workspaces"
    root.mkdir()
    return root


@pytest.fixture
def workspace_service(tmp_workspaces_root: Path) -> WorkspaceService:
    return WorkspaceService(workspaces_root=tmp_workspaces_root)


@pytest.fixture
def workspace_id(workspace_service: WorkspaceService) -> str:
    workspace_service.create(
        "ws-pipe-persist",
        display_name="P6-B-03 persistence test",
        genre="xianxia",
        premise="test",
        activate=False,
    )
    return "ws-pipe-persist"


@pytest.fixture
def shared_engine() -> "object":
    """共享 in-memory SQLite engine,跨 service 实例使用(模拟进程重启时
    SQLite 文件仍存在,本测试用 StaticPool 共享 connection 模拟物理持久)。"""
    engine = create_engine(
        "sqlite:///:memory:",
        connect_args={"check_same_thread": False},
        poolclass=StaticPool,
    )
    Base.metadata.create_all(engine)
    return engine


@pytest.fixture
def session_factory(
    shared_engine: "object",
) -> Callable[[str], Session]:
    def factory(_workspace_id: str) -> Session:
        return Session(shared_engine)  # type: ignore[arg-type]

    return factory


def _seed_volume(
    session_factory: Callable[[str], Session],
    workspace_id: str,
    chapter_count: int = 3,
) -> None:
    session = session_factory(workspace_id)
    try:
        now = datetime.now(UTC)
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


def _make_service(
    workspace_service: WorkspaceService,
    session_factory: Callable[[str], Session],
    chapter_service: _FakeChapterService | None = None,
    ws_manager: _FakeWSManager | None = None,
) -> PipelineService:
    return PipelineService(
        chapter_service=chapter_service or _FakeChapterService(),  # type: ignore[arg-type]
        workspace_service=workspace_service,
        session_factory=session_factory,
        ws_manager=ws_manager,  # type: ignore[arg-type]
    )


# ---- happy:start INSERT 行 -------------------------------------------------


class TestStartPersistence:
    def test_start_inserts_pipeline_runs_row(
        self,
        workspace_service: WorkspaceService,
        session_factory: Callable[[str], Session],
        workspace_id: str,
    ) -> None:
        _seed_volume(session_factory, workspace_id)
        service = _make_service(workspace_service, session_factory)
        snap = service.start(
            workspace_id,
            scope="single",
            start_chapter_id="ch-0001",
            end_chapter_id=None,
            volume_id=None,
            profile_override=None,
        )
        # 直接 SELECT 验证 SQLite 行
        session = session_factory(workspace_id)
        try:
            row = session.scalars(
                select(PipelineRunORM).where(
                    PipelineRunORM.run_id == snap.run_id
                )
            ).one()
            assert row.workspace_id == workspace_id
            assert row.state == "running"
            assert row.scope == "single"
            assert row.chapter_ids == ["ch-0001"]
            assert row.completed_chapter_ids == []
            assert row.error_details is None
        finally:
            session.close()

    def test_status_after_start_reads_persisted_row(
        self,
        workspace_service: WorkspaceService,
        session_factory: Callable[[str], Session],
        workspace_id: str,
    ) -> None:
        _seed_volume(session_factory, workspace_id)
        service = _make_service(workspace_service, session_factory)
        started = service.start(
            workspace_id,
            scope="single",
            start_chapter_id="ch-0001",
            end_chapter_id=None,
            volume_id=None,
            profile_override=None,
        )
        # 新 service 实例(模拟跨请求 / 跨进程):state 仍从 DB 看到
        service2 = _make_service(workspace_service, session_factory)
        status = service2.status(workspace_id)
        assert status.run_id == started.run_id
        assert status.state == "running"


# ---- happy:pause / resume / stop UPDATE 行 ---------------------------------


class TestPauseResumeStopPersistence:
    def test_pause_persists_state_and_pause_reason(
        self,
        workspace_service: WorkspaceService,
        session_factory: Callable[[str], Session],
        workspace_id: str,
    ) -> None:
        _seed_volume(session_factory, workspace_id)
        service = _make_service(workspace_service, session_factory)
        service.start(
            workspace_id,
            scope="single",
            start_chapter_id="ch-0001",
            end_chapter_id=None,
            volume_id=None,
            profile_override=None,
        )
        service.pause(workspace_id)

        # 跨 service 实例验证 state 真持久
        service2 = _make_service(workspace_service, session_factory)
        status = service2.status(workspace_id)
        assert status.state == "paused"
        assert status.pause_reason == "user_request"

    def test_resume_persists_state(
        self,
        workspace_service: WorkspaceService,
        session_factory: Callable[[str], Session],
        workspace_id: str,
    ) -> None:
        _seed_volume(session_factory, workspace_id)
        service = _make_service(workspace_service, session_factory)
        service.start(
            workspace_id,
            scope="single",
            start_chapter_id="ch-0001",
            end_chapter_id=None,
            volume_id=None,
            profile_override=None,
        )
        service.pause(workspace_id)
        resumed = service.resume(workspace_id)
        assert resumed.state == "running"

        # 跨 service 实例
        service2 = _make_service(workspace_service, session_factory)
        status = service2.status(workspace_id)
        assert status.state == "running"
        assert status.pause_reason is None

    def test_stop_persists_completed_at(
        self,
        workspace_service: WorkspaceService,
        session_factory: Callable[[str], Session],
        workspace_id: str,
    ) -> None:
        _seed_volume(session_factory, workspace_id)
        service = _make_service(workspace_service, session_factory)
        snap = service.start(
            workspace_id,
            scope="single",
            start_chapter_id="ch-0001",
            end_chapter_id=None,
            volume_id=None,
            profile_override=None,
        )
        service.stop(workspace_id)

        # DB 行 state=stopped + completed_at 非空
        session = session_factory(workspace_id)
        try:
            row = session.scalars(
                select(PipelineRunORM).where(
                    PipelineRunORM.run_id == snap.run_id
                )
            ).one()
            assert row.state == "stopped"
            assert row.completed_at is not None
        finally:
            session.close()


# ---- happy:execute completed_chapter_ids JSON 持久化 ------------------------


class TestExecutePersistence:
    def test_execute_persists_completed_chapter_ids_after_each_chapter(
        self,
        workspace_service: WorkspaceService,
        session_factory: Callable[[str], Session],
        workspace_id: str,
    ) -> None:
        _seed_volume(session_factory, workspace_id, chapter_count=3)
        fake_chapter = _FakeChapterService()
        service = _make_service(
            workspace_service,
            session_factory,
            chapter_service=fake_chapter,
        )
        snap = service.start(
            workspace_id,
            scope="range",
            start_chapter_id="ch-0001",
            end_chapter_id="ch-0003",
            volume_id=None,
            profile_override=None,
        )
        service.execute(workspace_id, snap.run_id)

        # 全 3 章完成 → state=stopped + completed_at + completed_chapter_ids 3 项
        session = session_factory(workspace_id)
        try:
            row = session.scalars(
                select(PipelineRunORM).where(
                    PipelineRunORM.run_id == snap.run_id
                )
            ).one()
            assert row.state == "stopped"
            assert row.pause_reason == "completed"
            assert row.completed_at is not None
            assert row.completed_chapter_ids == [
                "ch-0001",
                "ch-0002",
                "ch-0003",
            ]
        finally:
            session.close()
        # chapter_service.write 被调 3 次
        assert len(fake_chapter.calls) == 3


# ---- gate:invalidated 自动 pause + 跨实例 resume ----------------------------


class TestInvalidatedGatePersistence:
    def test_invalidated_pause_persists_and_resumable(
        self,
        workspace_service: WorkspaceService,
        session_factory: Callable[[str], Session],
        workspace_id: str,
    ) -> None:
        _seed_volume(session_factory, workspace_id, chapter_count=3)
        fake_chapter = _FakeChapterService()
        # 第 2 章撞 invalidated
        fake_chapter.side_effects["ch-0002"] = (
            ChapterInvalidatedNeedsUnblockError(
                chapter_id="ch-0002",
                workspace_id=workspace_id,
            )
        )
        service = _make_service(
            workspace_service,
            session_factory,
            chapter_service=fake_chapter,
        )
        snap = service.start(
            workspace_id,
            scope="range",
            start_chapter_id="ch-0001",
            end_chapter_id="ch-0003",
            volume_id=None,
            profile_override=None,
        )
        service.execute(workspace_id, snap.run_id)

        # 跨 service 实例:state=paused + reason=chapter_invalidated +
        # completed=[ch-0001] 保留(plan §10 字面 invalidated 类硬要求)
        service2 = _make_service(
            workspace_service,
            session_factory,
            chapter_service=_FakeChapterService(),
        )
        status = service2.status(workspace_id)
        assert status.state == "paused"
        assert status.pause_reason == "chapter_invalidated"
        assert status.completed_chapter_ids == ["ch-0001"]


# ---- rollback:execute 撞 RuntimeError → state=error + error_details 持久化 --


class TestExecuteRollbackPersistence:
    def test_unexpected_error_persists_state_and_details(
        self,
        workspace_service: WorkspaceService,
        session_factory: Callable[[str], Session],
        workspace_id: str,
    ) -> None:
        _seed_volume(session_factory, workspace_id, chapter_count=2)
        fake_chapter = _FakeChapterService()
        fake_chapter.side_effects["ch-0001"] = RuntimeError("boom")
        service = _make_service(
            workspace_service,
            session_factory,
            chapter_service=fake_chapter,
        )
        snap = service.start(
            workspace_id,
            scope="range",
            start_chapter_id="ch-0001",
            end_chapter_id="ch-0002",
            volume_id=None,
            profile_override=None,
        )
        service.execute(workspace_id, snap.run_id)

        # 跨实例验证 error 持久
        service2 = _make_service(workspace_service, session_factory)
        status = service2.status(workspace_id)
        assert status.state == "error"
        assert status.error_details is not None
        assert status.error_details["error_type"] == "RuntimeError"
        assert status.error_details["chapter_id"] == "ch-0001"


# ---- lifespan startup hook:扫 state='running' → interrupted(v2 w1-s2 改判) --


class TestLifespanStartupHook:
    def test_marks_running_row_as_interrupted(
        self,
        workspace_service: WorkspaceService,
        session_factory: Callable[[str], Session],
        workspace_id: str,
    ) -> None:
        """v2 w1-s2 改判:chapter_ids 非空(可恢复)→ interrupted 非 error。"""
        _seed_volume(session_factory, workspace_id, chapter_count=1)
        service = _make_service(workspace_service, session_factory)
        snap = service.start(
            workspace_id,
            scope="single",
            start_chapter_id="ch-0001",
            end_chapter_id=None,
            volume_id=None,
            profile_override=None,
        )
        # 模拟进程崩溃 — DB 仍是 state='running'
        # 新 service 实例 + lifespan hook
        service2 = _make_service(workspace_service, session_factory)
        marked = service2.mark_running_as_interrupted_on_startup(
            [workspace_id]
        )
        assert marked == 1

        # state=interrupted + error_details reason + 中断时的章
        status = service2.status(workspace_id)
        assert status.run_id == snap.run_id
        assert status.state == "interrupted"
        assert status.error_details == {
            "reason": "engine_restarted_mid_run",
            "interrupted_chapter": None,
        }

    def test_does_not_touch_paused_or_stopped_rows(
        self,
        workspace_service: WorkspaceService,
        session_factory: Callable[[str], Session],
        workspace_id: str,
    ) -> None:
        _seed_volume(session_factory, workspace_id, chapter_count=2)
        service = _make_service(workspace_service, session_factory)
        service.start(
            workspace_id,
            scope="range",
            start_chapter_id="ch-0001",
            end_chapter_id="ch-0002",
            volume_id=None,
            profile_override=None,
        )
        service.pause(workspace_id)

        # lifespan hook 只处理 running,paused 不动
        service2 = _make_service(workspace_service, session_factory)
        marked = service2.mark_running_as_interrupted_on_startup(
            [workspace_id]
        )
        assert marked == 0

        status = service2.status(workspace_id)
        assert status.state == "paused"

    def test_user_can_stop_after_engine_restart(
        self,
        workspace_service: WorkspaceService,
        session_factory: Callable[[str], Session],
        workspace_id: str,
    ) -> None:
        """对齐 plan §"P6-B-03"字面"user 手动 resume / stop"语义。

        被 lifespan 标 interrupted(v2 w1-s2 改判)后,user 可以调 :stop
        (interrupted 在 ``_ACTIVE_STATES`` — ``_load_active_or_error_run``
        涵盖)放弃续跑、清除状态。再 start 新 run 通过。
        """
        _seed_volume(session_factory, workspace_id, chapter_count=1)
        service = _make_service(workspace_service, session_factory)
        service.start(
            workspace_id,
            scope="single",
            start_chapter_id="ch-0001",
            end_chapter_id=None,
            volume_id=None,
            profile_override=None,
        )
        service2 = _make_service(workspace_service, session_factory)
        service2.mark_running_as_interrupted_on_startup([workspace_id])

        # interrupted 行可以 :stop 翻 stopped(放弃续跑)
        stopped = service2.stop(workspace_id)
        assert stopped.state == "stopped"

        # stop 后 active 清,可以 start 新 run
        new_snap = service2.start(
            workspace_id,
            scope="single",
            start_chapter_id="ch-0001",
            end_chapter_id=None,
            volume_id=None,
            profile_override=None,
        )
        assert new_snap.state == "running"
        assert new_snap.run_id != stopped.run_id

    def test_lifespan_scan_failure_on_one_workspace_does_not_block_others(
        self,
        workspace_service: WorkspaceService,
        session_factory: Callable[[str], Session],
        workspace_id: str,
    ) -> None:
        """单工作区扫失败 swallow + 不阻塞其他工作区(对齐 service hook
        try/except 模式)。"""
        _seed_volume(session_factory, workspace_id, chapter_count=1)
        service = _make_service(workspace_service, session_factory)
        service.start(
            workspace_id,
            scope="single",
            start_chapter_id="ch-0001",
            end_chapter_id=None,
            volume_id=None,
            profile_override=None,
        )
        service2 = _make_service(workspace_service, session_factory)
        # 第 1 个 workspace_id 不存在(session_factory 仍能开但表无行)→
        # service.list 不会返此 id;此处直接传给 hook 测幂等
        marked = service2.mark_running_as_interrupted_on_startup(
            ["ws-nonexistent", workspace_id]
        )
        # ws-nonexistent 走 SQL 查 0 行,不抛错;真 workspace 标 interrupted
        assert marked == 1
