"""``ExportService`` 单测(P6-B-04 md / chaptered_md)。

覆盖维度(对齐 plan §10 测试约定 5 类):

- **happy**:create_export + execute md/chaptered_md → zip 落盘 + 内容正确
- **error**:未知 format(如 ``"pdf"``)→ ExportFormatUnsupported / unknown
  export_id → ExportNotFound / scope 各档不合法 → ExportScopeInvalid
  (P6-B-06 起 md / chaptered_md / epub / docx 4 档全支持)
- **gate**:scope=full / volume 静默跳过 invalidated;scope=range 含 invalidated
  → ExportScopeInvalid
- **rollback**:execute 失败时 manifest=failed + 半 zip 不留
- **WS event**:4 stage(collecting / rendering / packaging / done)publish 验证
"""

from __future__ import annotations

import json
import zipfile
from collections.abc import Callable, Iterator
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

import pytest
from sqlalchemy import create_engine
from sqlalchemy.orm import Session
from sqlalchemy.pool import StaticPool

from src.api import task_registry
from src.core.db import Base
from src.core.db.models import ChapterORM, VolumeORM, WorkspaceORM
from src.services.export_service import (
    ExportFormatUnsupportedError,
    ExportNotFoundError,
    ExportScopeInvalidError,
    ExportService,
)
from src.services.workspace_service import WorkspaceService


class _FakeWSManager:
    """记录 publish 调用的最小 WS manager(对齐 P6-B-02 模式)。"""

    def __init__(self) -> None:
        self.events: list[dict[str, Any]] = []

    def publish(self, *, channel: str, event: str, data: dict[str, Any]) -> None:
        self.events.append(
            {"channel": channel, "event": event, "data": data}
        )


@pytest.fixture
def workspace_service(tmp_path: Path) -> WorkspaceService:
    workspaces_root = tmp_path / "workspaces"
    archives_root = tmp_path / "archives"
    workspaces_root.mkdir()
    archives_root.mkdir()
    return WorkspaceService(
        workspaces_root=workspaces_root, archives_root=archives_root
    )


@pytest.fixture
def workspace_id(workspace_service: WorkspaceService) -> str:
    workspace_service.create(
        "ws-exp-svc",
        display_name="P6-B-04 export svc test",
        genre="xianxia",
        premise="一个测试故事",
        activate=False,
    )
    return "ws-exp-svc"


@pytest.fixture
def session_factory() -> Callable[[str], Session]:
    engine = create_engine(
        "sqlite:///:memory:",
        connect_args={"check_same_thread": False},
        poolclass=StaticPool,
    )
    Base.metadata.create_all(engine)

    def factory(workspace_id: str) -> Session:
        return Session(engine)

    return factory


def _seed_db(
    session_factory: Callable[[str], Session],
    workspace_id: str,
    *,
    chapter_count: int = 3,
    invalidated_ids: list[str] | None = None,
) -> None:
    """种 1 卷 N 章 + 各章 ChapterORM 行(可指定 invalidated 列表)。"""
    session = session_factory(workspace_id)
    try:
        now = datetime.now(timezone.utc)
        session.merge(
            WorkspaceORM(
                workspace_id=workspace_id,
                display_name="x",
                genre="x",
                premise="p",
                created_at=now,
                last_active_at=now,
                engine_version="0.1.0",
                status="active",
                total_chapters=chapter_count,
                completed_chapters=0,
                config_overrides={},
            )
        )
        session.merge(
            VolumeORM(
                volume_id="vol-1",
                workspace_id=workspace_id,
                title="开端卷",
                summary="testing 开端",
                chapter_count=chapter_count,
                tension_template="heros_journey",
                arcs_json=[],
                tension_curve_json=[],
                created_at=now,
                last_updated_at=now,
            )
        )
        for i in range(1, chapter_count + 1):
            chapter_id = f"ch-{i:04d}"
            ch_status = (
                "invalidated"
                if invalidated_ids and chapter_id in invalidated_ids
                else "approved"
            )
            session.merge(
                ChapterORM(
                    chapter_id=chapter_id,
                    workspace_id=workspace_id,
                    current_version=1,
                    status=ch_status,
                    rewrite_count=0,
                    revise_count=0,
                    created_at=now,
                    approved_at=now if ch_status == "approved" else None,
                    total_token_usage={},
                    total_cost_usd=0.0,
                )
            )
        session.commit()
    finally:
        session.close()


