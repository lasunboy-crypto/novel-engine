"""简易章节编排器(P1-B-13;P2-B-19 起接 vector_store / session_factory + 抽取记忆)。

职责:把 Writer + Reviewer + Reviser 串成"写 → 审 → (修订 / 重写) → 审 → ..."
循环,直到 ``verdict=pass`` 通过(``mark_final``)或触发预算耗尽 / Reviewer
连续解析失败而升人工(``HUMAN_INTERVENTION``)。

设计要点:
- **依赖反转**:接 ``LLMClient`` 协议,不实例化 ``ClaudeClient``;具体客户端
  实例由调用方(P1-B-15 ``ChapterService``)注入。
- **状态机即源**:每一次状态切换都调
  ``src.core.state_machine.transitions.transition``,不在 Orchestrator 内
  自己写状态推导。``force_escalate`` 走 ``force=True`` 显式接口(P1-B-16
  起的状态机审计 hook 也能捕获)。
- **counts 来源单一**:``revise_count`` / ``rewrite_count`` 来自
  ``ChapterCard.max_revises`` / ``max_rewrites``(P1-B-02 已落地的字段),
  ``orchestrate_chapter`` 函数签名不再重复声明,避免双源不一致。
- **状态推进不变量**:每个分支先调 agent,**成功后**再 ``transition``;agent
  抛异常时 ``state`` 保持原值并向上传播。``revise_count`` / ``rewrite_count``
  +1 时机:transition 把 state 切到 REVISING / REWRITING 之后立即 +1
  (对应 NEXT_STEPS"每次进 REVISING 后 revise_count += 1"语义)。
- **memory_section 由调用方传入**:Orchestrator 不读 ``data/world.md`` /
  ``characters.md``,职责纯协调;P1-B-15 ``ChapterService`` 调
  ``build_memory_section(paths)`` 后传进来。

P2-B-19 wiring(可选注入,默认 None 退化为 P1 行为):

- ``vector_store`` + ``session_factory`` 同时给定时:
  - Writer / Reviewer 在每次调用前各自打开短 session 透传 ``db_session`` +
    ``vector_store``(用于 voice 段拼装 / retriever 拿历史记忆),session 在
    调用边界 ``close()``,避免长事务跨 transition。
  - 终态 ``APPROVED`` 后调 ``memory_extractor.extract`` 抽增量记忆 + commit
    SQLite + 写 Chroma;**失败吞 + structlog ``memory_extraction_failed``**,
    不冒泡到 caller(章节已通过审稿 + final.md 已落,extract 是辅助任务,
    失败重试归 P3+)。W3.5-C 起 ``ChapterService`` 可通过
    ``post_approval_memory_enabled=False`` 关闭本同步 hook,在 ready 水位后
    调度后台富记忆加工。
- 任一为 ``None`` → 退化 P1 路径(Writer/Reviewer 不附 voice/检索段,APPROVED
  后不抽取记忆),保留与既有单测兼容。

post-v6 起正文生产固定单稿；自动重试仍由客户端和状态机的既有有界合同负责。
"""

from __future__ import annotations

import contextvars
from difflib import SequenceMatcher
import hashlib
import json
import uuid
from collections.abc import Callable, Iterator
from concurrent.futures import CancelledError, Executor, Future, ThreadPoolExecutor
from contextlib import contextmanager
from dataclasses import dataclass
from datetime import UTC, datetime
from functools import partial
from pathlib import Path
from typing import Any, Literal, cast

import structlog
from pydantic import BaseModel, Field
from sqlalchemy import select
from sqlalchemy.orm import Session

from src.core.agents.creative_delta import (
    EXPERIMENTAL_PIPELINE_CONTRACT, uses_experimental_pipeline,
)
from src.config import settings
from src.core.agents.continuity_validator import (
    ContinuityValidationResult,
    uses_existing_q3_for_hard_book_clock,
    validate_continuity,
)
from src.core.agents.cross_chapter_dedup_validator import (
    DedupResult,
    PriorChapter,
    validate_cross_chapter_dedup,
)
from src.core.agents.ending_tension_validator import (
    EndingTensionResult,
    validate_ending_tension,
)
from src.core.agents.memory_extractor import ExtractedSceneAnchor
from src.core.agents.memory_extractor import extract as extract_memories
from src.core.agents.scene_anchor_validator import (
    SceneAnchorResult,
    validate_scene_anchor,
)
from src.core.agents.thread_closure_validator import (
    ThreadClosureCandidate,
    validate_thread_closure_batch,
)
from src.core.agents.must_feasibility_validator import (
    RECENT_SUMMARIES_FOR_PRECHECK,
    validate_must_feasibility,
)
from src.core.agents.forbidden_crossing_validator import (
    ForbiddenCrossingResult,
    validate_forbidden_crossing,
)
from src.core.agents.must_achieve_validator import (
    RequiredCoreStateEvidence,
    ValidationResult,
    validate_must_achieve,
)
from src.core.agents.reviewer import ReviewerParseError, review_chapter
from src.core.agents.reviser import revise_chapter
from src.core.agents.setting_consistency_validator import (
    CharacterIdentityRef,
    CharacterMotivationRef,
    SettingConsistencyResult,
    validate_setting_consistency,
)
from src.core.agents.anti_tic_trim import trim_chapter_tics
from src.core.agents.word_count_trim import WordCountTrimOutput, trim_to_word_count
from src.core.agents.writer import (
    RewriteConstraintGuardrail,
    WriterDraftIntegrityError,
    WriterOutput,
    write_chapter,
)
from src.core.chapter_versioning import (
    ChapterVersioning,
    ChapterVersioningError,
    collect_canonical_version_files,
)
from src.core.db.models import (
    AttemptLedgerORM,
    ChapterORM,
    ChapterSummaryORM,
    ChapterVersionORM,
    ReviewIssueORM,
    ReviewResultORM,
    WorkspaceORM,
)
from src.core.llm.base import (
    MAX_PROVIDER_ATTEMPTS_PER_LOGICAL_CALL,
    LLMClient,
    TokenUsage,
)
from src.core.llm.cancel import LLMCancelledError
from src.core.memory.store import (
    list_characters_by_workspace,
    list_plot_threads_by_workspace,
    list_timeline_entries_by_workspace,
)
from src.core.memory.vector import VectorStore
from src.core.models.chapter import ChapterVersion
from src.core.models.chapter_card import ChapterCard, chapter_seq_from_id
from src.core.models.character import Character
from src.core.models.continuity_ledger import ProvenanceBoundContinuityCapsule
from src.core.models.core_state import CoreStateProjection
from src.core.models.review import (
    ConstraintResult,
    ConstraintViolation,
    ReviewIssue,
    ReviewResult,
    stable_constraint_id,
)
from src.core.models.setting_contract import SettingContract
from src.core.models.timeline_entry import TimelineEntry
from src.core.routing.router import ModelRouter, ProviderAttemptCapacity
from src.core.rules.cross_chapter_voice_validator import (
    resolve_cross_chapter_voice_constraint_spec,
)
from src.core.timing import StageTimer
from src.core.rules.style_fingerprint import (
    PovSummary,
    compute_style_fingerprint,
    summarize_voice_from_fingerprint,
)
from src.core.rules.style_drift_validator import (
    check_style_drift,
    resolve_rewrite_style_constraint_spec,
)
from src.core.rules.style_trend_validator import compute_trend_anchor
from src.core.rules.minimal_false_pass_guards import (
    apply_objective_reviewer_guards,
    guard_must_fail_to_pass,
)
from src.core.rules.prose_hard_contract import (
    ExecutableConstraintPlan,
    ExecutableConstraintPlanError,
    PARAGRAPH_MEAN_HARD_CONSTRAINT_ID,
    PARAGRAPH_MEAN_HARD_MIN,
    ProseConstraintStage,
    compile_executable_constraint_plan,
)
from src.core.rules.prose_revision_contract import (
    GLOBAL_NO_PROGRESS_FEEDBACK,
    ProseRevisionContract,
    ProseRevisionNoProgressError,
    build_reviser_revision_contract,
    build_rewrite_revision_contract,
    count_effective_content_rewrites,
)
from src.core.rules.paragraph_reflow import (
    PARAGRAPH_REFLOW_MODEL_USED,
    reflow_paragraph_mean_only,
)
from src.core.rules.validator_pipeline import (
    classify_validator_severity,
    merge_into_review_verdict_overrides,
    run_validator_pipeline,
)
from src.core.rules.warning_routing import (
    build_soft_warning,
    finalize_review_warning_signals,
    load_approved_final_warnings,
    render_warning_watchlist,
)
from src.core.rules.validators import (
    ValidatorResult,
    classify_word_count_acceptance,
    check_character_existence,
    check_dead_character_references,
    check_word_count,
    word_count_acceptance_bounds,
)
from src.core.state_machine.states import ChapterState
from src.core.state_machine.transitions import transition
from src.core.workspace.workspace_paths import WorkspacePaths

# P2-B-19:read-only / 抽取记忆共用的零参 session 工厂。每次调用返回新 session,
# orchestrator 内部用 ``_maybe_session`` contextmanager 包成短事务 / close on
# exit。``ChapterService`` 把"按 workspace_id 开 session"工厂闭包成零参再
# 注入,让 Orchestrator 不感知 workspace_id。
PipelineSessionFactory = Callable[[], Session]

# P1-B-20:每次状态切换后调用的 hook,签名 ``(prev_state, next_state) -> None``。
# 调用方(P1-B-19 路由层)在闭包里调 WebSocket manager.publish;Orchestrator
# 不 import manager,保持业务层不感知 WebSocket。同步签名:Orchestrator 是
# 同步代码,sync 回调让闭包零 ``await`` 调用。
StateTransitionCallback = Callable[[ChapterState, ChapterState], None]

_log = structlog.get_logger(__name__)

# Reviewer 解析失败的最大尝试次数。
# v1.0.x reviewer-resilience(2026-05-20):2 → 5。本地 Qwen 自由输出 JSON
# 单次失败概率约 5-20%,两次都失败概率仍可达 4%(luo2 ch-0001 实测命中)。
# 同时 reviewer.py 引入 json_repair.loads 兜底层 + retry 时 user message 附加
# "上次错误反馈"段引导 LLM 自我修正,综合后预期 5 次都失败的概率 < 0.1%。
REVIEWER_MAX_ATTEMPTS = 5
# Historical tests import the private name. Keep it as a compatibility alias;
# the Provider-0 stage registry imports the public owner constant above.
_REVIEWER_MAX_ATTEMPTS = REVIEWER_MAX_ATTEMPTS

# A GLOBAL rewrite that returns no material change receives exactly one
# same-model retry against the same frozen source/plan; a second no-progress
# result propagates and cannot materialise a version.
GLOBAL_REWRITE_NO_PROGRESS_MAX_RETRIES = 1


class OrchestratorResult(BaseModel):
    """章节编排循环的最终结果。

    ``escalation_reason`` 在 ``final_state == HUMAN_INTERVENTION`` 时给出原因;
    其他终态下必为 ``None``。**P1 阶段的可能取值集合**(消费方需 handle):

    - ``"rewrite_budget_exhausted"``:Reviewer 给 rewrite 判决但 ``rewrite_count``
      已达 ``ChapterCard.max_rewrites``,或 revise 名额耗尽后转 rewrite 时同样
      已达上限。
    - ``"reviewer_parse_failed_after_retry"``:Reviewer LLM 输出连续两次无法
      解析为合法 JSON,两次 ``raw_output`` 与 ``model_used`` 都已落
      structlog ``orchestrator_review_unparseable`` 事件供 P7 调优时回查。

    P2+ 可能新增取值,届时再扩展本 docstring。
    """

    final_state: ChapterState
    final_version_id: int | None = None
    final_path: Path | None = None
    reviews: list[ReviewResult] = Field(default_factory=list)
    versions: list[ChapterVersion] = Field(default_factory=list)
    revise_count: int = Field(0, ge=0)
    rewrite_count: int = Field(0, ge=0)
    escalation_reason: str | None = None

    # v1.1 acceptance 修复2 / Fix D:升人工时的结构化详情(目前仅
    # ``must_infeasible_pre_write`` 路径填 — ``{"findings": [{"must_text",
    # "confidence", "evidence", "suggestion"}], "checked_at_state": "pre_write"}``),
    # ChapterService 落 ``ChapterORM.escalation_details`` 供孤儿章人工裁决 UI
    # 展示"哪条 must 不可达 + 建议处置"。其余升人工路径 None(向后兼容)。
    escalation_details: dict[str, Any] | None = None

class _PersistedRewriteReviewContext(BaseModel):
    """显式 Rewrite 跨 invocation 恢复所需的最小审稿 checkpoint。"""

    constraint_results: list[ConstraintResult] = Field(default_factory=list)
    constraint_violations: list[ConstraintViolation] = Field(default_factory=list)
    failed_constraints: list[str] = Field(default_factory=list)
    hard_constraints_passed: bool
    verdict: Literal[
        "pass",
        "revise",
        "rewrite",
        "escalate_to_human",
        "parse_failed",
    ]
    score_overall: float = Field(allow_inf_nan=False)


class RewriteCheckpointRecoveryError(RuntimeError):
    """显式 Rewrite 的持久化审稿 checkpoint 无法安全恢复。"""


class PrewriteAdmissionDataError(RewriteCheckpointRecoveryError):
    """Writer 前权威角色数据无法读取；属于安全 checkpoint 恢复硬停。"""


def _run_prewrite_card_admission(
    *,
    chapter_card: ChapterCard,
    known_character_ids: set[str] | None,
    character_records: list[Character] | None,
) -> list[ValidatorResult]:
    """在任何 Writer/Rewriter/Reviser 调用前拦章节卡/数据缺陷。

    这两项检查都不读正文，正文模型也无法修复。返回值只含失败项；调用方统一
    进入 ``human_intervention``，不写版本、不消耗 rewrite/revise 预算。
    """
    failures: list[ValidatorResult] = []
    if known_character_ids is not None:
        existence = check_character_existence(
            chapter_card=chapter_card,
            known_character_ids=known_character_ids,
        )
        if not existence.passed:
            failures.append(existence)
    if character_records is not None:
        dead_reference = check_dead_character_references(
            chapter_card=chapter_card,
            character_records=character_records,
        )
        if not dead_reference.passed:
            failures.append(dead_reference)
    return failures


class RewriteCheckpointContext(BaseModel):
    """显式 Rewrite 在任何写入/Provider 前冻结的产品 checkpoint。"""

    version_id: int = Field(ge=1)
    source_content: str = Field(min_length=1)
    source_style_fingerprint: dict[str, float]
    v1_style_fingerprint: dict[str, float]
    review: _PersistedRewriteReviewContext | None = None
    review_result: ReviewResult | None = None
    word_count_policy_rebased: bool = False
    previous_reviewer_parse_errors: list[str] = Field(default_factory=list, max_length=1)
    review_contract_rebase_reason: Literal["rewind_requires_content_rewrite"] | None = (
        None
    )


@dataclass(frozen=True, slots=True)
class _WriterPromptContext:
    """唯一稿 Writer 的动态上下文。"""

    prev_chapter_voice_summary: PovSummary | None
    prev_version_fingerprint: dict[str, float] | None
    validator_failures: list[dict[str, Any]] | None
    paragraph_reflow_failures: list[dict[str, Any]] | None
    mechanical_reflow_only: bool
    defer_word_count_trim: bool
    rewrite_constraint_guardrail: RewriteConstraintGuardrail | None
    prev_chapter_baseline_fingerprint: dict[str, float] | None
    warning_watchlist_section: str
    executable_constraint_plan: ExecutableConstraintPlan
    revision_contract: ProseRevisionContract | None


@contextmanager
def _maybe_session(
    session_factory: PipelineSessionFactory | None,
) -> Iterator[Session | None]:
    """P2-B-19 helper:0-arg session factory → 短 session 上下文。

    给定 ``None`` 时 yield ``None``(P1 兼容路径,Writer/Reviewer 不附 voice
    / 检索段);给定时 ``factory()`` 开 session 并保证 ``close()`` 在退出时
    执行。**不在此处 commit/rollback**——Writer/Reviewer 路径只读,不需要;
    extract 路径由 caller 显式 commit/rollback(语义清晰,事务边界归 caller)。
    """
    if session_factory is None:
        yield None
        return
    session = session_factory()
    try:
        yield session
    finally:
        session.close()


def _format_persisted_constraint_violation(violation: ConstraintViolation) -> str:
    """把 R2 结构化违规原样送回显式 Rewrite，不改变 HARD/SOFT 裁决。"""
    message = (
        f"设定一致性[{violation.severity.upper()}:{violation.constraint_id[:12]}]:"
        f"规则「{violation.rule_quote}」；正文「{violation.evidence_quote}」"
    )
    if violation.ambiguity:
        message += f"；歧义说明「{violation.ambiguity}」"
    return message


def _validate_persisted_rewrite_review_semantics(
    review: _PersistedRewriteReviewContext,
    *,
    chapter_id: str,
    version_reviewed: int,
) -> None:
    """验证 review checkpoint 字段彼此自洽，避免把矛盾账本交给 Writer。"""
    constraint_ids = [item.constraint_id for item in review.constraint_results]
    if len(constraint_ids) != len(set(constraint_ids)):
        raise ValueError("constraint_results 存在重复 constraint_id")

    hard_violations = [
        item for item in review.constraint_violations if item.severity == "hard"
    ]
    for violation in hard_violations:
        setting_hard = (
            violation.domain == "world.unique_rules"
            and bool(violation.source_hash)
            and violation.ambiguity is None
        )
        deterministic_hard = (
            violation.domain
            in {"character.voice", "chapter.internal", "chapter.style"}
            and violation.ambiguity is None
            and violation.source_chapter_id in {None, chapter_id}
            and violation.source_final_version in {None, version_reviewed}
        )
        if not setting_hard and not deterministic_hard:
            raise ValueError("HARD ConstraintViolation 不满足确定性资格")

    hard_failure_evidence = (
        bool(review.failed_constraints)
        or any(result.status == "fail" for result in review.constraint_results)
        or bool(hard_violations)
    )
    if review.hard_constraints_passed == hard_failure_evidence:
        raise ValueError("hard_constraints_passed 与失败证据矛盾")
    if review.verdict == "pass" and not review.hard_constraints_passed:
        raise ValueError("verdict=pass 不能同时声明 HARD 未通过")


def _parse_persisted_rewrite_review_row(
    row: ReviewResultORM,
    *,
    chapter_id: str,
    version_reviewed: int,
) -> _PersistedRewriteReviewContext:
    """严格反序列化一条 review ORM；任一坏约束都视作账本损坏。"""
    raw_constraint_results = row.constraint_results
    raw_constraint_violations = row.constraint_violations
    raw_failed_constraints = row.failed_constraints
    if (
        not isinstance(raw_constraint_results, list)
        or not isinstance(raw_constraint_violations, list)
        or not isinstance(raw_failed_constraints, list)
    ):
        _log.error(
            "rewrite_checkpoint_review_shape_invalid",
            chapter_id=chapter_id,
            version_reviewed=version_reviewed,
            review_id=row.review_id,
        )
        raise RewriteCheckpointRecoveryError(
            "显式 Rewrite 的 review checkpoint 形状损坏: "
            f"{chapter_id}/v{version_reviewed}/review={row.review_id}"
        )
    if any(
        not isinstance(name, str) or not name.strip() for name in raw_failed_constraints
    ):
        _log.error(
            "rewrite_checkpoint_failed_constraint_invalid",
            chapter_id=chapter_id,
            version_reviewed=version_reviewed,
            review_id=row.review_id,
        )
        raise RewriteCheckpointRecoveryError(
            "显式 Rewrite 的 failed_constraints checkpoint 损坏: "
            f"{chapter_id}/v{version_reviewed}/review={row.review_id}"
        )

    constraint_results: list[ConstraintResult] = []
    for payload in raw_constraint_results:
        try:
            constraint_results.append(ConstraintResult.model_validate(payload))
        except Exception as exc:  # noqa: BLE001 — 坏账本必须在 Provider 前 fail-closed
            _log.error(
                "rewrite_checkpoint_constraint_result_invalid",
                chapter_id=chapter_id,
                version_reviewed=version_reviewed,
                review_id=row.review_id,
            )
            raise RewriteCheckpointRecoveryError(
                "显式 Rewrite 的 hard-gate checkpoint 损坏: "
                f"{chapter_id}/v{version_reviewed}/review={row.review_id}"
            ) from exc

    constraint_violations: list[ConstraintViolation] = []
    for payload in raw_constraint_violations:
        try:
            constraint_violations.append(ConstraintViolation.model_validate(payload))
        except Exception as exc:  # noqa: BLE001 — 坏账本必须在 Provider 前 fail-closed
            _log.error(
                "rewrite_checkpoint_constraint_violation_invalid",
                chapter_id=chapter_id,
                version_reviewed=version_reviewed,
                review_id=row.review_id,
            )
            raise RewriteCheckpointRecoveryError(
                "显式 Rewrite 的结构化违规 checkpoint 损坏: "
                f"{chapter_id}/v{version_reviewed}/review={row.review_id}"
            ) from exc

    try:
        review = _PersistedRewriteReviewContext(
            constraint_results=constraint_results,
            constraint_violations=constraint_violations,
            failed_constraints=raw_failed_constraints,
            hard_constraints_passed=row.hard_constraints_passed,
            verdict=cast(
                Literal[
                    "pass",
                    "revise",
                    "rewrite",
                    "escalate_to_human",
                    "parse_failed",
                ],
                row.verdict,
            ),
            score_overall=row.score_overall,
        )
        _validate_persisted_rewrite_review_semantics(
            review,
            chapter_id=chapter_id,
            version_reviewed=version_reviewed,
        )
        return review
    except Exception as exc:  # noqa: BLE001 — verdict/score 同属恢复 checkpoint
        _log.error(
            "rewrite_checkpoint_review_fields_invalid",
            chapter_id=chapter_id,
            version_reviewed=version_reviewed,
            review_id=row.review_id,
        )
        raise RewriteCheckpointRecoveryError(
            "显式 Rewrite 的 review checkpoint 字段损坏: "
            f"{chapter_id}/v{version_reviewed}/review={row.review_id}"
        ) from exc


def _restore_persisted_review_result(
    session: Session,
    *,
    row: ReviewResultORM,
    parsed: _PersistedRewriteReviewContext,
) -> ReviewResult | None:
    """恢复可直接续判的完整 ReviewResult；parse_failed 由 UI Rewrite 处理。"""
    if parsed.verdict == "parse_failed":
        return None
    if (
        not isinstance(row.suggestions, list)
        or any(not isinstance(item, str) for item in row.suggestions)
        or not isinstance(row.hard_constraint_regressions, list)
        or any(
            not isinstance(item, str) or not item.strip()
            for item in row.hard_constraint_regressions
        )
    ):
        raise RewriteCheckpointRecoveryError(
            f"审稿展示/回退 checkpoint 损坏: review={row.review_id}"
        )
    issue_rows = list(
        session.scalars(
            select(ReviewIssueORM)
            .where(ReviewIssueORM.review_id == row.review_id)
            .order_by(ReviewIssueORM.role, ReviewIssueORM.position, ReviewIssueORM.id)
        )
    )
    issues: list[ReviewIssue] = []
    revise_targets: list[ReviewIssue] = []
    for issue_row in issue_rows:
        issue = ReviewIssue(
            paragraph_anchor=issue_row.paragraph_anchor,
            issue_type=issue_row.issue_type,
            description=issue_row.description,
            suggestion=issue_row.suggestion,
        )
        if issue_row.role == "issue":
            issues.append(issue)
        elif issue_row.role == "revise_target":
            revise_targets.append(issue)
        else:
            raise RewriteCheckpointRecoveryError(
                f"审稿 issue role checkpoint 损坏: review={row.review_id}"
            )
    try:
        return ReviewResult(
            review_id=row.review_id,
            chapter_id=row.chapter_id,
            version_reviewed=row.version_reviewed,
            reviewed_at=row.reviewed_at,
            reviewer_model=row.reviewer_model,
            score_plot_progression=row.score_plot_progression,
            score_tension_match=row.score_tension_match,
            score_prose_quality=row.score_prose_quality,
            score_consistency=row.score_consistency,
            score_hook_management=row.score_hook_management,
            score_voice_consistency=row.score_voice_consistency,
            score_imagery=row.score_imagery,
            score_interiority=row.score_interiority,
            score_formulaic=row.score_formulaic,
            score_immersion=row.score_immersion,
            score_overall=row.score_overall,
            hard_constraints_passed=parsed.hard_constraints_passed,
            failed_constraints=list(parsed.failed_constraints),
            issues=issues,
            suggestions=list(row.suggestions),
            verdict=parsed.verdict,
            revise_targets=revise_targets,
            constraint_violations=list(parsed.constraint_violations),
            constraint_results=list(parsed.constraint_results),
            hard_constraint_regressions=list(row.hard_constraint_regressions),
            validator_failure_details=None,
        )
    except Exception as exc:
        raise RewriteCheckpointRecoveryError(
            f"完整审稿 checkpoint 无法恢复: review={row.review_id}"
        ) from exc


def _preexisting_setting_failure_suggestion(constraint_ids: list[str]) -> str:
    """生成新鲜审稿与 checkpoint 恢复共用的非 regression 说明。"""

    return (
        "本轮首次检出的 setting HARD 证据位于相对冻结源未改动的正文，"
        "因此不归因于本轮 rewrite 回退；Judge 的失败结论仍保留，并按现有"
        "局部修复合同处理。constraint IDs: " + " / ".join(constraint_ids)
    )


_HARD_REGRESSION_SUGGESTION_PREFIX = (
    "rewrite 破坏了上一轮已通过 hard gate（按稳定 constraint ID）: "
)


def _hard_regression_suggestion(
    *, constraint_ids: list[str], results_by_id: dict[str, ConstraintResult]
) -> str:
    labels = [
        (
            f"{constraint_id} "
            f"[{results_by_id[constraint_id].domain}] "
            f"{results_by_id[constraint_id].rule_text}"
            if constraint_id in results_by_id
            else constraint_id
        )
        for constraint_id in constraint_ids
    ]
    return (
        _HARD_REGRESSION_SUGGESTION_PREFIX
        + " / ".join(labels)
        + "。请检查章节卡/大纲是否存在约束冲突,不要继续让 writer 猜。"
    )


def _normalize_persisted_preexisting_regressions(
    *,
    review: ReviewResult,
    previous_content: str,
    current_content: str,
    workspace_id: str,
) -> ReviewResult:
    """恢复时移除旧代码误加的 regression 归因，不修改 Judge 失败事实。

    只处理同时满足以下条件的 setting ID：已被持久化为 regression、当前仍是
    fail，且完整证据位于 previous/current 的同一未改 matching block。原
    ``ConstraintResult``、setting HARD violation、verdict 与 hard=false 均保留。
    """

    if not review.hard_constraint_regressions:
        return review
    failed_setting_by_id = {
        result.constraint_id: result
        for result in review.constraint_results
        if result.domain == "setting" and result.status == "fail"
    }
    preexisting_ids = [
        constraint_id
        for constraint_id in review.hard_constraint_regressions
        if (
            constraint_id in failed_setting_by_id
            and _evidence_is_unchanged_between_versions(
                evidence=failed_setting_by_id[constraint_id].details,
                source_content=previous_content,
                current_content=current_content,
            )
        )
    ]
    if not preexisting_ids:
        return review

    preexisting = set(preexisting_ids)
    regression_markers = {
        f"hard_constraint_regression:{constraint_id}"
        for constraint_id in preexisting_ids
    }
    suggestion = _preexisting_setting_failure_suggestion(preexisting_ids)
    remaining_regressions = [
        constraint_id
        for constraint_id in review.hard_constraint_regressions
        if constraint_id not in preexisting
    ]
    suggestions = [
        item
        for item in review.suggestions
        if not item.startswith(_HARD_REGRESSION_SUGGESTION_PREFIX)
    ]
    if remaining_regressions:
        suggestions.append(
            _hard_regression_suggestion(
                constraint_ids=remaining_regressions,
                results_by_id={
                    result.constraint_id: result
                    for result in review.constraint_results
                },
            )
        )
    if suggestion not in suggestions:
        suggestions.append(suggestion)
    _log.warning(
        "persisted_hard_constraint_regression_reclassified",
        workspace_id=workspace_id,
        chapter_id=review.chapter_id,
        version_reviewed=review.version_reviewed,
        constraint_ids=preexisting_ids,
    )
    return review.model_copy(
        update={
            "failed_constraints": [
                marker
                for marker in review.failed_constraints
                if marker not in regression_markers
            ],
            "hard_constraint_regressions": remaining_regressions,
            "suggestions": suggestions,
        }
    )


def load_rewrite_checkpoint_context(
    session_factory: PipelineSessionFactory | None,
    *,
    paths: WorkspacePaths,
    chapter_id: str,
    version_id: int,
    allow_stale_chapter_projection: bool = False,
    allow_unpersisted_reviewer_task_id: str | None = None,
) -> RewriteCheckpointContext:
    """冻结显式 Rewrite 所需正文、v1 风格和审稿账本；损坏即 fail-closed。

    ``allow_unpersisted_reviewer_task_id`` 只由 ChapterService 在精确验真的
    用户取消断点或已归档且语法无效的本地 Reviewer 格式失败中传入。它仅忽略
    该旧 task 的一个已成功但未落库 Reviewer 响应，使当前正文重新进入 REVIEWING；started、其他 task、已有
    ReviewResult 或任意版本/磁盘漂移仍照常阻断。
    """
    versioning = ChapterVersioning(paths)
    try:
        disk_version_files = collect_canonical_version_files(
            paths.chapter_dir(chapter_id)
        )
        v1_content = versioning.read_version_content(chapter_id, 1)
        source_content = (
            v1_content
            if version_id == 1
            else versioning.read_version_content(chapter_id, version_id)
        )
    except (OSError, UnicodeDecodeError, ChapterVersioningError) as exc:
        _log.exception(
            "rewrite_checkpoint_chapter_content_load_failed",
            chapter_id=chapter_id,
            version_id=version_id,
        )
        raise RewriteCheckpointRecoveryError(
            f"显式 Rewrite 的正文 checkpoint 缺失或损坏: {chapter_id}/v{version_id}"
        ) from exc
    if not v1_content.strip() or not source_content.strip():
        _log.error(
            "rewrite_checkpoint_chapter_content_empty",
            chapter_id=chapter_id,
            version_id=version_id,
        )
        raise RewriteCheckpointRecoveryError(
            f"显式 Rewrite 的正文 checkpoint 为空: {chapter_id}/v{version_id}"
        )

    review: _PersistedRewriteReviewContext | None = None
    review_result: ReviewResult | None = None
    if session_factory is not None:
        try:
            with _maybe_session(session_factory) as session:
                assert session is not None
                chapter_row = session.get(ChapterORM, chapter_id)
                version_rows = list(
                    session.scalars(
                        select(ChapterVersionORM)
                        .where(ChapterVersionORM.chapter_id == chapter_id)
                        .order_by(ChapterVersionORM.version_id)
                    )
                )
                rows_by_version = {row.version_id: row for row in version_rows}
                db_version_ids = set(rows_by_version)
                disk_version_ids = set(disk_version_files)
                v1_row = rows_by_version.get(1)
                source_row = rows_by_version.get(version_id)
                review_row = (
                    session.execute(
                        select(ReviewResultORM)
                        .where(ReviewResultORM.chapter_id == chapter_id)
                        .where(ReviewResultORM.version_reviewed == version_id)
                        .order_by(
                            ReviewResultORM.reviewed_at.desc(),
                            ReviewResultORM.review_id.desc(),
                        )
                    )
                    .scalars()
                    .first()
                )

                if chapter_row is None:
                    raise RewriteCheckpointRecoveryError(
                        f"显式 Rewrite 的章节账本缺失: {chapter_id}"
                    )
                if chapter_row.workspace_id != paths.workspace_id:
                    raise RewriteCheckpointRecoveryError(
                        f"显式 Rewrite 的 workspace checkpoint 串位: {chapter_id}"
                    )
                if not db_version_ids or max(db_version_ids) != version_id:
                    raise RewriteCheckpointRecoveryError(
                        "显式 Rewrite 的 DB 最新版本已变化: "
                        f"{chapter_id}/expected-v{version_id}/"
                        f"db-versions={sorted(db_version_ids)}"
                    )
                if disk_version_ids != db_version_ids:
                    raise RewriteCheckpointRecoveryError(
                        "显式 Rewrite 的磁盘/DB 版本集合不一致: "
                        f"{chapter_id}/disk={sorted(disk_version_ids)}/"
                        f"db={sorted(db_version_ids)}"
                    )
                if (
                    not allow_stale_chapter_projection
                    and chapter_row.current_version != version_id
                ):
                    raise RewriteCheckpointRecoveryError(
                        "显式 Rewrite 的 current_version 已变化: "
                        f"{chapter_id}/expected-v{version_id}/"
                        f"actual-v{chapter_row.current_version}"
                    )
                if v1_row is None or source_row is None:
                    raise RewriteCheckpointRecoveryError(
                        f"显式 Rewrite 的版本账本缺失: {chapter_id}/v{version_id}"
                    )
                if v1_row.content != v1_content or source_row.content != source_content:
                    raise RewriteCheckpointRecoveryError(
                        f"显式 Rewrite 的 SQLite/正文文件 checkpoint 不一致: "
                        f"{chapter_id}/v{version_id}"
                    )
                reviewer_attempts = list(
                    session.scalars(
                        select(AttemptLedgerORM)
                        .where(AttemptLedgerORM.chapter_id == chapter_id)
                        .where(
                            AttemptLedgerORM.agent_name.in_(
                                ("reviewer", "reviewer_escalation")
                            )
                        )
                        .where(AttemptLedgerORM.created_at >= source_row.created_at)
                        .order_by(AttemptLedgerORM.created_at.desc())
                    )
                )
                unsafe_attempt = next(
                    (
                        attempt
                        for attempt in reviewer_attempts
                        if attempt.status == "started"
                        or (
                            attempt.status == "succeeded"
                            and not (
                                review_row is None
                                and allow_unpersisted_reviewer_task_id is not None
                                and attempt.task_id
                                == allow_unpersisted_reviewer_task_id
                                and attempt.agent_name == "reviewer"
                            )
                            and (
                                review_row is None
                                or attempt.completed_at is None
                                or attempt.completed_at > review_row.reviewed_at
                            )
                        )
                    ),
                    None,
                )
                if unsafe_attempt is not None:
                    raise RewriteCheckpointRecoveryError(
                        "显式 Rewrite 检出 Reviewer 请求仍为未知或"
                        "成功响应晚于最新审稿账本、尚未消费: "
                        f"{chapter_id}/v{version_id}/"
                        f"attempt={unsafe_attempt.attempt_id}/"
                        f"status={unsafe_attempt.status}"
                    )
                if review_row is not None:
                    review = _parse_persisted_rewrite_review_row(
                        review_row,
                        chapter_id=chapter_id,
                        version_reviewed=version_id,
                    )
                    review_result = _restore_persisted_review_result(
                        session,
                        row=review_row,
                        parsed=review,
                    )
                    previous_row = rows_by_version.get(version_id - 1)
                    if (
                        review_result is not None
                        and version_id > 1
                        and source_row.creation_type in {"rewrite", "revise"}
                        and previous_row is not None
                    ):
                        review_result = _normalize_persisted_preexisting_regressions(
                            review=review_result,
                            previous_content=previous_row.content,
                            current_content=source_content,
                            workspace_id=paths.workspace_id,
                        )
                        review = review.model_copy(
                            update={
                                "failed_constraints": list(
                                    review_result.failed_constraints
                                )
                            }
                        )
        except RewriteCheckpointRecoveryError:
            _log.exception(
                "rewrite_checkpoint_integrity_failed",
                workspace_id=paths.workspace_id,
                chapter_id=chapter_id,
                version_id=version_id,
            )
            raise
        except Exception as exc:  # noqa: BLE001 — DB checkpoint 异常 fail-closed
            _log.exception(
                "rewrite_checkpoint_integrity_check_failed",
                workspace_id=paths.workspace_id,
                chapter_id=chapter_id,
                version_id=version_id,
            )
            raise RewriteCheckpointRecoveryError(
                f"无法校验显式 Rewrite checkpoint: {chapter_id}/v{version_id}"
            ) from exc

    word_count_policy_rebased = False
    card_path = paths.cards_dir / f"{chapter_id}.json"
    if review_result is not None and card_path.is_file():
        from src.core.rules.prose_length_policy import rebase_historical_length_review

        card = ChapterCard.model_validate_storage(json.loads(card_path.read_text(encoding="utf-8")))
        original_review = review_result
        review_result = rebase_historical_length_review(review_result, card)
        word_count_policy_rebased = review_result is not original_review
        if review is not None:
            review = review.model_copy(update={"failed_constraints": list(review_result.failed_constraints)})

    return RewriteCheckpointContext(
        version_id=version_id,
        source_content=source_content,
        source_style_fingerprint=compute_style_fingerprint(source_content),
        v1_style_fingerprint=compute_style_fingerprint(v1_content),
        review=review,
        review_result=review_result,
        word_count_policy_rebased=word_count_policy_rebased,
    )


