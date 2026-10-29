"""卷 / 弧 / 张力曲线点 Pydantic 模型(对应 PROJECT_SPEC §3 + FRONTEND_SPEC §4.2 / §4.6 + ROADMAP §P5)。

P5-B-01 引入。对应数据模型按 plan-kickoff 决策 2=C 处置:**Volume 主表落 SQLite**
(支持 UI 列表查询 + 跨表 FK,例如 P5+ ``metrics_per_chapter.volume_id`` 反查),
**Arc / TensionCurvePoint 走 JSON 字段嵌套**(``arcs_json`` / ``tension_curve_json``
两列,SQLAlchemy ``sa.JSON`` 自动 dict ↔ json.dumps,对齐 P4-B-01 ``report_json``
先例);outline 整体 SSOT 仍在 ``workspaces/<id>/data/outline/volume_plans.json``
文件,DB 是查询缓存。

设计要点:

1. ``volume_id``:工作区内唯一,字符集走全局 ``ID_PATTERN``(对齐 chapter_id /
   workspace_id 模式)。
2. ``tension_template``:**Literal 4 档**字面值严格对齐 FRONTEND_SPEC §4.2 字面
   ``英雄之旅 / 平稳日常 / 强刺激爽文 / 自由`` → 对应英文枚举 ``heros_journey /
   steady_daily / strong_stimulation / free``。**前端表单字段下拉直接使用本枚举**
   字面值,后端不做中英文映射(避免下游 P5-F-02 表单字段名错位)。
3. ``arcs``:卷内"弧"(剧情段)列表;每个弧覆盖 ``chapter_range = (start, end)``
   闭区间(``1 <= start <= end <= chapter_count``)。**P5-B-04 outline:commit
   gate 校验 [HARD] 副本类剧情 ≥ 3 章**(PROJECT_SPEC §4.1)= ``end - start + 1
   >= 3``;数据结构在本任务先落,gate 逻辑在 P5-B-04 实施。
4. ``tension_curve``:卷内**每章一个 ``TensionCurvePoint``**(``chapter_number``
   + ``tension`` 1-10);长度严格等于 ``chapter_count``(Pydantic ``@model_validator``
   校验)。``tension_template.apply()`` 在 P5-B-09 ``volumes:apply-template`` 端点
   消费,本任务先内置 4 模板字典(见 ``src/core/models/tension_curve.py``)。
5. ``arc_phase``:沿用 ``ChapterCard.arc_phase`` 5 档(对齐 PROJECT_SPEC §3.2);
   本任务 ``Arc`` 模型不重定义,从 chapter_card 引用。
6. **时间字段** ``DateTime`` UTC + ``_at`` 后缀(对齐 CLAUDE.md 命名约定)。
"""

import datetime as _dt
from typing import Literal

from pydantic import BaseModel, Field, field_validator, model_validator

from src.core.models import ID_PATTERN, WORKSPACE_ID_PATTERN

TENSION_TEMPLATE_VALUES: tuple[str, ...] = (
    "heros_journey",
    "steady_daily",
    "strong_stimulation",
    "free",
)
"""``Volume.tension_template`` 允许的 4 档枚举值集合。

严格对齐 FRONTEND_SPEC §4.2 字面"英雄之旅 / 平稳日常 / 强刺激爽文 / 自由";
模板字典见 ``src/core/models/tension_curve.py``。
"""

TensionTemplateName = Literal[
    "heros_journey",
    "steady_daily",
    "strong_stimulation",
    "free",
]


class TensionCurvePoint(BaseModel):
    """卷内单章张力点(``Volume.tension_curve`` 列表元素)。

    1:1 对应 ``Chapter`` — 每章一个点,值域 ``tension`` 1-10
    (与 ``ChapterCard.tension_level`` 对齐)。``is_arc_finale`` 与
    ``ChapterCard.is_arc_finale`` 是同名同义字段,但本模型为 outline
    级缓存,生成 ChapterCard 时由 ``ChapterCardGenerator`` 透传。
    """

    chapter_number: int = Field(..., ge=1, description="卷内章序号(从 1 起)")
    tension: int = Field(..., ge=1, le=10, description="张力等级 1-10")
    is_arc_finale: bool = Field(False, description="本章是否为弧线终章")


