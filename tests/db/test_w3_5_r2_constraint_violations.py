"""W3.5-R2 ``review_results.constraint_violations`` 迁移测试。"""

from __future__ import annotations

from datetime import datetime, timezone
from pathlib import Path

import pytest
from alembic import command
from alembic.config import Config
from sqlalchemy import create_engine, inspect, text


_REV_R2 = "0026_review_results_constraint_violations"
_REV_PREV = "0025_chapter_writing_watermarks"


def _alembic_config(db_path: Path) -> Config:
    cfg = Config("alembic.ini")
    cfg.set_main_option("sqlalchemy.url", f"sqlite:///{db_path}")
    return cfg


@pytest.fixture
def tmp_db_path(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> Path:
    db = tmp_path / "novel.db"
    monkeypatch.setenv("NOVEL_DB_PATH", str(db))
    return db


def _columns(db_path: Path) -> dict[str, dict]:
    engine = create_engine(f"sqlite:///{db_path}")
    return {
        column["name"]: column
        for column in inspect(engine).get_columns("review_results")
    }


def _seed_review_before_r2(db_path: Path) -> None:
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
        conn.execute(
            text(
                "INSERT INTO review_results (review_id, chapter_id, "
                "version_reviewed, reviewed_at, reviewer_model, "
                "score_plot_progression, score_tension_match, "
                "score_prose_quality, score_consistency, "
                "score_hook_management, score_voice_consistency, "
                "score_overall, hard_constraints_passed, failed_constraints, "
                "suggestions, verdict) VALUES ('rv1','ch-0001',1,:t,'mock',"
                "8,8,8,8,8,8,8,1,'[]','[]','pass')"
            ),
            {"t": now},
        )


def test_upgrade_adds_non_null_json_column(tmp_db_path: Path) -> None:
    cfg = _alembic_config(tmp_db_path)
    command.upgrade(cfg, _REV_R2)
    column = _columns(tmp_db_path)["constraint_violations"]
    assert column["nullable"] is False


def test_existing_review_gets_empty_array_default(tmp_db_path: Path) -> None:
    cfg = _alembic_config(tmp_db_path)
    command.upgrade(cfg, _REV_PREV)
    _seed_review_before_r2(tmp_db_path)
    command.upgrade(cfg, _REV_R2)

    engine = create_engine(f"sqlite:///{tmp_db_path}")
    with engine.connect() as conn:
        value = conn.execute(
            text(
                "SELECT constraint_violations FROM review_results "
                "WHERE review_id='rv1'"
            )
        ).scalar_one()
    assert value == "[]"


def test_downgrade_drops_constraint_violations(tmp_db_path: Path) -> None:
    cfg = _alembic_config(tmp_db_path)
    command.upgrade(cfg, _REV_R2)
    assert "constraint_violations" in _columns(tmp_db_path)

    command.downgrade(cfg, _REV_PREV)
    assert "constraint_violations" not in _columns(tmp_db_path)
