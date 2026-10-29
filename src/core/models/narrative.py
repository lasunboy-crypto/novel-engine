"""叙事交接材料：参考与表达空间不升级为剧情 HARD。"""

from __future__ import annotations

import hashlib
import json
from typing import Literal

from pydantic import BaseModel, ConfigDict, Field, model_validator

from src.core.models import ID_PATTERN
from src.core.models.authoring_contract import AuthoringContract


class UserNarrativeRequest(BaseModel):
    """程序保存的用户原文；哈希只验传递完整性，不判断文学语义。"""

    model_config = ConfigDict(extra="forbid", frozen=True)

    version: Literal[1, 2] = 1
    premise: str = Field(min_length=1)
    genre: str = ""
    reading_preferences: str | None = Field(None, exclude_if=lambda value: value is None)
    source_sha256: str = Field(pattern=r"^[a-f0-9]{64}$")

    @staticmethod
    def digest(premise: str, genre: str, reading_preferences: str | None = None) -> str:
        source = {"premise": premise, "genre": genre}
        if reading_preferences is not None:
            source["reading_preferences"] = reading_preferences
        material = json.dumps(source,
                              ensure_ascii=False, sort_keys=True, separators=(",", ":"))
        return hashlib.sha256(material.encode()).hexdigest()

    @classmethod
    def from_source(cls, *, premise: str, genre: str, reading_preferences: str | None = None) -> UserNarrativeRequest:
        return cls(version=2 if reading_preferences is not None else 1, premise=premise, genre=genre,
                   reading_preferences=reading_preferences,
                   source_sha256=cls.digest(premise, genre, reading_preferences))

    @model_validator(mode="after")
    def intact_source(self) -> UserNarrativeRequest:
        if (self.version == 2) != (self.reading_preferences is not None):
            raise ValueError("阅读方向 v2 必须保留原始偏好；v1 不得静默加入")
        if self.source_sha256 != self.digest(self.premise, self.genre, self.reading_preferences):
            raise ValueError("用户原始要求来源不符，不能以模型归纳替代")
        return self


PlanningMode = Literal["whole_book", "opening_segment"]


class PlanningScope(BaseModel):
    """程序保存的规划尺度；参考篇幅不形成正文下限或完成配额。"""

    model_config = ConfigDict(extra="forbid", frozen=True)
    mode: PlanningMode
    total_volumes: int = Field(ge=1, le=20)
    chapters_per_volume: int = Field(ge=1, le=200)
    reference_chapter_words: int = Field(gt=0)

    def reading_reference(self) -> dict[str, int]:
        chapters = self.total_volumes * self.chapters_per_volume
        return {"planned_chapters": chapters,
                "reference_total_words": chapters * self.reference_chapter_words}


class PhaseDevelopment(BaseModel):
    """主线阶段的生成设计；只指导规划，不升级为章级必须事件。"""

    model_config = ConfigDict(extra="forbid", frozen=True, str_strip_whitespace=True)
    experience: str = Field(min_length=1, description="这一阶段实际展开的经历")
    resulting_change: str = Field(min_length=1, description="经历后人物的办法、关系、所能或处境如何变化")
    space_share: int = Field(ge=1, le=100, description="本次规划篇幅的参考百分比；不是张力或正文配额")
    summary_processes: str = Field(description="可概述或跨过的普通过程；无则空串")


class _StoryIntentFields(BaseModel):
    """全书选择的阅读方向；用户原话由程序保留，模型只补充设计。"""

    model_config = ConfigDict(extra="forbid", frozen=True)

    reader_interest: str = Field(..., min_length=1)
    protagonist_start: str = Field(..., min_length=1)
    personal_drive: str = Field(..., min_length=1)
    central_pressure: str = Field(..., min_length=1)
    narrative_tone: str = Field(..., min_length=1)
    opening_orientation: str = Field(..., min_length=1)


class StoryProgressionDesign(BaseModel):
    """全书生成方向；不定义章级必达、章号或文学配额。"""

    model_config = ConfigDict(extra="forbid", frozen=True, str_strip_whitespace=True)

    story_engine: str = Field(..., min_length=1, description="本书持续产生值得阅读的经历的动力")
    opening_development: str = Field(..., min_length=1, description="开篇怎样进入主要经历，非首章必达")
    stage_progression: str = Field(..., min_length=1, description="阶段经历和结果如何打开下一阶段")
    payoff_rhythm: str = Field(..., min_length=1, description="阶段所得与后续期待的关系，不设频次")
    time_and_detail: str = Field(..., min_length=1, description="本书哪些经历展开、哪些时间与过程概述")


class StoryIntentDesign(_StoryIntentFields):
    """同一次定位调用完成推进设计，模型不接管用户原文。"""

    version: Literal[2] = 2
    progression_design: StoryProgressionDesign


class ScopedStoryIntentDesign(StoryIntentDesign):
    """同一次定位明确本次范围的实际成果，不额外调用规划者。"""

    scope_outcome: str = Field(min_length=1, description="本次规划结束时已实际发生的阶段结果，与主要阅读体验相配")


class ScopeExperienceDesign(BaseModel):
    """同次定位区分入场条件与入场后经历；供规划，不形成正文配额。"""
    model_config = ConfigDict(extra="forbid", frozen=True, str_strip_whitespace=True)
    core_experience: str = Field(min_length=1, description="读者主要持续体验什么")
    entry_conditions: str = Field(min_length=1, description="进入主要经历必要的条件；已进入可说明现状")
    experience_after_entry: str = Field(min_length=1, description="进入后实际展开哪些不同经历及其结果")
    subsequent_use: str = Field(min_length=1, description="所得或变化之后怎样影响新的尝试、关系和处境")


