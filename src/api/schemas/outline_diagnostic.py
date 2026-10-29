"""大纲只读对照诊断接口合同；不改变原书生成状态。"""
from typing import Literal
from pydantic import BaseModel, ConfigDict, Field, JsonValue


class DiagnosticStartIn(BaseModel):
    model_config = ConfigDict(extra="forbid")
    request_hash: str = Field(pattern=r"^[0-9a-f]{64}$")


class DiagnosticJobOut(BaseModel):
    model_config = ConfigDict(extra="forbid")
    job_id: str
    status: Literal["pending", "started", "completed", "invalid"]
    input_upper_bound: int
    result: JsonValue = None
    error: str | None = None


class DiagnosticOut(BaseModel):
    model_config = ConfigDict(extra="forbid")
    diagnostic_id: str
    source_hash: str
    request_hash: str
    authorization_present: bool
    first_attempt_reserve_cny: float
    all_attempts_reserve_cny: float
    cost_cap_cny: float
    physical_request_cap: int
    full_book_verdict: Literal[False] = False
    jobs: list[DiagnosticJobOut]


class RepairPrepareIn(BaseModel):
    model_config = ConfigDict(extra="forbid")
    diagnostic_id: str = Field(pattern=r"^diagnostic_[0-9a-f]{64}$")
    conflict_index: int = Field(ge=0)
    allowed_paths: list[str] = Field(min_length=1, max_length=8)
    direction: str = Field(min_length=1, max_length=1200)


class FailedCandidateRepairPrepareIn(BaseModel):
    model_config = ConfigDict(extra="forbid")
    conflict_indexes: list[int] = Field(min_length=1, max_length=12)
    allowed_paths: list[str] = Field(min_length=1, max_length=10)
    direction: str = Field(min_length=1, max_length=1200)
    parent_repair_id: str | None = Field(None, pattern=r"^repair_[0-9a-f]{64}$")
    parent_review_id: str | None = Field(None, pattern=r"^diagnostic_[0-9a-f]{64}$")


class RepairStartIn(BaseModel):
    model_config = ConfigDict(extra="forbid")
    request_hash: str = Field(pattern=r"^[0-9a-f]{64}$")


class RepairCommitIn(BaseModel):
    model_config = ConfigDict(extra="forbid")
    review_id: str = Field(pattern=r"^diagnostic_[0-9a-f]{64}$")


class RepairSourceQuoteOut(BaseModel):
    path: str
    quote: str


class RepairConflictOut(BaseModel):
    desc: str
    reason: str
    source_quotes: list[RepairSourceQuoteOut]


class RepairOut(BaseModel):
    model_config = ConfigDict(extra="forbid")
    repair_id: str
    request_hash: str
    source_hash: str
    section: str
    allowed_fields: dict[str, str | dict[str, JsonValue]]
    dependent_sections: list[str]
    status: Literal["pending", "started", "staged", "invalid", "committed"]
    review_id: str | None = None
    error: str | None = None
    proposal: JsonValue = None
    candidate_hash: str | None = None
    parent_repair_id: str | None = None
    parent_review_id: str | None = None
    source_conflicts: list[RepairConflictOut] = Field(default_factory=list)
    authorization_present: bool
    physical_request_cap: int
    cost_cap_cny: float
