"""单章章节卡路由 + ``outline:commit`` 首卡集成测试。

覆盖 6+ 用例:
1. ``POST /chapters/{id}/card:generate`` happy path:200 + ChapterCardOut +
   SSOT 文件落盘 + ORM 行 card_status=draft
2. ``POST /chapters/{id}/card:generate`` 缺 ``X-Workspace-Id`` → 422
3. ``POST /chapters/{id}/card:generate`` body 缺字段 → 422
4. ``POST /outline:commit`` 后只异步生成 ``ch-0001`` 并进入 ``pending_review``
5. 响应返 ``first_card_task_id``，任务结果只含首卡

测试基础设施
============

- ``MockLLMClient`` 全 dispatch(outline_builder / chapter_card_generator 两个分支)
- ``app.dependency_overrides[get_chapter_card_service] / get_outline_service]``
  注入到同 tmp_path workspaces 根
- ``BackgroundTasks`` 由 TestClient 同步等待完成(starlette 行为)
"""

from __future__ import annotations

from collections.abc import Callable, Iterator
from pathlib import Path

import pytest
from fastapi.testclient import TestClient
from sqlalchemy import create_engine
from sqlalchemy.orm import Session
from sqlalchemy.pool import StaticPool

from src.api import task_registry
from src.api.deps import get_chapter_card_service, get_outline_service
from src.api.main import create_app
from src.core.db import Base
from src.core.db.models import ChapterCardORM
from src.core.llm.mock_client import MockLLMClient
from src.services.chapter_card_service import ChapterCardService
from src.services.chapter_service import ChapterService
from src.services.outline_service import OutlineService
from src.services.workspace_service import WorkspaceService


# ---- fixtures ---------------------------------------------------------------


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
        "ws-card-api",
        display_name="P5-B-06 card route test",
        genre="xianxia",
        premise="test",
        activate=False,
    )
    return "ws-card-api"


@pytest.fixture
def session_factory() -> Callable[[str], Session]:
    """单 in-memory engine + StaticPool + check_same_thread=False。

    StaticPool 必要原因:FastAPI ``BackgroundTasks`` 在 ``anyio.to_thread``
    线程池跑首卡任务,默认 SingletonThreadPool 让每
    线程拿独立 connection(``:memory:`` 数据连接本地)→ tables 不可见。
    StaticPool 让所有线程共享单 connection,数据跨线程一致。
    """
    engine = create_engine(
        "sqlite:///:memory:",
        connect_args={"check_same_thread": False},
        poolclass=StaticPool,
    )
    Base.metadata.create_all(engine)

    def factory(workspace_id: str) -> Session:
        return Session(engine)

    return factory


def _assert_async_task_done(resp, *, expected_type: str):
    """v1.0.x Commit B 改造:6 端点全改 BackgroundTasks 模式,统一断言 helper。"""
    assert resp.status_code == 202, (
        f"期望 202 Accepted,实得 {resp.status_code}:{resp.text[:200]}"
    )
    info = resp.json()["data"]
    assert info["type"] == expected_type
    assert info["status"] == "pending"
    task_id = info["task_id"]
    final = task_registry.get(task_id)
    assert final is not None, f"task_id={task_id} 未在 registry"
    assert final.status == "done", (
        f"期望 status=done,实得 {final.status} error={final.error}"
    )
    return info, final.result


@pytest.fixture(autouse=True)
def _clear_task_registry() -> Iterator[None]:
    task_registry.clear()
    yield
    task_registry.clear()


@pytest.fixture
def client(
    workspace_service: WorkspaceService,
    session_factory: Callable[[str], Session],
    workspace_id: str,
) -> Iterator[TestClient]:
    app = create_app()
    mock_llm = MockLLMClient()
    chapter_service = ChapterService(
        workspace_service=workspace_service,
        client=mock_llm,
        model="claude-sonnet-4-6",
        session_factory=session_factory,
    )
    card_service = ChapterCardService(
        workspace_service=workspace_service,
        chapter_service=chapter_service,
        session_factory=session_factory,
        client=mock_llm,
        model="claude-sonnet-4-6",
        ws_manager=None,
    )
    outline_service = OutlineService(
        workspace_service=workspace_service,
        client=mock_llm,
        model="claude-sonnet-4-6",
        router_factory=None,
        session_factory=session_factory,
        ws_manager=None,
    )
    app.dependency_overrides[get_chapter_card_service] = lambda: card_service
    app.dependency_overrides[get_outline_service] = lambda: outline_service
    with TestClient(app) as c:
        yield c
    app.dependency_overrides.clear()


# ---- 测试:POST /chapters/{id}/card:generate --------------------------------


