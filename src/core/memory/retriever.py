"""P2 向量记忆库:章节卡 → query 文本 → top_K 检索(P2-B-08)。

接收 ``ChapterCard``,组装 query 文本(主线 + 出场角色 + 关联伏笔),
调注入的 ``VectorStore`` 拿 top_K 命中,返回结构化 ``RetrievedMemory``
列表。**P2 不引入重排**(P3 引入)。

为什么这次做
-------------

解锁 P2 向量记忆链路最后一段——P2-B-09 ``MemoryExtractor`` 抽取记忆写
回 ``VectorStore``,Writer 在写下一章时通过本模块拿到与当前章节卡相关
的历史记忆;P2 验收清单第 3 / 4 / 5 条(检索包含上一章信息 / 连续 3 章
无矛盾 / voice forbidden 检出)的命中率依赖本模块组装的 query 质量。

设计要点
--------

1. **入参 ``ChapterCard`` + ``VectorStore`` 注入式**:retriever 不创建
   ``VectorStore``,不感知 chroma 持久化目录;调用方(P2-B-09 / P2-B-13
   / Writer)按工作区生命周期持有一个 ``VectorStore`` 实例,然后传给
   ``retrieve_memories_for_chapter`` 复用。
2. **query 文本组装规则**:沿任务模板字面"主线 + 出场角色 + 关联伏笔"
   逐段拼接。各段对应的 ``ChapterCard`` 字段:

   - **主线**:``must_achieve``(必达事件列表)+ ``setting``(场景设置)
     + ``next_chapter_seed``(给下一章的种子)
   - **出场角色**:``pov_character``(视角角色)+ ``related_characters``
     (出场角色 ID 列表)
   - **关联伏笔**:``related_plot_threads``(关联伏笔 ID 列表)

   各段以 ``\\n`` 拼接成单 query 文本,送 embedding 一次调用即可;空可选
   字段(``must_achieve = []`` / ``related_characters = []`` 等)对应段
   直接跳过,不生成 ``"主线: "`` 这样的空标签。
3. **retriever 不查 SQLite**:本模块单一职责只读 ``ChapterCard``;角色
   / 伏笔的"名字 + 简介"塞进 query 文本是上层调用方的事——调用方可
   先 ``list_characters_by_workspace`` 再把名字 / 简介合并进 query 文
   本(P2-B-09 + 调 retriever 时按需扩),也可 query 后用 ``where`` 子
   句过滤(更高效)。
4. **不引入 instruction-aware**(P2-B-08 自决项,留 P3+):Qwen3-Embedding
   官方 ``prompt_name="query"`` 路径(实测 prompts dict 已定义
   ``Instruct: ...\\nQuery:`` 前缀)涨 1%-5% 检索精度。引入需扩
   ``embed(texts, *, is_query=False)`` 参数 **同时**改 P2-B-06
   ``VectorStore`` query 路径让其透传或绕过(``EmbeddingFunction`` 当
   前签名 ``Callable[[list[str]], list[list[float]]]`` 不带 ``is_query``
   形参)。改 P2-B-06 契约工程代价 > 1-5% 边际精度收益,留 P3 切本地
   Qwen 时统一接入。
5. **不引入重排**(P2 范围外,P3 引入):任务模板字面"P2 不引入重排"。
6. **collection 默认 ``"memories"``**:P2 阶段统一一个 collection 命中
   下游调用方(MemoryExtractor / 静态资产导入 / Writer)预期;P3+ 拆
   多 collection(角色 / 伏笔 / 时间线 / 场景)时调用方可自定义传入。
7. **``top_k`` 默认 ``20``**:对齐任务模板字面 ``K=20``。
8. **空 ``ChapterCard`` 边界**:``ChapterCard`` 的必填字段(``setting``
   / ``pov_character`` / ``next_chapter_seed``)Pydantic 已保证非空字符
   串,所以组装出的 query 文本至少包含场景 + 视角 + 种子三段,**不会**
   出现空 query 文本场景。``must_achieve`` / ``related_characters`` /
   ``related_plot_threads`` 这三个 list 字段允许传空列表(spec 字面无
   ``min_items`` 约束),空时对应段跳过。

边界
----

- ``top_k <= 0`` → ``RetrieverError``(对齐 ``VectorStoreError`` 的语义,
  让调用方按业务翻译)。
- ``where`` 透传 chroma metadata 过滤;``None`` → 不过滤。
- 实际命中数可能 ``< top_k``(集合不够大);返回的 ``RetrievedMemory``
  列表按 ``distance`` 升序(chroma L2 距离越小越相似)。
- ``VectorStore.query`` 自身错误(集合不存在的 chroma 兜底返 ``[]``;非
  正 ``top_k`` 已被本模块前置拦截)自然冒泡。
"""

