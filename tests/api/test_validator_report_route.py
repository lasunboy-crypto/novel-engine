"""``GET /chapters/{id}/validators/latest`` 路由集成测试(P6-B-11)。

覆盖 5 个用例:
1. happy:端点 200 + ValidatorReportOut schema 字段全
2. error1:章节卡不存在 → 404 ``CHAPTER_CARD_NOT_FOUND``
3. error2:final.md 不存在 → 404 ``VALIDATOR_REPORT_NOT_FOUND``
4. error3:missing ``X-Workspace-Id`` → 422
5. logic:must_achieve 部分命中 → 200 + passed=False + details 透 items_missing
   (端点是查询性 GET,不应因校验失败返 422)

测试基础设施沿用 ``test_chapter_retry_references.py`` 同款。
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
from src.api.deps import (
    get_chapter_service,
    get_validator_report_service,
)
from src.api.main import create_app
from src.core.db import Base
from src.core.db.models import CharacterORM, WorkspaceORM
from src.core.llm.mock_client import MockLLMClient
from src.core.models.chapter_card import ChapterCard, ContinuityContext
from src.services.chapter_service import ChapterService
from src.services.validator_report_service import ValidatorReportService
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
        "ws-validator-route",
        display_name="P6-B-11 validator route test",
        genre="xianxia",
        premise="test",
        activate=False,
    )
    return "ws-validator-route"


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
    session_factory: Callable[[str], Session],
    workspace_id: str,
    character_id: str,
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


def _make_card(
    chapter_id: str,
    *,
    workspace_id: str = "ws-validator-route",
    must_achieve: list[str] | None = None,
    forbidden: list[str] | None = None,
    related_characters: list[str] | None = None,
) -> ChapterCard:
    return ChapterCard(
        chapter_id=chapter_id,
        workspace_id=workspace_id,
        volume_id="vol-1",
        chapter_number=1,
        title="测试章",
        tension_level=5,
        word_count_min=1,
        word_count_max=100_000,
        must_achieve=must_achieve if must_achieve is not None else [],
        must_unresolved=[],
        forbidden=forbidden if forbidden is not None else [],
        pov_character=(related_characters or ["char-protagonist"])[0],
        setting="测试场景",
        continuity_context=ContinuityContext(
            prev_chapter_tail="(开篇)",
            unfinished_actions=[],
            time_gap="开篇",
            scene_continuation="switch",
            pov_change=False,
            pov_from=None,
            pov_to=(related_characters or ["char-protagonist"])[0],
        ),
        next_chapter_seed="种子",
        arc_phase="opening",
        is_arc_finale=False,
        related_plot_threads=[],
        related_characters=related_characters or [],
    )


def _write_final_md(
    workspace_service: WorkspaceService,
    workspace_id: str,
    chapter_id: str,
    content: str,
) -> None:
    chapter_dir = (
        workspace_service.workspaces_root
        / workspace_id
        / "data"
        / "chapters"
        / chapter_id
    )
    chapter_dir.mkdir(parents=True, exist_ok=True)
    (chapter_dir / "final.md").write_text(content, encoding="utf-8")


@pytest.fixture
def client(
    workspace_service: WorkspaceService,
    session_factory: Callable[[str], Session],
    workspace_id: str,
) -> Iterator[TestClient]:
    app = create_app()
    chapter_service = ChapterService(
        workspace_service=workspace_service,
        client=MockLLMClient(),
        model="claude-sonnet-4-6",
        session_factory=session_factory,
    )
    validator_service = ValidatorReportService(
        workspace_service=workspace_service,
        chapter_service=chapter_service,
        session_factory=session_factory,
    )
    app.dependency_overrides[get_chapter_service] = lambda: chapter_service
    app.dependency_overrides[get_validator_report_service] = (
        lambda: validator_service
    )
    with TestClient(app) as c:
        yield c
    app.dependency_overrides.clear()


# ---- 测试 -------------------------------------------------------------------


class TestValidatorReportRoute:
    def test_happy_path_returns_200_with_full_schema(
        self,
        client: TestClient,
        workspace_service: WorkspaceService,
        workspace_id: str,
        session_factory: Callable[[str], Session],
    ) -> None:
        _seed_workspace_orm(session_factory, workspace_id)
        _seed_character_orm(session_factory, workspace_id, "char-protagonist")
        from src.services.chapter_service import ChapterService

        chapter_service = ChapterService(
            workspace_service=workspace_service,
            client=MockLLMClient(),
            model="claude-sonnet-4-6",
            session_factory=session_factory,
        )
        card = _make_card(
            "ch-0001",
            workspace_id=workspace_id,
            must_achieve=["李清玄出场"],
            related_characters=["char-protagonist"],
        )
        chapter_service.save_chapter_card(workspace_id, card)
        chapter_text = "李清玄出场,踏入丹道阁,开启重生之路。" * 10
        _write_final_md(workspace_service, workspace_id, "ch-0001", chapter_text)

        resp = client.get(
            "/api/v1/chapters/ch-0001/validators/latest",
            headers={"X-Workspace-Id": workspace_id},
        )
        assert resp.status_code == 200, resp.text
        body = resp.json()
        data = body["data"]
        assert data["chapter_id"] == "ch-0001"
        assert data["workspace_id"] == workspace_id
        assert data["passed"] is True
        assert data["failed_constraint_names"] == []
        assert data["chapter_text_length"] == len(chapter_text)
        # validator 列表必含 3 节奏类核心
        # (v1.0.x A2-02 起 must_achieve 移交 LLM validator,节奏类从 4 降到 3)
        names = {v["name"] for v in data["validators"]}
        assert {"word_count", "must_unresolved", "forbidden"}.issubset(names)

    def test_card_missing_returns_404_card_not_found(
        self,
        client: TestClient,
        workspace_id: str,
        session_factory: Callable[[str], Session],
    ) -> None:
        """章节卡不存在 → 404 ``CHAPTER_CARD_NOT_FOUND``(路由前置守卫)。"""
        _seed_workspace_orm(session_factory, workspace_id)
        resp = client.get(
            "/api/v1/chapters/ch-9999/validators/latest",
            headers={"X-Workspace-Id": workspace_id},
        )
        assert resp.status_code == 404, resp.text
        assert resp.json()["error"]["code"] == "CHAPTER_CARD_NOT_FOUND"

    def test_final_md_missing_returns_404_validator_report_not_found(
        self,
        client: TestClient,
        workspace_service: WorkspaceService,
        workspace_id: str,
        session_factory: Callable[[str], Session],
    ) -> None:
        """章节卡有 + final.md 没 → 404 ``VALIDATOR_REPORT_NOT_FOUND``。"""
        _seed_workspace_orm(session_factory, workspace_id)
        from src.services.chapter_service import ChapterService

        chapter_service = ChapterService(
            workspace_service=workspace_service,
            client=MockLLMClient(),
            model="claude-sonnet-4-6",
            session_factory=session_factory,
        )
        card = _make_card(
            "ch-0001",
            workspace_id=workspace_id,
            related_characters=["char-protagonist"],
        )
        chapter_service.save_chapter_card(workspace_id, card)
        # **不**写 final.md

        resp = client.get(
            "/api/v1/chapters/ch-0001/validators/latest",
            headers={"X-Workspace-Id": workspace_id},
        )
        assert resp.status_code == 404, resp.text
        assert resp.json()["error"]["code"] == "VALIDATOR_REPORT_NOT_FOUND"

    def test_missing_workspace_header_returns_422(
        self, client: TestClient
    ) -> None:
        resp = client.get("/api/v1/chapters/ch-0001/validators/latest")
        assert resp.status_code == 422, resp.text
        assert resp.json()["error"]["code"] == "MISSING_WORKSPACE_HEADER"

    def test_forbidden_hit_returns_200_with_failed_passed_flag(
        self,
        client: TestClient,
        workspace_service: WorkspaceService,
        workspace_id: str,
        session_factory: Callable[[str], Session],
    ) -> None:
        """validator 失败不应返 422 — 端点是查询性 GET,passed=False 在 200 响应内。

        v1.0.x A2-02 起 must_achieve 移交 LLM validator,本测试 reframe 用
        forbidden 触发 pipeline-level "passed=False" 200 响应契约。
        """
        _seed_workspace_orm(session_factory, workspace_id)
        _seed_character_orm(session_factory, workspace_id, "char-protagonist")
        from src.services.chapter_service import ChapterService

        chapter_service = ChapterService(
            workspace_service=workspace_service,
            client=MockLLMClient(),
            model="claude-sonnet-4-6",
            session_factory=session_factory,
        )
        card = _make_card(
            "ch-0001",
            workspace_id=workspace_id,
            forbidden=["手机"],
            related_characters=["char-protagonist"],
        )
        chapter_service.save_chapter_card(workspace_id, card)
        _write_final_md(
            workspace_service,
            workspace_id,
            "ch-0001",
            "他掏出手机查看消息。" * 50,
        )

        resp = client.get(
            "/api/v1/chapters/ch-0001/validators/latest",
            headers={"X-Workspace-Id": workspace_id},
        )
        assert resp.status_code == 200, resp.text
        data = resp.json()["data"]
        assert data["passed"] is False
        assert "forbidden" in data["failed_constraint_names"]
        forbidden_result = next(
            v for v in data["validators"] if v["name"] == "forbidden"
        )
        assert forbidden_result["passed"] is False
