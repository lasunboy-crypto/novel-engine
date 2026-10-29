"""工作区备份 / 恢复服务(P6-B-07)。

对应 PROJECT_SPEC §11.3 字面"自动每章 APPROVED 后增量备份(只备 db + outline,
向量库可重建)+ 手动全量 + 保留最近 30 份自动 + 全部手动"。本任务为 v1 收官
落地"无数据丢失"机制(对齐 P6-acceptance 验收 #6 字面)。

设计要点
========

- **文件系统 SSOT**:备份元数据存 ``backups/<backup_id>.zip`` 自身 + 内嵌
  ``backup_meta.json``,**无 SQLite 表 / 无 Alembic 0010**。``list`` 走目录
  扫描 + 文件名解析(对齐 p6_plan §P6-B-07 字面"涉及文件"不含 ``alembic/
  versions/``;启动指令字面 0010 提示是接力预设,工程上不需要,按 plan §字
  面走 — 类 A 处置,impl 文档登记)。
- **三类备份**:
  - ``auto_{ts}_{chapter_id}.zip`` — 每章 approved 后自动跑(只 db + outline +
    cards,~50KB);30 份循环保留。
  - ``manual_{ts}.zip`` — 手动 ``POST :backup`` 全量(db + outline + cards +
    volumes + assets + chapters + meta.json);保留全部。
  - ``pre_restore_{ts}.zip`` — ``:restore`` 覆盖前自动备份原工作区(整
    workspace 目录,排除 ``backups/`` 自身防递归);内部用,不进 ``list``。
- **schema_version 通过 alembic ScriptDirectory 动态读取**:zip 内嵌
  ``backup_meta.json`` 字段 ``schema_version`` 存当前 Alembic head revision_id
  字符串(如 ``"0009_pipeline_runs_table"``);``restore`` 时与当前 head 比对,
  ``newer`` 抛 ``RESTORE_VERSION_INCOMPATIBLE`` 422,``older`` 同错码不同
  details 留 P6-B-08 migrate 流程接入(本任务返 422 + ``needs_migration:
  true`` marker)。
- **WS event**:``workspace.{workspace_id}.events`` 频道发 ``backup_created``
  / ``restore_completed`` 2 事件;publish 失败 swallow + structlog(对齐
  ChapterRewindService ``_publish_chapter_event`` 同型)。
"""

from __future__ import annotations

import json
import os
import re
import sqlite3
import shutil
import stat
import tempfile
import threading
import uuid
import zipfile
from collections.abc import Callable, Iterable, Iterator
from contextlib import contextmanager
from dataclasses import dataclass
from datetime import datetime, timezone
from pathlib import Path, PurePosixPath
from typing import Any, Literal

import structlog

from src.api.websocket.channels import workspace_events_channel
from src.core.models import WORKSPACE_ID_PATTERN
from src.core.workspace.meta import read_meta, write_meta
from src.core.workspace.workspace_paths import WorkspacePaths
from src.services.workspace_operation_guard import (
    WorkspaceOperationConflictError,
    acquire_workspace_file_lock,
    ensure_workspace_quiescent,
    workspace_operation_lock,
)
from src.services.workspace_service import WorkspaceService

_log = structlog.get_logger(__name__)

_AUTO_BACKUP_PREFIX = "auto_"
_MANUAL_BACKUP_PREFIX = "manual_"
_PRE_RESTORE_PREFIX = "pre_restore_"
_BACKUP_SUFFIX = ".zip"
_BACKUP_TIMESTAMP_FMT = "%Y%m%dT%H%M%SZ"
_BACKUP_META_FILENAME = "backup_meta.json"
_AUTO_BACKUP_KEEP_COUNT = 30
_RESTORE_TRANSACTION_DIRNAME = ".restore_transactions"
_RESTORE_MARKER_VERSION = 1
_RESTORE_TRANSACTION_STATES = frozenset(
    {"prepared", "original_moved", "swapped", "rollback_required"}
)
_FULL_RESTORE_REQUIRED_MEMBERS = frozenset(
    {
        "meta.json",
        "project.yaml",
        "data/novel.db",
        "data/outline/",
        "data/cards/",
        "prompts/",
        "data/chapters/",
        "data/chroma/",
    }
)
_MIGRATION_BACKUP_ID_RE = re.compile(r"^manual_\d{8}T\d{6}Z_migration_[0-9a-f]{8}$")
_RESTORABLE_BACKUP_ID_RE = re.compile(
    r"^(?:manual_\d{8}T\d{6}Z(?:_[0-9a-f]{8}|_migration_[0-9a-f]{8})?"
    r"|pre_restore_\d{8}T\d{6}Z(?:_[0-9a-f]{8})?)$"
)

BackupType = Literal["auto", "manual", "pre_restore"]

_BACKUP_LOCKS_GUARD = threading.Lock()
_BACKUP_LOCKS: dict[tuple[str, str], threading.RLock] = {}


# =============================================================================
# 服务异常
# =============================================================================


class BackupServiceError(Exception):
    """备份 / 恢复操作失败基类。"""


class BackupNotFoundError(BackupServiceError):
    """指定 ``backup_id`` 不在工作区 ``backups/`` 目录中(``:restore`` 404)。"""

    def __init__(self, workspace_id: str, backup_id: str) -> None:
        self.workspace_id = workspace_id
        self.backup_id = backup_id
        super().__init__(f"备份不存在: workspace={workspace_id} backup_id={backup_id}")


class BackupInvalidFormatError(BackupServiceError):
    """zip 结构损坏(``backup_meta.json`` 缺失 / 非合法 JSON / 字段缺失,
    或解压时 zip 损坏)。``:restore`` 翻 422 ``BACKUP_INVALID_FORMAT``。"""

    def __init__(self, workspace_id: str, backup_id: str, reason: str) -> None:
        self.workspace_id = workspace_id
        self.backup_id = backup_id
        self.reason = reason
        super().__init__(
            f"备份格式损坏: workspace={workspace_id} backup_id={backup_id} "
            f"reason={reason}"
        )


class RestoreVersionIncompatibleError(BackupServiceError):
    """zip 内 schema_version 与当前引擎 Alembic head 不匹配。

    422 ``RESTORE_VERSION_INCOMPATIBLE``:
    - ``backup_newer``:zip schema > 当前(用户 / 这台引擎过旧 → 升级引擎)
    - ``backup_older``:zip schema < 当前(留 P6-B-08 migrate 流程接入,本
      任务返 422 + ``needs_migration: true``)
    """

    def __init__(
        self,
        workspace_id: str,
        backup_id: str,
        backup_schema_version: str,
        current_schema_version: str,
        reason: Literal["backup_newer", "backup_older"],
    ) -> None:
        self.workspace_id = workspace_id
        self.backup_id = backup_id
        self.backup_schema_version = backup_schema_version
        self.current_schema_version = current_schema_version
        self.reason = reason
        super().__init__(
            f"备份版本不兼容: workspace={workspace_id} backup_id={backup_id} "
            f"backup_schema={backup_schema_version} "
            f"current_schema={current_schema_version} reason={reason}"
        )


# =============================================================================
# 出参 dataclass
# =============================================================================


@dataclass(frozen=True)
class BackupInfo:
    """单份备份的元数据(``GET /workspaces/{id}/backups`` 列表项)。"""

    backup_id: str
    backup_type: BackupType
    workspace_id: str
    created_at: datetime
    chapter_id: str | None
    schema_version: str
    engine_version: str
    file_path: str
    size_bytes: int
    purpose: str | None = None


@dataclass(frozen=True)
class RestoreResult:
    """``restore_from_backup`` 出参。"""

    workspace_id: str
    backup_id: str
    pre_restore_backup_id: str


@dataclass(frozen=True)
class PurgeBackupsResult:
    """``purge_backups`` 出参(v1.1 w3-s8 F1)。"""

    workspace_id: str
    deleted_count: int
    freed_bytes: int


def _workspace_backup_lock(
    workspaces_root: Path,
    workspace_id: str,
) -> threading.RLock:
    key = (str(workspaces_root.resolve()), workspace_id)
    with _BACKUP_LOCKS_GUARD:
        return _BACKUP_LOCKS.setdefault(key, threading.RLock())


@contextmanager
def _workspace_backup_read_lock(
    workspaces_root: Path,
    workspace_id: str,
) -> Iterator[None]:
    """让 auto/prune 与 destructive maintenance 使用同一锁序。"""

    file_lock = acquire_workspace_file_lock(
        workspaces_root,
        workspace_id,
        mode="shared",
    )
    try:
        with _workspace_backup_lock(workspaces_root, workspace_id):
            yield
    finally:
        file_lock.close()


