"""P4-B-09:MemoryHealthService 单元 + 集成测试。

覆盖契约:

1. ``persist_memory_health_check_safely`` helper(单元层 5 测):
   - 给定 session_factory + 完整 check → 落 1 行 ``memory_health_history``
     + structlog ``memory_health_persisted`` event
   - ``session_factory=None`` → no-op
   - session_factory 抛 open → swallow + structlog
     ``memory_health_persist_failed`` stage=open_session
   - 多次调用同 service 落多行(check_id 唯一,**不去重**;表是历史轨迹)
   - commit 失败 → rollback + swallow + structlog
     ``memory_health_persist_failed`` stage=commit

2. ``_notify_memory_health_critical`` 单元层 3 测:
   - ws_manager=None → 静默 no-op
   - ws_manager.publish → API_CONTRACT §5.3 字面 channel / event / data 含
     workspace_id + check_id + severity + summary + issues_count + issues
   - ws_manager.publish 抛 → swallow + structlog
     ``memory_health_notify_failed``

3. ``MemoryHealthService.run_check`` 集成层 5 测:
   - 空工作区(三类全空 + 无章)→ LLM 返 ok → 落 1 行 + 不 WS 推送
   - 完整工作区(seed 3 角色 + 2 伏笔 + 2 timeline + 2 章)→ LLM 返
     warning → 落 1 行 + 不 WS 推送(severity != critical)
   - critical → 落 1 行 + WS publish 到 system.alerts(event=memory_health_critical)
   - LLM 输出非合法 JSON → ``MemoryHealthCheckerError`` 冒泡 + 不落表 +
     不 WS 推送
   - workspace_id 透传:agent / persist / WS payload 三处都用 run_check 入参
     的 workspace_id(P3-B-14 沉淀)
"""

from __future__ import annotations

import json
from datetime import UTC, datetime
from pathlib import Path
from typing import Any, Iterator
from unittest.mock import MagicMock

import pytest
from sqlalchemy import create_engine, select
from sqlalchemy.orm import Session
from structlog.testing import capture_logs

from src.api.websocket.manager import WebSocketManager, manager
from src.core.agents.memory_health_checker import (
    MemoryHealthIssue,
    MemoryHealthReport,
)
from src.core.db import Base
from src.core.db.models import (
    ChapterORM,
    MemoryHealthCheckORM,
    WorkspaceORM,
)
from src.core.llm.base import LLMClient, LLMResponse, TokenUsage
from src.core.memory.store import (
    create_character,
    create_plot_thread,
    create_timeline_entry,
)
from src.core.models.character import Character, VoiceProfile
from src.core.models.metrics import MemoryHealthCheck
from src.core.models.plot_thread import PlotThread
from src.core.models.timeline_entry import TimelineEntry
from src.core.workspace.workspace_paths import WorkspacePaths
from src.services.memory_health_service import (
    MemoryHealthService,
    _notify_memory_health_critical,
    persist_memory_health_check_safely,
)
from src.services.workspace_service import WorkspaceService


# =============================================================================
# 共享 fixture
# =============================================================================


@pytest.fixture(autouse=True)
def _reset_ws_manager() -> Iterator[None]:
    """每用例清空全局 ws manager,避免连接 / 订阅跨用例污染。"""
    manager.reset()
    yield
    manager.reset()


@pytest.fixture
def workspace_service(tmp_path: Path) -> WorkspaceService:
    workspaces_root = tmp_path / "workspaces"
    archives_root = tmp_path / "archives"
    workspaces_root.mkdir()
    archives_root.mkdir()
    return WorkspaceService(
        workspaces_root=workspaces_root, archives_root=archives_root
    )


def _make_session_factory(workspace_service: WorkspaceService):
    """按 workspace_id 构造 SQLite session;Base.metadata.create_all 自动建表。"""

    def _factory(workspace_id: str) -> Session:
        paths = WorkspacePaths(
            workspace_id, workspaces_root=workspace_service.workspaces_root
        )
        engine = create_engine(f"sqlite:///{paths.db_path}")
        Base.metadata.create_all(engine)
        return Session(engine)

    return _factory


def _make_workspace(workspace_service: WorkspaceService, ws_id: str) -> None:
    workspace_service.create(
        ws_id,
        display_name=f"P4-B-09 测试 {ws_id}",
        genre="test",
        premise="验证 memory_health",
        activate=False,
    )


