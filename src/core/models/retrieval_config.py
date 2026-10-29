"""检索 top_k 工作区级配置(P6-B-14)。

引擎默认 ``config.yaml`` 的 ``retrieval:`` 段;工作区 ``project.yaml`` 可整段覆盖
(``ConfigService.load_retrieval_config``)。本模块只定义 Pydantic 模型 + 加载入口,
**不修改 retriever / writer 调用链**(``src/core/memory/retriever.py``
``DEFAULT_TOP_K=20`` 默认值与 ``src/core/agents/writer.py`` ``_DEFAULT_RETRIEVER_TOP_K=10``
显式传参均保留;工作区覆盖运行时接入留 P6-F-08+ — 启动指令字面接力 marker)。

字段语义
========

- ``top_k``         : 检索命中数上限。``retriever.retrieve_memories_for_chapter``
  默认 20;工作区级覆盖供 ChapterService / Orchestrator 透传时按工作区配置取默认值
  (caller 显式传 ``top_k=`` 仍优先)。
- ``min_similarity`` : 相似度下界(闭区间)。``0.0`` = 不过滤(默认);``> 0.0`` 时
  caller 按 ``1 - distance >= min_similarity`` 过滤命中结果。P6-B-14 仅暴露字段,
  retriever 接入留 P6-F-08+(本任务自决项 — 不动 retriever 签名)。

字段默认值对齐当前 retriever 实现
================================

- ``top_k`` 20  ← ``src/core/memory/retriever.py`` ``DEFAULT_TOP_K``
- ``min_similarity`` 0.0  ← 当前 retriever 无相似度过滤逻辑,默认不过滤

字段约束
========

- ``top_k >= 1`` (检索至少返 1 条;``retriever.py:195`` ``top_k <= 0`` 抛错对齐)
- ``0.0 <= min_similarity <= 1.0`` (chroma L2 距离归一化后 ``1 - distance`` 落在
  ``[0, 1]`` 区间,与"相似度"语义自然对齐)
"""

from __future__ import annotations

from pydantic import BaseModel, ConfigDict, Field


class RetrievalConfig(BaseModel):
    """``retrieval:`` 配置段(P6-B-14 工作区级检索默认)。

    Pydantic v2 model;字段默认值对齐 ``src/core/memory/retriever.py``
    ``DEFAULT_TOP_K=20`` + 现 retriever 无 ``min_similarity`` 过滤逻辑。

    工作区级 vs caller 显式传参
    ----------------------------
    本模型是**工作区级默认**(项目级,跨章节复用);``writer.write_chapter``
    ``retriever_top_k=10`` 等 caller 显式传参**仍优先**(对齐运行时优先级
    ``caller > Workspace > Engine``)。caller 链接入留 P6-F-08+,本任务
    只暴露端点 + service 方法。
    """

    model_config = ConfigDict(extra="forbid")

    top_k: int = Field(
        default=20,
        ge=1,
        description=(
            "检索命中数上限;对齐 ``retriever.DEFAULT_TOP_K`` 默认 20。"
            "caller 显式传 ``top_k=`` 仍优先(运行时优先级 caller > Workspace > Engine)。"
        ),
    )
    min_similarity: float = Field(
        default=0.0,
        ge=0.0,
        le=1.0,
        description=(
            "相似度下界(闭区间);``0.0`` = 不过滤(默认),"
            "``> 0.0`` 时按 ``1 - distance >= min_similarity`` 过滤。"
            "P6-B-14 暴露字段,retriever 接入留 P6-F-08+。"
        ),
    )
