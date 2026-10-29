"""R9 v5 冻结证据支持的最小误放行纯代码护栏。"""

from __future__ import annotations

import re
from collections.abc import Sequence

import structlog

from src.core.agents.must_achieve_validator import ValidationResult
from src.core.models.review import ConstraintResult, ReviewIssue, ReviewResult
from src.core.rules.prose_hard_contract import ExecutableConstraintPlan
from src.core.rules.prose_revision_contract import (
    LocalizedAdjacentDuplicate,
    ProseRevisionContract,
    ProseRevisionContractError,
    find_localized_adjacent_duplicates,
    revision_evidence_is_source_bound,
)

_log = structlog.get_logger(__name__)

_PARAGRAPH_SEPARATOR = re.compile(r"(?:\r?\n[ \t]*){2,}")
_DUPLICATE_ISSUE_TERMS = ("重复", "复读", "冗余", "重合", "雷同")
_ADJACENCY_ISSUE_TERMS = ("相邻", "上一段", "下一段", "紧接", "连续两段", "本段与")
_META_TERM = "上一章"
_META_SIGNAL_TERMS = ("元叙事", "第四面墙", "沉浸", "出戏", "引擎结构")
_META_PROTECTED_CONTEXT_TERMS = (
    "档案",
    "书信",
    "手稿",
    "目录",
    "标题",
    "书中书",
    "小说",
    "作者",
    "读者",
    "编辑",
    "章节结构",
    "章节安排",
    "讨论章节",
)
_QUOTE_PAIRS = (("“", "”"), ("「", "」"), ("『", "』"), ("《", "》"), ('"', '"'))
_CANONICAL_MUST_TRANSITION_FAILURE = "must_achieve_transition_unverified"


def _previous_failed_must_by_id(
    source_review: ReviewResult | None,
) -> dict[str, ConstraintResult]:
    if source_review is None:
        return {}
    return {
        item.constraint_id: item
        for item in source_review.constraint_results
        if item.domain == "must_achieve" and item.status == "fail"
    }


def _assert_transition_identity(
    *,
    source_review: ReviewResult,
    candidate_version_id: int,
    revision_contract: ProseRevisionContract,
    executable_constraint_plan: ExecutableConstraintPlan,
) -> None:
    if source_review.version_reviewed != revision_contract.source_version_id:
        raise ProseRevisionContractError(
            "must fail→pass 的源 ReviewResult 与 revision source version 不一致"
        )
    if candidate_version_id != revision_contract.source_version_id + 1:
        raise ProseRevisionContractError(
            "must fail→pass 的候选版本不是冻结源的直接后继"
        )
    if executable_constraint_plan.plan_hash != revision_contract.plan_hash:
        raise ProseRevisionContractError(
            "must fail→pass 的 ExecutableConstraintPlan 与 revision contract 漂移"
        )


def _transition_quotes(
    *, result: ValidationResult, current: ConstraintResult,
    previous: ConstraintResult, candidate_text: str,
) -> tuple[str, ...]:
    """复合要求只取上一版失败分项的本次原文证据，避免丢失真正修复。"""
    proofs = result.coverage_evidence.get(current.constraint_id, ())
    if not proofs:
        return (current.details,)
    if (
        "".join(proof.requirement_part for proof in proofs) != current.rule_text
        or any(not proof.evidence or proof.evidence not in candidate_text for proof in proofs)
    ):
        raise ProseRevisionContractError("must fail→pass 的分项原文证据发生漂移")
    prefix = "未完成分项["
    if not previous.details.startswith(prefix) or "]: " not in previous.details:
        return (current.details,)
    failed_part = previous.details[len(prefix):].split("]: ", 1)[0]
    start = current.rule_text.find(failed_part)
    if not failed_part or start < 0 or current.rule_text.find(failed_part, start + 1) >= 0:
        return (current.details,)
    end = start + len(failed_part)
    quotes: list[str] = []
    cursor = 0
    for proof in proofs:
        next_cursor = cursor + len(proof.requirement_part)
        if cursor < end and next_cursor > start:
            quotes.append(proof.evidence)
        cursor = next_cursor
    return tuple(quotes) or (current.details,)