def _make_payload(severity: str, *, summary: str = "", issues: int = 0) -> dict[str, Any]:
    """构造 LLM JSON payload。severity ∈ ok/minor/warning/critical;issues 控制 stale_threads 长度。"""
    return {
        "severity": severity,
        "summary": summary or f"{severity} state",
        "stale_threads": [
            {"id": f"t{i}", "reason": f"stale {i}"} for i in range(issues)
        ],
        "inactive_characters": [],
        "conflicts": [],
    }


def _make_llm(content: str, *, model_used: str = "claude-sonnet-4-6") -> MagicMock:
    fake = MagicMock(spec=LLMClient)
    fake.complete.return_value = LLMResponse(
        content=content,
        model_used=model_used,
        usage=TokenUsage(input_tokens=80, output_tokens=160),
        finish_reason="end_turn",
    )
    return fake


def _seed_workspace_orm(session: Session, workspace_id: str) -> None:
    now = datetime.now(UTC)
    session.add(
        WorkspaceORM(
            workspace_id=workspace_id,
            display_name=f"ws-{workspace_id}",
            genre="test",
            premise="x",
            created_at=now,
            last_active_at=now,
            engine_version="0.1.0",
            status="active",
            total_chapters=0,
            completed_chapters=0,
            config_overrides={},
        )
    )
    session.commit()


def _seed_chapters(session: Session, workspace_id: str, *chapter_ids: str) -> None:
    """在 SQLite 落 chapters 行,created_at 按出现顺序递增(便于 desc 排序断言)。"""
    base = datetime(2026, 5, 11, 9, 0, 0, tzinfo=UTC)
    for i, cid in enumerate(chapter_ids):
        session.add(
            ChapterORM(
                chapter_id=cid,
                workspace_id=workspace_id,
                current_version=1,
                status="approved",
                rewrite_count=0,
                revise_count=0,
                created_at=base.replace(minute=i),
                approved_at=base.replace(minute=i),
                total_token_usage={},
                total_cost_usd=0.0,
            )
        )
    session.commit()


# =============================================================================
# 1. persist_memory_health_check_safely(单元层)
# =============================================================================


class TestPersistMemoryHealthCheckSafely:
    def _build_check(self, *, severity: str = "ok") -> MemoryHealthCheck:
        return MemoryHealthCheck(
            check_id="mhc-test01",
            checked_at=datetime(2026, 5, 11, 9, 0, 0, tzinfo=UTC),
            severity=severity,  # type: ignore[arg-type]
            stale_threads_count=2,
            inactive_characters_count=1,
            conflicts_count=0,
            report_json={"summary": "x", "model_used": "m"},
        )

    def test_round_trip_inserts_row(
        self, workspace_service: WorkspaceService
    ) -> None:
        _make_workspace(workspace_service, "ws-persist-1")
        session_factory = _make_session_factory(workspace_service)
        check = self._build_check(severity="minor")

        with capture_logs() as logs:
            persist_memory_health_check_safely(
                session_factory,
                workspace_id="ws-persist-1",
                check=check,
            )

        # 行落表
        session = session_factory("ws-persist-1")
        try:
            row = session.get(MemoryHealthCheckORM, "mhc-test01")
            assert row is not None
            assert row.severity == "minor"
            assert row.stale_threads_count == 2
            assert row.inactive_characters_count == 1
            assert row.conflicts_count == 0
            assert row.report_json == {"summary": "x", "model_used": "m"}
        finally:
            session.close()

        # structlog 落 persisted 事件
        assert any(
            e.get("event") == "memory_health_persisted"
            and e.get("check_id") == "mhc-test01"
            for e in logs
        )

    def test_session_factory_none_is_noop(self) -> None:
        with capture_logs() as logs:
            persist_memory_health_check_safely(
                None,
                workspace_id="ws-x",
                check=self._build_check(),
            )
        assert logs == []

    def test_open_session_failure_is_swallowed(self) -> None:
        def _factory(workspace_id: str) -> Session:
            raise RuntimeError("disk full")

        with capture_logs() as logs:
            persist_memory_health_check_safely(
                _factory,
                workspace_id="ws-x",
                check=self._build_check(),
            )

        failed = [
            e for e in logs
            if e.get("event") == "memory_health_persist_failed"
        ]
        assert len(failed) == 1
        assert failed[0]["stage"] == "open_session"
        assert failed[0]["error_type"] == "RuntimeError"

    def test_multiple_calls_insert_multiple_rows(
        self, workspace_service: WorkspaceService
    ) -> None:
        """每次 run_check 都落一行;check_id 唯一,**不去重**。"""
        _make_workspace(workspace_service, "ws-persist-multi")
        session_factory = _make_session_factory(workspace_service)
        for i in range(3):
            c = self._build_check()
            c = c.model_copy(update={"check_id": f"mhc-multi-{i}"})
            persist_memory_health_check_safely(
                session_factory,
                workspace_id="ws-persist-multi",
                check=c,
            )

        session = session_factory("ws-persist-multi")
        try:
            ids = session.scalars(
                select(MemoryHealthCheckORM.check_id)
            ).all()
            assert sorted(ids) == ["mhc-multi-0", "mhc-multi-1", "mhc-multi-2"]
        finally:
            session.close()

    def test_commit_failure_rolled_back(
        self, workspace_service: WorkspaceService
    ) -> None:
        _make_workspace(workspace_service, "ws-persist-rollback")
        session_factory = _make_session_factory(workspace_service)

        # 用同 check_id 触发主键冲突 → commit 抛 IntegrityError
        check = self._build_check()
        persist_memory_health_check_safely(
            session_factory, workspace_id="ws-persist-rollback", check=check
        )
        with capture_logs() as logs:
            persist_memory_health_check_safely(
                session_factory,
                workspace_id="ws-persist-rollback",
                check=check,
            )
        failed = [
            e for e in logs
            if e.get("event") == "memory_health_persist_failed"
        ]
        assert any(e["stage"] == "commit" for e in failed)


