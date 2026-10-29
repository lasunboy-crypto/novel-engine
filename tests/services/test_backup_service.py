"""BackupService 单测(P6-B-07)。

覆盖维度(对齐 plan §10 测试约定 5 类):

- **happy**:create_auto_backup / create_manual_backup / list_backups /
  restore_from_backup / prune_old_auto_backups 走通 + zip 结构验证 +
  backup_meta.json 字段完整 + pre_restore zip 落盘
- **error**:工作区不存在 → BackupServiceError;backup_id 不存在 →
  BackupNotFoundError;zip 损坏 → BackupInvalidFormatError;schema_version
  不匹配 → RestoreVersionIncompatibleError
- **gate**:list_backups 跳过损坏 zip(不抛错);prune 跨 30 份淘汰最老
- **rollback**:restore 后原 workspace 数据已替换 + pre_restore 保留
- **WS event**:每 happy path 配套 ws_manager.publish 调用断言

不依赖真 LLM / 真 alembic 数据库(``tmp_path`` + 标准库 zipfile)。
"""

from __future__ import annotations

import json
import sqlite3
import threading
import zipfile
from datetime import UTC, datetime
from pathlib import Path
from typing import Any

import pytest
from sqlalchemy import create_engine, inspect
from sqlalchemy.orm import Session

from src.core.db import Base, WorkspaceORM
from src.config import settings
from src.api.main import _run_workspace_recovery_admission_scan
from src.api.deps import reset_runtime_workspace_admission
from src.services import backup_service as backup_service_module
from src.services.backup_service import (
    BackupInvalidFormatError,
    BackupNotFoundError,
    BackupService,
    BackupServiceError,
    RestoreVersionIncompatibleError,
    recover_interrupted_workspace_restores,
)
from src.services.workspace_service import WorkspaceService
from src.services.workspace_operation_guard import workspace_operation_lock
from src.services.workspace_version_service import WorkspaceVersionService


# ---------------------------------------------------------------------------
# Fakes
# ---------------------------------------------------------------------------


class _FakeWSManager:
    """记录所有 publish 调用,供断言。"""

    def __init__(self, *, raise_exc: Exception | None = None) -> None:
        self.events: list[dict[str, Any]] = []
        self._raise_exc = raise_exc

    def publish(self, *, channel: str, event: str, data: dict[str, Any]) -> None:
        if self._raise_exc is not None:
            raise self._raise_exc
        self.events.append({"channel": channel, "event": event, "data": data})


# ---------------------------------------------------------------------------
# Fixtures
# ---------------------------------------------------------------------------


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
        "ws-backup-test",
        display_name="P6-B-07 backup service test",
        genre="scifi",
        premise="test premise",
        activate=False,
    )
    return "ws-backup-test"


@pytest.fixture
def ws_manager() -> _FakeWSManager:
    return _FakeWSManager()


@pytest.fixture
def backup_service(
    workspace_service: WorkspaceService, ws_manager: _FakeWSManager
) -> BackupService:
    return BackupService(workspace_service=workspace_service, ws_manager=ws_manager)


def _populate_workspace_data(workspaces_root: Path, workspace_id: str) -> None:
    """在工作区写入测试数据(db / outline / cards)模拟真实写作产出。"""
    root = workspaces_root / workspace_id
    db_path = root / "data" / "novel.db"
    db_path.parent.mkdir(parents=True, exist_ok=True)
    engine = create_engine(f"sqlite:///{db_path}")
    Base.metadata.create_all(engine)
    now = datetime.now(UTC)
    with Session(engine) as session:
        session.add(
            WorkspaceORM(
                workspace_id=workspace_id,
                display_name="backup test",
                genre="scifi",
                premise="test",
                created_at=now,
                last_active_at=now,
                engine_version="0.1.0",
                status="active",
                total_chapters=1,
                completed_chapters=0,
                config_overrides={},
            )
        )
        session.commit()
    engine.dispose()

    outline_dir = root / "data" / "outline"
    outline_dir.mkdir(parents=True, exist_ok=True)
    (outline_dir / "synopsis.json").write_text(
        '{"text": "test outline"}', encoding="utf-8"
    )

    cards_dir = root / "data" / "cards"
    cards_dir.mkdir(parents=True, exist_ok=True)
    (cards_dir / "ch-0001.json").write_text(
        '{"chapter_id": "ch-0001"}', encoding="utf-8"
    )


# ---------------------------------------------------------------------------
# happy path
# ---------------------------------------------------------------------------


class TestCreateAutoBackupHappy:
    def test_auto_backup_creates_zip_with_expected_contents(
        self,
        backup_service: BackupService,
        workspace_service: WorkspaceService,
        workspace_id: str,
        ws_manager: _FakeWSManager,
    ) -> None:
        _populate_workspace_data(workspace_service.workspaces_root, workspace_id)

        info = backup_service.create_auto_backup(workspace_id, "ch-0001")

        assert info.backup_type == "auto"
        assert info.chapter_id == "ch-0001"
        assert info.backup_id.startswith("auto_")
        assert info.backup_id.endswith("_ch-0001")
        zip_path = Path(info.file_path)
        assert zip_path.is_file()
        assert zip_path.suffix == ".zip"

        # zip 内含 backup_meta.json + db + outline + cards
        with zipfile.ZipFile(zip_path, "r") as zf:
            names = set(zf.namelist())
            assert "backup_meta.json" in names
            assert "meta.json" in names
            assert "data/novel.db" in names
            assert "data/outline/synopsis.json" in names
            assert "data/cards/ch-0001.json" in names

            meta = json.loads(zf.read("backup_meta.json").decode("utf-8"))
            assert meta["backup_type"] == "auto"
            assert meta["chapter_id"] == "ch-0001"
            assert meta["workspace_id"] == workspace_id
            assert "schema_version" in meta

        # WS publish 调用断言
        assert len(ws_manager.events) == 1
        evt = ws_manager.events[0]
        assert evt["event"] == "backup_created"
        assert evt["channel"] == f"workspace.{workspace_id}.events"
        assert evt["data"]["backup_type"] == "auto"
        assert evt["data"]["chapter_id"] == "ch-0001"


