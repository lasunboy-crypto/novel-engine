"""continuity_validator agent 单测(v1.0.x wave 3 Commit D)。

覆盖范围(对齐 plan §1 Part 4.1):

1. **happy 全 yes**:3 Q 条件全满足 + LLM 全 yes → passed=True
2. **HARD Q1 fail**:unfinished_actions 非空 + LLM no → failed_hard=["unfinished_actions"]
3. **HARD Q2 fail**:scene_continuation=same + LLM no → failed_hard=["scene_same"]
4. **HARD Q1+Q2 双 fail**:双 HARD 挂
5. **SOFT Q3 fail**:time_gap=三日后 + LLM no → passed=True(SOFT 不动 passed)+ soft_warnings
6. **HARD + SOFT 混合 fail**:Q1 + Q3 同时 fail → passed=False + soft_warnings 非空
7. **uncertain 分轴**:Q1/Q2 HARD 计 failed_hard；typed Q3 与 SOFT 计 soft_warnings
8. **首章 skip**:chapter_number=1 → skipped=True / 0 LLM call
9. **空 prev_chapter_tail skip**:prev_chapter_tail="" → skipped=True
10. **三 Q 条件全不满足 skip**:[] / switch / 立即续接 → skipped=True
11. **Q1 条件不满足**:unfinished_actions=[] → Q1 skip,Q2/Q3 正常跑
12. **Q2 条件不满足**:scene_continuation=switch → Q2 skip
13. **Q3 条件不满足**:time_gap=立即续接 → Q3 skip(兜底值)
14. **Q3 yes**:time_gap 非空 + LLM yes → soft_warnings 空
15. **LLM fail-open**:第一次 LLM 抛 → 整 validator fail-open / passed=True / fail_open=True
16. **setup fail-open**:prompt 文件缺失 → fail-open + 不发起 LLM 调用
17. **agent marker 识别**:user message 含 ``<!-- agent_role: continuity_validator -->``

完全 mock ``LLMClient``;用临时目录注入 fake ``continuity_validator_v1.md`` prompt 模板。
fixture 复用 must_achieve_validator 同款模式(``fake_prompts_dir`` + ``mock_client``)。
"""

from __future__ import annotations

import json
from pathlib import Path
from typing import Any
from unittest.mock import MagicMock

import pytest
import structlog
from structlog.testing import capture_logs

from src.core.agents import _prompt as prompt_module
from src.core.agents.continuity_validator import (
    ContinuityTechnicalError,
    ContinuityValidationResult,
    _build_q_specs,
    validate_continuity,
)
from src.core.llm.base import LLMClient, LLMResponse, TokenUsage
from src.core.models.chapter_card import ChapterCard, ContinuityContext
from src.core.models.core_state import (
    BookClockConstraint,
    BookClockInterval,
    advance_book_time_projection,
    build_typed_book_clock_constraint,
    make_core_state_projection,
)


# ---------------------------------------------------------------------------
# 工厂 & 公共 fixture(复用 test_validators_v2.py 同款模式)
# ---------------------------------------------------------------------------


def _make_continuity(**overrides: Any) -> ContinuityContext:
    payload: dict[str, Any] = dict(
        prev_chapter_tail="他握紧剑柄,目光落在阵眼上。" + "字" * 100,
        unfinished_actions=["剑势悬而未发"],
        time_gap="立即续接",
        scene_continuation="same",
        pov_change=False,
        pov_from="林衍",
        pov_to="林衍",
    )
    payload.update(overrides)
    return ContinuityContext(**payload)


def _make_card(**overrides: Any) -> ChapterCard:
    payload: dict[str, Any] = dict(
        chapter_id="ch-0002",
        workspace_id="ws-demo",
        volume_id="v01",
        chapter_number=2,
        title="第二章·破阵",
        tension_level=8,
        word_count_min=1,
        word_count_max=10_000_000,
        plot_node_budget=3,
        must_achieve=[],
        must_unresolved=[],
        forbidden=[],
        pov_character="林衍",
        setting="青崖山顶",
        continuity_context=_make_continuity(),
        next_chapter_seed="玉佩颤动",
        arc_phase="rising",
        is_arc_finale=False,
        related_plot_threads=["thread-main"],
        related_characters=["char-lin-yan"],
    )
    payload.update(overrides)
    return ChapterCard(**payload)


def _hard_clock_card() -> ChapterCard:
    previous = BookClockInterval(
        minimum_minutes=7 * 24 * 60,
        maximum_minutes=10 * 24 * 60,
    )
    return _make_card(
        continuity_context=_make_continuity(
            unfinished_actions=[],
            scene_continuation="switch",
            time_gap="一日后",
        ),
        core_state_contract_version=1,
        core_state_projection_hash="d" * 64,
        book_clock_constraint=BookClockConstraint(
            time_gap="一日后",
            previous_interval=previous,
            gap_interval=BookClockInterval(
                minimum_minutes=24 * 60,
                maximum_minutes=24 * 60,
            ),
            current_interval=BookClockInterval(
                minimum_minutes=8 * 24 * 60,
                maximum_minutes=11 * 24 * 60,
            ),
            is_hard=True,
        ),
    )


def _typed_clock_card(time_gap: str = "同夜稍后，约一小时后") -> ChapterCard:
    opening = build_typed_book_clock_constraint(
        chapter_id="ch-0001",
        time_gap="开篇",
        previous_projection=None,
        previous_projection_hash="0" * 64,
        is_first_chapter=True,
    )
    opening_time = advance_book_time_projection(
        previous_projection=None,
        constraint=opening,
    )
    projection = make_core_state_projection(
        book_clock=opening.current_interval,
        book_time=opening_time,
        book_time_contract_version=1,
    )
    clock = build_typed_book_clock_constraint(
        chapter_id="ch-0002",
        time_gap=time_gap,
        previous_projection=opening_time,
        previous_projection_hash=projection.projection_hash,
        is_first_chapter=False,
    )
    return _make_card(
        continuity_context=_make_continuity(
            unfinished_actions=[],
            scene_continuation="switch",
            time_gap=time_gap,
        ),
        core_state_contract_version=1,
        core_state_projection_hash=projection.projection_hash,
        book_clock_constraint=clock,
    )


def _make_response(
    *,
    verdict: str = "yes",
    evidence: str = "本章开头明确承接上一章",
    model_used: str = "qwen-novel:latest",
) -> LLMResponse:
    payload = {"verdict": verdict, "evidence": evidence}
    return LLMResponse(
        content=json.dumps(payload, ensure_ascii=False),
        model_used=model_used,
        usage=TokenUsage(input_tokens=500, output_tokens=30),
        finish_reason="stop",
    )


@pytest.fixture
def fake_prompts_dir(tmp_path: Path) -> Path:
    """在临时目录里建 continuity_validator_v1.md(fake)。"""
    prompts_dir = tmp_path / "prompts"
    prompts_dir.mkdir()
    (prompts_dir / "continuity_validator_v1.md").write_text(
        "FAKE_CONTINUITY_VALIDATOR_SYSTEM_PROMPT\n\n用于单测断言的 system 标记。",
        encoding="utf-8",
    )
    return prompts_dir


@pytest.fixture(autouse=True)
def _clear_prompt_cache() -> None:
    prompt_module.load_prompt_template.cache_clear()


