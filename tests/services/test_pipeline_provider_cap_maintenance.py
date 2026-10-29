"""暂停 run 的 Provider cap 离线维护服务测试。"""

from __future__ import annotations

import sqlite3
import zipfile
from dataclasses import dataclass
from datetime import UTC, datetime, timedelta
from pathlib import Path
from typing import Any

import pytest
from sqlalchemy import create_engine, select
from sqlalchemy.orm import Session

from src.core.db import Base
from src.core.db.models import (
    AttemptLedgerORM,
    ChapterOperationLeaseORM,
    ChapterORM,
    ChapterVersionORM,
    PipelineRunORM,
    WorkspaceORM,
)
from src.core.workspace.workspace_paths import WorkspacePaths
from src.services.backup_service import BackupService, BackupServiceError
from src.services.pipeline_provider_cap_maintenance import (
    PipelineProviderCapMaintenanceError,
    PipelineProviderCapMaintenanceService,
)
from src.services.workspace_service import WorkspaceService
from src.version import ENGINE_VERSION

_WORKSPACE_ID = "ws-cap-maintenance"
_RUN_ID = "run_cap_maintenance"
_CURRENT_CHAPTER = "ch-0006"


@dataclass(frozen=True)
class _Environment:
    workspace_service: WorkspaceService
    backup_service: BackupService
    service: PipelineProviderCapMaintenanceService
    db_path: Path
    engine: Any
    cap: int


@pytest.fixture
def environment(tmp_path: Path) -> _Environment:
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
        display_name="cap maintenance",
        genre="scifi",
        premise="checkpoint",
        activate=False,
    )
    paths = WorkspacePaths(
        _WORKSPACE_ID,
        workspaces_root=workspaces_root,
    )
    engine = create_engine(f"sqlite:///{paths.db_path}")
    Base.metadata.create_all(engine)
    cap = 3
    now = datetime.now(UTC)
    with Session(engine) as session:
        session.add(
            WorkspaceORM(
                workspace_id=_WORKSPACE_ID,
                display_name="cap maintenance",
                genre="scifi",
                premise="checkpoint",
                created_at=now,
                last_active_at=now,
                engine_version=ENGINE_VERSION,
                status="active",
                total_chapters=6,
                completed_chapters=5,
                config_overrides={},
            )
        )
        session.add(
            ChapterORM(
                chapter_id=_CURRENT_CHAPTER,
                workspace_id=_WORKSPACE_ID,
                current_version=1,
                status="approved",
                rewrite_count=0,
                revise_count=0,
                created_at=now,
                approved_at=now,
                total_token_usage={},
                total_cost_usd=0.0,
                ready_for_next_chapter_at=None,
                memory_enrichment_status="pending",
                memory_enriched_at=None,
                memory_enrichment_details=None,
            )
        )
        session.add(
            PipelineRunORM(
                run_id=_RUN_ID,
                workspace_id=_WORKSPACE_ID,
                state="paused",
                scope="range",
                start_chapter_id="ch-0001",
                end_chapter_id=_CURRENT_CHAPTER,
                volume_id=None,
                profile_override=None,
                chapter_ids=[f"ch-{index:04d}" for index in range(1, 7)],
                current_chapter=_CURRENT_CHAPTER,
                completed_chapter_ids=[f"ch-{index:04d}" for index in range(1, 6)],
                pause_reason="continuity_ledger_not_ready",
                error_details=None,
                failure_policy="stop",
                skipped_chapter_ids=[],
                provider_call_cap=cap,
                provider_call_count=cap,
                transport_retry_count=0,
                pipeline_attempt_count=1,
                upstream_generation_attempt_count=0,
                created_at=now,
                updated_at=now,
                completed_at=None,
            )
        )
        for index in range(1, cap + 1):
            succeeded = index < cap
            session.add(
                AttemptLedgerORM(
                    attempt_id=f"att-provider-{index}",
                    workspace_id=_WORKSPACE_ID,
                    run_id=_RUN_ID,
                    task_id=f"task-provider-{index}",
                    chapter_id=("ch-0003" if index == cap else _CURRENT_CHAPTER),
                    attempt_kind="provider",
                    operation_kind="agent_call",
                    logical_call_id=f"call-{index}",
                    agent_name=("plot_thread_merger" if not succeeded else "reviewer"),
                    provider="openai_compatible",
                    model="qwen3.7-max",
                    attempt_number=1,
                    attempt_cap=cap,
                    status="succeeded" if succeeded else "started",
                    version_before=1,
                    version_after=1 if succeeded else None,
                    error_type=None,
                    created_at=now,
                    completed_at=now if succeeded else None,
                )
            )
        session.commit()

    final_path = paths.chapter_dir(_CURRENT_CHAPTER) / "final.md"
    final_path.parent.mkdir(parents=True, exist_ok=True)
    final_path.write_text("第六章正文", encoding="utf-8")

    backup_service = BackupService(
        workspace_service=workspace_service,
        ws_manager=None,
        active_task_checker=lambda _workspace_id: False,
    )
    service = PipelineProviderCapMaintenanceService(
        workspace_service=workspace_service,
        backup_service=backup_service,
        active_task_checker=lambda _workspace_id: False,
    )
    return _Environment(
        workspace_service=workspace_service,
        backup_service=backup_service,
        service=service,
        db_path=paths.db_path,
        engine=engine,
        cap=cap,
    )


