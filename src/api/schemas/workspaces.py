"""工作区相关请求 / 响应 schema(P1-B-18)。

对齐 PROJECT_SPEC §3.1 ``Workspace`` 模型与 §9 工作区生命周期 9 个动作的 API
形态;路由层(``src/api/routes/workspaces.py``)的所有 ``response_model`` 都从
本模块取,**严禁裸 dict**(CLAUDE.md 硬约束 §5)。

设计要点
========

- ``WorkspaceOut`` 直接继承 ``Workspace``(空子类),既复用字段又给 API 表面
  保留独立类型坐标,后续要分化(如补 ``_links`` / ETag)只改本模块。
- 动作类响应(archive / backup / clean / purge)不复用 ``WorkspaceOut``,各自
  携带语义最小字段(归档路径、备份路径等)。绝对路径暴露在 P1 默认 127.0.0.1
  本机调试场景下安全;P3+ 公网模式应改成相对项目根的相对路径或不暴露。
- ``WorkspacePurgeIn.confirm`` 是 ``str``(对齐 PROJECT_SPEC §9.3 字面"API 传
  ``confirm=<workspace_id>``"),路由层前置校验 ``confirm == workspace_id``
  不匹配抛 ``PurgeConfirmMismatch``(404 之外的"操作不该执行的事前阻止")。
"""

from __future__ import annotations

from typing import Literal

from pydantic import BaseModel, ConfigDict, Field

from src.core.models import WORKSPACE_ID_PATTERN
from src.core.models.style_template import StyleTemplateName
from src.core.models.workspace import Workspace


class WorkspaceOut(Workspace):
    """工作区元数据响应体(单个或列表元素)。

    v1.0.x wave 6 s1+s4 起加 ``outline_status`` API 表面专属字段(对齐 schema
    设计要点 — "P2+ 可能加 API 表面专属字段"扩展点);该字段不入 ``Workspace``
    Pydantic 与 meta.json,源自 ``WorkspaceORM.outline_status``(P5-B-07
    引入 3 档 DB 运行态)。仅 ``GET /workspaces/{id}`` 端点填真值,其他端点
    (``POST /workspaces`` create / ``:switch`` / ``:archive`` / ``GET
    /workspaces`` list)默认 ``"not_started"`` 不真查(避免 N 次 SQL 性能影响)。
    OutlinePage 据此选三档状态分支(未生成 / 已生成未 commit / 已 commit)。
    """

    outline_status: Literal["not_started", "generating", "committed"] = Field(
        default="not_started",
        description=(
            "outline 流转状态(复用 P5-B-07 ``WorkspaceORM.outline_status`` 3 "
            "档枚举)。前端 OutlinePage 据此渲染三档状态分支:``not_started`` "
            "→ 显示'生成大纲'按钮 + 引导写 project.yaml(wave 6 s1 X1 时序"
            "修复);``generating`` → 显示工作流面板(生成中接 WS 进度,完成"
            "后显示'确认并保存'等按钮);``committed`` → 显示当前 OutlinePage "
            "既有 UI(预览 + 编辑卷规划)。仅 ``GET /workspaces/{id}`` 端点填"
            "真值,其他端点默认 ``not_started``。"
        ),
    )


# ---- 请求体 ------------------------------------------------------------------


