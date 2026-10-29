"""R8 章节操作持久租约的直接回归测试。

本文件只使用本地临时 SQLite、MockLLMClient 与临时工作区，不创建后台
Worker，也不会连接 Provider。覆盖租约唯一性、Pipeline/UI 双向互斥、冷启动
与取消时的安全释放边界，以及 JSON ``null``/SQL ``NULL`` 的 CAS 兼容。
"""

from __future__ import annotations

from collections.abc import Callable, Iterator
from dataclasses import dataclass
from datetime import UTC, datetime, timedelta
from pathlib import Path
import threading

import pytest
from sqlalchemy import Engine, create_engine, text
from sqlalchemy.orm import Session

from src.api import task_registry
from src.api.routes.chapters import (
    _claim_chapter_worker_or_cleanup,
    _run_chapter_cancellable,
    _run_chapter_write_task,
)
from src.core.db import Base
from src.core.db.models import (
    AttemptLedgerORM,
    ChapterOperationLeaseORM,
    ChapterORM,
    ChapterSummaryORM,
    ChapterVersionORM,
    PipelineRunORM,
    ReviewResultORM,
    VolumeORM,
    WorkspaceORM,
)
from src.core.chapter_versioning import ChapterVersioningError
from src.core.llm.mock_client import MockLLMClient
from src.core.llm.cancel import LLMCancelledError
from src.core.models.chapter_card import ChapterCard, ContinuityContext
from src.core.models.continuity_ledger import (
    compute_continuity_ledger_hash,
    raw_final_tail,
    sha256_text,
)
from src.core.state_machine.states import ChapterState
from src.core.workspace.workspace_paths import WorkspacePaths
from src.services.chapter_operation_lease import (
    ChapterOperationLease,
    ChapterOperationLeaseConflict,
    ChapterOperationLeaseError,
    ChapterOperationLeaseStore,
)
from src.services.attempt_ledger import bind_attempt_scope
from src.services.chapter_rewind_service import (
    ChapterRewindService,
    ChapterRewindServiceError,
)
from src.services.chapter_service import (
    ChapterService,
    RewriteCheckpointRecoveryError,
    _ChapterBudget,
)
from src.services.pipeline_service import PipelineService
from src.services.workspace_service import WorkspaceService


_WORKSPACE_ID = "ws-r8-operation-lease"
_CHAPTER_1 = "ch-0001"
_CHAPTER_2 = "ch-0002"


class _NoopPipelineChapterService:
    """Pipeline start/resume 测试不进入章节生成循环。"""

    def resume_approved_checkpoint(self, *_args: object, **_kwargs: object) -> bool:
        return False

    def require_ready_for_next_chapter(self, *_args: object, **_kwargs: object) -> None:
        return None

    def write(self, *_args: object, **_kwargs: object) -> None:
        return None


class _ControllableVectorStore:
    """rewind crash-recovery 用本地向量假体。"""

    def __init__(self, *, fail: bool = False) -> None:
        self.fail = fail
        self.calls: list[tuple[str, str]] = []

    def soft_delete_by_chapter_id(
        self,
        collection: str,
        *,
        chapter_id: str,
    ) -> int:
        self.calls.append((collection, chapter_id))
        if self.fail:
            raise RuntimeError("simulated Chroma failure")
        return 1


@pytest.fixture
def usable_vector_store() -> _ControllableVectorStore:
    """CAS 回归使用的成功 Chroma 假体，保持 rewind 完整提交合同。"""

    return _ControllableVectorStore()


@dataclass(frozen=True)
class _Harness:
    workspace_service: WorkspaceService
    session_factory: Callable[[str], Session]
    engine: Engine
    store: ChapterOperationLeaseStore
    chapter_service: ChapterService
    pipeline_service: PipelineService

    @property
    def paths(self) -> WorkspacePaths:
        return WorkspacePaths(
            _WORKSPACE_ID,
            workspaces_root=self.workspace_service.workspaces_root,
        )


@pytest.fixture
def harness(tmp_path: Path) -> Iterator[_Harness]:
    workspaces_root = tmp_path / "workspaces"
    archives_root = tmp_path / "archives"
    workspaces_root.mkdir()
    archives_root.mkdir()
    workspace_service = WorkspaceService(
        workspaces_root=workspaces_root,
        archives_root=archives_root,
    )
    workspace_service.create(
        _WORKSPACE_ID,
        display_name="R8 operation lease test",
        genre="xianxia",
        premise="test",
        total_chapters=2,
        activate=False,
    )
    paths = WorkspacePaths(_WORKSPACE_ID, workspaces_root=workspaces_root)
    engine = create_engine(
        f"sqlite:///{paths.db_path}",
        connect_args={"check_same_thread": False, "timeout": 5},
    )
    Base.metadata.create_all(engine)

    def session_factory(_workspace_id: str) -> Session:
        return Session(engine)

    now = datetime.now(UTC)
    with Session(engine) as session:
        session.merge(
            WorkspaceORM(
                workspace_id=_WORKSPACE_ID,
                display_name="R8 operation lease test",
                genre="xianxia",
                premise="test",
                created_at=now,
                last_active_at=now,
                engine_version="0.1.0",
                status="active",
                total_chapters=2,
                completed_chapters=0,
                config_overrides={},
            )
        )
        session.merge(
            VolumeORM(
                volume_id="vol-1",
                workspace_id=_WORKSPACE_ID,
                title="第一卷",
                summary="test",
                chapter_count=2,
                tension_template="heros_journey",
                arcs_json=[],
                tension_curve_json=[],
                created_at=now,
                last_updated_at=now,
            )
        )
        session.commit()

    store = ChapterOperationLeaseStore(session_factory)
    chapter_service = ChapterService(
        workspace_service=workspace_service,
        client=MockLLMClient(),
        model="mock-model",
        session_factory=session_factory,
        operation_lease_store=store,
    )
    pipeline_service = PipelineService(
        chapter_service=_NoopPipelineChapterService(),  # type: ignore[arg-type]
        workspace_service=workspace_service,
        session_factory=session_factory,
    )
    yield _Harness(
        workspace_service=workspace_service,
        session_factory=session_factory,
        engine=engine,
        store=store,
        chapter_service=chapter_service,
        pipeline_service=pipeline_service,
    )
    engine.dispose()


def _acquire(
    harness: _Harness,
    *,
    chapter_id: str = _CHAPTER_1,
    task_id: str = "task-1",
    run_id: str | None = None,
    source_version: int = 0,
    operation_kind: str = "rewrite",
) -> ChapterOperationLease:
    return harness.store.acquire(
        workspace_id=_WORKSPACE_ID,
        chapter_id=chapter_id,
        operation_kind=operation_kind,
        source_version=source_version,
        task_id=task_id,
        run_id=run_id,
    )


def _mark_phase(
    harness: _Harness,
    lease: ChapterOperationLease,
    phase: str,
) -> ChapterOperationLease:
    return harness.store.mark_phase(lease, phase=phase)


def _insert_attempt(
    harness: _Harness,
    lease: ChapterOperationLease,
    *,
    status: str,
    agent_name: str = "writer",
    created_at: datetime | None = None,
    attempt_id: str | None = None,
    task_id: str | None = None,
    error_type: str | None = None,
    operation_kind: str | None = None,
) -> None:
    now = created_at or datetime.now(UTC)
    resolved_task_id = task_id or lease.task_id
    with harness.session_factory(_WORKSPACE_ID) as session:
        session.add(
            AttemptLedgerORM(
                attempt_id=(
                    attempt_id or f"attempt-{resolved_task_id}-{agent_name}-{status}"
                ),
                workspace_id=_WORKSPACE_ID,
                run_id=lease.run_id,
                task_id=resolved_task_id,
                chapter_id=lease.chapter_id,
                attempt_kind="provider",
                operation_kind=operation_kind or lease.operation_kind,
                logical_call_id=f"logical-{resolved_task_id}",
                agent_name=agent_name,
                provider="mock",
                model="mock-model",
                attempt_number=1,
                attempt_cap=3,
                status=status,
                version_before=lease.source_version,
                version_after=None,
                error_type=(
                    error_type
                    if status == "failed"
                    else None
                )
                or ("KnownFailure" if status == "failed" else None),
                created_at=now,
                completed_at=None if status == "started" else now,
            )
        )
        session.commit()


def _insert_review(
    harness: _Harness,
    *,
    version_id: int,
    reviewed_at: datetime,
    review_id: str,
) -> None:
    """写入一条可被 Rewrite checkpoint 严格恢复的完整 PASS 审稿。"""

    with harness.session_factory(_WORKSPACE_ID) as session:
        session.add(
            ReviewResultORM(
                review_id=review_id,
                chapter_id=_CHAPTER_1,
                version_reviewed=version_id,
                reviewed_at=reviewed_at,
                reviewer_model="mock-model",
                score_plot_progression=9.0,
                score_tension_match=9.0,
                score_prose_quality=9.0,
                score_consistency=9.0,
                score_hook_management=9.0,
                score_voice_consistency=9.0,
                score_overall=9.0,
                hard_constraints_passed=True,
                failed_constraints=[],
                constraint_violations=[],
                constraint_results=[],
                hard_constraint_regressions=[],
                suggestions=[],
                verdict="pass",
            )
        )
        session.commit()


def _start_pipeline(harness: _Harness, *, cap: int = 17):
    return harness.pipeline_service.start(
        _WORKSPACE_ID,
        scope="volume",
        start_chapter_id=None,
        end_chapter_id=None,
        volume_id="vol-1",
        profile_override=None,
        provider_call_cap=cap,
    )


def _set_run_checkpoint(
    harness: _Harness,
    run_id: str,
    *,
    state: str,
    current_chapter: str | None,
) -> None:
    with harness.session_factory(_WORKSPACE_ID) as session:
        run = session.get(PipelineRunORM, run_id)
        assert run is not None
        run.state = state
        run.current_chapter = current_chapter
        run.updated_at = datetime.now(UTC)
        session.commit()


def _reserve_ui(
    harness: _Harness,
    monkeypatch: pytest.MonkeyPatch,
    *,
    chapter_id: str,
    task_id: str,
) -> str | None:
    monkeypatch.setattr(
        harness.chapter_service,
        "_run_gates",
        lambda *_args, **_kwargs: None,
    )
    return harness.chapter_service.reserve_operation_task(
        _WORKSPACE_ID,
        chapter_id,
        operation="rewrite",
        task_id=task_id,
    )


