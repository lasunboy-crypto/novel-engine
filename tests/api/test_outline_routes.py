"""outline 路由集成测试(P5-B-04)。

覆盖 5 个端点 happy path + 关键错误码:

- ``POST /outline:generate``:200 + ``TaskInfo`` 含 4 段产出 + task_id
- ``POST /outline:regenerate-section``:200 + 单段重生(上游不重跑)
- ``POST /outline:commit``:200 + 落 volumes 表 / 422 ``OUTLINE_ARC_TOO_SHORT``
  (副本段 < 3 章)+ 404 ``OUTLINE_NOT_FOUND``(未生成)
- ``GET /outline``:200 + 4 段聚合
- ``PUT /outline``:200 + 单段 patch

测试基础设施
============

``app.dependency_overrides[get_outline_service]`` 注入 ``MockLLMClient``
+ tmp_path workspaces;无真 LLM 依赖,对齐 CLAUDE.md §维护人环境约束。
"""

from __future__ import annotations

from collections.abc import Callable, Iterator
from pathlib import Path
from typing import Any

import pytest
from fastapi.testclient import TestClient
from sqlalchemy import create_engine, select
from sqlalchemy.orm import Session

from src.api import task_registry
from src.api.deps import get_chapter_card_service, get_outline_service
from src.api.main import create_app
from src.core.db import Base
from src.core.db.models import VolumeORM
from src.core.llm.mock_client import MockLLMClient
from src.core.workspace.workspace_paths import WorkspacePaths
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
        "ws-outline-api",
        display_name="P5-B-04 outline routes test",
        genre="test",
        premise="x",
        activate=False,
    )
    return "ws-outline-api"


def _assert_async_task_done(resp, *, expected_type: str):
    """v1.0.x Commit B 改造:6 端点全改 BackgroundTasks 模式,统一断言 helper。

    与 test_chapters_routes.py 内同型(就近声明避免跨文件 conftest 改动);
    返 (info_dict, result_dict),让 result 字段访问保持原样。
    """
    assert resp.status_code == 202, (
        f"期望 202 Accepted,实得 {resp.status_code}:{resp.text[:200]}"
    )
    body = resp.json()
    info = body["data"]
    assert info["type"] == expected_type
    assert info["status"] == "pending"
    assert info["task_id"].startswith("task_")
    task_id = info["task_id"]
    final = task_registry.get(task_id)
    assert final is not None, f"task_id={task_id} 未在 registry"
    assert final.status == "done", (
        f"期望 status=done,实得 {final.status} error={final.error}"
    )
    return info, final.result


def _assert_async_task_failed(resp, *, expected_type: str, expected_code: str):
    """v1.0.x Commit B 改造:断言异步任务 → failed + error.code 命中。"""
    assert resp.status_code == 202, (
        f"期望 202 Accepted,实得 {resp.status_code}:{resp.text[:200]}"
    )
    body = resp.json()
    info = body["data"]
    assert info["type"] == expected_type
    task_id = info["task_id"]
    final = task_registry.get(task_id)
    assert final is not None
    assert final.status == "failed", (
        f"期望 status=failed,实得 {final.status} result={final.result}"
    )
    assert final.error is not None
    assert final.error["code"] == expected_code
    return info, final.error


@pytest.fixture(autouse=True)
def _clear_task_registry() -> Iterator[None]:
    task_registry.clear()
    yield
    task_registry.clear()


def _make_session_factory(
    workspace_service: WorkspaceService,
) -> Callable[[str], Session]:
    def _factory(workspace_id: str) -> Session:
        paths = WorkspacePaths(
            workspace_id, workspaces_root=workspace_service.workspaces_root
        )
        engine = create_engine(f"sqlite:///{paths.db_path}")
        Base.metadata.create_all(engine)
        return Session(engine)

    return _factory