def guard_must_fail_to_pass(
    *,
    result: ValidationResult,
    source_review: ReviewResult | None,
    candidate_text: str,
    candidate_version_id: int,
    revision_contract: ProseRevisionContract | None,
    executable_constraint_plan: ExecutableConstraintPlan | None,
) -> ValidationResult:
    """阻止同一 must ID 在无 source-bound 逐字证据时从 fail 随机翻 pass。

    首稿、没有上一版同 ID fail、或不带冻结修订合同的路径逐对象返回，保持旧语义。
    该函数只消费 Judge 已返回的临时字段，不发调用、不触发 schema retry。
    """

    previous_failed = _previous_failed_must_by_id(source_review)
    if (
        previous_failed
        and source_review is not None
        and revision_contract is not None
        and executable_constraint_plan is not None
        and not result.skipped
        and not result.fail_open
    ):
        # 当前结果的 ID 正常由同一冻结计划在代码侧生成，不来自 Provider。
        # 若语义三元组完全相同却换了 ID，说明稳定身份或计划接线漂移；不能因
        # ``constraint_id`` 对不上而绕过下面的 fail→pass 护栏。
        for current in result.constraint_results:
            if current.domain != "must_achieve" or current.status != "pass":
                continue
            semantic_match = next(
                (
                    previous
                    for previous in previous_failed.values()
                    if current.source_path == previous.source_path
                    and current.rule_text == previous.rule_text
                    and current.domain == previous.domain
                ),
                None,
            )
            if (
                semantic_match is not None
                and current.constraint_id != semantic_match.constraint_id
            ):
                _assert_transition_identity(
                    source_review=source_review,
                    candidate_version_id=candidate_version_id,
                    revision_contract=revision_contract,
                    executable_constraint_plan=executable_constraint_plan,
                )
                raise ProseRevisionContractError(
                    "must fail→pass 的同语义 constraint_id 发生漂移"
                )
    transitioning = [
        item
        for item in result.constraint_results
        if item.domain == "must_achieve"
        and item.status == "pass"
        and item.constraint_id in previous_failed
    ]
    if not transitioning:
        return result
    if (
        source_review is None
        or revision_contract is None
        or executable_constraint_plan is None
    ):
        # 只处理“候选来自冻结源版本”的授权窄场景；人工稿与首稿不在此猜来源。
        return result

    _assert_transition_identity(
        source_review=source_review,
        candidate_version_id=candidate_version_id,
        revision_contract=revision_contract,
        executable_constraint_plan=executable_constraint_plan,
    )

    invalid_ids: set[str] = set()
    verified_quotes: dict[str, str] = {}
    for current in transitioning:
        previous = previous_failed[current.constraint_id]
        if (
            current.source_path != previous.source_path
            or current.rule_text != previous.rule_text
            or current.domain != previous.domain
        ):
            raise ProseRevisionContractError(
                "must fail→pass 的稳定 constraint_id 对应规则语义发生漂移"
            )
        verified = next((
            quote for quote in _transition_quotes(
                result=result, current=current, previous=previous, candidate_text=candidate_text,
            )
            if revision_evidence_is_source_bound(
                contract=revision_contract, candidate_content=candidate_text, evidence=quote,
            )
        ), None)
        if verified is None:
            invalid_ids.add(current.constraint_id)
        else:
            verified_quotes[current.constraint_id] = verified

    if not invalid_ids and all(
        item.details == verified_quotes.get(item.constraint_id, item.details)
        for item in result.constraint_results
    ):
        return result

    guarded_results: list[ConstraintResult] = []
    missing_items = list(result.missing_items)
    for current in result.constraint_results:
        if current.constraint_id not in invalid_ids:
            guarded_results.append(
                current.model_copy(update={"details": verified_quotes[current.constraint_id]})
                if current.constraint_id in verified_quotes else current
            )
            continue
        previous = previous_failed[current.constraint_id]
        canonical_evidence = " ".join(previous.details.split()) or (
            _CANONICAL_MUST_TRANSITION_FAILURE
        )
        guarded_results.append(
            current.model_copy(
                update={"status": "fail", "details": canonical_evidence}
            )
        )
        if current.rule_text not in missing_items:
            missing_items.append(current.rule_text)

    if invalid_ids:
        _log.warning(
            "must_achieve_fail_to_pass_evidence_rejected",
            source_version_id=revision_contract.source_version_id,
            candidate_version_id=candidate_version_id,
            constraint_ids=sorted(invalid_ids),
            llm_calls_count=result.llm_calls_count,
        )
    return ValidationResult(
        passed=not any(item.status == "fail" for item in guarded_results),
        missing_items=missing_items,
        constraint_results=guarded_results,
        llm_calls_count=result.llm_calls_count,
        skipped=result.skipped,
        fail_open=result.fail_open,
        coverage_evidence=result.coverage_evidence,
    )