def _seed_chapter(
    harness: _Harness,
    *,
    chapter_id: str = _CHAPTER_1,
    status: str = "reviewing",
    current_version: int = 1,
    memory_details_storage: str = "json_null",
) -> None:
    now = datetime.now(UTC)
    with harness.session_factory(_WORKSPACE_ID) as session:
        session.add(
            ChapterORM(
                chapter_id=chapter_id,
                workspace_id=_WORKSPACE_ID,
                current_version=current_version,
                status=status,
                rewrite_count=0,
                revise_count=0,
                created_at=now,
                approved_at=now if status == "approved" else None,
                total_token_usage={},
                total_cost_usd=0.0,
                ready_for_next_chapter_at=None,
                memory_enrichment_status="pending",
                memory_enriched_at=None,
                memory_enrichment_details=None,
            )
        )
        if current_version > 0:
            session.add(
                ChapterVersionORM(
                    chapter_id=chapter_id,
                    version_id=current_version,
                    content="正文",
                    word_count=2,
                    created_at=now,
                    creation_type="initial",
                    model_used="mock-model",
                    review_score=None,
                    notes="",
                    style_fingerprint=None,
                )
            )
        session.commit()
    if memory_details_storage == "sql_null":
        with harness.engine.begin() as connection:
            connection.execute(
                text(
                    "UPDATE chapters SET memory_enrichment_details = NULL "
                    "WHERE chapter_id = :chapter_id"
                ),
                {"chapter_id": chapter_id},
            )


def _write_version_file(
    harness: _Harness,
    *,
    version_id: int,
    content: str,
) -> Path:
    chapter_dir = harness.paths.chapter_dir(_CHAPTER_1)
    chapter_dir.mkdir(parents=True, exist_ok=True)
    path = harness.paths.chapter_version_file(
        _CHAPTER_1,
        f"v{version_id}.md",
    )
    path.write_text(content, encoding="utf-8")
    return path


def _seed_switch_version_checkpoint(harness: _Harness) -> None:
    """构造 current=v2、DB/盘 v1-v2 完整且尚未 ready 的切换现场。"""

    _seed_chapter(
        harness,
        status="human_intervention",
        current_version=1,
    )
    now = datetime.now(UTC)
    with harness.session_factory(_WORKSPACE_ID) as session:
        row = session.get(ChapterORM, _CHAPTER_1)
        assert row is not None
        row.current_version = 2
        session.add(
            ChapterVersionORM(
                chapter_id=_CHAPTER_1,
                version_id=2,
                content="第二版正文",
                word_count=5,
                created_at=now,
                creation_type="rewrite",
                model_used="mock-model",
                review_score=None,
                notes="",
                style_fingerprint=None,
            )
        )
        session.commit()
    _write_version_file(harness, version_id=1, content="正文")
    _write_version_file(harness, version_id=2, content="第二版正文")


def _seed_provider_checkpoint(harness: _Harness) -> None:
    """构造 DB/盘完全一致的单版本 Reviewer/Reviser 恢复基线。"""

    _seed_chapter(harness, status="reviewing", current_version=1)
    _write_version_file(harness, version_id=1, content="正文")


def _seed_cancelled_post_review_checkpoint(
    harness: _Harness,
) -> ChapterOperationLease:
    """构造 R9 v6 的精确断点：v2 已落、Reviewer 成功、gate 中被用户取消。"""

    _seed_provider_checkpoint(harness)
    run = _start_pipeline(harness)
    _set_run_checkpoint(
        harness,
        run.run_id,
        state="paused",
        current_chapter=_CHAPTER_1,
    )
    lease = _mark_phase(
        harness,
        _acquire(
            harness,
            source_version=1,
            operation_kind="write",
            task_id="task-cancelled-post-review",
        ),
        "provider_capable_pipeline",
    )
    writer_at = lease.created_at + timedelta(seconds=1)
    _insert_attempt(
        harness,
        lease,
        status="succeeded",
        agent_name="writer",
        created_at=writer_at,
        operation_kind="agent_call",
    )
    version_at = lease.created_at + timedelta(seconds=2)
    with harness.session_factory(_WORKSPACE_ID) as session:
        chapter = session.get(ChapterORM, _CHAPTER_1)
        assert chapter is not None
        chapter.current_version = 2
        chapter.status = "reviewing"
        chapter.rewrite_count = 1
        chapter.approved_at = None
        chapter.escalation_details = {
            "reason": "rewind_requires_content_rewrite",
            "source_version_id": 1,
            "source_status": "approved",
        }
        session.add(
            ChapterVersionORM(
                chapter_id=_CHAPTER_1,
                version_id=2,
                content="第二版正文",
                word_count=5,
                created_at=version_at,
                creation_type="rewrite",
                model_used="mock-model",
                review_score=None,
                notes="",
                style_fingerprint=None,
            )
        )
        session.commit()
    _write_version_file(harness, version_id=2, content="第二版正文")
    harness.paths.chapter_version_file(_CHAPTER_1, "final.md").write_text(
        "正文",
        encoding="utf-8",
    )
    _insert_attempt(
        harness,
        lease,
        status="succeeded",
        agent_name="reviewer",
        created_at=lease.created_at + timedelta(seconds=3),
        operation_kind="agent_call",
    )
    _insert_attempt(
        harness,
        lease,
        status="succeeded",
        agent_name="cross_chapter_dedup_validator",
        created_at=lease.created_at + timedelta(seconds=4),
        operation_kind="agent_call",
    )
    _insert_attempt(
        harness,
        lease,
        status="failed",
        agent_name="must_achieve_validator",
        created_at=lease.created_at + timedelta(seconds=5),
        error_type="LLMCancelledError",
        operation_kind="agent_call",
    )
    return lease


def _materialize_revise_version(
    harness: _Harness,
    lease: ChapterOperationLease,
    *,
    disk_mode: str,
) -> None:
    """写入 v(source+1) DB，并按场景省略或破坏对应磁盘正文。"""

    created_at = lease.created_at + timedelta(seconds=2)
    with harness.session_factory(_WORKSPACE_ID) as session:
        row = session.get(ChapterORM, _CHAPTER_1)
        assert row is not None
        row.current_version = 2
        session.add(
            ChapterVersionORM(
                chapter_id=_CHAPTER_1,
                version_id=2,
                content="修订后的正文",
                word_count=6,
                created_at=created_at,
                creation_type="revise",
                model_used="mock-model",
                review_score=None,
                notes="",
                style_fingerprint=None,
            )
        )
        session.commit()
    if disk_mode == "mismatch":
        _write_version_file(harness, version_id=2, content="磁盘冲突正文")
    elif disk_mode == "complete":
        _write_version_file(harness, version_id=2, content="修订后的正文")
    else:
        assert disk_mode == "db_only"


def _seed_body_checkpoint_without_ledger(harness: _Harness) -> None:
    """构造 DB/v1/final 一致但尚无 continuity ledger 的 approved 水位。"""

    _seed_chapter(harness, status="approved", current_version=1)
    chapter_dir = harness.paths.chapter_dir(_CHAPTER_1)
    chapter_dir.mkdir(parents=True, exist_ok=True)
    harness.paths.chapter_version_file(_CHAPTER_1, "v1.md").write_text(
        "正文",
        encoding="utf-8",
    )
    harness.paths.chapter_version_file(_CHAPTER_1, "final.md").write_text(
        "正文",
        encoding="utf-8",
    )


def _seed_exact_ready_checkpoint(harness: _Harness) -> None:
    """构造 final/v1/SQLite/card/hash 全部吻合的 approved+ready 水位。"""

    content = "山门风起，林衍收剑回望。"
    now = datetime.now(UTC)
    source_hash = sha256_text(content)
    final_tail = raw_final_tail(content)
    ledger_hash = compute_continuity_ledger_hash(
        chapter_id=_CHAPTER_1,
        workspace_id=_WORKSPACE_ID,
        summary_text="林衍在山门收剑。",
        pov_character=None,
        scene_setting=None,
        key_events=[],
        character_interactions=[],
        unfinished_actions=[],
        character_states=[],
        established_facts=[],
        revealed_mechanics=[],
        raw_tail=final_tail,
        source_version_id=1,
        source_text_hash=source_hash,
        evidence_records=[],
    )
    card = ChapterCard(
        chapter_id=_CHAPTER_1,
        workspace_id=_WORKSPACE_ID,
        volume_id="vol-1",
        chapter_number=1,
        title="第一章",
        tension_level=5,
        word_count_min=1,
        word_count_max=4000,
        plot_node_budget=1,
        must_achieve=[],
        must_unresolved=[],
        forbidden=[],
        pov_character="林衍",
        setting="山门",
        continuity_context=ContinuityContext(
            prev_chapter_tail="",
            unfinished_actions=[],
            time_gap="开篇",
            scene_continuation="switch",
            pov_change=False,
            pov_from=None,
            pov_to="林衍",
        ),
        next_chapter_seed="待续",
        arc_phase="opening",
        is_arc_finale=False,
        related_plot_threads=[],
        related_characters=["林衍"],
        review_threshold=7.5,
        revise_threshold=6.5,
        max_rewrites=3,
        max_revises=2,
    )
    harness.paths.cards_dir.mkdir(parents=True, exist_ok=True)
    (harness.paths.cards_dir / f"{_CHAPTER_1}.json").write_text(
        card.model_dump_json(indent=2),
        encoding="utf-8",
    )
    chapter_dir = harness.paths.chapter_dir(_CHAPTER_1)
    chapter_dir.mkdir(parents=True, exist_ok=True)
    harness.paths.chapter_version_file(_CHAPTER_1, "v1.md").write_text(
        content,
        encoding="utf-8",
    )
    harness.paths.chapter_version_file(_CHAPTER_1, "final.md").write_text(
        content,
        encoding="utf-8",
    )
    with harness.session_factory(_WORKSPACE_ID) as session:
        session.add(
            ChapterORM(
                chapter_id=_CHAPTER_1,
                workspace_id=_WORKSPACE_ID,
                current_version=1,
                status="approved",
                rewrite_count=0,
                revise_count=0,
                created_at=now,
                approved_at=now,
                total_token_usage={},
                total_cost_usd=0.0,
                ready_for_next_chapter_at=now,
                memory_enrichment_status="pending",
                memory_enriched_at=None,
                memory_enrichment_details={
                    "schema_version": 1,
                    "ready_at": now.isoformat(),
                    "final_version_id": 1,
                    "final_path": str(
                        harness.paths.chapter_version_file(_CHAPTER_1, "final.md")
                    ),
                    "continuity_ledger_hash": ledger_hash,
                    "continuity_ledger_source_text_hash": source_hash,
                },
            )
        )
        session.add(
            ChapterVersionORM(
                chapter_id=_CHAPTER_1,
                version_id=1,
                content=content,
                word_count=len(content),
                created_at=now,
                creation_type="initial",
                model_used="mock-model",
                review_score=9.0,
                notes="",
                style_fingerprint=None,
            )
        )
        session.add(
            ChapterSummaryORM(
                chapter_id=_CHAPTER_1,
                workspace_id=_WORKSPACE_ID,
                summary_text="林衍在山门收剑。",
                pov_character=None,
                scene_setting=None,
                key_events=[],
                character_interactions=[],
                unfinished_actions=[],
                character_states=[],
                established_facts=[],
                revealed_mechanics=[],
                raw_final_tail=final_tail,
                source_version_id=1,
                source_text_hash=source_hash,
                evidence_records=[],
                ledger_hash=ledger_hash,
                validated_at=now,
                extraction_trigger="reviewer_pass",
                model_used="mock-model",
                created_at=now,
                updated_at=now,
            )
        )
        session.commit()