def _write_final_md(
    workspace_service: WorkspaceService,
    workspace_id: str,
    chapter_id: str,
    content: str,
) -> None:
    from src.core.workspace.workspace_paths import WorkspacePaths

    paths = WorkspacePaths(
        workspace_id, workspaces_root=workspace_service.workspaces_root
    )
    ch_dir = paths.chapter_dir(chapter_id)
    ch_dir.mkdir(parents=True, exist_ok=True)
    (ch_dir / "final.md").write_text(content, encoding="utf-8")


def _write_version_md(
    workspace_service: WorkspaceService,
    workspace_id: str,
    chapter_id: str,
    version_id: int,
    content: str,
) -> None:
    """v1.0.x reviewer-resilience:辅助函数 — 物理写 v{N}.md 但不写 final.md。
    供测试 fallback 路径用(模拟 force_pass 历史数据缺 final.md 场景)。"""
    from src.core.workspace.workspace_paths import WorkspacePaths

    paths = WorkspacePaths(
        workspace_id, workspaces_root=workspace_service.workspaces_root
    )
    ch_dir = paths.chapter_dir(chapter_id)
    ch_dir.mkdir(parents=True, exist_ok=True)
    (ch_dir / f"v{version_id}.md").write_text(content, encoding="utf-8")


def _write_card_json(
    workspace_service: WorkspaceService,
    workspace_id: str,
    chapter_id: str,
    *,
    title: str,
    chapter_number: int,
) -> None:
    from src.core.workspace.workspace_paths import WorkspacePaths

    paths = WorkspacePaths(
        workspace_id, workspaces_root=workspace_service.workspaces_root
    )
    paths.cards_dir.mkdir(parents=True, exist_ok=True)
    (paths.cards_dir / f"{chapter_id}.json").write_text(
        json.dumps(
            {"chapter_id": chapter_id, "title": title, "chapter_number": chapter_number},
            ensure_ascii=False,
        ),
        encoding="utf-8",
    )


@pytest.fixture
def export_service(
    workspace_service: WorkspaceService,
    session_factory: Callable[[str], Session],
) -> ExportService:
    return ExportService(
        workspace_service=workspace_service,
        session_factory=session_factory,
        ws_manager=None,
        engine_version="0.1.0",
    )


@pytest.fixture(autouse=True)
def clear_task_registry() -> Iterator[None]:
    task_registry.clear()
    yield
    task_registry.clear()


# ---- happy:create + execute md / chaptered_md ------------------------------


