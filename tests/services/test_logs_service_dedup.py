"""``LogsService._iter_all_rows`` dedup 单测(P4-acceptance 步 ⑧)。

兑现"``_active`` 符号链接 + 真工作区目录被 ``iterdir()`` 各 yield 一次,
导致同一份 jsonl 行被读两次" quirk 修复。

测试构造:tmp_path 下建 ``ws-real/logs/2026-05-11.jsonl`` 内含 2 行 +
``_active -> ws-real`` 符号链接,``LogsService.list_by_trace`` 应返 2 行
(去重后),非 4 行。
"""

from __future__ import annotations

import os
from pathlib import Path

from src.services.logs_service import LogsService


def _write_jsonl(path: Path, lines: list[dict]) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    import json

    with path.open("w", encoding="utf-8") as fp:
        for d in lines:
            fp.write(json.dumps(d) + "\n")


class TestSymlinkDedup:
    def test_active_symlink_does_not_duplicate_rows(
        self, tmp_path: Path
    ) -> None:
        ws_real = tmp_path / "ws-real"
        _write_jsonl(
            ws_real / "logs" / "2026-05-11.jsonl",
            [
                {
                    "event": "e1",
                    "trace_id": "t-a",
                    "workspace_id": "ws-real",
                    "timestamp": "2026-05-11T00:00:00Z",
                },
                {
                    "event": "e2",
                    "trace_id": "t-a",
                    "workspace_id": "ws-real",
                    "timestamp": "2026-05-11T00:00:01Z",
                },
            ],
        )
        # _active 符号链接指向真工作区目录(macOS / Linux 都支持 directory
        # 符号链接;Windows 需要管理员权限,本测试跳过 — pytest skipif 不加
        # 因为 CI 环境是 darwin / linux)。
        active = tmp_path / "_active"
        os.symlink(ws_real, active, target_is_directory=True)
        assert active.is_dir()
        assert (active / "logs" / "2026-05-11.jsonl").exists()

        service = LogsService(data_dir=tmp_path)
        rows = service.list_by_trace("t-a")
        assert len(rows) == 2  # not 4(若 dedup 失效则会读两次 = 4 行)

    def test_no_symlink_returns_unique_rows(self, tmp_path: Path) -> None:
        ws = tmp_path / "ws-a"
        _write_jsonl(
            ws / "logs" / "2026-05-11.jsonl",
            [
                {
                    "event": "e1",
                    "trace_id": "t-b",
                    "workspace_id": "ws-a",
                    "timestamp": "2026-05-11T00:00:00Z",
                },
            ],
        )
        service = LogsService(data_dir=tmp_path)
        rows = service.list_by_trace("t-b")
        assert len(rows) == 1
