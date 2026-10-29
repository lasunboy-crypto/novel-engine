"""三个真实 provider client 的物理 attempt 观察点回归。"""

from __future__ import annotations

from collections.abc import Callable

import pytest

from src.core.llm import claude_client, ollama_client, openai_compatible_client
from src.core.llm.attempt_context import (
    ProviderAttemptOutcome,
    bind_provider_attempt_observer,
    run_observed_provider_attempt,
)


class _RecordingObserver:
    def __init__(self, *, block: bool = False) -> None:
        self.block = block
        self.begun: list[int] = []
        self.finished: list[tuple[str, bool, str | None]] = []

    def begin(self, *, technical_attempt: int) -> str:
        self.begun.append(technical_attempt)
        if self.block:
            raise RuntimeError("hard cap")
        return f"attempt-{technical_attempt}"

    def finish(
        self,
        attempt_id: str,
        *,
        succeeded: bool,
        error_type: str | None,
        outcome: ProviderAttemptOutcome | None = None,
    ) -> None:
        del outcome
        self.finished.append((attempt_id, succeeded, error_type))


@pytest.mark.parametrize(
    "retry",
    (
        claude_client._retry,
        ollama_client._retry,
        openai_compatible_client._retry,
    ),
)
def test_each_real_client_retry_loop_observes_successful_physical_call(
    retry: Callable[[Callable[[], str]], str],
) -> None:
    observer = _RecordingObserver()
    network_calls = 0

    def _network_call() -> str:
        nonlocal network_calls
        network_calls += 1
        return "ok"

    with bind_provider_attempt_observer(observer):
        assert retry(_network_call) == "ok"

    assert network_calls == 1
    assert observer.begun == [1]
    assert observer.finished == [("attempt-1", True, None)]


@pytest.mark.parametrize(
    "retry",
    (
        claude_client._retry,
        ollama_client._retry,
        openai_compatible_client._retry,
    ),
)
def test_each_real_client_hard_gate_runs_before_network(
    retry: Callable[[Callable[[], str]], str],
) -> None:
    observer = _RecordingObserver(block=True)
    network_calls = 0

    def _network_call() -> str:
        nonlocal network_calls
        network_calls += 1
        return "should-not-run"

    with bind_provider_attempt_observer(observer):
        with pytest.raises(RuntimeError, match="hard cap"):
            retry(_network_call)

    assert network_calls == 0
    assert observer.begun == [1]
    assert observer.finished == []


def test_keyboard_interrupt_finishes_attempt_before_reraise() -> None:
    observer = _RecordingObserver()

    def _interrupted() -> None:
        raise KeyboardInterrupt

    with bind_provider_attempt_observer(observer):
        with pytest.raises(KeyboardInterrupt):
            run_observed_provider_attempt(_interrupted, technical_attempt=1)

    assert observer.begun == [1]
    assert observer.finished == [("attempt-1", False, "KeyboardInterrupt")]
