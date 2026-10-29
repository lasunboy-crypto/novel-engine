"""OutlineBuilder 4 段产出 Pydantic 模型(P5-B-03 引入)。

对应 API_CONTRACT §4.2 关键流程 1 字面 WebSocket stage 4 段
``world | characters | plot | volume_plans``;每段是 OutlineBuilder agent
单次串行调用的 LLM 输出 schema。

设计要点:

1. **4 段 schema 独立**:每段对应一个 Pydantic 模型(``WorldOutput`` /
   ``CharactersOutput`` / ``PlotOutput`` / ``VolumePlansOutput``)。下游
   ``outline:regenerate-section`` 端点单段重生时复用同段 schema 解析。
2. **`VolumePlanItem` 不复用 `Volume`**:Outline 阶段是"未提交的计划",
   ``Volume`` 是"提交后的库内卷"。前者无 ``workspace_id`` / ``created_at``
   / ``last_updated_at`` 系统字段、含 ``linked_phases`` / ``linked_turning_points``
   / ``linked_threads`` 跨段关联字段、``tension_curve`` 是 ``list[int]``
   LLM 友好形态;后者是 DB ORM 镜像 + ``TensionCurvePoint`` 含 ``is_arc_finale``
   元数据。P5-B-04 outline:commit 端点负责"VolumePlanItem → Volume"映射。
3. **[HARD] 副本横跨校验留 P5-B-04**:本模型层只校验 arc.chapter_range
   边界 + tension_curve 长度 == chapter_count + arcs 覆盖 [1, chapter_count];
   ``arc_type`` 含"副本" + ``end - start + 1 < 3`` 的 [HARD] 校验在 P5-B-04
   ``src/core/rules/outline_validators.py`` 集中(对齐 p5_plan.md §8 字面
   "outline 级 [HARD] 在此集中,区别于 P4-B-06 ``validators.py`` 章节级
   集中点")。
4. **`ID_PATTERN` 字符集**:沿用 ``src/core/models/__init__.py``,允许字母 /
   数字 / 下划线 / 连字符,1-64 字。
5. **角色 / 阶段 / 转折 / 伏笔 / arc / volume 各级 ID 唯一性**:在数组内做
   字段校验(``@field_validator``),跨数组不校验(后续 outline:commit
   service 层做跨段引用完整性)。
6. **字段最少必要**:模型不写章节级字段(留 P5-B-05 ChapterCardGenerator);
   不写 voice_profile 完整结构(``voice_hint`` 字符串足以,正式 voice_profile
   由 ChapterCardGenerator 派生);不写 metric 字段(P4 范围)。
"""

from __future__ import annotations

from typing import Any, Literal

from pydantic import (
    BaseModel,
    ConfigDict,
    Field,
    field_validator,
    model_validator,
)

from src.core.models import ID_PATTERN
from src.core.models.character import IdentityFacet
from src.core.models.narrative import NarrativeContext, PhaseDevelopment, StoryIntent
from src.core.models.drama import (
    DramaticBeatContract,
    LoadBearingObjectContract,
    VolumeDramaticPromise,
)
from src.core.models.volume import TensionTemplateName

# ---------------------------------------------------------------------------
# Stage 1: World
# ---------------------------------------------------------------------------


class WorldOutput(BaseModel):
    """OutlineBuilder 第 1 段产出:世界观。

    5 字段对齐 ``prompts/outline_builder/world.md`` JSON 契约;非幻想类
    ``power_system`` 允许空字符串,但键不能缺。
    """

    setting: str = Field(..., min_length=1, description="时代背景 + 地理 / 大环境")
    power_system: str = Field(
        ..., description="力量体系 / 修炼体系 / 技术体系(非幻想类允许空字符串)"
    )
    factions: str = Field(..., min_length=1, description="主要势力 / 派别 / 组织")
    culture: str = Field(..., min_length=1, description="文化 / 经济 / 社会基础")
    unique_rules: str = Field(
        ..., description="真正不可破例的世界硬规则；没有则空字符串"
    )
    story_intent: StoryIntent | None = Field(
        None, exclude_if=lambda value: value is None,
        description="程序继承的全书故事定位；旧世界无此字段，不补写历史文件。",
    )


# ---------------------------------------------------------------------------
# Stage 2: Characters
# ---------------------------------------------------------------------------


CharacterRole = Literal["protagonist", "deuteragonist", "antagonist", "supporting"]
"""核心人设角色定位 4 档(对齐 ``prompts/outline_builder/characters.md`` 字面)。"""

MAX_INITIAL_CHARACTER_CORE_FACTS = 8


class InitialCharacterCoreFact(BaseModel):
    """正文开始前已经成立的角色核心事实。

    这不是章节事件，也不授权后续变化；它只把 ``characters`` 段已经明确写出的
    身份、生死、永久伤势、失忆或阵营基线结构化，供第 1 章卡片冻结投影使用。
    ``source_field`` 与逐字证据让代码可以回查事实确实来自同一角色的稳定字段，
    不能由模型另写一条没有上游出处的新设定。
    """

    model_config = ConfigDict(extra="forbid")

    attribute: Literal[
        "identity", "life_status", "permanent_injury", "amnesia", "faction"
    ]
    value: str = Field(..., min_length=1, max_length=160)
    source_field: Literal["archetype", "background", "identity_facets"]
    evidence_quote: str = Field(..., min_length=1, max_length=300)

    @model_validator(mode="after")
    def _validate_value_is_grounded_in_quote(self) -> InitialCharacterCoreFact:
        if self.value not in self.evidence_quote:
            raise ValueError("initial_core_fact.value 必须逐字出现在 evidence_quote")
        return self


