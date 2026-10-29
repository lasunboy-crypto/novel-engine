"""OpenAICompatibleClient 单测(完全 mock httpx,v1.0.x post-wave5 H1)。

不真接 dashscope / DeepSeek 等真 API。用 ``httpx.MockTransport`` 注入伪造
SSE 响应,验证:

- 多 chunk 增量拼接出完整 ``content``
- ``reasoning_content`` 字段拆到独立缓冲(不进 ``LLMResponse.content``)
- ``[DONE]`` 终止符正确处理
- ``usage`` 字段在最后 chunk 正确解析(``prompt_tokens`` / ``completion_tokens``)
- 4xx 立即抛(不重试)
- 5xx + 连接错误 + ``ReadTimeout`` + SSE EOF 重试 3 次后抛最后一次
- ``InvalidChunkJSONError``(非 JSON payload)不重试直接抛
- ``enable_thinking`` / ``preserve_thinking`` 单次 keyword 透传到 body 顶层
- Qwen3.8 Max 普通请求不把回答上限偷换成思考链总上限；strict JSON
  把回答 + thinking 预算转换成有界 ``max_completion_tokens``
- SSE 即使持续收到 chunk，也受整次请求总墙钟约束且不会自动重试
- Qwen3.8 Max 请求在网络前拒绝超过官方输入/输出上限的 payload，并保留
  Qwen3.7 的历史 64K 输出预检
- ``base_url`` 末尾 ``/`` strip + 请求路径拼接正确
- API key 空 / base_url 空 构造时抛 ``ValueError``
- system / user / assistant 三角色都进 messages 数组
- ``close()`` 关闭 owned http client(不关 external)

集成测(真 dashscope 端到端)在 ``tests/integration/test_qwen_max_ping.py``,
默认被 ``-m 'not integration'`` 排除,需要本机配 ``DASHSCOPE_API_KEY`` 才跑。

目录选择(类 B 处置)
=====================
启动指令字面 ``tests/core/llm/test_openai_compatible_client.py``,但项目
既有 unit / integration 分类(``tests/unit/test_ollama_client.py`` 等)
是惯例。本测对齐 ollama 同型路径走 ``tests/unit/``,impl 文档登记类 B
处置(工程惯例 > 启动指令字面)。
"""

from __future__ import annotations

from typing import Any, Iterable

import httpx
import pytest
from pydantic import BaseModel, SecretStr, model_validator

from src.core.llm import openai_compatible_client as occ
from src.core.llm.base import LLMAccessError, LLMAuthError, Message
from src.core.llm.strict_json_schema import (
    STRICT_JSON_RESPONSE_CONTRACT_PARAM,
    PreparedStructuredJsonSchema,
    StrictJsonResponseViolation,
    StrictJsonSchemaDriftError,
    TOKENHUB_HY3_BASE_URL,
    build_hy3_strict_contract,
)

_DEFAULT_BASE_URL = "https://dashscope.aliyuncs.com/compatible-mode/v1"


def _sse_bytes(lines: Iterable[str]) -> bytes:
    """把多行 SSE 字面拼成 ``b"line1\\nline2\\n..."`` 字节流。

    httpx 的 ``Response.iter_lines`` 按 ``\\n`` 切分。每个 SSE event 用
    空行(``\\n``)分隔,即 ``b"data: ...\\n\\n"`` 形式 — 转字节后是
    ``b"data: ...\\n\\ndata: ...\\n\\n"``。
    """
    return ("\n".join(lines) + "\n").encode("utf-8")


def _sse_chunk(
    *,
    content: str | None = None,
    reasoning: str | None = None,
    finish_reason: str | None = None,
    model: str = "qwen3.8-max",
) -> str:
    """生成单 SSE ``data: <json>`` 行(JSON payload 形如 OpenAI 兼容 delta)。"""
    delta: dict[str, Any] = {}
    if content is not None:
        delta["content"] = content
    if reasoning is not None:
        delta["reasoning_content"] = reasoning
    choice: dict[str, Any] = {"index": 0, "delta": delta}
    if finish_reason is not None:
        choice["finish_reason"] = finish_reason
    import json as _json

    payload = {"model": model, "choices": [choice]}
    return f"data: {_json.dumps(payload)}"


def _sse_usage_chunk(
    prompt_tokens: int,
    completion_tokens: int,
    *,
    model: str = "qwen3.8-max",
) -> str:
    """生成含 ``usage`` 字段的最后一个 SSE chunk(对齐 stream_options.include_usage)。"""
    import json as _json

    payload = {
        "model": model,
        "choices": [],
        "usage": {
            "prompt_tokens": prompt_tokens,
            "completion_tokens": completion_tokens,
            "total_tokens": prompt_tokens + completion_tokens,
        },
    }
    return f"data: {_json.dumps(payload)}"


def _sse_response(lines: list[str]) -> httpx.Response:
    """SSE httpx.Response with content-type=text/event-stream + 终止符 [DONE]。

    自动追加 ``data: [DONE]`` 终止符 + 末尾空行。
    """
    full = list(lines) + ["", "data: [DONE]", ""]
    return httpx.Response(
        200,
        content=_sse_bytes(full),
        headers={"content-type": "text/event-stream"},
    )


def _build_client(
    handler,
    *,
    model: str = "qwen3.8-max",
    base_url: str = _DEFAULT_BASE_URL,
    api_key: str = "sk-test-dashscope-key",
    enable_thinking: bool | None = False,
    preserve_thinking: bool | None = False,
    stream_response: bool = True,
    total_timeout_seconds: float = 1200.0,
    max_attempts: int = 3,
) -> occ.OpenAICompatibleClient:
    """构造 OpenAICompatibleClient + 注入 httpx.MockTransport 跑 handler。"""
    transport = httpx.MockTransport(handler)
    http = httpx.Client(transport=transport)
    return occ.OpenAICompatibleClient(
        model=model,
        base_url=base_url,
        api_key=SecretStr(api_key),
        enable_thinking=enable_thinking,
        preserve_thinking=preserve_thinking,
        stream_response=stream_response,
        total_timeout_seconds=total_timeout_seconds,
        max_attempts=max_attempts,
        http_client=http,
    )


@pytest.fixture(autouse=True)
def _no_sleep(monkeypatch: pytest.MonkeyPatch) -> None:
    """重试退避不真睡眠,加速测试。"""
    monkeypatch.setattr(occ.time, "sleep", lambda _seconds: None)


# ---- TestConstructor --------------------------------------------------------


