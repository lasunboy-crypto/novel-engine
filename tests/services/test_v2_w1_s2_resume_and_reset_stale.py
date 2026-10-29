"""断点续跑闭环 + 中断章自愈防回退测试(v2 w1-s2)。

覆盖 v2_plan §4 w1-s2 "产出的防回退护栏" 4 项:

① **interrupted 判定与 resume 续跑测**(kill-restart 模拟):running 行
   重启 → 翻 interrupted(可恢复)→ resume 从断点继续,已完成章不重写
   (闭合 v2_plan §2.2 缺口 2 — error 死路不能 resume)。
② **approved 章防重写测(治理性,governance §2.30)**:循环选章跳过
   status 已 approved/published 的章 — completed 只记本 run 进度,新 run /
   重启 resume 场景不查章真实状态会重写已过审内容。
③ **reset_stale 扫描测**:非终态(reviewing/rewriting/revising)+ 无活跃
   task 才重置;human_intervention/invalidated("等人")不动;active
   pipeline run / active tasks 对账跳过不误杀(闭合缺口 6)。
④ **lifespan hook 接线测**:``_run_memory_enrichment_startup_scan`` 只收口
   上个进程遗留的富记忆 running、不重放 Provider；随后
   ``_run_chapter_reset_stale_scan`` 逐 workspace 调 reset_stale。富记忆扫描
   任一失败会阻止启动；普通 reset_stale 仍沿用逐 workspace fail-open。

Fake 设计沿用 ``test_pipeline_run_loop_semantics.py``(w1-s1):
``_FakeChapterService.write`` 记录调用序列;卡状态种 ``ChapterCardORM``
行;章状态种 ``ChapterORM`` 行(防重写判定以 chapters 表为真相源)。
"""

from __future__ import annotations

import hashlib
from collections.abc import Callable
from datetime import datetime, timezone
from pathlib import Path
from types import SimpleNamespace
from typing import Any

import pytest
from sqlalchemy import create_engine, select
from sqlalchemy.orm import Session
from sqlalchemy.pool import StaticPool

from src.core.db import Base
from src.core.db.models import (
    AttemptLedgerORM,
    ChapterCardORM,
    ChapterORM,
    PipelineRunORM,
    VolumeORM,
    WorkspaceORM,
)
from src.core.memory.vector import VectorChapterSnapshot
from src.core.state_machine.states import ChapterState
from src.core.workspace.workspace_paths import WorkspacePaths
from src.services.chapter_rewind_service import (
    ChapterRewindService,
    ChapterRewindServiceError,
)
from src.services.pipeline_service import (
    PipelineAlreadyRunningError,
    PipelineService,
    PipelineServiceError,
)
from src.services.workspace_service import WorkspaceService


# ---- Fakes ------------------------------------------------------------------


class _FakeChapterService:
    """ChapterService stub:全部成功(``final_state=APPROVED``)+ 记录调用。

    写成功后把 ChapterORM 行翻 approved(对齐真实 write 终态副作用 —
    防重写测试里"resume 后继续写的章"会变成终态,贴近生产)。
    """

    def __init__(
        self,
        session_factory: Callable[[str], Session] | None = None,
        *,
        checkpoint_resume_for: set[str] | None = None,
    ) -> None:
        self.calls: list[str] = []
        self.checkpoint_calls: list[str] = []
        self._session_factory = session_factory
        self._checkpoint_resume_for = checkpoint_resume_for or set()

    def resume_approved_checkpoint(
        self,
        workspace_id: str,
        chapter_id: str,
        *,
        profile_override: str | None = None,
    ) -> bool:
        del profile_override
        self.checkpoint_calls.append(chapter_id)
        if chapter_id not in self._checkpoint_resume_for:
            return False
        assert self._session_factory is not None
        session = self._session_factory(workspace_id)
        try:
            row = session.get(ChapterORM, chapter_id)
            assert row is not None
            assert row.status == "approved"
            row.ready_for_next_chapter_at = datetime.now(timezone.utc)
            session.commit()
        finally:
            session.close()
        return True

    def write(
        self,
        workspace_id: str,
        chapter_id: str,
        *,
        on_state_transition=None,
        profile_override: str | None = None,
    ) -> Any:
        self.calls.append(chapter_id)
        if self._session_factory is not None:
            session = self._session_factory(workspace_id)
            try:
                row = session.get(ChapterORM, chapter_id)
                if row is not None:
                    row.status = "approved"
                    session.commit()
            finally:
                session.close()
        return SimpleNamespace(
            final_state=ChapterState.APPROVED, escalation_reason=None
        )

    def calls_for(self, chapter_id: str) -> int:
        return sum(1 for ch in self.calls if ch == chapter_id)


class _FakeWSManager:
    def __init__(self) -> None:
        self.events: list[dict[str, Any]] = []

    def publish(self, *, channel: str, event: str, data: dict[str, Any]) -> None:
        self.events.append({"channel": channel, "event": event, "data": data})

    def events_named(self, event: str) -> list[dict[str, Any]]:
        return [e for e in self.events if e["event"] == event]


class _FakeVectorStore:
    """冷启动审计 fake：只记录本章统计与严格软隔离。"""

    def __init__(
        self,
        *,
        chapter_counts: dict[str, int] | None = None,
        fail_quarantine: bool = False,
    ) -> None:
        self.chapter_counts = dict(chapter_counts or {})
        self.fail_quarantine = fail_quarantine
        self.calls: list[tuple[str, str, str]] = []

    def count_by_chapter_id(self, collection: str, *, chapter_id: str) -> int:
        self.calls.append(("count", collection, chapter_id))
        return self.chapter_counts.get(chapter_id, 0)

    def snapshot_by_chapter_id(
        self,
        collection: str,
        *,
        chapter_id: str,
    ) -> VectorChapterSnapshot:
        self.calls.append(("snapshot", collection, chapter_id))
        count = self.chapter_counts.get(chapter_id, 0)
        return VectorChapterSnapshot(
            count=count,
            digest=hashlib.sha256(f"{chapter_id}:{count}".encode()).hexdigest(),
        )

    def soft_delete_by_chapter_id(self, collection: str, *, chapter_id: str) -> int:
        self.calls.append(("soft_delete", collection, chapter_id))
        if self.fail_quarantine:
            raise RuntimeError("chroma quarantine failed(fake)")
        return self.chapter_counts.get(chapter_id, 0)


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
        "ws-w1s2",
        display_name="v2 w1-s2 resume + reset_stale test",
        genre="xianxia",
        premise="test",
        activate=False,
    )
    return "ws-w1s2"


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
def fake_ws_manager() -> _FakeWSManager:
    return _FakeWSManager()