def _restore_transaction_root(workspaces_root: Path) -> Path:
    return workspaces_root.resolve() / _RESTORE_TRANSACTION_DIRNAME


def _restore_transaction_paths(
    workspaces_root: Path,
    workspace_id: str,
    transaction_id: str,
) -> tuple[Path, Path, Path, Path]:
    transaction_root = _restore_transaction_root(workspaces_root)
    marker_path = transaction_root / f"{workspace_id}.json"
    candidate_path = transaction_root / (f"{workspace_id}.{transaction_id}.candidate")
    quarantine_path = transaction_root / (f"{workspace_id}.{transaction_id}.quarantine")
    return transaction_root, marker_path, candidate_path, quarantine_path


def _fsync_directory(path: Path) -> None:
    descriptor = os.open(path, os.O_RDONLY)
    try:
        os.fsync(descriptor)
    finally:
        os.close(descriptor)


def _fsync_tree(root: Path) -> None:
    """在 marker 宣布 candidate ready 前持久化普通文件与目录项。"""

    directories: list[Path] = []
    for current_root, dirnames, filenames in os.walk(root):
        current = Path(current_root)
        directories.append(current)
        for filename in filenames:
            file_path = current / filename
            if file_path.is_symlink():
                raise BackupServiceError(f"restore candidate 出现符号链接: {file_path}")
            with file_path.open("rb") as file_handle:
                os.fsync(file_handle.fileno())
        for dirname in dirnames:
            child = current / dirname
            if child.is_symlink():
                raise BackupServiceError(f"restore candidate 出现符号链接: {child}")
    for directory in reversed(directories):
        _fsync_directory(directory)


def _write_restore_marker(marker_path: Path, payload: dict[str, object]) -> None:
    marker_path.parent.mkdir(parents=True, exist_ok=True)
    temporary = marker_path.with_name(f".{marker_path.name}.{uuid.uuid4().hex}.tmp")
    encoded = json.dumps(
        payload,
        ensure_ascii=False,
        sort_keys=True,
    ).encode("utf-8")
    try:
        with temporary.open("xb") as file_handle:
            file_handle.write(encoded)
            file_handle.flush()
            os.fsync(file_handle.fileno())
        os.replace(temporary, marker_path)
        _fsync_directory(marker_path.parent)
    finally:
        temporary.unlink(missing_ok=True)


def _read_restore_marker(marker_path: Path) -> dict[str, str | int]:
    try:
        raw = json.loads(marker_path.read_text(encoding="utf-8"))
    except (OSError, UnicodeDecodeError, json.JSONDecodeError) as exc:
        raise BackupServiceError(
            f"restore transaction marker 损坏，保留现场并拒绝启动: {marker_path}"
        ) from exc
    if not isinstance(raw, dict):
        raise BackupServiceError(
            f"restore transaction marker 不是 object: {marker_path}"
        )
    expected_keys = {
        "version",
        "workspace_id",
        "transaction_id",
        "state",
        "backup_id",
        "schema_version",
    }
    if set(raw) != expected_keys:
        raise BackupServiceError(
            f"restore transaction marker 字段不完整或含未知字段: {marker_path}"
        )
    version = raw.get("version")
    workspace_id = raw.get("workspace_id")
    transaction_id = raw.get("transaction_id")
    state = raw.get("state")
    backup_id = raw.get("backup_id")
    schema_version = raw.get("schema_version")
    if version != _RESTORE_MARKER_VERSION:
        raise BackupServiceError(f"restore transaction marker 版本不支持: {version!r}")
    if (
        not isinstance(workspace_id, str)
        or re.fullmatch(WORKSPACE_ID_PATTERN, workspace_id) is None
    ):
        raise BackupServiceError("restore transaction workspace_id 非法")
    if (
        not isinstance(transaction_id, str)
        or re.fullmatch(r"[0-9a-f]{32}", transaction_id) is None
    ):
        raise BackupServiceError("restore transaction transaction_id 非法")
    if state not in _RESTORE_TRANSACTION_STATES:
        raise BackupServiceError(f"restore transaction state 非法: {state!r}")
    if not isinstance(backup_id, str) or not isinstance(schema_version, str):
        raise BackupServiceError("restore transaction 备份标识或 schema 非法")
    return {
        "version": version,
        "workspace_id": workspace_id,
        "transaction_id": transaction_id,
        "state": state,
        "backup_id": backup_id,
        "schema_version": schema_version,
    }


def _validate_restored_workspace_root(
    workspace_root: Path,
    *,
    workspace_id: str,
    schema_version: str,
) -> None:
    """删除原目录 quarantine 前再次验真已交换的候选目录。"""

    from src.services.workspace_version_service import (
        get_current_schema_version,
        validate_current_restore_database,
        validate_r8_migration_rollback_database,
    )

    workspace = read_meta(workspace_root / "meta.json")
    if (
        workspace.workspace_id != workspace_id
        or workspace.schema_version != schema_version
    ):
        raise BackupServiceError(
            "restore candidate meta 身份或 schema 与 marker 不一致"
        )
    db_path = workspace_root / "data" / "novel.db"
    if schema_version == get_current_schema_version():
        validate_current_restore_database(
            db_path,
            declared_schema=schema_version,
            expected_workspace_id=workspace_id,
        )
    else:
        validate_r8_migration_rollback_database(
            db_path,
            declared_schema=schema_version,
            expected_workspace_id=workspace_id,
        )


def _unlink_restore_marker(marker_path: Path) -> None:
    marker_path.unlink(missing_ok=True)
    if marker_path.parent.is_dir():
        _fsync_directory(marker_path.parent)


def _recover_restore_marker(
    workspaces_root: Path,
    marker_path: Path,
) -> str:
    marker = _read_restore_marker(marker_path)
    workspace_id = str(marker["workspace_id"])
    transaction_id = str(marker["transaction_id"])
    state = str(marker["state"])
    schema_version = str(marker["schema_version"])
    transaction_root, expected_marker, candidate, quarantine = (
        _restore_transaction_paths(
            workspaces_root,
            workspace_id,
            transaction_id,
        )
    )
    if marker_path.resolve() != expected_marker.resolve():
        raise BackupServiceError(
            "restore transaction marker 文件名与 workspace_id 不一致"
        )
    workspace_root = workspaces_root.resolve() / workspace_id

    if state in {"prepared", "rollback_required"}:
        if quarantine.exists():
            if workspace_root.exists():
                rollback_candidate = candidate
                if rollback_candidate.exists():
                    shutil.rmtree(rollback_candidate)
                os.replace(workspace_root, rollback_candidate)
                _fsync_directory(workspaces_root.resolve())
                _fsync_directory(transaction_root)
            os.replace(quarantine, workspace_root)
            _fsync_directory(workspaces_root.resolve())
            _fsync_directory(transaction_root)
        elif not workspace_root.exists():
            raise BackupServiceError(
                "restore rollback 缺少原工作区与 quarantine，保留现场并拒绝启动"
            )
        if candidate.exists():
            shutil.rmtree(candidate)
        _unlink_restore_marker(marker_path)
        return workspace_id

    if not workspace_root.exists():
        if candidate.exists():
            os.replace(candidate, workspace_root)
            _fsync_directory(workspaces_root.resolve())
            _fsync_directory(transaction_root)
        elif quarantine.exists():
            # candidate 丢失时只能回滚原目录，不能制造半恢复工作区。
            os.replace(quarantine, workspace_root)
            _fsync_directory(workspaces_root.resolve())
            _fsync_directory(transaction_root)
            _unlink_restore_marker(marker_path)
            return workspace_id
        else:
            raise BackupServiceError(
                "restore transaction 缺少 workspace/candidate/quarantine"
            )

    try:
        _validate_restored_workspace_root(
            workspace_root,
            workspace_id=workspace_id,
            schema_version=schema_version,
        )
    except Exception:
        if not quarantine.exists():
            raise
        rollback_payload = dict(marker)
        rollback_payload["state"] = "rollback_required"
        _write_restore_marker(marker_path, rollback_payload)
        if candidate.exists():
            shutil.rmtree(candidate)
        os.replace(workspace_root, candidate)
        _fsync_directory(workspaces_root.resolve())
        _fsync_directory(transaction_root)
        os.replace(quarantine, workspace_root)
        _fsync_directory(workspaces_root.resolve())
        _fsync_directory(transaction_root)
        shutil.rmtree(candidate)
        _unlink_restore_marker(marker_path)
        return workspace_id

    if quarantine.exists():
        shutil.rmtree(quarantine)
        _fsync_directory(transaction_root)
    if candidate.exists():
        shutil.rmtree(candidate)
        _fsync_directory(transaction_root)
    _unlink_restore_marker(marker_path)
    return workspace_id


