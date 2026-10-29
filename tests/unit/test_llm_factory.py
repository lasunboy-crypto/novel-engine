"""LLM 工厂单测(P1-B-22 起,P3-B-04 扩 ollama 分支,
post-wave5 H1 routing completion 扩 openai_compatible 分支 + 显式 provider 参数)。

按 ``provider`` 参数(post-wave5 H1 起)分发到 ``MockLLMClient`` / ``ClaudeClient`` /
``OllamaClient`` / ``OpenAICompatibleClient``;``provider`` 不传时 fallback
``settings.llm_provider`` 全局开关(向后兼容)。配 API key 时走 claude;无
key 时 ClaudeClient 自抛 ValueError。``ollama`` 分支可选透传 ``base_url``
(``None`` 走 OllamaClient 内部默认 ``http://localhost:11434``);
``openai_compatible`` 分支 base_url 必填(无默认 endpoint)。

测试用 ``monkeypatch.setattr(factory, "settings", ...)`` 注入临时 Settings
实例,**不复用模块级 settings 单例**(对齐 P1-B-01 沉淀:测试不复用 settings)。
"""

from __future__ import annotations

import json

import httpx
import pytest
from pydantic import SecretStr

from src.config import Settings
from src.core.llm import factory as factory_module
from src.core.llm.base import Message
from src.core.llm.claude_client import ClaudeClient
from src.core.llm.factory import make_llm_client
from src.core.llm.mock_client import MockLLMClient
from src.core.llm.ollama_client import OllamaClient
from src.core.llm.openai_compatible_client import OpenAICompatibleClient


def _build_settings(**env: str) -> Settings:
    """构造一份隔离的 Settings 实例,绕开 .env。"""
    import os

    for k, v in env.items():
        os.environ[k] = v
    return Settings(_env_file=None)  # type: ignore[call-arg]


@pytest.fixture
def clean_env(monkeypatch: pytest.MonkeyPatch) -> pytest.MonkeyPatch:
    for key in (
        "LLM_PROVIDER",
        "CLAUDE_API_KEY",
        "DASHSCOPE_API_KEY",
        "DEEPSEEK_API_KEY",
        "TOKENHUB_API_KEY",
        "ALIBABA_MODEL_STUDIO_WORKSPACE_ID",
        "OPENAI_COMPATIBLE_API_KEY",
        "API_HOST",
        "API_PORT",
        "API_AUTH_ENABLED",
        "API_AUTH_TOKEN",
    ):
        monkeypatch.delenv(key, raising=False)
    return monkeypatch


