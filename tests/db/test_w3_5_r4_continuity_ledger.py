"""W3.5-R4 ``chapter_summaries`` 连续性小账本迁移回归。"""

from __future__ import annotations

from datetime import UTC, datetime
from pathlib import Path

import pytest
from alembic import command
from alembic.config import Config
from sqlalchemy import create_engine, inspect, text

from src.core.db.models import ChapterSummaryORM


_PREVIOUS_REVISION = "0026_review_results_constraint_violations"
_R4_REVISION = "0027_continuity_ledger_provenance"
_R4_COLUMNS = {
    "character_states",
    "established_facts",
    "revealed_mechanics",
    "raw_final_tail",
    "source_version_id",
    "source_text_hash",
    "evidence_records",
    "ledger_hash",
    "validated_at",
}


def _alembic_config(db_path: Path) -> Config:
    repo_root = Path(__file__).resolve().parents[2]
    cfg = Config(str(repo_root / "alembic.ini"))
    cfg.set_main_option("script_location", str(repo_root / "alembic"))
    cfg.set_main_option("sqlalchemy.url", f"sqlite:///{db_path}")
    return cfg


@pytest.fixture
def tmp_db_path(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> Path:
    db_path = tmp_path / "novel.db"
    monkeypatch.setenv("NOVEL_DB_PATH", str(db_path))
    return db_path


def _columns(db_path: Path) -> set[str]:
    engine = create_engine(f"sqlite:///{db_path}")
    return {
        column["name"]
        for column in inspect(engine).get_columns("chapter_summaries")
    }


def _seed_pre_r4_summary(db_path: Path) -> None:
    engine = create_engine(f"sqlite:///{db_path}")
    now = datetime.now(UTC).isoformat()
    with engine.begin() as connection:
        connection.execute(
            text(
                "INSERT INTO workspaces (workspace_id, display_name, genre, "
                "premise, created_at, last_active_at, engine_version, status, "
                "total_chapters, completed_chapters, config_overrides, "
                "outline_status, default_voice) VALUES "
                "('ws-r4','R4','test','p',:now,:now,'0.1.0','active',2,1,"
                "'{}','committed','third_person')"
            ),
            {"now": now},
        )
        connection.execute(
            text(
                "INSERT INTO chapter_summaries "
                "(chapter_id, workspace_id, summary_text, pov_character, "
                "scene_setting, key_events, character_interactions, "
                "unfinished_actions, extraction_trigger, model_used, "
                "created_at, updated_at) VALUES "
                "('ch-0001','ws-r4','旧摘要','lin-yan','山门','[]','[]','[]',"
                "'reviewer_pass','mock',:now,:now)"
            ),
            {"now": now},
        )


def test_upgrade_adds_r4_columns_and_old_rows_remain_unvalidated(
    tmp_db_path: Path,
) -> None:
    cfg = _alembic_config(tmp_db_path)
    command.upgrade(cfg, _PREVIOUS_REVISION)
    _seed_pre_r4_summary(tmp_db_path)

    command.upgrade(cfg, _R4_REVISION)
    assert _R4_COLUMNS <= _columns(tmp_db_path)

    engine = create_engine(f"sqlite:///{tmp_db_path}")
    with engine.connect() as connection:
        row = connection.execute(
            text(
                "SELECT character_states, established_facts, "
                "revealed_mechanics, raw_final_tail, source_version_id, "
                "source_text_hash, evidence_records, ledger_hash, validated_at "
                "FROM chapter_summaries WHERE chapter_id='ch-0001'"
            )
        ).one()
    assert tuple(row[:3]) == ("[]", "[]", "[]")
    assert row[3] == ""
    assert row[4] == 0
    assert row[5] == ""
    assert row[6] == "[]"
    assert row[7] == ""
    assert row[8] is None


def test_head_columns_match_current_orm(tmp_db_path: Path) -> None:
    command.upgrade(_alembic_config(tmp_db_path), "head")
    assert _columns(tmp_db_path) == {
        column.name for column in ChapterSummaryORM.__table__.columns
    }


def test_downgrade_to_0026_drops_only_r4_columns(tmp_db_path: Path) -> None:
    cfg = _alembic_config(tmp_db_path)
    command.upgrade(cfg, _R4_REVISION)
    assert _R4_COLUMNS <= _columns(tmp_db_path)

    command.downgrade(cfg, _PREVIOUS_REVISION)
    columns = _columns(tmp_db_path)
    assert not (_R4_COLUMNS & columns)
    assert {
        "chapter_id",
        "summary_text",
        "unfinished_actions",
        "created_at",
    } <= columns
