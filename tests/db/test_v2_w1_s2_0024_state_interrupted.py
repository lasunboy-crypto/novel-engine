"""v2 w1-s2:0024 ``pipeline_runs.state`` CHECK 扩 ``interrupted`` 迁移测试。

验证 0024 迁移:
1. upgrade 后 CHECK 约束改名 ``ck_pipeline_runs_state_5_levels`` →
   ``ck_pipeline_runs_state_6_levels``,``interrupted`` 可写;
2. CHECK 仍拒绝枚举外值(越界第 7 档);
3. downgrade 回 0023:存量 ``interrupted`` 行先翻 ``error``(数据兼容)再
   收回 5 档约束 + ``interrupted`` 不再可写;
4. upgrade → downgrade → upgrade 幂等;
5. **batch 重建表后既有 scope / failure_policy CHECK 幸存**(0015 / 0023
   同场景模式 — 表重建反射保留既有约束不静默丢失)。

**Alembic 纪律自检**(CLAUDE.md §实施纪律):downgrade target 用**显式
revision** ``0023_pipeline_runs_failure_policy``(非 ``-1``),后续 head
推进不影响本测试。
"""
from __future__ import annotations

import sqlite3
from datetime import datetime, timezone
from pathlib import Path

import pytest
from alembic import command
from alembic.config import Config
from sqlalchemy import create_engine, text
from sqlalchemy.exc import IntegrityError


_REV_0023 = "0023_pipeline_runs_failure_policy"
_REV_0024 = "0024_pipeline_runs_state_interrupted"


def _alembic_config(db_path: Path) -> Config:
    cfg = Config("alembic.ini")
    cfg.set_main_option("sqlalchemy.url", f"sqlite:///{db_path}")
    return cfg


