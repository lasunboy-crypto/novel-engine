"""验证 P4-B-01 alembic 0004 迁移与 5 张 metrics 表 ORM / Pydantic 模型。

P4-B-01 引入 5 张表 ``metrics_daily`` / ``metrics_per_chapter`` /
``system_metrics_daily`` / ``memory_health_history`` / ``style_audit_history``
对应 OBSERVABILITY §3.1 字面 ``CREATE TABLE``。

覆盖维度(沿用 ``test_p2_models.py`` / ``test_p2_b17_chapters_state.py`` 同结构):

- upgrade head 后 13 张业务表(P1 5 + P2 3 + P4 5)+ 索引齐全
- downgrade -1 干净退回 0003_chapters_state_7_levels(只删 P4 5 表)
- upgrade → downgrade -1 → upgrade 幂等
- ORM round-trip:Pydantic ↔ ORM ↔ SQLite 字段一致(5 模型各覆盖一例)
- ``memory_health_history.severity`` CHECK 约束:4 档允许写,非档值
  ``IntegrityError``
- ``metrics_per_chapter`` 决策 5 字段(``cache_input_tokens`` /
  ``cache_hit_ratio``)默认 ``None`` 可写;给值时正常落库
"""

from __future__ import annotations

import sqlite3
from datetime import date, datetime, timezone
from pathlib import Path

import pytest
from alembic import command
from alembic.config import Config
from sqlalchemy import create_engine, inspect, select, text
from sqlalchemy.exc import IntegrityError
from sqlalchemy.orm import Session

from src.core.db import (
    MEMORY_HEALTH_SEVERITY_VALUES,
    STYLE_AUDIT_SEVERITY_VALUES,
    MemoryHealthCheckORM,
    MetricsDailyORM,
    MetricsPerChapterORM,
    StyleAuditEntryORM,
    SystemMetricsDailyORM,
)
from src.core.models.metrics import (
    MemoryHealthCheck,
    MetricsDaily,
    MetricsPerChapter,
    StyleAuditEntry,
    SystemMetricsDaily,
)


_REPO_ROOT = Path(__file__).resolve().parents[2]


def _alembic_config() -> Config:
    cfg = Config(str(_REPO_ROOT / "alembic.ini"))
    cfg.set_main_option("script_location", str(_REPO_ROOT / "alembic"))
    return cfg


