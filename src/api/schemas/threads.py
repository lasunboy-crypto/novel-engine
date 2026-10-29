"""伏笔 CRUD 请求 / 响应 schema(P2-B-15)。

对齐 PROJECT_SPEC §3.4 ``PlotThread`` 模型 + API_CONTRACT §4 URL 规范(顶级
资源 ``/threads``,工作区上下文走 ``X-Workspace-Id`` header)。

设计要点
========

完全沿用 P2-B-14 ``Character`` schema 模板:

- ``PlotThreadOut`` 直接继承 ``PlotThread``(空子类),保留 API 表面独立演进
  坐标(P2+ 可能加 ``_links`` / 引用计数等字段)。
- POST / PUT 请求体直接用 ``PlotThread``(P1-B-19 ``PUT /chapters/{id}/card``
  同模式),路由层校验 ``body.workspace_id`` / ``body.thread_id`` 与 URL /
  Header 一致;不一致抛 ``PlotThreadWorkspaceMismatch`` /
  ``PlotThreadIdMismatch`` 422。
- ``PlotThreadDeleteResult``:DELETE 成功响应 data 段,只回显 ``thread_id``。
"""

from __future__ import annotations

from pydantic import BaseModel, ConfigDict

from src.core.models.plot_thread import PlotThread


class PlotThreadOut(PlotThread):
    """``GET /threads`` / ``GET /threads/{id}`` 响应 data 段(空子类)。"""


class PlotThreadDeleteResult(BaseModel):
    """``DELETE /threads/{id}`` 成功响应 data 段。"""

    model_config = ConfigDict(extra="forbid")

    thread_id: str