class CharacterOutlineItem(BaseModel):
    """OutlineBuilder 第 2 段产出的单个角色条目。

    与 ``src/core/models/character.Character`` 解耦:
    - **Outline 阶段**:LLM 产 ``voice_hint`` 一句话 + 简化字段,无 SQLite ORM 字段。
    - **后续 P5-B-05 ChapterCardGenerator**:基于本条目派生完整 ``Character`` +
      ``VoiceProfile`` 入 SQLite。
    """

    character_id: str = Field(
        ..., pattern=ID_PATTERN, description="稳定 ID,字符集见 ID_PATTERN"
    )
    name: str = Field(..., min_length=1, description="显示名")
    role: CharacterRole = Field(..., description="角色定位 4 档")
    archetype: str = Field(..., min_length=1, description="原型(如'萌新剑修')")
    appearance: str = Field(..., min_length=1, description="外貌描述")
    personality: str = Field(..., min_length=1, description="性格描述")
    background: str = Field(
        ..., min_length=1,
        description="正文开篇前已经成立的背景与经历；未来剧情结果留在主线，不写成既往履历",
    )
    motivation: str = Field(
        ..., min_length=1, description="正文开篇时的驱动力与尚未实现的内心目标",
    )
    present_desire: str = Field(
        "", exclude_if=lambda value: not value,
        description="开篇眼前所求；新叙事交接使用，旧人物允许空。",
    )
    personal_stakes: str = Field(
        "", exclude_if=lambda value: not value,
        description="愿望背后的牵挂、顾忌或可能损失；不统一要求创伤。",
    )
    voice_hint: str = Field(
        ...,
        min_length=1,
        description="一句话说话风格(供 ChapterCardGenerator 派生 voice_profile)",
    )
    identity_facets: list[IdentityFacet] = Field(
        default_factory=list,
        description=(
            "角色多情境身份侧写(v1.1 wave 6 session 2 / A3)。**只有双界 / 卧底 / "
            "易容 / 双重生活等'同一角色在不同情境是不同身份'的角色才填**;普通角色"
            "留空 []。每条 = {context 情境, identity 简短身份名词}。commit 时透传到"
            "``Character.identity_facets`` 落库,供 writer 注入 + 覆盖自检 + 职业黑名单。"
        ),
    )
    initial_core_facts: list[InitialCharacterCoreFact] = Field(
        default_factory=list,
        max_length=2,
        description=(
            "正文开始前已成立的角色核心事实，最多 2 条；没有则显式输出 []。"
            "每条必须逐字绑定 archetype/background/identity_facets 之一，不能写"
            "未来章节才会发生的变化。"
        ),
    )
    relationships_brief: dict[str, str] = Field(
        default_factory=dict,
        description="开篇时与其他 character_id 已成立的关系、共同前史和当前压力；后续互动留在主线(空时用 {})",
    )

    @field_validator("initial_core_facts", mode="before")
    @classmethod
    def _normalize_legacy_null_initial_core_facts(cls, value: Any) -> Any:
        """旧 characters.json 曾把“无初始事实”持久化为显式 null。"""

        return [] if value is None else value

    @model_validator(mode="after")
    def _validate_initial_core_fact_sources(self) -> CharacterOutlineItem:
        source_texts = {
            "archetype": self.archetype,
            "background": self.background,
            "identity_facets": "\n".join(
                f"{facet.context}:{facet.identity}" for facet in self.identity_facets
            ),
        }
        seen_attributes: set[str] = set()
        for fact in self.initial_core_facts:
            if fact.attribute in seen_attributes:
                raise ValueError(
                    "initial_core_facts 同一 attribute 只能建立一条初始事实"
                )
            seen_attributes.add(fact.attribute)
            if fact.evidence_quote not in source_texts[fact.source_field]:
                raise ValueError(
                    "initial_core_fact.evidence_quote 必须逐字出现在声明的稳定角色字段"
                )
        return self


class CharactersOutput(BaseModel):
    """OutlineBuilder 第 2 段完整产出。

    ``characters`` 长度 4-8(对齐 prompt 字面);character_id 数组内唯一。
    """

    characters: list[CharacterOutlineItem] = Field(
        ..., min_length=4, max_length=8, description="核心人设 4-8 个"
    )

    @field_validator("characters")
    @classmethod
    def _validate_unique_character_ids(
        cls, items: list[CharacterOutlineItem]
    ) -> list[CharacterOutlineItem]:
        seen: set[str] = set()
        for item in items:
            if item.character_id in seen:
                raise ValueError(
                    f"Duplicate character_id within characters: {item.character_id}"
                )
            seen.add(item.character_id)
        total_initial_facts = sum(len(item.initial_core_facts) for item in items)
        if total_initial_facts > MAX_INITIAL_CHARACTER_CORE_FACTS:
            raise ValueError("全书 initial_core_facts 最多 8 条")
        return items


# ---------------------------------------------------------------------------
# Stage 3: Plot
# ---------------------------------------------------------------------------


class PlotPhase(BaseModel):
    """plot.main_phases 数组单元。"""

    phase_id: str = Field(..., pattern=ID_PATTERN, description="阶段稳定 ID")
    title: str = Field(..., min_length=1, description="阶段标题")
    summary: str = Field(..., min_length=1, description="阶段概要")
    development: PhaseDevelopment | None = Field(
        None, exclude_if=lambda value: value is None,
        description="阶段经历与篇幅参考；旧大纲缺省，不属于正文验收合同",
    )


