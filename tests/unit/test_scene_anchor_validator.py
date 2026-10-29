"""scene_anchor_validator agent 单测(v1.1 wave 6 session 3,B2 复核)。

覆盖:
1. **natural**:LLM 返 natural → is_teleport=False
2. **teleport**:LLM 返 teleport → is_teleport=True + evidence
3. **uncertain 视作 teleport**:对齐 continuity uncertain=fail
4. **fail-open LLM 抛**:→ verdict=natural + fail_open
5. **fail-open 非法 JSON**:→ natural
6. **setup fail-open**:prompt 缺失 → natural + 0 LLM call
7. **user message 内容**:含入梦点 / 醒来点 / marker
"""

from __future__ import annotations

import json
from pathlib import Path
from unittest.mock import MagicMock

import pytest
import structlog

from src.core.agents import _prompt as prompt_module
from src.core.agents.scene_anchor_validator import (
    SceneAnchorResult,
    validate_scene_anchor,
)
from src.core.llm.base import LLMClient, LLMResponse, TokenUsage


def _make_response(
    *, verdict: str = "natural", evidence: str = "占位", model_used: str = "qwen"
) -> LLMResponse:
    return LLMResponse(
        content=json.dumps({"verdict": verdict, "evidence": evidence}, ensure_ascii=False),
        model_used=model_used,
        usage=TokenUsage(input_tokens=400, output_tokens=30),
        finish_reason="stop",
    )


@pytest.fixture
def fake_prompts_dir(tmp_path: Path) -> Path:
    prompts_dir = tmp_path / "prompts"
    prompts_dir.mkdir()
    (prompts_dir / "scene_anchor_validator_v1.md").write_text(
        "FAKE_SCENE_ANCHOR_VALIDATOR_SYSTEM_PROMPT\n\n单测断言标记。",
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


def _call(
    mock_client: MagicMock,
    fake_prompts_dir: Path,
    *,
    enter_location: str = "工厂地下密室",
    wake_location: str = "诊所催眠舱",
    chapter_text: str = "沈舟睁开眼," + "字" * 200,
) -> SceneAnchorResult:
    return validate_scene_anchor(
        enter_location=enter_location,
        wake_location=wake_location,
        chapter_text=chapter_text,
        chapter_id="ch-0022",
        enter_chapter_id="ch-0018",
        workspace_id="ws-demo",
        client=mock_client,
        model="qwen",
        prompts_dir=fake_prompts_dir,
    )


def test_natural(mock_client: MagicMock, fake_prompts_dir: Path) -> None:
    mock_client.complete.return_value = _make_response(
        verdict="natural", evidence="正文交代连夜转移"
    )
    result = _call(mock_client, fake_prompts_dir)
    assert result.verdict == "natural"
    assert result.is_teleport is False
    assert result.llm_calls_count == 1
    assert result.fail_open is False


def test_teleport(mock_client: MagicMock, fake_prompts_dir: Path) -> None:
    mock_client.complete.return_value = _make_response(
        verdict="teleport", evidence="密室入睡诊所醒,零交代"
    )
    result = _call(mock_client, fake_prompts_dir)
    assert result.verdict == "teleport"
    assert result.is_teleport is True
    assert "零交代" in result.evidence


def test_uncertain_treated_as_teleport(
    mock_client: MagicMock, fake_prompts_dir: Path
) -> None:
    mock_client.complete.return_value = _make_response(verdict="uncertain")
    result = _call(mock_client, fake_prompts_dir)
    assert result.verdict == "uncertain"
    assert result.is_teleport is True  # uncertain 视作 teleport 处置


def test_llm_raises_fail_open(
    mock_client: MagicMock, fake_prompts_dir: Path
) -> None:
    mock_client.complete.side_effect = RuntimeError("timeout")
    result = _call(mock_client, fake_prompts_dir)
    assert result.verdict == "natural"
    assert result.fail_open is True
    assert result.is_teleport is False
    assert result.llm_calls_count == 1


def test_invalid_json_fail_open(
    mock_client: MagicMock, fake_prompts_dir: Path
) -> None:
    mock_client.complete.return_value = LLMResponse(
        content="非 JSON",
        model_used="qwen",
        usage=TokenUsage(input_tokens=5, output_tokens=2),
        finish_reason="stop",
    )
    result = _call(mock_client, fake_prompts_dir)
    assert result.verdict == "natural"
    assert result.fail_open is True


def test_setup_fail_open_missing_prompt(
    mock_client: MagicMock, tmp_path: Path
) -> None:
    empty = tmp_path / "empty"
    empty.mkdir()
    result = _call(mock_client, empty)
    assert result.verdict == "natural"
    assert result.fail_open is True
    assert result.llm_calls_count == 0
    assert mock_client.complete.call_count == 0


def test_user_message_contains_anchors_and_marker(
    mock_client: MagicMock, fake_prompts_dir: Path
) -> None:
    _call(mock_client, fake_prompts_dir)
    user_msg = next(
        m for m in mock_client.complete.call_args.kwargs["messages"] if m.role == "user"
    )
    assert "工厂地下密室" in user_msg.content  # 入梦点
    assert "诊所催眠舱" in user_msg.content  # 醒来点
    assert "ch-0018" in user_msg.content  # 入梦点所在章
    assert "<!-- agent_role: scene_anchor_validator -->" in user_msg.content
