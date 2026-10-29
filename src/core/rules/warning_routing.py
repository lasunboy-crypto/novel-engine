"""结构化 WARN 的持久化、去重、过期与后续提示词路由。

WARN 复用 ``review_results.constraint_violations`` 既有 JSON 列，不新增数据库
迁移。只有自动审校已经 PASS、且该版本随后成为已批准章节 ``current_version``
的记录，才有资格进入下一章提示词；失败草稿、legacy 无路由元数据记录和维护者
专用告警一律不会注入 Writer/Planner。
"""

from __future__ import annotations

from typing import Any, Literal

from sqlalchemy import and_, select
from sqlalchemy.orm import Session

from src.core.db.models import ChapterORM, ReviewResultORM
from src.core.models.chapter_card import chapter_seq_from_id
from src.core.models.review import (
    ConstraintViolation,
    ReviewResult,
    stable_constraint_id,
)

WarningAudience = Literal["planner", "writer", "maintainer"]
WarningDomain = Literal[
    "world.unique_rules",
    "world.setting",
    "world.power_system",
    "character.identity",
    "character.motivation",
    "character.voice",
    "character.profession",
    "chapter.internal",
    "chapter.continuity",
    "chapter.timeline",
    "chapter.style",
    "chapter.dedup",
    "chapter.ending",
]

MAX_WARNINGS_PER_REVIEW = 24
MAX_ROUTED_WARNINGS_PER_AUDIENCE = 8


class WarningRoutingError(RuntimeError):
    """已批准 final 的结构化 WARN 账本无法安全解析。"""


def next_chapter_id(chapter_id: str) -> str | None:
    """返回相邻下一章 ID；非标准 ID 不猜测。"""
    seq = chapter_seq_from_id(chapter_id)
    return f"ch-{seq + 1:04d}" if seq is not None else None


def build_soft_warning(
    *,
    domain: WarningDomain,
    source_path: str,
    rule_text: str,
    evidence: str,
    audiences: list[WarningAudience],
    scope: Literal["current_revision", "next_chapter", "book", "maintenance"],
    confidence: Literal["low", "medium", "high"] = "medium",
    suggested_action: str | None = None,
    explanation: str | None = None,
    ambiguity: str | None = None,
) -> ConstraintViolation:
    """从稳定规则身份构造一条不会参与 verdict 的 SOFT 观察。"""
    return ConstraintViolation(
        constraint_id=stable_constraint_id(
            source_path=source_path,
            domain=domain,
            rule_text=rule_text,
        ),
        domain=domain,
        source_path=source_path,
        source_hash="",
        rule_quote=rule_text,
        evidence_quote=evidence or "未提供可定位证据",
        severity="soft",
        ambiguity=ambiguity,
        audiences=list(dict.fromkeys(audiences)),
        scope=scope,
        confidence=confidence,
        suggested_action=suggested_action,
        explanation=explanation,
    )


def _default_warning_route(
    warning: ConstraintViolation,
) -> tuple[
    list[WarningAudience],
    Literal["current_revision", "next_chapter", "book", "maintenance"],
    str,
]:
    """给本轮新 SOFT 补显式路由；不用于 legacy 历史记录。"""
    if warning.domain == "chapter.continuity":
        return ["planner"], "next_chapter", "下一章规划时修正衔接表达或到场交代。"
    if warning.domain in {"chapter.style", "chapter.dedup"}:
        return ["writer"], "next_chapter", "下一章起草时作为非阻断 watchlist 参考。"
    if warning.domain in {"character.voice", "character.profession"}:
        return ["writer", "maintainer"], "next_chapter", "核对说话归属或角色身份后再决定是否调整。"
    if warning.domain == "chapter.ending":
        return ["planner", "maintainer"], "maintenance", "由维护者复核终局设计，不触发正文自动重写。"
    if warning.domain == "chapter.timeline":
        return ["maintainer"], "maintenance", "检查时间线数据；正文改写无法修复该数据问题。"
    return ["maintainer"], "maintenance", "由维护者核对不确定事实；不得自动升级为 HARD。"


def finalize_review_warning_signals(review: ReviewResult) -> ReviewResult:
    """给本轮 SOFT 补来源/路由并按稳定规则 ID 去重、封顶。

    只有 ``verdict=pass`` 且所有 HARD 通过的版本才写
    ``source_final_version``。失败尝试仍保留当前章审计观察，但天然不具备后续
    传播资格。
    """
    hard_items = [item for item in review.constraint_violations if item.severity == "hard"]
    soft_items = [item for item in review.constraint_violations if item.severity == "soft"]
    eligible_final_version = (
        review.version_reviewed
        if review.verdict == "pass" and review.hard_constraints_passed
        else None
    )
    expiry = next_chapter_id(review.chapter_id)

    positions: dict[str, int] = {}
    finalized: list[ConstraintViolation] = []
    for item in soft_items:
        audiences = list(item.audiences)
        scope = item.scope
        suggested_action = item.suggested_action
        if not audiences:
            audiences, scope, default_action = _default_warning_route(item)
            suggested_action = suggested_action or default_action
        routed = item.model_copy(
            update={
                "source_chapter_id": review.chapter_id,
                "source_final_version": eligible_final_version,
                "audiences": audiences,
                "scope": scope,
                "expires_after_chapter_id": (
                    item.expires_after_chapter_id
                    or (expiry if scope == "next_chapter" else None)
                ),
                "suggested_action": suggested_action,
            }
        )
        old_position = positions.get(routed.constraint_id)
        if old_position is None:
            positions[routed.constraint_id] = len(finalized)
            finalized.append(routed)
        else:
            # 同一规则本轮多次命中时保留最后一条证据，不重复挤占 prompt。
            finalized[old_position] = routed

    if len(finalized) > MAX_WARNINGS_PER_REVIEW:
        finalized = finalized[-MAX_WARNINGS_PER_REVIEW:]
    return review.model_copy(update={"constraint_violations": hard_items + finalized})


