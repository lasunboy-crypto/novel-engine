"""task_registry cancel signal + status CHECK reconcile 单测(v1.1 w4-s6 = J1)。

覆盖:
- ``register_cancel_event`` 幂等 + workspace 记录
- ``cancel_task``:set Event + 置 ``cancel_requested``;无 in-flight Event 返 False
- ``cancel_workspace_tasks``:按 workspace 级联 set in-flight Event(覆盖 pipeline 等)
- ``update_to_cancelled``:翻 cancelled + 默认 error payload
- ``bind_cancel_event``:登记 + contextvar 绑定 + 退出 unregister
- ``reconcile_task_status_check``:旧 5 档 CHECK 的 _tasks.db 无损升级到 6 档
"""

from __future__ import annotations

from collections.abc import Iterator
from pathlib import Path
import threading
import time

import pytest
from sqlalchemy import create_engine, text

from src.api import task_registry
from src.core.llm.cancel import current_cancel_event, is_cancelled
from src.services.task_db import (
    make_task_session_factory,
    reconcile_task_status_check,
)


@pytest.fixture(autouse=True)
def _clear_cancel_events() -> Iterator[None]:
    """每用例清进程内 cancel Event 真源(模块全局,autouse db 隔离不覆盖它)。"""
    task_registry._cancel_events.clear()
    task_registry._cancel_event_ws.clear()
    yield
    task_registry._cancel_events.clear()
    task_registry._cancel_event_ws.clear()


# ---- register / cancel ------------------------------------------------------


class TestCancelEvents:
    def test_register_idempotent_same_event(self) -> None:
        tid = task_registry.new_task_id()
        e1 = task_registry.register_cancel_event(tid, "ws-1")
        e2 = task_registry.register_cancel_event(tid, "ws-1")
        assert e1 is e2
        assert task_registry._cancel_event_ws[tid] == "ws-1"

    def test_unregister_clears_both_dicts(self) -> None:
        tid = task_registry.new_task_id()
        task_registry.register_cancel_event(tid, "ws-1")
        task_registry.unregister_cancel_event(tid)
        assert tid not in task_registry._cancel_events
        assert tid not in task_registry._cancel_event_ws

    def test_cancel_task_sets_event_and_flag(self) -> None:
        tid = task_registry.new_task_id()
        task_registry.record_pending(tid, type="chapter_write", workspace_id="ws-1")
        event = task_registry.register_cancel_event(tid, "ws-1")
        assert task_registry.cancel_task(tid) is True
        assert event.is_set()

    def test_cancel_task_no_inflight_event_returns_false(self) -> None:
        """任务在 db 但无 in-flight Event(已终态 / 跨重启)→ 返 False,仅置 flag。"""
        tid = task_registry.new_task_id()
        task_registry.record_pending(tid, type="chapter_write", workspace_id="ws-1")
        assert task_registry.cancel_task(tid) is False

    def test_pending_task_can_be_claimed_only_once(self) -> None:
        tid = task_registry.new_task_id()
        task_registry.record_pending(tid, type="chapter_write", workspace_id="ws-1")

        assert task_registry.claim_pending_for_run(tid) == "claimed"
        assert task_registry.claim_pending_for_run(tid) == "rejected"
        info = task_registry.get(tid)
        assert info is not None
        assert info.status == "running"

    def test_cancelled_pending_task_is_not_claimed_for_run(self) -> None:
        tid = task_registry.new_task_id()
        task_registry.record_pending(tid, type="chapter_write", workspace_id="ws-1")
        assert task_registry.cancel_task(tid) is False

        assert task_registry.claim_pending_for_run(tid) == "cancelled"
        info = task_registry.get(tid)
        assert info is not None
        assert info.status == "cancelled"
        assert info.error == {
            "code": "TASK_CANCELLED",
            "reason": "user_requested_before_worker_start",
        }

    def test_cancel_serializes_with_pending_claim(self) -> None:
        """cancel 与 claim 共锁，不能插入 claim 的读取/CAS 中间。"""

        tid = task_registry.new_task_id()
        task_registry.record_pending(tid, type="chapter_write", workspace_id="ws-1")
        started = threading.Event()
        finished = threading.Event()

        def _cancel() -> None:
            started.set()
            task_registry.cancel_task(tid)
            finished.set()

        task_registry._registration_lock.acquire()
        try:
            thread = threading.Thread(target=_cancel)
            thread.start()
            assert started.wait(timeout=1)
            time.sleep(0.01)
            assert not finished.is_set()
        finally:
            task_registry._registration_lock.release()
        thread.join(timeout=1)
        assert finished.is_set()
        assert task_registry.claim_pending_for_run(tid) == "cancelled"

    def test_cancel_workspace_tasks_cascades_by_workspace(self) -> None:
        """按 workspace 级联 set in-flight Event(含不进 task_records 的 key)。"""
        t_ws1 = task_registry.new_task_id()
        t_ws2 = task_registry.new_task_id()
        run_ws1 = "run_pipeline_abc"  # 模拟 pipeline run_id(不进 task_records)
        e1 = task_registry.register_cancel_event(t_ws1, "ws-1")
        e_run = task_registry.register_cancel_event(run_ws1, "ws-1")
        e2 = task_registry.register_cancel_event(t_ws2, "ws-2")

        count = task_registry.cancel_workspace_tasks("ws-1")
        assert count == 2
        assert e1.is_set()
        assert e_run.is_set()
        assert not e2.is_set()  # 其它 workspace 不受影响

    def test_cancel_workspace_serializes_with_pending_claim(self) -> None:
        """整工作区取消也不能插入 pending claim 的读取/CAS 窗口。"""

        tid = task_registry.new_task_id()
        task_registry.record_pending(
            tid,
            type="chapter_write",
            workspace_id="ws-1",
        )
        started = threading.Event()
        finished = threading.Event()

        def _cancel_workspace() -> None:
            started.set()
            task_registry.cancel_workspace_tasks("ws-1")
            finished.set()

        task_registry._registration_lock.acquire()
        try:
            thread = threading.Thread(target=_cancel_workspace)
            thread.start()
            assert started.wait(timeout=1)
            time.sleep(0.01)
            assert not finished.is_set()
        finally:
            task_registry._registration_lock.release()
        thread.join(timeout=1)
        assert finished.is_set()
        assert task_registry.claim_pending_for_run(tid) == "cancelled"

    def test_update_to_cancelled_default_error(self) -> None:
        tid = task_registry.new_task_id()
        task_registry.record_pending(tid, type="chapter_write")
        info = task_registry.update_to_cancelled(tid)
        assert info is not None
        assert info.status == "cancelled"
        assert info.error == {"code": "TASK_CANCELLED", "reason": "user_requested"}

    def test_update_to_cancelled_missing_returns_none(self) -> None:
        assert task_registry.update_to_cancelled("task_nonexistent") is None

    def test_bind_cancel_event_binds_and_unregisters(self) -> None:
        tid = task_registry.new_task_id()
        with task_registry.bind_cancel_event(tid, "ws-1"):
            # 块内 contextvar 已绑该 task 的 Event
            assert current_cancel_event() is not None
            assert is_cancelled() is False
            # 外部 cancel_task set 后块内可见
            task_registry.cancel_task(tid)
            assert is_cancelled() is True
        # 退出后 contextvar reset + dict 真源清理
        assert current_cancel_event() is None
        assert tid not in task_registry._cancel_events


