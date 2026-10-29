"""导出器模块(P6-B-04 起)。

按 plan §"P6-B-04" / §"P6-B-05" / §"P6-B-06" 字面拆分:

- ``markdown.py`` — md(全章拼接)+ chaptered_md(每章一文件 + index.md)的
  纯渲染函数(无 IO,接 ``ChapterPayload`` 列表 → 返字符串 / 字典)。
- ``epub.py``(P6-B-05) — EPUB 3.0 渲染(``ebooklib``);返 ``bytes``,由
  ``ExportService`` 写入 zip。
- ``docx.py``(P6-B-06) — .docx 渲染(``python-docx``);返 ``bytes``,由
  ``ExportService`` 写入 zip。
"""

from src.core.exporters.docx import render_docx
from src.core.exporters.epub import render_epub
from src.core.exporters.markdown import (
    ChapterPayload,
    build_chapter_filename,
    build_index_md,
    build_volume_dirname,
    render_chapter_only,
    render_markdown_full,
    render_per_chapter_files,
)

__all__ = [
    "ChapterPayload",
    "build_chapter_filename",
    "build_index_md",
    "build_volume_dirname",
    "render_chapter_only",
    "render_docx",
    "render_epub",
    "render_markdown_full",
    "render_per_chapter_files",
]
