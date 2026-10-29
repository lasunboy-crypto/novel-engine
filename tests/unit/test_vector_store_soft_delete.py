"""``VectorStore.soft_delete_by_chapter_id`` 单测(P6-B-01)。

覆盖维度:

- happy path:已存在 ``metadata.chapter_id == target_id`` 条目 → metadata
  合并 ``revoked: true`` + 返实际处理数
- 命中 0 条 → 静默 no-op,返 0(对齐"幂等" — 同章重复 rewind 不抛)
- collection 不存在 → 自动建空 + 返 0(对齐既有 ``get_or_create_collection``
  兜底语义)
- ``chapter_id`` 空字符串 → ``VectorStoreError`` 抛
- 多 chapter_id 混在同 collection:只软删除目标 chapter_id 命中,其他不受影响
- 软删除后 metadata 其他字段保留(``entity_type`` / ``entity_id`` 等不丢)
- retriever ``$ne True`` where 过滤实证:软删除后 ``retrieve_memories_for_chapter``
  默认不返软删除条目;``include_revoked=True`` 仍可拿到

mock embedding fn 沿用 ``test_vector_store.py`` 同模式 — 固定查表 4 维。
"""

from __future__ import annotations

from pathlib import Path

import pytest

from src.core.memory.retriever import (
    DEFAULT_COLLECTION,
    retrieve_memories_for_chapter,
)
from src.core.memory.vector import (
    VectorStore,
    VectorStoreError,
    count_persisted_chapter_vectors_read_only,
    inspect_persisted_chapter_vectors_read_only,
)
from src.core.models.chapter_card import ChapterCard, ContinuityContext


_TEXT_TO_VEC: dict[str, list[float]] = {
    "char-ling-yan": [1.0, 0.0, 0.0, 0.0],
    "char-bai-ling": [0.9, 0.1, 0.0, 0.0],
    "thread-revenge": [0.0, 1.0, 0.0, 0.0],
    "timeline-event-1": [0.0, 0.0, 1.0, 0.0],
    "test-query": [0.5, 0.5, 0.0, 0.0],
}


def _mock_embedding_fn(texts: list[str]) -> list[list[float]]:
    return [_TEXT_TO_VEC.get(t, [0.0, 0.0, 0.0, 1.0]) for t in texts]


@pytest.fixture
def store(tmp_path: Path) -> VectorStore:
    return VectorStore(
        "ws-rewind-test",
        embedding_fn=_mock_embedding_fn,
        persist_dir=tmp_path / "chroma",
    )


def _make_card_for_query(chapter_id: str = "ch-0001") -> ChapterCard:
    """构造一个 retriever 可用的 ChapterCard 最小实例(对齐
    ``tests/unit/test_retriever.py::_make_card`` 同模式)。"""
    return ChapterCard(
        chapter_id=chapter_id,
        workspace_id="ws-rewind-test",
        volume_id="v01",
        chapter_number=1,
        title="测试章节",
        tension_level=5,
        word_count_min=2000,
        word_count_max=4000,
        plot_node_budget=2,
        must_achieve=["test event"],
        must_unresolved=[],
        forbidden=[],
        pov_character="林衍",
        setting="测试场景",
        continuity_context=ContinuityContext(
            previous_chapter_id="ch-0000",
            prev_chapter_tail="前情提要:剑势悬而未发",
            unfinished_actions=[],
            time_gap="立即续接",
            scene_continuation="same",
            pov_change=False,
            pov_from="林衍",
            pov_to="林衍",
        ),
        next_chapter_seed="test seed",
        arc_phase="rising",
        is_arc_finale=False,
        related_plot_threads=["plot-test"],
        related_characters=["林衍"],
        review_threshold=7.5,
        revise_threshold=6.5,
        max_rewrites=2,
        max_revises=2,
    )


