"""同工作区富记忆 Worker 的跨线程、跨进程串行锁。

正文主链允许下一章在上一章富记忆仍运行时继续，但 Character/Plot/Chroma
是工作区共享的 upsert 存储。若两个富记忆 Worker 同时写，崩溃恢复无法仅凭
单章 baseline 区分“本 Worker 副作用”和“另一章合法写入”。因此所有生产
富记忆 Worker 在来源验真、baseline 冻结和 Provider 调用之前先取得同工作区
独占锁，并持有到终态落库完成。进程崩溃时内核自动释放 flock；SQLite 中的
``running`` checkpoint 仍由冷启动安全收口。
"""

from __future__ import annotations

import fcntl
import threading
from collections.abc import Iterator
from contextlib import contextmanager
from pathlib import Path


_LOCKS_GUARD = threading.Lock()
_WORKSPACE_LOCKS: dict[tuple[str, str], threading.RLock] = {}


def _workspace_lock(workspaces_root: Path, workspace_id: str) -> threading.RLock:
    key = (str(workspaces_root.resolve()), workspace_id)
    with _LOCKS_GUARD:
        return _WORKSPACE_LOCKS.setdefault(key, threading.RLock())


@contextmanager
def serialize_memory_enrichment_worker(
    workspaces_root: Path,
    workspace_id: str,
) -> Iterator[None]:
    """阻塞等待同 workspace 前一个 Worker 完成，再进入富记忆主链。"""

    process_lock = _workspace_lock(workspaces_root, workspace_id)
    with process_lock:
        lock_dir = workspaces_root.resolve() / ".memory_worker_locks"
        lock_dir.mkdir(parents=True, exist_ok=True)
        lock_path = lock_dir / f"{workspace_id}.lock"
        with lock_path.open("a+b") as file_handle:
            fcntl.flock(file_handle.fileno(), fcntl.LOCK_EX)
            try:
                yield
            finally:
                fcntl.flock(file_handle.fileno(), fcntl.LOCK_UN)


__all__ = ["serialize_memory_enrichment_worker"]
