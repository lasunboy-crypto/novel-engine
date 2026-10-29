"""角色 CRUD 请求 / 响应 schema(P2-B-14)。

对齐 PROJECT_SPEC §3.3 ``Character`` / ``VoiceProfile`` 模型 + API_CONTRACT
§4 URL 规范(顶级资源 ``/characters``,工作区上下文走 ``X-Workspace-Id``
header)。

设计要点
========

- ``CharacterOut`` 直接继承 ``Character``(空子类),沿用 ``WorkspaceOut`` /
  ``ChapterCardOut`` 模式:字段完全一致,保留 API 表面独立演进坐标
  (P2+ 可能加 ``_links`` / 引用计数等字段)。
- POST / PUT 请求体直接用 ``Character``(P1-B-19 ``PUT /chapters/{id}/card``
  同模式),路由层校验 ``body.workspace_id`` / ``body.character_id`` 与 URL /
  Header 一致;不一致抛 ``CharacterWorkspaceMismatch`` /
  ``CharacterIdMismatch`` 422。
- ``CharacterDeleteResult``:DELETE 成功响应 data 段,与
  ``WorkspaceCleanResult`` / ``WorkspacePurgeResult`` 同形态,只回显
  ``character_id``。
"""

from __future__ import annotations

from pydantic import BaseModel, ConfigDict

from src.core.models.character import Character


class CharacterOut(Character):
    """``GET /characters`` / ``GET /characters/{id}`` 响应 data 段(空子类)。"""


class CharacterDeleteResult(BaseModel):
    """``DELETE /characters/{id}`` 成功响应 data 段。"""

    model_config = ConfigDict(extra="forbid")

    character_id: str
