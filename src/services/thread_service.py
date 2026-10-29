"""伏笔(PlotThread)CRUD 服务层(P2-B-15)。

把 ``src/core/memory/store.py``(P2-B-04)的低层 SQLite CRUD 包装成业务语义
明确的伏笔管理 service:

- ``list_threads``:列出工作区下全部伏笔,可选按 ``status`` / ``importance`` 过滤
- ``get_thread``:按 ID 取伏笔,跨工作区视为不存在(语义+安全)
- ``create_thread``:新建伏笔,重复 ID 抛 ``PlotThreadAlreadyExistsError``
- ``update_thread``:全量更新,缺失抛 ``PlotThreadNotFoundError``
- ``delete_thread``:按 ID 删除,缺失抛 ``PlotThreadNotFoundError``

设计要点
========

完全沿用 P2-B-14 ``CharacterService`` 模板,字面替换 Character → PlotThread,
**只新增 ``list_threads`` 的 ``status`` / ``importance`` 内存过滤**(store 层
``list_plot_threads_by_workspace`` 无过滤参数,P2-B-04 决策"过滤逻辑放
caller";本任务在 service 层实现以避免污染 store 层签名)。

1. **session 由工厂注入**:每次方法调用取一个新 ``Session``,在 try/finally
   中 ``close``;成功 ``commit``,失败 ``rollback``。工厂签名
   ``Callable[[str], Session]`` 接 ``workspace_id`` 返回绑定到本工作区
   ``novel.db`` 的 ``Session`` 实例。
2. **跨工作区禁止**:``get`` / ``update`` / ``delete`` 若发现 thread.workspace_id
   ≠ 入参 workspace_id,统一翻译为 ``PlotThreadNotFoundError``——既符合"跨
   工作区资源物理隔离"硬约束(CLAUDE.md §9),也避免向调用方暴露"该 ID 在
   其他工作区存在"的信息。
3. **WorkspaceORM upsert**:写操作前调 ``ensure_workspace_orm``(P2-B-13 复
   用),读操作不 upsert(沿用 P2-B-14)。
4. **错误体系**:``PlotThreadServiceError`` 基类 + ``PlotThreadNotFoundError`` /
   ``PlotThreadAlreadyExistsError`` 两个具名子类,由 ``api/errors.py``
   ``_translate_service_error`` 翻译为 ``PLOT_THREAD_NOT_FOUND (404)`` /
   ``PLOT_THREAD_ALREADY_EXISTS (409)``。
5. **list 过滤**:``status`` ∈ ``planted/developing/resolved/abandoned``;
   ``importance`` ∈ 1-5。两个参数都是可选,``None`` 表示不过滤。两参数同时
   提供时按 AND 组合。过滤完成后保留 store 层的 ``(created_at, thread_id)``
   升序。
"""

from __future__ import annotations

from collections.abc import Callable

from sqlalchemy.orm import Session

from src.core.memory.store import (
    create_plot_thread as _store_create_thread,
)
from src.core.memory.store import (
    delete_plot_thread as _store_delete_thread,
)
from src.core.memory.store import (
    get_plot_thread as _store_get_thread,
)
from src.core.memory.store import (
    list_plot_threads_by_workspace as _store_list_threads,
)
from src.core.memory.store import (
    update_plot_thread as _store_update_thread,
)
from src.core.models.plot_thread import PlotThread
from src.services.asset_import import ensure_workspace_orm
from src.services.workspace_service import WorkspaceService

SessionFactory = Callable[[str], Session]


class PlotThreadServiceError(Exception):
    """伏笔 service 层操作失败的基类。

    子类按"操作语义"细分,由 ``api/errors.py`` 翻译表映射到 ApiError 子类。
    """


class PlotThreadNotFoundError(PlotThreadServiceError):
    """伏笔不存在(或在其他工作区,跨工作区视为不存在)。"""


class PlotThreadAlreadyExistsError(PlotThreadServiceError):
    """伏笔 ID 在 SQLite 中已存在(全局主键冲突)。"""


