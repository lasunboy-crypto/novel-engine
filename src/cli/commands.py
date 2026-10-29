"""CLI 命令实现(P1-B-21)。

每个 handler 接 ``argparse.Namespace``,返回 int 退出码:

- ``0``:成功
- ``1``:已知业务错误(``WorkspaceServiceError`` / ``ChapterServiceError`` /
  ``ChapterVersioningError`` / ``MetaFileError``;``--issues-file`` 校验失败)
- ``2``:未知异常(由 ``dispatch`` 捕获 + 打 traceback)

CLI 全部走 services 层(``WorkspaceService`` / ``ChapterService``),与 API 路由
共用业务逻辑。**CLI 不直接调 core/、不直连数据库、不调 LLM**(对齐 CLAUDE.md
§实施纪律)。

工作区上下文
============

工作区子命令(``info`` / ``clean`` / ``archive`` / ``purge`` / ``backup``)与
所有章节命令均支持"显式 > ``_active``"两段式解析:命令行传入则用命令行,
否则读 ``workspaces/_active`` 软链(P1-B-14 落地的目标);两者都没 → 抛
``WorkspaceServiceError("无活动工作区, ...")``。

``create`` / ``switch`` / ``restore`` 三个命令必须显式传 ID(语义上需要"接下
来要操作哪个工作区"的明确信号),不读 ``_active`` 兜底。``list`` 不需要 ID。

测试钩子
========

``make_workspace_service`` / ``make_chapter_service`` 是模块级工厂函数,handler
内通过 module-level lookup 调用,测试用 ``monkeypatch.setattr(cmd_module, ...)``
注入 mock 客户端版本。这与 P1-B-19 ``api/deps.py`` 的工厂模式一致。
"""

from __future__ import annotations

import json
import os
import sys
import traceback
from argparse import Namespace
from collections.abc import Callable
from pathlib import Path
from typing import Any, cast

import structlog
from sqlalchemy import create_engine
from sqlalchemy.orm import Session

from src.config import settings
from src.core.chapter_versioning import ChapterVersioningError
from src.core.db import Base
from src.core.llm.base import LLMClient
from src.core.llm.factory import make_llm_client
from src.core.memory.vector import VectorStore
from src.core.models.chapter import ChapterVersion
from src.core.models.review import ReviewIssue
from src.core.models.workspace import Workspace
from src.core.orchestrator.chapter_orchestrator import OrchestratorResult
from src.core.routing.router import ModelRouter
from src.core.routing.run_model_manifest_router import (
    build_manifest_fallback_sentinel,
    build_model_router,
)
from src.core.workspace.meta import MetaFileError
from src.core.workspace.workspace_paths import WorkspacePaths
from src.services.asset_import import (
    AssetImportError,
    ImportAssetsResult,
    ensure_workspace_orm,
    import_assets,
)
from src.services.attempt_ledger import wrap_router_with_attempt_tracking
from src.services.backup_service import (
    BackupInfo,
    BackupService,
    BackupServiceError,
)
from src.services.chapter_rewind_service import (
    ChapterRewindService,
    ChapterRewindServiceError,
)
from src.services.chapter_operation_lease import ChapterOperationLeaseStore
from src.services.chapter_service import (
    ChapterService,
    ChapterServiceError,
    ChapterStatusInfo,
)
from src.services.config_service import load_workspace_config
from src.services.export_service import (
    ExportService,
    ExportServiceError,
)
from src.services.pipeline_service import (
    PipelineRunSnapshot,
    PipelineService,
    PipelineServiceError,
)
from src.services.pipeline_provider_cap_maintenance import (
    PipelineProviderCapMaintenanceResult,
    PipelineProviderCapMaintenanceService,
)
from src.services.workspace_service import (
    WorkspaceService,
    WorkspaceServiceError,
)
from src.services.workspace_version_service import (
    MigrationResult,
    VersionStatus,
    WorkspaceVersionService,
    WorkspaceVersionServiceError,
)

# P1 默认模型(对齐 P1-B-08 cloud_quality / P1-B-19 ``api/deps.py``)。
# P3 引入 Profile 体系时改读 ``Settings.default_model``。
_DEFAULT_MODEL = "claude-sonnet-4-6"
_ACTIVE_LINK_NAME = "_active"

EXIT_OK = 0
EXIT_BUSINESS_ERROR = 1
EXIT_UNKNOWN_ERROR = 2


# ---- service 工厂(可被测试 monkeypatch 替换) -------------------------------


_BOOTSTRAP_LOG = structlog.get_logger(__name__)


def _resolve_bootstrap_target() -> tuple[str, str, str | None]:
    """读引擎默认 ``config.yaml`` 返 default profile 的 ``(provider, model, base_url)`` 三元组。

    与 ``src/api/deps.py:_resolve_bootstrap_target`` 同型 — post-wave5 H1
    routing completion fixup §6 留观项闭合(2026-05-27 v1.1 w1-b1-rerun
    启动期顺手补 ``cli/commands.py:161`` 同型坑,让 CLI ``write-chapter``
    支持引擎配置的活动 default profile 真实取证；当前默认是
    ``cloud_qwen_max_medium_creative``)。

    Fallback:config.yaml 不存在 / yaml 语法错 → ``(settings.llm_provider,
    _DEFAULT_MODEL, None)`` 极端兜底 + structlog warning。
    """
    from src.config import settings as _settings
    from src.core.config_loader import load_writer_profiles_config

    try:
        config = load_writer_profiles_config(_settings.config_yaml_path)
        default_profile = config.profiles[config.routing.default]
        return (
            default_profile.provider,
            default_profile.model,
            default_profile.base_url,
        )
    except Exception as exc:  # noqa: BLE001
        _BOOTSTRAP_LOG.warning(
            "bootstrap_target_fallback",
            reason="读 engine config.yaml 失败,fallback 到 settings.llm_provider 极端兜底",
            error_type=type(exc).__name__,
            error_message=str(exc),
        )
        return _settings.llm_provider, _DEFAULT_MODEL, None


def _resolve_bootstrap_client() -> tuple[LLMClient, str]:
    sentinel = build_manifest_fallback_sentinel(
        config_path=settings.config_yaml_path
    )
    if sentinel is not None:
        return sentinel
    provider, model, base_url = _resolve_bootstrap_target()
    return make_llm_client(model, base_url=base_url, provider=provider), model


def make_workspace_service() -> WorkspaceService:
    """构造 ``WorkspaceService``;默认从 ``settings.data_dir`` 取 root。"""
    return WorkspaceService()


def make_chapter_service(workspace_service: WorkspaceService) -> ChapterService:
    """构造 ``ChapterService``,LLM 客户端按引擎默认 ``config.yaml`` profile 三元组装,
    并注入 ``session_factory`` + ``vector_store_factory``(P2-B-17 / P2-B-19 起)。

    P1-B-22 起统一走 ``make_llm_client``。**v1.1 w1-b1-rerun 起**(2026-05-27,
    顺手补 H1 fixup §6 留观)CLI bootstrap 也走 ``_resolve_bootstrap_target()``
    读引擎默认 ``config.yaml`` 的 default profile,与 ``api/deps.py`` 同模式,
    支持 ``LLM_PROVIDER=openai_compatible`` + dashscope qwen3.8-max 等云端
    profile 真实证。

    ``session_factory`` 复用 ``make_db_session``——CLI 命令调
    ``write-chapter`` / ``rewrite-chapter`` 时,write/rewrite 入口 upsert
    chapters 行 + 状态机 transition 落库;``status`` / ``list`` 优先读库。
    与 ``api/deps.py:_make_session_factory`` 行为对齐(每次调用返回新
    Session,``Base.metadata.create_all`` idempotent 兜底 schema)。

    ``vector_store_factory``(P2-B-19 起)复用 ``make_vector_store``——
    write/rewrite verdict=pass 终态后 Orchestrator 调
    ``memory_extractor.extract`` 抽取增量记忆;Writer/Reviewer 注入
    db_session 拿 voice 段、注入 vector_store 拿历史检索段。

    测试通过 ``monkeypatch.setattr(commands, "make_chapter_service", ...)`` 注入
    带显式 mock LLM 客户端的版本,等价于 ``api/deps.py`` 中
    ``app.dependency_overrides[get_chapter_service]``。
    """

    def _session_factory(workspace_id: str) -> Session:
        paths = WorkspacePaths(
            workspace_id, workspaces_root=workspace_service.workspaces_root
        )
        return make_db_session(paths)

    def _vector_store_factory(workspace_id: str) -> VectorStore:
        paths = WorkspacePaths(
            workspace_id, workspaces_root=workspace_service.workspaces_root
        )
        return make_vector_store(workspace_id, paths)

    def _router_factory(workspace_id: str) -> ModelRouter:
        # P3-B-06:对齐 ``api/deps._make_router_factory`` 行为——每次写作
        # 请求合并引擎默认 + 工作区 ``project.yaml`` 后产新 router。
        config = load_workspace_config(workspace_id)
        return cast(
            ModelRouter,
            wrap_router_with_attempt_tracking(
                build_model_router(config, config_path=settings.config_yaml_path),
                session_factory=_session_factory,
                workspace_id=workspace_id,
            ),
        )

    client, model = _resolve_bootstrap_client()
    return ChapterService(
        workspace_service=workspace_service,
        client=client,
        model=model,
        session_factory=_session_factory,
        vector_store_factory=_vector_store_factory,
        router_factory=_router_factory,
        memory_enrichment_scheduler="background",
        operation_lease_store=ChapterOperationLeaseStore(_session_factory),
    )