@pytest.fixture(autouse=True)
def _ensure_debug_level_for_capture_logs() -> None:
    """``continuity_validator_skipped`` / ``_q_passed`` 是 DEBUG level。"""
    structlog.configure(wrapper_class=structlog.make_filtering_bound_logger(0))


@pytest.fixture
def mock_client() -> MagicMock:
    client = MagicMock(spec=LLMClient)
    client.complete.return_value = _make_response()
    return client


def _call(
    mock_client: MagicMock,
    fake_prompts_dir: Path,
    *,
    chapter_card: ChapterCard | None = None,
    chapter_text: str = "占位章节正文(中文,~500 字)。" + "字" * 500,
    chapter_id: str = "ch-0002",
    workspace_id: str = "ws-demo",
    model: str = "qwen-novel:latest",
    prev_chapter_card: ChapterCard | None = None,
) -> ContinuityValidationResult:
    return validate_continuity(
        chapter_text=chapter_text,
        chapter_card=chapter_card or _make_card(),
        chapter_id=chapter_id,
        workspace_id=workspace_id,
        client=mock_client,
        model=model,
        prompts_dir=fake_prompts_dir,
        prev_chapter_card=prev_chapter_card,
    )


# ---------------------------------------------------------------------------
# 1. Happy:3 Q 全 yes
# ---------------------------------------------------------------------------


class TestHappy:
    def test_three_qs_all_yes(
        self, mock_client: MagicMock, fake_prompts_dir: Path
    ) -> None:
        # 3 Q 全条件满足(unfinished_actions 非空 + scene_continuation=same +
        # time_gap=三日后)+ LLM 全 yes → passed=True / 0 failed / 0 soft warnings
        mock_client.complete.side_effect = [
            _make_response(verdict="yes", evidence="Q1 承接"),
            _make_response(verdict="yes", evidence="Q2 同空间"),
            _make_response(verdict="yes", evidence="Q3 时间一致"),
        ]
        card = _make_card(
            continuity_context=_make_continuity(
                unfinished_actions=["剑势悬而未发"],
                scene_continuation="same",
                time_gap="三日后",
            )
        )
        result = _call(mock_client, fake_prompts_dir, chapter_card=card)

        assert result.passed is True
        assert result.failed_hard_checks == []
        assert result.soft_warnings == []
        assert result.llm_calls_count == 3
        assert result.skipped is False
        assert result.fail_open is False
        assert mock_client.complete.call_count == 3
        assert [item.status for item in result.constraint_results] == [
            "pass",
            "pass",
        ]
        assert {item.domain for item in result.constraint_results} == {"continuity"}


# ---------------------------------------------------------------------------
# 2. HARD fail:Q1 unfinished_actions / Q2 scene_same / Q1+Q2 双 fail
# ---------------------------------------------------------------------------


class TestHardFail:
    def test_q1_unfinished_actions_fail(
        self, mock_client: MagicMock, fake_prompts_dir: Path
    ) -> None:
        # Q1 active + Q2 skip + Q3 skip;LLM 返 no → failed_hard=["unfinished_actions"]
        mock_client.complete.return_value = _make_response(
            verdict="no", evidence="本章开头无承接"
        )
        card = _make_card(
            continuity_context=_make_continuity(
                unfinished_actions=["剑势悬而未发"],
                scene_continuation="switch",  # Q2 skip
                time_gap="立即续接",  # Q3 skip(兜底值)
            )
        )
        result = _call(mock_client, fake_prompts_dir, chapter_card=card)

        assert result.passed is False
        assert result.failed_hard_checks == ["unfinished_actions"]
        assert result.soft_warnings == []
        assert result.llm_calls_count == 1
        assert result.fail_open is False
        assert [item.status for item in result.constraint_results] == ["fail"]
        assert "剑势悬而未发" in result.constraint_results[0].rule_text

    def test_q2_scene_same_fail(
        self, mock_client: MagicMock, fake_prompts_dir: Path
    ) -> None:
        # Q1 skip + Q2 active + Q3 skip;LLM 返 no → failed_hard=["scene_same"]
        mock_client.complete.return_value = _make_response(
            verdict="no", evidence="本章开头切到新地方"
        )
        card = _make_card(
            continuity_context=_make_continuity(
                unfinished_actions=[],  # Q1 skip
                scene_continuation="same",
                time_gap="立即续接",  # Q3 skip
            )
        )
        result = _call(mock_client, fake_prompts_dir, chapter_card=card)

        assert result.passed is False
        assert result.failed_hard_checks == ["scene_same"]
        assert result.soft_warnings == []
        assert result.llm_calls_count == 1
        assert [item.status for item in result.constraint_results] == ["fail"]

    def test_q1_q2_both_fail(
        self, mock_client: MagicMock, fake_prompts_dir: Path
    ) -> None:
        # Q1 + Q2 双 HARD active + Q3 skip;两 LLM 调用都返 no
        mock_client.complete.side_effect = [
            _make_response(verdict="no", evidence="Q1 未承接"),
            _make_response(verdict="no", evidence="Q2 切到新空间"),
        ]
        card = _make_card(
            continuity_context=_make_continuity(
                unfinished_actions=["剑势悬而未发"],
                scene_continuation="same",
                time_gap="立即续接",  # Q3 skip
            )
        )
        result = _call(mock_client, fake_prompts_dir, chapter_card=card)

        assert result.passed is False
        # 顺序固定:Q1 → Q2
        assert result.failed_hard_checks == ["unfinished_actions", "scene_same"]
        assert result.soft_warnings == []
        assert result.llm_calls_count == 2

    def test_uncertain_treated_as_hard_fail(
        self, mock_client: MagicMock, fake_prompts_dir: Path
    ) -> None:
        # uncertain 计入 failed_hard(对齐 must_achieve_validator)
        mock_client.complete.return_value = _make_response(
            verdict="uncertain", evidence="正文含糊"
        )
        card = _make_card(
            continuity_context=_make_continuity(
                unfinished_actions=["剑势悬而未发"],
                scene_continuation="switch",
                time_gap="立即续接",
            )
        )
        result = _call(mock_client, fake_prompts_dir, chapter_card=card)

        assert result.passed is False
        assert result.failed_hard_checks == ["unfinished_actions"]


