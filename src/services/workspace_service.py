"""工作区生命周期服务(P1-B-14 / P7-B-07)。

对应 PROJECT_SPEC §9 工作区生命周期。本模块负责把"工作区"从无到有创建出来、
枚举、激活切换、查看元数据、清理运行时数据、归档、彻底删除、备份、恢复、
**完全销毁**(P7-B-07 起新增 ``destroy``)等动作的副作用集中在一处,API
路由(P1-B-18)与 CLI(P1-B-21)共用。

设计要点
--------

- ``WorkspacePaths``(P1-B-04)只算路径不做 IO;创建目录骨架的副作用全部
  落本模块的 ``create``。
- ``meta.json`` 读写走 ``src.core.workspace.meta`` 的 ``read_meta`` /
  ``write_meta``,**不直接拼路径**。
- ``_active`` 软链放在 ``workspaces_root / _active``,POSIX 相对软链(目标
  写工作区 ID,非绝对路径),整个 ``workspaces/`` 目录可整体迁移不破链。
- ``archive`` 写到**项目级** ``archives/<id>.tar.gz`` 目录(对应
  PROJECT_SPEC §2.4 项目根 ``archives/``);``backup`` 写到**工作区内**
  ``backups/<id>-<timestamp>.tar.gz``。两者语义不同,见各方法 docstring 第一句。
- ``destroy`` 是 ``purge`` 的"超集"语义(P7-B-07):purge 只删工作区目录
  保留顶级 archive;destroy 三方全删 — 工作区目录 + 工作区内 backups/
  (含在工作区目录内,删 root 即删 backups)+ 顶级 archives/<id>.tar.gz。
  生产场景"写完一本书完全清磁盘"专用。
- 失败统一抛 ``WorkspaceServiceError(Exception)``,P1-B-17 全局错误体系起来后
  升级为 ``ApiError`` 子类。
- ``ws_manager`` 可选注入(P7-B-07 起 destroy 需 publish ``workspace_destroyed``
  WS event,沿用 BackupService / ChapterRewindService 同模式;为 ``None``
  时 publish noop,CLI 走 None,API 走单例)。
"""

from __future__ import annotations

import os
import json
import shutil
import tarfile
from dataclasses import dataclass
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

import structlog

from src.api import task_registry
from src.api.websocket.channels import workspace_events_channel
from src.config import settings
from src.core.models.style_template import (
    STYLE_TEMPLATES,
    style_guide_filename,
)
from src.core.models.workspace import Workspace
from src.core.workspace.meta import MetaFileError, read_meta, write_meta
from src.core.workspace.workspace_paths import WorkspacePaths
from src.services.workspace_operation_guard import (
    WorkspaceOperationConflictError,
    ensure_workspace_quiescent,
    workspace_operation_lock,
)
from src.version import ENGINE_VERSION

_log = structlog.get_logger(__name__)

#: 引擎级风格指南模板目录(相对 ``settings.prompts_dir``);I1 引入。
_STYLE_GUIDE_TEMPLATES_SUBDIR = "style_guide_templates"
#: 写入工作区 ``prompts/`` 的风格指南文件名;Writer 运行时读取。
_WORKSPACE_STYLE_GUIDE_FILENAME = "style_guide.md"

_ACTIVE_LINK_NAME = "_active"
_ARCHIVE_SUFFIX = ".tar.gz"
_BACKUP_TIMESTAMP_FMT = "%Y%m%dT%H%M%SZ"
_RUNTIME_INITIALIZATION_MARKER = ".runtime_initialization_pending.json"


def _reset_vector_store_cache(workspace_id: str) -> None:
    """v1.0.x A2-01:lazy import + 调 ``api.deps.reset_vector_store_cache``。

    workspace_service 模块顶层不能直接 ``from src.api.deps import ...``
    (deps.py 同时 import WorkspaceService 触发循环);函数内 lazy import 在
    运行时模块已初始化,无 ImportError。

    deps 模块导入异常 / cache 不存在(理论上不会触发,deps.py 必先加载)→
    swallow + log,避免阻塞 destroy / purge / archive / restore 主流程。
    """
    try:
        from src.api.deps import reset_vector_store_cache

        reset_vector_store_cache(workspace_id)
    except Exception as exc:  # noqa: BLE001
        _log.warning(
            "vector_store_cache_reset_failed",
            workspace_id=workspace_id,
            error_type=type(exc).__name__,
            error_message=str(exc),
        )


class WorkspaceServiceError(Exception):
    """工作区生命周期操作失败时抛出。

    P1-B-17 全局错误体系起来后会升级为 ``ApiError`` 子类。
    """


