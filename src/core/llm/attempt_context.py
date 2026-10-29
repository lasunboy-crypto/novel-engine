"""真实 provider 物理调用 attempt 观察上下文。

本模块位于 ``core.llm``，只提供与数据库无关的观察协议。生产装配层可把
持久化 observer 绑定到当前 ``ContextVar``；三个真实 client 的 retry 循环在
每次物理请求前后调用 :func:`run_observed_provider_attempt`。这样 hard cap
发生在网络请求之前，同时 client 不反向依赖 service/SQLAlchemy。
"""

from __future__ import annotations

from collections.abc import Callable, Iterator
from contextlib import contextmanager
from contextvars import ContextVar
from dataclasses import dataclass
from typing import Literal, Protocol, TypeVar


T = TypeVar("T")

ProviderUsageStatus = Literal["known", "unknown"]
ProviderResponseStatus = Literal[
    "completed",
    "failed",
    "stream_incomplete",
    "usage_missing",
]


@dataclass(frozen=True, slots=True)
class ProviderAttemptOutcome:
    """Database-free physical response outcome passed to the ledger observer."""

    usage_status: ProviderUsageStatus
    input_tokens: int | None
    output_tokens: int | None
    provider_request_id: str | None
    response_status: ProviderResponseStatus

    @classmethod
    def known(
        cls,
        *,
        input_tokens: int,
        output_tokens: int,
        provider_request_id: str | None = None,
    ) -> "ProviderAttemptOutcome":
        if input_tokens < 0 or output_tokens < 0:
            raise ValueError("provider usage token 不允许为负数")
        return cls(
            usage_status="known",
            input_tokens=input_tokens,
            output_tokens=output_tokens,
            provider_request_id=_bounded_request_id(provider_request_id),
            response_status="completed",
        )

    @classmethod
    def unknown(
        cls,
        *,
        response_status: ProviderResponseStatus,
        provider_request_id: str | None = None,
    ) -> "ProviderAttemptOutcome":
        return cls(
            usage_status="unknown",
            input_tokens=None,
            output_tokens=None,
            provider_request_id=_bounded_request_id(provider_request_id),
            response_status=response_status,
        )


def _bounded_request_id(value: str | None) -> str | None:
    if not isinstance(value, str):
        return None
    cleaned = value.strip()
    if not cleaned:
        return None
    return cleaned[:256]


class ProviderAttemptObserver(Protocol):
    """物理 provider attempt 的持久化观察接口。"""

    def begin(self, *, technical_attempt: int) -> str:
        """调用前持久化并执行 hard cap，返回 attempt id。"""

    def finish(
        self,
        attempt_id: str,
        *,
        succeeded: bool,
        error_type: str | None,
        outcome: ProviderAttemptOutcome | None = None,
    ) -> None:
        """调用后写入结果；调用前已落的 attempt 永不删除。"""


_OBSERVER: ContextVar[ProviderAttemptObserver | None] = ContextVar(
    "provider_attempt_observer",
    default=None,
)


@contextmanager
def bind_provider_attempt_observer(
    observer: ProviderAttemptObserver,
) -> Iterator[None]:
    """在当前调用链绑定 observer，并在退出时恢复上层上下文。"""

    token = _OBSERVER.set(observer)
    try:
        yield
    finally:
        _OBSERVER.reset(token)


def run_observed_provider_attempt(
    call: Callable[[], T],
    *,
    technical_attempt: int,
    outcome_from_result: Callable[[T], ProviderAttemptOutcome] | None = None,
    outcome_from_error: Callable[[BaseException], ProviderAttemptOutcome]
    | None = None,
) -> T:
    """执行一次物理请求并通知 observer。

    ``observer.begin`` 在 ``call`` 之前执行；其 hard-cap 异常会直接冒泡，确保
    达到上限时零网络调用。没有绑定 observer 的 legacy/unit 路径保持原行为。
    """

    observer = _OBSERVER.get()
    if observer is None:
        return call()

    attempt_id = observer.begin(technical_attempt=technical_attempt)
    try:
        result = call()
    except BaseException as exc:
        # KeyboardInterrupt / cancellation 也必须先闭合 attempt，再原样上抛；
        # 这不是吞掉中断，只是避免留下 R5/activity 的 started 幽灵记录。
        outcome = (
            outcome_from_error(exc)
            if outcome_from_error is not None
            else ProviderAttemptOutcome.unknown(response_status="failed")
        )
        observer.finish(
            attempt_id,
            succeeded=False,
            error_type=type(exc).__name__,
            outcome=outcome,
        )
        raise
    outcome = (
        outcome_from_result(result)
        if outcome_from_result is not None
        else ProviderAttemptOutcome.unknown(response_status="completed")
    )
    observer.finish(
        attempt_id,
        succeeded=True,
        error_type=None,
        outcome=outcome,
    )
    return result
