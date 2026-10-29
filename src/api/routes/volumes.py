"""Volumes 路由(P5-B-09)。

对齐 plan §"P5-B-09" 字面 + FRONTEND_SPEC §4.6 字面 + P5-B-01 ``Volume``
模型 + P5-B-07 ``WorkspaceORM.outline_status`` 字段。

5 个端点
========

| 方法 | 路径 | 作用 |
|------|------|------|
| GET  | ``/workspaces/{id}/volumes`` | 列工作区所有卷 |
| GET  | ``/workspaces/{id}/volumes/{vid}`` | 读单卷(含 arcs / tension_curve)|
| PUT  | ``/workspaces/{id}/volumes/{vid}`` | patch 卷字段 |
| POST | ``/workspaces/{id}/volumes/{vid}:apply-template`` | 一键应用张力模板 |
| GET  | ``/workspaces/{id}/outline:status`` | 读 ``WorkspaceORM.outline_status`` |

工作区上下文
============

与 ``outline.router`` 同模板:``workspace_id`` 走 URL path,**不要**
``X-Workspace-Id`` header — 端点字面就是"为指定工作区操作卷"。

职责边界
========

路由层只做:参数解析 + 调 service + 包装响应(对齐 CLAUDE.md §实施纪律
"路由 ≤ 30 行/接口")。CRUD / 字典查表 / 落 ORM 全在 ``VolumeService``
集中。
"""

from __future__ import annotations

from fastapi import APIRouter, Depends

from src.api.deps import get_volume_service
from src.api.schemas.common import SuccessEnvelope
from src.api.schemas.volumes import (
    ApplyTemplateIn,
    ApplyTemplateOut,
    OutlineStatusOut,
    VolumeListOut,
    VolumeOut,
    VolumeUpdateIn,
)
from src.services.volume_service import VolumeService, volume_to_out_dict

router = APIRouter(prefix="/workspaces", tags=["volumes"])


# ---- 内部辅助 ---------------------------------------------------------------


def _to_volume_out(volume) -> VolumeOut:  # type: ignore[no-untyped-def]
    """``Volume`` Pydantic → ``VolumeOut``(同字段独立子类构造)。"""
    return VolumeOut(**volume_to_out_dict(volume))


# ---- GET /volumes(列表)----------------------------------------------------


@router.get(
    "/{workspace_id}/volumes",
    response_model=SuccessEnvelope[VolumeListOut],
    summary="列出工作区所有卷",
    description=(
        "返工作区内所有卷,按 ``volume_id`` 升序(确定性顺序)。空工作区"
        "返 ``volumes: []``。工作区不存在 → ``WORKSPACE_INVALID 422``。"
    ),
)
async def list_volumes(
    workspace_id: str,
    service: VolumeService = Depends(get_volume_service),
) -> SuccessEnvelope[VolumeListOut]:
    volumes = service.list_volumes(workspace_id)
    return SuccessEnvelope(
        data=VolumeListOut(volumes=[_to_volume_out(v) for v in volumes])
    )


# ---- GET /volumes/{vid}(单卷)----------------------------------------------


@router.get(
    "/{workspace_id}/volumes/{volume_id}",
    response_model=SuccessEnvelope[VolumeOut],
    summary="读取单卷",
    description=(
        "返单卷完整字段(含 ``arcs`` / ``tension_curve``)。卷不存在或不属于"
        "本工作区 → ``VOLUME_NOT_FOUND 404``。工作区不存在 → "
        "``WORKSPACE_INVALID 422``。"
    ),
)
async def get_volume(
    workspace_id: str,
    volume_id: str,
    service: VolumeService = Depends(get_volume_service),
) -> SuccessEnvelope[VolumeOut]:
    volume = service.get_volume(workspace_id, volume_id)
    return SuccessEnvelope(data=_to_volume_out(volume))


# ---- PUT /volumes/{vid}(patch)---------------------------------------------


@router.put(
    "/{workspace_id}/volumes/{volume_id}",
    response_model=SuccessEnvelope[VolumeOut],
    summary="更新单卷(patch 字段)",
    description=(
        "patch 语义:仅非 None 字段被更新,其余保留原值;允许 patch 的字段"
        "见 ``VolumeUpdateIn``(``volume_id`` / ``workspace_id`` / 时间戳"
        "不可改)。**不变式**:``Volume.model_validate`` 整体校验 patch 后"
        "新值(``tension_curve`` 长度 == ``chapter_count`` / arc 区间 ≤ "
        "chapter_count / arc_id 唯一);违规 → ``WORKSPACE_INVALID 422``。"
    ),
)
async def update_volume(
    workspace_id: str,
    volume_id: str,
    body: VolumeUpdateIn,
    service: VolumeService = Depends(get_volume_service),
) -> SuccessEnvelope[VolumeOut]:
    patch = body.model_dump(exclude_unset=True, mode="python")
    volume = service.update_volume(workspace_id, volume_id, patch)
    return SuccessEnvelope(data=_to_volume_out(volume))


# ---- POST /volumes/{vid}:apply-template ------------------------------------


@router.post(
    "/{workspace_id}/volumes/{volume_id}:apply-template",
    response_model=SuccessEnvelope[ApplyTemplateOut],
    summary="一键应用张力模板",
    description=(
        "根据模板字典(``src/core/models/tension_curve.py`` P5-B-01 内置 4 档)"
        "线性插值缩放到 ``Volume.chapter_count`` 长度,填充 "
        "``tension_curve_json`` + 同步 ``tension_template`` 字段;"
        "**不动 ``arcs_json``**(对齐 plan §P5-B-09 字面 + FRONTEND_SPEC §4.6 "
        "字面 — 模板下拉只更新张力曲线,arcs 是创意决策不应被模板覆盖)。"
        "``is_arc_finale`` 标志位通过既有 arcs 末位章号计算。卷不存在 → "
        "``VOLUME_NOT_FOUND 404``。"
    ),
)
async def apply_template_to_volume(
    workspace_id: str,
    volume_id: str,
    body: ApplyTemplateIn,
    service: VolumeService = Depends(get_volume_service),
) -> SuccessEnvelope[ApplyTemplateOut]:
    _, curve = service.apply_template(workspace_id, volume_id, body.template)
    return SuccessEnvelope(
        data=ApplyTemplateOut(
            volume_id=volume_id,
            template=body.template,
            tension_curve=curve,
        )
    )


# ---- GET /outline:status(顺手加,plan §P5-B-09 ④ + P5-B-07/08 §6 marker)---


@router.get(
    "/{workspace_id}/outline:status",
    response_model=SuccessEnvelope[OutlineStatusOut],
    summary="读取 outline 当前状态",
    description=(
        "暴露 ``WorkspaceORM.outline_status``(P5-B-07 引入,3 档 "
        "``not_started`` / ``generating`` / ``committed``);ORM 行不存在 → "
        "返 ``not_started``(对齐 alembic 0007 字面默认值)。"
        "前端 P5-F-02 NewBookPage 用此判断是否禁用'生成'按钮 / 跳转 "
        "OutlinePage。"
    ),
)
async def get_outline_status(
    workspace_id: str,
    service: VolumeService = Depends(get_volume_service),
) -> SuccessEnvelope[OutlineStatusOut]:
    status = service.get_outline_status(workspace_id)
    return SuccessEnvelope(
        data=OutlineStatusOut(
            workspace_id=workspace_id,
            outline_status=status,
        )
    )