def _confirm(environment: _Environment, *, new_cap: int | None = None) -> str:
    target = new_cap if new_cap is not None else environment.cap + 2
    return environment.service.confirmation_token(
        workspace_id=_WORKSPACE_ID,
        run_id=_RUN_ID,
        expected_current_cap=environment.cap,
        new_cap=target,
    )


def _increase(
    environment: _Environment,
    *,
    new_cap: int | None = None,
    confirm: str | None = None,
):
    target = new_cap if new_cap is not None else environment.cap + 2
    return environment.service.increase_cap(
        workspace_id=_WORKSPACE_ID,
        run_id=_RUN_ID,
        expected_current_cap=environment.cap,
        new_cap=target,
        confirm=confirm
        if confirm is not None
        else _confirm(
            environment,
            new_cap=target,
        ),
    )


def _run_cap(environment: _Environment) -> int:
    with Session(environment.engine) as session:
        return int(session.get(PipelineRunORM, _RUN_ID).provider_call_cap)


def _backup_paths(environment: _Environment) -> list[Path]:
    return sorted(
        (
            environment.workspace_service.workspaces_root / _WORKSPACE_ID / "backups"
        ).glob("manual_*.zip")
    )


def _set_chapter_escalated_checkpoint(
    environment: _Environment, *, clear_started: bool = True
) -> Path:
    """把隔离 fixture 改成可由正式 Chapter UI 继续的升级人工 checkpoint。"""

    now = datetime.now(UTC)
    content = "第六章待修正文"
    with Session(environment.engine) as session:
        run = session.get(PipelineRunORM, _RUN_ID)
        chapter = session.get(ChapterORM, _CURRENT_CHAPTER)
        assert run is not None
        assert chapter is not None
        run.pause_reason = "chapter_escalated"
        # 真实 v5 曾 stop 后从产品路径恢复；resume 保留旧 completed_at，但
        # updated_at 会被后续活动推进。该时间戳不是当前 paused run 的终态。
        run.completed_at = now - timedelta(days=1)
        run.updated_at = now
        chapter.status = "human_intervention"
        chapter.approved_at = None
        chapter.escalation_details = {
            "reason": "repeated_hard_constraint_no_progress"
        }
        session.add(
            ChapterVersionORM(
                chapter_id=_CURRENT_CHAPTER,
                version_id=1,
                content=content,
                word_count=len(content),
                created_at=now,
                creation_type="rewrite",
                model_used="qwen3.7-max",
                review_score=8.2,
                notes="",
                style_fingerprint=None,
            )
        )
        if clear_started:
            for attempt in session.scalars(select(AttemptLedgerORM)).all():
                attempt.status = "succeeded"
                attempt.completed_at = now
        session.commit()

    paths = WorkspacePaths(
        _WORKSPACE_ID,
        workspaces_root=environment.workspace_service.workspaces_root,
    )
    final_path = paths.chapter_dir(_CURRENT_CHAPTER) / "final.md"
    final_path.unlink()
    version_path = paths.chapter_dir(_CURRENT_CHAPTER) / "v1.md"
    version_path.write_text(content, encoding="utf-8")
    return version_path


