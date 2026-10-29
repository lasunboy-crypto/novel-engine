"""章节版本管理:把章节正文落盘到 ``data/chapters/<chapter_id>/v{N}.md``。

职责边界(P1-B-05):
  - 仅管"正文文件"的落盘与版本号编号;**元数据(creation_type / model_used /
    notes / review_score / created_at / word_count)只进返回的 ``ChapterVersion``
    对象,不写入文件**。元数据持久化由 P1-B-16 的 ``chapter_versions`` 表负责。
  - 文件名固定 ``v{N}.md``(N 从 1 起,对齐 ``ChapterVersion.version_id ge=1``);
    不把 ``creation_type`` 编进文件名(版本号已足够定位)。
  - ``write_version`` 自动算下一个版本号;调用方不传 ``version_id``。
  - ``next_version_id`` 在版本断档(如有 v1、v3 无 v2)时返回 ``max+1``,保持
    单调递增。
  - 同版本号文件已存在 → 抛错(写错版本号是 bug,不能静默覆盖)。
  - ``mark_final`` 把指定版本拷贝成 ``final.md``;若 ``final.md`` 已存在则
    **直接覆盖**(对应"重新审核后最终通过另一版本"的合法场景)。
  - 章节目录不存在时由本模块 ``mkdir(parents=True, exist_ok=True)``,不要求
    上层预创建。
"""

from __future__ import annotations

import os
import re
from datetime import datetime, timezone
from pathlib import Path
from typing import Literal
from uuid import uuid4

import structlog

from src.core.models.chapter import ChapterVersion
from src.core.rules.style_fingerprint import compute_style_fingerprint
from src.core.workspace.workspace_paths import WorkspacePaths

CreationType = Literal["draft", "rewrite", "revise", "manual_edit"]

_CANONICAL_VERSION_FILE_PATTERN = re.compile(r"^v([1-9][0-9]*)\.md$")
_VERSION_LIKE_FILE_PATTERN = re.compile(r"^v(.+)\.md$", re.IGNORECASE)

_log = structlog.get_logger(__name__)


class ChapterVersioningError(Exception):
    """章节版本落盘失败时抛出。

    P1-B-17 全局错误体系起来后会升级为 ``ApiError`` 子类,目前先用普通 Exception。
    """


class ChapterVersionDurabilityError(ChapterVersioningError):
    """正文已发布可见，但目录项耐久性无法确认。

    调用方不得把它当成“完全没写”后直接重试；R8 持久操作租约会保留现场，
    冷启动再按磁盘、数据库和账本三方证据决定能否幂等收口。
    """


def collect_canonical_version_files(chapter_dir: Path) -> dict[int, Path]:
    """返回规范 ``vN.md`` 文件；数字型别名一律 fail-closed。

    版本文件名只允许 ASCII ``v[1-9][0-9]*.md``。例如 ``v01.md``、
    ``v0.md``、``V1.md`` 或使用非 ASCII 数字的 ``v١.md`` 都不能被折叠成
    合法版本号；恢复调用方必须先保留现场并停止 Provider。
    """

    version_files: dict[int, Path] = {}
    for entry in sorted(chapter_dir.iterdir(), key=lambda path: path.name):
        if not entry.is_file():
            continue
        canonical_match = _CANONICAL_VERSION_FILE_PATTERN.fullmatch(entry.name)
        if canonical_match is not None:
            version_id = int(canonical_match.group(1))
            existing = version_files.get(version_id)
            if existing is not None:
                raise ChapterVersioningError(
                    "章节目录存在重复版本别名: "
                    f"v{version_id} -> {existing.name}, {entry.name}"
                )
            version_files[version_id] = entry
            continue

        version_like_match = _VERSION_LIKE_FILE_PATTERN.fullmatch(entry.name)
        if (
            version_like_match is not None
            and version_like_match.group(1).isdigit()
        ):
            raise ChapterVersioningError(
                "章节目录存在非规范版本文件名，要求 v[1-9][0-9]*.md: "
                f"{entry.name}"
            )
    return version_files