def _normalize_summary_to_description(data: Any) -> Any:
    """v1.0.x wave 5 session 4:把 input dict 的 ``summary`` 键改名 ``description``。

    解决 wave 5 session 2 的 ``alias="summary"`` 接力遗漏 — Pydantic alias /
    validation_alias 都会让 OpenAPI Input schema 字段名变 ``summary``,与立法
    精神"output 始终用 description / 前端零字面回归"冲突。改用 model_validator
    mode="before" 在 input 阶段手动把 summary → description normalize,这样
    Pydantic field 名 ``description`` 不被 alias 替换,OpenAPI Input/Output
    schema 字段都是 ``description``,前端 types 字段名一致零漂移。
    """
    if isinstance(data, dict) and "summary" in data and "description" not in data:
        new_data = dict(data)
        new_data["description"] = new_data.pop("summary")
        return new_data
    return data


class PlotTurningPoint(BaseModel):
    """plot.major_turning_points 数组单元。

    v1.0.x wave 5 session 2 schema 容错(LLM 双名兼容):``description`` 字段
    接受 ``summary`` 别名 input。LLM(尤其 qwen-novel)易把 "description"
    字段名写成 "summary";input 双名兼容,output 始终用 ``description``。

    **v1.0.x wave 5 session 4 接力修复**:wave 5 session 2 用 ``alias="summary"``
    虽实现 input 双名兼容,但 Pydantic v2 让 OpenAPI schema 字段名变 alias
    名(``summary``),与立法精神"output 始终 description"冲突 → 前端 types
    regen 后字段名漂移。改用 ``@model_validator(mode="before")`` 在 input
    阶段把 summary → description normalize,OpenAPI Input/Output schema 字
    段都是 ``description``(零前端字面回归 + 保留 LLM input 双名兼容)。
    """

    model_config = ConfigDict(populate_by_name=True)

    turning_id: str = Field(..., pattern=ID_PATTERN, description="转折稳定 ID")
    title: str = Field(..., min_length=1, description="转折标题")
    description: str = Field(
        ..., min_length=1, description="转折描述(LLM 可写 summary,统一收 description)"
    )

    @model_validator(mode="before")
    @classmethod
    def _accept_summary_alias(cls, data: Any) -> Any:
        return _normalize_summary_to_description(data)


class PlotLongThread(BaseModel):
    """plot.long_threads 全书级伏笔种子(与 PlotThread SQLite 模型解耦)。

    v1.0.x wave 5 session 2 schema 容错(同 PlotTurningPoint),wave 5
    session 4 接力修复(详 PlotTurningPoint docstring)。
    """

    model_config = ConfigDict(populate_by_name=True)

    thread_id: str = Field(..., pattern=ID_PATTERN, description="伏笔稳定 ID")
    title: str = Field(..., min_length=1, description="伏笔标题")
    description: str = Field(
        ...,
        min_length=1,
        description="伏笔描述 + 埋 / 收意图(LLM 可写 summary,统一收 description)",
    )

    @model_validator(mode="before")
    @classmethod
    def _accept_summary_alias(cls, data: Any) -> Any:
        return _normalize_summary_to_description(data)


class PlotOutput(BaseModel):
    """OutlineBuilder 第 3 段完整产出。

    数组数量约束对齐 ``prompts/outline_builder/plot.md`` 字面:
    main_phases 4-7、major_turning_points 3-6、long_threads 3-6。
    """

    core_conflict: str = Field(
        ..., min_length=1, description="一句话讲清根本对抗"
    )
    main_phases: list[PlotPhase] = Field(
        ..., min_length=4, max_length=7, description="主线阶段 4-7 个"
    )
    major_turning_points: list[PlotTurningPoint] = Field(
        ..., min_length=3, max_length=6, description="关键转折 3-6 个"
    )
    long_threads: list[PlotLongThread] = Field(
        ..., min_length=3, max_length=6, description="长伏笔种子 3-6 条"
    )
    character_arcs: dict[str, str] = Field(
        ...,
        description="character_id → 角色弧光描述(覆盖 protagonist + antagonist)",
    )
    load_bearing_objects: list[LoadBearingObjectContract] = Field(
        default_factory=list,
        max_length=12,
        description=(
            "真正决定 turning/beat/finale 可行性的承重物件目录；"
            "按因果作用选择，不按出现频率；旧 plot 缺字段时兼容为 []。"
        ),
    )

    @field_validator("main_phases")
    @classmethod
    def _validate_unique_phase_ids(cls, items: list[PlotPhase]) -> list[PlotPhase]:
        seen: set[str] = set()
        for item in items:
            if item.phase_id in seen:
                raise ValueError(f"Duplicate phase_id within main_phases: {item.phase_id}")
            seen.add(item.phase_id)
        return items

    @field_validator("major_turning_points")
    @classmethod
    def _validate_unique_turning_ids(
        cls, items: list[PlotTurningPoint]
    ) -> list[PlotTurningPoint]:
        seen: set[str] = set()
        for item in items:
            if item.turning_id in seen:
                raise ValueError(
                    f"Duplicate turning_id within major_turning_points: {item.turning_id}"
                )
            seen.add(item.turning_id)
        return items

    @field_validator("long_threads")
    @classmethod
    def _validate_unique_thread_ids(
        cls, items: list[PlotLongThread]
    ) -> list[PlotLongThread]:
        seen: set[str] = set()
        for item in items:
            if item.thread_id in seen:
                raise ValueError(
                    f"Duplicate thread_id within long_threads: {item.thread_id}"
                )
            seen.add(item.thread_id)
        return items

    @field_validator("load_bearing_objects")
    @classmethod
    def _validate_unique_load_bearing_object_ids(
        cls, items: list[LoadBearingObjectContract]
    ) -> list[LoadBearingObjectContract]:
        seen: set[str] = set()
        for item in items:
            if item.object_id in seen:
                raise ValueError(
                    "Duplicate object_id within load_bearing_objects: "
                    f"{item.object_id}"
                )
            seen.add(item.object_id)
        return items

    @model_validator(mode="after")
    def _validate_load_bearing_object_plot_links(self) -> PlotOutput:
        phase_ids = {item.phase_id for item in self.main_phases}
        turning_ids = {item.turning_id for item in self.major_turning_points}
        thread_ids = {item.thread_id for item in self.long_threads}
        reserved_ids = phase_ids | turning_ids | thread_ids
        for item in self.load_bearing_objects:
            if item.object_id in reserved_ids:
                raise ValueError(
                    "load_bearing_object.object_id 与 plot 稳定 ID 冲突:"
                    f"{item.object_id}"
                )
            if (
                item.linked_turning_id is not None
                and item.linked_turning_id not in turning_ids
            ):
                raise ValueError(
                    "load_bearing_object 引用未知 linked_turning_id:"
                    f"{item.object_id}/{item.linked_turning_id}"
                )
            if (
                item.linked_thread_id is not None
                and item.linked_thread_id not in thread_ids
            ):
                raise ValueError(
                    "load_bearing_object 引用未知 linked_thread_id:"
                    f"{item.object_id}/{item.linked_thread_id}"
                )
        return self


