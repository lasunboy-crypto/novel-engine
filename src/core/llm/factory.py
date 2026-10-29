"""LLM 客户端工厂(P1-B-22 起,P3-B-04 扩 ollama,v1.0.x post-wave5 H1 扩 openai_compatible,
post-wave5 H1 routing completion 起按 profile.provider 分发)。

按 ``provider`` 参数分发到 ``MockLLMClient`` / ``ClaudeClient`` /
``OllamaClient`` / ``OpenAICompatibleClient``;``provider`` 不传时 fallback
到 ``settings.llm_provider`` 全局开关(向后兼容 cli/commands bypass router
的简单路径)。api/cli 两处工厂
(``src/api/deps.py::get_chapter_service`` / ``src/cli/commands.py::make_chapter_service``)
共用本入口,避免重复。

派发路径演进(post-wave5 H1 routing completion)
================================================

**旧路径(P1-B-22 → post-wave5 H1 第一阶段)**:工厂只看 ``settings.llm_provider``
全局开关,与 ``WriterProfile.provider`` 字面值解耦 — Router 选中
``cloud_qwen_max_thinking``(provider=openai_compatible)时,只要
``settings.llm_provider=ollama``,工厂仍构造 ``OllamaClient``,profile
字面失效。Router 的 ``_maybe_fallback_for_provider_mismatch``(P7-B-06.5)
是补丁层 — 它检测 mismatch 后回退 default profile,但本质上仍是"单一全局
provider"假设。

**新路径(post-wave5 H1 routing completion 起)**:Router 在
``get_client`` 把 ``profile.provider`` 显式传给工厂;工厂按该参数派发,
不再受 ``settings.llm_provider`` 全局开关限制。**真正的多 provider
共存路径**(本地 ollama + 云端 dashscope 同时跑)由此打通。

``settings.llm_provider`` 在新路径下退化为:
- cli/commands 等 bypass Router 的简单 caller 用作"工厂缺省 provider"
- 测试场景未指定 ``provider`` 时的 fallback
- 不再作为"全局单一 provider"的硬开关

openai_compatible 分支的 API key 选择
=====================================
工厂与 Router/配置校验共用规范 endpoint registry：DashScope/DeepSeek 只有
精确官方 HTTPS hostname 才能取各自专用 key；其他合法兼容 endpoint 只能取
``openai_compatible_api_key``。不再用模型名、路径或松散 substring 决定凭据。
"""

from __future__ import annotations

from src.config import settings
from src.core.llm.base import LLMClient
from src.core.llm.claude_client import ClaudeClient
from src.core.llm.mock_client import MockLLMClient
from src.core.llm.ollama_client import OllamaClient
from src.core.llm.openai_compatible_client import OpenAICompatibleClient
from src.core.llm.openai_compatible_endpoint import (
    resolve_openai_compatible_endpoint,
)


_QWEN_HYBRID_THINKING_MODELS = frozenset({
    "qwen3.8-max",
    "qwen3.8-27b",
    "qwen3.7-max",
    "qwen3.7-max-2026-05-20",
    "qwen3.7-max-2026-06-08",
})


