"""风格漂移审计路由(P4-B-10;P7 完整实现升级位)。

POST ``/api/v1/workspaces/{workspace_id}/style/audit`` — 同步触发一次审计,
返回 stub ``StyleAuditEntry`` 记录(P4 阶段 ``severity="stub"`` /
``drift_score=None``;P7 完整实现时返正式 4 档 severity + 0-10 浮点 drift_score)。

URL 形态选 A(嵌套子资源,对齐 P4-B-04 metrics 路由 + P4-B-09 memory_health
路由模式):``workspace_id`` 走 URL path,不要 ``X-Workspace-Id`` header —
端点字面就是"为指定工作区运行风格审计"。

错误语义
--------

- 工作区不存在 → ``WORKSPACE_NOT_FOUND (404)``(由
  ``WorkspaceServiceError`` 全局翻译器兜底)。
- 工作区 meta.json 不合法 → ``WORKSPACE_INVALID (422)``(由 ``MetaFileError``
  全局翻译器兜底)。
- ``chapter_range_start`` / ``_end`` 任一不存在 → ``CHAPTER_NOT_FOUND
  (404)``(``StyleAuditServiceError`` 经 ``_translate_service_error`` 翻译)。
"""

from __future__ import annotations

from fastapi import APIRouter, Depends

from src.api.deps import get_style_audit_service
from src.api.schemas.common import SuccessEnvelope
from src.api.schemas.style_audit import StyleAuditEntryOut, StyleAuditRequest
from src.services.style_audit_service import StyleAuditService

router = APIRouter(prefix="/workspaces", tags=["style_audit"])


@router.post(
    "/{workspace_id}/style/audit",
    response_model=SuccessEnvelope[StyleAuditEntryOut],
    summary="触发一次风格漂移审计(同步;P4-B-10 stub)",
    description=(
        "同步触发 StyleAuditor stub agent;P4-B-10 阶段返 severity='stub' /"
        " drift_score=null 占位记录,落 style_audit_history 表(P7 完整实"
        "现时复用同 schema)。"
    ),
)
async def trigger_style_audit(
    workspace_id: str,
    payload: StyleAuditRequest | None = None,
    service: StyleAuditService = Depends(get_style_audit_service),
) -> SuccessEnvelope[StyleAuditEntryOut]:
    request = payload or StyleAuditRequest()
    entry = service.run_audit(
        workspace_id,
        chapter_range_start=request.chapter_range_start,
        chapter_range_end=request.chapter_range_end,
    )
    return SuccessEnvelope(
        data=StyleAuditEntryOut.model_validate(entry.model_dump())
    )
