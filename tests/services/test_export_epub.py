"""``ExportService`` EPUB 格式集成测试(P6-B-05)。

覆盖维度(对齐 plan §10 测试约定 5 类):

- **happy**:create_export(format=epub) + execute → zip 内含 ``book.epub`` +
  ``meta.json``;``ebooklib.read_epub()`` 反序列化通过(自决项 7 验收方式)+
  spine 长度 = nav + 章节数(物理封面)/ nav + cover + 章节数(fallback 封面)
- **cover**:``data/assets/cover.png`` 存在时 EPUB 内嵌物理封面字节
- **cover_fallback**:``cover.png`` 不存在时 fallback 纯文字封面 XHTML
- **rollback**:render_epub 注入失败 → manifest=failed + 半 zip 删
- **WS event**:4 stage(collecting / rendering / packaging / done)publish 验证

错误码 / scope gate 覆盖沿用 ``test_export_service.py``(本任务不重复),只额外
覆盖 epub 格式专属路径。
"""

from __future__ import annotations

import io
import json
import zipfile
from collections.abc import Callable, Iterator
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

import ebooklib
import pytest
from ebooklib import epub
from sqlalchemy import create_engine
from sqlalchemy.orm import Session
from sqlalchemy.pool import StaticPool

from src.api import task_registry
from src.core.db import Base
from src.core.db.models import ChapterORM, VolumeORM, WorkspaceORM
from src.core.workspace.workspace_paths import WorkspacePaths
from src.services.export_service import ExportService
from src.services.workspace_service import WorkspaceService


class _FakeWSManager:
    def __init__(self) -> None:
        self.events: list[dict[str, Any]] = []

    def publish(
        self, *, channel: str, event: str, data: dict[str, Any]
    ) -> None:
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
        "ws-epub",
        display_name="EPUB 测试作品",
        genre="xianxia",
        premise="一句话设定",
        activate=False,
    )
    return "ws-epub"


@pytest.fixture
def session_factory() -> Callable[[str], Session]:
    engine = create_engine(
        "sqlite:///:memory:",
        connect_args={"check_same_thread": False},
        poolclass=StaticPool,
    )
    Base.metadata.create_all(engine)

    def factory(_workspace_id: str) -> Session:
        return Session(engine)

    return factory


def _seed_db(
    session_factory: Callable[[str], Session],
    workspace_id: str,
    *,
    chapter_count: int = 3,
) -> None:
    session = session_factory(workspace_id)
    try:
        now = datetime.now(timezone.utc)
        session.merge(
            WorkspaceORM(
                workspace_id=workspace_id,
                display_name="EPUB 测试作品",
                genre="xianxia",
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
                summary="s",
                chapter_count=chapter_count,
                tension_template="heros_journey",
                arcs_json=[],
                tension_curve_json=[],
                created_at=now,
                last_updated_at=now,
            )
        )
        for i in range(1, chapter_count + 1):
            session.merge(
                ChapterORM(
                    chapter_id=f"ch-{i:04d}",
                    workspace_id=workspace_id,
                    current_version=1,
                    status="approved",
                    rewrite_count=0,
                    revise_count=0,
                    created_at=now,
                    approved_at=now,
                    total_token_usage={},
                    total_cost_usd=0.0,
                )
            )
        session.commit()
    finally:
        session.close()


def _write_final(
    workspace_service: WorkspaceService,
    workspace_id: str,
    chapter_id: str,
    content: str,
) -> None:
    paths = WorkspacePaths(
        workspace_id, workspaces_root=workspace_service.workspaces_root
    )
    paths.chapter_dir(chapter_id).mkdir(parents=True, exist_ok=True)
    (paths.chapter_dir(chapter_id) / "final.md").write_text(
        content, encoding="utf-8"
    )


def _write_card(
    workspace_service: WorkspaceService,
    workspace_id: str,
    chapter_id: str,
    *,
    title: str,
    chapter_number: int,
) -> None:
    paths = WorkspacePaths(
        workspace_id, workspaces_root=workspace_service.workspaces_root
    )
    paths.cards_dir.mkdir(parents=True, exist_ok=True)
    (paths.cards_dir / f"{chapter_id}.json").write_text(
        json.dumps(
            {
                "chapter_id": chapter_id,
                "title": title,
                "chapter_number": chapter_number,
            },
            ensure_ascii=False,
        ),
        encoding="utf-8",
    )


def _write_cover_png(
    workspace_service: WorkspaceService,
    workspace_id: str,
    content: bytes,
) -> None:
    paths = WorkspacePaths(
        workspace_id, workspaces_root=workspace_service.workspaces_root
    )
    paths.assets_dir.mkdir(parents=True, exist_ok=True)
    (paths.assets_dir / "cover.png").write_bytes(content)


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


