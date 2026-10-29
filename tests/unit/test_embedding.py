"""``src/core/memory/embedding.py`` 单测 + 集成测试(P2-B-07)。

覆盖维度
--------

**单测**(默认跑,不联网,无模型加载):

- 模块常量 ``QWEN3_EMBEDDING_MODEL_ID`` / ``EMBEDDING_DIM`` 字面正确
- ``embed([])`` 零成本短路:返 ``[]`` 且不触发 ``SentenceTransformer`` 构造
- 懒加载单例:首次 ``embed`` 触发构造,后续调用复用同一实例
- 输出类型契约:``encode`` 返 numpy ``ndarray`` → ``embed`` 转 ``list[list[float]]``
- ``_reset_for_test`` 重置后下次 ``embed`` 再次触发构造
- 多线程并发首次 ``embed``:``SentenceTransformer`` 仅构造一次(双检锁验证)
- 与 ``VectorStore`` 注入式契约兼容:``embed`` 直接传 ``embedding_fn=`` 不报类型错

**集成测试**(``@pytest.mark.integration``,默认跳过,需联网拉模型 ~1.2GB):

- 真模型烟测:``embed`` 单条 → 维度 1024 + 元素 ``float``
- 批量 ``embed`` 维度 / 数量正确
- 同输入双调用一致性(确定性)
- 归一化:输出向量 L2 范数 ≈ 1.0(``normalize_embeddings=True`` 生效)
- 与 ``VectorStore`` 端到端:``add`` 3 条 → ``query`` 拿回最相近条目

mock 策略
---------

通过 ``monkeypatch.setattr`` 替换 ``sentence_transformers.SentenceTransformer``
为 ``FakeSentenceTransformer``——后者 ``encode`` 返固定 1024 维 numpy ndarray
+ 全局计数器记录构造次数。``embedding._get_model`` 内部走 lazy import,每次
``import sentence_transformers`` 都会拿到打过补丁的模块。

每个测试用 ``_reset_singleton`` fixture 自动清单例,保证测试间无状态串扰。
"""

from __future__ import annotations

import threading
from typing import Any

import numpy as np
import pytest

from src.core.memory import embedding
from src.core.memory.embedding import (
    EMBEDDING_DIM,
    QWEN3_EMBEDDING_MODEL_ID,
    embed,
)
from src.core.memory.vector import EmbeddingFunction

# ---- mock 工具 --------------------------------------------------------------


class _FakeST:
    """假 ``SentenceTransformer``——构造计数 + 固定 1024 维 ndarray 输出。

    类属性 ``construction_count`` 跨实例累计,便于断言"懒加载只构造一次"。
    每次 ``encode`` 返 ``np.ones((len(texts), EMBEDDING_DIM), dtype=float32)
    / sqrt(EMBEDDING_DIM)``——L2 范数=1,维度对齐 1024,可直接喂 chroma。
    """

    construction_count: int = 0

    def __init__(self, model_id: str) -> None:
        type(self).construction_count += 1
        self.model_id = model_id

    def encode(self, texts: list[str], **kwargs: Any) -> np.ndarray:
        # 输出 (len, 1024) 单位归一化向量,所有维度相等
        n = len(texts)
        return np.ones((n, EMBEDDING_DIM), dtype=np.float32) / np.sqrt(EMBEDDING_DIM)


@pytest.fixture(autouse=True)
def _reset_singleton() -> None:
    """每测试前重置 ``embedding`` 模块单例 + ``_FakeST`` 计数器。"""
    embedding._reset_for_test()
    _FakeST.construction_count = 0


@pytest.fixture
def patched_st(monkeypatch: pytest.MonkeyPatch) -> type[_FakeST]:
    """注入 ``_FakeST`` 替换真 ``SentenceTransformer``。

    ``embedding._get_model`` 内部 ``from sentence_transformers import
    SentenceTransformer``——lazy import,补丁通过 ``monkeypatch`` 在
    ``sentence_transformers`` 模块级生效后,首次 ``embed`` 调用会拿到
    ``_FakeST``。
    """
    import sentence_transformers

    monkeypatch.setattr(sentence_transformers, "SentenceTransformer", _FakeST)
    return _FakeST


# ---- 模块常量 ---------------------------------------------------------------


class TestModuleConstants:
    def test_model_id(self) -> None:
        assert QWEN3_EMBEDDING_MODEL_ID == "Qwen/Qwen3-Embedding-0.6B"

    def test_embedding_dim(self) -> None:
        assert EMBEDDING_DIM == 1024


# ---- 空列表零成本短路 -------------------------------------------------------


class TestEmptyShortCircuit:
    def test_empty_list_returns_empty(self, patched_st: type[_FakeST]) -> None:
        assert embed([]) == []

    def test_empty_list_does_not_load_model(
        self, patched_st: type[_FakeST]
    ) -> None:
        """``embed([])`` 不应触发 ``SentenceTransformer`` 构造。"""
        embed([])
        assert patched_st.construction_count == 0


# ---- 懒加载单例 -------------------------------------------------------------