def make_llm_client(
    model: str,
    base_url: str | None = None,
    provider: str | None = None,
) -> LLMClient:
    """按 ``provider`` 参数构造 LLM 客户端;不传时 fallback ``settings.llm_provider``。

    :param model: 默认请求模型名。``mock`` provider 不消费此参数,仅用于
        ``LLMResponse.model_used`` 后缀;``claude`` provider 用于实际请求
        模型(如 ``claude-sonnet-4-6``);``ollama`` provider 用作
        ``/api/chat`` 请求体的 ``model`` 字段(如 ``qwen-novel``);
        ``openai_compatible`` provider 用作 OpenAI 兼容 body ``model`` 字段
        (如 ``qwen3.8-max``)。
    :param base_url: ``ollama`` provider 可选(``None`` 走默认
        ``http://localhost:11434``);``openai_compatible`` provider **必填**
        (无内置默认 endpoint,需 WriterProfile.base_url 显式声明)。``mock``
        / ``claude`` provider 忽略此参数。
    :param provider: 显式 provider 字面(``"mock"`` / ``"claude"`` /
        ``"ollama"`` / ``"openai_compatible"`` 之一)。**Router.get_client
        路径显式传 ``profile.provider``,实现多 provider 共存**;``None``
        时 fallback ``settings.llm_provider``(cli/commands 等 bypass Router
        路径)。
    :return: ``MockLLMClient``(``mock``)/ ``ClaudeClient``(``claude``)/
        ``OllamaClient``(``ollama``)/ ``OpenAICompatibleClient``
        (``openai_compatible``)。
    :raises ValueError: ``provider`` 字面未知;``openai_compatible``
        provider 未提供 ``base_url``;``claude`` / ``openai_compatible`` provider
        的 API key 校验失败由对应 client ``__init__`` 自抛。
    """
    actual_provider = provider if provider is not None else settings.llm_provider
    if actual_provider == "mock":
        return MockLLMClient()
    if actual_provider == "claude":
        # 显式从 settings 读 api_key 并 inject,避免 ClaudeClient 隐式
        # 依赖模块级 settings 单例(便于工厂层 monkeypatch settings 测试)。
        return ClaudeClient(model=model, api_key=settings.claude_api_key)
    if actual_provider == "ollama":
        # base_url 显式 None 时走 OllamaClient 默认值,避免传 None 触发
        # rstrip("/") AttributeError。``enable_thinking`` 不在工厂层暴露,
        # 由 Router(P3-B-05)在 ``complete()`` 调用时按 Profile.extra_request_kwargs
        # 翻译为单次 keyword(对齐 P3-B-03 impl §6 marker)。
        if base_url is None:
            return OllamaClient(model=model)
        return OllamaClient(model=model, base_url=base_url)
    if actual_provider == "openai_compatible":
        # OpenAI 兼容 provider 无内置默认 endpoint,base_url 必填。凭据由
        # 规范 hostname registry 精确决定，含糊 URL 在 client 构造前拒绝。
        # ``enable_thinking`` 不作为工厂入参暴露；Router 仍按
        # Profile.extra_request_kwargs 翻译为单次 keyword。下方只为明确的
        # Qwen hybrid 裸 client 设置安全默认值。
        if base_url is None:
            raise ValueError(
                "openai_compatible provider 需要显式 base_url(无内置默认"
                " endpoint);请通过 WriterProfile.base_url 显式声明,如"
                " https://dashscope.aliyuncs.com/compatible-mode/v1"
            )
        endpoint = resolve_openai_compatible_endpoint(
            base_url,
            alibaba_model_studio_workspace_id=(
                settings.alibaba_model_studio_workspace_id or None
            ),
        )
        api_key = getattr(settings, endpoint.settings_field)
        # Qwen hybrid Max 省略 enable_thinking 时由百炼默认开启思考；Qwen3.8
        # 还会默认 preserve_thinking=true。Router 会按 profile 在 complete()
        # 时显式覆盖；这里再保护绕过 Router 的历史脚本，使裸 factory client
        # 仍保持 standard/thinking-off，且不把思维链带入后续请求。
        # 其他未知 OpenAI-compatible 模型保持 None，不注入百炼专有扩展。
        default_enable_thinking = (
            False if model in _QWEN_HYBRID_THINKING_MODELS else None
        )
        default_preserve_thinking = (
            False if model in _QWEN_HYBRID_THINKING_MODELS else None
        )
        return OpenAICompatibleClient(
            model=model,
            base_url=base_url,
            api_key=api_key,
            enable_thinking=default_enable_thinking,
            preserve_thinking=default_preserve_thinking,
        )
    raise ValueError(f"未知的 provider: {actual_provider!r}")