def test_increase_cap_creates_full_backup_and_changes_only_cap(
    environment: _Environment,
    tmp_path: Path,
) -> None:
    with Session(environment.engine) as session:
        before_run = session.get(PipelineRunORM, _RUN_ID)
        before_attempts = session.scalars(
            select(AttemptLedgerORM).order_by(AttemptLedgerORM.attempt_id)
        ).all()
        before_attempt_facts = [
            (
                row.attempt_id,
                row.status,
                row.completed_at,
                row.error_type,
            )
            for row in before_attempts
        ]
        before_state = (
            before_run.state,
            before_run.pause_reason,
            before_run.current_chapter,
            list(before_run.completed_chapter_ids),
            before_run.provider_call_count,
        )

    result = _increase(environment)

    assert result.previous_cap == environment.cap
    assert result.new_cap == environment.cap + 2
    assert result.provider_call_count == environment.cap
    assert result.provider_attempt_status_counts == {
        "started": 1,
        "succeeded": 2,
    }
    assert result.restart_required is True
    assert Path(result.backup_file_path).is_file()
    assert result.backup_size_bytes > 0

    backup_db = tmp_path / "backup-novel.db"
    with zipfile.ZipFile(result.backup_file_path) as archive:
        assert "data/novel.db" in archive.namelist()
        backup_db.write_bytes(archive.read("data/novel.db"))
    with sqlite3.connect(backup_db) as connection:
        backup_cap = connection.execute(
            "SELECT provider_call_cap FROM pipeline_runs WHERE run_id = ?",
            (_RUN_ID,),
        ).fetchone()[0]
    assert backup_cap == environment.cap

    with Session(environment.engine) as session:
        after_run = session.get(PipelineRunORM, _RUN_ID)
        assert after_run.provider_call_cap == environment.cap + 2
        assert (
            after_run.state,
            after_run.pause_reason,
            after_run.current_chapter,
            list(after_run.completed_chapter_ids),
            after_run.provider_call_count,
        ) == before_state
        after_attempt_facts = [
            (
                row.attempt_id,
                row.status,
                row.completed_at,
                row.error_type,
            )
            for row in session.scalars(
                select(AttemptLedgerORM).order_by(AttemptLedgerORM.attempt_id)
            ).all()
        ]
        assert after_attempt_facts == before_attempt_facts


def test_chapter_escalated_checkpoint_creates_backup_and_changes_only_cap(
    environment: _Environment,
) -> None:
    _set_chapter_escalated_checkpoint(environment)

    result = _increase(environment)

    assert result.pause_reason == "chapter_escalated"
    assert result.state == "paused"
    assert result.current_chapter == _CURRENT_CHAPTER
    assert result.provider_attempt_status_counts == {"succeeded": environment.cap}
    assert _run_cap(environment) == environment.cap + 2
    assert len(_backup_paths(environment)) == 1
    with Session(environment.engine) as session:
        chapter = session.get(ChapterORM, _CURRENT_CHAPTER)
        assert chapter is not None
        assert chapter.status == "human_intervention"
        assert chapter.current_version == 1
        assert chapter.ready_for_next_chapter_at is None


def test_chapter_escalated_started_attempt_refuses_before_backup(
    environment: _Environment,
) -> None:
    _set_chapter_escalated_checkpoint(environment, clear_started=False)

    with pytest.raises(
        PipelineProviderCapMaintenanceError,
        match="仍有 started Provider attempt",
    ):
        _increase(environment)

    assert _run_cap(environment) == environment.cap
    assert _backup_paths(environment) == []


