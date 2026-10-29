"""``src/core/memory/retriever.py`` 单测(P2-B-08)。

覆盖维度:

- ``assemble_query_text`` 完整字段输入 → 拼成完整文本,各段格式正确
- ``assemble_query_text`` 空可选字段(must_achieve / related_characters /
  related_plot_threads 为空 list)→ 对应段跳过,query 文本不留空标签
- ``assemble_query_text`` 极端最小章节卡(必填字段非空,可选 list 全空)
  → 仍返回非空 query 文本(场景 + 种子 + 视角三段)
- ``retrieve_memories_for_chapter`` query_text / top_k / collection /
  where 参数透传给 ``VectorStore.query``
- ``retrieve_memories_for_chapter`` 出参 ``RetrievedMemory`` 字段映射
  正确(QueryHit.id → memory_id、QueryHit.document → content、metadata
  / distance 透传)
- ``retrieve_memories_for_chapter`` ``top_k <= 0`` 抛 ``RetrieverError``
- ``retrieve_memories_for_chapter`` collection 为空(从未写入)→ 返 ``[]``
- 与 ``VectorStore`` 集成路径:用真 ``VectorStore`` + mock embedding_fn,
  写入 3 条文档 → retrieve 拿到 ``RetrievedMemory`` 列表(端到端)

mock 策略:retriever 主要单测用 ``unittest.mock.MagicMock`` 模拟
``VectorStore.query``,验证调用参数透传 + 出参映射;集成路径用真
``VectorStore`` + 4 维查表 mock embedding fn(沿用 P2-B-06 测试风格)。
"""

from __future__ import annotations

from pathlib import Path
from typing import Any
from unittest.mock import MagicMock

import pytest

from src.core.memory.retriever import (
    DEFAULT_COLLECTION,
    DEFAULT_TOP_K,
    RetrievedMemory,
    RetrieverError,
    assemble_query_text,
    retrieve_memories_for_chapter,
)
from src.core.memory.vector import QueryHit, VectorStore
from src.core.models.chapter_card import ChapterCard, ContinuityContext

# ---- ChapterCard 构造工具 --------------------------------------------------


def _make_continuity(**overrides: Any) -> ContinuityContext:
    payload: dict[str, Any] = dict(
        prev_chapter_tail="他握紧剑柄,目光不离对面那道身影。",
        unfinished_actions=["剑势悬而未发"],
        time_gap="立即续接",
        scene_continuation="same",
        pov_change=False,
        pov_from="林衍",
        pov_to="林衍",
    )
    payload.update(overrides)
    return ContinuityContext(**payload)


def _make_card(**overrides: Any) -> ChapterCard:
    """完整字段 ChapterCard 构造工具(沿用 ``test_writer.py`` 风格)。"""
    payload: dict[str, Any] = dict(
        chapter_id="ch-0002",
        workspace_id="ws-demo",
        volume_id="v01",
        chapter_number=2,
        title="第二章·破阵",
        tension_level=8,
        word_count_min=2800,
        word_count_max=4500,
        plot_node_budget=3,
        must_achieve=["林衍突破第一道剑阵", "苏离表明立场"],
        must_unresolved=["阵眼真凶身份"],
        forbidden=["现代科技元素", "提及第三卷剧情"],
        pov_character="林衍",
        setting="青崖山顶残阵",
        continuity_context=_make_continuity(),
        next_chapter_seed="苏离消失前留下的玉佩在颤动",
        arc_phase="rising",
        is_arc_finale=False,
        related_plot_threads=["thread-main", "thread-suli"],
        related_characters=["林衍", "苏离"],
        review_threshold=7.5,
        revise_threshold=6.5,
        max_rewrites=3,
        max_revises=2,
    )
    payload.update(overrides)
    return ChapterCard(**payload)


# ---- 模块常量 ---------------------------------------------------------------


class TestModuleConstants:
    def test_default_collection(self) -> None:
        assert DEFAULT_COLLECTION == "memories"

    def test_default_top_k(self) -> None:
        assert DEFAULT_TOP_K == 20


# ---- assemble_query_text ----------------------------------------------------


