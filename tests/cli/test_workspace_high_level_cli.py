"""CLI workspace 高阶动作测试(P7-B-04)。

测试覆盖(对齐 plan §"P7 专属:CLI 等价补齐 4 大模块测试约定"4 类):

- **happy path**:4 子命令各 1-2 测调通 + stdout / ``--json`` 双格式
- **error path**:不存在 workspace / version mismatch / backup_id 不存在 /
  restore confirm mismatch → rc=1 / SystemExit(2)
- **gate**:version-status 三种状态(up-to-date / migration_required /
  unknown_schema)+ migrate 已最新幂等 + restore --latest 取首项 + restore
  argparse 互斥校验
- **session lifecycle**:migrate → version-status → backups 三命令往返,
  无 lock 残留(每命令独立工厂调用 + 进程退出前 commit;tmp_path 真物理目录)

mock 策略
=========

- ``commands.make_workspace_service``:monkeypatch 成 tmp_path 版本
- ``commands.make_backup_service`` / ``make_workspace_version_service``:**沿用
  默认工厂**(真 BackupService + 真 WorkspaceVersionService),只是 workspaces_root
  由 fake make_workspace_service 注入 tmp_path
- ``isolate_alembic_env`` fixture 隔离 NOVEL_DB_PATH 环境变量(对齐
  tests/services/test_workspace_version_service.py 同模式)
- 不依赖物理 fixture(全走 tmp_path / WorkspaceService 自动创建工作区目录 +
  真 alembic upgrade 创建 sqlite 表)
"""

from __future__ import annotations

import json
import sqlite3
from collections.abc import Iterator
from datetime import UTC, datetime
from pathlib import Path
from typing import Any

import pytest
from sqlalchemy import create_engine
from sqlalchemy.orm import Session

from src.cli import commands as cmd_module
from src.cli.commands import dispatch
from src.cli.main import build_parser
from src.core.db import Base, WorkspaceORM
from src.core.workspace.workspace_paths import WorkspacePaths
from src.services.backup_service import BackupService
from src.services.workspace_service import WorkspaceService
from src.services.workspace_version_service import get_current_schema_version
from src.version import ENGINE_VERSION


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


@pytest.fixture(autouse=True)
def _patch_workspace_factory(
    monkeypatch: pytest.MonkeyPatch,
    workspaces_root: Path,
    archives_root: Path,
) -> None:
    """统一替换 CLI ``make_workspace_service`` 工厂走 tmp_path。

    ``make_backup_service`` / ``make_workspace_version_service`` 不替换 — 真服务
    内部已经接 workspace_service.workspaces_root,会自动跟随 tmp_path。
    """

    def _make_ws() -> WorkspaceService:
        return WorkspaceService(
            workspaces_root=workspaces_root,
            archives_root=archives_root,
        )

    monkeypatch.setattr(cmd_module, "make_workspace_service", _make_ws)


@pytest.fixture(autouse=True)
def _isolate_alembic_env(monkeypatch: pytest.MonkeyPatch) -> Iterator[None]:
    """确保测试期间 NOVEL_DB_PATH 由本测试控制,不串到其他测试。

    ``WorkspaceVersionService._run_alembic_upgrade`` 通过 os.environ 临时设置 +
    finally 还原原值;为防外层若提前设置串入,这里 monkeypatch 清空确保
    清白起点(monkeypatch 在 fixture teardown 时自动恢复)。
    """
    monkeypatch.delenv("NOVEL_DB_PATH", raising=False)
    yield


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------


def run_cli(args: list[str]) -> int:
    """走完整 ``build_parser → parse_args → dispatch`` 链路,返退出码。"""
    parser = build_parser()
    parsed = parser.parse_args(args)
    return dispatch(parsed)


def _extract_json_line(stdout: str) -> dict[str, Any]:
    """从 stdout 提取 ``--json`` 输出行(对齐 P7-B-02 / B-03 同 helper)。

    structlog 日志走 stdout(对齐 ``src/api/logging.py`` ``logging.basicConfig
    stream=sys.stdout``),与 CLI 的 JSON 输出会混在一起。反向扫"最后一行
    以 ``{`` 起始且能被 ``json.loads`` 解析"。
    """
    for line in reversed(stdout.splitlines()):
        line = line.strip()
        if line.startswith("{"):
            return json.loads(line)
    raise AssertionError(f"未在 stdout 找到 JSON 行: {stdout!r}")


