"""工作区路由(P1-B-18)。

对齐 PROJECT_SPEC §9 工作区生命周期 9 个动作 + API_CONTRACT §2 / §4.6 URL
规范(自定义动词用 ``POST /resource/{id}:action``,Google AIP-136 风格)。

路由职责边界
============

路由层只做:

- 参数校验(Pydantic schema + ``PurgeConfirmMismatch`` 字段比对)
- **单字段单条件**状态守卫(``WorkspaceAlreadyArchived`` / ``ArchiveNotFound``)
- 调 service + 包装响应

**不做**:

- 复合条件判断("status active 且 last_active_at < now-7d" 等)→ service
- 算 score / 决定 verdict / 串多 service → service
- try/except 翻译异常 → 全局 ``ApiErrorTranslationMiddleware``(P1-B-17)

路由前置校验 vs service 异常翻译
================================

两条互补轨道(完整划分见 ``docs/impl/p1_b_18_workspaces_routes.md``):

- **前置校验路由抛专用 ApiError**:``PurgeConfirmMismatch`` /
  ``WorkspaceAlreadyArchived`` / ``ArchiveNotFound``——"操作不该执行的事前
  阻止",细分错误码引导前端正确处置。
- **service 异常走翻译表**:``WorkspaceServiceError`` → ``WorkspaceConflict``
  (P1-B-17 决策,P1 简版粗粒度,P2 service 抛错误码异常子类时再细分)。
"""

from __future__ import annotations

from fastapi import APIRouter, Body, Depends, status

from src.api.deps import (
    get_backup_service,
    get_chapter_rewind_service,
    get_volume_service,
    get_workspace_service,
    get_workspace_version_service,
)
from src.api.errors import (
    PurgeBackupsConfirmMismatch,
    PurgeConfirmMismatch,
    RestoreConfirmMismatch,
    WorkspaceAlreadyArchived,
    WorkspaceDestroyConfirmInvalid,
    WorkspaceNotFound,
)
from src.api.schemas.backups import (
    BackupInfoOut,
    BackupListOut,
    PurgeBackupsIn,
    PurgeBackupsResultOut,
    RestoreIn,
    RestoreResultOut,
)
from src.api.schemas.chapters import (
    WorkspaceResetStaleIn,
    WorkspaceResetStaleOut,
    WorkspaceUnblockAllIn,
    WorkspaceUnblockAllOut,
)
from src.api.schemas.common import SuccessEnvelope
from src.api.schemas.workspace_version import (
    MigrateIn,
    MigrateResultOut,
    VersionStatusOut,
)
from src.api.schemas.workspaces import (
    WorkspaceArchiveResult,
    WorkspaceCleanResult,
    WorkspaceCreateIn,
    WorkspaceDestroyIn,
    WorkspaceDestroyResult,
    WorkspaceOut,
    WorkspacePurgeIn,
    WorkspacePurgeResult,
)
from src.services.backup_service import BackupService
from src.services.chapter_rewind_service import ChapterRewindService
from src.services.post_v8_abc_selection_activity import (
    validate_product_workspace_create,
)
from src.services.volume_service import VolumeService
from src.services.workspace_service import WorkspaceService
from src.services.workspace_version_service import WorkspaceVersionService

router = APIRouter(prefix="/workspaces", tags=["workspaces"])


# ---- CRUD -------------------------------------------------------------------


@router.post(
    "",
    response_model=SuccessEnvelope[WorkspaceOut],
    status_code=status.HTTP_201_CREATED,
    summary="创建工作区",
    description="创建一个新工作区,默认 activate=true 同步把 _active 软链指过去。",
)
async def create_workspace(
    body: WorkspaceCreateIn,
    service: WorkspaceService = Depends(get_workspace_service),
) -> SuccessEnvelope[WorkspaceOut]:
    validate_product_workspace_create(
        workspace_id=body.workspace_id,
        display_name=body.display_name,
        genre=body.genre,
        premise=body.premise,
        total_chapters=body.total_chapters,
        default_voice=body.default_voice,
        style_template=body.style_template,
    )
    workspace = service.create(
        body.workspace_id,
        display_name=body.display_name,
        genre=body.genre,
        premise=body.premise,
        total_chapters=body.total_chapters,
        default_voice=body.default_voice,
        style_template=body.style_template,
        activate=body.activate,
    )
    return SuccessEnvelope(data=WorkspaceOut.model_validate(workspace.model_dump()))


