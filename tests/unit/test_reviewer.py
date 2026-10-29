"""Reviewer agent 单测(P1-B-11 简版 + P2-B-11 voice 注入)。

完全 mock ``LLMClient``,不真调 API;用临时目录注入 fake ``reviewer.md``,
避免依赖仓库根真实提示词文件做断言。

P2-B-11 新增 ``TestVoiceProfileSection``(5 用例)覆盖 ``db_session`` 注入下
"## 出场角色声音档案(参考)"段在 user message 中的拼装行为。
"""

from __future__ import annotations

import json
from datetime import UTC, datetime
from pathlib import Path
from unittest.mock import MagicMock

import pytest
from pydantic import ValidationError
from sqlalchemy.orm import Session

from src.core.agents import _prompt as prompt_module
from src.core.agents import reviewer as reviewer_module
from src.core.agents.reviewer import ReviewerParseError, review_chapter
from src.core.llm.base import LLMClient, LLMResponse, Message, TokenUsage
from src.core.models.chapter_card import ChapterCard, ContinuityContext
from src.core.models.character import Character, VoiceProfile
from src.core.models.review import (
    REVIEWER_ISSUES_MAX_ITEMS,
    REVIEWER_SUGGESTION_MAX_CHARS,
    REVIEWER_SUGGESTIONS_MAX_ITEMS,
    REVIEW_ISSUE_DESCRIPTION_MAX_CHARS,
    REVIEW_ISSUE_PARAGRAPH_ANCHOR_MAX_CHARS,
    REVIEW_ISSUE_SUGGESTION_MAX_CHARS,
    REVIEW_ISSUE_TYPE_MAX_CHARS,
    ReviewIssue,
    ReviewResult,
)


def _make_continuity(**overrides) -> ContinuityContext:
    payload = dict(
        prev_chapter_tail="他握紧剑柄,目光不离对面那道身影。",
        unfinished_actions=["剑势悬而未发"],
        time_gap="立即续接",
        scene_continuation="same",
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
        title="第二章·破阵",
        tension_level=8,
        word_count_min=2800,
        word_count_max=4500,
        plot_node_budget=3,
        must_achieve=["林衍突破第一道剑阵", "苏离表明立场"],
        must_unresolved=["阵眼真凶身份"],
        forbidden=["现代科技元素", "提及第三卷剧情"],
        pov_character="林衍",
        setting="青崖山顶残阵",
        continuity_context=_make_continuity(),
        next_chapter_seed="苏离消失前留下的玉佩在颤动",
        arc_phase="rising",
        is_arc_finale=False,
        related_plot_threads=["thread-main", "thread-suli"],
        related_characters=["林衍", "苏离"],
        review_threshold=7.5,
        revise_threshold=6.5,
        max_rewrites=3,
        max_revises=2,
    )
    payload.update(overrides)
    return ChapterCard(**payload)


def _llm_payload(
    *,
    score_plot_progression: float = 8.0,
    score_tension_match: float = 8.0,
    score_prose_quality: float = 8.0,
    score_consistency: float = 8.0,
    score_hook_management: float = 8.0,
    score_voice_consistency: float = 8.0,
    score_imagery: float | None = None,
    score_interiority: float | None = None,
    score_formulaic: float | None = None,
    score_immersion: float | None = None,
    hard_constraints_passed: bool = True,
    failed_constraints: list[str] | None = None,
    issues: list[dict] | None = None,
    suggestions: list[str] | None = None,
) -> dict:
    payload: dict = {
        "score_plot_progression": score_plot_progression,
        "score_tension_match": score_tension_match,
        "score_prose_quality": score_prose_quality,
        "score_consistency": score_consistency,
        "score_hook_management": score_hook_management,
        "score_voice_consistency": score_voice_consistency,
        "hard_constraints_passed": hard_constraints_passed,
        "failed_constraints": failed_constraints or [],
        "issues": issues
        if issues is not None
        else [
            {
                "paragraph_anchor": "他握紧剑柄,目光不离对面那道身影",
                "issue_type": "pacing",
                "description": "铺陈过长延滞节奏",
                "suggestion": "压缩为 2-3 句直入剑势",
            }
        ],
        "suggestions": suggestions or ["下章张力升至 9 时压缩句长"],
    }
    # M2(v1.1 wave 5 s2):score_imagery 仅在显式给出时加入(默认不加 → 测"LLM
    # 漏产 → result.score_imagery is None"容错路径)。
    if score_imagery is not None:
        payload["score_imagery"] = score_imagery
    # M2(v1.1 wave 5 s3):score_interiority 同 imagery —— 仅显式给出时加入。
    if score_interiority is not None:
        payload["score_interiority"] = score_interiority
    # M2(v1.1 wave 5 s9):套路化 / 沉浸感同 imagery —— 仅显式给出时加入。
    if score_formulaic is not None:
        payload["score_formulaic"] = score_formulaic
    if score_immersion is not None:
        payload["score_immersion"] = score_immersion
    return payload


def _make_response(
    *,
    content: str | None = None,
    payload: dict | None = None,
    model_used: str = "claude-sonnet-4-6-20250101",
    finish_reason: str | None = "end_turn",
) -> LLMResponse:
    if content is None:
        content = json.dumps(payload if payload is not None else _llm_payload(), ensure_ascii=False)
    return LLMResponse(
        content=content,
        model_used=model_used,
        usage=TokenUsage(input_tokens=1500, output_tokens=400),
        finish_reason=finish_reason,
    )


@pytest.fixture
def fake_prompts_dir(tmp_path: Path) -> Path:
    """在临时目录里建一个 reviewer.md,内容可断言。"""
    prompts_dir = tmp_path / "prompts"
    prompts_dir.mkdir()
    (prompts_dir / "reviewer.md").write_text(
        "FAKE_REVIEWER_SYSTEM_PROMPT\n\n用于单测断言的 system 标记。",
        encoding="utf-8",
    )
    return prompts_dir


@pytest.fixture(autouse=True)
def _clear_prompt_cache():
    """每个用例清一次 lru_cache,避免不同 fake 模板互相污染。

    Rule of Three 抽取后,缓存对象在 ``src.core.agents._prompt`` 里,
    Writer/Reviewer/Reviser 共享。各自单测都按本 fixture 清一次,顺序
    无关。
    """
    prompt_module.load_prompt_template.cache_clear()
    yield
    prompt_module.load_prompt_template.cache_clear()


@pytest.fixture
def mock_client() -> MagicMock:
    client = MagicMock(spec=LLMClient)
    client.complete.return_value = _make_response()
    return client


def _call_review(
    mock_client: MagicMock,
    fake_prompts_dir: Path,
    *,
    card: ChapterCard | None = None,
    chapter_text: str = "正文(略)",
    chapter_id: str = "ch-0002",
    version_reviewed: int = 1,
    model: str = "claude-sonnet-4-6",
    **overrides,
) -> ReviewResult:
    return review_chapter(
        client=mock_client,
        chapter_card=card if card is not None else _make_card(),
        chapter_text=chapter_text,
        chapter_id=chapter_id,
        version_reviewed=version_reviewed,
        model=model,
        prompts_dir=fake_prompts_dir,
        **overrides,
    )


