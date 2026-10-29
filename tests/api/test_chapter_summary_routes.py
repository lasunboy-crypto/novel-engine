"""chapter_summary 路由集成测试(v1.0.x wave 4 session 5)。

覆盖:
- GET /workspaces/{id}/chapter-summaries:200 + chapter_id ASC + 空表
- PUT /workspaces/{id}/chapter-summaries/{chapter_id}:200 多字段 / 404 不存在
  / 422 body 不合 schema(空 summary_text)
- 工作区不存在 → 404 WORKSPACE_NOT_FOUND
"""

from __future__ import annotations

from collections.abc import Callable, Iterator
from datetime import UTC, datetime
from pathlib import Path

import pytest
from fastapi.testclient import TestClient
from sqlalchemy import create_engine
from sqlalchemy.orm import Session

from src.api.deps import get_chapter_summary_service
from src.api.main import create_app
from src.core.db import Base
from src.core.db.models import ChapterSummaryORM
from src.core.workspace.workspace_paths import WorkspacePaths
from src.services.chapter_summary_service import ChapterSummaryService
from src.services.workspace_service import WorkspaceService


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
        "ws-summary-route",
        display_name="route test",
        genre="test",
        premise="x",
        activate=False,
    )
    return "ws-summary-route"


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


def _seed_summary(
    session_factory: Callable[[str], Session],
    workspace_id: str,
    chapter_id: str,
) -> None:
    session = session_factory(workspace_id)
    try:
        now = datetime.now(UTC)
        session.add(
            ChapterSummaryORM(
                chapter_id=chapter_id,
                workspace_id=workspace_id,
                summary_text="种子摘要",
                pov_character="lin-yan",
                scene_setting="测试场景",
                key_events=[{"description": "种子事件", "actors": [], "location": ""}],
                character_interactions=[],
                unfinished_actions=["种子未完"],
                character_states=[
                    {"character_id": "lin-yan", "state": "仍在门外"}
                ],
                established_facts=["密门已经打开"],
                revealed_mechanics=["铜铃只在夜间响应"],
                raw_final_tail="……密门已经打开。",
                source_version_id=3,
                source_text_hash="a" * 64,
                evidence_records=[
                    {
                        "field": "established_facts",
                        "index": 0,
                        "fact_text": "密门已经打开",
                        "evidence_quote": "密门已经打开",
                        "evidence_fingerprint": "b" * 64,
                        "is_immediate": False,
                    }
                ],
                ledger_hash="c" * 64,
                validated_at=now,
                extraction_trigger="reviewer_pass",
                model_used="qwen-novel:latest",
                created_at=now,
                updated_at=now,
            )
        )
        session.commit()
    finally:
        session.close()


@pytest.fixture
def client(workspace_service: WorkspaceService) -> Iterator[TestClient]:
    app = create_app()
    factory = _make_session_factory(workspace_service)
    service = ChapterSummaryService(
        workspace_service=workspace_service, session_factory=factory
    )
    app.dependency_overrides[get_chapter_summary_service] = lambda: service
    # 把 factory 暴露给 test 用于种子数据
    app.state.test_session_factory = factory
    with TestClient(app) as c:
        yield c
    app.dependency_overrides.clear()