def make_chapter_rewind_service(
    workspace_service: WorkspaceService,
) -> ChapterRewindService:
    """构造 ``ChapterRewindService``(P7-B-01)。

    对齐 ``api/deps.py:get_chapter_rewind_service`` 字段映射:复用同 CLI
    ``session_factory`` / ``vector_store_factory`` 工厂。**CLI 不需 WS publish**
    (单机本地命令行无 client 监听),``ws_manager=None``。

    测试通过 ``monkeypatch.setattr(commands, "make_chapter_rewind_service", ...)``
    注入带 in-memory engine + Fake VectorStore 的版本。
    """

    def _session_factory(workspace_id: str) -> Session:
        paths = WorkspacePaths(
            workspace_id, workspaces_root=workspace_service.workspaces_root
        )
        return make_db_session(paths)

    def _vector_store_factory(workspace_id: str) -> VectorStore:
        paths = WorkspacePaths(
            workspace_id, workspaces_root=workspace_service.workspaces_root
        )
        return make_vector_store(workspace_id, paths)

    def _router_factory(workspace_id: str) -> ModelRouter:
        return cast(
            ModelRouter,
            wrap_router_with_attempt_tracking(
                build_model_router(
                    load_workspace_config(workspace_id),
                    config_path=settings.config_yaml_path,
                ),
                session_factory=_session_factory,
                workspace_id=workspace_id,
            ),
        )

    return ChapterRewindService(
        workspace_service=workspace_service,
        session_factory=_session_factory,
        vector_store_factory=_vector_store_factory,
        ws_manager=None,
        router_factory=_router_factory,
        operation_lease_store=ChapterOperationLeaseStore(_session_factory),
    )


def make_pipeline_service(
    workspace_service: WorkspaceService,
) -> PipelineService:
    """构造 ``PipelineService``(P7-B-02)。

    对齐 ``api/deps.py:get_pipeline_service`` 字段映射:复用 CLI ``session_factory``
    + ``make_chapter_service``;**CLI 不传 ws_manager**(单机本地命令行无 client
    监听 publish 没意义,对齐 P7-B-01 ``make_chapter_rewind_service`` 同模式),
    ``ws_manager=None``。

    与 API 单例不同 — CLI 每次命令构造新 service,DB ``pipeline_runs`` 表是真相
    源(P6-B-03 SQLite 持久化背书),无跨命令内存状态。

    **CLI 不调 ``execute``**:start 端点只 INSERT pipeline_runs 行(:start API
    端点同样设计 — execute 由 BackgroundTasks 在响应已发出后调,CLI 没有
    BackgroundTasks 上下文)。用户启动 pipeline 后通过 ``novel pipeline status``
    poll 状态;实际章节循环由后端 server 进程的 BackgroundTasks 跑(server
    与 CLI 共享 ``pipeline_runs`` 表)。**注**:若 server 未启动,start 命令
    只会写 pipeline_runs 行,无章节循环跑。

    测试通过 ``monkeypatch.setattr(commands, "make_pipeline_service", ...)``
    注入带 in-memory engine + FakeChapterService + ws_manager=None 的版本。
    """

    def _session_factory(workspace_id: str) -> Session:
        paths = WorkspacePaths(
            workspace_id, workspaces_root=workspace_service.workspaces_root
        )
        return make_db_session(paths)

    chapter_service = make_chapter_service(workspace_service)
    return PipelineService(
        chapter_service=chapter_service,
        workspace_service=workspace_service,
        session_factory=_session_factory,
        ws_manager=None,
    )


def make_pipeline_provider_cap_maintenance_service(
    workspace_service: WorkspaceService,
) -> PipelineProviderCapMaintenanceService:
    """构造不含 LLM/Worker 的离线 Provider cap 维护服务。"""

    from src.api import task_registry

    return PipelineProviderCapMaintenanceService(
        workspace_service=workspace_service,
        backup_service=make_backup_service(workspace_service),
        active_task_checker=task_registry.has_active_tasks,
    )


def make_export_service(
    workspace_service: WorkspaceService,
    *,
    ws_manager: Any = None,
) -> ExportService:
    """构造 ``ExportService``(P7-B-03)。

    对齐 ``api/deps.py:get_export_service`` 字段映射:复用 CLI ``session_factory``
    工厂。**``ws_manager`` 可注入**(默认 ``None``)— ``cmd_export_run`` 同步阻塞
    跑 ``execute`` 时注入 ``_CLIStageObserver`` 把 stage event 推 stderr 实现
    "polls task progress" 等效 UX(stderr 不污染 stdout JSON 输出);``list`` /
    ``download`` 不需 stage observer 传默认 ``None``。

    **CLI 同步直调 ``execute``**(与 P7-B-02 ``pipeline:start`` 不同 —— API
    ``:create`` 端点用 ``BackgroundTasks`` 异步跑 execute,CLI 无 BackgroundTasks
    上下文;启动指令字面 "polls GET /tasks/{task_id} 直到 done" 类 B 工程判断
    改为同步直调 + stage observer 推 stderr,impl 文档登记)。

    测试通过 ``monkeypatch.setattr(commands, "make_export_service", ...)``
    注入带 in-memory engine + 真 ExportService(支持 4 格式真渲染)+
    ws_manager 注入 ``_CLIStageObserver`` 验证 stage 序列。
    """

    def _session_factory(workspace_id: str) -> Session:
        paths = WorkspacePaths(
            workspace_id, workspaces_root=workspace_service.workspaces_root
        )
        return make_db_session(paths)

    return ExportService(
        workspace_service=workspace_service,
        session_factory=_session_factory,
        ws_manager=ws_manager,
    )


def make_backup_service(
    workspace_service: WorkspaceService,
) -> BackupService:
    """构造 ``BackupService``(P7-B-04)。

    对齐 ``api/deps.py:get_backup_service`` 字段映射。**CLI 不传 ws_manager**
    (单机本地命令行无 client 监听 publish 没意义),但复用持久 TaskRegistry
    active checker，避免另一个 API/CLI 进程尚有任务时覆盖恢复工作区。

    测试通过 ``monkeypatch.setattr(commands, "make_backup_service", ...)`` 注入
    带 fake ws_manager 或 tmp_path workspace_service 的版本。
    """
    from src.api import task_registry

    return BackupService(
        workspace_service=workspace_service,
        ws_manager=None,
        active_task_checker=task_registry.has_active_tasks,
    )


def make_workspace_version_service(
    workspace_service: WorkspaceService,
) -> WorkspaceVersionService:
    """构造 ``WorkspaceVersionService``(P7-B-04)。

    对齐 ``api/deps.py:get_workspace_version_service`` 字段映射:内部复用
    新建的 ``BackupService``(``migrate`` 流程 pre-migration 备份依赖)。
    **CLI 不传 ws_manager**(``ws_manager=None``)。
    """
    backup_service = make_backup_service(workspace_service)
    return WorkspaceVersionService(
        workspace_service=workspace_service,
        backup_service=backup_service,
        ws_manager=None,
    )