# ---------------------------------------------------------------------------
# Stage 4: VolumePlans
# ---------------------------------------------------------------------------


ArcPhase = Literal["opening", "rising", "climax", "falling", "resolution"]
"""卷内弧阶段枚举(对齐 PROJECT_SPEC §3.2 ChapterCard.arc_phase 5 档)。"""


class VolumeBeatPlan(BaseModel):
    """卷规划阶段单个 Beat(v1.1 G3 治本核心)。

    与 ``src/core/models/volume.Beat`` 形态接近但**不复用**:Outline 阶段是
    LLM 自由产出的中间产物,DB ORM 阶段在 P5-B-04 commit 时映射到
    ``volumes.arcs_json.beats`` 嵌套字段。

    **字段对应**(对齐 [prompts/outline_builder/volume_plans.md](../../../prompts/outline_builder/volume_plans.md) §"约束 7"):

    - ``beat_id``:beat 稳定 ID(``ID_PATTERN`` 字符集)
    - ``chapter_range``:覆盖章节闭区间 (start, end);**必须落在所属
      ``VolumeArcPlan.chapter_range`` 内**(由 ``VolumePlanItem._validate_beats_within_arcs``
      model_validator 跨字段校验)
    - ``summary``:50-100 字描述本 beat 节奏单元
    - ``must_advance``:list[str],2-4 条本 beat 必须推进的事件(LLM-as-judge
      ``beat_alignment_validator`` 用作 yes/no 询问素材)
    - ``forbidden_advance``:list[str],1-3 条本 beat 严禁触发的事件(典型:
      后续 arc / beat 的核心剧情字面;LLM-as-judge 字面比对命中即抛 ValueError)
    """

    planning_milestones: list[str] = Field(default_factory=list, exclude_if=lambda value: not value,
        description="仅选择式规划使用：本段实际完成的规划节点ID，不是正文词表")
    beat_id: str = Field(..., pattern=ID_PATTERN, description="beat 稳定 ID")
    chapter_range: tuple[int, int] = Field(
        ..., description="覆盖章节闭区间 (start, end);必须落在所属 arc 内"
    )
    summary: str = Field(
        ..., min_length=1, description="beat 推进范围(给 LLM-as-judge 看的 ground truth)"
    )
    must_advance: list[str] = Field(
        default_factory=list,
        description="本 beat 必须推进的事件 list(自然语言;喂 LLM-as-judge yes/no 询问)",
    )
    forbidden_advance: list[str] = Field(
        default_factory=list,
        description=(
            "本 beat 严禁触发的事件 list(典型:后续 arc 核心剧情字面;命中即"
            "抛 ValueError → retry-with-feedback)"
        ),
    )
    dramatic_contract: DramaticBeatContract | None = Field(
        default=None,
        description=(
            "仅转折、对抗、情感峰与 finale 等高价值 beat 使用的 typed 戏剧合同；"
            "普通推进、独处与留白 beat 为 null。"
        ),
    )

    @model_validator(mode="after")
    def _validate_range_monotonic(self) -> VolumeBeatPlan:
        start, end = self.chapter_range
        if start < 1:
            raise ValueError(
                f"VolumeBeatPlan.chapter_range start must be >= 1, got {start}"
            )
        if end < start:
            raise ValueError(
                f"VolumeBeatPlan.chapter_range end ({end}) must be >= start ({start})"
            )
        return self


