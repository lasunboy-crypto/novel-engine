"""Source-bound, single-section outline patch contract used before any product write."""
from __future__ import annotations

from copy import deepcopy
from typing import Any

from pydantic import BaseModel, ConfigDict, Field, model_validator

from src.core.agents.outline_review_packets import digest, leaves, resolve
from src.core.models.outline import CharactersOutput, PlotOutput, VolumePlansOutput, WorldOutput


SECTIONS = ("world", "characters", "plot", "volume_plans")
MODELS: dict[str, type[BaseModel]] = dict(world=WorldOutput, characters=CharactersOutput, plot=PlotOutput,
                                         volume_plans=VolumePlansOutput)
DEPENDENTS = {
    "world": ("characters", "plot", "volume_plans", "mini_plan"),
    "characters": ("plot", "volume_plans", "mini_plan"),
    "plot": ("volume_plans", "mini_plan"),
    "volume_plans": ("mini_plan",),
}


class RepairBoundaryError(ValueError):
    """The proposal is stale, changes unapproved facts, or breaks a section contract."""


class FieldChange(BaseModel):
    model_config = ConfigDict(extra="forbid")
    path: str = Field(min_length=1)
    old: str
    new: str


class PatchProposal(BaseModel):
    model_config = ConfigDict(extra="forbid")
    source_hash: str = Field(pattern=r"^[0-9a-f]{64}$")
    changes: list[FieldChange] = Field(min_length=1, max_length=8)
    explanation: str = Field(min_length=1, max_length=1200)

    @model_validator(mode="after")
    def unique_paths(self) -> PatchProposal:
        if len({change.path for change in self.changes}) != len(self.changes):
            raise ValueError("补丁字段重复")
        return self


class ObjectChange(BaseModel):
    model_config = ConfigDict(extra="forbid")
    path: str = Field(min_length=1)
    old: dict[str, Any]
    new: dict[str, Any]


class FailedCandidatePatchProposal(BaseModel):
    """A quarantined candidate may repair text across sections and one typed beat contract."""

    model_config = ConfigDict(extra="forbid")
    source_hash: str = Field(pattern=r"^[0-9a-f]{64}$")
    changes: list[FieldChange] = Field(default_factory=list, max_length=8)
    contract_changes: list[ObjectChange] = Field(default_factory=list, max_length=2)
    explanation: str = Field(min_length=1, max_length=1200)

    @model_validator(mode="after")
    def unique_nonempty_paths(self) -> FailedCandidatePatchProposal:
        paths = [item.path for item in self.changes] + [item.path for item in self.contract_changes]
        if not paths or len(paths) != len(set(paths)):
            raise ValueError("候选补丁缺少改动或字段重复")
        return self


def dot_pointer(path: str) -> str:
    """The reviewer's dot path is converted only after exact source-path admission."""
    if not path or any(not part or part in {".", ".."} for part in path.split(".")):
        raise RepairBoundaryError("无效来源字段")
    return "/" + "/".join(part.replace("~", "~0").replace("/", "~1") for part in path.split("."))


def allowed_fields(source: dict[str, Any], quotes: list[dict], requested: list[str]) -> tuple[str, dict[str, str]]:
    if not requested or len(requested) > 8 or len(set(requested)) != len(requested):
        raise RepairBoundaryError("请选择 1 至 8 个不重复的原文叶字段")
    quoted = {item["path"] for item in quotes}
    if not set(requested) <= quoted:
        raise RepairBoundaryError("只能选择这条矛盾所引用的字段")
    sections = {path.split(".", 1)[0] for path in requested}
    if len(sections) != 1 or next(iter(sections)) not in SECTIONS:
        raise RepairBoundaryError("一次只允许修改同一大纲段")
    section = next(iter(sections))
    admitted: dict[str, str] = {}
    for path in requested:
        pointer = dot_pointer(path)
        if pointer not in leaves(source):
            raise RepairBoundaryError(f"只允许修改已有叶字段：{path}")
        value = resolve(source, pointer)
        if not isinstance(value, str):
            raise RepairBoundaryError(f"只允许修改原文字符串：{path}")
        if path.rsplit(".", 1)[-1] in {"character_id", "volume_id", "arc_id", "beat_id", "phase_id", "turning_id"}:
            raise RepairBoundaryError("结构身份字段不能通过文本补丁修改")
        admitted[path] = value
    return section, admitted