def test_chapter_escalated_disk_drift_refuses_before_backup(
    environment: _Environment,
) -> None:
    version_path = _set_chapter_escalated_checkpoint(environment)
    version_path.write_text("与数据库不同", encoding="utf-8")

    with pytest.raises(
        PipelineProviderCapMaintenanceError,
        match="DB/磁盘正文不一致",
    ):
        _increase(environment)

    assert _run_cap(environment) == environment.cap
    assert _backup_paths(environment) == []


def test_chapter_escalated_missing_db_version_refuses_before_backup(
    environment: _Environment,
) -> None:
    _set_chapter_escalated_checkpoint(environment)
    with environment.engine.begin() as connection:
        connection.exec_driver_sql(
            "DELETE FROM chapter_versions WHERE chapter_id = ? AND version_id = 1",
            (_CURRENT_CHAPTER,),
        )

    with pytest.raises(
        PipelineProviderCapMaintenanceError,
        match="缺少当前不可变正文版本",
    ):
        _increase(environment)

    assert _run_cap(environment) == environment.cap
    assert _backup_paths(environment) == []


def test_chapter_escalated_missing_reason_refuses_before_backup(
    environment: _Environment,
) -> None:
    _set_chapter_escalated_checkpoint(environment)
    with environment.engine.begin() as connection:
        connection.exec_driver_sql(
            "UPDATE chapters SET escalation_details = '{}' WHERE chapter_id = ?",
            (_CURRENT_CHAPTER,),
        )

    with pytest.raises(
        PipelineProviderCapMaintenanceError,
        match="缺少升级原因",
    ):
        _increase(environment)

    assert _run_cap(environment) == environment.cap
    assert _backup_paths(environment) == []


def test_chapter_escalated_final_file_refuses_before_backup(
    environment: _Environment,
) -> None:
    version_path = _set_chapter_escalated_checkpoint(environment)
    (version_path.parent / "final.md").write_text("不应存在", encoding="utf-8")

    with pytest.raises(
        PipelineProviderCapMaintenanceError,
        match="已存在 final.md",
    ):
        _increase(environment)

    assert _run_cap(environment) == environment.cap
    assert _backup_paths(environment) == []


def test_chapter_escalated_completed_at_without_later_recovery_refuses(
    environment: _Environment,
) -> None:
    _set_chapter_escalated_checkpoint(environment)
    now = datetime.now(UTC)
    with Session(environment.engine) as session:
        run = session.get(PipelineRunORM, _RUN_ID)
        assert run is not None
        run.completed_at = now
        run.updated_at = now
        session.commit()

    with pytest.raises(
        PipelineProviderCapMaintenanceError,
        match="completed_at 没有更晚恢复证据",
    ):
        _increase(environment)

    assert _run_cap(environment) == environment.cap
    assert _backup_paths(environment) == []


@pytest.mark.parametrize(
    ("field", "value", "message"),
    [
        ("state", "running", "running_pipeline"),
        ("pause_reason", "user_request", "continuity_ledger_not_ready"),
        ("provider_call_count", 2, "尚未精确耗尽"),
    ],
)
def test_run_checkpoint_drift_refuses_before_backup(
    environment: _Environment,
    field: str,
    value: Any,
    message: str,
) -> None:
    with environment.engine.begin() as connection:
        connection.exec_driver_sql(
            f"UPDATE pipeline_runs SET {field} = ? WHERE run_id = ?",
            (value, _RUN_ID),
        )

    with pytest.raises(PipelineProviderCapMaintenanceError, match=message):
        _increase(environment)

    assert _run_cap(environment) == environment.cap
    assert _backup_paths(environment) == []