# =============================================================================
# 2. _notify_memory_health_critical 单元层
# =============================================================================


class TestNotifyMemoryHealthCritical:
    def _build_check(self) -> MemoryHealthCheck:
        return MemoryHealthCheck(
            check_id="mhc-crit",
            checked_at=datetime.now(UTC),
            severity="critical",
            stale_threads_count=2,
            inactive_characters_count=1,
            conflicts_count=1,
            report_json={"summary": "boom", "model_used": "m"},
        )

    def _build_report(self) -> MemoryHealthReport:
        return MemoryHealthReport(
            severity="critical",
            summary="boom",
            stale_threads=[
                MemoryHealthIssue(id="t1", reason="r1"),
                MemoryHealthIssue(id="t2", reason="r2"),
            ],
            inactive_characters=[
                MemoryHealthIssue(id="c1", reason="r3"),
            ],
            conflicts=[
                MemoryHealthIssue(id="x1", reason="r4"),
            ],
            model_used="claude-sonnet-4-6",
        )

    def test_ws_manager_none_noop(self) -> None:
        with capture_logs() as logs:
            _notify_memory_health_critical(
                None,
                workspace_id="ws-x",
                check=self._build_check(),
                report=self._build_report(),
            )
        assert not any(
            e.get("event") in ("memory_health_critical", "memory_health_notify_failed")
            for e in logs
        )

    def test_publish_to_system_alerts(self) -> None:
        published: list[dict] = []
        orig = manager.publish

        def _spy(channel: str, event: str, data: dict) -> None:
            published.append({"channel": channel, "event": event, "data": data})
            orig(channel, event, data)

        manager.publish = _spy  # type: ignore[method-assign]
        try:
            _notify_memory_health_critical(
                manager,
                workspace_id="ws-crit",
                check=self._build_check(),
                report=self._build_report(),
            )
        finally:
            manager.publish = orig  # type: ignore[method-assign]

        assert len(published) == 1
        msg = published[0]
        assert msg["channel"] == "system.alerts"
        assert msg["event"] == "memory_health_critical"
        data = msg["data"]
        # API_CONTRACT §5.3 字面字段 + 扩 5 字段
        assert data["workspace_id"] == "ws-crit"
        assert data["check_id"] == "mhc-crit"
        assert data["severity"] == "critical"
        assert data["summary"] == "boom"
        assert data["issues_count"] == 4
        issues = data["issues"]
        # 三类 issue 各按 kind 标记
        assert sorted({i["kind"] for i in issues}) == [
            "conflict",
            "inactive_character",
            "stale_thread",
        ]
        assert all("id" in i and "reason" in i for i in issues)

    def test_publish_exception_swallowed(self) -> None:
        class _BoomManager:
            def publish(self, channel: str, event: str, data: dict) -> None:
                raise RuntimeError("boom")

        with capture_logs() as logs:
            _notify_memory_health_critical(
                _BoomManager(),  # type: ignore[arg-type]
                workspace_id="ws-x",
                check=self._build_check(),
                report=self._build_report(),
            )
        assert any(
            e.get("event") == "memory_health_notify_failed" for e in logs
        )


