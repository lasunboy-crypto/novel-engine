"""0032 核心状态/书内时钟账本迁移回归。"""

from __future__ import annotations

import sqlite3
from datetime import UTC, datetime
from pathlib import Path

import pytest
from alembic import command
from alembic.config import Config
from sqlalchemy import create_engine, inspect, text


_REPO_ROOT = Path(__file__).resolve().parents[2]
_PREVIOUS_REVISION = "0031_plot_thread_origin_expiry"
_HEAD_REVISION = "0033_provider_attempt_outcomes"


def _alembic_config(db_path: Path) -> Config:
    config = Config(str(_REPO_ROOT / "alembic.ini"))
    config.set_main_option("script_location", str(_REPO_ROOT / "alembic"))
    config.set_main_option("sqlalchemy.url", f"sqlite:///{db_path}")
    return config


@pytest.fixture
def tmp_db_path(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> Path:
    db_path = tmp_path / "novel.db"
    monkeypatch.setenv("NOVEL_DB_PATH", str(db_path))
    return db_path


def _current_revision(db_path: Path) -> str:
    with sqlite3.connect(db_path) as connection:
        row = connection.execute("SELECT version_num FROM alembic_version").fetchone()
    assert row is not None
    return str(row[0])


def _seed_pre_0032_rows(db_path: Path) -> None:
    now = datetime.now(UTC).isoformat()
    engine = create_engine(f"sqlite:///{db_path}")
    with engine.begin() as connection:
        connection.execute(
            text(
                "INSERT INTO workspaces (workspace_id, display_name, genre, "
                "premise, created_at, last_active_at, engine_version, status, "
                "total_chapters, completed_chapters, config_overrides, "
                "outline_status, default_voice) VALUES "
                "('ws-core','核心状态','test','p',:now,:now,'test',"
                "'active',2,1,'{}','committed','third_person')"
            ),
            {"now": now},
        )
        connection.execute(
            text(
                "INSERT INTO chapters (chapter_id, workspace_id, current_version, "
                "status, rewrite_count, revise_count, created_at, approved_at, "
                "total_token_usage, total_cost_usd, scene_anchor) VALUES "
                "('ch-0001','ws-core',1,'approved',0,0,:now,:now,'{}',0.0,'{}')"
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
                "('ch-0001','ws-core','旧摘要','lin-yan','井房','[]','[]','[]',"
                "'reviewer_pass','mock',:now,:now)"
            ),
            {"now": now},
        )


def test_upgrade_backfills_legacy_rows_without_claiming_committed(
    tmp_db_path: Path,
) -> None:
    config = _alembic_config(tmp_db_path)
    command.upgrade(config, _PREVIOUS_REVISION)
    _seed_pre_0032_rows(tmp_db_path)

    command.upgrade(config, "head")

    assert _current_revision(tmp_db_path) == _HEAD_REVISION
    engine = create_engine(f"sqlite:///{tmp_db_path}")
    with engine.connect() as connection:
        chapter = connection.execute(
            text(
                "SELECT core_state_commit_hash, core_state_commit_status, "
                "core_state_commit_details FROM chapters WHERE chapter_id='ch-0001'"
            )
        ).one()
        summary = connection.execute(
            text(
                "SELECT core_state_candidates FROM chapter_summaries "
                "WHERE chapter_id='ch-0001'"
            )
        ).one()
    assert tuple(chapter) == ("", "disabled", "{}")
    assert tuple(summary) == ("[]",)


def test_head_schema_has_exact_revision_shape_and_partial_unique_index(
    tmp_db_path: Path,
) -> None:
    command.upgrade(_alembic_config(tmp_db_path), "head")
    inspector = inspect(create_engine(f"sqlite:///{tmp_db_path}"))

    assert {
        "core_state_commit_hash",
        "core_state_commit_status",
        "core_state_commit_details",
    } <= {column["name"] for column in inspector.get_columns("chapters")}
    assert "core_state_candidates" in {
        column["name"] for column in inspector.get_columns("chapter_summaries")
    }
    assert {
        column["name"] for column in inspector.get_columns("core_state_revisions")
    } == {
        "revision_id",
        "workspace_id",
        "state_key",
        "entity_type",
        "entity_id",
        "attribute",
        "value_json",
        "value_text",
        "source_kind",
        "source_chapter_id",
        "source_version_id",
        "source_path",
        "source_text_hash",
        "evidence_quote",
        "valid_from_chapter",
        "valid_to_chapter",
        "supersedes_revision_id",
        "status",
        "change_reason",
        "ledger_hash",
        "created_at",
        "retracted_at",
    }
    indexes = {
        index["name"]: (tuple(index["column_names"]), bool(index["unique"]))
        for index in inspector.get_indexes("core_state_revisions")
    }
    assert indexes["uq_core_state_revisions_one_active"] == (
        ("workspace_id", "state_key"),
        True,
    )


def test_database_rejects_two_active_values_for_same_state_key(
    tmp_db_path: Path,
) -> None:
    config = _alembic_config(tmp_db_path)
    command.upgrade(config, _PREVIOUS_REVISION)
    _seed_pre_0032_rows(tmp_db_path)
    command.upgrade(config, "head")
    values = (
        "ws-core",
        "object:青铜镜:surface",
        "object",
        "青铜镜",
        "surface",
        '{"value":"无锈"}',
        "无锈",
        "approved_text",
        "ch-0001",
        1,
        "chapter_summaries/ch-0001.core",
        "a" * 64,
        "镜面无锈。",
        1,
        None,
        None,
        "active",
        "首次建立",
        "b" * 64,
        datetime.now(UTC).isoformat(),
        None,
    )
    sql = (
        "INSERT INTO core_state_revisions "
        "(revision_id, workspace_id, state_key, entity_type, entity_id, "
        "attribute, value_json, value_text, source_kind, source_chapter_id, "
        "source_version_id, source_path, source_text_hash, evidence_quote, "
        "valid_from_chapter, valid_to_chapter, supersedes_revision_id, status, "
        "change_reason, ledger_hash, created_at, retracted_at) VALUES "
        "(?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)"
    )
    with sqlite3.connect(tmp_db_path) as connection:
        connection.execute(sql, ("csr-first", *values))
        with pytest.raises(sqlite3.IntegrityError):
            connection.execute(sql, ("csr-second", *values))


def test_downgrade_removes_only_0032_artifacts_and_preserves_rows(
    tmp_db_path: Path,
) -> None:
    config = _alembic_config(tmp_db_path)
    command.upgrade(config, _PREVIOUS_REVISION)
    _seed_pre_0032_rows(tmp_db_path)
    command.upgrade(config, "head")

    command.downgrade(config, _PREVIOUS_REVISION)

    inspector = inspect(create_engine(f"sqlite:///{tmp_db_path}"))
    assert "core_state_revisions" not in inspector.get_table_names()
    assert not {
        "core_state_commit_hash",
        "core_state_commit_status",
        "core_state_commit_details",
    } & {column["name"] for column in inspector.get_columns("chapters")}
    assert "core_state_candidates" not in {
        column["name"] for column in inspector.get_columns("chapter_summaries")
    }
    with sqlite3.connect(tmp_db_path) as connection:
        assert connection.execute(
            "SELECT status FROM chapters WHERE chapter_id='ch-0001'"
        ).fetchone() == ("approved",)
        assert connection.execute(
            "SELECT summary_text FROM chapter_summaries WHERE chapter_id='ch-0001'"
        ).fetchone() == ("旧摘要",)
    assert _current_revision(tmp_db_path) == _PREVIOUS_REVISION
