"""B2 双时空场景锚点跨章比对 + 落库测试(v1.1 wave 6 session 3)。

覆盖 orchestrator 内 B2 纯逻辑 / DB 层(不依赖真 LLM):

1. ``_locations_match``:地点字面"显然同地点"粗筛(相等 / 互为子串)
2. ``_persist_scene_anchor_to_chapter``:本章 scene_anchor 落 ChapterORM 列
3. ``_check_scene_anchor_continuity_safely``:跨章查最近入梦点 + 代码粗筛 +
   LLM 复核 → teleport 才 WARN(护留白:有位移交代 / 同地点 / 无历史入梦点 →
   不报、不调 LLM)

scene_anchor_validator agent 本身单测见 ``test_scene_anchor_validator.py``;
extract 抽 scene_anchor 字段见 ``test_memory_extractor.py``。
"""

from __future__ import annotations

import json
from datetime import UTC, datetime
from unittest.mock import MagicMock

import pytest
import structlog
from sqlalchemy import create_engine
from sqlalchemy.orm import Session, sessionmaker
from sqlalchemy.pool import StaticPool
from structlog.testing import capture_logs

from src.core.agents.memory_extractor import ExtractedSceneAnchor
from src.core.db import Base, ChapterORM, WorkspaceORM
from src.core.llm.base import LLMClient, LLMResponse, TokenUsage
from src.core.orchestrator.chapter_orchestrator import (
    _check_scene_anchor_continuity_safely,
    _locations_match,
    _persist_scene_anchor_to_chapter,
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
    with Session(engine) as s:
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
                total_chapters=0,
                completed_chapters=0,
            )
        )
        s.commit()
    return maker


def _seed_chapter(
    maker, chapter_id: str, scene_anchor: dict | None = None
) -> None:
    with maker() as s:
        s.add(
            ChapterORM(
                chapter_id=chapter_id,
                workspace_id=_WS,
                current_version=1,
                status="approved",
                rewrite_count=0,
                revise_count=0,
                created_at=datetime.now(UTC),
                scene_anchor=scene_anchor if scene_anchor is not None else {},
            )
        )
        s.commit()


def _mock_client(verdict: str = "teleport") -> MagicMock:
    client = MagicMock(spec=LLMClient)
    client.complete.return_value = LLMResponse(
        content=json.dumps({"verdict": verdict, "evidence": "判定依据"}),
        model_used="qwen",
        usage=TokenUsage(input_tokens=300, output_tokens=20),
        finish_reason="stop",
    )
    return client


# ---------------------------------------------------------------------------
# 1. _locations_match
# ---------------------------------------------------------------------------


class TestLocationsMatch:
    def test_exact_match(self) -> None:
        assert _locations_match("诊所催眠室", "诊所催眠室") is True

    def test_substring_match(self) -> None:
        assert _locations_match("诊所", "诊所二楼催眠室") is True
        assert _locations_match("诊所二楼催眠室", "诊所") is True

    def test_whitespace_normalized(self) -> None:
        assert _locations_match("城郊 工厂", "城郊工厂") is True

    def test_distinct_no_match(self) -> None:
        assert _locations_match("病房病床", "城郊工厂越野车") is False

    def test_empty_no_match(self) -> None:
        assert _locations_match("", "诊所") is False
        assert _locations_match("诊所", "  ") is False


# ---------------------------------------------------------------------------
# 2. _persist_scene_anchor_to_chapter
# ---------------------------------------------------------------------------