def orchestrate_chapter(
    *,
    chapter_card: ChapterCard,
    paths: WorkspacePaths,
    memory_section: str,
    workspace_id: str,
    router: ModelRouter | None = None,
    client: LLMClient | None = None,
    model: str | None = None,
    initial_state: ChapterState = ChapterState.DRAFTING,
    initial_revise_count: int = 0,
    initial_rewrite_count: int = 0,
    on_state_transition: StateTransitionCallback | None = None,
    session_factory: PipelineSessionFactory | None = None,
    vector_store: VectorStore | None = None,
    world_contract: SettingContract | None = None,
    progression_consistency_enabled: bool = False,
    progression_label: str = "境界",
    skip_must_feasibility_precheck: bool = False,
    style_anchor_book: bool = False,
    anti_tic_trim_enabled: bool = False,
    word_count_trim_enabled: bool = False,
    initial_last_version_id: int | None = None,
    single_review_pass: bool = False,
    post_approval_memory_enabled: bool = True,
    rewrite_checkpoint_context: RewriteCheckpointContext | None = None,
    core_state_projection: CoreStateProjection | None = None,
    continuity_capsule: ProvenanceBoundContinuityCapsule | None = None,
) -> OrchestratorResult:
    """跑一次完整章节编排循环。

    :param chapter_card: 章节卡;``max_revises`` / ``max_rewrites`` 等预算
        字段从此读出,Orchestrator 不另外接参数。
    :param continuity_capsule: ChapterService 在首次 Provider 请求前从已验证
        ledger 构造的瞬态来源合同；缺失或不可证时为 None，各消费者渲染原
        完整 Prompt。它不持久化、不新增调用，也不触发短版失败 retry。
    :param paths: 工作区路径工具;Orchestrator 用其拼章节版本目录。
    :param memory_section: 文件式记忆段落,由调用方
        (``ChapterService``)调 ``build_memory_section(paths)`` 后传入;
        允许 ``""``。
    :param workspace_id: P3-B-14 加。当前操作工作区 ID,**仅供日志事件契约 +
        透传给 ``extract_memories`` 写库**;Orchestrator 业务逻辑(状态机 /
        agent 调度)不依赖此参数(对齐"按 workspace_id 开 session"工厂闭包成
        零参的设计)。``ChapterService._orchestrate`` 透传。该参数解耦
        ``chapter_card.workspace_id`` 字段(卡片是模板化资产,可能跨工作区复
        制时字段漂移),让 ``memory_extracted`` 事件 / 落库 ``workspace_id``
        字段始终反映真实操作上下文(P3-B-14 修 P3-acceptance 观察 1)。
    :param router: P3-B-06 主路径。``ModelRouter`` 实例;给定时所有 agent
        统一走 router,各自按 ``agent_routing`` / Writer 规则匹配命中具体
        profile + LLMClient + 生成参数。``None`` 时退化为 P1/P2 legacy
        路径(``client`` + ``model`` 必传)。
    :param client: legacy 路径 LLM 客户端;``router=None`` 时必传。
    :param model: legacy 路径模型名;``router=None`` 时必传。三个 agent
        共用同一模型(P1 不做模型路由,P3 起由 router 接管)。
    :param initial_state: 起始状态,默认 ``DRAFTING``;允许调用方传 REVIEWING
        等(对应"已有版本但需重新审"的恢复场景)。
    :param initial_revise_count: 已有的修订次数,默认 0(对应章节首次进入
        编排;``ChapterService`` 在重启续跑时按 ``Chapter.revise_count`` 注入)。
    :param initial_rewrite_count: 已有的重写次数,默认 0(同上,按
        ``Chapter.rewrite_count`` 注入)。
    :param on_state_transition: P1-B-20 加。每次成功 ``transition`` 后调一下
        ``on_state_transition(prev_state, next_state)``;``None`` 表示不推送。
        Orchestrator 不 import WebSocket manager,推送语义由调用方在闭包里
        实现(``ChapterService.write/rewrite`` 透传 → ``api.routes.chapters``
        闭包调 ``manager.publish``)。**回调内若抛异常**会冒泡到 Orchestrator
        主循环,导致 agent 已写出的版本丢失尾部 transition——回调实现方应
        自己 try/except 兜底(本任务路由闭包的 ``manager.publish`` 是 sync
        ``put_nowait``,不会抛)。
    :param session_factory: P2-B-19 加。零参 session 工厂(``ChapterService``
        把"按 workspace_id 开"包成零参再注入)。给定时 Writer/Reviewer 在每
        次调用打开短 session 拿 voice 段;APPROVED 后调 ``extract_memories``
        commit 增量记忆。``None`` → P1 兼容路径(无 voice / retriever / 抽取)。
    :param vector_store: P2-B-19 加。注入的 ``VectorStore`` 实例(已绑定
        workspace_id + embedding_fn + persist_dir)。给定时 Writer 调
        ``retrieve_memories_for_chapter`` 拿历史记忆 + APPROVED 后 extract 写
        回 Chroma。``None`` → P1 兼容路径。两参必须**同时**给定才启用 wiring;
        任一为 None 则退化为 P1 路径(避免半挂状态)。
    :param world_contract: v1.1 wave 6 session 3(B1)。世界设定契约
        (``SettingContract``,world 不变规则),由 ``ChapterService`` 从工作区
        ``outline/world.json`` 加载后透传;审稿期 setting_consistency_validator 用其
        (+ 出场角色 identity_facets)判定本章正文是否违反设定。``None`` → B1 自动
        skip(无契约 / 无 outline 工作区,fail-open 不阻塞)。
    :param progression_consistency_enabled: 模块②(v1.1 w5-s10)境界一致性。
        ``ChapterService`` 按工作区 style_template(∈ 3 题材)+ generation 配置解析后
        透传;``True`` 时 writer 注入"截止上一章{label}+单调不倒退"防漂移 + extract
        锚定增量抽进阶等级 + 注入↔写出闭环 WARN 校验。``False``(非 3 题材 / 开关
        off)→ writer / extract 进阶子管线全 no-op(逐字节零回归,gov §2.22)。
    :param progression_label: 进阶等级题材标签(仙侠/爽文=境界、战斗=战力);
        仅 ``progression_consistency_enabled=True`` 时用于 writer 注入 + extract 抽取。
    :param style_anchor_book: v2 w2-s2 注入端 A/B — True 时 writer prompt
        风格基线段的**段均/句均数字**换源为全书锚(ch1-3 final 指纹中位数,
        与 style_trend_validator 同锚同源,取自 preread 的
        ``style_trend_history``);主导人称判定 / 比喻密度仍来自前一章。
        锚窗口未就位(全书前 3 章)/ wiring 关 → 自动退化为前章锚现状行为。
        默认 False(零回归);由 ``ChapterService._orchestrate`` 解析工作区
        ``generation.style_anchor_source`` 配置透传。
    :param anti_tic_trim_enabled: v2 W3.5 wave-3 加。``True`` 时 writer 出稿后
        (draft + rewrite 两路)加一道 anti-tic trim pass(``trim_chapter_tics``,
        路由复用 writer profile)清两类 deepseek 固有 tic(「不是A是B」句式 + 演后
        盖章金句),清完的稿再走 review/validator 管线。trim 内三护栏(tic 密度 gate /
        截断回退 / 越界回退)任何触发回退原文。默认 False = **完全不调用**(零成本零
        延迟零风险,逐字节零回归);由 ``ChapterService._orchestrate`` 解析工作区
        ``generation.anti_tic_trim_enabled`` 配置透传。
    :param post_approval_memory_enabled: W3.5-C 写作水位拆分。默认 True 保持
        直接调用 orchestrator 的旧行为;``ChapterService`` 主链路传 False,
        让 approved 后先落 ``ready_for_next_chapter`` 硬水位,再后台跑摘要 /
        memory extractor / 向量等富加工。
    :param rewrite_checkpoint_context: ``ChapterService`` 在任何状态写入前冻结并
        校验过的显式 Rewrite 正文 / v1 风格 / 审稿 checkpoint。直接调用本
        函数时可留空，由本函数在 Writer 前读取。
    :return: ``OrchestratorResult``;``final_state ∈ {APPROVED, HUMAN_INTERVENTION}``。
    """
    # P3-B-06:router 与 legacy 路径二选一,二者皆缺即抛错。半挂(router 给定
    # 但 client/model 也给)以 router 为准,legacy 参数被忽略。
    if router is None and (client is None or model is None):
        raise TypeError(
            "orchestrate_chapter: 必须传入 router= 或 (client= + model=);二者均缺失"
        )

    # P2-B-19:wiring 启用条件——两个注入参数必须同时给定。半挂(只给一个)
    # 退化为 P1 路径,避免"Writer 注入了 vector_store 但 Reviewer 没 db_session"
    # 的偏态行为。
    if continuity_capsule is not None:
        try:
            continuity_capsule.assert_matches_projection(
                frozen_provenance=chapter_card.continuity_ledger_provenance,
                prev_chapter_tail=chapter_card.continuity_context.prev_chapter_tail,
                unfinished_actions=(
                    chapter_card.continuity_context.unfinished_actions
                ),
                pov_from=chapter_card.continuity_context.pov_from,
            )
        except ValueError as exc:
            _log.warning(
                "continuity_capsule_full_prompt_fallback",
                workspace_id=workspace_id,
                chapter_id=chapter_card.chapter_id,
                reason=str(exc),
                consumer="orchestrator",
            )
            continuity_capsule = None

    # 新实验按 ChapterCard 闭区间验收；旧管线保留历史容差。
    allow_word_count_tolerance = not (
        getattr(router, "pipeline_contract", None) == EXPERIMENTAL_PIPELINE_CONTRACT
        or (client is not None and uses_experimental_pipeline(client))
    )
    if chapter_card.word_count_policy_version != 0:
        word_count_trim_enabled = False
    wiring_enabled = session_factory is not None and vector_store is not None
    versioning = ChapterVersioning(paths)
    chapter_id = chapter_card.chapter_id

    # R9:角色引用/死亡状态属于 Writer 前 admission，不能继续绑定 vector_store
    # 是否可用。只要有 SQLite session，就先独立、fail-closed 读取角色权威数据；
    # 查询失败直接阻止正文请求。Timeline/风格观察仍沿用一般 preread fail-open。
    # P4-acceptance-followup(轮 89):一次性预读 Character / TimelineEntry,作为闭包变量
    # 透传给唯一稿 ``_apply_validator_pipeline`` 调用点。
    # 单章编排循环内预读一次足够(本工作区角色 / 时间线静态资产,章节内编排
    # 不会跨工作区 + 不会在循环中插入新行)。R9 起角色权威数据读取独立
    # fail-closed；失败直接阻止正文请求。只有 timeline / 风格等观察性预读继续
    # 沿用既有 fail-open 语义。
    pre_character_records: list[Character] | None = None
    pre_timeline_entries: list[TimelineEntry] | None = None
    pre_known_character_ids: set[str] | None = None
    # v1.1 wave 1 session 6 / G10:cross_chapter_voice_validator 预读 4 项 —
    # workspace.default_voice + 前一章 baseline fingerprint + baseline chapter_id +
    # is_first_chapter 判定。fail-open:wiring 关 / 查询失败 → 全默认值,validator
    # 自动 skip(passed=True)。
    cross_chapter_baseline_fp: dict[str, float] | None = None
    cross_chapter_baseline_chapter_id: str | None = None
    cross_chapter_workspace_default_voice: Literal[
        "third_person", "first_person", "free"
    ] = "third_person"
    # 全书首章用全局 chapter_id(ch-0001)判,不用卷内 chapter_number==1
    # (多卷下每卷开头卷内号=1 会被误当首章跳过跨章节 voice 校验;
    # v1.1 wave 4 B1 acceptance 修,与 continuity_validator 同型)。
    cross_chapter_is_first_chapter: bool = (
        chapter_seq_from_id(chapter_card.chapter_id) == 1
    )
    # v1.1 acceptance 修复2 / Fix D:写前 must 可行性预检的近章摘要上下文
    # (``(chapter_id, summary_text, unfinished_actions)``,近 N 章升序;v2 w2-s3
    # 补 unfinished_actions —— 摘要可能把"结局未知 / 表面收束"压成完成态,该字段是
    # 抽取端独立兜住的悬念信号,渲染进预检 prompt 收窄误判面)。同 preread session
    # 顺路查询;失败 swallow → 空列表(预检退化为仅靠 prev_chapter_card,仍可跑;
    # prev card 也无 → validator 自 skip)。
    pre_recent_summaries: list[tuple[str, str, list[str]]] = []
    # v2 w2-s2:跨章风格趋势检测历史(早于当前章的 approved 章 final 指纹
    # 升序)。None = wiring 关 / 查询失败 → pipeline 内趋势检测 skip。
    style_trend_history: list[tuple[str, dict[str, float] | None]] | None = None
    if session_factory is not None:
        try:
            with _maybe_session(session_factory) as admission_session:
                assert admission_session is not None
                characters = list_characters_by_workspace(
                    admission_session, workspace_id
                )
        except Exception as exc:
            _log.exception(
                "prewrite_admission_character_data_failed",
                workspace_id=workspace_id,
                chapter_id=chapter_id,
            )
            raise PrewriteAdmissionDataError(
                f"Writer 前无法读取角色权威数据: {workspace_id}/{chapter_id}"
            ) from exc
        # 查询成功后的空表是权威事实，不能与“未接持久层”混成 None 后跳过。
        # 空表 + 章节卡引用角色必须在 admission 失败，且 Writer 调用数保持 0。
        pre_character_records = characters
        pre_known_character_ids = {character.character_id for character in characters}

    if wiring_enabled:
        try:
            with _maybe_session(session_factory) as preread_session:
                assert preread_session is not None
                timeline = list_timeline_entries_by_workspace(
                    preread_session, workspace_id
                )
                # Fix D 预检上下文:近 N 章摘要(chapter_id zero-padded 字符串
                # 序即章节序,与 chapter_card_service.derive_context 同款用法)。
                summary_rows = (
                    preread_session.query(ChapterSummaryORM)
                    .filter(ChapterSummaryORM.workspace_id == workspace_id)
                    .filter(ChapterSummaryORM.chapter_id < chapter_id)
                    .order_by(ChapterSummaryORM.chapter_id.desc())
                    .limit(RECENT_SUMMARIES_FOR_PRECHECK)
                    .all()
                )
                pre_recent_summaries = [
                    (
                        row.chapter_id,
                        row.summary_text,
                        list(row.unfinished_actions or []),
                    )
                    for row in reversed(summary_rows)
                ]
                # 空 timeline → 视作未启用时间线管理；角色权威数据已由上方
                # 独立 admission 预读，不会因一般观察查询失败而被清空。
                pre_timeline_entries = timeline if timeline else None
                # v1.1 wave 1 session 6 / G10:cross_chapter context 同 session 预读。
                (
                    cross_chapter_baseline_fp,
                    cross_chapter_baseline_chapter_id,
                    cross_chapter_workspace_default_voice,
                ) = _resolve_cross_chapter_voice_context(
                    preread_session=preread_session,
                    workspace_id=workspace_id,
                    chapter_card=chapter_card,
                )
                # v2 w2-s2:趋势检测历史同 session 顺路预读(helper 内部自
                # swallow 返空列表;空列表≠None — 锚窗口不足时 validator 自 skip)。
                style_trend_history = _resolve_style_trend_history(
                    preread_session=preread_session,
                    workspace_id=workspace_id,
                    current_chapter_id=chapter_id,
                )
        except Exception:
            _log.exception(
                "validator_preread_failed",
                workspace_id=workspace_id,
                chapter_id=chapter_id,
            )
            pre_timeline_entries = None
            cross_chapter_baseline_fp = None
            cross_chapter_baseline_chapter_id = None
            cross_chapter_workspace_default_voice = "third_person"
            style_trend_history = None

    # WARN 路由账本与一般 validator preread 不同：来源必须是已批准 final，且
    # 任一 payload 损坏都要在 Provider 前硬停，不能被上方 fail-open 吞掉。
    writer_warning_watchlist_section = ""
    if session_factory is not None:
        with _maybe_session(session_factory) as warning_session:
            assert warning_session is not None
            writer_warning_watchlist_section = render_warning_watchlist(
                load_approved_final_warnings(
                    warning_session,
                    workspace_id=workspace_id,
                    target_chapter_id=chapter_id,
                    audience="writer",
                ),
                audience="writer",
                include_style_observations=not (chapter_card.narrative_context is not None
                    and chapter_card.narrative_context.story_intent.version >= 6),
            )

    # v2 w2-s2 债② 注入端闸门基线同步:ON 时构造 **effective 跨章基线**(段均/
    # 句均换全书锚,POV/代词/比喻/std 保前章),下游 writer prompt 锚 + 闸门
    # (cross_chapter_voice 段均 WARN + style_drift G17 异常判定参考)**全部统一用它**,
    # 杜绝"writer 锚全书、闸门判前章"两把尺打架。默认 OFF / 锚窗口未就位 /
    # wiring off → effective == 前章基线,全下游逐字节零回归(详见函数 docstring)。
    cross_chapter_anchor_fp, book_anchor_applied = (
        _resolve_effective_cross_chapter_baseline(
            cross_chapter_baseline_fp,
            style_trend_history,
            style_anchor_book=style_anchor_book,
        )
    )
    cross_chapter_voice_spec = resolve_cross_chapter_voice_constraint_spec(
        baseline_fingerprint=cross_chapter_anchor_fp,
        baseline_chapter_id=cross_chapter_baseline_chapter_id,
        workspace_default_voice=cross_chapter_workspace_default_voice,
        chapter_card=chapter_card,
        mini_plan_event=None,
    )

    state = initial_state
    revise_count = initial_revise_count
    rewrite_count = initial_rewrite_count
    versions: list[ChapterVersion] = []
    reviews: list[ReviewResult] = []
    last_version_id: int | None = None
    # v2 W3.5 UI 缺口3:re-review 入口(initial_state=REVIEWING + single_review_pass)
    # 直接对已落盘的人工改稿审一遍,不重写。seed last_version_id 让 REVIEWING 分支
    # 读到该版本正文；恢复时从同一份正文重算指纹，不因 versions 为空而漏输入。
    if initial_last_version_id is not None:
        last_version_id = initial_last_version_id
    escalation_reason: str | None = None
    # v2 W3.5-s7:version_id → 该版本是否被 word_count_trim 改写。落版本时记录,
    # _apply_validator_pipeline 调用时查 → style_drift 句均维度对 trim 版本降
    # warn-only(详 check_style_drift §2.47)。未记录的 version_id 默认 False
    # (revise 路径不走 word_count_trim)。
    wc_trimmed_by_version: dict[int, bool] = {}
    wc_trim_attempted_by_version: dict[int, bool] = {}
    wc_trim_attempts_by_version: dict[int, int] = {}
    # R9:每个实际正文版本绑定 Writer/Reviser 在 Provider 前看到的同一份计划。
    # 仅在本次 orchestration 内存中透传；恢复路径用同一机械编译器重建。
    constraint_plans_by_version: dict[int, ExecutableConstraintPlan] = {}
    # 当前调用内把每个修订版本绑定回冻结源正文，供写后区分“本轮新引入”与
    # “未改动旧证据被本轮 Judge 首次检出”。不持久化、不改变 Judge 结论。
    revision_contracts_by_version: dict[int, ProseRevisionContract] = {}
    escalation_details: dict[str, Any] | None = None
    # W3.5-B:version_id → 本次 rewrite 开始时构造的 hard gate 保序账本。
    # REVIEWING 阶段用它判断 rewrite 是否破坏上一轮已通过项。
    rewrite_guardrails_by_version: dict[int, RewriteConstraintGuardrail] = {}
    # R8 checkpoint recovery:UI :rewrite 从既有版本重新进入 Orchestrator 时，
    # ``versions`` 只包含本次新写出的版本。预先从产品 v1 正文 SSOT 重算一次
    # 基线，既供 Writer prompt 使用，也供随后 style_drift HARD gate 使用，
    # 避免恢复 invocation 因列表里没有 v1 而静默 fail-open。
    active_rewrite_checkpoint = rewrite_checkpoint_context
    if (
        initial_state == ChapterState.REWRITING
        and initial_last_version_id is not None
        and initial_last_version_id >= 1
        and active_rewrite_checkpoint is None
    ):
        active_rewrite_checkpoint = load_rewrite_checkpoint_context(
            session_factory,
            paths=paths,
            chapter_id=chapter_id,
            version_id=initial_last_version_id,
        )

    persisted_v1_style_fingerprint: dict[str, float] | None = (
        active_rewrite_checkpoint.v1_style_fingerprint
        if active_rewrite_checkpoint is not None
        else None
    )
    if (
        persisted_v1_style_fingerprint is None
        and initial_state != ChapterState.REWRITING
        and initial_last_version_id is not None
        and initial_last_version_id >= 1
    ):
        try:
            persisted_v1_style_fingerprint = compute_style_fingerprint(
                versioning.read_version_content(chapter_id, 1)
            )
        except (OSError, ChapterVersioningError):
            _log.warning(
                "checkpoint_v1_fingerprint_unavailable",
                workspace_id=workspace_id,
                chapter_id=chapter_id,
                initial_last_version_id=initial_last_version_id,
            )

    # R8 ch-0003:v7-v9 均由产品 UI 的显式 Rewrite 启动新 invocation，内存
    # ``reviews`` 因此为空。旧路径只重算 word_count，导致 v8 真正的
    # ``style_drift`` 与 8 条已通过 hard gates 在 v9 prompt 中全部丢失，并被
    # 错写成“Reviewer 自然 rewrite + 扩到字数下限”。这里在任何 Provider
    # 调用前读取最新持久化 review checkpoint；明细仍由下方对 SSOT 正文重算。
    persisted_rewrite_review_context = (
        active_rewrite_checkpoint.review
        if active_rewrite_checkpoint is not None
        else None
    )
    pending_resume_review = (
        active_rewrite_checkpoint.review_result
        if (
            initial_state == ChapterState.REVIEWING
            and active_rewrite_checkpoint is not None
            and active_rewrite_checkpoint.version_id == initial_last_version_id
            # 正式采用新篇幅策略后，旧综合判决不能直接续用或改成 pass；
            # 复审同一源正文，保留旧判决，不先因旧字数失败调用 Writer。
            and not active_rewrite_checkpoint.word_count_policy_rebased
        )
        else None
    )

    def _source_review_for_revision(
        contract: ProseRevisionContract | None,
    ) -> ReviewResult | None:
        """按冻结 source version 取同 invocation 或显式 checkpoint 审稿。"""

        if contract is None:
            return None
        candidates = [
            reviews[-1] if reviews else None,
            (
                active_rewrite_checkpoint.review_result
                if active_rewrite_checkpoint is not None
                else None
            ),
        ]
        return next(
            (
                candidate
                for candidate in candidates
                if candidate is not None
                and candidate.version_reviewed == contract.source_version_id
            ),
            None,
        )

    def _fire(prev: ChapterState, nxt: ChapterState) -> None:
        """触发 ``on_state_transition`` hook + 落 ``state_transition`` 日志。

        P1-B-20 加 hook(WS 推送);P1-B-24 加 ``state_transition`` structlog
        事件(OBSERVABILITY §2.4)。同状态(``prev == nxt``)不触发——理论上
        ``transition`` 不会返回原状态,这里仅作保险。

        日志先落、再触发 hook:hook 内部若抛(P1-B-20 文档说回调实现方该
        自己 try/except),日志已经落了,故障定位有线索。
        """
        if prev == nxt:
            return
        _log.info(
            "state_transition",
            from_state=prev.value,
            to_state=nxt.value,
        )
        if on_state_transition is not None:
            on_state_transition(prev, nxt)

    def _chapter_timer(stage: str, **fields: Any) -> StageTimer:
        return StageTimer(
            _log,
            "chapter_stage_timing",
            {
                "workspace_id": workspace_id,
                "chapter_id": chapter_id,
                "stage": stage,
                "state": state.value,
                "revise_count": revise_count,
                "rewrite_count": rewrite_count,
                **fields,
            },
        )

    def _recompute_checkpoint_deterministic_failures(
        *,
        source_content: str,
        current_version_id: int,
        persisted_failure_names: set[str],
        current_fingerprint: dict[str, float] | None = None,
    ) -> list[dict[str, Any]]:
        """从正文 SSOT 恢复未持久化的 word-count/style 失败明细。"""

        failures: list[dict[str, Any]] = []
        word_count_result = check_word_count(
            source_content,
            word_count_min=chapter_card.word_count_min,
            word_count_max=chapter_card.word_count_max,
            allow_word_count_tolerance=allow_word_count_tolerance,
        )
        if chapter_card.word_count_policy_version == 0 and not word_count_result.passed:
            failures.append(
                {
                    "name": word_count_result.name,
                    "details": dict(word_count_result.details),
                }
            )
        if (
            "style_drift" in persisted_failure_names
            and persisted_v1_style_fingerprint is not None
        ):
            style_result = check_style_drift(
                baseline_fingerprint=persisted_v1_style_fingerprint,
                current_fingerprint=(
                    current_fingerprint or compute_style_fingerprint(source_content)
                ),
                baseline_version_id=1,
                current_version_id=current_version_id,
                cross_chapter_baseline_fingerprint=cross_chapter_anchor_fp,
                cross_chapter_baseline_chapter_id=(
                    cross_chapter_baseline_chapter_id
                ),
            )
            if not style_result.passed:
                failures.append(
                    {
                        "name": style_result.name,
                        "details": dict(style_result.details),
                    }
                )
        return failures

    # P2-B-19:Writer / Reviewer 注入闭包 — 给定 wiring 时透传 db_session +
    # vector_store;否则字面 None(等价 P1)。封装成内部 helper 让三处 Writer/
    # Reviewer 调用点字面一致。
    # P3-B-06:router 给定时透传 router=router,legacy client/model 被忽略;
    # 否则 fallback 到 client + model legacy 路径。
    # v1.1 wave 1 session 7 / G11(同 session 顺手补 G10 A 层透传):
    # - G10 补:closure 内部基于 ``cross_chapter_baseline_fp`` +
    #   ``cross_chapter_workspace_default_voice`` 算 ``PovSummary``,作为
    #   ``prev_chapter_voice_summary`` 透传给 writer。**G10 session 漏接通**
    #   (docstring 字面写"orchestrator 透传"但 _call_writer 实际未透传)
    #   → G11 同 _call_writer 改动一并补接;首章 / fingerprint None / wiring
    #   off → 仍传 None,writer 自跳过该段。
    # - G11 新:``creation_type='rewrite'`` 时,从 closure 内 ``versions[0]``
    #   拿 v1 ``style_fingerprint`` + 从 ``reviews[-1]`` 拿
    #   ``validator_failure_details``,透传给 writer 让 user message 附"##
    #   rewrite mode 字数收敛指导" 段。draft 路径透传 ``creation_type='draft'``
    #   或 None,该段不附。
    def _resolve_writer_prompt_context(
        *,
        creation_type: Literal["draft", "rewrite", "revise"] | None = None,
        rewrite_constraint_guardrail: RewriteConstraintGuardrail | None = None,
    ) -> _WriterPromptContext:
        """冻结一次 Writer prompt 上下文，供单稿或并行候选共同使用。"""
        # G10 补:算前一章 voice summary;基于 wave 6 s6 已预读的
        # cross_chapter_anchor_fp(v2 w2-s2 债② 起 = effective 基线:OFF 时
        # 等于前章基线,ON 时段均/句均换全书锚)+ cross_chapter_workspace_default_voice。
        # 首章 / wiring off / 查询失败 → cross_chapter_anchor_fp=None → 传 None
        # (writer 自跳过 "## 跨章节视角 / 风格基线" 段)。POV 判定从 anchor_fp
        # 的代词频次算(effective 保前章代词 → POV 仍是前章,语义不变)。
        prev_voice: PovSummary | None = None
        if cross_chapter_anchor_fp is not None:
            prev_voice = summarize_voice_from_fingerprint(
                cross_chapter_anchor_fp,
                default_voice=cross_chapter_workspace_default_voice,
            )
            # ON 且锚窗口就位 → 标记 book_anchor,writer ``_format_voice_baseline``
            # 据此渲染 "BOOK VOICE ANCHOR" 段名 + 诚实标注数字来源。
            if book_anchor_applied:
                prev_voice = prev_voice.model_copy(
                    update={"anchor_source": "book_anchor"}
                )

        # G11:rewrite 路径下,从 closure 内 versions / reviews 算上下文。
        # - prev_version_fp 取 versions[0](本章 v1,即首版)的 fingerprint;
        #   v1 落盘时 ChapterVersioning.write_version 自动算并写入 fingerprint
        #   字段(G5 已建);理论上 rewrite 路径 versions 不空 + v1 fingerprint
        #   非 None(空文本兜底 _empty_fingerprint 也是 dict[str, 0.0])。
        # - validator_failures 取 reviews[-1] 的 validator_failure_details;
        #   reviews 不空(rewrite 必然由前一次 review 触发);若 reviewer 自然
        #   verdict=rewrite 不附 details → None 透传,writer 段渲染"无"。
        prev_version_fp: dict[str, float] | None = None
        validator_failures_payload: list[dict[str, Any]] | None = None
        paragraph_reflow_failures: list[dict[str, Any]] | None = None
        mechanical_reflow_only = False
        defer_word_count_trim = False
        if creation_type == "rewrite":
            if (
                not reviews
                and active_rewrite_checkpoint is not None
                and last_version_id == active_rewrite_checkpoint.version_id
            ):
                refreshed_checkpoint = load_rewrite_checkpoint_context(
                    session_factory,
                    paths=paths,
                    chapter_id=chapter_id,
                    version_id=active_rewrite_checkpoint.version_id,
                )
                if active_rewrite_checkpoint.review_contract_rebase_reason is not None:
                    # rewind 后章节卡可能已由正式 Card Review 流程重生。旧正文和
                    # 风格指纹仍是不可变 source checkpoint，但旧 ReviewResult 的
                    # constraint_id 属于旧卡合同，不能拿来校验或指导新卡重写。
                    # 刷新比较仍覆盖 source/version/fingerprint；只把明确废止的
                    # 旧审稿投影成与入口相同的 contract-rebase 视图。
                    refreshed_checkpoint = refreshed_checkpoint.model_copy(
                        update={
                            "review": None,
                            "review_result": None,
                            "review_contract_rebase_reason": (
                                active_rewrite_checkpoint.review_contract_rebase_reason
                            ),
                        }
                    )
                if refreshed_checkpoint != active_rewrite_checkpoint:
                    _log.error(
                        "rewrite_checkpoint_changed_before_writer",
                        workspace_id=workspace_id,
                        chapter_id=chapter_id,
                        version_id=active_rewrite_checkpoint.version_id,
                    )
                    raise RewriteCheckpointRecoveryError(
                        "显式 Rewrite checkpoint 在 Writer 前发生变化，已阻止请求: "
                        f"{chapter_id}/v{active_rewrite_checkpoint.version_id}"
                    )
                if (
                    rewrite_constraint_guardrail is None
                    and active_rewrite_checkpoint.review_contract_rebase_reason
                    is not None
                ):
                    rewrite_constraint_guardrail = RewriteConstraintGuardrail(
                        other_failures=[
                            "product_rewind_contract_rebase:"
                            + active_rewrite_checkpoint.review_contract_rebase_reason
                        ]
                    )
            invocation_v1 = next(
                (version for version in versions if version.version_id == 1),
                None,
            )
            prev_version_fp = (
                persisted_v1_style_fingerprint
                if persisted_v1_style_fingerprint is not None
                else (
                    invocation_v1.style_fingerprint
                    if invocation_v1 is not None
                    else None
                )
            )
            if reviews:
                validator_failures_payload = reviews[-1].validator_failure_details
                if (
                    validator_failures_payload is None
                    and last_version_id is not None
                    and active_rewrite_checkpoint is not None
                    and active_rewrite_checkpoint.version_id == last_version_id
                ):
                    # review_results 不持久化 validator_failure_details。Pipeline
                    # 从 REVIEWING checkpoint 续判后 reviews 已非空，仍必须从
                    # 同一正文 SSOT 重算，否则纯代码 reflow 会被误退回 Writer。
                    validator_failures_payload = (
                        _recompute_checkpoint_deterministic_failures(
                            source_content=(
                                active_rewrite_checkpoint.source_content
                            ),
                            current_version_id=last_version_id,
                            persisted_failure_names=set(
                                reviews[-1].failed_constraints
                            ),
                            current_fingerprint=(
                                active_rewrite_checkpoint.source_style_fingerprint
                            ),
                        )
                        or None
                    )
                if rewrite_constraint_guardrail is None:
                    rewrite_constraint_guardrail = _build_rewrite_constraint_guardrail(
                        review=reviews[-1],
                        chapter_card=chapter_card,
                    )
            elif last_version_id is not None:
                # ``validator_failure_details`` 目前不进 review_results ORM。
                # 对 checkpoint 的最新正文重算零成本确定性事实。word-count
                # 始终可重算；style-drift 仅在持久化 review 明确记录它失败时
                # 重算，既恢复原反馈，又避免缺少 ``word_count_trimmed`` 历史
                # 标记时凭空制造新的 style 失败。
                source_content = (
                    active_rewrite_checkpoint.source_content
                    if (
                        active_rewrite_checkpoint is not None
                        and active_rewrite_checkpoint.version_id == last_version_id
                    )
                    else versioning.read_version_content(chapter_id, last_version_id)
                )
                persisted_failure_names = set(
                    persisted_rewrite_review_context.failed_constraints
                    if persisted_rewrite_review_context is not None
                    else []
                )
                recomputed_failures = (
                    _recompute_checkpoint_deterministic_failures(
                        source_content=source_content,
                        current_version_id=last_version_id,
                        persisted_failure_names=persisted_failure_names,
                        current_fingerprint=(
                            active_rewrite_checkpoint.source_style_fingerprint
                            if (
                                active_rewrite_checkpoint is not None
                                and active_rewrite_checkpoint.version_id
                                == last_version_id
                            )
                            else None
                        ),
                    )
                )

                validator_failures_payload = recomputed_failures or None

                if (
                    rewrite_constraint_guardrail is None
                    and persisted_rewrite_review_context is not None
                ):
                    # word_count / style_drift 以本次对同一正文的重算为准：这也
                    # 自动淘汰修复前因边界舍入留下的陈旧 style_drift 标签。
                    deterministic_names = {"word_count", "style_drift"}
                    other_failures = [
                        name
                        for name in persisted_rewrite_review_context.failed_constraints
                        if name not in deterministic_names
                    ]
                    other_failures.extend(
                        str(item.get("name") or "<unknown>")
                        for item in recomputed_failures
                    )
                    other_failures.extend(
                        _format_persisted_constraint_violation(item)
                        for item in (
                            persisted_rewrite_review_context.constraint_violations
                        )
                    )
                    other_failures = list(dict.fromkeys(other_failures))
                    if not other_failures:
                        if persisted_rewrite_review_context.verdict == "pass":
                            other_failures.append(
                                "manual_product_rewrite_after_persisted_pass"
                            )
                        else:
                            other_failures.append(
                                "persisted reviewer verdict="
                                f"{persisted_rewrite_review_context.verdict}, "
                                "score_overall="
                                f"{persisted_rewrite_review_context.score_overall:.1f}"
                            )
                    rewrite_constraint_guardrail = RewriteConstraintGuardrail(
                        must_fix=[
                            result
                            for result in (
                                persisted_rewrite_review_context.constraint_results
                            )
                            if result.status == "fail"
                        ],
                        must_preserve=[
                            result
                            for result in (
                                persisted_rewrite_review_context.constraint_results
                            )
                            if result.status == "pass"
                        ],
                        constraint_violations=list(
                            persisted_rewrite_review_context.constraint_violations
                        ),
                        other_failures=other_failures,
                    )

        # v1.1 audit G17 治本(2026-05-27 b1-rerun-deep-audit):rewrite 路径下
        # 透传前一章 final approved baseline,用于 writer._format_rewrite_baseline
        # 内 v1 异常检测 + WARN 降级。draft 路径 / 首章 / cross_chapter_anchor_fp
        # None → 仍传 None,writer 段保持原 v1 锚定行为。
        # v2 w2-s2 债②:用 effective 基线(ON 时全书锚)→ G17 rewrite 锚定与
        # 闸门 cross_chapter_voice / style_drift 同源,ON 语义下全链路一致。
        prev_chapter_baseline_fp: dict[str, float] | None = None
        if creation_type == "rewrite":
            prev_chapter_baseline_fp = cross_chapter_anchor_fp

        stage: ProseConstraintStage = creation_type or "draft"
        executable_constraint_plan = compile_executable_constraint_plan(
            chapter_card=chapter_card,
            stage=stage,
            setting_contract=world_contract,
            character_records=pre_character_records,
            cross_chapter_voice=cross_chapter_voice_spec,
            rewrite_style=(
                resolve_rewrite_style_constraint_spec(
                    v1_fingerprint=prev_version_fp,
                    cross_chapter_baseline_fingerprint=(prev_chapter_baseline_fp),
                )
                if stage in {"rewrite", "revise"}
                else None
            ),
            core_state_projection=core_state_projection,
        )
        executable_constraint_plan.assert_matches_core_state(
            chapter_card=chapter_card,
            projection=core_state_projection,
        )
        if (
            creation_type == "rewrite"
            and active_rewrite_checkpoint is not None
            and active_rewrite_checkpoint.review_result is not None
        ):
            executable_constraint_plan.assert_review_uses_only_planned_hard(
                active_rewrite_checkpoint.review_result
            )

        revision_contract: ProseRevisionContract | None = None
        if creation_type == "rewrite":
            if last_version_id is None:
                # P1/文件系统兼容入口允许把“首版 rewrite”当作无源生成；生产
                # DB 路径必须有 checkpoint/source version，绝不静默绕过合同。
                if session_factory is not None:
                    raise RewriteCheckpointRecoveryError(
                        "Writer rewrite 缺少不可变源版本，已阻止请求"
                    )
            else:
                source_content = (
                    active_rewrite_checkpoint.source_content
                    if (
                        active_rewrite_checkpoint is not None
                        and active_rewrite_checkpoint.version_id == last_version_id
                    )
                    else versioning.read_version_content(chapter_id, last_version_id)
                )
                effective_guardrail = (
                    rewrite_constraint_guardrail or RewriteConstraintGuardrail()
                )
                (
                    validator_failures_payload,
                    effective_guardrail,
                    paragraph_reflow_failures,
                ) = _partition_mechanical_paragraph_reflow(
                    source_content=source_content,
                    validator_failures=validator_failures_payload,
                    guardrail=effective_guardrail,
                    executable_constraint_plan=executable_constraint_plan,
                )
                (
                    validator_failures_payload,
                    effective_guardrail,
                    defer_word_count_trim,
                ) = _partition_mixed_above_max_rewrite(
                    source_content=source_content,
                    validator_failures=validator_failures_payload,
                    guardrail=effective_guardrail,
                    executable_constraint_plan=executable_constraint_plan,
                    allow_word_count_tolerance=allow_word_count_tolerance,
                )
                rewrite_constraint_guardrail = effective_guardrail
                mechanical_reflow_only = _is_mechanical_reflow_only(
                    writer_failures=validator_failures_payload,
                    guardrail=effective_guardrail,
                    reflow_failures=paragraph_reflow_failures,
                )
                revision_contract = build_rewrite_revision_contract(
                    source_content=source_content,
                    source_version_id=last_version_id,
                    executable_constraint_plan=executable_constraint_plan,
                    must_fix=effective_guardrail.must_fix,
                    must_preserve=effective_guardrail.must_preserve,
                    constraint_violations=(
                        effective_guardrail.constraint_violations
                    ),
                    other_failures=_revision_contract_other_failures(
                        effective_guardrail
                    ),
                    validator_failures=(
                        (paragraph_reflow_failures or ())
                        if mechanical_reflow_only
                        else (validator_failures_payload or ())
                    ),
                )

        return _WriterPromptContext(
            prev_chapter_voice_summary=prev_voice,
            prev_version_fingerprint=prev_version_fp,
            validator_failures=validator_failures_payload,
            paragraph_reflow_failures=paragraph_reflow_failures,
            mechanical_reflow_only=mechanical_reflow_only,
            defer_word_count_trim=defer_word_count_trim,
            rewrite_constraint_guardrail=rewrite_constraint_guardrail,
            prev_chapter_baseline_fingerprint=prev_chapter_baseline_fp,
            warning_watchlist_section=writer_warning_watchlist_section,
            executable_constraint_plan=executable_constraint_plan,
            revision_contract=revision_contract,
        )

    def _call_writer(
        *,
        creation_type: Literal["draft", "rewrite", "revise"] | None = None,
        rewrite_constraint_guardrail: RewriteConstraintGuardrail | None = None,
        prompt_context: _WriterPromptContext | None = None,
    ) -> WriterOutput:
        if prompt_context is None:
            prompt_context = _resolve_writer_prompt_context(
                creation_type=creation_type,
                rewrite_constraint_guardrail=rewrite_constraint_guardrail,
            )
        if prompt_context.mechanical_reflow_only:
            if prompt_context.revision_contract is None:
                raise RewriteCheckpointRecoveryError(
                    "纯代码段均修复缺少冻结源合同，已阻止写入"
                )
            writer_out = _build_mechanical_reflow_writer_output(
                revision_contract=prompt_context.revision_contract,
                executable_constraint_plan=prompt_context.executable_constraint_plan,
                validator_failures=prompt_context.paragraph_reflow_failures,
            )
            _log.info(
                "paragraph_mean_mechanical_reflow_provider_skipped",
                workspace_id=workspace_id,
                chapter_id=chapter_id,
                source_version_id=prompt_context.revision_contract.source_version_id,
                source_sha256=prompt_context.revision_contract.source_sha256,
            )
            return writer_out

        def _invoke_writer(context: _WriterPromptContext) -> WriterOutput:
            with _maybe_session(session_factory if wiring_enabled else None) as s:
                return write_chapter(
                    router=router,
                    client=client,
                    model=model,
                    chapter_card=chapter_card,
                    memory_section=memory_section,
                    setting_contract=world_contract,
                    db_session=s,
                    workspace_paths=paths,
                    vector_store=vector_store if wiring_enabled else None,
                    workspace_id=workspace_id,
                    prev_chapter_voice_summary=(
                        context.prev_chapter_voice_summary
                    ),
                    creation_type=creation_type,
                    prev_version_fingerprint=context.prev_version_fingerprint,
                    validator_failures=context.validator_failures,
                    rewrite_constraint_guardrail=(
                        context.rewrite_constraint_guardrail
                    ),
                    prev_chapter_baseline_fingerprint=(
                        context.prev_chapter_baseline_fingerprint
                    ),
                    warning_watchlist_section=context.warning_watchlist_section,
                    progression_consistency_enabled=progression_consistency_enabled,
                    progression_label=progression_label,
                    executable_constraint_plan=context.executable_constraint_plan,
                    core_state_projection=core_state_projection,
                    revision_contract=context.revision_contract,
                    continuity_capsule=continuity_capsule,
                )

        try:
            writer_out = _invoke_writer(prompt_context)
        except ProseRevisionNoProgressError:
            contract = prompt_context.revision_contract
            if creation_type != "rewrite" or contract is None or contract.mode != "global":
                raise
            current_guardrail = (
                prompt_context.rewrite_constraint_guardrail
                or RewriteConstraintGuardrail()
            )
            retry_guardrail = current_guardrail.model_copy(
                update={
                    "other_failures": [
                        *current_guardrail.other_failures,
                        GLOBAL_NO_PROGRESS_FEEDBACK,
                    ]
                }
            )
            retry_context = _resolve_writer_prompt_context(
                creation_type="rewrite",
                rewrite_constraint_guardrail=retry_guardrail,
            )
            retry_contract = retry_context.revision_contract
            if (
                retry_contract is None
                or retry_contract.mode != "global"
                or retry_contract.source_version_id != contract.source_version_id
                or retry_contract.source_sha256 != contract.source_sha256
                or retry_contract.plan_hash != contract.plan_hash
            ):
                raise RewriteCheckpointRecoveryError(
                    "GLOBAL no-progress 自适应重试的源版本或计划发生漂移，已阻止请求"
                )
            _log.warning(
                "global_rewrite_no_progress_retry",
                workspace_id=workspace_id,
                chapter_id=chapter_id,
                source_version_id=contract.source_version_id,
                source_sha256=contract.source_sha256,
                plan_hash=contract.plan_hash,
                retry_limit=GLOBAL_REWRITE_NO_PROGRESS_MAX_RETRIES,
            )
            prompt_context = retry_context
            # 同一个 Writer、同一冻结源与计划只做一次自适应重试；若仍无变化，
            # ``ProseRevisionNoProgressError`` 原样冒泡，且全程没有新版本/Reviewer。
            writer_out = _invoke_writer(prompt_context)

        # v2 W3.5 wave-3:anti-tic trim gated pass —— writer 出稿后清两类 deepseek 固有
        # tic(「不是A是B」句式 + 演后盖章金句),清完的稿再走 review/validator 管线
        # (出货前过质量门审的就是 trim 后内容)。draft + rewrite 两路都经本闭包 → 两路出稿
        # 都被 trim。**开关关 = 完全不调用**(零成本零延迟零风险,行为与本功能前逐字节等价)。
        # trim 内三护栏(tic 密度 gate / 截断回退 / 越界回退)任何触发都回退原文,
        # 绝不交残章 / 改坏干净章 / 阻断生成(零 fail-open)。session 已关闭再调,
        # 不在 trim LLM 调用期间持锁。
        revision_contract = writer_out.revision_contract
        localized_revision = (
            revision_contract is not None and revision_contract.mode == "localized"
        )
        if localized_revision and (anti_tic_trim_enabled or word_count_trim_enabled):
            assert revision_contract is not None
            _log.info(
                "localized_revision_unbounded_postprocessor_skipped",
                workspace_id=workspace_id,
                chapter_id=chapter_id,
                source_version_id=revision_contract.source_version_id,
                anti_tic_trim_enabled=anti_tic_trim_enabled,
                word_count_trim_enabled=word_count_trim_enabled,
            )
        if anti_tic_trim_enabled and not localized_revision:
            trim_out = trim_chapter_tics(
                content=writer_out.content,
                router=router,
                client=client,
                model=model,
                chapter_id=chapter_id,
            )
            if trim_out.trimmed:
                writer_out = writer_out.model_copy(update={"content": trim_out.content})

        # v2 W3.5-s3rev:word_count trim gated pass —— anti-tic 之后,若正文仍超
        # ``word_count_max``,看着真正文 trim 精简到 ≤上限(bounded-retry + 代码量字数校验,
        # 详 ``word_count_trim`` 模块 + docs/acceptance/v2_w3_5_s3rev_length_ab_findings.md)。
        # 压平后 review 看到的就是 ≤cap 稿 → 根本不进 rewrite 循环。**实验钉死 trim 胜盲 rewrite
        # (对调盲读 2:0,保留好剧情、不加回过场、不爆长)**。零 fail-open:不收敛 / 异常回退原稿,
        # 交既有 rewrite 循环兜底(= 功能关时等价行为)。开关关 / 没超长 = 完全不调用(零成本零延迟)。
        if (
            word_count_trim_enabled
            and not localized_revision
            and not prompt_context.defer_word_count_trim
        ):
            wc_out = trim_to_word_count(
                content=writer_out.content,
                chapter_card=chapter_card,
                router=router,
                client=client,
                model=model,
                chapter_id=chapter_id,
                soft_floor=chapter_card.word_count_min,
            )
            writer_out = writer_out.model_copy(
                update={
                    "word_count_trim_attempted": wc_out.attempts > 0,
                    "word_count_trim_attempts": wc_out.attempts,
                }
            )
            if wc_out.trimmed:
                # v2 W3.5-s7:标记本稿被 word_count_trim 改写 → 下游 style_drift
                # 句均维度降 warn-only(trim 重写句长是 by-design,§2.47)。
                writer_out = writer_out.model_copy(
                    update={"content": wc_out.content, "word_count_trimmed": True}
                )
        if creation_type == "rewrite":
            reflowed = reflow_paragraph_mean_only(
                content=writer_out.content,
                validator_failures=prompt_context.paragraph_reflow_failures,
            )
            if reflowed.applied:
                writer_out = writer_out.model_copy(update={"content": reflowed.content})
                _log.info(
                    "paragraph_mean_mechanical_reflow_applied",
                    workspace_id=workspace_id,
                    chapter_id=chapter_id,
                    before_mean=reflowed.before_mean,
                    after_mean=reflowed.after_mean,
                    upper_bound=reflowed.upper_bound,
                )
        return writer_out

    def _can_route_budget_neutral_paragraph_reflow(
        *,
        review: ReviewResult,
        source_content: str,
        current_version_id: int,
        executable_constraint_plan: ExecutableConstraintPlan,
        current_fingerprint: dict[str, float] | None = None,
    ) -> bool:
        """仅当 Reviewer 本身已通过且只剩可机械段均失败时绕过内容预算。"""

        if (
            review.verdict != "rewrite"
            or review.score_overall < chapter_card.review_threshold
        ):
            return False
        validator_failures = review.validator_failure_details
        if validator_failures is None:
            validator_failures = (
                _recompute_checkpoint_deterministic_failures(
                    source_content=source_content,
                    current_version_id=current_version_id,
                    persisted_failure_names=set(review.failed_constraints),
                    current_fingerprint=current_fingerprint,
                )
                or None
            )
        guardrail = _build_rewrite_constraint_guardrail(
            review=review,
            chapter_card=chapter_card,
        )
        writer_failures, effective_guardrail, reflow_failures = (
            _partition_mechanical_paragraph_reflow(
                source_content=source_content,
                validator_failures=validator_failures,
                guardrail=guardrail,
                executable_constraint_plan=executable_constraint_plan,
            )
        )
        eligible = _is_mechanical_reflow_only(
            writer_failures=writer_failures,
            guardrail=effective_guardrail,
            reflow_failures=reflow_failures,
        )
        if eligible:
            _log.info(
                "paragraph_mean_budget_neutral_recovery_eligible",
                workspace_id=workspace_id,
                chapter_id=chapter_id,
                source_version_id=current_version_id,
                rewrite_count=rewrite_count,
                max_rewrites=chapter_card.max_rewrites,
            )
        return eligible

    def _resolve_constraint_plan_for_review(
        version_id: int,
    ) -> ExecutableConstraintPlan:
        """取本次版本计划；旧 checkpoint 则用同一权威输入零成本重建。"""

        existing = constraint_plans_by_version.get(version_id)
        if existing is not None:
            return existing
        recovered_version = next(
            (version for version in versions if version.version_id == version_id),
            None,
        )
        recovered_creation_type: str | None = (
            recovered_version.creation_type if recovered_version is not None else None
        )
        if recovered_creation_type is None and session_factory is not None:
            with _maybe_session(session_factory) as recovery_session:
                assert recovery_session is not None
                recovered_row = recovery_session.get(
                    ChapterVersionORM, (chapter_id, version_id)
                )
                if recovered_row is None:
                    raise ExecutableConstraintPlanError(
                        "恢复版本缺少 ChapterVersion 账本，无法重建约束计划"
                    )
                recovered_creation_type = recovered_row.creation_type
        if recovered_creation_type == "draft":
            recovered_stage: ProseConstraintStage = "draft"
        elif recovered_creation_type == "rewrite":
            recovered_stage = "rewrite"
        elif recovered_creation_type in {"revise", "manual_edit"}:
            recovered_stage = "revise"
        elif recovered_creation_type is None and session_factory is None:
            # 无持久层的 legacy 直调没有可核对账本，只能沿用历史约定：v1=draft，
            # 后续版本按 revise 审查。产品路径绝不走这条降级。
            recovered_stage = "draft" if version_id == 1 else "revise"
        else:
            raise ExecutableConstraintPlanError(
                "恢复版本 creation_type 非法，拒绝猜测约束计划阶段: "
                f"version_id={version_id},creation_type={recovered_creation_type!r}"
            )
        invocation_v1 = next(
            (version for version in versions if version.version_id == 1),
            None,
        )
        v1_fingerprint = (
            invocation_v1.style_fingerprint
            if invocation_v1 is not None
            else persisted_v1_style_fingerprint
        )
        recovered = compile_executable_constraint_plan(
            chapter_card=chapter_card,
            stage=recovered_stage,
            setting_contract=world_contract,
            character_records=pre_character_records,
            cross_chapter_voice=cross_chapter_voice_spec,
            rewrite_style=resolve_rewrite_style_constraint_spec(
                v1_fingerprint=v1_fingerprint,
                cross_chapter_baseline_fingerprint=cross_chapter_anchor_fp,
            ),
            core_state_projection=core_state_projection,
        )
        constraint_plans_by_version[version_id] = recovered
        return recovered

    # v1.1 w3-s7 E5:上一章卡 SSOT(``ch-{N-1}.json``)在 REVIEWING 分支若逐轮
    # 重读磁盘会重复做 Pydantic 解析。本章生成期间上一章
    # 已 approved 不会变,故在 while 循环 + 闭包定义前 **per-orchestration 加载一次**,
    # 两处调用点复用该对象,消除冗余磁盘读 + 解析。
    # 不用全局 ``functools.lru_cache``:跨 orchestration 上一章卡可能被手动编辑 →
    # stale 数据进 Q4 continuity_validator(plan §4 line 178 标注风险);
    # per-orchestration 闭包捕获天然零 stale 风险。
    _prev_chapter_card = _load_prev_chapter_card_safe(
        paths=paths, chapter_id=chapter_card.chapter_id
    )

    # ===== R9:Writer 无法修复的章节卡/角色数据缺陷写前 admission =====
    # character_existence 与旧称 death_foreshadowing 的检查对象都只是章节卡 / DB，
    # 放在写后会让正文模型为数据问题反复重写。全新起草、显式 Rewrite 与 Reviser
    # 入口统一先查；命中直接升人工，且不生成版本、不增加 rewrite/revise count。
    if initial_state in {
        ChapterState.DRAFTING,
        ChapterState.REWRITING,
        ChapterState.REVISING,
    }:
        admission_failures = _run_prewrite_card_admission(
            chapter_card=chapter_card,
            known_character_ids=pre_known_character_ids,
            character_records=pre_character_records,
        )
        if admission_failures:
            escalation_reason = "chapter_card_admission_failed"
            escalation_details = {
                "checked_at_state": "pre_write",
                "summary": (
                    "章节卡或角色数据存在正文模型无法修复的硬缺陷；已在任何付费"
                    "写作调用前停止，请先修章节卡/角色状态。"
                ),
                "findings": [
                    {"name": item.name, "details": dict(item.details)}
                    for item in admission_failures
                ],
            }
            _log.warning(
                "chapter_card_admission_failed",
                workspace_id=workspace_id,
                chapter_id=chapter_id,
                failures=[item.name for item in admission_failures],
                details=[dict(item.details) for item in admission_failures],
            )
            prev_state = state
            state = transition(state, "force_escalate", force=True)
            _fire(prev_state, state)

    # ===== v1.1 acceptance 修复2 / Fix D:写前 must 可行性预检 =====
    # writer 动笔前逐条判 must_achieve 是否已被前文消费(同一事件已在前章发生并
    # 解决完毕 → 本章永不可达成 → 写后 must judge 会 4/4 正确拒绝 → budget 必
    # 耗尽,luo-accept-v1 ch-0007 实证现场)。高置信检出 → 提前升 human_intervention
    # (省 4 版 writer+reviewer+validators 浪费),详情落 escalation_details 供
    # 孤儿章人工裁决 UI 展示。
    #
    # 门控(全部满足才跑,任何一条不满足 = 零行为变化):
    # - wiring_enabled:生产路径必开;P1 兼容 / 纯 mock 编排测路径不触发;
    # - 全新写作(initial_state=DRAFTING 且 initial_rewrite_count=0):恢复 /
    #   重写场景 v1 已存在,写前预检时机已过,写后 must judge 兜底;
    # - must_achieve 非空。
    # validator 自身再挡一层:prev card + 近章摘要全空(典型 = 全书首章)→
    # skip 零 LLM;LLM/setup 异常 fail-open 放行写作(judge 挂不阻断创作)。
    if (
        state != ChapterState.HUMAN_INTERVENTION
        and wiring_enabled
        and initial_state == ChapterState.DRAFTING
        and initial_rewrite_count == 0
        and chapter_card.must_achieve
        # v2 Fix 2:人工复核确认 Fix D 误判后,经 UI ":ignore-feasibility" 显式跳过
        # 写前预检(写后 must_achieve_validator 仍兜底验真实际内容,非无脑放过)。
        and not skip_must_feasibility_precheck
    ):
        precheck = validate_must_feasibility(
            must_achieve=chapter_card.must_achieve,
            chapter_id=chapter_id,
            prev_chapter_card=_prev_chapter_card,
            recent_summaries=pre_recent_summaries,
            workspace_id=workspace_id,
            router=router,
            client=client,
            model=model,
        )
        if not precheck.passed:
            escalation_reason = "must_infeasible_pre_write"
            escalation_details = {
                "checked_at_state": "pre_write",
                "findings": [
                    {
                        "must_text": f.must_text,
                        "confidence": f.confidence,
                        "evidence": f.evidence,
                        "suggestion": f.suggestion,
                    }
                    for f in precheck.findings
                ],
            }
            _log.warning(
                "must_infeasible_pre_write_escalated",
                workspace_id=workspace_id,
                chapter_id=chapter_id,
                findings_count=len(precheck.findings),
                items_total=precheck.items_total,
            )
            prev_state = state
            state = transition(state, "force_escalate", force=True)
            _fire(prev_state, state)

    while state not in {ChapterState.APPROVED, ChapterState.HUMAN_INTERVENTION}:
        if state == ChapterState.DRAFTING:
            try:
                with _chapter_timer("writer", creation_type="draft"):
                    writer_out = _call_writer(creation_type="draft")
            except WriterDraftIntegrityError as exc:
                # 坏结构没有可安全落版/审核的正文；账本保留原调用，不重发或补写。
                # v3 单独字数越界已交给原有写后规则，不会从这里提前升级。
                escalation_reason = "writer_draft_integrity_failed"
                escalation_details = {
                    "checked_at_state": "post_writer_pre_version",
                    "summary": "Writer 返回的正文结构或内部指令泄漏检查未通过。",
                    "reasons": list(exc.reasons),
                    "details": exc.details,
                }
                _log.warning(
                    "writer_draft_integrity_failed",
                    chapter_id=chapter_id,
                    workspace_id=workspace_id,
                    reasons=list(exc.reasons),
                )
                prev_state = state
                state = transition(state, "force_escalate", force=True)
                _fire(prev_state, state)
                continue
            cv = versioning.write_version(
                chapter_id,
                writer_out.content,
                creation_type="draft",
                model_used=writer_out.model_used,
            )
            versions.append(cv)
            last_version_id = cv.version_id
            if writer_out.executable_constraint_plan is None:
                raise ValueError("WriterOutput 缺少 ExecutableConstraintPlan")
            constraint_plans_by_version[last_version_id] = (
                writer_out.executable_constraint_plan
            )
            if writer_out.revision_contract is not None:
                revision_contracts_by_version[last_version_id] = (
                    writer_out.revision_contract
                )
            # v2 W3.5-s7:记录本稿是否被 word_count_trim 改写(透传 style_drift 句均豁免)。
            wc_trimmed_by_version[last_version_id] = writer_out.word_count_trimmed
            wc_trim_attempted_by_version[last_version_id] = (
                writer_out.word_count_trim_attempted
            )
            wc_trim_attempts_by_version[last_version_id] = (
                writer_out.word_count_trim_attempts
            )
            persist_chapter_version_safely(
                session_factory, chapter_id=chapter_id, version=cv
            )
            prev_state = state
            state = transition(state, "draft_done")
            _fire(prev_state, state)

        elif state == ChapterState.REWRITING:
            rewrite_constraint_guardrail = (
                _build_rewrite_constraint_guardrail(
                    review=reviews[-1],
                    chapter_card=chapter_card,
                )
                if reviews
                else None
            )
            rewrite_prompt_context = _resolve_writer_prompt_context(
                creation_type="rewrite",
                rewrite_constraint_guardrail=rewrite_constraint_guardrail,
            )
            with _chapter_timer("rewrite_round", creation_type="rewrite"):
                with _chapter_timer("writer", creation_type="rewrite"):
                    writer_out = _call_writer(
                        creation_type="rewrite",
                        rewrite_constraint_guardrail=rewrite_constraint_guardrail,
                        prompt_context=rewrite_prompt_context,
                    )
            cv = versioning.write_version(
                chapter_id,
                writer_out.content,
                creation_type="rewrite",
                model_used=writer_out.model_used,
            )
            versions.append(cv)
            last_version_id = cv.version_id
            if writer_out.executable_constraint_plan is None:
                raise ValueError("WriterOutput 缺少 ExecutableConstraintPlan")
            constraint_plans_by_version[last_version_id] = (
                writer_out.executable_constraint_plan
            )
            if writer_out.revision_contract is not None:
                revision_contracts_by_version[last_version_id] = (
                    writer_out.revision_contract
                )
            if rewrite_prompt_context.rewrite_constraint_guardrail is not None:
                rewrite_guardrails_by_version[last_version_id] = (
                    rewrite_prompt_context.rewrite_constraint_guardrail
                )
            # v2 W3.5-s7:记录本稿是否被 word_count_trim 改写(透传 style_drift 句均豁免)。
            wc_trimmed_by_version[last_version_id] = writer_out.word_count_trimmed
            wc_trim_attempted_by_version[last_version_id] = (
                writer_out.word_count_trim_attempted
            )
            wc_trim_attempts_by_version[last_version_id] = (
                writer_out.word_count_trim_attempts
            )
            persist_chapter_version_safely(
                session_factory, chapter_id=chapter_id, version=cv
            )
            prev_state = state
            state = transition(state, "rewrite_done")
            _fire(prev_state, state)

        elif state == ChapterState.REVISING:
            assert last_version_id is not None  # 进入 REVISING 前必有 draft 版本
            last_review = reviews[-1]
            source_version_id = last_version_id
            content = versioning.read_version_content(chapter_id, last_version_id)
            invocation_v1 = next(
                (version for version in versions if version.version_id == 1),
                None,
            )
            revise_plan = compile_executable_constraint_plan(
                chapter_card=chapter_card,
                stage="revise",
                setting_contract=world_contract,
                character_records=pre_character_records,
                cross_chapter_voice=cross_chapter_voice_spec,
                rewrite_style=resolve_rewrite_style_constraint_spec(
                    v1_fingerprint=(
                        invocation_v1.style_fingerprint
                        if invocation_v1 is not None
                        else persisted_v1_style_fingerprint
                    ),
                    cross_chapter_baseline_fingerprint=cross_chapter_anchor_fp,
                ),
                core_state_projection=core_state_projection,
            )
            revision_contract = build_reviser_revision_contract(
                source_content=content,
                source_version_id=source_version_id,
                executable_constraint_plan=revise_plan,
                issues=last_review.revise_targets,
                validator_failures=last_review.validator_failure_details or (),
                constraint_results=last_review.constraint_results,
            )
            use_word_count_trim = _should_use_word_count_trim_revision(
                review=last_review,
                chapter_text=content,
                chapter_card=chapter_card,
                executable_constraint_plan=revise_plan,
                allow_word_count_tolerance=allow_word_count_tolerance,
            )
            revision_content: str
            revision_model_used: str
            revision_was_word_count_trim = False
            if use_word_count_trim:
                if revision_contract.mode != "global":
                    raise ValueError(
                        "单独 above_max 必须由 GLOBAL revision contract 授权"
                    )
                revision_contract.assert_compatible(
                    source_content=content,
                    source_version_id=source_version_id,
                    executable_constraint_plan=revise_plan,
                )
                proactive_trim_already_attempted = wc_trim_attempted_by_version.get(
                    source_version_id, False
                )
                if proactive_trim_already_attempted:
                    # Writer 后的主动 pass 已做过同一个最多两刀闭环；Reviewer 再报
                    # above-max 只是确认它没收敛，不构成再发第三、第四刀的新证据。
                    trim_out = WordCountTrimOutput(
                        content=content,
                        trimmed=False,
                        reason="fallback_proactive_trim_already_attempted",
                        model_used=None,
                        orig_chars=len(content),
                        final_chars=len(content),
                        attempts=wc_trim_attempts_by_version.get(source_version_id, 0),
                        chain=[len(content)],
                    )
                    _log.info(
                        "word_count_trim_revision_skipped_after_proactive_attempt",
                        workspace_id=workspace_id,
                        chapter_id=chapter_id,
                        source_version_id=source_version_id,
                        attempts=trim_out.attempts,
                        action="rewrite",
                    )
                else:
                    _log.info(
                        "word_count_trim_revision_dispatched",
                        workspace_id=workspace_id,
                        chapter_id=chapter_id,
                        source_version_id=source_version_id,
                        actual_word_count=len(content),
                        word_count_min=chapter_card.word_count_min,
                        word_count_max=chapter_card.word_count_max,
                        source_sha256=revision_contract.source_sha256,
                        plan_hash=revision_contract.plan_hash,
                    )
                    with _chapter_timer(
                        "reviser",
                        creation_type="revise",
                        method="word_count_trim",
                    ):
                        trim_out = trim_to_word_count(
                            content=content,
                            chapter_card=chapter_card,
                            router=router,
                            client=client,
                            model=model,
                            chapter_id=chapter_id,
                            # reactive revise 必须守同一 ChapterCard 闭区间；默认
                            # 2500 只适合历史固定卡，不能另造软下限。
                            soft_floor=chapter_card.word_count_min,
                        )
                trim_in_chapter_band = (
                    trim_out.trimmed
                    and classify_word_count_acceptance(
                        len(trim_out.content),
                        word_count_min=chapter_card.word_count_min,
                        word_count_max=chapter_card.word_count_max,
                        allow_word_count_tolerance=allow_word_count_tolerance,
                    )
                    != "rejected"
                )
                reactive_trim_reason = (
                    trim_out.reason
                    if trim_in_chapter_band
                    else (
                        "fallback_outside_chapter_band"
                        if trim_out.trimmed
                        else trim_out.reason
                    )
                )
                _log.info(
                    "word_count_trim_revision_result",
                    workspace_id=workspace_id,
                    chapter_id=chapter_id,
                    source_version_id=source_version_id,
                    trimmed=trim_in_chapter_band,
                    raw_trimmed=trim_out.trimmed,
                    reason=reactive_trim_reason,
                    attempts=trim_out.attempts,
                    chain=trim_out.chain,
                    action=("review" if trim_in_chapter_band else "rewrite"),
                )
                if not trim_in_chapter_band:
                    # 专用 trim 已在内部做满“输出 → len() → 区外仍超则第二刀”。
                    # 没有进入本地验收带的候选时不落一份伪 revise/重复版本，也不
                    # 重新付费 Reviewer；直接经既有状态机转 GLOBAL rewrite。只有
                    # rewrite 预算本来就耗尽时才进入人工。
                    prev_state = state
                    state = transition(state, "revise_done")
                    _fire(prev_state, state)

                    prev_state = state
                    if rewrite_count >= chapter_card.max_rewrites:
                        state = transition(
                            state,
                            "escalate",
                            verdict="rewrite",
                            rewrite_count=rewrite_count,
                            max_rewrites=chapter_card.max_rewrites,
                        )
                        escalation_reason = "rewrite_budget_exhausted"
                        escalation_details = _build_rewrite_budget_escalation_details(
                            review=last_review,
                            rewrite_count=rewrite_count,
                            max_rewrites=chapter_card.max_rewrites,
                        )
                        escalation_details.update(
                            {
                                "checked_at_state": (
                                    "post_word_count_trim_not_converged"
                                ),
                                "word_count_trim_not_converged": True,
                                "source_version_id": source_version_id,
                                "source_sha256": revision_contract.source_sha256,
                                "trim_reason": reactive_trim_reason,
                                "trim_attempts": trim_out.attempts,
                                "trim_chain": trim_out.chain,
                            }
                        )
                    else:
                        state = transition(state, "verdict_rewrite")
                        rewrite_count += 1
                    _fire(prev_state, state)
                    continue
                if trim_out.model_used is None:
                    raise ValueError("word_count_trim 成功但缺少 model_used")
                revision_content = trim_out.content
                revision_model_used = trim_out.model_used
                revision_was_word_count_trim = True
            else:
                with _chapter_timer(
                    "reviser", creation_type="revise", method="general_reviser"
                ):
                    reviser_out = revise_chapter(
                        router=router,
                        client=client,
                        model=model,
                        chapter_text=content,
                        issues=last_review.revise_targets,
                        # v1.1 G8:`_apply_validator_pipeline` light 失败时
                        # verdict=revise,validator_failure_details 已写入 review;
                        # below_min / 混合意见仍由通用 Reviser 消费。单独
                        # above_max 已在上方改走专用 trim 闭环。
                        validator_failures=last_review.validator_failure_details,
                        # R9:Reviser 与 Writer/validator 共用同一 ChapterCard 与
                        # 上一轮结构化 HARD 结果；只允许按阶段做局部 patch。
                        chapter_card=chapter_card,
                        constraint_results=list(last_review.constraint_results),
                        executable_constraint_plan=revise_plan,
                        revision_contract=revision_contract,
                        core_state_projection=core_state_projection,
                    )
                if reviser_out.executable_constraint_plan is None:
                    raise ValueError("ReviserOutput 缺少 ExecutableConstraintPlan")
                revision_content = reviser_out.content
                revision_model_used = reviser_out.model_used
            cv = versioning.write_version(
                chapter_id,
                revision_content,
                creation_type="revise",
                model_used=revision_model_used,
            )
            versions.append(cv)
            last_version_id = cv.version_id
            constraint_plans_by_version[last_version_id] = revise_plan
            revision_contracts_by_version[last_version_id] = revision_contract
            # 与 draft/rewrite 后的同一既有 trim 语义一致：受控精简会主动改变句长，
            # 下游只把该句均维度降 WARN；其他 HARD 仍完整重验。
            wc_trimmed_by_version[last_version_id] = revision_was_word_count_trim
            wc_trim_attempted_by_version[last_version_id] = (
                revision_was_word_count_trim
            )
            wc_trim_attempts_by_version[last_version_id] = (
                trim_out.attempts if revision_was_word_count_trim else 0
            )
            if revision_content == content:
                # R8 ch-0003:Reviser 曾逐字返回源正文；旧循环仍把同文送给
                # Reviewer + 全套写后 LLM gates，形成无新证据的付费盲重。
                # duplicate revise 版本仍落盘，既保存模型真实输出，也让 R5
                # 版本账本继续权威计数；但相同正文直接复用上一份失败事实，
                # 不再付费重审。尚有 rewrite 名额就走既有 REVIEWING →
                # REWRITING 转换；否则按既有预算耗尽语义升人工。
                content_sha256 = hashlib.sha256(content.encode("utf-8")).hexdigest()
                inherited_review = last_review.model_copy(
                    update={
                        "review_id": f"rev-inherited-{uuid.uuid4().hex[:12]}",
                        "version_reviewed": last_version_id,
                        "reviewed_at": datetime.now(UTC),
                        "reviewer_model": f"inherited:{last_review.reviewer_model}",
                        "suggestions": [
                            "[继承审稿] Reviser 输出与源正文逐字一致；本版本继承 "
                            f"v{source_version_id} 的审稿事实，未再次调用 Reviewer。",
                            *last_review.suggestions,
                        ],
                    }
                )
                persist_chapter_version_safely(
                    session_factory,
                    chapter_id=chapter_id,
                    version=cv,
                    inherited_review_checkpoint=inherited_review,
                )
                reviews.append(inherited_review)
                _log.warning(
                    "reviser_no_content_change",
                    chapter_id=chapter_id,
                    source_version_id=source_version_id,
                    duplicate_version_id=last_version_id,
                    content_sha256=content_sha256,
                    revise_count=revise_count,
                    rewrite_count=rewrite_count,
                    max_rewrites=chapter_card.max_rewrites,
                    action=(
                        "escalate_literary_no_effect"
                        if last_review.revise_targets
                        else "escalate"
                        if rewrite_count >= chapter_card.max_rewrites
                        else "rewrite"
                    ),
                )
                prev_state = state
                state = transition(state, "revise_done")
                _fire(prev_state, state)

                prev_state = state
                if last_review.revise_targets:
                    state = transition(state, "force_escalate", force=True)
                    escalation_reason = "literary_revise_limit_reached"
                    escalation_details = _build_literary_revise_escalation_details(
                        review=last_review,
                        revise_count=revise_count,
                    )
                    escalation_details.update(
                        {
                            "checked_at_state": "post_revise_no_effect",
                            "reviser_no_content_change": True,
                            "source_version_id": source_version_id,
                            "duplicate_version_id": last_version_id,
                            "content_sha256": content_sha256,
                        }
                    )
                elif rewrite_count >= chapter_card.max_rewrites:
                    state = transition(
                        state,
                        "escalate",
                        verdict="rewrite",
                        rewrite_count=rewrite_count,
                        max_rewrites=chapter_card.max_rewrites,
                    )
                    escalation_reason = "rewrite_budget_exhausted"
                    escalation_details = _build_rewrite_budget_escalation_details(
                        review=last_review,
                        rewrite_count=rewrite_count,
                        max_rewrites=chapter_card.max_rewrites,
                    )
                    escalation_details.update(
                        {
                            "checked_at_state": "post_revise_no_effect",
                            "reviser_no_content_change": True,
                            "source_version_id": source_version_id,
                            "duplicate_version_id": last_version_id,
                            "content_sha256": content_sha256,
                        }
                    )
                else:
                    state = transition(state, "verdict_rewrite")
                    rewrite_count += 1
                _fire(prev_state, state)
                continue
            persist_chapter_version_safely(
                session_factory, chapter_id=chapter_id, version=cv
            )
            prev_state = state
            state = transition(state, "revise_done")
            _fire(prev_state, state)

        elif state == ChapterState.REVIEWING:
            assert last_version_id is not None  # 进入 REVIEWING 前必有版本可审
            if (
                pending_resume_review is not None
                and pending_resume_review.version_reviewed == last_version_id
            ):
                # Reviewer + 全部写后 gates 的最终结果已在前一进程完整落账。
                # 恢复时直接续判，禁止再次付费审同一正文。
                review = pending_resume_review
                pending_resume_review = None
                reviews.append(review)
                _log.info(
                    "review_checkpoint_resumed_without_provider",
                    workspace_id=workspace_id,
                    chapter_id=chapter_id,
                    version_reviewed=last_version_id,
                    review_id=review.review_id,
                    verdict=review.verdict,
                )
                prev_state = state
                if single_review_pass and review.verdict != "pass":
                    escalation_reason = "re_review_failed"
                    escalation_details = _build_re_review_escalation_details(
                        review=review
                    )
                    state = transition(state, "force_escalate", force=True)
                else:
                    checkpoint_content = (
                        active_rewrite_checkpoint.source_content
                        if (
                            active_rewrite_checkpoint is not None
                            and active_rewrite_checkpoint.version_id
                            == last_version_id
                        )
                        else versioning.read_version_content(
                            chapter_id, last_version_id
                        )
                    )
                    checkpoint_plan = _resolve_constraint_plan_for_review(
                        last_version_id
                    )
                    budget_neutral_reflow = (
                        _can_route_budget_neutral_paragraph_reflow(
                            review=review,
                            source_content=checkpoint_content,
                            current_version_id=last_version_id,
                            executable_constraint_plan=checkpoint_plan,
                            current_fingerprint=(
                                active_rewrite_checkpoint.source_style_fingerprint
                                if (
                                    active_rewrite_checkpoint is not None
                                    and active_rewrite_checkpoint.version_id
                                    == last_version_id
                                )
                                else None
                            ),
                        )
                    )
                    (
                        state,
                        revise_count,
                        rewrite_count,
                        escalation_reason,
                    ) = _handle_verdict(
                        state=state,
                        review=review,
                        chapter_card=chapter_card,
                        revise_count=revise_count,
                        rewrite_count=rewrite_count,
                        budget_neutral_mechanical_reflow=(
                            budget_neutral_reflow
                        ),
                    )
                    if escalation_reason == "rewrite_budget_exhausted":
                        escalation_details = _build_rewrite_budget_escalation_details(
                            review=review,
                            rewrite_count=rewrite_count,
                            max_rewrites=chapter_card.max_rewrites,
                        )
                    elif escalation_reason == "literary_revise_limit_reached":
                        escalation_details = (
                            _build_literary_revise_escalation_details(
                                review=review,
                                revise_count=revise_count,
                            )
                        )
                _fire(prev_state, state)
                continue
            content = versioning.read_version_content(chapter_id, last_version_id)
            current_plan = _resolve_constraint_plan_for_review(last_version_id)
            try:
                # P2-B-19:Reviewer 也注入短 session 拿 voice 段对账数据。
                # v1.1 audit G22 治本(2026-05-27 b1-rerun-deep-audit):透传当前
                # 版本 fingerprint + 前一章 baseline fingerprint 给 reviewer,让
                # reviewer prompt v5 "风格指纹客观参考" 段渲染客观指标供 LLM 兜
                # 底打分(paragraph_mean 低于共享硬底线时扣分；确定性层派 rewrite，杜绝
                # luo-clean-v1 ch-5 现场"reviewer 主观高分 + validator 截获"悖论)。
                # 与 ChapterVersioning.write_version 同源计算。恢复时 versions
                # 只含本轮新增版本，不能据此省略原请求已有的风格参考。
                current_fp_for_review = compute_style_fingerprint(content)
                with _chapter_timer("reviewer", version_reviewed=last_version_id):
                    with _maybe_session(
                        session_factory if wiring_enabled else None
                    ) as review_session:
                        review = _review_with_retry(
                            router=router,
                            client=client,
                            model=model,
                            chapter_card=chapter_card,
                            chapter_text=content,
                            chapter_id=chapter_id,
                            version_reviewed=last_version_id,
                            db_session=review_session,
                            workspace_id=workspace_id,
                            current_fingerprint=current_fp_for_review,
                            prev_chapter_baseline_fingerprint=cross_chapter_anchor_fp,
                            executable_constraint_plan=current_plan,
                            initial_attempt_errors=(
                                rewrite_checkpoint_context.previous_reviewer_parse_errors
                                if rewrite_checkpoint_context is not None
                                and last_version_id == rewrite_checkpoint_context.version_id
                                else None
                            ),
                        )
                        review = apply_objective_reviewer_guards(
                            review=review,
                            chapter_text=content,
                            revision_contract=revision_contracts_by_version.get(
                                last_version_id
                            ),
                        )
            except ReviewerParseError as parse_exc:
                # v1.0.x reviewer-resilience:_REVIEWER_MAX_ATTEMPTS 次都失败,
                # raw_output 已分别按 attempt=1..N 落 structlog;通过状态机
                # force 接口升人工,不绕过 transition。
                # v1.0.x:升人工前持久化占位 review_results 行(score=0,
                # verdict='parse_failed',suggestions 含 raw_output 预览与错误
                # 摘要),让前端"审稿历史"能渲染"自动审稿失败 ×N"红色行,
                # 不再"暂无审稿"让用户毫无线索。
                escalation_reason = "reviewer_parse_failed_after_retry"
                attempts_log: list[str] = getattr(parse_exc, "attempts_log", [])
                escalation_details = _build_reviewer_parse_escalation_details(
                    raw_output=parse_exc.raw_output,
                    model_used=parse_exc.model_used,
                    attempts_log=attempts_log,
                )
                _persist_parse_failure_safely(
                    session_factory=session_factory,
                    chapter_id=chapter_id,
                    version_reviewed=last_version_id,
                    raw_output=parse_exc.raw_output,
                    model_used=parse_exc.model_used,
                    attempts_log=attempts_log,
                )
                prev_state = state
                state = transition(state, "force_escalate", force=True)
                _fire(prev_state, state)
                continue

            # P4-B-06:Reviewer 跑完后调度 [HARD] validator pipeline。任一失败 →
            # ``review.verdict`` 覆盖为 ``rewrite``,``failed_constraints`` 合并
            # Reviewer 自报 + validator 失败约束;``validator_failed`` 事件已在
            # ``run_validator_pipeline`` 内 emit。新 review 入 ``reviews`` 列表
            # + 落 review_results 表,确保前端 / metrics 看到的是最终 verdict。
            # P7-B-06:pipeline 后再串行调 must_achieve_validator(独立 LLM agent,
            # N 次 yes/no 调用),与 pipeline substring 检查互补;pipeline /
            # validator 任一失败均覆盖 verdict=rewrite。
            # v1.1 wave 1 session 4(G5):主路径 vN >= 2 时拉 baseline/current
            # fingerprint 触发 style_drift 检测;v1 自身审阅时返全 None 跳过。
            (
                bfp_main,
                cfp_main,
                bvid_main,
                cvid_main,
            ) = _resolve_style_fingerprint_pair(
                versions=versions,
                current_version_id=last_version_id,
                persisted_v1_fingerprint=persisted_v1_style_fingerprint,
                persisted_current_fingerprint=current_fp_for_review,
            )
            # 同一源正文的指标同时进入 Reviewer 和确定性检查。
            ccfp_current_main = current_fp_for_review
            with _chapter_timer(
                "deterministic_validators", version_reviewed=last_version_id
            ):
                review = _apply_validator_pipeline(
                    review=review,
                    chapter_text=content,
                    chapter_card=chapter_card,
                    workspace_id=workspace_id,
                    known_character_ids=pre_known_character_ids,
                    timeline_entries=pre_timeline_entries,
                    character_records=pre_character_records,
                    baseline_style_fingerprint=bfp_main,
                    current_style_fingerprint=cfp_main,
                    baseline_version_id=bvid_main,
                    current_version_id=cvid_main,
                    cross_chapter_baseline_fingerprint=cross_chapter_anchor_fp,
                    cross_chapter_baseline_chapter_id=cross_chapter_baseline_chapter_id,
                    cross_chapter_current_fingerprint=ccfp_current_main,
                    workspace_default_voice=cross_chapter_workspace_default_voice,
                    mini_plan_event=None,  # 首版不接 mini_plan 文件 IO,留 wave 2+
                    is_first_chapter=cross_chapter_is_first_chapter,
                    style_trend_history=style_trend_history,
                    # v2 W3.5-s7:本版本被 word_count_trim 改写 → style_drift 句均降 warn-only。
                    current_word_count_trimmed=wc_trimmed_by_version.get(
                        last_version_id, False
                    ),
                    executable_constraint_plan=current_plan,
                    allow_word_count_tolerance=allow_word_count_tolerance,
                )
            # v2 间隙 A1(2026-06-13;W3.5 补强3 加 forbidden_crossing):6 个写后 LLM
            # 闸经统一调度并发判定 + 串行
            # 原序合并(回退开关 post_review_gate_max_concurrency=1 时逐字等价旧
            # 串行链)。各闸所需上下文由唯一稿主路径透传。
            with _chapter_timer(
                "post_review_gates",
                version_reviewed=last_version_id,
                gate_count=6,
                max_concurrency=settings.post_review_gate_max_concurrency,
            ):
                review = _run_post_review_llm_gates(
                    review=review,
                    chapter_text=content,
                    chapter_card=chapter_card,
                    workspace_id=workspace_id,
                    router=router,
                    client=client,
                    model=model,
                    prev_chapter_card=_prev_chapter_card,
                    world_contract=world_contract,
                    character_records=pre_character_records,
                    session_factory=session_factory,
                    versioning=versioning,
                    executable_constraint_plan=current_plan,
                    core_state_projection=core_state_projection,
                    source_review=_source_review_for_revision(
                        revision_contracts_by_version.get(last_version_id)
                    ),
                    revision_contract=revision_contracts_by_version.get(
                        last_version_id
                    ),
                    continuity_capsule=continuity_capsule,
                )
            # v1.1 audit G22b 兜底硬覆盖(2026-05-27 b1-rerun-deep-audit):
            # paragraph_mean 低于共享 PARAGRAPH_MEAN_HARD_MIN 视作散文诗式
            # mode collapse,强制覆盖 verdict=
            # rewrite + tension_match=3.0 + prose_quality=3.0 + 加 pacing issue。
            # **本兜底位于所有 validator 后**,确保即使 reviewer 主观高分 + validator
            # 全过(理论极端 case)也能被截获。配合 G22a prompt 段 + G22 reviewer
            # 透传 fingerprint,形成 prompt 主推(LLM 主导)+ post-process 兜底
            # (硬截获)双层防御。
            review = _apply_paragraph_collapse_override(
                review=review,
                current_fingerprint=ccfp_current_main,
                chapter_id=chapter_id,
                version_reviewed=last_version_id,
                executable_constraint_plan=current_plan,
            )
            review = _annotate_hard_constraint_regressions(
                review=review,
                guardrail=rewrite_guardrails_by_version.get(last_version_id),
                chapter_id=chapter_id,
                workspace_id=workspace_id,
                version_reviewed=last_version_id,
                source_content=(
                    revision_contracts_by_version[last_version_id].source_content
                    if last_version_id in revision_contracts_by_version
                    else None
                ),
                current_content=content,
            )
            review = finalize_review_warning_signals(review)
            current_plan.assert_review_uses_only_planned_hard(review)
            budget_neutral_reflow = _can_route_budget_neutral_paragraph_reflow(
                review=review,
                source_content=content,
                current_version_id=last_version_id,
                executable_constraint_plan=current_plan,
                current_fingerprint=ccfp_current_main,
            )
            repeated_failure_ids = _repeated_hard_failure_ids(
                previous=reviews[-1] if reviews else None,
                current=review,
            )
            reviews.append(review)
            persist_review_result_safely(session_factory, review=review)
            prev_state = state
            if repeated_failure_ids and not budget_neutral_reflow:
                escalation_reason = "repeated_hard_constraint_no_progress"
                escalation_details = {
                    "constraint_ids": repeated_failure_ids,
                    "plan_hash": current_plan.plan_hash,
                    "version_reviewed": last_version_id,
                    "message": "同一 HARD ID 以相同证据再次失败，已阻止盲目付费重试。",
                }
                state = transition(state, "force_escalate", force=True)
            elif single_review_pass and review.verdict != "pass":
                # v2 W3.5 UI 缺口3:re-review 模式(人工改稿后单次重审)。pass 走
                # 正常 _handle_verdict → APPROVED;非 pass 不再自动改写(人工已动手),
                # 直接回人工 + 专属 reason/详情,让 UI 列反馈供再改 / 强制通过。
                escalation_reason = "re_review_failed"
                escalation_details = _build_re_review_escalation_details(review=review)
                state = transition(state, "force_escalate", force=True)
            else:
                state, revise_count, rewrite_count, escalation_reason = _handle_verdict(
                    state=state,
                    review=review,
                    chapter_card=chapter_card,
                    revise_count=revise_count,
                    rewrite_count=rewrite_count,
                    budget_neutral_mechanical_reflow=budget_neutral_reflow,
                )
                # v2 W3.5 UI 缺口3:单稿路径 rewrite 预算耗尽升人工 → 填详情。
                if escalation_reason == "rewrite_budget_exhausted":
                    escalation_details = _build_rewrite_budget_escalation_details(
                        review=review,
                        rewrite_count=rewrite_count,
                        max_rewrites=chapter_card.max_rewrites,
                    )
                elif escalation_reason == "literary_revise_limit_reached":
                    escalation_details = _build_literary_revise_escalation_details(
                        review=review,
                        revise_count=revise_count,
                    )
            _fire(prev_state, state)

        else:  # 防御:理论上 while 已挡住 APPROVED / HUMAN_INTERVENTION
            raise RuntimeError(f"orchestrator 进入未预期状态: {state}")

    final_version_id: int | None = None
    final_path: Path | None = None
    if state == ChapterState.APPROVED:
        # 通过审稿:把当前最后一个版本标定为 final.md。
        assert last_version_id is not None
        final_version_id = last_version_id
        final_path = versioning.mark_final(chapter_id, last_version_id)

        # P2-B-19:wiring 启用时,在 APPROVED 终态后调 MemoryExtractor 抽取
        # 增量记忆并 commit SQLite + 写 Chroma。失败**吞 + structlog error**
        # 不冒泡——章节已通过审稿 + final.md 已落盘,extract 是辅助任务,失
        # 败重试 / 兜底降级归 P3+。
        if wiring_enabled and post_approval_memory_enabled and final_path is not None:
            assert session_factory is not None  # type narrow(wiring_enabled 已保证)
            assert vector_store is not None
            _extract_memories_safely(
                session_factory=session_factory,
                vector_store=vector_store,
                router=router,
                client=client,
                chapter_card=chapter_card,
                workspace_id=workspace_id,
                final_path=final_path,
                model=model,
                setting_contract=world_contract,
                progression_consistency_enabled=progression_consistency_enabled,
                progression_label=progression_label,
            )
            # v1.1 wave 6 session 4(B5):口头承诺追踪。**仅终章触发**(本章 = 全书
            # 最后一章时)校验 DB 里正文累积的 plot_threads 是否兑现:重要 + 未收口 +
            # 无 payoff 的 → 复用 thread_closure_validator 判"有交代开放 vs 埋了不收"。
            # WARN-only,全 swallow(extract 后纯观测)。在 extract 写事务之外(护 Y1-C)。
            _check_plot_thread_payoff_safely(
                session_factory=session_factory,
                chapter_card=chapter_card,
                workspace_id=workspace_id,
                router=router,
                client=client,
                model=model,
            )
    elif state == ChapterState.HUMAN_INTERVENTION:
        # P1-B-24:OBSERVABILITY §2.4 `human_intervention_required` 事件。
        # ``escalation_reason`` 在 `_handle_verdict` / reviewer parse retry
        # 路径已设置,反映升人工的真正原因。
        _log.warning(
            "human_intervention_required",
            reason=escalation_reason,
            revise_count=revise_count,
            rewrite_count=rewrite_count,
        )

    return OrchestratorResult(
        final_state=state,
        final_version_id=final_version_id,
        final_path=final_path,
        reviews=reviews,
        versions=versions,
        revise_count=revise_count,
        rewrite_count=rewrite_count,
        escalation_reason=escalation_reason,
        escalation_details=escalation_details,
    )