@router.get(
    "",
    response_model=SuccessEnvelope[list[WorkspaceOut]],
    summary="列出所有工作区",
    description="枚举本机 workspaces 根目录下的所有合法工作区(meta.json 存在且合法)。",
)
async def list_workspaces(
    service: WorkspaceService = Depends(get_workspace_service),
) -> SuccessEnvelope[list[WorkspaceOut]]:
    workspaces = service.list()
    return SuccessEnvelope(
        data=[WorkspaceOut.model_validate(w.model_dump()) for w in workspaces],
    )


@router.get(
    "/{workspace_id}",
    response_model=SuccessEnvelope[WorkspaceOut],
    summary="工作区详情",
    description=(
        "读 meta.json 返回单个工作区元数据 + ``outline_status`` API 表面专属"
        "字段(wave 6 s1+s4 起新增,3 档 ``not_started`` / ``generating`` / "
        "``committed``)。OutlinePage 据此渲染三档状态分支。``list_workspaces``"
        "端点性能保守不填(默认 ``not_started``)。"
    ),
)
async def get_workspace(
    workspace_id: str,
    service: WorkspaceService = Depends(get_workspace_service),
    volume_service: VolumeService = Depends(get_volume_service),
) -> SuccessEnvelope[WorkspaceOut]:
    workspace = service.info(workspace_id)
    outline_status = volume_service.get_outline_status(workspace_id)
    out = WorkspaceOut.model_validate(
        {**workspace.model_dump(), "outline_status": outline_status}
    )
    return SuccessEnvelope(data=out)


# ---- 生命周期动作(POST /{id}:action) --------------------------------------


@router.post(
    "/{workspace_id}:switch",
    response_model=SuccessEnvelope[WorkspaceOut],
    summary="切换激活工作区",
    description="把 _active 软链指向目标工作区,刷新 last_active_at。重复 switch 幂等。",
)
async def switch_workspace(
    workspace_id: str,
    service: WorkspaceService = Depends(get_workspace_service),
) -> SuccessEnvelope[WorkspaceOut]:
    workspace = service.switch(workspace_id)
    return SuccessEnvelope(data=WorkspaceOut.model_validate(workspace.model_dump()))


@router.post(
    "/{workspace_id}:archive",
    response_model=SuccessEnvelope[WorkspaceArchiveResult],
    summary="归档工作区",
    description=(
        "打包到项目级 archives/<id>.tar.gz 并删除原目录。"
        "已归档(meta.status='archived')直接返回 WORKSPACE_ALREADY_ARCHIVED(409)。"
    ),
)
async def archive_workspace(
    workspace_id: str,
    service: WorkspaceService = Depends(get_workspace_service),
) -> SuccessEnvelope[WorkspaceArchiveResult]:
    current = service.info(workspace_id)
    if current.status == "archived":
        raise WorkspaceAlreadyArchived(
            f"工作区 {workspace_id} 已归档,无需重复归档"
        )
    archive_path = service.archive(workspace_id)
    return SuccessEnvelope(
        data=WorkspaceArchiveResult(
            workspace_id=workspace_id, archive_path=str(archive_path)
        )
    )


@router.post(
    "/{workspace_id}:clean",
    response_model=SuccessEnvelope[WorkspaceCleanResult],
    summary="清理运行时数据",
    description="删除 data/chroma/、工作区内 archives/、logs/;保留正文/大纲/人设/SQLite db。",
)
async def clean_workspace(
    workspace_id: str,
    service: WorkspaceService = Depends(get_workspace_service),
) -> SuccessEnvelope[WorkspaceCleanResult]:
    service.clean(workspace_id)
    return SuccessEnvelope(data=WorkspaceCleanResult(workspace_id=workspace_id))


@router.post(
    "/{workspace_id}:purge",
    response_model=SuccessEnvelope[WorkspacePurgeResult],
    summary="物理删除工作区",
    description=(
        "彻底删除工作区目录(不动顶级 archives 与工作区内 backups)。"
        "请求 body 必传 confirm,值等于 URL 中 workspace_id。"
    ),
)
async def purge_workspace(
    workspace_id: str,
    body: WorkspacePurgeIn,
    service: WorkspaceService = Depends(get_workspace_service),
) -> SuccessEnvelope[WorkspacePurgeResult]:
    if body.confirm != workspace_id:
        raise PurgeConfirmMismatch(
            f"请在 confirm 字段填入工作区 ID '{workspace_id}' 以确认删除"
        )
    service.purge(workspace_id, confirm=True)
    return SuccessEnvelope(data=WorkspacePurgeResult(workspace_id=workspace_id))


