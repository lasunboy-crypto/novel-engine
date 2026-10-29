"""P3-B-01:WriterProfile + RoutingRule + RoutingConfig + WriterProfilesConfig 模型测试。

测试矩阵
========
- WriterProfile:合法构造 / 缺必填字段 / provider 越界 / base_url 空字符串
- RoutingRule:合法构造 / condition 空 / profile 越界
- RoutingConfig:default + rules + agent_routing 合法 / 默认空 rules+agent_routing /
  resolve_for_writer stub 返默认 / resolve_for_agent 命中 + miss 回退
- WriterProfilesConfig:整体合法 / routing.default 引用未定义 profile /
  rules[i].profile 引用未定义 / agent_routing 引用未定义 / profiles dict key 与
  profile_id 不一致

设计沿用 ``test_models.py`` 风格(每模型最少 3 用例 + 用 ValidationError 断言)。
"""

from __future__ import annotations

import pytest
from pydantic import ValidationError

from src.core.models.routing import RoutingConfig, RoutingRule, WriterProfilesConfig
from src.core.models.writer_profile import WriterProfile


# ---------- 测试构造器(最小合法 payload) ----------


def _profile_payload(**overrides) -> dict:
    base = {
        "profile_id": "local_qwen_32k",
        "provider": "ollama",
        "model": "qwen-novel",
        "base_url": "http://localhost:11434",
        "context_window": 24576,
        "generation_params": {"temperature": 0.7, "num_predict": 4096},
        "extra_request_kwargs": {"think": False},
        "cost_per_1k_input": 0.0,
        "cost_per_1k_output": 0.0,
        "notes": "P3 默认 profile",
    }
    base.update(overrides)
    return base


def _rule_payload(**overrides) -> dict:
    base = {
        "condition": "is_arc_finale == True",
        "profile": "cloud_premium",
    }
    base.update(overrides)
    return base


def _routing_payload(**overrides) -> dict:
    base = {
        "default": "local_qwen_32k",
        "rules": [_rule_payload()],
        "agent_routing": {
            "writer": "local_qwen_32k",
            "reviewer": "local_qwen_safe",
        },
    }
    base.update(overrides)
    return base


# ---------- WriterProfile ----------


