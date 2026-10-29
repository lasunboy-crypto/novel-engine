"""R9 结构化 WARN 生命周期与下游路由回归。"""

from __future__ import annotations

from datetime import UTC, datetime, timedelta

import pytest
from sqlalchemy import create_engine
from sqlalchemy.orm import Session

from src.core.db.models import Base, ChapterORM, ReviewResultORM, WorkspaceORM
from src.core.models.review import ReviewResult
from src.core.rules.warning_routing import (
    WarningRoutingError,
    build_soft_warning,
    finalize_review_warning_signals,
    load_approved_final_warnings,
    render_warning_watchlist,
)


def _review(
    *,
    chapter_id: str = "ch-0002",
    version: int = 3,
    verdict: str = "pass",
    hard_passed: bool = True,
    failed_constraints: list[str] | None = None,
    violations: list | None = None,
) -> ReviewResult:
    now = datetime.now(UTC)
    return ReviewResult(
        review_id=f"review-{chapter_id}-{version}-{verdict}",
        chapter_id=chapter_id,
        version_reviewed=version,
        reviewed_at=now,
        reviewer_model="test-reviewer",
        score_plot_progression=8.0,
        score_tension_match=8.0,
        score_prose_quality=8.0,
        score_consistency=8.0,
        score_hook_management=8.0,
        score_voice_consistency=8.0,
        score_overall=8.0,
        hard_constraints_passed=hard_passed,
        failed_constraints=failed_constraints or [],
        issues=[],
        suggestions=[],
        verdict=verdict,
        revise_targets=[],
        constraint_violations=violations or [],
    )


def _style_warning(*, evidence: str = "段落均值偏高"):
    return build_soft_warning(
        domain="chapter.style",
        source_path="runtime.style.cross_chapter.paragraph_mean",
        rule_text="跨章段均偏差超过 50% 只作观察",
        evidence=evidence,
        audiences=["writer"],
        scope="next_chapter",
        confidence="high",
        suggested_action="下一章留意自然段落呼吸，不机械追齐前章。",
    )


def test_finalize_pass_stamps_final_source_dedupes_and_keeps_verdict() -> None:
    first = _style_warning(evidence="旧证据")
    last = first.model_copy(update={"evidence_quote": "最新证据"})

    result = finalize_review_warning_signals(
        _review(violations=[first, last])
    )

    assert result.verdict == "pass"
    assert result.hard_constraints_passed is True
    assert result.failed_constraints == []
    assert len(result.constraint_violations) == 1
    warning = result.constraint_violations[0]
    assert warning.evidence_quote == "最新证据"
    assert warning.source_chapter_id == "ch-0002"
    assert warning.source_final_version == 3
    assert warning.expires_after_chapter_id == "ch-0003"


def test_failed_attempt_warning_never_becomes_hard_or_propagatable() -> None:
    result = finalize_review_warning_signals(
        _review(
            verdict="rewrite",
            hard_passed=False,
            failed_constraints=["setting"],
            violations=[_style_warning()],
        )
    )

    warning = result.constraint_violations[0]
    assert result.verdict == "rewrite"
    assert result.failed_constraints == ["setting"]
    assert warning.severity == "soft"
    assert warning.source_final_version is None


def _workspace() -> WorkspaceORM:
    now = datetime.now(UTC)
    return WorkspaceORM(
        workspace_id="ws-r9",
        display_name="R9 warning routing",
        genre="test",
        premise="test",
        created_at=now,
        last_active_at=now,
        engine_version="0.1.0",
        status="active",
        total_chapters=3,
        completed_chapters=1,
        config_overrides={},
    )


def _chapter(*, chapter_id: str, version: int, status: str) -> ChapterORM:
    now = datetime.now(UTC)
    return ChapterORM(
        chapter_id=chapter_id,
        workspace_id="ws-r9",
        current_version=version,
        status=status,
        rewrite_count=0,
        revise_count=0,
        created_at=now,
        approved_at=now if status == "approved" else None,
        total_token_usage={},
        total_cost_usd=0.0,
    )


def _review_row(
    *,
    review_id: str,
    chapter_id: str,
    version: int,
    payloads: list,
    reviewed_at: datetime | None = None,
    verdict: str = "pass",
    hard_passed: bool = True,
) -> ReviewResultORM:
    return ReviewResultORM(
        review_id=review_id,
        chapter_id=chapter_id,
        version_reviewed=version,
        reviewed_at=reviewed_at or datetime.now(UTC),
        reviewer_model="test-reviewer",
        score_plot_progression=8.0,
        score_tension_match=8.0,
        score_prose_quality=8.0,
        score_consistency=8.0,
        score_hook_management=8.0,
        score_voice_consistency=8.0,
        score_overall=8.0,
        hard_constraints_passed=hard_passed,
        failed_constraints=[] if hard_passed else ["setting"],
        constraint_violations=payloads,
        constraint_results=[],
        hard_constraint_regressions=[],
        suggestions=[],
        verdict=verdict,
    )


