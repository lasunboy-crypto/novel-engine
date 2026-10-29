"""0033 physical provider attempt outcome migration regression."""

from __future__ import annotations

from datetime import UTC, datetime
from pathlib import Path

import pytest
from alembic import command
from alembic.config import Config
from sqlalchemy import create_engine, inspect, text

from src.core.db.models import AttemptLedgerORM


_PREVIOUS_REVISION = "0032_core_state_book_clock"
_HEAD_REVISION = "0033_provider_attempt_outcomes"
_OUTCOME_COLUMNS = {
    "usage_status",
    "input_tokens",
    "output_tokens",
    "reserved_input_tokens",
    "reserved_output_tokens",
    "configured_cost_currency",
    "configured_input_cost_per_1k",
    "configured_output_cost_per_1k",
    "configured_cost_amount",
    "configured_cost_basis",
    "provider_request_id",
    "response_status",
}


def _alembic_config(db_path: Path) -> Config:
    repo_root = Path(__file__).resolve().parents[2]
    config = Config(str(repo_root / "alembic.ini"))
    config.set_main_option("script_location", str(repo_root / "alembic"))
    config.set_main_option("sqlalchemy.url", f"sqlite:///{db_path}")
    return config


@pytest.fixture
def tmp_db_path(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> Path:
    db_path = tmp_path / "novel.db"
    monkeypatch.setenv("NOVEL_DB_PATH", str(db_path))
    return db_path


def _columns(db_path: Path) -> set[str]:
    engine = create_engine(f"sqlite:///{db_path}")
    return {
        str(column["name"])
        for column in inspect(engine).get_columns("attempt_ledger")
    }


def _seed_historical_attempt(db_path: Path) -> None:
    now = datetime.now(UTC).isoformat()
    engine = create_engine(f"sqlite:///{db_path}")
    with engine.begin() as connection:
        connection.execute(
            text(
                "INSERT INTO attempt_ledger ("
                "attempt_id,workspace_id,run_id,task_id,chapter_id,attempt_kind,"
                "operation_kind,logical_call_id,agent_name,provider,model,"
                "attempt_number,attempt_cap,status,version_before,version_after,"
                "error_type,created_at,completed_at) VALUES ("
                "'att-history','ws-history','run-history','task-history',NULL,"
                "'provider','agent_call','call-history','reviewer',"
                "'openai_compatible','qwen3.8-max',1,10,'started',NULL,NULL,NULL,"
                ":now,NULL)"
            ),
            {"now": now},
        )


def test_upgrade_adds_outcome_columns_without_backfilling_history(
    tmp_db_path: Path,
) -> None:
    config = _alembic_config(tmp_db_path)
    command.upgrade(config, _PREVIOUS_REVISION)
    _seed_historical_attempt(tmp_db_path)

    command.upgrade(config, "head")

    assert _HEAD_REVISION in {
        row[0]
        for row in create_engine(f"sqlite:///{tmp_db_path}")
        .connect()
        .execute(text("SELECT version_num FROM alembic_version"))
    }
    assert _OUTCOME_COLUMNS <= _columns(tmp_db_path)
    engine = create_engine(f"sqlite:///{tmp_db_path}")
    with engine.connect() as connection:
        historical = connection.execute(
            text(
                "SELECT "
                + ",".join(sorted(_OUTCOME_COLUMNS))
                + " FROM attempt_ledger WHERE attempt_id='att-history'"
            )
        ).one()
    assert all(value is None for value in historical)


def test_head_columns_match_attempt_orm(tmp_db_path: Path) -> None:
    command.upgrade(_alembic_config(tmp_db_path), "head")
    assert _columns(tmp_db_path) == {
        column.name for column in AttemptLedgerORM.__table__.columns
    }


def test_downgrade_removes_only_outcome_columns(tmp_db_path: Path) -> None:
    config = _alembic_config(tmp_db_path)
    command.upgrade(config, "head")
    command.downgrade(config, _PREVIOUS_REVISION)
    assert not (_OUTCOME_COLUMNS & _columns(tmp_db_path))
