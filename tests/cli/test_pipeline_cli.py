"""CLI pipeline 5 动作测试(P7-B-02)。

测试覆盖(对齐 plan §"P7 专属:CLI 等价补齐 4 大模块测试约定"4 类):

- **happy path**:5 子命令各 1 测调通 + stdout/JSON 双格式 + exit code 0
- **error path**:无 active / 已有 active / 不存在 workspace / 无效 scope
  → exit code 1 + stderr 有翻译;argparse required 缺失 → SystemExit(2)
- **gate**:scope 4 档(single / range / volume / until)各 1 测覆盖
  + 状态机非法 resume(无 active)/stop(无 active)
- **session lifecycle**:连续命令共享 engine + DB pipeline_runs 行真落库
  (验证 commit 真生效 + 无 lock 残留)

mock 策略
=========

- ``commands.make_workspace_service``:monkeypatch 成 tmp_path WorkspaceService
- ``commands.make_pipeline_service``:monkeypatch 注入带 in-memory engine
  + FakeChapterService + ws_manager=None 的版本(避真 LLM / 避 WS publish)
- 不依赖物理 fixture(全走 tmp_path / WorkspaceService 自动创建工作区目录)
"""

from __future__ import annotations

import json
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
from src.services.pipeline_service import PipelineService
from src.services.workspace_service import WorkspaceService


# ---------------------------------------------------------------------------
# Fakes
# ---------------------------------------------------------------------------


class _FakeChapterService:
    """ChapterService stub:CLI 不调 execute,但 PipelineService 构造需要它。"""

    def write(
        self,
        workspace_id: str,
        chapter_id: str,
        *,
        on_state_transition: Any = None,
        profile_override: str | None = None,
    ) -> Any:  # pragma: no cover — CLI 不调 execute,本方法不会被触发
        return None


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
    (对齐 tests/services/test_pipeline_service.py 同模式;PipelineService
    内部多次开 session,需共享 engine 才能跨 session 读到上一次的 commit)。
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
    """统一替换 CLI 工厂:WorkspaceService 走 tmp_path,PipelineService 走共享
    in-memory engine + FakeChapterService + ws_manager=None。
    """

    def _make_ws() -> WorkspaceService:
        return WorkspaceService(
            workspaces_root=workspaces_root,
            archives_root=archives_root,
        )

    def _make_pipeline(ws_service: WorkspaceService) -> PipelineService:
        return PipelineService(
            chapter_service=_FakeChapterService(),  # type: ignore[arg-type]
            workspace_service=ws_service,
            session_factory=session_factory,
            ws_manager=None,
        )

    monkeypatch.setattr(cmd_module, "make_workspace_service", _make_ws)
    monkeypatch.setattr(cmd_module, "make_pipeline_service", _make_pipeline)


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------


def run_cli(args: list[str]) -> int:
    """走完整 ``build_parser → parse_args → dispatch`` 链路,返退出码。"""
    parser = build_parser()
    parsed = parser.parse_args(args)
    return dispatch(parsed)


def _extract_json_line(stdout: str) -> dict[str, Any]:
    """从 stdout 提取 ``--json`` 输出行。

    structlog 日志走 stdout(对齐 ``src/api/logging.py`` ``logging.basicConfig
    stream=sys.stdout``),与 CLI 的 JSON 输出会混在一起。通过"反向扫最后一行
    以 ``{`` 起始且能被 ``json.loads`` 解析"提取 JSON 行(每个 ``--json``
    命令只 print 1 行 JSON dump,稳定可定位)。
    """
    for line in reversed(stdout.splitlines()):
        line = line.strip()
        if line.startswith("{"):
            return json.loads(line)
    raise AssertionError(f"未在 stdout 找到 JSON 行: {stdout!r}")


