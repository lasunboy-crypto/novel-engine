"""正式 LLM JSON 合同的保守裸引号补救回归。"""

from __future__ import annotations

import json

import pytest
from structlog.testing import capture_logs

from src.core.agents._json_contract import loads_with_quote_repair


def test_repairs_only_unescaped_inner_ascii_quotes() -> None:
    broken = (
        '{"results":[{"index":1,"verdict":"intentional",'
        '"reason":"对话归属标记"林澈说"属基础叙事语法结构，非内容重复"}]}'
    )

    data = loads_with_quote_repair(
        broken,
        agent_name="cross_chapter_dedup_validator",
        model_used="qwen3.8-max",
    )

    assert data["results"][0]["reason"] == (
        '对话归属标记"林澈说"属基础叙事语法结构，非内容重复'
    )


def test_quote_repair_emits_attempt_and_applied_monitoring() -> None:
    broken = '{"summary":"他说"停下"，随后转身"}'

    with capture_logs() as logs:
        loads_with_quote_repair(
            broken,
            agent_name="chapter_summary_extractor",
            model_used="qwen3.8-max",
        )

    events = [entry["event"] for entry in logs]
    assert events == [
        "structured_json_quote_repair_attempted",
        "structured_json_quote_repair_applied",
    ]


@pytest.mark.parametrize(
    "broken",
    [
        "这不是 JSON",
        '{"results": [}',
        '{"results": [{"index": 1 "verdict": "intentional"}]}',
    ],
)
def test_structural_damage_still_fails_closed_to_caller(broken: str) -> None:
    with capture_logs() as logs, pytest.raises(json.JSONDecodeError):
        loads_with_quote_repair(
            broken,
            agent_name="test_agent",
            model_used="qwen3.8-max",
        )

    events = [entry["event"] for entry in logs]
    assert events == [
        "structured_json_quote_repair_attempted",
        "structured_json_quote_repair_failed",
    ]
