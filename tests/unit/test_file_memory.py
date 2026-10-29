"""文件式记忆的单元测试(P1-B-06)。

覆盖验收要点:
  - 两份文件齐全 → 返回包含两段标题 + 内容
  - 只有 world.md → 返回只含世界观段
  - 只有 characters.md → 返回只含人物段
  - 两份都缺 → 返回 ""(不抛错)
  - data_dir 整个不存在 → 返回 ""(不抛错)
  - 文件存在但内容为纯空白 → 视同缺失
  - 内容前后有空白 → strip 后正确拼装
"""

from __future__ import annotations

from pathlib import Path

import pytest

from src.core.memory.file_memory import build_memory_section
from src.core.workspace.workspace_paths import WorkspacePaths


@pytest.fixture
def paths(tmp_path: Path) -> WorkspacePaths:
    """绑定到临时工作区的 WorkspacePaths。"""

    return WorkspacePaths("ws_demo", workspaces_root=tmp_path)


def _seed_data_dir(paths: WorkspacePaths) -> Path:
    """创建 data_dir 并返回其路径。"""

    data_dir = paths.data_dir
    data_dir.mkdir(parents=True, exist_ok=True)
    return data_dir


class TestBothPresent:
    """两份记忆文件齐全时的拼装。"""

    def test_returns_two_sections_in_order(self, paths: WorkspacePaths) -> None:
        data_dir = _seed_data_dir(paths)
        (data_dir / "world.md").write_text(
            "蒸汽朋克伦敦,1899 年。", encoding="utf-8"
        )
        (data_dir / "characters.md").write_text(
            "艾达:发明家,机械义肢工程师。", encoding="utf-8"
        )

        result = build_memory_section(paths)

        # 两段都在,世界观在前
        assert "## 世界观" in result
        assert "## 人物" in result
        assert result.index("## 世界观") < result.index("## 人物")
        assert "蒸汽朋克伦敦" in result
        assert "艾达:发明家" in result

    def test_sections_separated_by_blank_line(self, paths: WorkspacePaths) -> None:
        data_dir = _seed_data_dir(paths)
        (data_dir / "world.md").write_text("W", encoding="utf-8")
        (data_dir / "characters.md").write_text("C", encoding="utf-8")

        result = build_memory_section(paths)

        assert result == "## 世界观\n\nW\n\n## 人物\n\nC"


class TestOneMissing:
    """只有一份记忆文件时,只输出该段,不抛错。"""

    def test_only_world_present(self, paths: WorkspacePaths) -> None:
        data_dir = _seed_data_dir(paths)
        (data_dir / "world.md").write_text("仅世界观", encoding="utf-8")

        result = build_memory_section(paths)

        assert result == "## 世界观\n\n仅世界观"
        assert "## 人物" not in result

    def test_only_characters_present(self, paths: WorkspacePaths) -> None:
        data_dir = _seed_data_dir(paths)
        (data_dir / "characters.md").write_text("仅人物", encoding="utf-8")

        result = build_memory_section(paths)

        assert result == "## 人物\n\n仅人物"
        assert "## 世界观" not in result


class TestBothMissing:
    """两份都缺时返回空字符串而非抛错。"""

    def test_data_dir_exists_but_empty(self, paths: WorkspacePaths) -> None:
        _seed_data_dir(paths)

        assert build_memory_section(paths) == ""

    def test_data_dir_does_not_exist(self, paths: WorkspacePaths) -> None:
        # 故意不创建 data_dir,验证 P1-B-14 之前调用也安全
        assert not paths.data_dir.exists()

        assert build_memory_section(paths) == ""


class TestBlankContent:
    """文件存在但只含空白字符 → 视同缺失。"""

    def test_world_blank_characters_present(self, paths: WorkspacePaths) -> None:
        data_dir = _seed_data_dir(paths)
        (data_dir / "world.md").write_text("   \n\n  \t\n", encoding="utf-8")
        (data_dir / "characters.md").write_text("有内容", encoding="utf-8")

        result = build_memory_section(paths)

        assert result == "## 人物\n\n有内容"
        assert "## 世界观" not in result

    def test_both_blank(self, paths: WorkspacePaths) -> None:
        data_dir = _seed_data_dir(paths)
        (data_dir / "world.md").write_text("\n", encoding="utf-8")
        (data_dir / "characters.md").write_text("   ", encoding="utf-8")

        assert build_memory_section(paths) == ""


class TestStripping:
    """正文前后空白被剥除,但内部换行保留。"""

    def test_leading_trailing_whitespace_stripped(
        self, paths: WorkspacePaths
    ) -> None:
        data_dir = _seed_data_dir(paths)
        (data_dir / "world.md").write_text(
            "\n\n  世界观第一行\n第二行  \n\n", encoding="utf-8"
        )

        result = build_memory_section(paths)

        # 标题后紧跟一个空行,然后是 strip 后的正文,正文内部换行保留
        assert result == "## 世界观\n\n世界观第一行\n第二行"