def _review_with_retry(
    *,
    router: ModelRouter | None,
    client: LLMClient | None,
    model: str | None,
    chapter_card: ChapterCard,
    chapter_text: str,
    chapter_id: str,
    version_reviewed: int,
    db_session: Session | None = None,
    workspace_id: str | None = None,
    current_fingerprint: dict[str, float] | None = None,
    prev_chapter_baseline_fingerprint: dict[str, float] | None = None,
    executable_constraint_plan: ExecutableConstraintPlan | None = None,
    initial_attempt_errors: list[str] | None = None,
) -> ReviewResult:
    """调 Reviewer,捕获 ``ReviewerParseError`` 并 retry 一次。

    两次 ``raw_output`` / ``model_used`` 分别按 ``attempt=1/2`` 落
    ``orchestrator_review_unparseable`` 事件,P7 调优要分辨"同类问题连续两次"
    vs"不同问题"——只记一次拿不到这个信号。

    P2-B-19 起新增 ``db_session`` 透传:Reviewer 从中查 voice_profile 对账段
    (``card.related_characters`` 的 voice 字段);``None`` 时退化 P1 路径。
    P7-B-05 起新增 ``workspace_id`` 透传:Reviewer 内部解析工作区级阈值覆盖
    (ChapterCard 单章字段 + 模块常量);``None`` 时退化到 P3-P6 既有路径。
    """
    last_exc: ReviewerParseError | None = None
    # v1.0.x reviewer-resilience:累积每次 attempt 的简短错误描述,
    # 下一次 attempt 时附到 user message 让 LLM 自我修正格式问题。
    previous_attempt_errors = list(initial_attempt_errors or [])
    if len(previous_attempt_errors) >= _REVIEWER_MAX_ATTEMPTS:
        raise ValueError("Reviewer 格式重试预算已耗尽，不能因恢复获得新预算")
    for attempt in range(len(previous_attempt_errors) + 1, _REVIEWER_MAX_ATTEMPTS + 1):
        try:
            return review_chapter(
                router=router,
                client=client,
                model=model,
                chapter_card=chapter_card,
                chapter_text=chapter_text,
                chapter_id=chapter_id,
                version_reviewed=version_reviewed,
                db_session=db_session,
                workspace_id=workspace_id,
                previous_attempt_errors=(
                    list(previous_attempt_errors) if previous_attempt_errors else None
                ),
                current_fingerprint=current_fingerprint,
                prev_chapter_baseline_fingerprint=prev_chapter_baseline_fingerprint,
                executable_constraint_plan=executable_constraint_plan,
            )
        except ReviewerParseError as exc:
            _log.error(
                "orchestrator_review_unparseable",
                attempt=attempt,
                max_attempts=_REVIEWER_MAX_ATTEMPTS,
                chapter_id=chapter_id,
                version_reviewed=version_reviewed,
                raw_output=exc.raw_output,
                model_used=exc.model_used,
            )
            # 错误摘要(简短描述 + raw_output 前 300 字)累积给下次 retry。
            short_err = str(exc).split("\n", 1)[0][:200]
            raw_preview = (exc.raw_output or "")[:300].replace("\n", " ⏎ ")
            previous_attempt_errors.append(
                f"第 {attempt} 次尝试失败:{short_err}(原始输出前 300 字预览:{raw_preview})"
            )
            last_exc = exc
    assert last_exc is not None  # 循环必至少跑一次
    # v1.0.x reviewer-resilience:把累积的错误摘要附给 last_exc,供 caller
    # (chapter_orchestrator 升人工分支)持久化到占位 review_results 行。
    last_exc.attempts_log = previous_attempt_errors  # type: ignore[attr-defined]
    raise last_exc