class TestMakeLLMClient:
    def test_default_provider_is_mock(
        self,
        clean_env: pytest.MonkeyPatch,
    ) -> None:
        # 无 LLM_PROVIDER 环境变量 → settings.llm_provider 默认 mock
        clean_env.setattr(factory_module, "settings", _build_settings())
        client = make_llm_client(model="claude-sonnet-4-6")
        assert isinstance(client, MockLLMClient)

    def test_explicit_mock_provider(
        self,
        clean_env: pytest.MonkeyPatch,
    ) -> None:
        clean_env.setattr(
            factory_module,
            "settings",
            _build_settings(LLM_PROVIDER="mock"),
        )
        client = make_llm_client(model="claude-sonnet-4-6")
        assert isinstance(client, MockLLMClient)

    def test_claude_provider_with_api_key(
        self,
        clean_env: pytest.MonkeyPatch,
    ) -> None:
        clean_env.setattr(
            factory_module,
            "settings",
            _build_settings(LLM_PROVIDER="claude", CLAUDE_API_KEY="sk-test"),
        )
        client = make_llm_client(model="claude-sonnet-4-6")
        assert isinstance(client, ClaudeClient)

    def test_claude_provider_without_api_key_raises(
        self,
        clean_env: pytest.MonkeyPatch,
    ) -> None:
        # LLM_PROVIDER=claude 但无 API key → ClaudeClient.__init__ 自抛
        clean_env.setattr(
            factory_module,
            "settings",
            _build_settings(LLM_PROVIDER="claude"),
        )
        with pytest.raises(ValueError, match="API key"):
            make_llm_client(model="claude-sonnet-4-6")

    def test_ollama_provider_default_base_url(
        self,
        clean_env: pytest.MonkeyPatch,
    ) -> None:
        # LLM_PROVIDER=ollama 不传 base_url → OllamaClient 走默认
        # http://localhost:11434(对齐 docs/local_llm_config.md 验证模板)。
        clean_env.setattr(
            factory_module,
            "settings",
            _build_settings(LLM_PROVIDER="ollama"),
        )
        client = make_llm_client(model="qwen-novel")
        assert isinstance(client, OllamaClient)
        assert client._model == "qwen-novel"
        assert client._base_url == "http://localhost:11434"

    def test_ollama_provider_explicit_base_url(
        self,
        clean_env: pytest.MonkeyPatch,
    ) -> None:
        # Router(P3-B-05)从 WriterProfile.base_url 透传时走此路径;
        # 工厂只需把 base_url 转给 OllamaClient,不做端点校验。
        clean_env.setattr(
            factory_module,
            "settings",
            _build_settings(LLM_PROVIDER="ollama"),
        )
        client = make_llm_client(
            model="qwen-novel",
            base_url="http://192.168.1.50:11434",
        )
        assert isinstance(client, OllamaClient)
        assert client._base_url == "http://192.168.1.50:11434"

    def test_ollama_provider_explicit_none_base_url_equals_default(
        self,
        clean_env: pytest.MonkeyPatch,
    ) -> None:
        # base_url=None 显式传入,等价不传(避免 OllamaClient 内部
        # rstrip("/") 收到 None 触发 AttributeError)。
        clean_env.setattr(
            factory_module,
            "settings",
            _build_settings(LLM_PROVIDER="ollama"),
        )
        client = make_llm_client(model="qwen-novel", base_url=None)
        assert isinstance(client, OllamaClient)
        assert client._base_url == "http://localhost:11434"

    def test_mock_provider_ignores_base_url(
        self,
        clean_env: pytest.MonkeyPatch,
    ) -> None:
        # base_url 仅 ollama 分支消费,mock / claude 显式忽略。
        # 这是 Router 接 factory 时不必关心 provider 的前置保证。
        clean_env.setattr(factory_module, "settings", _build_settings())
        client = make_llm_client(
            model="any",
            base_url="http://example.com:11434",
        )
        assert isinstance(client, MockLLMClient)

    def test_unknown_provider_raises(
        self,
        clean_env: pytest.MonkeyPatch,
    ) -> None:
        # 直接绕过 Settings 校验,模拟代码层的异常 provider 字符串
        # (实际上 Literal 拒绝非法值,这里用 mock settings 注入)。
        # ``"qwen"`` 在 P3-B-04 后仍是稳定的"工厂不识别"占位:Settings
        # Literal 已含 mock / claude / ollama / openai_compatible,``qwen``
        # 不在白名单。
        class _FakeSettings:
            llm_provider = "qwen"
            claude_api_key = SecretStr("sk-test")
            dashscope_api_key = SecretStr("")

        clean_env.setattr(factory_module, "settings", _FakeSettings())
        with pytest.raises(ValueError, match="未知的 provider"):
            make_llm_client(model="claude-sonnet-4-6")


# ---------- post-wave5 H1 routing completion 新增 ----------


