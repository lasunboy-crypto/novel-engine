"""``PipelineService`` 单元测试(P6-B-02)。

覆盖维度(对齐 plan §10 测试约定 5 类):

- **happy**:start / pause / resume / stop / status 各 1+ 测覆盖状态翻转 +
  snapshot 字段完整 + WS event publish
- **error**:3 错误码各 1 测(``PipelineAlreadyRunningError`` /
  ``PipelineNotRunningError`` / ``PipelineInvalidScopeError`` + 解析时各种
  非法 scope 字段组合)
- **gate**:execute 循环撞 invalidated → 自动 pause + reason=chapter_invalidated
- **rollback**:chapter_service.write 抛通用异常 → state=error + error_details
- **WS event**:每 happy path 配套断言 publish 事件名 + data
"""

from __future__ import annotations

from collections.abc import Callable
from copy import deepcopy
from datetime import datetime, timezone
from decimal import Decimal
from pathlib import Path
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
    ChapterVersionORM,
    PipelineRunORM,
    VolumeORM,
    WorkspaceORM,
)
from src.core.llm.base import LLMAccessError
from src.core.routing.run_model_manifest_router import (
    RunModelManifestBinding,
    SelectionTestLossBreakerBinding,
)
from src.services.attempt_ledger import (
    AttemptLedgerStore,
    AttemptScope,
    bind_attempt_scope,
    current_attempt_scope,
)
from src.services.chapter_rewind_service import ChapterInvalidatedNeedsUnblockError
from src.services.chapter_service import (
    ContinuityCheckpointCorruptionError,
    ContinuityLedgerNotReadyError,
    MemoryEnrichmentNotReadyError,
)
from src.services.pipeline_service import (
    PipelineAlreadyRunningError,
    PipelineInvalidScopeError,
    PipelineNotRunningError,
    PipelineService,
    PipelineServiceError,
)
from src.services.workspace_service import WorkspaceService


def _seed_failed_reviewer(
    session_factory: Callable[[str], Session],
    workspace_id: str,
    run_id: str,
    error_type: str,
    *,
    agent_name: str = "reviewer",
) -> None:
    now = datetime.now(timezone.utc)
    with session_factory(workspace_id) as session:
        session.add(
            AttemptLedgerORM(
                attempt_id="att-reviewer-disconnected",
                workspace_id=workspace_id,
                run_id=run_id,
                task_id="task-reviewer",
                chapter_id="ch-0001",
                attempt_kind="provider",
                operation_kind="write",
                logical_call_id="call-reviewer",
                agent_name=agent_name,
                provider="mock",
                model="mock",
                attempt_number=1,
                attempt_cap=3,
                status="failed",
                error_type=error_type,
                usage_status="unknown",
                created_at=now,
                completed_at=now,
            )
        )
        session.commit()


# ---- Fakes ------------------------------------------------------------------


class _FakeChapterService:
    """ChapterService stub:write 默认成功;可注入 raise 模拟 invalidated /
    其他异常路径。"""

    def __init__(
        self,
        *,
        raise_on: dict[str, Exception] | None = None,
        checkpoint_raise_on: dict[str, Exception] | None = None,
        checkpoint_resume_on: set[str] | None = None,
        checkpoint_recovers_memory_on: set[str] | None = None,
        ready_raise_on: dict[str, Exception] | None = None,
        memory_raise_on: dict[str, Exception] | None = None,
        release_operation_task_result: bool = True,
    ) -> None:
        self.calls: list[tuple[str, str, str | None]] = []
        self.checkpoint_calls: list[str] = []
        self.ready_calls: list[str] = []
        self.memory_calls: list[str] = []
        self._raise_on = raise_on or {}
        self._checkpoint_raise_on = checkpoint_raise_on or {}
        self._checkpoint_resume_on = checkpoint_resume_on or set()
        self._checkpoint_recovers_memory_on = (
            checkpoint_recovers_memory_on or set()
        )
        self._ready_raise_on = ready_raise_on or {}
        self._memory_raise_on = memory_raise_on or {}
        self._release_operation_task_result = release_operation_task_result
        self.release_operation_task_calls: list[tuple[str, str, str]] = []

    def resume_approved_checkpoint(
        self,
        workspace_id: str,
        chapter_id: str,
        *,
        profile_override: str | None = None,
    ) -> bool:
        del workspace_id, profile_override
        self.checkpoint_calls.append(chapter_id)
        if chapter_id in self._checkpoint_raise_on:
            raise self._checkpoint_raise_on[chapter_id]
        resumed = chapter_id in self._checkpoint_resume_on
        if resumed and chapter_id in self._checkpoint_recovers_memory_on:
            self._memory_raise_on.pop(chapter_id, None)
        return resumed

    def require_ready_for_next_chapter(
        self, workspace_id: str, chapter_id: str
    ) -> None:
        del workspace_id
        self.ready_calls.append(chapter_id)
        if chapter_id in self._ready_raise_on:
            raise self._ready_raise_on[chapter_id]

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
        return None  # OrchestratorResult stub — pipeline 不读 result

    def require_memory_enrichment_succeeded(
        self, workspace_id: str, chapter_id: str
    ) -> None:
        del workspace_id
        self.memory_calls.append(chapter_id)
        if chapter_id in self._memory_raise_on:
            raise self._memory_raise_on[chapter_id]

    def release_operation_task_if_never_dispatched(
        self,
        workspace_id: str,
        chapter_id: str,
        *,
        task_id: str,
    ) -> bool:
        self.release_operation_task_calls.append((workspace_id, chapter_id, task_id))
        return self._release_operation_task_result


class _ManifestChapterService(_FakeChapterService):
    def __init__(self, *, token: str = "run-model-manifest:test:v1") -> None:
        super().__init__()
        self.binding = RunModelManifestBinding(
            manifest_id="deepseek-v4-flash",
            token=token,
            selection_test=SelectionTestLossBreakerBinding(
                activity_scope="selection_test",
                token=f"selection-test:{token}:v1",
                max_physical_requests=7,
                max_configured_rmb=Decimal("2.50"),
            ),
        )
        self.observed_attempt_scopes: list[AttemptScope] = []

    def active_run_model_manifest_binding(
        self, workspace_id: str
    ) -> RunModelManifestBinding:
        del workspace_id
        return self.binding

    def write(
        self,
        workspace_id: str,
        chapter_id: str,
        *,
        on_state_transition=None,
        profile_override: str | None = None,
    ) -> Any:
        scope = current_attempt_scope()
        assert scope is not None
        self.observed_attempt_scopes.append(scope)
        return super().write(
            workspace_id,
            chapter_id,
            on_state_transition=on_state_transition,
            profile_override=profile_override,
        )


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
        "ws-pipe-svc",
        display_name="P6-B-02 pipeline service test",
        genre="xianxia",
        premise="test",
        activate=False,
    )
    return "ws-pipe-svc"


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