class TestLeaseUniqueness:
    def test_same_chapter_second_acquire_conflicts(self, harness: _Harness) -> None:
        first = _acquire(harness, task_id="task-first")

        with pytest.raises(ChapterOperationLeaseConflict) as exc_info:
            _acquire(harness, task_id="task-second")

        assert exc_info.value.existing.lease_id == first.lease_id
        assert harness.store.get(_WORKSPACE_ID, _CHAPTER_1) == first

    def test_stale_release_cannot_delete_replacement_lease(
        self, harness: _Harness
    ) -> None:
        stale = _acquire(harness, task_id="task-stale")
        harness.store.release(stale)
        replacement = _acquire(harness, task_id="task-new")

        with pytest.raises(ChapterOperationLeaseError, match="释放失败"):
            harness.store.release(stale)

        assert harness.store.get(_WORKSPACE_ID, _CHAPTER_1) == replacement

    def test_stale_source_snapshot_is_rejected_before_lease_insert(
        self,
        harness: _Harness,
    ) -> None:
        """B 读 v1 后 A 提交 v2；B 不得留 source=v1 的假锁。"""

        _seed_chapter(harness, current_version=1)
        stale_source = 1
        now = datetime.now(UTC)
        with harness.session_factory(_WORKSPACE_ID) as session:
            session.add(
                ChapterVersionORM(
                    chapter_id=_CHAPTER_1,
                    version_id=2,
                    content="新正文",
                    word_count=3,
                    created_at=now,
                    creation_type="rewrite",
                    model_used="mock-model",
                    review_score=None,
                    notes="",
                    style_fingerprint=None,
                )
            )
            row = session.get(ChapterORM, _CHAPTER_1)
            assert row is not None
            row.current_version = 2
            row.rewrite_count = 1
            session.commit()

        with pytest.raises(ChapterOperationLeaseError, match="source_version 已过期"):
            _acquire(
                harness,
                task_id="task-stale-source",
                source_version=stale_source,
            )
        assert harness.store.get(_WORKSPACE_ID, _CHAPTER_1) is None


class TestSingleUseWorkerClaim:
    def test_same_task_second_service_entry_cannot_reuse_advanced_lease(
        self,
        harness: _Harness,
    ) -> None:
        """同 task 只能把 reserved 租约交给一个 service Worker。"""

        _seed_chapter(harness, current_version=1)
        with bind_attempt_scope(
            workspace_id=_WORKSPACE_ID,
            task_id="task-same-callback",
            chapter_id=_CHAPTER_1,
        ):
            lease = harness.chapter_service._acquire_chapter_operation_state(  # noqa: SLF001
                _WORKSPACE_ID,
                _CHAPTER_1,
                expected=_ChapterBudget(1, 0, 0),
                target_state="reviewing",
                operation="write",
            )
            assert lease is not None
            assert lease.phase == "chapter_state_claiming"
            with pytest.raises(RewriteCheckpointRecoveryError):
                harness.chapter_service._acquire_chapter_operation_state(  # noqa: SLF001
                    _WORKSPACE_ID,
                    _CHAPTER_1,
                    expected=_ChapterBudget(1, 0, 0),
                    target_state="reviewing",
                    operation="write",
                )

        retained = harness.store.get(_WORKSPACE_ID, _CHAPTER_1)
        assert retained is not None
        assert retained.lease_id == lease.lease_id
        assert retained.phase == "chapter_state_claiming"

    def test_stale_phase_cas_loser_cannot_release_winner_lease(
        self,
        harness: _Harness,
    ) -> None:
        """过期 callback 的 reserved 快照不能删掉已 claim 的锁。"""

        stale = _acquire(
            harness,
            task_id="task-phase-race",
            operation_kind="write",
        )
        with harness.session_factory(_WORKSPACE_ID) as winner_session:
            harness.store.begin_immediate(winner_session)
            winner = harness.store.mark_phase_in_session(
                winner_session,
                stale,
                phase="chapter_state_claiming",
            )
            winner_session.commit()

        with harness.session_factory(_WORKSPACE_ID) as loser_session:
            harness.store.begin_immediate(loser_session)
            with pytest.raises(ChapterOperationLeaseError):
                harness.store.mark_phase_in_session(
                    loser_session,
                    stale,
                    phase="chapter_state_claiming",
                )
            loser_session.rollback()

        assert (
            harness.store.release_if_phase(
                stale,
                expected_phase="reserved_before_provider",
            )
            is False
        )
        retained = harness.store.get(_WORKSPACE_ID, _CHAPTER_1)
        assert retained is not None
        assert retained.lease_id == winner.lease_id
        assert retained.phase == "chapter_state_claiming"

    def test_cancel_before_callback_releases_reserved_lease_without_service_call(
        self,
        harness: _Harness,
        monkeypatch: pytest.MonkeyPatch,
    ) -> None:
        """reserve→pending cancel→迟到 callback 必须 0 Worker/Provider。"""

        task_id = "task-cancel-before-callback"
        _reserve_ui(
            harness,
            monkeypatch,
            chapter_id=_CHAPTER_1,
            task_id=task_id,
        )
        task_registry.record_pending(
            task_id,
            type="chapter_rewrite",
            workspace_id=_WORKSPACE_ID,
        )
        assert task_registry.cancel_task(task_id) is False

        assert (
            _claim_chapter_worker_or_cleanup(
                harness.chapter_service,
                _WORKSPACE_ID,
                _CHAPTER_1,
                task_id,
            )
            is False
        )
        assert harness.store.get(_WORKSPACE_ID, _CHAPTER_1) is None
        task = task_registry.get(task_id)
        assert task is not None
        assert task.status == "cancelled"

    def test_duplicate_callback_is_rejected_without_releasing_reserved_lease(
        self,
        harness: _Harness,
        monkeypatch: pytest.MonkeyPatch,
    ) -> None:
        task_id = "task-duplicate-callback"
        _reserve_ui(
            harness,
            monkeypatch,
            chapter_id=_CHAPTER_1,
            task_id=task_id,
        )
        task_registry.record_pending(
            task_id,
            type="chapter_rewrite",
            workspace_id=_WORKSPACE_ID,
        )

        assert _claim_chapter_worker_or_cleanup(
            harness.chapter_service,
            _WORKSPACE_ID,
            _CHAPTER_1,
            task_id,
        )
        assert not _claim_chapter_worker_or_cleanup(
            harness.chapter_service,
            _WORKSPACE_ID,
            _CHAPTER_1,
            task_id,
        )
        retained = harness.store.get(_WORKSPACE_ID, _CHAPTER_1)
        assert retained is not None
        assert retained.phase == "reserved_before_provider"

    def test_duplicate_callback_cannot_unregister_winner_cancel_event(
        self,
        harness: _Harness,
        monkeypatch: pytest.MonkeyPatch,
    ) -> None:
        """loser 在 Event 绑定前被拦，用户仍能取消 winner。"""

        task_id = "task-duplicate-cancel-event"
        monkeypatch.setattr(
            harness.chapter_service,
            "_run_gates",
            lambda *_args, **_kwargs: None,
        )
        harness.chapter_service.reserve_operation_task(
            _WORKSPACE_ID,
            _CHAPTER_1,
            operation="write",
            task_id=task_id,
        )
        task_registry.record_pending(
            task_id,
            type="chapter_write",
            workspace_id=_WORKSPACE_ID,
        )
        entered = threading.Event()
        allow_cancel_raise = threading.Event()
        calls: list[str] = []

        def _blocked_write(*_args: object, **_kwargs: object) -> None:
            calls.append("write")
            entered.set()
            assert allow_cancel_raise.wait(timeout=3)
            raise LLMCancelledError

        monkeypatch.setattr(harness.chapter_service, "write", _blocked_write)

        args = (
            task_id,
            _WORKSPACE_ID,
            _run_chapter_write_task,
            harness.chapter_service,
            _WORKSPACE_ID,
            _CHAPTER_1,
            task_id,
        )
        winner = threading.Thread(
            target=_run_chapter_cancellable,
            args=args,
            kwargs={"profile_override": None},
        )
        winner.start()
        assert entered.wait(timeout=3)

        # 重复 callback 只做 rejected 退出，不会 bind/unregister
        # winner 的共享 Event。
        _run_chapter_cancellable(*args, profile_override=None)
        assert task_registry.cancel_task(task_id) is True
        allow_cancel_raise.set()
        winner.join(timeout=3)
        assert not winner.is_alive()

        assert calls == ["write"]
        assert harness.store.get(_WORKSPACE_ID, _CHAPTER_1) is None
        task = task_registry.get(task_id)
        assert task is not None
        assert task.status == "cancelled"


class TestPipelineUiMutualExclusion:
    def test_ui_reserve_blocks_pipeline_start(
        self,
        harness: _Harness,
        monkeypatch: pytest.MonkeyPatch,
    ) -> None:
        _reserve_ui(
            harness,
            monkeypatch,
            chapter_id=_CHAPTER_1,
            task_id="task-ui",
        )

        with pytest.raises(ChapterOperationLeaseError, match="Pipeline start"):
            _start_pipeline(harness)

    def test_pipeline_start_blocks_ui_reserve(
        self,
        harness: _Harness,
        monkeypatch: pytest.MonkeyPatch,
    ) -> None:
        _start_pipeline(harness)

        with pytest.raises(
            RewriteCheckpointRecoveryError,
            match="Pipeline 正在运行",
        ):
            _reserve_ui(
                harness,
                monkeypatch,
                chapter_id=_CHAPTER_1,
                task_id="task-ui",
            )

    def test_ui_reserve_blocks_pipeline_resume(
        self,
        harness: _Harness,
        monkeypatch: pytest.MonkeyPatch,
    ) -> None:
        run = _start_pipeline(harness)
        _set_run_checkpoint(
            harness,
            run.run_id,
            state="paused",
            current_chapter=_CHAPTER_1,
        )
        _reserve_ui(
            harness,
            monkeypatch,
            chapter_id=_CHAPTER_1,
            task_id="task-ui",
        )

        with pytest.raises(ChapterOperationLeaseError, match="Pipeline resume"):
            harness.pipeline_service.resume(_WORKSPACE_ID)

    def test_pipeline_resume_blocks_ui_reserve(
        self,
        harness: _Harness,
        monkeypatch: pytest.MonkeyPatch,
    ) -> None:
        run = _start_pipeline(harness)
        _set_run_checkpoint(
            harness,
            run.run_id,
            state="paused",
            current_chapter=_CHAPTER_1,
        )
        resumed = harness.pipeline_service.resume(_WORKSPACE_ID)
        assert resumed.state == "running"

        with pytest.raises(
            RewriteCheckpointRecoveryError,
            match="Pipeline 正在运行",
        ):
            _reserve_ui(
                harness,
                monkeypatch,
                chapter_id=_CHAPTER_1,
                task_id="task-ui",
            )

    def test_paused_current_ui_reserve_inherits_run_and_cap(
        self,
        harness: _Harness,
        monkeypatch: pytest.MonkeyPatch,
    ) -> None:
        run = _start_pipeline(harness, cap=23)
        _set_run_checkpoint(
            harness,
            run.run_id,
            state="paused",
            current_chapter=_CHAPTER_1,
        )

        _reserve_ui(
            harness,
            monkeypatch,
            chapter_id=_CHAPTER_1,
            task_id="task-ui",
        )
        lease = harness.store.get(_WORKSPACE_ID, _CHAPTER_1)
        assert lease is not None
        assert lease.run_id == run.run_id
        scope = harness.chapter_service._resolve_manual_attempt_scope(
            _WORKSPACE_ID,
            _CHAPTER_1,
            task_id="task-ui",
        )
        assert scope == {
            "workspace_id": _WORKSPACE_ID,
            "run_id": run.run_id,
            "task_id": "task-ui",
            "chapter_id": _CHAPTER_1,
            "provider_call_cap": 23,
        }

    def test_paused_non_current_ui_reserve_is_rejected(
        self,
        harness: _Harness,
        monkeypatch: pytest.MonkeyPatch,
    ) -> None:
        run = _start_pipeline(harness)
        _set_run_checkpoint(
            harness,
            run.run_id,
            state="paused",
            current_chapter=_CHAPTER_1,
        )

        with pytest.raises(
            RewriteCheckpointRecoveryError,
            match="只允许修复尚未完成的 current chapter",
        ):
            _reserve_ui(
                harness,
                monkeypatch,
                chapter_id=_CHAPTER_2,
                task_id="task-ui",
            )


