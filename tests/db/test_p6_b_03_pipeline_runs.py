"""P6-B-03 ``pipeline_runs`` 表 + Alembic 0009 round-trip 测试。

对齐 docs/plan/p6_plan.md §"P6-B-03" + ``src/core/db/models.py``
``PipelineRunORM`` + ``alembic/versions/0009_pipeline_runs_table.py``。

测试范围(5 类覆盖,plan §10 测试约定):

- **upgrade head** 建出 ``pipeline_runs`` 表 + 2 索引 + 2 CHECK 约束
- **downgrade -1** 只删 ``pipeline_runs``,P1-P5 + 0008 既有表保留
  (pin 到 ``0009_pipeline_runs_table`` 显式 revision,对齐 CLAUDE.md
  §Alembic head 纪律)
- **upgrade-downgrade-upgrade 幂等**(同源)
- **state CHECK 约束**:5 档允许 + 第 6 档拒绝(IntegrityError)
- **scope CHECK 约束**:4 档允许 + 第 5 档拒绝(IntegrityError)
- **ORM round-trip**:Pydantic-like fields → ORM insert → ORM select 字段
  完整(含 JSON 字段 chapter_ids / completed_chapter_ids / error_details)
- **workspace_id FK CASCADE**:WorkspaceORM delete 级联清 pipeline_runs 行
"""

from __future__ import annotations

import sqlite3
from datetime import UTC, datetime
from pathlib import Path

import pytest
from alembic import command
from alembic.config import Config
from sqlalchemy import create_engine, inspect, text
from sqlalchemy.exc import IntegrityError
from sqlalchemy.orm import Session

from src.core.db import (
    PIPELINE_SCOPE_VALUES,
    PIPELINE_STATE_VALUES,
    PipelineRunORM,
    WorkspaceORM,
)


def _alembic_config(db_path: Path) -> Config:
    cfg = Config("alembic.ini")
    cfg.set_main_option("sqlalchemy.url", f"sqlite:///{db_path}")
    return cfg


