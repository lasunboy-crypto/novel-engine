# LLMClient 抽象基类(预留 Profile 化演进)
"""LLM 客户端抽象层。

本模块只定义 provider 无关的抽象基类与数据结构,具体 provider 实现见
`claude_client.py`(P1-B-08)及未来的 OpenAI 兼容客户端(P3)。

设计原则:
- `complete` 同步签名,P3 若需异步另加 `acomplete`,不破现签名。
- `Message` / `LLMResponse` 字段保持 provider 无关,不漏 Anthropic / OpenAI
  专有概念(system 参数、tool_use、stream 等)。
- `LLMResponse` 只透传 provider 直接给的原始数据;`latency_ms` / `cost_usd`
  由调用方/可观测层算,不入此结构(对齐 OBSERVABILITY §2.4 `llm_response`)。
"""

from __future__ import annotations

from abc import ABC, abstractmethod
from typing import Any, Literal

from pydantic import BaseModel, Field, PrivateAttr


# 三个正式 Provider client 共用的单次 logical call 物理尝试上限。并发调度器
# 必须从真正拥有 transport retry 的这一层读取，不能各自复制一个可能漂移的 3。
MAX_PROVIDER_ATTEMPTS_PER_LOGICAL_CALL = 3


class LLMAuthError(RuntimeError):
    """LLM provider 认证失败(API key 无效 / 失效 / 缺失)。

    v1.1 wave 4 B1 S1 ②(2026-06-03 维护人拍板):维护人场景 = 同一时间只用
    **一个**云端 provider、主动选择切换(买了千问用千问、换 deepseek 改配置),
    **明确不要"key 失效自动换另一个 provider"的降级行为**。故云端 key 失效时
    正确行为是 **fail-fast + 清晰报错**(本异常),让维护人自己定位 key 问题或
    主动切换,而非系统自作主张降级。各 :class:`LLMClient` 实现捕获底层认证错误
    (``httpx.HTTPStatusError`` 401 / ``anthropic.AuthenticationError``)后
    包装成本异常,附人话提示(哪个 provider + 怎么排查)。

    **与章节卡调度无关**:正式章节卡现在只走线性单卡；历史 beat 批量及其
    超窗口降级已经退役。provider 认证失败不得借机切换模型或改写章节卡调度。
    """

    def __init__(
        self,
        message: str,
        *,
        status_code: int | None = None,
        provider_code: str | None = None,
        provider_message: str | None = None,
        provider_request_id: str | None = None,
    ) -> None:
        self.status_code = status_code
        self.provider_code = provider_code
        self.provider_message = provider_message
        self.provider_request_id = provider_request_id
        super().__init__(message)


class LLMAccessError(LLMAuthError):
    """Provider 已收到凭据，但拒绝模型/工作区/配额访问。

    OpenAI 兼容端点的 HTTP 403 不能等同于“API key 无效”；
    它还可能表示模型或 workspace 权限、服务未开通、账户策略或
    配额限制。本异常保留 provider 返回的安全结构化字段，但不保留
    response headers/body，也不触发 provider 切换或自动降级。
    """


class Message(BaseModel):
    """LLM 对话消息(provider 无关)。

    role 取值统一限制在 system / user / assistant 三种;Anthropic 的 system
    单独参数化、OpenAI tool 角色等差异由具体 provider 子类在 `complete`
    内部转换,基类不感知。
    """

    role: Literal["system", "user", "assistant"]
    content: str


class TokenUsage(BaseModel):
    """token 使用统计。

    与 OBSERVABILITY §2.4 `llm_response` 事件的 `input_tokens` /
    `output_tokens` 字段对齐,供调用方写日志、算成本、累加 metrics。
    """

    input_tokens: int = Field(ge=0)
    output_tokens: int = Field(ge=0)

    @property
    def total_tokens(self) -> int:
        """input + output 之和,便利读出。"""
        return self.input_tokens + self.output_tokens


class LLMResponse(BaseModel):
    """LLM 调用结果(provider 无关)。

    `model_used` 必填:实际使用的模型名,可能因 fallback 与请求模型不同;
    日志、metrics 与成本核算都以此字段为准。

    `finish_reason` 不强制 enum:不同 provider 取值不同(Anthropic
    `end_turn` / `max_tokens` / `stop_sequence` / `tool_use`,OpenAI
    `stop` / `length` / `tool_calls` / `content_filter`),原样透传。
    """

    content: str
    model_used: str
    usage: TokenUsage
    finish_reason: str | None = None
    provider_request_id: str | None = Field(default=None, max_length=256)
    _reasoning_chars: int = PrivateAttr(default=0)

    @property
    def reasoning_chars(self) -> int:
        """Return observed reasoning length without serialising its contents.

        This is deliberately a private response observation rather than an API
        or persistence field.  Ordinary clients keep the default ``0``;
        compatible clients set it after separating ``reasoning_content`` from
        final content.
        """

        return self._reasoning_chars


class LLMClient(ABC):
    """LLM 客户端抽象基类。

    具体 provider 子类(`ClaudeClient`、未来的 OpenAI 兼容客户端)实现
    `complete`。基类不约束 `__init__` 签名,子类自行决定如何接收
    模型名、API key、Profile 等参数。
    """

    def prepare_strict_json_schema(
        self,
        *,
        model: str,
        schema_name: str,
        schema: dict[str, Any],
        gen_params: dict[str, Any],
    ) -> dict[str, Any]:
        """仅在 provider/模型明确支持时附加严格 JSON Schema。

        默认实现保持请求原样，避免把 OpenAI 兼容字段误发给
        Claude、Ollama 或尚未验证的兼容端点。已验证的客户端子类
        覆写本方法；调用方始终保留原 Pydantic/业务校验。
        """

        del model, schema_name, schema
        return dict(gen_params)

    @abstractmethod
    def complete(
        self,
        messages: list[Message],
        **gen_params: Any,
    ) -> LLMResponse:
        """同步生成补全。

        Args:
            messages: 对话消息序列;system 角色也通过此结构传入,由具体
                provider 自行转换为 provider 原生形态。
            **gen_params: 生成参数(`temperature`、`max_tokens`、`top_p`
                等),由具体 provider 解释,基类不限制可用键集。

        Returns:
            含正文、实际使用模型、token 使用量、结束原因的 `LLMResponse`。

        Raises:
            子类应在 provider 调用失败、超时、限流等情境下抛出明确异常;
            基类不规定具体异常类型(P1-B-17 起统一错误体系后再约束)。
        """
        ...
