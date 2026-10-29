"""Source-bound checks for the two small questions in an outline repair review."""
from __future__ import annotations

from typing import Literal

from pydantic import BaseModel, ConfigDict, Field


class RepairReviewQuote(BaseModel):
    model_config = ConfigDict(extra="forbid")

    path: str = Field(min_length=1)
    quote: str = Field(min_length=4, max_length=500)


class RepairReviewFinding(BaseModel):
    model_config = ConfigDict(extra="forbid")

    reason: str = Field(min_length=1, max_length=1200)
    source_quotes: list[RepairReviewQuote] = Field(min_length=2, max_length=8)


class RepairDeltaReview(BaseModel):
    """A compatible answer is useful only after the source and coverage checks below."""

    model_config = ConfigDict(extra="forbid")

    job_id: Literal["resolution", "regression"]
    repair_id: str
    candidate_hash: str
    outcome: Literal["compatible", "conflict", "unknown"]
    reviewed_changed_paths: list[str] = Field(min_length=1, max_length=8)
    evidence_quotes: list[RepairReviewQuote] = Field(min_length=2, max_length=12)
    findings: list[RepairReviewFinding] = Field(max_length=10)
    reason: str = Field(min_length=1, max_length=1600)


def _value_at_path(material: dict, path: str) -> str:
    value: object = material
    try:
        for segment in path.split("."):
            value = value[int(segment)] if isinstance(value, list) else value[segment]  # type: ignore[index]
    except (KeyError, IndexError, TypeError, ValueError) as exc:
        raise ValueError(f"审核引文位置不存在: {path}") from exc
    if not isinstance(value, str):
        raise ValueError(f"审核引文不是字符串字段: {path}")
    return value


def validate_repair_delta_review(
    result: RepairDeltaReview, *, material: dict, repair_id: str, candidate_hash: str,
    changed_paths: set[str], prior_counterpart_paths: set[str], job_id: str,
) -> RepairDeltaReview:
    if result.repair_id != repair_id or result.candidate_hash != candidate_hash or result.job_id != job_id:
        raise ValueError("增量复审的任务或候选版本不一致")
    if len(result.reviewed_changed_paths) != len(changed_paths) or set(result.reviewed_changed_paths) != changed_paths:
        raise ValueError("增量复审没有覆盖每个获准修改字段")
    quotes = [*result.evidence_quotes, *(q for finding in result.findings for q in finding.source_quotes)]
    for item in quotes:
        if item.quote not in _value_at_path(material, item.path):
            raise ValueError(f"审核引文不在候选原文中: {item.path}")
    for path in changed_paths:
        if not any(item.path == path for item in quotes):
            raise ValueError(f"缺少修改字段的逐字引文: {path}")
    if job_id == "resolution" and not any(item.path in prior_counterpart_paths for item in quotes):
        raise ValueError("原冲突另一端未被逐字核对")
    if job_id == "regression" and not any(item.path not in changed_paths for item in quotes):
        raise ValueError("新增事实未与其余大纲原文对照")
    if result.outcome == "compatible" and result.findings:
        raise ValueError("兼容结论不能同时报告硬冲突或未知点")
    if result.outcome != "compatible" and not result.findings:
        raise ValueError("拦截结论必须说明具体冲突或未知点")
    for finding in result.findings:
        if not any(item.path in changed_paths for item in finding.source_quotes):
            raise ValueError("增量复审只处理与修改字段有关的问题")
    return result