def _create_workspace(workspace_id: str = "ws-v") -> None:
    """通过 fake make_workspace_service 创建工作区(skeleton + meta.json)。

    新建 workspace 的 schema_version 字段已由 ``WorkspaceService.create``
    自动写为当前 head;engine_version=ENGINE_VERSION(0.1.0)— 默认 happy。
    """
    ws_service = cmd_module.make_workspace_service()
    ws_service.create(
        workspace_id,
        display_name="workspace high-level CLI test",
        genre="scifi",
        premise="test premise",
        activate=False,
    )


_UNCHANGED = object()


def _rewrite_meta(
    workspaces_root: Path,
    workspace_id: str,
    *,
    schema_version: Any = _UNCHANGED,
    engine_version: str | None = None,
) -> None:
    """手工改 meta.json 的 schema_version / engine_version(模拟旧工作区)。

    ``schema_version=_UNCHANGED`` (默认) 即不动该字段;``None`` 即显式写 null;
    字符串值即写入。
    """
    meta_path = workspaces_root / workspace_id / "meta.json"
    payload = json.loads(meta_path.read_text(encoding="utf-8"))
    if schema_version is not _UNCHANGED:
        payload["schema_version"] = schema_version
    if engine_version is not None:
        payload["engine_version"] = engine_version
    meta_path.write_text(
        json.dumps(payload, ensure_ascii=False, indent=2), encoding="utf-8"
    )


def _create_real_backup(workspace_id: str) -> str:
    """走真 BackupService 创建一份 manual 备份;返 backup_id。

    避免直接拼接 zip 文件(确保和 BackupService 内部 zip 结构 / backup_meta.json
    schema 完全一致 — 防 RestoreVersionIncompatibleError)。
    """
    ws_service = cmd_module.make_workspace_service()
    _initialize_current_database(ws_service, workspace_id)
    backup_service = BackupService(workspace_service=ws_service, ws_manager=None)
    info = backup_service.create_manual_backup(workspace_id)
    return info.backup_id


def _initialize_current_database(
    workspace_service: WorkspaceService,
    workspace_id: str,
) -> Path:
    db_path = WorkspacePaths(
        workspace_id,
        workspaces_root=workspace_service.workspaces_root,
    ).db_path
    engine = create_engine(f"sqlite:///{db_path}")
    Base.metadata.create_all(engine)
    with Session(engine) as session:
        if session.get(WorkspaceORM, workspace_id) is None:
            now = datetime.now(UTC)
            session.add(
                WorkspaceORM(
                    workspace_id=workspace_id,
                    display_name="CLI workspace",
                    genre="scifi",
                    premise="test",
                    created_at=now,
                    last_active_at=now,
                    engine_version=ENGINE_VERSION,
                    status="active",
                    total_chapters=1,
                    completed_chapters=0,
                    config_overrides={},
                )
            )
            session.commit()
    engine.dispose()
    return db_path


def _prepare_supported_cli_migration_source(
    workspaces_root: Path,
    workspace_id: str,
) -> str:
    previous_schema = "0029_structured_rewrite_constraint_results"
    workspace_service = cmd_module.make_workspace_service()
    db_path = _initialize_current_database(workspace_service, workspace_id)
    connection = sqlite3.connect(db_path)
    try:
        connection.execute("DROP TABLE chapter_operation_leases")
        connection.execute(
            "CREATE TABLE alembic_version "
            "(version_num VARCHAR(32) NOT NULL PRIMARY KEY)"
        )
        connection.execute(
            "INSERT INTO alembic_version(version_num) VALUES (?)",
            (previous_schema,),
        )
        connection.commit()
    finally:
        connection.close()
    _rewrite_meta(
        workspaces_root,
        workspace_id,
        schema_version=previous_schema,
    )
    return previous_schema


# ---------------------------------------------------------------------------
# 1. happy path(4 子命令)
# ---------------------------------------------------------------------------


