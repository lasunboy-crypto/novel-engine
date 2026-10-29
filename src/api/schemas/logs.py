"""日志查询 API 表面 schema(P4-B-05)。

对应 OBSERVABILITY §2.6 字面 3 个 GET 端点(``/logs?...`` / ``/logs/trace/{id}``)
+ 1 个 WS 端点(``/logs/stream``)的请求 / 响应形态。

**严格 ``forbid extra``**(对齐 P1-B-17 / P4-B-04 既有契约纪律)。日志条目
本身字段动态(structlog 任意调用方可加 kwargs),通过 ``LogEntryOut.extra``
字典装载未识别字段;模型层固定 8 个核心字段(对齐 OBSERVABILITY §2.2 必含)。
"""

from __future__ import annotations

import datetime as _dt
from typing import Any, Literal

from pydantic import BaseModel, ConfigDict, Field, model_validator


CORE_FIELDS = (
    "timestamp",
    "level",
    "logger",
    "event",
    "trace_id",
    "request_id",
    "workspace_id",
    "chapter_id",
    "module",
    "message",
)
"""OBSERVABILITY §2.2 必含字段集合(``module`` / ``message`` 可选,其余强字段)。"""


class LogEntryOut(BaseModel):
    """单条 jsonl 日志(structlog ``JSONRenderer`` 输出形态)。

    OBSERVABILITY §2.2 必含字段为强字段(``timestamp`` / ``level`` /
    ``logger`` / ``event``);上下文字段(``trace_id`` / ``request_id`` /
    ``workspace_id`` / ``chapter_id``)由 ``bind_chapter_context`` /
    ``RequestIDMiddleware`` 注入,**可空**(后台任务无 request_id;系统级
    事件无 chapter_id)。``module`` / ``message`` 可空。

    ``extra`` 装其余动态字段(每个事件特有的 kwargs,如 ``model_used`` /
    ``score_overall`` / ``cost_usd``);序列化时 forbid extra 由
    ``model_validator`` 在 before 阶段把未识别字段倒入 ``extra`` 实现,
    避免 schema 锁死必收紧。
    """

    model_config = ConfigDict(extra="forbid")

    timestamp: _dt.datetime
    level: str
    logger: str
    event: str
    trace_id: str | None = None
    request_id: str | None = None
    workspace_id: str | None = None
    chapter_id: str | None = None
    module: str | None = None
    message: str | None = None
    extra: dict[str, Any] = Field(default_factory=dict)

    @model_validator(mode="before")
    @classmethod
    def _collect_extra(cls, data: Any) -> Any:
        if not isinstance(data, dict):
            return data
        known = set(CORE_FIELDS) | {"extra"}
        extra: dict[str, Any] = dict(data.get("extra") or {})
        cleaned: dict[str, Any] = {}
        for k, v in data.items():
            if k in known and k != "extra":
                cleaned[k] = v
            elif k == "extra":
                continue
            else:
                extra[k] = v
        cleaned["extra"] = extra
        return cleaned


class LogQueryOut(BaseModel):
    """``GET /logs`` 分页响应 ``data``。"""

    model_config = ConfigDict(extra="forbid")

    items: list[LogEntryOut]
    page: int = Field(..., ge=1, description="当前页(从 1 起)")
    page_size: int = Field(..., ge=1, le=500, description="每页条数")
    total: int = Field(..., ge=0, description="符合筛选条件的总条数")


TraceKind = Literal["trace_id", "request_id"]


class TraceViewOut(BaseModel):
    """``GET /logs/trace/{id}`` 响应 ``data``(对齐 OBSERVABILITY §4.1 / §4.2)。

    路径参数 ``{id}`` 后端按字面规则智能分流:``req_`` 前缀 → 按 ``request_id``
    查;否则按 ``trace_id`` 查。``kind`` 字段告诉客户端实际的分流结果,便于
    前端渲染 trace 视图(章节级 vs API 级)。
    """

    model_config = ConfigDict(extra="forbid")

    kind: TraceKind
    id: str
    items: list[LogEntryOut]
    total: int = Field(..., ge=0)


class StreamSubscribeIn(BaseModel):
    """WS ``/logs/stream`` 客户端订阅消息体。

    保留 schema 用于后续 WS 连接握手扩展(filter 字段);本任务 P4-B-05
    最简实现:WS 接通后 server 推送所有新增日志行(无 filter),客户端
    本地过滤。后续若需服务端过滤,可在客户端首次发送本消息体收紧字段。
    """

    model_config = ConfigDict(extra="forbid")

    workspace_id: str | None = None
    chapter_id: str | None = None
    event: str | None = None
    level: str | None = None
