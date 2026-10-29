"""角色 CRUD 服务层(P2-B-14)。

把 ``src/core/memory/store.py``(P2-B-04)的低层 SQLite CRUD 包装成业务语义
明确的角色管理 service:

- ``list_characters``:列出工作区下全部角色
- ``get_character``:按 ID 取角色,跨工作区视为不存在(语义+安全)
- ``create_character``:新建角色,重复 ID 抛 ``CharacterAlreadyExistsError``
- ``update_character``:全量更新,缺失抛 ``CharacterNotFoundError``
- ``delete_character``:按 ID 删除,缺失抛 ``CharacterNotFoundError``

设计要点
========

1. **session 由工厂注入**:每次方法调用取一个新 ``Session``,在 try/finally
   中 ``close``;成功调用 ``session.commit()``,失败 ``session.rollback()``。
   工厂 ``session_factory: Callable[[str], Session]`` 接 ``workspace_id`` 返
   回 ``Session`` 实例(默认 P1 ``data_dir/<workspace_id>/data/novel.db``,
   测试可替换 in-memory engine)。
2. **跨工作区禁止**:``get`` / ``update`` / ``delete`` 若发现 character.workspace_id
   ≠ 入参 workspace_id,统一翻译为 ``CharacterNotFoundError``——既符合"跨工作
   区资源物理隔离"硬约束(CLAUDE.md §9),也避免向调用方暴露"该 ID 在其他
   工作区存在"的信息(安全考量)。
3. **WorkspaceORM upsert**:写操作前调 ``ensure_workspace_orm``(P2-B-13 复
   用),把 ``meta.json`` 元数据 sync 到 SQLite,避免 FK 语义违反。读操作
   不 upsert(``list`` / ``get`` 在空工作区下返回空 / 不存在均合理)。
4. **错误体系**:``CharacterServiceError`` 基类 + ``CharacterNotFoundError`` /
   ``CharacterAlreadyExistsError`` 两个具名子类,由 ``api/errors.py``
   ``_translate_service_error`` 翻译为 ``CHARACTER_NOT_FOUND (404)`` /
   ``CHARACTER_ALREADY_EXISTS (409)``。**不直接抛 ``ApiError``**——保持
   service 层与 API 错误体系解耦(P1 沉淀:service 抛错误码异常子类比按
   消息子串分流精度高)。
5. **workspace 校验**:写操作前调 ``WorkspaceService.info(workspace_id)``,
   ``meta.json`` 缺失 → ``MetaFileError`` 自然冒泡(翻译为
   ``WORKSPACE_INVALID 422``)。
"""

from __future__ import annotations

from collections.abc import Callable

from sqlalchemy.orm import Session

from src.core.memory.store import (
    create_character as _store_create_character,
)
from src.core.memory.store import (
    delete_character as _store_delete_character,
)
from src.core.memory.store import (
    get_character as _store_get_character,
)
from src.core.memory.store import (
    list_characters_by_workspace as _store_list_characters,
)
from src.core.memory.store import (
    update_character as _store_update_character,
)
from src.core.models.character import Character
from src.services.asset_import import ensure_workspace_orm
from src.services.workspace_service import WorkspaceService

SessionFactory = Callable[[str], Session]


class CharacterServiceError(Exception):
    """角色 service 层操作失败的基类。

    子类按"操作语义"细分,由 ``api/errors.py`` 翻译表映射到 ApiError 子类。
    """


class CharacterNotFoundError(CharacterServiceError):
    """角色不存在(或在其他工作区,跨工作区视为不存在)。"""


class CharacterAlreadyExistsError(CharacterServiceError):
    """角色 ID 在 SQLite 中已存在(全局主键冲突)。"""


class CharacterService:
    """角色 CRUD 业务层。

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

    def list_characters(self, workspace_id: str) -> list[Character]:
        """列出工作区下所有角色,按 ``character_id`` 升序。

        ``meta.json`` 不存在 → ``MetaFileError`` 冒泡(翻译为 422)。空工作区
        返回 ``[]``。
        """
        self._workspace_service.info(workspace_id)
        session = self._session_factory(workspace_id)
        try:
            return _store_list_characters(session, workspace_id)
        finally:
            session.close()

    def get_character(self, workspace_id: str, character_id: str) -> Character:
        """按 ``character_id`` 取角色;不存在或跨工作区抛
        ``CharacterNotFoundError``。"""
        self._workspace_service.info(workspace_id)
        session = self._session_factory(workspace_id)
        try:
            char = _store_get_character(session, character_id)
            if char is None or char.workspace_id != workspace_id:
                raise CharacterNotFoundError(
                    f"角色不存在: workspace={workspace_id} character={character_id}"
                )
            return char
        finally:
            session.close()

    # ---- 写操作 ------------------------------------------------------------

    def create_character(
        self, workspace_id: str, character: Character
    ) -> Character:
        """新增角色;``character_id`` 已存在抛 ``CharacterAlreadyExistsError``。

        路由层已保证 ``character.workspace_id == workspace_id``;此处不再校验。
        """
        workspace = self._workspace_service.info(workspace_id)
        session = self._session_factory(workspace_id)
        try:
            ensure_workspace_orm(session, workspace)
            if _store_get_character(session, character.character_id) is not None:
                raise CharacterAlreadyExistsError(
                    f"角色已存在: {character.character_id}"
                )
            created = _store_create_character(session, character)
            session.commit()
            return created
        except Exception:
            session.rollback()
            raise
        finally:
            session.close()

    def update_character(
        self, workspace_id: str, character: Character
    ) -> Character:
        """全量更新;不存在或跨工作区抛 ``CharacterNotFoundError``。

        路由层已保证 ``character.character_id`` / ``character.workspace_id``
        与 URL / Header 一致。
        """
        workspace = self._workspace_service.info(workspace_id)
        session = self._session_factory(workspace_id)
        try:
            ensure_workspace_orm(session, workspace)
            existing = _store_get_character(session, character.character_id)
            if existing is None or existing.workspace_id != workspace_id:
                raise CharacterNotFoundError(
                    f"角色不存在: workspace={workspace_id} character={character.character_id}"
                )
            updated = _store_update_character(session, character)
            session.commit()
            return updated
        except Exception:
            session.rollback()
            raise
        finally:
            session.close()

    def delete_character(self, workspace_id: str, character_id: str) -> None:
        """按 ``character_id`` 删除;不存在或跨工作区抛
        ``CharacterNotFoundError``。"""
        self._workspace_service.info(workspace_id)
        session = self._session_factory(workspace_id)
        try:
            existing = _store_get_character(session, character_id)
            if existing is None or existing.workspace_id != workspace_id:
                raise CharacterNotFoundError(
                    f"角色不存在: workspace={workspace_id} character={character_id}"
                )
            _store_delete_character(session, character_id)
            session.commit()
        except Exception:
            session.rollback()
            raise
        finally:
            session.close()
