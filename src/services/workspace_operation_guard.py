"""工作区级破坏性维护操作的单进程静默性保护。

``migrate`` 和 ``restore`` 都会改写工作区的持久状态。正常 API 路径必须先
确认本进程没有尚未结束的 task、正在运行的 pipeline，且数据库中没有章节
操作租约；否则在创建备份、执行 DDL 或覆盖文件前拒绝。CLI 不注入 task
checker，表示维护人已在离线进程中执行，因此不会把崩溃后遗留的
``pipeline_runs.state='running'`` 错当成仍存活的 Worker；章节操作租约仍然
属于必须人工对账的安全现场，任何入口都不能越过。
"""

from __future__ import annotations

import sqlite3
import threading
import fcntl
from collections.abc import Callable, Iterator
from contextlib import contextmanager
from pathlib import Path
from typing import BinaryIO, Literal

from src.services.memory_enrichment_worker_guard import (
    serialize_memory_enrichment_worker,
)


class WorkspaceOperationConflictError(RuntimeError):
    """工作区存在活跃操作或无法安全确认静默状态。"""

    def __init__(self, workspace_id: str, reason: str) -> None:
        self.workspace_id = workspace_id
        self.reason = reason
        super().__init__(
            f"工作区存在活跃操作，拒绝维护写入: workspace={workspace_id} "
            f"reason={reason}"
        )


_LOCKS_GUARD = threading.Lock()
_WORKSPACE_LOCKS: dict[tuple[str, str], threading.RLock] = {}
_RUNTIME_OWNERS_GUARD = threading.Lock()
_RUNTIME_OWNER_ROOTS: set[str] = set()
_MAINTENANCE_LOCAL = threading.local()


class WorkspaceFileLock:
    """跨进程 flock handle；调用方必须在持久操作结束后 ``close``。"""

    def __init__(self, file_handle: BinaryIO) -> None:
        self._file_handle = file_handle
        self._closed = False

    def close(self) -> None:
        if self._closed:
            return
        try:
            fcntl.flock(self._file_handle.fileno(), fcntl.LOCK_UN)
        finally:
            self._file_handle.close()
            self._closed = True


class RuntimeOwnerLock:
    """后端进程整个 lifespan 持有的单实例锁。"""

    def __init__(self, *, root_key: str, file_handle: BinaryIO) -> None:
        self._root_key = root_key
        self._file_handle = file_handle
        self._closed = False

    def close(self) -> None:
        if self._closed:
            return
        with _RUNTIME_OWNERS_GUARD:
            try:
                fcntl.flock(self._file_handle.fileno(), fcntl.LOCK_UN)
            finally:
                self._file_handle.close()
                _RUNTIME_OWNER_ROOTS.discard(self._root_key)
                self._closed = True


def _runtime_owner_lock_path(workspaces_root: Path) -> Path:
    lock_dir = workspaces_root.resolve() / ".operation_locks"
    lock_dir.mkdir(parents=True, exist_ok=True)
    return lock_dir / "runtime-owner.lock"


def acquire_runtime_owner_lock(workspaces_root: Path) -> RuntimeOwnerLock:
    """独占接管一个 data root；第二个后端在恢复写入前硬停。"""

    root_key = str(workspaces_root.resolve())
    with _RUNTIME_OWNERS_GUARD:
        if root_key in _RUNTIME_OWNER_ROOTS:
            raise WorkspaceOperationConflictError(
                "__runtime__",
                "runtime_owner_already_held_in_process",
            )
        file_handle = _runtime_owner_lock_path(workspaces_root).open("a+b")
        try:
            fcntl.flock(
                file_handle.fileno(),
                fcntl.LOCK_EX | fcntl.LOCK_NB,
            )
        except BlockingIOError as exc:
            file_handle.close()
            raise WorkspaceOperationConflictError(
                "__runtime__",
                "runtime_owner_held_by_another_process",
            ) from exc
        _RUNTIME_OWNER_ROOTS.add(root_key)
    return RuntimeOwnerLock(root_key=root_key, file_handle=file_handle)


def _acquire_runtime_maintenance_lock(
    workspaces_root: Path,
) -> WorkspaceFileLock | None:
    """离线维护持共享锁；本进程 API 已持 owner 时无需重复 flock。"""

    root_key = str(workspaces_root.resolve())
    with _RUNTIME_OWNERS_GUARD:
        if root_key in _RUNTIME_OWNER_ROOTS:
            return None
        file_handle = _runtime_owner_lock_path(workspaces_root).open("a+b")
        try:
            fcntl.flock(
                file_handle.fileno(),
                fcntl.LOCK_SH | fcntl.LOCK_NB,
            )
        except BlockingIOError as exc:
            file_handle.close()
            raise WorkspaceOperationConflictError(
                "__runtime__",
                "runtime_owner_active",
            ) from exc
    return WorkspaceFileLock(file_handle)


def acquire_workspace_file_lock(
    workspaces_root: Path,
    workspace_id: str,
    *,
    mode: Literal["shared", "exclusive"],
) -> WorkspaceFileLock:
    """非阻塞获取跨进程工作区锁，避免 UI Worker 与 CLI 维护并发。"""

    lock_dir = workspaces_root.resolve() / ".operation_locks"
    lock_dir.mkdir(parents=True, exist_ok=True)
    lock_path = lock_dir / f"{workspace_id}.lock"
    file_handle = lock_path.open("a+b")
    operation = fcntl.LOCK_SH if mode == "shared" else fcntl.LOCK_EX
    try:
        fcntl.flock(file_handle.fileno(), operation | fcntl.LOCK_NB)
    except BlockingIOError as exc:
        file_handle.close()
        raise WorkspaceOperationConflictError(
            workspace_id,
            f"cross_process_{mode}_lock_busy",
        ) from exc
    return WorkspaceFileLock(file_handle)


