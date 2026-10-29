"""工作区版本状态查询与迁移请求 / 响应 schema(P6-B-08)。

对应 ``WorkspaceVersionService`` 的对外 API 表面。路由 ``response_model`` 全
从本模块取,**严禁裸 dict**(CLAUDE.md §硬约束 5)。
"""

from __future__ import annotations

from pydantic import BaseModel, ConfigDict, Field


class VersionStatusOut(BaseModel):
    """``GET /workspaces/{id}/version-status`` 响应 data 段。

    对齐 PROJECT_SPEC §11.1 字面 7 字段(engine 双轨 + schema 双轨 + match 标识
    + ``migration_required`` 顶层判定)。
    """

    model_config = ConfigDict(extra="forbid")

    workspace_id: str
    engine_version_match: bool = Field(
        ..., description="workspace meta.engine_version 与当前引擎是否一致"
    )
    schema_version_match: bool = Field(
        ...,
        description=(
            "workspace meta.schema_version 与当前 Alembic head revision_id "
            "是否一致(workspace_schema 为 null 视为不一致 → 需迁移)"
        ),
    )
    current_engine: str = Field(
        ..., description="当前引擎版本(``src/version.py`` ENGINE_VERSION)"
    )
    workspace_engine: str = Field(
        ..., description="工作区 meta.json 中的 engine_version"
    )
    current_schema: str = Field(
        ..., description="当前 Alembic head revision_id 字符串"
    )
    workspace_schema: str | None = Field(
        default=None,
        description=(
            "工作区 meta.json 中的 schema_version;旧工作区(P6-B-08 前创建)"
            "无该字段 → null"
        ),
    )
    migration_required: bool = Field(
        ...,
        description=(
            "engine_version_match / schema_version_match 任一 false 即 true,"
            "前端据此决定是否提示 doctor / migrate"
        ),
    )


class MigrateIn(BaseModel):
    """``POST /workspaces/{id}:migrate`` 请求体。

    ``confirm`` 必须等于 URL 中的 ``workspace_id``(对齐 ``PurgeConfirmMismatch``
    / ``RestoreConfirmMismatch`` 同模式 — migrate 是覆盖性操作,strict 二次确认
    防呆)。
    """

    model_config = ConfigDict(extra="forbid")

    confirm: str = Field(
        ...,
        min_length=1,
        description="二次确认字段,值必须等于 URL 中的 workspace_id",
    )


class MigrateResultOut(BaseModel):
    """``POST :migrate`` 成功响应 data 段。"""

    model_config = ConfigDict(extra="forbid")

    workspace_id: str
    pre_migration_backup_id: str = Field(
        ...,
        description=(
            "迁移前自动创建的 manual 备份 ID(对齐 PROJECT_SPEC §11.2 字面"
            "'migrate 必须有 rollback 后路')"
        ),
    )
    from_engine_version: str = Field(
        ..., description="迁移前 workspace.engine_version"
    )
    to_engine_version: str = Field(
        ..., description="迁移后 workspace.engine_version(== 当前引擎)"
    )
    from_schema_version: str | None = Field(
        default=None,
        description="迁移前 workspace.schema_version(可能为 null 旧工作区)",
    )
    to_schema_version: str = Field(
        ...,
        description="迁移后 workspace.schema_version(== 当前 Alembic head)",
    )


__all__ = [
    "MigrateIn",
    "MigrateResultOut",
    "VersionStatusOut",
]
