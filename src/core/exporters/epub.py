"""EPUB 3.0 导出渲染器(P6-B-05)。

对齐 plan §"P6-B-05" 字面 + plan-kickoff 决策 6=A(``uv add ebooklib`` 装入
EPUB 3.0 标准实现库):

- ``EpubBook`` 实例化 + 每章 ``EpubHtml`` + spine / TOC 按卷分组组装
- cover 占位:``data/assets/cover.png`` 存在 → ``set_cover`` 物理图片;否则
  fallback 纯文字封面 XHTML(显示 ``display_name`` + 引擎水印)
- 验收:``ebooklib.read_epub()`` 反序列化通过即过(自决项 7,不强制装
  ``epubcheck`` CLI)

设计要点
========

1. **纯函数 + bytes 返回**:本模块不做磁盘 IO,接 ``ChapterPayload`` 列表 +
   options → 返 EPUB 二进制 ``bytes``(由 ``ExportService`` 写入 zip)。``ebooklib.epub.write_epub``
   接受 ``BytesIO`` 文件对象,无需 tempfile 中转。
2. **XHTML 转义**:章节正文(markdown 源)转 XHTML 时只走最小转义 — 按空行
   切段、``html.escape`` 处理 ``<>&`` 实体;**不引入 markdown 解析器**(避免
   第二个新依赖触达 CLAUDE.md §硬约束 1)。
3. **cover 处置(自决项 1)**:``cover_image_bytes`` 非空时 ``set_cover``;
   否则 fallback ``EpubHtml`` 文字封面(spine 首位)。物理读 ``cover.png``
   走 ``ExportService`` 层(本模块只接 bytes)。
4. **TOC 按卷分组**:``book.toc = [(Section('卷 N: 标题'), [ch1, ch2, ...]), ...]``;
   spine 走 ``['nav', cover?, ch1, ch2, ...]`` 全平铺(nav / cover 由 ebooklib
   生成)。
5. **空 final.md 兜底**:章节缺正文(``content`` 为空)时段落输出占位
   "(章节未生成)";不抛错让导出整体失败(对齐 markdown 渲染器同款约定)。
"""

from __future__ import annotations

import html
from io import BytesIO

from ebooklib import epub

from src.core.exporters.markdown import ChapterPayload


def _content_to_xhtml_paragraphs(content: str) -> str:
    """章节正文 markdown 文本 → XHTML 段落串。

    分段规则:优先按双换行 ``\\n\\n`` 切段(markdown 段落约定);双换行无
    匹配时 fallback 单换行 ``\\n``。每非空段包裹 ``<p>``;HTML 实体转义
    走 ``html.escape``;**不解析** markdown 内联语法(粗体 / 斜体 / 链接等)。
    """
    if not content.strip():
        return "<p>(章节未生成)</p>"
    paragraphs = [p.strip() for p in content.split("\n\n") if p.strip()]
    if not paragraphs:
        paragraphs = [p.strip() for p in content.split("\n") if p.strip()]
    return "\n".join(f"<p>{html.escape(p)}</p>" for p in paragraphs)


def _build_chapter_xhtml(chapter: ChapterPayload) -> str:
    """单章 XHTML body 内容(标题 + 段落)。

    布局:``<h2>第 N 章: 标题</h2>`` + ``<p>`` 段落 × M。ebooklib 自动包裹
    完整 XHTML 文档骨架(``<html xmlns="..."><body>...</body></html>``)。
    """
    title_html = html.escape(f"第 {chapter.chapter_number} 章: {chapter.display_title}")
    body_html = _content_to_xhtml_paragraphs(chapter.content)
    return f"<h2>{title_html}</h2>\n{body_html}"