def _paragraph_spans(text: str) -> list[tuple[int, int, str]]:
    spans: list[tuple[int, int, str]] = []
    cursor = 0
    for separator in _PARAGRAPH_SEPARATOR.finditer(text):
        raw = text[cursor : separator.start()]
        if raw.strip():
            leading = len(raw) - len(raw.lstrip())
            trailing = len(raw.rstrip())
            spans.append((cursor + leading, cursor + trailing, raw.strip()))
        cursor = separator.end()
    raw = text[cursor:]
    if raw.strip():
        leading = len(raw) - len(raw.lstrip())
        spans.append((cursor + leading, len(text.rstrip()), raw.strip()))
    return spans


def _unique_issue_anchor_span(
    *, issue: ReviewIssue, chapter_text: str
) -> tuple[int, int] | None:
    anchor = issue.paragraph_anchor.strip()
    if not anchor:
        return None
    start = chapter_text.find(anchor)
    if start < 0 or chapter_text.find(anchor, start + 1) >= 0:
        return None
    return start, start + len(anchor)


def _reviewer_duplicate_signal(
    *,
    issues: Sequence[ReviewIssue],
    chapter_text: str,
    finding: LocalizedAdjacentDuplicate,
) -> ReviewIssue | None:
    for issue in issues:
        signal = " ".join(
            (issue.issue_type, issue.description, issue.suggestion)
        ).casefold()
        if not any(term in signal for term in _DUPLICATE_ISSUE_TERMS):
            continue
        if not any(term in signal for term in _ADJACENCY_ISSUE_TERMS):
            continue
        span = _unique_issue_anchor_span(issue=issue, chapter_text=chapter_text)
        if span is None:
            continue
        anchor = issue.paragraph_anchor.strip()
        if anchor in finding.changed_text or anchor in finding.neighbor_text:
            return issue
    return None


def _duplicate_revise_issue(
    *, finding: LocalizedAdjacentDuplicate, reviewer_issue: ReviewIssue | None
) -> ReviewIssue:
    if reviewer_issue is None:
        description = (
            "LOCALIZED 新增或替换的长段与紧邻保留段发生可代码确认的重复。"
        )
        suggestion = "仅处理该 changed paragraph，删除复写内容并保留原修订目标。"
    else:
        description = reviewer_issue.description
        suggestion = reviewer_issue.suggestion
    return ReviewIssue(
        # 候选内两个段落可能逐字相同；已有字段直接承载本模块稳定 anchor_id，
        # 下一轮 Reviser 合同按 ID 唯一定位，不需要新增 schema。
        paragraph_anchor=finding.changed_anchor_id,
        issue_type="adjacent_duplicate",
        description=description,
        suggestion=suggestion,
    )