class TestExecuteMd:
    def test_full_scope_md_writes_zip_with_book_and_meta(
        self,
        export_service: ExportService,
        workspace_service: WorkspaceService,
        session_factory: Callable[[str], Session],
        workspace_id: str,
    ) -> None:
        _seed_db(session_factory, workspace_id, chapter_count=2)
        _write_final_md(
            workspace_service, workspace_id, "ch-0001", "第一章正文 ABC"
        )
        _write_final_md(
            workspace_service, workspace_id, "ch-0002", "第二章正文 DEF"
        )
        _write_card_json(
            workspace_service,
            workspace_id,
            "ch-0001",
            title="启程",
            chapter_number=1,
        )
        _write_card_json(
            workspace_service,
            workspace_id,
            "ch-0002",
            title="转折",
            chapter_number=2,
        )

        task_id = task_registry.new_task_id()
        task_registry.record_pending(task_id, type="workspace_export")
        export_id, manifest = export_service.create_export(
            workspace_id,
            task_id=task_id,
            format="md",
            scope="full",
            volume_id=None,
            start_chapter_id=None,
            end_chapter_id=None,
            include_metadata=True,
            include_review_history=False,
        )
        assert export_id.startswith("exp_")
        assert manifest["status"] == "pending"

        result = export_service.execute(
            workspace_id, task_id=task_id, export_id=export_id
        )
        assert result["status"] == "done"
        assert result["chapter_count"] == 2

        zip_path = export_service.get_download_path(workspace_id, export_id)
        with zipfile.ZipFile(zip_path) as zf:
            names = zf.namelist()
            assert "book.md" in names
            assert "meta.json" in names
            book = zf.read("book.md").decode("utf-8")
            assert "卷 1: 开端卷" in book
            assert "第 1 章: 启程" in book
            assert "第 2 章: 转折" in book
            assert "第一章正文 ABC" in book
            assert "第二章正文 DEF" in book
            meta = json.loads(zf.read("meta.json").decode("utf-8"))
            assert meta["workspace_id"] == workspace_id
            assert meta["chapter_count"] == 2
            assert meta["volume_count"] == 1
            assert meta["engine_version"] == "0.1.0"
            assert meta["format"] == "md"

        # task_registry done
        info = task_registry.get(task_id)
        assert info is not None
        assert info.status == "done"

    def test_chaptered_md_writes_per_chapter_files_plus_index(
        self,
        export_service: ExportService,
        workspace_service: WorkspaceService,
        session_factory: Callable[[str], Session],
        workspace_id: str,
    ) -> None:
        _seed_db(session_factory, workspace_id, chapter_count=2)
        _write_final_md(workspace_service, workspace_id, "ch-0001", "正文 A")
        _write_final_md(workspace_service, workspace_id, "ch-0002", "正文 B")
        _write_card_json(
            workspace_service,
            workspace_id,
            "ch-0001",
            title="一",
            chapter_number=1,
        )
        _write_card_json(
            workspace_service,
            workspace_id,
            "ch-0002",
            title="二",
            chapter_number=2,
        )

        task_id = task_registry.new_task_id()
        task_registry.record_pending(task_id, type="workspace_export")
        export_id, _ = export_service.create_export(
            workspace_id,
            task_id=task_id,
            format="chaptered_md",
            scope="full",
            volume_id=None,
            start_chapter_id=None,
            end_chapter_id=None,
            include_metadata=True,
            include_review_history=False,
        )
        export_service.execute(
            workspace_id, task_id=task_id, export_id=export_id
        )

        zip_path = export_service.get_download_path(workspace_id, export_id)
        with zipfile.ZipFile(zip_path) as zf:
            names = sorted(zf.namelist())
            assert "index.md" in names
            assert "meta.json" in names
            assert "volume-1/chapter-0001.md" in names
            assert "volume-1/chapter-0002.md" in names
            ch1 = zf.read("volume-1/chapter-0001.md").decode("utf-8")
            assert "第 1 章: 一" in ch1
            assert "正文 A" in ch1
            idx = zf.read("index.md").decode("utf-8")
            assert "卷 1: 开端卷" in idx
            assert "volume-1/chapter-0001.md" in idx

    def test_list_exports_orders_by_created_desc(
        self,
        export_service: ExportService,
        workspace_service: WorkspaceService,
        session_factory: Callable[[str], Session],
        workspace_id: str,
    ) -> None:
        _seed_db(session_factory, workspace_id, chapter_count=1)
        _write_final_md(workspace_service, workspace_id, "ch-0001", "x")
        for _ in range(3):
            task_id = task_registry.new_task_id()
            task_registry.record_pending(task_id, type="workspace_export")
            export_id, _ = export_service.create_export(
                workspace_id,
                task_id=task_id,
                format="md",
                scope="full",
                volume_id=None,
                start_chapter_id=None,
                end_chapter_id=None,
                include_metadata=True,
                include_review_history=False,
            )
            export_service.execute(
                workspace_id, task_id=task_id, export_id=export_id
            )
        items = export_service.list_exports(workspace_id)
        assert len(items) == 3
        # 倒序:每条 created_at 字符串非递增
        created = [it["created_at"] for it in items]
        assert created == sorted(created, reverse=True)


