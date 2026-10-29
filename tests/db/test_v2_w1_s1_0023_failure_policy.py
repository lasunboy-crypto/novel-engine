"""v2 w1-s1:0023 ``pipeline_runs`` failure_policy + skipped_chapter_ids 迁移测试。

验证 0023 迁移:
1. upgrade 后 ``pipeline_runs`` 有 ``failure_policy``(NOT NULL)+
   ``skipped_chapter_ids``(NOT NULL)两列;
2. 0022 时插入的旧 run 行,upgrade 0023 后 backfill 为 ``'stop'`` / ``'[]'``
   (server_default 生效,v2 前 run 全停语义不变);
3. downgrade 回 0022 后两列被 drop;
4. CHECK 约束 ``ck_pipeline_runs_failure_policy_3_levels`` 拒绝枚举外值;
5. **batch 重建表后既有 state / scope CHECK 幸存**(0015 同场景模式 —
   create_check_constraint 触发表重建,验证反射保留既有约束不静默丢失)。

**Alembic 纪律自检**(CLAUDE.md §实施纪律):downgrade target 用**显式 revision**
``0022_chapters_escalation_details``(非 ``-1``),后续 head 推进不影响本测试。
"""
from __future__ import annotations

import json
from datetime import datetime, timezone
from pathlib import Path

import pytest
from alembic import command
from alembic.config import Config
from sqlalchemy import create_engine, inspect, text
from sqlalchemy.exc import IntegrityError


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


def _seed_workspace_and_run(db_path: Path) -> None:
    """0022 schema 下插 workspace + 旧 run 行(无新两列)。"""
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
                "INSERT INTO pipeline_runs (run_id, workspace_id, state, "
                "scope, start_chapter_id, end_chapter_id, volume_id, "
                "profile_override, chapter_ids, current_chapter, "
                "completed_chapter_ids, pause_reason, error_details, "
                "created_at, updated_at, completed_at) VALUES "
                "('run_old1','ws1','stopped','single','ch-0001',NULL,NULL,"
                "NULL,'[\"ch-0001\"]','ch-0001','[\"ch-0001\"]','completed',"
                "NULL,:t,:t,:t)"
            ),
            {"t": now},
        )


def test_upgrade_adds_two_columns_not_null(tmp_db_path: Path) -> None:
    """upgrade 0023 后两列存在且 NOT NULL。"""
    cfg = _alembic_config(tmp_db_path)
    command.upgrade(cfg, "0023_pipeline_runs_failure_policy")
    cols = _columns(tmp_db_path, "pipeline_runs")
    assert "failure_policy" in cols
    assert cols["failure_policy"]["nullable"] is False
    assert "skipped_chapter_ids" in cols
    assert cols["skipped_chapter_ids"]["nullable"] is False


def test_existing_run_backfilled_stop_empty(tmp_db_path: Path) -> None:
    """0022 时插入的 run 行,upgrade 0023 后 backfill 为 'stop' / '[]'。"""
    cfg = _alembic_config(tmp_db_path)
    command.upgrade(cfg, "0022_chapters_escalation_details")
    _seed_workspace_and_run(tmp_db_path)
    command.upgrade(cfg, "0023_pipeline_runs_failure_policy")
    engine = create_engine(f"sqlite:///{tmp_db_path}")
    with engine.connect() as conn:
        row = conn.execute(
            text(
                "SELECT failure_policy, skipped_chapter_ids FROM "
                "pipeline_runs WHERE run_id='run_old1'"
            )
        ).one()
    assert row[0] == "stop"
    assert json.loads(row[1]) == []


def test_downgrade_drops_columns(tmp_db_path: Path) -> None:
    """downgrade 回 0022 后两列被 drop(显式 revision,非 -1)。"""
    cfg = _alembic_config(tmp_db_path)
    command.upgrade(cfg, "0023_pipeline_runs_failure_policy")
    cols = _columns(tmp_db_path, "pipeline_runs")
    assert "failure_policy" in cols
    command.downgrade(cfg, "0022_chapters_escalation_details")
    cols = _columns(tmp_db_path, "pipeline_runs")
    assert "failure_policy" not in cols
    assert "skipped_chapter_ids" not in cols


def test_check_constraint_rejects_bogus_policy(tmp_db_path: Path) -> None:
    """CHECK 约束拒绝 failure_policy 枚举外值。"""
    cfg = _alembic_config(tmp_db_path)
    command.upgrade(cfg, "0023_pipeline_runs_failure_policy")
    _seed_workspace_and_run_0023(tmp_db_path, policy="bogus", expect_fail=True)


def test_check_constraint_accepts_three_levels(tmp_db_path: Path) -> None:
    """CHECK 约束放行 stop / skip / retry 三档。"""
    cfg = _alembic_config(tmp_db_path)
    command.upgrade(cfg, "0023_pipeline_runs_failure_policy")
    for i, policy in enumerate(("stop", "skip", "retry")):
        _seed_workspace_and_run_0023(
            tmp_db_path,
            policy=policy,
            run_id=f"run_p{i}",
            seed_workspace=(i == 0),
        )


def test_preexisting_state_scope_checks_survive_rebuild(
    tmp_db_path: Path,
) -> None:
    """batch 重建表后既有 state / scope CHECK 幸存(不静默丢失)。"""
    cfg = _alembic_config(tmp_db_path)
    command.upgrade(cfg, "0023_pipeline_runs_failure_policy")
    engine = create_engine(f"sqlite:///{tmp_db_path}")
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
    with pytest.raises(IntegrityError):
        with engine.begin() as conn:
            conn.execute(
                text(
                    "INSERT INTO pipeline_runs (run_id, workspace_id, state, "
                    "scope, chapter_ids, completed_chapter_ids, "
                    "failure_policy, skipped_chapter_ids, created_at, "
                    "updated_at) VALUES ('run_bad','ws1','bogus_state',"
                    "'single','[]','[]','stop','[]',:t,:t)"
                ),
                {"t": now},
            )


def _seed_workspace_and_run_0023(
    db_path: Path,
    *,
    policy: str,
    run_id: str = "run_new1",
    expect_fail: bool = False,
    seed_workspace: bool = True,
) -> None:
    """0023 schema 下插行(带新两列);``expect_fail`` 断言 CHECK 拒绝。"""
    engine = create_engine(f"sqlite:///{db_path}")
    now = datetime.now(timezone.utc).isoformat()
    if seed_workspace:
        with engine.begin() as conn:
            conn.execute(
                text(
                    "INSERT INTO workspaces (workspace_id, display_name, "
                    "genre, premise, created_at, last_active_at, "
                    "engine_version, status, total_chapters, "
                    "completed_chapters, config_overrides, default_voice) "
                    "VALUES ('ws1','名','xianxia','一句话',:t,:t,'0.1.0',"
                    "'active',3,0,'{}','third_person')"
                ),
                {"t": now},
            )
    insert_sql = text(
        "INSERT INTO pipeline_runs (run_id, workspace_id, state, scope, "
        "chapter_ids, completed_chapter_ids, failure_policy, "
        "skipped_chapter_ids, created_at, updated_at) VALUES "
        f"('{run_id}','ws1','stopped','single','[]','[]',:policy,'[]',:t,:t)"
    )
    if expect_fail:
        with pytest.raises(IntegrityError):
            with engine.begin() as conn:
                conn.execute(insert_sql, {"policy": policy, "t": now})
        return
    with engine.begin() as conn:
        conn.execute(insert_sql, {"policy": policy, "t": now})
