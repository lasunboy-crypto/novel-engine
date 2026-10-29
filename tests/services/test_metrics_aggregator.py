"""P4-B-04:metrics_daily 聚合 helper 单元 + 集成测试。

覆盖契约:

1. ``aggregate_daily_safely`` helper(单元层 7 测):
   - 空表(无 metrics_per_chapter 行)→ upsert 一行 counts=0 + avg=None
   - 1 行 metrics_per_chapter 当日 APPROVED → counts=1 + avg=该值
   - 2 行 metrics_per_chapter 当日 + 1 行非当日 → 当日 2,跨天不串
   - 重复触发 → 幂等(全量重算,不双计数)
   - 包含 rewrite_count >= 1 / revise_count >= 1 → chapters_rewritten / revised
   - per-model writer_calls 分桶(qwen / claude-sonnet / claude-opus)
   - session_factory=None → no-op
   - session_factory 抛 → swallow + structlog log

2. ``ChapterService.write`` 集成层(2 测):
   - mock pass 路径 → APPROVED 后 metrics_daily 当日行落库 +
     chapters_drafted=chapters_approved=1
   - 无 session_factory → 不写 metrics_daily 行

3. compute_overview / list_aggregated_rows / get_aggregated_row 3 个 read
   helper(各 1 测)。
"""

from __future__ import annotations

import datetime as _dt
from datetime import datetime, timedelta, timezone
from pathlib import Path

import pytest
from sqlalchemy import create_engine, select
from sqlalchemy.orm import Session
from structlog.testing import capture_logs

from src.core.db import Base
from src.core.db.models import (
    AttemptLedgerORM,
    ChapterORM,  # noqa: F401 -- metadata.create_all 防御
    ChapterVersionORM,  # noqa: F401 -- 同上
    MetricsDailyORM,
    MetricsPerChapterORM,
    ReviewIssueORM,  # noqa: F401 -- 同上
    ReviewResultORM,  # noqa: F401 -- 同上
    WorkspaceORM,  # noqa: F401 -- 同上
)
from src.core.llm.mock_client import MockLLMClient
from src.core.memory.vector import VectorStore
from src.core.models.chapter_card import ChapterCard, ContinuityContext
from src.core.workspace.workspace_paths import WorkspacePaths
from src.services.chapter_service import ChapterService
from src.services.metrics_aggregator import (
    aggregate_daily_safely,
    compute_overview,
    get_aggregated_row,
    list_aggregated_rows,
)
from src.services.workspace_service import WorkspaceService

pytestmark = pytest.mark.usefixtures("valid_character_admission")


# ---- fixtures --------------------------------------------------------------


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
    workspace_id: str = "ws-agg",
    # P4-B-06:validator 默认放宽,本文件测 metrics aggregator,validator 不参与。
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
        "ws-agg",
        display_name="P4-B-04 metrics_aggregator 集成测试",
        genre="test",
        premise="验证 metrics_daily 落库",
        total_chapters=3,
    )
    return "ws-agg"


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


def _seed_per_chapter(
    session: Session,
    chapter_id: str,
    *,
    approved_at: datetime,
    final_score: float = 8.5,
    rewrite_count: int = 0,
    revise_count: int = 0,
    total_cost_usd: float = 0.0,
    total_tokens: int = 1000,
    writer_model: str = "mock-default",
    writer_calls: int = 1,
    total_latency_seconds: float = 30.0,
) -> None:
    session.add(
        MetricsPerChapterORM(
            chapter_id=chapter_id,
            drafted_at=approved_at - timedelta(seconds=total_latency_seconds),
            approved_at=approved_at,
            total_latency_seconds=total_latency_seconds,
            rewrite_count=rewrite_count,
            revise_count=revise_count,
            legacy_parallel_drafts_used=False,
            final_score_overall=final_score,
            final_score_plot_progression=final_score,
            final_score_tension_match=final_score,
            final_score_prose_quality=final_score,
            final_score_consistency=final_score,
            final_score_voice_consistency=final_score,
            final_score_hook_management=final_score,
            word_count=1234,
            target_word_count_min=800,
            target_word_count_max=2000,
            target_tension_level=6,
            actual_tension_estimate=None,
            total_tokens=total_tokens,
            total_cost_usd=total_cost_usd,
            writer_profile=writer_model,
            writer_model=writer_model,
            writer_calls=writer_calls,
            reviewer_calls=1,
            reviser_calls=revise_count,
            cache_input_tokens=None,
            cache_hit_ratio=None,
        )
    )
    session.commit()