class TestHardBookClock:
    def test_typed_approximate_uncertain_is_soft_without_numeric_hard(
        self, mock_client: MagicMock, fake_prompts_dir: Path
    ) -> None:
        mock_client.complete.return_value = _make_response(
            verdict="uncertain",
            evidence="正文只写稍后，无法证明与约一小时矛盾",
        )

        result = _call(
            mock_client,
            fake_prompts_dir,
            chapter_card=_typed_clock_card(),
        )

        assert result.passed is True
        assert result.failed_hard_checks == []
        assert len(result.soft_warnings) == 1
        assert result.constraint_results[0].status == "pass"
        user_message = mock_client.complete.call_args.kwargs["messages"][1].content
        assert "同夜稍后，约一小时后" in user_message
        assert "近似量不生成分钟上下界" in user_message
        assert "主时间线累计 HARD" not in user_message

    def test_typed_closed_relation_direct_contradiction_is_hard(
        self, mock_client: MagicMock, fake_prompts_dir: Path
    ) -> None:
        mock_client.complete.return_value = _make_response(
            verdict="no",
            evidence="正文明确写成次日清晨，直接违反同夜关系",
        )

        result = _call(
            mock_client,
            fake_prompts_dir,
            chapter_card=_typed_clock_card(),
            chapter_text="直到次日清晨，他们才回到门廊。",
        )

        assert result.passed is False
        assert result.failed_hard_checks == ["time_gap"]
        assert result.soft_warnings == []
        assert result.constraint_results[0].status == "fail"

    @pytest.mark.parametrize(
        ("time_gap", "chapter_text", "evidence"),
        [
            ("三小时后", "整整五小时后，他们才抵达。", "明确时长从三小时改成五小时"),
            ("两到三天后", "五天后，他们才抵达。", "明确超出两到三天闭区间"),
        ],
    )
    def test_typed_explicit_exact_and_bounds_direct_conflicts_are_hard(
        self,
        mock_client: MagicMock,
        fake_prompts_dir: Path,
        time_gap: str,
        chapter_text: str,
        evidence: str,
    ) -> None:
        mock_client.complete.return_value = _make_response(
            verdict="no",
            evidence=evidence,
        )

        result = _call(
            mock_client,
            fake_prompts_dir,
            chapter_card=_typed_clock_card(time_gap),
            chapter_text=chapter_text,
        )

        assert result.passed is False
        assert result.failed_hard_checks == ["time_gap"]
        assert result.constraint_results[0].status == "fail"
        rule = result.constraint_results[0].rule_text
        assert time_gap in rule
        assert "距开篇" not in rule

    def test_sentinel_gap_keeps_legacy_zero_call_boundary(
        self, mock_client: MagicMock, fake_prompts_dir: Path
    ) -> None:
        card = _hard_clock_card().model_copy(
            update={
                "continuity_context": _make_continuity(
                    unfinished_actions=[],
                    scene_continuation="switch",
                    time_gap="立即续接",
                ),
                "book_clock_constraint": BookClockConstraint(
                    time_gap="立即续接",
                    previous_interval=BookClockInterval(
                        minimum_minutes=7 * 24 * 60,
                        maximum_minutes=10 * 24 * 60,
                    ),
                    gap_interval=BookClockInterval(
                        minimum_minutes=0, maximum_minutes=5
                    ),
                    current_interval=BookClockInterval(
                        minimum_minutes=7 * 24 * 60,
                        maximum_minutes=10 * 24 * 60 + 5,
                    ),
                    is_hard=True,
                ),
            }
        )

        result = _call(mock_client, fake_prompts_dir, chapter_card=card)

        assert result.skipped is True
        mock_client.complete.assert_not_called()

    def test_clear_accumulated_clock_is_hard_and_checks_beyond_opening(
        self, mock_client: MagicMock, fake_prompts_dir: Path
    ) -> None:
        mock_client.complete.return_value = _make_response(
            verdict="no",
            evidence="正文宣称主线事件发生在三个月后",
        )
        chapter_text = "开头正常推进。" + "字" * 1500 + "那已是三个月后。"

        result = _call(
            mock_client,
            fake_prompts_dir,
            chapter_card=_hard_clock_card(),
            chapter_text=chapter_text,
        )

        assert result.passed is False
        assert result.failed_hard_checks == ["time_gap"]
        assert result.soft_warnings == []
        assert len(result.constraint_results) == 1
        constraint = result.constraint_results[0]
        assert constraint.status == "fail"
        assert constraint.source_hash == "d" * 64
        assert "11520—15840 分钟" in constraint.rule_text
        user_message = mock_client.complete.call_args.kwargs["messages"][1].content
        assert "最多前 6000 字" in user_message
        assert "那已是三个月后" in user_message
        assert "对白、梦境、回忆、倒叙和插叙不改写主时间线" in user_message

    def test_hard_clock_transport_failure_is_not_silently_passed(
        self, mock_client: MagicMock, fake_prompts_dir: Path
    ) -> None:
        mock_client.complete.side_effect = RuntimeError("offline judge failed")

        with pytest.raises(ContinuityTechnicalError, match="transport_or_parse"):
            _call(
                mock_client,
                fake_prompts_dir,
                chapter_card=_hard_clock_card(),
            )

    def test_unrelated_q_failure_cannot_bypass_required_hard_clock(
        self, mock_client: MagicMock, fake_prompts_dir: Path
    ) -> None:
        """Q1 先坏也必须技术停止，不能提前 fail-open 跳过后面的时钟 HARD。"""

        mock_client.complete.side_effect = RuntimeError("Q1 transport failed")
        card = _hard_clock_card().model_copy(
            update={
                "continuity_context": _make_continuity(
                    unfinished_actions=["继续拆解青铜镜"],
                    scene_continuation="switch",
                    time_gap="一日后",
                )
            }
        )

        with pytest.raises(ContinuityTechnicalError, match="transport_or_parse"):
            _call(mock_client, fake_prompts_dir, chapter_card=card)


# ---------------------------------------------------------------------------
# 3. SOFT fail:Q3 time_gap / Q3 yes / HARD + SOFT 混合
# ---------------------------------------------------------------------------


class TestSoftAndMixed:
    def test_q3_time_gap_fail_does_not_affect_passed(
        self, mock_client: MagicMock, fake_prompts_dir: Path
    ) -> None:
        # Q3 active + LLM no → passed=True(SOFT 不动 passed)+ soft_warnings 非空
        mock_client.complete.return_value = _make_response(
            verdict="no", evidence="正文写'次日清晨'"
        )
        card = _make_card(
            continuity_context=_make_continuity(
                unfinished_actions=[],  # Q1 skip
                scene_continuation="switch",  # Q2 skip
                time_gap="三日后",  # Q3 active(非兜底)
            )
        )
        result = _call(mock_client, fake_prompts_dir, chapter_card=card)

        assert result.passed is True  # SOFT 不影响 passed
        assert result.failed_hard_checks == []
        assert len(result.soft_warnings) == 1
        warning = result.soft_warnings[0]
        assert "time_gap=三日后" in warning
        assert "正文写'次日清晨'" in warning
        assert result.llm_calls_count == 1

    def test_q3_time_gap_yes_emits_no_soft_warning(
        self, mock_client: MagicMock, fake_prompts_dir: Path
    ) -> None:
        # Q3 active + LLM yes → soft_warnings 空
        mock_client.complete.return_value = _make_response(
            verdict="yes", evidence="正文写'三日后',与 time_gap 一致"
        )
        card = _make_card(
            continuity_context=_make_continuity(
                unfinished_actions=[],
                scene_continuation="switch",
                time_gap="三日后",
            )
        )
        result = _call(mock_client, fake_prompts_dir, chapter_card=card)

        assert result.passed is True
        assert result.soft_warnings == []
        assert result.llm_calls_count == 1

    def test_hard_and_soft_mixed_fail(
        self, mock_client: MagicMock, fake_prompts_dir: Path
    ) -> None:
        # Q1 fail(HARD)+ Q3 fail(SOFT)→ passed=False + soft_warnings 非空
        mock_client.complete.side_effect = [
            _make_response(verdict="no", evidence="Q1 未承接"),
            _make_response(verdict="no", evidence="Q3 时间矛盾"),
        ]
        card = _make_card(
            continuity_context=_make_continuity(
                unfinished_actions=["剑势悬而未发"],
                scene_continuation="switch",  # Q2 skip
                time_gap="三日后",
            )
        )
        result = _call(mock_client, fake_prompts_dir, chapter_card=card)

        assert result.passed is False
        assert result.failed_hard_checks == ["unfinished_actions"]
        assert len(result.soft_warnings) == 1
        assert "Q3 时间矛盾" in result.soft_warnings[0]
        assert result.llm_calls_count == 2

    def test_uncertain_counts_as_soft_warning(
        self, mock_client: MagicMock, fake_prompts_dir: Path
    ) -> None:
        # Q3 uncertain → 计入 soft_warnings(对齐 HARD uncertain → failed_hard 模式)
        mock_client.complete.return_value = _make_response(
            verdict="uncertain", evidence="时间描写含糊"
        )
        card = _make_card(
            continuity_context=_make_continuity(
                unfinished_actions=[],
                scene_continuation="switch",
                time_gap="三日后",
            )
        )
        result = _call(mock_client, fake_prompts_dir, chapter_card=card)

        assert result.passed is True
        assert len(result.soft_warnings) == 1
        assert "时间描写含糊" in result.soft_warnings[0]


