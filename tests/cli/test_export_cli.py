"""CLI export 3 子命令测试(P7-B-03)。

测试覆盖(对齐 plan §"P7 专属:CLI 等价补齐 4 大模块测试约定" 4 类):

- **happy path**:3 子命令各 1 测调通 + stdout / JSON 双格式 + exit code 0
- **error path**:不存在 workspace / 不存在 export_id / 无效 format(argparse choices)
  / scope=range 缺 start/end → exit code 1 + stderr 有翻译;argparse 缺 required
  → SystemExit(2)
- **gate**:4 format × scope=full path(md / chaptered_md / epub / docx 各 1 测)
  + scope=volume / scope=range 跨字段映射校验
- **session lifecycle**:run → list → download 三命令往返,共享 engine 无 lock 残留

mock 策略
=========

- ``commands.make_workspace_service``:monkeypatch 成 tmp_path WorkspaceService
- ``commands.make_export_service``:monkeypatch 注入带 in-memory engine
  session_factory + ws_manager 透传(``cmd_export_run`` 注入 ``_CLIStageObserver``)
- 不依赖物理 fixture(全走 tmp_path + WorkspaceService 自动创建工作区目录);
  ExportService 真渲染 md / chaptered_md / epub / docx 全 4 格式(ebooklib /
  python-docx 已装入,P6-B-05 / P6-B-06 留),内容空 → render 占位文本
"""

from __future__ import annotations

import json
import zipfile
from collections.abc import Callable
from datetime import UTC, datetime
from pathlib import Path
from typing import Any

import pytest
from sqlalchemy import create_engine
from sqlalchemy.orm import Session
from sqlalchemy.pool import StaticPool

from src.cli import commands as cmd_module
from src.cli.commands import dispatch
from src.cli.main import build_parser
from src.core.db import Base
from src.core.db.models import VolumeORM, WorkspaceORM
from src.services.export_service import ExportService
from src.services.workspace_service import WorkspaceService


# ---------------------------------------------------------------------------
# Fixtures
# ---------------------------------------------------------------------------


@pytest.fixture
def workspaces_root(tmp_path: Path) -> Path:
    root = tmp_path / "workspaces"
    root.mkdir()
    return root


@pytest.fixture
def archives_root(tmp_path: Path) -> Path:
    root = tmp_path / "archives"
    root.mkdir()
    return root


@pytest.fixture
def shared_engine():
    """单条 in-memory engine 复用,StaticPool 跨 Session 共享同一物理连接
    (对齐 tests/cli/test_pipeline_cli.py 同模式;ExportService 每次开新 session
    + 关 session,需 StaticPool 保证 ORM 行跨 session 可见)。
    """
    engine = create_engine(
        "sqlite:///:memory:",
        connect_args={"check_same_thread": False},
        poolclass=StaticPool,
    )
    Base.metadata.create_all(engine)
    return engine


@pytest.fixture
def session_factory(shared_engine) -> Callable[[str], Session]:
    def factory(workspace_id: str) -> Session:
        return Session(shared_engine)

    return factory


@pytest.fixture(autouse=True)
def _patch_factories(
    monkeypatch: pytest.MonkeyPatch,
    workspaces_root: Path,
    archives_root: Path,
    session_factory: Callable[[str], Session],
) -> None:
    """统一替换 CLI 工厂:WorkspaceService 走 tmp_path,ExportService 走共享
    in-memory engine + ws_manager 透传(``cmd_export_run`` 注入
    ``_CLIStageObserver`` 时由本工厂转发给 ExportService 构造)。
    """

    def _make_ws() -> WorkspaceService:
        return WorkspaceService(
            workspaces_root=workspaces_root,
            archives_root=archives_root,
        )

    def _make_export(
        ws_service: WorkspaceService, *, ws_manager: Any = None
    ) -> ExportService:
        return ExportService(
            workspace_service=ws_service,
            session_factory=session_factory,
            ws_manager=ws_manager,
        )

    monkeypatch.setattr(cmd_module, "make_workspace_service", _make_ws)
    monkeypatch.setattr(cmd_module, "make_export_service", _make_export)


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------


def run_cli(args: list[str]) -> int:
    """走完整 ``build_parser → parse_args → dispatch`` 链路,返退出码。"""
    parser = build_parser()
    parsed = parser.parse_args(args)
    return dispatch(parsed)


def _extract_json_line(stdout: str) -> dict[str, Any]:
    """从 stdout 提取 ``--json`` 输出行(沿用 test_pipeline_cli.py 同 helper)。

    structlog 日志走 stdout 与 CLI 的 JSON 输出会混在一起;反向扫"以 ``{``
    起始且能被 ``json.loads`` 解析"的行,每命令只 print 1 行 JSON。
    """
    for line in reversed(stdout.splitlines()):
        line = line.strip()
        if line.startswith("{"):
            return json.loads(line)
    raise AssertionError(f"未在 stdout 找到 JSON 行: {stdout!r}")