def _infer_prev_chapter_id(chapter_id: str) -> str | None:
    """v1.1 wave 1 session 6 / G10:从当前 chapter_id 字符串推前一章 chapter_id。

    工作区 chapter_id 默认按 outline 生成全局连续 ``ch-NNNN`` 编号(跨卷连续),
    所以 ``ch-0008`` 的前一章 = ``ch-0007``(可能是同卷或上一卷最后一章)。

    返 ``None`` 时:chapter_id 不符合 ``ch-{int:0Nd}`` 模式 / 本身是 ``ch-0001``
    (无前一章可推)/ int 解析失败。Caller 应视为"无 baseline"走首章 / fail-open
    分支。
    """
    parts = chapter_id.split("-", 1)
    if len(parts) != 2 or parts[0] != "ch":
        return None
    try:
        num = int(parts[1])
    except ValueError:
        return None
    if num <= 1:
        return None
    width = len(parts[1])
    return f"ch-{num - 1:0{width}d}"


def _resolve_cross_chapter_voice_context(
    *,
    preread_session,
    workspace_id: str,
    chapter_card: ChapterCard,
) -> tuple[
    dict[str, float] | None,
    str | None,
    Literal["third_person", "first_person", "free"],
]:
    """v1.1 wave 1 session 6 / G10:一次性查 cross_chapter validator 所需上下文。
    **v1.1 wave 1 session 8 / G12**:扩展为**递归向上找最近 approved 祖父章节**
    (而非只看直接前一章),避免 ch-N-1 撞 human_intervention 时 baseline 落空
    导致 validator 走 fail-open 分支让漂移传染下游。

    返 ``(baseline_fingerprint, baseline_chapter_id, workspace_default_voice)``
    三元组:
    - 找到最近 approved 祖父章节 + 有 fingerprint → 返该章 fingerprint + chapter_id
    - 全部上游章节都非 approved / 无 fingerprint / 走到 ch-0001 之前 → 返
      ``(None, None, default_voice)``。caller validator 在此场景**统一回退到
      workspace.default_voice 校验路径**(G12 治本上游;不再 fail-open 漏抓)。

    **is_first_chapter** 由 caller 用 ``chapter_card.chapter_number == 1`` 计算
    (本 helper 不返该字段 — chapter_number 字段是 Pydantic 模型层,caller 直接读)。

    **G12 设计要点**:
    - 接受 approved / published 状态的祖父章节(避免 drafting / human_intervention
      / rewriting 状态的脏 fingerprint;沿用 G10 同字面约束)
    - 任一 ORM 查询异常 → log + 继续向上找(不立即返 None,避免单次 query 错误
      把整个链路打断)
    - 递归向上 _infer_prev_chapter_id(ch-N) → ch-N-1 → ... → ch-0001 → None
      (函数自身已处理 ch-0001 → None 边界)
    """
    # 拿 workspace.default_voice
    default_voice: Literal["third_person", "first_person", "free"] = "third_person"
    try:
        ws = preread_session.get(WorkspaceORM, workspace_id)
        if ws is not None and ws.default_voice in (
            "third_person",
            "first_person",
            "free",
        ):
            default_voice = ws.default_voice  # type: ignore[assignment]
    except Exception:
        _log.exception(
            "cross_chapter_voice_workspace_query_failed",
            workspace_id=workspace_id,
        )

    # G12 递归祖父循环:从直接前一章往前找,直到撞 approved 祖父章节 OR 走到 ch-0001
    candidate_id = _infer_prev_chapter_id(chapter_card.chapter_id)
    skipped_chapters: list[str] = []
    while candidate_id is not None:
        try:
            candidate = preread_session.get(ChapterORM, candidate_id)
            if candidate is None:
                # ORM 行缺失:可能是 v1 老工作区数据 / 测试 fixture 残缺
                skipped_chapters.append(f"{candidate_id}:row_missing")
                candidate_id = _infer_prev_chapter_id(candidate_id)
                continue
            if candidate.status not in ("approved", "published"):
                skipped_chapters.append(f"{candidate_id}:{candidate.status}")
                candidate_id = _infer_prev_chapter_id(candidate_id)
                continue
            candidate_version = preread_session.get(
                ChapterVersionORM, (candidate_id, candidate.current_version)
            )
            if candidate_version is None or candidate_version.style_fingerprint is None:
                # approved 但无 fingerprint(向后兼容 G5 前的旧数据)
                skipped_chapters.append(f"{candidate_id}:no_fingerprint")
                candidate_id = _infer_prev_chapter_id(candidate_id)
                continue
            # 找到 approved 祖父章节 + 有 fingerprint
            if skipped_chapters:
                _log.info(
                    "cross_chapter_voice_grandparent_baseline_found",
                    workspace_id=workspace_id,
                    current_chapter_id=chapter_card.chapter_id,
                    baseline_chapter_id=candidate_id,
                    skipped_chapters=skipped_chapters,
                )
            return candidate_version.style_fingerprint, candidate_id, default_voice
        except Exception:
            _log.exception(
                "cross_chapter_voice_baseline_query_failed",
                workspace_id=workspace_id,
                candidate_chapter_id=candidate_id,
            )
            # 异常时继续向上找,不立即返 None(单次 query 错误不打断递归)
            if candidate_id is None:
                break
            candidate_id = _infer_prev_chapter_id(candidate_id)
            continue
    # 全部上游都非 approved / 无 fingerprint / 走到 ch-0001 之前
    if skipped_chapters:
        _log.warning(
            "cross_chapter_voice_no_approved_ancestor",
            workspace_id=workspace_id,
            current_chapter_id=chapter_card.chapter_id,
            skipped_chapters=skipped_chapters,
        )
    return None, None, default_voice


