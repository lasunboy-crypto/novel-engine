"""P3-B-05 ModelRouter + 受限 AST 规则求值单测。

测试矩阵
========

- :func:`evaluate_rule`:合法 ``==`` / ``>=`` / ``in`` / ``and`` / ``or`` /
  ``not`` 求值;非法 AST 节点(``ast.Call`` / ``ast.Attribute`` / ``ast.BinOp``)
  拒绝;未声明字段拒绝;语法错拒绝;结果非 bool 拒绝。
- :class:`ModelRouter` Writer 路径:``is_arc_finale=True`` 命中 cloud_premium /
  ``tension_level=10`` 命中 cloud_premium / 默认 ChapterCard 走 default profile /
  无 ChapterCard 时走 ``agent_routing.writer`` 兜底。
- :class:`ModelRouter` agent 维度路由(决策 1 选 B):reviewer / memory_extractor
  按 ``agent_routing`` 命中各自 profile;未配置的 ``reviewer_escalation``
  回退到 ``default``。
- LLMClient 缓存:同一 profile 二次 ``get_client`` 复用同一实例;不同
  profile 触发新工厂调用。
- ``build_complete_kwargs`` 翻译路径:OllamaClient 路径 ``think`` →
  ``enable_thinking``;MockLLMClient 路径 ``think`` 不翻译留在 dict 中
  (P3-B-04 marker §6 守护)。
- ``override_profile_id`` 路径(P3-F-03 单次写作覆盖入口)。
"""

from __future__ import annotations

import json
from collections.abc import Iterator
from concurrent.futures import ThreadPoolExecutor
from threading import Barrier, Lock
from time import sleep
from typing import Any
from unittest.mock import MagicMock

import httpx
import pytest
from pydantic import SecretStr

from src.core.llm.base import LLMClient, LLMResponse, Message
from src.core.llm.mock_client import MockLLMClient
from src.core.llm.ollama_client import OllamaClient
from src.core.llm.openai_compatible_client import OpenAICompatibleClient
from src.core.models.routing import (
    RoutingConfig,
    RoutingRule,
    WriterProfilesConfig,
)
from src.core.models.writer_profile import WriterProfile
from src.core.routing import ModelRouter, RoutedTarget, RuleEvalError, evaluate_rule


# ---------- evaluate_rule 单测 ----------


class TestEvaluateRule:
    def test_eq_true(self) -> None:
        assert evaluate_rule("is_arc_finale == True", {"is_arc_finale": True}) is True

    def test_eq_false(self) -> None:
        assert evaluate_rule("is_arc_finale == True", {"is_arc_finale": False}) is False

    def test_neq(self) -> None:
        assert evaluate_rule("arc_phase != 'opening'", {"arc_phase": "climax"}) is True

    def test_gte(self) -> None:
        assert evaluate_rule("tension_level >= 9", {"tension_level": 10}) is True
        assert evaluate_rule("tension_level >= 9", {"tension_level": 8}) is False

    def test_lt(self) -> None:
        assert evaluate_rule("tension_level < 5", {"tension_level": 3}) is True

    def test_in_list(self) -> None:
        assert (
            evaluate_rule(
                "arc_phase in ['climax', 'falling']",
                {"arc_phase": "climax"},
            )
            is True
        )
        assert (
            evaluate_rule(
                "arc_phase in ['climax', 'falling']",
                {"arc_phase": "opening"},
            )
            is False
        )

    def test_not_in(self) -> None:
        assert (
            evaluate_rule(
                "arc_phase not in ['opening']",
                {"arc_phase": "rising"},
            )
            is True
        )

    def test_bool_and(self) -> None:
        ctx = {"is_arc_finale": True, "tension_level": 10}
        assert (
            evaluate_rule("is_arc_finale == True and tension_level >= 9", ctx) is True
        )

    def test_bool_or(self) -> None:
        ctx = {"is_arc_finale": False, "tension_level": 10}
        assert evaluate_rule("is_arc_finale == True or tension_level >= 9", ctx) is True

    def test_unary_not(self) -> None:
        assert evaluate_rule("not is_arc_finale", {"is_arc_finale": False}) is True

    def test_reject_function_call(self) -> None:
        with pytest.raises(RuleEvalError, match="非法 AST 节点"):
            evaluate_rule("len(must_achieve) > 0", {"must_achieve": [1]})

    def test_reject_attribute_access(self) -> None:
        with pytest.raises(RuleEvalError, match="非法 AST 节点"):
            evaluate_rule("card.is_arc_finale", {"card": object()})

    def test_reject_arithmetic(self) -> None:
        with pytest.raises(RuleEvalError, match="非法 AST 节点"):
            evaluate_rule("tension_level + 1 > 5", {"tension_level": 5})

    def test_reject_unknown_field(self) -> None:
        with pytest.raises(RuleEvalError, match="未声明字段"):
            evaluate_rule("ghost_field == True", {"is_arc_finale": True})

    def test_reject_syntax_error(self) -> None:
        with pytest.raises(RuleEvalError, match="语法错"):
            evaluate_rule("is_arc_finale ==", {"is_arc_finale": True})

    def test_reject_non_bool_result(self) -> None:
        # 字段名引用本身求值为 int → 非 bool 结果
        with pytest.raises(RuleEvalError, match="结果应为 bool"):
            evaluate_rule("tension_level", {"tension_level": 10})


# ---------- ModelRouter 测试 fixture ----------


