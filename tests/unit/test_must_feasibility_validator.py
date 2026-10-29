"""must_feasibility_validator 单元测(v1.1 acceptance 修复2 / Fix D,2026-06-10)。

完全 mock ``LLMClient``;fixture 字面取自 **luo-accept-v1 ch-0007 真实现场**
(must"强行压制副人格救下流浪汉"已被 ch-0006 消费 — arc_beats 已含解决"掰开
手指 + 镇纸砸头"),让防回退测试钉死真实现场而非合成文本。
"""

from __future__ import annotations

import json
from pathlib import Path
from unittest.mock import MagicMock

import pytest

from src.core.agents import _prompt as prompt_module
from src.core.agents.must_feasibility_validator import (
    MUST_INFEASIBLE_CONFIDENCE_THRESHOLD,
    RECENT_SUMMARIES_FOR_PRECHECK,
    validate_must_feasibility,
)
from src.core.llm.base import LLMClient, LLMResponse, TokenUsage
from src.core.models.chapter_card import ChapterCard, ContinuityContext

# ---- luo-accept-v1 ch-0007 真实字面(2026-06-10 acceptance 现场) ------------

_CH7_MUST_CONSUMED = "沈舟在防空洞强行压制副人格,救下被掐住的流浪汉"
_CH6_ARC_BEAT = (
    "沈舟从旁观梦境的承受者转为必须直面侵蚀威胁的求生者——在防空洞内死死掰开"
    "自己掐住流浪汉脖子的手指,用随身携带的镇纸砸破额头换取清醒。"
)
_CH6_SUMMARY = (
    "沈舟实地勘察老街废弃防空洞,确认其与梦中诏狱结构重合;遭锦衣卫人格侵蚀,"
    "险些掐死流浪汉,最终掰开手指、自伤换取清醒,流浪汉已获救逃离。"
)
_SUGGESTION = (
    "该 must 已被 ch-0006 消费(冲突已解决),建议从卡中删除,或改写为"
    "“沈舟处理掐人事件的心理余波”等后果延续式措辞。"
)


def _prev_card(**overrides: object) -> ChapterCard:
    payload: dict = dict(
        chapter_id="ch-0006",
        workspace_id="ws-demo",
        volume_id="v01",
        chapter_number=6,
        title="第六章·防空洞",
        tension_level=8,
        word_count_min=2500,
        word_count_max=4000,
        plot_node_budget=3,
        must_achieve=["沈舟确认防空洞与梦中诏狱结构重合"],
        must_unresolved=["锦衣卫人格的来历"],
        forbidden=[],
        pov_character="shen-zhou",
        setting="老街废弃防空洞",
        continuity_context=ContinuityContext(
            prev_chapter_tail="他攥着手电,光柱抖得厉害。",
            unfinished_actions=["防空洞深处的铁门未开"],
            time_gap="立即续接",
            scene_continuation="same",
            pov_change=False,
            pov_from="shen-zhou",
            pov_to="shen-zhou",
        ),
        next_chapter_seed="诊所里那个新病人的眼神不对劲",
        arc_phase="climax",
        is_arc_finale=True,
        related_plot_threads=["thread-main"],
        related_characters=["shen-zhou"],
        review_threshold=7.5,
        revise_threshold=6.5,
        max_rewrites=3,
        max_revises=2,
        arc_beats={"shen-zhou": _CH6_ARC_BEAT},
    )
    payload.update(overrides)
    return ChapterCard(**payload)


def _item_payload(
    must: str,
    verdict: str,
    confidence: float,
    *,
    suggestion: str = "",
) -> dict:
    return {
        "must": must,
        "verdict": verdict,
        "confidence": confidence,
        "evidence": "前章已完结收束写明该冲突已解决。",
        "suggestion": suggestion,
    }


def _make_response(items: list[dict], *, fenced: bool = False) -> LLMResponse:
    body = json.dumps({"items": items}, ensure_ascii=False)
    content = f"```json\n{body}\n```" if fenced else body
    return LLMResponse(
        content=content,
        model_used="qwen-max",
        usage=TokenUsage(input_tokens=600, output_tokens=120),
        finish_reason="stop",
    )


@pytest.fixture
def fake_prompts_dir(tmp_path: Path) -> Path:
    prompts_dir = tmp_path / "prompts"
    prompts_dir.mkdir()
    (prompts_dir / "must_feasibility_validator_v1.md").write_text(
        "FAKE_MUST_FEASIBILITY_VALIDATOR_PROMPT\n\n用于单测的 system 标记。",
        encoding="utf-8",
    )
    return prompts_dir


@pytest.fixture(autouse=True)
def _clear_prompt_cache() -> None:
    prompt_module.load_prompt_template.cache_clear()


