"""``src/core/memory/vector.py`` ``VectorStore`` 单测(P2-B-06)。

覆盖维度:

- ``add`` / ``count`` round-trip
- ``query`` 返回 ``QueryHit`` 按距离升序、metadata round-trip、命中数 < ``top_k``
- ``delete`` 幂等(不存在的 id 不报错)
- 多 collection 在同一 ``VectorStore`` 实例内互不干扰
- 工作区物理隔离:不同 ``persist_dir`` 不共享数据
- 跨实例持久化:同 ``persist_dir`` 重开后数据仍在
- 错误语义:长度不一致 / 非正 ``top_k`` → ``VectorStoreError``
- 边界:空 ``add`` / 空 ``delete`` 静默 no-op、空 collection ``query`` 返
  ``[]`` / ``count`` 返 ``0``、``top_k`` 超过集合大小

mock embedding fn 设计:把固定文本映射到 4 维"概念向量",让 ``query`` 距
离排序可断言(L2 距离越小越相似,chroma 默认距离空间)。未注册文本兜底
``[0,0,0,1]``,确保维度一致(chroma 同 collection 内首次 add 锁定维度)。

每个测试通过 ``store`` fixture 拿到独立 ``tmp_path / 'chroma'`` 目录,
chromadb 在不同路径之间自然隔离,避免测试间状态串扰。
"""

from __future__ import annotations

from pathlib import Path

import pytest

from src.core.memory.vector import (
    QueryHit,
    VectorStore,
    VectorStoreError,
)

# ---- 测试工具 ---------------------------------------------------------------

_TEXT_TO_VEC: dict[str, list[float]] = {
    "猫": [1.0, 0.0, 0.0, 0.0],
    "猫科动物": [0.9, 0.1, 0.0, 0.0],
    "狗": [0.0, 1.0, 0.0, 0.0],
    "椅子": [0.0, 0.0, 1.0, 0.0],
    "桌子": [0.0, 0.0, 0.9, 0.1],
}


def _mock_embedding_fn(texts: list[str]) -> list[list[float]]:
    """固定查表 embedding;未注册文本回 ``[0,0,0,1]`` 兜底,保 4 维统一。"""
    return [_TEXT_TO_VEC.get(t, [0.0, 0.0, 0.0, 1.0]) for t in texts]


@pytest.fixture
def store(tmp_path: Path) -> VectorStore:
    """每个测试用独立 ``tmp_path / 'chroma'`` 实例化 ``VectorStore``。

    pytest ``tmp_path`` fixture 每测试唯一,chromadb ``PersistentClient``
    在不同 path 之间自然隔离,避免测试间状态串扰。
    """
    return VectorStore(
        "ws1",
        embedding_fn=_mock_embedding_fn,
        persist_dir=tmp_path / "chroma",
    )


# ---- add / count ------------------------------------------------------------


class TestAddCount:
    def test_add_then_count(self, store: VectorStore) -> None:
        store.add(
            "memories",
            ids=["a", "b", "c"],
            documents=["猫", "狗", "椅子"],
        )
        assert store.count("memories") == 3

    def test_count_empty_collection(self, store: VectorStore) -> None:
        """从未写入过的 collection ``count`` 返 0,且不抛错。"""
        assert store.count("never_written") == 0

    def test_add_empty_is_noop(self, store: VectorStore) -> None:
        store.add("memories", ids=[], documents=[])
        assert store.count("memories") == 0

    def test_add_with_metadata(self, store: VectorStore) -> None:
        store.add(
            "memories",
            ids=["a"],
            documents=["猫"],
            metadatas=[{"chapter": "v1c001", "score": 5}],
        )
        assert store.count("memories") == 1

    def test_add_length_mismatch_ids_documents(
        self, store: VectorStore
    ) -> None:
        with pytest.raises(VectorStoreError, match="长度不一致"):
            store.add("memories", ids=["a"], documents=["猫", "狗"])

    def test_add_length_mismatch_metadatas(self, store: VectorStore) -> None:
        with pytest.raises(VectorStoreError, match="metadatas .* 长度不一致"):
            store.add(
                "memories",
                ids=["a", "b"],
                documents=["猫", "狗"],
                metadatas=[{"k": "v"}],
            )


