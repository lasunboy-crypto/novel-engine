"""记忆健康度服务(P4-B-09)。

对齐 OBSERVABILITY §3.1 ``memory_health_history`` 表 + §3.3 阈值告警表
"记忆健康度 critical → 流水线自动暂停 + 强制人工介入" + API_CONTRACT §5.3
``system.alerts`` 频道 ``memory_health_critical`` 事件 + PROJECT_SPEC §2.2
"MemoryHealthChecker:记忆库健康度审计"。

职责
====

1. **caller 预读编排**:打开工作区 SQLite session → list_characters /
   list_plot_threads / list_timeline / 取近 N 章 chapter_id → 关 session;
   再把数据传给 ``memory_health_checker.check_memory_health`` agent
   (P4-B-07 沉淀 + P4-B-08 接力 — "预读数据接口"纪律)。
2. **持久化**:把 LLM 报告 upsert 到 ``memory_health_history`` 表,失败
   swallow + structlog log(``persist_memory_health_check_safely`` helper,
   ``*_safely`` 模式第 6 处)。
3. **WS 推送**:``severity=critical`` 时 publish
   ``memory_health_critical`` 到 ``system.alerts`` 频道(复用 P4-B-08
   ``system_alerts_channel()`` helper);失败 swallow + structlog
   ``memory_health_notify_failed`` 事件。
4. **返回 ``MemoryHealthCheck`` Pydantic**:含 check_id / checked_at /
   severity / 3 类 count / report_json,API 路由直接序列化为响应。

设计要点
========

- **session 由工厂注入**:与 ``CharacterService`` / ``ChapterService`` 模板
  对齐 — ``session_factory: Callable[[str], Session]`` 接 ``workspace_id``
  返一个新 ``Session``。预读编排独立 session,持久化单独 session(两 session
  关闭点清晰)。
- **LLM 客户端注入**:构造时传 ``llm`` + ``model``,P4-B-09 范围内不嵌入
  ``ModelRouter`` 路径(对齐 ``CharacterService`` / ``MemoryExtractor``
  P2-B-09 简版模式,P3-B-06 router 接入归后续 task)。**测试通过 fake LLM
  client 注入**(对齐 ``test_memory_extractor.py`` 模式)。
- **workspace_id 透传**(P3-B-14):caller 把 ``run_check(workspace_id)``
  入参 ``workspace_id`` 显式透传 agent + persist helper + WS payload。
- **``*_safely`` helper 模式第 6 处判定**(P4-B-09 启动指令 ④):本任务
  ``persist_memory_health_check_safely`` 与 P4-B-02 / P4-B-03 / P4-B-04
  / P4-B-08 五处 DB session swallow 共用 try/except/log + rollback +
  close 骨架,但**各处情境略有差异**(P4-B-02 是 idempotent insert + FK
  关联,P4-B-03 是反查多表 + 计算 metrics,P4-B-04 是聚合重算,P4-B-08
  是 WS publish swallow)— **暂不抽公共模块**,理由:抽出 ``with_session_safely``
  通用 helper 需要 ``(session_factory, *, workspace_id, op_name, op_callable)``
  签名,callable 签名又要带 ``session, *, workspace_id``,加上不同 caller
  写 log 事件名不同,反复传 event_name 比内联 try/except 多 2 行,**负面
  收益**。沿用各任务内联模式;P4-B-10 第 7 处 frontend_errors 写库后再
  判定一次。
- **WS publish swallow**:与 P4-B-08 ``emit_cost_guard_event`` 同源
  (try/except WebSocketManager.publish + log ``memory_health_notify_failed``)。
- **API_CONTRACT §5.3 字面 ``memory_health_critical`` data 字段**:
  ``{workspace_id, issues}``;扩 ``check_id`` / ``severity`` / ``summary``
  / ``issues_count`` 与 structlog 字段同源,便于前端 ChaptersBoardPage 主页
  告警徽章渲染(P4-F-05)。
"""

from __future__ import annotations

import uuid
from collections.abc import Callable
from datetime import UTC, datetime
from pathlib import Path
from typing import Any