class TestOpenAICompatibleProvider:
    """``openai_compatible`` provider 工厂分支(post-wave5 H1 第一阶段加入,
    单测原放 ``test_openai_compatible_client.py``;routing completion 起
    迁入工厂分发矩阵单测以对齐 4 client 全覆盖)。"""

    def test_openai_compatible_requires_base_url(
        self,
        clean_env: pytest.MonkeyPatch,
    ) -> None:
        # base_url 必填:无内置默认 endpoint;dashscope / DeepSeek / Moonshot
        # 等 OpenAI 兼容 provider 共用此分支,各自 endpoint 由 WriterProfile
        # 显式声明。
        clean_env.setattr(
            factory_module,
            "settings",
            _build_settings(
                LLM_PROVIDER="openai_compatible",
                DASHSCOPE_API_KEY="sk-dashscope-test",
            ),
        )
        with pytest.raises(ValueError, match="需要显式 base_url"):
            make_llm_client(model="qwen3.8-max")

    @pytest.mark.parametrize(
        "model",
        [
            "qwen3.8-max",
            "qwen3.7-max",
            "qwen3.7-max-2026-05-20",
            "qwen3.7-max-2026-06-08",
        ],
    )
    def test_openai_compatible_with_base_url(
        self,
        clean_env: pytest.MonkeyPatch,
        model: str,
    ) -> None:
        clean_env.setattr(
            factory_module,
            "settings",
            _build_settings(
                LLM_PROVIDER="openai_compatible",
                DASHSCOPE_API_KEY="sk-dashscope-test",
            ),
        )
        client = make_llm_client(
            model=model,
            base_url="https://dashscope.aliyuncs.com/compatible-mode/v1",
        )
        assert isinstance(client, OpenAICompatibleClient)
        assert client._default_enable_thinking is False
        assert client._default_preserve_thinking is False

    @pytest.mark.parametrize(
        "model",
        ["qwen3.7-max-preview", "qwen3.7-max-2026-05-17"],
    )
    def test_thinking_only_qwen_variant_is_not_forced_off(
        self,
        clean_env: pytest.MonkeyPatch,
        model: str,
    ) -> None:
        clean_env.setattr(
            factory_module,
            "settings",
            _build_settings(
                LLM_PROVIDER="openai_compatible",
                DASHSCOPE_API_KEY="sk-dashscope-test",
            ),
        )
        client = make_llm_client(
            model=model,
            base_url="https://dashscope.aliyuncs.com/compatible-mode/v1",
        )
        assert isinstance(client, OpenAICompatibleClient)
        assert client._default_enable_thinking is None
        assert client._default_preserve_thinking is None


