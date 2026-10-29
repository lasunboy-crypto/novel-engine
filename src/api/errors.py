"""API 错误体系与全局异常处理(P1-B-17)。

对齐 API_CONTRACT §3.2 / §3.3 / §11(实施要点第 6 条)。

设计原则
========

- **集中翻译**:路由层不写 ``try/except``,直接抛 ``ApiError`` 子类或让下层
  ``ServiceError`` / ``MetaFileError`` 等自然冒泡;由本模块统一翻译。
- **错误码命名**:``<DOMAIN>_<REASON>`` 大写下划线(API_CONTRACT §3.3)。
- **HTTP 状态映射**:见 ``ApiError`` 子类 ``http_status`` 类属性。
- **兜底翻译走中间件**:``Exception`` 基类 handler 不能注册到 FastAPI/starlette
  ExceptionMiddleware(框架强制把 ``Exception`` 与 ``500`` 状态码绑给
  ``ServerErrorMiddleware``,后者**始终** raise 给 ASGI 服务器即使 response 已发,
  TestClient / 上游网关都会捕到 traceback)。改用 ``ApiErrorTranslationMiddleware``
  在 user middleware 最内层 try/except + 直接 send response,绕过
  ``ServerErrorMiddleware``。
- **未知异常不暴露 traceback**:仅返回 ``INTERNAL_ERROR`` 通用消息;完整
  traceback 由 structlog ``api_unhandled_exception`` 事件落盘供运维诊断
  (维护人补充要求)。

P1 子类覆盖范围
==============

只覆盖 P1-B-18 / P1-B-19 路由会用到的域:工作区、章节、章节卡、版本、参数
冲突。P2+ 引入 retrieval / pipeline / cost guard 时再加,**不预埋**。
"""

from __future__ import annotations

import json
from typing import Any

import structlog
from fastapi import FastAPI, Request
from fastapi.exceptions import RequestValidationError
from fastapi.responses import JSONResponse
from starlette.types import ASGIApp, Receive, Scope, Send

from starlette.exceptions import HTTPException as StarletteHTTPException

from src.api.schemas.common import ErrorEnvelope, ErrorPayload
from src.core.agents.chapter_card_generator import ChapterCardGeneratorError
from src.core.agents.memory_health_checker import MemoryHealthCheckerError
from src.core.agents.reference_validator import ReferenceValidationFailedError
from src.core.chapter_versioning import ChapterVersioningError
from src.core.orchestrator.chapter_orchestrator import RewriteCheckpointRecoveryError
from src.core.workspace.meta import MetaFileError
from src.services.asset_import import AssetImportError
from src.services.attempt_ledger import ProviderCallCapExceeded
from src.services.character_service import (
    CharacterAlreadyExistsError,
    CharacterNotFoundError,
)
from src.services.chapter_card_service import (
    CardContinuityBridgeRequiredError,
    CardContinuityLedgerStaleError,
    CardInvalidStateTransitionError,
    CardNotFoundError,
)
from src.services.chapter_operation_lease import ChapterOperationLeaseError
from src.services.chapter_rewind_service import (
    ChapterInvalidatedNeedsUnblockError,
    ChapterRewindInvalidStateError,
    ChapterRewindServiceError,
    ChapterUnblockInvalidStateError,
    ForcePassContinuityLedgerNotReadyError,
)
from src.services.chapter_service import (
    ChapterCardContinuityBridgeError,
    ChapterCardContinuityStaleError,
    ChapterCardSettingStaleError,
    ChapterReReviewInvalidStateError,
    ChapterReReviewNoVersionError,
    ChapterServiceError,
    ContinuityLedgerNotReadyError,
)
from src.services.core_state_service import CoreStateServiceError
from src.services.chapter_summary_service import (
    ChapterSummaryNotFoundError,
    ChapterSummaryServiceError,
)
from src.services.backup_service import (
    BackupInvalidFormatError,
    BackupNotFoundError,
    BackupServiceError,
    RestoreVersionIncompatibleError,
)
from src.core.agents.outline_builder import OutlineBuilderError
from src.services.outline_service import (
    OutlineGateViolation,
    OutlineLengthCalibrationRequired as OutlineLengthCalibrationRequiredError,
    OutlineNotFound as OutlineNotFoundServiceError,
)
from src.services.export_service import (
    ExportFormatUnsupportedError,
    ExportNotFoundError,
    ExportScopeInvalidError,
    ExportServiceError,
)
from src.services.pipeline_service import (
    PipelineAlreadyRunningError,
    PipelineInvalidScopeError,
    PipelineNotRunningError,
    PipelineServiceError,
)
from src.services.profile_override import (
    ProfileNotFoundError,
    WriterProfileNotFoundError,
)
from src.services.post_v8_abc_selection_activity import SelectionActivityError
from src.services.style_audit_service import StyleAuditServiceError
from src.services.thread_service import (
    PlotThreadAlreadyExistsError,
    PlotThreadNotFoundError,
)
from src.services.timeline_service import (
    TimelineEntryAlreadyExistsError,
    TimelineEntryNotFoundError,
)
from src.services.volume_service import (
    VolumeNotFoundError,
    VolumeUpdateInvalidError,
)
from src.services.validator_report_service import (
    ValidatorReportNotFoundError,
)
from src.services.workspace_service import (
    WorkspaceDestroyConfirmInvalidError,
    WorkspaceServiceError,
)
from src.services.workspace_version_service import (
    WorkspaceMigrationBackupFailedError,
    WorkspaceMigrationConfirmInvalidError,
    WorkspaceVersionIncompatibleError,
    WorkspaceVersionServiceError,
)

# 注:Reviewer 解析失败(``ReviewerParseError``)不直接走 API 异常体系——它在
# Orchestrator 内部被 retry / 升人工,API 路由不会拿到裸的 parse error。

_log = structlog.get_logger(__name__)


# =============================================================================
# ApiError 基类与子类
# =============================================================================


class ApiError(Exception):
    """API 层错误基类。所有 P1 域子类继承自此。

    - ``code`` / ``http_status`` 类属性由子类覆盖。
    - ``message`` 实例属性允许在 raise 处自定义;不传则用类属性 ``default_message``。
    - ``details`` 可选,字段级错误等结构化信息。
    """

    code: str = "INTERNAL_ERROR"
    http_status: int = 500
    default_message: str = "服务器内部错误"

    def __init__(
        self,
        message: str | None = None,
        *,
        details: dict[str, Any] | None = None,
    ) -> None:
        self.message = message if message is not None else self.default_message
        self.details = details
        super().__init__(self.message)


# ---- 工作区域 ---------------------------------------------------------------


class WorkspaceNotFound(ApiError):
    code = "WORKSPACE_NOT_FOUND"
    http_status = 404
    default_message = "工作区不存在"


class WorkspaceAlreadyExists(ApiError):
    code = "WORKSPACE_ALREADY_EXISTS"
    http_status = 409
    default_message = "工作区已存在"


class WorkspaceInvalid(ApiError):
    """``meta.json`` 缺失 / 解析失败 / 字段不合法时抛出。"""

    code = "WORKSPACE_INVALID"
    http_status = 422
    default_message = "工作区元数据不合法"


class WorkspaceConflict(ApiError):
    """通用工作区状态冲突(归档已存在、目录已占用等)。"""

    code = "WORKSPACE_CONFLICT"
    http_status = 409
    default_message = "工作区状态冲突"


class WorkspaceAlreadyArchived(ApiError):
    """工作区已处于 archived 状态,不允许重复归档。

    路由层前置守卫:调 ``WorkspaceService.archive`` 前先 ``info()`` 看 ``status``,
    已是 ``archived`` 则直接抛(避免覆盖已有归档,语义比通用 conflict 精确)。
    """

    code = "WORKSPACE_ALREADY_ARCHIVED"
    http_status = 409
    default_message = "工作区已归档,无需重复归档"


class ArchiveNotFound(ApiError):
    """``:restore`` 时项目级 ``archives/<id>.tar.gz`` 不存在。

    路由层前置守卫:调 ``WorkspaceService.restore`` 前先 ``Path.exists()`` 检查,
    缺失直接抛 404(资源存在性检查,语义比通用 conflict 精确)。
    """

    code = "ARCHIVE_NOT_FOUND"
    http_status = 404
    default_message = "归档文件不存在"


class PurgeConfirmMismatch(ApiError):
    """``:purge`` 二次确认字段与 ``workspace_id`` 不匹配。

    路由层前置参数校验:``body.confirm == workspace_id`` 不匹配直接抛
    (purge 不可逆,错误信息越精确越能避免误删)。
    """

    code = "PURGE_CONFIRM_MISMATCH"
    http_status = 422
    default_message = "二次确认字段不匹配"


class WorkspaceDestroyConfirmInvalid(ApiError):
    """``:destroy`` 二次确认字段与 ``workspace_id`` 不匹配(P7-B-07)。

    路由层前置参数校验:``body.confirm == workspace_id`` 不匹配直接抛
    (destroy 三方全删完全不可逆,strict 二次确认对齐 ``PurgeConfirmMismatch``
    / ``RestoreConfirmMismatch`` / ``WorkspaceMigrationConfirmInvalid`` 同模式)。
    ``details`` 含 ``workspace_id`` / ``got``(用户传入的 confirm 字面值)。
    """

    code = "WORKSPACE_DESTROY_CONFIRM_INVALID"
    http_status = 422
    default_message = "destroy 二次确认字段不匹配"


# ---- 备份 / 恢复域(P6-B-07) ----------------------------------------------


class PurgeBackupsConfirmMismatch(ApiError):
    """``:purge-backups`` 二次确认字段与 ``workspace_id`` 不匹配(v1.1 w3-s8 F1)。

    路由层前置参数校验:``body.confirm == workspace_id`` 不匹配直接抛
    (清空全部备份破坏性,strict 二次确认对齐 ``PurgeConfirmMismatch`` /
    ``RestoreConfirmMismatch`` 同模式)。``details`` 含 ``workspace_id``。
    """

    code = "PURGE_BACKUPS_CONFIRM_MISMATCH"
    http_status = 422
    default_message = "purge-backups 二次确认字段不匹配"


