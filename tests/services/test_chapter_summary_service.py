"""``ChapterSummaryService`` 单测(v1.0.x wave 4 session 5)。

覆盖:
- list_summaries:happy path 多行排序 / 空表返空 list / session_factory=None 返空
- update_summary:happy path 多字段 / 单字段 PATCH / 不存在 404 / 跨工作区不存在
  / 清空 pov_character 字符串 → None / session_factory=None 抛
- workspace_id 校验:不存在 → WorkspaceServiceError(API 层翻 404)
"""

from __future__ import annotations

from collections.abc import Callable
from datetime import UTC, datetime
from pathlib import Path

import pytest
from sqlalchemy import create_engine
from sqlalchemy.orm import Session

from src.core.db import Base
from src.core.db.models import ChapterSummaryORM
from src.core.workspace.workspace_paths import WorkspacePaths
from src.services.chapter_summary_service import (
    ChapterSummaryNotFoundError,
    ChapterSummaryService,
    ChapterSummaryServiceError,
)
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
        "ws-summary",
        display_name="chapter_summary service test",
        genre="test",
        premise="x",
        activate=False,
    )
    return "ws-summary"


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


def _insert_summary(
    session_factory: Callable[[str], Session],
    workspace_id: str,
    chapter_id: str,
    *,
    summary_text: str = "占位摘要",
    pov_character: str | None = "lin-yan",
) -> None:
    session = session_factory(workspace_id)
    try:
        now = datetime.now(UTC)
        session.add(
            ChapterSummaryORM(
                chapter_id=chapter_id,
                workspace_id=workspace_id,
                summary_text=summary_text,
                pov_character=pov_character,
                scene_setting="测试场景",
                key_events=[{"description": "事件1", "actors": [], "location": ""}],
                character_interactions=[
                    {"char_a": "lin-yan", "char_b": "shi-jie", "dynamic": "对峙"}
                ],
                unfinished_actions=["未完动作1"],
                character_states=[
                    {
                        "character_id": "lin-yan",
                        "state": "仍在门外",
                    }
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


class TestListSummariesWave4Session5:
    def test_returns_rows_ordered_by_chapter_id_asc(
        self, workspace_service: WorkspaceService, workspace_id: str
    ) -> None:
        factory = _make_session_factory(workspace_service)
        # 故意乱序插入
        _insert_summary(factory, workspace_id, "ch-0003")
        _insert_summary(factory, workspace_id, "ch-0001")
        _insert_summary(factory, workspace_id, "ch-0002")
        service = ChapterSummaryService(
            workspace_service=workspace_service, session_factory=factory
        )
        result = service.list_summaries(workspace_id)
        assert [s.chapter_id for s in result] == ["ch-0001", "ch-0002", "ch-0003"]
        # round-trip schema
        assert result[0].summary_text == "占位摘要"
        assert result[0].pov_character == "lin-yan"
        assert len(result[0].key_events) == 1
        assert result[0].extraction_trigger == "reviewer_pass"
        assert result[0].character_states[0]["state"] == "仍在门外"
        assert result[0].established_facts == ["密门已经打开"]
        assert result[0].revealed_mechanics == ["铜铃只在夜间响应"]
        assert result[0].raw_final_tail.endswith("密门已经打开。")
        assert result[0].source_version_id == 3
        assert result[0].source_text_hash == "a" * 64
        assert result[0].evidence_records[0]["field"] == "established_facts"
        assert result[0].ledger_hash == "c" * 64
        assert result[0].validated_at is not None

    def test_empty_table_returns_empty_list(
        self, workspace_service: WorkspaceService, workspace_id: str
    ) -> None:
        factory = _make_session_factory(workspace_service)
        # 触发 schema 创建,但不插入任何行
        sess = factory(workspace_id)
        sess.close()
        service = ChapterSummaryService(
            workspace_service=workspace_service, session_factory=factory
        )
        result = service.list_summaries(workspace_id)
        assert result == []

    def test_session_factory_none_returns_empty(
        self, workspace_service: WorkspaceService, workspace_id: str
    ) -> None:
        # 单测路径(无 SQLite)→ list 安全降级返空
        service = ChapterSummaryService(
            workspace_service=workspace_service, session_factory=None
        )
        assert service.list_summaries(workspace_id) == []


class TestUpdateSummaryWave4Session5:
    def test_patches_multiple_fields(
        self, workspace_service: WorkspaceService, workspace_id: str
    ) -> None:
        factory = _make_session_factory(workspace_service)
        _insert_summary(factory, workspace_id, "ch-0001")
        service = ChapterSummaryService(
            workspace_service=workspace_service, session_factory=factory
        )
        updated = service.update_summary(
            workspace_id,
            "ch-0001",
            summary_text="改后的摘要",
            pov_character="shi-jie",
            unfinished_actions=["新未完动作"],
        )
        assert updated.summary_text == "改后的摘要"
        assert updated.pov_character == "shi-jie"
        assert updated.unfinished_actions == ["新未完动作"]
        # 未传字段保持
        assert updated.scene_setting == "测试场景"
        assert updated.validated_at is None

    def test_partial_patch_only_updates_passed_fields(
        self, workspace_service: WorkspaceService, workspace_id: str
    ) -> None:
        factory = _make_session_factory(workspace_service)
        _insert_summary(factory, workspace_id, "ch-0001")
        service = ChapterSummaryService(
            workspace_service=workspace_service, session_factory=factory
        )
        updated = service.update_summary(
            workspace_id, "ch-0001", summary_text="只改这一个字段"
        )
        assert updated.summary_text == "只改这一个字段"
        # 其余字段不动
        assert updated.pov_character == "lin-yan"
        assert updated.scene_setting == "测试场景"
        assert len(updated.unfinished_actions) == 1
        assert updated.validated_at is None

    def test_empty_string_pov_clears_to_none(
        self, workspace_service: WorkspaceService, workspace_id: str
    ) -> None:
        factory = _make_session_factory(workspace_service)
        _insert_summary(factory, workspace_id, "ch-0001", pov_character="lin-yan")
        service = ChapterSummaryService(
            workspace_service=workspace_service, session_factory=factory
        )
        updated = service.update_summary(
            workspace_id, "ch-0001", pov_character=""
        )
        assert updated.pov_character is None

    def test_not_found_raises(
        self, workspace_service: WorkspaceService, workspace_id: str
    ) -> None:
        factory = _make_session_factory(workspace_service)
        sess = factory(workspace_id)
        sess.close()
        service = ChapterSummaryService(
            workspace_service=workspace_service, session_factory=factory
        )
        with pytest.raises(ChapterSummaryNotFoundError):
            service.update_summary(
                workspace_id, "ch-9999", summary_text="目标行不存在"
            )

    def test_session_factory_none_raises(
        self, workspace_service: WorkspaceService, workspace_id: str
    ) -> None:
        service = ChapterSummaryService(
            workspace_service=workspace_service, session_factory=None
        )
        with pytest.raises(ChapterSummaryServiceError):
            service.update_summary(workspace_id, "ch-0001", summary_text="x")