@pytest.fixture
def client(workspace_service: WorkspaceService) -> Iterator[TestClient]:
    app = create_app()
    session_factory = _make_session_factory(workspace_service)
    mock_llm = MockLLMClient()
    service = OutlineService(
        workspace_service=workspace_service,
        client=mock_llm,
        model="claude-sonnet-4-6",
        router_factory=None,
        session_factory=session_factory,
        ws_manager=None,
    )
    # P5-B-06:outline:commit 端点同时注入 chapter_card_service(BackgroundTasks
    # 跑批量章节卡)。本测试关心 outline 路由 happy path,不深查批量产出,故
    # card_service 注入到同 workspace_service 即可让 BG 任务跑通(失败被 service
    # 顶层 swallow,不影响 commit 端点 200 响应)。
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
    app.dependency_overrides[get_outline_service] = lambda: service
    app.dependency_overrides[get_chapter_card_service] = lambda: card_service
    with TestClient(app) as c:
        yield c
    app.dependency_overrides.clear()


# ---- POST /outline:generate -------------------------------------------------


class TestGenerateEndpoint:
    def test_returns_task_info_with_full_outline(
        self, client: TestClient, workspace_id: str
    ) -> None:
        resp = client.post(
            f"/api/v1/workspaces/{workspace_id}/outline:generate",
            json={
                "premise": "一个废柴炼丹师在末法时代重生",
                "total_volumes": 3,
                "chapters_per_volume": 9,
                "tension_template": "heros_journey",
                "genre": "仙侠",
            },
        )
        # v1.0.x Commit B:202 + 后台跑 + registry done
        info, result = _assert_async_task_done(
            resp, expected_type="outline_generate"
        )
        assert result["task_id"] == info["task_id"]
        outline = result["outline"]
        assert outline["world"]["setting"]
        assert len(outline["characters"]["characters"]) >= 4
        assert outline["plot"]["core_conflict"]
        assert len(outline["volume_plans"]["volumes"]) == 3

    def test_rejects_second_active_outline_worker(
        self, client: TestClient, workspace_id: str
    ) -> None:
        """同 workspace 已有 pending outline task 时不得创建第二个 Worker。"""
        active_task_id = "task_existing_outline"
        task_registry.record_pending(
            active_task_id,
            type="outline_generate",
            workspace_id=workspace_id,
        )

        resp = client.post(
            f"/api/v1/workspaces/{workspace_id}/outline:generate",
            json={
                "premise": "x",
                "total_volumes": 3,
                "chapters_per_volume": 9,
                "tension_template": "heros_journey",
                "genre": "仙侠",
            },
        )

        assert resp.status_code == 409
        error = resp.json()["error"]
        assert error["code"] == "OUTLINE_GENERATION_ALREADY_RUNNING"
        assert error["details"]["active_task_id"] == active_task_id
        assert task_registry.get(active_task_id).status == "pending"  # type: ignore[union-attr]

    def test_rejects_duplicate_after_matching_task_is_done(
        self,
        client: TestClient,
        workspace_id: str,
        monkeypatch: pytest.MonkeyPatch,
    ) -> None:
        """done checkpoint 与当前五段一致时，迟到 POST 不得再次调用生成服务。"""
        service = client.app.dependency_overrides[get_outline_service]()
        original_generate = service.generate_outline
        calls = 0

        def _counted_generate(*args: Any, **kwargs: Any):  # type: ignore[no-untyped-def]
            nonlocal calls
            calls += 1
            return original_generate(*args, **kwargs)

        monkeypatch.setattr(service, "generate_outline", _counted_generate)
        payload = {
            "premise": "x",
            "total_volumes": 3,
            "chapters_per_volume": 9,
            "tension_template": "heros_journey",
            "genre": "仙侠",
        }

        first_resp = client.post(
            f"/api/v1/workspaces/{workspace_id}/outline:generate",
            json=payload,
        )
        first, _ = _assert_async_task_done(
            first_resp, expected_type="outline_generate"
        )
        second_resp = client.post(
            f"/api/v1/workspaces/{workspace_id}/outline:generate",
            json=payload,
        )

        assert calls == 1
        assert second_resp.status_code == 409
        error = second_resp.json()["error"]
        assert error["code"] == "OUTLINE_CHECKPOINT_ALREADY_EXISTS"
        assert error["details"]["task_id"] == first["task_id"]

    def test_validation_error_missing_required(
        self, client: TestClient, workspace_id: str
    ) -> None:
        resp = client.post(
            f"/api/v1/workspaces/{workspace_id}/outline:generate",
            json={"premise": "x"},  # missing total_volumes / etc.
        )
        assert resp.status_code == 422

    def test_publishes_event_done_frame_for_frontend(
        self,
        client: TestClient,
        workspace_id: str,
        monkeypatch: pytest.MonkeyPatch,
    ) -> None:
        """v1.0.x wave 3 Commit B follow-up(2026-05-21):路由层 wrapper 必须
        publish ``event="done"`` 帧供前端 ``NewBookPage.handleTaskFrame`` 切 ready。

        前任 Commit B 注释误判 "OutlineService 内部已 publish 'done' stage event",
        实际只 publish ``event="stage"`` data.stage="done";前端等的是
        ``frame.event === "done"``,永远不匹配,导致 NewBookPage 永远卡"生成中"。
        本测试装 spy 捕获模块级 ``ws_manager.publish`` 调用,断言路由 wrapper
        末尾确实发出 ``event="done"`` 帧。
        """
        from src.api.routes import outline as outline_route

        published: list[dict[str, Any]] = []
        orig_publish = outline_route.ws_manager.publish

        def _spy(channel: str, event: str, data: dict[str, Any]) -> None:
            published.append({"channel": channel, "event": event, "data": data})
            orig_publish(channel, event, data)

        monkeypatch.setattr(outline_route.ws_manager, "publish", _spy)

        resp = client.post(
            f"/api/v1/workspaces/{workspace_id}/outline:generate",
            json={
                "premise": "x",
                "total_volumes": 3,
                "chapters_per_volume": 9,
                "tension_template": "heros_journey",
                "genre": "仙侠",
            },
        )
        info, _ = _assert_async_task_done(resp, expected_type="outline_generate")
        task_id = info["task_id"]
        channel = f"task.{task_id}.progress"

        # 必须有恰好 1 帧 event="done" 到该 task 的 progress 频道(NewBookPage
        # 等的就是这个 frame.event === "done";前任 Commit B 漏发 bug 回归保障)
        done_frames = [
            m for m in published if m["channel"] == channel and m["event"] == "done"
        ]
        assert len(done_frames) == 1, (
            f"期望 1 帧 event='done',实得 {len(done_frames)};published="
            f"{[(m['event'], m.get('data', {}).get('stage')) for m in published if m['channel'] == channel]}"
        )
        # event="done" 帧 data 含 result(对齐 chapters.py 同型)
        assert done_frames[0]["data"]["task_id"] == task_id
        assert "result" in done_frames[0]["data"]


