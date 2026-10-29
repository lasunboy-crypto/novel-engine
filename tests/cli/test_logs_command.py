"""``novel logs`` CLI 子命令测试(P4-B-05)。

覆盖 OBSERVABILITY §2.6 字面 CLI 子命令 6 个 flag 的输出契约:

- ``--chapter`` / ``--trace`` / ``--request`` / ``--event`` / ``--level`` /
  ``--workspace`` 多字段过滤
- ``--since`` / ``--until`` ISO 8601 时间范围
- ``--tail [N]`` 取最近 N 条(默认 50)
- ``--limit N`` 非 ``--tail`` 模式输出条数

测试基础设施:`monkeypatch.setattr(settings, "data_dir", tmp_path)` 让
``cmd_logs`` 默认从 ``tmp_path`` 读 jsonl;``capsys`` 捕获 stdout 验证输出。
"""

from __future__ import annotations

import json
from pathlib import Path

import pytest

from src.cli import commands as cmd_module
from src.cli.main import build_parser
from src.config import settings


# ---- helpers --------------------------------------------------------------


def run_cli(args: list[str]) -> int:
    parser = build_parser()
    parsed = parser.parse_args(args)
    return cmd_module.dispatch(parsed)


def _seed_jsonl(directory: Path, rows: list[dict]) -> None:
    directory.mkdir(parents=True, exist_ok=True)
    path = directory / "2026-05-10.jsonl"
    with path.open("w", encoding="utf-8") as fh:
        for r in rows:
            fh.write(json.dumps(r) + "\n")


