"""富记忆 Worker 的 SQLite 本地副作用快照。

本模块只读当前 session，不调用 Provider、不提交事务。Worker 在
``pending -> running`` 的同一事务内冻结 baseline；冷启动恢复用同一函数
重算并逐字段比较，避免把本章早已存在的合法记忆误当成中断写入。
"""

from __future__ import annotations

import hashlib
import json
from dataclasses import dataclass
from datetime import date, datetime
from typing import Any

from sqlalchemy import select
from sqlalchemy.orm import Session

from src.core.db.models import (
    ChapterORM,
    CharacterORM,
    PlotThreadORM,
    TimelineEntryORM,
)
from src.core.models.chapter_card import chapter_seq_from_id


SQLITE_MEMORY_FOOTPRINT_FIELDS = frozenset(
    {
        "character_first_appearance",
        "character_last_updated",
        "character_state_timeline",
        "plot_planted",
        "plot_last_updated",
        "timeline_entries",
        "scene_anchor",
    }
)


@dataclass(frozen=True)
class SQLiteMemorySnapshot:
    """本章可被富记忆改写的 SQLite 投影及内容摘要。"""

    footprint: dict[str, int]
    digest: str


def _json_value(value: Any) -> Any:
    """把 ORM 列值归一为稳定 JSON；未知标量保守转字符串。"""

    if value is None or isinstance(value, (bool, int, float, str)):
        return value
    if isinstance(value, (datetime, date)):
        return value.isoformat()
    if isinstance(value, bytes):
        return {"__bytes_hex__": value.hex()}
    if isinstance(value, dict):
        return {
            str(key): _json_value(item)
            for key, item in sorted(value.items(), key=lambda pair: str(pair[0]))
        }
    if isinstance(value, (list, tuple)):
        return [_json_value(item) for item in value]
    return str(value)


def _orm_record(value: Any) -> dict[str, Any]:
    return {
        column.name: _json_value(getattr(value, column.name))
        for column in value.__table__.columns
    }


def audit_sqlite_memory_footprint(
    *,
    session: Session,
    workspace_id: str,
    row: ChapterORM,
) -> dict[str, int]:
    """返回与本章关联的富记忆 SQLite footprint，只读且顺序无关。"""

    chapter_id = row.chapter_id
    chapter_seq = chapter_seq_from_id(chapter_id)
    characters = session.scalars(
        select(CharacterORM).where(CharacterORM.workspace_id == workspace_id)
    ).all()
    plots = session.scalars(
        select(PlotThreadORM).where(PlotThreadORM.workspace_id == workspace_id)
    ).all()
    timeline_count = len(
        session.scalars(
            select(TimelineEntryORM)
            .where(TimelineEntryORM.workspace_id == workspace_id)
            .where(TimelineEntryORM.chapter_id == chapter_id)
        ).all()
    )
    state_timeline_count = 0
    if chapter_seq is not None:
        for character in characters:
            snapshots = character.state_timeline or []
            state_timeline_count += sum(
                1
                for snapshot in snapshots
                if isinstance(snapshot, dict)
                and snapshot.get("as_of_chapter") == chapter_seq
            )
    return {
        "character_first_appearance": sum(
            character.first_appearance_chapter == chapter_id
            for character in characters
        ),
        "character_last_updated": sum(
            character.last_updated_chapter == chapter_id
            for character in characters
        ),
        "character_state_timeline": state_timeline_count,
        "plot_planted": sum(
            plot.planted_chapter == chapter_id for plot in plots
        ),
        "plot_last_updated": sum(
            plot.last_updated_chapter == chapter_id for plot in plots
        ),
        "timeline_entries": timeline_count,
        "scene_anchor": int(bool(row.scene_anchor)),
    }


def audit_sqlite_memory_snapshot(
    *,
    session: Session,
    workspace_id: str,
    row: ChapterORM,
) -> SQLiteMemorySnapshot:
    """对本轮可能触及的整行内容做稳定摘要，检测同数量原地覆盖。"""

    chapter_id = row.chapter_id
    chapter_seq = chapter_seq_from_id(chapter_id)
    characters = session.scalars(
        select(CharacterORM).where(CharacterORM.workspace_id == workspace_id)
    ).all()
    relevant_characters = []
    for character in characters:
        snapshots = character.state_timeline or []
        has_current_snapshot = chapter_seq is not None and any(
            isinstance(snapshot, dict)
            and snapshot.get("as_of_chapter") == chapter_seq
            for snapshot in snapshots
        )
        if (
            character.first_appearance_chapter == chapter_id
            or character.last_updated_chapter == chapter_id
            or has_current_snapshot
        ):
            relevant_characters.append(character)
    plots = session.scalars(
        select(PlotThreadORM).where(PlotThreadORM.workspace_id == workspace_id)
    ).all()
    relevant_plots = [
        plot
        for plot in plots
        if plot.planted_chapter == chapter_id
        or plot.last_updated_chapter == chapter_id
    ]
    timeline = session.scalars(
        select(TimelineEntryORM)
        .where(TimelineEntryORM.workspace_id == workspace_id)
        .where(TimelineEntryORM.chapter_id == chapter_id)
    ).all()
    payload = {
        "workspace_id": workspace_id,
        "chapter_id": chapter_id,
        "characters": [
            _orm_record(value)
            for value in sorted(
                relevant_characters,
                key=lambda value: value.character_id,
            )
        ],
        "plot_threads": [
            _orm_record(value)
            for value in sorted(
                relevant_plots,
                key=lambda value: value.thread_id,
            )
        ],
        "timeline_entries": [
            _orm_record(value)
            for value in sorted(timeline, key=lambda value: value.entry_id)
        ],
        "scene_anchor": _json_value(row.scene_anchor),
    }
    encoded = json.dumps(
        payload,
        ensure_ascii=False,
        sort_keys=True,
        separators=(",", ":"),
    ).encode("utf-8")
    return SQLiteMemorySnapshot(
        footprint=audit_sqlite_memory_footprint(
            session=session,
            workspace_id=workspace_id,
            row=row,
        ),
        digest=hashlib.sha256(encoded).hexdigest(),
    )


def validate_sqlite_memory_footprint(value: object) -> dict[str, int]:
    """严格验证持久 baseline；不接受缺字段、bool 或负数。"""

    if not isinstance(value, dict) or set(value) != SQLITE_MEMORY_FOOTPRINT_FIELDS:
        raise ValueError("sqlite footprint 字段集合不完整")
    if any(type(item) is not int or item < 0 for item in value.values()):
        raise ValueError("sqlite footprint 只能包含非负整数")
    return dict(value)


__all__ = [
    "SQLiteMemorySnapshot",
    "SQLITE_MEMORY_FOOTPRINT_FIELDS",
    "audit_sqlite_memory_footprint",
    "audit_sqlite_memory_snapshot",
    "validate_sqlite_memory_footprint",
]
