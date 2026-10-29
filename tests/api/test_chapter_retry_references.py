"""``POST /chapters/{id}:retry-references`` 路由集成测试(P6-B-10)。

覆盖 4 个用例:
1. happy:retry-references 端点 200 + 走 strict ID 约束 → 返合法 ChapterCardOut
2. error:retry 仍失败 → 422 ``REFERENCE_VALIDATION_FAILED`` + details unknown_ids
3. 缺 ``X-Workspace-Id`` → 422
4. body 缺字段 → 422

测试基础设施沿用 P5-B-06 ``test_chapter_card_generate_route.py`` 同款。
"""

from __future__ import annotations

from collections.abc import Callable, Iterator
from datetime import UTC, datetime
from pathlib import Path

import pytest
from fastapi.testclient import TestClient
from sqlalchemy import create_engine
from sqlalchemy.orm import Session
from sqlalchemy.pool import StaticPool

from src.api import task_registry
from src.api.deps import get_chapter_card_service, get_outline_service
from src.api.main import create_app
from src.core.agents.chapter_card_generator import ChapterCardGenerationContext
from src.core.db import Base
from src.core.db.models import CharacterORM, WorkspaceORM
from src.core.llm.mock_client import MockLLMClient
from src.core.models.chapter_card import ChapterCard, ContinuityContext
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
        "ws-retry-refs",
        display_name="P6-B-10 retry-references route test",
        genre="xianxia",
        premise="test",
        activate=False,
    )
    return "ws-retry-refs"


@pytest.fixture
def session_factory() -> Callable[[str], Session]:
    engine = create_engine(
        "sqlite:///:memory:",
        connect_args={"check_same_thread": False},
        poolclass=StaticPool,
    )
    Base.metadata.create_all(engine)

    def factory(workspace_id: str) -> Session:
        return Session(engine)

    return factory


@pytest.fixture(autouse=True)
def _clear_task_registry() -> Iterator[None]:
    task_registry.clear()
    yield
    task_registry.clear()


def _seed_workspace_orm(
    session_factory: Callable[[str], Session], workspace_id: str
) -> None:
    session = session_factory(workspace_id)
    try:
        now = datetime.now(UTC)
        session.merge(
            WorkspaceORM(
                workspace_id=workspace_id,
                display_name="ws",
                genre="xianxia",
                premise="test",
                created_at=now,
                last_active_at=now,
                engine_version="0.1.0",
                status="active",
                total_chapters=12,
                completed_chapters=0,
                config_overrides={},
            )
        )
        session.commit()
    finally:
        session.close()


def _seed_character_orm(
    session_factory: Callable[[str], Session], workspace_id: str, character_id: str
) -> None:
    session = session_factory(workspace_id)
    try:
        session.merge(
            CharacterORM(
                character_id=character_id,
                workspace_id=workspace_id,
                name=character_id,
                aliases=[],
                archetype="主角",
                appearance="(test)",
                personality="(test)",
                background="(test)",
                voice_profile={
                    "description": "test voice",
                    "catchphrases": [],
                    "forbidden_phrases": [],
                    "tone": "neutral",
                    "sentence_style": "short",
                    "self_address": "我",
                    "addresses_others": {},
                },
                goals_long_term=[],
                goals_short_term=[],
                current_state={},
                relationships={},
                abilities=[],
                secrets=[],
                first_appearance_chapter=None,
                last_updated_chapter=None,
                revoked_at=None,
            )
        )
        session.commit()
    finally:
        session.close()


def _write_outline_fixtures(
    workspace_service: WorkspaceService, workspace_id: str
) -> None:
    """落 4 段 outline JSON;直接复用 test_chapter_card_service.py 同款。"""
    from tests.services.test_chapter_card_service import (
        _write_outline_fixtures as _shared_write,
    )

    _shared_write(workspace_service, workspace_id)


def _make_card(
    chapter_id: str,
    *,
    pov_character: str,
    related_characters: list[str],
) -> ChapterCard:
    return ChapterCard(
        chapter_id=chapter_id,
        workspace_id="ws-retry-refs",
        volume_id="vol-1",
        chapter_number=1,
        title="测试章",
        tension_level=5,
        must_achieve=["事件 A"],
        must_unresolved=[],
        forbidden=[],
        pov_character=pov_character,
        setting="测试场景",
        continuity_context=ContinuityContext(
            prev_chapter_tail="(开篇)",
            unfinished_actions=[],
            time_gap="开篇",
            scene_continuation="switch",
            pov_change=False,
            pov_from=None,
            pov_to=pov_character,
        ),
        next_chapter_seed="种子",
        arc_phase="opening",
        is_arc_finale=False,
        related_plot_threads=[],
        related_characters=related_characters,
    )


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