class TestEarlyLeaseReleaseBoundary:
    @pytest.mark.parametrize("damage", ["missing_run", "wrong_current"])
    def test_startup_rejects_corrupt_pipeline_binding_before_release(
        self,
        harness: _Harness,
        damage: str,
    ) -> None:
        """语法合法但缺失/串章的 run 绑定不能被当成零调用锁洗掉。"""

        run_id = "run-corrupt-binding"
        if damage == "wrong_current":
            now = datetime.now(UTC)
            with harness.session_factory(_WORKSPACE_ID) as session:
                session.add(
                    PipelineRunORM(
                        run_id=run_id,
                        workspace_id=_WORKSPACE_ID,
                        state="paused",
                        scope="volume",
                        start_chapter_id=None,
                        end_chapter_id=None,
                        volume_id="vol-1",
                        profile_override=None,
                        chapter_ids=[_CHAPTER_1, _CHAPTER_2],
                        current_chapter=_CHAPTER_2,
                        completed_chapter_ids=[],
                        pause_reason="user_request",
                        error_details=None,
                        failure_policy="stop",
                        skipped_chapter_ids=[],
                        provider_call_cap=17,
                        provider_call_count=0,
                        transport_retry_count=0,
                        pipeline_attempt_count=0,
                        upstream_generation_attempt_count=0,
                        created_at=now,
                        updated_at=now,
                        completed_at=None,
                    )
                )
                session.commit()
        now = datetime.now(UTC)
        with harness.session_factory(_WORKSPACE_ID) as session:
            session.add(
                ChapterOperationLeaseORM(
                    workspace_id=_WORKSPACE_ID,
                    chapter_id=_CHAPTER_1,
                    lease_id="chapter_op_" + "b" * 32,
                    operation_kind="rewrite",
                    source_version=0,
                    task_id="task-corrupt-binding",
                    run_id=run_id,
                    phase="reserved_before_provider",
                    created_at=now,
                    updated_at=now,
                )
            )
            session.commit()
        lease = harness.store.get(_WORKSPACE_ID, _CHAPTER_1)
        assert lease is not None

        with pytest.raises(
            RewriteCheckpointRecoveryError,
            match=("缺失或跨工作区" if damage == "missing_run" else "当前 checkpoint"),
        ):
            harness.chapter_service.reconcile_operation_leases_on_startup(
                _WORKSPACE_ID,
                apply=False,
            )

        assert harness.store.get(_WORKSPACE_ID, _CHAPTER_1) == lease

    @pytest.mark.parametrize(
        ("operation_kind", "phase"),
        [
            ("unknown-operation", "reserved_before_provider"),
            ("rewrite", "source_mutation_committed"),
        ],
    )
    def test_startup_rejects_corrupt_operation_phase_shape(
        self,
        harness: _Harness,
        operation_kind: str,
        phase: str,
    ) -> None:
        """未知 operation 或错配 phase 必须保留并阻断，不得按早期锁释放。"""

        now = datetime.now(UTC)
        with harness.session_factory(_WORKSPACE_ID) as session:
            session.add(
                ChapterOperationLeaseORM(
                    workspace_id=_WORKSPACE_ID,
                    chapter_id=_CHAPTER_1,
                    lease_id="chapter_op_" + "a" * 32,
                    operation_kind=operation_kind,
                    source_version=0,
                    task_id="task-corrupt-shape",
                    run_id=None,
                    phase=phase,
                    created_at=now,
                    updated_at=now,
                )
            )
            session.commit()

        with pytest.raises(
            ChapterOperationLeaseError,
            match="持久形状损坏",
        ):
            harness.chapter_service.reconcile_operation_leases_on_startup(
                _WORKSPACE_ID,
                apply=False,
            )

        with harness.session_factory(_WORKSPACE_ID) as session:
            row = session.get(
                ChapterOperationLeaseORM,
                (_WORKSPACE_ID, _CHAPTER_1),
            )
            assert row is not None
            assert row.operation_kind == operation_kind
            assert row.phase == phase

    @pytest.mark.parametrize(
        ("attempt_status", "should_release"),
        [
            ("failed", True),
            ("started", False),
            ("succeeded", False),
        ],
    )
    def test_startup_ledger_phase_releases_only_known_failed_attempt(
        self,
        harness: _Harness,
        attempt_status: str,
        should_release: bool,
    ) -> None:
        """无有效 ledger：failed 可重进；started/succeeded 保留结果现场。"""

        _seed_body_checkpoint_without_ledger(harness)
        lease = _mark_phase(
            harness,
            _acquire(
                harness,
                source_version=1,
                operation_kind="write",
            ),
            "continuity_ledger_may_call_provider",
        )
        _insert_attempt(
            harness,
            lease,
            status=attempt_status,
            agent_name="chapter_summary_extractor",
        )

        released, retained = (
            harness.chapter_service.reconcile_operation_leases_on_startup(_WORKSPACE_ID)
        )

        if should_release:
            assert released == [_CHAPTER_1]
            assert retained == []
            assert harness.store.get(_WORKSPACE_ID, _CHAPTER_1) is None
        else:
            assert released == []
            assert retained == [_CHAPTER_1]
            assert harness.store.get(_WORKSPACE_ID, _CHAPTER_1) == lease

    @pytest.mark.parametrize(
        ("attempt_status", "should_release"),
        [
            ("failed", True),
            ("started", False),
            ("succeeded", False),
        ],
    )
    def test_same_process_cleanup_ledger_phase_matches_attempt_outcome(
        self,
        harness: _Harness,
        attempt_status: str,
        should_release: bool,
    ) -> None:
        """同进程失败回调与 cold-start 使用同一安全分类。"""

        _seed_body_checkpoint_without_ledger(harness)
        lease = _mark_phase(
            harness,
            _acquire(
                harness,
                source_version=1,
                operation_kind="write",
            ),
            "continuity_ledger_may_call_provider",
        )
        _insert_attempt(
            harness,
            lease,
            status=attempt_status,
            agent_name="chapter_summary_extractor",
        )

        released = harness.chapter_service.release_operation_task_if_never_dispatched(
            _WORKSPACE_ID,
            _CHAPTER_1,
            task_id=lease.task_id,
        )

        assert released is should_release
        if should_release:
            assert harness.store.get(_WORKSPACE_ID, _CHAPTER_1) is None
        else:
            assert harness.store.get(_WORKSPACE_ID, _CHAPTER_1) == lease

    @pytest.mark.parametrize("attempt_status", [None, "failed"])
    @pytest.mark.parametrize(
        "phase", ["chapter_state_claiming", "provider_capable_pipeline"]
    )
    def test_startup_releases_zero_materialization_early_lease(
        self,
        harness: _Harness,
        phase: str,
        attempt_status: str | None,
    ) -> None:
        lease = _mark_phase(harness, _acquire(harness), phase)
        if attempt_status is not None:
            _insert_attempt(harness, lease, status=attempt_status)

        released, retained = (
            harness.chapter_service.reconcile_operation_leases_on_startup(_WORKSPACE_ID)
        )

        assert released == [_CHAPTER_1]
        assert retained == []
        assert harness.store.get(_WORKSPACE_ID, _CHAPTER_1) is None

    def test_startup_preflight_classifies_without_releasing_lease(
        self,
        harness: _Harness,
    ) -> None:
        """全局 preflight 只分类；必须等全部工作区验真后才允许 apply。"""

        lease = _mark_phase(
            harness,
            _acquire(harness),
            "chapter_state_claiming",
        )

        released, retained = (
            harness.chapter_service.reconcile_operation_leases_on_startup(
                _WORKSPACE_ID,
                apply=False,
            )
        )

        assert released == [_CHAPTER_1]
        assert retained == []
        assert harness.store.get(_WORKSPACE_ID, _CHAPTER_1) == lease

    @pytest.mark.parametrize("attempt_status", ["started", "succeeded"])
    @pytest.mark.parametrize(
        "phase", ["chapter_state_claiming", "provider_capable_pipeline"]
    )
    def test_startup_retains_unknown_or_returned_provider_attempt(
        self,
        harness: _Harness,
        phase: str,
        attempt_status: str,
    ) -> None:
        lease = _mark_phase(harness, _acquire(harness), phase)
        _insert_attempt(harness, lease, status=attempt_status)

        released, retained = (
            harness.chapter_service.reconcile_operation_leases_on_startup(_WORKSPACE_ID)
        )

        assert released == []
        assert retained == [_CHAPTER_1]
        assert harness.store.get(_WORKSPACE_ID, _CHAPTER_1) == lease

    @pytest.mark.parametrize("attempt_status", ["started", "succeeded"])
    @pytest.mark.parametrize(
        "phase", ["chapter_state_claiming", "provider_capable_pipeline"]
    )
    def test_startup_retains_cross_task_provider_attempt_after_lease(
        self,
        harness: _Harness,
        phase: str,
        attempt_status: str,
    ) -> None:
        """同章租约后的跨 task 出网是账本异常，不能被过滤成零调用。"""

        lease = _mark_phase(harness, _acquire(harness), phase)
        _insert_attempt(
            harness,
            lease,
            status=attempt_status,
            task_id="task-context-was-misbound",
        )

        released, retained = (
            harness.chapter_service.reconcile_operation_leases_on_startup(_WORKSPACE_ID)
        )

        assert released == []
        assert retained == [_CHAPTER_1]
        assert harness.store.get(_WORKSPACE_ID, _CHAPTER_1) == lease

    @pytest.mark.parametrize("attempt_status", [None, "failed"])
    @pytest.mark.parametrize(
        "phase", ["chapter_state_claiming", "provider_capable_pipeline"]
    )
    def test_task_cancel_releases_only_proven_zero_materialization(
        self,
        harness: _Harness,
        phase: str,
        attempt_status: str | None,
    ) -> None:
        lease = _mark_phase(harness, _acquire(harness), phase)
        if attempt_status is not None:
            _insert_attempt(harness, lease, status=attempt_status)

        assert harness.chapter_service.release_operation_task_if_never_dispatched(
            _WORKSPACE_ID,
            _CHAPTER_1,
            task_id=lease.task_id,
        )
        assert harness.store.get(_WORKSPACE_ID, _CHAPTER_1) is None

    @pytest.mark.parametrize("attempt_status", ["started", "succeeded"])
    @pytest.mark.parametrize(
        "phase", ["chapter_state_claiming", "provider_capable_pipeline"]
    )
    def test_task_cancel_retains_unknown_or_returned_provider_attempt(
        self,
        harness: _Harness,
        phase: str,
        attempt_status: str,
    ) -> None:
        lease = _mark_phase(harness, _acquire(harness), phase)
        _insert_attempt(harness, lease, status=attempt_status)

        assert not (
            harness.chapter_service.release_operation_task_if_never_dispatched(
                _WORKSPACE_ID,
                _CHAPTER_1,
                task_id=lease.task_id,
            )
        )
        assert harness.store.get(_WORKSPACE_ID, _CHAPTER_1) == lease

    def test_task_cancel_retains_cross_task_provider_attempt_after_lease(
        self,
        harness: _Harness,
    ) -> None:
        lease = _mark_phase(
            harness,
            _acquire(harness),
            "chapter_state_claiming",
        )
        _insert_attempt(
            harness,
            lease,
            status="started",
            task_id="task-context-was-misbound",
        )

        assert not (
            harness.chapter_service.release_operation_task_if_never_dispatched(
                _WORKSPACE_ID,
                _CHAPTER_1,
                task_id=lease.task_id,
            )
        )
        assert harness.store.get(_WORKSPACE_ID, _CHAPTER_1) == lease


