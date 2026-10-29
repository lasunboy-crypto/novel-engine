"""章节版本管理的单元测试。

覆盖 P1-B-05 验收要点:
  - write_version 在临时工作区写 3 个版本,按版本号读回内容一致
  - 4 种 creation_type(draft / rewrite / revise / manual_edit)各覆盖一条
  - next_version_id 在空目录返回 1,有文件时返回 max + 1
  - next_version_id 在版本断档(v1 + v3 缺 v2)时返回 max + 1 = 4
  - 白盒:强制 next_version_id 返回已存在版本号 → 抛 ChapterVersioningError
  - 发布时发生同版本竞态 → 保留竞争方完整文件并清理临时文件
  - chapter_dir 不存在时 write_version 自动创建
  - read_version_content 文件不存在抛 ChapterVersioningError
  - mark_final 把 v{N}.md 拷贝成 final.md
  - mark_final 在 final.md 已存在时再次标记另一版本 → 直接覆盖,不抛错
  - mark_final 原子替换失败 → 保留上一份 final 并清理临时文件
  - mark_final 源版本不存在 → 抛 ChapterVersioningError
"""

from __future__ import annotations

from pathlib import Path

import pytest

from src.core.chapter_versioning import (
    ChapterVersioning,
    ChapterVersionDurabilityError,
    ChapterVersioningError,
)
from src.core.workspace.workspace_paths import WorkspacePaths


@pytest.fixture
def versioning(tmp_path: Path) -> ChapterVersioning:
    """绑定到临时工作区的 ChapterVersioning 实例。

    v1.0.x wave 6 s2.6a:fixture 预创建 ``paths.root`` 对齐生产语义
    (workspace_service.create 已建好骨架),让 ChapterVersioning.write_version
    新加的"workspace 销毁防御"check 在测试路径下正常 fallthrough。
    """

    paths = WorkspacePaths("ws_demo", workspaces_root=tmp_path)
    paths.root.mkdir(parents=True, exist_ok=True)
    return ChapterVersioning(paths)


class TestNextVersionId:
    """next_version_id 推断规则。"""

    def test_returns_one_when_chapter_dir_missing(
        self, versioning: ChapterVersioning
    ) -> None:
        assert versioning.next_version_id("v1c001") == 1

    def test_returns_one_when_chapter_dir_empty(
        self, versioning: ChapterVersioning, tmp_path: Path
    ) -> None:
        chapter_dir = (
            tmp_path / "ws_demo" / "data" / "chapters" / "v1c001"
        )
        chapter_dir.mkdir(parents=True)
        assert versioning.next_version_id("v1c001") == 1

    def test_returns_max_plus_one_with_consecutive_versions(
        self, versioning: ChapterVersioning
    ) -> None:
        versioning.write_version(
            "v1c001",
            "first",
            creation_type="draft",
            model_used="claude-sonnet-4-6",
        )
        versioning.write_version(
            "v1c001",
            "second",
            creation_type="rewrite",
            model_used="claude-sonnet-4-6",
        )
        assert versioning.next_version_id("v1c001") == 3

    def test_returns_max_plus_one_with_gap(
        self, versioning: ChapterVersioning, tmp_path: Path
    ) -> None:
        """版本断档(有 v1、v3 无 v2)时返回 max + 1 = 4,不回填 v2。"""

        chapter_dir = (
            tmp_path / "ws_demo" / "data" / "chapters" / "v1c001"
        )
        chapter_dir.mkdir(parents=True)
        (chapter_dir / "v1.md").write_text("first", encoding="utf-8")
        (chapter_dir / "v3.md").write_text("third", encoding="utf-8")

        assert versioning.next_version_id("v1c001") == 4

    def test_ignores_non_version_files(
        self, versioning: ChapterVersioning, tmp_path: Path
    ) -> None:
        """final.md / 其它无关文件不应影响版本号推断。"""

        chapter_dir = (
            tmp_path / "ws_demo" / "data" / "chapters" / "v1c001"
        )
        chapter_dir.mkdir(parents=True)
        (chapter_dir / "v1.md").write_text("first", encoding="utf-8")
        (chapter_dir / "final.md").write_text("first", encoding="utf-8")
        (chapter_dir / "notes.txt").write_text("noise", encoding="utf-8")

        assert versioning.next_version_id("v1c001") == 2

    @pytest.mark.parametrize("alias", ["v01.md", "v0.md", "v١.md"])
    def test_rejects_noncanonical_numeric_alias_without_writing(
        self,
        versioning: ChapterVersioning,
        tmp_path: Path,
        alias: str,
    ) -> None:
        paths = WorkspacePaths("ws_demo", workspaces_root=tmp_path)
        chapter_dir = paths.chapter_dir("v1c001")
        chapter_dir.mkdir(parents=True)
        alias_path = chapter_dir / alias
        alias_path.write_text("损坏别名", encoding="utf-8")

        with pytest.raises(ChapterVersioningError, match="非规范版本文件名"):
            versioning.next_version_id("v1c001")
        with pytest.raises(ChapterVersioningError, match="非规范版本文件名"):
            versioning.write_version(
                "v1c001",
                "不得落盘的新正文",
                creation_type="draft",
                model_used="mock",
            )

        assert sorted(path.name for path in chapter_dir.iterdir()) == [alias]


