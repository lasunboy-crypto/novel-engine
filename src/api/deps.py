"""FastAPI 依赖注入工厂(P1-B-18 起)。

P1 提供:

- ``get_workspace_service``:每次请求 new ``WorkspaceService``(无状态)。
- ``get_chapter_service``(P1-B-19):每次请求 new ``ChapterService``,内部
  组合 ``WorkspaceService`` + ``ClaudeClient`` + 默认模型名。测试 override
  时直接替换 ``get_chapter_service`` 即可,不必再绑 LLM。
- ``get_workspace_id``(P1-B-19):章节路由的 ``X-Workspace-Id`` header 解析。
  缺失抛 ``MissingWorkspaceHeader (422)``。

为什么不用模块级单例
====================

模块级 ``WorkspaceService()`` 在 import 时就 ``settings.data_dir.resolve()``,
会污染测试 cwd 解析顺序;改为 Depends 工厂后,真实请求路径与测试覆盖路径都
显式可控。``ChapterService`` 同理(还要避开"测试 import 时实例化 ClaudeClient
触发 API key 校验"的问题)。

模型名硬编码原因
================

P1 默认模型 ``claude-sonnet-4-6``(对应 P1-B-08 ``cloud_quality`` profile)。
``Settings`` 暂未提供 ``default_model`` 字段,跨任务调整 P1-B-01 不在本任务
范围内;**P3 引入 Profile 体系时再加 Settings 字段**,届时本工厂改读
``settings.default_model``,签名不变。

LLM provider 切换(P1-B-22 / v1.0.x post-wave5 H1 routing completion fixup)
==========================================================================

LLM 客户端构造统一走 ``src.core.llm.factory.make_llm_client``。**post-wave5
H1 routing completion fixup**(本次)起:deps 层 bootstrap client 构造点
统一走 ``_resolve_bootstrap_target()`` 读引擎默认 ``config.yaml`` 的
``writer_routing.default`` profile 三元组 ``(provider, model, base_url)``
再传给工厂,**不再依赖 ``settings.llm_provider`` 全局开关**。

本 fixup 修法解决的 bug:H1 routing completion(轮 172)只改 router 路径,
deps.py 的 bootstrap client 仍只传 ``model`` 不传 ``provider``/``base_url``
→ 工厂 fallback ``settings.llm_provider`` → 若维护人 .env 残留
``LLM_PROVIDER=openai_compatible`` 但 deps 没渠道传 base_url → ValueError 500。
修后:deps bootstrap 跟着 engine default profile 字面走,与 routing
completion 设计原则一致(provider 跟 profile 字面,不看 settings)。
"""

from __future__ import annotations

import sqlite3
import threading
from collections.abc import Callable
from pathlib import Path

import structlog
from fastapi import Depends, Header
from sqlalchemy import create_engine
from sqlalchemy.orm import Session

from src.api.errors import MissingWorkspaceHeader
from src.api.websocket.manager import manager as _ws_manager_singleton
from src.config import settings
from src.core.db import Base
from src.core.llm.factory import make_llm_client
from src.core.llm.base import LLMClient, LLMResponse, Message
from src.core.memory.vector import (
    VectorStore,
    inspect_persisted_chapter_vectors_read_only,
)
from src.core.routing.router import ModelRouter
from src.core.routing.run_model_manifest_router import (
    build_manifest_fallback_sentinel,
    build_model_router,
)
from src.core.workspace.workspace_paths import WorkspacePaths
from src.services.backup_service import BackupService
from src.services.attempt_ledger import wrap_router_with_attempt_tracking
from src.services.character_service import CharacterService
from src.services.chapter_service import ChapterService
from src.services.config_service import load_workspace_config
from src.services.cost_guard import (
    CostGuardConfig,
    load_cost_guard_config,
)
from src.services.core_state_service import CoreStateService
from src.services.card_generator_config import (
    CardGeneratorConfig,
    load_card_generator_config,
)
from src.services.chapter_card_service import ChapterCardService
from src.services.chapter_operation_lease import ChapterOperationLeaseStore
from src.services.chapter_rewind_service import ChapterRewindService
from src.services.chapter_summary_service import ChapterSummaryService
from src.services.export_service import ExportService
from src.services.memory_health_service import MemoryHealthService
from src.services.outline_service import OutlineService
from src.services.pipeline_service import PipelineService
from src.services.style_audit_service import StyleAuditService
from src.services.style_template_service import StyleTemplateService
from src.services.thread_service import PlotThreadService
from src.services.timeline_service import TimelineEntryService
from src.services.validator_report_service import ValidatorReportService
from src.services.volume_service import VolumeService
from src.services.workspace_service import WorkspaceService
from src.services.workspace_version_service import (
    WorkspaceVersionService,
    WorkspaceVersionServiceError,
    get_current_schema_version,
    validate_workspace_for_runtime_open,
)
from src.services.workspace_operation_guard import (
    WorkspaceFileLock,
    acquire_workspace_file_lock,
)
from src.version import ENGINE_VERSION

# P1 默认模型(P1-B-08 cloud_quality profile)。P3 升 Settings.default_model 时改。
_DEFAULT_MODEL = "claude-sonnet-4-6"

# P4-acceptance:cost_guard 配置模块级缓存(冷启动加载一次,所有请求复用)。
# 配置变更需重启进程生效;若需要 hot-reload,P5+ 加 SIGHUP / watchdog。
# 加载失败(yaml 不存在 / 解析错)时 fallback 到 ``None``,gate 静默跳过
# (对齐 ``ChapterService._run_gates`` 内 try/except 同源)。
try:
    _COST_GUARD_CONFIG: CostGuardConfig | None = load_cost_guard_config()
except Exception:  # noqa: BLE001
    _COST_GUARD_CONFIG = None


# card_generator 辅助配置模块级缓存(同 cost_guard 模式)。批量出卡已退役；
# 加载失败时只回退到相似度阈值与样式控制器的字段默认值。
try:
    _CARD_GENERATOR_CONFIG: CardGeneratorConfig = load_card_generator_config()
except Exception:  # noqa: BLE001
    _CARD_GENERATOR_CONFIG = CardGeneratorConfig()


# v1.0.x A2-01:VectorStore 进程级缓存(2026-05-19 hot-fix wave 2)。
#
# 根因:ChromaDB ``PersistentClient`` 内部用 ``SharedSystemClient._identifier_to_system``
# 类级缓存按"path + settings"键去重 system 实例;同一磁盘路径在同进程内第二次
# 实例化(且 settings 与首次不一致)会抛 ``ValueError: An instance of Chroma
# already exists for ephemeral with different settings``,导致 ``:write`` 第二次
# 请求必挂。
#
# 修法沿用 ``cost_guard._WORKSPACE_COST_GUARD_CACHE`` 模式 — 工作区级 lazy 缓存
# + 显式 reset 函数;destroy / purge / archive / restore / restore_from_backup
# 之后必须清缓存(对应工作区 chroma/ 目录被删或被覆盖,缓存的 VectorStore 引
# 用陈旧)。无锁(单进程 FastAPI;Uvicorn worker 多 process 每 worker 独立缓存,
# 不共享是预期行为)。
_VECTOR_STORE_CACHE: dict[str, VectorStore] = {}

