"""W3.5-C 写作水位拆分回归测试。

核心契约:
- approved 后终稿 + DB approved + ready 水位先落账；手动章节操作可独立查看;
- 正式 Run 必须再等摘要 / memory extractor / 向量等富记忆成功，才生成下一章卡;
- 手动 Chapter UI 的富记忆仍可后台执行，失败必须显性记录;
- ready 硬水位失败要抛错,不能静默带病续跑。
"""

from __future__ import annotations

from collections.abc import Callable
from copy import deepcopy
from datetime import datetime, timezone
from pathlib import Path

import pytest
from sqlalchemy import create_engine, select
from sqlalchemy.orm import Session
from structlog.testing import capture_logs

from src.api.errors import CostGuardTriggered
from src.core.agents.chapter_summary_extractor import (
    continuity_ledger_provenance,
    load_validated_continuity_ledger,
)
from src.core.chapter_versioning import ChapterVersioning
from src.core.db import (
    Base,
    ChapterORM,
    ChapterSummaryORM,
    ChapterVersionORM,
    PipelineRunORM,
    ReviewResultORM,
    VolumeORM,
)
from src.core.db.models import AttemptLedgerORM
from src.core.llm.mock_client import MockLLMClient
from src.core.memory.vector import VectorStore
from src.core.models.chapter_card import ChapterCard, ContinuityContext
from src.core.orchestrator.chapter_orchestrator import (
    MemoryExtractionOutcome,
    OrchestratorResult,
)
from src.core.state_machine.states import ChapterState
from src.core.workspace.workspace_paths import WorkspacePaths
from src.services.chapter_service import (
    ChapterService,
    ChapterServiceError,
    ContinuityCheckpointCorruptionError,
    ContinuityLedgerNotReadyError,
    MemoryEnrichmentNotReadyError,
)
from src.services.attempt_ledger import ProviderCallCapExceeded, bind_attempt_scope
from src.services.pipeline_service import PipelineService, PipelineWatchdogConfig
from src.services.workspace_service import WorkspaceService

pytestmark = pytest.mark.usefixtures("valid_character_admission")


def _mock_embedding_fn(texts: list[str]) -> list[list[float]]:
    return [[0.1, 0.2, 0.3, 0.4] for _ in texts]


def _make_continuity(**overrides) -> ContinuityContext:
    payload = dict(
        prev_chapter_tail="",
        unfinished_actions=[],
        time_gap="新场景",
        scene_continuation="switch",
        pov_change=False,
        pov_from=None,
        pov_to="林衍",
    )
    payload.update(overrides)
    return ContinuityContext(**payload)


def _make_card(chapter_id: str, chapter_number: int) -> ChapterCard:
    return ChapterCard(
        chapter_id=chapter_id,
        workspace_id="ws-watermark",
        volume_id="v01",
        chapter_number=chapter_number,
        title=f"第 {chapter_number} 章·水位",
        tension_level=6,
        word_count_min=1,
        word_count_max=4000,
        plot_node_budget=2,
        must_achieve=[],
        must_unresolved=[],
        forbidden=[],
        pov_character="林衍",
        setting="山门",
        continuity_context=_make_continuity(),
        next_chapter_seed="待续",
        arc_phase="rising",
        is_arc_finale=False,
        related_plot_threads=[],
        related_characters=["lin-yan"],
        review_threshold=7.5,
        revise_threshold=6.5,
        max_rewrites=1,
        max_revises=1,
    )


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
def prepared_workspace(workspace_service: WorkspaceService) -> str:
    workspace_service.create(
        "ws-watermark",
        display_name="写作水位测试",
        genre="test",
        premise="验证 ready 与 memory_enriched 拆分",
        total_chapters=3,
    )
    return "ws-watermark"


def _session_factory_for(workspace_service: WorkspaceService) -> Callable[[str], Session]:
    def _session_factory(workspace_id: str) -> Session:
        paths = WorkspacePaths(
            workspace_id, workspaces_root=workspace_service.workspaces_root
        )
        engine = create_engine(f"sqlite:///{paths.db_path}")
        Base.metadata.create_all(engine)
        return Session(engine)

    return _session_factory


def _vector_store_factory_for(
    workspace_service: WorkspaceService,
) -> Callable[[str], VectorStore]:
    def _vector_store_factory(workspace_id: str) -> VectorStore:
        paths = WorkspacePaths(
            workspace_id, workspaces_root=workspace_service.workspaces_root
        )
        return VectorStore(
            workspace_id,
            embedding_fn=_mock_embedding_fn,
            persist_dir=paths.chroma_dir,
        )

    return _vector_store_factory


def _chapter_row(workspace_service: WorkspaceService, chapter_id: str) -> ChapterORM:
    paths = WorkspacePaths(
        "ws-watermark", workspaces_root=workspace_service.workspaces_root
    )
    engine = create_engine(f"sqlite:///{paths.db_path}")
    with Session(engine) as session:
        row = session.get(ChapterORM, chapter_id)
        assert row is not None
        session.expunge(row)
        return row


def _attach_current_ledger_provenance(
    *,
    service: ChapterService,
    workspace_service: WorkspaceService,
    chapter_id: str,
    previous_chapter_id: str,
) -> None:
    """让测试中的下一章卡显式冻结刚完成的上一章同步小账本。"""

    paths = WorkspacePaths(
        "ws-watermark", workspaces_root=workspace_service.workspaces_root
    )
    ledger = load_validated_continuity_ledger(
        workspace_id="ws-watermark",
        chapter_id=previous_chapter_id,
        paths=paths,
        session_factory=_session_factory_for(workspace_service),
    )
    card = service.read_chapter_card("ws-watermark", chapter_id)
    continuity_context = card.continuity_context.model_copy(
        update={
            "prev_chapter_tail": ledger.raw_final_tail,
            "unfinished_actions": list(ledger.unfinished_actions),
            "pov_from": ledger.pov_character,
        }
    )
    service.save_chapter_card(
        "ws-watermark",
        card.model_copy(
            update={
                "continuity_ledger_provenance": continuity_ledger_provenance(
                    ledger
                ),
                "continuity_context": continuity_context,
            }
        ),
    )