class TestProviderCheckpointCrashRecovery:
    def test_user_cancelled_post_review_checkpoint_marks_replay_and_releases_atomically(
        self,
        harness: _Harness,
    ) -> None:
        lease = _seed_cancelled_post_review_checkpoint(harness)

        def _cancelled(task_id: str, workspace_id: str, task_type: str) -> bool:
            return (
                task_id == lease.task_id
                and workspace_id == _WORKSPACE_ID
                and task_type == "chapter_write"
            )

        prospective_released, prospective_retained = (
            harness.chapter_service.reconcile_operation_leases_on_startup(
                _WORKSPACE_ID,
                apply=False,
                cancelled_task_check=_cancelled,
            )
        )
        assert prospective_released == [_CHAPTER_1]
        assert prospective_retained == []
        assert harness.store.get(_WORKSPACE_ID, _CHAPTER_1) == lease
        with harness.session_factory(_WORKSPACE_ID) as session:
            chapter = session.get(ChapterORM, _CHAPTER_1)
            assert chapter is not None
            assert chapter.escalation_details == {
                "reason": "rewind_requires_content_rewrite",
                "source_version_id": 1,
                "source_status": "approved",
            }

        released, retained = (
            harness.chapter_service.reconcile_operation_leases_on_startup(
                _WORKSPACE_ID,
                cancelled_task_check=_cancelled,
            )
        )

        assert released == [_CHAPTER_1]
        assert retained == []
        assert harness.store.get(_WORKSPACE_ID, _CHAPTER_1) is None
        with harness.session_factory(_WORKSPACE_ID) as session:
            chapter = session.get(ChapterORM, _CHAPTER_1)
            assert chapter is not None
            marker = chapter.escalation_details
            assert isinstance(marker, dict)
            assert marker["reason"] == "cancelled_post_review_replay"
            assert marker["source_task_id"] == lease.task_id
            assert marker["source_lease_id"] == lease.lease_id
            assert marker["source_version_id"] == 1
            assert marker["version_reviewed"] == 2
            assert marker["run_id"] == lease.run_id
            assert len(marker["content_sha256"]) == 64
            assert len(marker["attempt_fingerprint"]) == 64

        reset_result = ChapterRewindService(
            workspace_service=harness.workspace_service,
            session_factory=harness.session_factory,
            active_task_checker=lambda _workspace_id: False,
            operation_lease_store=harness.store,
        ).reset_stale(_WORKSPACE_ID)

        assert reset_result.reset_count == 0
        assert reset_result.reset_chapter_ids == []
        with harness.session_factory(_WORKSPACE_ID) as session:
            chapter = session.get(ChapterORM, _CHAPTER_1)
            assert chapter is not None
            assert chapter.status == "reviewing"
            assert chapter.escalation_details["reason"] == (
                "cancelled_post_review_replay"
            )

    def test_cancelled_post_review_replay_routes_to_reviewing_without_writer(
        self,
        harness: _Harness,
        monkeypatch: pytest.MonkeyPatch,
    ) -> None:
        lease = _seed_cancelled_post_review_checkpoint(harness)
        released, retained = (
            harness.chapter_service.reconcile_operation_leases_on_startup(
                _WORKSPACE_ID,
                cancelled_task_check=lambda task_id, workspace_id, task_type: (
                    task_id == lease.task_id
                    and workspace_id == _WORKSPACE_ID
                    and task_type == "chapter_write"
                ),
            )
        )
        assert released == [_CHAPTER_1]
        assert retained == []

        monkeypatch.setattr(
            harness.chapter_service,
            "_run_gates",
            lambda *_args, **_kwargs: None,
        )
        captured: dict[str, object] = {}
        sentinel = object()

        def _capture_orchestrate(**kwargs: object) -> object:
            captured.update(kwargs)
            return sentinel

        monkeypatch.setattr(
            harness.chapter_service,
            "_orchestrate",
            _capture_orchestrate,
        )

        result = harness.chapter_service.write(_WORKSPACE_ID, _CHAPTER_1)

        assert result is sentinel
        assert captured["initial_state"] == ChapterState.REVIEWING
        assert captured["initial_last_version_id"] == 2
        assert captured["initial_rewrite_count"] == 1
        checkpoint = captured["rewrite_checkpoint_context"]
        assert checkpoint is not None
        assert getattr(checkpoint, "version_id") == 2
        assert getattr(checkpoint, "review") is None
        assert getattr(checkpoint, "review_result") is None

    @pytest.mark.parametrize(
        "damage",
        ["task_not_cancelled", "started_attempt", "disk_drift"],
    )
    def test_cancelled_post_review_replay_retains_any_unproven_checkpoint(
        self,
        harness: _Harness,
        damage: str,
    ) -> None:
        lease = _seed_cancelled_post_review_checkpoint(harness)
        if damage == "started_attempt":
            _insert_attempt(
                harness,
                lease,
                status="started",
                agent_name="ending_tension_validator",
                attempt_id="attempt-unknown-started",
                created_at=lease.created_at + timedelta(seconds=6),
                operation_kind="agent_call",
            )
        elif damage == "disk_drift":
            _write_version_file(harness, version_id=2, content="磁盘漂移正文")

        released, retained = (
            harness.chapter_service.reconcile_operation_leases_on_startup(
                _WORKSPACE_ID,
                cancelled_task_check=lambda *_args: damage != "task_not_cancelled",
            )
        )

        assert released == []
        assert retained == [_CHAPTER_1]
        assert harness.store.get(_WORKSPACE_ID, _CHAPTER_1) == lease
        with harness.session_factory(_WORKSPACE_ID) as session:
            chapter = session.get(ChapterORM, _CHAPTER_1)
            assert chapter is not None
            assert chapter.escalation_details["reason"] == (
                "rewind_requires_content_rewrite"
            )

    def test_startup_finishes_approved_pass_before_final_and_releases(
        self,
        harness: _Harness,
    ) -> None:
        """PASS/approved 已落但 mark_final 前崩溃时只做本地确定性收口。"""

        _seed_provider_checkpoint(harness)
        lease = _mark_phase(
            harness,
            _acquire(
                harness,
                source_version=1,
                operation_kind="re-review",
            ),
            "provider_capable_pipeline",
        )
        reviewer_at = lease.created_at + timedelta(seconds=1)
        _insert_attempt(
            harness,
            lease,
            status="succeeded",
            agent_name="reviewer",
            created_at=reviewer_at,
        )
        _insert_review(
            harness,
            version_id=1,
            reviewed_at=reviewer_at + timedelta(seconds=1),
            review_id="review-approved-before-final",
        )
        with harness.session_factory(_WORKSPACE_ID) as session:
            row = session.get(ChapterORM, _CHAPTER_1)
            assert row is not None
            row.status = "approved"
            row.approved_at = reviewer_at + timedelta(seconds=2)
            session.commit()

        final_path = harness.paths.chapter_version_file(_CHAPTER_1, "final.md")
        assert not final_path.exists()
        # 同进程失败/取消回调不能在 final 尚未验真时删锁。
        assert not (
            harness.chapter_service.release_operation_task_if_never_dispatched(
                _WORKSPACE_ID,
                _CHAPTER_1,
                task_id=lease.task_id,
            )
        )

        released, retained = (
            harness.chapter_service.reconcile_operation_leases_on_startup(_WORKSPACE_ID)
        )

        assert released == [_CHAPTER_1]
        assert retained == []
        assert final_path.read_text(encoding="utf-8") == "正文"
        assert harness.store.get(_WORKSPACE_ID, _CHAPTER_1) is None

    def test_startup_retargets_old_known_final_to_new_approved_version(
        self,
        harness: _Harness,
    ) -> None:
        """Rewrite 通过后若 final 仍指旧 v1，可按完整账本收口到 v2。"""

        _seed_provider_checkpoint(harness)
        old_final = harness.paths.chapter_version_file(_CHAPTER_1, "final.md")
        old_final.write_text("正文", encoding="utf-8")
        lease = _mark_phase(
            harness,
            _acquire(
                harness,
                source_version=1,
                operation_kind="rewrite",
            ),
            "provider_capable_pipeline",
        )
        writer_at = lease.created_at + timedelta(seconds=1)
        _insert_attempt(
            harness,
            lease,
            status="succeeded",
            agent_name="writer",
            created_at=writer_at,
        )
        version_at = lease.created_at + timedelta(seconds=2)
        with harness.session_factory(_WORKSPACE_ID) as session:
            row = session.get(ChapterORM, _CHAPTER_1)
            assert row is not None
            row.current_version = 2
            session.add(
                ChapterVersionORM(
                    chapter_id=_CHAPTER_1,
                    version_id=2,
                    content="第二版通过正文",
                    word_count=7,
                    created_at=version_at,
                    creation_type="rewrite",
                    model_used="mock-model",
                    review_score=None,
                    notes="",
                    style_fingerprint=None,
                )
            )
            session.commit()
        _write_version_file(
            harness,
            version_id=2,
            content="第二版通过正文",
        )
        reviewer_at = lease.created_at + timedelta(seconds=3)
        _insert_attempt(
            harness,
            lease,
            status="succeeded",
            agent_name="reviewer",
            created_at=reviewer_at,
        )
        _insert_review(
            harness,
            version_id=2,
            reviewed_at=reviewer_at + timedelta(seconds=1),
            review_id="review-rewrite-approved-before-final",
        )
        with harness.session_factory(_WORKSPACE_ID) as session:
            row = session.get(ChapterORM, _CHAPTER_1)
            assert row is not None
            row.status = "approved"
            row.approved_at = reviewer_at + timedelta(seconds=2)
            session.commit()

        released, retained = (
            harness.chapter_service.reconcile_operation_leases_on_startup(_WORKSPACE_ID)
        )

        assert released == [_CHAPTER_1]
        assert retained == []
        assert old_final.read_text(encoding="utf-8") == "第二版通过正文"

    def test_startup_retains_approved_pass_with_unknown_final_bytes(
        self,
        harness: _Harness,
    ) -> None:
        """final 不是任一 DB/盘已知版本时不得覆盖证据或释放租约。"""

        _seed_provider_checkpoint(harness)
        lease = _mark_phase(
            harness,
            _acquire(
                harness,
                source_version=1,
                operation_kind="re-review",
            ),
            "provider_capable_pipeline",
        )
        reviewer_at = lease.created_at + timedelta(seconds=1)
        _insert_attempt(
            harness,
            lease,
            status="succeeded",
            agent_name="reviewer",
            created_at=reviewer_at,
        )
        _insert_review(
            harness,
            version_id=1,
            reviewed_at=reviewer_at + timedelta(seconds=1),
            review_id="review-approved-corrupt-final",
        )
        with harness.session_factory(_WORKSPACE_ID) as session:
            row = session.get(ChapterORM, _CHAPTER_1)
            assert row is not None
            row.status = "approved"
            row.approved_at = reviewer_at + timedelta(seconds=2)
            session.commit()
        final_path = harness.paths.chapter_version_file(_CHAPTER_1, "final.md")
        final_path.write_text("未知且不能覆盖的正文", encoding="utf-8")

        released, retained = (
            harness.chapter_service.reconcile_operation_leases_on_startup(_WORKSPACE_ID)
        )

        assert released == []
        assert retained == [_CHAPTER_1]
        assert final_path.read_text(encoding="utf-8") == "未知且不能覆盖的正文"
        assert harness.store.get(_WORKSPACE_ID, _CHAPTER_1) == lease

    def test_provider_phase_latest_source_releases_new_complete_review(
        self,
        harness: _Harness,
    ) -> None:
        """latest==source 只能由本租约后完整落库的 review 证明可续跑。"""

        _seed_provider_checkpoint(harness)
        lease = _mark_phase(
            harness,
            _acquire(harness, source_version=1),
            "provider_capable_pipeline",
        )
        attempt_at = lease.created_at + timedelta(seconds=1)
        _insert_attempt(
            harness,
            lease,
            status="succeeded",
            agent_name="reviewer",
            created_at=attempt_at,
        )
        _insert_review(
            harness,
            version_id=1,
            reviewed_at=attempt_at + timedelta(seconds=1),
            review_id="review-current-lease",
        )

        released, retained = (
            harness.chapter_service.reconcile_operation_leases_on_startup(_WORKSPACE_ID)
        )

        assert released == [_CHAPTER_1]
        assert retained == []
        assert harness.store.get(_WORKSPACE_ID, _CHAPTER_1) is None

    @pytest.mark.parametrize("disk_drift", ["mutated_source", "extra_version"])
    def test_revise_rejects_disk_catalog_drift_before_provider(
        self,
        harness: _Harness,
        monkeypatch: pytest.MonkeyPatch,
        disk_drift: str,
    ) -> None:
        """Reviser 必须在任何调用前冻结完整 DB/盘版本目录。"""

        _seed_provider_checkpoint(harness)
        if disk_drift == "mutated_source":
            _write_version_file(
                harness,
                version_id=1,
                content="磁盘被改过的正文",
            )
        else:
            _write_version_file(
                harness,
                version_id=2,
                content="只有磁盘存在的幽灵版本",
            )
        monkeypatch.setattr(
            harness.chapter_service,
            "_run_gates",
            lambda *_args, **_kwargs: None,
        )
        monkeypatch.setattr(
            harness.chapter_service,
            "read_chapter_card",
            lambda *_args, **_kwargs: object(),
        )

        def _provider_must_not_run(**_kwargs: object) -> None:
            raise AssertionError("revise source 验真失败后不得调用 Provider")

        monkeypatch.setattr(
            "src.services.chapter_service.revise_chapter",
            _provider_must_not_run,
        )

        with pytest.raises(RewriteCheckpointRecoveryError):
            harness.chapter_service.revise(
                _WORKSPACE_ID,
                _CHAPTER_1,
                issues=[],
            )

        assert harness.store.get(_WORKSPACE_ID, _CHAPTER_1) is None

    def test_provider_phase_reviewer_succeeded_without_review_retains(
        self,
        harness: _Harness,
    ) -> None:
        """Reviewer 已返回却没有 review row，响应可能未消费，必须保留。"""

        _seed_provider_checkpoint(harness)
        lease = _mark_phase(
            harness,
            _acquire(harness, source_version=1),
            "provider_capable_pipeline",
        )
        _insert_attempt(
            harness,
            lease,
            status="succeeded",
            agent_name="reviewer",
            created_at=lease.created_at + timedelta(seconds=1),
        )

        released, retained = (
            harness.chapter_service.reconcile_operation_leases_on_startup(_WORKSPACE_ID)
        )

        assert released == []
        assert retained == [_CHAPTER_1]
        assert harness.store.get(_WORKSPACE_ID, _CHAPTER_1) == lease

    def test_review_success_before_latest_version_without_review_row_retains(
        self,
        harness: _Harness,
    ) -> None:
        """Reviewer 成功响应不能被较新版本时间掩盖而错误释放租约。"""

        _seed_provider_checkpoint(harness)
        lease = _mark_phase(
            harness,
            _acquire(harness, source_version=1),
            "provider_capable_pipeline",
        )
        _insert_attempt(
            harness,
            lease,
            status="succeeded",
            agent_name="reviewer",
            created_at=lease.created_at + timedelta(seconds=1),
        )
        selected_at = lease.created_at + timedelta(seconds=2)
        with harness.session_factory(_WORKSPACE_ID) as session:
            row = session.get(ChapterORM, _CHAPTER_1)
            assert row is not None
            row.current_version = 2
            session.add(
                ChapterVersionORM(
                    chapter_id=_CHAPTER_1,
                    version_id=2,
                    content="已落盘但未落审稿记录的正文",
                    word_count=8,
                    created_at=selected_at,
                    creation_type="draft",
                    model_used="mock-model",
                    review_score=None,
                    notes="",
                    style_fingerprint=None,
                )
            )
            session.commit()
        _write_version_file(
            harness,
            version_id=2,
            content="已落盘但未落审稿记录的正文",
        )

        released, retained = (
            harness.chapter_service.reconcile_operation_leases_on_startup(_WORKSPACE_ID)
        )

        assert released == []
        assert retained == [_CHAPTER_1]
        assert harness.store.get(_WORKSPACE_ID, _CHAPTER_1) == lease

    def test_provider_phase_old_review_and_new_continuity_success_retains(
        self,
        harness: _Harness,
    ) -> None:
        """旧 review 不能掩盖本租约尚未落账的 continuity 成功响应。"""

        _seed_provider_checkpoint(harness)
        lease = _mark_phase(
            harness,
            _acquire(harness, source_version=1),
            "provider_capable_pipeline",
        )
        _insert_review(
            harness,
            version_id=1,
            reviewed_at=lease.created_at - timedelta(seconds=1),
            review_id="review-before-current-lease",
        )
        _insert_attempt(
            harness,
            lease,
            status="succeeded",
            agent_name="chapter_summary_extractor",
            created_at=lease.created_at + timedelta(seconds=1),
        )

        released, retained = (
            harness.chapter_service.reconcile_operation_leases_on_startup(_WORKSPACE_ID)
        )

        assert released == []
        assert retained == [_CHAPTER_1]
        assert harness.store.get(_WORKSPACE_ID, _CHAPTER_1) == lease

    @pytest.mark.parametrize(
        ("disk_mode", "should_release"),
        [
            ("db_only", False),
            ("mismatch", False),
            ("complete", True),
        ],
    )
    def test_reviser_phase_requires_exact_source_plus_one_db_disk_checkpoint(
        self,
        harness: _Harness,
        disk_mode: str,
        should_release: bool,
    ) -> None:
        """Reviser 只有完整且逐字一致的 v(source+1) 才允许恢复。"""

        _seed_provider_checkpoint(harness)
        lease = _mark_phase(
            harness,
            _acquire(
                harness,
                source_version=1,
                operation_kind="revise",
            ),
            "reviser_may_call_provider",
        )
        _insert_attempt(
            harness,
            lease,
            status="succeeded",
            agent_name="reviser",
            created_at=lease.created_at + timedelta(seconds=1),
        )
        _materialize_revise_version(
            harness,
            lease,
            disk_mode=disk_mode,
        )

        released, retained = (
            harness.chapter_service.reconcile_operation_leases_on_startup(_WORKSPACE_ID)
        )

        if should_release:
            assert released == [_CHAPTER_1]
            assert retained == []
            assert harness.store.get(_WORKSPACE_ID, _CHAPTER_1) is None
        else:
            assert released == []
            assert retained == [_CHAPTER_1]
            assert harness.store.get(_WORKSPACE_ID, _CHAPTER_1) == lease

    @pytest.mark.parametrize(
        ("attempt_status", "should_release"),
        [
            ("failed", True),
            ("started", False),
            ("succeeded", False),
        ],
    )
    def test_continuity_refresh_phase_classifies_attempt_terminal_safely(
        self,
        harness: _Harness,
        attempt_status: str,
        should_release: bool,
    ) -> None:
        """refresh failed 可重进；started/succeeded 均保留未知或未消费结果。"""

        _seed_provider_checkpoint(harness)
        lease = _mark_phase(
            harness,
            _acquire(harness, source_version=1),
            "continuity_refresh_may_call_provider",
        )
        _insert_attempt(
            harness,
            lease,
            status=attempt_status,
            agent_name="chapter_summary_extractor",
            created_at=lease.created_at + timedelta(seconds=1),
        )

        released, retained = (
            harness.chapter_service.reconcile_operation_leases_on_startup(_WORKSPACE_ID)
        )

        if should_release:
            assert released == [_CHAPTER_1]
            assert retained == []
            assert harness.store.get(_WORKSPACE_ID, _CHAPTER_1) is None
        else:
            assert released == []
            assert retained == [_CHAPTER_1]
            assert harness.store.get(_WORKSPACE_ID, _CHAPTER_1) == lease


