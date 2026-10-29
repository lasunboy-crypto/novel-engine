"""metrics 路由集成测试(P4-B-04)。

覆盖 OBSERVABILITY §3.2 字面 7 个工作区端点 + 1 个系统级端点的契约:

- ``GET /workspaces/{id}/metrics/overview``
- ``GET /workspaces/{id}/metrics/daily?since=&until=``
- ``GET /workspaces/{id}/metrics/per-chapter?order=``
- ``GET /workspaces/{id}/metrics/tension-curve``
- ``GET /workspaces/{id}/metrics/cost-breakdown``
- ``GET /workspaces/{id}/metrics/memory-health``
- ``GET /workspaces/{id}/metrics/style-audits``
- ``GET /system/metrics``

测试矩阵
========

主路径(填 mock metrics 数据 → 端点正确返序列化)
- 200 + data + 字段值与种子数据吻合
- 空表 → 200 + 空数组 / 0 字段(语义"还没到 APPROVED")
- 工作区不存在 → 404 ``WORKSPACE_NOT_FOUND``

测试基础设施
============
- ``app.dependency_overrides[get_metrics_service]`` 注入测 service(指向
  ``tmp_path``);system_session_factory 也走 tmp_path。
"""

from __future__ import annotations

import datetime as _dt
from collections.abc import Iterator
from datetime import datetime, timezone
from pathlib import Path

import pytest
from fastapi.testclient import TestClient
from sqlalchemy import create_engine
from sqlalchemy.orm import Session

from src.api.main import create_app
from src.api.routes.metrics import get_metrics_service
from src.core.db import Base
from src.core.db.models import (
    MemoryHealthCheckORM,
    MetricsDailyORM,
    MetricsPerChapterORM,
    StyleAuditEntryORM,
    SystemMetricsDailyORM,
)
from src.core.workspace.workspace_paths import WorkspacePaths
from src.services.metrics_service import MetricsService
from src.services.workspace_service import WorkspaceService


# ---- fixtures --------------------------------------------------------------


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
        "ws-routes",
        display_name="P4-B-04 metrics routes 集成测试",
        genre="test",
        premise="x",
        total_chapters=3,
    )
    return "ws-routes"


@pytest.fixture
def system_engine(tmp_path: Path):
    engine = create_engine(f"sqlite:///{tmp_path}/_system_metrics.db")
    Base.metadata.create_all(engine)
    return engine


@pytest.fixture
def session_factory(workspace_service: WorkspaceService):
    def _factory(workspace_id: str) -> Session:
        paths = WorkspacePaths(
            workspace_id, workspaces_root=workspace_service.workspaces_root
        )
        engine = create_engine(f"sqlite:///{paths.db_path}")
        Base.metadata.create_all(engine)
        return Session(engine)

    return _factory


@pytest.fixture
def system_session_factory(system_engine):
    def _factory() -> Session:
        return Session(system_engine)

    return _factory


@pytest.fixture
def metrics_service(
    workspace_service: WorkspaceService,
    session_factory,
    system_session_factory,
) -> MetricsService:
    return MetricsService(
        workspace_service=workspace_service,
        session_factory=session_factory,
        system_session_factory=system_session_factory,
    )


@pytest.fixture
def client(metrics_service: MetricsService) -> Iterator[TestClient]:
    app = create_app()
    app.dependency_overrides[get_metrics_service] = lambda: metrics_service
    with TestClient(app) as c:
        yield c
    app.dependency_overrides.clear()


# ---- 数据 seed helper ------------------------------------------------------


