"""mechanism_consistency_validator agent 单测(v1.1 wave 6 session 1 / A1)。

覆盖(对齐 beat_alignment_validator 同型测试范围):

1. **happy**:无矛盾 + 无缺规则 → passed=True
2. **contradiction**:真互斥设定 → passed=False / contradictions 非空(HARD)
3. **vague only**:高概念缺规则但无矛盾 → passed=True + vague_mechanisms 非空(WARN 不阻断,护 C3)
4. **LLM fail**:LLM 抛异常 → fail-open(passed=True + fail_open=True)
5. **setup fail**:prompt 文件缺失 → fail-open
6. **retry feedback**:``format_mechanism_failure_for_retry`` 输出含 contradictions 字面

完全 mock ``LLMClient``;用临时目录注入 fake ``mechanism_consistency_validator_v1.md``。
"""

from __future__ import annotations

import json
from pathlib import Path
from unittest.mock import MagicMock

import pytest

from src.core.agents import _prompt as prompt_module
from src.core.agents.mechanism_consistency_validator import (
    MechanismConsistencyResult,
    format_mechanism_failure_for_retry,
    validate_mechanism_consistency,
)
from src.core.llm.base import LLMClient, LLMResponse, TokenUsage
from src.core.models.outline import WorldOutput


def _make_response(
    *,
    contradictions: list[str] | None = None,
    vague_mechanisms: list[str] | None = None,
    evidence: str = "机制自洽",
    model_used: str = "qwen-max",
) -> LLMResponse:
    payload = {
        "contradictions": contradictions or [],
        "vague_mechanisms": vague_mechanisms or [],
        "evidence": evidence,
    }
    return LLMResponse(
        content=json.dumps(payload, ensure_ascii=False),
        model_used=model_used,
        usage=TokenUsage(input_tokens=500, output_tokens=60),
        finish_reason="stop",
    )


def _make_world(
    *,
    unique_rules: str = "入梦者纯意识穿梭,肉体留现实催眠舱;梦中所获随醒来消散。",
) -> WorldOutput:
    return WorldOutput(
        setting="近未来都市,催眠治疗普及。",
        power_system="入梦技术:治疗师引导患者进入梦境。",
        factions="心理诊所 vs 地下黑客组织。",
        culture="科技与伦理张力下的都市文化。",
        unique_rules=unique_rules,
    )


@pytest.fixture
def fake_prompts_dir(tmp_path: Path) -> Path:
    prompts_dir = tmp_path / "prompts"
    prompts_dir.mkdir()
    (prompts_dir / "mechanism_consistency_validator_v1.md").write_text(
        "FAKE_MECHANISM_CONSISTENCY_VALIDATOR_PROMPT\n\n用于单测的 system 标记。",
        encoding="utf-8",
    )
    return prompts_dir


@pytest.fixture(autouse=True)
def _clear_prompt_cache() -> None:
    prompt_module.load_prompt_template.cache_clear()


@pytest.fixture
def mock_client() -> MagicMock:
    client = MagicMock(spec=LLMClient)
    client.complete.return_value = _make_response()
    return client


def _call(
    mock_client: MagicMock,
    fake_prompts_dir: Path,
    *,
    world: WorldOutput | None = None,
) -> MechanismConsistencyResult:
    return validate_mechanism_consistency(
        world=world if world is not None else _make_world(),
        premise="心理医生进入患者梦境治疗,卷入跨梦境与现实的连环案。",
        genre="悬疑",
        workspace_id="ws-demo",
        client=mock_client,
        model="qwen-max",
        prompts_dir=fake_prompts_dir,
    )


class TestHappy:
    def test_no_contradiction_no_vague_passes(
        self, mock_client: MagicMock, fake_prompts_dir: Path
    ) -> None:
        mock_client.complete.return_value = _make_response(
            contradictions=[], vague_mechanisms=[], evidence="机制自洽,规则齐全"
        )
        result = _call(mock_client, fake_prompts_dir)
        assert result.passed is True
        assert result.contradictions == []
        assert result.vague_mechanisms == []
        assert result.llm_calls_count == 1
        assert result.fail_open is False
        assert mock_client.complete.call_count == 1


class TestContradiction:
    def test_contradiction_fails_hard(
        self, mock_client: MagicMock, fake_prompts_dir: Path
    ) -> None:
        mock_client.complete.return_value = _make_response(
            contradictions=[
                "unique_rules 同时说'梦中物随醒消散'与'可实体化携回现实',互斥",
            ],
            vague_mechanisms=[],
            evidence="两条规则对'梦境物品能否带回现实'互斥",
        )
        result = _call(
            mock_client,
            fake_prompts_dir,
            world=_make_world(
                unique_rules="纯意识穿梭,梦中物随醒消散;但资深者可将梦中器物实体化携回现实。",
            ),
        )
        assert result.passed is False
        assert len(result.contradictions) == 1
        assert "互斥" in result.contradictions[0]


class TestVagueOnly:
    def test_vague_does_not_block(
        self, mock_client: MagicMock, fake_prompts_dir: Path
    ) -> None:
        """高概念缺规则是 WARN — 不影响 passed(护 §0 第一原则:留白合法)。"""
        mock_client.complete.return_value = _make_response(
            contradictions=[],
            vague_mechanisms=[
                "'意识实验体'是承重设定但缺肉体状态规则,易写出'肉体在 vs 不在'矛盾",
            ],
            evidence="承重机制缺规则,WARN 标记非矛盾",
        )
        result = _call(mock_client, fake_prompts_dir)
        assert result.passed is True  # WARN 不阻断
        assert len(result.vague_mechanisms) == 1
        assert result.contradictions == []


class TestFailOpen:
    def test_llm_exception_fail_open(
        self, mock_client: MagicMock, fake_prompts_dir: Path
    ) -> None:
        mock_client.complete.side_effect = RuntimeError("LLM 瞬态故障")
        result = _call(mock_client, fake_prompts_dir)
        assert result.passed is True
        assert result.fail_open is True

    def test_setup_missing_prompt_fail_open(
        self, mock_client: MagicMock, tmp_path: Path
    ) -> None:
        empty_dir = tmp_path / "empty_prompts"
        empty_dir.mkdir()
        result = validate_mechanism_consistency(
            world=_make_world(),
            client=mock_client,
            model="qwen-max",
            prompts_dir=empty_dir,
        )
        assert result.passed is True
        assert result.fail_open is True
        assert result.llm_calls_count == 0


class TestRetryFeedback:
    def test_format_failure_contains_contradictions(self) -> None:
        result = MechanismConsistencyResult(
            passed=False,
            contradictions=["A 与 B 互斥", "C 违反 premise"],
            vague_mechanisms=[],
            evidence="两处矛盾",
        )
        text = format_mechanism_failure_for_retry(result=result)
        assert "A 与 B 互斥" in text
        assert "C 违反 premise" in text
        assert "互斥" in text
