"""P4-B-02:chapter_versions 表 wiring 集成 + helper 单元测试。

覆盖契约:

1. ``persist_chapter_version_safely`` helper(单元层):
   - 给定 session_factory 时落 1 行(字段全字段 round-trip 正确)
   - ``session_factory=None`` 时静默 no-op
   - 复合主键 ``(chapter_id, version_id)`` 的同内容重放 → 静默幂等
   - 同主键但内容不同 → fail closed，不把 crash 残留冒充成功
   - session_factory 抛 → fail-closed + structlog ``chapter_version_persist_failed``
   - commit 抛(FK 违反等)→ rollback + fail-closed

2. ``ChapterService.write`` / ``rewrite`` / ``revise`` 路径(集成层):
   - mock verdict=pass + session_factory + vector_store 都注入 → 1 行(version_id=1)
   - session_factory **only** + vector_store=None → 仍落 1 行(P4-B-02 解耦 wiring 与 DB persist gate)
   - revise 后再 +1 行(version_id=2,creation_type=revise)
   - session_factory=None → 0 行(P1 兼容)

"""

from __future__ import annotations

from datetime import datetime, timezone
from pathlib import Path
from unittest.mock import MagicMock

import pytest
from sqlalchemy import create_engine
from sqlalchemy.orm import Session
from structlog.testing import capture_logs

from src.core.agents._prompt import detect_agent_role
from src.core.db import Base
from src.core.db.models import (
    ChapterORM,
    ChapterVersionORM,
    PipelineRunORM,
    ReviewResultORM,
    WorkspaceORM,
)
from src.core.llm.mock_client import MockLLMClient
from src.core.memory.vector import VectorStore
from src.core.models.chapter import ChapterVersion
from src.core.models.chapter_card import ChapterCard, ContinuityContext
from src.core.models.review import ReviewIssue, ReviewResult
from src.core.orchestrator.chapter_orchestrator import (
    ChapterVersionPersistenceError,
    RewriteCheckpointRecoveryError,
    persist_chapter_version_safely,
)
from src.core.rules.prose_revision_contract import ProseRevisionNoProgressError
from src.core.workspace.workspace_paths import WorkspacePaths
from src.services.chapter_service import ChapterService, ChapterServiceError
from src.services.workspace_service import WorkspaceService
from tests.revision_contract_helpers import single_target_localized_noop_response

pytestmark = pytest.mark.usefixtures("valid_character_admission")


# ---- 辅助 fixtures ---------------------------------------------------------


def _mock_embedding_fn(texts: list[str]) -> list[list[float]]:
    return [[0.1, 0.2, 0.3, 0.4] for _ in texts]


class _RoleRecordingMockClient(MockLLMClient):
    def __init__(self, *, fail_role: str | None = None) -> None:
        super().__init__()
        self.roles: list[str] = []
        self._fail_role = fail_role

    def complete(self, messages, **gen_params):
        role, _ = detect_agent_role(messages)
        self.roles.append(role)
        if role == self._fail_role:
            raise TimeoutError(f"{role} provider timeout")
        return super().complete(messages, **gen_params)


class _NoopLocalizedReviserClient(_RoleRecordingMockClient):
    """Writer/Reviewer 走正常 mock；Reviser 返回合法 identity 的原锚回显。"""

    def complete(self, messages, **gen_params):
        role, _ = detect_agent_role(messages)
        if role == "reviser":
            self.roles.append(role)
            return single_target_localized_noop_response(messages)
        return super().complete(messages, **gen_params)


def _make_continuity(**overrides) -> ContinuityContext:
    payload = dict(
        prev_chapter_tail="",
        unfinished_actions=[],
        time_gap="新场景",
        scene_continuation="switch",
        pov_change=False,
        pov_from=None,
        pov_to="林衍",
    )
    payload.update(overrides)
    return ContinuityContext(**payload)


