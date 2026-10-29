"""Writer / agent 模型 Profile(对应 PROJECT_SPEC §3.7 + ROADMAP §P3 决策 1)。

P3-B-01 引入。本模块仅定义 ``WriterProfile`` Pydantic 模型,不实现 LLMClient
构造逻辑(留 P3-B-04 ``factory.py`` + P3-B-05 ``ModelRouter``)。

类 A 冲突登记(P3-plan §1 + P3-B-01 决策 6 一致性)
=================================================
PROJECT_SPEC §3.7 字面 ``provider: Literal["claude", "openai_compatible"]``;
PROJECT_SPEC §7 example yaml ``provider: openai_compatible base_url: ...``。
P3-plan 决策 6 选 A(走 ollama 原生 API + ``think:false`` 顶层参数,非 OpenAI
兼容路径,因 ``think`` 字段为 ollama 原生扩展,OpenAI 兼容路径不支持);
本模块 ``provider`` 字段实际枚举为 ``["claude", "ollama", "mock"]``,与 spec
§3.7 字面不严格对齐。属于"代码命名忠于实现"——impl 文档登记类 A,v2 wishlist
登记 spec 在 v2 同步更新建议。

字段语义对齐
============
- ``provider``:LLM provider 枚举,与 ``Settings.llm_provider`` 取值集对齐
  (mock / claude / ollama)。决定 ``factory.make_llm_client_from_profile``
  分发到哪个 client 子类。
- ``model``:provider 视角下的模型名(Anthropic 是 ``claude-sonnet-4-6``,
  ollama 是 ``qwen-novel`` 等本地 tag)。
- ``base_url``:仅 ollama / OpenAI 兼容路径用,Claude 走 SDK 默认 endpoint
  (``api.anthropic.com``),此字段为 ``None``。
- ``context_window``:模型上下文窗口长度,主要供 Reranker / 长 prompt 截断
  策略参考(P3-B-08 +)。
- ``generation_params``:温度 / max_tokens / top_p / top_k 等生成参数,直接
  透传给具体 client 的 ``complete(**gen_params)``。
- ``extra_request_kwargs``:**P3-B-01 新增字段(spec §3.7 无)**,容纳非
  OpenAI 标准的 provider 扩展字段和 client-local 请求护栏。ollama 路径用
  ``think: False``；Qwen 路径的 ``input_token_budget`` 由本地 client 消费，
  不会发送给 Provider。
- ``cost_per_1k_input`` / ``cost_per_1k_output`` + ``cost_currency``:配置
  估算价与原币种。物理 attempt 账本按原币种保存；只有 USD profile 继续
  进入 legacy ``chapters.total_cost_usd`` 投影。本地 ollama 默认 0.0。
"""

from __future__ import annotations

from typing import Any, Literal

from pydantic import BaseModel, Field, field_validator, model_validator

from src.core.llm.openai_compatible_endpoint import (
    resolve_openai_compatible_endpoint,
)