class Beat(BaseModel):
    """弧内"beat"(剧情节奏单元),覆盖 arc 内连续章节子区间。

    v1.1 wave 1 session 1 引入(G3 beat 拆分路径,plan-kickoff Q7=B 拍板)。

    **动机**:LLM 平铺燃文倾向治理 — arc 粒度 10 章太大,LLM 在 arc 终章前
    几章就提前推进 arc 高潮(luo-cloud-test ch-10 把 arc-3 核心剧情"返回地球"
    提前到 arc-1 末章触发,5 卷全失守)。把 arc 拆成更细的 beat
    (3-5 章 / beat),每 beat 显式声明"完成什么(must_advance)+ 不许触发
    什么(forbidden_advance)",mini_plan 按 beat 而非 arc 校验。

    **字段设计**(plan §G3 Q3=A 拍板,5 字段):

    - ``beat_id``:工作区内不冲突,沿用 ``ID_PATTERN`` 字符集(对齐 arc_id 同型)
    - ``chapter_range``:覆盖章节闭区间 ``(start, end)``;必须落在所属
      ``Arc.chapter_range`` 内(由 ``Volume._validate_arcs_within_chapter_count``
      间接保证)+ beats 间不重叠(由 ``Arc._validate_beats_no_overlap`` 校验)
    - ``summary``:一段话描述本 beat 推进范围(给 LLM-as-judge validator 看的
      ground truth)
    - ``must_advance``:本 beat 必须推进的事件 list(自然语言描述,给
      ``beat_alignment_validator`` 当 yes/no 询问素材)
    - ``forbidden_advance``:本 beat 禁止触发的事件 list(典型:后续 arc
      的核心剧情字面;LLM-as-judge 命中即抛 ValueError → retry-with-feedback)

    **粒度约束**(plan §G3 Q2=A 拍板):LLM 自由拆 2-4 beats/arc;
    数据模型层不强制 ``len(beats) ∈ [2, 4]``(允许空 list 兼容旧 arc 数据),
    粒度引导在 ``outline_builder volume_plans prompt v6`` 字面落实(plan §G3 Q2=A)。

    **向后兼容**:Pydantic 默认 ``beats=[]`` — 旧 arcs_json(无 beats 字段)
    反序列化时落空 list,mini_plan + validator 走 arc-level fallback 路径
    (无 beat 时不调 beat_alignment_validator,沿用既有 arc summary
    引导)。
    """

    beat_id: str = Field(
        ...,
        pattern=ID_PATTERN,
        description="beat 唯一 ID,工作区内不冲突(字符集见 ID_PATTERN)",
    )
    chapter_range: tuple[int, int] = Field(
        ...,
        description=(
            "覆盖章节闭区间 (start, end);必须落在所属 Arc.chapter_range 内 + "
            "同 arc 内 beats 不重叠(@model_validator 校验)"
        ),
    )
    summary: str = Field(
        ..., min_length=1, description="beat 推进范围(给 LLM-as-judge 看的 ground truth)"
    )
    must_advance: list[str] = Field(
        default_factory=list,
        description=(
            "本 beat 必须推进的事件 list(自然语言;喂 beat_alignment_validator "
            "yes/no 询问)"
        ),
    )
    forbidden_advance: list[str] = Field(
        default_factory=list,
        description=(
            "本 beat 禁止触发的事件 list(典型:后续 arc 核心剧情字面;命中即"
            "抛 ValueError → retry-with-feedback)"
        ),
    )

    @model_validator(mode="after")
    def _validate_range_monotonic(self) -> "Beat":
        start, end = self.chapter_range
        if start < 1:
            raise ValueError(
                f"Beat.chapter_range start must be >= 1, got {start}"
            )
        if end < start:
            raise ValueError(
                f"Beat.chapter_range end ({end}) must be >= start ({start})"
            )
        return self