class BackupNotFound(ApiError):
    """``POST /workspaces/{id}:restore`` 指定 ``backup_id`` 对应 zip 不存在。

    路由层先调 ``BackupService.restore_from_backup`` 触发本异常,翻译表把
    ``BackupNotFoundError`` 翻成本错误码。``details`` 含 ``workspace_id`` /
    ``backup_id`` 供前端 ExportPage 列表回放或重新选择备份。
    """

    code = "BACKUP_NOT_FOUND"
    http_status = 404
    default_message = "备份不存在"


class RestoreVersionIncompatible(ApiError):
    """``:restore`` 时 zip 内 schema_version 与当前 Alembic head 不一致。

    422 ``UNPROCESSABLE_ENTITY``:
    - ``reason="backup_newer"``:备份是更新版引擎产出 → 升级引擎再 restore
    - ``reason="backup_older"``:备份是更旧引擎产出 → 需走 P6-B-08 migrate
      流程(``needs_migration=true`` marker,本任务 v1 阶段返 422 + marker)

    ``details`` 含 ``workspace_id`` / ``backup_id`` / ``backup_schema_version``
    / ``current_schema_version`` / ``reason`` / ``needs_migration`` /
    ``needs_engine_upgrade`` 供前端展示明确指引。
    """

    code = "RESTORE_VERSION_INCOMPATIBLE"
    http_status = 422
    default_message = "备份版本与当前引擎不兼容"


class BackupInvalidFormat(ApiError):
    """``:restore`` 时 zip 结构损坏(``backup_meta.json`` 缺失 / 不合法 /
    字段缺失 / zip 文件本身 BadZipFile)。

    422 ``UNPROCESSABLE_ENTITY``:错在备份文件本身(用户外部修改 / 复制时
    截断 / 磁盘异常),非引擎缺陷。``details`` 含 ``workspace_id`` /
    ``backup_id`` / ``reason``。
    """

    code = "BACKUP_INVALID_FORMAT"
    http_status = 422
    default_message = "备份文件格式损坏"


class RestoreConfirmMismatch(ApiError):
    """``:restore`` 二次确认字段与 ``workspace_id`` 不匹配。

    路由层前置校验,对齐 ``PurgeConfirmMismatch`` 同模式 — restore 是覆盖性
    操作(原工作区数据被替换),strict 二次确认防止误恢复。``details`` 含
    ``workspace_id``。

    plan §P6-B-07 字面列 3 错误码(``BACKUP_NOT_FOUND`` /
    ``RESTORE_VERSION_INCOMPATIBLE`` / ``BACKUP_INVALID_FORMAT``)未列 confirm
    错码,本错误码是 Code 工程自决补 plan 漏审(类 C 处置 — impl 文档登记)。
    """

    code = "RESTORE_CONFIRM_MISMATCH"
    http_status = 422
    default_message = "二次确认字段不匹配"


# ---- 数据迁移域(P6-B-08) -------------------------------------------------


class WorkspaceVersionIncompatible(ApiError):
    """``POST /workspaces/{id}:migrate`` 时 workspace engine 主版本与当前
    引擎主版本跨级,引擎拒绝自动迁移(对齐 plan §P6-B-08 字面)。

    422 ``UNPROCESSABLE_ENTITY``:语义"跨大版本破坏性升级,需专门 v2 迁移
    工具"。``details`` 含 ``workspace_id`` / ``workspace_engine_version`` /
    ``current_engine_version``,前端可据此提示用户停留在旧引擎或升级到匹配
    版本(v1 期 ENGINE_VERSION ``0.x.y`` 同主版本,理论不触发;为 v2 (1.0.0)
    预留 gate)。
    """

    code = "WORKSPACE_VERSION_INCOMPATIBLE"
    http_status = 422
    default_message = "workspace 主版本与当前引擎跨级,需专门 v2 迁移工具"


class WorkspaceMigrationConfirmInvalid(ApiError):
    """``POST /workspaces/{id}:migrate`` 二次确认字段不匹配。

    路由层前置参数校验(对齐 ``PurgeConfirmMismatch`` / ``RestoreConfirmMismatch``
    同模式 — migrate 是覆盖性操作)。``details`` 含 ``workspace_id``。
    """

    code = "WORKSPACE_MIGRATION_CONFIRM_INVALID"
    http_status = 422
    default_message = "二次确认字段不匹配"


class WorkspaceMigrationBackupFailed(ApiError):
    """``POST /workspaces/{id}:migrate`` pre-migration 备份失败,迁移未开始。

    500 ``INTERNAL_SERVER_ERROR``:语义"备份失败 → migrate 不开始"(保证
    rollback 后路,对齐 plan §P6-B-08 字面)。``details`` 含 ``workspace_id``
    / ``reason``(underlying error type / message)。前端可提示用户检查磁盘
    空间 / 工作区目录权限后重试。
    """

    code = "WORKSPACE_MIGRATION_BACKUP_FAILED"
    http_status = 500
    default_message = "pre-migration 备份失败,迁移未开始"


# ---- 章节域 -----------------------------------------------------------------


class ChapterNotFound(ApiError):
    code = "CHAPTER_NOT_FOUND"
    http_status = 404
    default_message = "章节不存在"


class ChapterCardNotFound(ApiError):
    """章节卡文件 ``data/cards/<chapter_id>.json`` 不存在。

    路由前置守卫(P1-B-19):``:write`` / ``:rewrite`` / ``:revise`` /
    ``GET /card`` 调 service 前先 ``card_path_for(...).is_file()`` 检查,
    缺失抛 404。语义比"卡内容不合法"(``CHAPTER_CARD_INVALID``)精确——
    存在性 vs 合法性双轨。
    """

    code = "CHAPTER_CARD_NOT_FOUND"
    http_status = 404
    default_message = "章节卡不存在"


class ChapterCardInvalid(ApiError):
    """章节卡 JSON 不合法或字段校验失败。"""

    code = "CHAPTER_CARD_INVALID"
    http_status = 422
    default_message = "章节卡不合法"


class ChapterCardSettingConflict(ApiError):
    """章节卡与权威世界规则存在写前 HARD 冲突。"""

    code = "CHAPTER_CARD_SETTING_CONFLICT"
    http_status = 422
    default_message = "章节卡与权威世界规则冲突"


class ChapterCardContractConflict(ApiError):
    """章节卡 must_achieve 与 forbidden 直接互斥。"""

    code = "CHAPTER_CARD_CONTRACT_CONFLICT"
    http_status = 422
    default_message = "章节卡必达与禁止合同直接冲突"


class ChapterCardSettingStale(ApiError):
    """Writer 前发现章节卡绑定的世界合同 hash 已陈旧。"""

    code = "CHAPTER_CARD_SETTING_STALE"
    http_status = 409
    default_message = "章节卡世界合同已陈旧，必须重新生成"


class ContinuityLedgerNotReady(ApiError):
    """approved 后同步小账本未达到下一章硬水位。"""

    code = "CONTINUITY_LEDGER_NOT_READY"
    http_status = 409
    default_message = "连续性小账本未就绪，已暂停后续章节"


class ChapterCardContinuityStale(ApiError):
    """章节卡冻结的上一章 ledger provenance 已陈旧。"""

    code = "CHAPTER_CARD_CONTINUITY_STALE"
    http_status = 409
    default_message = "章节卡连续性来源已陈旧，必须重新生成"


class ChapterCardContinuityBridgeRequired(ApiError):
    """即时未完动作遇到跳时/换景但卡片没有明确承接。"""

    code = "CHAPTER_CARD_CONTINUITY_BRIDGE_REQUIRED"
    http_status = 422
    default_message = "章节卡缺少跨章承接说明"


class ChapterSummaryNotFound(ApiError):
    """章节摘要(``chapter_summaries`` 表)行不存在。

    v1.0.x wave 4 session 5 引入:用户编辑摘要的 PUT 端点目标行未在 LLM 抽取
    后落库(章节未 approved / 抽取 fail-open 返 None / chapter_id 错)→ 404。
    """

    code = "CHAPTER_SUMMARY_NOT_FOUND"
    http_status = 404
    default_message = "章节摘要不存在"


class ChapterIdMismatch(ApiError):
    """``PUT /chapters/{id}/card`` 请求体内 ``chapter_id`` 与 URL 不匹配。

    路由前置参数校验:``body.chapter_id == path.chapter_id`` 不一致直接抛
    (避免把 B 的章节卡误写到 A 的路径)。类比 ``PurgeConfirmMismatch`` 模式。
    """

    code = "CHAPTER_ID_MISMATCH"
    http_status = 422
    default_message = "请求体内 chapter_id 与 URL 不匹配"


class ChapterInvalidState(ApiError):
    """章节当前状态不允许该操作(对应 PROJECT_SPEC §6 状态机非法转换)。"""

    code = "CHAPTER_INVALID_STATE"
    http_status = 409
    default_message = "章节状态不允许该操作"


class ChapterVersionConflict(ApiError):
    """章节版本号冲突(目标版本已存在 / 切换源不存在 等)。"""

    code = "CHAPTER_VERSION_CONFLICT"
    http_status = 409
    default_message = "章节版本冲突"


class ChapterRewindInvalidState(ApiError):
    """``POST /chapters/{id}:rewind`` 目标章节 status 不允许 rewind(P6-B-01)。

    422 ``UNPROCESSABLE_ENTITY``:不允许的源状态包括 ``drafting``(本就未写)
    与 ``invalidated``(上游已 rewind,need unblock)。``details`` 含
    ``chapter_id`` / ``current_status`` / ``allowed_sources`` 帮前端提示
    用户检查当前章节状态。
    """

    code = "CHAPTER_REWIND_INVALID_STATE"
    http_status = 422
    default_message = "章节状态不允许 rewind"


