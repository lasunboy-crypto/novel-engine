"""P3-B-03:OllamaClient 单测(完全 mock httpx)。

不真接 ollama HTTP。用 ``httpx.MockTransport`` 注入伪造响应,验证:
- 请求体正确(``think:false`` 默认 + ``stream:false`` + ``options`` dict)
- ``enable_thinking=True`` 切换生效
- 响应解析(``message.content`` / ``prompt_eval_count`` / ``eval_count``)
- 4xx 不重试 / 5xx + 网络错误重试 / 重试耗尽抛最后一次

集成测(真 ollama 端到端)在 ``tests/integration/test_ollama_client.py``,
默认被 ``-m 'not integration'`` 排除,需要本机起 ollama serve + 创建
``qwen-novel`` tag 才能跑。
"""

from __future__ import annotations

import json
from typing import Any

import httpx
import pytest

from src.core.llm import ollama_client as oc
from src.core.llm.base import Message


def _fake_ollama_response(
    *,
    content: str = "hello",
    model: str = "qwen-novel:latest",
    prompt_eval_count: int = 12,
    eval_count: int = 8,
    done: bool = True,
    done_reason: str | None = "stop",
) -> dict[str, Any]:
    """伪造 ollama ``/api/chat`` 非流式响应体,只填本类用到的字段。"""
    payload: dict[str, Any] = {
        "model": model,
        "created_at": "2026-05-09T00:00:00Z",
        "message": {"role": "assistant", "content": content},
        "done": done,
        "prompt_eval_count": prompt_eval_count,
        "eval_count": eval_count,
        "total_duration": 1_000_000,
    }
    if done_reason is not None:
        payload["done_reason"] = done_reason
    return payload


def _build_client_with_handler(
    handler,
    *,
    model: str = "qwen-novel",
    base_url: str = "http://localhost:11434",
    enable_thinking: bool = False,
) -> oc.OllamaClient:
    """构造 OllamaClient + 注入 ``httpx.MockTransport`` 跑 handler。"""
    transport = httpx.MockTransport(handler)
    http = httpx.Client(transport=transport)
    return oc.OllamaClient(
        model=model,
        base_url=base_url,
        enable_thinking=enable_thinking,
        http_client=http,
    )


@pytest.fixture(autouse=True)
def _no_sleep(monkeypatch: pytest.MonkeyPatch) -> None:
    """重试退避不真睡眠,加速测试。"""
    monkeypatch.setattr(oc.time, "sleep", lambda _seconds: None)


# ---- TestConstructor --------------------------------------------------------


class TestConstructor:
    def test_default_base_url_strips_trailing_slash(self) -> None:
        client = _build_client_with_handler(
            lambda req: httpx.Response(200, json=_fake_ollama_response()),
            base_url="http://localhost:11434/",
        )
        assert client._base_url == "http://localhost:11434"

    def test_default_enable_thinking_is_false(self) -> None:
        client = _build_client_with_handler(
            lambda req: httpx.Response(200, json=_fake_ollama_response()),
        )
        assert client._default_enable_thinking is False

    def test_external_http_client_not_owned(self) -> None:
        transport = httpx.MockTransport(
            lambda req: httpx.Response(200, json=_fake_ollama_response())
        )
        http = httpx.Client(transport=transport)
        client = oc.OllamaClient(model="qwen-novel", http_client=http)
        assert client._owns_http is False
        client.close()  # 不应关闭外部托管的 http
        # 验证 http 仍可用:发个请求看不抛
        r = http.post("http://localhost:11434/api/chat", json={})
        assert r.status_code == 200

    def test_internal_http_client_owned(self) -> None:
        client = oc.OllamaClient(model="qwen-novel")
        assert client._owns_http is True
        client.close()


# ---- TestRequestBody --------------------------------------------------------