from __future__ import annotations

from typing import Any

from pydantic import BaseModel, ConfigDict, Field

from src.core.memory.vector import VectorStore
from src.core.models.chapter_card import ChapterCard

DEFAULT_COLLECTION = "memories"
"""P2 阶段统一 collection 名。下游调用方调 retriever 时如不显式传
``collection=`` 参数即用此默认值。P3+ 拆多 collection 时调用方可自定义。"""

DEFAULT_TOP_K = 20
"""任务模板字面 ``K=20`` 的默认值。"""


class RetrievedMemory(BaseModel):
    """retriever 单条检索结果。

    与 ``VectorStore.QueryHit`` 字段实质等价但语义升一档(retriever 是
    "记忆检索"语义层,vector_store 是"向量存储"基础设施层),让上层调
    用方拿到的是"记忆对象"而非"chroma 命中"。``frozen=True`` 让命中结
    果不可变,降低误改风险(retriever 阶段只读)。
    """

    model_config = ConfigDict(frozen=True)

    memory_id: str = Field(..., description="记忆 ID(对齐写入 vector store 时的 chroma id)")
    content: str = Field(..., description="记忆原文(写入时的 document)")
    metadata: dict[str, Any] = Field(
        default_factory=dict,
        description="记忆元数据(由 P2-B-09 写入时附加,如 entity_type / chapter_id 等)",
    )
    distance: float = Field(
        ...,
        description="向量距离(chroma L2,越小越相似)。归一化向量下与余弦距离单调等价。",
    )


class RetrieverError(Exception):
    """retriever 操作失败时抛出。

    主要触发场景:``top_k <= 0``。``VectorStore`` 自身错误(非法 collection
    名 / 维度不一致 / where 子句格式错等)自然冒泡,由调用方按业务翻译。
    """


# ---- 内部辅助 ---------------------------------------------------------------


def assemble_query_text(card: ChapterCard) -> str:
    """根据 ``ChapterCard`` 组装 query 文本。

    返回的字符串结构(每段一行,空可选字段段直接跳过)::

        主线: <must_achieve 用 " | " 拼接>
        场景: <setting>
        种子: <next_chapter_seed>
        视角: <pov_character>
        出场角色: <related_characters 用 " " 拼接>
        关联伏笔: <related_plot_threads 用 " " 拼接>

    必填字段(``setting`` / ``next_chapter_seed`` / ``pov_character``)
    Pydantic 已保证非空字符串,query 文本至少包含场景 + 种子 + 视角三
    段,**不会**返回空字符串。

    :param card: 章节卡。
    :returns: 拼好的 query 文本。
    """
    parts: list[str] = []
    if card.must_achieve:
        parts.append("主线: " + " | ".join(card.must_achieve))
    if card.setting:
        parts.append(f"场景: {card.setting}")
    if card.next_chapter_seed:
        parts.append(f"种子: {card.next_chapter_seed}")
    if card.pov_character:
        parts.append(f"视角: {card.pov_character}")
    if card.related_characters:
        parts.append("出场角色: " + " ".join(card.related_characters))
    if card.related_plot_threads:
        parts.append("关联伏笔: " + " ".join(card.related_plot_threads))
    return "\n".join(parts)


# ---- 对外接口 ---------------------------------------------------------------