class TestCreateManualBackupHappy:
    def test_manual_backup_creates_zip_with_full_contents(
        self,
        backup_service: BackupService,
        workspace_service: WorkspaceService,
        workspace_id: str,
        ws_manager: _FakeWSManager,
    ) -> None:
        _populate_workspace_data(workspace_service.workspaces_root, workspace_id)
        # 加 chapters / assets 目录验全量
        root = workspace_service.workspaces_root / workspace_id
        chapters_dir = root / "data" / "chapters" / "ch-0001"
        chapters_dir.mkdir(parents=True, exist_ok=True)
        (chapters_dir / "final.md").write_text("正文 v1", encoding="utf-8")
        assets_dir = root / "data" / "assets"
        assets_dir.mkdir(parents=True, exist_ok=True)
        (assets_dir / "world.md").write_text("# 世界观", encoding="utf-8")
        project_yaml = root / "project.yaml"
        project_yaml.write_text("writer_profile: safe-profile\n", encoding="utf-8")
        prompt_file = root / "prompts" / "style_guide.md"
        prompt_file.parent.mkdir(parents=True, exist_ok=True)
        prompt_file.write_text("风格锚", encoding="utf-8")
        chroma_file = root / "data" / "chroma" / "vector.bin"
        chroma_file.parent.mkdir(parents=True, exist_ok=True)
        chroma_file.write_bytes(b"vector")

        info = backup_service.create_manual_backup(workspace_id)

        assert info.backup_type == "manual"
        assert info.chapter_id is None
        assert info.backup_id.startswith("manual_")
        zip_path = Path(info.file_path)

        with zipfile.ZipFile(zip_path, "r") as zf:
            names = set(zf.namelist())
            assert "data/novel.db" in names
            assert "data/outline/synopsis.json" in names
            assert "data/cards/ch-0001.json" in names
            assert "data/chapters/ch-0001/final.md" in names
            assert "data/assets/world.md" in names
            assert "project.yaml" in names
            assert "prompts/style_guide.md" in names
            assert "data/chroma/vector.bin" in names

        assert ws_manager.events[-1]["event"] == "backup_created"
        assert ws_manager.events[-1]["data"]["backup_type"] == "manual"

    def test_same_second_manual_backups_do_not_overwrite(
        self,
        backup_service: BackupService,
        workspace_service: WorkspaceService,
        workspace_id: str,
        monkeypatch: pytest.MonkeyPatch,
    ) -> None:
        _populate_workspace_data(workspace_service.workspaces_root, workspace_id)
        monkeypatch.setattr(
            backup_service,
            "_now_compact",
            lambda: "20260808T120000Z",
        )

        first = backup_service.create_manual_backup(workspace_id)
        second = backup_service.create_manual_backup(workspace_id)

        assert first.backup_id != second.backup_id
        assert Path(first.file_path).is_file()
        assert Path(second.file_path).is_file()


class TestCreateMigrationBackup:
    def test_wal_commits_are_in_consistent_snapshot_and_source_version_is_kept(
        self,
        backup_service: BackupService,
        workspace_service: WorkspaceService,
        workspace_id: str,
        tmp_path: Path,
    ) -> None:
        db_path = workspace_service.workspaces_root / workspace_id / "data" / "novel.db"
        connection = sqlite3.connect(db_path)
        try:
            connection.execute("PRAGMA journal_mode = WAL")
            connection.execute("PRAGMA wal_autocheckpoint = 0")
            connection.execute("CREATE TABLE migration_marker (value TEXT NOT NULL)")
            connection.execute(
                "INSERT INTO migration_marker(value) VALUES ('committed-in-wal')"
            )
            connection.commit()
            assert Path(f"{db_path}-wal").stat().st_size > 0

            info = backup_service.create_migration_backup(
                workspace_id,
                source_schema_version=("0029_structured_rewrite_constraint_results"),
                source_engine_version="0.1.0",
            )
        finally:
            connection.close()

        extract_root = tmp_path / "migration-backup-extracted"
        with zipfile.ZipFile(info.file_path, "r") as zf:
            meta = json.loads(zf.read("backup_meta.json").decode("utf-8"))
            assert meta["schema_version"] == (
                "0029_structured_rewrite_constraint_results"
            )
            assert meta["engine_version"] == "0.1.0"
            assert "data/novel.db" in zf.namelist()
            zf.extract("data/novel.db", path=extract_root)

        snapshot = sqlite3.connect(extract_root / "data" / "novel.db")
        try:
            value = snapshot.execute("SELECT value FROM migration_marker").fetchone()
        finally:
            snapshot.close()
        assert value == ("committed-in-wal",)

    def test_missing_database_is_recorded_without_inventing_one(
        self,
        backup_service: BackupService,
        workspace_id: str,
    ) -> None:
        info = backup_service.create_migration_backup(
            workspace_id,
            source_schema_version="0029_structured_rewrite_constraint_results",
            source_engine_version="0.1.0",
        )

        with zipfile.ZipFile(info.file_path, "r") as zf:
            meta = json.loads(zf.read("backup_meta.json").decode("utf-8"))
            assert "data/novel.db" not in zf.namelist()
            assert "data/novel.db" not in meta["contents"]
            assert meta["schema_version"] == (
                "0029_structured_rewrite_constraint_results"
            )

    def test_same_second_migration_backups_have_unique_ids(
        self,
        backup_service: BackupService,
        workspace_id: str,
        monkeypatch: pytest.MonkeyPatch,
    ) -> None:
        monkeypatch.setattr(
            backup_service,
            "_now_compact",
            lambda: "20260808T120000Z",
        )

        first = backup_service.create_migration_backup(
            workspace_id,
            source_schema_version="0029_structured_rewrite_constraint_results",
            source_engine_version="0.1.0",
        )
        second = backup_service.create_migration_backup(
            workspace_id,
            source_schema_version="0029_structured_rewrite_constraint_results",
            source_engine_version="0.1.0",
        )

        assert first.backup_id != second.backup_id
        assert Path(first.file_path).is_file()
        assert Path(second.file_path).is_file()


@pytest.mark.parametrize("backup_kind", ["auto", "manual"])
def test_normal_backups_keep_committed_wal_rows(
    backup_service: BackupService,
    workspace_service: WorkspaceService,
    workspace_id: str,
    tmp_path: Path,
    backup_kind: str,
) -> None:
    _populate_workspace_data(workspace_service.workspaces_root, workspace_id)
    db_path = workspace_service.workspaces_root / workspace_id / "data" / "novel.db"
    live = sqlite3.connect(db_path)
    try:
        assert live.execute("PRAGMA journal_mode = WAL").fetchone() == ("wal",)
        live.execute("PRAGMA wal_autocheckpoint = 0")
        live.execute("CREATE TABLE normal_backup_wal (value TEXT)")
        live.execute("INSERT INTO normal_backup_wal(value) VALUES ('committed')")
        live.commit()
        assert Path(f"{db_path}-wal").stat().st_size > 0
        info = (
            backup_service.create_auto_backup(workspace_id, "ch-0001")
            if backup_kind == "auto"
            else backup_service.create_manual_backup(workspace_id)
        )
    finally:
        live.close()

    extracted = tmp_path / f"{backup_kind}-wal"
    with zipfile.ZipFile(info.file_path, "r") as zf:
        zf.extract("data/novel.db", path=extracted)
    snapshot = sqlite3.connect(extracted / "data" / "novel.db")
    try:
        row = snapshot.execute("SELECT value FROM normal_backup_wal").fetchone()
    finally:
        snapshot.close()
    assert row == ("committed",)


