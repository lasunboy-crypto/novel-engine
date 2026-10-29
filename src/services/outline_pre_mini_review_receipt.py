"""Source-bound proof that the five pre-mini-plan checks already passed.

The recovery path reads original, immutable request/response captures. It never
changes outline text or substitutes a new model verdict for a missing response.
"""

from __future__ import annotations

import hashlib
import json
from pathlib import Path
from typing import Any

from src.core.agents.outline_consistency_validator import (
    _SPLIT_STAGES,
    _SplitReviewPayload,
    split_review_sources,
    validate_split_review_payload,
)
from src.core.agents.outline_review_packets import digest
from src.core.llm.base import Message
from src.core.llm.prompt_hash import hash_prompt, render_prompt_text

VERSION = "pre-mini-five-review-v1"


def make_receipt(source: dict[str, Any], gaps: list[str], *, evidence: list[dict[str, Any]]) -> dict[str, Any]:
    return {
        "version": VERSION,
        "source_hash": digest(source),
        "review_stages": list(_SPLIT_STAGES),
        "coverage_gaps": gaps,
        "evidence": evidence,
    }


def read_receipt(path: Path, source: dict[str, Any]) -> list[str] | None:
    if not path.is_file():
        return None
    receipt = json.loads(path.read_text(encoding="utf-8"))
    if (receipt.get("version") != VERSION
            or receipt.get("review_stages") != list(_SPLIT_STAGES)
            or not isinstance(receipt.get("coverage_gaps"), list)
            or not all(isinstance(gap, str) for gap in receipt["coverage_gaps"])
            or not isinstance(receipt.get("evidence"), list)
            or len(receipt["evidence"]) != len(_SPLIT_STAGES)
            or [item.get("stage") if isinstance(item, dict) else None
                for item in receipt["evidence"]] != list(_SPLIT_STAGES)):
        raise ValueError("五项审核完成凭据损坏")
    if receipt.get("source_hash") != digest(source):
        return None
    return receipt["coverage_gaps"]


def _archived_messages(path: Path) -> list[Message]:
    text = path.read_text(encoding="utf-8")
    parts = text.split("\n\n---\n\n## role: user\n\n")
    if len(parts) != 2 or not parts[0].startswith("## role: system\n\n"):
        raise ValueError("审核请求归档格式无效")
    messages = [
        Message(role="system", content=parts[0][len("## role: system\n\n"):]),
        Message(role="user", content=parts[1]),
    ]
    if render_prompt_text(messages) != text or hash_prompt(messages) != path.stem:
        raise ValueError("审核请求归档哈希不符")
    return messages


def _captured_response(path: Path) -> str:
    summary = json.loads(path.with_suffix(".summary.json").read_text(encoding="utf-8"))
    headers = json.loads(path.with_suffix(".headers.json").read_text(encoding="utf-8"))
    body = path.with_suffix(".body").read_bytes()
    if (summary.get("sha256") != hashlib.sha256(body).hexdigest()
            or summary.get("bytes") != len(body)
            or summary.get("transport_error_type") is not None
            or headers.get("status_code") != 200):
        raise ValueError("审核原始响应不完整")
    answer = ""
    finish_reason = None
    usage = None
    done = False
    for line in body.decode("utf-8").splitlines():
        if not line.startswith("data: "):
            continue
        event = line[6:]
        if event == "[DONE]":
            done = True
            continue
        chunk = json.loads(event)
        if chunk.get("error"):
            raise ValueError("审核原始响应含 Provider 错误")
        choices = chunk.get("choices") or []
        if choices:
            item = choices[0]
            answer += item.get("delta", {}).get("content") or ""
            finish_reason = item.get("finish_reason") or finish_reason
        usage = chunk.get("usage") or usage
    if not done or finish_reason != "stop" or not usage:
        raise ValueError("审核原始响应未正常结束或缺用量")
    return answer


def recover_from_captures(
    source: dict[str, Any], *, capture_dir: Path, archive_dir: Path,
) -> dict[str, Any]:
    """Recover only a consecutive, complete five-stage PASS from raw evidence."""
    archived: dict[str, tuple[str, list[Message]]] = {}
    for path in archive_dir.glob("*.txt"):
        messages = _archived_messages(path)
        wire = [{"role": msg.role, "content": msg.content} for msg in messages]
        wire_hash = hashlib.sha256(json.dumps(
            wire, ensure_ascii=False, sort_keys=True,
        ).encode()).hexdigest()
        archived[wire_hash] = (path.stem, messages)

    requests = sorted(capture_dir.glob("*.request.json"))
    for first in range(len(requests) - len(_SPLIT_STAGES) + 1):
        results: list[_SplitReviewPayload] = []
        evidence: list[dict[str, Any]] = []
        for offset, stage in enumerate(_SPLIT_STAGES):
            request_path = requests[first + offset]
            request = json.loads(request_path.read_text(encoding="utf-8"))
            record = archived.get(request.get("messages_sha256", ""))
            if record is None:
                break
            prompt_hash, messages = record
            try:
                user_text = messages[1].content
                packet, _ = json.JSONDecoder().raw_decode(user_text[user_text.index("{"):])
                data = packet["data"]
                visible = split_review_sources(stage, source)
                if (request.get("method") != "POST"
                        or request.get("host") != "api.deepseek.com"
                        or request.get("path") != "/chat/completions"
                        or request.get("generation", {}).get("model") != "deepseek-flash"
                        or data.get("review_stage") != stage
                        or data.get("source_hash") != digest(visible)
                        or any(data.get(key) != value for key, value in visible.items())):
                    break
                prefix = request_path.with_name(request_path.name.removesuffix(".request.json"))
                response = _captured_response(prefix)
                result = validate_split_review_payload(
                    _SplitReviewPayload.model_validate_json(response),
                    stage=stage, source=source,
                )
                if result.outcome != "compatible":
                    break
            except (OSError, ValueError, KeyError, TypeError):
                break
            results.append(result)
            evidence.append({
                "stage": stage, "prompt_hash": prompt_hash,
                "capture": request_path.stem.removesuffix(".request"),
                "response_sha256": hashlib.sha256(response.encode()).hexdigest(),
            })
        if len(results) == len(_SPLIT_STAGES):
            return make_receipt(
                source, [gap for result in results for gap in result.coverage_gaps],
                evidence=evidence,
            )
    raise ValueError("没有找到与当前四段原文完全一致、五项齐全的原始通过证据")