class VolumeArcPlan(BaseModel):
    """卷规划阶段单个 Arc。

    与 ``src/core/models/volume.Arc`` 形态接近但**不复用**:Outline 阶段
    ``arc_type`` 是 LLM 自由文本,DB ORM 阶段在 P5-B-04 commit 时映射。

    **v1.1 wave 1 session 1 扩展**(G3 beat 拆分路径):新增 ``beats: list[VolumeBeatPlan]``
    字段,LLM 自由拆 2-4 beats/arc(粒度引导在 volume_plans prompt v6 字面)。
    空 list 走 arc-level fallback 路径(向后兼容旧 outline 数据)。
    """

    arc_id: str = Field(..., pattern=ID_PATTERN, description="弧稳定 ID")
    arc_type: str = Field(
        ...,
        min_length=1,
        description="弧类型自由文本(常用'主线' / '副本' / '支线' / '番外')",
    )
    chapter_range: tuple[int, int] = Field(
        ..., description="覆盖章节闭区间 (start, end);start >= 1, end >= start"
    )
    phase: ArcPhase = Field(..., description="弧在卷中的阶段(5 档枚举)")
    summary: str = Field(..., min_length=1, description="弧概要")
    beats: list[VolumeBeatPlan] = Field(
        default_factory=list,
        description=(
            "v1.1 G3 beat 拆分路径(Q7=B):弧内 beat 列表(LLM 自由拆 2-4 beats/arc;"
            "空 list 走 arc-level fallback 兼容旧数据)"
        ),
    )

    @model_validator(mode="after")
    def _validate_range_monotonic(self) -> VolumeArcPlan:
        start, end = self.chapter_range
        if start < 1:
            raise ValueError(
                f"VolumeArcPlan.chapter_range start must be >= 1, got {start}"
            )
        if end < start:
            raise ValueError(
                f"VolumeArcPlan.chapter_range end ({end}) must be >= start ({start})"
            )
        return self

    @model_validator(mode="after")
    def _validate_unique_beat_ids(self) -> VolumeArcPlan:
        seen: set[str] = set()
        for beat in self.beats:
            if beat.beat_id in seen:
                raise ValueError(
                    f"Duplicate VolumeBeatPlan.beat_id within arc: {beat.beat_id}"
                )
            seen.add(beat.beat_id)
        return self

    @model_validator(mode="after")
    def _validate_beats_within_arc_range(self) -> VolumeArcPlan:
        """每个 Beat.chapter_range 必须落在 Arc.chapter_range 内。"""
        arc_start, arc_end = self.chapter_range
        for beat in self.beats:
            b_start, b_end = beat.chapter_range
            if b_start < arc_start or b_end > arc_end:
                raise ValueError(
                    f"VolumeBeatPlan {beat.beat_id} chapter_range ({b_start}, {b_end}) "
                    f"must be within VolumeArcPlan.chapter_range ({arc_start}, {arc_end})"
                )
        return self

    @model_validator(mode="after")
    def _validate_beats_no_overlap(self) -> VolumeArcPlan:
        """同 arc 内 beats chapter_range 不能重叠(允许相邻 — 上 end+1 == 下 start)。"""
        sorted_beats = sorted(self.beats, key=lambda b: b.chapter_range[0])
        for prev, curr in zip(sorted_beats, sorted_beats[1:], strict=False):
            prev_end = prev.chapter_range[1]
            curr_start = curr.chapter_range[0]
            if curr_start <= prev_end:
                raise ValueError(
                    f"VolumeBeatPlans {prev.beat_id} (ends ch-{prev_end}) and "
                    f"{curr.beat_id} (starts ch-{curr_start}) overlap"
                )
        return self


