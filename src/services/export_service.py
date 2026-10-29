"""导出引擎业务层(P6-B-04 md / chaptered_md + 异步任务)。

对齐 plan §"P6-B-04" + 决策 5=A(导出格式 4 档,本任务只实现 md /
chaptered_md;epub / docx 留 P6-B-05/06)+ §"P6 端点表"10/11/12。

设计要点
========

1. **3 端点对应 service 方法**:

   - ``create_export(workspace_id, ...)`` — :create 端点入口;校验 scope +
     生成 ``export_id`` + 落 manifest.json(``status=pending``)+ 返
     ``(task_id, export_id)``。
   - ``execute(workspace_id, *, task_id, export_id, ...)`` — 后台异步任务
     入口(由路由层 BackgroundTasks 调用);跑 collecting → rendering →
     packaging → done 4 stage。
   - ``list_exports(workspace_id)`` — :list 端点入口;扫
     ``exports/`` 目录返最近 N 条 manifest。
   - ``get_download_path(workspace_id, export_id)`` — :download 端点入口;
     返 zip 物理路径(由 FastAPI ``FileResponse`` 流式响应)。

2. **物理布局**(对齐 plan §"P6-B-04" 字面 ``workspaces/{id}/exports/{export_id}/``):

   ```
   workspaces/{id}/exports/{export_id}/
   ├── manifest.json    # {export_id, format, scope, status, ...} 元数据
   └── export.zip       # 真正的导出 zip(内含 book.md / 各章 md + meta.json)
   ```

   zip 内含(format=md):

   ```
   book.md            # 全章拼接
   meta.json          # 作者 / 字数 / 卷章数 / 创作时间 / 引擎版本(include_metadata=true)
   ```

   zip 内含(format=chaptered_md):

   ```
   index.md
   volume-1/chapter-0001.md
   volume-1/chapter-0002.md
   ...
   meta.json          # 同上
   ```

3. **invalidated 处置**(决策 3=A invalidated 档语义):

   - ``scope=full`` / ``scope=volume``:扫 ChapterORM ``status='invalidated'``
     的章节**默认跳过**,不抛错;manifest.json + meta.json 记录跳过数。
   - ``scope=range``:扫 [start, end] 闭区间内若含 invalidated 章节,**直接
     抛 ``ExportScopeInvalidError`` 422** + details 含 ``invalidated_chapter_ids``
     + reason 提示 user 先 unblock / rewind。

4. **5 WS event**(对齐 plan §"P6-B-04" 字面 stage `collecting / rendering /
   packaging / done`):

   - ``stage=collecting``:进入 execute 后立即推
   - ``stage=rendering``:章节内容拼接前推
   - ``stage=packaging``:zip 打包前推
   - ``stage=done``:任务成功完成推
   - ``stage=failed``:任务失败兜底推(顶层 swallow 内)

   全部推到 ``task.{task_id}.progress`` 频道(对齐 P5-B-06 batch_stage 模式)。

5. **3 异常类**(对齐 plan §"P6-B-04" 字面 3 错误码):

   - ``ExportFormatUnsupportedError`` → 422 ``EXPORT_FORMAT_UNSUPPORTED``
   - ``ExportNotFoundError`` → 404 ``EXPORT_NOT_FOUND``
   - ``ExportScopeInvalidError`` → 422 ``EXPORT_SCOPE_INVALID``

6. **rollback**:execute 失败时 manifest.json 翻 ``failed`` + ``error`` 字段;
   半完成的 zip 删除(对齐 plan §10 测试约定 rollback 类"导出失败时不留
   垃圾文件")。
"""

from __future__ import annotations

import json
import secrets
import shutil
import tempfile
import zipfile
from collections.abc import Callable
from datetime import UTC, datetime
from pathlib import Path
from typing import TYPE_CHECKING, Any

import structlog
from sqlalchemy import select
from sqlalchemy.orm import Session

from src.api.websocket.channels import task_progress_channel
from src.core.db.models import ChapterORM, VolumeORM
from src.core.exporters import (
    ChapterPayload,
    build_index_md,
    render_docx,
    render_epub,
    render_markdown_full,
    render_per_chapter_files,
)
from src.core.workspace.workspace_paths import WorkspacePaths
from src.services.workspace_service import WorkspaceService

if TYPE_CHECKING:
    from src.api.websocket.manager import WebSocketManager


_log = structlog.get_logger(__name__)


SessionFactory = Callable[[str], Session]


