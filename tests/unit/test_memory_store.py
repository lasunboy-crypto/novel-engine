"""``src/core/memory/store.py`` 五件套 CRUD 单测(P2-B-04)。

覆盖维度:

- 三类各自的 5 个 CRUD(create / get / list_by_workspace / update / delete)
- ``workspace_id`` 过滤(同 db 内多工作区隔离正确)
- ``update`` / ``delete`` 缺失行抛 ``MemoryStoreError``
- ``get`` 缺失返回 ``None``
- ``VoiceProfile`` 嵌入 JSON 列的 round-trip(写入 dict / 读出还原 Pydantic)
- ``TimelineEntry`` 创建不做单调校验(允许逆序 / 重复 ``time_order``,验证
  P2-B-02 决策落地)
- ``list_*_by_workspace`` 排序约定(Character 按 id / PlotThread 按 created_at /
  TimelineEntry 按 time_order)

为避免引 alembic command(``tests/unit/`` 目录约定:纯 Pydantic / 业务逻辑,
不引 alembic command),用 ``Base.metadata.create_all`` 直建 schema;schema 正
确性由 ``tests/db/test_p2_models.py`` 覆盖,本测试只关心 store 行为。
"""

from __future__ import annotations

from datetime import datetime, timezone
from pathlib import Path
from typing import Any

import pytest
from sqlalchemy import create_engine
from sqlalchemy.orm import Session

from src.core.db import Base, PlotThreadORM, WorkspaceORM
from src.core.memory.store import (
    MemoryStoreError,
    create_character,
    create_plot_thread,
    create_timeline_entry,
    delete_character,
    delete_plot_thread,
    delete_timeline_entry,
    get_character,
    get_plot_thread,
    get_timeline_entry,
    list_characters_by_workspace,
    list_plot_threads_by_workspace,
    list_timeline_entries_by_workspace,
    update_character,
    update_plot_thread,
    update_timeline_entry,
)
from src.core.models.character import Character, VoiceProfile
from src.core.models.plot_thread import PlotThread
from src.core.models.timeline_entry import TimelineEntry


# ---- 测试数据工厂 -----------------------------------------------------------

def _voice_profile() -> VoiceProfile:
    return VoiceProfile(
        catchphrases=["哼"],
        tone="冷峻",
        sentence_style="短句直白",
        self_address="本座",
        addresses_others={"hero_001": "小子"},
        forbidden_phrases=["亲爱的"],
    )


def _character(
    *,
    character_id: str = "villain_001",
    workspace_id: str = "ws1",
    **overrides: Any,
) -> Character:
    payload: dict[str, Any] = {
        "character_id": character_id,
        "workspace_id": workspace_id,
        "name": "魔尊",
        "aliases": ["魔头"],
        "archetype": "反派宗师",
        "appearance": "黑袍高冠",
        "personality": "孤傲",
        "background": "千年前坠魔",
        "voice_profile": _voice_profile(),
        "goals_long_term": ["重夺天命"],
        "goals_short_term": ["夺至宝"],
        "current_state": {"injury": "轻"},
        "relationships": {"hero_001": "宿敌"},
        "abilities": ["剑诀"],
        "secrets": ["生父"],
        "first_appearance_chapter": "v1c003",
        "last_updated_chapter": "v1c012",
    }
    payload.update(overrides)
    return Character(**payload)


def _plot_thread(
    *,
    thread_id: str = "thread_001",
    workspace_id: str = "ws1",
    **overrides: Any,
) -> PlotThread:
    payload: dict[str, Any] = {
        "thread_id": thread_id,
        "workspace_id": workspace_id,
        "title": "主角身世",
        "status": "developing",
        "importance": 5,
        "planted_chapter": "v1c001",
        "planned_payoff": "v1c050",
        "actual_payoff": None,
        "description": "魔族血脉",
        "related_characters": ["hero_001"],
        "created_at": datetime(2026, 5, 1, tzinfo=timezone.utc),
        "last_updated_chapter": "v1c012",
    }
    payload.update(overrides)
    return PlotThread(**payload)


def _timeline_entry(
    *,
    entry_id: str = "t_001",
    workspace_id: str = "ws1",
    **overrides: Any,
) -> TimelineEntry:
    payload: dict[str, Any] = {
        "entry_id": entry_id,
        "workspace_id": workspace_id,
        "chapter_id": "v1c003",
        "description": "首次交锋",
        "time_label": "建元三年春",
        "time_order": 100,
        "involved_characters": ["hero_001", "villain_001"],
    }
    payload.update(overrides)
    return TimelineEntry(**payload)


