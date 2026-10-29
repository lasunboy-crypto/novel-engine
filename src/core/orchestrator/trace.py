"""章节级 trace_id 生成(P1-B-24)。

对齐 OBSERVABILITY §2.3:

- ``trace_id`` 章节维度,格式 ``{chapter_id}-{operation}{n}``
- 例:``ch-0012-rewrite0``、``ch-0012-rewrite1``、``ch-0012-revise0``、
  ``ch-0012-write0``
- 一章从生成到 APPROVED 共享一个 trace_id 系列;不同 ``operation`` 用不同
  后缀区分(write / rewrite / revise)

P1 简版职责
============

只生成字符串,不维护"已用过哪些 n";调用方
(``ChapterService.write/rewrite/revise``)按场景显式传 ``operation`` /
``n``。``write`` 总是 ``write0``;``rewrite`` 用 ``prior_rewrite_count``
(P1-B-15 已经从调用方接进来)做 n;``revise`` 同理。

P2 持久化 chapters 表后,可改为"读 chapters.write_count / rewrite_count
/ revise_count 自动递增 n";本签名可向后兼容(``n`` 改默认值即可)。
"""

from __future__ import annotations

from typing import Literal

# Trace operation 类型。``write`` 与 ``rewrite`` 走 Orchestrator;``revise``
# 走 ChapterService.revise(不走 Orchestrator);``re-review``(v2 W3.5 UI 缺口3)
# 走 ChapterService.re_review,对人工改稿入 REVIEWING 单次重审。
TraceOperation = Literal["write", "rewrite", "revise", "re-review"]


def make_trace_id(
    chapter_id: str,
    operation: TraceOperation,
    n: int,
) -> str:
    """构造章节级 trace_id。

    格式 ``{chapter_id}-{operation}{n}``。``n`` 从 0 开始计数,与
    OBSERVABILITY §2.3 字面例 ``v1c012-rewrite0`` / ``v1c012-rewrite1``
    保持一致(0-based)。

    Args:
        chapter_id: 章节 ID,如 ``"ch-0001"``。
        operation: 操作类型,``"write"`` / ``"rewrite"`` / ``"revise"`` 之一。
        n: 同 operation 在本章内的第几次(0-based)。

    Returns:
        ``f"{chapter_id}-{operation}{n}"``,如 ``"ch-0001-rewrite0"``。

    Raises:
        ValueError: ``n`` 为负数(P1 调用方传入永远 ≥ 0,这里仅作保险)。
    """
    if n < 0:
        raise ValueError(f"trace_id n 不能为负: {n}")
    return f"{chapter_id}-{operation}{n}"