# ---------------------------------------------------------------------------
# 4. Skip 边界(首章 / 空 prev_chapter_tail / 三 Q 全不满足)
# ---------------------------------------------------------------------------


class TestSkip:
    def test_first_chapter_skipped(
        self, mock_client: MagicMock, fake_prompts_dir: Path
    ) -> None:
        # 全书首章(全局 chapter_id=ch-0001)→ skip 整 validator
        # (v1.1 w4 B1:首章判断改用全局 chapter_id,非卷内 chapter_number==1)
        card = _make_card(chapter_id="ch-0001", chapter_number=1)
        result = _call(mock_client, fake_prompts_dir, chapter_card=card)

        assert result.passed is True
        assert result.skipped is True
        assert result.llm_calls_count == 0
        assert mock_client.complete.call_count == 0

    def test_volume_opening_not_treated_as_first_chapter(
        self, mock_client: MagicMock, fake_prompts_dir: Path
    ) -> None:
        """v1.1 w4 B1 回归:卷开头(卷内 chapter_number=1 但全局 chapter_id≠ch-0001)
        不再被误当全书首章 skip。用空 prev_tail 让它走 skip 边界 2(empty),日志
        reason=empty_prev_chapter_tail(而非 first_chapter)即证 skip 边界 1 未误触。"""
        card = _make_card(
            chapter_id="ch-0007",
            chapter_number=1,
            continuity_context=_make_continuity(prev_chapter_tail=""),
        )
        with capture_logs() as logs:
            _call(
                mock_client,
                fake_prompts_dir,
                chapter_card=card,
                chapter_id="ch-0007",
            )
        skip_events = [
            e for e in logs if e.get("event") == "continuity_validator_skipped"
        ]
        assert len(skip_events) == 1
        assert skip_events[0]["reason"] == "empty_prev_chapter_tail"
        assert mock_client.complete.call_count == 0

    def test_empty_prev_chapter_tail_skipped(
        self, mock_client: MagicMock, fake_prompts_dir: Path
    ) -> None:
        # prev_chapter_tail 空字符串 → skip(continuity_extractor 首章兜底)
        card = _make_card(continuity_context=_make_continuity(prev_chapter_tail=""))
        result = _call(mock_client, fake_prompts_dir, chapter_card=card)

        assert result.passed is True
        assert result.skipped is True
        assert mock_client.complete.call_count == 0

    def test_all_three_qs_inactive_skipped(
        self, mock_client: MagicMock, fake_prompts_dir: Path
    ) -> None:
        # 三 Q 全不满足 → skip(无判定对象)
        card = _make_card(
            continuity_context=_make_continuity(
                unfinished_actions=[],  # Q1 skip
                scene_continuation="switch",  # Q2 skip
                time_gap="立即续接",  # Q3 skip(兜底)
            )
        )
        result = _call(mock_client, fake_prompts_dir, chapter_card=card)

        assert result.passed is True
        assert result.skipped is True
        assert mock_client.complete.call_count == 0

    def test_skip_emits_log_event(
        self, mock_client: MagicMock, fake_prompts_dir: Path
    ) -> None:
        card = _make_card(chapter_id="ch-0001", chapter_number=1)
        with capture_logs() as logs:
            _call(
                mock_client,
                fake_prompts_dir,
                chapter_card=card,
                chapter_id="ch-0001",
            )

        skip_events = [
            e for e in logs if e.get("event") == "continuity_validator_skipped"
        ]
        assert len(skip_events) == 1
        assert skip_events[0]["chapter_id"] == "ch-0001"
        assert skip_events[0]["reason"] == "first_chapter"


# ---------------------------------------------------------------------------
# 5. 单 Q 条件 gating(其他 Q 跳过)
# ---------------------------------------------------------------------------


class TestQConditionGating:
    def test_only_q1_active(
        self, mock_client: MagicMock, fake_prompts_dir: Path
    ) -> None:
        # Q1 active + Q2/Q3 skip → 1 次 LLM 调用,passed=True
        mock_client.complete.return_value = _make_response(verdict="yes")
        card = _make_card(
            continuity_context=_make_continuity(
                unfinished_actions=["剑势悬而未发"],
                scene_continuation="switch",  # Q2 skip
                time_gap="立即续接",  # Q3 skip
            )
        )
        result = _call(mock_client, fake_prompts_dir, chapter_card=card)

        assert result.passed is True
        assert result.llm_calls_count == 1
        assert mock_client.complete.call_count == 1

    def test_only_q2_active(
        self, mock_client: MagicMock, fake_prompts_dir: Path
    ) -> None:
        mock_client.complete.return_value = _make_response(verdict="yes")
        card = _make_card(
            continuity_context=_make_continuity(
                unfinished_actions=[],
                scene_continuation="same",
                time_gap="立即续接",
            )
        )
        result = _call(mock_client, fake_prompts_dir, chapter_card=card)

        assert result.passed is True
        assert result.llm_calls_count == 1

    def test_only_q3_active(
        self, mock_client: MagicMock, fake_prompts_dir: Path
    ) -> None:
        mock_client.complete.return_value = _make_response(verdict="yes")
        card = _make_card(
            continuity_context=_make_continuity(
                unfinished_actions=[],
                scene_continuation="switch",
                time_gap="三日后",
            )
        )
        result = _call(mock_client, fake_prompts_dir, chapter_card=card)

        assert result.passed is True
        assert result.llm_calls_count == 1
        assert result.soft_warnings == []


# ---------------------------------------------------------------------------
# 5b. Q2 梦境 / 醒来例外(v1.1 收官重跑,2026-06-11 luo-accept-v2 ch-0003 实证)
#     双世界 / 梦境题材"从梦中醒来切回现实"是核心叙事装置,scene_same HARD 不应
#     误杀。voice 校验早有 setting / mini plan 明示的梦境/POV 例外，
#     continuity scene_same 缺对称例外 → ch-0003 教科书级梦醒转场被反复
#     override 耗尽 budget 误杀孤儿章。
#     真 LLM 原型已验:真实梦醒转场 no→yes + 无桥接硬跳切仍 no(不放水)。
#     本组为 mock 级防回退:Q2 question 文本 + judge prompt 含梦境例外字面不被回退。
# ---------------------------------------------------------------------------


