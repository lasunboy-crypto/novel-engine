"""beat_alignment_validator agent 单测(v1.1 wave 1 session 1)。

6 类覆盖(对齐 must_achieve_validator 同型测试范围 + 本 agent 独有 forbidden 路径):

1. **happy**:chapter_event 推进 must + 未触发 forbidden → passed=True
2. **forbidden violated**:chapter_event 触发 forbidden(luo-cloud-test 现场重演)
   → passed=False / forbidden_advance_violated 非空
3. **partial must**:chapter_event 只推进部分 must(过渡章场景)+ verdict=pass
4. **empty beat**:beat.must_advance + forbidden_advance 都空 → 跳过 + skipped=True
5. **LLM fail**:LLM 抛异常 → fail-open(passed=True + fail_open=True)
6. **retry feedback 格式化**:``format_validation_failure_for_retry`` 输出含 forbidden +
   missing must + summary 跑题 三类失败明细字面

完全 mock ``LLMClient``;用临时目录注入 fake ``beat_alignment_validator_v1.md`` 模板。
"""

from __future__ import annotations

import json
from pathlib import Path
from unittest.mock import MagicMock

import pytest
import structlog

from src.core.agents import _prompt as prompt_module
from src.core.agents.beat_alignment_validator import (
    BeatAlignmentValidationResult,
    format_validation_failure_for_retry,
    validate_beat_alignment,
)
from src.core.llm.base import LLMClient, LLMResponse, TokenUsage
from src.core.models.outline import VolumeBeatPlan


# ---------------------------------------------------------------------------
# 工厂 & 公共 fixture
# ---------------------------------------------------------------------------


def _make_response(
    *,
    verdict: str = "pass",
    must_advance_advanced: list[str] | None = None,
    must_advance_missing: list[str] | None = None,
    forbidden_advance_violated: list[str] | None = None,
    summary_in_range: bool = True,
    evidence: str = "本章 event 字面符合 beat 范围",
    info_increment_substantive: bool | None = None,
    info_increment_issue: str | None = None,
    model_used: str = "qwen-novel:latest",
) -> LLMResponse:
    payload: dict = {
        "verdict": verdict,
        "must_advance_advanced": must_advance_advanced or [],
        "must_advance_missing": must_advance_missing or [],
        "forbidden_advance_violated": forbidden_advance_violated or [],
        "summary_in_range": summary_in_range,
        "evidence": evidence,
    }
    # A2:仅在显式传入时写入(测"旧 LLM 输出无该字段 → schema 默认 True" 路径)
    if info_increment_substantive is not None:
        payload["info_increment_substantive"] = info_increment_substantive
    if info_increment_issue is not None:
        payload["info_increment_issue"] = info_increment_issue
    return LLMResponse(
        content=json.dumps(payload, ensure_ascii=False),
        model_used=model_used,
        usage=TokenUsage(input_tokens=600, output_tokens=80),
        finish_reason="stop",
    )


def _make_beat(
    *,
    beat_id: str = "v1-arc-1-beat-1",
    chapter_range: tuple[int, int] = (1, 3),
    summary: str = "arc-1 开局 — 林衍在矿洞苏醒 + 首次触碰锈剑",
    must_advance: list[str] | None = None,
    forbidden_advance: list[str] | None = None,
) -> VolumeBeatPlan:
    # 关键:用 ``is not None`` 而非 ``or``,允许显式传入 ``[]`` 测"empty" 路径
    # (``[] or default`` 会被 default 覆盖,违背测试意图)。
    return VolumeBeatPlan(
        beat_id=beat_id,
        chapter_range=chapter_range,
        summary=summary,
        must_advance=(
            must_advance
            if must_advance is not None
            else ["林衍首次触碰锈剑", "剑灵首次共鸣"]
        ),
        forbidden_advance=(
            forbidden_advance
            if forbidden_advance is not None
            else ["林衍走出矿洞", "薛无现身"]
        ),
    )


@pytest.fixture
def fake_prompts_dir(tmp_path: Path) -> Path:
    prompts_dir = tmp_path / "prompts"
    prompts_dir.mkdir()
    (prompts_dir / "beat_alignment_validator_v1.md").write_text(
        "FAKE_BEAT_ALIGNMENT_VALIDATOR_PROMPT\n\n用于单测断言的 system 标记。",
        encoding="utf-8",
    )
    return prompts_dir


@pytest.fixture(autouse=True)
def _clear_prompt_cache() -> None:
    prompt_module.load_prompt_template.cache_clear()


