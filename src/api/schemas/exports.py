"""导出路由请求 / 响应 schema(P6-B-04)。

对应 ``POST /api/v1/workspaces/{id}/exports`` /
``GET /api/v1/workspaces/{id}/exports`` /
``GET /api/v1/workspaces/{id}/exports/{export_id}/download`` 3 端点
(对齐 plan §"P6 端点表汇总" 10/11/12 + plan §"P6-B-04" 字面)。

format 4 档语义(plan §"P6-B-04" + 决策 5=A):

- ``md`` = 全章拼接到单 ``book.md``(带 ``# 卷 1`` / ``## 第 1 章`` 二级标题)。
- ``chaptered_md`` = 每章一文件 ``volume-{N}/chapter-{NNNN}.md`` + 索引 ``index.md``。
- ``epub`` / ``docx`` 在 P6-B-04 占位返 422 ``EXPORT_FORMAT_UNSUPPORTED``;
  分别在 P6-B-05 / P6-B-06 接力翻 200(对齐 plan §"P6-B-04" 字面"本任务只
  实现 md / chaptered_md")。

scope 3 档语义(plan §"P6-B-04" 字面):

- ``full`` = 全书所有章节(invalidated 章节默认跳过)。
- ``volume`` = 指定 ``volume_id`` 下所有章节(invalidated 章节默认跳过)。
- ``range`` = ``[start_chapter_id, end_chapter_id]`` 闭区间;
  范围内含 invalidated 章节时返 422 ``EXPORT_SCOPE_INVALID``(决策 3=A
  invalidated 档语义 — user 必须先 unblock / rewind 链清理)。

跨字段约束在 service 层 ``ExportService.create_export`` 校验(scope 与
volume_id / start / end 的映射 + invalidated 章节扫描);Pydantic 层只做
类型与基础长度校验,避免 schema 与 service 双份校验漂移。
"""

from __future__ import annotations

from datetime import datetime
from typing import Literal

from pydantic import BaseModel, ConfigDict, Field


# ---- 字面类型 ---------------------------------------------------------------


ExportFormat = Literal["md", "chaptered_md", "epub", "docx"]
"""导出格式 4 档(plan §"P6-B-04" + 决策 5=A)。

epub / docx 在本任务返 422 占位,P6-B-05 / P6-B-06 接力翻 200。
"""


ExportScope = Literal["full", "volume", "range"]
"""导出范围 3 档(plan §"P6-B-04" 字面)。"""


ExportStatus = Literal["pending", "running", "done", "failed"]
"""导出任务状态 4 档(对齐既有 ``TaskStatus`` 4 档命名)。"""


# ---- 请求体 ------------------------------------------------------------------


class ExportCreateIn(BaseModel):
    """``POST /api/v1/workspaces/{id}/exports`` 请求体。

    跨字段约束在 service 层 ``ExportService.create_export`` 校验:

    - ``scope=full``:无额外字段必填。
    - ``scope=volume``:``volume_id`` 必填。
    - ``scope=range``:``start_chapter_id`` + ``end_chapter_id`` 必填。

    违反 → ``EXPORT_SCOPE_INVALID 422``。
    """

    model_config = ConfigDict(extra="forbid")

    format: ExportFormat = Field(
        ..., description="导出格式 4 档,见 ExportFormat"
    )
    scope: ExportScope = Field(
        ..., description="导出范围 3 档,见 ExportScope"
    )
    volume_id: str | None = Field(
        default=None,
        min_length=1,
        description="卷 ID(scope=volume 必填;其他 scope 忽略)",
    )
    start_chapter_id: str | None = Field(
        default=None,
        min_length=1,
        description="起始章节 ID(scope=range 必填;其他 scope 忽略)",
    )
    end_chapter_id: str | None = Field(
        default=None,
        min_length=1,
        description="结束章节 ID(scope=range 必填;其他 scope 忽略)",
    )
    include_length_notes: bool = Field(
        True, description="编辑稿在特别长的章节标题后附提示；关闭可导出纯净正文"
    )
    include_metadata: bool = Field(
        default=True,
        description="zip 内是否包含 ``meta.json``(作者 / 字数 / 卷章数等)",
    )
    include_review_history: bool = Field(
        default=False,
        description="是否在导出末尾附 review_history.json(review_results 表行)",
    )


# ---- 响应体 ------------------------------------------------------------------


class ExportCreateOut(BaseModel):
    """``:create`` 端点响应 data 段(返 202 + ``{task_id, export_id, ...}``)。"""

    model_config = ConfigDict(extra="forbid")

    task_id: str = Field(
        ..., description="本次导出后台任务 ID(``task_<16hex>``)"
    )
    export_id: str = Field(
        ..., description="本次导出资源 ID(``exp_<16hex>``)"
    )
    workspace_id: str
    format: ExportFormat
    scope: ExportScope
    status: ExportStatus = Field(
        ..., description="任务初始状态(返 202 时为 ``pending``)"
    )


class ExportItemOut(BaseModel):
    """导出列表项 / 单个导出详情(``GET /exports`` 列表元素 + 内部 manifest)。"""

    model_config = ConfigDict(extra="forbid")

    export_id: str
    workspace_id: str
    format: ExportFormat
    scope: ExportScope
    status: ExportStatus
    created_at: datetime
    completed_at: datetime | None = Field(
        ..., description="任务完成时间;pending / running / failed 时为 None"
    )
    chapter_count: int | None = Field(
        ...,
        description="导出包含的章节数(invalidated 跳过后的);pending 时为 None",
    )
    file_size_bytes: int | None = Field(
        ..., description="zip 文件大小字节;pending / failed 时为 None"
    )
    error: str | None = Field(
        ..., description="任务 failed 时的错误消息(短串);成功时为 None"
    )


class ExportListOut(BaseModel):
    """``GET /api/v1/workspaces/{id}/exports`` 响应 data 段(最近 N 次)。"""

    model_config = ConfigDict(extra="forbid")

    items: list[ExportItemOut] = Field(
        ...,
        description="按 ``created_at`` 倒序的导出条目(plan §P6-B-04 字面最近 10 次)",
    )


__all__ = [
    "ExportCreateIn",
    "ExportCreateOut",
    "ExportFormat",
    "ExportItemOut",
    "ExportListOut",
    "ExportScope",
    "ExportStatus",
]