class TestCompletedLeaseStartupRecovery:
    def test_interrupted_human_result_is_paused_then_lease_released(
        self, harness: _Harness
    ) -> None:
        run = _start_pipeline(harness)
        _set_run_checkpoint(
            harness,
            run.run_id,
            state="running",
            current_chapter=_CHAPTER_1,
        )
        lease = _acquire(harness, run_id=run.run_id)
        lease = _mark_phase(harness, lease, "operation_complete")
        with harness.session_factory(_WORKSPACE_ID) as session:
            session.add(
                ChapterORM(
                    chapter_id=_CHAPTER_1,
                    workspace_id=_WORKSPACE_ID,
                    current_version=0,
                    status="human_intervention",
                    rewrite_count=0,
                    revise_count=0,
                    created_at=datetime.now(UTC),
                    approved_at=None,
                    total_token_usage={},
                    total_cost_usd=0.0,
                )
            )
            stored_run = session.get(PipelineRunORM, run.run_id)
            assert stored_run is not None
            stored_run.state = "interrupted"
            stored_run.updated_at = datetime.now(UTC)
            session.commit()

        released, retained = (
            harness.chapter_service.reconcile_operation_leases_on_startup(_WORKSPACE_ID)
        )

        assert released == [_CHAPTER_1]
        assert retained == []
        assert harness.store.get(_WORKSPACE_ID, _CHAPTER_1) is None
        with harness.session_factory(_WORKSPACE_ID) as session:
            stored_run = session.get(PipelineRunORM, run.run_id)
            assert stored_run is not None
            assert stored_run.state == "paused"
            assert stored_run.pause_reason == "chapter_escalated"
            assert stored_run.error_details == {
                "reason": "chapter_escalated",
                "chapter_id": _CHAPTER_1,
                "recovered_from": "operation_complete_without_run_disposition",
            }

    def test_interrupted_exact_approved_ready_result_releases_lease(
        self, harness: _Harness
    ) -> None:
        _seed_exact_ready_checkpoint(harness)
        run = _start_pipeline(harness)
        _set_run_checkpoint(
            harness,
            run.run_id,
            state="running",
            current_chapter=_CHAPTER_1,
        )
        lease = _acquire(
            harness,
            run_id=run.run_id,
            source_version=1,
        )
        _mark_phase(harness, lease, "operation_complete")
        _set_run_checkpoint(
            harness,
            run.run_id,
            state="interrupted",
            current_chapter=_CHAPTER_1,
        )

        released, retained = (
            harness.chapter_service.reconcile_operation_leases_on_startup(_WORKSPACE_ID)
        )

        assert released == [_CHAPTER_1]
        assert retained == []
        assert harness.store.get(_WORKSPACE_ID, _CHAPTER_1) is None