def _seed_workspace_and_volume(
    session_factory: Callable[[str], Session],
    workspace_id: str,
    *,
    chapter_count: int = 3,
) -> None:
    """种 WorkspaceORM + 单卷 N 章(ch-0001..ch-000N)。"""
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
                title="VOL-1",
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


def _seed_chapter(
    session_factory: Callable[[str], Session],
    workspace_id: str,
    chapter_id: str,
    *,
    status: str,
    ready: bool | None = None,
) -> None:
    """种 ChapterORM 行(章状态真相源,防重写/自愈判定读此表)。"""
    session = session_factory(workspace_id)
    try:
        now = datetime.now(timezone.utc)
        is_ready = status in ("approved", "published") if ready is None else ready
        session.merge(
            ChapterORM(
                chapter_id=chapter_id,
                workspace_id=workspace_id,
                current_version=1,
                status=status,
                rewrite_count=0,
                revise_count=0,
                created_at=now,
                approved_at=now if status in ("approved", "published") else None,
                total_token_usage={},
                total_cost_usd=0.0,
                ready_for_next_chapter_at=now if is_ready else None,
            )
        )
        session.commit()
    finally:
        session.close()


def _seed_all_cards_approved(
    session_factory: Callable[[str], Session],
    workspace_id: str,
    chapter_ids: list[str],
) -> None:
    session = session_factory(workspace_id)
    try:
        for ch in chapter_ids:
            session.merge(
                ChapterCardORM(
                    chapter_id=ch,
                    workspace_id=workspace_id,
                    card_status="approved",
                    created_at=datetime.now(timezone.utc),
                )
            )
        session.commit()
    finally:
        session.close()


def _chapter_status(
    session_factory: Callable[[str], Session],
    workspace_id: str,
    chapter_id: str,
) -> str:
    session = session_factory(workspace_id)
    try:
        row = session.get(ChapterORM, chapter_id)
        assert row is not None
        return row.status
    finally:
        session.close()


def _make_pipeline(
    workspace_service: WorkspaceService,
    session_factory: Callable[[str], Session],
    chapter_service: _FakeChapterService,
    ws_manager: _FakeWSManager,
) -> PipelineService:
    return PipelineService(
        chapter_service=chapter_service,  # type: ignore[arg-type]
        workspace_service=workspace_service,
        session_factory=session_factory,
        ws_manager=ws_manager,  # type: ignore[arg-type]
    )


def _make_rewind(
    workspace_service: WorkspaceService,
    session_factory: Callable[[str], Session],
    ws_manager: _FakeWSManager | None = None,
    active_task_checker: Callable[[str], bool] | None = None,
    vector_store: _FakeVectorStore | None = None,
) -> ChapterRewindService:
    vector = vector_store or _FakeVectorStore()
    return ChapterRewindService(
        workspace_service=workspace_service,
        session_factory=session_factory,
        vector_store_factory=lambda _workspace_id: vector,
        ws_manager=ws_manager,  # type: ignore[arg-type]
        active_task_checker=active_task_checker,
    )


def _start_range_run(service: PipelineService, workspace_id: str) -> str:
    snap = service.start(
        workspace_id,
        scope="range",
        start_chapter_id="ch-0001",
        end_chapter_id="ch-0003",
        volume_id=None,
        profile_override=None,
    )
    return snap.run_id


def _simulate_crash_mid_run(
    session_factory: Callable[[str], Session],
    workspace_id: str,
    run_id: str,
    *,
    completed: list[str],
    current: str | None,
) -> None:
    """模拟"进程死时行还停在 running":手工把 run 行写成中断现场。"""
    session = session_factory(workspace_id)
    try:
        row = session.get(PipelineRunORM, run_id)
        assert row is not None
        row.state = "running"
        row.completed_chapter_ids = list(completed)
        row.current_chapter = current
        session.commit()
    finally:
        session.close()


# =============================================================================
# ① interrupted 判定与 resume 续跑(kill-restart 模拟,缺口 2)
# =============================================================================