class _CLIStageObserver:
    """`cmd_export_run` 注入的 ``ws_manager`` stub:把 stage event 写 stderr。

    实现 ``publish(channel, event, data)`` 等价接口(对齐
    ``src.api.websocket.manager.WebSocketManager.publish`` 签名);ExportService
    `_publish_stage` 调它推 ``stage=collecting/rendering/packaging/done`` 4 阶段
    + ``failed`` 兜底。CLI 同步阻塞跑 ``execute`` 时,每跨一个 stage 实时打
    stderr(``[export] stage=collecting ...``)— 用户看到进度反馈,
    ``--json`` 输出走 stdout 不被污染。
    """

    def __init__(self) -> None:
        self.events: list[dict[str, Any]] = []

    def publish(
        self, channel: str, event: str, data: dict[str, Any]
    ) -> None:
        self.events.append({"channel": channel, "event": event, "data": data})
        stage = data.get("stage", "?")
        export_id = data.get("export_id", "?")
        extras = {
            k: v for k, v in data.items() if k not in ("stage", "export_id")
        }
        extras_text = " ".join(f"{k}={v}" for k, v in extras.items())
        print(
            f"[export] stage={stage} export_id={export_id} {extras_text}".rstrip(),
            file=sys.stderr,
        )


def make_db_session(paths: WorkspacePaths) -> Session:
    """构造工作区 SQLite ``Session`` + 确保 schema 已建(``Base.metadata.create_all``)。

    P2-B-13 起 CLI 命令首次接入 SQLite;``WorkspaceService.create`` 只写
    ``meta.json`` 不建 ``novel.db``,本工厂 idempotent 建 schema 把 P2-B-04
    引入的三表(characters / plot_threads / timeline)以及既有 P1-B-03 表
    全部铺好。

    与 alembic 迁移路径不冲突:``create_all`` 看到表已存在直接跳过。P2-B-17
    起若 alembic 0003 加列,本工厂仍可用——schema drift 由专门的迁移命令
    管,本工厂只保"表存在",不保"表结构最新"。

    测试通过 ``monkeypatch.setattr(commands, "make_db_session", ...)`` 注入
    in-memory engine 替换。
    """
    engine = create_engine(f"sqlite:///{paths.db_path}")
    Base.metadata.create_all(engine)
    return Session(engine)


def make_vector_store(workspace_id: str, paths: WorkspacePaths) -> VectorStore:
    """构造工作区 ``VectorStore``,默认 ``embedding_fn=embed``(Qwen3 单例懒加载)。

    单测注入 mock embedding fn:``monkeypatch.setattr(commands, "make_vector_store",
    ...)``——避开真模型加载(~1.2GB)。

    懒 import ``embed`` 避免模块顶层引入触发 sentence_transformers 加载;只
    有 CLI 走真路径才加载。
    """
    from src.core.memory.embedding import embed

    return VectorStore(
        workspace_id,
        embedding_fn=embed,
        persist_dir=paths.chroma_dir,
    )


# ---- 内部辅助 ----------------------------------------------------------------


def _resolve_workspace_id(
    service: WorkspaceService, override: str | None
) -> str:
    """统一上下文解析:``override`` > ``_active`` 软链;都没 → 抛错。

    P1-B-14 ``WorkspaceService._set_active`` 写的是相对软链(目标 = 工作区
    目录名),所以 ``os.readlink`` 直接返工作区 ID。软链不存在或目标已被
    archive/purge 时,后续 service 调用自然抛 ``WorkspaceServiceError("工作区
    不存在")``,不需要在此预读 meta 验证。
    """
    if override:
        return override
    link = service.workspaces_root / _ACTIVE_LINK_NAME
    if link.is_symlink():
        return os.readlink(link)
    raise WorkspaceServiceError(
        "无活动工作区,且未指定 --workspace。先 `novel workspace switch <ID>` "
        "或在命令上加 `--workspace <ID>`。"
    )


def _print_workspace(ws: Workspace) -> None:
    """``info`` / ``create`` / ``switch`` / ``restore`` 共用的工作区元数据展示。"""
    print(f"workspace_id      : {ws.workspace_id}")
    print(f"display_name      : {ws.display_name}")
    print(f"genre             : {ws.genre}")
    print(f"premise           : {ws.premise}")
    print(f"status            : {ws.status}")
    print(f"engine_version    : {ws.engine_version}")
    print(f"created_at        : {ws.created_at.isoformat()}")
    print(f"last_active_at    : {ws.last_active_at.isoformat()}")
    print(f"total_chapters    : {ws.total_chapters}")
    print(f"completed_chapters: {ws.completed_chapters}")


def _print_orchestrator_result(result: OrchestratorResult) -> None:
    """``write-chapter`` / ``rewrite-chapter`` 的输出格式。

    与 API ``WriteResultSummary`` 字段一致,但是 CLI 给人看,加上 reviews 列表
    每条的 verdict / score 简报(API result 段也带 review_summary,这里同源)。
    """
    print(f"final_state       : {result.final_state.value}")
    print(f"final_version_id  : {result.final_version_id}")
    print(f"final_path        : {result.final_path}")
    print(f"version_count     : {len(result.versions)}")
    print(f"revise_count      : {result.revise_count}")
    print(f"rewrite_count     : {result.rewrite_count}")
    if result.escalation_reason:
        print(f"escalation_reason : {result.escalation_reason}")
    if result.reviews:
        print()
        print("reviews:")
        for i, r in enumerate(result.reviews, 1):
            print(
                f"  {i}. version_reviewed={r.version_reviewed} "
                f"verdict={r.verdict} score_overall={r.score_overall:.2f}"
            )


def _print_chapter_version(cv: ChapterVersion) -> None:
    """``revise-chapter`` 的输出格式;不打 ``content`` 全文(过长且终端不友好)。"""
    print(f"version_id        : {cv.version_id}")
    print(f"creation_type     : {cv.creation_type}")
    print(f"word_count        : {cv.word_count}")
    print(f"created_at        : {cv.created_at.isoformat()}")
    print(f"model_used        : {cv.model_used}")
    if cv.review_score is not None:
        print(f"review_score      : {cv.review_score:.2f}")


def _print_chapter_status(info: ChapterStatusInfo) -> None:
    """章节状态查询(P1 简版,3 档 + ``unknown``)。本任务暂未挂命令,留给 P2 升级
    chapters 表后再加 ``novel status-chapter``;此 helper 备用。"""
    print(f"chapter_id        : {info.chapter_id}")
    print(f"status            : {info.status}")
    print(f"latest_version_id : {info.latest_version_id}")
    print(f"has_final         : {info.has_final}")
    print(f"version_count     : {info.version_count}")


def _emit_json_or_human(
    args: Namespace,
    *,
    payload: Any,
    human_printer: Callable[[], None],
) -> None:
    """``--json`` flag 通用分发(P7-B-02 起统一引入)。

    ``args.json=True`` → ``json.dumps(payload, ensure_ascii=False, default=str)``
    单行 stdout(机器友好);``False`` → 调 ``human_printer()`` 走 key:value 风格
    (对齐 ``_print_workspace`` 既有人类可读风格)。

    ``payload`` 通常是 Pydantic ``.model_dump(mode="json")`` 结果,``default=str``
    兜底 datetime / Path 等非 JSON 原生类型(对齐 Pydantic mode="json" 行为)。
    """
    if getattr(args, "json", False):
        print(
            json.dumps(
                payload, ensure_ascii=False, default=str, sort_keys=False
            )
        )
    else:
        human_printer()


def _print_pipeline_snapshot(snapshot: PipelineRunSnapshot) -> None:
    """``pipeline start / pause / resume / stop / status`` 共用的 snapshot 展示。

    字段顺序对齐 ``PipelineStatusOut``(API_CONTRACT §4.5 字面);``state=idle``
    占位时只显示 ``workspace_id`` + ``state`` + 提示;``error`` 时附 ``error_details``。
    """
    print(f"run_id                : {snapshot.run_id or '(无 active run)'}")
    print(f"workspace_id          : {snapshot.workspace_id}")
    print(f"state                 : {snapshot.state}")
    if snapshot.state == "idle":
        print("(workspace 当前无 active 或历史 pipeline run)")
        return
    print(f"scope                 : {snapshot.scope}")
    print(f"current_chapter       : {snapshot.current_chapter or '(无)'}")
    print(f"progress              : {snapshot.progress:.2%}")
    print(
        f"completed_count       : "
        f"{len(snapshot.completed_chapter_ids)} / {len(snapshot.chapter_ids)}"
    )
    if snapshot.profile_override:
        print(f"profile_override      : {snapshot.profile_override}")
    if snapshot.pause_reason:
        print(f"pause_reason          : {snapshot.pause_reason}")
    if snapshot.error_details:
        print(f"error_details         : {snapshot.error_details}")
    print(f"provider_call_cap     : {snapshot.provider_call_cap}")
    print(f"provider_call_count   : {snapshot.provider_call_count}")
    print(f"transport_retry_count : {snapshot.transport_retry_count}")
    print(f"pipeline_attempt_count: {snapshot.pipeline_attempt_count}")
    print(
        "upstream_attempt_count: "
        f"{snapshot.upstream_generation_attempt_count}"
    )
    if snapshot.chapter_ids:
        print("chapter_ids           :")
        for cid in snapshot.chapter_ids:
            marker = "✓" if cid in snapshot.completed_chapter_ids else " "
            if cid == snapshot.current_chapter:
                marker = "▶"
            print(f"  [{marker}] {cid}")