class WriterProfile(BaseModel):
    """LLM Profile 配置(对齐 PROJECT_SPEC §3.7,P3-B-01 引入)。

    每个 profile 描述一个具体 provider + 模型组合 + 生成参数 + 成本表;
    ``ModelRouter``(P3-B-05)按 ``RoutingConfig`` 规则把每次 LLM 调用
    映射到具体 profile,再由 ``factory.make_llm_client_from_profile``
    实例化对应 ``LLMClient`` 子类。
    """

    profile_id: str = Field(
        ...,
        description="Profile 唯一 ID,与 ``RoutingConfig.default`` / ``rules.profile`` 引用对齐",
        min_length=1,
        max_length=64,
    )
    provider: Literal["claude", "ollama", "openai_compatible", "mock"] = Field(
        ...,
        description=(
            "LLM provider 枚举,与 ``Settings.llm_provider`` 取值集对齐。"
            "``claude``(Anthropic SDK)/ ``ollama``(P3-B-01,本地原生 API,"
            "thinking 字段为 ollama 原生扩展)/ ``openai_compatible``(v1.0.x"
            " post-wave5 H1,覆盖 dashscope qwen3.8-max / DeepSeek / Moonshot 等"
            "所有 OpenAI 兼容 API)/ ``mock``(无 API key 默认)。"
            "**v1.1 w3-s8 F2**:spec §3.7 字面已同步为本 4 档枚举(原仅列"
            " ``claude`` / ``openai_compatible`` 2 档)。"
        ),
    )
    model: str = Field(
        ...,
        description="provider 视角下的模型名,如 claude-sonnet-4-6 / qwen-novel",
        min_length=1,
    )
    base_url: str | None = Field(
        default=None,
        description=(
            "非默认 endpoint 时填(ollama 默认 http://localhost:11434);"
            "Claude 走 SDK 默认 endpoint 时为 None;openai_compatible provider"
            "**必填**(如 dashscope ``https://dashscope.aliyuncs.com/compatible-mode/v1``)"
        ),
    )
    context_window: int = Field(
        ...,
        ge=1,
        description="模型上下文窗口长度(tokens),供 Reranker / 长 prompt 截断策略参考",
    )
    generation_params: dict[str, Any] = Field(
        default_factory=dict,
        description="温度 / max_tokens / top_p / top_k 等生成参数,透传给 ``LLMClient.complete``",
    )
    extra_request_kwargs: dict[str, Any] = Field(
        default_factory=dict,
        description=(
            "P3-B-01 新增字段(spec §3.7 无)。容纳 provider 扩展字段;"
            "ollama 路径用此字段透传 ``think: False``(对齐 docs/local_llm_config.md)。"
            "Qwen ``input_token_budget`` 是 client-local 写前护栏,不会进入 HTTP body。"
            "Claude provider 此字段保持空 dict。"
        ),
    )
    cost_per_1k_input: float = Field(
        default=0.0,
        ge=0.0,
        description="每 1k 输入 token 的配置估算。本地推理默认 0.0",
    )
    cost_per_1k_output: float = Field(
        default=0.0,
        ge=0.0,
        description="每 1k 输出 token 的配置估算。本地推理默认 0.0",
    )
    cost_currency: str = Field(
        default="USD",
        pattern=r"^[A-Z]{3}$",
        description=(
            "cost_per_1k_input/output 的 ISO 4217 三字母币种。只标记币种，"
            "不做汇率换算、折扣或实际账单推断"
        ),
    )
    notes: str = Field(
        default="",
        description="自由文本说明,如 'finale 章用此 profile' 等,供 ConfigPage 展示",
    )

    @field_validator("cost_currency", mode="before")
    @classmethod
    def _normalize_cost_currency(cls, value: Any) -> Any:
        return value.strip().upper() if isinstance(value, str) else value

    @model_validator(mode="after")
    def _validate_provider_specific(self) -> "WriterProfile":
        """provider 与字段组合的弱一致性检查。

        - ollama provider:``base_url`` 可 None(走 OllamaClient 默认
          ``http://localhost:11434``)或显式覆盖;空字符串非法。
        - claude provider:``base_url`` 不应填(被 anthropic SDK 忽略);
          软检查仅排除空字符串,``None`` 合法。
        - **openai_compatible provider**:``base_url`` **必填且非空**
          (v1.0.x post-wave5 H1 引入)— 该 provider 是"通用 OpenAI 兼容
          endpoint",没有内置默认 endpoint,WriterProfile 必须显式声明
          (dashscope / DeepSeek / Moonshot 各自的 base_url 不同)。

        强校验(如 API key 是否设置)在 ``factory.make_llm_client_from_profile``
        / ``OpenAICompatibleClient.__init__`` 时落地,本模块只管字段静态约束。
        """
        if self.base_url is not None and self.base_url.strip() == "":
            raise ValueError(
                f"WriterProfile[{self.profile_id}] base_url 不允许为空字符串;"
                "若不需要覆盖默认 endpoint 请传 None"
            )
        if self.provider == "openai_compatible" and (
            self.base_url is None or self.base_url.strip() == ""
        ):
            raise ValueError(
                f"WriterProfile[{self.profile_id}] provider=openai_compatible 时"
                " base_url 必填(该 provider 无内置默认 endpoint;请显式声明"
                " 如 https://dashscope.aliyuncs.com/compatible-mode/v1)"
            )
        if self.provider == "openai_compatible" and self.base_url is not None:
            resolve_openai_compatible_endpoint(self.base_url)

        request_params = dict(self.generation_params)
        request_params.update(self.extra_request_kwargs)
        input_token_budget = request_params.get("input_token_budget")
        if input_token_budget is not None:
            if (
                not isinstance(input_token_budget, int)
                or isinstance(input_token_budget, bool)
                or input_token_budget <= 0
            ):
                raise ValueError(
                    f"WriterProfile[{self.profile_id}] input_token_budget 必须为正整数"
                )
            if input_token_budget > self.context_window:
                raise ValueError(
                    f"WriterProfile[{self.profile_id}] input_token_budget="
                    f"{input_token_budget} 不得超过 context_window={self.context_window}"
                )
            if self.provider != "openai_compatible":
                raise ValueError(
                    f"WriterProfile[{self.profile_id}] input_token_budget 目前只由"
                    " openai_compatible client 本地消费"
                )

        thinking_budget = request_params.get("thinking_budget")
        reasoning_effort = request_params.get("reasoning_effort")
        if thinking_budget is not None and reasoning_effort is not None:
            raise ValueError(
                f"WriterProfile[{self.profile_id}] thinking_budget 与 reasoning_effort"
                " 不得同时设置"
            )
        if thinking_budget is not None:
            if (
                not isinstance(thinking_budget, int)
                or isinstance(thinking_budget, bool)
                or not 0 <= thinking_budget <= 262_144
            ):
                raise ValueError(
                    f"WriterProfile[{self.profile_id}] thinking_budget 必须为"
                    " 0..262144 的整数"
                )
            if request_params.get("enable_thinking") is False and thinking_budget > 0:
                raise ValueError(
                    f"WriterProfile[{self.profile_id}] enable_thinking=false 时不得"
                    "配置正 thinking_budget"
                )

        # 这是保守的 profile 内部预算一致性检查，不替代 provider 的分模型硬上限。
        # 只在三项均为整数时执行，避免对其它兼容 Provider 的自定义参数作猜测。
        answer_budget = request_params.get("max_tokens")
        if (
            isinstance(input_token_budget, int)
            and not isinstance(input_token_budget, bool)
            and isinstance(answer_budget, int)
            and not isinstance(answer_budget, bool)
        ):
            total_reserved = input_token_budget + answer_budget
            if isinstance(thinking_budget, int) and not isinstance(
                thinking_budget, bool
            ):
                total_reserved += thinking_budget
            if total_reserved > self.context_window:
                raise ValueError(
                    f"WriterProfile[{self.profile_id}] input/thinking/answer 预算合计"
                    f" {total_reserved} 超过 context_window={self.context_window}"
                )
        return self