def _resolve_effective_cross_chapter_baseline(
    cross_chapter_baseline_fp: dict[str, float] | None,
    style_trend_history: list[tuple[str, dict[str, float] | None]] | None,
    *,
    style_anchor_book: bool,
) -> tuple[dict[str, float] | None, bool]:
    """v2 w2-s2 债②:构造 **effective 跨章基线** — ON 时段均/句均换全书锚。

    返 ``(effective_fingerprint, book_anchor_applied)``。effective 基线是
    下游**全链路统一锚源**:writer prompt voice 段 + cross_chapter_voice 段均
    闸(WARN)+ style_drift G17 异常判定参考。三者用同一基线 → 杜绝"writer 锚全书、
    闸门判前章"两把尺打架(对齐 G17 两端一致铁律,governance §2.25/§2.33)。

    - **OFF / 锚窗口未就位 / wiring off** → 返 ``(原 cross_chapter_baseline_fp,
      False)``,下游逐字节零回归(等价 prev_chapter 锚)。
    - **ON 且锚窗口就位** → 段均/句均换全书锚(``compute_trend_anchor`` 中位,
      与检测端 style_trend_validator **同锚同源**),**其余维度(代词/比喻/std)
      保前章**(全书锚只算段均/句均;POV 判定从代词频次算 → 仍是前章语义)。

    为何换源(w2-s1 标定 §4):前章锚是慢漂的结构性放大器 — ch10 锚 ch9,
    ch9 已比 ch1 重,锚自身跟着漂(温水煮青蛙);全书锚把拉力锚点固定在开篇。
    """
    if not (
        style_anchor_book
        and style_trend_history is not None
        and cross_chapter_baseline_fp is not None
    ):
        return cross_chapter_baseline_fp, False
    book_anchor = compute_trend_anchor(style_trend_history)
    if (
        book_anchor is None
        or "paragraph_length_mean" not in book_anchor
        or "sentence_length_mean" not in book_anchor
    ):
        return cross_chapter_baseline_fp, False
    return {
        **cross_chapter_baseline_fp,
        "paragraph_length_mean": book_anchor["paragraph_length_mean"],
        "sentence_length_mean": book_anchor["sentence_length_mean"],
    }, True


def _resolve_style_trend_history(
    *,
    preread_session,
    workspace_id: str,
    current_chapter_id: str,
) -> list[tuple[str, dict[str, float] | None]]:
    """v2 w2-s2:预读趋势检测历史序列 — 早于当前章的 approved/published 章
    final 指纹(current_version 行),按 chapter_id 升序。

    与 w2-s1 标定同源口径(``ChapterVersionORM.style_fingerprint`` 落库值);
    chapter_id zero-padded 字典序 = 章序(Fix D 预检 / derive_context 同款
    用法)。指纹 None 的章(G5 前旧数据)保留在列表里由
    ``compute_trend_anchor`` / ``check_style_trend`` 统一跳过 — 本 helper
    不做语义过滤,只做 IO。

    任何查询异常 → 返空列表(fail-open,趋势检测整体 skip;对齐 preread
    段 swallow 先例)。30-100 章 = 同数量级 ORM get,SQLite 本地毫秒级。
    """
    history: list[tuple[str, dict[str, float] | None]] = []
    try:
        chapter_rows = (
            preread_session.query(ChapterORM)
            .filter(ChapterORM.workspace_id == workspace_id)
            .filter(ChapterORM.chapter_id < current_chapter_id)
            .filter(ChapterORM.status.in_(("approved", "published")))
            .order_by(ChapterORM.chapter_id.asc())
            .all()
        )
        for row in chapter_rows:
            version = preread_session.get(
                ChapterVersionORM, (row.chapter_id, row.current_version)
            )
            fingerprint = version.style_fingerprint if version is not None else None
            history.append((row.chapter_id, fingerprint))
    except Exception:
        _log.exception(
            "style_trend_history_preread_failed",
            workspace_id=workspace_id,
            current_chapter_id=current_chapter_id,
        )
        return []
    return history


def _resolve_style_fingerprint_pair(
    *,
    versions: list[ChapterVersion],
    current_version_id: int,
    persisted_v1_fingerprint: dict[str, float] | None = None,
    persisted_current_fingerprint: dict[str, float] | None = None,
) -> tuple[dict[str, float] | None, dict[str, float] | None, int | None, int | None]:
    """v1.1 wave 1 session 4(G5):从已写盘 versions list 拿 (baseline, current,
    baseline_vid, current_vid) 四元组,供 ``_apply_validator_pipeline`` 透传给
    ``check_style_drift``。

    全 None 触发条件(任一即跳过 style_drift 检测):

    - ``current_version_id < 2`` — v1 自身审阅(baseline 就是自己)
    - versions 列表内找不到 ``version_id == 1`` 的 v1 — 旧数据 / 异常路径
    - ``v1.style_fingerprint is None`` — 旧数据未抽指纹(向后兼容 fail-open)
    - 找不到 ``version_id == current_version_id`` 的 vN(理论不可能,防御)
    - ``current_v.style_fingerprint is None`` — 当前版本未抽指纹(理论不可能,
      ``ChapterVersioning.write_version`` 自动抽;旧数据 / 测试路径防御)
    """
    if current_version_id < 2:
        return None, None, None, None
    v1 = next((v for v in versions if v.version_id == 1), None)
    baseline_fingerprint = (
        v1.style_fingerprint if v1 is not None else persisted_v1_fingerprint
    )
    if baseline_fingerprint is None:
        return None, None, None, None
    current_v = next((v for v in versions if v.version_id == current_version_id), None)
    current_fingerprint = (
        current_v.style_fingerprint
        if current_v is not None
        else persisted_current_fingerprint
    )
    if current_fingerprint is None:
        return None, None, None, None
    return baseline_fingerprint, current_fingerprint, 1, current_version_id


def _apply_validator_pipeline(
    *,
    review: ReviewResult,
    chapter_text: str,
    chapter_card: ChapterCard,
    workspace_id: str,
    known_character_ids: set[str] | None = None,
    timeline_entries: list[TimelineEntry] | None = None,
    character_records: list[Character] | None = None,
    baseline_style_fingerprint: dict[str, float] | None = None,
    current_style_fingerprint: dict[str, float] | None = None,
    baseline_version_id: int | None = None,
    current_version_id: int | None = None,
    cross_chapter_baseline_fingerprint: dict[str, float] | None = None,
    cross_chapter_baseline_chapter_id: str | None = None,
    cross_chapter_current_fingerprint: dict[str, float] | None = None,
    workspace_default_voice: Literal[
        "third_person", "first_person", "free"
    ] = "third_person",
    mini_plan_event: str | None = None,
    is_first_chapter: bool = False,
    style_trend_history: list[tuple[str, dict[str, float] | None]] | None = None,
    current_word_count_trimmed: bool = False,
    executable_constraint_plan: ExecutableConstraintPlan | None = None,
    allow_word_count_tolerance: bool = True,
) -> ReviewResult:
    """P4-B-06:Reviewer 跑完后跑 [HARD] validator pipeline,失败按严重度派 verdict。

    主路径(全过):返原 ``review`` 不动;Reviewer 自报 ``verdict`` 透到
    ``_handle_verdict``。

    失败路径(v1.1 G8 起按严重度分级):
    - emit ``validator_failed`` 事件已在 ``run_validator_pipeline`` 内完成
    - ``classify_validator_severity(report)`` 判 ``"light"`` / ``"heavy"``
    - ``light``(恰好 1 条 ``word_count`` 失败 + 偏差 < 5%)→ verdict 派
      ``revise``,reviser 路径激活(局部 patch 补/删字数);**v1.1 G8 修
      revise dead code in practice 根因**
    - ``heavy``(其他全部情况:多 validator 失败 / 非 word_count 失败 /
      偏差 ≥ 5%)→ verdict 派 ``rewrite``(沿用 P4-B-06 原行为)
    - ``review.model_copy(update=...)`` 覆盖 verdict + hard_constraints_passed
      + failed_constraints + ``validator_failure_details``(v1.1 G8 新加:
      reviser 在 REVISING 分支读这字段做针对性 patch)
    - caller 用新 review 走原 ``_handle_verdict`` 路径,自动复用
      ``revise_count / rewrite_count + 1`` / budget 检查 / escalate 升人工逻辑

    v1.1 G8 互动设计提醒:**后续 ``_apply_must_achieve_validator`` 和
    ``_apply_continuity_validator``**(若它们失败)会**强制覆盖** verdict
    = ``rewrite`` + hard_constraints_passed=False(line 870-876 / 1016-1022),
    本函数派的 light revise 在那两个 validator 也挂时会被升级为 rewrite。
    这是符合预期的:must_achieve / continuity 失败语义重(关键事件没铺设 /
    POV 场景错乱),patch 难补,必须整章重写。

    R9:章节卡角色存在性与死亡状态检查已经前移到 Writer 之前的 admission，
    不再进入写后 pipeline，也不再把数据缺陷伪装成正文 rewrite。这里保留的
    ``timeline_entries`` / ``character_records`` 只驱动 timeline WARN 与
    可归因 voice 检查；不可归因 voice 命中也是 WARN。

    v2 w2-s2:``style_trend_history``(早于当前章 approved 章指纹升序)
    透传激活 ``check_style_trend`` 跨章慢漂检测(WARN-only,pipeline 内
    失败只 warn 留痕不计入 report.passed,**本函数的 verdict 覆盖逻辑对
    趋势维度永不触发**)。
    """
    report = run_validator_pipeline(
        chapter_text,
        chapter_card=chapter_card,
        workspace_id=workspace_id,
        known_character_ids=known_character_ids,
        timeline_entries=timeline_entries,
        character_records=character_records,
        baseline_style_fingerprint=baseline_style_fingerprint,
        current_style_fingerprint=current_style_fingerprint,
        baseline_version_id=baseline_version_id,
        current_version_id=current_version_id,
        cross_chapter_baseline_fingerprint=cross_chapter_baseline_fingerprint,
        cross_chapter_baseline_chapter_id=cross_chapter_baseline_chapter_id,
        cross_chapter_current_fingerprint=cross_chapter_current_fingerprint,
        workspace_default_voice=workspace_default_voice,
        mini_plan_event=mini_plan_event,
        is_first_chapter=is_first_chapter,
        style_trend_history=style_trend_history,
        # v2 W3.5-s7:current 被 word_count_trim 改写 → style_drift 句均降 warn-only。
        current_word_count_trimmed=current_word_count_trimmed,
        executable_constraint_plan=executable_constraint_plan,
        allow_word_count_tolerance=allow_word_count_tolerance,
    )
    # forbidden 字面命中与后续 semantic crossing 检查同源于 ChapterCard 的
    # 同一条规则，必须先写成同一个稳定 constraint_id。后续 LLM 结果按该 ID
    # 合并，不能在 failed_constraints 里再造第二个“forbidden 越界”身份。
    literal_forbidden = next(
        (item for item in report.results if item.name == "forbidden"), None
    )
    if literal_forbidden is not None and chapter_card.forbidden:
        literal_hits = set(literal_forbidden.details.get("forbidden_hits", []))
        if executable_constraint_plan is not None:
            planned_forbidden = executable_constraint_plan.by_kind("forbidden")
            review = _merge_constraint_results(
                review,
                [
                    item.as_constraint_result(
                        status="fail" if item.rule_text in literal_hits else "pass",
                        details=(
                            f"正文包含 forbidden 字面命中：{item.rule_text}"
                            if item.rule_text in literal_hits
                            else ""
                        ),
                    )
                    for item in planned_forbidden
                ],
            )
        else:
            source_path = f"chapter_cards/{chapter_card.chapter_id}.forbidden"
            review = _merge_constraint_results(
                review,
                [
                    ConstraintResult(
                        constraint_id=stable_constraint_id(
                            source_path=source_path,
                            domain="forbidden",
                            rule_text=rule,
                        ),
                        domain="forbidden",
                        source_path=source_path,
                        rule_text=rule,
                        status="fail" if rule in literal_hits else "pass",
                        details=(
                            f"正文包含 forbidden 字面命中：{rule}"
                            if rule in literal_hits
                            else ""
                        ),
                    )
                    for rule in chapter_card.forbidden
                ],
            )
    structured_observations = [
        *report.hard_violations,
        *report.warning_signals,
    ]
    if structured_observations:
        # 确定性 HARD 失败用计划 ID 写入既有结构化观察；WARN 仍绝不进入
        # failed_constraints、verdict 或 validator_failure_details，且只有批准 final
        # 才可能按 audience 路由。
        review = review.model_copy(
            update={
                "constraint_violations": list(review.constraint_violations)
                + structured_observations
            }
        )
    if report.passed:
        return review
    severity = classify_validator_severity(report)
    verdict: Literal["revise", "rewrite"] = (
        "revise" if severity == "light" else "rewrite"
    )
    overrides = merge_into_review_verdict_overrides(
        report=report,
        existing_failed_constraints=list(review.failed_constraints),
        verdict=verdict,
    )
    _log.info(
        "validator_pipeline_verdict_dispatched",
        chapter_id=chapter_card.chapter_id,
        workspace_id=workspace_id,
        severity=severity,
        verdict=verdict,
        failed_validators=report.failed_constraint_names,
    )
    return review.model_copy(update=overrides)


def _apply_must_achieve_validator(
    *,
    review: ReviewResult,
    chapter_text: str,
    chapter_card: ChapterCard,
    workspace_id: str,
    router: ModelRouter | None,
    client: LLMClient | None,
    model: str | None,
    executable_constraint_plan: ExecutableConstraintPlan | None = None,
    character_records: list[Character] | None = None,
    source_review: ReviewResult | None = None,
    revision_contract: ProseRevisionContract | None = None,
) -> ReviewResult:
    """P7-B-06:Reviewer + ``_apply_validator_pipeline`` 后调度独立 must_achieve
    LLM validator agent(N 次独立 yes/no 调用)。

    与 ``_apply_validator_pipeline`` 互补:
    - pipeline 用 substring 检查 must_achieve(确定性 + 严格,允许 LLM fallback)
    - 本 helper 用独立 LLM agent 检查 must_achieve(温度 0.3 / 每次问 1 条)

    串行模式:Reviewer → pipeline 覆盖(若失败)→ 本 helper 覆盖(若 validator
    失败)。任一覆盖把 verdict 设为 rewrite + hard_constraints_passed=False +
    合并 failed_constraints。

    决策 3:章节卡 ``must_achieve=[]`` → validator 自身跳过 + 返
    ``ValidationResult(passed=True, skipped=True)`` → 本 helper 返原 review
    不动。

    权威产品路径:``ExecutableConstraintPlan`` 与 ``ModelRouter`` 同时存在时，
    validator setup/传输/解析失败抛 ``MustAchieveTechnicalError``，由 orchestrator
    技术停止，不能降级为 PASS。冻结旧活动 / 直接 client 的 legacy caller 保留
    历史 fail-open。

    v2 间隙 A1(2026-06-13):拆为 ``_judge_must_achieve``(纯判定,可并发)+
    ``_merge_must_achieve``(纯 review 合并,串行按原链序)。本 wrapper 保留
    原签名 + 行为(judge→merge 串行组合),既有 caller / 单测零改动。
    """
    result = _judge_must_achieve(
        chapter_text=chapter_text,
        chapter_card=chapter_card,
        workspace_id=workspace_id,
        router=router,
        client=client,
        model=model,
        executable_constraint_plan=executable_constraint_plan,
        character_records=character_records,
    )
    result = guard_must_fail_to_pass(
        result=result,
        source_review=source_review,
        candidate_text=chapter_text,
        candidate_version_id=review.version_reviewed,
        revision_contract=revision_contract,
        executable_constraint_plan=executable_constraint_plan,
    )
    return _merge_must_achieve(
        review=review,
        result=result,
        chapter_card=chapter_card,
        workspace_id=workspace_id,
    )


def _judge_must_achieve(
    *,
    chapter_text: str,
    chapter_card: ChapterCard,
    workspace_id: str,
    router: ModelRouter | None,
    client: LLMClient | None,
    model: str | None,
    executable_constraint_plan: ExecutableConstraintPlan | None = None,
    character_records: list[Character] | None = None,
) -> ValidationResult:
    """A1:must_achieve 判定阶段(纯 LLM 调用,不碰 review,可并发提交)。"""
    must_achieve = list(chapter_card.must_achieve)
    if executable_constraint_plan is not None:
        executable_constraint_plan.assert_matches_chapter_card(chapter_card)
        must_achieve = [
            item.rule_text
            for item in executable_constraint_plan.by_kind("must_achieve")
        ]
    required_core_state = None
    if executable_constraint_plan is not None and chapter_card.core_state_transitions:
        core_rules = executable_constraint_plan.by_kind("core_state")
        if core_rules:
            from src.core.models.core_state import CORE_STATE_RELATION_EVIDENCE_GUIDANCE

            names = {c.character_id: c.name for c in (character_records or [])}
            objects = {}
            if chapter_card.dramatic_source is not None:
                objects = {
                    item.object_id: item.label
                    for item in chapter_card.dramatic_source.beat_contract.load_bearing_objects
                }
            requirements = tuple(
                f"本章正文明确证明{objects.get(t.entity_id, t.entity_id)}"
                f"（{t.entity_type}:{t.entity_id}）的{t.attribute}为"
                f"{names.get(t.to_value, t.to_value)}。"
                for t in chapter_card.core_state_transitions
            )
            required_core_state = RequiredCoreStateEvidence(
                constraint=core_rules[0], requirements=requirements,
                context=CORE_STATE_RELATION_EVIDENCE_GUIDANCE + "\n"
                "以下迁移理由仅供解释目标关系，不新增必须发生的动作，不能要求兑现其中的未来计划：\n"
                + "\n".join(f"- {t.entity_type}:{t.entity_id}:{t.attribute}: {t.reason}"
                            for t in chapter_card.core_state_transitions),
            )
    return validate_must_achieve(
        chapter_text=chapter_text,
        must_achieve=must_achieve,
        chapter_id=chapter_card.chapter_id,
        workspace_id=workspace_id,
        router=router,
        client=client,
        model=model,
        required_core_state=required_core_state,
        authoritative=(
            executable_constraint_plan is not None and router is not None
        ),
    )


def _merge_constraint_results(
    review: ReviewResult, results: list[ConstraintResult]
) -> ReviewResult:
    """按稳定 ID 合并本轮 validator 事实；同规则多检测采用 fail-wins。"""
    if not results:
        return review
    merged = list(review.constraint_results)
    positions = {result.constraint_id: index for index, result in enumerate(merged)}
    for result in results:
        position = positions.get(result.constraint_id)
        if position is None:
            positions[result.constraint_id] = len(merged)
            merged.append(result)
        else:
            previous = merged[position]
            if (
                previous.domain != result.domain
                or previous.source_path != result.source_path
                or previous.rule_text != result.rule_text
            ):
                raise ValueError(
                    "同一 constraint_id 对应了不同规则语义，已阻止审稿账本合并"
                )
            if previous.status == "fail" or result.status == "fail":
                evidence = list(
                    dict.fromkeys(
                        item for item in (previous.details, result.details) if item
                    )
                )
                merged[position] = result.model_copy(
                    update={"status": "fail", "details": "；".join(evidence)}
                )
            else:
                merged[position] = result
    return review.model_copy(update={"constraint_results": merged})


def _merge_must_achieve(
    *,
    review: ReviewResult,
    result: ValidationResult,
    chapter_card: ChapterCard,
    workspace_id: str,
) -> ReviewResult:
    """A1:must_achieve 合并阶段(纯 review 变换,串行按原链序;与拆分前
    ``_apply_must_achieve_validator`` 后半段逐字一致)。"""
    review = _merge_constraint_results(review, result.constraint_results)
    # passed 涵盖三类情况:① must_achieve=[] skip;② 所有条目 yes;
    # ③ 仅 legacy caller 的 LLM fail-open。权威计划技术失败在此之前已抛出。
    if result.passed:
        return review

    # validator 失败:覆盖 verdict=rewrite + hard_constraints_passed=False +
    # 合并 failed_constraints(reviewer 自报 + 本 validator missing items)。
    # missing 名前缀 "must_achieve: " 让 LogsPage / metrics 能按维度聚合。
    missing_constraint_names = [
        f"must_achieve: {item}" for item in result.missing_items
    ]
    missing_constraint_names.extend(
        f"{item.domain}: {item.rule_text}"
        for item in result.constraint_results
        if item.status == "fail" and item.domain != "must_achieve"
    )
    merged_failed = list(
        dict.fromkeys(list(review.failed_constraints) + missing_constraint_names)
    )
    _log.info(
        "must_achieve_validator_overrode_verdict",
        chapter_id=chapter_card.chapter_id,
        workspace_id=workspace_id,
        original_verdict=review.verdict,
        missing_count=len(result.missing_items),
        llm_calls_count=result.llm_calls_count,
    )
    return review.model_copy(
        update={
            "verdict": "rewrite",
            "hard_constraints_passed": False,
            "failed_constraints": merged_failed,
        }
    )


def _apply_forbidden_crossing_validator(
    *,
    review: ReviewResult,
    chapter_text: str,
    chapter_card: ChapterCard,
    workspace_id: str,
    router: ModelRouter | None,
    client: LLMClient | None,
    model: str | None,
    executable_constraint_plan: ExecutableConstraintPlan | None = None,
) -> ReviewResult:
    """v2 W3.5 补强3:写后正文级 forbidden 越界 LLM-judge(HARD 硬闸)。

    读本章正文 vs 卡 ``forbidden`` 清单,判定是否**真实发生**了被禁止的事(尤其
    隐式"进入类"越界 = 终点 overshoot)。越界确凿 → 覆盖 verdict=rewrite + 合并
    failed_constraints(克隆 ``_merge_must_achieve`` HARD 语义)→ reviser 删越界
    重写;budget 耗尽 → 升人工。

    - 卡 ``forbidden=[]`` / 正文空 → validator skip(passed=True)→ 返原 review。
    - LLM 异常 → validator fail-open(passed=True)→ 返原 review(宁可漏报不误杀,
      与零误杀同向:judge 故障绝不误触发 rewrite)。

    v2 间隙 A1 同型:拆 ``_judge_forbidden_crossing``(纯判定,可并发)+
    ``_merge_forbidden_crossing``(纯 review 合并,串行按原链序)。本 wrapper 保留
    原签名(judge→merge 串行组合)供串行回退路径 + 单测调用。
    """
    return _merge_forbidden_crossing(
        review=review,
        result=_judge_forbidden_crossing(
            chapter_text=chapter_text,
            chapter_card=chapter_card,
            workspace_id=workspace_id,
            router=router,
            client=client,
            model=model,
            executable_constraint_plan=executable_constraint_plan,
        ),
        chapter_card=chapter_card,
        workspace_id=workspace_id,
    )


def _judge_forbidden_crossing(
    *,
    chapter_text: str,
    chapter_card: ChapterCard,
    workspace_id: str,
    router: ModelRouter | None,
    client: LLMClient | None,
    model: str | None,
    executable_constraint_plan: ExecutableConstraintPlan | None = None,
) -> ForbiddenCrossingResult:
    """补强3:forbidden 越界判定阶段(纯 LLM 调用,不碰 review,可并发提交)。"""
    forbidden = list(chapter_card.forbidden)
    must_achieve = list(chapter_card.must_achieve)
    if executable_constraint_plan is not None:
        executable_constraint_plan.assert_matches_chapter_card(chapter_card)
        forbidden = [
            item.rule_text for item in executable_constraint_plan.by_kind("forbidden")
        ]
        must_achieve = [
            item.rule_text
            for item in executable_constraint_plan.by_kind("must_achieve")
        ]
    return validate_forbidden_crossing(
        chapter_text=chapter_text,
        forbidden=forbidden,
        must_achieve=must_achieve,
        chapter_id=chapter_card.chapter_id,
        workspace_id=workspace_id,
        router=router,
        client=client,
        model=model,
    )


def _merge_forbidden_crossing(
    *,
    review: ReviewResult,
    result: ForbiddenCrossingResult,
    chapter_card: ChapterCard,
    workspace_id: str,
) -> ReviewResult:
    """补强3:forbidden 越界合并阶段(纯 review 变换,HARD 覆盖,克隆 must_achieve)。"""
    review = _merge_constraint_results(review, result.constraint_results)
    # passed 涵盖:① forbidden=[] skip;② 无越界;③ LLM fail-open。都不覆盖 verdict。
    if result.passed:
        return review

    # 越界确凿:展示层只保留一个 canonical ``forbidden`` 域；具体哪条规则、
    # 字面/语义证据全部在上方同 ID constraint_results，避免同一规则双计。
    merged_failed = [
        item
        for item in review.failed_constraints
        if item != "forbidden" and not item.startswith("forbidden越界:")
    ]
    merged_failed.append("forbidden")
    _log.info(
        "forbidden_crossing_validator_overrode_verdict",
        chapter_id=chapter_card.chapter_id,
        workspace_id=workspace_id,
        original_verdict=review.verdict,
        violation_count=len(result.violations),
        llm_calls_count=result.llm_calls_count,
    )
    return review.model_copy(
        update={
            "verdict": "rewrite",
            "hard_constraints_passed": False,
            "failed_constraints": merged_failed,
        }
    )


def _load_prev_chapter_card_safe(
    *, paths: WorkspacePaths, chapter_id: str
) -> ChapterCard | None:
    """从 SSOT JSON 文件加载**全局**上一章 ``ChapterCard``,失败 swallow + log +
    返 ``None``(fail-open,不阻塞主流程)。

    供 ``_apply_continuity_validator`` + 写前 ``must_feasibility`` 预检调用。

    v2 W3.5-S1 修复(2026-06-23):原按 ``chapter_number - 1`` 派生上一章,但
    ``chapter_number`` 是**卷内**章号(卷2 起每卷从 1 重置),多卷书会错位 ——
    例 ch-0010(卷2 第 3 章,chapter_number=3)派生出 ``ch-0002`` 而非 ``ch-0009``,
    把卷1 早期章节当"上一章"喂进 must_feasibility / continuity,污染判定上下文
    (s7-accept-v1 ch-0010 must_feasibility "consumed" 误判 = 本 bug + mini_plan
    走位 + deepseek judge 把空间冲突误贴 consumed 三因叠加;详
    ``docs/acceptance/v2_w3_5_s1_deepseek_switch_audit.md`` §3)。改用**全局**
    chapter_id 派生(``_infer_prev_chapter_id``,跨卷连续 ch-NNNN)。

    边界 / 决策:
    - ``chapter_id`` 无前一章(全书首章 ch-0001 / 非法格式)→ 返 None(Q4/预检 skip)。
    - 跨卷边界:卷2 首章(ch-0008)上一章 = 上卷末章(ch-0007),叙事顺序连续,正确。
    - 文件不存在 / JSON 损坏 / Pydantic 校验失败 → swallow + warn + 返 None,让
      Q4 自动 skip(对齐 ``chapter_summary_extractor`` fail-open 模式)。
    """
    prev_chapter_id = _infer_prev_chapter_id(chapter_id)
    if prev_chapter_id is None:
        return None
    card_path = paths.cards_dir / f"{prev_chapter_id}.json"
    if not card_path.is_file():
        _log.debug(
            "load_prev_chapter_card_not_found",
            prev_chapter_id=prev_chapter_id,
            card_path=str(card_path),
        )
        return None
    try:
        return ChapterCard.model_validate_json_storage(
            card_path.read_text(encoding="utf-8")
        )
    except Exception as exc:  # noqa: BLE001 — fail-open
        _log.warning(
            "load_prev_chapter_card_failed",
            prev_chapter_id=prev_chapter_id,
            card_path=str(card_path),
            error_type=type(exc).__name__,
            error_message=str(exc),
        )
        return None