class TestSoftDeleteHappyPath:
    def test_count_by_chapter_id_is_read_only_and_embedding_free(
        self, store: VectorStore
    ) -> None:
        store.add(
            "memories",
            ids=["a", "b", "c"],
            documents=["a", "b", "c"],
            metadatas=[
                {"chapter_id": "ch-0003"},
                {"chapter_id": "ch-0003"},
                {"chapter_id": "ch-0004"},
            ],
        )

        assert store.count_by_chapter_id(
            "memories", chapter_id="ch-0003"
        ) == 2
        assert store.count("memories") == 3

    def test_snapshot_digest_detects_same_count_content_update(
        self,
        store: VectorStore,
    ) -> None:
        store.add(
            "memories",
            ids=["same-id"],
            documents=["char-ling-yan"],
            metadatas=[{"chapter_id": "ch-0003", "revoked": False}],
        )
        before = store.snapshot_by_chapter_id(
            "memories",
            chapter_id="ch-0003",
        )

        store.add(
            "memories",
            ids=["same-id"],
            documents=["char-bai-ling"],
            metadatas=[{"chapter_id": "ch-0003", "revoked": True}],
        )
        after = store.snapshot_by_chapter_id(
            "memories",
            chapter_id="ch-0003",
        )

        assert before.count == after.count == 1
        assert before.digest != after.digest

    def test_snapshot_keeps_preexisting_revoked_metadata_stable(
        self,
        store: VectorStore,
    ) -> None:
        """已有 revoked 向量是合法 baseline；未改动时摘要必须稳定。"""

        store.add(
            "memories",
            ids=["revoked-id"],
            documents=["legacy-memory"],
            metadatas=[{"chapter_id": "ch-0003", "revoked": True}],
        )

        first = store.snapshot_by_chapter_id(
            "memories",
            chapter_id="ch-0003",
        )
        second = store.snapshot_by_chapter_id(
            "memories",
            chapter_id="ch-0003",
        )

        assert first.count == second.count == 1
        assert first.digest == second.digest

    def test_snapshot_count_does_not_create_missing_source_directory(
        self,
        tmp_path: Path,
    ) -> None:
        missing = tmp_path / "missing-chroma"

        assert count_persisted_chapter_vectors_read_only(
            workspace_id="ws-rewind-test",
            persist_dir=missing,
            collection="memories",
            chapter_id="ch-0003",
        ) == 0

        assert not missing.exists()

    def test_snapshot_count_reads_copy_without_changing_source_tree(
        self,
        tmp_path: Path,
    ) -> None:
        persist_dir = tmp_path / "source-chroma"
        source = VectorStore(
            "ws-rewind-source",
            embedding_fn=_mock_embedding_fn,
            persist_dir=persist_dir,
        )
        source.add(
            "memories",
            ids=["source-a"],
            documents=["char-ling-yan"],
            metadatas=[{"chapter_id": "ch-0003"}],
        )
        before = {
            path.relative_to(persist_dir): (path.read_bytes(), path.stat().st_mtime_ns)
            for path in persist_dir.rglob("*")
            if path.is_file()
        }

        assert count_persisted_chapter_vectors_read_only(
            workspace_id="ws-rewind-source",
            persist_dir=persist_dir,
            collection="memories",
            chapter_id="ch-0003",
        ) == 1

        after = {
            path.relative_to(persist_dir): (path.read_bytes(), path.stat().st_mtime_ns)
            for path in persist_dir.rglob("*")
            if path.is_file()
        }
        assert after == before

        snapshot = inspect_persisted_chapter_vectors_read_only(
            workspace_id="ws-rewind-source",
            persist_dir=persist_dir,
            collection="memories",
            chapter_id="ch-0003",
        )
        assert snapshot.count == 1
        assert len(snapshot.digest) == 64
        assert {
            path.relative_to(persist_dir): (
                path.read_bytes(),
                path.stat().st_mtime_ns,
            )
            for path in persist_dir.rglob("*")
            if path.is_file()
        } == before

    def test_soft_delete_marks_metadata_revoked_true(
        self, store: VectorStore
    ) -> None:
        store.add(
            "memories",
            ids=["char-ling-yan"],
            documents=["char-ling-yan"],
            metadatas=[
                {
                    "entity_type": "character",
                    "entity_id": "ling-yan",
                    "chapter_id": "ch-0003",
                    "workspace_id": "ws-rewind-test",
                }
            ],
        )
        count = store.soft_delete_by_chapter_id(
            "memories", chapter_id="ch-0003"
        )
        assert count == 1

        # 实证 metadata 已加 revoked=True,其他字段保留
        hits = store.query("memories", query_text="test-query", top_k=10)
        assert len(hits) == 1
        assert hits[0].metadata["revoked"] is True
        assert hits[0].metadata["entity_type"] == "character"
        assert hits[0].metadata["entity_id"] == "ling-yan"
        assert hits[0].metadata["chapter_id"] == "ch-0003"

    def test_soft_delete_multiple_entries_same_chapter(
        self, store: VectorStore
    ) -> None:
        store.add(
            "memories",
            ids=["a", "b", "c"],
            documents=["char-ling-yan", "thread-revenge", "timeline-event-1"],
            metadatas=[
                {"chapter_id": "ch-0003", "entity_type": "character"},
                {"chapter_id": "ch-0003", "entity_type": "plot_thread"},
                {"chapter_id": "ch-0003", "entity_type": "timeline"},
            ],
        )
        count = store.soft_delete_by_chapter_id(
            "memories", chapter_id="ch-0003"
        )
        assert count == 3

    def test_soft_delete_only_targets_matching_chapter_id(
        self, store: VectorStore
    ) -> None:
        store.add(
            "memories",
            ids=["a", "b", "c"],
            documents=["char-ling-yan", "char-bai-ling", "thread-revenge"],
            metadatas=[
                {"chapter_id": "ch-0003", "entity_type": "character"},
                {"chapter_id": "ch-0004", "entity_type": "character"},
                {"chapter_id": "ch-0005", "entity_type": "plot_thread"},
            ],
        )
        count = store.soft_delete_by_chapter_id(
            "memories", chapter_id="ch-0003"
        )
        assert count == 1

        # 只 ch-0003 的条目被标记
        result = store._get_collection("memories").get()  # noqa: SLF001
        id_to_meta = dict(zip(result["ids"], result["metadatas"]))
        assert id_to_meta["a"].get("revoked") is True
        assert "revoked" not in id_to_meta["b"]
        assert "revoked" not in id_to_meta["c"]