# R8：普通 API session 只能打开本进程 startup admission 已对账的 current
# 工作区。key 使用 resolved workspaces_root，避免测试/多数据根互相污染。
_RUNTIME_ADMISSION_LOCK = threading.RLock()
_RUNTIME_ADMISSION_CONFIGURED_ROOTS: set[str] = set()
_RUNTIME_ADMITTED_WORKSPACES: dict[str, set[str]] = {}
_RUNTIME_UNINITIALIZED_WORKSPACES: dict[str, set[str]] = {}
_RUNTIME_RESTART_REQUIRED_WORKSPACES: dict[str, set[str]] = {}
_RUNTIME_DATABASE_IDENTITIES: dict[str, dict[str, tuple[int, int]]] = {}
_RUNTIME_UNINITIALIZED_DATABASE_STATES: dict[
    str,
    dict[str, tuple[int, int, int, int, int] | None],
] = {}
_RUNTIME_INITIALIZATION_MARKER = ".runtime_initialization_pending.json"


def _database_file_state(
    db_path: Path,
) -> tuple[int, int, int, int, int] | None:
    """返回足够识别 startup 时同一空 DB 的只读文件状态。"""

    if not db_path.is_file():
        return None
    stat = db_path.stat()
    return (
        stat.st_dev,
        stat.st_ino,
        stat.st_size,
        stat.st_mtime_ns,
        stat.st_ctime_ns,
    )


class _LifespanNoProviderClient(LLMClient):
    """冷启动本地恢复占位；任何误入生成路径都在读密钥/出网前硬停。"""

    def complete(
        self,
        messages: list[Message],  # noqa: ARG002
        **gen_params: object,  # noqa: ARG002
    ) -> LLMResponse:
        raise RuntimeError("FastAPI lifespan 本地恢复禁止构造或调用 Provider client")


class _WorkspaceLockedSession(Session):
    """Session 生命周期内持有跨进程 shared lock。"""

    def __init__(
        self,
        *args: object,
        workspace_file_lock: WorkspaceFileLock,
        **kwargs: object,
    ) -> None:
        self._workspace_file_lock = workspace_file_lock
        super().__init__(*args, **kwargs)

    def close(self) -> None:
        try:
            super().close()
        finally:
            self._workspace_file_lock.close()


def reset_vector_store_cache(workspace_id: str | None = None) -> None:
    """A2-01:清 VectorStore 工作区级缓存。

    :param workspace_id: 给定时清单工作区;``None`` 时清全部(单测 / 全量 reload
        场景)。
    """
    if workspace_id is None:
        _VECTOR_STORE_CACHE.clear()
    else:
        _VECTOR_STORE_CACHE.pop(workspace_id, None)


def configure_runtime_workspace_admission(
    workspaces_root: Path,
    *,
    admitted_workspace_ids: list[str],
    restart_required_workspace_ids: list[str],
    uninitialized_workspace_ids: list[str] | None = None,
) -> None:
    """用一次完整 startup admission 的结果替换当前进程准入状态。"""

    root_key = str(workspaces_root.resolve())
    with _RUNTIME_ADMISSION_LOCK:
        _RUNTIME_ADMISSION_CONFIGURED_ROOTS.add(root_key)
        _RUNTIME_ADMITTED_WORKSPACES[root_key] = set(admitted_workspace_ids)
        _RUNTIME_UNINITIALIZED_WORKSPACES[root_key] = set(
            uninitialized_workspace_ids or []
        )
        _RUNTIME_RESTART_REQUIRED_WORKSPACES[root_key] = set(
            restart_required_workspace_ids
        )
        identities: dict[str, tuple[int, int]] = {}
        for workspace_id in admitted_workspace_ids:
            db_path = WorkspacePaths(
                workspace_id,
                workspaces_root=workspaces_root,
            ).db_path
            if db_path.is_file():
                stat = db_path.stat()
                identities[workspace_id] = (stat.st_dev, stat.st_ino)
        _RUNTIME_DATABASE_IDENTITIES[root_key] = identities
        _RUNTIME_UNINITIALIZED_DATABASE_STATES[root_key] = {
            workspace_id: _database_file_state(
                WorkspacePaths(
                    workspace_id,
                    workspaces_root=workspaces_root,
                ).db_path
            )
            for workspace_id in uninitialized_workspace_ids or []
        }


def mark_runtime_workspace_restart_required(
    workspaces_root: Path,
    workspace_id: str,
) -> None:
    """在线 migrate 后锁住普通 API，直到重启完成 startup recovery。"""

    root_key = str(workspaces_root.resolve())
    with _RUNTIME_ADMISSION_LOCK:
        _RUNTIME_ADMISSION_CONFIGURED_ROOTS.add(root_key)
        _RUNTIME_ADMITTED_WORKSPACES.setdefault(root_key, set()).discard(workspace_id)
        _RUNTIME_UNINITIALIZED_WORKSPACES.setdefault(root_key, set()).discard(
            workspace_id
        )
        _RUNTIME_DATABASE_IDENTITIES.setdefault(root_key, {}).pop(workspace_id, None)
        _RUNTIME_UNINITIALIZED_DATABASE_STATES.setdefault(root_key, {}).pop(
            workspace_id, None
        )
        _RUNTIME_RESTART_REQUIRED_WORKSPACES.setdefault(root_key, set()).add(
            workspace_id
        )


def reset_runtime_workspace_admission(workspaces_root: Path | None = None) -> None:
    """清进程准入缓存；生产只随进程退出清空，显式调用仅供测试。"""

    with _RUNTIME_ADMISSION_LOCK:
        if workspaces_root is None:
            _RUNTIME_ADMISSION_CONFIGURED_ROOTS.clear()
            _RUNTIME_ADMITTED_WORKSPACES.clear()
            _RUNTIME_UNINITIALIZED_WORKSPACES.clear()
            _RUNTIME_RESTART_REQUIRED_WORKSPACES.clear()
            _RUNTIME_DATABASE_IDENTITIES.clear()
            _RUNTIME_UNINITIALIZED_DATABASE_STATES.clear()
            return
        root_key = str(workspaces_root.resolve())
        _RUNTIME_ADMISSION_CONFIGURED_ROOTS.discard(root_key)
        _RUNTIME_ADMITTED_WORKSPACES.pop(root_key, None)
        _RUNTIME_UNINITIALIZED_WORKSPACES.pop(root_key, None)
        _RUNTIME_RESTART_REQUIRED_WORKSPACES.pop(root_key, None)
        _RUNTIME_DATABASE_IDENTITIES.pop(root_key, None)
        _RUNTIME_UNINITIALIZED_DATABASE_STATES.pop(root_key, None)


def _assert_current_workspace_meta(
    workspaces_root: Path,
    workspace_id: str,
) -> None:
    workspace = WorkspaceService(workspaces_root=workspaces_root).info(workspace_id)
    current_schema = get_current_schema_version()
    if (
        workspace.engine_version != ENGINE_VERSION
        or workspace.schema_version != current_schema
    ):
        raise WorkspaceVersionServiceError(
            "runtime admission: workspace meta 不是 current，普通 API 拒绝打开: "
            f"workspace={workspace_id} "
            f"engine={workspace.engine_version!r}/{ENGINE_VERSION!r} "
            f"schema={workspace.schema_version!r}/{current_schema!r}"
        )