class Arc(BaseModel):
    """卷内"弧"(剧情段),覆盖连续章节区间。

    ``chapter_range`` 是闭区间 ``(start, end)``;**P5-B-04 outline:commit
    gate 校验 [HARD] 副本类剧情 ≥ 3 章**(PROJECT_SPEC §4.1):
    ``end - start + 1 >= 3``,违规返 422 ``OUTLINE_ARC_TOO_SHORT``。
    数据结构本任务先落,gate 逻辑在 P5-B-04 实施(对齐 p5_plan.md §8)。

    **v1.1 wave 1 session 1 扩展**(G3 beat 拆分路径,Q7=B 拍板):
    新增 ``beats: list[Beat]`` 字段,LLM 自由拆 2-4 beats/arc(由
    outline_builder volume_plans prompt v6 引导)。空 list 时走 arc-level
    fallback 路径,向后兼容旧 arcs_json 数据。详 ``Beat`` docstring。
    """

    arc_id: str = Field(
        ...,
        pattern=ID_PATTERN,
        description="弧唯一 ID,卷内不冲突(字符集见 ID_PATTERN)",
    )
    arc_type: str = Field(
        ...,
        description='弧类型(自由文本,常见值"主线" / "副本" / "支线" / "番外")',
    )
    chapter_range: tuple[int, int] = Field(
        ...,
        description="覆盖章节闭区间 (start, end);[HARD] 副本类 end-start+1 ≥ 3",
    )
    phase: Literal["opening", "rising", "climax", "falling", "resolution"] = Field(
        ..., description="弧在卷中的阶段(对齐 ChapterCard.arc_phase 5 档)"
    )
    summary: str = Field(..., description="弧概要(一段话描述)")
    beats: list[Beat] = Field(
        default_factory=list,
        description=(
            "v1.1 G3 beat 拆分路径(Q7=B):弧内 beat 列表(LLM 自由拆 2-4 beats/arc,"
            "粒度引导在 prompt 层);空 list 走 arc-level fallback(向后兼容)"
        ),
    )

    @model_validator(mode="after")
    def _validate_range_monotonic(self) -> "Arc":
        start, end = self.chapter_range
        if start < 1:
            raise ValueError(
                f"Arc.chapter_range start must be >= 1, got {start}"
            )
        if end < start:
            raise ValueError(
                f"Arc.chapter_range end ({end}) must be >= start ({start})"
            )
        return self

    @model_validator(mode="after")
    def _validate_unique_beat_ids(self) -> "Arc":
        seen: set[str] = set()
        for beat in self.beats:
            if beat.beat_id in seen:
                raise ValueError(f"Duplicate Beat.beat_id within arc: {beat.beat_id}")
            seen.add(beat.beat_id)
        return self

    @model_validator(mode="after")
    def _validate_beats_within_arc_range(self) -> "Arc":
        """每个 Beat.chapter_range 必须落在 Arc.chapter_range 内。"""
        arc_start, arc_end = self.chapter_range
        for beat in self.beats:
            b_start, b_end = beat.chapter_range
            if b_start < arc_start or b_end > arc_end:
                raise ValueError(
                    f"Beat {beat.beat_id} chapter_range ({b_start}, {b_end}) "
                    f"must be within Arc.chapter_range ({arc_start}, {arc_end})"
                )
        return self

    @model_validator(mode="after")
    def _validate_beats_no_overlap(self) -> "Arc":
        """同 arc 内 beats chapter_range 不能重叠(允许相邻 — 上一个 end+1 == 下一个 start)。

        注:不强制 "beats 完整覆盖 arc.chapter_range" — Q2=A 拍板 LLM 自由
        2-4 beats/arc,允许某些章节落在 arc 内但不在任何 beat 内(走
        arc-level fallback)。
        """
        sorted_beats = sorted(self.beats, key=lambda b: b.chapter_range[0])
        for prev, curr in zip(sorted_beats, sorted_beats[1:], strict=False):
            prev_end = prev.chapter_range[1]
            curr_start = curr.chapter_range[0]
            if curr_start <= prev_end:
                raise ValueError(
                    f"Beats {prev.beat_id} (ends ch-{prev_end}) and "
                    f"{curr.beat_id} (starts ch-{curr_start}) overlap"
                )
        return self


