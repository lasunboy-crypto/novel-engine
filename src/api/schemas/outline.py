"""Outline 路由请求 / 响应 schema(P5-B-04)。

对齐 API_CONTRACT §4.2 关键流程 1 字面 5 端点 + ``src/core/models/outline.py``
4 段产出 Pydantic 模型。

5 个端点的 schema
=================

- ``POST /outline:generate`` — body ``OutlineGenerateIn``,response 含
  ``task_id`` + 4 段产出聚合(P1 同步模式,对齐 chapters ``:write`` 同形)。
- ``POST /outline:regenerate-section`` — body ``OutlineRegenerateSectionIn``
  ``{section, premise, ...}``,response 返单段产出 dict(类型按 section 分)。
- ``GET /outline`` — response ``OutlineOut``,4 段可空。
- ``PUT /outline`` — body ``OutlineUpdateIn``,4 段全 optional patch。
- ``POST /outline:commit`` — 无 body,response ``OutlineCommitResult``。
"""

from __future__ import annotations

from typing import Literal

from pydantic import BaseModel, ConfigDict, Field

from src.core.models.narrative import PlanningMode
from src.core.models.outline import (
    CharactersOutput,
    MiniPlanOutput,
    PlotOutput,
    VolumePlansOutput,
    WorldOutput,
)
from src.core.models.volume import TensionTemplateName

OutlineSectionLiteral = Literal[
    "world", "characters", "plot", "volume_plans", "mini_plan"
]
"""v1.0.x wave 4 session 1 起从 4 段扩到 5 段(加 ``mini_plan``)。"""


# =============================================================================
# 请求 schema
# =============================================================================


class OutlineGenerateIn(BaseModel):
    """``POST /outline:generate`` 请求体。"""

    model_config = ConfigDict(extra="forbid")

    premise: str = Field(
        ..., min_length=1, description="一句话设定(用户输入)"
    )
    total_volumes: int = Field(
        ..., ge=1, le=20, description="目标卷数(1-20)"
    )
    chapters_per_volume: int = Field(
        ..., ge=1, le=200, description="每卷章数(1-200)"
    )
    tension_template: TensionTemplateName = Field(
        ..., description="张力曲线模板(4 档)"
    )
    genre: str = Field(default="", description="题材(可空)")
    planning_mode: PlanningMode = Field("whole_book", description="本次规划完整故事或长篇开局")


class OutlineRegenerateSectionIn(BaseModel):
    """``POST /outline:regenerate-section`` 请求体。

    ``volume_plans`` 段必须同时给 ``total_volumes`` / ``chapters_per_volume``
    / ``tension_template``;其余段可空。``keep_others`` 字段在本端点实际
    行为为"始终从落盘文件读取上游 context,不重跑上游"(对齐 p5_plan §6 +
    docs/impl/p5_b_03 §6 接力 marker),保留字面接受布尔避免破坏 spec。
    """

    model_config = ConfigDict(extra="forbid")

    section: OutlineSectionLiteral = Field(
        ..., description="5 段之一(v1.0.x wave 4 session 1 起加 mini_plan)"
    )
    premise: str = Field(..., min_length=1, description="一句话设定")
    keep_others: bool = Field(
        default=True,
        description=(
            "保留其余段(本端点恒为 True 语义,字段保留向 API_CONTRACT 字面对齐;"
            "上游 context 始终从落盘文件读取,不重跑上游)"
        ),
    )
    total_volumes: int | None = Field(
        default=None, ge=1, le=20, description="(volume_plans 段必传)"
    )
    chapters_per_volume: int | None = Field(
        default=None, ge=1, le=200, description="(volume_plans 段必传)"
    )
    tension_template: TensionTemplateName | None = Field(
        default=None, description="(volume_plans 段必传)"
    )
    genre: str = Field(default="", description="题材(可空)")
    planning_mode: PlanningMode | None = Field(None, description="重生世界时可指定；省略沿用原规划范围")


class OutlineUpdateIn(BaseModel):
    """``PUT /outline`` 请求体。单段或整段 patch(v1.0.x wave 4 session 1 加 ``mini_plan``)。"""

    model_config = ConfigDict(extra="forbid")

    world: WorldOutput | None = None
    characters: CharactersOutput | None = None
    plot: PlotOutput | None = None
    volume_plans: VolumePlansOutput | None = None
    mini_plan: MiniPlanOutput | None = None


# =============================================================================
# 响应 schema
# =============================================================================


class OutlineOut(BaseModel):
    """``GET /outline`` 响应 data 段。

    v1.0.x wave 4 session 1 起 5 段(原 4 段 + ``mini_plan``);全段可空(生成
    过程中部分可读,``mini_plan`` 在 wave 4 前生成的旧工作区 outline 也为空)。
    """

    model_config = ConfigDict(extra="forbid")

    world: WorldOutput | None = None
    characters: CharactersOutput | None = None
    plot: PlotOutput | None = None
    volume_plans: VolumePlansOutput | None = None
    mini_plan: MiniPlanOutput | None = None


class OutlineGenerateResult(BaseModel):
    """``POST /outline:generate`` ``TaskInfo.result`` 段。

    含 ``task_id`` + 5 段完整产出(v1.0.x wave 4 session 1 起从 4 段扩到 5 段
    加 mini_plan)。对齐 P1 同步模式:同步跑完 + 返结果,P3 切后台 worker 时
    改为只返 task_id + WS 推 stage 增量 + ``GET /outline`` 取产出。
    """

    model_config = ConfigDict(extra="forbid")

    task_id: str
    outline: OutlineOut


class OutlineCommitResult(BaseModel):
    """``POST /outline:commit`` 响应 data 段。

    含已落库的 volumes 数(供前端跳转 VolumePlanPage)+ 是否触发 commit
    gate(全过则 ``gates_passed=True``)+ 异步首卡任务 ID。
    """

    model_config = ConfigDict(extra="forbid")

    workspace_id: str
    volumes_count: int
    gates_passed: bool = True
    first_card_task_id: str = Field(
        ...,
        description=(
            "异步生成 ch-0001 的任务 ID。客户端可订阅任务进度；后续章节卡"
            "不预生成，由正式 Run 在上一章富记忆成功后逐章生成。"
        ),
    )


__all__ = [
    "OutlineCommitResult",
    "OutlineGenerateIn",
    "OutlineGenerateResult",
    "OutlineOut",
    "OutlineRegenerateSectionIn",
    "OutlineSectionLiteral",
    "OutlineUpdateIn",
]