# ---- POST /outline:regenerate-section ---------------------------------------


class TestRegenerateSectionEndpoint:
    def test_regenerate_world_no_upstream_needed(
        self, client: TestClient, workspace_id: str
    ) -> None:
        resp = client.post(
            f"/api/v1/workspaces/{workspace_id}/outline:regenerate-section",
            json={
                "section": "world",
                "premise": "x",
                "keep_others": True,
            },
        )
        # v1.0.x Commit B:result = OutlineOut 4 段聚合
        _, result = _assert_async_task_done(
            resp, expected_type="outline_regenerate_section"
        )
        assert result["world"] is not None
        assert result["characters"] is None  # 未生成

    def test_regenerate_characters_without_world_returns_404(
        self, client: TestClient, workspace_id: str
    ) -> None:
        # v1.0.x Commit B:OutlineNotFound → task_registry update_to_failed
        # 翻 OUTLINE_NOT_FOUND;HTTP 仍 202(异步语义)。
        resp = client.post(
            f"/api/v1/workspaces/{workspace_id}/outline:regenerate-section",
            json={"section": "characters", "premise": "x", "keep_others": True},
        )
        _assert_async_task_failed(
            resp,
            expected_type="outline_regenerate_section",
            expected_code="OUTLINE_NOT_FOUND",
        )

    def test_regenerate_checkpoint_is_workspace_bound_and_blocks_duplicate_generate(
        self, client: TestClient, workspace_id: str
    ) -> None:
        payload = {
            "premise": "x",
            "total_volumes": 3,
            "chapters_per_volume": 9,
            "tension_template": "heros_journey",
            "genre": "仙侠",
        }
        generated = client.post(
            f"/api/v1/workspaces/{workspace_id}/outline:generate",
            json=payload,
        )
        _assert_async_task_done(generated, expected_type="outline_generate")

        regenerated = client.post(
            f"/api/v1/workspaces/{workspace_id}/outline:regenerate-section",
            json={"section": "world", "premise": "x", "keep_others": True},
        )
        regen_info, _ = _assert_async_task_done(
            regenerated, expected_type="outline_regenerate_section"
        )
        bound = task_registry.list_done_tasks(
            type="outline_regenerate_section", workspace_id=workspace_id
        )
        assert [task.task_id for task in bound] == [regen_info["task_id"]]

        duplicate = client.post(
            f"/api/v1/workspaces/{workspace_id}/outline:generate",
            json=payload,
        )
        assert duplicate.status_code == 409
        error = duplicate.json()["error"]
        assert error["code"] == "OUTLINE_CHECKPOINT_ALREADY_EXISTS"
        assert error["details"]["task_id"] == regen_info["task_id"]


