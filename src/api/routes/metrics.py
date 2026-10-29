"""Metrics API 路由(P4-B-04)。

OBSERVABILITY §3.2 字面 7 个工作区端点 + 1 个系统级端点:

| 方法 | 路径 | 说明 |
|------|------|------|
| GET | ``/workspaces/{id}/metrics/overview``       | 概览卡片数据 |
| GET | ``/workspaces/{id}/metrics/daily``          | 每日聚合范围(``since=&until=``) |
| GET | ``/workspaces/{id}/metrics/per-chapter``    | 分章详细(``order=``) |
| GET | ``/workspaces/{id}/metrics/tension-curve``  | 张力曲线(target vs actual) |
| GET | ``/workspaces/{id}/metrics/cost-breakdown`` | 按 writer_model 分组 |
| GET | ``/workspaces/{id}/metrics/memory-health``  | 记忆健康度历史(P4-B-09 后有数据) |
| GET | ``/workspaces/{id}/metrics/style-audits``   | 风格漂移历史(P4-B-10 stub) |
| GET | ``/system/metrics``                         | 系统级今日 + 历史 |

URL 形态选 A(嵌套子资源,对齐 P3-B-10 config 路由模式):workspace_id 走
URL path,不要 ``X-Workspace-Id`` header — 端点字面就是"指定工作区的指标"。

回退 / 错误语义
---------------

- 工作区不存在 → ``WORKSPACE_NOT_FOUND (404)``(由 ``WorkspaceServiceError``
  全局翻译器兜底)。
- 工作区存在但 metrics 表为空 → 200 + 空数组 / 0 字段(语义:还没有完成
  到 APPROVED 的章节)。
- ``/system/metrics`` 永远 200(系统级,无 workspace 校验)。

P4 简化
-------

- ``per-chapter`` 不支持 ``volume=`` 过滤(metrics_per_chapter 无 volume_id
  字段;P5+ 加 chapter_id → volume_id 反查后再实现)。
- ``tension-curve`` ``actual_tension_estimate`` 字段当前 None(P5+ Reviewer
  接入估计字段后填),前端按 None 渲染"待实现"占位。
- ``writer_profile`` 字段按"profile == model 字面相等"渲染(P4-B-03 类 A
  fallback;P5+ ChapterVersionORM.writer_profile_id 透传后切换)。
"""

from __future__ import annotations

import datetime as _dt
from typing import Literal

from fastapi import APIRouter, Depends, Query

from src.api.deps import _make_session_factory
from src.api.schemas.common import SuccessEnvelope
from src.api.schemas.metrics import (
    CostBreakdownOut,
    CostBucket,
    DailyListOut,
    MemoryHealthListOut,
    MemoryHealthOut,
    MetricsDailyOut,
    OverviewOut,
    PerChapterListOut,
    PerChapterOut,
    StyleAuditListOut,
    StyleAuditOut,
    SystemMetricsDailyOut,
    SystemMetricsOut,
    TensionCurveOut,
    TensionPoint,
)
from src.services.metrics_service import MetricsService
from src.services.workspace_service import WorkspaceService

# 系统级端点单独挂在根前缀(/system/metrics 不归 /workspaces 子资源)
workspace_router = APIRouter(prefix="/workspaces", tags=["metrics"])
system_router = APIRouter(prefix="/system", tags=["metrics"])


def get_metrics_service() -> MetricsService:
    """构造默认 ``MetricsService``,从 ``settings.data_dir`` 推算 session 工厂。

    测试通过 ``app.dependency_overrides[get_metrics_service] = lambda: ...``
    注入指向 ``tmp_path`` 的 service,避免污染本机 settings.data_dir。
    """
    workspace_service = WorkspaceService()
    session_factory = _make_session_factory(workspace_service.workspaces_root)
    return MetricsService(
        workspace_service=workspace_service,
        session_factory=session_factory,
        system_session_factory=None,
    )


@workspace_router.get(
    "/{workspace_id}/metrics/overview",
    response_model=SuccessEnvelope[OverviewOut],
    summary="metrics 概览(累计 + 今日)",
)
async def get_overview(
    workspace_id: str,
    service: MetricsService = Depends(get_metrics_service),
) -> SuccessEnvelope[OverviewOut]:
    payload = service.get_overview(workspace_id)
    return SuccessEnvelope(data=OverviewOut.model_validate(payload))