import structlog
from sqlalchemy import select
from sqlalchemy.orm import Session

from src.api.websocket.channels import system_alerts_channel
from src.api.websocket.manager import WebSocketManager
from src.core.agents.memory_health_checker import (
    MemoryHealthReport,
    check_memory_health,
)
from src.core.db.models import ChapterORM, MemoryHealthCheckORM
from src.core.llm.base import LLMClient
from src.core.memory.store import (
    list_characters_by_workspace,
    list_plot_threads_by_workspace,
    list_timeline_entries_by_workspace,
)
from src.core.models.metrics import MemoryHealthCheck
from src.core.routing.router import ModelRouter
from src.services.workspace_service import WorkspaceService

_log = structlog.get_logger(__name__)

SessionFactory = Callable[[str], Session]
"""按 ``workspace_id`` 开新 SQLite ``Session`` 工厂(与
``CharacterService`` / ``ChapterService`` 同形)。"""
RouterFactory = Callable[[str], ModelRouter]

# 默认近期章节窗口大小(用于 stale / inactive 判定的"近 N 章")。
DEFAULT_RECENT_CHAPTERS_WINDOW = 5


class MemoryHealthService:
    """记忆健康度审计业务层。

    依赖 ``WorkspaceService``(校验 meta.json + 取 ``Workspace``)+
    ``session_factory``(每次调用 new 一个 Session 给本工作区的 SQLite)
    + ``llm`` + ``model``(注入的 LLM 客户端 + 默认模型名)+ 可选
    ``ws_manager``(WebSocket 推送;``None`` 时 ``severity=critical``
    仅落日志不推送,供单测注入)。
    """

    def __init__(
        self,
        *,
        workspace_service: WorkspaceService,
        session_factory: SessionFactory,
        llm: LLMClient,
        model: str,
        router_factory: RouterFactory | None = None,
        ws_manager: WebSocketManager | None = None,
        prompts_dir: Path | None = None,
        recent_chapters_window: int = DEFAULT_RECENT_CHAPTERS_WINDOW,
    ) -> None:
        self._workspace_service = workspace_service
        self._session_factory = session_factory
        self._llm = llm
        self._model = model
        self._router_factory = router_factory
        self._ws_manager = ws_manager
        self._prompts_dir = prompts_dir
        self._recent_chapters_window = recent_chapters_window

    def run_check(self, workspace_id: str) -> MemoryHealthCheck:
        """触发一次记忆健康度审计。

        流程:
        1. 校验工作区 meta.json 存在(``WorkspaceService.info``)。
        2. 打开 SQLite session → 预读 characters / plot_threads / timeline
           + 近 N 章 chapter_id → 关 session。
        3. 调 ``check_memory_health`` agent → 得 ``MemoryHealthReport``。
        4. ``persist_memory_health_check_safely`` 落 ``memory_health_history``
           表(失败 swallow)。
        5. ``severity=critical`` 时 ``_notify_memory_health_critical``
           publish WS(失败 swallow)。
        6. 返回 ``MemoryHealthCheck`` Pydantic(API 序列化)。

        :param workspace_id: 当前操作工作区 ID;**transparently 透传**到
            agent / persist / WS payload。
        :return: ``MemoryHealthCheck`` Pydantic 完整报告(含 check_id /
            checked_at / 4 档 severity / 3 类 count / report_json)。
        :raises WorkspaceServiceError / MetaFileError: 工作区不存在或
            meta.json 不合法;由路由层翻译为 ``WORKSPACE_NOT_FOUND``
            (404)/ ``WORKSPACE_INVALID``(422)。
        :raises MemoryHealthCheckerError: LLM 输出非合法 JSON;由路由层翻
            译为 ``MEMORY_HEALTH_CHECKER_FAILED``(502 — 上游 LLM 异常)。
        """
        # 1. 校验工作区。
        self._workspace_service.info(workspace_id)

        # 2. 预读三类硬记忆 + 近 N 章 chapter_id。
        characters, plot_threads, timeline, recent_chapter_ids = (
            self._read_workspace_snapshot(workspace_id)
        )

        # 3. 调 agent(注入的 LLM + 模型 + 默认 prompts_dir)。
        router = (
            self._router_factory(workspace_id)
            if self._router_factory is not None
            else None
        )
        report = check_memory_health(
            workspace_id=workspace_id,
            characters=characters,
            plot_threads=plot_threads,
            timeline=timeline,
            recent_chapter_ids=recent_chapter_ids,
            router=router,
            llm=self._llm if router is None else None,
            model=self._model if router is None else None,
            prompts_dir=self._prompts_dir,
        )

        # 4. 落表(swallow on failure)+ 5. critical → WS 推送(swallow on failure)。
        check = _build_memory_health_check(report)
        persist_memory_health_check_safely(
            self._session_factory,
            workspace_id=workspace_id,
            check=check,
        )
        if check.severity == "critical":
            _notify_memory_health_critical(
                self._ws_manager,
                workspace_id=workspace_id,
                check=check,
                report=report,
            )

        return check

    # ---- 内部 helper(预读编排)-----------------------------------------------

    def _read_workspace_snapshot(
        self, workspace_id: str
    ) -> tuple[list, list, list, list[str]]:
        """打开 session 预读 4 类数据 → 关 session 返回。

        独立 session 与持久化 session 解耦,避免事务边界混淆;读完即关。
        """
        session = self._session_factory(workspace_id)
        try:
            characters = list_characters_by_workspace(session, workspace_id)
            plot_threads = list_plot_threads_by_workspace(session, workspace_id)
            timeline = list_timeline_entries_by_workspace(session, workspace_id)
            recent_chapter_ids = _query_recent_chapter_ids(
                session,
                workspace_id=workspace_id,
                window=self._recent_chapters_window,
            )
            return characters, plot_threads, timeline, recent_chapter_ids
        finally:
            session.close()