def _seed_workspace_and_volume(
    session_factory: Callable[[str], Session],
    workspace_id: str,
    *,
    volumes: list[tuple[str, int]] | None = None,
) -> None:
    """种 WorkspaceORM + 多卷 VolumeORM。

    ``volumes`` 默认 ``[("vol-1", 3)]`` = 1 卷 3 章(ch-0001..ch-0003)。
    多卷例:``[("vol-1", 2), ("vol-2", 3)]`` = 5 章(ch-0001..ch-0005,前 2 章
    属 vol-1,后 3 章属 vol-2)。
    """
    vols = volumes if volumes is not None else [("vol-1", 3)]
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
                total_chapters=sum(c for _, c in vols),
                completed_chapters=0,
                config_overrides={},
            )
        )
        for vid, count in vols:
            session.merge(
                VolumeORM(
                    volume_id=vid,
                    workspace_id=workspace_id,
                    title=vid.upper(),
                    summary="s",
                    chapter_count=count,
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


def _seed_card_status(
    session_factory: Callable[[str], Session],
    workspace_service: WorkspaceService,
    workspace_id: str,
    chapter_id: str,
    *,
    status: str,
) -> None:
    """写入卡状态索引和最小文件 SSOT；PipelineService fake 不解析卡内容。"""

    now = datetime.now(timezone.utc)
    session = session_factory(workspace_id)
    try:
        session.merge(
            ChapterCardORM(
                chapter_id=chapter_id,
                workspace_id=workspace_id,
                card_status=status,
                card_approved_at=now if status == "approved" else None,
                created_at=now,
                rejection_notes=None,
            )
        )
        session.commit()
    finally:
        session.close()
    card_dir = workspace_service.workspaces_root / workspace_id / "data" / "cards"
    card_dir.mkdir(parents=True, exist_ok=True)
    (card_dir / f"{chapter_id}.json").write_text("{}", encoding="utf-8")


def _seed_reviewing_version(
    session_factory: Callable[[str], Session],
    workspace_service: WorkspaceService,
    workspace_id: str,
    chapter_id: str,
    *,
    version_id: int = 4,
    content: str = "已落库但尚未审稿的正文",
    chapter_status: str = "reviewing",
) -> None:
    """构造 Provider 拒绝前已持久化、尚未审稿的正文 checkpoint。"""

    now = datetime.now(timezone.utc)
    session = session_factory(workspace_id)
    try:
        session.add(
            ChapterORM(
                chapter_id=chapter_id,
                workspace_id=workspace_id,
                current_version=version_id,
                status=chapter_status,
                rewrite_count=2,
                revise_count=0,
                created_at=now,
                approved_at=None,
                total_token_usage={},
                total_cost_usd=0.0,
            )
        )
        session.add(
            ChapterVersionORM(
                chapter_id=chapter_id,
                version_id=version_id,
                content=content,
                word_count=len(content),
                created_at=now,
                creation_type="rewrite",
                model_used="qwen3.7-max",
                review_score=None,
                notes="",
            )
        )
        session.commit()
    finally:
        session.close()
    chapter_dir = (
        workspace_service.workspaces_root
        / workspace_id
        / "data"
        / "chapters"
        / chapter_id
    )
    chapter_dir.mkdir(parents=True, exist_ok=True)
    (chapter_dir / f"v{version_id}.md").write_text(content, encoding="utf-8")


@pytest.fixture
def fake_ws_manager() -> _FakeWSManager:
    return _FakeWSManager()


@pytest.fixture
def fake_chapter_service() -> _FakeChapterService:
    return _FakeChapterService()


@pytest.fixture
def service(
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


# ---- happy path ------------------------------------------------------------


class TestStartScope:
    def test_start_single_returns_snapshot_with_one_chapter(
        self,
        service: PipelineService,
        workspace_id: str,
        session_factory: Callable[[str], Session],
        fake_ws_manager: _FakeWSManager,
    ) -> None:
        _seed_workspace_and_volume(session_factory, workspace_id)
        snap = service.start(
            workspace_id,
            scope="single",
            start_chapter_id="ch-0001",
            end_chapter_id=None,
            volume_id=None,
            profile_override=None,
        )
        assert snap.state == "running"
        assert snap.chapter_ids == ["ch-0001"]
        assert snap.scope == "single"
        assert snap.run_id.startswith("run_")
        assert snap.progress == 0.0
        # WS event
        assert any(e["event"] == "pipeline_started" for e in fake_ws_manager.events)

    def test_start_range_returns_inclusive_chapter_list(
        self,
        service: PipelineService,
        workspace_id: str,
        session_factory: Callable[[str], Session],
    ) -> None:
        _seed_workspace_and_volume(
            session_factory, workspace_id, volumes=[("vol-1", 5)]
        )
        snap = service.start(
            workspace_id,
            scope="range",
            start_chapter_id="ch-0002",
            end_chapter_id="ch-0004",
            volume_id=None,
            profile_override=None,
        )
        assert snap.chapter_ids == ["ch-0002", "ch-0003", "ch-0004"]

    def test_start_volume_returns_all_chapters_in_volume(
        self,
        service: PipelineService,
        workspace_id: str,
        session_factory: Callable[[str], Session],
    ) -> None:
        _seed_workspace_and_volume(
            session_factory,
            workspace_id,
            volumes=[("vol-1", 2), ("vol-2", 3)],
        )
        snap = service.start(
            workspace_id,
            scope="volume",
            start_chapter_id=None,
            end_chapter_id=None,
            volume_id="vol-2",
            profile_override=None,
        )
        assert snap.chapter_ids == ["ch-0003", "ch-0004", "ch-0005"]

    def test_start_until_with_explicit_end(
        self,
        service: PipelineService,
        workspace_id: str,
        session_factory: Callable[[str], Session],
    ) -> None:
        _seed_workspace_and_volume(
            session_factory, workspace_id, volumes=[("vol-1", 5)]
        )
        snap = service.start(
            workspace_id,
            scope="until",
            start_chapter_id="ch-0002",
            end_chapter_id="ch-0003",
            volume_id=None,
            profile_override=None,
        )
        assert snap.chapter_ids == ["ch-0002", "ch-0003"]

    def test_start_until_with_no_end_defaults_to_volume_end(
        self,
        service: PipelineService,
        workspace_id: str,
        session_factory: Callable[[str], Session],
    ) -> None:
        """自决项 6:until + end_chapter_id=None → 默认到 start 所在卷末。"""
        _seed_workspace_and_volume(
            session_factory,
            workspace_id,
            volumes=[("vol-1", 3), ("vol-2", 3)],
        )
        # start=ch-0002 属 vol-1(chapters ch-0001/ch-0002/ch-0003)
        snap = service.start(
            workspace_id,
            scope="until",
            start_chapter_id="ch-0002",
            end_chapter_id=None,
            volume_id=None,
            profile_override=None,
        )
        # 应到 vol-1 末章 ch-0003 为止
        assert snap.chapter_ids == ["ch-0002", "ch-0003"]


class TestPauseResumeStop:
    def test_pause_then_resume_round_trip(
        self,
        service: PipelineService,
        workspace_id: str,
        session_factory: Callable[[str], Session],
        fake_ws_manager: _FakeWSManager,
    ) -> None:
        _seed_workspace_and_volume(session_factory, workspace_id)
        service.start(
            workspace_id,
            scope="single",
            start_chapter_id="ch-0001",
            end_chapter_id=None,
            volume_id=None,
            profile_override=None,
        )
        paused = service.pause(workspace_id)
        assert paused.state == "paused"
        assert paused.pause_reason == "user_request"
        assert any(e["event"] == "pipeline_paused" for e in fake_ws_manager.events)
        resumed, transitioned = service.resume_with_transition(workspace_id)
        assert transitioned is True
        assert resumed.state == "running"
        assert resumed.pause_reason is None
        resumed_again, transitioned_again = service.resume_with_transition(workspace_id)
        assert resumed_again.run_id == resumed.run_id
        assert transitioned_again is False
        assert [e["event"] for e in fake_ws_manager.events].count(
            "pipeline_resumed"
        ) == 1

    def test_resume_closes_orphan_pipeline_attempt_but_not_provider(
        self,
        service: PipelineService,
        workspace_id: str,
        session_factory: Callable[[str], Session],
    ) -> None:
        _seed_workspace_and_volume(session_factory, workspace_id)
        started = service.start(
            workspace_id,
            scope="single",
            start_chapter_id="ch-0001",
            end_chapter_id=None,
            volume_id=None,
            profile_override=None,
        )
        ledger = AttemptLedgerStore(session_factory)
        pipeline = ledger.begin_pipeline_attempt(
            workspace_id=workspace_id,
            run_id=started.run_id,
            chapter_id="ch-0001",
        )
        provider_id = ledger.begin_provider_attempt(
            scope=AttemptScope(
                workspace_id=workspace_id,
                run_id=started.run_id,
                task_id="task-reviewer",
                chapter_id="ch-0001",
                provider_call_cap=10,
            ),
            logical_call_id="logical-reviewer",
            operation_kind="agent_call",
            agent_name="reviewer",
            provider="openai_compatible",
            model="qwen-test",
            technical_attempt=1,
        )
        service.pause(workspace_id)

        resumed, transitioned = service.resume_with_transition(workspace_id)

        assert transitioned is True
        assert resumed.run_id == started.run_id
        session = session_factory(workspace_id)
        try:
            pipeline_row = session.get(AttemptLedgerORM, pipeline.attempt_id)
            provider_row = session.get(AttemptLedgerORM, provider_id)
        finally:
            session.close()
        assert pipeline_row is not None
        assert pipeline_row.status == "failed"
        assert pipeline_row.error_type == "PipelineInterrupted"
        assert pipeline_row.version_after is None
        assert provider_row is not None
        assert provider_row.status == "started"
        assert provider_row.completed_at is None

    def test_resume_allows_ready_current_with_running_memory_without_replay(
        self,
        service: PipelineService,
        workspace_id: str,
        session_factory: Callable[[str], Session],
    ) -> None:
        """合法 ready+running 按双水位降级续跑，不在 resume 重排 Worker。"""
        _seed_workspace_and_volume(session_factory, workspace_id)
        started = service.start(
            workspace_id,
            scope="single",
            start_chapter_id="ch-0001",
            end_chapter_id=None,
            volume_id=None,
            profile_override=None,
        )
        session = session_factory(workspace_id)
        try:
            now = datetime.now(timezone.utc)
            session.add(
                ChapterORM(
                    chapter_id="ch-0001",
                    workspace_id=workspace_id,
                    current_version=9,
                    status="approved",
                    rewrite_count=6,
                    revise_count=2,
                    created_at=now,
                    approved_at=now,
                    total_token_usage={},
                    total_cost_usd=0.0,
                    ready_for_next_chapter_at=now,
                    memory_enrichment_status="running",
                    memory_enrichment_details={
                        "started_at": "2026-08-02T12:30:11+00:00",
                        "tasks": [],
                    },
                )
            )
            run = session.get(PipelineRunORM, started.run_id)
            assert run is not None
            run.state = "paused"
            run.pause_reason = "user_request"
            run.current_chapter = "ch-0001"
            session.commit()
        finally:
            session.close()

        resumed, transitioned = service.resume_with_transition(workspace_id)

        assert transitioned is True
        assert resumed.state == "running"
        assert resumed.current_chapter == "ch-0001"
        session = session_factory(workspace_id)
        try:
            attempts = list(
                session.scalars(
                    select(AttemptLedgerORM).where(
                        AttemptLedgerORM.run_id == started.run_id
                    )
                )
            )
        finally:
            session.close()
        assert attempts == []

    @pytest.mark.parametrize(
        ("corruption", "error_pattern"),
        [
            ("wrong_workspace", "workspace checkpoint 串位"),
            ("outside_scope", "不属于合法 chapter_ids scope"),
        ],
    )
    def test_resume_preserves_corrupt_current_chapter_before_cas_or_attempt(
        self,
        service: PipelineService,
        workspace_id: str,
        session_factory: Callable[[str], Session],
        fake_ws_manager: _FakeWSManager,
        corruption: str,
        error_pattern: str,
    ) -> None:
        """current 串位或越出 run scope 均须零副作用硬停。"""
        _seed_workspace_and_volume(session_factory, workspace_id)
        started = service.start(
            workspace_id,
            scope="single",
            start_chapter_id="ch-0001",
            end_chapter_id=None,
            volume_id=None,
            profile_override=None,
        )
        session = session_factory(workspace_id)
        try:
            now = datetime.now(timezone.utc)
            run = session.get(PipelineRunORM, started.run_id)
            assert run is not None
            run.state = "paused"
            run.pause_reason = "user_request"
            run.current_chapter = (
                "ch-outside-scope" if corruption == "outside_scope" else "ch-0001"
            )
            if corruption == "wrong_workspace":
                session.add(
                    WorkspaceORM(
                        workspace_id="ws-other",
                        display_name="other",
                        genre="x",
                        premise="p",
                        created_at=now,
                        last_active_at=now,
                        engine_version="0.1.0",
                        status="active",
                        total_chapters=1,
                        completed_chapters=0,
                        config_overrides={},
                    )
                )
                session.add(
                    ChapterORM(
                        chapter_id="ch-0001",
                        workspace_id="ws-other",
                        current_version=1,
                        status="approved",
                        rewrite_count=0,
                        revise_count=0,
                        created_at=now,
                        approved_at=now,
                        total_token_usage={},
                        total_cost_usd=0.0,
                    )
                )
            session.commit()
            before = {
                column.name: deepcopy(getattr(run, column.name))
                for column in PipelineRunORM.__table__.columns
            }
        finally:
            session.close()
        event_count_before = len(fake_ws_manager.events)

        with pytest.raises(
            ContinuityCheckpointCorruptionError,
            match=error_pattern,
        ):
            service.resume_with_transition(workspace_id)

        assert len(fake_ws_manager.events) == event_count_before
        session = session_factory(workspace_id)
        try:
            run = session.get(PipelineRunORM, started.run_id)
            assert run is not None
            after = {
                column.name: deepcopy(getattr(run, column.name))
                for column in PipelineRunORM.__table__.columns
            }
            assert after == before
            attempts = list(
                session.scalars(
                    select(AttemptLedgerORM).where(
                        AttemptLedgerORM.run_id == started.run_id
                    )
                )
            )
        finally:
            session.close()
        assert attempts == []

    def test_resume_before_first_write_allows_missing_chapter_row_without_artifacts(
        self,
        service: PipelineService,
        workspace_id: str,
        session_factory: Callable[[str], Session],
    ) -> None:
        """run 已选章但 Worker 尚未首写时，零产物缺行是合法 checkpoint。"""

        _seed_workspace_and_volume(session_factory, workspace_id)
        started = service.start(
            workspace_id,
            scope="single",
            start_chapter_id="ch-0001",
            end_chapter_id=None,
            volume_id=None,
            profile_override=None,
        )
        with session_factory(workspace_id) as session:
            run = session.get(PipelineRunORM, started.run_id)
            assert run is not None
            run.state = "paused"
            run.pause_reason = "user_request"
            run.current_chapter = "ch-0001"
            session.commit()

        snapshot, transitioned = service.resume_with_transition(workspace_id)

        assert transitioned is True
        assert snapshot.state == "running"
        assert snapshot.current_chapter == "ch-0001"
        with session_factory(workspace_id) as session:
            assert session.get(ChapterORM, "ch-0001") is None
            attempts = list(
                session.scalars(
                    select(AttemptLedgerORM).where(
                        AttemptLedgerORM.run_id == started.run_id
                    )
                )
            )
        assert attempts == []

    @pytest.mark.parametrize(
        ("corruption", "expected_pattern"),
        [
            ({"chapter_ids": "ch-0001"}, "chapter_ids 必须是 list"),
            ({"chapter_ids": []}, "chapter_ids 不得为空"),
            (
                {"chapter_ids": ["ch-0001", ""]},
                "chapter_ids 只能包含非空字符串",
            ),
            (
                {"chapter_ids": ["ch-0001", "ch-0001"]},
                "chapter_ids 包含重复章节",
            ),
            (
                {"completed_chapter_ids": "ch-0001"},
                "completed_chapter_ids 必须是 list",
            ),
            (
                {"completed_chapter_ids": [""]},
                "completed_chapter_ids 只能包含非空字符串",
            ),
            (
                {"completed_chapter_ids": ["ch-0001", "ch-0001"]},
                "completed_chapter_ids 包含重复章节",
            ),
            (
                {"completed_chapter_ids": ["ch-9999"]},
                "completed_chapter_ids 含有 chapter_ids scope 外章节",
            ),
            (
                {"skipped_chapter_ids": "ch-0001"},
                "skipped_chapter_ids 必须是 list",
            ),
            (
                {"skipped_chapter_ids": [0]},
                "skipped_chapter_ids 只能包含非空字符串",
            ),
            (
                {"skipped_chapter_ids": ["ch-0002", "ch-0002"]},
                "skipped_chapter_ids 包含重复章节",
            ),
            (
                {"skipped_chapter_ids": ["ch-9999"]},
                "skipped_chapter_ids 含有 chapter_ids scope 外章节",
            ),
            (
                {
                    "completed_chapter_ids": ["ch-0001"],
                    "skipped_chapter_ids": ["ch-0001"],
                },
                "completed_chapter_ids 与 skipped_chapter_ids 必须互斥",
            ),
        ],
    )
    def test_resume_validates_all_scope_lists_before_cas_or_attempt(
        self,
        service: PipelineService,
        workspace_id: str,
        session_factory: Callable[[str], Session],
        fake_ws_manager: _FakeWSManager,
        fake_chapter_service: _FakeChapterService,
        corruption: dict[str, object],
        expected_pattern: str,
    ) -> None:
        """current=None 也必须先封闭验证三份 scope 列表。"""
        _seed_workspace_and_volume(session_factory, workspace_id)
        started = service.start(
            workspace_id,
            scope="range",
            start_chapter_id="ch-0001",
            end_chapter_id="ch-0003",
            volume_id=None,
            profile_override=None,
        )
        session = session_factory(workspace_id)
        try:
            run = session.get(PipelineRunORM, started.run_id)
            assert run is not None
            run.state = "paused"
            run.pause_reason = "user_request"
            run.current_chapter = None
            for field_name, value in corruption.items():
                setattr(run, field_name, value)
            session.commit()
            before = {
                column.name: deepcopy(getattr(run, column.name))
                for column in PipelineRunORM.__table__.columns
            }
        finally:
            session.close()
        event_count_before = len(fake_ws_manager.events)

        with pytest.raises(
            ContinuityCheckpointCorruptionError,
            match=expected_pattern,
        ):
            service.resume_with_transition(workspace_id)

        assert len(fake_ws_manager.events) == event_count_before
        assert fake_chapter_service.calls == []
        assert fake_chapter_service.checkpoint_calls == []
        assert fake_chapter_service.ready_calls == []
        session = session_factory(workspace_id)
        try:
            run = session.get(PipelineRunORM, started.run_id)
            assert run is not None
            after = {
                column.name: deepcopy(getattr(run, column.name))
                for column in PipelineRunORM.__table__.columns
            }
            attempts = list(
                session.scalars(
                    select(AttemptLedgerORM).where(
                        AttemptLedgerORM.run_id == started.run_id
                    )
                )
            )
        finally:
            session.close()
        assert after == before
        assert attempts == []

    @pytest.mark.parametrize(
        ("memory_status", "memory_details", "has_enriched_at"),
        [
            ("future_state", {"tasks": []}, False),
            ("running", None, False),
            ("pending", "broken-details", False),
            (
                "running",
                {
                    "started_at": "2026-08-02T12:30:11+00:00",
                    "tasks": {},
                },
                False,
            ),
            ("failed", {"tasks": [], "failures": {}}, False),
            ("running", {"started_at": "not-a-time", "tasks": []}, False),
            (
                "running",
                {
                    "started_at": "2026-08-02T12:30:11+00:00",
                    "tasks": [],
                },
                True,
            ),
            ("succeeded", {"tasks": []}, False),
        ],
    )
    def test_resume_preserves_corrupt_memory_checkpoint_before_cas_or_attempt(
        self,
        service: PipelineService,
        workspace_id: str,
        session_factory: Callable[[str], Session],
        memory_status: str,
        memory_details: object,
        has_enriched_at: bool,
    ) -> None:
        """未知 status / 坏 details 原样保留，不能先翻 run 或建 attempt。"""
        _seed_workspace_and_volume(session_factory, workspace_id)
        started = service.start(
            workspace_id,
            scope="single",
            start_chapter_id="ch-0001",
            end_chapter_id=None,
            volume_id=None,
            profile_override=None,
        )
        session = session_factory(workspace_id)
        try:
            now = datetime.now(timezone.utc)
            session.add(
                ChapterORM(
                    chapter_id="ch-0001",
                    workspace_id=workspace_id,
                    current_version=9,
                    status="approved",
                    rewrite_count=6,
                    revise_count=2,
                    created_at=now,
                    approved_at=now,
                    total_token_usage={},
                    total_cost_usd=0.0,
                    ready_for_next_chapter_at=None,
                    memory_enrichment_status=memory_status,
                    memory_enrichment_details=memory_details,
                    memory_enriched_at=(now if has_enriched_at else None),
                )
            )
            run = session.get(PipelineRunORM, started.run_id)
            assert run is not None
            run.state = "paused"
            run.pause_reason = "user_request"
            run.current_chapter = "ch-0001"
            session.commit()
        finally:
            session.close()

        with pytest.raises(
            ContinuityCheckpointCorruptionError,
            match="富记忆 checkpoint 结构损坏",
        ):
            service.resume_with_transition(workspace_id)

        session = session_factory(workspace_id)
        try:
            run = session.get(PipelineRunORM, started.run_id)
            row = session.get(ChapterORM, "ch-0001")
            assert run is not None
            assert row is not None
            assert run.state == "paused"
            assert run.pause_reason == "user_request"
            assert run.pipeline_attempt_count == 0
            assert row.ready_for_next_chapter_at is None
            assert row.memory_enrichment_status == memory_status
            assert row.memory_enrichment_details == memory_details
            assert (row.memory_enriched_at is not None) is has_enriched_at
            attempts = list(
                session.scalars(
                    select(AttemptLedgerORM).where(
                        AttemptLedgerORM.run_id == started.run_id
                    )
                )
            )
        finally:
            session.close()
        assert attempts == []

    def test_resume_with_live_heartbeat_keeps_started_pipeline_attempt(
        self,
        service: PipelineService,
        workspace_id: str,
        session_factory: Callable[[str], Session],
    ) -> None:
        """paused 不等于 Worker 已死；有 heartbeat 时不得误收口。"""
        _seed_workspace_and_volume(session_factory, workspace_id)
        started = service.start(
            workspace_id,
            scope="single",
            start_chapter_id="ch-0001",
            end_chapter_id=None,
            volume_id=None,
            profile_override=None,
        )
        ledger = AttemptLedgerStore(session_factory)
        pipeline = ledger.begin_pipeline_attempt(
            workspace_id=workspace_id,
            run_id=started.run_id,
            chapter_id="ch-0001",
        )
        service.pause(workspace_id)
        service._heartbeats[workspace_id] = object()  # type: ignore[assignment]  # noqa: SLF001
        try:
            resumed, transitioned = service.resume_with_transition(workspace_id)
        finally:
            service._heartbeats.pop(workspace_id, None)  # noqa: SLF001

        assert transitioned is True
        assert resumed.run_id == started.run_id
        session = session_factory(workspace_id)
        try:
            pipeline_row = session.get(AttemptLedgerORM, pipeline.attempt_id)
        finally:
            session.close()
        assert pipeline_row is not None
        assert pipeline_row.status == "started"
        assert pipeline_row.completed_at is None

    @pytest.mark.parametrize("safe_to_continue", [False, True])
    def test_resume_only_allows_audited_safe_startup_memory_failure(
        self,
        service: PipelineService,
        workspace_id: str,
        session_factory: Callable[[str], Session],
        safe_to_continue: bool,
    ) -> None:
        """冷启动 unknown 只有完成来源验真 + 本地隔离证明才可降级续跑。"""

        _seed_workspace_and_volume(session_factory, workspace_id)
        started = service.start(
            workspace_id,
            scope="single",
            start_chapter_id="ch-0001",
            end_chapter_id=None,
            volume_id=None,
            profile_override=None,
        )
        session = session_factory(workspace_id)
        try:
            now = datetime.now(timezone.utc)
            session.add(
                ChapterORM(
                    chapter_id="ch-0001",
                    workspace_id=workspace_id,
                    current_version=9,
                    status="approved",
                    rewrite_count=6,
                    revise_count=2,
                    created_at=now,
                    approved_at=now,
                    total_token_usage={},
                    total_cost_usd=0.0,
                    ready_for_next_chapter_at=now,
                    memory_enrichment_status="failed",
                    memory_enrichment_details={
                        "schema_version": 1,
                        "ready_at": now.isoformat(),
                        "final_version_id": 9,
                        "final_path": "/tmp/ch-0001/final.md",
                        "continuity_ledger_hash": "0" * 64,
                        "continuity_ledger_source_text_hash": "1" * 64,
                        "tasks": [],
                        "failures": [
                            {
                                "stage": "memory_enrichment_task",
                                "result_unknown": True,
                            }
                        ],
                        "startup_recovery": {
                            "reason": "engine_restarted_mid_memory_enrichment",
                            "previous_status": "running",
                            "result_unknown": True,
                            "automatic_provider_replay": False,
                            "started_provider_attempt_ids": [],
                            "unconsumed_provider_attempts": [],
                            "provider_result_unknown": False,
                            "source_provenance_verified": safe_to_continue,
                            "source_provenance_error": (
                                None if safe_to_continue else "source unknown"
                            ),
                            "sqlite_footprint": {
                                "character_first_appearance": 0,
                                "character_last_updated": 0,
                                "character_state_timeline": 0,
                                "plot_planted": 0,
                                "plot_last_updated": 0,
                                "timeline_entries": 0,
                                "scene_anchor": 0 if safe_to_continue else 1,
                            },
                            "sqlite_side_effect_count": (0 if safe_to_continue else 1),
                            "vector_count": 0,
                            "vector_quarantined_count": 0,
                            "vector_quarantine_succeeded": True,
                            "vector_quarantine_error": None,
                            "side_effects_verified_absent": safe_to_continue,
                            "safe_to_continue": safe_to_continue,
                            "quarantine_required": not safe_to_continue,
                            "recovery_disposition": (
                                "safe_degraded_no_sqlite_or_vector_persist"
                                if safe_to_continue
                                else "blocked_uncertain"
                            ),
                        },
                    },
                    memory_enriched_at=None,
                )
            )
            run = session.get(PipelineRunORM, started.run_id)
            assert run is not None
            run.state = "paused"
            run.pause_reason = "user_request"
            run.current_chapter = "ch-0001"
            session.commit()
        finally:
            session.close()

        if safe_to_continue:
            snapshot, transitioned = service.resume_with_transition(workspace_id)
            assert transitioned is True
            assert snapshot.state == "running"
        else:
            with pytest.raises(
                ContinuityCheckpointCorruptionError,
                match="blocked_uncertain",
            ):
                service.resume_with_transition(workspace_id)
            session = session_factory(workspace_id)
            try:
                run = session.get(PipelineRunORM, started.run_id)
                assert run is not None
                assert run.state == "paused"
                assert run.pipeline_attempt_count == 0
            finally:
                session.close()

    def test_recoverable_continuity_error_resumes_same_run_and_blocks_start(
        self,
        service: PipelineService,
        workspace_id: str,
        session_factory: Callable[[str], Session],
    ) -> None:
        """历史 continuity error 保持 error 事实视图，但可原 run resume。"""
        _seed_workspace_and_volume(session_factory, workspace_id)
        started = service.start(
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
                "message": "ledger quote invalid",
                "chapter_id": "ch-0001",
            }
            session.commit()
        finally:
            session.close()

        status = service.status(workspace_id)
        assert status.state == "error"
        assert status.run_id == started.run_id
        with pytest.raises(PipelineAlreadyRunningError):
            service.start(
                workspace_id,
                scope="single",
                start_chapter_id="ch-0001",
                end_chapter_id=None,
                volume_id=None,
                profile_override=None,
            )

        resumed = service.resume(workspace_id)
        assert resumed.run_id == started.run_id
        assert resumed.state == "running"
        assert resumed.error_details is None

    def test_generic_error_remains_non_resumable(
        self,
        service: PipelineService,
        workspace_id: str,
        session_factory: Callable[[str], Session],
    ) -> None:
        """普通 error 仍走 stop/start，不扩大 resume 边界。"""
        _seed_workspace_and_volume(session_factory, workspace_id)
        started = service.start(
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
            row.error_details = {"error_type": "RuntimeError"}
            session.commit()
        finally:
            session.close()

        with pytest.raises(PipelineNotRunningError):
            service.resume(workspace_id)

    def test_core_state_conflict_preserves_same_run_until_card_reapproved(
        self,
        service: PipelineService,
        workspace_id: str,
        session_factory: Callable[[str], Session],
    ) -> None:
        _seed_workspace_and_volume(session_factory, workspace_id)
        started = service.start(
            workspace_id,
            scope="single",
            start_chapter_id="ch-0001",
            end_chapter_id=None,
            volume_id=None,
            profile_override=None,
        )
        with session_factory(workspace_id) as session:
            row = session.get(PipelineRunORM, started.run_id)
            assert row is not None
            row.state = "error"
            row.current_chapter = "ch-0001"
            row.error_details = {
                "error_type": "CoreStateConflictError",
                "message": "首章初始状态缺少基线",
                "chapter_id": "ch-0001",
            }
            session.commit()

        status = service.status(workspace_id)
        assert status.error_details is not None
        assert (
            status.error_details["recovery_kind"]
            == "core_state_initial_baseline"
        )
        assert status.error_details["recovery_ready"] is False
        assert (
            status.error_details["recovery_block_reason"]
            == "card_not_reapproved"
        )
        with pytest.raises(PipelineAlreadyRunningError):
            service.start(
                workspace_id,
                scope="single",
                start_chapter_id="ch-0001",
                end_chapter_id=None,
                volume_id=None,
                profile_override=None,
            )
        with pytest.raises(PipelineNotRunningError):
            service.resume(workspace_id)

    def test_nonfirst_core_state_transition_checkpoint_resumes_same_run(
        self,
        service: PipelineService,
        workspace_id: str,
        session_factory: Callable[[str], Session],
        monkeypatch: pytest.MonkeyPatch,
    ) -> None:
        """严格 preflight 通过后只开放原 run Continue，不允许新 Start。"""

        _seed_workspace_and_volume(
            session_factory, workspace_id, volumes=[("vol-1", 5)]
        )
        started = service.start(
            workspace_id,
            scope="range",
            start_chapter_id="ch-0001",
            end_chapter_id="ch-0005",
            volume_id=None,
            profile_override=None,
        )
        with session_factory(workspace_id) as session:
            row = session.get(PipelineRunORM, started.run_id)
            assert row is not None
            row.state = "error"
            row.current_chapter = "ch-0005"
            row.completed_chapter_ids = [
                "ch-0001",
                "ch-0002",
                "ch-0003",
                "ch-0004",
            ]
            row.error_details = {
                "error_type": "CoreStateConflictError",
                "message": "批准 transition 候选被旧中心性规则误降级",
                "chapter_id": "ch-0005",
            }
            session.commit()

        checked: list[str] = []

        def _safe_checkpoint(
            _session: Session,
            *,
            workspace_id: str,
            row: PipelineRunORM,
        ) -> None:
            del workspace_id
            checked.append(row.current_chapter or "")

        monkeypatch.setattr(
            service,
            "_require_core_state_transition_resume_safe",
            _safe_checkpoint,
        )

        status = service.status(workspace_id)
        assert status.error_details is not None
        assert (
            status.error_details["recovery_kind"]
            == "core_state_transition_checkpoint"
        )
        assert status.error_details["recovery_ready"] is True
        with pytest.raises(PipelineAlreadyRunningError):
            service.start(
                workspace_id,
                scope="single",
                start_chapter_id="ch-0005",
                end_chapter_id=None,
                volume_id=None,
                profile_override=None,
            )

        resumed = service.resume(workspace_id)
        assert resumed.run_id == started.run_id
        assert resumed.state == "running"
        assert resumed.error_details is None
        assert checked == ["ch-0005", "ch-0005"]

    @pytest.mark.parametrize(
        "error_type",
        [
            "LLMAuthError",
            "LLMAccessError",
            "RemoteProtocolError",
            "ReadTimeout",
            "SSEStreamError",
        ],
    )
    @pytest.mark.parametrize("chapter_status", ["reviewing", "drafting"])
    def test_provider_reviewer_error_resumes_same_run_from_unreviewed_version(
        self,
        service: PipelineService,
        workspace_service: WorkspaceService,
        workspace_id: str,
        session_factory: Callable[[str], Session],
        error_type: str,
        chapter_status: str,
    ) -> None:
        """旧/新 403 均恢复同 run；重启归一化 drafting 也须可重审。"""

        _seed_workspace_and_volume(session_factory, workspace_id)
        started = service.start(
            workspace_id,
            scope="single",
            start_chapter_id="ch-0001",
            end_chapter_id=None,
            volume_id=None,
            profile_override=None,
        )
        _seed_reviewing_version(
            session_factory,
            workspace_service,
            workspace_id,
            "ch-0001",
            chapter_status=chapter_status,
        )
        session = session_factory(workspace_id)
        try:
            row = session.get(PipelineRunORM, started.run_id)
            assert row is not None
            row.state = "error"
            row.current_chapter = "ch-0001"
            row.error_details = {
                "error_type": error_type,
                "message": "HTTP 403",
                "chapter_id": "ch-0001",
            }
            session.commit()
        finally:
            session.close()

        if error_type in {"RemoteProtocolError", "ReadTimeout", "SSEStreamError"}:
            _seed_failed_reviewer(
                session_factory, workspace_id, started.run_id, error_type
            )

        status = service.status(workspace_id)
        assert status.run_id == started.run_id
        assert status.error_details is not None
        assert status.error_details["recovery_kind"] == "provider_review_checkpoint"
        assert status.error_details["recovery_ready"] is True

        service._heartbeats[workspace_id] = object()  # type: ignore[assignment]  # noqa: SLF001
        try:
            blocked = service.status(workspace_id)
            assert blocked.error_details is not None
            assert blocked.error_details["recovery_ready"] is False
            assert (
                blocked.error_details["recovery_block_reason"]
                == "checkpoint_not_clean"
            )
            with pytest.raises(
                ContinuityCheckpointCorruptionError,
                match="旧 Worker 心跳",
            ):
                service.resume_with_transition(workspace_id)
        finally:
            service._heartbeats.pop(workspace_id, None)  # noqa: SLF001

        with pytest.raises(PipelineAlreadyRunningError):
            service.start(
                workspace_id,
                scope="single",
                start_chapter_id="ch-0001",
                end_chapter_id=None,
                volume_id=None,
                profile_override=None,
            )

        resumed, transitioned = service.resume_with_transition(workspace_id)
        assert transitioned is True
        assert resumed.run_id == started.run_id
        assert resumed.state == "running"
        assert resumed.current_chapter == "ch-0001"
        assert resumed.error_details is None

    @pytest.mark.parametrize("agent_name", [None, "writer", "chapter_summarizer"])
    def test_transport_recovery_requires_terminal_reviewer_evidence(
        self,
        service: PipelineService,
        workspace_service: WorkspaceService,
        workspace_id: str,
        session_factory: Callable[[str], Session],
        agent_name: str | None,
    ) -> None:
        _seed_workspace_and_volume(session_factory, workspace_id)
        started = service.start(
            workspace_id,
            scope="single",
            start_chapter_id="ch-0001",
            end_chapter_id=None,
            volume_id=None,
            profile_override=None,
        )
        _seed_reviewing_version(
            session_factory, workspace_service, workspace_id, "ch-0001"
        )
        with session_factory(workspace_id) as session:
            row = session.get(PipelineRunORM, started.run_id)
            assert row is not None
            row.state = "error"
            row.current_chapter = "ch-0001"
            row.error_details = {
                "error_type": "RemoteProtocolError",
                "chapter_id": "ch-0001",
            }
            session.commit()
        if agent_name:
            _seed_failed_reviewer(
                session_factory,
                workspace_id,
                started.run_id,
                "RemoteProtocolError",
                agent_name=agent_name,
            )
        status = service.status(workspace_id)
        assert status.error_details is not None
        assert status.error_details["recovery_ready"] is False
        with pytest.raises(
            ContinuityCheckpointCorruptionError, match="Reviewer 失败账本"
        ):
            service.resume_with_transition(workspace_id)
        assert service.status(workspace_id).state == "error"

    def test_provider_reviewer_recovery_rejects_disk_db_drift_before_cas(
        self,
        service: PipelineService,
        workspace_service: WorkspaceService,
        workspace_id: str,
        session_factory: Callable[[str], Session],
    ) -> None:
        """磁盘正文与 DB 版本不同时禁止 Worker/Provider 重启。"""

        _seed_workspace_and_volume(session_factory, workspace_id)
        started = service.start(
            workspace_id,
            scope="single",
            start_chapter_id="ch-0001",
            end_chapter_id=None,
            volume_id=None,
            profile_override=None,
        )
        _seed_reviewing_version(
            session_factory,
            workspace_service,
            workspace_id,
            "ch-0001",
        )
        version_path = (
            workspace_service.workspaces_root
            / workspace_id
            / "data"
            / "chapters"
            / "ch-0001"
            / "v4.md"
        )
        version_path.write_text("被篡改的正文", encoding="utf-8")
        session = session_factory(workspace_id)
        try:
            row = session.get(PipelineRunORM, started.run_id)
            assert row is not None
            row.state = "error"
            row.current_chapter = "ch-0001"
            row.error_details = {
                "error_type": "LLMAuthError",
                "message": "legacy HTTP 403",
                "chapter_id": "ch-0001",
            }
            session.commit()
        finally:
            session.close()

        status = service.status(workspace_id)
        assert status.error_details is not None
        assert status.error_details["recovery_ready"] is False
        assert (
            status.error_details["recovery_block_reason"]
            == "checkpoint_not_clean"
        )
        with pytest.raises(
            ContinuityCheckpointCorruptionError,
            match="磁盘/DB",
        ):
            service.resume_with_transition(workspace_id)
        session = session_factory(workspace_id)
        try:
            row = session.get(PipelineRunORM, started.run_id)
            assert row is not None
            assert row.state == "error"
            assert row.error_details is not None
        finally:
            session.close()

    def test_card_generation_error_blocks_new_start_until_card_is_approved(
        self,
        service: PipelineService,
        workspace_service: WorkspaceService,
        workspace_id: str,
        session_factory: Callable[[str], Session],
    ) -> None:
        """写前卡失败保留原 run；未批准时开始和继续都不能越过。"""

        _seed_workspace_and_volume(session_factory, workspace_id)
        started = service.start(
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
            row.current_chapter = "ch-0001"
            row.error_details = {
                "reason": "card_generation_failed",
                "message": "card contract ambiguous",
                "chapter_id": "ch-0001",
            }
            session.commit()
        finally:
            session.close()
        _seed_card_status(
            session_factory,
            workspace_service,
            workspace_id,
            "ch-0001",
            status="draft",
        )

        status = service.status(workspace_id)
        assert status.error_details is not None
        assert status.error_details["recovery_kind"] == "chapter_card_repair"
        assert status.error_details["recovery_ready"] is False
        assert (
            status.error_details["recovery_block_reason"] == "chapter_card_not_approved"
        )
        with pytest.raises(PipelineAlreadyRunningError):
            service.start(
                workspace_id,
                scope="single",
                start_chapter_id="ch-0001",
                end_chapter_id=None,
                volume_id=None,
                profile_override=None,
            )
        with pytest.raises(PipelineNotRunningError):
            service.resume(workspace_id)

    def test_approved_card_generation_error_resumes_same_run(
        self,
        service: PipelineService,
        workspace_service: WorkspaceService,
        workspace_id: str,
        session_factory: Callable[[str], Session],
    ) -> None:
        """卡片经产品批准且正文现场为空时，只恢复原 run。"""

        _seed_workspace_and_volume(session_factory, workspace_id)
        started = service.start(
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
            row.current_chapter = "ch-0001"
            row.error_details = {
                "reason": "card_generation_failed",
                "message": "card contract ambiguous",
                "chapter_id": "ch-0001",
            }
            session.commit()
        finally:
            session.close()
        _seed_card_status(
            session_factory,
            workspace_service,
            workspace_id,
            "ch-0001",
            status="approved",
        )

        status = service.status(workspace_id)
        assert status.error_details is not None
        assert status.error_details["recovery_ready"] is True
        resumed = service.resume(workspace_id)
        assert resumed.run_id == started.run_id
        assert resumed.state == "running"
        assert resumed.error_details is None

    def test_continuity_bridge_error_allows_clean_draft_same_run_resume(
        self,
        service: PipelineService,
        workspace_service: WorkspaceService,
        workspace_id: str,
        session_factory: Callable[[str], Session],
    ) -> None:
        """确定性 bridge 拒绝可让原 run 重新进入自动校验与定向重试。"""

        _seed_workspace_and_volume(session_factory, workspace_id)
        started = service.start(
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
            row.current_chapter = "ch-0001"
            row.error_details = {
                "reason": "card_generation_failed",
                "message": (
                    "循环内按 continuity ledger 重生成/批准失败: "
                    "CardContinuityBridgeRequiredError: 缺少逐字承接"
                ),
                "chapter_id": "ch-0001",
            }
            session.commit()
        finally:
            session.close()
        _seed_card_status(
            session_factory,
            workspace_service,
            workspace_id,
            "ch-0001",
            status="draft",
        )

        status = service.status(workspace_id)
        assert status.error_details is not None
        assert status.error_details["recovery_ready"] is True
        resumed = service.resume(workspace_id)
        assert resumed.run_id == started.run_id
        assert resumed.state == "running"
        assert resumed.error_details is None

    def test_continuity_bridge_draft_recovery_rejects_writing_artifacts(
        self,
        service: PipelineService,
        workspace_service: WorkspaceService,
        workspace_id: str,
        session_factory: Callable[[str], Session],
    ) -> None:
        """bridge 草稿只有在正文现场完全为空时才可恢复 Worker。"""

        _seed_workspace_and_volume(session_factory, workspace_id)
        started = service.start(
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
            row.current_chapter = "ch-0001"
            row.error_details = {
                "reason": "card_generation_failed",
                "message": (
                    "CardContinuityBridgeRequiredError: 缺少逐字承接"
                ),
                "chapter_id": "ch-0001",
            }
            session.commit()
        finally:
            session.close()
        _seed_card_status(
            session_factory,
            workspace_service,
            workspace_id,
            "ch-0001",
            status="draft",
        )
        chapter_dir = (
            workspace_service.workspaces_root
            / workspace_id
            / "data"
            / "chapters"
            / "ch-0001"
        )
        chapter_dir.mkdir(parents=True, exist_ok=True)
        (chapter_dir / "v1.md").write_text("unexpected", encoding="utf-8")

        status = service.status(workspace_id)
        assert status.error_details is not None
        assert status.error_details["recovery_ready"] is False
        assert status.error_details["recovery_block_reason"] == "checkpoint_not_clean"
        with pytest.raises(ContinuityCheckpointCorruptionError):
            service.resume(workspace_id)

    def test_exhausted_continuity_bridge_retry_draft_cannot_resume_again(
        self,
        service: PipelineService,
        workspace_service: WorkspaceService,
        workspace_id: str,
        session_factory: Callable[[str], Session],
    ) -> None:
        """新代码已用过定向重试后，不因异常类名仍在而再次放行。"""

        _seed_workspace_and_volume(session_factory, workspace_id)
        started = service.start(
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
            row.current_chapter = "ch-0001"
            row.error_details = {
                "reason": "card_generation_failed",
                "message": (
                    "CardContinuityBridgeRequiredError: 定向重试后仍缺少逐字承接"
                ),
                "chapter_id": "ch-0001",
                "continuity_bridge_retry_exhausted": True,
            }
            session.commit()
        finally:
            session.close()
        _seed_card_status(
            session_factory,
            workspace_service,
            workspace_id,
            "ch-0001",
            status="draft",
        )

        status = service.status(workspace_id)
        assert status.error_details is not None
        assert status.error_details["recovery_ready"] is False
        assert (
            status.error_details["recovery_block_reason"]
            == "chapter_card_not_approved"
        )
        with pytest.raises(PipelineNotRunningError):
            service.resume(workspace_id)

    def test_card_generation_recovery_rejects_writing_artifacts(
        self,
        service: PipelineService,
        workspace_service: WorkspaceService,
        workspace_id: str,
        session_factory: Callable[[str], Session],
    ) -> None:
        """声称写前失败却已有正文文件时按 checkpoint 损坏硬停。"""

        _seed_workspace_and_volume(session_factory, workspace_id)
        started = service.start(
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
            row.current_chapter = "ch-0001"
            row.error_details = {
                "reason": "card_generation_failed",
                "message": "card contract ambiguous",
                "chapter_id": "ch-0001",
            }
            session.commit()
        finally:
            session.close()
        _seed_card_status(
            session_factory,
            workspace_service,
            workspace_id,
            "ch-0001",
            status="approved",
        )
        chapter_dir = (
            workspace_service.workspaces_root
            / workspace_id
            / "data"
            / "chapters"
            / "ch-0001"
        )
        chapter_dir.mkdir(parents=True, exist_ok=True)
        (chapter_dir / "v1.md").write_text("unexpected", encoding="utf-8")

        status = service.status(workspace_id)
        assert status.error_details is not None
        assert status.error_details["recovery_ready"] is False
        assert status.error_details["recovery_block_reason"] == "checkpoint_not_clean"
        with pytest.raises(ContinuityCheckpointCorruptionError):
            service.resume(workspace_id)

    def test_card_generation_recovery_rejects_workspace_started_attempt(
        self,
        service: PipelineService,
        workspace_service: WorkspaceService,
        workspace_id: str,
        session_factory: Callable[[str], Session],
    ) -> None:
        """卡片 UI task 即使无 run_id，started attempt 仍阻止恢复 Worker。"""

        _seed_workspace_and_volume(session_factory, workspace_id)
        started = service.start(
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
                AttemptLedgerORM(
                    attempt_id="att-card-ui-active",
                    workspace_id=workspace_id,
                    run_id=None,
                    task_id="task-card-ui-retry",
                    chapter_id="ch-0001",
                    attempt_kind="provider",
                    operation_kind="chapter_card_generator",
                    logical_call_id="call-card-ui-retry",
                    agent_name="chapter_card_generator",
                    provider="mock",
                    model="mock",
                    attempt_number=1,
                    attempt_cap=1,
                    status="started",
                    version_before=None,
                    version_after=None,
                    error_type=None,
                    created_at=now,
                    completed_at=None,
                )
            )
            session.commit()
        finally:
            session.close()
        _seed_card_status(
            session_factory,
            workspace_service,
            workspace_id,
            "ch-0001",
            status="approved",
        )

        status = service.status(workspace_id)
        assert status.error_details is not None
        assert status.error_details["recovery_ready"] is False
        assert status.error_details["recovery_block_reason"] == "checkpoint_not_clean"
        with pytest.raises(ContinuityCheckpointCorruptionError):
            service.resume(workspace_id)

    def test_pause_idempotent_double_pause_no_extra_event(
        self,
        service: PipelineService,
        workspace_id: str,
        session_factory: Callable[[str], Session],
        fake_ws_manager: _FakeWSManager,
    ) -> None:
        _seed_workspace_and_volume(session_factory, workspace_id)
        service.start(
            workspace_id,
            scope="single",
            start_chapter_id="ch-0001",
            end_chapter_id=None,
            volume_id=None,
            profile_override=None,
        )
        service.pause(workspace_id)
        before = sum(
            1 for e in fake_ws_manager.events if e["event"] == "pipeline_paused"
        )
        # 二次 pause:无新 event
        service.pause(workspace_id)
        after = sum(
            1 for e in fake_ws_manager.events if e["event"] == "pipeline_paused"
        )
        assert after == before

    def test_stop_clears_active_run(
        self,
        service: PipelineService,
        workspace_id: str,
        session_factory: Callable[[str], Session],
        fake_ws_manager: _FakeWSManager,
    ) -> None:
        _seed_workspace_and_volume(session_factory, workspace_id)
        service.start(
            workspace_id,
            scope="single",
            start_chapter_id="ch-0001",
            end_chapter_id=None,
            volume_id=None,
            profile_override=None,
        )
        snap = service.stop(workspace_id)
        assert snap.state == "stopped"
        assert any(e["event"] == "pipeline_stopped" for e in fake_ws_manager.events)
        # status 端点应能查到 last_run snapshot(虽 active 已清除)
        status_snap = service.status(workspace_id)
        assert status_snap.state == "stopped"

    def test_late_internal_failure_cannot_resurrect_stopped_run(
        self,
        service: PipelineService,
        workspace_id: str,
        session_factory: Callable[[str], Session],
        fake_ws_manager: _FakeWSManager,
    ) -> None:
        """在途调用晚到的 pause/error/skip 都不能覆盖用户 Stop 终态。"""

        _seed_workspace_and_volume(session_factory, workspace_id)
        started = service.start(
            workspace_id,
            scope="single",
            start_chapter_id="ch-0001",
            end_chapter_id=None,
            volume_id=None,
            profile_override=None,
        )
        service.stop(workspace_id)
        paused_before = sum(
            event["event"] == "pipeline_paused"
            for event in fake_ws_manager.events
        )

        service._auto_pause(  # noqa: SLF001
            workspace_id,
            run_id=started.run_id,
            reason="memory_enrichment_failed",
            blocked_chapter_id="ch-0001",
        )
        service._mark_error(  # noqa: SLF001
            workspace_id=workspace_id,
            run_id=started.run_id,
            error_details={"error_type": "LateFailure"},
        )
        service._record_skip(  # noqa: SLF001
            workspace_id,
            run_id=started.run_id,
            chapter_id="ch-0001",
        )

        status = service.status(workspace_id)
        assert status.state == "stopped"
        assert status.pause_reason == "user_request"
        assert status.skipped_chapter_ids == []
        assert (
            sum(
                event["event"] == "pipeline_paused"
                for event in fake_ws_manager.events
            )
            == paused_before
        )

    @pytest.mark.parametrize("external_state", ["paused", "interrupted"])
    def test_late_internal_failure_preserves_external_state(
        self,
        external_state: str,
        service: PipelineService,
        workspace_id: str,
        session_factory: Callable[[str], Session],
        fake_ws_manager: _FakeWSManager,
    ) -> None:
        """用户暂停或 watchdog 中断先落库后，迟到失败不得覆盖原因。"""

        _seed_workspace_and_volume(session_factory, workspace_id)
        started = service.start(
            workspace_id,
            scope="single",
            start_chapter_id="ch-0001",
            end_chapter_id=None,
            volume_id=None,
            profile_override=None,
        )
        if external_state == "paused":
            service.pause(workspace_id, reason="user_request")
        else:
            service._mark_interrupted(  # noqa: SLF001
                workspace_id=workspace_id,
                run_id=started.run_id,
                error_details={"reason": "chapter_watchdog_timeout"},
            )
        before = service.status(workspace_id)

        service._auto_pause(  # noqa: SLF001
            workspace_id,
            run_id=started.run_id,
            reason="memory_enrichment_failed",
            blocked_chapter_id="ch-0001",
        )
        service._mark_error(  # noqa: SLF001
            workspace_id=workspace_id,
            run_id=started.run_id,
            error_details={"error_type": "LateFailure"},
        )

        after = service.status(workspace_id)
        assert after.state == before.state
        assert after.pause_reason == before.pause_reason
        assert after.error_details == before.error_details

    def test_status_idle_when_no_run(
        self, service: PipelineService, workspace_id: str
    ) -> None:
        snap = service.status(workspace_id)
        assert snap.state == "idle"
        assert snap.run_id == ""
        assert snap.chapter_ids == []


# ---- error path -----------------------------------------------------------


class TestErrors:
    def test_start_twice_raises_already_running(
        self,
        service: PipelineService,
        workspace_id: str,
        session_factory: Callable[[str], Session],
    ) -> None:
        _seed_workspace_and_volume(session_factory, workspace_id)
        service.start(
            workspace_id,
            scope="single",
            start_chapter_id="ch-0001",
            end_chapter_id=None,
            volume_id=None,
            profile_override=None,
        )
        with pytest.raises(PipelineAlreadyRunningError) as exc_info:
            service.start(
                workspace_id,
                scope="single",
                start_chapter_id="ch-0002",
                end_chapter_id=None,
                volume_id=None,
                profile_override=None,
            )
        assert exc_info.value.workspace_id == workspace_id
        assert exc_info.value.active_state == "running"

    def test_pause_without_active_raises_not_running(
        self, service: PipelineService, workspace_id: str
    ) -> None:
        with pytest.raises(PipelineNotRunningError) as exc_info:
            service.pause(workspace_id)
        assert exc_info.value.action == "pause"

    def test_resume_without_active_raises_not_running(
        self, service: PipelineService, workspace_id: str
    ) -> None:
        with pytest.raises(PipelineNotRunningError) as exc_info:
            service.resume(workspace_id)
        assert exc_info.value.action == "resume"

    def test_stop_without_active_raises_not_running(
        self, service: PipelineService, workspace_id: str
    ) -> None:
        with pytest.raises(PipelineNotRunningError) as exc_info:
            service.stop(workspace_id)
        assert exc_info.value.action == "stop"

    def test_start_range_missing_end_raises_invalid_scope(
        self,
        service: PipelineService,
        workspace_id: str,
        session_factory: Callable[[str], Session],
    ) -> None:
        _seed_workspace_and_volume(session_factory, workspace_id)
        with pytest.raises(PipelineInvalidScopeError) as exc_info:
            service.start(
                workspace_id,
                scope="range",
                start_chapter_id="ch-0001",
                end_chapter_id=None,
                volume_id=None,
                profile_override=None,
            )
        assert exc_info.value.scope == "range"
        assert "end_chapter_id" in exc_info.value.reason

    def test_start_volume_missing_volume_id_raises_invalid_scope(
        self,
        service: PipelineService,
        workspace_id: str,
        session_factory: Callable[[str], Session],
    ) -> None:
        _seed_workspace_and_volume(session_factory, workspace_id)
        with pytest.raises(PipelineInvalidScopeError) as exc_info:
            service.start(
                workspace_id,
                scope="volume",
                start_chapter_id=None,
                end_chapter_id=None,
                volume_id=None,
                profile_override=None,
            )
        assert "volume_id" in exc_info.value.reason

    def test_start_volume_unknown_raises_invalid_scope(
        self,
        service: PipelineService,
        workspace_id: str,
        session_factory: Callable[[str], Session],
    ) -> None:
        _seed_workspace_and_volume(session_factory, workspace_id)
        with pytest.raises(PipelineInvalidScopeError) as exc_info:
            service.start(
                workspace_id,
                scope="volume",
                start_chapter_id=None,
                end_chapter_id=None,
                volume_id="vol-nonexistent",
                profile_override=None,
            )
        assert "vol-nonexistent" in exc_info.value.reason

    def test_start_range_start_after_end_raises_invalid_scope(
        self,
        service: PipelineService,
        workspace_id: str,
        session_factory: Callable[[str], Session],
    ) -> None:
        _seed_workspace_and_volume(
            session_factory, workspace_id, volumes=[("vol-1", 5)]
        )
        with pytest.raises(PipelineInvalidScopeError) as exc_info:
            service.start(
                workspace_id,
                scope="range",
                start_chapter_id="ch-0004",
                end_chapter_id="ch-0002",
                volume_id=None,
                profile_override=None,
            )
        assert "lex 序" in exc_info.value.reason or "≤" in exc_info.value.reason

    def test_start_range_start_not_exist_raises_invalid_scope(
        self,
        service: PipelineService,
        workspace_id: str,
        session_factory: Callable[[str], Session],
    ) -> None:
        _seed_workspace_and_volume(
            session_factory, workspace_id, volumes=[("vol-1", 3)]
        )
        with pytest.raises(PipelineInvalidScopeError) as exc_info:
            service.start(
                workspace_id,
                scope="range",
                start_chapter_id="ch-9999",
                end_chapter_id="ch-0003",
                volume_id=None,
                profile_override=None,
            )
        assert "ch-9999" in exc_info.value.reason

    def test_start_no_volume_in_workspace_raises_invalid_scope(
        self,
        service: PipelineService,
        workspace_id: str,
        session_factory: Callable[[str], Session],
    ) -> None:
        # 只种 WorkspaceORM,不种 VolumeORM
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
                    total_chapters=0,
                    completed_chapters=0,
                    config_overrides={},
                )
            )
            session.commit()
        finally:
            session.close()
        with pytest.raises(PipelineInvalidScopeError) as exc_info:
            service.start(
                workspace_id,
                scope="range",
                start_chapter_id="ch-0001",
                end_chapter_id="ch-0002",
                volume_id=None,
                profile_override=None,
            )
        assert "无任何 volume" in exc_info.value.reason


# ---- execute 循环(happy + gate + rollback)--------------------------------


class TestExecute:
    def test_execute_completes_all_chapters_and_publishes_events(
        self,
        service: PipelineService,
        workspace_id: str,
        session_factory: Callable[[str], Session],
        fake_chapter_service: _FakeChapterService,
        fake_ws_manager: _FakeWSManager,
    ) -> None:
        _seed_workspace_and_volume(
            session_factory, workspace_id, volumes=[("vol-1", 3)]
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

        # chapter_service.write 应被调 3 次
        assert [c[1] for c in fake_chapter_service.calls] == [
            "ch-0001",
            "ch-0002",
            "ch-0003",
        ]
        # WS event 类型计数
        event_names = [e["event"] for e in fake_ws_manager.events]
        assert event_names.count("pipeline_started") == 1
        assert event_names.count("chapter_started") == 3
        assert event_names.count("chapter_finished") == 3
        assert event_names.count("pipeline_stopped") == 1
        # 最后一个 pipeline_stopped data 含 reason=completed
        stopped_events = [
            e for e in fake_ws_manager.events if e["event"] == "pipeline_stopped"
        ]
        assert stopped_events[-1]["data"]["reason"] == "completed"

        # status:active 清除,last_run state=stopped
        final = service.status(workspace_id)
        assert final.state == "stopped"
        assert final.pause_reason == "completed"
        assert final.completed_chapter_ids == ["ch-0001", "ch-0002", "ch-0003"]
        assert final.progress == 1.0

    @pytest.mark.parametrize(
        ("boundary_action", "expected_state"),
        [("pause", "paused"), ("stop", "stopped")],
    )
    def test_pause_or_stop_between_selection_and_attempt_blocks_chapter_start(
        self,
        boundary_action: str,
        expected_state: str,
        workspace_service: WorkspaceService,
        workspace_id: str,
        session_factory: Callable[[str], Session],
        fake_chapter_service: _FakeChapterService,
        monkeypatch: pytest.MonkeyPatch,
    ) -> None:
        """章已选中但 attempt 尚未开始时，用户边界操作必须赢得竞态。"""

        _seed_workspace_and_volume(session_factory, workspace_id)
        svc = PipelineService(
            chapter_service=fake_chapter_service,  # type: ignore[arg-type]
            workspace_service=workspace_service,
            session_factory=session_factory,
        )
        started = svc.start(
            workspace_id,
            scope="single",
            start_chapter_id="ch-0001",
            end_chapter_id=None,
            volume_id=None,
            profile_override=None,
        )

        def _boundary_cost_check(_workspace_id: str) -> None:
            if boundary_action == "pause":
                svc.pause(workspace_id)
            else:
                svc.stop(workspace_id)
            return None

        monkeypatch.setattr(svc, "_evaluate_run_cost_gate", _boundary_cost_check)
        svc.execute(workspace_id, started.run_id)

        assert svc.status(workspace_id).state == expected_state
        assert fake_chapter_service.calls == []
        session = session_factory(workspace_id)
        try:
            attempts = list(
                session.scalars(
                    select(AttemptLedgerORM).where(
                        AttemptLedgerORM.run_id == started.run_id,
                        AttemptLedgerORM.attempt_kind == "pipeline",
                    )
                )
            )
        finally:
            session.close()
        assert attempts == []

    def test_same_run_resume_during_owner_teardown_is_handed_off(
        self,
        service: PipelineService,
        workspace_id: str,
        session_factory: Callable[[str], Session],
        monkeypatch: pytest.MonkeyPatch,
    ) -> None:
        """Continue 撞旧 heartbeat 收尾时，原 owner 接手而非留下 running 僵尸。"""

        _seed_workspace_and_volume(session_factory, workspace_id)
        started = service.start(
            workspace_id,
            scope="single",
            start_chapter_id="ch-0001",
            end_chapter_id=None,
            volume_id=None,
            profile_override=None,
        )
        execute_flags: list[bool] = []

        def _controlled_loop(
            controlled_workspace_id: str,
            controlled_run_id: str,
            *,
            explicit_resume: bool = False,
        ) -> None:
            execute_flags.append(explicit_resume)
            if len(execute_flags) == 1:
                service.pause(controlled_workspace_id)
                service.resume_with_transition(controlled_workspace_id)
                # 模拟路由 BackgroundTask 在旧 owner 注销 heartbeat 前到达。
                service.execute(
                    controlled_workspace_id,
                    controlled_run_id,
                    explicit_resume=True,
                )
                return
            service.stop(controlled_workspace_id)

        monkeypatch.setattr(service, "_execute_loop", _controlled_loop)
        service.execute(workspace_id, started.run_id)

        assert execute_flags == [False, True]
        assert service.status(workspace_id).state == "stopped"
        assert workspace_id not in service._heartbeats  # noqa: SLF001

    def test_inflight_success_after_user_stop_does_not_mutate_run_progress(
        self,
        workspace_service: WorkspaceService,
        workspace_id: str,
        session_factory: Callable[[str], Session],
        fake_ws_manager: _FakeWSManager,
        fake_chapter_service: _FakeChapterService,
        monkeypatch: pytest.MonkeyPatch,
    ) -> None:
        """Stop 后迟到的成功可收账/释租约，但 stopped run 快照保持冻结。"""

        _seed_workspace_and_volume(session_factory, workspace_id)
        svc = PipelineService(
            chapter_service=fake_chapter_service,  # type: ignore[arg-type]
            workspace_service=workspace_service,
            session_factory=session_factory,
            ws_manager=fake_ws_manager,  # type: ignore[arg-type]
        )
        started = svc.start(
            workspace_id,
            scope="single",
            start_chapter_id="ch-0001",
            end_chapter_id=None,
            volume_id=None,
            profile_override=None,
        )
        original_write = fake_chapter_service.write

        def _stop_then_return(
            selected_workspace_id: str,
            chapter_id: str,
            *,
            on_state_transition=None,
            profile_override: str | None = None,
        ) -> Any:
            result = original_write(
                selected_workspace_id,
                chapter_id,
                on_state_transition=on_state_transition,
                profile_override=profile_override,
            )
            svc.stop(selected_workspace_id)
            return result

        monkeypatch.setattr(fake_chapter_service, "write", _stop_then_return)
        svc.execute(workspace_id, started.run_id)

        status = svc.status(workspace_id)
        assert status.state == "stopped"
        assert status.pause_reason == "user_request"
        assert status.completed_chapter_ids == []
        chapter_finished = [
            event
            for event in fake_ws_manager.events
            if event["event"] == "chapter_finished"
        ]
        assert chapter_finished == []
        session = session_factory(workspace_id)
        try:
            attempt = session.scalar(
                select(AttemptLedgerORM).where(
                    AttemptLedgerORM.run_id == started.run_id,
                    AttemptLedgerORM.attempt_kind == "pipeline",
                )
            )
        finally:
            session.close()
        assert attempt is not None
        assert attempt.status == "succeeded"

    def test_resume_backfills_ui_repaired_current_checkpoint(
        self,
        service: PipelineService,
        workspace_id: str,
        session_factory: Callable[[str], Session],
        fake_chapter_service: _FakeChapterService,
    ) -> None:
        """UI 批准当前章后回原 run 进度；不把该章再送进写作。"""
        _seed_workspace_and_volume(
            session_factory,
            workspace_id,
            volumes=[("vol-1", 2)],
        )
        started = service.start(
            workspace_id,
            scope="range",
            start_chapter_id="ch-0001",
            end_chapter_id="ch-0002",
            volume_id=None,
            profile_override=None,
        )
        session = session_factory(workspace_id)
        try:
            now = datetime.now(timezone.utc)
            session.add(
                ChapterORM(
                    chapter_id="ch-0001",
                    workspace_id=workspace_id,
                    current_version=1,
                    status="approved",
                    rewrite_count=0,
                    revise_count=0,
                    created_at=now,
                    approved_at=now,
                    total_token_usage={},
                    total_cost_usd=0.0,
                    ready_for_next_chapter_at=now,
                )
            )
            run = session.get(PipelineRunORM, started.run_id)
            assert run is not None
            run.state = "paused"
            run.pause_reason = "user_request"
            run.current_chapter = "ch-0001"
            session.commit()
        finally:
            session.close()

        service.resume(workspace_id)
        service.execute(workspace_id, started.run_id)

        status = service.status(workspace_id)
        assert status.state == "stopped"
        assert status.completed_chapter_ids == ["ch-0001", "ch-0002"]
        assert [call[1] for call in fake_chapter_service.calls] == ["ch-0002"]
        session = session_factory(workspace_id)
        try:
            attempts = list(
                session.scalars(
                    select(AttemptLedgerORM)
                    .where(AttemptLedgerORM.run_id == started.run_id)
                    .where(AttemptLedgerORM.attempt_kind == "pipeline")
                )
            )
        finally:
            session.close()
        assert [row.chapter_id for row in attempts] == ["ch-0002"]

    @pytest.mark.parametrize("memory_status", ["pending", "failed"])
    def test_explicit_resume_routes_current_memory_through_checkpoint(
        self,
        memory_status: str,
        workspace_service: WorkspaceService,
        workspace_id: str,
        session_factory: Callable[[str], Session],
        fake_ws_manager: _FakeWSManager,
    ) -> None:
        """Continue 先恢复当前章富记忆，不能被 terminal backfill 提前挡住。"""

        memory_failure = MemoryEnrichmentNotReadyError(
            chapter_id="ch-0001",
            status=memory_status,
            reason="memory checkpoint fixture",
        )
        fake_chapter = _FakeChapterService(
            checkpoint_resume_on={"ch-0001"},
            checkpoint_recovers_memory_on={"ch-0001"},
            memory_raise_on={"ch-0001": memory_failure},
        )
        service = PipelineService(
            chapter_service=fake_chapter,  # type: ignore[arg-type]
            workspace_service=workspace_service,
            session_factory=session_factory,
            ws_manager=fake_ws_manager,  # type: ignore[arg-type]
        )
        _seed_workspace_and_volume(
            session_factory,
            workspace_id,
            volumes=[("vol-1", 1)],
        )
        started = service.start(
            workspace_id,
            scope="single",
            start_chapter_id="ch-0001",
            end_chapter_id=None,
            volume_id=None,
            profile_override=None,
        )
        session = session_factory(workspace_id)
        try:
            now = datetime.now(timezone.utc)
            session.add(
                ChapterORM(
                    chapter_id="ch-0001",
                    workspace_id=workspace_id,
                    current_version=1,
                    status="approved",
                    rewrite_count=0,
                    revise_count=0,
                    created_at=now,
                    approved_at=now,
                    total_token_usage={},
                    total_cost_usd=0.0,
                    ready_for_next_chapter_at=now,
                )
            )
            run = session.get(PipelineRunORM, started.run_id)
            assert run is not None
            run.state = "paused"
            run.pause_reason = "memory_enrichment_failed"
            run.current_chapter = "ch-0001"
            session.commit()
        finally:
            session.close()

        service.resume(workspace_id)
        service.execute(
            workspace_id,
            started.run_id,
            explicit_resume=True,
        )

        status = service.status(workspace_id)
        assert status.state == "stopped"
        assert status.pause_reason == "completed"
        assert status.completed_chapter_ids == ["ch-0001"]
        assert fake_chapter.checkpoint_calls == ["ch-0001"]
        assert fake_chapter.calls == []
        assert fake_chapter.memory_calls == ["ch-0001", "ch-0001"]
        session = session_factory(workspace_id)
        try:
            attempts = list(
                session.scalars(
                    select(AttemptLedgerORM)
                    .where(AttemptLedgerORM.run_id == started.run_id)
                    .where(AttemptLedgerORM.attempt_kind == "pipeline")
                )
            )
        finally:
            session.close()
        assert len(attempts) == 1
        assert attempts[0].chapter_id == "ch-0001"
        assert attempts[0].status == "succeeded"

    def test_corrupt_ui_checkpoint_is_never_backfilled(
        self,
        workspace_service: WorkspaceService,
        workspace_id: str,
        session_factory: Callable[[str], Session],
        fake_ws_manager: _FakeWSManager,
    ) -> None:
        corruption = ContinuityCheckpointCorruptionError(
            chapter_id="ch-0001",
            reason="final/db drift",
        )
        fake_chapter = _FakeChapterService(ready_raise_on={"ch-0001": corruption})
        service = PipelineService(
            chapter_service=fake_chapter,  # type: ignore[arg-type]
            workspace_service=workspace_service,
            session_factory=session_factory,
            ws_manager=fake_ws_manager,  # type: ignore[arg-type]
        )
        _seed_workspace_and_volume(session_factory, workspace_id)
        started = service.start(
            workspace_id,
            scope="single",
            start_chapter_id="ch-0001",
            end_chapter_id=None,
            volume_id=None,
            profile_override=None,
        )
        session = session_factory(workspace_id)
        try:
            now = datetime.now(timezone.utc)
            session.add(
                ChapterORM(
                    chapter_id="ch-0001",
                    workspace_id=workspace_id,
                    current_version=1,
                    status="approved",
                    rewrite_count=0,
                    revise_count=0,
                    created_at=now,
                    approved_at=now,
                    total_token_usage={},
                    total_cost_usd=0.0,
                    ready_for_next_chapter_at=now,
                )
            )
            run = session.get(PipelineRunORM, started.run_id)
            assert run is not None
            run.state = "paused"
            run.pause_reason = "user_request"
            run.current_chapter = "ch-0001"
            session.commit()
        finally:
            session.close()

        service.resume(workspace_id)
        service.execute(workspace_id, started.run_id)

        status = service.status(workspace_id)
        assert status.state == "paused"
        assert status.pause_reason == "continuity_checkpoint_corruption"
        assert status.completed_chapter_ids == []
        assert fake_chapter.calls == []

    def test_execute_auto_pause_on_invalidated_chapter(
        self,
        workspace_service: WorkspaceService,
        workspace_id: str,
        session_factory: Callable[[str], Session],
        fake_ws_manager: _FakeWSManager,
    ) -> None:
        """invalidated gate 联动:撞 ChapterInvalidatedNeedsUnblockError → 自动
        pause + reason=chapter_invalidated + 不撤回已完成章节(plan §10 gate 类)。"""
        _seed_workspace_and_volume(
            session_factory, workspace_id, volumes=[("vol-1", 3)]
        )
        # ch-0002 撞 invalidated;ch-0001 成功
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
        snap = svc.start(
            workspace_id,
            scope="range",
            start_chapter_id="ch-0001",
            end_chapter_id="ch-0003",
            volume_id=None,
            profile_override=None,
        )
        svc.execute(workspace_id, snap.run_id)

        # ch-0001 成功;ch-0002 撞 invalidated;ch-0003 未跑
        assert [c[1] for c in fake_chapter.calls] == ["ch-0001", "ch-0002"]
        # state 翻 paused,reason=chapter_invalidated
        status = svc.status(workspace_id)
        assert status.state == "paused"
        assert status.pause_reason == "chapter_invalidated"
        assert status.completed_chapter_ids == ["ch-0001"]
        assert status.current_chapter == "ch-0002"

        # WS event 含 pipeline_paused + data.reason=chapter_invalidated
        paused_events = [
            e for e in fake_ws_manager.events if e["event"] == "pipeline_paused"
        ]
        assert len(paused_events) == 1
        assert paused_events[0]["data"]["reason"] == "chapter_invalidated"
        assert paused_events[0]["data"]["chapter_id"] == "ch-0002"

    def test_execute_marks_error_on_unexpected_exception(
        self,
        workspace_service: WorkspaceService,
        workspace_id: str,
        session_factory: Callable[[str], Session],
        fake_ws_manager: _FakeWSManager,
    ) -> None:
        """rollback 类:chapter_service.write 抛通用异常 → state=error +
        error_details + 不冒泡(plan §10 rollback 类)。"""
        _seed_workspace_and_volume(
            session_factory, workspace_id, volumes=[("vol-1", 3)]
        )
        fake_chapter = _FakeChapterService(
            raise_on={"ch-0001": RuntimeError("LLM 调用挂了")}
        )
        svc = PipelineService(
            chapter_service=fake_chapter,  # type: ignore[arg-type]
            workspace_service=workspace_service,
            session_factory=session_factory,
            ws_manager=fake_ws_manager,  # type: ignore[arg-type]
        )
        snap = svc.start(
            workspace_id,
            scope="single",
            start_chapter_id="ch-0001",
            end_chapter_id=None,
            volume_id=None,
            profile_override=None,
        )
        # 不应冒泡
        svc.execute(workspace_id, snap.run_id)

        status = svc.status(workspace_id)
        assert status.state == "error"
        assert status.error_details is not None
        assert status.error_details["error_type"] == "RuntimeError"
        assert status.error_details["chapter_id"] == "ch-0001"

    def test_execute_persists_safe_provider_error_fields(
        self,
        workspace_service: WorkspaceService,
        workspace_id: str,
        session_factory: Callable[[str], Session],
        fake_ws_manager: _FakeWSManager,
    ) -> None:
        """Provider 错误只落白名单诊断字段，供后续按真实 code 处置。"""

        _seed_workspace_and_volume(session_factory, workspace_id)
        provider_error = LLMAccessError(
            "provider access denied",
            status_code=403,
            provider_code="AllocationQuota.FreeTierOnly",
            provider_message="Free quota exhausted",
            provider_request_id="req-provider-403",
        )
        fake_chapter = _FakeChapterService(
            raise_on={"ch-0001": provider_error}
        )
        svc = PipelineService(
            chapter_service=fake_chapter,  # type: ignore[arg-type]
            workspace_service=workspace_service,
            session_factory=session_factory,
            ws_manager=fake_ws_manager,  # type: ignore[arg-type]
        )
        snap = svc.start(
            workspace_id,
            scope="single",
            start_chapter_id="ch-0001",
            end_chapter_id=None,
            volume_id=None,
            profile_override=None,
        )
        svc.execute(workspace_id, snap.run_id)

        session = session_factory(workspace_id)
        try:
            row = session.get(PipelineRunORM, snap.run_id)
            assert row is not None
            assert row.error_details == {
                "error_type": "LLMAccessError",
                "message": "provider access denied",
                "chapter_id": "ch-0001",
                "status_code": 403,
                "provider_code": "AllocationQuota.FreeTierOnly",
                "provider_message": "Free quota exhausted",
                "provider_request_id": "req-provider-403",
            }
        finally:
            session.close()

    @pytest.mark.parametrize("failure_policy", ["retry", "skip"])
    def test_retained_operation_lease_pauses_before_retry_or_skip(
        self,
        failure_policy: str,
        workspace_service: WorkspaceService,
        workspace_id: str,
        session_factory: Callable[[str], Session],
        fake_ws_manager: _FakeWSManager,
    ) -> None:
        """普通异常也不能越过已物化或结果未知的持久操作租约。"""

        _seed_workspace_and_volume(
            session_factory, workspace_id, volumes=[("vol-1", 2)]
        )
        fake_chapter = _FakeChapterService(
            raise_on={"ch-0001": RuntimeError("响应后本地提交失败")},
            release_operation_task_result=False,
        )
        svc = PipelineService(
            chapter_service=fake_chapter,  # type: ignore[arg-type]
            workspace_service=workspace_service,
            session_factory=session_factory,
            ws_manager=fake_ws_manager,  # type: ignore[arg-type]
        )
        snap = svc.start(
            workspace_id,
            scope="range",
            start_chapter_id="ch-0001",
            end_chapter_id="ch-0002",
            volume_id=None,
            profile_override=None,
            failure_policy=failure_policy,  # type: ignore[arg-type]
        )

        svc.execute(workspace_id, snap.run_id)

        status = svc.status(workspace_id)
        assert status.state == "paused"
        assert status.pause_reason == "chapter_operation_safety_block"
        assert status.current_chapter == "ch-0001"
        assert status.completed_chapter_ids == []
        assert status.skipped_chapter_ids == []
        assert [call[1] for call in fake_chapter.calls] == ["ch-0001"]
        assert len(fake_chapter.release_operation_task_calls) == 1

    def test_execute_pauses_on_continuity_ledger_not_ready(
        self,
        workspace_service: WorkspaceService,
        workspace_id: str,
        session_factory: Callable[[str], Session],
        fake_ws_manager: _FakeWSManager,
    ) -> None:
        """未来 ledger checkpoint 失败直接 paused，不落不可恢复 error。"""
        _seed_workspace_and_volume(session_factory, workspace_id)
        fake_chapter = _FakeChapterService(
            raise_on={
                "ch-0001": ContinuityLedgerNotReadyError(
                    chapter_id="ch-0001",
                    reason="quote invalid",
                )
            }
        )
        svc = PipelineService(
            chapter_service=fake_chapter,  # type: ignore[arg-type]
            workspace_service=workspace_service,
            session_factory=session_factory,
            ws_manager=fake_ws_manager,  # type: ignore[arg-type]
        )
        snap = svc.start(
            workspace_id,
            scope="single",
            start_chapter_id="ch-0001",
            end_chapter_id=None,
            volume_id=None,
            profile_override=None,
        )

        svc.execute(workspace_id, snap.run_id)

        status = svc.status(workspace_id)
        assert status.state == "paused"
        assert status.pause_reason == "continuity_ledger_not_ready"
        assert status.current_chapter == "ch-0001"
        assert status.completed_chapter_ids == []
        paused_events = [
            event
            for event in fake_ws_manager.events
            if event["event"] == "pipeline_paused"
        ]
        assert paused_events[-1]["data"]["reason"] == ("continuity_ledger_not_ready")
        session = session_factory(workspace_id)
        try:
            attempts = list(
                session.scalars(
                    select(AttemptLedgerORM).where(
                        AttemptLedgerORM.run_id == snap.run_id,
                        AttemptLedgerORM.attempt_kind == "pipeline",
                    )
                )
            )
            assert len(attempts) == 1
            assert attempts[0].status == "failed"
            assert attempts[0].completed_at is not None
            assert attempts[0].error_type == "ContinuityLedgerNotReadyError"
        finally:
            session.close()

    @pytest.mark.parametrize(
        ("status", "pause_reason"),
        [
            ("pending", "memory_enrichment_not_ready"),
            ("failed", "memory_enrichment_failed"),
        ],
    )
    def test_current_chapter_memory_must_succeed_before_run_completion(
        self,
        status: str,
        pause_reason: str,
        workspace_service: WorkspaceService,
        workspace_id: str,
        session_factory: Callable[[str], Session],
        fake_ws_manager: _FakeWSManager,
    ) -> None:
        """正文已完成也不能在富记忆 pending/failed 时记成 Run completed。"""
        _seed_workspace_and_volume(session_factory, workspace_id)
        memory_error = MemoryEnrichmentNotReadyError(
            chapter_id="ch-0001",
            status=status,
            reason="fixture",
        )
        fake_chapter = _FakeChapterService(
            memory_raise_on={"ch-0001": memory_error}
        )
        svc = PipelineService(
            chapter_service=fake_chapter,  # type: ignore[arg-type]
            workspace_service=workspace_service,
            session_factory=session_factory,
            ws_manager=fake_ws_manager,  # type: ignore[arg-type]
        )
        snap = svc.start(
            workspace_id,
            scope="single",
            start_chapter_id="ch-0001",
            end_chapter_id=None,
            volume_id=None,
            profile_override=None,
        )

        svc.execute(workspace_id, snap.run_id)

        current = svc.status(workspace_id)
        assert current.state == "paused"
        assert current.pause_reason == pause_reason
        assert current.completed_chapter_ids == []
        assert [call[1] for call in fake_chapter.calls] == ["ch-0001"]
        session = session_factory(workspace_id)
        try:
            attempt = session.scalar(
                select(AttemptLedgerORM).where(
                    AttemptLedgerORM.run_id == snap.run_id,
                    AttemptLedgerORM.attempt_kind == "pipeline",
                )
            )
            assert attempt is not None
            assert attempt.status == "failed"
            assert attempt.error_type == "MemoryEnrichmentNotReadyError"
        finally:
            session.close()

    def test_previous_memory_gate_runs_before_next_card_or_pipeline_attempt(
        self,
        workspace_service: WorkspaceService,
        workspace_id: str,
        session_factory: Callable[[str], Session],
        fake_ws_manager: _FakeWSManager,
    ) -> None:
        """单章 Run 从 ch2 开始时，也必须先等真实 ch1 富记忆成功。"""
        _seed_workspace_and_volume(session_factory, workspace_id)
        fake_chapter = _FakeChapterService(
            memory_raise_on={
                "ch-0001": MemoryEnrichmentNotReadyError(
                    chapter_id="ch-0001",
                    status="running",
                    reason="existing worker",
                )
            }
        )
        svc = PipelineService(
            chapter_service=fake_chapter,  # type: ignore[arg-type]
            workspace_service=workspace_service,
            session_factory=session_factory,
            ws_manager=fake_ws_manager,  # type: ignore[arg-type]
        )
        snap = svc.start(
            workspace_id,
            scope="single",
            start_chapter_id="ch-0002",
            end_chapter_id=None,
            volume_id=None,
            profile_override=None,
        )

        svc.execute(workspace_id, snap.run_id)

        current = svc.status(workspace_id)
        assert current.state == "paused"
        assert current.pause_reason == "memory_enrichment_not_ready"
        assert current.current_chapter == "ch-0002"
        assert fake_chapter.memory_calls == ["ch-0001"]
        assert fake_chapter.checkpoint_calls == []
        assert fake_chapter.calls == []
        session = session_factory(workspace_id)
        try:
            attempts = list(
                session.scalars(
                    select(AttemptLedgerORM).where(
                        AttemptLedgerORM.run_id == snap.run_id,
                        AttemptLedgerORM.attempt_kind == "pipeline",
                    )
                )
            )
            assert attempts == []
        finally:
            session.close()

    def test_in_scope_previous_memory_blocker_becomes_recoverable_current(
        self,
        workspace_service: WorkspaceService,
        workspace_id: str,
        session_factory: Callable[[str], Session],
        fake_ws_manager: _FakeWSManager,
    ) -> None:
        """范围内前置章阻断时 current 指回它，Continue 先补记忆再写下一章。"""

        memory_error = MemoryEnrichmentNotReadyError(
            chapter_id="ch-0001",
            status="failed",
            reason="known extractor failure",
        )
        fake_chapter = _FakeChapterService(
            checkpoint_resume_on={"ch-0001"},
            memory_raise_on={"ch-0001": memory_error},
        )
        svc = PipelineService(
            chapter_service=fake_chapter,  # type: ignore[arg-type]
            workspace_service=workspace_service,
            session_factory=session_factory,
            ws_manager=fake_ws_manager,  # type: ignore[arg-type]
        )
        _seed_workspace_and_volume(
            session_factory,
            workspace_id,
            volumes=[("vol-1", 2)],
        )
        session = session_factory(workspace_id)
        try:
            now = datetime.now(timezone.utc)
            session.add(
                ChapterORM(
                    chapter_id="ch-0001",
                    workspace_id=workspace_id,
                    current_version=1,
                    status="approved",
                    rewrite_count=0,
                    revise_count=0,
                    created_at=now,
                    approved_at=now,
                    total_token_usage={},
                    total_cost_usd=0.0,
                    ready_for_next_chapter_at=now,
                )
            )
            session.commit()
        finally:
            session.close()
        started = svc.start(
            workspace_id,
            scope="range",
            start_chapter_id="ch-0001",
            end_chapter_id="ch-0002",
            volume_id=None,
            profile_override=None,
        )

        svc.execute(workspace_id, started.run_id)

        blocked = svc.status(workspace_id)
        assert blocked.state == "paused"
        assert blocked.pause_reason == "memory_enrichment_failed"
        assert blocked.current_chapter == "ch-0001"
        assert blocked.completed_chapter_ids == []
        assert fake_chapter.calls == []

        fake_chapter._checkpoint_recovers_memory_on.add("ch-0001")
        svc.resume(workspace_id)
        svc.execute(
            workspace_id,
            started.run_id,
            explicit_resume=True,
        )

        completed = svc.status(workspace_id)
        assert completed.state == "stopped"
        assert completed.pause_reason == "completed"
        assert completed.completed_chapter_ids == ["ch-0001", "ch-0002"]
        assert [call[1] for call in fake_chapter.calls] == ["ch-0002"]
        assert fake_chapter.checkpoint_calls == ["ch-0001", "ch-0002"]

    def test_same_run_checkpoint_resume_after_two_historical_failures(
        self,
        workspace_service: WorkspaceService,
        workspace_id: str,
        session_factory: Callable[[str], Session],
        fake_ws_manager: _FakeWSManager,
    ) -> None:
        """两条旧 attempt 不得把根因修复后的同 run checkpoint 永久卡死。"""
        _seed_workspace_and_volume(session_factory, workspace_id)
        checkpoint_error = ContinuityLedgerNotReadyError(
            chapter_id="ch-0001",
            reason="quote invalid",
        )
        fake_chapter = _FakeChapterService(
            checkpoint_raise_on={"ch-0001": checkpoint_error},
        )
        svc = PipelineService(
            chapter_service=fake_chapter,  # type: ignore[arg-type]
            workspace_service=workspace_service,
            session_factory=session_factory,
            ws_manager=fake_ws_manager,  # type: ignore[arg-type]
        )
        started = svc.start(
            workspace_id,
            scope="single",
            start_chapter_id="ch-0001",
            end_chapter_id=None,
            volume_id=None,
            profile_override=None,
        )

        # 两次产品 invocation 都留下失败事实，并停在同一可恢复 checkpoint。
        svc.execute(workspace_id, started.run_id)
        assert svc.status(workspace_id).state == "paused"
        svc.resume(workspace_id)
        svc.execute(workspace_id, started.run_id)
        assert svc.status(workspace_id).state == "paused"

        # 模拟卡内根因修复：第三次只补 checkpoint，不重写正文。
        fake_chapter._checkpoint_raise_on.clear()
        fake_chapter._checkpoint_resume_on.add("ch-0001")
        resumed = svc.resume(workspace_id)
        assert resumed.run_id == started.run_id
        svc.execute(workspace_id, started.run_id)

        status = svc.status(workspace_id)
        assert status.run_id == started.run_id
        assert status.state == "stopped"
        assert status.completed_chapter_ids == ["ch-0001"]
        assert status.pipeline_attempt_count == 3
        assert fake_chapter.checkpoint_calls == [
            "ch-0001",
            "ch-0001",
            "ch-0001",
        ]
        assert fake_chapter.calls == []

        session = session_factory(workspace_id)
        try:
            attempts = list(
                session.scalars(
                    select(AttemptLedgerORM)
                    .where(AttemptLedgerORM.run_id == started.run_id)
                    .where(AttemptLedgerORM.attempt_kind == "pipeline")
                    .order_by(AttemptLedgerORM.attempt_number)
                )
            )
        finally:
            session.close()
        assert [row.attempt_number for row in attempts] == [1, 2, 3]
        assert [row.status for row in attempts] == [
            "failed",
            "failed",
            "succeeded",
        ]
        assert [row.error_type for row in attempts] == [
            "ContinuityLedgerNotReadyError",
            "ContinuityLedgerNotReadyError",
            None,
        ]

    def test_checkpoint_corruption_bypasses_skip_and_pauses_before_next_chapter(
        self,
        workspace_service: WorkspaceService,
        workspace_id: str,
        session_factory: Callable[[str], Session],
        fake_ws_manager: _FakeWSManager,
    ) -> None:
        """真实 checkpoint 损坏无条件暂停；skip 也不能越过或写下一章。"""
        _seed_workspace_and_volume(
            session_factory,
            workspace_id,
            volumes=[("vol-1", 2)],
        )
        fake_chapter = _FakeChapterService(
            checkpoint_raise_on={
                "ch-0001": ContinuityCheckpointCorruptionError(
                    chapter_id="ch-0001",
                    reason="final/db drift",
                )
            }
        )
        svc = PipelineService(
            chapter_service=fake_chapter,  # type: ignore[arg-type]
            workspace_service=workspace_service,
            session_factory=session_factory,
            ws_manager=fake_ws_manager,  # type: ignore[arg-type]
        )
        snap = svc.start(
            workspace_id,
            scope="range",
            start_chapter_id="ch-0001",
            end_chapter_id="ch-0002",
            volume_id=None,
            profile_override=None,
            failure_policy="skip",
        )

        svc.execute(workspace_id, snap.run_id)

        status = svc.status(workspace_id)
        assert status.state == "paused"
        assert status.pause_reason == "continuity_checkpoint_corruption"
        assert status.completed_chapter_ids == []
        assert status.skipped_chapter_ids == []
        assert fake_chapter.checkpoint_calls == ["ch-0001"]
        assert fake_chapter.calls == []
        session = session_factory(workspace_id)
        try:
            attempts = list(
                session.scalars(
                    select(AttemptLedgerORM).where(
                        AttemptLedgerORM.run_id == snap.run_id,
                        AttemptLedgerORM.attempt_kind == "pipeline",
                    )
                )
            )
            assert len(attempts) == 1
            assert attempts[0].status == "failed"
            assert attempts[0].completed_at is not None
            assert attempts[0].error_type == ("ContinuityCheckpointCorruptionError")
        finally:
            session.close()

    def test_execute_revalidates_completed_terminal_before_skipping(
        self,
        workspace_service: WorkspaceService,
        workspace_id: str,
        session_factory: Callable[[str], Session],
        fake_ws_manager: _FakeWSManager,
    ) -> None:
        """每次 resume 都复核 completed terminal，不能因 run 进度绕过损坏。"""
        _seed_workspace_and_volume(
            session_factory,
            workspace_id,
            volumes=[("vol-1", 2)],
        )
        corruption = ContinuityCheckpointCorruptionError(
            chapter_id="ch-0001",
            reason="ledger hash drift",
        )
        fake_chapter = _FakeChapterService(ready_raise_on={"ch-0001": corruption})
        svc = PipelineService(
            chapter_service=fake_chapter,  # type: ignore[arg-type]
            workspace_service=workspace_service,
            session_factory=session_factory,
            ws_manager=fake_ws_manager,  # type: ignore[arg-type]
        )
        snap = svc.start(
            workspace_id,
            scope="range",
            start_chapter_id="ch-0001",
            end_chapter_id="ch-0002",
            volume_id=None,
            profile_override=None,
        )
        session = session_factory(workspace_id)
        try:
            now = datetime.now(timezone.utc)
            session.add(
                ChapterORM(
                    chapter_id="ch-0001",
                    workspace_id=workspace_id,
                    current_version=1,
                    status="approved",
                    rewrite_count=0,
                    revise_count=0,
                    created_at=now,
                    approved_at=now,
                    total_token_usage={},
                    total_cost_usd=0.0,
                    ready_for_next_chapter_at=now,
                )
            )
            run = session.get(PipelineRunORM, snap.run_id)
            assert run is not None
            run.completed_chapter_ids = ["ch-0001"]
            session.commit()
        finally:
            session.close()

        svc.execute(workspace_id, snap.run_id)

        status = svc.status(workspace_id)
        assert status.state == "paused"
        assert status.pause_reason == "continuity_checkpoint_corruption"
        assert status.completed_chapter_ids == ["ch-0001"]
        assert status.skipped_chapter_ids == []
        assert fake_chapter.ready_calls == ["ch-0001"]
        assert fake_chapter.checkpoint_calls == []
        assert fake_chapter.calls == []

    def test_execute_resume_after_pause_continues_remaining_chapters(
        self,
        service: PipelineService,
        workspace_id: str,
        session_factory: Callable[[str], Session],
        fake_chapter_service: _FakeChapterService,
    ) -> None:
        """pause/resume 集成:start → execute → pause(模拟外部 :pause)→
        resume → execute → 从未完处继续。"""
        _seed_workspace_and_volume(
            session_factory, workspace_id, volumes=[("vol-1", 3)]
        )
        snap = service.start(
            workspace_id,
            scope="range",
            start_chapter_id="ch-0001",
            end_chapter_id="ch-0003",
            volume_id=None,
            profile_override=None,
        )
        # 模拟"跑一章后被外部 pause":先手动 pause,再 execute(应立即退出)
        service.pause(workspace_id)
        service.execute(workspace_id, snap.run_id)
        # paused 状态下不调 chapter_service.write
        assert fake_chapter_service.calls == []
        # resume → execute 继续
        service.resume(workspace_id)
        service.execute(workspace_id, snap.run_id)
        # 现在应跑完所有 3 章
        assert [c[1] for c in fake_chapter_service.calls] == [
            "ch-0001",
            "ch-0002",
            "ch-0003",
        ]
        # 最终 state = stopped(completed)
        assert service.status(workspace_id).state == "stopped"

    def test_execute_profile_override_propagated_to_chapter_service(
        self,
        service: PipelineService,
        workspace_id: str,
        session_factory: Callable[[str], Session],
        fake_chapter_service: _FakeChapterService,
    ) -> None:
        _seed_workspace_and_volume(
            session_factory, workspace_id, volumes=[("vol-1", 2)]
        )
        snap = service.start(
            workspace_id,
            scope="range",
            start_chapter_id="ch-0001",
            end_chapter_id="ch-0002",
            volume_id=None,
            profile_override="cloud_premium",
        )
        service.execute(workspace_id, snap.run_id)
        # 每次 write 调用都带 profile_override="cloud_premium"
        assert all(c[2] == "cloud_premium" for c in fake_chapter_service.calls)

    def test_production_manifest_persists_identity_without_selection_breakers(
        self,
        workspace_service: WorkspaceService,
        workspace_id: str,
        session_factory: Callable[[str], Session],
        fake_ws_manager: _FakeWSManager,
    ) -> None:
        _seed_workspace_and_volume(session_factory, workspace_id)
        fake_chapter = _ManifestChapterService()
        svc = PipelineService(
            chapter_service=fake_chapter,  # type: ignore[arg-type]
            workspace_service=workspace_service,
            session_factory=session_factory,
            ws_manager=fake_ws_manager,  # type: ignore[arg-type]
        )

        snap = svc.start(
            workspace_id,
            scope="single",
            start_chapter_id="ch-0001",
            end_chapter_id=None,
            volume_id=None,
            profile_override=None,
            provider_call_cap=23,
        )
        assert snap.profile_override == fake_chapter.binding.token
        assert snap.provider_call_cap == 23

        fake_chapter.binding = RunModelManifestBinding(
            manifest_id="deepseek-v4-flash",
            token="run-model-manifest:test:drifted",
            selection_test=SelectionTestLossBreakerBinding(
                activity_scope="selection_test",
                token="selection-test:run-model-manifest:test:drifted:v1",
                max_physical_requests=7,
                max_configured_rmb=Decimal("2.50"),
            ),
        )
        svc.execute(workspace_id, snap.run_id)

        status = svc.status(workspace_id)
        assert status.state == "error"
        assert status.error_details == {
            "reason": "RUN_MODEL_MANIFEST_DRIFT",
            "chapter_id": "ch-0001",
        }
        assert fake_chapter.calls == []

    def test_production_manifest_executes_original_provider_cap_not_test_breakers(
        self,
        workspace_service: WorkspaceService,
        workspace_id: str,
        session_factory: Callable[[str], Session],
        fake_ws_manager: _FakeWSManager,
    ) -> None:
        _seed_workspace_and_volume(
            session_factory,
            workspace_id,
            volumes=[("vol-1", 1)],
        )
        fake_chapter = _ManifestChapterService()
        svc = PipelineService(
            chapter_service=fake_chapter,  # type: ignore[arg-type]
            workspace_service=workspace_service,
            session_factory=session_factory,
            ws_manager=fake_ws_manager,  # type: ignore[arg-type]
        )

        snap = svc.start(
            workspace_id,
            scope="single",
            start_chapter_id="ch-0001",
            end_chapter_id=None,
            volume_id=None,
            profile_override=None,
            provider_call_cap=23,
        )
        svc.execute(workspace_id, snap.run_id)

        assert len(fake_chapter.observed_attempt_scopes) == 1
        observed = fake_chapter.observed_attempt_scopes[0]
        assert observed.activity_scope == "production"
        assert observed.provider_call_cap == 23
        assert observed.run_model_manifest_token == fake_chapter.binding.token
        assert observed.selection_test_manifest_token is None
        assert observed.configured_cost_currency is None
        assert observed.configured_cost_cap is None
        assert fake_chapter.calls == [
            (workspace_id, "ch-0001", fake_chapter.binding.token)
        ]

    def test_only_explicit_selection_scope_persists_selection_breakers(
        self,
        workspace_service: WorkspaceService,
        workspace_id: str,
        session_factory: Callable[[str], Session],
        fake_ws_manager: _FakeWSManager,
    ) -> None:
        _seed_workspace_and_volume(session_factory, workspace_id)
        fake_chapter = _ManifestChapterService()
        svc = PipelineService(
            chapter_service=fake_chapter,  # type: ignore[arg-type]
            workspace_service=workspace_service,
            session_factory=session_factory,
            ws_manager=fake_ws_manager,  # type: ignore[arg-type]
        )

        with bind_attempt_scope(
            workspace_id=workspace_id,
            activity_scope="selection_test",
            run_model_manifest_token=fake_chapter.binding.token,
            selection_test_manifest_token=(
                fake_chapter.binding.selection_test.token
            ),
        ):
            snap = svc.start(
                workspace_id,
                scope="single",
                start_chapter_id="ch-0001",
                end_chapter_id=None,
                volume_id=None,
                profile_override=None,
            )

        assert snap.profile_override == fake_chapter.binding.selection_test.token
        assert snap.provider_call_cap == 7
        svc.execute(workspace_id, snap.run_id)

        assert len(fake_chapter.observed_attempt_scopes) == 1
        observed = fake_chapter.observed_attempt_scopes[0]
        assert observed.activity_scope == "selection_test"
        assert observed.provider_call_cap == 7
        assert observed.run_model_manifest_token == fake_chapter.binding.token
        assert (
            observed.selection_test_manifest_token
            == fake_chapter.binding.selection_test.token
        )
        assert observed.configured_cost_currency == "CNY"
        assert observed.configured_cost_cap == Decimal("2.50")

    def test_explicit_selection_scope_rejects_repository_zero_zero_breakers(
        self,
        workspace_service: WorkspaceService,
        workspace_id: str,
        session_factory: Callable[[str], Session],
        fake_ws_manager: _FakeWSManager,
    ) -> None:
        _seed_workspace_and_volume(session_factory, workspace_id)
        fake_chapter = _ManifestChapterService()
        fake_chapter.binding = RunModelManifestBinding(
            manifest_id="deepseek-v4-flash",
            token=fake_chapter.binding.token,
            selection_test=SelectionTestLossBreakerBinding(
                activity_scope="selection_test",
                token=fake_chapter.binding.selection_test.token,
                max_physical_requests=0,
                max_configured_rmb=Decimal("0"),
            ),
        )
        svc = PipelineService(
            chapter_service=fake_chapter,  # type: ignore[arg-type]
            workspace_service=workspace_service,
            session_factory=session_factory,
            ws_manager=fake_ws_manager,  # type: ignore[arg-type]
        )

        with bind_attempt_scope(
            workspace_id=workspace_id,
            activity_scope="selection_test",
            run_model_manifest_token=fake_chapter.binding.token,
            selection_test_manifest_token=(
                fake_chapter.binding.selection_test.token
            ),
        ):
            with pytest.raises(PipelineServiceError, match="0/0"):
                svc.start(
                    workspace_id,
                    scope="single",
                    start_chapter_id="ch-0001",
                    end_chapter_id=None,
                    volume_id=None,
                    profile_override=None,
                )

    def test_manifest_rejects_independent_pipeline_profile_override(
        self,
        workspace_service: WorkspaceService,
        workspace_id: str,
        session_factory: Callable[[str], Session],
        fake_ws_manager: _FakeWSManager,
    ) -> None:
        _seed_workspace_and_volume(session_factory, workspace_id)
        svc = PipelineService(
            chapter_service=_ManifestChapterService(),  # type: ignore[arg-type]
            workspace_service=workspace_service,
            session_factory=session_factory,
            ws_manager=fake_ws_manager,  # type: ignore[arg-type]
        )

        with pytest.raises(PipelineServiceError, match="禁止另传 profile_override"):
            svc.start(
                workspace_id,
                scope="single",
                start_chapter_id="ch-0001",
                end_chapter_id=None,
                volume_id=None,
                profile_override="cloud_premium",
            )


# ---- WS publish swallow rollback ------------------------------------------


class TestWsPublishSwallow:
    def test_publish_failure_swallowed_no_propagate(
        self,
        workspace_service: WorkspaceService,
        workspace_id: str,
        session_factory: Callable[[str], Session],
    ) -> None:
        """WS publish 抛异常时 service 不冒泡(对齐 P6-B-01 swallow 模式)。"""
        _seed_workspace_and_volume(session_factory, workspace_id)

        class _FailingWS:
            def publish(self, **kwargs: Any) -> None:
                raise RuntimeError("ws 挂了")

        svc = PipelineService(
            chapter_service=_FakeChapterService(),  # type: ignore[arg-type]
            workspace_service=workspace_service,
            session_factory=session_factory,
            ws_manager=_FailingWS(),  # type: ignore[arg-type]
        )
        # start 内部 publish 失败 swallow,不冒泡
        snap = svc.start(
            workspace_id,
            scope="single",
            start_chapter_id="ch-0001",
            end_chapter_id=None,
            volume_id=None,
            profile_override=None,
        )
        assert snap.state == "running"


@pytest.mark.parametrize("card_status", [None, "pending_review", "approved"])
def test_pipeline_waits_for_commit_first_card_without_generating(
    workspace_service, workspace_id, session_factory, card_status
):
    from unittest.mock import Mock
    from src.api import task_registry

    _seed_workspace_and_volume(session_factory, workspace_id)
    if card_status is not None:
        with session_factory(workspace_id) as session:
            session.add(ChapterCardORM(workspace_id=workspace_id, chapter_id="ch-0001", card_status=card_status, created_at=datetime.now(timezone.utc)))
            session.commit()
    card_service = Mock()
    service = PipelineService(
        workspace_service=workspace_service,
        chapter_service=_FakeChapterService(),
        session_factory=session_factory,
        chapter_card_service=card_service,
    )
    task_registry.record_pending("auto-first", type="chapter_card_first_after_commit", workspace_id=workspace_id)
    failure = service._ensure_chapter_card_ready(workspace_id, "ch-0001")
    assert failure["reason"] == "card_generation_in_progress"
    assert failure["stop_mode"] == "pause"
    assert not card_service.mock_calls
    if card_status is not None:
        task_registry.update_to_done("auto-first", result={})
        assert service._ensure_chapter_card_ready(workspace_id, "ch-0001") is None
        card_service.generate_next_card.assert_not_called()
