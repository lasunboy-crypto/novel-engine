"""伏笔(PlotThread)CRUD 路由(P2-B-15)。

5 个端点(全部强制 ``X-Workspace-Id`` header):

| 方法 | 路径 | 说明 |
|------|------|------|
| GET    | ``/threads`` | 列出工作区下所有伏笔(可选 ``?status=`` / ``?importance=`` 过滤) |
| GET    | ``/threads/{thread_id}`` | 取单个伏笔详情 |
| POST   | ``/threads`` | 新建伏笔 |
| PUT    | ``/threads/{thread_id}`` | 全量更新伏笔 |
| DELETE | ``/threads/{thread_id}`` | 删除伏笔 |

工作区上下文
============

沿用 P1-B-19 决策 1 / P2-B-14 模板:工作区 ID 走 ``X-Workspace-Id`` header,
路由依赖 ``Depends(get_workspace_id)`` 解析,缺失抛
``MissingWorkspaceHeader (422)``。POST / PUT 请求体内 ``workspace_id`` 与
header 不一致 → ``PlotThreadWorkspaceMismatch (422)``;PUT 请求体内
``thread_id`` 与 URL 不一致 → ``PlotThreadIdMismatch (422)``。

筛选参数(GET ``/threads``)
============================

- ``status`` ∈ ``planted`` / ``developing`` / ``resolved`` / ``abandoned``
  (PROJECT_SPEC §3.4 / FRONTEND_SPEC §4.5 字面)
- ``importance`` ∈ 1-5

两参数都是可选;不传不过滤;同时给按 AND 组合。Pydantic ``Literal`` /
``conint`` 在 query 里走 FastAPI 的 ``Query`` 类型校验,非法值 → 422
``VALIDATION_FAILED``。

错误码矩阵
==========

| 错误码 | 来源 | 触发场景 |
|--------|------|----------|
| ``MISSING_WORKSPACE_HEADER`` (422) | 路由前置依赖 | header 缺失 |
| ``PLOT_THREAD_WORKSPACE_MISMATCH`` (422) | 路由前置守卫 | body.workspace_id ≠ header |
| ``PLOT_THREAD_ID_MISMATCH`` (422) | 路由前置守卫 | PUT body.thread_id ≠ URL |
| ``PLOT_THREAD_NOT_FOUND`` (404) | service 异常翻译 | 不存在或跨工作区 |
| ``PLOT_THREAD_ALREADY_EXISTS`` (409) | service 异常翻译 | POST ID 已被占用 |
| ``WORKSPACE_INVALID`` (422) | service 异常翻译 | meta.json 缺失 / 损坏 |
| ``VALIDATION_FAILED`` (422) | Pydantic | 请求体或 query 不合 schema |
"""

from __future__ import annotations

from typing import Literal

from fastapi import APIRouter, Depends, Query, status

from src.api.deps import get_thread_service, get_workspace_id
from src.api.errors import PlotThreadIdMismatch, PlotThreadWorkspaceMismatch
from src.api.schemas.common import SuccessEnvelope
from src.api.schemas.threads import PlotThreadDeleteResult, PlotThreadOut
from src.core.models.plot_thread import PlotThread
from src.services.thread_service import PlotThreadService

router = APIRouter(prefix="/threads", tags=["threads"])

ThreadStatus = Literal["planted", "developing", "resolved", "abandoned"]


@router.get(
    "",
    response_model=SuccessEnvelope[list[PlotThreadOut]],
    summary="列出工作区下所有伏笔",
    description=(
        "按 (created_at, thread_id) 升序返回。可选 ?status= / ?importance= 过滤。"
        "需 X-Workspace-Id header。"
    ),
)
async def list_threads(
    workspace_id: str = Depends(get_workspace_id),
    service: PlotThreadService = Depends(get_thread_service),
    status_filter: ThreadStatus | None = Query(default=None, alias="status"),
    importance: int | None = Query(default=None, ge=1, le=5),
) -> SuccessEnvelope[list[PlotThreadOut]]:
    threads = service.list_threads(
        workspace_id, status=status_filter, importance=importance
    )
    return SuccessEnvelope(
        data=[PlotThreadOut.model_validate(t.model_dump()) for t in threads],
    )


@router.get(
    "/{thread_id}",
    response_model=SuccessEnvelope[PlotThreadOut],
    summary="读取单个伏笔详情",
    description="不存在或不属于当前工作区(跨工作区视为不存在)→ PLOT_THREAD_NOT_FOUND(404)。",
)
async def get_thread(
    thread_id: str,
    workspace_id: str = Depends(get_workspace_id),
    service: PlotThreadService = Depends(get_thread_service),
) -> SuccessEnvelope[PlotThreadOut]:
    thread = service.get_thread(workspace_id, thread_id)
    return SuccessEnvelope(data=PlotThreadOut.model_validate(thread.model_dump()))


@router.post(
    "",
    response_model=SuccessEnvelope[PlotThreadOut],
    status_code=status.HTTP_201_CREATED,
    summary="新建伏笔",
    description=(
        "请求体是完整 PlotThread。body.workspace_id 必须等于 X-Workspace-Id header。"
        "thread_id 已存在 → PLOT_THREAD_ALREADY_EXISTS(409)。"
    ),
)
async def create_thread(
    body: PlotThread,
    workspace_id: str = Depends(get_workspace_id),
    service: PlotThreadService = Depends(get_thread_service),
) -> SuccessEnvelope[PlotThreadOut]:
    if body.workspace_id != workspace_id:
        raise PlotThreadWorkspaceMismatch(
            f"请求体内 workspace_id='{body.workspace_id}' 与 header '{workspace_id}' 不一致"
        )
    thread = service.create_thread(workspace_id, body)
    return SuccessEnvelope(data=PlotThreadOut.model_validate(thread.model_dump()))


@router.put(
    "/{thread_id}",
    response_model=SuccessEnvelope[PlotThreadOut],
    summary="全量更新伏笔",
    description=(
        "body.thread_id 必须等于 URL 中的 thread_id;body.workspace_id 必须"
        "等于 X-Workspace-Id header。不存在或跨工作区 → PLOT_THREAD_NOT_FOUND(404)。"
    ),
)
async def update_thread(
    thread_id: str,
    body: PlotThread,
    workspace_id: str = Depends(get_workspace_id),
    service: PlotThreadService = Depends(get_thread_service),
) -> SuccessEnvelope[PlotThreadOut]:
    if body.thread_id != thread_id:
        raise PlotThreadIdMismatch(
            f"请求体内 thread_id='{body.thread_id}' 与 URL '{thread_id}' 不一致"
        )
    if body.workspace_id != workspace_id:
        raise PlotThreadWorkspaceMismatch(
            f"请求体内 workspace_id='{body.workspace_id}' 与 header '{workspace_id}' 不一致"
        )
    thread = service.update_thread(workspace_id, body)
    return SuccessEnvelope(data=PlotThreadOut.model_validate(thread.model_dump()))


@router.delete(
    "/{thread_id}",
    response_model=SuccessEnvelope[PlotThreadDeleteResult],
    summary="删除伏笔",
    description="不存在或跨工作区 → PLOT_THREAD_NOT_FOUND(404)。",
)
async def delete_thread(
    thread_id: str,
    workspace_id: str = Depends(get_workspace_id),
    service: PlotThreadService = Depends(get_thread_service),
) -> SuccessEnvelope[PlotThreadDeleteResult]:
    service.delete_thread(workspace_id, thread_id)
    return SuccessEnvelope(data=PlotThreadDeleteResult(thread_id=thread_id))