class TestListBackupsHappy:
    def test_list_returns_auto_and_manual_sorted_desc(
        self,
        backup_service: BackupService,
        workspace_service: WorkspaceService,
        workspace_id: str,
    ) -> None:
        _populate_workspace_data(workspace_service.workspaces_root, workspace_id)

        auto_info = backup_service.create_auto_backup(workspace_id, "ch-0001")
        manual_info = backup_service.create_manual_backup(workspace_id)

        backups = backup_service.list_backups(workspace_id)
        ids = {b.backup_id for b in backups}
        assert auto_info.backup_id in ids
        assert manual_info.backup_id in ids
        # 不应含 pre_restore_*(本测试无 restore 触发)
        assert not any(b.backup_id.startswith("pre_restore_") for b in backups)

    def test_list_skips_invalid_zip(
        self,
        backup_service: BackupService,
        workspace_service: WorkspaceService,
        workspace_id: str,
    ) -> None:
        # 创建一个损坏 zip 文件(非合法 zip 格式)
        backups_dir = workspace_service.workspaces_root / workspace_id / "backups"
        backups_dir.mkdir(parents=True, exist_ok=True)
        bad_zip = backups_dir / "auto_20260101T000000Z_ch-0001.zip"
        bad_zip.write_bytes(b"not a real zip")

        # 也加一个合法 manual 备份
        _populate_workspace_data(workspace_service.workspaces_root, workspace_id)
        manual_info = backup_service.create_manual_backup(workspace_id)

        backups = backup_service.list_backups(workspace_id)
        ids = {b.backup_id for b in backups}
        assert manual_info.backup_id in ids
        # 损坏 zip 不应出现在 list 里
        assert "auto_20260101T000000Z_ch-0001" not in ids


class TestPruneOldAutoBackupsHappy:
    def test_prune_keeps_only_recent_30(
        self,
        backup_service: BackupService,
        workspace_service: WorkspaceService,
        workspace_id: str,
    ) -> None:
        _populate_workspace_data(workspace_service.workspaces_root, workspace_id)
        backups_dir = workspace_service.workspaces_root / workspace_id / "backups"
        backups_dir.mkdir(parents=True, exist_ok=True)

        # 手动产 35 份 auto 备份(直接落文件,加伪 backup_meta.json),时间戳
        # 字面递增。
        for i in range(35):
            backup_id = f"auto_20260101T{i:06d}Z_ch-0001"
            zip_path = backups_dir / f"{backup_id}.zip"
            with zipfile.ZipFile(zip_path, "w") as zf:
                meta = {
                    "backup_id": backup_id,
                    "backup_type": "auto",
                    "workspace_id": workspace_id,
                    "created_at": f"2026-01-01T{i:02d}:00:00+00:00",
                    "chapter_id": "ch-0001",
                    "schema_version": "0009_pipeline_runs_table",
                    "engine_version": "0.1.0",
                    "contents": [],
                }
                zf.writestr("backup_meta.json", json.dumps(meta))

        deleted = backup_service.prune_old_auto_backups(workspace_id)
        assert deleted == 5  # 35 - 30

        remaining = [
            f
            for f in backups_dir.iterdir()
            if f.suffix == ".zip" and f.name.startswith("auto_")
        ]
        assert len(remaining) == 30


# ---------------------------------------------------------------------------
# purge_backups path(v1.1 w3-s8 F1)
# ---------------------------------------------------------------------------


class TestPurgeBackupsHappy:
    def test_purge_deletes_all_known_prefixes_and_reports_freed(
        self,
        backup_service: BackupService,
        workspace_service: WorkspaceService,
        workspace_id: str,
    ) -> None:
        """purge 删 auto + manual + pre_restore 三类前缀,返删除数 + 释放字节。"""
        _populate_workspace_data(workspace_service.workspaces_root, workspace_id)
        backups_dir = workspace_service.workspaces_root / workspace_id / "backups"
        backups_dir.mkdir(parents=True, exist_ok=True)

        names = [
            "auto_20260101T000000Z_ch-0001.zip",
            "manual_20260102T000000Z.zip",
            "pre_restore_20260103T000000Z.zip",
        ]
        for name in names:
            (backups_dir / name).write_bytes(b"x" * 100)

        result = backup_service.purge_backups(workspace_id)

        assert result.deleted_count == 3
        assert result.freed_bytes == 300
        assert result.workspace_id == workspace_id
        remaining = [f for f in backups_dir.iterdir() if f.suffix == ".zip"]
        assert remaining == []

    def test_purge_preserves_unrelated_files(
        self,
        backup_service: BackupService,
        workspace_service: WorkspaceService,
        workspace_id: str,
    ) -> None:
        """purge 只删已知前缀 zip,不误删 backups/ 下其他文件(前缀白名单)。"""
        _populate_workspace_data(workspace_service.workspaces_root, workspace_id)
        backups_dir = workspace_service.workspaces_root / workspace_id / "backups"
        backups_dir.mkdir(parents=True, exist_ok=True)
        (backups_dir / "auto_20260101T000000Z_ch-0001.zip").write_bytes(b"x")
        (backups_dir / "README.txt").write_text("keep me")
        (backups_dir / "unrelated.zip").write_bytes(b"y")  # 非已知前缀

        result = backup_service.purge_backups(workspace_id)

        assert result.deleted_count == 1
        assert (backups_dir / "README.txt").is_file()
        assert (backups_dir / "unrelated.zip").is_file()

    def test_purge_no_backups_dir_returns_zero(
        self,
        backup_service: BackupService,
        workspace_service: WorkspaceService,
        workspace_id: str,
    ) -> None:
        """backups/ 不存在 → deleted_count=0 / freed_bytes=0,不抛错。"""
        _populate_workspace_data(workspace_service.workspaces_root, workspace_id)
        result = backup_service.purge_backups(workspace_id)
        assert result.deleted_count == 0
        assert result.freed_bytes == 0

    def test_purge_nonexistent_workspace_raises(
        self, backup_service: BackupService
    ) -> None:
        """工作区不存在 → BackupServiceError(_validate_workspace 抛)。"""
        with pytest.raises(BackupServiceError):
            backup_service.purge_backups("no_such_workspace")

    def test_purge_waits_for_workspace_maintenance_lock(
        self,
        backup_service: BackupService,
        workspace_service: WorkspaceService,
        workspace_id: str,
    ) -> None:
        backups_dir = workspace_service.workspaces_root / workspace_id / "backups"
        anchor = backups_dir / "pre_restore_20260103T000000Z.zip"
        anchor.write_bytes(b"rollback anchor")
        call_started = threading.Event()
        call_done = threading.Event()
        failures: list[BaseException] = []

        def _purge() -> None:
            call_started.set()
            try:
                backup_service.purge_backups(workspace_id)
            except BaseException as exc:  # noqa: BLE001
                failures.append(exc)
            finally:
                call_done.set()

        with workspace_operation_lock(
            workspace_service.workspaces_root,
            workspace_id,
        ):
            worker = threading.Thread(target=_purge)
            worker.start()
            assert call_started.wait(timeout=5)
            assert not call_done.wait(timeout=0.2)
            assert anchor.read_bytes() == b"rollback anchor"

        worker.join(timeout=5)
        assert not worker.is_alive()
        assert failures == []
        assert not anchor.exists()


# ---------------------------------------------------------------------------
# restore path
# ---------------------------------------------------------------------------


