"""v1.0 hot-fix A-03 Alembic 0010 backfill round-trip 测试。

对齐 ``alembic/versions/0010_backfill_completed_chapters.py``:
- ``upgrade`` 把 ``workspaces.completed_chapters`` 设为 SELECT COUNT(*) FROM
  ``chapters WHERE status='approved'``(按 workspace_id 聚合)
- ``downgrade`` noop(纯数据回填,原值不可知)

测试范围 (v1.1 wave 1 session 6 / G10 起):**改用 ``head`` + 显式 pin
``downgrade 0009_pipeline_runs_table``**。原 v1.0 hot-fix A-03 设计是
``upgrade(cfg, "0010_backfill_completed_chapters")`` + ``downgrade -1``,
那时 head==0010。后续 0011-0014 加列不动 ``workspaces`` 表所以这个测试
不破。**0015 是第一个加 ``workspaces.default_voice`` 列的下游迁移** —
ORM 类含最新 schema 时 ``session.get(WorkspaceORM, ...)`` 会 SELECT 全列
撞 "no such column: workspaces.default_voice"(测试 pin 到 0010,DB
schema 没 default_voice)。修法 = upgrade target 改 ``head``(让 schema
含所有列,ORM 查询不破)+ downgrade 显式 pin ``0009_pipeline_runs_table``
(0010 的前一档,确保 upgrade 回 head 时 0010 backfill SQL 重跑;不能用
``-1`` 否则退到 0014 而错过 0010 backfill)。沿用 CLAUDE.md §"新增
Alembic 迁移前必 grep 旧测试" 纪律的同型字面思路:**字面 revision pin**,
只是 pin 的 target 从"自身迁移"换成"自身的前一档"。
"""

from __future__ import annotations

from datetime import UTC, datetime
from pathlib import Path

import pytest
from alembic import command
from alembic.config import Config
from sqlalchemy import create_engine, select
from sqlalchemy.orm import Session

from src.core.db import (
    ChapterORM,
    WorkspaceORM,
)


def _alembic_config(db_path: Path) -> Config:
    cfg = Config("alembic.ini")
    cfg.set_main_option("sqlalchemy.url", f"sqlite:///{db_path}")
    return cfg


