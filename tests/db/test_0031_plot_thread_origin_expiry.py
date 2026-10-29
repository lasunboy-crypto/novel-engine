"""验证 0031 PlotThread 来源/休眠策略迁移与历史主伏笔保护。"""

from __future__ import annotations

import sqlite3
from pathlib import Path

import pytest
from alembic import command
from alembic.config import Config
from sqlalchemy import create_engine, inspect


_REPO_ROOT = Path(__file__).resolve().parents[2]
_PREVIOUS_REVISION = "0030_r8_chapter_operation_leases"
_HEAD_REVISION = "0033_provider_attempt_outcomes"


def _alembic_config() -> Config:
    config = Config(str(_REPO_ROOT / "alembic.ini"))
    config.set_main_option("script_location", str(_REPO_ROOT / "alembic"))
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


def _seed_legacy_thread(db_path: Path) -> None:
    with sqlite3.connect(db_path) as connection:
        connection.execute(
            """
            INSERT INTO plot_threads (
                thread_id, workspace_id, title, thread_type, status, importance,
                planted_chapter, planned_payoff, actual_payoff, description,
                related_characters, created_at, last_updated_chapter, revoked_at
            ) VALUES (
                'legacy-main', 'ws-legacy', '历史主伏笔', 'foreshadowing',
                'planted', 5, NULL, 'ch-0012', NULL, '大纲阶段已规划',
                '[]', '2026-08-16 00:00:00', NULL, NULL
            )
            """
        )


def test_upgrade_backfills_legacy_rows_as_outline_never(tmp_db_path: Path) -> None:
    config = _alembic_config()
    command.upgrade(config, _PREVIOUS_REVISION)
    _seed_legacy_thread(tmp_db_path)

    command.upgrade(config, "head")

    assert _current_revision(tmp_db_path) == _HEAD_REVISION
    with sqlite3.connect(tmp_db_path) as connection:
        row = connection.execute(
            "SELECT origin, expiry_policy, source_evidence "
            "FROM plot_threads WHERE thread_id='legacy-main'"
        ).fetchone()
    assert row == ("outline", "never", None)


def test_head_schema_has_exact_columns_defaults_and_checks(tmp_db_path: Path) -> None:
    config = _alembic_config()
    command.upgrade(config, "head")
    inspector = inspect(create_engine(f"sqlite:///{tmp_db_path}"))
    columns = {
        column["name"]: column
        for column in inspector.get_columns("plot_threads")
    }
    assert columns["origin"]["nullable"] is False
    assert "outline" in str(columns["origin"]["default"])
    assert columns["expiry_policy"]["nullable"] is False
    assert "never" in str(columns["expiry_policy"]["default"])
    assert columns["source_evidence"]["nullable"] is True
    check_names = {
        check["name"] for check in inspector.get_check_constraints("plot_threads")
    }
    assert {
        "ck_plot_threads_origin",
        "ck_plot_threads_expiry_policy",
    }.issubset(check_names)


def test_checks_reject_unknown_internal_values(tmp_db_path: Path) -> None:
    config = _alembic_config()
    command.upgrade(config, "head")
    with sqlite3.connect(tmp_db_path) as connection:
        with pytest.raises(sqlite3.IntegrityError):
            connection.execute(
                """
                INSERT INTO plot_threads (
                    thread_id, workspace_id, title, status, importance,
                    description, related_characters, created_at, origin,
                    expiry_policy
                ) VALUES (
                    'bad', 'ws', '坏数据', 'planted', 4, '坏数据', '[]',
                    '2026-08-16 00:00:00', 'canon', 'chapter_end'
                )
                """
            )


def test_downgrade_removes_metadata_but_preserves_thread(tmp_db_path: Path) -> None:
    config = _alembic_config()
    command.upgrade(config, _PREVIOUS_REVISION)
    _seed_legacy_thread(tmp_db_path)
    command.upgrade(config, "head")

    command.downgrade(config, _PREVIOUS_REVISION)

    inspector = inspect(create_engine(f"sqlite:///{tmp_db_path}"))
    columns = {
        column["name"] for column in inspector.get_columns("plot_threads")
    }
    assert not {"origin", "expiry_policy", "source_evidence"} & columns
    with sqlite3.connect(tmp_db_path) as connection:
        row = connection.execute(
            "SELECT title FROM plot_threads WHERE thread_id='legacy-main'"
        ).fetchone()
    assert row == ("历史主伏笔",)
    assert _current_revision(tmp_db_path) == _PREVIOUS_REVISION
