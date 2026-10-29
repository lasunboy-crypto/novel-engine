"""``LogsService`` + 模块 helper 单测(P4-B-05)。

覆盖 ``classify_trace_id`` 智能分流 / ``_parse_iso`` 容错 / 损坏 jsonl 行
swallow 不冒泡 / 空目录返空 等边角行为。
"""

from __future__ import annotations

import datetime as _dt
import json
from pathlib import Path

from src.services.logs_service import (
    LogQueryFilters,
    LogsService,
    classify_trace_id,
)


def _seed(directory: Path, lines: list[str]) -> None:
    directory.mkdir(parents=True, exist_ok=True)
    (directory / "2026-05-10.jsonl").write_text("\n".join(lines) + "\n")


def _seed_json(directory: Path, rows: list[dict]) -> None:
    _seed(directory, [json.dumps(r) for r in rows])


class TestClassifyTraceId:
    def test_request_id_prefix(self) -> None:
        assert classify_trace_id("req_abc123") == "request_id"

    def test_trace_id_chapter_format(self) -> None:
        assert classify_trace_id("ch-0001-write0") == "trace_id"

    def test_arbitrary_string_falls_to_trace_id(self) -> None:
        assert classify_trace_id("foo") == "trace_id"


class TestQueryRobustness:
    """``query`` / ``tail`` 在异常输入下的容错。"""

    def test_corrupt_line_swallowed(self, tmp_path: Path) -> None:
        _seed(
            tmp_path / "system_logs",
            [
                json.dumps(
                    {
                        "timestamp": "2026-05-10T10:00:00Z",
                        "level": "INFO",
                        "logger": "x",
                        "event": "good",
                    }
                ),
                "{not-valid-json",
                json.dumps(
                    {
                        "timestamp": "2026-05-10T11:00:00Z",
                        "level": "INFO",
                        "logger": "x",
                        "event": "also-good",
                    }
                ),
            ],
        )
        service = LogsService(data_dir=tmp_path)
        items, total = service.query(LogQueryFilters())
        assert total == 2
        assert {it["event"] for it in items} == {"good", "also-good"}

    def test_missing_directory(self, tmp_path: Path) -> None:
        service = LogsService(data_dir=tmp_path / "absent")
        items, total = service.query(LogQueryFilters())
        assert items == []
        assert total == 0

    def test_since_until_filters_naive_timestamp(self, tmp_path: Path) -> None:
        """timestamp 字段无 timezone 时按 UTC 处理(``_matches`` 兜底)。"""
        _seed_json(
            tmp_path / "system_logs",
            [
                {
                    "timestamp": "2026-05-10T10:00:00",  # naive
                    "level": "INFO",
                    "logger": "x",
                    "event": "naive-ts",
                }
            ],
        )
        service = LogsService(data_dir=tmp_path)
        filters = LogQueryFilters(
            since=_dt.datetime(2026, 5, 10, 9, 0, 0, tzinfo=_dt.timezone.utc),
            until=_dt.datetime(2026, 5, 10, 11, 0, 0, tzinfo=_dt.timezone.utc),
        )
        items, total = service.query(filters)
        assert total == 1
        assert items[0]["event"] == "naive-ts"

    def test_tail_returns_descending(self, tmp_path: Path) -> None:
        _seed_json(
            tmp_path / "system_logs",
            [
                {
                    "timestamp": f"2026-05-10T10:0{i}:00Z",
                    "level": "INFO",
                    "logger": "x",
                    "event": f"e{i}",
                }
                for i in range(5)
            ],
        )
        service = LogsService(data_dir=tmp_path)
        rows = service.tail(n=3)
        events = [r["event"] for r in rows]
        assert events == ["e4", "e3", "e2"]

    def test_list_by_trace_filters_correctly(self, tmp_path: Path) -> None:
        _seed_json(
            tmp_path / "system_logs",
            [
                {
                    "timestamp": "2026-05-10T10:00:00Z",
                    "level": "INFO",
                    "logger": "x",
                    "event": "a",
                    "trace_id": "T1",
                },
                {
                    "timestamp": "2026-05-10T10:00:01Z",
                    "level": "INFO",
                    "logger": "x",
                    "event": "b",
                    "trace_id": "T2",
                },
                {
                    "timestamp": "2026-05-10T10:00:02Z",
                    "level": "INFO",
                    "logger": "x",
                    "event": "c",
                    "trace_id": "T1",
                },
            ],
        )
        service = LogsService(data_dir=tmp_path)
        rows = service.list_by_trace("T1")
        events = [r["event"] for r in rows]
        assert events == ["a", "c"]

    def test_workspace_dir_routed_when_workspace_id_filter_set(
        self, tmp_path: Path
    ) -> None:
        """``workspace_id`` 过滤时只读对应工作区目录,不遍历无关目录。"""
        _seed_json(
            tmp_path / "ws-A" / "logs",
            [
                {
                    "timestamp": "2026-05-10T10:00:00Z",
                    "level": "INFO",
                    "logger": "x",
                    "event": "from-A",
                    "workspace_id": "ws-A",
                }
            ],
        )
        _seed_json(
            tmp_path / "ws-B" / "logs",
            [
                {
                    "timestamp": "2026-05-10T10:00:01Z",
                    "level": "INFO",
                    "logger": "x",
                    "event": "from-B",
                    "workspace_id": "ws-B",
                }
            ],
        )
        service = LogsService(data_dir=tmp_path)
        items, total = service.query(LogQueryFilters(workspace_id="ws-A"))
        assert total == 1
        assert items[0]["event"] == "from-A"