# ---- error:format / scope / not_found ---------------------------------------


class TestErrors:
    def test_unknown_format_returns_unsupported(
        self,
        export_service: ExportService,
        session_factory: Callable[[str], Session],
        workspace_id: str,
    ) -> None:
        """P6-B-06 起 4 格式(md / chaptered_md / epub / docx)全部支持;
        service 层 _SUPPORTED_FORMATS 校验对未知 format 字符串(例
        ``"pdf"``)抛 ExportFormatUnsupportedError。

        注:路由层 Pydantic ``ExportFormat`` Literal 已限制 4 档,真实路径
        不可能触发本异常;本测仅覆盖 service 层直接调用的边界(对齐 plan
        §10 测试约定 error 类)。"""
        _seed_db(session_factory, workspace_id)
        task_id = task_registry.new_task_id()
        with pytest.raises(ExportFormatUnsupportedError) as exc:
            export_service.create_export(
                workspace_id,
                task_id=task_id,
                format="pdf",  # type: ignore[arg-type]
                scope="full",
                volume_id=None,
                start_chapter_id=None,
                end_chapter_id=None,
                include_metadata=True,
                include_review_history=False,
            )
        assert exc.value.format == "pdf"
        # 4 档已支持全集
        assert "md" in exc.value.supported_formats
        assert "chaptered_md" in exc.value.supported_formats
        assert "epub" in exc.value.supported_formats
        assert "docx" in exc.value.supported_formats

    def test_unknown_export_id_download_returns_not_found(
        self,
        export_service: ExportService,
        workspace_id: str,
        session_factory: Callable[[str], Session],
    ) -> None:
        _seed_db(session_factory, workspace_id)
        with pytest.raises(ExportNotFoundError) as exc:
            export_service.get_download_path(workspace_id, "exp_nonexistent")
        assert exc.value.export_id == "exp_nonexistent"

    def test_scope_volume_missing_volume_id_returns_invalid(
        self,
        export_service: ExportService,
        session_factory: Callable[[str], Session],
        workspace_id: str,
    ) -> None:
        _seed_db(session_factory, workspace_id)
        task_id = task_registry.new_task_id()
        with pytest.raises(ExportScopeInvalidError) as exc:
            export_service.create_export(
                workspace_id,
                task_id=task_id,
                format="md",
                scope="volume",
                volume_id=None,
                start_chapter_id=None,
                end_chapter_id=None,
                include_metadata=True,
                include_review_history=False,
            )
        assert exc.value.scope == "volume"

    def test_scope_range_missing_end_returns_invalid(
        self,
        export_service: ExportService,
        session_factory: Callable[[str], Session],
        workspace_id: str,
    ) -> None:
        _seed_db(session_factory, workspace_id)
        task_id = task_registry.new_task_id()
        with pytest.raises(ExportScopeInvalidError) as exc:
            export_service.create_export(
                workspace_id,
                task_id=task_id,
                format="md",
                scope="range",
                volume_id=None,
                start_chapter_id="ch-0001",
                end_chapter_id=None,
                include_metadata=True,
                include_review_history=False,
            )
        assert "end_chapter_id" in exc.value.reason

    def test_unknown_scope_returns_invalid(
        self,
        export_service: ExportService,
        session_factory: Callable[[str], Session],
        workspace_id: str,
    ) -> None:
        _seed_db(session_factory, workspace_id)
        task_id = task_registry.new_task_id()
        with pytest.raises(ExportScopeInvalidError):
            export_service.create_export(
                workspace_id,
                task_id=task_id,
                format="md",
                scope="unknown",
                volume_id=None,
                start_chapter_id=None,
                end_chapter_id=None,
                include_metadata=True,
                include_review_history=False,
            )