class Volume(BaseModel):
    """卷(对应 ``volumes`` 表 + ``workspaces/<id>/data/outline/volume_plans.json``)。

    P5-B-01 落数据结构,P5-B-04 OutlineBuilder volume_plans 段产出 + commit
    gate 校验,P5-B-09 Volumes CRUD + apply-template 端点消费。
    """

    volume_id: str = Field(
        ...,
        pattern=ID_PATTERN,
        description="卷唯一 ID,工作区内不冲突(字符集见 ID_PATTERN)",
    )
    workspace_id: str = Field(
        ..., pattern=WORKSPACE_ID_PATTERN, description="所属工作区 ID"
    )
    title: str = Field(..., min_length=1, description="卷标题")
    summary: str = Field(..., description="卷概要")
    chapter_count: int = Field(..., ge=1, description="卷内章节总数")
    tension_template: TensionTemplateName = Field(
        ...,
        description=(
            "张力曲线模板(4 档:heros_journey / steady_daily / "
            "strong_stimulation / free,对齐 FRONTEND_SPEC §4.2 字面)"
        ),
    )
    arcs: list[Arc] = Field(
        default_factory=list,
        description="卷内弧列表(JSON 字段 arcs_json 序列化,P5-B-04 落值)",
    )
    tension_curve: list[TensionCurvePoint] = Field(
        default_factory=list,
        description=(
            "卷内每章张力点列表(JSON 字段 tension_curve_json 序列化);"
            "长度 == chapter_count(@model_validator 校验,P5-B-04 落值后生效)"
        ),
    )
    created_at: _dt.datetime = Field(
        ..., description="创建时间 ISO 8601 UTC"
    )
    last_updated_at: _dt.datetime = Field(
        ..., description="最近更新时间 ISO 8601 UTC"
    )

    @field_validator("arcs")
    @classmethod
    def _validate_unique_arc_ids(cls, arcs: list[Arc]) -> list[Arc]:
        seen: set[str] = set()
        for arc in arcs:
            if arc.arc_id in seen:
                raise ValueError(f"Duplicate Arc.arc_id within volume: {arc.arc_id}")
            seen.add(arc.arc_id)
        return arcs

    @model_validator(mode="after")
    def _validate_tension_curve_length(self) -> "Volume":
        """``tension_curve`` 非空时长度必须 == ``chapter_count``。

        允许空列表(初始 Volume 仅落 title / chapter_count,curve 待 OutlineBuilder
        volume_plans 段或 apply-template 后填充);填充后强校验长度。
        """
        if self.tension_curve and len(self.tension_curve) != self.chapter_count:
            raise ValueError(
                f"Volume.tension_curve length ({len(self.tension_curve)}) "
                f"must equal chapter_count ({self.chapter_count})"
            )
        return self

    @model_validator(mode="after")
    def _validate_arcs_within_chapter_count(self) -> "Volume":
        """每个 Arc 的 ``chapter_range`` 必须落在 ``[1, chapter_count]`` 内。

        本任务仅校验区间边界;[HARD] 副本类剧情 ≥ 3 章在 P5-B-04 outline:commit
        gate 校验(对齐 p5_plan.md §8,数据结构层不前置校验避免阻塞编辑态)。
        """
        for arc in self.arcs:
            start, end = arc.chapter_range
            if end > self.chapter_count:
                raise ValueError(
                    f"Arc {arc.arc_id} chapter_range end ({end}) exceeds "
                    f"Volume.chapter_count ({self.chapter_count})"
                )
        return self