class ChapterInvalidatedNeedsUnblock(ApiError):
    """``chapter:write`` / ``:rewrite`` / ``:revise`` gate + ``:force-pass``
    端点检测到本章 ``status='invalidated'``(P6-B-01)。

    409 ``CONFLICT``:语义"上游 rewind 后本章 stale,需 user 先 ``:unblock``
    才能继续"。``details`` 含 ``chapter_id`` / ``workspace_id``,前端
    ChapterEditorPage / RunControlPage 可据此引导用户调 unblock 端点。
    """

    code = "CHAPTER_INVALIDATED_NEEDS_UNBLOCK"
    http_status = 409
    default_message = "章节 stale 需先 unblock 才能继续操作"


class ChapterUnblockInvalidState(ApiError):
    """``POST /chapters/{id}:unblock`` 目标章节 status 非 ``invalidated``
    (P6-B-01)。

    422 ``UNPROCESSABLE_ENTITY``:unblock 仅对 ``invalidated`` 源有意义,
    其他状态本就可写无需 unblock。``details`` 含 ``chapter_id`` /
    ``current_status``。
    """

    code = "CHAPTER_UNBLOCK_INVALID_STATE"
    http_status = 422
    default_message = "章节状态非 invalidated 不可 unblock"


class ChapterReReviewInvalidState(ApiError):
    """``POST /chapters/{id}:re-review`` 目标章节 status 非 ``human_intervention``
    (v2 W3.5 UI 缺口3)。

    422 ``UNPROCESSABLE_ENTITY``:重审(对人工改稿重跑写后审校)仅对孤儿章
    ``human_intervention`` 有意义。``details`` 含 ``chapter_id`` /
    ``current_status``,前端据此提示用户当前状态不可重审。
    """

    code = "CHAPTER_RE_REVIEW_INVALID_STATE"
    http_status = 422
    default_message = "章节状态非 human_intervention 不可重审"


class ChapterReReviewNoVersion(ApiError):
    """``POST /chapters/{id}:re-review`` 目标章节无任何版本可审(v2 W3.5 UI 缺口3)。

    422 ``UNPROCESSABLE_ENTITY``:重审需有人工改稿的版本;正常流程下孤儿章必有
    版本,此错误防御异常态 / fixture。``details`` 含 ``chapter_id``。
    """

    code = "CHAPTER_RE_REVIEW_NO_VERSION"
    http_status = 422
    default_message = "章节无版本可重审"


class ChapterInvalidProfile(ApiError):
    """``:write`` / ``:rewrite`` 请求体 ``profile_override`` 引用未声明 profile_id。

    P3-B-11 加(P3-F-03 类 C 顺延 + 启动审议自决项)。``ProfileNotFoundError``
    经 ``_translate_service_error`` 翻译为本错误码,前端可据此引导用户从
    WriterProfileSelector 下拉里重选合法 profile。
    """

    code = "CHAPTER_INVALID_PROFILE"
    http_status = 422
    default_message = "请求体 profile_override 引用未声明 profile"


class ReferenceValidationFailed(ApiError):
    """ChapterCard 4 字段引用 ID 在工作区 ``characters`` / ``plot_threads`` 表
    内无对应行(P6-B-10 引入,P5 候选 1 v1 内解决)。

    422 ``UNPROCESSABLE_ENTITY``:LLM 输出在 agent 跨字段校验后仍引用了工作区
    SQLite 表中不存在的 ID(自动重试 1 次后仍失败 / ``:retry-references``
    手动重试失败 / Writer 前置 gate 检测到持久化卡引用过期 ID)。``details`` 含:

    - ``workspace_id`` / ``chapter_id``
    - ``unknown_character_ids``:``related_characters`` / ``pov_character`` /
      ``continuity_context.pov_to`` 引用但表中无行的角色 ID 列表
    - ``unknown_thread_ids``:``related_plot_threads`` 引用但表中无行的伏笔
      ID 列表

    前端 ChapterCardReviewPage 可据此提示用户:① 编辑章节卡删除未知 ID;
    ② 调用 ``POST /chapters/{id}:retry-references`` 触发系统重试;③ 若卡内
    引用本应存在,检查 character / plot_thread CRUD 是否漏行。
    """

    code = "REFERENCE_VALIDATION_FAILED"
    http_status = 422
    default_message = "章节卡引用了工作区不存在的角色 / 伏笔 ID"


class WriterProfileNotFound(ApiError):
    """章节卡 ``force_profile`` 字段引用的 profile_id 未在 ``writer_profiles``
    config 声明(P6-B-09 引入,对齐 plan §字面)。

    与 :class:`ChapterInvalidProfile` 区分:后者是单次 request body
    ``profile_override`` 越界(transient,前端引导改下拉重发);本错误码是
    章节卡持久化字段越界(deployment 配置漂移 — Pydantic Literal 允许的 4 档
    profile 之一在当前 WriterRouter 未注册),用户必须先编辑章节卡
    ``force_profile`` 字段才能再次 ``:write`` / ``:rewrite``。

    details 含 ``profile_id`` + ``known_profiles``,前端 ChapterCardReviewPage
    可据此弹出"当前部署未配置此 profile,请改选 [list]"提示。
    """

    code = "WRITER_PROFILE_NOT_FOUND"
    http_status = 422
    default_message = "章节卡 force_profile 字段引用未声明 profile"


class ValidatorReportNotFound(ApiError):
    """章节无 ``final.md``,validator 报告无法生成(P6-B-11 引入,类 C 顺延 P6-F-01)。

    404 ``NOT_FOUND``:章节卡存在但 ``data/chapters/<id>/final.md`` 缺失 ——
    章节尚未 ``:write`` 出正文,无文本供 validator 校验。

    与 :class:`ChapterCardNotFound` 区分:后者是"章节卡本身缺失"(走 422
    ``CHAPTER_CARD_INVALID`` / 404 ``CHAPTER_CARD_NOT_FOUND``);本错误码是
    "卡有 + 正文没有"。前端 ``ChapterCardReviewPage`` "关键字段达成对比"块
    据此提示"先 :write 写出正文再来查看校验结果"。
    """

    code = "VALIDATOR_REPORT_NOT_FOUND"
    http_status = 404
    default_message = "章节正文不存在,无法生成 validator 报告"


# ---- 角色域(P2-B-14) ------------------------------------------------------


class CharacterNotFound(ApiError):
    """角色不存在或不属于当前工作区(跨工作区视为不存在)。"""

    code = "CHARACTER_NOT_FOUND"
    http_status = 404
    default_message = "角色不存在"


class CharacterAlreadyExists(ApiError):
    """``POST /characters`` 时 ``character_id`` 在 SQLite 中已存在。

    ``character_id`` 是 SQLite 主键,全局唯一;若已被其他工作区占用,本错
    误同样触发(避免覆盖他工作区数据)。
    """

    code = "CHARACTER_ALREADY_EXISTS"
    http_status = 409
    default_message = "角色已存在"


class CharacterIdMismatch(ApiError):
    """``PUT /characters/{id}`` 请求体内 ``character_id`` 与 URL 不一致。

    路由层前置参数校验,类比 ``ChapterIdMismatch`` 模式。
    """

    code = "CHARACTER_ID_MISMATCH"
    http_status = 422
    default_message = "请求体内 character_id 与 URL 不匹配"


class CharacterWorkspaceMismatch(ApiError):
    """``POST`` / ``PUT /characters`` 请求体内 ``workspace_id`` 与
    ``X-Workspace-Id`` header 不一致。

    路由层前置参数校验:防止用 A 工作区 header 写 B 工作区数据。
    """

    code = "CHARACTER_WORKSPACE_MISMATCH"
    http_status = 422
    default_message = "请求体内 workspace_id 与 X-Workspace-Id header 不匹配"


# ---- 伏笔域(P2-B-15) ------------------------------------------------------


class PlotThreadNotFound(ApiError):
    """伏笔不存在或不属于当前工作区(跨工作区视为不存在)。"""

    code = "PLOT_THREAD_NOT_FOUND"
    http_status = 404
    default_message = "伏笔不存在"


class PlotThreadAlreadyExists(ApiError):
    """``POST /threads`` 时 ``thread_id`` 在 SQLite 中已存在。

    ``thread_id`` 是 SQLite 主键,全局唯一;若已被其他工作区占用,本错
    误同样触发(避免覆盖他工作区数据)。
    """

    code = "PLOT_THREAD_ALREADY_EXISTS"
    http_status = 409
    default_message = "伏笔已存在"


class PlotThreadIdMismatch(ApiError):
    """``PUT /threads/{id}`` 请求体内 ``thread_id`` 与 URL 不一致。

    路由层前置参数校验,类比 ``ChapterIdMismatch`` / ``CharacterIdMismatch``
    模式。
    """

    code = "PLOT_THREAD_ID_MISMATCH"
    http_status = 422
    default_message = "请求体内 thread_id 与 URL 不匹配"


class PlotThreadWorkspaceMismatch(ApiError):
    """``POST`` / ``PUT /threads`` 请求体内 ``workspace_id`` 与
    ``X-Workspace-Id`` header 不一致。

    路由层前置参数校验:防止用 A 工作区 header 写 B 工作区数据。
    """

    code = "PLOT_THREAD_WORKSPACE_MISMATCH"
    http_status = 422
    default_message = "请求体内 workspace_id 与 X-Workspace-Id header 不匹配"


# ---- 时间线域(P2-B-16) ----------------------------------------------------


class TimelineEntryNotFound(ApiError):
    """时间线条目不存在或不属于当前工作区(跨工作区视为不存在)。"""

    code = "TIMELINE_ENTRY_NOT_FOUND"
    http_status = 404
    default_message = "时间线条目不存在"