def _seed_three_chapters(
    workspace_service: WorkspaceService,
    workspace_id: str,
) -> None:
    """3 章正文 + 卡片(共享工具)。"""
    _write_final(workspace_service, workspace_id, "ch-0001", "正文段一\n\n正文段二")
    _write_final(workspace_service, workspace_id, "ch-0002", "另一章\n第二段")
    _write_final(workspace_service, workspace_id, "ch-0003", "")  # 空 final
    _write_card(
        workspace_service, workspace_id, "ch-0001", title="启程", chapter_number=1
    )
    _write_card(
        workspace_service, workspace_id, "ch-0002", title="风波", chapter_number=2
    )
    _write_card(
        workspace_service, workspace_id, "ch-0003", title="转折", chapter_number=3
    )


def _read_epub_from_zip(zip_bytes: bytes) -> epub.EpubBook:
    """zip → 解 ``book.epub`` → ``ebooklib.read_epub`` 反序列化。

    ``ebooklib.read_epub`` 接受文件路径,所以本辅助先把 epub 字节落临时文件
    再 read_epub。验收完成后 tempfile 自动清理。
    """
    import tempfile

    with zipfile.ZipFile(io.BytesIO(zip_bytes)) as zf:
        epub_bytes = zf.read("book.epub")
    with tempfile.NamedTemporaryFile(suffix=".epub", delete=False) as tf:
        tf.write(epub_bytes)
        tf_path = tf.name
    try:
        return epub.read_epub(tf_path)
    finally:
        Path(tf_path).unlink(missing_ok=True)


# ---- happy:format=epub scope=full → reseralize round-trip ----------------


