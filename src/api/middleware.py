"""ASGI 中间件:request_id 注入 + API 调用日志(P1-B-17)+ 系统 metrics 持久化(P4-B-04)+ API 限流(P4-B-10)。

对齐 OBSERVABILITY §2.3(``request_id`` 格式 ``req_<random12>``)与 §2.4(API
事件 ``api_request``)+ §3.1 ``system_metrics_daily`` 表 + API_CONTRACT §8.1
接口限流(P4-B-10 升级为按 workspace 滑窗版,默认 100 req/min)。

实现选型:纯 ASGI 中间件,**不用** ``BaseHTTPMiddleware``
=========================================================

starlette 0.40+ 已知问题:``BaseHTTPMiddleware`` 内部用 anyio task group +
collapse_excgroups 把下游异常重新抛出,会绕过 FastAPI 的全局 ``Exception``
handler——表现为 endpoint raise 一个未注册类型的异常时,响应不会被 ApiError
体系翻译,而是冒到 ServerErrorMiddleware 默认 500。改写成纯 ASGI 中间件后,
异常正常冒泡给 ``ExceptionMiddleware``,被全局 handler 接住翻译成
``INTERNAL_ERROR`` 包络。

P1 不实现:``client_ip`` / ``user_agent`` 字段。OBSERVABILITY §2.5 写"用户 IP
/ 浏览器 UA → 保留前两段后段脱敏",但 UA 不是分段结构,语义存疑;P1 默认绑
``127.0.0.1`` 本机调试无脱敏需求,P3+ 公网模式再实现 §2.5 含义并明确脱敏规则。

P4-B-04 ``ApiMetricsMiddleware``
================================

新增 ``ApiMetricsMiddleware`` 在 AccessLog 基础上写 ``system_metrics_daily``
表:每个请求结束时调 ``record_api_request_safely`` 增量 upsert 当日跨工作
区聚合行(api_requests / api_errors / avg_api_latency_ms 三字段)。session
工厂可注入 ``None`` 关闭(测试 / P1 兼容);默认绑 None,生产 ``create_app``
显式注入 system db 工厂。
"""

from __future__ import annotations

import json
import secrets
import time
from collections import deque
from collections.abc import Callable
from typing import Any

import structlog
from sqlalchemy.orm import Session
from starlette.types import ASGIApp, Message, Receive, Scope, Send

from src.api.websocket.channels import system_alerts_channel
from src.api.websocket.manager import WebSocketManager
from src.services.system_metrics_writer import record_api_request_safely

_log = structlog.get_logger(__name__)

SystemSessionFactory = Callable[[], Session]
"""零参 system db 工厂签名(对齐 ``system_metrics_writer`` 模块)。"""


def _generate_request_id() -> str:
    """生成 ``req_<random12>``(OBSERVABILITY §2.3)。

    用 ``secrets.token_hex(6)`` 输出 12 个十六进制字符,长度恒定且 URL 安全。
    """

    return f"req_{secrets.token_hex(6)}"


def _read_header(scope: Scope, name: bytes) -> str | None:
    """从 ASGI scope 中读单个请求头(case-insensitive)。"""

    for key, value in scope.get("headers", []):
        if key.lower() == name:
            return value.decode("latin-1")
    return None


class RequestIDMiddleware:
    """为每个 HTTP 请求生成 ``request_id`` 并注入 ``scope["state"]``。

    优先复用客户端传入的 ``X-Request-ID`` 头(便于跨进程串联),不存在时新生成。
    响应头始终回显 ``X-Request-ID``。

    挂载顺序:``add_middleware`` 时**最后注册**(放最外层),保证下游中间件与
    路由能从 ``request.state.request_id`` / ``scope["state"]["request_id"]`` 读到。
    """

    def __init__(self, app: ASGIApp) -> None:
        self.app = app

    async def __call__(self, scope: Scope, receive: Receive, send: Send) -> None:
        if scope["type"] != "http":
            await self.app(scope, receive, send)
            return

        incoming = _read_header(scope, b"x-request-id")
        request_id = incoming if incoming else _generate_request_id()

        # Starlette ``Request.state`` 读 ``scope["state"]`` dict,这里写进去后路由
        # 可以直接 ``request.state.request_id``。
        scope.setdefault("state", {})["request_id"] = request_id

        # 绑到 structlog contextvars,本次请求处理过程中所有日志自动带该字段。
        structlog.contextvars.bind_contextvars(request_id=request_id)

        async def send_wrapper(message: Message) -> None:
            if message["type"] == "http.response.start":
                headers = list(message.get("headers", []))
                headers.append((b"x-request-id", request_id.encode("latin-1")))
                message["headers"] = headers
            await send(message)

        try:
            await self.app(scope, receive, send_wrapper)
        finally:
            structlog.contextvars.clear_contextvars()