def _create_workspace(workspace_id: str = "ws-exp") -> None:
    """通过 WorkspaceService 创建 meta.json + 工作区目录。"""
    ws_service = cmd_module.make_workspace_service()
    ws_service.create(
        workspace_id,
        display_name="export CLI test",
        genre="xianxia",
        premise="test premise",
        activate=False,
    )


def _seed_workspace_and_volume(
    session_factory: Callable[[str], Session],
    workspace_id: str,
    *,
    volumes: list[tuple[str, int]] | None = None,
) -> None:
    """种 WorkspaceORM + 多卷 VolumeORM(对齐 test_pipeline_cli.py 同 helper)。

    ``volumes`` 默认 ``[("vol-1", 3)]`` = 1 卷 3 章(ch-0001..ch-0003)。
    """
    vols = volumes if volumes is not None else [("vol-1", 3)]
    session = session_factory(workspace_id)
    try:
        now = datetime.now(UTC)
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
                total_chapters=sum(c for _, c in vols),
                completed_chapters=0,
                config_overrides={},
            )
        )
        for vid, count in vols:
            session.merge(
                VolumeORM(
                    volume_id=vid,
                    workspace_id=workspace_id,
                    title=vid.upper(),
                    summary="s",
                    chapter_count=count,
                    tension_template="heros_journey",
                    arcs_json=[],
                    tension_curve_json=[],
                    created_at=now,
                    last_updated_at=now,
                )
            )
        session.commit()
    finally:
        session.close()


# ---------------------------------------------------------------------------
# 1. happy path(3 子命令各 1 测调通 + stdout / JSON 双格式)
# ---------------------------------------------------------------------------


class TestExportRunHappy:
    def test_run_md_full_human_readable_output(
        self,
        session_factory: Callable[[str], Session],
        capsys: pytest.CaptureFixture,
    ) -> None:
        _create_workspace("ws-md")
        _seed_workspace_and_volume(session_factory, "ws-md")

        rc = run_cli(
            [
                "export",
                "run",
                "ws-md",
                "--format",
                "md",
                "--scope",
                "full",
            ]
        )
        assert rc == 0
        captured = capsys.readouterr()
        out = captured.out
        err = captured.err
        assert "status            : done" in out
        assert "format            : md" in out
        assert "scope             : full" in out
        assert "chapter_count     : 3" in out
        # stage observer 推 stderr(collecting / rendering / packaging / done)
        assert "stage=collecting" in err
        assert "stage=rendering" in err
        assert "stage=packaging" in err
        assert "stage=done" in err

    def test_run_chaptered_md_json_output(
        self,
        session_factory: Callable[[str], Session],
        capsys: pytest.CaptureFixture,
    ) -> None:
        _create_workspace("ws-chap")
        _seed_workspace_and_volume(session_factory, "ws-chap")

        rc = run_cli(
            [
                "export",
                "run",
                "ws-chap",
                "--format",
                "chaptered_md",
                "--scope",
                "full",
                "--json",
            ]
        )
        assert rc == 0
        payload = _extract_json_line(capsys.readouterr().out)
        assert payload["status"] == "done"
        assert payload["format"] == "chaptered_md"
        assert payload["scope"] == "full"
        assert payload["chapter_count"] == 3
        assert payload["export_id"].startswith("exp_")
        assert payload["task_id"].startswith("task_")


class TestExportListHappy:
    def test_list_after_run_returns_one_item(
        self,
        session_factory: Callable[[str], Session],
        capsys: pytest.CaptureFixture,
    ) -> None:
        _create_workspace("ws-list")
        _seed_workspace_and_volume(session_factory, "ws-list")

        # run 一次产出
        assert (
            run_cli(
                [
                    "export",
                    "run",
                    "ws-list",
                    "--format",
                    "md",
                    "--scope",
                    "full",
                ]
            )
            == 0
        )
        capsys.readouterr()

        # list
        rc = run_cli(["export", "list", "ws-list"])
        assert rc == 0
        out = capsys.readouterr().out
        assert "export_count      : 1" in out
        assert "format         : md" in out
        assert "status         : done" in out

    def test_list_empty_workspace_returns_zero_count(
        self,
        capsys: pytest.CaptureFixture,
    ) -> None:
        _create_workspace("ws-empty")
        rc = run_cli(["export", "list", "ws-empty"])
        assert rc == 0
        out = capsys.readouterr().out
        assert "export_count      : 0" in out
        assert "(workspace 当前无任何导出条目)" in out


