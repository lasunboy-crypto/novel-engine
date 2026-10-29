"""章级看门狗 + Run 级成本护栏防回退测试(v2 w1-s3)。

覆盖 v2_plan §4 w1-s3 "产出的防回退护栏" 3 项 + 实施期新增面:

① **看门狗超时翻态测**:超章级预算 → run ``interrupted`` +
   ``error_details.reason=chapter_watchdog_timeout``;未超不动;已翻过
   (flagged)不重复;run 非 running(pause/stop 竞争)不覆盖(闭合
   v2_plan §2.2 缺口 5)。
② **cost gate 接线测**(P4-B-08 留观闭账):超限 → run paused +
   ``pause_reason=cost_guard_exceeded`` + 零 write 调用 +
   ``cost_guard_triggered`` WS 事件;未超直行;``action=warn`` 只通知
   不停;未注入(None)= disable;章级 gate 抛 ``CostGuardTriggered``
   特判不走失败策略(skip 策略下也 pause)。
③ **看门狗与 reset_stale 协同测**(两机制设计自洽):真 ``_watchdog_tick``
   翻 interrupted(章留非终态 reviewing)→ ``reset_stale`` 自愈回
   drafting(interrupted 不触发"active_pipeline_run"防误杀跳过)→
   resume + execute 续跑该章至完成。
④ 实施期新增:single-flight 循环互斥(同 run 重入静默 / 异 run 冲突翻
   interrupted+``pipeline_loop_conflict``)、心跳生命周期(选章登记 /
   退出注销)、``PipelineWatchdogConfig`` 加载容错。

Fake 设计沿用 ``test_v2_w1_s2_resume_and_reset_stale.py``(w1-s2):
``_FakeChapterService.write`` 记录调用序列 + 可配置抛
``CostGuardTriggered``;卡状态种 ``ChapterCardORM`` 行;章状态种
``ChapterORM`` 行。watchdog 逻辑测试直调 ``_watchdog_tick(now)``,不依赖
daemon 线程节奏。
"""

from __future__ import annotations

import time
from collections.abc import Callable
from datetime import UTC, datetime
from pathlib import Path
from types import SimpleNamespace
from typing import Any

import pytest
from pydantic import ValidationError
from sqlalchemy import create_engine
from sqlalchemy.orm import Session
from sqlalchemy.pool import StaticPool

from src.api.errors import CostGuardTriggered
from src.core.db import Base
from src.core.db.models import (
    ChapterCardORM,
    ChapterORM,
    MetricsDailyORM,
    PipelineRunORM,
    VolumeORM,
    WorkspaceORM,
)
from src.core.state_machine.states import ChapterState
from src.services.chapter_rewind_service import ChapterRewindService
from src.services.cost_guard import CostGuardConfig
from src.services.pipeline_service import (
    PipelineService,
    PipelineWatchdogConfig,
    _LoopHeartbeat,
    load_pipeline_watchdog_config,
)
from src.services.workspace_service import WorkspaceService


# ---- Fakes ------------------------------------------------------------------


class _FakeChapterService:
    """ChapterService stub:成功(``final_state=APPROVED``)/ 可配置抛
    ``CostGuardTriggered``(模拟章级 gate)+ 记录调用 + 心跳内省钩子。"""

    def __init__(
        self,
        session_factory: Callable[[str], Session] | None = None,
        *,
        raise_cost_guard: bool = False,
    ) -> None:
        self.calls: list[str] = []
        self._session_factory = session_factory
        self._raise_cost_guard = raise_cost_guard
        # 心跳内省:测试注入 service 引用后,write 时快照心跳条目
        self.service: PipelineService | None = None
        self.heartbeat_snapshots: list[tuple[str | None, str]] = []

    def write(
        self,
        workspace_id: str,
        chapter_id: str,
        *,
        on_state_transition=None,
        profile_override: str | None = None,
    ) -> Any:
        self.calls.append(chapter_id)
        if self.service is not None:
            hb = self.service._heartbeats.get(workspace_id)
            self.heartbeat_snapshots.append(
                (hb.chapter_id if hb is not None else None, chapter_id)
            )
        if self._raise_cost_guard:
            raise CostGuardTriggered(
                f"workspace {workspace_id} cost guard 触发: test",
                details={"scope": "workspace_daily", "workspace_id": workspace_id},
            )
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
        "ws-w1s3",
        display_name="v2 w1-s3 watchdog + cost guard test",
        genre="xianxia",
        premise="test",
        activate=False,
    )
    return "ws-w1s3"


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
) -> None:
    session = session_factory(workspace_id)
    try:
        now = datetime.now(UTC)
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
                    created_at=datetime.now(UTC),
                )
            )
        session.commit()
    finally:
        session.close()