class PlotThreadService:
    """伏笔 CRUD 业务层。

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

    def list_threads(
        self,
        workspace_id: str,
        *,
        status: str | None = None,
        importance: int | None = None,
    ) -> list[PlotThread]:
        """列出工作区下伏笔,按 ``(created_at, thread_id)`` 升序。

        ``status`` / ``importance`` 可选,为 ``None`` 不过滤;两参数同时给按
        AND 组合。``meta.json`` 不存在 → ``MetaFileError`` 冒泡(翻译为 422)。
        空工作区返回 ``[]``。
        """
        self._workspace_service.info(workspace_id)
        session = self._session_factory(workspace_id)
        try:
            threads = _store_list_threads(session, workspace_id)
        finally:
            session.close()

        if status is not None:
            threads = [t for t in threads if t.status == status]
        if importance is not None:
            threads = [t for t in threads if t.importance == importance]
        return threads

    def get_thread(self, workspace_id: str, thread_id: str) -> PlotThread:
        """按 ``thread_id`` 取伏笔;不存在或跨工作区抛
        ``PlotThreadNotFoundError``。"""
        self._workspace_service.info(workspace_id)
        session = self._session_factory(workspace_id)
        try:
            thread = _store_get_thread(session, thread_id)
            if thread is None or thread.workspace_id != workspace_id:
                raise PlotThreadNotFoundError(
                    f"伏笔不存在: workspace={workspace_id} thread={thread_id}"
                )
            return thread
        finally:
            session.close()

    # ---- 写操作 ------------------------------------------------------------

    def create_thread(
        self, workspace_id: str, thread: PlotThread
    ) -> PlotThread:
        """新增伏笔;``thread_id`` 已存在抛 ``PlotThreadAlreadyExistsError``。

        路由层已保证 ``thread.workspace_id == workspace_id``;此处不再校验。
        """
        workspace = self._workspace_service.info(workspace_id)
        session = self._session_factory(workspace_id)
        try:
            ensure_workspace_orm(session, workspace)
            if _store_get_thread(session, thread.thread_id) is not None:
                raise PlotThreadAlreadyExistsError(
                    f"伏笔已存在: {thread.thread_id}"
                )
            created = _store_create_thread(session, thread)
            session.commit()
            return created
        except Exception:
            session.rollback()
            raise
        finally:
            session.close()

    def update_thread(
        self, workspace_id: str, thread: PlotThread
    ) -> PlotThread:
        """全量更新;不存在或跨工作区抛 ``PlotThreadNotFoundError``。

        路由层已保证 ``thread.thread_id`` / ``thread.workspace_id`` 与 URL /
        Header 一致。
        """
        workspace = self._workspace_service.info(workspace_id)
        session = self._session_factory(workspace_id)
        try:
            ensure_workspace_orm(session, workspace)
            existing = _store_get_thread(session, thread.thread_id)
            if existing is None or existing.workspace_id != workspace_id:
                raise PlotThreadNotFoundError(
                    f"伏笔不存在: workspace={workspace_id} thread={thread.thread_id}"
                )
            updated = _store_update_thread(session, thread)
            session.commit()
            return updated
        except Exception:
            session.rollback()
            raise
        finally:
            session.close()

    def delete_thread(self, workspace_id: str, thread_id: str) -> None:
        """按 ``thread_id`` 删除;不存在或跨工作区抛
        ``PlotThreadNotFoundError``。"""
        self._workspace_service.info(workspace_id)
        session = self._session_factory(workspace_id)
        try:
            existing = _store_get_thread(session, thread_id)
            if existing is None or existing.workspace_id != workspace_id:
                raise PlotThreadNotFoundError(
                    f"伏笔不存在: workspace={workspace_id} thread={thread_id}"
                )
            _store_delete_thread(session, thread_id)
            session.commit()
        except Exception:
            session.rollback()
            raise
        finally:
            session.close()