class TimelineEntryAlreadyExists(ApiError):
    """``POST /timeline`` 时 ``entry_id`` 在 SQLite 中已存在。

    ``entry_id`` 是 SQLite 主键,全局唯一;若已被其他工作区占用,本错误
    同样触发(避免覆盖他工作区数据)。
    """

    code = "TIMELINE_ENTRY_ALREADY_EXISTS"
    http_status = 409
    default_message = "时间线条目已存在"


class TimelineEntryIdMismatch(ApiError):
    """``PUT /timeline/{id}`` 请求体内 ``entry_id`` 与 URL 不一致。

    路由层前置参数校验,类比 ``ChapterIdMismatch`` / ``CharacterIdMismatch``
    / ``PlotThreadIdMismatch`` 模式。
    """

    code = "TIMELINE_ENTRY_ID_MISMATCH"
    http_status = 422
    default_message = "请求体内 entry_id 与 URL 不匹配"


class TimelineEntryWorkspaceMismatch(ApiError):
    """``POST`` / ``PUT /timeline`` 请求体内 ``workspace_id`` 与
    ``X-Workspace-Id`` header 不一致。

    路由层前置参数校验:防止用 A 工作区 header 写 B 工作区数据。
    """

    code = "TIMELINE_ENTRY_WORKSPACE_MISMATCH"
    http_status = 422
    default_message = "请求体内 workspace_id 与 X-Workspace-Id header 不匹配"


# ---- 记忆健康度域(P4-B-09) ------------------------------------------------


class MemoryHealthCheckerFailed(ApiError):
    """``POST /workspaces/{id}/memory/check`` 上游 LLM 输出非合法 JSON / 字段
    不合 schema。

    502 而非 500:错在上游 LLM 服务,非引擎缺陷;前端可提示"模型输出异常,
    可重试一次"。``MemoryHealthCheckerError``(``src/core/agents/memory_health_checker.py``)
    经 ``_translate_service_error`` 翻译为本错误码,details 含 ``raw_output``
    片段与 ``model_used`` 便于诊断(retry 决策走人工)。
    """

    code = "MEMORY_HEALTH_CHECKER_FAILED"
    http_status = 502
    default_message = "记忆健康度审计 LLM 输出异常,请重试或检查 prompt"


# ---- 主流水线 gate(P4-acceptance) -----------------------------------------


class CostGuardTriggered(ApiError):
    """``ChapterService.write/rewrite/revise`` 入口前置 gate 检测到 cost_guard
    超限(对齐 OBSERVABILITY §3.4 ``cost_guard.action_on_exceed=pause``)。

    402 ``PAYMENT_REQUIRED``:语义贴近"成本预算耗尽,请充值/调整 limit",
    与"工作区状态冲突"(409)区分开;前端可针对性提示"今日成本已超
    ``$X`` 上限"。details 含 ``scope`` / ``current_usd`` / ``limit_usd`` /
    ``reason`` 供前端定位维度,与 structlog ``cost_guard_triggered``
    + WS ``system.alerts`` payload 字段同源。
    """

    code = "COST_GUARD_TRIGGERED"
    http_status = 402
    default_message = "成本守卫触发,流水线已暂停"


class ProviderCallCapReached(ApiError):
    """R5 持久化 provider 物理调用 hard cap 已耗尽。"""

    code = "PROVIDER_CALL_CAP_EXCEEDED"
    http_status = 409
    default_message = "provider 调用预算已耗尽，下一次网络调用已停止"


class MiniPlanSettingConflict(ApiError):
    """单卷 mini plan 与权威世界规则存在写前 HARD 冲突。"""

    code = "MINI_PLAN_SETTING_CONFLICT"
    http_status = 422
    default_message = "mini plan 与权威世界规则冲突"


class PlotSettingConflict(ApiError):
    """主线 plot 与权威世界规则存在写盘前 HARD 冲突。"""

    code = "PLOT_SETTING_CONFLICT"
    http_status = 422
    default_message = "主线 plot 与权威世界规则冲突"


class VolumePlansSettingConflict(ApiError):
    """卷规划与权威世界规则存在写盘前 HARD 冲突。"""

    code = "VOLUME_PLANS_SETTING_CONFLICT"
    http_status = 422
    default_message = "卷规划 volume_plans 与权威世界规则冲突"


class OutlineArcTooShort(ApiError):
    """``POST /outline:commit`` gate 校验失败:[HARD] 副本类剧情 ≥ 3 章
    违规(PROJECT_SPEC §4.1 字面 + p5_plan.md §8)。

    422 ``UNPROCESSABLE_ENTITY``:outline 数据结构本身合法(Pydantic 通过),
    但违反了"副本类弧段 ≥ 3 章"的业务硬约束。``details.violations`` 列违规
    arc 区间(``volume_id`` / ``arc_id`` / ``arc_type`` / ``chapter_range`` /
    ``span_length``),前端 NewBookPage / VolumePlanPage 可据此高亮违规
    arc 引导用户修复后重试 commit。

    与 ``MEMORY_HEALTH_BLOCKED``(409)/ ``COST_GUARD_TRIGGERED``(402)区分:
    本错误属于"用户输入校验失败"(422),非"运行时阻断"。
    """

    code = "OUTLINE_ARC_TOO_SHORT"
    http_status = 422
    default_message = "outline 校验失败:副本类剧情必须横跨 ≥ 3 章"


class OutlineVolumeSeamReplay(ApiError):
    """``POST /outline:commit`` gate 校验失败:[HARD] 卷界事件重演
    (v1.1 acceptance 修复1,2026-06-10)。

    422 ``UNPROCESSABLE_ENTITY``:新卷首章 mini_plan 重演了前卷末章**已完结**
    的事件 — 该章写作期 must_achieve 与已写正文 / 连续性记忆冲突,永不可达成,
    会撞 rewrite budget 死锁(luo-accept-v1 ch-0007 现场)。``details.violations``
    列违规卷界(``prev_chapter_id`` / ``next_chapter_id`` / ``confidence`` /
    ``evidence``),用户应在 OutlinePage regenerate-section mini_plan 重生后
    重试 commit。
    """

    code = "OUTLINE_VOLUME_SEAM_REPLAY"
    http_status = 422
    default_message = "outline 校验失败:新卷首章重演了前卷末章已完结的事件"


class OutlineConsistencyConflict(ApiError):
    """大纲生成期跨段一致性硬伤(v2 W3.5 UI 缺口1)。

    422 ``UNPROCESSABLE_ENTITY``:``outline_consistency_validator`` 判定 4 段
    (world/characters/plot/volume_plans)存在跨段名字/称谓矛盾或角色混淆
    (``OutlineBuilderError`` 带非空 ``contradictions``)。在 mini_plan 之前拦截,
    防一路污染下游出卡→正文。``details`` 含 ``stage`` / ``contradictions``
    (每条冲突文字)/ ``coverage_gaps`` / ``evidence``,前端 OutlinePage 列出每条
    冲突 + 依据并红字高亮涉及的实体名,用户「重新生成」对应段后重审。
    """

    code = "OUTLINE_CONSISTENCY_CONFLICT"
    http_status = 422
    default_message = "大纲跨段一致性校验失败:存在名字/称谓矛盾或角色混淆"


class OutlineBuildFailed(ApiError):
    """大纲某段 LLM 输出非法(JSON 解析 / schema 校验失败,重试耗尽)。

    422 ``UNPROCESSABLE_ENTITY``:``OutlineBuilderError``(无 contradictions 的
    通用失败)。原落通用 ``INTERNAL_ERROR``(v2 W3.5 UI 缺口1 前);现给结构化码 +
    ``details``(``stage`` / ``model_used``)让前端提示"哪段生成失败、可重新生成"。
    """

    code = "OUTLINE_BUILD_FAILED"
    http_status = 422
    default_message = "大纲生成失败:某段 LLM 输出无法解析"


class OutlineLengthCalibrationRequired(ApiError):
    """Writer 模型缺少长度校准，正式大纲生成不能静默跳过卷内细化。"""

    code = "OUTLINE_LENGTH_CALIBRATION_REQUIRED"
    http_status = 409
    default_message = "当前 Writer 模型尚未完成长度校准，已阻止大纲生成"


class OutlineNotFound(ApiError):
    """``GET`` / ``POST :commit`` 时大纲文件不存在或不全。

    404 — 资源未生成。``commit_outline`` 在 4 段 JSON 文件之一缺失时抛此错。
    """

    code = "OUTLINE_NOT_FOUND"
    http_status = 404
    default_message = "大纲不存在或尚未完整生成"


# ---- 流水线运行控制(P6-B-02)----------------------------------------------


class PipelineAlreadyRunning(ApiError):
    """``POST /api/v1/pipeline:start`` 时 workspace 已有 active run。

    409 ``CONFLICT``:语义"流水线已运行/暂停中,不能再次启动"。``details``
    含 ``workspace_id`` / ``active_run_id`` / ``active_state`` 帮前端 RunControlPage
    引导用户先 stop / resume 当前 run。
    """

    code = "PIPELINE_ALREADY_RUNNING"
    http_status = 409
    default_message = "流水线已有 active run,无法再次启动"


class PipelineNotRunning(ApiError):
    """``POST /api/v1/pipeline:pause`` / ``:resume`` / ``:stop`` 时 workspace
    无 active run。

    409 ``CONFLICT``:语义"无可暂停/恢复/终止的流水线"。``details`` 含
    ``workspace_id`` / ``action``(``pause`` / ``resume`` / ``stop``)。
    """

    code = "PIPELINE_NOT_RUNNING"
    http_status = 409
    default_message = "无 active 流水线,无法执行此操作"


class PipelineInvalidScope(ApiError):
    """``POST /api/v1/pipeline:start`` scope 与字段映射不一致 / 解析后空 /
    chapter_id 不存在。

    422 ``UNPROCESSABLE_ENTITY``:用户输入校验失败(scope=range 缺
    end_chapter_id / volume_id 不存在 / start > end 等)。``details`` 含
    ``scope`` / ``reason`` / 缺失字段名 供前端表单定位错误位置。
    """

    code = "PIPELINE_INVALID_SCOPE"
    http_status = 422
    default_message = "scope 字段组合不合法"