def _prepare_runtime_workspace_session(
    workspaces_root: Path,
    workspace_id: str,
) -> tuple[WorkspaceFileLock, bool]:
    """原子完成 epoch 准入并获取 session 生命周期 shared file lock。

    返回 ``(lock, needs_initialization)``。调用方必须在异常时关闭 lock，并在
    首次 ``create_all`` 成功后记录数据库 inode。
    """

    root_key = str(workspaces_root.resolve())
    with _RUNTIME_ADMISSION_LOCK:
        if workspace_id in _RUNTIME_RESTART_REQUIRED_WORKSPACES.get(root_key, set()):
            raise WorkspaceVersionServiceError(
                "工作区已迁移或启动时被跳过，必须重启后完成 startup "
                f"recovery 才能继续普通操作: {workspace_id}"
            )
        _assert_current_workspace_meta(workspaces_root, workspace_id)
        configured = root_key in _RUNTIME_ADMISSION_CONFIGURED_ROOTS
        admitted = workspace_id in _RUNTIME_ADMITTED_WORKSPACES.get(root_key, set())
        uninitialized = workspace_id in _RUNTIME_UNINITIALIZED_WORKSPACES.get(
            root_key, set()
        )
        db_path = WorkspacePaths(
            workspace_id,
            workspaces_root=workspaces_root,
        ).db_path

        if admitted:
            expected_identity = _RUNTIME_DATABASE_IDENTITIES.get(root_key, {}).get(
                workspace_id
            )
            if not db_path.is_file() or db_path.stat().st_size == 0:
                raise WorkspaceVersionServiceError(
                    "已准入工作区数据库在运行中丢失或清空，拒绝静默重建: "
                    f"{workspace_id}"
                )
            stat = db_path.stat()
            actual_identity = (stat.st_dev, stat.st_ino)
            if expected_identity is None or actual_identity != expected_identity:
                raise WorkspaceVersionServiceError(
                    "已准入工作区数据库在运行中被替换，必须保留现场并重启对账: "
                    f"{workspace_id}"
                )
            needs_initialization = False
        elif uninitialized:
            expected_uninitialized_state = _RUNTIME_UNINITIALIZED_DATABASE_STATES.get(
                root_key, {}
            ).get(workspace_id)
            if _database_file_state(db_path) != expected_uninitialized_state:
                raise WorkspaceVersionServiceError(
                    "未初始化工作区数据库在 startup epoch 内丢失、替换或变化，"
                    f"拒绝静默初始化: {workspace_id}"
                )
            needs_initialization = True
        else:
            # 未配置 root 只用于测试/嵌入式调用；已配置 root 中的未知工作区只
            # 能是 startup 后刚由产品创建的 current workspace。
            initialized = validate_workspace_for_runtime_open(
                WorkspaceService(workspaces_root=workspaces_root),
                workspace_id,
            )
            if configured and initialized:
                raise WorkspaceVersionServiceError(
                    "现存工作区未经过本进程 startup recovery，拒绝直接打开；"
                    f"请重启后再继续: {workspace_id}"
                )
            _RUNTIME_ADMISSION_CONFIGURED_ROOTS.add(root_key)
            if initialized:
                stat = db_path.stat()
                _RUNTIME_ADMITTED_WORKSPACES.setdefault(root_key, set()).add(
                    workspace_id
                )
                _RUNTIME_DATABASE_IDENTITIES.setdefault(root_key, {})[workspace_id] = (
                    stat.st_dev,
                    stat.st_ino,
                )
                needs_initialization = False
            else:
                _RUNTIME_UNINITIALIZED_WORKSPACES.setdefault(root_key, set()).add(
                    workspace_id
                )
                _RUNTIME_UNINITIALIZED_DATABASE_STATES.setdefault(root_key, {})[
                    workspace_id
                ] = _database_file_state(db_path)
                needs_initialization = True

        # 保持 admission lock 直到 shared file lock 到手；maintenance 只有拿到
        # exclusive lock 后才会写并在释放前 mark restart，消除 validate/create
        # 与 marker 之间的 TOCTOU。
        file_lock = acquire_workspace_file_lock(
            workspaces_root,
            workspace_id,
            mode="shared",
        )
        if workspace_id in _RUNTIME_RESTART_REQUIRED_WORKSPACES.get(root_key, set()):
            file_lock.close()
            raise WorkspaceVersionServiceError(
                f"工作区维护状态已变化，必须重启后再继续: {workspace_id}"
            )
        if needs_initialization:
            expected_state = _RUNTIME_UNINITIALIZED_DATABASE_STATES.get(
                root_key, {}
            ).get(workspace_id)
            if _database_file_state(db_path) != expected_state:
                file_lock.close()
                raise WorkspaceVersionServiceError(
                    "未初始化工作区数据库在取得维护互斥锁前发生变化，"
                    f"拒绝静默初始化: {workspace_id}"
                )
        else:
            expected_identity = _RUNTIME_DATABASE_IDENTITIES.get(root_key, {}).get(
                workspace_id
            )
            state_after_lock = _database_file_state(db_path)
            if (
                expected_identity is None
                or state_after_lock is None
                or state_after_lock[2] == 0
                or state_after_lock[:2] != expected_identity
            ):
                file_lock.close()
                raise WorkspaceVersionServiceError(
                    "已准入工作区数据库在取得维护互斥锁前丢失、清空或替换，"
                    f"必须保留现场并重启对账: {workspace_id}"
                )
        return file_lock, needs_initialization


def _record_runtime_workspace_initialized(
    workspaces_root: Path,
    workspace_id: str,
) -> None:
    root_key = str(workspaces_root.resolve())
    db_path = WorkspacePaths(
        workspace_id,
        workspaces_root=workspaces_root,
    ).db_path
    if not db_path.is_file() or db_path.stat().st_size == 0:
        raise WorkspaceVersionServiceError(
            f"工作区首次数据库初始化未产出有效文件: {workspace_id}"
        )
    stat = db_path.stat()
    with _RUNTIME_ADMISSION_LOCK:
        _RUNTIME_UNINITIALIZED_WORKSPACES.setdefault(root_key, set()).discard(
            workspace_id
        )
        _RUNTIME_ADMITTED_WORKSPACES.setdefault(root_key, set()).add(workspace_id)
        _RUNTIME_DATABASE_IDENTITIES.setdefault(root_key, {})[workspace_id] = (
            stat.st_dev,
            stat.st_ino,
        )
        _RUNTIME_UNINITIALIZED_DATABASE_STATES.setdefault(root_key, {}).pop(
            workspace_id, None
        )