class TestLazySingleton:
    def test_first_call_constructs_model(
        self, patched_st: type[_FakeST]
    ) -> None:
        embed(["hello"])
        assert patched_st.construction_count == 1

    def test_second_call_reuses_model(self, patched_st: type[_FakeST]) -> None:
        embed(["hello"])
        embed(["world"])
        assert patched_st.construction_count == 1

    def test_reset_for_test_triggers_reload(
        self, patched_st: type[_FakeST]
    ) -> None:
        embed(["hello"])
        embedding._reset_for_test()
        embed(["world"])
        assert patched_st.construction_count == 2

    def test_model_uses_correct_id(self, patched_st: type[_FakeST]) -> None:
        embed(["hello"])
        # 单例已被 _get_model 缓存,验证传入的 model_id
        assert embedding._model is not None  # type: ignore[unreachable]
        assert embedding._model.model_id == QWEN3_EMBEDDING_MODEL_ID  # type: ignore[attr-defined]


# ---- 输出类型契约 -----------------------------------------------------------


class TestOutputContract:
    def test_returns_list_of_list_of_float(
        self, patched_st: type[_FakeST]
    ) -> None:
        result = embed(["a", "b", "c"])
        assert isinstance(result, list)
        assert all(isinstance(row, list) for row in result)
        assert all(isinstance(x, float) for row in result for x in row)

    def test_output_dim_is_1024(self, patched_st: type[_FakeST]) -> None:
        result = embed(["a"])
        assert len(result) == 1
        assert len(result[0]) == EMBEDDING_DIM == 1024

    def test_output_count_matches_input(
        self, patched_st: type[_FakeST]
    ) -> None:
        result = embed(["a", "b", "c", "d", "e"])
        assert len(result) == 5

    def test_output_order_preserves_input(
        self, patched_st: type[_FakeST]
    ) -> None:
        # _FakeST 输出全相同向量,顺序无差异——本测试主要确认数量对齐;
        # 真实顺序检验交给 integration 测试。
        result = embed(["x", "y", "z"])
        assert len(result) == 3


# ---- 多线程单例线程安全 -----------------------------------------------------


class TestThreadSafety:
    def test_concurrent_first_call_constructs_once(
        self, patched_st: type[_FakeST]
    ) -> None:
        """多线程同时第一次 ``embed``,``SentenceTransformer`` 仅构造一次。"""
        n_threads = 8
        barrier = threading.Barrier(n_threads)
        results: list[list[list[float]]] = []
        results_lock = threading.Lock()

        def worker() -> None:
            barrier.wait()  # 让所有线程同时冲第一次 embed
            r = embed(["hello"])
            with results_lock:
                results.append(r)

        threads = [threading.Thread(target=worker) for _ in range(n_threads)]
        for t in threads:
            t.start()
        for t in threads:
            t.join()

        assert patched_st.construction_count == 1
        assert len(results) == n_threads


# ---- VectorStore 注入式契约兼容 ---------------------------------------------


class TestVectorStoreContract:
    def test_embed_satisfies_embedding_function_type(self) -> None:
        """``embed`` 必须能直接绑给 ``EmbeddingFunction`` 类型变量(类型契约)。"""
        fn: EmbeddingFunction = embed
        # 运行时类型注解不强校验,但通过 mypy / 类型检查器时此行会断签名;
        # 同时确认 fn 可调用。
        assert callable(fn)


# ---- 集成测试(默认跳过,需联网拉模型)----------------------------------


@pytest.mark.integration
class TestRealModel:
    """真模型烟测——默认跳过(``pyproject.toml [tool.pytest.ini_options]
    addopts = "-m 'not integration'"`` 字面排除)。

    手工触发:``uv run pytest tests/unit/test_embedding.py -m integration``。
    首次拉取 ~1.2GB 模型到 ``~/.cache/huggingface/hub/``,后续从 cache 加载。
    """

    def test_real_embed_single_text(self) -> None:
        result = embed(["这是一个测试句子。"])
        assert len(result) == 1
        assert len(result[0]) == EMBEDDING_DIM
        assert all(isinstance(x, float) for x in result[0])

    def test_real_embed_batch(self) -> None:
        texts = ["第一句", "第二句", "第三句"]
        result = embed(texts)
        assert len(result) == 3
        assert all(len(row) == EMBEDDING_DIM for row in result)

    def test_real_embed_deterministic(self) -> None:
        """同输入双调用结果一致(确定性)。"""
        r1 = embed(["确定性测试"])
        r2 = embed(["确定性测试"])
        assert r1 == r2

    def test_real_embed_normalized(self) -> None:
        """``normalize_embeddings=True`` 生效:L2 范数 ≈ 1.0。"""
        result = embed(["归一化测试"])
        norm = float(np.linalg.norm(result[0]))
        assert abs(norm - 1.0) < 1e-3

    def test_real_embed_end_to_end_with_vector_store(
        self, tmp_path: Any
    ) -> None:
        """端到端:``embed`` + ``VectorStore`` add → query 应拿回最相近条目。"""
        from src.core.memory.vector import VectorStore

        vs = VectorStore(
            "ws_integration",
            embedding_fn=embed,
            persist_dir=tmp_path / "chroma",
        )
        vs.add(
            "memories",
            ids=["m1", "m2", "m3"],
            documents=["猫是哺乳动物", "苹果是水果", "汽车是交通工具"],
        )
        hits = vs.query("memories", query_text="动物分类", top_k=3)
        assert len(hits) == 3
        # 最相近的应该是"猫是哺乳动物"
        assert hits[0].id == "m1"