class TestVersionStatusHappy:
    def test_version_status_fresh_workspace_human_readable(
        self,
        capsys: pytest.CaptureFixture,
    ) -> None:
        """新创建工作区 schema_version=当前 head + engine=ENGINE_VERSION,
        migration_required=False。"""
        _create_workspace("ws-v1")
        rc = run_cli(["workspace", "version-status", "ws-v1"])
        assert rc == 0
        out = capsys.readouterr().out
        assert "workspace_id          : ws-v1" in out
        assert f"current_engine        : {ENGINE_VERSION}" in out
        assert f"workspace_engine      : {ENGINE_VERSION}" in out
        assert "engine_version_match  : True" in out
        assert f"workspace_schema      : {get_current_schema_version()}" in out
        assert "schema_version_match  : True" in out
        assert "migration_required    : False" in out

    def test_version_status_json_format(
        self,
        capsys: pytest.CaptureFixture,
    ) -> None:
        _create_workspace("ws-v-json")
        rc = run_cli(["workspace", "version-status", "ws-v-json", "--json"])
        assert rc == 0
        payload = _extract_json_line(capsys.readouterr().out)
        assert payload["workspace_id"] == "ws-v-json"
        assert payload["engine_version_match"] is True
        assert payload["schema_version_match"] is True
        assert payload["migration_required"] is False
        assert payload["current_schema"] == get_current_schema_version()


class TestBackupsHappy:
    def test_backups_empty_workspace_returns_friendly_message(
        self,
        capsys: pytest.CaptureFixture,
    ) -> None:
        _create_workspace("ws-empty-bk")
        rc = run_cli(["workspace", "backups", "ws-empty-bk"])
        assert rc == 0
        out = capsys.readouterr().out
        assert "workspace_id  : ws-empty-bk" in out
        assert "backup_count  : 0" in out
        assert "(workspace 当前无任何备份)" in out

    def test_backups_after_manual_backup_lists_one_entry_with_json(
        self,
        capsys: pytest.CaptureFixture,
    ) -> None:
        _create_workspace("ws-bk")
        backup_id = _create_real_backup("ws-bk")

        rc = run_cli(["workspace", "backups", "ws-bk", "--json"])
        assert rc == 0
        payload = _extract_json_line(capsys.readouterr().out)
        assert payload["workspace_id"] == "ws-bk"
        assert len(payload["backups"]) == 1
        item = payload["backups"][0]
        assert item["backup_id"] == backup_id
        assert item["backup_type"] == "manual"
        assert item["schema_version"] == get_current_schema_version()
        assert item["engine_version"] == ENGINE_VERSION
        assert item["size_bytes"] > 0


class TestMigrateHappy:
    def test_migrate_already_up_to_date_is_rejected_without_backup(
        self,
        capsys: pytest.CaptureFixture,
        workspaces_root: Path,
    ) -> None:
        """current 工作区不重复备份/DDL，避免无意义维护写入。"""
        _create_workspace("ws-m")
        rc = run_cli(["workspace", "migrate", "ws-m", "--confirm", "ws-m"])
        assert rc == 1
        captured = capsys.readouterr()
        assert "已经是当前版本" in captured.err
        backups_dir = workspaces_root / "ws-m" / "backups"
        assert not list(backups_dir.glob("manual_*.zip"))

    def test_migrate_rejects_empty_null_schema_workspace_without_writing(
        self,
        capsys: pytest.CaptureFixture,
        workspaces_root: Path,
    ) -> None:
        """空 SQLite 不是可验真的旧产品库，CLI 必须在 DDL 前拒绝。"""
        _create_workspace("ws-old")
        _rewrite_meta(workspaces_root, "ws-old", schema_version=None)

        # 跑前 version-status 确认 migration_required=true
        rc = run_cli(["workspace", "version-status", "ws-old", "--json"])
        assert rc == 0
        pre_status = _extract_json_line(capsys.readouterr().out)
        assert pre_status["workspace_schema"] is None
        assert pre_status["migration_required"] is True

        rc = run_cli(
            ["workspace", "migrate", "ws-old", "--confirm", "ws-old", "--json"]
        )
        assert rc == 1
        assert "数据库为空" in capsys.readouterr().err
        assert cmd_module.make_workspace_service().info("ws-old").schema_version is None