# ---- 导出域(P6-B-04)------------------------------------------------------


class ExportFormatUnsupported(ApiError):
    """``POST /workspaces/{id}/exports`` format 不被支持(P6-B-04 占位 epub /
    docx + 未来未知 format)。

    422 ``UNPROCESSABLE_ENTITY``:语义"该 format 尚未实现"。P6-B-04 只实现
    ``md`` / ``chaptered_md``;``epub`` / ``docx`` 占位返本错(决策 5=A 拆 3
    任务 P6-B-04/05/06,P6-B-05 接力翻 200 epub,P6-B-06 接力翻 200 docx)。
    ``details`` 含 ``format`` / ``supported_formats`` 帮前端 ExportPage 提示
    用户切到已实现格式。
    """

    code = "EXPORT_FORMAT_UNSUPPORTED"
    http_status = 422
    default_message = "导出格式尚未实现,请改选 md / chaptered_md"


class ExportNotFound(ApiError):
    """``GET /workspaces/{id}/exports/{export_id}/download`` 时 export 记录
    不存在(目录 / manifest.json / zip 任一缺失)。

    404 ``NOT_FOUND``:导出记录不存在或已过期(P6-B-04 暂无淘汰策略,
    P7+ 引入)。``details`` 含 ``export_id`` / ``workspace_id``。
    """

    code = "EXPORT_NOT_FOUND"
    http_status = 404
    default_message = "导出记录不存在"


class ExportScopeInvalid(ApiError):
    """``POST /workspaces/{id}/exports`` scope 字段组合不合法 / scope=range
    包含 invalidated 章节(P6-B-01 决策 3=A invalidated 档语义)。

    422 ``UNPROCESSABLE_ENTITY``:典型场景 — ① scope=volume 缺
    ``volume_id`` / ② scope=range 缺 ``start_chapter_id`` 或
    ``end_chapter_id`` / ③ scope=range [start, end] 闭区间内含
    ``invalidated`` 状态章节(需 user 先 :unblock 或 rewind 链清理)/ ④
    ``volume_id`` 不存在 / ``start_chapter_id`` > ``end_chapter_id`` 等。
    ``details`` 含 ``scope`` / ``reason`` + 视情况附 ``invalidated_chapter_ids``
    或缺失字段名供前端 ExportPage 表单定位错误。
    """

    code = "EXPORT_SCOPE_INVALID"
    http_status = 422
    default_message = "scope 字段组合不合法"


class CardInvalidStateTransition(ApiError):
    """``POST /chapters/{id}/card:approve`` / ``card:reject`` 当前 ``card_status``
    不允许该转换(P5-B-07 引入,决策 6=C 4 档转换规则集中表)。

    409 ``CONFLICT``:语义"章节卡处于不允许该操作的状态"。``details`` 含
    ``chapter_id`` / ``current_status`` / ``action`` / ``allowed_transitions``
    供前端 ChapterCardReviewPage 给用户提示"为何不能 approve/reject"
    (例如已 approved 状态再 approve 一次属重复操作)。

    转换规则集中表(对齐启动指令字面 + decision 6=C):

    - ``approve``:合法源 ``draft`` / ``pending_review`` / ``rejected``;
      非法源 ``approved`` → 409(重复操作)。
    - ``reject``:合法源 ``draft`` / ``pending_review`` / ``approved``;
      非法源 ``rejected`` → 409(重复操作)。

    与 ``CARD_NOT_APPROVED``(P5-B-06,409,``chapter:write`` gate)区分:
    本错误针对**审核流入口**(approve/reject),前者针对**写作流入口**
    (write/rewrite/revise)。
    """

    code = "CARD_INVALID_STATE_TRANSITION"
    http_status = 409
    default_message = "章节卡状态不允许该操作"


class CardNotApproved(ApiError):
    """``ChapterService.write/rewrite/revise`` 入口前置 gate 检测到本章
    ``ChapterCard.card_status != "approved"``(P5-B-06 决策 6=C 处置)。

    409 ``CONFLICT``:语义"章节卡未通过审核,继续写作冲突,请先审核章节卡
    (``ChapterCardReviewPage``)或等待审核流跑完"。details 含 ``chapter_id``
    / ``workspace_id`` / ``card_status`` 供前端跳转 ChapterCardReviewPage
    引导审核(P5-F-06)。**章节卡 SQLite 行不存在**(尚未生成)→ 视为
    ``card_status="draft"``(隐含未审核)→ 同样抛本错(对齐"无卡不能写"
    语义,outline:commit 后批量生成会写入 SQLite 行)。

    与 ``CHAPTER_INVALID_STATE``(章节正文状态机非法转换)区分:本错误属
    "章节卡审核未通过",非"章节状态不允许"。
    """

    code = "CARD_NOT_APPROVED"
    http_status = 409
    default_message = "章节卡未通过审核,请先完成章节卡审核后再触发写作"


class MemoryHealthBlocked(ApiError):
    """``ChapterService.write/rewrite/revise`` 入口前置 gate 读 最新一行
    ``memory_health_history.severity=critical`` 时阻断。

    409 ``CONFLICT``:语义"当前记忆状态与继续写作冲突,请人工介入修复后
    再触发"。details 含 ``check_id`` / ``severity`` / ``checked_at`` 供前端
    定位最新审计快照(配合 P4-F-05 HealthScoreBadge 跳转 / P5+ 记忆健康
    详情页)。本 gate **只读** ``memory_health_history`` 表(不主动跑
    checker,避免每次 :write 引入 LLM 开销;新鲜度依赖外部 ``POST
    /memory/check`` 显式触发 / 未来定时 cron 维持)。
    """

    code = "MEMORY_HEALTH_BLOCKED"
    http_status = 409
    default_message = "记忆健康度审计为 critical,流水线已阻断"


# ---- 任务域 -----------------------------------------------------------------


class TaskNotFound(ApiError):
    """``GET /tasks/{task_id}`` 未在进程内 LRU 字典命中。

    P1 阶段任务无持久化(LRU N=64);未命中 = 任务不存在或已被淘汰。P3
    引入持久化任务表后语义升级为"任务真不存在"。
    """

    code = "TASK_NOT_FOUND"
    http_status = 404
    default_message = "任务不存在或已过期"


# ---- 公共 / 请求语义 --------------------------------------------------------


class MissingWorkspaceHeader(ApiError):
    """章节路由必须带 ``X-Workspace-Id`` header(P1-B-19 决策 1)。

    P1 阶段 ``/chapters/{id}*`` 的工作区上下文通过 header 显式传(不嵌入 URL,
    保留 spec §4.1 "顶级 chapter" 字面契约)。缺失抛 422。
    """

    code = "MISSING_WORKSPACE_HEADER"
    http_status = 422
    default_message = "请在 X-Workspace-Id header 中提供工作区 ID"


# ---- 卷域(P5-B-09)--------------------------------------------------------


class VolumeNotFound(ApiError):
    """卷不存在或不属于当前工作区(跨工作区视为不存在)。

    ``VolumeNotFoundError``(``src/services/volume_service.py``)经
    ``_translate_service_error`` 翻译为本错误码;路由层不写 try/except。
    对齐 ``CharacterNotFound`` / ``PlotThreadNotFound`` 跨工作区视为 404
    的语义。
    """

    code = "VOLUME_NOT_FOUND"
    http_status = 404
    default_message = "卷不存在"


# ---- 静态资产导入域(P5-B-08)----------------------------------------------


class AssetsImportInvalid(ApiError):
    """``POST /api/v1/workspaces/{id}/assets:import`` 上传 markdown 解析失败。

    422 ``UNPROCESSABLE_ENTITY``:错在用户上传的 markdown 内容(必填字段缺
    失 / int 字段格式错 / 字段值不合 Pydantic schema 等),非引擎缺陷。
    ``AssetImportError`` 经 ``_translate_service_error`` 翻译为本错误码;前端
    NewBookPage 可据 message 引导用户检查文件内容(message 已带文件名 + 段
    名定位)。

    与 ``WORKSPACE_INVALID``(422)区分:本错误是"资产文件本身的格式问题",
    后者是"工作区 meta.json 缺失或不合法"。
    """

    code = "ASSETS_IMPORT_INVALID"
    http_status = 422
    default_message = "静态资产 markdown 解析失败"


# ---- 配置域(P3-B-10) ------------------------------------------------------


class ConfigYamlNotFound(ApiError):
    """引擎默认 ``config.yaml`` 不存在。

    P3-B-02 ``ConfigService`` 决定 fail-fast 不静默回退(因引擎默认是兜底);
    路由层翻译为 500,前端可提示"安装异常,请联系运维"。生产环境理应永远
    不触发(``config.yaml`` 是仓库根的引擎自带文件)。
    """

    code = "CONFIG_YAML_NOT_FOUND"
    http_status = 500
    default_message = "引擎默认 config.yaml 文件不存在,请检查仓库根目录"


class ConfigYamlInvalid(ApiError):
    """``project.yaml`` 顶层非 dict / 段落类型错 / 合并后违反交叉校验。

    422 而非 500:错在工作区配置文件(用户可改),非引擎缺陷。前端可指引
    用户检查 ``workspaces/<id>/project.yaml`` 字段。
    """

    code = "CONFIG_YAML_INVALID"
    http_status = 422
    default_message = "工作区 project.yaml 配置不合法"


# ---- API key 凭证管理域(P6-B-16) -----------------------------------------


class ApiKeyProviderUnknown(ApiError):
    """``PUT /api/v1/config/api-keys`` body ``provider`` 不在 KNOWN_PROVIDERS。

    422 而非 404:provider 字段语义上是 "可枚举",未知值是 body 校验错误
    (前端下拉只能选已知 provider),不是 "资源不存在"。当前 KNOWN_PROVIDERS
    只含 ``claude``;后续若 Qwen 走 API key 形态(目前是本地 ollama 无 key),
    可在 ``ApiKeyService.KNOWN_PROVIDERS`` 扩展不动本错误码。
    """

    code = "API_KEY_PROVIDER_UNKNOWN"
    http_status = 422
    default_message = "未知的 API key provider"