def _make_profiles_config() -> WriterProfilesConfig:
    """构造 5-profile 配置(对齐 ``config.yaml`` 字面缩减版)。"""
    profiles = {
        "local_qwen_32k": WriterProfile(
            profile_id="local_qwen_32k",
            provider="ollama",
            model="qwen-novel",
            base_url="http://localhost:11434",
            context_window=24576,
            generation_params={"temperature": 0.7, "num_predict": 4096},
            extra_request_kwargs={"think": False},
        ),
        "local_qwen_safe": WriterProfile(
            profile_id="local_qwen_safe",
            provider="ollama",
            model="qwen-novel",
            base_url="http://localhost:11434",
            context_window=16000,
            generation_params={"temperature": 0.5, "num_predict": 2048},
            extra_request_kwargs={"think": False},
        ),
        "cloud_quality": WriterProfile(
            profile_id="cloud_quality",
            provider="claude",
            model="claude-sonnet-4-6",
            context_window=200000,
            generation_params={"temperature": 0.85, "max_tokens": 6000},
        ),
        "cloud_premium": WriterProfile(
            profile_id="cloud_premium",
            provider="claude",
            model="claude-opus-4-7",
            context_window=200000,
            generation_params={"temperature": 0.85, "max_tokens": 8000},
        ),
        "mock_default": WriterProfile(
            profile_id="mock_default",
            provider="mock",
            model="mock-default",
            context_window=16000,
        ),
    }
    routing = RoutingConfig(
        default="local_qwen_32k",
        rules=[
            RoutingRule(condition="is_arc_finale == True", profile="cloud_premium"),
            RoutingRule(condition="tension_level >= 9", profile="cloud_premium"),
        ],
        agent_routing={
            "writer": "local_qwen_32k",
            "reviewer": "local_qwen_32k",
            "reviser": "local_qwen_32k",
            "memory_extractor": "local_qwen_safe",
            "continuity": "local_qwen_safe",
            "reranker": "local_qwen_safe",
            # reviewer_escalation 故意不填(决策 4 选 A 默认 disable)
        },
    )
    return WriterProfilesConfig(profiles=profiles, routing=routing)


def _chapter_card_dict(**overrides: Any) -> dict[str, Any]:
    """ChapterCard 字段 dict(规则求值上下文)。"""
    base = {
        "is_arc_finale": False,
        "tension_level": 5,
        "arc_phase": "rising",
    }
    base.update(overrides)
    return base


class _FakeFactory:
    """记账型 client 工厂,验证缓存命中 + provider 参数注入。

    **post-wave5 H1 routing completion 起**,Router.get_client 显式传
    ``provider=profile.provider`` 给工厂(实现多 provider 共存)。本 fake
    工厂的 ``__call__`` 签名跟随更新,记账 ``provider`` 字面以便单测断言。
    """

    def __init__(self) -> None:
        self.calls: list[tuple[str, str | None, str | None]] = []

    def __call__(
        self,
        *,
        model: str,
        base_url: str | None = None,
        provider: str | None = None,
    ) -> LLMClient:
        self.calls.append((model, base_url, provider))
        # 真实的 OllamaClient / MockLLMClient,便于 isinstance 检查。
        # OllamaClient 不会发起真实 HTTP 调用(本测试不调 .complete)。
        if model == "qwen-novel":
            return OllamaClient(
                model=model, base_url=base_url or "http://localhost:11434"
            )
        if model == "mock-default":
            return MockLLMClient()
        # claude profile:不构造真 ClaudeClient(避免 API key 检查),用 MagicMock
        # 模拟一个非 OllamaClient 的 client。
        client = MagicMock(spec=LLMClient)
        client._model = model
        return client


# ---------- Writer 路径(规则匹配) ----------


class TestRouterWriterPath:
    def test_arc_finale_routes_to_cloud_premium(self) -> None:
        router = ModelRouter(_make_profiles_config(), client_factory=_FakeFactory())
        target = router.route(
            "writer", chapter_card=_chapter_card_dict(is_arc_finale=True)
        )
        assert target.profile.profile_id == "cloud_premium"
        assert target.profile.model == "claude-opus-4-7"

    def test_high_tension_routes_to_cloud_premium(self) -> None:
        router = ModelRouter(_make_profiles_config(), client_factory=_FakeFactory())
        target = router.route(
            "writer", chapter_card=_chapter_card_dict(tension_level=10)
        )
        assert target.profile.profile_id == "cloud_premium"

    def test_tension_at_threshold_routes_to_cloud_premium(self) -> None:
        # tension == 9 (阈值边界):规则 ``>= 9`` 命中
        router = ModelRouter(_make_profiles_config(), client_factory=_FakeFactory())
        target = router.route(
            "writer", chapter_card=_chapter_card_dict(tension_level=9)
        )
        assert target.profile.profile_id == "cloud_premium"

    def test_default_chapter_card_routes_to_local_qwen(self) -> None:
        router = ModelRouter(_make_profiles_config(), client_factory=_FakeFactory())
        target = router.route("writer", chapter_card=_chapter_card_dict())
        assert target.profile.profile_id == "local_qwen_32k"
        assert target.profile.model == "qwen-novel"

    def test_no_chapter_card_falls_back_to_agent_routing_writer(self) -> None:
        # 无 ChapterCard 上下文 → 走 ``agent_routing.writer`` = local_qwen_32k
        router = ModelRouter(_make_profiles_config(), client_factory=_FakeFactory())
        target = router.route("writer")
        assert target.profile.profile_id == "local_qwen_32k"

    def test_first_matching_rule_wins(self) -> None:
        # 同时满足两条规则(arc_finale=True + tension=10),按列表顺序首条命中。
        # 两条都路由到 cloud_premium,但断言 select_profile 只走第一条 rule
        # (验证短路逻辑通过 evaluate_rule 副作用——本用例只断结果一致)。
        router = ModelRouter(_make_profiles_config(), client_factory=_FakeFactory())
        target = router.route(
            "writer",
            chapter_card=_chapter_card_dict(is_arc_finale=True, tension_level=10),
        )
        assert target.profile.profile_id == "cloud_premium"

    def test_chapter_card_pydantic_model_supported(self) -> None:
        # 入参是带 ``model_dump()`` 的 pydantic-like 对象时也能取字段。
        class _FakeCard:
            def model_dump(self) -> dict[str, Any]:
                return _chapter_card_dict(is_arc_finale=True)

        router = ModelRouter(_make_profiles_config(), client_factory=_FakeFactory())
        target = router.route("writer", chapter_card=_FakeCard())
        assert target.profile.profile_id == "cloud_premium"