class TestVerdictMapping:
    """三档判决路径:pass / revise / rewrite(低分 + 硬约束失败)。"""

    def test_pass_when_all_scores_high(
        self, mock_client: MagicMock, fake_prompts_dir: Path
    ) -> None:
        # 所有分 8.0 → score_overall=8.0,≥ review_threshold=7.5 → pass
        mock_client.complete.return_value = _make_response(payload=_llm_payload())
        result = _call_review(mock_client, fake_prompts_dir)
        assert result.verdict == "pass"
        assert result.revise_targets == []
        assert result.score_overall == pytest.approx(8.0)

    def test_revise_when_score_in_middle_band(
        self, mock_client: MagicMock, fake_prompts_dir: Path
    ) -> None:
        # 所有分 7.0 → score_overall=7.0,落在 [6.5, 7.5) → revise
        mock_client.complete.return_value = _make_response(
            payload=_llm_payload(
                score_plot_progression=7.0,
                score_tension_match=7.0,
                score_prose_quality=7.0,
                score_consistency=7.0,
                score_hook_management=7.0,
                score_voice_consistency=7.0,
            )
        )
        result = _call_review(mock_client, fake_prompts_dir)
        assert result.verdict == "revise"
        assert result.score_overall == pytest.approx(7.0)
        # revise 时 revise_targets = issues
        assert len(result.revise_targets) == len(result.issues) == 1

    def test_rewrite_when_score_below_revise_threshold(
        self, mock_client: MagicMock, fake_prompts_dir: Path
    ) -> None:
        # 所有分 5.0 → score_overall=5.0,< revise_threshold=6.5 → rewrite
        mock_client.complete.return_value = _make_response(
            payload=_llm_payload(
                score_plot_progression=5.0,
                score_tension_match=5.0,
                score_prose_quality=5.0,
                score_consistency=5.0,
                score_hook_management=5.0,
                score_voice_consistency=5.0,
            )
        )
        result = _call_review(mock_client, fake_prompts_dir)
        assert result.verdict == "rewrite"
        assert result.revise_targets == []

    def test_hard_constraints_no_longer_drives_verdict_p7_b_06(
        self, mock_client: MagicMock, fake_prompts_dir: Path
    ) -> None:
        # P7-B-06:Reviewer 不再判 hard_constraints。即便 LLM 输出
        # ``hard_constraints_passed=False + failed_constraints=[...]``
        # (Pydantic ``extra=ignore`` 默认丢弃),reviewer 构造的
        # ReviewResult 仍按默认 hard_constraints_passed=True / failed=[]。
        # verdict 完全由 score_overall 决定。
        mock_client.complete.return_value = _make_response(
            payload=_llm_payload(
                score_plot_progression=9.0,
                score_tension_match=9.0,
                score_prose_quality=9.0,
                score_consistency=9.0,
                score_hook_management=9.0,
                score_voice_consistency=9.0,
                hard_constraints_passed=False,
                failed_constraints=["出现禁止元素:emoji"],
            )
        )
        result = _call_review(mock_client, fake_prompts_dir)
        # 9.0 score → pass(P7-B-06 不再走 hard_constraints 失败路径)
        assert result.verdict == "pass"
        # 默认值,LLM 字面 False 被丢弃
        assert result.hard_constraints_passed is True
        assert result.failed_constraints == []
        assert result.score_overall == pytest.approx(9.0)


class TestPromptAssembly:
    """user 含正文与章节卡硬约束;system 来自模板文件;不含衔接桥。"""

    def test_user_message_contains_chapter_text(
        self, mock_client: MagicMock, fake_prompts_dir: Path
    ) -> None:
        _call_review(
            mock_client,
            fake_prompts_dir,
            chapter_text="第二章测试正文,这是一段断言用文本。",
        )
        user_text = mock_client.complete.call_args.kwargs["messages"][1].content
        assert "第二章测试正文,这是一段断言用文本。" in user_text
        assert "## 待审正文" in user_text

    def test_dense_contrast_candidates_are_soft_and_precede_text(
        self, mock_client: MagicMock, fake_prompts_dir: Path
    ) -> None:
        chapter_text = (
            "他不是害怕，而是谨慎。"
            "她看似平静，实则一直挡在门前。"
        )
        _call_review(
            mock_client,
            fake_prompts_dir,
            chapter_text=chapter_text,
        )
        user_text = mock_client.complete.call_args.kwargs["messages"][1].content
        signal_pos = user_text.index("## 公式化对照句候选")
        text_pos = user_text.index("## 待审正文")
        assert signal_pos < text_pos
        assert "SOFT；不得单独决定 verdict" in user_text

    def test_single_contrast_sentence_is_not_injected_as_a_problem(
        self, mock_client: MagicMock, fake_prompts_dir: Path
    ) -> None:
        _call_review(
            mock_client,
            fake_prompts_dir,
            chapter_text="他不是拒绝合作，而是要求先核对授权。",
        )
        user_text = mock_client.complete.call_args.kwargs["messages"][1].content
        assert "## 公式化对照句候选" not in user_text

    def test_user_message_contains_card_constraints(
        self, mock_client: MagicMock, fake_prompts_dir: Path
    ) -> None:
        _call_review(mock_client, fake_prompts_dir)
        user_text = mock_client.complete.call_args.kwargs["messages"][1].content
        # 硬约束三组列表至少各命中一项
        assert "林衍突破第一道剑阵" in user_text  # must_achieve
        assert "阵眼真凶身份" in user_text  # must_unresolved
        assert "现代科技元素" in user_text  # forbidden
        # 视角 / 场景 / 字数区间
        assert "林衍" in user_text
        assert "青崖山顶残阵" in user_text
        assert "[2800, 4500]" in user_text

    def test_user_message_excludes_continuity_context(
        self, mock_client: MagicMock, fake_prompts_dir: Path
    ) -> None:
        # P1 简版:Reviewer 不需要衔接桥;prev_chapter_tail 不应进 user
        _call_review(mock_client, fake_prompts_dir)
        user_text = mock_client.complete.call_args.kwargs["messages"][1].content
        assert "他握紧剑柄,目光不离对面那道身影。" not in user_text
        assert "ContinuityContext" not in user_text

    def test_system_message_loaded_from_template_file(
        self, mock_client: MagicMock, fake_prompts_dir: Path
    ) -> None:
        _call_review(mock_client, fake_prompts_dir)
        messages = mock_client.complete.call_args.kwargs["messages"]
        assert messages[0].role == "system"
        assert "FAKE_REVIEWER_SYSTEM_PROMPT" in messages[0].content

    def test_user_message_ends_with_reviewer_role_marker(
        self, mock_client: MagicMock, fake_prompts_dir: Path
    ) -> None:
        # P1-B-22:reviewer marker 带 chapter_id(供 Mock LLM client 决定
        # verdict 路径);真 LLM 视 HTML 注释为字面忽略。
        _call_review(mock_client, fake_prompts_dir, chapter_id="ch-0002")
        user_text = mock_client.complete.call_args.kwargs["messages"][1].content
        assert user_text.rstrip().endswith(
            "<!-- agent_role: reviewer chapter_id: ch-0002 -->"
        )


