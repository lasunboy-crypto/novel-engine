"""健康检查路由(P1-B-17,P4-acceptance 升级,P4-acceptance-followup 加
workspace 5 项)。

对齐 API_CONTRACT §4.1 + OBSERVABILITY §6 字面 10 项健康检查。

**P4-acceptance 升级**:6 项全局检查 + 总判定 ``status=ok|degraded|down``。
**P4-acceptance-followup 升级**:`?workspace_id=` query 参数触发 5 项工作区
检查(workspace_sqlite / workspace_chroma / embedding / backup_dir /
engine_version_compat),余下仅 1 项 ``claude_api_reachable`` 留 v2_wishlist
(维护人无 API key)。

向后兼容:``data.status`` / ``data.engine_version`` 字段保留;新增
``checks`` / ``summary`` 字段(P1 客户端可忽略未识别字段不受影响)。
"""

from __future__ import annotations

from typing import Any

from fastapi import APIRouter, Query
from pydantic import BaseModel, ConfigDict

from src.api.schemas.common import SuccessEnvelope
from src.services.health_service import run_health_checks

router = APIRouter(tags=["system"])


class HealthCheckItemOut(BaseModel):
    """单项检查结果(OBSERVABILITY §6 字面)。"""

    model_config = ConfigDict(extra="forbid")

    name: str
    outcome: str  # pass | fail | skip
    detail: str | None = None


class HealthSummary(BaseModel):
    """6 项检查的合计计数。"""

    model_config = ConfigDict(extra="forbid")

    pass_count: int
    fail_count: int
    skip_count: int


class HealthData(BaseModel):
    """``GET /api/v1/system/health`` 响应体 ``data`` 段(P4-acceptance 升级)。"""

    model_config = ConfigDict(extra="forbid")

    status: str  # ok | degraded | down
    engine_version: str
    checks: list[HealthCheckItemOut] = []
    summary: HealthSummary | None = None


@router.get(
    "/system/health",
    response_model=SuccessEnvelope[HealthData],
    summary="健康检查",
    description=(
        "返回引擎健康状态。`?workspace_id=...` 给定时附带 5 项工作区检查"
        "(P4-acceptance-followup 升级);未给只跑 6 项全局检查。"
        "status=ok 全过 | degraded 有失败但非关键项 | down 关键项失败。"
        "claude_api_reachable 留 v2_wishlist(维护人无 API key)。"
    ),
)
async def health(
    workspace_id: str | None = Query(
        None,
        description="工作区 ID;给定时附带 5 项工作区检查(SQLite/Chroma/embedding/backup/engine_version_compat)",
    ),
) -> SuccessEnvelope[HealthData]:
    report: dict[str, Any] = run_health_checks(workspace_id=workspace_id)
    return SuccessEnvelope(data=HealthData.model_validate(report))