class TestSoftDeleteEdgeCases:
    def test_soft_delete_no_match_returns_zero(
        self, store: VectorStore
    ) -> None:
        store.add(
            "memories",
            ids=["a"],
            documents=["char-ling-yan"],
            metadatas=[{"chapter_id": "ch-0001"}],
        )
        count = store.soft_delete_by_chapter_id(
            "memories", chapter_id="ch-9999"
        )
        assert count == 0
        # 原 metadata 保持
        hits = store.query("memories", query_text="test-query", top_k=5)
        assert "revoked" not in hits[0].metadata

    def test_soft_delete_empty_collection_returns_zero(
        self, store: VectorStore
    ) -> None:
        """collection 不存在 → 自动建空 + 返 0(对齐既有 get_or_create 兜底)。"""
        count = store.soft_delete_by_chapter_id(
            "nonexistent_collection", chapter_id="ch-0001"
        )
        assert count == 0

    def test_soft_delete_empty_chapter_id_raises(
        self, store: VectorStore
    ) -> None:
        with pytest.raises(VectorStoreError, match="chapter_id 不能为空"):
            store.soft_delete_by_chapter_id("memories", chapter_id="")
        with pytest.raises(VectorStoreError, match="chapter_id 不能为空"):
            store.count_by_chapter_id("memories", chapter_id="")

    def test_soft_delete_idempotent_repeat_call(
        self, store: VectorStore
    ) -> None:
        """同章重复 rewind:第 2 次仍返 1(metadata 已有 revoked=True 但
        条目仍命中 chapter_id 匹配)。语义对齐"按 chapter_id 撤回"幂等。"""
        store.add(
            "memories",
            ids=["a"],
            documents=["char-ling-yan"],
            metadatas=[{"chapter_id": "ch-0003"}],
        )
        first = store.soft_delete_by_chapter_id(
            "memories", chapter_id="ch-0003"
        )
        second = store.soft_delete_by_chapter_id(
            "memories", chapter_id="ch-0003"
        )
        assert first == 1
        assert second == 1  # 重复调用仍命中 + 标记(幂等)


class TestRetrieverFiltersRevokedByDefault:
    def test_retriever_excludes_revoked_by_default(
        self, store: VectorStore
    ) -> None:
        store.add(
            DEFAULT_COLLECTION,
            ids=["a", "b"],
            documents=["char-ling-yan", "char-bai-ling"],
            metadatas=[
                {"chapter_id": "ch-0001", "entity_type": "character"},
                {"chapter_id": "ch-0002", "entity_type": "character"},
            ],
        )
        # rewind ch-0001
        store.soft_delete_by_chapter_id(
            DEFAULT_COLLECTION, chapter_id="ch-0001"
        )

        hits = retrieve_memories_for_chapter(
            _make_card_for_query(),
            vector_store=store,
            top_k=10,
        )
        # 默认 include_revoked=False,只命中 ch-0002 那条
        ids = [h.memory_id for h in hits]
        assert "a" not in ids
        assert "b" in ids

    def test_retriever_includes_revoked_when_opt_in(
        self, store: VectorStore
    ) -> None:
        store.add(
            DEFAULT_COLLECTION,
            ids=["a", "b"],
            documents=["char-ling-yan", "char-bai-ling"],
            metadatas=[
                {"chapter_id": "ch-0001"},
                {"chapter_id": "ch-0002"},
            ],
        )
        store.soft_delete_by_chapter_id(
            DEFAULT_COLLECTION, chapter_id="ch-0001"
        )

        hits = retrieve_memories_for_chapter(
            _make_card_for_query(),
            vector_store=store,
            top_k=10,
            include_revoked=True,
        )
        ids = [h.memory_id for h in hits]
        assert "a" in ids
        assert "b" in ids