class TestReturnValue:
    """``ReviewResult`` 字段完整且字段值正确透传 / 推算。"""

    def test_returns_review_result_with_all_fields(
        self, mock_client: MagicMock, fake_prompts_dir: Path
    ) -> None:
        mock_client.complete.return_value = _make_response(
            payload=_llm_payload(),
            model_used="claude-sonnet-4-6-20260101",
        )
        result = _call_review(
            mock_client,
            fake_prompts_dir,
            chapter_id="ch-0002",
            version_reviewed=3,
        )
        assert isinstance(result, ReviewResult)
        assert result.review_id.startswith("rev-")
        assert len(result.review_id) == len("rev-") + 8
        assert result.chapter_id == "ch-0002"
        assert result.version_reviewed == 3
        assert isinstance(result.reviewed_at, datetime)
        assert result.reviewed_at.tzinfo is UTC
        # reviewer_model 用响应里的 model_used,而非请求时的 model
        assert result.reviewer_model == "claude-sonnet-4-6-20260101"

    def test_six_subscores_passed_through(
        self, mock_client: MagicMock, fake_prompts_dir: Path
    ) -> None:
        mock_client.complete.return_value = _make_response(
            payload=_llm_payload(
                score_plot_progression=7.5,
                score_tension_match=8.5,
                score_prose_quality=6.0,
                score_consistency=9.0,
                score_hook_management=7.0,
                score_voice_consistency=8.0,
            )
        )
        result = _call_review(mock_client, fake_prompts_dir)
        assert result.score_plot_progression == 7.5
        assert result.score_tension_match == 8.5
        assert result.score_prose_quality == 6.0
        assert result.score_consistency == 9.0
        assert result.score_hook_management == 7.0
        assert result.score_voice_consistency == 8.0

    def test_imagery_passed_through(
        self, mock_client: MagicMock, fake_prompts_dir: Path
    ) -> None:
        # M2(G28):画面性分透传到 ReviewResult。
        mock_client.complete.return_value = _make_response(
            payload=_llm_payload(score_imagery=6.5)
        )
        result = _call_review(mock_client, fake_prompts_dir)
        assert result.score_imagery == 6.5

    def test_imagery_none_when_llm_omits(
        self, mock_client: MagicMock, fake_prompts_dir: Path
    ) -> None:
        # M2(G28)容错:LLM 漏产 score_imagery → result.score_imagery is None
        # (optional 字段,不抛 parse error)。
        mock_client.complete.return_value = _make_response(payload=_llm_payload())
        result = _call_review(mock_client, fake_prompts_dir)
        assert result.score_imagery is None

    def test_imagery_does_not_affect_score_overall(
        self, mock_client: MagicMock, fake_prompts_dir: Path
    ) -> None:
        # M2(G28)核心契约:imagery **永不进 score_overall 加权** → 极端 imagery
        # 值不改变 verdict 用的综合分(保护 G8 reviewer 基线 + design §M2"M2 不进
        # 第一关闸门")。同样 6 维分,imagery=0 vs imagery=10 → score_overall 一致。
        base = dict(
            score_plot_progression=8.0,
            score_tension_match=8.0,
            score_prose_quality=8.0,
            score_consistency=8.0,
            score_hook_management=8.0,
            score_voice_consistency=8.0,
        )
        mock_client.complete.return_value = _make_response(
            payload=_llm_payload(score_imagery=0.0, **base)
        )
        low = _call_review(mock_client, fake_prompts_dir).score_overall
        mock_client.complete.return_value = _make_response(
            payload=_llm_payload(score_imagery=10.0, **base)
        )
        high = _call_review(mock_client, fake_prompts_dir).score_overall
        assert low == high  # imagery 不进加权 → score_overall 逐字节一致

    def test_interiority_passed_through(
        self, mock_client: MagicMock, fake_prompts_dir: Path
    ) -> None:
        # M2(s3):内心戏分透传到 ReviewResult。
        mock_client.complete.return_value = _make_response(
            payload=_llm_payload(score_interiority=6.5)
        )
        result = _call_review(mock_client, fake_prompts_dir)
        assert result.score_interiority == 6.5

    def test_interiority_none_when_llm_omits(
        self, mock_client: MagicMock, fake_prompts_dir: Path
    ) -> None:
        # M2(s3)容错:LLM 漏产 score_interiority → result.score_interiority is None
        # (optional 字段,不抛 parse error)。
        mock_client.complete.return_value = _make_response(payload=_llm_payload())
        result = _call_review(mock_client, fake_prompts_dir)
        assert result.score_interiority is None

    def test_interiority_does_not_affect_score_overall(
        self, mock_client: MagicMock, fake_prompts_dir: Path
    ) -> None:
        # M2(s3)核心契约:interiority **永不进 score_overall 加权** → 极端 interiority
        # 值不改变 verdict 用的综合分(保护 G8 reviewer 基线 + design §M2"M2 不进
        # 第一关闸门")。同样 6 维分,interiority=0 vs interiority=10 → score_overall 一致。
        base = dict(
            score_plot_progression=8.0,
            score_tension_match=8.0,
            score_prose_quality=8.0,
            score_consistency=8.0,
            score_hook_management=8.0,
            score_voice_consistency=8.0,
        )
        mock_client.complete.return_value = _make_response(
            payload=_llm_payload(score_interiority=0.0, **base)
        )
        low = _call_review(mock_client, fake_prompts_dir).score_overall
        mock_client.complete.return_value = _make_response(
            payload=_llm_payload(score_interiority=10.0, **base)
        )
        high = _call_review(mock_client, fake_prompts_dir).score_overall
        assert low == high  # interiority 不进加权 → score_overall 逐字节一致

    def test_formulaic_and_immersion_passed_through(
        self, mock_client: MagicMock, fake_prompts_dir: Path
    ) -> None:
        # M2(s9):套路化 / 沉浸感分透传到 ReviewResult。
        mock_client.complete.return_value = _make_response(
            payload=_llm_payload(score_formulaic=3.0, score_immersion=8.5)
        )
        result = _call_review(mock_client, fake_prompts_dir)
        assert result.score_formulaic == 3.0
        assert result.score_immersion == 8.5

    def test_formulaic_immersion_none_when_llm_omits(
        self, mock_client: MagicMock, fake_prompts_dir: Path
    ) -> None:
        # M2(s9)容错:LLM 漏产 → None(optional 字段,不抛 parse error,零回归)。
        mock_client.complete.return_value = _make_response(payload=_llm_payload())
        result = _call_review(mock_client, fake_prompts_dir)
        assert result.score_formulaic is None
        assert result.score_immersion is None

    def test_formulaic_immersion_do_not_affect_score_overall(
        self, mock_client: MagicMock, fake_prompts_dir: Path
    ) -> None:
        # M2(s9)核心契约:套路化 / 沉浸感 **永不进 score_overall 加权** → 保护 G8
        # reviewer 基线(同 imagery/interiority)。套路化是反极性维度,尤其要确认它
        # 不被误进加权(否则高套路稿 verdict 会被错误拉低 / 拉高)。
        base = dict(
            score_plot_progression=8.0,
            score_tension_match=8.0,
            score_prose_quality=8.0,
            score_consistency=8.0,
            score_hook_management=8.0,
            score_voice_consistency=8.0,
        )
        mock_client.complete.return_value = _make_response(
            payload=_llm_payload(score_formulaic=0.0, score_immersion=0.0, **base)
        )
        a = _call_review(mock_client, fake_prompts_dir).score_overall
        mock_client.complete.return_value = _make_response(
            payload=_llm_payload(score_formulaic=10.0, score_immersion=10.0, **base)
        )
        b = _call_review(mock_client, fake_prompts_dir).score_overall
        assert a == b  # 两维都不进加权 → score_overall 逐字节一致

    def test_score_overall_computed_per_spec_formula(
        self, mock_client: MagicMock, fake_prompts_dir: Path
    ) -> None:
        # 按 PROJECT_SPEC §4.4 加权:0.25 plot + 0.20 tension + 0.20 consistency
        # + 0.15 voice + 0.10 prose + 0.10 hook
        mock_client.complete.return_value = _make_response(
            payload=_llm_payload(
                score_plot_progression=10.0,
                score_tension_match=8.0,
                score_consistency=6.0,
                score_voice_consistency=4.0,
                score_prose_quality=2.0,
                score_hook_management=0.0,
            )
        )
        # 0.25*10 + 0.20*8 + 0.20*6 + 0.15*4 + 0.10*2 + 0.10*0
        # = 2.5 + 1.6 + 1.2 + 0.6 + 0.2 + 0.0 = 6.1
        result = _call_review(mock_client, fake_prompts_dir)
        assert result.score_overall == pytest.approx(6.1)

    def test_excellence_metrics_excluded_from_weight_tables(self) -> None:
        # v1.1 wave 7 / B 持久化 **保证①**(维护人明确要求):4 个卓越性纯度量
        # **结构性**不在 score_overall 权重表里 → 即便 wave 7 把它们持久化到
        # review_results 列,verdict 计算仍永不沾这 4 维。比 behavioral 测(改值
        # 不变 overall)更硬:直接断言 _compute_score_overall 用的两张权重表字面
        # 不含这 4 个 key。**谁可能破坏**:未来若有人误把某度量加进权重表想"让它
        # 参与打分"→ 本测立刻红,提醒"列存在 ≠ 该参与 verdict"(治理不变量)。
        from src.core.agents.reviewer import (
            _SCORE_WEIGHTS,
            _SCORE_WEIGHTS_WITH_TEMPLATE,
        )

        excellence = {
            "score_imagery",
            "score_interiority",
            "score_formulaic",
            "score_immersion",
        }
        assert excellence.isdisjoint(_SCORE_WEIGHTS.keys())
        assert excellence.isdisjoint(_SCORE_WEIGHTS_WITH_TEMPLATE.keys())

    def test_issues_and_suggestions_passed_through(
        self, mock_client: MagicMock, fake_prompts_dir: Path
    ) -> None:
        mock_client.complete.return_value = _make_response(
            payload=_llm_payload(
                # 让 verdict=revise,revise_targets 才会带 issues
                score_plot_progression=7.0,
                score_tension_match=7.0,
                score_prose_quality=7.0,
                score_consistency=7.0,
                score_hook_management=7.0,
                score_voice_consistency=7.0,
                issues=[
                    {
                        "paragraph_anchor": "锚点 A",
                        "issue_type": "pacing",
                        "description": "节奏问题",
                        "suggestion": "压缩",
                    },
                    {
                        "paragraph_anchor": "锚点 B",
                        "issue_type": "voice",
                        "description": "声音不一致",
                        "suggestion": "调整对白",
                    },
                ],
                suggestions=["建议 1", "建议 2"],
            )
        )
        result = _call_review(mock_client, fake_prompts_dir)
        assert len(result.issues) == 2
        assert all(isinstance(it, ReviewIssue) for it in result.issues)
        assert result.issues[0].issue_type == "pacing"
        assert result.suggestions == ["建议 1", "建议 2"]
        # revise 时 revise_targets 与 issues 内容一致
        assert len(result.revise_targets) == 2
        assert result.revise_targets[0].paragraph_anchor == "锚点 A"


