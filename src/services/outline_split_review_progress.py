"""Preserve generated pre-mini material and each valid part of its review.

Only compatible, source-bound verdicts are reusable. Historical recovery reads
product archives tied to succeeded ledger attempts, never substitutes a verdict.
"""

from __future__ import annotations

import json
import sqlite3
from pathlib import Path
from typing import Any

from src.core.agents.outline_consistency_validator import (
    _SPLIT_STAGES,
    _SplitReviewPayload,
    SplitReviewStage,
    split_review_sources,
    validate_split_review_payload,
)
from src.core.agents.outline_review_packets import digest
from src.services.outline_pre_mini_review_receipt import _archived_messages


def _write(path: Path, value: dict[str, Any]) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    temporary = path.with_suffix(".tmp")
    temporary.write_text(
        json.dumps(value, ensure_ascii=False, indent=2), encoding="utf-8"
    )
    temporary.replace(path)


def _expanded_archive_data(data: dict[str, Any]) -> dict[str, Any]:
    """Reverse only the two verified user-request references in wire archives."""
    from src.core.models.narrative import UserNarrativeRequest

    original = data.get("user_request")
    request = (
        UserNarrativeRequest.model_validate(original) if original is not None else None
    )

    def expand(value: Any) -> Any:
        if isinstance(value, dict):
            if set(value) == {"data_path"}:
                if request is None:
                    raise ValueError("归档用户原文引用没有可验证来源")
                if value["data_path"] == "/data/user_request":
                    return request.model_dump(mode="json")
                if value["data_path"] == "/data/user_request/premise":
                    return request.premise
                raise ValueError("归档包含未知资料引用，不能猜测还原")
            return {key: expand(item) for key, item in value.items()}
        if isinstance(value, list):
            return [expand(item) for item in value]
        return value

    return expand(data)


