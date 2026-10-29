"""B6 终局张力门控测试(v1.1 wave 6 session 4)。

覆盖 orchestrator 内 ``_apply_ending_tension_validator`` 门控逻辑(mock LLM):

1. **非 arc finale**:``is_arc_finale=False`` → 直接返回,不查 DB 不调 LLM
2. **arc finale 但非全书终章**:seq < total → 返回
3. **session_factory None**:P1 兼容 → 返回
4. **真终章 + earned**:触发 LLM,review 不动
5. **真终章 + abrupt**:触发 LLM,evidence 合并进 suggestions
6. **异常 swallow**:session 抛 → 返原 review

agent 本身单测见 ``test_ending_tension_validator.py``。
"""

from __future__ import annotations

import json
from datetime import UTC, datetime
from types import SimpleNamespace
from unittest.mock import MagicMock

import pytest
import structlog
from sqlalchemy import create_engine
from sqlalchemy.orm import sessionmaker
from sqlalchemy.pool import StaticPool

from src.core.db import Base, WorkspaceORM
from src.core.llm.base import LLMClient, LLMResponse, TokenUsage
from src.core.models.review import ReviewResult
from src.core.orchestrator.chapter_orchestrator import (
    _apply_ending_tension_validator,
)

_WS = "ws-demo"


@pytest.fixture(autouse=True)
def _ensure_debug_level() -> None:
    structlog.configure(wrapper_class=structlog.make_filtering_bound_logger(0))


@pytest.fixture
def session_factory():  # noqa: ANN201
    engine = create_engine(
        "sqlite://",
        connect_args={"check_same_thread": False},
        poolclass=StaticPool,
    )
    Base.metadata.create_all(engine)
    return sessionmaker(bind=engine)


def _seed_workspace(maker, total_chapters: int) -> None:
    with maker() as s:
        s.add(
            WorkspaceORM(
                workspace_id=_WS,
                display_name="demo",
                genre="悬疑",
                premise="一句话设定",
                created_at=datetime.now(UTC),
                last_active_at=datetime.now(UTC),
                engine_version="v1.1",
                status="active",
                total_chapters=total_chapters,
                completed_chapters=0,
            )
        )
        s.commit()


def _card(chapter_id: str, *, is_arc_finale: bool) -> SimpleNamespace:
    return SimpleNamespace(
        chapter_id=chapter_id, is_arc_finale=is_arc_finale, tension_level=8
    )


def _review() -> ReviewResult:
    return ReviewResult(
        review_id="rev-030",
        chapter_id="ch-0030",
        version_reviewed=1,
        reviewed_at=datetime.now(UTC),
        reviewer_model="qwen-novel:latest",
        score_plot_progression=8.0,
        score_tension_match=8.0,
        score_prose_quality=8.0,
        score_consistency=8.0,
        score_hook_management=8.0,
        score_voice_consistency=8.0,
        score_overall=8.5,
        hard_constraints_passed=True,
        failed_constraints=[],
        issues=[],
        suggestions=[],
        verdict="pass",
        revise_targets=[],
    )


def _mock_client(verdict: str = "earned") -> MagicMock:
    client = MagicMock(spec=LLMClient)
    client.complete.return_value = LLMResponse(
        content=json.dumps({"verdict": verdict, "evidence": "判定依据"}),
        model_used="qwen",
        usage=TokenUsage(input_tokens=300, output_tokens=20),
        finish_reason="stop",
    )
    return client


def test_non_arc_finale_skips(session_factory) -> None:  # noqa: ANN001
    _seed_workspace(session_factory, total_chapters=30)
    client = _mock_client()
    out = _apply_ending_tension_validator(
        review=_review(),
        chapter_text="正文",
        chapter_card=_card("ch-0010", is_arc_finale=False),
        workspace_id=_WS,
        session_factory=session_factory,
        router=None,
        client=client,
        model="mock",
    )
    client.complete.assert_not_called()
    assert out.suggestions == []


def test_arc_finale_but_not_book_end_skips(session_factory) -> None:  # noqa: ANN001
    """卷末(is_arc_finale)但非全书终章(seq<total)→ 不调 LLM。"""
    _seed_workspace(session_factory, total_chapters=30)
    client = _mock_client()
    _apply_ending_tension_validator(
        review=_review(),
        chapter_text="正文",
        chapter_card=_card("ch-0010", is_arc_finale=True),
        workspace_id=_WS,
        session_factory=session_factory,
        router=None,
        client=client,
        model="mock",
    )
    client.complete.assert_not_called()


def test_session_factory_none_skips() -> None:
    client = _mock_client()
    _apply_ending_tension_validator(
        review=_review(),
        chapter_text="正文",
        chapter_card=_card("ch-0030", is_arc_finale=True),
        workspace_id=_WS,
        session_factory=None,
        router=None,
        client=client,
        model="mock",
    )
    client.complete.assert_not_called()


def test_true_finale_earned_no_change(session_factory) -> None:  # noqa: ANN001
    _seed_workspace(session_factory, total_chapters=30)
    client = _mock_client(verdict="earned")
    out = _apply_ending_tension_validator(
        review=_review(),
        chapter_text="高潮后安葬同伴道别回望,风停了。",
        chapter_card=_card("ch-0030", is_arc_finale=True),
        workspace_id=_WS,
        session_factory=session_factory,
        router=None,
        client=client,
        model="mock",
    )
    client.complete.assert_called_once()
    assert out.suggestions == []  # earned 不动 review


def test_true_finale_abrupt_appends_suggestion(
    session_factory,  # noqa: ANN001
) -> None:
    _seed_workspace(session_factory, total_chapters=30)
    client = _mock_client(verdict="abrupt")
    out = _apply_ending_tension_validator(
        review=_review(),
        chapter_text="搏斗后直接旁白三年后幸福生活。",
        chapter_card=_card("ch-0030", is_arc_finale=True),
        workspace_id=_WS,
        session_factory=session_factory,
        router=None,
        client=client,
        model="mock",
    )
    client.complete.assert_called_once()
    assert len(out.suggestions) == 1
    assert "终局张力" in out.suggestions[0]


def test_exception_swallowed_returns_original() -> None:
    def _broken():  # noqa: ANN202
        raise RuntimeError("db down")

    review = _review()
    out = _apply_ending_tension_validator(
        review=review,
        chapter_text="正文",
        chapter_card=_card("ch-0030", is_arc_finale=True),
        workspace_id=_WS,
        session_factory=_broken,
        router=None,
        client=_mock_client(),
        model="mock",
    )
    assert out.suggestions == []  # 异常 swallow,返原 review
