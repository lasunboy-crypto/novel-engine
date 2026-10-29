"""FastAPI 应用入口(P1-B-17)。

对齐 API_CONTRACT §1(总体原则)/ §3(响应格式)/ §11(实施要点)。

启动顺序
========

1. 进入 lifespan → ``configure_logging()`` 把 structlog 全局配置好
   (OBSERVABILITY §2.1)。
2. 落 ``system_startup`` 事件(OBSERVABILITY §2.4 系统事件)。
3. 应用接管请求。

中间件挂载顺序(自外到内)
==========================

1. ``CORSMiddleware``(最外,P1-F-01 加):浏览器跨源预检 ``OPTIONS`` 直接由
   它响应,不进入 RequestID / AccessLog,降低预检请求噪音。允许 origin =
   ``http://localhost:5173/5174`` + ``http://127.0.0.1:5173/5174``;允许 header
   显式列 ``X-Workspace-Id``(P1-B-19 章节路由必传)+ ``Content-Type`` +
   ``Authorization``。
2. ``RequestIDMiddleware``:所有请求第一时间拿到 ``request_id``,写
   ``scope.state``,装到响应头。
3. ``AccessLogMiddleware``(中间):请求结束后落 ``api_request`` 事件。
4. ``ApiErrorTranslationMiddleware``(最内 user middleware,在 starlette
   ``ExceptionMiddleware`` 之外):兜底翻译未被 FastAPI handler 处理的异常,
   见 ``src/api/errors.py`` 模块 docstring。

starlette ``add_middleware`` 用 ``insert(0, ...)``,**最后注册的最外层**。
所以代码里调用顺序是从内到外:Translation 先 add,CORS 最后 add。

绑定与认证
==========

``settings`` import 时已校验 ``api_host=0.0.0.0`` 必须配 ``api_auth_enabled=true``
(``src/config.py`` model_validator)。本模块只起 app,不启动 uvicorn(uvicorn
由 CLI 或外部入口启动,host/port 走 ``settings.api_host`` / ``settings.api_port``)。
"""

from __future__ import annotations

from collections.abc import AsyncIterator
from contextlib import asynccontextmanager

import structlog
from fastapi import FastAPI
from starlette.middleware.cors import CORSMiddleware

from src.api.errors import (
    ApiErrorTranslationMiddleware,
    register_exception_handlers,
)
from src.api.logging import configure_logging
from src.api.middleware import (
    AccessLogMiddleware,
    ApiMetricsMiddleware,
    RateLimitMiddleware,
    RequestIDMiddleware,
    SystemSessionFactory,
)
from src.api.websocket.manager import manager as ws_manager
from src.config import settings
from src.services.workspace_operation_guard import acquire_runtime_owner_lock
from src.services.system_db import make_system_session_factory
from src.api.routes import (
    assets,
    chapter_summary,
    chapters,
    characters,
    config,
    exports,
    frontend_errors,
    health,
    logs,
    memory_health,
    metrics,
    outline,
    pipeline,
    style_audit,
    style_templates,
    tasks,
    threads,
    timeline,
    volumes,
    websocket,
    workspaces,
)
from src.version import ENGINE_VERSION

API_PREFIX = "/api/v1"

# 前端开发服务器允许的源:CLAUDE.md §部署形态字面 5173 + worktree 约定 5174
# (`docs/plan/p1_plan.md` 工作流约定 §2)。loopback 两套地址(localhost / 127.0.0.1)
# 都列以避免浏览器解析差异引入的 CORS 误判。
DEV_FRONTEND_ORIGINS = (
    "http://localhost:5173",
    "http://localhost:5174",
    "http://127.0.0.1:5173",
    "http://127.0.0.1:5174",
)

# CORS 允许的请求 header。X-Workspace-Id 是 P1-B-19 章节路由的强制自定义 header,
# 不显式列会被浏览器预检拦截(403)。Content-Type / Authorization 走标准。
CORS_ALLOWED_HEADERS = ("X-Workspace-Id", "Content-Type", "Authorization")