class OutlineSplitReviewProgress:
    def __init__(
        self, directory: Path, input_binding: dict[str, Any] | None = None
    ) -> None:
        self.directory = directory
        self.input_binding = input_binding or {}

    def prepare(self, source: dict[str, Any]) -> None:
        _write(
            self.directory / "candidate.json",
            {
                "version": 1,
                "source": source,
                "source_hash": digest(source),
                "input_binding": self.input_binding,
            },
        )

    def load(
        self, stage: SplitReviewStage, source: dict[str, Any]
    ) -> _SplitReviewPayload | None:
        visible = split_review_sources(stage, source)
        path = self.directory / f"{stage}-{digest(visible)}.json"
        if not path.is_file():
            return None
        record = json.loads(path.read_text(encoding="utf-8"))
        if (
            record.get("version") != 1
            or record.get("payload_hash") != digest(record.get("payload"))
            or record.get("source_hash") != digest(visible)
        ):
            raise ValueError("已通过的分项审核凭据损坏，停止而不重复调用")
        result = validate_split_review_payload(
            _SplitReviewPayload.model_validate(record["payload"]),
            stage=stage,
            source=source,
        )
        if result.outcome != "compatible":
            raise ValueError("未通过的审核不能作为续跑通过凭据")
        return result

    def save(
        self,
        stage: SplitReviewStage,
        source: dict[str, Any],
        payload: _SplitReviewPayload,
        evidence: dict[str, Any],
    ) -> None:
        result = validate_split_review_payload(payload, stage=stage, source=source)
        if result.outcome != "compatible":
            raise ValueError("仅保存同源 compatible 通过凭据")
        visible = split_review_sources(stage, source)
        value = result.model_dump(mode="json")
        _write(
            self.directory / f"{stage}-{digest(visible)}.json",
            {
                "version": 1,
                "source_hash": digest(visible),
                "payload": value,
                "payload_hash": digest(value),
                "evidence": evidence,
            },
        )

    def candidate(
        self,
        parent: dict[str, Any],
        *,
        premise: str,
        genre: str,
        total_volumes: int,
        chapters_per_volume: int,
        planning_mode: str,
    ) -> dict[str, Any] | None:
        path = self.directory / "candidate.json"
        if not path.is_file():
            return None
        record = json.loads(path.read_text(encoding="utf-8"))
        source = record.get("source")
        if (
            record.get("version") != 1
            or not isinstance(source, dict)
            or digest(source) != record.get("source_hash")
            or record.get("input_binding") != self.input_binding
        ):
            raise ValueError("待审核的卷规划恢复材料损坏")
        if (
            parent.get("volume_plans") is not None
            or parent.get("mini_plan") is not None
        ):
            return None
        if (
            source.get("premise") != premise
            or source.get("genre") != genre
            or any(
                source.get(k) != parent.get(k) for k in ("world", "characters", "plot")
            )
        ):
            raise ValueError("上游原文或建书要求已变化，不能续接旧审核")
        scope = (source.get("world", {}).get("story_intent") or {}).get(
            "planning_scope"
        )
        if (
            not isinstance(scope, dict)
            or scope.get("mode") != planning_mode
            or scope.get("total_volumes") != total_volumes
            or scope.get("chapters_per_volume") != chapters_per_volume
        ):
            raise ValueError("本次规划范围与原恢复材料不一致")
        return source

    @staticmethod
    def assert_idle(workspace: Path) -> None:
        database = workspace / "data/novel.db"
        if not database.is_file():
            return
        with sqlite3.connect(
            database.resolve().as_uri() + "?mode=ro", uri=True
        ) as connection:
            if connection.execute(
                "select count(*) from attempt_ledger where attempt_kind='provider' "
                "and (status='started' or usage_status!='known')"
            ).fetchone()[0]:
                raise ValueError("仍有未完成或用量未知的请求，不能恢复重发")
            if connection.execute("select count(*) from chapters").fetchone()[0]:
                raise ValueError("已有章节，不能从上游恢复点重新进入")

    def recover_archives(
        self, workspace: Path, parent: dict[str, Any], **scope: Any
    ) -> dict[str, Any] | None:
        """Recover a technical interruption before any canonical volume/mini exists."""
        if (
            parent.get("volume_plans") is not None
            or parent.get("mini_plan") is not None
            or any(parent.get(k) is None for k in ("world", "characters", "plot"))
        ):
            return None
        database, task_db = workspace / "data/novel.db", workspace.parent / "_tasks.db"
        if not database.is_file() or not task_db.is_file():
            return None
        with sqlite3.connect(
            database.resolve().as_uri() + "?mode=ro", uri=True
        ) as connection:
            connection.row_factory = sqlite3.Row
            attempts = [
                dict(r)
                for r in connection.execute(
                    "select * from attempt_ledger where attempt_kind='provider' order by created_at"
                )
            ]
            if not attempts:
                return None
            if any(
                r["status"] == "started" or r["usage_status"] != "known"
                for r in attempts
            ):
                raise ValueError("仍有未完成或用量未知的请求，不能恢复重发")
            if connection.execute("select count(*) from chapters").fetchone()[0]:
                raise ValueError("已有章节，不能从上游恢复点重新进入")
        last = attempts[-1]
        with sqlite3.connect(
            task_db.resolve().as_uri() + "?mode=ro", uri=True
        ) as connection:
            connection.row_factory = sqlite3.Row
            task = connection.execute(
                "select * from task_records where task_id=?", (last["task_id"],)
            ).fetchone()
        if (
            task is None
            or task["type"] != "outline_generate"
            or task["status"] != "failed"
            or task["cancel_requested"]
            or task["workspace_id"] != workspace.name
            or last["agent_name"] != "outline_consistency_validator"
        ):
            return None
        relevant = {
            r["provider_request_id"]: r
            for r in attempts
            if r["task_id"] == last["task_id"]
        }
        records = []
        for path in (workspace / "archives/responses").glob("*/*.json"):
            response = json.loads(path.read_text(encoding="utf-8"))
            attempt = relevant.get(response.get("provider_request_id"))
            if attempt is None:
                continue
            if (
                attempt["status"] != "succeeded"
                or attempt["model"] != "deepseek-flash"
                or response.get("model_used") != attempt["model"]
                or response.get("finish_reason") not in {"stop", "end_turn"}
                or response.get("usage", {}).get("input_tokens")
                != attempt["input_tokens"]
                or response.get("usage", {}).get("output_tokens")
                != attempt["output_tokens"]
            ):
                raise ValueError("归档响应与已成功账本不一致")
            prompt_hash = response["prompt_hash"]
            messages = _archived_messages(
                workspace / f"archives/prompts/{prompt_hash}.txt"
            )
            packet = json.loads(messages[1].content)
            data = _expanded_archive_data(packet.get("data", {}))
            records.append((attempt, response, data))
        records.sort(key=lambda row: row[0]["created_at"])
        if not records or records[-1][0]["attempt_id"] != last["attempt_id"]:
            raise ValueError("最后一次失败审核的原始响应缺失")
        final_data = records[-1][2]
        if final_data.get("review_stage") not in {"volume_plans", "cross"}:
            return None
        source = {
            k: final_data[k]
            for k in ("world", "characters", "plot", "volume_plans", "premise", "genre")
        }
        if final_data.get("source_hash") != digest(
            split_review_sources(final_data["review_stage"], source)
        ):
            raise ValueError("原始审核请求与完整候选来源不符")
        try:
            validate_split_review_payload(
                _SplitReviewPayload.model_validate_json(records[-1][1]["content"]),
                stage=final_data["review_stage"],
                source=source,
            )
        except ValueError:
            pass  # Invalid output is retried, never promoted to compatible.
        else:
            raise ValueError("原审核已有有效裁决，须走定点修复而非技术重试")
        # Persist no generated material until the caller's source/range is proven.
        probe = OutlineSplitReviewProgress(self.directory, self.input_binding)
        record = {
            "version": 1,
            "source": source,
            "source_hash": digest(source),
            "input_binding": self.input_binding,
        }
        for key in ("world", "characters", "plot"):
            if source[key] != parent[key]:
                raise ValueError("原始候选与当前上游原文不同")
        planned = (source["world"].get("story_intent") or {}).get(
            "planning_scope"
        ) or {}
        if (
            source["premise"] != scope["premise"]
            or source["genre"] != scope["genre"]
            or planned.get("mode") != scope["planning_mode"]
            or planned.get("total_volumes") != scope["total_volumes"]
            or planned.get("chapters_per_volume") != scope["chapters_per_volume"]
        ):
            raise ValueError("原始候选与当前建书范围不一致")
        # A generated volume request also binds the original curve and count.
        generated = [
            data
            for _, _, data in records
            if "volume_contract" in data and "original_volume" not in data
        ]
        if (
            not generated
            or generated[-1].get("tension_template")
            != self.input_binding["tension_template"]
            or planned.get("reference_chapter_words")
            != self.input_binding["reference_chapter_words"]
            or generated[-1].get("total_volumes") != scope["total_volumes"]
            or generated[-1].get("chapters_per_volume") != scope["chapters_per_volume"]
        ):
            raise ValueError("原卷规划的生成范围或张力模板无法确认")
        valid = []
        for attempt, response, data in records:
            stage = data.get("review_stage")
            if stage not in _SPLIT_STAGES:
                continue
            visible = split_review_sources(stage, source)
            if data.get("source_hash") != digest(visible) or any(
                data.get(k) != v for k, v in visible.items()
            ):
                continue
            try:
                payload = validate_split_review_payload(
                    _SplitReviewPayload.model_validate_json(response["content"]),
                    stage=stage,
                    source=source,
                )
            except ValueError:
                continue
            if payload.outcome == "compatible":
                valid.append(
                    (
                        stage,
                        payload,
                        {
                            "source": "product_archive",
                            "attempt_id": attempt["attempt_id"],
                            "prompt_hash": response["prompt_hash"],
                            "provider_request_id": response["provider_request_id"],
                        },
                    )
                )
        if {stage for stage, _, _ in valid} != set(
            _SPLIT_STAGES[: _SPLIT_STAGES.index(final_data["review_stage"])]
        ):
            raise ValueError("原始已通过审核不是完整连续前缀")
        _write(probe.directory / "candidate.json", record)
        for stage, payload, evidence in valid:
            probe.save(stage, source, payload, evidence)
        return probe.candidate(
            parent, **{k: v for k, v in scope.items() if k != "tension_template"}
        )