class TestRestoreFromBackupHappy:
    def test_crash_after_original_move_rolls_back_from_durable_marker(
        self,
        backup_service: BackupService,
        workspace_service: WorkspaceService,
        workspace_id: str,
        monkeypatch: pytest.MonkeyPatch,
    ) -> None:
        _populate_workspace_data(workspace_service.workspaces_root, workspace_id)
        source = backup_service.create_manual_backup(workspace_id)
        outline = (
            workspace_service.workspaces_root
            / workspace_id
            / "data"
            / "outline"
            / "synopsis.json"
        )
        outline.write_text('{"text":"live-dirty"}', encoding="utf-8")
        original_write = backup_service_module._write_restore_marker
        calls = 0

        def _crash_on_second_marker(path: Path, payload: dict[str, object]) -> None:
            nonlocal calls
            calls += 1
            if calls == 2:
                raise KeyboardInterrupt("simulated process death")
            original_write(path, payload)

        monkeypatch.setattr(
            backup_service_module,
            "_write_restore_marker",
            _crash_on_second_marker,
        )
        with pytest.raises(KeyboardInterrupt):
            backup_service.restore_from_backup(workspace_id, source.backup_id)

        assert not (workspace_service.workspaces_root / workspace_id).exists()
        recover_interrupted_workspace_restores(workspace_service.workspaces_root)
        assert "live-dirty" in outline.read_text(encoding="utf-8")

    def test_startup_finishes_swapped_candidate_before_admission(
        self,
        backup_service: BackupService,
        workspace_service: WorkspaceService,
        workspace_id: str,
        monkeypatch: pytest.MonkeyPatch,
    ) -> None:
        _populate_workspace_data(workspace_service.workspaces_root, workspace_id)
        source = backup_service.create_manual_backup(workspace_id)
        outline = (
            workspace_service.workspaces_root
            / workspace_id
            / "data"
            / "outline"
            / "synopsis.json"
        )
        outline.write_text('{"text":"live-dirty"}', encoding="utf-8")
        original_write = backup_service_module._write_restore_marker
        calls = 0

        def _crash_on_third_marker(path: Path, payload: dict[str, object]) -> None:
            nonlocal calls
            calls += 1
            if calls == 3:
                raise KeyboardInterrupt("simulated process death")
            original_write(path, payload)

        monkeypatch.setattr(
            backup_service_module,
            "_write_restore_marker",
            _crash_on_third_marker,
        )
        with pytest.raises(KeyboardInterrupt):
            backup_service.restore_from_backup(workspace_id, source.backup_id)

        monkeypatch.setattr(
            backup_service_module,
            "_write_restore_marker",
            original_write,
        )
        monkeypatch.setattr(
            settings,
            "data_dir",
            workspace_service.workspaces_root,
        )
        reset_runtime_workspace_admission()
        admitted = _run_workspace_recovery_admission_scan()
        reset_runtime_workspace_admission()

        assert admitted == [workspace_id]
        assert "test outline" in outline.read_text(encoding="utf-8")

    def test_recovery_rolls_back_when_swapped_candidate_fails_validation(
        self,
        backup_service: BackupService,
        workspace_service: WorkspaceService,
        workspace_id: str,
        monkeypatch: pytest.MonkeyPatch,
    ) -> None:
        _populate_workspace_data(workspace_service.workspaces_root, workspace_id)
        source = backup_service.create_manual_backup(workspace_id)
        root = workspace_service.workspaces_root / workspace_id
        outline = root / "data" / "outline" / "synopsis.json"
        outline.write_text('{"text":"live-dirty"}', encoding="utf-8")
        original_write = backup_service_module._write_restore_marker
        calls = 0

        def _crash_after_swap(path: Path, payload: dict[str, object]) -> None:
            nonlocal calls
            calls += 1
            if calls == 3:
                raise KeyboardInterrupt("simulated process death")
            original_write(path, payload)

        monkeypatch.setattr(
            backup_service_module,
            "_write_restore_marker",
            _crash_after_swap,
        )
        with pytest.raises(KeyboardInterrupt):
            backup_service.restore_from_backup(workspace_id, source.backup_id)

        (root / "meta.json").write_text("{broken", encoding="utf-8")
        monkeypatch.setattr(
            backup_service_module,
            "_write_restore_marker",
            original_write,
        )
        recovered = recover_interrupted_workspace_restores(
            workspace_service.workspaces_root
        )

        assert recovered == [workspace_id]
        assert "live-dirty" in outline.read_text(encoding="utf-8")
        assert not (
            workspace_service.workspaces_root
            / ".restore_transactions"
            / f"{workspace_id}.json"
        ).exists()

    def test_recovery_rejects_unmarked_candidate_without_touching_workspace(
        self,
        workspace_service: WorkspaceService,
        workspace_id: str,
    ) -> None:
        root = workspace_service.workspaces_root / workspace_id
        sentinel = root / "preserved.txt"
        sentinel.write_text("safe", encoding="utf-8")
        orphan = (
            workspace_service.workspaces_root
            / ".restore_transactions"
            / f"{workspace_id}.{'a' * 32}.candidate"
        )
        orphan.mkdir(parents=True)

        with pytest.raises(BackupServiceError, match="无 durable marker"):
            recover_interrupted_workspace_restores(
                workspace_service.workspaces_root,
                workspace_id=workspace_id,
            )

        assert sentinel.read_text(encoding="utf-8") == "safe"
        assert orphan.is_dir()

    def test_restore_overwrites_workspace_and_creates_pre_restore(
        self,
        backup_service: BackupService,
        workspace_service: WorkspaceService,
        workspace_id: str,
        ws_manager: _FakeWSManager,
    ) -> None:
        # 初始数据 + 备份
        _populate_workspace_data(workspace_service.workspaces_root, workspace_id)
        info = backup_service.create_manual_backup(workspace_id)

        # 模拟用户修改了工作区(改 outline)
        outline_path = (
            workspace_service.workspaces_root
            / workspace_id
            / "data"
            / "outline"
            / "synopsis.json"
        )
        outline_path.write_text('{"text": "modified after backup"}', encoding="utf-8")

        result = backup_service.restore_from_backup(workspace_id, info.backup_id)

        assert result.backup_id == info.backup_id
        assert result.pre_restore_backup_id.startswith("pre_restore_")

        # 工作区恢复到备份内容
        restored_text = outline_path.read_text(encoding="utf-8")
        assert "test outline" in restored_text

        # pre_restore zip 落盘
        backups_dir = workspace_service.workspaces_root / workspace_id / "backups"
        pre_restore_zip = backups_dir / f"{result.pre_restore_backup_id}.zip"
        assert pre_restore_zip.is_file()

        # WS publish restore_completed
        events = [e for e in ws_manager.events if e["event"] == "restore_completed"]
        assert len(events) == 1
        assert events[0]["data"]["backup_id"] == info.backup_id

    def test_restore_pre_restore_preserves_modified_state(
        self,
        backup_service: BackupService,
        workspace_service: WorkspaceService,
        workspace_id: str,
    ) -> None:
        """rollback 测试:restore 前的 workspace 修改内容应被 pre_restore zip 保留。"""
        _populate_workspace_data(workspace_service.workspaces_root, workspace_id)
        info = backup_service.create_manual_backup(workspace_id)

        outline_path = (
            workspace_service.workspaces_root
            / workspace_id
            / "data"
            / "outline"
            / "synopsis.json"
        )
        outline_path.write_text('{"text": "modified before restore"}', encoding="utf-8")
        chroma_sentinel = (
            workspace_service.workspaces_root
            / workspace_id
            / "data"
            / "chroma"
            / "pre-restore-sentinel.txt"
        )
        chroma_sentinel.parent.mkdir(parents=True, exist_ok=True)
        chroma_sentinel.write_text("live-before-restore", encoding="utf-8")
        project_yaml = workspace_service.workspaces_root / workspace_id / "project.yaml"
        project_yaml.write_text("model: live-before-restore\n", encoding="utf-8")
        prompt_sentinel = (
            workspace_service.workspaces_root / workspace_id / "prompts" / "writer.md"
        )
        prompt_sentinel.parent.mkdir(parents=True, exist_ok=True)
        prompt_sentinel.write_text("live prompt", encoding="utf-8")

        result = backup_service.restore_from_backup(workspace_id, info.backup_id)

        # 验 pre_restore zip 内容含"modified before restore"
        backups_dir = workspace_service.workspaces_root / workspace_id / "backups"
        pre_zip = backups_dir / f"{result.pre_restore_backup_id}.zip"
        with zipfile.ZipFile(pre_zip, "r") as zf:
            content = zf.read("data/outline/synopsis.json").decode("utf-8")
            assert "modified before restore" in content
            assert (
                zf.read("data/chroma/pre-restore-sentinel.txt").decode("utf-8")
                == "live-before-restore"
            )
            assert zf.read("project.yaml").decode("utf-8") == (
                "model: live-before-restore\n"
            )
            assert zf.read("prompts/writer.md").decode("utf-8") == ("live prompt")

    def test_same_second_pre_restore_anchors_do_not_overwrite(
        self,
        backup_service: BackupService,
        workspace_service: WorkspaceService,
        workspace_id: str,
        monkeypatch: pytest.MonkeyPatch,
    ) -> None:
        _populate_workspace_data(workspace_service.workspaces_root, workspace_id)
        source = backup_service.create_manual_backup(workspace_id)
        monkeypatch.setattr(
            backup_service,
            "_now_compact",
            lambda: "20260808T120000Z",
        )

        first = backup_service.restore_from_backup(
            workspace_id,
            source.backup_id,
        )
        second = backup_service.restore_from_backup(
            workspace_id,
            source.backup_id,
        )

        assert first.pre_restore_backup_id != second.pre_restore_backup_id
        backups_dir = workspace_service.workspaces_root / workspace_id / "backups"
        assert (backups_dir / f"{first.pre_restore_backup_id}.zip").is_file()
        assert (backups_dir / f"{second.pre_restore_backup_id}.zip").is_file()

    def test_pre_restore_database_snapshot_keeps_committed_wal_rows(
        self,
        backup_service: BackupService,
        workspace_service: WorkspaceService,
        workspace_id: str,
        tmp_path: Path,
    ) -> None:
        root = workspace_service.workspaces_root / workspace_id
        db_path = root / "data" / "novel.db"
        _populate_workspace_data(workspace_service.workspaces_root, workspace_id)
        baseline = backup_service.create_manual_backup(workspace_id)

        live = sqlite3.connect(db_path)
        try:
            assert live.execute("PRAGMA journal_mode = WAL").fetchone() == ("wal",)
            live.execute("PRAGMA wal_autocheckpoint = 0")
            live.execute("CREATE TABLE wal_before_restore (value TEXT)")
            live.execute(
                "INSERT INTO wal_before_restore(value) VALUES (?)",
                ("committed",),
            )
            live.commit()
            assert Path(f"{db_path}-wal").stat().st_size > 0

            result = backup_service.restore_from_backup(
                workspace_id,
                baseline.backup_id,
            )
        finally:
            live.close()

        pre_zip = root / "backups" / f"{result.pre_restore_backup_id}.zip"
        extracted = tmp_path / "pre-restore-wal"
        with zipfile.ZipFile(pre_zip, "r") as zf:
            zf.extract("data/novel.db", path=extracted)
        snapshot = sqlite3.connect(extracted / "data" / "novel.db")
        try:
            row = snapshot.execute("SELECT value FROM wal_before_restore").fetchone()
        finally:
            snapshot.close()
        assert row == ("committed",)

    def test_restore_pre_migration_backup_recovers_old_db_and_meta(
        self,
        backup_service: BackupService,
        workspace_service: WorkspaceService,
        workspace_id: str,
    ) -> None:
        """产品生成的迁移前旧版备份可通过正常 restore 路径回滚。"""

        previous_schema = "0029_structured_rewrite_constraint_results"
        root = workspace_service.workspaces_root / workspace_id
        meta_path = root / "meta.json"
        old_meta = json.loads(meta_path.read_text(encoding="utf-8"))
        old_meta["schema_version"] = previous_schema
        meta_path.write_text(
            json.dumps(old_meta, ensure_ascii=False, indent=2),
            encoding="utf-8",
        )
        db_path = root / "data" / "novel.db"
        _populate_workspace_data(workspace_service.workspaces_root, workspace_id)
        engine = create_engine(f"sqlite:///{db_path}")
        with engine.begin() as connection:
            connection.exec_driver_sql("DROP TABLE chapter_operation_leases")
        engine.dispose()

        info = backup_service.create_migration_backup(
            workspace_id,
            source_schema_version=previous_schema,
            source_engine_version=old_meta["engine_version"],
        )

        WorkspaceVersionService._run_alembic_migration(
            db_path,
            stamp_revision=previous_schema,
        )
        current_meta = dict(old_meta)
        current_meta["schema_version"] = backup_service._get_current_schema_version()
        meta_path.write_text(
            json.dumps(current_meta, ensure_ascii=False, indent=2),
            encoding="utf-8",
        )

        result = backup_service.restore_from_backup(
            workspace_id,
            info.backup_id,
        )

        assert result.backup_id == info.backup_id
        restored_engine = create_engine(f"sqlite:///{db_path}")
        restored_tables = set(inspect(restored_engine).get_table_names())
        restored_engine.dispose()
        assert "workspaces" in restored_tables
        assert "chapter_operation_leases" not in restored_tables
        restored_meta = json.loads(meta_path.read_text(encoding="utf-8"))
        assert restored_meta["schema_version"] == previous_schema

        current_anchor = root / "backups" / f"{result.pre_restore_backup_id}.zip"
        with zipfile.ZipFile(current_anchor, "r") as zf:
            current_anchor_meta = json.loads(
                zf.read("backup_meta.json").decode("utf-8")
            )
        assert current_anchor_meta["schema_version"] == (
            backup_service._get_current_schema_version()
        )

        roundtrip = backup_service.restore_from_backup(
            workspace_id,
            result.pre_restore_backup_id,
        )
        assert roundtrip.backup_id == result.pre_restore_backup_id
        roundtrip_engine = create_engine(f"sqlite:///{db_path}")
        roundtrip_tables = set(inspect(roundtrip_engine).get_table_names())
        roundtrip_engine.dispose()
        assert "chapter_operation_leases" in roundtrip_tables
        assert (
            json.loads(meta_path.read_text(encoding="utf-8"))["schema_version"]
            == backup_service._get_current_schema_version()
        )

    def test_restore_current_schema_pre_migration_backup_uses_current_path(
        self,
        backup_service: BackupService,
        workspace_service: WorkspaceService,
        workspace_id: str,
    ) -> None:
        """DB 已 head、仅补 meta 前的 current 备份不误走旧 0029 verifier。"""

        root = workspace_service.workspaces_root / workspace_id
        db_path = root / "data" / "novel.db"
        _populate_workspace_data(workspace_service.workspaces_root, workspace_id)
        chroma_sentinel = root / "data" / "chroma" / "migration-sentinel.bin"
        chroma_sentinel.parent.mkdir(parents=True, exist_ok=True)
        chroma_sentinel.write_bytes(b"before-migration")
        project_yaml = root / "project.yaml"
        project_yaml.write_text("model: before-migration\n", encoding="utf-8")
        prompt_sentinel = root / "prompts" / "writer.md"
        prompt_sentinel.parent.mkdir(parents=True, exist_ok=True)
        prompt_sentinel.write_text("before prompt", encoding="utf-8")
        workspace = workspace_service.info(workspace_id)
        current_schema = backup_service._get_current_schema_version()

        info = backup_service.create_migration_backup(
            workspace_id,
            source_schema_version=current_schema,
            source_engine_version=workspace.engine_version,
        )

        connection = sqlite3.connect(db_path)
        try:
            connection.execute("CREATE TABLE post_backup_marker (value TEXT NOT NULL)")
            connection.commit()
        finally:
            connection.close()
        chroma_sentinel.write_bytes(b"after-migration")
        project_yaml.write_text("model: after-migration\n", encoding="utf-8")
        prompt_sentinel.write_text("after prompt", encoding="utf-8")

        result = backup_service.restore_from_backup(
            workspace_id,
            info.backup_id,
        )

        assert result.backup_id == info.backup_id
        restored = create_engine(f"sqlite:///{db_path}")
        restored_tables = set(inspect(restored).get_table_names())
        restored.dispose()
        assert "post_backup_marker" not in restored_tables
        assert "chapter_operation_leases" in restored_tables
        assert chroma_sentinel.read_bytes() == b"before-migration"
        assert project_yaml.read_text(encoding="utf-8") == ("model: before-migration\n")
        assert prompt_sentinel.read_text(encoding="utf-8") == "before prompt"