@pytest.fixture(autouse=True)
def _ensure_debug_level_for_capture_logs() -> None:
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
    beat: VolumeBeatPlan,
    chapter_event: str = "林衍在矿洞首次触碰锈剑,剑灵微弱共鸣救他一命。",
    chapter_id: str = "ch-0001",
    workspace_id: str = "ws-demo",
    model: str = "qwen-novel:latest",
    info_increment: str = "",
) -> BeatAlignmentValidationResult:
    return validate_beat_alignment(
        chapter_event=chapter_event,
        beat=beat,
        chapter_id=chapter_id,
        info_increment=info_increment,
        workspace_id=workspace_id,
        client=mock_client,
        model=model,
        prompts_dir=fake_prompts_dir,
    )


# ---------------------------------------------------------------------------
# 1. Happy:pass + 推进 must + 未触发 forbidden
# ---------------------------------------------------------------------------


class TestHappy:
    def test_pass_with_must_advanced(
        self, mock_client: MagicMock, fake_prompts_dir: Path
    ) -> None:
        mock_client.complete.return_value = _make_response(
            verdict="pass",
            must_advance_advanced=["林衍首次触碰锈剑", "剑灵首次共鸣"],
            must_advance_missing=[],
            forbidden_advance_violated=[],
            summary_in_range=True,
            evidence="本章 event 字面体现林衍在矿洞触碰锈剑 + 剑灵共鸣",
        )
        beat = _make_beat()
        result = _call(mock_client, fake_prompts_dir, beat=beat)

        assert result.passed is True
        assert result.must_advance_advanced == ["林衍首次触碰锈剑", "剑灵首次共鸣"]
        assert result.must_advance_missing == []
        assert result.forbidden_advance_violated == []
        assert result.summary_in_range is True
        assert result.llm_calls_count == 1
        assert result.skipped is False
        assert result.fail_open is False
        assert mock_client.complete.call_count == 1


# ---------------------------------------------------------------------------
# 2. Forbidden violated:luo-cloud-test 现场重演
# ---------------------------------------------------------------------------


class TestForbiddenViolated:
    def test_chapter_event_triggers_forbidden(
        self, mock_client: MagicMock, fake_prompts_dir: Path
    ) -> None:
        """luo-cloud-test 2026-05-25 真实战:ch-10 触发 arc-3 核心剧情字面。"""
        mock_client.complete.return_value = _make_response(
            verdict="fail",
            must_advance_advanced=[],
            must_advance_missing=["获得第一件残片", "引导者退场"],
            forbidden_advance_violated=["携带残片返回地球", "确立双时空穿梭"],
            summary_in_range=False,
            evidence="chapter_event 字面命中 2 条 forbidden,arc-3 核心剧情被提前 11 章剧透",
        )
        beat = _make_beat(
            beat_id="v1-arc-1-beat-3",
            chapter_range=(8, 10),
            summary="arc-1 收束 — 主角获得第一件残片 + 引导者退场",
            must_advance=["获得第一件残片", "引导者退场"],
            forbidden_advance=[
                "携带残片返回地球",
                "确立双时空穿梭",
                "面见嬴政",
                "本卷终极反派李斯现身",
            ],
        )
        result = _call(
            mock_client,
            fake_prompts_dir,
            beat=beat,
            chapter_event=(
                "林衍携带残片返回地球天文台,首次接触现代科学仪器,"
                "确立双时空穿梭的存在。"
            ),
            chapter_id="ch-0010",
        )

        assert result.passed is False
        assert result.forbidden_advance_violated == [
            "携带残片返回地球",
            "确立双时空穿梭",
        ]
        assert result.must_advance_missing == ["获得第一件残片", "引导者退场"]
        assert result.summary_in_range is False
        assert result.llm_calls_count == 1
        assert result.fail_open is False


# ---------------------------------------------------------------------------
# 3. Partial must:过渡章场景
# ---------------------------------------------------------------------------


