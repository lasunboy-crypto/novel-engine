"""ChapterService._run_gates 单测(P4-acceptance 步 ⑦)。

兑现"主流水线 gate 接入"留观项(P4-B-08 + P4-B-09 + P4-B-10 三处接力):

- cost_guard:本工作区单日 cost > ``per_workspace_daily_limit`` → 抛
  ``CostGuardTriggered``(402)
- memory_health:``memory_health_history.severity=critical`` 最新一行 → 抛
  ``MemoryHealthBlocked``(409)
- style_audit:本任务 noop(``StyleAuditor stub`` 永不 critical)

测试覆盖 8 条:
1. session_factory=None → P1 兼容路径 gate 静默跳过
2. cost_guard_config=None + 无 history → 放行
3. cost_guard 配置 + workspace_today=0 → 放行
4. cost_guard 配置 + workspace_today > limit → 抛 ``CostGuardTriggered`` +
   ``details.scope=workspace_daily``
5. memory_health 无行 → 放行
6. memory_health 最新 critical → 抛 ``MemoryHealthBlocked`` +
   ``details.severity=critical``
7. memory_health 最新 warning → 放行(只 critical 阻断,4 档判定明确)
8. cost_guard 触发优先级高于 memory_health(都触发时先抛 cost_guard)

测试不依赖真 LLM / mock 路径走 ``MagicMock`` LLM client。
"""

from __future__ import annotations

import uuid
from datetime import datetime, timedelta, timezone
from pathlib import Path
from unittest.mock import MagicMock

import pytest
from sqlalchemy import create_engine
from sqlalchemy.orm import Session

from src.api.errors import CardNotApproved, CostGuardTriggered, MemoryHealthBlocked
from src.core.db import Base
from src.core.db.models import (
    AttemptLedgerORM,
    ChapterCardORM,
    ChapterORM,
    MemoryHealthCheckORM,
    MetricsDailyORM,
    WorkspaceORM,
)
from src.core.llm.base import LLMClient
from src.core.models.chapter_card import ChapterCard, ContinuityContext
from src.services.chapter_service import ChapterService
from src.services.cost_guard import CostGuardConfig
from src.services.workspace_service import WorkspaceService

pytestmark = pytest.mark.usefixtures("valid_character_admission")


# ---------------------------------------------------------------------------
# fixtures
# ---------------------------------------------------------------------------


@pytest.fixture
def workspace_service(tmp_path: Path) -> WorkspaceService:
    """tmp_path 下隔离的 workspaces/ + archives/。"""
    workspaces_root = tmp_path / "workspaces"
    archives_root = tmp_path / "archives"
    workspaces_root.mkdir()
    archives_root.mkdir()
    return WorkspaceService(
        workspaces_root=workspaces_root, archives_root=archives_root
    )


@pytest.fixture
def prepared_workspace_id(workspace_service: WorkspaceService) -> str:
    """建一个 ws-gate-test 工作区,workspace_id 返出。"""
    workspace_service.create(
        "ws-gate-test",
        display_name="gate 测试工作区",
        genre="xianxia",
        premise="测试用",
        total_chapters=10,
    )
    return "ws-gate-test"


@pytest.fixture
def session_factory(workspace_service: WorkspaceService):
    """每次调 factory(workspace_id) 返回新 Session(共用单一 in-memory engine
    保证写读一致);Base.metadata.create_all idempotent 建 schema。"""
    engine = create_engine("sqlite:///:memory:")
    Base.metadata.create_all(engine)

    def factory(workspace_id: str) -> Session:
        # 同 engine 内多 session 共享数据(SQLite :memory: 默认按连接隔离,
        # 这里通过 future engine 单实例 + Session(engine) 共连接池);
        # 同 workspace_id 多次调多次开 session 但数据一致。
        return Session(engine)

    return factory


@pytest.fixture
def mock_client() -> MagicMock:
    return MagicMock(spec=LLMClient)


def _make_service(
    *,
    workspace_service: WorkspaceService,
    mock_client: MagicMock,
    session_factory=None,
    cost_guard_config: CostGuardConfig | None = None,
    ws_manager=None,
) -> ChapterService:
    return ChapterService(
        workspace_service=workspace_service,
        client=mock_client,
        model="claude-sonnet-4-6",
        session_factory=session_factory,
        cost_guard_config=cost_guard_config,
        ws_manager=ws_manager,
    )


