"""工作区路径工具与 meta.json 读写的单元测试。

覆盖 P1-B-04 验收要点:
  - WorkspacePaths 各 property 路径正确,且**不创建任何目录**
  - meta.json 写 → 读往返字段一致
  - meta.json 缺失 / JSON 损坏 / schema 不符 → 抛 MetaFileError(带清晰消息)
  - 引擎版本比对三档:compatible / workspace_older / workspace_newer
  - 非法版本字符串抛 ValueError
"""

from __future__ import annotations

from datetime import datetime, timezone
from pathlib import Path

import pytest

from src.core.models.workspace import Workspace
from src.core.workspace.meta import (
    MetaFileError,
    compare_engine_version,
    read_meta,
    write_meta,
)
from src.core.workspace.workspace_paths import WorkspacePaths


def _workspace_payload(**overrides: object) -> Workspace:
    """构造最小合法 Workspace 实例,测试可按需覆盖字段。"""

    payload: dict[str, object] = {
        "workspace_id": "scifi_book_1",
        "display_name": "科幻测试本",
        "genre": "scifi",
        "premise": "在火星上写小说。",
        "created_at": datetime(2026, 1, 1, tzinfo=timezone.utc),
        "last_active_at": datetime(2026, 1, 2, tzinfo=timezone.utc),
        "engine_version": "0.1.0",
        "status": "active",
        "total_chapters": 10,
        "completed_chapters": 0,
    }
    payload.update(overrides)
    return Workspace(**payload)


