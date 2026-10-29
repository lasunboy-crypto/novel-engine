"""P2 记忆三表 SQLite CRUD(P2-B-04)。

提供 ``Character`` / ``PlotThread`` / ``TimelineEntry`` 的低层数据访问
层(create / get / list_by_workspace / update / delete 五件套)。SQLAlchemy
2.x 风格,**不直接连 sqlite3**;调用方负责创建 ``Session`` 并管理事务边界
——本模块只 ``flush()``,不 ``commit()``。

设计要点
--------

1. **入参 / 出参用 Pydantic**:让上层 service(P2-B-14 / P2-B-15 / P2-B-16
   路由层)免去 ORM ↔ Pydantic 转换样板,与 P1 ``ChapterService`` 风格对齐
   (P1 service 也是入 / 出 Pydantic,内部走 dataclass / 文件)。
2. **不在 store 内 commit**:``flush()`` 把 SQL 错误前置(``IntegrityError``
   等),``commit()`` 由调用方按业务边界统一控制——便于 P2-B-09
   ``MemoryExtractor`` 在同一事务内批量写多条记忆。
3. **time_order 单调校验放 service 层**(沿用 P2-B-02 决策"模型层不做跨字段
   校验"):本模块允许 ``TimelineEntry`` 重复 / 乱序写入,§4.2 [HARD] 时间线
   单调校验由后续 service / Reviewer 在 commit 前批量校验。
4. **update / delete 对缺失行抛 ``MemoryStoreError``**:对齐 P1
   ``ChapterServiceError`` / ``WorkspaceServiceError`` 失败语义,上层 API
   翻译为 404。
5. **get 缺失返回 ``None``**:由调用方按业务决定是 404 还是降级——与 P1
   ``WorkspaceService.info`` 抛错风格区分(get 是查询,not-found 不是错误)。
6. **list_by_workspace 排序**:Character 按 ``character_id`` / PlotThread 按
   ``(created_at, thread_id)`` / TimelineEntry 按 ``(time_order, entry_id)``
   ——分别是各类最自然的展示顺序;``time_order`` 重复时用 ``entry_id`` 二次
   排序保证结果稳定。

边界
----

- ``IntegrityError``(主键重复 / FK 违反 / NOT NULL 违反)自然冒泡,由调用方
  按业务翻译——store 不吞、不重写错误语义。
- ``workspace_id`` 跨工作区搬运角色 / 伏笔的禁止由 service 层校验,本层不
  做"不许换 workspace_id"的特殊保护。
"""

from __future__ import annotations

from datetime import datetime, timezone

from sqlalchemy import select
from sqlalchemy.orm import Session

from src.core.db import CharacterORM, PlotThreadORM, TimelineEntryORM
from src.core.models.character import (
    Character,
    CharacterStateSnapshot,
    IdentityFacet,
    VoiceProfile,
)
from src.core.models.plot_thread import PlotThread
from src.core.models.timeline_entry import TimelineEntry


class MemoryStoreError(Exception):
    """记忆存储层操作失败时抛出。

    主要触发场景:``update`` / ``delete`` 目标行不存在。FK 违反等 SQLAlchemy
    错误自然冒泡(``IntegrityError``),由调用方按业务翻译。
    """


# ---- 内部转换辅助 -----------------------------------------------------------

def _character_orm_to_pydantic(row: CharacterORM) -> Character:
    """``CharacterORM`` → ``Character``;``voice_profile`` JSON dict 还原回 Pydantic 子模型。"""
    return Character(
        character_id=row.character_id,
        workspace_id=row.workspace_id,
        name=row.name,
        aliases=list(row.aliases),
        archetype=row.archetype,
        appearance=row.appearance,
        personality=row.personality,
        background=row.background,
        identity_facets=[
            IdentityFacet(**f) for f in (row.identity_facets or [])
        ],
        voice_profile=VoiceProfile(**row.voice_profile),
        goals_long_term=list(row.goals_long_term),
        goals_short_term=list(row.goals_short_term),
        current_state=dict(row.current_state),
        state_timeline=[
            CharacterStateSnapshot(**snap) for snap in (row.state_timeline or [])
        ],
        relationships=dict(row.relationships),
        abilities=list(row.abilities),
        secrets=list(row.secrets),
        first_appearance_chapter=row.first_appearance_chapter,
        last_updated_chapter=row.last_updated_chapter,
    )


def _ensure_utc(dt: datetime) -> datetime:
    """SQLite ``DateTime(timezone=True)`` 回读会丢 tz(SQLite 无原生 tz-aware
    datetime 类型,SQLAlchemy 存为 naive)。store 按"入库为 UTC"约定恢复
    ``timezone.utc``,让调用方拿到的始终是 tz-aware 时间。"""
    return dt if dt.tzinfo is not None else dt.replace(tzinfo=timezone.utc)