# ---------------------------------------------------------------------------
# error path
# ---------------------------------------------------------------------------


class TestErrorPath:
    def test_restore_rejects_backup_id_path_escape_before_read(
        self,
        backup_service: BackupService,
        workspace_service: WorkspaceService,
        workspace_id: str,
    ) -> None:
        sentinel = workspace_service.workspaces_root / workspace_id / "preserved.txt"
        sentinel.write_text("safe", encoding="utf-8")

        with pytest.raises(BackupInvalidFormatError, match="backup_id"):
            backup_service.restore_from_backup(workspace_id, "../outside")

        assert sentinel.read_text(encoding="utf-8") == "safe"

    def test_restore_rejects_incremental_auto_before_pre_restore_or_delete(
        self,
        backup_service: BackupService,
        workspace_service: WorkspaceService,
        workspace_id: str,
    ) -> None:
        _populate_workspace_data(workspace_service.workspaces_root, workspace_id)
        auto = backup_service.create_auto_backup(workspace_id, "ch-0001")
        sentinel = workspace_service.workspaces_root / workspace_id / "preserved.txt"
        sentinel.write_text("safe", encoding="utf-8")
        before = set(
            (workspace_service.workspaces_root / workspace_id / "backups").glob(
                "pre_restore_*.zip"
            )
        )

        with pytest.raises(BackupInvalidFormatError, match="全量恢复"):
            backup_service.restore_from_backup(workspace_id, auto.backup_id)

        assert sentinel.read_text(encoding="utf-8") == "safe"
        after = set(
            (workspace_service.workspaces_root / workspace_id / "backups").glob(
                "pre_restore_*.zip"
            )
        )
        assert after == before

    @pytest.mark.parametrize(
        "removed_member",
        [
            "project.yaml",
            "prompts/",
            "data/outline/",
            "data/cards/",
            "data/chapters/",
            "data/chroma/",
        ],
    )
    def test_restore_rejects_self_consistent_manifest_missing_required_member(
        self,
        backup_service: BackupService,
        workspace_service: WorkspaceService,
        workspace_id: str,
        removed_member: str,
    ) -> None:
        _populate_workspace_data(workspace_service.workspaces_root, workspace_id)
        source = backup_service.create_manual_backup(workspace_id)
        zip_path = Path(source.file_path)
        with zipfile.ZipFile(zip_path, "r") as zf:
            members = {
                info.filename: zf.read(info.filename)
                for info in zf.infolist()
                if info.filename not in {"backup_meta.json", removed_member}
            }
            meta = json.loads(zf.read("backup_meta.json").decode("utf-8"))
        meta["contents"] = sorted(members)
        with zipfile.ZipFile(zip_path, "w") as zf:
            for name, payload in members.items():
                zf.writestr(name, payload)
            zf.writestr("backup_meta.json", json.dumps(meta))
        sentinel = workspace_service.workspaces_root / workspace_id / "preserved.txt"
        sentinel.write_text("safe", encoding="utf-8")

        with pytest.raises(BackupInvalidFormatError, match="缺少必要 member"):
            backup_service.restore_from_backup(workspace_id, source.backup_id)

        assert sentinel.read_text(encoding="utf-8") == "safe"
        assert not list(
            (workspace_service.workspaces_root / workspace_id / "backups").glob(
                "pre_restore_*.zip"
            )
        )

    def test_restore_rejects_contents_that_omit_real_zip_member(
        self,
        backup_service: BackupService,
        workspace_service: WorkspaceService,
        workspace_id: str,
    ) -> None:
        _populate_workspace_data(workspace_service.workspaces_root, workspace_id)
        source = backup_service.create_manual_backup(workspace_id)
        zip_path = Path(source.file_path)
        with zipfile.ZipFile(zip_path, "r") as zf:
            members = {
                info.filename: zf.read(info.filename)
                for info in zf.infolist()
                if info.filename != "backup_meta.json"
            }
            meta = json.loads(zf.read("backup_meta.json").decode("utf-8"))
        meta["contents"].remove("project.yaml")
        with zipfile.ZipFile(zip_path, "w") as zf:
            for name, payload in members.items():
                zf.writestr(name, payload)
            zf.writestr("backup_meta.json", json.dumps(meta))

        with pytest.raises(BackupInvalidFormatError, match="实际 members 不一致"):
            backup_service.restore_from_backup(workspace_id, source.backup_id)

    def test_restore_rejects_unsafe_zip_member_before_extract_or_delete(
        self,
        backup_service: BackupService,
        workspace_service: WorkspaceService,
        workspace_id: str,
        tmp_path: Path,
    ) -> None:
        backup_id = "manual_20260101T000000Z"
        zip_path = (
            workspace_service.workspaces_root
            / workspace_id
            / "backups"
            / f"{backup_id}.zip"
        )
        zip_path.parent.mkdir(parents=True, exist_ok=True)
        with zipfile.ZipFile(zip_path, "w") as zf:
            zf.writestr("../escaped.txt", "bad")
        sentinel = workspace_service.workspaces_root / workspace_id / "preserved.txt"
        sentinel.write_text("safe", encoding="utf-8")

        with pytest.raises(BackupInvalidFormatError, match="member"):
            backup_service.restore_from_backup(workspace_id, backup_id)

        assert sentinel.read_text(encoding="utf-8") == "safe"
        assert not (tmp_path / "escaped.txt").exists()

    def test_restore_rejects_current_archive_without_database_before_delete(
        self,
        backup_service: BackupService,
        workspace_service: WorkspaceService,
        workspace_id: str,
    ) -> None:
        backup_id = "manual_20260101T000000Z"
        root = workspace_service.workspaces_root / workspace_id
        workspace = workspace_service.info(workspace_id)
        workspace_meta = json.loads((root / "meta.json").read_text(encoding="utf-8"))
        zip_path = root / "backups" / f"{backup_id}.zip"
        zip_path.parent.mkdir(parents=True, exist_ok=True)
        with zipfile.ZipFile(zip_path, "w") as zf:
            zf.writestr(
                "backup_meta.json",
                json.dumps(
                    {
                        "backup_id": backup_id,
                        "backup_type": "manual",
                        "workspace_id": workspace_id,
                        "created_at": "2026-01-01T00:00:00+00:00",
                        "chapter_id": None,
                        "schema_version": workspace.schema_version,
                        "engine_version": workspace.engine_version,
                        "contents": ["meta.json"],
                    }
                ),
            )
            zf.writestr("meta.json", json.dumps(workspace_meta))
        sentinel = root / "preserved.txt"
        sentinel.write_text("safe", encoding="utf-8")

        with pytest.raises(BackupInvalidFormatError, match="novel.db"):
            backup_service.restore_from_backup(workspace_id, backup_id)

        assert sentinel.read_text(encoding="utf-8") == "safe"

    def test_current_restore_rejects_other_workspace_database_identity(
        self,
        backup_service: BackupService,
        workspace_service: WorkspaceService,
        workspace_id: str,
    ) -> None:
        source_id = "ws-backup-source"
        workspace_service.create(
            source_id,
            display_name="source",
            genre="scifi",
            premise="source",
            activate=False,
        )
        _populate_workspace_data(workspace_service.workspaces_root, source_id)
        source = backup_service.create_manual_backup(source_id)
        destination = (
            workspace_service.workspaces_root
            / workspace_id
            / "backups"
            / f"{source.backup_id}.zip"
        )
        destination.parent.mkdir(parents=True, exist_ok=True)
        with zipfile.ZipFile(source.file_path, "r") as source_zip:
            with zipfile.ZipFile(destination, "w") as target_zip:
                for name in source_zip.namelist():
                    payload = source_zip.read(name)
                    if name == "backup_meta.json":
                        meta = json.loads(payload.decode("utf-8"))
                        meta["workspace_id"] = workspace_id
                        payload = json.dumps(meta).encode("utf-8")
                    elif name == "meta.json":
                        meta = json.loads(payload.decode("utf-8"))
                        meta["workspace_id"] = workspace_id
                        payload = json.dumps(meta).encode("utf-8")
                    target_zip.writestr(name, payload)
        sentinel = workspace_service.workspaces_root / workspace_id / "preserved.txt"
        sentinel.write_text("safe", encoding="utf-8")

        with pytest.raises(BackupInvalidFormatError, match="异工作区"):
            backup_service.restore_from_backup(
                workspace_id,
                source.backup_id,
            )

        assert sentinel.read_text(encoding="utf-8") == "safe"

    def test_0029_restore_rejects_other_workspace_database_identity(
        self,
        backup_service: BackupService,
        workspace_service: WorkspaceService,
        workspace_id: str,
        tmp_path: Path,
    ) -> None:
        other_id = "ws-other-0029"
        db_path = tmp_path / "other-0029.db"
        engine = create_engine(f"sqlite:///{db_path}")
        Base.metadata.create_all(engine)
        now = datetime.now(UTC)
        with Session(engine) as session:
            session.add(
                WorkspaceORM(
                    workspace_id=other_id,
                    display_name="other",
                    genre="scifi",
                    premise="other",
                    created_at=now,
                    last_active_at=now,
                    engine_version="0.1.0",
                    status="active",
                    total_chapters=1,
                    completed_chapters=0,
                    config_overrides={},
                )
            )
            session.commit()
        with engine.begin() as connection:
            connection.exec_driver_sql("DROP TABLE chapter_operation_leases")
        engine.dispose()

        backup_id = "manual_20260101T000000Z_migration_deadbeef"
        previous_schema = "0029_structured_rewrite_constraint_results"
        root = workspace_service.workspaces_root / workspace_id
        workspace = workspace_service.info(workspace_id)
        workspace_meta = json.loads((root / "meta.json").read_text(encoding="utf-8"))
        workspace_meta["schema_version"] = previous_schema
        zip_path = root / "backups" / f"{backup_id}.zip"
        zip_path.parent.mkdir(parents=True, exist_ok=True)
        with zipfile.ZipFile(zip_path, "w") as zf:
            zf.writestr(
                "backup_meta.json",
                json.dumps(
                    {
                        "backup_id": backup_id,
                        "backup_type": "manual",
                        "workspace_id": workspace_id,
                        "created_at": "2026-01-01T00:00:00+00:00",
                        "chapter_id": None,
                        "schema_version": previous_schema,
                        "engine_version": workspace.engine_version,
                        "contents": ["meta.json", "data/novel.db"],
                        "purpose": "pre_migration",
                    }
                ),
            )
            zf.writestr("meta.json", json.dumps(workspace_meta))
            zf.write(db_path, "data/novel.db")
        sentinel = root / "preserved.txt"
        sentinel.write_text("safe", encoding="utf-8")

        with pytest.raises(BackupInvalidFormatError, match="异工作区"):
            backup_service.restore_from_backup(workspace_id, backup_id)

        assert sentinel.read_text(encoding="utf-8") == "safe"

    def test_backup_workspace_not_found(self, backup_service: BackupService) -> None:
        with pytest.raises(BackupServiceError):
            backup_service.create_manual_backup("no_such_workspace")

    def test_restore_backup_not_found(
        self, backup_service: BackupService, workspace_id: str
    ) -> None:
        with pytest.raises(BackupNotFoundError) as exc_info:
            backup_service.restore_from_backup(workspace_id, "manual_19990101T000000Z")
        assert exc_info.value.workspace_id == workspace_id
        assert exc_info.value.backup_id == "manual_19990101T000000Z"

    def test_restore_invalid_zip_format(
        self,
        backup_service: BackupService,
        workspace_service: WorkspaceService,
        workspace_id: str,
    ) -> None:
        backups_dir = workspace_service.workspaces_root / workspace_id / "backups"
        backups_dir.mkdir(parents=True, exist_ok=True)
        bad_zip = backups_dir / "manual_20260101T000000Z.zip"
        bad_zip.write_bytes(b"definitely not a zip")

        with pytest.raises(BackupInvalidFormatError):
            backup_service.restore_from_backup(workspace_id, "manual_20260101T000000Z")

    def test_restore_version_incompatible_newer(
        self,
        backup_service: BackupService,
        workspace_service: WorkspaceService,
        workspace_id: str,
    ) -> None:
        # 创建一个 schema_version 高于当前 head 的 backup zip(伪造未来版本)
        backups_dir = workspace_service.workspaces_root / workspace_id / "backups"
        backups_dir.mkdir(parents=True, exist_ok=True)
        backup_id = "manual_20260101T000000Z"
        zip_path = backups_dir / f"{backup_id}.zip"
        with zipfile.ZipFile(zip_path, "w") as zf:
            meta = {
                "backup_id": backup_id,
                "backup_type": "manual",
                "workspace_id": workspace_id,
                "created_at": "2026-01-01T00:00:00+00:00",
                "chapter_id": None,
                "schema_version": "9999_future_schema",  # 字典序 > 当前 head
                "engine_version": "0.1.0",
                "contents": [],
            }
            zf.writestr("backup_meta.json", json.dumps(meta))

        with pytest.raises(RestoreVersionIncompatibleError) as exc_info:
            backup_service.restore_from_backup(workspace_id, backup_id)
        assert exc_info.value.reason == "backup_newer"

    def test_restore_version_incompatible_older(
        self,
        backup_service: BackupService,
        workspace_service: WorkspaceService,
        workspace_id: str,
    ) -> None:
        backups_dir = workspace_service.workspaces_root / workspace_id / "backups"
        backups_dir.mkdir(parents=True, exist_ok=True)
        backup_id = "manual_20260101T000000Z"
        zip_path = backups_dir / f"{backup_id}.zip"
        with zipfile.ZipFile(zip_path, "w") as zf:
            meta = {
                "backup_id": backup_id,
                "backup_type": "manual",
                "workspace_id": workspace_id,
                "created_at": "2026-01-01T00:00:00+00:00",
                "chapter_id": None,
                "schema_version": "0001_baseline",  # 字典序 < 当前 head
                "engine_version": "0.1.0",
                "contents": [],
            }
            zf.writestr("backup_meta.json", json.dumps(meta))

        with pytest.raises(RestoreVersionIncompatibleError) as exc_info:
            backup_service.restore_from_backup(workspace_id, backup_id)
        assert exc_info.value.reason == "backup_older"

    def test_forged_pre_migration_purpose_without_product_identity_is_rejected(
        self,
        backup_service: BackupService,
        workspace_service: WorkspaceService,
        workspace_id: str,
    ) -> None:
        backups_dir = workspace_service.workspaces_root / workspace_id / "backups"
        backups_dir.mkdir(parents=True, exist_ok=True)
        backup_id = "manual_20260101T000000Z"
        zip_path = backups_dir / f"{backup_id}.zip"
        with zipfile.ZipFile(zip_path, "w") as zf:
            meta = {
                "backup_id": backup_id,
                "backup_type": "manual",
                "workspace_id": workspace_id,
                "created_at": "2026-01-01T00:00:00+00:00",
                "chapter_id": None,
                "schema_version": "0001_baseline",
                "engine_version": "0.1.0",
                "contents": [],
                "purpose": "pre_migration",
            }
            zf.writestr("backup_meta.json", json.dumps(meta))

        with pytest.raises(
            BackupInvalidFormatError,
            match="专用 backup_id 不合法",
        ):
            backup_service.restore_from_backup(workspace_id, backup_id)

    @pytest.mark.parametrize(
        ("schema_version", "database_bytes", "message"),
        [
            (
                "0028_persistent_attempt_ledger",
                b"not-used-because-schema-is-stale",
                "只接受直接前序 0029",
            ),
            (
                "0029_structured_rewrite_constraint_results",
                b"forged-not-sqlite",
                "归档数据库验真失败",
            ),
        ],
    )
    def test_pre_migration_archive_requires_direct_exact_database(
        self,
        backup_service: BackupService,
        workspace_service: WorkspaceService,
        workspace_id: str,
        schema_version: str,
        database_bytes: bytes,
        message: str,
    ) -> None:
        backups_dir = workspace_service.workspaces_root / workspace_id / "backups"
        backups_dir.mkdir(parents=True, exist_ok=True)
        backup_id = "manual_20260101T000000Z_migration_deadbeef"
        zip_path = backups_dir / f"{backup_id}.zip"
        engine_version = workspace_service.info(workspace_id).engine_version
        backup_meta = {
            "backup_id": backup_id,
            "backup_type": "manual",
            "workspace_id": workspace_id,
            "created_at": "2026-01-01T00:00:00+00:00",
            "chapter_id": None,
            "schema_version": schema_version,
            "engine_version": engine_version,
            "contents": ["meta.json", "data/novel.db"],
            "purpose": "pre_migration",
        }
        workspace_meta = {
            "workspace_id": workspace_id,
            "schema_version": schema_version,
            "engine_version": engine_version,
        }
        with zipfile.ZipFile(zip_path, "w") as zf:
            zf.writestr("backup_meta.json", json.dumps(backup_meta))
            zf.writestr("meta.json", json.dumps(workspace_meta))
            zf.writestr("data/novel.db", database_bytes)

        with pytest.raises(BackupInvalidFormatError, match=message):
            backup_service.restore_from_backup(workspace_id, backup_id)

    def test_pre_migration_archive_rejects_current_shape_disguised_as_0029(
        self,
        backup_service: BackupService,
        workspace_service: WorkspaceService,
        workspace_id: str,
        tmp_path: Path,
    ) -> None:
        forged_db = tmp_path / "forged-current.db"
        engine = create_engine(f"sqlite:///{forged_db}")
        Base.metadata.create_all(engine)
        engine.dispose()
        backups_dir = workspace_service.workspaces_root / workspace_id / "backups"
        backups_dir.mkdir(parents=True, exist_ok=True)
        backup_id = "manual_20260101T000000Z_migration_cafebabe"
        schema_version = "0029_structured_rewrite_constraint_results"
        engine_version = workspace_service.info(workspace_id).engine_version
        with zipfile.ZipFile(backups_dir / f"{backup_id}.zip", "w") as zf:
            zf.writestr(
                "backup_meta.json",
                json.dumps(
                    {
                        "backup_id": backup_id,
                        "backup_type": "manual",
                        "workspace_id": workspace_id,
                        "created_at": "2026-01-01T00:00:00+00:00",
                        "chapter_id": None,
                        "schema_version": schema_version,
                        "engine_version": engine_version,
                        "contents": ["meta.json", "data/novel.db"],
                        "purpose": "pre_migration",
                    }
                ),
            )
            zf.writestr(
                "meta.json",
                json.dumps(
                    {
                        "workspace_id": workspace_id,
                        "schema_version": schema_version,
                        "engine_version": engine_version,
                    }
                ),
            )
            zf.write(forged_db, "data/novel.db")

        with pytest.raises(
            BackupInvalidFormatError,
            match="chapter_operation_leases",
        ):
            backup_service.restore_from_backup(workspace_id, backup_id)


# ---------------------------------------------------------------------------
# gate / WS event
# ---------------------------------------------------------------------------


class TestWSEvent:
    def test_publish_failure_does_not_break_backup(
        self,
        workspace_service: WorkspaceService,
        workspace_id: str,
    ) -> None:
        """ws_manager.publish 抛错 → swallow + structlog,不影响 backup 产出。"""
        ws = _FakeWSManager(raise_exc=RuntimeError("simulated ws failure"))
        service = BackupService(workspace_service=workspace_service, ws_manager=ws)
        _populate_workspace_data(workspace_service.workspaces_root, workspace_id)

        info = service.create_manual_backup(workspace_id)
        assert Path(info.file_path).is_file()
