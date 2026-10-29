"""Reviewer 边界升级 Claude 复审单测(P3-B-08)。

测试矩阵
========

- **legacy 路径(``router=None``)**:无 router → 永不触发升级,即使 score
  落在 [6.0, 8.0]。
- **router 在位 + 未配置 ``agent_routing.reviewer_escalation``**:不触发升
  级(对齐 P3-plan 决策 4 选 A 默认 disable 语义)。
- **router 在位 + 显式配置 ``agent_routing.reviewer_escalation``**:
  - score=7.0(区间内)→ 触发,返复审结果(替换首审)
  - score=6.0(下限闭区间)→ 触发
  - score=8.0(上限闭区间)→ 触发
  - score=5.9(略低于下限)→ 不触发
  - score=8.1(略高于上限)→ 不触发
  - score=9.0(高分)→ 不触发
- **复审结果替换首审**:升级路径下返回的 ``ReviewResult.reviewer_model``
  来自复审 LLM 响应,字段值采复审 payload。
- **routing 仅依赖键存在性**(``has_agent_routing``):配成 ``default`` 兜
  底 profile 仍触发(只要键存在),配 ``None`` 或不写键不触发。

注:本任务不验"reviewer_escalation 升级链路本身的 LLM 输出契约"——升级路
径复用同一份 reviewer.md prompt 与同一份 ``_perform_review``,只换 profile
路由名,审稿契约一致。语义层面在 P3-acceptance 真 LLM 联调时验。
"""

from __future__ import annotations

import json
from pathlib import Path
from unittest.mock import MagicMock

import pytest

from src.core.agents import _prompt as prompt_module
from src.core.agents.reviewer import (
    ESCALATION_LOWER_BOUND,
    ESCALATION_UPPER_BOUND,
    review_chapter,
)
from src.core.llm.base import LLMClient, LLMResponse, TokenUsage
from src.core.models.chapter_card import ChapterCard, ContinuityContext
from src.core.models.routing import (
    RoutingConfig,
    WriterProfilesConfig,
)
from src.core.models.writer_profile import WriterProfile
from src.core.routing import ModelRouter


# ---------- factory(对齐 test_reviewer.py) ----------


def _make_continuity(**overrides) -> ContinuityContext:
    payload = dict(
        prev_chapter_tail="他握紧剑柄。",
        unfinished_actions=[],  # P4-B-07:衔接桥默认通过
        time_gap="立即续接",
        scene_continuation="switch",  # P4-B-07:跳过 scene_continuation_location
        pov_change=False,
        pov_from="林衍",
        pov_to="林衍",
    )
    payload.update(overrides)
    return ContinuityContext(**payload)


def _make_card(**overrides) -> ChapterCard:
    payload: dict = dict(
        chapter_id="ch-0002",
        workspace_id="ws-demo",
        volume_id="v01",
        chapter_number=2,
        title="第二章",
        tension_level=8,
        word_count_min=2800,
        word_count_max=4500,
        plot_node_budget=3,
        must_achieve=["林衍突破第一道剑阵"],
        must_unresolved=["阵眼真凶身份"],
        forbidden=["现代科技元素"],
        pov_character="林衍",
        setting="青崖山顶残阵",
        continuity_context=_make_continuity(),
        next_chapter_seed="苏离消失前留下的玉佩",
        arc_phase="rising",
        is_arc_finale=False,
        related_plot_threads=["thread-main"],
        related_characters=["林衍"],
        review_threshold=7.5,
        revise_threshold=6.5,
        max_rewrites=3,
        max_revises=2,
    )
    payload.update(overrides)
    return ChapterCard(**payload)


def _llm_payload(*, score: float) -> dict:
    """把 6 分项分都设为同一值,综合分 = score。"""
    return {
        "score_plot_progression": score,
        "score_tension_match": score,
        "score_prose_quality": score,
        "score_consistency": score,
        "score_hook_management": score,
        "score_voice_consistency": score,
        "hard_constraints_passed": True,
        "failed_constraints": [],
        "issues": [],
        "suggestions": [],
    }


def _make_response(*, score: float, model_used: str) -> LLMResponse:
    return LLMResponse(
        content=json.dumps(_llm_payload(score=score), ensure_ascii=False),
        model_used=model_used,
        usage=TokenUsage(input_tokens=1000, output_tokens=200),
        finish_reason="end_turn",
    )


