"""回退章节时保护书级大纲实体，并修复旧 rewind 遗留的误撤销。

``characters.json`` / ``plot.json`` 是书级大纲 SSOT。MemoryExtractor 会在角色或
伏笔首次进入正文时把同一 ORM 行的 ``first_appearance_chapter`` /
``planted_chapter`` 回填为当前章；因此 rewind 不能只凭这两个字段判断实体来自正文。
本模块用当前大纲 ID 重新建立来源边界，并把首次出场章被回退的书级实体恢复为大纲
基线。正文新生实体仍由既有 soft-revoke 路径处理。
"""

from __future__ import annotations

import json
from dataclasses import dataclass
from pathlib import Path

from sqlalchemy import select
from sqlalchemy.orm import Session

from src.core.db.models import CharacterORM, PlotThreadORM
from src.core.models.outline import (
    CharacterOutlineItem,
    CharactersOutput,
    PlotLongThread,
    PlotOutput,
)
from src.core.workspace.workspace_paths import WorkspacePaths
from src.services.outline_service import (
    _outline_character_to_character,
    _outline_plot_thread_to_thread,
)


class OutlineEntityRecoveryError(Exception):
    """大纲实体基线缺失或损坏，禁止带着不完整来源边界继续改库。"""


@dataclass(frozen=True)
class OutlineEntityBaselines:
    """当前书级大纲中的角色与伏笔，按稳定 ID 索引。"""

    characters: dict[str, CharacterOutlineItem]
    plot_threads: dict[str, PlotLongThread]


@dataclass(frozen=True)
class OutlineEntityRestoreResult:
    """本次实际恢复的书级实体 ID。"""

    character_ids: tuple[str, ...] = ()
    plot_thread_ids: tuple[str, ...] = ()


def _read_json(path: Path) -> object:
    try:
        return json.loads(path.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError) as exc:
        raise OutlineEntityRecoveryError(
            f"无法读取书级大纲实体基线: {path}: {exc}"
        ) from exc


def load_outline_entity_baselines(
    *,
    workspaces_root: Path,
    workspace_id: str,
) -> OutlineEntityBaselines:
    """读取现行大纲 SSOT；老工作区缺某段时该类基线按空集合兼容。"""

    paths = WorkspacePaths(workspace_id, workspaces_root=workspaces_root)
    characters_path = paths.outline_dir / "characters.json"
    plot_path = paths.outline_dir / "plot.json"

    characters: dict[str, CharacterOutlineItem] = {}
    if characters_path.is_file():
        try:
            characters_payload = CharactersOutput.model_validate(
                _read_json(characters_path)
            )
        except Exception as exc:
            if isinstance(exc, OutlineEntityRecoveryError):
                raise
            raise OutlineEntityRecoveryError(
                f"书级角色基线不合法: {characters_path}: {exc}"
            ) from exc
        characters = {
            item.character_id: item for item in characters_payload.characters
        }

    plot_threads: dict[str, PlotLongThread] = {}
    if plot_path.is_file():
        try:
            plot_payload = PlotOutput.model_validate(_read_json(plot_path))
        except Exception as exc:
            if isinstance(exc, OutlineEntityRecoveryError):
                raise
            raise OutlineEntityRecoveryError(
                f"书级伏笔基线不合法: {plot_path}: {exc}"
            ) from exc
        plot_threads = {
            item.thread_id: item for item in plot_payload.long_threads
        }

    return OutlineEntityBaselines(
        characters=characters,
        plot_threads=plot_threads,
    )


def restore_rewound_outline_entities_in_session(
    session: Session,
    *,
    workspace_id: str,
    chapter_id: str,
    baselines: OutlineEntityBaselines,
    require_revoked: bool = False,
) -> OutlineEntityRestoreResult:
    """在 caller 事务内恢复首次于目标章落正文的书级实体。

    只命中 ``ID 仍在当前大纲`` 且首次章精确等于 ``chapter_id`` 的行。这样的书级
    实体在目标章之前尚无正文状态，恢复到大纲提交基线不会抹掉更早章节的有效更新。
    """

    restored_character_ids: list[str] = []
    if baselines.characters:
        character_stmt = (
            select(CharacterORM)
                .where(CharacterORM.workspace_id == workspace_id)
                .where(CharacterORM.first_appearance_chapter == chapter_id)
                .where(
                    CharacterORM.character_id.in_(tuple(baselines.characters))
                )
                .order_by(CharacterORM.character_id)
        )
        if require_revoked:
            character_stmt = character_stmt.where(
                CharacterORM.revoked_at.is_not(None)
            )
        character_rows = list(session.scalars(character_stmt))
        for character_row in character_rows:
            character_baseline = _outline_character_to_character(
                workspace_id=workspace_id,
                item=baselines.characters[character_row.character_id],
            )
            for key, value in character_baseline.model_dump(
                mode="python"
            ).items():
                setattr(character_row, key, value)
            character_row.revoked_at = None
            restored_character_ids.append(character_row.character_id)

    restored_thread_ids: list[str] = []
    if baselines.plot_threads:
        plot_stmt = (
            select(PlotThreadORM)
                .where(PlotThreadORM.workspace_id == workspace_id)
                .where(PlotThreadORM.planted_chapter == chapter_id)
                .where(
                    PlotThreadORM.thread_id.in_(tuple(baselines.plot_threads))
                )
                .order_by(PlotThreadORM.thread_id)
        )
        if require_revoked:
            plot_stmt = plot_stmt.where(PlotThreadORM.revoked_at.is_not(None))
        plot_rows = list(session.scalars(plot_stmt))
        for plot_row in plot_rows:
            thread_baseline = _outline_plot_thread_to_thread(
                workspace_id=workspace_id,
                item=baselines.plot_threads[plot_row.thread_id],
                now=plot_row.created_at,
            )
            thread_payload = thread_baseline.model_dump(mode="python")
            thread_payload.pop("created_at", None)
            for key, value in thread_payload.items():
                setattr(plot_row, key, value)
            plot_row.origin = "outline"
            plot_row.expiry_policy = "never"
            plot_row.source_evidence = None
            plot_row.revoked_at = None
            restored_thread_ids.append(plot_row.thread_id)

    session.flush()
    return OutlineEntityRestoreResult(
        character_ids=tuple(restored_character_ids),
        plot_thread_ids=tuple(restored_thread_ids),
    )


__all__ = [
    "OutlineEntityBaselines",
    "OutlineEntityRecoveryError",
    "OutlineEntityRestoreResult",
    "load_outline_entity_baselines",
    "restore_rewound_outline_entities_in_session",
]