class TestRestoreHappy:
    def test_restore_with_backup_id_overwrites_and_creates_pre_restore(
        self,
        capsys: pytest.CaptureFixture,
        workspaces_root: Path,
    ) -> None:
        _create_workspace("ws-r")
        # 写入用户数据到 BackupService 会打包的 data/outline/ 子目录
        outline_dir = workspaces_root / "ws-r" / "data" / "outline"
        outline_dir.mkdir(parents=True, exist_ok=True)
        outline_file = outline_dir / "synopsis.json"
        outline_file.write_text('{"v": "v1 outline"}', encoding="utf-8")

        backup_id = _create_real_backup("ws-r")
        # 改动用户数据 → restore 应拉回 v1
        outline_file.write_text('{"v": "v2 dirty"}', encoding="utf-8")

        rc = run_cli(
            [
                "workspace",
                "restore",
                "ws-r",
                "--backup-id",
                backup_id,
                "--confirm",
                "ws-r",
            ]
        )
        assert rc == 0
        out = capsys.readouterr().out
        assert "workspace_id          : ws-r" in out
        assert f"backup_id             : {backup_id}" in out
        assert "pre_restore_backup_id : pre_restore_" in out
        # 用户文件被还原为 v1
        assert outline_file.read_text(encoding="utf-8") == '{"v": "v1 outline"}'
        # pre_restore 备份落盘(BackupService.list 不返,但目录里在)
        backups_dir = workspaces_root / "ws-r" / "backups"
        pre_restore_files = list(backups_dir.glob("pre_restore_*.zip"))
        assert len(pre_restore_files) == 1

    def test_restore_with_latest_flag_picks_backup_from_list(
        self,
        capsys: pytest.CaptureFixture,
    ) -> None:
        """``--latest`` 等价"从 list_backups() 取第一项"(已按 created_at
        倒序排)。本测试创建 1 份 manual 备份后用 ``--latest`` 调通,验证
        映射逻辑而非"多份对比"(秒级时间戳同名风险见 BackupService 设计)。
        """
        _create_workspace("ws-latest")
        backup_id = _create_real_backup("ws-latest")

        rc = run_cli(
            [
                "workspace",
                "restore",
                "ws-latest",
                "--latest",
                "--confirm",
                "ws-latest",
                "--json",
            ]
        )
        assert rc == 0
        payload = _extract_json_line(capsys.readouterr().out)
        assert payload["backup_id"] == backup_id
        assert payload["workspace_id"] == "ws-latest"


# ---------------------------------------------------------------------------
# 2. error path(状态机 / 不存在 / 参数校验)
# ---------------------------------------------------------------------------


