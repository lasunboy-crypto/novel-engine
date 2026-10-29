"""must_achieve_validator agent 单测(P7-B-06 引入,v1.1 wave 2 session 6 G9.3 改批处理)。

7 类覆盖(plan §4 wave 2 w2-s6 + G9.3 实施):
1. **happy**:3 条 must_achieve 全 yes → passed=True / missing_items=[]
2. **hard miss**:3 条全 no → passed=False / missing_items=[全 3 条]
3. **partial**:2 yes 1 no → passed=False / missing_items=[1 条]
4. **empty must_achieve**:章节卡无 must_achieve → 跳过 + passed=True / skipped=True
5. **LLM fail**:Qwen 抛异常 → fail-open(passed=True) + warn 日志
6. **setup fail-open**:prompt 文件缺失 → fail-open(passed=True) + 未调 LLM
7. **retry-with-feedback**(G9.3 新增):batch schema 错位 → retry 1 次成功 / retry 仍失败 fail-open

完全 mock ``LLMClient``;用临时目录注入 fake ``must_achieve_validator_v2.md`` prompt
模板。Pydantic ``_ValidatorBatchPayload`` schema 校验 LLM 批处理输出。
"""

from __future__ import annotations

import json
from pathlib import Path
from unittest.mock import MagicMock

import pytest
import structlog
from structlog.testing import capture_logs

from src.core.agents import _prompt as prompt_module
from src.core.agents.must_achieve_validator import (
    MustAchieveTechnicalError,
    ValidationResult,
    _parse_and_validate_batch,
    validate_must_achieve,
)
from src.core.llm.base import LLMClient, LLMResponse, TokenUsage


# ---------------------------------------------------------------------------
# 工厂 & 公共 fixture
# ---------------------------------------------------------------------------


def _make_batch_response(
    *,
    verdicts: list[str] | None = None,
    evidences: list[str] | None = None,
    model_used: str = "qwen3.7-max",
) -> LLMResponse:
    """合成批处理 LLM 输出:results list,每条含 item_index / verdict / evidence。

    verdicts 长度决定 results 条数;evidences 缺省按 verdicts 长度补占位字面。
    """
    verdicts = verdicts or ["yes"]
    evidences = evidences or [f"占位证据 {i + 1}" for i in range(len(verdicts))]
    assert len(verdicts) == len(evidences)
    payload = {
        "results": [
            {
                "item_index": idx + 1,
                "verdict": v,
                "evidence": e,
            }
            for idx, (v, e) in enumerate(zip(verdicts, evidences))
        ]
    }
    return LLMResponse(
        content=json.dumps(payload, ensure_ascii=False),
        model_used=model_used,
        usage=TokenUsage(input_tokens=3500, output_tokens=300),
        finish_reason="stop",
    )


@pytest.fixture
def fake_prompts_dir(tmp_path: Path) -> Path:
    """在临时目录里建 must_achieve_validator_v2.md(fake)。"""
    prompts_dir = tmp_path / "prompts"
    prompts_dir.mkdir()
    (prompts_dir / "must_achieve_validator_v2.md").write_text(
        "FAKE_VALIDATOR_SYSTEM_PROMPT\n\n用于单测断言的 system 标记。",
        encoding="utf-8",
    )
    return prompts_dir


@pytest.fixture(autouse=True)
def _clear_prompt_cache() -> None:
    prompt_module.load_prompt_template.cache_clear()


@pytest.fixture(autouse=True)
def _ensure_debug_level_for_capture_logs() -> None:
    """``must_achieve_validator_skipped`` 是 DEBUG level。"""
    structlog.configure(wrapper_class=structlog.make_filtering_bound_logger(0))


@pytest.fixture
def mock_client() -> MagicMock:
    client = MagicMock(spec=LLMClient)
    client.complete.return_value = _make_batch_response(verdicts=["yes"])
    return client


def _call(
    mock_client: MagicMock,
    fake_prompts_dir: Path,
    *,
    must_achieve: list[str],
    chapter_text: str = "占位章节正文(中文,~200 字)。" + "字" * 200,
    chapter_id: str = "ch-0002",
    workspace_id: str = "ws-demo",
    model: str = "qwen3.7-max",
    authoritative: bool = False,
) -> ValidationResult:
    return validate_must_achieve(
        chapter_text=chapter_text,
        must_achieve=must_achieve,
        chapter_id=chapter_id,
        workspace_id=workspace_id,
        client=mock_client,
        model=model,
        prompts_dir=fake_prompts_dir,
        authoritative=authoritative,
    )


