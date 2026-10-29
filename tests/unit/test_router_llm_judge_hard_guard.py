"""LLM-as-judge profile resolver hard guard 测试(v1.1 wave 2 临时会话 2026-05-28)。

ModelRouter.select_profile 出口处对 4 个 LLM-as-judge agent
(must_vs_forbidden_validator / beat_alignment_validator /
must_achieve_validator / continuity_validator)做 thinking-on 硬约束。

防御场景:
- w2-s7 G9.4 本地 small model 实验误切 LLM-judge → guard 立刻 raise
- w3-s2 H5 UI routing 切换器把 LLM-judge 切到 thinking off → guard 截获
- 工作区 project.yaml agent_routing 整段覆盖漏列 LLM-judge → 走 default 兜底
  时若 default thinking off,guard 截获
- override_profile_id 显式覆盖到 thinking off → guard 截获(防 maintainer 误切)
"""

from __future__ import annotations

from unittest.mock import MagicMock

import pytest

from src.core.llm.base import LLMClient
from src.core.models.routing import (
    LLM_AS_JUDGE_AGENTS,
    RoutingConfig,
    WriterProfilesConfig,
    is_thinking_on_profile,
)
from src.core.models.writer_profile import WriterProfile
from src.core.routing.router import ModelRouter


# ===========================================================================
# is_thinking_on_profile 函数契约(各 provider 分支)
# ===========================================================================


class TestIsThinkingOnProfileFunction:
    """is_thinking_on_profile 判定函数行为。"""

    def test_mock_provider_always_pass(self) -> None:
        profile = WriterProfile(
            profile_id="mock_default",
            provider="mock",
            model="mock-model",
            context_window=8192,
            extra_request_kwargs={},
        )
        assert is_thinking_on_profile(profile) is True

    def test_claude_provider_always_pass(self) -> None:
        """Claude 默认 reasoning 较强,不强求显式 thinking 字段。"""
        profile = WriterProfile(
            profile_id="cloud_quality",
            provider="claude",
            model="claude-sonnet-4-6",
            context_window=200000,
            extra_request_kwargs={},
        )
        assert is_thinking_on_profile(profile) is True

    def test_ollama_think_true_returns_true(self) -> None:
        profile = WriterProfile(
            profile_id="local_qwen_thinking",
            provider="ollama",
            model="qwen-novel",
            base_url="http://localhost:11434",
            context_window=16000,
            extra_request_kwargs={"think": True},
        )
        assert is_thinking_on_profile(profile) is True

    def test_ollama_think_false_returns_false(self) -> None:
        profile = WriterProfile(
            profile_id="local_qwen_safe",
            provider="ollama",
            model="qwen-novel",
            base_url="http://localhost:11434",
            context_window=16000,
            extra_request_kwargs={"think": False},
        )
        assert is_thinking_on_profile(profile) is False

    def test_ollama_missing_think_returns_false(self) -> None:
        """ollama profile 未声明 think → 默认 False(对齐 dict.get 语义)。"""
        profile = WriterProfile(
            profile_id="local_qwen_no_think_field",
            provider="ollama",
            model="qwen-novel",
            base_url="http://localhost:11434",
            context_window=16000,
            extra_request_kwargs={},
        )
        assert is_thinking_on_profile(profile) is False

    def test_openai_compatible_enable_thinking_true(self) -> None:
        profile = WriterProfile(
            profile_id="cloud_qwen_max_thinking",
            provider="openai_compatible",
            model="qwen3.7-max",
            base_url="https://dashscope.aliyuncs.com/compatible-mode/v1",
            context_window=1000000,
            extra_request_kwargs={"enable_thinking": True},
        )
        assert is_thinking_on_profile(profile) is True

    def test_openai_compatible_enable_thinking_false(self) -> None:
        profile = WriterProfile(
            profile_id="cloud_qwen_max_standard",
            provider="openai_compatible",
            model="qwen3.7-max",
            base_url="https://dashscope.aliyuncs.com/compatible-mode/v1",
            context_window=1000000,
            extra_request_kwargs={"enable_thinking": False},
        )
        assert is_thinking_on_profile(profile) is False