@pytest.fixture
def fake_prompts_dir(tmp_path: Path) -> Path:
    prompts_dir = tmp_path / "prompts"
    prompts_dir.mkdir()
    (prompts_dir / "reviewer.md").write_text(
        "FAKE_REVIEWER_SYSTEM_PROMPT\n\n用于单测。",
        encoding="utf-8",
    )
    return prompts_dir


@pytest.fixture(autouse=True)
def _clear_prompt_cache():
    prompt_module.load_prompt_template.cache_clear()
    yield
    prompt_module.load_prompt_template.cache_clear()


def _make_router(
    *,
    primary_client: LLMClient,
    escalation_client: LLMClient | None,
    include_escalation_routing: bool,
) -> ModelRouter:
    """构造一个 ModelRouter,Reviewer 主审与升级路由不同 profile。

    :param primary_client: ``router.route("reviewer")`` 命中后返的 client
        (本测试不真调 ollama,用 MagicMock spec=LLMClient)。
    :param escalation_client: ``router.route("reviewer_escalation")`` 命中
        后返的 client。
    :param include_escalation_routing: 是否在 ``agent_routing`` 显式配置
        ``reviewer_escalation`` 键(决定 ``has_agent_routing`` 返 True/False)。
    """
    profiles = {
        "local_qwen_safe": WriterProfile(
            profile_id="local_qwen_safe",
            provider="ollama",
            model="qwen-novel",
            base_url="http://localhost:11434",
            context_window=16000,
        ),
        "cloud_quality": WriterProfile(
            profile_id="cloud_quality",
            provider="claude",
            model="claude-sonnet-4-6",
            context_window=200000,
            generation_params={"temperature": 0.85, "max_tokens": 6000},
        ),
    }
    agent_routing = {"reviewer": "local_qwen_safe"}
    if include_escalation_routing:
        agent_routing["reviewer_escalation"] = "cloud_quality"
    routing = RoutingConfig(
        default="local_qwen_safe",
        rules=[],
        agent_routing=agent_routing,
    )
    config = WriterProfilesConfig(profiles=profiles, routing=routing)

    def fake_factory(
        *,
        model: str,
        base_url: str | None = None,
        provider: str | None = None,
    ) -> LLMClient:
        # 按 model 名分发主审 vs 复审 client
        if model == "claude-sonnet-4-6":
            assert escalation_client is not None, "缺 escalation_client"
            return escalation_client
        return primary_client

    return ModelRouter(config, client_factory=fake_factory)


# ---------- legacy 路径(router=None)永不触发 ----------


class TestLegacyPath:
    def test_no_router_no_escalation_even_if_score_in_band(
        self, fake_prompts_dir: Path
    ) -> None:
        primary = MagicMock(spec=LLMClient)
        primary.complete.return_value = _make_response(
            score=7.0, model_used="legacy-mock"
        )
        result = review_chapter(
            chapter_card=_make_card(),
            chapter_text="正文",
            chapter_id="ch-0002",
            version_reviewed=1,
            client=primary,
            model="qwen-novel",
            prompts_dir=fake_prompts_dir,
        )
        # 只调一次 LLM,返主审结果
        assert primary.complete.call_count == 1
        assert result.score_overall == pytest.approx(7.0)
        assert result.reviewer_model == "legacy-mock"


# ---------- router 在位 + 未配置 reviewer_escalation ----------


class TestRouterWithoutEscalationConfig:
    def test_no_escalation_config_no_trigger_even_in_band(
        self, fake_prompts_dir: Path
    ) -> None:
        primary = MagicMock(spec=LLMClient)
        primary.complete.return_value = _make_response(
            score=7.0, model_used="qwen-novel"
        )
        router = _make_router(
            primary_client=primary,
            escalation_client=None,
            include_escalation_routing=False,
        )
        result = review_chapter(
            chapter_card=_make_card(),
            chapter_text="正文",
            chapter_id="ch-0002",
            version_reviewed=1,
            router=router,
            prompts_dir=fake_prompts_dir,
        )
        # 主审一次调用,无升级
        assert primary.complete.call_count == 1
        assert result.score_overall == pytest.approx(7.0)
        # reviewer_model 来自主审 client
        assert result.reviewer_model == "qwen-novel"