@pytest.fixture
def tmp_db_path(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> Path:
    db_path = tmp_path / "novel.db"
    monkeypatch.setenv("NOVEL_DB_PATH", str(db_path))
    return db_path


def _table_names(db_path: Path) -> list[str]:
    con = sqlite3.connect(str(db_path))
    try:
        rows = con.execute(
            "SELECT name FROM sqlite_master WHERE type='table' ORDER BY name"
        ).fetchall()
    finally:
        con.close()
    return [r[0] for r in rows]


def _index_names(db_path: Path) -> list[str]:
    con = sqlite3.connect(str(db_path))
    try:
        rows = con.execute(
            "SELECT name FROM sqlite_master WHERE type='index' "
            "AND name NOT LIKE 'sqlite_%' ORDER BY name"
        ).fetchall()
    finally:
        con.close()
    return [r[0] for r in rows]


P4_TABLES = {
    "metrics_daily",
    "metrics_per_chapter",
    "system_metrics_daily",
    "memory_health_history",
    "style_audit_history",
}

P4_INDEXES = {
    "ix_memory_health_history_checked_at",
    "ix_style_audit_history_audited_at",
}


class TestAlembicMigration:
    """alembic 0004 schema 层面验证。"""

    def test_upgrade_head_creates_p4_tables(self, tmp_db_path: Path) -> None:
        cfg = _alembic_config()
        command.upgrade(cfg, "head")
        tables = set(_table_names(tmp_db_path))
        assert P4_TABLES.issubset(tables)
        # P1 / P2 表仍在
        assert {
            "workspaces",
            "chapters",
            "chapter_versions",
            "review_results",
            "review_issues",
            "characters",
            "plot_threads",
            "timeline_entries",
        }.issubset(tables)

    def test_upgrade_head_creates_p4_indexes(self, tmp_db_path: Path) -> None:
        cfg = _alembic_config()
        command.upgrade(cfg, "head")
        indexes = set(_index_names(tmp_db_path))
        assert P4_INDEXES.issubset(indexes)

    def test_downgrade_minus_one_drops_only_p4_tables(
        self, tmp_db_path: Path
    ) -> None:
        cfg = _alembic_config()
        # 显式 upgrade 到 0004(本测试验证 P4-B-01 自身 round-trip,而非
        # 任意后续 head);P5-B-01 加入 0005 后 head != 0004,继续用 "head"
        # 会导致 downgrade -1 退到 0004 而非 0003。沿用 test_p2_models /
        # test_p2_b17_chapters_state 显式 revision 模式(P4-B-01 沉淀纪律,
        # P5-B-01 实施时第 2 处证据)。
        command.upgrade(cfg, "0004_p4_metrics_tables")
        command.downgrade(cfg, "-1")
        tables = set(_table_names(tmp_db_path))
        # P4 表全消失
        assert tables.isdisjoint(P4_TABLES)
        # P1 + P2 表保留
        assert {
            "workspaces",
            "chapters",
            "chapter_versions",
            "review_results",
            "review_issues",
            "characters",
            "plot_threads",
            "timeline_entries",
        }.issubset(tables)

    def test_upgrade_downgrade_upgrade_idempotent(
        self, tmp_db_path: Path
    ) -> None:
        cfg = _alembic_config()
        command.upgrade(cfg, "head")
        command.downgrade(cfg, "-1")
        command.upgrade(cfg, "head")
        tables = set(_table_names(tmp_db_path))
        assert P4_TABLES.issubset(tables)

    def test_p4_columns_match_orm(self, tmp_db_path: Path) -> None:
        """alembic 0004 各表列名与 ORM ``__table__.columns`` 一致。"""
        cfg = _alembic_config()
        command.upgrade(cfg, "head")
        engine = create_engine(f"sqlite:///{tmp_db_path}")
        inspector = inspect(engine)

        for orm_cls in (
            MetricsDailyORM,
            MetricsPerChapterORM,
            SystemMetricsDailyORM,
            MemoryHealthCheckORM,
            StyleAuditEntryORM,
        ):
            db_cols = {c["name"] for c in inspector.get_columns(orm_cls.__tablename__)}
            orm_cols = {c.name for c in orm_cls.__table__.columns}
            assert db_cols == orm_cols, (
                f"{orm_cls.__tablename__} columns mismatch: "
                f"db={db_cols} orm={orm_cols}"
            )


class TestMemoryHealthSeverityCheck:
    """``memory_health_history.severity`` 4 档 CHECK 约束。"""

    @pytest.fixture
    def session(self, tmp_db_path: Path) -> Session:
        command.upgrade(_alembic_config(), "head")
        engine = create_engine(f"sqlite:///{tmp_db_path}")
        # SQLite 默认不强制 CHECK 与 FK,但 SQLAlchemy 默认连接 PRAGMA
        # foreign_keys=ON 不开 CHECK;CHECK 在 SQLite 默认就强制(自 3.6+)。
        with Session(engine) as session:
            yield session

    @pytest.mark.parametrize("severity", MEMORY_HEALTH_SEVERITY_VALUES)
    def test_each_allowed_severity_writes(
        self, session: Session, severity: str
    ) -> None:
        now = datetime.now(timezone.utc)
        session.add(
            MemoryHealthCheckORM(
                check_id=f"chk_{severity}",
                checked_at=now,
                severity=severity,
                stale_threads_count=0,
                inactive_characters_count=0,
                conflicts_count=0,
                report_json={},
            )
        )
        session.commit()
        loaded = session.get(MemoryHealthCheckORM, f"chk_{severity}")
        assert loaded is not None
        assert loaded.severity == severity

    def test_disallowed_severity_raises(self, session: Session) -> None:
        now = datetime.now(timezone.utc)
        session.add(
            MemoryHealthCheckORM(
                check_id="chk_bad",
                checked_at=now,
                severity="catastrophic",
                stale_threads_count=0,
                inactive_characters_count=0,
                conflicts_count=0,
                report_json={},
            )
        )
        with pytest.raises(IntegrityError):
            session.commit()


class TestStyleAuditSeverityCheck:
    """``style_audit_history.severity`` 4 档 CHECK 约束(v1.1 w4-s5 / I2.2,
    0018 迁移加;旧 stub 行 upgrade 时 fold → ok)。"""

    @pytest.fixture
    def session(self, tmp_db_path: Path) -> Session:
        command.upgrade(_alembic_config(), "head")
        engine = create_engine(f"sqlite:///{tmp_db_path}")
        with Session(engine) as session:
            yield session

    @pytest.mark.parametrize("severity", STYLE_AUDIT_SEVERITY_VALUES)
    def test_each_allowed_severity_writes(
        self, session: Session, severity: str
    ) -> None:
        now = datetime.now(timezone.utc)
        session.add(
            StyleAuditEntryORM(
                audit_id=f"sty_{severity}",
                audited_at=now,
                drift_score=1.0,
                severity=severity,
                report_json={},
            )
        )
        session.commit()
        loaded = session.get(StyleAuditEntryORM, f"sty_{severity}")
        assert loaded is not None
        assert loaded.severity == severity

    def test_disallowed_severity_raises(self, session: Session) -> None:
        # 旧 stub 值现在被 CHECK 约束拒绝(0018 已 fold 历史行 → ok)。
        now = datetime.now(timezone.utc)
        session.add(
            StyleAuditEntryORM(
                audit_id="sty_bad",
                audited_at=now,
                drift_score=None,
                severity="stub",
                report_json={},
            )
        )
        with pytest.raises(IntegrityError):
            session.commit()

    def test_0018_folds_legacy_stub_to_ok(self, tmp_db_path: Path) -> None:
        """0018 upgrade:0017 时插入的 severity='stub' 旧行被 fold 成 'ok'。"""
        from sqlalchemy import text

        cfg = _alembic_config()
        # 升到 0017(CHECK 约束尚未加,可插 stub 行)。
        command.upgrade(cfg, "0017_plot_threads_thread_type")
        engine = create_engine(f"sqlite:///{tmp_db_path}")
        now = datetime.now(timezone.utc).isoformat()
        with engine.begin() as conn:
            conn.execute(
                text(
                    "INSERT INTO style_audit_history "
                    "(audit_id, audited_at, drift_score, severity, report_json) "
                    "VALUES ('sty_legacy', :now, NULL, 'stub', '{}')"
                ),
                {"now": now},
            )
        # 升到 head(0018):fold stub → ok + 加 CHECK。
        command.upgrade(cfg, "head")
        with Session(engine) as session:
            loaded = session.get(StyleAuditEntryORM, "sty_legacy")
            assert loaded is not None
            assert loaded.severity == "ok"  # stub 已 fold


class TestOrmRoundTrip:
    """5 模型各 1 例:Pydantic → ORM → SQLite → 回读 → Pydantic 字段一致。"""

    @pytest.fixture
    def session(self, tmp_db_path: Path) -> Session:
        command.upgrade(_alembic_config(), "head")
        engine = create_engine(f"sqlite:///{tmp_db_path}")
        with Session(engine) as session:
            yield session

    def test_metrics_daily_round_trip(self, session: Session) -> None:
        m = MetricsDaily(
            date=date(2026, 5, 10),
            chapters_drafted=3,
            chapters_approved=2,
            chapters_rewritten=1,
            chapters_revised=1,
            chapters_escalated=0,
            avg_review_score=8.4,
            avg_rewrites_per_chapter=0.5,
            avg_revises_per_chapter=0.5,
            total_input_tokens=12345,
            total_output_tokens=6789,
            total_cost_usd=0.42,
            avg_chapter_latency_seconds=185.0,
            qwen_calls=10,
            claude_sonnet_calls=2,
            claude_opus_calls=0,
        )
        session.add(MetricsDailyORM(**m.model_dump()))
        session.commit()

        loaded = session.get(MetricsDailyORM, date(2026, 5, 10))
        assert loaded is not None
        assert loaded.chapters_drafted == 3
        assert loaded.avg_review_score == pytest.approx(8.4)
        assert loaded.qwen_calls == 10

    def test_metrics_per_chapter_round_trip_with_cache_disabled(
        self, session: Session
    ) -> None:
        """决策 5 默认 disable:``cache_input_tokens`` / ``cache_hit_ratio``
        为 ``None`` 可正常落库 + 回读。"""
        now = datetime.now(timezone.utc)
        m = MetricsPerChapter(
            chapter_id="ch-0001",
            drafted_at=now,
            approved_at=now,
            total_latency_seconds=120.5,
            rewrite_count=0,
            revise_count=0,
            legacy_parallel_drafts_used=False,
            final_score_overall=8.5,
            final_score_plot_progression=8.0,
            final_score_tension_match=8.5,
            final_score_prose_quality=9.0,
            final_score_consistency=8.5,
            final_score_voice_consistency=8.0,
            final_score_hook_management=8.5,
            word_count=2500,
            target_word_count_min=2000,
            target_word_count_max=3000,
            target_tension_level=7,
            actual_tension_estimate=6.8,
            total_tokens=14000,
            total_cost_usd=0.0,
            writer_profile="local_qwen_32k",
            writer_model="qwen-novel:latest",
            writer_calls=1,
            reviewer_calls=1,
            reviser_calls=0,
        )
        session.add(MetricsPerChapterORM(**m.model_dump()))
        session.commit()

        loaded = session.get(MetricsPerChapterORM, "ch-0001")
        assert loaded is not None
        assert loaded.cache_input_tokens is None
        assert loaded.cache_hit_ratio is None
        assert loaded.legacy_parallel_drafts_used is False
        assert loaded.writer_profile == "local_qwen_32k"

    def test_metrics_per_chapter_round_trip_with_cache_enabled(
        self, session: Session
    ) -> None:
        """决策 5 启用时:prompt cache 字段非空可正常落库。"""
        now = datetime.now(timezone.utc)
        m = MetricsPerChapter(
            chapter_id="ch-0002",
            drafted_at=now,
            approved_at=now,
            total_latency_seconds=90.0,
            rewrite_count=1,
            revise_count=0,
            final_score_overall=8.85,
            final_score_plot_progression=8.5,
            final_score_tension_match=8.5,
            final_score_prose_quality=9.0,
            final_score_consistency=9.0,
            final_score_voice_consistency=8.5,
            final_score_hook_management=9.0,
            word_count=2800,
            target_word_count_min=2500,
            target_word_count_max=3200,
            target_tension_level=8,
            actual_tension_estimate=7.5,
            total_tokens=18000,
            total_cost_usd=0.15,
            writer_profile="cloud_quality",
            writer_model="claude-sonnet-4-6",
            writer_calls=2,
            reviewer_calls=2,
            reviser_calls=0,
            cache_input_tokens=4500,
            cache_hit_ratio=0.32,
        )
        row = MetricsPerChapterORM(**m.model_dump())
        # 物理列只为历史 DB 读取兼容保留，不再进入活动 Pydantic/API 合同。
        row.legacy_parallel_drafts_used = True
        session.add(row)
        session.commit()

        loaded = session.get(MetricsPerChapterORM, "ch-0002")
        assert loaded is not None
        assert loaded.cache_input_tokens == 4500
        assert loaded.cache_hit_ratio == pytest.approx(0.32)
        assert loaded.legacy_parallel_drafts_used is True

    def test_system_metrics_daily_round_trip(self, session: Session) -> None:
        m = SystemMetricsDaily(
            date=date(2026, 5, 10),
            api_requests=1024,
            api_errors=3,
            avg_api_latency_ms=42.5,
            ws_connections=8,
            frontend_errors=1,
            pipeline_runs=6,
        )
        session.add(SystemMetricsDailyORM(**m.model_dump()))
        session.commit()

        loaded = session.get(SystemMetricsDailyORM, date(2026, 5, 10))
        assert loaded is not None
        assert loaded.api_requests == 1024
        assert loaded.avg_api_latency_ms == pytest.approx(42.5)

    def test_memory_health_check_round_trip(self, session: Session) -> None:
        now = datetime.now(timezone.utc)
        check = MemoryHealthCheck(
            check_id="chk_001",
            checked_at=now,
            severity="warning",
            stale_threads_count=2,
            inactive_characters_count=1,
            conflicts_count=0,
            report_json={
                "stale_threads": ["thread_007", "thread_012"],
                "inactive_characters": ["minor_npc_03"],
            },
        )
        session.add(MemoryHealthCheckORM(**check.model_dump()))
        session.commit()

        loaded = session.get(MemoryHealthCheckORM, "chk_001")
        assert loaded is not None
        assert loaded.severity == "warning"
        assert loaded.report_json["stale_threads"] == ["thread_007", "thread_012"]

    def test_style_audit_entry_round_trip(self, session: Session) -> None:
        """v1.1 w4-s5 / I2.2:severity 4 档 + drift_score 0-10 round-trip。"""
        now = datetime.now(timezone.utc)
        entry = StyleAuditEntry(
            audit_id="audit_001",
            audited_at=now,
            chapter_range_start="ch-0001",
            chapter_range_end="ch-0010",
            drift_score=3.5,
            severity="minor",
            report_json={"summary": "略偏离", "dimension_notes": []},
        )
        session.add(StyleAuditEntryORM(**entry.model_dump()))
        session.commit()

        loaded = session.get(StyleAuditEntryORM, "audit_001")
        assert loaded is not None
        assert loaded.severity == "minor"
        assert loaded.drift_score == 3.5

    def test_metrics_daily_avg_fields_nullable(self, session: Session) -> None:
        """无样本日的 avg_* 字段可写 None(spec 字面 NULLable)。"""
        m = MetricsDaily(
            date=date(2026, 5, 1),
            chapters_drafted=0,
            chapters_approved=0,
            chapters_rewritten=0,
            chapters_revised=0,
            chapters_escalated=0,
            avg_review_score=None,
            avg_rewrites_per_chapter=None,
            avg_revises_per_chapter=None,
            total_input_tokens=0,
            total_output_tokens=0,
            total_cost_usd=0.0,
            avg_chapter_latency_seconds=None,
            qwen_calls=0,
            claude_sonnet_calls=0,
            claude_opus_calls=0,
        )
        session.add(MetricsDailyORM(**m.model_dump()))
        session.commit()

        loaded = session.get(MetricsDailyORM, date(2026, 5, 1))
        assert loaded is not None
        assert loaded.avg_review_score is None
        assert loaded.avg_chapter_latency_seconds is None


class TestPydanticDefaults:
    """Pydantic 模型的默认值与字段约束。"""

    def test_metrics_daily_defaults(self) -> None:
        """计数字段默认 0;avg_* 默认 None。"""
        m = MetricsDaily(date=date(2026, 5, 10))
        assert m.chapters_drafted == 0
        assert m.total_input_tokens == 0
        assert m.total_cost_usd == 0.0
        assert m.avg_review_score is None
        assert m.avg_chapter_latency_seconds is None

    def test_memory_health_severity_literal_rejects_invalid(self) -> None:
        with pytest.raises(ValueError):
            MemoryHealthCheck(
                check_id="x",
                checked_at=datetime.now(timezone.utc),
                severity="catastrophic",  # type: ignore[arg-type]
                report_json={},
            )

    def test_metrics_per_chapter_cache_fields_default_none(self) -> None:
        """决策 5 字段默认 None,显式传 None 也允许。"""
        now = datetime.now(timezone.utc)
        m = MetricsPerChapter(
            chapter_id="ch-x",
            drafted_at=now,
            approved_at=now,
            total_latency_seconds=1.0,
            final_score_overall=8.0,
            final_score_plot_progression=8.0,
            final_score_tension_match=8.0,
            final_score_prose_quality=8.0,
            final_score_consistency=8.0,
            final_score_voice_consistency=8.0,
            final_score_hook_management=8.0,
            word_count=2000,
            target_word_count_min=1500,
            target_word_count_max=2500,
            target_tension_level=5,
            writer_profile="p",
            writer_model="m",
        )
        assert m.cache_input_tokens is None
        assert m.cache_hit_ratio is None
        assert "parallel_drafts_used" not in m.model_dump()
        assert m.rewrite_count == 0


class TestUniqueConstraint:
    """``metrics_daily`` / ``system_metrics_daily`` ``date`` 主键唯一性。"""

    @pytest.fixture
    def session(self, tmp_db_path: Path) -> Session:
        command.upgrade(_alembic_config(), "head")
        engine = create_engine(f"sqlite:///{tmp_db_path}")
        with Session(engine) as session:
            yield session

    def test_metrics_daily_date_unique(self, session: Session) -> None:
        d = date(2026, 5, 10)
        session.add(
            MetricsDailyORM(
                date=d,
                chapters_drafted=1,
                chapters_approved=1,
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
        session.commit()

        # 同 date 二次写入应抛 IntegrityError(PK 冲突)
        session.add(
            MetricsDailyORM(
                date=d,
                chapters_drafted=2,
                chapters_approved=2,
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
        with pytest.raises(IntegrityError):
            session.commit()
        session.rollback()

    def test_select_by_date_works(self, session: Session) -> None:
        for i, d in enumerate(
            [date(2026, 5, 1), date(2026, 5, 2), date(2026, 5, 3)]
        ):
            session.add(
                SystemMetricsDailyORM(
                    date=d,
                    api_requests=100 * (i + 1),
                    api_errors=0,
                    ws_connections=0,
                    frontend_errors=0,
                    pipeline_runs=0,
                )
            )
        session.commit()

        rows = session.scalars(
            select(SystemMetricsDailyORM).order_by(SystemMetricsDailyORM.date)
        ).all()
        assert [r.api_requests for r in rows] == [100, 200, 300]


class TestForeignKeyAbsence:
    """``metrics_per_chapter.chapter_id`` 不加 FK(ORM 决策);允许引用尚不
    存在的 chapter_id(P4 metrics 历史样本独立性)。"""

    def test_metrics_per_chapter_no_fk_to_chapters(
        self, tmp_db_path: Path
    ) -> None:
        cfg = _alembic_config()
        command.upgrade(cfg, "head")
        engine = create_engine(f"sqlite:///{tmp_db_path}")
        inspector = inspect(engine)
        fks = inspector.get_foreign_keys("metrics_per_chapter")
        assert fks == [], (
            f"metrics_per_chapter should have NO foreign keys, got {fks}"
        )

    def test_can_insert_metrics_for_nonexistent_chapter(
        self, tmp_db_path: Path
    ) -> None:
        cfg = _alembic_config()
        command.upgrade(cfg, "head")
        engine = create_engine(f"sqlite:///{tmp_db_path}")
        # 显式开 FK 强制(SQLite 默认 OFF;真实运行 P1-B-18 session
        # factory 也开 PRAGMA foreign_keys=ON,这里模拟)
        with engine.connect() as conn:
            conn.execute(text("PRAGMA foreign_keys = ON"))
            conn.commit()

        with Session(engine) as session:
            session.execute(text("PRAGMA foreign_keys = ON"))
            now = datetime.now(timezone.utc)
            # chapter_id 'ghost-ch-9999' 不在 chapters 表;若加了 FK 会
            # 因 chapters 行不存在而抛 IntegrityError。无 FK 应正常写。
            m = MetricsPerChapter(
                chapter_id="ghost-ch-9999",
                drafted_at=now,
                approved_at=now,
                total_latency_seconds=1.0,
                final_score_overall=8.0,
                final_score_plot_progression=8.0,
                final_score_tension_match=8.0,
                final_score_prose_quality=8.0,
                final_score_consistency=8.0,
                final_score_voice_consistency=8.0,
                final_score_hook_management=8.0,
                word_count=2000,
                target_word_count_min=1500,
                target_word_count_max=2500,
                target_tension_level=5,
                writer_profile="p",
                writer_model="m",
            )
            session.add(MetricsPerChapterORM(**m.model_dump()))
            session.commit()

            loaded = session.get(MetricsPerChapterORM, "ghost-ch-9999")
            assert loaded is not None