class TestConstructor:
    def test_empty_api_key_raises(self) -> None:
        with pytest.raises(ValueError, match="缺少 API key"):
            occ.OpenAICompatibleClient(
                model="qwen3.8-max",
                base_url=_DEFAULT_BASE_URL,
                api_key=SecretStr(""),
            )

    def test_empty_base_url_raises(self) -> None:
        with pytest.raises(ValueError, match="缺少 base_url"):
            occ.OpenAICompatibleClient(
                model="qwen3.8-max",
                base_url="",
                api_key=SecretStr("sk-x"),
            )

    def test_base_url_trailing_slash_stripped(self) -> None:
        """``base_url`` 末尾 ``/`` 应被 strip,避免拼接出 ``//chat/completions``。"""
        captured: dict[str, Any] = {}

        def handler(req: httpx.Request) -> httpx.Response:
            captured["url"] = str(req.url)
            return _sse_response(
                [
                    _sse_chunk(content="x", finish_reason="stop"),
                    _sse_usage_chunk(1, 1),
                ]
            )

        client = _build_client(handler, base_url=_DEFAULT_BASE_URL + "/")
        client.complete([Message(role="user", content="hi")])
        assert "//chat/completions" not in captured["url"]
        assert captured["url"].endswith("/chat/completions")


class TestStrictJsonSchema:
    def test_supported_qwen_attaches_schema_and_preserves_explicit_total_limit(
        self,
    ) -> None:
        client = _build_client(lambda _req: _sse_response([]))
        schema = {
            "type": "object",
            "properties": {"summary": {"type": "string"}},
            "required": ["summary"],
            "additionalProperties": False,
        }

        params = client.prepare_strict_json_schema(
            model="qwen3.8-max",
            schema_name="chapter summary/中文",
            schema=schema,
            gen_params={
                "temperature": 0.2,
                "max_tokens": 2000,
                "max_completion_tokens": 3000,
            },
        )

        assert params["temperature"] == 0.2
        assert "max_tokens" not in params
        assert params["max_completion_tokens"] == 3000
        assert params["response_format"] == {
            "type": "json_schema",
            "json_schema": {
                "name": "chapter_summary",
                "strict": True,
                "schema": schema,
            },
        }

    def test_thinking_profile_converts_answer_and_reasoning_to_total_limit(
        self,
    ) -> None:
        client = _build_client(lambda _req: _sse_response([]), enable_thinking=True)

        params = client.prepare_strict_json_schema(
            model="qwen3.8-max",
            schema_name="reviewer",
            schema={"type": "object"},
            gen_params={
                "max_tokens": 8_000,
                "thinking_budget": 16_384,
            },
        )

        assert "max_tokens" not in params
        assert params["max_completion_tokens"] == 24_384

    def test_strict_json_without_total_or_convertible_limit_fails_closed(
        self,
    ) -> None:
        client = _build_client(lambda _req: _sse_response([]))

        with pytest.raises(ValueError, match="strict JSON 必须配置"):
            client.prepare_strict_json_schema(
                model="qwen3.8-max",
                schema_name="reviewer",
                schema={"type": "object"},
                gen_params={"temperature": 0.2},
            )

    @pytest.mark.parametrize(
        ("model", "base_url"),
        [
            ("qwen-plus", _DEFAULT_BASE_URL),
            ("qwen3.8-max", "https://example.com/v1"),
        ],
    )
    def test_unverified_model_or_provider_keeps_request_unchanged(
        self,
        model: str,
        base_url: str,
    ) -> None:
        client = _build_client(lambda _req: _sse_response([]), base_url=base_url)
        original = {"temperature": 0.3, "max_tokens": 1234}

        params = client.prepare_strict_json_schema(
            model=model,
            schema_name="reviewer",
            schema={"type": "object"},
            gen_params=original,
        )

        assert params == original
        assert params is not original

    def test_strict_request_carries_schema_and_quote_safety_instruction(self) -> None:
        captured: dict[str, Any] = {}

        def handler(req: httpx.Request) -> httpx.Response:
            import json as _json

            captured.update(_json.loads(req.content))
            return _sse_response(
                [
                    _sse_chunk(content='{"summary":"合规"}', finish_reason="stop"),
                    _sse_usage_chunk(12, 8),
                ]
            )

        client = _build_client(handler)
        params = client.prepare_strict_json_schema(
            model="qwen3.8-max",
            schema_name="reviewer",
            schema={
                "type": "object",
                "properties": {"summary": {"type": "string"}},
                "required": ["summary"],
                "additionalProperties": False,
            },
            gen_params={"temperature": 0.1, "max_tokens": 2000},
        )

        response = client.complete(
            [
                Message(role="system", content="审阅正文并返回 JSON。"),
                Message(role="user", content="正文"),
            ],
            model="qwen3.8-max",
            **params,
        )

        assert response.content == '{"summary":"合规"}'
        assert captured["response_format"]["json_schema"]["strict"] is True
        assert "max_tokens" not in captured
        assert captured["max_completion_tokens"] == 2000
        assert captured["stream"] is True
        system_text = captured["messages"][0]["content"]
        assert "字符串值内禁止使用未转义的英文双引号" in system_text
        assert "不要 Markdown 代码块" in system_text


class _Hy3WirePayload(BaseModel):
    summary: str


class _Hy3CrossFieldPayload(BaseModel):
    left: str
    right: str

    @model_validator(mode="after")
    def _values_must_match(self) -> "_Hy3CrossFieldPayload":
        if self.left != self.right:
            raise ValueError("left/right must match")
        return self


def _hy3_strict_params(
    output_model: type[BaseModel],
    *,
    schema_name: str = "hy3_payload",
) -> dict[str, Any]:
    raw_schema = output_model.model_json_schema()
    prepared_schema = PreparedStructuredJsonSchema(
        raw_schema,
        output_model=output_model,
        raw_schema=raw_schema,
        transform=None,
    )
    contract = build_hy3_strict_contract(
        schema_name=schema_name,
        schema=prepared_schema,
    )
    return {
        "max_completion_tokens": 2048,
        "response_format": {
            "type": "json_schema",
            "json_schema": {
                "name": contract.schema_name,
                "schema": contract.schema,
                "strict": True,
            },
        },
        STRICT_JSON_RESPONSE_CONTRACT_PARAM: contract,
    }