class TestWriteVersion:
    """write_version 落盘 + 返回对象。"""

    def test_writes_three_versions_round_trip(
        self, versioning: ChapterVersioning
    ) -> None:
        bodies = ["第一版正文", "第二版正文(更长一点)", "第三版正文……"]
        for body in bodies:
            versioning.write_version(
                "v1c001",
                body,
                creation_type="draft",
                model_used="claude-sonnet-4-6",
            )

        for idx, body in enumerate(bodies, start=1):
            assert versioning.read_version_content("v1c001", idx) == body

    def test_returns_chapter_version_object(
        self, versioning: ChapterVersioning
    ) -> None:
        version = versioning.write_version(
            "v1c001",
            "草稿正文",
            creation_type="draft",
            model_used="claude-sonnet-4-6",
            notes="首稿",
        )
        assert version.version_id == 1
        assert version.content == "草稿正文"
        assert version.word_count == len("草稿正文")
        assert version.creation_type == "draft"
        assert version.model_used == "claude-sonnet-4-6"
        assert version.notes == "首稿"
        assert version.review_score is None

    def test_creates_chapter_dir_when_missing(
        self, versioning: ChapterVersioning, tmp_path: Path
    ) -> None:
        """章节目录不存在时,write_version 应自动创建,无需上层预创建。"""

        chapter_dir = (
            tmp_path / "ws_demo" / "data" / "chapters" / "v1c001"
        )
        assert not chapter_dir.exists()

        versioning.write_version(
            "v1c001",
            "正文",
            creation_type="draft",
            model_used="claude-sonnet-4-6",
        )

        assert chapter_dir.exists()
        assert (chapter_dir / "v1.md").read_text(encoding="utf-8") == "正文"

    def test_covers_all_creation_types(
        self, versioning: ChapterVersioning
    ) -> None:
        """draft / rewrite / revise / manual_edit 四种 creation_type 各落一版。"""

        types = ("draft", "rewrite", "revise", "manual_edit")
        for idx, creation_type in enumerate(types, start=1):
            version = versioning.write_version(
                "v1c001",
                f"v{idx} 正文",
                creation_type=creation_type,
                model_used="claude-sonnet-4-6",
            )
            assert version.version_id == idx
            assert version.creation_type == creation_type

    def test_write_collision_raises_when_forced(
        self,
        versioning: ChapterVersioning,
        tmp_path: Path,
        monkeypatch: pytest.MonkeyPatch,
    ) -> None:
        """白盒:强制 next_version_id 返回已存在的版本号,验证抛错。"""

        chapter_dir = (
            tmp_path / "ws_demo" / "data" / "chapters" / "v1c001"
        )
        chapter_dir.mkdir(parents=True)
        (chapter_dir / "v1.md").write_text("外部占位", encoding="utf-8")

        monkeypatch.setattr(
            versioning, "next_version_id", lambda chapter_id: 1
        )

        with pytest.raises(ChapterVersioningError) as exc_info:
            versioning.write_version(
                "v1c001",
                "新内容",
                creation_type="draft",
                model_used="claude-sonnet-4-6",
            )
        assert "拒绝覆盖" in str(exc_info.value)

    def test_atomic_publish_race_preserves_competing_version(
        self,
        versioning: ChapterVersioning,
        tmp_path: Path,
        monkeypatch: pytest.MonkeyPatch,
    ) -> None:
        """exists 检查后另一写者抢先发布时，不覆盖对方且不留 temp。"""

        chapter_dir = (
            tmp_path / "ws_demo" / "data" / "chapters" / "v1c001"
        )
        target = chapter_dir / "v1.md"

        def _racing_link(source: Path, destination: Path) -> None:
            assert Path(source).read_text(encoding="utf-8") == "本次待发布正文"
            Path(destination).write_text("竞争方完整正文", encoding="utf-8")
            raise FileExistsError(str(destination))

        monkeypatch.setattr("src.core.chapter_versioning.os.link", _racing_link)

        with pytest.raises(ChapterVersioningError, match="拒绝覆盖"):
            versioning.write_version(
                "v1c001",
                "本次待发布正文",
                creation_type="draft",
                model_used="mock",
            )

        assert target.read_text(encoding="utf-8") == "竞争方完整正文"
        assert list(chapter_dir.glob(".*.tmp")) == []

    def test_directory_fsync_failure_after_publish_keeps_version_and_blocks(
        self,
        versioning: ChapterVersioning,
        tmp_path: Path,
        monkeypatch: pytest.MonkeyPatch,
    ) -> None:
        """link 已成功但 fsync 失败时保留正文并阻止上层继续提交。"""

        def _failed_fsync(_directory: Path) -> None:
            raise OSError("simulated directory fsync failure")

        monkeypatch.setattr(
            ChapterVersioning,
            "_fsync_directory",
            staticmethod(_failed_fsync),
        )

        with pytest.raises(
            ChapterVersionDurabilityError,
            match="目录持久化失败",
        ):
            versioning.write_version(
                "v1c001",
                "已完整发布的第一版",
                creation_type="draft",
                model_used="mock",
            )

        chapter_dir = tmp_path / "ws_demo" / "data" / "chapters" / "v1c001"
        assert (chapter_dir / "v1.md").read_text(encoding="utf-8") == (
            "已完整发布的第一版"
        )
        assert versioning.next_version_id("v1c001") == 2
        assert list(chapter_dir.glob(".*.tmp")) == []