def _make_valid_card(workspace_id: str, chapter_id: str = "ch-0001") -> ChapterCard:
    """为非角色目标的 gate 测试提供可执行的完整章节卡。"""
    return ChapterCard(
        chapter_id=chapter_id,
        workspace_id=workspace_id,
        volume_id="v01",
        chapter_number=1,
        title="gate 测试章节",
        tension_level=5,
        word_count_min=1,
        word_count_max=10**8,
        plot_node_budget=1,
        must_achieve=[],
        must_unresolved=[],
        forbidden=[],
        pov_character="林衍",
        setting="测试场景",
        continuity_context=ContinuityContext(
            prev_chapter_tail="",
            unfinished_actions=[],
            time_gap="新场景",
            scene_continuation="switch",
            pov_change=False,
            pov_from=None,
            pov_to="lin-yan",
        ),
        next_chapter_seed="待续",
        arc_phase="rising",
        is_arc_finale=False,
        related_plot_threads=[],
        related_characters=["lin-yan"],
    )


def _make_workspace_row(workspace_id: str) -> WorkspaceORM:
    now = datetime.now(timezone.utc)
    return WorkspaceORM(
        workspace_id=workspace_id,
        display_name="gate test ws",
        genre="xianxia",
        premise="test",
        created_at=now,
        last_active_at=now,
        engine_version="0.1.0",
        status="active",
        total_chapters=0,
        completed_chapters=0,
        config_overrides={},
    )


def _insert_metrics_daily(
    session_factory, workspace_id: str, *, today_cost: float
) -> None:
    """落 metrics_daily 当日行(WorkspaceORM FK 先 sync)。

    v2 w2-s2 metrics 修复 B 起 cost gate **不再读本表**(改读 chapters
    累计账,见 ``_insert_chapter_cost``);保留本 helper 供
    ``test_metrics_daily_row_no_longer_drives_gate`` 反向断言旧读数源失效。
    """
    session = session_factory(workspace_id)
    try:
        session.merge(_make_workspace_row(workspace_id))
        today = datetime.now(timezone.utc).date()
        session.merge(
            MetricsDailyORM(
                date=today,
                chapters_drafted=0,
                chapters_approved=0,
                chapters_rewritten=0,
                chapters_revised=0,
                chapters_escalated=0,
                total_input_tokens=0,
                total_output_tokens=0,
                total_cost_usd=today_cost,
                qwen_calls=0,
                claude_sonnet_calls=0,
                claude_opus_calls=0,
            )
        )
        session.commit()
    finally:
        session.close()


def _insert_chapter_cost(
    session_factory,
    workspace_id: str,
    *,
    cost_usd: float,
    chapter_id: str = "ch-cost-0001",
    status: str = "human_intervention",
) -> None:
    """落 ``chapters`` 一行带 ``total_cost_usd``(WorkspaceORM FK 先 sync)。

    v2 w2-s2 metrics 修复 B:cost gate 读数源 = ``SUM(chapters.total_cost_usd)``
    工作区累计真值账。默认 status=human_intervention(孤儿章)— 正是修复
    要覆盖的"非 approved 章成本不该躲过护栏"语义(w2-s1 报告 §7.1 漏记①)。
    """
    session = session_factory(workspace_id)
    try:
        session.merge(_make_workspace_row(workspace_id))
        now = datetime.now(timezone.utc)
        session.merge(
            ChapterORM(
                chapter_id=chapter_id,
                workspace_id=workspace_id,
                current_version=1,
                status=status,
                rewrite_count=0,
                revise_count=0,
                created_at=now,
                approved_at=now if status in ("approved", "published") else None,
                total_token_usage={},
                total_cost_usd=cost_usd,
            )
        )
        session.commit()
    finally:
        session.close()


def _insert_memory_health(
    session_factory,
    workspace_id: str,
    *,
    severity: str,
    age_minutes: int = 1,
) -> str:
    """落 memory_health_history 一行(checked_at = now - age_minutes)。"""
    session = session_factory(workspace_id)
    check_id = f"mh-{uuid.uuid4().hex[:8]}"
    try:
        session.merge(_make_workspace_row(workspace_id))
        session.add(
            MemoryHealthCheckORM(
                check_id=check_id,
                checked_at=datetime.now(timezone.utc)
                - timedelta(minutes=age_minutes),
                severity=severity,
                stale_threads_count=0,
                inactive_characters_count=0,
                conflicts_count=0,
                report_json={"severity": severity, "issues": []},
            )
        )
        session.commit()
    finally:
        session.close()
    return check_id