class ApiKeyWriteFailed(ApiError):
    """``ApiKeyService.put`` 写入 ``.env`` 时 IO 失败(权限/磁盘满/路径异常)。

    500 而非 422:写入是服务端动作,失败属服务端缺陷(磁盘问题 / 文件权限)
    而非 user 输入错;前端应提示 "保存失败,请联系管理员检查 .env 文件权限"。
    """

    code = "API_KEY_WRITE_FAILED"
    http_status = 500
    default_message = "API key 写入 .env 文件失败"


# ---- 通用 -------------------------------------------------------------------


class ValidationFailed(ApiError):
    """通用 Pydantic 422,handler 内部用,不直接 raise。"""

    code = "VALIDATION_FAILED"
    http_status = 422
    default_message = "请求参数校验失败"


# =============================================================================
# Service 异常 → ApiError 翻译
# =============================================================================


def _translate_service_error(exc: BaseException) -> ApiError | None:
    """把已知 service 层异常翻译成 ApiError。

    无匹配返回 ``None``,由兜底中间件当未知异常处理(返回 ``INTERNAL_ERROR``)。

    P1 翻译表:
      ``MetaFileError`` -> ``WorkspaceInvalid``
      ``WorkspaceServiceError`` -> ``WorkspaceConflict``
      ``ChapterServiceError`` -> ``ChapterNotFound``(message 含"不存在")
                              -> ``ChapterCardInvalid``(message 含"章节卡")
                              -> ``ChapterInvalidState``(其他)
      ``ChapterVersioningError`` -> ``ChapterVersionConflict``

    P1 简版:``ChapterServiceError`` 按消息子串分流,精度不高但够用。P2 引入
    SQLite 落库后建议改为 service 层抛带错误码的异常子类(类似 ``ApiError``
    模式),那时再升级翻译表。
    """

    if isinstance(exc, ProviderCallCapExceeded):
        return ProviderCallCapReached(
            str(exc),
            details={
                "workspace_id": exc.workspace_id,
                "used": exc.used,
                "cap": exc.cap,
            },
        )

    if isinstance(
        exc,
        (RewriteCheckpointRecoveryError, ChapterOperationLeaseError),
    ):
        return ChapterInvalidState(
            str(exc),
            details={"reason": "chapter_operation_or_checkpoint_not_safe"},
        )

    if isinstance(exc, CoreStateServiceError):
        return ChapterInvalidState(
            str(exc),
            details={"reason": "core_state_contract_or_revision_not_safe"},
        )

    if isinstance(exc, AssetImportError):
        return AssetsImportInvalid(str(exc))

    if isinstance(exc, VolumeNotFoundError):
        return VolumeNotFound(str(exc))

    if isinstance(exc, VolumeUpdateInvalidError):
        # 复用 WORKSPACE_INVALID 422(对齐既有 outline / asset_import 模式;
        # 本任务自决项 — 不增 VolumeUpdateInvalid 独立错误码,见
        # docs/impl/p5_b_09 §"自决项")。details 含 Pydantic 字段级 errors
        # 供前端定位。
        return WorkspaceInvalid(
            str(exc), details={"errors": exc.errors}
        )

    if isinstance(exc, MetaFileError):
        return WorkspaceInvalid(str(exc))

    # P7-B-07:WorkspaceDestroyConfirmInvalidError 是 WorkspaceServiceError 子类,
    # 必须先匹配,否则被基类兜底翻成 409 WORKSPACE_CONFLICT(语义偏离 422 confirm
    # 错误)。对齐 BackupNotFoundError → BackupServiceError 同顺序敏感模式。
    if isinstance(exc, WorkspaceDestroyConfirmInvalidError):
        return WorkspaceDestroyConfirmInvalid(
            str(exc),
            details={
                "workspace_id": exc.workspace_id,
                "got": exc.got,
            },
        )

    if isinstance(exc, SelectionActivityError):
        return WorkspaceInvalid(str(exc))
    if isinstance(exc, WorkspaceServiceError):
        return WorkspaceConflict(str(exc))

    # v2 W3.5 UI 缺口3:re-review 状态闸错误。**必须在通用 ChapterServiceError
    # 分支之前**(两者都是 ChapterServiceError 子类,先匹配具体的)。
    if isinstance(exc, ChapterReReviewInvalidStateError):
        return ChapterReReviewInvalidState(
            str(exc),
            details={
                "chapter_id": exc.chapter_id,
                "current_status": exc.current_status,
            },
        )

    if isinstance(exc, ChapterReReviewNoVersionError):
        return ChapterReReviewNoVersion(
            str(exc),
            details={"chapter_id": exc.chapter_id},
        )

    if isinstance(exc, ChapterCardSettingStaleError):
        return ChapterCardSettingStale(
            str(exc),
            details={
                "failure_layer": "writer_preflight_setting_stale",
                "chapter_id": exc.chapter_id,
                "constraint_id": exc.constraint_id,
                "card_contract_hash": exc.card_contract_hash,
                "current_contract_hash": exc.current_contract_hash,
            },
        )

    if isinstance(exc, ContinuityLedgerNotReadyError):
        return ContinuityLedgerNotReady(
            str(exc),
            details={
                "failure_layer": "continuity_ledger_before_ready",
                "chapter_id": exc.chapter_id,
                "reason": exc.reason,
            },
        )

    if isinstance(exc, ChapterCardContinuityStaleError):
        return ChapterCardContinuityStale(
            str(exc),
            details={
                "failure_layer": "writer_preflight_continuity_stale",
                "chapter_id": exc.chapter_id,
                "reason": exc.reason,
                "frozen": (
                    exc.frozen.model_dump(mode="json")
                    if exc.frozen is not None
                    else None
                ),
                "current": (
                    exc.current.model_dump(mode="json")
                    if exc.current is not None
                    else None
                ),
            },
        )

    if isinstance(exc, ChapterCardContinuityBridgeError):
        return ChapterCardContinuityBridgeRequired(
            str(exc),
            details={
                "failure_layer": "writer_preflight_continuity_bridge",
                "chapter_id": exc.chapter_id,
                "reason": exc.reason,
            },
        )

    if isinstance(exc, ChapterServiceError):
        msg = str(exc)
        if "不存在" in msg and "章节卡" not in msg:
            return ChapterNotFound(msg)
        if "章节卡" in msg:
            return ChapterCardInvalid(msg)
        return ChapterInvalidState(msg)

    if isinstance(exc, ChapterCardGeneratorError):
        if exc.error_code == ChapterCardSettingConflict.code:
            return ChapterCardSettingConflict(str(exc), details=exc.details)
        if exc.error_code == ChapterCardContractConflict.code:
            return ChapterCardContractConflict(str(exc), details=exc.details)

    if isinstance(exc, ChapterVersioningError):
        return ChapterVersionConflict(str(exc))

    # WriterProfileNotFoundError 必须在 ProfileNotFoundError 之前匹配:它是
    # ProfileNotFoundError 的同名 ValueError 但语义不同(章节卡持久化 vs 单次
    # request body),且并非子类关系(平行 ValueError),precedence 与子类匹配
    # 无关,但显式先列以便阅读时一眼能看到两个 422 分流。
    if isinstance(exc, WriterProfileNotFoundError):
        # details 由 _orchestrate 抛出点构造时附在 exc.args(参考 ChapterServiceError
        # 模式),本翻译只取 str(exc) 作为 message;details 由 ApiError __init__
        # 透传(若 service 层用 exc.details 属性,本调用形态需扩展)。
        return WriterProfileNotFound(str(exc), details=getattr(exc, "details", None))

    if isinstance(exc, ProfileNotFoundError):
        return ChapterInvalidProfile(str(exc))

    if isinstance(exc, CharacterNotFoundError):
        return CharacterNotFound(str(exc))

    if isinstance(exc, CharacterAlreadyExistsError):
        return CharacterAlreadyExists(str(exc))

    if isinstance(exc, PlotThreadNotFoundError):
        return PlotThreadNotFound(str(exc))

    if isinstance(exc, PlotThreadAlreadyExistsError):
        return PlotThreadAlreadyExists(str(exc))

    if isinstance(exc, TimelineEntryNotFoundError):
        return TimelineEntryNotFound(str(exc))

    if isinstance(exc, TimelineEntryAlreadyExistsError):
        return TimelineEntryAlreadyExists(str(exc))

    if isinstance(exc, CardContinuityLedgerStaleError):
        return ChapterCardContinuityStale(
            str(exc),
            details={
                "failure_layer": "card_approval_continuity_stale",
                "chapter_id": exc.chapter_id,
                "reason": exc.reason,
                "frozen": (
                    exc.frozen.model_dump(mode="json")
                    if exc.frozen is not None
                    else None
                ),
                "current": (
                    exc.current.model_dump(mode="json")
                    if exc.current is not None
                    else None
                ),
            },
        )

    if isinstance(exc, CardContinuityBridgeRequiredError):
        return ChapterCardContinuityBridgeRequired(
            str(exc),
            details={
                "failure_layer": "card_approval_continuity_bridge",
                "chapter_id": exc.chapter_id,
                "reason": exc.reason,
            },
        )

    if isinstance(exc, CardInvalidStateTransitionError):
        return CardInvalidStateTransition(
            str(exc),
            details={
                "chapter_id": exc.chapter_id,
                "current_status": exc.current_status,
                "action": exc.action,
                "allowed_sources": list(exc.allowed_sources),
            },
        )

    if isinstance(exc, CardNotFoundError):
        return ChapterCardNotFound(str(exc))

    # v1.0.x wave 4 session 5:ChapterSummaryNotFoundError 是
    # ChapterSummaryServiceError 子类,顺序敏感先匹配子类。
    if isinstance(exc, ChapterSummaryNotFoundError):
        return ChapterSummaryNotFound(str(exc))

    if isinstance(exc, ChapterSummaryServiceError):
        # 未细分子类的兜底翻 WORKSPACE_INVALID 422(对齐 outline / asset_import
        # 同型;细分错误码留 v1.1 真实战触发后再加)
        return WorkspaceInvalid(str(exc))

    # P6-B-01:chapter_rewind_service 异常翻译(顺序敏感 — Invalidated /
    # UnblockInvalidState / RewindInvalidState 是 ChapterRewindServiceError
    # 子类,具体子类优先匹配;最后 fallback 兜底基类翻 CHAPTER_INVALID_STATE
    # 409 对齐 force-pass 通用源不合法 / "章节不存在" 通过 message 含"不存在"
    # 关键字翻 CHAPTER_NOT_FOUND 404)。
    if isinstance(exc, ChapterInvalidatedNeedsUnblockError):
        return ChapterInvalidatedNeedsUnblock(
            str(exc),
            details={
                "chapter_id": exc.chapter_id,
                "workspace_id": exc.workspace_id,
            },
        )

    if isinstance(exc, ChapterRewindInvalidStateError):
        return ChapterRewindInvalidState(
            str(exc),
            details={
                "chapter_id": exc.chapter_id,
                "current_status": exc.current_status,
                "allowed_sources": list(exc.allowed_sources),
            },
        )

    if isinstance(exc, ChapterUnblockInvalidStateError):
        return ChapterUnblockInvalidState(
            str(exc),
            details={
                "chapter_id": exc.chapter_id,
                "current_status": exc.current_status,
            },
        )

    if isinstance(exc, ForcePassContinuityLedgerNotReadyError):
        return ContinuityLedgerNotReady(
            str(exc),
            details={
                "failure_layer": "force_pass_continuity_ledger_before_ready",
                "chapter_id": exc.chapter_id,
                "reason": exc.reason,
            },
        )

    if isinstance(exc, ChapterRewindServiceError):
        msg = str(exc)
        if "不存在" in msg:
            return ChapterNotFound(msg)
        return ChapterInvalidState(msg)

    # P6-B-07:backup_service 异常翻译(顺序敏感 — 具体子类优先,
    # BackupServiceError 基类最后兜底翻 WORKSPACE_CONFLICT 409 对齐 P1-B-14
    # WorkspaceServiceError 同语义"工作区状态冲突 / IO 失败")
    if isinstance(exc, BackupNotFoundError):
        return BackupNotFound(
            str(exc),
            details={
                "workspace_id": exc.workspace_id,
                "backup_id": exc.backup_id,
            },
        )

    if isinstance(exc, RestoreVersionIncompatibleError):
        return RestoreVersionIncompatible(
            str(exc),
            details={
                "workspace_id": exc.workspace_id,
                "backup_id": exc.backup_id,
                "backup_schema_version": exc.backup_schema_version,
                "current_schema_version": exc.current_schema_version,
                "reason": exc.reason,
                "needs_migration": exc.reason == "backup_older",
                "needs_engine_upgrade": exc.reason == "backup_newer",
            },
        )

    if isinstance(exc, BackupInvalidFormatError):
        return BackupInvalidFormat(
            str(exc),
            details={
                "workspace_id": exc.workspace_id,
                "backup_id": exc.backup_id,
                "reason": exc.reason,
            },
        )

    if isinstance(exc, BackupServiceError):
        return WorkspaceConflict(str(exc))

    # P6-B-08:workspace_version_service 异常翻译(顺序敏感 — 3 具体子类优先,
    # WorkspaceVersionServiceError 基类最后兜底翻 WORKSPACE_CONFLICT 409 对齐
    # P1-B-14 WorkspaceServiceError 同语义)
    if isinstance(exc, WorkspaceVersionIncompatibleError):
        return WorkspaceVersionIncompatible(
            str(exc),
            details={
                "workspace_id": exc.workspace_id,
                "workspace_engine_version": exc.workspace_engine_version,
                "current_engine_version": exc.current_engine_version,
            },
        )

    if isinstance(exc, WorkspaceMigrationConfirmInvalidError):
        return WorkspaceMigrationConfirmInvalid(
            str(exc),
            details={"workspace_id": exc.workspace_id},
        )

    if isinstance(exc, WorkspaceMigrationBackupFailedError):
        return WorkspaceMigrationBackupFailed(
            str(exc),
            details={
                "workspace_id": exc.workspace_id,
                "reason": exc.reason,
            },
        )

    if isinstance(exc, WorkspaceVersionServiceError):
        return WorkspaceConflict(str(exc))

    if isinstance(exc, OutlineGateViolation):
        # acceptance 修复1(2026-06-10):按违规 rule 分流错误码 — 同一异常类
        # 承载两种 gate(arc 横跨 / 卷界重演),service 层分别 raise,rule 集合
        # 必然纯一;混合兜底走 arc 码保持向后兼容。
        details = {
            "violations": [f.model_dump(mode="json") for f in exc.failures],
        }
        rules = {getattr(f, "rule", "") for f in exc.failures}
        if rules == {"volume_seam_replay"}:
            return OutlineVolumeSeamReplay(str(exc), details=details)
        return OutlineArcTooShort(str(exc), details=details)

    if isinstance(exc, OutlineNotFoundServiceError):
        return OutlineNotFound(str(exc))

    if isinstance(exc, OutlineLengthCalibrationRequiredError):
        return OutlineLengthCalibrationRequired(
            str(exc),
            details={
                "workspace_id": exc.workspace_id,
                "model": exc.model,
                "genre": exc.genre,
                "reason": exc.reason,
                "blocked_stage": "volume_refine",
                "provider_requests_started": 0,
            },
        )

    # v2 W3.5 UI 缺口1:大纲生成失败结构化。带 contradictions = 跨段一致性硬伤
    # (列冲突 + 高亮实体名);否则 = 某段 JSON/schema 失败(提示哪段可重生)。
    # 原均落通用 INTERNAL_ERROR,前端只见一行 message 无可操作信息。
    if isinstance(exc, OutlineBuilderError):
        if exc.error_code == PlotSettingConflict.code:
            return PlotSettingConflict(
                str(exc),
                details={
                    "failure_layer": "plot",
                    "stage": exc.stage,
                    "contract_hash": exc.contract_hash,
                    "attempts": exc.attempts,
                    "constraint_violations": list(exc.violations or []),
                },
            )
        if exc.error_code == VolumePlansSettingConflict.code:
            return VolumePlansSettingConflict(
                str(exc),
                details={
                    "failure_layer": "volume_plans",
                    "stage": exc.stage,
                    "contract_hash": exc.contract_hash,
                    "attempts": exc.attempts,
                    "constraint_violations": list(exc.violations or []),
                },
            )
        if exc.error_code == MiniPlanSettingConflict.code:
            return MiniPlanSettingConflict(
                str(exc),
                details={
                    "failure_layer": "mini_plan",
                    "stage": exc.stage,
                    "contract_hash": exc.contract_hash,
                    "attempts": exc.attempts,
                    "constraint_violations": list(exc.violations or []),
                },
            )
        if exc.contradictions:
            return OutlineConsistencyConflict(
                str(exc),
                details={
                    "stage": exc.stage,
                    "contradictions": list(exc.contradictions),
                    "contradiction_details": list(exc.contradiction_details or []),
                    "coverage_gaps": list(exc.coverage_gaps or []),
                    "evidence": exc.evidence,
                    "candidate_id": exc.candidate_id,
                    "repair_scope": exc.repair_scope,
                },
            )
        if exc.model_used == "outline_arc_span_gate":
            return OutlineArcTooShort(
                str(exc),
                details={
                    "stage": exc.stage,
                    "model_used": exc.model_used,
                    "violations": list(exc.violations or []),
                },
            )
        return OutlineBuildFailed(
            str(exc),
            details={"stage": exc.stage, "model_used": exc.model_used},
        )

    # P6-B-02:pipeline_service 异常翻译(顺序敏感 — 具体子类优先,
    # PipelineServiceError 基类最后兜底翻 INTERNAL_ERROR 500)
    if isinstance(exc, PipelineAlreadyRunningError):
        return PipelineAlreadyRunning(
            str(exc),
            details={
                "workspace_id": exc.workspace_id,
                "active_run_id": exc.active_run_id,
                "active_state": exc.active_state,
            },
        )

    if isinstance(exc, PipelineNotRunningError):
        return PipelineNotRunning(
            str(exc),
            details={
                "workspace_id": exc.workspace_id,
                "action": exc.action,
            },
        )

    if isinstance(exc, PipelineInvalidScopeError):
        scope_details: dict[str, Any] = {
            "scope": exc.scope,
            "reason": exc.reason,
        }
        scope_details.update(exc.invalid_details)
        return PipelineInvalidScope(str(exc), details=scope_details)

    # P6-B-04:export_service 异常翻译(具体子类优先,基类兜底 INTERNAL_ERROR)
    if isinstance(exc, ExportFormatUnsupportedError):
        return ExportFormatUnsupported(
            str(exc),
            details={
                "format": exc.format,
                "supported_formats": list(exc.supported_formats),
            },
        )

    if isinstance(exc, ExportNotFoundError):
        return ExportNotFound(
            str(exc),
            details={
                "export_id": exc.export_id,
                "workspace_id": exc.workspace_id,
            },
        )

    if isinstance(exc, ExportScopeInvalidError):
        export_scope_details: dict[str, Any] = {
            "scope": exc.scope,
            "reason": exc.reason,
        }
        export_scope_details.update(exc.invalid_details)
        return ExportScopeInvalid(str(exc), details=export_scope_details)

    if isinstance(exc, ExportServiceError):
        # 基类兜底:未识别的 ExportServiceError 子类(预留 P6-B-05/06 epub/docx
        # 实现期间新增子类时不必同步改翻译表)→ 翻 INTERNAL_ERROR 500。
        return ApiError(str(exc))

    if isinstance(exc, PipelineServiceError):
        # 基类兜底:未识别的 PipelineServiceError 子类(P6-B-03 接力可能新增)
        # 翻 INTERNAL_ERROR 500;此 path 仅作 forward-compat 保险,P6-B-02 内
        # 实际只 raise 上方 3 具体子类。
        return ApiError(str(exc))

    # P6-B-10:reference_validator 异常翻译。本异常由
    # ``ChapterCardService.generate_single_chapter_card`` / ``retry_references``
    # / ``ChapterService._run_gates`` 抛出;details schema 与
    # ReferenceValidationFailedError 属性字段对齐。
    if isinstance(exc, ReferenceValidationFailedError):
        return ReferenceValidationFailed(
            str(exc),
            details={
                "workspace_id": exc.workspace_id,
                "chapter_id": exc.chapter_id,
                "unknown_character_ids": list(exc.unknown_character_ids),
                "unknown_thread_ids": list(exc.unknown_thread_ids),
            },
        )

    # P6-B-11:validator_report_service 异常翻译(类 C 顺延 P6-F-01)。
    # 由 ``ValidatorReportService.get_latest_report`` 抛出;`final.md` 缺失时
    # 翻 404 ``VALIDATOR_REPORT_NOT_FOUND``,前端据此提示用户先 :write。
    if isinstance(exc, ValidatorReportNotFoundError):
        return ValidatorReportNotFound(str(exc))

    if isinstance(exc, MemoryHealthCheckerError):
        details: dict[str, Any] = {
            "model_used": exc.model_used,
            # 截断 raw_output 防 details 过大;诊断时 caller 可查 structlog
            # ``memory_health_checker_parse_failed`` 事件拿完整内容。
            "raw_output_preview": exc.raw_output[:500],
        }
        return MemoryHealthCheckerFailed(str(exc), details=details)

    # P4-B-10:风格审计 service 异常翻译为 CHAPTER_NOT_FOUND(P4 stub 只在
    # chapter_range_start / _end 校验失败时抛此异常,语义统一)。
    if isinstance(exc, StyleAuditServiceError):
        return ChapterNotFound(str(exc))

    return None


