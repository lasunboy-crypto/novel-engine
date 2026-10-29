"""I1(v1.1 wave 4 session 3):Writer 写作风格指南段单测。

覆盖 ``_build_style_guide_section`` + ``_build_user_message`` 注入:
读工作区 ``prompts/style_guide.md`` 注入 + 各降级路径(未选模板 / 缺 paths /
空文件 / 超长截断)。
"""

from __future__ import annotations

from pathlib import Path

from src.core.agents.writer import (
    _STYLE_GUIDE_FILENAME,
    _STYLE_GUIDE_MAX_CHARS,
    _build_style_guide_section,
)
from src.core.workspace.workspace_paths import WorkspacePaths


def _paths(tmp_path: Path) -> WorkspacePaths:
    p = WorkspacePaths("ws-1", workspaces_root=tmp_path)
    p.prompts_dir.mkdir(parents=True, exist_ok=True)
    return p


def _write_guide(paths: WorkspacePaths, text: str) -> None:
    (paths.prompts_dir / _STYLE_GUIDE_FILENAME).write_text(text, encoding="utf-8")


def test_none_paths_returns_empty() -> None:
    assert _build_style_guide_section(workspace_paths=None) == ""


def test_missing_file_returns_empty(tmp_path: Path) -> None:
    paths = _paths(tmp_path)
    assert _build_style_guide_section(workspace_paths=paths) == ""


def test_empty_file_returns_empty(tmp_path: Path) -> None:
    paths = _paths(tmp_path)
    _write_guide(paths, "   \n  ")
    assert _build_style_guide_section(workspace_paths=paths) == ""


def test_present_guide_injected_with_header(tmp_path: Path) -> None:
    paths = _paths(tmp_path)
    _write_guide(paths, "# 写作风格指南:搞笑爽文\n\n轻松解压读着爽。")
    section = _build_style_guide_section(workspace_paths=paths)
    assert "## 写作风格指南" in section
    assert "全书一致风格锚点" in section
    assert "轻松解压读着爽" in section


def test_overlong_guide_truncated(tmp_path: Path) -> None:
    paths = _paths(tmp_path)
    body = "甲" * (_STYLE_GUIDE_MAX_CHARS + 500)
    _write_guide(paths, body)
    section = _build_style_guide_section(workspace_paths=paths)
    assert section.endswith("……")
    # 正文被截断到上限:原文 MAX+500 个"甲"只剩 MAX 个(其余被截 + 省略号替代)。
    assert section.count("甲") == _STYLE_GUIDE_MAX_CHARS


def test_user_message_includes_style_guide_section(tmp_path: Path) -> None:
    """风格指南段经 _build_user_message 注入 prompt(非空时)。"""
    from src.core.agents.writer import _build_user_message
    from src.core.models.chapter_card import ChapterCard, ContinuityContext

    card = ChapterCard(
        chapter_id="ch-0001",
        workspace_id="ws-1",
        volume_id="vol-1",
        chapter_number=1,
        title="开篇",
        tension_level=5,
        must_achieve=["开场"],
        must_unresolved=["悬念"],
        forbidden=["现代用语"],
        pov_character="char-hero",
        setting="山中",
        continuity_context=ContinuityContext(
            prev_chapter_tail="",
            unfinished_actions=[],
            time_gap="无",
            scene_continuation="same",
            pov_change=False,
            pov_from=None,
            pov_to="char-hero",
        ),
        next_chapter_seed="下一章引子",
        arc_phase="rising",
        is_arc_finale=False,
        related_plot_threads=[],
        callback_to=[],
        related_characters=["char-hero"],
    )
    msg = _build_user_message(
        chapter_card=card,
        memory_section="",
        retrieved_section="",
        style_guide_section="## 写作风格指南(WRITING STYLE GUIDE)\n\n体裁基调测试串",
    )
    assert "体裁基调测试串" in msg