# ---------- Agent 维度路由(决策 1 选 B) ----------


class TestRouterAgentDimension:
    def test_reviewer_routes_to_local_qwen_32k(self) -> None:
        router = ModelRouter(_make_profiles_config(), client_factory=_FakeFactory())
        target = router.route("reviewer")
        assert target.profile.profile_id == "local_qwen_32k"

    def test_memory_extractor_routes_to_local_qwen_safe(self) -> None:
        router = ModelRouter(_make_profiles_config(), client_factory=_FakeFactory())
        target = router.route("memory_extractor")
        assert target.profile.profile_id == "local_qwen_safe"
        assert target.profile.context_window == 16000

    def test_continuity_routes_to_local_qwen_safe(self) -> None:
        router = ModelRouter(_make_profiles_config(), client_factory=_FakeFactory())
        target = router.route("continuity")
        assert target.profile.profile_id == "local_qwen_safe"

    def test_reviewer_escalation_unconfigured_falls_back_to_default(self) -> None:
        # ``agent_routing`` 不含 ``reviewer_escalation`` → 回退 default。
        # 决策 4 选 A:运行时该 agent 实际是否调用由业务层另行 disable。
        router = ModelRouter(_make_profiles_config(), client_factory=_FakeFactory())
        target = router.route("reviewer_escalation")
        assert target.profile.profile_id == "local_qwen_32k"

    def test_unknown_agent_falls_back_to_default(self) -> None:
        # 未知 agent 名(P5+ 加新 agent 还没注册到 agent_routing)→ default。
        router = ModelRouter(_make_profiles_config(), client_factory=_FakeFactory())
        target = router.route("future_agent_xyz")
        assert target.profile.profile_id == "local_qwen_32k"


# ---------- LLMClient 缓存 ----------


class TestRouterClientCache:
    def test_same_profile_reuses_client(self) -> None:
        factory = _FakeFactory()
        router = ModelRouter(_make_profiles_config(), client_factory=factory)
        target1 = router.route("reviewer")
        target2 = router.route("reviser")  # 也走 local_qwen_32k
        assert target1.client is target2.client
        # 工厂只被调用一次(同一 (provider, model, base_url) 三元组)
        assert len(factory.calls) == 1

    def test_concurrent_first_hit_constructs_exactly_one_client(self) -> None:
        class _SlowFactory(_FakeFactory):
            def __init__(self) -> None:
                super().__init__()
                self._lock = Lock()

            def __call__(
                self,
                *,
                model: str,
                base_url: str | None = None,
                provider: str | None = None,
            ) -> LLMClient:
                with self._lock:
                    # 放大旧 check-then-set 的并发窗口；加锁后仍只能进入一次。
                    sleep(0.02)
                    return super().__call__(
                        model=model,
                        base_url=base_url,
                        provider=provider,
                    )

        factory = _SlowFactory()
        router = ModelRouter(_make_profiles_config(), client_factory=factory)
        profile = router.select_profile("reviewer")
        barrier = Barrier(8)

        def resolve() -> LLMClient:
            barrier.wait(timeout=2)
            return router.get_client(profile)

        with ThreadPoolExecutor(max_workers=8) as executor:
            clients = list(executor.map(lambda _: resolve(), range(8)))

        assert len(factory.calls) == 1
        assert all(client is clients[0] for client in clients)

    def test_different_profiles_get_different_clients(self) -> None:
        factory = _FakeFactory()
        router = ModelRouter(_make_profiles_config(), client_factory=factory)
        t_writer = router.route("reviewer")  # local_qwen_32k (ollama, qwen-novel)
        t_extractor = router.route(
            "memory_extractor"
        )  # local_qwen_safe (ollama, qwen-novel,
        # 同 provider/model/base_url) → 应复用同一缓存
        assert t_writer.client is t_extractor.client
        assert len(factory.calls) == 1

        t_finale = router.route(
            "writer", chapter_card=_chapter_card_dict(is_arc_finale=True)
        )
        # cloud_premium (claude, claude-opus-4-7) → 新构造
        assert t_finale.client is not t_writer.client
        assert len(factory.calls) == 2

    def test_cache_key_includes_base_url(self) -> None:
        # 两个 profile 同 provider/model 但 base_url 不同 → 不同缓存条目
        config = _make_profiles_config()
        # 替换 local_qwen_safe 的 base_url 为远程端点
        new_profiles = dict(config.profiles)
        new_profiles["local_qwen_safe"] = WriterProfile(
            profile_id="local_qwen_safe",
            provider="ollama",
            model="qwen-novel",
            base_url="http://192.168.1.50:11434",  # 不同 base_url
            context_window=16000,
            extra_request_kwargs={"think": False},
        )
        config = WriterProfilesConfig(profiles=new_profiles, routing=config.routing)

        factory = _FakeFactory()
        router = ModelRouter(config, client_factory=factory)
        router.route("reviewer")  # local_qwen_32k (localhost)
        router.route("memory_extractor")  # local_qwen_safe (192.168.1.50)
        assert len(factory.calls) == 2


# ---------- complete_kwargs 翻译(think → enable_thinking) ----------


