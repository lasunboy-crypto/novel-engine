"""B5 口头承诺追踪测试(v1.1 wave 6 session 4)。

覆盖 orchestrator 内 ``_check_plot_thread_payoff_safely`` 纯逻辑 / DB 层(mock LLM):

1. **仅终章触发**:非终章(current_seq < total_chapters)→ 不查不调 LLM
2. **total_chapters=0**:工作区未设总章数 → skip
3. **无候选**:全低重要性 / 全已收口 / 全有 payoff → 0 LLM call
4. **dangling WARN**:终章 + 重要未收口悬念,LLM 判 dangling → WARN log
5. **open_with_setup 护 C1**:LLM 判有交代开放 → **不**产 dangling WARN
6. **候选筛选**:resolved/abandoned 排除 / actual_payoff 排除 / importance 低排除
7. **_load_ending_summaries**:按 chapter_id 降序取最近 N 章 summary_text
8. **异常 swallow**:session 异常 → 不冒泡

thread_closure_validator agent 本身单测见 ``test_thread_closure_validator.py``。
"""

from __future__ import annotations

import json
from datetime import UTC, datetime
from unittest.mock import MagicMock

import pytest
import structlog
from sqlalchemy import create_engine
from sqlalchemy.orm import sessionmaker
from sqlalchemy.pool import StaticPool
from structlog.testing import capture_logs

from types import SimpleNamespace

from src.core.db import Base, ChapterSummaryORM, WorkspaceORM
from src.core.llm.base import LLMClient, LLMResponse, TokenUsage
from src.core.memory.store import create_plot_thread
from src.core.models.plot_thread import PlotThread
from src.core.orchestrator.chapter_orchestrator import (
    _check_plot_thread_payoff_safely,
    _load_ending_summaries,
)

_WS = "ws-demo"


@pytest.fixture(autouse=True)
def _ensure_debug_level() -> None:
    structlog.configure(wrapper_class=structlog.make_filtering_bound_logger(0))


@pytest.fixture
def session_factory():  # noqa: ANN201 — 测试 fixture
    engine = create_engine(
        "sqlite://",
        connect_args={"check_same_thread": False},
        poolclass=StaticPool,
    )
    Base.metadata.create_all(engine)
    maker = sessionmaker(bind=engine)
    return maker


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


def _seed_thread(
    maker,
    *,
    thread_id: str,
    title: str = "界外真局",
    status: str = "developing",
    importance: int = 5,
    actual_payoff: str | None = None,
    last_updated_chapter: str | None = "ch-0024",
) -> None:
    with maker() as s:
        create_plot_thread(
            s,
            PlotThread(
                thread_id=thread_id,
                workspace_id=_WS,
                title=title,
                status=status,  # type: ignore[arg-type]
                importance=importance,
                planted_chapter="ch-0020",
                planned_payoff=None,
                actual_payoff=actual_payoff,
                description="反复暗示世界之外另有操盘者",
                last_updated_chapter=last_updated_chapter,
                created_at=datetime.now(UTC),
            ),
        )
        s.commit()


def _seed_summary(maker, chapter_id: str, summary_text: str) -> None:
    with maker() as s:
        s.add(
            ChapterSummaryORM(
                chapter_id=chapter_id,
                workspace_id=_WS,
                summary_text=summary_text,
                extraction_trigger="reviewer_pass",
                model_used="qwen",
                created_at=datetime.now(UTC),
                updated_at=datetime.now(UTC),
            )
        )
        s.commit()


def _card(chapter_id: str) -> SimpleNamespace:
    """轻量桩:``_check_plot_thread_payoff_safely`` 只读 ``chapter_card.chapter_id``。"""
    return SimpleNamespace(chapter_id=chapter_id)


def _mock_client(verdict: str = "dangling") -> MagicMock:
    """thread_closure 批处理 mock:对每条候选返指定 verdict(按 index 对齐)。"""
    client = MagicMock(spec=LLMClient)

    def _complete(*args, **kwargs):  # noqa: ANN002, ANN003
        user_msg = kwargs["messages"][1].content
        import re

        nums = re.findall(r"^\s*(\d+)\.\s", user_msg, flags=re.MULTILINE)
        n = len(nums) or 1
        results = [
            {"index": i + 1, "verdict": verdict, "evidence": f"判定{i + 1}"}
            for i in range(n)
        ]
        return LLMResponse(
            content=json.dumps({"results": results}, ensure_ascii=False),
            model_used="qwen",
            usage=TokenUsage(input_tokens=400, output_tokens=60),
            finish_reason="stop",
        )

    client.complete.side_effect = _complete
    return client


# ---------------------------------------------------------------------------
# 仅终章触发
# ---------------------------------------------------------------------------


def test_non_final_chapter_skips(session_factory) -> None:  # noqa: ANN001
    """非终章(seq < total)→ 不调 LLM。"""
    _seed_workspace(session_factory, total_chapters=30)
    _seed_thread(session_factory, thread_id="t1")
    client = _mock_client()
    _check_plot_thread_payoff_safely(
        session_factory=session_factory,
        chapter_card=_card("ch-0010"),
        workspace_id=_WS,
        router=None,
        client=client,
        model="mock",
    )
    client.complete.assert_not_called()


