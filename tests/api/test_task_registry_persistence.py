"""task_registry SQLite 持久化单测(v1.1 wave 3 session 5 = E3)。

覆盖:
- 持久化往返(record → 新工厂/新"进程"仍可 get → 模拟重启不丢)
- 重启恢复扫描(pending/running → interrupted;终态行不动)
- 容量软上限淘汰(超 _MAX_TASKS 删最旧)
- get miss / clear / 幂等 re-record / workspace_id 落库
- 独立 TaskBase metadata 不污染主 Base(alembic 不受影响的护栏)
"""

from __future__ import annotations

from collections.abc import Iterator
from pathlib import Path

import pytest

from src.api import task_registry
from src.services.task_db import make_task_session_factory


@pytest.fixture()
def isolated_db(tmp_path: Path) -> Iterator[Path]:
    """把 task_registry 指到本用例独占 tmp ``_tasks.db``(覆盖全局 autouse 隔离,
    便于本测试自己控制'同一文件、新工厂'模拟重启)。"""
    root = tmp_path / "tasks_root"
    task_registry.set_session_factory(make_task_session_factory(data_dir=root))
    yield root
    task_registry.reset_session_factory()


def test_persist_round_trip(isolated_db: Path) -> None:
    tid = task_registry.new_task_id()
    task_registry.record_pending(tid, type="chapter_write", workspace_id="ws-1")
    info = task_registry.get(tid)
    assert info is not None
    assert info.status == "pending"
    assert info.progress == 0
    assert info.created_at.tzinfo is not None  # tz-aware 回填


def test_survives_new_factory_same_file(isolated_db: Path) -> None:
    """同一 _tasks.db 文件、重建工厂(模拟进程重启)后任务仍在。"""
    tid = task_registry.new_task_id()
    task_registry.record_done(tid, type="chapter_write", result={"v": 1})

    # 模拟"重启":重置 + 用同一 data_dir 重建工厂。
    task_registry.reset_session_factory()
    task_registry.set_session_factory(make_task_session_factory(data_dir=isolated_db))

    info = task_registry.get(tid)
    assert info is not None
    assert info.status == "done"
    assert info.result == {"v": 1}


def test_startup_scan_marks_interrupted(isolated_db: Path) -> None:
    pending_id = task_registry.new_task_id()
    done_id = task_registry.new_task_id()
    failed_id = task_registry.new_task_id()
    task_registry.record_pending(pending_id, type="outline_generate")
    task_registry.record_done(done_id, type="chapter_write", result={})
    task_registry.record_failed(failed_id, type="chapter_write", error={"code": "X"})

    marked = task_registry.mark_running_as_interrupted_on_startup()
    assert marked == 1  # 只有 pending 行被翻

    interrupted = task_registry.get(pending_id)
    assert interrupted is not None
    assert interrupted.status == "interrupted"
    assert interrupted.error == {
        "code": "TASK_INTERRUPTED",
        "reason": "engine_restarted_mid_run",
    }
    # 终态行不受影响
    assert task_registry.get(done_id).status == "done"
    assert task_registry.get(failed_id).status == "failed"


def test_startup_scan_idempotent(isolated_db: Path) -> None:
    """第二次扫描无残留未终态行 → 返回 0,不重复翻已 interrupted 行。"""
    tid = task_registry.new_task_id()
    task_registry.record_pending(tid, type="chapter_write")
    assert task_registry.mark_running_as_interrupted_on_startup() == 1
    assert task_registry.mark_running_as_interrupted_on_startup() == 0
    assert task_registry.get(tid).status == "interrupted"