@pytest.fixture
def in_memory_session():
    engine = create_engine("sqlite:///:memory:")
    Base.metadata.create_all(engine)
    session = Session(engine)
    yield session
    session.close()


@pytest.fixture
def session_factory(in_memory_session: Session):
    engine = in_memory_session.get_bind()

    def _factory() -> Session:
        return Session(engine)

    return _factory


# ---- helper 单元测试 -------------------------------------------------------


class TestAggregateDailySafely:
    def test_empty_table_creates_zero_row(
        self, in_memory_session: Session, session_factory
    ) -> None:
        date = _dt.date(2026, 5, 10)
        aggregate_daily_safely(
            session_factory, workspace_id="ws-agg", date=date
        )
        engine = in_memory_session.get_bind()
        with Session(engine) as verify:
            row = verify.get(MetricsDailyORM, date)
        assert row is not None
        assert row.chapters_drafted == 0
        assert row.chapters_approved == 0
        assert row.chapters_rewritten == 0
        assert row.chapters_revised == 0
        assert row.avg_review_score is None
        assert row.avg_chapter_latency_seconds is None
        assert row.total_cost_usd == 0.0

    def test_single_chapter_aggregates(
        self, in_memory_session: Session, session_factory
    ) -> None:
        approved = datetime(2026, 5, 10, 12, 0, 0, tzinfo=timezone.utc)
        _seed_per_chapter(
            in_memory_session,
            "ch-0001",
            approved_at=approved,
            final_score=8.5,
            rewrite_count=0,
            total_cost_usd=0.05,
            total_tokens=2000,
            writer_model="mock-default",
            writer_calls=1,
            total_latency_seconds=42.5,
        )
        aggregate_daily_safely(
            session_factory, workspace_id="ws-agg", date=approved.date()
        )
        engine = in_memory_session.get_bind()
        with Session(engine) as verify:
            row = verify.get(MetricsDailyORM, approved.date())
        assert row is not None
        assert row.chapters_drafted == 1
        assert row.chapters_approved == 1
        assert row.chapters_rewritten == 0
        assert row.chapters_revised == 0
        assert row.avg_review_score == pytest.approx(8.5)
        assert row.avg_rewrites_per_chapter == pytest.approx(0.0)
        assert row.total_cost_usd == pytest.approx(0.05)
        assert row.total_input_tokens == 2000
        assert row.avg_chapter_latency_seconds == pytest.approx(42.5)

    def test_cross_day_isolation(
        self, in_memory_session: Session, session_factory
    ) -> None:
        today = datetime(2026, 5, 10, 6, 0, 0, tzinfo=timezone.utc)
        yesterday = datetime(2026, 5, 9, 23, 0, 0, tzinfo=timezone.utc)
        _seed_per_chapter(
            in_memory_session, "ch-0001", approved_at=today, final_score=9.0
        )
        _seed_per_chapter(
            in_memory_session, "ch-0002", approved_at=today, final_score=8.0
        )
        _seed_per_chapter(
            in_memory_session,
            "ch-0003",
            approved_at=yesterday,
            final_score=7.5,
        )
        aggregate_daily_safely(
            session_factory, workspace_id="ws-agg", date=today.date()
        )
        engine = in_memory_session.get_bind()
        with Session(engine) as verify:
            row = verify.get(MetricsDailyORM, today.date())
        assert row is not None
        assert row.chapters_approved == 2
        assert row.avg_review_score == pytest.approx(8.5)

    def test_idempotent_full_recompute(
        self, in_memory_session: Session, session_factory
    ) -> None:
        approved = datetime(2026, 5, 10, 12, 0, 0, tzinfo=timezone.utc)
        _seed_per_chapter(
            in_memory_session, "ch-0001", approved_at=approved, total_cost_usd=0.1
        )
        date = approved.date()
        aggregate_daily_safely(
            session_factory, workspace_id="ws-agg", date=date
        )
        aggregate_daily_safely(
            session_factory, workspace_id="ws-agg", date=date
        )
        aggregate_daily_safely(
            session_factory, workspace_id="ws-agg", date=date
        )
        engine = in_memory_session.get_bind()
        with Session(engine) as verify:
            count = verify.query(MetricsDailyORM).count()
            row = verify.get(MetricsDailyORM, date)
        assert count == 1
        assert row is not None
        assert row.chapters_approved == 1
        assert row.total_cost_usd == pytest.approx(0.1)

    def test_rewritten_revised_counts(
        self, in_memory_session: Session, session_factory
    ) -> None:
        approved = datetime(2026, 5, 10, 12, 0, 0, tzinfo=timezone.utc)
        _seed_per_chapter(
            in_memory_session,
            "ch-0001",
            approved_at=approved,
            rewrite_count=2,
            revise_count=0,
        )
        _seed_per_chapter(
            in_memory_session,
            "ch-0002",
            approved_at=approved,
            rewrite_count=0,
            revise_count=1,
        )
        _seed_per_chapter(
            in_memory_session,
            "ch-0003",
            approved_at=approved,
            rewrite_count=1,
            revise_count=1,
        )
        aggregate_daily_safely(
            session_factory, workspace_id="ws-agg", date=approved.date()
        )
        engine = in_memory_session.get_bind()
        with Session(engine) as verify:
            row = verify.get(MetricsDailyORM, approved.date())
        assert row is not None
        assert row.chapters_drafted == 3
        # rewritten = 章节中 rewrite_count >= 1 = ch-0001 + ch-0003 = 2
        assert row.chapters_rewritten == 2
        # revised = 章节中 revise_count >= 1 = ch-0002 + ch-0003 = 2
        assert row.chapters_revised == 2
        assert row.avg_rewrites_per_chapter == pytest.approx(1.0)  # (2+0+1)/3
        assert row.avg_revises_per_chapter == pytest.approx(2 / 3)

    def test_per_model_call_buckets(
        self, in_memory_session: Session, session_factory
    ) -> None:
        approved = datetime(2026, 5, 10, 12, 0, 0, tzinfo=timezone.utc)
        _seed_per_chapter(
            in_memory_session,
            "ch-0001",
            approved_at=approved,
            writer_model="qwen-novel",
            writer_calls=2,
        )
        _seed_per_chapter(
            in_memory_session,
            "ch-0002",
            approved_at=approved,
            writer_model="claude-sonnet-4-6",
            writer_calls=1,
        )
        _seed_per_chapter(
            in_memory_session,
            "ch-0003",
            approved_at=approved,
            writer_model="claude-opus-4-7",
            writer_calls=1,
        )
        _seed_per_chapter(
            in_memory_session,
            "ch-0004",
            approved_at=approved,
            writer_model="mock-default",
            writer_calls=1,
        )
        aggregate_daily_safely(
            session_factory, workspace_id="ws-agg", date=approved.date()
        )
        engine = in_memory_session.get_bind()
        with Session(engine) as verify:
            row = verify.get(MetricsDailyORM, approved.date())
        assert row is not None
        assert row.qwen_calls == 2
        assert row.claude_sonnet_calls == 1
        assert row.claude_opus_calls == 1
        # mock-default 不进任何桶,但仍计入 chapters_drafted
        assert row.chapters_drafted == 4

    def test_per_model_buckets_use_all_provider_attempts(
        self,
        in_memory_session: Session,
        session_factory,
    ) -> None:
        """R5 后按所有 agent 的物理调用分桶，失败与 retry 也不能漏记。"""
        approved = datetime(2026, 5, 10, 12, 0, 0, tzinfo=timezone.utc)
        _seed_per_chapter(
            in_memory_session,
            "ch-0001",
            approved_at=approved,
            writer_model="mock-default",
            writer_calls=99,
        )
        models = (
            "qwen3.7-max",
            "qwen3.7-max",
            "claude-sonnet-4-6",
            "claude-opus-4-7",
        )
        for index, model in enumerate(models, start=1):
            in_memory_session.add(
                AttemptLedgerORM(
                    attempt_id=f"att-daily-{index}",
                    workspace_id="ws-agg",
                    run_id="run-r5",
                    task_id="task-r5",
                    chapter_id="ch-0001",
                    attempt_kind="provider",
                    operation_kind="agent_call",
                    logical_call_id=(
                        "call-qwen" if model.startswith("qwen") else f"call-{index}"
                    ),
                    agent_name=("reviewer" if index == 2 else "writer"),
                    provider="openai_compatible",
                    model=model,
                    attempt_number=2 if index == 2 else 1,
                    attempt_cap=10,
                    status="failed" if index == 2 else "succeeded",
                    version_before=None,
                    version_after=None,
                    error_type="ReadTimeout" if index == 2 else None,
                    created_at=approved,
                    completed_at=approved,
                )
            )
        in_memory_session.commit()

        aggregate_daily_safely(
            session_factory,
            workspace_id="ws-agg",
            date=approved.date(),
        )

        with Session(in_memory_session.get_bind()) as verify:
            row = verify.get(MetricsDailyORM, approved.date())
        assert row is not None
        assert (
            row.qwen_calls,
            row.claude_sonnet_calls,
            row.claude_opus_calls,
        ) == (2, 1, 1)

    def test_no_op_when_session_factory_is_none(self) -> None:
        aggregate_daily_safely(
            None, workspace_id="ws-agg", date=_dt.date(2026, 5, 10)
        )

    def test_swallows_session_factory_exception(self) -> None:
        def _broken_factory() -> Session:
            raise RuntimeError("[test] session 工厂故意挂")

        with capture_logs() as logs:
            aggregate_daily_safely(
                _broken_factory,
                workspace_id="ws-agg",
                date=_dt.date(2026, 5, 10),
            )
        events = [
            log for log in logs if log.get("event") == "metrics_daily_aggregate_failed"
        ]
        assert len(events) == 1
        assert events[0]["stage"] == "open_session"
        assert events[0]["error_type"] == "RuntimeError"