@pytest.fixture
def mock_client() -> MagicMock:
    client = MagicMock(spec=LLMClient)
    client.complete.return_value = _make_response(
        [_item_payload(_CH7_MUST_CONSUMED, "feasible", 0.9)]
    )
    return client


class TestConstants:
    def test_confidence_threshold_locked(self) -> None:
        """高置信门槛 0.8 是设计常量(写前误杀代价高,宁漏不误杀);变更须
        重新评估写前/写后判定方向的反向设计(模块 docstring)。"""
        assert MUST_INFEASIBLE_CONFIDENCE_THRESHOLD == 0.8

    def test_recent_summaries_constant_locked(self) -> None:
        assert RECENT_SUMMARIES_FOR_PRECHECK == 3


class TestSkipPaths:
    def test_empty_must_achieve_skips_zero_llm(
        self, mock_client: MagicMock, fake_prompts_dir: Path
    ) -> None:
        result = validate_must_feasibility(
            must_achieve=[],
            chapter_id="ch-0007",
            prev_chapter_card=_prev_card(),
            client=mock_client,
            model="qwen-max",
            prompts_dir=fake_prompts_dir,
        )
        assert result.passed is True
        assert result.skipped is True
        assert result.llm_calls_count == 0
        mock_client.complete.assert_not_called()

    def test_no_context_skips_zero_llm(
        self, mock_client: MagicMock, fake_prompts_dir: Path
    ) -> None:
        """prev card + 近章摘要全空(典型 = 全书首章)→ skip 零 LLM。"""
        result = validate_must_feasibility(
            must_achieve=[_CH7_MUST_CONSUMED],
            chapter_id="ch-0001",
            prev_chapter_card=None,
            recent_summaries=[],
            client=mock_client,
            model="qwen-max",
            prompts_dir=fake_prompts_dir,
        )
        assert result.passed is True
        assert result.skipped is True
        mock_client.complete.assert_not_called()


class TestVerdicts:
    def test_consumed_high_confidence_fails_with_finding(
        self, mock_client: MagicMock, fake_prompts_dir: Path
    ) -> None:
        """ch-0007 真实现场:must 已被 ch-0006 消费,高置信 → finding(含
        suggestion 透传供孤儿章 UI 展示)。"""
        mock_client.complete.return_value = _make_response(
            [
                _item_payload(
                    _CH7_MUST_CONSUMED,
                    "consumed",
                    0.95,
                    suggestion=_SUGGESTION,
                )
            ]
        )
        result = validate_must_feasibility(
            must_achieve=[_CH7_MUST_CONSUMED],
            chapter_id="ch-0007",
            prev_chapter_card=_prev_card(),
            recent_summaries=[("ch-0006", _CH6_SUMMARY, [])],
            client=mock_client,
            model="qwen-max",
            prompts_dir=fake_prompts_dir,
        )
        assert result.passed is False
        assert len(result.findings) == 1
        finding = result.findings[0]
        assert finding.must_text == _CH7_MUST_CONSUMED
        assert finding.confidence == 0.95
        assert finding.suggestion == _SUGGESTION
        assert result.llm_calls_count == 1
        assert [item.status for item in result.constraint_results] == ["fail"]
        assert result.constraint_results[0].rule_text == _CH7_MUST_CONSUMED

    def test_consumed_below_threshold_passes(
        self, mock_client: MagicMock, fake_prompts_dir: Path
    ) -> None:
        """consumed 但置信 < 0.8 → 放行写作(宁漏不误杀;写后 judge 兜底)。"""
        mock_client.complete.return_value = _make_response(
            [_item_payload(_CH7_MUST_CONSUMED, "consumed", 0.7)]
        )
        result = validate_must_feasibility(
            must_achieve=[_CH7_MUST_CONSUMED],
            chapter_id="ch-0007",
            prev_chapter_card=_prev_card(),
            client=mock_client,
            model="qwen-max",
            prompts_dir=fake_prompts_dir,
        )
        assert result.passed is True
        assert result.findings == []
        assert [item.status for item in result.constraint_results] == ["pass"]

    def test_uncertain_passes(
        self, mock_client: MagicMock, fake_prompts_dir: Path
    ) -> None:
        """uncertain 放行 — 与写后 must_achieve_validator"uncertain 视作
        missing"刻意反向(写前误杀代价高)的关键不变量。"""
        mock_client.complete.return_value = _make_response(
            [_item_payload(_CH7_MUST_CONSUMED, "uncertain", 0.99)]
        )
        result = validate_must_feasibility(
            must_achieve=[_CH7_MUST_CONSUMED],
            chapter_id="ch-0007",
            prev_chapter_card=_prev_card(),
            client=mock_client,
            model="qwen-max",
            prompts_dir=fake_prompts_dir,
        )
        assert result.passed is True
        assert result.findings == []

    def test_feasible_passes(
        self, mock_client: MagicMock, fake_prompts_dir: Path
    ) -> None:
        result = validate_must_feasibility(
            must_achieve=[_CH7_MUST_CONSUMED],
            chapter_id="ch-0007",
            prev_chapter_card=_prev_card(),
            client=mock_client,
            model="qwen-max",
            prompts_dir=fake_prompts_dir,
        )
        assert result.passed is True
        assert result.llm_calls_count == 1

    def test_mixed_batch_only_high_confidence_consumed_reported(
        self, mock_client: MagicMock, fake_prompts_dir: Path
    ) -> None:
        """批处理 3 条:consumed 高置信 1 条 → 仅 1 条 finding;1 次 LLM。"""
        musts = [_CH7_MUST_CONSUMED, "沈舟接诊发狂病患", "白露发现新线索"]
        mock_client.complete.return_value = _make_response(
            [
                _item_payload(musts[0], "consumed", 0.9, suggestion=_SUGGESTION),
                _item_payload(musts[1], "feasible", 0.8),
                _item_payload(musts[2], "uncertain", 0.5),
            ]
        )
        result = validate_must_feasibility(
            must_achieve=musts,
            chapter_id="ch-0007",
            prev_chapter_card=_prev_card(),
            client=mock_client,
            model="qwen-max",
            prompts_dir=fake_prompts_dir,
        )
        assert result.passed is False
        assert len(result.findings) == 1
        assert result.findings[0].must_text == musts[0]
        assert result.items_total == 3
        assert result.llm_calls_count == 1