@pytest.fixture
def tmp_db_path(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> Path:
    db = tmp_path / "novel.db"
    monkeypatch.setenv("NOVEL_DB_PATH", str(db))
    return db


def _table_sql(db_path: Path) -> str:
    con = sqlite3.connect(str(db_path))
    try:
        row = con.execute(
            "SELECT sql FROM sqlite_master "
            "WHERE type='table' AND name='pipeline_runs'"
        ).fetchone()
    finally:
        con.close()
    assert row is not None
    return row[0]


def _seed_workspace(db_path: Path) -> None:
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


def _insert_run(
    db_path: Path, *, run_id: str, state: str
) -> None:
    engine = create_engine(f"sqlite:///{db_path}")
    now = datetime.now(timezone.utc).isoformat()
    with engine.begin() as conn:
        conn.execute(
            text(
                "INSERT INTO pipeline_runs (run_id, workspace_id, state, "
                "scope, start_chapter_id, end_chapter_id, volume_id, "
                "profile_override, chapter_ids, current_chapter, "
                "completed_chapter_ids, pause_reason, error_details, "
                "failure_policy, skipped_chapter_ids, "
                "created_at, updated_at, completed_at) VALUES "
                "(:rid,'ws1',:st,'single','ch-0001',NULL,NULL,"
                "NULL,'[\"ch-0001\"]','ch-0001','[]',NULL,NULL,"
                "'stop','[]',:t,:t,NULL)"
            ),
            {"rid": run_id, "st": state, "t": now},
        )


def test_upgrade_renames_check_and_allows_interrupted(
    tmp_db_path: Path,
) -> None:
    """upgrade 0024 后约束名 6_levels + interrupted 行可写。"""
    cfg = _alembic_config(tmp_db_path)
    command.upgrade(cfg, _REV_0024)
    sql = _table_sql(tmp_db_path)
    assert "ck_pipeline_runs_state_6_levels" in sql
    assert "ck_pipeline_runs_state_5_levels" not in sql
    _seed_workspace(tmp_db_path)
    _insert_run(tmp_db_path, run_id="run_int", state="interrupted")  # 不抛


def test_upgrade_check_still_rejects_bogus_state(tmp_db_path: Path) -> None:
    """6 档 CHECK 仍拒绝枚举外值。"""
    cfg = _alembic_config(tmp_db_path)
    command.upgrade(cfg, _REV_0024)
    _seed_workspace(tmp_db_path)
    with pytest.raises(IntegrityError):
        _insert_run(tmp_db_path, run_id="run_bad", state="completed")


def test_downgrade_converts_interrupted_to_error(tmp_db_path: Path) -> None:
    """downgrade 回 0023:存量 interrupted 行翻 error + 5 档约束回归 +
    interrupted 不再可写(显式 revision,非 -1)。"""
    cfg = _alembic_config(tmp_db_path)
    command.upgrade(cfg, _REV_0024)
    _seed_workspace(tmp_db_path)
    _insert_run(tmp_db_path, run_id="run_int", state="interrupted")
    _insert_run(tmp_db_path, run_id="run_ok", state="stopped")

    command.downgrade(cfg, _REV_0023)

    sql = _table_sql(tmp_db_path)
    assert "ck_pipeline_runs_state_5_levels" in sql
    assert "ck_pipeline_runs_state_6_levels" not in sql
    engine = create_engine(f"sqlite:///{tmp_db_path}")
    with engine.connect() as conn:
        state = conn.execute(
            text("SELECT state FROM pipeline_runs WHERE run_id='run_int'")
        ).scalar_one()
        untouched = conn.execute(
            text("SELECT state FROM pipeline_runs WHERE run_id='run_ok'")
        ).scalar_one()
    assert state == "error"
    assert untouched == "stopped"
    with pytest.raises(IntegrityError):
        _insert_run(tmp_db_path, run_id="run_int2", state="interrupted")


def test_upgrade_downgrade_upgrade_idempotent(tmp_db_path: Path) -> None:
    """0024 → 0023 → 0024 幂等(显式 revision 防 head 漂移)。"""
    cfg = _alembic_config(tmp_db_path)
    command.upgrade(cfg, _REV_0024)
    command.downgrade(cfg, _REV_0023)
    command.upgrade(cfg, _REV_0024)
    assert "ck_pipeline_runs_state_6_levels" in _table_sql(tmp_db_path)
    _seed_workspace(tmp_db_path)
    _insert_run(tmp_db_path, run_id="run_int", state="interrupted")  # 不抛


def test_preexisting_scope_failure_policy_checks_survive_rebuild(
    tmp_db_path: Path,
) -> None:
    """batch 重建表后既有 scope / failure_policy CHECK 幸存(不静默丢失)。"""
    cfg = _alembic_config(tmp_db_path)
    command.upgrade(cfg, _REV_0024)
    sql = _table_sql(tmp_db_path)
    assert "ck_pipeline_runs_scope_4_levels" in sql
    assert "ck_pipeline_runs_failure_policy_3_levels" in sql
    # 行为级双保险:scope 越界仍被拒
    _seed_workspace(tmp_db_path)
    engine = create_engine(f"sqlite:///{tmp_db_path}")
    now = datetime.now(timezone.utc).isoformat()
    with pytest.raises(IntegrityError):
        with engine.begin() as conn:
            conn.execute(
                text(
                    "INSERT INTO pipeline_runs (run_id, workspace_id, state, "
                    "scope, start_chapter_id, end_chapter_id, volume_id, "
                    "profile_override, chapter_ids, current_chapter, "
                    "completed_chapter_ids, pause_reason, error_details, "
                    "failure_policy, skipped_chapter_ids, "
                    "created_at, updated_at, completed_at) VALUES "
                    "('run_s','ws1','running','bogus','ch-0001',NULL,NULL,"
                    "NULL,'[\"ch-0001\"]',NULL,'[]',NULL,NULL,"
                    "'stop','[]',:t,:t,NULL)"
                ),
                {"t": now},
            )