def _apply_continuity_validator(
    *,
    review: ReviewResult,
    chapter_text: str,
    chapter_card: ChapterCard,
    workspace_id: str,
    router: ModelRouter | None,
    client: LLMClient | None,
    model: str | None,
    prev_chapter_card: ChapterCard | None = None,
    executable_constraint_plan: ExecutableConstraintPlan | None = None,
    continuity_capsule: ProvenanceBoundContinuityCapsule | None = None,
) -> ReviewResult:
    """v1.0.x wave 3 Commit D:Reviewer + ``_apply_validator_pipeline`` +
    ``_apply_must_achieve_validator`` 后调度衔接桥 LLM validator(§4.3 全 3 条,
    2 HARD + 1 SOFT)。

    v1.0.x wave 4 session 5 起接入 ``prev_chapter_card`` 入参激活 Q4 chapter_card
    自洽性(SOFT;session 4 schema / prompt / mock 已就位)— caller 用
    ``_load_prev_chapter_card_safe`` 提前加载,本 helper 透传给 ``validate_continuity``。
    ``prev_chapter_card=None`` → Q4 自动 skip(首章 / 文件缺失 / 加载失败的统一兜底)。

    与 ``_apply_validator_pipeline`` 互补:
    - pipeline(Commit D 起)只跑 §4.1 节奏类 + §4.2 一致性类(§4.3 字面 substring
      比对器已在 ``validator_pipeline.py`` L129+L133 删除调用,函数本身保留)。
    - 本 helper 用独立 LLM agent 接管 §4.3 全 3 条语义判定(温度 0.3 / 每次问
      1 个 Q yes/no/uncertain)。

    串行模式:Reviewer → pipeline 覆盖(若失败)→ must_achieve_validator 覆盖
    (若失败)→ 本 helper 覆盖(若 HARD fail)/ 合并 suggestions(若 SOFT fail)。

    HARD fail(Q1 unfinished_actions / Q2 scene_same)→ 覆盖 verdict=rewrite +
    hard_constraints_passed=False + 合并 failed_constraints with prefix
    ``"continuity: <name>"``;**任一 HARD fail 触发覆盖**(对齐
    must_achieve_validator)。

    SOFT fail(Q3 time_gap)→ **不动 verdict**,把 ``soft_warnings`` 短句列表
    合并进 ``review.suggestions``,让 Reviser/Writer 下次拿到信号(plan §1.5
    字面"做了不等于没做")。

    skipped(首章 / 空 prev_chapter_tail / 三 Q 条件全不满足)→ 返原 review 不动。
    fail_open(LLM 抛异常 / 返非法 JSON / setup 失败)→ 返原 review 不动 + log
    warn 已在 validator 内 emit(对齐 must_achieve_validator 同型)。

    自决项:本 helper 不在 ``validate_continuity`` 调用外层再加一层 try/except
    — validator 自身已 fail-open 包覆所有 LLM 异常,本 helper 信任其契约。
    若 validator 自身代码 bug 致非预期异常,**应当**冒泡到 orchestrator 主循环
    暴露 bug(对齐 P1-B-13 "agent 抛异常 state 保持原值并向上传播" 不变量 +
    ``_apply_must_achieve_validator`` 同型决策)。

    v2 间隙 A1(2026-06-13):拆为 ``_judge_continuity`` + ``_merge_continuity``
    (同 must_achieve 模式)。本 wrapper 不传 ``q_executor`` → continuity 内部
    Q 串行(保持原行为,既有单测零改动)。
    """
    return _merge_continuity(
        review=review,
        result=_judge_continuity(
            chapter_text=chapter_text,
            chapter_card=chapter_card,
            workspace_id=workspace_id,
            router=router,
            client=client,
            model=model,
            prev_chapter_card=prev_chapter_card,
            executable_constraint_plan=executable_constraint_plan,
            continuity_capsule=continuity_capsule,
        ),
        chapter_card=chapter_card,
        workspace_id=workspace_id,
    )


def _judge_continuity(
    *,
    chapter_text: str,
    chapter_card: ChapterCard,
    workspace_id: str,
    router: ModelRouter | None,
    client: LLMClient | None,
    model: str | None,
    prev_chapter_card: ChapterCard | None = None,
    q_executor: Executor | None = None,
    executable_constraint_plan: ExecutableConstraintPlan | None = None,
    continuity_capsule: ProvenanceBoundContinuityCapsule | None = None,
) -> ContinuityValidationResult:
    """A1:continuity 判定阶段(纯 LLM 调用,可并发提交)。

    ``q_executor`` 透传给 ``validate_continuity`` 做 Q1-Q5 内部并发(由
    ``_run_post_review_llm_gates`` 在闸间并发模式下传入**独立**池实例,与
    外层闸间池不同实例 → 无嵌套 ThreadPoolExecutor 死锁)。``None`` → Q 串行。
    """
    if executable_constraint_plan is not None:
        executable_constraint_plan.assert_matches_chapter_card(chapter_card)
    return validate_continuity(
        chapter_text=chapter_text,
        chapter_card=chapter_card,
        chapter_id=chapter_card.chapter_id,
        workspace_id=workspace_id,
        router=router,
        client=client,
        model=model,
        prev_chapter_card=prev_chapter_card,
        executor=q_executor,
        continuity_capsule=continuity_capsule,
    )


def _merge_continuity(
    *,
    review: ReviewResult,
    result: ContinuityValidationResult,
    chapter_card: ChapterCard,
    workspace_id: str,
) -> ReviewResult:
    """A1:continuity 合并阶段(纯 review 变换,串行按原链序;与拆分前
    ``_apply_continuity_validator`` 后半段逐字一致)。"""
    review = _merge_constraint_results(review, result.constraint_results)
    # skipped / fail_open / 全过(无 HARD fail + 无 SOFT warning)→ review 不动。
    if not result.failed_hard_checks and not result.soft_warnings:
        return review

    update_dict: dict[str, object] = {}

    # HARD fail:覆盖 verdict=rewrite + 合并 failed_constraints with "continuity: " prefix。
    if result.failed_hard_checks:
        hard_marker_names = [
            f"continuity: {name}" for name in result.failed_hard_checks
        ]
        merged_failed = list(
            dict.fromkeys(list(review.failed_constraints) + hard_marker_names)
        )
        update_dict["verdict"] = "rewrite"
        update_dict["hard_constraints_passed"] = False
        update_dict["failed_constraints"] = merged_failed
        _log.info(
            "continuity_validator_overrode_verdict",
            chapter_id=chapter_card.chapter_id,
            workspace_id=workspace_id,
            original_verdict=review.verdict,
            failed_hard_count=len(result.failed_hard_checks),
            failed_hard_names=list(result.failed_hard_checks),
            llm_calls_count=result.llm_calls_count,
        )

    # SOFT warning:合并进 suggestions,不动 verdict(Q3 time_gap 走此路径,plan §1.5)。
    if result.soft_warnings:
        merged_suggestions = list(review.suggestions) + list(result.soft_warnings)
        update_dict["suggestions"] = merged_suggestions
        warning_details = result.soft_warning_details or [
            {
                "q_name": f"legacy_{index}",
                "evidence": warning,
                "message": warning,
            }
            for index, warning in enumerate(result.soft_warnings, start=1)
        ]
        continuity_warnings = [
            build_soft_warning(
                domain="chapter.continuity",
                source_path=(f"runtime.validator.continuity.soft.{detail['q_name']}"),
                rule_text=(
                    "衔接软观察应交给下一章规划，不能触发当前正文 rewrite："
                    f"{detail['q_name']}"
                ),
                evidence=detail["evidence"],
                audiences=["planner"],
                scope="next_chapter",
                confidence="medium",
                suggested_action="下一章出卡时校准时间、到场或章节卡衔接；不回写为 HARD。",
                explanation=detail["message"],
            )
            for detail in warning_details
        ]
        update_dict["constraint_violations"] = (
            list(review.constraint_violations) + continuity_warnings
        )
        _log.info(
            "continuity_validator_appended_soft_warnings",
            chapter_id=chapter_card.chapter_id,
            workspace_id=workspace_id,
            soft_warnings_count=len(result.soft_warnings),
            llm_calls_count=result.llm_calls_count,
        )

    return review.model_copy(update=update_dict)


def _apply_setting_consistency_validator(
    *,
    review: ReviewResult,
    chapter_text: str,
    chapter_card: ChapterCard,
    workspace_id: str,
    world_contract: SettingContract | None,
    character_records: list[Character] | None,
    router: ModelRouter | None,
    client: LLMClient | None,
    model: str | None,
    executable_constraint_plan: ExecutableConstraintPlan | None = None,
    core_state_projection: CoreStateProjection | None = None,
) -> ReviewResult:
    """v1.1 wave 6 session 3(B1):设定一致性校验(下游审稿期 LLM-as-judge)。

    在 Reviewer + pipeline + must_achieve + continuity 之后调度独立 LLM-judge,把
    本章正文与设定契约(world 不变规则 + 出场角色固定身份)比对,**只报正文违反
    契约的点**(冲突才报警,护留白)。治 B1 30 章长跑 A 类设定一致性 bug
    (绣春刀穿帮 / 肉体在不在 / 沈舟刑警身份写串 / 病历张冠李戴)。

    R2:validator 只返回已完成来源/正文双向回查的结构化 violation。HARD 覆盖
    ``verdict=rewrite``；SOFT 只追加 suggestions，不改变 verdict。

    数据来源:``world_contract`` 由 caller 从 ``outline/world.json`` 加载透传;出场
    角色 identity_facets 从预读 ``character_records`` 筛 ``related_characters`` 命中
    且有多情境身份的角色(普通角色无 facets,跳过)。契约 + 身份都空 → validator
    内 skip；权威 contract 下传输/解析失败有界重试后抛 technical error，legacy/import
    才允许显式降级。

    **A5 扩展(v1.1 wave 6 session 4)**:同时从 ``character_records`` 筛出场角色的
    核心动机(``goals_long_term`` + ``goals_short_term``)注入,让同一 LLM-judge 顺带
    校验"正文即兴加的概念是否与已确立动机打架"(深化合法,根本矛盾才报 —— 治反派
    扁平 / 进化论失控)。复用 B1 审稿期 wiring,不新造 agent。

    v2 间隙 A1(2026-06-13):拆为 ``_judge_setting_consistency``(含 identity /
    motivation 预处理 + LLM 调用,可并发)+ ``_merge_setting_consistency``。
    """
    return _merge_setting_consistency(
        review=review,
        result=_judge_setting_consistency(
            chapter_text=chapter_text,
            chapter_card=chapter_card,
            workspace_id=workspace_id,
            world_contract=world_contract,
            character_records=character_records,
            router=router,
            client=client,
            model=model,
            executable_constraint_plan=executable_constraint_plan,
            core_state_projection=core_state_projection,
        ),
        chapter_card=chapter_card,
        workspace_id=workspace_id,
    )


def _judge_setting_consistency(
    *,
    chapter_text: str,
    chapter_card: ChapterCard,
    workspace_id: str,
    world_contract: SettingContract | None,
    character_records: list[Character] | None,
    router: ModelRouter | None,
    client: LLMClient | None,
    model: str | None,
    executable_constraint_plan: ExecutableConstraintPlan | None = None,
    core_state_projection: CoreStateProjection | None = None,
) -> SettingConsistencyResult:
    """A1:setting 判定阶段(identity/motivation 预处理 + LLM 调用,可并发)。"""
    contract = world_contract or SettingContract()
    if executable_constraint_plan is not None:
        executable_constraint_plan.assert_matches_chapter_card(chapter_card)
        executable_constraint_plan.assert_matches_setting_contract(world_contract)
        executable_constraint_plan.assert_matches_character_records(
            chapter_card=chapter_card,
            character_records=character_records,
        )
        executable_constraint_plan.assert_matches_core_state(
            chapter_card=chapter_card,
            projection=core_state_projection,
        )
    identities: list[CharacterIdentityRef] = []
    motivations: list[CharacterMotivationRef] = []
    if character_records:
        related = set(chapter_card.related_characters)
        for c in character_records:
            if c.character_id not in related:
                continue
            if c.identity_facets:
                identities.append(
                    CharacterIdentityRef(
                        character_name=c.name,
                        facets=tuple(c.identity_facets),
                    )
                )
            # A5:核心动机 = 长期 + 短期目标(过滤空串);有动机才注入。
            goals = tuple(
                g.strip()
                for g in (*c.goals_long_term, *c.goals_short_term)
                if g and g.strip()
            )
            if goals:
                motivations.append(
                    CharacterMotivationRef(character_name=c.name, motivations=goals)
                )

    return validate_setting_consistency(
        chapter_text=chapter_text,
        contract=contract,
        character_identities=identities,
        character_motivations=motivations,
        chapter_id=chapter_card.chapter_id,
        workspace_id=workspace_id,
        router=router,
        client=client,
        model=model,
        core_state_projection=core_state_projection,
        core_state_constraint=(
            executable_constraint_plan.by_kind("core_state")[0]
            if executable_constraint_plan is not None
            and executable_constraint_plan.by_kind("core_state")
            else None
        ),
        book_clock_constraint=(
            executable_constraint_plan.by_kind("book_clock")[0]
            if executable_constraint_plan is not None
            and executable_constraint_plan.by_kind("book_clock")
            and not uses_existing_q3_for_hard_book_clock(chapter_card)
            else None
        ),
    )


def _merge_setting_consistency(
    *,
    review: ReviewResult,
    result: SettingConsistencyResult,
    chapter_card: ChapterCard,
    workspace_id: str,
) -> ReviewResult:
    """R2:结构化 setting 违规按 HARD/SOFT 合并，串并行共用。"""
    review = _merge_constraint_results(review, result.constraint_results)
    # skip / legacy fail-open / 无违反 → review 不动。
    if not result.violations:
        return review

    hard_violations = [
        violation for violation in result.violations if violation.severity == "hard"
    ]
    soft_violations = [
        violation for violation in result.violations if violation.severity == "soft"
    ]
    merged_structured = list(review.constraint_violations) + list(result.violations)
    merged_suggestions = list(review.suggestions) + [
        (
            f"设定一致性[SOFT:{violation.constraint_id[:12]}]:"
            f"规则「{violation.rule_quote}」；正文「{violation.evidence_quote}」"
        )
        for violation in soft_violations
    ]
    update: dict[str, object] = {
        "constraint_violations": merged_structured,
        "suggestions": merged_suggestions,
    }

    if hard_violations:
        hard_failures = [
            (
                f"setting:{violation.constraint_id}:"
                f"规则「{violation.rule_quote}」；正文「{violation.evidence_quote}」"
            )
            for violation in hard_violations
        ]
        update.update(
            {
                "verdict": "rewrite",
                "hard_constraints_passed": False,
                "failed_constraints": list(
                    dict.fromkeys(list(review.failed_constraints) + hard_failures)
                ),
            }
        )
        _log.warning(
            "setting_consistency_validator_overrode_verdict",
            chapter_id=chapter_card.chapter_id,
            workspace_id=workspace_id,
            original_verdict=review.verdict,
            hard_violation_ids=[
                violation.constraint_id for violation in hard_violations
            ],
            llm_calls_count=result.llm_calls_count,
        )
    if soft_violations:
        _log.info(
            "setting_consistency_validator_appended_soft_warnings",
            chapter_id=chapter_card.chapter_id,
            workspace_id=workspace_id,
            soft_violation_ids=[
                violation.constraint_id for violation in soft_violations
            ],
            llm_calls_count=result.llm_calls_count,
        )
    return review.model_copy(update=update)


# B4(v1.1 wave 6 session 4):正文跨章去重回看窗口(只与最近 N 章比对,控成本 +
# 重复在临近章最刺眼)。与 cross_chapter_dedup_validator._LOOKBACK_CHAPTERS 对齐
# (judge prompt 渲染"近 N 章"用同值)。
_DEDUP_LOOKBACK_CHAPTERS: int = 5


def _load_prior_chapters_for_dedup(
    *,
    session_factory: PipelineSessionFactory,
    versioning: ChapterVersioning,
    workspace_id: str,
    current_chapter_id: str,
    lookback: int,
) -> list[PriorChapter]:
    """B4:读最近 ``lookback`` 章 approved 终稿正文(供跨章去重初筛)。

    按 ``chapter_id`` 降序(零填充 ch-NNNN 字典序 = 章序)取本章之前的 approved 章;
    逐章读 ``v{current_version}.md``(approved 章 ``current_version`` 即终稿版本)。
    单章读失败(版本文件缺)→ 跳过该章不冒泡(WARN-only 路径,缺一章不影响整体)。
    """
    session = session_factory()
    try:
        rows = session.execute(
            select(ChapterORM.chapter_id, ChapterORM.current_version)
            .where(ChapterORM.workspace_id == workspace_id)
            .where(ChapterORM.chapter_id < current_chapter_id)
            .where(ChapterORM.status == "approved")
            .order_by(ChapterORM.chapter_id.desc())
            .limit(lookback)
        ).all()
    finally:
        session.close()

    prior: list[PriorChapter] = []
    for cid, ver in rows:
        try:
            text = versioning.read_version_content(cid, ver)
        except Exception:  # noqa: BLE001 — 缺版本文件跳过该章,不影响整体
            continue
        if text.strip():
            prior.append(PriorChapter(chapter_id=cid, text=text))
    return prior


def _apply_cross_chapter_dedup_validator(
    *,
    review: ReviewResult,
    chapter_text: str,
    chapter_card: ChapterCard,
    workspace_id: str,
    session_factory: PipelineSessionFactory | None,
    versioning: ChapterVersioning,
    router: ModelRouter | None,
    client: LLMClient | None,
    model: str | None,
) -> ReviewResult:
    """v1.1 wave 6 session 4(B4):正文跨章去重(下游审稿期,代码初筛 + LLM 判意图)。

    在 Reviewer + pipeline + must_achieve + continuity + setting 之后调度:用代码
    相似度初筛找出本章正文与近 N 章正文的疑似重复(句级近重复 / 高频套话),再用
    一次批量 LLM 判每条"注水复读(抓)vs 有意回扣 / 母题呼应 / 章末钩子(护)"。治
    B1 30 章长跑 D 类注水套路(破空 10×/撕裂 62×/进化 13×)+ B 类玉佩逐句重写。

    **SOFT / WARN-only**(对齐 wave 6 全 WARN 默认 + §0 第一原则):判为注水的条目
    合并进 ``review.suggestions``,**不覆盖 verdict**。**为何不 HARD**:"重复是注水
    还是有意回扣"是文学语义判断,LLM-judge 可能误把母题呼应 / 钩子当注水(宁可漏报
    不误伤)。HARD 升级留 wave 6 末约束预算实测后定。

    **全 swallow**:session_factory 为 None(P1 兼容)/ 无前序章 / 任何异常 → 返原
    review 不动(审稿主流程不因去重初筛失败而中断)。与 D4(卡级 must_achieve)分工:
    本 agent 只管正文文本相似度,不看卡级必达事件。

    v2 间隙 A1(2026-06-13):拆为 ``_judge_cross_chapter_dedup``(读 DB 前序章
    初筛 + LLM 判 + 全 swallow,可并发;失败/无前序章 → None)+
    ``_merge_cross_chapter_dedup``(None / 无 violations → review 不动)。
    """
    return _merge_cross_chapter_dedup(
        review=review,
        result=_judge_cross_chapter_dedup(
            chapter_text=chapter_text,
            chapter_card=chapter_card,
            workspace_id=workspace_id,
            session_factory=session_factory,
            versioning=versioning,
            router=router,
            client=client,
            model=model,
        ),
        chapter_card=chapter_card,
        workspace_id=workspace_id,
    )


def _judge_cross_chapter_dedup(
    *,
    chapter_text: str,
    chapter_card: ChapterCard,
    workspace_id: str,
    session_factory: PipelineSessionFactory | None,
    versioning: ChapterVersioning,
    router: ModelRouter | None,
    client: LLMClient | None,
    model: str | None,
) -> DedupResult | None:
    """A1:dedup 判定阶段(读 DB 前序章 + LLM,全 swallow,可并发)。

    返 ``None`` 表示"无判定结果"(session_factory 为 None / 无前序章 / 任何
    异常)→ merge 阶段视同 review 不动。异常 swallow + warn 在本阶段 emit。
    """
    if session_factory is None:
        return None
    try:
        prior_chapters = _load_prior_chapters_for_dedup(
            session_factory=session_factory,
            versioning=versioning,
            workspace_id=workspace_id,
            current_chapter_id=chapter_card.chapter_id,
            lookback=_DEDUP_LOOKBACK_CHAPTERS,
        )
        if not prior_chapters:
            return None
        return validate_cross_chapter_dedup(
            current_text=chapter_text,
            prior_chapters=prior_chapters,
            chapter_id=chapter_card.chapter_id,
            workspace_id=workspace_id,
            router=router,
            client=client,
            model=model,
        )
    except Exception as exc:  # noqa: BLE001 — WARN-only,全 swallow 不阻塞主流程
        _log.warning(
            "cross_chapter_dedup_check_failed",
            chapter_id=chapter_card.chapter_id,
            workspace_id=workspace_id,
            error_type=type(exc).__name__,
            error_message=str(exc),
        )
        return None


def _merge_cross_chapter_dedup(
    *,
    review: ReviewResult,
    result: DedupResult | None,
    chapter_card: ChapterCard,
    workspace_id: str,
) -> ReviewResult:
    """A1:dedup 合并阶段(None / 无 violations → 不动;与拆分前后半段一致)。"""
    # v1.1 wave 5 s1(M1)台账 record 已断开(2026-06-05):原型 A/B 实证 M1 信号源
    # (B4)+ 注入点(卡片层)双错位,对读者套话无效。M1 从管线下线,style_controller.py
    # 保留作通用件(详 docs/decisions/v1_1_w5_s1_m1_postmortem.md)。

    if result is None or not result.violations:
        return review

    merged_suggestions = list(review.suggestions) + list(result.violations)
    dedup_warnings = [
        build_soft_warning(
            domain="chapter.dedup",
            source_path="runtime.validator.cross_chapter_dedup",
            rule_text="跨章疑似注水复读只作下一章写作 watchlist",
            evidence=violation,
            audiences=["writer"],
            scope="next_chapter",
            confidence="medium",
            suggested_action="下一章避免机械复用同一短语或句式；有意母题回扣仍可保留。",
            explanation="LLM 已把候选判为 padding，但文学回响存在误判可能，故不设 HARD。",
        )
        for violation in result.violations
    ]
    _log.info(
        "cross_chapter_dedup_validator_appended_soft_warnings",
        chapter_id=chapter_card.chapter_id,
        workspace_id=workspace_id,
        violation_count=len(result.violations),
        candidate_count=result.candidates_count,
        llm_calls_count=result.llm_calls_count,
    )
    return review.model_copy(
        update={
            "suggestions": merged_suggestions,
            "constraint_violations": list(review.constraint_violations)
            + dedup_warnings,
        }
    )


def _apply_ending_tension_validator(
    *,
    review: ReviewResult,
    chapter_text: str,
    chapter_card: ChapterCard,
    workspace_id: str,
    session_factory: PipelineSessionFactory | None,
    router: ModelRouter | None,
    client: LLMClient | None,
    model: str | None,
) -> ReviewResult:
    """v1.1 wave 6 session 4(B6):终局张力曲线(**仅全书终章**触发,审稿期 LLM-judge)。

    判终章结局是否与前文张力断裂(强行宣告岁月静好,治 bug D5)。**抓"强行 / 断裂",
    不抓"平静收尾"本身**(护 §0:平静 / 留白 / 余韵收尾合法)。

    **门控(两段廉价 gate)**:① ``chapter_card.is_arc_finale`` 为 False → 直接返回
    (绝大多数章不查 DB);② 查 ``WorkspaceORM.total_chapters``,本章非全书最后一章
    (``chapter_seq < total``)→ 返回。两 gate 都过才发起 1 次 LLM 调用。

    **SOFT / WARN-only,全 swallow**:abrupt 时把 evidence 合并进 ``review.suggestions``,
    不覆盖 verdict;session_factory 为 None / 任何异常 → 返原 review。

    v2 间隙 A1(2026-06-13):拆为 ``_judge_ending_tension``(门控 + 查 DB +
    LLM,全 swallow,可并发;门控未过/异常 → None)+ ``_merge_ending_tension``。
    """
    return _merge_ending_tension(
        review=review,
        result=_judge_ending_tension(
            chapter_text=chapter_text,
            chapter_card=chapter_card,
            workspace_id=workspace_id,
            session_factory=session_factory,
            router=router,
            client=client,
            model=model,
        ),
        chapter_card=chapter_card,
        workspace_id=workspace_id,
    )


def _judge_ending_tension(
    *,
    chapter_text: str,
    chapter_card: ChapterCard,
    workspace_id: str,
    session_factory: PipelineSessionFactory | None,
    router: ModelRouter | None,
    client: LLMClient | None,
    model: str | None,
) -> EndingTensionResult | None:
    """A1:ending_tension 判定阶段(门控 + 查 DB total_chapters + LLM,可并发)。

    返 ``None`` 表示"无判定结果"(session_factory 为 None / 非 arc finale /
    非全书终章 / 任何异常)→ merge 阶段视同 review 不动。
    """
    if session_factory is None or not chapter_card.is_arc_finale:
        return None
    try:
        current_seq = chapter_seq_from_id(chapter_card.chapter_id)
        if current_seq is None:
            return None
        session = session_factory()
        try:
            workspace = session.get(WorkspaceORM, workspace_id)
            total_chapters = workspace.total_chapters if workspace else 0
        finally:
            session.close()
        # 仅全书最后一章触发(arc finale 但非全书终章 → 不查)。
        if total_chapters <= 0 or current_seq < total_chapters:
            return None

        return validate_ending_tension(
            chapter_text=chapter_text,
            tension_level=chapter_card.tension_level,
            chapter_id=chapter_card.chapter_id,
            workspace_id=workspace_id,
            router=router,
            client=client,
            model=model,
        )
    except Exception as exc:  # noqa: BLE001 — WARN-only,全 swallow 不阻塞主流程
        _log.warning(
            "ending_tension_check_failed",
            chapter_id=chapter_card.chapter_id,
            workspace_id=workspace_id,
            error_type=type(exc).__name__,
            error_message=str(exc),
        )
        return None


def _merge_ending_tension(
    *,
    review: ReviewResult,
    result: EndingTensionResult | None,
    chapter_card: ChapterCard,
    workspace_id: str,
) -> ReviewResult:
    """A1:ending_tension 合并阶段(None / passed / 无 evidence → 不动)。"""
    if result is None or result.passed or not result.evidence:
        return review

    warning = f"终局张力:结局与前文张力断裂(强行岁月静好)——{result.evidence}"
    merged_suggestions = list(review.suggestions) + [warning]
    warning_signal = build_soft_warning(
        domain="chapter.ending",
        source_path="runtime.validator.ending_tension",
        rule_text="终局张力与前文断裂需要人工复核，不能自动打回",
        evidence=result.evidence,
        audiences=["planner", "maintainer"],
        scope="maintenance",
        confidence="medium",
        suggested_action="由维护者复核终章设计；如需调整应走人工改稿而非自动 rewrite。",
        explanation=warning,
    )
    _log.info(
        "ending_tension_validator_appended_soft_warning",
        chapter_id=chapter_card.chapter_id,
        workspace_id=workspace_id,
        verdict=result.verdict,
    )
    return review.model_copy(
        update={
            "suggestions": merged_suggestions,
            "constraint_violations": list(review.constraint_violations)
            + [warning_signal],
        }
    )


POST_REVIEW_GATE_LOGICAL_CALL_MAXIMA: dict[str, int] = {
    # must schema 自纠最多 2 个 logical call；continuity 最多 Q1-Q5 各 1；
    # authoritative setting 技术重试最多 2；其余三个 Judge 各最多 1。
    "must_achieve": 2,
    "continuity": 5,
    "forbidden_crossing": 1,
    "setting": 2,
    "dedup": 1,
    "ending": 1,
}
_POST_REVIEW_GATE_LOGICAL_CALL_MAXIMA = POST_REVIEW_GATE_LOGICAL_CALL_MAXIMA
_POST_REVIEW_GATE_REQUIRED_PROVIDER_ATTEMPTS = (
    sum(POST_REVIEW_GATE_LOGICAL_CALL_MAXIMA.values())
    * MAX_PROVIDER_ATTEMPTS_PER_LOGICAL_CALL
)


def _is_post_review_stop(error: BaseException) -> bool:
    """用户取消/进程级停止必须压过同批普通业务失败。"""

    return isinstance(
        error,
        (LLMCancelledError, CancelledError, KeyboardInterrupt, SystemExit),
    )


def _drain_post_review_gate_futures(
    futures: list[Future[Any]],
) -> list[Any]:
    """完整回收已提交 Judge，并按“停止优先、链序业务失败”裁决异常。

    所有 future 都先 ``result()``，因此迟到的成功响应也会先走完它自己的
    AttemptLedger 终态落账；看到取消/KeyboardInterrupt 后只取消尚未开始的
    future，运行中的仍被回收。若同批既有业务异常又有停止信号，最终抛停止。
    """

    results: list[Any] = [None] * len(futures)
    errors: list[BaseException] = []
    for index, future in enumerate(futures):
        try:
            results[index] = future.result()
        except BaseException as exc:  # noqa: BLE001 - 必须回收取消/KeyboardInterrupt
            errors.append(exc)
            if _is_post_review_stop(exc):
                for pending in futures[index + 1 :]:
                    pending.cancel()

    if errors:
        stop = next((error for error in errors if _is_post_review_stop(error)), None)
        selected = stop or errors[0]
        raise selected.with_traceback(selected.__traceback__)
    return results


