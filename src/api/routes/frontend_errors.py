"""前端错误上报路由(P4-B-10)。

POST ``/api/v1/system/frontend-errors`` — 接受 OBSERVABILITY §8 字面 6 字段
(error_type / message / stack 前 1000 字 / url / user_action / browser)+
可选 ``workspace_id``,落 structlog ``frontend_error`` 事件 +
``system_metrics_daily.<today>.frontend_errors`` 累加。

URL 形态选 B(全局 system 资源):本端点服务全局错误上报,**不读
X-Workspace-Id header**(P4-B-10 启动指令注 ⑦)— 全局 ErrorBoundary +
window.onerror 上报场景未必有工作区上下文,``workspace_id`` 字段化在
请求 body 里,允许 ``None`` 表"未登录或全局错误"。

设计要点
========

- **失败 swallow**:落事件 + 累加为副作用,服务端兜底 swallow 失败 — 即便
  ``system_metrics_daily`` 写入失败也返 200,避免前端上报失败循环
  (对齐 P4-B-04 ``ApiMetricsMiddleware`` 同节奏)。
- **system_session_factory 通过 ``Request.app.state`` 取**:与
  ``ApiMetricsMiddleware`` 共用同一个 ``make_system_session_factory()`` 单
  例不是必须的(每次新开 session 也廉价),但为对账方便,我们把生产
  入口构造的 ``system_session_factory`` 绑到 ``app.state.system_session_factory``
  上,路由层取;测试可通过 ``app.state`` 覆盖 / 注 ``None`` no-op。
"""

from __future__ import annotations

import structlog
from fastapi import APIRouter, Request

from src.api.schemas.common import SuccessEnvelope
from src.api.schemas.frontend_errors import FrontendErrorReport
from src.services.system_metrics_writer import record_frontend_error_safely

router = APIRouter(prefix="/system", tags=["frontend_errors"])

_log = structlog.get_logger(__name__)


class _EmptyData(dict):
    """空 data 段占位(API_CONTRACT §3.1 包络要求 data 字段存在)。"""


@router.post(
    "/frontend-errors",
    response_model=SuccessEnvelope[dict],
    summary="前端错误上报(P4-B-10;OBSERVABILITY §8)",
    description=(
        "接受全局 ErrorBoundary / window.onerror / unhandledrejection 捕获的"
        " 前端错误,服务端落 frontend_error structlog 事件 + system_metrics_daily"
        ".frontend_errors 累加。响应空 data,前端 swallow 失败不重试。"
    ),
)
async def report_frontend_error(
    payload: FrontendErrorReport,
    request: Request,
) -> SuccessEnvelope[dict]:
    _log.warning(
        "frontend_error",
        error_type=payload.error_type,
        message=payload.message,
        stack=payload.stack,
        url=payload.url,
        user_action=payload.user_action,
        browser=payload.browser,
        workspace_id=payload.workspace_id,
    )
    # 取 system_session_factory(生产入口 main.py 注入到 app.state);测试默
    # 认 None → no-op,只走 structlog 事件路径。
    system_session_factory = getattr(
        request.app.state, "system_session_factory", None
    )
    record_frontend_error_safely(system_session_factory)
    return SuccessEnvelope(data={})
