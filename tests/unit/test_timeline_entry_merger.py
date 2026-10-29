"""timeline_entry_merger agent 单测(v1.1 wave 2 session 5 A3 续,
2026-05-29 引入)。

7 类覆盖(对齐 plot_thread_merger / character_disambiguator 同型):
1. happy:no_merge + 空 decisions
2. merge (跨章节回忆同事件场景重演)
3. empty extracted / existing skip
4. LLM fail-open
5. invalid IDs filtered
6. top-K candidate selection
"""

from __future__ import annotations

import json
from pathlib import Path
from unittest.mock import MagicMock

import pytest
import structlog

from src.core.agents import _prompt as prompt_module
from src.core.agents.timeline_entry_merger import (
    TimelineEntryMergeResult,
    _parse_llm_payload,
    _repair_unescaped_inner_quotes,
    merge_timeline_entries_with_existing,
    select_top_k_existing_candidates,
)
from src.core.llm.base import LLMClient, LLMResponse, TokenUsage
from src.core.models.timeline_entry import TimelineEntry


def _make_response(
    *,
    verdict: str = "no_merge",
    decisions: list[dict] | None = None,
    evidence: str = "本章 extracted 与 existing 无同事件重复",
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
        usage=TokenUsage(input_tokens=600, output_tokens=80),
        finish_reason="stop",
    )


def _make_entry(
    *,
    entry_id: str,
    description: str,
    time_label: str = "占位时间",
    chapter_id: str = "ch-0001",
    is_flashback: bool = False,
    workspace_id: str = "ws-demo",
) -> TimelineEntry:
    return TimelineEntry(
        entry_id=entry_id,
        workspace_id=workspace_id,
        chapter_id=chapter_id,
        description=description,
        time_label=time_label,
        time_order=1,
        involved_characters=[],
        is_flashback=is_flashback,
    )


@pytest.fixture
def fake_prompts_dir(tmp_path: Path) -> Path:
    prompts_dir = tmp_path / "prompts"
    prompts_dir.mkdir()
    (prompts_dir / "timeline_entry_merger_v1.md").write_text(
        "FAKE_TIMELINE_ENTRY_MERGER_PROMPT", encoding="utf-8"
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
    existing: list[TimelineEntry] | None = None,
    chapter_id: str = "ch-0005",
    workspace_id: str = "ws-demo",
) -> TimelineEntryMergeResult:
    default_extracted = extracted if extracted is not None else [
        {"entry_id": "ts-new-1", "description": "新事件", "time_label": "新时间"}
    ]
    default_existing = existing if existing is not None else [
        _make_entry(entry_id="ts-existing-1", description="既有事件"),
    ]
    return merge_timeline_entries_with_existing(
        extracted=default_extracted,
        existing=default_existing,
        chapter_id=chapter_id,
        workspace_id=workspace_id,
        client=mock_client,
        model="qwen3.7-max",
        prompts_dir=fake_prompts_dir,
    )


class TestHappy:
    def test_no_merge(
        self, mock_client: MagicMock, fake_prompts_dir: Path
    ) -> None:
        result = _call(mock_client, fake_prompts_dir)
        assert result.merge_decisions == []
        assert result.skipped is False
        assert result.fail_open is False
        assert result.llm_calls_count == 1


class TestMerge:
    def test_cross_chapter_recall_same_event(
        self, mock_client: MagicMock, fake_prompts_dir: Path
    ) -> None:
        """跨章节回忆同事件:ch-5 倒叙描述 ch-1 同一事件。"""
        mock_client.complete.return_value = _make_response(
            verdict="merge",
            decisions=[
                {
                    "extracted_entry_id": "ts-mengjing-ch5-1",
                    "target_existing_entry_id": "ts-shen-zhou-mengjing-001",
                    "confidence": 0.9,
                    "reason": "ch-5 倒叙回顾 ch-1 沈舟梦境锦衣卫巡夜事件",
                }
            ],
            evidence="本章 1 条跨章节回忆同事件",
        )
        result = _call(
            mock_client,
            fake_prompts_dir,
            extracted=[
                {
                    "entry_id": "ts-mengjing-ch5-1",
                    "description": "沈舟梦境中作为锦衣卫巡夜",
                    "time_label": "明朝某日",
                }
            ],
            existing=[
                _make_entry(
                    entry_id="ts-shen-zhou-mengjing-001",
                    description="沈舟梦中化身锦衣卫巡夜",
                    time_label="明朝某日",
                    chapter_id="ch-0001",
                ),
            ],
        )
        assert len(result.merge_decisions) == 1
        d = result.merge_decisions[0]
        assert d.target_existing_entry_id == "ts-shen-zhou-mengjing-001"


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
        assert result.merge_decisions == []