# =============================================================================
# 内部工具函数 + persist_*_safely(模块级,便于单测直接调用)
# =============================================================================


def _query_recent_chapter_ids(
    session: Session,
    *,
    workspace_id: str,
    window: int,
) -> list[str]:
    """读工作区近 ``window`` 章的 ``chapter_id``,按 ``created_at`` desc。

    用于 stale_threads / inactive_characters 判定的"近 N 章"维度。无章
    时返空列表(全新工作区合理)。
    """
    rows = session.execute(
        select(ChapterORM.chapter_id)
        .where(ChapterORM.workspace_id == workspace_id)
        .order_by(ChapterORM.created_at.desc())
        .limit(window)
    ).scalars().all()
    return list(rows)


def _build_memory_health_check(report: MemoryHealthReport) -> MemoryHealthCheck:
    """``MemoryHealthReport`` → ``MemoryHealthCheck`` Pydantic 转换。

    生成 ``check_id``(UUID-12 截断,对齐其他历史表如
    ``MetricsPerChapterORM`` 主键风格)+ ``checked_at`` UTC now + 三类
    count + ``report_json`` 序列化(LLM 输出 dump + model_used + summary)。
    """
    return MemoryHealthCheck(
        check_id=f"mhc-{uuid.uuid4().hex[:12]}",
        checked_at=datetime.now(UTC),
        severity=report.severity,
        stale_threads_count=len(report.stale_threads),
        inactive_characters_count=len(report.inactive_characters),
        conflicts_count=len(report.conflicts),
        report_json={
            "summary": report.summary,
            "model_used": report.model_used,
            "stale_threads": [i.model_dump() for i in report.stale_threads],
            "inactive_characters": [
                i.model_dump() for i in report.inactive_characters
            ],
            "conflicts": [i.model_dump() for i in report.conflicts],
        },
    )


