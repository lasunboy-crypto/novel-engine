"""P4-B-03:metrics_per_chapter 写入 hook 单元 + 集成测试。

覆盖契约:

1. ``write_per_chapter_safely`` helper(单元层 6 测):
   - 给定 session_factory + 完整前置数据(chapter row + 1 version + 1 review)
     → 落 1 行,所有字段从 DB 反查 + card 字面正确
   - chapter_id 主键已存在 → 静默幂等
   - ``session_factory=None`` → 静默 no-op
   - chapter row 不存在 → log warning + 不写
   - chapter row.approved_at is None → log warning + 不写
   - session_factory 抛 → swallow + structlog ``metrics_per_chapter_persist_failed``

2. ``ChapterService.write`` 集成层(2 测):
   - mock verdict=pass + session_factory + vector_store 都注入 → APPROVED 后
     落 metrics_per_chapter 1 行,score / cost / latency 字段非空 +
     target_word_count_min / target_tension_level 与 card 一致
   - session_factory=None → 不写 metrics 行(P1 兼容)
"""

from __future__ import annotations

from datetime import datetime, timedelta, timezone
from pathlib import Path

import pytest
from sqlalchemy import create_engine, inspect, select
from sqlalchemy.orm import Session
from structlog.testing import capture_logs

from src.core.db import Base
from src.core.db.models import (
    AttemptLedgerORM,
    ChapterORM,
    ChapterVersionORM,
    MetricsPerChapterORM,
    ReviewIssueORM,  # noqa: F401 -- 避免 ORM 注册遗漏(防御 metadata.create_all)
    ReviewResultORM,
    WorkspaceORM,
)
from src.core.llm.mock_client import MockLLMClient
from src.core.memory.vector import VectorStore
from src.core.models.chapter_card import ChapterCard, ContinuityContext
from src.core.workspace.workspace_paths import WorkspacePaths
from src.services.chapter_service import ChapterService
from src.services.metrics_writer import write_per_chapter_safely
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


def _make_card(
    chapter_id: str = "ch-0001",
    chapter_number: int = 1,
    *,
    workspace_id: str = "ws-metrics",
    # P4-B-06:validator 默认放宽,本文件测 metrics writer,validator 不参与。
    word_count_min: int = 1,
    word_count_max: int = 10**8,
    tension_level: int = 6,
) -> ChapterCard:
    return ChapterCard(
        chapter_id=chapter_id,
        workspace_id=workspace_id,
        volume_id="v01",
        chapter_number=chapter_number,
        title=f"第 {chapter_number} 章·测试",
        tension_level=tension_level,
        word_count_min=word_count_min,
        word_count_max=word_count_max,
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
        "ws-metrics",
        display_name="P4-B-03 metrics_per_chapter 集成测试",
        genre="test",
        premise="验证 metrics_per_chapter 落库",
        total_chapters=3,
    )
    return "ws-metrics"


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


# ---- helper 单元测试 -------------------------------------------------------