# ===========================================================================
# LLM_AS_JUDGE_AGENTS 集合契约
# ===========================================================================


class TestLLMAsJudgeAgentsSet:
    """LLM_AS_JUDGE_AGENTS 集合内容稳定性 — wave 1 治理依赖的核心契约。"""

    def test_contains_g20_must_vs_forbidden_validator(self) -> None:
        assert "must_vs_forbidden_validator" in LLM_AS_JUDGE_AGENTS

    def test_contains_g3_beat_alignment_validator(self) -> None:
        assert "beat_alignment_validator" in LLM_AS_JUDGE_AGENTS

    def test_contains_must_achieve_validator(self) -> None:
        assert "must_achieve_validator" in LLM_AS_JUDGE_AGENTS

    def test_contains_continuity_validator(self) -> None:
        assert "continuity_validator" in LLM_AS_JUDGE_AGENTS

    def test_contains_a3_plot_thread_merger(self) -> None:
        """A3 治本(v1.1 wave 2 session 5)— plot_thread_merger 加入 LLM-judge 保护栏。"""
        assert "plot_thread_merger" in LLM_AS_JUDGE_AGENTS

    def test_contains_a3_character_disambiguator(self) -> None:
        """A3 续(v1.1 wave 2 session 5,2026-05-29)— character_disambiguator 加入 LLM-judge 保护栏。"""
        assert "character_disambiguator" in LLM_AS_JUDGE_AGENTS

    def test_contains_a3_timeline_entry_merger(self) -> None:
        """A3 续(v1.1 wave 2 session 5,2026-05-29)— timeline_entry_merger 加入 LLM-judge 保护栏。"""
        assert "timeline_entry_merger" in LLM_AS_JUDGE_AGENTS

    def test_contains_a1_mechanism_consistency_validator(self) -> None:
        """A1(v1.1 wave 6 session 1,2026-06-04)— mechanism_consistency_validator
        加入 LLM-judge 保护栏(world 机制矛盾 / 缺规则判定须 thinking on)。"""
        assert "mechanism_consistency_validator" in LLM_AS_JUDGE_AGENTS

    def test_contains_a4_thread_closure_validator(self) -> None:
        """A4(v1.1 wave 6 session 1,2026-06-04)— thread_closure_validator
        加入 LLM-judge 保护栏(伏笔收口判定须 thinking on 护 C1 留白)。"""
        assert "thread_closure_validator" in LLM_AS_JUDGE_AGENTS

    def test_contains_a3_identity_coverage_validator(self) -> None:
        """A3(v1.1 wave 6 session 2,2026-06-04)— identity_coverage_validator
        加入 LLM-judge 保护栏(多情境身份覆盖度判定须 thinking on)。"""
        assert "identity_coverage_validator" in LLM_AS_JUDGE_AGENTS

    def test_contains_b1_setting_consistency_validator(self) -> None:
        """B1(v1.1 wave 6 session 3,2026-06-04)— setting_consistency_validator
        加入 LLM-judge 保护栏(本章正文违反设定契约判定须 thinking on)。"""
        assert "setting_consistency_validator" in LLM_AS_JUDGE_AGENTS

    def test_contains_b2_scene_anchor_validator(self) -> None:
        """B2(v1.1 wave 6 session 3,2026-06-04)— scene_anchor_validator
        加入 LLM-judge 保护栏(醒来点≠入梦点是否真硬跳复核须 thinking on)。"""
        assert "scene_anchor_validator" in LLM_AS_JUDGE_AGENTS

    def test_contains_b4_cross_chapter_dedup_validator(self) -> None:
        """B4(v1.1 wave 6 session 4,2026-06-04)— cross_chapter_dedup_validator
        加入 LLM-judge 保护栏(跨章重复是注水还是有意回扣判定须 thinking on)。"""
        assert "cross_chapter_dedup_validator" in LLM_AS_JUDGE_AGENTS

    def test_contains_b6_ending_tension_validator(self) -> None:
        """B6(v1.1 wave 6 session 4,2026-06-04)— ending_tension_validator
        加入 LLM-judge 保护栏(终章结局是否与前文张力断裂判定须 thinking on)。"""
        assert "ending_tension_validator" in LLM_AS_JUDGE_AGENTS

    def test_does_not_contain_writer(self) -> None:
        """writer 不在 LLM-judge 集合(避免 writer 切 thinking off 也被 guard 阻塞)。"""
        assert "writer" not in LLM_AS_JUDGE_AGENTS

    def test_does_not_contain_chapter_card_generator(self) -> None:
        """chapter_card_generator 不在(配置 thinking on 是软优化非治理硬约束)。"""
        assert "chapter_card_generator" not in LLM_AS_JUDGE_AGENTS

    def test_contains_volume_seam_replay_validator(self) -> None:
        """acceptance 修复1(2026-06-10)— volume_seam_replay_validator 加入
        LLM-judge 保护栏(卷界"重演 vs 后果延续"语义区分须 thinking on;标定
        实证 bigram 表面重叠分不开真假,全靠语义判)。"""
        assert "volume_seam_replay_validator" in LLM_AS_JUDGE_AGENTS

    def test_contains_must_feasibility_validator(self) -> None:
        """acceptance 修复2 / Fix D(2026-06-10)— must_feasibility_validator
        加入 LLM-judge 保护栏(写前"同一事件已消费 vs 后果延续 vs 可转述达成"
        语义区分须 thinking on;高置信才升人工,误杀代价高)。"""
        assert "must_feasibility_validator" in LLM_AS_JUDGE_AGENTS

    def test_contains_outline_consistency_validator(self) -> None:
        """v2 W3.5-S3(2026-06-23)— outline_consistency_validator 加入 LLM-judge
        保护栏(大纲跨段"同一实体是否矛盾 / 两角色是否被混为一人"语义判定须
        thinking on;qwen 例外亦 qwen_thinking,硬护栏不破)。"""
        assert "outline_consistency_validator" in LLM_AS_JUDGE_AGENTS

    def test_contains_forbidden_crossing_validator(self) -> None:
        """v2 W3.5 补强3(2026-06-26)— forbidden_crossing_validator 加入 LLM-judge
        保护栏(正文级 forbidden 越界判定须 thinking on:接近临界点 vs 真跨过 /
        隐式越界 vs 留白的语义区分,命门=零误杀;qwen 例外亦 qwen_thinking,硬护栏不破)。"""
        assert "forbidden_crossing_validator" in LLM_AS_JUDGE_AGENTS

    def test_size_is_eighteen(self) -> None:
        """wave 1 末 4 个 LLM-as-judge agent + v1.1 wave 2 session 5 加 3 个(plot_thread_merger +
        character_disambiguator + timeline_entry_merger)+ v1.1 wave 6 session 1 加 2 个
        (mechanism_consistency_validator + thread_closure_validator)+ v1.1 wave 6 session 2
        加 1 个(identity_coverage_validator)+ v1.1 wave 6 session 3 加 2 个
        (setting_consistency_validator + scene_anchor_validator)+ v1.1 wave 6 session 4
        加 2 个(cross_chapter_dedup_validator + ending_tension_validator)+ v1.1 acceptance
        修复1(2026-06-10)加 1 个(volume_seam_replay_validator)+ v1.1 acceptance
        修复2 / Fix D(2026-06-10)加 1 个(must_feasibility_validator)+ v2 W3.5-S3
        (2026-06-23)加 1 个(outline_consistency_validator)+ v2 W3.5 补强3
        (2026-06-26)加 1 个(forbidden_crossing_validator);增减须改
        invariants 文档 docs/decisions/v1_1_wave1_governance_invariants.md。"""
        assert len(LLM_AS_JUDGE_AGENTS) == 18