def retrieve_memories_for_chapter(
    card: ChapterCard,
    *,
    vector_store: VectorStore,
    collection: str = DEFAULT_COLLECTION,
    top_k: int | None = None,
    where: dict[str, Any] | None = None,
    include_revoked: bool = False,
    workspace_id: str | None = None,
    before_chapter_seq: int | None = None,
) -> list[RetrievedMemory]:
    """根据章节卡检索 top_K 相关记忆。

    流程:
    1. ``assemble_query_text(card)`` 组 query 文本;
    2. 调 ``vector_store.query(collection, query_text=..., top_k=...,
       where=...)`` 拿 ``list[QueryHit]``;
    3. **P6-B-01 起默认过滤 ``revoked: True``** 的软删除条目(对齐
       ``VectorStore.soft_delete_by_chapter_id`` 写入的 metadata);设
       ``include_revoked=True`` 可关闭过滤(debug / 历史回放用)。
    4. 映射到 ``RetrievedMemory`` 出参列表(字段一一对齐:``id`` →
       ``memory_id``、``document`` → ``content``、``metadata`` /
       ``distance`` 透传)。

    :param card: 章节卡。
    :param vector_store: 注入的 ``VectorStore`` 实例(已绑定 workspace_id
        + embedding_fn + persist_dir)。
    :param collection: chroma collection 名,默认 ``"memories"``。
    :param top_k: 命中数上限。**P7-B-05 起**默认 ``None``,语义为"按工作区配置
        解析"—— ``workspace_id`` 给定时调 ``ConfigService.load_retrieval_config``
        取工作区合并值(P6-B-14 落地的 ``retrieval.top_k`` 字段),否则回退
        ``DEFAULT_TOP_K=20``。caller 显式传 ``int`` 覆盖工作区配置(向后兼容
        P2-P6 既有 caller 显式 int 调用)。
    :param where: 透传 chroma metadata 过滤(``None`` → 不过滤;非 None
        且 ``include_revoked=False`` 时与 ``{"revoked": {"$ne": True}}``
        以 ``$and`` 合并)。
    :param include_revoked: 是否包含软删除条目,默认 ``False``。
    :param workspace_id: P7-B-05 加,用于 ``top_k=None`` 时从
        ``ConfigService.load_retrieval_config(workspace_id)`` 读工作区覆盖。
        ``None`` 时退化到 ``DEFAULT_TOP_K=20`` 文字常量(P2-P6 legacy 兼容)。
        配置加载失败 swallow + ``DEFAULT_TOP_K`` 兜底(对齐 P2-B-19 /
        P3-B-07 "辅助任务失败 swallow + 默认兜底"先例)。
    :param before_chapter_seq: G1(v1.1 wave 4 session 1)时序过滤。给定时只检索
        Chroma metadata ``chapter_seq < before_chapter_seq`` 的条目(防止把"未来 /
        本章"记忆 + 过期状态检索进 Writer prompt)。值传**本章全局序**(从
        ``chapter_id ch-NNNN`` 解析)。``None`` 时不做时序过滤(P2-P7 既有 caller
        向后兼容)。**注意**:无 ``chapter_seq`` metadata 的旧条目(0016 前入库 /
        ``ch-NNNN`` 模式外)会被 Chroma ``$lt`` 静默排除 → 既有工作区需先跑
        backfill 脚本补 ``chapter_seq``(对齐 wave 3 E6 backfill 模式)。
    :raises RetrieverError: 解析后的 ``top_k <= 0``。
    :returns: ``list[RetrievedMemory]`` 按 ``distance`` 升序;集合为空 / 不
        足 ``top_k`` 时长度可能小于 ``top_k``。
    """
    effective_top_k = _resolve_top_k(top_k=top_k, workspace_id=workspace_id)
    if effective_top_k <= 0:
        raise RetrieverError(f"top_k 必须为正整数,实际 {effective_top_k}")
    top_k = effective_top_k
    query_text = assemble_query_text(card)
    where = _merge_chapter_seq_filter(where, before_chapter_seq=before_chapter_seq)
    effective_where = _merge_revoked_filter(where, include_revoked=include_revoked)
    hits = vector_store.query(
        collection=collection,
        query_text=query_text,
        top_k=top_k,
        where=effective_where,
    )
    return [
        RetrievedMemory(
            memory_id=hit.id,
            content=hit.document,
            metadata=hit.metadata,
            distance=hit.distance,
        )
        for hit in hits
    ]


