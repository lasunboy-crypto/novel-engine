"""工作区备份 / 恢复请求 / 响应 schema(P6-B-07)。

对应 ``BackupService`` 的对外 API 表面。路由 ``response_model`` 全从本模块取,
**严禁裸 dict**(CLAUDE.md §硬约束 5)。
"""

from __future__ import annotations

from datetime import datetime
from typing import Literal

from pydantic import BaseModel, ConfigDict, Field

BackupTypeLiteral = Literal["auto", "manual"]
"""``GET /workspaces/{id}/backups`` 列表元素的 ``backup_type`` 字面量。

``pre_restore`` 为内部备份不进入 list,故 API 表面只暴露 2 档(对齐
``BackupService.list_backups`` 实现)。
"""


class BackupInfoOut(BaseModel):
    """单份备份的元数据响应。"""

    model_config = ConfigDict(extra="forbid")

    backup_id: str = Field(..., description="备份唯一标识(也是 zip 文件 stem)")
    backup_type: BackupTypeLiteral = Field(..., description="备份类型")
    workspace_id: str
    created_at: datetime = Field(..., description="备份创建时刻(ISO UTC)")
    chapter_id: str | None = Field(
        default=None,
        description="自动备份触发的章节 ID;手动备份为 null",
    )
    schema_version: str = Field(
        ...,
        description="备份创建时 Alembic head revision_id(用于恢复时版本比对)",
    )
    engine_version: str = Field(
        ..., description="备份创建时的引擎版本 src/version.py"
    )
    file_path: str = Field(..., description="备份 zip 文件绝对路径")
    size_bytes: int = Field(..., ge=0, description="zip 文件大小,单位字节")


class BackupListOut(BaseModel):
    """``GET /workspaces/{id}/backups`` 响应 data 段。

    顺序:auto(最近 30 份倒序)+ manual(全部倒序)。
    """

    model_config = ConfigDict(extra="forbid")

    workspace_id: str
    backups: list[BackupInfoOut] = Field(default_factory=list)


class RestoreIn(BaseModel):
    """``POST /workspaces/{id}:restore`` 请求体(P6-B-07 新语义)。

    ``confirm`` 必须等于 URL 中的 ``workspace_id``(二次确认,对齐
    ``PurgeConfirmMismatch`` 同模式 — restore 是覆盖性操作,strict 同款防呆)。
    """

    model_config = ConfigDict(extra="forbid")

    backup_id: str = Field(
        ...,
        min_length=1,
        description="要恢复的备份 ID(``BackupInfoOut.backup_id`` 字面值)",
    )
    confirm: str = Field(
        ...,
        min_length=1,
        description="二次确认字段,值必须等于 URL 中的 workspace_id",
    )


class RestoreResultOut(BaseModel):
    """``:restore`` 成功响应 data 段。"""

    model_config = ConfigDict(extra="forbid")

    workspace_id: str
    backup_id: str = Field(..., description="本次恢复使用的备份 ID")
    pre_restore_backup_id: str = Field(
        ...,
        description=(
            "恢复前对原工作区物理备份产生的 ``pre_restore_{ts}`` ID,"
            "用于撤销恢复(后续 P6-B-08 数据迁移流程接入)"
        ),
    )


class PurgeBackupsIn(BaseModel):
    """``POST /workspaces/{id}:purge-backups`` 请求体(v1.1 w3-s8 F1)。

    ``confirm`` 必须等于 URL 中的 ``workspace_id``(二次确认,对齐 ``RestoreIn``
    同模式 — 清空备份是破坏性操作,strict 防呆)。
    """

    model_config = ConfigDict(extra="forbid")

    confirm: str = Field(
        ...,
        min_length=1,
        description="二次确认字段,值必须等于 URL 中的 workspace_id",
    )


class PurgeBackupsResultOut(BaseModel):
    """``:purge-backups`` 成功响应 data 段(v1.1 w3-s8 F1)。"""

    model_config = ConfigDict(extra="forbid")

    workspace_id: str
    deleted_count: int = Field(
        ..., ge=0, description="本次删除的备份份数(auto + manual + pre_restore)"
    )
    freed_bytes: int = Field(
        ..., ge=0, description="本次释放的磁盘空间,单位字节"
    )


__all__ = [
    "BackupInfoOut",
    "BackupListOut",
    "BackupTypeLiteral",
    "PurgeBackupsIn",
    "PurgeBackupsResultOut",
    "RestoreIn",
    "RestoreResultOut",
]
