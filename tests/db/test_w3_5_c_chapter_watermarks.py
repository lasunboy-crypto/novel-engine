"""W3.5-C ``chapters`` 写作水位列迁移测试。

覆盖:
- 0025 upgrade 后新增 ready / memory_enriched 四列;
- ``memory_enrichment_status`` server_default 填 ``pending``;
- downgrade 回 0024 后列消失。
"""

from __future__ import annotations

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


def _columns(db_path: Path, table: str) -> dict[str, dict]:
    engine = create_engine(f"sqlite:///{db_path}")
    return {c["name"]: c for c in inspect(engine).get_columns(table)}


def _seed_workspace_and_chapter(db_path: Path) -> None:
    engine = create_engine(f"sqlite:///{db_path}")
    now = datetime.now(timezone.utc).isoformat()
    with engine.begin() as conn:
        conn.execute(
            text(
                "INSERT INTO workspaces (workspace_id, display_name, genre, "
                "premise, created_at, last_active_at, engine_version, status, "
                "total_chapters, completed_chapters, config_overrides, "
                "default_voice) VALUES ('ws1','名','xianxia','一句话',:t,:t,"
                "'0.1.0','active',3,0,'{}','third_person')"
            ),
            {"t": now},
        )
        conn.execute(
            text(
                "INSERT INTO chapters (chapter_id, workspace_id, "
                "current_version, status, rewrite_count, revise_count, "
                "created_at, approved_at, total_token_usage, total_cost_usd, "
                "scene_anchor) VALUES ('ch-0001','ws1',1,'approved',0,0,"
                ":t,:t,'{}',0.0,'{}')"
            ),
            {"t": now},
        )


def test_upgrade_adds_watermark_columns(tmp_db_path: Path) -> None:
    cfg = _alembic_config(tmp_db_path)
    command.upgrade(cfg, "0025_chapter_writing_watermarks")

    cols = _columns(tmp_db_path, "chapters")
    assert "ready_for_next_chapter_at" in cols
    assert "memory_enrichment_status" in cols
    assert cols["memory_enrichment_status"]["nullable"] is False
    assert "memory_enriched_at" in cols
    assert "memory_enrichment_details" in cols


def test_existing_insert_gets_pending_default(tmp_db_path: Path) -> None:
    cfg = _alembic_config(tmp_db_path)
    command.upgrade(cfg, "0025_chapter_writing_watermarks")
    _seed_workspace_and_chapter(tmp_db_path)

    engine = create_engine(f"sqlite:///{tmp_db_path}")
    with engine.connect() as conn:
        row = conn.execute(
            text(
                "SELECT ready_for_next_chapter_at, memory_enrichment_status, "
                "memory_enriched_at, memory_enrichment_details FROM chapters "
                "WHERE chapter_id='ch-0001'"
            )
        ).one()
    assert row[0] is None
    assert row[1] == "pending"
    assert row[2] is None
    assert row[3] is None


def test_downgrade_drops_watermark_columns(tmp_db_path: Path) -> None:
    cfg = _alembic_config(tmp_db_path)
    command.upgrade(cfg, "0025_chapter_writing_watermarks")
    assert "ready_for_next_chapter_at" in _columns(tmp_db_path, "chapters")

    command.downgrade(cfg, "0024_pipeline_runs_state_interrupted")

    cols = _columns(tmp_db_path, "chapters")
    assert "ready_for_next_chapter_at" not in cols
    assert "memory_enrichment_status" not in cols
    assert "memory_enriched_at" not in cols
    assert "memory_enrichment_details" not in cols