# ---------------------------------------------------------------------------
# Tests
# ---------------------------------------------------------------------------


class TestRunGatesNoOp:
    """session_factory=None / 配置 None 时 gate 静默跳过(P1/P2/P3 兼容)。"""

    def test_no_session_factory_skips_all_gates(
        self,
        workspace_service: WorkspaceService,
        mock_client: MagicMock,
        prepared_workspace_id: str,
    ) -> None:
        service = _make_service(
            workspace_service=workspace_service,
            mock_client=mock_client,
            session_factory=None,
            cost_guard_config=CostGuardConfig(
                daily_usd_limit=0.01,
                monthly_usd_limit=0.01,
                per_workspace_daily_limit=0.01,
                action_on_exceed="pause",
                notify_via=["log"],
            ),
        )
        # 即便配了一个最严的 cost_guard,session_factory=None 时不读 DB,
        # gate 全 noop,不抛任何异常。
        service._run_gates(prepared_workspace_id)


class TestCostGuardGate:
    """cost_guard 维度(workspace_today_cost vs per_workspace_daily_limit)。"""

    def test_zero_cost_passes(
        self,
        workspace_service: WorkspaceService,
        mock_client: MagicMock,
        session_factory,
        prepared_workspace_id: str,
    ) -> None:
        service = _make_service(
            workspace_service=workspace_service,
            mock_client=mock_client,
            session_factory=session_factory,
            cost_guard_config=CostGuardConfig(
                daily_usd_limit=10.0,
                monthly_usd_limit=100.0,
                per_workspace_daily_limit=5.0,
                action_on_exceed="pause",
                notify_via=["log"],
            ),
        )
        # 无 chapters 行 → SUM=0.0 → 放行。
        service._run_gates(prepared_workspace_id)

    def test_under_limit_passes(
        self,
        workspace_service: WorkspaceService,
        mock_client: MagicMock,
        session_factory,
        prepared_workspace_id: str,
    ) -> None:
        _insert_chapter_cost(
            session_factory, prepared_workspace_id, cost_usd=0.5
        )
        service = _make_service(
            workspace_service=workspace_service,
            mock_client=mock_client,
            session_factory=session_factory,
            cost_guard_config=CostGuardConfig(
                daily_usd_limit=10.0,
                monthly_usd_limit=100.0,
                per_workspace_daily_limit=5.0,
                action_on_exceed="pause",
                notify_via=["log"],
            ),
        )
        service._run_gates(prepared_workspace_id)

    def test_over_limit_raises_cost_guard_triggered(
        self,
        workspace_service: WorkspaceService,
        mock_client: MagicMock,
        session_factory,
        prepared_workspace_id: str,
    ) -> None:
        _insert_chapter_cost(
            session_factory, prepared_workspace_id, cost_usd=0.1
        )
        service = _make_service(
            workspace_service=workspace_service,
            mock_client=mock_client,
            session_factory=session_factory,
            cost_guard_config=CostGuardConfig(
                daily_usd_limit=10.0,
                monthly_usd_limit=100.0,
                per_workspace_daily_limit=0.05,  # mock 严限
                action_on_exceed="pause",
                notify_via=["log"],
            ),
        )
        with pytest.raises(CostGuardTriggered) as exc_info:
            service._run_gates(prepared_workspace_id)
        err = exc_info.value
        assert err.code == "COST_GUARD_TRIGGERED"
        assert err.http_status == 402
        assert err.details is not None
        assert err.details["scope"] == "workspace_daily"
        assert err.details["current_usd"] == pytest.approx(0.1)
        assert err.details["limit_usd"] == pytest.approx(0.05)
        assert err.details["workspace_id"] == prepared_workspace_id

    def test_rmb_guard_uses_attempt_ledger_not_legacy_usd_projection(
        self,
        workspace_service: WorkspaceService,
        mock_client: MagicMock,
        session_factory,
        prepared_workspace_id: str,
    ) -> None:
        now = datetime.now(timezone.utc)
        with session_factory(prepared_workspace_id) as session:
            session.merge(_make_workspace_row(prepared_workspace_id))
            session.add(
                AttemptLedgerORM(
                    attempt_id="att-rmb-cost-guard",
                    workspace_id=prepared_workspace_id,
                    run_id="run-rmb",
                    task_id="task-rmb",
                    chapter_id=None,
                    attempt_kind="provider",
                    operation_kind="upstream_generation",
                    logical_call_id="call-rmb",
                    agent_name="outline_builder",
                    provider="openai_compatible",
                    model="qwen3.8-max",
                    attempt_number=1,
                    attempt_cap=10,
                    status="succeeded",
                    version_before=None,
                    version_after=None,
                    error_type=None,
                    usage_status="known",
                    input_tokens=1000,
                    output_tokens=1000,
                    reserved_input_tokens=1000,
                    reserved_output_tokens=1000,
                    configured_cost_currency="RMB",
                    configured_input_cost_per_1k=0.05,
                    configured_output_cost_per_1k=0.05,
                    configured_cost_amount=0.1,
                    configured_cost_basis="metered_usage",
                    provider_request_id="req-rmb",
                    response_status="completed",
                    created_at=now,
                    completed_at=now,
                )
            )
            session.commit()
        service = _make_service(
            workspace_service=workspace_service,
            mock_client=mock_client,
            session_factory=session_factory,
            cost_guard_config=CostGuardConfig(
                cost_currency="RMB",
                daily_usd_limit=10.0,
                monthly_usd_limit=100.0,
                per_workspace_daily_limit=0.05,
                action_on_exceed="pause",
                notify_via=["log"],
            ),
        )

        with pytest.raises(CostGuardTriggered) as exc_info:
            service._run_gates(prepared_workspace_id)

        assert exc_info.value.details is not None
        assert exc_info.value.details["current_usd"] == pytest.approx(0.1)
        assert exc_info.value.details["currency"] == "RMB"

    # ---- v2 w2-s2 metrics 修复 B 防回退(读数源 = chapters 累计真值账) ----

    def test_orphan_chapter_cost_counted_by_gate(
        self,
        workspace_service: WorkspaceService,
        mock_client: MagicMock,
        session_factory,
        prepared_workspace_id: str,
    ) -> None:
        """w2-s1 报告 §7.1 漏记①防回退:**孤儿章(human_intervention)成本
        必须被 gate 计入** — metrics_daily 永不入账该章($2.35/章现场),
        旧读数源下孤儿章烧钱对护栏隐身;chapters 真值账天然覆盖。"""
        _insert_chapter_cost(
            session_factory,
            prepared_workspace_id,
            cost_usd=6.0,
            status="human_intervention",  # 孤儿章:approved_at 为 None
        )
        service = _make_service(
            workspace_service=workspace_service,
            mock_client=mock_client,
            session_factory=session_factory,
            cost_guard_config=CostGuardConfig(
                daily_usd_limit=100.0,
                monthly_usd_limit=1000.0,
                per_workspace_daily_limit=5.0,
                action_on_exceed="pause",
                notify_via=["log"],
            ),
        )
        with pytest.raises(CostGuardTriggered):
            service._run_gates(prepared_workspace_id)

    def test_metrics_daily_row_no_longer_drives_gate(
        self,
        workspace_service: WorkspaceService,
        mock_client: MagicMock,
        session_factory,
        prepared_workspace_id: str,
    ) -> None:
        """反向断言读数源切换:metrics_daily 行有超限金额但 chapters 账为空
        → gate **不**触发(若本测试挂 = 有人把读数源改回 metrics_daily,
        必须先重审 w2-s1 报告 §7.1 的低估实证再动)。"""
        _insert_metrics_daily(
            session_factory, prepared_workspace_id, today_cost=999.0
        )
        service = _make_service(
            workspace_service=workspace_service,
            mock_client=mock_client,
            session_factory=session_factory,
            cost_guard_config=CostGuardConfig(
                daily_usd_limit=100.0,
                monthly_usd_limit=1000.0,
                per_workspace_daily_limit=5.0,
                action_on_exceed="pause",
                notify_via=["log"],
            ),
        )
        service._run_gates(prepared_workspace_id)

    def test_multi_chapter_costs_summed(
        self,
        workspace_service: WorkspaceService,
        mock_client: MagicMock,
        session_factory,
        prepared_workspace_id: str,
    ) -> None:
        """SUM 语义:多章成本累加(单章均低于限额、合计超限 → 触发)。"""
        _insert_chapter_cost(
            session_factory,
            prepared_workspace_id,
            cost_usd=3.0,
            chapter_id="ch-cost-0001",
            status="approved",
        )
        _insert_chapter_cost(
            session_factory,
            prepared_workspace_id,
            cost_usd=3.0,
            chapter_id="ch-cost-0002",
            status="human_intervention",
        )
        service = _make_service(
            workspace_service=workspace_service,
            mock_client=mock_client,
            session_factory=session_factory,
            cost_guard_config=CostGuardConfig(
                daily_usd_limit=100.0,
                monthly_usd_limit=1000.0,
                per_workspace_daily_limit=5.0,
                action_on_exceed="pause",
                notify_via=["log"],
            ),
        )
        with pytest.raises(CostGuardTriggered) as exc_info:
            service._run_gates(prepared_workspace_id)
        assert exc_info.value.details is not None
        assert exc_info.value.details["current_usd"] == pytest.approx(6.0)


