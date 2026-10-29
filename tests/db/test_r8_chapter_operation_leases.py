"""验证 R8 0030 章节操作租约迁移的形状、兼容与回退。"""

from __future__ import annotations

import sqlite3
from pathlib import Path

import pytest
from alembic import command
from alembic.config import Config
from sqlalchemy import create_engine, inspect

from src.core.db import Base


_REPO_ROOT = Path(__file__).resolve().parents[2]
_PREVIOUS_REVISION = "0029_structured_rewrite_constraint_results"
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


def _assert_lease_table_shape(db_path: Path) -> None:
    engine = create_engine(f"sqlite:///{db_path}")
    inspector = inspect(engine)
    assert {column["name"] for column in inspector.get_columns(
        "chapter_operation_leases"
    )} == {
        "workspace_id",
        "chapter_id",
        "lease_id",
        "operation_kind",
        "source_version",
        "task_id",
        "run_id",
        "phase",
        "created_at",
        "updated_at",
    }
    assert inspector.get_pk_constraint("chapter_operation_leases")[
        "constrained_columns"
    ] == ["workspace_id", "chapter_id"]
    assert {
        tuple(item["column_names"])
        for item in inspector.get_unique_constraints(
            "chapter_operation_leases"
        )
    } == {("lease_id",)}
    foreign_keys = inspector.get_foreign_keys("chapter_operation_leases")
    assert len(foreign_keys) == 1
    assert foreign_keys[0]["constrained_columns"] == ["workspace_id"]
    assert foreign_keys[0]["referred_table"] == "workspaces"
    assert foreign_keys[0]["referred_columns"] == ["workspace_id"]
    assert foreign_keys[0]["options"]["ondelete"] == "CASCADE"
    assert {
        (item["name"], tuple(item["column_names"]), bool(item["unique"]))
        for item in inspector.get_indexes("chapter_operation_leases")
    } == {
        ("ix_chapter_operation_leases_task_id", ("task_id",), False)
    }


def _create_malformed_preexisting_table(db_path: Path, *, omit: str) -> None:
    constraints: list[str] = []
    if omit != "primary_key":
        constraints.append("PRIMARY KEY (workspace_id, chapter_id)")
    if omit != "unique":
        constraints.append("UNIQUE (lease_id)")
    if omit != "foreign_key":
        constraints.append(
            "FOREIGN KEY (workspace_id) REFERENCES workspaces(workspace_id) "
            "ON DELETE CASCADE"
        )
    constraint_sql = ",\n                ".join(constraints)
    with sqlite3.connect(db_path) as connection:
        connection.execute(
            f"""
            CREATE TABLE chapter_operation_leases (
                workspace_id VARCHAR NOT NULL,
                chapter_id VARCHAR NOT NULL,
                lease_id VARCHAR NOT NULL,
                operation_kind VARCHAR NOT NULL,
                source_version INTEGER NOT NULL,
                task_id VARCHAR NOT NULL,
                run_id VARCHAR,
                phase VARCHAR NOT NULL,
                created_at DATETIME NOT NULL,
                updated_at DATETIME NOT NULL,
                {constraint_sql}
            )
            """
        )
        if omit != "index":
            connection.execute(
                "CREATE INDEX ix_chapter_operation_leases_task_id "
                "ON chapter_operation_leases (task_id)"
            )


def test_upgrade_from_0029_creates_exact_lease_schema(tmp_db_path: Path) -> None:
    config = _alembic_config()
    command.upgrade(config, _PREVIOUS_REVISION)

    command.upgrade(config, "head")

    assert _current_revision(tmp_db_path) == _HEAD_REVISION
    _assert_lease_table_shape(tmp_db_path)


def test_upgrade_accepts_create_all_precreated_same_shape(tmp_db_path: Path) -> None:
    config = _alembic_config()
    command.upgrade(config, _PREVIOUS_REVISION)
    engine = create_engine(f"sqlite:///{tmp_db_path}")
    Base.metadata.create_all(engine)
    _assert_lease_table_shape(tmp_db_path)

    command.upgrade(config, "head")

    assert _current_revision(tmp_db_path) == _HEAD_REVISION
    _assert_lease_table_shape(tmp_db_path)


@pytest.mark.parametrize(
    "omitted_shape",
    ["primary_key", "unique", "foreign_key", "index"],
)
def test_upgrade_rejects_same_columns_with_missing_shape(
    tmp_db_path: Path,
    omitted_shape: str,
) -> None:
    config = _alembic_config()
    command.upgrade(config, _PREVIOUS_REVISION)
    _create_malformed_preexisting_table(tmp_db_path, omit=omitted_shape)

    with pytest.raises(RuntimeError, match="结构与 0030 不一致"):
        command.upgrade(config, "head")

    assert _current_revision(tmp_db_path) == _PREVIOUS_REVISION


def test_downgrade_to_0029_drops_only_lease_table(tmp_db_path: Path) -> None:
    config = _alembic_config()
    command.upgrade(config, "head")

    command.downgrade(config, _PREVIOUS_REVISION)

    engine = create_engine(f"sqlite:///{tmp_db_path}")
    inspector = inspect(engine)
    assert "chapter_operation_leases" not in inspector.get_table_names()
    assert "workspaces" in inspector.get_table_names()
    assert _current_revision(tmp_db_path) == _PREVIOUS_REVISION