class AccessLogMiddleware:
    """请求结束后落一条 ``api_request`` 日志(OBSERVABILITY §2.4)。

    字段:``method`` / ``path`` / ``status_code`` / ``latency_ms`` / ``request_id``。
    P1 不记 ``user_agent`` / ``client_ip``(见模块 docstring)。

    错误响应(>=400)走 ``WARNING``,成功走 ``INFO``。``api_error`` 事件由
    ``errors.py`` 的全局 handler 在异常翻译时单独发,本中间件不重复发。
    """

    def __init__(self, app: ASGIApp) -> None:
        self.app = app

    async def __call__(self, scope: Scope, receive: Receive, send: Send) -> None:
        if scope["type"] != "http":
            await self.app(scope, receive, send)
            return

        start = time.perf_counter()
        # 用 dict 包一层好让闭包写值;status_code 默认 500 兜底"未发出 response.start"
        # 的极端场景。
        captured: dict[str, Any] = {"status_code": 500}

        async def send_wrapper(message: Message) -> None:
            if message["type"] == "http.response.start":
                captured["status_code"] = message["status"]
            await send(message)

        try:
            await self.app(scope, receive, send_wrapper)
        finally:
            latency_ms = round((time.perf_counter() - start) * 1000, 2)
            status_code = captured["status_code"]
            request_id = scope.get("state", {}).get("request_id")
            log_method = _log.warning if status_code >= 400 else _log.info
            log_method(
                "api_request",
                method=scope.get("method", ""),
                path=scope.get("path", ""),
                status_code=status_code,
                latency_ms=latency_ms,
                request_id=request_id,
            )


class ApiMetricsMiddleware:
    """请求结束后 upsert ``system_metrics_daily.<today>`` 行(P4-B-04)。

    与 ``AccessLogMiddleware`` 解耦的独立中间件:前者落 structlog 事件(本机
    日志线),后者落 SQL 持久化(供 ``GET /api/v1/system/metrics`` 端点查询)。
    解耦的好处:① 测试可单独关 metrics 持久化避免文件 IO 噪音;② 路径 panic
    时只影响日志,不连带影响 metrics;③ 反之亦然。

    ``session_factory`` 注入:零参工厂返回绑定到系统级 SQLite db 的 Session
    (``Base.metadata.create_all`` idempotent 建表,与工作区 db 隔离);
    ``None`` → no-op(测试默认 / P1 兼容)。
    """

    def __init__(
        self,
        app: ASGIApp,
        *,
        session_factory: SystemSessionFactory | None = None,
    ) -> None:
        self.app = app
        self._session_factory = session_factory

    async def __call__(self, scope: Scope, receive: Receive, send: Send) -> None:
        if scope["type"] != "http":
            await self.app(scope, receive, send)
            return

        start = time.perf_counter()
        captured: dict[str, Any] = {"status_code": 500}

        async def send_wrapper(message: Message) -> None:
            if message["type"] == "http.response.start":
                captured["status_code"] = message["status"]
            await send(message)

        try:
            await self.app(scope, receive, send_wrapper)
        finally:
            latency_ms = round((time.perf_counter() - start) * 1000, 2)
            status_code = captured["status_code"]
            record_api_request_safely(
                self._session_factory,
                status_code=status_code,
                latency_ms=latency_ms,
            )


# =============================================================================
# P4-B-10:API 限流(滑窗 / 每 workspace 100 req/min 默认)
# =============================================================================


DEFAULT_RATE_LIMIT_PER_MINUTE: int = 100
"""默认每 workspace 每分钟限流上限(plan §P4-B-10 字面 100 req/min)。"""