class TestMemoryHealthGate:
    """memory_health 维度(读 history 最新行 severity)。"""

    def test_no_history_passes(
        self,
        workspace_service: WorkspaceService,
        mock_client: MagicMock,
        session_factory,
        prepared_workspace_id: str,
    ) -> None:
        service = _make_service(
            workspace_service=workspace_service,
            mock_client=mock_client,
            session_factory=session_factory,
            cost_guard_config=None,
        )
        service._run_gates(prepared_workspace_id)

    def test_warning_severity_passes(
        self,
        workspace_service: WorkspaceService,
        mock_client: MagicMock,
        session_factory,
        prepared_workspace_id: str,
    ) -> None:
        _insert_memory_health(
            session_factory, prepared_workspace_id, severity="warning"
        )
        service = _make_service(
            workspace_service=workspace_service,
            mock_client=mock_client,
            session_factory=session_factory,
            cost_guard_config=None,
        )
        service._run_gates(prepared_workspace_id)

    def test_critical_severity_raises_memory_health_blocked(
        self,
        workspace_service: WorkspaceService,
        mock_client: MagicMock,
        session_factory,
        prepared_workspace_id: str,
    ) -> None:
        check_id = _insert_memory_health(
            session_factory, prepared_workspace_id, severity="critical"
        )
        service = _make_service(
            workspace_service=workspace_service,
            mock_client=mock_client,
            session_factory=session_factory,
            cost_guard_config=None,
        )
        with pytest.raises(MemoryHealthBlocked) as exc_info:
            service._run_gates(prepared_workspace_id)
        err = exc_info.value
        assert err.code == "MEMORY_HEALTH_BLOCKED"
        assert err.http_status == 409
        assert err.details is not None
        assert err.details["check_id"] == check_id
        assert err.details["severity"] == "critical"
        assert err.details["workspace_id"] == prepared_workspace_id