class TestWorkspaceHighLevelError:
    def test_version_status_workspace_not_found_business_error(
        self,
        capsys: pytest.CaptureFixture,
    ) -> None:
        rc = run_cli(["workspace", "version-status", "ws-ghost"])
        assert rc == 1
        err = capsys.readouterr().err
        assert "错误" in err

    def test_migrate_confirm_mismatch_refuses_without_calling_service(
        self,
        capsys: pytest.CaptureFixture,
    ) -> None:
        _create_workspace("ws-mc")
        rc = run_cli(["workspace", "migrate", "ws-mc", "--confirm", "wrong-id"])
        assert rc == 1
        err = capsys.readouterr().err
        assert "--confirm" in err

    def test_restore_confirm_mismatch_refuses(
        self,
        capsys: pytest.CaptureFixture,
    ) -> None:
        _create_workspace("ws-rc")
        backup_id = _create_real_backup("ws-rc")
        rc = run_cli(
            [
                "workspace",
                "restore",
                "ws-rc",
                "--backup-id",
                backup_id,
                "--confirm",
                "wrong-id",
            ]
        )
        assert rc == 1
        err = capsys.readouterr().err
        assert "--confirm" in err

    def test_restore_backup_id_not_found(
        self,
        capsys: pytest.CaptureFixture,
    ) -> None:
        _create_workspace("ws-rnf")
        rc = run_cli(
            [
                "workspace",
                "restore",
                "ws-rnf",
                "--backup-id",
                "manual_99991231T999999Z",
                "--confirm",
                "ws-rnf",
            ]
        )
        assert rc == 1
        err = capsys.readouterr().err
        assert "错误" in err
        assert "manual_99991231T999999Z" in err

    def test_restore_latest_with_no_backups_returns_business_error(
        self,
        capsys: pytest.CaptureFixture,
    ) -> None:
        _create_workspace("ws-rnb")
        rc = run_cli(
            [
                "workspace",
                "restore",
                "ws-rnb",
                "--latest",
                "--confirm",
                "ws-rnb",
            ]
        )
        assert rc == 1
        err = capsys.readouterr().err
        assert "无任何可全量恢复的手动备份" in err

    def test_restore_mutex_no_arg_argparse_error(self) -> None:
        """argparse 互斥组 required=True:不传 --backup-id 也不传 --latest →
        SystemExit(2)。"""
        with pytest.raises(SystemExit) as exc_info:
            run_cli(
                [
                    "workspace",
                    "restore",
                    "ws-x",
                    "--confirm",
                    "ws-x",
                ]
            )
        assert exc_info.value.code == 2

    def test_restore_missing_confirm_argparse_error(self) -> None:
        """argparse required=True 校验缺失 --confirm → SystemExit(2)。"""
        with pytest.raises(SystemExit) as exc_info:
            run_cli(
                [
                    "workspace",
                    "restore",
                    "ws-x",
                    "--backup-id",
                    "manual_x",
                ]
            )
        assert exc_info.value.code == 2

    def test_migrate_missing_confirm_argparse_error(self) -> None:
        with pytest.raises(SystemExit) as exc_info:
            run_cli(["workspace", "migrate", "ws-x"])
        assert exc_info.value.code == 2


# ---------------------------------------------------------------------------
# 3. gate(version-status 三档 + 互斥)
# ---------------------------------------------------------------------------


class TestVersionStatusGate:
    def test_version_status_old_schema_returns_migration_required(
        self,
        workspaces_root: Path,
        capsys: pytest.CaptureFixture,
    ) -> None:
        _create_workspace("ws-old-schema")
        _rewrite_meta(
            workspaces_root,
            "ws-old-schema",
            schema_version="0001_initial",
        )
        rc = run_cli(["workspace", "version-status", "ws-old-schema", "--json"])
        assert rc == 0
        payload = _extract_json_line(capsys.readouterr().out)
        assert payload["workspace_schema"] == "0001_initial"
        assert payload["schema_version_match"] is False
        assert payload["migration_required"] is True

    def test_version_status_null_schema_old_workspace(
        self,
        workspaces_root: Path,
        capsys: pytest.CaptureFixture,
    ) -> None:
        """schema_version=null (P6-B-08 前) → migration_required=true。"""
        _create_workspace("ws-null-schema")
        _rewrite_meta(workspaces_root, "ws-null-schema", schema_version=None)
        rc = run_cli(["workspace", "version-status", "ws-null-schema", "--json"])
        assert rc == 0
        payload = _extract_json_line(capsys.readouterr().out)
        assert payload["workspace_schema"] is None
        assert payload["schema_version_match"] is False
        assert payload["migration_required"] is True


# ---------------------------------------------------------------------------
# 4. session lifecycle(连续命令往返)
# ---------------------------------------------------------------------------


class TestWorkspaceHighLevelSessionLifecycle:
    def test_migrate_then_version_status_then_backups_consistency(
        self,
        workspaces_root: Path,
        capsys: pytest.CaptureFixture,
    ) -> None:
        """模拟"老工作区 → migrate → 验证 → 列表"完整往返。

        ① migrate 落 pre-migration manual backup → ② version-status 显
        migration_required=false → ③ backups 列出至少 1 份 manual。验证
        每命令独立工厂调用 + 进程退出前 commit + 物理 meta.json 持久化生效。
        """
        _create_workspace("ws-life")
        _prepare_supported_cli_migration_source(workspaces_root, "ws-life")

        # 1. migrate
        rc = run_cli(["workspace", "migrate", "ws-life", "--confirm", "ws-life"])
        assert rc == 0
        capsys.readouterr()  # 清掉 stdout

        # 2. version-status 应已就绪
        rc = run_cli(["workspace", "version-status", "ws-life", "--json"])
        assert rc == 0
        status = _extract_json_line(capsys.readouterr().out)
        assert status["migration_required"] is False

        # 3. backups 列表应有至少 1 份 manual(pre-migration backup)
        rc = run_cli(["workspace", "backups", "ws-life", "--json"])
        assert rc == 0
        listing = _extract_json_line(capsys.readouterr().out)
        manual_entries = [b for b in listing["backups"] if b["backup_type"] == "manual"]
        assert len(manual_entries) >= 1


