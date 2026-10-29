"""DOCX 导出渲染器(P6-B-06)。

对齐 plan §"P6-B-06" 字面 + plan-kickoff 决策 6=A(``uv add python-docx``
装入微软 Word .docx 标准实现库):

- ``Document`` 实例化 + ``add_heading`` per 卷(Heading 1)/ 章(Heading 2)
- ``add_paragraph`` per 段(段间空行)+ TOC 占位 ``add_paragraph
  "[请在 Word 中按 F9 更新目录]"``(自决项 8 拍板:python-docx 不原生支持
  TOC field)
- 验收:``Document(book.docx)`` 反序列化读取 paragraphs / heading 计数即过

设计要点
========

1. **纯函数 + bytes 返回**:本模块不做磁盘 IO,接 ``ChapterPayload`` 列表 +
   options → 返 .docx 二进制 ``bytes``(由 ``ExportService`` 写入 zip)。
   ``Document.save`` 接受文件对象,走 ``BytesIO`` 缓冲对齐 P6-B-05 epub
   模式。
2. **段落分割**:章节正文(markdown 源)按双换行 ``\\n\\n`` 切段,fallback
   单换行 ``\\n``;**不解析** markdown 内联语法(粗体 / 斜体 / 链接等),
   纯文本透传(对齐 P6-B-05 epub 同款约定)。
3. **TOC 占位段落**(自决项 8):python-docx 1.x 不原生支持 TOC field
   插入,运行时需用户在 Word 中按 F9 更新;本任务在文档开头标题下放占位
   段落 ``[请在 Word 中按 F9 更新目录]``,user 自行替换为真正 TOC。
4. **空 final.md 兜底**:章节缺正文(``content`` 为空)时段落输出占位
   "(章节未生成)";不抛错让导出整体失败(对齐 md / epub 渲染器同款约定)。
"""

from __future__ import annotations

from io import BytesIO

from docx import Document

from src.core.exporters.markdown import ChapterPayload

_TOC_PLACEHOLDER: str = "[请在 Word 中按 F9 更新目录]"
"""TOC 占位段落字面(自决项 8;python-docx 不原生支持 TOC field)。"""

_EMPTY_CONTENT_PLACEHOLDER: str = "(章节未生成)"
"""空 final.md 占位段落字面(对齐 md / epub 渲染器)。"""


def _split_paragraphs(content: str) -> list[str]:
    """章节正文 markdown 文本 → 段落字符串列表。

    分段规则:优先按双换行 ``\\n\\n`` 切段(markdown 段落约定);双换行无
    匹配时 fallback 单换行 ``\\n``。空内容返单元素列表
    ``["(章节未生成)"]``。
    """
    if not content.strip():
        return [_EMPTY_CONTENT_PLACEHOLDER]
    paragraphs = [p.strip() for p in content.split("\n\n") if p.strip()]
    if not paragraphs:
        paragraphs = [p.strip() for p in content.split("\n") if p.strip()]
    if not paragraphs:
        return [_EMPTY_CONTENT_PLACEHOLDER]
    return paragraphs


def render_docx(
    chapters: list[ChapterPayload],
    *,
    workspace_display_name: str | None = None,
) -> bytes:
    """渲染 .docx 文件 → 返二进制 ``bytes``。

    布局:

    - 顶部 ``Heading 0`` ``<display_name>``(若提供)+ TOC 占位段落
    - 按卷分组:``Heading 1`` ``卷 N: <volume_title>``
    - 卷内每章:``Heading 2`` ``第 N 章: <title>`` + ``Normal`` 段落 × M

    参数:

    - ``chapters``:已过滤(invalidated 跳过)的章节列表,卷内顺序对齐
      ``global_index``。
    - ``workspace_display_name``:作 .docx 顶部标题;空时跳过(无 Heading 0)。

    返:.docx 文件完整二进制内容(zipfile 压缩后的 OOXML 字节)。
    """
    doc = Document()

    if workspace_display_name:
        doc.add_heading(workspace_display_name, level=0)
    # TOC 占位段落(自决项 8)
    doc.add_paragraph(_TOC_PLACEHOLDER)

    current_volume_id: str | None = None
    for ch in chapters:
        if ch.volume_id != current_volume_id:
            doc.add_heading(
                f"卷 {ch.volume_index}: {ch.volume_title}", level=1
            )
            current_volume_id = ch.volume_id
        doc.add_heading(f"第 {ch.chapter_number} 章: {ch.display_title}", level=2)
        for paragraph in _split_paragraphs(ch.content):
            doc.add_paragraph(paragraph)

    buf = BytesIO()
    doc.save(buf)
    return buf.getvalue()


__all__ = ["render_docx"]