class TestRequestBody:
    def test_default_request_body_has_think_false_stream_false(self) -> None:
        captured: dict[str, Any] = {}

        def handler(request: httpx.Request) -> httpx.Response:
            captured["url"] = str(request.url)
            captured["body"] = json.loads(request.content)
            return httpx.Response(200, json=_fake_ollama_response())

        client = _build_client_with_handler(handler)
        client.complete([Message(role="user", content="ping")])

        assert captured["url"] == "http://localhost:11434/api/chat"
        body = captured["body"]
        assert body["model"] == "qwen-novel"
        assert body["stream"] is False
        assert body["think"] is False
        assert body["messages"] == [{"role": "user", "content": "ping"}]
        # 没传 gen_params 时不应有 options 字段(避免请求体污染)
        assert "options" not in body

    def test_caller_enable_thinking_true_overrides_default(self) -> None:
        captured: dict[str, Any] = {}

        def handler(request: httpx.Request) -> httpx.Response:
            captured["body"] = json.loads(request.content)
            return httpx.Response(200, json=_fake_ollama_response())

        client = _build_client_with_handler(handler)  # 默认 enable_thinking=False
        client.complete(
            [Message(role="user", content="ping")],
            enable_thinking=True,
        )

        assert captured["body"]["think"] is True

    def test_init_enable_thinking_true_default(self) -> None:
        captured: dict[str, Any] = {}

        def handler(request: httpx.Request) -> httpx.Response:
            captured["body"] = json.loads(request.content)
            return httpx.Response(200, json=_fake_ollama_response())

        client = _build_client_with_handler(handler, enable_thinking=True)
        client.complete([Message(role="user", content="ping")])

        assert captured["body"]["think"] is True

    def test_caller_enable_thinking_false_overrides_init_true(self) -> None:
        captured: dict[str, Any] = {}

        def handler(request: httpx.Request) -> httpx.Response:
            captured["body"] = json.loads(request.content)
            return httpx.Response(200, json=_fake_ollama_response())

        client = _build_client_with_handler(handler, enable_thinking=True)
        client.complete(
            [Message(role="user", content="ping")],
            enable_thinking=False,
        )

        assert captured["body"]["think"] is False

    def test_caller_overrides_model_per_call(self) -> None:
        captured: dict[str, Any] = {}

        def handler(request: httpx.Request) -> httpx.Response:
            captured["body"] = json.loads(request.content)
            return httpx.Response(200, json=_fake_ollama_response())

        client = _build_client_with_handler(handler)
        client.complete(
            [Message(role="user", content="ping")],
            model="qwen-novel:35b",
        )

        assert captured["body"]["model"] == "qwen-novel:35b"

    def test_gen_params_go_to_options_dict(self) -> None:
        captured: dict[str, Any] = {}

        def handler(request: httpx.Request) -> httpx.Response:
            captured["body"] = json.loads(request.content)
            return httpx.Response(200, json=_fake_ollama_response())

        client = _build_client_with_handler(handler)
        client.complete(
            [Message(role="user", content="ping")],
            temperature=0.7,
            num_predict=4096,
            top_p=0.9,
        )

        assert captured["body"]["options"] == {
            "temperature": 0.7,
            "num_predict": 4096,
            "top_p": 0.9,
        }

    def test_system_role_passed_through_directly(self) -> None:
        """ollama 原生 ``/api/chat`` 支持 ``role:system``,不需要像 anthropic 单独 pop。"""
        captured: dict[str, Any] = {}

        def handler(request: httpx.Request) -> httpx.Response:
            captured["body"] = json.loads(request.content)
            return httpx.Response(200, json=_fake_ollama_response())

        client = _build_client_with_handler(handler)
        client.complete(
            [
                Message(role="system", content="you are a writer"),
                Message(role="user", content="write a chapter"),
            ]
        )

        assert captured["body"]["messages"] == [
            {"role": "system", "content": "you are a writer"},
            {"role": "user", "content": "write a chapter"},
        ]


# ---- TestResponseParsing ----------------------------------------------------


class TestResponseParsing:
    def test_returns_llm_response_with_content_and_token_counts(self) -> None:
        client = _build_client_with_handler(
            lambda req: httpx.Response(
                200,
                json=_fake_ollama_response(
                    content="hello world",
                    model="qwen-novel:latest",
                    prompt_eval_count=15,
                    eval_count=20,
                ),
            ),
        )
        resp = client.complete([Message(role="user", content="ping")])

        assert resp.content == "hello world"
        assert resp.model_used == "qwen-novel:latest"
        assert resp.usage.input_tokens == 15
        assert resp.usage.output_tokens == 20
        assert resp.usage.total_tokens == 35
        assert resp.finish_reason == "stop"

    def test_done_reason_length_passed_through(self) -> None:
        client = _build_client_with_handler(
            lambda req: httpx.Response(
                200, json=_fake_ollama_response(done_reason="length")
            ),
        )
        resp = client.complete([Message(role="user", content="ping")])
        assert resp.finish_reason == "length"

    def test_legacy_response_without_done_reason_falls_back_to_stop(self) -> None:
        """旧版 ollama 仅 ``done:true``,无 ``done_reason``;回退 ``stop``。"""
        client = _build_client_with_handler(
            lambda req: httpx.Response(
                200,
                json=_fake_ollama_response(done=True, done_reason=None),
            ),
        )
        resp = client.complete([Message(role="user", content="ping")])
        assert resp.finish_reason == "stop"

    def test_done_false_yields_none_finish_reason(self) -> None:
        client = _build_client_with_handler(
            lambda req: httpx.Response(
                200,
                json=_fake_ollama_response(done=False, done_reason=None),
            ),
        )
        resp = client.complete([Message(role="user", content="ping")])
        assert resp.finish_reason is None

    def test_missing_token_counts_default_to_zero(self) -> None:
        """ollama 极少数情况下不返 token 计数;不应崩,默认 0。"""

        def handler(request: httpx.Request) -> httpx.Response:
            return httpx.Response(
                200,
                json={
                    "model": "qwen-novel",
                    "message": {"role": "assistant", "content": "x"},
                    "done": True,
                },
            )

        client = _build_client_with_handler(handler)
        resp = client.complete([Message(role="user", content="ping")])
        assert resp.usage.input_tokens == 0
        assert resp.usage.output_tokens == 0
        assert resp.content == "x"

    def test_response_not_dict_raises_runtime_error(self) -> None:
        client = _build_client_with_handler(
            lambda req: httpx.Response(200, json=["not", "a", "dict"]),
        )
        with pytest.raises(RuntimeError, match="JSON 对象"):
            client.complete([Message(role="user", content="ping")])


