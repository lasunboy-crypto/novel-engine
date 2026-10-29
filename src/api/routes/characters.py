"""角色 CRUD 路由(P2-B-14)。

5 个端点(全部强制 ``X-Workspace-Id`` header):

| 方法 | 路径 | 说明 |
|------|------|------|
| GET    | ``/characters`` | 列出工作区下所有角色 |
| GET    | ``/characters/{character_id}`` | 取单个角色详情 |
| POST   | ``/characters`` | 新建角色 |
| PUT    | ``/characters/{character_id}`` | 全量更新角色 |
| DELETE | ``/characters/{character_id}`` | 删除角色 |

工作区上下文
============

沿用 P1-B-19 决策 1(章节路由首创、本任务起复用到 P2 三类 CRUD):工作区
ID 走 ``X-Workspace-Id`` header,路由依赖 ``Depends(get_workspace_id)`` 解析,
缺失抛 ``MissingWorkspaceHeader (422)``。POST / PUT 请求体内 ``workspace_id``
与 header 不一致 → ``CharacterWorkspaceMismatch (422)``;PUT 请求体内
``character_id`` 与 URL 不一致 → ``CharacterIdMismatch (422)``(类比
``ChapterIdMismatch`` 模式)。

错误码矩阵
==========

| 错误码 | 来源 | 触发场景 |
|--------|------|----------|
| ``MISSING_WORKSPACE_HEADER`` (422) | 路由前置依赖 | header 缺失 |
| ``CHARACTER_WORKSPACE_MISMATCH`` (422) | 路由前置守卫 | body.workspace_id ≠ header |
| ``CHARACTER_ID_MISMATCH`` (422) | 路由前置守卫 | PUT body.character_id ≠ URL |
| ``CHARACTER_NOT_FOUND`` (404) | service 异常翻译 | 不存在或跨工作区 |
| ``CHARACTER_ALREADY_EXISTS`` (409) | service 异常翻译 | POST ID 已被占用 |
| ``WORKSPACE_INVALID`` (422) | service 异常翻译 | meta.json 缺失 / 损坏 |
| ``VALIDATION_FAILED`` (422) | Pydantic | 请求体字段不合 schema |
"""

from __future__ import annotations

from fastapi import APIRouter, Depends, status

from src.api.deps import get_character_service, get_workspace_id
from src.api.errors import CharacterIdMismatch, CharacterWorkspaceMismatch
from src.api.schemas.characters import CharacterDeleteResult, CharacterOut
from src.api.schemas.common import SuccessEnvelope
from src.core.models.character import Character
from src.services.character_service import CharacterService

router = APIRouter(prefix="/characters", tags=["characters"])


@router.get(
    "",
    response_model=SuccessEnvelope[list[CharacterOut]],
    summary="列出工作区下所有角色",
    description="按 character_id 升序返回。需 X-Workspace-Id header。",
)
async def list_characters(
    workspace_id: str = Depends(get_workspace_id),
    service: CharacterService = Depends(get_character_service),
) -> SuccessEnvelope[list[CharacterOut]]:
    chars = service.list_characters(workspace_id)
    return SuccessEnvelope(
        data=[CharacterOut.model_validate(c.model_dump()) for c in chars],
    )


@router.get(
    "/{character_id}",
    response_model=SuccessEnvelope[CharacterOut],
    summary="读取单个角色详情",
    description="不存在或不属于当前工作区(跨工作区视为不存在)→ CHARACTER_NOT_FOUND(404)。",
)
async def get_character(
    character_id: str,
    workspace_id: str = Depends(get_workspace_id),
    service: CharacterService = Depends(get_character_service),
) -> SuccessEnvelope[CharacterOut]:
    char = service.get_character(workspace_id, character_id)
    return SuccessEnvelope(data=CharacterOut.model_validate(char.model_dump()))


@router.post(
    "",
    response_model=SuccessEnvelope[CharacterOut],
    status_code=status.HTTP_201_CREATED,
    summary="新建角色",
    description=(
        "请求体是完整 Character。body.workspace_id 必须等于 X-Workspace-Id header。"
        "character_id 已存在 → CHARACTER_ALREADY_EXISTS(409)。"
    ),
)
async def create_character(
    body: Character,
    workspace_id: str = Depends(get_workspace_id),
    service: CharacterService = Depends(get_character_service),
) -> SuccessEnvelope[CharacterOut]:
    if body.workspace_id != workspace_id:
        raise CharacterWorkspaceMismatch(
            f"请求体内 workspace_id='{body.workspace_id}' 与 header '{workspace_id}' 不一致"
        )
    char = service.create_character(workspace_id, body)
    return SuccessEnvelope(data=CharacterOut.model_validate(char.model_dump()))


@router.put(
    "/{character_id}",
    response_model=SuccessEnvelope[CharacterOut],
    summary="全量更新角色",
    description=(
        "body.character_id 必须等于 URL 中的 character_id;body.workspace_id 必须"
        "等于 X-Workspace-Id header。不存在或跨工作区 → CHARACTER_NOT_FOUND(404)。"
    ),
)
async def update_character(
    character_id: str,
    body: Character,
    workspace_id: str = Depends(get_workspace_id),
    service: CharacterService = Depends(get_character_service),
) -> SuccessEnvelope[CharacterOut]:
    if body.character_id != character_id:
        raise CharacterIdMismatch(
            f"请求体内 character_id='{body.character_id}' 与 URL '{character_id}' 不一致"
        )
    if body.workspace_id != workspace_id:
        raise CharacterWorkspaceMismatch(
            f"请求体内 workspace_id='{body.workspace_id}' 与 header '{workspace_id}' 不一致"
        )
    char = service.update_character(workspace_id, body)
    return SuccessEnvelope(data=CharacterOut.model_validate(char.model_dump()))


@router.delete(
    "/{character_id}",
    response_model=SuccessEnvelope[CharacterDeleteResult],
    summary="删除角色",
    description="不存在或跨工作区 → CHARACTER_NOT_FOUND(404)。",
)
async def delete_character(
    character_id: str,
    workspace_id: str = Depends(get_workspace_id),
    service: CharacterService = Depends(get_character_service),
) -> SuccessEnvelope[CharacterDeleteResult]:
    service.delete_character(workspace_id, character_id)
    return SuccessEnvelope(data=CharacterDeleteResult(character_id=character_id))