class WorkspaceCreateIn(BaseModel):
    """``POST /api/v1/workspaces`` 请求体。"""

    model_config = ConfigDict(extra="forbid")

    workspace_id: str = Field(
        ...,
        pattern=WORKSPACE_ID_PATTERN,
        description=(
            "工作区目录名,亦作为唯一标识(对齐 PROJECT_SPEC §3.1)。"
            "字符集与长度见 ``src.core.models.WORKSPACE_ID_PATTERN``,防 path traversal。"
        ),
    )
    display_name: str = Field(..., min_length=1, description="人类可读的工作区名称")
    genre: str = Field(..., min_length=1, description="题材")
    premise: str = Field(..., min_length=1, description="一句话设定")
    total_chapters: int = Field(default=0, ge=0, description="计划总章数")
    default_voice: Literal["third_person", "first_person", "free"] = Field(
        default="third_person",
        description=(
            "工作区默认人称视角(v1.1 wave 3 s4 / L2,对齐 CLAUDE.md §核心硬约束"
            "第 11 条 + G10 cross_chapter_voice_validator)。"
            "``third_person`` = 项目级默认(中文长篇优先);"
            "``first_person`` = 第一人称(用户显式选);"
            "``free`` = 关闭跨章节视角硬约束,只 warn log(用户选 / 实验)。"
            "落到 ``Workspace.default_voice``(meta.json)→ ORM upsert → G10 读。"
        ),
    )
    style_template: StyleTemplateName | None = Field(
        default=None,
        description=(
            "写作风格模板(v1.1 wave 4 session 3 / I1)。7 档预设见 "
            "``src/core/models/style_template.py``;``None`` = 不选(默认)。"
            "落到 ``Workspace.style_template``(meta.json)+ 据模板生成工作区 "
            "``prompts/style_guide.md``,Writer 运行时读取注入。"
        ),
    )
    activate: bool = Field(
        default=True,
        description=(
            "创建后是否同步激活(更新 _active 软链)。默认 True 对齐 CLI "
            "`novel workspace create` 直觉;批量导入或测试场景显式传 false。"
        ),
    )


class WorkspacePurgeIn(BaseModel):
    """``POST /api/v1/workspaces/{id}:purge`` 请求体。

    ``confirm`` 必须等于 URL 中的 ``workspace_id``(PROJECT_SPEC §9.3 二次确认)。
    """

    model_config = ConfigDict(extra="forbid")

    confirm: str = Field(
        ...,
        min_length=1,
        description="二次确认字段,值必须等于 URL 中的 workspace_id",
    )


class WorkspaceDestroyIn(BaseModel):
    """``POST /api/v1/workspaces/{id}:destroy`` 请求体(P7-B-07)。

    ``confirm`` 必须等于 URL 中的 ``workspace_id``(对齐 ``WorkspacePurgeIn``
    + ``RestoreIn`` 同模式 — 不可逆操作 strict 二次确认)。
    """

    model_config = ConfigDict(extra="forbid")

    confirm: str = Field(
        ...,
        min_length=1,
        description="二次确认字段,值必须等于 URL 中的 workspace_id",
    )


# ---- 动作类响应 data 段 ------------------------------------------------------


class WorkspaceArchiveResult(BaseModel):
    """``:archive`` 成功响应 data 段。"""

    model_config = ConfigDict(extra="forbid")

    workspace_id: str
    archive_path: str = Field(..., description="项目级 archives/<id>.tar.gz 绝对路径")


class WorkspaceBackupResult(BaseModel):
    """``:backup`` 成功响应 data 段。"""

    model_config = ConfigDict(extra="forbid")

    workspace_id: str
    backup_path: str = Field(
        ..., description="工作区内 backups/<id>-<timestamp>.tar.gz 绝对路径"
    )


class WorkspaceCleanResult(BaseModel):
    """``:clean`` 成功响应 data 段。无副作用产物,仅回显 workspace_id。"""

    model_config = ConfigDict(extra="forbid")

    workspace_id: str


class WorkspacePurgeResult(BaseModel):
    """``:purge`` 成功响应 data 段。工作区已物理删除,仅回显 workspace_id。"""

    model_config = ConfigDict(extra="forbid")

    workspace_id: str


class WorkspaceDestroyResult(BaseModel):
    """``:destroy`` 成功响应 data 段(P7-B-07)。

    ``deleted_paths`` 记录实际删除的绝对路径列表(0-2 项 — 工作区目录必含 +
    顶级 archive 若存在)。前端可据此 toast 给用户精准反馈,审计也能从日志
    复盘"实际清理了哪些文件"。
    """

    model_config = ConfigDict(extra="forbid")

    workspace_id: str
    deleted_paths: list[str] = Field(
        ...,
        description=(
            "实际删除的绝对路径列表;0-2 项 — 工作区目录(含工作区内 backups/)"
            "必含,顶级 archives/<id>.tar.gz 若存在亦含"
        ),
    )
