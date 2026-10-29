"""``PipelineService`` Run 循环语义防回退测试(v2 w1-s1)。

覆盖 v2_plan §4 w1-s1 "产出的防回退护栏" 4 项:

① **孤儿章停跑测(治理性,governance §2.30)**:write 返回
   ``HUMAN_INTERVENTION`` → run paused(reason=``chapter_escalated``)+
   该章**不进 completed** + 下一章**零调用** — 治 30 章长跑实证的
   "孤儿章带病续跑 / 衔接接计划非实际"(v2_plan §2.2 缺口 1)。
② **失败策略三档路由测**:stop / skip / retry 各自状态转移(缺口 4)。
③ **卡生成接线全链路测**:循环 → card_service 调用(缺卡生成 + draft 自动
   approve + rejected 不绕过)+ 失败路径(缺口 3)。
④ Alembic 0023 升降测在 ``tests/db/test_v2_w1_s1_0023_failure_policy.py``。

Fake 设计:``_FakeChapterService.write`` 按章返回行为序列(ok / orphan /
异常),模拟孤儿章·重试场景;``_FakeCardService`` 记录 generate / approve
调用供接线断言。卡状态种子直接写 ``ChapterCardORM`` 行(``_ensure_chapter_
card_ready`` 以 ORM 为真相源,对齐 write() card_status gate 同源)。
"""

from __future__ import annotations

from collections.abc import Callable
from datetime import datetime, timezone
from pathlib import Path
from types import SimpleNamespace
from typing import Any

import pytest
from sqlalchemy import create_engine
from sqlalchemy.orm import Session
from sqlalchemy.pool import StaticPool

from src.core.db import Base
from src.core.db.models import ChapterCardORM, VolumeORM, WorkspaceORM
from src.core.state_machine.states import ChapterState
from src.services.attempt_ledger import (
    AttemptLedgerStore,
    ProviderCallCapExceeded,
)
from src.services.chapter_card_service import (
    CardContinuityBridgeRequiredError,
    CardContinuityLedgerStaleError,
    CardInvalidStateTransitionError,
)
from src.services.pipeline_service import PipelineService
from src.services.workspace_service import WorkspaceService


# ---- Fakes ------------------------------------------------------------------


def _ok_result() -> SimpleNamespace:
    """成功章 stub(``final_state=APPROVED``)。"""
    return SimpleNamespace(
        final_state=ChapterState.APPROVED, escalation_reason=None
    )


def _orphan_result(reason: str = "rewrite_budget_exhausted") -> SimpleNamespace:
    """孤儿章 stub(``final_state=HUMAN_INTERVENTION``,正常返回不抛错 —
    对齐 ``chapter_service.write`` 真实行为)。"""
    return SimpleNamespace(
        final_state=ChapterState.HUMAN_INTERVENTION,
        escalation_reason=reason,
    )


class _FakeChapterService:
    """ChapterService stub:``behaviors`` 按 chapter_id 给行为序列。

    每章行为是 list(每次 write 消费一个,耗尽后默认 ok),元素:
    ``"ok"`` / ``"orphan"`` / Exception 实例(写时 raise)。
    未配置的章默认 ok。
    """

    def __init__(
        self,
        *,
        behaviors: dict[str, list[Any]] | None = None,
    ) -> None:
        self.calls: list[tuple[str, str, str | None]] = []
        self._behaviors = behaviors or {}

    def write(
        self,
        workspace_id: str,
        chapter_id: str,
        *,
        on_state_transition=None,
        profile_override: str | None = None,
    ) -> Any:
        self.calls.append((workspace_id, chapter_id, profile_override))
        queue = self._behaviors.get(chapter_id)
        behavior: Any = "ok"
        if queue:
            behavior = queue.pop(0)
        if isinstance(behavior, Exception):
            raise behavior
        if behavior == "orphan":
            return _orphan_result()
        return _ok_result()

    def calls_for(self, chapter_id: str) -> int:
        return sum(1 for _, ch, _ in self.calls if ch == chapter_id)