def recover_interrupted_workspace_restores(
    workspaces_root: Path,
    *,
    workspace_id: str | None = None,
) -> list[str]:
    """在 startup admission 前或下次 restore 时本地收口目录交换。"""

    root = workspaces_root.resolve()
    transaction_root = _restore_transaction_root(root)
    if not transaction_root.is_dir():
        return []
    markers = sorted(transaction_root.glob("*.json"))
    if workspace_id is not None:
        markers = [
            marker for marker in markers if marker.name == f"{workspace_id}.json"
        ]
    recovered: list[str] = []
    referenced_artifacts: set[Path] = set()
    for marker_path in markers:
        marker = _read_restore_marker(marker_path)
        marker_workspace_id = str(marker["workspace_id"])
        transaction_id = str(marker["transaction_id"])
        _, _, candidate, quarantine = _restore_transaction_paths(
            root,
            marker_workspace_id,
            transaction_id,
        )
        referenced_artifacts.update({candidate.resolve(), quarantine.resolve()})
        with workspace_operation_lock(root, marker_workspace_id):
            recovered.append(_recover_restore_marker(root, marker_path))

    unexpected = [
        entry
        for entry in transaction_root.iterdir()
        if entry.suffix != ".json"
        and not entry.name.startswith(".")
        and entry.resolve() not in referenced_artifacts
        and (workspace_id is None or entry.name.startswith(f"{workspace_id}."))
    ]
    if unexpected:
        raise BackupServiceError(
            "发现无 durable marker 的 restore transaction 现场，"
            f"拒绝继续: {[entry.name for entry in unexpected]!r}"
        )
    return recovered


# =============================================================================
# 服务类
# =============================================================================