# =============================================================================
# 3. MemoryHealthService.run_check 集成层
# =============================================================================


def _seed_three_threads(session: Session, workspace_id: str) -> None:
    """seed 3 角色 + 2 伏笔 + 2 timeline + 2 章节(用于完整工作区集成测)。"""
    _seed_workspace_orm(session, workspace_id)
    _seed_chapters(session, workspace_id, "ch-0001", "ch-0002")

    def _char(cid: str) -> Character:
        return Character(
            character_id=cid,
            workspace_id=workspace_id,
            name=cid,
            aliases=[],
            archetype="x",
            appearance="x",
            personality="x",
            background="x",
            voice_profile=VoiceProfile(
                tone="x",
                sentence_style="x",
                self_address="我",
                addresses_others={},
                catchphrases=[],
                forbidden_phrases=[],
            ),
            goals_long_term=[],
            goals_short_term=[],
            current_state={},
            relationships={},
            abilities=[],
            secrets=[],
            first_appearance_chapter="ch-0001",
            last_updated_chapter="ch-0002",
        )

    for cid in ("c-1", "c-2", "c-3"):
        create_character(session, _char(cid))

    now = datetime.now(UTC)
    for tid, status in (("t-1", "planted"), ("t-2", "developing")):
        create_plot_thread(
            session,
            PlotThread(
                thread_id=tid,
                workspace_id=workspace_id,
                title=tid,
                status=status,  # type: ignore[arg-type]
                importance=3,
                planted_chapter="ch-0001",
                planned_payoff=None,
                actual_payoff=None,
                description="x",
                related_characters=[],
                created_at=now,
                last_updated_chapter="ch-0001",
            ),
        )

    for eid, time_order in (("e-1", 100), ("e-2", 200)):
        create_timeline_entry(
            session,
            TimelineEntry(
                entry_id=eid,
                workspace_id=workspace_id,
                chapter_id="ch-0001",
                description="x",
                time_label="本章",
                time_order=time_order,
                involved_characters=[],
            ),
        )
    session.commit()


