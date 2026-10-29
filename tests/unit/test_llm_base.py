"""P1-B-07:LLMClient 基类与配套数据模型的单测。

不 mock provider 子类,真子类的端到端测试留给 P1-B-08 ClaudeClient。
"""

from __future__ import annotations

import pytest
from pydantic import ValidationError

from src.core.llm.base import LLMClient, LLMResponse, Message, TokenUsage


class TestLLMClientAbstract:
    """主验收:基类纯抽象,直接实例化必须失败。"""

    def test_instantiate_base_raises_type_error(self) -> None:
        with pytest.raises(TypeError):
            LLMClient()  # type: ignore[abstract]


class TestTokenUsage:
    def test_total_tokens_property(self) -> None:
        usage = TokenUsage(input_tokens=100, output_tokens=250)
        assert usage.total_tokens == 350

    def test_negative_tokens_rejected(self) -> None:
        with pytest.raises(ValidationError):
            TokenUsage(input_tokens=-1, output_tokens=10)
        with pytest.raises(ValidationError):
            TokenUsage(input_tokens=10, output_tokens=-1)


class TestMessage:
    def test_role_must_be_known(self) -> None:
        with pytest.raises(ValidationError):
            Message(role="tool", content="x")  # type: ignore[arg-type]


class TestLLMResponse:
    def test_construct_minimal(self) -> None:
        resp = LLMResponse(
            content="hello",
            model_used="claude-sonnet-4-6",
            usage=TokenUsage(input_tokens=10, output_tokens=5),
        )
        assert resp.finish_reason is None
        assert resp.usage.total_tokens == 15

    def test_missing_required_field_rejected(self) -> None:
        with pytest.raises(ValidationError):
            LLMResponse(  # type: ignore[call-arg]
                content="hello",
                usage=TokenUsage(input_tokens=10, output_tokens=5),
            )