class TestPartialMust:
    def test_pass_with_partial_must_advanced(
        self, mock_client: MagicMock, fake_prompts_dir: Path
    ) -> None:
        """过渡章只推进 1 条 must,其余留后续章 → verdict 仍 pass。"""
        mock_client.complete.return_value = _make_response(
            verdict="pass",
            must_advance_advanced=["首次主动挥剑"],
            must_advance_missing=["林衍走出矿洞", "与剑灵建立深度共鸣"],
            forbidden_advance_violated=[],
            summary_in_range=True,
            evidence="本章 event 落在 beat 中段挥剑推进,'走出矿洞'已隐含",
        )
        beat = _make_beat(
            beat_id="v1-arc-1-beat-2",
            chapter_range=(4, 7),
            summary="arc-1 中段 — 主角离开矿洞 + 学会与锈剑共鸣",
            must_advance=[
                "林衍走出矿洞",
                "首次主动挥剑",
                "与剑灵建立深度共鸣",
            ],
            forbidden_advance=["薛无现身救人", "返回地球"],
        )
        result = _call(
            mock_client,
            fake_prompts_dir,
            beat=beat,
            chapter_event=(
                "林衍在荒原行走中遇到第一波低阶妖兽袭击,被迫凭本能挥剑击退。"
            ),
            chapter_id="ch-0005",
        )

        assert result.passed is True
        assert result.must_advance_advanced == ["首次主动挥剑"]
        assert result.must_advance_missing == [
            "林衍走出矿洞",
            "与剑灵建立深度共鸣",
        ]
        assert result.forbidden_advance_violated == []


# ---------------------------------------------------------------------------
# 4. Empty beat:must + forbidden 都空 → 跳过
# ---------------------------------------------------------------------------


class TestEmptyBeat:
    def test_skip_when_both_must_and_forbidden_empty(
        self, mock_client: MagicMock, fake_prompts_dir: Path
    ) -> None:
        beat = _make_beat(must_advance=[], forbidden_advance=[])
        result = _call(mock_client, fake_prompts_dir, beat=beat)

        assert result.passed is True
        assert result.skipped is True
        assert result.llm_calls_count == 0
        assert result.fail_open is False
        # 关键:LLM 调用从未发起
        assert mock_client.complete.call_count == 0

    def test_no_skip_when_only_must_present(
        self, mock_client: MagicMock, fake_prompts_dir: Path
    ) -> None:
        """只有 must_advance 非空也要走 LLM(只跳过两者都空)。"""
        beat = _make_beat(
            must_advance=["林衍首次触碰锈剑"], forbidden_advance=[]
        )
        result = _call(mock_client, fake_prompts_dir, beat=beat)

        assert result.passed is True  # mock 默认 pass
        assert result.skipped is False
        assert result.llm_calls_count == 1
        assert mock_client.complete.call_count == 1

    def test_no_skip_when_only_forbidden_present(
        self, mock_client: MagicMock, fake_prompts_dir: Path
    ) -> None:
        """只有 forbidden_advance 非空也要走 LLM(只跳过两者都空)。"""
        beat = _make_beat(must_advance=[], forbidden_advance=["返回地球"])
        result = _call(mock_client, fake_prompts_dir, beat=beat)

        assert result.skipped is False
        assert result.llm_calls_count == 1
        assert mock_client.complete.call_count == 1


# ---------------------------------------------------------------------------
# 5. LLM fail:抛异常 → fail-open
# ---------------------------------------------------------------------------


class TestLLMFail:
    def test_llm_exception_fail_open(
        self, mock_client: MagicMock, fake_prompts_dir: Path
    ) -> None:
        mock_client.complete.side_effect = RuntimeError("Qwen 404 transient")
        beat = _make_beat()
        result = _call(mock_client, fake_prompts_dir, beat=beat)

        assert result.passed is True  # fail-open
        assert result.fail_open is True
        assert result.llm_calls_count == 1
        assert result.skipped is False
        assert result.forbidden_advance_violated == []
        assert result.must_advance_advanced == []

    def test_llm_invalid_json_fail_open(
        self, mock_client: MagicMock, fake_prompts_dir: Path
    ) -> None:
        """LLM 输出非合法 JSON 也走 fail-open(parser 抛 ValueError)。"""
        mock_client.complete.return_value = LLMResponse(
            content="not a json output",
            model_used="qwen-novel:latest",
            usage=TokenUsage(input_tokens=600, output_tokens=10),
            finish_reason="stop",
        )
        beat = _make_beat()
        result = _call(mock_client, fake_prompts_dir, beat=beat)

        assert result.passed is True
        assert result.fail_open is True
        assert result.llm_calls_count == 1

    def test_llm_invalid_schema_fail_open(
        self, mock_client: MagicMock, fake_prompts_dir: Path
    ) -> None:
        """LLM 输出 JSON 但缺字段 → fail-open。"""
        mock_client.complete.return_value = LLMResponse(
            content='{"verdict": "pass"}',  # 缺 evidence / must / forbidden
            model_used="qwen-novel:latest",
            usage=TokenUsage(input_tokens=600, output_tokens=10),
            finish_reason="stop",
        )
        beat = _make_beat()
        result = _call(mock_client, fake_prompts_dir, beat=beat)

        assert result.passed is True
        assert result.fail_open is True

    def test_prompt_file_missing_setup_fail_open(
        self, mock_client: MagicMock, tmp_path: Path
    ) -> None:
        """prompt 文件不存在 setup 阶段 fail-open(对齐 must_achieve_validator)。"""
        empty_prompts_dir = tmp_path / "empty_prompts"
        empty_prompts_dir.mkdir()
        beat = _make_beat()
        result = _call(mock_client, empty_prompts_dir, beat=beat)

        assert result.passed is True
        assert result.fail_open is True
        assert result.llm_calls_count == 0  # setup 阶段失败,LLM 未调
        assert mock_client.complete.call_count == 0