@asynccontextmanager
async def _lifespan(_app: FastAPI) -> AsyncIterator[None]:
    """应用生命周期 hook:启动时配置 logging + 发系统事件 + 扫 pipeline_runs。

    P6-B-03 起增加流水线启动扫描:进程崩溃重启后 active 行的
    BackgroundTasks 已死,残留 running 行是僵尸状态。v2 w1-s2 改判:数据
    完整可恢复 → 翻 ``interrupted``(可 ``:resume`` 断点续跑),不完整
    兜底翻 ``error``;均带 ``error_details={"reason":
    "engine_restarted_mid_run"}``(不自动续跑 — user 手动 resume)。
    随后的富记忆扫描先把上个进程遗留的 ``memory_enrichment=running``
    诚实收口为 ``failed`` 且禁止自动重放未知 Provider 请求；章自愈扫描
    (``_run_chapter_reset_stale_scan``)再把中断时停在流程中间态的章翻回
    ``drafting``。
    """

    configure_logging()
    runtime_owner = acquire_runtime_owner_lock(settings.data_dir)
    try:
        structlog.get_logger(__name__).info(
            "system_startup",
            version=ENGINE_VERSION,
        )
        # 全局 task DB 必须先完成无损 schema reconcile + 僵尸任务收口；任何
        # 读/shape/commit 失败都发生在 workspace 恢复写入之前并阻止服务启动。
        _run_task_startup_scan()
        admitted_workspace_ids = _run_workspace_recovery_admission_scan()
        _run_workspace_recovery_state_preflight(admitted_workspace_ids)
        _run_pipeline_startup_scan(admitted_workspace_ids)
        _run_chapter_operation_lease_startup_scan(admitted_workspace_ids)
        _run_memory_enrichment_startup_scan(admitted_workspace_ids)
        # v2 w1-s2:章自愈必须排在 task 启动扫描之后 — 僵尸 pending/running
        # task 行已翻 interrupted,reset_stale 的 active_tasks 对账不被挡
        _run_chapter_reset_stale_scan(admitted_workspace_ids)
        _run_workspace_version_startup_scan()
        _run_default_profile_health_check()
        yield
    finally:
        runtime_owner.close()


def _run_workspace_recovery_admission_scan() -> list[str]:
    """在任何 workspace DB 恢复扫描前完成只读版本与结构准入。"""

    from src.api.deps import configure_runtime_workspace_admission
    from src.services.backup_service import recover_interrupted_workspace_restores
    from src.services.workspace_service import WorkspaceService
    from src.services.workspace_version_service import (
        admit_workspaces_for_startup_recovery,
        get_current_schema_version,
        list_workspaces_for_startup_admission,
    )

    workspace_service = WorkspaceService()
    # restore 的目录交换必须先于 list/admission 收口；损坏或无法判定的 marker
    # 直接上抛，保证任何后续 workspace 恢复写入之前 fail-closed。
    recover_interrupted_workspace_restores(workspace_service.workspaces_root)
    current_schema = get_current_schema_version()
    workspaces = list_workspaces_for_startup_admission(workspace_service)
    restart_required_workspace_ids = [
        workspace.workspace_id
        for workspace in workspaces
        if (
            workspace.engine_version != ENGINE_VERSION
            or workspace.schema_version != current_schema
        )
    ]
    workspace_ids = admit_workspaces_for_startup_recovery(workspace_service)
    current_workspace_ids = {
        workspace.workspace_id
        for workspace in workspaces
        if (
            workspace.engine_version == ENGINE_VERSION
            and workspace.schema_version == current_schema
        )
    }
    configure_runtime_workspace_admission(
        workspace_service.workspaces_root,
        admitted_workspace_ids=workspace_ids,
        restart_required_workspace_ids=restart_required_workspace_ids,
        uninitialized_workspace_ids=sorted(current_workspace_ids - set(workspace_ids)),
    )
    structlog.get_logger(__name__).info(
        "workspace_recovery_admission_scan_complete",
        workspaces_admitted=len(workspace_ids),
        workspaces_restart_required=len(restart_required_workspace_ids),
    )
    return workspace_ids