# ---------------------------------------------------------------------------
# 1. Happy:全 yes(批处理单次调用)
# ---------------------------------------------------------------------------


class TestHappy:
    def test_three_items_all_yes(
        self, mock_client: MagicMock, fake_prompts_dir: Path
    ) -> None:
        # G9.3 批处理:3 条 must_achieve,单次 LLM 返 list 全 yes
        mock_client.complete.return_value = _make_batch_response(
            verdicts=["yes", "yes", "yes"],
            evidences=["第 1 条达成", "第 2 条达成", "第 3 条达成"],
        )
        items = ["林衍突破第一道剑阵", "苏离表明立场", "玉佩颤动"]
        result = _call(mock_client, fake_prompts_dir, must_achieve=items)

        assert result.passed is True
        assert result.missing_items == []
        assert result.llm_calls_count == 1  # 批处理单次调用(v1 是 N 次)
        assert result.skipped is False
        assert result.fail_open is False
        assert [item.status for item in result.constraint_results] == [
            "pass",
            "pass",
            "pass",
        ]
        assert {item.domain for item in result.constraint_results} == {"must_achieve"}
        assert len({item.constraint_id for item in result.constraint_results}) == 3
        # 关键:批处理只调 1 次(对比 v1 的 3 次)
        assert mock_client.complete.call_count == 1

    def test_single_item_yes(
        self, mock_client: MagicMock, fake_prompts_dir: Path
    ) -> None:
        # N=1 也走批处理路径(单条 list)
        mock_client.complete.return_value = _make_batch_response(verdicts=["yes"])
        result = _call(
            mock_client, fake_prompts_dir, must_achieve=["林衍突破第一道剑阵"]
        )

        assert result.passed is True
        assert result.llm_calls_count == 1


# ---------------------------------------------------------------------------
# 2. Hard miss:全 no
# ---------------------------------------------------------------------------


class TestHardMiss:
    def test_three_items_all_no(
        self, mock_client: MagicMock, fake_prompts_dir: Path
    ) -> None:
        mock_client.complete.return_value = _make_batch_response(
            verdicts=["no", "no", "no"],
            evidences=["未发现", "未发现", "未发现"],
        )
        items = ["事件 A", "事件 B", "事件 C"]
        result = _call(mock_client, fake_prompts_dir, must_achieve=items)

        assert result.passed is False
        # missing_items 按入参 must_achieve 顺序
        assert result.missing_items == items
        assert result.llm_calls_count == 1
        assert result.skipped is False
        assert result.fail_open is False
        assert [item.status for item in result.constraint_results] == [
            "fail",
            "fail",
            "fail",
        ]

    def test_uncertain_treated_as_missing(
        self, mock_client: MagicMock, fake_prompts_dir: Path
    ) -> None:
        # 自决项:uncertain 视作 missing(plan §1 Part 2.1 字面沿用 v1)
        mock_client.complete.return_value = _make_batch_response(
            verdicts=["uncertain"], evidences=["正文含糊"]
        )
        items = ["林衍突破第一道剑阵"]
        result = _call(mock_client, fake_prompts_dir, must_achieve=items)

        assert result.passed is False
        assert result.missing_items == items


# ---------------------------------------------------------------------------
# 3. Partial:部分 yes 部分 no(单次批处理混合 verdict)
# ---------------------------------------------------------------------------