@router.post(
    "/{workspace_id}:destroy",
    response_model=SuccessEnvelope[WorkspaceDestroyResult],
    summary="完全销毁工作区(三方全删,完全不可逆)",
    description=(
        "三方全删:① 工作区目录(含工作区内 backups/ / data/novel.db / chroma/ "
        "等全部数据)② 顶级 archives/<id>.tar.gz(若存在)。对比 purge 仅删工作区"
        "目录保留 backups 与顶级 archive,destroy 完全不可逆 — 无任何 restore "
        "路径。body.confirm 必须等于 workspace_id(strict 二次确认)。"
        "WS publish workspace_destroyed。生产场景'写完一本书清磁盘'专用。"
    ),
)
async def destroy_workspace(
    workspace_id: str,
    body: WorkspaceDestroyIn,
    service: WorkspaceService = Depends(get_workspace_service),
) -> SuccessEnvelope[WorkspaceDestroyResult]:
    # 路由层 strict 二次确认前置(对齐 PurgeConfirmMismatch / RestoreConfirmMismatch
    # 同模式)— confirm 错误优先于 not found 检查,语义"操作不该执行的事前阻止"。
    if body.confirm != workspace_id:
        raise WorkspaceDestroyConfirmInvalid(
            f"请在 confirm 字段填入工作区 ID '{workspace_id}' 以确认完全销毁",
            details={"workspace_id": workspace_id, "got": body.confirm},
        )
    # 工作区不存在 → 404(对齐 plan §1 Part 4 测试 #4 + 启动指令字面"404
    # workspace 不存在")。service 抛 WorkspaceServiceError 翻 409 不够精确,
    # 路由层主动 preempt 抛 WorkspaceNotFound。
    workspace_dir = service.workspaces_root / workspace_id
    if not workspace_dir.is_dir():
        raise WorkspaceNotFound(
            f"工作区不存在: {workspace_id}",
            details={"workspace_id": workspace_id},
        )
    result = service.destroy(workspace_id, confirm=body.confirm)
    return SuccessEnvelope(
        data=WorkspaceDestroyResult(
            workspace_id=result.workspace_id,
            deleted_paths=result.deleted_paths,
        )
    )


@router.post(
    "/{workspace_id}:backup",
    response_model=SuccessEnvelope[BackupInfoOut],
    summary="立即手动备份工作区",
    description=(
        "全量备份到工作区内 backups/manual_<timestamp>.zip(db + outline + "
        "cards + volumes + assets + chapters + meta.json),原目录不动。"
        "P6-B-07 起替换 P1 既有 .tar.gz 路径为 .zip 结构化备份。"
    ),
)
async def backup_workspace(
    workspace_id: str,
    service: BackupService = Depends(get_backup_service),
) -> SuccessEnvelope[BackupInfoOut]:
    info = service.create_manual_backup(workspace_id)
    return SuccessEnvelope(
        data=BackupInfoOut(
            backup_id=info.backup_id,
            backup_type=info.backup_type,  # type: ignore[arg-type]
            workspace_id=info.workspace_id,
            created_at=info.created_at,
            chapter_id=info.chapter_id,
            schema_version=info.schema_version,
            engine_version=info.engine_version,
            file_path=info.file_path,
            size_bytes=info.size_bytes,
        )
    )


@router.post(
    "/{workspace_id}:restore",
    response_model=SuccessEnvelope[RestoreResultOut],
    summary="从备份恢复工作区(P6-B-07 起 .zip 模式)",
    description=(
        "从工作区内 backups/<backup_id>.zip 解压覆盖工作区。"
        "二次确认 body.confirm == workspace_id;原工作区先 pre_restore "
        "物理备份到 backups/pre_restore_<ts>.zip 后覆盖。schema_version "
        "不兼容 → RESTORE_VERSION_INCOMPATIBLE 422;backup_id 对应 zip "
        "不存在 → BACKUP_NOT_FOUND 404;zip 结构损坏 → BACKUP_INVALID_FORMAT 422。"
    ),
)
async def restore_workspace(
    workspace_id: str,
    body: RestoreIn,
    service: BackupService = Depends(get_backup_service),
) -> SuccessEnvelope[RestoreResultOut]:
    if body.confirm != workspace_id:
        raise RestoreConfirmMismatch(
            f"请在 confirm 字段填入工作区 ID '{workspace_id}' 以确认恢复",
            details={"workspace_id": workspace_id},
        )
    result = service.restore_from_backup(workspace_id, body.backup_id)
    return SuccessEnvelope(
        data=RestoreResultOut(
            workspace_id=result.workspace_id,
            backup_id=result.backup_id,
            pre_restore_backup_id=result.pre_restore_backup_id,
        )
    )


