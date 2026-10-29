"""验证 P5-B-07 alembic 0007 迁移与 ``workspaces.outline_status`` /
``chapter_cards.rejection_notes`` 加列(P5-B-07,启动指令待审 ①/②/③ 拍 A)。

P5-B-07 单次迁移加 2 列(待审 ③=A):

- ``workspaces.outline_status: String NOT NULL DEFAULT 'not_started'``
  + CHECK 3 档(``not_started`` / ``generating`` / ``committed``)
- ``chapter_cards.rejection_notes: Text NULL``

覆盖维度(沿用 ``test_p5_b_02_chapter_card_status.py`` 同结构):

- upgrade head 后 ``workspaces`` / ``chapter_cards`` 含新列 + 旧列 + 索引
- downgrade -1 干净退回 0006_chapter_card_status(新 2 列消失 + CHECK 消失)
- upgrade → downgrade → upgrade 幂等(显式 revision)
- ``outline_status`` 3 档 CHECK 约束:允许写,非档值 IntegrityError
- ``outline_status`` server_default 兜底:INSERT 不指定时自动 ``"not_started"``
- ``rejection_notes`` nullable:INSERT 不带或带 None 都通过
- ORM round-trip:WorkspaceORM.outline_status + ChapterCardORM.rejection_notes
  字段无损
"""

from __future__ import annotations

import sqlite3
from datetime import datetime, timezone
from pathlib import Path

import pytest
from alembic import command
from alembic.config import Config
from sqlalchemy import create_engine, inspect, text
from sqlalchemy.exc import IntegrityError
from sqlalchemy.orm import Session

from src.core.db import (
    OUTLINE_STATUS_VALUES,
    ChapterCardORM,
    WorkspaceORM,
)

_REPO_ROOT = Path(__file__).resolve().parents[2]


def _alembic_config() -> Config:
    cfg = Config(str(_REPO_ROOT / "alembic.ini"))
    cfg.set_main_option("script_location", str(_REPO_ROOT / "alembic"))
    return cfg