@pytest.fixture
def seeded_logs(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> Path:
    """种子两份 jsonl + 把 settings.data_dir 指向 tmp_path。"""
    sys_rows = [
        {
            "timestamp": "2026-05-10T10:00:00.000Z",
            "level": "INFO",
            "logger": "src.api.middleware",
            "event": "api_request",
            "request_id": "req_abcdef012345",
            "method": "GET",
            "path": "/api/v1/health",
            "status_code": 200,
        },
        {
            "timestamp": "2026-05-10T11:00:00.000Z",
            "level": "WARNING",
            "logger": "src.api.middleware",
            "event": "api_request",
            "request_id": "req_abcdef012345",
            "status_code": 404,
        },
    ]
    ws_rows = [
        {
            "timestamp": "2026-05-10T10:30:00.000Z",
            "level": "INFO",
            "logger": "src.core.agents.writer",
            "event": "chapter_draft_completed",
            "trace_id": "ch-0001-write0",
            "workspace_id": "ws-cli-demo",
            "chapter_id": "ch-0001",
        },
        {
            "timestamp": "2026-05-10T10:31:00.000Z",
            "level": "INFO",
            "logger": "src.core.agents.reviewer",
            "event": "review_completed",
            "trace_id": "ch-0001-write0",
            "workspace_id": "ws-cli-demo",
            "chapter_id": "ch-0001",
        },
    ]
    _seed_jsonl(tmp_path / "system_logs", sys_rows)
    _seed_jsonl(tmp_path / "ws-cli-demo" / "logs", ws_rows)
    monkeypatch.setattr(settings, "data_dir", tmp_path)
    return tmp_path


# ---- argparse 解析 ---------------------------------------------------------


class TestLogsArgparse:
    """``build_parser`` 暴露的 logs 子命令 + 8 个 flag 契约。"""

    def test_default_args(self) -> None:
        parser = build_parser()
        ns = parser.parse_args(["logs"])
        assert ns.command == "logs"
        for attr in (
            "chapter",
            "trace",
            "request",
            "event",
            "level",
            "workspace",
            "since",
            "until",
            "tail",
        ):
            assert getattr(ns, attr) is None
        assert ns.limit == 50

    def test_tail_const_default_50(self) -> None:
        parser = build_parser()
        ns = parser.parse_args(["logs", "--tail"])
        assert ns.tail == 50

    def test_tail_with_value(self) -> None:
        parser = build_parser()
        ns = parser.parse_args(["logs", "--tail", "10"])
        assert ns.tail == 10

    def test_all_flags_round_trip(self) -> None:
        parser = build_parser()
        ns = parser.parse_args(
            [
                "logs",
                "--chapter",
                "ch-0001",
                "--trace",
                "ch-0001-write0",
                "--request",
                "req_abc",
                "--event",
                "review_completed",
                "--level",
                "WARNING",
                "--workspace",
                "ws-cli-demo",
                "--since",
                "2026-05-10T00:00:00Z",
                "--until",
                "2026-05-10T23:59:59Z",
                "--limit",
                "20",
            ]
        )
        assert ns.chapter == "ch-0001"
        assert ns.trace == "ch-0001-write0"
        assert ns.request == "req_abc"
        assert ns.event == "review_completed"
        assert ns.level == "WARNING"
        assert ns.workspace == "ws-cli-demo"
        assert ns.since == "2026-05-10T00:00:00Z"
        assert ns.until == "2026-05-10T23:59:59Z"
        assert ns.limit == 20


# ---- IO 输出 --------------------------------------------------------------


class TestLogsCommandIO:
    """``cmd_logs`` IO 输出契约(stdout 行格式 + 过滤效果)。"""

    def test_tail_n_returns_last_two_descending(
        self,
        seeded_logs: Path,
        capsys: pytest.CaptureFixture,
    ) -> None:
        rc = run_cli(["logs", "--tail", "2"])
        assert rc == 0
        out = capsys.readouterr().out.strip().splitlines()
        # 4 条总数,tail 2 → 取最近 2 条降序
        assert len(out) == 2
        assert "11:00:00" in out[0]  # 最近(api_request 404)
        assert "10:31:00" in out[1]

    def test_filter_by_event(
        self,
        seeded_logs: Path,
        capsys: pytest.CaptureFixture,
    ) -> None:
        rc = run_cli(["logs", "--event", "review_completed"])
        assert rc == 0
        out = capsys.readouterr().out.strip().splitlines()
        assert len(out) == 1
        assert "review_completed" in out[0]
        assert "ch-0001" in out[0]

    def test_filter_by_workspace(
        self,
        seeded_logs: Path,
        capsys: pytest.CaptureFixture,
    ) -> None:
        rc = run_cli(["logs", "--workspace", "ws-cli-demo"])
        assert rc == 0
        out = capsys.readouterr().out.strip().splitlines()
        assert len(out) == 2
        for line in out:
            assert "workspace_id=ws-cli-demo" in line

    def test_filter_by_request_id(
        self,
        seeded_logs: Path,
        capsys: pytest.CaptureFixture,
    ) -> None:
        rc = run_cli(["logs", "--request", "req_abcdef012345"])
        assert rc == 0
        out = capsys.readouterr().out.strip().splitlines()
        assert len(out) == 2
        for line in out:
            assert "request_id=req_abcdef012345" in line

    def test_filter_by_level_warning_only(
        self,
        seeded_logs: Path,
        capsys: pytest.CaptureFixture,
    ) -> None:
        rc = run_cli(["logs", "--level", "WARNING"])
        assert rc == 0
        out = capsys.readouterr().out.strip().splitlines()
        assert len(out) == 1
        assert "WARNING" in out[0]

    def test_invalid_iso_since_returns_business_error(
        self,
        seeded_logs: Path,
        capsys: pytest.CaptureFixture,
    ) -> None:
        """``--since`` 解析失败 → 翻 ValueError → dispatch 翻 EXIT_BUSINESS_ERROR。"""
        rc = run_cli(["logs", "--since", "not-a-date"])
        assert rc == cmd_module.EXIT_BUSINESS_ERROR
        err = capsys.readouterr().err
        assert "ISO 8601" in err

    def test_no_logs_dir_returns_empty(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch,
        capsys: pytest.CaptureFixture,
    ) -> None:
        empty = tmp_path / "no-logs"
        empty.mkdir()
        monkeypatch.setattr(settings, "data_dir", empty)
        rc = run_cli(["logs", "--tail"])
        assert rc == 0
        assert capsys.readouterr().out == ""