def _run_post_review_llm_gates(
    *,
    review: ReviewResult,
    chapter_text: str,
    chapter_card: ChapterCard,
    workspace_id: str,
    router: ModelRouter | None,
    client: LLMClient | None,
    model: str | None,
    prev_chapter_card: ChapterCard | None,
    world_contract: SettingContract | None,
    character_records: list[Character] | None,
    session_factory: PipelineSessionFactory | None,
    versioning: ChapterVersioning,
    executable_constraint_plan: ExecutableConstraintPlan | None = None,
    core_state_projection: CoreStateProjection | None = None,
    source_review: ReviewResult | None = None,
    revision_contract: ProseRevisionContract | None = None,
    continuity_capsule: ProvenanceBoundContinuityCapsule | None = None,
) -> ReviewResult:
    """A1(v2 间隙,2026-06-13;W3.5 补强3 2026-06-26 加 forbidden_crossing 第 6 闸):
    写后 6 个独立 LLM 闸的统一调度。

    覆盖 ``_apply_validator_pipeline``(纯规则,前)与
    ``_apply_paragraph_collapse_override``(兜底,后)之间的 6 个 LLM-judge 闸:
    **must_achieve → continuity → forbidden_crossing → setting → dedup → ending**(原链顺序)。

    两种模式由 ``settings.post_review_gate_max_concurrency`` 切换:

    - **=1(回退)**:逐个调原 ``_apply_*`` helper —— 与拆分前的串行链**逐字
      等价**(零行为变化、零线程开销)。运维回退开关 / 单测默认走此路。
    - **>1(并发)**:6 个闸的**判定阶段**(``_judge_*``)在 ``gate_ex`` 池并发;
      **合并阶段**(``_merge_*``)仍按上述原链顺序**串行**重放 —— verdict /
      failed_constraints / suggestions 的合并逻辑与回退路径逐字段相同(防回退
      测 ``test_post_review_gate_parallelism`` 锁死并发==串行)。

    **零质量风险设计**:① judge 阶段纯判定不碰 review,并发安全;② merge 串行
    按固定顺序 → 列表拼接顺序确定;③ verdict 覆盖来自 must_achieve /
    continuity / forbidden_crossing 及 R2 setting HARD；setting SOFT、dedup、ending
    只 append suggestions，合并无跨闸依赖;
    ④ 各 judge 经 ``contextvars.copy_context().run`` 提交,structlog workspace
    上下文随线程行(对齐 continuity Q 并发同型,A3② 同根);⑤ continuity
    内部 Q1-Q5 并发用**独立** ``q_ex`` 池(与 ``gate_ex`` 不同实例 → 无嵌套
    ThreadPoolExecutor 死锁,峰值线程 = 2×N);⑥ 原有 SOFT judge 保持既有
    fail-open；权威 must Judge 与 R2 权威 setting Judge 的 setup/传输/解析失败
    均从 future 冒泡为 typed technical error；并发不得把停止语义降级成 pass。
    """
    if executable_constraint_plan is not None:
        executable_constraint_plan.assert_matches_chapter_card(chapter_card)
        executable_constraint_plan.assert_matches_setting_contract(world_contract)
        executable_constraint_plan.assert_matches_character_records(
            chapter_card=chapter_card,
            character_records=character_records,
        )
        executable_constraint_plan.assert_matches_core_state(
            chapter_card=chapter_card,
            projection=core_state_projection,
        )

    n = settings.post_review_gate_max_concurrency
    if n > 1:
        capacity = (
            router if isinstance(router, ProviderAttemptCapacity) else None
        )
        cap_allows_parallel = False
        fallback_reason = "provider_cap_capability_unavailable"
        if capacity is not None:
            try:
                cap_allows_parallel = capacity.can_afford_provider_attempts(
                    _POST_REVIEW_GATE_REQUIRED_PROVIDER_ATTEMPTS
                )
                fallback_reason = "provider_cap_headroom"
            except Exception as exc:  # noqa: BLE001 - 余量不可证实时固定串行
                fallback_reason = "provider_cap_headroom_check_failed"
                _log.error(
                    "post_review_parallel_cap_headroom_check_failed",
                    chapter_id=chapter_card.chapter_id,
                    workspace_id=workspace_id,
                    required_attempts=(
                        _POST_REVIEW_GATE_REQUIRED_PROVIDER_ATTEMPTS
                    ),
                    error_type=type(exc).__name__,
                )
        if not cap_allows_parallel:
            n = 1
            _log.info(
                "post_review_parallel_fallback",
                chapter_id=chapter_card.chapter_id,
                workspace_id=workspace_id,
                reason=fallback_reason,
                required_attempts=_POST_REVIEW_GATE_REQUIRED_PROVIDER_ATTEMPTS,
                logical_call_maxima=_POST_REVIEW_GATE_LOGICAL_CALL_MAXIMA,
            )
    if n <= 1:
        # 串行回退:逐个调原 _apply_*(与拆分前逐字等价,零线程开销)。
        review = _apply_must_achieve_validator(
            review=review,
            chapter_text=chapter_text,
            chapter_card=chapter_card,
            workspace_id=workspace_id,
            router=router,
            client=client,
            model=model,
            executable_constraint_plan=executable_constraint_plan,
            character_records=character_records,
            source_review=source_review,
            revision_contract=revision_contract,
        )
        review = _apply_continuity_validator(
            review=review,
            chapter_text=chapter_text,
            chapter_card=chapter_card,
            workspace_id=workspace_id,
            router=router,
            client=client,
            model=model,
            prev_chapter_card=prev_chapter_card,
            executable_constraint_plan=executable_constraint_plan,
            continuity_capsule=continuity_capsule,
        )
        review = _apply_forbidden_crossing_validator(
            review=review,
            chapter_text=chapter_text,
            chapter_card=chapter_card,
            workspace_id=workspace_id,
            router=router,
            client=client,
            model=model,
            executable_constraint_plan=executable_constraint_plan,
        )
        review = _apply_setting_consistency_validator(
            review=review,
            chapter_text=chapter_text,
            chapter_card=chapter_card,
            workspace_id=workspace_id,
            world_contract=world_contract,
            character_records=character_records,
            router=router,
            client=client,
            model=model,
            executable_constraint_plan=executable_constraint_plan,
            core_state_projection=core_state_projection,
        )
        review = _apply_cross_chapter_dedup_validator(
            review=review,
            chapter_text=chapter_text,
            chapter_card=chapter_card,
            workspace_id=workspace_id,
            session_factory=session_factory,
            versioning=versioning,
            router=router,
            client=client,
            model=model,
        )
        review = _apply_ending_tension_validator(
            review=review,
            chapter_text=chapter_text,
            chapter_card=chapter_card,
            workspace_id=workspace_id,
            session_factory=session_factory,
            router=router,
            client=client,
            model=model,
        )
        if executable_constraint_plan is not None:
            executable_constraint_plan.assert_review_uses_only_planned_hard(review)
        return review

    # 并发判定:gate_ex(闸间)+ q_ex(continuity Q 间,独立池防嵌套死锁)。
    # 每个 judge 经 copy_context().run 提交(独立 Context 副本,规避并发 enter)。
    with (
        ThreadPoolExecutor(max_workers=n) as gate_ex,
        ThreadPoolExecutor(max_workers=n) as q_ex,
    ):
        ma_future = gate_ex.submit(
            contextvars.copy_context().run,
            partial(
                _judge_must_achieve,
                chapter_text=chapter_text,
                chapter_card=chapter_card,
                workspace_id=workspace_id,
                router=router,
                client=client,
                model=model,
                executable_constraint_plan=executable_constraint_plan,
                character_records=character_records,
            ),
        )
        cont_future = gate_ex.submit(
            contextvars.copy_context().run,
            partial(
                _judge_continuity,
                chapter_text=chapter_text,
                chapter_card=chapter_card,
                workspace_id=workspace_id,
                router=router,
                client=client,
                model=model,
                prev_chapter_card=prev_chapter_card,
                q_executor=q_ex,
                executable_constraint_plan=executable_constraint_plan,
                continuity_capsule=continuity_capsule,
            ),
        )
        fc_future = gate_ex.submit(
            contextvars.copy_context().run,
            partial(
                _judge_forbidden_crossing,
                chapter_text=chapter_text,
                chapter_card=chapter_card,
                workspace_id=workspace_id,
                router=router,
                client=client,
                model=model,
                executable_constraint_plan=executable_constraint_plan,
            ),
        )
        setting_future = gate_ex.submit(
            contextvars.copy_context().run,
            partial(
                _judge_setting_consistency,
                chapter_text=chapter_text,
                chapter_card=chapter_card,
                workspace_id=workspace_id,
                world_contract=world_contract,
                character_records=character_records,
                router=router,
                client=client,
                model=model,
                executable_constraint_plan=executable_constraint_plan,
                core_state_projection=core_state_projection,
            ),
        )
        dedup_future = gate_ex.submit(
            contextvars.copy_context().run,
            partial(
                _judge_cross_chapter_dedup,
                chapter_text=chapter_text,
                chapter_card=chapter_card,
                workspace_id=workspace_id,
                session_factory=session_factory,
                versioning=versioning,
                router=router,
                client=client,
                model=model,
            ),
        )
        ending_future = gate_ex.submit(
            contextvars.copy_context().run,
            partial(
                _judge_ending_tension,
                chapter_text=chapter_text,
                chapter_card=chapter_card,
                workspace_id=workspace_id,
                session_factory=session_factory,
                router=router,
                client=client,
                model=model,
            ),
        )

        # 先完整回收全部已提交 future；停止/取消压过同批普通业务失败。随后才按
        # 原链顺序合并，迟到结果不会越过 AttemptLedger 终态落账或改变 merge 顺序。
        (
            ma_result,
            cont_result,
            fc_result,
            setting_result,
            dedup_result,
            ending_result,
        ) = _drain_post_review_gate_futures(
            [
                ma_future,
                cont_future,
                fc_future,
                setting_future,
                dedup_future,
                ending_future,
            ]
        )

        # 合并阶段:严格按原链顺序串行重放。权威 must / setting technical error
        # 已在上方统一回收后冒泡，其余语义与旧串行链一致。
        must_result = guard_must_fail_to_pass(
            result=ma_result,
            source_review=source_review,
            candidate_text=chapter_text,
            candidate_version_id=review.version_reviewed,
            revision_contract=revision_contract,
            executable_constraint_plan=executable_constraint_plan,
        )
        review = _merge_must_achieve(
            review=review,
            result=must_result,
            chapter_card=chapter_card,
            workspace_id=workspace_id,
        )
        review = _merge_continuity(
            review=review,
            result=cont_result,
            chapter_card=chapter_card,
            workspace_id=workspace_id,
        )
        review = _merge_forbidden_crossing(
            review=review,
            result=fc_result,
            chapter_card=chapter_card,
            workspace_id=workspace_id,
        )
        review = _merge_setting_consistency(
            review=review,
            result=setting_result,
            chapter_card=chapter_card,
            workspace_id=workspace_id,
        )
        review = _merge_cross_chapter_dedup(
            review=review,
            result=dedup_result,
            chapter_card=chapter_card,
            workspace_id=workspace_id,
        )
        review = _merge_ending_tension(
            review=review,
            result=ending_result,
            chapter_card=chapter_card,
            workspace_id=workspace_id,
        )
    if executable_constraint_plan is not None:
        executable_constraint_plan.assert_review_uses_only_planned_hard(review)
    return review


def _repeated_hard_failure_ids(
    *, previous: ReviewResult | None, current: ReviewResult
) -> list[str]:
    """识别连续两版同一 HARD ID + 同一非空证据，避免无进展付费循环。"""

    if (
        previous is None
        or current.verdict == "pass"
        or current.version_reviewed != previous.version_reviewed + 1
    ):
        return []

    def evidence_by_id(review: ReviewResult) -> dict[str, str]:
        evidence: dict[str, str] = {}
        for result in review.constraint_results:
            normalized = " ".join(result.details.split())
            if result.status == "fail" and normalized:
                evidence[result.constraint_id] = normalized
        for violation in review.constraint_violations:
            normalized = " ".join(violation.evidence_quote.split())
            if violation.severity == "hard" and normalized:
                evidence[violation.constraint_id] = normalized
        return evidence

    previous_evidence = evidence_by_id(previous)
    current_evidence = evidence_by_id(current)
    return sorted(
        constraint_id
        for constraint_id, evidence in current_evidence.items()
        if previous_evidence.get(constraint_id) == evidence
    )


def _build_rewrite_constraint_guardrail(
    *, review: ReviewResult, chapter_card: ChapterCard
) -> RewriteConstraintGuardrail:
    """R6:上一轮结构化结果直接分成“必须修”与“必须保留”两张表。"""
    del chapter_card  # 规则身份已经冻结在 review.constraint_results 中。
    return RewriteConstraintGuardrail(
        must_fix=[
            result for result in review.constraint_results if result.status == "fail"
        ],
        must_preserve=[
            result for result in review.constraint_results if result.status == "pass"
        ],
        constraint_violations=[
            violation
            for violation in review.constraint_violations
            if violation.severity == "hard"
        ],
        other_failures=_summarize_current_rewrite_failures(review),
    )


def _summarize_current_rewrite_failures(review: ReviewResult) -> list[str]:
    """提炼本轮 rewrite 必须修复的失败项。"""
    failures = list(review.failed_constraints)
    for detail in review.validator_failure_details or []:
        name = str(detail.get("name") or "<unknown>")
        if name not in failures:
            failures.append(name)
    if not failures:
        failures.append(
            f"reviewer verdict={review.verdict}, score_overall={review.score_overall:.1f}"
        )
    return failures


def _partition_mixed_above_max_rewrite(
    *,
    source_content: str,
    validator_failures: list[dict[str, Any]] | None,
    guardrail: RewriteConstraintGuardrail,
    executable_constraint_plan: ExecutableConstraintPlan,
    allow_word_count_tolerance: bool = True,
) -> tuple[
    list[dict[str, Any]] | None,
    RewriteConstraintGuardrail,
    bool,
]:
    """把“超长 + 其他结构化 HARD”拆成先语义、后字数两阶段。

    上游章节卡与 Writer 可见的名义字数目标完全不变；这里只把上一轮
    ``above_max`` 失败从本次语义修复反馈中暂时拿掉。Writer/LOCALIZED patch
    先只修其余 HARD，候选随后仍走完整写后验收；若最终只剩超长，既有
    source-bound ``word_count_trim`` 路由再单独压缩。这样不再要求一次 GLOBAL
    输出同时“补关键事件”和“大幅删正文”。
    """

    if executable_constraint_plan.word_count_policy_version != 0:
        return validator_failures, guardrail, False
    word_count_items = executable_constraint_plan.by_kind("word_count")
    if len(word_count_items) != 1:
        return validator_failures, guardrail, False
    word_count_item = word_count_items[0]
    minimum = word_count_item.parameter("minimum")
    maximum = word_count_item.parameter("maximum")
    if type(minimum) is not int or type(maximum) is not int:
        raise ValueError("word_count 计划参数类型非法")
    _, acceptance_max = word_count_acceptance_bounds(
        word_count_min=minimum,
        word_count_max=maximum,
        allow_word_count_tolerance=allow_word_count_tolerance,
    )
    word_count_failures = [
        failure
        for failure in validator_failures or []
        if failure.get("name") == "word_count"
        and isinstance(failure.get("details"), dict)
        and failure["details"].get("reason") == "above_max"
        and failure["details"].get("actual_word_count") == len(source_content)
        and failure["details"].get("word_count_min") == minimum
        and failure["details"].get("word_count_max") == maximum
        and len(source_content) > acceptance_max
    ]
    if len(word_count_failures) != 1:
        return validator_failures, guardrail, False

    def result_kind(result: ConstraintResult) -> str:
        return executable_constraint_plan.result_constraint(
            result.constraint_id
        ).kind

    def violation_kind(violation: ConstraintViolation) -> str:
        return executable_constraint_plan.hard_violation_constraint(violation).kind

    has_non_word_count_hard = any(
        result_kind(result) != "word_count" for result in guardrail.must_fix
    ) or any(
        violation.severity == "hard" and violation_kind(violation) != "word_count"
        for violation in guardrail.constraint_violations
    )
    if not has_non_word_count_hard:
        return validator_failures, guardrail, False

    removed_violations = [
        violation
        for violation in guardrail.constraint_violations
        if violation.severity == "hard"
        and violation_kind(violation) == "word_count"
    ]
    removable_failures = {
        "word_count",
        f"hard_constraint_regression:{word_count_item.constraint_id}",
        *(
            _format_persisted_constraint_violation(violation)
            for violation in removed_violations
        ),
    }
    effective_guardrail = guardrail.model_copy(
        update={
            "must_fix": [
                result
                for result in guardrail.must_fix
                if result_kind(result) != "word_count"
            ],
            "constraint_violations": [
                violation
                for violation in guardrail.constraint_violations
                if not (
                    violation.severity == "hard"
                    and violation_kind(violation) == "word_count"
                )
            ],
            "other_failures": [
                failure
                for failure in guardrail.other_failures
                if failure not in removable_failures
                and "word_count" not in failure.casefold()
            ],
        }
    )
    effective_failures = [
        failure
        for failure in validator_failures or []
        if failure.get("name") != "word_count"
    ]
    _log.info(
        "mixed_above_max_semantic_repair_staged",
        chapter_id=executable_constraint_plan.chapter_id,
        source_word_count=len(source_content),
        deferred_failure="word_count:above_max",
        remaining_constraint_ids=[
            result.constraint_id for result in effective_guardrail.must_fix
        ]
        + [
            violation.constraint_id
            for violation in effective_guardrail.constraint_violations
            if violation.severity == "hard"
        ],
    )
    return effective_failures or None, effective_guardrail, True


def _partition_mechanical_paragraph_reflow(
    *,
    source_content: str,
    validator_failures: list[dict[str, Any]] | None,
    guardrail: RewriteConstraintGuardrail,
    executable_constraint_plan: ExecutableConstraintPlan,
) -> tuple[
    list[dict[str, Any]] | None,
    RewriteConstraintGuardrail,
    list[dict[str, Any]] | None,
]:
    """把可机械完成的段均修复从 Writer 任务中剥离。

    返回 ``(writer_failures, writer_guardrail, reflow_failures)``。只有源正文可在
    既有句末边界上纯插空行并收敛，且计划中能唯一找到 paragraph style ID 时
    才剥离；否则保持原任务，让既有 GLOBAL 路径 fail-safe 处理。
    """

    preview = reflow_paragraph_mean_only(
        content=source_content,
        validator_failures=validator_failures,
    )
    if not preview.applied:
        return validator_failures, guardrail, None
    paragraph_items = [
        item
        for item in executable_constraint_plan.by_kind("style_drift")
        if item.parameter("dimension") == "paragraph_length_mean"
    ]
    if len(paragraph_items) != 1:
        return validator_failures, guardrail, None

    paragraph_id = paragraph_items[0].constraint_id
    removed_violations = [
        violation
        for violation in guardrail.constraint_violations
        if violation.constraint_id == paragraph_id
    ]
    removable_failures = {
        "style_drift",
        f"hard_constraint_regression:{paragraph_id}",
        *(
            _format_persisted_constraint_violation(violation)
            for violation in removed_violations
        ),
    }
    effective_guardrail = guardrail.model_copy(
        update={
            "constraint_violations": [
                violation
                for violation in guardrail.constraint_violations
                if violation.constraint_id != paragraph_id
            ],
            "other_failures": [
                failure
                for failure in guardrail.other_failures
                if failure not in removable_failures
            ],
        }
    )
    _log.info(
        "paragraph_mean_failure_partitioned_to_code",
        chapter_id=executable_constraint_plan.chapter_id,
        constraint_id=paragraph_id,
        before_mean=preview.before_mean,
        after_mean=preview.after_mean,
        upper_bound=preview.upper_bound,
    )
    return None, effective_guardrail, validator_failures


def _is_mechanical_reflow_only(
    *,
    writer_failures: list[dict[str, Any]] | None,
    guardrail: RewriteConstraintGuardrail,
    reflow_failures: list[dict[str, Any]] | None,
) -> bool:
    """判断本轮是否只剩纯代码段均修复，因而不应调用 Writer。"""

    return bool(
        reflow_failures
        and writer_failures is None
        and not guardrail.must_fix
        and not guardrail.constraint_violations
        and not _revision_contract_other_failures(guardrail)
    )


def _build_mechanical_reflow_writer_output(
    *,
    revision_contract: ProseRevisionContract,
    executable_constraint_plan: ExecutableConstraintPlan,
    validator_failures: list[dict[str, Any]] | None,
) -> WriterOutput:
    """从冻结源只插空行并构造零 Provider WriterOutput。"""

    revision_contract.assert_compatible(
        source_content=revision_contract.source_content,
        source_version_id=revision_contract.source_version_id,
        executable_constraint_plan=executable_constraint_plan,
    )
    result = reflow_paragraph_mean_only(
        content=revision_contract.source_content,
        validator_failures=validator_failures,
    )
    if not result.applied:
        raise RewriteCheckpointRecoveryError(
            "纯代码段均修复在冻结源上不再收敛，已阻止 Provider 与写入"
        )
    return WriterOutput(
        content=result.content,
        model_used=PARAGRAPH_REFLOW_MODEL_USED,
        token_usage=TokenUsage(input_tokens=0, output_tokens=0),
        finish_reason="stop",
        executable_constraint_plan=executable_constraint_plan,
        revision_contract=revision_contract,
    )


def _revision_contract_other_failures(
    guardrail: RewriteConstraintGuardrail,
) -> list[str]:
    """去掉已由结构化 violation 传入合同的重复展示文案。"""

    structured_displays = {
        _format_persisted_constraint_violation(violation)
        for violation in guardrail.constraint_violations
    }
    return [
        failure
        for failure in guardrail.other_failures
        if failure not in structured_displays
    ]


def _annotate_hard_constraint_regressions(
    *,
    review: ReviewResult,
    guardrail: RewriteConstraintGuardrail | None,
    chapter_id: str,
    workspace_id: str,
    version_reviewed: int,
    source_content: str | None = None,
    current_content: str | None = None,
) -> ReviewResult:
    """W3.5-B:若 rewrite 破坏上一轮已通过 hard gate,在 review 上标记回退。

    本函数把回退强制改成 ``verdict=rewrite`` / hard false，并把“为什么来回
    打架”以同一 ID 写进结构化字段、``failed_constraints`` / ``suggestions`` /
    日志；后续 state transition 与预算消费仍交既有 verdict / R5 budget 流程。
    """
    if guardrail is None:
        return review

    preserved_by_id = {
        result.constraint_id: result for result in guardrail.must_preserve
    }
    failed_by_id = {
        result.constraint_id: result
        for result in review.constraint_results
        if result.status == "fail"
    }
    candidate_regression_ids = [
        result.constraint_id
        for result in guardrail.must_preserve
        if result.constraint_id in failed_by_id
    ]
    if not candidate_regression_ids:
        return review

    preexisting_ids: list[str] = []
    if source_content is not None and current_content is not None:
        for constraint_id in candidate_regression_ids:
            failed = failed_by_id[constraint_id]
            if (
                failed.domain == "setting"
                and _evidence_is_unchanged_between_versions(
                    evidence=failed.details,
                    source_content=source_content,
                    current_content=current_content,
                )
            ):
                preexisting_ids.append(constraint_id)
    regression_ids = [
        constraint_id
        for constraint_id in candidate_regression_ids
        if constraint_id not in preexisting_ids
    ]
    preexisting_suggestion: str | None = None
    if preexisting_ids:
        preexisting_suggestion = _preexisting_setting_failure_suggestion(
            preexisting_ids
        )
        _log.warning(
            "hard_constraint_preexisting_failure_detected",
            chapter_id=chapter_id,
            workspace_id=workspace_id,
            version_reviewed=version_reviewed,
            constraint_ids=preexisting_ids,
        )
    if not regression_ids:
        assert preexisting_suggestion is not None
        return review.model_copy(
            update={"suggestions": list(review.suggestions) + [preexisting_suggestion]}
        )

    regression_markers = [
        f"hard_constraint_regression:{constraint_id}"
        for constraint_id in regression_ids
    ]
    merged_failed = list(
        dict.fromkeys(list(review.failed_constraints) + regression_markers)
    )
    suggestion = _hard_regression_suggestion(
        constraint_ids=regression_ids,
        results_by_id=preserved_by_id,
    )
    _log.warning(
        "hard_constraint_regression_detected",
        chapter_id=chapter_id,
        workspace_id=workspace_id,
        version_reviewed=version_reviewed,
        constraint_ids=regression_ids,
    )
    return review.model_copy(
        update={
            "verdict": "rewrite",
            "hard_constraints_passed": False,
            "failed_constraints": merged_failed,
            "hard_constraint_regressions": list(
                dict.fromkeys(list(review.hard_constraint_regressions) + regression_ids)
            ),
            "suggestions": list(review.suggestions)
            + ([preexisting_suggestion] if preexisting_suggestion else [])
            + [suggestion],
        }
    )


def _evidence_is_unchanged_between_versions(
    *, evidence: str, source_content: str, current_content: str
) -> bool:
    """证据必须完整位于 source/current 的同一 matching block 才算未改动。"""

    needle = evidence.strip()
    if len(needle) < 8:
        return False
    matcher = SequenceMatcher(
        None,
        source_content,
        current_content,
        autojunk=False,
    )
    for block in matcher.get_matching_blocks():
        if block.size < len(needle):
            continue
        source_block = source_content[block.a : block.a + block.size]
        offset = source_block.find(needle)
        if offset < 0:
            continue
        if (
            current_content[
                block.b + offset : block.b + offset + len(needle)
            ]
            == needle
        ):
            return True
    return False


# v1.1 audit G22b 治本(2026-05-27 b1-rerun-deep-audit):散文诗式 mode collapse
# 兜底硬覆盖阈值。
#
# paragraph_mean < 该值视作"散文诗式 mode collapse"(luo-clean-v1 ch-5 rerun-4
# 现场 paragraph_mean=13.5),强制覆盖 verdict=rewrite + tension_match=3.0 +
# prose_quality=3.0,无论 reviewer 主观给了多少分。Reviewer prompt 负责同步
# 降分并产 pacing issue；真正的 HARD verdict 只由本确定性 helper 按共享合同
# 给出，避免 Reviewer 另造第二把裁决尺。
#
# 精确阈值只取 ``PARAGRAPH_MEAN_HARD_MIN``；本地不复制数值。历史标定目标是
# 给正常中文长篇段落留足噪声余地，同时截获 luo-clean-v1 的 13-15 字/段塌缩。
_PARAGRAPH_COLLAPSE_THRESHOLD: float = PARAGRAPH_MEAN_HARD_MIN

# 短文本 guard:total_chars < 该值时跳过 G22b 兜底检测,对齐 G5 / G10 同型
# (MIN_CHARS_FOR_DRIFT_CHECK / MIN_CHARS_FOR_VOICE_CHECK = 500)。
# 真实长篇章节 word_count_min ≥ 1500-2500 字,远超此下限;短样本(mock fixture
# / 单测异常路径)直接绕过避免误判。
_MIN_CHARS_FOR_COLLAPSE_CHECK: float = 500.0


def _apply_paragraph_collapse_override(
    *,
    review: ReviewResult,
    current_fingerprint: dict[str, float] | None,
    chapter_id: str,
    version_reviewed: int,
    executable_constraint_plan: ExecutableConstraintPlan | None = None,
) -> ReviewResult:
    """G22b 兜底:paragraph_mean 低于共享硬底线时强制 verdict=rewrite。

    数据缺失(current_fingerprint=None / paragraph_length_mean=0)→ 返原 review
    不动(向后兼容旧路径 + draft 路径 mock fixture)。

    短文本 guard(total_chars < 500)→ 跳过(对齐 G5 / G10 同型,避免 mock
    fixture / 单测异常路径误判)。

    与 reviewer.md G22a 段的关系：G22a 只让 LLM 同步降分并记录 pacing issue；
    本 helper 是唯一 HARD verdict 来源。即使 LLM 没按提示降分，本层仍会按同一
    阈值覆盖分数与判决。
    """
    if executable_constraint_plan is not None and executable_constraint_plan.narrative_policy_version == 1:
        return review
    if current_fingerprint is None:
        return review
    total_chars = current_fingerprint.get("total_chars", 0.0)
    if total_chars < _MIN_CHARS_FOR_COLLAPSE_CHECK:
        return review
    para_mean = current_fingerprint.get("paragraph_length_mean", 0.0)
    if not (0.0 < para_mean < _PARAGRAPH_COLLAPSE_THRESHOLD):
        return review

    # 已经被上游(_apply_validator_pipeline / continuity_validator)覆盖为 rewrite
    # 时也无害再覆盖,但 issues / score 仍要加 G22b 信号。
    collapse_issue = ReviewIssue(
        paragraph_anchor=f"<G22b mode collapse 兜底,paragraph_mean={para_mean:.1f} 字>",
        issue_type="pacing",
        description=(
            f"paragraph_mean={para_mean:.1f} 字严重偏低 "
            f"(<{_PARAGRAPH_COLLAPSE_THRESHOLD:g}),"
            "散文诗式 mode collapse(非高张力)"
        ),
        suggestion=(
            "rewrite 矫正段落均值回 60-110 字默认自然参考带,"
            "描写 / 心理 / 对白段保持完整画面 — "
            "短句仅用于关键动作瞬间,不要全篇短句独立成段"
        ),
    )
    merged_failed = list(
        dict.fromkeys(
            list(review.failed_constraints) + [PARAGRAPH_MEAN_HARD_CONSTRAINT_ID]
        )
    )
    merged_violations = list(review.constraint_violations)
    if executable_constraint_plan is not None:
        paragraph_constraints = executable_constraint_plan.by_kind(
            "paragraph_mean_floor"
        )
        if len(paragraph_constraints) != 1:
            raise ExecutableConstraintPlanError(
                "paragraph_mean_floor 写后结果无法唯一回指 Writer 前计划"
            )
        merged_violations.append(
            paragraph_constraints[0].as_hard_violation(
                evidence_quote=json.dumps(
                    {
                        "actual_paragraph_length_mean": para_mean,
                        "minimum": _PARAGRAPH_COLLAPSE_THRESHOLD,
                    },
                    ensure_ascii=False,
                    sort_keys=True,
                    separators=(",", ":"),
                ),
                source_chapter_id=chapter_id,
                source_final_version=version_reviewed,
            )
        )
    new_review = review.model_copy(
        update={
            "verdict": "rewrite",
            "hard_constraints_passed": False,
            "failed_constraints": merged_failed,
            "score_tension_match": min(review.score_tension_match, 3.0),
            "score_prose_quality": min(review.score_prose_quality, 3.0),
            "issues": list(review.issues) + [collapse_issue],
            "constraint_violations": merged_violations,
        }
    )
    _log.warning(
        "reviewer_paragraph_collapse_override",
        chapter_id=chapter_id,
        version_reviewed=version_reviewed,
        paragraph_mean=para_mean,
        threshold=_PARAGRAPH_COLLAPSE_THRESHOLD,
        original_verdict=review.verdict,
        original_score_tension_match=review.score_tension_match,
        original_score_prose_quality=review.score_prose_quality,
    )
    return new_review


# v2 W3.5 UI 缺口3:升人工 escalation_details 构造器(供孤儿章人工裁决 UI 展示
# "为什么进人工"详情)。must_infeasible_pre_write 早已就地填(行 ~840);本组三个
# 函数补齐其余升人工出口的详情:rewrite 预算耗尽 / reviewer 解析失败。
# 共同契约:返回的 dict 不含 ``reason`` 键(chapter_service 同步时把
# ``escalation_reason`` 合入,见 chapter_service.py:_sync_escalation_details_safely),
# 含 ``checked_at_state`` + ``summary``(人类可读中文)+ 原因专属结构化字段。
_ESCALATION_RAW_OUTPUT_PREVIEW_LIMIT = 800


def _build_rewrite_budget_escalation_details(
    *, review: ReviewResult, rewrite_count: int, max_rewrites: int
) -> dict[str, Any]:
    """rewrite 预算耗尽升人工详情(最常见出口)。

    透出最后一次审校的判决 + 综合分 + 命中问题(issues:维度 / 描述 / 建议)+
    总体建议 + validator 失败明细,让维护人看到"卡在哪个维度、reviewer 怎么说"。
    """
    return {
        "checked_at_state": "post_review",
        "summary": (
            f"已重写 {rewrite_count}/{max_rewrites} 次,最后一次审校判决仍为 "
            f"{review.verdict}(综合分 {review.score_overall:.1f}),重写预算耗尽自动升人工。"
        ),
        "rewrite_count": rewrite_count,
        "max_rewrites": max_rewrites,
        "last_verdict": review.verdict,
        "score_overall": review.score_overall,
        "issues": [
            {
                "issue_type": iss.issue_type,
                "description": iss.description,
                "suggestion": iss.suggestion,
                "paragraph_anchor": iss.paragraph_anchor,
            }
            for iss in review.issues
        ],
        "suggestions": list(review.suggestions),
        "validator_failures": review.validator_failure_details or [],
    }


def _build_literary_revise_escalation_details(
    *, review: ReviewResult, revise_count: int
) -> dict[str, Any]:
    """文学局部修订一次后仍未通过时停止自动改写。"""

    summary = (
        "文学问题没有可用的局部修订名额；为避免改动已通过内容，系统没有转入自动"
        "全文 rewrite，已直接升人工。"
        if revise_count == 0
        else "文学问题已使用一次局部修订，复审仍未通过；为避免把已通过内容继续改坏，"
        "系统已停止自动全文 rewrite 并升人工。"
    )
    return {
        "checked_at_state": "post_review",
        "summary": summary,
        "revise_count": revise_count,
        "last_verdict": review.verdict,
        "score_overall": review.score_overall,
        "issues": [
            {
                "issue_type": issue.issue_type,
                "description": issue.description,
                "suggestion": issue.suggestion,
                "paragraph_anchor": issue.paragraph_anchor,
            }
            for issue in review.revise_targets
        ],
        "suggestions": list(review.suggestions),
    }


def _build_reviewer_parse_escalation_details(
    *, raw_output: str, model_used: str, attempts_log: list[str]
) -> dict[str, Any]:
    """reviewer 连续解析失败升人工详情。

    自动审稿无法产出合法 JSON(模型输出格式损坏);透出模型名 + 尝试次数 +
    末次原始输出预览,供维护人判断"是模型抽风还是正文有问题"。
    """
    return {
        "checked_at_state": "reviewing",
        "summary": (
            f"自动审稿连续 {_REVIEWER_MAX_ATTEMPTS} 次解析失败(模型 "
            f"{model_used} 未产出合法审稿 JSON),无法得到判决,升人工。"
        ),
        "model_used": model_used,
        "attempts": list(attempts_log),
        "raw_output_preview": raw_output[:_ESCALATION_RAW_OUTPUT_PREVIEW_LIMIT],
    }


def _build_re_review_escalation_details(*, review: ReviewResult) -> dict[str, Any]:
    """re-review(人工改稿后重审)未通过 → 回人工的详情。

    与 rewrite 预算耗尽不同:这是"人工改完正文重审仍不达标",summary 措辞贴该
    场景(不提"重写次数")。透出审校维度 + 反馈让维护人决定再改 / 强制通过。
    """
    return {
        "checked_at_state": "re_review",
        "summary": (
            f"人工改稿后重审未通过(判决 {review.verdict},综合分 "
            f"{review.score_overall:.1f}):请按下列反馈再改后重审,或用「强制通过」批准。"
        ),
        "last_verdict": review.verdict,
        "score_overall": review.score_overall,
        "issues": [
            {
                "issue_type": iss.issue_type,
                "description": iss.description,
                "suggestion": iss.suggestion,
                "paragraph_anchor": iss.paragraph_anchor,
            }
            for iss in review.issues
        ],
        "suggestions": list(review.suggestions),
        "validator_failures": review.validator_failure_details or [],
    }


def _should_use_word_count_trim_revision(
    *,
    review: ReviewResult,
    chapter_text: str,
    chapter_card: ChapterCard,
    executable_constraint_plan: ExecutableConstraintPlan,
    allow_word_count_tolerance: bool = True,
) -> bool:
    """识别“只有 above_max 需要改”的 reactive revise。

    这不是第二套字数规则：``validator_failure_details`` 是既有写后 validator
    产生的失败事实，本函数只在 Provider 前核对它仍与当前源正文和同一
    ChapterCard 完全一致。只有没有 ReviewIssue target、没有其他结构化 HARD fail
    的单独超上限，才改走已有 ``word_count_trim``；below_min、混合意见及其他失败
    继续走通用 Reviser / rewrite。
    """

    if review.verdict != "revise" or review.revise_targets:
        return False
    if any(item.status == "fail" for item in review.constraint_results):
        return False
    if chapter_card.word_count_policy_version != 0:
        return False
    hard_kinds = [
        executable_constraint_plan.hard_violation_constraint(item).kind
        for item in review.constraint_violations
        if item.severity == "hard"
    ]
    if hard_kinds != ["word_count"]:
        # 当前计划化写后路径必须已经把唯一 deterministic word-count 失败
        # 物化为一个 hard observation；缺失、重复或混有 setting/style 等其他
        # HARD 都不得被“仅字数”专用路由吞掉。
        return False
    failures = review.validator_failure_details or []
    if len(failures) != 1:
        return False
    failure = failures[0]
    details = failure.get("details")
    if failure.get("name") != "word_count" or not isinstance(details, dict):
        return False
    if details.get("reason") != "above_max":
        return False

    actual = details.get("actual_word_count")
    minimum = details.get("word_count_min")
    maximum = details.get("word_count_max")
    if type(actual) is not int:
        raise ValueError("above_max validator failure 的实际字数类型非法")
    if type(minimum) is not int or type(maximum) is not int:
        raise ValueError("above_max validator failure 的名义边界类型非法")
    _, acceptance_max = word_count_acceptance_bounds(
        word_count_min=chapter_card.word_count_min,
        word_count_max=chapter_card.word_count_max,
        allow_word_count_tolerance=allow_word_count_tolerance,
    )
    if (
        actual != len(chapter_text)
        or minimum != chapter_card.word_count_min
        or maximum != chapter_card.word_count_max
        or actual <= acceptance_max
    ):
        raise ValueError(
            "above_max validator failure 与当前源正文或 ChapterCard 已漂移"
        )
    return True


