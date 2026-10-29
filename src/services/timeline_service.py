"""时间线条目(TimelineEntry)CRUD 服务层(P2-B-16)。

把 ``src/core/memory/store.py``(P2-B-04)的低层 SQLite CRUD 包装成业务语义
明确的时间线管理 service:

- ``list_entries``:列出工作区下全部时间线条目,按 ``(time_order, entry_id)`` 升序
- ``get_entry``:按 ID 取条目,跨工作区视为不存在(语义+安全)
- ``create_entry``:新建条目,重复 ID 抛 ``TimelineEntryAlreadyExistsError``
- ``update_entry``:全量更新,缺失抛 ``TimelineEntryNotFoundError``
- ``delete_entry``:按 ID 删除,缺失抛 ``TimelineEntryNotFoundError``

设计要点
========

完全沿用 P2-B-14 ``CharacterService`` / P2-B-15 ``PlotThreadService`` 模板,
字面替换 Character / PlotThread → TimelineEntry。**store list 已按 ``time_order``
升序**(P2-B-04 ``list_timeline_entries_by_workspace``),service 层不需重排;
**不在 service 层做 ``time_order`` 单调校验**——PROJECT_SPEC §4.2 [HARD]
时间线单调校验由 Reviewer 在 commit 前批量执行(P2-B-02 / P2-B-04 决策),
本层允许 ``time_order`` 重复 / 乱序写入。

1. **session 由工厂注入**:每次方法调用取一个新 ``Session``,在 try/finally
   中 ``close``;成功 ``commit``,失败 ``rollback``。工厂签名
   ``Callable[[str], Session]`` 接 ``workspace_id`` 返回绑定到本工作区
   ``novel.db`` 的 ``Session`` 实例。
2. **跨工作区禁止**:``get`` / ``update`` / ``delete`` 若发现 entry.workspace_id
   ≠ 入参 workspace_id,统一翻译为 ``TimelineEntryNotFoundError``——既符合
   "跨工作区资源物理隔离"硬约束(CLAUDE.md §9),也避免向调用方暴露"该
   ID 在其他工作区存在"的信息。
3. **WorkspaceORM upsert**:写操作前调 ``ensure_workspace_orm``(P2-B-13 复
   用),读操作不 upsert(沿用 P2-B-14 / P2-B-15)。
4. **错误体系**:``TimelineEntryServiceError`` 基类 +
   ``TimelineEntryNotFoundError`` / ``TimelineEntryAlreadyExistsError`` 两个
   具名子类,由 ``api/errors.py`` ``_translate_service_error`` 翻译为
   ``TIMELINE_ENTRY_NOT_FOUND (404)`` / ``TIMELINE_ENTRY_ALREADY_EXISTS (409)``。
"""

from __future__ import annotations

from collections.abc import Callable

from sqlalchemy.orm import Session

from src.core.memory.store import (
    create_timeline_entry as _store_create_entry,
)
from src.core.memory.store import (
    delete_timeline_entry as _store_delete_entry,
)
from src.core.memory.store import (
    get_timeline_entry as _store_get_entry,
)
from src.core.memory.store import (
    list_timeline_entries_by_workspace as _store_list_entries,
)
from src.core.memory.store import (
    update_timeline_entry as _store_update_entry,
)
from src.core.models.timeline_entry import TimelineEntry
from src.services.asset_import import ensure_workspace_orm
from src.services.workspace_service import WorkspaceService

SessionFactory = Callable[[str], Session]


class TimelineEntryServiceError(Exception):
    """时间线 service 层操作失败的基类。

    子类按"操作语义"细分,由 ``api/errors.py`` 翻译表映射到 ApiError 子类。
    """


class TimelineEntryNotFoundError(TimelineEntryServiceError):
    """时间线条目不存在(或在其他工作区,跨工作区视为不存在)。"""


class TimelineEntryAlreadyExistsError(TimelineEntryServiceError):
    """时间线条目 ID 在 SQLite 中已存在(全局主键冲突)。"""