def test_loader_routes_only_approved_current_pass_final() -> None:
    engine = create_engine("sqlite:///:memory:")
    Base.metadata.create_all(engine)
    current = finalize_review_warning_signals(
        _review(violations=[_style_warning()])
    ).constraint_violations[0]
    old = current.model_copy(
        update={"source_final_version": 2, "evidence_quote": "旧版本不得传播"}
    )
    draft = current.model_copy(
        update={
            "source_chapter_id": "ch-0001",
            "source_final_version": 1,
            "scope": "book",
            "expires_after_chapter_id": "ch-0003",
        }
    )

    with Session(engine) as session:
        session.add(_workspace())
        session.add_all(
            [
                _chapter(chapter_id="ch-0001", version=1, status="drafting"),
                _chapter(chapter_id="ch-0002", version=3, status="approved"),
            ]
        )
        session.add_all(
            [
                _review_row(
                    review_id="review-draft",
                    chapter_id="ch-0001",
                    version=1,
                    payloads=[draft.model_dump(mode="json")],
                ),
                _review_row(
                    review_id="review-old",
                    chapter_id="ch-0002",
                    version=2,
                    payloads=[old.model_dump(mode="json")],
                ),
                _review_row(
                    review_id="review-current",
                    chapter_id="ch-0002",
                    version=3,
                    payloads=[current.model_dump(mode="json")],
                ),
            ]
        )
        session.commit()

        routed = load_approved_final_warnings(
            session,
            workspace_id="ws-r9",
            target_chapter_id="ch-0003",
            audience="writer",
        )

    assert [item.evidence_quote for item in routed] == ["段落均值偏高"]
    assert routed[0].source_chapter_id == "ch-0002"
    assert routed[0].source_final_version == 3


def test_latest_nonpass_review_blocks_older_pass_for_same_final() -> None:
    engine = create_engine("sqlite:///:memory:")
    Base.metadata.create_all(engine)
    warning = finalize_review_warning_signals(
        _review(violations=[_style_warning()])
    ).constraint_violations[0]
    now = datetime.now(UTC)

    with Session(engine) as session:
        session.add(_workspace())
        session.add(_chapter(chapter_id="ch-0002", version=3, status="approved"))
        session.add_all(
            [
                _review_row(
                    review_id="review-pass",
                    chapter_id="ch-0002",
                    version=3,
                    payloads=[warning.model_dump(mode="json")],
                    reviewed_at=now,
                ),
                _review_row(
                    review_id="review-later-fail",
                    chapter_id="ch-0002",
                    version=3,
                    payloads=[warning.model_dump(mode="json")],
                    reviewed_at=now + timedelta(seconds=1),
                    verdict="rewrite",
                    hard_passed=False,
                ),
            ]
        )
        session.commit()

        assert load_approved_final_warnings(
            session,
            workspace_id="ws-r9",
            target_chapter_id="ch-0003",
            audience="writer",
        ) == []


def test_malformed_approved_final_warning_ledger_hard_stops() -> None:
    engine = create_engine("sqlite:///:memory:")
    Base.metadata.create_all(engine)
    with Session(engine) as session:
        session.add(_workspace())
        session.add(_chapter(chapter_id="ch-0002", version=3, status="approved"))
        session.add(
            _review_row(
                review_id="review-broken",
                chapter_id="ch-0002",
                version=3,
                payloads=[{"not": "a constraint violation"}],
            )
        )
        session.commit()

        with pytest.raises(WarningRoutingError, match="无法解析"):
            load_approved_final_warnings(
                session,
                workspace_id="ws-r9",
                target_chapter_id="ch-0003",
                audience="writer",
            )


def test_loader_filters_resolution_audience_and_expiry_then_caps() -> None:
    engine = create_engine("sqlite:///:memory:")
    Base.metadata.create_all(engine)
    base = finalize_review_warning_signals(
        _review(violations=[_style_warning()])
    ).constraint_violations[0]
    book_warnings = [
        base.model_copy(
            update={
                "constraint_id": f"{index:064x}",
                "source_chapter_id": "ch-0002",
                "source_final_version": 3,
                "scope": "book",
                "expires_after_chapter_id": "ch-0006",
                "evidence_quote": f"有效观察 {index}",
            }
        )
        for index in range(10)
    ]
    filtered = [
        base.model_copy(
            update={
                "constraint_id": "f" * 64,
                "source_chapter_id": "ch-0002",
                "source_final_version": 3,
                "scope": "book",
                "resolution": "resolved",
            }
        ),
        base.model_copy(
            update={
                "constraint_id": "e" * 64,
                "source_chapter_id": "ch-0002",
                "source_final_version": 3,
                "scope": "book",
                "audiences": ["planner"],
            }
        ),
        base.model_copy(
            update={
                "constraint_id": "d" * 64,
                "source_chapter_id": "ch-0002",
                "source_final_version": 3,
                "scope": "book",
                "expires_after_chapter_id": "ch-0003",
            }
        ),
    ]

    with Session(engine) as session:
        session.add(_workspace())
        session.add(_chapter(chapter_id="ch-0002", version=3, status="approved"))
        session.add(
            _review_row(
                review_id="review-filter-cap",
                chapter_id="ch-0002",
                version=3,
                payloads=[
                    item.model_dump(mode="json")
                    for item in [*book_warnings, *filtered]
                ],
            )
        )
        session.commit()

        routed = load_approved_final_warnings(
            session,
            workspace_id="ws-r9",
            target_chapter_id="ch-0004",
            audience="writer",
        )

    assert len(routed) == 8
    assert {item.constraint_id for item in routed} == {
        f"{index:064x}" for index in range(2, 10)
    }
    assert all(item.resolution == "open" for item in routed)
    assert all("writer" in item.audiences for item in routed)


def test_rendered_watchlist_is_explicitly_soft_and_nonblocking() -> None:
    warning = finalize_review_warning_signals(
        _review(violations=[_style_warning()])
    ).constraint_violations[0]
    rendered = render_warning_watchlist([warning], audience="writer")

    assert "SOFT WATCHLIST" in rendered
    assert "绝不触发打回" in rendered
    assert "不是 failed_constraints" in rendered
    assert "不得在 rewrite/revise 中升级为 HARD" in rendered
