"""P2 向量记忆库:Qwen3-Embedding-0.6B 单例懒加载 + ``embed`` 函数(P2-B-07)。

提供模块级 ``embed(texts) -> list[list[float]]`` 函数,签名符合
``src.core.memory.vector.EmbeddingFunction =
Callable[[list[str]], list[list[float]]]`` 注入式契约——P2-B-08 retriever
直接 ``VectorStore(..., embedding_fn=embed, ...)`` 即可,无须再做类型适配。

模型 ``Qwen/Qwen3-Embedding-0.6B`` 单例懒加载:模块导入时**不触发**模型
下载/加载,首次 ``embed(texts)`` 调用时才初始化全局 ``_model``,后续调用
复用同一实例。模型权重首次拉取到 ``~/.cache/huggingface/hub/``(由
``sentence-transformers`` 默认行为决定),~1.21GB(``model.safetensors``
1.19GB + tokenizer 15MB + 配置 < 1MB)。

输出向量维度固定 ``1024``(``EMBEDDING_DIM``)。Qwen3-Embedding-0.6B 通过
MRL(Matryoshka Representation Learning)支持 32-1024 任意降维,本模块
**不启用降维**——P2 阶段统一返 1024 维,留 P3+ 在 retriever 层根据 collection
类别按需降维。

设计要点
--------

1. **懒加载**:模块导入时不触发模型下载;首次 ``embed`` 调用才初始化。这
   让 P2-B-06 既有 22 测试 + P1 全量 550 测试无任何附加成本(单测注入
   mock fn 即可覆盖路径)。
2. **单例线程安全**:用 ``threading.Lock`` 双检锁保护单例初始化,避免多
   线程场景重复构造 ``SentenceTransformer`` 实例(每实例占 ~1.2GB RAM)。
3. **空列表零成本短路**:``embed([])`` 直接返 ``[]``,**不**触发模型加载
   ——便于 batch caller 在无新条目时调用本函数零成本(P2-B-09
   MemoryExtractor 一章可能抽 0 条新记忆)。
4. **CPU dtype 默认 fp32**:``sentence-transformers`` 在无 CUDA 时默认 fp32,
   本模块不强制 fp16(CPU 上 fp16 不一定加速,且精度损耗多余);GPU 用户
   后续可在 P3+ 引入显式 dtype 控制。
5. **归一化输出**:``encode(..., normalize_embeddings=True)`` + 显式 fp32
   post-process 二次归一化,让输出向量严格 L2 范数=1(浮点误差 ~1e-7)。
   Qwen3-Embedding-0.6B 模型权重为 **bfloat16** + ST 模块链已含 ``Normalize``
   模块,但 bf16 精度下归一化误差量级 ~1e-3(实测 norm ≈ 0.998 / 1.0036),
   单凭 ``normalize_embeddings=True`` 无法严格归一。这里在 fp32 numpy 上
   显式 post-process 一次,兑现 chromadb 默认 L2 距离与余弦距离单调等价
   (归一向量 L2² = 2 - 2·cos)的前提,排序结果一致。Qwen3-Embedding 系列
   + bge 系列官方推荐都走"归一化向量 + 余弦"。
6. **测试钩子 ``_reset_for_test``**:暴露下划线前缀函数让单测重置单例,
   命名明确不是公开 API。
7. **不启用 instruction prompt**:本模块 ``encode`` 不传 ``prompt_name``
   参数。Qwen3-Embedding-0.6B 官方实测加 instruction 涨 1%-5%,P2-B-07
   范围内不展开,留 P2-B-08 retriever 接入 query 路径时按需扩
   ``embed(texts, *, is_query=False)`` 可选参数。

边界
----

- 模型首次拉取需联网。离线环境调用 ``embed`` 会从
  ``sentence-transformers`` / ``huggingface_hub`` 抛 ``OSError`` /
  ``LocalEntryNotFoundError`` 等异常,本模块**不**重写错误,自然冒泡。
- ``texts`` 含空字符串 / 全空白:由 ``SentenceTransformer`` 自身处理
  (返回稀疏但合法的 1024 维向量),本模块不前置过滤——上层调用方
  (P2-B-09 MemoryExtractor / P2-B-13 静态资产导入)负责语义清洗。
"""

from __future__ import annotations

import threading
from typing import TYPE_CHECKING

import numpy as np

if TYPE_CHECKING:
    from sentence_transformers import SentenceTransformer

QWEN3_EMBEDDING_MODEL_ID = "Qwen/Qwen3-Embedding-0.6B"
"""HF model id。对齐 PROJECT_SPEC §2.3 字面 'Qwen3-Embedding-0.6B 本地
embedding(1024 维)'。"""

EMBEDDING_DIM = 1024
"""输出向量维度。由模型自身决定;Qwen3-Embedding-0.6B 通过 MRL 支持
32-1024 任意降维,本模块不启用,统一返 1024 维。"""


_model: "SentenceTransformer | None" = None
_model_lock = threading.Lock()


def _get_model() -> "SentenceTransformer":
    """取或建模型单例。首次调用触发 ``SentenceTransformer`` 初始化:下载
    模型到 ``~/.cache/huggingface/hub/``,加载到 RAM ~1.2GB。

    线程安全:双检锁(check-lock-check)避免初始化竞争。
    """
    global _model
    if _model is None:
        with _model_lock:
            if _model is None:
                from sentence_transformers import SentenceTransformer

                _model = SentenceTransformer(QWEN3_EMBEDDING_MODEL_ID)
    return _model


def embed(texts: list[str]) -> list[list[float]]:
    """对 ``texts`` 批量计算 1024 维 embedding,返回 ``list[list[float]]``。

    符合 ``src.core.memory.vector.EmbeddingFunction = Callable[[list[str]],
    list[list[float]]]`` 注入式契约。

    - ``texts`` 为空 → 直接返 ``[]``,**不**加载模型(零成本短路)。
    - 输出顺序与 ``texts`` 一一对应。
    - 输出维度固定 1024(``EMBEDDING_DIM``);向量已 L2 归一化。
    - 首次调用触发模型下载 + 加载;后续调用复用单例。

    :raises OSError: 离线环境首次调用,模型缓存不存在(由
        ``sentence-transformers`` / ``huggingface_hub`` 自然冒泡,本模块
        不重写)。
    """
    if not texts:
        return []
    model = _get_model()
    embeddings = model.encode(
        texts,
        convert_to_numpy=True,
        normalize_embeddings=True,
        show_progress_bar=False,
    )
    # bf16 模型权重 + 模型自身 Normalize 模块在 bf16 上归一化精度仅 ~1e-3。
    # 这里在 fp32 上显式 post-process,确保严格 L2 归一(精度 ~1e-7),
    # 兑现 chromadb 默认 L2 距离与余弦距离单调等价的前提。
    embeddings = embeddings.astype(np.float32, copy=False)
    norms = np.linalg.norm(embeddings, axis=-1, keepdims=True)
    embeddings = embeddings / np.maximum(norms, 1e-12)
    return embeddings.tolist()


def _reset_for_test() -> None:
    """重置模块级单例。**仅供单测使用**,生产代码不应调用。

    使用场景:测试用例需要在 mock 模式下覆盖 ``embed`` 内部状态,或验证
    懒加载行为(单测前置 ``_reset_for_test()`` 让"未加载"成为已知初态)。
    """
    global _model
    with _model_lock:
        _model = None