def reset_global_config_caches() -> None:
    """v1.1 wave 2 H6:清进程级 / 引擎默认 config.yaml 派生的模块级缓存。

    适用场景
    --------
    维护人 / 管理员改了**引擎默认** ``config.yaml`` (而非工作区 ``project.yaml``)
    并希望不重启进程即时生效时调用。当前两个模块级缓存:

    - ``_COST_GUARD_CONFIG`` (P4-acceptance,line 96-99):引擎默认 cost_guard 段
    - ``_CARD_GENERATOR_CONFIG`` (P5-B-06,line 105-108):引擎默认 card_generator 段

    本 helper 清空两个 module global → 下次 ``get_chapter_service`` / 等依赖
    工厂构造时会重新读 config.yaml 派生新配置(deps.py 顶部 try-except 兜底
    模式保留 — yaml 损坏时 fallback 到字面默认)。

    工作区级 vs 全局
    ----------------
    - 工作区级 ``project.yaml`` 改动 → 走 ``reset_workspace_config_caches(workspace_id)``
      (清工作区 cost_guard cache,不动模块级);
    - 引擎默认 ``config.yaml`` 改动 → 走本 helper (清模块级,所有工作区下次取
      config 即拿新值)。

    **router 路径无需 invalidate**:``_make_router_factory()`` 字面每次 LLM
    调用都 ``load_workspace_config(workspace_id)`` 重读 yaml(P3-B-06 设计),
    routing 配置改完立即生效,无需 cache reset。
    """
    global _COST_GUARD_CONFIG, _CARD_GENERATOR_CONFIG

    try:
        _COST_GUARD_CONFIG = load_cost_guard_config()
    except Exception:  # noqa: BLE001
        _COST_GUARD_CONFIG = None

    try:
        _CARD_GENERATOR_CONFIG = load_card_generator_config()
    except Exception:  # noqa: BLE001
        _CARD_GENERATOR_CONFIG = CardGeneratorConfig()


def reset_workspace_config_caches(workspace_id: str) -> None:
    """v1.1 wave 2 H6:清工作区级 config 派生缓存(统一入口)。

    调用方:``PUT /workspaces/{id}/config`` 写完后调,让本次写入对当前进程
    立即可见(替代分散在各 service 内的 ``reset_*`` 调用,集中维护)。

    当前清的缓存
    ------------
    - ``cost_guard`` 工作区级 lazy cache (``reset_workspace_cost_guard_config_cache``,
      P7-B-05 引入)

    无需清的(各 service 自己保证即时生效)
    ----------------------------------------
    - ``reviewer`` 配置:``ConfigService.load_reviewer_config(workspace_id)``
      每次 caller 实例化重读 yaml,无独立缓存。
    - ``retrieval`` 配置:同 reviewer,每次 caller 重读。
    - ``writer_profiles`` / ``writer_routing`` / ``agent_routing``:
      ``_make_router_factory()`` 字面每次 LLM 调用重读 yaml (P3-B-06)。

    本 helper 是**统一入口** — 未来若新增工作区级 service-side 缓存
    (例:H6 后续若加 reviewer / retrieval 模块级缓存优化),只需在本函数
    内追加 reset 调用,caller 端 (``routes/config.py``) 无需改动。
    """
    from src.services.cost_guard import (
        reset_workspace_cost_guard_config_cache,
    )

    reset_workspace_cost_guard_config_cache(workspace_id)


def get_workspace_service() -> WorkspaceService:
    """构造 ``WorkspaceService``,绑定 ``settings.data_dir``(默认行为)。

    P7-B-07 起注入 ``_ws_manager_singleton`` — ``WorkspaceService.destroy``
    需 publish ``workspace_destroyed`` WS event(其他既有方法不依赖 ws_manager;
    注入 None 也兼容,仅 destroy 在 ws_manager=None 时 publish noop)。
    """

    return WorkspaceService(ws_manager=_ws_manager_singleton)


def get_chapter_service(
    workspace_service: WorkspaceService = Depends(get_workspace_service),
) -> ChapterService:
    """构造 ``ChapterService``,内部装一个真实 ``ClaudeClient`` +
    ``session_factory`` 注入(P2-B-17 起 chapters 表 7 档 state 同步)。

    测试通过 ``app.dependency_overrides[get_chapter_service] = lambda: ...``
    注入 mock 客户端版本(``ChapterService(workspace_service=..., client=mock,
    model=...)``)。注意覆盖的是 ``get_chapter_service`` 本身,不必再覆盖
    ``get_workspace_service``——FastAPI 解依赖时一旦命中 override 就不再
    继续解嵌套依赖(避免覆盖路径与生产路径深度耦合)。

    ``session_factory`` 与 ``CharacterService`` / ``PlotThreadService`` /
    ``TimelineEntryService`` 同模板,复用 ``_make_session_factory``——每个
    请求 new 一个工作区 ``Session``,``Base.metadata.create_all`` idempotent
    建 schema(production 下还会加上 alembic 迁移,本工厂仅作 schema 兜底)。

    P6-B-07 起:注入 ``BackupService`` — ChapterService approve transaction
    后同步调 ``create_auto_backup``(失败 swallow 不阻塞 approve)。
    """

    client, model = _resolve_bootstrap_client()
    session_factory = _make_session_factory(workspace_service.workspaces_root)
    return ChapterService(
        workspace_service=workspace_service,
        client=client,
        model=model,
        session_factory=session_factory,
        vector_store_factory=_make_vector_store_factory(
            workspace_service.workspaces_root
        ),
        router_factory=_make_router_factory(workspace_service.workspaces_root),
        cost_guard_config=_COST_GUARD_CONFIG,
        ws_manager=_ws_manager_singleton,
        backup_service=BackupService(
            workspace_service=workspace_service,
            ws_manager=_ws_manager_singleton,
        ),
        memory_enrichment_scheduler="background",
        operation_lease_store=ChapterOperationLeaseStore(session_factory),
    )


def get_core_state_service(
    workspace_service: WorkspaceService = Depends(get_workspace_service),
) -> CoreStateService:
    """构造无 Provider 依赖的核心状态/书内时钟审计服务。"""

    return CoreStateService(
        session_factory=_make_session_factory(workspace_service.workspaces_root)
    )


def _make_router_factory(
    workspaces_root: Path | None = None,
) -> Callable[[str], ModelRouter]:
    """构造 ``ModelRouter`` 工厂(P3-B-06 起)。

    每次调用 ``factory(workspace_id)`` 通过
    ``services.config_service.load_workspace_config`` 合并引擎默认 ``config.yaml``
    + 工作区 ``project.yaml`` 覆盖,产出新 ``ModelRouter`` 实例。每次写作请求
    重新构造 router(成本极低:一次 yaml 读取 + ``WriterProfilesConfig`` 校
    验),保证 hot-reload 工作区配置也能立即生效。

    LLMClient 缓存仍发生在 router 内部:同 ``(provider, model, base_url)``
    三元组在单次 router 实例的生命周期内复用 client(P3-B-05 决策)。
    """

    resolved_root = (
        workspaces_root
        if workspaces_root is not None
        else WorkspaceService().workspaces_root
    )
    session_factory = _make_session_factory(resolved_root)

    def factory(workspace_id: str) -> ModelRouter:
        from src.services.post_v8_abc_selection_activity import (
            load_product_selection_activity,
        )

        load_product_selection_activity(workspace_id)
        config = load_workspace_config(workspace_id)
        return wrap_router_with_attempt_tracking(
            build_model_router(config, config_path=settings.config_yaml_path),
            workspace_id=workspace_id,
            session_factory=session_factory,
        )  # type: ignore[return-value]

    return factory