class VolumePlanItem(BaseModel):
    """单个 Volume 的 outline 阶段计划(尚未入 DB)。

    与 ``src/core/models/volume.Volume`` 不复用:
    - 无 ``workspace_id`` / ``created_at`` / ``last_updated_at``(commit 时注入)
    - 含跨段引用 ``linked_phases`` / ``linked_turning_points`` / ``linked_threads``
    - ``tension_curve`` 是 ``list[int]`` 而非 ``list[TensionCurvePoint]``
      (LLM 输出 1-10 整数序列,P5-B-09 ``volumes:apply-template`` 端点
      负责 ``int`` → ``TensionCurvePoint`` 转换 + 注入 ``is_arc_finale``)
    """

    volume_id: str = Field(..., pattern=ID_PATTERN, description="卷稳定 ID")
    title: str = Field(..., min_length=1, description="卷标题")
    summary: str = Field(..., min_length=1, description="本卷在主线中的位置")
    chapter_count: int = Field(..., ge=1, description="卷内章节总数")
    tension_template: TensionTemplateName = Field(
        ..., description="张力曲线模板(4 档)"
    )
    linked_phases: list[str] = Field(
        default_factory=list,
        description="关联 plot.main_phases 的 phase_id(自由文本)",
    )
    linked_turning_points: list[str] = Field(
        default_factory=list,
        description="关联 plot.major_turning_points 的 turning_id",
    )
    linked_threads: list[str] = Field(
        default_factory=list,
        description="关联 plot.long_threads 的 thread_id",
    )
    dramatic_promise: VolumeDramaticPromise | None = Field(
        default=None,
        description=(
            "本卷唯一主戏剧承诺；新生成的悬疑卷必须显式给出，legacy 与不要求尖锐"
            "对抗的题材允许 null。"
        ),
    )
    arcs: list[VolumeArcPlan] = Field(
        ..., min_length=2, max_length=4, description="卷内弧 2-4 个"
    )
    tension_curve: list[int] = Field(
        ...,
        description="每章张力 1-10 整数序列,长度 == chapter_count",
    )

    @model_validator(mode="before")
    @classmethod
    def _clamp_and_pad_tension_curve(cls, data: Any) -> Any:
        """v1.0.x wave 5 session 2 schema 容错:tension_curve clamp + pad/trim。

        luo3 实测 qwen-novel 高频出错:① 元素越界(写 11/12/13 超 schema [1,10]);
        ② 长度数错(29 vs chapter_count 30 / 30 vs 29 等差 1-2 的轻微偏差)。
        本 before validator 在 Pydantic schema 校验前先做容错修正:

        - **越界 clamp**:每个元素钳制到 [1, 10],<1 → 1,>10 → 10
        - **长度 pad/trim**:若 tension_curve 长度 != chapter_count
          - 长度 > chapter_count:从末尾截断
          - 长度 < chapter_count:用 5(中段张力)补齐到 chapter_count

        修正后既有 ``_validate_tension_range`` field_validator(已弃用,逻辑
        并入此 before validator)+ ``_validate_tension_curve_length`` model_validator
        都能通过。**非破坏性**:已合规的输入完全不动。
        """
        if not isinstance(data, dict):
            return data
        tension_curve = data.get("tension_curve")
        chapter_count = data.get("chapter_count")
        if not isinstance(tension_curve, list):
            return data
        # clamp 越界
        clamped = [
            max(1, min(10, int(v))) if isinstance(v, (int, float)) else v
            for v in tension_curve
        ]
        # pad/trim 长度
        if isinstance(chapter_count, int) and chapter_count >= 1:
            if len(clamped) > chapter_count:
                clamped = clamped[:chapter_count]
            elif len(clamped) < chapter_count:
                clamped = clamped + [5] * (chapter_count - len(clamped))
        data["tension_curve"] = clamped
        return data

    @field_validator("arcs")
    @classmethod
    def _validate_unique_arc_ids(
        cls, items: list[VolumeArcPlan]
    ) -> list[VolumeArcPlan]:
        seen: set[str] = set()
        for arc in items:
            if arc.arc_id in seen:
                raise ValueError(
                    f"Duplicate arc_id within volume: {arc.arc_id}"
                )
            seen.add(arc.arc_id)
        return items

    @field_validator("tension_curve")
    @classmethod
    def _validate_tension_range(cls, values: list[int]) -> list[int]:
        """v1.0.x wave 5 session 2 起,clamp 已由 ``_clamp_and_pad_tension_curve``
        before validator 完成;此 field_validator 保留作"最终防线"——若 before
        validator 因输入非 dict 等异常路径未触发,此处仍兜底 raise。
        """
        for v in values:
            if not (1 <= v <= 10):
                raise ValueError(
                    f"tension_curve element must be in [1, 10], got {v}"
                )
        return values

    @model_validator(mode="after")
    def _validate_tension_curve_length(self) -> VolumePlanItem:
        if len(self.tension_curve) != self.chapter_count:
            raise ValueError(
                f"VolumePlanItem.tension_curve length ({len(self.tension_curve)}) "
                f"must equal chapter_count ({self.chapter_count})"
            )
        return self

    @model_validator(mode="after")
    def _validate_arcs_cover_chapter_count(self) -> VolumePlanItem:
        """每个 Arc 的 chapter_range 必须落在 [1, chapter_count] 内;
        首 arc start == 1;末 arc end == chapter_count;arc 间连续不重叠不间断。

        [HARD] 副本横跨规则(arc_type 含'副本' + end-start+1 < 3 时拒绝)留
        P5-B-04 ``src/core/rules/outline_validators.py`` 集中校验,本模型
        层不前置(避免阻塞 OutlineBuilder 单段 regenerate 路径)。
        """
        if not self.arcs:
            return self

        sorted_arcs = sorted(self.arcs, key=lambda a: a.chapter_range[0])
        first_start = sorted_arcs[0].chapter_range[0]
        last_end = sorted_arcs[-1].chapter_range[1]

        if first_start != 1:
            raise ValueError(
                f"First arc chapter_range start must be 1, got {first_start}"
            )
        if last_end != self.chapter_count:
            raise ValueError(
                f"Last arc chapter_range end ({last_end}) must equal "
                f"chapter_count ({self.chapter_count})"
            )

        prev_end = 0
        for arc in sorted_arcs:
            start, end = arc.chapter_range
            if end > self.chapter_count:
                raise ValueError(
                    f"Arc {arc.arc_id} chapter_range end ({end}) exceeds "
                    f"chapter_count ({self.chapter_count})"
                )
            if start != prev_end + 1:
                raise ValueError(
                    f"Arc {arc.arc_id} chapter_range start ({start}) must be "
                    f"prev_end + 1 ({prev_end + 1}); arcs must be contiguous "
                    f"without gap or overlap"
                )
            prev_end = end
        return self


class VolumePlansOutput(BaseModel):
    """OutlineBuilder 第 4 段完整产出。

    ``volumes`` 长度 == ``total_volumes``(由 caller 用户传入,本模型不绑
    硬约束,在 OutlineBuilder agent 层 + outline:commit service 层校验)。
    volume_id 数组内唯一。
    """

    volumes: list[VolumePlanItem] = Field(
        ..., min_length=1, description="卷规划列表"
    )

    @field_validator("volumes")
    @classmethod
    def _validate_unique_volume_ids(
        cls, items: list[VolumePlanItem]
    ) -> list[VolumePlanItem]:
        seen: set[str] = set()
        for vol in items:
            if vol.volume_id in seen:
                raise ValueError(
                    f"Duplicate volume_id within volumes: {vol.volume_id}"
                )
            seen.add(vol.volume_id)
        return items


# ---------------------------------------------------------------------------
# Stage 5: MiniPlan(v1.0.x wave 4 引入 — 章级事件分配)
# ---------------------------------------------------------------------------