def _seed_today_cost(
    session_factory: Callable[[str], Session],
    workspace_id: str,
    *,
    total_cost_usd: float,
) -> None:
    """把成本灌进 ``chapters.total_cost_usd`` 累计账(Run 级 cost gate 预读源)。

    v2 w2-s2 metrics 修复 B 起,cost gate 读数源 = ``SUM(chapters.
    total_cost_usd)``(工作区累计真值账)而非 ``metrics_daily`` 当日行 —
    本 helper 把指定金额写到工作区第一章上(SUM 语义下等价于任意分布)。
    函数名保留 ``today`` 字面以最小化 diff(语义 = 工作区累计成本)。
    """
    session = session_factory(workspace_id)
    try:
        first = (
            session.query(ChapterORM)
            .filter(ChapterORM.workspace_id == workspace_id)
            .order_by(ChapterORM.chapter_id.asc())
            .first()
        )
        assert first is not None, "_seed_today_cost 需先 _seed_standard_book"
        first.total_cost_usd = total_cost_usd
        session.commit()
    finally:
        session.close()


def _run_state(
    session_factory: Callable[[str], Session],
    workspace_id: str,
    run_id: str,
) -> PipelineRunORM:
    session = session_factory(workspace_id)
    try:
        row = session.get(PipelineRunORM, run_id)
        assert row is not None
        session.expunge(row)
        return row
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
    *,
    cost_guard_config: CostGuardConfig | None = None,
    watchdog_config: PipelineWatchdogConfig | None = None,
) -> PipelineService:
    service = PipelineService(
        chapter_service=chapter_service,  # type: ignore[arg-type]
        workspace_service=workspace_service,
        session_factory=session_factory,
        ws_manager=ws_manager,  # type: ignore[arg-type]
        cost_guard_config=cost_guard_config,
        # 显式注入防测试读仓库根 config.yaml(默认 2400s 不干扰,但显式更稳)
        watchdog_config=watchdog_config or PipelineWatchdogConfig(),
    )
    chapter_service.service = service
    return service