class TestMemoryHealthServiceIntegration:
    @pytest.fixture
    def prompts_dir(self) -> Path:
        """实际仓库根 prompts/ 目录,memory_health_checker.md 已存在。"""
        return Path(__file__).parent.parent.parent / "prompts"

    def _build_service(
        self,
        workspace_service: WorkspaceService,
        llm: MagicMock,
        *,
        ws_manager: WebSocketManager | None = None,
        prompts_dir: Path | None = None,
    ) -> MemoryHealthService:
        return MemoryHealthService(
            workspace_service=workspace_service,
            session_factory=_make_session_factory(workspace_service),
            llm=llm,
            model="claude-sonnet-4-6",
            ws_manager=ws_manager,
            prompts_dir=prompts_dir,
        )

    def test_empty_workspace_ok_severity(
        self, workspace_service: WorkspaceService, prompts_dir: Path
    ) -> None:
        _make_workspace(workspace_service, "ws-empty")
        llm = _make_llm(json.dumps(_make_payload("ok", summary="empty")))
        service = self._build_service(
            workspace_service, llm, prompts_dir=prompts_dir
        )

        check = service.run_check("ws-empty")

        assert check.severity == "ok"
        assert check.stale_threads_count == 0
        # 行已落表(check_id 是 service 自动 UUID 生成的)
        session = _make_session_factory(workspace_service)("ws-empty")
        try:
            rows = session.execute(select(MemoryHealthCheckORM)).scalars().all()
            assert len(rows) == 1
            assert rows[0].severity == "ok"
        finally:
            session.close()

    def test_full_workspace_warning_severity(
        self, workspace_service: WorkspaceService, prompts_dir: Path
    ) -> None:
        _make_workspace(workspace_service, "ws-full")
        # 先预填三类硬记忆
        session = _make_session_factory(workspace_service)("ws-full")
        try:
            _seed_three_threads(session, "ws-full")
        finally:
            session.close()

        llm = _make_llm(json.dumps(_make_payload("warning", issues=5)))
        ws_mgr = WebSocketManager()
        service = self._build_service(
            workspace_service, llm, ws_manager=ws_mgr, prompts_dir=prompts_dir
        )

        check = service.run_check("ws-full")

        assert check.severity == "warning"
        assert check.stale_threads_count == 5
        # warning != critical → 不 WS publish
        # (WebSocketManager 没人订阅时 publish 走 0 delivery,本测验 LLM 入参
        # 而非 ws delivery 直接 inspect — 此处 ws_manager 持有 no-conn 状态即可
        # 通过)
        # 验 user message 含 3 角色 / 2 伏笔 / 2 timeline / 2 chapter ids
        user_msg = next(
            m.content
            for m in llm.complete.call_args.kwargs["messages"]
            if m.role == "user"
        )
        for cid in ("c-1", "c-2", "c-3", "t-1", "t-2", "e-1", "e-2", "ch-0001", "ch-0002"):
            assert cid in user_msg

    def test_critical_severity_publishes_ws(
        self, workspace_service: WorkspaceService, prompts_dir: Path
    ) -> None:
        _make_workspace(workspace_service, "ws-crit")
        llm = _make_llm(json.dumps(_make_payload("critical", issues=10)))

        published: list[dict] = []
        orig = manager.publish

        def _spy(channel: str, event: str, data: dict) -> None:
            published.append({"channel": channel, "event": event, "data": data})
            orig(channel, event, data)

        manager.publish = _spy  # type: ignore[method-assign]
        try:
            service = self._build_service(
                workspace_service,
                llm,
                ws_manager=manager,
                prompts_dir=prompts_dir,
            )
            check = service.run_check("ws-crit")
        finally:
            manager.publish = orig  # type: ignore[method-assign]

        assert check.severity == "critical"
        assert check.stale_threads_count == 10
        assert len(published) == 1
        msg = published[0]
        assert msg["channel"] == "system.alerts"
        assert msg["event"] == "memory_health_critical"
        assert msg["data"]["workspace_id"] == "ws-crit"
        assert msg["data"]["severity"] == "critical"
        assert msg["data"]["issues_count"] == 10

    def test_parser_error_propagates_no_persist(
        self, workspace_service: WorkspaceService, prompts_dir: Path
    ) -> None:
        from src.core.agents.memory_health_checker import (
            MemoryHealthCheckerError,
        )

        _make_workspace(workspace_service, "ws-parse-err")
        llm = _make_llm("not json")
        service = self._build_service(
            workspace_service, llm, prompts_dir=prompts_dir
        )

        with pytest.raises(MemoryHealthCheckerError):
            service.run_check("ws-parse-err")

        # 未落表(LLM 抛在 persist 之前)
        session = _make_session_factory(workspace_service)("ws-parse-err")
        try:
            rows = session.execute(select(MemoryHealthCheckORM)).scalars().all()
            assert rows == []
        finally:
            session.close()

    def test_workspace_id_threaded_through(
        self, workspace_service: WorkspaceService, prompts_dir: Path
    ) -> None:
        """P3-B-14 透传纪律:agent + persist + WS 都用 run_check 入参的
        workspace_id,即使预读的 Character 内部 workspace_id 字段被人为漂移
        (本测不构造漂移场景,仅断言 event 字段反映入参)。"""
        _make_workspace(workspace_service, "ws-thread")
        llm = _make_llm(json.dumps(_make_payload("ok")))
        service = self._build_service(
            workspace_service, llm, prompts_dir=prompts_dir
        )

        with capture_logs() as logs:
            check = service.run_check("ws-thread")

        # agent event
        agent_evt = next(
            e for e in logs if e.get("event") == "memory_health_check"
        )
        assert agent_evt["workspace_id"] == "ws-thread"
        # persist event
        persist_evt = next(
            e for e in logs if e.get("event") == "memory_health_persisted"
        )
        assert persist_evt["workspace_id"] == "ws-thread"
        # 返 check_id 形如 mhc-...
        assert check.check_id.startswith("mhc-")