# ---- TestRetry --------------------------------------------------------------


class TestIsRetryable:
    def test_connect_error_retryable(self) -> None:
        exc = httpx.ConnectError("conn refused")
        assert oc._is_retryable(exc) is True

    def test_connect_timeout_retryable(self) -> None:
        exc = httpx.ConnectTimeout("timed out")
        assert oc._is_retryable(exc) is True

    def test_read_timeout_retryable(self) -> None:
        exc = httpx.ReadTimeout("read timed out")
        assert oc._is_retryable(exc) is True

    def test_remote_protocol_error_retryable(self) -> None:
        exc = httpx.RemoteProtocolError("server closed conn")
        assert oc._is_retryable(exc) is True

    def test_5xx_status_error_retryable(self) -> None:
        request = httpx.Request("POST", "http://localhost:11434/api/chat")
        response = httpx.Response(503, request=request)
        exc = httpx.HTTPStatusError("503", request=request, response=response)
        assert oc._is_retryable(exc) is True

    def test_4xx_status_error_not_retryable(self) -> None:
        request = httpx.Request("POST", "http://localhost:11434/api/chat")
        response = httpx.Response(404, request=request)
        exc = httpx.HTTPStatusError("404", request=request, response=response)
        assert oc._is_retryable(exc) is False

    def test_unknown_exception_not_retryable(self) -> None:
        assert oc._is_retryable(ValueError("nope")) is False


class TestCompleteRetry:
    def test_retries_5xx_then_succeeds(self) -> None:
        call_count = {"n": 0}

        def handler(request: httpx.Request) -> httpx.Response:
            call_count["n"] += 1
            if call_count["n"] < 3:
                return httpx.Response(503)
            return httpx.Response(200, json=_fake_ollama_response(content="finally"))

        client = _build_client_with_handler(handler)
        resp = client.complete([Message(role="user", content="ping")])

        assert resp.content == "finally"
        assert call_count["n"] == 3

    def test_retries_exhausted_raises_last(self) -> None:
        call_count = {"n": 0}

        def handler(request: httpx.Request) -> httpx.Response:
            call_count["n"] += 1
            return httpx.Response(503)

        client = _build_client_with_handler(handler)
        with pytest.raises(httpx.HTTPStatusError):
            client.complete([Message(role="user", content="ping")])
        assert call_count["n"] == 3  # MAX_ATTEMPTS

    def test_4xx_raises_immediately_no_retry(self) -> None:
        call_count = {"n": 0}

        def handler(request: httpx.Request) -> httpx.Response:
            call_count["n"] += 1
            return httpx.Response(404, json={"error": "model not found"})

        client = _build_client_with_handler(handler)
        with pytest.raises(httpx.HTTPStatusError):
            client.complete([Message(role="user", content="ping")])
        assert call_count["n"] == 1

    def test_connect_error_retried(self) -> None:
        call_count = {"n": 0}

        def handler(request: httpx.Request) -> httpx.Response:
            call_count["n"] += 1
            if call_count["n"] < 2:
                raise httpx.ConnectError("conn refused")
            return httpx.Response(200, json=_fake_ollama_response(content="ok"))

        client = _build_client_with_handler(handler)
        resp = client.complete([Message(role="user", content="ping")])

        assert resp.content == "ok"
        assert call_count["n"] == 2