def _create_workspace(workspace_id: str = "ws-pipe") -> None:
    """通过 WorkspaceService 创建 meta.json + 工作区目录。"""
    ws_service = cmd_module.make_workspace_service()
    ws_service.create(
        workspace_id,
        display_name="pipeline CLI test",
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
    """种 WorkspaceORM + 多卷 VolumeORM(对齐 test_pipeline_service.py 同 helper)。

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
# 1. happy path(5 子命令各 1 + stdout / --json 双格式)
# ---------------------------------------------------------------------------


class TestPipelineStartHappy:
    def test_start_single_human_readable_output(
        self,
        session_factory: Callable[[str], Session],
        capsys: pytest.CaptureFixture,
    ) -> None:
        _create_workspace("ws-start")
        _seed_workspace_and_volume(session_factory, "ws-start")

        rc = run_cli(
            [
                "pipeline",
                "start",
                "ws-start",
                "--scope",
                "single",
                "--start",
                "ch-0001",
                "--provider-call-cap",
                "96",
            ]
        )
        assert rc == 0
        out = capsys.readouterr().out
        assert "workspace_id          : ws-start" in out
        assert "state                 : running" in out
        assert "scope                 : single" in out
        # chapter_ids 列表 + 当前未跑章节标记 [ ]
        assert "ch-0001" in out
        assert "run_id                : run_" in out  # 16-hex run_id
        assert "provider_call_cap     : 96" in out
        assert "provider_call_count   : 0" in out

    def test_start_range_json_output(
        self,
        session_factory: Callable[[str], Session],
        capsys: pytest.CaptureFixture,
    ) -> None:
        _create_workspace("ws-range")
        _seed_workspace_and_volume(session_factory, "ws-range")

        rc = run_cli(
            [
                "pipeline",
                "start",
                "ws-range",
                "--scope",
                "range",
                "--start",
                "ch-0001",
                "--end",
                "ch-0003",
                "--json",
            ]
        )
        assert rc == 0
        payload = _extract_json_line(capsys.readouterr().out)
        assert payload["workspace_id"] == "ws-range"
        assert payload["state"] == "running"
        assert payload["scope"] == "range"
        assert payload["chapter_ids"] == ["ch-0001", "ch-0002", "ch-0003"]
        assert payload["completed_chapter_ids"] == []
        assert payload["run_id"].startswith("run_")
        assert payload["provider_call_cap"] == 1000
        assert payload["pipeline_attempt_count"] == 0


class TestPipelinePauseResumeStopHappy:
    def test_pause_then_resume_then_stop_full_lifecycle(
        self,
        session_factory: Callable[[str], Session],
        capsys: pytest.CaptureFixture,
    ) -> None:
        _create_workspace("ws-life")
        _seed_workspace_and_volume(session_factory, "ws-life")

        # start
        assert (
            run_cli(
                [
                    "pipeline",
                    "start",
                    "ws-life",
                    "--scope",
                    "single",
                    "--start",
                    "ch-0001",
                ]
            )
            == 0
        )
        capsys.readouterr()  # 清掉 stdout

        # pause
        assert run_cli(["pipeline", "pause", "ws-life"]) == 0
        out = capsys.readouterr().out
        assert "state                 : paused" in out

        # resume
        assert run_cli(["pipeline", "resume", "ws-life"]) == 0
        out = capsys.readouterr().out
        assert "state                 : running" in out

        # stop
        assert run_cli(["pipeline", "stop", "ws-life"]) == 0
        out = capsys.readouterr().out
        assert "state                 : stopped" in out


class TestPipelineStatusHappy:
    def test_status_idle_workspace_returns_idle_placeholder(
        self,
        capsys: pytest.CaptureFixture,
    ) -> None:
        """从未跑过 pipeline 的 workspace → state=idle + 占位 snapshot。"""
        _create_workspace("ws-idle")

        rc = run_cli(["pipeline", "status", "ws-idle"])
        assert rc == 0
        out = capsys.readouterr().out
        assert "state                 : idle" in out
        assert "(无 active run)" in out
        assert "(workspace 当前无 active 或历史 pipeline run)" in out

    def test_status_running_after_start_with_json(
        self,
        session_factory: Callable[[str], Session],
        capsys: pytest.CaptureFixture,
    ) -> None:
        _create_workspace("ws-stat")
        _seed_workspace_and_volume(session_factory, "ws-stat")
        assert (
            run_cli(
                [
                    "pipeline",
                    "start",
                    "ws-stat",
                    "--scope",
                    "single",
                    "--start",
                    "ch-0001",
                ]
            )
            == 0
        )
        capsys.readouterr()

        # status --json
        rc = run_cli(["pipeline", "status", "ws-stat", "--json"])
        assert rc == 0
        out = capsys.readouterr().out
        payload = json.loads(out.strip())
        assert payload["state"] == "running"
        assert payload["scope"] == "single"
        assert payload["chapter_ids"] == ["ch-0001"]


# ---------------------------------------------------------------------------
# 2. error path(无 active / 已有 active / 不存在 ws / 无效 scope)
# ---------------------------------------------------------------------------


class TestPipelineErrors:
    def test_pause_without_active_returns_business_error(
        self,
        capsys: pytest.CaptureFixture,
    ) -> None:
        _create_workspace("ws-empty")
        rc = run_cli(["pipeline", "pause", "ws-empty"])
        assert rc == 1
        err = capsys.readouterr().err
        assert "无 active run" in err
        assert "pause" in err

    def test_resume_without_active_returns_business_error(
        self,
        capsys: pytest.CaptureFixture,
    ) -> None:
        _create_workspace("ws-empty")
        rc = run_cli(["pipeline", "resume", "ws-empty"])
        assert rc == 1
        err = capsys.readouterr().err
        assert "无 active run" in err

    def test_stop_without_active_returns_business_error(
        self,
        capsys: pytest.CaptureFixture,
    ) -> None:
        _create_workspace("ws-empty")
        rc = run_cli(["pipeline", "stop", "ws-empty"])
        assert rc == 1
        err = capsys.readouterr().err
        assert "无 active run" in err

    def test_start_when_already_running_returns_business_error(
        self,
        session_factory: Callable[[str], Session],
        capsys: pytest.CaptureFixture,
    ) -> None:
        """已有 active run 时再 start → PipelineAlreadyRunningError(409 / rc=1)。"""
        _create_workspace("ws-dup")
        _seed_workspace_and_volume(session_factory, "ws-dup")
        assert (
            run_cli(
                [
                    "pipeline",
                    "start",
                    "ws-dup",
                    "--scope",
                    "single",
                    "--start",
                    "ch-0001",
                ]
            )
            == 0
        )
        capsys.readouterr()

        rc = run_cli(
            [
                "pipeline",
                "start",
                "ws-dup",
                "--scope",
                "single",
                "--start",
                "ch-0002",
            ]
        )
        assert rc == 1
        err = capsys.readouterr().err
        assert "已有 active run" in err

    def test_status_workspace_not_found_business_error(
        self,
        capsys: pytest.CaptureFixture,
    ) -> None:
        """workspace 不存在 → WorkspaceService.info 抛 MetaFileError → rc=1。

        注:status 命令本身不调 ws_service.info(直接读 pipeline_runs 表无行
        → idle snapshot)。所以 status 对不存在的 workspace 应返 idle 而非 1。
        本测试改测 ``pipeline start`` 对不存在 workspace 的行为。
        """
        rc = run_cli(
            [
                "pipeline",
                "start",
                "ws-ghost",
                "--scope",
                "single",
                "--start",
                "ch-0001",
            ]
        )
        assert rc == 1
        err = capsys.readouterr().err
        assert "错误" in err

    def test_start_missing_required_scope_argparse_error(self) -> None:
        """argparse ``required=True`` 校验缺失 --scope → SystemExit(2)。"""
        with pytest.raises(SystemExit) as exc_info:
            run_cli(["pipeline", "start", "ws-x", "--start", "ch-0001"])
        assert exc_info.value.code == 2

    def test_start_invalid_scope_choice_argparse_error(self) -> None:
        """argparse ``choices=[...]`` 拒绝非法 scope → SystemExit(2)。"""
        with pytest.raises(SystemExit) as exc_info:
            run_cli(
                [
                    "pipeline",
                    "start",
                    "ws-x",
                    "--scope",
                    "everything",  # 非 4 档之一
                    "--start",
                    "ch-0001",
                ]
            )
        assert exc_info.value.code == 2


# ---------------------------------------------------------------------------
# 3. gate(scope 4 档 + 状态机)
# ---------------------------------------------------------------------------


class TestPipelineScopeGate:
    def test_scope_single_resolves_one_chapter(
        self,
        session_factory: Callable[[str], Session],
        capsys: pytest.CaptureFixture,
    ) -> None:
        _create_workspace("ws-single")
        _seed_workspace_and_volume(session_factory, "ws-single")
        assert (
            run_cli(
                [
                    "pipeline",
                    "start",
                    "ws-single",
                    "--scope",
                    "single",
                    "--start",
                    "ch-0002",
                    "--json",
                ]
            )
            == 0
        )
        payload = _extract_json_line(capsys.readouterr().out)
        assert payload["chapter_ids"] == ["ch-0002"]

    def test_scope_volume_resolves_full_volume(
        self,
        session_factory: Callable[[str], Session],
        capsys: pytest.CaptureFixture,
    ) -> None:
        _create_workspace("ws-vol")
        # 多卷:vol-1 (2 章) + vol-2 (3 章) — 总 5 章,vol-2 是 ch-0003..ch-0005
        _seed_workspace_and_volume(
            session_factory,
            "ws-vol",
            volumes=[("vol-1", 2), ("vol-2", 3)],
        )
        assert (
            run_cli(
                [
                    "pipeline",
                    "start",
                    "ws-vol",
                    "--scope",
                    "volume",
                    "--volume-id",
                    "vol-2",
                    "--json",
                ]
            )
            == 0
        )
        payload = _extract_json_line(capsys.readouterr().out)
        assert payload["chapter_ids"] == ["ch-0003", "ch-0004", "ch-0005"]
        assert payload["volume_id"] == "vol-2"

    def test_scope_until_default_to_volume_end(
        self,
        session_factory: Callable[[str], Session],
        capsys: pytest.CaptureFixture,
    ) -> None:
        """``until`` 不传 ``--end`` → 默认到起点所在卷末(对齐 service 同语义)。"""
        _create_workspace("ws-until")
        _seed_workspace_and_volume(
            session_factory,
            "ws-until",
            volumes=[("vol-1", 2), ("vol-2", 3)],
        )
        assert (
            run_cli(
                [
                    "pipeline",
                    "start",
                    "ws-until",
                    "--scope",
                    "until",
                    "--start",
                    "ch-0003",
                    "--json",
                ]
            )
            == 0
        )
        payload = _extract_json_line(capsys.readouterr().out)
        # ch-0003 起到 vol-2 末(ch-0005)
        assert payload["chapter_ids"] == ["ch-0003", "ch-0004", "ch-0005"]

    def test_scope_range_missing_end_returns_invalid_scope_error(
        self,
        session_factory: Callable[[str], Session],
        capsys: pytest.CaptureFixture,
    ) -> None:
        """``range`` 缺 ``--end`` → service 抛 PipelineInvalidScopeError → rc=1。"""
        _create_workspace("ws-bad")
        _seed_workspace_and_volume(session_factory, "ws-bad")
        rc = run_cli(
            [
                "pipeline",
                "start",
                "ws-bad",
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

    def test_pause_idempotent_when_already_paused(
        self,
        session_factory: Callable[[str], Session],
        capsys: pytest.CaptureFixture,
    ) -> None:
        """已 paused 二次 pause 返当前 snapshot,rc=0(service 幂等)。"""
        _create_workspace("ws-idem")
        _seed_workspace_and_volume(session_factory, "ws-idem")
        run_cli(
            [
                "pipeline",
                "start",
                "ws-idem",
                "--scope",
                "single",
                "--start",
                "ch-0001",
            ]
        )
        run_cli(["pipeline", "pause", "ws-idem"])
        capsys.readouterr()

        rc = run_cli(["pipeline", "pause", "ws-idem"])
        assert rc == 0
        out = capsys.readouterr().out
        assert "state                 : paused" in out


# ---------------------------------------------------------------------------
# 4. session lifecycle(连续命令共享 engine + DB 行真落库 + 无 lock 残留)
# ---------------------------------------------------------------------------


class TestPipelineSessionLifecycle:
    def test_back_to_back_commands_share_engine_no_lock(
        self,
        session_factory: Callable[[str], Session],
        capsys: pytest.CaptureFixture,
    ) -> None:
        """连续 5 个 CLI 命令(start → status → pause → resume → stop),
        共享 engine 但每命令独立 session;无 lock 残留(若 session 未 close,
        第二条命令会 SQLite 'database is locked' 抛错)。
        """
        _create_workspace("ws-rt")
        _seed_workspace_and_volume(session_factory, "ws-rt")

        for args in (
            [
                "pipeline",
                "start",
                "ws-rt",
                "--scope",
                "single",
                "--start",
                "ch-0001",
            ],
            ["pipeline", "status", "ws-rt"],
            ["pipeline", "pause", "ws-rt"],
            ["pipeline", "resume", "ws-rt"],
            ["pipeline", "stop", "ws-rt"],
        ):
            rc = run_cli(args)
            capsys.readouterr()
            assert rc == 0, f"step {args} failed with rc={rc}"

        # 验证最终 state=stopped 行已落 DB(开新 session 读)
        from src.core.db.models import PipelineRunORM

        session = session_factory("ws-rt")
        try:
            rows = (
                session.query(PipelineRunORM)
                .filter(PipelineRunORM.workspace_id == "ws-rt")
                .all()
            )
            assert len(rows) == 1
            assert rows[0].state == "stopped"
            assert rows[0].pause_reason == "user_request"
        finally:
            session.close()

    def test_status_after_stop_shows_last_stopped_run(
        self,
        session_factory: Callable[[str], Session],
        capsys: pytest.CaptureFixture,
    ) -> None:
        """stop 后 status 命令应展示最近的 stopped run(``_load_last_run`` fallback)。"""
        _create_workspace("ws-after")
        _seed_workspace_and_volume(session_factory, "ws-after")
        run_cli(
            [
                "pipeline",
                "start",
                "ws-after",
                "--scope",
                "single",
                "--start",
                "ch-0001",
            ]
        )
        run_cli(["pipeline", "stop", "ws-after"])
        capsys.readouterr()

        rc = run_cli(["pipeline", "status", "ws-after", "--json"])
        assert rc == 0
        payload = _extract_json_line(capsys.readouterr().out)
        assert payload["state"] == "stopped"
        assert payload["pause_reason"] == "user_request"