# ---- POST /outline:commit ---------------------------------------------------


class TestCommitEndpoint:
    def test_commit_success(
        self,
        client: TestClient,
        workspace_id: str,
        workspace_service: WorkspaceService,
    ) -> None:
        # 9 章 / 3 卷 mock → 副本中段 = 3 章 ✅
        client.post(
            f"/api/v1/workspaces/{workspace_id}/outline:generate",
            json={
                "premise": "x",
                "total_volumes": 3,
                "chapters_per_volume": 9,
                "tension_template": "heros_journey",
            },
        )
        resp = client.post(
            f"/api/v1/workspaces/{workspace_id}/outline:commit"
        )
        assert resp.status_code == 200
        data = resp.json()["data"]
        assert data["workspace_id"] == workspace_id
        assert data["volumes_count"] == 3
        assert data["gates_passed"] is True

        # DB 内确认行已写入
        session = _make_session_factory(workspace_service)(workspace_id)
        try:
            rows = session.execute(select(VolumeORM)).scalars().all()
            assert len(rows) == 3
        finally:
            session.close()

    def test_arc_too_short_never_persists_volume_before_commit(
        self,
        client: TestClient,
        workspace_id: str,
        workspace_service: WorkspaceService,
    ) -> None:
        # 6 章 → mock arcs [1,2]/[3,4]/[5,6],副本中段 = 2 章 → [HARD] 违规
        client.post(
            f"/api/v1/workspaces/{workspace_id}/outline:generate",
            json={
                "premise": "x",
                "total_volumes": 2,
                "chapters_per_volume": 6,
                "tension_template": "heros_journey",
            },
        )
        resp = client.post(
            f"/api/v1/workspaces/{workspace_id}/outline:commit"
        )
        assert resp.status_code == 404
        body = resp.json()
        assert body["error"]["code"] == "OUTLINE_NOT_FOUND"
        assert not (workspace_service.workspaces_root / workspace_id / "data" / "outline" / "volume_plans.json").exists()

        # gate 失败时 volumes 表不应写入
        session = _make_session_factory(workspace_service)(workspace_id)
        try:
            rows = session.execute(select(VolumeORM)).scalars().all()
            assert rows == []
        finally:
            session.close()

    def test_commit_missing_outline_returns_404(
        self, client: TestClient, workspace_id: str
    ) -> None:
        resp = client.post(
            f"/api/v1/workspaces/{workspace_id}/outline:commit"
        )
        assert resp.status_code == 404
        assert resp.json()["error"]["code"] == "OUTLINE_NOT_FOUND"