# ===========================================================================
# ModelRouter.select_profile hard guard 触发场景
# ===========================================================================


def _make_factory() -> object:
    """无副作用 LLMClient 工厂(返 MagicMock 占位)。"""
    factory = MagicMock()
    factory.return_value = MagicMock(spec=LLMClient)
    return factory


def _build_ollama_thinking_on_off_config() -> WriterProfilesConfig:
    """ollama 双 profile:thinking on / off,routing 默认 thinking on,
    LLM-judge 未显式列出时也走 thinking on default。"""
    profiles = {
        "local_thinking_on": WriterProfile(
            profile_id="local_thinking_on",
            provider="ollama",
            model="qwen-novel",
            base_url="http://localhost:11434",
            context_window=16000,
            extra_request_kwargs={"think": True},
        ),
        "local_thinking_off": WriterProfile(
            profile_id="local_thinking_off",
            provider="ollama",
            model="qwen-novel",
            base_url="http://localhost:11434",
            context_window=16000,
            extra_request_kwargs={"think": False},
        ),
    }
    routing = RoutingConfig(
        default="local_thinking_on",
        rules=[],
        agent_routing={
            "writer": "local_thinking_off",  # writer thinking off OK 不触发 guard
        },
    )
    return WriterProfilesConfig(profiles=profiles, routing=routing)