def _resolve_top_k(
    *, top_k: int | None, workspace_id: str | None
) -> int:
    """P7-B-05:top_k 解析优先级 — caller 显式 > workspace 配置 > ``DEFAULT_TOP_K``。

    - caller 给定 ``int`` → 直接用(覆盖工作区配置,向后兼容 P2-P6 显式 int 调用)
    - ``top_k=None`` + ``workspace_id`` 给定 → 调
      ``ConfigService.load_retrieval_config(workspace_id).top_k``(P6-B-14 字段)
    - ``top_k=None`` + ``workspace_id=None`` → ``DEFAULT_TOP_K=20``(P2-B-08 文字常量)
    - 配置加载失败 → swallow + ``DEFAULT_TOP_K``(辅助任务"加载失败兜底"先例)

    延迟 import ``ConfigService`` 规避 ``src.services.config_service`` ↔
    ``src.core.memory.retriever`` 顶层循环:retriever 是 core 层,config_service
    是 services 层,顶层 import 会形成跨层倒挂;函数体内 import 在调用时模块已
    完成初始化,无 ImportError 风险。
    """
    if top_k is not None:
        return top_k
    if workspace_id is None:
        return DEFAULT_TOP_K
    try:
        from src.services.config_service import ConfigService

        return ConfigService().load_retrieval_config(workspace_id).top_k
    except Exception:  # noqa: BLE001 — swallow per "辅助任务失败兜底"先例
        return DEFAULT_TOP_K


def _merge_revoked_filter(
    where: dict[str, Any] | None,
    *,
    include_revoked: bool,
) -> dict[str, Any] | None:
    """合并 ``revoked != True`` 过滤到 caller 的 where 子句(P6-B-01)。

    - ``include_revoked=True`` → 不附加,原样透传 ``where``
    - ``where is None`` + 不含 revoked → 返 ``{"revoked": {"$ne": True}}``
    - ``where`` 非空 + 不含 revoked → 用 ``$and`` 合并 caller where 与
      revoked filter(对齐 chromadb 1.x ``$and`` 多条件语义)
    - ``where`` 已含 ``"revoked"`` key → caller 显式覆盖,不再注入(尊重
      caller 显式意图,避免与 caller 自定义条件冲突)
    """
    if include_revoked:
        return where
    revoked_filter = {"revoked": {"$ne": True}}
    if where is None:
        return revoked_filter
    if "revoked" in where:
        return where
    return {"$and": [where, revoked_filter]}


def _merge_chapter_seq_filter(
    where: dict[str, Any] | None,
    *,
    before_chapter_seq: int | None,
) -> dict[str, Any] | None:
    """合并 ``chapter_seq < before_chapter_seq`` 时序过滤到 where(G1)。

    - ``before_chapter_seq is None`` → 原样透传(不过滤)
    - ``where is None`` → 返 ``{"chapter_seq": {"$lt": N}}``
    - ``where`` 已显式含 ``"chapter_seq"`` key → 尊重 caller 显式覆盖,不注入
    - 否则 ``$and`` 合并(``where`` 自身是 ``{"$and": [...]}`` 时把 seq 条件
      并入同一 ``$and`` 列表保持扁平,避免不必要嵌套)
    """
    if before_chapter_seq is None:
        return where
    seq_filter = {"chapter_seq": {"$lt": before_chapter_seq}}
    if where is None:
        return seq_filter
    if "chapter_seq" in where:
        return where
    if "$and" in where and isinstance(where["$and"], list):
        return {"$and": [*where["$and"], seq_filter]}
    return {"$and": [where, seq_filter]}