def _seed_workspace(session: Session, workspace_id: str) -> None:
    now = datetime.now(timezone.utc)
    session.add(
        WorkspaceORM(
            workspace_id=workspace_id,
            display_name=f"工作区-{workspace_id}",
            genre="scifi",
            premise="一句话",
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


@pytest.fixture
def session(tmp_path: Path) -> Session:
    """临时 SQLite + ``Base.metadata.create_all`` + seed 两个 workspace。

    用 ``Base.metadata.create_all`` 直建 schema(而非跑 alembic),避免引入
    alembic command 依赖,保持 ``tests/unit/`` 目录纯度;schema 正确性由
    ``tests/db/test_p2_models.py`` 单独覆盖。
    """
    db_path = tmp_path / "novel.db"
    engine = create_engine(f"sqlite:///{db_path}")
    Base.metadata.create_all(engine)
    with Session(engine) as s:
        _seed_workspace(s, "ws1")
        _seed_workspace(s, "ws2")
        yield s


# ---- Character CRUD ---------------------------------------------------------


class TestCharacterCRUD:
    def test_create_returns_persisted(self, session: Session) -> None:
        created = create_character(session, _character())
        session.commit()
        assert created.character_id == "villain_001"
        assert created.voice_profile.tone == "冷峻"
        assert created.voice_profile.self_address == "本座"

    def test_get_round_trip(self, session: Session) -> None:
        original = _character()
        create_character(session, original)
        session.commit()
        loaded = get_character(session, "villain_001")
        assert loaded == original

    def test_get_missing_returns_none(self, session: Session) -> None:
        assert get_character(session, "nope") is None

    def test_list_by_workspace_filters_and_orders(
        self, session: Session
    ) -> None:
        create_character(session, _character(character_id="c_b", workspace_id="ws1"))
        create_character(session, _character(character_id="c_a", workspace_id="ws1"))
        create_character(session, _character(character_id="c_x", workspace_id="ws2"))
        session.commit()

        ws1 = list_characters_by_workspace(session, "ws1")
        assert [c.character_id for c in ws1] == ["c_a", "c_b"]
        ws2 = list_characters_by_workspace(session, "ws2")
        assert [c.character_id for c in ws2] == ["c_x"]

    def test_list_by_workspace_empty(self, session: Session) -> None:
        assert list_characters_by_workspace(session, "ws1") == []

    def test_update_persists_changes(self, session: Session) -> None:
        create_character(session, _character())
        session.commit()
        result = update_character(
            session, _character(name="新名字", aliases=["新别名"])
        )
        session.commit()
        assert result.name == "新名字"
        assert result.aliases == ["新别名"]
        loaded = get_character(session, "villain_001")
        assert loaded is not None
        assert loaded.name == "新名字"
        assert loaded.aliases == ["新别名"]

    def test_update_persists_voice_profile_change(
        self, session: Session
    ) -> None:
        """更新 voice_profile JSON 嵌入子结构,验证 dict ↔ Pydantic 往返。"""
        create_character(session, _character())
        session.commit()
        new_voice = VoiceProfile(
            catchphrases=["呵"],
            tone="戏谑",
            sentence_style="长句修饰",
            self_address="我",
            addresses_others={},
            forbidden_phrases=[],
        )
        update_character(session, _character(voice_profile=new_voice))
        session.commit()
        loaded = get_character(session, "villain_001")
        assert loaded is not None
        assert loaded.voice_profile == new_voice

    def test_update_missing_raises(self, session: Session) -> None:
        with pytest.raises(MemoryStoreError, match="角色不存在"):
            update_character(session, _character(character_id="ghost"))

    def test_delete_removes(self, session: Session) -> None:
        create_character(session, _character())
        session.commit()
        delete_character(session, "villain_001")
        session.commit()
        assert get_character(session, "villain_001") is None

    def test_delete_missing_raises(self, session: Session) -> None:
        with pytest.raises(MemoryStoreError, match="角色不存在"):
            delete_character(session, "ghost")


# ---- PlotThread CRUD --------------------------------------------------------


class TestPlotThreadCRUD:
    def test_create_and_get(self, session: Session) -> None:
        original = _plot_thread()
        create_plot_thread(session, original)
        session.commit()
        loaded = get_plot_thread(session, "thread_001")
        assert loaded == original

    def test_internal_origin_metadata_is_separate_from_public_model(
        self, session: Session
    ) -> None:
        create_plot_thread(
            session,
            _plot_thread(thread_id="temp-1"),
            origin="emergent",
            expiry_policy="volume_end",
            source_evidence="正文中唯一且足够长的逐字伏笔证据。",
        )
        session.commit()
        row = session.get(PlotThreadORM, "temp-1")
        assert row is not None
        assert row.origin == "emergent"
        assert row.expiry_policy == "volume_end"
        assert row.source_evidence == "正文中唯一且足够长的逐字伏笔证据。"
        # 公开 PlotThread API 仍保持原字段，不把内部临时层扩成 Canon 模型。
        loaded = get_plot_thread(session, "temp-1")
        assert loaded is not None
        assert "origin" not in loaded.model_dump()

    def test_get_missing_returns_none(self, session: Session) -> None:
        assert get_plot_thread(session, "nope") is None

    def test_list_by_workspace_filters_and_orders_by_created_at(
        self, session: Session
    ) -> None:
        early = datetime(2026, 1, 1, tzinfo=timezone.utc)
        late = datetime(2026, 6, 1, tzinfo=timezone.utc)
        create_plot_thread(
            session, _plot_thread(thread_id="t_late", created_at=late)
        )
        create_plot_thread(
            session, _plot_thread(thread_id="t_early", created_at=early)
        )
        create_plot_thread(
            session,
            _plot_thread(thread_id="t_other", workspace_id="ws2"),
        )
        session.commit()

        ws1 = list_plot_threads_by_workspace(session, "ws1")
        assert [t.thread_id for t in ws1] == ["t_early", "t_late"]
        ws2 = list_plot_threads_by_workspace(session, "ws2")
        assert [t.thread_id for t in ws2] == ["t_other"]

    def test_list_by_workspace_empty(self, session: Session) -> None:
        assert list_plot_threads_by_workspace(session, "ws1") == []

    def test_update_persists_status_change(self, session: Session) -> None:
        create_plot_thread(session, _plot_thread())
        session.commit()
        result = update_plot_thread(
            session,
            _plot_thread(status="resolved", actual_payoff="v1c050"),
        )
        session.commit()
        assert result.status == "resolved"
        assert result.actual_payoff == "v1c050"

    def test_update_missing_raises(self, session: Session) -> None:
        with pytest.raises(MemoryStoreError, match="伏笔不存在"):
            update_plot_thread(session, _plot_thread(thread_id="ghost"))

    def test_delete_removes(self, session: Session) -> None:
        create_plot_thread(session, _plot_thread())
        session.commit()
        delete_plot_thread(session, "thread_001")
        session.commit()
        assert get_plot_thread(session, "thread_001") is None

    def test_delete_missing_raises(self, session: Session) -> None:
        with pytest.raises(MemoryStoreError, match="伏笔不存在"):
            delete_plot_thread(session, "ghost")


# ---- TimelineEntry CRUD -----------------------------------------------------


class TestTimelineEntryCRUD:
    def test_create_and_get(self, session: Session) -> None:
        original = _timeline_entry()
        create_timeline_entry(session, original)
        session.commit()
        loaded = get_timeline_entry(session, "t_001")
        assert loaded == original

    def test_get_missing_returns_none(self, session: Session) -> None:
        assert get_timeline_entry(session, "nope") is None

    def test_list_by_workspace_orders_by_time_order(
        self, session: Session
    ) -> None:
        """逆序写入,list 按 time_order 升序返回。"""
        create_timeline_entry(
            session, _timeline_entry(entry_id="t_3", time_order=300)
        )
        create_timeline_entry(
            session, _timeline_entry(entry_id="t_1", time_order=100)
        )
        create_timeline_entry(
            session, _timeline_entry(entry_id="t_2", time_order=200)
        )
        session.commit()

        ws1 = list_timeline_entries_by_workspace(session, "ws1")
        assert [e.entry_id for e in ws1] == ["t_1", "t_2", "t_3"]
        assert [e.time_order for e in ws1] == [100, 200, 300]

    def test_list_by_workspace_filters(self, session: Session) -> None:
        create_timeline_entry(session, _timeline_entry(entry_id="t_a"))
        create_timeline_entry(
            session, _timeline_entry(entry_id="t_b", workspace_id="ws2")
        )
        session.commit()

        ws1 = list_timeline_entries_by_workspace(session, "ws1")
        assert [e.entry_id for e in ws1] == ["t_a"]
        ws2 = list_timeline_entries_by_workspace(session, "ws2")
        assert [e.entry_id for e in ws2] == ["t_b"]

    def test_create_allows_duplicate_time_order(
        self, session: Session
    ) -> None:
        """store 不做单调校验(P2-B-02 决策);允许同 ``time_order`` 共存。

        同 ``time_order`` 时按 ``entry_id`` 二次排序保证结果稳定。
        """
        create_timeline_entry(
            session, _timeline_entry(entry_id="t_b", time_order=100)
        )
        create_timeline_entry(
            session, _timeline_entry(entry_id="t_a", time_order=100)
        )
        session.commit()

        entries = list_timeline_entries_by_workspace(session, "ws1")
        assert [e.entry_id for e in entries] == ["t_a", "t_b"]

    def test_update_changes_time_order(self, session: Session) -> None:
        create_timeline_entry(session, _timeline_entry())
        session.commit()
        result = update_timeline_entry(
            session, _timeline_entry(time_order=999, time_label="终局")
        )
        session.commit()
        assert result.time_order == 999
        assert result.time_label == "终局"

    def test_update_missing_raises(self, session: Session) -> None:
        with pytest.raises(MemoryStoreError, match="时间线条目不存在"):
            update_timeline_entry(session, _timeline_entry(entry_id="ghost"))

    def test_delete_removes(self, session: Session) -> None:
        create_timeline_entry(session, _timeline_entry())
        session.commit()
        delete_timeline_entry(session, "t_001")
        session.commit()
        assert get_timeline_entry(session, "t_001") is None

    def test_delete_missing_raises(self, session: Session) -> None:
        with pytest.raises(MemoryStoreError, match="时间线条目不存在"):
            delete_timeline_entry(session, "ghost")