class TestReadVersionContent:
    """read_version_content 异常翻译。"""

    def test_missing_file_raises(
        self, versioning: ChapterVersioning, tmp_path: Path
    ) -> None:
        chapter_dir = (
            tmp_path / "ws_demo" / "data" / "chapters" / "v1c001"
        )
        chapter_dir.mkdir(parents=True)

        with pytest.raises(ChapterVersioningError) as exc_info:
            versioning.read_version_content("v1c001", 99)
        assert "版本文件不存在" in str(exc_info.value)


class TestMarkFinal:
    """mark_final 拷贝行为。"""

    def test_copies_to_final(
        self, versioning: ChapterVersioning, tmp_path: Path
    ) -> None:
        versioning.write_version(
            "v1c001",
            "草稿",
            creation_type="draft",
            model_used="claude-sonnet-4-6",
        )
        versioning.write_version(
            "v1c001",
            "最终通过版",
            creation_type="rewrite",
            model_used="claude-sonnet-4-6",
        )

        final_path = versioning.mark_final("v1c001", 2)
        expected = (
            tmp_path
            / "ws_demo"
            / "data"
            / "chapters"
            / "v1c001"
            / "final.md"
        ).resolve()
        assert final_path == expected
        assert final_path.read_text(encoding="utf-8") == "最终通过版"

    def test_overwrites_existing_final(
        self, versioning: ChapterVersioning
    ) -> None:
        """final.md 已存在时再次标记另一版本 → 直接覆盖,不抛错。"""

        versioning.write_version(
            "v1c001",
            "v1 内容",
            creation_type="draft",
            model_used="claude-sonnet-4-6",
        )
        versioning.write_version(
            "v1c001",
            "v2 内容",
            creation_type="rewrite",
            model_used="claude-sonnet-4-6",
        )

        first_final = versioning.mark_final("v1c001", 1)
        assert first_final.read_text(encoding="utf-8") == "v1 内容"

        # 再次标记 v2 为 final,应直接覆盖
        second_final = versioning.mark_final("v1c001", 2)
        assert second_final == first_final
        assert second_final.read_text(encoding="utf-8") == "v2 内容"

    def test_missing_source_raises(
        self, versioning: ChapterVersioning
    ) -> None:
        with pytest.raises(ChapterVersioningError) as exc_info:
            versioning.mark_final("v1c001", 1)
        assert "源版本文件不存在" in str(exc_info.value)

    def test_replace_failure_preserves_previous_final_and_cleans_temp(
        self,
        versioning: ChapterVersioning,
        tmp_path: Path,
        monkeypatch: pytest.MonkeyPatch,
    ) -> None:
        """replace 未成功时，读者始终只能看到旧 final，不会看到半文件。"""

        versioning.write_version(
            "v1c001",
            "旧最终版",
            creation_type="draft",
            model_used="mock",
        )
        versioning.write_version(
            "v1c001",
            "新最终版",
            creation_type="rewrite",
            model_used="mock",
        )
        final_path = versioning.mark_final("v1c001", 1)

        def _failed_replace(source: Path, destination: Path) -> None:
            assert Path(source).read_bytes() == "新最终版".encode()
            assert Path(destination).read_text(encoding="utf-8") == "旧最终版"
            raise OSError("simulated replace failure")

        monkeypatch.setattr(
            "src.core.chapter_versioning.os.replace", _failed_replace
        )

        with pytest.raises(OSError, match="simulated replace failure"):
            versioning.mark_final("v1c001", 2)

        assert final_path.read_text(encoding="utf-8") == "旧最终版"
        chapter_dir = final_path.parent
        assert list(chapter_dir.glob(".*.tmp")) == []

    def test_directory_fsync_failure_after_replace_keeps_final_and_blocks(
        self,
        versioning: ChapterVersioning,
        monkeypatch: pytest.MonkeyPatch,
    ) -> None:
        """replace 已成功但 fsync 失败时保留新 final，并阻止上层完成。"""

        versioning.write_version(
            "v1c001",
            "新最终版",
            creation_type="draft",
            model_used="mock",
        )

        def _failed_fsync(_directory: Path) -> None:
            raise OSError("simulated directory fsync failure")

        monkeypatch.setattr(
            ChapterVersioning,
            "_fsync_directory",
            staticmethod(_failed_fsync),
        )

        with pytest.raises(
            ChapterVersionDurabilityError,
            match="目录持久化失败",
        ):
            versioning.mark_final("v1c001", 1)

        final_path = versioning._paths.chapter_version_file("v1c001", "final.md")
        assert final_path.read_text(encoding="utf-8") == "新最终版"
        assert list(final_path.parent.glob(".*.tmp")) == []