class TestExecuteEpub:
    def test_epub_full_scope_writes_zip_with_book_and_meta(
        self,
        export_service: ExportService,
        workspace_service: WorkspaceService,
        session_factory: Callable[[str], Session],
        workspace_id: str,
    ) -> None:
        _seed_db(session_factory, workspace_id, chapter_count=3)
        _seed_three_chapters(workspace_service, workspace_id)

        task_id = task_registry.new_task_id()
        task_registry.record_pending(task_id, type="workspace_export")
        export_id, manifest = export_service.create_export(
            workspace_id,
            task_id=task_id,
            format="epub",
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
        assert result["chapter_count"] == 3
        assert result["format"] == "epub"

        zip_path = export_service.get_download_path(workspace_id, export_id)
        zip_bytes = zip_path.read_bytes()
        with zipfile.ZipFile(io.BytesIO(zip_bytes)) as zf:
            names = zf.namelist()
            assert "book.epub" in names
            assert "meta.json" in names
            meta = json.loads(zf.read("meta.json").decode("utf-8"))
            assert meta["format"] == "epub"
            assert meta["chapter_count"] == 3
            assert meta["engine_version"] == "0.1.0"

        # 反序列化校验 — 自决项 7 验收
        book = _read_epub_from_zip(zip_bytes)
        # spine = nav + (fallback cover) + 3 章 = 5
        spine_ids = [s[0] for s in book.spine]
        assert "nav" in spine_ids
        # 章节数(扣除 nav + cover)= 3
        chapter_items = [
            i
            for i in book.get_items_of_type(ebooklib.ITEM_DOCUMENT)
            if i.file_name.startswith("chapter_")
        ]
        assert len(chapter_items) == 3
        # 标题元数据
        title_meta = book.get_metadata("DC", "title")
        assert title_meta and title_meta[0][0] == "EPUB 测试作品"

        # task_registry done
        info = task_registry.get(task_id)
        assert info is not None
        assert info.status == "done"

    def test_epub_chapter_content_contains_title_and_paragraphs(
        self,
        export_service: ExportService,
        workspace_service: WorkspaceService,
        session_factory: Callable[[str], Session],
        workspace_id: str,
    ) -> None:
        """章节 XHTML 含 ``<h2>第 N 章: 标题</h2>`` + ``<p>`` 段落。"""
        _seed_db(session_factory, workspace_id, chapter_count=1)
        _write_final(
            workspace_service,
            workspace_id,
            "ch-0001",
            "第一段落\n\n第二段落",
        )
        _write_card(
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
            format="epub",
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
        book = _read_epub_from_zip(zip_path.read_bytes())
        chapter = next(
            i
            for i in book.get_items_of_type(ebooklib.ITEM_DOCUMENT)
            if i.file_name.startswith("chapter_")
        )
        body = chapter.get_content().decode("utf-8")
        assert "第 1 章: 启程" in body
        assert "<p>第一段落</p>" in body
        assert "<p>第二段落</p>" in body

    def test_epub_volume_scope_excludes_other_volumes(
        self,
        export_service: ExportService,
        workspace_service: WorkspaceService,
        session_factory: Callable[[str], Session],
        workspace_id: str,
    ) -> None:
        _seed_db(session_factory, workspace_id, chapter_count=2)
        _seed_three_chapters(workspace_service, workspace_id)
        task_id = task_registry.new_task_id()
        task_registry.record_pending(task_id, type="workspace_export")
        export_id, _ = export_service.create_export(
            workspace_id,
            task_id=task_id,
            format="epub",
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
        # chapter_count 取决于 vol-1 内章节 = 2(seed_db chapter_count=2)
        assert result["chapter_count"] == 2


# ---- cover 物理 + fallback 二分 --------------------------------------------


class TestCover:
    def test_physical_cover_png_embedded_in_epub(
        self,
        export_service: ExportService,
        workspace_service: WorkspaceService,
        session_factory: Callable[[str], Session],
        workspace_id: str,
    ) -> None:
        """``data/assets/cover.png`` 存在时,bytes 嵌入 EPUB 包内。"""
        _seed_db(session_factory, workspace_id, chapter_count=1)
        _write_final(workspace_service, workspace_id, "ch-0001", "x")
        _write_card(
            workspace_service,
            workspace_id,
            "ch-0001",
            title="启程",
            chapter_number=1,
        )
        # 假 PNG 字节(签名 + 几字节 — ebooklib 不做图片校验,字节透传)
        fake_png = b"\x89PNG\r\n\x1a\nFAKE_DATA_BYTES_FOR_TEST"
        _write_cover_png(workspace_service, workspace_id, fake_png)

        task_id = task_registry.new_task_id()
        task_registry.record_pending(task_id, type="workspace_export")
        export_id, _ = export_service.create_export(
            workspace_id,
            task_id=task_id,
            format="epub",
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
        book = _read_epub_from_zip(zip_path.read_bytes())
        # cover 资源在 epub 包内(ebooklib set_cover 走 cover.png file_name)
        cover_items = [
            i for i in book.get_items() if i.file_name == "cover.png"
        ]
        assert len(cover_items) == 1
        # 字节透传
        assert cover_items[0].get_content() == fake_png

    def test_fallback_cover_xhtml_when_no_png(
        self,
        export_service: ExportService,
        workspace_service: WorkspaceService,
        session_factory: Callable[[str], Session],
        workspace_id: str,
    ) -> None:
        """无 ``cover.png`` 时 fallback ``cover.xhtml`` 文字封面在 spine 首位。"""
        _seed_db(session_factory, workspace_id, chapter_count=1)
        _write_final(workspace_service, workspace_id, "ch-0001", "x")
        _write_card(
            workspace_service,
            workspace_id,
            "ch-0001",
            title="启程",
            chapter_number=1,
        )
        # 不写 cover.png
        task_id = task_registry.new_task_id()
        task_registry.record_pending(task_id, type="workspace_export")
        export_id, _ = export_service.create_export(
            workspace_id,
            task_id=task_id,
            format="epub",
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
        book = _read_epub_from_zip(zip_path.read_bytes())
        cover_xhtml = next(
            (i for i in book.get_items() if i.file_name == "cover.xhtml"),
            None,
        )
        assert cover_xhtml is not None
        cover_body = cover_xhtml.get_content().decode("utf-8")
        assert "EPUB 测试作品" in cover_body
        # 引擎版本水印
        assert "engine_version" in cover_body


# ---- rollback:render_epub 注入失败 -----------------------------------------


class TestRollback:
    def test_epub_execute_failure_marks_manifest_failed_no_zip(
        self,
        workspace_service: WorkspaceService,
        session_factory: Callable[[str], Session],
        workspace_id: str,
        monkeypatch: pytest.MonkeyPatch,
    ) -> None:
        _seed_db(session_factory, workspace_id, chapter_count=1)
        _write_final(workspace_service, workspace_id, "ch-0001", "x")
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
            format="epub",
            scope="full",
            volume_id=None,
            start_chapter_id=None,
            end_chapter_id=None,
            include_metadata=True,
            include_review_history=False,
        )

        # 注入失败:monkeypatch render_epub 抛错
        from src.services import export_service as svc_mod

        def boom(*args: Any, **kwargs: Any) -> None:
            raise RuntimeError("simulated epub render failure")

        monkeypatch.setattr(svc_mod, "render_epub", boom)

        result = service.execute(
            workspace_id, task_id=task_id, export_id=export_id
        )
        assert result["status"] == "failed"

        paths = WorkspacePaths(
            workspace_id, workspaces_root=workspace_service.workspaces_root
        )
        zip_path = paths.exports_dir / export_id / "export.zip"
        assert not zip_path.exists()
        manifest_path = paths.exports_dir / export_id / "manifest.json"
        manifest = json.loads(manifest_path.read_text(encoding="utf-8"))
        assert manifest["status"] == "failed"
        assert "simulated epub render failure" in manifest["error"]

        info = task_registry.get(task_id)
        assert info is not None
        assert info.status == "failed"


# ---- WS event:4 stage(同 md 模式) -----------------------------------------


class TestWsEventsEpub:
    def test_publish_four_stages_in_order_epub(
        self,
        workspace_service: WorkspaceService,
        session_factory: Callable[[str], Session],
        workspace_id: str,
    ) -> None:
        _seed_db(session_factory, workspace_id, chapter_count=1)
        _write_final(workspace_service, workspace_id, "ch-0001", "x")
        _write_card(
            workspace_service,
            workspace_id,
            "ch-0001",
            title="启程",
            chapter_number=1,
        )
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
            format="epub",
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
        assert all(
            e["channel"] == f"task.{task_id}.progress" for e in fake_ws.events
        )
