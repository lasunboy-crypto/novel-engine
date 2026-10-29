"""风格漂移审计请求 / 响应 schema(P4-B-10)。

对齐 OBSERVABILITY §3.1 ``style_audit_history`` 表字段 + Pydantic
``StyleAuditEntry`` 模型(``src/core/models/metrics.py``)+ API_CONTRACT
§3.1 响应包络。

设计要点
========

- ``StyleAuditRequest`` 请求体:可选 ``chapter_range_start`` /
  ``chapter_range_end``(plan §P4-B-10 字面入参字段)。两个字段 None 表
  "整体审计无范围",P4 stub 允许;P7 完整实现可加 query 参数覆盖
  ``recent_chapters_window`` 等。
- ``StyleAuditEntryOut`` 直接继承 ``StyleAuditEntry``(空子类),沿用
  ``MemoryHealthCheckOut`` 模式:字段完全一致,保留 API 表面独立演进坐标。
"""

from __future__ import annotations

from pydantic import BaseModel, Field

from src.core.models.metrics import StyleAuditEntry


class StyleAuditRequest(BaseModel):
    """``POST /workspaces/{id}/style/audit`` 请求体。

    P4 stub 阶段两字段均可选;P7 完整实现可扩 ``recent_chapters_window``
    等参数。
    """

    chapter_range_start: str | None = Field(
        None,
        description="审计起点 chapter_id(可选,P7 完整版预读对应章节正文)",
    )
    chapter_range_end: str | None = Field(
        None,
        description="审计终点 chapter_id(可选,同上)",
    )


class StyleAuditEntryOut(StyleAuditEntry):
    """``POST /workspaces/{id}/style/audit`` 响应 data 段(空子类)。

    继承 ``StyleAuditEntry`` 7 字段(audit_id / audited_at /
    chapter_range_start / _end / drift_score / severity / report_json)。
    P4 stub 阶段 ``drift_score=None`` / ``severity="stub"``;P7 完整实现时
    字段值升级(schema 不变)。
    """


__all__ = ["StyleAuditEntryOut", "StyleAuditRequest"]