def _build_ollama_misconfigured_config() -> WriterProfilesConfig:
    """误配置:LLM-judge agent 切到 thinking off,应被 guard 截获。"""
    profiles = {
        "local_thinking_on": WriterProfile(
            profile_id="local_thinking_on",
            provider="ollama",
            model="qwen-novel",
            base_url="http://localhost:11434",
            context_window=16000,
            extra_request_kwargs={"think": True},
        ),
        "local_thinking_off": WriterProfile(
            profile_id="local_thinking_off",
            provider="ollama",
            model="qwen-novel",
            base_url="http://localhost:11434",
            context_window=16000,
            extra_request_kwargs={"think": False},
        ),
    }
    routing = RoutingConfig(
        default="local_thinking_off",  # default thinking off — 漂移温床
        rules=[],
        agent_routing={
            # must_vs_forbidden_validator 故意不列 → 走 default thinking off
            "writer": "local_thinking_off",
        },
    )
    return WriterProfilesConfig(profiles=profiles, routing=routing)


class TestRouterLLMJudgeHardGuard:
    """ModelRouter.select_profile 出口的 LLM-as-judge thinking-on hard guard。"""

    def test_llm_judge_with_thinking_on_passes(self) -> None:
        """当前全集合 LLM-judge agent 走 thinking-on profile → 不触发 guard。"""
        router = ModelRouter(
            _build_ollama_thinking_on_off_config(),
            client_factory=_make_factory(),
        )
        for agent in sorted(LLM_AS_JUDGE_AGENTS):
            profile = router.select_profile(agent)
            assert profile.profile_id == "local_thinking_on", (
                f"{agent} 应路由到 thinking-on profile"
            )

    def test_writer_with_thinking_off_passes(self) -> None:
        """writer 不在 LLM-judge 集合 → thinking off 不触发 guard。"""
        router = ModelRouter(
            _build_ollama_thinking_on_off_config(),
            client_factory=_make_factory(),
        )
        profile = router.select_profile("writer")
        assert profile.profile_id == "local_thinking_off"

    def test_llm_judge_resolved_to_thinking_off_raises(self) -> None:
        """全集合 LLM-judge agent 走 thinking off default → guard raise。"""
        router = ModelRouter(
            _build_ollama_misconfigured_config(),
            client_factory=_make_factory(),
        )
        for agent in sorted(LLM_AS_JUDGE_AGENTS):
            with pytest.raises(ValueError, match="LLM-as-judge agent"):
                router.select_profile(agent)

    def test_llm_judge_explicit_thinking_off_raises(self) -> None:
        """LLM-judge agent 显式路由到 thinking off → guard raise(显式漂移)。"""
        profiles = {
            "local_thinking_off": WriterProfile(
                profile_id="local_thinking_off",
                provider="ollama",
                model="qwen-novel",
                base_url="http://localhost:11434",
                context_window=16000,
                extra_request_kwargs={"think": False},
            ),
        }
        routing = RoutingConfig(
            default="local_thinking_off",
            rules=[],
            agent_routing={"beat_alignment_validator": "local_thinking_off"},
        )
        config = WriterProfilesConfig(profiles=profiles, routing=routing)
        router = ModelRouter(config, client_factory=_make_factory())
        with pytest.raises(ValueError, match="thinking off"):
            router.select_profile("beat_alignment_validator")

    def test_guard_error_message_guides_fix(self) -> None:
        """错误信息应含 fix 引导(显式声明 thinking-on profile + docs reference)。"""
        router = ModelRouter(
            _build_ollama_misconfigured_config(),
            client_factory=_make_factory(),
        )
        with pytest.raises(ValueError) as exc_info:
            router.select_profile("continuity_validator")
        msg = str(exc_info.value)
        assert "continuity_validator" in msg
        assert "thinking" in msg.lower()
        assert "wave 1" in msg or "G3/G20" in msg

    def test_override_path_also_guarded(self) -> None:
        """override_profile_id 显式覆盖到 thinking off + LLM-judge agent → guard raise。

        防 maintainer 误用 override_profile_id 把 LLM-judge agent 漂移到 thinking off。
        """
        router = ModelRouter(
            _build_ollama_thinking_on_off_config(),
            client_factory=_make_factory(),
        )
        with pytest.raises(ValueError, match="thinking off"):
            router.select_profile(
                "must_achieve_validator",
                override_profile_id="local_thinking_off",
            )