def _print_pipeline_provider_cap_maintenance_result(
    result: PipelineProviderCapMaintenanceResult,
) -> None:
    """展示离线 cap 维护结果与恢复边界。"""

    print(f"workspace_id          : {result.workspace_id}")
    print(f"run_id                : {result.run_id}")
    print(f"state                 : {result.state}")
    print(f"pause_reason          : {result.pause_reason}")
    print(f"current_chapter       : {result.current_chapter}")
    print(f"provider_call_count   : {result.provider_call_count}")
    print(f"provider_call_cap     : {result.previous_cap} -> {result.new_cap}")
    print(f"provider_attempts    : {result.provider_attempt_status_counts}")
    print(f"backup_id             : {result.backup_id}")
    print(f"backup_file_path      : {result.backup_file_path}")
    print(f"backup_size_bytes     : {result.backup_size_bytes}")
    print(f"restart_required      : {str(result.restart_required).lower()}")
    next_action = (
        "从现有 Chapter UI 点击 Rewrite"
        if result.pause_reason == "chapter_escalated"
        else "从现有 Run UI 点击 Continue"
    )
    print(f"next_action           : {next_action}")


def _read_issues_file(path_str: str) -> list[ReviewIssue]:
    """读 ``--issues-file`` 路径,解析为 ``ReviewIssue`` 列表。

    校验顺序:文件存在 → JSON 合法 → 顶层是数组 → 每个元素满足 schema。
    任一失败抛 ``ValueError``,由 ``dispatch`` 翻译成退出码 1 + stderr。
    """
    path = Path(path_str)
    if not path.is_file():
        raise ValueError(f"--issues-file 文件不存在: {path}")
    try:
        raw = json.loads(path.read_text(encoding="utf-8"))
    except json.JSONDecodeError as exc:
        raise ValueError(f"--issues-file 内容不是合法 JSON: {exc}") from exc
    if not isinstance(raw, list):
        raise ValueError("--issues-file 内容必须是 JSON 数组")
    try:
        return [ReviewIssue.model_validate(item) for item in raw]
    except Exception as exc:  # pydantic.ValidationError
        raise ValueError(
            f"--issues-file 内有元素不符合 ReviewIssue schema: {exc}"
        ) from exc


# ---- workspace 子命令 -------------------------------------------------------


def cmd_workspace_create(args: Namespace) -> int:
    svc = make_workspace_service()
    ws = svc.create(
        args.workspace_id,
        display_name=args.display_name,
        genre=args.genre,
        premise=args.premise,
        total_chapters=args.total_chapters,
        activate=not args.no_activate,
    )
    print(f"已创建工作区: {ws.workspace_id}")
    _print_workspace(ws)
    return EXIT_OK


def cmd_workspace_list(args: Namespace) -> int:
    svc = make_workspace_service()
    workspaces = svc.list()
    if not workspaces:
        print("(尚无任何工作区)")
        return EXIT_OK
    active_id: str | None = None
    link = svc.workspaces_root / _ACTIVE_LINK_NAME
    if link.is_symlink():
        active_id = os.readlink(link)
    for ws in workspaces:
        marker = "* " if ws.workspace_id == active_id else "  "
        print(f"{marker}{ws.workspace_id:<24} [{ws.status}] {ws.display_name}")
    return EXIT_OK


def cmd_workspace_switch(args: Namespace) -> int:
    svc = make_workspace_service()
    ws = svc.switch(args.workspace_id)
    print(f"已切换激活工作区: {ws.workspace_id}")
    _print_workspace(ws)
    return EXIT_OK


def cmd_workspace_info(args: Namespace) -> int:
    svc = make_workspace_service()
    ws_id = _resolve_workspace_id(svc, args.workspace_id)
    ws = svc.info(ws_id)
    _print_workspace(ws)
    return EXIT_OK


def cmd_workspace_clean(args: Namespace) -> int:
    svc = make_workspace_service()
    ws_id = _resolve_workspace_id(svc, args.workspace_id)
    svc.clean(ws_id)
    print(f"已清理工作区运行时数据: {ws_id}(用户数据保留)")
    return EXIT_OK


def cmd_workspace_archive(args: Namespace) -> int:
    svc = make_workspace_service()
    ws_id = _resolve_workspace_id(svc, args.workspace_id)
    archive_file = svc.archive(ws_id)
    print(f"已归档工作区: {ws_id}")
    print(f"归档文件: {archive_file}")
    return EXIT_OK


def cmd_workspace_purge(args: Namespace) -> int:
    if not args.confirm:
        print(
            "purge 是不可逆操作,必须显式带 --confirm 才能执行。",
            file=sys.stderr,
        )
        return EXIT_BUSINESS_ERROR
    svc = make_workspace_service()
    ws_id = _resolve_workspace_id(svc, args.workspace_id)
    svc.purge(ws_id, confirm=True)
    print(f"已彻底删除工作区目录: {ws_id}(顶级 archives 备份保留,可 restore)")
    return EXIT_OK


def cmd_workspace_backup(args: Namespace) -> int:
    svc = make_workspace_service()
    ws_id = _resolve_workspace_id(svc, args.workspace_id)
    backup_file = svc.backup(ws_id)
    print(f"已备份工作区: {ws_id}")
    print(f"备份文件: {backup_file}")
    return EXIT_OK


def cmd_workspace_destroy(args: Namespace) -> int:
    """``novel workspace destroy <ws> --confirm <ws> [--json]``(P7-B-07)。

    对齐 ``POST /api/v1/workspaces/{id}:destroy`` 端点。三方全删 — 工作区目录
    + 工作区内 backups/(含在工作区目录内)+ 顶级 archives/<id>.tar.gz(若
    存在);**完全不可逆**。生产场景"写完一本书清磁盘"专用。

    流程:
    1. ``--confirm`` 二次确认 strict 必须等于 ``workspace_id``(对齐
       ``cmd_workspace_restore`` / ``cmd_workspace_migrate`` 同模式)
    2. 调 ``WorkspaceService.destroy`` 走完整流程(rmtree 工作区目录 + unlink
       顶级 archive + WS publish);CLI ``ws_manager=None`` 走 publish noop。
    3. 输出实际 ``deleted_paths`` 列表(``--json`` 走 stdout 单行,人类格式
       走 key:value 列表)
    """
    if args.confirm != args.workspace_id:
        print(
            f"destroy 是不可逆操作,必须用 --confirm 输入与 workspace_id 完全一致"
            f"的字符串才能执行(传入: --confirm={args.confirm!r}, "
            f"workspace_id={args.workspace_id!r})。",
            file=sys.stderr,
        )
        return EXIT_BUSINESS_ERROR

    svc = make_workspace_service()
    result = svc.destroy(args.workspace_id, confirm=args.confirm)

    payload = {
        "workspace_id": result.workspace_id,
        "deleted_paths": list(result.deleted_paths),
    }

    def _human() -> None:
        print(f"workspace_id  : {result.workspace_id}")
        print(f"deleted_paths : {len(result.deleted_paths)} 项")
        for path in result.deleted_paths:
            print(f"  - {path}")

    _emit_json_or_human(args, payload=payload, human_printer=_human)
    return EXIT_OK