class TestY2QuoteRepair:
    """v1.1 wave 6 / Y2:_parse_llm_payload 对字符串值内裸英文双引号的补救。

    根因 = LLM 在 evidence / reason 写裸 " 致 JSON 坏 → failed_open(B1 ch-6/12/25)。
    修法 = json.loads 失败后用状态机补救裸引号一次,修复后须 parse 成功才采用,
    否则回退原 failed_open;最坏(误判)只影响 evidence 文本,不影响 verdict/decisions。
    """

    def test_recovers_unescaped_quote_in_evidence(self) -> None:
        """evidence 含裸英文双引号 → 补救后解析成功(救回,不 failed_open)。"""
        raw = (
            '{"verdict": "no_merge", "decisions": [], '
            '"evidence": "本章提到"联系人"线索,与既有不同事件"}'
        )
        payload = _parse_llm_payload(raw_text=raw, model_used="test")
        assert payload.verdict == "no_merge"
        assert payload.decisions == []
        assert "联系人" in payload.evidence

    def test_valid_json_unaffected(self) -> None:
        """合法 JSON(无裸引号)走正常路径,补救不触发、结果不变。"""
        raw = (
            '{"verdict": "no_merge", "decisions": [], '
            '"evidence": "无同事件匹配"}'
        )
        payload = _parse_llm_payload(raw_text=raw, model_used="test")
        assert payload.evidence == "无同事件匹配"

    def test_repair_state_machine_escapes_inner_only(self) -> None:
        """状态机:字符串值内裸引号转义,结构引号(key/value 边界)保留。"""
        broken = '{"k": "a"b", "n": 1}'
        data = json.loads(_repair_unescaped_inner_quotes(broken))
        assert data["k"] == 'a"b'
        assert data["n"] == 1

    def test_unrepairable_still_fails_open(self) -> None:
        """无法修复的坏 JSON(结构截断)→ 仍抛 ValueError(failed_open 不回退)。"""
        raw = '{"verdict": "no_merge", "decisions": ['
        with pytest.raises(ValueError):
            _parse_llm_payload(raw_text=raw, model_used="test")


class TestInvalidIDs:
    def test_unknown_ids_filtered(
        self, mock_client: MagicMock, fake_prompts_dir: Path
    ) -> None:
        mock_client.complete.return_value = _make_response(
            verdict="merge",
            decisions=[
                {
                    "extracted_entry_id": "not-in-extracted",
                    "target_existing_entry_id": "ts-existing-1",
                    "confidence": 0.9,
                    "reason": "虚构",
                }
            ],
            evidence="LLM 编造",
        )
        result = _call(mock_client, fake_prompts_dir)
        assert result.merge_decisions == []
        assert result.fail_open is False


class TestTopKCandidates:
    def test_high_similarity_entry_selected(self) -> None:
        extracted = [
            {
                "entry_id": "ts-new",
                "description": "沈舟梦中战斗",
                "time_label": "明朝某日",
            }
        ]
        existing = [
            _make_entry(entry_id="ts-1", description="沈舟梦中与人战斗"),
            _make_entry(entry_id="ts-2", description="完全无关的事件 ABC XYZ"),
        ]
        result = select_top_k_existing_candidates(
            extracted=extracted, existing=existing
        )
        assert len(result) == 1
        assert result[0].entry_id == "ts-1"

    def test_low_similarity_all_filtered(self) -> None:
        extracted = [
            {"entry_id": "ts-new", "description": "全新事件", "time_label": "时间"}
        ]
        existing = [
            _make_entry(entry_id="ts-1", description="完全无关 ABC"),
            _make_entry(entry_id="ts-2", description="另一个 XYZ"),
        ]
        result = select_top_k_existing_candidates(
            extracted=extracted, existing=existing
        )
        assert result == []