class TestJsonNullCasCompatibility:
    @pytest.mark.parametrize("storage", ["json_null", "sql_null"])
    def test_chapter_state_claim_accepts_both_null_encodings(
        self,
        harness: _Harness,
        storage: str,
    ) -> None:
        _seed_chapter(harness, memory_details_storage=storage)

        lease = harness.chapter_service._acquire_chapter_operation_state(
            _WORKSPACE_ID,
            _CHAPTER_1,
            expected=_ChapterBudget(
                current_version=1,
                rewrite_count=0,
                revise_count=0,
            ),
            target_state="rewriting",
            operation="rewrite",
        )

        assert lease is not None
        assert lease.phase == "chapter_state_claiming"
        with harness.session_factory(_WORKSPACE_ID) as session:
            row = session.get(ChapterORM, _CHAPTER_1)
            assert row is not None
            assert row.status == "rewriting"

    @pytest.mark.parametrize("storage", ["json_null", "sql_null"])
    def test_rewind_claim_accepts_both_null_encodings(
        self,
        harness: _Harness,
        storage: str,
        usable_vector_store: _ControllableVectorStore,
    ) -> None:
        _seed_chapter(harness, memory_details_storage=storage)
        service = ChapterRewindService(
            workspace_service=harness.workspace_service,
            session_factory=harness.session_factory,
            vector_store_factory=lambda _workspace_id: usable_vector_store,
            operation_lease_store=harness.store,
        )

        result = service.rewind(_WORKSPACE_ID, _CHAPTER_1)

        assert result.to_status == "drafting"
        assert usable_vector_store.calls == [("memories", _CHAPTER_1)]
        assert harness.store.get(_WORKSPACE_ID, _CHAPTER_1) is None
        with harness.session_factory(_WORKSPACE_ID) as session:
            row = session.get(ChapterORM, _CHAPTER_1)
            assert row is not None
            assert row.status == "drafting"