class TestQ2DreamSceneException:
    def test_q2_question_contains_dream_exception(self) -> None:
        # scene_continuation="same" 激活的 Q2 question 必含梦境/醒来例外字面
        specs = _build_q_specs(_make_continuity(scene_continuation="same"))
        q2 = next(s for s in specs if s.name == "scene_same")
        assert "梦境" in q2.question
        assert "醒来" in q2.question
        # 仍保留"无理由跳切判 no"防放水语义(自门控:只对真梦境桥接放行)
        assert "无理由" in q2.question and "跳切" in q2.question

    def test_judge_prompt_file_contains_dream_exception(self) -> None:
        # judge prompt(continuity_validator_v1.md)含梦境例外指引 + 梦醒示例
        prompts_dir = Path(__file__).resolve().parents[2] / "prompts"
        text = (prompts_dir / "continuity_validator_v1.md").read_text(encoding="utf-8")
        assert "梦境" in text and "醒来" in text
        assert "例 2b" in text  # 梦醒转场示例(承接感官细节后过渡)不被删
        # 反例(无桥接硬跳切仍 no)在示例中显式保留,防"放水"回退
        assert "无桥接" in text or "硬跳切" in text


# ---------------------------------------------------------------------------
# 6. fail-open(LLM 抛 / 非法 JSON / setup 失败)
# ---------------------------------------------------------------------------


class TestFailOpen:
    def test_first_llm_call_raises_returns_fail_open(
        self, mock_client: MagicMock, fake_prompts_dir: Path
    ) -> None:
        # Q1 active + LLM 抛 → 整 validator fail-open / passed=True
        mock_client.complete.side_effect = RuntimeError("Ollama 404 Not Found")
        card = _make_card(
            continuity_context=_make_continuity(
                unfinished_actions=["剑势悬而未发"],
                scene_continuation="same",
                time_gap="三日后",
            )
        )
        result = _call(mock_client, fake_prompts_dir, chapter_card=card)

        assert result.passed is True
        assert result.fail_open is True
        assert result.failed_hard_checks == []
        assert result.soft_warnings == []
        # 第 1 次抛在 complete() 内未计数,后续 Q2/Q3 不再发起
        assert result.llm_calls_count == 0
        assert mock_client.complete.call_count == 1

    def test_invalid_json_returns_fail_open(
        self, mock_client: MagicMock, fake_prompts_dir: Path
    ) -> None:
        mock_client.complete.return_value = LLMResponse(
            content="not valid json {{",
            model_used="qwen-novel:latest",
            usage=TokenUsage(input_tokens=500, output_tokens=30),
            finish_reason="stop",
        )
        card = _make_card(
            continuity_context=_make_continuity(
                unfinished_actions=["剑势悬而未发"],
                scene_continuation="switch",
                time_gap="立即续接",
            )
        )
        result = _call(mock_client, fake_prompts_dir, chapter_card=card)

        assert result.passed is True
        assert result.fail_open is True

    def test_partial_progress_then_fail_returns_fail_open(
        self, mock_client: MagicMock, fake_prompts_dir: Path
    ) -> None:
        # Q1 成功 + Q2 LLM 抛 → 整体 fail-open(放弃部分进度,语义一致性优先)
        mock_client.complete.side_effect = [
            _make_response(verdict="yes"),
            ConnectionError("ollama runner crashed"),
        ]
        card = _make_card(
            continuity_context=_make_continuity(
                unfinished_actions=["剑势悬而未发"],
                scene_continuation="same",
                time_gap="三日后",
            )
        )
        result = _call(mock_client, fake_prompts_dir, chapter_card=card)

        assert result.passed is True
        assert result.fail_open is True
        assert result.llm_calls_count == 1  # Q1 完成 + Q2 抛(未完成)+ Q3 不发起
        assert mock_client.complete.call_count == 2

    def test_setup_fail_open_missing_prompt(
        self, mock_client: MagicMock, tmp_path: Path
    ) -> None:
        # prompts_dir 不含 continuity_validator_v1.md → fail-open + 0 LLM 调用
        empty_dir = tmp_path / "empty_prompts"
        empty_dir.mkdir()
        card = _make_card(
            continuity_context=_make_continuity(
                unfinished_actions=["剑势悬而未发"],
            )
        )
        result = validate_continuity(
            chapter_text="占位正文",
            chapter_card=card,
            chapter_id="ch-0002",
            workspace_id="ws-demo",
            client=mock_client,
            model="qwen-novel:latest",
            prompts_dir=empty_dir,
        )

        assert result.passed is True
        assert result.fail_open is True
        assert result.llm_calls_count == 0
        assert mock_client.complete.call_count == 0

    def test_fail_open_emits_warn_log(
        self, mock_client: MagicMock, fake_prompts_dir: Path
    ) -> None:
        mock_client.complete.side_effect = TimeoutError("ollama deadline")
        card = _make_card(
            continuity_context=_make_continuity(
                unfinished_actions=["剑势悬而未发"],
            )
        )
        with capture_logs() as logs:
            _call(
                mock_client,
                fake_prompts_dir,
                chapter_card=card,
                chapter_id="ch-0099",
            )

        warn_events = [
            e for e in logs if e.get("event") == "continuity_validator_failed_open"
        ]
        assert len(warn_events) == 1
        assert warn_events[0]["chapter_id"] == "ch-0099"
        assert warn_events[0]["error_type"] == "TimeoutError"
        assert warn_events[0]["q_name"] == "unfinished_actions"


# ---------------------------------------------------------------------------
# 7. agent marker 识别(user message 末尾)
# ---------------------------------------------------------------------------


class TestAgentMarker:
    def test_user_message_contains_continuity_validator_marker(
        self, mock_client: MagicMock, fake_prompts_dir: Path
    ) -> None:
        mock_client.complete.return_value = _make_response(verdict="yes")
        card = _make_card(
            continuity_context=_make_continuity(
                unfinished_actions=["剑势悬而未发"],
                scene_continuation="switch",
                time_gap="立即续接",
            )
        )
        _call(mock_client, fake_prompts_dir, chapter_card=card)

        # 抓本次 complete 调用的 messages 参数
        call_args = mock_client.complete.call_args
        messages = call_args.kwargs["messages"]
        user_msg = next(m for m in messages if m.role == "user")
        assert "<!-- agent_role: continuity_validator -->" in user_msg.content
        # 确认 user message 含 3 段必要内容:问题 + 上一章末尾 + 本章开头
        assert "## 问题" in user_msg.content
        assert "## 上一章末尾" in user_msg.content
        assert "## 本章开头" in user_msg.content


# ---------------------------------------------------------------------------
# 8. Q4 chapter_card 自洽性(v1.0.x wave 4 session 4)
# ---------------------------------------------------------------------------


