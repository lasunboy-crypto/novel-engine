"""WebSocket 连接 / 订阅 / 推送管理(P1-B-20)。

对齐 API_CONTRACT §5(WebSocket 协议)。**P1 单进程内存实现**,P3 多 worker
化时换 Redis pub-sub 或类似机制。

设计要点
========

- **publish 是同步 API**:把消息丢进每个订阅连接的 ``asyncio.Queue``
  (``put_nowait``),由每连接的 drainer 异步任务真正 ``await ws.send_json``。
  业务层(Orchestrator / ChapterService)是同步代码,sync ``publish`` 让
  路由层闭包零 ``await`` 调用。
- **业务层不 import manager**:Orchestrator / ChapterService 通过
  ``on_state_transition: Callable[[ChapterState, ChapterState], None]`` 回调
  接 manager,manager 只在 ``api.routes.websocket`` 与 ``api.routes.chapters``
  的闭包里被引用——保持业务层不感知 WebSocket。
- **每连接 ``asyncio.Queue``**:`publish` 写,drainer 读;断连时 drainer
  cancel,outbox 自动 GC。订阅 / 取消订阅是 ``set`` 操作,O(1)。
- **不持久化连接 / 订阅**:重启即清空。P3 多 worker 化时引入 Redis pub-sub
  或类似机制。

频道命名见 ``channels.py``。
"""

from __future__ import annotations

import asyncio
import secrets
from collections.abc import Iterable
from datetime import datetime, timezone
from typing import Any

import structlog
from fastapi import WebSocket

_log = structlog.get_logger(__name__)


class _Connection:
    """单个 WebSocket 连接的内部状态:websocket 对象 + 订阅频道集合 + 出站队列。

    ``missed_pongs`` 由心跳逻辑(``api.routes.websocket._heartbeat``)读写;
    收到 ``pong`` 时清零,发出 ``ping`` 时 +1,达到上限即关连接。
    """

    __slots__ = ("websocket", "conn_id", "channels", "outbox", "missed_pongs")

    def __init__(self, websocket: WebSocket, conn_id: str) -> None:
        self.websocket = websocket
        self.conn_id = conn_id
        self.channels: set[str] = set()
        self.outbox: asyncio.Queue[dict[str, Any]] = asyncio.Queue()
        self.missed_pongs: int = 0


class WebSocketManager:
    """连接管理 + 订阅 + 同步 publish(P1 单进程内存实现)。

    所有公共方法均线程不安全,只允许在同一 event loop 内调用——P1 单 worker
    场景下足够。P3 多 worker 化时换实现,接口形态尽量保持不变。
    """

    def __init__(self) -> None:
        self._connections: dict[str, _Connection] = {}

    # ---- 连接生命周期 ------------------------------------------------------

    async def connect(self, websocket: WebSocket) -> str:
        """``await websocket.accept()`` 并登记一个新连接,返回 ``conn_id``。"""
        await websocket.accept()
        conn_id = f"conn_{secrets.token_hex(8)}"
        self._connections[conn_id] = _Connection(websocket, conn_id)
        _log.info("ws_connect", conn_id=conn_id)
        return conn_id

    def disconnect(self, conn_id: str) -> None:
        """注销连接(不主动 ``close`` websocket;调用方负责 close 与 task 取消)。"""
        if self._connections.pop(conn_id, None) is not None:
            _log.info("ws_disconnect", conn_id=conn_id)

    # ---- 订阅 --------------------------------------------------------------

    def subscribe(self, conn_id: str, channels: Iterable[str]) -> None:
        """把若干频道加进连接的订阅集合;未知 ``conn_id`` 静默忽略。"""
        conn = self._connections.get(conn_id)
        if conn is None:
            return
        new = set(channels)
        conn.channels |= new
        _log.info("ws_subscribe", conn_id=conn_id, channels=sorted(new))

    def unsubscribe(self, conn_id: str, channels: Iterable[str]) -> None:
        """从连接订阅集合中移除若干频道;未知 ``conn_id`` 静默忽略。"""
        conn = self._connections.get(conn_id)
        if conn is None:
            return
        gone = set(channels) & conn.channels
        conn.channels -= gone
        if gone:
            _log.info("ws_unsubscribe", conn_id=conn_id, channels=sorted(gone))

    def channels_for(self, conn_id: str) -> set[str]:
        """读单个连接的订阅快照(测试与诊断用)。"""
        conn = self._connections.get(conn_id)
        return set(conn.channels) if conn else set()

    # ---- 推送 --------------------------------------------------------------

    def publish(
        self, channel: str, event: str, data: dict[str, Any]
    ) -> None:
        """同步推送:把消息塞进每个订阅了 ``channel`` 的连接的 outbox 队列。

        实际 ``await ws.send_json`` 由每连接的 drainer task 异步执行。
        ``data`` 必须可 JSON 序列化(``websocket.send_json`` 会进一步校验)。
        """
        msg = _build_message(channel, event, data)
        delivered = 0
        for conn in self._connections.values():
            if channel in conn.channels:
                conn.outbox.put_nowait(msg)
                delivered += 1
        # 注:structlog 把第一个位置参数当 ``event``(消息名);本协议字段同名
        # ``event`` 会撞,改用 ``ws_event`` key 旁路。
        _log.debug(
            "ws_publish",
            channel=channel,
            ws_event=event,
            delivered=delivered,
        )

    # ---- 内部访问(供 routes/websocket.py drainer / heartbeat 用)----------

    def get_connection(self, conn_id: str) -> _Connection | None:
        """供 drainer / heartbeat 取连接对象;路由层之外不要调用。"""
        return self._connections.get(conn_id)

    # ---- 测试用 ------------------------------------------------------------

    def reset(self) -> None:
        """清空连接表(测试用;生产路径不应调用)。"""
        self._connections.clear()


def _build_message(
    channel: str, event: str, data: dict[str, Any]
) -> dict[str, Any]:
    """组装 API_CONTRACT §5.2 字面格式的服务端推送消息。"""
    return {
        "channel": channel,
        "event": event,
        "timestamp": _now_iso(),
        "data": data,
    }


def _now_iso() -> str:
    """ISO 8601 UTC 时间字符串,Z 后缀(API_CONTRACT §9 字段命名约定)。"""
    return (
        datetime.now(timezone.utc)
        .isoformat(timespec="milliseconds")
        .replace("+00:00", "Z")
    )


# 模块级单例。路由层 (`api.routes.websocket` / `api.routes.chapters`) 与测试
# 共用同一份;业务层不 import 此对象。
manager = WebSocketManager()


__all__ = ["WebSocketManager", "manager"]