class TestBuildCompleteKwargs:
    def test_ollama_translates_think_to_enable_thinking(self) -> None:
        router = ModelRouter(_make_profiles_config(), client_factory=_FakeFactory())
        target = router.route("reviewer")
        assert isinstance(target.client, OllamaClient)
        # think 不再出现在 kwargs 中
        assert "think" not in target.complete_kwargs
        # 翻译成显式 enable_thinking keyword
        assert target.complete_kwargs.get("enable_thinking") is False
        # generation_params 仍透传
        assert target.complete_kwargs.get("temperature") == 0.7
        assert target.complete_kwargs.get("num_predict") == 4096

    def test_ollama_enable_thinking_true(self) -> None:
        # 构造一个 think=True 的 profile,验证翻译保留 bool 值
        profile = WriterProfile(
            profile_id="thinking_on",
            provider="ollama",
            model="qwen-novel",
            base_url="http://localhost:11434",
            context_window=24576,
            extra_request_kwargs={"think": True},
        )
        client = OllamaClient(model="qwen-novel")
        kwargs = ModelRouter.build_complete_kwargs(profile, client)
        assert kwargs.get("enable_thinking") is True
        assert "think" not in kwargs
        client.close()

    def test_mock_client_does_not_translate_think(self) -> None:
        # mock-default profile 的 extra_request_kwargs 是空 dict,这里手动构造
        # 一个 mock provider profile 但带 think 字段,验证 router 不翻译。
        profile = WriterProfile(
            profile_id="mock_with_think",
            provider="mock",
            model="mock-default",
            context_window=16000,
            extra_request_kwargs={"think": False},
        )
        kwargs = ModelRouter.build_complete_kwargs(profile, MockLLMClient())
        # 非 OllamaClient → think 留在 dict 中(MockLLMClient.complete 透传忽略)
        assert kwargs.get("think") is False
        assert "enable_thinking" not in kwargs

    def test_empty_extra_kwargs_no_translation(self) -> None:
        profile = WriterProfile(
            profile_id="cloud_quality",
            provider="claude",
            model="claude-sonnet-4-6",
            context_window=200000,
            generation_params={"temperature": 0.85, "max_tokens": 6000},
        )
        client = MagicMock(spec=LLMClient)
        kwargs = ModelRouter.build_complete_kwargs(profile, client)
        assert kwargs == {"temperature": 0.85, "max_tokens": 6000}

    def test_openai_compatible_keeps_thinking_and_local_input_budgets(self) -> None:
        profile = WriterProfile(
            profile_id="qwen_medium",
            provider="openai_compatible",
            model="qwen3.8-max",
            base_url="https://dashscope.aliyuncs.com/compatible-mode/v1",
            context_window=1_000_000,
            generation_params={"temperature": 0.85, "max_tokens": 8_000},
            extra_request_kwargs={
                "enable_thinking": True,
                "preserve_thinking": False,
                "thinking_budget": 16_384,
                "input_token_budget": 524_288,
            },
        )
        client = MagicMock(spec=OpenAICompatibleClient)
        kwargs = ModelRouter.build_complete_kwargs(profile, client)
        assert kwargs == {
            "temperature": 0.85,
            "max_tokens": 8_000,
            "enable_thinking": True,
            "preserve_thinking": False,
            "thinking_budget": 16_384,
            "input_token_budget": 524_288,
        }


# ---------- override_profile_id ----------


class TestRouterOverride:
    def test_override_skips_rules_and_agent_routing(self) -> None:
        # 即便 ChapterCard 命中 arc_finale 规则,override 也能强制选 mock_default
        router = ModelRouter(_make_profiles_config(), client_factory=_FakeFactory())
        target = router.route(
            "writer",
            chapter_card=_chapter_card_dict(is_arc_finale=True),
            override_profile_id="mock_default",
        )
        assert target.profile.profile_id == "mock_default"

    def test_override_unknown_profile_raises(self) -> None:
        router = ModelRouter(_make_profiles_config(), client_factory=_FakeFactory())
        with pytest.raises(KeyError, match="未在 writer_profiles 声明"):
            router.route("writer", override_profile_id="ghost_profile")


# ---------- provider-unavailable fallback(v1.1 wave 2 H2 重写自 P7-B-06.5) ----------


@pytest.fixture()
def isolated_api_keys() -> Iterator[None]:
    """保护 5 个 API key 字段(claude/dashscope/deepseek/tokenhub/generic)
    + 默认清空,让 fallback 测试在干净环境下走"key 未配"路径。teardown 恢复
    原值,防跨测污染(主仓 .env 可能含 dashscope key 让"未配"断言失真)。
    """
    from pydantic import SecretStr as _SecretStr

    from src import config as _config_module

    attrs = (
        "claude_api_key",
        "dashscope_api_key",
        "deepseek_api_key",
        "tokenhub_api_key",
        "openai_compatible_api_key",
    )
    originals = {a: getattr(_config_module.settings, a) for a in attrs}
    try:
        for a in attrs:
            object.__setattr__(_config_module.settings, a, _SecretStr(""))
        yield
    finally:
        for a, v in originals.items():
            object.__setattr__(_config_module.settings, a, v)


def _set_api_key(provider: str, value: str) -> None:
    """显式配某 provider 的 key(配合 isolated_api_keys fixture 用)。"""
    from pydantic import SecretStr as _SecretStr

    from src import config as _config_module

    attr_map = {
        "claude": "claude_api_key",
        "dashscope": "dashscope_api_key",
        "deepseek": "deepseek_api_key",
        "tokenhub": "tokenhub_api_key",
        "openai_compatible": "openai_compatible_api_key",
    }
    object.__setattr__(_config_module.settings, attr_map[provider], _SecretStr(value))


