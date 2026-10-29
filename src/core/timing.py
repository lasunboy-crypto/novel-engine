"""轻量阶段耗时记录工具。

只依赖 ``time.monotonic`` 与 structlog,用于主流程阶段级观测。它不改变业务
控制流:异常仍按原路径冒泡,计时日志只补充 ``duration_ms`` / ``status`` 等字段。
"""

from __future__ import annotations

from dataclasses import dataclass, field
from time import monotonic
from typing import Any


@dataclass
class StageTimer:
    """上下文管理器:退出时写一条结构化阶段耗时日志。"""

    logger: Any
    event: str
    fields: dict[str, Any] = field(default_factory=dict)
    _started_at: float | None = None
    _finished: bool = False

    def __enter__(self) -> "StageTimer":
        self._started_at = monotonic()
        return self

    def finish(self, *, status: str = "succeeded", **extra: Any) -> int:
        """立即写日志并返回耗时毫秒;重复调用不重复写日志。"""
        if self._finished:
            return 0
        now = monotonic()
        started_at = self._started_at if self._started_at is not None else now
        duration_ms = int((now - started_at) * 1000)
        payload = {
            **self.fields,
            **extra,
            "status": status,
            "duration_ms": duration_ms,
        }
        self.logger.info(self.event, **payload)
        self._finished = True
        return duration_ms

    def __exit__(self, exc_type: Any, exc: BaseException | None, tb: Any) -> bool:
        if self._finished:
            return False
        if exc is None:
            self.finish(status="succeeded")
        else:
            self.finish(
                status="failed",
                error_type=exc_type.__name__ if exc_type is not None else "Exception",
                error_message=str(exc),
            )
        return False


def log_stage_timing(
    logger: Any,
    event: str,
    *,
    status: str = "skipped",
    duration_ms: int = 0,
    **fields: Any,
) -> None:
    """记录无需计时块的阶段状态,例如 disabled/skipped 分支。"""
    logger.info(event, **fields, status=status, duration_ms=duration_ms)
