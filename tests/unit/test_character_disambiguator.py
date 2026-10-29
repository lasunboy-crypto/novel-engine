"""character_disambiguator agent 单测(v1.1 wave 2 session 5 A3 续,
2026-05-29 引入)。

8 类覆盖:
1. happy:no_disambiguate + 空 decisions
2. match (描述性指代真名披露场景重演)
3. multiple match
4. empty extracted / existing skip
5. LLM fail / invalid JSON / schema mismatch fail-open
6. invalid IDs filtered
7. top-K 字面相似轨
8. top-K 描述性指代兜底轨
"""

from __future__ import annotations

import json
from pathlib import Path
from unittest.mock import MagicMock

import pytest
import structlog

from src.core.agents import _prompt as prompt_module
from src.core.agents.character_disambiguator import (
    CharacterDisambiguationResult,
    disambiguate_characters_with_existing,
    select_top_k_existing_candidates,
)
from src.core.llm.base import LLMClient, LLMResponse, TokenUsage
from src.core.models.character import Character, VoiceProfile


def _make_response(
    *,
    verdict: str = "no_disambiguate",
    decisions: list[dict] | None = None,
    evidence: str = "本章 extracted 与 existing 无同一人不同表述",
    model_used: str = "qwen3.7-max",
) -> LLMResponse:
    payload = {
        "verdict": verdict,
        "decisions": decisions or [],
        "evidence": evidence,
    }
    return LLMResponse(
        content=json.dumps(payload, ensure_ascii=False),
        model_used=model_used,
        usage=TokenUsage(input_tokens=800, output_tokens=120),
        finish_reason="stop",
    )


def _make_character(
    *,
    character_id: str,
    name: str,
    archetype: str = "测试角色",
    workspace_id: str = "ws-demo",
) -> Character:
    return Character(
        character_id=character_id,
        workspace_id=workspace_id,
        name=name,
        aliases=[],
        archetype=archetype,
        appearance="占位外貌",
        personality="占位性格",
        background="占位背景",
        voice_profile=VoiceProfile(
            tone="平淡",
            sentence_style="平实",
            self_address="我",
            address_others={},
            common_phrases=[],
            forbidden_phrases=[],
        ),
        goals_long_term=[],
        goals_short_term=[],
        current_state={},
        relationships={},
        abilities=[],
        secrets=[],
    )


@pytest.fixture
def fake_prompts_dir(tmp_path: Path) -> Path:
    prompts_dir = tmp_path / "prompts"
    prompts_dir.mkdir()
    (prompts_dir / "character_disambiguator_v1.md").write_text(
        "FAKE_CHARACTER_DISAMBIGUATOR_PROMPT", encoding="utf-8"
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
    extracted: list[dict] | None = None,
    existing: list[Character] | None = None,
    chapter_id: str = "ch-0005",
    workspace_id: str = "ws-demo",
) -> CharacterDisambiguationResult:
    default_extracted = extracted if extracted is not None else [
        {"character_id": "ch-new-1", "name": "新角色", "description": "新角色 desc"}
    ]
    default_existing = existing if existing is not None else [
        _make_character(character_id="ch-existing-1", name="既有角色"),
    ]
    return disambiguate_characters_with_existing(
        extracted=default_extracted,
        existing=default_existing,
        chapter_id=chapter_id,
        workspace_id=workspace_id,
        client=mock_client,
        model="qwen3.7-max",
        prompts_dir=fake_prompts_dir,
    )


class TestHappy:
    def test_no_disambiguate(
        self, mock_client: MagicMock, fake_prompts_dir: Path
    ) -> None:
        result = _call(mock_client, fake_prompts_dir)
        assert result.disambiguation_decisions == []
        assert result.skipped is False
        assert result.fail_open is False
        assert result.llm_calls_count == 1


