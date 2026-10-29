"""v1.0.x wave 4 session 3a Alembic 0012 ``chapter_summaries`` 表。

对齐 ``alembic/versions/0012_chapter_summary_table.py``:
- ``upgrade`` 新建表 + 1 索引 + 1 CHECK 约束(``extraction_trigger`` 2 档)
- ``downgrade`` drop 表 + 索引

测试范围(对齐 CLAUDE.md §Alembic head 纪律 — 显式 revision pin
``0012_chapter_summary_table`` 避免后续 head 漂移)。
"""

from __future__ import annotations

from datetime import UTC, datetime
from pathlib import Path

import pytest
from alembic import command
from alembic.config import Config
from sqlalchemy import MetaData, Table, create_engine, inspect, select
from sqlalchemy.exc import IntegrityError

from src.core.db import CHAPTER_SUMMARY_TRIGGER_VALUES


_V0012_COLUMNS = {
    "chapter_id",
    "workspace_id",
    "summary_text",
    "pov_character",
    "scene_setting",
    "key_events",
    "character_interactions",
    "unfinished_actions",
    "extraction_trigger",
    "model_used",
    "created_at",
    "updated_at",
}


def _chapter_summaries_table(engine) -> Table:
    """反射 0012 历史表，避免用已演进到 R4 的 live ORM 查询旧 revision。"""

    return Table(
        "chapter_summaries",
        MetaData(),
        autoload_with=engine,
    )


