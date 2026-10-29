"""Already paid five-stage reviews survive an interrupted mini-plan."""

from __future__ import annotations

import hashlib
import json
from pathlib import Path

import pytest

from src.core.agents.outline_consistency_validator import (
    _SPLIT_STAGES, split_review_messages, split_review_sources,
)
from src.core.agents.outline_review_packets import digest
from src.core.llm.prompt_hash import hash_prompt, render_prompt_text
from src.services.outline_pre_mini_review_receipt import (
    read_receipt, recover_from_captures,
)


def _source() -> dict:
    return {"world": {"setting": "青岚山"}, "characters": {"characters": []},
            "plot": {"main_phases": []}, "volume_plans": {"volumes": []},
            "premise": "药农查明灵田枯败", "genre": "xianxia"}


def _capture(directory: Path, archive: Path, *, source: dict,
             bad_stage: str | None = None) -> None:
    directory.mkdir()
    archive.mkdir()
    for number, stage in enumerate(_SPLIT_STAGES, start=1):
        messages = split_review_messages(stage, source, prompts_dir=Path("prompts"))
        (archive / f"{hash_prompt(messages)}.txt").write_text(render_prompt_text(messages))
        wire = [{"role": msg.role, "content": msg.content} for msg in messages]
        request = {"method": "POST", "host": "api.deepseek.com",
                   "path": "/chat/completions",
                   "messages_sha256": hashlib.sha256(json.dumps(
                       wire, ensure_ascii=False, sort_keys=True,
                   ).encode()).hexdigest(),
                   "generation": {"model": "deepseek-flash"}}
        prefix = directory / f"{number:02d}"
        (directory / f"{number:02d}.request.json").write_text(json.dumps(request))
        visible = split_review_sources(stage, source)
        response = {"review_stage": stage, "source_hash": digest(visible),
                    "outcome": "unknown" if bad_stage == stage else "compatible",
                    "reviewed_sections": list(visible.keys())[:4] if stage == "cross" else [stage],
                    "contradictions": [], "coverage_gaps": [],
                    "requested_paths": [], "evidence": "原文已核对",
                    "repair_scope": "unclear" if bad_stage == stage else "none"}
        body = ("data: " + json.dumps({"choices": [{"delta": {
            "content": json.dumps(response, ensure_ascii=False)},
            "finish_reason": "stop"}], "usage": {"total_tokens": 100}},
            ensure_ascii=False) + "\n\ndata: [DONE]\n").encode()
        prefix.with_suffix(".body").write_bytes(body)
        prefix.with_suffix(".summary.json").write_text(json.dumps({
            "sha256": hashlib.sha256(body).hexdigest(), "bytes": len(body),
            "transport_error_type": None,
        }))
        prefix.with_suffix(".headers.json").write_text('{"status_code":200}')


def test_recover_exact_five_passes_without_provider(tmp_path: Path) -> None:
    source = _source()
    raw, archive = tmp_path / "raw", tmp_path / "archive"
    _capture(raw, archive, source=source)
    receipt = recover_from_captures(source, capture_dir=raw, archive_dir=archive)
    path = tmp_path / "receipt.json"
    path.write_text(json.dumps(receipt))
    assert read_receipt(path, source) == []
    changed = {**source, "premise": "前提已经改变"}
    assert read_receipt(path, changed) is None


def test_unknown_stage_cannot_be_recovered(tmp_path: Path) -> None:
    source = _source()
    raw, archive = tmp_path / "raw", tmp_path / "archive"
    _capture(raw, archive, source=source, bad_stage="plot")
    with pytest.raises(ValueError, match="没有找到"):
        recover_from_captures(source, capture_dir=raw, archive_dir=archive)