class TestParseFailure:
    """LLM 输出非 JSON / 缺字段 / 字段类型错 → ``ReviewerParseError``。"""

    def test_non_json_output_raises_parse_error_with_raw(
        self, mock_client: MagicMock, fake_prompts_dir: Path
    ) -> None:
        mock_client.complete.return_value = _make_response(
            content="这不是 JSON,只是一段闲聊文字。",
            model_used="claude-sonnet-4-6-fail",
        )
        with pytest.raises(ReviewerParseError) as exc_info:
            _call_review(mock_client, fake_prompts_dir)
        assert exc_info.value.raw_output == "这不是 JSON,只是一段闲聊文字。"
        assert exc_info.value.model_used == "claude-sonnet-4-6-fail"

    def test_json_missing_field_raises_parse_error(
        self, mock_client: MagicMock, fake_prompts_dir: Path
    ) -> None:
        # 缺 score_voice_consistency
        partial = _llm_payload()
        del partial["score_voice_consistency"]
        mock_client.complete.return_value = _make_response(content=json.dumps(partial))
        with pytest.raises(ReviewerParseError):
            _call_review(mock_client, fake_prompts_dir)

    def test_json_score_out_of_range_raises_parse_error(
        self, mock_client: MagicMock, fake_prompts_dir: Path
    ) -> None:
        # 分数超出 [0, 10] 区间应被中间模型 ge/le 拦截
        bad = _llm_payload(score_plot_progression=11.0)
        mock_client.complete.return_value = _make_response(content=json.dumps(bad))
        with pytest.raises(ReviewerParseError):
            _call_review(mock_client, fake_prompts_dir)

    def test_json_wrapped_in_markdown_fence_is_tolerated(
        self, mock_client: MagicMock, fake_prompts_dir: Path
    ) -> None:
        # LLM 偶发把 JSON 套进 ```json ... ``` 即使 prompt 禁止;Reviewer 应剥掉
        body = json.dumps(_llm_payload(), ensure_ascii=False)
        wrapped = f"```json\n{body}\n```"
        mock_client.complete.return_value = _make_response(content=wrapped)
        result = _call_review(mock_client, fake_prompts_dir)
        assert result.verdict == "pass"

    def test_json_with_trailing_html_comment_is_tolerated(
        self, mock_client: MagicMock, fake_prompts_dir: Path
    ) -> None:
        # P1-B-22:真 LLM 极小概率把 user message 末尾的
        # ``<!-- agent_role: reviewer chapter_id: ch-0001 -->`` marker 复读
        # 到 JSON 输出尾部,Reviewer 应顺手剥掉。
        body = json.dumps(_llm_payload(), ensure_ascii=False)
        contaminated = (
            f"{body}\n<!-- agent_role: reviewer chapter_id: ch-0002 -->"
        )
        mock_client.complete.return_value = _make_response(content=contaminated)
        result = _call_review(mock_client, fake_prompts_dir)
        assert result.verdict == "pass"

    def test_json_in_fence_with_trailing_html_comment_is_tolerated(
        self, mock_client: MagicMock, fake_prompts_dir: Path
    ) -> None:
        # P1-B-22:fence 包裹 + 后面再跟一个 trailing 注释,两层杂质都剥。
        body = json.dumps(_llm_payload(), ensure_ascii=False)
        contaminated = (
            f"```json\n{body}\n```\n"
            "<!-- agent_role: reviewer chapter_id: ch-0002 -->"
        )
        mock_client.complete.return_value = _make_response(content=contaminated)
        result = _call_review(mock_client, fake_prompts_dir)
        assert result.verdict == "pass"

    def test_json_with_trailing_comma_repaired_by_json_repair(
        self, mock_client: MagicMock, fake_prompts_dir: Path
    ) -> None:
        """v1.0.x reviewer-resilience(2026-05-20):json_repair 兜底层修末尾多
        逗号 — 严格 ``json.loads`` 会拒收,json_repair 自动修复 → Pydantic 校验通过。
        """
        # 合法 payload 字段全在,只是末尾多一个 ``,``(本地 Qwen 偶发输出)
        body = json.dumps(_llm_payload(), ensure_ascii=False)
        # 在最后 } 前插入一个 trailing 逗号
        broken = body[:-1].rstrip() + ",\n}"
        mock_client.complete.return_value = _make_response(content=broken)
        result = _call_review(mock_client, fake_prompts_dir)
        # 修复成功 → 流程继续,verdict 由 score 决定(_llm_payload 默认 7.0+ → pass)
        assert result.verdict == "pass"

    def test_json_missing_comma_repaired_by_json_repair(
        self, mock_client: MagicMock, fake_prompts_dir: Path
    ) -> None:
        """v1.0.x reviewer-resilience:json_repair 修两字段之间漏的逗号
        (luo2 ch-0001 attempt 1 命中的真实 LLM 错误形态)。"""
        # 手工构造:第一个字段后漏逗号,严格 json.loads 必挂;score 全 8.5 → pass
        broken = (
            '{\n'
            '  "score_plot_progression": 8.5\n'
            '  "score_tension_match": 8.5,\n'
            '  "score_prose_quality": 8.5,\n'
            '  "score_consistency": 8.5,\n'
            '  "score_hook_management": 8.5,\n'
            '  "score_voice_consistency": 8.5,\n'
            '  "issues": [],\n'
            '  "suggestions": []\n'
            '}'
        )
        mock_client.complete.return_value = _make_response(content=broken)
        result = _call_review(mock_client, fake_prompts_dir)
        # 关键断言:解析成功(没抛 ReviewerParseError);verdict 取决于 score
        assert result.score_overall == pytest.approx(8.5)
        assert result.verdict == "pass"

    def test_json_garbage_input_still_raises_parse_error_after_repair_fails(
        self, mock_client: MagicMock, fake_prompts_dir: Path
    ) -> None:
        """v1.0.x reviewer-resilience:完全非 JSON 的纯文本 → json_repair 兜底
        也救不了 → 仍抛 ReviewerParseError(json_repair 对纯散文返字符串,
        非 dict 触发"json_repair returned non-dict"分支)。"""
        mock_client.complete.return_value = _make_response(
            content="这是一段完全的散文,没有任何 JSON 结构。",
            model_used="qwen-novel",
        )
        with pytest.raises(ReviewerParseError):
            _call_review(mock_client, fake_prompts_dir)