# ---- gate:invalidated 跳过 / scope=range 含 invalidated --------------------


class TestInvalidatedGate:
    def test_scope_full_silently_skips_invalidated(
        self,
        export_service: ExportService,
        workspace_service: WorkspaceService,
        session_factory: Callable[[str], Session],
        workspace_id: str,
    ) -> None:
        _seed_db(
            session_factory,
            workspace_id,
            chapter_count=3,
            invalidated_ids=["ch-0002"],
        )
        for cid, n in [("ch-0001", 1), ("ch-0002", 2), ("ch-0003", 3)]:
            _write_final_md(
                workspace_service, workspace_id, cid, f"正文 {cid}"
            )
            _write_card_json(
                workspace_service,
                workspace_id,
                cid,
                title=f"标题{n}",
                chapter_number=n,
            )

        task_id = task_registry.new_task_id()
        task_registry.record_pending(task_id, type="workspace_export")
        export_id, _ = export_service.create_export(
            workspace_id,
            task_id=task_id,
            format="md",
            scope="full",
            volume_id=None,
            start_chapter_id=None,
            end_chapter_id=None,
            include_metadata=True,
            include_review_history=False,
        )
        result = export_service.execute(
            workspace_id, task_id=task_id, export_id=export_id
        )
        # 跳过 ch-0002 → 只导出 2 章
        assert result["chapter_count"] == 2
        zip_path = export_service.get_download_path(workspace_id, export_id)
        with zipfile.ZipFile(zip_path) as zf:
            book = zf.read("book.md").decode("utf-8")
            assert "正文 ch-0001" in book
            assert "正文 ch-0003" in book
            assert "正文 ch-0002" not in book

    def test_scope_range_with_invalidated_inside_raises(
        self,
        export_service: ExportService,
        workspace_service: WorkspaceService,
        session_factory: Callable[[str], Session],
        workspace_id: str,
    ) -> None:
        _seed_db(
            session_factory,
            workspace_id,
            chapter_count=3,
            invalidated_ids=["ch-0002"],
        )
        task_id = task_registry.new_task_id()
        with pytest.raises(ExportScopeInvalidError) as exc:
            export_service.create_export(
                workspace_id,
                task_id=task_id,
                format="md",
                scope="range",
                volume_id=None,
                start_chapter_id="ch-0001",
                end_chapter_id="ch-0003",
                include_metadata=True,
                include_review_history=False,
            )
        assert exc.value.invalid_details["invalidated_chapter_ids"] == [
            "ch-0002"
        ]
        assert "unblock" in exc.value.reason or "rewind" in exc.value.reason

    def test_scope_volume_skips_invalidated(
        self,
        export_service: ExportService,
        workspace_service: WorkspaceService,
        session_factory: Callable[[str], Session],
        workspace_id: str,
    ) -> None:
        _seed_db(
            session_factory,
            workspace_id,
            chapter_count=3,
            invalidated_ids=["ch-0001"],
        )
        for cid, n in [("ch-0001", 1), ("ch-0002", 2), ("ch-0003", 3)]:
            _write_final_md(workspace_service, workspace_id, cid, f"正文 {cid}")
            _write_card_json(
                workspace_service,
                workspace_id,
                cid,
                title=f"标题{n}",
                chapter_number=n,
            )
        task_id = task_registry.new_task_id()
        task_registry.record_pending(task_id, type="workspace_export")
        export_id, _ = export_service.create_export(
            workspace_id,
            task_id=task_id,
            format="md",
            scope="volume",
            volume_id="vol-1",
            start_chapter_id=None,
            end_chapter_id=None,
            include_metadata=True,
            include_review_history=False,
        )
        result = export_service.execute(
            workspace_id, task_id=task_id, export_id=export_id
        )
        assert result["chapter_count"] == 2