# ---------- router 在位 + 显式配置 reviewer_escalation ----------


class TestRouterWithEscalationConfig:
    def test_score_in_band_triggers_escalation(
        self, fake_prompts_dir: Path
    ) -> None:
        # 主审 7.0 → 触发;复审给 8.5 → 最终结果用复审
        primary = MagicMock(spec=LLMClient)
        primary.complete.return_value = _make_response(
            score=7.0, model_used="qwen-novel"
        )
        escalation = MagicMock(spec=LLMClient)
        escalation.complete.return_value = _make_response(
            score=8.5, model_used="claude-sonnet-4-6-20250101"
        )
        router = _make_router(
            primary_client=primary,
            escalation_client=escalation,
            include_escalation_routing=True,
        )
        result = review_chapter(
            chapter_card=_make_card(),
            chapter_text="正文",
            chapter_id="ch-0002",
            version_reviewed=1,
            router=router,
            prompts_dir=fake_prompts_dir,
        )
        # 主审 + 复审各调一次
        assert primary.complete.call_count == 1
        assert escalation.complete.call_count == 1
        # 最终结果取复审
        assert result.score_overall == pytest.approx(8.5)
        assert result.reviewer_model == "claude-sonnet-4-6-20250101"
        assert result.verdict == "pass"

    def test_score_at_lower_bound_triggers_escalation(
        self, fake_prompts_dir: Path
    ) -> None:
        # score = 6.0 边界,闭区间内 → 触发
        assert ESCALATION_LOWER_BOUND == 6.0
        primary = MagicMock(spec=LLMClient)
        primary.complete.return_value = _make_response(
            score=6.0, model_used="qwen-novel"
        )
        escalation = MagicMock(spec=LLMClient)
        escalation.complete.return_value = _make_response(
            score=7.0, model_used="claude-sonnet-4-6"
        )
        router = _make_router(
            primary_client=primary,
            escalation_client=escalation,
            include_escalation_routing=True,
        )
        result = review_chapter(
            chapter_card=_make_card(),
            chapter_text="正文",
            chapter_id="ch-0002",
            version_reviewed=1,
            router=router,
            prompts_dir=fake_prompts_dir,
        )
        assert escalation.complete.call_count == 1
        assert result.score_overall == pytest.approx(7.0)

    def test_score_at_upper_bound_triggers_escalation(
        self, fake_prompts_dir: Path
    ) -> None:
        assert ESCALATION_UPPER_BOUND == 8.0
        primary = MagicMock(spec=LLMClient)
        primary.complete.return_value = _make_response(
            score=8.0, model_used="qwen-novel"
        )
        escalation = MagicMock(spec=LLMClient)
        escalation.complete.return_value = _make_response(
            score=8.7, model_used="claude-sonnet-4-6"
        )
        router = _make_router(
            primary_client=primary,
            escalation_client=escalation,
            include_escalation_routing=True,
        )
        result = review_chapter(
            chapter_card=_make_card(),
            chapter_text="正文",
            chapter_id="ch-0002",
            version_reviewed=1,
            router=router,
            prompts_dir=fake_prompts_dir,
        )
        assert escalation.complete.call_count == 1
        assert result.score_overall == pytest.approx(8.7)

    def test_score_below_lower_bound_no_escalation(
        self, fake_prompts_dir: Path
    ) -> None:
        # score = 5.9 略低于 6.0 → 不触发
        primary = MagicMock(spec=LLMClient)
        primary.complete.return_value = _make_response(
            score=5.9, model_used="qwen-novel"
        )
        escalation = MagicMock(spec=LLMClient)
        router = _make_router(
            primary_client=primary,
            escalation_client=escalation,
            include_escalation_routing=True,
        )
        result = review_chapter(
            chapter_card=_make_card(),
            chapter_text="正文",
            chapter_id="ch-0002",
            version_reviewed=1,
            router=router,
            prompts_dir=fake_prompts_dir,
        )
        # 复审 client 没调用
        assert primary.complete.call_count == 1
        assert escalation.complete.call_count == 0
        assert result.score_overall == pytest.approx(5.9)
        # 5.9 < revise_threshold 6.5 → rewrite
        assert result.verdict == "rewrite"

    def test_score_above_upper_bound_no_escalation(
        self, fake_prompts_dir: Path
    ) -> None:
        # score = 8.1 略高于 8.0 → 不触发
        primary = MagicMock(spec=LLMClient)
        primary.complete.return_value = _make_response(
            score=8.1, model_used="qwen-novel"
        )
        escalation = MagicMock(spec=LLMClient)
        router = _make_router(
            primary_client=primary,
            escalation_client=escalation,
            include_escalation_routing=True,
        )
        result = review_chapter(
            chapter_card=_make_card(),
            chapter_text="正文",
            chapter_id="ch-0002",
            version_reviewed=1,
            router=router,
            prompts_dir=fake_prompts_dir,
        )
        assert primary.complete.call_count == 1
        assert escalation.complete.call_count == 0
        assert result.score_overall == pytest.approx(8.1)
        # 8.1 >= 7.5 (review_threshold) → pass
        assert result.verdict == "pass"

    def test_high_score_no_escalation(
        self, fake_prompts_dir: Path
    ) -> None:
        # 9.0 远高于上限 → 不触发
        primary = MagicMock(spec=LLMClient)
        primary.complete.return_value = _make_response(
            score=9.0, model_used="qwen-novel"
        )
        escalation = MagicMock(spec=LLMClient)
        router = _make_router(
            primary_client=primary,
            escalation_client=escalation,
            include_escalation_routing=True,
        )
        result = review_chapter(
            chapter_card=_make_card(),
            chapter_text="正文",
            chapter_id="ch-0002",
            version_reviewed=1,
            router=router,
            prompts_dir=fake_prompts_dir,
        )
        assert escalation.complete.call_count == 0
        assert result.score_overall == pytest.approx(9.0)