class TestInterruptedResumeCycle:
    """running 残留行 → interrupted → resume 断点续跑(不重 start、进度不清零)。"""

    def test_kill_restart_resume_continues_from_breakpoint(
        self,
        workspace_service: WorkspaceService,
        workspace_id: str,
        session_factory: Callable[[str], Session],
        fake_ws_manager: _FakeWSManager,
    ) -> None:
        """完整 kill-restart 模拟:ch-0001 写完后崩溃 → 重启翻 interrupted
        → resume → 从 ch-0002 继续,ch-0001 不重写。"""
        _seed_workspace_and_volume(session_factory, workspace_id)
        _seed_all_cards_approved(
            session_factory, workspace_id, ["ch-0001", "ch-0002", "ch-0003"]
        )
        for ch in ("ch-0001", "ch-0002", "ch-0003"):
            _seed_chapter(session_factory, workspace_id, ch, status="drafting")

        chapter_service = _FakeChapterService(session_factory)
        service = _make_pipeline(
            workspace_service, session_factory, chapter_service, fake_ws_manager
        )
        run_id = _start_range_run(service, workspace_id)
        # 模拟:循环写完 ch-0001(completed + 章 approved)后进程死
        _simulate_crash_mid_run(
            session_factory,
            workspace_id,
            run_id,
            completed=["ch-0001"],
            current="ch-0002",
        )
        _seed_chapter(session_factory, workspace_id, "ch-0001", status="approved")

        # 重启:新 service 实例 + lifespan hook
        service2 = _make_pipeline(
            workspace_service, session_factory, chapter_service, fake_ws_manager
        )
        preflight_count = service2.mark_running_as_interrupted_on_startup(
            [workspace_id],
            apply=False,
        )
        assert preflight_count == 1
        assert service2.status(workspace_id).state == "running"
        marked = service2.mark_running_as_interrupted_on_startup([workspace_id])
        assert marked == 1
        status = service2.status(workspace_id)
        assert status.state == "interrupted"
        assert status.run_id == run_id
        assert status.error_details == {
            "reason": "engine_restarted_mid_run",
            "interrupted_chapter": "ch-0002",
        }

        # resume:同 run 续跑(不是新 run),进度保留
        snap = service2.resume(workspace_id)
        assert snap.state == "running"
        assert snap.run_id == run_id
        assert snap.completed_chapter_ids == ["ch-0001"]
        assert snap.error_details is None  # 陈旧中断原因已清

        # execute:从 ch-0002 继续,ch-0001 零重写
        service2.execute(workspace_id, run_id)
        assert chapter_service.calls_for("ch-0001") == 0
        assert chapter_service.calls_for("ch-0002") == 1
        assert chapter_service.calls_for("ch-0003") == 1
        final = service2.status(workspace_id)
        assert final.state == "stopped"
        assert final.pause_reason == "completed"
        assert final.completed_chapter_ids == ["ch-0001", "ch-0002", "ch-0003"]

    def test_interrupted_blocks_new_start(
        self,
        workspace_service: WorkspaceService,
        workspace_id: str,
        session_factory: Callable[[str], Session],
        fake_ws_manager: _FakeWSManager,
    ) -> None:
        """interrupted 行挡新 :start(须先 resume 续跑或 stop 放弃)—
        防两行可 resume 并存 → ``one_or_none`` 多行炸。"""
        _seed_workspace_and_volume(session_factory, workspace_id)
        chapter_service = _FakeChapterService()
        service = _make_pipeline(
            workspace_service, session_factory, chapter_service, fake_ws_manager
        )
        run_id = _start_range_run(service, workspace_id)
        _simulate_crash_mid_run(
            session_factory, workspace_id, run_id, completed=[], current=None
        )
        service.mark_running_as_interrupted_on_startup([workspace_id])

        with pytest.raises(PipelineAlreadyRunningError):
            _start_range_run(service, workspace_id)

    def test_interrupted_can_be_stopped_then_new_start(
        self,
        workspace_service: WorkspaceService,
        workspace_id: str,
        session_factory: Callable[[str], Session],
        fake_ws_manager: _FakeWSManager,
    ) -> None:
        """interrupted → stop 放弃续跑 → 可 start 新 run。"""
        _seed_workspace_and_volume(session_factory, workspace_id)
        chapter_service = _FakeChapterService()
        service = _make_pipeline(
            workspace_service, session_factory, chapter_service, fake_ws_manager
        )
        run_id = _start_range_run(service, workspace_id)
        _simulate_crash_mid_run(
            session_factory, workspace_id, run_id, completed=[], current=None
        )
        service.mark_running_as_interrupted_on_startup([workspace_id])

        stopped = service.stop(workspace_id)
        assert stopped.state == "stopped"
        new_run = _start_range_run(service, workspace_id)
        assert new_run != run_id

    def test_pause_on_interrupted_is_idempotent(
        self,
        workspace_service: WorkspaceService,
        workspace_id: str,
        session_factory: Callable[[str], Session],
        fake_ws_manager: _FakeWSManager,
    ) -> None:
        """pause 对 interrupted 幂等返回(已不在跑,保留语义标记不翻 paused)。"""
        _seed_workspace_and_volume(session_factory, workspace_id)
        chapter_service = _FakeChapterService()
        service = _make_pipeline(
            workspace_service, session_factory, chapter_service, fake_ws_manager
        )
        run_id = _start_range_run(service, workspace_id)
        _simulate_crash_mid_run(
            session_factory, workspace_id, run_id, completed=[], current=None
        )
        service.mark_running_as_interrupted_on_startup([workspace_id])

        snap = service.pause(workspace_id)
        assert snap.state == "interrupted"

    def test_empty_chapter_ids_falls_back_to_error(
        self,
        workspace_service: WorkspaceService,
        workspace_id: str,
        session_factory: Callable[[str], Session],
        fake_ws_manager: _FakeWSManager,
    ) -> None:
        """``chapter_ids`` 空(行数据不完整,理论不可达)→ error 兜底非
        interrupted。"""
        _seed_workspace_and_volume(session_factory, workspace_id)
        now = datetime.now(timezone.utc)
        session = session_factory(workspace_id)
        try:
            session.add(
                PipelineRunORM(
                    run_id="run-broken",
                    workspace_id=workspace_id,
                    state="running",
                    scope="single",
                    start_chapter_id="ch-0001",
                    end_chapter_id=None,
                    volume_id=None,
                    profile_override=None,
                    chapter_ids=[],
                    current_chapter=None,
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

        chapter_service = _FakeChapterService()
        service = _make_pipeline(
            workspace_service, session_factory, chapter_service, fake_ws_manager
        )
        marked = service.mark_running_as_interrupted_on_startup([workspace_id])
        assert marked == 1
        session = session_factory(workspace_id)
        try:
            row = session.get(PipelineRunORM, "run-broken")
            assert row is not None
            assert row.state == "error"
        finally:
            session.close()

    def test_multiple_active_runs_block_startup_without_mutation(
        self,
        workspace_service: WorkspaceService,
        workspace_id: str,
        session_factory: Callable[[str], Session],
        fake_ws_manager: _FakeWSManager,
    ) -> None:
        """损坏的多 active 现场不能部分翻态后继续启动。"""

        _seed_workspace_and_volume(session_factory, workspace_id)
        now = datetime.now(timezone.utc)
        session = session_factory(workspace_id)
        try:
            for run_id, state in (
                ("run-running", "running"),
                ("run-paused", "paused"),
            ):
                session.add(
                    PipelineRunORM(
                        run_id=run_id,
                        workspace_id=workspace_id,
                        state=state,
                        scope="single",
                        start_chapter_id="ch-0001",
                        end_chapter_id=None,
                        volume_id=None,
                        profile_override=None,
                        chapter_ids=["ch-0001"],
                        current_chapter="ch-0001",
                        completed_chapter_ids=[],
                        skipped_chapter_ids=[],
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
        service = _make_pipeline(
            workspace_service,
            session_factory,
            _FakeChapterService(),
            fake_ws_manager,
        )

        with pytest.raises(PipelineServiceError, match="多个 active"):
            service.mark_running_as_interrupted_on_startup(
                [workspace_id],
                apply=False,
            )

        session = session_factory(workspace_id)
        try:
            states = {
                row.run_id: row.state
                for row in session.scalars(
                    select(PipelineRunORM).where(
                        PipelineRunORM.workspace_id == workspace_id
                    )
                )
            }
            assert states == {"run-running": "running", "run-paused": "paused"}
        finally:
            session.close()


# =============================================================================
# ② approved 章防重写(治理性护栏,governance §2.30)
# =============================================================================


class TestApprovedChapterNoRewrite:
    """循环选章跳过 status 已 approved/published 的章。

    治理语义:completed 只记**本 run** 进度 — 新 run / 重启 resume 场景
    已过审章不在 completed 里,不查章真实状态会**重写已过审内容**(用户
    已确认的正文被静默覆盖,数据级不可逆)。
    """

    def test_new_run_skips_already_approved_chapters(
        self,
        workspace_service: WorkspaceService,
        workspace_id: str,
        session_factory: Callable[[str], Session],
        fake_ws_manager: _FakeWSManager,
    ) -> None:
        """新 run(completed 空)对已 approved 章零调用,只写未过审章。"""
        _seed_workspace_and_volume(session_factory, workspace_id)
        _seed_all_cards_approved(
            session_factory, workspace_id, ["ch-0001", "ch-0002", "ch-0003"]
        )
        _seed_chapter(session_factory, workspace_id, "ch-0001", status="approved")
        _seed_chapter(session_factory, workspace_id, "ch-0002", status="approved")
        _seed_chapter(session_factory, workspace_id, "ch-0003", status="drafting")

        chapter_service = _FakeChapterService(session_factory)
        service = _make_pipeline(
            workspace_service, session_factory, chapter_service, fake_ws_manager
        )
        run_id = _start_range_run(service, workspace_id)
        service.execute(workspace_id, run_id)

        assert chapter_service.calls_for("ch-0001") == 0
        assert chapter_service.calls_for("ch-0002") == 0
        assert chapter_service.calls_for("ch-0003") == 1
        snap = service.status(workspace_id)
        assert snap.state == "stopped"
        assert snap.pause_reason == "completed"
        # completed 只记本 run 实际写的章(诚实进度)
        assert snap.completed_chapter_ids == ["ch-0003"]

    def test_approved_without_ready_resumes_checkpoint_without_write(
        self,
        workspace_service: WorkspaceService,
        workspace_id: str,
        session_factory: Callable[[str], Session],
        fake_ws_manager: _FakeWSManager,
    ) -> None:
        """approved+ready 空重选该章，但只走 checkpoint resumer。"""
        _seed_workspace_and_volume(session_factory, workspace_id)
        _seed_all_cards_approved(session_factory, workspace_id, ["ch-0001"])
        _seed_chapter(
            session_factory,
            workspace_id,
            "ch-0001",
            status="approved",
            ready=False,
        )
        chapter_service = _FakeChapterService(
            session_factory,
            checkpoint_resume_for={"ch-0001"},
        )
        service = _make_pipeline(
            workspace_service, session_factory, chapter_service, fake_ws_manager
        )
        snap = service.start(
            workspace_id,
            scope="single",
            start_chapter_id="ch-0001",
            end_chapter_id=None,
            volume_id=None,
            profile_override=None,
        )

        service.execute(workspace_id, snap.run_id)

        assert chapter_service.checkpoint_calls == ["ch-0001"]
        assert chapter_service.calls_for("ch-0001") == 0
        status = service.status(workspace_id)
        assert status.run_id == snap.run_id
        assert status.state == "stopped"
        assert status.pause_reason == "completed"
        assert status.completed_chapter_ids == ["ch-0001"]
        session = session_factory(workspace_id)
        try:
            row = session.get(ChapterORM, "ch-0001")
            assert row is not None
            assert row.ready_for_next_chapter_at is not None
            attempts = list(
                session.scalars(
                    select(AttemptLedgerORM).where(
                        AttemptLedgerORM.run_id == snap.run_id,
                        AttemptLedgerORM.attempt_kind == "pipeline",
                    )
                )
            )
            assert len(attempts) == 1
            assert attempts[0].status == "succeeded"
            assert attempts[0].completed_at is not None
            assert attempts[0].error_type is None
        finally:
            session.close()

    def test_published_chapters_also_skipped(
        self,
        workspace_service: WorkspaceService,
        workspace_id: str,
        session_factory: Callable[[str], Session],
        fake_ws_manager: _FakeWSManager,
    ) -> None:
        _seed_workspace_and_volume(session_factory, workspace_id)
        _seed_all_cards_approved(
            session_factory, workspace_id, ["ch-0001", "ch-0002", "ch-0003"]
        )
        _seed_chapter(
            session_factory,
            workspace_id,
            "ch-0001",
            status="published",
            ready=True,
        )
        _seed_chapter(session_factory, workspace_id, "ch-0002", status="drafting")
        _seed_chapter(session_factory, workspace_id, "ch-0003", status="drafting")

        chapter_service = _FakeChapterService(session_factory)
        service = _make_pipeline(
            workspace_service, session_factory, chapter_service, fake_ws_manager
        )
        run_id = _start_range_run(service, workspace_id)
        service.execute(workspace_id, run_id)

        assert chapter_service.calls_for("ch-0001") == 0
        assert chapter_service.calls_for("ch-0002") == 1
        assert chapter_service.calls_for("ch-0003") == 1

    def test_resume_with_lost_completed_does_not_rewrite_approved(
        self,
        workspace_service: WorkspaceService,
        workspace_id: str,
        session_factory: Callable[[str], Session],
        fake_ws_manager: _FakeWSManager,
    ) -> None:
        """防重写护栏核心价值场景:崩溃时 completed 记账丢失(章已
        approved 但不在 completed)→ resume 后终态判定兜住,不重写。"""
        _seed_workspace_and_volume(session_factory, workspace_id)
        _seed_all_cards_approved(
            session_factory, workspace_id, ["ch-0001", "ch-0002", "ch-0003"]
        )
        # ch-0001 已 approved 但 completed=[](记账与章状态不同步的崩溃现场)
        _seed_chapter(session_factory, workspace_id, "ch-0001", status="approved")
        _seed_chapter(session_factory, workspace_id, "ch-0002", status="drafting")
        _seed_chapter(session_factory, workspace_id, "ch-0003", status="drafting")

        chapter_service = _FakeChapterService(session_factory)
        service = _make_pipeline(
            workspace_service, session_factory, chapter_service, fake_ws_manager
        )
        run_id = _start_range_run(service, workspace_id)
        _simulate_crash_mid_run(
            session_factory, workspace_id, run_id, completed=[], current="ch-0001"
        )
        service.mark_running_as_interrupted_on_startup([workspace_id])
        service.resume(workspace_id)
        service.execute(workspace_id, run_id)

        assert chapter_service.calls_for("ch-0001") == 0  # 不重写已过审
        assert chapter_service.calls_for("ch-0002") == 1
        assert chapter_service.calls_for("ch-0003") == 1

    def test_all_chapters_terminal_completes_run_with_zero_writes(
        self,
        workspace_service: WorkspaceService,
        workspace_id: str,
        session_factory: Callable[[str], Session],
        fake_ws_manager: _FakeWSManager,
    ) -> None:
        """全部章已终态 → run 直接自然完成,零写调用。"""
        _seed_workspace_and_volume(session_factory, workspace_id)
        for ch in ("ch-0001", "ch-0002", "ch-0003"):
            _seed_chapter(session_factory, workspace_id, ch, status="approved")

        chapter_service = _FakeChapterService()
        service = _make_pipeline(
            workspace_service, session_factory, chapter_service, fake_ws_manager
        )
        run_id = _start_range_run(service, workspace_id)
        service.execute(workspace_id, run_id)

        assert chapter_service.calls == []
        snap = service.status(workspace_id)
        assert snap.state == "stopped"
        assert snap.pause_reason == "completed"

    def test_non_terminal_statuses_are_still_selected(
        self,
        workspace_service: WorkspaceService,
        workspace_id: str,
        session_factory: Callable[[str], Session],
        fake_ws_manager: _FakeWSManager,
    ) -> None:
        """终态集合不许误扩:reviewing / human_intervention / invalidated
        等非终态章仍被选中(只有 approved / published 跳过)。"""
        _seed_workspace_and_volume(session_factory, workspace_id)
        _seed_all_cards_approved(
            session_factory, workspace_id, ["ch-0001", "ch-0002", "ch-0003"]
        )
        _seed_chapter(session_factory, workspace_id, "ch-0001", status="reviewing")
        _seed_chapter(
            session_factory, workspace_id, "ch-0002", status="human_intervention"
        )
        _seed_chapter(session_factory, workspace_id, "ch-0003", status="invalidated")

        chapter_service = _FakeChapterService(session_factory)
        service = _make_pipeline(
            workspace_service, session_factory, chapter_service, fake_ws_manager
        )
        run_id = _start_range_run(service, workspace_id)
        service.execute(workspace_id, run_id)

        # 三章全部被选中写(非终态不跳过 — 它们是否可写由 write 自身 gate 管)
        assert chapter_service.calls_for("ch-0001") == 1
        assert chapter_service.calls_for("ch-0002") == 1
        assert chapter_service.calls_for("ch-0003") == 1


# =============================================================================
# ③ reset_stale 扫描(缺口 6:中断章自愈)
# =============================================================================


class TestResetStale:
    """非终态 + 无活跃 task 才重置;"等人"态不动;对账跳过不误杀。"""

    def test_resets_mid_flow_statuses_to_drafting(
        self,
        workspace_service: WorkspaceService,
        workspace_id: str,
        session_factory: Callable[[str], Session],
        fake_ws_manager: _FakeWSManager,
    ) -> None:
        _seed_workspace_and_volume(session_factory, workspace_id)
        _seed_chapter(session_factory, workspace_id, "ch-0001", status="reviewing")
        _seed_chapter(session_factory, workspace_id, "ch-0002", status="rewriting")
        _seed_chapter(session_factory, workspace_id, "ch-0003", status="revising")

        rewind = _make_rewind(workspace_service, session_factory, fake_ws_manager)
        result = rewind.reset_stale(workspace_id)

        assert result.skipped_reason is None
        assert result.reset_chapter_ids == ["ch-0001", "ch-0002", "ch-0003"]
        assert result.reset_count == 3
        for ch in ("ch-0001", "ch-0002", "ch-0003"):
            assert _chapter_status(session_factory, workspace_id, ch) == "drafting"

    def test_does_not_touch_waiting_for_human_statuses(
        self,
        workspace_service: WorkspaceService,
        workspace_id: str,
        session_factory: Callable[[str], Session],
        fake_ws_manager: _FakeWSManager,
    ) -> None:
        """human_intervention / invalidated 语义"等人",自愈不动。"""
        _seed_workspace_and_volume(session_factory, workspace_id)
        _seed_chapter(
            session_factory, workspace_id, "ch-0001", status="human_intervention"
        )
        _seed_chapter(session_factory, workspace_id, "ch-0002", status="invalidated")

        rewind = _make_rewind(workspace_service, session_factory, fake_ws_manager)
        result = rewind.reset_stale(workspace_id)

        assert result.reset_count == 0
        assert (
            _chapter_status(session_factory, workspace_id, "ch-0001")
            == "human_intervention"
        )
        assert (
            _chapter_status(session_factory, workspace_id, "ch-0002") == "invalidated"
        )

    def test_does_not_touch_terminal_and_drafting(
        self,
        workspace_service: WorkspaceService,
        workspace_id: str,
        session_factory: Callable[[str], Session],
        fake_ws_manager: _FakeWSManager,
    ) -> None:
        _seed_workspace_and_volume(session_factory, workspace_id)
        _seed_chapter(session_factory, workspace_id, "ch-0001", status="approved")
        _seed_chapter(session_factory, workspace_id, "ch-0002", status="published")
        _seed_chapter(session_factory, workspace_id, "ch-0003", status="drafting")

        rewind = _make_rewind(workspace_service, session_factory, fake_ws_manager)
        result = rewind.reset_stale(workspace_id)

        assert result.reset_count == 0
        assert _chapter_status(session_factory, workspace_id, "ch-0001") == "approved"
        assert _chapter_status(session_factory, workspace_id, "ch-0002") == "published"

    def test_skips_when_active_pipeline_run(
        self,
        workspace_service: WorkspaceService,
        workspace_id: str,
        session_factory: Callable[[str], Session],
        fake_ws_manager: _FakeWSManager,
    ) -> None:
        """对账层 1:running pipeline run(循环正在写章,pipeline 不进
        task_registry)→ 跳过不误杀。"""
        _seed_workspace_and_volume(session_factory, workspace_id)
        _seed_chapter(session_factory, workspace_id, "ch-0001", status="reviewing")
        chapter_service = _FakeChapterService()
        pipeline = _make_pipeline(
            workspace_service, session_factory, chapter_service, fake_ws_manager
        )
        _start_range_run(pipeline, workspace_id)  # 行 state=running

        rewind = _make_rewind(workspace_service, session_factory, fake_ws_manager)
        result = rewind.reset_stale(workspace_id)

        assert result.skipped_reason == "active_pipeline_run"
        assert result.reset_count == 0
        assert _chapter_status(session_factory, workspace_id, "ch-0001") == "reviewing"

    def test_paused_or_interrupted_run_does_not_block_heal(
        self,
        workspace_service: WorkspaceService,
        workspace_id: str,
        session_factory: Callable[[str], Session],
        fake_ws_manager: _FakeWSManager,
    ) -> None:
        """只有 running 挡自愈:重启后 run=interrupted 时必须能自愈中断章
        (否则断点续跑闭环死锁 — resume 前章永远停非终态)。"""
        _seed_workspace_and_volume(session_factory, workspace_id)
        _seed_chapter(session_factory, workspace_id, "ch-0001", status="reviewing")
        chapter_service = _FakeChapterService()
        pipeline = _make_pipeline(
            workspace_service, session_factory, chapter_service, fake_ws_manager
        )
        run_id = _start_range_run(pipeline, workspace_id)
        _simulate_crash_mid_run(
            session_factory, workspace_id, run_id, completed=[], current="ch-0001"
        )
        pipeline.mark_running_as_interrupted_on_startup([workspace_id])

        rewind = _make_rewind(workspace_service, session_factory, fake_ws_manager)
        result = rewind.reset_stale(workspace_id)

        assert result.skipped_reason is None
        assert result.reset_chapter_ids == ["ch-0001"]
        assert _chapter_status(session_factory, workspace_id, "ch-0001") == "drafting"

    def test_skips_when_active_tasks(
        self,
        workspace_service: WorkspaceService,
        workspace_id: str,
        session_factory: Callable[[str], Session],
        fake_ws_manager: _FakeWSManager,
    ) -> None:
        """对账层 2:task_registry 有 pending/running 任务(chapter:write
        等 BackgroundTask 在写)→ 跳过不误杀。"""
        _seed_workspace_and_volume(session_factory, workspace_id)
        _seed_chapter(session_factory, workspace_id, "ch-0001", status="reviewing")

        rewind = _make_rewind(
            workspace_service,
            session_factory,
            fake_ws_manager,
            active_task_checker=lambda ws: True,
        )
        result = rewind.reset_stale(workspace_id)

        assert result.skipped_reason == "active_tasks"
        assert result.reset_count == 0
        assert _chapter_status(session_factory, workspace_id, "ch-0001") == "reviewing"

    def test_empty_scan_is_idempotent(
        self,
        workspace_service: WorkspaceService,
        workspace_id: str,
        session_factory: Callable[[str], Session],
        fake_ws_manager: _FakeWSManager,
    ) -> None:
        _seed_workspace_and_volume(session_factory, workspace_id)

        rewind = _make_rewind(workspace_service, session_factory, fake_ws_manager)
        result = rewind.reset_stale(workspace_id)
        assert result.reset_count == 0
        assert result.reset_chapter_ids == []
        assert result.skipped_reason is None

    def test_publishes_chapter_stale_reset_event(
        self,
        workspace_service: WorkspaceService,
        workspace_id: str,
        session_factory: Callable[[str], Session],
        fake_ws_manager: _FakeWSManager,
    ) -> None:
        _seed_workspace_and_volume(session_factory, workspace_id)
        _seed_chapter(session_factory, workspace_id, "ch-0001", status="reviewing")

        rewind = _make_rewind(workspace_service, session_factory, fake_ws_manager)
        rewind.reset_stale(workspace_id)

        events = fake_ws_manager.events_named("chapter_stale_reset")
        assert len(events) == 1
        assert events[0]["data"]["chapter_id"] == "ch-0001"
        assert events[0]["data"]["to_status"] == "drafting"


# =============================================================================
# ④ 富记忆进程中断收口 + lifespan hook 接线
# =============================================================================


class TestMemoryEnrichmentStartupRecoveryR8:
    """新进程只收口富记忆僵尸态，不自动重放可能 unknown 的 Provider 阶段。"""

    def test_running_becomes_failed_with_evidence_and_is_idempotent(
        self,
        workspace_service: WorkspaceService,
        workspace_id: str,
        session_factory: Callable[[str], Session],
        monkeypatch: pytest.MonkeyPatch,
    ) -> None:
        _seed_workspace_and_volume(session_factory, workspace_id, chapter_count=9)
        _seed_chapter(session_factory, workspace_id, "ch-0001", status="approved")
        _seed_chapter(session_factory, workspace_id, "ch-0002", status="approved")
        _seed_chapter(
            session_factory,
            workspace_id,
            "ch-0003",
            status="approved",
            ready=False,
        )
        _seed_chapter(session_factory, workspace_id, "ch-0004", status="approved")
        _seed_chapter(session_factory, workspace_id, "ch-0005", status="approved")
        _seed_chapter(session_factory, workspace_id, "ch-0006", status="approved")
        _seed_chapter(session_factory, workspace_id, "ch-0007", status="approved")
        _seed_chapter(session_factory, workspace_id, "ch-0008", status="approved")
        _seed_chapter(session_factory, workspace_id, "ch-0009", status="approved")
        session = session_factory(workspace_id)
        try:
            running = session.get(ChapterORM, "ch-0001")
            succeeded = session.get(ChapterORM, "ch-0002")
            non_ready = session.get(ChapterORM, "ch-0003")
            non_object = session.get(ChapterORM, "ch-0004")
            bad_tasks = session.get(ChapterORM, "ch-0005")
            bad_failures = session.get(ChapterORM, "ch-0006")
            unknown_status = session.get(ChapterORM, "ch-0007")
            running_with_success_time = session.get(ChapterORM, "ch-0008")
            running_with_bad_started_at = session.get(ChapterORM, "ch-0009")
            assert running is not None
            assert succeeded is not None
            assert non_ready is not None
            assert non_object is not None
            assert bad_tasks is not None
            assert bad_failures is not None
            assert unknown_status is not None
            assert running_with_success_time is not None
            assert running_with_bad_started_at is not None
            running.memory_enrichment_status = "running"
            running.memory_enrichment_details = {
                "started_at": "2026-08-02T12:30:11+00:00",
                "tasks": [{"name": "chapter_summary", "status": "succeeded"}],
            }
            succeeded.memory_enrichment_status = "succeeded"
            succeeded.memory_enrichment_details = {"tasks": []}
            succeeded.memory_enriched_at = datetime.now(timezone.utc)
            non_ready.memory_enrichment_status = "pending"
            non_ready.memory_enrichment_details = None
            non_object.memory_enrichment_status = "pending"
            non_object.memory_enrichment_details = None
            bad_tasks.memory_enrichment_status = "pending"
            bad_tasks.memory_enrichment_details = None
            bad_failures.memory_enrichment_status = "pending"
            bad_failures.memory_enrichment_details = None
            unknown_status.memory_enrichment_status = "pending"
            unknown_status.memory_enrichment_details = None
            running_with_success_time.memory_enrichment_status = "succeeded"
            running_with_success_time.memory_enrichment_details = {"tasks": []}
            running_with_success_time.memory_enriched_at = datetime.now(timezone.utc)
            running_with_bad_started_at.memory_enrichment_status = "pending"
            running_with_bad_started_at.memory_enrichment_details = None
            session.commit()
        finally:
            session.close()

        paths = WorkspacePaths(
            workspace_id,
            workspaces_root=workspace_service.workspaces_root,
        )
        final_path = paths.chapter_version_file("ch-0001", "final.md")
        final_path.parent.mkdir(parents=True, exist_ok=True)
        final_path.write_text("第一章终稿", encoding="utf-8")
        monkeypatch.setattr(
            "src.services.chapter_rewind_service.load_validated_continuity_ledger",
            lambda **_kwargs: SimpleNamespace(
                ledger_hash="0" * 64,
                source_text_hash="1" * 64,
            ),
        )

        rewind = _make_rewind(workspace_service, session_factory)
        assert rewind.mark_running_memory_enrichment_as_failed_on_startup(
            workspace_id
        ) == ["ch-0001"]

        session = session_factory(workspace_id)
        try:
            running = session.get(ChapterORM, "ch-0001")
            succeeded = session.get(ChapterORM, "ch-0002")
            non_ready = session.get(ChapterORM, "ch-0003")
            non_object = session.get(ChapterORM, "ch-0004")
            bad_tasks = session.get(ChapterORM, "ch-0005")
            bad_failures = session.get(ChapterORM, "ch-0006")
            unknown_status = session.get(ChapterORM, "ch-0007")
            running_with_success_time = session.get(ChapterORM, "ch-0008")
            running_with_bad_started_at = session.get(ChapterORM, "ch-0009")
            assert running is not None
            assert succeeded is not None
            assert non_ready is not None
            assert non_object is not None
            assert bad_tasks is not None
            assert bad_failures is not None
            assert unknown_status is not None
            assert running_with_success_time is not None
            assert running_with_bad_started_at is not None
            assert running.memory_enrichment_status == "failed"
            assert running.memory_enriched_at is None
            details = running.memory_enrichment_details
            assert details["started_at"] == "2026-08-02T12:30:11+00:00"
            assert details["tasks"] == [
                {"name": "chapter_summary", "status": "succeeded"}
            ]
            assert details["failures"][-1]["error_type"] == (
                "EngineRestartedMidMemoryEnrichment"
            )
            assert details["failures"][-1]["result_unknown"] is True
            recovery = details["startup_recovery"]
            assert recovery["reason"] == "engine_restarted_mid_memory_enrichment"
            assert recovery["previous_status"] == "running"
            assert recovery["result_unknown"] is True
            assert recovery["automatic_provider_replay"] is False
            assert recovery["sqlite_side_effect_count"] == 0
            assert recovery["vector_count"] == 0
            assert recovery["vector_quarantined_count"] == 0
            assert recovery["vector_quarantine_succeeded"] is True
            assert recovery["side_effects_verified_absent"] is True
            assert recovery["safe_to_continue"] is True
            assert recovery["quarantine_required"] is False
            assert recovery["recovery_disposition"] == (
                "safe_degraded_no_sqlite_or_vector_persist"
            )
            assert succeeded.memory_enrichment_status == "succeeded"
            assert non_ready.memory_enrichment_status == "pending"
            assert non_ready.memory_enrichment_details is None
            assert non_object.memory_enrichment_status == "pending"
            assert non_object.memory_enrichment_details is None
            assert bad_tasks.memory_enrichment_status == "pending"
            assert bad_tasks.memory_enrichment_details is None
            assert bad_failures.memory_enrichment_status == "pending"
            assert bad_failures.memory_enrichment_details is None
            assert unknown_status.memory_enrichment_status == "pending"
            assert unknown_status.memory_enrichment_details is None
            assert running_with_success_time.memory_enrichment_status == "succeeded"
            assert running_with_success_time.memory_enriched_at is not None
            assert running_with_success_time.memory_enrichment_details == {"tasks": []}
            assert running_with_bad_started_at.memory_enrichment_status == "pending"
            assert running_with_bad_started_at.memory_enrichment_details is None
        finally:
            session.close()

        assert (
            rewind.mark_running_memory_enrichment_as_failed_on_startup(workspace_id)
            == []
        )

    def test_startup_hook_fails_closed_on_first_workspace_error(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        from src.api import main as main_mod

        calls: list[str] = []

        class _FakeRewind:
            def mark_running_memory_enrichment_as_failed_on_startup(
                self, ws_id: str
            ) -> list[str]:
                calls.append(ws_id)
                if ws_id == "ws-bad":
                    raise RuntimeError("db 损坏(fake)")
                return ["ch-0001"]

        monkeypatch.setattr(
            "src.api.deps.build_rewind_components_for_lifespan",
            lambda: (_FakeRewind(), ["ws-bad", "ws-good"]),
        )
        with pytest.raises(RuntimeError, match="db 损坏"):
            main_mod._run_memory_enrichment_startup_scan()
        assert calls == ["ws-bad"]

    def test_lifespan_builder_uses_vector_content_snapshot_not_count(self) -> None:
        """冷启动 baseline 必须拿 count+digest，不能退回只有 count。"""

        import inspect

        from src.api import deps as deps_mod

        source = inspect.getsource(deps_mod.build_rewind_components_for_lifespan)
        assert "inspect_persisted_chapter_vectors_read_only" in source
        assert "count_persisted_chapter_vectors_read_only" not in source

    def test_new_running_missing_baseline_is_preserved_and_blocks_startup(
        self,
        workspace_service: WorkspaceService,
        workspace_id: str,
        session_factory: Callable[[str], Session],
    ) -> None:
        """新式 Worker 丢 baseline 是 checkpoint 损坏，不能降格成 legacy。"""

        _seed_workspace_and_volume(session_factory, workspace_id)
        _seed_chapter(session_factory, workspace_id, "ch-0001", status="approved")
        details = {
            "schema_version": 1,
            "ready_at": "2026-08-02T12:30:00+00:00",
            "final_version_id": 1,
            "final_path": "/tmp/ch-0001/final.md",
            "continuity_ledger_hash": "0" * 64,
            "continuity_ledger_source_text_hash": "1" * 64,
            "started_at": "2026-08-02T12:30:11+00:00",
            "tasks": [],
            "lease_id": "a" * 32,
        }
        with session_factory(workspace_id) as session:
            row = session.get(ChapterORM, "ch-0001")
            assert row is not None
            row.memory_enrichment_status = "running"
            row.memory_enrichment_details = details
            session.commit()

        rewind = _make_rewind(workspace_service, session_factory)
        with pytest.raises(
            ChapterRewindServiceError,
            match="side_effect_baseline",
        ):
            rewind.mark_running_memory_enrichment_as_failed_on_startup(workspace_id)

        with session_factory(workspace_id) as session:
            row = session.get(ChapterORM, "ch-0001")
            assert row is not None
            assert row.memory_enrichment_status == "running"
            assert row.memory_enrichment_details == details

    @pytest.mark.parametrize("failure_mode", ["source", "vector_presence"])
    def test_unknown_side_effects_remain_blocked_after_startup_audit(
        self,
        workspace_service: WorkspaceService,
        workspace_id: str,
        session_factory: Callable[[str], Session],
        failure_mode: str,
    ) -> None:
        _seed_workspace_and_volume(session_factory, workspace_id)
        _seed_chapter(session_factory, workspace_id, "ch-0001", status="approved")
        session = session_factory(workspace_id)
        try:
            row = session.get(ChapterORM, "ch-0001")
            assert row is not None
            row.memory_enrichment_status = "running"
            row.memory_enrichment_details = {
                "started_at": "2026-08-02T12:30:11+00:00",
                "tasks": [],
            }
            session.commit()
        finally:
            session.close()

        vector = _FakeVectorStore(
            chapter_counts=(
                {"ch-0001": 1} if failure_mode == "vector_presence" else None
            ),
        )
        rewind = _make_rewind(
            workspace_service,
            session_factory,
            vector_store=vector,
        )
        assert rewind.mark_running_memory_enrichment_as_failed_on_startup(
            workspace_id
        ) == ["ch-0001"]

        session = session_factory(workspace_id)
        try:
            row = session.get(ChapterORM, "ch-0001")
            assert row is not None
            assert row.memory_enrichment_status == "failed"
            recovery = row.memory_enrichment_details["startup_recovery"]
            assert recovery["result_unknown"] is True
            assert recovery["safe_to_continue"] is False
            assert recovery["quarantine_required"] is True
            assert recovery["recovery_disposition"] == "blocked_uncertain"
            if failure_mode == "source":
                assert recovery["source_provenance_verified"] is False
                assert recovery["vector_quarantine_succeeded"] is True
            else:
                assert recovery["sqlite_side_effect_count"] == 0
                assert recovery["vector_quarantine_succeeded"] is False
                assert "Chroma 存在本章向量" in (recovery["vector_quarantine_error"])
        finally:
            session.close()

    def test_lifespan_orders_memory_recovery_after_task_scan_before_chapter_reset(
        self,
    ) -> None:
        """全局 task 先 fail-closed，再准入、预检并执行 workspace 恢复写。"""
        import inspect

        from src.api import main as main_mod

        src = inspect.getsource(main_mod._lifespan)
        src = src[src.index("configure_logging()") :]
        admission_pos = src.index("_run_workspace_recovery_admission_scan")
        preflight_pos = src.index("_run_workspace_recovery_state_preflight")
        pipeline_pos = src.index("_run_pipeline_startup_scan")
        task_pos = src.index("_run_task_startup_scan")
        lease_pos = src.index("_run_chapter_operation_lease_startup_scan")
        memory_pos = src.index("_run_memory_enrichment_startup_scan")
        chapter_pos = src.index("_run_chapter_reset_stale_scan")
        assert (
            task_pos
            < admission_pos
            < preflight_pos
            < pipeline_pos
            < lease_pos
            < memory_pos
            < chapter_pos
        )


class TestLifespanResetStaleScanWiring:
    """main.py 启动 hook → 逐 workspace 调 reset_stale;失败 swallow。"""

    def test_hook_calls_reset_stale_for_each_workspace(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        from src.api import main as main_mod

        calls: list[str] = []

        class _FakeRewind:
            def reset_stale(self, ws_id: str) -> Any:
                calls.append(ws_id)
                return SimpleNamespace(reset_count=1, skipped_reason=None)

        monkeypatch.setattr(
            "src.api.deps.build_rewind_components_for_lifespan",
            lambda: (_FakeRewind(), ["ws-a", "ws-b"]),
        )
        main_mod._run_chapter_reset_stale_scan()
        assert calls == ["ws-a", "ws-b"]

    def test_hook_swallows_single_workspace_failure(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        """单 workspace 抛错不阻塞其他 workspace,也不冒泡阻塞启动。"""
        from src.api import main as main_mod

        calls: list[str] = []

        class _FakeRewind:
            def reset_stale(self, ws_id: str) -> Any:
                calls.append(ws_id)
                if ws_id == "ws-bad":
                    raise RuntimeError("db 损坏(fake)")
                return SimpleNamespace(reset_count=0, skipped_reason=None)

        monkeypatch.setattr(
            "src.api.deps.build_rewind_components_for_lifespan",
            lambda: (_FakeRewind(), ["ws-bad", "ws-good"]),
        )
        main_mod._run_chapter_reset_stale_scan()  # 不抛
        assert calls == ["ws-bad", "ws-good"]

    def test_hook_swallows_builder_failure(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        from src.api import main as main_mod

        def _boom() -> Any:
            raise RuntimeError("deps 构造失败(fake)")

        monkeypatch.setattr("src.api.deps.build_rewind_components_for_lifespan", _boom)
        main_mod._run_chapter_reset_stale_scan()  # 不抛 = swallow 兜底生效

    def test_lifespan_orders_reset_stale_after_task_scan(self) -> None:
        """顺序护栏:reset_stale 必须排在 task 启动扫描之后(僵尸 task 先翻
        interrupted,active_tasks 对账才不被挡)。源码顺序断言(lifespan
        是 async 上下文,mock 全链路成本高;字面顺序足以防换序回退)。"""
        import inspect

        from src.api import main as main_mod

        src = inspect.getsource(main_mod._lifespan)
        src = src[src.index("configure_logging()") :]
        task_pos = src.index("_run_task_startup_scan")
        heal_pos = src.index("_run_chapter_reset_stale_scan")
        assert task_pos < heal_pos
