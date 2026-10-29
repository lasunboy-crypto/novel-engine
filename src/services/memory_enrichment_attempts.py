"""富记忆恢复用 Provider attempt 审计；只读账本，不调度、不重放。"""

from __future__ import annotations

from datetime import UTC, datetime
from typing import Any

from sqlalchemy import or_, select
from sqlalchemy.orm import Session

from src.core.db.models import AttemptLedgerORM


RICH_MEMORY_PROVIDER_AGENTS = frozenset(
    {
        "memory_extractor",
        "character_disambiguator",
        "plot_thread_merger",
        "timeline_entry_merger",
        "scene_anchor_validator",
        "thread_closure_validator",
    }
)


class MemoryEnrichmentAttemptLedgerError(ValueError):
    """富记忆 attempt 账本形状与 transport 生命周期合同矛盾。"""


def _as_utc(value: datetime) -> datetime:
    if value.tzinfo is None or value.utcoffset() is None:
        return value.replace(tzinfo=UTC)
    return value.astimezone(UTC)


def load_unconsumed_memory_provider_attempts(
    session: Session,
    *,
    workspace_id: str,
    chapter_id: str,
    anchor: datetime,
) -> list[dict[str, Any]]:
    """返回 anchor 后可能已出网、但没有 durable consumption receipt 的请求。"""

    anchor_utc = _as_utc(anchor)
    rows = list(
        session.scalars(
            select(AttemptLedgerORM)
            .where(AttemptLedgerORM.workspace_id == workspace_id)
            .where(AttemptLedgerORM.chapter_id == chapter_id)
            .where(AttemptLedgerORM.attempt_kind == "provider")
            .where(AttemptLedgerORM.status.in_(("started", "succeeded")))
            .where(AttemptLedgerORM.agent_name.in_(RICH_MEMORY_PROVIDER_AGENTS))
            .where(
                or_(
                    AttemptLedgerORM.created_at >= anchor_utc,
                    AttemptLedgerORM.completed_at >= anchor_utc,
                )
            )
            .order_by(
                AttemptLedgerORM.created_at,
                AttemptLedgerORM.attempt_id,
            )
        )
    )
    result: list[dict[str, Any]] = []
    for row in rows:
        if row.operation_kind != "agent_call":
            raise MemoryEnrichmentAttemptLedgerError(
                "富记忆 Provider attempt.operation_kind 必须是 agent_call: "
                f"attempt={row.attempt_id} actual={row.operation_kind!r}"
            )
        if row.status == "started":
            if row.completed_at is not None or row.error_type is not None:
                raise MemoryEnrichmentAttemptLedgerError(
                    "started 富记忆 Provider attempt 不能已有 completed/error: "
                    f"attempt={row.attempt_id}"
                )
        elif row.completed_at is None or row.error_type is not None:
            raise MemoryEnrichmentAttemptLedgerError(
                "succeeded 富记忆 Provider attempt 必须有 completed_at 且无 error: "
                f"attempt={row.attempt_id}"
            )
        result.append(
            {
                "attempt_id": row.attempt_id,
                "status": row.status,
                "agent_name": row.agent_name,
            }
        )
    return result


__all__ = [
    "MemoryEnrichmentAttemptLedgerError",
    "RICH_MEMORY_PROVIDER_AGENTS",
    "load_unconsumed_memory_provider_attempts",
]