class TestAssembleQueryText:
    def test_full_card_all_segments_present(self) -> None:
        """完整章节卡 → query 文本含全 6 段,顺序固定。"""
        card = _make_card()
        text = assemble_query_text(card)
        # 顺序断言 + 内容断言
        lines = text.split("\n")
        assert lines[0] == "主线: 林衍突破第一道剑阵 | 苏离表明立场"
        assert lines[1] == "场景: 青崖山顶残阵"
        assert lines[2] == "种子: 苏离消失前留下的玉佩在颤动"
        assert lines[3] == "视角: 林衍"
        assert lines[4] == "出场角色: 林衍 苏离"
        assert lines[5] == "关联伏笔: thread-main thread-suli"
        assert len(lines) == 6

    def test_empty_must_achieve_segment_skipped(self) -> None:
        card = _make_card(must_achieve=[])
        text = assemble_query_text(card)
        assert "主线:" not in text
        # 其他段仍在
        assert "场景: 青崖山顶残阵" in text
        assert "视角: 林衍" in text

    def test_empty_related_characters_segment_skipped(self) -> None:
        card = _make_card(related_characters=[])
        text = assemble_query_text(card)
        assert "出场角色:" not in text
        assert "视角: 林衍" in text  # pov_character 仍在

    def test_empty_related_plot_threads_segment_skipped(self) -> None:
        card = _make_card(related_plot_threads=[])
        text = assemble_query_text(card)
        assert "关联伏笔:" not in text

    def test_minimal_card_still_returns_nonempty(self) -> None:
        """可选 list 全空,必填字段非空 → query 文本至少含场景 + 种子 + 视角三段。"""
        card = _make_card(
            must_achieve=[],
            related_characters=[],
            related_plot_threads=[],
        )
        text = assemble_query_text(card)
        assert text  # 非空
        assert "场景: 青崖山顶残阵" in text
        assert "种子: 苏离消失前留下的玉佩在颤动" in text
        assert "视角: 林衍" in text

    def test_segments_separated_by_newline(self) -> None:
        card = _make_card()
        text = assemble_query_text(card)
        # 段间分隔符是 \n,不是 \n\n 或 ' '
        assert "\n" in text
        assert "\n\n" not in text


# ---- retrieve_memories_for_chapter:参数透传 + 出参映射 -----------------


class TestRetrieveParamsThrough:
    def test_query_text_passed_to_vector_store(self) -> None:
        card = _make_card()
        vs = MagicMock(spec=VectorStore)
        vs.query.return_value = []
        retrieve_memories_for_chapter(card, vector_store=vs)
        # 取出实际调用参数
        kwargs = vs.query.call_args.kwargs
        assert kwargs["query_text"] == assemble_query_text(card)

    def test_default_collection_and_top_k(self) -> None:
        card = _make_card()
        vs = MagicMock(spec=VectorStore)
        vs.query.return_value = []
        retrieve_memories_for_chapter(card, vector_store=vs)
        kwargs = vs.query.call_args.kwargs
        assert kwargs["collection"] == "memories"
        assert kwargs["top_k"] == 20

    def test_custom_collection_and_top_k(self) -> None:
        card = _make_card()
        vs = MagicMock(spec=VectorStore)
        vs.query.return_value = []
        retrieve_memories_for_chapter(
            card,
            vector_store=vs,
            collection="characters",
            top_k=5,
        )
        kwargs = vs.query.call_args.kwargs
        assert kwargs["collection"] == "characters"
        assert kwargs["top_k"] == 5

    def test_where_clause_passed_through(self) -> None:
        """P6-B-01 起,caller 提供 where 时与 ``{"revoked": {"$ne": True}}``
        以 ``$and`` 合并(默认排除软删除条目)。``include_revoked=True``
        可关闭过滤。"""
        card = _make_card()
        vs = MagicMock(spec=VectorStore)
        vs.query.return_value = []
        where = {"entity_type": {"$eq": "character"}}
        retrieve_memories_for_chapter(card, vector_store=vs, where=where)
        kwargs = vs.query.call_args.kwargs
        assert kwargs["where"] == {
            "$and": [where, {"revoked": {"$ne": True}}]
        }

    def test_where_clause_passed_through_include_revoked(self) -> None:
        """``include_revoked=True`` 时 caller where 原样透传不附加 revoked
        过滤(P6-B-01)。"""
        card = _make_card()
        vs = MagicMock(spec=VectorStore)
        vs.query.return_value = []
        where = {"entity_type": {"$eq": "character"}}
        retrieve_memories_for_chapter(
            card,
            vector_store=vs,
            where=where,
            include_revoked=True,
        )
        kwargs = vs.query.call_args.kwargs
        assert kwargs["where"] == where

    def test_where_default_filter_revoked(self) -> None:
        """P6-B-01 起,where 默认 None 时填 ``{"revoked": {"$ne": True}}``
        过滤软删除条目(对齐 ``VectorStore.soft_delete_by_chapter_id``)。"""
        card = _make_card()
        vs = MagicMock(spec=VectorStore)
        vs.query.return_value = []
        retrieve_memories_for_chapter(card, vector_store=vs)
        kwargs = vs.query.call_args.kwargs
        assert kwargs["where"] == {"revoked": {"$ne": True}}

    def test_where_default_none_when_include_revoked(self) -> None:
        """``include_revoked=True`` 时不附加任何 where(向后兼容老语义)。"""
        card = _make_card()
        vs = MagicMock(spec=VectorStore)
        vs.query.return_value = []
        retrieve_memories_for_chapter(
            card, vector_store=vs, include_revoked=True
        )
        kwargs = vs.query.call_args.kwargs
        assert kwargs["where"] is None