class TestExportDownloadHappy:
    def test_download_writes_zip_with_correct_size(
        self,
        session_factory: Callable[[str], Session],
        capsys: pytest.CaptureFixture,
        tmp_path: Path,
    ) -> None:
        _create_workspace("ws-dl")
        _seed_workspace_and_volume(session_factory, "ws-dl")

        # run 拿 export_id
        assert (
            run_cli(
                [
                    "export",
                    "run",
                    "ws-dl",
                    "--format",
                    "md",
                    "--scope",
                    "full",
                    "--json",
                ]
            )
            == 0
        )
        payload = _extract_json_line(capsys.readouterr().out)
        export_id = payload["export_id"]
        expected_size = payload["file_size_bytes"]

        # download 写到 tmp_path / sub / out.zip(测父目录自动创建)
        out_file = tmp_path / "sub" / "out.zip"
        rc = run_cli(
            [
                "export",
                "download",
                "ws-dl",
                export_id,
                "--out",
                str(out_file),
            ]
        )
        assert rc == 0
        captured_out = capsys.readouterr().out
        assert out_file.is_file()
        assert out_file.stat().st_size == expected_size
        assert "output_path" in captured_out
        assert f"file_size_bytes   : {expected_size}" in captured_out

        # 验证 zip 内容正确(book.md + meta.json 默认 include_metadata=True)
        with zipfile.ZipFile(out_file, "r") as zf:
            names = set(zf.namelist())
        assert "book.md" in names
        assert "meta.json" in names


# ---------------------------------------------------------------------------
# 2. error path(不存在 ws / 不存在 export_id / 无效 format / scope 缺字段)
# ---------------------------------------------------------------------------


class TestExportErrors:
    def test_run_workspace_not_found_business_error(
        self,
        capsys: pytest.CaptureFixture,
    ) -> None:
        """workspace 不存在 → WorkspaceService.info 抛 MetaFileError → rc=1。"""
        rc = run_cli(
            [
                "export",
                "run",
                "ws-ghost",
                "--format",
                "md",
                "--scope",
                "full",
            ]
        )
        assert rc == 1
        err = capsys.readouterr().err
        assert "错误" in err

    def test_run_invalid_format_argparse_error(self) -> None:
        """argparse ``choices=[...]`` 拒绝非法 format → SystemExit(2)。"""
        with pytest.raises(SystemExit) as exc_info:
            run_cli(
                [
                    "export",
                    "run",
                    "ws-x",
                    "--format",
                    "pdf",  # 非 4 档之一
                    "--scope",
                    "full",
                ]
            )
        assert exc_info.value.code == 2

    def test_run_missing_required_scope_argparse_error(self) -> None:
        """argparse ``required=True`` 校验缺失 --scope → SystemExit(2)。"""
        with pytest.raises(SystemExit) as exc_info:
            run_cli(
                ["export", "run", "ws-x", "--format", "md"]
            )
        assert exc_info.value.code == 2

    def test_download_export_id_not_found_business_error(
        self,
        capsys: pytest.CaptureFixture,
        tmp_path: Path,
    ) -> None:
        """不存在的 export_id → ExportNotFoundError → rc=1。"""
        _create_workspace("ws-404")
        rc = run_cli(
            [
                "export",
                "download",
                "ws-404",
                "exp_nonexistent",
                "--out",
                str(tmp_path / "x.zip"),
            ]
        )
        assert rc == 1
        err = capsys.readouterr().err
        assert "错误" in err


# ---------------------------------------------------------------------------
# 3. gate(4 format × scope full + scope=volume / scope=range 跨字段校验)
# ---------------------------------------------------------------------------


class TestExportFormatGate:
    """4 format × scope=full path 各 1 测,验真渲染落 zip(对齐 plan §自决项)。"""

    @pytest.mark.parametrize(
        ("format_name", "expected_filename"),
        [
            ("md", "book.md"),
            ("chaptered_md", "index.md"),
            ("epub", "book.epub"),
            ("docx", "book.docx"),
        ],
    )
    def test_run_each_format_writes_expected_files(
        self,
        format_name: str,
        expected_filename: str,
        session_factory: Callable[[str], Session],
        capsys: pytest.CaptureFixture,
        tmp_path: Path,
    ) -> None:
        ws_id = f"ws-gate-{format_name}"
        _create_workspace(ws_id)
        _seed_workspace_and_volume(session_factory, ws_id)

        rc = run_cli(
            [
                "export",
                "run",
                ws_id,
                "--format",
                format_name,
                "--scope",
                "full",
                "--json",
            ]
        )
        assert rc == 0, f"format={format_name} failed"
        payload = _extract_json_line(capsys.readouterr().out)
        assert payload["status"] == "done"
        assert payload["format"] == format_name

        # download 验证 zip 内文件存在
        export_id = payload["export_id"]
        out_file = tmp_path / f"{format_name}.zip"
        assert (
            run_cli(
                [
                    "export",
                    "download",
                    ws_id,
                    export_id,
                    "--out",
                    str(out_file),
                ]
            )
            == 0
        )
        capsys.readouterr()
        with zipfile.ZipFile(out_file, "r") as zf:
            names = set(zf.namelist())
        assert expected_filename in names, (
            f"format={format_name} 缺 {expected_filename}: 实有 {names}"
        )


