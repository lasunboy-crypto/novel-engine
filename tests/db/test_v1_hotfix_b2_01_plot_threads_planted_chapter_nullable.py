"""v1.0.x B2-01 Alembic 0011 ``plot_threads.planted_chapter`` 改可空。

对齐 ``alembic/versions/0011_plot_threads_planted_chapter_nullable.py``:
- ``upgrade`` 把 ``planted_chapter`` 列约束 ``NOT NULL`` → ``NULL``
- ``downgrade`` 还原 ``NOT NULL``(若表中已有 NULL 行,回滚失败 — 预期)

测试范围(对齐 CLAUDE.md §Alembic head 纪律 — 显式 revision pin
``0011_plot_threads_planted_chapter_nullable`` 避免后续 head 漂移)。

**v1.1 wave 4 session 2(G2)起改用 raw SQL 插 / 读 plot_threads**:原测试用
``PlotThreadORM`` 实例 + ``session.commit()`` / ``session.get`` 操作行,但 ORM 类随
schema 演进新增列(G2 加 ``thread_type`` server_default),SQLAlchemy 的 eager-default
机制会发 ``INSERT ... RETURNING thread_type`` / ``SELECT thread_type``,而本测试 pin
到 0011 schema 无该列 → "no such column: thread_type" 失败。沿用 ``_seed_workspace``
同型 raw SQL 策略:只 INSERT / SELECT 0011 schema 真实存在的列,schema 继续演化下不破。
"""

from __future__ import annotations

from datetime import UTC, datetime
from pathlib import Path

import pytest
from alembic import command
from alembic.config import Config
from sqlalchemy import create_engine, inspect, text
from sqlalchemy.exc import IntegrityError


def _alembic_config(db_path: Path) -> Config:
    cfg = Config("alembic.ini")
    cfg.set_main_option("sqlalchemy.url", f"sqlite:///{db_path}")
    return cfg


