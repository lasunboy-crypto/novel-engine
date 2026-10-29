"""伏笔模型(对应 PROJECT_SPEC §3.4 PlotThread)。

字段在 P2-B-02 由维护人现场补齐,基于 FRONTEND_SPEC §4.5(伏笔管理页面字段)
反推。spec §3.4 字面"沿用 v1.0,各加 ``workspace_id``"但 v1.0 不在 git history,
属 spec 内部"引用未定义文档"歧义,处置见 docs/impl/p2_b_02_plot_timeline_model.md §4。
"""

from datetime import datetime
from typing import Literal

from pydantic import BaseModel, Field


class PlotThread(BaseModel):
    """伏笔(plot thread)。

    描述本书一条伏笔从埋下到兑现的全生命周期。状态机沿用 FRONTEND_SPEC §4.5:
    planted → developing → resolved | abandoned。重要性 1-5,供前端筛选与
    Reviewer 评估"重要伏笔是否被忽视"。工作区物理隔离,对齐 CLAUDE.md 硬约束 9。
    """

    thread_id: str = Field(..., description="伏笔唯一 ID,工作区内不冲突")
    workspace_id: str = Field(..., description="所属工作区 ID")
    title: str = Field(..., description="伏笔标题(列表显示用)")
    thread_type: Literal[
        "plot_event", "foreshadowing", "metaphor", "callback"
    ] = Field(
        "plot_event",
        description=(
            "伏笔类型显式分类(v1.1 wave 4 session 2 / G2 引入):"
            "``plot_event`` 普通剧情线 / ``foreshadowing`` 伏笔 / "
            "``metaphor`` 隐喻象征 / ``callback`` 回响呼应。默认 "
            "``plot_event`` 向后兼容旧数据(Alembic 0017 server_default)。"
            "辅助 UI 分类展示;G2 callback 链路按 thread_id 精确 lookup,"
            "不依赖本字段。"
        ),
    )
    status: Literal["planted", "developing", "resolved", "abandoned"] = Field(
        ..., description="伏笔状态:埋下 / 发展 / 已兑现 / 已放弃"
    )
    importance: int = Field(
        ..., ge=1, le=5, description="重要性 1-5(FRONTEND_SPEC §4.5 字面范围)"
    )
    planted_chapter: str | None = Field(
        None,
        description=(
            "埋伏笔的章节 ID。v1.0.x B2-01 起改可空:outline 阶段批落 plot_threads "
            "时没有具体章节,落 None;后续用户手动编辑 / MemoryExtractor 反推时回填。"
        ),
    )
    planned_payoff: str | None = Field(
        None, description="计划兑现的章节 ID,初创时可空"
    )
    actual_payoff: str | None = Field(
        None, description="实际兑现的章节 ID,未兑现为 None"
    )
    description: str = Field(..., description="伏笔详细描述(详情抽屉用)")
    related_characters: list[str] = Field(
        default_factory=list, description="相关角色 ID 列表"
    )
    created_at: datetime = Field(..., description="创建时间(UTC)")
    last_updated_chapter: str | None = Field(
        None, description="最近被更新的章节 ID,未更新为 None"
    )