class WorkspaceDestroyConfirmInvalidError(WorkspaceServiceError):
    """``destroy`` 二次确认字段(``confirm``)与 ``workspace_id`` 不匹配。

    P7-B-07 引入。路由层 / CLI 已先做相同前置校验直接抛
    ``WorkspaceDestroyConfirmInvalid`` ApiError;service 内部再做一次防御性
    校验,触发表示有 caller 绕过 confirm 校验直接调 service。translation 表
    把本异常翻成 422 ``WORKSPACE_DESTROY_CONFIRM_INVALID``。
    """

    def __init__(self, workspace_id: str, got: str) -> None:
        self.workspace_id = workspace_id
        self.got = got
        super().__init__(
            f"destroy 二次确认不匹配: workspace_id={workspace_id!r}, confirm={got!r}"
        )


@dataclass(frozen=True)
class WorkspaceDestroyResult:
    """``destroy`` 出参(P7-B-07)。``deleted_paths`` 记录实际删除的路径列表
    供前端 / CLI 回显 + 审计;路径用绝对字符串(``str(Path)``,对齐
    ``archive_path`` 字段同模式)。

    可能含 0-2 项:
    - 工作区目录(``paths.root``,含工作区内 backups/);若 destroy 前不存在
      则 service 直接抛 not found(不进入本 result)
    - 顶级 archive(``archives/<id>.tar.gz``);若不存在则不计入 deleted_paths
    """

    workspace_id: str
    deleted_paths: list[str]