class TestCardGenerateRoute:
    def test_happy_path_generates_and_persists(
        self,
        client: TestClient,
        workspace_id: str,
        workspace_service: WorkspaceService,
        session_factory: Callable[[str], Session],
    ) -> None:
        # 先 outline:generate + commit 以确保 outline 4 段 + volumes 表都齐
        # v1.0.x Commit B:outline:generate 改异步,202 + registry done
        resp = client.post(
            f"/api/v1/workspaces/{workspace_id}/outline:generate",
            json={
                "premise": "一个废柴炼丹师在末法时代重生",
                "total_volumes": 2,
                "chapters_per_volume": 9,
                "tension_template": "heros_journey",
                "genre": "xianxia",
            },
        )
        _assert_async_task_done(resp, expected_type="outline_generate")
        resp = client.post(
            f"/api/v1/workspaces/{workspace_id}/outline:commit"
        )
        assert resp.status_code == 200

        # 找 outline 文件读 volume_id
        outline_dir = (
            workspace_service.workspaces_root / workspace_id / "data" / "outline"
        )
        import json

        volume_plans = json.loads(
            (outline_dir / "volume_plans.json").read_text(encoding="utf-8")
        )
        first_volume_id = volume_plans["volumes"][0]["volume_id"]

        # 现在调单章 card:generate(ch-0042 一个未在批量内的章号)
        # v1.0.x Commit B:card:generate 改异步,202 + registry done + result.card
        resp = client.post(
            "/api/v1/chapters/ch-0042/card:generate",
            headers={"X-Workspace-Id": workspace_id},
            json={"volume_id": first_volume_id, "chapter_number": 2},
        )
        _, card = _assert_async_task_done(
            resp, expected_type="chapter_card_generate"
        )
        assert card["chapter_id"] == "ch-0042"
        assert card["chapter_number"] == 2
        assert card["card_status"] == "draft"

        # SSOT 文件
        card_file = (
            workspace_service.workspaces_root
            / workspace_id
            / "data"
            / "cards"
            / "ch-0042.json"
        )
        assert card_file.is_file()

        # ORM 行
        session = session_factory(workspace_id)
        try:
            row = session.get(ChapterCardORM, "ch-0042")
        finally:
            session.close()
        assert row is not None
        assert row.card_status == "draft"

    def test_missing_workspace_header_returns_422(
        self, client: TestClient
    ) -> None:
        resp = client.post(
            "/api/v1/chapters/ch-0001/card:generate",
            json={"volume_id": "vol-1", "chapter_number": 1},
        )
        assert resp.status_code == 422
        assert resp.json()["error"]["code"] == "MISSING_WORKSPACE_HEADER"

    def test_missing_body_field_returns_422(
        self, client: TestClient, workspace_id: str
    ) -> None:
        resp = client.post(
            "/api/v1/chapters/ch-0001/card:generate",
            headers={"X-Workspace-Id": workspace_id},
            json={"volume_id": "vol-1"},  # 缺 chapter_number
        )
        assert resp.status_code == 422
        assert resp.json()["error"]["code"] == "VALIDATION_FAILED"


# ---- 测试:POST /outline:commit 异步首卡 ------------------------------------


class TestOutlineCommitFirstCardTrigger:
    def test_commit_returns_first_card_task_id(
        self, client: TestClient, workspace_id: str
    ) -> None:
        # 先生成 outline
        # v1.0.x Commit B:outline:generate 改异步,前置调用直接 fire & forget
        # (TestClient 同步等 BackgroundTasks 跑完;不校验响应,因仅作前置)
        client.post(
            f"/api/v1/workspaces/{workspace_id}/outline:generate",
            json={
                "premise": "一个废柴炼丹师在末法时代重生",
                "total_volumes": 2,
                "chapters_per_volume": 9,
                "tension_template": "heros_journey",
                "genre": "xianxia",
            },
        )
        resp = client.post(
            f"/api/v1/workspaces/{workspace_id}/outline:commit"
        )
        assert resp.status_code == 200, resp.text
        result = resp.json()["data"]
        assert result["gates_passed"] is True
        assert result["first_card_task_id"] is not None
        assert result["first_card_task_id"].startswith("task_")

    def test_commit_produces_only_first_card_for_review(
        self,
        client: TestClient,
        workspace_id: str,
        session_factory: Callable[[str], Session],
    ) -> None:
        """§2.138:正式大纲提交只生成当前第一张卡并等待审核。"""
        # v1.0.x Commit B:outline:generate 改异步,前置调用直接 fire & forget
        # (TestClient 同步等 BackgroundTasks 跑完;不校验响应,因仅作前置)
        client.post(
            f"/api/v1/workspaces/{workspace_id}/outline:generate",
            json={
                "premise": "一个废柴炼丹师在末法时代重生",
                "total_volumes": 2,
                "chapters_per_volume": 9,
                "tension_template": "heros_journey",
                "genre": "xianxia",
            },
        )
        resp = client.post(
            f"/api/v1/workspaces/{workspace_id}/outline:commit"
        )
        first_card_task_id = resp.json()["data"]["first_card_task_id"]

        # FastAPI BackgroundTasks 在 TestClient 下同步等待完成,任务已 done。
        task_resp = client.get(f"/api/v1/tasks/{first_card_task_id}")
        assert task_resp.status_code == 200, task_resp.text
        task_data = task_resp.json()["data"]
        assert task_data["status"] == "done"
        result = task_data["result"]
        assert result is not None
        assert result["cards_generated"] == ["ch-0001"]
        assert result["cards_failed"] == []

        # ORM 行 card_status 校验
        session = session_factory(workspace_id)
        try:
            statuses = {
                row.chapter_id: row.card_status
                for row in session.query(ChapterCardORM).all()
            }
        finally:
            session.close()
        assert statuses == {"ch-0001": "pending_review"}