class TestGatePriority:
    """两 gate 都触发时 cost_guard 优先抛(代码顺序:cost_guard 先于 memory_health)。"""

    def test_cost_guard_takes_priority_over_memory_health(
        self,
        workspace_service: WorkspaceService,
        mock_client: MagicMock,
        session_factory,
        prepared_workspace_id: str,
    ) -> None:
        _insert_chapter_cost(
            session_factory, prepared_workspace_id, cost_usd=10.0
        )
        _insert_memory_health(
            session_factory, prepared_workspace_id, severity="critical"
        )
        service = _make_service(
            workspace_service=workspace_service,
            mock_client=mock_client,
            session_factory=session_factory,
            cost_guard_config=CostGuardConfig(
                daily_usd_limit=100.0,
                monthly_usd_limit=1000.0,
                per_workspace_daily_limit=1.0,
                action_on_exceed="pause",
                notify_via=["log"],
            ),
        )
        with pytest.raises(CostGuardTriggered):
            service._run_gates(prepared_workspace_id)


# ---------------------------------------------------------------------------
# P5-B-06:card_status gate
# ---------------------------------------------------------------------------


def _insert_chapter_card_orm(
    session_factory,
    workspace_id: str,
    chapter_id: str,
    *,
    card_status: str,
) -> None:
    """落 ``chapter_cards`` 一行(WorkspaceORM FK 先 sync)。"""
    session = session_factory(workspace_id)
    try:
        session.merge(_make_workspace_row(workspace_id))
        session.add(
            ChapterCardORM(
                chapter_id=chapter_id,
                workspace_id=workspace_id,
                card_status=card_status,
                created_at=datetime.now(timezone.utc),
            )
        )
        session.commit()
    finally:
        session.close()