def cmd_workspace_restore(args: Namespace) -> int:
    """``novel workspace restore <ws> (--backup-id <id> | --latest) --confirm <ws> [--json]``。

    对齐 ``POST /api/v1/workspaces/{id}:restore`` 端点(P6-B-07 新语义,backup_id
    模式)。**P1 期 ``WorkspaceService.restore`` tar.gz un-archive 模式不再
    暴露为 CLI 子命令**(自决项,对齐 P7-plan 决策 6=B + plan §"P7-B-04"段
    字面"CLI 子命令统一切到新 ``BackupService.restore`` + backup_id 模式");
    ``WorkspaceService.restore`` 方法本身保留供 ``v2 / 应急 recovery`` 程序化
    调用,不再有 CLI 入口。

    流程:
    1. ``--confirm`` 二次确认必须等于 ``workspace_id``(强 strict 防呆,对齐
       ``RestoreConfirmMismatch`` API 同模式)
    2. ``--backup-id`` 与 ``--latest`` 互斥二选一(argparse mutually_exclusive_group
       required=True):前者从 ``list_backups`` 列表挑指定 id;后者取列表第一项
       (BackupService.list_backups 已按 created_at 倒序排,第一项即最近备份)
    3. 调 ``BackupService.restore_from_backup`` 走完整流程(pre_restore 备份 +
       覆盖工作区 + WS publish + meta.last_active_at 刷新)
    """
    if args.confirm != args.workspace_id:
        print(
            f"restore 是覆盖性动作,必须用 --confirm 输入与 workspace_id 完全一致"
            f"的字符串才能执行(传入: --confirm={args.confirm!r}, "
            f"workspace_id={args.workspace_id!r})。",
            file=sys.stderr,
        )
        return EXIT_BUSINESS_ERROR

    workspace_service = make_workspace_service()
    backup_service = make_backup_service(workspace_service)

    # 解析 backup_id:显式 --backup-id 优先;--latest 时查 list_backups 取首项
    backup_id: str | None = args.backup_id
    if backup_id is None:
        if not args.latest:
            # argparse 的 mutually_exclusive_group required=True 已保此分支不
            # 应被触发,留作防御。
            print(
                "必须提供 --backup-id <id> 或 --latest 任一参数。",
                file=sys.stderr,
            )
            return EXIT_BUSINESS_ERROR
        backups = backup_service.list_backups(args.workspace_id)
        restorable_backups = [
            backup for backup in backups if backup.backup_type == "manual"
        ]
        if not restorable_backups:
            print(
                f"workspace {args.workspace_id} 当前无任何可全量恢复的手动备份"
                f"(运行 `novel workspace backups {args.workspace_id}` 查看)。",
                file=sys.stderr,
            )
            return EXIT_BUSINESS_ERROR
        backup_id = restorable_backups[0].backup_id

    result = backup_service.restore_from_backup(args.workspace_id, backup_id)
    payload = {
        "workspace_id": result.workspace_id,
        "backup_id": result.backup_id,
        "pre_restore_backup_id": result.pre_restore_backup_id,
    }

    def _human() -> None:
        print(f"workspace_id          : {result.workspace_id}")
        print(f"backup_id             : {result.backup_id}")
        print(f"pre_restore_backup_id : {result.pre_restore_backup_id}")

    _emit_json_or_human(args, payload=payload, human_printer=_human)
    return EXIT_OK


def cmd_workspace_migrate(args: Namespace) -> int:
    """``novel workspace migrate <workspace_id> --confirm <workspace_id> [--json]``。

    对齐 ``POST /api/v1/workspaces/{id}:migrate`` 端点(P6-B-08)。**最高破坏性**
    动作 — 触发 ``alembic upgrade head`` 应用到工作区 db + 落 meta.json 新
    schema_version / engine_version + WS publish ``version_migrated``,故强制
    ``--confirm`` 必须输入与 ``workspace_id`` 完全一致的字符串作为二次确认
    (对齐 P6-B-08 ``MigrateIn.confirm`` 同模式)。

    流程:
    1. ``--confirm`` 二次确认(CLI 早返;service 内部也做一遍兜底)
    2. service.migrate(): ① workspace engine major 校验 ② BackupService.
       create_manual_backup 做 pre-migration 备份(失败迁移不开始 — rollback
       后路保证)③ alembic upgrade head ④ 写 meta.json
    """
    if args.confirm != args.workspace_id:
        print(
            f"migrate 是覆盖性动作,必须用 --confirm 输入与 workspace_id 完全一致"
            f"的字符串才能执行(传入: --confirm={args.confirm!r}, "
            f"workspace_id={args.workspace_id!r})。",
            file=sys.stderr,
        )
        return EXIT_BUSINESS_ERROR
    workspace_service = make_workspace_service()
    version_service = make_workspace_version_service(workspace_service)
    result = version_service.migrate(args.workspace_id, args.confirm)
    payload = {
        "workspace_id": result.workspace_id,
        "pre_migration_backup_id": result.pre_migration_backup_id,
        "from_engine_version": result.from_engine_version,
        "to_engine_version": result.to_engine_version,
        "from_schema_version": result.from_schema_version,
        "to_schema_version": result.to_schema_version,
    }
    _emit_json_or_human(
        args,
        payload=payload,
        human_printer=lambda: _print_migration_result(result),
    )
    return EXIT_OK


def cmd_workspace_version_status(args: Namespace) -> int:
    """``novel workspace version-status <workspace_id> [--json]``。

    对齐 ``GET /api/v1/workspaces/{id}/version-status`` 端点(P6-B-08)。读
    workspace meta.json + 当前引擎 → 比对返 ``VersionStatus``(无副作用);
    ``migration_required=true`` 时前端 / 用户据此决定是否调 ``:migrate``。
    """
    workspace_service = make_workspace_service()
    version_service = make_workspace_version_service(workspace_service)
    status = version_service.get_version_status(args.workspace_id)
    payload = {
        "workspace_id": status.workspace_id,
        "engine_version_match": status.engine_version_match,
        "schema_version_match": status.schema_version_match,
        "current_engine": status.current_engine,
        "workspace_engine": status.workspace_engine,
        "current_schema": status.current_schema,
        "workspace_schema": status.workspace_schema,
        "migration_required": status.migration_required,
    }
    _emit_json_or_human(
        args,
        payload=payload,
        human_printer=lambda: _print_version_status(status),
    )
    return EXIT_OK


def cmd_workspace_backups(args: Namespace) -> int:
    """``novel workspace backups <workspace_id> [--json]``。

    对齐 ``GET /api/v1/workspaces/{id}/backups`` 端点(P6-B-07)。列出最近
    30 份自动备份 + 全部手动备份(按 created_at 倒序;不含内部 pre_restore_*)。
    空目录返 ``backups=[]`` + 友好文案。
    """
    workspace_service = make_workspace_service()
    backup_service = make_backup_service(workspace_service)
    backups = backup_service.list_backups(args.workspace_id)
    payload = {
        "workspace_id": args.workspace_id,
        "backups": [
            {
                "backup_id": b.backup_id,
                "backup_type": b.backup_type,
                "workspace_id": b.workspace_id,
                "created_at": b.created_at.isoformat(),
                "chapter_id": b.chapter_id,
                "schema_version": b.schema_version,
                "engine_version": b.engine_version,
                "file_path": b.file_path,
                "size_bytes": b.size_bytes,
            }
            for b in backups
        ],
    }

    def _human() -> None:
        print(f"workspace_id  : {args.workspace_id}")
        print(f"backup_count  : {len(backups)}")
        if not backups:
            print("(workspace 当前无任何备份)")
            return
        print("backups:")
        for b in backups:
            _print_backup_info(b)

    _emit_json_or_human(args, payload=payload, human_printer=_human)
    return EXIT_OK


def _print_version_status(status: VersionStatus) -> None:
    """``version-status`` 子命令人类可读展示。"""
    print(f"workspace_id          : {status.workspace_id}")
    print(f"current_engine        : {status.current_engine}")
    print(f"workspace_engine      : {status.workspace_engine}")
    print(f"engine_version_match  : {status.engine_version_match}")
    print(f"current_schema        : {status.current_schema}")
    print(f"workspace_schema      : {status.workspace_schema}")
    print(f"schema_version_match  : {status.schema_version_match}")
    print(f"migration_required    : {status.migration_required}")


def _print_migration_result(result: MigrationResult) -> None:
    """``migrate`` 子命令人类可读展示。"""
    print(f"workspace_id              : {result.workspace_id}")
    print(f"pre_migration_backup_id   : {result.pre_migration_backup_id}")
    print(f"from_engine_version       : {result.from_engine_version}")
    print(f"to_engine_version         : {result.to_engine_version}")
    print(f"from_schema_version       : {result.from_schema_version}")
    print(f"to_schema_version         : {result.to_schema_version}")


