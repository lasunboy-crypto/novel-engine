"""R8 migrate/restore 写前静默性硬门回归。"""

from __future__ import annotations

import json
import sqlite3
import subprocess
import sys
import textwrap
import threading
from pathlib import Path

import pytest

from src.core.workspace.workspace_paths import WorkspacePaths
from src.services.backup_service import BackupService, BackupServiceError
from src.services.workspace_operation_guard import (
    WorkspaceOperationConflictError,
    acquire_runtime_owner_lock,
    ensure_workspace_quiescent,
    workspace_operation_lock,
)
from src.services.memory_enrichment_worker_guard import (
    serialize_memory_enrichment_worker,
)
from src.services.workspace_service import WorkspaceService
from src.services.workspace_version_service import (
    WorkspaceVersionService,
    WorkspaceVersionServiceError,
)


@pytest.fixture
def workspace_service(tmp_path: Path) -> WorkspaceService:
    service = WorkspaceService(
        workspaces_root=tmp_path / "workspaces",
        archives_root=tmp_path / "archives",
    )
    service.create(
        "ws-maintenance-guard",
        display_name="maintenance guard",
        genre="scifi",
        premise="test",
        activate=False,
    )
    return service


def _db_path(workspace_service: WorkspaceService) -> Path:
    return WorkspacePaths(
        "ws-maintenance-guard",
        workspaces_root=workspace_service.workspaces_root,
    ).db_path


def _rewrite_old_schema(workspace_service: WorkspaceService) -> None:
    meta_path = workspace_service.workspaces_root / "ws-maintenance-guard" / "meta.json"
    payload = json.loads(meta_path.read_text(encoding="utf-8"))
    payload["schema_version"] = "0029_structured_rewrite_constraint_results"
    meta_path.write_text(
        json.dumps(payload, ensure_ascii=False, indent=2),
        encoding="utf-8",
    )


def _create_pipeline_state(db_path: Path, state: str) -> None:
    db_path.parent.mkdir(parents=True, exist_ok=True)
    connection = sqlite3.connect(db_path)
    try:
        connection.execute(
            "CREATE TABLE pipeline_runs (run_id TEXT PRIMARY KEY, state TEXT)"
        )
        connection.execute(
            "INSERT INTO pipeline_runs(run_id, state) VALUES (?, ?)",
            ("run-maintenance", state),
        )
        connection.commit()
    finally:
        connection.close()


def _create_operation_lease(db_path: Path) -> None:
    db_path.parent.mkdir(parents=True, exist_ok=True)
    connection = sqlite3.connect(db_path)
    try:
        connection.execute("CREATE TABLE chapter_operation_leases (lease_id TEXT)")
        connection.execute(
            "INSERT INTO chapter_operation_leases(lease_id) VALUES ('lease-active')"
        )
        connection.commit()
    finally:
        connection.close()


def test_quiescence_classifies_running_but_not_paused_as_live_worker(
    workspace_service: WorkspaceService,
) -> None:
    db_path = _db_path(workspace_service)
    _create_pipeline_state(db_path, "paused")

    ensure_workspace_quiescent(
        workspace_id="ws-maintenance-guard",
        db_path=db_path,
        active_task_checker=lambda _workspace_id: False,
    )

    connection = sqlite3.connect(db_path)
    try:
        connection.execute("UPDATE pipeline_runs SET state = 'running'")
        connection.commit()
    finally:
        connection.close()
    with pytest.raises(
        WorkspaceOperationConflictError,
        match="running_pipeline:run-maintenance",
    ):
        ensure_workspace_quiescent(
            workspace_id="ws-maintenance-guard",
            db_path=db_path,
            active_task_checker=lambda _workspace_id: False,
        )

    # CLI 不注入 checker，表示当前进程没有产品 Worker；崩溃遗留的 running
    # 行不能让旧 schema 陷入“必须先恢复、但恢复又必须先迁移”的死锁。
    ensure_workspace_quiescent(
        workspace_id="ws-maintenance-guard",
        db_path=db_path,
        active_task_checker=None,
    )


def test_migrate_rejects_active_task_before_backup_or_database_write(
    workspace_service: WorkspaceService,
) -> None:
    workspace_id = "ws-maintenance-guard"
    _rewrite_old_schema(workspace_service)
    backup_service = BackupService(workspace_service=workspace_service)
    service = WorkspaceVersionService(
        workspace_service=workspace_service,
        backup_service=backup_service,
        active_task_checker=lambda _workspace_id: True,
    )
    meta_path = workspace_service.workspaces_root / workspace_id / "meta.json"
    before_meta = meta_path.read_bytes()
    backups_dir = workspace_service.workspaces_root / workspace_id / "backups"
    before_backups = list(backups_dir.glob("*.zip"))

    with pytest.raises(WorkspaceVersionServiceError, match="active_task"):
        service.migrate(workspace_id, workspace_id)

    assert meta_path.read_bytes() == before_meta
    assert list(backups_dir.glob("*.zip")) == before_backups
    assert not _db_path(workspace_service).exists()


def test_migrate_rejects_running_pipeline_before_preflight_or_backup(
    workspace_service: WorkspaceService,
) -> None:
    workspace_id = "ws-maintenance-guard"
    _rewrite_old_schema(workspace_service)
    db_path = _db_path(workspace_service)
    _create_pipeline_state(db_path, "running")
    before_db = db_path.read_bytes()
    service = WorkspaceVersionService(
        workspace_service=workspace_service,
        backup_service=BackupService(workspace_service=workspace_service),
        active_task_checker=lambda _workspace_id: False,
    )

    with pytest.raises(WorkspaceVersionServiceError, match="running_pipeline"):
        service.migrate(workspace_id, workspace_id)

    assert db_path.read_bytes() == before_db
    assert not list(
        (workspace_service.workspaces_root / workspace_id / "backups").glob("*.zip")
    )