def _workspace_lock(workspaces_root: Path, workspace_id: str) -> threading.RLock:
    key = (str(workspaces_root.resolve()), workspace_id)
    with _LOCKS_GUARD:
        return _WORKSPACE_LOCKS.setdefault(key, threading.RLock())


@contextmanager
def workspace_operation_lock(
    workspaces_root: Path,
    workspace_id: str,
) -> Iterator[None]:
    """串行化同一进程、同一工作区的 migrate/restore 维护操作。"""

    key = (str(workspaces_root.resolve()), workspace_id)
    held_keys = getattr(_MAINTENANCE_LOCAL, "held_keys", None)
    if held_keys is None:
        held_keys = set()
        _MAINTENANCE_LOCAL.held_keys = held_keys
    lock = _workspace_lock(workspaces_root, workspace_id)
    with lock:
        if key in held_keys:
            yield
            return
        runtime_lock = _acquire_runtime_maintenance_lock(workspaces_root)
        try:
            # 与富记忆 Worker 统一采用 memory -> workspace 顺序。Worker 在
            # memory 锁内才可能开普通 shared DB session；维护先等它完整收口，
            # 再拿 workspace exclusive，避免双方反向持锁或 Chroma/SQLite
            # snapshot 被 Worker 从 session gap 穿越。
            with serialize_memory_enrichment_worker(
                workspaces_root,
                workspace_id,
            ):
                file_lock = acquire_workspace_file_lock(
                    workspaces_root,
                    workspace_id,
                    mode="exclusive",
                )
                try:
                    held_keys.add(key)
                    yield
                finally:
                    held_keys.discard(key)
                    file_lock.close()
        finally:
            if runtime_lock is not None:
                runtime_lock.close()


def ensure_workspace_quiescent(
    *,
    workspace_id: str,
    db_path: Path,
    active_task_checker: Callable[[str], bool] | None,
    check_running_pipeline: bool | None = None,
) -> None:
    """在任何维护写入前只读确认工作区没有活 Worker 或操作租约。

    ``active_task_checker is not None`` 表示正常 API 进程：除 task registry
    外还检查 ``pipeline_runs.state='running'``。CLI 不传 checker，避免把
    上个进程崩溃遗留、实际上已没有 Worker 的 running 行永久锁死在旧 schema。
    ``paused`` / ``interrupted`` 是可恢复 checkpoint，不是活 Worker。
    """

    if active_task_checker is not None:
        try:
            has_active_task = active_task_checker(workspace_id)
        except Exception as exc:  # noqa: BLE001
            raise WorkspaceOperationConflictError(
                workspace_id,
                f"task_registry_check_failed:{type(exc).__name__}:{exc}",
            ) from exc
        if has_active_task:
            raise WorkspaceOperationConflictError(
                workspace_id,
                "active_task",
            )

    if not db_path.is_file():
        return

    uri = f"{db_path.resolve().as_uri()}?mode=ro"
    try:
        connection = sqlite3.connect(uri, uri=True)
        try:
            connection.execute("PRAGMA query_only = ON")
            table_names = {
                str(row[0])
                for row in connection.execute(
                    "SELECT name FROM sqlite_master "
                    "WHERE type = 'table' AND name NOT GLOB 'sqlite_*'"
                )
            }
            should_check_pipeline = (
                active_task_checker is not None
                if check_running_pipeline is None
                else check_running_pipeline
            )
            if should_check_pipeline and "pipeline_runs" in table_names:
                running = connection.execute(
                    "SELECT run_id FROM pipeline_runs WHERE state = 'running' LIMIT 1"
                ).fetchone()
                if running is not None:
                    raise WorkspaceOperationConflictError(
                        workspace_id,
                        f"running_pipeline:{running[0]}",
                    )
            if "chapter_operation_leases" in table_names:
                lease_columns = {
                    str(row[1])
                    for row in connection.execute(
                        "PRAGMA table_info(chapter_operation_leases)"
                    )
                }
                lease = connection.execute(
                    (
                        "SELECT lease_id FROM chapter_operation_leases LIMIT 1"
                        if "lease_id" in lease_columns
                        else "SELECT 1 FROM chapter_operation_leases LIMIT 1"
                    )
                ).fetchone()
                if lease is not None:
                    raise WorkspaceOperationConflictError(
                        workspace_id,
                        f"active_chapter_operation_lease:{lease[0]}",
                    )
        finally:
            connection.close()
    except WorkspaceOperationConflictError:
        raise
    except sqlite3.Error as exc:
        if active_task_checker is None:
            # 离线 CLI 没有活 Worker；损坏数据库由 migrate 的严格 shape preflight
            # 或 restore 的 pre-restore 备份负责收口，不能让静默性辅助检查抢先
            # 把正常修复入口永久锁死。
            return
        raise WorkspaceOperationConflictError(
            workspace_id,
            f"sqlite_quiescence_check_failed:{type(exc).__name__}:{exc}",
        ) from exc


__all__ = [
    "RuntimeOwnerLock",
    "WorkspaceFileLock",
    "WorkspaceOperationConflictError",
    "acquire_runtime_owner_lock",
    "acquire_workspace_file_lock",
    "ensure_workspace_quiescent",
    "workspace_operation_lock",
]