def _plot_thread_orm_to_pydantic(row: PlotThreadORM) -> PlotThread:
    return PlotThread(
        thread_id=row.thread_id,
        workspace_id=row.workspace_id,
        title=row.title,
        thread_type=row.thread_type,
        status=row.status,
        importance=row.importance,
        planted_chapter=row.planted_chapter,
        planned_payoff=row.planned_payoff,
        actual_payoff=row.actual_payoff,
        description=row.description,
        related_characters=list(row.related_characters),
        created_at=_ensure_utc(row.created_at),
        last_updated_chapter=row.last_updated_chapter,
    )


def _timeline_entry_orm_to_pydantic(row: TimelineEntryORM) -> TimelineEntry:
    return TimelineEntry(
        entry_id=row.entry_id,
        workspace_id=row.workspace_id,
        chapter_id=row.chapter_id,
        description=row.description,
        time_label=row.time_label,
        time_order=row.time_order,
        involved_characters=list(row.involved_characters),
        is_flashback=row.is_flashback,
    )


# ---- Character CRUD ---------------------------------------------------------

def create_character(session: Session, character: Character) -> Character:
    """新增角色;返回落库后的 Pydantic 副本。

    ``voice_profile`` 通过 ``model_dump()`` 自动序列化为 dict 写入 JSON 列;
    ``character_id`` 重复或 ``workspace_id`` FK 违反由 SQLAlchemy 抛
    ``IntegrityError``(冒泡到调用方)。
    """
    row = CharacterORM(**character.model_dump())
    session.add(row)
    session.flush()
    return _character_orm_to_pydantic(row)


def get_character(session: Session, character_id: str) -> Character | None:
    """按 ``character_id`` 取角色;不存在返回 ``None``。"""
    row = session.get(CharacterORM, character_id)
    return None if row is None else _character_orm_to_pydantic(row)


def list_characters_by_workspace(
    session: Session, workspace_id: str
) -> list[Character]:
    """列出工作区下全部角色,按 ``character_id`` 升序。"""
    rows = session.scalars(
        select(CharacterORM)
        .where(CharacterORM.workspace_id == workspace_id)
        .order_by(CharacterORM.character_id)
    ).all()
    return [_character_orm_to_pydantic(r) for r in rows]


def update_character(session: Session, character: Character) -> Character:
    """以 ``character_id`` 为主键全量更新;不存在抛 ``MemoryStoreError``。

    ``workspace_id`` 也允许更新——业务上"跨工作区搬运"的禁止由 service
    层负责,本层不限制。
    """
    row = session.get(CharacterORM, character.character_id)
    if row is None:
        raise MemoryStoreError(f"角色不存在: {character.character_id}")
    for key, value in character.model_dump().items():
        setattr(row, key, value)
    # rewind 重跑复活语义(v1.1 w4 b1):update = 该记忆现在 active,清 soft-revoke
    # 标记。否则 rewind soft-revoke 的行被重跑 update 后仍 revoked_at 非空 → 重跑
    # 产生的记忆被旧标记困住、retriever/reference gate 不可见(luo-clean-v2 ch-7 现场)。
    # revoked_at 不在 Pydantic model,上面 model_dump setattr 不会碰,需显式清。
    row.revoked_at = None
    session.flush()
    return _character_orm_to_pydantic(row)


def delete_character(session: Session, character_id: str) -> None:
    """按 ``character_id`` 删除;不存在抛 ``MemoryStoreError``。"""
    row = session.get(CharacterORM, character_id)
    if row is None:
        raise MemoryStoreError(f"角色不存在: {character_id}")
    session.delete(row)
    session.flush()


# ---- PlotThread CRUD --------------------------------------------------------

def create_plot_thread(
    session: Session,
    thread: PlotThread,
    *,
    origin: str = "manual",
    expiry_policy: str = "never",
    source_evidence: str | None = None,
) -> PlotThread:
    """新增伏笔，并把内部来源/休眠元数据与公开 PlotThread 模型分开保存。

    普通 API/导入调用默认是 ``manual + never``；大纲和 MemoryExtractor 必须
    分别显式传 ``outline`` / ``emergent``。这些内部字段不会在 update 时被
    Pydantic ``model_dump`` 覆盖，因此大纲主伏笔不会被后续正文抽取降级成临时层。
    """
    row = PlotThreadORM(
        **thread.model_dump(),
        origin=origin,
        expiry_policy=expiry_policy,
        source_evidence=source_evidence,
    )
    session.add(row)
    session.flush()
    return _plot_thread_orm_to_pydantic(row)


def get_plot_thread(session: Session, thread_id: str) -> PlotThread | None:
    """按 ``thread_id`` 取伏笔;不存在返回 ``None``。"""
    row = session.get(PlotThreadORM, thread_id)
    return None if row is None else _plot_thread_orm_to_pydantic(row)


def list_plot_threads_by_workspace(
    session: Session, workspace_id: str
) -> list[PlotThread]:
    """列出工作区下全部伏笔,按 ``(created_at, thread_id)`` 升序。

    创建时序最自然;``thread_id`` 二次排序保证同时戳条目稳定。
    """
    rows = session.scalars(
        select(PlotThreadORM)
        .where(PlotThreadORM.workspace_id == workspace_id)
        .order_by(PlotThreadORM.created_at, PlotThreadORM.thread_id)
    ).all()
    return [_plot_thread_orm_to_pydantic(r) for r in rows]