def _summary_values(
    *,
    chapter_id: str,
    workspace_id: str,
    trigger: str = "reviewer_pass",
    pov_character: str | None = "lin-yan",
    scene_setting: str | None = "廊外",
    key_events: list[dict] | None = None,
    character_interactions: list[dict] | None = None,
    unfinished_actions: list[str] | None = None,
) -> dict:
    now = datetime.now(UTC)
    return {
        "chapter_id": chapter_id,
        "workspace_id": workspace_id,
        "summary_text": f"摘要 {trigger}",
        "pov_character": pov_character,
        "scene_setting": scene_setting,
        "key_events": key_events or [],
        "character_interactions": character_interactions or [],
        "unfinished_actions": unfinished_actions or [],
        "extraction_trigger": trigger,
        "model_used": "qwen-novel",
        "created_at": now,
        "updated_at": now,
    }


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
    """seed 一个 workspace 满足 FK。

    **v1.1 wave 1 session 6 起改用 raw SQL**(原 ``session.merge(WorkspaceORM)``
    会 SELECT 全列含最新 ORM 类的 ``default_voice`` 字段,但本测试 pin 到
    0012 schema 没该列→ "no such column" 失败)。raw SQL 只 INSERT 真正需要
    的列,DB 用列默认值填其余,schema 演化下不破。沿用 P5-B-07 outline_status
    server_default 同型策略。
    """
    from sqlalchemy import text as _sa_text

    engine = create_engine(f"sqlite:///{db_path}", connect_args={"timeout": 5})
    now = datetime.now(UTC).isoformat()
    with engine.begin() as conn:
        conn.execute(_sa_text("PRAGMA foreign_keys = ON"))
        conn.execute(
            _sa_text(
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


class TestAlembicMigration:
    """Alembic 0012 round-trip + 表结构 + CHECK 约束。"""

    def test_upgrade_creates_table_and_index(self, tmp_db_path: Path) -> None:
        """0012 upgrade 后 chapter_summaries 表 + workspace_id 索引存在。"""
        cfg = _alembic_config(tmp_db_path)
        command.upgrade(cfg, "0012_chapter_summary_table")

        engine = create_engine(f"sqlite:///{tmp_db_path}")
        inspector = inspect(engine)
        assert "chapter_summaries" in inspector.get_table_names()
        indexes = {
            ix["name"] for ix in inspector.get_indexes("chapter_summaries")
        }
        assert "ix_chapter_summaries_workspace_id" in indexes

    def test_columns_match_orm(self, tmp_db_path: Path) -> None:
        """0012 upgrade 后保持该 revision 自身的历史列集。"""
        cfg = _alembic_config(tmp_db_path)
        command.upgrade(cfg, "0012_chapter_summary_table")

        engine = create_engine(f"sqlite:///{tmp_db_path}")
        inspector = inspect(engine)
        db_cols = {c["name"] for c in inspector.get_columns("chapter_summaries")}
        assert db_cols == _V0012_COLUMNS

    @pytest.mark.parametrize("trigger", CHAPTER_SUMMARY_TRIGGER_VALUES)
    def test_each_allowed_trigger_writes(
        self, tmp_db_path: Path, trigger: str
    ) -> None:
        """``extraction_trigger`` 2 档枚举都能 INSERT。"""
        cfg = _alembic_config(tmp_db_path)
        command.upgrade(cfg, "0012_chapter_summary_table")
        _seed_workspace(tmp_db_path, "ws-trigger")

        engine = create_engine(f"sqlite:///{tmp_db_path}")
        table = _chapter_summaries_table(engine)
        with engine.begin() as conn:
            conn.execute(
                table.insert().values(
                    **_summary_values(
                        chapter_id=f"ch-{trigger}",
                        workspace_id="ws-trigger",
                        trigger=trigger,
                        key_events=[{"description": "事件"}],
                        unfinished_actions=["未完动作"],
                    )
                )
            )
            stored = conn.execute(
                select(table.c.extraction_trigger).where(
                    table.c.chapter_id == f"ch-{trigger}"
                )
            ).scalar_one()
        assert stored == trigger

    def test_invalid_trigger_rejected(self, tmp_db_path: Path) -> None:
        """非枚举 ``extraction_trigger`` 值 INSERT 抛 IntegrityError(CHECK 约束)。"""
        cfg = _alembic_config(tmp_db_path)
        command.upgrade(cfg, "0012_chapter_summary_table")
        _seed_workspace(tmp_db_path, "ws-bad")

        engine = create_engine(f"sqlite:///{tmp_db_path}")
        table = _chapter_summaries_table(engine)
        with pytest.raises(IntegrityError), engine.begin() as conn:
            conn.execute(
                table.insert().values(
                    **_summary_values(
                        chapter_id="ch-bad-trigger",
                        workspace_id="ws-bad",
                        trigger="manual_override",
                    )
                )
            )

    def test_nullable_columns_accept_none(self, tmp_db_path: Path) -> None:
        """``pov_character`` / ``scene_setting`` 可空,INSERT None 不抛。"""
        cfg = _alembic_config(tmp_db_path)
        command.upgrade(cfg, "0012_chapter_summary_table")
        _seed_workspace(tmp_db_path, "ws-null")

        engine = create_engine(f"sqlite:///{tmp_db_path}")
        table = _chapter_summaries_table(engine)
        with engine.begin() as conn:
            conn.execute(
                table.insert().values(
                    **_summary_values(
                        chapter_id="ch-null-fields",
                        workspace_id="ws-null",
                        pov_character=None,
                        scene_setting=None,
                    )
                )
            )
            row = conn.execute(
                select(table.c.pov_character, table.c.scene_setting).where(
                    table.c.chapter_id == "ch-null-fields"
                )
            ).one()
        assert row.pov_character is None
        assert row.scene_setting is None

    def test_json_columns_round_trip(self, tmp_db_path: Path) -> None:
        """``key_events`` / ``character_interactions`` / ``unfinished_actions``
        三个 JSON 字段 round-trip 保留结构。"""
        cfg = _alembic_config(tmp_db_path)
        command.upgrade(cfg, "0012_chapter_summary_table")
        _seed_workspace(tmp_db_path, "ws-json")

        engine = create_engine(f"sqlite:///{tmp_db_path}")
        key_events = [
            {"description": "灰雀越窗", "actors": ["lin-yan"], "location": "廊外"},
            {"description": "玉佩落地", "actors": ["lin-yan", "shen-zhao"]},
        ]
        interactions = [{"char_a": "lin-yan", "char_b": "shen-zhao", "tone": "试探"}]
        unfinished = ["蝉鸣未止", "灰雀飞向琉璃瓦"]

        table = _chapter_summaries_table(engine)
        with engine.begin() as conn:
            conn.execute(
                table.insert().values(
                    **_summary_values(
                        chapter_id="ch-json-rt",
                        workspace_id="ws-json",
                        key_events=key_events,
                        character_interactions=interactions,
                        unfinished_actions=unfinished,
                    )
                )
            )
            row = conn.execute(
                select(
                    table.c.key_events,
                    table.c.character_interactions,
                    table.c.unfinished_actions,
                ).where(table.c.chapter_id == "ch-json-rt")
            ).one()
        assert row.key_events == key_events
        assert row.character_interactions == interactions
        assert row.unfinished_actions == unfinished

    def test_downgrade_drops_table(self, tmp_db_path: Path) -> None:
        """0012 → 0011 downgrade 后 chapter_summaries 表消失,既有表保留。"""
        cfg = _alembic_config(tmp_db_path)
        command.upgrade(cfg, "0012_chapter_summary_table")
        # 验证表存在
        engine = create_engine(f"sqlite:///{tmp_db_path}")
        inspector = inspect(engine)
        assert "chapter_summaries" in inspector.get_table_names()

        command.downgrade(cfg, "-1")

        # 重新 inspect 避免 cache
        engine2 = create_engine(f"sqlite:///{tmp_db_path}")
        inspector2 = inspect(engine2)
        tables = set(inspector2.get_table_names())
        assert "chapter_summaries" not in tables
        # 0011 时既有表保留(workspaces / plot_threads / etc)
        assert "workspaces" in tables
        assert "plot_threads" in tables

    def test_round_trip_upgrade_downgrade_upgrade_idempotent(
        self, tmp_db_path: Path
    ) -> None:
        """0012 → 0011 → 0012 幂等(显式 revision pin 防 head 漂移)。"""
        cfg = _alembic_config(tmp_db_path)
        command.upgrade(cfg, "0012_chapter_summary_table")
        command.downgrade(cfg, "-1")
        command.upgrade(cfg, "0012_chapter_summary_table")

        engine = create_engine(f"sqlite:///{tmp_db_path}")
        inspector = inspect(engine)
        assert "chapter_summaries" in inspector.get_table_names()
        # CHECK 约束在 round-trip 后仍有效
        _seed_workspace(tmp_db_path, "ws-rt")
        table = _chapter_summaries_table(engine)
        with pytest.raises(IntegrityError), engine.begin() as conn:
            conn.execute(
                table.insert().values(
                    **_summary_values(
                        chapter_id="ch-rt-bad",
                        workspace_id="ws-rt",
                        trigger="invalid",
                    )
                )
            )
