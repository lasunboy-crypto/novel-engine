"""时间线条目 CRUD 请求 / 响应 schema(P2-B-16)。

对齐 PROJECT_SPEC §3.4 ``TimelineEntry`` 模型 + API_CONTRACT §4 URL 规范
(顶级资源 ``/timeline``,工作区上下文走 ``X-Workspace-Id`` header)。

设计要点
========

完全沿用 P2-B-14 ``Character`` / P2-B-15 ``PlotThread`` schema 模板:

- ``TimelineEntryOut`` 直接继承 ``TimelineEntry``(空子类),保留 API 表面
  独立演进坐标。
- POST / PUT 请求体直接用 ``TimelineEntry``(P1-B-19 ``PUT /chapters/{id}/card``
  同模式),路由层校验 ``body.workspace_id`` / ``body.entry_id`` 与 URL /
  Header 一致;不一致抛 ``TimelineEntryWorkspaceMismatch`` /
  ``TimelineEntryIdMismatch`` 422。
- ``TimelineEntryDeleteResult``:DELETE 成功响应 data 段,只回显 ``entry_id``。
"""

from __future__ import annotations

from pydantic import BaseModel, ConfigDict

from src.core.models.timeline_entry import TimelineEntry


class TimelineEntryOut(TimelineEntry):
    """``GET /timeline`` / ``GET /timeline/{id}`` 响应 data 段(空子类)。"""


class TimelineEntryDeleteResult(BaseModel):
    """``DELETE /timeline/{id}`` 成功响应 data 段。"""

    model_config = ConfigDict(extra="forbid")

    entry_id: str