class TestPartial:
    def test_two_yes_one_no(
        self, mock_client: MagicMock, fake_prompts_dir: Path
    ) -> None:
        mock_client.complete.return_value = _make_batch_response(
            verdicts=["yes", "no", "yes"],
            evidences=["A 达成", "B 未发现", "C 达成"],
        )
        items = ["事件 A", "事件 B", "事件 C"]
        result = _call(mock_client, fake_prompts_dir, must_achieve=items)

        assert result.passed is False
        # 只 "事件 B" 进 missing(顺序按入参,不按 LLM 输出顺序)
        assert result.missing_items == ["事件 B"]
        assert result.llm_calls_count == 1

    def test_mixed_verdicts_yes_uncertain_no(
        self, mock_client: MagicMock, fake_prompts_dir: Path
    ) -> None:
        # yes / uncertain / no 三档混合:uncertain 与 no 都计 missing
        mock_client.complete.return_value = _make_batch_response(
            verdicts=["yes", "uncertain", "no"],
            evidences=["A 达成", "B 含糊", "C 未发现"],
        )
        items = ["A", "B", "C"]
        result = _call(mock_client, fake_prompts_dir, must_achieve=items)

        assert result.passed is False
        assert result.missing_items == ["B", "C"]

    def test_item_index_out_of_order_in_llm_output(
        self, mock_client: MagicMock, fake_prompts_dir: Path
    ) -> None:
        # G9.3 关键:LLM 输出乱序但 item_index 正确 → caller 按 item_index 排序
        # 还原对应关系(防 LLM 输出顺序倒置)
        payload = {
            "results": [
                {"item_index": 3, "verdict": "no", "evidence": "C 未发现"},
                {"item_index": 1, "verdict": "yes", "evidence": "A 达成"},
                {"item_index": 2, "verdict": "yes", "evidence": "B 达成"},
            ]
        }
        mock_client.complete.return_value = LLMResponse(
            content=json.dumps(payload, ensure_ascii=False),
            model_used="qwen3.7-max",
            usage=TokenUsage(input_tokens=3500, output_tokens=300),
            finish_reason="stop",
        )
        items = ["A", "B", "C"]
        result = _call(mock_client, fake_prompts_dir, must_achieve=items)

        assert result.passed is False
        # missing 应为 ["C"](按 item_index 3 对应 must_achieve[2]="C")
        assert result.missing_items == ["C"]


# ---------------------------------------------------------------------------
# 4. Empty must_achieve:跳过 + passed=True(沿用 v1)
# ---------------------------------------------------------------------------


class TestEmptyMustAchieve:
    def test_empty_list_skipped(
        self, mock_client: MagicMock, fake_prompts_dir: Path
    ) -> None:
        result = _call(mock_client, fake_prompts_dir, must_achieve=[])

        assert result.passed is True
        assert result.missing_items == []
        assert result.llm_calls_count == 0
        assert result.skipped is True
        assert result.fail_open is False
        # 关键:不发起任何 LLM 调用
        assert mock_client.complete.call_count == 0

    def test_empty_list_emits_skip_log_event(
        self, mock_client: MagicMock, fake_prompts_dir: Path
    ) -> None:
        with capture_logs() as logs:
            _call(
                mock_client,
                fake_prompts_dir,
                must_achieve=[],
                chapter_id="ch-0099",
                workspace_id="ws-empty",
            )

        skip_events = [
            e for e in logs if e.get("event") == "must_achieve_validator_skipped"
        ]
        assert len(skip_events) == 1
        assert skip_events[0]["chapter_id"] == "ch-0099"
        assert skip_events[0]["workspace_id"] == "ws-empty"
        assert skip_events[0]["reason"] == "empty_must_achieve"


# ---------------------------------------------------------------------------
# 5. LLM fail:LLM 调用抛 → 直接 fail-open(不 retry,沿用 v1 决策 4)
# ---------------------------------------------------------------------------


class TestLLMFail:
    def test_llm_call_raises_returns_fail_open(
        self, mock_client: MagicMock, fake_prompts_dir: Path
    ) -> None:
        # LLM 调用本身抛(网络 / 5xx)→ 直接 fail-open,不 retry(retry 大概率也失败)
        mock_client.complete.side_effect = RuntimeError("dashscope 504 Gateway Timeout")
        items = ["事件 A", "事件 B"]
        result = _call(mock_client, fake_prompts_dir, must_achieve=items)

        assert result.passed is True
        assert result.missing_items == []
        # llm_calls_count=0 因为 complete() 在递增前就抛了
        assert result.llm_calls_count == 0
        assert result.fail_open is True
        assert result.skipped is False
        # 只调了 1 次(直接 fail-open,不 retry)
        assert mock_client.complete.call_count == 1

    def test_llm_fail_emits_warn_log(
        self, mock_client: MagicMock, fake_prompts_dir: Path
    ) -> None:
        mock_client.complete.side_effect = TimeoutError("dashscope deadline")
        with capture_logs() as logs:
            _call(
                mock_client,
                fake_prompts_dir,
                must_achieve=["事件 A"],
                chapter_id="ch-0099",
            )

        warn_events = [
            e for e in logs if e.get("event") == "must_achieve_validator_failed_open"
        ]
        assert len(warn_events) == 1
        assert warn_events[0]["chapter_id"] == "ch-0099"
        assert warn_events[0]["error_type"] == "TimeoutError"