class TestLocalSourceMutationRecovery:
    def test_switch_terminal_ready_refuses_before_final_mutation_and_releases_lease(
        self, harness: _Harness
    ) -> None:
        _seed_chapter(harness, status="approved", current_version=1)
        now = datetime.now(UTC)
        with harness.session_factory(_WORKSPACE_ID) as session:
            row = session.get(ChapterORM, _CHAPTER_1)
            assert row is not None
            row.current_version = 2
            row.ready_for_next_chapter_at = now
            session.add(
                ChapterVersionORM(
                    chapter_id=_CHAPTER_1,
                    version_id=2,
                    content="当前已批准正文",
                    word_count=7,
                    created_at=now,
                    creation_type="rewrite",
                    model_used="mock-model",
                    review_score=9.0,
                    notes="",
                    style_fingerprint=None,
                )
            )
            session.commit()
        chapter_dir = harness.paths.chapter_dir(_CHAPTER_1)
        chapter_dir.mkdir(parents=True, exist_ok=True)
        harness.paths.chapter_version_file(_CHAPTER_1, "v1.md").write_text(
            "正文",
            encoding="utf-8",
        )
        harness.paths.chapter_version_file(_CHAPTER_1, "v2.md").write_text(
            "当前已批准正文",
            encoding="utf-8",
        )
        final_path = harness.paths.chapter_version_file(_CHAPTER_1, "final.md")
        final_path.write_text("当前已批准正文", encoding="utf-8")

        with pytest.raises(
            RewriteCheckpointRecoveryError,
            match="不能直接切换 final 版本",
        ):
            harness.chapter_service.switch_version(
                _WORKSPACE_ID,
                _CHAPTER_1,
                1,
            )

        assert final_path.read_text(encoding="utf-8") == "当前已批准正文"
        assert harness.store.get(_WORKSPACE_ID, _CHAPTER_1) is None

    @pytest.mark.parametrize(
        ("final_before", "crash_point"),
        [
            ("第二版正文", "before_mark_final"),
            ("正文", "after_mark_final"),
        ],
    )
    def test_switch_crash_deterministically_finishes_recorded_target(
        self,
        harness: _Harness,
        final_before: str,
        crash_point: str,
    ) -> None:
        """phase 已记目标时，mark_final 前后崩溃都只能收口到该目标。"""

        _seed_switch_version_checkpoint(harness)
        lease = _mark_phase(
            harness,
            _acquire(
                harness,
                source_version=2,
                operation_kind="switch-version",
            ),
            "local_final_mutation:v1",
        )
        final_path = harness.paths.chapter_version_file(
            _CHAPTER_1,
            "final.md",
        )
        final_path.write_text(final_before, encoding="utf-8")

        released, retained = (
            harness.chapter_service.reconcile_operation_leases_on_startup(_WORKSPACE_ID)
        )

        assert crash_point in {"before_mark_final", "after_mark_final"}
        assert released == [_CHAPTER_1]
        assert retained == []
        assert harness.store.get(_WORKSPACE_ID, _CHAPTER_1) is None
        assert final_path.read_text(encoding="utf-8") == "正文"
        with harness.session_factory(_WORKSPACE_ID) as session:
            row = session.get(ChapterORM, _CHAPTER_1)
            assert row is not None
            # switch-version 只切 final，不改不可变版本水位。
            assert row.current_version == 2
        assert lease.phase == "local_final_mutation:v1"

    def test_switch_crash_retains_noncanonical_version_alias(
        self,
        harness: _Harness,
    ) -> None:
        """v01.md 不能被静默折叠成 v1 后继续改 final/释放租约。"""

        _seed_switch_version_checkpoint(harness)
        lease = _mark_phase(
            harness,
            _acquire(
                harness,
                source_version=2,
                operation_kind="switch-version",
            ),
            "local_final_mutation:v1",
        )
        final_path = harness.paths.chapter_version_file(
            _CHAPTER_1,
            "final.md",
        )
        final_path.write_text("第二版正文", encoding="utf-8")
        alias_path = harness.paths.chapter_dir(_CHAPTER_1) / "v01.md"
        alias_path.write_text("损坏别名", encoding="utf-8")

        with pytest.raises(
            ChapterVersioningError,
            match="非规范版本文件名",
        ):
            harness.chapter_service.reconcile_operation_leases_on_startup(
                _WORKSPACE_ID,
                apply=False,
            )
        assert final_path.read_text(encoding="utf-8") == "第二版正文"

        with pytest.raises(
            ChapterVersioningError,
            match="非规范版本文件名",
        ):
            harness.chapter_service.reconcile_operation_leases_on_startup(_WORKSPACE_ID)
        assert harness.store.get(_WORKSPACE_ID, _CHAPTER_1) == lease
        assert final_path.read_text(encoding="utf-8") == "第二版正文"
        assert alias_path.read_text(encoding="utf-8") == "损坏别名"

    @pytest.mark.parametrize("invalid_final", ["corrupt_file", "directory"])
    def test_switch_crash_retains_non_db_or_unreadable_final(
        self,
        harness: _Harness,
        invalid_final: str,
    ) -> None:
        """final 不是任一 DB/盘版本时不猜目标，也不删除现场租约。"""

        _seed_switch_version_checkpoint(harness)
        lease = _mark_phase(
            harness,
            _acquire(
                harness,
                source_version=2,
                operation_kind="switch-version",
            ),
            "local_final_mutation:v1",
        )
        final_path = harness.paths.chapter_version_file(
            _CHAPTER_1,
            "final.md",
        )
        if invalid_final == "corrupt_file":
            final_path.write_text("不属于 DB 的正文", encoding="utf-8")
        else:
            final_path.mkdir()

        released, retained = (
            harness.chapter_service.reconcile_operation_leases_on_startup(_WORKSPACE_ID)
        )

        assert released == []
        assert retained == [_CHAPTER_1]
        assert harness.store.get(_WORKSPACE_ID, _CHAPTER_1) == lease

    def test_manual_edit_file_only_crash_reconstructs_db_and_final(
        self, harness: _Harness
    ) -> None:
        _seed_chapter(
            harness,
            status="human_intervention",
            current_version=1,
        )
        lease = _acquire(
            harness,
            source_version=1,
            operation_kind="update-content-manual",
        )
        chapter_dir = harness.paths.chapter_dir(_CHAPTER_1)
        chapter_dir.mkdir(parents=True, exist_ok=True)
        target_path = harness.paths.chapter_version_file(_CHAPTER_1, "v2.md")
        target_path.write_text("人工修复后的正文", encoding="utf-8")

        released, retained = (
            harness.chapter_service.reconcile_operation_leases_on_startup(_WORKSPACE_ID)
        )

        assert released == [_CHAPTER_1]
        assert retained == []
        assert harness.store.get(_WORKSPACE_ID, _CHAPTER_1) is None
        assert (
            harness.paths.chapter_version_file(_CHAPTER_1, "final.md").read_text(
                encoding="utf-8"
            )
            == "人工修复后的正文"
        )
        with harness.session_factory(_WORKSPACE_ID) as session:
            row = session.get(ChapterORM, _CHAPTER_1)
            version = session.get(ChapterVersionORM, (_CHAPTER_1, 2))
            assert row is not None
            assert row.current_version == 2
            assert version is not None
            assert version.content == "人工修复后的正文"
            assert version.creation_type == "manual_edit"
        assert lease.source_version == 1

    def test_manual_edit_disk_db_mismatch_retains_lease(
        self, harness: _Harness
    ) -> None:
        _seed_chapter(
            harness,
            status="human_intervention",
            current_version=1,
        )
        lease = _acquire(
            harness,
            source_version=1,
            operation_kind="update-content-manual",
        )
        now = datetime.now(UTC)
        with harness.session_factory(_WORKSPACE_ID) as session:
            session.add(
                ChapterVersionORM(
                    chapter_id=_CHAPTER_1,
                    version_id=2,
                    content="数据库中的另一份正文",
                    word_count=10,
                    created_at=now,
                    creation_type="manual_edit",
                    model_used="manual",
                    review_score=None,
                    notes="",
                    style_fingerprint=None,
                )
            )
            session.commit()
        chapter_dir = harness.paths.chapter_dir(_CHAPTER_1)
        chapter_dir.mkdir(parents=True, exist_ok=True)
        harness.paths.chapter_version_file(_CHAPTER_1, "v2.md").write_text(
            "磁盘中的冲突正文",
            encoding="utf-8",
        )

        released, retained = (
            harness.chapter_service.reconcile_operation_leases_on_startup(_WORKSPACE_ID)
        )

        assert released == []
        assert retained == [_CHAPTER_1]
        assert harness.store.get(_WORKSPACE_ID, _CHAPTER_1) == lease
        assert not harness.paths.chapter_version_file(_CHAPTER_1, "final.md").exists()

    def test_rewind_committed_crash_finishes_local_recovery(
        self, harness: _Harness
    ) -> None:
        _seed_chapter(harness, status="drafting", current_version=1)
        lease = _acquire(
            harness,
            source_version=1,
            operation_kind="rewind",
        )
        lease = _mark_phase(harness, lease, "source_mutation_committed")

        vector_store = _ControllableVectorStore()
        recovery_service = ChapterService(
            workspace_service=harness.workspace_service,
            client=MockLLMClient(),
            model="mock-model",
            session_factory=harness.session_factory,
            vector_store_factory=lambda _workspace_id: vector_store,
            operation_lease_store=harness.store,
        )

        released, retained = recovery_service.reconcile_operation_leases_on_startup(
            _WORKSPACE_ID
        )

        assert released == [_CHAPTER_1]
        assert retained == []
        assert harness.store.get(_WORKSPACE_ID, _CHAPTER_1) is None
        assert vector_store.calls == [("memories", _CHAPTER_1)]

    def test_rewind_chroma_failure_retains_lease_until_startup_recovery(
        self, harness: _Harness
    ) -> None:
        _seed_chapter(harness, status="approved", current_version=1)
        vector_store = _ControllableVectorStore(fail=True)
        rewind_service = ChapterRewindService(
            workspace_service=harness.workspace_service,
            session_factory=harness.session_factory,
            vector_store_factory=lambda _workspace_id: vector_store,
            operation_lease_store=harness.store,
        )

        with pytest.raises(
            ChapterRewindServiceError,
            match="已保留 source_mutation_committed",
        ):
            rewind_service.rewind(_WORKSPACE_ID, _CHAPTER_1)

        lease = harness.store.get(_WORKSPACE_ID, _CHAPTER_1)
        assert lease is not None
        assert lease.operation_kind == "rewind"
        assert lease.phase == "source_mutation_committed"
        with harness.session_factory(_WORKSPACE_ID) as session:
            row = session.get(ChapterORM, _CHAPTER_1)
            assert row is not None
            assert row.status == "drafting"

        vector_store.fail = False
        recovery_service = ChapterService(
            workspace_service=harness.workspace_service,
            client=MockLLMClient(),
            model="mock-model",
            session_factory=harness.session_factory,
            vector_store_factory=lambda _workspace_id: vector_store,
            operation_lease_store=harness.store,
        )
        released, retained = recovery_service.reconcile_operation_leases_on_startup(
            _WORKSPACE_ID
        )

        assert released == [_CHAPTER_1]
        assert retained == []
        assert harness.store.get(_WORKSPACE_ID, _CHAPTER_1) is None
        assert vector_store.calls == [
            ("memories", _CHAPTER_1),
            ("memories", _CHAPTER_1),
        ]

    def test_paused_run_rejects_rewind_of_non_current_chapter(
        self, harness: _Harness
    ) -> None:
        _seed_chapter(harness, chapter_id=_CHAPTER_2, status="reviewing")
        run = _start_pipeline(harness)
        _set_run_checkpoint(
            harness,
            run.run_id,
            state="paused",
            current_chapter=_CHAPTER_1,
        )
        service = ChapterRewindService(
            workspace_service=harness.workspace_service,
            session_factory=harness.session_factory,
            operation_lease_store=harness.store,
        )

        with pytest.raises(
            ChapterRewindServiceError,
            match="只允许修复尚未完成的 current chapter",
        ):
            service.rewind(_WORKSPACE_ID, _CHAPTER_2)

        assert harness.store.get(_WORKSPACE_ID, _CHAPTER_2) is None
        with harness.session_factory(_WORKSPACE_ID) as session:
            row = session.get(ChapterORM, _CHAPTER_2)
            assert row is not None
            assert row.status == "reviewing"

    def test_paused_current_completed_rewind_reopens_same_run(
        self,
        harness: _Harness,
        monkeypatch: pytest.MonkeyPatch,
    ) -> None:
        """false-pass current 可正式 rewind，并原子撤回原 run completed 进度。"""

        _seed_chapter(harness, status="approved", current_version=1)
        run = _start_pipeline(harness, cap=23)
        _set_run_checkpoint(
            harness,
            run.run_id,
            state="paused",
            current_chapter=_CHAPTER_1,
        )
        with harness.session_factory(_WORKSPACE_ID) as session:
            run_row = session.get(PipelineRunORM, run.run_id)
            assert run_row is not None
            run_row.completed_chapter_ids = [_CHAPTER_1]
            session.commit()

        vector_store = _ControllableVectorStore()
        rewind_service = ChapterRewindService(
            workspace_service=harness.workspace_service,
            session_factory=harness.session_factory,
            vector_store_factory=lambda _workspace_id: vector_store,
            operation_lease_store=harness.store,
        )

        result = rewind_service.rewind(_WORKSPACE_ID, _CHAPTER_1)

        assert result.from_status == "approved"
        assert result.to_status == "drafting"
        assert harness.store.get(_WORKSPACE_ID, _CHAPTER_1) is None
        with harness.session_factory(_WORKSPACE_ID) as session:
            chapter = session.get(ChapterORM, _CHAPTER_1)
            run_row = session.get(PipelineRunORM, run.run_id)
            assert chapter is not None
            assert chapter.status == "drafting"
            assert run_row is not None
            assert run_row.state == "paused"
            assert run_row.current_chapter == _CHAPTER_1
            assert run_row.completed_chapter_ids == []

        _reserve_ui(
            harness,
            monkeypatch,
            chapter_id=_CHAPTER_1,
            task_id="task-ui-after-rewind",
        )
        lease = harness.store.get(_WORKSPACE_ID, _CHAPTER_1)
        assert lease is not None
        assert lease.run_id == run.run_id


def test_reset_stale_skips_chapter_with_retained_lease(harness: _Harness) -> None:
    _seed_chapter(harness, status="reviewing", current_version=0)
    lease = _mark_phase(
        harness,
        _acquire(harness, source_version=0),
        "provider_capable_pipeline",
    )
    service = ChapterRewindService(
        workspace_service=harness.workspace_service,
        session_factory=harness.session_factory,
        operation_lease_store=harness.store,
    )

    result = service.reset_stale(_WORKSPACE_ID)

    assert result.reset_count == 0
    assert result.skipped_reason == "retained_operation_lease"
    assert harness.store.get(_WORKSPACE_ID, _CHAPTER_1) == lease
    with harness.session_factory(_WORKSPACE_ID) as session:
        row = session.get(ChapterORM, _CHAPTER_1)
        assert row is not None
        assert row.status == "reviewing"


@pytest.mark.parametrize("phase", ["reserved_before_provider", "continuity_ledger_may_call_provider"])
@pytest.mark.parametrize("damage", [None, "started", "failed", "wrong_agent", "wrong_task", "version", "missing_receipt", "old_receipt", "unknown_receipt", "source_guard", "summary_refresh"])
def test_relation_checkpoint_lease_requires_same_known_response_and_safe_source(harness, monkeypatch, phase, damage):
    import json
    from src.services.core_state_service import CoreStateService
    from src.services import relation_registration_recovery as recovery
    _seed_exact_ready_checkpoint(harness)
    with harness.session_factory(_WORKSPACE_ID) as session:
        chapter = session.get(ChapterORM, _CHAPTER_1)
        chapter.ready_for_next_chapter_at = None
        chapter.core_state_commit_status = "pending"
        chapter.core_state_commit_details = {"conflicts": ["pending relation"]}
        chapter.memory_enrichment_status = "pending"
        session.commit()
    lease = _acquire(harness, operation_kind="checkpoint-resume", source_version=1)
    if damage == "version":
        with harness.session_factory(_WORKSPACE_ID) as session:
            session.get(ChapterORM, _CHAPTER_1).current_version = 2
            session.commit()
    if phase != "reserved_before_provider":
        lease = _mark_phase(harness, lease, phase)
    payload = {"schema_version": 2, "chapter_id": _CHAPTER_1, "transient_eligible_keys": ["object:herb:possession"]}
    def source_guard(*args, **kwargs):
        if damage == "source_guard":
            raise ValueError("unknown response or source drift")
        return damage == "summary_refresh"
    monkeypatch.setattr(CoreStateService, "require_pending_transition_candidate_recovery_safe", source_guard)
    monkeypatch.setattr(recovery, "recovery_payload", lambda **kwargs: payload)
    prepared = lease.created_at - timedelta(seconds=1) if damage == "old_receipt" else datetime.now(UTC)
    if damage != "missing_receipt":
        recovery.decision_path(paths=harness.paths, payload=payload).write_text(json.dumps({
            "source": payload, "status": "request_prepared" if damage == "unknown_receipt" else "responded",
            "prepared_at": prepared.isoformat(), "raw_response": "source guard separately validates"}))
    _insert_attempt(harness, lease, status=damage if damage in {"started", "failed"} else "succeeded",
        agent_name="writer" if damage == "wrong_agent" else "chapter_card_generator",
        task_id="different-task" if damage == "wrong_task" else lease.task_id)
    if damage == "failed" and phase == "reserved_before_provider":
        # 既有“尚无物化且调用明确失败”路径独立可释放；本新关系路径仍拒绝。
        assert not harness.chapter_service._relation_recovery_lease_is_safe(lease, paths=harness.paths)
        return
    released, retained = harness.chapter_service.reconcile_operation_leases_on_startup(_WORKSPACE_ID)
    assert released == ([_CHAPTER_1] if damage is None else [])
    assert retained == ([] if damage is None else [_CHAPTER_1])