class TestQ4Wave4Session4:
    """v1.0.x wave 4 session 4 — Q4 验本章 continuity_context 与上一章 ChapterCard
    衔接自洽性。SOFT(对齐 Q3 同型):fail → 合并 soft_warnings,不动 verdict。"""

    def test_q4_skipped_when_prev_chapter_card_is_none(
        self, mock_client: MagicMock, fake_prompts_dir: Path
    ) -> None:
        """``prev_chapter_card=None``(默认,既有 caller 全走此路径)→ Q4 condition
        gated skip;Q1/Q2/Q3 condition 全空时整 validator skip。"""
        card = _make_card(
            continuity_context=_make_continuity(
                unfinished_actions=[],
                scene_continuation="switch",
                time_gap="立即续接",
            )
        )
        result = _call(
            mock_client, fake_prompts_dir, chapter_card=card, prev_chapter_card=None
        )
        assert result.skipped is True
        assert result.passed is True
        assert result.llm_calls_count == 0
        # 既有 mock_client 不应被调用一次(全 skip)
        assert mock_client.complete.call_count == 0

    def test_q4_active_yes_no_soft_warning(
        self, mock_client: MagicMock, fake_prompts_dir: Path
    ) -> None:
        """prev_chapter_card 给定 + Q4 active + LLM 返 yes → 无 SOFT warning,
        validator passed。"""
        mock_client.complete.return_value = _make_response(
            verdict="yes",
            evidence="Q4 本章 unfinished_actions 承接 prev next_chapter_seed",
        )
        prev_card = _make_card(
            chapter_id="ch-0001",
            chapter_number=1,
            next_chapter_seed="玉佩纹路真实身份悬念",
            pov_character="lin-yan",
        )
        card = _make_card(
            continuity_context=_make_continuity(
                unfinished_actions=[],
                scene_continuation="switch",
                time_gap="立即续接",
                pov_from="lin-yan",
                pov_to="lin-yan",
            )
        )
        result = _call(
            mock_client,
            fake_prompts_dir,
            chapter_card=card,
            prev_chapter_card=prev_card,
        )
        assert result.passed is True
        assert result.skipped is False
        assert result.failed_hard_checks == []
        assert result.soft_warnings == []
        assert result.llm_calls_count == 1

    def test_q4_question_uses_contradiction_wording_y3(self) -> None:
        """v1.1 wave 6 / Y3 口径回归:Q4 question 必须是『矛盾检测』口径
        (明确『尚未体现 next_chapter_seed 不算矛盾』),防回退到旧『承接验证
        (未体现=no)』高频误报口径(B1 30 章长跑~50% 续跑章误报)。"""
        cc = _make_continuity(
            unfinished_actions=[],
            scene_continuation="switch",
            time_gap="立即续接",
            pov_from="lin-yan",
            pov_to="lin-yan",
        )
        prev_card = _make_card(
            chapter_id="ch-0001",
            chapter_number=1,
            next_chapter_seed="玉佩纹路真实身份悬念",
            pov_character="lin-yan",
        )
        specs = _build_q_specs(cc, prev_chapter_card=prev_card)
        q4 = next(s for s in specs if s.name == "chapter_card_self_consistency")
        # 新口径关键约束在场
        assert "明显矛盾" in q4.question
        assert "不算矛盾" in q4.question
        # 旧『承接验证 / 完全失联=no』口径已移除
        assert "完全失联" not in q4.question

    def test_q4_active_no_emits_soft_warning(
        self, mock_client: MagicMock, fake_prompts_dir: Path
    ) -> None:
        """Q4 active + LLM 返 no → 合并 soft_warnings(SOFT 不动 verdict)。"""
        mock_client.complete.return_value = _make_response(
            verdict="no",
            evidence="本章 unfinished_actions 完全空,未承接 prev next_chapter_seed 的玉佩悬念",
        )
        prev_card = _make_card(
            chapter_id="ch-0001",
            chapter_number=1,
            next_chapter_seed="玉佩纹路真实身份悬念",
            pov_character="lin-yan",
        )
        card = _make_card(
            continuity_context=_make_continuity(
                unfinished_actions=[],
                scene_continuation="switch",
                time_gap="立即续接",
                pov_from="su-wanqing",
                pov_to="su-wanqing",
            )
        )
        result = _call(
            mock_client,
            fake_prompts_dir,
            chapter_card=card,
            prev_chapter_card=prev_card,
        )
        # SOFT fail:passed=True(只看 HARD)+ failed_hard_checks 空 + soft_warnings 1 条
        assert result.passed is True
        assert result.failed_hard_checks == []
        assert len(result.soft_warnings) == 1
        warning = result.soft_warnings[0]
        # 警告字串含 prev_seed / prev_pov 字面 + evidence
        assert "玉佩纹路真实身份悬念" in warning
        assert "lin-yan" in warning
        assert "未承接" in warning  # evidence 片段
        assert result.llm_calls_count == 1

    def test_q4_active_uncertain_emits_soft_warning(
        self, mock_client: MagicMock, fake_prompts_dir: Path
    ) -> None:
        """uncertain 视作 fail(对齐 must_achieve_validator 同型决策)→
        Q4 SOFT 走 soft_warnings,验证档位与 no 等价。"""
        mock_client.complete.return_value = _make_response(
            verdict="uncertain",
            evidence="line不清楚 prev_seed 与 unfinished_actions 关联",
        )
        prev_card = _make_card(
            chapter_id="ch-0001",
            chapter_number=1,
            next_chapter_seed="玉佩纹路真实身份悬念",
            pov_character="lin-yan",
        )
        card = _make_card(
            continuity_context=_make_continuity(
                unfinished_actions=["不相关动作"],
                scene_continuation="switch",
                time_gap="立即续接",
            )
        )
        result = _call(
            mock_client,
            fake_prompts_dir,
            chapter_card=card,
            prev_chapter_card=prev_card,
        )
        # Q1 unfinished_actions 非空也 active → LLM 同样返 uncertain →
        # Q1 HARD uncertain → passed=False + Q4 SOFT uncertain → soft_warnings 1 条
        assert result.passed is False  # Q1 HARD fail 翻盘 passed
        assert "unfinished_actions" in result.failed_hard_checks
        assert len(result.soft_warnings) == 1
        # 验 Q4 warning 字串含 prev_seed 字面(区分 Q4 与其他 SOFT 来源)
        assert "玉佩纹路真实身份悬念" in result.soft_warnings[0]
        assert result.llm_calls_count == 2  # Q1 + Q4

    def test_q4_condition_gated_skip_when_prev_seed_and_pov_empty(
        self, mock_client: MagicMock, fake_prompts_dir: Path
    ) -> None:
        """``prev_chapter_card`` 给定但 next_chapter_seed + pov_character 全为空 →
        Q4 condition gated skip。"""
        # 注意 ChapterCard 字段都 non-null,这里走 model_copy 把 next_chapter_seed
        # 设为字符串空白(strip 后空)+ pov_character 空白
        prev_card = _make_card(
            chapter_id="ch-0001",
            chapter_number=1,
            next_chapter_seed="   ",
            pov_character="   ",
        )
        card = _make_card(
            continuity_context=_make_continuity(
                unfinished_actions=[],
                scene_continuation="switch",
                time_gap="立即续接",
            )
        )
        result = _call(
            mock_client,
            fake_prompts_dir,
            chapter_card=card,
            prev_chapter_card=prev_card,
        )
        # Q1/Q2/Q3/Q4 全 inactive → 整 validator skip
        assert result.skipped is True
        assert result.llm_calls_count == 0
        assert mock_client.complete.call_count == 0

    def test_q4_user_message_contains_prev_card_data(
        self, mock_client: MagicMock, fake_prompts_dir: Path
    ) -> None:
        """Q4 user message 含 prev_next_chapter_seed + prev_pov_character + 本章
        unfinished_actions + pov_from / pov_to 5 字段数据段。"""
        mock_client.complete.return_value = _make_response(verdict="yes")
        prev_card = _make_card(
            chapter_id="ch-0001",
            chapter_number=1,
            next_chapter_seed="玉佩纹路真实身份悬念",
            pov_character="lin-yan",
        )
        card = _make_card(
            continuity_context=_make_continuity(
                unfinished_actions=["玉佩纹路真实身份未揭"],
                scene_continuation="switch",
                time_gap="立即续接",
                pov_from="lin-yan",
                pov_to="su-wanqing",
            )
        )
        _call(
            mock_client,
            fake_prompts_dir,
            chapter_card=card,
            prev_chapter_card=prev_card,
        )
        # mock_client 总共被调一次(只 Q4 active);Q1 因 unfinished_actions 非空
        # 也 active → 2 次调用;取最后一次(Q4)的 user_msg
        all_calls = mock_client.complete.call_args_list
        # 至少调用 1 次 — 找到 Q4 那次(数据段含 prev_next_chapter_seed 字面)
        q4_call_found = False
        for call in all_calls:
            user_msg = next(m for m in call.kwargs["messages"] if m.role == "user")
            if "prev_next_chapter_seed" in user_msg.content:
                q4_call_found = True
                assert "玉佩纹路真实身份悬念" in user_msg.content
                assert "lin-yan" in user_msg.content
                assert "玉佩纹路真实身份未揭" in user_msg.content
                assert "su-wanqing" in user_msg.content
                break
        assert q4_call_found, "Q4 user message 未含 prev_next_chapter_seed 字段"