_SUPPORTED_FORMATS: tuple[str, ...] = ("md", "chaptered_md", "epub", "docx")
"""P6-B-04 起支持的 format 集合;P6-B-05 接入 epub;P6-B-06 接入 docx(4 档全集)。"""


_COVER_FILENAME: str = "cover.png"
"""``data/assets/`` 下 EPUB 封面图候选文件名(P6-B-05 自决项)。"""


_INVALIDATED_STATUS: str = "invalidated"
"""ChapterORM.status 中的 invalidated 档(P6-B-01 决策 3=A 新增 8 档之一)。"""


_MAX_LIST_ITEMS: int = 10
"""``:list`` 端点最近 N 次导出条目数(对齐 plan §"P6-B-04" 字面"最近 10 次")。"""


# =============================================================================
# 异常类型
# =============================================================================


class ExportServiceError(Exception):
    """export_service 通用错误基类(翻译 INTERNAL_ERROR 500 兜底)。"""


class ExportFormatUnsupportedError(ExportServiceError):
    """format 不在已实现集合内(epub/docx 在 P6-B-04 占位;未来未知值)。

    路由层翻译为 ``EXPORT_FORMAT_UNSUPPORTED (422)``。
    """

    def __init__(
        self,
        *,
        format: str,
        supported_formats: tuple[str, ...] = _SUPPORTED_FORMATS,
    ) -> None:
        self.format = format
        self.supported_formats = supported_formats
        super().__init__(
            f"format={format} 尚未实现,目前支持: {list(supported_formats)}"
        )


class ExportNotFoundError(ExportServiceError):
    """:download / :get 端点 export_id 不存在(目录 / manifest 缺失)。

    路由层翻译为 ``EXPORT_NOT_FOUND (404)``。
    """

    def __init__(self, *, workspace_id: str, export_id: str) -> None:
        self.workspace_id = workspace_id
        self.export_id = export_id
        super().__init__(
            f"workspace {workspace_id} 内 export_id={export_id} 不存在"
        )


class ExportScopeInvalidError(ExportServiceError):
    """scope 字段组合 / scope=range 含 invalidated 等不合法。

    路由层翻译为 ``EXPORT_SCOPE_INVALID (422)``。
    """

    def __init__(
        self,
        *,
        scope: str,
        reason: str,
        details: dict[str, Any] | None = None,
    ) -> None:
        self.scope = scope
        self.reason = reason
        self.invalid_details = details or {}
        super().__init__(f"scope={scope} 不合法: {reason}")


# =============================================================================
# Service
# =============================================================================


