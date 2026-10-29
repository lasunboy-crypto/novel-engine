"""Bind length observations to the Writer implementation that produced them."""

from __future__ import annotations

import hashlib
import json
from pathlib import Path

from src.core.models.run_model_manifest import RunModelManifest


def writer_calibration_contract_hash(
    manifest: RunModelManifest, *, prompts_dir: Path | None = None
) -> str:
    if manifest.pipeline_contract != "creative_delta_v6":
        raise ValueError("Writer calibration binding requires the explicit v6 pipeline")
    root = Path(__file__).resolve().parents[3]
    prompt_root = prompts_dir or root / "prompts"
    sources = {
        path: hashlib.sha256((root / path).read_bytes()).hexdigest()
        for path in (
            "src/core/agents/writer.py",
            "src/core/agents/writer_draft_view.py",
            "src/core/agents/writer_salience.py",
        )
    }
    sources["writer_prompt"] = hashlib.sha256(
        (prompt_root / "writer_draft_hy3_v5.md").read_bytes()
    ).hexdigest()
    behavior = manifest.prose_generation or manifest.tiers["medium"]
    payload = {
        "model": manifest.exact_model_id,
        "model_revision": manifest.model_revision,
        "pipeline": manifest.pipeline_contract,
        "writer_generation": behavior.model_dump(mode="json"),
        "sources": sources,
    }
    if manifest.handoff_format is not None:
        payload["handoff_format"] = manifest.handoff_format
        sources["structured_handoff"] = hashlib.sha256(
            (root / "src/core/agents/structured_handoff.py").read_bytes()
        ).hexdigest()
    return hashlib.sha256(json.dumps(payload, sort_keys=True).encode()).hexdigest()