# ---- query ------------------------------------------------------------------


class TestQuery:
    def test_query_returns_closest_first(self, store: VectorStore) -> None:
        """L2 距离越小越相似,query("猫") → "猫科动物" 排第一。

        ``狗`` 与 ``椅子`` 距 ``猫`` 的 L2 距离平局(都是 2.0,各自正交基向
        量),不断言后两名顺序;只断言"猫科动物"严格排首位。
        """
        store.add(
            "memories",
            ids=["cat_family", "dog", "chair"],
            documents=["猫科动物", "狗", "椅子"],
        )
        hits = store.query("memories", query_text="猫", top_k=3)
        assert hits[0].id == "cat_family"
        assert {hits[1].id, hits[2].id} == {"dog", "chair"}
        assert hits[0].distance < hits[1].distance
        assert hits[0].distance < hits[2].distance

    def test_query_returns_query_hit_pydantic(
        self, store: VectorStore
    ) -> None:
        """返回类型是 ``QueryHit``,字段齐全。"""
        store.add(
            "memories",
            ids=["a"],
            documents=["猫"],
            metadatas=[{"chapter": "v1c001", "score": 5}],
        )
        hits = store.query("memories", query_text="猫", top_k=1)
        assert len(hits) == 1
        hit = hits[0]
        assert isinstance(hit, QueryHit)
        assert hit.id == "a"
        assert hit.document == "猫"
        assert hit.metadata == {"chapter": "v1c001", "score": 5}
        assert hit.distance == pytest.approx(0.0)

    def test_query_top_k_clamps_to_collection_size(
        self, store: VectorStore
    ) -> None:
        """``top_k=20`` 但只写入 2 条 → 返 2 条,不抛错。"""
        store.add("memories", ids=["a", "b"], documents=["猫", "狗"])
        hits = store.query("memories", query_text="猫", top_k=20)
        assert len(hits) == 2

    def test_query_empty_collection_returns_empty(
        self, store: VectorStore
    ) -> None:
        hits = store.query("never_written", query_text="猫", top_k=5)
        assert hits == []

    def test_query_metadata_none_normalizes_to_empty_dict(
        self, store: VectorStore
    ) -> None:
        """``add`` 不带 metadatas → 取出 ``QueryHit.metadata`` 是 ``{}``,不是 ``None``。"""
        store.add("memories", ids=["a"], documents=["猫"])
        hits = store.query("memories", query_text="猫", top_k=1)
        assert hits[0].metadata == {}

    def test_query_top_k_zero_raises(self, store: VectorStore) -> None:
        with pytest.raises(VectorStoreError, match="top_k 必须为正整数"):
            store.query("memories", query_text="猫", top_k=0)

    def test_query_top_k_negative_raises(self, store: VectorStore) -> None:
        with pytest.raises(VectorStoreError, match="top_k 必须为正整数"):
            store.query("memories", query_text="猫", top_k=-3)

    def test_query_with_where_filter(self, store: VectorStore) -> None:
        """``where`` 透传 chroma metadata 过滤。"""
        store.add(
            "memories",
            ids=["a", "b", "c"],
            documents=["猫", "猫科动物", "狗"],
            metadatas=[
                {"chapter": "v1c001"},
                {"chapter": "v1c002"},
                {"chapter": "v1c001"},
            ],
        )
        hits = store.query(
            "memories",
            query_text="猫",
            top_k=10,
            where={"chapter": "v1c001"},
        )
        assert {h.id for h in hits} == {"a", "c"}


# ---- delete -----------------------------------------------------------------