def _max_reviewer_issue_payload() -> dict[str, str]:
    return {
        "paragraph_anchor": "锚" * REVIEW_ISSUE_PARAGRAPH_ANCHOR_MAX_CHARS,
        "issue_type": "类" * REVIEW_ISSUE_TYPE_MAX_CHARS,
        "description": "描" * REVIEW_ISSUE_DESCRIPTION_MAX_CHARS,
        "suggestion": "改" * REVIEW_ISSUE_SUGGESTION_MAX_CHARS,
    }


class TestReviewerBusinessPayloadBounds:
    """F5：Reviewer 业务载荷在 Provider schema、本地解析和 retry 同尺。"""

    def test_review_issue_accepts_exact_string_boundaries(self) -> None:
        issue = ReviewIssue(**_max_reviewer_issue_payload())
        assert len(issue.paragraph_anchor) == REVIEW_ISSUE_PARAGRAPH_ANCHOR_MAX_CHARS
        assert len(issue.issue_type) == REVIEW_ISSUE_TYPE_MAX_CHARS
        assert len(issue.description) == REVIEW_ISSUE_DESCRIPTION_MAX_CHARS
        assert len(issue.suggestion) == REVIEW_ISSUE_SUGGESTION_MAX_CHARS

    @pytest.mark.parametrize(
        ("field_name", "max_chars"),
        [
            ("paragraph_anchor", REVIEW_ISSUE_PARAGRAPH_ANCHOR_MAX_CHARS),
            ("issue_type", REVIEW_ISSUE_TYPE_MAX_CHARS),
            ("description", REVIEW_ISSUE_DESCRIPTION_MAX_CHARS),
            ("suggestion", REVIEW_ISSUE_SUGGESTION_MAX_CHARS),
        ],
    )
    def test_review_issue_rejects_each_single_overlength_field(
        self, field_name: str, max_chars: int
    ) -> None:
        payload = _max_reviewer_issue_payload()
        payload[field_name] = "超" * (max_chars + 1)
        with pytest.raises(ValidationError):
            ReviewIssue(**payload)

    def test_combined_exact_boundary_payload_is_accepted(
        self, mock_client: MagicMock, fake_prompts_dir: Path
    ) -> None:
        payload = _llm_payload(
            score_plot_progression=7.0,
            score_tension_match=7.0,
            score_prose_quality=7.0,
            score_consistency=7.0,
            score_hook_management=7.0,
            score_voice_consistency=7.0,
            issues=[
                _max_reviewer_issue_payload()
                for _ in range(REVIEWER_ISSUES_MAX_ITEMS)
            ],
            suggestions=[
                "建" * REVIEWER_SUGGESTION_MAX_CHARS
                for _ in range(REVIEWER_SUGGESTIONS_MAX_ITEMS)
            ],
        )
        mock_client.complete.return_value = _make_response(payload=payload)

        result = _call_review(mock_client, fake_prompts_dir)

        assert result.verdict == "revise"
        assert len(result.issues) == REVIEWER_ISSUES_MAX_ITEMS
        assert len(result.revise_targets) == REVIEWER_ISSUES_MAX_ITEMS
        assert len(result.suggestions) == REVIEWER_SUGGESTIONS_MAX_ITEMS

    @pytest.mark.parametrize("field_name", ["issues", "suggestions"])
    def test_collection_plus_one_is_typed_parse_failure(
        self,
        field_name: str,
        mock_client: MagicMock,
        fake_prompts_dir: Path,
    ) -> None:
        payload = _llm_payload()
        if field_name == "issues":
            payload[field_name] = [
                _max_reviewer_issue_payload()
                for _ in range(REVIEWER_ISSUES_MAX_ITEMS + 1)
            ]
        else:
            payload[field_name] = [
                "建议" for _ in range(REVIEWER_SUGGESTIONS_MAX_ITEMS + 1)
            ]
        mock_client.complete.return_value = _make_response(payload=payload)

        with pytest.raises(ReviewerParseError):
            _call_review(mock_client, fake_prompts_dir)

    def test_standalone_suggestion_plus_one_is_typed_parse_failure(
        self, mock_client: MagicMock, fake_prompts_dir: Path
    ) -> None:
        payload = _llm_payload(
            suggestions=["建" * (REVIEWER_SUGGESTION_MAX_CHARS + 1)]
        )
        mock_client.complete.return_value = _make_response(payload=payload)

        with pytest.raises(ReviewerParseError):
            _call_review(mock_client, fake_prompts_dir)

    def test_strict_schema_carries_same_business_bounds(
        self, fake_prompts_dir: Path
    ) -> None:
        client = MagicMock(spec=LLMClient)
        captured: dict[str, object] = {}

        def prepare_schema(**kwargs: object) -> dict[str, object]:
            captured.update(kwargs)
            gen_params = kwargs["gen_params"]
            assert isinstance(gen_params, dict)
            return dict(gen_params)

        client.prepare_strict_json_schema.side_effect = prepare_schema
        client.complete.return_value = _make_response()

        _call_review(client, fake_prompts_dir)

        schema = captured["schema"]
        assert isinstance(schema, dict)
        properties = schema["properties"]
        issues = properties["issues"]
        suggestions = properties["suggestions"]
        assert issues["maxItems"] == REVIEWER_ISSUES_MAX_ITEMS
        assert suggestions["maxItems"] == REVIEWER_SUGGESTIONS_MAX_ITEMS
        issue_properties = issues["items"]["properties"]
        assert issue_properties["paragraph_anchor"]["maxLength"] == (
            REVIEW_ISSUE_PARAGRAPH_ANCHOR_MAX_CHARS
        )
        assert issue_properties["issue_type"]["maxLength"] == (
            REVIEW_ISSUE_TYPE_MAX_CHARS
        )
        assert issue_properties["description"]["maxLength"] == (
            REVIEW_ISSUE_DESCRIPTION_MAX_CHARS
        )
        assert issue_properties["suggestion"]["maxLength"] == (
            REVIEW_ISSUE_SUGGESTION_MAX_CHARS
        )
        assert suggestions["items"]["maxLength"] == REVIEWER_SUGGESTION_MAX_CHARS

    def test_existing_orchestrator_retry_recovers_from_oversized_payload(
        self,
        mock_client: MagicMock,
        fake_prompts_dir: Path,
        monkeypatch: pytest.MonkeyPatch,
    ) -> None:
        from src.core.orchestrator.chapter_orchestrator import _review_with_retry

        monkeypatch.setattr(reviewer_module.settings, "prompts_dir", fake_prompts_dir)
        oversized = _llm_payload(
            suggestions=["建" * (REVIEWER_SUGGESTION_MAX_CHARS + 1)]
        )
        mock_client.complete.side_effect = [
            _make_response(payload=oversized),
            _make_response(payload=_llm_payload()),
        ]

        result = _review_with_retry(
            router=None,
            client=mock_client,
            model="mock-model",
            chapter_card=_make_card(),
            chapter_text="正文",
            chapter_id="ch-0002",
            version_reviewed=1,
        )

        assert result.verdict == "pass"
        assert mock_client.complete.call_count == 2
        second_user_message = mock_client.complete.call_args_list[1].kwargs[
            "messages"
        ][1].content
        assert "上次输出格式错误反馈" in second_user_message