# ---------------------------------------------------------------------------
# P7-B-07:destroy 子命令(三方全删 + strict confirm)
# ---------------------------------------------------------------------------


class TestDestroyHappy:
    def test_destroy_human_readable(
        self,
        capsys: pytest.CaptureFixture,
        workspaces_root: Path,
    ) -> None:
        _create_workspace("ws-destroy-1")
        assert (workspaces_root / "ws-destroy-1").is_dir()

        rc = run_cli(
            ["workspace", "destroy", "ws-destroy-1", "--confirm", "ws-destroy-1"]
        )
        assert rc == 0
        out = capsys.readouterr().out
        assert "workspace_id  : ws-destroy-1" in out
        assert "deleted_paths : 1 项" in out  # 无 archive,只删 workspace 目录
        assert not (workspaces_root / "ws-destroy-1").is_dir()

    def test_destroy_json_format_with_archive_two_paths(
        self,
        capsys: pytest.CaptureFixture,
        workspaces_root: Path,
        archives_root: Path,
    ) -> None:
        """workspace + 顶级 archive 双在场 → deleted_paths 2 项。"""
        _create_workspace("ws-destroy-2")
        # 先 archive 一次产 tar.gz,再重新 create 同 id 让 workspace 目录回归
        ws_service = cmd_module.make_workspace_service()
        ws_service.archive("ws-destroy-2")
        _create_workspace("ws-destroy-2")
        assert (workspaces_root / "ws-destroy-2").is_dir()
        assert (archives_root / "ws-destroy-2.tar.gz").is_file()

        rc = run_cli(
            [
                "workspace",
                "destroy",
                "ws-destroy-2",
                "--confirm",
                "ws-destroy-2",
                "--json",
            ]
        )
        assert rc == 0
        payload = _extract_json_line(capsys.readouterr().out)
        assert payload["workspace_id"] == "ws-destroy-2"
        assert len(payload["deleted_paths"]) == 2
        assert not (workspaces_root / "ws-destroy-2").is_dir()
        assert not (archives_root / "ws-destroy-2.tar.gz").is_file()


class TestDestroyError:
    def test_destroy_confirm_mismatch_returns_business_error(
        self,
        capsys: pytest.CaptureFixture,
        workspaces_root: Path,
    ) -> None:
        _create_workspace("ws-destroy-x")

        rc = run_cli(["workspace", "destroy", "ws-destroy-x", "--confirm", "wrong-id"])
        assert rc == 1  # EXIT_BUSINESS_ERROR
        err = capsys.readouterr().err
        assert "destroy 是不可逆操作" in err
        assert (workspaces_root / "ws-destroy-x").is_dir()  # 文件系统未改动

    def test_destroy_missing_workspace_returns_business_error(
        self,
        capsys: pytest.CaptureFixture,
    ) -> None:
        # 工作区不存在 + confirm 正确 → service 抛 WorkspaceServiceError
        # ("工作区不存在")→ dispatch _BUSINESS_ERRORS 翻 rc=1
        rc = run_cli(["workspace", "destroy", "ghost-ws", "--confirm", "ghost-ws"])
        assert rc == 1
        err = capsys.readouterr().err
        assert "工作区不存在" in err

    def test_destroy_missing_confirm_argparse_exits(
        self,
    ) -> None:
        """--confirm 是 required;不传走 argparse SystemExit(2) 不进 dispatch。"""
        parser = build_parser()
        with pytest.raises(SystemExit) as excinfo:
            parser.parse_args(["workspace", "destroy", "ws-x"])
        assert excinfo.value.code == 2