def _term_is_inside_quote(paragraph: str, *, term_start: int, term_end: int) -> bool:
    for left, right in _QUOTE_PAIRS:
        left_index = paragraph.rfind(left, 0, term_start + 1)
        if left_index < 0:
            continue
        right_index = paragraph.find(right, term_end)
        if right_index >= 0 and (left != right or right_index > left_index):
            return True
    return False


def _meta_issue_is_confirmed(*, issue: ReviewIssue, chapter_text: str) -> bool:
    signal = " ".join((issue.issue_type, issue.description, issue.suggestion))
    if _META_TERM not in signal or not any(
        term in signal for term in _META_SIGNAL_TERMS
    ):
        return False
    anchor_span = _unique_issue_anchor_span(issue=issue, chapter_text=chapter_text)
    if anchor_span is None or _META_TERM not in issue.paragraph_anchor:
        return False

    term_start = chapter_text.find(_META_TERM, anchor_span[0], anchor_span[1])
    if term_start < 0:
        return False
    paragraph_row = next(
        (
            row
            for row in _paragraph_spans(chapter_text)
            if row[0] <= term_start < row[1]
        ),
        None,
    )
    if paragraph_row is None:
        return False
    paragraph_start, _paragraph_end, paragraph = paragraph_row
    local_start = term_start - paragraph_start
    local_end = local_start + len(_META_TERM)
    if _term_is_inside_quote(
        paragraph, term_start=local_start, term_end=local_end
    ):
        return False
    if paragraph.lstrip().startswith(("—", "——")):
        return False
    if any(term in paragraph for term in _META_PROTECTED_CONTEXT_TERMS):
        return False
    return True


def apply_objective_reviewer_guards(
    *,
    review: ReviewResult,
    chapter_text: str,
    revision_contract: ProseRevisionContract | None,
) -> ReviewResult:
    """只把两类已证实客观问题从高分 pass 降为现有 revise。"""

    if review.verdict != "pass":
        return review

    confirmed_targets: list[ReviewIssue] = []
    synthetic_issues: list[ReviewIssue] = []
    if revision_contract is not None and revision_contract.mode == "localized":
        for finding in find_localized_adjacent_duplicates(
            contract=revision_contract,
            candidate_content=chapter_text,
        ):
            reviewer_issue = _reviewer_duplicate_signal(
                issues=review.issues,
                chapter_text=chapter_text,
                finding=finding,
            )
            if finding.match_kind == "high_overlap" and reviewer_issue is None:
                continue
            target = _duplicate_revise_issue(
                finding=finding,
                reviewer_issue=reviewer_issue,
            )
            confirmed_targets.append(target)
            synthetic_issues.append(target)

    confirmed_targets.extend(
        issue
        for issue in review.issues
        if _meta_issue_is_confirmed(issue=issue, chapter_text=chapter_text)
    )
    if not confirmed_targets:
        return review

    deduplicated_targets = list(
        {
            (
                item.paragraph_anchor,
                item.issue_type,
                item.description,
                item.suggestion,
            ): item
            for item in confirmed_targets
        }.values()
    )
    known_issues = {
        (
            item.paragraph_anchor,
            item.issue_type,
            item.description,
            item.suggestion,
        )
        for item in review.issues
    }
    issues = list(review.issues)
    issues.extend(
        item
        for item in synthetic_issues
        if (
            item.paragraph_anchor,
            item.issue_type,
            item.description,
            item.suggestion,
        )
        not in known_issues
    )
    _log.info(
        "reviewer_objective_issue_overrode_pass",
        chapter_id=review.chapter_id,
        version_reviewed=review.version_reviewed,
        issue_types=[item.issue_type for item in deduplicated_targets],
    )
    return review.model_copy(
        update={
            "verdict": "revise",
            "issues": issues,
            "revise_targets": deduplicated_targets,
        }
    )


__all__ = [
    "apply_objective_reviewer_guards",
    "guard_must_fail_to_pass",
]