class TestCardStatusGate:
    """P5-B-06 决策 6=C:``card_status != "approved"`` → 抛 ``CardNotApproved`` (409)。"""

    def test_no_chapter_id_skips_card_gate(
        self,
        workspace_service: WorkspaceService,
        mock_client: MagicMock,
        session_factory,
        prepared_workspace_id: str,
    ) -> None:
        """``chapter_id=None`` 时 card gate 跳过(向后兼容非章节级调用方)。"""
        service = _make_service(
            workspace_service=workspace_service,
            mock_client=mock_client,
            session_factory=session_factory,
        )
        # 不传 chapter_id;ChapterCardORM 表为空也不应抛(legacy 兼容路径)。
        service._run_gates(prepared_workspace_id)

    def test_legacy_file_card_passes(
        self,
        workspace_service: WorkspaceService,
        mock_client: MagicMock,
        session_factory,
        prepared_workspace_id: str,
    ) -> None:
        """P1-P4 向后兼容:ORM 行不存在但 ``data/cards/<id>.json`` 存在 → 视为 approved 放行。"""
        service = _make_service(
            workspace_service=workspace_service,
            mock_client=mock_client,
            session_factory=session_factory,
        )
        service.save_chapter_card(
            prepared_workspace_id, _make_valid_card(prepared_workspace_id)
        )
        service._run_gates(prepared_workspace_id, chapter_id="ch-0001")

    def test_no_orm_row_no_file_raises_card_not_approved(
        self,
        workspace_service: WorkspaceService,
        mock_client: MagicMock,
        session_factory,
        prepared_workspace_id: str,
    ) -> None:
        """ORM 行不存在 + 文件不存在 → 抛 ``CardNotApproved`` (P5+ 新流程)。"""
        service = _make_service(
            workspace_service=workspace_service,
            mock_client=mock_client,
            session_factory=session_factory,
        )
        with pytest.raises(CardNotApproved) as exc_info:
            service._run_gates(prepared_workspace_id, chapter_id="ch-0042")
        assert exc_info.value.details is not None
        assert exc_info.value.details["chapter_id"] == "ch-0042"
        assert exc_info.value.details["card_status"] is None

    def test_orm_pending_review_raises_card_not_approved(
        self,
        workspace_service: WorkspaceService,
        mock_client: MagicMock,
        session_factory,
        prepared_workspace_id: str,
    ) -> None:
        _insert_chapter_card_orm(
            session_factory,
            prepared_workspace_id,
            "ch-0001",
            card_status="pending_review",
        )
        service = _make_service(
            workspace_service=workspace_service,
            mock_client=mock_client,
            session_factory=session_factory,
        )
        with pytest.raises(CardNotApproved) as exc_info:
            service._run_gates(prepared_workspace_id, chapter_id="ch-0001")
        assert exc_info.value.details is not None
        assert exc_info.value.details["card_status"] == "pending_review"

    def test_orm_draft_raises_card_not_approved(
        self,
        workspace_service: WorkspaceService,
        mock_client: MagicMock,
        session_factory,
        prepared_workspace_id: str,
    ) -> None:
        _insert_chapter_card_orm(
            session_factory,
            prepared_workspace_id,
            "ch-0001",
            card_status="draft",
        )
        service = _make_service(
            workspace_service=workspace_service,
            mock_client=mock_client,
            session_factory=session_factory,
        )
        with pytest.raises(CardNotApproved):
            service._run_gates(prepared_workspace_id, chapter_id="ch-0001")

    def test_orm_rejected_raises_card_not_approved(
        self,
        workspace_service: WorkspaceService,
        mock_client: MagicMock,
        session_factory,
        prepared_workspace_id: str,
    ) -> None:
        _insert_chapter_card_orm(
            session_factory,
            prepared_workspace_id,
            "ch-0001",
            card_status="rejected",
        )
        service = _make_service(
            workspace_service=workspace_service,
            mock_client=mock_client,
            session_factory=session_factory,
        )
        with pytest.raises(CardNotApproved):
            service._run_gates(prepared_workspace_id, chapter_id="ch-0001")

    def test_orm_approved_passes(
        self,
        workspace_service: WorkspaceService,
        mock_client: MagicMock,
        session_factory,
        prepared_workspace_id: str,
    ) -> None:
        _insert_chapter_card_orm(
            session_factory,
            prepared_workspace_id,
            "ch-0001",
            card_status="approved",
        )
        service = _make_service(
            workspace_service=workspace_service,
            mock_client=mock_client,
            session_factory=session_factory,
        )
        service.save_chapter_card(
            prepared_workspace_id, _make_valid_card(prepared_workspace_id)
        )
        service._run_gates(prepared_workspace_id, chapter_id="ch-0001")