# ---- reconcile status CHECK -------------------------------------------------


def _create_legacy_5value_db(db_path: Path) -> None:
    """手工建一个旧 5 档 CHECK 的 task_records 表 + 插一行(模拟 J1 前的 _tasks.db)。"""
    engine = create_engine(f"sqlite:///{db_path}")
    with engine.begin() as conn:
        conn.execute(
            text(
                "CREATE TABLE task_records ("
                "task_id VARCHAR PRIMARY KEY, type VARCHAR NOT NULL, "
                "status VARCHAR NOT NULL, progress INTEGER NOT NULL DEFAULT 0, "
                "result_json JSON, error_json JSON, workspace_id VARCHAR, "
                "cancel_requested BOOLEAN NOT NULL DEFAULT 0, "
                "created_at DATETIME NOT NULL, updated_at DATETIME NOT NULL, "
                "CHECK (status IN ('pending','running','done','failed','interrupted'))"
                ")"
            )
        )
        conn.execute(
            text(
                "INSERT INTO task_records "
                "(task_id, type, status, progress, created_at, updated_at) "
                "VALUES ('task_old', 'chapter_write', 'done', 100, "
                "'2026-01-01 00:00:00', '2026-01-01 00:00:00')"
            )
        )
    engine.dispose()


class TestReconcileStatusCheck:
    def test_legacy_db_upgraded_lossless(self, tmp_path: Path) -> None:
        root = tmp_path / "tasks_root"
        root.mkdir()
        db_path = root / "_tasks.db"
        _create_legacy_5value_db(db_path)

        # reconcile 前:旧 CHECK 拒绝 cancelled
        engine = create_engine(f"sqlite:///{db_path}")
        with engine.begin() as conn:
            with pytest.raises(Exception):  # noqa: B017,PT011 — IntegrityError
                conn.execute(
                    text(
                        "INSERT INTO task_records "
                        "(task_id, type, status, progress, created_at, updated_at) "
                        "VALUES ('t2','chapter_write','cancelled',0,"
                        "'2026-01-02 00:00:00','2026-01-02 00:00:00')"
                    )
                )
        engine.dispose()

        # reconcile:旧表无损升级到 6 档
        assert reconcile_task_status_check(data_dir=root) is True

        # reconcile 后:cancelled 可插 + 旧行保留
        engine = create_engine(f"sqlite:///{db_path}")
        with engine.begin() as conn:
            conn.execute(
                text(
                    "INSERT INTO task_records "
                    "(task_id, type, status, progress, cancel_requested, "
                    "created_at, updated_at) "
                    "VALUES ('t2','chapter_write','cancelled',0,0,"
                    "'2026-01-02 00:00:00','2026-01-02 00:00:00')"
                )
            )
            old = conn.execute(
                text("SELECT status FROM task_records WHERE task_id='task_old'")
            ).fetchone()
            assert old is not None and old[0] == "done"  # 旧行无损保留
        engine.dispose()

    def test_reconcile_idempotent(self, tmp_path: Path) -> None:
        """已是 6 档(或全新部署 create_all)→ reconcile 返 False(不重建)。"""
        root = tmp_path / "tasks_root"
        root.mkdir()
        # 用当前工厂建一个 6 档新表
        make_task_session_factory(data_dir=root)().close()
        assert reconcile_task_status_check(data_dir=root) is False

    def test_reconcile_missing_db_returns_false(self, tmp_path: Path) -> None:
        assert reconcile_task_status_check(data_dir=tmp_path / "nonexistent") is False