@workspace_router.get(
    "/{workspace_id}/metrics/daily",
    response_model=SuccessEnvelope[DailyListOut],
    summary="metrics_daily 每日聚合范围",
)
async def list_daily(
    workspace_id: str,
    since: _dt.date | None = Query(None, description="起始日期(inclusive)"),
    until: _dt.date | None = Query(None, description="终止日期(inclusive)"),
    service: MetricsService = Depends(get_metrics_service),
) -> SuccessEnvelope[DailyListOut]:
    rows = service.list_daily(workspace_id, since=since, until=until)
    items = [MetricsDailyOut.model_validate(r) for r in rows]
    return SuccessEnvelope(data=DailyListOut(items=items))


@workspace_router.get(
    "/{workspace_id}/metrics/per-chapter",
    response_model=SuccessEnvelope[PerChapterListOut],
    summary="metrics_per_chapter 分章详细",
)
async def list_per_chapter(
    workspace_id: str,
    order: Literal[
        "approved_at_desc", "approved_at_asc", "chapter_id_asc"
    ] = Query("approved_at_desc", description="排序方式"),
    service: MetricsService = Depends(get_metrics_service),
) -> SuccessEnvelope[PerChapterListOut]:
    rows = service.list_per_chapter(workspace_id, order=order)
    items = [PerChapterOut.model_validate(r) for r in rows]
    return SuccessEnvelope(data=PerChapterListOut(items=items))


@workspace_router.get(
    "/{workspace_id}/metrics/tension-curve",
    response_model=SuccessEnvelope[TensionCurveOut],
    summary="张力曲线(目标 vs 实际)",
)
async def get_tension_curve(
    workspace_id: str,
    service: MetricsService = Depends(get_metrics_service),
) -> SuccessEnvelope[TensionCurveOut]:
    points = service.get_tension_curve(workspace_id)
    return SuccessEnvelope(
        data=TensionCurveOut(
            points=[TensionPoint.model_validate(p) for p in points]
        )
    )


@workspace_router.get(
    "/{workspace_id}/metrics/cost-breakdown",
    response_model=SuccessEnvelope[CostBreakdownOut],
    summary="按 writer_model 分组的成本",
)
async def get_cost_breakdown(
    workspace_id: str,
    service: MetricsService = Depends(get_metrics_service),
) -> SuccessEnvelope[CostBreakdownOut]:
    buckets = service.get_cost_breakdown(workspace_id)
    return SuccessEnvelope(
        data=CostBreakdownOut(
            buckets=[CostBucket.model_validate(b) for b in buckets]
        )
    )


@workspace_router.get(
    "/{workspace_id}/metrics/memory-health",
    response_model=SuccessEnvelope[MemoryHealthListOut],
    summary="记忆健康度历史(P4-B-09 后有数据)",
)
async def list_memory_health(
    workspace_id: str,
    limit: int = Query(50, ge=1, le=200),
    service: MetricsService = Depends(get_metrics_service),
) -> SuccessEnvelope[MemoryHealthListOut]:
    items = service.list_memory_health(workspace_id, limit=limit)
    return SuccessEnvelope(
        data=MemoryHealthListOut(
            items=[MemoryHealthOut.model_validate(i) for i in items]
        )
    )


@workspace_router.get(
    "/{workspace_id}/metrics/style-audits",
    response_model=SuccessEnvelope[StyleAuditListOut],
    summary="风格漂移历史(P4-B-10 stub)",
)
async def list_style_audits(
    workspace_id: str,
    limit: int = Query(50, ge=1, le=200),
    service: MetricsService = Depends(get_metrics_service),
) -> SuccessEnvelope[StyleAuditListOut]:
    items = service.list_style_audits(workspace_id, limit=limit)
    return SuccessEnvelope(
        data=StyleAuditListOut(
            items=[StyleAuditOut.model_validate(i) for i in items]
        )
    )


@system_router.get(
    "/metrics",
    response_model=SuccessEnvelope[SystemMetricsOut],
    summary="系统级 metrics(今日 + 最近 30 天)",
)
async def get_system_metrics(
    history_days: int = Query(30, ge=1, le=365),
    service: MetricsService = Depends(get_metrics_service),
) -> SuccessEnvelope[SystemMetricsOut]:
    payload = service.get_system_metrics(history_days=history_days)
    return SuccessEnvelope(
        data=SystemMetricsOut(
            today=SystemMetricsDailyOut.model_validate(payload["today"]),
            history=[
                SystemMetricsDailyOut.model_validate(h) for h in payload["history"]
            ],
        )
    )