# ---------- has_agent_routing 严格语义验 ----------


class TestHasAgentRoutingSemantics:
    """``has_agent_routing`` 严格按字典键存在性返 bool,不被 default 兜底干扰。"""

    def test_has_agent_routing_returns_false_for_unconfigured(self) -> None:
        primary = MagicMock(spec=LLMClient)
        router = _make_router(
            primary_client=primary,
            escalation_client=None,
            include_escalation_routing=False,
        )
        assert router.has_agent_routing("reviewer_escalation") is False
        # 已配置的 reviewer 应返 True
        assert router.has_agent_routing("reviewer") is True

    def test_has_agent_routing_returns_true_when_explicitly_configured(
        self,
    ) -> None:
        primary = MagicMock(spec=LLMClient)
        escalation = MagicMock(spec=LLMClient)
        router = _make_router(
            primary_client=primary,
            escalation_client=escalation,
            include_escalation_routing=True,
        )
        assert router.has_agent_routing("reviewer_escalation") is True


# ---------- 升级路径 routing 复审走 reviewer_escalation profile ----------


class TestEscalationProfileRouting:
    def test_escalation_path_uses_reviewer_escalation_profile(
        self, fake_prompts_dir: Path
    ) -> None:
        """复审实际命中 cloud_quality profile(model=claude-sonnet-4-6)。"""
        primary = MagicMock(spec=LLMClient)
        primary.complete.return_value = _make_response(
            score=7.0, model_used="qwen-novel"
        )
        escalation = MagicMock(spec=LLMClient)
        escalation.complete.return_value = _make_response(
            score=7.5, model_used="claude-sonnet-4-6"
        )
        router = _make_router(
            primary_client=primary,
            escalation_client=escalation,
            include_escalation_routing=True,
        )
        review_chapter(
            chapter_card=_make_card(),
            chapter_text="正文",
            chapter_id="ch-0002",
            version_reviewed=1,
            router=router,
            prompts_dir=fake_prompts_dir,
        )
        # 复审调用 model 参数应是 claude-sonnet-4-6(对齐 cloud_quality profile)
        escalation_call_kwargs = escalation.complete.call_args.kwargs
        assert escalation_call_kwargs["model"] == "claude-sonnet-4-6"
        # 主审调用 model 应是 qwen-novel(对齐 local_qwen_safe profile)
        primary_call_kwargs = primary.complete.call_args.kwargs
        assert primary_call_kwargs["model"] == "qwen-novel"