class TestWriterProfile:
    def test_合法构造_全字段(self) -> None:
        p = WriterProfile(**_profile_payload())
        assert p.profile_id == "local_qwen_32k"
        assert p.provider == "ollama"
        assert p.extra_request_kwargs == {"think": False}
        assert p.cost_per_1k_input == 0.0

    def test_合法构造_claude_最小字段(self) -> None:
        p = WriterProfile(
            profile_id="cloud_quality",
            provider="claude",
            model="claude-sonnet-4-6",
            context_window=200000,
        )
        assert p.base_url is None
        assert p.generation_params == {}
        assert p.extra_request_kwargs == {}
        assert p.notes == ""

    def test_缺必填字段_provider抛错(self) -> None:
        payload = _profile_payload()
        del payload["provider"]
        with pytest.raises(ValidationError):
            WriterProfile(**payload)

    def test_provider越界(self) -> None:
        """provider Literal 校验 — 真正不在合法集的字面应被拒。

        v1.0.x post-wave5 H1:openai_compatible 加入合法集(覆盖 dashscope
        qwen3.7-max / DeepSeek / Moonshot 等)。本测改用一个明显非法的字面
        ``totally_fake_provider`` 验证 Literal 校验仍生效。
        """
        with pytest.raises(ValidationError):
            WriterProfile(**_profile_payload(provider="totally_fake_provider"))

    def test_provider_openai_compatible_accepted(self) -> None:
        """v1.0.x post-wave5 H1:openai_compatible 是合法 provider。

        需配 base_url(类似 dashscope endpoint),否则
        ``_validate_provider_specific`` 抛 ValidationError。
        """
        payload = _profile_payload(
            provider="openai_compatible",
            base_url="https://dashscope.aliyuncs.com/compatible-mode/v1",
        )
        p = WriterProfile(**payload)
        assert p.provider == "openai_compatible"
        assert p.base_url == "https://dashscope.aliyuncs.com/compatible-mode/v1"

    def test_provider_openai_compatible_requires_base_url(self) -> None:
        """openai_compatible provider 缺 base_url 抛 ValidationError。"""
        with pytest.raises(ValidationError, match="base_url 必填"):
            WriterProfile(
                **_profile_payload(
                    provider="openai_compatible",
                    base_url=None,
                )
            )

    @pytest.mark.parametrize(
        "base_url",
        [
            "https://user@example.invalid/v1",
            "https://example.invalid/v1?query=1",
            "https://example.invalid/v1#fragment",
            "http://api.deepseek.com/v1",
            "https://api.deepseek.com:8443/v1",
            " https://example.invalid/v1",
            "https://exa mple.invalid/v1",
            "ftp://example.invalid/v1",
        ],
    )
    def test_openai_compatible_rejects_ambiguous_endpoint(
        self, base_url: str
    ) -> None:
        with pytest.raises(ValidationError, match="base_url"):
            WriterProfile(
                **_profile_payload(
                    provider="openai_compatible",
                    base_url=base_url,
                )
            )

    def test_context_window非正抛错(self) -> None:
        with pytest.raises(ValidationError):
            WriterProfile(**_profile_payload(context_window=0))

    def test_cost_per_1k_input负数抛错(self) -> None:
        with pytest.raises(ValidationError):
            WriterProfile(**_profile_payload(cost_per_1k_input=-0.01))

    def test_cost_currency_is_explicit_and_normalized(self) -> None:
        profile = WriterProfile(**_profile_payload(cost_currency="rmb"))
        assert profile.cost_currency == "RMB"

    @pytest.mark.parametrize("currency", ["", "US", "USDT", "12$"])
    def test_cost_currency_rejects_ambiguous_code(self, currency: str) -> None:
        with pytest.raises(ValidationError, match="cost_currency"):
            WriterProfile(**_profile_payload(cost_currency=currency))

    def test_base_url空字符串抛错(self) -> None:
        """空字符串 base_url 是配置错误,应抛而非静默放行。"""
        with pytest.raises(ValidationError, match="base_url 不允许为空字符串"):
            WriterProfile(**_profile_payload(base_url=""))

    def test_profile_id超长抛错(self) -> None:
        with pytest.raises(ValidationError):
            WriterProfile(**_profile_payload(profile_id="x" * 65))

    def test_qwen分层预算合法(self) -> None:
        p = WriterProfile(
            **_profile_payload(
                provider="openai_compatible",
                model="qwen3.8-max",
                base_url="https://dashscope.aliyuncs.com/compatible-mode/v1",
                context_window=1_000_000,
                generation_params={"max_tokens": 8_000},
                extra_request_kwargs={
                    "enable_thinking": True,
                    "preserve_thinking": False,
                    "thinking_budget": 16_384,
                    "input_token_budget": 262_144,
                },
            )
        )
        assert p.extra_request_kwargs["thinking_budget"] == 16_384
        assert p.extra_request_kwargs["input_token_budget"] == 262_144

    @pytest.mark.parametrize("budget", [0, -1, True, 1.5])
    def test_input_token_budget必须为正整数(self, budget) -> None:
        with pytest.raises(ValidationError, match="input_token_budget 必须为正整数"):
            WriterProfile(
                **_profile_payload(
                    extra_request_kwargs={
                        "think": False,
                        "input_token_budget": budget,
                    }
                )
            )

    def test_input_token_budget不得超过上下文窗口(self) -> None:
        with pytest.raises(ValidationError, match="不得超过 context_window"):
            WriterProfile(
                **_profile_payload(
                    context_window=8_000,
                    extra_request_kwargs={
                        "think": False,
                        "input_token_budget": 8_001,
                    },
                )
            )

    def test_input_token_budget不得静默传给非兼容client(self) -> None:
        with pytest.raises(ValidationError, match="只由 openai_compatible client"):
            WriterProfile(
                **_profile_payload(
                    extra_request_kwargs={
                        "think": False,
                        "input_token_budget": 8_000,
                    }
                )
            )

    def test_qwen思考预算冲突拒绝(self) -> None:
        with pytest.raises(ValidationError, match="不得同时设置"):
            WriterProfile(
                **_profile_payload(
                    provider="openai_compatible",
                    model="qwen3.8-max",
                    base_url="https://dashscope.aliyuncs.com/compatible-mode/v1",
                    extra_request_kwargs={
                        "enable_thinking": True,
                        "thinking_budget": 4_096,
                        "reasoning_effort": "low",
                    },
                )
            )

    def test_thinking_off不得带正思考预算(self) -> None:
        with pytest.raises(ValidationError, match="enable_thinking=false"):
            WriterProfile(
                **_profile_payload(
                    provider="openai_compatible",
                    model="qwen3.8-max",
                    base_url="https://dashscope.aliyuncs.com/compatible-mode/v1",
                    extra_request_kwargs={
                        "enable_thinking": False,
                        "thinking_budget": 4_096,
                    },
                )
            )