def _make_card(chapter_id: str = "ch-0001", chapter_number: int = 1) -> ChapterCard:
    return ChapterCard(
        chapter_id=chapter_id,
        workspace_id="ws-versioning",
        volume_id="v01",
        chapter_number=chapter_number,
        title=f"第 {chapter_number} 章·测试",
        tension_level=6,
        # P4-B-06:validator 默认放宽,本文件测 chapter_versions 落库,validator 不参与。
        word_count_min=1,
        word_count_max=10**8,
        plot_node_budget=2,
        must_achieve=[],
        must_unresolved=[],
        forbidden=[],
        pov_character="林衍",
        setting="测试场景",
        continuity_context=_make_continuity(),
        next_chapter_seed="待续",
        arc_phase="rising",
        is_arc_finale=False,
        related_plot_threads=[],
        related_characters=["lin-yan"],
        review_threshold=7.5,
        revise_threshold=6.5,
        max_rewrites=1,
        max_revises=1,
    )


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
def prepared_workspace(workspace_service: WorkspaceService) -> str:
    workspace_service.create(
        "ws-versioning",
        display_name="P4-B-02 versioning DB 集成测试",
        genre="test",
        premise="验证 chapter_versions 落库",
        total_chapters=3,
    )
    return "ws-versioning"


def _make_session_factory(workspace_service: WorkspaceService):
    """按 workspace_id 构造 SQLite session;Base.metadata.create_all 自动建表。"""

    def _factory(workspace_id: str) -> Session:
        paths = WorkspacePaths(
            workspace_id, workspaces_root=workspace_service.workspaces_root
        )
        engine = create_engine(f"sqlite:///{paths.db_path}")
        Base.metadata.create_all(engine)
        return Session(engine)

    return _factory


def _make_vector_store_factory(workspace_service: WorkspaceService):
    def _factory(workspace_id: str) -> VectorStore:
        paths = WorkspacePaths(
            workspace_id, workspaces_root=workspace_service.workspaces_root
        )
        return VectorStore(
            workspace_id,
            embedding_fn=_mock_embedding_fn,
            persist_dir=paths.chroma_dir,
        )

    return _factory


def _seed_pipeline_run(
    session_factory,
    *,
    state: str,
    completed: list[str] | None = None,
    skipped: list[str] | None = None,
) -> None:
    now = datetime.now(timezone.utc)
    with session_factory("ws-versioning") as session:
        session.add(
            PipelineRunORM(
                run_id="run-r8-checkpoint",
                workspace_id="ws-versioning",
                state=state,
                scope="single",
                start_chapter_id="ch-0001",
                end_chapter_id="ch-0001",
                volume_id=None,
                profile_override=None,
                chapter_ids=["ch-0001"],
                current_chapter="ch-0001",
                completed_chapter_ids=completed or [],
                pause_reason="user_request" if state == "paused" else None,
                error_details=None,
                failure_policy="stop",
                skipped_chapter_ids=skipped or [],
                provider_call_cap=180,
                provider_call_count=0,
                transport_retry_count=0,
                pipeline_attempt_count=0,
                upstream_generation_attempt_count=0,
                created_at=now,
                updated_at=now,
                completed_at=None,
            )
        )
        session.commit()


# ---- helper 单元测试 -------------------------------------------------------


