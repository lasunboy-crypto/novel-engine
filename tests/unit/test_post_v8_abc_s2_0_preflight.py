"""POST-V8 S2-0 candidate protocol preflight (Provider 0 only).

These tests deliberately construct every client with a literal dummy key and an
``httpx.MockTransport``.  They neither load a candidate profile nor contact a
real Provider.  The assertions record both the compatible wire behaviour and
the raw-client evidence consumed by the central manifest lock before S2-1.
"""

from __future__ import annotations

import json
from collections.abc import Callable
from typing import Any

import httpx
import pytest
from pydantic import SecretStr

from src.core.llm.base import Message
from src.core.llm.openai_compatible_client import (
    OpenAICompatibleClient,
    ProviderUsageMissingError,
)
from src.core.llm.openai_compatible_endpoint import (
    resolve_openai_compatible_endpoint,
)


_DUMMY_KEY = SecretStr("post-v8-s2-0-dummy-key")
_SCHEMA = {
    "type": "object",
    "properties": {"verdict": {"type": "string"}},
    "required": ["verdict"],
    "additionalProperties": False,
}
_CANDIDATES = (
    (
        "Hy3",
        "hy3",
        "https://tokenhub.tencentmaas.com/v1",
        "tokenhub",
        "tokenhub_api_key",
    ),
    (
        "deepseek-v4-flash",
        "deepseek-v4-flash",
        "https://api.deepseek.com",
        "deepseek",
        "deepseek_api_key",
    ),
    (
        "qwen3.8-27b",
        "qwen3.8-27b",
        "https://dashscope.aliyuncs.com/compatible-mode/v1",
        "dashscope",
        "dashscope_api_key",
    ),
)


def _sse_response(
    *,
    model: str,
    include_usage: bool = True,
    request_id: str = "req-s2-0-mock",
) -> httpx.Response:
    chunks: list[dict[str, Any]] = [
        {
            "id": request_id,
            "model": model,
            "choices": [
                {
                    "delta": {"reasoning_content": "mock reasoning"},
                    "finish_reason": None,
                }
            ],
            "usage": None,
        },
        {
            "id": request_id,
            "model": model,
            "choices": [
                {
                    "delta": {"content": '{"verdict":"pass"}'},
                    "finish_reason": "stop",
                }
            ],
            "usage": None,
        },
    ]
    if include_usage:
        chunks.append(
            {
                "id": request_id,
                "model": model,
                "choices": [],
                "usage": {
                    "prompt_tokens": 11,
                    "completion_tokens": 7,
                    "total_tokens": 18,
                    "completion_tokens_details": {"reasoning_tokens": 3},
                },
            }
        )
    body = "".join(f"data: {json.dumps(chunk)}\n\n" for chunk in chunks)
    body += "data: [DONE]\n\n"
    return httpx.Response(
        200,
        content=body.encode(),
        headers={
            "content-type": "text/event-stream",
            "x-request-id": request_id,
        },
    )


def _client(
    *,
    model: str,
    base_url: str,
    handler: Callable[[httpx.Request], httpx.Response],
    enable_thinking: bool | None = None,
) -> OpenAICompatibleClient:
    return OpenAICompatibleClient(
        model=model,
        base_url=base_url,
        api_key=_DUMMY_KEY,
        enable_thinking=enable_thinking,
        preserve_thinking=False if model == "qwen3.8-27b" else None,
        http_client=httpx.Client(transport=httpx.MockTransport(handler)),
    )