@router.get(
    "/{workspace_id}/backups",
    response_model=SuccessEnvelope[BackupListOut],
    summary="列出工作区备份",
    description=(
        "返回最近 30 份自动备份 + 全部手动备份(按 created_at 倒序),"
        "不含内部 pre_restore_*.zip。"
    ),
)
async def list_workspace_backups(
    workspace_id: str,
    service: BackupService = Depends(get_backup_service),
) -> SuccessEnvelope[BackupListOut]:
    backups = service.list_backups(workspace_id)
    return SuccessEnvelope(
        data=BackupListOut(
            workspace_id=workspace_id,
            backups=[
                BackupInfoOut(
                    backup_id=b.backup_id,
                    backup_type=b.backup_type,  # type: ignore[arg-type]
                    workspace_id=b.workspace_id,
                    created_at=b.created_at,
                    chapter_id=b.chapter_id,
                    schema_version=b.schema_version,
                    engine_version=b.engine_version,
                    file_path=b.file_path,
                    size_bytes=b.size_bytes,
                )
                for b in backups
            ],
        )
    )


@router.post(
    "/{workspace_id}:purge-backups",
    response_model=SuccessEnvelope[PurgeBackupsResultOut],
    summary="清空工作区全部备份(释放磁盘,v1.1 w3-s8 F1)",
    description=(
        "删除工作区 backups/ 下全部备份 zip(auto + manual + pre_restore)。"
        "prune 只削超额 auto,manual 永不自动清 → 本端点补'全清'入口供 UI 释放磁盘。"
        "二次确认 body.confirm == workspace_id;不匹配 → 422 "
        "PURGE_BACKUPS_CONFIRM_MISMATCH。返删除份数 + 释放字节。工作区不存在 → "
        "409 WORKSPACE_CONFLICT(对齐备份域其他端点)。**不可逆**,清空后无法 "
        "restore 历史备份。"
    ),
)
async def purge_workspace_backups(
    workspace_id: str,
    body: PurgeBackupsIn,
    service: BackupService = Depends(get_backup_service),
) -> SuccessEnvelope[PurgeBackupsResultOut]:
    if body.confirm != workspace_id:
        raise PurgeBackupsConfirmMismatch(
            f"请在 confirm 字段填入工作区 ID '{workspace_id}' 以确认清空全部备份",
            details={"workspace_id": workspace_id},
        )
    result = service.purge_backups(workspace_id)
    return SuccessEnvelope(
        data=PurgeBackupsResultOut(
            workspace_id=result.workspace_id,
            deleted_count=result.deleted_count,
            freed_bytes=result.freed_bytes,
        )
    )


@router.get(
    "/{workspace_id}/version-status",
    response_model=SuccessEnvelope[VersionStatusOut],
    summary="工作区版本兼容性检查(doctor)",
    description=(
        "返回 workspace meta.json 的 engine_version / schema_version 与"
        "当前引擎版本 + Alembic head revision_id 的比对结果。任一不匹配 "
        "migration_required=true,前端 / CLI 据此决定是否调 :migrate 端点。"
    ),
)
async def get_workspace_version_status(
    workspace_id: str,
    service: WorkspaceVersionService = Depends(get_workspace_version_service),
) -> SuccessEnvelope[VersionStatusOut]:
    status_data = service.get_version_status(workspace_id)
    return SuccessEnvelope(
        data=VersionStatusOut(
            workspace_id=status_data.workspace_id,
            engine_version_match=status_data.engine_version_match,
            schema_version_match=status_data.schema_version_match,
            current_engine=status_data.current_engine,
            workspace_engine=status_data.workspace_engine,
            current_schema=status_data.current_schema,
            workspace_schema=status_data.workspace_schema,
            migration_required=status_data.migration_required,
        )
    )