def apply_proposal(source: dict[str, Any], *, source_hash: str, allowed: dict[str, str],
                   proposal: PatchProposal) -> tuple[dict[str, Any], str]:
    if digest(source) != source_hash or proposal.source_hash != source_hash:
        raise RepairBoundaryError("大纲源版本已经变化")
    if set(change.path for change in proposal.changes) != set(allowed):
        raise RepairBoundaryError("补丁必须且只能包含本次获准字段")
    sections = {path.split(".", 1)[0] for path in allowed}
    if len(sections) != 1:
        raise RepairBoundaryError("不能跨大纲段写入")
    section = next(iter(sections))
    candidate = deepcopy(source)
    for change in proposal.changes:
        if change.old != allowed[change.path] or change.new == change.old or not change.new.strip():
            raise RepairBoundaryError("旧值不符、空改动或清空字段")
        parts = change.path.split(".")
        parent: Any = candidate
        for part in parts[:-1]:
            parent = parent[int(part)] if isinstance(parent, list) else parent[part]
        last = parts[-1]
        parent[int(last) if isinstance(parent, list) else last] = change.new
    before, after = leaves(source), leaves(candidate)
    changed = {path for path in before if after.get(path) != before[path]}
    if set(after) != set(before) or changed != {dot_pointer(path) for path in allowed}:
        raise RepairBoundaryError("补丁越界或改变了大纲结构")
    MODELS[section].model_validate(candidate[section])
    return candidate, section


def allowed_failed_candidate_fields(
    source: dict[str, Any], quotes: list[dict], requested: list[str],
) -> dict[str, str | dict[str, Any]]:
    """Admit only quoted strings or the enclosing typed dramatic contract."""
    if not requested or len(requested) > 10 or len(requested) != len(set(requested)):
        raise RepairBoundaryError("请选择 1 至 10 个不重复的引用字段")
    if any(a != b and b.startswith(a + ".") for a in requested for b in requested):
        raise RepairBoundaryError("戏剧合同整体与其中叶字段不能同时修改")
    quoted = {item["path"] for item in quotes}
    admitted: dict[str, str | dict[str, Any]] = {}
    for path in requested:
        is_contract = path.endswith(".dramatic_contract") and any(
            quote.startswith(path + ".") for quote in quoted
        )
        if path not in quoted and not is_contract:
            raise RepairBoundaryError(f"字段未被失败审核引用：{path}")
        section = path.split(".", 1)[0]
        if section not in SECTIONS:
            raise RepairBoundaryError("不可修改大纲以外的字段")
        value = resolve(source, dot_pointer(path))
        if is_contract:
            if not isinstance(value, dict):
                raise RepairBoundaryError("戏剧合同原值不是对象")
        elif not isinstance(value, str) or dot_pointer(path) not in leaves(source):
            raise RepairBoundaryError("仅可修改已引用的字符串叶字段")
        if path.rsplit(".", 1)[-1] in {
            "character_id", "volume_id", "arc_id", "beat_id", "phase_id", "turning_id",
        }:
            raise RepairBoundaryError("结构身份字段不能单独修改")
        admitted[path] = value
    return admitted


def apply_failed_candidate_proposal(
    source: dict[str, Any], *, source_hash: str,
    allowed: dict[str, str | dict[str, Any]], proposal: FailedCandidatePatchProposal,
) -> dict[str, Any]:
    if digest(source) != source_hash or proposal.source_hash != source_hash:
        raise RepairBoundaryError("隔离候选来源已变化")
    text_paths = {item.path for item in proposal.changes}
    contract_paths = {item.path for item in proposal.contract_changes}
    if text_paths & contract_paths or text_paths | contract_paths != set(allowed):
        raise RepairBoundaryError("补丁字段与准入范围不同")
    candidate = deepcopy(source)
    def _replace_value(path: str, value: Any) -> None:
        parts = path.split(".")
        target: Any = candidate
        for part in parts[:-1]:
            target = target[int(part)] if isinstance(target, list) else target[part]
        last = parts[-1]
        target[int(last) if isinstance(target, list) else last] = value

    for text_change in proposal.changes:
        old = allowed[text_change.path]
        if text_change.old != old or text_change.new == old:
            raise RepairBoundaryError("旧值不符或没有实质改动")
        if not isinstance(old, str) or not text_change.new.strip():
            raise RepairBoundaryError("文本补丁类型或内容无效")
        _replace_value(text_change.path, text_change.new)
    for contract_change in proposal.contract_changes:
        old = allowed[contract_change.path]
        if contract_change.old != old or contract_change.new == old:
            raise RepairBoundaryError("旧值不符或没有实质改动")
        if not isinstance(old, dict) or set(contract_change.new) != set(old):
            raise RepairBoundaryError("戏剧合同字段结构改变")
        _replace_value(contract_change.path, contract_change.new)
    before, after = leaves(source), leaves(candidate)
    changed = {path for path in before if after.get(path) != before[path]}
    allowed_pointers = [dot_pointer(path) for path in allowed]
    if set(before) != set(after) or any(
        not any(path == parent or path.startswith(parent + "/") for parent in allowed_pointers)
        for path in changed
    ):
        raise RepairBoundaryError("补丁改变了未获准字段或结构")
    for section in SECTIONS:
        MODELS[section].model_validate(candidate[section])
    return candidate
