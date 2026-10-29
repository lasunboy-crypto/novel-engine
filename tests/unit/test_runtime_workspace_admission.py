"""R8 普通 API session 的 workspace runtime admission 回归。"""

from __future__ import annotations

import json
import sqlite3
import subprocess
import sys
from collections.abc import Iterator
from datetime import UTC, datetime
from pathlib import Path

import pytest
from sqlalchemy import create_engine, inspect
from sqlalchemy.orm import Session

from src.api.deps import (
    _make_session_factory,
    configure_runtime_workspace_admission,
    get_workspace_version_service,
    reset_runtime_workspace_admission,
)
from src.api import deps as deps_module
from src.core.db import Base, WorkspaceORM
from src.core.workspace.workspace_paths import WorkspacePaths
from src.services.workspace_service import WorkspaceService
from src.services.workspace_version_service import WorkspaceVersionServiceError
from src.services.workspace_version_service import (
    WorkspaceStartupRecoveryAdmissionError,
    admit_workspaces_for_startup_recovery,
)
from src.version import ENGINE_VERSION


@pytest.fixture(autouse=True)
def _clean_runtime_admission() -> Iterator[None]:
    reset_runtime_workspace_admission()
    yield
    reset_runtime_workspace_admission()


@pytest.fixture
def workspace_service(tmp_path: Path) -> WorkspaceService:
    return WorkspaceService(
        workspaces_root=tmp_path / "workspaces",
        archives_root=tmp_path / "archives",
    )


def _create_workspace(
    workspace_service: WorkspaceService,
    workspace_id: str,
) -> None:
    workspace_service.create(
        workspace_id,
        display_name="runtime admission test",
        genre="scifi",
        premise="test",
        activate=False,
    )


def _rewrite_schema(
    workspace_service: WorkspaceService,
    workspace_id: str,
    schema_version: str,
) -> None:
    meta_path = workspace_service.workspaces_root / workspace_id / "meta.json"
    payload = json.loads(meta_path.read_text(encoding="utf-8"))
    payload["engine_version"] = ENGINE_VERSION
    payload["schema_version"] = schema_version
    meta_path.write_text(
        json.dumps(payload, ensure_ascii=False, indent=2),
        encoding="utf-8",
    )


def _create_current_database(
    workspace_service: WorkspaceService,
    workspace_id: str,
) -> Path:
    db_path = WorkspacePaths(
        workspace_id,
        workspaces_root=workspace_service.workspaces_root,
    ).db_path
    engine = create_engine(f"sqlite:///{db_path}")
    Base.metadata.create_all(engine)
    now = datetime.now(UTC)
    with Session(engine) as session:
        session.add(
            WorkspaceORM(
                workspace_id=workspace_id,
                display_name="runtime admission identity",
                genre="scifi",
                premise="test",
                created_at=now,
                last_active_at=now,
                engine_version=ENGINE_VERSION,
                status="active",
                total_chapters=1,
                completed_chapters=0,
                config_overrides={},
            )
        )
        session.commit()
    engine.dispose()
    return db_path


def _create_empty_current_database(
    workspace_service: WorkspaceService,
    workspace_id: str,
) -> Path:
    db_path = WorkspacePaths(
        workspace_id,
        workspaces_root=workspace_service.workspaces_root,
    ).db_path
    engine = create_engine(f"sqlite:///{db_path}")
    Base.metadata.create_all(engine)
    engine.dispose()
    return db_path