class MiniPlanItem(BaseModel):
    """单章的 mini_plan 事件分配(v1.0.x wave 4 session 1 引入)。

    每章 1 个 item,产 LLM 决策的"本章承担什么具体事件"。下游
    ChapterCardGenerator(会话 2)在 derive_context 时读对应章的
    MiniPlanItem.one_line_event 作为章级 arc summary 注入 prompt,避免
    LLM 反复从粗粒度 arc summary 抽同一组动作。

    设计要点:
    - ``chapter_id`` / ``volume_id`` / ``arc_id`` 跨段引用 outline 已生成 ID
    - ``one_line_event`` 30-60 字一句话(prompt 强约束,模型层不硬卡 max_length)
    - ``linked_characters`` / ``linked_threads`` 可空(过渡章 / 内心独白章合理空)
    - ``chapter_number`` 卷内 1-based,与 ``ChapterCard.chapter_number`` 同义
    """

    chapter_id: str = Field(
        ..., pattern=ID_PATTERN, description="章节稳定 ID(ch-XXXX)"
    )
    chapter_number: int = Field(
        ..., ge=1, description="卷内 1-based 章序号"
    )
    volume_id: str = Field(
        ..., pattern=ID_PATTERN, description="所属卷 ID"
    )
    arc_id: str = Field(
        ..., pattern=ID_PATTERN, description="所属 arc ID"
    )
    one_line_event: str = Field(
        ...,
        min_length=1,
        description="本章承担的事件(30-60 字一句话,含主语 + 动词 + 宾语结构)",
    )
    narrative_context: NarrativeContext | None = Field(
        None, exclude_if=lambda value: value is None,
        description="本章读者交代安排及同源故事定位；不作为文学 HARD。",
    )
    info_increment: str = Field(
        default="",
        description=(
            "本章相对前文的**新增信息增量**(v1.1 wave 6 session 2 / A2 引入,治 B1 "
            "暴露的战斗同质化 / 主线停滞 / 注水)。一句话点明本章带来的**新冲突 / "
            "新揭示 / 新动机 / 关系实质变化**中至少一类的具体增量——不是 one_line_event "
            "的复述,而是回答'这一章让读者多知道 / 多发生了什么前面没有的'。战斗 / 对抗章"
            "须写出本场特有的差异化战术 / 筹码 / 代价(不得是上一场的重复)。**WARN-only "
            "校验**(beat_alignment_validator info_increment_substantive 维度):空 / 空泛 / "
            "纯复读只 log warn 不阻断(护 §0 留白第一原则,过渡 / 内心戏章可以是轻增量)。"
            "default='' 保后向兼容(旧 outline 数据无此字段)。"
        ),
    )
    linked_characters: list[str] = Field(
        default_factory=list,
        description="本章涉及的核心角色 ID(可空 — 过渡章 / 独白章)",
    )
    linked_threads: list[str] = Field(
        default_factory=list,
        description="本章推进的伏笔 thread_id(可空)",
    )
    callback_anchors: list[str] = Field(
        default_factory=list,
        description=(
            "本章呼应(回响)的伏笔 / 隐喻 thread_id(v1.1 wave 4 session 2 / "
            "G2 引入,可空)。语义区别于 ``linked_threads``:后者是'本章推进'"
            "的伏笔,本字段是'本章回响早先埋下'的伏笔 / 隐喻。OutlineBuilder "
            "在 mini_plan 阶段就规划回响结构;下游 chapter_card_service 把本字段"
            "确定性派生为 ``ChapterCard.callback_to``,Writer 据此绕 embedding "
            "精确 lookup。必须命中 outline ``plot.long_threads`` 内 thread_id"
            "(与 linked_threads 同校验,见 ``_validate_mini_plan_volume_refs``)。"
        ),
    )
    chapter_focus: str = Field(
        default="",
        description=(
            "本章**结构焦点**(v1.1 wave 5 session 6 / M4''' 引入,治'章节结构不跟"
            "戏剧功能走'真根因)。一句话点明**本章的注意力 / 分量该投向哪、采用什么"
            "结构形态**——但**只在偏离题材默认结构时填**:情感峰(角色死亡 / 重大牺牲)"
            "→ '情感单焦'(分量集中在读者的情感锚点,另一线退为简短回响,别对半稀释)/ "
            "角色重大转折(信仰崩塌 / 黑化等 character_arc 蜕变)→ '角色单焦'(围绕该角色"
            "做对手戏 / 内心戏)/ 反派前史揭示 → 'backstory 单焦' / 困局升级 → 纯单线"
            "制造压迫。**默认结构(题材的核心钩子,如双界互证查案 / 单线推进)留空 ''**。"
            "**Q1 铁律**:内容驱动**非配额** —— 绝大多数章保持默认(留空),只在戏剧功能"
            "真需要时偏离;为了'多样'硬套 = 减分。**护留白**:default='' 向后兼容(旧 "
            "outline 无此字段 / 未偏离 → 空)。下游 ``chapter_card_service`` 读出 → "
            "``ChapterCard.chapter_focus`` **确定性透传**(非 card LLM 重产,避免变异)"
            "→ Writer 据此知道'本章别按默认结构稀释'。**题材感知**:偏离的加分 / 红线"
            "随 genre 不同(由 mini_plan prompt 注入,如悬疑题材 POV 克制防泄底)。"
        ),
    )
    arc_beats: dict[str, str] = Field(
        default_factory=dict,
        description=(
            "本章**逐章角色弧光台阶**(v1.1 wave 5 session 7 / M5 引入,治 B1 读者实测"
            "'人物弧光近乎为零 / 配角零成长 / 弧光全靠旁白声明')。key=``character_id``"
            "(命中 outline characters 清单),value=**自包含的一句话**点明该角色本章"
            "**从什么态 → 什么态 + 通过什么具体动作 / 抉择 / 代价演出**。**命门:动作演出"
            "非旁白声明** —— value 必须写'通过他做了 X'(✅'林疏影首次为查案默许伪造一条"
            "线索,亲手按下证物删除键、删完盯着屏幕没说话'),**绝不写'他开始动摇'式旁白"
            "声明(❌ 这正是读者病根:沈舟'理性防线崩塌'旁白复读 ≥5 次)**。**Q1 铁律:"
            "内容驱动非配额** —— 绝大多数章 ``arc_beats`` 留空 ``{}``;只在某角色弧光真"
            "推进一个可见台阶时标注(全书预期个位数关键台阶 + 若干轻台阶,如配角与主角的"
            "信任 / 情感积累)。为'每章都有成长'硬塞 = 注水减分。**护留白**:default={} "
            "向后兼容(旧 outline 无此字段 / 本章无弧光推进 → 空)。下游 "
            "``chapter_card_service`` 读出 → ``ChapterCard.arc_beats`` **确定性透传**"
            "(非 card LLM 重产,避免变异)→ Writer 据此用**动作**演出弧光台阶。与 G1 "
            "累积状态互补:G1 = 地板(已确立状态别倒退),arc_beats = 本章该迈的那一步(向前)。"
        ),
    )
    interaction_focus: str = Field(
        default="",
        description=(
            "本章**互动场景中心**(v1.1 wave 5 session 8 / M4''-轴A 引入,治**松 beat 题材"
            "〔治愈日常 / 慢生活类〕'独处内省扎堆'**:戏剧引擎默认让主角独自做事 / 感慨,"
            "即便配角在场也被写成主角内心戏的背景板)。一句话点明**本章的戏剧核心是主角"
            "与谁的一场真实互动**,且**那个配角要被写成有主体性的真人**(自己的声音 / 动作 / "
            "目的 / 与主角的温和摩擦),情感**从这场互动里长出来**——而非主角独白 + 配角当"
            "催化符号。**只在本章戏剧核心是人物互动时填**(尤其是河边偶遇 / 共做一事 / "
            "温和分歧这类**容易被写成'主角观察+独白'的安静互动**),典型:'本章核心是林晚"
            "与陈阿婆一起琢磨无字日记并产生方法分歧 —— 写成两人你来我往的对手戏,陈阿婆有"
            "自己的判断和坚持,别写成林晚独自烧本子'。**默认留空 ''**:真正该独处的章(独自"
            "顿悟 / panic 发作 / 安静转折留白)、或互动已是高戏剧自带场景的章(如他人闯入"
            "撞翻书架),都留空。**Q1 铁律**:内容驱动**非配额** —— 不是'每章必须有互动';"
            "**护留白**:治愈日常需要安静独处的留白,该独处的章保留,治的是'**默认独处 + "
            "重复同一内省母题**'非消灭独处;为'凑互动'把留白章硬塞 = 减分。**护留白**:"
            "default='' 向后兼容(旧 outline 无此字段 / 非互动中心章 → 空)。下游 "
            "``chapter_card_service`` 读出 → ``ChapterCard.interaction_focus`` **确定性"
            "透传**(非 card LLM 重产,避免变异)→ Writer 据此把配角写成真人、让情感从互动"
            "演出。**题材感知**:本字段主要服务松 beat 题材(由 mini_plan prompt 题材 "
            "profile 注入,紧 beat 题材如悬疑默认不触发)。与 chapter_focus(整体结构形态)"
            "/ arc_beats(谁演哪步弧光)/ craft_focus(哪几处深挖内心 / 画面)正交互补。"
        ),
    )
    dramatic_contract: DramaticBeatContract | None = Field(
        default=None,
        description=(
            "本章承担所属高价值 beat 时逐字段复制的 typed 戏剧合同；普通独处、"
            "治愈与留白章为 null。"
        ),
    )
    breakthrough_payoff: str = Field(
        default="",
        description=(
            "本章**升级可感化要点**(v1.1 wave 5 session 10 / 模块① 引入,治**升级流'突破"
            "爽点失约'**:grounding 实证当前引擎把境界突破写成'经脉撕裂+痛苦氛围+一个境界"
            "名词',无可感 power-delta——不交代起点跨度、不展示突破后能做到之前做不到的事)。"
            "**仅在本章是境界突破/晋级章时填**:一句话点明**突破到什么 + 要落地的可感化点**"
            "(典型:'突破炼气巅峰 —— 要展示新能力:灵识外放感知翻倍 / 当场碾压之前险胜的"
            "铁甲犀级对手,让读者感到实力跃迁,别只丢境界名词')。**起点境界由模块②注入"
            "writer,本字段不必重复**,只标终点 + 可感化落点。**默认留空 ''**:非突破章、紧"
            "门控题材(非 3 题材之一)、或工作区开关 OFF → 空。**护内容驱动非配额**:不是"
            "每章硬塞突破,只在真有境界跃迁的章填。下游 ``chapter_card_service`` 读出 → "
            "``ChapterCard.breakthrough_payoff`` **确定性透传**(非 card LLM 重产)→ Writer "
            "``_format_breakthrough_payoff`` 注入'升级可感化'指令。与 chapter_focus / "
            "arc_beats / interaction_focus / craft_focus 正交;与模块②(境界一致性,防倒退)"
            "互补:②管'地板别倒退 + 起点',①管'本章这步跃迁写得爽'。**题材门控**:仅 3 "
            "题材(古典仙侠/搞笑爽文/热血战斗)+ 开关 ON 时 mini_plan 才填。"
        ),
    )


class MiniPlanOutput(BaseModel):
    """OutlineBuilder 第 5 段完整产出 — 全书章级事件分配。

    ``items`` 长度 == sum(chapter_count for v in volumes);全章 chapter_id
    唯一,按 (volume_id, chapter_number) 排序。caller(``outline_service``)
    在 commit 时校验 items 覆盖全书章号无缺漏 — 模型层只做 chapter_id 唯一
    + 排序校验(避免与 caller 的 total_chapters 输入耦合)。
    """

    items: list[MiniPlanItem] = Field(
        ..., min_length=1, description="全书章级事件分配列表"
    )

    @field_validator("items")
    @classmethod
    def _validate_unique_chapter_ids(
        cls, items: list[MiniPlanItem]
    ) -> list[MiniPlanItem]:
        seen: set[str] = set()
        for item in items:
            if item.chapter_id in seen:
                raise ValueError(
                    f"Duplicate chapter_id within mini_plan items: {item.chapter_id}"
                )
            seen.add(item.chapter_id)
        return items