def test_restore_rejects_active_task_before_pre_restore_or_overwrite(
    workspace_service: WorkspaceService,
) -> None:
    workspace_id = "ws-maintenance-guard"
    unguarded = BackupService(workspace_service=workspace_service)
    backup = unguarded.create_manual_backup(workspace_id)
    marker = workspace_service.workspaces_root / workspace_id / "keep.txt"
    marker.write_text("do not overwrite", encoding="utf-8")
    backups_dir = workspace_service.workspaces_root / workspace_id / "backups"
    before_backups = {path.name for path in backups_dir.glob("*.zip")}
    guarded = BackupService(
        workspace_service=workspace_service,
        active_task_checker=lambda _workspace_id: True,
    )

    with pytest.raises(BackupServiceError, match="active_task"):
        guarded.restore_from_backup(workspace_id, backup.backup_id)

    assert marker.read_text(encoding="utf-8") == "do not overwrite"
    assert {path.name for path in backups_dir.glob("*.zip")} == before_backups


def test_restore_rejects_operation_lease_before_pre_restore_or_overwrite(
    workspace_service: WorkspaceService,
) -> None:
    workspace_id = "ws-maintenance-guard"
    unguarded = BackupService(workspace_service=workspace_service)
    backup = unguarded.create_manual_backup(workspace_id)
    db_path = _db_path(workspace_service)
    _create_operation_lease(db_path)
    before_db = db_path.read_bytes()
    backups_dir = workspace_service.workspaces_root / workspace_id / "backups"
    before_backups = {path.name for path in backups_dir.glob("*.zip")}
    guarded = BackupService(
        workspace_service=workspace_service,
        active_task_checker=lambda _workspace_id: False,
    )

    with pytest.raises(
        BackupServiceError,
        match="active_chapter_operation_lease:lease-active",
    ):
        guarded.restore_from_backup(workspace_id, backup.backup_id)

    assert db_path.read_bytes() == before_db
    assert {path.name for path in backups_dir.glob("*.zip")} == before_backups


def test_offline_restore_still_rejects_persisted_running_pipeline(
    workspace_service: WorkspaceService,
) -> None:
    workspace_id = "ws-maintenance-guard"
    service = BackupService(workspace_service=workspace_service)
    backup = service.create_manual_backup(workspace_id)
    db_path = _db_path(workspace_service)
    _create_pipeline_state(db_path, "running")
    before_db = db_path.read_bytes()

    with pytest.raises(BackupServiceError, match="running_pipeline"):
        service.restore_from_backup(workspace_id, backup.backup_id)

    assert db_path.read_bytes() == before_db


def test_runtime_owner_rejects_second_backend_in_same_process(
    workspace_service: WorkspaceService,
) -> None:
    owner = acquire_runtime_owner_lock(workspace_service.workspaces_root)
    try:
        with pytest.raises(
            WorkspaceOperationConflictError,
            match="runtime_owner_already_held_in_process",
        ):
            acquire_runtime_owner_lock(workspace_service.workspaces_root)
    finally:
        owner.close()


def test_runtime_owner_blocks_cross_process_offline_maintenance(
    workspace_service: WorkspaceService,
) -> None:
    owner = acquire_runtime_owner_lock(workspace_service.workspaces_root)
    try:
        program = textwrap.dedent(
            """
            import sys
            from pathlib import Path
            from src.services.workspace_operation_guard import (
                WorkspaceOperationConflictError,
                workspace_operation_lock,
            )

            try:
                with workspace_operation_lock(Path(sys.argv[1]), "ws-child"):
                    print("unexpectedly-acquired")
            except WorkspaceOperationConflictError as exc:
                print(exc.reason)
            """
        )
        result = subprocess.run(
            [sys.executable, "-c", program, str(workspace_service.workspaces_root)],
            check=True,
            capture_output=True,
            text=True,
        )
    finally:
        owner.close()

    assert result.stdout.strip() == "runtime_owner_active"

    # owner 释放后，同一离线维护入口应恢复可用。
    result_after_release = subprocess.run(
        [sys.executable, "-c", program, str(workspace_service.workspaces_root)],
        check=True,
        capture_output=True,
        text=True,
    )
    assert result_after_release.stdout.strip() == "unexpectedly-acquired"


def test_maintenance_waits_for_memory_worker_before_any_write(
    workspace_service: WorkspaceService,
) -> None:
    root = workspace_service.workspaces_root
    workspace_id = "ws-maintenance-guard"
    worker_holds_lock = threading.Event()
    release_worker = threading.Event()
    maintenance_started = threading.Event()
    maintenance_entered = threading.Event()
    marker = root / workspace_id / "maintenance-write.txt"

    def _memory_worker() -> None:
        with serialize_memory_enrichment_worker(root, workspace_id):
            worker_holds_lock.set()
            assert release_worker.wait(timeout=5)

    def _maintenance() -> None:
        maintenance_started.set()
        with workspace_operation_lock(root, workspace_id):
            marker.write_text("entered", encoding="utf-8")
            maintenance_entered.set()

    worker = threading.Thread(target=_memory_worker)
    worker.start()
    assert worker_holds_lock.wait(timeout=5)
    maintenance = threading.Thread(target=_maintenance)
    maintenance.start()
    assert maintenance_started.wait(timeout=5)
    assert not maintenance_entered.wait(timeout=0.2)
    assert not marker.exists()

    release_worker.set()
    worker.join(timeout=5)
    maintenance.join(timeout=5)
    assert not worker.is_alive()
    assert not maintenance.is_alive()
    assert maintenance_entered.is_set()
    assert marker.read_text(encoding="utf-8") == "entered"