def test_old_meta_is_rejected_before_database_open_or_create_all(
    workspace_service: WorkspaceService,
) -> None:
    workspace_id = "ws-old-runtime"
    _create_workspace(workspace_service, workspace_id)
    _rewrite_schema(
        workspace_service,
        workspace_id,
        "0029_structured_rewrite_constraint_results",
    )
    db_path = WorkspacePaths(
        workspace_id,
        workspaces_root=workspace_service.workspaces_root,
    ).db_path
    db_path.parent.mkdir(parents=True, exist_ok=True)
    connection = sqlite3.connect(db_path)
    try:
        connection.execute("CREATE TABLE preserved_evidence (value TEXT)")
        connection.execute("INSERT INTO preserved_evidence(value) VALUES ('unchanged')")
        connection.commit()
    finally:
        connection.close()
    before = db_path.read_bytes()
    before_mtime = db_path.stat().st_mtime_ns

    with pytest.raises(WorkspaceVersionServiceError, match="admission"):
        _make_session_factory(workspace_service.workspaces_root)(workspace_id)

    assert db_path.read_bytes() == before
    assert db_path.stat().st_mtime_ns == before_mtime
    assert not (workspace_service.workspaces_root / ".operation_locks").exists()
    connection = sqlite3.connect(f"file:{db_path}?mode=ro", uri=True)
    try:
        tables = {
            str(row[0])
            for row in connection.execute(
                "SELECT name FROM sqlite_master WHERE type='table'"
            )
        }
    finally:
        connection.close()
    assert tables == {"preserved_evidence"}


def test_other_workspace_database_is_rejected_by_startup_and_runtime_zero_write(
    workspace_service: WorkspaceService,
) -> None:
    workspace_id = "ws-identity-target"
    _create_workspace(workspace_service, workspace_id)
    db_path = _create_current_database(workspace_service, workspace_id)
    engine = create_engine(f"sqlite:///{db_path}")
    now = datetime.now(UTC)
    with Session(engine) as session:
        session.add(
            WorkspaceORM(
                workspace_id="ws-identity-source",
                display_name="source",
                genre="scifi",
                premise="source",
                created_at=now,
                last_active_at=now,
                engine_version=ENGINE_VERSION,
                status="active",
                total_chapters=1,
                completed_chapters=0,
                config_overrides={},
            )
        )
        session.commit()
    engine.dispose()
    before = db_path.read_bytes()

    with pytest.raises(
        WorkspaceStartupRecoveryAdmissionError,
        match="异工作区",
    ):
        admit_workspaces_for_startup_recovery(workspace_service)
    with pytest.raises(
        WorkspaceStartupRecoveryAdmissionError,
        match="异工作区",
    ):
        _make_session_factory(workspace_service.workspaces_root)(workspace_id)

    assert db_path.read_bytes() == before
    assert not (workspace_service.workspaces_root / ".operation_locks").exists()


def test_online_migration_requires_restart_before_normal_session(
    workspace_service: WorkspaceService,
) -> None:
    workspace_id = "ws-migrated-online"
    _create_workspace(workspace_service, workspace_id)
    _create_current_database(workspace_service, workspace_id)

    configure_runtime_workspace_admission(
        workspace_service.workspaces_root,
        admitted_workspace_ids=[],
        restart_required_workspace_ids=[workspace_id],
    )
    with pytest.raises(WorkspaceVersionServiceError, match="必须重启"):
        _make_session_factory(workspace_service.workspaces_root)(workspace_id)

    # 模拟新进程 startup admission 已验真并执行完本地 recovery。
    configure_runtime_workspace_admission(
        workspace_service.workspaces_root,
        admitted_workspace_ids=[workspace_id],
        restart_required_workspace_ids=[],
    )
    session = _make_session_factory(workspace_service.workspaces_root)(workspace_id)
    try:
        assert (
            "chapter_operation_leases" in inspect(session.get_bind()).get_table_names()
        )
        assert session.get(WorkspaceORM, workspace_id) is not None
    finally:
        session.get_bind().dispose()
        session.close()