class TestRetrieveOutputMapping:
    def test_query_hit_to_retrieved_memory(self) -> None:
        """``QueryHit`` 字段映射到 ``RetrievedMemory``。"""
        card = _make_card()
        vs = MagicMock(spec=VectorStore)
        vs.query.return_value = [
            QueryHit(
                id="m-1",
                document="林衍曾在第一卷击败过同名剑阵",
                metadata={"entity_type": "event", "chapter_id": "ch-0001"},
                distance=0.12,
            ),
            QueryHit(
                id="m-2",
                document="苏离的师承背景与玉佩有关",
                metadata={"entity_type": "character"},
                distance=0.34,
            ),
        ]
        results = retrieve_memories_for_chapter(card, vector_store=vs)
        assert len(results) == 2
        assert all(isinstance(r, RetrievedMemory) for r in results)

        r0 = results[0]
        assert r0.memory_id == "m-1"
        assert r0.content == "林衍曾在第一卷击败过同名剑阵"
        assert r0.metadata == {"entity_type": "event", "chapter_id": "ch-0001"}
        assert r0.distance == pytest.approx(0.12)

        r1 = results[1]
        assert r1.memory_id == "m-2"
        assert r1.content == "苏离的师承背景与玉佩有关"
        assert r1.metadata == {"entity_type": "character"}
        assert r1.distance == pytest.approx(0.34)

    def test_empty_hits_returns_empty_list(self) -> None:
        card = _make_card()
        vs = MagicMock(spec=VectorStore)
        vs.query.return_value = []
        results = retrieve_memories_for_chapter(card, vector_store=vs)
        assert results == []

    def test_retrieved_memory_is_frozen(self) -> None:
        """``RetrievedMemory`` ``frozen=True`` 不可变。"""
        rm = RetrievedMemory(
            memory_id="m-1",
            content="foo",
            metadata={},
            distance=0.0,
        )
        with pytest.raises(Exception):  # pydantic ValidationError
            rm.memory_id = "m-2"  # type: ignore[misc]


# ---- 错误语义 ---------------------------------------------------------------


class TestErrors:
    def test_top_k_zero_raises(self) -> None:
        card = _make_card()
        vs = MagicMock(spec=VectorStore)
        with pytest.raises(RetrieverError, match="top_k 必须为正整数"):
            retrieve_memories_for_chapter(card, vector_store=vs, top_k=0)
        # vs.query 应未被调用(前置拦截)
        vs.query.assert_not_called()

    def test_top_k_negative_raises(self) -> None:
        card = _make_card()
        vs = MagicMock(spec=VectorStore)
        with pytest.raises(RetrieverError, match="top_k 必须为正整数"):
            retrieve_memories_for_chapter(card, vector_store=vs, top_k=-3)
        vs.query.assert_not_called()


# ---- 与 VectorStore 集成路径(mock embedding) -----------------------------

# 沿用 P2-B-06 测试风格的 4 维查表 mock,让 query 距离排序可预期
_TEXT_TO_VEC: dict[str, list[float]] = {
    "ch-0001-event": [1.0, 0.0, 0.0, 0.0],  # 与"剑阵"语义相近
    "char-suli-bg": [0.0, 1.0, 0.0, 0.0],  # 与"苏离"语义相近
    "scene-cangya": [0.0, 0.0, 1.0, 0.0],  # 与"青崖山"语义相近
}


