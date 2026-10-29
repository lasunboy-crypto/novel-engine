"""``POST /tasks/{task_id}:cancel`` 路由单测(v1.1 w4-s6 = J1)。

端点行为(尽力中断语义):未终态任务 → 置 cancel_requested + 发取消信号,返当前
TaskInfo;已终态 → 幂等放过返当前状态;未命中 → 404 TASK_NOT_FOUND。

注:真正的"取消跑到一半的任务"中断由 client / registry 单测覆盖(见
``tests/unit/test_llm_cancel_j1.py`` + ``test_task_registry_cancel_j1.py``);
TestClient 同步跑 BackgroundTasks,不便测异步 race,本文件只覆盖端点契约。
"""

from __future__ import annotations

from collections.abc import Iterator

import pytest
from fastapi.testclient import TestClient

from src.api import task_registry
from src.api.main import create_app


@pytest.fixture()
def client() -> Iterator[TestClient]:
    app = create_app()
    with TestClient(app) as c:
        yield c


def test_cancel_pending_task_sets_flag(client: TestClient) -> None:
    tid = task_registry.new_task_id()
    task_registry.record_pending(tid, type="chapter_write", workspace_id="ws-1")

    resp = client.post(f"/api/v1/tasks/{tid}:cancel")
    assert resp.status_code == 200
    body = resp.json()
    assert body["data"]["task_id"] == tid
    # cancel 是尽力中断:立即返回时仍是 pending/running(worker 异步翻 cancelled)
    assert body["data"]["status"] in ("pending", "running")

    # cancel_requested 已置(持久层复查)
    from src.core.db.task_models import TaskRecordORM

    with task_registry._session() as session:  # type: ignore[attr-defined]
        orm = session.get(TaskRecordORM, tid)
        assert orm is not None and orm.cancel_requested is True


def test_cancel_terminal_task_idempotent(client: TestClient) -> None:
    tid = task_registry.new_task_id()
    task_registry.record_done(tid, type="chapter_write", result={"v": 1})

    resp = client.post(f"/api/v1/tasks/{tid}:cancel")
    assert resp.status_code == 200
    # 已终态幂等放过,返回原状态
    assert resp.json()["data"]["status"] == "done"


def test_cancel_unknown_task_404(client: TestClient) -> None:
    resp = client.post("/api/v1/tasks/task_nonexistent:cancel")
    assert resp.status_code == 404
    assert resp.json()["error"]["code"] == "TASK_NOT_FOUND"