class ExportService:
    """导出业务层(P6-B-04 md / chaptered_md 实现)。

    依赖:

    - ``workspace_service``:校验 workspace + 读 ``meta.json``(作者 / 章数等)
    - ``session_factory``:**必需**,开 ``data/novel.db`` Session 读 ChapterORM
      / VolumeORM / ChapterCardORM
    - ``ws_manager``:可选;无注入 → WS publish 静默跳过(对齐 P6-B-02 模式)
    - ``engine_version``:引擎版本字符串,写入 meta.json(对齐 P1-B-04 meta 模式)

    线程安全:**无锁**(每个 export_id 独立目录 + 后台任务串行写;任务彼此
    之间无共享可变状态)。
    """

    def __init__(
        self,
        *,
        workspace_service: WorkspaceService,
        session_factory: SessionFactory,
        ws_manager: "WebSocketManager | None" = None,
        engine_version: str = "0.1.0",
    ) -> None:
        self._workspace_service = workspace_service
        self._session_factory = session_factory
        self._ws_manager = ws_manager
        self._engine_version = engine_version

    # ---- :create 入口 -----------------------------------------------------

    def create_export(
        self,
        workspace_id: str,
        *,
        task_id: str,
        format: str,
        scope: str,
        volume_id: str | None,
        start_chapter_id: str | None,
        end_chapter_id: str | None,
        include_metadata: bool,
        include_review_history: bool,
        include_length_notes: bool = True,
    ) -> tuple[str, dict[str, Any]]:
        """校验 + 落初始 manifest.json + 返 ``(export_id, manifest_dict)``。

        校验顺序:

        1. workspace 存在(``WorkspaceService.info``)
        2. format 在 _SUPPORTED_FORMATS 内(否则 422 unsupported)
        3. scope 字段组合合法 + 解析 chapter_ids 顺带扫 invalidated
        4. 生成 export_id + 落 manifest.json(``status="pending"``)

        本方法**不**做章节内容收集 + 渲染 + 打包(走后台 execute)。
        """
        # 1. workspace 校验
        self._workspace_service.info(workspace_id)

        # 2. format 校验
        if format not in _SUPPORTED_FORMATS:
            raise ExportFormatUnsupportedError(format=format)

        # 3. scope 字段组合 + invalidated 校验(此处只解析参数 + 检测
        # invalidated;真正收集走 execute)
        chapter_summaries = self._resolve_and_validate_scope(
            workspace_id=workspace_id,
            scope=scope,
            volume_id=volume_id,
            start_chapter_id=start_chapter_id,
            end_chapter_id=end_chapter_id,
        )

        # 4. 生成 export_id + 落 manifest
        export_id = f"exp_{secrets.token_hex(8)}"
        export_dir = self._export_dir(workspace_id, export_id)
        export_dir.mkdir(parents=True, exist_ok=True)
        now = datetime.now(UTC)
        manifest: dict[str, Any] = {
            "export_id": export_id,
            "workspace_id": workspace_id,
            "format": format,
            "scope": scope,
            "volume_id": volume_id,
            "start_chapter_id": start_chapter_id,
            "end_chapter_id": end_chapter_id,
            "include_metadata": include_metadata,
            "include_review_history": include_review_history,
            "include_length_notes": include_length_notes,
            "status": "pending",
            "created_at": now.isoformat(),
            "completed_at": None,
            "chapter_count": len(chapter_summaries),
            "file_size_bytes": None,
            "task_id": task_id,
            "error": None,
        }
        self._write_manifest(workspace_id, export_id, manifest)
        return export_id, manifest

    # ---- :execute 后台任务 --------------------------------------------------

    def execute(
        self,
        workspace_id: str,
        *,
        task_id: str,
        export_id: str,
    ) -> dict[str, Any]:
        """后台跑章节收集 → 渲染 → zip 打包(对齐 P5-B-06 顶层 swallow 模式)。

        4 stage WS publish + structlog;任何异常顶层 swallow 翻 manifest.json
        ``status='failed'`` + 删半完成 zip。
        """
        try:
            return self._execute_inner(
                workspace_id, task_id=task_id, export_id=export_id
            )
        except Exception as exc:  # noqa: BLE001 — 顶层 swallow,对齐 P5-B-06 模式
            _log.error(
                "export_execute_failed",
                workspace_id=workspace_id,
                task_id=task_id,
                export_id=export_id,
                error_type=type(exc).__name__,
                error_message=str(exc),
            )
            # rollback:manifest 翻 failed + 删半完成 zip
            self._mark_failed(
                workspace_id=workspace_id,
                export_id=export_id,
                error=f"{type(exc).__name__}: {exc}",
            )
            # 同步落任务 registry 失败状态(对齐 P5-B-06 模式)
            self._update_task_failed(task_id, error_message=str(exc))
            self._publish_stage(
                task_id=task_id,
                stage="failed",
                export_id=export_id,
                error=str(exc),
            )
            return {
                "task_id": task_id,
                "export_id": export_id,
                "workspace_id": workspace_id,
                "status": "failed",
                "error": str(exc),
            }

    def _execute_inner(
        self,
        workspace_id: str,
        *,
        task_id: str,
        export_id: str,
    ) -> dict[str, Any]:
        manifest = self._read_manifest(workspace_id, export_id)
        if manifest is None:
            raise ExportServiceError(
                f"manifest 不存在: workspace={workspace_id} export_id={export_id}"
            )
        format = manifest["format"]
        scope = manifest["scope"]

        # ---- stage=collecting ----
        self._publish_stage(
            task_id=task_id, stage="collecting", export_id=export_id
        )
        # 标 running 状态
        manifest["status"] = "running"
        self._write_manifest(workspace_id, export_id, manifest)

        chapters = self._collect_chapter_payloads(
            workspace_id,
            scope=scope,
            volume_id=manifest.get("volume_id"),
            start_chapter_id=manifest.get("start_chapter_id"),
            end_chapter_id=manifest.get("end_chapter_id"),
            include_length_notes=manifest.get("include_length_notes", False),
        )
        workspace_info = self._workspace_service.info(workspace_id)

        # ---- stage=rendering ----
        self._publish_stage(
            task_id=task_id, stage="rendering", export_id=export_id
        )
        files: dict[str, str | bytes]
        if format == "md":
            files = {
                "book.md": render_markdown_full(
                    chapters,
                    workspace_display_name=workspace_info.display_name,
                )
            }
        elif format == "chaptered_md":
            files = dict(render_per_chapter_files(chapters))
            files["index.md"] = build_index_md(
                chapters,
                workspace_display_name=workspace_info.display_name,
            )
        elif format == "epub":
            cover_bytes = self._load_cover_image_bytes(workspace_id)
            files = {
                "book.epub": render_epub(
                    chapters,
                    workspace_id=workspace_id,
                    workspace_display_name=workspace_info.display_name,
                    author=workspace_info.display_name,
                    cover_image_bytes=cover_bytes,
                    engine_version=self._engine_version,
                )
            }
        elif format == "docx":
            files = {
                "book.docx": render_docx(
                    chapters,
                    workspace_display_name=workspace_info.display_name,
                )
            }
        else:
            raise ExportFormatUnsupportedError(format=format)

        # meta.json(include_metadata)
        if manifest.get("include_metadata", True):
            meta_json = self._build_meta_json(
                workspace_info=workspace_info,
                chapters=chapters,
                format=format,
                scope=scope,
            )
            files["meta.json"] = json.dumps(
                meta_json, ensure_ascii=False, indent=2
            )

        # ---- stage=packaging ----
        self._publish_stage(
            task_id=task_id, stage="packaging", export_id=export_id
        )
        zip_path = self._zip_path(workspace_id, export_id)
        self._write_zip_atomic(zip_path, files)

        # ---- stage=done ----
        now = datetime.now(UTC)
        size = zip_path.stat().st_size
        manifest["status"] = "done"
        manifest["completed_at"] = now.isoformat()
        manifest["chapter_count"] = len(chapters)
        manifest["file_size_bytes"] = size
        manifest["error"] = None
        self._write_manifest(workspace_id, export_id, manifest)

        self._update_task_done(
            task_id,
            result={
                "task_id": task_id,
                "export_id": export_id,
                "workspace_id": workspace_id,
                "status": "done",
                "format": format,
                "scope": scope,
                "chapter_count": len(chapters),
                "file_size_bytes": size,
            },
        )
        self._publish_stage(
            task_id=task_id,
            stage="done",
            export_id=export_id,
            chapter_count=len(chapters),
            file_size_bytes=size,
        )
        _log.info(
            "export_completed",
            workspace_id=workspace_id,
            export_id=export_id,
            task_id=task_id,
            format=format,
            scope=scope,
            chapter_count=len(chapters),
            file_size_bytes=size,
        )
        return {
            "task_id": task_id,
            "export_id": export_id,
            "workspace_id": workspace_id,
            "status": "done",
            "format": format,
            "scope": scope,
            "chapter_count": len(chapters),
            "file_size_bytes": size,
        }

    # ---- :list / :download 入口 --------------------------------------------

    def list_exports(self, workspace_id: str) -> list[dict[str, Any]]:
        """扫 ``exports/`` 目录返按 ``created_at`` 倒序的 manifest 列表
        (最近 N 条)。"""
        self._workspace_service.info(workspace_id)
        paths = self._paths(workspace_id)
        if not paths.exports_dir.is_dir():
            return []
        manifests: list[dict[str, Any]] = []
        for entry in paths.exports_dir.iterdir():
            if not entry.is_dir():
                continue
            manifest = self._read_manifest(workspace_id, entry.name)
            if manifest is not None:
                manifests.append(manifest)
        # 按 created_at 倒序;ISO 字符串 lex 序与时间序一致
        manifests.sort(
            key=lambda m: m.get("created_at", ""), reverse=True
        )
        return manifests[:_MAX_LIST_ITEMS]

    def get_download_path(self, workspace_id: str, export_id: str) -> Path:
        """返指定 export 的 zip 物理路径;不存在抛 ``ExportNotFoundError``。

        校验顺序:① workspace 存在 ② export_dir 存在 ③ manifest 存在 ④ zip
        存在 + status='done'。任一未满足都视为 404。
        """
        self._workspace_service.info(workspace_id)
        export_dir = self._export_dir(workspace_id, export_id)
        if not export_dir.is_dir():
            raise ExportNotFoundError(
                workspace_id=workspace_id, export_id=export_id
            )
        manifest = self._read_manifest(workspace_id, export_id)
        if manifest is None or manifest.get("status") != "done":
            raise ExportNotFoundError(
                workspace_id=workspace_id, export_id=export_id
            )
        zip_path = self._zip_path(workspace_id, export_id)
        if not zip_path.is_file():
            raise ExportNotFoundError(
                workspace_id=workspace_id, export_id=export_id
            )
        return zip_path

    # ---- 内部:scope 校验 ----------------------------------------------------

    def _resolve_and_validate_scope(
        self,
        *,
        workspace_id: str,
        scope: str,
        volume_id: str | None,
        start_chapter_id: str | None,
        end_chapter_id: str | None,
    ) -> list[dict[str, Any]]:
        """scope 3 档 → chapter_summaries(``{chapter_id, chapter_number,
        volume_id, volume_index, volume_title, global_index, status}``)。

        invalidated 处置:

        - ``full`` / ``volume``:静默跳过 invalidated 章节,返结果不含。
        - ``range``:[start, end] 内含 invalidated → 抛
          ``ExportScopeInvalidError`` 422 + details ``invalidated_chapter_ids``。

        其他不合法 → ``ExportScopeInvalidError`` 422 + reason。
        """
        if scope not in ("full", "volume", "range"):
            raise ExportScopeInvalidError(
                scope=scope, reason="scope 必须为 full / volume / range"
            )

        all_chapters = self._enumerate_all_chapters(workspace_id)
        if not all_chapters:
            raise ExportScopeInvalidError(
                scope=scope,
                reason="workspace 无任何 volume(outline 未 commit?)",
            )

        # 加章节状态(从 ChapterORM 拉,无行视为未写 = 非 invalidated)
        statuses = self._load_chapter_statuses(workspace_id)
        for ch in all_chapters:
            ch["status"] = statuses.get(ch["chapter_id"], None)

        if scope == "full":
            return [c for c in all_chapters if c["status"] != _INVALIDATED_STATUS]

        if scope == "volume":
            if not volume_id:
                raise ExportScopeInvalidError(
                    scope=scope, reason="scope=volume 需提供 volume_id"
                )
            in_volume = [c for c in all_chapters if c["volume_id"] == volume_id]
            if not in_volume:
                raise ExportScopeInvalidError(
                    scope=scope,
                    reason=f"volume_id={volume_id} 不存在或无章节",
                    details={"volume_id": volume_id},
                )
            return [
                c for c in in_volume if c["status"] != _INVALIDATED_STATUS
            ]

        # scope == "range"
        if not start_chapter_id or not end_chapter_id:
            raise ExportScopeInvalidError(
                scope=scope,
                reason="scope=range 需同时提供 start_chapter_id 与 end_chapter_id",
            )
        chapter_ids = [c["chapter_id"] for c in all_chapters]
        if start_chapter_id not in chapter_ids:
            raise ExportScopeInvalidError(
                scope=scope,
                reason=f"start_chapter_id={start_chapter_id} 不存在",
                details={"start_chapter_id": start_chapter_id},
            )
        if end_chapter_id not in chapter_ids:
            raise ExportScopeInvalidError(
                scope=scope,
                reason=f"end_chapter_id={end_chapter_id} 不存在",
                details={"end_chapter_id": end_chapter_id},
            )
        if start_chapter_id > end_chapter_id:
            raise ExportScopeInvalidError(
                scope=scope,
                reason="start_chapter_id 必须 ≤ end_chapter_id(lex 序)",
            )
        in_range = [
            c
            for c in all_chapters
            if start_chapter_id <= c["chapter_id"] <= end_chapter_id
        ]
        invalid_ids = [
            c["chapter_id"]
            for c in in_range
            if c["status"] == _INVALIDATED_STATUS
        ]
        if invalid_ids:
            raise ExportScopeInvalidError(
                scope=scope,
                reason=(
                    "scope=range 区间内含 invalidated 章节,请先 unblock 或"
                    " 沿 rewind 链清理后再导出"
                ),
                details={"invalidated_chapter_ids": invalid_ids},
            )
        return in_range

    # ---- 内部:章节内容收集 -------------------------------------------------

    def _collect_chapter_payloads(
        self,
        workspace_id: str,
        *,
        scope: str,
        volume_id: str | None,
        start_chapter_id: str | None,
        end_chapter_id: str | None,
        include_length_notes: bool = False,
    ) -> list[ChapterPayload]:
        """收集章节正文 + ChapterCard 标题 / 章号 → ``ChapterPayload`` 列表。

        正文读取优先级(v1.0.x reviewer-resilience 起):
        1. ``data/chapters/<chapter_id>/final.md``(正常路径)
        2. **fallback**:``data/chapters/<chapter_id>/v{current_version}.md``
           — 用于 force_pass 历史数据(v1.0.x 起 force_pass 已补 mark_final,
           但既有工作区可能存在 status=approved 但缺 final.md 的章节)
        3. 两者都缺 → content 空串(render 层占位"(章节未生成)")

        命中 fallback 时埋 ``export_chapter_missing_final`` warn 日志,提示
        维护人跑 ``scripts/backfill_final_md_for_force_passed.py --apply`` 修。

        标题从 ChapterCardORM + 文件 ``data/cards/<chapter_id>.json`` 读;两者
        都缺则 fallback ``"第 N 章"``。
        """
        summaries = self._resolve_and_validate_scope(
            workspace_id=workspace_id,
            scope=scope,
            volume_id=volume_id,
            start_chapter_id=start_chapter_id,
            end_chapter_id=end_chapter_id,
        )
        paths = self._paths(workspace_id)
        cards_titles = self._load_chapter_titles(workspace_id)
        current_versions = self._load_chapter_current_versions(workspace_id)
        payloads: list[ChapterPayload] = []
        for s in summaries:
            chapter_id = s["chapter_id"]
            final_path = paths.chapter_version_file(chapter_id, "final.md")
            content = ""
            if final_path.is_file():
                content = final_path.read_text(encoding="utf-8")
            else:
                # v1.0.x reviewer-resilience:fallback 读 v{current_version}.md
                # 兜底 force_pass 历史数据缺 final.md 场景。
                cv = current_versions.get(chapter_id)
                if cv is not None and cv > 0:
                    vfile = paths.chapter_version_file(
                        chapter_id, f"v{cv}.md"
                    )
                    if vfile.is_file():
                        content = vfile.read_text(encoding="utf-8")
                        _log.warning(
                            "export_chapter_missing_final",
                            workspace_id=workspace_id,
                            chapter_id=chapter_id,
                            current_version=cv,
                            fallback_path=str(vfile.name),
                        )
            title = cards_titles.get(
                chapter_id, f"第 {s['chapter_number']} 章"
            )
            payloads.append(
                ChapterPayload(
                    chapter_id=chapter_id,
                    chapter_number=s["chapter_number"],
                    global_index=s["global_index"],
                    volume_id=s["volume_id"],
                    volume_index=s["volume_index"],
                    volume_title=s["volume_title"],
                    title=title,
                    content=content,
                    editing_note=(self._chapter_length_note(paths, chapter_id, content)
                                  if include_length_notes and content.strip() else ""),
                )
            )
        return payloads

    @staticmethod
    def _chapter_length_note(paths: WorkspacePaths, chapter_id: str, content: str) -> str:
        from src.core.rules.prose_length_policy import length_editing_note

        maximum = None
        try:
            data = json.loads((paths.cards_dir / f"{chapter_id}.json").read_text(encoding="utf-8"))
            candidate = data.get("word_count_max")
            if type(candidate) is int and candidate > 0:
                maximum = candidate
        except (OSError, ValueError, AttributeError):
            pass
        return length_editing_note(len(content), maximum) if maximum is not None else ""

    def _load_chapter_current_versions(
        self, workspace_id: str
    ) -> dict[str, int]:
        """SELECT chapters.current_version 全表 → ``{chapter_id: current_version}``。

        v1.0.x reviewer-resilience(2026-05-20)新增,供 ``_collect_chapter_payloads``
        fallback 读 ``v{current_version}.md`` 用。
        """
        session = self._session_factory(workspace_id)
        try:
            stmt = select(
                ChapterORM.chapter_id, ChapterORM.current_version
            ).where(ChapterORM.workspace_id == workspace_id)
            rows = session.execute(stmt).all()
        finally:
            session.close()
        return {row.chapter_id: row.current_version for row in rows}

    def _load_cover_image_bytes(self, workspace_id: str) -> bytes | None:
        """读 ``data/assets/cover.png`` 字节;不存在 / 读失败返 None。

        P6-B-05 自决项:EPUB 渲染时优先用物理封面图;无图时由
        ``render_epub`` 生成 fallback 文字封面 XHTML(对齐 plan §"P6-B-05"
        字面"cover 占位"约定)。
        """
        paths = self._paths(workspace_id)
        cover_path = paths.assets_dir / _COVER_FILENAME
        if not cover_path.is_file():
            return None
        try:
            return cover_path.read_bytes()
        except OSError:
            return None

    def _load_chapter_titles(self, workspace_id: str) -> dict[str, str]:
        """读 ``data/cards/<chapter_id>.json`` 文件抽 ``title`` 字段。

        从文件读而非从 ChapterCardORM:title 字段不在 P5-B-02 最小列 ORM 中,
        ChapterCard 完整内容仍走文件 SSOT(决策 6=C)。
        """
        paths = self._paths(workspace_id)
        result: dict[str, str] = {}
        if not paths.cards_dir.is_dir():
            return result
        for card_file in paths.cards_dir.glob("*.json"):
            try:
                data = json.loads(card_file.read_text(encoding="utf-8"))
                chapter_id = data.get("chapter_id")
                title = data.get("title")
                if chapter_id and title:
                    result[chapter_id] = title
            except (OSError, json.JSONDecodeError):
                # 单卡解析失败 swallow + 跳过(避免 1 个坏卡阻塞整个导出)
                continue
        return result

    # ---- 内部:DB 查询 ------------------------------------------------------

    def _enumerate_all_chapters(
        self, workspace_id: str
    ) -> list[dict[str, Any]]:
        """读 VolumeORM × chapter_count → 全局章节列表(对齐 PipelineService
        ``_enumerate_all_chapters`` 模式)。

        返字段:``chapter_id`` / ``chapter_number`` (卷内 1-based) /
        ``volume_id`` / ``volume_index`` (全书 1-based 卷序) / ``volume_title``
        / ``global_index`` (全书 1-based 章序)。
        """
        session = self._session_factory(workspace_id)
        try:
            volumes = (
                session.query(VolumeORM)
                .filter(VolumeORM.workspace_id == workspace_id)
                .order_by(VolumeORM.volume_id)
                .all()
            )
        finally:
            session.close()

        result: list[dict[str, Any]] = []
        global_idx = 0
        for v_idx, vol in enumerate(volumes, start=1):
            for chapter_number in range(1, vol.chapter_count + 1):
                global_idx += 1
                result.append(
                    {
                        "chapter_id": f"ch-{global_idx:04d}",
                        "chapter_number": chapter_number,
                        "volume_id": vol.volume_id,
                        "volume_index": v_idx,
                        "volume_title": vol.title,
                        "global_index": global_idx,
                    }
                )
        return result

    def _load_chapter_statuses(
        self, workspace_id: str
    ) -> dict[str, str]:
        """SELECT chapters.status 全表 → ``{chapter_id: status}`` 字典。"""
        session = self._session_factory(workspace_id)
        try:
            stmt = select(ChapterORM.chapter_id, ChapterORM.status).where(
                ChapterORM.workspace_id == workspace_id
            )
            rows = session.execute(stmt).all()
        finally:
            session.close()
        return {row.chapter_id: row.status for row in rows}

    # ---- 内部:meta.json 组装 ----------------------------------------------

    def _build_meta_json(
        self,
        *,
        workspace_info: Any,
        chapters: list[ChapterPayload],
        format: str,
        scope: str,
    ) -> dict[str, Any]:
        """组装 zip 内 ``meta.json``。

        字段(对齐 plan §"P6-B-04" 字面"作者 / 字数 / 卷章数 / 创作时间 /
        引擎版本"):

        - ``workspace_id`` / ``display_name`` / ``genre`` / ``premise``
        - ``author``:作者(meta.json 暂无字段,取 ``display_name`` 兜底)
        - ``word_count``:全 chapters content 字符总数(中文按字符计;近似)
        - ``volume_count`` / ``chapter_count``
        - ``created_at``:workspace 创建时间(ISO)
        - ``exported_at``:本次导出完成时间
        - ``engine_version``
        - ``format`` / ``scope``
        """
        word_count = sum(len(ch.content) for ch in chapters)
        volume_count = len({ch.volume_id for ch in chapters})
        return {
            "workspace_id": workspace_info.workspace_id,
            "display_name": workspace_info.display_name,
            "author": workspace_info.display_name,
            "genre": workspace_info.genre,
            "premise": workspace_info.premise,
            "word_count": word_count,
            "volume_count": volume_count,
            "chapter_count": len(chapters),
            "created_at": workspace_info.created_at.isoformat(),
            "exported_at": datetime.now(UTC).isoformat(),
            "engine_version": self._engine_version,
            "format": format,
            "scope": scope,
        }

    # ---- 内部:manifest / zip / 路径 helper --------------------------------

    def _paths(self, workspace_id: str) -> WorkspacePaths:
        return WorkspacePaths(
            workspace_id,
            workspaces_root=self._workspace_service.workspaces_root,
        )

    def _export_dir(self, workspace_id: str, export_id: str) -> Path:
        return self._paths(workspace_id).exports_dir / export_id

    def _manifest_path(self, workspace_id: str, export_id: str) -> Path:
        return self._export_dir(workspace_id, export_id) / "manifest.json"

    def _zip_path(self, workspace_id: str, export_id: str) -> Path:
        return self._export_dir(workspace_id, export_id) / "export.zip"

    def _read_manifest(
        self, workspace_id: str, export_id: str
    ) -> dict[str, Any] | None:
        path = self._manifest_path(workspace_id, export_id)
        if not path.is_file():
            return None
        try:
            return json.loads(path.read_text(encoding="utf-8"))
        except (OSError, json.JSONDecodeError):
            return None

    def _write_manifest(
        self,
        workspace_id: str,
        export_id: str,
        manifest: dict[str, Any],
    ) -> None:
        path = self._manifest_path(workspace_id, export_id)
        path.parent.mkdir(parents=True, exist_ok=True)
        # 原子写:tmp + rename(对齐 OutlineService _atomic_write_json 模式)
        tmp_fd, tmp_path = tempfile.mkstemp(
            prefix=".manifest_", suffix=".tmp", dir=path.parent
        )
        try:
            with open(tmp_fd, "w", encoding="utf-8") as f:
                json.dump(manifest, f, ensure_ascii=False, indent=2)
            Path(tmp_path).replace(path)
        except Exception:
            Path(tmp_path).unlink(missing_ok=True)
            raise

    def _write_zip_atomic(
        self, zip_path: Path, files: dict[str, str | bytes]
    ) -> None:
        """原子写 zip 包:先写 ``.tmp`` 后 ``replace``,失败清理 tmp。

        ``files`` 字典的值可为 ``str``(纯文本如 markdown / json)或 ``bytes``
        (二进制如 EPUB 完整文件)。``zipfile.writestr`` 原生支持两种类型。
        """
        zip_path.parent.mkdir(parents=True, exist_ok=True)
        tmp_path = zip_path.with_suffix(".zip.tmp")
        try:
            with zipfile.ZipFile(
                tmp_path, "w", compression=zipfile.ZIP_DEFLATED
            ) as zf:
                for rel_path, content in files.items():
                    zf.writestr(rel_path, content)
            tmp_path.replace(zip_path)
        except Exception:
            tmp_path.unlink(missing_ok=True)
            raise

    def _mark_failed(
        self, *, workspace_id: str, export_id: str, error: str
    ) -> None:
        """manifest.json 翻 ``failed`` + 删半完成 zip + 删 tmp(rollback)。"""
        manifest = self._read_manifest(workspace_id, export_id)
        if manifest is None:
            # 连 manifest 都没落成功 — 直接删整个 export_dir
            export_dir = self._export_dir(workspace_id, export_id)
            if export_dir.is_dir():
                shutil.rmtree(export_dir, ignore_errors=True)
            return
        manifest["status"] = "failed"
        manifest["error"] = error
        manifest["completed_at"] = datetime.now(UTC).isoformat()
        try:
            self._write_manifest(workspace_id, export_id, manifest)
        except Exception:  # noqa: BLE001
            pass
        # 删半完成 zip / tmp
        zip_path = self._zip_path(workspace_id, export_id)
        zip_path.unlink(missing_ok=True)
        tmp_path = zip_path.with_suffix(".zip.tmp")
        tmp_path.unlink(missing_ok=True)

    # ---- 内部:task_registry 联动 ------------------------------------------

    def _update_task_done(self, task_id: str, *, result: dict[str, Any]) -> None:
        # 局部 import 规避 service 层 ↔ api 层循环
        from src.api import task_registry

        task_registry.update_to_done(task_id, result=result)

    def _update_task_failed(self, task_id: str, *, error_message: str) -> None:
        from src.api import task_registry

        task_registry.update_to_failed(
            task_id,
            error={"code": "EXPORT_FAILED", "message": error_message},
        )

    # ---- 内部:WS publish --------------------------------------------------

    def _publish_stage(
        self,
        *,
        task_id: str,
        stage: str,
        export_id: str,
        **extra: Any,
    ) -> None:
        """WS publish 到 ``task.{task_id}.progress``;失败 swallow + log
        (对齐章节卡任务 WS publish 的 swallow 模式)。"""
        if self._ws_manager is None:
            return
        data: dict[str, Any] = {
            "stage": stage,
            "export_id": export_id,
        }
        data.update(extra)
        try:
            self._ws_manager.publish(
                channel=task_progress_channel(task_id),
                event="stage",
                data=data,
            )
        except Exception as exc:  # noqa: BLE001
            _log.error(
                "export_stage_notify_failed",
                task_id=task_id,
                stage=stage,
                error_type=type(exc).__name__,
                error_message=str(exc),
            )


__all__ = [
    "ExportFormatUnsupportedError",
    "ExportNotFoundError",
    "ExportScopeInvalidError",
    "ExportService",
    "ExportServiceError",
]