@pytest.fixture
def tmp_db_path(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> Path:
    db = tmp_path / "novel.db"
    monkeypatch.setenv("NOVEL_DB_PATH", str(db))
    return db


def _seed_workspace(db_path: Path, workspace_id: str) -> None:
    """seed 一个 workspace,供 plot_threads FK 满足。

    **v1.1 wave 1 session 6 起改用 raw SQL**(原 ``session.merge(WorkspaceORM)``
    会 SELECT 全列含最新 ORM 类的 ``default_voice`` 字段,但本测试 pin 到
    0011 schema 没该列→ "no such column" 失败)。raw SQL 只 INSERT 真正需要
    的列,DB 用列默认值填其余,schema 演化下不破。沿用 P5-B-07 outline_status
    server_default 同型策略——加 workspace 列的迁移用 server_default 让
    旧测试 INSERT 缺列也兜底。
    """
    engine = create_engine(f"sqlite:///{db_path}")
    now = datetime.now(UTC).isoformat()
    with engine.begin() as conn:
        conn.execute(
            text(
                "INSERT OR REPLACE INTO workspaces "
                "(workspace_id, display_name, genre, premise, created_at, "
                "last_active_at, engine_version, status, total_chapters, "
                "completed_chapters, config_overrides) VALUES "
                "(:wid, :dn, :g, :p, :ca, :la, :ev, :st, :tc, :cc, :co)"
            ),
            {
                "wid": workspace_id,
                "dn": "x",
                "g": "x",
                "p": "p",
                "ca": now,
                "la": now,
                "ev": "0.1.0",
                "st": "active",
                "tc": 0,
                "cc": 0,
                "co": "{}",
            },
        )


def _insert_plot_thread_raw(
    db_path: Path,
    *,
    thread_id: str,
    workspace_id: str,
    title: str,
    planted_chapter: str | None,
    description: str,
) -> None:
    """raw SQL 插一行 plot_threads(只用 0011 schema 存在的列)。

    G2 起避免 ORM eager-default 触发 ``RETURNING thread_type``(0011 schema 无该列)。
    """
    engine = create_engine(f"sqlite:///{db_path}")
    now = datetime.now(UTC).isoformat()
    with engine.begin() as conn:
        conn.execute(
            text(
                "INSERT INTO plot_threads "
                "(thread_id, workspace_id, title, status, importance, "
                "planted_chapter, description, related_characters, created_at) "
                "VALUES (:tid, :wid, :ti, :st, :imp, :pc, :de, :rc, :ca)"
            ),
            {
                "tid": thread_id,
                "wid": workspace_id,
                "ti": title,
                "st": "planted",
                "imp": 3,
                "pc": planted_chapter,
                "de": description,
                "rc": "[]",
                "ca": now,
            },
        )


def _get_plot_thread_raw(db_path: Path, thread_id: str) -> dict | None:
    """raw SQL 读一行 plot_threads(只取 0011 schema 列),返回 dict 或 None。"""
    engine = create_engine(f"sqlite:///{db_path}")
    with engine.connect() as conn:
        row = conn.execute(
            text(
                "SELECT thread_id, title, planted_chapter "
                "FROM plot_threads WHERE thread_id = :tid"
            ),
            {"tid": thread_id},
        ).fetchone()
    if row is None:
        return None
    return {"thread_id": row[0], "title": row[1], "planted_chapter": row[2]}


class TestAlembicMigration:
    """Alembic 0011 round-trip + nullable=True 行为。"""

    def test_upgrade_allows_planted_chapter_null(
        self, tmp_db_path: Path
    ) -> None:
        """0011 upgrade 后 INSERT planted_chapter=NULL 不抛 IntegrityError。"""
        cfg = _alembic_config(tmp_db_path)
        command.upgrade(cfg, "0011_plot_threads_planted_chapter_nullable")
        _seed_workspace(tmp_db_path, "ws-a")

        # raw SQL INSERT(planted_chapter=None 不应抛)
        _insert_plot_thread_raw(
            tmp_db_path,
            thread_id="t-null-planted",
            workspace_id="ws-a",
            title="测试",
            planted_chapter=None,
            description="测试描述",
        )
        row = _get_plot_thread_raw(tmp_db_path, "t-null-planted")
        assert row is not None
        assert row["planted_chapter"] is None

    def test_downgrade_restores_not_null_for_clean_table(
        self, tmp_db_path: Path
    ) -> None:
        """upgrade 到 0011 后,无 NULL 行情况下 downgrade -1 还原 NOT NULL 成功。"""
        cfg = _alembic_config(tmp_db_path)
        command.upgrade(cfg, "0011_plot_threads_planted_chapter_nullable")
        # 不插任何 NULL 行,直接 downgrade
        command.downgrade(cfg, "-1")
        # 验证 schema:planted_chapter 列应为 NOT NULL
        engine = create_engine(f"sqlite:///{tmp_db_path}")
        inspector = inspect(engine)
        columns = {c["name"]: c for c in inspector.get_columns("plot_threads")}
        assert "planted_chapter" in columns
        # SQLAlchemy inspector 返 nullable bool
        assert columns["planted_chapter"]["nullable"] is False

    def test_round_trip_upgrade_downgrade_upgrade_idempotent(
        self, tmp_db_path: Path
    ) -> None:
        """0011 → 0010 → 0011 幂等(显式 revision pin 防 head 漂移)。"""
        cfg = _alembic_config(tmp_db_path)
        command.upgrade(cfg, "0011_plot_threads_planted_chapter_nullable")
        command.downgrade(cfg, "-1")
        command.upgrade(cfg, "0011_plot_threads_planted_chapter_nullable")
        # 仅断言无错;数据语义见前 2 测
        engine = create_engine(f"sqlite:///{tmp_db_path}")
        inspector = inspect(engine)
        columns = {c["name"]: c for c in inspector.get_columns("plot_threads")}
        assert columns["planted_chapter"]["nullable"] is True

    def test_existing_non_null_rows_preserved_after_upgrade(
        self, tmp_db_path: Path
    ) -> None:
        """0010 时存有非空 planted_chapter 行,upgrade 到 0011 后仍保留。"""
        cfg = _alembic_config(tmp_db_path)
        # 先升到 0010(0011 前一档)
        command.upgrade(cfg, "0010_backfill_completed_chapters")
        _seed_workspace(tmp_db_path, "ws-b")
        # 0010 时 planted_chapter 仍 NOT NULL,正常插入有值的行(raw SQL)
        _insert_plot_thread_raw(
            tmp_db_path,
            thread_id="t-with-chapter",
            workspace_id="ws-b",
            title="已埋",
            planted_chapter="ch-0001",
            description="ws-b 已埋",
        )
        # 升到 0011
        command.upgrade(cfg, "0011_plot_threads_planted_chapter_nullable")
        row = _get_plot_thread_raw(tmp_db_path, "t-with-chapter")
        assert row is not None
        assert row["planted_chapter"] == "ch-0001"
        assert row["title"] == "已埋"

    def test_downgrade_fails_when_null_rows_exist(
        self, tmp_db_path: Path
    ) -> None:
        """0011 时存有 planted_chapter=NULL 行,downgrade 到 0010 失败
        (NOT NULL 约束被违反,这是预期行为)。

        SQLite 走 batch_alter_table 模式,recreate table 时若已有 NULL 行
        撞 NOT NULL 约束抛 IntegrityError。
        """
        cfg = _alembic_config(tmp_db_path)
        command.upgrade(cfg, "0011_plot_threads_planted_chapter_nullable")
        _seed_workspace(tmp_db_path, "ws-c")
        _insert_plot_thread_raw(
            tmp_db_path,
            thread_id="t-null-for-downgrade",
            workspace_id="ws-c",
            title="挡 downgrade",
            planted_chapter=None,
            description="预期 downgrade 失败",
        )
        # downgrade 应失败(NOT NULL 约束被违反)
        with pytest.raises(IntegrityError):
            command.downgrade(cfg, "-1")