class TestExactHy3StrictJsonSchema:
    def test_exact_hy3_rejects_structured_request_without_local_contract(
        self,
    ) -> None:
        calls = 0

        def handler(_req: httpx.Request) -> httpx.Response:
            nonlocal calls
            calls += 1
            return _sse_response([])

        client = _build_client(
            handler,
            model="hy3",
            base_url=TOKENHUB_HY3_BASE_URL,
        )

        with pytest.raises(StrictJsonSchemaDriftError, match="Pydantic contract"):
            client.complete(
                [Message(role="user", content="return JSON")],
                response_format={"type": "json_object"},
            )

        assert calls == 0

    def test_request_body_carries_native_schema_and_strips_local_contract(
        self,
    ) -> None:
        captured: dict[str, Any] = {}

        def handler(req: httpx.Request) -> httpx.Response:
            import json as _json

            captured.update(_json.loads(req.content))
            return _sse_response(
                [
                    _sse_chunk(
                        content='{"summary":"合规"}',
                        finish_reason="stop",
                        model="hy3",
                    ),
                    _sse_usage_chunk(12, 8, model="hy3"),
                ]
            )

        client = _build_client(
            handler,
            model="hy3",
            base_url=TOKENHUB_HY3_BASE_URL,
        )
        response = client.complete(
            [Message(role="system", content="Return structured data")],
            **_hy3_strict_params(_Hy3WirePayload),
        )

        assert response.content == '{"summary":"合规"}'
        assert captured["model"] == "hy3"
        assert captured["response_format"]["type"] == "json_schema"
        assert captured["response_format"]["json_schema"]["strict"] is True
        assert (
            captured["response_format"]["json_schema"]["schema"]["additionalProperties"]
            is False
        )
        assert STRICT_JSON_RESPONSE_CONTRACT_PARAM not in captured
        assert captured["stream"] is True

    @pytest.mark.parametrize(
        ("content", "layer"),
        [
            ('{"summary":"裸"引号"}', "raw_json_parse"),
            ("{}", "wire_schema"),
        ],
    )
    def test_raw_or_wire_violation_is_one_request_and_never_repaired(
        self,
        content: str,
        layer: str,
    ) -> None:
        calls = 0

        def handler(_req: httpx.Request) -> httpx.Response:
            nonlocal calls
            calls += 1
            return _sse_response(
                [
                    _sse_chunk(content=content, finish_reason="stop", model="hy3"),
                    _sse_usage_chunk(12, 8, model="hy3"),
                ]
            )

        client = _build_client(
            handler,
            model="hy3",
            base_url=TOKENHUB_HY3_BASE_URL,
        )
        with pytest.raises(StrictJsonResponseViolation) as exc_info:
            client.complete(
                [Message(role="user", content="return JSON")],
                **_hy3_strict_params(_Hy3WirePayload),
            )

        assert exc_info.value.layer == layer
        assert calls == 1

    def test_wire_pydantic_violation_is_one_request_without_model_retry(
        self,
    ) -> None:
        calls = 0

        def handler(_req: httpx.Request) -> httpx.Response:
            nonlocal calls
            calls += 1
            return _sse_response(
                [
                    _sse_chunk(
                        content='{"left":"a","right":"b"}',
                        finish_reason="stop",
                        model="hy3",
                    ),
                    _sse_usage_chunk(12, 8, model="hy3"),
                ]
            )

        client = _build_client(
            handler,
            model="hy3",
            base_url=TOKENHUB_HY3_BASE_URL,
        )
        with pytest.raises(StrictJsonResponseViolation) as exc_info:
            client.complete(
                [Message(role="user", content="return JSON")],
                **_hy3_strict_params(_Hy3CrossFieldPayload),
            )

        assert exc_info.value.layer == "wire_pydantic"
        assert calls == 1

    def test_provider_schema_reject_is_not_retried_or_downgraded(self) -> None:
        calls = 0
        captured: dict[str, Any] = {}

        def handler(req: httpx.Request) -> httpx.Response:
            import json as _json

            nonlocal calls
            calls += 1
            captured.update(_json.loads(req.content))
            return httpx.Response(
                422,
                json={"error": {"code": "InvalidSchema", "message": "rejected"}},
            )

        client = _build_client(
            handler,
            model="hy3",
            base_url=TOKENHUB_HY3_BASE_URL,
        )
        with pytest.raises(httpx.HTTPStatusError):
            client.complete(
                [Message(role="user", content="return JSON")],
                **_hy3_strict_params(_Hy3WirePayload),
            )

        assert calls == 1
        assert captured["response_format"]["type"] == "json_schema"

    @pytest.mark.parametrize(
        ("base_url", "model"),
        [
            (TOKENHUB_HY3_BASE_URL, "hy3-preview"),
            ("https://example.com/v1", "hy3"),
        ],
    )
    def test_model_or_host_drift_fails_before_network(
        self,
        base_url: str,
        model: str,
    ) -> None:
        calls = 0

        def handler(_req: httpx.Request) -> httpx.Response:
            nonlocal calls
            calls += 1
            return _sse_response([])

        client = _build_client(handler, model=model, base_url=base_url)
        with pytest.raises(StrictJsonSchemaDriftError):
            client.complete(
                [Message(role="user", content="return JSON")],
                **_hy3_strict_params(_Hy3WirePayload),
            )
        assert calls == 0

    def test_schema_hash_drift_fails_before_network(self) -> None:
        calls = 0

        def handler(_req: httpx.Request) -> httpx.Response:
            nonlocal calls
            calls += 1
            return _sse_response([])

        params = _hy3_strict_params(_Hy3WirePayload)
        params["response_format"]["json_schema"]["schema"]["properties"]["extra"] = {
            "type": "string"
        }
        client = _build_client(
            handler,
            model="hy3",
            base_url=TOKENHUB_HY3_BASE_URL,
        )
        with pytest.raises(StrictJsonSchemaDriftError):
            client.complete(
                [Message(role="user", content="return JSON")],
                **params,
            )
        assert calls == 0

    def test_existing_qwen_strict_syntax_observation_remains_non_blocking(
        self,
    ) -> None:
        client = _build_client(
            lambda _req: _sse_response(
                [
                    _sse_chunk(content="not-json", finish_reason="stop"),
                    _sse_usage_chunk(12, 8),
                ]
            )
        )
        params = client.prepare_strict_json_schema(
            model="qwen3.8-max",
            schema_name="legacy_qwen",
            schema={
                "type": "object",
                "properties": {"summary": {"type": "string"}},
                "required": ["summary"],
                "additionalProperties": False,
            },
            gen_params={"max_tokens": 128},
        )

        response = client.complete(
            [Message(role="user", content="return JSON")],
            **params,
        )
        assert response.content == "not-json"


# ---- TestSSEAggregation -----------------------------------------------------


