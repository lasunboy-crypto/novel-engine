"""P1-B-08:ClaudeClient 单测。

不真调 anthropic API,完全 mock SDK。集成测试在 `tests/integration/`,默认
被 `addopts = "-m 'not integration'"` 排除,需要 `CLAUDE_API_KEY` + `-m
integration` 才跑。
"""

from __future__ import annotations

from types import SimpleNamespace
from typing import Any
from unittest.mock import MagicMock

import anthropic
import httpx
import pytest
from pydantic import SecretStr

from src.core.llm import claude_client as cc
from src.core.llm.base import LLMAuthError, Message


def _fake_response(
    *,
    text: str = "hello",
    model: str = "claude-sonnet-4-6-20250101",
    input_tokens: int = 10,
    output_tokens: int = 5,
    stop_reason: str = "end_turn",
    extra_blocks: list[Any] | None = None,
) -> SimpleNamespace:
    """伪造 anthropic SDK 返回的 Message 对象,只填本类用到的字段。"""
    blocks: list[Any] = [SimpleNamespace(type="text", text=text)]
    if extra_blocks:
        blocks.extend(extra_blocks)
    return SimpleNamespace(
        content=blocks,
        model=model,
        usage=SimpleNamespace(input_tokens=input_tokens, output_tokens=output_tokens),
        stop_reason=stop_reason,
    )


def _make_anthropic_exc(cls: type[BaseException], status_code: int | None = None):
    """用 `__new__` 绕过 anthropic 异常 __init__(避免构造 httpx.Response)。

    `_is_retryable` 只看 isinstance + 5xx APIStatusError 的 status_code,
    其它属性不参与判断。
    """
    exc = cls.__new__(cls)  # type: ignore[call-arg]
    if status_code is not None:
        exc.status_code = status_code  # type: ignore[attr-defined]
    return exc


@pytest.fixture
def mock_sdk(monkeypatch: pytest.MonkeyPatch) -> MagicMock:
    """替换 anthropic.Anthropic,使 ClaudeClient 拿到一个完全可控的 SDK。"""
    sdk = MagicMock()
    sdk.messages.create.return_value = _fake_response()
    monkeypatch.setattr(cc.anthropic, "Anthropic", MagicMock(return_value=sdk))
    return sdk


@pytest.fixture(autouse=True)
def _no_sleep(monkeypatch: pytest.MonkeyPatch) -> None:
    """重试退避不真睡眠,加速测试。"""
    monkeypatch.setattr(cc.time, "sleep", lambda _seconds: None)