@pytest.mark.parametrize(
    ("display_name", "model", "base_url", "authority", "settings_field"),
    _CANDIDATES,
)
def test_exact_candidate_endpoint_authority_and_sse_wire(
    display_name: str,
    model: str,
    base_url: str,
    authority: str,
    settings_field: str,
) -> None:
    """Exact IDs serialize on the intended host and parse terminal SSE usage."""

    endpoint = resolve_openai_compatible_endpoint(base_url)
    assert endpoint.credential_authority == authority, display_name
    assert endpoint.settings_field == settings_field, display_name

    captured: dict[str, Any] = {}

    def handler(request: httpx.Request) -> httpx.Response:
        captured["url"] = str(request.url)
        captured["body"] = json.loads(request.content)
        captured["authorization"] = request.headers["authorization"]
        return _sse_response(model=model)

    client = _client(
        model=model,
        base_url=base_url,
        handler=handler,
        enable_thinking=True if model == "qwen3.8-27b" else None,
    )
    generation_params: dict[str, Any] = {}
    if model == "hy3":
        generation_params.update(
            temperature=0.7,
            max_completion_tokens=24_384,
        )
        generation_params["thinking"] = {
            "type": "enabled",
            "budget_tokens": 16_384,
        }
    elif model == "deepseek-v4-flash":
        generation_params["max_tokens"] = 24_384
        generation_params["thinking"] = {"type": "enabled"}
        generation_params["reasoning_effort"] = "high"
    else:
        generation_params.update(
            temperature=0.7,
            max_completion_tokens=24_384,
        )
        generation_params["thinking_budget"] = 16_384

    # The current production capability registry intentionally does not mark
    # any of the three exact candidate IDs as strict-schema capable.
    prepared = client.prepare_strict_json_schema(
        model=model,
        schema_name="s2_0_protocol_probe",
        schema=_SCHEMA,
        gen_params=generation_params,
    )
    assert "response_format" not in prepared, display_name

    response = client.complete(
        [Message(role="user", content="Return JSON for this mock-only probe")],
        **prepared,
    )

    assert captured["url"] == f"{base_url}/chat/completions"
    assert captured["authorization"] == "Bearer post-v8-s2-0-dummy-key"
    assert captured["body"]["model"] == model
    assert captured["body"]["stream"] is True
    assert captured["body"]["stream_options"] == {"include_usage": True}
    if model == "hy3":
        assert "enable_thinking" not in captured["body"]
        assert "preserve_thinking" not in captured["body"]
        assert captured["body"]["thinking"] == generation_params["thinking"]
    elif model == "deepseek-v4-flash":
        assert "enable_thinking" not in captured["body"]
        assert "preserve_thinking" not in captured["body"]
        assert "thinking_budget" not in captured["body"]
        assert "temperature" not in captured["body"]
        assert "max_completion_tokens" not in captured["body"]
        assert captured["body"]["max_tokens"] == 24_384
        assert captured["body"]["thinking"] == {"type": "enabled"}
        assert captured["body"]["reasoning_effort"] == "high"
    else:
        assert captured["body"]["enable_thinking"] is True
        assert captured["body"]["preserve_thinking"] is False
        assert captured["body"]["thinking_budget"] == 16_384
    assert response.content == '{"verdict":"pass"}'
    assert response.model_used == model
    assert response.usage.input_tokens == 11
    assert response.usage.output_tokens == 7
    assert response.provider_request_id == "req-s2-0-mock"


@pytest.mark.parametrize(("_name", "model", "base_url", "_a", "_s"), _CANDIDATES)
def test_terminal_sse_without_usage_is_unknown_and_not_retried(
    _name: str,
    model: str,
    base_url: str,
    _a: str,
    _s: str,
) -> None:
    """A completed but unmetered response remains one unknown physical attempt."""

    calls = 0

    def handler(_request: httpx.Request) -> httpx.Response:
        nonlocal calls
        calls += 1
        return _sse_response(model=model, include_usage=False)

    client = _client(model=model, base_url=base_url, handler=handler)
    with pytest.raises(ProviderUsageMissingError, match="usage"):
        client.complete([Message(role="user", content="mock")])
    assert calls == 1


def test_response_model_drift_is_observable_but_not_fail_closed() -> None:
    """The raw client exposes drift for ``ManifestLockedClient`` to reject."""

    client = _client(
        model="hy3",
        base_url="https://tokenhub.tencentmaas.com/v1",
        handler=lambda _request: _sse_response(model="unexpected-fourth-model"),
    )

    response = client.complete([Message(role="user", content="mock")])

    assert response.model_used == "unexpected-fourth-model"