def _run_workspace_recovery_state_preflight(
    admitted_workspace_ids: list[str],
) -> None:
    """全工作区只读验真通过后，才允许后续启动扫描落任何状态。"""

    _run_pipeline_startup_scan(
        admitted_workspace_ids,
        apply=False,
    )
    _run_chapter_operation_lease_startup_scan(
        admitted_workspace_ids,
        apply=False,
    )
    _run_memory_enrichment_startup_scan(
        admitted_workspace_ids,
        apply=False,
    )


def _run_chapter_operation_lease_startup_scan(
    admitted_workspace_ids: list[str] | None = None,
    *,
    apply: bool = True,
) -> None:
    """在任何 stale reset 前分类持久租约；未知现场保留并继续硬拦。"""

    from src.api import task_registry
    from src.api.deps import build_pipeline_components_for_lifespan

    service, discovered_workspace_ids = build_pipeline_components_for_lifespan()
    workspace_ids = (
        discovered_workspace_ids
        if admitted_workspace_ids is None
        else admitted_workspace_ids
    )
    released, retained = service.reconcile_operation_leases_on_startup(
        workspace_ids,
        apply=apply,
        cancelled_task_check=task_registry.is_user_cancelled_task_checkpoint,
    )
    structlog.get_logger(__name__).info(
        "chapter_operation_lease_startup_scan_complete",
        workspaces_scanned=len(workspace_ids),
        released_safe_leases=released,
        retained_safety_leases=retained,
        automatic_provider_replay=False,
        apply=apply,
    )


def _run_memory_enrichment_startup_scan(
    admitted_workspace_ids: list[str] | None = None,
    *,
    apply: bool = True,
) -> None:
    """R8 启动恢复：收口上个进程遗留的富记忆 ``running``。

    本项目的正常部署合同是单进程、唯一后端；只有在该冷启动前提下，daemon
    memory Worker 才确定不能跨进程存活。新后端启动时只把 shape 严格合法的
    僵尸 ``running`` 记为结构化 ``failed``，保留 Provider attempt 现场并明确
    禁止自动重放；不调用 LLM、不改正文/ready/ledger/run checkpoint。该扫描器
    不具备跨进程 Worker 探测能力，不能被解释为多后端环境的存活判定器。

    任一工作区或顶层 builder 失败都会阻止应用启动，避免跳过损坏现场后继续
    接受写章请求；顺序必须在 task 启动扫描之后，确保普通 BackgroundTask
    僵尸先完成持久化收口。
    """

    log = structlog.get_logger(__name__)
    try:
        from src.api.deps import build_rewind_components_for_lifespan

        service, discovered_workspace_ids = build_rewind_components_for_lifespan()
        workspace_ids = (
            discovered_workspace_ids
            if admitted_workspace_ids is None
            else admitted_workspace_ids
        )
        interrupted_total = 0
        for ws_id in workspace_ids:
            interrupted_ids = (
                service.mark_running_memory_enrichment_as_failed_on_startup(ws_id)
                if apply
                else service.mark_running_memory_enrichment_as_failed_on_startup(
                    ws_id,
                    apply=False,
                )
            )
            interrupted_total += len(interrupted_ids)
        log.info(
            "memory_enrichment_startup_scan_complete",
            workspaces_scanned=len(workspace_ids),
            interrupted_enrichments=interrupted_total,
            automatic_provider_replay=False,
            apply=apply,
        )
    except Exception as exc:  # noqa: BLE001
        log.exception(
            "memory_enrichment_startup_scan_failed",
            error_type=type(exc).__name__,
            error_message=str(exc),
        )
        raise