def test_version_status_and_migrate_entry_remain_available_while_runtime_blocked(
    workspace_service: WorkspaceService,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    workspace_id = "ws-product-migrate-entry"
    _create_workspace(workspace_service, workspace_id)
    _rewrite_schema(
        workspace_service,
        workspace_id,
        "0029_structured_rewrite_constraint_results",
    )
    _create_current_database(workspace_service, workspace_id)
    configure_runtime_workspace_admission(
        workspace_service.workspaces_root,
        admitted_workspace_ids=[],
        restart_required_workspace_ids=[workspace_id],
    )
    from src.api import task_registry

    monkeypatch.setattr(
        task_registry,
        "has_active_tasks",
        lambda _workspace_id: False,
    )
    service = get_workspace_version_service(workspace_service)
    assert service.get_version_status(workspace_id).migration_required is True

    result = service.migrate(workspace_id, workspace_id)

    assert (
        result.to_schema_version
        == service.get_version_status(workspace_id).current_schema
    )
    with pytest.raises(WorkspaceVersionServiceError, match="必须重启"):
        _make_session_factory(workspace_service.workspaces_root)(workspace_id)


def test_current_existing_database_not_seen_at_startup_requires_restart(
    workspace_service: WorkspaceService,
) -> None:
    workspace_id = "ws-existing-not-admitted"
    _create_workspace(workspace_service, workspace_id)
    _create_current_database(workspace_service, workspace_id)
    configure_runtime_workspace_admission(
        workspace_service.workspaces_root,
        admitted_workspace_ids=[],
        restart_required_workspace_ids=[],
    )

    with pytest.raises(WorkspaceVersionServiceError, match="未经过本进程"):
        _make_session_factory(workspace_service.workspaces_root)(workspace_id)


def test_empty_exact_schema_substitution_is_rejected_zero_write(
    workspace_service: WorkspaceService,
) -> None:
    workspace_id = "ws-empty-exact-substitution"
    _create_workspace(workspace_service, workspace_id)
    (
        workspace_service.workspaces_root
        / workspace_id
        / ".runtime_initialization_pending.json"
    ).unlink()
    db_path = _create_empty_current_database(workspace_service, workspace_id)
    before = db_path.read_bytes()

    with pytest.raises(
        WorkspaceStartupRecoveryAdmissionError,
        match="主身份",
    ):
        admit_workspaces_for_startup_recovery(workspace_service)
    with pytest.raises(WorkspaceVersionServiceError, match="主身份"):
        _make_session_factory(workspace_service.workspaces_root)(workspace_id)

    assert db_path.read_bytes() == before


def test_current_meta_with_old_database_shape_is_rejected_without_create_all(
    workspace_service: WorkspaceService,
) -> None:
    workspace_id = "ws-current-meta-old-shape"
    _create_workspace(workspace_service, workspace_id)
    db_path = _create_current_database(workspace_service, workspace_id)
    connection = sqlite3.connect(db_path)
    try:
        connection.execute("DROP TABLE chapter_operation_leases")
        connection.commit()
    finally:
        connection.close()
    before = db_path.read_bytes()

    with pytest.raises(WorkspaceVersionServiceError, match="admission"):
        _make_session_factory(workspace_service.workspaces_root)(workspace_id)

    assert db_path.read_bytes() == before
    connection = sqlite3.connect(f"file:{db_path}?mode=ro", uri=True)
    try:
        tables = {
            str(row[0])
            for row in connection.execute(
                "SELECT name FROM sqlite_master WHERE type='table'"
            )
        }
    finally:
        connection.close()
    assert "chapter_operation_leases" not in tables


def test_fresh_current_workspace_can_initialize_after_startup(
    workspace_service: WorkspaceService,
) -> None:
    workspace_id = "ws-fresh-current"
    _create_workspace(workspace_service, workspace_id)
    db_path = WorkspacePaths(
        workspace_id,
        workspaces_root=workspace_service.workspaces_root,
    ).db_path
    marker = db_path.parents[1] / ".runtime_initialization_pending.json"
    assert marker.is_file()
    configure_runtime_workspace_admission(
        workspace_service.workspaces_root,
        admitted_workspace_ids=[],
        restart_required_workspace_ids=[],
    )

    session = _make_session_factory(workspace_service.workspaces_root)(workspace_id)
    try:
        assert db_path.is_file()
        assert (
            "chapter_operation_leases" in inspect(session.get_bind()).get_table_names()
        )
        assert session.get(WorkspaceORM, workspace_id) is not None
        assert not marker.exists()
    finally:
        session.get_bind().dispose()
        session.close()


def test_startup_identified_empty_database_initializes_same_inode(
    workspace_service: WorkspaceService,
) -> None:
    workspace_id = "ws-existing-empty"
    _create_workspace(workspace_service, workspace_id)
    db_path = WorkspacePaths(
        workspace_id,
        workspaces_root=workspace_service.workspaces_root,
    ).db_path
    db_path.parent.mkdir(parents=True, exist_ok=True)
    connection = sqlite3.connect(db_path)
    try:
        connection.execute("VACUUM")
    finally:
        connection.close()
    before = db_path.stat()
    assert before.st_size > 0
    configure_runtime_workspace_admission(
        workspace_service.workspaces_root,
        admitted_workspace_ids=[],
        restart_required_workspace_ids=[],
        uninitialized_workspace_ids=[workspace_id],
    )

    session = _make_session_factory(workspace_service.workspaces_root)(workspace_id)
    engine = session.get_bind()
    session.close()
    engine.dispose()

    after = db_path.stat()
    assert (after.st_dev, after.st_ino) == (before.st_dev, before.st_ino)
    assert after.st_size > 0
    assert not (db_path.parents[1] / ".runtime_initialization_pending.json").exists()


def test_admitted_epoch_reuses_shape_audit_cache(
    workspace_service: WorkspaceService,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    workspace_id = "ws-admission-cache"
    _create_workspace(workspace_service, workspace_id)
    _create_current_database(workspace_service, workspace_id)
    configure_runtime_workspace_admission(
        workspace_service.workspaces_root,
        admitted_workspace_ids=[workspace_id],
        restart_required_workspace_ids=[],
    )

    def _unexpected_full_schema_audit(*_args: object, **_kwargs: object) -> bool:
        raise AssertionError("已准入 epoch 不应每次 session 重跑全量 snapshot")

    monkeypatch.setattr(
        deps_module,
        "validate_workspace_for_runtime_open",
        _unexpected_full_schema_audit,
    )
    factory = _make_session_factory(workspace_service.workspaces_root)
    first = factory(workspace_id)
    second = factory(workspace_id)
    first_engine = first.get_bind()
    second_engine = second.get_bind()
    first.close()
    second.close()
    first_engine.dispose()
    second_engine.dispose()


def test_admitted_database_deleted_mid_process_is_not_recreated(
    workspace_service: WorkspaceService,
) -> None:
    workspace_id = "ws-deleted-after-admission"
    _create_workspace(workspace_service, workspace_id)
    db_path = _create_current_database(workspace_service, workspace_id)
    configure_runtime_workspace_admission(
        workspace_service.workspaces_root,
        admitted_workspace_ids=[workspace_id],
        restart_required_workspace_ids=[],
    )
    db_path.unlink()

    with pytest.raises(WorkspaceVersionServiceError, match="拒绝静默重建"):
        _make_session_factory(workspace_service.workspaces_root)(workspace_id)

    assert not db_path.exists()


def test_open_session_holds_cross_process_shared_maintenance_lock(
    workspace_service: WorkspaceService,
) -> None:
    workspace_id = "ws-cross-process-lock"
    _create_workspace(workspace_service, workspace_id)
    _create_current_database(workspace_service, workspace_id)
    configure_runtime_workspace_admission(
        workspace_service.workspaces_root,
        admitted_workspace_ids=[workspace_id],
        restart_required_workspace_ids=[],
    )
    session = _make_session_factory(workspace_service.workspaces_root)(workspace_id)
    engine = session.get_bind()
    lock_path = (
        workspace_service.workspaces_root / ".operation_locks" / f"{workspace_id}.lock"
    )
    child_script = (
        "import fcntl, pathlib, sys; "
        "handle = pathlib.Path(sys.argv[1]).open('a+b'); "
        "\ntry:\n"
        " fcntl.flock(handle.fileno(), fcntl.LOCK_EX | fcntl.LOCK_NB)\n"
        "except BlockingIOError:\n sys.exit(23)\n"
        "sys.exit(0)"
    )

    blocked = subprocess.run(
        [sys.executable, "-c", child_script, str(lock_path)],
        check=False,
    )
    assert blocked.returncode == 23

    session.close()
    engine.dispose()
    acquired = subprocess.run(
        [sys.executable, "-c", child_script, str(lock_path)],
        check=False,
    )
    assert acquired.returncode == 0