def _handle_verdict(
    *,
    state: ChapterState,
    review: ReviewResult,
    chapter_card: ChapterCard,
    revise_count: int,
    rewrite_count: int,
    budget_neutral_mechanical_reflow: bool = False,
) -> tuple[ChapterState, int, int, str | None]:
    """根据 verdict 与预算决定下一状态 + 计数更新 + 升人工原因。

    所有状态切换都经由 ``transition``,不绕过状态机。``revise_count`` /
    ``rewrite_count`` +1 时机:``transition`` 把 state 切到 REVISING /
    REWRITING 之后立即 +1。唯一例外是已由代码证明可完成的纯段落 reflow：
    它不调 Writer、不改非空白字符，因此不属于 content rewrite 预算。

    返回元组:``(next_state, new_revise_count, new_rewrite_count, escalation_reason)``。
    """
    verdict = review.verdict

    if verdict == "pass":
        return transition(state, "verdict_pass"), revise_count, rewrite_count, None

    if verdict == "rewrite":
        if budget_neutral_mechanical_reflow:
            next_state = transition(state, "verdict_rewrite")
            return next_state, revise_count, rewrite_count, None
        if rewrite_count >= chapter_card.max_rewrites:
            # 走 transition escalate 条件分支(rewrite_count >= max_rewrites)。
            next_state = transition(
                state,
                "escalate",
                verdict="rewrite",
                rewrite_count=rewrite_count,
                max_rewrites=chapter_card.max_rewrites,
            )
            return next_state, revise_count, rewrite_count, "rewrite_budget_exhausted"
        # 走 transition 4: REVIEWING --verdict_rewrite--> REWRITING
        next_state = transition(state, "verdict_rewrite")
        return next_state, revise_count, rewrite_count + 1, None

    if verdict == "revise":
        # Session D:带唯一正文锚点的文学问题只允许一次 LOCALIZED Reviser。
        # 第二次仍需 revise 时直接升人工，绝不把它转换成 GLOBAL Writer rewrite。
        if review.revise_targets and revise_count >= min(
            1, chapter_card.max_revises
        ):
            next_state = transition(state, "force_escalate", force=True)
            return (
                next_state,
                revise_count,
                rewrite_count,
                "literary_revise_limit_reached",
            )
        if revise_count < chapter_card.max_revises:
            # 走 transition 3: REVIEWING --verdict_revise--> REVISING
            # transition 内部判 revise_count < max_revises 后返回 REVISING。
            next_state = transition(
                state,
                "verdict_revise",
                revise_count=revise_count,
                max_revises=chapter_card.max_revises,
            )
            return next_state, revise_count + 1, rewrite_count, None

        # revise 名额耗尽,看 rewrite 名额是否还够。
        if rewrite_count >= chapter_card.max_rewrites:
            next_state = transition(
                state,
                "escalate",
                verdict="rewrite",
                rewrite_count=rewrite_count,
                max_rewrites=chapter_card.max_rewrites,
            )
            return next_state, revise_count, rewrite_count, "rewrite_budget_exhausted"

        # 走 transition 4 双触发路径:REVIEWING --verdict_revise (revise_count >= max_revises)--> REWRITING
        next_state = transition(
            state,
            "verdict_revise",
            revise_count=revise_count,
            max_revises=chapter_card.max_revises,
        )
        return next_state, revise_count, rewrite_count + 1, None

    # P1 Reviewer 不直接产出 escalate_to_human;走到这里说明 verdict 字面量
    # 出了 pass/revise/rewrite 三档之外,数据层 bug。
    raise RuntimeError(f"unexpected verdict from Reviewer: {verdict}")


def _extract_memories_safely(
    *,
    session_factory: PipelineSessionFactory,
    vector_store: VectorStore,
    router: ModelRouter | None,
    client: LLMClient | None,
    chapter_card: ChapterCard,
    workspace_id: str,
    final_path: Path,
    model: str | None,
    setting_contract: SettingContract | None = None,
    progression_consistency_enabled: bool = False,
    progression_label: str = "境界",
) -> "MemoryExtractionOutcome":
    """P2-B-19:在 APPROVED 终态后调 ``extract_memories`` + commit;失败吞。

    模块②(v1.1 w5-s10):``progression_consistency_enabled`` / ``progression_label``
    由 ``orchestrate_chapter`` 透传给 ``extract_memories``,启用时 extract 锚定增量
    抽进阶等级 + 注入↔写出闭环 WARN 校验;``False`` 时进阶子管线 no-op(零回归)。

    R8 起返回 ``MemoryExtractionOutcome``：除成功/失败外还明确记录向量写是否
    已开始、SQLite rollback 是否确认，以及结果是否未知。对象保留 bool
    真值兼容，失败仍不冒泡。

    事务边界:
    - 长 session 跨"读 final.md → LLM 调用 → SQLite flush → Chroma add → commit"
      整个流程,以保 SQLite 写在一个事务内。
    - LLM 解析失败 / SQLite 写失败 / Chroma 写失败 → ``rollback`` + structlog
      ``memory_extraction_failed`` 事件,**不冒泡**。章节已通过审稿 + final.md
      已落盘,extract 失败不应让 :write 整体回滚。
    - 失败后 Chroma 是否有"部分写入"?P2-B-09 决策 5"先 SQLite flush,后 Chroma
      add"——SQLite 失败时 Chroma 还没动;Chroma 失败时 SQLite 已 flush 但
      未 commit,本函数 rollback 即可。极端情况:Chroma 部分 add 后崩,本
      函数无法清理(P3+ 引入幂等再处理)。
    """
    try:
        content = final_path.read_text(encoding="utf-8")
    except OSError as exc:
        _log.error(
            "memory_extraction_failed",
            chapter_id=chapter_card.chapter_id,
            workspace_id=workspace_id,
            stage="read_final",
            error_type=type(exc).__name__,
            error_message=str(exc),
        )
        return MemoryExtractionOutcome(
            succeeded=False,
            failure_stage="read_final",
            vector_write_started=False,
            sqlite_rollback_succeeded=True,
            result_unknown=False,
        )

    session = session_factory()
    scene_anchor: ExtractedSceneAnchor | None = None
    vector_write_started = False

    def _mark_vector_write_started() -> None:
        nonlocal vector_write_started
        vector_write_started = True

    try:
        extraction_result = extract_memories(
            content=content,
            card=chapter_card,
            workspace_id=workspace_id,
            router=router,
            llm=client,
            model=model,
            db_session=session,
            vector_store=vector_store,
            setting_contract=setting_contract,
            progression_consistency_enabled=progression_consistency_enabled,
            progression_label=progression_label,
            on_vector_write_started=_mark_vector_write_started,
        )
        scene_anchor = extraction_result.scene_anchor
        # B2(v1.1 wave 6 session 3):落本章 scene_anchor 到 ChapterORM.scene_anchor
        # (纯 DB 写,无 LLM —— 在 extract 写事务内安全,不违 Y1-C)。
        _persist_scene_anchor_to_chapter(session, chapter_card.chapter_id, scene_anchor)
        session.commit()
    except Exception as exc:
        rollback_succeeded = True
        try:
            session.rollback()
        except Exception:  # noqa: BLE001 — rollback 未确认即 result_unknown
            rollback_succeeded = False
        _log.error(
            "memory_extraction_failed",
            chapter_id=chapter_card.chapter_id,
            workspace_id=workspace_id,
            stage="extract",
            error_type=type(exc).__name__,
            error_message=str(exc),
        )
        return MemoryExtractionOutcome(
            succeeded=False,
            failure_stage="extract",
            vector_write_started=vector_write_started,
            sqlite_rollback_succeeded=rollback_succeeded,
            result_unknown=(vector_write_started or not rollback_succeeded),
        )
    finally:
        session.close()

    # B2 跨章比对 + LLM 复核:**在 extract 写事务之外**(护 Y1-C 不变量:写事务不
    # 跨 LLM)。WARN-only,全 swallow(extract 已在 APPROVED 后,纯观测,不阻塞)。
    _check_scene_anchor_continuity_safely(
        session_factory=session_factory,
        chapter_card=chapter_card,
        workspace_id=workspace_id,
        chapter_text=content,
        scene_anchor=scene_anchor,
        router=router,
        client=client,
        model=model,
    )
    return MemoryExtractionOutcome(
        succeeded=True,
        failure_stage=None,
        vector_write_started=vector_write_started,
        sqlite_rollback_succeeded=True,
        result_unknown=False,
    )


@dataclass(frozen=True, slots=True)
class MemoryExtractionOutcome:
    """富记忆 SQL/Chroma 事务边界的结构化结果。"""

    succeeded: bool
    failure_stage: str | None
    vector_write_started: bool
    sqlite_rollback_succeeded: bool
    result_unknown: bool

    def __bool__(self) -> bool:
        return self.succeeded


# B2(v1.1 wave 6 session 3):跨章查"最近一次入梦点"的回看窗口上限(bound 查询;
# 双时空梦境弧通常跨几章,30 章足够覆盖且 SQLite 扫描成本可忽略)。
_SCENE_ANCHOR_LOOKBACK: int = 30


def _persist_scene_anchor_to_chapter(
    session: Session,
    chapter_id: str,
    scene_anchor: ExtractedSceneAnchor | None,
) -> None:
    """B2:把本章 scene_anchor 写到 ``ChapterORM.scene_anchor`` 列(纯 DB 写)。

    无锚点(``None`` / 入梦点+醒来点全空)→ 不写(保持 ``{}`` 默认)。chapter 行
    不存在(异常路径)→ 跳过(防御)。在 extract 写事务内调用(无 LLM,不违 Y1-C)。
    """
    if scene_anchor is None or scene_anchor.is_empty:
        return
    row = session.get(ChapterORM, chapter_id)
    if row is None:
        return
    row.scene_anchor = {
        "enter_dream_location": scene_anchor.enter_dream_location,
        "wake_location": scene_anchor.wake_location,
        "has_transition_explanation": scene_anchor.has_transition_explanation,
    }


def _locations_match(a: str, b: str) -> bool:
    """B2 代码粗筛:两地点字面是否"显然同一地点"(相等 / 互为子串)。

    命中 → 视作自然(不报 / 不调 LLM)。规范化:strip + 去空白。粗筛保守 ——
    只放过"显然同地点",真正同义 / 大小地点判断交给 LLM 复核。
    """
    na = a.strip().replace(" ", "")
    nb = b.strip().replace(" ", "")
    if not na or not nb:
        return False
    return na == nb or na in nb or nb in na


def _check_scene_anchor_continuity_safely(
    *,
    session_factory: PipelineSessionFactory,
    chapter_card: ChapterCard,
    workspace_id: str,
    chapter_text: str,
    scene_anchor: ExtractedSceneAnchor | None,
    router: ModelRouter | None,
    client: LLMClient | None,
    model: str | None,
) -> None:
    """B2:跨章比对"醒来点≠入梦点且无位移交代" + LLM 复核(治 bug B1/B2/B3 肉体瞬移)。

    **在 extract 写事务之外**调用(护 Y1-C:写事务不跨 LLM)。**WARN-only,全 swallow**
    —— extract 已在 APPROVED 后,纯观测,任何异常都不阻塞主流水线。

    流程:本章有醒来点 + 未交代位移 → 按 ``chapter_id`` 升序查最近一次入梦点 →
    代码粗筛(``_locations_match`` 放过显然同地点)→ 不匹配则 LLM 复核
    (``natural`` / ``teleport`` / ``uncertain``)→ 仅 teleport / uncertain WARN log。
    """
    try:
        if scene_anchor is None or not scene_anchor.wake_location.strip():
            return  # 本章无醒来点 → 无需比对
        if scene_anchor.has_transition_explanation:
            return  # 正文已交代位移 → 护留白,不报
        chapter_id = chapter_card.chapter_id
        session = session_factory()
        try:
            rows = session.execute(
                select(ChapterORM.chapter_id, ChapterORM.scene_anchor)
                .where(ChapterORM.workspace_id == workspace_id)
                .where(ChapterORM.chapter_id < chapter_id)
                .order_by(ChapterORM.chapter_id.desc())
                .limit(_SCENE_ANCHOR_LOOKBACK)
            ).all()
        finally:
            session.close()

        enter_location = ""
        enter_chapter_id = ""
        for cid, anchor in rows:
            loc = str((anchor or {}).get("enter_dream_location", "")).strip()
            if loc:
                enter_location = loc
                enter_chapter_id = cid
                break
        if not enter_location:
            return  # 无历史入梦点 → 无可比对

        wake = scene_anchor.wake_location.strip()
        if _locations_match(enter_location, wake):
            return  # 显然同地点 → 不报(省一次 LLM)

        result: SceneAnchorResult = validate_scene_anchor(
            enter_location=enter_location,
            wake_location=wake,
            chapter_text=chapter_text,
            chapter_id=chapter_id,
            enter_chapter_id=enter_chapter_id,
            workspace_id=workspace_id,
            router=router,
            client=client,
            model=model,
        )
        if result.is_teleport:
            _log.warning(
                "scene_anchor_teleport_warn",
                chapter_id=chapter_id,
                workspace_id=workspace_id,
                enter_location=enter_location,
                enter_chapter_id=enter_chapter_id,
                wake_location=wake,
                verdict=result.verdict,
                evidence=result.evidence,
                note=(
                    "B2:醒来点≠入梦点且无位移交代,LLM 复核判疑似肉体瞬移;"
                    "warn 不阻塞(extract 后纯观测)"
                ),
            )
    except Exception as exc:  # noqa: BLE001 — WARN-only,全 swallow
        _log.warning(
            "scene_anchor_check_failed",
            chapter_id=chapter_card.chapter_id,
            workspace_id=workspace_id,
            error_type=type(exc).__name__,
            error_message=str(exc),
        )


# B5(v1.1 wave 6 session 4):口头承诺追踪参数。
# - 重要性阈值:plot_thread.importance ≥ 该值 = "正文反复强调的悬念"(importance 是
#   LLM 抽取时按悬念分量评的 1-5;高 importance 未收口 = 读者会记得的承诺没兑现)。
#   注:更精确的"反复强调≥N次"可加 mention_count 字段,留 wave 6 末实测后定(§0
#   原则二:不为推测收益加 schema 字段)。
# - ending_window:取最近 N 章 ChapterSummary 作"结局区事件"给 judge 看是否兑现。
# - max_candidates:候选上限(防 LLM 输入爆;超出按 importance 取 top + log 丢弃数)。
_PAYOFF_IMPORTANCE_THRESHOLD: int = 4
_PAYOFF_ENDING_WINDOW: int = 8
_PAYOFF_MAX_CANDIDATES: int = 15
_PAYOFF_UNRESOLVED_STATUSES: frozenset[str] = frozenset({"planted", "developing"})


def _check_plot_thread_payoff_safely(
    *,
    session_factory: PipelineSessionFactory,
    chapter_card: ChapterCard,
    workspace_id: str,
    router: ModelRouter | None,
    client: LLMClient | None,
    model: str | None,
) -> None:
    """B5:口头承诺追踪(仅终章触发)—— 复用 thread_closure_validator 判 DB plot_threads 兑现。

    治 B1 30 章长跑 bug D4(界外真局埋了不收):writer 在正文反复强调的悬念到结局
    未兑现。本章累积的 plot_threads(extract 落库)里**重要 + 未收口 + 无 payoff**
    的伏笔,在**全书最后一章**一次性喂 thread_closure_validator 判:
    ``open_with_setup``(有交代的开放 = 合格,护 §0 C1 留白)/ ``dangling``(埋了不收 =
    WARN)/ ``closed``(已收口)。

    **与 A4(outline 期 thread_closure)分工**:A4 在 mini_plan 生成期查 **outline
    long_threads** 设计是否收口(上游);B5 在终章查 **DB 里正文实际累积的
    plot_threads** 是否兑现(下游)。数据源 + 触发点都不同,不重复造。

    **WARN-only,全 swallow**(对齐 §0 第一原则 + B2 同型):非终章 / 无候选 / 任何
    异常 → 直接返回不阻塞。在 extract 写事务之外调用(护 Y1-C:写事务不跨 LLM)。
    """
    try:
        current_seq = chapter_seq_from_id(chapter_card.chapter_id)
        if current_seq is None:
            return

        session = session_factory()
        try:
            workspace = session.get(WorkspaceORM, workspace_id)
            total_chapters = workspace.total_chapters if workspace else 0
            # 仅终章触发(本章 = 全书最后一章)。total_chapters 未设(0)→ skip。
            if total_chapters <= 0 or current_seq < total_chapters:
                return

            threads = list_plot_threads_by_workspace(session, workspace_id)
            ending_summaries = _load_ending_summaries(
                session, workspace_id, _PAYOFF_ENDING_WINDOW
            )
        finally:
            session.close()

        # 候选 = 重要(importance≥阈值)+ 未收口(planted/developing)+ 无实际兑现章。
        unresolved = [
            t
            for t in threads
            if t.importance >= _PAYOFF_IMPORTANCE_THRESHOLD
            and t.status in _PAYOFF_UNRESOLVED_STATUSES
            and not t.actual_payoff
        ]
        if not unresolved:
            return
        unresolved.sort(key=lambda t: t.importance, reverse=True)
        dropped = max(0, len(unresolved) - _PAYOFF_MAX_CANDIDATES)
        selected = unresolved[:_PAYOFF_MAX_CANDIDATES]
        if dropped:
            _log.info(
                "plot_thread_payoff_candidates_capped",
                chapter_id=chapter_card.chapter_id,
                workspace_id=workspace_id,
                total=len(unresolved),
                kept=len(selected),
                dropped=dropped,
            )

        candidates = [
            ThreadClosureCandidate(
                thread_title=t.title,
                thread_description=t.description,
                last_hooked_chapter=t.last_updated_chapter or t.planted_chapter,
            )
            for t in selected
        ]
        result = validate_thread_closure_batch(
            candidates=candidates,
            ending_events=ending_summaries,
            workspace_id=workspace_id,
            router=router,
            client=client,
            model=model,
        )
        dangling = result.dangling_titles
        if dangling:
            _log.warning(
                "plot_thread_payoff_dangling_warn",
                chapter_id=chapter_card.chapter_id,
                workspace_id=workspace_id,
                total_chapters=total_chapters,
                dangling_titles=dangling,
                candidate_count=len(candidates),
                note=(
                    "B5:正文反复强调的重要悬念到终章未兑现(埋了不收);"
                    "warn 不阻塞(终章 extract 后纯观测,护 C1 有交代开放合法)"
                ),
            )
    except Exception as exc:  # noqa: BLE001 — WARN-only,全 swallow
        _log.warning(
            "plot_thread_payoff_check_failed",
            chapter_id=chapter_card.chapter_id,
            workspace_id=workspace_id,
            error_type=type(exc).__name__,
            error_message=str(exc),
        )


def _load_ending_summaries(
    session: Session, workspace_id: str, window: int
) -> list[str]:
    """B5:读最近 ``window`` 章 ChapterSummary.summary_text 作"结局区事件"。

    按 ``chapter_id`` 降序取最近若干章摘要(无摘要 → 空 list,validator 自适应)。
    给 thread_closure_validator 看结局区发生了什么、悬念是否兑现。
    """
    rows = session.execute(
        select(ChapterSummaryORM.summary_text)
        .where(ChapterSummaryORM.workspace_id == workspace_id)
        .order_by(ChapterSummaryORM.chapter_id.desc())
        .limit(window)
    ).all()
    return [str(r[0]).strip() for r in rows if r[0] and str(r[0]).strip()]


class ChapterVersionPersistenceError(RuntimeError):
    """正文版本无法进入 DB 权威账本。"""


class ReviewCheckpointPersistenceError(RuntimeError):
    """已完成的 Reviewer 结果无法进入 DB 权威账本。"""


def _add_review_result_to_session(session: Session, review: ReviewResult) -> None:
    """把一份完整审稿结果加入 caller 事务；已存在同 ID 时幂等跳过。"""
    if session.get(ReviewResultORM, review.review_id) is not None:
        return
    session.add(
        ReviewResultORM(
            review_id=review.review_id,
            chapter_id=review.chapter_id,
            version_reviewed=review.version_reviewed,
            reviewed_at=review.reviewed_at,
            reviewer_model=review.reviewer_model,
            score_plot_progression=review.score_plot_progression,
            score_tension_match=review.score_tension_match,
            score_prose_quality=review.score_prose_quality,
            score_consistency=review.score_consistency,
            score_hook_management=review.score_hook_management,
            score_voice_consistency=review.score_voice_consistency,
            score_overall=review.score_overall,
            score_imagery=review.score_imagery,
            score_interiority=review.score_interiority,
            score_formulaic=review.score_formulaic,
            score_immersion=review.score_immersion,
            hard_constraints_passed=review.hard_constraints_passed,
            failed_constraints=list(review.failed_constraints),
            constraint_violations=[
                violation.model_dump(mode="json")
                for violation in review.constraint_violations
            ],
            constraint_results=[
                result.model_dump(mode="json") for result in review.constraint_results
            ],
            hard_constraint_regressions=list(review.hard_constraint_regressions),
            suggestions=list(review.suggestions),
            verdict=review.verdict,
        )
    )
    for idx, issue in enumerate(review.issues):
        session.add(
            ReviewIssueORM(
                review_id=review.review_id,
                role="issue",
                position=idx,
                paragraph_anchor=issue.paragraph_anchor,
                issue_type=issue.issue_type,
                description=issue.description,
                suggestion=issue.suggestion,
            )
        )
    for idx, issue in enumerate(review.revise_targets):
        session.add(
            ReviewIssueORM(
                review_id=review.review_id,
                role="revise_target",
                position=idx,
                paragraph_anchor=issue.paragraph_anchor,
                issue_type=issue.issue_type,
                description=issue.description,
                suggestion=issue.suggestion,
            )
        )


def persist_chapter_version_safely(
    session_factory: PipelineSessionFactory | None,
    *,
    chapter_id: str,
    version: ChapterVersion,
    inherited_review_checkpoint: ReviewResult | None = None,
) -> None:
    """落 ``chapter_versions`` 并同事务重算 ``chapters`` 查询投影。

    ``session_factory=None`` → no-op(P1 兼容路径,无 DB 写)。给定时打开短
    session、写一行、从版本表重算 ``current_version/rewrite_count/revise_count``
    后一次 commit。W3.5-R5 起 DB 版本不是辅助任务：失败必须冒泡，不能返回一份
    只存在文件系统、却没有预算消耗记录的“免费正文”。

    幂等:复合主键 ``(chapter_id, version_id)`` 已存在 → 静默跳过(章节内
    版本号在 ``ChapterVersioning.next_version_id`` 单调递增,理论上不会撞;
    幂等保护防御重跑场景)，但仍会重算章节投影以修复旧漂移。

    P3-B-14 沉淀的 "workspace_id 透传" 纪律对本函数 no-op:``chapter_versions``
    表无 workspace_id 字段,通过 ``chapter_id`` FK 关联 ``chapters`` 行间接
    携带 workspace_id。
    """
    if session_factory is None:
        return
    try:
        session = session_factory()
    except Exception as exc:
        _log.error(
            "chapter_version_persist_failed",
            chapter_id=chapter_id,
            version_id=version.version_id,
            stage="open_session",
            error_type=type(exc).__name__,
            error_message=str(exc),
        )
        raise ChapterVersionPersistenceError(
            f"无法打开章节版本账本: {chapter_id}/v{version.version_id}"
        ) from exc
    try:
        existing = session.get(ChapterVersionORM, (chapter_id, version.version_id))
        if existing is None:
            row = ChapterVersionORM(
                chapter_id=chapter_id,
                version_id=version.version_id,
                content=version.content,
                word_count=version.word_count,
                created_at=version.created_at,
                creation_type=version.creation_type,
                model_used=version.model_used,
                review_score=version.review_score,
                notes=version.notes,
                style_fingerprint=version.style_fingerprint,
            )
            session.add(row)
            session.flush()
        else:
            mismatched_fields = [
                field
                for field, actual, expected in (
                    ("content", existing.content, version.content),
                    ("word_count", existing.word_count, version.word_count),
                    ("creation_type", existing.creation_type, version.creation_type),
                    ("model_used", existing.model_used, version.model_used),
                    ("notes", existing.notes, version.notes),
                    (
                        "style_fingerprint",
                        existing.style_fingerprint,
                        version.style_fingerprint,
                    ),
                )
                if actual != expected
            ]
            if mismatched_fields:
                raise ChapterVersionPersistenceError(
                    "同一章节版本号已存在但内容/元数据不一致，"
                    "已阻止把 crash 残留冒充幂等成功: "
                    f"{chapter_id}/v{version.version_id}/"
                    f"fields={mismatched_fields}"
                )
        if inherited_review_checkpoint is not None:
            if inherited_review_checkpoint.version_reviewed != version.version_id:
                raise ChapterVersionPersistenceError(
                    "继承审稿 checkpoint 的 version_reviewed 与新版本不一致: "
                    f"{chapter_id}/v{version.version_id}"
                )
            _add_review_result_to_session(session, inherited_review_checkpoint)
        chapter = session.get(ChapterORM, chapter_id)
        if chapter is None:
            raise ChapterVersionPersistenceError(f"章节投影行不存在: {chapter_id}")
        version_rows = session.execute(
            select(
                ChapterVersionORM.version_id,
                ChapterVersionORM.creation_type,
                ChapterVersionORM.model_used,
                ChapterVersionORM.content,
            )
            .where(ChapterVersionORM.chapter_id == chapter_id)
            .order_by(ChapterVersionORM.version_id)
        ).all()
        chapter.current_version = (
            int(version_rows[-1].version_id) if version_rows else 0
        )
        chapter.rewrite_count = count_effective_content_rewrites(
            [
                (str(row.creation_type), str(row.model_used), str(row.content))
                for row in version_rows
            ],
            excluded_model_used=PARAGRAPH_REFLOW_MODEL_USED,
        )
        chapter.revise_count = sum(
            1 for row in version_rows if row.creation_type == "revise"
        )
        session.commit()
    except Exception as exc:
        session.rollback()
        _log.error(
            "chapter_version_persist_failed",
            chapter_id=chapter_id,
            version_id=version.version_id,
            stage="commit",
            error_type=type(exc).__name__,
            error_message=str(exc),
        )
        if isinstance(exc, ChapterVersionPersistenceError):
            raise
        raise ChapterVersionPersistenceError(
            f"章节版本账本提交失败: {chapter_id}/v{version.version_id}"
        ) from exc
    finally:
        session.close()


def persist_review_result_safely(
    session_factory: PipelineSessionFactory | None,
    *,
    review: ReviewResult,
) -> None:
    """P4-B-02:落 ``review_results`` + ``review_issues`` 表，失败硬停。

    ``session_factory=None`` → no-op。给定时打开短 session,写 ``ReviewResultORM``
    主行 + N 条 ``ReviewIssueORM``(``role="issue"``)+ M 条 ``ReviewIssueORM``
    (``role="revise_target"``),全部一次 commit。Reviewer 响应已经产生费用；
    任一失败都 rollback、记日志并抛错，不能让恢复路径再次盲审。

    幂等:``review_id`` 主键已存在 → 静默跳过。``review_id`` 由 Reviewer agent
    在产出时构造(``str(uuid4())``),理论上不会撞;幂等保护防御重跑场景。

    ``ReviewIssueORM.position`` 保 list 顺序(0-based),保证 ORM ↔ Pydantic
    round-trip 时 ``issues`` / ``revise_targets`` 列表顺序稳定。
    """
    if session_factory is None:
        return
    try:
        session = session_factory()
    except Exception as exc:
        _log.error(
            "review_result_persist_failed",
            review_id=review.review_id,
            chapter_id=review.chapter_id,
            stage="open_session",
            error_type=type(exc).__name__,
            error_message=str(exc),
        )
        raise ReviewCheckpointPersistenceError(
            f"无法打开审稿账本: {review.chapter_id}/v{review.version_reviewed}"
        ) from exc
    try:
        _add_review_result_to_session(session, review)
        session.commit()
    except Exception as exc:
        session.rollback()
        _log.error(
            "review_result_persist_failed",
            review_id=review.review_id,
            chapter_id=review.chapter_id,
            stage="commit",
            error_type=type(exc).__name__,
            error_message=str(exc),
        )
        if isinstance(exc, ReviewCheckpointPersistenceError):
            raise
        raise ReviewCheckpointPersistenceError(
            "审稿 checkpoint 提交失败: "
            f"{review.chapter_id}/v{review.version_reviewed}/review={review.review_id}"
        ) from exc
    finally:
        session.close()


def _persist_parse_failure_safely(
    session_factory: PipelineSessionFactory | None,
    *,
    chapter_id: str,
    version_reviewed: int,
    raw_output: str,
    model_used: str,
    attempts_log: list[str],
) -> None:
    """v1.0.x reviewer-resilience(2026-05-20):reviewer 连续 N 次解析失败升人工时,
    写一条占位 ``review_results`` 行,让前端"审稿历史"能渲染失败提示而非"暂无审稿"。

    占位行约定(与正常 review 区分):
    - ``verdict='parse_failed'`` — 非 Literal 既有 4 个值,前端按字面渲染红色提示行
    - 6 项 score 全 0.0,``score_overall=0.0``
    - ``hard_constraints_passed=False``
    - ``failed_constraints=['reviewer_parse_failed_x{N}']`` — 标识 N 次尝试均失败
    - ``suggestions`` 含每次 attempt 的错误摘要(累积自 ``_review_with_retry``)+
      末尾一条 raw_output 前 1000 字预览(供用户判读 LLM 输出问题)

    ``session_factory=None`` 保留 legacy no-op；生产 DB 写失败则抛错，避免已付费
    的解析失败事实丢失后被恢复路径重复请求。
    """
    if session_factory is None:
        return
    try:
        session = session_factory()
    except Exception as exc:
        _log.error(
            "review_result_persist_failed",
            chapter_id=chapter_id,
            stage="open_session_parse_failed",
            error_type=type(exc).__name__,
            error_message=str(exc),
        )
        raise ReviewCheckpointPersistenceError(
            f"无法打开 parse-failed 审稿账本: {chapter_id}/v{version_reviewed}"
        ) from exc
    try:
        review_id = f"rev-pf-{uuid.uuid4().hex[:8]}"
        suggestions: list[str] = list(attempts_log) if attempts_log else []
        raw_preview = (raw_output or "")[:1000]
        if raw_preview:
            suggestions.append(f"[原始输出预览(前 1000 字)] {raw_preview}")
        if not suggestions:
            suggestions = ["Reviewer LLM 多次输出无法解析,详见 structlog 日志。"]

        result_row = ReviewResultORM(
            review_id=review_id,
            chapter_id=chapter_id,
            version_reviewed=version_reviewed,
            reviewed_at=datetime.now(UTC),
            reviewer_model=model_used or "unknown",
            score_plot_progression=0.0,
            score_tension_match=0.0,
            score_prose_quality=0.0,
            score_consistency=0.0,
            score_hook_management=0.0,
            score_voice_consistency=0.0,
            score_overall=0.0,
            hard_constraints_passed=False,
            failed_constraints=[
                f"reviewer_parse_failed_x{len(attempts_log) or _REVIEWER_MAX_ATTEMPTS}"
            ],
            suggestions=suggestions,
            verdict="parse_failed",
        )
        session.add(result_row)
        session.commit()
        _log.info(
            "review_parse_failure_persisted",
            review_id=review_id,
            chapter_id=chapter_id,
            version_reviewed=version_reviewed,
            attempts=len(attempts_log) or _REVIEWER_MAX_ATTEMPTS,
        )
    except Exception as exc:
        session.rollback()
        _log.error(
            "review_result_persist_failed",
            chapter_id=chapter_id,
            stage="commit_parse_failed",
            error_type=type(exc).__name__,
            error_message=str(exc),
        )
        raise ReviewCheckpointPersistenceError(
            f"parse-failed 审稿 checkpoint 提交失败: {chapter_id}/v{version_reviewed}"
        ) from exc
    finally:
        session.close()