class TestExportScopeGate:
    def test_run_scope_volume_writes_volume_chapters(
        self,
        session_factory: Callable[[str], Session],
        capsys: pytest.CaptureFixture,
    ) -> None:
        """scope=volume 必填 --volume-id,正常路径返目标卷的章节数。"""
        _create_workspace("ws-vol")
        # 2 卷:vol-1 (2 章) + vol-2 (3 章)
        _seed_workspace_and_volume(
            session_factory,
            "ws-vol",
            volumes=[("vol-1", 2), ("vol-2", 3)],
        )
        rc = run_cli(
            [
                "export",
                "run",
                "ws-vol",
                "--format",
                "md",
                "--scope",
                "volume",
                "--volume-id",
                "vol-2",
                "--json",
            ]
        )
        assert rc == 0
        payload = _extract_json_line(capsys.readouterr().out)
        assert payload["status"] == "done"
        assert payload["chapter_count"] == 3

    def test_run_scope_range_missing_end_invalid_scope_error(
        self,
        session_factory: Callable[[str], Session],
        capsys: pytest.CaptureFixture,
    ) -> None:
        """scope=range 缺 --end → service 抛 ExportScopeInvalidError → rc=1。"""
        _create_workspace("ws-bad")
        _seed_workspace_and_volume(session_factory, "ws-bad")
        rc = run_cli(
            [
                "export",
                "run",
                "ws-bad",
                "--format",
                "md",
                "--scope",
                "range",
                "--start",
                "ch-0001",
            ]
        )
        assert rc == 1
        err = capsys.readouterr().err
        assert "range" in err
        assert "end_chapter_id" in err

    def test_run_scope_volume_missing_volume_id_invalid_scope_error(
        self,
        session_factory: Callable[[str], Session],
        capsys: pytest.CaptureFixture,
    ) -> None:
        """scope=volume 缺 --volume-id → service 抛 ExportScopeInvalidError → rc=1。"""
        _create_workspace("ws-novol")
        _seed_workspace_and_volume(session_factory, "ws-novol")
        rc = run_cli(
            [
                "export",
                "run",
                "ws-novol",
                "--format",
                "md",
                "--scope",
                "volume",
            ]
        )
        assert rc == 1
        err = capsys.readouterr().err
        assert "volume_id" in err


# ---------------------------------------------------------------------------
# 4. session lifecycle(run → list → download 往返,共享 engine 无 lock 残留)
# ---------------------------------------------------------------------------


class TestExportSessionLifecycle:
    def test_run_list_download_round_trip_no_lock(
        self,
        session_factory: Callable[[str], Session],
        capsys: pytest.CaptureFixture,
        tmp_path: Path,
    ) -> None:
        """连续 3 个 CLI 命令(run → list → download),共享 engine 但每命令
        独立 session;无 lock 残留(若 session 未 close,第二条命令会 SQLite
        'database is locked' 抛错);最终 zip 物理文件可读 + 字节匹配。
        """
        _create_workspace("ws-rt")
        _seed_workspace_and_volume(session_factory, "ws-rt")

        # 1) run --json 拿 export_id
        assert (
            run_cli(
                [
                    "export",
                    "run",
                    "ws-rt",
                    "--format",
                    "md",
                    "--scope",
                    "full",
                    "--json",
                ]
            )
            == 0
        )
        run_payload = _extract_json_line(capsys.readouterr().out)
        export_id = run_payload["export_id"]
        expected_size = run_payload["file_size_bytes"]

        # 2) list --json 应含此 export_id
        assert run_cli(["export", "list", "ws-rt", "--json"]) == 0
        list_payload = _extract_json_line(capsys.readouterr().out)
        item_ids = [item["export_id"] for item in list_payload["items"]]
        assert export_id in item_ids
        assert list_payload["items"][0]["status"] == "done"

        # 3) download 写文件 + 验证字节
        out_file = tmp_path / "round-trip.zip"
        assert (
            run_cli(
                [
                    "export",
                    "download",
                    "ws-rt",
                    export_id,
                    "--out",
                    str(out_file),
                    "--json",
                ]
            )
            == 0
        )
        dl_payload = _extract_json_line(capsys.readouterr().out)
        assert dl_payload["file_size_bytes"] == expected_size
        assert dl_payload["export_id"] == export_id
        assert out_file.stat().st_size == expected_size