# =============================================================================
# 响应组装
# =============================================================================


def _sanitize_validation_error(err: dict[str, Any]) -> dict[str, Any]:
    """把单条 Pydantic 校验错误转成 JSON 可序列化形态。

    ``model_validator`` / ``field_validator`` 抛 ``ValueError`` 时,Pydantic
    在 ``ctx`` 里塞原始异常对象(键 ``error``)+ ``input`` 可能含任意类型 —
    都非 JSON 原生 → 下游 ``model_dump(mode="json")`` 抛
    ``PydanticSerializationError`` → 错误响应反成 500。这里把 ``ctx`` 内的异常
    对象转成字符串消息,其余字段原样保留(loc / msg / type 等都是 JSON 原生)。
    """
    safe = dict(err)
    ctx = safe.get("ctx")
    if isinstance(ctx, dict):
        safe["ctx"] = {
            k: (str(v) if isinstance(v, BaseException) else v)
            for k, v in ctx.items()
        }
    return safe


def _build_error_response(
    *,
    request_id: str,
    code: str,
    message: str,
    http_status: int,
    details: dict[str, Any] | None = None,
) -> JSONResponse:
    """组装符合 API_CONTRACT §3.2 的错误响应。"""

    payload = ErrorEnvelope(
        error=ErrorPayload(
            code=code,
            message=message,
            details=details,
            trace_id=None,
            request_id=request_id,
        )
    )
    return JSONResponse(
        status_code=http_status,
        content=payload.model_dump(mode="json", exclude_none=True),
    )


