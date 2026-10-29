"""验证 P2-B-03 alembic 0002 迁移与 ORM 模型 schema 的正确性。

P2-B-03 引入 3 张记忆表 ``characters`` / ``plot_threads`` / ``timeline_entries``。
本测试沿用 P1-B-16 ``test_alembic_baseline.py`` 的结构(类 B 工程判断:与
baseline 同型,放 ``tests/db/`` 与 P1 一致),覆盖维度:

- upgrade head 后 8 张业务表 + 9 个索引 + alembic_version 都存在
- 三新表的列名、PK/FK、索引符合 ORM 声明
- downgrade -1 干净退回 0001_baseline(只删 P2 表,保留 P1 表)
- downgrade base 全部清除
- upgrade → downgrade -1 → upgrade 幂等
- ORM round-trip:Pydantic Character / PlotThread / TimelineEntry → ORM
  写入 → 回读 → 字段一致(含 VoiceProfile JSON 嵌入往返)
- 同一工作区内 status / time_order 跨条目并存,workspace_id 索引可过滤
"""

from __future__ import annotations

import sqlite3
from datetime import datetime, timezone
from pathlib import Path

import pytest
from alembic import command
from alembic.config import Config
from sqlalchemy import create_engine, inspect, select
from sqlalchemy.orm import Session

from src.core.db import (
    CharacterORM,
    PlotThreadORM,
    TimelineEntryORM,
    WorkspaceORM,
)
from src.core.models.character import Character, VoiceProfile
from src.core.models.plot_thread import PlotThread
from src.core.models.timeline_entry import TimelineEntry


_REPO_ROOT = Path(__file__).resolve().parents[2]


def _alembic_config() -> Config:
    """返回指向仓库 alembic.ini 的 alembic Config 实例。"""
    cfg = Config(str(_REPO_ROOT / "alembic.ini"))
    cfg.set_main_option("script_location", str(_REPO_ROOT / "alembic"))
    return cfg


