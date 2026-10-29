"""Volume 路由请求 / 响应 schema(P5-B-09)。

对齐 ``src/core/models/volume.py`` Pydantic 模型 + plan §"P5-B-09" 字面 +
FRONTEND_SPEC §4.6 字面"曲线模板下拉,可一键应用模板"。

5 个端点的 schema
=================

- ``GET /workspaces/{id}/volumes`` — 响应 ``VolumeListOut``(``volumes: list[VolumeOut]``)
- ``GET /workspaces/{id}/volumes/{vid}`` — 响应 ``VolumeOut``(单卷含 arcs / tension_curve)
- ``PUT /workspaces/{id}/volumes/{vid}`` — body ``VolumeUpdateIn``(patch),响应 ``VolumeOut``
- ``POST /workspaces/{id}/volumes/{vid}:apply-template`` — body ``ApplyTemplateIn``,
  响应 ``ApplyTemplateOut``;**只填 tension_curve_json + 同步 tension_template
  字段**,不动 arcs_json(对齐 plan §P5-B-09 字面"根据模板字典填充
  tension_curve_json" + FRONTEND_SPEC §4.6 字面)
- ``GET /workspaces/{id}/outline:status`` — 响应 ``OutlineStatusOut``(暴露
  ``WorkspaceORM.outline_status`` P5-B-07 引入的 3 档:``not_started`` /
  ``generating`` / ``committed``)
"""

from __future__ import annotations

import datetime as _dt

from pydantic import BaseModel, ConfigDict, Field

from src.core.models.volume import (
    Arc,
    TensionCurvePoint,
    TensionTemplateName,
)


# =============================================================================
# 响应 schema
# =============================================================================


class VolumeOut(BaseModel):
    """单卷响应 data 段(对齐 ``Volume`` Pydantic 字段)。

    与 ``Volume`` 1:1 字段对齐;独立子类(而非 ``Volume`` 别名)留出未来
    API 层独立演化空间(例如 P6+ 加 ``last_committed_at`` 等 API-only 派生
    字段)。
    """

    model_config = ConfigDict(extra="forbid")

    volume_id: str = Field(..., description="卷 ID(工作区内唯一)")
    workspace_id: str = Field(..., description="所属工作区 ID")
    title: str = Field(..., description="卷标题")
    summary: str = Field(..., description="卷概要")
    chapter_count: int = Field(..., ge=1, description="卷内章节总数")
    tension_template: TensionTemplateName = Field(
        ..., description="张力曲线模板(4 档)"
    )
    arcs: list[Arc] = Field(
        default_factory=list, description="卷内弧列表(可空)"
    )
    tension_curve: list[TensionCurvePoint] = Field(
        default_factory=list,
        description=(
            "卷内每章张力点列表;非空时长度 == chapter_count(后端校验)"
        ),
    )
    created_at: _dt.datetime = Field(..., description="创建时间 ISO 8601 UTC")
    last_updated_at: _dt.datetime = Field(
        ..., description="最近更新时间 ISO 8601 UTC"
    )


class VolumeListOut(BaseModel):
    """``GET /workspaces/{id}/volumes`` 响应 data 段。"""

    model_config = ConfigDict(extra="forbid")

    volumes: list[VolumeOut] = Field(
        default_factory=list, description="工作区内所有卷(空工作区返空列表)"
    )


# =============================================================================
# 请求 schema
# =============================================================================


class VolumeUpdateIn(BaseModel):
    """``PUT /workspaces/{id}/volumes/{vid}`` 请求体。所有字段 optional patch。

    **patch 语义**:仅非 None 字段被更新;其余保留原值。**校验时机**:
    服务层把 patch 合并入 ORM 现值后用 ``Volume.model_validate`` 整体校验,
    确保不变式(``tension_curve`` 非空时长度 == ``chapter_count`` /
    arc 区间 ≤ chapter_count / arc_id 唯一)在 patch 后仍成立。

    **不允许 patch 的字段**:``volume_id`` / ``workspace_id`` / ``created_at``
    / ``last_updated_at``(分别为主键 / 外键 / 不可变创建戳 / 由后端管理)。
    """

    model_config = ConfigDict(extra="forbid")

    title: str | None = Field(
        default=None, min_length=1, description="卷标题(可改)"
    )
    summary: str | None = Field(default=None, description="卷概要(可改)")
    chapter_count: int | None = Field(
        default=None,
        ge=1,
        description=(
            "卷内章节总数(改后必须同步改 ``tension_curve`` 长度,否则 422)"
        ),
    )
    tension_template: TensionTemplateName | None = Field(
        default=None, description="张力曲线模板(4 档之一,可改)"
    )
    arcs: list[Arc] | None = Field(
        default=None,
        description="覆盖卷内弧列表(整段替换);需保证 arc_id 唯一 + 区间合法",
    )
    tension_curve: list[TensionCurvePoint] | None = Field(
        default=None,
        description=(
            "覆盖卷内每章张力点列表(整段替换);非空时长度必须 == "
            "新生效的 ``chapter_count``"
        ),
    )


class ApplyTemplateIn(BaseModel):
    """``POST /workspaces/{id}/volumes/{vid}:apply-template`` 请求体。"""

    model_config = ConfigDict(extra="forbid")

    template: TensionTemplateName = Field(
        ..., description="模板名(4 档:heros_journey / steady_daily / strong_stimulation / free)"
    )


class ApplyTemplateOut(BaseModel):
    """``POST /volumes/{vid}:apply-template`` 响应 data 段。

    回显模板名 + 新生成的 ``tension_curve``(N 章 = ``Volume.chapter_count``);
    arcs_json **不在本响应**(本端点字面只动张力曲线 — 对齐 plan §P5-B-09 +
    FRONTEND_SPEC §4.6)。如需读完整卷,客户端再调 ``GET /volumes/{vid}``。
    """

    model_config = ConfigDict(extra="forbid")

    volume_id: str = Field(..., description="目标卷 ID")
    template: TensionTemplateName = Field(
        ..., description="本次应用的模板名(同步落 ``VolumeORM.tension_template``)"
    )
    tension_curve: list[TensionCurvePoint] = Field(
        ..., description="新生成的张力曲线(长度 == 卷的 chapter_count)"
    )


# =============================================================================
# Outline status 端点(P5-B-09 顺手加,plan §P5-B-09 ④ 字面 +
# P5-B-07 §6 / P5-B-08 §6 marker 累积 2 处)
# =============================================================================


class OutlineStatusOut(BaseModel):
    """``GET /workspaces/{id}/outline:status`` 响应 data 段。

    暴露 ``WorkspaceORM.outline_status``(P5-B-07 引入,3 档 ``not_started``
    / ``generating`` / ``committed``);ORM 行不存在时返 ``not_started``
    (对齐 P2/P4 老工作区 alembic 0007 升级前的默认值)。

    **前端用途**(P5-F-02 / P5-F-03):NewBookPage 显示当前 outline 状态;
    禁用"生成"按钮直到 ``not_started``;``committed`` 后跳转 OutlinePage。
    """

    model_config = ConfigDict(extra="forbid")

    workspace_id: str = Field(..., description="工作区 ID(回显)")
    outline_status: str = Field(
        ...,
        description=(
            "outline 当前状态(``not_started`` / ``generating`` / "
            "``committed``,对齐 ``WorkspaceORM.outline_status`` CHECK 3 档)"
        ),
    )
