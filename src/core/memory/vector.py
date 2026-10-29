"""P2 向量记忆库:基于 Chroma 的工作区级向量存储封装(P2-B-06)。

提供 ``VectorStore`` 类,封装一个工作区目录下的 Chroma 集合管理,暴露
``add / query / delete / count`` 四个对外接口。embedding 函数**注入式**——
P2-B-06 自身只依赖 chromadb,具体 embedding 实现由 P2-B-07(bge-m3)在调用
``VectorStore`` 时注入,本模块对 embedding 算法本身不感知。

设计要点
--------

1. **每实例绑定一个工作区**:``VectorStore(workspace_id, embedding_fn,
   persist_dir=...)`` 实例化时传入持久化目录,通常为
   ``WorkspacePaths.chroma_dir``。每工作区的 chroma 数据物理隔离
   (PROJECT_SPEC §2.4 字面"每工作区独立 chroma/ 目录")。
2. **统一用 ``PersistentClient``**:``persist_dir`` 必填(``Path``),Chroma
   客户端自身负责目录创建,本模块不主动 ``mkdir``。**不暴露 ``EphemeralClient``
   路径**——chromadb 1.x ``EphemeralClient()`` 在同一进程内复用 system
   缓存(多次实例化共享数据),与"每实例绑定一个工作区"的隔离语义冲突;
   测试场景统一用 ``tmp_path / 'chroma'`` 等隔离路径,每次测试拿到全新目
   录,自然实现隔离。
3. **embedding 函数注入式 + 显式预计算**:``VectorStore`` **不**把注入的 fn
   绑到 chroma collection 的 ``embedding_function`` 上,而是在
   ``add / query`` 内部先调 fn 算出 embedding,再以 ``embeddings=`` /
   ``query_embeddings=`` 显式传给 chroma。collection 创建时
   ``embedding_function=None``(chromadb 1.5+ 显式支持),完全绕过 chroma
   的 ``DefaultEmbeddingFunction``(避免它隐式触发 ONNX 模型下载)。这条
   设计让 embedding 调用对调用方/测试**完全可见**,也让 P2-B-07 真接入
   bge-m3 时只需提供一个 ``Callable[[list[str]], list[list[float]]]``,
   无需实现 chroma 的 ``EmbeddingFunction`` 抽象基类。
4. **collection 名由调用方指定**:``add / query / delete / count`` 都接
   ``collection: str`` 参数。P2-B-09 / P2-B-13 调用方按"角色 / 伏笔 / 时间
   线 / 场景 / ..."等类别选 collection 名。chroma 对 collection 名有正则
   约束(``[a-zA-Z0-9._-]{3,512}``,首尾不能是 ``.``);本模块**不**预先
   校验,让 chromadb 自身的 ``ValueError`` 自然冒泡(对齐
   ``memory/store.py`` "让 SQLAlchemy 错误自然冒泡"的纪律)。
5. **出参 Pydantic**:``query`` 返回 ``list[QueryHit]``,``QueryHit`` 是
   Pydantic 模型;对齐 P1 service 与 ``memory/store.py`` 的"入 / 出
   Pydantic"风格,避免上层处理 chromadb 原生 dict-of-lists 结构。
6. **collection 不存在的语义**:普通 ``add / query / delete / count`` 仍走
   ``get_or_create_collection``，保持首次使用自动建库；冷启动取证专用的
   ``snapshot_by_chapter_id`` 只取既有 collection，不存在时直接返回空摘要，
   绝不因“只读审计”创建 collection。

边界
----

- 长度不一致 / 非正 ``top_k`` / 非法参数 → ``VectorStoreError``,语义对齐
  ``memory/store.py`` 的 ``MemoryStoreError``。
- chromadb 自身的 ``ValueError`` / ``InvalidCollectionException`` 等错误
  自然冒泡(主键重复、非法 collection 名、where 子句格式错等),由调用方
  按业务翻译;本模块不重写错误语义。
- 工作区隔离由 ``persist_dir`` 物理目录达成;本模块**不**在 collection 名
  里再编进 ``workspace_id``——目录已隔离,collection 名简洁更便于
  retriever 拼字符串。
"""

from __future__ import annotations