class WorkspaceService:
    """工作区生命周期管理器(create / list / switch / info / clean / archive /
    purge / backup / restore / destroy)。

    实例化时绑定 ``workspaces_root`` 与 ``archives_root``;构造参数缺省都从
    ``settings.data_dir`` 推算,允许测试用 ``tmp_path`` 显式注入。

    ``ws_manager`` 可选注入(P7-B-07):为 ``None`` 时 WS publish noop;API
    路由路径走单例,CLI 路径走 ``None``。沿用 BackupService / ChapterRewindService
    同模式。
    """

    def __init__(
        self,
        *,
        workspaces_root: Path | None = None,
        archives_root: Path | None = None,
        ws_manager: object | None = None,
    ) -> None:
        root = workspaces_root if workspaces_root is not None else settings.data_dir
        self._workspaces_root = Path(root).resolve()
        if archives_root is not None:
            self._archives_root = Path(archives_root).resolve()
        else:
            # 项目级 archives/ 与 workspaces/ 同级(PROJECT_SPEC §2.4)。
            self._archives_root = self._workspaces_root.parent / "archives"
        self._ws_manager = ws_manager

    # ---- 公共只读属性 ------------------------------------------------------

    @property
    def workspaces_root(self) -> Path:
        """工作区根目录(只读;供 ChapterService 等同源构造 WorkspacePaths 用)。

        构造时已 ``.resolve()``,返回的是绝对路径。
        """
        return self._workspaces_root

    def archive_file_for(self, workspace_id: str) -> Path:
        """归档文件位置(``archives/<id>.tar.gz``)。

        路由层 ``:restore`` 前置检查 ``ArchiveNotFound`` 用;不读不写,只算路径。
        与内部辅助 ``_archive_file`` 同实现,公开方法只是把"路径计算"语义稳定
        到 API 边界。
        """
        return self._archive_file(workspace_id)

    # ---- 内部辅助 ----------------------------------------------------------

    def _paths(self, workspace_id: str) -> WorkspacePaths:
        return WorkspacePaths(workspace_id, workspaces_root=self._workspaces_root)

    def _archive_file(self, workspace_id: str) -> Path:
        return self._archives_root / f"{workspace_id}{_ARCHIVE_SUFFIX}"

    @staticmethod
    def _now() -> datetime:
        return datetime.now(timezone.utc)

    @staticmethod
    def _create_skeleton(paths: WorkspacePaths) -> None:
        """一次性 mkdir 出工作区运行时目录(含 P2+ 引入的 ``chroma/``)。

        v1.0.x wave 5 session 1:加 ``paths.chroma_dir``。原注释"P1 范围内
        目录"在 P2 引入 Chroma 后未同步,导致新工作区不建 ``data/chroma/``
        子目录,健康检查 ``workspace_chroma_readwrite`` 探针对此路径
        ``not exists`` 直接 fail(luo2 等新工作区在未走过任何向量写入路径
        前命中)。修法:创建工作区时一并建 ``chroma_dir``,与
        ``_check_workspace_chroma_readwrite`` 的"目录不存在 → 顺便初始化"
        防御层共同保证"新工作区 + 老工作区"两条路径都不再误报。
        """

        for directory in (
            paths.root,
            paths.prompts_dir,
            paths.data_dir,
            paths.chapters_dir,
            paths.cards_dir,
            paths.outline_dir,
            paths.exports_dir,
            paths.archives_dir,
            paths.backups_dir,
            paths.logs_dir,
            paths.chroma_dir,
        ):
            directory.mkdir(parents=True, exist_ok=True)

    def _set_active(self, workspace_id: str) -> None:
        """更新 ``_active`` 软链指向 ``workspace_id``。

        相对软链:目标只写工作区目录名,不写绝对路径,迁移整 workspaces/ 不破。
        已存在则先 unlink 重建(``symlink_to`` 不支持覆盖)。
        """

        self._workspaces_root.mkdir(parents=True, exist_ok=True)
        link_path = self._workspaces_root / _ACTIVE_LINK_NAME
        if link_path.is_symlink() or link_path.exists():
            link_path.unlink()
        os.symlink(workspace_id, link_path)

    # ---- 9 个生命周期方法 --------------------------------------------------

    def create(
        self,
        workspace_id: str,
        *,
        display_name: str,
        genre: str,
        premise: str,
        total_chapters: int = 0,
        default_voice: str = "third_person",
        style_template: str | None = None,
        activate: bool = True,
    ) -> Workspace:
        """创建一个新工作区,落目录骨架与初始 ``meta.json``。

        ``activate=True`` 时(默认)同步把 ``_active`` 软链指过去,等价于 CLI
        ``novel workspace create`` 的复合语义;批量导入或测试场景请显式传
        ``activate=False`` 解耦顺序副作用。

        ``default_voice``(v1.1 wave 3 s4 / L2)写入 ``Workspace.default_voice``
        (meta.json),后续 ORM upsert 同步,G10 ``cross_chapter_voice_validator``
        据此判定首章 / 兜底人称。3 档:``third_person``(项目级默认)/
        ``first_person`` / ``free``(关闭跨章节视角硬约束,只 warn)。

        ``style_template``(v1.1 wave 4 s3 / I1)写入 ``Workspace.style_template``
        (meta.json),并据所选模板把 ``prompts/style_guide_templates/<name>.md``
        正文拷入工作区 ``prompts/style_guide.md``,Writer 运行时读取注入 prompt。
        ``None``(默认)或非法值不生成文件(Writer 退化为既有隐式风格控制)。

        - 工作区目录已存在 → ``WorkspaceServiceError``(避免覆盖既有数据)
        - ``status`` 初始化为 ``"active"``,``engine_version`` 写当前
          ``ENGINE_VERSION``,``created_at`` / ``last_active_at`` 都用 UTC now
        """

        paths = self._paths(workspace_id)
        if paths.root.exists():
            raise WorkspaceServiceError(f"工作区已存在,拒绝覆盖: {paths.root}")

        # v1.0.x wave 6 s2.6a(2026-05-25):清除可能残留的销毁标记,
        # 允许同 ID 工作区在 destroy/purge/archive 后重新创建。
        task_registry.clear_workspace_destroyed_mark(workspace_id)

        self._create_skeleton(paths)

        # 函数级 import 避开 src.services.workspace_version_service ↔
        # src.services.backup_service ↔ src.services.workspace_service 三角
        # 依赖在模块顶层 import 时的潜在循环(P6-B-08 引入)。
        from src.services.workspace_version_service import (
            get_current_schema_version,
        )

        now = self._now()
        workspace = Workspace(
            workspace_id=workspace_id,
            display_name=display_name,
            genre=genre,
            premise=premise,
            created_at=now,
            last_active_at=now,
            engine_version=ENGINE_VERSION,
            schema_version=get_current_schema_version(),
            status="active",
            total_chapters=total_chapters,
            completed_chapters=0,
            config_overrides={},
            default_voice=default_voice,  # type: ignore[arg-type]
            style_template=style_template,  # type: ignore[arg-type]
        )
        write_meta(paths.meta_json, workspace)
        (paths.root / _RUNTIME_INITIALIZATION_MARKER).write_text(
            json.dumps(
                {
                    "workspace_id": workspace_id,
                    "created_at": now.isoformat(),
                },
                ensure_ascii=False,
                sort_keys=True,
            ),
            encoding="utf-8",
        )

        # I1:据所选风格模板生成工作区 prompts/style_guide.md(Writer 运行时读取)。
        self._write_style_guide(paths, style_template)

        if activate:
            self._set_active(workspace_id)

        # P1-B-24:OBSERVABILITY §2.4 `workspace_lifecycle` 事件。
        _log.info(
            "workspace_lifecycle",
            action="create",
            workspace_id=workspace_id,
            engine_version=ENGINE_VERSION,
            activated=activate,
        )

        return workspace

    @staticmethod
    def _write_style_guide(paths: WorkspacePaths, style_template: str | None) -> None:
        """据所选风格模板把引擎级模板正文写入工作区 ``prompts/style_guide.md``。

        I1 引入。``style_template`` 为 ``None`` / 非法 / 模板文件缺失时**静默跳过**
        (不写文件,Writer 退化为既有隐式风格控制)——风格模板是可选增强,
        不应因模板缺失阻断工作区创建。``prompts/`` 目录已由 ``_create_skeleton``
        建好。
        """

        if style_template is None or style_template not in STYLE_TEMPLATES:
            return

        src_path = (
            settings.prompts_dir.resolve()
            / _STYLE_GUIDE_TEMPLATES_SUBDIR
            / style_guide_filename(style_template)
        )
        try:
            content = src_path.read_text(encoding="utf-8")
        except OSError:
            _log.warning(
                "style_guide_template_missing",
                workspace_id=paths.workspace_id,
                style_template=style_template,
                template_path=str(src_path),
            )
            return

        dest_path = paths.prompts_dir / _WORKSPACE_STYLE_GUIDE_FILENAME
        dest_path.write_text(content, encoding="utf-8")
        _log.info(
            "style_guide_generated",
            workspace_id=paths.workspace_id,
            style_template=style_template,
        )

    def list(self) -> list[Workspace]:
        """枚举所有合法工作区。

        以"目录下存在合法 ``meta.json``"为唯一判定;不按名字过滤 ``_active``
        软链(软链解析后会跟到真目录,真目录已被独立扫到,不会重复)。
        ``meta.json`` 缺失或损坏的子目录直接跳过(P1 简版不报警,P1-B-17 起
        可加日志)。
        """

        if not self._workspaces_root.exists():
            return []

        result: list[Workspace] = []
        seen: set[str] = set()
        for entry in sorted(self._workspaces_root.iterdir()):
            # 软链解析后落到的真目录会被独立扫到,跳过软链本身防重复。
            if entry.is_symlink():
                continue
            if not entry.is_dir():
                continue
            meta_path = entry / "meta.json"
            if not meta_path.is_file():
                continue
            try:
                workspace = read_meta(meta_path)
            except MetaFileError:
                continue
            if workspace.workspace_id in seen:
                continue
            seen.add(workspace.workspace_id)
            result.append(workspace)
        return result

    def switch(self, workspace_id: str) -> Workspace:
        """把 ``_active`` 软链指向 ``workspace_id``,并刷新 ``last_active_at``。

        - 工作区不存在 → ``WorkspaceServiceError``
        """

        try:
            with workspace_operation_lock(self._workspaces_root, workspace_id):
                return self._switch_unlocked(workspace_id)
        except WorkspaceOperationConflictError as exc:
            raise WorkspaceServiceError(str(exc)) from exc

    def _switch_unlocked(self, workspace_id: str) -> Workspace:
        """在 workspace maintenance guard 内完成 meta RMW 与 active 切换。"""

        paths = self._paths(workspace_id)
        if not paths.meta_json.is_file():
            raise WorkspaceServiceError(f"工作区不存在或 meta.json 缺失: {paths.root}")

        workspace = read_meta(paths.meta_json)
        workspace = workspace.model_copy(update={"last_active_at": self._now()})
        write_meta(paths.meta_json, workspace)

        self._set_active(workspace_id)
        return workspace

    def info(self, workspace_id: str) -> Workspace:
        """读取 ``meta.json`` 返回 ``Workspace``。

        ``MetaFileError`` 直接透传(已带清晰中文消息)。
        """

        paths = self._paths(workspace_id)
        return read_meta(paths.meta_json)

    def clean(self, workspace_id: str) -> None:
        """在富记忆/数据库统一维护锁内清理运行时目录。"""

        try:
            with workspace_operation_lock(self._workspaces_root, workspace_id):
                self._ensure_destructive_quiescent(workspace_id)
                self._clean_unlocked(workspace_id)
        except WorkspaceOperationConflictError as exc:
            raise WorkspaceServiceError(str(exc)) from exc

    def _clean_unlocked(self, workspace_id: str) -> None:
        """清理工作区**内**运行时数据,保留正文 / 大纲 / 人设 / SQLite db。

        删除范围:``data/chroma/``、工作区内 ``archives/``(注意:这是
        prompts/responses 归档目录,**不是**项目级 ``archives/<id>.tar.gz``
        所在的顶级目录)、``logs/``。其余目录(chapters / cards / outline /
        exports / prompts / backups)与 ``data/novel.db`` 保留。

        - 工作区不存在 → ``WorkspaceServiceError``
        - 子目录不存在静默跳过(``shutil.rmtree(..., ignore_errors=True)``
          不用,因为我们要在确实存在时彻底删,不存在时不报错)
        """

        paths = self._paths(workspace_id)
        if not paths.root.is_dir():
            raise WorkspaceServiceError(f"工作区不存在: {paths.root}")

        for runtime_dir in (paths.chroma_dir, paths.archives_dir, paths.logs_dir):
            if runtime_dir.is_dir():
                shutil.rmtree(runtime_dir)
                runtime_dir.mkdir(parents=True, exist_ok=True)

        _log.info(
            "workspace_lifecycle",
            action="clean",
            workspace_id=workspace_id,
        )

    def archive(self, workspace_id: str) -> Path:
        """在统一维护锁内归档并删除工作区。"""

        try:
            with workspace_operation_lock(self._workspaces_root, workspace_id):
                self._ensure_destructive_quiescent(workspace_id)
                return self._archive_unlocked(workspace_id)
        except WorkspaceOperationConflictError as exc:
            raise WorkspaceServiceError(str(exc)) from exc

    def _archive_unlocked(self, workspace_id: str) -> Path:
        """把工作区打包到**项目级** ``archives/<id>.tar.gz`` 后删除原目录。

        注意目标位置是项目根的 ``archives/``(PROJECT_SPEC §2.4),与工作区
        内的 ``archives/``(prompts/responses 归档目录)语义不同,**不要混淆**。

        归档前先把 ``meta.status`` 改成 ``"archived"`` 落盘,再整体打包,
        最后 ``rmtree`` 原目录。

        - 工作区不存在 → ``WorkspaceServiceError``
        - 顶级 ``archives/<id>.tar.gz`` 已存在 → ``WorkspaceServiceError``
          (P1 简版,不做版本化;P6 完整流程再升)
        """

        paths = self._paths(workspace_id)
        if not paths.root.is_dir():
            raise WorkspaceServiceError(f"工作区不存在: {paths.root}")

        archive_file = self._archive_file(workspace_id)
        if archive_file.exists():
            raise WorkspaceServiceError(
                f"归档文件已存在,拒绝覆盖: {archive_file}(P1 简版不版本化)"
            )

        # 1. 先把 meta.status 改为 archived,落盘
        workspace = read_meta(paths.meta_json)
        workspace = workspace.model_copy(update={"status": "archived"})
        write_meta(paths.meta_json, workspace)

        # 2. 整体打包到顶级 archives/
        self._archives_root.mkdir(parents=True, exist_ok=True)
        with tarfile.open(archive_file, "w:gz") as tar:
            tar.add(paths.root, arcname=workspace_id)

        # v1.0.x wave 6 s2.6a(2026-05-25):mark workspace 销毁,后台跑的 BackgroundTask
        # 在入口查 task_registry.is_workspace_destroyed 命中 → early-return,避免 outline
        # writer / chapter_card writer 等写盘路径在 rmtree 后 mkdir 重建子目录残留。
        # 详 task_registry.mark_workspace_destroyed docstring + impl 文档 §race condition。
        task_registry.mark_workspace_destroyed(workspace_id)
        # J1(w4-s6):级联取消该工作区所有 in-flight 任务的 LLM 调用 → 尽早中断省
        # token(而非仅靠 writer 层 paths.root.exists 被动失败)。mark(防新任务入口)
        # + cancel(中断在跑任务)双管。
        task_registry.cancel_workspace_tasks(workspace_id)

        # 3. B-02 hot-fix(2026-05-19):先打日志再 rmtree(避免 per-workspace
        # logger 重建 workspaces/<id>/logs/ 目录),旧实现的"_active 软链可保留
        # switch 时覆盖"假设在实战中导致 dangling 软链引发 list/info 异常,
        # 改为同步清理。详 destroy 同位置注释。
        _log.info(
            "workspace_lifecycle",
            action="archive",
            workspace_id=workspace_id,
            archive_file=str(archive_file),
        )
        shutil.rmtree(paths.root)
        self._clear_dangling_active_link(workspace_id)
        _reset_vector_store_cache(workspace_id)

        return archive_file

    def purge(self, workspace_id: str, *, confirm: bool = False) -> None:
        """在统一维护锁内彻底删除工作区目录。"""

        if not confirm:
            raise WorkspaceServiceError(
                "purge 是不可逆操作,必须传 confirm=True 才能执行"
            )
        try:
            with workspace_operation_lock(self._workspaces_root, workspace_id):
                self._ensure_destructive_quiescent(workspace_id)
                self._purge_unlocked(workspace_id, confirm=confirm)
        except WorkspaceOperationConflictError as exc:
            raise WorkspaceServiceError(str(exc)) from exc

    def _purge_unlocked(
        self,
        workspace_id: str,
        *,
        confirm: bool = False,
    ) -> None:
        """彻底删除工作区目录(不动顶级 archives 与工作区内 backups 备份)。

        - ``confirm=False`` → ``WorkspaceServiceError``(强制二次确认)
        - 工作区不存在 → ``WorkspaceServiceError``
        - 留存项目级 ``archives/<id>.tar.gz``(若存在),用户可 restore
        """

        if not confirm:
            raise WorkspaceServiceError(
                "purge 是不可逆操作,必须传 confirm=True 才能执行"
            )

        paths = self._paths(workspace_id)
        if not paths.root.is_dir():
            raise WorkspaceServiceError(f"工作区不存在: {paths.root}")

        # v1.0.x wave 6 s2.6a(2026-05-25):mark workspace 销毁(BackgroundTask race 防御,
        # 同 destroy / archive)。详 task_registry.mark_workspace_destroyed docstring。
        task_registry.mark_workspace_destroyed(workspace_id)
        # J1(w4-s6):级联取消该工作区 in-flight 任务的 LLM 调用(同 destroy / archive)。
        task_registry.cancel_workspace_tasks(workspace_id)

        # B-02 hot-fix(2026-05-19):同 destroy — 先打日志再 rmtree,避免
        # per-workspace logger 重建被删目录(详 destroy 同位置注释)。
        _log.warning(
            "workspace_lifecycle",
            action="purge",
            workspace_id=workspace_id,
        )

        shutil.rmtree(paths.root)
        self._clear_dangling_active_link(workspace_id)
        _reset_vector_store_cache(workspace_id)

    def backup(self, workspace_id: str) -> Path:
        """复制一份工作区快照到**工作区内** ``backups/<id>-<timestamp>.tar.gz``。

        与 ``archive`` 区别:
          - ``archive``:工作区不再活跃,目标是**项目级 archives/**,**删除原目录**
          - ``backup``:工作区仍活跃,目标是**工作区内 backups/**,**原目录不动**

        时间戳格式 ``YYYYMMDDTHHMMSSZ``(紧凑、无 ``:`` / ``+``,跨平台兼容
        Windows / iCloud / S3),保证同秒以下不冲突;调用方需自行节流。

        - 工作区不存在 → ``WorkspaceServiceError``
        """

        paths = self._paths(workspace_id)
        if not paths.root.is_dir():
            raise WorkspaceServiceError(f"工作区不存在: {paths.root}")

        paths.backups_dir.mkdir(parents=True, exist_ok=True)
        timestamp = self._now().strftime(_BACKUP_TIMESTAMP_FMT)
        backup_file = paths.backups_dir / f"{workspace_id}-{timestamp}{_ARCHIVE_SUFFIX}"

        with tarfile.open(backup_file, "w:gz") as tar:
            tar.add(paths.root, arcname=workspace_id)

        _log.info(
            "workspace_lifecycle",
            action="backup",
            workspace_id=workspace_id,
            backup_file=str(backup_file),
        )

        return backup_file

    def restore(self, workspace_id: str) -> Workspace:
        """在统一维护锁内从项目级 archive 恢复工作区。"""

        try:
            with workspace_operation_lock(self._workspaces_root, workspace_id):
                self._ensure_destructive_quiescent(workspace_id)
                return self._restore_unlocked(workspace_id)
        except WorkspaceOperationConflictError as exc:
            raise WorkspaceServiceError(str(exc)) from exc

    def _restore_unlocked(self, workspace_id: str) -> Workspace:
        """从项目级 ``archives/<id>.tar.gz`` 解出工作区目录。

        解压后改 ``meta.status="active"`` 与 ``last_active_at=now``,落盘并
        返回 ``Workspace``。**不自动 switch**;调用方决定是否激活。

        - 归档文件不存在 → ``WorkspaceServiceError``
        - 工作区目录已存在 → ``WorkspaceServiceError``(避免覆盖现有数据)
        """

        archive_file = self._archive_file(workspace_id)
        if not archive_file.is_file():
            raise WorkspaceServiceError(f"归档文件不存在: {archive_file}")

        paths = self._paths(workspace_id)
        if paths.root.exists():
            raise WorkspaceServiceError(f"工作区目录已存在,拒绝覆盖: {paths.root}")

        self._workspaces_root.mkdir(parents=True, exist_ok=True)
        with tarfile.open(archive_file, "r:gz") as tar:
            # filter="data": 拒绝绝对路径 / 父级穿越 / 设备文件,Python 3.12 起
            # 是默认值,3.11 显式传同样安全。
            tar.extractall(self._workspaces_root, filter="data")

        # v1.0.x wave 6 s2.6a(2026-05-25):清除可能残留的销毁标记,
        # 允许归档恢复后新任务正常跑(同 create 路径同型)。
        task_registry.clear_workspace_destroyed_mark(workspace_id)

        # v1.0.x A2-01:工作区目录从归档解出后,缓存的旧 VectorStore(若存在)
        # 引用的 chroma 路径在 archive 阶段已被 rmtree → 现在路径重生但 ORM
        # SystemClient 仍指向旧实例,清缓存让下次 factory 调用按新文件重建。
        _reset_vector_store_cache(workspace_id)

        workspace = read_meta(paths.meta_json)
        workspace = workspace.model_copy(
            update={"status": "active", "last_active_at": self._now()}
        )
        write_meta(paths.meta_json, workspace)

        _log.info(
            "workspace_lifecycle",
            action="restore",
            workspace_id=workspace_id,
            archive_file=str(archive_file),
        )

        return workspace

    def sync_completed_chapters_to_meta(
        self,
        workspace_id: str,
        delta: int,
    ) -> None:
        """v1.0.x A2-03(2026-05-19):把 ``completed_chapters`` 增量同步写回
        ``meta.json``。

        轮 152 A-03 hot-fix 只修了 SQLite ORM(``WorkspaceORM.completed_chapters``)
        但 ``WorkspaceService.list_workspaces`` 通过 ``read_meta`` 读 meta.json
        渲染前端工作区列表 → ORM=N / meta.json=0 永久分裂,前端进度条永远 0/M。
        本 helper 在 caller(chapter_service / chapter_rewind_service)ORM 改完
        + commit 后调,把 delta 同步落 meta.json,保持 ORM 与 meta.json 两源
        一致。

        - ``delta > 0``:approve / force-pass 时 +1
        - ``delta < 0``:rewind 时减(可能 -N,N = target_approved_loss +
          subsequent_approved_loss)
        - 结果 floor 0(不下溢,即使 meta.json 滞后于 ORM)
        - 原子写(沿用 ``write_meta`` tmp + ``Path.replace``)
        - meta.json 不存在 / 损坏 / IO 错 → swallow + log,**不抛**(caller
          的 ORM commit 已成功落库,meta 失败只是镜像滞后,可由 backfill 脚本修)

        :param workspace_id: 工作区 ID
        :param delta: completed_chapters 的增量(正负皆可,0 静默 no-op)
        """
        if delta == 0:
            return
        paths = self._paths(workspace_id)
        if not paths.meta_json.is_file():
            _log.warning(
                "sync_completed_chapters_to_meta_skipped",
                workspace_id=workspace_id,
                reason="meta_json_missing",
                delta=delta,
            )
            return
        try:
            workspace = read_meta(paths.meta_json)
            new_count = max(0, workspace.completed_chapters + delta)
            updated = workspace.model_copy(update={"completed_chapters": new_count})
            write_meta(paths.meta_json, updated)
        except Exception as exc:  # noqa: BLE001
            _log.warning(
                "sync_completed_chapters_to_meta_failed",
                workspace_id=workspace_id,
                delta=delta,
                error_type=type(exc).__name__,
                error_message=str(exc),
            )

    def _clear_dangling_active_link(self, workspace_id: str) -> None:
        """B-02 hot-fix(2026-05-19):若 ``_active`` 软链指向刚被 destroy/purge/
        archive 的工作区,把它清掉,避免后续 list/info/recreate 撞 dangling
        target。

        非软链 / 指向其他工作区 / 软链不存在 → noop。
        """
        link_path = self._workspaces_root / _ACTIVE_LINK_NAME
        if not link_path.is_symlink():
            return
        try:
            target = os.readlink(link_path)
        except OSError:
            return
        if target == workspace_id:
            try:
                link_path.unlink()
            except OSError:
                # 并发清理时可能已被删,swallow
                pass

    def _ensure_destructive_quiescent(self, workspace_id: str) -> None:
        """删/换工作区持久状态前确认普通 Worker、pipeline 与租约均静默。"""

        ensure_workspace_quiescent(
            workspace_id=workspace_id,
            db_path=self._paths(workspace_id).db_path,
            active_task_checker=task_registry.has_active_tasks,
            check_running_pipeline=True,
        )

    def destroy(
        self,
        workspace_id: str,
        *,
        confirm: str,
    ) -> WorkspaceDestroyResult:
        """在统一维护锁内永久销毁工作区与顶级 archive。"""

        if confirm != workspace_id:
            raise WorkspaceDestroyConfirmInvalidError(workspace_id, confirm)
        try:
            with workspace_operation_lock(self._workspaces_root, workspace_id):
                self._ensure_destructive_quiescent(workspace_id)
                return self._destroy_unlocked(
                    workspace_id,
                    confirm=confirm,
                )
        except WorkspaceOperationConflictError as exc:
            raise WorkspaceServiceError(str(exc)) from exc

    def _destroy_unlocked(
        self,
        workspace_id: str,
        *,
        confirm: str,
    ) -> WorkspaceDestroyResult:
        """**完全销毁工作区**(P7-B-07,生产场景"写完一本书清磁盘"专用)。

        三方全删:
          1. ``shutil.rmtree(paths.root)`` — 工作区目录(含工作区内 backups/ /
             data/novel.db / chroma/ 等全部数据)
          2. ``archive_file.unlink(missing_ok=True)`` — 顶级
             ``archives/<id>.tar.gz``(若曾 archive 过则存在)

        语义对比 ``purge``:purge 仅删工作区目录,**保留**顶级 archive
        (用户可后续 restore);destroy 三方全删,**完全不可逆**(无任何
        restore 路径)。

        参数 ``confirm``:必须等于 ``workspace_id`` 完整字符串(强 strict 二次
        确认,对齐 ``BackupService.restore`` + ``WorkspaceVersionService.migrate``
        同模式)。

        - confirm 不等于 workspace_id → ``WorkspaceDestroyConfirmInvalidError``
        - 工作区目录不存在 → ``WorkspaceServiceError``(即使顶级 archive 存在
          也报 not found — destroy 的 SSOT 是工作区目录,纯 archive 残留属用户
          应直接 shell 操作的场景,plan §1 Part 4 测试 #4 字面 + §2 决策 5)
        - 工作区目录存在但 archive 不存在 → 只删工作区目录(deleted_paths 1 项)

        返回 ``WorkspaceDestroyResult(workspace_id, deleted_paths=[...])``
        记录实际删除的绝对路径字符串列表。

        WS event:``workspace.{workspace_id}.events`` 频道发 ``workspace_destroyed``
        (data = ``{workspace_id, deleted_paths, deleted_at}``)— 前端接到后可
        清本地 workspace 列表项 + toast。publish 失败 swallow + structlog
        (对齐 BackupService ``_publish_workspace_event`` 同型)。
        """

        if confirm != workspace_id:
            raise WorkspaceDestroyConfirmInvalidError(workspace_id, confirm)

        paths = self._paths(workspace_id)
        if not paths.root.is_dir():
            raise WorkspaceServiceError(f"工作区不存在: {paths.root}")

        # B-02 hot-fix(2026-05-19):预先计算将删的路径,**先打日志再 rmtree**。
        # 旧实现 rmtree 之后才打 _log.warning(workspace_id=...) → structlog 的
        # WorkspaceAwareJSONLHandler 检测 workspace_id 字段后,写日志会调
        # ``target_dir.mkdir(parents=True, exist_ok=True)`` 重建刚被 rmtree 的
        # ``workspaces/<id>/logs/`` 目录 → ``paths.root`` 在 destroy 返回时
        # 仍然存在(只剩 logs/ 子目录)→ 用户重提同 ID 撞 WORKSPACE_CONFLICT 409。
        # 修法:① 把日志调用前置到 rmtree 之前;② 同步清理可能 dangling 的
        # ``_active`` 软链(避免后续 list/info 撞坏链)。
        deleted_paths: list[str] = [str(paths.root)]
        archive_file = self._archive_file(workspace_id)
        if archive_file.is_file():
            deleted_paths.append(str(archive_file))

        now = self._now()
        # v1.0.x wave 6 s2.6a(2026-05-25):mark workspace 销毁,后台跑的 BackgroundTask
        # 在入口查 is_workspace_destroyed 命中 → early-return,避免 outline writer 等
        # 写盘路径在 rmtree 后 mkdir 重建子目录残留(详 task_registry.mark_workspace_destroyed
        # docstring + impl §race condition)。mark 在 _log.warning 之前(rmtree 之前 + log 之前)
        # 双层保险:即使 logger 仍有 mkdir 子目录 race 漏洞,任务也已早退。
        task_registry.mark_workspace_destroyed(workspace_id)
        # J1(w4-s6):级联取消该工作区 in-flight 任务的 LLM 调用 → 尽早中断省 token
        # (而非仅靠 writer 层 paths.root.exists 被动失败)。
        task_registry.cancel_workspace_tasks(workspace_id)

        # workspace_destroyed 警告级日志(对齐 P1-B-14 purge 同型 + 不可逆性升级)
        # —— 必须在 rmtree 之前打,避免 per-workspace logger 重建目录。
        _log.warning(
            "workspace_lifecycle",
            action="destroy",
            workspace_id=workspace_id,
            deleted_paths=deleted_paths,
        )

        # 1. 删工作区目录(含工作区内 backups/ / data/novel.db / chroma/ 等)
        shutil.rmtree(paths.root)

        # 2. 删顶级 archive(若存在)
        if archive_file.is_file():
            archive_file.unlink()

        # 3. 清 dangling _active 软链(若指向被删工作区)
        self._clear_dangling_active_link(workspace_id)

        # 4. v1.0.x A2-01:清 VectorStore 缓存(chroma/ 路径已删,缓存陈旧)
        _reset_vector_store_cache(workspace_id)

        # WS publish(顺序:发完 event 再返回结果,避免前端列表残留)
        self._publish_workspace_event(
            workspace_id=workspace_id,
            event="workspace_destroyed",
            data={
                "workspace_id": workspace_id,
                "deleted_paths": deleted_paths,
                "deleted_at": now.isoformat(),
            },
        )

        return WorkspaceDestroyResult(
            workspace_id=workspace_id,
            deleted_paths=deleted_paths,
        )

    # ---- WS publish 辅助(P7-B-07) ----------------------------------------

    def _publish_workspace_event(
        self,
        *,
        workspace_id: str,
        event: str,
        data: dict[str, Any],
    ) -> None:
        """WS publish 到 ``workspace.{workspace_id}.events``;失败 swallow + log。

        对齐 ``BackupService._publish_workspace_event`` 同型(structlog ``event``
        首参冲突 → 业务 event 改名 ``ws_event``)。P7-B-07 起 ``destroy`` 用。
        """
        if self._ws_manager is None:
            return
        try:
            self._ws_manager.publish(  # type: ignore[attr-defined]
                channel=workspace_events_channel(workspace_id),
                event=event,
                data=data,
            )
        except Exception as exc:  # noqa: BLE001
            _log.error(
                "workspace_event_notify_failed",
                workspace_id=workspace_id,
                ws_event=event,
                error_type=type(exc).__name__,
                error_message=str(exc),
            )