class TestRouterProviderUnavailableFallback:
    """v1.1 wave 2 H2 重写自 P7-B-06.5 — trigger 从 ``settings.llm_provider``
    全局开关改为 API key 可用性查询(详 :func:`_is_provider_usable`)。

    **核心收益**:解锁"真混跑 provider" — 同进程 agent_routing 不同 agent
    走不同 provider(本地 ollama memory_extractor + 云端 dashscope writer)。

    旧版起源:P7-B-06 实证期 LLM_PROVIDER=ollama + 章节卡 is_arc_finale=True
    命中 cloud_premium(model=claude-opus-4-7)→ OllamaClient 拿 claude 模型
    名调 ollama 撞 HTTP 404。新版仍覆盖同 protective 场景(claude key 未配
    → cloud_premium 不可用 → fallback),trigger 路径不同。
    """

    def test_no_fallback_when_provider_usable_ollama(
        self, monkeypatch: pytest.MonkeyPatch, isolated_api_keys: None
    ) -> None:
        """ollama 总可用(本地无需 key)→ 即使 LLM_PROVIDER=mock 之外
        全局开关随意设,只要 profile 是 ollama 就不 fallback。"""
        monkeypatch.setattr("src.config.settings.llm_provider", "claude")
        router = ModelRouter(_make_profiles_config(), client_factory=_FakeFactory())
        target = router.route(
            "writer",
            chapter_card=_chapter_card_dict(),  # → local_qwen_32k (ollama)
        )
        assert target.profile.profile_id == "local_qwen_32k"
        assert target.profile.provider == "ollama"

    def test_fallback_when_claude_profile_picked_but_claude_key_missing(
        self, monkeypatch: pytest.MonkeyPatch, isolated_api_keys: None
    ) -> None:
        """is_arc_finale=True → 选 cloud_premium(claude),claude key 未配 →
        fallback 到 default local_qwen_32k(ollama 总可用)。"""
        monkeypatch.setattr("src.config.settings.llm_provider", "ollama")
        router = ModelRouter(_make_profiles_config(), client_factory=_FakeFactory())
        target = router.route(
            "writer", chapter_card=_chapter_card_dict(is_arc_finale=True)
        )
        # cloud_premium (claude, key 未配) → 不可用 → fallback 到 default
        assert target.profile.profile_id == "local_qwen_32k"
        assert target.profile.provider == "ollama"
        assert target.profile.model == "qwen-novel"

    def test_no_fallback_when_claude_profile_picked_and_claude_key_configured(
        self, monkeypatch: pytest.MonkeyPatch, isolated_api_keys: None
    ) -> None:
        """**关键新行为**:claude key 已配 → cloud_premium 可用 → 不 fallback,
        真走 cloud_premium(对照旧版即使 key 配齐,只要 LLM_PROVIDER=ollama
        就强制 fallback)。"""
        _set_api_key("claude", "sk-ant-real-key-1234567")
        monkeypatch.setattr("src.config.settings.llm_provider", "ollama")
        router = ModelRouter(_make_profiles_config(), client_factory=_FakeFactory())
        target = router.route(
            "writer", chapter_card=_chapter_card_dict(is_arc_finale=True)
        )
        # claude key 已配 → cloud_premium 可用 → 不 fallback
        assert target.profile.profile_id == "cloud_premium"
        assert target.profile.provider == "claude"

    def test_fallback_when_high_tension_picks_claude_and_key_missing(
        self, monkeypatch: pytest.MonkeyPatch, isolated_api_keys: None
    ) -> None:
        """tension=10 → 选 cloud_premium,claude key 未配 → fallback。"""
        monkeypatch.setattr("src.config.settings.llm_provider", "ollama")
        router = ModelRouter(_make_profiles_config(), client_factory=_FakeFactory())
        target = router.route(
            "writer", chapter_card=_chapter_card_dict(tension_level=10)
        )
        assert target.profile.profile_id == "local_qwen_32k"

    def test_mock_provider_never_fallbacks(
        self, monkeypatch: pytest.MonkeyPatch, isolated_api_keys: None
    ) -> None:
        """LLM_PROVIDER=mock 短路 — profile.provider 字面与 settings 解耦,
        cloud_premium(claude key 未配)也保留(MockLLMClient 不消费 model)。"""
        monkeypatch.setattr("src.config.settings.llm_provider", "mock")
        router = ModelRouter(_make_profiles_config(), client_factory=_FakeFactory())
        target = router.route(
            "writer", chapter_card=_chapter_card_dict(is_arc_finale=True)
        )
        assert target.profile.profile_id == "cloud_premium"
        assert target.profile.provider == "claude"

    def test_override_path_skips_fallback(
        self, monkeypatch: pytest.MonkeyPatch, isolated_api_keys: None
    ) -> None:
        """override 路径不进入 fallback — 显式 force_profile 即意图明确,
        失败由 LLMClient 自然报错让上层定位非法 profile。"""
        monkeypatch.setattr("src.config.settings.llm_provider", "ollama")
        router = ModelRouter(_make_profiles_config(), client_factory=_FakeFactory())
        target = router.route("writer", override_profile_id="cloud_premium")
        # claude key 未配但 override 不 fallback,保留 cloud_premium
        assert target.profile.profile_id == "cloud_premium"
        assert target.profile.provider == "claude"

    def test_keep_profile_when_both_picked_and_default_unavailable(
        self, monkeypatch: pytest.MonkeyPatch, isolated_api_keys: None
    ) -> None:
        """picked profile + default profile 都不可用 → 保留原 profile 让
        LLMClient 自然报错(避免无限循环)。

        构造:default=cloud_quality (claude),agent_routing.reviewer=
        cloud_premium (claude),两 profile 都 claude,claude key 未配 →
        两者都不可用 → 保留 picked cloud_premium。
        """
        config = _make_profiles_config()
        config = WriterProfilesConfig(
            profiles=config.profiles,
            routing=RoutingConfig(
                default="cloud_quality",  # claude
                rules=config.routing.rules,
                agent_routing={
                    **config.routing.agent_routing,
                    "reviewer": "cloud_premium",  # claude
                },
            ),
        )
        monkeypatch.setattr("src.config.settings.llm_provider", "claude")
        router = ModelRouter(config, client_factory=_FakeFactory())
        target = router.route("reviewer")
        # picked=cloud_premium 不可用 + default=cloud_quality 也不可用 → 保留 picked
        assert target.profile.profile_id == "cloud_premium"
        assert target.profile.provider == "claude"

    def test_real_mixed_provider_unlocked_memory_extractor_stays_local(
        self, monkeypatch: pytest.MonkeyPatch, isolated_api_keys: None
    ) -> None:
        """**v1.1 wave 2 H2 核心收益证据** — agent_routing.memory_extractor=
        local_qwen_safe(ollama)+ dashscope key 配齐 + default=
        cloud_qwen_max_standard(openai_compatible)→ memory_extractor 真走
        本地 ollama,**不被 silent 升级到云端 default**(对照旧版逻辑此
        场景下 memory_extractor 总被 silent fallback)。
        """
        # 配齐 dashscope key 让云端 default 可用
        _set_api_key("dashscope", "sk-dashscope-real-1234567")
        # 构造云端 default + 本地 memory_extractor 的混跑配置
        base = _make_profiles_config()
        # 加 cloud_qwen_max_standard profile(openai_compatible)
        mixed_profiles = {
            **base.profiles,
            "cloud_qwen_max_standard": WriterProfile(
                profile_id="cloud_qwen_max_standard",
                provider="openai_compatible",
                model="qwen3.7-max",
                base_url="https://dashscope.aliyuncs.com/compatible-mode/v1",
                context_window=1000000,
                generation_params={"temperature": 0.85, "max_tokens": 8000},
            ),
        }
        config = WriterProfilesConfig(
            profiles=mixed_profiles,
            routing=RoutingConfig(
                default="cloud_qwen_max_standard",  # openai_compatible
                rules=base.routing.rules,
                agent_routing={
                    **base.routing.agent_routing,
                    "memory_extractor": "local_qwen_safe",  # ollama
                    "writer": "cloud_qwen_max_standard",  # openai_compatible
                },
            ),
        )
        monkeypatch.setattr("src.config.settings.llm_provider", "openai_compatible")
        router = ModelRouter(config, client_factory=_FakeFactory())

        # memory_extractor 真走本地 ollama(关键证据)
        mem_target = router.route("memory_extractor")
        assert mem_target.profile.profile_id == "local_qwen_safe"
        assert mem_target.profile.provider == "ollama"

        # writer 真走云端 openai_compatible(对照)
        writer_target = router.route("writer", chapter_card=_chapter_card_dict())
        assert writer_target.profile.profile_id == "cloud_qwen_max_standard"
        assert writer_target.profile.provider == "openai_compatible"


