"""LLM cancel signal 单测(v1.1 wave 4 session 6 = J1)。

覆盖:
- ``src/core/llm/cancel.py``:contextvar 绑定 / ``raise_if_cancelled`` / ``is_cancelled``
  / ``LLMCancelledError`` 是 ``BaseException`` 子类(穿透 ``except Exception``)。
- ``MockLLMClient``:绑定已 set 的 cancel Event → ``complete`` 抛 ``LLMCancelledError``。
- ``OpenAICompatibleClient``:流式 chunk 间查取消 → 中断流、不返完整 ``LLMResponse``。
"""

from __future__ import annotations

import threading
from typing import Any

import httpx
import pytest
from pydantic import SecretStr

import src.core.llm.openai_compatible_client as occ
from src.core.llm.base import Message
from src.core.llm.cancel import (
    LLMCancelledError,
    bound_cancel_event,
    current_cancel_event,
    is_cancelled,
    raise_if_cancelled,
)
from src.core.llm.mock_client import MockLLMClient


# ---- cancel 模块 ------------------------------------------------------------


class TestCancelModule:
    def test_unbound_context_is_not_cancelled(self) -> None:
        """未绑定 cancel Event → is_cancelled False / raise_if_cancelled 静默放过。"""
        assert current_cancel_event() is None
        assert is_cancelled() is False
        raise_if_cancelled()  # 不抛

    def test_bound_unset_event_not_cancelled(self) -> None:
        event = threading.Event()
        with bound_cancel_event(event):
            assert current_cancel_event() is event
            assert is_cancelled() is False
            raise_if_cancelled()  # 未 set → 不抛

    def test_bound_set_event_raises(self) -> None:
        event = threading.Event()
        event.set()
        with bound_cancel_event(event):
            assert is_cancelled() is True
            with pytest.raises(LLMCancelledError):
                raise_if_cancelled()

    def test_context_reset_on_exit(self) -> None:
        """退出 with 块后 contextvar reset(防线程池复用泄漏)。"""
        event = threading.Event()
        with bound_cancel_event(event):
            assert current_cancel_event() is event
        assert current_cancel_event() is None

    def test_cancelled_error_is_baseexception_not_exception(self) -> None:
        """LLMCancelledError 继承 BaseException 而非 Exception(穿透 except Exception)。"""
        assert issubclass(LLMCancelledError, BaseException)
        assert not issubclass(LLMCancelledError, Exception)

    def test_cancel_propagates_through_except_exception(self) -> None:
        """取消异常不被 ``except Exception`` 捕获(模拟 orchestrator fail-open swallow)。"""
        event = threading.Event()
        event.set()

        def fail_open_wrapper() -> str:
            try:
                with bound_cancel_event(event):
                    raise_if_cancelled()
                return "not-cancelled"
            except Exception:  # noqa: BLE001 — 模拟热路径 fail-open swallow
                return "swallowed"

        # except Exception 不该吞掉 BaseException 子类 → 异常穿透到这里
        with pytest.raises(LLMCancelledError):
            fail_open_wrapper()


# ---- MockLLMClient ----------------------------------------------------------


class TestMockClientCancel:
    def test_complete_raises_when_cancelled(self) -> None:
        event = threading.Event()
        event.set()
        client = MockLLMClient()
        messages = [Message(role="user", content="任意内容 [AGENT:writer]")]
        with bound_cancel_event(event):
            with pytest.raises(LLMCancelledError):
                client.complete(messages)

    def test_complete_normal_when_not_cancelled(self) -> None:
        """未取消时 mock 正常返回(确认 cancel 检查不误伤正常路径)。"""
        client = MockLLMClient()
        messages = [
            Message(role="user", content="写一段。\n<!-- agent_role: writer -->")
        ]
        with bound_cancel_event(threading.Event()):  # 未 set
            resp = client.complete(messages)
        assert resp.content


# ---- OpenAICompatibleClient(真流式 chunk 间中断)----------------------------

_DEFAULT_BASE_URL = "https://example.test/v1"


def _build_client(handler: Any) -> occ.OpenAICompatibleClient:
    transport = httpx.MockTransport(handler)
    http = httpx.Client(transport=transport)
    return occ.OpenAICompatibleClient(
        model="qwen3.7-max",
        base_url=_DEFAULT_BASE_URL,
        api_key=SecretStr("sk-test"),
        enable_thinking=False,
        http_client=http,
    )


def _sse_response(payload_lines: list[str]) -> httpx.Response:
    full = list(payload_lines) + ["", "data: [DONE]", ""]
    content = ("\n".join(full) + "\n").encode("utf-8")
    return httpx.Response(
        200, content=content, headers={"content-type": "text/event-stream"}
    )


class TestOpenAIClientCancel:
    def test_stream_interrupted_when_cancelled_preset(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        """绑定已 set 的 cancel Event → 第一个 chunk 间检查即抛,不返完整响应。"""
        monkeypatch.setattr(occ.time, "sleep", lambda _s: None)

        def handler(_req: httpx.Request) -> httpx.Response:
            import json

            line = "data: " + json.dumps(
                {"model": "qwen3.7-max", "choices": [{"delta": {"content": "片段"}}]}
            )
            return _sse_response([line, line, line])

        client = _build_client(handler)
        event = threading.Event()
        event.set()
        messages = [Message(role="user", content="写。 [AGENT:writer]")]
        with bound_cancel_event(event):
            with pytest.raises(LLMCancelledError):
                client.complete(messages)

    def test_stream_completes_when_not_cancelled(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        monkeypatch.setattr(occ.time, "sleep", lambda _s: None)

        def handler(_req: httpx.Request) -> httpx.Response:
            import json

            content_line = "data: " + json.dumps(
                {
                    "model": "qwen3.7-max",
                    "choices": [{"delta": {"content": "完整"}, "finish_reason": "stop"}],
                }
            )
            usage_line = "data: " + json.dumps(
                {
                    "model": "qwen3.7-max",
                    "choices": [],
                    "usage": {
                        "prompt_tokens": 3,
                        "completion_tokens": 2,
                        "total_tokens": 5,
                    },
                }
            )
            return _sse_response([content_line, usage_line])

        client = _build_client(handler)
        messages = [Message(role="user", content="写。 [AGENT:writer]")]
        with bound_cancel_event(threading.Event()):  # 未 set
            resp = client.complete(messages)
        assert resp.content == "完整"
