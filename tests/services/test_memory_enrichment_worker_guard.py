"""富记忆 workspace 串行锁回归；只用临时文件和线程，不调用 Provider。"""

from __future__ import annotations

import multiprocessing
import threading
from pathlib import Path
from queue import Empty

from src.services.memory_enrichment_worker_guard import (
    serialize_memory_enrichment_worker,
)


def _acquire_in_spawned_process(root: str, queue: object) -> None:
    with serialize_memory_enrichment_worker(Path(root), "ws-a"):
        queue.put("entered")  # type: ignore[attr-defined]


def test_same_workspace_workers_are_serialized(tmp_path: Path) -> None:
    first_entered = threading.Event()
    release_first = threading.Event()
    second_entered = threading.Event()
    order: list[str] = []

    def _first() -> None:
        with serialize_memory_enrichment_worker(tmp_path, "ws-a"):
            order.append("first-enter")
            first_entered.set()
            assert release_first.wait(timeout=2)
            order.append("first-exit")

    def _second() -> None:
        assert first_entered.wait(timeout=2)
        with serialize_memory_enrichment_worker(tmp_path, "ws-a"):
            order.append("second-enter")
            second_entered.set()

    first = threading.Thread(target=_first)
    second = threading.Thread(target=_second)
    first.start()
    second.start()
    assert first_entered.wait(timeout=2)
    assert not second_entered.wait(timeout=0.05)
    release_first.set()
    first.join(timeout=2)
    second.join(timeout=2)

    assert not first.is_alive()
    assert not second.is_alive()
    assert order == ["first-enter", "first-exit", "second-enter"]


def test_same_workspace_processes_are_serialized(tmp_path: Path) -> None:
    context = multiprocessing.get_context("spawn")
    queue = context.Queue()
    worker = context.Process(
        target=_acquire_in_spawned_process,
        args=(str(tmp_path), queue),
    )

    with serialize_memory_enrichment_worker(tmp_path, "ws-a"):
        worker.start()
        try:
            queue.get(timeout=0.2)
        except Empty:
            pass
        else:
            raise AssertionError("child process entered before parent released lock")

    assert queue.get(timeout=3) == "entered"
    worker.join(timeout=3)
    assert worker.exitcode == 0


def test_different_workspaces_do_not_block_each_other(tmp_path: Path) -> None:
    with serialize_memory_enrichment_worker(tmp_path, "ws-a"):
        entered = threading.Event()

        def _other_workspace() -> None:
            with serialize_memory_enrichment_worker(tmp_path, "ws-b"):
                entered.set()

        worker = threading.Thread(target=_other_workspace)
        worker.start()
        assert entered.wait(timeout=2)
        worker.join(timeout=2)
        assert not worker.is_alive()