# ---------- _is_provider_usable 纯函数单测(v1.1 wave 2 H2 引入) ----------


class TestIsProviderUsable:
    """:func:`_is_provider_usable` 纯函数 — 边界 + 各 provider 分支覆盖。"""

    def test_mock_always_usable(self) -> None:
        from src.core.routing.router import _is_provider_usable

        assert _is_provider_usable("mock") is True

    def test_ollama_always_usable_no_key_needed(self, isolated_api_keys: None) -> None:
        from src.core.routing.router import _is_provider_usable

        # 4 字段都空 → ollama 仍可用(本地路径)
        assert _is_provider_usable("ollama") is True

    def test_claude_requires_claude_api_key_non_empty(
        self, isolated_api_keys: None
    ) -> None:
        from src.core.routing.router import _is_provider_usable

        assert _is_provider_usable("claude") is False
        _set_api_key("claude", "sk-ant-1234567890")
        assert _is_provider_usable("claude") is True

    def test_dashscope_endpoint_requires_dashscope_key(
        self, isolated_api_keys: None
    ) -> None:
        from src.core.routing.router import _is_provider_usable

        endpoint = "https://dashscope.aliyuncs.com/compatible-mode/v1"
        assert _is_provider_usable("openai_compatible", endpoint) is False

        _set_api_key("openai_compatible", "sk-generic-1234567890")
        assert _is_provider_usable("openai_compatible", endpoint) is False
        _set_api_key("dashscope", "sk-ds-1234567890")
        assert _is_provider_usable("openai_compatible", endpoint) is True

    def test_deepseek_endpoint_requires_deepseek_key(
        self, isolated_api_keys: None
    ) -> None:
        from src.core.routing.router import _is_provider_usable

        endpoint = "https://api.deepseek.com/v1"
        _set_api_key("dashscope", "sk-ds-1234567890")
        assert _is_provider_usable("openai_compatible", endpoint) is False
        _set_api_key("deepseek", "sk-deepseek-1234567890")
        assert _is_provider_usable("openai_compatible", endpoint) is True

    def test_custom_endpoint_requires_generic_key(
        self, isolated_api_keys: None
    ) -> None:
        from src.core.routing.router import _is_provider_usable

        endpoint = "https://deepseek.example.invalid/v1"
        _set_api_key("deepseek", "sk-deepseek-1234567890")
        assert _is_provider_usable("openai_compatible", endpoint) is False
        _set_api_key("openai_compatible", "sk-oai-compat-1234567")
        assert _is_provider_usable("openai_compatible", endpoint) is True

    def test_invalid_endpoint_is_not_usable(
        self, isolated_api_keys: None
    ) -> None:
        from src.core.routing.router import _is_provider_usable

        _set_api_key("openai_compatible", "sk-oai-compat-1234567")
        assert (
            _is_provider_usable(
                "openai_compatible", "https://user@example.invalid/v1"
            )
            is False
        )

    def test_unknown_provider_returns_true_lets_factory_handle(
        self, isolated_api_keys: None
    ) -> None:
        """未知 provider 返 True(由工厂自然抛 'unknown provider' 错,避免
        本函数变 fail-closed 阻塞未来新 provider 接入)。"""
        from src.core.routing.router import _is_provider_usable

        assert _is_provider_usable("nonexistent_provider") is True


