"""Markdown 导出渲染器(P6-B-04)。

对齐 plan §"P6-B-04" 字面:

- ``md`` = 全章拼接成单 ``book.md``,带 ``# 卷 N: 标题`` / ``## 第 N 章: 标题``
  二级标题(对齐 FRONTEND_SPEC §4.14 字面"导出 markdown 单文件")。
- ``chaptered_md`` = 每章一 ``.md`` 文件 + 索引 ``index.md``,zip 内目录布局
  ``volume-{N}/chapter-{NNNN}.md`` × N + ``index.md``。

设计要点
========

1. **纯函数**:本模块不做 IO,接 ``ChapterPayload`` 列表 + ``options`` →
   返字符串 / 字典。zip 打包 + manifest.json 写盘走 ``ExportService``。
2. **空 final.md 兜底**:章节缺 final.md(尚未 approved)时正文段填占位
   "(章节未生成)";不抛错让导出整体失败。invalidated 章节由 service 层
   先过滤,本模块不感知。
3. **标题字符 sanitize**:文件名 ``chapter-{NNNN}.md`` 用 chapter 0-padded
   序号,不直接拼标题,避免特殊字符破坏 zip / 文件系统;标题进文件内容
   ``## 第 N 章: <title>`` 段(zip 内文件名稳定可机器解析)。
"""

from __future__ import annotations

from dataclasses import dataclass


@dataclass(frozen=True)
class ChapterPayload:
    """单章导出输入(由 ExportService 从工作区 chapter_cards / final.md
    收集后传入渲染器)。

    字段:

    - ``chapter_id``:章节 ID(``ch-NNNN`` 字面,4 位 0 填充)。
    - ``chapter_number``:卷内 1-based 章序号(对齐 ChapterCard.chapter_number)。
    - ``global_index``:全书 1-based 章序号(跨卷连续编号,对齐
      ``ch-{global_idx:04d}`` 派生)。
    - ``volume_id``:所属卷 ID。
    - ``volume_index``:全书 1-based 卷序号(volumes 排序后的位置)。
    - ``volume_title``:所属卷标题(VolumeORM.title)。
    - ``title``:本章标题(ChapterCard.title;缺卡时 fallback 到
      ``"第 N 章"`` 占位)。
    - ``content``:本章正文(``final.md`` 内容;缺 final.md 时为空串)。
    """

    chapter_id: str
    chapter_number: int
    global_index: int
    volume_id: str
    volume_index: int
    volume_title: str
    title: str
    content: str
    editing_note: str = ""

    @property
    def display_title(self) -> str:
        return f"{self.title}〔{self.editing_note}〕" if self.editing_note else self.title


def render_markdown_full(
    chapters: list[ChapterPayload],
    *,
    workspace_display_name: str | None = None,
) -> str:
    """全章拼接成单 markdown 字符串。

    布局:

    - 顶部 ``# <display_name>``(若提供)+ 空行
    - 按卷分组:``# 卷 N: <volume_title>`` + 空行
    - 卷内每章:``## 第 N 章: <title>`` + 空行 + ``content`` + 段间空行

    空章节(``content`` 为空)正文段输出 ``(章节未生成)`` 占位行。
    """
    lines: list[str] = []
    if workspace_display_name:
        lines.append(f"# {workspace_display_name}")
        lines.append("")

    current_volume_id: str | None = None
    for ch in chapters:
        if ch.volume_id != current_volume_id:
            lines.append(f"# 卷 {ch.volume_index}: {ch.volume_title}")
            lines.append("")
            current_volume_id = ch.volume_id
        lines.append(f"## 第 {ch.chapter_number} 章: {ch.display_title}")
        lines.append("")
        if ch.content.strip():
            lines.append(ch.content.rstrip())
        else:
            lines.append("(章节未生成)")
        lines.append("")

    return "\n".join(lines).rstrip() + "\n"


def render_chapter_only(chapter: ChapterPayload) -> str:
    """单章 markdown 字符串(chaptered_md 模式 per-file 内容)。

    布局:``## 第 N 章: <title>`` + 空行 + ``content``。文件级标题不含卷信息
    (卷信息体现在目录结构 ``volume-{N}/`` 中,对齐 plan §"P6-B-04" 字面
    ``volume-1/chapter-1.md``)。
    """
    parts = [f"## 第 {chapter.chapter_number} 章: {chapter.display_title}", ""]
    if chapter.content.strip():
        parts.append(chapter.content.rstrip())
    else:
        parts.append("(章节未生成)")
    return "\n".join(parts).rstrip() + "\n"


def build_index_md(
    chapters: list[ChapterPayload],
    *,
    workspace_display_name: str | None = None,
) -> str:
    """chaptered_md 模式的 ``index.md`` 索引文件。

    布局:

    - 顶部 ``# <display_name>``(若提供)+ 空行
    - 按卷分组:``## 卷 N: <volume_title>`` + 卷内每章 ``- [第 N 章: <title>](volume-{N}/chapter-{NNNN}.md)``
    """
    lines: list[str] = []
    if workspace_display_name:
        lines.append(f"# {workspace_display_name}")
        lines.append("")

    current_volume_id: str | None = None
    for ch in chapters:
        if ch.volume_id != current_volume_id:
            lines.append(f"## 卷 {ch.volume_index}: {ch.volume_title}")
            lines.append("")
            current_volume_id = ch.volume_id
        file_rel = (
            f"{build_volume_dirname(ch.volume_index)}/"
            f"{build_chapter_filename(ch.global_index)}"
        )
        lines.append(
            f"- [第 {ch.chapter_number} 章: {ch.display_title}]({file_rel})"
        )

    if not chapters:
        lines.append("(无章节)")

    return "\n".join(lines).rstrip() + "\n"


def render_per_chapter_files(
    chapters: list[ChapterPayload],
) -> dict[str, str]:
    """chaptered_md 模式:返 ``{zip 内相对路径: 文件内容}`` 字典。

    路径布局:``volume-{volume_index}/chapter-{global_index:04d}.md``。
    """
    files: dict[str, str] = {}
    for ch in chapters:
        rel = (
            f"{build_volume_dirname(ch.volume_index)}/"
            f"{build_chapter_filename(ch.global_index)}"
        )
        files[rel] = render_chapter_only(ch)
    return files


def build_volume_dirname(volume_index: int) -> str:
    """卷目录名 ``volume-{N}`` (1-based,对齐 plan §"P6-B-04" 字面)。"""
    return f"volume-{volume_index}"


def build_chapter_filename(global_index: int) -> str:
    """章节文件名 ``chapter-{NNNN}.md`` (4 位 0 填充,对齐 ``ch-NNNN`` 派生)。"""
    return f"chapter-{global_index:04d}.md"


__all__ = [
    "ChapterPayload",
    "build_chapter_filename",
    "build_index_md",
    "build_volume_dirname",
    "render_chapter_only",
    "render_markdown_full",
    "render_per_chapter_files",
]