# ---------------------------------------------------------------------------
# 9. Q5 角色到场连续性(v1.1 wave 6 session 3,B3)
# ---------------------------------------------------------------------------


class TestQ5CharacterPresence:
    """v1.1 wave 6 session 3 — Q5 验本章新出场角色是否有合理到场交代(治 B1
    bug B5 陆星野突兀出现)。SOFT(对齐 Q3/Q4 + wave 6 全 WARN 默认):fail →
    合并 soft_warnings,不动 verdict。代码标记(roster diff gating)+ LLM 复核。"""

    @staticmethod
    def _q5_only_cc() -> ContinuityContext:
        """构造让 Q1/Q2/Q3 全 inactive 的 continuity_context(只留 Q5 可激活)。"""
        return _make_continuity(
            unfinished_actions=[],
            scene_continuation="switch",
            time_gap="立即续接",
        )

    @staticmethod
    def _prev_card_with_roster(roster: list[str]) -> ChapterCard:
        """上一章卡:给定 roster + 空 next_chapter_seed/pov(让 Q4 skip)。"""
        return _make_card(
            chapter_id="ch-0001",
            chapter_number=1,
            next_chapter_seed="   ",
            pov_character="   ",
            related_characters=roster,
        )

    def test_q5_skipped_when_no_newly_present(
        self, mock_client: MagicMock, fake_prompts_dir: Path
    ) -> None:
        """本章 roster ⊆ 上一章 roster(无新出场角色)→ Q5 gated skip;其余 Q 全
        inactive → 整 validator skip。"""
        prev_card = self._prev_card_with_roster(["char-a", "char-b"])
        card = _make_card(
            continuity_context=self._q5_only_cc(),
            related_characters=["char-a"],  # ⊆ prev roster
        )
        result = _call(
            mock_client,
            fake_prompts_dir,
            chapter_card=card,
            prev_chapter_card=prev_card,
        )
        assert result.skipped is True
        assert result.llm_calls_count == 0
        assert mock_client.complete.call_count == 0

    def test_q5_skipped_when_prev_roster_empty(
        self, mock_client: MagicMock, fake_prompts_dir: Path
    ) -> None:
        """上一章 roster 为空(无对照基线)→ Q5 gated skip,即便本章有出场角色。"""
        prev_card = self._prev_card_with_roster([])
        card = _make_card(
            continuity_context=self._q5_only_cc(),
            related_characters=["char-c"],
        )
        result = _call(
            mock_client,
            fake_prompts_dir,
            chapter_card=card,
            prev_chapter_card=prev_card,
        )
        assert result.skipped is True
        assert result.llm_calls_count == 0

    def test_q5_active_yes_no_soft_warning(
        self, mock_client: MagicMock, fake_prompts_dir: Path
    ) -> None:
        """Q5 active(char-c 上一章不在场)+ LLM yes → 无 SOFT warning,passed。"""
        mock_client.complete.return_value = _make_response(
            verdict="yes", evidence="char-c 推门而入有明确到场交代"
        )
        prev_card = self._prev_card_with_roster(["char-a", "char-b"])
        card = _make_card(
            continuity_context=self._q5_only_cc(),
            related_characters=["char-a", "char-c"],  # char-c 新出场
        )
        result = _call(
            mock_client,
            fake_prompts_dir,
            chapter_card=card,
            prev_chapter_card=prev_card,
        )
        assert result.passed is True
        assert result.skipped is False
        assert result.failed_hard_checks == []
        assert result.soft_warnings == []
        assert result.llm_calls_count == 1

    def test_q5_active_no_emits_soft_warning(
        self, mock_client: MagicMock, fake_prompts_dir: Path
    ) -> None:
        """Q5 active + LLM no → 合并 soft_warnings(SOFT 不动 verdict,passed=True)。"""
        mock_client.complete.return_value = _make_response(
            verdict="no",
            evidence="上一章仅 char-a 独自,本章 char-c 直接在场无交代",
        )
        prev_card = self._prev_card_with_roster(["char-a"])
        card = _make_card(
            continuity_context=self._q5_only_cc(),
            related_characters=["char-a", "char-c"],
        )
        result = _call(
            mock_client,
            fake_prompts_dir,
            chapter_card=card,
            prev_chapter_card=prev_card,
        )
        assert result.passed is True  # SOFT 不影响 passed
        assert result.failed_hard_checks == []
        assert len(result.soft_warnings) == 1
        warning = result.soft_warnings[0]
        assert "角色到场连续性" in warning
        assert "char-c" in warning  # 新出场角色字面
        assert "无交代" in warning  # evidence 片段
        assert result.llm_calls_count == 1

    def test_q5_active_uncertain_emits_soft_warning(
        self, mock_client: MagicMock, fake_prompts_dir: Path
    ) -> None:
        """uncertain 视作 fail(对齐既定决策)→ Q5 SOFT 走 soft_warnings。"""
        mock_client.complete.return_value = _make_response(
            verdict="uncertain", evidence="不确定 char-c 是否首次登场"
        )
        prev_card = self._prev_card_with_roster(["char-a"])
        card = _make_card(
            continuity_context=self._q5_only_cc(),
            related_characters=["char-c"],
        )
        result = _call(
            mock_client,
            fake_prompts_dir,
            chapter_card=card,
            prev_chapter_card=prev_card,
        )
        assert result.passed is True
        assert len(result.soft_warnings) == 1
        assert "角色到场连续性" in result.soft_warnings[0]
        assert result.llm_calls_count == 1

    def test_q5_soft_does_not_flip_passed_with_hard(
        self, mock_client: MagicMock, fake_prompts_dir: Path
    ) -> None:
        """Q1 HARD fail + Q5 SOFT fail 同时 → passed=False(只看 HARD)+ soft_warnings
        非空(Q5)。验 Q5 不进 failed_hard_checks。"""
        mock_client.complete.return_value = _make_response(
            verdict="no", evidence="均未承接 / 无到场交代"
        )
        prev_card = self._prev_card_with_roster(["char-a"])
        card = _make_card(
            continuity_context=_make_continuity(
                unfinished_actions=["剑势悬而未发"],  # Q1 active
                scene_continuation="switch",
                time_gap="立即续接",
            ),
            related_characters=["char-c"],  # Q5 active
        )
        result = _call(
            mock_client,
            fake_prompts_dir,
            chapter_card=card,
            prev_chapter_card=prev_card,
        )
        assert result.passed is False  # Q1 HARD fail
        assert result.failed_hard_checks == ["unfinished_actions"]
        assert "character_presence" not in result.failed_hard_checks
        assert len(result.soft_warnings) == 1  # Q5 SOFT
        assert "角色到场连续性" in result.soft_warnings[0]
        assert result.llm_calls_count == 2  # Q1 + Q5

    def test_q5_build_q_specs_gating_and_protect_blank(self) -> None:
        """直接对 _build_q_specs 断言 Q5 spec 的 gating + 护留白措辞(防回退)。"""
        cc = self._q5_only_cc()
        prev_card = self._prev_card_with_roster(["char-a", "char-b"])
        specs = _build_q_specs(
            cc,
            prev_chapter_card=prev_card,
            current_related_characters=["char-a", "char-c"],
        )
        q5 = next((s for s in specs if s.name == "character_presence"), None)
        assert q5 is not None, "Q5 应在 char-c 新出场时激活"
        assert q5.q_type == "soft"
        # 护留白关键措辞在场(防回退到"硬抓所有新角色")
        assert "护留白" in q5.question
        assert "拿不准时判 yes" in q5.question
        assert "char-c" in q5.question  # 新出场角色入问
        # 无 current_related_characters → Q5 不激活
        specs_none = _build_q_specs(cc, prev_chapter_card=prev_card)
        assert all(s.name != "character_presence" for s in specs_none)

    def test_q5_user_message_contains_roster_data(
        self, mock_client: MagicMock, fake_prompts_dir: Path
    ) -> None:
        """Q5 user message 含 newly_present 角色 + 上一章 roster 数据段。"""
        mock_client.complete.return_value = _make_response(verdict="yes")
        prev_card = self._prev_card_with_roster(["char-a", "char-b"])
        card = _make_card(
            continuity_context=self._q5_only_cc(),
            related_characters=["char-c"],
        )
        _call(
            mock_client,
            fake_prompts_dir,
            chapter_card=card,
            prev_chapter_card=prev_card,
        )
        user_msg = next(
            m
            for m in mock_client.complete.call_args.kwargs["messages"]
            if m.role == "user"
        )
        assert "char-c" in user_msg.content
        assert "char-a" in user_msg.content  # roster 字面
        assert "上一章不在场" in user_msg.content


