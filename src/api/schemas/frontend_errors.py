"""前端错误上报请求 / 响应 schema(P4-B-10)。

对齐 OBSERVABILITY §8 字面 6 字段(error_type / message / stack 前 1000 字 /
url / user_action / browser)+ §2.4 字面 ``frontend_error`` 事件 + API_CONTRACT
§3.1 响应包络。

设计要点
========

- **全局 system 资源,不读 X-Workspace-Id header**(本任务启动指令注 ⑦):
  ``workspace_id`` 字段化在请求 body 里,允许 ``None`` 表"未登录或全局错
  误"(P4-F-06 全局 ErrorBoundary + window.onerror 上报场景未必有
  workspace 上下文)。
- **``stack`` 字段服务端截断至 1000 字符**(OBSERVABILITY §8 字面),前端可
  传任意长度,后端 ``model_validator`` 截断。
- **响应空体**:落事件 + 计数累加为副作用,响应仅 ``status_code=200`` +
  空 data,前端 swallow 失败不重试(避免上报失败循环;对齐 P4-F-06
  reporter 设计)。
"""

from __future__ import annotations

from pydantic import BaseModel, ConfigDict, Field, field_validator

_STACK_TRUNCATE_LENGTH: int = 1000
"""OBSERVABILITY §8 字面"stack(前 1000 字)" — 服务端兜底截断。"""


class FrontendErrorReport(BaseModel):
    """前端错误上报请求体(OBSERVABILITY §8 字段)。"""

    model_config = ConfigDict(extra="forbid")

    error_type: str = Field(
        ...,
        min_length=1,
        max_length=200,
        description="错误类型(如 'TypeError' / 'NetworkError' / 'UnhandledRejection')",
    )
    message: str = Field(
        ...,
        min_length=1,
        max_length=2000,
        description="错误消息(error.message)",
    )
    stack: str | None = Field(
        None,
        description="错误堆栈;服务端兜底截断前 1000 字符(§8 字面)",
    )
    url: str | None = Field(
        None,
        max_length=2000,
        description="发生错误的页面 URL(window.location.href)",
    )
    user_action: str | None = Field(
        None,
        max_length=500,
        description="用户最近动作 / 操作上下文(可选,前端自定义记录)",
    )
    browser: str | None = Field(
        None,
        max_length=500,
        description="浏览器标识(navigator.userAgent 简版 / 自定义)",
    )
    workspace_id: str | None = Field(
        None,
        description=(
            "当前工作区 ID(可选,全局 ErrorBoundary 未必有工作区上下文;"
            "本端点不读 X-Workspace-Id header,字段化 workspace_id 在 body 里)"
        ),
    )

    @field_validator("stack")
    @classmethod
    def _truncate_stack(cls, value: str | None) -> str | None:
        """前 1000 字符截断(OBSERVABILITY §8 字面)。"""
        if value is None:
            return None
        return value[:_STACK_TRUNCATE_LENGTH]


__all__ = ["FrontendErrorReport"]
