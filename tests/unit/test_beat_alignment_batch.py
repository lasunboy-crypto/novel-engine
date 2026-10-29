"""整卷节拍核对的调用数、覆盖和技术失败边界（零 Provider）。"""

from __future__ import annotations

import json
from pathlib import Path
from unittest.mock import MagicMock

import pytest
from pydantic import BaseModel

from src.core.agents import outline_builder
from src.core.agents.beat_alignment_validator import (
    BeatAlignmentBatchTechnicalError,
    BeatAlignmentValidationResult,
    validate_beat_alignment_batch,
)
from src.core.llm.base import LLMClient, LLMResponse, TokenUsage
from src.core.llm.mock_client import MockLLMClient
from tests.unit.test_outline_builder import _make_post_v6_parallel_volume


def _entries(count: int):
    volume, items = _make_post_v6_parallel_volume(count)
    return volume, items, [
        (item, next(
            beat for arc in volume.arcs if arc.arc_id == item.arc_id
            for beat in arc.beats
            if beat.chapter_range[0] <= item.chapter_number <= beat.chapter_range[1]
        ))
        for item in items
    ]


def _response(results: list[dict]) -> LLMResponse:
    return LLMResponse(
        content=json.dumps({"results": results}, ensure_ascii=False),
        model_used="mock-default",
        usage=TokenUsage(input_tokens=100, output_tokens=100),
        finish_reason="stop",
    )


def _rows(entries):
    return [{
        "chapter_id": item.chapter_id,
        "beat_id": beat.beat_id,
        "verdict": "pass",
        "must_advance_advanced": beat.must_advance[:1],
        "must_advance_missing": [],
        "forbidden_advance_violated": [],
        "summary_in_range": True,
        "evidence": "本章事件推进所属节拍",
        "chapter_evidence_quote": item.one_line_event[:8],
        "beat_evidence_quote": beat.summary[:8],
        "info_increment_substantive": True,
        "info_increment_issue": "",
    } for item, beat in entries]


def test_twelve_chapters_one_request(monkeypatch: pytest.MonkeyPatch) -> None:
    _, _, entries = _entries(12)
    client = MockLLMClient()
    original = client.complete
    calls: list[int] = []

    def count(messages, **kwargs):
        calls.append(1)
        return original(messages, **kwargs)

    monkeypatch.setattr(client, "complete", count)
    results = validate_beat_alignment_batch(
        entries=entries, client=client, model="mock-default",
        prompts_dir=Path("prompts"),
    )
    assert len(calls) == 1
    assert list(results) == [item.chapter_id for item, _ in entries]
    assert all(result.passed for result in results.values())


def test_json_handoff_keeps_batch_structured() -> None:
    _, _, entries = _entries(12)

    class JsonMock(MockLLMClient):
        creative_delta_capability = "creative_delta_v6"
        handoff_format = "json"

    client = JsonMock()
    results = validate_beat_alignment_batch(
        entries=entries, client=client, model="mock-default",
        prompts_dir=Path("prompts"),
    )
    assert len(results) == 12


def test_long_volume_is_bounded_and_every_group_is_checked() -> None:
    _, _, entries = _entries(25)
    client = MagicMock(spec=LLMClient)
    groups = [entries[:12], entries[12:24], entries[24:]]
    client.complete.side_effect = [_response(_rows(group)) for group in groups]
    results = validate_beat_alignment_batch(
        entries=entries, client=client, model="mock-default",
        prompts_dir=Path("prompts"),
    )
    assert list(results) == [item.chapter_id for item, _ in entries]
    assert client.complete.call_count == 3


def test_uncertain_result_requires_single_chapter_recheck() -> None:
    _, _, entries = _entries(12)
    rows = _rows(entries)
    rows[6]["verdict"] = "uncertain"
    client = MagicMock(spec=LLMClient)
    client.complete.return_value = _response(rows)
    results = validate_beat_alignment_batch(
        entries=entries, client=client, model="mock-default",
        prompts_dir=Path("prompts"),
    )
    assert not results["ch-0007"].passed
    assert results["ch-0007"].uncertain


@pytest.mark.parametrize("defect", ["missing", "duplicate", "foreign_must", "invented_quote"])
def test_incomplete_or_unbound_batch_never_passes(defect: str) -> None:
    _, _, entries = _entries(12)
    rows = _rows(entries)
    if defect == "missing":
        rows.pop()
    elif defect == "duplicate":
        rows[-1] = rows[0]
    else:
        if defect == "foreign_must":
            rows[0]["must_advance_advanced"] = ["别章的任务"]
        else:
            rows[0]["chapter_evidence_quote"] = "本章设计里不存在的引文"
    client = MagicMock(spec=LLMClient)
    client.complete.return_value = _response(rows)
    with pytest.raises(BeatAlignmentBatchTechnicalError):
        validate_beat_alignment_batch(
            entries=entries, client=client, model="mock-default",
            prompts_dir=Path("prompts"),
        )
    assert client.complete.call_count == 1