class TimelineEntryService:
    """时间线 CRUD 业务层。

    依赖 ``WorkspaceService``(校验 meta.json + 取 ``Workspace`` 给
    ``ensure_workspace_orm`` 用)+ ``session_factory``(每次调用 new 一个
    Session 给本工作区的 SQLite)。
    """

    def __init__(
        self,
        *,
        workspace_service: WorkspaceService,
        session_factory: SessionFactory,
    ) -> None:
        self._workspace_service = workspace_service
        self._session_factory = session_factory

    # ---- 读操作 ------------------------------------------------------------

    def list_entries(self, workspace_id: str) -> list[TimelineEntry]:
        """列出工作区下时间线条目,按 ``(time_order, entry_id)`` 升序
        (store 层已排序,service 不重排)。

        ``meta.json`` 不存在 → ``MetaFileError`` 冒泡(翻译为 422)。空工作区
        返回 ``[]``。
        """
        self._workspace_service.info(workspace_id)
        session = self._session_factory(workspace_id)
        try:
            return _store_list_entries(session, workspace_id)
        finally:
            session.close()

    def get_entry(self, workspace_id: str, entry_id: str) -> TimelineEntry:
        """按 ``entry_id`` 取条目;不存在或跨工作区抛
        ``TimelineEntryNotFoundError``。"""
        self._workspace_service.info(workspace_id)
        session = self._session_factory(workspace_id)
        try:
            entry = _store_get_entry(session, entry_id)
            if entry is None or entry.workspace_id != workspace_id:
                raise TimelineEntryNotFoundError(
                    f"时间线条目不存在: workspace={workspace_id} entry={entry_id}"
                )
            return entry
        finally:
            session.close()

    # ---- 写操作 ------------------------------------------------------------

    def create_entry(
        self, workspace_id: str, entry: TimelineEntry
    ) -> TimelineEntry:
        """新增条目;``entry_id`` 已存在抛 ``TimelineEntryAlreadyExistsError``。

        路由层已保证 ``entry.workspace_id == workspace_id``;此处不再校验。
        """
        workspace = self._workspace_service.info(workspace_id)
        session = self._session_factory(workspace_id)
        try:
            ensure_workspace_orm(session, workspace)
            if _store_get_entry(session, entry.entry_id) is not None:
                raise TimelineEntryAlreadyExistsError(
                    f"时间线条目已存在: {entry.entry_id}"
                )
            created = _store_create_entry(session, entry)
            session.commit()
            return created
        except Exception:
            session.rollback()
            raise
        finally:
            session.close()

    def update_entry(
        self, workspace_id: str, entry: TimelineEntry
    ) -> TimelineEntry:
        """全量更新;不存在或跨工作区抛 ``TimelineEntryNotFoundError``。

        路由层已保证 ``entry.entry_id`` / ``entry.workspace_id`` 与 URL /
        Header 一致。
        """
        workspace = self._workspace_service.info(workspace_id)
        session = self._session_factory(workspace_id)
        try:
            ensure_workspace_orm(session, workspace)
            existing = _store_get_entry(session, entry.entry_id)
            if existing is None or existing.workspace_id != workspace_id:
                raise TimelineEntryNotFoundError(
                    f"时间线条目不存在: workspace={workspace_id} entry={entry.entry_id}"
                )
            updated = _store_update_entry(session, entry)
            session.commit()
            return updated
        except Exception:
            session.rollback()
            raise
        finally:
            session.close()

    def delete_entry(self, workspace_id: str, entry_id: str) -> None:
        """按 ``entry_id`` 删除;不存在或跨工作区抛
        ``TimelineEntryNotFoundError``。"""
        self._workspace_service.info(workspace_id)
        session = self._session_factory(workspace_id)
        try:
            existing = _store_get_entry(session, entry_id)
            if existing is None or existing.workspace_id != workspace_id:
                raise TimelineEntryNotFoundError(
                    f"时间线条目不存在: workspace={workspace_id} entry={entry_id}"
                )
            _store_delete_entry(session, entry_id)
            session.commit()
        except Exception:
            session.rollback()
            raise
        finally:
            session.close()