def _make_rewind(
    workspace_service: WorkspaceService,
    session_factory: Callable[[str], Session],
) -> ChapterRewindService:
    return ChapterRewindService(
        workspace_service=workspace_service,
        session_factory=session_factory,
        ws_manager=None,
        active_task_checker=None,
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


def _seed_standard_book(
    session_factory: Callable[[str], Session],
    workspace_id: str,
    *,
    chapter_status: str = "drafting",
) -> list[str]:
    """3 章标准局:workspace + 卷 + 章行 + 卡全 approved。"""
    chapter_ids = ["ch-0001", "ch-0002", "ch-0003"]
    _seed_workspace_and_volume(session_factory, workspace_id)
    for ch in chapter_ids:
        _seed_chapter(session_factory, workspace_id, ch, status=chapter_status)
    _seed_all_cards_approved(session_factory, workspace_id, chapter_ids)
    return chapter_ids


# =============================================================================
# ① 看门狗超时翻态(缺口 5)
# =============================================================================


class TestWatchdogTimeout:
    """``_watchdog_tick``:超预算翻 interrupted;未超不动;不重复;不覆盖非 running。"""

    def _setup_running_with_heartbeat(
        self,
        workspace_service: WorkspaceService,
        session_factory: Callable[[str], Session],
        fake_ws_manager: _FakeWSManager,
        workspace_id: str,
        *,
        timeout_seconds: float = 100.0,
        elapsed_seconds: float = 0.0,
        chapter_id: str | None = "ch-0001",
    ) -> tuple[PipelineService, str, float]:
        """种标准局 + start run + 手工预置心跳(模拟循环正写某章)。

        返回 (service, run_id, now);``now - elapsed_seconds`` 是心跳起点。
        """
        _seed_standard_book(session_factory, workspace_id)
        chapter_service = _FakeChapterService(session_factory)
        service = _make_pipeline(
            workspace_service,
            session_factory,
            chapter_service,
            fake_ws_manager,
            watchdog_config=PipelineWatchdogConfig(
                chapter_timeout_seconds=timeout_seconds
            ),
        )
        run_id = _start_range_run(service, workspace_id)
        now = time.monotonic()
        service._heartbeats[workspace_id] = _LoopHeartbeat(
            run_id=run_id,
            chapter_id=chapter_id,
            started_monotonic=now - elapsed_seconds,
        )
        return service, run_id, now

    def test_timeout_marks_run_interrupted_with_reason(
        self, workspace_service, session_factory, fake_ws_manager, workspace_id
    ):
        service, run_id, now = self._setup_running_with_heartbeat(
            workspace_service,
            session_factory,
            fake_ws_manager,
            workspace_id,
            timeout_seconds=100.0,
            elapsed_seconds=150.0,
        )
        service._watchdog_tick(now)
        row = _run_state(session_factory, workspace_id, run_id)
        assert row.state == "interrupted"
        assert row.error_details["reason"] == "chapter_watchdog_timeout"
        assert row.error_details["chapter_id"] == "ch-0001"
        assert row.error_details["elapsed_seconds"] == pytest.approx(150.0, abs=1.0)
        assert row.error_details["timeout_seconds"] == 100.0
        # 心跳条目置 flagged 不删(single-flight 仍知道老循环占着)
        hb = service._heartbeats[workspace_id]
        assert hb.watchdog_flagged is True

    def test_under_budget_does_not_touch_run(
        self, workspace_service, session_factory, fake_ws_manager, workspace_id
    ):
        service, run_id, now = self._setup_running_with_heartbeat(
            workspace_service,
            session_factory,
            fake_ws_manager,
            workspace_id,
            timeout_seconds=100.0,
            elapsed_seconds=50.0,
        )
        service._watchdog_tick(now)
        row = _run_state(session_factory, workspace_id, run_id)
        assert row.state == "running"
        assert service._heartbeats[workspace_id].watchdog_flagged is False

    def test_flagged_entry_not_marked_twice(
        self, workspace_service, session_factory, fake_ws_manager, workspace_id
    ):
        """翻过一次后(flagged),即使 run 被手工翻回 running 也不再翻。"""
        service, run_id, now = self._setup_running_with_heartbeat(
            workspace_service,
            session_factory,
            fake_ws_manager,
            workspace_id,
            timeout_seconds=100.0,
            elapsed_seconds=150.0,
        )
        service._watchdog_tick(now)
        # 手工翻回 running(模拟 resume 抢在条目清理前)
        session = session_factory(workspace_id)
        try:
            row = session.get(PipelineRunORM, run_id)
            row.state = "running"
            session.commit()
        finally:
            session.close()
        service._watchdog_tick(now + 1000.0)
        row = _run_state(session_factory, workspace_id, run_id)
        assert row.state == "running"  # flagged 挡住第二次翻态

    def test_non_running_state_not_overwritten(
        self, workspace_service, session_factory, fake_ws_manager, workspace_id
    ):
        """run 已被 pause(用户竞争操作)→ watchdog 不覆盖 paused 态。"""
        service, run_id, now = self._setup_running_with_heartbeat(
            workspace_service,
            session_factory,
            fake_ws_manager,
            workspace_id,
            timeout_seconds=100.0,
            elapsed_seconds=150.0,
        )
        service.pause(workspace_id, reason="user_request")
        service._watchdog_tick(now)
        row = _run_state(session_factory, workspace_id, run_id)
        assert row.state == "paused"
        assert row.pause_reason == "user_request"

    def test_idle_gap_entry_ignored(
        self, workspace_service, session_factory, fake_ws_manager, workspace_id
    ):
        """``chapter_id=None``(入口 / 章间空隙)不计超时。"""
        service, run_id, now = self._setup_running_with_heartbeat(
            workspace_service,
            session_factory,
            fake_ws_manager,
            workspace_id,
            timeout_seconds=100.0,
            elapsed_seconds=99999.0,
            chapter_id=None,
        )
        service._watchdog_tick(now)
        row = _run_state(session_factory, workspace_id, run_id)
        assert row.state == "running"

    def test_disabled_watchdog_tick_noop(
        self, workspace_service, session_factory, fake_ws_manager, workspace_id
    ):
        """``chapter_timeout_seconds <= 0`` 禁用:tick 全 noop。"""
        service, run_id, now = self._setup_running_with_heartbeat(
            workspace_service,
            session_factory,
            fake_ws_manager,
            workspace_id,
            timeout_seconds=0.0,
            elapsed_seconds=99999.0,
        )
        service._watchdog_tick(now)
        row = _run_state(session_factory, workspace_id, run_id)
        assert row.state == "running"


# =============================================================================
# ② 心跳生命周期 + watchdog 线程启停
# =============================================================================


class TestHeartbeatLifecycle:
    def test_heartbeat_tracks_current_chapter_and_cleared_on_exit(
        self, workspace_service, session_factory, fake_ws_manager, workspace_id
    ):
        """write 时心跳条目指向正在写的章;execute 退出后注销。"""
        _seed_standard_book(session_factory, workspace_id)
        chapter_service = _FakeChapterService(session_factory)
        service = _make_pipeline(
            workspace_service, session_factory, chapter_service, fake_ws_manager
        )
        run_id = _start_range_run(service, workspace_id)
        service.execute(workspace_id, run_id)
        # write 期间心跳条目章号与正写章一致(_FakeChapterService 内省快照)
        assert chapter_service.heartbeat_snapshots == [
            ("ch-0001", "ch-0001"),
            ("ch-0002", "ch-0002"),
            ("ch-0003", "ch-0003"),
        ]
        # 循环退出 → 心跳注销
        assert workspace_id not in service._heartbeats

    def test_enabled_config_starts_daemon_thread(
        self, workspace_service, session_factory, fake_ws_manager, workspace_id
    ):
        _seed_standard_book(session_factory, workspace_id)
        chapter_service = _FakeChapterService(session_factory)
        service = _make_pipeline(
            workspace_service, session_factory, chapter_service, fake_ws_manager
        )
        run_id = _start_range_run(service, workspace_id)
        service.execute(workspace_id, run_id)
        assert service._watchdog_thread is not None
        assert service._watchdog_thread.daemon is True
        assert service._watchdog_thread.is_alive()

    def test_disabled_config_starts_no_thread(
        self, workspace_service, session_factory, fake_ws_manager, workspace_id
    ):
        _seed_standard_book(session_factory, workspace_id)
        chapter_service = _FakeChapterService(session_factory)
        service = _make_pipeline(
            workspace_service,
            session_factory,
            chapter_service,
            fake_ws_manager,
            watchdog_config=PipelineWatchdogConfig(chapter_timeout_seconds=0.0),
        )
        run_id = _start_range_run(service, workspace_id)
        service.execute(workspace_id, run_id)
        assert service._watchdog_thread is None


# =============================================================================
# ③ single-flight 循环互斥(顺手修 pre-existing 双循环缺陷)
# =============================================================================


class TestSingleFlight:
    def test_same_run_reentry_returns_silently(
        self, workspace_service, session_factory, fake_ws_manager, workspace_id
    ):
        """同 run 重入(:resume 重调 execute,老循环困在 write)→ 静默返回。"""
        _seed_standard_book(session_factory, workspace_id)
        chapter_service = _FakeChapterService(session_factory)
        service = _make_pipeline(
            workspace_service, session_factory, chapter_service, fake_ws_manager
        )
        run_id = _start_range_run(service, workspace_id)
        # 模拟老循环还活着(困在 write):手工预置心跳
        stale_hb = _LoopHeartbeat(
            run_id=run_id,
            chapter_id="ch-0001",
            started_monotonic=time.monotonic(),
        )
        service._heartbeats[workspace_id] = stale_hb
        service.execute(workspace_id, run_id)
        # 零 write 调用 + run 仍 running(老循环资产未被动)
        assert chapter_service.calls == []
        row = _run_state(session_factory, workspace_id, run_id)
        assert row.state == "running"
        # 老循环心跳条目原样保留(重入分支不走 finally 注销)
        assert service._heartbeats[workspace_id] is stale_hb

    def test_conflicting_run_marked_interrupted(
        self, workspace_service, session_factory, fake_ws_manager, workspace_id
    ):
        """异 run 占用(stop 后立刻 start 新 run)→ 新 run 翻 interrupted。"""
        _seed_standard_book(session_factory, workspace_id)
        chapter_service = _FakeChapterService(session_factory)
        service = _make_pipeline(
            workspace_service, session_factory, chapter_service, fake_ws_manager
        )
        old_run_id = _start_range_run(service, workspace_id)
        # 老 run 循环"还活着"(困在 write)
        old_hb = _LoopHeartbeat(
            run_id=old_run_id,
            chapter_id="ch-0001",
            started_monotonic=time.monotonic(),
        )
        service._heartbeats[workspace_id] = old_hb
        # 用户 stop 老 run 后立刻 start 新 run
        service.stop(workspace_id)
        new_snap = service.start(
            workspace_id,
            scope="range",
            start_chapter_id="ch-0001",
            end_chapter_id="ch-0003",
            volume_id=None,
            profile_override=None,
        )
        service.execute(workspace_id, new_snap.run_id)
        # 新 run 翻 interrupted + 冲突原因;零 write;老条目保留
        assert chapter_service.calls == []
        row = _run_state(session_factory, workspace_id, new_snap.run_id)
        assert row.state == "interrupted"
        assert row.error_details["reason"] == "pipeline_loop_conflict"
        assert row.error_details["occupied_by_run"] == old_run_id
        assert service._heartbeats[workspace_id] is old_hb
        # 老循环退出(心跳注销)后,新 run 可 resume 续跑
        del service._heartbeats[workspace_id]
        service.resume(workspace_id)
        service.execute(workspace_id, new_snap.run_id)
        row = _run_state(session_factory, workspace_id, new_snap.run_id)
        assert row.state == "stopped"
        assert row.pause_reason == "completed"


# =============================================================================
# ④ Run 级成本预检(P4-B-08 留观闭账)
# =============================================================================


def _cost_config(
    *,
    per_workspace_daily_limit: float = 10.0,
    action: str = "pause",
) -> CostGuardConfig:
    return CostGuardConfig(
        daily_usd_limit=99999.0,
        monthly_usd_limit=99999.0,
        per_workspace_daily_limit=per_workspace_daily_limit,
        action_on_exceed=action,  # type: ignore[arg-type]
        notify_via=["log", "websocket"],
    )


class TestRunCostGate:
    def test_over_limit_pauses_before_any_write(
        self, workspace_service, session_factory, fake_ws_manager, workspace_id
    ):
        _seed_standard_book(session_factory, workspace_id)
        _seed_today_cost(session_factory, workspace_id, total_cost_usd=15.0)
        chapter_service = _FakeChapterService(session_factory)
        service = _make_pipeline(
            workspace_service,
            session_factory,
            chapter_service,
            fake_ws_manager,
            cost_guard_config=_cost_config(per_workspace_daily_limit=10.0),
        )
        run_id = _start_range_run(service, workspace_id)
        service.execute(workspace_id, run_id)
        # 停在第一章之前:零 write + paused + 专属 reason
        assert chapter_service.calls == []
        row = _run_state(session_factory, workspace_id, run_id)
        assert row.state == "paused"
        assert row.pause_reason == "cost_guard_exceeded"
        # WS 双通道:system.alerts cost_guard_triggered + pipeline_paused
        triggered = fake_ws_manager.events_named("cost_guard_triggered")
        assert len(triggered) == 1
        assert triggered[0]["data"]["workspace_id"] == workspace_id
        paused = fake_ws_manager.events_named("pipeline_paused")
        assert paused[-1]["data"]["reason"] == "cost_guard_exceeded"

    def test_under_limit_runs_to_completion(
        self, workspace_service, session_factory, fake_ws_manager, workspace_id
    ):
        _seed_standard_book(session_factory, workspace_id)
        _seed_today_cost(session_factory, workspace_id, total_cost_usd=5.0)
        chapter_service = _FakeChapterService(session_factory)
        service = _make_pipeline(
            workspace_service,
            session_factory,
            chapter_service,
            fake_ws_manager,
            cost_guard_config=_cost_config(per_workspace_daily_limit=10.0),
        )
        run_id = _start_range_run(service, workspace_id)
        service.execute(workspace_id, run_id)
        assert chapter_service.calls == ["ch-0001", "ch-0002", "ch-0003"]
        row = _run_state(session_factory, workspace_id, run_id)
        assert row.state == "stopped"
        assert row.pause_reason == "completed"
        assert fake_ws_manager.events_named("cost_guard_triggered") == []

    def test_metrics_daily_row_no_longer_drives_run_gate(
        self, workspace_service, session_factory, fake_ws_manager, workspace_id
    ):
        """v2 w2-s2 metrics 修复 B 反向断言(Run 级):metrics_daily 当日行
        有超限金额但 chapters 账为空 → gate 不触发、run 正常跑完。Run 级
        预读与章级是两份独立代码(pipeline_service / chapter_service),
        各自锚读数源 = ``SUM(chapters.total_cost_usd)``。"""
        _seed_standard_book(session_factory, workspace_id)
        session = session_factory(workspace_id)
        try:
            session.merge(
                MetricsDailyORM(
                    date=datetime.now(UTC).date(),
                    total_cost_usd=999.0,
                )
            )
            session.commit()
        finally:
            session.close()
        chapter_service = _FakeChapterService(session_factory)
        service = _make_pipeline(
            workspace_service,
            session_factory,
            chapter_service,
            fake_ws_manager,
            cost_guard_config=_cost_config(per_workspace_daily_limit=10.0),
        )
        run_id = _start_range_run(service, workspace_id)
        service.execute(workspace_id, run_id)
        assert chapter_service.calls == ["ch-0001", "ch-0002", "ch-0003"]
        assert _run_state(session_factory, workspace_id, run_id).state == "stopped"
        assert fake_ws_manager.events_named("cost_guard_triggered") == []

    def test_warn_action_notifies_but_does_not_stop(
        self, workspace_service, session_factory, fake_ws_manager, workspace_id
    ):
        _seed_standard_book(session_factory, workspace_id)
        _seed_today_cost(session_factory, workspace_id, total_cost_usd=15.0)
        chapter_service = _FakeChapterService(session_factory)
        service = _make_pipeline(
            workspace_service,
            session_factory,
            chapter_service,
            fake_ws_manager,
            cost_guard_config=_cost_config(
                per_workspace_daily_limit=10.0, action="warn"
            ),
        )
        run_id = _start_range_run(service, workspace_id)
        service.execute(workspace_id, run_id)
        # 不停跑(3 章全写)+ 每章预检各发一次通知
        assert chapter_service.calls == ["ch-0001", "ch-0002", "ch-0003"]
        row = _run_state(session_factory, workspace_id, run_id)
        assert row.state == "stopped"
        assert len(fake_ws_manager.events_named("cost_guard_triggered")) == 3

    def test_none_config_disables_gate(
        self, workspace_service, session_factory, fake_ws_manager, workspace_id
    ):
        """未注入(None)= disable:超限也直行(CLI / legacy 构造回归保护)。"""
        _seed_standard_book(session_factory, workspace_id)
        _seed_today_cost(session_factory, workspace_id, total_cost_usd=99999.0)
        chapter_service = _FakeChapterService(session_factory)
        service = _make_pipeline(
            workspace_service,
            session_factory,
            chapter_service,
            fake_ws_manager,
            cost_guard_config=None,
        )
        run_id = _start_range_run(service, workspace_id)
        service.execute(workspace_id, run_id)
        assert chapter_service.calls == ["ch-0001", "ch-0002", "ch-0003"]
        assert fake_ws_manager.events_named("cost_guard_triggered") == []

    def test_resume_after_cost_pause_continues(
        self, workspace_service, session_factory, fake_ws_manager, workspace_id
    ):
        """成本低于限额(维护人调高限额 / 等效场景)后 resume 续跑完成 —
        paused 语义闭环。v2 w2-s2 起累计账语义下"次日清零"不再成立,真实
        解法 = ConfigPage 调高限额;测试以"成本归零"等价模拟(SUM 判定
        只看当前值与限额的相对关系)。"""
        _seed_standard_book(session_factory, workspace_id)
        _seed_today_cost(session_factory, workspace_id, total_cost_usd=15.0)
        chapter_service = _FakeChapterService(session_factory)
        service = _make_pipeline(
            workspace_service,
            session_factory,
            chapter_service,
            fake_ws_manager,
            cost_guard_config=_cost_config(per_workspace_daily_limit=10.0),
        )
        run_id = _start_range_run(service, workspace_id)
        service.execute(workspace_id, run_id)
        assert _run_state(session_factory, workspace_id, run_id).state == "paused"
        # 成本降到限额下(等价于维护人调高限额)
        _seed_today_cost(session_factory, workspace_id, total_cost_usd=0.0)
        service.resume(workspace_id)
        service.execute(workspace_id, run_id)
        row = _run_state(session_factory, workspace_id, run_id)
        assert row.state == "stopped"
        assert row.pause_reason == "completed"
        assert chapter_service.calls == ["ch-0001", "ch-0002", "ch-0003"]

    def test_chapter_gate_exception_pauses_even_with_skip_policy(
        self, workspace_service, session_factory, fake_ws_manager, workspace_id
    ):
        """章级 gate 抛 ``CostGuardTriggered`` → 特判 pause,不走 skip 策略
        (否则连环跳全章 → 假 completed)。"""
        _seed_standard_book(session_factory, workspace_id)
        chapter_service = _FakeChapterService(
            session_factory, raise_cost_guard=True
        )
        service = _make_pipeline(
            workspace_service, session_factory, chapter_service, fake_ws_manager
        )
        snap = service.start(
            workspace_id,
            scope="range",
            start_chapter_id="ch-0001",
            end_chapter_id="ch-0003",
            volume_id=None,
            profile_override=None,
            failure_policy="skip",
        )
        service.execute(workspace_id, snap.run_id)
        row = _run_state(session_factory, workspace_id, snap.run_id)
        assert row.state == "paused"
        assert row.pause_reason == "cost_guard_exceeded"
        # 只调了第一章(立即 pause),没有连环 skip
        assert chapter_service.calls == ["ch-0001"]
        assert row.skipped_chapter_ids == []


# =============================================================================
# ⑤ 看门狗与 reset_stale 协同(plan 点名:两机制设计自洽)
# =============================================================================


class TestWatchdogResetStaleSynergy:
    def test_timeout_then_reset_stale_then_resume_full_cycle(
        self, workspace_service, session_factory, fake_ws_manager, workspace_id
    ):
        """超时章重启后被自愈:watchdog 翻 interrupted(章留 reviewing)→
        reset_stale 自愈回 drafting(interrupted 不触发防误杀跳过)→
        resume 续跑该章至全书完成。"""
        _seed_standard_book(session_factory, workspace_id)
        chapter_service = _FakeChapterService(session_factory)
        service = _make_pipeline(
            workspace_service,
            session_factory,
            chapter_service,
            fake_ws_manager,
            watchdog_config=PipelineWatchdogConfig(chapter_timeout_seconds=100.0),
        )
        run_id = _start_range_run(service, workspace_id)
        # 模拟:循环正写 ch-0001 卡死(章停 reviewing,心跳超预算)
        _seed_chapter(
            session_factory, workspace_id, "ch-0001", status="reviewing"
        )
        now = time.monotonic()
        service._heartbeats[workspace_id] = _LoopHeartbeat(
            run_id=run_id,
            chapter_id="ch-0001",
            started_monotonic=now - 150.0,
        )
        # ① watchdog 翻态
        service._watchdog_tick(now)
        assert (
            _run_state(session_factory, workspace_id, run_id).state
            == "interrupted"
        )
        assert (
            _chapter_status(session_factory, workspace_id, "ch-0001")
            == "reviewing"
        )  # 章自然留非终态(不强杀不抢救,等 reset_stale)
        # ② 模拟卡死线程最终退出(心跳注销)+ 重启期 reset_stale 自愈
        del service._heartbeats[workspace_id]
        rewind = _make_rewind(workspace_service, session_factory)
        result = rewind.reset_stale(workspace_id)
        # interrupted 态不触发 active_pipeline_run 防误杀跳过
        assert result.skipped_reason is None
        assert result.reset_chapter_ids == ["ch-0001"]
        assert (
            _chapter_status(session_factory, workspace_id, "ch-0001")
            == "drafting"
        )
        # ③ resume 断点续跑:重选 ch-0001 写到全书完成
        service.resume(workspace_id)
        service.execute(workspace_id, run_id)
        row = _run_state(session_factory, workspace_id, run_id)
        assert row.state == "stopped"
        assert row.pause_reason == "completed"
        assert chapter_service.calls == ["ch-0001", "ch-0002", "ch-0003"]


# =============================================================================
# ⑥ PipelineWatchdogConfig 加载容错
# =============================================================================


class TestWatchdogConfigLoading:
    def test_missing_file_returns_defaults(self, tmp_path: Path):
        cfg = load_pipeline_watchdog_config(tmp_path / "nonexistent.yaml")
        assert cfg.chapter_timeout_seconds == 2400.0

    def test_missing_section_returns_defaults(self, tmp_path: Path):
        yaml_path = tmp_path / "config.yaml"
        yaml_path.write_text("cost_guard:\n  daily_usd_limit: 20.0\n")
        cfg = load_pipeline_watchdog_config(yaml_path)
        assert cfg.chapter_timeout_seconds == 2400.0

    def test_custom_value_loaded(self, tmp_path: Path):
        yaml_path = tmp_path / "config.yaml"
        yaml_path.write_text("pipeline:\n  chapter_timeout_seconds: 600\n")
        cfg = load_pipeline_watchdog_config(yaml_path)
        assert cfg.chapter_timeout_seconds == 600.0

    def test_invalid_type_raises(self, tmp_path: Path):
        yaml_path = tmp_path / "config.yaml"
        yaml_path.write_text("pipeline:\n  chapter_timeout_seconds: abc\n")
        with pytest.raises(ValidationError):
            load_pipeline_watchdog_config(yaml_path)

    def test_unknown_field_rejected(self, tmp_path: Path):
        yaml_path = tmp_path / "config.yaml"
        yaml_path.write_text("pipeline:\n  unknown_field: 1\n")
        with pytest.raises(ValidationError):
            load_pipeline_watchdog_config(yaml_path)

    def test_repo_root_config_yaml_has_section(self):
        """仓库根 config.yaml ``pipeline:`` 段防回退(w1-s3 加的字面)。"""
        cfg = load_pipeline_watchdog_config()
        assert cfg.chapter_timeout_seconds == 2400.0