def test_provider_cap_failure_does_not_fail_open() -> None:
    _, _, entries = _entries(12)
    client = MagicMock(spec=LLMClient)
    client.complete.side_effect = RuntimeError("物理请求额度用完")
    with pytest.raises(BeatAlignmentBatchTechnicalError, match="额度用完"):
        validate_beat_alignment_batch(
            entries=entries, client=client, model="mock-default",
            prompts_dir=Path("prompts"),
        )
    assert client.complete.call_count == 1


def test_batch_technical_failure_does_not_regenerate_mini_plan() -> None:
    class Draft(BaseModel):
        value: str

    client = MagicMock(spec=LLMClient)
    client.complete.return_value = LLMResponse(
        content='{"value":"ok"}', model_used="mock-default",
        usage=TokenUsage(input_tokens=10, output_tokens=10),
        finish_reason="stop",
    )

    def unavailable(_draft):
        raise BeatAlignmentBatchTechnicalError("额度用完")

    with pytest.raises(BeatAlignmentBatchTechnicalError, match="额度用完"):
        outline_builder._run_stage(
            stage="mini_plan", system_text="test", user_text="test",
            output_model=Draft, router=None, client=client,
            model="mock-default", max_tokens=1000, temperature=0.3,
            post_validate=unavailable,
            non_retryable_exceptions=(BeatAlignmentBatchTechnicalError,),
        )
    assert client.complete.call_count == 1


def test_only_batch_disputed_chapter_gets_individual_recheck(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    volume, items, _ = _entries(12)
    monkeypatch.setattr(outline_builder, "uses_experimental_outline", lambda *_: True)
    monkeypatch.setattr(
        outline_builder, "validate_beat_alignment_batch",
        lambda **_: {
            item.chapter_id: BeatAlignmentValidationResult(
                passed=item.chapter_id != "ch-0007",
                evidence="第七章疑似提前演出后章",
            )
            for item in items
        },
    )
    rechecked: list[str] = []

    def recheck(**kwargs):
        rechecked.append(kwargs["chapter_id"])
        return BeatAlignmentValidationResult(passed=True, evidence="留白可解释")

    monkeypatch.setattr(outline_builder, "validate_beat_alignment", recheck)
    outline_builder._validate_mini_plan_beat_alignment(
        items=items, volume=volume, router=None, client=None, model=None,
        prompts_dir=None,
    )
    assert rechecked == ["ch-0007"]


def test_disputed_recheck_failure_cannot_pass(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    volume, items, _ = _entries(12)
    monkeypatch.setattr(outline_builder, "uses_experimental_outline", lambda *_: True)
    monkeypatch.setattr(
        outline_builder, "validate_beat_alignment_batch",
        lambda **_: {
            item.chapter_id: BeatAlignmentValidationResult(
                passed=item.chapter_id != "ch-0007",
                evidence="第七章疑似提前演出后章",
            )
            for item in items
        },
    )
    monkeypatch.setattr(
        outline_builder, "validate_beat_alignment",
        lambda **_: BeatAlignmentValidationResult(
            passed=True, fail_open=True, evidence="额度用完",
        ),
    )
    with pytest.raises(BeatAlignmentBatchTechnicalError, match="定点复核调用失败"):
        outline_builder._validate_mini_plan_beat_alignment(
            items=items, volume=volume, router=None, client=None, model=None,
            prompts_dir=None,
        )


def test_confirmed_semantic_failure_goes_back_to_generator(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    volume, items, _ = _entries(12)
    monkeypatch.setattr(outline_builder, "uses_experimental_outline", lambda *_: True)
    monkeypatch.setattr(
        outline_builder, "validate_beat_alignment_batch",
        lambda **_: {
            item.chapter_id: BeatAlignmentValidationResult(
                passed=item.chapter_id != "ch-0007", evidence="疑似越界",
            )
            for item in items
        },
    )
    monkeypatch.setattr(
        outline_builder, "validate_beat_alignment",
        lambda **_: BeatAlignmentValidationResult(
            passed=False,
            forbidden_advance_violated=["不得跳过高潮"],
            evidence="明确提前发生", llm_calls_count=1,
        ),
    )
    with pytest.raises(ValueError, match="ch-0007"):
        outline_builder._validate_mini_plan_beat_alignment(
            items=items, volume=volume, router=None, client=None, model=None,
            prompts_dir=None,
        )


def test_experimental_chapter_without_owning_beat_is_rejected(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    volume, items, _ = _entries(12)
    monkeypatch.setattr(outline_builder, "uses_experimental_outline", lambda *_: True)
    items[0] = items[0].model_copy(update={"arc_id": volume.arcs[-1].arc_id})
    with pytest.raises(ValueError, match="找不到覆盖本章的节拍"):
        outline_builder._validate_mini_plan_beat_alignment(
            items=items, volume=volume, router=None, client=None, model=None,
            prompts_dir=None,
        )