# ---- v1.0.x reviewer-resilience:fallback 读 v{N}.md 当 final.md 缺失 -----


class TestFallbackToVersionMd:
    """v1.0.x reviewer-resilience(2026-05-20):问题 4 修法 — ExportService 在
    final.md 缺失时 fallback 读 v{current_version}.md。覆盖 force_pass 历史数据
    场景(status=approved 但因 mark_final 缺失导致无 final.md)。
    """

    def test_export_reads_version_md_when_final_missing(
        self,
        export_service: ExportService,
        workspace_service: WorkspaceService,
        session_factory: Callable[[str], Session],
        workspace_id: str,
    ) -> None:
        """ch-0001 status=approved + current_version=1 + 只有 v1.md(无 final.md)
        → 导出应读 v1.md 正文,不再渲染"(章节未生成)"占位。
        """
        _seed_db(session_factory, workspace_id, chapter_count=1)
        # 故意只写 v1.md,不写 final.md(模拟 force_pass 历史数据)
        _write_version_md(
            workspace_service, workspace_id, "ch-0001", 1, "第一章 force_pass 后的正文"
        )
        _write_card_json(
            workspace_service,
            workspace_id,
            "ch-0001",
            title="启程",
            chapter_number=1,
        )

        task_id = task_registry.new_task_id()
        task_registry.record_pending(task_id, type="workspace_export")
        export_id, _ = export_service.create_export(
            workspace_id,
            task_id=task_id,
            format="md",
            scope="full",
            volume_id=None,
            start_chapter_id=None,
            end_chapter_id=None,
            include_metadata=False,
            include_review_history=False,
        )
        result = export_service.execute(
            workspace_id, task_id=task_id, export_id=export_id
        )
        assert result["status"] == "done"

        zip_path = export_service.get_download_path(workspace_id, export_id)
        with zipfile.ZipFile(zip_path) as zf:
            book = zf.read("book.md").decode("utf-8")
        # 关键断言:读到了 v1.md 正文(而非"(章节未生成)"占位)
        assert "第一章 force_pass 后的正文" in book
        assert "(章节未生成)" not in book

    def test_export_prefers_final_md_when_both_exist(
        self,
        export_service: ExportService,
        workspace_service: WorkspaceService,
        session_factory: Callable[[str], Session],
        workspace_id: str,
    ) -> None:
        """final.md 存在时优先读 final.md(不被 fallback 路径影响)。"""
        _seed_db(session_factory, workspace_id, chapter_count=1)
        _write_version_md(
            workspace_service, workspace_id, "ch-0001", 1, "旧 v1 草稿"
        )
        _write_final_md(
            workspace_service, workspace_id, "ch-0001", "最终版正文"
        )
        _write_card_json(
            workspace_service,
            workspace_id,
            "ch-0001",
            title="启程",
            chapter_number=1,
        )

        task_id = task_registry.new_task_id()
        task_registry.record_pending(task_id, type="workspace_export")
        export_id, _ = export_service.create_export(
            workspace_id,
            task_id=task_id,
            format="md",
            scope="full",
            volume_id=None,
            start_chapter_id=None,
            end_chapter_id=None,
            include_metadata=False,
            include_review_history=False,
        )
        export_service.execute(
            workspace_id, task_id=task_id, export_id=export_id
        )
        zip_path = export_service.get_download_path(workspace_id, export_id)
        with zipfile.ZipFile(zip_path) as zf:
            book = zf.read("book.md").decode("utf-8")
        assert "最终版正文" in book
        assert "旧 v1 草稿" not in book

    def test_export_placeholder_when_both_missing(
        self,
        export_service: ExportService,
        workspace_service: WorkspaceService,
        session_factory: Callable[[str], Session],
        workspace_id: str,
    ) -> None:
        """final.md + v1.md 都缺 → 兜底"(章节未生成)"占位(渲染层既有约定)。"""
        _seed_db(session_factory, workspace_id, chapter_count=1)
        _write_card_json(
            workspace_service,
            workspace_id,
            "ch-0001",
            title="启程",
            chapter_number=1,
        )

        task_id = task_registry.new_task_id()
        task_registry.record_pending(task_id, type="workspace_export")
        export_id, _ = export_service.create_export(
            workspace_id,
            task_id=task_id,
            format="md",
            scope="full",
            volume_id=None,
            start_chapter_id=None,
            end_chapter_id=None,
            include_metadata=False,
            include_review_history=False,
        )
        export_service.execute(
            workspace_id, task_id=task_id, export_id=export_id
        )
        zip_path = export_service.get_download_path(workspace_id, export_id)
        with zipfile.ZipFile(zip_path) as zf:
            book = zf.read("book.md").decode("utf-8")
        assert "(章节未生成)" in book