def test_user_cancelled_checkpoint_requires_exact_workspace_type_and_terminal_shape(
    isolated_db: Path,
) -> None:
    tid = task_registry.new_task_id()
    task_registry.record_pending(
        tid,
        type="chapter_write",
        workspace_id="ws-r9-v6",
    )
    assert task_registry.cancel_task(tid) is False
    task_registry.update_to_cancelled(tid)

    assert task_registry.is_user_cancelled_task_checkpoint(
        tid,
        "ws-r9-v6",
        "chapter_write",
    )
    assert not task_registry.is_user_cancelled_task_checkpoint(
        tid,
        "ws-other",
        "chapter_write",
    )
    assert not task_registry.is_user_cancelled_task_checkpoint(
        tid,
        "ws-r9-v6",
        "chapter_rewrite",
    )

    failed_id = task_registry.new_task_id()
    task_registry.record_failed(
        failed_id,
        type="chapter_write",
        workspace_id="ws-r9-v6",
        error={"code": "TASK_CANCELLED", "reason": "user_requested"},
    )
    assert not task_registry.is_user_cancelled_task_checkpoint(
        failed_id,
        "ws-r9-v6",
        "chapter_write",
    )


def test_startup_scan_propagates_corrupt_task_database(tmp_path: Path) -> None:
    from sqlalchemy.exc import DatabaseError

    root = tmp_path / "corrupt_tasks"
    root.mkdir()
    (root / "_tasks.db").write_bytes(b"not a sqlite database")
    task_registry.set_session_factory(make_task_session_factory(data_dir=root))
    try:
        with pytest.raises(DatabaseError):
            task_registry.mark_running_as_interrupted_on_startup()
    finally:
        task_registry.reset_session_factory()


def test_startup_scan_propagates_commit_failure(isolated_db: Path) -> None:
    from sqlalchemy import create_engine
    from sqlalchemy.orm import Session

    task_id = task_registry.new_task_id()
    task_registry.record_pending(task_id, type="chapter_write")
    engine = create_engine(f"sqlite:///{isolated_db / '_tasks.db'}")

    class _CommitFailSession(Session):
        def commit(self) -> None:
            raise RuntimeError("simulated task startup commit failure")

    task_registry.set_session_factory(lambda: _CommitFailSession(engine))
    try:
        with pytest.raises(RuntimeError, match="startup commit failure"):
            task_registry.mark_running_as_interrupted_on_startup()
    finally:
        engine.dispose()


def test_update_missing_returns_none(isolated_db: Path) -> None:
    assert task_registry.update_to_done("task_nope", result={}) is None
    assert task_registry.update_to_failed("task_nope", error={}) is None
    assert task_registry.get("task_nope") is None


def test_re_record_same_id_upserts(isolated_db: Path) -> None:
    tid = task_registry.new_task_id()
    task_registry.record_pending(tid, type="chapter_write")
    # 同 id 再 record_done(典型:同步路径直接 done)→ upsert 不报错
    info = task_registry.record_done(tid, type="chapter_write", result={"k": 1})
    assert info.status == "done"
    assert task_registry.get(tid).result == {"k": 1}


def test_record_pending_if_no_active_reuses_existing_task(
    isolated_db: Path,
) -> None:
    """同 workspace/type 的第二次 reserve 不落新行，返回原 active task。"""
    first_id = task_registry.new_task_id()
    second_id = task_registry.new_task_id()

    first, first_created = task_registry.record_pending_if_no_active(
        first_id,
        type="outline_generate",
        workspace_id="ws-r8",
    )
    second, second_created = task_registry.record_pending_if_no_active(
        second_id,
        type="outline_generate",
        workspace_id="ws-r8",
    )

    assert first_created is True
    assert first.task_id == first_id
    assert second_created is False
    assert second.task_id == first_id
    assert task_registry.get(second_id) is None