def _run_chapter_reset_stale_scan(
    admitted_workspace_ids: list[str] | None = None,
) -> None:
    """v2 w1-s2 启动 hook 实现(独立函数便于测试):中断章自愈。

    扫所有工作区滞留非终态(reviewing / rewriting / revising)的章 → 翻回
    ``drafting``(进程中断时正在写的章停在流程中间态,无人重置则状态与
    实际不符)。``human_intervention`` / ``invalidated`` 语义"等人"不动;
    防误杀对账(active pipeline run + active tasks)在 service 内做 —
    本 hook 排在 ``_run_task_startup_scan`` 之后,僵尸 task 已翻
    interrupted 不挡对账。

    单工作区失败 swallow(逐 workspace try/except);本函数顶层 try/except
    再兜底,任何异常不阻塞应用启动(对齐 ``_run_pipeline_startup_scan``
    同模式 — 进程能起 > 启动 scan 必成)。
    """
    log = structlog.get_logger(__name__)
    try:
        from src.api.deps import build_rewind_components_for_lifespan

        service, discovered_workspace_ids = build_rewind_components_for_lifespan()
        workspace_ids = (
            discovered_workspace_ids
            if admitted_workspace_ids is None
            else admitted_workspace_ids
        )
        total_reset = 0
        for ws_id in workspace_ids:
            try:
                result = service.reset_stale(ws_id)
                total_reset += result.reset_count
            except Exception as exc:  # noqa: BLE001
                log.error(
                    "chapter_reset_stale_scan_workspace_failed",
                    workspace_id=ws_id,
                    error_type=type(exc).__name__,
                    error_message=str(exc),
                )
        log.info(
            "chapter_reset_stale_scan_complete",
            workspaces_scanned=len(workspace_ids),
            reset_chapters=total_reset,
        )
    except Exception as exc:  # noqa: BLE001
        log.error(
            "chapter_reset_stale_scan_failed",
            error_type=type(exc).__name__,
            error_message=str(exc),
        )


def _run_workspace_version_startup_scan() -> None:
    """P6-B-08 启动 hook 实现(独立函数便于测试)。

    扫所有工作区 meta.json,与当前引擎 ``ENGINE_VERSION`` + Alembic head
    比对,版本不一致落 ``workspace_version_mismatch`` warn 事件(每工作区一条)
    + 顶层 ``workspace_version_startup_scan_complete`` info 事件(聚合统计)。
    **不主动迁移** — 用户 / CLI doctor 显式调 ``:migrate`` 才迁移(对齐
    plan §P6-B-08 字面"启动扫描 hook 落 warn 事件,不主动迁移")。

    顶层 try/except 兜底,任何异常不阻塞应用启动(对齐
    ``_run_pipeline_startup_scan`` 同模式)。
    """
    log = structlog.get_logger(__name__)
    try:
        from src.services.workspace_service import WorkspaceService
        from src.services.workspace_version_service import (
            run_workspace_version_startup_scan,
        )

        workspace_service = WorkspaceService()
        stats = run_workspace_version_startup_scan(workspace_service)
        log.info(
            "workspace_version_startup_scan_complete",
            scanned=stats["scanned"],
            matched=stats["matched"],
            mismatched=stats["mismatched"],
        )
    except Exception as exc:  # noqa: BLE001
        log.error(
            "workspace_version_startup_scan_failed",
            error_type=type(exc).__name__,
            error_message=str(exc),
        )