class TestPersistChapterVersionSafely:
    """``persist_chapter_version_safely`` helper 单元层契约。"""

    @pytest.fixture
    def in_memory_session(self):
        """带 chapters 行(满足 FK)的 in-memory SQLite session。"""
        engine = create_engine("sqlite:///:memory:")
        Base.metadata.create_all(engine)
        session = Session(engine)
        now = datetime.now(timezone.utc)
        session.add(
            WorkspaceORM(
                workspace_id="ws-helper",
                display_name="helper",
                genre="test",
                premise="x",
                created_at=now,
                last_active_at=now,
                engine_version="0.1.0",
                status="active",
                total_chapters=1,
                completed_chapters=0,
                config_overrides={},
            )
        )
        session.add(
            ChapterORM(
                chapter_id="ch-0001",
                workspace_id="ws-helper",
                current_version=1,
                status="drafting",
                rewrite_count=0,
                revise_count=0,
                created_at=now,
                approved_at=None,
                total_token_usage={},
                total_cost_usd=0.0,
            )
        )
        session.commit()
        yield session
        session.close()

    @pytest.fixture
    def session_factory(self, in_memory_session: Session):
        """0-arg session factory 复用同一 in-memory engine。"""
        engine = in_memory_session.get_bind()

        def _factory() -> Session:
            return Session(engine)

        return _factory

    @staticmethod
    def _make_version(
        version_id: int = 1, creation_type: str = "draft"
    ) -> ChapterVersion:
        return ChapterVersion(
            version_id=version_id,
            content=f"[v{version_id}] mock content",
            word_count=100,
            created_at=datetime.now(timezone.utc),
            creation_type=creation_type,  # type: ignore[arg-type]
            model_used="mock-model",
            review_score=None,
            notes="",
        )

    def test_persists_row_with_full_fields(
        self, in_memory_session: Session, session_factory
    ) -> None:
        version = self._make_version()
        persist_chapter_version_safely(
            session_factory, chapter_id="ch-0001", version=version
        )

        engine = in_memory_session.get_bind()
        with Session(engine) as verify:
            row = verify.get(ChapterVersionORM, ("ch-0001", 1))
            chapter = verify.get(ChapterORM, "ch-0001")
        assert row is not None
        assert chapter is not None
        assert row.chapter_id == "ch-0001"
        assert row.version_id == 1
        assert row.content == "[v1] mock content"
        assert row.word_count == 100
        assert row.creation_type == "draft"
        assert row.model_used == "mock-model"
        assert row.review_score is None
        assert row.notes == ""
        assert chapter.current_version == 1
        assert chapter.rewrite_count == 0
        assert chapter.revise_count == 0

    def test_idempotent_on_duplicate_pk(
        self, in_memory_session: Session, session_factory
    ) -> None:
        """同 (chapter_id, version_id) 二次调:静默跳过,不抛 IntegrityError。"""
        version = self._make_version()
        persist_chapter_version_safely(
            session_factory, chapter_id="ch-0001", version=version
        )
        persist_chapter_version_safely(
            session_factory, chapter_id="ch-0001", version=version
        )
        engine = in_memory_session.get_bind()
        with Session(engine) as verify:
            count = verify.query(ChapterVersionORM).count()
        assert count == 1

    def test_duplicate_pk_with_different_content_fails_closed(
        self, in_memory_session: Session, session_factory
    ) -> None:
        """同一 vN 的正文不同不是幂等重放，必须保留旧账并硬停。"""

        original = self._make_version()
        persist_chapter_version_safely(
            session_factory, chapter_id="ch-0001", version=original
        )
        conflicting = original.model_copy(
            update={"content": "same id, other body"}
        )

        with pytest.raises(
            ChapterVersionPersistenceError,
            match="fields=\\['content'\\]",
        ):
            persist_chapter_version_safely(
                session_factory,
                chapter_id="ch-0001",
                version=conflicting,
            )

        engine = in_memory_session.get_bind()
        with Session(engine) as verify:
            rows = verify.query(ChapterVersionORM).all()
        assert len(rows) == 1
        assert rows[0].content == original.content

    def test_duplicate_revise_version_and_inherited_review_commit_atomically(
        self, in_memory_session: Session, session_factory
    ) -> None:
        """Reviser no-op 仍落事实版本，并同事务补齐该版本审稿 checkpoint。"""

        version = self._make_version(version_id=2, creation_type="revise")
        inherited = ReviewResult(
            review_id="rv-inherited-v2",
            chapter_id="ch-0001",
            version_reviewed=2,
            reviewed_at=datetime.now(timezone.utc),
            reviewer_model="inherited:mock-reviewer",
            score_plot_progression=7.0,
            score_tension_match=7.0,
            score_prose_quality=7.0,
            score_consistency=7.0,
            score_hook_management=7.0,
            score_voice_consistency=7.0,
            score_overall=7.0,
            hard_constraints_passed=True,
            failed_constraints=[],
            issues=[],
            suggestions=["[继承审稿] 同文不重复调用 Reviewer"],
            verdict="revise",
            revise_targets=[],
        )

        persist_chapter_version_safely(
            session_factory,
            chapter_id="ch-0001",
            version=version,
            inherited_review_checkpoint=inherited,
        )

        engine = in_memory_session.get_bind()
        with Session(engine) as verify:
            version_row = verify.get(ChapterVersionORM, ("ch-0001", 2))
            review_row = verify.get(ReviewResultORM, "rv-inherited-v2")
            chapter = verify.get(ChapterORM, "ch-0001")
            assert version_row is not None
            assert review_row is not None
            assert chapter is not None
            assert review_row.version_reviewed == 2
            assert review_row.reviewer_model == "inherited:mock-reviewer"
            assert chapter.current_version == 2
            assert chapter.revise_count == 1

    def test_no_op_when_session_factory_is_none(self) -> None:
        """``session_factory=None`` → 静默返,不抛。"""
        version = self._make_version()
        persist_chapter_version_safely(None, chapter_id="ch-0001", version=version)

    def test_session_factory_exception_fails_closed(self) -> None:
        """账本无法打开时必须冒泡，不能赠送一份免费正文。"""

        def _broken_factory() -> Session:
            raise RuntimeError("[test] session 工厂故意挂")

        version = self._make_version()
        with capture_logs() as logs:
            with pytest.raises(ChapterVersionPersistenceError):
                persist_chapter_version_safely(
                    _broken_factory, chapter_id="ch-0001", version=version
                )
        events = [
            log for log in logs if log.get("event") == "chapter_version_persist_failed"
        ]
        assert len(events) == 1
        assert events[0]["stage"] == "open_session"
        assert events[0]["error_type"] == "RuntimeError"
        assert events[0]["chapter_id"] == "ch-0001"
        assert events[0]["version_id"] == 1

    def test_commit_exception_rolls_back_and_fails_closed(self) -> None:
        """commit 抛(FK 违反等)→ rollback + fail-closed。

        构造空 SQLite(无 chapters 行),写 chapter_versions FK → 违反。
        SQLite 默认不强制 FK,需 PRAGMA;为简化,用 mock session 模拟 commit 抛。
        """
        mock_session = MagicMock(spec=Session)
        chapter = MagicMock(spec=ChapterORM)
        mock_session.get.side_effect = [None, chapter]
        mock_session.scalar.return_value = 0
        mock_session.commit.side_effect = RuntimeError("[test] commit 故意失败")

        def _factory() -> Session:
            return mock_session

        version = self._make_version()
        with capture_logs() as logs:
            with pytest.raises(ChapterVersionPersistenceError):
                persist_chapter_version_safely(
                    _factory, chapter_id="ch-0001", version=version
                )
        events = [
            log for log in logs if log.get("event") == "chapter_version_persist_failed"
        ]
        assert len(events) == 1
        assert events[0]["stage"] == "commit"
        mock_session.rollback.assert_called_once()
        mock_session.close.assert_called_once()