@pytest.mark.parametrize(
    ("field", "value", "message"),
    [
        ("status", "reviewing", "必须 approved"),
        ("memory_enrichment_status", "running", "不是 pending"),
    ],
)
def test_chapter_checkpoint_drift_refuses_before_backup(
    environment: _Environment,
    field: str,
    value: Any,
    message: str,
) -> None:
    with environment.engine.begin() as connection:
        connection.exec_driver_sql(
            f"UPDATE chapters SET {field} = ? WHERE chapter_id = ?",
            (value, _CURRENT_CHAPTER),
        )

    with pytest.raises(PipelineProviderCapMaintenanceError, match=message):
        _increase(environment)

    assert _run_cap(environment) == environment.cap
    assert _backup_paths(environment) == []


def test_attempt_ledger_projection_mismatch_refuses_before_backup(
    environment: _Environment,
) -> None:
    with environment.engine.begin() as connection:
        connection.exec_driver_sql(
            "DELETE FROM attempt_ledger WHERE attempt_id = ?",
            ("att-provider-1",),
        )

    with pytest.raises(
        PipelineProviderCapMaintenanceError,
        match="权威账本与当前 cap 不一致",
    ):
        _increase(environment)

    assert _run_cap(environment) == environment.cap
    assert _backup_paths(environment) == []


def test_wrong_confirm_and_oversized_increase_are_zero_write(
    environment: _Environment,
) -> None:
    with pytest.raises(
        PipelineProviderCapMaintenanceError,
        match="--confirm",
    ):
        _increase(environment, confirm="wrong")
    with pytest.raises(
        PipelineProviderCapMaintenanceError,
        match="单次最多",
    ):
        _increase(environment, new_cap=environment.cap + 21)

    assert _run_cap(environment) == environment.cap
    assert _backup_paths(environment) == []


def test_active_task_and_operation_lease_block_before_backup(
    environment: _Environment,
) -> None:
    active_service = PipelineProviderCapMaintenanceService(
        workspace_service=environment.workspace_service,
        backup_service=environment.backup_service,
        active_task_checker=lambda _workspace_id: True,
    )
    with pytest.raises(
        PipelineProviderCapMaintenanceError,
        match="active_task",
    ):
        active_service.increase_cap(
            workspace_id=_WORKSPACE_ID,
            run_id=_RUN_ID,
            expected_current_cap=environment.cap,
            new_cap=environment.cap + 2,
            confirm=_confirm(environment),
        )

    now = datetime.now(UTC)
    with Session(environment.engine) as session:
        session.add(
            ChapterOperationLeaseORM(
                workspace_id=_WORKSPACE_ID,
                chapter_id=_CURRENT_CHAPTER,
                lease_id="lease-cap-maintenance",
                operation_kind="pipeline_execute",
                source_version=1,
                task_id="task-cap-maintenance",
                run_id=_RUN_ID,
                phase="reserved",
                created_at=now,
                updated_at=now,
            )
        )
        session.commit()
    with pytest.raises(
        PipelineProviderCapMaintenanceError,
        match="active_chapter_operation_lease",
    ):
        _increase(environment)

    assert _run_cap(environment) == environment.cap
    assert _backup_paths(environment) == []


def test_backup_failure_leaves_cap_unchanged(
    environment: _Environment,
) -> None:
    class _FailingBackupService:
        def create_manual_backup(self, workspace_id: str) -> None:
            raise BackupServiceError(f"backup failed: {workspace_id}")

    service = PipelineProviderCapMaintenanceService(
        workspace_service=environment.workspace_service,
        backup_service=_FailingBackupService(),  # type: ignore[arg-type]
        active_task_checker=lambda _workspace_id: False,
    )
    with pytest.raises(
        PipelineProviderCapMaintenanceError,
        match="backup failed",
    ):
        service.increase_cap(
            workspace_id=_WORKSPACE_ID,
            run_id=_RUN_ID,
            expected_current_cap=environment.cap,
            new_cap=environment.cap + 2,
            confirm=_confirm(environment),
        )

    assert _run_cap(environment) == environment.cap
