"""G1 角色时序状态追踪测试(v1.1 wave 4 session 1)。

覆盖 G1 的"系统硬约束代替人工兜底"链路中**不依赖 LLM** 的纯逻辑层:

1. ``chapter_seq_from_id`` 全局章序解析(防卷内序号歧义)
2. ``latest_state_snapshot_before`` 截止 N-1 章最新快照选取
3. ``format_state_snapshot_lines`` 快照渲染(extractor / writer 共享,防漂移)
4. retriever ``_merge_chapter_seq_filter`` 时序过滤合并(防"未来记忆"泄漏)
5. store ``state_timeline`` JSON 列双向映射 round-trip

extract / writer prompt 注入的端到端测试见 ``test_memory_extractor.py`` /
``test_writer.py``(复用其 DB + vector_store fixture);迁移测试见
``tests/db/test_g1_characters_state_timeline.py``。
"""

from __future__ import annotations

from pathlib import Path

import pytest
from sqlalchemy import create_engine
from sqlalchemy.orm import Session

from src.core.db import Base, WorkspaceORM
from src.core.memory.retriever import _merge_chapter_seq_filter
from src.core.memory.store import create_character, get_character
from src.core.models.chapter_card import chapter_seq_from_id
from src.core.models.character import (
    Character,
    CharacterCoreFact,
    CharacterStateChange,
    CharacterStateSnapshot,
    VoiceProfile,
    format_state_snapshot_lines,
    latest_state_snapshot_before,
)


# ===========================================================================
# 1. chapter_seq_from_id:全局章序解析
# ===========================================================================


class TestChapterSeqFromId:
    def test_parses_zero_padded_global_seq(self) -> None:
        assert chapter_seq_from_id("ch-0001") == 1
        assert chapter_seq_from_id("ch-0010") == 10
        assert chapter_seq_from_id("ch-0050") == 50

    def test_parses_without_leading_zeros(self) -> None:
        assert chapter_seq_from_id("ch-5") == 5

    def test_returns_none_for_non_matching(self) -> None:
        assert chapter_seq_from_id("chapter-1") is None
        assert chapter_seq_from_id("ch-0001-alt") is None
        assert chapter_seq_from_id("") is None
        assert chapter_seq_from_id("v01-ch-0001") is None


# ===========================================================================
# 2. latest_state_snapshot_before:截止 N-1 章最新快照
# ===========================================================================


def _snap(seq: int, **kw) -> CharacterStateSnapshot:
    return CharacterStateSnapshot(as_of_chapter=seq, **kw)


class TestLatestStateSnapshotBefore:
    def test_picks_max_below_threshold(self) -> None:
        timeline = [_snap(1), _snap(5), _snap(8)]
        # 写第 10 章 → 取截止第 9 章(< 10)的最新 = as_of_chapter=8
        result = latest_state_snapshot_before(timeline, 10)
        assert result is not None and result.as_of_chapter == 8

    def test_excludes_current_and_future(self) -> None:
        timeline = [_snap(5), _snap(10), _snap(12)]
        # 写第 10 章 → 只看 < 10,取 as_of_chapter=5(10 / 12 都排除)
        result = latest_state_snapshot_before(timeline, 10)
        assert result is not None and result.as_of_chapter == 5

    def test_empty_timeline_returns_none(self) -> None:
        assert latest_state_snapshot_before([], 10) is None

    def test_all_at_or_after_threshold_returns_none(self) -> None:
        timeline = [_snap(10), _snap(11)]
        assert latest_state_snapshot_before(timeline, 10) is None


# ===========================================================================
# 3. format_state_snapshot_lines:快照渲染(共享,防漂移)
# ===========================================================================