# ---- GET /outline -----------------------------------------------------------


class TestGetOutline:
    def test_get_after_generate(
        self, client: TestClient, workspace_id: str
    ) -> None:
        client.post(
            f"/api/v1/workspaces/{workspace_id}/outline:generate",
            json={
                "premise": "x",
                "total_volumes": 3,
                "chapters_per_volume": 9,
                "tension_template": "heros_journey",
            },
        )
        resp = client.get(f"/api/v1/workspaces/{workspace_id}/outline")
        assert resp.status_code == 200
        data = resp.json()["data"]
        for stage in ("world", "characters", "plot", "volume_plans"):
            assert data[stage] is not None

    def test_get_empty_returns_all_null(
        self, client: TestClient, workspace_id: str
    ) -> None:
        resp = client.get(f"/api/v1/workspaces/{workspace_id}/outline")
        assert resp.status_code == 200
        data = resp.json()["data"]
        # v1.0.x wave 4 session 1:GET /outline 响应 5 段(原 4 段 + mini_plan)
        assert data == {
            "world": None,
            "characters": None,
            "plot": None,
            "volume_plans": None,
            "mini_plan": None,
        }


# ---- PUT /outline -----------------------------------------------------------


class TestPutOutline:
    def test_put_partial_patch_world_only(
        self, client: TestClient, workspace_id: str
    ) -> None:
        world_payload = {
            "setting": "时代背景:架空仙侠",
            "power_system": "九重炼丹境",
            "factions": "正派 / 魔域 / 散修",
            "culture": "门派林立",
            "unique_rules": "灵根分高低",
        }
        resp = client.put(
            f"/api/v1/workspaces/{workspace_id}/outline",
            json={"world": world_payload},
        )
        assert resp.status_code == 200
        data = resp.json()["data"]
        assert data["world"]["setting"] == "时代背景:架空仙侠"
        assert data["characters"] is None  # 未写

    def test_put_invalid_schema_returns_422(
        self, client: TestClient, workspace_id: str
    ) -> None:
        resp = client.put(
            f"/api/v1/workspaces/{workspace_id}/outline",
            json={"world": {"setting": "x"}},  # 缺其他必填字段
        )
        assert resp.status_code == 422


# ---- 已退役的未来章节卡批量入口 ---------------------------------------------


def test_generate_next_batch_route_is_removed(
    client: TestClient, workspace_id: str
) -> None:
    """UI 与后端同时退役；旧 URL 不得以隐藏能力继续工作。"""
    resp = client.post(
        f"/api/v1/workspaces/{workspace_id}/chapter-cards:generate-next-batch",
        json={"start_chapter_id": "ch-0001", "count": 2},
    )
    assert resp.status_code == 404


# ---- v1.1 wave 1 session 2 (G6.2):POST /chapter-cards:reset-word-count -----