class BackupService:
    """工作区备份 / 恢复服务。

    实例化时绑定 ``WorkspaceService``(共用 ``workspaces_root``)+ 可选
    ``ws_manager``(为 ``None`` 时 publish noop,与 ChapterRewindService 同
    模式)。

    5 个公共方法 + 4 个内部辅助:

    - ``create_auto_backup(workspace_id, chapter_id)`` — 自动备份(只 db +
      outline + cards),完成后跑 ``prune_old_auto_backups``。
    - ``create_manual_backup(workspace_id)`` — 手动全量备份。
    - ``restore_from_backup(workspace_id, backup_id, confirm)`` — 解压临时
      目录 → 校验 schema_version → 原工作区 pre_restore 备份 → 覆盖。
    - ``list_backups(workspace_id)`` — 列表(最近 30 份 auto + 全部 manual,
      不含 pre_restore)。
    - ``prune_old_auto_backups(workspace_id, keep=30)`` — 删除超出 ``keep``
      数的最老自动备份。
    """

    def __init__(
        self,
        *,
        workspace_service: WorkspaceService,
        ws_manager: object | None = None,
        active_task_checker: Callable[[str], bool] | None = None,
        restart_required_marker: Callable[[str], None] | None = None,
    ) -> None:
        self._workspace_service = workspace_service
        self._ws_manager = ws_manager
        self._active_task_checker = active_task_checker
        self._restart_required_marker = restart_required_marker

    # ---- 公共方法 ---------------------------------------------------------

    def create_auto_backup(
        self,
        workspace_id: str,
        chapter_id: str,
    ) -> BackupInfo:
        """每章 approved 后自动备份(只 db + outline + cards,~50KB)。

        chapter_id 拼进文件名(``auto_{ts}_{chapter_id}.zip``)便于事后回溯
        是哪一章触发。完成后跑 ``prune_old_auto_backups`` 保留最近 30 份。

        - 工作区不存在 → ``BackupServiceError``
        """
        with _workspace_backup_read_lock(
            self._workspace_service.workspaces_root,
            workspace_id,
        ):
            return self._create_auto_backup_locked(workspace_id, chapter_id)

    def _create_auto_backup_locked(
        self,
        workspace_id: str,
        chapter_id: str,
    ) -> BackupInfo:
        paths = self._validate_workspace(workspace_id)
        timestamp = self._now_compact()
        backup_id = f"{_AUTO_BACKUP_PREFIX}{timestamp}_{chapter_id}"
        zip_path = paths.backups_dir / f"{backup_id}{_BACKUP_SUFFIX}"

        paths.backups_dir.mkdir(parents=True, exist_ok=True)
        with self._consistent_database_snapshot(
            paths,
            purpose="auto",
        ) as snapshot_path:
            self._write_zip(
                zip_path=zip_path,
                paths=paths,
                backup_id=backup_id,
                backup_type="auto",
                workspace_id=workspace_id,
                chapter_id=chapter_id,
                include_full=False,
                database_snapshot=snapshot_path,
            )

        # prune 失败 swallow + log,不影响主备份产出
        try:
            self._prune_old_auto_backups_locked(workspace_id)
        except Exception as exc:  # noqa: BLE001
            _log.warning(
                "backup_prune_failed",
                workspace_id=workspace_id,
                error_type=type(exc).__name__,
                error_message=str(exc),
            )

        info = self._read_backup_info(zip_path)
        self._publish_workspace_event(
            workspace_id=workspace_id,
            event="backup_created",
            data={
                "workspace_id": workspace_id,
                "backup_id": backup_id,
                "backup_type": "auto",
                "chapter_id": chapter_id,
                "file_path": str(zip_path),
            },
        )
        _log.info(
            "backup_created",
            workspace_id=workspace_id,
            backup_id=backup_id,
            backup_type="auto",
            chapter_id=chapter_id,
        )
        return info

    def create_manual_backup(self, workspace_id: str) -> BackupInfo:
        """在 workspace/memory 静默锁内创建手动全量备份。"""

        try:
            with workspace_operation_lock(
                self._workspace_service.workspaces_root,
                workspace_id,
            ):
                paths = self._validate_workspace(workspace_id)
                ensure_workspace_quiescent(
                    workspace_id=workspace_id,
                    db_path=paths.db_path,
                    active_task_checker=self._active_task_checker,
                )
                with _workspace_backup_lock(
                    self._workspace_service.workspaces_root,
                    workspace_id,
                ):
                    return self._create_manual_backup_locked(workspace_id)
        except WorkspaceOperationConflictError as exc:
            raise BackupServiceError(str(exc)) from exc

    def _create_manual_backup_locked(self, workspace_id: str) -> BackupInfo:
        """手动全量备份(db + outline + cards + volumes + assets + chapters
        + meta.json)。

        - 工作区不存在 → ``BackupServiceError``
        """
        paths = self._validate_workspace(workspace_id)
        timestamp = self._now_compact()
        backup_id = f"{_MANUAL_BACKUP_PREFIX}{timestamp}_{uuid.uuid4().hex[:8]}"
        zip_path = paths.backups_dir / f"{backup_id}{_BACKUP_SUFFIX}"

        paths.backups_dir.mkdir(parents=True, exist_ok=True)
        with self._consistent_database_snapshot(
            paths,
            purpose="manual",
        ) as snapshot_path:
            self._write_zip(
                zip_path=zip_path,
                paths=paths,
                backup_id=backup_id,
                backup_type="manual",
                workspace_id=workspace_id,
                chapter_id=None,
                include_full=True,
                database_snapshot=snapshot_path,
            )

        info = self._read_backup_info(zip_path)
        self._publish_workspace_event(
            workspace_id=workspace_id,
            event="backup_created",
            data={
                "workspace_id": workspace_id,
                "backup_id": backup_id,
                "backup_type": "manual",
                "chapter_id": None,
                "file_path": str(zip_path),
            },
        )
        _log.info(
            "backup_created",
            workspace_id=workspace_id,
            backup_id=backup_id,
            backup_type="manual",
        )
        return info

    def create_migration_backup(
        self,
        workspace_id: str,
        *,
        source_schema_version: str | None,
        source_engine_version: str,
    ) -> BackupInfo:
        """在统一维护锁内创建可回滚的迁移前全量快照。"""

        try:
            with workspace_operation_lock(
                self._workspace_service.workspaces_root,
                workspace_id,
            ):
                paths = self._validate_workspace(workspace_id)
                ensure_workspace_quiescent(
                    workspace_id=workspace_id,
                    db_path=paths.db_path,
                    active_task_checker=self._active_task_checker,
                )
                with _workspace_backup_lock(
                    self._workspace_service.workspaces_root,
                    workspace_id,
                ):
                    return self._create_migration_backup_locked(
                        workspace_id,
                        source_schema_version=source_schema_version,
                        source_engine_version=source_engine_version,
                    )
        except WorkspaceOperationConflictError as exc:
            raise BackupServiceError(str(exc)) from exc

    def _create_migration_backup_locked(
        self,
        workspace_id: str,
        *,
        source_schema_version: str | None,
        source_engine_version: str,
    ) -> BackupInfo:
        """为受控迁移创建带真实来源版本的一致性全量备份。

        普通文件继续按 manual 全量口径收集；若 ``novel.db`` 存在，则通过
        SQLite online backup API 把主文件与 WAL 中已提交事务合成一个临时
        snapshot 后入 zip，避免直接复制主文件漏掉已提交数据。
        """

        paths = self._validate_workspace(workspace_id)
        timestamp = self._now_compact()
        backup_id = (
            f"{_MANUAL_BACKUP_PREFIX}{timestamp}_migration_{uuid.uuid4().hex[:8]}"
        )
        zip_path = paths.backups_dir / f"{backup_id}{_BACKUP_SUFFIX}"
        paths.backups_dir.mkdir(parents=True, exist_ok=True)

        schema_version = source_schema_version or "unknown"
        with self._consistent_database_snapshot(
            paths,
            purpose="migration",
        ) as snapshot_path:
            self._write_zip(
                zip_path=zip_path,
                paths=paths,
                backup_id=backup_id,
                backup_type="manual",
                workspace_id=workspace_id,
                chapter_id=None,
                include_full=True,
                database_snapshot=snapshot_path,
                schema_version_override=schema_version,
                engine_version_override=source_engine_version,
                purpose="pre_migration",
                workspace_meta_overrides={
                    "schema_version": schema_version,
                    "engine_version": source_engine_version,
                },
            )

        info = self._read_backup_info(zip_path)
        self._publish_workspace_event(
            workspace_id=workspace_id,
            event="backup_created",
            data={
                "workspace_id": workspace_id,
                "backup_id": backup_id,
                "backup_type": "manual",
                "chapter_id": None,
                "file_path": str(zip_path),
                "purpose": "pre_migration",
                "source_schema_version": schema_version,
            },
        )
        _log.info(
            "backup_created",
            workspace_id=workspace_id,
            backup_id=backup_id,
            backup_type="manual",
            purpose="pre_migration",
            source_schema_version=schema_version,
        )
        return info

    def list_backups(self, workspace_id: str) -> list[BackupInfo]:
        """列举本工作区的备份(最近 30 份 auto + 全部 manual,按 ``created_at``
        倒序;不含 ``pre_restore_*``)。

        - 工作区不存在 → ``BackupServiceError``
        - ``backups/`` 目录不存在或为空 → 返 ``[]``
        - 单个 zip 损坏 → 静默跳过 + structlog warn(避免一份坏数据让整体
          列表失败,对齐 P1-B-14 ``list`` 元数据缺失静默跳过模式)
        """
        paths = self._validate_workspace(workspace_id)
        if not paths.backups_dir.is_dir():
            return []

        auto: list[BackupInfo] = []
        manual: list[BackupInfo] = []
        for entry in sorted(paths.backups_dir.iterdir()):
            if not entry.is_file() or entry.suffix != _BACKUP_SUFFIX:
                continue
            name = entry.name
            if name.startswith(_PRE_RESTORE_PREFIX):
                continue
            if not (
                name.startswith(_AUTO_BACKUP_PREFIX)
                or name.startswith(_MANUAL_BACKUP_PREFIX)
            ):
                continue
            try:
                info = self._read_backup_info(entry)
            except BackupInvalidFormatError as exc:
                _log.warning(
                    "backup_list_skipped_invalid",
                    workspace_id=workspace_id,
                    backup_id=exc.backup_id,
                    reason=exc.reason,
                )
                continue
            if info.backup_type == "auto":
                auto.append(info)
            elif info.backup_type == "manual":
                manual.append(info)

        auto.sort(key=lambda x: x.created_at, reverse=True)
        manual.sort(key=lambda x: x.created_at, reverse=True)
        return [*auto[:_AUTO_BACKUP_KEEP_COUNT], *manual]

    def prune_old_auto_backups(
        self,
        workspace_id: str,
        *,
        keep: int = _AUTO_BACKUP_KEEP_COUNT,
    ) -> int:
        """删除超出 ``keep`` 份的最老自动备份,返回删除数量。

        ``keep`` 默认 30(对齐 plan §字面 + spec §11.3)。``manual_*`` /
        ``pre_restore_*`` 不动。
        """
        with _workspace_backup_read_lock(
            self._workspace_service.workspaces_root,
            workspace_id,
        ):
            return self._prune_old_auto_backups_locked(
                workspace_id,
                keep=keep,
            )

    def _prune_old_auto_backups_locked(
        self,
        workspace_id: str,
        *,
        keep: int = _AUTO_BACKUP_KEEP_COUNT,
    ) -> int:
        paths = self._validate_workspace(workspace_id)
        if not paths.backups_dir.is_dir():
            return 0

        autos: list[tuple[datetime, Path]] = []
        for entry in paths.backups_dir.iterdir():
            if (
                entry.is_file()
                and entry.suffix == _BACKUP_SUFFIX
                and entry.name.startswith(_AUTO_BACKUP_PREFIX)
            ):
                # 优先用 zip 内 backup_meta.json created_at;损坏 fallback mtime
                try:
                    info = self._read_backup_info(entry)
                    created_at = info.created_at
                except BackupInvalidFormatError:
                    created_at = datetime.fromtimestamp(
                        entry.stat().st_mtime, tz=timezone.utc
                    )
                autos.append((created_at, entry))

        autos.sort(key=lambda x: x[0], reverse=True)
        to_delete = autos[keep:]
        for _, path in to_delete:
            path.unlink(missing_ok=True)
        if to_delete:
            _log.info(
                "backup_pruned",
                workspace_id=workspace_id,
                deleted_count=len(to_delete),
                kept=min(len(autos), keep),
            )
        return len(to_delete)

    def purge_backups(self, workspace_id: str) -> PurgeBackupsResult:
        """删除工作区 ``backups/`` 下全部备份 zip,返回删除份数 + 释放字节(v1.1 w3-s8 F1)。

        范围:``auto_*`` + ``manual_*`` + ``pre_restore_*`` 三类已知前缀的 ``.zip``
        (与 ``list_backups`` / ``prune_old_auto_backups`` 同前缀白名单,不误删
        backups/ 下其他文件)。维护人 UI 一键清理释放磁盘用 —— ``prune_old_auto_backups``
        只削超额 auto,manual 永不自动清;本方法补"全清"入口。

        破坏性操作,二次确认(``confirm == workspace_id``)由路由层做,对齐
        ``:restore`` 防呆模式。

        - 工作区不存在 → ``BackupServiceError``(``_validate_workspace`` 抛)
        - ``backups/`` 目录不存在或无备份 → ``deleted_count=0 / freed_bytes=0``
        - 单文件 ``stat`` 失败 → 该份不计字节但仍删除(fail-open,不阻断整体清理)
        """
        try:
            with workspace_operation_lock(
                self._workspace_service.workspaces_root,
                workspace_id,
            ):
                paths = self._validate_workspace(workspace_id)
                ensure_workspace_quiescent(
                    workspace_id=workspace_id,
                    db_path=paths.db_path,
                    active_task_checker=self._active_task_checker,
                )
                with _workspace_backup_lock(
                    self._workspace_service.workspaces_root,
                    workspace_id,
                ):
                    return self._purge_backups_locked(workspace_id, paths=paths)
        except WorkspaceOperationConflictError as exc:
            raise BackupServiceError(str(exc)) from exc

    def _purge_backups_locked(
        self,
        workspace_id: str,
        *,
        paths: WorkspacePaths,
    ) -> PurgeBackupsResult:
        if not paths.backups_dir.is_dir():
            return PurgeBackupsResult(
                workspace_id=workspace_id, deleted_count=0, freed_bytes=0
            )

        known_prefixes = (
            _AUTO_BACKUP_PREFIX,
            _MANUAL_BACKUP_PREFIX,
            _PRE_RESTORE_PREFIX,
        )
        deleted_count = 0
        freed_bytes = 0
        for entry in paths.backups_dir.iterdir():
            if not entry.is_file() or entry.suffix != _BACKUP_SUFFIX:
                continue
            if not entry.name.startswith(known_prefixes):
                continue
            try:
                freed_bytes += entry.stat().st_size
            except OSError:
                pass
            entry.unlink(missing_ok=True)
            deleted_count += 1

        if deleted_count:
            _log.info(
                "backups_purged",
                workspace_id=workspace_id,
                deleted_count=deleted_count,
                freed_bytes=freed_bytes,
            )
        return PurgeBackupsResult(
            workspace_id=workspace_id,
            deleted_count=deleted_count,
            freed_bytes=freed_bytes,
        )

    def restore_from_backup(
        self,
        workspace_id: str,
        backup_id: str,
    ) -> RestoreResult:
        """在工作区维护锁和静默性硬门内执行备份恢复。"""

        try:
            with workspace_operation_lock(
                self._workspace_service.workspaces_root,
                workspace_id,
            ):
                recover_interrupted_workspace_restores(
                    self._workspace_service.workspaces_root,
                    workspace_id=workspace_id,
                )
                paths = self._validate_workspace(workspace_id)
                ensure_workspace_quiescent(
                    workspace_id=workspace_id,
                    db_path=paths.db_path,
                    active_task_checker=self._active_task_checker,
                    check_running_pipeline=True,
                )
                with _workspace_backup_lock(
                    self._workspace_service.workspaces_root,
                    workspace_id,
                ):
                    result = self._restore_from_backup_locked(
                        workspace_id,
                        backup_id,
                        paths=paths,
                    )
                if self._restart_required_marker is not None:
                    self._restart_required_marker(workspace_id)
                return result
        except WorkspaceOperationConflictError as exc:
            raise BackupServiceError(str(exc)) from exc

    def _restore_from_backup_locked(
        self,
        workspace_id: str,
        backup_id: str,
        *,
        paths: WorkspacePaths,
    ) -> RestoreResult:
        """从工作区 ``backups/{backup_id}.zip`` 恢复工作区。

        步骤:
          1. 校验 zip 存在 + ``backup_meta.json`` 合法
          2. 校验 schema_version 与当前 Alembic head 匹配
          3. 解压到临时目录(``backups/.restore_staging_{ts}/``)校验完整性
          4. 原工作区 ``pre_restore_{ts}.zip`` 全量备份(整 workspace 目录,
             排除 ``backups/`` 自身防递归)
          5. 删除工作区现有内容(保留 ``backups/`` 目录)
          6. 把临时目录内容覆盖工作区
          7. WS publish ``restore_completed``

        - 工作区不存在 → ``BackupServiceError``
        - backup_id 对应 zip 不存在 → ``BackupNotFoundError``
        - zip 损坏 / backup_meta.json 不合法 → ``BackupInvalidFormatError``
        - schema_version 不兼容 → ``RestoreVersionIncompatibleError``
        """
        if _RESTORABLE_BACKUP_ID_RE.fullmatch(backup_id) is None:
            raise BackupInvalidFormatError(
                workspace_id,
                backup_id,
                "backup_id 非法或不是可全量恢复的 manual/pre_restore 备份",
            )
        zip_path = paths.backups_dir / f"{backup_id}{_BACKUP_SUFFIX}"
        if zip_path.resolve().parent != paths.backups_dir.resolve():
            raise BackupInvalidFormatError(
                workspace_id,
                backup_id,
                "backup_id 路径越界",
            )
        if not zip_path.is_file():
            raise BackupNotFoundError(workspace_id, backup_id)

        self._validate_zip_member_safety(
            zip_path,
            workspace_id=workspace_id,
            backup_id=backup_id,
        )
        info = self._read_backup_info(zip_path)
        if info.backup_id != backup_id or info.workspace_id != workspace_id:
            raise BackupInvalidFormatError(
                workspace_id,
                backup_id,
                "backup_meta.json 的 backup_id/workspace_id 与恢复目标不一致",
            )
        current_schema = self._get_current_schema_version()
        if info.backup_type == "auto":
            raise BackupInvalidFormatError(
                workspace_id,
                backup_id,
                "auto 是增量取证备份，禁止按全量 restore 删除工作区",
            )
        migration_rollback = False
        if info.schema_version != current_schema:
            migration_rollback = self._is_verified_migration_rollback(
                zip_path=zip_path,
                info=info,
            )
        if info.schema_version != current_schema and not (
            migration_rollback and info.schema_version < current_schema
        ):
            reason: Literal["backup_newer", "backup_older"] = (
                "backup_newer"
                if info.schema_version > current_schema
                else "backup_older"
            )
            raise RestoreVersionIncompatibleError(
                workspace_id=workspace_id,
                backup_id=backup_id,
                backup_schema_version=info.schema_version,
                current_schema_version=current_schema,
                reason=reason,
            )
        if info.schema_version == current_schema:
            self._validate_current_restore_archive(
                zip_path=zip_path,
                info=info,
            )
        self._validate_full_restore_archive(zip_path=zip_path, info=info)

        # 候选目录、quarantine 与 durable marker 均放在工作区目录之外、同一
        # workspaces 文件系统内。只有候选完整落盘并验真后才交换目录。
        transaction_id = uuid.uuid4().hex
        transaction_root, marker_path, candidate_dir, quarantine_dir = (
            _restore_transaction_paths(
                self._workspace_service.workspaces_root,
                workspace_id,
                transaction_id,
            )
        )
        marker_payload: dict[str, object] = {
            "version": _RESTORE_MARKER_VERSION,
            "workspace_id": workspace_id,
            "transaction_id": transaction_id,
            "state": "prepared",
            "backup_id": backup_id,
            "schema_version": info.schema_version,
        }
        transaction_root.mkdir(parents=True, exist_ok=True)
        if marker_path.exists() or candidate_dir.exists() or quarantine_dir.exists():
            raise BackupServiceError(
                "restore transaction 现场未完成，拒绝开始新的目录交换"
            )

        try:
            candidate_dir.mkdir(parents=True, exist_ok=False)
            try:
                with zipfile.ZipFile(zip_path, "r") as zf:
                    self._validate_zip_infos(
                        zf,
                        workspace_id=workspace_id,
                        backup_id=backup_id,
                    )
                    zf.extractall(candidate_dir)
            except zipfile.BadZipFile as exc:
                raise BackupInvalidFormatError(
                    workspace_id, backup_id, f"zip 解压失败: {exc}"
                ) from exc
            (candidate_dir / _BACKUP_META_FILENAME).unlink(missing_ok=True)
            if (candidate_dir / paths.backups_dir.name).exists():
                raise BackupInvalidFormatError(
                    workspace_id,
                    backup_id,
                    "恢复归档不得内嵌 backups/ 目录",
                )

            # pre_restore 备份整 workspace，作为任何交换后的回滚锚。
            pre_timestamp = self._now_compact()
            pre_backup_id = (
                f"{_PRE_RESTORE_PREFIX}{pre_timestamp}_{uuid.uuid4().hex[:8]}"
            )
            pre_zip_path = paths.backups_dir / f"{pre_backup_id}{_BACKUP_SUFFIX}"
            if paths.db_path.is_file():
                with tempfile.TemporaryDirectory(
                    prefix="novel-engine-pre-restore-backup-"
                ) as temp_dir:
                    snapshot_path = Path(temp_dir) / "novel.db"
                    try:
                        self._snapshot_sqlite_database(
                            paths.db_path,
                            snapshot_path,
                        )
                    except BackupServiceError as exc:
                        # restore 也是损坏 DB 的修复入口。无法用 SQLite API
                        # snapshot 时仍在 workspace+memory 静默锁内保留原始
                        # main/WAL/SHM 文件，不能因“旧库已坏”丢掉 pre_restore
                        # 现场或反过来阻止恢复。
                        _log.warning(
                            "pre_restore_sqlite_snapshot_fallback_raw",
                            workspace_id=workspace_id,
                            error_type=type(exc).__name__,
                            error_message=str(exc),
                        )
                        snapshot_path = paths.db_path
                    self._write_zip(
                        zip_path=pre_zip_path,
                        paths=paths,
                        backup_id=pre_backup_id,
                        backup_type="pre_restore",
                        workspace_id=workspace_id,
                        chapter_id=None,
                        include_full=True,
                        include_backups=False,
                        database_snapshot=snapshot_path,
                    )
            else:
                self._write_zip(
                    zip_path=pre_zip_path,
                    paths=paths,
                    backup_id=pre_backup_id,
                    backup_type="pre_restore",
                    workspace_id=workspace_id,
                    chapter_id=None,
                    include_full=True,
                    include_backups=False,
                )

            # 候选目录必须保留原工作区全部备份，包括刚创建的 pre_restore。
            shutil.copytree(
                paths.backups_dir,
                candidate_dir / paths.backups_dir.name,
            )
            candidate_meta = candidate_dir / paths.meta_json.name
            workspace = read_meta(candidate_meta).model_copy(
                update={"last_active_at": self._now()}
            )
            write_meta(candidate_meta, workspace)
            _validate_restored_workspace_root(
                candidate_dir,
                workspace_id=workspace_id,
                schema_version=info.schema_version,
            )
            _fsync_tree(candidate_dir)
            _write_restore_marker(marker_path, marker_payload)

            # 两次同文件系统 rename 中间由 marker + quarantine 覆盖；任何
            # 崩溃点都能在下次 startup admission 前确定提交或回滚。
            os.replace(paths.root, quarantine_dir)
            _fsync_directory(self._workspace_service.workspaces_root)
            _fsync_directory(transaction_root)
            marker_payload["state"] = "original_moved"
            _write_restore_marker(marker_path, marker_payload)

            os.replace(candidate_dir, paths.root)
            _fsync_directory(self._workspace_service.workspaces_root)
            _fsync_directory(transaction_root)
            marker_payload["state"] = "swapped"
            _write_restore_marker(marker_path, marker_payload)
            _validate_restored_workspace_root(
                paths.root,
                workspace_id=workspace_id,
                schema_version=info.schema_version,
            )

            shutil.rmtree(quarantine_dir)
            _fsync_directory(transaction_root)
            _unlink_restore_marker(marker_path)
        except Exception:
            if marker_path.exists():
                # 正常异常也走与冷启动相同的确定性收口；若现场本身损坏，保留
                # marker/quarantine 并把错误上抛，禁止继续普通工作区写入。
                _recover_restore_marker(
                    self._workspace_service.workspaces_root,
                    marker_path,
                )
            elif candidate_dir.exists():
                shutil.rmtree(candidate_dir, ignore_errors=True)
            raise

        # v1.0.x A2-01:工作区内 chroma/ 已被候选目录替换,清缓存让下次
        # vector_store factory 按新文件重建,避免 ChromaDB SharedSystemClient
        # 仍指向旧实例触发 ValueError。lazy import 避开 deps.py ↔ backup_service
        # 潜在循环(deps.py 已 import BackupService)。
        try:
            from src.api.deps import reset_vector_store_cache

            reset_vector_store_cache(workspace_id)
        except Exception as exc:  # noqa: BLE001
            _log.warning(
                "vector_store_cache_reset_failed",
                workspace_id=workspace_id,
                error_type=type(exc).__name__,
                error_message=str(exc),
            )

        self._publish_workspace_event(
            workspace_id=workspace_id,
            event="restore_completed",
            data={
                "workspace_id": workspace_id,
                "backup_id": backup_id,
                "pre_restore_backup_id": pre_backup_id,
            },
        )
        _log.info(
            "restore_completed",
            workspace_id=workspace_id,
            backup_id=backup_id,
            pre_restore_backup_id=pre_backup_id,
        )
        return RestoreResult(
            workspace_id=workspace_id,
            backup_id=backup_id,
            pre_restore_backup_id=pre_backup_id,
        )

    # ---- 内部辅助 ---------------------------------------------------------

    def _validate_workspace(self, workspace_id: str) -> WorkspacePaths:
        """通过 ``WorkspaceService.info`` 校验工作区存在。"""
        try:
            self._workspace_service.info(workspace_id)
        except Exception as exc:  # noqa: BLE001 — 工作区 IO 异常域统一转 BackupServiceError
            raise BackupServiceError(
                f"工作区不存在或 meta.json 损坏: {workspace_id}: {exc}"
            ) from exc
        return WorkspacePaths(
            workspace_id,
            workspaces_root=self._workspace_service.workspaces_root,
        )

    @staticmethod
    def _now() -> datetime:
        return datetime.now(timezone.utc)

    @classmethod
    def _now_compact(cls) -> str:
        """``YYYYMMDDTHHMMSSZ`` 紧凑时间戳(对齐 P1-B-14 backup 同格式)。"""
        return cls._now().strftime(_BACKUP_TIMESTAMP_FMT)

    @staticmethod
    def _get_current_schema_version() -> str:
        """读当前 Alembic head revision_id 字符串。

        延迟导入 ``alembic.script.ScriptDirectory`` 避免顶层依赖(alembic
        在测试 fixture 路径里 lazy load)。脚本目录推断:仓库根 ``alembic/``
        相对 ``src/services/backup_service.py`` 上推 3 层。
        """
        from alembic.script import ScriptDirectory

        # src/services/backup_service.py -> repo root -> alembic/
        repo_root = Path(__file__).resolve().parents[2]
        script_location = repo_root / "alembic"
        script_dir = ScriptDirectory(str(script_location))
        head = script_dir.get_current_head()
        if head is None:
            # 极端情况(alembic/versions/ 空)— 用 fallback 标记;生产路径不
            # 应触发(P6 期 head 至少 0009_pipeline_runs_table)。
            return "unknown"
        return head

    def _write_zip(
        self,
        *,
        zip_path: Path,
        paths: WorkspacePaths,
        backup_id: str,
        backup_type: BackupType,
        workspace_id: str,
        chapter_id: str | None,
        include_full: bool,
        include_backups: bool = False,
        database_snapshot: Path | None = None,
        schema_version_override: str | None = None,
        engine_version_override: str | None = None,
        purpose: str | None = None,
        workspace_meta_overrides: dict[str, Any] | None = None,
    ) -> None:
        """把工作区内容打 zip 到 ``zip_path``。

        - ``include_full=False``(自动备份):只 db + outline + cards + meta.json
        - ``include_full=True``(手动 + pre_restore):上 + Chroma + volumes +
          assets + chapters 全部存在的子目录
        - ``include_backups=False`` 时**永不**打包 ``backups/`` 子目录(防递归;
          pre_restore 默认行为)
        """
        live_workspace = read_meta(paths.meta_json)
        contents: list[str] = []
        with zipfile.ZipFile(zip_path, "w", compression=zipfile.ZIP_DEFLATED) as zf:
            # workspace meta.json
            if paths.meta_json.is_file():
                if workspace_meta_overrides is None:
                    zf.write(paths.meta_json, arcname="meta.json")
                else:
                    workspace_meta = json.loads(
                        paths.meta_json.read_text(encoding="utf-8")
                    )
                    workspace_meta.update(workspace_meta_overrides)
                    zf.writestr(
                        "meta.json",
                        json.dumps(
                            workspace_meta,
                            ensure_ascii=False,
                            indent=2,
                        ),
                    )
                contents.append("meta.json")

            # data/novel.db(SQLite)
            database_source = (
                database_snapshot if database_snapshot is not None else paths.db_path
            )
            if database_source.is_file():
                zf.write(
                    database_source,
                    arcname=str(paths.db_path.relative_to(paths.root)),
                )
                contents.append(str(paths.db_path.relative_to(paths.root)))
                if database_snapshot is None or database_source == paths.db_path:
                    for suffix in ("-wal", "-shm"):
                        sidecar = Path(f"{paths.db_path}{suffix}")
                        if sidecar.is_file():
                            arcname = str(sidecar.relative_to(paths.root))
                            zf.write(sidecar, arcname=arcname)
                            contents.append(arcname)

            # data/outline/ 与 data/cards/ — 自动 + 手动 + pre_restore 都打包
            for sub in (paths.outline_dir, paths.cards_dir):
                if sub.is_dir():
                    self._add_directory(zf, sub, paths.root)
                    contents.append(f"{sub.relative_to(paths.root)}/")

            if include_full:
                # project.yaml 与工作区 prompt 覆盖决定 provider/model/成本护栏
                # 和写作合同；full restore 若遗漏会静默换运行配置。
                if paths.project_yaml.is_file():
                    project_arcname = str(paths.project_yaml.relative_to(paths.root))
                    zf.write(paths.project_yaml, arcname=project_arcname)
                    contents.append(project_arcname)
                else:
                    # 无 workspace override 等价空 mapping；归档仍显式携带该
                    # 关键配置位，restore 不接受“漏打包后退回全局默认”。
                    zf.writestr("project.yaml", "{}\n")
                    contents.append("project.yaml")
                required_directories = (
                    paths.prompts_dir,
                    paths.outline_dir,
                    paths.cards_dir,
                    paths.chapters_dir,
                    paths.chroma_dir,
                )
                missing_directories = [
                    directory
                    for directory in required_directories
                    if not directory.is_dir()
                ]
                if missing_directories:
                    raise BackupServiceError(
                        "全量备份缺少产品必要目录: "
                        f"{[str(path) for path in missing_directories]!r}"
                    )
                self._add_directory(zf, paths.prompts_dir, paths.root)
                contents.append(f"{paths.prompts_dir.relative_to(paths.root)}/")
                # 富记忆 Chroma 是工作区持久现场。迁移与 restore 调用本函数时
                # 已同时持 workspace exclusive 与 memory-worker 锁，因此可把
                # SQLite/WAL 等整个目录作为同一静默快照收入归档。
                self._add_directory(zf, paths.chroma_dir, paths.root)
                contents.append(f"{paths.chroma_dir.relative_to(paths.root)}/")
                # data/volumes/ — 一般不存在(P5 起 volumes 走 SQLite),保留
                # 兼容 plan §字面"volumes" 字面以防有未来文件式备援。
                volumes_dir = paths.data_dir / "volumes"
                if volumes_dir.is_dir():
                    self._add_directory(zf, volumes_dir, paths.root)
                    contents.append(f"{volumes_dir.relative_to(paths.root)}/")
                # 其余资产 / 章节正文
                for sub in (paths.assets_dir, paths.chapters_dir):
                    if sub.is_dir():
                        self._add_directory(zf, sub, paths.root)
                        contents.append(f"{sub.relative_to(paths.root)}/")

            if include_backups and paths.backups_dir.is_dir():
                # 仅 pre_restore 不调,本分支保留供未来全量包含 backups/ 用
                self._add_directory(zf, paths.backups_dir, paths.root)
                contents.append(f"{paths.backups_dir.relative_to(paths.root)}/")

            # ``contents`` 是归档真实 member 的精确清单，而非目录级摘要；
            # restore 会逐项比对，防止元数据自报完整但 zip 实际漏项。
            contents = sorted(zf.namelist())

            # backup_meta.json
            backup_meta: dict[str, Any] = {
                "backup_id": backup_id,
                "backup_type": backup_type,
                "workspace_id": workspace_id,
                "created_at": self._now().isoformat(),
                "chapter_id": chapter_id,
                "schema_version": (
                    schema_version_override
                    if schema_version_override is not None
                    else live_workspace.schema_version
                ),
                "engine_version": (
                    engine_version_override
                    if engine_version_override is not None
                    else live_workspace.engine_version
                ),
                "contents": contents,
            }
            if purpose is not None:
                backup_meta["purpose"] = purpose
            zf.writestr(
                _BACKUP_META_FILENAME,
                json.dumps(backup_meta, ensure_ascii=False, indent=2),
            )

    @staticmethod
    def _snapshot_sqlite_database(source_path: Path, target_path: Path) -> None:
        """用 SQLite backup API 生成含 WAL 已提交事务的一致性快照。"""

        source_uri = f"{source_path.resolve().as_uri()}?mode=ro"
        try:
            source = sqlite3.connect(source_uri, uri=True)
            try:
                source.execute("PRAGMA query_only = ON")
                target = sqlite3.connect(target_path)
                try:
                    source.backup(target)
                finally:
                    target.close()
            finally:
                source.close()
        except sqlite3.Error as exc:
            raise BackupServiceError(
                f"SQLite 一致性备份失败: {type(exc).__name__}: {exc}"
            ) from exc

    @contextmanager
    def _consistent_database_snapshot(
        self,
        paths: WorkspacePaths,
        *,
        purpose: str,
    ) -> Iterator[Path | None]:
        """优先 online backup；损坏库的非迁移备份退回原始 main/WAL/SHM。"""

        if not paths.db_path.is_file():
            yield None
            return
        with tempfile.TemporaryDirectory(
            prefix=f"novel-engine-{purpose}-backup-"
        ) as temp_dir:
            snapshot_path = Path(temp_dir) / "novel.db"
            try:
                self._snapshot_sqlite_database(paths.db_path, snapshot_path)
            except BackupServiceError as exc:
                if purpose == "migration":
                    raise
                _log.warning(
                    "sqlite_snapshot_fallback_raw",
                    workspace_id=paths.workspace_id,
                    purpose=purpose,
                    error_type=type(exc).__name__,
                    error_message=str(exc),
                )
                yield paths.db_path
                return
            yield snapshot_path

    @classmethod
    def _validate_zip_member_safety(
        cls,
        zip_path: Path,
        *,
        workspace_id: str,
        backup_id: str,
    ) -> None:
        try:
            with zipfile.ZipFile(zip_path, "r") as zf:
                cls._validate_zip_infos(
                    zf,
                    workspace_id=workspace_id,
                    backup_id=backup_id,
                )
        except zipfile.BadZipFile as exc:
            raise BackupInvalidFormatError(
                workspace_id,
                backup_id,
                f"zip 文件损坏: {exc}",
            ) from exc

    @staticmethod
    def _validate_zip_infos(
        zf: zipfile.ZipFile,
        *,
        workspace_id: str,
        backup_id: str,
    ) -> None:
        seen: set[str] = set()
        for info in zf.infolist():
            member = PurePosixPath(info.filename)
            if (
                not info.filename
                or member.is_absolute()
                or ".." in member.parts
                or info.filename in seen
            ):
                raise BackupInvalidFormatError(
                    workspace_id,
                    backup_id,
                    f"zip member 路径非法或重复: {info.filename!r}",
                )
            seen.add(info.filename)
            file_type = stat.S_IFMT(info.external_attr >> 16)
            if file_type not in {0, stat.S_IFREG, stat.S_IFDIR}:
                raise BackupInvalidFormatError(
                    workspace_id,
                    backup_id,
                    f"zip member 不是普通文件/目录: {info.filename!r}",
                )

    @staticmethod
    def _validate_full_restore_archive(
        *,
        zip_path: Path,
        info: BackupInfo,
    ) -> None:
        """确认 full 必需项存在且 ``contents`` 与真实 zip members 精确一致。"""

        try:
            with zipfile.ZipFile(zip_path, "r") as zf:
                backup_meta = json.loads(zf.read(_BACKUP_META_FILENAME).decode("utf-8"))
                contents = backup_meta.get("contents")
                if not isinstance(contents, list) or not all(
                    isinstance(item, str) for item in contents
                ):
                    raise ValueError("contents 必须是字符串 list")
                if len(contents) != len(set(contents)):
                    raise ValueError("contents 含重复 member")
                actual = {
                    member.filename
                    for member in zf.infolist()
                    if member.filename != _BACKUP_META_FILENAME
                }
                if set(contents) != actual:
                    raise ValueError(
                        "contents 与 zip 实际 members 不一致: "
                        f"missing={sorted(set(contents) - actual)!r} "
                        f"undeclared={sorted(actual - set(contents))!r}"
                    )
                missing_required = _FULL_RESTORE_REQUIRED_MEMBERS - actual
                if missing_required:
                    raise ValueError(
                        f"全量恢复归档缺少必要 member: {sorted(missing_required)!r}"
                    )
                if any(
                    member == "backups/" or member.startswith("backups/")
                    for member in actual
                ):
                    raise ValueError("全量恢复归档不得内嵌 backups/")
        except (
            KeyError,
            json.JSONDecodeError,
            UnicodeDecodeError,
            ValueError,
            zipfile.BadZipFile,
            OSError,
        ) as exc:
            raise BackupInvalidFormatError(
                info.workspace_id,
                info.backup_id,
                f"full restore manifest 验真失败: {exc}",
            ) from exc

    @staticmethod
    def _validate_current_restore_archive(
        *,
        zip_path: Path,
        info: BackupInfo,
    ) -> None:
        from src.services.workspace_version_service import (
            WorkspaceVersionServiceError,
            validate_current_restore_database,
        )

        try:
            with zipfile.ZipFile(zip_path, "r") as zf:
                backup_meta = json.loads(zf.read(_BACKUP_META_FILENAME).decode("utf-8"))
                workspace_meta = json.loads(zf.read("meta.json").decode("utf-8"))
                contents = backup_meta.get("contents")
                if not isinstance(contents, list):
                    raise ValueError("contents 不是 list")
                database_entry = "data/novel.db"
                if database_entry not in contents:
                    raise ValueError("current restore 缺少 data/novel.db 声明")
                expected = (
                    info.workspace_id,
                    info.schema_version,
                    info.engine_version,
                )
                actual = (
                    workspace_meta.get("workspace_id"),
                    workspace_meta.get("schema_version"),
                    workspace_meta.get("engine_version"),
                )
                if actual != expected:
                    raise ValueError("workspace meta 与 backup meta 不一致")
                with tempfile.TemporaryDirectory(
                    prefix="novel-engine-current-restore-audit-"
                ) as temp_dir:
                    snapshot_path = Path(temp_dir) / "novel.db"
                    with zf.open(database_entry, "r") as source:
                        with snapshot_path.open("wb") as target:
                            shutil.copyfileobj(source, target)
                    validate_current_restore_database(
                        snapshot_path,
                        declared_schema=info.schema_version,
                        expected_workspace_id=info.workspace_id,
                    )
        except (
            KeyError,
            json.JSONDecodeError,
            UnicodeDecodeError,
            ValueError,
            zipfile.BadZipFile,
            OSError,
            WorkspaceVersionServiceError,
        ) as exc:
            raise BackupInvalidFormatError(
                info.workspace_id,
                info.backup_id,
                f"current restore 归档验真失败: {exc}",
            ) from exc

    @staticmethod
    def _is_verified_migration_rollback(
        *,
        zip_path: Path,
        info: BackupInfo,
    ) -> bool:
        """确认旧 schema 迁移归档具有可安全恢复的结构证据。

        ``purpose`` 只是入口条件；还必须满足专用 ID、manual 类型，以及归档
        内 ``meta.json`` 的工作区/版本与 ``backup_meta.json`` 一致。普通手动
        备份或只伪造一个 purpose 字段都不能借此跨 schema 恢复。归档内证据
        只能证明内部自洽与结构安全，不声称具备外部签名或来源防伪能力。
        """

        if info.purpose != "pre_migration":
            return False
        if (
            info.backup_type != "manual"
            or _MIGRATION_BACKUP_ID_RE.fullmatch(info.backup_id) is None
        ):
            raise BackupInvalidFormatError(
                info.workspace_id,
                info.backup_id,
                "pre_migration 备份类型或专用 backup_id 不合法",
            )
        try:
            with zipfile.ZipFile(zip_path, "r") as zf:
                backup_meta = json.loads(zf.read(_BACKUP_META_FILENAME).decode("utf-8"))
                workspace_meta = json.loads(zf.read("meta.json").decode("utf-8"))
        except (KeyError, json.JSONDecodeError, UnicodeDecodeError) as exc:
            raise BackupInvalidFormatError(
                info.workspace_id,
                info.backup_id,
                f"pre_migration 归档缺少或损坏版本证据: {exc}",
            ) from exc

        contents = backup_meta.get("contents")
        if not isinstance(contents, list) or "meta.json" not in contents:
            raise BackupInvalidFormatError(
                info.workspace_id,
                info.backup_id,
                "pre_migration contents 未声明 meta.json",
            )
        expected = (
            info.workspace_id,
            info.schema_version,
            info.engine_version,
        )
        actual = (
            workspace_meta.get("workspace_id"),
            workspace_meta.get("schema_version"),
            workspace_meta.get("engine_version"),
        )
        if actual != expected:
            raise BackupInvalidFormatError(
                info.workspace_id,
                info.backup_id,
                "pre_migration 工作区 meta 与备份来源版本不一致",
            )
        from src.services.workspace_version_service import (
            WorkspaceVersionServiceError,
            validate_r8_migration_rollback_database,
        )

        if info.schema_version != "0029_structured_rewrite_constraint_results":
            raise BackupInvalidFormatError(
                info.workspace_id,
                info.backup_id,
                "pre_migration 回滚只接受直接前序 0029",
            )
        database_entry = "data/novel.db"
        if database_entry not in contents:
            raise BackupInvalidFormatError(
                info.workspace_id,
                info.backup_id,
                "pre_migration contents 未声明 data/novel.db",
            )
        try:
            with tempfile.TemporaryDirectory(
                prefix="novel-engine-rollback-audit-"
            ) as temp_dir:
                snapshot_path = Path(temp_dir) / "novel.db"
                with zipfile.ZipFile(zip_path, "r") as zf:
                    with zf.open(database_entry, "r") as source:
                        with snapshot_path.open("wb") as target:
                            shutil.copyfileobj(source, target)
                validate_r8_migration_rollback_database(
                    snapshot_path,
                    declared_schema=info.schema_version,
                    expected_workspace_id=info.workspace_id,
                )
        except (KeyError, zipfile.BadZipFile, OSError) as exc:
            raise BackupInvalidFormatError(
                info.workspace_id,
                info.backup_id,
                f"pre_migration novel.db 无法安全读取: {exc}",
            ) from exc
        except WorkspaceVersionServiceError as exc:
            raise BackupInvalidFormatError(
                info.workspace_id,
                info.backup_id,
                str(exc),
            ) from exc
        return True

    @staticmethod
    def _add_directory(zf: zipfile.ZipFile, directory: Path, base: Path) -> None:
        """递归加入目录项和普通文件，空目录也可被 manifest 验真。"""

        root_arcname = f"{directory.relative_to(base).as_posix()}/"
        zf.mkdir(root_arcname)
        for child in sorted(directory.rglob("*")):
            arcname = child.relative_to(base).as_posix()
            if child.is_symlink():
                raise BackupServiceError(f"备份源目录含符号链接，拒绝归档: {child}")
            if child.is_dir():
                zf.mkdir(f"{arcname}/")
            elif child.is_file():
                zf.write(child, arcname=arcname)

    @staticmethod
    def _read_backup_info(zip_path: Path) -> BackupInfo:
        """从 zip 读 ``backup_meta.json`` 构造 ``BackupInfo``。

        zip 损坏 / 缺 backup_meta.json / 字段缺失 → 抛 ``BackupInvalidFormatError``。
        """
        backup_id = zip_path.stem
        # 路径布局:workspaces/{id}/backups/{backup_id}.zip → workspace_id =
        # backups 父目录名
        workspace_id_hint = zip_path.parent.parent.name
        try:
            with zipfile.ZipFile(zip_path, "r") as zf:
                try:
                    meta_bytes = zf.read(_BACKUP_META_FILENAME)
                except KeyError as exc:
                    raise BackupInvalidFormatError(
                        workspace_id_hint,
                        backup_id,
                        f"zip 缺少 {_BACKUP_META_FILENAME}",
                    ) from exc
        except zipfile.BadZipFile as exc:
            raise BackupInvalidFormatError(
                workspace_id_hint, backup_id, f"zip 文件损坏: {exc}"
            ) from exc

        try:
            meta = json.loads(meta_bytes.decode("utf-8"))
        except (json.JSONDecodeError, UnicodeDecodeError) as exc:
            raise BackupInvalidFormatError(
                workspace_id_hint,
                backup_id,
                f"{_BACKUP_META_FILENAME} 非合法 JSON: {exc}",
            ) from exc

        required = (
            "backup_id",
            "backup_type",
            "workspace_id",
            "created_at",
            "schema_version",
        )
        missing = [k for k in required if k not in meta]
        if missing:
            raise BackupInvalidFormatError(
                workspace_id_hint,
                backup_id,
                f"{_BACKUP_META_FILENAME} 缺字段: {missing}",
            )

        try:
            created_at = datetime.fromisoformat(meta["created_at"])
        except (TypeError, ValueError) as exc:
            raise BackupInvalidFormatError(
                workspace_id_hint,
                backup_id,
                f"created_at 字段非合法 ISO 时间: {exc}",
            ) from exc

        return BackupInfo(
            backup_id=meta["backup_id"],
            backup_type=meta["backup_type"],
            workspace_id=meta["workspace_id"],
            created_at=created_at,
            chapter_id=meta.get("chapter_id"),
            schema_version=meta["schema_version"],
            engine_version=meta.get("engine_version", "unknown"),
            file_path=str(zip_path),
            size_bytes=zip_path.stat().st_size,
            purpose=meta.get("purpose"),
        )

    def _publish_workspace_event(
        self,
        *,
        workspace_id: str,
        event: str,
        data: dict[str, Any],
    ) -> None:
        """WS publish 到 ``workspace.{workspace_id}.events``;失败 swallow + log。

        对齐 ``ChapterRewindService._publish_chapter_event`` 同型(structlog
        ``event`` 首参冲突 → 业务 event 改名 ``ws_event``)。
        """
        if self._ws_manager is None:
            return
        try:
            self._ws_manager.publish(  # type: ignore[attr-defined]
                channel=workspace_events_channel(workspace_id),
                event=event,
                data=data,
            )
        except Exception as exc:  # noqa: BLE001
            _log.error(
                "backup_event_notify_failed",
                workspace_id=workspace_id,
                ws_event=event,
                error_type=type(exc).__name__,
                error_message=str(exc),
            )


# Iterable re-export 占位(防 ruff F401 — 实际 dataclass list 输出由路由层
# 拼装,本模块只暴露公共 5 方法 + dataclass)
_ = Iterable

__all__ = [
    "BackupInfo",
    "BackupInvalidFormatError",
    "BackupNotFoundError",
    "BackupService",
    "BackupServiceError",
    "BackupType",
    "RestoreResult",
    "RestoreVersionIncompatibleError",
]
