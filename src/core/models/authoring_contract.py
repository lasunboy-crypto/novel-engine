"""用户选择的小说规划；表达偏好不成为正文逐项验收表。"""
from __future__ import annotations

from typing import Literal

from pydantic import BaseModel, ConfigDict, Field, model_validator
from src.core.models.style_template import StyleTemplateName
from src.core.models.volume import TensionTemplateName


class AuthoringPreferences(BaseModel):
    model_config = ConfigDict(extra="forbid")
    total_volumes: int = Field(default=5, ge=1, le=20)
    chapters_per_volume: int = Field(default=30, ge=2, le=200)
    volume_chapters: list[int] = Field(default_factory=list)
    fixed_chapter_counts: bool = False
    reference_chapter_words: int = Field(default=2000, ge=500, le=10000)
    opening: Literal["direct", "brief", "extended", "suggested"] = "brief"
    voice: Literal["third_person", "first_person", "free"] = "third_person"
    reading_reference: str = ""
    style_template: StyleTemplateName | None = None
    tension_template: TensionTemplateName = "heros_journey"
    payoff: Literal["natural", "balanced", "strong"] = "balanced"
    romance: Literal["none", "light", "important", "suggested"] = "suggested"

    @model_validator(mode="after")
    def counts_valid(self) -> AuthoringPreferences:
        if self.volume_chapters and (len(self.volume_chapters) != self.total_volumes
                or any(n < 2 or n > 200 for n in self.volume_chapters)):
            raise ValueError("各卷章数必须与卷数一致，每卷2—200章")
        return self

    def counts(self) -> list[int]:
        return self.volume_chapters or [self.chapters_per_volume] * self.total_volumes

    def entry_deadline(self) -> int | None:
        return {"direct": 1, "brief": 4, "extended": 7, "suggested": None}[self.opening]


class AuthoringEntity(BaseModel):
    model_config = ConfigDict(extra="forbid")
    entity_id: str = Field(pattern=r"^[a-z][a-z0-9_-]+$")
    kind: Literal["character", "faction", "object"]
    name: str = Field(min_length=1)
    alternatives: list[str] = Field(default_factory=list)


class AuthoringMilestone(BaseModel):
    model_config = ConfigDict(extra="forbid")
    milestone_id: str = Field(pattern=r"^[a-z][a-z0-9_-]+$")
    kind: Literal["core_entry", "first_use", "first_result"]
    event: str = Field(min_length=1)


class AuthoringContract(BaseModel):
    """只传已选择的内容，不下传备选、交互记录和未采用的稿件。"""
    model_config = ConfigDict(extra="forbid")
    version: Literal[1] = 1
    revision: int = Field(ge=1)
    sections: dict[str, str]
    entities: list[AuthoringEntity]
    preferences: AuthoringPreferences
    milestones: list[AuthoringMilestone]

    @model_validator(mode="after")
    def unique_ids(self) -> AuthoringContract:
        for ids in ([e.entity_id for e in self.entities], [m.milestone_id for m in self.milestones]):
            if len(ids) != len(set(ids)):
                raise ValueError("规划引用ID重复")
        if sum(m.kind == "core_entry" for m in self.milestones) != 1:
            raise ValueError("规划须有且仅有一个真正进入核心经历的节点")
        return self

    def reading_preferences(self) -> str:
        p = self.preferences
        return "\n".join(filter(None, [p.reading_reference, self.sections.get("style", ""),
            "原始创意保留为来源；人物、组织和道具的当前名称以正式故事材料为准。",
            f"主导人称：{p.voice}；爽感方向：{p.payoff}；感情线：{p.romance}。这些是整体偏好，不设次数或篇幅配额。",
            f"每章参考约{p.reference_chapter_words}字；字数不作为拒稿条件。",
            self.sections.get("boundaries", "")]))