# ---- rollback:execute 失败时不留垃圾 ----------------------------------------


class TestRollback:
    def test_execute_failure_marks_manifest_failed_no_zip(
        self,
        workspace_service: WorkspaceService,
        session_factory: Callable[[str], Session],
        workspace_id: str,
        monkeypatch: pytest.MonkeyPatch,
    ) -> None:
        _seed_db(session_factory, workspace_id, chapter_count=1)
        _write_final_md(workspace_service, workspace_id, "ch-0001", "x")
        service = ExportService(
            workspace_service=workspace_service,
            session_factory=session_factory,
            ws_manager=None,
        )
        task_id = task_registry.new_task_id()
        task_registry.record_pending(task_id, type="workspace_export")
        export_id, _ = service.create_export(
            workspace_id,
            task_id=task_id,
            format="md",
            scope="full",
            volume_id=None,
            start_chapter_id=None,
            end_chapter_id=None,
            include_metadata=True,
            include_review_history=False,
        )

        # 注入失败:monkeypatch render_markdown_full 抛错
        from src.services import export_service as svc_mod

        def boom(*args: Any, **kwargs: Any) -> None:
            raise RuntimeError("simulated render failure")

        monkeypatch.setattr(svc_mod, "render_markdown_full", boom)

        result = service.execute(
            workspace_id, task_id=task_id, export_id=export_id
        )
        assert result["status"] == "failed"
        # zip 不留(rollback)
        from src.core.workspace.workspace_paths import WorkspacePaths

        paths = WorkspacePaths(
            workspace_id, workspaces_root=workspace_service.workspaces_root
        )
        zip_path = paths.exports_dir / export_id / "export.zip"
        assert not zip_path.exists()
        # manifest 翻 failed
        manifest_path = paths.exports_dir / export_id / "manifest.json"
        manifest = json.loads(manifest_path.read_text(encoding="utf-8"))
        assert manifest["status"] == "failed"
        assert "simulated render failure" in manifest["error"]
        # task_registry 失败状态
        info = task_registry.get(task_id)
        assert info is not None
        assert info.status == "failed"


# ---- WS event:4 stage publish 验证 -----------------------------------------