class TestPersist:
    def test_persists_non_empty_anchor(self, session_factory) -> None:
        _seed_chapter(session_factory, "ch-0013")
        anchor = ExtractedSceneAnchor(
            enter_dream_location="病房病床", wake_location=""
        )
        with session_factory() as s:
            _persist_scene_anchor_to_chapter(s, "ch-0013", anchor)
            s.commit()
        with session_factory() as s:
            row = s.get(ChapterORM, "ch-0013")
            assert row.scene_anchor["enter_dream_location"] == "病房病床"

    def test_empty_anchor_not_written(self, session_factory) -> None:
        _seed_chapter(session_factory, "ch-0013")
        with session_factory() as s:
            _persist_scene_anchor_to_chapter(s, "ch-0013", ExtractedSceneAnchor())
            s.commit()
        with session_factory() as s:
            assert s.get(ChapterORM, "ch-0013").scene_anchor == {}

    def test_none_anchor_noop(self, session_factory) -> None:
        _seed_chapter(session_factory, "ch-0013")
        with session_factory() as s:
            _persist_scene_anchor_to_chapter(s, "ch-0013", None)
            s.commit()
        with session_factory() as s:
            assert s.get(ChapterORM, "ch-0013").scene_anchor == {}

    def test_missing_chapter_row_noop(self, session_factory) -> None:
        # chapter 行不存在 → 跳过不抛
        with session_factory() as s:
            _persist_scene_anchor_to_chapter(
                s, "ch-9999", ExtractedSceneAnchor(enter_dream_location="X")
            )
            s.commit()


# ---------------------------------------------------------------------------
# 3. _check_scene_anchor_continuity_safely
# ---------------------------------------------------------------------------