# =============================================================================
# FastAPI handler 注册:ApiError + 422
# =============================================================================


_HTTP_EXC_CODE_MAP: dict[int, str] = {
    404: "ROUTE_NOT_FOUND",
    405: "METHOD_NOT_ALLOWED",
}


def register_exception_handlers(app: FastAPI) -> None:
    """注册三类 handler:``ApiError`` / ``RequestValidationError`` /
    starlette ``HTTPException``。

    ``Exception`` 基类的兜底翻译在 ``ApiErrorTranslationMiddleware`` 里做,见
    模块 docstring。
    """

    @app.exception_handler(ApiError)
    async def _api_error_handler(request: Request, exc: ApiError) -> JSONResponse:
        request_id = getattr(request.state, "request_id", "req_unknown")
        _log.info(
            "api_error",
            method=request.method,
            path=request.url.path,
            request_id=request_id,
            error_code=exc.code,
            http_status=exc.http_status,
        )
        return _build_error_response(
            request_id=request_id,
            code=exc.code,
            message=exc.message,
            http_status=exc.http_status,
            details=exc.details,
        )

    @app.exception_handler(StarletteHTTPException)
    async def _http_exception_handler(
        request: Request, exc: StarletteHTTPException
    ) -> JSONResponse:
        request_id = getattr(request.state, "request_id", "req_unknown")
        code = _HTTP_EXC_CODE_MAP.get(exc.status_code, f"HTTP_{exc.status_code}")
        message = exc.detail if isinstance(exc.detail, str) else str(exc.detail)
        _log.info(
            "api_error",
            method=request.method,
            path=request.url.path,
            request_id=request_id,
            error_code=code,
            http_status=exc.status_code,
        )
        return _build_error_response(
            request_id=request_id,
            code=code,
            message=message,
            http_status=exc.status_code,
        )

    @app.exception_handler(RequestValidationError)
    async def _validation_handler(
        request: Request, exc: RequestValidationError
    ) -> JSONResponse:
        request_id = getattr(request.state, "request_id", "req_unknown")
        # Pydantic 报的字段级错误塞进 details.errors,前端可定位字段。
        # model_validator(mode="after") 抛 ValueError 时,Pydantic 把原始异常
        # 对象塞进 ``ctx.error``(非 JSON 原生类型)→ 下游 model_dump(mode="json")
        # 无法序列化 → 反成 500。这里把 ctx 内的异常对象转成字符串消息,保留
        # 字段定位 + 错误文本,确保所有 model_validator 错误都返干净 422。
        details = {"errors": [_sanitize_validation_error(e) for e in exc.errors()]}
        _log.info(
            "api_error",
            method=request.method,
            path=request.url.path,
            request_id=request_id,
            error_code=ValidationFailed.code,
            http_status=ValidationFailed.http_status,
        )
        return _build_error_response(
            request_id=request_id,
            code=ValidationFailed.code,
            message=ValidationFailed.default_message,
            http_status=ValidationFailed.http_status,
            details=details,
        )


# =============================================================================
# 兜底翻译中间件(取代 FastAPI 全局 Exception handler)
# =============================================================================


class ApiErrorTranslationMiddleware:
    """ASGI 中间件:捕获未被 FastAPI ExceptionMiddleware 处理的异常,翻译成
    ApiError 响应包络后直接 send。

    挂载位置:user middleware **最内层**(最先 ``add_middleware``),保证它在
    ``RequestIDMiddleware`` / ``AccessLogMiddleware`` 内、starlette
    ExceptionMiddleware 外。

    职责边界:
    - 不接 ``ApiError`` / ``RequestValidationError``——它们已被 starlette
      ExceptionMiddleware dispatch 给 FastAPI 注册的 handler,转成正常 response。
    - 接 ``WorkspaceServiceError`` / ``MetaFileError`` / ``ChapterServiceError``
      / ``ChapterVersioningError`` 等已知 service 异常 → ``_translate_service_error``。
    - 兜底任何其他 ``Exception`` → ``INTERNAL_ERROR`` + structlog 落 traceback。
    """

    def __init__(self, app: ASGIApp) -> None:
        self.app = app

    async def __call__(self, scope: Scope, receive: Receive, send: Send) -> None:
        if scope["type"] != "http":
            await self.app(scope, receive, send)
            return

        try:
            await self.app(scope, receive, send)
        except Exception as exc:
            request_id = scope.get("state", {}).get("request_id", "req_unknown")
            method = scope.get("method", "")
            path = scope.get("path", "")

            translated = _translate_service_error(exc)
            if translated is not None:
                _log.info(
                    "api_error",
                    method=method,
                    path=path,
                    request_id=request_id,
                    error_code=translated.code,
                    http_status=translated.http_status,
                    source_exception=type(exc).__name__,
                )
                await _send_error(
                    send,
                    request_id=request_id,
                    code=translated.code,
                    message=translated.message,
                    http_status=translated.http_status,
                    details=translated.details,
                )
                return

            # 未知异常:traceback 落 structlog,响应给通用 INTERNAL_ERROR。
            _log.exception(
                "api_unhandled_exception",
                method=method,
                path=path,
                request_id=request_id,
                exception_type=type(exc).__name__,
            )
            await _send_error(
                send,
                request_id=request_id,
                code=ApiError.code,
                message=ApiError.default_message,
                http_status=ApiError.http_status,
                details=None,
            )


async def _send_error(
    send: Send,
    *,
    request_id: str,
    code: str,
    message: str,
    http_status: int,
    details: dict[str, Any] | None,
) -> None:
    """直接通过 ASGI ``send`` 发送 ApiError 响应,不经过 starlette Response 层。

    自己序列化以避免与 ServerErrorMiddleware / ExceptionMiddleware 的 sender
    包装链发生交互。
    """

    payload = ErrorEnvelope(
        error=ErrorPayload(
            code=code,
            message=message,
            details=details,
            trace_id=None,
            request_id=request_id,
        )
    )
    body = json.dumps(
        payload.model_dump(mode="json", exclude_none=True),
        ensure_ascii=False,
    ).encode("utf-8")
    await send(
        {
            "type": "http.response.start",
            "status": http_status,
            "headers": [
                (b"content-type", b"application/json; charset=utf-8"),
                (b"content-length", str(len(body)).encode("latin-1")),
            ],
        }
    )
    await send({"type": "http.response.body", "body": body})