class TestReadyForNextChapterWatermark:
    @pytest.mark.parametrize("refresh_known_summary", [False, True])
    def test_resume_approved_checkpoint_only_rebuilds_ledger_and_ready(
        self,
        workspace_service: WorkspaceService,
        prepared_workspace: str,
        monkeypatch: pytest.MonkeyPatch,
        refresh_known_summary: bool,
    ) -> None:
        """approved+ready 空补 ledger/ready，正文与审稿不重放，富记忆只排队。"""
        from src.services import chapter_service as chapter_service_module

        queued: list[Callable[[], None]] = []
        session_factory = _session_factory_for(workspace_service)
        svc = ChapterService(
            workspace_service=workspace_service,
            client=MockLLMClient(),
            model="claude-sonnet-4-6",
            session_factory=session_factory,
            vector_store_factory=_vector_store_factory_for(workspace_service),
            memory_enrichment_scheduler=queued.append,
        )
        svc.save_chapter_card(prepared_workspace, _make_card("ch-0001", 1))
        result = svc.write(prepared_workspace, "ch-0001")
        assert result.final_state == ChapterState.APPROVED
        queued.clear()

        paths = WorkspacePaths(
            prepared_workspace,
            workspaces_root=workspace_service.workspaces_root,
        )
        final_path = paths.chapter_version_file("ch-0001", "final.md")
        final_before = final_path.read_bytes()
        session = session_factory(prepared_workspace)
        try:
            row = session.get(ChapterORM, "ch-0001")
            assert row is not None
            approved_at_before = row.approved_at
            current_version_before = row.current_version
            row.ready_for_next_chapter_at = None
            row.memory_enrichment_details = None
            summary = session.get(ChapterSummaryORM, "ch-0001")
            assert summary is not None
            if refresh_known_summary:
                row.core_state_commit_details = {"conflicts": ["known relation gap"]}
                monkeypatch.setattr(
                    chapter_service_module.CoreStateService,
                    "require_pending_transition_candidate_recovery_safe",
                    lambda *args, **kwargs: True,
                )
                old_ledger_hash = summary.ledger_hash
            else:
                session.delete(summary)
            version_rows_before = [
                {
                    column.name: deepcopy(getattr(version, column.name))
                    for column in ChapterVersionORM.__table__.columns
                }
                for version in session.scalars(
                    select(ChapterVersionORM).order_by(
                        ChapterVersionORM.chapter_id,
                        ChapterVersionORM.version_id,
                    )
                ).all()
            ]
            review_rows_before = [
                {
                    column.name: deepcopy(getattr(review, column.name))
                    for column in ReviewResultORM.__table__.columns
                }
                for review in session.scalars(
                    select(ReviewResultORM).order_by(ReviewResultORM.review_id)
                ).all()
            ]
            session.commit()
        finally:
            session.close()

        extractor_calls: list[int] = []
        extractor_snapshots: list[tuple[str | None, ChapterCard | None]] = []
        original_extractor = (
            chapter_service_module.extract_and_persist_continuity_ledger
        )

        def _spy_extractor(**kwargs):
            extractor_calls.append(kwargs["source_version_id"])
            extractor_snapshots.append(
                (
                    kwargs.get("chapter_text_snapshot"),
                    kwargs.get("chapter_card_snapshot"),
                )
            )
            return original_extractor(**kwargs)

        monkeypatch.setattr(
            chapter_service_module,
            "extract_and_persist_continuity_ledger",
            _spy_extractor,
        )

        def _orchestrator_must_not_run(**_kwargs):
            raise AssertionError("checkpoint resume 不得调用 orchestrator")

        monkeypatch.setattr(
            chapter_service_module,
            "orchestrate_chapter",
            _orchestrator_must_not_run,
        )

        assert svc.resume_approved_checkpoint(prepared_workspace, "ch-0001")
        assert extractor_calls == [result.final_version_id]
        if refresh_known_summary:
            import json

            archive = paths.chapter_version_file(
                "ch-0001", f"summary-recovery-{old_ledger_hash}.json"
            )
            assert json.loads(archive.read_text())["ledger_hash"] == old_ledger_hash
        assert extractor_snapshots[0][0] == final_before.decode("utf-8")
        assert extractor_snapshots[0][1] is not None
        assert extractor_snapshots[0][1].chapter_id == "ch-0001"
        assert len(queued) == 1
        assert final_path.read_bytes() == final_before

        # ready 已落但 Worker 尚未 CAS 时，重复 resume 可再次提交闭包；真正
        # Provider 执行仍由 pending->running CAS 保证只有一个 Worker 获得。
        assert svc.resume_approved_checkpoint(prepared_workspace, "ch-0001")
        assert extractor_calls == [result.final_version_id]
        assert len(queued) == 2

        session = session_factory(prepared_workspace)
        try:
            row = session.get(ChapterORM, "ch-0001")
            assert row is not None
            assert row.status == "approved"
            assert row.approved_at == approved_at_before
            assert row.current_version == current_version_before
            assert row.ready_for_next_chapter_at is not None
            version_rows_after = [
                {
                    column.name: deepcopy(getattr(version, column.name))
                    for column in ChapterVersionORM.__table__.columns
                }
                for version in session.scalars(
                    select(ChapterVersionORM).order_by(
                        ChapterVersionORM.chapter_id,
                        ChapterVersionORM.version_id,
                    )
                ).all()
            ]
            review_rows_after = [
                {
                    column.name: deepcopy(getattr(review, column.name))
                    for column in ReviewResultORM.__table__.columns
                }
                for review in session.scalars(
                    select(ReviewResultORM).order_by(ReviewResultORM.review_id)
                ).all()
            ]
            assert version_rows_after == version_rows_before
            assert review_rows_after == review_rows_before
        finally:
            session.close()

    def test_resume_approved_checkpoint_retries_after_known_failed_summary_attempt(
        self,
        workspace_service: WorkspaceService,
        prepared_workspace: str,
        monkeypatch: pytest.MonkeyPatch,
    ) -> None:
        """summary 明确 failed 没有可消费结果，修复后可从 approved 水位重进。"""

        from src.services import chapter_service as chapter_service_module

        session_factory = _session_factory_for(workspace_service)
        svc = ChapterService(
            workspace_service=workspace_service,
            client=MockLLMClient(),
            model="claude-sonnet-4-6",
            session_factory=session_factory,
            memory_enrichment_scheduler=lambda _task: None,
        )
        svc.save_chapter_card(prepared_workspace, _make_card("ch-0001", 1))
        result = svc.write(prepared_workspace, "ch-0001")
        assert result.final_state == ChapterState.APPROVED

        with session_factory(prepared_workspace) as session:
            row = session.get(ChapterORM, "ch-0001")
            summary = session.get(ChapterSummaryORM, "ch-0001")
            assert row is not None
            assert row.approved_at is not None
            assert summary is not None
            row.ready_for_next_chapter_at = None
            session.delete(summary)
            now = datetime.now(timezone.utc)
            session.add(
                AttemptLedgerORM(
                    attempt_id="attempt-summary-known-failed",
                    workspace_id=prepared_workspace,
                    run_id=None,
                    task_id="task-summary-known-failed",
                    chapter_id="ch-0001",
                    attempt_kind="provider",
                    operation_kind="checkpoint-resume",
                    logical_call_id="logical-summary-known-failed",
                    agent_name="chapter_summary_extractor",
                    provider="mock",
                    model="mock-model",
                    attempt_number=1,
                    attempt_cap=3,
                    status="failed",
                    version_before=result.final_version_id,
                    version_after=None,
                    error_type="KnownFailure",
                    created_at=now,
                    completed_at=now,
                )
            )
            session.commit()

        calls: list[int] = []
        original_extractor = (
            chapter_service_module.extract_and_persist_continuity_ledger
        )

        def _spy_extractor(**kwargs):
            calls.append(kwargs["source_version_id"])
            return original_extractor(**kwargs)

        monkeypatch.setattr(
            chapter_service_module,
            "extract_and_persist_continuity_ledger",
            _spy_extractor,
        )

        assert svc.resume_approved_checkpoint(prepared_workspace, "ch-0001")
        assert calls == [result.final_version_id]

    @pytest.mark.parametrize("attempt_status", ["started", "succeeded"])
    def test_ready_pending_retains_unconsumed_memory_attempt_without_requeue(
        self,
        workspace_service: WorkspaceService,
        prepared_workspace: str,
        attempt_status: str,
    ) -> None:
        """pending 后已有出网证据时，Continue 保留现场且不重复排 Worker。"""

        queued: list[Callable[[], None]] = []
        session_factory = _session_factory_for(workspace_service)
        svc = ChapterService(
            workspace_service=workspace_service,
            client=MockLLMClient(),
            model="claude-sonnet-4-6",
            session_factory=session_factory,
            vector_store_factory=_vector_store_factory_for(workspace_service),
            memory_enrichment_scheduler=queued.append,
        )
        svc.save_chapter_card(prepared_workspace, _make_card("ch-0001", 1))
        result = svc.write(prepared_workspace, "ch-0001")
        assert result.final_state == ChapterState.APPROVED
        assert len(queued) == 1

        now = datetime.now(timezone.utc)
        with session_factory(prepared_workspace) as session:
            session.add(
                AttemptLedgerORM(
                    attempt_id=f"attempt-memory-{attempt_status}",
                    workspace_id=prepared_workspace,
                    run_id=None,
                    task_id="different-task-id-must-not-hide-request",
                    chapter_id="ch-0001",
                    attempt_kind="provider",
                    operation_kind="agent_call",
                    logical_call_id=f"logical-memory-{attempt_status}",
                    agent_name="memory_extractor",
                    provider="other-provider-still-must-block",
                    model="other-model-still-must-block",
                    attempt_number=1,
                    attempt_cap=3,
                    status=attempt_status,
                    version_before=result.final_version_id,
                    version_after=None,
                    error_type=None,
                    created_at=now,
                    completed_at=(
                        None if attempt_status == "started" else now
                    ),
                )
            )
            session.commit()

        with pytest.raises(
            ContinuityCheckpointCorruptionError,
            match="禁止重复排 Worker",
        ):
            svc.resume_approved_checkpoint(prepared_workspace, "ch-0001")
        assert len(queued) == 1
        with session_factory(prepared_workspace) as session:
            attempt = session.get(
                AttemptLedgerORM,
                f"attempt-memory-{attempt_status}",
            )
            assert attempt is not None
            assert attempt.status == attempt_status

    def test_ready_pending_requeues_only_attempts_with_explicit_retry_receipt(
        self,
        workspace_service: WorkspaceService,
        prepared_workspace: str,
    ) -> None:
        """显式恢复 CAS 后若进程中断，receipt 只消费精确旧 attempt。"""

        queued: list[Callable[[], None]] = []
        session_factory = _session_factory_for(workspace_service)
        svc = ChapterService(
            workspace_service=workspace_service,
            client=MockLLMClient(),
            model="claude-sonnet-4-6",
            session_factory=session_factory,
            vector_store_factory=_vector_store_factory_for(workspace_service),
            memory_enrichment_scheduler=queued.append,
        )
        svc.save_chapter_card(prepared_workspace, _make_card("ch-0001", 1))
        result = svc.write(prepared_workspace, "ch-0001")
        assert result.final_state == ChapterState.APPROVED
        assert len(queued) == 1

        with session_factory(prepared_workspace) as session:
            row = session.get(ChapterORM, "ch-0001")
            assert row is not None
            now = datetime.now(timezone.utc)
            attempt_id = "attempt-consumed-before-pending-gap"
            session.add(
                AttemptLedgerORM(
                    attempt_id=attempt_id,
                    workspace_id=prepared_workspace,
                    run_id="run-explicit-continue",
                    task_id="task-explicit-continue",
                    chapter_id="ch-0001",
                    attempt_kind="provider",
                    operation_kind="agent_call",
                    logical_call_id="logical-consumed-before-pending-gap",
                    agent_name="memory_extractor",
                    provider="mock",
                    model="mock-model",
                    attempt_number=1,
                    attempt_cap=3,
                    status="succeeded",
                    version_before=result.final_version_id,
                    version_after=None,
                    error_type=None,
                    created_at=now,
                    completed_at=now,
                )
            )
            details = dict(row.memory_enrichment_details)
            details["retry_history"] = [
                {
                    "trigger": "pipeline_explicit_continue",
                    "automatic_provider_replay": False,
                    "consumed_provider_attempt_ids": [attempt_id],
                }
            ]
            row.memory_enrichment_details = details
            session.commit()

        assert svc.resume_approved_checkpoint(prepared_workspace, "ch-0001")
        assert len(queued) == 2

    def test_ready_pending_rejects_malformed_memory_attempt_ledger(
        self,
        workspace_service: WorkspaceService,
        prepared_workspace: str,
    ) -> None:
        """富记忆 attempt 生命周期矛盾时硬停，不把坏账本洗成可重排。"""

        queued: list[Callable[[], None]] = []
        session_factory = _session_factory_for(workspace_service)
        svc = ChapterService(
            workspace_service=workspace_service,
            client=MockLLMClient(),
            model="claude-sonnet-4-6",
            session_factory=session_factory,
            vector_store_factory=_vector_store_factory_for(workspace_service),
            memory_enrichment_scheduler=queued.append,
        )
        svc.save_chapter_card(prepared_workspace, _make_card("ch-0001", 1))
        result = svc.write(prepared_workspace, "ch-0001")
        assert result.final_state == ChapterState.APPROVED
        now = datetime.now(timezone.utc)
        with session_factory(prepared_workspace) as session:
            session.add(
                AttemptLedgerORM(
                    attempt_id="attempt-memory-wrong-operation-kind",
                    workspace_id=prepared_workspace,
                    run_id=None,
                    task_id="task-memory-wrong-kind",
                    chapter_id="ch-0001",
                    attempt_kind="provider",
                    operation_kind="wrong-kind",
                    logical_call_id="logical-memory-wrong-kind",
                    agent_name="memory_extractor",
                    provider="mock",
                    model="mock",
                    attempt_number=1,
                    attempt_cap=3,
                    status="started",
                    version_before=result.final_version_id,
                    version_after=None,
                    error_type=None,
                    created_at=now,
                    completed_at=None,
                )
            )
            session.commit()

        with pytest.raises(
            ContinuityCheckpointCorruptionError,
            match="attempt 账本损坏",
        ):
            svc.resume_approved_checkpoint(prepared_workspace, "ch-0001")
        assert len(queued) == 1

    @pytest.mark.parametrize("attempt_status", ["started", "succeeded"])
    def test_resume_approved_checkpoint_retains_unconsumed_summary_response(
        self,
        workspace_service: WorkspaceService,
        prepared_workspace: str,
        monkeypatch: pytest.MonkeyPatch,
        attempt_status: str,
    ) -> None:
        """summary started/succeeded 但无 ledger 时保留现场，禁止重复请求。"""

        from src.services import chapter_service as chapter_service_module

        session_factory = _session_factory_for(workspace_service)
        svc = ChapterService(
            workspace_service=workspace_service,
            client=MockLLMClient(),
            model="claude-sonnet-4-6",
            session_factory=session_factory,
            memory_enrichment_scheduler=lambda _task: None,
        )
        svc.save_chapter_card(prepared_workspace, _make_card("ch-0001", 1))
        result = svc.write(prepared_workspace, "ch-0001")
        assert result.final_state == ChapterState.APPROVED

        with session_factory(prepared_workspace) as session:
            row = session.get(ChapterORM, "ch-0001")
            summary = session.get(ChapterSummaryORM, "ch-0001")
            assert row is not None
            assert summary is not None
            row.ready_for_next_chapter_at = None
            session.delete(summary)
            now = datetime.now(timezone.utc)
            session.add(
                AttemptLedgerORM(
                    attempt_id=f"attempt-summary-{attempt_status}",
                    workspace_id=prepared_workspace,
                    run_id=None,
                    task_id=f"task-summary-{attempt_status}",
                    chapter_id="ch-0001",
                    attempt_kind="provider",
                    operation_kind="checkpoint-resume",
                    logical_call_id=f"logical-summary-{attempt_status}",
                    agent_name="chapter_summary_extractor",
                    provider="mock",
                    model="mock-model",
                    attempt_number=1,
                    attempt_cap=3,
                    status=attempt_status,
                    version_before=result.final_version_id,
                    version_after=None,
                    error_type=None,
                    created_at=now,
                    completed_at=(
                        None if attempt_status == "started" else now
                    ),
                )
            )
            session.commit()

        extractor_called = False

        def _must_not_extract(**_kwargs):
            nonlocal extractor_called
            extractor_called = True
            raise AssertionError("存在未消费响应时不得再次抽取")

        monkeypatch.setattr(
            chapter_service_module,
            "extract_and_persist_continuity_ledger",
            _must_not_extract,
        )

        with pytest.raises(
            ContinuityCheckpointCorruptionError,
            match="结果未知，禁止重复请求",
        ):
            svc.resume_approved_checkpoint(prepared_workspace, "ch-0001")
        assert extractor_called is False

    def test_ready_checkpoint_allows_running_enrichment_without_replay(
        self,
        workspace_service: WorkspaceService,
        prepared_workspace: str,
        monkeypatch: pytest.MonkeyPatch,
    ) -> None:
        """ready 后合法 running 按双水位继续，且不能重排或调用编排器。"""
        from src.services import chapter_service as chapter_service_module

        queued: list[Callable[[], None]] = []
        session_factory = _session_factory_for(workspace_service)
        svc = ChapterService(
            workspace_service=workspace_service,
            client=MockLLMClient(),
            model="claude-sonnet-4-6",
            session_factory=session_factory,
            vector_store_factory=_vector_store_factory_for(workspace_service),
            memory_enrichment_scheduler=queued.append,
        )
        svc.save_chapter_card(prepared_workspace, _make_card("ch-0001", 1))
        result = svc.write(prepared_workspace, "ch-0001")
        assert result.final_state == ChapterState.APPROVED
        assert len(queued) == 1

        session = session_factory(prepared_workspace)
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

        monkeypatch.setattr(
            chapter_service_module,
            "orchestrate_chapter",
            lambda **_kwargs: (_ for _ in ()).throw(
                AssertionError("orphan enrichment preflight 后不得调用 orchestrator")
            ),
        )

        assert svc.resume_approved_checkpoint(prepared_workspace, "ch-0001")
        assert len(queued) == 1

    @pytest.mark.parametrize(
        (
            "memory_status",
            "memory_details",
            "has_enriched_at",
            "error_pattern",
        ),
        [
            (
                "running",
                {
                    "started_at": "2026-08-02T12:30:11+00:00",
                    "tasks": [],
                },
                False,
                "富记忆仍为 running",
            ),
            (
                "future_state",
                {"tasks": []},
                False,
                "富记忆 checkpoint 结构损坏",
            ),
            ("running", None, False, "富记忆 checkpoint 结构损坏"),
            (
                "pending",
                "broken-details",
                False,
                "富记忆 checkpoint 结构损坏",
            ),
            (
                "running",
                {
                    "started_at": "2026-08-02T12:30:11+00:00",
                    "tasks": {},
                },
                False,
                "富记忆 checkpoint 结构损坏",
            ),
            (
                "failed",
                {"tasks": [], "failures": {}},
                False,
                "富记忆 checkpoint 结构损坏",
            ),
            (
                "running",
                {"started_at": "not-a-time", "tasks": []},
                False,
                "富记忆 checkpoint 结构损坏",
            ),
            (
                "running",
                {
                    "started_at": "2026-08-02T12:30:11+00:00",
                    "tasks": [],
                },
                True,
                "富记忆 checkpoint 结构损坏",
            ),
            (
                "succeeded",
                {"tasks": []},
                False,
                "富记忆 checkpoint 结构损坏",
            ),
        ],
    )
    def test_not_ready_checkpoint_blocks_unsafe_memory_state_without_side_effects(
        self,
        workspace_service: WorkspaceService,
        prepared_workspace: str,
        monkeypatch: pytest.MonkeyPatch,
        memory_status: str,
        memory_details: object,
        has_enriched_at: bool,
        error_pattern: str,
    ) -> None:
        """ready 为空也先硬停，不补 ledger/ready、不排 Worker、不出网。"""
        from unittest.mock import MagicMock

        from src.services import chapter_service as chapter_service_module

        queued: list[Callable[[], None]] = []
        session_factory = _session_factory_for(workspace_service)
        client = MockLLMClient()
        svc = ChapterService(
            workspace_service=workspace_service,
            client=client,
            model="claude-sonnet-4-6",
            session_factory=session_factory,
            vector_store_factory=_vector_store_factory_for(workspace_service),
            memory_enrichment_scheduler=queued.append,
        )
        svc.save_chapter_card(prepared_workspace, _make_card("ch-0001", 1))
        result = svc.write(prepared_workspace, "ch-0001")
        assert result.final_state == ChapterState.APPROVED
        queued.clear()
        provider_calls_before = client._total_calls  # noqa: SLF001

        session = session_factory(prepared_workspace)
        try:
            row = session.get(ChapterORM, "ch-0001")
            assert row is not None
            row.ready_for_next_chapter_at = None
            row.memory_enrichment_status = memory_status
            row.memory_enrichment_details = deepcopy(memory_details)
            row.memory_enriched_at = (
                datetime.now(timezone.utc) if has_enriched_at else None
            )
            session.commit()
            before = {
                column.name: deepcopy(getattr(row, column.name))
                for column in ChapterORM.__table__.columns
            }
            version_count_before = len(
                list(
                    session.scalars(
                        select(ChapterVersionORM).where(
                            ChapterVersionORM.chapter_id == "ch-0001"
                        )
                    )
                )
            )
            review_count_before = len(
                list(
                    session.scalars(
                        select(ReviewResultORM).where(
                            ReviewResultORM.chapter_id == "ch-0001"
                        )
                    )
                )
            )
            attempt_count_before = len(
                list(
                    session.scalars(
                        select(AttemptLedgerORM).where(
                            AttemptLedgerORM.chapter_id == "ch-0001"
                        )
                    )
                )
            )
        finally:
            session.close()

        extractor = MagicMock()
        orchestrator = MagicMock()
        monkeypatch.setattr(
            chapter_service_module,
            "extract_and_persist_continuity_ledger",
            extractor,
        )
        monkeypatch.setattr(
            chapter_service_module,
            "orchestrate_chapter",
            orchestrator,
        )

        with pytest.raises(
            ContinuityCheckpointCorruptionError,
            match=error_pattern,
        ):
            svc.resume_approved_checkpoint(prepared_workspace, "ch-0001")

        extractor.assert_not_called()
        orchestrator.assert_not_called()
        assert queued == []
        assert client._total_calls == provider_calls_before  # noqa: SLF001
        session = session_factory(prepared_workspace)
        try:
            row = session.get(ChapterORM, "ch-0001")
            assert row is not None
            after = {
                column.name: deepcopy(getattr(row, column.name))
                for column in ChapterORM.__table__.columns
            }
            assert after == before
            assert (
                len(
                    list(
                        session.scalars(
                            select(ChapterVersionORM).where(
                                ChapterVersionORM.chapter_id == "ch-0001"
                            )
                        )
                    )
                )
                == version_count_before
            )
            assert (
                len(
                    list(
                        session.scalars(
                            select(ReviewResultORM).where(
                                ReviewResultORM.chapter_id == "ch-0001"
                            )
                        )
                    )
                )
                == review_count_before
            )
            assert (
                len(
                    list(
                        session.scalars(
                            select(AttemptLedgerORM).where(
                                AttemptLedgerORM.chapter_id == "ch-0001"
                            )
                        )
                    )
                )
                == attempt_count_before
            )
        finally:
            session.close()

    def test_resume_approved_checkpoint_rejects_final_version_drift_before_llm(
        self,
        workspace_service: WorkspaceService,
        prepared_workspace: str,
        monkeypatch: pytest.MonkeyPatch,
    ) -> None:
        """final.md != vN.md 时在 extractor/Provider 前硬拦截。"""
        from unittest.mock import MagicMock

        from src.services import chapter_service as chapter_service_module

        session_factory = _session_factory_for(workspace_service)
        svc = ChapterService(
            workspace_service=workspace_service,
            client=MockLLMClient(),
            model="claude-sonnet-4-6",
            session_factory=session_factory,
            memory_enrichment_scheduler=lambda _task: None,
        )
        svc.save_chapter_card(prepared_workspace, _make_card("ch-0001", 1))
        svc.write(prepared_workspace, "ch-0001")

        session = session_factory(prepared_workspace)
        try:
            row = session.get(ChapterORM, "ch-0001")
            assert row is not None
            row.ready_for_next_chapter_at = None
            session.commit()
        finally:
            session.close()
        paths = WorkspacePaths(
            prepared_workspace,
            workspaces_root=workspace_service.workspaces_root,
        )
        paths.chapter_version_file("ch-0001", "final.md").write_text(
            "被篡改的终稿", encoding="utf-8"
        )
        extractor = MagicMock()
        monkeypatch.setattr(
            chapter_service_module,
            "extract_and_persist_continuity_ledger",
            extractor,
        )

        with pytest.raises(ChapterServiceError, match="拒绝出网"):
            svc.resume_approved_checkpoint(prepared_workspace, "ch-0001")
        extractor.assert_not_called()
        assert (
            _chapter_row(workspace_service, "ch-0001").ready_for_next_chapter_at is None
        )

    def test_resume_approved_checkpoint_rejects_noncanonical_version_alias(
        self,
        workspace_service: WorkspaceService,
        prepared_workspace: str,
        monkeypatch: pytest.MonkeyPatch,
    ) -> None:
        """v01.md 不能在 approved 恢复中折叠成 v1 后继续 Provider。"""
        from unittest.mock import MagicMock

        from src.services import chapter_service as chapter_service_module

        session_factory = _session_factory_for(workspace_service)
        client = MockLLMClient()
        svc = ChapterService(
            workspace_service=workspace_service,
            client=client,
            model="claude-sonnet-4-6",
            session_factory=session_factory,
            memory_enrichment_scheduler=lambda _task: None,
        )
        svc.save_chapter_card(prepared_workspace, _make_card("ch-0001", 1))
        svc.write(prepared_workspace, "ch-0001")
        session = session_factory(prepared_workspace)
        try:
            row = session.get(ChapterORM, "ch-0001")
            assert row is not None
            row.ready_for_next_chapter_at = None
            session.commit()
            before = {
                column.name: deepcopy(getattr(row, column.name))
                for column in ChapterORM.__table__.columns
            }
            attempt_count_before = len(
                list(
                    session.scalars(
                        select(AttemptLedgerORM).where(
                            AttemptLedgerORM.chapter_id == "ch-0001"
                        )
                    )
                )
            )
        finally:
            session.close()
        paths = WorkspacePaths(
            prepared_workspace,
            workspaces_root=workspace_service.workspaces_root,
        )
        alias_path = paths.chapter_dir("ch-0001") / "v01.md"
        alias_path.write_text("损坏别名", encoding="utf-8")
        extractor = MagicMock()
        monkeypatch.setattr(
            chapter_service_module,
            "extract_and_persist_continuity_ledger",
            extractor,
        )
        provider_calls_before = client._total_calls  # noqa: SLF001

        with pytest.raises(
            ContinuityCheckpointCorruptionError,
            match="非规范版本文件名",
        ):
            svc.resume_approved_checkpoint(prepared_workspace, "ch-0001")

        extractor.assert_not_called()
        assert client._total_calls == provider_calls_before  # noqa: SLF001
        session = session_factory(prepared_workspace)
        try:
            row = session.get(ChapterORM, "ch-0001")
            assert row is not None
            after = {
                column.name: deepcopy(getattr(row, column.name))
                for column in ChapterORM.__table__.columns
            }
            assert after == before
            assert (
                len(
                    list(
                        session.scalars(
                            select(AttemptLedgerORM).where(
                                AttemptLedgerORM.chapter_id == "ch-0001"
                            )
                        )
                    )
                )
                == attempt_count_before
            )
        finally:
            session.close()
        assert alias_path.read_text(encoding="utf-8") == "损坏别名"

    def test_resume_published_without_ready_is_checkpoint_corruption(
        self,
        workspace_service: WorkspaceService,
        prepared_workspace: str,
    ) -> None:
        """published 永远不可恢复写作；ready 空必须专用硬停。"""
        session_factory = _session_factory_for(workspace_service)
        svc = ChapterService(
            workspace_service=workspace_service,
            client=MockLLMClient(),
            model="claude-sonnet-4-6",
            session_factory=session_factory,
            memory_enrichment_scheduler=lambda _task: None,
        )
        svc.save_chapter_card(prepared_workspace, _make_card("ch-0001", 1))
        svc.write(prepared_workspace, "ch-0001")
        session = session_factory(prepared_workspace)
        try:
            row = session.get(ChapterORM, "ch-0001")
            assert row is not None
            row.status = "published"
            row.ready_for_next_chapter_at = None
            session.commit()
        finally:
            session.close()

        with pytest.raises(
            ContinuityCheckpointCorruptionError,
            match="published.*缺少 ready_for_next_chapter",
        ):
            svc.resume_approved_checkpoint(prepared_workspace, "ch-0001")

    def test_resume_nonapproved_with_final_is_checkpoint_corruption(
        self,
        workspace_service: WorkspaceService,
        prepared_workspace: str,
    ) -> None:
        """DB 非 approved 但 final/approved_at 仍在时不可回退到 write。"""
        session_factory = _session_factory_for(workspace_service)
        svc = ChapterService(
            workspace_service=workspace_service,
            client=MockLLMClient(),
            model="claude-sonnet-4-6",
            session_factory=session_factory,
            memory_enrichment_scheduler=lambda _task: None,
        )
        svc.save_chapter_card(prepared_workspace, _make_card("ch-0001", 1))
        svc.write(prepared_workspace, "ch-0001")
        session = session_factory(prepared_workspace)
        try:
            row = session.get(ChapterORM, "ch-0001")
            assert row is not None
            row.status = "drafting"
            row.ready_for_next_chapter_at = None
            session.commit()
        finally:
            session.close()

        with pytest.raises(
            ContinuityCheckpointCorruptionError,
            match="状态不是 approved/published",
        ):
            svc.resume_approved_checkpoint(prepared_workspace, "ch-0001")

    def test_resume_missing_db_row_with_files_is_checkpoint_corruption(
        self,
        workspace_service: WorkspaceService,
        prepared_workspace: str,
    ) -> None:
        """正文文件仍在但 DB 章节行丢失时不可静默走全量 write。"""
        session_factory = _session_factory_for(workspace_service)
        svc = ChapterService(
            workspace_service=workspace_service,
            client=MockLLMClient(),
            model="claude-sonnet-4-6",
            session_factory=session_factory,
            memory_enrichment_scheduler=lambda _task: None,
        )
        svc.save_chapter_card(prepared_workspace, _make_card("ch-0001", 1))
        svc.write(prepared_workspace, "ch-0001")
        session = session_factory(prepared_workspace)
        try:
            row = session.get(ChapterORM, "ch-0001")
            assert row is not None
            session.delete(row)
            session.commit()
        finally:
            session.close()

        with pytest.raises(
            ContinuityCheckpointCorruptionError,
            match="DB 章节行缺失.*磁盘留有正文版本",
        ):
            svc.resume_approved_checkpoint(prepared_workspace, "ch-0001")

    def test_resume_force_pass_checkpoint_closes_ready_without_provider_replay(
        self,
        workspace_service: WorkspaceService,
        prepared_workspace: str,
    ) -> None:
        """force-pass 已批准现场只补同源账本/ready，不重放 Provider。"""
        session_factory = _session_factory_for(workspace_service)
        client = MockLLMClient()
        svc = ChapterService(
            workspace_service=workspace_service,
            client=client,
            model="claude-sonnet-4-6",
            session_factory=session_factory,
            memory_enrichment_scheduler=lambda _task: None,
        )
        svc.save_chapter_card(prepared_workspace, _make_card("ch-0001", 1))
        svc.write(prepared_workspace, "ch-0001")
        session = session_factory(prepared_workspace)
        try:
            row = session.get(ChapterORM, "ch-0001")
            assert row is not None
            assert row.approved_at is not None
            row.ready_for_next_chapter_at = None
            row.force_pass_at = row.approved_at
            row.force_pass_reason = "test-only"
            session.commit()
        finally:
            session.close()

        calls_before_resume = client._total_calls
        resumed = svc.resume_approved_checkpoint(prepared_workspace, "ch-0001")

        assert resumed is True
        assert client._total_calls == calls_before_resume
        row = _chapter_row(workspace_service, "ch-0001")
        assert row.ready_for_next_chapter_at is not None
        assert row.core_state_commit_status == "disabled"  # legacy v0 卡不回填

    @pytest.mark.parametrize("damage", ["missing_row", "drafting", "ready_null"])
    def test_require_ready_rejects_unproven_completed_checkpoint(
        self,
        workspace_service: WorkspaceService,
        prepared_workspace: str,
        damage: str,
    ) -> None:
        """completed 证明链缺 DB/terminal/ready 任一项都必须 corruption。"""
        session_factory = _session_factory_for(workspace_service)
        svc = ChapterService(
            workspace_service=workspace_service,
            client=MockLLMClient(),
            model="claude-sonnet-4-6",
            session_factory=session_factory,
            memory_enrichment_scheduler=lambda _task: None,
        )
        svc.save_chapter_card(prepared_workspace, _make_card("ch-0001", 1))
        svc.write(prepared_workspace, "ch-0001")
        session = session_factory(prepared_workspace)
        try:
            row = session.get(ChapterORM, "ch-0001")
            assert row is not None
            if damage == "missing_row":
                session.delete(row)
            elif damage == "drafting":
                row.status = "drafting"
            else:
                row.ready_for_next_chapter_at = None
            session.commit()
        finally:
            session.close()

        with pytest.raises(ContinuityCheckpointCorruptionError):
            svc.require_ready_for_next_chapter(prepared_workspace, "ch-0001")

    def test_resume_approved_checkpoint_rejects_matching_file_drift_from_db(
        self,
        workspace_service: WorkspaceService,
        prepared_workspace: str,
        monkeypatch: pytest.MonkeyPatch,
    ) -> None:
        """final/vN 同时被改成相同内容，也必须在 Provider 前由 DB 正文拦截。"""
        from unittest.mock import MagicMock

        from src.services import chapter_service as chapter_service_module

        session_factory = _session_factory_for(workspace_service)
        svc = ChapterService(
            workspace_service=workspace_service,
            client=MockLLMClient(),
            model="claude-sonnet-4-6",
            session_factory=session_factory,
            memory_enrichment_scheduler=lambda _task: None,
        )
        svc.save_chapter_card(prepared_workspace, _make_card("ch-0001", 1))
        result = svc.write(prepared_workspace, "ch-0001")

        session = session_factory(prepared_workspace)
        try:
            row = session.get(ChapterORM, "ch-0001")
            assert row is not None
            row.ready_for_next_chapter_at = None
            session.commit()
        finally:
            session.close()

        paths = WorkspacePaths(
            prepared_workspace,
            workspaces_root=workspace_service.workspaces_root,
        )
        corrupted = "final 与 vN 被同步篡改".encode()
        paths.chapter_version_file("ch-0001", "final.md").write_bytes(corrupted)
        paths.chapter_version_file(
            "ch-0001", f"v{result.final_version_id}.md"
        ).write_bytes(corrupted)
        extractor = MagicMock()
        monkeypatch.setattr(
            chapter_service_module,
            "extract_and_persist_continuity_ledger",
            extractor,
        )

        with pytest.raises(
            ChapterServiceError,
            match="DB immutable 正文不一致.*拒绝出网",
        ):
            svc.resume_approved_checkpoint(prepared_workspace, "ch-0001")
        extractor.assert_not_called()
        assert (
            _chapter_row(workspace_service, "ch-0001").ready_for_next_chapter_at is None
        )

    @pytest.mark.parametrize(
        ("failure_kind", "expected_type"),
        [
            ("cost_guard", CostGuardTriggered),
            ("provider_cap", ProviderCallCapExceeded),
            ("ledger_not_ready", ContinuityLedgerNotReadyError),
        ],
    )
    def test_resume_preserves_dedicated_checkpoint_stop_types(
        self,
        workspace_service: WorkspaceService,
        prepared_workspace: str,
        monkeypatch: pytest.MonkeyPatch,
        failure_kind: str,
        expected_type: type[Exception],
    ) -> None:
        """恢复专用硬停不得被本地 corruption 包装或普通 skip 吞掉。"""
        from src.services import chapter_service as chapter_service_module

        session_factory = _session_factory_for(workspace_service)
        svc = ChapterService(
            workspace_service=workspace_service,
            client=MockLLMClient(),
            model="claude-sonnet-4-6",
            session_factory=session_factory,
            memory_enrichment_scheduler=lambda _task: None,
        )
        svc.save_chapter_card(prepared_workspace, _make_card("ch-0001", 1))
        svc.write(prepared_workspace, "ch-0001")
        session = session_factory(prepared_workspace)
        try:
            row = session.get(ChapterORM, "ch-0001")
            assert row is not None
            row.ready_for_next_chapter_at = None
            if failure_kind != "cost_guard":
                # Provider/ledger 专用异常只有在同步小账本确实缺失时才会进入
                # extractor；已有有效 ledger 必须复用，不能为测试重复请求。
                summary = session.get(ChapterSummaryORM, "ch-0001")
                assert summary is not None
                session.delete(summary)
            session.commit()
        finally:
            session.close()

        if failure_kind == "cost_guard":
            failure: Exception = CostGuardTriggered(
                "test cost guard",
                details={"scope": "workspace_daily"},
            )

            def _fail_gate(*_args, **_kwargs) -> None:
                raise failure

            monkeypatch.setattr(svc, "_run_gates", _fail_gate)
        else:
            failure = (
                ProviderCallCapExceeded(
                    workspace_id=prepared_workspace,
                    used=20,
                    cap=20,
                )
                if failure_kind == "provider_cap"
                else ContinuityLedgerNotReadyError(
                    chapter_id="ch-0001",
                    reason="test ledger not ready",
                )
            )

            def _fail_extractor(**_kwargs):
                raise failure

            monkeypatch.setattr(
                chapter_service_module,
                "extract_and_persist_continuity_ledger",
                _fail_extractor,
            )

        with pytest.raises(expected_type):
            svc.resume_approved_checkpoint(prepared_workspace, "ch-0001")
        assert (
            _chapter_row(workspace_service, "ch-0001").ready_for_next_chapter_at is None
        )

    @pytest.mark.parametrize("card_damage", ["missing", "bad_json"])
    def test_pipeline_skip_cannot_bypass_broken_checkpoint_card(
        self,
        workspace_service: WorkspaceService,
        prepared_workspace: str,
        monkeypatch: pytest.MonkeyPatch,
        card_damage: str,
    ) -> None:
        """approved checkpoint 缺卡/坏卡时 skip 也必须暂停，下一章和 Provider 均零。"""
        from unittest.mock import MagicMock

        from src.services import chapter_service as chapter_service_module

        session_factory = _session_factory_for(workspace_service)
        chapter_service = ChapterService(
            workspace_service=workspace_service,
            client=MockLLMClient(),
            model="claude-sonnet-4-6",
            session_factory=session_factory,
            memory_enrichment_scheduler=lambda _task: None,
        )
        chapter_service.save_chapter_card(
            prepared_workspace, _make_card("ch-0001", 1)
        )
        chapter_service.write(prepared_workspace, "ch-0001")

        session = session_factory(prepared_workspace)
        try:
            row = session.get(ChapterORM, "ch-0001")
            assert row is not None
            row.ready_for_next_chapter_at = None
            session.merge(
                VolumeORM(
                    volume_id="v01",
                    workspace_id=prepared_workspace,
                    title="测试卷",
                    summary="checkpoint recovery",
                    chapter_count=2,
                    tension_template="heros_journey",
                    arcs_json=[],
                    tension_curve_json=[5, 5],
                    created_at=row.created_at,
                    last_updated_at=row.created_at,
                )
            )
            session.commit()
        finally:
            session.close()

        paths = WorkspacePaths(
            prepared_workspace,
            workspaces_root=workspace_service.workspaces_root,
        )
        card_path = paths.cards_dir / "ch-0001.json"
        if card_damage == "missing":
            card_path.unlink()
        else:
            card_path.write_text("{broken-json", encoding="utf-8")

        extractor = MagicMock()
        monkeypatch.setattr(
            chapter_service_module,
            "extract_and_persist_continuity_ledger",
            extractor,
        )

        class _WS:
            def __init__(self) -> None:
                self.events: list[dict] = []

            def publish(self, **event) -> None:
                self.events.append(event)

        ws = _WS()
        pipeline = PipelineService(
            chapter_service=chapter_service,
            workspace_service=workspace_service,
            session_factory=session_factory,
            ws_manager=ws,  # type: ignore[arg-type]
            watchdog_config=PipelineWatchdogConfig(chapter_timeout_seconds=0),
        )
        started = pipeline.start(
            prepared_workspace,
            scope="range",
            start_chapter_id="ch-0001",
            end_chapter_id="ch-0002",
            volume_id=None,
            profile_override=None,
            failure_policy="skip",
        )

        pipeline.execute(prepared_workspace, started.run_id)

        status = pipeline.status(prepared_workspace)
        assert status.state == "paused"
        assert status.pause_reason == "continuity_checkpoint_corruption"
        assert status.completed_chapter_ids == []
        assert status.skipped_chapter_ids == []
        assert [
            event["data"]["chapter_id"]
            for event in ws.events
            if event["event"] == "chapter_started"
        ] == ["ch-0001"]
        extractor.assert_not_called()
        session = session_factory(prepared_workspace)
        try:
            attempts = list(
                session.scalars(
                    select(AttemptLedgerORM).where(
                        AttemptLedgerORM.run_id == started.run_id,
                        AttemptLedgerORM.attempt_kind == "pipeline",
                    )
                )
            )
            assert len(attempts) == 1
            assert attempts[0].status == "failed"
            assert attempts[0].error_type == "ContinuityCheckpointCorruptionError"
        finally:
            session.close()

    def test_approved_marks_ready_before_memory_enrichment_runs(
        self,
        workspace_service: WorkspaceService,
        prepared_workspace: str,
    ) -> None:
        """approved 后先落 ready,富记忆任务只入队,尚未执行。"""
        queued: list[Callable[[], None]] = []
        svc = ChapterService(
            workspace_service=workspace_service,
            client=MockLLMClient(),
            model="claude-sonnet-4-6",
            session_factory=_session_factory_for(workspace_service),
            vector_store_factory=_vector_store_factory_for(workspace_service),
            memory_enrichment_scheduler=queued.append,
        )
        svc.save_chapter_card(prepared_workspace, _make_card("ch-0001", 1))

        result = svc.write(prepared_workspace, "ch-0001")

        assert result.final_state == ChapterState.APPROVED
        row = _chapter_row(workspace_service, "ch-0001")
        assert row.ready_for_next_chapter_at is not None
        assert row.memory_enrichment_status == "pending"
        assert row.memory_enriched_at is None
        assert len(queued) == 1
        svc.require_ready_for_next_chapter(prepared_workspace, "ch-0001")

        with pytest.raises(
            MemoryEnrichmentNotReadyError,
            match="status='pending'",
        ) as exc_info:
            svc.require_memory_enrichment_succeeded(
                prepared_workspace,
                "ch-0001",
            )
        assert exc_info.value.recovery_details == {
            "recovery_kind": "memory_pending_checkpoint",
            "recovery_ready": True,
            "recovery_block_reason": None,
            "failure_stages": [],
            "failure_types": [],
            "result_unknown": False,
        }

        queued[0]()
        svc.require_memory_enrichment_succeeded(
            prepared_workspace,
            "ch-0001",
        )

    def test_formal_run_attempt_executes_memory_enrichment_inline(
        self,
        workspace_service: WorkspaceService,
        prepared_workspace: str,
    ) -> None:
        """生产调度器即使配置为后台，正式 Run 也要等富记忆成功再返回。"""
        queued: list[Callable[[], None]] = []
        svc = ChapterService(
            workspace_service=workspace_service,
            client=MockLLMClient(),
            model="claude-sonnet-4-6",
            session_factory=_session_factory_for(workspace_service),
            vector_store_factory=_vector_store_factory_for(workspace_service),
            memory_enrichment_scheduler=queued.append,
        )
        svc.save_chapter_card(prepared_workspace, _make_card("ch-0001", 1))

        with bind_attempt_scope(
            workspace_id=prepared_workspace,
            run_id="run_memory_ordering_test",
            task_id="task_memory_ordering_test",
            chapter_id="ch-0001",
            provider_call_cap=100,
            synchronous_memory_enrichment=True,
        ):
            result = svc.write(prepared_workspace, "ch-0001")

        assert result.final_state == ChapterState.APPROVED
        assert queued == []
        row = _chapter_row(workspace_service, "ch-0001")
        assert row.memory_enrichment_status == "succeeded"
        assert row.memory_enriched_at is not None
        svc.require_memory_enrichment_succeeded(
            prepared_workspace,
            "ch-0001",
        )

    def test_formal_checkpoint_resume_requeues_pending_memory_inline(
        self,
        workspace_service: WorkspaceService,
        prepared_workspace: str,
    ) -> None:
        """ready 后调度丢失的 pending 在正式 Continue 内同步消费，不再撞回暂停。"""

        queued: list[Callable[[], None]] = []
        svc = ChapterService(
            workspace_service=workspace_service,
            client=MockLLMClient(),
            model="claude-sonnet-4-6",
            session_factory=_session_factory_for(workspace_service),
            vector_store_factory=_vector_store_factory_for(workspace_service),
            memory_enrichment_scheduler=queued.append,
        )
        svc.save_chapter_card(prepared_workspace, _make_card("ch-0001", 1))
        result = svc.write(prepared_workspace, "ch-0001")
        assert result.final_state == ChapterState.APPROVED
        assert len(queued) == 1
        queued.clear()  # 模拟进程死在 pending -> Worker acquire 之前。

        with bind_attempt_scope(
            workspace_id=prepared_workspace,
            run_id="run_pending_memory_resume",
            task_id="task_pending_memory_resume",
            chapter_id="ch-0001",
            provider_call_cap=100,
            synchronous_memory_enrichment=True,
        ):
            assert svc.resume_approved_checkpoint(
                prepared_workspace,
                "ch-0001",
            )

        assert queued == []
        row = _chapter_row(workspace_service, "ch-0001")
        assert row.memory_enrichment_status == "succeeded"
        assert row.memory_enriched_at is not None

    def test_manual_run_attached_attempt_keeps_memory_enrichment_background(
        self,
        workspace_service: WorkspaceService,
        prepared_workspace: str,
    ) -> None:
        """暂停 run 的手动章节修复有 run_id，但仍沿用后台富记忆。"""
        queued: list[Callable[[], None]] = []
        svc = ChapterService(
            workspace_service=workspace_service,
            client=MockLLMClient(),
            model="claude-sonnet-4-6",
            session_factory=_session_factory_for(workspace_service),
            vector_store_factory=_vector_store_factory_for(workspace_service),
            memory_enrichment_scheduler=queued.append,
        )
        svc.save_chapter_card(prepared_workspace, _make_card("ch-0001", 1))

        with bind_attempt_scope(
            workspace_id=prepared_workspace,
            run_id="run_manual_repair_test",
            task_id="task_manual_repair_test",
            chapter_id="ch-0001",
            provider_call_cap=100,
        ):
            result = svc.write(prepared_workspace, "ch-0001")

        assert result.final_state == ChapterState.APPROVED
        assert len(queued) == 1
        row = _chapter_row(workspace_service, "ch-0001")
        assert row.memory_enrichment_status == "pending"
        assert row.memory_enriched_at is None

    def test_write_and_enrichment_emit_stage_timing_logs(
        self,
        workspace_service: WorkspaceService,
        prepared_workspace: str,
    ) -> None:
        """E:章节主链路能区分 writer/reviewer/hard gates/ready/memory_enriched。"""
        queued: list[Callable[[], None]] = []
        svc = ChapterService(
            workspace_service=workspace_service,
            client=MockLLMClient(),
            model="claude-sonnet-4-6",
            session_factory=_session_factory_for(workspace_service),
            vector_store_factory=_vector_store_factory_for(workspace_service),
            memory_enrichment_scheduler=queued.append,
        )
        svc.save_chapter_card(prepared_workspace, _make_card("ch-0001", 1))

        with capture_logs() as logs:
            result = svc.write(prepared_workspace, "ch-0001")
            queued[0]()

        assert result.final_state == ChapterState.APPROVED
        timings = [
            log for log in logs if log.get("event") == "chapter_stage_timing"
        ]
        assert timings
        stages = {log["stage"] for log in timings}
        assert {
            "writer",
            "reviewer",
            "deterministic_validators",
            "post_review_gates",
            "continuity_ledger",
            "ready_for_next_chapter",
            "vector_store_factory",
            "memory_extractor",
            "chapter_summary_vector",
            "metrics_refresh",
            "memory_enriched",
        } <= stages
        for log in timings:
            assert log["workspace_id"] == prepared_workspace
            assert log["chapter_id"] == "ch-0001"
            assert log["status"] in {"succeeded", "failed", "skipped"}
            assert log["duration_ms"] >= 0

    def test_duplicate_memory_worker_cannot_repeat_provider_stages(
        self,
        workspace_service: WorkspaceService,
        prepared_workspace: str,
        monkeypatch: pytest.MonkeyPatch,
    ) -> None:
        """同一后台闭包被重复执行时，只有 pending->running 胜者能进富记忆。"""
        from src.services import chapter_service as chapter_service_module

        queued: list[Callable[[], None]] = []
        vector_factory_calls: list[str] = []
        extractor_calls: list[str] = []
        plot_check_calls: list[str] = []
        real_vector_factory = _vector_store_factory_for(workspace_service)

        def _vector_factory(workspace_id: str) -> VectorStore:
            vector_factory_calls.append(workspace_id)
            return real_vector_factory(workspace_id)

        def _extractor(**kwargs) -> bool:
            extractor_calls.append(kwargs["workspace_id"])
            return True

        def _plot_check(**kwargs) -> None:
            plot_check_calls.append(kwargs["workspace_id"])

        monkeypatch.setattr(chapter_service_module, "_extract_memories_safely", _extractor)
        monkeypatch.setattr(
            chapter_service_module,
            "_check_plot_thread_payoff_safely",
            _plot_check,
        )
        svc = ChapterService(
            workspace_service=workspace_service,
            client=MockLLMClient(),
            model="claude-sonnet-4-6",
            session_factory=_session_factory_for(workspace_service),
            vector_store_factory=_vector_factory,
            memory_enrichment_scheduler=queued.append,
        )
        svc.save_chapter_card(prepared_workspace, _make_card("ch-0001", 1))

        result = svc.write(prepared_workspace, "ch-0001")
        assert result.final_state == ChapterState.APPROVED
        assert len(queued) == 1
        vector_calls_before_enrichment = len(vector_factory_calls)

        queued[0]()
        queued[0]()

        # 唯一获胜 Worker 分别构造一次 baseline 审计 store 和一次实际富记忆
        # store；第二次执行同一闭包在 pending->running CAS 前后均不得再进入。
        assert len(vector_factory_calls) == vector_calls_before_enrichment + 2
        assert extractor_calls == [prepared_workspace]
        assert plot_check_calls == [prepared_workspace]
        row = _chapter_row(workspace_service, "ch-0001")
        assert row.memory_enrichment_status == "succeeded"
        assert row.memory_enrichment_details["final_version_id"] == (
            result.final_version_id
        )

    def test_running_memory_worker_blocks_rewrite_before_orchestrator(
        self,
        workspace_service: WorkspaceService,
        prepared_workspace: str,
        monkeypatch: pytest.MonkeyPatch,
    ) -> None:
        """Worker 先赢 pending->running 时，Rewrite 不得碰文件或 Provider。"""

        from src.services import chapter_service as chapter_service_module

        queued: list[Callable[[], None]] = []
        session_factory = _session_factory_for(workspace_service)
        svc = ChapterService(
            workspace_service=workspace_service,
            client=MockLLMClient(),
            model="claude-sonnet-4-6",
            session_factory=session_factory,
            vector_store_factory=_vector_store_factory_for(workspace_service),
            memory_enrichment_scheduler=queued.append,
        )
        svc.save_chapter_card(prepared_workspace, _make_card("ch-0001", 1))
        first = svc.write(prepared_workspace, "ch-0001")
        assert first.final_state == ChapterState.APPROVED

        session = session_factory(prepared_workspace)
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

        monkeypatch.setattr(
            chapter_service_module,
            "orchestrate_chapter",
            lambda **_kwargs: (_ for _ in ()).throw(
                AssertionError("running Worker 后不得进入 orchestrator")
            ),
        )
        with pytest.raises(
            ContinuityCheckpointCorruptionError,
            match="富记忆仍为 running",
        ):
            svc.rewrite(prepared_workspace, "ch-0001")

        paths = WorkspacePaths(
            prepared_workspace,
            workspaces_root=workspace_service.workspaces_root,
        )
        assert ChapterVersioning(paths).next_version_id("ch-0001") - 1 == (
            first.final_version_id
        )

    def test_rewrite_claim_makes_stale_memory_callback_zero_work(
        self,
        workspace_service: WorkspaceService,
        prepared_workspace: str,
    ) -> None:
        """Rewrite 先关闭旧 ready 后，旧 v1 callback 必须在 vector 前退出。"""

        queued: list[Callable[[], None]] = []
        vector_factory_calls: list[str] = []
        real_factory = _vector_store_factory_for(workspace_service)

        def _vector_factory(workspace_id: str) -> VectorStore:
            vector_factory_calls.append(workspace_id)
            return real_factory(workspace_id)

        svc = ChapterService(
            workspace_service=workspace_service,
            client=MockLLMClient(),
            model="claude-sonnet-4-6",
            session_factory=_session_factory_for(workspace_service),
            vector_store_factory=_vector_factory,
            memory_enrichment_scheduler=queued.append,
        )
        svc.save_chapter_card(prepared_workspace, _make_card("ch-0001", 1))
        first = svc.write(prepared_workspace, "ch-0001")
        assert first.final_state == ChapterState.APPROVED
        assert len(queued) == 1
        stale_callback = queued[0]

        second = svc.rewrite(prepared_workspace, "ch-0001")
        assert second.final_state == ChapterState.APPROVED
        assert len(queued) == 2
        calls_before_stale = len(vector_factory_calls)

        stale_callback()

        assert len(vector_factory_calls) == calls_before_stale
        row = _chapter_row(workspace_service, "ch-0001")
        assert row.current_version == second.final_version_id
        assert row.memory_enrichment_status == "pending"
        assert row.memory_enrichment_details["final_version_id"] == (
            second.final_version_id
        )

    @pytest.mark.parametrize("corruption", ["pending_provenance", "final_body"])
    def test_memory_worker_rejects_corrupt_source_before_vector_or_provider(
        self,
        workspace_service: WorkspaceService,
        prepared_workspace: str,
        corruption: str,
    ) -> None:
        """pending 来源或 DB/vN/final 快照不一致时，闭包保留现场并零工作。"""

        queued: list[Callable[[], None]] = []
        vector_factory_calls: list[str] = []
        real_factory = _vector_store_factory_for(workspace_service)

        def _vector_factory(workspace_id: str) -> VectorStore:
            vector_factory_calls.append(workspace_id)
            return real_factory(workspace_id)

        session_factory = _session_factory_for(workspace_service)
        svc = ChapterService(
            workspace_service=workspace_service,
            client=MockLLMClient(),
            model="claude-sonnet-4-6",
            session_factory=session_factory,
            vector_store_factory=_vector_factory,
            memory_enrichment_scheduler=queued.append,
        )
        svc.save_chapter_card(prepared_workspace, _make_card("ch-0001", 1))
        result = svc.write(prepared_workspace, "ch-0001")
        assert result.final_state == ChapterState.APPROVED
        assert len(queued) == 1

        paths = WorkspacePaths(
            prepared_workspace,
            workspaces_root=workspace_service.workspaces_root,
        )
        if corruption == "pending_provenance":
            session = session_factory(prepared_workspace)
            try:
                row = session.get(ChapterORM, "ch-0001")
                assert row is not None
                details = dict(row.memory_enrichment_details)
                details["continuity_ledger_hash"] = "0" * 64
                row.memory_enrichment_details = details
                session.commit()
            finally:
                session.close()
        else:
            paths.chapter_version_file("ch-0001", "final.md").write_text(
                "被并发改动的终稿",
                encoding="utf-8",
            )

        calls_before = len(vector_factory_calls)
        queued[0]()

        assert len(vector_factory_calls) == calls_before
        row = _chapter_row(workspace_service, "ch-0001")
        assert row.memory_enrichment_status == "pending"

    def test_stale_memory_lease_cannot_finish_after_aba_reacquire(
        self,
        workspace_service: WorkspaceService,
        prepared_workspace: str,
    ) -> None:
        """同版本 ABA 后旧 lease finish 必须 rowcount=0，不能覆盖新 Worker。"""

        queued: list[Callable[[], None]] = []
        session_factory = _session_factory_for(workspace_service)
        svc = ChapterService(
            workspace_service=workspace_service,
            client=MockLLMClient(),
            model="claude-sonnet-4-6",
            session_factory=session_factory,
            vector_store_factory=_vector_store_factory_for(workspace_service),
            memory_enrichment_scheduler=queued.append,
        )
        svc.save_chapter_card(prepared_workspace, _make_card("ch-0001", 1))
        result = svc.write(prepared_workspace, "ch-0001")
        assert result.final_version_id is not None
        paths = WorkspacePaths(
            prepared_workspace,
            workspaces_root=workspace_service.workspaces_root,
        )
        ledger_hash, source_hash = (
            svc._validated_memory_enrichment_source_checkpoint(  # noqa: SLF001
                workspace_id=prepared_workspace,
                chapter_id="ch-0001",
                final_version_id=result.final_version_id,
                paths=paths,
            )
        )
        lease_a = svc._acquire_memory_enrichment_worker(  # noqa: SLF001
            prepared_workspace,
            "ch-0001",
            final_version_id=result.final_version_id,
            started_at=datetime.now(timezone.utc),
            paths=paths,
            expected_ledger_hash=ledger_hash,
            expected_ledger_source_text_hash=source_hash,
        )
        assert lease_a is not None

        pending_details = {
            key: value
            for key, value in lease_a.running_details.items()
            if key not in {"started_at", "tasks", "lease_id"}
        }
        session = session_factory(prepared_workspace)
        try:
            row = session.get(ChapterORM, "ch-0001")
            assert row is not None
            row.memory_enrichment_status = "pending"
            row.memory_enrichment_details = pending_details
            session.commit()
        finally:
            session.close()

        lease_b = svc._acquire_memory_enrichment_worker(  # noqa: SLF001
            prepared_workspace,
            "ch-0001",
            final_version_id=result.final_version_id,
            started_at=datetime.now(timezone.utc),
            paths=paths,
            expected_ledger_hash=ledger_hash,
            expected_ledger_source_text_hash=source_hash,
        )
        assert lease_b is not None
        assert lease_b.running_details["lease_id"] != (
            lease_a.running_details["lease_id"]
        )

        assert not svc._finish_memory_enrichment_worker_safely(  # noqa: SLF001
            prepared_workspace,
            "ch-0001",
            lease=lease_a,
            status="succeeded",
            details={**lease_a.running_details, "finished_at": "stale"},
            enriched_at=datetime.now(timezone.utc),
        )
        row = _chapter_row(workspace_service, "ch-0001")
        assert row.memory_enrichment_status == "running"
        assert row.memory_enrichment_details == lease_b.running_details
        assert row.memory_enriched_at is None

    def test_next_chapter_does_not_wait_for_pending_memory_enrichment(
        self,
        workspace_service: WorkspaceService,
        prepared_workspace: str,
    ) -> None:
        """富记忆任务模拟慢任务未执行时,下一章仍能用上章 final 兜底推进。"""
        queued: list[Callable[[], None]] = []
        svc = ChapterService(
            workspace_service=workspace_service,
            client=MockLLMClient(),
            model="claude-sonnet-4-6",
            session_factory=_session_factory_for(workspace_service),
            vector_store_factory=_vector_store_factory_for(workspace_service),
            memory_enrichment_scheduler=queued.append,
        )
        svc.save_chapter_card(prepared_workspace, _make_card("ch-0001", 1))
        svc.save_chapter_card(prepared_workspace, _make_card("ch-0002", 2))

        first = svc.write(prepared_workspace, "ch-0001")
        _attach_current_ledger_provenance(
            service=svc,
            workspace_service=workspace_service,
            chapter_id="ch-0002",
            previous_chapter_id="ch-0001",
        )
        second = svc.write(prepared_workspace, "ch-0002")

        assert first.final_state == ChapterState.APPROVED
        assert second.final_state == ChapterState.APPROVED
        assert _chapter_row(workspace_service, "ch-0001").memory_enrichment_status == (
            "pending"
        )
        assert _chapter_row(workspace_service, "ch-0002").ready_for_next_chapter_at
        assert len(queued) == 2

    def test_next_chapter_does_not_misclassify_live_running_memory_as_orphan(
        self,
        workspace_service: WorkspaceService,
        prepared_workspace: str,
    ) -> None:
        """正常长跑允许活 daemon 的合法 running；不得重排前章富记忆。"""
        queued: list[Callable[[], None]] = []
        session_factory = _session_factory_for(workspace_service)
        svc = ChapterService(
            workspace_service=workspace_service,
            client=MockLLMClient(),
            model="claude-sonnet-4-6",
            session_factory=session_factory,
            vector_store_factory=_vector_store_factory_for(workspace_service),
            memory_enrichment_scheduler=queued.append,
        )
        svc.save_chapter_card(prepared_workspace, _make_card("ch-0001", 1))
        svc.save_chapter_card(prepared_workspace, _make_card("ch-0002", 2))

        first = svc.write(prepared_workspace, "ch-0001")
        assert first.final_state == ChapterState.APPROVED
        assert len(queued) == 1
        session = session_factory(prepared_workspace)
        try:
            row = session.get(ChapterORM, "ch-0001")
            assert row is not None
            row.memory_enrichment_status = "running"
            row.memory_enrichment_details = {
                "started_at": "2026-08-02T12:30:11+00:00",
                "tasks": [],
            }
            row.memory_enriched_at = None
            session.commit()
        finally:
            session.close()

        svc.require_ready_for_next_chapter(prepared_workspace, "ch-0001")
        _attach_current_ledger_provenance(
            service=svc,
            workspace_service=workspace_service,
            chapter_id="ch-0002",
            previous_chapter_id="ch-0001",
        )
        second = svc.write(prepared_workspace, "ch-0002")

        assert second.final_state == ChapterState.APPROVED
        assert len(queued) == 2
        assert _chapter_row(
            workspace_service, "ch-0001"
        ).memory_enrichment_status == "running"

    def test_memory_enrichment_failure_is_recorded_without_reverting_ready(
        self,
        workspace_service: WorkspaceService,
        prepared_workspace: str,
        monkeypatch: pytest.MonkeyPatch,
    ) -> None:
        """memory/summary/vector 类富加工失败 → 后台 failed,章节仍 approved+ready。"""
        queued: list[Callable[[], None]] = []
        svc = ChapterService(
            workspace_service=workspace_service,
            client=MockLLMClient(),
            model="claude-sonnet-4-6",
            session_factory=_session_factory_for(workspace_service),
            vector_store_factory=_vector_store_factory_for(workspace_service),
            memory_enrichment_scheduler=queued.append,
        )
        svc.save_chapter_card(prepared_workspace, _make_card("ch-0001", 1))

        monkeypatch.setattr(
            "src.services.chapter_service._extract_memories_safely",
            lambda **_kwargs: False,
        )
        monkeypatch.setattr(
            "src.services.chapter_service."
            "persist_continuity_ledger_vector_safely",
            lambda **_kwargs: False,
        )

        result = svc.write(prepared_workspace, "ch-0001")
        assert result.final_state == ChapterState.APPROVED
        queued[0]()

        row = _chapter_row(workspace_service, "ch-0001")
        assert row.ready_for_next_chapter_at is not None
        assert row.memory_enrichment_status == "failed"
        failures = row.memory_enrichment_details["failures"]
        assert {failure["stage"] for failure in failures} == {
            "memory_extractor",
            "chapter_summary_vector",
        }
        assert row.memory_enrichment_details["result_unknown"] is False
        assert row.memory_enrichment_details["manual_recovery_required"] is False
        assert row.memory_enrichment_details["safe_to_continue"] is True
        assert row.memory_enrichment_details["failure_disposition"] == (
            "known_failure_degraded"
        )

    @pytest.mark.parametrize("legacy_missing_core_hash", [False, True])
    def test_explicit_pipeline_continue_retries_only_known_failed_extractor(
        self,
        legacy_missing_core_hash: bool,
        workspace_service: WorkspaceService,
        prepared_workspace: str,
        monkeypatch: pytest.MonkeyPatch,
    ) -> None:
        """同一终稿的已知 extractor 失败只由正式 Continue 重试一次。"""

        from src.services import chapter_service as chapter_service_module

        queued: list[Callable[[], None]] = []
        session_factory = _session_factory_for(workspace_service)
        svc = ChapterService(
            workspace_service=workspace_service,
            client=MockLLMClient(),
            model="claude-sonnet-4-6",
            session_factory=session_factory,
            vector_store_factory=_vector_store_factory_for(workspace_service),
            memory_enrichment_scheduler=queued.append,
        )
        svc.save_chapter_card(prepared_workspace, _make_card("ch-0001", 1))
        result = svc.write(prepared_workspace, "ch-0001")
        assert result.final_state == ChapterState.APPROVED
        assert len(queued) == 1

        monkeypatch.setattr(
            chapter_service_module,
            "_extract_memories_safely",
            lambda **_kwargs: MemoryExtractionOutcome(
                succeeded=False,
                failure_stage="extract",
                vector_write_started=False,
                sqlite_rollback_succeeded=True,
                result_unknown=False,
            ),
        )
        queued[0]()
        failed = _chapter_row(workspace_service, "ch-0001")
        assert failed.memory_enrichment_status == "failed"

        with pytest.raises(MemoryEnrichmentNotReadyError) as not_ready:
            svc.require_memory_enrichment_succeeded(
                prepared_workspace,
                "ch-0001",
            )
        assert not_ready.value.recovery_details == {
            "recovery_kind": "memory_retry_safe",
            "recovery_ready": True,
            "recovery_block_reason": None,
            "failure_stages": ["memory_extractor"],
            "failure_types": ["MemoryExtractionFailed"],
            "result_unknown": False,
        }

        # 模拟真实 Provider transport 已成功、schema 消费已明确失败的账本。
        with session_factory(prepared_workspace) as session:
            now = datetime.now(timezone.utc)
            session.add(
                AttemptLedgerORM(
                    attempt_id="attempt-memory-known-schema-failure",
                    workspace_id=prepared_workspace,
                    run_id="run-memory-known-schema-failure",
                    task_id="task-memory-known-schema-failure",
                    chapter_id="ch-0001",
                    attempt_kind="provider",
                    operation_kind="agent_call",
                    logical_call_id="logical-memory-known-schema-failure",
                    agent_name="memory_extractor",
                    provider="mock",
                    model="mock-model",
                    attempt_number=1,
                    attempt_cap=3,
                    status="succeeded",
                    version_before=result.final_version_id,
                    version_after=None,
                    error_type=None,
                    created_at=now,
                    completed_at=now,
                )
            )
            version_ids_before = list(
                session.scalars(
                    select(ChapterVersionORM).where(
                        ChapterVersionORM.chapter_id == "ch-0001"
                    )
                )
            )
            version_ids_before = [row.version_id for row in version_ids_before]
            review_ids_before = list(
                session.scalars(
                    select(ReviewResultORM).where(
                        ReviewResultORM.chapter_id == "ch-0001"
                    )
                )
            )
            review_ids_before = [row.review_id for row in review_ids_before]
            session.commit()

        core_hash = "a" * 64
        monkeypatch.setattr(
            chapter_service_module.CoreStateService,
            "require_committed", lambda *args, **kwargs: core_hash,
        )
        with session_factory(prepared_workspace) as session:
            row = session.get(ChapterORM, "ch-0001")
            assert row is not None
            details = dict(row.memory_enrichment_details)
            row.core_state_commit_hash = core_hash
            if legacy_missing_core_hash:
                details.pop("core_state_commit_hash", None)
                details["retry_history"] = [{
                    "trigger": "pipeline_explicit_continue",
                    "automatic_provider_replay": False,
                    "run_id": "old-run", "task_id": "old-task",
                    "previous_sqlite_digest": details["side_effect_baseline"]["sqlite_digest"],
                    "consumed_provider_attempt_ids": [],
                }]
            else:
                details["core_state_commit_hash"] = core_hash
            row.memory_enrichment_details = details
            session.commit()

        if legacy_missing_core_hash:
            original_details = deepcopy(details)
            for corrupt in (
                {"core_state_commit_hash": "b" * 64},
                {"retry_history": []},
            ):
                with session_factory(prepared_workspace) as session:
                    row = session.get(ChapterORM, "ch-0001")
                    assert row is not None
                    row.memory_enrichment_details = {**original_details, **corrupt}
                    session.commit()
                with pytest.raises(ContinuityCheckpointCorruptionError):
                    svc.require_ready_for_next_chapter(prepared_workspace, "ch-0001")
            with session_factory(prepared_workspace) as session:
                row = session.get(ChapterORM, "ch-0001")
                assert row is not None
                row.memory_enrichment_details = original_details
                session.commit()
            # 来源核心提交本身失效时，缺字段兼容不能放行。
            with monkeypatch.context() as patch:
                def invalid_core(*args, **kwargs):
                    raise ValueError("核心提交来源已漂移")
                patch.setattr(chapter_service_module.CoreStateService, "require_committed", invalid_core)
                with pytest.raises(ContinuityCheckpointCorruptionError):
                    svc.require_ready_for_next_chapter(prepared_workspace, "ch-0001")

        retry_calls: list[str] = []

        def _successful_retry(**kwargs) -> MemoryExtractionOutcome:
            retry_calls.append(kwargs["chapter_card"].chapter_id)
            return MemoryExtractionOutcome(
                succeeded=True,
                failure_stage=None,
                vector_write_started=False,
                sqlite_rollback_succeeded=True,
                result_unknown=False,
            )

        monkeypatch.setattr(
            chapter_service_module,
            "_extract_memories_safely",
            _successful_retry,
        )
        monkeypatch.setattr(
            chapter_service_module,
            "_check_plot_thread_payoff_safely",
            lambda **_kwargs: None,
        )
        monkeypatch.setattr(
            chapter_service_module,
            "orchestrate_chapter",
            lambda **_kwargs: (_ for _ in ()).throw(
                AssertionError("富记忆恢复不得重放 Writer/Reviewer 编排器")
            ),
        )

        # 普通方法调用仍只读 checkpoint；没有正式 Pipeline scope 不得出网。
        assert svc.resume_approved_checkpoint(prepared_workspace, "ch-0001")
        assert retry_calls == []
        assert (
            _chapter_row(
                workspace_service, "ch-0001"
            ).memory_enrichment_status
            == "failed"
        )

        # 不能只验证 ChapterService 单点：真实 Continue 必须穿过
        # Pipeline 的 terminal 回填、选章、attempt scope 和 checkpoint
        # resumer，且仍然不得进入 Writer/Reviewer。
        session = session_factory(prepared_workspace)
        try:
            chapter = session.get(ChapterORM, "ch-0001")
            assert chapter is not None
            session.merge(
                VolumeORM(
                    volume_id="v01",
                    workspace_id=prepared_workspace,
                    title="测试卷",
                    summary="explicit memory retry integration",
                    chapter_count=1,
                    tension_template="heros_journey",
                    arcs_json=[],
                    tension_curve_json=[5],
                    created_at=chapter.created_at,
                    last_updated_at=chapter.created_at,
                )
            )
            session.commit()
        finally:
            session.close()
        pipeline = PipelineService(
            chapter_service=svc,
            workspace_service=workspace_service,
            session_factory=session_factory,
            watchdog_config=PipelineWatchdogConfig(chapter_timeout_seconds=0),
        )
        started = pipeline.start(
            prepared_workspace,
            scope="single",
            start_chapter_id="ch-0001",
            end_chapter_id=None,
            volume_id=None,
            profile_override=None,
        )
        pipeline.pause(prepared_workspace)
        session = session_factory(prepared_workspace)
        try:
            run = session.get(PipelineRunORM, started.run_id)
            assert run is not None
            run.current_chapter = "ch-0001"
            run.pause_reason = "memory_enrichment_failed"
            session.commit()
        finally:
            session.close()
        snapshot, transitioned = pipeline.resume_with_transition(
            prepared_workspace
        )
        assert transitioned is True
        assert snapshot.state == "running"
        pipeline.execute(
            prepared_workspace,
            started.run_id,
            explicit_resume=True,
        )

        assert retry_calls == ["ch-0001"]
        succeeded = _chapter_row(workspace_service, "ch-0001")
        assert succeeded.memory_enrichment_status == "succeeded"
        pipeline_status = pipeline.status(prepared_workspace)
        assert pipeline_status.state == "stopped"
        assert pipeline_status.pause_reason == "completed"
        assert pipeline_status.completed_chapter_ids == ["ch-0001"]
        history = succeeded.memory_enrichment_details["retry_history"]
        assert succeeded.memory_enrichment_details["core_state_commit_hash"] == core_hash
        assert len(history) == (2 if legacy_missing_core_hash else 1)
        assert history[-1]["trigger"] == "pipeline_explicit_continue"
        assert history[-1]["automatic_provider_replay"] is False
        assert history[-1]["consumed_provider_attempt_ids"] == [
            "attempt-memory-known-schema-failure"
        ]

        with session_factory(prepared_workspace) as session:
            versions_after = list(
                session.scalars(
                    select(ChapterVersionORM).where(
                        ChapterVersionORM.chapter_id == "ch-0001"
                    )
                )
            )
            reviews_after = list(
                session.scalars(
                    select(ReviewResultORM).where(
                        ReviewResultORM.chapter_id == "ch-0001"
                    )
                )
            )
        assert [row.version_id for row in versions_after] == version_ids_before
        assert [row.review_id for row in reviews_after] == review_ids_before

    @pytest.mark.parametrize(
        ("vector_write_started", "rollback_succeeded"),
        [(True, True), (False, False)],
    )
    def test_unknown_memory_side_effects_are_recorded_and_block_next_stage(
        self,
        workspace_service: WorkspaceService,
        prepared_workspace: str,
        monkeypatch: pytest.MonkeyPatch,
        vector_write_started: bool,
        rollback_succeeded: bool,
    ) -> None:
        """向量写已开始或 SQLite 回滚未确认时，保留现场并停止后续加工。"""

        queued: list[Callable[[], None]] = []
        svc = ChapterService(
            workspace_service=workspace_service,
            client=MockLLMClient(),
            model="claude-sonnet-4-6",
            session_factory=_session_factory_for(workspace_service),
            vector_store_factory=_vector_store_factory_for(workspace_service),
            memory_enrichment_scheduler=queued.append,
        )
        svc.save_chapter_card(prepared_workspace, _make_card("ch-0001", 1))
        monkeypatch.setattr(
            "src.services.chapter_service._extract_memories_safely",
            lambda **_kwargs: MemoryExtractionOutcome(
                succeeded=False,
                failure_stage="extract",
                vector_write_started=vector_write_started,
                sqlite_rollback_succeeded=rollback_succeeded,
                result_unknown=True,
            ),
        )
        monkeypatch.setattr(
            "src.services.chapter_service."
            "persist_continuity_ledger_vector_safely",
            lambda **_kwargs: (_ for _ in ()).throw(
                AssertionError("unknown 后不得继续写摘要向量")
            ),
        )

        svc.write(prepared_workspace, "ch-0001")
        queued[0]()

        row = _chapter_row(workspace_service, "ch-0001")
        details = row.memory_enrichment_details
        assert row.memory_enrichment_status == "failed"
        assert details["memory_extraction_outcome"] == {
            "succeeded": False,
            "failure_stage": "extract",
            "vector_write_started": vector_write_started,
            "sqlite_rollback_succeeded": rollback_succeeded,
            "result_unknown": True,
        }
        assert details["manual_recovery_required"] is True
        assert details["safe_to_continue"] is False
        assert details["failure_disposition"] == "blocked_unknown_side_effects"
        assert any(
            task.get("reason") == "prior_unknown_side_effects"
            for task in details["tasks"]
        )
        with pytest.raises(
            ContinuityCheckpointCorruptionError,
            match="未知 SQLite 副作用",
        ):
            svc.require_ready_for_next_chapter(prepared_workspace, "ch-0001")

    def test_pipeline_continue_pauses_without_replaying_startup_memory(
        self,
        workspace_service: WorkspaceService,
        prepared_workspace: str,
        monkeypatch: pytest.MonkeyPatch,
    ) -> None:
        """冷启动普通失败不盲重放；正式 Run 明确暂停而不是降级完成。"""

        queued: list[Callable[[], None]] = []
        session_factory = _session_factory_for(workspace_service)
        setup = ChapterService(
            workspace_service=workspace_service,
            client=MockLLMClient(),
            model="claude-sonnet-4-6",
            session_factory=session_factory,
            vector_store_factory=_vector_store_factory_for(workspace_service),
            memory_enrichment_scheduler=queued.append,
        )
        setup.save_chapter_card(prepared_workspace, _make_card("ch-0001", 1))
        setup.write(prepared_workspace, "ch-0001")
        assert len(queued) == 1

        session = session_factory(prepared_workspace)
        try:
            row = session.get(ChapterORM, "ch-0001")
            assert row is not None
            pending = dict(row.memory_enrichment_details)
            row.memory_enrichment_status = "failed"
            row.memory_enriched_at = None
            row.memory_enrichment_details = {
                **pending,
                "finished_at": datetime.now(timezone.utc).isoformat(),
                "tasks": [],
                "failures": [
                    {
                        "stage": "memory_enrichment_task",
                        "error_type": "EngineRestartedMidMemoryEnrichment",
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
                    "source_provenance_verified": True,
                    "source_provenance_error": None,
                    "sqlite_footprint": {
                        "character_first_appearance": 0,
                        "character_last_updated": 0,
                        "character_state_timeline": 0,
                        "plot_planted": 0,
                        "plot_last_updated": 0,
                        "timeline_entries": 0,
                        "scene_anchor": 0,
                    },
                    "sqlite_side_effect_count": 0,
                    "vector_count": 0,
                    "vector_quarantined_count": 0,
                    "vector_quarantine_succeeded": True,
                    "vector_quarantine_error": None,
                    "side_effects_verified_absent": True,
                    "safe_to_continue": True,
                    "quarantine_required": False,
                    "recovery_disposition": (
                        "safe_degraded_no_sqlite_or_vector_persist"
                    ),
                },
            }
            session.commit()
        finally:
            session.close()

        extractor_calls: list[str] = []

        def _must_not_replay(**kwargs) -> MemoryExtractionOutcome:
            extractor_calls.append(kwargs["chapter_card"].chapter_id)
            raise AssertionError("Continue 不得重放第三章富记忆 Provider 链")

        monkeypatch.setattr(
            "src.services.chapter_service._extract_memories_safely",
            _must_not_replay,
        )
        monkeypatch.setattr(
            "src.services.chapter_service._check_plot_thread_payoff_safely",
            lambda **_kwargs: None,
        )
        resumed = ChapterService(
            workspace_service=workspace_service,
            client=MockLLMClient(),
            model="claude-sonnet-4-6",
            session_factory=session_factory,
            vector_store_factory=_vector_store_factory_for(workspace_service),
            memory_enrichment_scheduler="inline",
        )
        pipeline = PipelineService(
            chapter_service=resumed,
            workspace_service=workspace_service,
            session_factory=session_factory,
            watchdog_config=PipelineWatchdogConfig(chapter_timeout_seconds=0),
        )
        started = pipeline.start(
            prepared_workspace,
            scope="single",
            start_chapter_id="ch-0001",
            end_chapter_id=None,
            volume_id=None,
            profile_override=None,
        )
        pipeline.pause(prepared_workspace)
        session = session_factory(prepared_workspace)
        try:
            run = session.get(PipelineRunORM, started.run_id)
            assert run is not None
            run.current_chapter = "ch-0001"
            session.commit()
        finally:
            session.close()
        snapshot, transitioned = pipeline.resume_with_transition(
            prepared_workspace
        )
        assert transitioned is True
        assert snapshot.state == "running"

        pipeline.execute(prepared_workspace, started.run_id)

        row = _chapter_row(workspace_service, "ch-0001")
        status = pipeline.status(prepared_workspace)
        assert extractor_calls == []
        assert row.memory_enrichment_status == "failed"
        assert row.memory_enrichment_details["startup_recovery"][
            "automatic_provider_replay"
        ] is False
        assert "retry_trigger" not in row.memory_enrichment_details
        assert status.state == "paused"
        assert status.pause_reason == "memory_enrichment_failed"
        assert status.completed_chapter_ids == []
        session = session_factory(prepared_workspace)
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

    def test_failed_memory_enrichment_uses_final_text_fallback_for_next_chapter(
        self,
        workspace_service: WorkspaceService,
        prepared_workspace: str,
        monkeypatch: pytest.MonkeyPatch,
    ) -> None:
        """前章富记忆 failed 时,下一章显式降级到 final.md + chapter card。"""
        queued: list[Callable[[], None]] = []
        svc = ChapterService(
            workspace_service=workspace_service,
            client=MockLLMClient(),
            model="claude-sonnet-4-6",
            session_factory=_session_factory_for(workspace_service),
            vector_store_factory=_vector_store_factory_for(workspace_service),
            memory_enrichment_scheduler=queued.append,
        )
        svc.save_chapter_card(prepared_workspace, _make_card("ch-0001", 1))
        svc.save_chapter_card(prepared_workspace, _make_card("ch-0002", 2))

        monkeypatch.setattr(
            "src.services.chapter_service._extract_memories_safely",
            lambda **_kwargs: False,
        )
        svc.write(prepared_workspace, "ch-0001")
        queued[0]()
        assert _chapter_row(workspace_service, "ch-0001").memory_enrichment_status == (
            "failed"
        )
        _attach_current_ledger_provenance(
            service=svc,
            workspace_service=workspace_service,
            chapter_id="ch-0002",
            previous_chapter_id="ch-0001",
        )

        with capture_logs() as logs:
            result = svc.write(prepared_workspace, "ch-0002")

        assert result.final_state == ChapterState.APPROVED
        degraded = [
            log
            for log in logs
            if log.get("event") == "continuity_refresh_degraded_memory_enrichment"
        ]
        assert degraded
        assert degraded[0]["fallback"] == "prev_final_md_and_chapter_card"

    def test_ready_watermark_failure_raises_before_returning_success(
        self,
        workspace_service: WorkspaceService,
        prepared_workspace: str,
        monkeypatch: pytest.MonkeyPatch,
    ) -> None:
        """硬水位失败必须抛错,不能把章节包装成可续跑。"""
        queued: list[Callable[[], None]] = []
        svc = ChapterService(
            workspace_service=workspace_service,
            client=MockLLMClient(),
            model="claude-sonnet-4-6",
            session_factory=_session_factory_for(workspace_service),
            vector_store_factory=_vector_store_factory_for(workspace_service),
            memory_enrichment_scheduler=queued.append,
        )
        svc.save_chapter_card(prepared_workspace, _make_card("ch-0001", 1))

        def _fake_orchestrate(**_kwargs):
            return OrchestratorResult(
                final_state=ChapterState.APPROVED,
                escalation_reason=None,
                versions=[],
                reviews=[],
                final_path=None,
                final_version_id=1,
                revise_count=0,
                rewrite_count=0,
            )

        monkeypatch.setattr(
            "src.services.chapter_service.orchestrate_chapter", _fake_orchestrate
        )

        with pytest.raises(ChapterServiceError, match="连续性小账本未就绪"):
            svc.write(prepared_workspace, "ch-0001")
        assert queued == []


def test_checkpoint_known_core_conflict_is_not_corruption(
    workspace_service, prepared_workspace, monkeypatch,
):
    from src.services.core_state_service import CoreStateConflictError, CoreStateService
    session_factory = _session_factory_for(workspace_service)
    svc = ChapterService(
        workspace_service=workspace_service, client=MockLLMClient(),
        model="claude-sonnet-4-6", session_factory=session_factory,
        vector_store_factory=_vector_store_factory_for(workspace_service),
        memory_enrichment_scheduler=lambda callback: None,
    )
    svc.save_chapter_card(prepared_workspace, _make_card("ch-0001", 1))
    result = svc.write(prepared_workspace, "ch-0001")
    assert result.final_state == ChapterState.APPROVED
    with session_factory(prepared_workspace) as session:
        row = session.get(ChapterORM, "ch-0001")
        row.ready_for_next_chapter_at = None
        session.commit()
    def fail_commit(*args, **kwargs):
        raise CoreStateConflictError(chapter_id="ch-0001", conflicts=["missing evidence"])
    monkeypatch.setattr(CoreStateService, "commit_approved_chapter", fail_commit)
    with pytest.raises(CoreStateConflictError, match="missing evidence"):
        svc.resume_approved_checkpoint(prepared_workspace, "ch-0001")