DEFAULT_RATE_WINDOW_SECONDS: float = 60.0
"""默认限流时间窗口(秒)。1 分钟 = 60 秒,与 ``DEFAULT_RATE_LIMIT_PER_MINUTE``
配对。P5+ 可考虑 burst + sustained 双窗设计。"""

ANONYMOUS_WORKSPACE_KEY: str = "__anonymous__"
"""没有 ``X-Workspace-Id`` header 的请求归到此 key 限流。

跨工作区匿名请求(健康检查 / OpenAPI / WebSocket upgrade 等)走同一桶,
防被单个未挂工作区 header 的脚本打满共享池。
"""


# 限流跳过的 path prefix(健康检查 / docs 探针不算入限流配额)。这些端
# 点的访问频率可能远超 100 req/min(浏览器 polling + 部署探针),不应被限。
_RATE_LIMIT_SKIP_PREFIXES: tuple[str, ...] = (
    "/api/v1/health",
    "/api/v1/system/health",
    "/docs",
    "/openapi.json",
    "/api/v1/openapi.json",
    "/redoc",
)


class RateLimitMiddleware:
    """按 workspace 的滑窗限流(P4-B-10)。

    实现:每 workspace 维护一个 ``deque[float]`` 时间戳队列,新请求来时:
    ① 弹出窗口外(now - window > t)所有时间戳;② 若剩余长度 >= 上限,返
    429 + WS publish ``rate_limit_exceeded`` 到 ``system.alerts`` 频道;③
    否则 append 当前时间戳放行。

    **挂载位置**(``main.py``):``RateLimitMiddleware`` 在
    ``ApiMetricsMiddleware`` 内层 + ``ApiErrorTranslationMiddleware`` 外
    层 — 即 ``add_middleware`` 顺序:Translation → RateLimit → ApiMetrics
    → AccessLog → RequestID → CORS。429 响应仍被 ``ApiMetricsMiddleware``
    看到 + 计入 ``system_metrics_daily.api_errors``(对齐启动指令注 ⑧
    "429 响应也算入 system_metrics_daily.api_errors")。

    **跳过的端点**:健康检查 / OpenAPI / docs 不算入配额(避免部署探针
    + 浏览器 polling 打满共享桶)。

    **WS publish 失败 swallow**:对齐 P4-B-08 ``cost_guard_notify_failed``
    模式,WS publish 异常 swallow + structlog ``rate_limit_notify_failed``
    事件;限流判定本身的 429 响应不受 WS 失败影响。

    **workspace key 解析**:优先 ``X-Workspace-Id`` header → URL path
    匹配 ``/api/v1/workspaces/{id}/...`` → fallback ``ANONYMOUS_WORKSPACE_KEY``。
    URL path 匹配让 P4-B-04 metrics / P4-B-09 memory_health / P4-B-10
    style_audit 路由(都走 path 而非 header)也能享受 per-workspace
    限流粒度。
    """

    def __init__(
        self,
        app: ASGIApp,
        *,
        limit_per_minute: int = DEFAULT_RATE_LIMIT_PER_MINUTE,
        window_seconds: float = DEFAULT_RATE_WINDOW_SECONDS,
        ws_manager: WebSocketManager | None = None,
        now: Callable[[], float] | None = None,
    ) -> None:
        self.app = app
        self._limit = limit_per_minute
        self._window = window_seconds
        self._ws_manager = ws_manager
        self._now = now if now is not None else time.monotonic
        # 每 workspace 一个时间戳 deque。dict 顺序无关,**无 LRU 淘汰**——
        # 单机本地部署,workspace 数量受限于磁盘目录(几十-几百级),内存
        # 占用可忽略(每 deque 最多 ``limit`` 个 float)。
        self._timestamps: dict[str, deque[float]] = {}

    async def __call__(self, scope: Scope, receive: Receive, send: Send) -> None:
        if scope["type"] != "http":
            await self.app(scope, receive, send)
            return

        path = scope.get("path", "")
        if any(path.startswith(p) for p in _RATE_LIMIT_SKIP_PREFIXES):
            await self.app(scope, receive, send)
            return

        workspace_key = _resolve_workspace_key(scope, path=path)
        method = scope.get("method", "")
        now = self._now()
        bucket = self._timestamps.setdefault(workspace_key, deque())

        # 1. 弹出窗口外的旧时间戳。
        threshold = now - self._window
        while bucket and bucket[0] < threshold:
            bucket.popleft()

        # 2. 超出上限 → 429 + WS publish + structlog。
        if len(bucket) >= self._limit:
            await self._reject(
                scope, send, workspace_key=workspace_key, method=method, path=path
            )
            return

        # 3. 放行 + 记当前时间戳。
        bucket.append(now)
        await self.app(scope, receive, send)

    async def _reject(
        self,
        scope: Scope,
        send: Send,
        *,
        workspace_key: str,
        method: str,
        path: str,
    ) -> None:
        """返 429 + WS publish ``rate_limit_exceeded`` 到 ``system.alerts``。

        响应包络对齐 API_CONTRACT §3.2 错误格式(``error.code`` /
        ``error.message`` / ``error.request_id``)— 直接 ASGI 写 JSON,不
        走 FastAPI 路由翻译(中间件层无 FastAPI 上下文)。
        """
        request_id = scope.get("state", {}).get("request_id")
        _log.warning(
            "rate_limit_exceeded",
            workspace_id=(
                None if workspace_key == ANONYMOUS_WORKSPACE_KEY else workspace_key
            ),
            method=method,
            path=path,
            limit_per_minute=self._limit,
            window_seconds=self._window,
            request_id=request_id,
        )
        # WS publish 失败 swallow,对齐 P4-B-08 ``cost_guard_notify_failed``。
        if self._ws_manager is not None:
            try:
                self._ws_manager.publish(
                    channel=system_alerts_channel(),
                    event="rate_limit_exceeded",
                    data={
                        "workspace_id": (
                            None
                            if workspace_key == ANONYMOUS_WORKSPACE_KEY
                            else workspace_key
                        ),
                        "limit_per_minute": self._limit,
                        "window_seconds": self._window,
                        "method": method,
                        "path": path,
                    },
                )
            except Exception as exc:  # noqa: BLE001 — swallow per P4-B-08 先例
                _log.error(
                    "rate_limit_notify_failed",
                    stage="ws_publish",
                    error_type=type(exc).__name__,
                    error_message=str(exc),
                )

        body = json.dumps(
            {
                "error": {
                    "code": "RATE_LIMIT_EXCEEDED",
                    "message": (
                        f"请求频率超出限制({self._limit} req/{int(self._window)}s),"
                        " 请稍后重试"
                    ),
                    "details": {
                        "workspace_id": (
                            None
                            if workspace_key == ANONYMOUS_WORKSPACE_KEY
                            else workspace_key
                        ),
                        "limit_per_minute": self._limit,
                        "window_seconds": self._window,
                    },
                    "request_id": request_id,
                },
            },
            ensure_ascii=False,
        ).encode("utf-8")

        await send(
            {
                "type": "http.response.start",
                "status": 429,
                "headers": [
                    (b"content-type", b"application/json; charset=utf-8"),
                    (b"content-length", str(len(body)).encode("latin-1")),
                ],
            }
        )
        await send({"type": "http.response.body", "body": body})


def _resolve_workspace_key(scope: Scope, *, path: str) -> str:
    """从 scope 解析 workspace 限流 key。

    顺序:① ``X-Workspace-Id`` header(章节路由约定);② URL path 匹配
    ``/api/v1/workspaces/{id}/...``(metrics / memory_health / style_audit
    等子资源路由);③ ``ANONYMOUS_WORKSPACE_KEY``(其他端点)。
    """
    header_value = _read_header(scope, b"x-workspace-id")
    if header_value:
        return header_value

    prefix = "/api/v1/workspaces/"
    if path.startswith(prefix):
        rest = path[len(prefix):]
        # 取第一段(``ws-abc/memory/check`` → ``ws-abc``)。
        slash_idx = rest.find("/")
        candidate = rest if slash_idx == -1 else rest[:slash_idx]
        if candidate:
            return candidate

    return ANONYMOUS_WORKSPACE_KEY
