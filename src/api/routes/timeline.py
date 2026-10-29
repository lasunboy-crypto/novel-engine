"""时间线条目(TimelineEntry)CRUD 路由(P2-B-16)。

5 个端点(全部强制 ``X-Workspace-Id`` header):

| 方法 | 路径 | 说明 |
|------|------|------|
| GET    | ``/timeline`` | 列出工作区下所有时间线条目(按 time_order 升序) |
| GET    | ``/timeline/{entry_id}`` | 取单个条目详情 |
| POST   | ``/timeline`` | 新建条目 |
| PUT    | ``/timeline/{entry_id}`` | 全量更新条目 |
| DELETE | ``/timeline/{entry_id}`` | 删除条目 |

工作区上下文
============

沿用 P1-B-19 决策 1 / P2-B-14 / P2-B-15 模板:工作区 ID 走 ``X-Workspace-Id``
header,路由依赖 ``Depends(get_workspace_id)`` 解析,缺失抛
``MissingWorkspaceHeader (422)``。POST / PUT 请求体内 ``workspace_id`` 与
header 不一致 → ``TimelineEntryWorkspaceMismatch (422)``;PUT 请求体内
``entry_id`` 与 URL 不一致 → ``TimelineEntryIdMismatch (422)``。

排序
====

GET ``/timeline`` 按 ``(time_order, entry_id)`` 升序返回(store 层
``list_timeline_entries_by_workspace`` 已排好序,service / 路由不重排)。
**单调校验不在此层做**——PROJECT_SPEC §4.2 [HARD] 时间线单调校验由
Reviewer 在 commit 前批量执行(P2-B-02 / P2-B-04 决策),允许中间态
``time_order`` 重复 / 乱序。

错误码矩阵
==========

| 错误码 | 来源 | 触发场景 |
|--------|------|----------|
| ``MISSING_WORKSPACE_HEADER`` (422) | 路由前置依赖 | header 缺失 |
| ``TIMELINE_ENTRY_WORKSPACE_MISMATCH`` (422) | 路由前置守卫 | body.workspace_id ≠ header |
| ``TIMELINE_ENTRY_ID_MISMATCH`` (422) | 路由前置守卫 | PUT body.entry_id ≠ URL |
| ``TIMELINE_ENTRY_NOT_FOUND`` (404) | service 异常翻译 | 不存在或跨工作区 |
| ``TIMELINE_ENTRY_ALREADY_EXISTS`` (409) | service 异常翻译 | POST ID 已被占用 |
| ``WORKSPACE_INVALID`` (422) | service 异常翻译 | meta.json 缺失 / 损坏 |
| ``VALIDATION_FAILED`` (422) | Pydantic | 请求体不合 schema |
"""

from __future__ import annotations

from fastapi import APIRouter, Depends, status

from src.api.deps import get_timeline_service, get_workspace_id
from src.api.errors import (
    TimelineEntryIdMismatch,
    TimelineEntryWorkspaceMismatch,
)
from src.api.schemas.common import SuccessEnvelope
from src.api.schemas.timeline import (
    TimelineEntryDeleteResult,
    TimelineEntryOut,
)
from src.core.models.timeline_entry import TimelineEntry
from src.services.timeline_service import TimelineEntryService

router = APIRouter(prefix="/timeline", tags=["timeline"])


@router.get(
    "",
    response_model=SuccessEnvelope[list[TimelineEntryOut]],
    summary="列出工作区下所有时间线条目",
    description="按 (time_order, entry_id) 升序返回。需 X-Workspace-Id header。",
)
async def list_entries(
    workspace_id: str = Depends(get_workspace_id),
    service: TimelineEntryService = Depends(get_timeline_service),
) -> SuccessEnvelope[list[TimelineEntryOut]]:
    entries = service.list_entries(workspace_id)
    return SuccessEnvelope(
        data=[TimelineEntryOut.model_validate(e.model_dump()) for e in entries],
    )


@router.get(
    "/{entry_id}",
    response_model=SuccessEnvelope[TimelineEntryOut],
    summary="读取单个时间线条目详情",
    description="不存在或不属于当前工作区(跨工作区视为不存在)→ TIMELINE_ENTRY_NOT_FOUND(404)。",
)
async def get_entry(
    entry_id: str,
    workspace_id: str = Depends(get_workspace_id),
    service: TimelineEntryService = Depends(get_timeline_service),
) -> SuccessEnvelope[TimelineEntryOut]:
    entry = service.get_entry(workspace_id, entry_id)
    return SuccessEnvelope(data=TimelineEntryOut.model_validate(entry.model_dump()))


@router.post(
    "",
    response_model=SuccessEnvelope[TimelineEntryOut],
    status_code=status.HTTP_201_CREATED,
    summary="新建时间线条目",
    description=(
        "请求体是完整 TimelineEntry。body.workspace_id 必须等于 X-Workspace-Id header。"
        "entry_id 已存在 → TIMELINE_ENTRY_ALREADY_EXISTS(409)。"
    ),
)
async def create_entry(
    body: TimelineEntry,
    workspace_id: str = Depends(get_workspace_id),
    service: TimelineEntryService = Depends(get_timeline_service),
) -> SuccessEnvelope[TimelineEntryOut]:
    if body.workspace_id != workspace_id:
        raise TimelineEntryWorkspaceMismatch(
            f"请求体内 workspace_id='{body.workspace_id}' 与 header '{workspace_id}' 不一致"
        )
    entry = service.create_entry(workspace_id, body)
    return SuccessEnvelope(data=TimelineEntryOut.model_validate(entry.model_dump()))


@router.put(
    "/{entry_id}",
    response_model=SuccessEnvelope[TimelineEntryOut],
    summary="全量更新时间线条目",
    description=(
        "body.entry_id 必须等于 URL 中的 entry_id;body.workspace_id 必须"
        "等于 X-Workspace-Id header。不存在或跨工作区 → TIMELINE_ENTRY_NOT_FOUND(404)。"
    ),
)
async def update_entry(
    entry_id: str,
    body: TimelineEntry,
    workspace_id: str = Depends(get_workspace_id),
    service: TimelineEntryService = Depends(get_timeline_service),
) -> SuccessEnvelope[TimelineEntryOut]:
    if body.entry_id != entry_id:
        raise TimelineEntryIdMismatch(
            f"请求体内 entry_id='{body.entry_id}' 与 URL '{entry_id}' 不一致"
        )
    if body.workspace_id != workspace_id:
        raise TimelineEntryWorkspaceMismatch(
            f"请求体内 workspace_id='{body.workspace_id}' 与 header '{workspace_id}' 不一致"
        )
    entry = service.update_entry(workspace_id, body)
    return SuccessEnvelope(data=TimelineEntryOut.model_validate(entry.model_dump()))


@router.delete(
    "/{entry_id}",
    response_model=SuccessEnvelope[TimelineEntryDeleteResult],
    summary="删除时间线条目",
    description="不存在或跨工作区 → TIMELINE_ENTRY_NOT_FOUND(404)。",
)
async def delete_entry(
    entry_id: str,
    workspace_id: str = Depends(get_workspace_id),
    service: TimelineEntryService = Depends(get_timeline_service),
) -> SuccessEnvelope[TimelineEntryDeleteResult]:
    service.delete_entry(workspace_id, entry_id)
    return SuccessEnvelope(data=TimelineEntryDeleteResult(entry_id=entry_id))
