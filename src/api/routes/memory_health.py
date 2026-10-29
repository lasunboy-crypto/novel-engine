"""记忆健康度检查路由(P4-B-09)。

POST ``/api/v1/workspaces/{workspace_id}/memory/check`` — 同步触发一次审计,
返回完整 ``MemoryHealthCheck`` 报告。

URL 形态选 A(嵌套子资源,对齐 P4-B-04 metrics 路由 + P3-B-10 config 路由
模式):``workspace_id`` 走 URL path,不要 ``X-Workspace-Id`` header — 端
点字面就是"为指定工作区运行健康度检查"。

错误语义
--------

- 工作区不存在 → ``WORKSPACE_NOT_FOUND (404)``(由
  ``WorkspaceServiceError`` 全局翻译器兜底)。
- 工作区 meta.json 不合法 → ``WORKSPACE_INVALID (422)``(由 ``MetaFileError``
  全局翻译器兜底)。
- LLM 输出非合法 JSON / 字段缺失 → ``MEMORY_HEALTH_CHECKER_FAILED (502)``
  (本任务在 ``api/errors.py`` 新增异常类 + 翻译表条目)。

API_CONTRACT §5.3 联动
-----------------------

``severity=critical`` 时,service 层会向 ``system.alerts`` 频道 publish
``memory_health_critical`` 事件(``data: {workspace_id, check_id, severity,
summary, issues_count, issues}``);路由本身仍以 200 + 完整报告响应,业务
方按 ``data.severity`` 字段判断是否需要触发"流水线暂停 + 人工裁决"
(P4-acceptance 接入 ChapterService gate)。
"""

from __future__ import annotations

from fastapi import APIRouter, Depends

from src.api.deps import get_memory_health_service
from src.api.schemas.common import SuccessEnvelope
from src.api.schemas.memory_health import MemoryHealthCheckOut
from src.services.memory_health_service import MemoryHealthService

router = APIRouter(prefix="/workspaces", tags=["memory_health"])


@router.post(
    "/{workspace_id}/memory/check",
    response_model=SuccessEnvelope[MemoryHealthCheckOut],
    summary="触发一次记忆健康度审计(同步)",
    description=(
        "同步触发 MemoryHealthChecker agent 审计当前工作区的角色 / 伏笔 / "
        "时间线三类硬记忆;落 memory_health_history 表 + severity=critical 时 "
        "向 system.alerts 频道 publish memory_health_critical 事件。"
    ),
)
async def trigger_memory_health_check(
    workspace_id: str,
    service: MemoryHealthService = Depends(get_memory_health_service),
) -> SuccessEnvelope[MemoryHealthCheckOut]:
    check = service.run_check(workspace_id)
    return SuccessEnvelope(
        data=MemoryHealthCheckOut.model_validate(check.model_dump())
    )
