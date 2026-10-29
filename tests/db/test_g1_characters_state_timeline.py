"""G1(v1.1 wave 4 session 1):0016 ``characters.state_timeline`` 迁移测试。

验证 0016 迁移:
1. upgrade 后 ``characters`` 有 ``state_timeline`` 列(nullable=False);
2. upgrade 后既有 character 行 backfill 为空数组(server_default '[]' 生效);
3. downgrade 回 0015 后列被 drop。

**Alembic 纪律自检**(CLAUDE.md §实施纪律):downgrade target 用**显式 revision**
``0015_workspace_default_voice``(非 ``-1``),后续 head 推进不影响本测试。
"""
from __future__ import annotations

import json
from datetime import datetime, timezone
from pathlib import Path

import pytest
from alembic import command
from alembic.config import Config
from sqlalchemy import create_engine, inspect, text


def _alembic_config(db_path: Path) -> Config:
    cfg = Config("alembic.ini")
    cfg.set_main_option("sqlalchemy.url", f"sqlite:///{db_path}")
    return cfg


@pytest.fixture
def tmp_db_path(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> Path:
    db = tmp_path / "novel.db"
    monkeypatch.setenv("NOVEL_DB_PATH", str(db))
    return db


def _columns(db_path: Path, table: str) -> dict:
    engine = create_engine(f"sqlite:///{db_path}")
    return {c["name"]: c for c in inspect(engine).get_columns(table)}


def test_upgrade_adds_state_timeline_column(tmp_db_path: Path) -> None:
    """upgrade 0016 后 ``characters.state_timeline`` 列存在且 NOT NULL。"""
    cfg = _alembic_config(tmp_db_path)
    command.upgrade(cfg, "0016_characters_state_timeline")
    cols = _columns(tmp_db_path, "characters")
    assert "state_timeline" in cols
    assert cols["state_timeline"]["nullable"] is False


def test_existing_row_backfilled_empty_array(tmp_db_path: Path) -> None:
    """0015 时插入的 character 行,upgrade 0016 后 state_timeline backfill 为 '[]'。"""
    cfg = _alembic_config(tmp_db_path)
    command.upgrade(cfg, "0015_workspace_default_voice")
    engine = create_engine(f"sqlite:///{tmp_db_path}")
    now = datetime.now(timezone.utc).isoformat()
    with engine.begin() as conn:
        conn.execute(
            text(
                "INSERT INTO workspaces (workspace_id, display_name, genre, "
                "premise, created_at, last_active_at, engine_version, status, "
                "total_chapters, completed_chapters, config_overrides, default_voice) "
                "VALUES ('ws1','名','xianxia','一句话',:t,:t,'0.1.0','active',0,0,"
                "'{}','third_person')"
            ),
            {"t": now},
        )
        conn.execute(
            text(
                "INSERT INTO characters (character_id, workspace_id, name, aliases, "
                "archetype, appearance, personality, background, voice_profile, "
                "goals_long_term, goals_short_term, current_state, relationships, "
                "abilities, secrets) VALUES ('c1','ws1','林衍','[]','剑修','白衣',"
                "'沉静','寒门','{}','[]','[]','{}','{}','[]','[]')"
            )
        )
    command.upgrade(cfg, "0016_characters_state_timeline")
    with engine.connect() as conn:
        row = conn.execute(
            text("SELECT state_timeline FROM characters WHERE character_id='c1'")
        ).scalar_one()
    assert json.loads(row) == []


def test_downgrade_drops_column(tmp_db_path: Path) -> None:
    """downgrade 回 0015 后 ``state_timeline`` 列被 drop。"""
    cfg = _alembic_config(tmp_db_path)
    command.upgrade(cfg, "0016_characters_state_timeline")
    assert "state_timeline" in _columns(tmp_db_path, "characters")
    command.downgrade(cfg, "0015_workspace_default_voice")
    assert "state_timeline" not in _columns(tmp_db_path, "characters")