class TestPreviousAttemptErrorsFeedback:
    """v1.0.x reviewer-resilience(2026-05-20):retry 时把上次错误反馈给 LLM。"""

    def test_previous_attempt_errors_appended_to_user_message(
        self, mock_client: MagicMock, fake_prompts_dir: Path
    ) -> None:
        """传 previous_attempt_errors 时,user message 末尾应含"上次输出格式错误反馈"段。"""
        mock_client.complete.return_value = _make_response(
            content=json.dumps(_llm_payload())
        )
        _call_review(
            mock_client,
            fake_prompts_dir,
            previous_attempt_errors=[
                "第 1 次尝试失败:JSON 第 36 行漏逗号",
                "第 2 次尝试失败:issues[1].suggestion 字段缺失",
            ],
        )
        kwargs = mock_client.complete.call_args.kwargs
        user_message = kwargs["messages"][1].content
        assert "上次输出格式错误反馈" in user_message
        assert "第 1 次尝试失败:JSON 第 36 行漏逗号" in user_message
        assert "issues[1].suggestion 字段缺失" in user_message

    def test_no_feedback_section_when_previous_attempt_errors_none(
        self, mock_client: MagicMock, fake_prompts_dir: Path
    ) -> None:
        """``previous_attempt_errors=None`` → user message 与原路径完全一致。"""
        mock_client.complete.return_value = _make_response(
            content=json.dumps(_llm_payload())
        )
        _call_review(mock_client, fake_prompts_dir)
        kwargs = mock_client.complete.call_args.kwargs
        user_message = kwargs["messages"][1].content
        assert "上次输出格式错误反馈" not in user_message


class TestClientInvocation:
    """``client.complete`` 调用形态:一次、两条消息、参数透传。"""

    def test_complete_called_once_with_two_messages_default_params(
        self, mock_client: MagicMock, fake_prompts_dir: Path
    ) -> None:
        _call_review(mock_client, fake_prompts_dir)
        assert mock_client.complete.call_count == 1
        kwargs = mock_client.complete.call_args.kwargs
        messages = kwargs["messages"]
        assert len(messages) == 2
        assert messages[0].role == "system"
        assert messages[1].role == "user"
        assert all(isinstance(m, Message) for m in messages)
        assert kwargs["model"] == "claude-sonnet-4-6"
        assert kwargs["max_tokens"] == 2000
        assert kwargs["temperature"] == 0.3

    def test_caller_can_override_generation_params(
        self, mock_client: MagicMock, fake_prompts_dir: Path
    ) -> None:
        _call_review(
            mock_client,
            fake_prompts_dir,
            model="claude-opus-4-7",
            max_tokens=4000,
            temperature=0.5,
        )
        kwargs = mock_client.complete.call_args.kwargs
        assert kwargs["model"] == "claude-opus-4-7"
        assert kwargs["max_tokens"] == 4000
        assert kwargs["temperature"] == 0.5


class TestPromptsDirInjection:
    """``prompts_dir`` 注入路径:显式传 / 默认走 ``settings.prompts_dir``。"""

    def test_explicit_prompts_dir_overrides_settings(
        self,
        mock_client: MagicMock,
        fake_prompts_dir: Path,
        monkeypatch: pytest.MonkeyPatch,
        tmp_path: Path,
    ) -> None:
        monkeypatch.setattr(
            reviewer_module.settings, "prompts_dir", tmp_path / "ignored"
        )
        _call_review(mock_client, fake_prompts_dir)
        system_text = mock_client.complete.call_args.kwargs["messages"][0].content
        assert "FAKE_REVIEWER_SYSTEM_PROMPT" in system_text

    def test_default_prompts_dir_uses_settings(
        self,
        mock_client: MagicMock,
        tmp_path: Path,
        monkeypatch: pytest.MonkeyPatch,
    ) -> None:
        custom_dir = tmp_path / "settings-prompts"
        custom_dir.mkdir()
        (custom_dir / "reviewer.md").write_text(
            "FROM_SETTINGS_DEFAULT", encoding="utf-8"
        )
        monkeypatch.setattr(reviewer_module.settings, "prompts_dir", custom_dir)

        review_chapter(
            client=mock_client,
            chapter_card=_make_card(),
            chapter_text="正文(略)",
            chapter_id="ch-0002",
            version_reviewed=1,
            model="claude-sonnet-4-6",
        )
        system_text = mock_client.complete.call_args.kwargs["messages"][0].content
        assert "FROM_SETTINGS_DEFAULT" in system_text


class TestReviewerParseErrorClass:
    """``ReviewerParseError`` 自身字段。"""

    def test_exception_carries_raw_output_and_model(self) -> None:
        err = ReviewerParseError(
            "bad", raw_output="<原始输出>", model_used="claude-x"
        )
        assert str(err) == "bad"
        assert err.raw_output == "<原始输出>"
        assert err.model_used == "claude-x"
        # 仍是 ValueError 子类,Orchestrator 可按 ValueError 兜
        assert isinstance(err, ValueError)


# ---- P2-B-11 voice 注入路径 ------------------------------------------------


def _make_voice_profile(**overrides) -> VoiceProfile:
    payload = dict(
        catchphrases=["剑势如山"],
        tone="冷峻克制",
        sentence_style="短句直白",
        self_address="我",
        addresses_others={"苏离": "苏小姐"},
        forbidden_phrases=["哎呀", "哈哈哈"],
    )
    payload.update(overrides)
    return VoiceProfile(**payload)


def _make_character(
    *, character_id: str = "林衍", name: str = "林衍", **overrides
) -> Character:
    payload: dict = dict(
        character_id=character_id,
        workspace_id="ws-demo",
        name=name,
        aliases=[],
        archetype="寒门主角",
        appearance="青衣配剑",
        personality="沉静内敛",
        background="山门弃徒",
        voice_profile=_make_voice_profile(),
        goals_long_term=["重夺剑宗"],
        goals_short_term=["破阵"],
        current_state={},
        relationships={},
        abilities=[],
        secrets=[],
        first_appearance_chapter="ch-0001",
        last_updated_chapter="ch-0002",
    )
    payload.update(overrides)
    return Character(**payload)


