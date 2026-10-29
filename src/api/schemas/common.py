"""通用响应 schema(P1-B-17)。

对齐 API_CONTRACT §3.1 / §3.2 字面示例:

- 成功响应:``{ data, meta? }``,**无** ``ok``、**无** ``error`` 字段。
- 错误响应:``{ error: { code, message, details?, trace_id?, request_id } }``,
  **无** ``data`` / ``meta``。

两者刻意拆成独立模型 ``SuccessEnvelope[T]`` 与 ``ErrorEnvelope``,而不是合并成
单一带可选字段的包络。理由:OpenAPI 生成的 TypeScript 类型更精确,客户端可以
通过"是否含 ``error`` 键"判断成败,等价 ``ok`` 字段但与契约文档示例字面一致。

字段命名严格按 ``snake_case``(CLAUDE.md §字段命名约定)。
"""

from __future__ import annotations

from typing import Any, Generic, TypeVar

from pydantic import BaseModel, ConfigDict, Field

T = TypeVar("T")


class Pagination(BaseModel):
    """分页元信息(API_CONTRACT §3.1 / §1 第 7 条)。

    分页接口必须填齐三个字段;非分页接口走 ``MetaPayload`` 不带分页键。
    """

    model_config = ConfigDict(extra="forbid")

    page: int = Field(ge=1, description="当前页号,1-based。")
    page_size: int = Field(ge=1, le=200, description="每页条数。")
    total: int = Field(ge=0, description="总条数(跨页)。")


class MetaPayload(BaseModel):
    """成功响应可选的 ``meta`` 段。

    P1 仅放分页;后续如需通用扩展(server_time、deprecation 提示等)在此追加。
    """

    model_config = ConfigDict(extra="forbid")

    pagination: Pagination | None = None


class SuccessEnvelope(BaseModel, Generic[T]):
    """成功响应包络。

    - ``data``:资源对象或数组,泛型由具体路由 ``response_model`` 实例化。
    - ``meta``:可选,分页时必含。
    """

    model_config = ConfigDict(extra="forbid")

    data: T
    meta: MetaPayload | None = None


class ErrorPayload(BaseModel):
    """错误体(API_CONTRACT §3.2)。

    - ``code``:机器可读错误码,``<DOMAIN>_<REASON>`` 大写下划线
      (CLAUDE.md §命名 & API_CONTRACT §3.3)。
    - ``message``:人类可读中文消息。
    - ``details``:可选,字段级错误等结构化信息。
    - ``trace_id``:可选,章节维度 trace(``{chapter_id}-{operation}{n}``,
      OBSERVABILITY §2.3);非章节路由通常为空。
    - ``request_id``:**必含**,API 维度 trace,排障基线。
    """

    model_config = ConfigDict(extra="forbid")

    code: str
    message: str
    details: dict[str, Any] | None = None
    trace_id: str | None = None
    request_id: str


class ErrorEnvelope(BaseModel):
    """错误响应包络。

    顶层只含 ``error``,不与 ``SuccessEnvelope`` 共用一个模型。
    """

    model_config = ConfigDict(extra="forbid")

    error: ErrorPayload