# ---- ChapterService 集成测试 -----------------------------------------------


class TestChapterVersioningIntegration:
    """``ChapterService.write`` / ``rewrite`` / ``revise`` 路径落 chapter_versions 验证。"""

    def test_write_pass_persists_one_version_row(
        self,
        workspace_service: WorkspaceService,
        prepared_workspace: str,
    ) -> None:
        """完整 wiring(session_factory + vector_store)+ verdict=pass → 落 1 行。"""
        svc = ChapterService(
            workspace_service=workspace_service,
            client=MockLLMClient(),
            model="claude-sonnet-4-6",
            session_factory=_make_session_factory(workspace_service),
            vector_store_factory=_make_vector_store_factory(workspace_service),
        )
        svc.save_chapter_card(prepared_workspace, _make_card("ch-0001", 1))

        result = svc.write(prepared_workspace, "ch-0001")
        assert result.final_version_id == 1

        paths = WorkspacePaths(
            prepared_workspace, workspaces_root=workspace_service.workspaces_root
        )
        engine = create_engine(f"sqlite:///{paths.db_path}")
        with Session(engine) as session:
            rows = (
                session.query(ChapterVersionORM)
                .filter(ChapterVersionORM.chapter_id == "ch-0001")
                .all()
            )
        assert len(rows) == 1
        assert rows[0].version_id == 1
        assert rows[0].creation_type == "draft"
        assert rows[0].word_count > 0
        assert rows[0].content  # 非空

    def test_session_factory_only_no_vector_store_still_persists(
        self,
        workspace_service: WorkspaceService,
        prepared_workspace: str,
    ) -> None:
        """P4-B-02 解耦 wiring 与 DB persist gate:只给 session_factory(无 vector_store)
        仍会落 chapter_versions 行(对齐 ``_upsert_chapter_state`` 现有模式)。"""
        svc = ChapterService(
            workspace_service=workspace_service,
            client=MockLLMClient(),
            model="claude-sonnet-4-6",
            session_factory=_make_session_factory(workspace_service),
            # vector_store_factory 故意不传
        )
        svc.save_chapter_card(prepared_workspace, _make_card("ch-0001", 1))
        svc.write(prepared_workspace, "ch-0001")

        paths = WorkspacePaths(
            prepared_workspace, workspaces_root=workspace_service.workspaces_root
        )
        engine = create_engine(f"sqlite:///{paths.db_path}")
        with Session(engine) as session:
            count = session.query(ChapterVersionORM).count()
        assert count == 1

    def test_no_session_factory_skips_persistence(
        self,
        workspace_service: WorkspaceService,
        prepared_workspace: str,
    ) -> None:
        """P1 兼容:session_factory=None → 不落库,文件版本仍正常落盘。"""
        svc = ChapterService(
            workspace_service=workspace_service,
            client=MockLLMClient(),
            model="claude-sonnet-4-6",
            # 不传 session_factory
        )
        svc.save_chapter_card(prepared_workspace, _make_card("ch-0001", 1))
        result = svc.write(prepared_workspace, "ch-0001")
        # 文件路径仍正常
        assert result.final_path is not None and result.final_path.is_file()

        # SQLite db 文件不应被建出来(P1 路径不动 DB)— 即使建了也应该 0 行
        paths = WorkspacePaths(
            prepared_workspace, workspaces_root=workspace_service.workspaces_root
        )
        if paths.db_path.exists():
            engine = create_engine(f"sqlite:///{paths.db_path}")
            with Session(engine) as session:
                # 表可能未建,直接查会抛;用 inspect
                from sqlalchemy import inspect

                if inspect(engine).has_table("chapter_versions"):
                    assert session.query(ChapterVersionORM).count() == 0

    def test_revise_persists_additional_version_row(
        self,
        workspace_service: WorkspaceService,
        prepared_workspace: str,
    ) -> None:
        """先 write 落 v1,再 revise 落 v2(creation_type='revise')。"""
        svc = ChapterService(
            workspace_service=workspace_service,
            client=MockLLMClient(),
            model="claude-sonnet-4-6",
            session_factory=_make_session_factory(workspace_service),
            vector_store_factory=_make_vector_store_factory(workspace_service),
        )
        svc.save_chapter_card(prepared_workspace, _make_card("ch-0001", 1))
        svc.write(prepared_workspace, "ch-0001")

        new_version = svc.revise(
            prepared_workspace,
            "ch-0001",
            issues=[
                ReviewIssue(
                    paragraph_anchor="Mock 写作输出 - chapter_id=ch-0001",
                    issue_type="pacing",
                    description="节奏问题",
                    suggestion="提速",
                )
            ],
        )
        assert new_version.version_id == 2
        assert new_version.creation_type == "revise"

        paths = WorkspacePaths(
            prepared_workspace, workspaces_root=workspace_service.workspaces_root
        )
        engine = create_engine(f"sqlite:///{paths.db_path}")
        with Session(engine) as session:
            rows = (
                session.query(ChapterVersionORM)
                .filter(ChapterVersionORM.chapter_id == "ch-0001")
                .order_by(ChapterVersionORM.version_id)
                .all()
            )
        assert len(rows) == 2
        assert rows[0].version_id == 1
        assert rows[0].creation_type == "draft"
        assert rows[1].version_id == 2
        assert rows[1].creation_type == "revise"

    def test_explicit_localized_noop_preserves_db_version_and_budget_projection(
        self,
        workspace_service: WorkspaceService,
        prepared_workspace: str,
    ) -> None:
        client = _NoopLocalizedReviserClient()
        session_factory = _make_session_factory(workspace_service)
        svc = ChapterService(
            workspace_service=workspace_service,
            client=client,
            model="mock",
            session_factory=session_factory,
        )
        svc.save_chapter_card(prepared_workspace, _make_card("ch-0001", 1))
        svc.write(prepared_workspace, "ch-0001")
        paths = WorkspacePaths(
            prepared_workspace,
            workspaces_root=workspace_service.workspaces_root,
        )
        with session_factory(prepared_workspace) as session:
            chapter = session.get(ChapterORM, "ch-0001")
            assert chapter is not None
            before = (
                chapter.current_version,
                chapter.rewrite_count,
                chapter.revise_count,
                session.query(ChapterVersionORM).count(),
            )
        client.roles.clear()

        with pytest.raises(ProseRevisionNoProgressError, match="实质变化"):
            svc.revise(
                prepared_workspace,
                "ch-0001",
                issues=[
                    ReviewIssue(
                        paragraph_anchor="Mock 写作输出 - chapter_id=ch-0001",
                        issue_type="pacing",
                        description="节奏问题",
                        suggestion="提速",
                    )
                ],
            )

        assert client.roles == ["reviser"]
        assert not (paths.chapter_dir("ch-0001") / "v2.md").exists()
        with session_factory(prepared_workspace) as session:
            chapter = session.get(ChapterORM, "ch-0001")
            assert chapter is not None
            assert (
                chapter.current_version,
                chapter.rewrite_count,
                chapter.revise_count,
                session.query(ChapterVersionORM).count(),
            ) == before

    def test_revise_rejects_noncanonical_disk_alias_before_provider_or_write(
        self,
        workspace_service: WorkspaceService,
        prepared_workspace: str,
    ) -> None:
        """DB v1 与磁盘 v01 别名并存时，Reviser 必须零调用并保留现场。"""
        client = _RoleRecordingMockClient()
        session_factory = _make_session_factory(workspace_service)
        svc = ChapterService(
            workspace_service=workspace_service,
            client=client,
            model="claude-sonnet-4-6",
            session_factory=session_factory,
            vector_store_factory=_make_vector_store_factory(workspace_service),
        )
        svc.save_chapter_card(prepared_workspace, _make_card("ch-0001", 1))
        svc.write(prepared_workspace, "ch-0001")

        paths = WorkspacePaths(
            prepared_workspace, workspaces_root=workspace_service.workspaces_root
        )
        chapter_dir = paths.chapter_dir("ch-0001")
        (chapter_dir / "v01.md").write_text("不得折叠的 v1 别名", encoding="utf-8")
        files_before = {
            path.name: path.read_bytes()
            for path in chapter_dir.iterdir()
            if path.is_file()
        }
        with session_factory(prepared_workspace) as session:
            chapter = session.get(ChapterORM, "ch-0001")
            assert chapter is not None
            db_before = (
                chapter.status,
                chapter.current_version,
                chapter.rewrite_count,
                chapter.revise_count,
                tuple(
                    (row.version_id, row.content)
                    for row in session.query(ChapterVersionORM)
                    .filter(ChapterVersionORM.chapter_id == "ch-0001")
                    .order_by(ChapterVersionORM.version_id)
                ),
            )
        client.roles.clear()

        with pytest.raises(RewriteCheckpointRecoveryError, match="非规范版本文件名"):
            svc.revise(
                prepared_workspace,
                "ch-0001",
                issues=[
                    ReviewIssue(
                        paragraph_anchor="测试锚点",
                        issue_type="pacing",
                        description="节奏问题",
                        suggestion="提速",
                    )
                ],
            )

        assert client.roles == []
        assert {
            path.name: path.read_bytes()
            for path in chapter_dir.iterdir()
            if path.is_file()
        } == files_before
        with session_factory(prepared_workspace) as session:
            chapter = session.get(ChapterORM, "ch-0001")
            assert chapter is not None
            assert (
                chapter.status,
                chapter.current_version,
                chapter.rewrite_count,
                chapter.revise_count,
                tuple(
                    (row.version_id, row.content)
                    for row in session.query(ChapterVersionORM)
                    .filter(ChapterVersionORM.chapter_id == "ch-0001")
                    .order_by(ChapterVersionORM.version_id)
                ),
            ) == db_before


    def test_writer_timeout_before_version_persisted_retries_as_first_draft(
        self,
        workspace_service: WorkspaceService,
        prepared_workspace: str,
    ) -> None:
        """正文未持久化前故障不消费 content budget，重启后仍从 v1 draft 起。"""
        session_factory = _make_session_factory(workspace_service)
        failing = ChapterService(
            workspace_service=workspace_service,
            client=_RoleRecordingMockClient(fail_role="writer"),
            model="mock",
            session_factory=session_factory,
        )
        failing.save_chapter_card(
            prepared_workspace,
            _make_card("ch-0001", 1),
        )
        with pytest.raises(TimeoutError, match="writer provider timeout"):
            failing.write(prepared_workspace, "ch-0001")

        restarted = ChapterService(
            workspace_service=workspace_service,
            client=MockLLMClient(),
            model="mock",
            session_factory=session_factory,
        )
        result = restarted.write(prepared_workspace, "ch-0001")
        assert result.final_version_id == 1
        assert result.rewrite_count == 0

        paths = WorkspacePaths(
            prepared_workspace,
            workspaces_root=workspace_service.workspaces_root,
        )
        engine = create_engine(f"sqlite:///{paths.db_path}")
        with Session(engine) as session:
            chapter = session.get(ChapterORM, "ch-0001")
            versions = list(
                session.query(ChapterVersionORM)
                .filter(ChapterVersionORM.chapter_id == "ch-0001")
                .all()
            )
        assert chapter is not None
        assert (chapter.current_version, chapter.rewrite_count) == (1, 0)
        assert [(row.version_id, row.creation_type) for row in versions] == [
            (1, "draft")
        ]

    def test_reviewer_timeout_after_version_persisted_restarts_from_review(
        self,
        workspace_service: WorkspaceService,
        prepared_workspace: str,
    ) -> None:
        """正文已持久化后故障，重启必须重审 v1，不能再生成免费 draft。"""
        session_factory = _make_session_factory(workspace_service)
        first_client = _RoleRecordingMockClient(fail_role="reviewer")
        first = ChapterService(
            workspace_service=workspace_service,
            client=first_client,
            model="mock",
            session_factory=session_factory,
        )
        first.save_chapter_card(
            prepared_workspace,
            _make_card("ch-0001", 1),
        )
        with pytest.raises(TimeoutError, match="reviewer provider timeout"):
            first.write(prepared_workspace, "ch-0001")
        assert first_client.roles[:2] == ["writer", "reviewer"]

        restarted_client = _RoleRecordingMockClient()
        restarted = ChapterService(
            workspace_service=workspace_service,
            client=restarted_client,
            model="mock",
            session_factory=session_factory,
        )
        result = restarted.write(prepared_workspace, "ch-0001")
        assert result.final_version_id == 1
        assert "writer" not in restarted_client.roles

        paths = WorkspacePaths(
            prepared_workspace,
            workspaces_root=workspace_service.workspaces_root,
        )
        engine = create_engine(f"sqlite:///{paths.db_path}")
        with Session(engine) as session:
            chapter = session.get(ChapterORM, "ch-0001")
            versions = list(
                session.query(ChapterVersionORM)
                .filter(ChapterVersionORM.chapter_id == "ch-0001")
                .all()
            )
        assert chapter is not None
        assert (chapter.current_version, chapter.rewrite_count) == (1, 0)
        assert [(row.version_id, row.creation_type) for row in versions] == [
            (1, "draft")
        ]

    def test_explicit_rewrite_ignores_caller_count_and_matches_projection(
        self,
        workspace_service: WorkspaceService,
        prepared_workspace: str,
    ) -> None:
        """caller 伪造 prior=999 也只能消费版本账本中的下一次 rewrite。"""
        session_factory = _make_session_factory(workspace_service)
        service = ChapterService(
            workspace_service=workspace_service,
            client=MockLLMClient(),
            model="mock",
            session_factory=session_factory,
        )
        service.save_chapter_card(
            prepared_workspace,
            _make_card("ch-0001", 1),
        )
        service.write(prepared_workspace, "ch-0001")
        result = service.rewrite(
            prepared_workspace,
            "ch-0001",
            prior_rewrite_count=999,
        )
        assert result.rewrite_count == 1

        paths = WorkspacePaths(
            prepared_workspace,
            workspaces_root=workspace_service.workspaces_root,
        )
        engine = create_engine(f"sqlite:///{paths.db_path}")
        with Session(engine) as session:
            chapter = session.get(ChapterORM, "ch-0001")
            creation_types = [
                row.creation_type
                for row in (
                    session.query(ChapterVersionORM)
                    .filter(ChapterVersionORM.chapter_id == "ch-0001")
                    .order_by(ChapterVersionORM.version_id)
                    .all()
                )
            ]
        assert chapter is not None
        assert (
            chapter.current_version,
            chapter.rewrite_count,
            chapter.revise_count,
        ) == (2, 1, 0)
        assert creation_types == ["draft", "rewrite"]