class TestWritePerChapterSafely:
    """``write_per_chapter_safely`` helper 单元层契约。"""

    @pytest.fixture
    def in_memory_session(self):
        """带 chapters + 1 version + 1 review 的 in-memory SQLite session。"""
        engine = create_engine("sqlite:///:memory:")
        Base.metadata.create_all(engine)
        session = Session(engine)
        now = datetime(2026, 5, 10, 12, 0, 0, tzinfo=timezone.utc)
        approved = now + timedelta(seconds=42.5)
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
                status="approved",
                rewrite_count=0,
                revise_count=0,
                created_at=now,
                approved_at=approved,
                total_token_usage={
                    "mock-default": {"input_tokens": 1500, "output_tokens": 800},
                    "claude-sonnet-4-6": {"input_tokens": 200, "output_tokens": 100},
                },
                total_cost_usd=0.1234,
            )
        )
        session.add(
            ChapterVersionORM(
                chapter_id="ch-0001",
                version_id=1,
                content="[v1] mock",
                word_count=1234,
                created_at=now,
                creation_type="draft",
                model_used="mock-default",
                review_score=8.5,
                notes="",
            )
        )
        session.add(
            ReviewResultORM(
                review_id="rev-0001",
                chapter_id="ch-0001",
                version_reviewed=1,
                reviewed_at=approved,
                reviewer_model="mock-default",
                score_plot_progression=8.0,
                score_tension_match=8.5,
                score_prose_quality=9.0,
                score_consistency=8.5,
                score_hook_management=8.0,
                score_voice_consistency=9.0,
                score_overall=8.5,
                hard_constraints_passed=True,
                failed_constraints=[],
                suggestions=[],
                verdict="pass",
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

    def test_persists_row_with_full_fields(
        self, in_memory_session: Session, session_factory
    ) -> None:
        card = _make_card(
            "ch-0001",
            workspace_id="ws-helper",
            word_count_min=800,
            word_count_max=2000,
            tension_level=7,
        )
        write_per_chapter_safely(
            session_factory,
            workspace_id="ws-helper",
            chapter_id="ch-0001",
            card=card,
        )

        engine = in_memory_session.get_bind()
        with Session(engine) as verify:
            row = verify.get(MetricsPerChapterORM, "ch-0001")
        assert row is not None
        # 时间 + latency(SQLite 不保留 tz,round-trip 后是 naive datetime;
        # 写入用 tz-aware,读出来要 normalize 比较)
        assert row.drafted_at.replace(tzinfo=timezone.utc) == datetime(
            2026, 5, 10, 12, 0, 0, tzinfo=timezone.utc
        )
        assert row.approved_at.replace(tzinfo=timezone.utc) == datetime(
            2026, 5, 10, 12, 0, 42, 500000, tzinfo=timezone.utc
        )
        assert row.total_latency_seconds == pytest.approx(42.5)
        # 计数:无 rewrite / 无 revise / 1 writer call(draft) / 1 reviewer
        assert row.rewrite_count == 0
        assert row.revise_count == 0
        assert row.writer_calls == 1
        assert row.reviewer_calls == 1
        assert row.reviser_calls == 0
        # parallel:single → False
        assert row.legacy_parallel_drafts_used is False
        # 6 档分项 + overall
        assert row.final_score_overall == 8.5
        assert row.final_score_plot_progression == 8.0
        assert row.final_score_tension_match == 8.5
        assert row.final_score_prose_quality == 9.0
        assert row.final_score_consistency == 8.5
        assert row.final_score_voice_consistency == 9.0
        assert row.final_score_hook_management == 8.0
        # 字数 + target
        assert row.word_count == 1234
        assert row.target_word_count_min == 800
        assert row.target_word_count_max == 2000
        assert row.target_tension_level == 7
        assert row.actual_tension_estimate is None
        # 成本 + token 累加(1500+800 + 200+100 = 2600)
        assert row.total_tokens == 2600
        assert row.total_cost_usd == pytest.approx(0.1234)
        # writer profile / model — fallback model_used(类 A 登记)
        assert row.writer_profile == "mock-default"
        assert row.writer_model == "mock-default"
        # cache 字段默认 disable
        assert row.cache_input_tokens is None
        assert row.cache_hit_ratio is None

    def test_local_paragraph_reflow_is_not_counted_as_writer_or_rewrite(
        self,
        in_memory_session: Session,
        session_factory,
    ) -> None:
        """纯代码插空行留版本审计，但运营指标只计真实 Writer/content rewrite。"""
        now = datetime(2026, 5, 10, 12, 1, 0, tzinfo=timezone.utc)
        in_memory_session.add_all(
            [
                ChapterVersionORM(
                    chapter_id="ch-0001",
                    version_id=2,
                    content="模型全文重写",
                    word_count=6,
                    created_at=now,
                    creation_type="rewrite",
                    model_used="qwen3.7-max",
                    review_score=8.0,
                    notes="",
                ),
                ChapterVersionORM(
                    chapter_id="ch-0001",
                    version_id=3,
                    content="模型全文重写。\n\n第二段。",
                    word_count=13,
                    created_at=now,
                    creation_type="rewrite",
                    model_used="local:paragraph_reflow",
                    review_score=8.0,
                    notes="",
                ),
            ]
        )
        in_memory_session.commit()

        write_per_chapter_safely(
            session_factory,
            workspace_id="ws-helper",
            chapter_id="ch-0001",
            card=_make_card("ch-0001", workspace_id="ws-helper"),
        )

        with Session(in_memory_session.get_bind()) as verify:
            row = verify.get(MetricsPerChapterORM, "ch-0001")
        assert row is not None
        assert row.rewrite_count == 1
        assert row.writer_calls == 2

    def test_provider_calls_come_from_attempt_ledger(
        self,
        in_memory_session: Session,
        session_factory,
    ) -> None:
        """有 R5 账本时，物理调用数覆盖旧版 version/review 近似值。"""
        now = datetime(2026, 5, 10, 12, 0, 0, tzinfo=timezone.utc)
        agents = ("writer", "writer", "reviewer", "reviser")
        for index, agent_name in enumerate(agents, start=1):
            in_memory_session.add(
                AttemptLedgerORM(
                    attempt_id=f"att-metrics-{index}",
                    workspace_id="ws-helper",
                    run_id="run-r5",
                    task_id="task-r5",
                    chapter_id="ch-0001",
                    attempt_kind="provider",
                    operation_kind="agent_call",
                    logical_call_id=f"call-{agent_name}",
                    agent_name=agent_name,
                    provider="openai_compatible",
                    model="qwen-r5",
                    attempt_number=2 if index == 2 else 1,
                    attempt_cap=10,
                    status="failed" if index == 2 else "succeeded",
                    version_before=None,
                    version_after=None,
                    error_type="ReadTimeout" if index == 2 else None,
                    created_at=now,
                    completed_at=now,
                )
            )
        in_memory_session.commit()

        write_per_chapter_safely(
            session_factory,
            workspace_id="ws-helper",
            chapter_id="ch-0001",
            card=_make_card("ch-0001", workspace_id="ws-helper"),
        )

        with Session(in_memory_session.get_bind()) as verify:
            row = verify.get(MetricsPerChapterORM, "ch-0001")
        assert row is not None
        assert (row.writer_calls, row.reviewer_calls, row.reviser_calls) == (
            2,
            1,
            1,
        )

    def test_idempotent_on_duplicate_pk(
        self, in_memory_session: Session, session_factory
    ) -> None:
        """同 chapter_id 二次调:静默跳过,不抛 IntegrityError。"""
        card = _make_card("ch-0001", workspace_id="ws-helper")
        write_per_chapter_safely(
            session_factory,
            workspace_id="ws-helper",
            chapter_id="ch-0001",
            card=card,
        )
        write_per_chapter_safely(
            session_factory,
            workspace_id="ws-helper",
            chapter_id="ch-0001",
            card=card,
        )
        engine = in_memory_session.get_bind()
        with Session(engine) as verify:
            count = verify.query(MetricsPerChapterORM).count()
        assert count == 1

    # ---- v2 w2-s2 metrics 修复 A:refresh_existing 成本补快照 ----------------

    def test_refresh_existing_updates_cost_after_hook_chain(
        self, in_memory_session: Session, session_factory
    ) -> None:
        """w2-s1 报告 §7.1 漏记②防回退:APPROVED 快照后 hook 链(summary+
        memory)继续把成本累进 chapters 账 → ``refresh_existing=True`` 二次
        调用必须把 per_chapter 行的成本/token 刷新到 chapters 账最新值,
        且**不动**评分等其余快照字段。"""
        card = _make_card("ch-0001", workspace_id="ws-helper")
        write_per_chapter_safely(
            session_factory,
            workspace_id="ws-helper",
            chapter_id="ch-0001",
            card=card,
        )
        # 模拟 hook 链 LLM 成本累进 chapters 账(0.1234 → 0.4234,+$0.30
        # 即报告 §7.1 每章稳定差 $0.21-0.38 区间)
        engine = in_memory_session.get_bind()
        with Session(engine) as mutate:
            chapter = mutate.get(ChapterORM, "ch-0001")
            chapter.total_cost_usd = 0.4234
            chapter.total_token_usage = {
                "mock-default": {"input_tokens": 2000, "output_tokens": 1100},
            }
            mutate.commit()
        write_per_chapter_safely(
            session_factory,
            workspace_id="ws-helper",
            chapter_id="ch-0001",
            card=card,
            refresh_existing=True,
        )
        with Session(engine) as verify:
            row = verify.get(MetricsPerChapterORM, "ch-0001")
        assert row.total_cost_usd == pytest.approx(0.4234)
        assert row.total_tokens == 3100
        # 其余快照字段不被 refresh 触碰
        assert row.final_score_overall == 8.5
        assert row.writer_calls == 1

    def test_refresh_existing_false_keeps_skip_semantics(
        self, in_memory_session: Session, session_factory
    ) -> None:
        """默认 ``refresh_existing=False`` 幂等跳过语义不回退:chapters 账
        变了也不刷新(只有显式 refresh caller 才能动已落行)。"""
        card = _make_card("ch-0001", workspace_id="ws-helper")
        write_per_chapter_safely(
            session_factory,
            workspace_id="ws-helper",
            chapter_id="ch-0001",
            card=card,
        )
        engine = in_memory_session.get_bind()
        with Session(engine) as mutate:
            mutate.get(ChapterORM, "ch-0001").total_cost_usd = 9.99
            mutate.commit()
        write_per_chapter_safely(
            session_factory,
            workspace_id="ws-helper",
            chapter_id="ch-0001",
            card=card,
        )
        with Session(engine) as verify:
            row = verify.get(MetricsPerChapterORM, "ch-0001")
        assert row.total_cost_usd == pytest.approx(0.1234)

    def test_refresh_existing_creates_row_when_missing(
        self, in_memory_session: Session, session_factory
    ) -> None:
        """refresh 调用兜底:行不存在(首次快照失败场景)→ 走新建路径
        落全字段行,不静默丢。"""
        card = _make_card("ch-0001", workspace_id="ws-helper")
        write_per_chapter_safely(
            session_factory,
            workspace_id="ws-helper",
            chapter_id="ch-0001",
            card=card,
            refresh_existing=True,
        )
        engine = in_memory_session.get_bind()
        with Session(engine) as verify:
            row = verify.get(MetricsPerChapterORM, "ch-0001")
        assert row is not None
        assert row.total_cost_usd == pytest.approx(0.1234)

    def test_no_op_when_session_factory_is_none(self) -> None:
        """``session_factory=None`` → 静默返,不抛。"""
        card = _make_card("ch-0001")
        write_per_chapter_safely(
            None, workspace_id="ws-helper", chapter_id="ch-0001", card=card
        )

    def test_skips_when_chapter_row_missing(self, session_factory) -> None:
        """目标 chapter_id 在 chapters 表无行 → log warning + 不写。"""
        card = _make_card("ch-9999", workspace_id="ws-helper")
        with capture_logs() as logs:
            write_per_chapter_safely(
                session_factory,
                workspace_id="ws-helper",
                chapter_id="ch-9999",
                card=card,
            )
        events = [
            log for log in logs
            if log.get("event") == "metrics_per_chapter_chapter_row_missing"
        ]
        assert len(events) == 1
        assert events[0]["chapter_id"] == "ch-9999"

    def test_skips_when_not_approved(
        self, in_memory_session: Session, session_factory
    ) -> None:
        """``chapters.approved_at is None`` → log warning + 不写 metrics 行。"""
        # 把 ch-0001 的 approved_at 清掉
        engine = in_memory_session.get_bind()
        with Session(engine) as s:
            row = s.get(ChapterORM, "ch-0001")
            assert row is not None
            row.approved_at = None
            s.commit()
        card = _make_card("ch-0001", workspace_id="ws-helper")
        with capture_logs() as logs:
            write_per_chapter_safely(
                session_factory,
                workspace_id="ws-helper",
                chapter_id="ch-0001",
                card=card,
            )
        events = [
            log for log in logs
            if log.get("event") == "metrics_per_chapter_not_approved"
        ]
        assert len(events) == 1
        with Session(engine) as verify:
            assert verify.query(MetricsPerChapterORM).count() == 0

    def test_swallows_session_factory_exception(self) -> None:
        """session_factory 调用本身抛 → swallow + structlog log,不冒泡。"""

        def _broken_factory() -> Session:
            raise RuntimeError("[test] session 工厂故意挂")

        card = _make_card("ch-0001")
        with capture_logs() as logs:
            write_per_chapter_safely(
                _broken_factory,
                workspace_id="ws-helper",
                chapter_id="ch-0001",
                card=card,
            )
        events = [
            log for log in logs
            if log.get("event") == "metrics_per_chapter_persist_failed"
        ]
        assert len(events) == 1
        assert events[0]["stage"] == "open_session"
        assert events[0]["error_type"] == "RuntimeError"

    def test_swallows_commit_exception(
        self, in_memory_session: Session
    ) -> None:
        """commit 抛 → swallow + structlog log + rollback + close。"""
        engine = in_memory_session.get_bind()
        rolled_back: list[bool] = []

        def _factory() -> Session:
            s = Session(engine)
            # 用 sqlalchemy event listener 在 BEFORE_COMMIT 抛错;比 monkeypatch
            # session.commit 方法稳健(后者 sqlalchemy 内部走多层组合)
            from sqlalchemy import event

            @event.listens_for(s, "before_commit")
            def _fail_before_commit(_session: Session) -> None:
                rolled_back.append(True)
                raise RuntimeError("[test] commit 故意失败")

            return s

        card = _make_card("ch-0001", workspace_id="ws-helper")
        with capture_logs() as logs:
            write_per_chapter_safely(
                _factory,
                workspace_id="ws-helper",
                chapter_id="ch-0001",
                card=card,
            )
        events = [
            log for log in logs
            if log.get("event") == "metrics_per_chapter_persist_failed"
        ]
        assert len(events) == 1
        assert events[0]["stage"] == "commit"
        assert events[0]["error_type"] == "RuntimeError"
        assert rolled_back == [True]
        # 没真正落 metrics 行(rollback 兜底)
        with Session(engine) as verify:
            assert verify.query(MetricsPerChapterORM).count() == 0


# ---- ChapterService 集成测试 -----------------------------------------------


class TestMetricsWriterIntegration:
    """``ChapterService.write`` mock pass 路径自动落 metrics_per_chapter 行。"""

    def test_write_pass_persists_metrics_row(
        self,
        workspace_service: WorkspaceService,
        prepared_workspace: str,
    ) -> None:
        """完整 wiring + verdict=pass → APPROVED 后落 metrics_per_chapter 1 行。"""
        svc = ChapterService(
            workspace_service=workspace_service,
            client=MockLLMClient(),
            model="claude-sonnet-4-6",
            session_factory=_make_session_factory(workspace_service),
            vector_store_factory=_make_vector_store_factory(workspace_service),
        )
        card = _make_card(
            "ch-0001",
            chapter_number=1,
            workspace_id=prepared_workspace,
            word_count_min=400,
            word_count_max=2000,
            tension_level=7,
        )
        svc.save_chapter_card(prepared_workspace, card)
        result = svc.write(prepared_workspace, "ch-0001")
        # 主流水线 APPROVED
        assert result.final_state.value == "approved"

        paths = WorkspacePaths(
            prepared_workspace, workspaces_root=workspace_service.workspaces_root
        )
        engine = create_engine(f"sqlite:///{paths.db_path}")
        with Session(engine) as session:
            rows = list(
                session.execute(select(MetricsPerChapterORM)).scalars()
            )
        assert len(rows) == 1
        row = rows[0]
        assert row.chapter_id == "ch-0001"
        # score / cost / latency / counts 字段非空
        assert row.final_score_overall is not None and row.final_score_overall > 0
        assert row.final_score_voice_consistency is not None
        assert row.total_latency_seconds is not None and row.total_latency_seconds >= 0
        assert row.word_count > 0
        # target 字段与 card 一致
        assert row.target_word_count_min == 400
        assert row.target_word_count_max == 2000
        assert row.target_tension_level == 7
        # parallel:single → False
        assert row.legacy_parallel_drafts_used is False
        # writer_calls >= 1(至少 1 draft);reviewer_calls >= 1
        assert row.writer_calls >= 1
        assert row.reviewer_calls >= 1
        assert row.reviser_calls == 0
        # cache 字段默认 disable(决策 5=B)
        assert row.cache_input_tokens is None
        assert row.cache_hit_ratio is None

    def test_no_session_factory_skips_metrics(
        self,
        workspace_service: WorkspaceService,
        prepared_workspace: str,
    ) -> None:
        """P1 兼容:session_factory=None → 不写 metrics 行,不抛错。"""
        svc = ChapterService(
            workspace_service=workspace_service,
            client=MockLLMClient(),
            model="claude-sonnet-4-6",
            # 不传 session_factory → 整个 wiring 退化到 P1 路径
        )
        card = _make_card("ch-0001", workspace_id=prepared_workspace)
        svc.save_chapter_card(prepared_workspace, card)
        result = svc.write(prepared_workspace, "ch-0001")
        assert result.final_state.value == "approved"

        paths = WorkspacePaths(
            prepared_workspace, workspaces_root=workspace_service.workspaces_root
        )
        # SQLite db 文件不应被建出来
        if paths.db_path.exists():
            engine = create_engine(f"sqlite:///{paths.db_path}")
            if inspect(engine).has_table("metrics_per_chapter"):
                with Session(engine) as session:
                    assert session.query(MetricsPerChapterORM).count() == 0
