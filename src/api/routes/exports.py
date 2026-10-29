"""导出引擎路由(P6-B-04 md / chaptered_md)。

对齐 plan §"P6-B-04" + §"P6 端点表" 10/11/12:

| 方法 | 路径 | 作用 |
|------|------|------|
| POST | ``/workspaces/{workspace_id}/exports``                       | 启动导出(202 + ``{task_id, export_id}``)|
| GET  | ``/workspaces/{workspace_id}/exports``                       | 列表最近 10 次导出 |
| GET  | ``/workspaces/{workspace_id}/exports/{export_id}/download``  | 下载 zip 文件(``application/zip``)|

任务模式
========

``:create`` 端点同 P5-B-06 / P6-B-02 异步入口:202 + ``{task_id, export_id}``
+ ``BackgroundTasks`` 调 ``ExportService.execute(...)`` 跑 4 stage
(collecting / rendering / packaging / done);前端订阅 WS
``task.{task_id}.progress`` 接 stage 增量。

错误码
======

- ``EXPORT_FORMAT_UNSUPPORTED 422``:format 不在 md / chaptered_md(epub / docx
  在 P6-B-05/06 接力翻 200)。
- ``EXPORT_SCOPE_INVALID 422``:scope 与字段映射不一致 / scope=range 含
  invalidated。
- ``EXPORT_NOT_FOUND 404``:export_id 不存在或任务未完成。
"""

from __future__ import annotations

from datetime import datetime
from pathlib import Path
from typing import Any

from fastapi import APIRouter, BackgroundTasks, Depends, status
from fastapi.responses import FileResponse

from src.api import task_registry
from src.api.deps import get_export_service
from src.api.schemas.common import SuccessEnvelope
from src.api.schemas.exports import (
    ExportCreateIn,
    ExportCreateOut,
    ExportItemOut,
    ExportListOut,
)
from src.services.export_service import ExportNotFoundError, ExportService

router = APIRouter(prefix="/workspaces", tags=["exports"])


def _run_export_execute_task(
    service: ExportService,
    workspace_id: str,
    task_id: str,
    export_id: str,
) -> None:
    """v1.0.x wave 6 s2.6a:export execute BackgroundTask wrapper(入口防御)。

    工作区已销毁 → ``update_to_failed`` 标 WORKSPACE_DESTROYED + early-return,
    不调 service.execute(避免读 SQLite 章节内容时抛错)。前端 polling task 状态
    会拿到 failed + 错误消息;不走 WS publish(exports 模块未引 ws helper)。

    正常路径(工作区健在)→ 调 service.execute,内部已管 stage / done event +
    task_registry update + WS publish(对齐 P6-B-04 设计)。
    """
    if task_registry.is_workspace_destroyed(workspace_id):
        task_registry.update_to_failed(
            task_id,
            error={
                "code": "WORKSPACE_DESTROYED",
                "message": "工作区已被销毁,导出任务取消",
                "details": {"workspace_id": workspace_id},
            },
        )
        return
    service.execute(workspace_id, task_id=task_id, export_id=export_id)


def _manifest_to_item(manifest: dict[str, Any]) -> ExportItemOut:
    """``manifest.json`` 字典 → ``ExportItemOut``。

    ``created_at`` / ``completed_at`` 从 ISO 字符串反序列化为 datetime;空时
    保持 None。manifest 中可能存在未来扩展字段(对齐 P6-B-05/06 新格式扩
    field),按 Pydantic ``extra="forbid"`` 只取已声明字段。
    """
    created_at_str = manifest.get("created_at")
    completed_at_str = manifest.get("completed_at")
    return ExportItemOut(
        export_id=manifest["export_id"],
        workspace_id=manifest["workspace_id"],
        format=manifest["format"],
        scope=manifest["scope"],
        status=manifest.get("status", "pending"),
        created_at=(
            datetime.fromisoformat(created_at_str)
            if created_at_str
            else datetime.now()
        ),
        completed_at=(
            datetime.fromisoformat(completed_at_str)
            if completed_at_str
            else None
        ),
        chapter_count=manifest.get("chapter_count"),
        file_size_bytes=manifest.get("file_size_bytes"),
        error=manifest.get("error"),
    )