# ---- read helper 单元测试 --------------------------------------------------


class TestReadHelpers:
    def test_get_aggregated_row_existing(
        self, in_memory_session: Session
    ) -> None:
        date = _dt.date(2026, 5, 10)
        in_memory_session.add(
            MetricsDailyORM(
                date=date,
                chapters_drafted=1,
                chapters_approved=1,
                chapters_rewritten=0,
                chapters_revised=0,
                chapters_escalated=0,
                avg_review_score=8.5,
                avg_rewrites_per_chapter=0.0,
                avg_revises_per_chapter=0.0,
                total_input_tokens=2000,
                total_output_tokens=0,
                total_cost_usd=0.05,
                avg_chapter_latency_seconds=30.0,
                qwen_calls=0,
                claude_sonnet_calls=0,
                claude_opus_calls=0,
            )
        )
        in_memory_session.commit()
        row = get_aggregated_row(in_memory_session, date)
        assert row is not None
        assert row.chapters_approved == 1
        assert get_aggregated_row(in_memory_session, _dt.date(2025, 1, 1)) is None

    def test_list_aggregated_rows_range(
        self, in_memory_session: Session
    ) -> None:
        for d in (_dt.date(2026, 5, 8), _dt.date(2026, 5, 9), _dt.date(2026, 5, 10)):
            in_memory_session.add(
                MetricsDailyORM(
                    date=d,
                    chapters_drafted=0,
                    chapters_approved=0,
                    chapters_rewritten=0,
                    chapters_revised=0,
                    chapters_escalated=0,
                    total_input_tokens=0,
                    total_output_tokens=0,
                    total_cost_usd=0.0,
                    qwen_calls=0,
                    claude_sonnet_calls=0,
                    claude_opus_calls=0,
                )
            )
        in_memory_session.commit()
        rows = list_aggregated_rows(
            in_memory_session,
            since=_dt.date(2026, 5, 9),
            until=_dt.date(2026, 5, 10),
        )
        assert [r.date for r in rows] == [_dt.date(2026, 5, 9), _dt.date(2026, 5, 10)]
        all_rows = list_aggregated_rows(in_memory_session)
        assert len(all_rows) == 3

    def test_compute_overview_empty_returns_zeros(
        self, in_memory_session: Session
    ) -> None:
        out = compute_overview(in_memory_session)
        assert out["chapters_approved"] == 0
        assert out["total_cost_usd"] == 0.0
        assert out["avg_review_score"] is None

    def test_compute_overview_with_rows(
        self, in_memory_session: Session
    ) -> None:
        approved = datetime(2026, 5, 10, 12, 0, 0, tzinfo=timezone.utc)
        _seed_per_chapter(
            in_memory_session,
            "ch-0001",
            approved_at=approved,
            final_score=8.0,
            total_cost_usd=0.1,
            total_tokens=1000,
            total_latency_seconds=20,
        )
        _seed_per_chapter(
            in_memory_session,
            "ch-0002",
            approved_at=approved,
            final_score=9.0,
            total_cost_usd=0.2,
            total_tokens=2000,
            total_latency_seconds=40,
        )
        out = compute_overview(in_memory_session)
        assert out["chapters_approved"] == 2
        assert out["total_cost_usd"] == pytest.approx(0.3)
        assert out["total_tokens"] == 3000
        assert out["avg_review_score"] == pytest.approx(8.5)
        assert out["avg_chapter_latency_seconds"] == pytest.approx(30.0)


