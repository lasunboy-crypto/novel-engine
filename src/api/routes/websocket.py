"""WebSocket 路由(P1-B-20)。

路径:``/api/v1/ws``(挂载时由 ``main.py`` 加 ``/api/v1`` 前缀)。

职责
====

- ``accept`` + 登记连接 → 启动两个 background task:**drainer**
  (``await outbox.get()`` → ``await ws.send_json``)+ **heartbeat**
  (周期发 ``ping``,两次未回 ``pong`` → 关连接)。
- 主循环 ``await ws.receive_json()`` 处理客户端消息:``subscribe`` /
  ``unsubscribe`` / ``pong``;未知 ``type`` 静默忽略(spec 没规定,P1 简版
  避免抛错关连接)。
- ``WebSocketDisconnect`` 时 cancel 两个 task + ``manager.disconnect``。

P1 不实现
=========

- **认证**:``API_AUTH_ENABLED=true`` 时按 API_CONTRACT §7.2 走 ``?token=xxx``
  query 参数,P1 默认无认证(``settings.api_auth_enabled=false``,且本任务不
  实现认证分支;路径绑 ``127.0.0.1`` 已是默认硬约束)。
- **断线重连恢复订阅**:客户端要重连后再次 ``subscribe``,不持久化订阅状态。
- **WebSocket 上的 OpenAPI 描述**:WebSocket 路由不进 OpenAPI(REST-only)。
"""

from __future__ import annotations

import asyncio

import structlog
from fastapi import APIRouter, WebSocket, WebSocketDisconnect

from src.api.websocket.manager import manager

_log = structlog.get_logger(__name__)

# 心跳参数:常量化便于测试 monkeypatch(对应 P1-B-20 设计选择 A)。
HEARTBEAT_INTERVAL_SECONDS: float = 30.0
HEARTBEAT_MAX_MISSED: int = 2

router = APIRouter()


@router.websocket("/ws")
async def websocket_endpoint(websocket: WebSocket) -> None:
    """WebSocket 入口。挂在 ``/api/v1/ws``(由 ``main.py`` 加前缀)。

    生命周期:``connect`` → 起 drainer + heartbeat → 主循环收消息 →
    ``WebSocketDisconnect`` / 异常 → cancel tasks + disconnect。
    """
    conn_id = await manager.connect(websocket)
    drain_task = asyncio.create_task(_drain_outbox(conn_id))
    heartbeat_task = asyncio.create_task(_heartbeat(conn_id))
    try:
        while True:
            msg = await websocket.receive_json()
            _handle_client_message(conn_id, msg)
    except WebSocketDisconnect:
        pass
    except Exception:  # pragma: no cover - defensive logging
        _log.exception("ws_endpoint_unhandled", conn_id=conn_id)
    finally:
        drain_task.cancel()
        heartbeat_task.cancel()
        manager.disconnect(conn_id)


def _handle_client_message(conn_id: str, msg: dict) -> None:
    """处理客户端消息(``subscribe`` / ``unsubscribe`` / ``pong``)。

    未知 ``type`` 静默忽略:P1-B-20 设计选择 C —— spec §5.1 只定义了三种,
    其他静默丢弃既不破坏前向兼容也不让连接因小问题断掉。
    """
    msg_type = msg.get("type") if isinstance(msg, dict) else None
    if msg_type == "subscribe":
        channels = msg.get("channels") or []
        if isinstance(channels, list):
            manager.subscribe(conn_id, channels)
    elif msg_type == "unsubscribe":
        channels = msg.get("channels") or []
        if isinstance(channels, list):
            manager.unsubscribe(conn_id, channels)
    elif msg_type == "pong":
        conn = manager.get_connection(conn_id)
        if conn is not None:
            conn.missed_pongs = 0


async def _drain_outbox(conn_id: str) -> None:
    """从连接 outbox 读消息并 ``await ws.send_json``。

    异常(连接已关、send 失败等)直接退出循环,主循环的 ``finally`` 会
    清理连接。``CancelledError`` 由调用方在 ``finally`` 触发,这里不特殊处理。
    """
    conn = manager.get_connection(conn_id)
    if conn is None:
        return
    try:
        while True:
            msg = await conn.outbox.get()
            try:
                await conn.websocket.send_json(msg)
            except Exception:
                _log.debug("ws_send_failed", conn_id=conn_id)
                return
    except asyncio.CancelledError:
        raise


async def _heartbeat(conn_id: str) -> None:
    """周期发送 ``ping``;``HEARTBEAT_MAX_MISSED`` 次未收到 ``pong`` 即关连接。

    每轮:``sleep(interval)`` → ``missed_pongs += 1``(在发 ping **之前**累加,
    收到 pong 时清零) → 超阈值 → ``ws.close`` 让主循环捕到
    ``WebSocketDisconnect`` → 否则 send ``{"type": "ping"}``。
    """
    interval = HEARTBEAT_INTERVAL_SECONDS
    max_missed = HEARTBEAT_MAX_MISSED
    try:
        while True:
            await asyncio.sleep(interval)
            conn = manager.get_connection(conn_id)
            if conn is None:
                return
            conn.missed_pongs += 1
            if conn.missed_pongs > max_missed:
                _log.info(
                    "ws_heartbeat_timeout",
                    conn_id=conn_id,
                    missed=conn.missed_pongs,
                )
                try:
                    await conn.websocket.close(code=1011)  # internal error
                except Exception:
                    pass
                return
            try:
                await conn.websocket.send_json({"type": "ping"})
            except Exception:
                return
    except asyncio.CancelledError:
        raise