class ChapterVersioning:
    """单工作区下的章节版本管理器。

    实例化时绑定 ``WorkspacePaths``;所有路径计算复用 ``WorkspacePaths`` 的
    ``chapter_dir`` / ``chapter_version_file``,本模块不重复拼路径逻辑。
    """

    FINAL_FILENAME = "final.md"

    def __init__(self, paths: WorkspacePaths) -> None:
        self._paths = paths

    @staticmethod
    def _fsync_directory(directory: Path) -> None:
        """持久化 rename/link 目录项，避免断电后只剩半个 checkpoint。"""

        fd = os.open(directory, os.O_RDONLY)
        try:
            os.fsync(fd)
        finally:
            os.close(fd)

    @classmethod
    def _write_new_atomic(cls, target: Path, content: str) -> None:
        """同目录 temp+fsync+hard-link，原子发布且绝不覆盖既有 vN。

        hard-link 成功是进程可见的发布点；目录 fsync 成功才证明断电后仍可恢复。
        若后者失败，正文不删除、不伪报成“完全没写”，但用专属异常阻止上层提交
        数据库或释放租约。
        """

        temp = target.with_name(f".{target.name}.{uuid4().hex}.tmp")
        try:
            with temp.open("x", encoding="utf-8") as handle:
                handle.write(content)
                handle.flush()
                os.fsync(handle.fileno())
            try:
                os.link(temp, target)
            except FileExistsError as exc:
                raise ChapterVersioningError(
                    f"版本文件已存在,拒绝覆盖: {target}"
                ) from exc
            try:
                cls._fsync_directory(target.parent)
            except OSError as exc:
                raise ChapterVersionDurabilityError(
                    "章节版本已发布可见，但目录持久化失败；已保留现场，禁止自动重试: "
                    f"{target}"
                ) from exc
        finally:
            try:
                temp.unlink()
            except FileNotFoundError:
                pass
            except OSError as exc:
                _log.warning(
                    "chapter_version_temp_cleanup_failed",
                    target=str(target),
                    temp=str(temp),
                    error_type=type(exc).__name__,
                    error_message=str(exc),
                )

    @classmethod
    def _replace_atomic_from_bytes(cls, target: Path, content: bytes) -> None:
        """同目录 temp+fsync+replace 原子切换 final 指针。

        ``os.replace`` 成功后 final 已对当前进程可见；目录 fsync 成功才证明
        断电后仍可恢复。失败时保留新 final，并用专属异常阻止上层释放租约。
        """

        temp = target.with_name(f".{target.name}.{uuid4().hex}.tmp")
        try:
            with temp.open("xb") as handle:
                handle.write(content)
                handle.flush()
                os.fsync(handle.fileno())
            os.replace(temp, target)
            try:
                cls._fsync_directory(target.parent)
            except OSError as exc:
                raise ChapterVersionDurabilityError(
                    "final 已切换可见，但目录持久化失败；已保留现场，禁止自动重试: "
                    f"{target}"
                ) from exc
        finally:
            try:
                temp.unlink()
            except FileNotFoundError:
                pass
            except OSError as exc:
                _log.warning(
                    "chapter_final_temp_cleanup_failed",
                    target=str(target),
                    temp=str(temp),
                    error_type=type(exc).__name__,
                    error_message=str(exc),
                )

    def next_version_id(self, chapter_id: str) -> int:
        """推断 ``chapter_id`` 下一个可用版本号。

        - 章节目录不存在或为空:返回 ``1``。
        - 已有 ``v1.md`` / ``v3.md`` 这种断档:返回 ``max + 1 = 4``,保持单调
          递增,避免回填 v2 让后来者误以为"v2 是 v3 的早期版本"。
        """

        chapter_dir = self._paths.chapter_dir(chapter_id)
        if not chapter_dir.exists():
            return 1

        version_files = collect_canonical_version_files(chapter_dir)
        return max(version_files, default=0) + 1

    def write_version(
        self,
        chapter_id: str,
        content: str,
        *,
        creation_type: CreationType,
        model_used: str,
        notes: str = "",
    ) -> ChapterVersion:
        """把 ``content`` 写到 ``v{N}.md`` 并返回 ``ChapterVersion``。

        ``N`` 由 ``next_version_id`` 自动决定;调用方只传业务元数据。
        ``review_score`` 初始为 ``None``,等 Reviewer 跑完后由上层(P1-B-13
        orchestrator)写回数据库,本模块不维护。

        - 章节目录不存在则自动创建(``mkdir(parents=True, exist_ok=True)``)。
        - 目标文件已存在 → ``ChapterVersioningError``。
        """

        # v1.0.x wave 6 s2.6a:race 防御 — 工作区已被 rmtree(destroy/purge/archive
        # 与异步章节写入任务的 race)→ raise ChapterVersioningError,caller(orchestrator
        # 或 chapter_service)的 try/except 会捕获 + 翻 task_registry failed +
        # publish failed event。raise 优于 silent return 因为本函数返 ChapterVersion
        # 是后续状态机的关键载荷,silent 返虚拟对象会误导下游。
        if not self._paths.root.is_dir():
            _log.warning(
                "workspace_writer_skipped",
                writer="write_version",
                chapter_id=chapter_id,
                creation_type=creation_type,
                reason="workspace_destroyed_during_async_task",
            )
            raise ChapterVersioningError(
                f"工作区已被销毁,拒绝写入章节版本: {self._paths.root}"
            )

        chapter_dir = self._paths.chapter_dir(chapter_id)
        chapter_dir.mkdir(parents=True, exist_ok=True)

        version_id = self.next_version_id(chapter_id)
        target = self._paths.chapter_version_file(chapter_id, f"v{version_id}.md")

        if target.exists():
            raise ChapterVersioningError(
                f"版本文件已存在,拒绝覆盖: {target}"
            )

        self._write_new_atomic(target, content)

        # v1.1 wave 1 session 4(G5):写盘时同步抽取风格指纹落 ChapterVersion,
        # 后续 persist_chapter_version_safely 写入 chapter_versions.style_fingerprint
        # 列。首版(v1)作 baseline,rewrite vN >= 2 由 style_drift_validator
        # 对比偏移度。0 长 content 兜底返全 0 fingerprint(此处不会触发,
        # write_version 调用方应保证 content 非空)。
        style_fingerprint = compute_style_fingerprint(content)

        return ChapterVersion(
            version_id=version_id,
            content=content,
            word_count=len(content),
            created_at=datetime.now(timezone.utc),
            creation_type=creation_type,
            model_used=model_used,
            review_score=None,
            notes=notes,
            style_fingerprint=style_fingerprint,
        )

    def read_version_content(self, chapter_id: str, version_id: int) -> str:
        """读出 ``v{version_id}.md`` 的正文。

        本模块只暴露"读正文",元数据请走数据库(P1-B-16)。文件不存在抛
        ``ChapterVersioningError``,统一异常翻译。
        """

        target = self._paths.chapter_version_file(
            chapter_id, f"v{version_id}.md"
        )
        try:
            return target.read_text(encoding="utf-8")
        except FileNotFoundError as exc:
            raise ChapterVersioningError(
                f"版本文件不存在: {target}"
            ) from exc

    def mark_final(self, chapter_id: str, version_id: int) -> Path:
        """把 ``v{version_id}.md`` 拷贝到 ``final.md``,返回 ``final.md`` 路径。

        - 源文件不存在 → ``ChapterVersioningError``。
        - ``final.md`` 已存在 → **直接覆盖**(P1 简单起见用拷贝;对应"重审后
          最终通过另一版本"的合法场景)。
        """

        source = self._paths.chapter_version_file(
            chapter_id, f"v{version_id}.md"
        )
        if not source.exists():
            raise ChapterVersioningError(
                f"无法标记 final:源版本文件不存在 {source}"
            )

        final_path = self._paths.chapter_version_file(
            chapter_id, self.FINAL_FILENAME
        )
        try:
            source_bytes = source.read_bytes()
        except OSError as exc:
            raise ChapterVersioningError(
                f"无法读取 final 源版本: {source}"
            ) from exc
        self._replace_atomic_from_bytes(final_path, source_bytes)
        return final_path