def _make_session_factory(workspaces_root: Path) -> Callable[[str], Session]:
    """构造工作区 SQLite ``Session`` 工厂(P2-B-14 起)。

    每次调用返回一个绑定到 ``workspaces/<workspace_id>/data/novel.db`` 的
    ``Session``;``Base.metadata.create_all`` idempotent 建 schema(沿用 P2-B-13
    ``cli/commands.make_db_session`` 风格,与 alembic 迁移路径不冲突——
    ``create_all`` 看到表已存在直接跳过,schema drift 由专门的迁移命令负责)。
    """

    def factory(workspace_id: str) -> Session:
        file_lock, needs_initialization = _prepare_runtime_workspace_session(
            workspaces_root,
            workspace_id,
        )
        paths = WorkspacePaths(workspace_id, workspaces_root=workspaces_root)
        if needs_initialization:
            # 先消费一次性 fresh marker；此后任一崩溃都 fail-closed，绝不在
            # 下一进程把已开始初始化的空/半库再当成全新工作区重建。
            (paths.root / _RUNTIME_INITIALIZATION_MARKER).unlink()
        engine = create_engine(f"sqlite:///{paths.db_path}")
        try:
            Base.metadata.create_all(engine)
            session = _WorkspaceLockedSession(
                bind=engine,
                workspace_file_lock=file_lock,
            )
            if needs_initialization:
                from src.services.asset_import import ensure_workspace_orm

                workspace = WorkspaceService(workspaces_root=workspaces_root).info(
                    workspace_id
                )
                ensure_workspace_orm(session, workspace)
                session.commit()
                _record_runtime_workspace_initialized(
                    workspaces_root,
                    workspace_id,
                )
            return session
        except Exception:
            engine.dispose()
            file_lock.close()
            raise

    return factory


def _make_existing_session_factory(
    workspaces_root: Path,
) -> Callable[[str], Session]:
    """构造 lifespan 恢复专用 Session 工厂，绝不创建 DB 或补建表。

    进入该工厂前，``main`` 已用只读 admission 验证 meta 和 schema。这里仍以
    SQLite ``mode=rw`` 打开既有文件，避免 admission 后路径被移走时退化成
    静默创建空库；启动恢复所需的状态写入仍按正常事务提交。
    """

    def factory(workspace_id: str) -> Session:
        file_lock, needs_initialization = _prepare_runtime_workspace_session(
            workspaces_root,
            workspace_id,
        )
        if needs_initialization:
            file_lock.close()
            raise FileNotFoundError(
                f"启动恢复只允许打开已初始化工作区数据库: {workspace_id}"
            )
        paths = WorkspacePaths(workspace_id, workspaces_root=workspaces_root)
        db_path = paths.db_path.resolve()
        if not db_path.is_file():
            file_lock.close()
            raise FileNotFoundError(f"启动恢复只允许打开既有工作区数据库: {db_path}")
        database_uri = f"{db_path.as_uri()}?mode=rw"
        engine = create_engine(
            "sqlite://",
            creator=lambda: sqlite3.connect(
                database_uri,
                uri=True,
                check_same_thread=False,
            ),
        )
        try:
            return _WorkspaceLockedSession(
                bind=engine,
                workspace_file_lock=file_lock,
            )
        except Exception:
            engine.dispose()
            file_lock.close()
            raise

    return factory


def _make_vector_store_factory(
    workspaces_root: Path,
) -> Callable[[str], VectorStore]:
    """构造工作区 ``VectorStore`` 工厂(P2-B-19 起)。

    每次调用 ``factory(workspace_id)`` 返回一个新 ``VectorStore`` 实例,绑定
    ``workspaces/<workspace_id>/data/chroma/`` 目录 + Qwen3-Embedding 单例
    懒加载 ``embed`` 函数。

    懒 import ``embed`` 避免模块顶层引入触发 sentence_transformers 加载;只
    有 API 真路径走才加载(测试 override 时不触发)。

    测试通过 ``app.dependency_overrides[get_chapter_service] = lambda: ...``
    替整个 ChapterService(本工厂不被单独 override)。
    """

    def factory(workspace_id: str) -> VectorStore:
        # v1.0.x A2-01:工作区级缓存命中即复用,miss 才新建。ChromaDB PersistentClient
        # 在同路径多次实例化(settings 与既存不一致)会抛 ValueError,缓存避免
        # 第二次写章节请求挂。reset_vector_store_cache 由 workspace_service.destroy
        # / purge / archive / restore + BackupService.restore_from_backup 调用清失效。
        cached = _VECTOR_STORE_CACHE.get(workspace_id)
        if cached is not None:
            return cached

        from src.core.memory.embedding import embed

        paths = WorkspacePaths(workspace_id, workspaces_root=workspaces_root)
        store = VectorStore(
            workspace_id,
            embedding_fn=embed,
            persist_dir=paths.chroma_dir,
        )
        _VECTOR_STORE_CACHE[workspace_id] = store
        return store

    return factory


def get_character_service(
    workspace_service: WorkspaceService = Depends(get_workspace_service),
) -> CharacterService:
    """构造 ``CharacterService``,session 工厂默认按工作区目录开 SQLite。

    测试通过 ``app.dependency_overrides[get_character_service] = lambda: ...``
    注入带 in-memory engine 的 service 替换。
    """

    return CharacterService(
        workspace_service=workspace_service,
        session_factory=_make_session_factory(workspace_service.workspaces_root),
    )


def get_thread_service(
    workspace_service: WorkspaceService = Depends(get_workspace_service),
) -> PlotThreadService:
    """构造 ``PlotThreadService``,session 工厂默认按工作区目录开 SQLite。

    与 ``get_character_service`` 同模板(P2-B-15)。
    """

    return PlotThreadService(
        workspace_service=workspace_service,
        session_factory=_make_session_factory(workspace_service.workspaces_root),
    )


def get_timeline_service(
    workspace_service: WorkspaceService = Depends(get_workspace_service),
) -> TimelineEntryService:
    """构造 ``TimelineEntryService``,session 工厂默认按工作区目录开 SQLite。

    与 ``get_character_service`` / ``get_thread_service`` 同模板(P2-B-16)。
    """

    return TimelineEntryService(
        workspace_service=workspace_service,
        session_factory=_make_session_factory(workspace_service.workspaces_root),
    )


_BOOTSTRAP_LOG = structlog.get_logger(__name__)