class TestResetChapterCardsWordCountEndpoint:
    """v1.1 wave 1 session 2 (G6.2):工作区级 word_count_min/max backfill
    端点 e2e 测试。

    覆盖:
    - 200 happy path:scope 默认 unapproved + 入参默认 None → model default;
      含 9 字段 schema strict 校验
    - scope='all' 含 approved
    - 入参覆盖 model default
    - 422 ``CHAPTER_CARDS_BATCH_INVALID``:min > max（兼容既有公开错误码）
    - 422 入参错(scope literal 不在白名单 → Pydantic 422)
    - 422 未知工作区
    - schema strict 拒绝 extra 字段(防回归网,wave 6 s2.6c bug 1 同型)
    """

    def _setup_outline_committed(
        self, client: TestClient, workspace_id: str
    ) -> None:
        client.post(
            f"/api/v1/workspaces/{workspace_id}/outline:generate",
            json={
                "premise": "x",
                "total_volumes": 3,
                "chapters_per_volume": 9,
                "tension_template": "heros_journey",
            },
        )
        commit_resp = client.post(
            f"/api/v1/workspaces/{workspace_id}/outline:commit"
        )
        assert commit_resp.status_code == 200, (
            f"outline:commit 失败 {commit_resp.status_code}: "
            f"{commit_resp.text[:300]}"
        )
        # commit 只生成 ch-0001；一张卡已足以验证字数重置端点。

    def test_returns_200_with_9_field_schema_strict_pass(
        self, client: TestClient, workspace_id: str
    ) -> None:
        """default scope='unapproved' + None min/max → 200 含 7 字段齐全。"""
        self._setup_outline_committed(client, workspace_id)
        resp = client.post(
            f"/api/v1/workspaces/{workspace_id}/chapter-cards:reset-word-count",
            json={},
        )
        assert resp.status_code == 200, (
            f"期望 200,实得 {resp.status_code}: {resp.text[:500]}"
        )
        data = resp.json()["data"]
        assert set(data.keys()) == {
            "workspace_id",
            "scope",
            "applied_word_count_min",
            "applied_word_count_max",
            "reset_chapter_ids",
            "skipped_chapter_ids",
            "failed_chapter_ids",
        }
        assert data["workspace_id"] == workspace_id
        assert data["scope"] == "unapproved"
        assert data["applied_word_count_min"] == 2500
        assert data["applied_word_count_max"] == 4000
        assert isinstance(data["reset_chapter_ids"], list)
        assert isinstance(data["skipped_chapter_ids"], list)
        assert isinstance(data["failed_chapter_ids"], list)

    def test_scope_all_passes(
        self, client: TestClient, workspace_id: str
    ) -> None:
        self._setup_outline_committed(client, workspace_id)
        resp = client.post(
            f"/api/v1/workspaces/{workspace_id}/chapter-cards:reset-word-count",
            json={"scope": "all"},
        )
        assert resp.status_code == 200
        assert resp.json()["data"]["scope"] == "all"

    def test_explicit_word_count_overrides_default(
        self, client: TestClient, workspace_id: str
    ) -> None:
        self._setup_outline_committed(client, workspace_id)
        resp = client.post(
            f"/api/v1/workspaces/{workspace_id}/chapter-cards:reset-word-count",
            json={
                "scope": "all",
                "word_count_min": 2000,
                "word_count_max": 3500,
            },
        )
        assert resp.status_code == 200
        data = resp.json()["data"]
        assert data["applied_word_count_min"] == 2000
        assert data["applied_word_count_max"] == 3500

    def test_explicit_policy_adoption_returns_version_and_preserves_range(
        self, client: TestClient, workspace_id: str
    ) -> None:
        self._setup_outline_committed(client, workspace_id)
        resp = client.post(
            f"/api/v1/workspaces/{workspace_id}/chapter-cards:reset-word-count",
            json={"scope": "all", "word_count_min": 2500,
                  "word_count_max": 3500, "word_count_policy_version": 1},
        )
        assert resp.status_code == 200
        data = resp.json()["data"]
        assert data["applied_word_count_policy_version"] == 1
        assert data["applied_word_count_min"] == 2500
        assert data["applied_word_count_max"] == 3500
        assert not data["failed_chapter_ids"]

    def test_min_greater_than_max_returns_422(
        self, client: TestClient, workspace_id: str
    ) -> None:
        self._setup_outline_committed(client, workspace_id)
        resp = client.post(
            f"/api/v1/workspaces/{workspace_id}/chapter-cards:reset-word-count",
            json={"word_count_min": 5000, "word_count_max": 3000},
        )
        assert resp.status_code == 422
        assert resp.json()["error"]["code"] == "CHAPTER_CARDS_BATCH_INVALID"

    def test_invalid_scope_literal_returns_422(
        self, client: TestClient, workspace_id: str
    ) -> None:
        """Pydantic 422:scope 不在 {'unapproved', 'all'} 白名单。"""
        self._setup_outline_committed(client, workspace_id)
        resp = client.post(
            f"/api/v1/workspaces/{workspace_id}/chapter-cards:reset-word-count",
            json={"scope": "all_approved_with_bonus"},
        )
        assert resp.status_code == 422

    def test_unknown_workspace_returns_422(
        self, client: TestClient
    ) -> None:
        """未知工作区:service 层 WorkspaceServiceError 翻 422。"""
        resp = client.post(
            "/api/v1/workspaces/ws-does-not-exist/chapter-cards:reset-word-count",
            json={},
        )
        assert resp.status_code in (422, 404)

    def test_schema_rejects_extra_field_at_validate_time(
        self, client: TestClient, workspace_id: str
    ) -> None:
        """schema strict forbid extra 字段(防回归网,
        对齐 wave 6 s2.6c bug 1 schema drift 模式)。"""
        from src.api.schemas.chapter_cards import (
            ChapterCardsResetWordCountResult,
        )

        assert (
            ChapterCardsResetWordCountResult.model_config.get("extra")
            == "forbid"
        )

        valid_payload = {
            "workspace_id": "ws-x",
            "scope": "unapproved",
            "applied_word_count_min": 2500,
            "applied_word_count_max": 4000,
            "reset_chapter_ids": [],
            "skipped_chapter_ids": [],
            "failed_chapter_ids": [],
        }
        ChapterCardsResetWordCountResult.model_validate(valid_payload)

        with pytest.raises(Exception) as excinfo:
            ChapterCardsResetWordCountResult.model_validate(
                {**valid_payload, "future_unknown_field": "x"}
            )
        assert (
            "extra" in str(excinfo.value).lower()
            or "forbid" in str(excinfo.value).lower()
        )