class TestNonJudgeAgentsBypassGuard:
    """非 LLM-judge agent thinking off 时 guard 不应阻塞。"""

    def test_chapter_card_generator_thinking_off_passes(self) -> None:
        """chapter_card_generator 不在 LLM_AS_JUDGE_AGENTS → thinking off 不阻塞。"""
        profiles = {
            "local_thinking_off": WriterProfile(
                profile_id="local_thinking_off",
                provider="ollama",
                model="qwen-novel",
                base_url="http://localhost:11434",
                context_window=16000,
                extra_request_kwargs={"think": False},
            ),
        }
        routing = RoutingConfig(
            default="local_thinking_off",
            rules=[],
            agent_routing={"chapter_card_generator": "local_thinking_off"},
        )
        config = WriterProfilesConfig(profiles=profiles, routing=routing)
        router = ModelRouter(config, client_factory=_make_factory())
        # 不应抛 — chapter_card_generator 不在 LLM-judge 集合
        profile = router.select_profile("chapter_card_generator")
        assert profile.profile_id == "local_thinking_off"

    def test_writer_thinking_off_passes(self) -> None:
        """writer 即使 thinking off 也通过 — 正文写作不要求 reasoning mode。"""
        profiles = {
            "local_thinking_off": WriterProfile(
                profile_id="local_thinking_off",
                provider="ollama",
                model="qwen-novel",
                base_url="http://localhost:11434",
                context_window=16000,
                extra_request_kwargs={"think": False},
            ),
        }
        routing = RoutingConfig(
            default="local_thinking_off",
            rules=[],
            agent_routing={},
        )
        config = WriterProfilesConfig(profiles=profiles, routing=routing)
        router = ModelRouter(config, client_factory=_make_factory())
        # writer 走 default,thinking off,但 writer 不在 LLM-judge 集合 → pass
        profile = router.select_profile("writer")
        assert profile.profile_id == "local_thinking_off"
