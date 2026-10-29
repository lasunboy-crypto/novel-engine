"""W3.5-R6 review constraint results / regression ID 迁移测试。"""

from __future__ import annotations

from datetime import datetime, timezone
from pathlib import Path

import pytest
from alembic import command
from alembic.config import Config
from sqlalchemy import create_engine, inspect, text


_REV_R6 = "0029_structured_rewrite_constraint_results"
_REV_PREV = "0028_persistent_attempt_ledger"
_REV_SEED = "0025_chapter_writing_watermarks"


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


def _seed_review(db_path: Path) -> None:
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


def test_upgrade_adds_non_null_json_columns(tmp_db_path: Path) -> None:
    command.upgrade(_alembic_config(tmp_db_path), _REV_R6)
    columns = _columns(tmp_db_path)
    assert columns["constraint_results"]["nullable"] is False
    assert columns["hard_constraint_regressions"]["nullable"] is False


def test_existing_review_gets_empty_arrays_without_string_backfill(
    tmp_db_path: Path,
) -> None:
    cfg = _alembic_config(tmp_db_path)
    command.upgrade(cfg, _REV_SEED)
    _seed_review(tmp_db_path)
    command.upgrade(cfg, _REV_R6)

    engine = create_engine(f"sqlite:///{tmp_db_path}")
    with engine.connect() as conn:
        row = conn.execute(
            text(
                "SELECT constraint_results, hard_constraint_regressions "
                "FROM review_results WHERE review_id='rv1'"
            )
        ).one()
    assert row == ("[]", "[]")


def test_downgrade_drops_only_r6_columns(tmp_db_path: Path) -> None:
    cfg = _alembic_config(tmp_db_path)
    command.upgrade(cfg, _REV_R6)
    command.downgrade(cfg, _REV_PREV)
    columns = _columns(tmp_db_path)
    assert "constraint_results" not in columns
    assert "hard_constraint_regressions" not in columns
    assert "constraint_violations" in columns
