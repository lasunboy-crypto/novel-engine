"""Resume a known empty must response by consuming source-bound response archives.

No verdict is synthesized. The normal agents parse the original responses again;
only the existing must schema-feedback attempt can require another physical call.
"""

from __future__ import annotations

import json
import re
from dataclasses import dataclass, field
from datetime import UTC, datetime
from threading import Lock
from typing import Any

from sqlalchemy import select
from sqlalchemy.orm import Session

from src.core.db.models import (
    AttemptLedgerORM,
    ChapterORM,
    ChapterVersionORM,
    PipelineRunORM,
    ReviewResultORM,
)
from src.core.llm.base import LLMClient, LLMResponse, Message
from src.core.llm.local_json_contract import (
    LocalJsonResponseViolation,
    local_contract_from_params,
    unwrap_json_code_fence,
)
from src.core.llm.prompt_hash import hash_prompt, render_prompt_text
from src.core.orchestrator.chapter_orchestrator import RewriteCheckpointRecoveryError
from src.core.routing.router import RoutedTarget
from src.core.workspace.workspace_paths import WorkspacePaths

_ALLOWED = frozenset(
    {
        "reviewer",
        "must_achieve_validator",
        "continuity_validator",
        "forbidden_crossing_validator",
        "setting_consistency_validator",
        "cross_chapter_dedup_validator",
        "ending_tension_validator",
    }
)


REVIEW_INPUT_MISMATCH_MESSAGE = "审核恢复请求与原始输入不一致，未复用响应或调用模型"


def is_known_review_recovery_input_error(row: PipelineRunORM) -> bool:
    """Only the historical, pre-dispatch mismatch; not arbitrary ValueErrors."""
    details = row.error_details or {}
    return (
        row.state == "error"
        and details.get("error_type") == "ValueError"
        and details.get("message") == REVIEW_INPUT_MISMATCH_MESSAGE
        and details.get("chapter_id") == row.current_chapter
    )


def _utc(value: datetime) -> datetime:
    return value.replace(tzinfo=UTC) if value.tzinfo is None else value.astimezone(UTC)


@dataclass(frozen=True)
class ArchivedReviewResponse:
    agent: str
    prompt_hash: str
    prompt_text: str
    response: LLMResponse


@dataclass
class KnownReviewResponseRecovery:
    source_task_id: str
    entries: tuple[ArchivedReviewResponse, ...]
    _used: set[int] = field(default_factory=set, init=False, repr=False)
    _lock: Any = field(default_factory=Lock, init=False, repr=False)

    def take(self, agent: str, messages: list[Message]) -> LLMResponse | None:
        with self._lock:
            pending = [
                (i, item)
                for i, item in enumerate(self.entries)
                if item.agent == agent and i not in self._used
            ]
            if not pending:
                return None
            matches = [
                (i, item)
                for i, item in pending
                if item.prompt_hash == hash_prompt(messages)
                and item.prompt_text == render_prompt_text(messages)
            ]
            if len(matches) != 1:
                raise RewriteCheckpointRecoveryError(REVIEW_INPUT_MISMATCH_MESSAGE)
            index, entry = matches[0]
            self._used.add(index)
            return entry.response.model_copy(deep=True)