class TestOpenAICompatibleKeyByBaseUrl:
    """v2 W3.5-S3(2026-06-24 Feng):openai_compatible 按 base_url 主机名选 key。

    deepseek 切官网 endpoint(api.deepseek.com)→ deepseek_api_key;dashscope qwen →
    dashscope_api_key。两 provider 各用各 key 不混(落地 config.py 注释的启发式;
    此前 factory 硬编码 dashscope_api_key,deepseek 切官网会拿错 key)。
    谁可能破坏:把 factory 改回硬编码单一 key,deepseek 官网调用拿 dashscope key 401。
    """

    def test_deepseek_base_url_uses_deepseek_key(
        self,
        clean_env: pytest.MonkeyPatch,
    ) -> None:
        clean_env.setattr(
            factory_module,
            "settings",
            _build_settings(
                LLM_PROVIDER="openai_compatible",
                DASHSCOPE_API_KEY="sk-dashscope-test",
                DEEPSEEK_API_KEY="sk-deepseek-test",
            ),
        )
        client = make_llm_client(
            model="deepseek-v4-pro",
            base_url="https://api.deepseek.com",
        )
        assert isinstance(client, OpenAICompatibleClient)
        assert client._default_enable_thinking is None
        assert client._default_preserve_thinking is None
        # 官网 deepseek base_url → 选 deepseek_api_key(非 dashscope)
        assert client._api_key.get_secret_value() == "sk-deepseek-test"

    def test_dashscope_base_url_uses_dashscope_key(
        self,
        clean_env: pytest.MonkeyPatch,
    ) -> None:
        clean_env.setattr(
            factory_module,
            "settings",
            _build_settings(
                LLM_PROVIDER="openai_compatible",
                DASHSCOPE_API_KEY="sk-dashscope-test",
                DEEPSEEK_API_KEY="sk-deepseek-test",
            ),
        )
        client = make_llm_client(
            model="qwen3.8-max",
            base_url="https://dashscope.aliyuncs.com/compatible-mode/v1",
        )
        assert isinstance(client, OpenAICompatibleClient)
        # qwen dashscope base_url → 仍选 dashscope_api_key(deepseek 切官网不影响 qwen)
        assert client._api_key.get_secret_value() == "sk-dashscope-test"

    def test_tokenhub_base_url_uses_only_tokenhub_key(
        self,
        clean_env: pytest.MonkeyPatch,
    ) -> None:
        clean_env.setattr(
            factory_module,
            "settings",
            _build_settings(
                LLM_PROVIDER="openai_compatible",
                TOKENHUB_API_KEY="sentinel-tokenhub",
                OPENAI_COMPATIBLE_API_KEY="sentinel-generic",
            ),
        )
        client = make_llm_client(
            model="hy3",
            base_url="https://tokenhub.tencentmaas.com/v1",
        )
        assert isinstance(client, OpenAICompatibleClient)
        assert client._api_key.get_secret_value() == "sentinel-tokenhub"

    def test_beijing_workspace_base_url_uses_dashscope_key(
        self,
        clean_env: pytest.MonkeyPatch,
    ) -> None:
        clean_env.setattr(
            factory_module,
            "settings",
            _build_settings(
                LLM_PROVIDER="openai_compatible",
                DASHSCOPE_API_KEY="sentinel-dashscope",
                ALIBABA_MODEL_STUDIO_WORKSPACE_ID="workspace-123",
                OPENAI_COMPATIBLE_API_KEY="sentinel-generic",
            ),
        )
        client = make_llm_client(
            model="qwen3.8-27b",
            base_url=(
                "https://workspace-123.cn-beijing.maas.aliyuncs.com/"
                "compatible-mode/v1"
            ),
        )
        assert isinstance(client, OpenAICompatibleClient)
        assert client._api_key.get_secret_value() == "sentinel-dashscope"

    @pytest.mark.parametrize("reserved", ["trial", "token-plan"])
    def test_alibaba_shared_host_label_cannot_be_workspace_authority(
        self,
        clean_env: pytest.MonkeyPatch,
        reserved: str,
    ) -> None:
        with pytest.raises(ValueError, match="保留域名"):
            _build_settings(
                ALIBABA_MODEL_STUDIO_WORKSPACE_ID=reserved,
            )

    @pytest.mark.parametrize(
        "base_url",
        [
            "https://example.invalid/v1",
            "https://deepseek.example.invalid/v1",
            "https://dashscope.aliyuncs.com.evil.invalid/v1",
            "https://tokenhub.tencentmaas.com.evil.invalid/v1",
            "https://workspace-123.cn-shanghai.maas.aliyuncs.com/compatible-mode/v1",
            "https://workspace-other.cn-beijing.maas.aliyuncs.com/compatible-mode/v1",
            "https://trial.cn-beijing.maas.aliyuncs.com/compatible-mode/v1",
            "https://xn--fsqu00a.xn--0zwm56d/v1",
        ],
    )
    def test_custom_or_deceptive_host_uses_only_generic_key(
        self,
        clean_env: pytest.MonkeyPatch,
        base_url: str,
    ) -> None:
        clean_env.setattr(
            factory_module,
            "settings",
            _build_settings(
                LLM_PROVIDER="openai_compatible",
                DASHSCOPE_API_KEY="sentinel-dashscope",
                DEEPSEEK_API_KEY="sentinel-deepseek",
                TOKENHUB_API_KEY="sentinel-tokenhub",
                ALIBABA_MODEL_STUDIO_WORKSPACE_ID="workspace-123",
                OPENAI_COMPATIBLE_API_KEY="sentinel-generic",
            ),
        )

        client = make_llm_client(model="custom-model", base_url=base_url)

        assert isinstance(client, OpenAICompatibleClient)
        assert client._api_key.get_secret_value() == "sentinel-generic"

    @pytest.mark.parametrize(
        ("base_url", "expected_key"),
        [
            (
                "HTTPS://DASHSCOPE.ALIYUNCS.COM.:443/compatible-mode/v1",
                "sentinel-dashscope",
            ),
            ("https://API.DEEPSEEK.COM.:443/v1", "sentinel-deepseek"),
            (
                "https://TOKENHUB.TENCENTMAAS.COM.:443/v1",
                "sentinel-tokenhub",
            ),
        ],
    )
    def test_official_host_canonical_forms_keep_dedicated_authority(
        self,
        clean_env: pytest.MonkeyPatch,
        base_url: str,
        expected_key: str,
    ) -> None:
        clean_env.setattr(
            factory_module,
            "settings",
            _build_settings(
                LLM_PROVIDER="openai_compatible",
                DASHSCOPE_API_KEY="sentinel-dashscope",
                DEEPSEEK_API_KEY="sentinel-deepseek",
                TOKENHUB_API_KEY="sentinel-tokenhub",
                OPENAI_COMPATIBLE_API_KEY="sentinel-generic",
            ),
        )

        client = make_llm_client(model="model", base_url=base_url)

        assert client._api_key.get_secret_value() == expected_key

    @pytest.mark.parametrize(
        "base_url",
        [
            "http://api.deepseek.com/v1",
            "https://api.deepseek.com:8443/v1",
            "https://user@example.invalid/v1",
            "https://example.invalid/v1?target=other",
            " https://example.invalid/v1",
            "https://exa mple.invalid/v1",
            "ftp://example.invalid/v1",
            "https:///v1",
        ],
    )
    def test_ambiguous_or_unsafe_endpoint_fails_before_client_creation(
        self,
        clean_env: pytest.MonkeyPatch,
        base_url: str,
    ) -> None:
        clean_env.setattr(
            factory_module,
            "settings",
            _build_settings(
                LLM_PROVIDER="openai_compatible",
                DASHSCOPE_API_KEY="sentinel-dashscope",
                DEEPSEEK_API_KEY="sentinel-deepseek",
                OPENAI_COMPATIBLE_API_KEY="sentinel-generic",
            ),
        )

        with pytest.raises(ValueError, match="base_url"):
            make_llm_client(model="model", base_url=base_url)

    def test_custom_endpoint_without_generic_key_fails_before_network(
        self,
        clean_env: pytest.MonkeyPatch,
    ) -> None:
        clean_env.setattr(
            factory_module,
            "settings",
            _build_settings(
                LLM_PROVIDER="openai_compatible",
                DASHSCOPE_API_KEY="sentinel-dashscope",
                DEEPSEEK_API_KEY="sentinel-deepseek",
            ),
        )

        with pytest.raises(ValueError, match="API key"):
            make_llm_client(
                model="custom-model",
                base_url="https://example.invalid/v1",
            )

    def test_custom_endpoint_final_authorization_header_uses_generic_sentinel(
        self,
        clean_env: pytest.MonkeyPatch,
    ) -> None:
        captured: dict[str, str | None] = {}

        def handler(request: httpx.Request) -> httpx.Response:
            captured["authorization"] = request.headers.get("Authorization")
            chunks = [
                {
                    "choices": [
                        {
                            "delta": {"content": "ok"},
                            "finish_reason": "stop",
                        }
                    ]
                },
                {
                    "choices": [],
                    "usage": {"prompt_tokens": 1, "completion_tokens": 1},
                },
            ]
            body = b"".join(
                f"data: {json.dumps(chunk)}\n\n".encode() for chunk in chunks
            ) + b"data: [DONE]\n\n"
            return httpx.Response(
                200,
                content=body,
                headers={"content-type": "text/event-stream"},
            )

        clean_env.setattr(
            factory_module,
            "settings",
            _build_settings(
                LLM_PROVIDER="openai_compatible",
                DASHSCOPE_API_KEY="sentinel-dashscope",
                DEEPSEEK_API_KEY="sentinel-deepseek",
                OPENAI_COMPATIBLE_API_KEY="sentinel-generic",
            ),
        )
        client = make_llm_client(
            model="custom-model",
            base_url="https://example.invalid/v1",
        )
        assert isinstance(client, OpenAICompatibleClient)
        client.close()
        client._http = httpx.Client(transport=httpx.MockTransport(handler))
        client._owns_http = True

        try:
            client.complete([Message(role="user", content="offline sentinel")])
        finally:
            client.close()

        assert captured["authorization"] == "Bearer sentinel-generic"


