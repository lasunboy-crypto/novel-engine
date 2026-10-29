"""G2(v1.1 wave 4 session 2):Writer 伏笔 / 隐喻回响段单测。

覆盖 ``_build_callback_section`` + ``_read_planted_excerpt``:
SQL lookup PlotThread by ID(绕 embedding)+ planted_chapter 原文片段 + 各降级路径。
"""

from __future__ import annotations

from datetime import datetime, timezone
from pathlib import Path

import pytest
from sqlalchemy import create_engine
from sqlalchemy.orm import Session, sessionmaker

from src.core.agents.writer import (
    _CALLBACK_EXCERPT_MAX_CHARS,
    _build_callback_section,
    _build_related_plot_thread_section,
    _read_planted_excerpt,
)
from src.core.db.models import Base, PlotThreadORM
from src.core.models.chapter_card import ChapterCard, ContinuityContext
from src.core.workspace.workspace_paths import WorkspacePaths


@pytest.fixture
def _session() -> Session:
    engine = create_engine("sqlite:///:memory:")
    Base.metadata.create_all(engine)
    return sessionmaker(bind=engine)()


def _make_card(
    *,
    workspace_id: str = "ws-1",
    callback_to: list[str] | None = None,
    related_plot_threads: list[str] | None = None,
    must_achieve: list[str] | None = None,
) -> ChapterCard:
    return ChapterCard(
        chapter_id="ch-0028",
        workspace_id=workspace_id,
        volume_id="vol-1",
        chapter_number=28,
        title="归乡",
        tension_level=6,
        must_achieve=must_achieve or ["主角归乡"],
        must_unresolved=["旧友下落"],
        forbidden=["现代用语"],
        pov_character="char-hero",
        setting="故乡老宅",
        continuity_context=ContinuityContext(
            prev_chapter_tail="上一章结尾",
            unfinished_actions=[],
            time_gap="数年后",
            scene_continuation="time_jump",
            pov_change=False,
            pov_from=None,
            pov_to="char-hero",
        ),
        next_chapter_seed="种子",
        arc_phase="falling",
        is_arc_finale=False,
        related_plot_threads=related_plot_threads or [],
        callback_to=callback_to or [],
        related_characters=["char-hero"],
    )


def _add_thread(
    session: Session,
    *,
    thread_id: str,
    workspace_id: str = "ws-1",
    title: str = "暴雨象征",
    thread_type: str = "metaphor",
    planted_chapter: str | None = "ch-0003",
    description: str = "第 3 章窗外暴雨象征主角内心失落。",
    origin: str = "outline",
    expiry_policy: str = "never",
    source_evidence: str | None = None,
) -> None:
    session.add(
        PlotThreadORM(
            thread_id=thread_id,
            workspace_id=workspace_id,
            title=title,
            thread_type=thread_type,
            status="planted",
            importance=4,
            planted_chapter=planted_chapter,
            planned_payoff=None,
            actual_payoff=None,
            description=description,
            origin=origin,
            expiry_policy=expiry_policy,
            source_evidence=source_evidence,
            related_characters=[],
            created_at=datetime.now(timezone.utc),
            last_updated_chapter=None,
        )
    )
    session.flush()


def test_related_plot_thread_section_keeps_outline_plan_separate(
    _session: Session,
) -> None:
    _add_thread(_session, thread_id="thread-main", title="大纲主伏笔")
    section = _build_related_plot_thread_section(
        db_session=_session,
        chapter_card=_make_card(related_plot_threads=["thread-main"]),
    )
    assert "大纲/人工主伏笔" in section
    assert "临时伏笔规则不改变" in section
    assert "临时伏笔收口:thread-main" not in section


def test_related_plot_thread_section_marks_selected_emergent_payoff(
    _session: Session,
) -> None:
    _add_thread(
        _session,
        thread_id="temp-letter",
        title="临时密信",
        origin="emergent",
        expiry_policy="volume_end",
        source_evidence="他留下密信：若三日未归，就把真相告诉所有人。",
    )
    marker = "[临时伏笔收口:temp-letter]林衍拆开密信并向众人公布真相"
    section = _build_related_plot_thread_section(
        db_session=_session,
        chapter_card=_make_card(
            related_plot_threads=["temp-letter"],
            must_achieve=[marker],
        ),
    )
    assert "临时伏笔:临时密信" in section
    assert "章节卡已用 [临时伏笔收口:temp-letter] 明确选择收口" in section
    assert "若三日未归" in section


def _make_paths(tmp_path: Path) -> WorkspacePaths:
    return WorkspacePaths("ws-1", workspaces_root=tmp_path)


def _write_final(paths: WorkspacePaths, chapter_id: str, content: str) -> None:
    chapter_dir = paths.chapter_dir(chapter_id)
    chapter_dir.mkdir(parents=True, exist_ok=True)
    (chapter_dir / "final.md").write_text(content, encoding="utf-8")