# ---------------------------------------------------------------------------
# 6. Setup fail-open(prompt 缺失 / 路由错配,沿用 v1)
# ---------------------------------------------------------------------------


class TestSetupFailOpen:
    def test_missing_prompt_file_fail_open(
        self, mock_client: MagicMock, tmp_path: Path
    ) -> None:
        # prompts_dir 不含 must_achieve_validator_v2.md → fail-open
        empty_dir = tmp_path / "empty_prompts"
        empty_dir.mkdir()
        result = validate_must_achieve(
            chapter_text="占位正文",
            must_achieve=["事件 A"],
            chapter_id="ch-0001",
            workspace_id="ws-demo",
            client=mock_client,
            model="claude-sonnet-4-6",
            prompts_dir=empty_dir,
        )

        assert result.passed is True
        assert result.fail_open is True
        # 未发起任何 LLM 调用(setup 阶段失败)
        assert mock_client.complete.call_count == 0

    def test_missing_prompt_file_authoritative_stops(
        self, mock_client: MagicMock, tmp_path: Path
    ) -> None:
        empty_dir = tmp_path / "empty_prompts_authoritative"
        empty_dir.mkdir()

        with pytest.raises(MustAchieveTechnicalError) as exc_info:
            validate_must_achieve(
                chapter_text="占位正文",
                must_achieve=["事件 A"],
                chapter_id="ch-0001",
                workspace_id="ws-demo",
                client=mock_client,
                model="qwen3.8-max",
                prompts_dir=empty_dir,
                authoritative=True,
            )

        assert exc_info.value.stage == "setup"
        assert mock_client.complete.call_count == 0


# ---------------------------------------------------------------------------
# 7. Retry-with-feedback(G9.3 新增):batch schema 错位 → retry 1 次
# ---------------------------------------------------------------------------