class TestMatch:
    def test_descriptive_reference_real_name_revealed(
        self, mock_client: MagicMock, fake_prompts_dir: Path
    ) -> None:
        """描述性指代 vs 真名披露场景:'胖官员' 与 '张大人' 合并。"""
        mock_client.complete.return_value = _make_response(
            verdict="disambiguate",
            decisions=[
                {
                    "extracted_character_id": "zhang-da-ren",
                    "target_existing_character_id": "pang-guan-yuan",
                    "confidence": 0.9,
                    "reason": "描述性指代'胖官员'的真名披露",
                }
            ],
            evidence="本章描述性指代真名披露",
        )
        result = _call(
            mock_client,
            fake_prompts_dir,
            extracted=[
                {
                    "character_id": "zhang-da-ren",
                    "name": "张大人",
                    "description": "高大魁梧、官员、织网者高层",
                }
            ],
            existing=[
                _make_character(
                    character_id="pang-guan-yuan", name="胖官员", archetype="官员"
                ),
            ],
        )
        assert len(result.disambiguation_decisions) == 1
        d = result.disambiguation_decisions[0]
        assert d.extracted_character_id == "zhang-da-ren"
        assert d.target_existing_character_id == "pang-guan-yuan"


class TestEmptyInput:
    def test_empty_extracted_skips(
        self, mock_client: MagicMock, fake_prompts_dir: Path
    ) -> None:
        result = _call(mock_client, fake_prompts_dir, extracted=[])
        assert result.skipped is True
        assert mock_client.complete.call_count == 0

    def test_empty_existing_skips(
        self, mock_client: MagicMock, fake_prompts_dir: Path
    ) -> None:
        result = _call(mock_client, fake_prompts_dir, existing=[])
        assert result.skipped is True
        assert mock_client.complete.call_count == 0


class TestLLMFailOpen:
    def test_llm_raises_fails_open(
        self, mock_client: MagicMock, fake_prompts_dir: Path
    ) -> None:
        mock_client.complete.side_effect = RuntimeError("LLM 故障")
        result = _call(mock_client, fake_prompts_dir)
        assert result.fail_open is True
        assert result.disambiguation_decisions == []


class TestInvalidIDs:
    def test_unknown_ids_filtered(
        self, mock_client: MagicMock, fake_prompts_dir: Path
    ) -> None:
        mock_client.complete.return_value = _make_response(
            verdict="disambiguate",
            decisions=[
                {
                    "extracted_character_id": "not-in-extracted",
                    "target_existing_character_id": "ch-existing-1",
                    "confidence": 0.9,
                    "reason": "虚构",
                }
            ],
            evidence="LLM 编造",
        )
        result = _call(mock_client, fake_prompts_dir)
        assert result.disambiguation_decisions == []
        assert result.fail_open is False


class TestTopKCandidates:
    def test_high_similarity_selected(self) -> None:
        extracted = [{"character_id": "c-new", "name": "林衍", "description": "d"}]
        existing = [
            _make_character(character_id="c-1", name="林衍二"),
            _make_character(character_id="c-2", name="完全无关 ABC"),
        ]
        result = select_top_k_existing_candidates(
            extracted=extracted, existing=existing
        )
        assert len(result) == 1
        assert result[0].character_id == "c-1"

    def test_descriptive_reference_track_picks_up_when_literal_misses(self) -> None:
        """描述性指代轨:字面相似度 ≈ 0 但 existing name 末位是 '人/官/师/夫' 等 → 无脑入选。"""
        extracted = [
            {"character_id": "c-new", "name": "张大人", "description": "官员"}
        ]
        existing = [
            _make_character(character_id="c-1", name="胖官员"),  # 字面相似度低,描述性指代轨入选
            _make_character(character_id="c-2", name="陆擎"),  # 字面相似度低,非描述性指代,过滤
        ]
        result = select_top_k_existing_candidates(
            extracted=extracted, existing=existing
        )
        assert len(result) == 1
        assert result[0].character_id == "c-1"  # 胖官员 末位"员"在描述性指代后缀里