class TestFormatStateSnapshotLines:
    def test_renders_all_nonempty_fields(self) -> None:
        snap = CharacterStateSnapshot(
            as_of_chapter=10,
            physical_state="右臂齐肘截断",
            injuries=["右臂截肢", "左肩刀伤"],
            acquired_items=["断魂刀"],
            current_relationships={"shi-jie": "生死之交"},
            state_changes=[
                CharacterStateChange(event_type="injury", description="右臂被斩断")
            ],
        )
        lines = format_state_snapshot_lines(snap)
        joined = "\n".join(lines)
        assert "身体状态:右臂齐肘截断" in joined
        assert "伤势:右臂截肢、左肩刀伤" in joined
        assert "持有物品 / 能力:断魂刀" in joined
        assert "关系:shi-jie=生死之交" in joined

    def test_empty_snapshot_returns_empty_list(self) -> None:
        assert format_state_snapshot_lines(CharacterStateSnapshot(as_of_chapter=3)) == []

    def test_skips_empty_fields(self) -> None:
        snap = CharacterStateSnapshot(as_of_chapter=3, injuries=["头部淤伤"])
        lines = format_state_snapshot_lines(snap)
        assert lines == ["伤势:头部淤伤"]

    def test_core_facts_are_not_duplicated_into_derived_memory_block(self) -> None:
        core_fact = CharacterCoreFact(
            state_key="character:lin-yan:life_status",
            attribute="life_status",
            value="死亡",
            source_chapter_id="ch-0003",
            source_version_id=1,
            source_path="chapter_summaries/ch-0003.core_state_candidates",
            source_text_hash="a" * 64,
            evidence_quote="林衍已确认死亡。",
            valid_from_chapter=3,
        )
        snapshot = CharacterStateSnapshot(
            as_of_chapter=3,
            authority="approved_text",
            core_facts=[core_fact],
        )

        # 核心事实只走有界 core-state capsule；旧角色状态段保持派生记忆口径。
        assert format_state_snapshot_lines(snapshot) == []


# ===========================================================================
# 4. retriever _merge_chapter_seq_filter:时序过滤合并
# ===========================================================================


class TestMergeChapterSeqFilter:
    def test_none_seq_passthrough(self) -> None:
        where = {"entity_type": "fact"}
        assert _merge_chapter_seq_filter(where, before_chapter_seq=None) is where

    def test_none_where_returns_seq_filter(self) -> None:
        assert _merge_chapter_seq_filter(None, before_chapter_seq=5) == {
            "chapter_seq": {"$lt": 5}
        }

    def test_merges_single_condition_where(self) -> None:
        result = _merge_chapter_seq_filter(
            {"entity_type": "fact"}, before_chapter_seq=5
        )
        assert result == {
            "$and": [{"entity_type": "fact"}, {"chapter_seq": {"$lt": 5}}]
        }

    def test_flattens_into_existing_and(self) -> None:
        where = {"$and": [{"entity_type": "character"}, {"x": 1}]}
        result = _merge_chapter_seq_filter(where, before_chapter_seq=7)
        # 并入同一 $and 列表保持扁平(不嵌套)
        assert result == {
            "$and": [
                {"entity_type": "character"},
                {"x": 1},
                {"chapter_seq": {"$lt": 7}},
            ]
        }

    def test_respects_caller_explicit_chapter_seq(self) -> None:
        where = {"chapter_seq": {"$gte": 2}}
        assert (
            _merge_chapter_seq_filter(where, before_chapter_seq=5) is where
        )


# ===========================================================================
# 5. store state_timeline 双向映射 round-trip
# ===========================================================================