class TestManualRewriteAttemptScopeR8:
    """暂停 run 的 Chapter UI 修复必须留在原活动 scope。"""

    def test_paused_current_checkpoint_reuses_run_cap_and_task(
        self,
        workspace_service: WorkspaceService,
        prepared_workspace: str,
    ) -> None:
        session_factory = _make_session_factory(workspace_service)
        _seed_pipeline_run(session_factory, state="paused")
        service = ChapterService(
            workspace_service=workspace_service,
            client=_RoleRecordingMockClient(),
            model="mock",
            session_factory=session_factory,
        )

        scope = service._resolve_manual_attempt_scope(  # noqa: SLF001
            prepared_workspace,
            "ch-0001",
            task_id="task-ui-rewrite",
        )

        assert scope == {
            "workspace_id": prepared_workspace,
            "run_id": "run-r8-checkpoint",
            "task_id": "task-ui-rewrite",
            "chapter_id": "ch-0001",
            "provider_call_cap": 180,
        }

    def test_running_run_rejects_before_any_client_call(
        self,
        workspace_service: WorkspaceService,
        prepared_workspace: str,
    ) -> None:
        session_factory = _make_session_factory(workspace_service)
        _seed_pipeline_run(session_factory, state="running")
        client = _RoleRecordingMockClient()
        service = ChapterService(
            workspace_service=workspace_service,
            client=client,
            model="mock",
            session_factory=session_factory,
        )
        service.save_chapter_card(
            prepared_workspace,
            _make_card("ch-0001", 1),
        )

        with pytest.raises(ChapterServiceError, match="pipeline 正在运行"):
            service.rewrite(
                prepared_workspace,
                "ch-0001",
                attempt_task_id="task-ui-rewrite",
            )

        assert client.roles == []

    @pytest.mark.parametrize(
        ("completed", "skipped"),
        [(["ch-0001"], []), ([], ["ch-0001"])],
    )
    def test_completed_or_skipped_checkpoint_is_rejected(
        self,
        workspace_service: WorkspaceService,
        prepared_workspace: str,
        completed: list[str],
        skipped: list[str],
    ) -> None:
        session_factory = _make_session_factory(workspace_service)
        _seed_pipeline_run(
            session_factory,
            state="paused",
            completed=completed,
            skipped=skipped,
        )
        service = ChapterService(
            workspace_service=workspace_service,
            client=_RoleRecordingMockClient(),
            model="mock",
            session_factory=session_factory,
        )

        with pytest.raises(ChapterServiceError, match="完成/跳过账本"):
            service._resolve_manual_attempt_scope(  # noqa: SLF001
                prepared_workspace,
                "ch-0001",
                task_id="task-ui-rewrite",
            )