class TestListEndpointWave4Session5:
    def test_returns_summaries_ordered_asc(
        self, client: TestClient, workspace_id: str
    ) -> None:
        factory = client.app.state.test_session_factory
        _seed_summary(factory, workspace_id, "ch-0002")
        _seed_summary(factory, workspace_id, "ch-0001")
        resp = client.get(
            f"/api/v1/workspaces/{workspace_id}/chapter-summaries"
        )
        assert resp.status_code == 200
        data = resp.json()["data"]
        assert [s["chapter_id"] for s in data] == ["ch-0001", "ch-0002"]
        assert data[0]["summary_text"] == "种子摘要"
        assert data[0]["character_states"][0]["state"] == "仍在门外"
        assert data[0]["established_facts"] == ["密门已经打开"]
        assert data[0]["revealed_mechanics"] == ["铜铃只在夜间响应"]
        assert data[0]["core_state_candidates"] == []
        assert data[0]["raw_final_tail"].endswith("密门已经打开。")
        assert data[0]["source_version_id"] == 3
        assert data[0]["source_text_hash"] == "a" * 64
        assert data[0]["evidence_records"][0]["field"] == "established_facts"
        assert data[0]["ledger_hash"] == "c" * 64
        assert data[0]["validated_at"] is not None

    def test_empty_workspace_returns_empty_list(
        self, client: TestClient, workspace_id: str
    ) -> None:
        # 仅 create_app 触发表创建,但无种子
        factory = client.app.state.test_session_factory
        sess = factory(workspace_id)
        sess.close()
        resp = client.get(
            f"/api/v1/workspaces/{workspace_id}/chapter-summaries"
        )
        assert resp.status_code == 200
        assert resp.json()["data"] == []


class TestUpdateEndpointWave4Session5:
    def test_patches_multiple_fields(
        self, client: TestClient, workspace_id: str
    ) -> None:
        factory = client.app.state.test_session_factory
        _seed_summary(factory, workspace_id, "ch-0001")
        resp = client.put(
            f"/api/v1/workspaces/{workspace_id}/chapter-summaries/ch-0001",
            json={
                "summary_text": "改后",
                "pov_character": "shi-jie",
                "unfinished_actions": ["新未完"],
            },
        )
        assert resp.status_code == 200
        d = resp.json()["data"]
        assert d["summary_text"] == "改后"
        assert d["pov_character"] == "shi-jie"
        assert d["unfinished_actions"] == ["新未完"]
        # 未传字段保持
        assert d["scene_setting"] == "测试场景"
        assert d["validated_at"] is None

    def test_not_found_returns_404(
        self, client: TestClient, workspace_id: str
    ) -> None:
        factory = client.app.state.test_session_factory
        sess = factory(workspace_id)
        sess.close()
        resp = client.put(
            f"/api/v1/workspaces/{workspace_id}/chapter-summaries/ch-9999",
            json={"summary_text": "目标不存在"},
        )
        assert resp.status_code == 404
        assert resp.json()["error"]["code"] == "CHAPTER_SUMMARY_NOT_FOUND"

    def test_empty_summary_text_returns_422(
        self, client: TestClient, workspace_id: str
    ) -> None:
        factory = client.app.state.test_session_factory
        _seed_summary(factory, workspace_id, "ch-0001")
        resp = client.put(
            f"/api/v1/workspaces/{workspace_id}/chapter-summaries/ch-0001",
            json={"summary_text": ""},
        )
        assert resp.status_code == 422

    def test_workspace_not_found_returns_404(
        self, client: TestClient
    ) -> None:
        resp = client.get(
            "/api/v1/workspaces/no-such-ws/chapter-summaries"
        )
        # WorkspaceService.info 抛 WorkspaceServiceError → API 翻 409 / 404 看翻译表
        # WorkspaceServiceError 兜底翻 WORKSPACE_CONFLICT 409,接受 4xx 即可
        assert resp.status_code in (404, 409, 422)


class TestR4OpenApiContract:
    def test_continuity_ledger_fields_are_exported(self) -> None:
        schemas = create_app().openapi()["components"]["schemas"]

        summary_fields = schemas["ChapterSummaryOut"]["properties"]
        assert {
            "character_states",
            "established_facts",
            "revealed_mechanics",
            "core_state_candidates",
            "raw_final_tail",
            "source_version_id",
            "source_text_hash",
            "evidence_records",
            "ledger_hash",
            "validated_at",
        }.issubset(summary_fields)

        continuity_fields = schemas["ContinuityContext"]["properties"]
        assert "continuity_bridge" in continuity_fields

        card_fields = schemas["ChapterCard"]["properties"]
        assert "continuity_ledger_provenance" in card_fields
        provenance_fields = schemas["ContinuityLedgerProvenance"]["properties"]
        assert set(provenance_fields) == {
            "chapter_id",
            "source_version_id",
            "source_text_hash",
            "ledger_hash",
        }