import hashlib
import json
import shutil
import tempfile
from collections.abc import Callable
from pathlib import Path
from typing import Any

import chromadb
from chromadb.api import ClientAPI
from chromadb.config import Settings
from chromadb.errors import NotFoundError
from pydantic import BaseModel, ConfigDict, Field

EmbeddingFunction = Callable[[list[str]], list[list[float]]]
"""embedding 函数签名:``texts: list[str]`` → ``list[list[float]]``。

P2-B-07(bge-m3)实现需符合此签名,1024 维输出。本模块对维度数不强制——
只要同一 ``VectorStore`` 实例内 fn 产出维度一致即可(chroma collection 首次
add 时锁定维度,后续 add 不一致会被 chromadb 自身抛错)。
"""


class QueryHit(BaseModel):
    """``query`` 单条命中结果。

    Pydantic 模型,对齐 P1 service / ``memory/store.py`` "入 / 出 Pydantic"
    风格,避免调用方处理 chromadb 原生 dict-of-lists 结构。
    """

    model_config = ConfigDict(frozen=True)

    id: str
    document: str
    metadata: dict[str, Any] = Field(default_factory=dict)
    distance: float


class VectorStoreError(Exception):
    """向量存储层操作失败时抛出。

    主要触发场景:``add`` ids/documents/metadatas 长度不一致、``query`` 非正
    ``top_k``。chromadb 自身错误(非法 collection 名、维度不一致、where 子句
    格式错等)自然冒泡。
    """


class VectorChapterSnapshot(BaseModel):
    """按 ``metadata.chapter_id`` 冻结的 Chroma 内容快照。"""

    model_config = ConfigDict(frozen=True)

    count: int = Field(..., ge=0)
    digest: str = Field(..., pattern=r"^[0-9a-f]{64}$")


def _empty_vector_digest() -> str:
    return hashlib.sha256(b"[]").hexdigest()


def _vector_json_value(value: Any) -> Any:
    if hasattr(value, "tolist"):
        value = value.tolist()
    if value is None or isinstance(value, (bool, int, float, str)):
        return value
    if isinstance(value, dict):
        return {
            str(key): _vector_json_value(item)
            for key, item in sorted(value.items(), key=lambda pair: str(pair[0]))
        }
    if isinstance(value, (list, tuple)):
        return [_vector_json_value(item) for item in value]
    return str(value)