# ---- ChapterService 集成测试 -----------------------------------------------


class TestAggregatorIntegration:
    def test_write_pass_persists_metrics_daily_row(
        self,
        workspace_service: WorkspaceService,
        prepared_workspace: str,
    ) -> None:
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
            # P4-B-06:不限字数,避免 validator 触发 rewrite。
            word_count_min=1,
            word_count_max=10**8,
            tension_level=7,
        )
        svc.save_chapter_card(prepared_workspace, card)
        result = svc.write(prepared_workspace, "ch-0001")
        assert result.final_state.value == "approved"

        paths = WorkspacePaths(
            prepared_workspace, workspaces_root=workspace_service.workspaces_root
        )
        engine = create_engine(f"sqlite:///{paths.db_path}")
        with Session(engine) as session:
            rows = list(session.execute(select(MetricsDailyORM)).scalars())
        assert len(rows) == 1
        row = rows[0]
        assert row.chapters_drafted == 1
        assert row.chapters_approved == 1
        assert row.avg_review_score is not None and row.avg_review_score > 0
        assert row.total_input_tokens >= 0

    def test_write_pass_without_session_factory_skips_metrics_daily(
        self,
        workspace_service: WorkspaceService,
        prepared_workspace: str,
    ) -> None:
        svc = ChapterService(
            workspace_service=workspace_service,
            client=MockLLMClient(),
            model="claude-sonnet-4-6",
        )
        card = _make_card(
            "ch-0001",
            chapter_number=1,
            workspace_id=prepared_workspace,
        )
        svc.save_chapter_card(prepared_workspace, card)
        result = svc.write(prepared_workspace, "ch-0001")
        assert result.final_state.value == "approved"

        # 工作区 db 文件未创建(无 session_factory) — 直接断言文件不存在
        paths = WorkspacePaths(
            prepared_workspace, workspaces_root=workspace_service.workspaces_root
        )
        assert not paths.db_path.exists()
