"""P4-B-10:StyleAuditService 单元 + 集成测试。

覆盖契约:

1. ``persist_style_audit_safely`` helper(单元层 5 测):
   - 给定 session_factory + 完整 entry → 落 1 行 ``style_audit_history``
     + structlog ``style_audit_persisted`` event
   - ``session_factory=None`` → no-op
   - session_factory 抛 open → swallow + structlog
     ``style_audit_persist_failed`` stage=open_session
   - 多次调用同 service 落多行(audit_id 唯一,**不去重**)
   - commit 失败 → rollback + swallow + structlog
     ``style_audit_persist_failed`` stage=commit

2. ``StyleAuditService.run_audit`` 集成层(v1.1 w4-s5 / I2.2 StyleAuditor 落地后):
   - 无 chapter_range + 未选模板 → 短路返 ok / drift=0(不调 LLM)
   - chapter_range_start + _end 都存在 → 落 1 行 + 字段透传
   - chapter_range_start 不存在 → ``StyleAuditServiceError``
   - chapter_range_end 不存在 → ``StyleAuditServiceError``
   - workspace_id 不存在 → ``MetaFileError`` / ``WorkspaceServiceError``
     冒泡(由 WorkspaceService.info 抛,本 service 不捕)
   - **选了模板 + 有版本正文 + 注入 router → 调 LLM 返 drift_score + severity**
     (mock router 返低 drift → severity=ok + dimension_notes 落 report_json)
"""

from __future__ import annotations

from datetime import UTC, datetime
from pathlib import Path
from types import SimpleNamespace

import pytest
from sqlalchemy import create_engine, select
from sqlalchemy.orm import Session
from structlog.testing import capture_logs

from src.core.chapter_versioning import ChapterVersioning
from src.core.db import Base
from src.core.db.models import ChapterORM, StyleAuditEntryORM, WorkspaceORM
from src.core.llm.mock_client import MockLLMClient
from src.core.models.metrics import StyleAuditEntry
from src.core.workspace.workspace_paths import WorkspacePaths
from src.services.style_audit_service import (
    StyleAuditService,
    StyleAuditServiceError,
    persist_style_audit_safely,
)
from src.services.workspace_service import WorkspaceService


def _fake_router_factory(client):
    """构造返回固定 mock client 的 router_factory(走 router.route 路径)。"""

    def _route(agent_name, chapter_card=None):
        return SimpleNamespace(
            client=client,
            profile=SimpleNamespace(model="mock-style-auditor"),
            complete_kwargs={},
        )

    return lambda _workspace_id: SimpleNamespace(route=_route)


# =============================================================================
# 共享 fixture
# =============================================================================


@pytest.fixture
def workspace_service(tmp_path: Path) -> WorkspaceService:
    workspaces_root = tmp_path / "workspaces"
    archives_root = tmp_path / "archives"
    workspaces_root.mkdir()
    archives_root.mkdir()
    return WorkspaceService(
        workspaces_root=workspaces_root, archives_root=archives_root
    )


def _make_session_factory(workspace_service: WorkspaceService):
    def _factory(workspace_id: str) -> Session:
        paths = WorkspacePaths(
            workspace_id, workspaces_root=workspace_service.workspaces_root
        )
        engine = create_engine(f"sqlite:///{paths.db_path}")
        Base.metadata.create_all(engine)
        return Session(engine)

    return _factory


@pytest.fixture
def workspace_id(workspace_service: WorkspaceService) -> str:
    workspace_service.create(
        "ws-style",
        display_name="P4-B-10 style audit 测试工作区",
        genre="test",
        premise="x",
        activate=False,
    )
    return "ws-style"


@pytest.fixture
def seeded_workspace(
    workspace_service: WorkspaceService, workspace_id: str
) -> str:
    """Seed 2 章节供 chapter_range 校验测试。"""
    factory = _make_session_factory(workspace_service)
    session = factory(workspace_id)
    try:
        session.add(
            WorkspaceORM(
                workspace_id=workspace_id,
                display_name="seed",
                genre="test",
                premise="x",
                created_at=datetime.now(UTC),
                last_active_at=datetime.now(UTC),
                engine_version="0.1",
                status="active",
            )
        )
        for n in (1, 2):
            session.add(
                ChapterORM(
                    chapter_id=f"ch-{n:04d}",
                    workspace_id=workspace_id,
                    current_version=1,
                    status="drafting",
                    created_at=datetime.now(UTC),
                )
            )
        session.commit()
    finally:
        session.close()
    return workspace_id


def _make_entry(
    severity: str = "ok",
    *,
    audit_id: str | None = None,
    chapter_range_start: str | None = None,
    chapter_range_end: str | None = None,
) -> StyleAuditEntry:
    # v1.1 w4-s5 / I2.2:severity 升 4 档 Literal + ORM CHECK 约束,默认 "ok";
    # drift_score 0.0(短路 / fail-open 语义),report_json 含 dimension_notes。
    return StyleAuditEntry(
        audit_id=audit_id or "sty-test123abcde",
        audited_at=datetime.now(UTC),
        chapter_range_start=chapter_range_start,
        chapter_range_end=chapter_range_end,
        drift_score=0.0,
        severity=severity,  # type: ignore[arg-type]
        report_json={
            "summary": "无偏移基线",
            "model_used": None,
            "dimension_notes": [],
        },
    )