@pytest.fixture
def tmp_db_path(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> Path:
    """tmp_path 下临时 novel.db + 设 NOVEL_DB_PATH 环境变量(alembic env.py 用)。"""
    db = tmp_path / "novel.db"
    monkeypatch.setenv("NOVEL_DB_PATH", str(db))
    return db


def _table_names(db_path: Path) -> list[str]:
    con = sqlite3.connect(str(db_path))
    try:
        rows = con.execute(
            "SELECT name FROM sqlite_master WHERE type='table' "
            "AND name != 'alembic_version' ORDER BY name"
        ).fetchall()
        return [r[0] for r in rows]
    finally:
        con.close()


def _index_names(db_path: Path, table: str) -> set[str]:
    con = sqlite3.connect(str(db_path))
    try:
        rows = con.execute(
            "SELECT name FROM sqlite_master WHERE type='index' "
            f"AND tbl_name='{table}'"
        ).fetchall()
        return {r[0] for r in rows}
    finally:
        con.close()


def _seed_workspace(session: Session, ws_id: str = "ws-p6-b-03") -> str:
    """种一个 WorkspaceORM(FK 父表)。"""
    now = datetime.now(UTC)
    session.merge(
        WorkspaceORM(
            workspace_id=ws_id,
            display_name="x",
            genre="x",
            premise="p",
            created_at=now,
            last_active_at=now,
            engine_version="0.1.0",
            status="active",
            total_chapters=0,
            completed_chapters=0,
            config_overrides={},
        )
    )
    session.commit()
    return ws_id


class TestAlembicMigration:
    """Alembic 0009 ``pipeline_runs`` 建表 + 索引 + CHECK 约束。"""

    def test_upgrade_head_creates_pipeline_runs_table(
        self, tmp_db_path: Path
    ) -> None:
        cfg = _alembic_config(tmp_db_path)
        command.upgrade(cfg, "head")
        assert "pipeline_runs" in _table_names(tmp_db_path)

    def test_upgrade_head_creates_indexes(self, tmp_db_path: Path) -> None:
        cfg = _alembic_config(tmp_db_path)
        command.upgrade(cfg, "head")
        indexes = _index_names(tmp_db_path, "pipeline_runs")
        assert "ix_pipeline_runs_workspace_id" in indexes
        assert "ix_pipeline_runs_state" in indexes

    def test_upgrade_head_creates_check_constraints(
        self, tmp_db_path: Path
    ) -> None:
        """sqlite_master 表 schema SQL 含两条 CHECK 约束名。"""
        cfg = _alembic_config(tmp_db_path)
        command.upgrade(cfg, "head")
        con = sqlite3.connect(str(tmp_db_path))
        try:
            row = con.execute(
                "SELECT sql FROM sqlite_master "
                "WHERE type='table' AND name='pipeline_runs'"
            ).fetchone()
        finally:
            con.close()
        assert row is not None
        # v2 w1-s2(0024):state CHECK 扩 interrupted,约束名 5 → 6 档
        assert "ck_pipeline_runs_state_6_levels" in row[0]
        assert "ck_pipeline_runs_scope_4_levels" in row[0]

    def test_downgrade_minus_one_drops_only_pipeline_runs(
        self, tmp_db_path: Path
    ) -> None:
        """显式 upgrade 到 ``0009_pipeline_runs_table`` 再 downgrade -1
        只删 ``pipeline_runs``,P1-P5 + 0008 表全部保留(CLAUDE.md §Alembic
        head 纪律字面 — 验本任务自身 round-trip)。
        """
        cfg = _alembic_config(tmp_db_path)
        command.upgrade(cfg, "0009_pipeline_runs_table")
        assert "pipeline_runs" in _table_names(tmp_db_path)
        command.downgrade(cfg, "-1")
        tables = _table_names(tmp_db_path)
        assert "pipeline_runs" not in tables
        # P1-P5 + 0008 已建表全部保留(抽样断言)
        for expected in (
            "workspaces",
            "chapters",
            "chapter_versions",
            "characters",
            "plot_threads",
            "volumes",
            "chapter_cards",
        ):
            assert expected in tables

    def test_upgrade_downgrade_upgrade_idempotent(
        self, tmp_db_path: Path
    ) -> None:
        """0009 → 0008 → 0009 幂等(显式 revision 防 head 漂移)。"""
        cfg = _alembic_config(tmp_db_path)
        command.upgrade(cfg, "0009_pipeline_runs_table")
        command.downgrade(cfg, "-1")
        command.upgrade(cfg, "0009_pipeline_runs_table")
        assert "pipeline_runs" in _table_names(tmp_db_path)


class TestStateCheckConstraint:
    """``state`` CHECK 约束 5 档允许 + 越界拒绝。"""

    @pytest.mark.parametrize("state_value", PIPELINE_STATE_VALUES)
    def test_each_allowed_state_writes(
        self, tmp_db_path: Path, state_value: str
    ) -> None:
        cfg = _alembic_config(tmp_db_path)
        command.upgrade(cfg, "head")
        engine = create_engine(f"sqlite:///{tmp_db_path}")
        with Session(engine) as session:
            ws_id = _seed_workspace(session)
            now = datetime.now(UTC)
            session.add(
                PipelineRunORM(
                    run_id=f"run-{state_value}",
                    workspace_id=ws_id,
                    state=state_value,
                    scope="single",
                    start_chapter_id="ch-0001",
                    end_chapter_id=None,
                    volume_id=None,
                    profile_override=None,
                    chapter_ids=["ch-0001"],
                    current_chapter=None,
                    completed_chapter_ids=[],
                    pause_reason=None,
                    error_details=None,
                    created_at=now,
                    updated_at=now,
                    completed_at=None,
                )
            )
            session.commit()

    def test_disallowed_state_raises(self, tmp_db_path: Path) -> None:
        cfg = _alembic_config(tmp_db_path)
        command.upgrade(cfg, "head")
        engine = create_engine(f"sqlite:///{tmp_db_path}")
        with Session(engine) as session:
            ws_id = _seed_workspace(session)
            now = datetime.now(UTC)
            session.add(
                PipelineRunORM(
                    run_id="run-bad",
                    workspace_id=ws_id,
                    state="completed",  # 第 7 档,越界(0024 后合法 6 档含 interrupted)
                    scope="single",
                    start_chapter_id="ch-0001",
                    end_chapter_id=None,
                    volume_id=None,
                    profile_override=None,
                    chapter_ids=["ch-0001"],
                    current_chapter=None,
                    completed_chapter_ids=[],
                    pause_reason=None,
                    error_details=None,
                    created_at=now,
                    updated_at=now,
                    completed_at=None,
                )
            )
            with pytest.raises(IntegrityError):
                session.commit()


class TestScopeCheckConstraint:
    """``scope`` CHECK 约束 4 档允许 + 越界拒绝。"""

    @pytest.mark.parametrize("scope_value", PIPELINE_SCOPE_VALUES)
    def test_each_allowed_scope_writes(
        self, tmp_db_path: Path, scope_value: str
    ) -> None:
        cfg = _alembic_config(tmp_db_path)
        command.upgrade(cfg, "head")
        engine = create_engine(f"sqlite:///{tmp_db_path}")
        with Session(engine) as session:
            ws_id = _seed_workspace(session)
            now = datetime.now(UTC)
            session.add(
                PipelineRunORM(
                    run_id=f"run-{scope_value}",
                    workspace_id=ws_id,
                    state="running",
                    scope=scope_value,
                    start_chapter_id="ch-0001",
                    end_chapter_id=None,
                    volume_id=None,
                    profile_override=None,
                    chapter_ids=["ch-0001"],
                    current_chapter=None,
                    completed_chapter_ids=[],
                    pause_reason=None,
                    error_details=None,
                    created_at=now,
                    updated_at=now,
                    completed_at=None,
                )
            )
            session.commit()

    def test_disallowed_scope_raises(self, tmp_db_path: Path) -> None:
        cfg = _alembic_config(tmp_db_path)
        command.upgrade(cfg, "head")
        engine = create_engine(f"sqlite:///{tmp_db_path}")
        with Session(engine) as session:
            ws_id = _seed_workspace(session)
            now = datetime.now(UTC)
            session.add(
                PipelineRunORM(
                    run_id="run-bad",
                    workspace_id=ws_id,
                    state="running",
                    scope="batch",  # 第 5 档,越界
                    start_chapter_id="ch-0001",
                    end_chapter_id=None,
                    volume_id=None,
                    profile_override=None,
                    chapter_ids=["ch-0001"],
                    current_chapter=None,
                    completed_chapter_ids=[],
                    pause_reason=None,
                    error_details=None,
                    created_at=now,
                    updated_at=now,
                    completed_at=None,
                )
            )
            with pytest.raises(IntegrityError):
                session.commit()


class TestOrmRoundTrip:
    """ORM ↔ SQLite round-trip(全字段含 JSON)。"""

    def test_full_run_round_trip(self, tmp_db_path: Path) -> None:
        cfg = _alembic_config(tmp_db_path)
        command.upgrade(cfg, "head")
        engine = create_engine(f"sqlite:///{tmp_db_path}")
        now = datetime.now(UTC)
        with Session(engine) as session:
            ws_id = _seed_workspace(session)
            session.add(
                PipelineRunORM(
                    run_id="run-rt",
                    workspace_id=ws_id,
                    state="paused",
                    scope="range",
                    start_chapter_id="ch-0001",
                    end_chapter_id="ch-0003",
                    volume_id="vol-1",
                    profile_override="cloud_premium",
                    chapter_ids=["ch-0001", "ch-0002", "ch-0003"],
                    current_chapter="ch-0002",
                    completed_chapter_ids=["ch-0001"],
                    pause_reason="user_request",
                    error_details={"reason": "test"},
                    created_at=now,
                    updated_at=now,
                    completed_at=None,
                )
            )
            session.commit()

        # 重新打开 session select
        with Session(engine) as session:
            row = session.get(PipelineRunORM, "run-rt")
            assert row is not None
            assert row.workspace_id == "ws-p6-b-03"
            assert row.state == "paused"
            assert row.scope == "range"
            assert row.start_chapter_id == "ch-0001"
            assert row.end_chapter_id == "ch-0003"
            assert row.volume_id == "vol-1"
            assert row.profile_override == "cloud_premium"
            assert row.chapter_ids == ["ch-0001", "ch-0002", "ch-0003"]
            assert row.current_chapter == "ch-0002"
            assert row.completed_chapter_ids == ["ch-0001"]
            assert row.pause_reason == "user_request"
            assert row.error_details == {"reason": "test"}


class TestForeignKeyCascade:
    """``workspace_id`` FK CASCADE:删 workspace 级联清 pipeline_runs。"""

    def test_workspace_delete_cascades(self, tmp_db_path: Path) -> None:
        cfg = _alembic_config(tmp_db_path)
        command.upgrade(cfg, "head")
        engine = create_engine(f"sqlite:///{tmp_db_path}")
        now = datetime.now(UTC)
        with Session(engine) as session:
            ws_id = _seed_workspace(session)
            session.add(
                PipelineRunORM(
                    run_id="run-cascade",
                    workspace_id=ws_id,
                    state="stopped",
                    scope="single",
                    start_chapter_id="ch-0001",
                    end_chapter_id=None,
                    volume_id=None,
                    profile_override=None,
                    chapter_ids=["ch-0001"],
                    current_chapter=None,
                    completed_chapter_ids=["ch-0001"],
                    pause_reason="completed",
                    error_details=None,
                    created_at=now,
                    updated_at=now,
                    completed_at=now,
                )
            )
            session.commit()

        # 开 FK + 删 workspace
        with Session(engine) as session:
            session.execute(text("PRAGMA foreign_keys = ON"))
            session.execute(
                text(
                    "DELETE FROM workspaces WHERE workspace_id = 'ws-p6-b-03'"
                )
            )
            session.commit()

        # pipeline_runs 行被级联删除
        with Session(engine) as session:
            count = session.execute(
                text("SELECT COUNT(*) FROM pipeline_runs")
            ).scalar()
            assert count == 0


class TestSchemaColumnsMatchOrm:
    """``alembic`` 0009 列名与 ``PipelineRunORM`` ``__table__.columns`` 一致。"""

    def test_columns_match(self, tmp_db_path: Path) -> None:
        cfg = _alembic_config(tmp_db_path)
        command.upgrade(cfg, "head")
        engine = create_engine(f"sqlite:///{tmp_db_path}")
        inspector = inspect(engine)
        db_cols = {c["name"] for c in inspector.get_columns("pipeline_runs")}
        orm_cols = {c.name for c in PipelineRunORM.__table__.columns}
        assert db_cols == orm_cols