class TestVoiceProfileSection:
    """P2-B-11:``db_session`` 注入路径下 user message 的"## 出场角色声音档案
    (参考)"段。Reviewer 用作评分对账数据(放 user 而非 system,与 Writer 区分)。
    """

    def test_no_section_when_db_session_none(
        self, mock_client: MagicMock, fake_prompts_dir: Path
    ) -> None:
        """``db_session=None`` → user message 不附 voice 段(P1 路径)。"""
        _call_review(mock_client, fake_prompts_dir)
        user_text = mock_client.complete.call_args.kwargs["messages"][1].content
        assert "## 出场角色声音档案" not in user_text

    def test_section_appears_with_voice_fields(
        self,
        mock_client: MagicMock,
        fake_prompts_dir: Path,
        monkeypatch: pytest.MonkeyPatch,
    ) -> None:
        """``db_session`` 非空 → user 含 voice 段;6 字段全展开。"""
        characters = {
            "林衍": _make_character(character_id="林衍", name="林衍"),
            "苏离": _make_character(
                character_id="苏离",
                name="苏离",
                voice_profile=_make_voice_profile(
                    catchphrases=[],
                    tone="清冷飘逸",
                    sentence_style="长句修饰",
                    self_address="本宫",
                    addresses_others={},
                    forbidden_phrases=["俗气"],
                ),
            ),
        }
        monkeypatch.setattr(
            reviewer_module,
            "get_character",
            lambda session, cid: characters.get(cid),
        )
        fake_session = MagicMock(spec=Session)
        _call_review(mock_client, fake_prompts_dir, db_session=fake_session)
        user_text = mock_client.complete.call_args.kwargs["messages"][1].content
        # 段标题与两个角色子标题
        assert "## 出场角色声音档案(参考)" in user_text
        assert "### 林衍(林衍)" in user_text
        assert "### 苏离(苏离)" in user_text
        # 林衍 6 字段
        assert "tone(语气):冷峻克制" in user_text
        assert "sentence_style(句式):短句直白" in user_text
        assert "self_address(自称):我" in user_text
        assert "addresses_others(对他人称呼):苏离→苏小姐" in user_text
        assert "catchphrases(口头禅):剑势如山" in user_text
        assert "forbidden_phrases(禁止用语):哎呀, 哈哈哈" in user_text
        # 苏离的空集合渲染成"无"
        assert "self_address(自称):本宫" in user_text

    def test_voice_section_position_between_constraints_and_text(
        self,
        mock_client: MagicMock,
        fake_prompts_dir: Path,
        monkeypatch: pytest.MonkeyPatch,
    ) -> None:
        """voice 段位置:constraints 块之后、待审正文之前——LLM 顺读"硬约束 →
        声音对账数据 → 正文",符合 P2-B-11 约定。
        """
        characters = {"林衍": _make_character(character_id="林衍", name="林衍")}
        monkeypatch.setattr(
            reviewer_module,
            "get_character",
            lambda session, cid: characters.get(cid),
        )
        fake_session = MagicMock(spec=Session)
        _call_review(
            mock_client,
            fake_prompts_dir,
            chapter_text="测试正文断言段",
            db_session=fake_session,
        )
        user_text = mock_client.complete.call_args.kwargs["messages"][1].content
        constraints_pos = user_text.find("## 章节卡硬约束")
        voice_pos = user_text.find("## 出场角色声音档案(参考)")
        text_pos = user_text.find("## 待审正文")
        assert -1 < constraints_pos < voice_pos < text_pos

    def test_section_skipped_when_related_empty(
        self,
        mock_client: MagicMock,
        fake_prompts_dir: Path,
        monkeypatch: pytest.MonkeyPatch,
    ) -> None:
        """``related_characters=[]`` → 不查 db,不附 voice 段。"""
        get_character_mock = MagicMock()
        monkeypatch.setattr(reviewer_module, "get_character", get_character_mock)
        fake_session = MagicMock(spec=Session)
        _call_review(
            mock_client,
            fake_prompts_dir,
            card=_make_card(related_characters=[]),
            db_session=fake_session,
        )
        user_text = mock_client.complete.call_args.kwargs["messages"][1].content
        assert "## 出场角色声音档案" not in user_text
        get_character_mock.assert_not_called()

    def test_section_skipped_when_all_characters_missing(
        self,
        mock_client: MagicMock,
        fake_prompts_dir: Path,
        monkeypatch: pytest.MonkeyPatch,
    ) -> None:
        """所有 ``related_characters`` 都查不到 → 不附 voice 段(等同 P1 路径)。"""
        monkeypatch.setattr(
            reviewer_module, "get_character", lambda session, cid: None
        )
        fake_session = MagicMock(spec=Session)
        _call_review(mock_client, fake_prompts_dir, db_session=fake_session)
        user_text = mock_client.complete.call_args.kwargs["messages"][1].content
        assert "## 出场角色声音档案" not in user_text

    def test_section_skips_missing_characters_individually(
        self,
        mock_client: MagicMock,
        fake_prompts_dir: Path,
        monkeypatch: pytest.MonkeyPatch,
    ) -> None:
        """部分查到 / 部分缺失 → 跳过缺失,只渲染查到的角色。"""
        characters = {"林衍": _make_character(character_id="林衍", name="林衍")}
        monkeypatch.setattr(
            reviewer_module,
            "get_character",
            lambda session, cid: characters.get(cid),
        )
        fake_session = MagicMock(spec=Session)
        # _make_card 默认 related_characters=["林衍", "苏离"]
        _call_review(mock_client, fake_prompts_dir, db_session=fake_session)
        user_text = mock_client.complete.call_args.kwargs["messages"][1].content
        assert "### 林衍(林衍)" in user_text
        assert "### 苏离" not in user_text


# ============================================================================
# v1.0.x Commit D:_resolve_reviewer_thresholds 三级 fallback
# ============================================================================