class VectorStore:
    """单工作区的 Chroma 向量存储封装(P2-B-06)。

    实例化绑定 ``workspace_id`` + ``persist_dir``;对外暴露
    ``add / query / delete / count`` 四接口,collection 名由调用方动态指定。
    """

    def __init__(
        self,
        workspace_id: str,
        *,
        embedding_fn: EmbeddingFunction,
        persist_dir: Path,
    ) -> None:
        """初始化向量存储。

        :param workspace_id: 工作区 ID,目前仅用于实例属性 + 错误信息;不参与
            collection 命名(物理目录已隔离)。
        :param embedding_fn: 注入式 embedding 函数,签名
            ``Callable[[list[str]], list[list[float]]]``。每次 ``add`` /
            ``query`` 都会同步调用一次。
        :param persist_dir: 持久化目录(必填)。生产路径通常为
            ``WorkspacePaths.chroma_dir``;测试用 ``tmp_path / 'chroma'``。
            目录不存在时由 chromadb 自身创建;不同 ``persist_dir`` 之间
            chromadb 自然隔离(同一进程亦然)。
        """
        self.workspace_id = workspace_id
        self._embedding_fn = embedding_fn
        self._client: ClientAPI = chromadb.PersistentClient(
            path=str(persist_dir),
            settings=Settings(anonymized_telemetry=False),
        )

    # ---- 内部辅助 -----------------------------------------------------------

    def _get_collection(self, name: str) -> Any:
        """取或建 collection;``embedding_function=None`` 显式禁用 chroma 默认
        ``DefaultEmbeddingFunction``(避免触发 ONNX 模型下载)——VectorStore
        在 ``add`` / ``query`` 内部预计算 embedding 后显式传 ``embeddings=``,
        chroma 永远不需要自己算 embedding。
        """
        return self._client.get_or_create_collection(
            name=name, embedding_function=None
        )

    # ---- 对外接口 -----------------------------------------------------------

    def add(
        self,
        collection: str,
        *,
        ids: list[str],
        documents: list[str],
        metadatas: list[dict[str, Any]] | None = None,
    ) -> None:
        """批量写入文档到指定 collection。

        - ``ids`` / ``documents`` 长度必须一致,``metadatas`` 若提供长度也必
          须等于 ``ids``,否则抛 ``VectorStoreError``。
        - ``ids`` 为空列表 → 静默 no-op(对齐 P2-B-09 批量增量写场景:可能
          一次抽取无新条目)。
        - ``documents`` 通过注入的 ``embedding_fn`` 一次批量算 embedding,
          再以 ``embeddings=`` 显式传 chroma。

        :raises VectorStoreError: 长度不一致。
        """
        if len(ids) != len(documents):
            raise VectorStoreError(
                f"ids ({len(ids)}) 与 documents ({len(documents)}) 长度不一致"
            )
        if metadatas is not None and len(metadatas) != len(ids):
            raise VectorStoreError(
                f"metadatas ({len(metadatas)}) 与 ids ({len(ids)}) 长度不一致"
            )
        if not ids:
            return
        embeddings = self._embedding_fn(documents)
        # 用 upsert 而非 add(v1.1 w4 b1):同 id → 覆盖(对齐 memory_extractor
        # _persist_to_vector_store docstring "同 ID 行为是 upsert" 的声明)。
        # chroma 原生 .add() 遇已存在 id 抛 DuplicateIDError;rewind soft_delete_by
        # _chapter_id 只标 metadata revoked=True 不物理删 → 重跑同章 extract 写同 id
        # 向量必撞 duplicate(luo-clean-v2 ch-7 重跑现场)。upsert 同时复活被软删的
        # 向量(新 metadata 不含 revoked → retriever 重新可见),正常(非重跑)路径
        # id 不重复时 upsert 与 add 等价,无副作用。
        self._get_collection(collection).upsert(
            ids=ids,
            documents=documents,
            embeddings=embeddings,
            metadatas=metadatas,
        )

    def query(
        self,
        collection: str,
        *,
        query_text: str,
        top_k: int = 20,
        where: dict[str, Any] | None = None,
    ) -> list[QueryHit]:
        """检索与 ``query_text`` 最相近的 ``top_k`` 条文档。

        - ``query_text`` 通过注入 fn 算单条 embedding,再以
          ``query_embeddings=[...]`` 传 chroma。
        - ``where`` 透传 chroma metadata 过滤(``None`` → 不过滤)。
        - collection 不存在 / 为空 → 返回 ``[]``(``get_or_create_collection``
          兜底)。
        - 实际命中数可能小于 ``top_k``(集合不够大);返回的 ``QueryHit``
          按 ``distance`` 升序(chroma 默认行为,L2 距离越小越相似)。

        :raises VectorStoreError: ``top_k`` <= 0。
        """
        if top_k <= 0:
            raise VectorStoreError(f"top_k 必须为正整数,实际 {top_k}")
        query_embedding = self._embedding_fn([query_text])[0]
        result = self._get_collection(collection).query(
            query_embeddings=[query_embedding],
            n_results=top_k,
            where=where,
        )
        ids = result["ids"][0]
        documents = result["documents"][0]
        metadatas_raw = result["metadatas"][0]
        distances = result["distances"][0]
        return [
            QueryHit(
                id=ids[i],
                document=documents[i],
                metadata=metadatas_raw[i] if metadatas_raw[i] is not None else {},
                distance=float(distances[i]),
            )
            for i in range(len(ids))
        ]

    def delete(self, collection: str, *, ids: list[str]) -> None:
        """按 ids 批量删除。

        - ``ids`` 为空 → 静默 no-op。
        - 不存在的 id 不抛错(chroma 行为,对齐"幂等删除"语义)。
        - collection 不存在 → 通过 ``get_or_create_collection`` 自动建空
          collection 后 delete,等同 no-op。
        """
        if not ids:
            return
        self._get_collection(collection).delete(ids=ids)

    def count(self, collection: str) -> int:
        """返回 collection 内文档条数;不存在 → ``0``(自动建空)。"""
        return self._get_collection(collection).count()

    def count_by_chapter_id(self, collection: str, *, chapter_id: str) -> int:
        """按 ``metadata.chapter_id`` 统计文档，不计算 embedding、不改数据。

        冷启动恢复用它判断中断的富记忆 Worker 是否已触及 Chroma；与
        ``soft_delete_by_chapter_id`` 共用同一 metadata 条件，但这里只读。
        """
        return self.snapshot_by_chapter_id(
            collection,
            chapter_id=chapter_id,
        ).count

    def snapshot_by_chapter_id(
        self,
        collection: str,
        *,
        chapter_id: str,
    ) -> VectorChapterSnapshot:
        """只读冻结本章向量 ids/document/metadata/embedding 的稳定摘要。"""

        if not chapter_id:
            raise VectorStoreError("chapter_id 不能为空字符串")
        try:
            existing = self._client.get_collection(
                name=collection,
                embedding_function=None,
            )
        except NotFoundError:
            return VectorChapterSnapshot(count=0, digest=_empty_vector_digest())
        result = existing.get(
            where={"chapter_id": chapter_id},
            include=["documents", "metadatas", "embeddings"],
        )
        ids = list(result.get("ids", []))
        documents_raw = result.get("documents")
        metadatas_raw = result.get("metadatas")
        embeddings_raw = result.get("embeddings")
        documents = (
            list(documents_raw)
            if documents_raw is not None
            else [None] * len(ids)
        )
        metadatas = (
            list(metadatas_raw)
            if metadatas_raw is not None
            else [None] * len(ids)
        )
        embeddings = (
            list(embeddings_raw)
            if embeddings_raw is not None
            else [None] * len(ids)
        )
        if not (
            len(documents) == len(metadatas) == len(embeddings) == len(ids)
        ):
            raise VectorStoreError("Chroma 本章快照列长度不一致")
        entries = sorted(
            (
                {
                    "id": entry_id,
                    "document": _vector_json_value(documents[index]),
                    "metadata": _vector_json_value(metadatas[index]),
                    "embedding": _vector_json_value(embeddings[index]),
                }
                for index, entry_id in enumerate(ids)
            ),
            key=lambda entry: entry["id"],
        )
        encoded = json.dumps(
            entries,
            ensure_ascii=False,
            sort_keys=True,
            separators=(",", ":"),
        ).encode("utf-8")
        return VectorChapterSnapshot(
            count=len(entries),
            digest=hashlib.sha256(encoded).hexdigest(),
        )

    def soft_delete_by_chapter_id(
        self,
        collection: str,
        *,
        chapter_id: str,
    ) -> int:
        """按 ``metadata.chapter_id`` 软删除(P6-B-01 rewind 端点入口)。

        把所有 ``metadata.chapter_id == chapter_id`` 的文档 metadata 合并
        ``{"revoked": True}``,**不物理删除**(可恢复);检索时由
        ``retriever.retrieve_memories_for_chapter`` 默认 where filter 加
        ``revoked != True`` 排除。

        - 命中 0 条 → 静默 no-op,返 ``0``(对齐"按 chapter_id 撤回"幂等
          语义:再次 rewind 同章不抛错)。
        - 命中 N 条 → 批量 ``collection.update`` 写新 metadatas,返 ``N``。
        - collection 不存在 → ``get_or_create_collection`` 兜底 + ``get``
          返空,等同 no-op 返 ``0``。

        :param collection: chroma collection 名。
        :param chapter_id: 软删除条件(``metadata.chapter_id`` 字段值匹配)。
        :returns: 实际软删除的文档条数。
        """
        if not chapter_id:
            raise VectorStoreError("chapter_id 不能为空字符串")
        col = self._get_collection(collection)
        result = col.get(where={"chapter_id": chapter_id})
        ids = result.get("ids", [])
        metadatas_raw = result.get("metadatas", []) or []
        if not ids:
            return 0
        new_metadatas: list[dict[str, Any]] = []
        for meta in metadatas_raw:
            updated = dict(meta) if meta is not None else {}
            updated["revoked"] = True
            new_metadatas.append(updated)
        col.update(ids=ids, metadatas=new_metadatas)
        return len(ids)

    def backfill_chapter_seq(self, collection: str, *, dry_run: bool = False) -> int:
        """G1(v1.1 wave 4 session 1):给缺 ``chapter_seq`` metadata 的旧条目回填。

        0016 / G1 起 ``memory_extractor._make_metadata`` 写入 ``chapter_seq``
        (从 ``chapter_id ch-NNNN`` 解析的全局序),供 retriever ``before_chapter_seq``
        时序过滤。**既有工作区**(本改动前入库)的旧条目无该字段,retriever 用
        Chroma ``$lt`` 过滤时会被静默排除 → 本方法遍历 collection,对已有
        ``chapter_id`` 且能解析全局序、但缺 ``chapter_seq`` 的条目补字段。

        - 已有 ``chapter_seq`` 的条目 → 跳过(幂等,可重复跑)。
        - ``chapter_id`` 缺失 / 非 ``ch-NNNN`` 模式 → 跳过(无法解析全局序)。
        - collection 不存在 / 空 → 返 ``0``。

        :param collection: chroma collection 名(典型 ``"memories"``)。
        :param dry_run: ``True`` 时只统计**需要**回填的条目数,不实际写盘。
        :returns: 实际(或 dry_run 下"将")回填的条目数。
        """
        from src.core.models.chapter_card import chapter_seq_from_id

        col = self._get_collection(collection)
        result = col.get()
        ids = result.get("ids", [])
        metadatas_raw = result.get("metadatas", []) or []
        if not ids:
            return 0
        update_ids: list[str] = []
        update_metadatas: list[dict[str, Any]] = []
        for entry_id, meta in zip(ids, metadatas_raw):
            meta_dict = dict(meta) if meta is not None else {}
            if "chapter_seq" in meta_dict:
                continue
            chapter_id = meta_dict.get("chapter_id")
            if not isinstance(chapter_id, str):
                continue
            seq = chapter_seq_from_id(chapter_id)
            if seq is None:
                continue
            meta_dict["chapter_seq"] = seq
            update_ids.append(entry_id)
            update_metadatas.append(meta_dict)
        if not update_ids:
            return 0
        if dry_run:
            return len(update_ids)
        col.update(ids=update_ids, metadatas=update_metadatas)
        return len(update_ids)