class TestRetryWithFeedback:
    def test_invalid_json_first_call_retry_success(
        self, mock_client: MagicMock, fake_prompts_dir: Path
    ) -> None:
        # 第 1 次返非合法 JSON → retry → 第 2 次返合法 list
        bad_response = LLMResponse(
            content="not a valid json {{",
            model_used="qwen3.7-max",
            usage=TokenUsage(input_tokens=3500, output_tokens=30),
            finish_reason="stop",
        )
        good_response = _make_batch_response(verdicts=["yes", "yes"])
        mock_client.complete.side_effect = [bad_response, good_response]
        items = ["事件 A", "事件 B"]
        result = _call(mock_client, fake_prompts_dir, must_achieve=items)

        # retry 后成功 → passed=True / fail_open=False
        assert result.passed is True
        assert result.missing_items == []
        assert result.fail_open is False
        # llm_calls_count=2(1 次主调 + 1 次 retry)
        assert result.llm_calls_count == 2
        assert mock_client.complete.call_count == 2

    def test_results_length_mismatch_retry_success(
        self, mock_client: MagicMock, fake_prompts_dir: Path
    ) -> None:
        # 第 1 次返 results 长度 2 但入参是 3 条 → retry → 第 2 次正确
        bad_payload = {
            "results": [
                {"item_index": 1, "verdict": "yes", "evidence": "A 达成"},
                {"item_index": 2, "verdict": "yes", "evidence": "B 达成"},
                # 缺第 3 条
            ]
        }
        bad_response = LLMResponse(
            content=json.dumps(bad_payload, ensure_ascii=False),
            model_used="qwen3.7-max",
            usage=TokenUsage(input_tokens=3500, output_tokens=200),
            finish_reason="stop",
        )
        good_response = _make_batch_response(verdicts=["yes", "yes", "yes"])
        mock_client.complete.side_effect = [bad_response, good_response]
        items = ["A", "B", "C"]
        result = _call(mock_client, fake_prompts_dir, must_achieve=items)

        assert result.passed is True
        assert result.llm_calls_count == 2
        assert mock_client.complete.call_count == 2

    def test_item_index_duplicate_retry_success(
        self, mock_client: MagicMock, fake_prompts_dir: Path
    ) -> None:
        # 第 1 次 item_index 重复(1, 1, 3 缺 2)→ retry → 第 2 次正确
        bad_payload = {
            "results": [
                {"item_index": 1, "verdict": "yes", "evidence": "A 达成"},
                {"item_index": 1, "verdict": "no", "evidence": "重复 index"},
                {"item_index": 3, "verdict": "yes", "evidence": "C 达成"},
            ]
        }
        bad_response = LLMResponse(
            content=json.dumps(bad_payload, ensure_ascii=False),
            model_used="qwen3.7-max",
            usage=TokenUsage(input_tokens=3500, output_tokens=200),
            finish_reason="stop",
        )
        good_response = _make_batch_response(verdicts=["yes", "yes", "yes"])
        mock_client.complete.side_effect = [bad_response, good_response]
        items = ["A", "B", "C"]
        result = _call(mock_client, fake_prompts_dir, must_achieve=items)

        assert result.passed is True
        assert result.llm_calls_count == 2

    def test_item_index_out_of_range_retry_success(
        self, mock_client: MagicMock, fake_prompts_dir: Path
    ) -> None:
        # 第 1 次 item_index 超界(1, 2, 5 — 但 N=3)→ retry → 第 2 次正确
        bad_payload = {
            "results": [
                {"item_index": 1, "verdict": "yes", "evidence": "A 达成"},
                {"item_index": 2, "verdict": "yes", "evidence": "B 达成"},
                {"item_index": 5, "verdict": "yes", "evidence": "超界"},
            ]
        }
        bad_response = LLMResponse(
            content=json.dumps(bad_payload, ensure_ascii=False),
            model_used="qwen3.7-max",
            usage=TokenUsage(input_tokens=3500, output_tokens=200),
            finish_reason="stop",
        )
        good_response = _make_batch_response(verdicts=["yes", "yes", "yes"])
        mock_client.complete.side_effect = [bad_response, good_response]
        items = ["A", "B", "C"]
        result = _call(mock_client, fake_prompts_dir, must_achieve=items)

        assert result.passed is True
        assert result.llm_calls_count == 2

    def test_invalid_verdict_value_retry_success(
        self, mock_client: MagicMock, fake_prompts_dir: Path
    ) -> None:
        # 第 1 次 verdict 非法值(maybe)→ retry → 第 2 次正确
        bad_payload = {
            "results": [
                {"item_index": 1, "verdict": "maybe", "evidence": "非法 verdict"},
            ]
        }
        bad_response = LLMResponse(
            content=json.dumps(bad_payload, ensure_ascii=False),
            model_used="qwen3.7-max",
            usage=TokenUsage(input_tokens=3500, output_tokens=200),
            finish_reason="stop",
        )
        good_response = _make_batch_response(verdicts=["yes"])
        mock_client.complete.side_effect = [bad_response, good_response]
        items = ["A"]
        result = _call(mock_client, fake_prompts_dir, must_achieve=items)

        assert result.passed is True
        assert result.llm_calls_count == 2

    def test_retry_still_fails_returns_fail_open(
        self, mock_client: MagicMock, fake_prompts_dir: Path
    ) -> None:
        # 第 1 次 schema 错 + 第 2 次 retry 仍然错 → fail-open
        bad_response_1 = LLMResponse(
            content="not valid json {{",
            model_used="qwen3.7-max",
            usage=TokenUsage(input_tokens=3500, output_tokens=30),
            finish_reason="stop",
        )
        bad_response_2 = LLMResponse(
            content='{"results": [{"item_index": 99, "verdict": "yes", "evidence": "x"}]}',
            model_used="qwen3.7-max",
            usage=TokenUsage(input_tokens=3500, output_tokens=200),
            finish_reason="stop",
        )
        mock_client.complete.side_effect = [bad_response_1, bad_response_2]
        items = ["A"]
        result = _call(mock_client, fake_prompts_dir, must_achieve=items)

        # retry 后仍失败 → fail-open
        assert result.passed is True
        assert result.fail_open is True
        assert result.llm_calls_count == 2
        assert mock_client.complete.call_count == 2

    def test_retry_emits_retry_log_event(
        self, mock_client: MagicMock, fake_prompts_dir: Path
    ) -> None:
        bad_response = LLMResponse(
            content="garbage",
            model_used="qwen3.7-max",
            usage=TokenUsage(input_tokens=3500, output_tokens=30),
            finish_reason="stop",
        )
        good_response = _make_batch_response(verdicts=["yes"])
        mock_client.complete.side_effect = [bad_response, good_response]

        with capture_logs() as logs:
            _call(
                mock_client,
                fake_prompts_dir,
                must_achieve=["事件 A"],
                chapter_id="ch-retry",
            )

        retry_events = [
            e
            for e in logs
            if e.get("event") == "must_achieve_validator_retry_triggered"
        ]
        assert len(retry_events) == 1
        assert retry_events[0]["chapter_id"] == "ch-retry"

    def test_retry_feedback_appears_in_second_user_message(
        self, mock_client: MagicMock, fake_prompts_dir: Path
    ) -> None:
        # 验 retry 时 user message 含 "上次输出问题" feedback 字面
        bad_response = LLMResponse(
            content="garbage",
            model_used="qwen3.7-max",
            usage=TokenUsage(input_tokens=3500, output_tokens=30),
            finish_reason="stop",
        )
        good_response = _make_batch_response(verdicts=["yes"])
        mock_client.complete.side_effect = [bad_response, good_response]

        _call(mock_client, fake_prompts_dir, must_achieve=["事件 A"])

        # 第 2 次调用的 user message(messages[1].content)应含 retry feedback
        assert mock_client.complete.call_count == 2
        second_call = mock_client.complete.call_args_list[1]
        messages = second_call.kwargs["messages"]
        second_user_message = messages[1].content
        assert "上次输出问题" in second_user_message
        # 也包含 "请自纠后重新输出" 字面
        assert "自纠" in second_user_message

    def test_bare_dialogue_quotes_in_evidence_are_repaired(self) -> None:
        raw = (
            '{"results": ['
            '{"item_index": 1, "verdict": "yes", '
            '"evidence": "秦岚说"卷宗调到了，我过来"然后挂了"}'
            ']}'
        )

        payloads = _parse_and_validate_batch(
            raw_text=raw,
            model_used="qwen3.8-max",
            expected_count=1,
        )

        assert payloads[0].evidence == '秦岚说"卷宗调到了，我过来"然后挂了'

    def test_unrepairable_json_is_still_rejected(self) -> None:
        with pytest.raises(ValueError, match="非合法 JSON"):
            _parse_and_validate_batch(
                raw_text='{"results": [',
                model_used="qwen3.8-max",
                expected_count=1,
            )

    def test_authoritative_retry_exhaustion_stops_instead_of_fail_open(
        self, mock_client: MagicMock, fake_prompts_dir: Path
    ) -> None:
        broken = LLMResponse(
            content='{"results": [',
            model_used="qwen3.8-max",
            usage=TokenUsage(input_tokens=10, output_tokens=10),
            finish_reason="stop",
        )
        mock_client.complete.side_effect = [broken, broken]

        with pytest.raises(MustAchieveTechnicalError) as exc_info:
            _call(
                mock_client,
                fake_prompts_dir,
                must_achieve=["事件 A"],
                authoritative=True,
            )

        assert exc_info.value.stage == "parse_or_schema"
        assert exc_info.value.attempts == 2

    def test_authoritative_transport_error_stops_instead_of_fail_open(
        self, mock_client: MagicMock, fake_prompts_dir: Path
    ) -> None:
        mock_client.complete.side_effect = TimeoutError("provider timeout")

        with pytest.raises(MustAchieveTechnicalError) as exc_info:
            _call(
                mock_client,
                fake_prompts_dir,
                must_achieve=["事件 A"],
                authoritative=True,
            )

        assert exc_info.value.stage == "transport"
        assert exc_info.value.attempts == 1