@pytest.fixture
def tmp_db_path(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> Path:
    """提供临时 db 路径并注入到 NOVEL_DB_PATH 供 alembic env.py 读取。"""
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


class TestUpgradeP2:
    def test_upgrade_creates_p2_tables(self, tmp_db_path: Path) -> None:
        # P4-B-01 起 head 已超过 P2 范围,显式 upgrade 到 P2 末梢
        # ``0002_p2_memory_models`` 验证 P2 tables 集合精确性。
        command.upgrade(_alembic_config(), "0002_p2_memory_models")
        tables = _table_names(tmp_db_path)
        assert "alembic_version" in tables
        assert sorted(set(tables) - {"alembic_version"}) == [
            "chapter_versions",
            "chapters",
            "characters",
            "plot_threads",
            "review_issues",
            "review_results",
            "timeline_entries",
            "workspaces",
        ]

    def test_upgrade_creates_p2_indexes(self, tmp_db_path: Path) -> None:
        command.upgrade(_alembic_config(), "0002_p2_memory_models")
        indexes = _index_names(tmp_db_path)
        # P1 基线 3 个 + P2 新增 6 个 = 9 个
        assert sorted(indexes) == [
            "ix_chapters_workspace_id",
            "ix_characters_workspace_id",
            "ix_plot_threads_importance",
            "ix_plot_threads_status",
            "ix_plot_threads_workspace_id",
            "ix_review_issues_review_id",
            "ix_review_results_chapter_id",
            "ix_timeline_entries_time_order",
            "ix_timeline_entries_workspace_id",
        ]


class TestSchemaShape:
    """以 ORM metadata 为真相源,反向校验 P2 三新表 schema 一致。"""

    @pytest.fixture(autouse=True)
    def _upgrade(self, tmp_db_path: Path) -> None:
        command.upgrade(_alembic_config(), "head")
        self.engine = create_engine(f"sqlite:///{tmp_db_path}")
        self.inspector = inspect(self.engine)

    def test_characters_columns_and_fk(self) -> None:
        cols = {c["name"] for c in self.inspector.get_columns("characters")}
        assert cols == {
            "character_id",
            "workspace_id",
            "name",
            "aliases",
            "archetype",
            "appearance",
            "personality",
            "background",
            # A3(0019 加列):角色多情境身份侧写(v1.1 wave 6 session 2)
            "identity_facets",
            "voice_profile",
            "goals_long_term",
            "goals_short_term",
            "current_state",
            # G1(0016 加列):角色时序状态快照(v1.1 wave 4 session 1)
            "state_timeline",
            "relationships",
            "abilities",
            "secrets",
            "first_appearance_chapter",
            "last_updated_chapter",
            # P6-B-01(0008 加列):rewind soft-delete 时间戳
            "revoked_at",
        }
        pk = self.inspector.get_pk_constraint("characters")
        assert pk["constrained_columns"] == ["character_id"]
        fks = self.inspector.get_foreign_keys("characters")
        assert len(fks) == 1
        fk = fks[0]
        assert fk["referred_table"] == "workspaces"
        assert fk["constrained_columns"] == ["workspace_id"]
        assert fk["referred_columns"] == ["workspace_id"]

    def test_characters_voice_profile_not_nullable(self) -> None:
        """VoiceProfile 嵌 characters 表 JSON 列,non-null(P2-B-03 决策)。"""
        col = next(
            c
            for c in self.inspector.get_columns("characters")
            if c["name"] == "voice_profile"
        )
        assert col["nullable"] is False

    def test_characters_optional_chapter_columns_nullable(self) -> None:
        for name in ("first_appearance_chapter", "last_updated_chapter"):
            col = next(
                c for c in self.inspector.get_columns("characters") if c["name"] == name
            )
            assert col["nullable"] is True

    def test_plot_threads_columns_and_fk(self) -> None:
        cols = {c["name"] for c in self.inspector.get_columns("plot_threads")}
        assert cols == {
            "thread_id",
            "workspace_id",
            "title",
            # G2(0017 加列):伏笔类型分类(v1.1 wave 4 session 2)
            "thread_type",
            "status",
            "importance",
            "planted_chapter",
            "planned_payoff",
            "actual_payoff",
            "description",
            # R9 两层伏笔内部来源/常规候选休眠元数据（不扩公开 API）。
            "origin",
            "expiry_policy",
            "source_evidence",
            "related_characters",
            "created_at",
            "last_updated_chapter",
            # P6-B-01(0008 加列):rewind soft-delete 时间戳
            "revoked_at",
        }
        pk = self.inspector.get_pk_constraint("plot_threads")
        assert pk["constrained_columns"] == ["thread_id"]
        fks = self.inspector.get_foreign_keys("plot_threads")
        assert len(fks) == 1
        assert fks[0]["referred_table"] == "workspaces"

    def test_plot_threads_status_importance_nullable(self) -> None:
        """status / importance 必填(模型层 Literal / ge le 已校验)。"""
        for name in ("status", "importance"):
            col = next(
                c
                for c in self.inspector.get_columns("plot_threads")
                if c["name"] == name
            )
            assert col["nullable"] is False

    def test_plot_threads_indexes(self) -> None:
        idx = {i["name"] for i in self.inspector.get_indexes("plot_threads")}
        assert idx == {
            "ix_plot_threads_workspace_id",
            "ix_plot_threads_status",
            "ix_plot_threads_importance",
        }

    def test_timeline_entries_columns_and_fk(self) -> None:
        cols = {c["name"] for c in self.inspector.get_columns("timeline_entries")}
        assert cols == {
            "entry_id",
            "workspace_id",
            "chapter_id",
            "description",
            "time_label",
            "time_order",
            "involved_characters",
            # P6-B-01(0008 加列):rewind soft-delete 时间戳
            "revoked_at",
            # v1.1 wave 1 session 1(0013 加列):E2 flashback 标记
            "is_flashback",
        }
        pk = self.inspector.get_pk_constraint("timeline_entries")
        assert pk["constrained_columns"] == ["entry_id"]
        fks = self.inspector.get_foreign_keys("timeline_entries")
        # P2-B-03 决策:chapter_id 不加 FK,只 workspace_id 一个 FK
        assert len(fks) == 1
        assert fks[0]["referred_table"] == "workspaces"
        assert fks[0]["constrained_columns"] == ["workspace_id"]

    def test_timeline_entries_indexes(self) -> None:
        idx = {i["name"] for i in self.inspector.get_indexes("timeline_entries")}
        assert idx == {
            "ix_timeline_entries_workspace_id",
            "ix_timeline_entries_time_order",
        }


class TestDowngrade:
    def test_downgrade_one_step_removes_only_p2_tables(
        self, tmp_db_path: Path
    ) -> None:
        """从 ``0002_p2_memory_models`` 显式 downgrade -1 只删 P2 三表,
        保留 P1 五表(回到 0001_baseline)。

        **显式 upgrade 到 ``0002_p2_memory_models``**(非 ``head``):本类只
        验证 P2-B-03 0002 → 0001 的 downgrade 范围;P2-B-17 起 head 已升至
        0003,从 head ``downgrade -1`` 退到 0002,不会触达本测试想验证的
        "0002 → 0001"逻辑(类比 P2-B-03 调整 ``test_alembic_baseline.py``
        把 ``head`` 改成 ``0001_baseline`` 的同型修改)。
        """
        cfg = _alembic_config()
        command.upgrade(cfg, "0002_p2_memory_models")
        command.downgrade(cfg, "-1")
        tables = _table_names(tmp_db_path)
        assert sorted(set(tables) - {"alembic_version"}) == [
            "chapter_versions",
            "chapters",
            "review_issues",
            "review_results",
            "workspaces",
        ]

    def test_downgrade_base_drops_all_business_tables(
        self, tmp_db_path: Path
    ) -> None:
        cfg = _alembic_config()
        command.upgrade(cfg, "head")
        command.downgrade(cfg, "base")
        tables = _table_names(tmp_db_path)
        assert tables == ["alembic_version"]

    def test_upgrade_downgrade_upgrade_idempotent(self, tmp_db_path: Path) -> None:
        # 显式 upgrade 到 ``0002_p2_memory_models`` 而非 ``head``:P5-B-07 起
        # head 已升至 0007(workspaces.outline_status + chapter_cards.rejection_notes
        # 加列),从 head ``downgrade -1`` 退到 0006 而非 0002,与本测试想验证的
        # P2-B-03 自身幂等无关。沿用 P5-B-02 / P5-B-01 / P4-B-01 / P2-B-17
        # 同型升级模式(对齐 CLAUDE.md §实施纪律 Alembic head 纪律字面)。
        cfg = _alembic_config()
        command.upgrade(cfg, "0002_p2_memory_models")
        command.downgrade(cfg, "-1")
        command.upgrade(cfg, "0002_p2_memory_models")
        tables = _table_names(tmp_db_path)
        assert "characters" in tables
        assert "plot_threads" in tables
        assert "timeline_entries" in tables


def _seed_workspace(session: Session, workspace_id: str = "ws1") -> None:
    """三新表都 FK 到 workspaces,写 P2 数据前先 seed 一行 workspace。"""
    now = datetime.now(timezone.utc)
    session.add(
        WorkspaceORM(
            workspace_id=workspace_id,
            display_name="工作区",
            genre="scifi",
            premise="一句话设定",
            created_at=now,
            last_active_at=now,
            engine_version="0.1.0",
            status="active",
            total_chapters=0,
            completed_chapters=0,
            config_overrides={},
        )
    )
    session.commit()


class TestOrmReadWrite:
    """端到端:Pydantic 模型 → ORM → SQLite → 回读 → 字段对齐。"""

    @pytest.fixture
    def session(self, tmp_db_path: Path) -> Session:
        command.upgrade(_alembic_config(), "head")
        engine = create_engine(f"sqlite:///{tmp_db_path}")
        with Session(engine) as session:
            _seed_workspace(session)
            yield session

    def test_character_round_trip_with_voice_profile(
        self, session: Session
    ) -> None:
        """VoiceProfile 嵌入 JSON 列的往返:dict → JSON 列 → dict → Pydantic。"""
        voice = VoiceProfile(
            catchphrases=["哼", "无聊"],
            tone="冷峻",
            sentence_style="短句直白",
            self_address="本座",
            addresses_others={"hero_001": "小子"},
            forbidden_phrases=["亲爱的"],
        )
        character = Character(
            character_id="villain_001",
            workspace_id="ws1",
            name="魔尊",
            aliases=["魔头", "黑袍"],
            archetype="反派宗师",
            appearance="黑袍高冠,瞳如寒星",
            personality="孤傲冷漠",
            background="千年前因情劫坠魔",
            voice_profile=voice,
            goals_long_term=["重夺天命"],
            goals_short_term=["夺取至宝"],
            current_state={"injury": "轻", "mood": "冷"},
            relationships={"hero_001": "宿敌"},
            abilities=["剑诀", "魔功"],
            secrets=["其实是主角生父"],
            first_appearance_chapter="v1c003",
            last_updated_chapter="v1c012",
        )

        row = CharacterORM(**character.model_dump())
        session.add(row)
        session.commit()

        loaded = session.get(CharacterORM, "villain_001")
        assert loaded is not None
        assert loaded.aliases == ["魔头", "黑袍"]
        assert loaded.voice_profile == voice.model_dump()
        assert loaded.relationships == {"hero_001": "宿敌"}
        assert loaded.first_appearance_chapter == "v1c003"

        # JSON 列回读后能 round-trip 回 Pydantic
        rebuilt = Character(
            character_id=loaded.character_id,
            workspace_id=loaded.workspace_id,
            name=loaded.name,
            aliases=loaded.aliases,
            archetype=loaded.archetype,
            appearance=loaded.appearance,
            personality=loaded.personality,
            background=loaded.background,
            voice_profile=VoiceProfile(**loaded.voice_profile),
            goals_long_term=loaded.goals_long_term,
            goals_short_term=loaded.goals_short_term,
            current_state=loaded.current_state,
            relationships=loaded.relationships,
            abilities=loaded.abilities,
            secrets=loaded.secrets,
            first_appearance_chapter=loaded.first_appearance_chapter,
            last_updated_chapter=loaded.last_updated_chapter,
        )
        assert rebuilt == character

    def test_plot_thread_round_trip(self, session: Session) -> None:
        now = datetime.now(timezone.utc)
        thread = PlotThread(
            thread_id="thread_001",
            workspace_id="ws1",
            title="主角身世之谜",
            status="developing",
            importance=5,
            planted_chapter="v1c001",
            planned_payoff="v1c050",
            actual_payoff=None,
            description="开篇暗示主角并非凡人,中段揭露父系魔族血脉",
            related_characters=["hero_001", "villain_001"],
            created_at=now,
            last_updated_chapter="v1c012",
        )
        session.add(PlotThreadORM(**thread.model_dump()))
        session.commit()

        loaded = session.get(PlotThreadORM, "thread_001")
        assert loaded is not None
        assert loaded.status == "developing"
        assert loaded.importance == 5
        assert loaded.related_characters == ["hero_001", "villain_001"]
        assert loaded.actual_payoff is None
        assert loaded.last_updated_chapter == "v1c012"

    def test_timeline_entry_round_trip(self, session: Session) -> None:
        entry = TimelineEntry(
            entry_id="t_001",
            workspace_id="ws1",
            chapter_id="v1c003",
            description="主角与魔尊首次交锋于断剑崖",
            time_label="建元三年春",
            time_order=100,
            involved_characters=["hero_001", "villain_001"],
        )
        session.add(TimelineEntryORM(**entry.model_dump()))
        session.commit()

        loaded = session.get(TimelineEntryORM, "t_001")
        assert loaded is not None
        assert loaded.time_label == "建元三年春"
        assert loaded.time_order == 100
        assert loaded.involved_characters == ["hero_001", "villain_001"]

    def test_workspace_filter_query(self, session: Session) -> None:
        """同 db 内 workspace_id 字段保留(P5 跨工作区聚合时使用),
        当前每工作区独立 db 通常只有一个 workspace_id,过滤查询应工作。"""
        now = datetime.now(timezone.utc)
        session.add_all(
            [
                PlotThreadORM(
                    thread_id="t_a",
                    workspace_id="ws1",
                    title="A",
                    status="planted",
                    importance=3,
                    planted_chapter="c001",
                    planned_payoff=None,
                    actual_payoff=None,
                    description="伏笔 A",
                    related_characters=[],
                    created_at=now,
                    last_updated_chapter=None,
                ),
                PlotThreadORM(
                    thread_id="t_b",
                    workspace_id="ws1",
                    title="B",
                    status="resolved",
                    importance=1,
                    planted_chapter="c002",
                    planned_payoff="c010",
                    actual_payoff="c010",
                    description="伏笔 B",
                    related_characters=["hero_001"],
                    created_at=now,
                    last_updated_chapter="c010",
                ),
            ]
        )
        session.commit()

        threads = session.scalars(
            select(PlotThreadORM)
            .where(PlotThreadORM.workspace_id == "ws1")
            .order_by(PlotThreadORM.thread_id)
        ).all()
        assert [t.thread_id for t in threads] == ["t_a", "t_b"]

        # status 索引可走筛选(单测不验证执行计划,只验证结果集正确)
        resolved = session.scalars(
            select(PlotThreadORM).where(PlotThreadORM.status == "resolved")
        ).all()
        assert len(resolved) == 1
        assert resolved[0].thread_id == "t_b"

    def test_timeline_entries_time_order_sortable(self, session: Session) -> None:
        """time_order 索引落地 §4.2 [HARD] 时间线单调校验,排序结果稳定。"""
        session.add_all(
            [
                TimelineEntryORM(
                    entry_id="t_3",
                    workspace_id="ws1",
                    chapter_id="c003",
                    description="第三事件",
                    time_label="春末",
                    time_order=300,
                    involved_characters=[],
                ),
                TimelineEntryORM(
                    entry_id="t_1",
                    workspace_id="ws1",
                    chapter_id="c001",
                    description="第一事件",
                    time_label="春初",
                    time_order=100,
                    involved_characters=[],
                ),
                TimelineEntryORM(
                    entry_id="t_2",
                    workspace_id="ws1",
                    chapter_id="c002",
                    description="第二事件",
                    time_label="春中",
                    time_order=200,
                    involved_characters=[],
                ),
            ]
        )
        session.commit()

        entries = session.scalars(
            select(TimelineEntryORM).order_by(TimelineEntryORM.time_order)
        ).all()
        assert [e.entry_id for e in entries] == ["t_1", "t_2", "t_3"]