class ExperiencedStoryIntentDesign(ScopedStoryIntentDesign):
    scope_experience: ScopeExperienceDesign


class StoryIntent(_StoryIntentFields):
    """旧版原样兼容；v4 保存规划尺度，v5 分开进入条件与主要经历。"""

    version: Literal[1, 2, 3, 4, 5, 6, 7] = 1
    authoring_plan: AuthoringContract | None = Field(None, exclude_if=lambda value: value is None)
    planning_scope: PlanningScope | None = Field(None, exclude_if=lambda value: value is None)
    scope_outcome: str | None = Field(None, min_length=1, exclude_if=lambda value: value is None)
    scope_experience: ScopeExperienceDesign | None = Field(None, exclude_if=lambda value: value is None)
    progression_design: StoryProgressionDesign | None = Field(
        default=None, exclude_if=lambda value: value is None,
    )
    user_request: UserNarrativeRequest | None = Field(
        default=None, exclude_if=lambda value: value is None,
    )

    @model_validator(mode="after")
    def request_version(self) -> StoryIntent:
        if (self.version >= 7) != (self.authoring_plan is not None):
            raise ValueError("选择式规划必须使用v7；旧故事不得静默加入规划")
        if self.version >= 2 and self.user_request is None:
            raise ValueError("故事定位 v2及以后版本 缺少用户原始要求")
        if self.version == 1 and self.user_request is not None:
            raise ValueError("带用户原始要求的故事定位须使用 v2及以后版本")
        if (self.version >= 3) != (self.progression_design is not None):
            raise ValueError("故事定位 v3及以后版本必须保留推进设计；旧版不能静默加入")
        if self.version < 6 and self.user_request is not None and self.user_request.version != 1:
            raise ValueError("旧故事定位不能静默使用新的阅读交接语义")
        if self.version >= 6 and (self.user_request is None or self.user_request.version != 2):
            raise ValueError("故事定位 v6 必须原样保存整体阅读偏好")
        if (self.version >= 5) != (self.scope_experience is not None):
            raise ValueError("故事定位 v5 必须保留主要经历设计；旧版不得静默加入")
        if self.version >= 4:
            if self.planning_scope is None or not self.scope_outcome or not self.scope_outcome.strip():
                raise ValueError("故事定位 v4及以后版本缺少规划范围或本段成果")
        elif self.planning_scope is not None or self.scope_outcome is not None:
            raise ValueError("旧故事定位不能静默加入规划范围")
        return self


class ReaderInformation(BaseModel):
    """从已有事实选择可交代的材料；不证明读者已经在正文中读过。"""

    model_config = ConfigDict(extra="forbid", frozen=True)

    source_path: str = Field(..., min_length=1)
    text: str = Field(..., min_length=1)
    use: Literal["introduce", "background", "defer"]


class CharacterParticipation(BaseModel):
    """相关引用与当场参与分开；分类由出卡模型决定。"""

    model_config = ConfigDict(extra="forbid", frozen=True)

    character_id: str = Field(..., pattern=ID_PATTERN)
    mode: Literal["present", "recalled", "mentioned", "restricted"]


class CharacterMotive(BaseModel):
    """当前欲望和切身利害复制人物设计，不在 Writer 前临时编造。"""

    model_config = ConfigDict(extra="forbid", frozen=True)

    present_desire: str = ""
    personal_stakes: str = ""


class NarrativeEventContract(BaseModel):
    """小纲同次设计的事实合同；不包括台词、情绪呈现或动作分镜。"""

    model_config = ConfigDict(extra="forbid", frozen=True)

    required_events: list[str] = Field(..., description="必要事件与结果；已由 typed 戏剧合同承接的结果不重复填写")
    unresolved_questions: list[str] = Field(..., description="只约束章末的未决问题，不要求逐条念出未知")
    forbidden_results: list[str] = Field(..., description="真正不能发生的行动、结果或秘密披露；不限制普通表达")

    @model_validator(mode="after")
    def distinct_facts(self) -> NarrativeEventContract:
        groups = (self.required_events, self.unresolved_questions, self.forbidden_results)
        for values in groups:
            if any(not value.strip() for value in values) or len(set(values)) != len(values):
                raise ValueError("事件合同不能包含空白或逐字重复的事实")
        if set(self.required_events) & set(self.forbidden_results):
            raise ValueError("同一事实不能同时是必达和禁止结果")
        return self


class NarrativeContext(BaseModel):
    """小纲至卡片的版本化交接；字段均为写作材料，不增加验收配额。"""

    model_config = ConfigDict(extra="forbid", frozen=True)

    version: Literal[1, 2] = 1
    event_contract: NarrativeEventContract | None = Field(
        None, exclude_if=lambda value: value is None,
        description="v2 必备的事实合同；其他叙事材料不自动升级为必达。v1 兼容旧书。",
    )
    story_intent: StoryIntent
    reader_information: list[ReaderInformation] = Field(default_factory=list)
    character_motives: dict[str, CharacterMotive] = Field(default_factory=dict)
    participation: list[CharacterParticipation] = Field(default_factory=list)

    @model_validator(mode="after")
    def unique_participation(self) -> NarrativeContext:
        if self.story_intent.version >= 6 and self.version != 2:
            raise ValueError("新阅读交接必须保留事件、状态与表达分离的事实合同")
        if (self.version == 2) != (self.event_contract is not None):
            raise ValueError("叙事 v2 必须保留独立事件合同，旧版不能静默加入")
        ids = [p.character_id for p in self.participation]
        if len(ids) != len(set(ids)):
            raise ValueError("人物参与方式不能重复")
        return self
