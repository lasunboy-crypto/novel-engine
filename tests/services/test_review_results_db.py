"""P4-B-02:review_results + review_issues 表 wiring 集成 + helper 单元测试。

覆盖契约:

1. ``persist_review_result_safely`` helper(单元层):
   - 给定 session_factory + ReviewResult 含 issues/revise_targets 时:
     落 1 review_results 行 + N issues + M revise_targets,role 字段区分
   - ``session_factory=None`` 时静默 no-op
   - ``review_id`` 主键已存在 → 静默幂等(防御重跑)
   - session_factory 抛 → swallow + structlog ``review_result_persist_failed``
   - commit 抛 → swallow + structlog log + rollback 调用

2. ``ChapterService.write`` 路径(集成层):
   - mock verdict=pass + wiring 启用 → review_results 1 行 + 0 issues
   - mock verdict=revise + max_revises=1 → 至少 2 review_results 行 + N issues
     (revise verdict + revise budget 耗尽 → escalate;两次审稿都落库)
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

from src.core.db import Base
from src.core.db.models import (
    ChapterORM,
    ReviewIssueORM,
    ReviewResultORM,
    WorkspaceORM,
)
from src.core.llm.mock_client import MockLLMClient
from src.core.memory.vector import VectorStore
from src.core.models.chapter_card import ChapterCard, ContinuityContext
from src.core.models.review import (
    REVIEWER_ISSUES_MAX_ITEMS,
    REVIEWER_SUGGESTIONS_MAX_ITEMS,
    ConstraintResult,
    ConstraintViolation,
    ReviewIssue,
    ReviewResult,
    stable_constraint_id,
)
from src.core.orchestrator.chapter_orchestrator import (
    ReviewCheckpointPersistenceError,
    persist_review_result_safely,
)
from src.core.workspace.workspace_paths import WorkspacePaths
from src.services.chapter_service import ChapterService
from src.services.workspace_service import WorkspaceService

pytestmark = pytest.mark.usefixtures("valid_character_admission")


# ---- 辅助 fixtures ---------------------------------------------------------


def _mock_embedding_fn(texts: list[str]) -> list[list[float]]:
    return [[0.1, 0.2, 0.3, 0.4] for _ in texts]


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
        workspace_id="ws-reviews",
        volume_id="v01",
        chapter_number=chapter_number,
        title=f"第 {chapter_number} 章·测试",
        tension_level=6,
        # P4-B-06:validator 默认放宽,本文件测 review_results 落库,validator 不参与。
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
        "ws-reviews",
        display_name="P4-B-02 review_results DB 集成测试",
        genre="test",
        premise="验证 review_results 落库",
        total_chapters=3,
    )
    return "ws-reviews"


def _make_session_factory(workspace_service: WorkspaceService):
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


def _make_review(
    review_id: str = "rv-0001",
    chapter_id: str = "ch-0001",
    issues: list[ReviewIssue] | None = None,
    revise_targets: list[ReviewIssue] | None = None,
    verdict: str = "pass",
) -> ReviewResult:
    return ReviewResult(
        review_id=review_id,
        chapter_id=chapter_id,
        version_reviewed=1,
        reviewed_at=datetime.now(timezone.utc),
        reviewer_model="mock-model",
        score_plot_progression=8.0,
        score_tension_match=8.0,
        score_prose_quality=8.0,
        score_consistency=8.0,
        score_hook_management=8.0,
        score_voice_consistency=8.0,
        score_overall=8.0,
        hard_constraints_passed=True,
        failed_constraints=[],
        issues=issues or [],
        suggestions=["[mock] 兜底建议"],
        verdict=verdict,  # type: ignore[arg-type]
        revise_targets=revise_targets or [],
    )


# ---- helper 单元测试 -------------------------------------------------------


class TestPersistReviewResultSafely:
    """``persist_review_result_safely`` helper 单元层契约。"""

    @pytest.fixture
    def in_memory_session(self):
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
        engine = in_memory_session.get_bind()

        def _factory() -> Session:
            return Session(engine)

        return _factory

    def test_persists_main_row_with_issues_and_revise_targets(
        self, in_memory_session: Session, session_factory
    ) -> None:
        """落主行 + N issues(role='issue') + M revise_targets(role='revise_target'),
        position 字段保 list 顺序(0-based)。"""
        issues = [
            ReviewIssue(
                paragraph_anchor="开篇段落锚点",
                issue_type="pacing",
                description="节奏偏慢",
                suggestion="压缩前两段",
            ),
            ReviewIssue(
                paragraph_anchor="结尾段落锚点",
                issue_type="hook",
                description="钩子弱",
                suggestion="加悬念",
            ),
        ]
        revise_targets = [
            ReviewIssue(
                paragraph_anchor="目标段落锚点",
                issue_type="voice",
                description="对白脱戏",
                suggestion="改语气",
            ),
        ]
        review = _make_review(
            issues=issues, revise_targets=revise_targets, verdict="revise"
        )

        persist_review_result_safely(session_factory, review=review)

        engine = in_memory_session.get_bind()
        with Session(engine) as verify:
            main = verify.get(ReviewResultORM, "rv-0001")
            assert main is not None
            assert main.chapter_id == "ch-0001"
            assert main.verdict == "revise"
            assert main.score_overall == 8.0
            assert main.hard_constraints_passed is True

            issue_rows = (
                verify.query(ReviewIssueORM)
                .filter(ReviewIssueORM.review_id == "rv-0001")
                .order_by(ReviewIssueORM.id)
                .all()
            )
            issue_role_rows = [r for r in issue_rows if r.role == "issue"]
            target_role_rows = [r for r in issue_rows if r.role == "revise_target"]
            assert len(issue_role_rows) == 2
            assert len(target_role_rows) == 1
            # position 0-based,顺序保留
            assert [r.position for r in issue_role_rows] == [0, 1]
            assert issue_role_rows[0].paragraph_anchor == "开篇段落锚点"
            assert issue_role_rows[1].paragraph_anchor == "结尾段落锚点"
            assert target_role_rows[0].position == 0
            assert target_role_rows[0].paragraph_anchor == "目标段落锚点"

    def test_f5_max_reviewer_payload_has_bounded_db_row_fanout(
        self, in_memory_session: Session, session_factory
    ) -> None:
        """最多 3 个 LLM issues，revise 投影后最多写 3+3 个明细行。"""
        issues = [
            ReviewIssue(
                paragraph_anchor=f"边界锚点 {index}",
                issue_type="pacing",
                description="具体问题",
                suggestion="局部修改",
            )
            for index in range(REVIEWER_ISSUES_MAX_ITEMS)
        ]
        review = _make_review(
            review_id="rv-f5-boundary",
            issues=issues,
            revise_targets=list(issues),
            verdict="revise",
        ).model_copy(
            update={
                "suggestions": [
                    f"边界建议 {index}"
                    for index in range(REVIEWER_SUGGESTIONS_MAX_ITEMS)
                ]
            }
        )

        persist_review_result_safely(session_factory, review=review)

        engine = in_memory_session.get_bind()
        with Session(engine) as verify:
            main = verify.get(ReviewResultORM, "rv-f5-boundary")
            assert main is not None
            assert len(main.suggestions) == REVIEWER_SUGGESTIONS_MAX_ITEMS
            rows = (
                verify.query(ReviewIssueORM)
                .filter(ReviewIssueORM.review_id == "rv-f5-boundary")
                .all()
            )
            assert len([row for row in rows if row.role == "issue"]) == (
                REVIEWER_ISSUES_MAX_ITEMS
            )
            assert len([row for row in rows if row.role == "revise_target"]) == (
                REVIEWER_ISSUES_MAX_ITEMS
            )
            assert len(rows) == REVIEWER_ISSUES_MAX_ITEMS * 2

    def test_persists_excellence_metrics_round_trip(
        self, in_memory_session: Session, session_factory
    ) -> None:
        # v1.1 wave 7 / B 持久化:4 个卓越性纯度量(画面感 / 内心戏 / 套路化 /
        # 沉浸感)落 review_results 新列(Alembic 0021)+ round-trip。
        review = _make_review().model_copy(
            update={
                "score_imagery": 8.5,
                "score_interiority": 7.0,
                "score_formulaic": 2.5,
                "score_immersion": 9.0,
            }
        )
        persist_review_result_safely(session_factory, review=review)
        engine = in_memory_session.get_bind()
        with Session(engine) as verify:
            row = verify.get(ReviewResultORM, "rv-0001")
            assert row is not None
            assert row.score_imagery == pytest.approx(8.5)
            assert row.score_interiority == pytest.approx(7.0)
            assert row.score_formulaic == pytest.approx(2.5)
            assert row.score_immersion == pytest.approx(9.0)
            # 不变量:4 列与 verdict 正交,verdict 仍是 reviewer 算定的值不受影响
            assert row.verdict == "pass"

    def test_persists_constraint_violations_round_trip(
        self, in_memory_session: Session, session_factory
    ) -> None:
        violation = ConstraintViolation(
            constraint_id="e" * 64,
            domain="world.unique_rules",
            source_path="data/outline/world.json",
            source_hash="f" * 64,
            rule_quote="记忆抹除不可逆。",
            evidence_quote="被抹除的记忆全部恢复。",
            severity="hard",
            ambiguity=None,
        )
        review = _make_review().model_copy(
            update={"constraint_violations": [violation]}
        )
        persist_review_result_safely(session_factory, review=review)

        engine = in_memory_session.get_bind()
        with Session(engine) as verify:
            row = verify.get(ReviewResultORM, "rv-0001")
            assert row is not None
            assert len(row.constraint_violations) == 1
            restored = ConstraintViolation.model_validate(row.constraint_violations[0])
            assert restored == violation

    def test_persists_constraint_results_and_regressions_round_trip(
        self, in_memory_session: Session, session_factory
    ) -> None:
        source_path = "chapter_cards/ch-0001.forbidden"
        rule_text = "禁止提前揭示最终真相"
        result = ConstraintResult(
            constraint_id=stable_constraint_id(
                source_path=source_path,
                domain="forbidden",
                rule_text=rule_text,
            ),
            domain="forbidden",
            source_path=source_path,
            rule_text=rule_text,
            status="fail",
            details="正文已经揭示最终真相",
        )
        review = _make_review().model_copy(
            update={
                "constraint_results": [result],
                "hard_constraint_regressions": [result.constraint_id],
            }
        )
        persist_review_result_safely(session_factory, review=review)

        engine = in_memory_session.get_bind()
        with Session(engine) as verify:
            row = verify.get(ReviewResultORM, "rv-0001")
            assert row is not None
            assert ConstraintResult.model_validate(row.constraint_results[0]) == result
            assert row.hard_constraint_regressions == [result.constraint_id]

    def test_excellence_metrics_null_when_unset(
        self, in_memory_session: Session, session_factory
    ) -> None:
        # LLM 漏产 / 旧审稿(0021 前)→ ReviewResult 4 度量默认 None → 列存 NULL
        # (向后兼容,前端 ExcellenceMetrics 全 None → 优雅不渲染)。
        persist_review_result_safely(session_factory, review=_make_review())
        engine = in_memory_session.get_bind()
        with Session(engine) as verify:
            row = verify.get(ReviewResultORM, "rv-0001")
            assert row is not None
            assert row.score_imagery is None
            assert row.score_interiority is None
            assert row.score_formulaic is None
            assert row.score_immersion is None

    def test_idempotent_on_duplicate_review_id(
        self, in_memory_session: Session, session_factory
    ) -> None:
        review = _make_review()
        persist_review_result_safely(session_factory, review=review)
        persist_review_result_safely(session_factory, review=review)
        engine = in_memory_session.get_bind()
        with Session(engine) as verify:
            count = verify.query(ReviewResultORM).count()
        assert count == 1

    def test_no_op_when_session_factory_is_none(self) -> None:
        review = _make_review()
        persist_review_result_safely(None, review=review)

    def test_session_factory_exception_is_logged_and_raised(self) -> None:
        def _broken() -> Session:
            raise RuntimeError("[test] 故意挂")

        review = _make_review()
        with capture_logs() as logs:
            with pytest.raises(ReviewCheckpointPersistenceError):
                persist_review_result_safely(_broken, review=review)
        events = [
            log for log in logs if log.get("event") == "review_result_persist_failed"
        ]
        assert len(events) == 1
        assert events[0]["stage"] == "open_session"
        assert events[0]["error_type"] == "RuntimeError"
        assert events[0]["review_id"] == "rv-0001"
        assert events[0]["chapter_id"] == "ch-0001"

    def test_commit_exception_rolls_back_and_is_raised(self) -> None:
        mock_session = MagicMock(spec=Session)
        mock_session.get.return_value = None
        mock_session.commit.side_effect = RuntimeError("[test] commit 故意失败")

        def _factory() -> Session:
            return mock_session

        review = _make_review()
        with capture_logs() as logs:
            with pytest.raises(ReviewCheckpointPersistenceError):
                persist_review_result_safely(_factory, review=review)
        events = [
            log for log in logs if log.get("event") == "review_result_persist_failed"
        ]
        assert len(events) == 1
        assert events[0]["stage"] == "commit"
        mock_session.rollback.assert_called_once()
        mock_session.close.assert_called_once()


# ---- v1.0.x reviewer-resilience:_persist_parse_failure_safely 单元 ----------


class TestPersistParseFailureSafely:
    """v1.0.x reviewer-resilience(2026-05-20):reviewer 连续解析失败升人工时
    写占位 review_results 行的 helper。让前端"审稿历史"渲染失败提示而非
    "暂无审稿"让用户毫无线索。
    """

    @pytest.fixture
    def in_memory_session(self):
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
                status="reviewing",
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
        engine = in_memory_session.get_bind()

        def _factory() -> Session:
            return Session(engine)

        return _factory

    def test_persists_placeholder_row_when_called(
        self, in_memory_session: Session, session_factory
    ) -> None:
        """成功路径:写一条 verdict='parse_failed' + score=0 占位行。"""
        from src.core.orchestrator.chapter_orchestrator import (
            _persist_parse_failure_safely,
        )

        _persist_parse_failure_safely(
            session_factory,
            chapter_id="ch-0001",
            version_reviewed=1,
            raw_output='{"invalid": json,}',
            model_used="qwen-novel",
            attempts_log=[
                "第 1 次尝试失败:invalid_json line 1 col 30",
                "第 2 次尝试失败:schema_mismatch issues[1].suggestion missing",
            ],
        )

        engine = in_memory_session.get_bind()
        with Session(engine) as verify:
            rows = (
                verify.query(ReviewResultORM)
                .filter(ReviewResultORM.chapter_id == "ch-0001")
                .all()
            )
            assert len(rows) == 1
            row = rows[0]
            assert row.verdict == "parse_failed"
            assert row.score_overall == 0.0
            assert row.score_plot_progression == 0.0
            assert row.hard_constraints_passed is False
            # failed_constraints 含 reviewer_parse_failed_xN 标记
            assert any("reviewer_parse_failed" in c for c in row.failed_constraints)
            # suggestions 含累积错误摘要 + raw_output 预览
            suggestions_blob = "\n".join(row.suggestions)
            assert "第 1 次尝试失败" in suggestions_blob
            assert "第 2 次尝试失败" in suggestions_blob
            assert "原始输出预览" in suggestions_blob

    def test_no_op_when_session_factory_is_none(self) -> None:
        """对齐 persist_review_result_safely:session_factory=None → noop 不抛。"""
        from src.core.orchestrator.chapter_orchestrator import (
            _persist_parse_failure_safely,
        )

        _persist_parse_failure_safely(
            None,
            chapter_id="ch-0001",
            version_reviewed=1,
            raw_output="x",
            model_used="qwen-novel",
            attempts_log=[],
        )  # 不应抛

    def test_session_factory_exception_is_logged_and_raised(self) -> None:
        """Reviewer 已付费后账本打开失败必须硬停，避免恢复时重复请求。"""
        from src.core.orchestrator.chapter_orchestrator import (
            _persist_parse_failure_safely,
        )

        def _broken() -> Session:
            raise RuntimeError("[test] 故意挂")

        with capture_logs() as logs:
            with pytest.raises(ReviewCheckpointPersistenceError):
                _persist_parse_failure_safely(
                    _broken,
                    chapter_id="ch-0001",
                    version_reviewed=1,
                    raw_output="x",
                    model_used="qwen-novel",
                    attempts_log=[],
                )
        events = [
            log for log in logs if log.get("event") == "review_result_persist_failed"
        ]
        assert len(events) == 1
        assert events[0]["stage"] == "open_session_parse_failed"

    def test_empty_raw_output_uses_fallback_suggestion(
        self, in_memory_session: Session, session_factory
    ) -> None:
        """raw_output='' + attempts_log=[] → 用 fallback 提示文案,不留空 suggestions。"""
        from src.core.orchestrator.chapter_orchestrator import (
            _persist_parse_failure_safely,
        )

        _persist_parse_failure_safely(
            session_factory,
            chapter_id="ch-0001",
            version_reviewed=1,
            raw_output="",
            model_used="qwen-novel",
            attempts_log=[],
        )
        engine = in_memory_session.get_bind()
        with Session(engine) as verify:
            row = (
                verify.query(ReviewResultORM)
                .filter(ReviewResultORM.chapter_id == "ch-0001")
                .one()
            )
        assert row.suggestions  # 非空
        assert "Reviewer LLM" in row.suggestions[0]


# ---- ChapterService 集成测试 -----------------------------------------------


class TestReviewResultsIntegration:
    """``ChapterService.write`` 路径落 review_results + review_issues 验证。"""

    def test_write_pass_persists_one_review_zero_issues(
        self,
        workspace_service: WorkspaceService,
        prepared_workspace: str,
    ) -> None:
        """mock verdict=pass + wiring 启用 → 1 review_results 行 + 0 issues。"""
        svc = ChapterService(
            workspace_service=workspace_service,
            client=MockLLMClient(),
            model="claude-sonnet-4-6",
            session_factory=_make_session_factory(workspace_service),
            vector_store_factory=_make_vector_store_factory(workspace_service),
        )
        svc.save_chapter_card(prepared_workspace, _make_card("ch-0001", 1))
        svc.write(prepared_workspace, "ch-0001")

        paths = WorkspacePaths(
            prepared_workspace, workspaces_root=workspace_service.workspaces_root
        )
        engine = create_engine(f"sqlite:///{paths.db_path}")
        with Session(engine) as session:
            reviews = (
                session.query(ReviewResultORM)
                .filter(ReviewResultORM.chapter_id == "ch-0001")
                .all()
            )
            issues = session.query(ReviewIssueORM).count()
        assert len(reviews) == 1
        assert reviews[0].verdict == "pass"
        assert reviews[0].score_overall >= 7.5  # mock pass=9.0
        assert reviews[0].hard_constraints_passed is True
        assert reviews[0].version_reviewed == 1
        assert issues == 0

    def test_write_revise_persists_review_with_issues(
        self,
        workspace_service: WorkspaceService,
        prepared_workspace: str,
        monkeypatch: pytest.MonkeyPatch,
    ) -> None:
        """mock verdict=revise(全程)+ max_revises=1 → 至少 2 reviews + 含 issues 行。

        revise 路径:第一轮 review verdict=revise → REVISING(revise_count=1)→
        Reviser → 第二轮 review verdict=revise(revise_count >= max_revises)→
        rewrite 升级 → REWRITING → 第三轮 review verdict=revise(rewrite_count >=
        max_rewrites=1)→ HUMAN_INTERVENTION。三次审稿都落 review_results。
        """
        monkeypatch.setenv("LLM_MOCK_VERDICT", "revise")
        svc = ChapterService(
            workspace_service=workspace_service,
            client=MockLLMClient(),
            model="claude-sonnet-4-6",
            session_factory=_make_session_factory(workspace_service),
            vector_store_factory=_make_vector_store_factory(workspace_service),
        )
        svc.save_chapter_card(prepared_workspace, _make_card("ch-0001", 1))
        svc.write(prepared_workspace, "ch-0001")

        paths = WorkspacePaths(
            prepared_workspace, workspaces_root=workspace_service.workspaces_root
        )
        engine = create_engine(f"sqlite:///{paths.db_path}")
        with Session(engine) as session:
            reviews = (
                session.query(ReviewResultORM)
                .filter(ReviewResultORM.chapter_id == "ch-0001")
                .order_by(ReviewResultORM.reviewed_at)
                .all()
            )
            # mock revise:每条 review 含 1 issue + 1 revise_target(P1-B-22 mock 行为)
            issue_rows = session.query(ReviewIssueORM).order_by(ReviewIssueORM.id).all()

        assert len(reviews) >= 2  # 至少两轮审稿都落库
        assert all(r.verdict == "revise" for r in reviews)
        assert all(r.score_overall < 8.0 for r in reviews)  # mock revise=7.0

        # mock revise 路径每条 review 都有 issues 与 revise_targets
        roles = {r.role for r in issue_rows}
        assert "issue" in roles
        assert "revise_target" in roles

    def test_no_session_factory_skips_persistence(
        self,
        workspace_service: WorkspaceService,
        prepared_workspace: str,
    ) -> None:
        """P1 兼容:不传 session_factory → 不落 review_results 表。"""
        svc = ChapterService(
            workspace_service=workspace_service,
            client=MockLLMClient(),
            model="claude-sonnet-4-6",
        )
        svc.save_chapter_card(prepared_workspace, _make_card("ch-0001", 1))
        svc.write(prepared_workspace, "ch-0001")

        paths = WorkspacePaths(
            prepared_workspace, workspaces_root=workspace_service.workspaces_root
        )
        if paths.db_path.exists():
            from sqlalchemy import inspect

            engine = create_engine(f"sqlite:///{paths.db_path}")
            if inspect(engine).has_table("review_results"):
                with Session(engine) as session:
                    assert session.query(ReviewResultORM).count() == 0