def _run_pipeline_startup_scan(
    admitted_workspace_ids: list[str] | None = None,
    *,
    apply: bool = True,
) -> None:
    """P6-B-03 启动 hook 实现(独立函数便于测试;v2 w1-s2 改判)。

    扫所有工作区 ``pipeline_runs.state='running'`` 行 → 数据完整可恢复
    (``chapter_ids`` 非空)翻 ``interrupted``(可 :resume 断点续跑),
    不完整兜底翻 ``error``;均带 ``error_details={"reason":
    "engine_restarted_mid_run"}``。``apply=False`` 是全工作区只读预检。
    任一 workspace 扫描失败都阻止启动；否则永久 running 僵尸既不能 resume，
    又会挡住新 run，不能按普通可观测性故障吞掉。
    """
    log = structlog.get_logger(__name__)
    try:
        from src.api.deps import build_pipeline_components_for_lifespan

        service, discovered_workspace_ids = build_pipeline_components_for_lifespan()
        workspace_ids = (
            discovered_workspace_ids
            if admitted_workspace_ids is None
            else admitted_workspace_ids
        )
        marked = (
            service.mark_running_as_interrupted_on_startup(workspace_ids)
            if apply
            else service.mark_running_as_interrupted_on_startup(
                workspace_ids,
                apply=False,
            )
        )
        log.info(
            "pipeline_startup_scan_complete",
            workspaces_scanned=len(workspace_ids),
            marked_interrupted=marked,
            apply=apply,
        )
    except Exception as exc:  # noqa: BLE001
        log.error(
            "pipeline_startup_scan_failed",
            error_type=type(exc).__name__,
            error_message=str(exc),
        )
        raise


def _run_task_startup_scan() -> None:
    """v1.1 wave 3 session 5(E3)启动 hook 实现(独立函数便于测试)。

    ① v1.1 w4-s6(J1)先 ``reconcile_task_status_check`` 无损升级既有 ``_tasks.db``
    的 ``status`` CHECK 约束到 6 档(加 ``cancelled`` —— 该表非 alembic 管理,
    ``create_all`` 不改既有表约束,详 ``task_db.reconcile_task_status_check``);
    ② 再扫残留 ``pending`` / ``running`` 行 → 翻 ``interrupted``(in-flight
    BackgroundTask 随进程重启已死,这些是僵尸态)。让前端 ``GET /tasks/{id}`` 拿到
    清晰的"已中断"而非永久"运行中"。

    任一读取、表结构 reconcile 或 commit 失败都向上抛并阻止应用启动；否则
    后续恢复会在无法判断是否仍有活 task 的情况下写 workspace，可能制造
    重复 Worker/请求。
    """
    log = structlog.get_logger(__name__)
    try:
        from src.api import task_registry
        from src.services.task_db import reconcile_task_status_check

        reconciled = reconcile_task_status_check()
        marked = task_registry.mark_running_as_interrupted_on_startup()
        log.info(
            "task_startup_scan_complete",
            marked_interrupted=marked,
            status_check_reconciled=reconciled,
        )
    except Exception as exc:  # noqa: BLE001
        log.exception(
            "task_startup_scan_failed",
            error_type=type(exc).__name__,
            error_message=str(exc),
        )
        raise


def _run_default_profile_health_check() -> None:
    """v1.1 wave 2 session 3 H2.6 启动 hook 实现(独立函数便于测试)。

    启动期只读取非敏感 profile 元数据。``mock`` / ``ollama`` 可直接标为本地
    可用；需要凭据的 provider 把可用性检查推迟到首次 Provider runtime，启动
    与本地恢复链绝不调用 ``SecretStr.get_secret_value``。

    **fail-soft 兜底** — 不阻止应用启动(对齐 plan §4 w2-s6 字面);任何
    异常顶层 try/except 吞掉,继续启动(对齐 ``_run_pipeline_startup_scan``
    同模式 — 进程能起 > 启动 scan 必成)。

    首次真实 Provider client 构造仍按既有 runtime 路径验 key；本函数不改变
    那条失败语义。
    """
    log = structlog.get_logger(__name__)
    try:
        from src.config import settings
        from src.core.config_loader import load_writer_profiles_config

        cfg = load_writer_profiles_config(settings.config_yaml_path)
        default_profile_id = cfg.routing.default
        default_profile = cfg.profiles[default_profile_id]
        provider = default_profile.provider
        if provider in {"mock", "ollama"}:
            log.info(
                "default_profile_health_check_complete",
                default_profile_id=default_profile_id,
                provider=provider,
                usable=True,
                secret_access=False,
            )
            return

        log.info(
            "default_profile_health_check_deferred",
            default_profile_id=default_profile_id,
            provider=provider,
            usable=None,
            secret_access=False,
            deferred_until="first_provider_runtime",
        )
    except Exception as exc:  # noqa: BLE001
        log.error(
            "default_profile_health_check_failed",
            error_type=type(exc).__name__,
            error_message=str(exc),
        )