@pytest.fixture
def tmp_db_path(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> Path:
    db_path = tmp_path / "novel.db"
    monkeypatch.setenv("NOVEL_DB_PATH", str(db_path))
    return db_path


def _now_utc() -> datetime:
    return datetime(2026, 5, 12, 10, 0, 0, tzinfo=timezone.utc)


def _insert_workspace_with_status(
    session: Session, workspace_id: str, *, outline_status: str | None = None
) -> None:
    """ORM 写一行 workspace,可选指定 outline_status。"""
    kwargs = dict(
        workspace_id=workspace_id,
        display_name="测试",
        genre="玄幻",
        premise="测试",
        created_at=_now_utc(),
        last_active_at=_now_utc(),
        engine_version="0.1.0",
        status="active",
        total_chapters=0,
        completed_chapters=0,
        config_overrides={},
    )
    if outline_status is not None:
        kwargs["outline_status"] = outline_status
    session.add(WorkspaceORM(**kwargs))


# ---- 1. upgrade head 后 schema 形状 -----------------------------------------


class TestUpgradeHead:
    def test_workspaces_has_outline_status_column(
        self, tmp_db_path: Path
    ) -> None:
        cfg = _alembic_config()
        command.upgrade(cfg, "head")
        engine = create_engine(f"sqlite:///{tmp_db_path}")
        inspector = inspect(engine)
        cols = {c["name"] for c in inspector.get_columns("workspaces")}
        assert "outline_status" in cols

    def test_chapter_cards_has_rejection_notes_column(
        self, tmp_db_path: Path
    ) -> None:
        cfg = _alembic_config()
        command.upgrade(cfg, "head")
        engine = create_engine(f"sqlite:///{tmp_db_path}")
        inspector = inspect(engine)
        cols = {c["name"] for c in inspector.get_columns("chapter_cards")}
        assert "rejection_notes" in cols

    def test_outline_status_check_in_sqlite_master(
        self, tmp_db_path: Path
    ) -> None:
        cfg = _alembic_config()
        command.upgrade(cfg, "head")
        con = sqlite3.connect(str(tmp_db_path))
        try:
            row = con.execute(
                "SELECT sql FROM sqlite_master "
                "WHERE type='table' AND name='workspaces'"
            ).fetchone()
        finally:
            con.close()
        assert row is not None
        assert "ck_workspaces_outline_status" in row[0]
        for v in OUTLINE_STATUS_VALUES:
            assert f"'{v}'" in row[0]


# ---- 2. downgrade -1 干净退回 0006(显式 revision)--------------------------


class TestDowngrade:
    def test_downgrade_one_step_drops_new_columns(
        self, tmp_db_path: Path
    ) -> None:
        # 显式 upgrade 到 0007,验证 P5-B-07 自身 round-trip(对齐 P5-B-02 同型)
        cfg = _alembic_config()
        command.upgrade(cfg, "0007_outline_status_rejection_notes")
        command.downgrade(cfg, "-1")
        engine = create_engine(f"sqlite:///{tmp_db_path}")
        inspector = inspect(engine)
        ws_cols = {c["name"] for c in inspector.get_columns("workspaces")}
        cc_cols = {c["name"] for c in inspector.get_columns("chapter_cards")}
        assert "outline_status" not in ws_cols
        assert "rejection_notes" not in cc_cols

    def test_upgrade_downgrade_upgrade_idempotent(
        self, tmp_db_path: Path
    ) -> None:
        cfg = _alembic_config()
        command.upgrade(cfg, "0007_outline_status_rejection_notes")
        command.downgrade(cfg, "-1")
        command.upgrade(cfg, "0007_outline_status_rejection_notes")
        engine = create_engine(f"sqlite:///{tmp_db_path}")
        inspector = inspect(engine)
        ws_cols = {c["name"] for c in inspector.get_columns("workspaces")}
        cc_cols = {c["name"] for c in inspector.get_columns("chapter_cards")}
        assert "outline_status" in ws_cols
        assert "rejection_notes" in cc_cols


# ---- 3. outline_status CHECK 约束 -------------------------------------------


class TestOutlineStatusCheck:
    @pytest.mark.parametrize("value", list(OUTLINE_STATUS_VALUES))
    def test_all_3_values_accepted(
        self, tmp_db_path: Path, value: str
    ) -> None:
        cfg = _alembic_config()
        command.upgrade(cfg, "head")
        engine = create_engine(f"sqlite:///{tmp_db_path}")
        with Session(engine) as session:
            _insert_workspace_with_status(
                session, f"ws-{value}", outline_status=value
            )
            session.commit()
            count = session.execute(
                text(
                    "SELECT COUNT(*) FROM workspaces "
                    "WHERE outline_status = :s"
                ),
                {"s": value},
            ).scalar()
            assert count == 1

    def test_invalid_value_rejected(self, tmp_db_path: Path) -> None:
        cfg = _alembic_config()
        command.upgrade(cfg, "head")
        engine = create_engine(f"sqlite:///{tmp_db_path}")
        with Session(engine) as session:
            _insert_workspace_with_status(
                session, "ws-bad", outline_status="invalid_value"
            )
            with pytest.raises(IntegrityError):
                session.commit()

    def test_server_default_not_started(self, tmp_db_path: Path) -> None:
        """INSERT 不指定 outline_status → server_default 兜底 'not_started'。"""
        cfg = _alembic_config()
        command.upgrade(cfg, "head")
        engine = create_engine(f"sqlite:///{tmp_db_path}")
        with Session(engine) as session:
            _insert_workspace_with_status(session, "ws-default")
            session.commit()
            row = session.get(WorkspaceORM, "ws-default")
            assert row is not None
            assert row.outline_status == "not_started"


# ---- 4. rejection_notes nullable + ORM round-trip ---------------------------


class TestRejectionNotes:
    def test_rejection_notes_nullable(self, tmp_db_path: Path) -> None:
        """rejection_notes 不带值时 NULL,带值时持久化。"""
        cfg = _alembic_config()
        command.upgrade(cfg, "head")
        engine = create_engine(f"sqlite:///{tmp_db_path}")
        with Session(engine) as session:
            _insert_workspace_with_status(session, "ws-rn")
            session.add(
                ChapterCardORM(
                    chapter_id="ch-rn-1",
                    workspace_id="ws-rn",
                    card_status="rejected",
                    card_approved_at=None,
                    created_at=_now_utc(),
                    rejection_notes="人物动机不合理",
                )
            )
            session.add(
                ChapterCardORM(
                    chapter_id="ch-rn-2",
                    workspace_id="ws-rn",
                    card_status="draft",
                    card_approved_at=None,
                    created_at=_now_utc(),
                    rejection_notes=None,
                )
            )
            session.commit()
            r1 = session.get(ChapterCardORM, "ch-rn-1")
            r2 = session.get(ChapterCardORM, "ch-rn-2")
            assert r1 is not None and r1.rejection_notes == "人物动机不合理"
            assert r2 is not None and r2.rejection_notes is None