def _resolve_bootstrap_target() -> tuple[str, str, str | None]:
    """读引擎默认 ``config.yaml`` 返 default profile 的 ``(provider, model, base_url)`` 三元组。

    供 deps.py 各 ``get_*_service`` bootstrap LLM client 构造点统一调用。
    **post-wave5 H1 routing completion fixup**(本次)修法:bootstrap client
    不再依赖 ``settings.llm_provider`` 全局开关派发,而是按引擎默认
    ``config.yaml`` 的 ``writer_routing.default`` profile 字面三元组派发,
    与 routing completion 设计原则(provider 跟 profile 字面走)一致。

    为何不带 ``workspace_id``
    --------------------------
    FastAPI Depends 解析 ``get_*_service`` 时无请求上下文(``X-Workspace-Id``
    在 router 层才解析,deps 早于此)。bootstrap client 是构造时的占位,
    实际 provider 请求都走 router_factory 重新装 client(router 是
    workspace-aware 的)，并由 R5 统一 attempt 包装器在网络前持久化计数。
    bootstrap client 只作 legacy/test 回退占位。

    Fallback
    --------
    ``config.yaml`` 不存在 / yaml 语法错 / Pydantic 交叉校验失败 → 回到
    ``(settings.llm_provider, _DEFAULT_MODEL, None)`` 极端兜底,保护:
    - 单测场景未真实创建 config.yaml
    - 临时损坏 yaml(维护人在编辑)
    - 引擎首次启动 yaml 还没写
    Fallback 时打 structlog warning,便于诊断。

    :return: ``(provider, model, base_url)`` 三元组,可直接 ``**`` 解构传给
        ``make_llm_client(model=..., base_url=..., provider=...)``。
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
    """Build legacy bootstrap only when no central manifest is selected.

    Production services still accept a constructor-level fallback for old
    tests and legacy call sites.  Under a single-model manifest that fallback
    must be credential-free and must fail if accidentally invoked; all real
    calls go through ``_make_router_factory``.
    """

    sentinel = build_manifest_fallback_sentinel(
        config_path=settings.config_yaml_path
    )
    if sentinel is not None:
        return sentinel
    provider, model, base_url = _resolve_bootstrap_target()
    return make_llm_client(model, base_url=base_url, provider=provider), model


def get_memory_health_service(
    workspace_service: WorkspaceService = Depends(get_workspace_service),
) -> MemoryHealthService:
    """构造 ``MemoryHealthService``,session 工厂按工作区目录开 SQLite,
    LLM 客户端通过 ``make_llm_client(...)`` 装(对齐 ``get_chapter_service``
    模式)。**post-wave5 H1 routing completion fixup**:走
    ``_resolve_bootstrap_target()`` 读引擎默认 ``config.yaml`` 的 default
    profile 三元组,bootstrap client 不再受 ``settings.llm_provider`` 全局
    开关影响；生产路径另注入带 R5 attempt 账本的 router_factory。

    测试通过 ``app.dependency_overrides[get_memory_health_service] = lambda: ...``
    注入带 fake LLM + in-memory engine 的 service。

    ``ws_manager`` 默认 ``_ws_manager_singleton``(API 路径下 ``severity=critical``
    时 WS publish ``system.alerts`` 推送;单测覆盖通过 deps override 注入
    测试 manager 或 ``None`` 检查不推送语义)。
    """

    client, model = _resolve_bootstrap_client()
    return MemoryHealthService(
        workspace_service=workspace_service,
        session_factory=_make_session_factory(workspace_service.workspaces_root),
        llm=client,
        model=model,
        router_factory=_make_router_factory(workspace_service.workspaces_root),
        ws_manager=_ws_manager_singleton,
    )


def get_outline_service(
    workspace_service: WorkspaceService = Depends(get_workspace_service),
) -> OutlineService:
    """构造 ``OutlineService``(P5-B-04)。

    LLM 客户端通过 ``make_llm_client(...)`` 装(post-wave5 H1 routing
    completion fixup 起走 ``_resolve_bootstrap_target()``,bootstrap client
    跟引擎默认 profile 字面派发);router_factory 走 ``_make_router_factory()``
    同 ChapterService 模式 — 走 ``agent_routing.outline_builder`` 路由(P5
    plan-kickoff 决策 4=B 默认 ``local_qwen_32k``,但 mock 模式直接 dispatch
    到 MockLLMClient outline_* 分支)。

    ``session_factory`` 注入用于 ``outline:commit`` 端点落 ``VolumeORM``;
    ``ws_manager`` 单例用于 ``:generate`` 5 stage WS 推送。

    测试通过 ``app.dependency_overrides[get_outline_service] = lambda: ...``
    注入 fake LLM + tmp_path workspaces 版本。
    """
    client, model = _resolve_bootstrap_client()
    return OutlineService(
        workspace_service=workspace_service,
        client=client,
        model=model,
        router_factory=_make_router_factory(workspace_service.workspaces_root),
        session_factory=_make_session_factory(workspace_service.workspaces_root),
        ws_manager=_ws_manager_singleton,
    )


def get_chapter_card_service(
    workspace_service: WorkspaceService = Depends(get_workspace_service),
    chapter_service: ChapterService = Depends(get_chapter_service),
) -> ChapterCardService:
    """构造 ``ChapterCardService``(P5-B-06)。

    依赖 ``WorkspaceService``(校验工作区 + 读 outline 文件根)+
    ``ChapterService``(复用 ``save_chapter_card`` 原子写 + 路径辅助);
    LLM 客户端通过 ``make_llm_client(...)`` 装(post-wave5 H1 routing
    completion fixup 起走 ``_resolve_bootstrap_target()``,bootstrap client
    跟引擎默认 profile 字面派发);router_factory 走 ``_make_router_factory()``
    同 ``OutlineService`` / ``ChapterService`` 模式(P3-B-06)。

    ``session_factory`` / ``ws_manager`` / ``card_config`` 模块级共享。

    测试通过 ``app.dependency_overrides[get_chapter_card_service] = lambda: ...``
    注入 fake LLM + tmp_path workspaces 版本。
    """
    client, model = _resolve_bootstrap_client()
    return ChapterCardService(
        workspace_service=workspace_service,
        chapter_service=chapter_service,
        session_factory=_make_session_factory(workspace_service.workspaces_root),
        client=client,
        model=model,
        router_factory=_make_router_factory(workspace_service.workspaces_root),
        vector_store_factory=_make_vector_store_factory(
            workspace_service.workspaces_root
        ),
        ws_manager=_ws_manager_singleton,
        card_config=_CARD_GENERATOR_CONFIG,
    )


def get_chapter_summary_service(
    workspace_service: WorkspaceService = Depends(get_workspace_service),
) -> ChapterSummaryService:
    """构造 ``ChapterSummaryService``(v1.0.x wave 4 session 5)。

    给前端 OutlinePage 第 6 tab 用 — GET 列表 + PUT 单条编辑。LLM 抽取在
    ``ChapterService`` / ``ChapterRewindService`` 的 hook 内自动跑,本 service
    不调 LLM,只暴露用户事后编辑入口。

    测试通过 ``app.dependency_overrides[get_chapter_summary_service] = lambda: ...``
    注入 tmp_path workspaces 版本。
    """
    return ChapterSummaryService(
        workspace_service=workspace_service,
        session_factory=_make_session_factory(workspace_service.workspaces_root),
    )


def get_backup_service(
    workspace_service: WorkspaceService = Depends(get_workspace_service),
) -> BackupService:
    """构造 ``BackupService``(P6-B-07)。

    依赖 ``WorkspaceService``(校验 workspace 存在)+ ``ws_manager`` 单例
    (publish ``backup_created`` / ``restore_completed``)。测试通过
    ``app.dependency_overrides[get_backup_service] = lambda: ...`` 注入带
    fake ws_manager 的版本。
    """
    from src.api import task_registry

    return BackupService(
        workspace_service=workspace_service,
        ws_manager=_ws_manager_singleton,
        active_task_checker=task_registry.has_active_tasks,
        restart_required_marker=lambda workspace_id: (
            mark_runtime_workspace_restart_required(
                workspace_service.workspaces_root,
                workspace_id,
            )
        ),
    )


def get_workspace_version_service(
    workspace_service: WorkspaceService = Depends(get_workspace_service),
) -> WorkspaceVersionService:
    """构造 ``WorkspaceVersionService``(P6-B-08)。

    依赖 ``WorkspaceService``(校验工作区 + 读 meta.json)+ ``BackupService``
    (pre-migration backup,对齐 plan §P6-B-08 字面"调 BackupService.create_
    manual_backup 作 pre-migration 备份")+ ``ws_manager`` 单例(publish
    ``version_migrated`` 到 ``workspace.{workspace_id}.events`` 频道)。

    测试通过 ``app.dependency_overrides[get_workspace_version_service] = ...``
    注入带 fake BackupService + fake ws_manager 的版本。
    """
    from src.api import task_registry

    backup_service = BackupService(
        workspace_service=workspace_service,
        ws_manager=_ws_manager_singleton,
        active_task_checker=task_registry.has_active_tasks,
        restart_required_marker=lambda workspace_id: (
            mark_runtime_workspace_restart_required(
                workspace_service.workspaces_root,
                workspace_id,
            )
        ),
    )
    return WorkspaceVersionService(
        workspace_service=workspace_service,
        backup_service=backup_service,
        ws_manager=_ws_manager_singleton,
        active_task_checker=task_registry.has_active_tasks,
        restart_required_marker=lambda workspace_id: (
            mark_runtime_workspace_restart_required(
                workspace_service.workspaces_root,
                workspace_id,
            )
        ),
    )


def get_chapter_rewind_service(
    workspace_service: WorkspaceService = Depends(get_workspace_service),
) -> ChapterRewindService:
    """构造 ``ChapterRewindService``(P6-B-01;v2 w1-s2 扩 reset_stale)。

    依赖 ``WorkspaceService``(校验 workspace + ``ensure_workspace_orm`` 兜底
    FK)+ ``session_factory``(写 ChapterORM / CharacterORM / PlotThreadORM /
    TimelineEntryORM)+ ``vector_store_factory``(Chroma soft-delete,生产路径
    走 Qwen3-Embedding 单例;失败 swallow + structlog)+ ``ws_manager``
    单例(WS publish ``chapter_rewound`` / ``chapter_invalidated`` /
    ``chapter_unblocked`` / ``chapter_force_passed`` / ``chapter_stale_reset``)
    + ``active_task_checker``(v2 w1-s2 reset_stale 对账 task_registry 防
    误杀,api 层组装时注入打破 services → api 反向依赖)。

    测试通过 ``app.dependency_overrides[get_chapter_rewind_service] = ...``
    注入带 in-memory engine + fake VectorStore 的版本。
    """
    from src.api import task_registry

    session_factory = _make_session_factory(workspace_service.workspaces_root)
    return ChapterRewindService(
        workspace_service=workspace_service,
        session_factory=session_factory,
        vector_store_factory=_make_vector_store_factory(
            workspace_service.workspaces_root
        ),
        ws_manager=_ws_manager_singleton,
        router_factory=_make_router_factory(workspace_service.workspaces_root),
        active_task_checker=task_registry.has_active_tasks,
        operation_lease_store=ChapterOperationLeaseStore(session_factory),
    )


_PIPELINE_SERVICE_SINGLETON: PipelineService | None = None


def get_pipeline_service(
    workspace_service: WorkspaceService = Depends(get_workspace_service),
    chapter_service: ChapterService = Depends(get_chapter_service),
    chapter_card_service: ChapterCardService = Depends(get_chapter_card_service),
) -> PipelineService:
    """构造 / 复用 ``PipelineService`` 模块级单例(P6-B-02)。

    pipeline state 是**跨请求共享内存状态**(active run snapshot 字典),
    必须用单例(对齐 ``ws_manager`` 同模式 — 不每次 Depends 重新构造,否则
    ``:start`` 后立即 ``GET /status`` 拿到空 state)。

    依赖 ``ChapterService``(execute 循环里调 ``write``)+ ``WorkspaceService``
    (校验 + outline VolumeORM 读 session)+ ``session_factory`` /
    ``ws_manager`` 模块级共享。**首次 Depends 时构造单例**,后续复用;测试
    通过 ``app.dependency_overrides[get_pipeline_service] = lambda: ...``
    覆盖整个服务,跳过单例。

    **P6-B-03 接力**:state 持久化迁到 SQLite ``pipeline_runs`` 表后,
    PipelineService 改读 ORM,单例约束放松(可每次重新构造,DB 是真相源)。
    """
    global _PIPELINE_SERVICE_SINGLETON
    if _PIPELINE_SERVICE_SINGLETON is None:
        _PIPELINE_SERVICE_SINGLETON = PipelineService(
            chapter_service=chapter_service,
            workspace_service=workspace_service,
            session_factory=_make_session_factory(workspace_service.workspaces_root),
            ws_manager=_ws_manager_singleton,
            # v2 w1-s1 卡生成入循环:execute 循环写前卡保障(缺卡生成 +
            # draft 自动 approve)。card_service 自身无状态(每方法开
            # session),单例持有首个构造实例无害。
            chapter_card_service=chapter_card_service,
            # v2 w1-s3 Run 级成本预检:与 ChapterService 同源注入同一
            # _COST_GUARD_CONFIG 单例(None=未启用时两层 gate 一致 noop;
            # 工作区级覆盖由 get_workspace_cost_guard_config 在调用点合并)。
            # watchdog_config 不注入 → service 内部读 config.yaml
            # ``pipeline:`` 段(缺段走默认 2400s),生产路径自动启用。
            cost_guard_config=_COST_GUARD_CONFIG,
        )
    return _PIPELINE_SERVICE_SINGLETON


def build_pipeline_components_for_lifespan() -> tuple[PipelineService, list[str]]:
    """P6-B-03 FastAPI lifespan 启动 hook 专用 builder。

    返 ``(PipelineService, workspace_ids)`` 元组,**与 ``Depends`` 解依赖
    无关**:lifespan 在 app 启动早期触发,没有请求上下文,不能走 ``Depends``
    链路;本函数手工构造一份 service + 枚举工作区列表给 lifespan 调
    ``mark_running_as_interrupted_on_startup``(v2 w1-s2 改判,原
    ``mark_running_as_error_on_startup``)。

    构造 chapter_service 时只装一个确定性拒绝调用的本地占位 client；不解析
    provider profile、不读取密钥，也不构造 router。execute 循环不在 lifespan
    里跑，chapter_service 仅作恢复方法的类型依赖。**workspace_service 是真实
    例**，扫工作区目录 + 读 meta.json 拿 ID 列表。
    """
    workspace_service = WorkspaceService()
    session_factory = _make_existing_session_factory(workspace_service.workspaces_root)
    chapter_service = ChapterService(
        workspace_service=workspace_service,
        client=_LifespanNoProviderClient(),
        model="lifespan-no-provider",
        session_factory=session_factory,
        ws_manager=_ws_manager_singleton,
        operation_lease_store=ChapterOperationLeaseStore(session_factory),
    )
    service = PipelineService(
        chapter_service=chapter_service,
        workspace_service=workspace_service,
        session_factory=session_factory,
        ws_manager=_ws_manager_singleton,
    )
    workspace_ids = [ws.workspace_id for ws in workspace_service.list()]
    return service, workspace_ids


def build_rewind_components_for_lifespan() -> tuple[ChapterRewindService, list[str]]:
    """v2 w1-s2 FastAPI lifespan 启动 hook 专用 builder(中断章自愈)。

    返 ``(ChapterRewindService, workspace_ids)`` 元组给 lifespan 调
    ``reset_stale``(对齐 ``build_pipeline_components_for_lifespan`` 同
    模式 — lifespan 无请求上下文不能走 ``Depends``)。

    与请求路径 ``get_chapter_rewind_service`` 同配方:``active_task_checker``
    注入 ``task_registry.has_active_tasks``(启动场景下 task 启动扫描已把
    僵尸 pending/running 翻 interrupted,对账必为 False 不挡自愈;留着注入
    是为对账语义一致 + 防 lifespan 排序回退);``vector_store_factory`` 懒
    构造,reset_stale 不触 Chroma 零开销。
    """
    from src.api import task_registry

    workspace_service = WorkspaceService()
    session_factory = _make_existing_session_factory(workspace_service.workspaces_root)
    service = ChapterRewindService(
        workspace_service=workspace_service,
        session_factory=session_factory,
        vector_store_factory=_make_vector_store_factory(
            workspace_service.workspaces_root
        ),
        vector_count_auditor=lambda workspace_id, chapter_id: (
            inspect_persisted_chapter_vectors_read_only(
                workspace_id=workspace_id,
                persist_dir=WorkspacePaths(
                    workspace_id,
                    workspaces_root=workspace_service.workspaces_root,
                ).chroma_dir,
                collection="memories",
                chapter_id=chapter_id,
            )
        ),
        ws_manager=_ws_manager_singleton,
        active_task_checker=task_registry.has_active_tasks,
        operation_lease_store=ChapterOperationLeaseStore(session_factory),
    )
    workspace_ids = [ws.workspace_id for ws in workspace_service.list()]
    return service, workspace_ids


def get_export_service(
    workspace_service: WorkspaceService = Depends(get_workspace_service),
) -> ExportService:
    """构造 ``ExportService``(P6-B-04)。

    依赖 ``WorkspaceService``(校验 + 读 ``meta.json`` 作者 / 创作时间)+
    ``session_factory``(读 VolumeORM / ChapterORM / ChapterCardORM,scope
    解析 + invalidated 跳过 + 章节标题查表)+ ``ws_manager`` 单例(WS publish
    ``task.{task_id}.progress`` 4 stage)+ ``engine_version``(写入 zip 内
    ``meta.json``)。

    测试通过 ``app.dependency_overrides[get_export_service] = lambda: ...``
    注入带 tmp_path workspaces + in-memory engine session 工厂的版本。
    """
    from src.version import ENGINE_VERSION

    return ExportService(
        workspace_service=workspace_service,
        session_factory=_make_session_factory(workspace_service.workspaces_root),
        ws_manager=_ws_manager_singleton,
        engine_version=ENGINE_VERSION,
    )


def get_volume_service(
    workspace_service: WorkspaceService = Depends(get_workspace_service),
) -> VolumeService:
    """构造 ``VolumeService``(P5-B-09)。

    依赖 ``WorkspaceService``(校验工作区存在 + 读 ``WorkspaceORM.outline_status``)
    + ``session_factory``(读 / 写 ``VolumeORM`` × N + 读 ``WorkspaceORM``);
    无 LLM client / WS manager(纯 CRUD + 字典查表)。

    测试通过 ``app.dependency_overrides[get_volume_service] = lambda: ...``
    注入带 tmp_path workspaces + in-memory engine session 工厂的版本。
    """
    return VolumeService(
        workspace_service=workspace_service,
        session_factory=_make_session_factory(workspace_service.workspaces_root),
    )


def get_validator_report_service(
    workspace_service: WorkspaceService = Depends(get_workspace_service),
    chapter_service: ChapterService = Depends(get_chapter_service),
) -> ValidatorReportService:
    """构造 ``ValidatorReportService``(P6-B-11,类 C 顺延 P6-F-01)。

    依赖 ``WorkspaceService``(校验 + workspaces_root 路径根)+ ``ChapterService``
    (复用 ``read_chapter_card`` + 既有 ``ChapterServiceError`` 翻译路径)+
    ``session_factory``(读 ``CharacterORM`` / ``TimelineEntryORM``,过滤
    ``revoked_at IS NULL`` 对齐 P6-B-01 soft-delete 联动)。

    无 LLM client / WS manager(纯查询服务,无 LLM 调用,无副作用)。

    测试通过 ``app.dependency_overrides[get_validator_report_service] = lambda: ...``
    注入带 tmp_path workspaces + in-memory engine session 工厂的版本。
    """
    return ValidatorReportService(
        workspace_service=workspace_service,
        chapter_service=chapter_service,
        session_factory=_make_session_factory(workspace_service.workspaces_root),
    )


def get_style_audit_service(
    workspace_service: WorkspaceService = Depends(get_workspace_service),
) -> StyleAuditService:
    """构造 ``StyleAuditService``,session 工厂按工作区目录开 SQLite。

    v1.1 wave 4 s5 / I2.2:StyleAuditor 落地为真 LLM agent,注入
    ``router_factory``(``_make_router_factory()``,与 ``ChapterService`` 同源)
    供按工作区构造 ``ModelRouter`` 路由到 ``style_auditor`` profile。无模板 /
    无可审章节时 agent 短路返 ok(不调 LLM);LLM / setup 失败 fail-open 返 ok。

    测试通过 ``app.dependency_overrides[get_style_audit_service] = lambda: ...``
    注入带 tmp_path session 的 service。
    """

    return StyleAuditService(
        workspace_service=workspace_service,
        session_factory=_make_session_factory(workspace_service.workspaces_root),
        router_factory=_make_router_factory(workspace_service.workspaces_root),
    )


def get_style_template_service() -> StyleTemplateService:
    """构造 ``StyleTemplateService``(I1)。

    无状态、不绑工作区、不碰 DB(只读引擎级 ``prompts/style_guide_templates/``)。
    测试可 ``app.dependency_overrides`` 注入带 tmp prompts_dir 的实例。
    """

    return StyleTemplateService()


def get_workspace_id(
    x_workspace_id: str | None = Header(default=None, alias="X-Workspace-Id"),
) -> str:
    """解析 ``X-Workspace-Id`` header。

    P1-B-19 决策 1:章节路由的工作区上下文走 header 显式传(不嵌入 URL,
    保留 spec §4.1 "顶级 chapter" 字面契约;不读 ``_active`` 软链避免隐式
    上下文混淆)。

    缺失或空字符串 → ``MissingWorkspaceHeader (422)``。
    """

    if not x_workspace_id:
        raise MissingWorkspaceHeader()
    return x_workspace_id