class TestSSEAggregation:
    def test_multi_chunk_content_concatenated(self) -> None:
        """多个 ``delta.content`` chunk 应按顺序拼接成完整正文。"""
        chunks = [
            _sse_chunk(content="第一段"),
            _sse_chunk(content="第二段"),
            _sse_chunk(content="第三段", finish_reason="stop"),
            _sse_usage_chunk(prompt_tokens=42, completion_tokens=10),
        ]
        client = _build_client(lambda _req: _sse_response(chunks))
        result = client.complete([Message(role="user", content="写三段")])
        assert result.content == "第一段第二段第三段"
        assert result.usage.input_tokens == 42
        assert result.usage.output_tokens == 10
        assert result.finish_reason == "stop"
        assert result.model_used == "qwen3.8-max"

    def test_reasoning_content_kept_separate_from_main_content(self) -> None:
        """``delta.reasoning_content`` 应单独入缓冲不进 LLMResponse.content。

        thinking 模式典型流:思考 chunks(content=null + reasoning_content)
        → 输出 chunks(content + reasoning_content=null)。
        """
        chunks = [
            _sse_chunk(reasoning="先想想..."),
            _sse_chunk(reasoning="再想想..."),
            _sse_chunk(content="最终答案"),
            _sse_chunk(content="补充", finish_reason="stop"),
            _sse_usage_chunk(prompt_tokens=20, completion_tokens=5),
        ]
        client = _build_client(lambda _req: _sse_response(chunks))
        result = client.complete([Message(role="user", content="深度问题")])
        # content 字段只含 ``最终答案补充`` 不含 ``先想想再想想``
        assert result.content == "最终答案补充"
        assert "先想想" not in result.content
        assert "再想想" not in result.content
        assert result.reasoning_chars == len("先想想...再想想...")

    def test_top_level_sse_error_is_explicit_and_never_retried(self) -> None:
        """TokenHub 类中途 error frame 不能被当成空 choices 后继续成功。"""

        calls = {"n": 0}

        def handler(_req: httpx.Request) -> httpx.Response:
            calls["n"] += 1
            import json as _json

            payload = {
                "id": "req-tokenhub-error-1",
                "error": {
                    "code": "BackendUnavailable",
                    "message": "fallback is disabled",
                    "request_id": "req-tokenhub-error-1",
                },
            }
            return httpx.Response(
                200,
                content=_sse_bytes(
                    [
                        f"data: {_json.dumps(payload)}",
                        "",
                        "data: [DONE]",
                        "",
                    ]
                ),
                headers={"content-type": "text/event-stream"},
            )

        client = _build_client(handler)
        with pytest.raises(occ.SSEProviderError) as exc_info:
            client.complete([Message(role="user", content="x")])

        assert calls["n"] == 1
        assert exc_info.value.provider_code == "BackendUnavailable"
        assert exc_info.value.provider_request_id == "req-tokenhub-error-1"

    def test_done_marker_terminates_stream(self) -> None:
        """``data: [DONE]`` 后的行应被忽略(防御服务端不规范多发)。"""
        # 把 [DONE] 放中间,后续行不应再被解析
        full = [
            _sse_chunk(content="正确", finish_reason="stop"),
            _sse_usage_chunk(prompt_tokens=2, completion_tokens=1),
            "",
            "data: [DONE]",
            "",
            _sse_chunk(content="不该被读到"),
            "",
        ]
        client = _build_client(
            lambda _req: httpx.Response(
                200,
                content=_sse_bytes(full),
                headers={"content-type": "text/event-stream"},
            )
        )
        result = client.complete([Message(role="user", content="x")])
        assert result.content == "正确"
        assert "不该被读到" not in result.content

    def test_done_without_usage_is_unknown_and_never_retried(self) -> None:
        """已完成请求缺 usage 不能伪装成成功 0，也不能自动重发扩大费用。"""
        calls = {"n": 0}

        def handler(_req: httpx.Request) -> httpx.Response:
            calls["n"] += 1
            return _sse_response([_sse_chunk(content="已有正文", finish_reason="stop")])

        client = _build_client(handler)
        with pytest.raises(occ.ProviderUsageMissingError, match="usage"):
            client.complete([Message(role="user", content="x")])
        assert calls["n"] == 1

    def test_success_exposes_safe_provider_request_id(self) -> None:
        chunks = [
            _sse_chunk(content="正文", finish_reason="stop"),
            _sse_usage_chunk(prompt_tokens=2, completion_tokens=1),
        ]
        full = chunks + ["", "data: [DONE]", ""]
        client = _build_client(
            lambda _req: httpx.Response(
                200,
                content=_sse_bytes(full),
                headers={
                    "content-type": "text/event-stream",
                    "x-request-id": "req-success-123",
                },
            )
        )

        result = client.complete([Message(role="user", content="x")])

        assert result.provider_request_id == "req-success-123"

    def test_missing_done_marker_raises_sse_stream_error(self) -> None:
        """SSE 流末尾未见 [DONE] 应抛 SSEStreamError(进入 retry)。"""
        full = [_sse_chunk(content="x", finish_reason="stop")]  # 故意不加 [DONE]
        client = _build_client(
            lambda _req: httpx.Response(
                200,
                content=_sse_bytes(full),
                headers={"content-type": "text/event-stream"},
            )
        )
        with pytest.raises(occ.SSEStreamError, match="未见 \\[DONE\\]"):
            client.complete([Message(role="user", content="x")])

    def test_invalid_chunk_json_raises_without_retry(self) -> None:
        """非 JSON payload 的 ``data:`` 行应抛 InvalidChunkJSONError 不重试。"""
        call_count = {"n": 0}

        def handler(_req: httpx.Request) -> httpx.Response:
            call_count["n"] += 1
            full = ["data: {not_valid_json}", "", "data: [DONE]", ""]
            return httpx.Response(
                200,
                content=_sse_bytes(full),
                headers={"content-type": "text/event-stream"},
            )

        client = _build_client(handler)
        with pytest.raises(occ.InvalidChunkJSONError, match="非合法 JSON"):
            client.complete([Message(role="user", content="x")])
        # 不重试 — 只调用了 1 次
        assert call_count["n"] == 1

    def test_continuous_sse_is_stopped_by_total_wall_clock_without_retry(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        """持续有 chunk 会绕过 httpx read timeout，但不能绕过整次墙钟。"""
        call_count = {"n": 0}

        def handler(_req: httpx.Request) -> httpx.Response:
            call_count["n"] += 1
            return _sse_response(
                [
                    _sse_chunk(reasoning="仍在思考"),
                    _sse_chunk(reasoning="继续思考"),
                    _sse_chunk(content="不会走到这里", finish_reason="stop"),
                ]
            )

        clock = iter((0.0, 0.4, 1.1))
        monkeypatch.setattr(occ.time, "monotonic", lambda: next(clock))
        client = _build_client(handler, total_timeout_seconds=1.0)

        with pytest.raises(occ.SSETotalTimeoutError, match="超过本地总墙钟"):
            client.complete([Message(role="user", content="x")])
        assert call_count["n"] == 1

    def test_upstream_override_allows_long_sse_without_leaking_to_wire(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        """单次豁免只关掉 SSE 总墙钟，不改变默认调用和 HTTP 参数。"""
        import itertools
        import json as _json

        bodies: list[dict[str, Any]] = []

        def handler(req: httpx.Request) -> httpx.Response:
            bodies.append(_json.loads(req.content))
            return _sse_response(
                [
                    _sse_chunk(reasoning="继续思考"),
                    _sse_chunk(content="完成", finish_reason="stop"),
                    _sse_usage_chunk(1, 2),
                ]
            )

        clock = itertools.count(start=0.0, step=0.6)
        monkeypatch.setattr(occ.time, "monotonic", lambda: next(clock))
        client = _build_client(handler, total_timeout_seconds=1.0)
        result = client.complete(
            [Message(role="user", content="x")], disable_total_timeout=True
        )

        assert result.content == "完成"
        assert len(bodies) == 1
        assert "disable_total_timeout" not in bodies[0]


# ---- TestRequestBody --------------------------------------------------------


class TestRequestBody:
    def test_qwen_thinking_keeps_reasoning_capacity_unconstrained_by_max_tokens(
        self,
    ) -> None:
        """thinking-on 的 8K 只限回答，不能擅自缩小思考链能力。"""
        captured: dict[str, Any] = {}

        def handler(req: httpx.Request) -> httpx.Response:
            import json as _json

            captured["body"] = _json.loads(req.content)
            return _sse_response(
                [
                    _sse_chunk(content="x", finish_reason="stop"),
                    _sse_usage_chunk(1, 1),
                ]
            )

        client = _build_client(handler, enable_thinking=True)
        client.complete([Message(role="user", content="x")], max_tokens=8_000)

        assert captured["body"]["max_tokens"] == 8_000
        assert "max_completion_tokens" not in captured["body"]

    def test_qwen_standard_does_not_inject_max_completion_tokens(self) -> None:
        """thinking-off 没有思维链，保留既有 ``max_tokens`` wire。"""
        captured: dict[str, Any] = {}

        def handler(req: httpx.Request) -> httpx.Response:
            import json as _json

            captured["body"] = _json.loads(req.content)
            return _sse_response(
                [
                    _sse_chunk(content="x", finish_reason="stop"),
                    _sse_usage_chunk(1, 1),
                ]
            )

        client = _build_client(handler, enable_thinking=False)
        client.complete([Message(role="user", content="x")], max_tokens=8_000)

        assert captured["body"]["max_tokens"] == 8_000
        assert "max_completion_tokens" not in captured["body"]

    def test_explicit_max_completion_tokens_is_preserved(self) -> None:
        """caller 显式给完整输出预算时不得被 ``max_tokens`` 覆盖。"""
        captured: dict[str, Any] = {}

        def handler(req: httpx.Request) -> httpx.Response:
            import json as _json

            captured["body"] = _json.loads(req.content)
            return _sse_response(
                [
                    _sse_chunk(content="x", finish_reason="stop"),
                    _sse_usage_chunk(1, 1),
                ]
            )

        client = _build_client(handler, enable_thinking=True)
        client.complete(
            [Message(role="user", content="x")],
            max_tokens=8_000,
            max_completion_tokens=12_000,
        )

        assert captured["body"]["max_completion_tokens"] == 12_000

    def test_unknown_compatible_model_does_not_receive_qwen_mapping(self) -> None:
        """DashScope 专属兼容语义不能污染未知 OpenAI-compatible provider。"""
        captured: dict[str, Any] = {}

        def handler(req: httpx.Request) -> httpx.Response:
            import json as _json

            captured["body"] = _json.loads(req.content)
            return _sse_response(
                [
                    _sse_chunk(content="x", finish_reason="stop"),
                    _sse_usage_chunk(1, 1),
                ]
            )

        client = _build_client(
            handler,
            model="some-compatible-model",
            enable_thinking=True,
        )
        client.complete([Message(role="user", content="x")], max_tokens=8_000)

        assert "max_completion_tokens" not in captured["body"]

    def test_qwen38_accepts_public_output_limit(self) -> None:
        captured: dict[str, Any] = {}

        def handler(req: httpx.Request) -> httpx.Response:
            import json as _json

            captured["body"] = _json.loads(req.content)
            return _sse_response(
                [
                    _sse_chunk(content="x", finish_reason="stop"),
                    _sse_usage_chunk(1, 1),
                ]
            )

        client = _build_client(handler, enable_thinking=True)
        client.complete(
            [Message(role="user", content="x")],
            max_tokens=131_072,
        )
        assert captured["body"]["max_tokens"] == 131_072

    def test_qwen38_rejects_output_over_public_limit_before_network(self) -> None:
        calls = {"n": 0}

        def handler(_req: httpx.Request) -> httpx.Response:
            calls["n"] += 1
            raise AssertionError("超限请求不得到达 HTTP transport")

        client = _build_client(handler, enable_thinking=True)
        with pytest.raises(ValueError, match="超过官方输出上限"):
            client.complete(
                [Message(role="user", content="x")],
                max_tokens=131_073,
            )
        assert calls["n"] == 0

    def test_qwen37_keeps_historical_64k_output_limit(self) -> None:
        calls = {"n": 0}

        def handler(_req: httpx.Request) -> httpx.Response:
            calls["n"] += 1
            raise AssertionError("超限请求不得到达 HTTP transport")

        client = _build_client(
            handler,
            model="qwen3.7-max",
            enable_thinking=True,
        )
        with pytest.raises(ValueError, match="Qwen3.7 Max.*超过官方输出上限"):
            client.complete(
                [Message(role="user", content="x")],
                max_tokens=65_537,
            )
        assert calls["n"] == 0

    def test_qwen_rejects_oversized_thinking_input_before_network(self) -> None:
        calls = {"n": 0}

        def handler(_req: httpx.Request) -> httpx.Response:
            calls["n"] += 1
            raise AssertionError("超限请求不得到达 HTTP transport")

        client = _build_client(handler, enable_thinking=True)
        thinking_limit = occ._QWEN_MAX_MODEL_LIMITS["qwen3.8-max"][2]
        oversized = "x" * thinking_limit
        with pytest.raises(ValueError, match="输入超过官方单请求安全上限"):
            client.complete([Message(role="user", content=oversized)])
        assert calls["n"] == 0

    def test_qwen_omitted_thinking_uses_default_thinking_input_limit(self) -> None:
        calls = {"n": 0}

        def handler(_req: httpx.Request) -> httpx.Response:
            calls["n"] += 1
            raise AssertionError("默认 thinking 超限请求不得到达 HTTP transport")

        client = _build_client(handler, enable_thinking=None)
        # 单条消息固定余量 96；该长度只超过 thinking 上限 1 token upper bound，
        # 仍低于 standard 上限，能锁住“None 被误当 standard”的回归。
        thinking_limit = occ._QWEN_MAX_MODEL_LIMITS["qwen3.8-max"][2]
        oversized = "x" * (thinking_limit - 95)
        with pytest.raises(ValueError, match="limit=983616,thinking=True"):
            client.complete([Message(role="user", content=oversized)])
        assert calls["n"] == 0

    def test_bounded_thinking_and_local_input_budget_wire_contract(self) -> None:
        """思考预算进 body；本地输入保险丝绝不能外发。"""
        captured: dict[str, Any] = {}

        def handler(req: httpx.Request) -> httpx.Response:
            import json as _json

            captured["body"] = _json.loads(req.content)
            return _sse_response(
                [
                    _sse_chunk(content="x", finish_reason="stop"),
                    _sse_usage_chunk(1, 1),
                ]
            )

        client = _build_client(handler, enable_thinking=True)
        client.complete(
            [Message(role="user", content="x")],
            thinking_budget=16_384,
            input_token_budget=262_144,
        )
        assert captured["body"]["thinking_budget"] == 16_384
        assert "input_token_budget" not in captured["body"]

    def test_local_input_budget_rejects_before_network(self) -> None:
        calls = {"n": 0}

        def handler(_req: httpx.Request) -> httpx.Response:
            calls["n"] += 1
            raise AssertionError("profile 预算超限请求不得到达 HTTP transport")

        client = _build_client(handler, enable_thinking=True)
        with pytest.raises(ValueError, match="超过 profile 本地冗余预算"):
            client.complete(
                [Message(role="user", content="x")],
                input_token_budget=95,
            )
        assert calls["n"] == 0

    @pytest.mark.parametrize("budget", [0, -1, True, 1.5])
    def test_local_input_budget_requires_positive_integer(self, budget: Any) -> None:
        calls = {"n": 0}

        def handler(_req: httpx.Request) -> httpx.Response:
            calls["n"] += 1
            raise AssertionError("非法预算不得到达 HTTP transport")

        client = _build_client(handler, enable_thinking=True)
        with pytest.raises(ValueError, match="input_token_budget 必须为正整数"):
            client.complete(
                [Message(role="user", content="x")],
                input_token_budget=budget,
            )
        assert calls["n"] == 0

    def test_qwen_rejects_invalid_or_conflicting_thinking_budget(self) -> None:
        calls = {"n": 0}

        def handler(_req: httpx.Request) -> httpx.Response:
            calls["n"] += 1
            raise AssertionError("非法思考预算不得到达 HTTP transport")

        client = _build_client(handler, enable_thinking=True)
        with pytest.raises(ValueError, match="thinking_budget 必须为"):
            client.complete(
                [Message(role="user", content="x")],
                thinking_budget=262_145,
            )
        with pytest.raises(ValueError, match="不得同时设置"):
            client.complete(
                [Message(role="user", content="x")],
                thinking_budget=4_096,
                reasoning_effort="low",
            )
        assert calls["n"] == 0

    def test_qwen_thinking_off_rejects_positive_thinking_budget(self) -> None:
        calls = {"n": 0}

        def handler(_req: httpx.Request) -> httpx.Response:
            calls["n"] += 1
            raise AssertionError("thinking off 冲突不得到达 HTTP transport")

        client = _build_client(handler, enable_thinking=False)
        with pytest.raises(ValueError, match="enable_thinking=false"):
            client.complete(
                [Message(role="user", content="x")],
                thinking_budget=4_096,
            )
        assert calls["n"] == 0

    def test_enable_thinking_false_is_explicit_in_body(self) -> None:
        """Qwen standard 的显式 ``False`` 必须进入真实 HTTP body。"""
        captured: dict[str, Any] = {}

        def handler(req: httpx.Request) -> httpx.Response:
            import json as _json

            captured["body"] = _json.loads(req.content)
            return _sse_response(
                [
                    _sse_chunk(content="x", finish_reason="stop"),
                    _sse_usage_chunk(1, 1),
                ]
            )

        client = _build_client(handler, enable_thinking=False)
        client.complete([Message(role="user", content="x")])
        assert captured["body"]["enable_thinking"] is False

    def test_enable_thinking_none_is_omitted_for_unknown_provider(self) -> None:
        """只有 ``None`` 才允许为未知兼容 provider 省略扩展字段。"""
        captured: dict[str, Any] = {}

        def handler(req: httpx.Request) -> httpx.Response:
            import json as _json

            captured["body"] = _json.loads(req.content)
            return _sse_response(
                [
                    _sse_chunk(content="x", finish_reason="stop"),
                    _sse_usage_chunk(1, 1),
                ]
            )

        client = _build_client(
            handler,
            model="some-compatible-model",
            enable_thinking=None,
            preserve_thinking=None,
        )
        client.complete([Message(role="user", content="x")])
        assert "enable_thinking" not in captured["body"]
        assert "preserve_thinking" not in captured["body"]

    def test_enable_thinking_single_call_keyword_override(self) -> None:
        """构造默认 False + ``complete(enable_thinking=True)`` 单次切换。"""
        captured: dict[str, Any] = {}

        def handler(req: httpx.Request) -> httpx.Response:
            import json as _json

            captured["body"] = _json.loads(req.content)
            return _sse_response(
                [
                    _sse_chunk(content="x", finish_reason="stop"),
                    _sse_usage_chunk(1, 1),
                ]
            )

        client = _build_client(handler, enable_thinking=False)
        client.complete(
            [Message(role="user", content="x")],
            enable_thinking=True,
        )
        assert captured["body"]["enable_thinking"] is True

    def test_preserve_thinking_false_is_explicit_in_body(self) -> None:
        """3.8 默认会保留思维链，活动配置的显式 False 必须到达 HTTP body。"""
        captured: dict[str, Any] = {}

        def handler(req: httpx.Request) -> httpx.Response:
            import json as _json

            captured["body"] = _json.loads(req.content)
            return _sse_response(
                [
                    _sse_chunk(content="x", finish_reason="stop"),
                    _sse_usage_chunk(1, 1),
                ]
            )

        client = _build_client(handler, preserve_thinking=False)
        client.complete([Message(role="user", content="x")])
        assert captured["body"]["preserve_thinking"] is False

    def test_preserve_thinking_single_call_keyword_override(self) -> None:
        captured: dict[str, Any] = {}

        def handler(req: httpx.Request) -> httpx.Response:
            import json as _json

            captured["body"] = _json.loads(req.content)
            return _sse_response(
                [
                    _sse_chunk(content="x", finish_reason="stop"),
                    _sse_usage_chunk(1, 1),
                ]
            )

        client = _build_client(handler, preserve_thinking=False)
        client.complete(
            [Message(role="user", content="x")],
            preserve_thinking=True,
        )
        assert captured["body"]["preserve_thinking"] is True

    def test_stream_and_include_usage_forced(self) -> None:
        """``stream`` / ``stream_options`` 强制覆盖,即使 caller 传 stream=False。"""
        captured: dict[str, Any] = {}

        def handler(req: httpx.Request) -> httpx.Response:
            import json as _json

            captured["body"] = _json.loads(req.content)
            return _sse_response(
                [
                    _sse_chunk(content="x", finish_reason="stop"),
                    _sse_usage_chunk(1, 1),
                ]
            )

        client = _build_client(handler)
        client.complete(
            [Message(role="user", content="x")],
            stream=False,  # 故意传 False,应被强制改 True
            stream_options={"include_usage": False},  # 同上
        )
        assert captured["body"]["stream"] is True
        assert captured["body"]["stream_options"] == {"include_usage": True}

    def test_non_stream_json_body_and_response_are_complete(self) -> None:
        """校准可显式禁用 SSE，同时保留思考、正文、model 与 usage。"""
        captured: dict[str, Any] = {}

        def handler(req: httpx.Request) -> httpx.Response:
            import json as _json

            captured["body"] = _json.loads(req.content)
            captured["accept"] = req.headers["accept"]
            return httpx.Response(
                200,
                json={
                    "model": "qwen3.7-max-2026-05-20",
                    "choices": [
                        {
                            "message": {
                                "role": "assistant",
                                "reasoning_content": "内部思考",
                                "content": "最终答案",
                            },
                            "finish_reason": "stop",
                        }
                    ],
                    "usage": {
                        "prompt_tokens": 21,
                        "completion_tokens": 8,
                        "total_tokens": 29,
                    },
                },
            )

        client = _build_client(
            handler,
            enable_thinking=True,
            stream_response=False,
        )
        result = client.complete(
            [Message(role="user", content="x")],
            stream=True,
            stream_options={"include_usage": True},
        )
        assert captured["body"]["stream"] is False
        assert "stream_options" not in captured["body"]
        assert captured["body"]["enable_thinking"] is True
        assert captured["accept"] == "application/json"
        assert result.content == "最终答案"
        assert result.model_used == "qwen3.7-max-2026-05-20"
        assert result.usage.input_tokens == 21
        assert result.usage.output_tokens == 8
        assert result.finish_reason == "stop"

    def test_non_stream_malformed_json_retries_three_times(self) -> None:
        """完整 JSON 未形成时仍是有界 technical retry，不得误作业务结果。"""
        calls = {"n": 0}

        def handler(_req: httpx.Request) -> httpx.Response:
            calls["n"] += 1
            return httpx.Response(200, content=b'{"choices":')

        client = _build_client(handler, stream_response=False)
        with pytest.raises(occ.NonStreamingResponseError, match="完整 JSON"):
            client.complete([Message(role="user", content="x")])
        assert calls["n"] == 3

    def test_non_stream_missing_usage_is_unknown_without_retry(self) -> None:
        """完整响应缺 usage 已发生费用，禁止自动重发。"""
        calls = {"n": 0}

        def handler(_req: httpx.Request) -> httpx.Response:
            calls["n"] += 1
            return httpx.Response(
                200,
                json={
                    "model": "qwen3.7-max",
                    "choices": [
                        {
                            "message": {"content": "x"},
                            "finish_reason": "stop",
                        }
                    ],
                },
            )

        client = _build_client(handler, stream_response=False)
        with pytest.raises(occ.ProviderUsageMissingError, match="usage"):
            client.complete([Message(role="user", content="x")])
        assert calls["n"] == 1

    def test_three_roles_pass_through_to_messages(self) -> None:
        """system / user / assistant 三角色都在 messages 数组内(无 split)。"""
        captured: dict[str, Any] = {}

        def handler(req: httpx.Request) -> httpx.Response:
            import json as _json

            captured["body"] = _json.loads(req.content)
            return _sse_response(
                [
                    _sse_chunk(content="x", finish_reason="stop"),
                    _sse_usage_chunk(1, 1),
                ]
            )

        client = _build_client(handler)
        client.complete(
            [
                Message(role="system", content="你是助手"),
                Message(role="user", content="问题"),
                Message(role="assistant", content="先答"),
                Message(role="user", content="追问"),
            ]
        )
        messages = captured["body"]["messages"]
        assert len(messages) == 4
        assert messages[0] == {"role": "system", "content": "你是助手"}
        assert messages[1] == {"role": "user", "content": "问题"}
        assert messages[2] == {"role": "assistant", "content": "先答"}
        assert messages[3] == {"role": "user", "content": "追问"}

    def test_authorization_header_uses_bearer(self) -> None:
        """``Authorization`` 头应是 ``Bearer <key>`` 格式。"""
        captured: dict[str, Any] = {}

        def handler(req: httpx.Request) -> httpx.Response:
            captured["auth"] = req.headers.get("Authorization")
            return _sse_response(
                [
                    _sse_chunk(content="x", finish_reason="stop"),
                    _sse_usage_chunk(1, 1),
                ]
            )

        client = _build_client(handler, api_key="sk-my-test-key")
        client.complete([Message(role="user", content="x")])
        assert captured["auth"] == "Bearer sk-my-test-key"

    def test_extra_gen_params_pass_through(self) -> None:
        """``temperature`` / ``max_tokens`` 等 OpenAI 标准 params 透传 body 顶层。"""
        captured: dict[str, Any] = {}

        def handler(req: httpx.Request) -> httpx.Response:
            import json as _json

            captured["body"] = _json.loads(req.content)
            return _sse_response(
                [
                    _sse_chunk(content="x", finish_reason="stop"),
                    _sse_usage_chunk(1, 1),
                ]
            )

        client = _build_client(handler)
        client.complete(
            [Message(role="user", content="x")],
            temperature=0.7,
            max_tokens=4096,
            top_p=0.9,
        )
        assert captured["body"]["temperature"] == 0.7
        assert captured["body"]["max_tokens"] == 4096
        assert captured["body"]["top_p"] == 0.9


# ---- TestErrorHandlingAndRetry ----------------------------------------------


class TestErrorHandlingAndRetry:
    def test_selection_scope_can_reduce_transport_to_one_attempt(self) -> None:
        """S2-1 明示零 retry 后，503 也只能形成一个 physical attempt。"""

        call_count = {"n": 0}

        def handler(_req: httpx.Request) -> httpx.Response:
            call_count["n"] += 1
            return httpx.Response(503, json={"error": "Service Unavailable"})

        client = _build_client(handler, max_attempts=1)
        with pytest.raises(httpx.HTTPStatusError):
            client.complete([Message(role="user", content="x")])
        assert call_count["n"] == 1

    def test_429_immediate_raise_without_transport_retry(self) -> None:
        """并行批遇到 429 时不在 transport 层放大请求数。"""
        call_count = {"n": 0}

        def handler(_req: httpx.Request) -> httpx.Response:
            call_count["n"] += 1
            return httpx.Response(429, json={"error": "rate limited"})

        client = _build_client(handler)
        with pytest.raises(httpx.HTTPStatusError) as exc_info:
            client.complete([Message(role="user", content="x")])
        assert exc_info.value.response.status_code == 429
        assert call_count["n"] == 1

    def test_4xx_immediate_raise_no_retry(self) -> None:
        """非认证 4xx(400/404 等)立即抛 HTTPStatusError,不重试。

        注:401 包装成 LLMAuthError，403 包装成 LLMAccessError；
        故本测试用 404 验证其余 4xx 行为不变。
        """
        call_count = {"n": 0}

        def handler(_req: httpx.Request) -> httpx.Response:
            call_count["n"] += 1
            return httpx.Response(404, json={"error": "Not Found"})

        client = _build_client(handler)
        with pytest.raises(httpx.HTTPStatusError) as exc_info:
            client.complete([Message(role="user", content="x")])
        assert exc_info.value.response.status_code == 404
        assert call_count["n"] == 1  # 未重试

    def test_5xx_retry_three_times_then_raise(self) -> None:
        """500 服务端错应重试 3 次后抛最后一次。"""
        call_count = {"n": 0}

        def handler(_req: httpx.Request) -> httpx.Response:
            call_count["n"] += 1
            return httpx.Response(503, json={"error": "Service Unavailable"})

        client = _build_client(handler)
        with pytest.raises(httpx.HTTPStatusError) as exc_info:
            client.complete([Message(role="user", content="x")])
        assert exc_info.value.response.status_code == 503
        assert call_count["n"] == 3  # 重试 3 次

    def test_5xx_then_success_returns(self) -> None:
        """503 一次后第二次成功 → 正常返。"""
        call_count = {"n": 0}

        def handler(_req: httpx.Request) -> httpx.Response:
            call_count["n"] += 1
            if call_count["n"] == 1:
                return httpx.Response(503)
            return _sse_response(
                [
                    _sse_chunk(content="ok", finish_reason="stop"),
                    _sse_usage_chunk(5, 2),
                ]
            )

        client = _build_client(handler)
        result = client.complete([Message(role="user", content="x")])
        assert result.content == "ok"
        assert call_count["n"] == 2

    def test_connect_error_retries(self) -> None:
        """ConnectError 应重试。"""
        call_count = {"n": 0}

        def handler(_req: httpx.Request) -> httpx.Response:
            call_count["n"] += 1
            raise httpx.ConnectError("connection refused")

        client = _build_client(handler)
        with pytest.raises(httpx.ConnectError):
            client.complete([Message(role="user", content="x")])
        assert call_count["n"] == 3

    def test_read_timeout_retries(self) -> None:
        """ReadTimeout 应重试。"""
        call_count = {"n": 0}

        def handler(_req: httpx.Request) -> httpx.Response:
            call_count["n"] += 1
            raise httpx.ReadTimeout("response timeout")

        client = _build_client(handler)
        with pytest.raises(httpx.ReadTimeout):
            client.complete([Message(role="user", content="x")])
        assert call_count["n"] == 3

    def test_sse_stream_error_retries(self) -> None:
        """SSEStreamError(未见 [DONE])应进 retry,3 次后抛。"""
        call_count = {"n": 0}

        def handler(_req: httpx.Request) -> httpx.Response:
            call_count["n"] += 1
            # 故意不发 [DONE]
            return httpx.Response(
                200,
                content=_sse_bytes([_sse_chunk(content="x", finish_reason="stop")]),
                headers={"content-type": "text/event-stream"},
            )

        client = _build_client(handler)
        with pytest.raises(occ.SSEStreamError):
            client.complete([Message(role="user", content="x")])
        assert call_count["n"] == 3


# ---- TestClose --------------------------------------------------------------


class TestClose:
    def test_close_closes_owned_http_client(self) -> None:
        """构造时自创建的 httpx.Client 应在 close 时关闭。"""
        client = occ.OpenAICompatibleClient(
            model="qwen3.7-max",
            base_url=_DEFAULT_BASE_URL,
            api_key=SecretStr("sk-x"),
        )
        assert not client._http.is_closed
        client.close()
        assert client._http.is_closed

    def test_close_does_not_close_external_http_client(self) -> None:
        """外部注入的 http_client 由 caller 自管,close 不关。"""
        external = httpx.Client(
            transport=httpx.MockTransport(lambda _req: _sse_response([]))
        )
        client = occ.OpenAICompatibleClient(
            model="qwen3.7-max",
            base_url=_DEFAULT_BASE_URL,
            api_key=SecretStr("sk-x"),
            http_client=external,
        )
        client.close()
        assert not external.is_closed
        external.close()  # caller 自己关


class TestAuthErrorFailFast:
    """401 认证失败与 403 权限/配额拒绝均 fail-fast，但语义分离。

    维护人场景=单一云端 provider 主动选择、不要自动降级换 provider。故 key
    无效/失效(401)时正确行为是包装成清晰人话错误 fail-fast,而非裸
    HTTPStatusError traceback、也非自动切别的 provider。
    """

    def test_401_raises_clear_llm_auth_error_no_retry(self) -> None:
        calls = {"n": 0}

        def handler(_req: httpx.Request) -> httpx.Response:
            calls["n"] += 1
            return httpx.Response(401, json={"error": {"message": "invalid api key"}})

        client = _build_client(handler)
        with pytest.raises(LLMAuthError) as ei:
            client.complete([Message(role="user", content="hi")])
        msg = str(ei.value)
        assert "认证失败" in msg and "API key" in msg
        assert "openai_compatible" in msg  # 报清楚是哪个 provider
        assert calls["n"] == 1, "401 不应重试(4xx 立即 fail-fast)"

    def test_403_preserves_safe_provider_code_without_calling_it_bad_key(
        self,
    ) -> None:
        calls = {"n": 0}

        def handler(_req: httpx.Request) -> httpx.Response:
            calls["n"] += 1
            return httpx.Response(
                403,
                json={
                    "code": "AllocationQuota.FreeTierOnly",
                    "message": "Free quota exhausted",
                    "request_id": "req-provider-403",
                },
            )

        client = _build_client(handler)
        with pytest.raises(LLMAccessError) as ei:
            client.complete([Message(role="user", content="hi")])
        error = ei.value
        assert error.status_code == 403
        assert error.provider_code == "AllocationQuota.FreeTierOnly"
        assert error.provider_message == "Free quota exhausted"
        assert error.provider_request_id == "req-provider-403"
        assert "拒绝访问" in str(error)
        assert "不等同于 API key 失效" in str(error)
        assert calls["n"] == 1, "403 不应自动重试"

    def test_403_nested_error_fields_are_bounded_and_credentials_redacted(
        self,
    ) -> None:
        def handler(_req: httpx.Request) -> httpx.Response:
            return httpx.Response(
                403,
                json={
                    "error": {
                        "code": "AccessDenied",
                        "message": "Authorization: Bearer sk-secret123456 denied",
                        "requestId": "req-nested",
                    }
                },
            )

        client = _build_client(handler)
        with pytest.raises(LLMAccessError) as ei:
            client.complete([Message(role="user", content="hi")])
        error = ei.value
        assert error.provider_code == "AccessDenied"
        assert error.provider_request_id == "req-nested"
        assert "sk-secret123456" not in str(error)
        assert "[REDACTED]" in str(error)

    def test_500_still_httpx_error_not_auth(self) -> None:
        """5xx 不被误包成 LLMAuthError/LLMAccessError。"""

        def handler(_req: httpx.Request) -> httpx.Response:
            return httpx.Response(500, json={"error": {"message": "boom"}})

        client = _build_client(handler)
        with pytest.raises(httpx.HTTPStatusError):
            client.complete([Message(role="user", content="hi")])