# =============================================================================
# persist_style_audit_safely 单元
# =============================================================================


class TestPersistStyleAuditSafely:
    def test_persists_row_and_emits_event(
        self, workspace_service: WorkspaceService, workspace_id: str
    ) -> None:
        factory = _make_session_factory(workspace_service)
        entry = _make_entry()
        with capture_logs() as logs:
            persist_style_audit_safely(
                factory, workspace_id=workspace_id, entry=entry
            )
        # 行落表
        session = factory(workspace_id)
        try:
            rows = session.execute(select(StyleAuditEntryORM)).scalars().all()
            assert len(rows) == 1
            assert rows[0].audit_id == entry.audit_id
            assert rows[0].severity == "ok"
            assert rows[0].drift_score == 0.0
        finally:
            session.close()
        events = [e for e in logs if e["event"] == "style_audit_persisted"]
        assert len(events) == 1
        assert events[0]["workspace_id"] == workspace_id
        assert events[0]["audit_id"] == entry.audit_id

    def test_session_factory_none_is_noop(self) -> None:
        # 不抛、不写、不发 event(只发 frontend_error 类副作用的反相)。
        persist_style_audit_safely(
            None, workspace_id="ws", entry=_make_entry()
        )

    def test_open_session_failure_swallowed(self) -> None:
        def _factory(_: str) -> Session:
            raise RuntimeError("boom-open")

        with capture_logs() as logs:
            persist_style_audit_safely(
                _factory, workspace_id="ws", entry=_make_entry()
            )
        events = [
            e
            for e in logs
            if e["event"] == "style_audit_persist_failed"
            and e["stage"] == "open_session"
        ]
        assert len(events) == 1
        assert events[0]["error_type"] == "RuntimeError"

    def test_multiple_calls_persist_multiple_rows(
        self, workspace_service: WorkspaceService, workspace_id: str
    ) -> None:
        # audit_id 唯一,**不去重**(history 表是轨迹)。
        factory = _make_session_factory(workspace_service)
        e1 = _make_entry(audit_id="sty-aaaaaaaaaaaa")
        e2 = _make_entry(audit_id="sty-bbbbbbbbbbbb")
        persist_style_audit_safely(factory, workspace_id=workspace_id, entry=e1)
        persist_style_audit_safely(factory, workspace_id=workspace_id, entry=e2)
        session = factory(workspace_id)
        try:
            rows = session.execute(select(StyleAuditEntryORM)).scalars().all()
            assert {r.audit_id for r in rows} == {
                "sty-aaaaaaaaaaaa",
                "sty-bbbbbbbbbbbb",
            }
        finally:
            session.close()

    def test_commit_failure_swallowed_and_rollback(
        self, workspace_service: WorkspaceService, workspace_id: str, monkeypatch
    ) -> None:
        # 用 monkeypatch 让 Session.commit 抛错;rollback 应被调,行不入表,
        # structlog 落 stage=commit 失败事件。
        factory = _make_session_factory(workspace_service)

        original_commit = Session.commit
        rollback_called = {"count": 0}
        original_rollback = Session.rollback

        def _boom_commit(self: Session) -> None:
            raise RuntimeError("boom-commit")

        def _track_rollback(self: Session) -> None:
            rollback_called["count"] += 1
            original_rollback(self)

        monkeypatch.setattr(Session, "commit", _boom_commit)
        monkeypatch.setattr(Session, "rollback", _track_rollback)
        try:
            with capture_logs() as logs:
                persist_style_audit_safely(
                    factory,
                    workspace_id=workspace_id,
                    entry=_make_entry(),
                )
        finally:
            monkeypatch.setattr(Session, "commit", original_commit)
            monkeypatch.setattr(Session, "rollback", original_rollback)
        events = [
            e
            for e in logs
            if e["event"] == "style_audit_persist_failed"
            and e["stage"] == "commit"
        ]
        assert len(events) == 1
        assert events[0]["error_type"] == "RuntimeError"
        assert rollback_called["count"] >= 1
        # 行不应入表。
        verify_session = factory(workspace_id)
        try:
            rows = (
                verify_session.execute(select(StyleAuditEntryORM)).scalars().all()
            )
            assert len(rows) == 0
        finally:
            verify_session.close()


# =============================================================================
# StyleAuditService.run_audit 集成
# =============================================================================