# ---- WORKSPACE NOT FOUND ----------------------------------------------------


class TestWorkspaceErrors:
    def test_generate_unknown_workspace_returns_422_or_404(
        self, client: TestClient
    ) -> None:
        # v1.0.x Commit B:WorkspaceServiceError 在 BackgroundTasks 内捕获 →
        # update_to_failed,HTTP 202 + registry status=failed + error.code 命中。
        resp = client.post(
            "/api/v1/workspaces/no-such-ws/outline:generate",
            json={
                "premise": "x",
                "total_volumes": 3,
                "chapters_per_volume": 9,
                "tension_template": "heros_journey",
            },
        )
        assert resp.status_code == 202
        info = resp.json()["data"]
        final = task_registry.get(info["task_id"])
        assert final is not None
        assert final.status == "failed"
        assert final.error["code"] in (
            "WORKSPACE_NOT_FOUND",
            "WORKSPACE_INVALID",
            "WORKSPACE_CONFLICT",
        )


@pytest.mark.parametrize("endpoint,method,mode", [
    ("generate", "generate_outline", "opening_segment"),
    ("regenerate-section", "regenerate_section", "opening_segment"),
    ("regenerate-section", "regenerate_section", None),
])
def test_planning_scope_mode_reaches_background_service(client, workspace_id, monkeypatch, endpoint, method, mode):
    from types import SimpleNamespace
    from src.services.outline_service import OutlineReadinessError
    seen = []
    def capture(self, ws, **kwargs):
        seen.append((ws, kwargs))
        if endpoint == "generate":
            return SimpleNamespace(world=None, characters=None, plot=None, volume_plans=None, mini_plan=None)
        raise OutlineReadinessError("isolated API capture")
    monkeypatch.setattr(OutlineService, method, capture)
    body = {"premise": "普通人成长", "genre": "都市", "total_volumes": 2,
            "chapters_per_volume": 12, "tension_template": "heros_journey"}
    if mode is not None:
        body["planning_mode"] = mode
    if endpoint == "regenerate-section":
        body["section"] = "world"
    response = client.post(f"/api/v1/workspaces/{workspace_id}/outline:{endpoint}", json=body)
    assert response.status_code == 202
    assert len(seen) == 1 and seen[0][0] == workspace_id
    assert seen[0][1]["planning_mode"] == mode
