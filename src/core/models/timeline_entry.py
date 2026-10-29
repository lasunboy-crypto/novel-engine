"""时间线条目模型(对应 PROJECT_SPEC §3.4 TimelineEntry)。

字段在 P2-B-02 由维护人现场补齐,落地 PROJECT_SPEC §4.2 [HARD] 时间线单调校验。
``time_label`` 给读者看(允许"建元三年春"这类架空 / 模糊纪年),``time_order``
给机器排序 + 单调性校验(纯整数严格递增)——两字段拆开避免强制公历纪年。
"""

from pydantic import BaseModel, Field


class TimelineEntry(BaseModel):
    """时间线条目。

    描述故事内某个事件在叙事时间轴上的位置。Reviewer 用 ``time_order`` 做
    PROJECT_SPEC §4.2 [HARD] 时间线单调校验,前端用 ``time_label`` 显示。
    工作区物理隔离,对齐 CLAUDE.md 硬约束 9。
    """

    entry_id: str = Field(..., description="时间线条目唯一 ID,工作区内不冲突")
    workspace_id: str = Field(..., description="所属工作区 ID")
    chapter_id: str = Field(..., description="该事件发生的章节 ID")
    description: str = Field(..., description="事件描述")
    time_label: str = Field(
        ...,
        description="显示用时间标签,允许架空 / 模糊纪年(如 建元三年春)",
    )
    time_order: int = Field(
        ...,
        description="排序键,严格递增,PROJECT_SPEC §4.2 [HARD] 时间线单调校验依据",
    )
    involved_characters: list[str] = Field(
        default_factory=list, description="涉及角色 ID 列表"
    )
    is_flashback: bool = Field(
        default=False,
        description=(
            "v1.1 E2 引入:标记本 entry 是否描述 flashback / 回忆 / 穿越回过去 / "
            "双时空穿梭的另一时空。``check_timeline_monotonic`` 校验时跳过 "
            "``is_flashback=True`` 的 entry,只在主时间线内做单调校验。"
            "``MemoryExtractor`` prompt 引导 LLM 标记此字段;旧数据反序列化默认 False"
            "(向后兼容)。详 docs/plan/v1_1_candidates.md §E2 紧迫升级证据。"
        ),
    )