class TestFailOpen:
    def test_llm_error_fails_open(
        self, mock_client: MagicMock, fake_prompts_dir: Path
    ) -> None:
        mock_client.complete.side_effect = RuntimeError("api down")
        result = validate_must_feasibility(
            must_achieve=[_CH7_MUST_CONSUMED],
            chapter_id="ch-0007",
            prev_chapter_card=_prev_card(),
            client=mock_client,
            model="qwen-max",
            prompts_dir=fake_prompts_dir,
        )
        assert result.passed is True
        assert result.fail_open is True

    def test_unparseable_mock_output_fails_open(
        self, mock_client: MagicMock, fake_prompts_dir: Path
    ) -> None:
        """mock 路径安全性:marker 未注册 → mock 返非 JSON 默认文本 →
        fail-open 放行写作(既有 mock 编排测零回归的机制保证)。"""
        mock_client.complete.return_value = LLMResponse(
            content="第一章正文……(mock 默认 writer 文本)",
            model_used="mock",
            usage=TokenUsage(input_tokens=10, output_tokens=10),
            finish_reason="stop",
        )
        result = validate_must_feasibility(
            must_achieve=[_CH7_MUST_CONSUMED],
            chapter_id="ch-0007",
            prev_chapter_card=_prev_card(),
            client=mock_client,
            model="qwen-max",
            prompts_dir=fake_prompts_dir,
        )
        assert result.passed is True
        assert result.fail_open is True

    def test_items_count_mismatch_fails_open(
        self, mock_client: MagicMock, fake_prompts_dir: Path
    ) -> None:
        """LLM 输出条数 ≠ 输入条数 = 输出不可信 → 整体 fail-open。"""
        mock_client.complete.return_value = _make_response(
            [
                _item_payload(_CH7_MUST_CONSUMED, "consumed", 0.95),
                _item_payload("多出来的幻觉条目", "consumed", 0.95),
            ]
        )
        result = validate_must_feasibility(
            must_achieve=[_CH7_MUST_CONSUMED],
            chapter_id="ch-0007",
            prev_chapter_card=_prev_card(),
            client=mock_client,
            model="qwen-max",
            prompts_dir=fake_prompts_dir,
        )
        assert result.passed is True
        assert result.fail_open is True

    def test_setup_failure_fails_open(self, mock_client: MagicMock) -> None:
        """prompt 模板缺失(setup 异常)→ fail-open,零 LLM 调用。"""
        result = validate_must_feasibility(
            must_achieve=[_CH7_MUST_CONSUMED],
            chapter_id="ch-0007",
            prev_chapter_card=_prev_card(),
            client=mock_client,
            model="qwen-max",
            prompts_dir=Path("/nonexistent/prompts"),
        )
        assert result.passed is True
        assert result.fail_open is True
        mock_client.complete.assert_not_called()