# ---------------------------------------------------------------------------
# 18. Q1-Q5 并发判定(v2 间隙 A1,2026-06-13):executor 给定时各 Q 并发,
#     verdict 语义(failed_hard_checks / soft_warnings 原序 + passed)与串行
#     逐字段一致;完成顺序乱不影响收集顺序。
# ---------------------------------------------------------------------------


class TestQParallelism:
    """A1:continuity 内部 Q 并发与串行等价(content-aware fake 制造完成顺序乱)。"""

    @staticmethod
    def _content_aware_fake(slow_q1: bool = False) -> Any:
        """按 user message 的 Q 特征词返回对应 verdict(并发调用顺序无关)。

        Q1(含"未完成动作")返 no;Q2(含"物理空间")返 no;其余 yes。
        ``slow_q1`` → Q1 sleep 让 Q2 先完成,验证收集仍按 q_specs 原序。
        """
        import time

        def fake(*, messages: Any, **kwargs: Any) -> LLMResponse:
            user_text = "\n".join(m.content for m in messages if m.role == "user")
            if "未完成动作" in user_text:  # Q1
                if slow_q1:
                    time.sleep(0.1)
                return _make_response(verdict="no", evidence="Q1 未承接")
            if "物理空间" in user_text:  # Q2
                return _make_response(verdict="no", evidence="Q2 切到新空间")
            return _make_response(verdict="yes")

        return fake

    def _call_with_executor(
        self,
        fake_prompts_dir: Path,
        *,
        slow_q1: bool,
        executor: Any,
    ) -> ContinuityValidationResult:
        client = MagicMock(spec=LLMClient)
        client.complete.side_effect = self._content_aware_fake(slow_q1=slow_q1)
        # Q1 + Q2 双 HARD active(unfinished_actions 非空 + scene_continuation=same）
        card = _make_card(
            continuity_context=_make_continuity(
                unfinished_actions=["剑势悬而未发"],
                scene_continuation="same",
                time_gap="立即续接",  # Q3 skip
            )
        )
        return validate_continuity(
            chapter_text="占位章节正文。" + "字" * 500,
            chapter_card=card,
            chapter_id="ch-0002",
            workspace_id="ws-demo",
            client=client,
            model="qwen-novel:latest",
            prompts_dir=fake_prompts_dir,
            executor=executor,
        )

    def test_parallel_equals_serial_field_by_field(
        self, fake_prompts_dir: Path
    ) -> None:
        """串行(executor=None)与并发(ThreadPoolExecutor)逐字段相等。"""
        from concurrent.futures import ThreadPoolExecutor

        serial = self._call_with_executor(
            fake_prompts_dir, slow_q1=False, executor=None
        )
        with ThreadPoolExecutor(max_workers=4) as ex:
            parallel = self._call_with_executor(
                fake_prompts_dir, slow_q1=False, executor=ex
            )

        assert serial.passed is False and parallel.passed is False
        assert serial.failed_hard_checks == parallel.failed_hard_checks
        assert serial.soft_warnings == parallel.soft_warnings
        assert serial.llm_calls_count == parallel.llm_calls_count == 2

    def test_collection_order_is_q_spec_order_not_completion(
        self, fake_prompts_dir: Path
    ) -> None:
        """Q1 慢、Q2 快(Q2 先完成)→ failed_hard 仍按 q_specs 原序 [Q1, Q2]。"""
        from concurrent.futures import ThreadPoolExecutor

        with ThreadPoolExecutor(max_workers=4) as ex:
            result = self._call_with_executor(
                fake_prompts_dir, slow_q1=True, executor=ex
            )
        # 即使 Q2 先算完,收集顺序仍是 q_specs 原序(unfinished_actions 在前)
        assert result.failed_hard_checks == ["unfinished_actions", "scene_same"]

    def test_parallel_fail_open_on_q_exception(self, fake_prompts_dir: Path) -> None:
        """并发模式下任一 Q 抛异常 → 整体 fail-open(决策 4 保持)。"""
        from concurrent.futures import ThreadPoolExecutor

        client = MagicMock(spec=LLMClient)

        def boom(*, messages: Any, **kwargs: Any) -> LLMResponse:
            user_text = "\n".join(m.content for m in messages if m.role == "user")
            if "未完成动作" in user_text:
                raise RuntimeError("Q1 LLM 瞬态错误")
            return _make_response(verdict="yes")

        client.complete.side_effect = boom
        card = _make_card(
            continuity_context=_make_continuity(
                unfinished_actions=["剑势悬而未发"],
                scene_continuation="same",
                time_gap="立即续接",
            )
        )
        with ThreadPoolExecutor(max_workers=4) as ex:
            result = validate_continuity(
                chapter_text="占位正文。" + "字" * 500,
                chapter_card=card,
                chapter_id="ch-0002",
                workspace_id="ws-demo",
                client=client,
                model="qwen-novel:latest",
                prompts_dir=fake_prompts_dir,
                executor=ex,
            )
        assert result.fail_open is True
        assert result.passed is True