class _FakeCardService:
    """ChapterCardService stub:记录 generate / approve 调用供接线断言。

    - ``fail_generate_for``:集合内的章 generate 抛 RuntimeError。
    - ``fail_approve_for``:集合内的章 approve 抛 RuntimeError。
    - ``already_approved_for``:集合内的章 approve 抛
      ``CardInvalidStateTransitionError``(已 approved 竞态,应幂等放行)。
    """

    def __init__(self) -> None:
        self.generate_calls: list[dict[str, Any]] = []
        self.approve_calls: list[str] = []
        self.fail_generate_for: set[str] = set()
        self.fail_approve_for: set[str] = set()
        self.already_approved_for: set[str] = set()
        self.stale_once_for: set[str] = set()
        self.bridge_once_for: set[str] = set()
        self.regenerate_calls: list[str] = []
        self.bridge_regenerate_calls: list[tuple[str, list[str]]] = []

    def generate_next_card(
        self,
        workspace_id: str,
        *,
        chapter_id: str,
        task_id: str,
    ) -> dict[str, Any]:
        self.generate_calls.append(
            {
                "workspace_id": workspace_id,
                "chapter_id": chapter_id,
                "task_id": task_id,
            }
        )
        if chapter_id in self.fail_generate_for:
            raise RuntimeError("LLM 卡生成失败(fake)")
        return {
            "cards_generated": [chapter_id],
            "cards_failed": [],
        }

    def transition_card_status(
        self,
        workspace_id: str,
        chapter_id: str,
        *,
        action: str,
        rejection_notes: str | None = None,
    ) -> dict[str, Any]:
        self.approve_calls.append(chapter_id)
        if chapter_id in self.stale_once_for:
            self.stale_once_for.remove(chapter_id)
            raise CardContinuityLedgerStaleError(
                chapter_id=chapter_id,
                reason="旧预生成卡缺 provenance",
                frozen=None,
                current=None,
            )
        if chapter_id in self.bridge_once_for:
            self.bridge_once_for.remove(chapter_id)
            raise CardContinuityBridgeRequiredError(
                chapter_id=chapter_id,
                reason="continuity_bridge 未逐字承接即时未完动作",
                required_actions=["确认低频共振是否预示步骤即将触发"],
            )
        if chapter_id in self.already_approved_for:
            raise CardInvalidStateTransitionError(
                chapter_id=chapter_id,
                current_status="approved",
                action="approve",
                allowed_sources=("draft", "pending_review", "rejected"),
            )
        if chapter_id in self.fail_approve_for:
            raise RuntimeError("approve 失败(fake)")
        return {
            "chapter_id": chapter_id,
            "from_status": "draft",
            "to_status": "approved",
        }

    def regenerate_stale_unapproved_card(
        self,
        workspace_id: str,
        chapter_id: str,
    ) -> SimpleNamespace:
        self.regenerate_calls.append(chapter_id)
        return SimpleNamespace(chapter_id=chapter_id, workspace_id=workspace_id)

    def regenerate_continuity_bridge_unapproved_card(
        self,
        workspace_id: str,
        chapter_id: str,
        *,
        required_actions: list[str],
    ) -> SimpleNamespace:
        self.bridge_regenerate_calls.append((chapter_id, list(required_actions)))
        return SimpleNamespace(chapter_id=chapter_id, workspace_id=workspace_id)


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
        "ws-loop-sem",
        display_name="v2 w1-s1 run loop semantics test",
        genre="xianxia",
        premise="test",
        activate=False,
    )
    return "ws-loop-sem"


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


@pytest.fixture
def fake_card_service() -> _FakeCardService:
    return _FakeCardService()


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