def count_persisted_chapter_vectors_read_only(
    *,
    workspace_id: str,
    persist_dir: Path,
    collection: str,
    chapter_id: str,
) -> int:
    """在临时副本中审计 Chroma，绝不创建或打开来源目录。"""

    if not persist_dir.is_dir():
        return 0

    def _embedding_must_not_run(_texts: list[str]) -> list[list[float]]:
        raise VectorStoreError("只读 Chroma 审计不得调用 embedding")

    with tempfile.TemporaryDirectory(
        prefix="novel-engine-chroma-audit-"
    ) as temp_dir:
        snapshot_dir = Path(temp_dir) / "chroma"
        shutil.copytree(persist_dir, snapshot_dir)
        snapshot = VectorStore(
            workspace_id,
            embedding_fn=_embedding_must_not_run,
            persist_dir=snapshot_dir,
        )
        return snapshot.count_by_chapter_id(
            collection,
            chapter_id=chapter_id,
        )


def inspect_persisted_chapter_vectors_read_only(
    *,
    workspace_id: str,
    persist_dir: Path,
    collection: str,
    chapter_id: str,
) -> VectorChapterSnapshot:
    """在临时副本中读取 Chroma 内容摘要，来源目录保持逐字不变。"""

    if not persist_dir.is_dir():
        return VectorChapterSnapshot(count=0, digest=_empty_vector_digest())

    def _embedding_must_not_run(_texts: list[str]) -> list[list[float]]:
        raise VectorStoreError("只读 Chroma 审计不得调用 embedding")

    with tempfile.TemporaryDirectory(
        prefix="novel-engine-chroma-audit-"
    ) as temp_dir:
        snapshot_dir = Path(temp_dir) / "chroma"
        shutil.copytree(persist_dir, snapshot_dir)
        snapshot = VectorStore(
            workspace_id,
            embedding_fn=_embedding_must_not_run,
            persist_dir=snapshot_dir,
        )
        return snapshot.snapshot_by_chapter_id(
            collection,
            chapter_id=chapter_id,
        )