class TestStyleAuditServiceRunAudit:
    def test_no_chapter_range_no_template_short_circuits_ok(
        self, workspace_service: WorkspaceService, workspace_id: str
    ) -> None:
        # 工作区未选模板 → StyleAuditor 短路返 ok / drift=0(不调 LLM)。
        service = StyleAuditService(
            workspace_service=workspace_service,
            session_factory=_make_session_factory(workspace_service),
        )
        entry = service.run_audit(workspace_id)
        assert entry.severity == "ok"
        assert entry.drift_score == 0.0
        assert entry.chapter_range_start is None
        assert entry.chapter_range_end is None
        assert entry.audit_id.startswith("sty-")
        # 落表
        session = _make_session_factory(workspace_service)(workspace_id)
        try:
            rows = session.execute(select(StyleAuditEntryORM)).scalars().all()
            assert len(rows) == 1
            assert rows[0].severity == "ok"
        finally:
            session.close()

    def test_with_chapter_range_persists_with_fields(
        self, workspace_service: WorkspaceService, seeded_workspace: str
    ) -> None:
        service = StyleAuditService(
            workspace_service=workspace_service,
            session_factory=_make_session_factory(workspace_service),
        )
        entry = service.run_audit(
            seeded_workspace,
            chapter_range_start="ch-0001",
            chapter_range_end="ch-0002",
        )
        assert entry.chapter_range_start == "ch-0001"
        assert entry.chapter_range_end == "ch-0002"
        # 字段被落入表
        session = _make_session_factory(workspace_service)(seeded_workspace)
        try:
            row = (
                session.execute(select(StyleAuditEntryORM)).scalars().first()
            )
            assert row is not None
            assert row.chapter_range_start == "ch-0001"
            assert row.chapter_range_end == "ch-0002"
        finally:
            session.close()

    def test_missing_chapter_range_start_raises(
        self, workspace_service: WorkspaceService, seeded_workspace: str
    ) -> None:
        service = StyleAuditService(
            workspace_service=workspace_service,
            session_factory=_make_session_factory(workspace_service),
        )
        with pytest.raises(StyleAuditServiceError) as exc_info:
            service.run_audit(
                seeded_workspace,
                chapter_range_start="ch-9999",
                chapter_range_end="ch-0002",
            )
        assert "ch-9999" in str(exc_info.value)
        # 不应有任何行落表(校验失败先于持久化)。
        session = _make_session_factory(workspace_service)(seeded_workspace)
        try:
            rows = session.execute(select(StyleAuditEntryORM)).scalars().all()
            assert len(rows) == 0
        finally:
            session.close()

    def test_missing_chapter_range_end_raises(
        self, workspace_service: WorkspaceService, seeded_workspace: str
    ) -> None:
        service = StyleAuditService(
            workspace_service=workspace_service,
            session_factory=_make_session_factory(workspace_service),
        )
        with pytest.raises(StyleAuditServiceError) as exc_info:
            service.run_audit(
                seeded_workspace,
                chapter_range_start="ch-0001",
                chapter_range_end="ch-9999",
            )
        assert "ch-9999" in str(exc_info.value)

    def test_workspace_not_exist_raises(
        self, workspace_service: WorkspaceService
    ) -> None:
        # WorkspaceService.info 在 workspace 不存在时抛 MetaFileError(或子类);
        # 本 service 不捕(由路由层全局翻译器兜底)。
        service = StyleAuditService(
            workspace_service=workspace_service,
            session_factory=_make_session_factory(workspace_service),
        )
        with pytest.raises(Exception):  # noqa: BLE001 - 任一 WorkspaceServiceError / MetaFileError
            service.run_audit("ws-not-exist")

    def test_with_template_and_content_calls_llm(
        self, workspace_service: WorkspaceService
    ) -> None:
        # 选了模板 + 有版本正文 + 注入 router → StyleAuditor 调 LLM(mock 返低
        # drift 2.0 → severity=ok)+ dimension_notes 落 report_json。
        workspace_service.create(
            "ws-tpl",
            display_name="带模板工作区",
            genre="test",
            premise="x",
            activate=False,
            style_template="comedic_burst",
        )
        # seed chapter 行 + 写 v1 正文文件。
        factory = _make_session_factory(workspace_service)
        session = factory("ws-tpl")
        try:
            session.add(
                ChapterORM(
                    chapter_id="ch-0001",
                    workspace_id="ws-tpl",
                    current_version=1,
                    status="approved",
                    created_at=datetime.now(UTC),
                )
            )
            session.commit()
        finally:
            session.close()
        paths = WorkspacePaths(
            "ws-tpl", workspaces_root=workspace_service.workspaces_root
        )
        ChapterVersioning(paths).write_version(
            "ch-0001",
            "“哈哈哈,你也有今天!”少年大笑着,一脚踹翻了反派。",
            creation_type="draft",
            model_used="x",
        )

        service = StyleAuditService(
            workspace_service=workspace_service,
            session_factory=factory,
            router_factory=_fake_router_factory(MockLLMClient()),
        )
        entry = service.run_audit("ws-tpl")
        assert entry.severity == "ok"  # mock drift 2.0 < 2.5 → ok
        assert entry.drift_score == 2.0
        assert entry.report_json["model_used"] == "mock-mock-style-auditor"
        assert entry.report_json["dimension_notes"]  # 非空(mock 产 3 条)