# ---- _build_callback_section ---------------------------------------------- #


def test_callback_section_empty_when_no_callback_to(_session: Session) -> None:
    assert _build_callback_section(
        db_session=_session, workspace_paths=None, chapter_card=_make_card()
    ) == ""


def test_callback_section_empty_when_no_session(tmp_path: Path) -> None:
    assert _build_callback_section(
        db_session=None,
        workspace_paths=_make_paths(tmp_path),
        chapter_card=_make_card(callback_to=["thread-rain"]),
    ) == ""


def test_callback_section_renders_meta_and_excerpt(
    _session: Session, tmp_path: Path
) -> None:
    _add_thread(_session, thread_id="thread-rain")
    paths = _make_paths(tmp_path)
    _write_final(paths, "ch-0003", "窗外暴雨倾盆,他独坐窗前,心绪如雨。")
    section = _build_callback_section(
        db_session=_session,
        workspace_paths=paths,
        chapter_card=_make_card(callback_to=["thread-rain"]),
    )
    assert "伏笔 / 隐喻回响" in section
    assert "暴雨象征" in section
    assert "类型:metaphor" in section
    assert "第 3 章窗外暴雨象征主角内心失落" in section
    assert "ch-0003" in section
    assert "窗外暴雨倾盆" in section


def test_callback_section_graceful_without_excerpt(
    _session: Session, tmp_path: Path
) -> None:
    _add_thread(_session, thread_id="thread-rain")
    section = _build_callback_section(
        db_session=_session,
        workspace_paths=_make_paths(tmp_path),  # 不写 final.md
        chapter_card=_make_card(callback_to=["thread-rain"]),
    )
    assert "暴雨象征" in section
    assert "原文片段" not in section


def test_callback_section_skips_cross_workspace(
    _session: Session, tmp_path: Path
) -> None:
    _add_thread(_session, thread_id="thread-other", workspace_id="ws-OTHER")
    section = _build_callback_section(
        db_session=_session,
        workspace_paths=_make_paths(tmp_path),
        chapter_card=_make_card(workspace_id="ws-1", callback_to=["thread-other"]),
    )
    assert section == ""


def test_callback_section_skips_unknown_thread(
    _session: Session, tmp_path: Path
) -> None:
    section = _build_callback_section(
        db_session=_session,
        workspace_paths=_make_paths(tmp_path),
        chapter_card=_make_card(callback_to=["thread-does-not-exist"]),
    )
    assert section == ""


def test_callback_section_multiple_threads(
    _session: Session, tmp_path: Path
) -> None:
    _add_thread(_session, thread_id="thread-rain", title="暴雨象征")
    _add_thread(
        _session,
        thread_id="thread-sword",
        title="锈剑之谜",
        thread_type="foreshadowing",
        planted_chapter="ch-0001",
        description="第 1 章锈剑剑灵微弱共鸣。",
    )
    paths = _make_paths(tmp_path)
    _write_final(paths, "ch-0003", "暴雨原文。")
    _write_final(paths, "ch-0001", "锈剑原文。")
    section = _build_callback_section(
        db_session=_session,
        workspace_paths=paths,
        chapter_card=_make_card(callback_to=["thread-rain", "thread-sword"]),
    )
    assert "暴雨象征" in section
    assert "锈剑之谜" in section
    assert "类型:foreshadowing" in section


# ---- _read_planted_excerpt ------------------------------------------------ #


def test_read_excerpt_none_when_no_paths() -> None:
    assert _read_planted_excerpt(
        workspace_paths=None, planted_chapter="ch-0003"
    ) == ""


def test_read_excerpt_none_when_no_planted_chapter(tmp_path: Path) -> None:
    assert _read_planted_excerpt(
        workspace_paths=_make_paths(tmp_path), planted_chapter=None
    ) == ""


def test_read_excerpt_truncates_long_content(tmp_path: Path) -> None:
    paths = _make_paths(tmp_path)
    _write_final(paths, "ch-0003", "雨" * (_CALLBACK_EXCERPT_MAX_CHARS + 200))
    excerpt = _read_planted_excerpt(
        workspace_paths=paths, planted_chapter="ch-0003"
    )
    assert excerpt.endswith("……")
    # 中文省略号 "……" 是 2 个 U+2026 码点
    assert len(excerpt) == _CALLBACK_EXCERPT_MAX_CHARS + 2


def test_read_excerpt_full_when_short(tmp_path: Path) -> None:
    paths = _make_paths(tmp_path)
    _write_final(paths, "ch-0003", "短片段。")
    assert _read_planted_excerpt(
        workspace_paths=paths, planted_chapter="ch-0003"
    ) == "短片段。"