def test_list_done_tasks_can_include_legacy_unbound_rows(
    isolated_db: Path,
) -> None:
    bound_id = task_registry.new_task_id()
    legacy_id = task_registry.new_task_id()
    other_id = task_registry.new_task_id()
    task_registry.record_done(
        bound_id,
        type="outline_generate",
        workspace_id="ws-r8",
        result={},
    )
    task_registry.record_done(legacy_id, type="outline_generate", result={})
    task_registry.record_done(
        other_id,
        type="outline_generate",
        workspace_id="ws-other",
        result={},
    )

    tasks = task_registry.list_done_tasks(
        type="outline_generate",
        workspace_id="ws-r8",
        include_unbound=True,
    )

    assert {task.task_id for task in tasks} == {bound_id, legacy_id}


def test_clear_empties_table(isolated_db: Path) -> None:
    tid = task_registry.new_task_id()
    task_registry.record_done(tid, type="chapter_write", result={})
    task_registry.clear()
    assert task_registry.get(tid) is None


def test_eviction_caps_oldest(
    isolated_db: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """超 _MAX_TASKS 时按 created_at 删最旧。"""
    monkeypatch.setattr(task_registry, "_MAX_TASKS", 3)
    ids = []
    for i in range(5):
        tid = task_registry.new_task_id()
        ids.append(tid)
        task_registry.record_done(tid, type="chapter_write", result={"i": i})
    # 只保留最近 3 条;最旧 2 条被淘汰
    assert task_registry.get(ids[0]) is None
    assert task_registry.get(ids[1]) is None
    assert task_registry.get(ids[2]) is not None
    assert task_registry.get(ids[4]) is not None


def test_eviction_never_deletes_pending_or_running(
    isolated_db: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """容量上限只能淘汰历史终态；活 Worker/lease 的任务行必须保留。"""

    monkeypatch.setattr(task_registry, "_MAX_TASKS", 2)
    pending_id = task_registry.new_task_id()
    running_id = task_registry.new_task_id()
    task_registry.record_pending(
        pending_id,
        type="chapter_write",
        workspace_id="ws-active",
    )
    task_registry.record_pending(
        running_id,
        type="chapter_rewrite",
        workspace_id="ws-active",
    )
    assert task_registry.claim_pending_for_run(running_id) == "claimed"

    terminal_ids = []
    for i in range(3):
        task_id = task_registry.new_task_id()
        terminal_ids.append(task_id)
        task_registry.record_done(
            task_id,
            type="chapter_write",
            result={"i": i},
            workspace_id="ws-active",
        )

    assert task_registry.get(pending_id).status == "pending"  # type: ignore[union-attr]
    assert task_registry.get(running_id).status == "running"  # type: ignore[union-attr]
    assert all(task_registry.get(task_id) is None for task_id in terminal_ids)


def test_destroyed_marks_in_memory_only(isolated_db: Path) -> None:
    task_registry.mark_workspace_destroyed("ws-dead")
    assert task_registry.is_workspace_destroyed("ws-dead") is True
    task_registry.clear_workspace_destroyed_mark("ws-dead")
    assert task_registry.is_workspace_destroyed("ws-dead") is False


def test_task_base_isolated_from_main_base() -> None:
    """护栏:TaskRecordORM 在独立 metadata,不进主 Base(故 alembic 不受影响)。"""
    from src.core.db import Base
    from src.core.db.task_models import TaskBase, TaskRecordORM

    assert "task_records" in TaskBase.metadata.tables
    assert "task_records" not in Base.metadata.tables
    assert TaskRecordORM.__tablename__ == "task_records"


def test_active_task_type_filter(isolated_db: Path) -> None:
    task_registry.record_pending("first", type="chapter_card_first_after_commit", workspace_id="ws-a")
    task_registry.record_pending("other", type="chapter_write", workspace_id="ws-b")
    kinds = ("chapter_card_first_after_commit",)
    assert task_registry.has_active_tasks("ws-a", task_types=kinds)
    assert not task_registry.has_active_tasks("ws-b", task_types=kinds)
    assert task_registry.has_active_tasks("ws-b")
    task_registry.update_to_done("first", result={})
    assert not task_registry.has_active_tasks("ws-a", task_types=kinds)