@pytest.fixture
def session(tmp_path: Path) -> Session:
    engine = create_engine(f"sqlite:///{tmp_path / 'novel.db'}")
    Base.metadata.create_all(engine)
    from datetime import datetime, timezone

    with Session(engine) as s:
        now = datetime.now(timezone.utc)
        s.add(
            WorkspaceORM(
                workspace_id="ws-demo",
                display_name="工作区",
                genre="xianxia",
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
        s.commit()
        yield s


def _make_character(**overrides) -> Character:
    payload = dict(
        character_id="lin-yan",
        workspace_id="ws-demo",
        name="林衍",
        archetype="剑修",
        appearance="白衣",
        personality="沉静",
        background="寒门",
        voice_profile=VoiceProfile(tone="冷峻", sentence_style="短句", self_address="我"),
        goals_long_term=["剑道"],
        goals_short_term=["试炼"],
    )
    payload.update(overrides)
    return Character(**payload)


class TestStateTimelineRoundTrip:
    def test_state_timeline_persists_and_reloads(self, session: Session) -> None:
        character = _make_character(
            state_timeline=[
                CharacterStateSnapshot(
                    as_of_chapter=10,
                    physical_state="右臂截断",
                    injuries=["右臂截肢"],
                    acquired_items=["断魂刀"],
                    current_relationships={"shi-jie": "生死之交"},
                    state_changes=[
                        CharacterStateChange(
                            event_type="injury", description="右臂被斩断"
                        )
                    ],
                )
            ]
        )
        create_character(session, character)
        session.commit()

        reloaded = get_character(session, "lin-yan")
        assert reloaded is not None
        assert len(reloaded.state_timeline) == 1
        snap = reloaded.state_timeline[0]
        assert snap.as_of_chapter == 10
        assert snap.injuries == ["右臂截肢"]
        assert snap.acquired_items == ["断魂刀"]
        assert snap.current_relationships == {"shi-jie": "生死之交"}
        assert snap.state_changes[0].event_type == "injury"

    def test_default_empty_timeline(self, session: Session) -> None:
        create_character(session, _make_character())
        session.commit()
        reloaded = get_character(session, "lin-yan")
        assert reloaded is not None
        assert reloaded.state_timeline == []


# ===========================================================================
# 6. VectorStore.backfill_chapter_seq:既有工作区 Chroma 旧条目回填
# ===========================================================================


def _mock_embed(texts: list[str]) -> list[list[float]]:
    return [[0.1, 0.2, 0.3, 0.4] for _ in texts]


class TestBackfillChapterSeq:
    def _store(self, tmp_path: Path):
        from src.core.memory.vector import VectorStore

        return VectorStore(
            "ws-demo", embedding_fn=_mock_embed, persist_dir=tmp_path / "chroma"
        )

    def test_backfills_missing_seq_and_idempotent(self, tmp_path: Path) -> None:
        store = self._store(tmp_path)
        store.add(
            "memories",
            ids=["fact-1", "fact-2", "fact-3", "fact-4"],
            documents=["d1", "d2", "d3", "d4"],
            metadatas=[
                # 缺 chapter_seq,chapter_id 可解析 → 应回填 5
                {"entity_type": "fact", "chapter_id": "ch-0005"},
                # 已有 chapter_seq → 跳过
                {"entity_type": "fact", "chapter_id": "ch-0003", "chapter_seq": 3},
                # chapter_id 非 ch-NNNN 模式 → 跳过
                {"entity_type": "fact", "chapter_id": "weird"},
                # 缺 chapter_id → 跳过
                {"entity_type": "fact"},
            ],
        )
        # dry_run:统计将回填条数(只有 fact-1)
        assert store.backfill_chapter_seq("memories", dry_run=True) == 1
        # 实际回填
        assert store.backfill_chapter_seq("memories") == 1
        # 幂等:再跑 0 条
        assert store.backfill_chapter_seq("memories") == 0
        # fact-1 现在带 chapter_seq=5
        hits = store.query(
            "memories", query_text="x", top_k=10, where={"entity_type": "fact"}
        )
        meta_by_id = {h.id: h.metadata for h in hits}
        assert meta_by_id["fact-1"]["chapter_seq"] == 5
        assert "chapter_seq" not in meta_by_id["fact-3"]

    def test_empty_collection_returns_zero(self, tmp_path: Path) -> None:
        store = self._store(tmp_path)
        assert store.backfill_chapter_seq("memories") == 0