def _mock_query_match_first_section(texts: list[str]) -> list[list[float]]:
    """简化策略:embed query 时,根据 query 文本里出现的 token 决定主导
    维度。让"主线"段含"剑阵"→ 第 0 维主导,匹配 m1;含"苏离"→ 第 1
    维主导,匹配 m2。

    本 mock 仅服务集成测试,断言 retriever 端到端通路畅通即可,不追求
    真实 embedding 语义。
    """
    out: list[list[float]] = []
    for t in texts:
        if "剑阵" in t:
            out.append([1.0, 0.0, 0.0, 0.0])
        elif "苏离" in t:
            out.append([0.0, 1.0, 0.0, 0.0])
        elif "青崖山" in t:
            out.append([0.0, 0.0, 1.0, 0.0])
        else:
            # 文档路径(写入 add 时)按字面查表,query 段命中时已早返
            out.append(_TEXT_TO_VEC.get(t, [0.0, 0.0, 0.0, 1.0]))
    return out


class TestIntegrationWithVectorStore:
    def test_end_to_end_retrieve(self, tmp_path: Path) -> None:
        """端到端:VectorStore + retriever,写入 3 条文档,query 拿到 top_K
        命中,distance 升序。"""
        vs = VectorStore(
            "ws-it",
            embedding_fn=_mock_query_match_first_section,
            persist_dir=tmp_path / "chroma",
        )
        vs.add(
            "memories",
            ids=["m1", "m2", "m3"],
            documents=["ch-0001-event", "char-suli-bg", "scene-cangya"],
            metadatas=[
                {"entity_type": "event", "chapter_id": "ch-0001"},
                {"entity_type": "character", "char_id": "苏离"},
                {"entity_type": "scene", "scene_id": "青崖"},
            ],
        )
        # ChapterCard 主线段含"剑阵"→ query 命中 m1 最靠前
        card = _make_card(
            must_achieve=["林衍突破第一道剑阵"],
        )
        results = retrieve_memories_for_chapter(
            card,
            vector_store=vs,
            top_k=3,
        )
        assert len(results) == 3
        assert all(isinstance(r, RetrievedMemory) for r in results)
        # m1 最相近(query 第 0 维主导,m1 文档也是第 0 维主导)
        assert results[0].memory_id == "m1"
        assert results[0].metadata == {"entity_type": "event", "chapter_id": "ch-0001"}
        # 升序:m1 distance 最小
        distances = [r.distance for r in results]
        assert distances == sorted(distances)

    def test_top_k_larger_than_collection(self, tmp_path: Path) -> None:
        """``top_k`` 超过集合大小 → 返回全部,不抛错。"""
        vs = VectorStore(
            "ws-it",
            embedding_fn=_mock_query_match_first_section,
            persist_dir=tmp_path / "chroma",
        )
        vs.add(
            "memories",
            ids=["m1"],
            documents=["ch-0001-event"],
        )
        card = _make_card(must_achieve=["剑阵"])
        results = retrieve_memories_for_chapter(
            card,
            vector_store=vs,
            top_k=20,  # 默认值,但集合只有 1 条
        )
        assert len(results) == 1
        assert results[0].memory_id == "m1"

    def test_empty_collection_returns_empty(self, tmp_path: Path) -> None:
        """从未写入的 collection → ``query`` 返 ``[]``,retriever 也返
        ``[]``(VectorStore 自动建空 collection 兜底)。"""
        vs = VectorStore(
            "ws-it",
            embedding_fn=_mock_query_match_first_section,
            persist_dir=tmp_path / "chroma",
        )
        card = _make_card()
        results = retrieve_memories_for_chapter(
            card,
            vector_store=vs,
            collection="never_written",
        )
        assert results == []

    def test_where_clause_filters(self, tmp_path: Path) -> None:
        """``where`` 透传到 chroma,过滤 metadata。"""
        vs = VectorStore(
            "ws-it",
            embedding_fn=_mock_query_match_first_section,
            persist_dir=tmp_path / "chroma",
        )
        vs.add(
            "memories",
            ids=["m1", "m2"],
            documents=["ch-0001-event", "char-suli-bg"],
            metadatas=[
                {"entity_type": "event"},
                {"entity_type": "character"},
            ],
        )
        card = _make_card(must_achieve=["剑阵"])
        results = retrieve_memories_for_chapter(
            card,
            vector_store=vs,
            top_k=10,
            where={"entity_type": "character"},
        )
        # 只有 m2 通过过滤
        assert len(results) == 1
        assert results[0].memory_id == "m2"