class TestDelete:
    def test_delete_removes(self, store: VectorStore) -> None:
        store.add(
            "memories", ids=["a", "b", "c"], documents=["猫", "狗", "椅子"]
        )
        store.delete("memories", ids=["b"])
        assert store.count("memories") == 2
        hits = store.query("memories", query_text="狗", top_k=10)
        assert {h.id for h in hits} == {"a", "c"}

    def test_delete_missing_ids_is_idempotent(
        self, store: VectorStore
    ) -> None:
        """chroma 对不存在的 id 不抛错;``delete`` 整体语义是幂等。"""
        store.add("memories", ids=["a"], documents=["猫"])
        store.delete("memories", ids=["nonexistent"])
        assert store.count("memories") == 1

    def test_delete_empty_is_noop(self, store: VectorStore) -> None:
        store.add("memories", ids=["a"], documents=["猫"])
        store.delete("memories", ids=[])
        assert store.count("memories") == 1

    def test_delete_on_missing_collection_is_noop(
        self, store: VectorStore
    ) -> None:
        store.delete("never_written", ids=["any"])
        assert store.count("never_written") == 0


# ---- 多 collection 与 工作区隔离 -------------------------------------------


class TestIsolation:
    def test_multiple_collections_in_same_store_independent(
        self, store: VectorStore
    ) -> None:
        store.add("characters", ids=["c1"], documents=["猫"])
        store.add("plot_threads", ids=["t1", "t2"], documents=["狗", "椅子"])
        assert store.count("characters") == 1
        assert store.count("plot_threads") == 2
        store.delete("characters", ids=["c1"])
        assert store.count("characters") == 0
        assert store.count("plot_threads") == 2

    def test_workspaces_with_separate_persist_dirs_isolated(
        self, tmp_path: Path
    ) -> None:
        """物理目录隔离:不同 ``persist_dir`` 的 store 不共享数据。"""
        ws1_store = VectorStore(
            "ws1",
            embedding_fn=_mock_embedding_fn,
            persist_dir=tmp_path / "ws1" / "chroma",
        )
        ws2_store = VectorStore(
            "ws2",
            embedding_fn=_mock_embedding_fn,
            persist_dir=tmp_path / "ws2" / "chroma",
        )
        ws1_store.add("memories", ids=["a"], documents=["猫"])
        ws2_store.add("memories", ids=["b"], documents=["狗"])
        assert ws1_store.count("memories") == 1
        assert ws2_store.count("memories") == 1
        ws1_hits = ws1_store.query("memories", query_text="猫", top_k=10)
        assert [h.id for h in ws1_hits] == ["a"]
        ws2_hits = ws2_store.query("memories", query_text="狗", top_k=10)
        assert [h.id for h in ws2_hits] == ["b"]


# ---- 跨实例持久化 -----------------------------------------------------------


class TestPersistence:
    def test_persistent_client_round_trip_across_instances(
        self, tmp_path: Path
    ) -> None:
        """同 ``persist_dir`` 重开 store 仍能读到之前写入的数据。"""
        persist_dir = tmp_path / "chroma"
        first = VectorStore(
            "ws1",
            embedding_fn=_mock_embedding_fn,
            persist_dir=persist_dir,
        )
        first.add(
            "memories",
            ids=["a", "b"],
            documents=["猫", "狗"],
            metadatas=[{"chapter": "v1c001"}, {"chapter": "v1c002"}],
        )
        del first

        second = VectorStore(
            "ws1",
            embedding_fn=_mock_embedding_fn,
            persist_dir=persist_dir,
        )
        assert second.count("memories") == 2
        hits = second.query("memories", query_text="猫", top_k=2)
        assert [h.id for h in hits] == ["a", "b"]
        assert hits[0].metadata == {"chapter": "v1c001"}


# ---- 实例属性 ---------------------------------------------------------------


class TestInstanceAttributes:
    def test_workspace_id_attribute_exposed(self, tmp_path: Path) -> None:
        store = VectorStore(
            "my_ws",
            embedding_fn=_mock_embedding_fn,
            persist_dir=tmp_path / "chroma",
        )
        assert store.workspace_id == "my_ws"