def _build_cover_xhtml(
    workspace_display_name: str,
    *,
    engine_version: str | None,
) -> str:
    """fallback 纯文字封面 XHTML(无 ``cover.png`` 时用)。

    居中显示 ``display_name`` + 引擎版本水印。无外部 CSS — 只用内联 style
    避免 EPUB CSS 资源依赖。
    """
    title = html.escape(workspace_display_name or "(未命名作品)")
    watermark = (
        f'<p style="text-align:center;font-size:0.8em;color:#888;'
        f'margin-top:4em">engine_version: {html.escape(engine_version)}</p>'
        if engine_version
        else ""
    )
    return (
        f'<div style="text-align:center;padding-top:30%">'
        f"<h1>{title}</h1>"
        f"{watermark}"
        f"</div>"
    )


def render_epub(
    chapters: list[ChapterPayload],
    *,
    workspace_id: str,
    workspace_display_name: str | None = None,
    author: str | None = None,
    language: str = "zh-CN",
    cover_image_bytes: bytes | None = None,
    cover_image_name: str = "cover.png",
    engine_version: str | None = None,
) -> bytes:
    """渲染 EPUB 3.0 文件 → 返二进制 ``bytes``。

    参数:

    - ``chapters``:已过滤(invalidated 跳过)的章节列表,卷内顺序对齐
      ``global_index``。
    - ``workspace_id``:作 EPUB ``DC.identifier``(对齐 EPUB 3.0 必填元数据)。
    - ``workspace_display_name``:作 EPUB ``DC.title``;空时 fallback
      ``workspace_id``。
    - ``author``:作 EPUB ``DC.creator``;空时 fallback ``workspace_display_name``。
    - ``language``:作 EPUB ``DC.language``,默认中文 ``zh-CN``。
    - ``cover_image_bytes``:``cover.png`` 物理字节;空时走 fallback 文字封面。
    - ``cover_image_name``:cover 资源文件名,默认 ``cover.png``。
    - ``engine_version``:fallback 封面水印用;``set_cover`` 物理封面时不显示。

    返:EPUB 文件完整二进制内容(``zipfile`` 压缩后的 .epub 字节)。
    """
    book = epub.EpubBook()
    book.set_identifier(workspace_id)
    book.set_title(workspace_display_name or workspace_id)
    book.set_language(language)
    book.add_author(author or workspace_display_name or workspace_id)

    # cover:物理图 vs fallback 文字封面
    cover_item: epub.EpubHtml | None = None
    if cover_image_bytes:
        book.set_cover(cover_image_name, cover_image_bytes)
    else:
        cover_item = epub.EpubHtml(
            title="封面",
            file_name="cover.xhtml",
            lang=language,
        )
        cover_item.content = _build_cover_xhtml(
            workspace_display_name or workspace_id,
            engine_version=engine_version,
        )
        book.add_item(cover_item)

    # 章节 + 按卷分组准备 TOC
    epub_chapters: list[epub.EpubHtml] = []
    grouped: dict[int, tuple[str, list[epub.EpubHtml]]] = {}
    for ch in chapters:
        item = epub.EpubHtml(
            title=f"第 {ch.chapter_number} 章: {ch.display_title}",
            file_name=f"chapter_{ch.global_index:04d}.xhtml",
            lang=language,
        )
        item.content = _build_chapter_xhtml(ch)
        book.add_item(item)
        epub_chapters.append(item)
        if ch.volume_index not in grouped:
            grouped[ch.volume_index] = (ch.volume_title, [])
        grouped[ch.volume_index][1].append(item)

    # TOC 按卷分组,卷序升序
    toc: list[tuple[epub.Section, list[epub.EpubHtml]]] = []
    for v_idx in sorted(grouped):
        v_title, items = grouped[v_idx]
        toc.append((epub.Section(f"卷 {v_idx}: {v_title}"), items))
    book.toc = toc

    # spine:['nav', cover?, ch1, ch2, ...];set_cover 模式下封面由 ebooklib 自动加
    spine: list[object] = ["nav"]
    if cover_item is not None:
        spine.append(cover_item)
    spine.extend(epub_chapters)
    book.spine = spine

    # EPUB 3.0 标准必需:nav.xhtml + toc.ncx
    book.add_item(epub.EpubNcx())
    book.add_item(epub.EpubNav())

    buf = BytesIO()
    epub.write_epub(buf, book)
    return buf.getvalue()


__all__ = ["render_epub"]