def _seed_card(
    session_factory: Callable[[str], Session],
    workspace_id: str,
    chapter_id: str,
    *,
    card_status: str,
) -> None:
    """种 ChapterCardORM 行(卡状态真相源,对齐 write() gate 同源)。"""
    session = session_factory(workspace_id)
    try:
        session.merge(
            ChapterCardORM(
                chapter_id=chapter_id,
                workspace_id=workspace_id,
                card_status=card_status,
                created_at=datetime.now(timezone.utc),
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
    for ch in chapter_ids:
        _seed_card(
            session_factory, workspace_id, ch, card_status="approved"
        )


def _make_service(
    workspace_service: WorkspaceService,
    session_factory: Callable[[str], Session],
    chapter_service: _FakeChapterService,
    ws_manager: _FakeWSManager,
    card_service: _FakeCardService | None,
) -> PipelineService:
    return PipelineService(
        chapter_service=chapter_service,  # type: ignore[arg-type]
        workspace_service=workspace_service,
        session_factory=session_factory,
        ws_manager=ws_manager,  # type: ignore[arg-type]
        chapter_card_service=card_service,  # type: ignore[arg-type]
    )


def _start_run(
    service: PipelineService,
    workspace_id: str,
    *,
    failure_policy: str = "stop",
) -> str:
    """启动 range run(ch-0001..ch-0003)返回 run_id。"""
    snap = service.start(
        workspace_id,
        scope="range",
        start_chapter_id="ch-0001",
        end_chapter_id="ch-0003",
        volume_id=None,
        profile_override=None,
        failure_policy=failure_policy,  # type: ignore[arg-type]
    )
    return snap.run_id


# =============================================================================
# ① 孤儿章停跑(治理性护栏,governance §2.30)
# =============================================================================


class TestOrphanChapterStopPolicy:
    """write 升 human_intervention(正常返回不抛错)→ stop 策略停跑。

    治理语义:孤儿章**不进 completed** + **下一章零调用** — 防"带病续跑/
    下一章衔接接计划非实际"回归(v2_plan §2.2 缺口 1 / 30 章长跑实证)。
    """

    def test_orphan_pauses_run_not_completed_next_chapter_zero_calls(
        self,
        workspace_service: WorkspaceService,
        workspace_id: str,
        session_factory: Callable[[str], Session],
        fake_ws_manager: _FakeWSManager,
        fake_card_service: _FakeCardService,
    ) -> None:
        _seed_workspace_and_volume(session_factory, workspace_id)
        _seed_all_cards_approved(
            session_factory, workspace_id, ["ch-0001", "ch-0002", "ch-0003"]
        )
        chapter_service = _FakeChapterService(
            behaviors={"ch-0002": ["orphan"]}
        )
        service = _make_service(
            workspace_service,
            session_factory,
            chapter_service,
            fake_ws_manager,
            fake_card_service,
        )
        run_id = _start_run(service, workspace_id, failure_policy="stop")
        service.execute(workspace_id, run_id)

        snap = service.status(workspace_id)
        # run 暂停 + 原因 = chapter_escalated(可人工解决后 resume)
        assert snap.state == "paused"
        assert snap.pause_reason == "chapter_escalated"
        # 孤儿章不进 completed(衔接不接计划)
        assert snap.completed_chapter_ids == ["ch-0001"]
        assert "ch-0002" not in snap.completed_chapter_ids
        # 下一章零调用(停在孤儿章,不带病续跑)
        assert chapter_service.calls_for("ch-0003") == 0
        # current_chapter 停在孤儿章供 UI 直达
        assert snap.current_chapter == "ch-0002"

    def test_orphan_publishes_paused_event_with_blocked_chapter(
        self,
        workspace_service: WorkspaceService,
        workspace_id: str,
        session_factory: Callable[[str], Session],
        fake_ws_manager: _FakeWSManager,
        fake_card_service: _FakeCardService,
    ) -> None:
        _seed_workspace_and_volume(session_factory, workspace_id)
        _seed_all_cards_approved(
            session_factory, workspace_id, ["ch-0001", "ch-0002", "ch-0003"]
        )
        chapter_service = _FakeChapterService(
            behaviors={"ch-0001": ["orphan"]}
        )
        service = _make_service(
            workspace_service,
            session_factory,
            chapter_service,
            fake_ws_manager,
            fake_card_service,
        )
        run_id = _start_run(service, workspace_id, failure_policy="stop")
        service.execute(workspace_id, run_id)

        paused = [
            e
            for e in fake_ws_manager.events
            if e["event"] == "pipeline_paused"
        ]
        assert len(paused) == 1
        assert paused[0]["data"]["reason"] == "chapter_escalated"
        assert paused[0]["data"]["chapter_id"] == "ch-0001"

    def test_orphan_resume_after_manual_fix_continues_run(
        self,
        workspace_service: WorkspaceService,
        workspace_id: str,
        session_factory: Callable[[str], Session],
        fake_ws_manager: _FakeWSManager,
        fake_card_service: _FakeCardService,
    ) -> None:
        """pause 不是死路:人工解决后 resume + execute 从孤儿章续跑。"""
        _seed_workspace_and_volume(session_factory, workspace_id)
        _seed_all_cards_approved(
            session_factory, workspace_id, ["ch-0001", "ch-0002", "ch-0003"]
        )
        # ch-0002 第一次孤儿(→pause),第二次(人工解决后 resume)成功
        chapter_service = _FakeChapterService(
            behaviors={"ch-0002": ["orphan", "ok"]}
        )
        service = _make_service(
            workspace_service,
            session_factory,
            chapter_service,
            fake_ws_manager,
            fake_card_service,
        )
        run_id = _start_run(service, workspace_id, failure_policy="stop")
        service.execute(workspace_id, run_id)
        assert service.status(workspace_id).state == "paused"

        service.resume(workspace_id)
        service.execute(workspace_id, run_id)
        snap = service.status(workspace_id)
        assert snap.state == "stopped"
        assert snap.pause_reason == "completed"
        assert snap.completed_chapter_ids == [
            "ch-0001",
            "ch-0002",
            "ch-0003",
        ]


# =============================================================================
# ② 失败策略三档路由
# =============================================================================


class TestFailurePolicySkip:
    def test_orphan_skip_records_and_continues_to_completion(
        self,
        workspace_service: WorkspaceService,
        workspace_id: str,
        session_factory: Callable[[str], Session],
        fake_ws_manager: _FakeWSManager,
        fake_card_service: _FakeCardService,
    ) -> None:
        _seed_workspace_and_volume(session_factory, workspace_id)
        _seed_all_cards_approved(
            session_factory, workspace_id, ["ch-0001", "ch-0002", "ch-0003"]
        )
        chapter_service = _FakeChapterService(
            behaviors={"ch-0002": ["orphan"]}
        )
        service = _make_service(
            workspace_service,
            session_factory,
            chapter_service,
            fake_ws_manager,
            fake_card_service,
        )
        run_id = _start_run(service, workspace_id, failure_policy="skip")
        service.execute(workspace_id, run_id)

        snap = service.status(workspace_id)
        assert snap.state == "stopped"
        assert snap.pause_reason == "completed"
        assert snap.completed_chapter_ids == ["ch-0001", "ch-0003"]
        assert snap.skipped_chapter_ids == ["ch-0002"]
        # 跳过章不进 completed;ch-0003 正常写
        assert chapter_service.calls_for("ch-0003") == 1

    def test_write_exception_skip_continues(
        self,
        workspace_service: WorkspaceService,
        workspace_id: str,
        session_factory: Callable[[str], Session],
        fake_ws_manager: _FakeWSManager,
        fake_card_service: _FakeCardService,
    ) -> None:
        """skip 策略下写章异常不再 error 全停(vs P6-B-02 既有行为)。"""
        _seed_workspace_and_volume(session_factory, workspace_id)
        _seed_all_cards_approved(
            session_factory, workspace_id, ["ch-0001", "ch-0002", "ch-0003"]
        )
        chapter_service = _FakeChapterService(
            behaviors={"ch-0002": [RuntimeError("LLM boom")]}
        )
        service = _make_service(
            workspace_service,
            session_factory,
            chapter_service,
            fake_ws_manager,
            fake_card_service,
        )
        run_id = _start_run(service, workspace_id, failure_policy="skip")
        service.execute(workspace_id, run_id)

        snap = service.status(workspace_id)
        assert snap.state == "stopped"
        assert snap.completed_chapter_ids == ["ch-0001", "ch-0003"]
        assert snap.skipped_chapter_ids == ["ch-0002"]


class TestFailurePolicyRetry:
    def test_historical_attempts_do_not_consume_new_invocation_retry_window(
        self,
        workspace_service: WorkspaceService,
        workspace_id: str,
        session_factory: Callable[[str], Session],
        fake_ws_manager: _FakeWSManager,
        fake_card_service: _FakeCardService,
    ) -> None:
        """历史 attempt 只审计；修复后新 invocation 仍有一次自动 retry。"""

        _seed_workspace_and_volume(
            session_factory,
            workspace_id,
            chapter_count=1,
        )
        _seed_all_cards_approved(
            session_factory,
            workspace_id,
            ["ch-0001"],
        )
        bootstrap = _make_service(
            workspace_service,
            session_factory,
            _FakeChapterService(),
            fake_ws_manager,
            fake_card_service,
        )
        run = bootstrap.start(
            workspace_id,
            scope="single",
            start_chapter_id="ch-0001",
            end_chapter_id=None,
            volume_id=None,
            profile_override=None,
            failure_policy="retry",
        )
        ledger = AttemptLedgerStore(session_factory)
        for error_type in ("ReadTimeout", "ValidatorQuoteError"):
            historical = ledger.begin_pipeline_attempt(
                workspace_id=workspace_id,
                run_id=run.run_id,
                chapter_id="ch-0001",
            )
            ledger.finish_pipeline_attempt(
                workspace_id=workspace_id,
                attempt_id=historical.attempt_id,
                succeeded=False,
                error_type=error_type,
            )

        restarted_chapter = _FakeChapterService(
            behaviors={"ch-0001": [RuntimeError("fresh failure"), "ok"]}
        )
        restarted = _make_service(
            workspace_service,
            session_factory,
            restarted_chapter,
            fake_ws_manager,
            fake_card_service,
        )
        restarted.execute(workspace_id, run.run_id)

        status = restarted.status(workspace_id)
        assert status.state == "stopped"
        assert status.pipeline_attempt_count == 4
        assert status.completed_chapter_ids == ["ch-0001"]
        assert restarted_chapter.calls_for("ch-0001") == 2

    def test_retry_then_success_completes_with_two_calls(
        self,
        workspace_service: WorkspaceService,
        workspace_id: str,
        session_factory: Callable[[str], Session],
        fake_ws_manager: _FakeWSManager,
        fake_card_service: _FakeCardService,
    ) -> None:
        _seed_workspace_and_volume(session_factory, workspace_id)
        _seed_all_cards_approved(
            session_factory, workspace_id, ["ch-0001", "ch-0002", "ch-0003"]
        )
        # ch-0002 第一次孤儿,重试成功
        chapter_service = _FakeChapterService(
            behaviors={"ch-0002": ["orphan", "ok"]}
        )
        service = _make_service(
            workspace_service,
            session_factory,
            chapter_service,
            fake_ws_manager,
            fake_card_service,
        )
        run_id = _start_run(service, workspace_id, failure_policy="retry")
        service.execute(workspace_id, run_id)

        snap = service.status(workspace_id)
        assert snap.state == "stopped"
        assert snap.completed_chapter_ids == [
            "ch-0001",
            "ch-0002",
            "ch-0003",
        ]
        assert snap.skipped_chapter_ids == []
        assert chapter_service.calls_for("ch-0002") == 2

    def test_retry_exhausted_orphan_falls_back_to_pause(
        self,
        workspace_service: WorkspaceService,
        workspace_id: str,
        session_factory: Callable[[str], Session],
        fake_ws_manager: _FakeWSManager,
        fake_card_service: _FakeCardService,
    ) -> None:
        _seed_workspace_and_volume(session_factory, workspace_id)
        _seed_all_cards_approved(
            session_factory, workspace_id, ["ch-0001", "ch-0002", "ch-0003"]
        )
        # 两次都孤儿 → 重试名额(1 次)耗尽 → 按 stop 处置(pause)
        chapter_service = _FakeChapterService(
            behaviors={"ch-0002": ["orphan", "orphan"]}
        )
        service = _make_service(
            workspace_service,
            session_factory,
            chapter_service,
            fake_ws_manager,
            fake_card_service,
        )
        run_id = _start_run(service, workspace_id, failure_policy="retry")
        service.execute(workspace_id, run_id)

        snap = service.status(workspace_id)
        assert snap.state == "paused"
        assert snap.pause_reason == "chapter_escalated"
        assert snap.completed_chapter_ids == ["ch-0001"]
        assert chapter_service.calls_for("ch-0002") == 2
        assert chapter_service.calls_for("ch-0003") == 0

    def test_retry_exhausted_exception_falls_back_to_error(
        self,
        workspace_service: WorkspaceService,
        workspace_id: str,
        session_factory: Callable[[str], Session],
        fake_ws_manager: _FakeWSManager,
        fake_card_service: _FakeCardService,
    ) -> None:
        _seed_workspace_and_volume(session_factory, workspace_id)
        _seed_all_cards_approved(
            session_factory, workspace_id, ["ch-0001", "ch-0002", "ch-0003"]
        )
        chapter_service = _FakeChapterService(
            behaviors={
                "ch-0002": [RuntimeError("boom1"), RuntimeError("boom2")]
            }
        )
        service = _make_service(
            workspace_service,
            session_factory,
            chapter_service,
            fake_ws_manager,
            fake_card_service,
        )
        run_id = _start_run(service, workspace_id, failure_policy="retry")
        service.execute(workspace_id, run_id)

        snap = service.status(workspace_id)
        assert snap.state == "error"
        assert snap.error_details is not None
        assert snap.error_details["chapter_id"] == "ch-0002"


class TestProviderCallCap:
    def test_cap_exception_pauses_before_failure_policy_retry(
        self,
        workspace_service: WorkspaceService,
        workspace_id: str,
        session_factory: Callable[[str], Session],
        fake_ws_manager: _FakeWSManager,
        fake_card_service: _FakeCardService,
    ) -> None:
        """活动 hard cap 是全局停止线，不能被章级 retry/skip 绕过。"""

        _seed_workspace_and_volume(
            session_factory,
            workspace_id,
            chapter_count=1,
        )
        _seed_all_cards_approved(
            session_factory,
            workspace_id,
            ["ch-0001"],
        )
        chapter_service = _FakeChapterService(
            behaviors={
                "ch-0001": [
                    ProviderCallCapExceeded(
                        workspace_id=workspace_id,
                        used=1,
                        cap=1,
                    )
                ]
            }
        )
        service = _make_service(
            workspace_service,
            session_factory,
            chapter_service,
            fake_ws_manager,
            fake_card_service,
        )
        run = service.start(
            workspace_id,
            scope="single",
            start_chapter_id="ch-0001",
            end_chapter_id=None,
            volume_id=None,
            profile_override=None,
            failure_policy="retry",
            provider_call_cap=1,
        )
        service.execute(workspace_id, run.run_id)

        status = service.status(workspace_id)
        assert status.state == "paused"
        assert status.pause_reason == "provider_call_cap_exceeded"
        assert status.provider_call_cap == 1
        assert status.pipeline_attempt_count == 1
        assert chapter_service.calls_for("ch-0001") == 1


class TestFailurePolicyStopExceptionKeepsLegacyError:
    """stop(默认)策略下写章异常仍走 error 全停 — P6-B-02 既有行为回归红线。"""

    def test_write_exception_stop_marks_error(
        self,
        workspace_service: WorkspaceService,
        workspace_id: str,
        session_factory: Callable[[str], Session],
        fake_ws_manager: _FakeWSManager,
        fake_card_service: _FakeCardService,
    ) -> None:
        _seed_workspace_and_volume(session_factory, workspace_id)
        _seed_all_cards_approved(
            session_factory, workspace_id, ["ch-0001", "ch-0002", "ch-0003"]
        )
        chapter_service = _FakeChapterService(
            behaviors={"ch-0002": [RuntimeError("boom")]}
        )
        service = _make_service(
            workspace_service,
            session_factory,
            chapter_service,
            fake_ws_manager,
            fake_card_service,
        )
        run_id = _start_run(service, workspace_id, failure_policy="stop")
        service.execute(workspace_id, run_id)

        snap = service.status(workspace_id)
        assert snap.state == "error"
        assert snap.error_details is not None
        assert snap.error_details["error_type"] == "RuntimeError"
        assert chapter_service.calls_for("ch-0003") == 0


# =============================================================================
# ③ 卡生成入循环(接线全链路)
# =============================================================================


class TestCardGenerationInLoop:
    def test_missing_card_generates_one_card_approves_then_writes(
        self,
        workspace_service: WorkspaceService,
        workspace_id: str,
        session_factory: Callable[[str], Session],
        fake_ws_manager: _FakeWSManager,
        fake_card_service: _FakeCardService,
    ) -> None:
        """缺卡 → generate_next_card(本章) + approve + write。"""
        _seed_workspace_and_volume(session_factory, workspace_id)
        # 不种任何卡行 = 3 章全缺卡
        chapter_service = _FakeChapterService()
        service = _make_service(
            workspace_service,
            session_factory,
            chapter_service,
            fake_ws_manager,
            fake_card_service,
        )
        run_id = _start_run(service, workspace_id)
        service.execute(workspace_id, run_id)

        snap = service.status(workspace_id)
        assert snap.state == "stopped"
        assert snap.completed_chapter_ids == [
            "ch-0001",
            "ch-0002",
            "ch-0003",
        ]
        # 每章一次单卡生成 + 一次 approve,顺序逐章
        assert [c["chapter_id"] for c in fake_card_service.generate_calls] == [
            "ch-0001",
            "ch-0002",
            "ch-0003",
        ]
        assert fake_card_service.approve_calls == [
            "ch-0001",
            "ch-0002",
            "ch-0003",
        ]

    def test_approved_card_zero_card_service_calls(
        self,
        workspace_service: WorkspaceService,
        workspace_id: str,
        session_factory: Callable[[str], Session],
        fake_ws_manager: _FakeWSManager,
        fake_card_service: _FakeCardService,
    ) -> None:
        """卡已 approved → 零 generate / 零 approve(不重复调卡服务)。"""
        _seed_workspace_and_volume(session_factory, workspace_id)
        _seed_all_cards_approved(
            session_factory, workspace_id, ["ch-0001", "ch-0002", "ch-0003"]
        )
        chapter_service = _FakeChapterService()
        service = _make_service(
            workspace_service,
            session_factory,
            chapter_service,
            fake_ws_manager,
            fake_card_service,
        )
        run_id = _start_run(service, workspace_id)
        service.execute(workspace_id, run_id)

        assert service.status(workspace_id).state == "stopped"
        assert fake_card_service.generate_calls == []
        assert fake_card_service.approve_calls == []

    def test_draft_card_auto_approved_no_generate(
        self,
        workspace_service: WorkspaceService,
        workspace_id: str,
        session_factory: Callable[[str], Session],
        fake_ws_manager: _FakeWSManager,
        fake_card_service: _FakeCardService,
    ) -> None:
        """draft 卡 → 自动 approve(driver 实证模式产品化),不重复生成。"""
        _seed_workspace_and_volume(session_factory, workspace_id)
        for ch in ["ch-0001", "ch-0002", "ch-0003"]:
            _seed_card(session_factory, workspace_id, ch, card_status="draft")
        chapter_service = _FakeChapterService()
        service = _make_service(
            workspace_service,
            session_factory,
            chapter_service,
            fake_ws_manager,
            fake_card_service,
        )
        run_id = _start_run(service, workspace_id)
        service.execute(workspace_id, run_id)

        assert service.status(workspace_id).state == "stopped"
        assert fake_card_service.generate_calls == []
        assert fake_card_service.approve_calls == [
            "ch-0001",
            "ch-0002",
            "ch-0003",
        ]

    def test_stale_pre_generated_draft_regenerates_from_current_ledger(
        self,
        workspace_service: WorkspaceService,
        workspace_id: str,
        session_factory: Callable[[str], Session],
        fake_ws_manager: _FakeWSManager,
        fake_card_service: _FakeCardService,
    ) -> None:
        """预生成草稿缺 provenance → 到章时重生成一次，再批准后才写。"""

        _seed_workspace_and_volume(session_factory, workspace_id)
        for chapter_id in ["ch-0001", "ch-0002", "ch-0003"]:
            _seed_card(
                session_factory,
                workspace_id,
                chapter_id,
                card_status="draft",
            )
        fake_card_service.stale_once_for.add("ch-0002")
        chapter_service = _FakeChapterService()
        service = _make_service(
            workspace_service,
            session_factory,
            chapter_service,
            fake_ws_manager,
            fake_card_service,
        )

        run_id = _start_run(service, workspace_id)
        service.execute(workspace_id, run_id)

        assert service.status(workspace_id).state == "stopped"
        assert fake_card_service.regenerate_calls == ["ch-0002"]
        assert fake_card_service.approve_calls == [
            "ch-0001",
            "ch-0002",
            "ch-0002",
            "ch-0003",
        ]
        assert chapter_service.calls_for("ch-0002") == 1

    def test_stale_regeneration_bridge_failure_gets_one_targeted_retry(
        self,
        workspace_service: WorkspaceService,
        workspace_id: str,
        session_factory: Callable[[str], Session],
        fake_ws_manager: _FakeWSManager,
        fake_card_service: _FakeCardService,
    ) -> None:
        """预生成卡先因 ledger 陈旧重生，再用 bridge 拒绝证据定向重试。"""

        _seed_workspace_and_volume(session_factory, workspace_id)
        for chapter_id in ["ch-0001", "ch-0002", "ch-0003"]:
            _seed_card(
                session_factory,
                workspace_id,
                chapter_id,
                card_status="draft",
            )
        fake_card_service.stale_once_for.add("ch-0002")
        fake_card_service.bridge_once_for.add("ch-0002")
        chapter_service = _FakeChapterService()
        service = _make_service(
            workspace_service,
            session_factory,
            chapter_service,
            fake_ws_manager,
            fake_card_service,
        )

        run_id = _start_run(service, workspace_id)
        service.execute(workspace_id, run_id)

        assert service.status(workspace_id).state == "stopped"
        assert fake_card_service.regenerate_calls == ["ch-0002"]
        assert fake_card_service.bridge_regenerate_calls == [
            ("ch-0002", ["确认低频共振是否预示步骤即将触发"])
        ]
        assert fake_card_service.approve_calls == [
            "ch-0001",
            "ch-0002",
            "ch-0002",
            "ch-0002",
            "ch-0003",
        ]
        assert chapter_service.calls_for("ch-0002") == 1

    def test_failed_targeted_bridge_retry_is_marked_exhausted(
        self,
        workspace_service: WorkspaceService,
        workspace_id: str,
        session_factory: Callable[[str], Session],
        fake_ws_manager: _FakeWSManager,
        fake_card_service: _FakeCardService,
    ) -> None:
        """定向重试后的二次失败必须落 exhausted，禁止以后再 Continue 重放。"""

        _seed_workspace_and_volume(session_factory, workspace_id)
        for chapter_id in ["ch-0001", "ch-0002", "ch-0003"]:
            _seed_card(
                session_factory,
                workspace_id,
                chapter_id,
                card_status="draft",
            )
        fake_card_service.bridge_once_for.add("ch-0001")
        fake_card_service.fail_approve_for.add("ch-0001")
        chapter_service = _FakeChapterService()
        service = _make_service(
            workspace_service,
            session_factory,
            chapter_service,
            fake_ws_manager,
            fake_card_service,
        )

        run_id = _start_run(service, workspace_id)
        service.execute(workspace_id, run_id)

        snap = service.status(workspace_id)
        assert snap.state == "error"
        assert snap.error_details is not None
        assert snap.error_details["continuity_bridge_retry_exhausted"] is True
        assert snap.error_details["recovery_ready"] is False
        assert fake_card_service.bridge_regenerate_calls == [
            ("ch-0001", ["确认低频共振是否预示步骤即将触发"])
        ]
        assert chapter_service.calls == []

    def test_rejected_card_pauses_run_write_zero_calls(
        self,
        workspace_service: WorkspaceService,
        workspace_id: str,
        session_factory: Callable[[str], Session],
        fake_ws_manager: _FakeWSManager,
        fake_card_service: _FakeCardService,
    ) -> None:
        """rejected 卡(用户明确拒过)不自动绕过 → pause + 该章零写调用。"""
        _seed_workspace_and_volume(session_factory, workspace_id)
        _seed_card(
            session_factory, workspace_id, "ch-0001", card_status="approved"
        )
        _seed_card(
            session_factory, workspace_id, "ch-0002", card_status="rejected"
        )
        _seed_card(
            session_factory, workspace_id, "ch-0003", card_status="approved"
        )
        chapter_service = _FakeChapterService()
        service = _make_service(
            workspace_service,
            session_factory,
            chapter_service,
            fake_ws_manager,
            fake_card_service,
        )
        run_id = _start_run(service, workspace_id, failure_policy="stop")
        service.execute(workspace_id, run_id)

        snap = service.status(workspace_id)
        assert snap.state == "paused"
        assert snap.pause_reason == "card_rejected"
        # rejected 卡的章:不生成 / 不 approve / 不写
        assert fake_card_service.generate_calls == []
        assert fake_card_service.approve_calls == []
        assert chapter_service.calls_for("ch-0002") == 0
        assert chapter_service.calls_for("ch-0003") == 0

    def test_card_generation_failure_stop_marks_error(
        self,
        workspace_service: WorkspaceService,
        workspace_id: str,
        session_factory: Callable[[str], Session],
        fake_ws_manager: _FakeWSManager,
        fake_card_service: _FakeCardService,
    ) -> None:
        _seed_workspace_and_volume(session_factory, workspace_id)
        fake_card_service.fail_generate_for.add("ch-0001")
        chapter_service = _FakeChapterService()
        service = _make_service(
            workspace_service,
            session_factory,
            chapter_service,
            fake_ws_manager,
            fake_card_service,
        )
        run_id = _start_run(service, workspace_id, failure_policy="stop")
        service.execute(workspace_id, run_id)

        snap = service.status(workspace_id)
        assert snap.state == "error"
        assert snap.error_details is not None
        assert snap.error_details["reason"] == "card_generation_failed"
        assert chapter_service.calls == []

    def test_card_generation_failure_skip_continues(
        self,
        workspace_service: WorkspaceService,
        workspace_id: str,
        session_factory: Callable[[str], Session],
        fake_ws_manager: _FakeWSManager,
        fake_card_service: _FakeCardService,
    ) -> None:
        """卡生成失败同走失败策略:skip → 记账继续(plan 卡字面)。"""
        _seed_workspace_and_volume(session_factory, workspace_id)
        fake_card_service.fail_generate_for.add("ch-0002")
        chapter_service = _FakeChapterService()
        service = _make_service(
            workspace_service,
            session_factory,
            chapter_service,
            fake_ws_manager,
            fake_card_service,
        )
        run_id = _start_run(service, workspace_id, failure_policy="skip")
        service.execute(workspace_id, run_id)

        snap = service.status(workspace_id)
        assert snap.state == "stopped"
        assert snap.completed_chapter_ids == ["ch-0001", "ch-0003"]
        assert snap.skipped_chapter_ids == ["ch-0002"]

    def test_already_approved_transition_race_is_idempotent(
        self,
        workspace_service: WorkspaceService,
        workspace_id: str,
        session_factory: Callable[[str], Session],
        fake_ws_manager: _FakeWSManager,
        fake_card_service: _FakeCardService,
    ) -> None:
        """approve 撞 CardInvalidStateTransitionError(已 approved 竞态)
        = 幂等放行,照常写章。"""
        _seed_workspace_and_volume(session_factory, workspace_id)
        for ch in ["ch-0001", "ch-0002", "ch-0003"]:
            _seed_card(session_factory, workspace_id, ch, card_status="draft")
        fake_card_service.already_approved_for.update(
            {"ch-0001", "ch-0002", "ch-0003"}
        )
        chapter_service = _FakeChapterService()
        service = _make_service(
            workspace_service,
            session_factory,
            chapter_service,
            fake_ws_manager,
            fake_card_service,
        )
        run_id = _start_run(service, workspace_id)
        service.execute(workspace_id, run_id)

        snap = service.status(workspace_id)
        assert snap.state == "stopped"
        assert snap.completed_chapter_ids == [
            "ch-0001",
            "ch-0002",
            "ch-0003",
        ]

    def test_no_card_service_injected_legacy_passthrough(
        self,
        workspace_service: WorkspaceService,
        workspace_id: str,
        session_factory: Callable[[str], Session],
        fake_ws_manager: _FakeWSManager,
    ) -> None:
        """card_service 未注入(legacy / lifespan builder)→ 跳过卡保障
        直接写(向后兼容,P6-B-02 既有测试同形)。"""
        _seed_workspace_and_volume(session_factory, workspace_id)
        chapter_service = _FakeChapterService()
        service = _make_service(
            workspace_service,
            session_factory,
            chapter_service,
            fake_ws_manager,
            card_service=None,
        )
        run_id = _start_run(service, workspace_id)
        service.execute(workspace_id, run_id)

        snap = service.status(workspace_id)
        assert snap.state == "stopped"
        assert snap.completed_chapter_ids == [
            "ch-0001",
            "ch-0002",
            "ch-0003",
        ]


# =============================================================================
# failure_policy 持久化 + snapshot 暴露
# =============================================================================


class TestFailurePolicyPersistence:
    def test_start_persists_failure_policy_and_snapshot_exposes(
        self,
        workspace_service: WorkspaceService,
        workspace_id: str,
        session_factory: Callable[[str], Session],
        fake_ws_manager: _FakeWSManager,
        fake_card_service: _FakeCardService,
    ) -> None:
        _seed_workspace_and_volume(session_factory, workspace_id)
        chapter_service = _FakeChapterService()
        service = _make_service(
            workspace_service,
            session_factory,
            chapter_service,
            fake_ws_manager,
            fake_card_service,
        )
        snap = service.start(
            workspace_id,
            scope="single",
            start_chapter_id="ch-0001",
            end_chapter_id=None,
            volume_id=None,
            profile_override=None,
            failure_policy="skip",
        )
        assert snap.failure_policy == "skip"
        assert snap.skipped_chapter_ids == []
        # status 读回同值(ORM 持久化)
        assert service.status(workspace_id).failure_policy == "skip"

    def test_default_failure_policy_is_stop(
        self,
        workspace_service: WorkspaceService,
        workspace_id: str,
        session_factory: Callable[[str], Session],
        fake_ws_manager: _FakeWSManager,
        fake_card_service: _FakeCardService,
    ) -> None:
        _seed_workspace_and_volume(session_factory, workspace_id)
        chapter_service = _FakeChapterService()
        service = _make_service(
            workspace_service,
            session_factory,
            chapter_service,
            fake_ws_manager,
            fake_card_service,
        )
        snap = service.start(
            workspace_id,
            scope="single",
            start_chapter_id="ch-0001",
            end_chapter_id=None,
            volume_id=None,
            profile_override=None,
        )
        assert snap.failure_policy == "stop"

    def test_idle_snapshot_has_stop_policy_empty_skipped(
        self,
        workspace_service: WorkspaceService,
        workspace_id: str,
        session_factory: Callable[[str], Session],
        fake_ws_manager: _FakeWSManager,
        fake_card_service: _FakeCardService,
    ) -> None:
        chapter_service = _FakeChapterService()
        service = _make_service(
            workspace_service,
            session_factory,
            chapter_service,
            fake_ws_manager,
            fake_card_service,
        )
        snap = service.status(workspace_id)
        assert snap.state == "idle"
        assert snap.failure_policy == "stop"
        assert snap.skipped_chapter_ids == []