def _print_backup_info(info: BackupInfo) -> None:
    """``backups`` 子命令中单条备份的展示。"""
    print(f"  backup_id      : {info.backup_id}")
    print(f"    backup_type  : {info.backup_type}")
    print(f"    created_at   : {info.created_at.isoformat()}")
    if info.chapter_id is not None:
        print(f"    chapter_id   : {info.chapter_id}")
    print(f"    schema       : {info.schema_version}")
    print(f"    engine       : {info.engine_version}")
    print(f"    size_bytes   : {info.size_bytes}")


def _print_import_assets_result(result: ImportAssetsResult) -> None:
    """``import-assets`` 输出格式:文件清单 + 四类条目数。"""
    print(f"workspace_id      : {result.workspace_id}")
    if result.files_processed:
        print("files_processed   :")
        for filename in result.files_processed:
            print(f"  - {filename}")
    else:
        print("files_processed   : (无,目录下未找到任何资产文件)")
    print("counts:")
    for entity_type in ("character", "plot_thread", "timeline", "fact"):
        print(f"  {entity_type:<12}: {result.counts.get(entity_type, 0)}")


def cmd_workspace_import_assets(args: Namespace) -> int:
    """``novel workspace import-assets <md_dir> [--workspace WS_ID]``。

    流程:
    1. 解析工作区 ID(``--workspace`` 或 ``_active`` 软链)。
    2. ``WorkspaceService.info`` 读 meta.json 验工作区合法。
    3. 构造 ``WorkspacePaths`` + ``Session``(``make_db_session``)+
       ``VectorStore``(``make_vector_store``)。
    4. ``ensure_workspace_orm`` 同步 meta → ``WorkspaceORM``(idempotent)。
    5. 调 ``import_assets`` service 解析 + 入库;成功后 ``session.commit()``。
    6. 失败回滚事务 + 抛 ``AssetImportError``(由 dispatch 翻译退出码 1)。
    """
    workspace_service = make_workspace_service()
    ws_id = _resolve_workspace_id(workspace_service, args.workspace)
    workspace = workspace_service.info(ws_id)

    md_dir = Path(args.md_dir)
    paths = WorkspacePaths(
        ws_id, workspaces_root=workspace_service.workspaces_root
    )

    session = make_db_session(paths)
    try:
        vector_store = make_vector_store(ws_id, paths)
        ensure_workspace_orm(session, workspace)
        try:
            result = import_assets(
                md_dir,
                workspace_id=ws_id,
                db_session=session,
                vector_store=vector_store,
            )
        except Exception:
            session.rollback()
            raise
        else:
            session.commit()
    finally:
        session.close()

    print(f"导入资产完成: workspace={ws_id} md_dir={md_dir}")
    _print_import_assets_result(result)
    return EXIT_OK


# ---- 章节命令 ---------------------------------------------------------------


def cmd_write_chapter(args: Namespace) -> int:
    workspace_service = make_workspace_service()
    chapter_service = make_chapter_service(workspace_service)
    ws_id = _resolve_workspace_id(workspace_service, args.workspace)
    print(f"写章: workspace={ws_id} chapter={args.chapter_id}")
    result = chapter_service.write(ws_id, args.chapter_id)
    _print_orchestrator_result(result)
    return EXIT_OK


def cmd_rewrite_chapter(args: Namespace) -> int:
    workspace_service = make_workspace_service()
    chapter_service = make_chapter_service(workspace_service)
    ws_id = _resolve_workspace_id(workspace_service, args.workspace)
    print(
        f"重写: workspace={ws_id} chapter={args.chapter_id} "
        f"prior_rewrite_count={args.prior_rewrite_count}"
    )
    result = chapter_service.rewrite(
        ws_id,
        args.chapter_id,
        prior_rewrite_count=args.prior_rewrite_count,
    )
    _print_orchestrator_result(result)
    return EXIT_OK


def cmd_revise_chapter(args: Namespace) -> int:
    workspace_service = make_workspace_service()
    chapter_service = make_chapter_service(workspace_service)
    ws_id = _resolve_workspace_id(workspace_service, args.workspace)
    issues = _read_issues_file(args.issues_file)
    print(
        f"修订: workspace={ws_id} chapter={args.chapter_id} "
        f"issues_count={len(issues)} source_version_id={args.source_version_id}"
    )
    new_version = chapter_service.revise(
        ws_id,
        args.chapter_id,
        issues=issues,
        source_version_id=args.source_version_id,
    )
    _print_chapter_version(new_version)
    return EXIT_OK


# ---- chapter 子命令组(P7-B-01) -------------------------------------------


def cmd_chapter_rewind(args: Namespace) -> int:
    """``novel chapter rewind <workspace_id> <chapter_id> --confirm <workspace_id>``。

    对齐 ``POST /chapters/{chapter_id}:rewind`` 端点(P6-B-01)。**最高破坏性**
    动作 — 目标章回退 + 后续章 invalidated + Chroma soft-delete + extracted_*
    三表 revoked_at,故强制 ``--confirm`` 必须输入与 ``workspace_id`` 完全一致
    的字符串作为二次确认(对齐 P6-B-08 ``workspace migrate`` confirm 语义,
    plan 字面 ``[--confirm <id>]`` 类 B 工程判断升级为 required;impl 文档登记)。
    """
    if args.confirm != args.workspace_id:
        print(
            f"rewind 是高破坏性动作,必须用 --confirm 输入与 workspace_id 完全一致"
            f"的字符串才能执行(传入: --confirm={args.confirm!r}, "
            f"workspace_id={args.workspace_id!r})。",
            file=sys.stderr,
        )
        return EXIT_BUSINESS_ERROR
    workspace_service = make_workspace_service()
    rewind_service = make_chapter_rewind_service(workspace_service)
    result = rewind_service.rewind(args.workspace_id, args.chapter_id)
    print(f"chapter_id            : {result.chapter_id}")
    print(f"from_status           : {result.from_status}")
    print(f"to_status             : {result.to_status}")
    print(f"invalidated_count     : {len(result.invalidated_chapter_ids)}")
    if result.invalidated_chapter_ids:
        print("invalidated_chapter_ids:")
        for cid in result.invalidated_chapter_ids:
            print(f"  - {cid}")
    print(f"vector_revoked_count  : {result.vector_revoked_count}")
    print(f"extracted_revoked_count: {result.extracted_revoked_count}")
    return EXIT_OK


def cmd_chapter_force_pass(args: Namespace) -> int:
    """``novel chapter force-pass <workspace_id> <chapter_id> --reason <text>``。

    对齐 ``POST /chapters/{chapter_id}:force-pass`` 端点(P6-B-01)。``--reason``
    必传(对齐 ``ChapterForcePassIn.reason`` ``min_length=1`` 强制非空,
    PROJECT_SPEC §5.4 字面"破坏性操作要 user 显式留痕")。argparse 层面
    ``required=True`` 已保 reason 非 None;空字符串由 service 抛
    ``ChapterRewindServiceError`` 翻 ``EXIT_BUSINESS_ERROR``。
    """
    workspace_service = make_workspace_service()
    rewind_service = make_chapter_rewind_service(workspace_service)
    result = rewind_service.force_pass(
        args.workspace_id, args.chapter_id, reason=args.reason
    )
    print(f"chapter_id        : {result.chapter_id}")
    print(f"from_status       : {result.from_status}")
    print(f"to_status         : {result.to_status}")
    print(f"force_pass_at     : {result.force_pass_at.isoformat()}")
    print(f"force_pass_reason : {result.force_pass_reason}")
    return EXIT_OK


def cmd_chapter_unblock(args: Namespace) -> int:
    """``novel chapter unblock <workspace_id> <chapter_id>``。

    对齐 ``POST /chapters/{chapter_id}:unblock`` 端点(P6-B-01)。单章 unblock
    把 ``invalidated`` → ``drafting``。非 ``invalidated`` 源由 service 抛
    ``ChapterUnblockInvalidStateError`` 翻 ``EXIT_BUSINESS_ERROR``。
    """
    workspace_service = make_workspace_service()
    rewind_service = make_chapter_rewind_service(workspace_service)
    result = rewind_service.unblock(args.workspace_id, args.chapter_id)
    print(f"chapter_id  : {result.chapter_id}")
    print(f"from_status : {result.from_status}")
    print(f"to_status   : {result.to_status}")
    return EXIT_OK


