"""工作区元数据模型(对应 PROJECT_SPEC §3.1)。"""

from datetime import datetime
from typing import Literal

from pydantic import BaseModel, Field

from src.core.models import WORKSPACE_ID_PATTERN
from src.core.models.style_template import StyleTemplateName


class Workspace(BaseModel):
    """工作区元数据。

    对应工作区目录下 ``meta.json`` 的内容,描述一本小说的全局信息。
    """

    authoring_enabled: bool = False
    workspace_id: str = Field(
        ...,
        pattern=WORKSPACE_ID_PATTERN,
        description="工作区目录名,亦作为唯一标识(字符集见 WORKSPACE_ID_PATTERN)",
    )
    display_name: str = Field(..., description="人类可读的工作区名称")
    genre: str = Field(..., description="题材,如 scifi / fantasy / xianxia")
    premise: str = Field(..., description="一句话设定")
    created_at: datetime = Field(..., description="创建时间(UTC)")
    last_active_at: datetime = Field(..., description="最近一次活动时间(UTC)")
    engine_version: str = Field(..., description="创建该工作区时的引擎版本")
    schema_version: str | None = Field(
        default=None,
        description=(
            "工作区数据库的 Alembic head revision_id(P6-B-08 引入)。"
            "新工作区在 create 时写入当前 head;旧工作区 meta.json 无该字段时为 None,"
            "由 `/workspaces/{id}/version-status` 判定需迁移(对齐 PROJECT_SPEC §11.1)。"
        ),
    )
    status: Literal["active", "archived", "completed"] = Field(
        ..., description="工作区状态"
    )
    total_chapters: int = Field(..., ge=0, description="计划总章数")
    completed_chapters: int = Field(..., ge=0, description="已完成章数")
    config_overrides: dict = Field(
        default_factory=dict, description="工作区级配置覆盖"
    )
    default_voice: Literal["third_person", "first_person", "free"] = Field(
        default="third_person",
        description=(
            "工作区默认人称视角(v1.1 wave 1 session 6 / G10 引入,对齐 "
            "CLAUDE.md §核心硬约束第 11 条 + L2 NewBookPage UI 预留)。"
            "``third_person`` = 项目级默认(中文长篇优先);"
            "``first_person`` = 第一人称(用户显式选);"
            "``free`` = 关闭跨章节视角硬约束,只 warn log(用户选 / 实验)。"
            "默认 ``third_person`` 让既有工作区 backfill 后符合项目级硬约束。"
        ),
    )
    style_template: StyleTemplateName | None = Field(
        default=None,
        description=(
            "工作区写作风格模板(v1.1 wave 4 session 3 / I1 引入)。7 档预设见 "
            "``src/core/models/style_template.py``。``None`` = 不选模板,不生成 "
            "``prompts/style_guide.md``,Writer 退化为既有前章风格指纹跨章基线"
            "隐式控制(向后兼容)。选定后 ``WorkspaceService.create`` 据模板写入 "
            "工作区 ``prompts/style_guide.md``,Writer 运行时读取注入 prompt。"
        ),
    )