# ---------- RoutingRule ----------


class TestRoutingRule:
    def test_合法构造(self) -> None:
        r = RoutingRule(**_rule_payload())
        assert r.condition == "is_arc_finale == True"
        assert r.profile == "cloud_premium"

    def test_condition空抛错(self) -> None:
        with pytest.raises(ValidationError):
            RoutingRule(condition="", profile="cloud_premium")

    def test_profile空抛错(self) -> None:
        with pytest.raises(ValidationError):
            RoutingRule(condition="x == 1", profile="")


# ---------- RoutingConfig ----------


class TestRoutingConfig:
    def test_合法构造(self) -> None:
        rc = RoutingConfig(**_routing_payload())
        assert rc.default == "local_qwen_32k"
        assert len(rc.rules) == 1
        assert rc.agent_routing["writer"] == "local_qwen_32k"

    def test_默认空_rules_agent_routing(self) -> None:
        rc = RoutingConfig(default="local_qwen_32k")
        assert rc.rules == []
        assert rc.agent_routing == {}

    def test_resolve_for_writer_当前stub返默认(self) -> None:
        """P3-B-01 桩实现:任何 context 都返 default;P3-B-05 替换为 AST 求值。"""
        rc = RoutingConfig(**_routing_payload())
        assert rc.resolve_for_writer({"is_arc_finale": True}) == "local_qwen_32k"
        assert rc.resolve_for_writer({}) == "local_qwen_32k"

    def test_resolve_for_agent_命中(self) -> None:
        rc = RoutingConfig(**_routing_payload())
        assert rc.resolve_for_agent("reviewer") == "local_qwen_safe"

    def test_resolve_for_agent_未配置回退default(self) -> None:
        rc = RoutingConfig(**_routing_payload())
        # memory_extractor 未在 agent_routing 里 → 回退 default
        assert rc.resolve_for_agent("memory_extractor") == "local_qwen_32k"


# ---------- WriterProfilesConfig(交叉校验) ----------


class TestWriterProfilesConfig:
    def _profiles_dict(self, *ids: str) -> dict[str, WriterProfile]:
        """构造 profiles 字典,每个 id 用最小合法 ollama profile。"""
        return {pid: WriterProfile(**_profile_payload(profile_id=pid)) for pid in ids}

    def test_合法构造_全引用闭环(self) -> None:
        profiles = self._profiles_dict(
            "local_qwen_32k", "local_qwen_safe", "cloud_premium"
        )
        routing = RoutingConfig(**_routing_payload())
        cfg = WriterProfilesConfig(profiles=profiles, routing=routing)
        assert "local_qwen_32k" in cfg.profiles
        assert cfg.routing.default == "local_qwen_32k"

    def test_routing_default_引用未定义profile(self) -> None:
        profiles = self._profiles_dict("local_qwen_32k")
        routing = RoutingConfig(default="missing_profile")
        with pytest.raises(ValidationError, match="writer_routing.default"):
            WriterProfilesConfig(profiles=profiles, routing=routing)

    def test_routing_rule_引用未定义profile(self) -> None:
        profiles = self._profiles_dict("local_qwen_32k")
        routing = RoutingConfig(
            default="local_qwen_32k",
            rules=[RoutingRule(condition="x == 1", profile="missing_premium")],
        )
        with pytest.raises(ValidationError, match=r"rules\[0\]\.profile"):
            WriterProfilesConfig(profiles=profiles, routing=routing)

    def test_agent_routing_引用未定义profile(self) -> None:
        profiles = self._profiles_dict("local_qwen_32k")
        routing = RoutingConfig(
            default="local_qwen_32k",
            agent_routing={"reviewer": "missing_profile"},
        )
        with pytest.raises(ValidationError, match="agent_routing"):
            WriterProfilesConfig(profiles=profiles, routing=routing)

    def test_profiles_dict_key_与profile_id不一致(self) -> None:
        """yaml 字面 key=`local_qwen_32k`,但 value 里 profile_id=`other_id` → 拒绝。"""
        profiles = {
            "local_qwen_32k": WriterProfile(**_profile_payload(profile_id="other_id")),
        }
        routing = RoutingConfig(default="local_qwen_32k")
        with pytest.raises(ValidationError, match="profile_id"):
            WriterProfilesConfig(profiles=profiles, routing=routing)

    def test_profiles_空dict抛错(self) -> None:
        """profiles 必须至少 1 个;空 dict 是无效配置。"""
        with pytest.raises(ValidationError):
            WriterProfilesConfig(
                profiles={},
                routing=RoutingConfig(default="x"),
            )