class TestPromptAssembly:
    def test_code_fenced_json_parsed(
        self, mock_client: MagicMock, fake_prompts_dir: Path
    ) -> None:
        mock_client.complete.return_value = _make_response(
            [_item_payload(_CH7_MUST_CONSUMED, "consumed", 0.9)],
            fenced=True,
        )
        result = validate_must_feasibility(
            must_achieve=[_CH7_MUST_CONSUMED],
            chapter_id="ch-0007",
            prev_chapter_card=_prev_card(),
            client=mock_client,
            model="qwen-max",
            prompts_dir=fake_prompts_dir,
        )
        assert result.passed is False
        assert len(result.findings) == 1

    def test_user_message_contains_context_and_marker(
        self, mock_client: MagicMock, fake_prompts_dir: Path
    ) -> None:
        """user message 含前章已完结收束(arc_beats)+ 近章摘要 + role marker;
        system 用模板文件。"""
        validate_must_feasibility(
            must_achieve=[_CH7_MUST_CONSUMED],
            chapter_id="ch-0007",
            prev_chapter_card=_prev_card(),
            recent_summaries=[("ch-0006", _CH6_SUMMARY, [])],
            client=mock_client,
            model="qwen-max",
            prompts_dir=fake_prompts_dir,
        )
        call = mock_client.complete.call_args
        system_text = call.kwargs["messages"][0].content
        user_text = call.kwargs["messages"][1].content
        assert "FAKE_MUST_FEASIBILITY_VALIDATOR_PROMPT" in system_text
        assert _CH6_ARC_BEAT in user_text
        assert _CH6_SUMMARY in user_text
        assert _CH7_MUST_CONSUMED in user_text
        assert "<!-- agent_role: must_feasibility_validator -->" in user_text


# ---- w2-s3:近章 unfinished_actions 渲染进预检 prompt(消费端补线)---------------

_CH28_UNFINISHED = "探查双界时空纽带断裂后的最终走向(尚未显现)"


class TestUnfinishedActionsWiringW2S3:
    """w2-s3:近章摘要的 ``unfinished_actions`` 渲染进 user message —— 摘要可能把
    "结局未知 / 表面收束"压成完成态,该字段把"事未完"的事实喂给预检,收窄
    "延续 / 收尾前文悬念"被误判 consumed 的面。钉死:有则渲染带标签 / 空则不渲染 /
    多章不错位。
    """

    def test_unfinished_actions_rendered_with_label(
        self, mock_client: MagicMock, fake_prompts_dir: Path
    ) -> None:
        validate_must_feasibility(
            must_achieve=[_CH7_MUST_CONSUMED],
            chapter_id="ch-0029",
            prev_chapter_card=_prev_card(),
            recent_summaries=[("ch-0028", _CH6_SUMMARY, [_CH28_UNFINISHED])],
            client=mock_client,
            model="qwen-max",
            prompts_dir=fake_prompts_dir,
        )
        user_text = mock_client.complete.call_args.kwargs["messages"][1].content
        assert _CH28_UNFINISHED in user_text
        assert "该章未完结动作" in user_text

    def test_empty_unfinished_actions_not_rendered(
        self, mock_client: MagicMock, fake_prompts_dir: Path
    ) -> None:
        validate_must_feasibility(
            must_achieve=[_CH7_MUST_CONSUMED],
            chapter_id="ch-0029",
            prev_chapter_card=_prev_card(),
            recent_summaries=[("ch-0028", _CH6_SUMMARY, [])],
            client=mock_client,
            model="qwen-max",
            prompts_dir=fake_prompts_dir,
        )
        user_text = mock_client.complete.call_args.kwargs["messages"][1].content
        assert _CH6_SUMMARY in user_text  # 摘要照常渲染
        assert "该章未完结动作" not in user_text  # 空则不加标签行

    def test_multi_chapter_unfinished_no_mismatch(
        self, mock_client: MagicMock, fake_prompts_dir: Path
    ) -> None:
        """多章:每章 unfinished 跟随其摘要、不串章(顺序与摘要一致升序)。"""
        validate_must_feasibility(
            must_achieve=[_CH7_MUST_CONSUMED],
            chapter_id="ch-0029",
            prev_chapter_card=_prev_card(),
            recent_summaries=[
                ("ch-0027", "前章甲摘要梗概。", ["甲悬念尚未揭晓"]),
                ("ch-0028", _CH6_SUMMARY, [_CH28_UNFINISHED]),
            ],
            client=mock_client,
            model="qwen-max",
            prompts_dir=fake_prompts_dir,
        )
        user_text = mock_client.complete.call_args.kwargs["messages"][1].content
        assert user_text.index("ch-0027") < user_text.index("ch-0028")
        assert user_text.index("甲悬念尚未揭晓") < user_text.index(_CH28_UNFINISHED)
