"""选择式策划草稿及请求；与正式大纲及旧工作区隔离。"""
from __future__ import annotations

import re
from typing import Literal

from pydantic import BaseModel, ConfigDict, Field, model_validator

from src.core.models.authoring_contract import (
    AuthoringContract, AuthoringEntity, AuthoringMilestone, AuthoringPreferences,
)
from src.core.models.outline import CharactersOutput, PlotOutput, WorldOutput

SectionKey = Literal["concept", "protagonist", "characters", "factions", "gift", "world",
                     "plot", "ending", "style", "relationships", "boundaries"]


class PlanOption(BaseModel):
    model_config = ConfigDict(extra="forbid")
    option_id: str = Field(min_length=1)
    label: str = Field(min_length=1)
    content: str = Field(min_length=1)
    milestones: list[AuthoringMilestone] | None = None


class PlanSection(BaseModel):
    model_config = ConfigDict(extra="forbid")
    key: SectionKey
    title: str
    options: list[PlanOption] = Field(min_length=1, max_length=4)
    selected: str

    @model_validator(mode="after")
    def selection_exists(self) -> PlanSection:
        ids = [o.option_id for o in self.options]
        if len(set(ids)) != len(ids) or self.selected not in ids:
            raise ValueError("选项ID重复或当前选择不存在")
        return self


class AuthoringProposal(BaseModel):
    model_config = ConfigDict(extra="forbid")
    title: str = Field(min_length=1)
    sections: list[PlanSection] = Field(min_length=1)
    entities: list[AuthoringEntity]
    milestones: list[AuthoringMilestone]

    @model_validator(mode="after")
    def complete_proposal(self) -> AuthoringProposal:
        from typing import get_args
        keys = [s.key for s in self.sections]
        if len(keys) != len(set(keys)) or set(keys) != set(get_args(SectionKey)):
            raise ValueError("提案必须包含全部选择组且不能重复")
        ids = [e.entity_id for e in self.entities]
        if len(ids) != len(set(ids)):
            raise ValueError("实体ID重复")
        names = {e.name: e.entity_id for e in self.entities}
        if len(names) != len(self.entities) or any(not name.strip() for name in names):
            raise ValueError("人物、组织、道具须有可区分的名称")
        def references(text: str) -> str:
            if not names:
                return text
            pattern = re.compile("|".join(re.escape(name) for name in sorted(names, key=len, reverse=True)))
            parts = re.split(r"(\[\[[^\[\]]+\]\])", text)
            return "".join(part if i % 2 else pattern.sub(lambda m: f"[[{names[m[0]]}]]", part) for i, part in enumerate(parts))
        for section in self.sections:
            for option in section.options:
                option.content = references(option.content)
                for milestone in option.milestones or []:
                    milestone.event = references(milestone.event)
        for milestone in self.milestones:
            milestone.event = references(milestone.event)
        texts = [o.content for s in self.sections for o in s.options] + [m.event for m in self.milestones] + [m.event for s in self.sections for o in s.options for m in (o.milestones or [])]
        for text in texts:
            refs = re.findall(r"\[\[([^\[\]]+)\]\]", text)
            if any(ref not in ids for ref in refs):
                raise ValueError("提案引用了不存在的人物、组织或道具")
        self.contract(1, AuthoringPreferences())
        return self

    def contract(self, revision: int, preferences: AuthoringPreferences) -> AuthoringContract:
        def render(text: str) -> str:
            names = {e.entity_id: e.name for e in self.entities}
            return re.sub(r"\[\[([^\[\]]+)\]\]", lambda match: names[match[1]], text)
        selected_plot = next(o for s in self.sections if s.key == "plot" for o in s.options if o.option_id == s.selected)
        return AuthoringContract(revision=revision, preferences=preferences,
            entities=[e.model_copy(update={"alternatives": []}) for e in self.entities],
            sections={s.key: render(next(o.content for o in s.options if o.option_id == s.selected)) for s in self.sections},
            milestones=[m.model_copy(update={"event": render(m.event)}) for m in (selected_plot.milestones or self.milestones)])


class AuthoringDraft(BaseModel):
    model_config = ConfigDict(extra="forbid")
    version: Literal[1] = 1
    revision: int = 1
    premise: str = Field(min_length=1)
    genre: str = "xianxia"
    preferences: AuthoringPreferences = Field(default_factory=AuthoringPreferences)
    proposal: AuthoringProposal | None = None
    status: Literal["draft", "prepared", "confirmed"] = "draft"
    prepared_hashes: dict[str, str] = Field(default_factory=dict)
    pending_changes: bool = False
    last_action: str = ""


class AuthoringSaveIn(BaseModel):
    model_config = ConfigDict(extra="forbid")
    expected_revision: int = Field(ge=0)
    premise: str | None = None
    genre: str | None = None
    preferences: AuthoringPreferences | None = None
    selections: dict[SectionKey, str] = Field(default_factory=dict)
    names: dict[str, str] = Field(default_factory=dict)
    custom_sections: dict[SectionKey, str] = Field(default_factory=dict)


class AuthoringActionIn(BaseModel):
    model_config = ConfigDict(extra="forbid")
    expected_revision: int = Field(ge=1)
    action: Literal["propose", "revise", "prepare", "confirm", "restore"]
    direction: str = ""
    restore_revision: int | None = None


class AuthoringView(BaseModel):
    model_config = ConfigDict(extra="forbid")
    draft: AuthoringDraft | None = None
    contract: AuthoringContract | None = None
    world: WorldOutput | None = None
    characters: CharactersOutput | None = None
    plot: PlotOutput | None = None
    history: list[int] = Field(default_factory=list)
    active_task_id: str | None = None
