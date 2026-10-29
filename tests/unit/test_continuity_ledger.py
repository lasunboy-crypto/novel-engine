"""W3.5-R4 连续性小账本纯合同回归。"""

from __future__ import annotations

import pytest

from src.core.models.continuity_ledger import (
    ContinuityEvidenceRecord,
    ContinuityLedgerProvenance,
    compute_continuity_ledger_hash,
    make_evidence_record,
    sha256_text,
    validate_continuity_bridge,
    validate_continuity_projection,
    validate_evidence_records,
    validate_frozen_provenance,
)


_FINAL_TEXT = "镜面裂开一道血线。林衍正伸手按住裂口，血仍在往外渗。"
_ACTION = "林衍正伸手按住裂口"


def _evidence(
    *,
    field: str = "unfinished_actions",
    fact_text: str = _ACTION,
    quote: str = "林衍正伸手按住裂口",
    immediate: bool = True,
) -> ContinuityEvidenceRecord:
    return make_evidence_record(
        field=field,
        index=0,
        fact_text=fact_text,
        evidence_quote=quote,
        chapter_text=_FINAL_TEXT,
        is_immediate=immediate,
    )


def _ledger_hash(
    records: list[ContinuityEvidenceRecord],
    *,
    summary_text: str = "镜面裂开，林衍仍在止血。",
) -> str:
    return compute_continuity_ledger_hash(
        chapter_id="ch-0006",
        workspace_id="ws-r4",
        summary_text=summary_text,
        pov_character=None,
        scene_setting=None,
        key_events=[],
        character_interactions=[],
        unfinished_actions=[_ACTION],
        character_states=[],
        established_facts=[],
        revealed_mechanics=[],
        raw_tail=_FINAL_TEXT,
        source_version_id=3,
        source_text_hash=sha256_text(_FINAL_TEXT),
        evidence_records=records,
    )


def test_evidence_quote_must_be_exact_final_substring() -> None:
    record = _evidence()
    assert record.evidence_fingerprint == sha256_text(record.evidence_quote)

    with pytest.raises(ValueError, match="无法在 final.md 定位"):
        _evidence(quote="林衍伸手止住了血")


def test_evidence_coverage_and_fingerprint_are_revalidated() -> None:
    record = _evidence()
    validate_evidence_records(
        records=[record],
        chapter_text=_FINAL_TEXT,
        expected_facts={("unfinished_actions", 0): _ACTION},
    )

    with pytest.raises(ValueError, match="覆盖不完整"):
        validate_evidence_records(
            records=[],
            chapter_text=_FINAL_TEXT,
            expected_facts={("unfinished_actions", 0): _ACTION},
        )

    tampered = record.model_copy(update={"evidence_fingerprint": "0" * 64})
    with pytest.raises(ValueError, match="fingerprint 不匹配"):
        validate_evidence_records(
            records=[tampered],
            chapter_text=_FINAL_TEXT,
            expected_facts={("unfinished_actions", 0): _ACTION},
        )


def test_ledger_hash_is_stable_and_changes_with_semantics() -> None:
    records = [_evidence()]
    first = _ledger_hash(records)
    assert first == _ledger_hash(records)
    assert first != _ledger_hash(records, summary_text="摘要被改写")


def test_frozen_provenance_requires_exact_current_ledger() -> None:
    current = ContinuityLedgerProvenance(
        chapter_id="ch-0006",
        source_version_id=3,
        source_text_hash=sha256_text(_FINAL_TEXT),
        ledger_hash=_ledger_hash([_evidence()]),
    )
    validate_frozen_provenance(frozen=current, current=current)

    with pytest.raises(ValueError, match="缺少"):
        validate_frozen_provenance(frozen=None, current=current)

    stale = current.model_copy(update={"source_version_id": 2})
    with pytest.raises(ValueError, match="已陈旧"):
        validate_frozen_provenance(frozen=stale, current=current)


def test_f_ch6_immediate_crisis_requires_bridge_for_time_jump() -> None:
    records = [_evidence()]
    with pytest.raises(ValueError, match="continuity_bridge 为空"):
        validate_continuity_bridge(
            evidence_records=records,
            time_gap="数小时后",
            scene_continuation="switch",
            continuity_bridge="",
        )

    with pytest.raises(ValueError, match="未逐字承接"):
        validate_continuity_bridge(
            evidence_records=records,
            time_gap="数小时后",
            scene_continuation="switch",
            continuity_bridge="数小时后，伤口已经处理完毕。",
        )

    validate_continuity_bridge(
        evidence_records=records,
        time_gap="数小时后",
        scene_continuation="switch",
        continuity_bridge=f"先承接“{_ACTION}”，确认止血后才切到数小时后。",
    )


def test_same_scene_immediate_continuation_needs_no_extra_bridge() -> None:
    validate_continuity_bridge(
        evidence_records=[_evidence()],
        time_gap="紧接上一刻",
        scene_continuation="same",
        continuity_bridge="",
    )


def test_existing_previous_ledger_rejects_first_chapter_time_gap() -> None:
    with pytest.raises(ValueError, match="time_gap 不得使用首章字面"):
        validate_continuity_bridge(
            evidence_records=[],
            time_gap="开篇",
            scene_continuation="switch",
            continuity_bridge="",
        )


def test_card_continuity_projection_must_match_ledger_exactly() -> None:
    kwargs = {
        "ledger_raw_final_tail": _FINAL_TEXT,
        "ledger_unfinished_actions": [_ACTION],
        "ledger_pov_character": "lin-yan",
        "card_prev_chapter_tail": _FINAL_TEXT,
        "card_unfinished_actions": [_ACTION],
        "card_pov_from": "lin-yan",
    }
    validate_continuity_projection(**kwargs)

    with pytest.raises(ValueError, match="unfinished_actions"):
        validate_continuity_projection(
            **{**kwargs, "card_unfinished_actions": []}
        )