@router.post(
    "/{workspace_id}/exports",
    response_model=SuccessEnvelope[ExportCreateOut],
    status_code=status.HTTP_202_ACCEPTED,
    summary="启动异步导出任务",
    description=(
        "启动新导出任务。format 4 档(md / chaptered_md / epub / docx)— "
        "P6-B-04 仅实现 md / chaptered_md,epub / docx 返 422 "
        "``EXPORT_FORMAT_UNSUPPORTED``(P6-B-05/06 接力)。scope 3 档(full "
        "/ volume / range)。返 202 + ``{task_id, export_id}``;实际渲染 + "
        "打包走 ``BackgroundTasks``。前端订阅 WS "
        "``task.{task_id}.progress`` 接 stage 增量(collecting / rendering "
        "/ packaging / done)。scope=range 含 invalidated 章节 → 422 "
        "``EXPORT_SCOPE_INVALID``;scope=full / volume 默认跳过 invalidated。"
    ),
)
async def create_export(
    workspace_id: str,
    body: ExportCreateIn,
    background_tasks: BackgroundTasks,
    service: ExportService = Depends(get_export_service),
) -> SuccessEnvelope[ExportCreateOut]:
    task_id = task_registry.new_task_id()
    task_registry.record_pending(task_id, type="workspace_export")
    export_id, _manifest = service.create_export(
        workspace_id,
        task_id=task_id,
        format=body.format,
        scope=body.scope,
        volume_id=body.volume_id,
        start_chapter_id=body.start_chapter_id,
        end_chapter_id=body.end_chapter_id,
        include_metadata=body.include_metadata,
        include_review_history=body.include_review_history,
        include_length_notes=body.include_length_notes,
    )
    background_tasks.add_task(
        _run_export_execute_task,
        service,
        workspace_id,
        task_id,
        export_id,
    )
    return SuccessEnvelope(
        data=ExportCreateOut(
            task_id=task_id,
            export_id=export_id,
            workspace_id=workspace_id,
            format=body.format,
            scope=body.scope,
            status="pending",
        )
    )


@router.get(
    "/{workspace_id}/exports",
    response_model=SuccessEnvelope[ExportListOut],
    summary="列出最近导出条目",
    description=(
        "返工作区最近 10 条导出条目(按 created_at 倒序)。每条含 "
        "export_id / format / scope / status / created_at / completed_at "
        "/ chapter_count / file_size_bytes / error。任务未完成(pending / "
        "running)条目仍出现在列表,前端可据 status 与 task_id 显示进度。"
    ),
)
async def list_exports(
    workspace_id: str,
    service: ExportService = Depends(get_export_service),
) -> SuccessEnvelope[ExportListOut]:
    manifests = service.list_exports(workspace_id)
    items = [_manifest_to_item(m) for m in manifests]
    return SuccessEnvelope(data=ExportListOut(items=items))


@router.get(
    "/{workspace_id}/exports/{export_id}/download",
    summary="下载导出 zip 文件",
    description=(
        "返导出 zip 文件流(``application/zip``)。export_id 不存在 / 任务"
        "未完成(status != done)/ zip 文件缺失 → 404 ``EXPORT_NOT_FOUND``。"
        "下载文件名形如 ``{workspace_id}-{export_id}.zip`` (Content-Disposition: "
        "attachment)便于浏览器保存。"
    ),
    responses={
        200: {
            "content": {"application/zip": {}},
            "description": "导出 zip 文件流",
        }
    },
)
async def download_export(
    workspace_id: str,
    export_id: str,
    service: ExportService = Depends(get_export_service),
) -> FileResponse:
    zip_path: Path = service.get_download_path(workspace_id, export_id)
    if not zip_path.is_file():
        # 双重保险(get_download_path 已校验,这里防 TOCTOU)
        raise ExportNotFoundError(
            workspace_id=workspace_id, export_id=export_id
        )
    return FileResponse(
        path=zip_path,
        media_type="application/zip",
        filename=f"{workspace_id}-{export_id}.zip",
    )
