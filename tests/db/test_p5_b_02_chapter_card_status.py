"""验证 P5-B-02 alembic 0006 迁移与 ``chapter_cards`` 表 ORM。

P5-B-02 引入 ``chapter_cards`` 表 — **最小列设计**(类 C 处置):章节卡
完整内容仍走文件 ``data/cards/<chapter_id>.json``(SSOT,对齐 plan-kickoff
决策 2=C 先例);本表只缓存审核状态字段,支持 P5-B-06 ``chapter:write``
gate 高效检索 ``card_status != 'approved'``。

覆盖维度(沿用 ``test_p5_b_01_volumes.py`` 同结构):

- upgrade head 后 15 张业务表(P1 5 + P2 3 + P4 5 + P5 2)+ 索引齐全
- downgrade -1 干净退回 0005_volumes_table(只删 chapter_cards 表 + 索引)
- upgrade → downgrade → upgrade 幂等(显式 revision 不踩 head 硬编码假设)
- ``card_status`` 4 档 CHECK 约束:允许写,非档值 IntegrityError
- ORM round-trip:chapter_id PK / workspace_id / card_status /
  card_approved_at / created_at 字段无损
- ``workspace_id`` FK CASCADE:删 workspace 时连带 cascade chapter_cards
- ``card_status`` server_default 兜底:``INSERT`` 不指定 ``card_status``
  时自动落 ``"draft"``(对齐 Pydantic 默认 + 迁移 ``server_default``)
- 表列名与 ORM ``__table__.columns`` 一致(对齐 P5-B-01 同型测试)
- Alembic 0006 safety backfill ``UPDATE`` 语句对空表 no-op 不抛
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
    CARD_STATUS_VALUES,
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


def _table_names(db_path: Path) -> list[str]:
    con = sqlite3.connect(str(db_path))
    try:
        rows = con.execute(
            "SELECT name FROM sqlite_master WHERE type='table' ORDER BY name"
        ).fetchall()
    finally:
        con.close()
    return [r[0] for r in rows]


def _index_names(db_path: Path) -> list[str]:
    con = sqlite3.connect(str(db_path))
    try:
        rows = con.execute(
            "SELECT name FROM sqlite_master WHERE type='index' "
            "AND name NOT LIKE 'sqlite_%' ORDER BY name"
        ).fetchall()
    finally:
        con.close()
    return [r[0] for r in rows]


def _now_utc() -> datetime:
    return datetime(2026, 5, 12, 10, 0, 0, tzinfo=timezone.utc)


def _insert_workspace(session: Session, workspace_id: str = "ws-test") -> None:
    session.add(
        WorkspaceORM(
            workspace_id=workspace_id,
            display_name="测试工作区",
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
    )
    session.commit()


P5_B_02_TABLES = {"chapter_cards"}
P5_B_02_INDEXES = {"ix_chapter_cards_workspace_id"}


class TestAlembicMigration:
    """alembic 0006 schema 层面验证。"""

    def test_upgrade_head_creates_chapter_cards_table(
        self, tmp_db_path: Path
    ) -> None:
        cfg = _alembic_config()
        command.upgrade(cfg, "head")
        tables = set(_table_names(tmp_db_path))
        assert P5_B_02_TABLES.issubset(tables)
        # P1 / P2 / P4 / P5-B-01 表全保留
        assert {
            "workspaces",
            "chapters",
            "characters",
            "metrics_daily",
            "memory_health_history",
            "volumes",
        }.issubset(tables)

    def test_upgrade_head_creates_p5_b_02_index(
        self, tmp_db_path: Path
    ) -> None:
        cfg = _alembic_config()
        command.upgrade(cfg, "head")
        indexes = set(_index_names(tmp_db_path))
        assert P5_B_02_INDEXES.issubset(indexes)

    def test_downgrade_minus_one_drops_only_chapter_cards(
        self, tmp_db_path: Path
    ) -> None:
        cfg = _alembic_config()
        # **显式 revision** 升到 0006,避免 P5-B-03+ 加 0007 后本测试断言失败
        # (对齐 CLAUDE.md §实施纪律新增条目:Alembic head 硬编码假设修复)
        command.upgrade(cfg, "0006_chapter_card_status")
        command.downgrade(cfg, "-1")
        tables = set(_table_names(tmp_db_path))
        # P5-B-02 表全消失
        assert tables.isdisjoint(P5_B_02_TABLES)
        # P5-B-01 volumes 表保留 + 其他保留
        assert {"workspaces", "volumes", "metrics_daily"}.issubset(tables)

    def test_upgrade_downgrade_upgrade_idempotent(
        self, tmp_db_path: Path
    ) -> None:
        cfg = _alembic_config()
        # **显式 revision** 验证 0006 自身幂等,不受未来 0007+ 加入影响
        command.upgrade(cfg, "0006_chapter_card_status")
        command.downgrade(cfg, "-1")
        command.upgrade(cfg, "0006_chapter_card_status")
        tables = set(_table_names(tmp_db_path))
        assert P5_B_02_TABLES.issubset(tables)

    def test_chapter_cards_columns_match_orm(
        self, tmp_db_path: Path
    ) -> None:
        cfg = _alembic_config()
        command.upgrade(cfg, "head")
        engine = create_engine(f"sqlite:///{tmp_db_path}")
        inspector = inspect(engine)
        db_cols = {c["name"] for c in inspector.get_columns("chapter_cards")}
        orm_cols = {c.name for c in ChapterCardORM.__table__.columns}
        assert db_cols == orm_cols


class TestChapterCardsStatusCheck:
    """``chapter_cards.card_status`` 4 档 CHECK 约束。"""

    @pytest.fixture
    def session(self, tmp_db_path: Path) -> Session:
        command.upgrade(_alembic_config(), "head")
        engine = create_engine(f"sqlite:///{tmp_db_path}")
        with Session(engine) as session:
            _insert_workspace(session)
            yield session

    @pytest.mark.parametrize("status", CARD_STATUS_VALUES)
    def test_each_allowed_status_writes(
        self, session: Session, status: str
    ) -> None:
        session.add(
            ChapterCardORM(
                chapter_id=f"ch-{status}",
                workspace_id="ws-test",
                card_status=status,
                card_approved_at=None,
                created_at=_now_utc(),
            )
        )
        session.commit()
        loaded = session.get(ChapterCardORM, f"ch-{status}")
        assert loaded is not None
        assert loaded.card_status == status

    def test_disallowed_status_raises_integrity_error(
        self, session: Session
    ) -> None:
        session.add(
            ChapterCardORM(
                chapter_id="ch-bad",
                workspace_id="ws-test",
                card_status="archived",  # 非 4 档枚举
                card_approved_at=None,
                created_at=_now_utc(),
            )
        )
        with pytest.raises(IntegrityError):
            session.commit()


class TestChapterCardOrmRoundTrip:
    """``ChapterCardORM`` 字段 round-trip(覆盖 NULL approved_at + 显式 datetime)。"""

    @pytest.fixture
    def session(self, tmp_db_path: Path) -> Session:
        command.upgrade(_alembic_config(), "head")
        engine = create_engine(f"sqlite:///{tmp_db_path}")
        with Session(engine) as session:
            _insert_workspace(session)
            yield session

    def test_draft_card_round_trip_with_null_approved_at(
        self, session: Session
    ) -> None:
        row = ChapterCardORM(
            chapter_id="ch-0001",
            workspace_id="ws-test",
            card_status="draft",
            card_approved_at=None,
            created_at=_now_utc(),
        )
        session.add(row)
        session.commit()
        session.expunge_all()

        loaded = session.get(ChapterCardORM, "ch-0001")
        assert loaded is not None
        assert loaded.chapter_id == "ch-0001"
        assert loaded.workspace_id == "ws-test"
        assert loaded.card_status == "draft"
        assert loaded.card_approved_at is None
        # SQLite 不保留 tz 信息(对齐 P5-B-01 同型测试,只比朴素值)
        assert loaded.created_at.replace(tzinfo=None) == _now_utc().replace(
            tzinfo=None
        )

    def test_approved_card_round_trip_with_approved_at(
        self, session: Session
    ) -> None:
        approved_at = datetime(2026, 5, 13, 12, 30, 0, tzinfo=timezone.utc)
        row = ChapterCardORM(
            chapter_id="ch-0002",
            workspace_id="ws-test",
            card_status="approved",
            card_approved_at=approved_at,
            created_at=_now_utc(),
        )
        session.add(row)
        session.commit()
        session.expunge_all()

        loaded = session.get(ChapterCardORM, "ch-0002")
        assert loaded is not None
        assert loaded.card_status == "approved"
        assert loaded.card_approved_at is not None
        # SQLite 不保留 tz 信息;朴素值应一致
        assert loaded.card_approved_at.replace(tzinfo=None) == approved_at.replace(
            tzinfo=None
        )


class TestChapterCardWorkspaceForeignKey:
    """``workspace_id`` FK CASCADE 行为(对齐 P5-B-01 volumes FK 测试)。"""

    def test_workspace_id_fk_cascades_on_delete(
        self, tmp_db_path: Path
    ) -> None:
        command.upgrade(_alembic_config(), "head")
        engine = create_engine(f"sqlite:///{tmp_db_path}")

        # SQLite 默认不强制 FK,需 PRAGMA foreign_keys=ON
        with engine.begin() as conn:
            conn.execute(text("PRAGMA foreign_keys = ON"))
        with Session(engine) as session:
            session.execute(text("PRAGMA foreign_keys = ON"))
            _insert_workspace(session, workspace_id="ws-tbd")
            session.add(
                ChapterCardORM(
                    chapter_id="ch-fk-test",
                    workspace_id="ws-tbd",
                    card_status="draft",
                    card_approved_at=None,
                    created_at=_now_utc(),
                )
            )
            session.commit()
            assert (
                session.get(ChapterCardORM, "ch-fk-test") is not None
            )

            # 删 workspace 应 cascade 删 chapter_cards 行
            session.execute(text("PRAGMA foreign_keys = ON"))
            ws = session.get(WorkspaceORM, "ws-tbd")
            session.delete(ws)
            session.commit()
            assert session.get(ChapterCardORM, "ch-fk-test") is None


class TestServerDefaultDraft:
    """``card_status`` server_default ``'draft'`` 兜底:迁移层 INSERT 不指定
    时自动填 ``"draft"``(对齐 Pydantic 默认对齐)。"""

    def test_server_default_fills_draft_on_raw_insert(
        self, tmp_db_path: Path
    ) -> None:
        command.upgrade(_alembic_config(), "head")
        engine = create_engine(f"sqlite:///{tmp_db_path}")
        with Session(engine) as session:
            _insert_workspace(session, workspace_id="ws-sd")
            # 绕过 ORM 默认,走 raw SQL 不指定 card_status,验证 server_default
            session.execute(
                text(
                    "INSERT INTO chapter_cards "
                    "(chapter_id, workspace_id, created_at) "
                    "VALUES (:cid, :wid, :cat)"
                ),
                {
                    "cid": "ch-sd-test",
                    "wid": "ws-sd",
                    "cat": _now_utc().isoformat(),
                },
            )
            session.commit()
            row = session.execute(
                text(
                    "SELECT card_status FROM chapter_cards "
                    "WHERE chapter_id = 'ch-sd-test'"
                )
            ).first()
            assert row is not None
            assert row[0] == "draft"


class TestAlembicSafetyBackfill:
    """Alembic 0006 内 ``UPDATE chapter_cards SET card_status='approved'
    WHERE card_status IS NULL`` 兜底语句对空表 no-op 不抛(对齐反问引子 ②
    推荐:UPDATE 在迁移内一并落)。"""

    def test_safety_backfill_runs_idempotently(
        self, tmp_db_path: Path
    ) -> None:
        # upgrade head 已经执行了 backfill UPDATE — 空表 0 行更新,不应抛
        command.upgrade(_alembic_config(), "head")
        engine = create_engine(f"sqlite:///{tmp_db_path}")
        with engine.connect() as conn:
            count = conn.execute(
                text("SELECT COUNT(*) FROM chapter_cards")
            ).scalar()
            assert count == 0  # 新表初始为空,backfill 是 no-op