def cmd_chapter_unblock_all(args: Namespace) -> int:
    """``novel chapter unblock-all <workspace_id>``。

    对齐 ``POST /workspaces/{workspace_id}:unblock-all`` 端点(P6-B-01)。批量
    把 workspace 内所有 ``invalidated`` 章节 → ``drafting``。空状态幂等
    (``unblocked_count=0``)。
    """
    workspace_service = make_workspace_service()
    rewind_service = make_chapter_rewind_service(workspace_service)
    result = rewind_service.unblock_all(args.workspace_id)
    print(f"workspace_id    : {result.workspace_id}")
    print(f"unblocked_count : {result.unblocked_count}")
    if result.unblocked_chapter_ids:
        print("unblocked_chapter_ids:")
        for cid in result.unblocked_chapter_ids:
            print(f"  - {cid}")
    return EXIT_OK


# ---- pipeline 子命令组(P7-B-02) ------------------------------------------


def cmd_pipeline_start(args: Namespace) -> int:
    """``novel pipeline start <workspace_id> --scope <s> [...] [--json]``。

    对齐 ``POST /api/v1/pipeline:start`` 端点(P6-B-02 / B-03)。scope 4 档
    ``single | range | volume | until``;字段映射约束由 service 层
    ``PipelineService.start`` + ``_resolve_chapters`` 跨字段校验(违规抛
    ``PipelineInvalidScopeError`` → ``EXIT_BUSINESS_ERROR``)。

    **CLI 不调 ``execute``**(对齐 API ``:start`` 端点同样设计 — 章节循环由
    BackgroundTasks 在响应已发出后跑;CLI 无 BackgroundTasks 上下文)。本命令
    只 INSERT pipeline_runs 行 + publish ``pipeline_started``;若 server 进程
    正在运行,server 端的 BackgroundTasks 不会接管(它们是 server 进程 :start
    端点的 hook),CLI 启动的 run 实际不会跑章节,仅持久化 SQLite 行(适合
    "手动占位 run 后用 server :resume 接管"或测试 / 调试场景)。
    """
    workspace_service = make_workspace_service()
    pipeline_service = make_pipeline_service(workspace_service)
    snapshot = pipeline_service.start(
        args.workspace_id,
        scope=args.scope,
        start_chapter_id=args.start,
        end_chapter_id=args.end,
        volume_id=args.volume_id,
        profile_override=args.profile,
        provider_call_cap=args.provider_call_cap,
    )
    _emit_json_or_human(
        args,
        payload=snapshot.model_dump(mode="json"),
        human_printer=lambda: _print_pipeline_snapshot(snapshot),
    )
    return EXIT_OK


def cmd_pipeline_pause(args: Namespace) -> int:
    """``novel pipeline pause <workspace_id> [--json]``。

    对齐 ``POST /api/v1/pipeline:pause`` 端点。无 active run → 抛
    ``PipelineNotRunningError`` → ``EXIT_BUSINESS_ERROR``。
    无 run_id flag — 自动按 workspace 找 active run(对齐 API 端点同样行为)。
    """
    workspace_service = make_workspace_service()
    pipeline_service = make_pipeline_service(workspace_service)
    snapshot = pipeline_service.pause(args.workspace_id)
    _emit_json_or_human(
        args,
        payload=snapshot.model_dump(mode="json"),
        human_printer=lambda: _print_pipeline_snapshot(snapshot),
    )
    return EXIT_OK


def cmd_pipeline_resume(args: Namespace) -> int:
    """``novel pipeline resume <workspace_id> [--json]``。

    对齐 ``POST /api/v1/pipeline:resume`` 端点。``paused → running``。
    **注**:CLI 调 ``resume`` 后**不会自动重启 execute 循环**(与 API
    ``:resume`` 端点不同 — 后者通过 BackgroundTasks 重启)。用户需 server
    进程才能接续跑章节;CLI 单独 ``resume`` 等同"翻 state 占位"。
    """
    workspace_service = make_workspace_service()
    pipeline_service = make_pipeline_service(workspace_service)
    snapshot = pipeline_service.resume(args.workspace_id)
    _emit_json_or_human(
        args,
        payload=snapshot.model_dump(mode="json"),
        human_printer=lambda: _print_pipeline_snapshot(snapshot),
    )
    return EXIT_OK


def cmd_pipeline_stop(args: Namespace) -> int:
    """``novel pipeline stop <workspace_id> [--json]``。

    对齐 ``POST /api/v1/pipeline:stop`` 端点。``running / paused / error``
    → ``stopped``(error 也可 stop — service 层 ``_load_active_or_error_run``
    宽过滤)。
    """
    workspace_service = make_workspace_service()
    pipeline_service = make_pipeline_service(workspace_service)
    snapshot = pipeline_service.stop(args.workspace_id)
    _emit_json_or_human(
        args,
        payload=snapshot.model_dump(mode="json"),
        human_printer=lambda: _print_pipeline_snapshot(snapshot),
    )
    return EXIT_OK


def cmd_pipeline_status(args: Namespace) -> int:
    """``novel pipeline status <workspace_id> [--json]``。

    对齐 ``GET /api/v1/pipeline/status`` 端点。返当前 active run 状态;无
    active run → 取最近 1 行(任意 state);无任何历史 → ``state=idle``
    占位 snapshot。**永不抛错**(无 run 也是合法状态)。
    """
    workspace_service = make_workspace_service()
    pipeline_service = make_pipeline_service(workspace_service)
    snapshot = pipeline_service.status(args.workspace_id)
    _emit_json_or_human(
        args,
        payload=snapshot.model_dump(mode="json"),
        human_printer=lambda: _print_pipeline_snapshot(snapshot),
    )
    return EXIT_OK


def cmd_pipeline_increase_provider_cap(args: Namespace) -> int:
    """离线提高已耗尽、已暂停 run 的 Provider hard cap。

    命令只做完整备份与 cap CAS，不翻 run state、不构造 LLM client、不执行
    Worker。成功后仍须启动唯一后端，再从结果指定的既有产品 UI 入口恢复。
    """

    workspace_service = make_workspace_service()
    service = make_pipeline_provider_cap_maintenance_service(workspace_service)
    result = service.increase_cap(
        workspace_id=args.workspace_id,
        run_id=args.run_id,
        expected_current_cap=args.expected_current_cap,
        new_cap=args.new_cap,
        confirm=args.confirm,
    )
    _emit_json_or_human(
        args,
        payload=result.model_dump(mode="json"),
        human_printer=lambda: _print_pipeline_provider_cap_maintenance_result(result),
    )
    return EXIT_OK


# ---- export 子命令组(P7-B-03) --------------------------------------------


def _print_export_run_result(result: dict[str, Any]) -> None:
    """`cmd_export_run` 同步阻塞完成后的最终结果展示(对齐 ``ExportService.execute``
    return dict 字段名)。"""
    print(f"export_id         : {result['export_id']}")
    print(f"workspace_id      : {result['workspace_id']}")
    print(f"task_id           : {result['task_id']}")
    print(f"status            : {result['status']}")
    if result["status"] == "done":
        print(f"format            : {result.get('format')}")
        print(f"scope             : {result.get('scope')}")
        print(f"chapter_count     : {result.get('chapter_count')}")
        print(f"file_size_bytes   : {result.get('file_size_bytes')}")
    elif result["status"] == "failed":
        print(f"error             : {result.get('error')}")


def _print_export_item(item: dict[str, Any]) -> None:
    """list 子命令中单条 manifest 展示(对齐 ``ExportItemOut`` 字段)。"""
    print(f"  export_id        : {item.get('export_id')}")
    print(f"    format         : {item.get('format')}")
    print(f"    scope          : {item.get('scope')}")
    print(f"    status         : {item.get('status')}")
    print(f"    created_at     : {item.get('created_at')}")
    completed = item.get("completed_at")
    if completed:
        print(f"    completed_at   : {completed}")
    chapter_count = item.get("chapter_count")
    if chapter_count is not None:
        print(f"    chapter_count  : {chapter_count}")
    file_size = item.get("file_size_bytes")
    if file_size is not None:
        print(f"    file_size_bytes: {file_size}")
    error = item.get("error")
    if error:
        print(f"    error          : {error}")


