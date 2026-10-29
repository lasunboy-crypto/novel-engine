"""ending_tension_validator agent 单测(v1.1 wave 6 session 4,B6)。

覆盖:
1. **earned**:LLM 返 earned(自然收束)→ passed=True
2. **abrupt**:LLM 返 abrupt(强行断裂)→ passed=False + evidence
3. **skip 空正文**:chapter_text 空 → skipped
4. **fail-open LLM 抛 / 非法 JSON / setup**(prompt 缺失)
5. **user message**:含终章标记 + 张力等级 + marker

注:仅终章触发的**门控逻辑**(is_arc_finale + total_chapters)在 orchestrator 层,
见 ``test_ending_tension_gating.py``;本文件只测 agent 本身。
"""

from __future__ import annotations

import json
from pathlib import Path
from unittest.mock import MagicMock

import pytest
import structlog

from src.core.agents import _prompt as prompt_module
from src.core.agents.ending_tension_validator import validate_ending_tension
from src.core.llm.base import LLMClient, LLMResponse, TokenUsage


def _make_response(
    *, verdict: str = "earned", evidence: str = "结局自然收束有铺垫"
) -> LLMResponse:
    return LLMResponse(
        content=json.dumps(
            {"verdict": verdict, "evidence": evidence}, ensure_ascii=False
        ),
        model_used="qwen-novel:latest",
        usage=TokenUsage(input_tokens=600, output_tokens=30),
        finish_reason="stop",
    )


@pytest.fixture
def fake_prompts_dir(tmp_path: Path) -> Path:
    prompts_dir = tmp_path / "prompts"
    prompts_dir.mkdir()
    (prompts_dir / "ending_tension_validator_v1.md").write_text(
        "FAKE_ENDING_TENSION_VALIDATOR_SYSTEM_PROMPT\n\n单测断言标记。",
        encoding="utf-8",
    )
    return prompts_dir


@pytest.fixture(autouse=True)
def _clear_prompt_cache() -> None:
    prompt_module.load_prompt_template.cache_clear()


@pytest.fixture(autouse=True)
def _ensure_debug_level() -> None:
    structlog.configure(wrapper_class=structlog.make_filtering_bound_logger(0))


@pytest.fixture
def mock_client() -> MagicMock:
    client = MagicMock(spec=LLMClient)
    client.complete.return_value = _make_response()
    return client


_CHAPTER = "高潮后,他安葬了同伴,与幸存者一一道别,独自回望战场。风停了。" + "尾" * 100


def _call(mock_client: MagicMock, fake_prompts_dir: Path, **kw):  # noqa: ANN003
    return validate_ending_tension(
        chapter_text=kw.pop("chapter_text", _CHAPTER),
        tension_level=kw.pop("tension_level", 8),
        chapter_id="ch-0030",
        workspace_id="ws-demo",
        client=mock_client,
        model="qwen-novel:latest",
        prompts_dir=fake_prompts_dir,
        **kw,
    )


def test_earned_passes(mock_client: MagicMock, fake_prompts_dir: Path) -> None:
    result = _call(mock_client, fake_prompts_dir)
    assert result.passed is True
    assert result.verdict == "earned"
    assert result.llm_calls_count == 1
    assert result.skipped is False


def test_abrupt_reported(mock_client: MagicMock, fake_prompts_dir: Path) -> None:
    mock_client.complete.return_value = _make_response(
        verdict="abrupt", evidence="生死搏斗后无过渡直接旁白三年后幸福生活,张力断裂"
    )
    result = _call(mock_client, fake_prompts_dir)
    assert result.passed is False
    assert result.verdict == "abrupt"
    assert "张力断裂" in result.evidence


def test_empty_text_skips(mock_client: MagicMock, fake_prompts_dir: Path) -> None:
    result = _call(mock_client, fake_prompts_dir, chapter_text="   ")
    assert result.skipped is True
    assert result.llm_calls_count == 0
    mock_client.complete.assert_not_called()


def test_llm_raises_fail_open(
    mock_client: MagicMock, fake_prompts_dir: Path
) -> None:
    mock_client.complete.side_effect = RuntimeError("timeout")
    result = _call(mock_client, fake_prompts_dir)
    assert result.passed is True
    assert result.fail_open is True


def test_invalid_json_fail_open(
    mock_client: MagicMock, fake_prompts_dir: Path
) -> None:
    mock_client.complete.return_value = LLMResponse(
        content="不是 JSON",
        model_used="qwen-novel:latest",
        usage=TokenUsage(input_tokens=10, output_tokens=5),
        finish_reason="stop",
    )
    result = _call(mock_client, fake_prompts_dir)
    assert result.passed is True
    assert result.fail_open is True


def test_setup_fail_open_missing_prompt(
    mock_client: MagicMock, tmp_path: Path
) -> None:
    empty_dir = tmp_path / "empty"
    empty_dir.mkdir()
    result = _call(mock_client, empty_dir)
    assert result.passed is True
    assert result.fail_open is True
    assert result.llm_calls_count == 0
    mock_client.complete.assert_not_called()


def test_user_message_contains_finale_marker_and_tension(
    mock_client: MagicMock, fake_prompts_dir: Path
) -> None:
    _call(mock_client, fake_prompts_dir, tension_level=9)
    content = next(
        m
        for m in mock_client.complete.call_args.kwargs["messages"]
        if m.role == "user"
    ).content
    assert "全书最后一章" in content
    assert "9" in content  # tension_level
    assert "<!-- agent_role: ending_tension_validator -->" in content