class TestWorkspacePaths:
    """路径计算正确且不触发 IO。"""

    def test_root_under_workspaces_root(self, tmp_path: Path) -> None:
        wp = WorkspacePaths("scifi_book_1", workspaces_root=tmp_path)
        assert wp.root == (tmp_path / "scifi_book_1").resolve()
        assert wp.workspace_id == "scifi_book_1"

    def test_top_level_paths(self, tmp_path: Path) -> None:
        wp = WorkspacePaths("scifi_book_1", workspaces_root=tmp_path)
        root = (tmp_path / "scifi_book_1").resolve()
        assert wp.meta_json == root / "meta.json"
        assert wp.project_yaml == root / "project.yaml"
        assert wp.prompts_dir == root / "prompts"
        assert wp.archives_dir == root / "archives"
        assert wp.backups_dir == root / "backups"
        assert wp.logs_dir == root / "logs"

    def test_data_subpaths(self, tmp_path: Path) -> None:
        wp = WorkspacePaths("scifi_book_1", workspaces_root=tmp_path)
        data = (tmp_path / "scifi_book_1" / "data").resolve()
        assert wp.data_dir == data
        assert wp.db_path == data / "novel.db"
        assert wp.chroma_dir == data / "chroma"
        assert wp.chapters_dir == data / "chapters"
        assert wp.cards_dir == data / "cards"
        assert wp.outline_dir == data / "outline"
        assert wp.exports_dir == data / "exports"

    def test_chapter_paths(self, tmp_path: Path) -> None:
        wp = WorkspacePaths("scifi_book_1", workspaces_root=tmp_path)
        chapters = (tmp_path / "scifi_book_1" / "data" / "chapters").resolve()
        assert wp.chapter_dir("v1c012") == chapters / "v1c012"
        assert (
            wp.chapter_version_file("v1c012", "v1.md")
            == chapters / "v1c012" / "v1.md"
        )

    def test_does_not_create_directories(self, tmp_path: Path) -> None:
        """实例化与读取所有路径不应创建任何目录。"""
        wp = WorkspacePaths("scifi_book_1", workspaces_root=tmp_path)
        # 触一遍所有 property,确保都不带副作用
        _ = (
            wp.root,
            wp.meta_json,
            wp.data_dir,
            wp.db_path,
            wp.chroma_dir,
            wp.chapters_dir,
            wp.cards_dir,
            wp.outline_dir,
            wp.exports_dir,
            wp.archives_dir,
            wp.backups_dir,
            wp.logs_dir,
            wp.chapter_dir("v1c012"),
        )
        assert not wp.root.exists()
        assert list(tmp_path.iterdir()) == []

    def test_workspaces_root_resolved_at_init(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        """workspaces_root 在 __init__ 内 resolve,后续切换 cwd 不影响路径。"""
        monkeypatch.chdir(tmp_path)
        wp = WorkspacePaths("scifi_book_1", workspaces_root=Path("./local_root"))
        expected = (tmp_path / "local_root" / "scifi_book_1").resolve()
        assert wp.root == expected
        # 切到别处再读路径,仍应是初始化时的绝对路径
        other_dir = tmp_path / "elsewhere"
        other_dir.mkdir()
        monkeypatch.chdir(other_dir)
        assert wp.root == expected


class TestMetaReadWrite:
    """meta.json 读写往返 + 异常翻译。"""

    def test_round_trip(self, tmp_path: Path) -> None:
        wp = WorkspacePaths("scifi_book_1", workspaces_root=tmp_path)
        wp.root.mkdir(parents=True)
        original = _workspace_payload()
        write_meta(wp.meta_json, original)

        loaded = read_meta(wp.meta_json)
        assert loaded == original
        # 写入后不应残留 .tmp
        assert not wp.meta_json.with_suffix(".json.tmp").exists()

    def test_missing_file_raises_meta_file_error(self, tmp_path: Path) -> None:
        with pytest.raises(MetaFileError) as exc_info:
            read_meta(tmp_path / "nope.json")
        assert "meta.json 不存在" in str(exc_info.value)

    def test_invalid_json_raises_meta_file_error(self, tmp_path: Path) -> None:
        path = tmp_path / "meta.json"
        path.write_text("{ not valid json", encoding="utf-8")
        with pytest.raises(MetaFileError) as exc_info:
            read_meta(path)
        assert "不是合法 JSON" in str(exc_info.value)

    def test_schema_mismatch_raises_meta_file_error(self, tmp_path: Path) -> None:
        path = tmp_path / "meta.json"
        path.write_text('{"workspace_id": "x"}', encoding="utf-8")  # 缺大量必填
        with pytest.raises(MetaFileError) as exc_info:
            read_meta(path)
        assert "Workspace schema" in str(exc_info.value)


class TestCompareEngineVersion:
    """版本比对三档 + 非法格式拒绝。"""

    def test_compatible(self) -> None:
        assert (
            compare_engine_version("0.1.0", current_version="0.1.0") == "compatible"
        )

    def test_workspace_older(self) -> None:
        assert (
            compare_engine_version("0.1.0", current_version="0.2.0")
            == "workspace_older"
        )
        assert (
            compare_engine_version("0.1.5", current_version="0.2.0")
            == "workspace_older"
        )
        assert (
            compare_engine_version("0.9.9", current_version="1.0.0")
            == "workspace_older"
        )

    def test_workspace_newer(self) -> None:
        assert (
            compare_engine_version("0.2.0", current_version="0.1.0")
            == "workspace_newer"
        )
        assert (
            compare_engine_version("1.0.0", current_version="0.9.9")
            == "workspace_newer"
        )

    def test_default_uses_engine_version_constant(self) -> None:
        """不传 current_version 时回落到 src.version.ENGINE_VERSION。"""
        from src.version import ENGINE_VERSION

        assert compare_engine_version(ENGINE_VERSION) == "compatible"

    def test_invalid_format_raises(self) -> None:
        with pytest.raises(ValueError, match="X.Y.Z"):
            compare_engine_version("0.1", current_version="0.1.0")
        with pytest.raises(ValueError, match="X.Y.Z"):
            compare_engine_version("0.1.0.0", current_version="0.1.0")
        with pytest.raises(ValueError, match="非负整数"):
            compare_engine_version("0.1.0a1", current_version="0.1.0")