class TestWsEvents:
    def test_publish_four_stages_in_order(
        self,
        workspace_service: WorkspaceService,
        session_factory: Callable[[str], Session],
        workspace_id: str,
    ) -> None:
        _seed_db(session_factory, workspace_id, chapter_count=1)
        _write_final_md(workspace_service, workspace_id, "ch-0001", "x")
        fake_ws = _FakeWSManager()
        service = ExportService(
            workspace_service=workspace_service,
            session_factory=session_factory,
            ws_manager=fake_ws,  # type: ignore[arg-type]
        )
        task_id = task_registry.new_task_id()
        task_registry.record_pending(task_id, type="workspace_export")
        export_id, _ = service.create_export(
            workspace_id,
            task_id=task_id,
            format="md",
            scope="full",
            volume_id=None,
            start_chapter_id=None,
            end_chapter_id=None,
            include_metadata=True,
            include_review_history=False,
        )
        service.execute(
            workspace_id, task_id=task_id, export_id=export_id
        )
        stages = [e["data"]["stage"] for e in fake_ws.events]
        assert stages == ["collecting", "rendering", "packaging", "done"]
        # 所有 event 都 publish 到 task.{id}.progress
        assert all(
            e["channel"] == f"task.{task_id}.progress"
            for e in fake_ws.events
        )

    def test_failure_publishes_failed_stage(
        self,
        workspace_service: WorkspaceService,
        session_factory: Callable[[str], Session],
        workspace_id: str,
        monkeypatch: pytest.MonkeyPatch,
    ) -> None:
        _seed_db(session_factory, workspace_id, chapter_count=1)
        _write_final_md(workspace_service, workspace_id, "ch-0001", "x")
        fake_ws = _FakeWSManager()
        service = ExportService(
            workspace_service=workspace_service,
            session_factory=session_factory,
            ws_manager=fake_ws,  # type: ignore[arg-type]
        )
        task_id = task_registry.new_task_id()
        task_registry.record_pending(task_id, type="workspace_export")
        export_id, _ = service.create_export(
            workspace_id,
            task_id=task_id,
            format="md",
            scope="full",
            volume_id=None,
            start_chapter_id=None,
            end_chapter_id=None,
            include_metadata=True,
            include_review_history=False,
        )
        from src.services import export_service as svc_mod

        monkeypatch.setattr(
            svc_mod,
            "render_markdown_full",
            lambda *a, **kw: (_ for _ in ()).throw(RuntimeError("x")),
        )
        service.execute(
            workspace_id, task_id=task_id, export_id=export_id
        )
        stages = [e["data"]["stage"] for e in fake_ws.events]
        assert "failed" in stages


@pytest.mark.parametrize("format", ["md", "chaptered_md", "epub", "docx"])
def test_length_notes_are_only_in_editorial_export(format, export_service, workspace_service,
    session_factory, workspace_id):
    from src.core.workspace.workspace_paths import WorkspacePaths
    _seed_db(session_factory, workspace_id, chapter_count=1)
    prose = "山门外下起雨来，她望着母亲的背影。" * 500
    _write_final_md(workspace_service, workspace_id, "ch-0001", prose)
    paths = WorkspacePaths(workspace_id, workspaces_root=workspace_service.workspaces_root)
    paths.cards_dir.mkdir(parents=True, exist_ok=True)
    card = {"chapter_id": "ch-0001", "title": "春雨", "chapter_number": 1, "word_count_max": 3500}
    source = json.dumps(card, ensure_ascii=False)
    (paths.cards_dir / "ch-0001.json").write_text(source)
    for editorial in (True, False):
        task_id = task_registry.new_task_id()
        task_registry.record_pending(task_id, type="workspace_export")
        export_id, _ = export_service.create_export(workspace_id, task_id=task_id,
            format=format, scope="full", volume_id=None, start_chapter_id=None, end_chapter_id=None,
            include_metadata=False, include_review_history=False, include_length_notes=editorial)
        result = export_service.execute(workspace_id, task_id=task_id, export_id=export_id)
        assert result["status"] == "done"
        with zipfile.ZipFile(export_service.get_download_path(workspace_id, export_id)) as bundle:
            if format in {"md", "chaptered_md"}:
                exported = "".join(bundle.read(name).decode() for name in bundle.namelist() if name.endswith(".md"))
            else:
                import io
                file_name = next(name for name in bundle.namelist() if name.endswith("." + format))
                with zipfile.ZipFile(io.BytesIO(bundle.read(file_name))) as inner:
                    exported = "".join(inner.read(name).decode() for name in inner.namelist()
                        if name.endswith((".xml", ".xhtml", ".html", ".ncx")))
        assert ("人工分章" in exported) is editorial
        assert "春雨" in exported
    assert (paths.cards_dir / "ch-0001.json").read_text() == source
    assert paths.chapter_version_file("ch-0001", "final.md").read_text() == prose