def _seed_per_chapter(
    session: Session,
    chapter_id: str,
    *,
    approved_at: datetime,
    final_score: float = 8.5,
    rewrite_count: int = 0,
    revise_count: int = 0,
    total_cost_usd: float = 0.05,
    total_tokens: int = 1000,
    writer_model: str = "claude-sonnet-4-6",
    writer_calls: int = 1,
    target_tension_level: int = 6,
    actual_tension_estimate: float | None = None,
) -> None:
    session.add(
        MetricsPerChapterORM(
            chapter_id=chapter_id,
            drafted_at=approved_at - _dt.timedelta(seconds=30),
            approved_at=approved_at,
            total_latency_seconds=30.0,
            rewrite_count=rewrite_count,
            revise_count=revise_count,
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
            target_tension_level=target_tension_level,
            actual_tension_estimate=actual_tension_estimate,
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


def _seed_daily(
    session: Session,
    date: _dt.date,
    *,
    chapters_approved: int = 1,
    avg_review_score: float | None = 8.5,
    total_cost_usd: float = 0.1,
) -> None:
    session.add(
        MetricsDailyORM(
            date=date,
            chapters_drafted=chapters_approved,
            chapters_approved=chapters_approved,
            chapters_rewritten=0,
            chapters_revised=0,
            chapters_escalated=0,
            avg_review_score=avg_review_score,
            avg_rewrites_per_chapter=0.0,
            avg_revises_per_chapter=0.0,
            total_input_tokens=2000,
            total_output_tokens=0,
            total_cost_usd=total_cost_usd,
            avg_chapter_latency_seconds=30.0,
            qwen_calls=0,
            claude_sonnet_calls=chapters_approved,
            claude_opus_calls=0,
        )
    )


# ---- 测试 ------------------------------------------------------------------


class TestOverview:
    def test_empty_workspace_returns_zeros(
        self, client: TestClient, workspace_id: str
    ) -> None:
        resp = client.get(f"/api/v1/workspaces/{workspace_id}/metrics/overview")
        assert resp.status_code == 200
        body = resp.json()
        assert "data" in body and "error" not in body
        d = body["data"]
        assert d["workspace_id"] == workspace_id
        assert d["chapters_approved"] == 0
        assert d["total_cost_usd"] == 0.0
        assert d["avg_review_score"] is None

    def test_with_per_chapter_rows(
        self,
        client: TestClient,
        workspace_id: str,
        session_factory,
    ) -> None:
        approved = datetime(2026, 5, 10, 12, 0, 0, tzinfo=timezone.utc)
        with session_factory(workspace_id) as s:
            _seed_per_chapter(
                s, "ch-0001", approved_at=approved, final_score=8.0,
                total_cost_usd=0.1, total_tokens=1000,
            )
            _seed_per_chapter(
                s, "ch-0002", approved_at=approved, final_score=9.0,
                total_cost_usd=0.2, total_tokens=2000,
            )
            s.commit()
        resp = client.get(f"/api/v1/workspaces/{workspace_id}/metrics/overview")
        assert resp.status_code == 200
        d = resp.json()["data"]
        assert d["chapters_approved"] == 2
        assert d["total_cost_usd"] == pytest.approx(0.3)
        assert d["total_tokens"] == 3000
        assert d["avg_review_score"] == pytest.approx(8.5)

    def test_workspace_not_found_returns_error(self, client: TestClient) -> None:
        """工作区不存在 / meta.json 缺失 → 4xx + WORKSPACE_* 错误码。

        ``WorkspaceService.info()`` 缺 meta.json 抛 ``MetaFileError`` →
        ``api/errors.py`` 全局翻译为 ``WORKSPACE_INVALID (422)``;若上游
        改路径调 ``ensure_workspace_orm`` 等抛 ``WorkspaceServiceError``
        会变 ``WORKSPACE_NOT_FOUND (404)``。两种语义都符合"用户可见的工作区
        不可达",本测试接受任一档(对齐 P3-B-10 ``test_config_routes`` 模式)。
        """
        resp = client.get("/api/v1/workspaces/not-exist/metrics/overview")
        assert resp.status_code in {404, 422}
        body = resp.json()
        assert body["error"]["code"] in {
            "WORKSPACE_INVALID",
            "WORKSPACE_NOT_FOUND",
        }


class TestDaily:
    def test_empty_returns_empty_array(
        self, client: TestClient, workspace_id: str
    ) -> None:
        resp = client.get(f"/api/v1/workspaces/{workspace_id}/metrics/daily")
        assert resp.status_code == 200
        assert resp.json()["data"]["items"] == []

    def test_range_filter(
        self,
        client: TestClient,
        workspace_id: str,
        session_factory,
    ) -> None:
        with session_factory(workspace_id) as s:
            _seed_daily(s, _dt.date(2026, 5, 8), chapters_approved=1)
            _seed_daily(s, _dt.date(2026, 5, 9), chapters_approved=2)
            _seed_daily(s, _dt.date(2026, 5, 10), chapters_approved=3)
            s.commit()
        resp = client.get(
            f"/api/v1/workspaces/{workspace_id}/metrics/daily"
            "?since=2026-05-09&until=2026-05-10"
        )
        assert resp.status_code == 200
        items = resp.json()["data"]["items"]
        assert [i["date"] for i in items] == ["2026-05-09", "2026-05-10"]
        assert items[0]["chapters_approved"] == 2
        assert items[1]["chapters_approved"] == 3

    def test_unbounded_returns_all(
        self,
        client: TestClient,
        workspace_id: str,
        session_factory,
    ) -> None:
        with session_factory(workspace_id) as s:
            _seed_daily(s, _dt.date(2026, 5, 8))
            _seed_daily(s, _dt.date(2026, 5, 9))
            s.commit()
        resp = client.get(f"/api/v1/workspaces/{workspace_id}/metrics/daily")
        assert resp.status_code == 200
        assert len(resp.json()["data"]["items"]) == 2


class TestPerChapter:
    def test_empty_returns_empty_array(
        self, client: TestClient, workspace_id: str
    ) -> None:
        resp = client.get(
            f"/api/v1/workspaces/{workspace_id}/metrics/per-chapter"
        )
        assert resp.status_code == 200
        assert resp.json()["data"]["items"] == []

    def test_default_order_desc(
        self,
        client: TestClient,
        workspace_id: str,
        session_factory,
    ) -> None:
        with session_factory(workspace_id) as s:
            _seed_per_chapter(
                s, "ch-0001",
                approved_at=datetime(2026, 5, 8, tzinfo=timezone.utc),
            )
            _seed_per_chapter(
                s, "ch-0002",
                approved_at=datetime(2026, 5, 10, tzinfo=timezone.utc),
            )
            _seed_per_chapter(
                s, "ch-0003",
                approved_at=datetime(2026, 5, 9, tzinfo=timezone.utc),
            )
            s.commit()
        resp = client.get(
            f"/api/v1/workspaces/{workspace_id}/metrics/per-chapter"
        )
        items = resp.json()["data"]["items"]
        assert [i["chapter_id"] for i in items] == ["ch-0002", "ch-0003", "ch-0001"]

    def test_ascending_order(
        self,
        client: TestClient,
        workspace_id: str,
        session_factory,
    ) -> None:
        with session_factory(workspace_id) as s:
            _seed_per_chapter(
                s, "ch-0001",
                approved_at=datetime(2026, 5, 10, tzinfo=timezone.utc),
            )
            _seed_per_chapter(
                s, "ch-0002",
                approved_at=datetime(2026, 5, 8, tzinfo=timezone.utc),
            )
            s.commit()
        resp = client.get(
            f"/api/v1/workspaces/{workspace_id}/metrics/per-chapter"
            "?order=approved_at_asc"
        )
        items = resp.json()["data"]["items"]
        assert [i["chapter_id"] for i in items] == ["ch-0002", "ch-0001"]


class TestTensionCurve:
    def test_returns_target_and_actual_per_chapter(
        self,
        client: TestClient,
        workspace_id: str,
        session_factory,
    ) -> None:
        with session_factory(workspace_id) as s:
            _seed_per_chapter(
                s, "ch-0001",
                approved_at=datetime(2026, 5, 8, tzinfo=timezone.utc),
                target_tension_level=4,
                actual_tension_estimate=None,
            )
            _seed_per_chapter(
                s, "ch-0002",
                approved_at=datetime(2026, 5, 10, tzinfo=timezone.utc),
                target_tension_level=8,
                actual_tension_estimate=7.5,
            )
            s.commit()
        resp = client.get(
            f"/api/v1/workspaces/{workspace_id}/metrics/tension-curve"
        )
        points = resp.json()["data"]["points"]
        assert [p["chapter_id"] for p in points] == ["ch-0001", "ch-0002"]
        assert points[0]["target_tension_level"] == 4
        assert points[0]["actual_tension_estimate"] is None
        assert points[1]["actual_tension_estimate"] == pytest.approx(7.5)


class TestCostBreakdown:
    def test_groups_by_writer_model(
        self,
        client: TestClient,
        workspace_id: str,
        session_factory,
    ) -> None:
        approved = datetime(2026, 5, 10, tzinfo=timezone.utc)
        with session_factory(workspace_id) as s:
            _seed_per_chapter(
                s, "ch-0001",
                approved_at=approved,
                writer_model="qwen-novel",
                total_cost_usd=0.0,
                total_tokens=5000,
            )
            _seed_per_chapter(
                s, "ch-0002",
                approved_at=approved,
                writer_model="qwen-novel",
                total_cost_usd=0.0,
                total_tokens=3000,
            )
            _seed_per_chapter(
                s, "ch-0003",
                approved_at=approved,
                writer_model="claude-sonnet-4-6",
                total_cost_usd=0.5,
                total_tokens=10000,
            )
            s.commit()
        resp = client.get(
            f"/api/v1/workspaces/{workspace_id}/metrics/cost-breakdown"
        )
        buckets = resp.json()["data"]["buckets"]
        # 按 writer_model asc 排序
        assert [b["writer_model"] for b in buckets] == [
            "claude-sonnet-4-6",
            "qwen-novel",
        ]
        claude_bucket = buckets[0]
        qwen_bucket = buckets[1]
        assert claude_bucket["chapters_count"] == 1
        assert claude_bucket["total_cost_usd"] == pytest.approx(0.5)
        assert claude_bucket["total_tokens"] == 10000
        assert qwen_bucket["chapters_count"] == 2
        assert qwen_bucket["total_cost_usd"] == 0.0
        assert qwen_bucket["total_tokens"] == 8000


class TestMemoryHealth:
    def test_empty_returns_empty(
        self, client: TestClient, workspace_id: str
    ) -> None:
        resp = client.get(
            f"/api/v1/workspaces/{workspace_id}/metrics/memory-health"
        )
        assert resp.status_code == 200
        assert resp.json()["data"]["items"] == []

    def test_returns_desc_by_checked_at(
        self,
        client: TestClient,
        workspace_id: str,
        session_factory,
    ) -> None:
        with session_factory(workspace_id) as s:
            s.add(
                MemoryHealthCheckORM(
                    check_id="check-001",
                    checked_at=datetime(2026, 5, 8, tzinfo=timezone.utc),
                    severity="ok",
                    stale_threads_count=0,
                    inactive_characters_count=0,
                    conflicts_count=0,
                    report_json={},
                )
            )
            s.add(
                MemoryHealthCheckORM(
                    check_id="check-002",
                    checked_at=datetime(2026, 5, 10, tzinfo=timezone.utc),
                    severity="warning",
                    stale_threads_count=2,
                    inactive_characters_count=1,
                    conflicts_count=0,
                    report_json={"stale": ["thread-x"]},
                )
            )
            s.commit()
        resp = client.get(
            f"/api/v1/workspaces/{workspace_id}/metrics/memory-health"
        )
        items = resp.json()["data"]["items"]
        # checked_at desc → check-002 在前
        assert [i["check_id"] for i in items] == ["check-002", "check-001"]
        assert items[0]["severity"] == "warning"


class TestStyleAudits:
    def test_empty_returns_empty(
        self, client: TestClient, workspace_id: str
    ) -> None:
        resp = client.get(
            f"/api/v1/workspaces/{workspace_id}/metrics/style-audits"
        )
        assert resp.status_code == 200
        assert resp.json()["data"]["items"] == []

    def test_returns_desc_by_audited_at(
        self,
        client: TestClient,
        workspace_id: str,
        session_factory,
    ) -> None:
        with session_factory(workspace_id) as s:
            s.add(
                StyleAuditEntryORM(
                    audit_id="audit-001",
                    audited_at=datetime(2026, 5, 8, tzinfo=timezone.utc),
                    chapter_range_start="ch-0001",
                    chapter_range_end="ch-0010",
                    drift_score=1.0,
                    severity="ok",
                    report_json={},
                )
            )
            s.add(
                StyleAuditEntryORM(
                    audit_id="audit-002",
                    audited_at=datetime(2026, 5, 10, tzinfo=timezone.utc),
                    chapter_range_start="ch-0011",
                    chapter_range_end="ch-0020",
                    drift_score=4.0,
                    severity="minor",
                    report_json={},
                )
            )
            s.commit()
        resp = client.get(
            f"/api/v1/workspaces/{workspace_id}/metrics/style-audits"
        )
        items = resp.json()["data"]["items"]
        assert [i["audit_id"] for i in items] == ["audit-002", "audit-001"]


class TestSystemMetrics:
    def test_empty_system_db_returns_zeros_today(
        self, client: TestClient
    ) -> None:
        """System db 空 → 今天行 counts=0,history 空。"""
        resp = client.get("/api/v1/system/metrics")
        assert resp.status_code == 200
        d = resp.json()["data"]
        assert d["today"]["api_requests"] == 0
        assert d["today"]["api_errors"] == 0
        assert d["today"]["avg_api_latency_ms"] is None
        # history 可能含本测试运行过程中累加的当日行(依测试执行顺序)
        # 但绝不会含其他日期(空 db 启动)
        for h in d["history"]:
            assert isinstance(h["date"], str)

    def test_system_db_with_seeded_today(
        self, client: TestClient, system_engine
    ) -> None:
        today = _dt.datetime.now(_dt.timezone.utc).date()
        with Session(system_engine) as s:
            s.add(
                SystemMetricsDailyORM(
                    date=today,
                    api_requests=42,
                    api_errors=3,
                    avg_api_latency_ms=15.5,
                    ws_connections=2,
                    frontend_errors=0,
                    pipeline_runs=5,
                )
            )
            s.commit()
        resp = client.get("/api/v1/system/metrics")
        assert resp.status_code == 200
        d = resp.json()["data"]
        assert d["today"]["api_requests"] == 42
        assert d["today"]["api_errors"] == 3
        assert d["today"]["avg_api_latency_ms"] == pytest.approx(15.5)
        assert d["today"]["pipeline_runs"] == 5
