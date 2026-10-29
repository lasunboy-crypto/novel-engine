"""验证 P2-B-17 alembic 0003 迁移:``chapters.status`` 7 档 CHECK 约束。

覆盖维度:

- upgrade head 后 ``chapters`` 表存在 ``ck_chapters_status_7_levels`` CHECK 约束
- 7 档枚举值都能写入,非枚举值 INSERT 抛 IntegrityError
- downgrade -1 干净退回 0002_p2_memory_models(CHECK 约束消失)
- downgrade fold:``rewriting`` / ``revising`` / ``published`` /
  ``human_intervention`` 4 档归并到 baseline 3 档(``drafting`` /
  ``reviewing`` / ``approved``)
- upgrade → downgrade -1 → upgrade 幂等

沿用 ``tests/db/test_alembic_baseline.py`` / ``test_p2_models.py`` 同结构
(类 B 工程判断:与 baseline 同型,放 ``tests/db/`` 与 P1/P2 一致)。
"""

from __future__ import annotations

import sqlite3
from datetime import datetime, timezone
from pathlib import Path

import pytest
from alembic import command
from alembic.config import Config
from sqlalchemy import create_engine, insert, text
from sqlalchemy.exc import IntegrityError
from sqlalchemy.orm import Session

from src.core.db import (
    CHAPTER_STATUS_VALUES,
    Base,
    ChapterORM,
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


def _seed_workspace(session: Session, workspace_id: str = "ws1") -> None:
    """testfixture:塞一行 WorkspaceORM 满足 chapters FK。

    **P5 候选 3 / P6-acceptance 重构**(原 P5-B-07 起 raw SQL,本任务改用
    SQLAlchemy Core ``insert(WorkspaceORM).values(...)``):本测试 fixture 部分
    用例显式 upgrade 到 ``0003_chapters_state_7_levels``(非 head),0003 schema
    无 P5-B-07 加列的 ``outline_status`` 列。SQLAlchemy 2.0 ORM ``session.add(
    WorkspaceORM(...))`` unit-of-work 路径会发 ``RETURNING outline_status``
    取 server-side default 回填,触发"no such column: outline_status"。**Core
    ``insert(Model).values(...)`` 默认不发 RETURNING**(只有 ``.returning(...)``
    显式调用才发),在两种 schema(0003 / head)上都能工作。字面满足"raw SQL →
    SQLAlchemy ORM 重构"语义 — ``insert(WorkspaceORM)`` 基于 ORM 元数据,
    字段名 / 类型由 ORM 模型校验,避免裸 text SQL 字段拼写漂移。
    """
    now = datetime.now(timezone.utc)
    session.execute(
        insert(WorkspaceORM).values(
            workspace_id=workspace_id,
            display_name="x",
            genre="scifi",
            premise="p",
            created_at=now,
            last_active_at=now,
            engine_version="0.1.0",
            status="active",
            total_chapters=0,
            completed_chapters=0,
            config_overrides={},
        )
    )
    session.flush()


def _make_chapter(chapter_id: str, status: str, workspace_id: str = "ws1") -> ChapterORM:
    """ORM 构造 ChapterORM(用于 head schema 测试)。"""
    now = datetime.now(timezone.utc)
    return ChapterORM(
        chapter_id=chapter_id,
        workspace_id=workspace_id,
        current_version=1,
        status=status,
        rewrite_count=0,
        revise_count=0,
        created_at=now,
        approved_at=None,
        total_token_usage={},
        total_cost_usd=0.0,
    )


def _insert_chapter_core(
    session: Session,
    chapter_id: str,
    status: str,
    workspace_id: str = "ws1",
) -> None:
    """SQLAlchemy Core ``insert(ChapterORM).values(...)`` 插一行(用于 pin 到
    历史 revision 的测试,避免 ORM unit-of-work 在 ``force_pass_at`` /
    ``force_pass_reason`` 列上发 RETURNING 撞 "no such column"——P5-B-07 同型
    ORM RETURNING 陷阱 + P6-B-01 新增列扩展)。

    **P5 候选 3 / P6-acceptance 重构**:原 ``_insert_chapter_raw`` 走 raw text
    SQL,本任务改用 ``insert(ChapterORM).values(...)`` Core 模式(字段名 / 类型
    由 ORM 模型校验,Core 默认不发 RETURNING 在历史 schema 也工作)。
    """
    now = datetime.now(timezone.utc)
    session.execute(
        insert(ChapterORM).values(
            chapter_id=chapter_id,
            workspace_id=workspace_id,
            current_version=1,
            status=status,
            rewrite_count=0,
            revise_count=0,
            created_at=now,
            approved_at=None,
            total_token_usage={},
            total_cost_usd=0.0,
        )
    )


class TestUpgradeAddsCheckConstraint:
    """upgrade 0003 后,``chapters.status`` 必有 CHECK 约束。"""

    def test_check_constraint_present_in_sqlite_master(
        self, tmp_db_path: Path
    ) -> None:
        # P6-B-01 起,head 上是 ``ck_chapters_status_8_levels``(扩 invalidated
        # 第 8 档);本测试 pin 到 ``0003_chapters_state_7_levels`` 验证 P2-B-17
        # 自身落地的 7 档 CHECK 约束(CLAUDE.md §Alembic head 硬编码假设修复
        # 纪律:旧测试用 ``command.upgrade(cfg, "<own_revision_id>")`` 显式
        # 指定 P2-B-17 自身 revision)。
        command.upgrade(_alembic_config(), "0003_chapters_state_7_levels")
        con = sqlite3.connect(str(tmp_db_path))
        try:
            row = con.execute(
                "SELECT sql FROM sqlite_master "
                "WHERE type='table' AND name='chapters'"
            ).fetchone()
        finally:
            con.close()
        assert row is not None
        assert "ck_chapters_status_7_levels" in row[0]
        # SQL 谓词包含 0003 baseline 的 7 档(``invalidated`` 是 P6-B-01 0008
        # 才扩入,本测试 pin 在 0003 不应出现)。
        for v in CHAPTER_STATUS_VALUES:
            if v == "invalidated":
                continue
            assert f"'{v}'" in row[0]
        assert "'invalidated'" not in row[0]

    def test_all_7_values_accepted(self, tmp_db_path: Path) -> None:
        """7 档枚举每档都能写入 chapters。"""
        command.upgrade(_alembic_config(), "head")
        engine = create_engine(f"sqlite:///{tmp_db_path}")
        with Session(engine) as session:
            _seed_workspace(session)
            for i, status in enumerate(CHAPTER_STATUS_VALUES):
                session.add(_make_chapter(f"c{i}", status))
            session.commit()
            # 全部落库
            count = session.execute(
                text("SELECT COUNT(*) FROM chapters")
            ).scalar()
            assert count == len(CHAPTER_STATUS_VALUES)

    def test_invalid_value_rejected(self, tmp_db_path: Path) -> None:
        """非 7 档值 → CHECK 失败 → IntegrityError。"""
        command.upgrade(_alembic_config(), "head")
        engine = create_engine(f"sqlite:///{tmp_db_path}")
        with Session(engine) as session:
            _seed_workspace(session)
            session.add(_make_chapter("c-bad", "not_started"))
            with pytest.raises(IntegrityError):
                session.commit()


class TestDowngrade:
    """downgrade -1 干净退回 0002,且 fold 4 新档到 3 档。

    P4-B-01 起 head 已超过 0003,显式 upgrade 到 ``0003_chapters_state_7_levels``
    再 downgrade -1 才能验证 P2-B-17 自身的 down 行为(否则只是 undo 0004)。
    """

    def test_downgrade_drops_check(self, tmp_db_path: Path) -> None:
        cfg = _alembic_config()
        command.upgrade(cfg, "0003_chapters_state_7_levels")
        command.downgrade(cfg, "-1")
        con = sqlite3.connect(str(tmp_db_path))
        try:
            row = con.execute(
                "SELECT sql FROM sqlite_master "
                "WHERE type='table' AND name='chapters'"
            ).fetchone()
        finally:
            con.close()
        assert row is not None
        # CHECK 约束已消失,但表仍在
        assert "ck_chapters_status_7_levels" not in row[0]

    def test_downgrade_folds_4_new_states_to_3_baseline(
        self, tmp_db_path: Path
    ) -> None:
        """4 档新状态归并:rewriting/human_intervention → drafting,
        revising → reviewing,published → approved。"""
        cfg = _alembic_config()
        command.upgrade(cfg, "0003_chapters_state_7_levels")

        # 入库 4 档新状态 + 3 档 baseline 各一行(raw SQL,避免 ORM 模型新加列
        # 在 0003 历史 schema 上撞"no such column" — P6-B-01 force_pass_*)
        engine = create_engine(f"sqlite:///{tmp_db_path}")
        with Session(engine) as session:
            _seed_workspace(session)
            for cid, status in (
                ("c-rewriting", "rewriting"),
                ("c-revising", "revising"),
                ("c-published", "published"),
                ("c-hi", "human_intervention"),
                ("c-drafting", "drafting"),
                ("c-reviewing", "reviewing"),
                ("c-approved", "approved"),
            ):
                _insert_chapter_core(session, cid, status)
            session.commit()

        # downgrade
        command.downgrade(cfg, "-1")

        # 验证 fold 后的状态值
        con = sqlite3.connect(str(tmp_db_path))
        try:
            rows = con.execute(
                "SELECT chapter_id, status FROM chapters ORDER BY chapter_id"
            ).fetchall()
        finally:
            con.close()

        actual = dict(rows)
        assert actual["c-rewriting"] == "drafting"  # rewriting → drafting
        assert actual["c-revising"] == "reviewing"  # revising → reviewing
        assert actual["c-published"] == "approved"  # published → approved
        assert actual["c-hi"] == "drafting"  # human_intervention → drafting
        # baseline 3 档不变
        assert actual["c-drafting"] == "drafting"
        assert actual["c-reviewing"] == "reviewing"
        assert actual["c-approved"] == "approved"


class TestRoundTrip:
    """upgrade → downgrade → upgrade 幂等(P2-B-03 同模式)。

    P4-B-01 起 head 已超过 0003,显式 upgrade 到 ``0003_chapters_state_7_levels``
    才能精确验证 P2-B-17 自身的 round-trip。
    """

    def test_round_trip_idempotent(self, tmp_db_path: Path) -> None:
        cfg = _alembic_config()
        command.upgrade(cfg, "0003_chapters_state_7_levels")
        command.downgrade(cfg, "-1")
        command.upgrade(cfg, "0003_chapters_state_7_levels")
        # CHECK 约束再次出现
        con = sqlite3.connect(str(tmp_db_path))
        try:
            row = con.execute(
                "SELECT sql FROM sqlite_master "
                "WHERE type='table' AND name='chapters'"
            ).fetchone()
        finally:
            con.close()
        assert row is not None
        assert "ck_chapters_status_7_levels" in row[0]


class TestOrmCheckConstraint:
    """``Base.metadata.create_all`` 也建出带 CHECK 约束的表(api/deps.py 与
    cli/commands.make_db_session 都走这条路径,与 alembic 迁移结果一致)。
    """

    def test_create_all_includes_check(self, tmp_path: Path) -> None:
        db_path = tmp_path / "via_create_all.db"
        engine = create_engine(f"sqlite:///{db_path}")
        Base.metadata.create_all(engine)
        con = sqlite3.connect(str(db_path))
        try:
            row = con.execute(
                "SELECT sql FROM sqlite_master "
                "WHERE type='table' AND name='chapters'"
            ).fetchone()
        finally:
            con.close()
        assert row is not None
        # P6-B-01 起 ORM CHECK 约束名升级到 ``ck_chapters_status_8_levels``
        # (扩 ``invalidated`` 第 8 档,Alembic 0008 重建 CHECK)
        assert "ck_chapters_status_8_levels" in row[0]