# ---- 测试 -------------------------------------------------------------------


class TestRetryReferencesRoute:
    def test_happy_path_returns_200_with_strict_card(
        self,
        client: TestClient,
        workspace_service: WorkspaceService,
        workspace_id: str,
        session_factory: Callable[[str], Session],
        monkeypatch: pytest.MonkeyPatch,
    ) -> None:
        _write_outline_fixtures(workspace_service, workspace_id)
        _seed_workspace_orm(session_factory, workspace_id)
        _seed_character_orm(session_factory, workspace_id, "char-protagonist")

        # monkeypatch retry path 走严格 ID 约束 mock
        def _strict_mock(ctx: ChapterCardGenerationContext, **kwargs: object) -> ChapterCard:  # type: ignore[no-untyped-def]
            assert ctx.is_retry_with_strict_ids is True, "retry 必须带 strict flag"
            pov = ctx.character_voices[0].character_id
            return _make_card(
                ctx.chapter_id,
                pov_character=pov,
                related_characters=[pov],
            )

        monkeypatch.setattr(
            "src.services.chapter_card_service.generate_chapter_card",
            _strict_mock,
        )

        resp = client.post(
            "/api/v1/chapters/ch-0001:retry-references",
            headers={"X-Workspace-Id": workspace_id},
            json={"volume_id": "vol-1", "chapter_number": 1},
        )
        assert resp.status_code == 200, resp.text
        body = resp.json()
        # v1.0.x wave 5 session 4:response data 加 cascade 字段
        # (data.card + invalidated_chapter_ids + cascade_regenerate_task_id)
        data = body["data"]
        card = data["card"]
        assert card["chapter_id"] == "ch-0001"
        assert card["pov_character"] == "char-protagonist"
        assert card["card_status"] == "draft"
        # 单章 fixture 无后续 → 空 cascade
        assert data["invalidated_chapter_ids"] == []
        assert data["cascade_regenerate_task_id"] is None

    def test_retry_still_invalid_returns_422(
        self,
        client: TestClient,
        workspace_service: WorkspaceService,
        workspace_id: str,
        session_factory: Callable[[str], Session],
        monkeypatch: pytest.MonkeyPatch,
    ) -> None:
        _write_outline_fixtures(workspace_service, workspace_id)
        _seed_workspace_orm(session_factory, workspace_id)
        _seed_character_orm(session_factory, workspace_id, "char-protagonist")

        # 强制 agent 仍返 bai-ling(模拟 LLM 不听话)
        def _always_invalid(ctx: ChapterCardGenerationContext, **kwargs: object) -> ChapterCard:  # type: ignore[no-untyped-def]
            return _make_card(
                ctx.chapter_id,
                pov_character="bai-ling",
                related_characters=["bai-ling"],
            )

        monkeypatch.setattr(
            "src.services.chapter_card_service.generate_chapter_card",
            _always_invalid,
        )

        resp = client.post(
            "/api/v1/chapters/ch-0001:retry-references",
            headers={"X-Workspace-Id": workspace_id},
            json={"volume_id": "vol-1", "chapter_number": 1},
        )
        assert resp.status_code == 422, resp.text
        body = resp.json()
        assert body["error"]["code"] == "REFERENCE_VALIDATION_FAILED"
        details = body["error"]["details"]
        assert details["workspace_id"] == workspace_id
        assert details["chapter_id"] == "ch-0001"
        assert details["unknown_character_ids"] == ["bai-ling"]
        assert details["unknown_thread_ids"] == []

    def test_missing_workspace_header_returns_422(
        self, client: TestClient
    ) -> None:
        resp = client.post(
            "/api/v1/chapters/ch-0001:retry-references",
            json={"volume_id": "vol-1", "chapter_number": 1},
        )
        assert resp.status_code == 422

    def test_missing_body_field_returns_422(
        self,
        client: TestClient,
        workspace_id: str,
    ) -> None:
        resp = client.post(
            "/api/v1/chapters/ch-0001:retry-references",
            headers={"X-Workspace-Id": workspace_id},
            json={"volume_id": "vol-1"},  # 缺 chapter_number
        )
        assert resp.status_code == 422