# ---------------------------------------------------------------------------
# 6. retry feedback 格式化
# ---------------------------------------------------------------------------


class TestRetryFeedback:
    def test_format_includes_forbidden_and_missing(self) -> None:
        result = BeatAlignmentValidationResult(
            passed=False,
            must_advance_advanced=[],
            must_advance_missing=["获得第一件残片"],
            forbidden_advance_violated=["携带残片返回地球"],
            summary_in_range=False,
            evidence="ch-10 字面触发 arc-3 核心剧情",
            llm_calls_count=1,
        )
        msg = format_validation_failure_for_retry(
            result=result, chapter_id="ch-0010", beat_id="v1-arc-1-beat-3"
        )

        # 关键 — error msg 必含 forbidden 字面(LLM retry 时直接读)
        assert "携带残片返回地球" in msg
        assert "获得第一件残片" in msg
        assert "ch-0010" in msg
        assert "v1-arc-1-beat-3" in msg
        assert "forbidden_advance" in msg
        assert "must_advance" in msg
        assert "summary" in msg  # summary 跑题 marker
        assert "ch-10 字面触发 arc-3 核心剧情" in msg

    def test_format_skip_missing_when_some_advanced(self) -> None:
        """has must_advanced ≥ 1 时不报"未推进任何 must"(过渡章正常)。"""
        result = BeatAlignmentValidationResult(
            passed=False,
            must_advance_advanced=["首次挥剑"],
            must_advance_missing=["走出矿洞"],  # 只是过渡章,不报
            forbidden_advance_violated=["返回地球"],
            summary_in_range=True,
            evidence="ch 触发 forbidden",
            llm_calls_count=1,
        )
        msg = format_validation_failure_for_retry(
            result=result, chapter_id="ch-0005", beat_id="b2"
        )

        # forbidden 必报
        assert "返回地球" in msg
        # must_advance_missing 不应该出现"未推进任何 must"明文(因为已推进 1 条)
        assert "未推进任何 must_advance" not in msg
        # summary in range 时不应有 summary 跑题报告
        assert "跑出 beat.summary" not in msg


# ---------------------------------------------------------------------------
# 7. info_increment 维度(A2,wave 6 session 2)— WARN-only,独立于 verdict
# ---------------------------------------------------------------------------