def update_plot_thread(session: Session, thread: PlotThread) -> PlotThread:
    """以 ``thread_id`` 为主键全量更新;不存在抛 ``MemoryStoreError``。"""
    row = session.get(PlotThreadORM, thread.thread_id)
    if row is None:
        raise MemoryStoreError(f"伏笔不存在: {thread.thread_id}")
    for key, value in thread.model_dump().items():
        setattr(row, key, value)
    # rewind 重跑复活语义(v1.1 w4 b1):见 update_character 同段注释。
    row.revoked_at = None
    session.flush()
    return _plot_thread_orm_to_pydantic(row)


def delete_plot_thread(session: Session, thread_id: str) -> None:
    """按 ``thread_id`` 删除;不存在抛 ``MemoryStoreError``。"""
    row = session.get(PlotThreadORM, thread_id)
    if row is None:
        raise MemoryStoreError(f"伏笔不存在: {thread_id}")
    session.delete(row)
    session.flush()


# ---- TimelineEntry CRUD -----------------------------------------------------

def create_timeline_entry(
    session: Session, entry: TimelineEntry
) -> TimelineEntry:
    """新增时间线条目;**不做单调校验**(P2-B-02 决策)。

    PROJECT_SPEC §4.2 [HARD] 时间线单调校验由 service / Reviewer 层在 commit
    前批量校验,本层允许 ``time_order`` 重复 / 乱序写入。
    """
    row = TimelineEntryORM(**entry.model_dump())
    session.add(row)
    session.flush()
    return _timeline_entry_orm_to_pydantic(row)


def get_timeline_entry(
    session: Session, entry_id: str
) -> TimelineEntry | None:
    """按 ``entry_id`` 取时间线条目;不存在返回 ``None``。"""
    row = session.get(TimelineEntryORM, entry_id)
    return None if row is None else _timeline_entry_orm_to_pydantic(row)


def list_timeline_entries_by_workspace(
    session: Session, workspace_id: str, *, include_revoked: bool = False
) -> list[TimelineEntry]:
    """列出工作区下时间线条目,按 ``(time_order, entry_id)`` 升序。

    ``ix_timeline_entries_time_order`` 索引覆盖主排序键;``entry_id`` 二次
    排序保证同 ``time_order`` 条目稳定。

    :param include_revoked: 默认 ``False`` — **不返回**软删除(``revoked_at``
        非空)条目,与 :class:`VectorStore` 的 ``include_revoked`` 语义一致
        (soft-delete 数据默认不可见)。**为何默认过滤**(B1 长跑 S4 rewind
        现场实证):rewind 章后台 soft-revoke 的旧 timeline entry 仍留库
        (``revoked_at`` 非空,且 ``time_order`` 停在 S3 修复前的旧值),若
        被预读出来喂 ``check_timeline_monotonic`` 会与重跑后的 active 条目
        ``time_order`` 碰撞 → 每章刷 ``timeline_monotonic_warn_only`` 误报。
        active 数据本身全局严格单调(已 query 实证),误报纯由 revoked 脏数据
        参与校验引起。``validator_report_service`` 早已显式 ``revoked_at IS
        NULL`` 过滤(对的范本),本函数对齐之 → 同型隐患(timeline_monotonic
        预读 / timeline_entry_merger 的 existing 候选 / health / API)一并根治。
        需要含软删除条目的审计场景显式传 ``include_revoked=True``。
    """
    stmt = select(TimelineEntryORM).where(
        TimelineEntryORM.workspace_id == workspace_id
    )
    if not include_revoked:
        stmt = stmt.where(TimelineEntryORM.revoked_at.is_(None))
    stmt = stmt.order_by(
        TimelineEntryORM.time_order, TimelineEntryORM.entry_id
    )
    rows = session.scalars(stmt).all()
    return [_timeline_entry_orm_to_pydantic(r) for r in rows]


def update_timeline_entry(
    session: Session, entry: TimelineEntry
) -> TimelineEntry:
    """以 ``entry_id`` 为主键全量更新;不存在抛 ``MemoryStoreError``。"""
    row = session.get(TimelineEntryORM, entry.entry_id)
    if row is None:
        raise MemoryStoreError(f"时间线条目不存在: {entry.entry_id}")
    for key, value in entry.model_dump().items():
        setattr(row, key, value)
    # rewind 重跑复活语义(v1.1 w4 b1):见 update_character 同段注释。
    row.revoked_at = None
    session.flush()
    return _timeline_entry_orm_to_pydantic(row)


def delete_timeline_entry(session: Session, entry_id: str) -> None:
    """按 ``entry_id`` 删除;不存在抛 ``MemoryStoreError``。"""
    row = session.get(TimelineEntryORM, entry_id)
    if row is None:
        raise MemoryStoreError(f"时间线条目不存在: {entry_id}")
    session.delete(row)
    session.flush()
