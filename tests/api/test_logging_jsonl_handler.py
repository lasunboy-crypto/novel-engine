"""``WorkspaceAwareJSONLHandler`` 单测(P4-acceptance)。

覆盖 emit 时按 ``workspace_id`` 字段分流到工作区目录 / 系统目录的核心
契约;``data_dir=None`` / 非 JSON 行 / I/O 失败三档兜底。
"""

from __future__ import annotations

import json
import logging
from pathlib import Path

from src.api.logging import WorkspaceAwareJSONLHandler


def _make_record(message: str) -> logging.LogRecord:
    return logging.LogRecord(
        name="test",
        level=logging.INFO,
        pathname=__file__,
        lineno=1,
        msg=message,
        args=(),
        exc_info=None,
    )


class TestEmit:
    def test_workspace_id_field_routes_to_workspace_dir(
        self, tmp_path: Path
    ) -> None:
        handler = WorkspaceAwareJSONLHandler(data_dir=tmp_path)
        msg = json.dumps(
            {"event": "test", "workspace_id": "ws-a", "level": "info"}
        )
        handler.emit(_make_record(msg))

        ws_logs = tmp_path / "ws-a" / "logs"
        assert ws_logs.exists()
        files = list(ws_logs.glob("*.jsonl"))
        assert len(files) == 1
        line = files[0].read_text(encoding="utf-8").strip()
        assert json.loads(line)["workspace_id"] == "ws-a"

    def test_no_workspace_id_routes_to_system_dir(self, tmp_path: Path) -> None:
        handler = WorkspaceAwareJSONLHandler(data_dir=tmp_path)
        msg = json.dumps({"event": "api_request", "level": "info"})
        handler.emit(_make_record(msg))

        sys_logs = tmp_path / "system_logs"
        assert sys_logs.exists()
        files = list(sys_logs.glob("*.jsonl"))
        assert len(files) == 1

    def test_non_json_line_routes_to_system_dir(self, tmp_path: Path) -> None:
        handler = WorkspaceAwareJSONLHandler(data_dir=tmp_path)
        handler.emit(_make_record("plain console message"))

        sys_logs = tmp_path / "system_logs"
        files = list(sys_logs.glob("*.jsonl"))
        assert len(files) == 1
        assert "plain console message" in files[0].read_text(encoding="utf-8")

    def test_data_dir_none_is_noop(self) -> None:
        handler = WorkspaceAwareJSONLHandler(data_dir=None)
        # 不抛错,不写任何文件;静默 return。
        handler.emit(
            _make_record(json.dumps({"event": "x", "workspace_id": "ws"}))
        )

    def test_empty_workspace_id_routes_to_system_dir(
        self, tmp_path: Path
    ) -> None:
        """``workspace_id`` 为空字符串视作无效,落系统目录。"""
        handler = WorkspaceAwareJSONLHandler(data_dir=tmp_path)
        msg = json.dumps({"event": "edge", "workspace_id": ""})
        handler.emit(_make_record(msg))

        assert not (tmp_path / "" / "logs").exists()
        assert (tmp_path / "system_logs").exists()