class TestExplicitProviderParameter:
    """post-wave5 H1 routing completion 引入:工厂签名加可选 ``provider``
    参数。Router.get_client 显式传 ``provider=profile.provider``,
    实现"多 provider 共存"路径 — 同进程内不同 profile 可绑不同 provider
    的 client,不再受 ``settings.llm_provider`` 全局开关限制。

    本类覆盖 4 client 全矩阵 × 显式 provider 参数 + settings 不匹配场景。
    """

    def test_explicit_mock_overrides_settings(
        self,
        clean_env: pytest.MonkeyPatch,
    ) -> None:
        # settings=ollama 但显式 provider=mock → 拿 MockLLMClient
        # (这正是 Router 在 luo3+luo-cloud-test 共存时,mock 测试路径
        # 不依赖 settings 的关键)
        clean_env.setattr(
            factory_module,
            "settings",
            _build_settings(LLM_PROVIDER="ollama"),
        )
        client = make_llm_client(model="anything", provider="mock")
        assert isinstance(client, MockLLMClient)

    def test_explicit_ollama_overrides_settings(
        self,
        clean_env: pytest.MonkeyPatch,
    ) -> None:
        # settings=openai_compatible(维护人 .env 配的云端开关)+ 显式
        # provider=ollama(luo3 工作区命中本地 profile)→ 拿 OllamaClient
        # 这是"多 provider 共存"的核心场景。
        clean_env.setattr(
            factory_module,
            "settings",
            _build_settings(
                LLM_PROVIDER="openai_compatible",
                DASHSCOPE_API_KEY="sk-test",
            ),
        )
        client = make_llm_client(
            model="qwen-novel",
            base_url="http://localhost:11434",
            provider="ollama",
        )
        assert isinstance(client, OllamaClient)
        assert client._base_url == "http://localhost:11434"

    def test_explicit_openai_compatible_overrides_settings(
        self,
        clean_env: pytest.MonkeyPatch,
    ) -> None:
        # settings=ollama(维护人 .env 残留本地开关)+ 显式 provider=
        # openai_compatible(luo-cloud-test 工作区命中云端 profile)→ 拿
        # OpenAICompatibleClient。这是 H1 routing completion 修复的核心
        # 场景:project.yaml routing 真生效,与 settings 全局开关解耦。
        clean_env.setattr(
            factory_module,
            "settings",
            _build_settings(
                LLM_PROVIDER="ollama",
                DASHSCOPE_API_KEY="sk-dashscope-test",
            ),
        )
        client = make_llm_client(
            model="qwen3.7-max",
            base_url="https://dashscope.aliyuncs.com/compatible-mode/v1",
            provider="openai_compatible",
        )
        assert isinstance(client, OpenAICompatibleClient)

    def test_explicit_claude_overrides_settings(
        self,
        clean_env: pytest.MonkeyPatch,
    ) -> None:
        # settings=mock + 显式 provider=claude(配 API key)→ 拿 ClaudeClient
        clean_env.setattr(
            factory_module,
            "settings",
            _build_settings(
                LLM_PROVIDER="mock",
                CLAUDE_API_KEY="sk-claude-test",
            ),
        )
        client = make_llm_client(
            model="claude-sonnet-4-6", provider="claude"
        )
        assert isinstance(client, ClaudeClient)

    def test_provider_none_fallbacks_to_settings(
        self,
        clean_env: pytest.MonkeyPatch,
    ) -> None:
        # 显式 provider=None(等价不传)→ 走 settings.llm_provider 全局
        # 开关,保证 cli/commands 等 bypass Router 的旧路径行为不变。
        clean_env.setattr(
            factory_module,
            "settings",
            _build_settings(LLM_PROVIDER="ollama"),
        )
        client = make_llm_client(model="qwen-novel", provider=None)
        assert isinstance(client, OllamaClient)

    def test_explicit_unknown_provider_raises(
        self,
        clean_env: pytest.MonkeyPatch,
    ) -> None:
        clean_env.setattr(
            factory_module,
            "settings",
            _build_settings(LLM_PROVIDER="mock"),
        )
        with pytest.raises(ValueError, match="未知的 provider"):
            make_llm_client(model="anything", provider="banana")
