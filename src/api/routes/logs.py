"""日志查询 API 路由(P4-B-05)。

OBSERVABILITY §2.6 字面端点(系统级,不归 ``/workspaces`` 子资源):

| 方法 | 路径 | 说明 |
|------|------|------|
| GET  | ``/logs?...``                    | 多字段过滤分页查询 |
| GET  | ``/logs/trace/{id}``             | Trace 视图(``id`` 按 ``req_`` 前缀智能分流为 trace_id 或 request_id) |
| WS   | ``/logs/stream``                 | 实时日志流(P4-B-05 最简实现) |

任务模板字面"5 端点"实际是 §2.6 3 GET + 1 WS = 4 路由(``GET /logs/trace/{request_id}``
与 ``GET /logs/trace/{trace_id}`` 共用同一 path,后端按字面格式智能识别
分流;§4.2 字面 ``GET /api/v1/logs/trace/{request_id}`` 是 §2.6 单 path 的
复用扩展表达,impl 文档登记类 A 冲突)。

数据来源 + 错误语义见 ``src.services.logs_service`` 模块 docstring。
"""

from __future__ import annotations

import asyncio
import datetime as _dt

from fastapi import APIRouter, Depends, Query, WebSocket, WebSocketDisconnect

from src.api.schemas.common import SuccessEnvelope
from src.api.schemas.logs import (
    LogEntryOut,
    LogQueryOut,
    TraceViewOut,
)
from src.config import settings
from src.services.logs_service import (
    LogQueryFilters,
    LogsService,
    classify_trace_id,
)

router = APIRouter(tags=["logs"])

# WS tail 间隔(秒)。常量化便于测试 monkeypatch。
LOG_STREAM_POLL_SECONDS: float = 1.0


def get_logs_service() -> LogsService:
    """构造默认 ``LogsService``,从 ``settings.data_dir`` 取根目录。

    测试通过 ``app.dependency_overrides[get_logs_service] = lambda: ...``
    注入指向 ``tmp_path`` 的实例,避免污染本机 settings.data_dir。
    """
    return LogsService(data_dir=settings.data_dir)


@router.get(
    "/logs",
    response_model=SuccessEnvelope[LogQueryOut],
    summary="日志查询(多字段过滤 + 分页)",
)
async def list_logs(
    workspace_id: str | None = Query(None, description="按 workspace_id 字段过滤"),
    chapter_id: str | None = Query(None, description="按 chapter_id 字段过滤"),
    trace_id: str | None = Query(None, description="按 trace_id 字段过滤"),
    request_id: str | None = Query(None, description="按 request_id 字段过滤"),
    event: str | None = Query(None, description="按 event 字段过滤(snake_case)"),
    level: str | None = Query(
        None, description="按 level 字段过滤(case-insensitive)"
    ),
    since: _dt.datetime | None = Query(None, description="起始时间(inclusive)"),
    until: _dt.datetime | None = Query(None, description="终止时间(inclusive)"),
    page: int = Query(1, ge=1),
    page_size: int = Query(50, ge=1, le=500),
    service: LogsService = Depends(get_logs_service),
) -> SuccessEnvelope[LogQueryOut]:
    filters = LogQueryFilters(
        workspace_id=workspace_id,
        chapter_id=chapter_id,
        trace_id=trace_id,
        request_id=request_id,
        event=event,
        level=level,
        since=since,
        until=until,
        page=page,
        page_size=page_size,
    )
    items, total = service.query(filters)
    return SuccessEnvelope(
        data=LogQueryOut(
            items=[LogEntryOut.model_validate(r) for r in items],
            page=page,
            page_size=page_size,
            total=total,
        )
    )


@router.get(
    "/logs/trace/{trace_or_request_id}",
    response_model=SuccessEnvelope[TraceViewOut],
    summary="Trace 视图(智能分流 trace_id / request_id)",
)
async def get_trace_view(
    trace_or_request_id: str,
    service: LogsService = Depends(get_logs_service),
) -> SuccessEnvelope[TraceViewOut]:
    kind = classify_trace_id(trace_or_request_id)
    if kind == "request_id":
        rows = service.list_by_request(trace_or_request_id)
    else:
        rows = service.list_by_trace(trace_or_request_id)
    items = [LogEntryOut.model_validate(r) for r in rows]
    return SuccessEnvelope(
        data=TraceViewOut(
            kind=kind,
            id=trace_or_request_id,
            items=items,
            total=len(items),
        )
    )


@router.websocket("/logs/stream")
async def logs_stream(websocket: WebSocket) -> None:
    """实时日志流(P4-B-05 最简实现)。

    握手后周期(``LOG_STREAM_POLL_SECONDS``)调 ``LogsService.tail`` 比对
    上次 cursor(``timestamp``+``event``)只 push 新增行。客户端 ``close`` /
    ``WebSocketDisconnect`` 时退出循环。

    P5+ 升级路径:文件变更通知(inotify / FSEvents)替代 polling,降低 CPU
    + 减少日志读取延迟。
    """
    await websocket.accept()
    service = get_logs_service()
    last_seen: str | None = None
    try:
        while True:
            rows = service.tail(n=200)  # 反向(最新在前)
            new_rows = []
            for r in rows:
                cursor = f"{r.get('timestamp', '')}|{r.get('event', '')}"
                if last_seen is not None and cursor == last_seen:
                    break
                new_rows.append(r)
            if new_rows:
                # 反向回正向(发送时按 timestamp 升序),刷新 cursor。
                last_seen = (
                    f"{new_rows[0].get('timestamp', '')}|"
                    f"{new_rows[0].get('event', '')}"
                )
                for row in reversed(new_rows):
                    await websocket.send_json(row)
            await asyncio.sleep(LOG_STREAM_POLL_SECONDS)
    except WebSocketDisconnect:
        return