@router.post(
    "/{workspace_id}:migrate",
    response_model=SuccessEnvelope[MigrateResultOut],
    summary="数据迁移(pre-migration 备份 + alembic upgrade head)",
    description=(
        "对工作区执行受控数据迁移:① 二次确认 body.confirm == workspace_id;"
        "② 走 BackupService.create_manual_backup 做 pre-migration 备份"
        "(失败 → 500 WORKSPACE_MIGRATION_BACKUP_FAILED 迁移不开始);"
        "③ alembic upgrade head 应用到工作区 db;④ 更新 meta.json 的 "
        "engine_version / schema_version 字段;⑤ WS publish version_migrated。"
        "workspace 主版本与当前引擎跨级 → 422 WORKSPACE_VERSION_INCOMPATIBLE。"
    ),
)
async def migrate_workspace(
    workspace_id: str,
    body: MigrateIn,
    service: WorkspaceVersionService = Depends(get_workspace_version_service),
) -> SuccessEnvelope[MigrateResultOut]:
    result = service.migrate(workspace_id, body.confirm)
    return SuccessEnvelope(
        data=MigrateResultOut(
            workspace_id=result.workspace_id,
            pre_migration_backup_id=result.pre_migration_backup_id,
            from_engine_version=result.from_engine_version,
            to_engine_version=result.to_engine_version,
            from_schema_version=result.from_schema_version,
            to_schema_version=result.to_schema_version,
        )
    )


@router.post(
    "/{workspace_id}:unblock-all",
    response_model=SuccessEnvelope[WorkspaceUnblockAllOut],
    summary="批量 unblock workspace 内所有 invalidated 章节",
    description=(
        "把 ``chapters.status == 'invalidated'`` 的所有章节翻 ``drafting``。"
        "空状态(无 invalidated 章节)→ 200 + count=0 幂等(自决项 2,对齐 "
        "P3-B-12 batch 端点先例)。WS publish ``chapter_unblocked`` 每章。"
    ),
)
async def unblock_all_chapters(
    workspace_id: str,
    body: WorkspaceUnblockAllIn = Body(default_factory=WorkspaceUnblockAllIn),
    service: ChapterRewindService = Depends(get_chapter_rewind_service),
) -> SuccessEnvelope[WorkspaceUnblockAllOut]:
    _ = body
    result = service.unblock_all(workspace_id)
    return SuccessEnvelope(
        data=WorkspaceUnblockAllOut(
            workspace_id=result.workspace_id,
            unblocked_chapter_ids=result.unblocked_chapter_ids,
            unblocked_count=result.unblocked_count,
        )
    )


@router.post(
    "/{workspace_id}:reset-stale",
    response_model=SuccessEnvelope[WorkspaceResetStaleOut],
    summary="批量重置 workspace 内滞留非终态的中断章(v2 w1-s2 章自愈)",
    description=(
        "把进程中断时停在流程中间态(``reviewing`` / ``rewriting`` / "
        "``revising``)的章翻回 ``drafting``(版本历史等中间产物不删,"
        "下次 write 全流程自然覆盖)。``human_intervention`` / "
        "``invalidated`` 语义\"等人\"不在自愈范围;``drafting`` 本身是"
        "可写起点无需重置。**防误杀对账**:该 workspace 有 running "
        "pipeline run 或 pending/running 后台任务时跳过扫描(200 + "
        "``skipped_reason`` 标注,等任务结束后重试)。空扫描 → 200 + "
        "count=0 幂等(对齐 unblock-all 模式)。WS publish "
        "``chapter_stale_reset`` 每章。lifespan 启动时自动跑一轮同逻辑。"
    ),
)
async def reset_stale_chapters(
    workspace_id: str,
    body: WorkspaceResetStaleIn = Body(default_factory=WorkspaceResetStaleIn),
    service: ChapterRewindService = Depends(get_chapter_rewind_service),
) -> SuccessEnvelope[WorkspaceResetStaleOut]:
    _ = body
    result = service.reset_stale(workspace_id)
    return SuccessEnvelope(
        data=WorkspaceResetStaleOut(
            workspace_id=result.workspace_id,
            reset_chapter_ids=result.reset_chapter_ids,
            reset_count=result.reset_count,
            skipped_reason=result.skipped_reason,  # type: ignore[arg-type]
        )
    )