def cmd_export_run(args: Namespace) -> int:
    """``novel export run <workspace_id> --format <f> --scope <s> [...] [--json]``。

    对齐 ``POST /api/v1/workspaces/{id}/exports`` 端点(P6-B-04 / B-05 / B-06)。
    format 4 档 ``md | chaptered_md | epub | docx``;scope 3 档 ``full | volume |
    range``;字段映射约束由 service 层校验(``ExportScopeInvalidError`` /
    ``ExportFormatUnsupportedError`` → ``EXIT_BUSINESS_ERROR``)。

    **CLI 同步阻塞跑 ``execute``**(与 API ``:create`` 端点不同 —— API 用
    ``BackgroundTasks`` 异步;CLI 无该上下文,直接同步直调阻塞用户终端)。stage
    事件由注入的 ``_CLIStageObserver`` 实时推到 stderr,``--json`` 输出走 stdout
    不污染。``execute`` 内部异常顶层 swallow 翻 manifest ``failed`` + return
    dict ``status='failed'``,本 handler 据 ``status`` 决定 rc(0 / 1)。

    **类 B 处置**(impl 文档登记):启动指令字面 "polls GET /tasks/{task_id}
    每 2s 直到 done" 工程判断改为 service 直调 + stage observer。理由:
    ① task_registry 是进程内 LRU(CLI / server 各自进程不共享,polls 必须
    server 在跑);② CLI service 直调模式 P7-B-01/02 沉淀;③ 同步阻塞 + stage
    observer 推 stderr 实现等效"用户实时看进度"UX,不破坏 CLI 自给原则。
    """
    workspace_service = make_workspace_service()
    observer = _CLIStageObserver()
    service = make_export_service(workspace_service, ws_manager=observer)

    # 局部 import 避循环 (task_registry 在 src.api 层)
    from src.api import task_registry

    task_id = task_registry.new_task_id()
    task_registry.record_pending(task_id, type="workspace_export")
    export_id, _manifest = service.create_export(
        args.workspace_id,
        task_id=task_id,
        format=args.format,
        scope=args.scope,
        volume_id=args.volume_id,
        start_chapter_id=args.start,
        end_chapter_id=args.end,
        include_metadata=args.include_metadata,
        include_review_history=args.include_review_history,
        include_length_notes=not getattr(args, "no_length_notes", False),
    )
    result = service.execute(
        args.workspace_id, task_id=task_id, export_id=export_id
    )
    _emit_json_or_human(
        args,
        payload=result,
        human_printer=lambda: _print_export_run_result(result),
    )
    return EXIT_OK if result.get("status") == "done" else EXIT_BUSINESS_ERROR


def cmd_export_list(args: Namespace) -> int:
    """``novel export list <workspace_id> [--json]``。

    对齐 ``GET /api/v1/workspaces/{id}/exports`` 端点。按 ``created_at`` 倒序列
    出最近 10 条 manifest(含 pending / running / done / failed 全档)。空列表
    返 0 + 友好文案。
    """
    workspace_service = make_workspace_service()
    service = make_export_service(workspace_service)
    manifests = service.list_exports(args.workspace_id)

    def _human() -> None:
        print(f"workspace_id      : {args.workspace_id}")
        print(f"export_count      : {len(manifests)}")
        if not manifests:
            print("(workspace 当前无任何导出条目)")
            return
        print("exports:")
        for m in manifests:
            _print_export_item(m)

    _emit_json_or_human(
        args,
        payload={"workspace_id": args.workspace_id, "items": manifests},
        human_printer=_human,
    )
    return EXIT_OK


def cmd_export_download(args: Namespace) -> int:
    """``novel export download <workspace_id> <export_id> --out <path> [--json]``。

    对齐 ``GET /api/v1/workspaces/{id}/exports/{export_id}/download`` 端点。
    调 ``ExportService.get_download_path`` 拿源 zip 物理路径 + ``shutil.copyfile``
    写到用户指定 ``--out`` 路径(不存在的目录树自动创建);成功打印输出路径 +
    字节数。``export_id`` 不存在 / 任务未完成 → ``ExportNotFoundError``
    → ``EXIT_BUSINESS_ERROR``。
    """
    import shutil

    workspace_service = make_workspace_service()
    service = make_export_service(workspace_service)
    zip_path = service.get_download_path(args.workspace_id, args.export_id)

    out_path = Path(args.out)
    out_path.parent.mkdir(parents=True, exist_ok=True)
    shutil.copyfile(zip_path, out_path)
    size = out_path.stat().st_size

    payload = {
        "workspace_id": args.workspace_id,
        "export_id": args.export_id,
        "source_path": str(zip_path),
        "output_path": str(out_path.resolve()),
        "file_size_bytes": size,
    }

    def _human() -> None:
        print(f"workspace_id      : {args.workspace_id}")
        print(f"export_id         : {args.export_id}")
        print(f"source_path       : {zip_path}")
        print(f"output_path       : {out_path.resolve()}")
        print(f"file_size_bytes   : {size}")

    _emit_json_or_human(args, payload=payload, human_printer=_human)
    return EXIT_OK


# ---- logs 子命令(P4-B-05) ------------------------------------------------


def cmd_logs(args: Namespace) -> int:
    """``novel logs ...`` — 日志查询(对齐 OBSERVABILITY §2.6 + §4.1 / §4.2)。

    参数语义:
    - ``--chapter`` / ``--trace`` / ``--request`` / ``--event`` / ``--level``
      / ``--workspace``:多字段 AND 过滤;
    - ``--since`` / ``--until``:ISO 8601 时间范围;
    - ``--tail [N]``:取最近 ``N`` 条(默认 50),按 timestamp 降序输出;
    - ``--limit N``(非 ``--tail``):分页第一页输出条数上限,默认 50,按
      timestamp 升序输出。

    输出格式:每行一条,``[timestamp] level event field1=val1 ...``
    人类友好 + machine grep 友好混合。
    """
    from datetime import datetime

    from src.config import settings
    from src.services.logs_service import LogQueryFilters, LogsService

    def _parse_when(value: str | None) -> datetime | None:
        if value is None:
            return None
        text = value.replace("Z", "+00:00") if value.endswith("Z") else value
        try:
            return datetime.fromisoformat(text)
        except ValueError as exc:  # 翻成 ValueError → dispatch 翻为 1
            raise ValueError(
                f"--since / --until 必须是 ISO 8601 时间,无法解析:{value!r}"
            ) from exc

    service = LogsService(data_dir=settings.data_dir)
    if args.tail is not None:
        rows = service.tail(n=args.tail)
    else:
        # ``--workspace`` 缺省遍历全工作区(无 _active 默认绑定):logs 是
        # 系统级查询,要求"先切活动工作区才能看日志"反而提高使用门槛;
        # caller 真要按工作区过滤显式传 ``--workspace``。
        workspace_id = args.workspace
        filters = LogQueryFilters(
            workspace_id=workspace_id,
            chapter_id=args.chapter,
            trace_id=args.trace,
            request_id=args.request,
            event=args.event,
            level=args.level,
            since=_parse_when(args.since),
            until=_parse_when(args.until),
            page=1,
            page_size=args.limit,
        )
        rows, _total = service.query(filters)

    for row in rows:
        ts = row.get("timestamp", "")
        level = row.get("level", "?")
        event = row.get("event", "")
        extras = []
        for key in (
            "trace_id",
            "request_id",
            "workspace_id",
            "chapter_id",
            "module",
        ):
            val = row.get(key)
            if val is not None:
                extras.append(f"{key}={val}")
        suffix = " ".join(extras)
        print(f"[{ts}] {level} {event} {suffix}".rstrip())
    return EXIT_OK


# ---- dispatch ---------------------------------------------------------------


_BUSINESS_ERRORS: tuple[type[BaseException], ...] = (
    WorkspaceServiceError,
    ChapterServiceError,
    ChapterRewindServiceError,
    PipelineServiceError,
    ExportServiceError,
    BackupServiceError,
    WorkspaceVersionServiceError,
    ChapterVersioningError,
    MetaFileError,
    AssetImportError,
    # ``_read_issues_file`` 校验失败 / 其余路由层主动抛的合法性错误
    ValueError,
    # 兜底:某些 IO 路径未被翻译(理论上 service 已封,留作防御)
    FileNotFoundError,
)


def dispatch(args: Namespace) -> int:
    """命令分发:每条 ``set_defaults(handler=...)`` 由本函数统一调用 + 错误归类。

    成功 → handler 返回值(0)。已知业务错误 → 1 + 错误信息打 stderr。
    未知异常 → 2 + traceback 打 stderr(便于排查)。
    """
    handler: Callable[[Namespace], int] = args.handler
    try:
        return handler(args)
    except _BUSINESS_ERRORS as exc:
        print(f"错误: {exc}", file=sys.stderr)
        return EXIT_BUSINESS_ERROR
    except Exception:
        traceback.print_exc()
        return EXIT_UNKNOWN_ERROR