# ---------- RoutedTarget 集成验证 ----------


class TestRoutedTargetUsage:
    def test_returns_named_tuple(self) -> None:
        router = ModelRouter(_make_profiles_config(), client_factory=_FakeFactory())
        target = router.route("reviewer")
        assert isinstance(target, RoutedTarget)
        assert hasattr(target, "profile")
        assert hasattr(target, "client")
        assert hasattr(target, "complete_kwargs")

    def test_target_can_invoke_mock_client_complete(self) -> None:
        # 全链路烟囱:Router → MockLLMClient → 完整 LLMResponse。
        # 通过 override 选 mock_default 走 mock client。
        factory = _FakeFactory()
        router = ModelRouter(_make_profiles_config(), client_factory=factory)
        target = router.route("writer", override_profile_id="mock_default")

        messages = [
            Message(
                role="user",
                content="测试 user 消息 <!-- agent_role: writer chapter_id: ch-test -->",
            )
        ]
        response = target.client.complete(messages, **target.complete_kwargs)
        assert isinstance(response, LLMResponse)
        assert response.usage.input_tokens >= 0


# ---------- post-wave5 H1 routing completion: 多 provider 共存路径 ----------


def _make_multi_provider_config() -> WriterProfilesConfig:
    """构造含 openai_compatible profile 的多 provider 配置(routing completion
    实证场景缩减版)。

    模拟 luo-cloud-test 工作区 project.yaml 覆盖后的合并结果:
    - routing.default = cloud_qwen_max_standard(openai_compatible)
    - agent_routing.outline_builder = cloud_qwen_max_thinking(openai_compatible)
    - agent_routing.continuity = local_qwen_safe(ollama,未覆盖保留引擎默认)
    """
    profiles = {
        "local_qwen_32k": WriterProfile(
            profile_id="local_qwen_32k",
            provider="ollama",
            model="qwen-novel",
            base_url="http://localhost:11434",
            context_window=24576,
            generation_params={"temperature": 0.7, "num_predict": 4096},
            extra_request_kwargs={"think": False},
        ),
        "local_qwen_safe": WriterProfile(
            profile_id="local_qwen_safe",
            provider="ollama",
            model="qwen-novel",
            base_url="http://localhost:11434",
            context_window=16000,
            generation_params={"temperature": 0.5, "num_predict": 2048},
            extra_request_kwargs={"think": False},
        ),
        "cloud_qwen_max_thinking": WriterProfile(
            profile_id="cloud_qwen_max_thinking",
            provider="openai_compatible",
            model="qwen3.7-max",
            base_url="https://dashscope.aliyuncs.com/compatible-mode/v1",
            context_window=1000000,
            generation_params={"temperature": 0.7, "max_tokens": 8000},
            extra_request_kwargs={"enable_thinking": True},
        ),
        "cloud_qwen_max_standard": WriterProfile(
            profile_id="cloud_qwen_max_standard",
            provider="openai_compatible",
            model="qwen3.7-max",
            base_url="https://dashscope.aliyuncs.com/compatible-mode/v1",
            context_window=1000000,
            generation_params={"temperature": 0.85, "max_tokens": 8000},
            extra_request_kwargs={"enable_thinking": False},
        ),
    }
    routing = RoutingConfig(
        default="cloud_qwen_max_standard",
        rules=[],
        agent_routing={
            "writer": "cloud_qwen_max_standard",
            "outline_builder": "cloud_qwen_max_thinking",
            "reviewer": "cloud_qwen_max_thinking",
            "continuity": "local_qwen_safe",  # 未覆盖,留本地
        },
    )
    return WriterProfilesConfig(profiles=profiles, routing=routing)


class _ProviderRecordingFactory:
    """记账型工厂(本节专用):只记账,不构造真 client。

    用 ``MagicMock(spec=LLMClient)`` 占位避免 OllamaClient 真构造时 base_url
    解析 / OpenAICompatibleClient 真构造时 api_key 校验。
    """

    def __init__(self) -> None:
        self.calls: list[dict[str, str | None]] = []

    def __call__(
        self,
        *,
        model: str,
        base_url: str | None = None,
        provider: str | None = None,
    ) -> LLMClient:
        self.calls.append({"model": model, "base_url": base_url, "provider": provider})
        client = MagicMock(spec=LLMClient)
        client._model = model
        return client