def test_total_chapters_zero_skips(session_factory) -> None:  # noqa: ANN001
    """total_chapters=0(未设)→ skip。"""
    _seed_workspace(session_factory, total_chapters=0)
    _seed_thread(session_factory, thread_id="t1")
    client = _mock_client()
    _check_plot_thread_payoff_safely(
        session_factory=session_factory,
        chapter_card=_card("ch-0030"),
        workspace_id=_WS,
        router=None,
        client=client,
        model="mock",
    )
    client.complete.assert_not_called()


# ---------------------------------------------------------------------------
# 无候选
# ---------------------------------------------------------------------------


def test_no_candidates_low_importance(session_factory) -> None:  # noqa: ANN001
    """终章但全低重要性 → 无候选 → 0 LLM call。"""
    _seed_workspace(session_factory, total_chapters=30)
    _seed_thread(session_factory, thread_id="t1", importance=2)
    client = _mock_client()
    _check_plot_thread_payoff_safely(
        session_factory=session_factory,
        chapter_card=_card("ch-0030"),
        workspace_id=_WS,
        router=None,
        client=client,
        model="mock",
    )
    client.complete.assert_not_called()


def test_no_candidates_all_resolved(session_factory) -> None:  # noqa: ANN001
    """终章但全已收口(resolved)→ 无候选。"""
    _seed_workspace(session_factory, total_chapters=30)
    _seed_thread(session_factory, thread_id="t1", status="resolved")
    client = _mock_client()
    _check_plot_thread_payoff_safely(
        session_factory=session_factory,
        chapter_card=_card("ch-0030"),
        workspace_id=_WS,
        router=None,
        client=client,
        model="mock",
    )
    client.complete.assert_not_called()


def test_no_candidates_with_payoff(session_factory) -> None:  # noqa: ANN001
    """终章但有 actual_payoff(已兑现)→ 无候选。"""
    _seed_workspace(session_factory, total_chapters=30)
    _seed_thread(
        session_factory, thread_id="t1", actual_payoff="ch-0029"
    )
    client = _mock_client()
    _check_plot_thread_payoff_safely(
        session_factory=session_factory,
        chapter_card=_card("ch-0030"),
        workspace_id=_WS,
        router=None,
        client=client,
        model="mock",
    )
    client.complete.assert_not_called()


# ---------------------------------------------------------------------------
# dangling / open_with_setup
# ---------------------------------------------------------------------------


def test_final_chapter_dangling_warns(session_factory) -> None:  # noqa: ANN001
    """终章 + 重要未收口悬念,LLM 判 dangling → WARN log。"""
    _seed_workspace(session_factory, total_chapters=30)
    _seed_thread(session_factory, thread_id="t1", title="界外真局")
    _seed_summary(session_factory, "ch-0030", "主角归宗,一句话否认界外真局")
    client = _mock_client(verdict="dangling")
    with capture_logs() as logs:
        _check_plot_thread_payoff_safely(
            session_factory=session_factory,
            chapter_card=_card("ch-0030"),
            workspace_id=_WS,
            router=None,
            client=client,
            model="mock",
        )
    client.complete.assert_called_once()
    warn = [e for e in logs if e["event"] == "plot_thread_payoff_dangling_warn"]
    assert len(warn) == 1
    assert "界外真局" in warn[0]["dangling_titles"]


def test_final_chapter_open_with_setup_no_warn(
    session_factory,  # noqa: ANN001
) -> None:
    """LLM 判 open_with_setup(有交代开放)→ 不产 dangling WARN(护 §0 C1)。"""
    _seed_workspace(session_factory, total_chapters=30)
    _seed_thread(session_factory, thread_id="t1", title="妹妹能否救回")
    client = _mock_client(verdict="open_with_setup")
    with capture_logs() as logs:
        _check_plot_thread_payoff_safely(
            session_factory=session_factory,
            chapter_card=_card("ch-0030"),
            workspace_id=_WS,
            router=None,
            client=client,
            model="mock",
        )
    client.complete.assert_called_once()  # 有候选,确实调了 LLM
    warn = [e for e in logs if e["event"] == "plot_thread_payoff_dangling_warn"]
    assert warn == []  # 但开放结局合法,不报 dangling


# ---------------------------------------------------------------------------
# _load_ending_summaries
# ---------------------------------------------------------------------------


def test_load_ending_summaries_order_and_limit(
    session_factory,  # noqa: ANN001
) -> None:
    """按 chapter_id 降序取最近 N 章 summary_text(跳过空摘要)。"""
    _seed_workspace(session_factory, total_chapters=30)
    for i in range(1, 6):
        _seed_summary(session_factory, f"ch-{i:04d}", f"第{i}章摘要")
    with session_factory() as s:
        out = _load_ending_summaries(s, _WS, window=3)
    assert out == ["第5章摘要", "第4章摘要", "第3章摘要"]


# ---------------------------------------------------------------------------
# 异常 swallow
# ---------------------------------------------------------------------------


def test_exception_swallowed(session_factory) -> None:  # noqa: ANN001
    """session_factory 抛异常 → 全 swallow 不冒泡。"""

    def _broken():  # noqa: ANN202
        raise RuntimeError("db down")

    # 不应抛(WARN-only,全 swallow)。
    _check_plot_thread_payoff_safely(
        session_factory=_broken,
        chapter_card=_card("ch-0030"),
        workspace_id=_WS,
        router=None,
        client=_mock_client(),
        model="mock",
    )