class TestInfoIncrementDimension:
    def test_substantive_true_no_warn(
        self, mock_client: MagicMock, fake_prompts_dir: Path
    ) -> None:
        """info_increment 实质 → substantive=True + issue 空 + passed 不受影响。"""
        mock_client.complete.return_value = _make_response(
            verdict="pass",
            info_increment_substantive=True,
            info_increment_issue="",
        )
        result = _call(
            mock_client,
            fake_prompts_dir,
            beat=_make_beat(),
            info_increment="新揭示:主角有能与剑灵共鸣的特殊体质。",
        )
        assert result.passed is True
        assert result.info_increment_substantive is True
        assert result.info_increment_issue == ""

    def test_weak_increment_warns_but_verdict_pass(
        self, mock_client: MagicMock, fake_prompts_dir: Path
    ) -> None:
        """⚠️核心:verdict=pass 但增量薄弱 → passed 仍 True,只标 substantive=False。"""
        mock_client.complete.return_value = _make_response(
            verdict="pass",
            info_increment_substantive=False,
            info_increment_issue="增量是'战斗升级'空泛占位,为上一场同套路重复",
        )
        result = _call(
            mock_client,
            fake_prompts_dir,
            beat=_make_beat(),
            info_increment="战斗升级,主角更强了。",
        )
        # verdict 维度仍 pass — info_increment 不影响 passed
        assert result.passed is True
        # info_increment 维度独立标记 false
        assert result.info_increment_substantive is False
        assert "空泛占位" in result.info_increment_issue

    def test_fail_verdict_with_substantive_true_independent(
        self, mock_client: MagicMock, fake_prompts_dir: Path
    ) -> None:
        """⚠️反向:verdict=fail(forbidden 触发)但增量实质 → 两维度各自独立。"""
        mock_client.complete.return_value = _make_response(
            verdict="fail",
            forbidden_advance_violated=["返回地球"],
            summary_in_range=False,
            info_increment_substantive=True,
            info_increment_issue="",
        )
        result = _call(
            mock_client,
            fake_prompts_dir,
            beat=_make_beat(forbidden_advance=["返回地球"]),
            info_increment="新揭示:跨界穿行机制确立。",
        )
        assert result.passed is False  # forbidden 触发
        assert result.info_increment_substantive is True  # 但增量实质

    def test_empty_info_increment_forces_substantive_true(
        self, mock_client: MagicMock, fake_prompts_dir: Path
    ) -> None:
        """info_increment 为空 → 强制 substantive=True(不判不报),即便 LLM 返 false。"""
        mock_client.complete.return_value = _make_response(
            verdict="pass",
            info_increment_substantive=False,  # LLM 即便误返 false
            info_increment_issue="不该出现",
        )
        result = _call(
            mock_client,
            fake_prompts_dir,
            beat=_make_beat(),
            info_increment="",  # 空 → 跳过 info_increment 维度
        )
        assert result.passed is True
        assert result.info_increment_substantive is True
        assert result.info_increment_issue == ""

    def test_old_llm_output_without_field_defaults_true(
        self, mock_client: MagicMock, fake_prompts_dir: Path
    ) -> None:
        """旧 LLM 输出无 info_increment 字段 → schema 默认 True(fail-safe 不误报)。"""
        # _make_response 不传 info_increment_* → payload 不含该字段
        mock_client.complete.return_value = _make_response(verdict="pass")
        result = _call(
            mock_client,
            fake_prompts_dir,
            beat=_make_beat(),
            info_increment="新冲突:新对手登场。",
        )
        assert result.passed is True
        assert result.info_increment_substantive is True

    def test_user_message_includes_info_increment(
        self, mock_client: MagicMock, fake_prompts_dir: Path
    ) -> None:
        """info_increment 文本应进 user message(供 judge 判定)。"""
        marker = "新揭示:锈剑前主人是陨落剑修。"
        _call(
            mock_client,
            fake_prompts_dir,
            beat=_make_beat(),
            info_increment=marker,
        )
        # 取出实际发给 LLM 的 user message
        call_kwargs = mock_client.complete.call_args
        messages = call_kwargs.kwargs["messages"]
        user_msg = next(m for m in messages if m.role == "user")
        assert marker in user_msg.content
        assert "info_increment" in user_msg.content


def test_character_identity_reaches_actual_alignment_request(mock_client, fake_prompts_dir):
    context = '[{"name":"甲","background":"绘图员，提供地图；匿名司机身份未明"}]'
    result = validate_beat_alignment(chapter_event="甲提供车辙图", beat=_make_beat(),
        chapter_id="ch-0001", character_context=context, client=mock_client,
        model="test", prompts_dir=fake_prompts_dir)
    assert result.passed
    messages = mock_client.complete.call_args.kwargs["messages"]
    assert context in messages[1].content
    assert "只用于辨认实体" in messages[1].content


def test_full_design_fields_reach_alignment_request(mock_client, fake_prompts_dir):
    design = {"one_line_event": "负责人承认改时", "interaction_focus": "调查者施压，负责人质疑来源后选择承认", "arc_beats": {"char-001": "从封锁转为承认"}, "chapter_focus": "", "breakthrough_payoff": ""}
    result = validate_beat_alignment(chapter_event=design["one_line_event"], beat=_make_beat(),
        chapter_id="ch-0001", chapter_design=design, client=mock_client,
        model="test", prompts_dir=fake_prompts_dir)
    assert result.passed
    user = mock_client.complete.call_args.kwargs["messages"][1].content
    assert "完整创意设计" in user
    assert "调查者施压，负责人质疑来源后选择承认" in user
    assert "从封锁转为承认" in user