def _warning_applies_to_target(
    warning: ConstraintViolation, *, target_chapter_id: str
) -> bool:
    if warning.resolution != "open" or warning.source_chapter_id is None:
        return False
    target_seq = chapter_seq_from_id(target_chapter_id)
    source_seq = chapter_seq_from_id(warning.source_chapter_id)
    if target_seq is None or source_seq is None or target_seq <= source_seq:
        return False
    if warning.scope == "next_chapter":
        return target_seq == source_seq + 1
    if warning.scope == "book":
        if warning.expires_after_chapter_id is None:
            return True
        expiry_seq = chapter_seq_from_id(warning.expires_after_chapter_id)
        return expiry_seq is not None and target_seq <= expiry_seq
    return False


def load_approved_final_warnings(
    session: Session,
    *,
    workspace_id: str,
    target_chapter_id: str,
    audience: Literal["planner", "writer"],
) -> list[ConstraintViolation]:
    """读取可向目标章节传播的已批准 final WARN。

    查询只接受 ``Chapter.status in {approved,published}``、review 版本等于章节
    ``current_version``、结构化记录自报的 final 版本也完全相等。任一 payload
    损坏都按账本损坏硬停，不能静默把未知数据塞进付费提示词。
    """
    rows = session.execute(
        select(
            ChapterORM.chapter_id,
            ChapterORM.current_version,
            ReviewResultORM.review_id,
            ReviewResultORM.reviewed_at,
            ReviewResultORM.verdict,
            ReviewResultORM.hard_constraints_passed,
            ReviewResultORM.constraint_violations,
        )
        .join(
            ReviewResultORM,
            and_(
                ReviewResultORM.chapter_id == ChapterORM.chapter_id,
                ReviewResultORM.version_reviewed == ChapterORM.current_version,
            ),
        )
        .where(ChapterORM.workspace_id == workspace_id)
        .where(ChapterORM.chapter_id < target_chapter_id)
        .where(ChapterORM.status.in_(("approved", "published")))
        .order_by(
            ChapterORM.chapter_id.desc(),
            ReviewResultORM.reviewed_at.desc(),
            ReviewResultORM.review_id.desc(),
        )
    ).all()

    # 同一 final 被重复审阅时只认最新一条完整 PASS review。
    latest_by_chapter: dict[str, Any] = {}
    for row in rows:
        latest_by_chapter.setdefault(row.chapter_id, row)

    routed: list[ConstraintViolation] = []
    seen_ids: set[str] = set()
    for row in latest_by_chapter.values():
        if row.verdict != "pass" or not row.hard_constraints_passed:
            continue
        payloads = row.constraint_violations
        if not isinstance(payloads, list):
            raise WarningRoutingError(
                f"已批准 final WARN 账本形状损坏: {row.chapter_id}/review={row.review_id}"
            )
        for payload in reversed(payloads):
            try:
                warning = ConstraintViolation.model_validate(payload)
            except Exception as exc:  # noqa: BLE001 - checkpoint 损坏必须硬停
                raise WarningRoutingError(
                    "已批准 final WARN 账本无法解析: "
                    f"{row.chapter_id}/review={row.review_id}"
                ) from exc
            if warning.severity != "soft" or audience not in warning.audiences:
                continue
            if (
                warning.source_chapter_id != row.chapter_id
                or warning.source_final_version != row.current_version
            ):
                continue
            if not _warning_applies_to_target(
                warning, target_chapter_id=target_chapter_id
            ):
                continue
            if warning.constraint_id in seen_ids:
                continue
            seen_ids.add(warning.constraint_id)
            routed.append(warning)
            if len(routed) >= MAX_ROUTED_WARNINGS_PER_AUDIENCE:
                return list(reversed(routed))
    return list(reversed(routed))


def render_warning_watchlist(
    warnings: list[ConstraintViolation], *, audience: Literal["planner", "writer"],
    include_style_observations: bool = True,
) -> str:
    """渲染明确标注 SOFT/nonblocking 的下游 watchlist。"""
    if audience == "writer" and not include_style_observations:
        warnings = [warning for warning in warnings if warning.domain != "chapter.style"]
    if not warnings:
        return ""
    audience_label = "章节规划" if audience == "planner" else "正文起草"
    lines = [
        "## 已批准前文 WARN（SOFT WATCHLIST；绝不触发打回）",
        "",
        f"以下信号只供本章{audience_label}参考。它们不是 failed_constraints，"
        "不得覆盖本章 ChapterCard/权威世界契约，也不得在 rewrite/revise 中升级为 HARD。",
        "",
    ]
    for warning in warnings:
        explanation = (warning.explanation or warning.rule_quote)[:240]
        evidence = warning.evidence_quote[:300]
        action = (warning.suggested_action or "结合本章情境酌情处理。")[:240]
        lines.append(
            f"- [{warning.constraint_id[:12]}] 来源 "
            f"{warning.source_chapter_id}/v{warning.source_final_version}；"
            f"观察：{explanation}；证据：{evidence}；建议：{action}"
        )
    return "\n".join(lines)


__all__ = [
    "MAX_ROUTED_WARNINGS_PER_AUDIENCE",
    "MAX_WARNINGS_PER_REVIEW",
    "WarningRoutingError",
    "build_soft_warning",
    "finalize_review_warning_signals",
    "load_approved_final_warnings",
    "next_chapter_id",
    "render_warning_watchlist",
]