def load_known_empty_must_recovery(
    session: Session,
    *,
    paths: WorkspacePaths,
    workspace_id: str,
    chapter_id: str,
    current_version: int,
) -> KnownReviewResponseRecovery | None:
    """Read-only, narrow admission; never releases a lease or modifies content."""
    if current_version < 1:
        return None
    try:
        chapter = session.get(ChapterORM, chapter_id)
        versions = list(
            session.scalars(
                select(ChapterVersionORM)
                .where(ChapterVersionORM.chapter_id == chapter_id)
                .order_by(ChapterVersionORM.version_id)
            )
        )
        latest = versions[-1] if versions else None
        if (
            chapter is None
            or chapter.workspace_id != workspace_id
            or chapter.status not in {"reviewing", "drafting"}
            or chapter.current_version != current_version
            or latest is None
            or latest.version_id != current_version
            or chapter.approved_at is not None
            or chapter.ready_for_next_chapter_at is not None
            or chapter.memory_enrichment_status != "pending"
            or session.scalar(
                select(ReviewResultORM.review_id)
                .where(
                    ReviewResultORM.chapter_id == chapter_id,
                    ReviewResultORM.version_reviewed == current_version,
                )
                .limit(1)
            )
            is not None
            or paths.chapter_version_file(chapter_id, "final.md").exists()
        ):
            return None
        for version in versions:
            if (
                paths.chapter_version_file(
                    chapter_id, f"v{version.version_id}.md"
                ).read_text()
                != version.content
            ):
                return None
        attempts = list(
            session.scalars(
                select(AttemptLedgerORM)
                .where(
                    AttemptLedgerORM.workspace_id == workspace_id,
                    AttemptLedgerORM.chapter_id == chapter_id,
                    AttemptLedgerORM.attempt_kind == "provider",
                    AttemptLedgerORM.created_at >= latest.created_at,
                )
                .order_by(AttemptLedgerORM.created_at)
            )
        )
        reviewer = [a for a in attempts if a.agent_name == "reviewer"]
        must = [a for a in attempts if a.agent_name == "must_achieve_validator"]
        if len(reviewer) != 1 or len(must) != 1:
            return None
        task_id, run_id = reviewer[0].task_id, reviewer[0].run_id
        if not task_id or not run_id:
            return None
        run = session.get(PipelineRunORM, run_id)
        if (
            run is None
            or run.workspace_id != workspace_id
            or run.current_chapter != chapter_id
            or (
                run.state not in {"paused", "running", "interrupted"}
                and not is_known_review_recovery_input_error(run)
            )
            or chapter_id not in run.chapter_ids
            or chapter_id in run.completed_chapter_ids
            or chapter_id in run.skipped_chapter_ids
        ):
            return None
        failed = list(
            session.scalars(
                select(AttemptLedgerORM).where(
                    AttemptLedgerORM.workspace_id == workspace_id,
                    AttemptLedgerORM.chapter_id == chapter_id,
                    AttemptLedgerORM.task_id == task_id,
                    AttemptLedgerORM.run_id == run_id,
                    AttemptLedgerORM.attempt_kind == "pipeline",
                    AttemptLedgerORM.status == "failed",
                    AttemptLedgerORM.error_type == "MustAchieveTechnicalError",
                )
            )
        )
        if len(failed) != 1 or failed[0].completed_at is None:
            return None
        archives: dict[str, list[tuple[Any, dict[str, Any]]]] = {}
        for path in (paths.archives_dir / "responses").glob("*/*.json"):
            raw = json.loads(path.read_text())
            if not isinstance(raw, dict):
                return None
            archives.setdefault(raw.get("provider_request_id", ""), []).append(
                (path, raw)
            )
        entries = []
        for attempt in attempts:
            if (
                attempt.task_id != task_id
                or attempt.run_id != run_id
                or attempt.agent_name not in _ALLOWED
                or attempt.status != "succeeded"
                or attempt.usage_status != "known"
                or attempt.response_status != "completed"
                or not attempt.provider_request_id
                or attempt.completed_at is None
                or _utc(attempt.completed_at) > _utc(failed[0].completed_at)
            ):
                return None
            matches = archives.get(attempt.provider_request_id, [])
            if len(matches) != 1:
                return None
            path, raw = matches[0]
            response = LLMResponse.model_validate(raw)
            captured = _utc(datetime.fromisoformat(raw["captured_at"]))
            if (
                response.model_used != attempt.model
                or response.finish_reason != "stop"
                or response.usage.input_tokens != attempt.input_tokens
                or response.usage.output_tokens != attempt.output_tokens
                or raw.get("prompt_hash") != path.parent.name
                or not re.fullmatch(r"[a-f0-9]{12}", path.parent.name)
                or not _utc(attempt.completed_at)
                <= captured
                <= _utc(failed[0].completed_at)
            ):
                return None
            if attempt.agent_name == "must_achieve_validator":
                if response.content.strip():
                    return None
            else:
                # Full semantic/schema checks remain with the original agents.
                if not isinstance(
                    json.loads(unwrap_json_code_fence(response.content)), dict
                ):
                    return None
            text = (
                paths.archives_dir / "prompts" / f"{path.parent.name}.txt"
            ).read_text()
            entries.append(
                ArchivedReviewResponse(
                    attempt.agent_name, path.parent.name, text, response
                )
            )
        return KnownReviewResponseRecovery(task_id, tuple(entries))
    except (OSError, ValueError, KeyError, TypeError):
        return None


class _RecoveryClient(LLMClient):
    def __init__(
        self, inner: LLMClient, agent: str, recovery: KnownReviewResponseRecovery
    ):
        self._inner, self._agent, self._recovery = inner, agent, recovery

    def prepare_strict_json_schema(self, **kwargs: Any) -> dict[str, Any]:
        return self._inner.prepare_strict_json_schema(**kwargs)

    def complete(self, messages: list[Message], **gen_params: Any) -> LLMResponse:
        renderer = getattr(self._inner, "messages_for_cost_reserve", None)
        if not callable(renderer):
            raise ValueError("审核归档恢复缺少最终请求渲染能力")
        frozen = getattr(self._inner, "_assert_frozen", None)
        if callable(frozen):
            frozen()
        prepared = renderer(messages, gen_params)
        response = self._recovery.take(self._agent, prepared)
        if response is None:
            return self._inner.complete(messages, **gen_params)
        if gen_params.get("model") not in {None, response.model_used}:
            raise ValueError("审核恢复模型身份不一致")
        response = response.model_copy(
            update={"content": unwrap_json_code_fence(response.content)}
        )
        contract = local_contract_from_params(gen_params)
        if contract is not None:
            try:
                contract.validate(response.content)
            except ValueError as exc:
                raise LocalJsonResponseViolation(response, contract, exc) from exc
        return response

    def __getattr__(self, name: str) -> Any:
        return getattr(self._inner, name)


class KnownReviewRecoveryRouter:
    """Wrap outside physical-attempt/cost tracking: archive reads cost zero."""

    def __init__(self, inner: Any, recovery: KnownReviewResponseRecovery):
        self._inner, self._recovery = inner, recovery

    def route(self, agent_name: str, **kwargs: Any) -> RoutedTarget:
        target = self._inner.route(agent_name, **kwargs)
        if agent_name not in _ALLOWED:
            return target
        return RoutedTarget(
            profile=target.profile,
            client=_RecoveryClient(target.client, agent_name, self._recovery),
            complete_kwargs=target.complete_kwargs,
        )

    def __getattr__(self, name: str) -> Any:
        return getattr(self._inner, name)