def persist_memory_health_check_safely(
    session_factory: SessionFactory | None,
    *,
    workspace_id: str,
    check: MemoryHealthCheck,
) -> None:
    """落 ``memory_health_history`` 表,失败 swallow + structlog log。

    ``session_factory=None`` → no-op(测试无 db 路径)。给定时打开短 session、
    插入一行 ``MemoryHealthCheckORM``、commit、close。事务边界全在本函数
    内,**不冒泡**——审计结果是辅助任务,落库失败不应阻断 API 响应或
    WS 推送(对齐 P2-B-19 / P3-B-07 / P3-B-13 / P4-B-02 / P4-B-03 / P4-B-04
    六先例,这是第 6 处 DB session swallow)。

    ``check_id`` UUID 唯一生成,**不做幂等去重**(每次 ``run_check`` 触发
    都落一行新的审计快照;``memory_health_history`` 表本就是历史轨迹,
    重复触发应有多行)。
    """
    if session_factory is None:
        return
    try:
        session = session_factory(workspace_id)
    except Exception as exc:  # noqa: BLE001 — swallow per P2-B-19 / P3-B-07 / P4-B-02/03/04 先例
        _log.error(
            "memory_health_persist_failed",
            workspace_id=workspace_id,
            check_id=check.check_id,
            stage="open_session",
            error_type=type(exc).__name__,
            error_message=str(exc),
        )
        return
    try:
        row = MemoryHealthCheckORM(
            check_id=check.check_id,
            checked_at=check.checked_at,
            severity=check.severity,
            stale_threads_count=check.stale_threads_count,
            inactive_characters_count=check.inactive_characters_count,
            conflicts_count=check.conflicts_count,
            report_json=dict(check.report_json),
        )
        session.add(row)
        session.commit()
        _log.info(
            "memory_health_persisted",
            workspace_id=workspace_id,
            check_id=check.check_id,
            severity=check.severity,
            issues_count=(
                check.stale_threads_count
                + check.inactive_characters_count
                + check.conflicts_count
            ),
        )
    except Exception as exc:  # noqa: BLE001 — swallow per 同上 6 先例
        session.rollback()
        _log.error(
            "memory_health_persist_failed",
            workspace_id=workspace_id,
            check_id=check.check_id,
            stage="commit",
            error_type=type(exc).__name__,
            error_message=str(exc),
        )
    finally:
        session.close()


def _notify_memory_health_critical(
    ws_manager: WebSocketManager | None,
    *,
    workspace_id: str,
    check: MemoryHealthCheck,
    report: MemoryHealthReport,
) -> None:
    """``severity=critical`` 时 WS publish ``memory_health_critical``。

    复用 P4-B-08 ``system_alerts_channel()`` helper;API_CONTRACT §5.3 字面
    ``memory_health_critical`` data 字段 ``{workspace_id, issues}``,扩
    ``check_id`` / ``severity`` / ``summary`` / ``issues_count`` 与
    structlog 同源便于前端 AlertBadge / HealthScoreBadge(P4-F-05)定位。

    ``ws_manager=None`` → 不推送(测试 / 无 WS 上下文场景);失败 swallow
    + structlog ``memory_health_notify_failed`` 事件(对齐 P4-B-08
    ``cost_guard_notify_failed`` 模式)。
    """
    if ws_manager is None:
        return
    try:
        issues_payload: list[dict[str, Any]] = []
        for kind, items in (
            ("stale_thread", report.stale_threads),
            ("inactive_character", report.inactive_characters),
            ("conflict", report.conflicts),
        ):
            for issue in items:
                issues_payload.append(
                    {"kind": kind, "id": issue.id, "reason": issue.reason}
                )
        ws_manager.publish(
            channel=system_alerts_channel(),
            event="memory_health_critical",
            data={
                "workspace_id": workspace_id,
                "check_id": check.check_id,
                "severity": check.severity,
                "summary": report.summary,
                "issues_count": (
                    check.stale_threads_count
                    + check.inactive_characters_count
                    + check.conflicts_count
                ),
                "issues": issues_payload,
            },
        )
    except Exception as exc:  # noqa: BLE001 — swallow per P4-B-08 cost_guard_notify_failed 先例
        _log.error(
            "memory_health_notify_failed",
            workspace_id=workspace_id,
            check_id=check.check_id,
            stage="ws_publish",
            error_type=type(exc).__name__,
            error_message=str(exc),
        )


__all__ = [
    "DEFAULT_RECENT_CHAPTERS_WINDOW",
    "MemoryHealthService",
    "SessionFactory",
    "persist_memory_health_check_safely",
]