class TestResolveReviewerThresholdsFallback:
    """v1.0.x Commit D:``_resolve_reviewer_thresholds`` 三级 fallback 实证。

    优先级链(由高到低):
    1. 工作区 project.yaml 显式 reviewer 段(``ConfigService.load_reviewer_config_explicit``
       返非 None)→ 用工作区配置
    2. ``chapter_card.review_threshold / revise_threshold`` 字段(bug ⑤ tension-aware
       单章细粒度)→ 用章节卡字段
    3. ``ESCALATION_*_BOUND`` 模块常量(workspace_id=None 或 ConfigService 抛)
       兜底 escalation 上下界。
    """

    def test_workspace_explicit_overrides_chapter_card(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        """优先级 1:工作区显式 ReviewerConfig 覆盖 chapter_card 字段。"""
        from src.core.agents.reviewer import _resolve_reviewer_thresholds
        from src.core.models.reviewer_config import ReviewerConfig

        # 工作区显式设了 pass=8.5 / revise=7.0;chapter_card 字段是 6.0 / 5.5
        explicit_cfg = ReviewerConfig(
            pass_threshold=8.5,
            revise_threshold=7.0,
            escalation_lower_bound=5.5,
            escalation_upper_bound=7.5,
        )

        class _FakeConfigService:
            def load_reviewer_config_explicit(self, workspace_id: str):
                return explicit_cfg

            def load_reviewer_config(self, workspace_id: str):
                raise AssertionError("不应被调用:工作区显式覆盖优先")

        import src.services.config_service as cfg_mod

        monkeypatch.setattr(cfg_mod, "ConfigService", _FakeConfigService)

        card = _make_card(review_threshold=6.0, revise_threshold=5.5)
        result = _resolve_reviewer_thresholds(
            chapter_card=card, workspace_id="ws-x"
        )
        # 工作区显式值覆盖 chapter_card + escalation 也走工作区
        assert result == (8.5, 7.0, 5.5, 7.5)

    def test_chapter_card_fields_used_when_workspace_has_no_explicit_section(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        """优先级 2:工作区无显式 reviewer 段 → 用 chapter_card 字段
        (bug ⑤ tension-aware 单章阈值在工作区无 yaml 覆盖时生效)。"""
        from src.core.agents.reviewer import _resolve_reviewer_thresholds
        from src.core.models.reviewer_config import ReviewerConfig

        # 工作区无显式 → explicit 返 None
        # 引擎默认走 load_reviewer_config 路径返 7.5/6.5(模拟旧默认 / 兜底)
        engine_cfg = ReviewerConfig(
            pass_threshold=7.5,
            revise_threshold=6.5,
            escalation_lower_bound=6.0,
            escalation_upper_bound=8.0,
        )

        class _FakeConfigService:
            def load_reviewer_config_explicit(self, workspace_id: str):
                return None

            def load_reviewer_config(self, workspace_id: str):
                return engine_cfg

        import src.services.config_service as cfg_mod

        monkeypatch.setattr(cfg_mod, "ConfigService", _FakeConfigService)

        card = _make_card(review_threshold=6.0, revise_threshold=5.5)
        result = _resolve_reviewer_thresholds(
            chapter_card=card, workspace_id="ws-x"
        )
        # chapter_card 字段生效(pass=6.0 / revise=5.5),escalation 走引擎默认
        assert result == (6.0, 5.5, 6.0, 8.0)

    def test_workspace_id_none_falls_back_to_chapter_card_and_constants(
        self,
    ) -> None:
        """优先级 2/3 边界:workspace_id=None → 跳过 ConfigService 走 chapter_card
        字段 + 模块级 ESCALATION 常量(legacy 路径 / 单测 fixture)。"""
        from src.core.agents.reviewer import (
            ESCALATION_LOWER_BOUND,
            ESCALATION_UPPER_BOUND,
            _resolve_reviewer_thresholds,
        )

        card = _make_card(review_threshold=6.0, revise_threshold=5.5)
        result = _resolve_reviewer_thresholds(
            chapter_card=card, workspace_id=None
        )
        assert result == (
            6.0,
            5.5,
            ESCALATION_LOWER_BOUND,
            ESCALATION_UPPER_BOUND,
        )

    def test_config_service_load_failure_falls_back_gracefully(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        """ConfigService 抛异常 → swallow + 退到 chapter_card 字段 + 模块常量
        (对齐"辅助任务失败兜底"先例,不抛断 orchestrator 主流程)。"""
        from src.core.agents.reviewer import (
            ESCALATION_LOWER_BOUND,
            ESCALATION_UPPER_BOUND,
            _resolve_reviewer_thresholds,
        )

        class _ExplodingConfigService:
            def __init__(self) -> None:
                raise RuntimeError("ConfigService 初始化失败")

        import src.services.config_service as cfg_mod

        monkeypatch.setattr(cfg_mod, "ConfigService", _ExplodingConfigService)

        card = _make_card(review_threshold=6.0, revise_threshold=5.5)
        result = _resolve_reviewer_thresholds(
            chapter_card=card, workspace_id="ws-broken"
        )
        # 双 try/except 都 swallow → chapter_card 字段生效,escalation 走模块常量
        assert result == (
            6.0,
            5.5,
            ESCALATION_LOWER_BOUND,
            ESCALATION_UPPER_BOUND,
        )


class TestLocalReviewerResponseRecovery:
    @staticmethod
    def _local_failure(raw, *, finish_reason="stop"):
        from src.core.llm.local_json_contract import LocalJsonContract, LocalJsonResponseViolation
        from src.core.agents._structured_output import strict_schema_from_model
        from src.core.llm.strict_json_schema import canonical_schema_hash
        schema = strict_schema_from_model(reviewer_module._ReviewerLLMPayload)
        reviewer_module._apply_reviewer_business_bounds(schema)
        contract = LocalJsonContract("reviewer", schema, canonical_schema_hash(schema),
                                     reviewer_module._ReviewerLLMPayload)
        response = _make_response(content=raw, finish_reason=finish_reason)
        return LocalJsonResponseViolation(response, contract, ValueError("invalid local wire"))

    def test_missing_closer_reuses_existing_parser_and_keeps_same_strict_contract(
        self, mock_client, fake_prompts_dir,
    ):
        payload = reviewer_module._ReviewerLLMPayload.model_validate(_llm_payload()).model_dump()
        raw = json.dumps(payload, ensure_ascii=False)[:-1]
        failure = self._local_failure(raw)
        mock_client.complete.side_effect = failure
        result = _call_review(mock_client, fake_prompts_dir)
        assert result.verdict == "pass"
        assert mock_client.complete.call_count == 1
        assert failure.response.content == raw

    @pytest.mark.parametrize("damage", ["numeric_string", "extra_field", "truncated"])
    def test_local_contract_or_truncation_cannot_be_hidden_by_permissive_parser(
        self, mock_client, fake_prompts_dir, damage,
    ):
        payload = reviewer_module._ReviewerLLMPayload.model_validate(_llm_payload()).model_dump()
        if damage == "numeric_string":
            payload["score_plot_progression"] = "8"
        elif damage == "extra_field":
            payload["allow_pass"] = True
        raw = json.dumps(payload)
        if damage == "truncated":
            raw = raw[:-1]
        mock_client.complete.side_effect = self._local_failure(
            raw, finish_reason="length" if damage == "truncated" else "stop")
        with pytest.raises(ReviewerParseError):
            _call_review(mock_client, fake_prompts_dir)

    def test_schema_failure_enters_existing_bounded_retry(self, mock_client, fake_prompts_dir, monkeypatch):
        from src.core.orchestrator.chapter_orchestrator import _review_with_retry
        monkeypatch.setattr(reviewer_module.settings, "prompts_dir", fake_prompts_dir)
        raw = '{"score_plot_progression":"bad"}'
        mock_client.complete.side_effect = [self._local_failure(raw), _make_response()]
        result = _review_with_retry(router=None, client=mock_client, model="mock-model",
            chapter_card=_make_card(), chapter_text="正文", chapter_id="ch-0002", version_reviewed=1)
        assert result.verdict == "pass" and mock_client.complete.call_count == 2
        assert "上次输出格式错误反馈" in mock_client.complete.call_args_list[1].kwargs["messages"][1].content

    def test_recovery_keeps_consumed_attempt_in_existing_limit(self, mock_client, fake_prompts_dir, monkeypatch):
        from src.core.orchestrator.chapter_orchestrator import _review_with_retry
        monkeypatch.setattr(reviewer_module.settings, "prompts_dir", fake_prompts_dir)
        mock_client.complete.side_effect = self._local_failure('{"score_plot_progression":"bad"}')
        with pytest.raises(ReviewerParseError) as error:
            _review_with_retry(router=None, client=mock_client, model="mock-model",
                chapter_card=_make_card(), chapter_text="正文", chapter_id="ch-0002", version_reviewed=1,
                initial_attempt_errors=["第 1 次尝试失败:旧格式错误"])
        from src.core.orchestrator.chapter_orchestrator import _REVIEWER_MAX_ATTEMPTS
        assert mock_client.complete.call_count == _REVIEWER_MAX_ATTEMPTS - 1
        assert len(error.value.attempts_log) == _REVIEWER_MAX_ATTEMPTS


def test_local_reviewer_schema_drift_is_not_a_model_format_retry(mock_client, fake_prompts_dir):
    failure = TestLocalReviewerResponseRecovery._local_failure('{"score_plot_progression":8')
    failure.contract.schema["additionalProperties"] = True
    mock_client.complete.side_effect = failure
    with pytest.raises(ValueError, match="schema") as error:
        _call_review(mock_client, fake_prompts_dir)
    assert not isinstance(error.value, ReviewerParseError)
    assert mock_client.complete.call_count == 1