class TestConstructor:
    def test_empty_api_key_rejected(self) -> None:
        with pytest.raises(ValueError, match="API key"):
            cc.ClaudeClient(model="claude-sonnet-4-6", api_key=SecretStr(""))

    def test_explicit_api_key_overrides_settings(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        captured: dict[str, Any] = {}

        def fake_anthropic(**kwargs: Any) -> MagicMock:
            captured.update(kwargs)
            return MagicMock()

        monkeypatch.setattr(cc.anthropic, "Anthropic", fake_anthropic)
        cc.ClaudeClient(model="claude-sonnet-4-6", api_key=SecretStr("explicit-key"))
        assert captured["api_key"] == "explicit-key"

    def test_falls_back_to_settings_api_key(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        captured: dict[str, Any] = {}

        def fake_anthropic(**kwargs: Any) -> MagicMock:
            captured.update(kwargs)
            return MagicMock()

        monkeypatch.setattr(cc.anthropic, "Anthropic", fake_anthropic)
        monkeypatch.setattr(
            cc.settings, "claude_api_key", SecretStr("settings-key")
        )
        cc.ClaudeClient(model="claude-sonnet-4-6")
        assert captured["api_key"] == "settings-key"


class TestSplitSystemAndTurns:
    def test_no_system_messages(self) -> None:
        messages = [
            Message(role="user", content="hi"),
            Message(role="assistant", content="yo"),
        ]
        system, turns = cc._split_system_and_turns(messages)
        assert system == ""
        assert turns == [
            {"role": "user", "content": "hi"},
            {"role": "assistant", "content": "yo"},
        ]

    def test_multiple_system_messages_joined(self) -> None:
        messages = [
            Message(role="system", content="rule A"),
            Message(role="system", content="rule B"),
            Message(role="user", content="go"),
        ]
        system, turns = cc._split_system_and_turns(messages)
        assert system == "rule A\n\nrule B"
        assert turns == [{"role": "user", "content": "go"}]

    def test_only_system_message(self) -> None:
        messages = [Message(role="system", content="alone")]
        system, turns = cc._split_system_and_turns(messages)
        assert system == "alone"
        assert turns == []


class TestExtractText:
    def test_concatenates_all_text_blocks(self) -> None:
        resp = _fake_response(
            text="part1",
            extra_blocks=[
                SimpleNamespace(type="text", text="part2"),
                SimpleNamespace(type="tool_use", id="x"),  # 非 text,过滤掉
            ],
        )
        assert cc._extract_text(resp) == "part1part2"

    def test_empty_content_returns_empty(self) -> None:
        resp = SimpleNamespace(content=[])
        assert cc._extract_text(resp) == ""


class TestIsRetryable:
    def test_rate_limit_error_retryable(self) -> None:
        exc = _make_anthropic_exc(anthropic.RateLimitError, status_code=429)
        assert cc._is_retryable(exc) is True

    def test_api_connection_error_retryable(self) -> None:
        exc = _make_anthropic_exc(anthropic.APIConnectionError)
        assert cc._is_retryable(exc) is True

    def test_5xx_status_error_retryable(self) -> None:
        exc = _make_anthropic_exc(anthropic.InternalServerError, status_code=503)
        assert cc._is_retryable(exc) is True

    def test_4xx_not_retryable(self) -> None:
        exc = _make_anthropic_exc(anthropic.BadRequestError, status_code=400)
        assert cc._is_retryable(exc) is False

    def test_authentication_error_not_retryable(self) -> None:
        exc = _make_anthropic_exc(anthropic.AuthenticationError, status_code=401)
        assert cc._is_retryable(exc) is False

    def test_unknown_exception_not_retryable(self) -> None:
        assert cc._is_retryable(ValueError("nope")) is False


class TestCompleteHappyPath:
    def test_returns_llm_response(self, mock_sdk: MagicMock) -> None:
        client = cc.ClaudeClient(
            model="claude-sonnet-4-6", api_key=SecretStr("test-key")
        )
        resp = client.complete([Message(role="user", content="ping")])

        assert resp.content == "hello"
        assert resp.model_used == "claude-sonnet-4-6-20250101"  # 用 SDK 返回的实际模型
        assert resp.usage.input_tokens == 10
        assert resp.usage.output_tokens == 5
        assert resp.usage.total_tokens == 15
        assert resp.finish_reason == "end_turn"

    def test_passes_default_max_tokens(self, mock_sdk: MagicMock) -> None:
        client = cc.ClaudeClient(
            model="claude-sonnet-4-6", api_key=SecretStr("test-key")
        )
        client.complete([Message(role="user", content="ping")])

        kwargs = mock_sdk.messages.create.call_args.kwargs
        assert kwargs["max_tokens"] == 4096
        assert kwargs["model"] == "claude-sonnet-4-6"
        assert kwargs["messages"] == [{"role": "user", "content": "ping"}]
        assert "system" not in kwargs  # 没传 system 就不该传该 kwarg

    def test_caller_overrides_max_tokens_and_model(
        self, mock_sdk: MagicMock
    ) -> None:
        client = cc.ClaudeClient(
            model="claude-sonnet-4-6", api_key=SecretStr("test-key")
        )
        client.complete(
            [Message(role="user", content="ping")],
            max_tokens=8000,
            model="claude-opus-4-7",
            temperature=0.85,
        )

        kwargs = mock_sdk.messages.create.call_args.kwargs
        assert kwargs["max_tokens"] == 8000
        assert kwargs["model"] == "claude-opus-4-7"
        assert kwargs["temperature"] == 0.85

    def test_system_message_pulled_to_top_level(self, mock_sdk: MagicMock) -> None:
        client = cc.ClaudeClient(
            model="claude-sonnet-4-6", api_key=SecretStr("test-key")
        )
        client.complete(
            [
                Message(role="system", content="you are a writer"),
                Message(role="user", content="write a chapter"),
            ]
        )

        kwargs = mock_sdk.messages.create.call_args.kwargs
        assert kwargs["system"] == "you are a writer"
        assert kwargs["messages"] == [
            {"role": "user", "content": "write a chapter"}
        ]


class TestCompleteRetry:
    def test_retries_then_succeeds(self, mock_sdk: MagicMock) -> None:
        rate_limit = _make_anthropic_exc(anthropic.RateLimitError, status_code=429)
        mock_sdk.messages.create.side_effect = [
            rate_limit,
            rate_limit,
            _fake_response(text="finally"),
        ]
        client = cc.ClaudeClient(
            model="claude-sonnet-4-6", api_key=SecretStr("test-key")
        )

        resp = client.complete([Message(role="user", content="ping")])

        assert resp.content == "finally"
        assert mock_sdk.messages.create.call_count == 3

    def test_retries_exhausted_raises_last(self, mock_sdk: MagicMock) -> None:
        rate_limit = _make_anthropic_exc(anthropic.RateLimitError, status_code=429)
        mock_sdk.messages.create.side_effect = [rate_limit, rate_limit, rate_limit]
        client = cc.ClaudeClient(
            model="claude-sonnet-4-6", api_key=SecretStr("test-key")
        )

        with pytest.raises(anthropic.RateLimitError):
            client.complete([Message(role="user", content="ping")])
        assert mock_sdk.messages.create.call_count == 3

    def test_non_retryable_raises_immediately(self, mock_sdk: MagicMock) -> None:
        bad_request = _make_anthropic_exc(
            anthropic.BadRequestError, status_code=400
        )
        mock_sdk.messages.create.side_effect = bad_request
        client = cc.ClaudeClient(
            model="claude-sonnet-4-6", api_key=SecretStr("test-key")
        )

        with pytest.raises(anthropic.BadRequestError):
            client.complete([Message(role="user", content="ping")])
        assert mock_sdk.messages.create.call_count == 1  # 不重试,只调一次

    def test_5xx_is_retried(self, mock_sdk: MagicMock) -> None:
        server_error = _make_anthropic_exc(
            anthropic.InternalServerError, status_code=503
        )
        mock_sdk.messages.create.side_effect = [
            server_error,
            _fake_response(text="ok"),
        ]
        client = cc.ClaudeClient(
            model="claude-sonnet-4-6", api_key=SecretStr("test-key")
        )

        resp = client.complete([Message(role="user", content="ping")])
        assert resp.content == "ok"
        assert mock_sdk.messages.create.call_count == 2


class TestAuthErrorFailFast:
    """S1 ②(2026-06-03 维护人拍板):claude 认证失败 → fail-fast 清晰 LLMAuthError。

    维护人场景=单一云端 provider 主动选择、不要自动降级换 provider。key 无效/
    失效时正确行为是包装成清晰人话错误 fail-fast,而非裸 anthropic.
    AuthenticationError、也非自动切别的 provider。
    """

    def test_authentication_error_wrapped(self, mock_sdk: MagicMock) -> None:
        req = httpx.Request("POST", "https://api.anthropic.com/v1/messages")
        resp = httpx.Response(401, request=req)
        mock_sdk.messages.create.side_effect = anthropic.AuthenticationError(
            "invalid x-api-key", response=resp, body=None
        )
        client = cc.ClaudeClient(
            model="claude-sonnet-4-6", api_key=SecretStr("bad-key")
        )
        with pytest.raises(LLMAuthError) as ei:
            client.complete([Message(role="user", content="hi")])
        msg = str(ei.value)
        assert "认证失败" in msg and "API key" in msg
        assert "claude" in msg  # 报清楚是哪个 provider
        # 401 不重试(4xx 立即 fail-fast)
        assert mock_sdk.messages.create.call_count == 1