def create_app(
    *, system_session_factory: SystemSessionFactory | None = None
) -> FastAPI:
    """工厂函数。测试通过 ``create_app()`` 拿到独立 app 实例,避免共享单例。

    ``system_session_factory``:P4-B-04 ``ApiMetricsMiddleware`` 用于落
    ``system_metrics_daily`` 表的零参 SQLite session 工厂。**默认 None
    (no-op)**——既有测试 ``create_app()`` 不会触发系统 db 写文件;生产
    入口(uvicorn entry / module-level ``app``)显式传 ``make_system_session_factory()``
    开启系统 metrics 持久化(对齐 P4-B-04 渐进 opt-in 节奏,完全 wire-up
    留给 P4-B-08 / P4-acceptance 收口)。
    """

    app = FastAPI(
        title="Novel Engine API",
        version=ENGINE_VERSION,
        description=(
            "长篇小说创作引擎 API(P1)。响应包络见 API_CONTRACT §3.1 / §3.2;"
            "错误码见 src/api/errors.py。"
        ),
        lifespan=_lifespan,
    )

    # P4-B-10:前端错误上报路由读 ``app.state.system_session_factory`` 落
    # ``system_metrics_daily.frontend_errors`` 累加,与 ``ApiMetricsMiddleware``
    # 同源同库不同字段。默认 None → no-op(对齐 P4-B-04 渐进 opt-in)。
    app.state.system_session_factory = system_session_factory

    # 中间件挂载顺序:从内到外依次 add(starlette 后注册的在最外层)。
    app.add_middleware(ApiErrorTranslationMiddleware)
    # P4-B-10 ``RateLimitMiddleware``:挂在 ``ApiMetricsMiddleware`` 内层
    # + ``ApiErrorTranslationMiddleware`` 外层 — 429 响应被 ApiMetrics 看
    # 到 + 计入 ``system_metrics_daily.api_errors``。每 workspace 100
    # req/min 默认滑窗;超限返 429 + WS publish 到 ``system.alerts``。
    app.add_middleware(RateLimitMiddleware, ws_manager=ws_manager)
    # P4-B-04 ``ApiMetricsMiddleware`` 与 ``AccessLogMiddleware`` 同层(都在
    # ``RequestIDMiddleware`` 内、``ApiErrorTranslationMiddleware`` 外):前者
    # 落 system_metrics_daily SQL,后者落 structlog ``api_request`` 事件,
    # 解耦互不影响(任一失败不连累另一,见 ``ApiMetricsMiddleware`` docstring)。
    app.add_middleware(ApiMetricsMiddleware, session_factory=system_session_factory)
    app.add_middleware(AccessLogMiddleware)
    app.add_middleware(RequestIDMiddleware)
    # CORS 在最外层:浏览器预检 OPTIONS 直接由它响应,不进入 RequestID / AccessLog
    # 链路,降低预检请求对日志的噪音。
    app.add_middleware(
        CORSMiddleware,
        allow_origins=list(DEV_FRONTEND_ORIGINS),
        allow_credentials=True,
        allow_methods=["GET", "POST", "PUT", "DELETE", "OPTIONS"],
        allow_headers=list(CORS_ALLOWED_HEADERS),
    )

    register_exception_handlers(app)

    app.include_router(health.router, prefix=API_PREFIX)
    app.include_router(workspaces.router, prefix=API_PREFIX)
    app.include_router(config.router, prefix=API_PREFIX)
    # P6-B-16 API key 凭证管理(独立路由,非 workspace 子资源):
    # GET / PUT /config/api-keys。
    app.include_router(config.api_keys_router, prefix=API_PREFIX)
    app.include_router(chapters.router, prefix=API_PREFIX)
    app.include_router(characters.router, prefix=API_PREFIX)
    app.include_router(threads.router, prefix=API_PREFIX)
    app.include_router(timeline.router, prefix=API_PREFIX)
    app.include_router(tasks.router, prefix=API_PREFIX)
    # P4-B-04 metrics 路由:工作区子资源 7 端点 + 系统级 1 端点。
    app.include_router(metrics.workspace_router, prefix=API_PREFIX)
    app.include_router(metrics.system_router, prefix=API_PREFIX)
    # P4-B-05 logs 路由:系统级 ``/logs?...`` / ``/logs/trace/{id}`` GET +
    # WS ``/logs/stream``。
    app.include_router(logs.router, prefix=API_PREFIX)
    # P4-B-09 记忆健康度审计:POST ``/workspaces/{id}/memory/check``。
    app.include_router(memory_health.router, prefix=API_PREFIX)
    # P5-B-04 outline 路由:5 端点(generate / regenerate-section / commit /
    # GET / PUT outline)。
    app.include_router(outline.router, prefix=API_PREFIX)
    from src.api.routes import outline_diagnostic
    app.include_router(outline_diagnostic.router, prefix=API_PREFIX)
    from src.api.routes import authoring
    app.include_router(authoring.router, prefix=API_PREFIX)
    # v1.0.x wave 4 session 5:chapter_summary CRUD(GET 列表 + PUT 单条编辑)。
    app.include_router(chapter_summary.router, prefix=API_PREFIX)
    # P5-B-08 静态资产 import 路由:POST /workspaces/{id}/assets:import。
    app.include_router(assets.router, prefix=API_PREFIX)
    # P5-B-09 volumes CRUD 路由:GET 列表 / GET 单卷 / PUT / POST :apply-template
    # + GET /workspaces/{id}/outline:status。
    app.include_router(volumes.router, prefix=API_PREFIX)
    # P4-B-10 风格漂移审计(stub):POST ``/workspaces/{id}/style/audit``。
    app.include_router(style_audit.router, prefix=API_PREFIX)
    app.include_router(style_templates.router, prefix=API_PREFIX)
    # P6-B-02 流水线运行控制(内存版 v1,SQLite 持久化推到 P6-B-03):
    # ``/pipeline:start`` / ``:pause`` / ``:resume`` / ``:stop`` / ``/pipeline/status``。
    app.include_router(pipeline.router, prefix=API_PREFIX)
    # P6-B-04 导出引擎 md / chaptered_md(epub / docx 留 P6-B-05/06):
    # ``POST /workspaces/{id}/exports`` (202 异步) + ``GET /exports`` 列表 +
    # ``GET /exports/{id}/download`` zip 下载。
    app.include_router(exports.router, prefix=API_PREFIX)
    # P4-B-10 前端错误上报:POST ``/system/frontend-errors``。
    app.include_router(frontend_errors.router, prefix=API_PREFIX)
    # WebSocket 路由(P1-B-20)。WebSocket 不进 OpenAPI(REST-only),
    # 但同样挂在 /api/v1 前缀下,客户端连接 ``ws://.../api/v1/ws``。
    app.include_router(websocket.router, prefix=API_PREFIX)

    return app


app = create_app(system_session_factory=make_system_session_factory())
