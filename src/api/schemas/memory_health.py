"""记忆健康度检查请求 / 响应 schema(P4-B-09)。

对齐 OBSERVABILITY §3.1 ``memory_health_history`` 表字段 + Pydantic
``MemoryHealthCheck`` 模型(``src/core/models/metrics.py``)+ API_CONTRACT
§3.1 响应包络 + §5.3 ``memory_health_critical`` 频道事件。

设计要点
========

- ``MemoryHealthCheckOut`` 直接继承 ``MemoryHealthCheck``(空子类),沿用
  ``CharacterOut`` / ``WorkspaceOut`` 模式:字段完全一致,保留 API 表面
  独立演进坐标。
- POST 路由本身无请求体(同步等结果模式;``recent_chapter_window`` 等
  调参留 query 参数 + future-flag,P4-B-09 范围内 fix-size)。**预留**:
  ``MemoryHealthCheckRequest`` 在本文件登场前未列入 schema/__init__.py
  exports,P4-B-10 / P4-acceptance 真 LLM 配套实现按需扩。
"""

from __future__ import annotations

from src.core.models.metrics import MemoryHealthCheck


class MemoryHealthCheckOut(MemoryHealthCheck):
    """``POST /workspaces/{id}/memory/check`` 响应 data 段(空子类)。

    继承 ``MemoryHealthCheck`` 7 字段(check_id / checked_at / severity /
    3 类 count / report_json);report_json 内含 LLM 输出的完整 summary +
    issues 三类列表,前端可直接消费。
    """


__all__ = ["MemoryHealthCheckOut"]