class TestRoutingCompletionMultiProvider:
    """post-wave5 H1 routing completion:Router.get_client 把 ``profile.provider``
    显式传给工厂,实现多 provider 共存路径(本地 ollama + 云端 dashscope
    同进程共存)。"""

    def test_get_client_passes_profile_provider_for_local(self) -> None:
        # local profile → 工厂收到 provider="ollama" 字面
        factory = _ProviderRecordingFactory()
        router = ModelRouter(_make_multi_provider_config(), client_factory=factory)
        router.route("continuity")  # 命中 local_qwen_safe
        assert len(factory.calls) == 1
        assert factory.calls[0]["provider"] == "ollama"
        assert factory.calls[0]["model"] == "qwen-novel"
        assert factory.calls[0]["base_url"] == "http://localhost:11434"

    def test_get_client_passes_profile_provider_for_cloud(self) -> None:
        # cloud profile → 工厂收到 provider="openai_compatible" 字面
        factory = _ProviderRecordingFactory()
        router = ModelRouter(_make_multi_provider_config(), client_factory=factory)
        router.route("outline_builder")  # 命中 cloud_qwen_max_thinking
        assert len(factory.calls) == 1
        assert factory.calls[0]["provider"] == "openai_compatible"
        assert factory.calls[0]["model"] == "qwen3.7-max"
        assert factory.calls[0]["base_url"] == (
            "https://dashscope.aliyuncs.com/compatible-mode/v1"
        )

    def test_standard_routes_send_explicit_false_to_http_body(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        """Standard 直配与 default 回退都把 False 送到最终 wire body。"""
        from src.config import settings as _settings

        monkeypatch.setattr(_settings, "llm_provider", "openai_compatible")
        captured: list[dict[str, Any]] = []

        def handler(request: httpx.Request) -> httpx.Response:
            captured.append(json.loads(request.content))
            stream = (
                'data: {"model":"qwen3.7-max","choices":[{"index":0,'
                '"delta":{"content":"ok"},"finish_reason":"stop"}]}\n\n'
                'data: {"model":"qwen3.7-max","choices":[],"usage":'
                '{"prompt_tokens":1,"completion_tokens":1}}\n\n'
                "data: [DONE]\n\n"
            )
            return httpx.Response(
                200,
                content=stream.encode(),
                headers={"content-type": "text/event-stream"},
            )

        http_client = httpx.Client(transport=httpx.MockTransport(handler))

        def factory(
            *,
            model: str,
            base_url: str | None = None,
            provider: str | None = None,
        ) -> LLMClient:
            assert provider == "openai_compatible"
            assert base_url is not None
            return OpenAICompatibleClient(
                model=model,
                base_url=base_url,
                api_key=SecretStr("sk-offline-test"),
                http_client=http_client,
            )

        router = ModelRouter(_make_multi_provider_config(), client_factory=factory)
        try:
            for agent_name in ("writer", "memory_health_checker"):
                target = router.route(agent_name)
                assert target.profile.profile_id == "cloud_qwen_max_standard"
                assert target.complete_kwargs["enable_thinking"] is False
                target.client.complete(
                    [Message(role="user", content="offline")],
                    **target.complete_kwargs,
                )
        finally:
            http_client.close()

        assert [body["enable_thinking"] for body in captured] == [False, False]

    def test_multi_provider_coexistence_in_single_router(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        """端到端核心场景:同一 Router 实例下,不同 agent 命中不同
        provider 的 profile,工厂分别收到对应 provider 字面 — 本地 ollama
        + 云端 openai_compatible 共存零冲突。

        **这是 routing completion 的核心验证**:对照 H1 第一阶段旧路径
        (factory 按 settings.llm_provider 全局派发),同 router 实例下
        无法实现"continuity 走本地 ollama + outline_builder 走云端
        dashscope"的多 provider 共存。
        """
        # settings.llm_provider 故意设成 ollama,验证 Router 不依赖该全局
        # 开关 — 而是按 profile.provider 字面走工厂。注意此时
        # _maybe_fallback 会触发(profile.provider=openai_compatible
        # != settings=ollama),但 default.provider=openai_compatible 也
        # != settings,fallback 不生效,保留原 profile。
        from src.config import settings as _settings

        monkeypatch.setattr(_settings, "llm_provider", "ollama")

        factory = _ProviderRecordingFactory()
        router = ModelRouter(_make_multi_provider_config(), client_factory=factory)

        # 本地 agent:命中 local_qwen_safe(ollama, qwen-novel, localhost)
        router.route("continuity")
        # 云端 agent(同一 Router 实例):命中 cloud_qwen_max_thinking
        # (openai_compatible, qwen3.7-max, dashscope)
        router.route("outline_builder")
        # 云端 agent(writer):命中 cloud_qwen_max_standard,与
        # cloud_qwen_max_thinking 共用 (provider, model, base_url) 三元组
        # → 缓存命中,工厂不被再次调用
        router.route("writer", chapter_card=_chapter_card_dict())

        # 工厂调用次数 = 不同 (provider, model, base_url) 三元组数 = 2
        # (ollama+qwen-novel+localhost / openai_compatible+qwen3.7-max+dashscope)
        assert len(factory.calls) == 2
        providers = sorted(c["provider"] for c in factory.calls)
        assert providers == ["ollama", "openai_compatible"]

    def test_provider_in_cache_key_separates_providers(self) -> None:
        """缓存 key 含 provider 维度:同 model + 同 base_url 但不同
        provider 字面的 profile 不会共用 client 实例。
        """
        # 构造一个边界 config:两个 profile 同 model + 同 base_url
        # (理论 corner case,实战不会出现,但缓存 key 必须按字面分隔)
        profiles = {
            "edge_a": WriterProfile(
                profile_id="edge_a",
                provider="ollama",
                model="shared-model",
                base_url="https://edge-host:8080",
                context_window=16000,
            ),
            "edge_b": WriterProfile(
                profile_id="edge_b",
                provider="openai_compatible",
                model="shared-model",
                base_url="https://edge-host:8080",
                context_window=16000,
            ),
        }
        routing = RoutingConfig(
            default="edge_a",
            agent_routing={"writer": "edge_a", "reviewer": "edge_b"},
        )
        config = WriterProfilesConfig(profiles=profiles, routing=routing)

        factory = _ProviderRecordingFactory()
        router = ModelRouter(config, client_factory=factory)
        # 本测试只验证 client cache key。用显式 override 跳过运行时
        # provider-usable fallback，避免测试结果依赖宿主机是否配置了这个
        # 虚构 custom endpoint 的 generic API key。
        router.route("writer", override_profile_id="edge_a")
        router.route("reviewer", override_profile_id="edge_b")

        # 即使 model + base_url 完全相同,provider 字面不同 → 缓存 key 不同
        # → 工厂被调用 2 次
        assert len(factory.calls) == 2
        providers = sorted(c["provider"] for c in factory.calls)
        assert providers == ["ollama", "openai_compatible"]