@pytest.fixture
def tmp_db_path(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> Path:
    db = tmp_path / "novel.db"
    monkeypatch.setenv("NOVEL_DB_PATH", str(db))
    return db


def _seed_chapters_with_statuses(
    db_path: Path,
    workspace_id: str,
    statuses: list[str],
    *,
    initial_completed_chapters: int = 0,
) -> None:
    """seed 一个 workspace + 多个 chapter,workspace.completed_chapters 故意填错。"""
    engine = create_engine(f"sqlite:///{db_path}")
    now = datetime.now(UTC)
    with Session(engine) as session:
        session.merge(
            WorkspaceORM(
                workspace_id=workspace_id,
                display_name="x",
                genre="x",
                premise="p",
                created_at=now,
                last_active_at=now,
                engine_version="0.1.0",
                status="active",
                total_chapters=len(statuses),
                completed_chapters=initial_completed_chapters,
                config_overrides={},
            )
        )
        for i, status in enumerate(statuses, start=1):
            session.merge(
                ChapterORM(
                    chapter_id=f"ch-{i:04d}",
                    workspace_id=workspace_id,
                    current_version=1,
                    status=status,
                    rewrite_count=0,
                    revise_count=0,
                    created_at=now,
                    approved_at=now if status == "approved" else None,
                    total_token_usage={},
                    total_cost_usd=0.0,
                )
            )
        session.commit()


class TestAlembicMigration:
    """Alembic 0010 backfill 行为 + downgrade noop。"""

    def test_upgrade_backfills_completed_chapters_from_approved_count(
        self, tmp_db_path: Path
    ) -> None:
        """5 approved + 2 reviewing + 1 drafting + 1 invalidated → completed=5。"""
        cfg = _alembic_config(tmp_db_path)
        # 显式 pin 0010(B2-01 起新 head=0011,沿 head 字面会导致 downgrade -1
        # 退到 0010 而非 0009,upgrade 0010 noop 不重跑 backfill SQL → 测试断
        # 言失败)
        command.upgrade(cfg, "head")
        _seed_chapters_with_statuses(
            tmp_db_path,
            "ws-a",
            [
                "approved", "approved", "approved", "approved", "approved",
                "reviewing", "reviewing",
                "drafting",
                "invalidated",
            ],
            initial_completed_chapters=0,
        )
        # 再 downgrade -1 + upgrade 触发 backfill SQL 再跑一次
        command.downgrade(cfg, "0009_pipeline_runs_table")
        command.upgrade(cfg, "head")

        engine = create_engine(f"sqlite:///{tmp_db_path}")
        with Session(engine) as session:
            ws_row = session.get(WorkspaceORM, "ws-a")
            assert ws_row is not None
            assert ws_row.completed_chapters == 5

    def test_upgrade_corrects_wrong_initial_count(
        self, tmp_db_path: Path
    ) -> None:
        """seed 时 completed_chapters 故意填错(99),backfill 后纠正为 approved 数。"""
        cfg = _alembic_config(tmp_db_path)
        command.upgrade(cfg, "head")
        _seed_chapters_with_statuses(
            tmp_db_path,
            "ws-b",
            ["approved", "approved", "reviewing"],
            initial_completed_chapters=99,
        )
        # 触发 backfill:downgrade -1 后再 upgrade
        command.downgrade(cfg, "0009_pipeline_runs_table")
        command.upgrade(cfg, "head")

        engine = create_engine(f"sqlite:///{tmp_db_path}")
        with Session(engine) as session:
            ws_row = session.get(WorkspaceORM, "ws-b")
            assert ws_row is not None
            assert ws_row.completed_chapters == 2  # 2 approved

    def test_upgrade_zero_chapters_workspace_stays_zero(
        self, tmp_db_path: Path
    ) -> None:
        """没有 chapters 行的工作区 backfill 后 completed_chapters 仍为 0。"""
        cfg = _alembic_config(tmp_db_path)
        command.upgrade(cfg, "head")
        engine = create_engine(f"sqlite:///{tmp_db_path}")
        now = datetime.now(UTC)
        with Session(engine) as session:
            session.merge(
                WorkspaceORM(
                    workspace_id="ws-empty",
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
        # 触发 backfill
        command.downgrade(cfg, "0009_pipeline_runs_table")
        command.upgrade(cfg, "head")

        with Session(engine) as session:
            ws_row = session.get(WorkspaceORM, "ws-empty")
            assert ws_row is not None
            assert ws_row.completed_chapters == 0

    def test_downgrade_noop_preserves_completed_chapters(
        self, tmp_db_path: Path
    ) -> None:
        """downgrade 不动数据,completed_chapters 保留 upgrade 后的值。"""
        cfg = _alembic_config(tmp_db_path)
        command.upgrade(cfg, "head")
        _seed_chapters_with_statuses(
            tmp_db_path,
            "ws-c",
            ["approved", "approved"],
            initial_completed_chapters=0,
        )
        # 触发 backfill
        command.downgrade(cfg, "0009_pipeline_runs_table")
        command.upgrade(cfg, "head")

        engine = create_engine(f"sqlite:///{tmp_db_path}")
        with Session(engine) as session:
            ws_row = session.get(WorkspaceORM, "ws-c")
            assert ws_row.completed_chapters == 2
        # downgrade 一档 noop —— completed_chapters 仍 2(v1.1 wave 1 session 6:
        # downgrade 到 0009 后 schema 缺 0010+ 加的列,ORM ``session.get`` 会
        # SELECT 全列撞 "no such column";改用 raw SQL 只读 completed_chapters)
        command.downgrade(cfg, "0009_pipeline_runs_table")
        from sqlalchemy import text as _sa_text
        with engine.connect() as conn:
            row = conn.execute(
                _sa_text(
                    "SELECT completed_chapters FROM workspaces "
                    "WHERE workspace_id = :wid"
                ),
                {"wid": "ws-c"},
            ).fetchone()
            assert row is not None
            assert row.completed_chapters == 2

    def test_upgrade_downgrade_upgrade_idempotent(
        self, tmp_db_path: Path
    ) -> None:
        """0010 → 0009 → 0010 幂等(显式 revision pin 防 head 漂移)。"""
        cfg = _alembic_config(tmp_db_path)
        command.upgrade(cfg, "head")
        command.downgrade(cfg, "0009_pipeline_runs_table")
        command.upgrade(cfg, "head")
        # 仅断言 upgrade 链路无错;数据语义见前 3 测
        engine = create_engine(f"sqlite:///{tmp_db_path}")
        with Session(engine) as session:
            # workspaces 表存在(可 SELECT 不抛)
            session.execute(select(WorkspaceORM)).all()