class TestCheckContinuity:
    @staticmethod
    def _card(chapter_id: str) -> MagicMock:
        card = MagicMock()
        card.chapter_id = chapter_id
        return card

    def test_teleport_emits_warn(self, session_factory) -> None:
        # 入梦点 ch-0013=病房;本章 ch-0016 醒来=工厂,无位移交代 → LLM 判 teleport
        _seed_chapter(
            session_factory,
            "ch-0013",
            {"enter_dream_location": "病房病床", "wake_location": "", "has_transition_explanation": False},
        )
        _seed_chapter(session_factory, "ch-0016")
        client = _mock_client("teleport")
        anchor = ExtractedSceneAnchor(
            enter_dream_location="", wake_location="城郊工厂越野车", has_transition_explanation=False
        )
        with capture_logs() as logs:
            _check_scene_anchor_continuity_safely(
                session_factory=session_factory,
                chapter_card=self._card("ch-0016"),
                workspace_id=_WS,
                chapter_text="沈舟在越野车里醒来" + "字" * 100,
                scene_anchor=anchor,
                router=None,
                client=client,
                model="qwen",
            )
        assert any(
            e.get("event") == "scene_anchor_teleport_warn" for e in logs
        )
        assert client.complete.call_count == 1

    def test_natural_no_warn(self, session_factory) -> None:
        _seed_chapter(
            session_factory,
            "ch-0013",
            {"enter_dream_location": "病房病床", "wake_location": "", "has_transition_explanation": False},
        )
        _seed_chapter(session_factory, "ch-0016")
        client = _mock_client("natural")
        anchor = ExtractedSceneAnchor(
            enter_dream_location="", wake_location="城郊工厂越野车"
        )
        with capture_logs() as logs:
            _check_scene_anchor_continuity_safely(
                session_factory=session_factory,
                chapter_card=self._card("ch-0016"),
                workspace_id=_WS,
                chapter_text="正文",
                scene_anchor=anchor,
                router=None,
                client=client,
                model="qwen",
            )
        assert not any(
            e.get("event") == "scene_anchor_teleport_warn" for e in logs
        )
        assert client.complete.call_count == 1  # 仍调了 LLM 复核,只是判 natural

    def test_has_transition_skips_llm(self, session_factory) -> None:
        _seed_chapter(
            session_factory,
            "ch-0013",
            {"enter_dream_location": "病房病床", "wake_location": "", "has_transition_explanation": False},
        )
        _seed_chapter(session_factory, "ch-0016")
        client = _mock_client("teleport")
        anchor = ExtractedSceneAnchor(
            enter_dream_location="", wake_location="工厂", has_transition_explanation=True
        )
        _check_scene_anchor_continuity_safely(
            session_factory=session_factory,
            chapter_card=self._card("ch-0016"),
            workspace_id=_WS,
            chapter_text="正文",
            scene_anchor=anchor,
            router=None,
            client=client,
            model="qwen",
        )
        assert client.complete.call_count == 0  # 有位移交代 → 不调 LLM

    def test_same_location_skips_llm(self, session_factory) -> None:
        _seed_chapter(
            session_factory,
            "ch-0013",
            {"enter_dream_location": "诊所", "wake_location": "", "has_transition_explanation": False},
        )
        _seed_chapter(session_factory, "ch-0016")
        client = _mock_client("teleport")
        anchor = ExtractedSceneAnchor(
            enter_dream_location="", wake_location="诊所二楼催眠室"
        )
        _check_scene_anchor_continuity_safely(
            session_factory=session_factory,
            chapter_card=self._card("ch-0016"),
            workspace_id=_WS,
            chapter_text="正文",
            scene_anchor=anchor,
            router=None,
            client=client,
            model="qwen",
        )
        assert client.complete.call_count == 0  # 诊所 ⊂ 诊所二楼催眠室 → 粗筛放过

    def test_no_prior_enter_location_skips(self, session_factory) -> None:
        # 仅前序章无入梦点 → 无可比对
        _seed_chapter(session_factory, "ch-0013")  # 空 scene_anchor
        _seed_chapter(session_factory, "ch-0016")
        client = _mock_client("teleport")
        anchor = ExtractedSceneAnchor(enter_dream_location="", wake_location="工厂")
        _check_scene_anchor_continuity_safely(
            session_factory=session_factory,
            chapter_card=self._card("ch-0016"),
            workspace_id=_WS,
            chapter_text="正文",
            scene_anchor=anchor,
            router=None,
            client=client,
            model="qwen",
        )
        assert client.complete.call_count == 0

    def test_no_wake_location_skips(self, session_factory) -> None:
        _seed_chapter(
            session_factory,
            "ch-0013",
            {"enter_dream_location": "病房", "wake_location": "", "has_transition_explanation": False},
        )
        _seed_chapter(session_factory, "ch-0016")
        client = _mock_client("teleport")
        # 本章只入梦(无醒来)→ 无需比对
        anchor = ExtractedSceneAnchor(enter_dream_location="另一处", wake_location="")
        _check_scene_anchor_continuity_safely(
            session_factory=session_factory,
            chapter_card=self._card("ch-0016"),
            workspace_id=_WS,
            chapter_text="正文",
            scene_anchor=anchor,
            router=None,
            client=client,
            model="qwen",
        )
        assert client.complete.call_count == 0

    def test_picks_most_recent_enter_location(self, session_factory) -> None:
        # ch-0011 入梦=旧地点;ch-0014 入梦=新地点;本章 ch-0016 醒来应对照 ch-0014
        _seed_chapter(
            session_factory,
            "ch-0011",
            {"enter_dream_location": "旧地点A", "wake_location": "", "has_transition_explanation": False},
        )
        _seed_chapter(
            session_factory,
            "ch-0014",
            {"enter_dream_location": "新地点B", "wake_location": "", "has_transition_explanation": False},
        )
        _seed_chapter(session_factory, "ch-0016")
        client = _mock_client("teleport")
        anchor = ExtractedSceneAnchor(enter_dream_location="", wake_location="醒来点C")
        _check_scene_anchor_continuity_safely(
            session_factory=session_factory,
            chapter_card=self._card("ch-0016"),
            workspace_id=_WS,
            chapter_text="正文",
            scene_anchor=anchor,
            router=None,
            client=client,
            model="qwen",
        )
        # 复核被调用,user message 里入梦点应是最近的"新地点B"(ch-0014),非 ch-0011
        user_msg = next(
            m for m in client.complete.call_args.kwargs["messages"] if m.role == "user"
        )
        assert "新地点B" in user_msg.content
        assert "旧地点A" not in user_msg.content
        assert "ch-0014" in user_msg.content
