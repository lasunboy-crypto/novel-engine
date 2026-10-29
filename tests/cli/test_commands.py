"""CLI 测试(P1-B-21)。

测试组织
========

- ``TestWorkspaceCommands``:9 个工作区子命令完整生命周期 + 各动作边界。
- ``TestChapterCommands``:write / rewrite / revise(mock LLM 客户端);
  ``--issues-file`` 解析的 4 类边界(不存在 / 非 JSON / 非数组 / schema 不符)。
- ``TestActiveContext``:``--workspace`` 覆盖、``_active`` 软链解析、缺失上下文报错。
- ``TestDispatch``:退出码 0 / 1 / 2;argparse 未知子命令的 ``SystemExit(2)``。

mock 策略
=========

- ``commands.make_workspace_service``:monkeypatch 成 ``WorkspaceService(workspaces_root=tmp_path/...)``。
- ``commands.make_chapter_service``:monkeypatch 成带 ``MagicMock(spec=LLMClient)`` 的版本。
- 三个 agent 模板用 fake 内容塞 tmp 路径(与 ``tests/services/test_chapter_service.py`` 同源)。
"""

from __future__ import annotations

import json
import os
from argparse import Namespace
from pathlib import Path
from unittest.mock import MagicMock

import pytest

from src.cli import commands as cmd_module
from src.cli.commands import dispatch
from src.cli.main import build_parser
from src.config import settings
from src.core.agents import _prompt as prompt_module
from src.core.llm.base import LLMClient, LLMResponse, TokenUsage
from src.core.models.chapter_card import ChapterCard, ContinuityContext
from src.core.workspace.workspace_paths import WorkspacePaths
from src.services.chapter_service import ChapterService
from src.services.workspace_service import (
    WorkspaceService,
    WorkspaceServiceError,
)
from tests.revision_contract_helpers import (
    contract_response_sequence,
    single_target_localized_response,
)


# ---------------------------------------------------------------------------
# 工厂 helper
# ---------------------------------------------------------------------------


def _make_continuity() -> ContinuityContext:
    return ContinuityContext(
        prev_chapter_tail="他握紧剑柄,目光不离对面那道身影。",
        unfinished_actions=[],  # P4-B-07:衔接桥默认通过
        time_gap="立即续接",
        scene_continuation="switch",  # P4-B-07:跳过 scene_continuation_location
        pov_change=False,
        pov_from="林衍",
        pov_to="林衍",
    )


def _make_card(chapter_id: str = "ch-0002", **overrides) -> ChapterCard:
    payload: dict = dict(
        chapter_id=chapter_id,
        workspace_id="ws-demo",
        volume_id="v01",
        chapter_number=2,
        title="第二章·破阵",
        tension_level=8,
        # P4-B-06:validator 默认放宽,本文件测 CLI 命令,validator 不参与。
        word_count_min=1,
        word_count_max=10**8,
        plot_node_budget=3,
        must_achieve=[],
        must_unresolved=[],
        forbidden=[],
        pov_character="林衍",
        setting="青崖山顶残阵",
        continuity_context=_make_continuity(),
        next_chapter_seed="苏离消失前留下的玉佩在颤动",
        arc_phase="rising",
        is_arc_finale=False,
        related_plot_threads=["thread-main"],
        related_characters=["林衍"],
        review_threshold=7.5,
        revise_threshold=6.5,
        max_rewrites=3,
        max_revises=2,
    )
    payload.update(overrides)
    return ChapterCard(**payload)


def _writer_response(content: str = "正文") -> LLMResponse:
    return LLMResponse(
        content=content,
        model_used="claude-sonnet-4-6-20250101",
        usage=TokenUsage(input_tokens=10, output_tokens=20),
        finish_reason="end_turn",
    )


def _reviewer_response(score: float = 8.0) -> LLMResponse:
    payload = {
        "score_plot_progression": score,
        "score_tension_match": score,
        "score_prose_quality": score,
        "score_consistency": score,
        "score_hook_management": score,
        "score_voice_consistency": score,
        "hard_constraints_passed": True,
        "failed_constraints": [],
        "issues": [
            {
                "paragraph_anchor": "他握紧剑柄",
                "issue_type": "pacing",
                "description": "节奏过缓",
                "suggestion": "压缩",
            }
        ],
        "suggestions": [],
    }
    return LLMResponse(
        content=json.dumps(payload, ensure_ascii=False),
        model_used="claude-sonnet-4-6-20250101",
        usage=TokenUsage(input_tokens=15, output_tokens=4),
        finish_reason="end_turn",
    )


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
def mock_client() -> MagicMock:
    return MagicMock(spec=LLMClient)


@pytest.fixture(autouse=True)
def _patch_factories(
    monkeypatch: pytest.MonkeyPatch,
    workspaces_root: Path,
    archives_root: Path,
    mock_client: MagicMock,
) -> None:
    """统一替换 ``make_workspace_service`` / ``make_chapter_service`` 工厂。"""

    def _make_ws() -> WorkspaceService:
        return WorkspaceService(
            workspaces_root=workspaces_root,
            archives_root=archives_root,
        )

    def _make_cs(ws_service: WorkspaceService) -> ChapterService:
        return ChapterService(
            workspace_service=ws_service,
            client=mock_client,
            model="claude-sonnet-4-6",
        )

    monkeypatch.setattr(cmd_module, "make_workspace_service", _make_ws)
    monkeypatch.setattr(cmd_module, "make_chapter_service", _make_cs)


@pytest.fixture(autouse=True)
def _patch_prompts(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    """三个 agent 默认从 ``settings.prompts_dir`` 取模板;CLI 测试统一注入 fake。

    与 ``tests/services/test_chapter_service.py`` 的同名 fixture 同源。
    """
    prompts_dir = tmp_path / "prompts_root"
    prompts_dir.mkdir()
    (prompts_dir / "writer.md").write_text("FAKE_WRITER", encoding="utf-8")
    (prompts_dir / "reviewer.md").write_text("FAKE_REVIEWER", encoding="utf-8")
    (prompts_dir / "reviser.md").write_text("FAKE_REVISER", encoding="utf-8")
    monkeypatch.setattr(settings, "prompts_dir", prompts_dir)
    prompt_module.load_prompt_template.cache_clear()
    yield
    prompt_module.load_prompt_template.cache_clear()


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------


def run_cli(args: list[str]) -> int:
    """走完整 ``build_parser → parse_args → dispatch`` 链路,返退出码。

    与 ``main()`` 等价但不调 ``configure_logging``,保持 pytest 输出简洁。
    """
    parser = build_parser()
    parsed = parser.parse_args(args)
    return dispatch(parsed)


def _create_ws_via_cli(workspace_id: str = "ws1", **extra: str) -> int:
    args = [
        "workspace",
        "create",
        workspace_id,
        "--display-name",
        extra.get("display_name", "科幻测试本"),
        "--genre",
        extra.get("genre", "scifi"),
        "--premise",
        extra.get("premise", "人类在月球建殖民地"),
    ]
    return run_cli(args)


def _save_card_via_service(workspace_id: str = "ws-demo") -> None:
    """跨过 CLI 直接落章节卡(P1 没暴露 ``card put`` 命令,P2 加;此处用
    service 直接写,等价于路由层 ``PUT /card``)。"""
    ws = cmd_module.make_workspace_service()
    cs = cmd_module.make_chapter_service(ws)
    cs.save_chapter_card(workspace_id, _make_card())


# ---------------------------------------------------------------------------
# 1. workspace 命令
# ---------------------------------------------------------------------------


class TestWorkspaceCommands:
    """对齐 ``WorkspaceService`` 9 个动作的 CLI 入口。"""

    def test_create_then_info_then_list_active_marker(
        self, workspaces_root: Path, capsys: pytest.CaptureFixture
    ) -> None:
        # create + 自动激活
        rc = _create_ws_via_cli("ws1")
        assert rc == 0
        out = capsys.readouterr().out
        assert "已创建工作区: ws1" in out
        assert (workspaces_root / "_active").is_symlink()

        # info(读 _active)
        rc = run_cli(["workspace", "info"])
        assert rc == 0
        out = capsys.readouterr().out
        assert "workspace_id      : ws1" in out

        # list 标 *
        rc = run_cli(["workspace", "list"])
        assert rc == 0
        out = capsys.readouterr().out
        assert "* ws1" in out

    def test_list_empty(self, capsys: pytest.CaptureFixture) -> None:
        rc = run_cli(["workspace", "list"])
        assert rc == 0
        assert "尚无任何工作区" in capsys.readouterr().out

    def test_switch_changes_active(self, workspaces_root: Path) -> None:
        _create_ws_via_cli("ws1")
        _create_ws_via_cli("ws2", genre="fantasy")
        # ws2 因后 create 而成为 _active
        assert os.readlink(workspaces_root / "_active") == "ws2"
        rc = run_cli(["workspace", "switch", "ws1"])
        assert rc == 0
        assert os.readlink(workspaces_root / "_active") == "ws1"

    def test_archive_moves_workspace_to_archives_dir(
        self, workspaces_root: Path, archives_root: Path
    ) -> None:
        """P1 期 ``workspace archive`` 写 archives/<id>.tar.gz + 删原目录;
        ``workspace restore`` 在 P7-B-04 起切到 BackupService.restore + backup_id
        新语义(对齐 P6-B-07),旧 tar.gz un-archive 模式不再有 CLI 入口
        (``WorkspaceService.restore`` 方法保留供程序化调用)。
        """
        _create_ws_via_cli("ws1")
        rc = run_cli(["workspace", "archive", "ws1"])
        assert rc == 0
        assert (archives_root / "ws1.tar.gz").is_file()
        assert not (workspaces_root / "ws1").is_dir()

    def test_backup_keeps_root(self, workspaces_root: Path) -> None:
        _create_ws_via_cli("ws1")
        rc = run_cli(["workspace", "backup"])  # 默认读 _active
        assert rc == 0
        assert (workspaces_root / "ws1").is_dir()
        backups = list((workspaces_root / "ws1" / "backups").iterdir())
        assert len(backups) == 1
        assert backups[0].name.startswith("ws1-")
        assert backups[0].name.endswith(".tar.gz")

    def test_clean_keeps_user_data(self, workspaces_root: Path) -> None:
        _create_ws_via_cli("ws1")
        # 模拟运行时数据(应被 clean)
        (workspaces_root / "ws1" / "logs" / "x.log").write_text(
            "log", encoding="utf-8"
        )
        # 模拟用户数据(应保留)
        chap_dir = workspaces_root / "ws1" / "data" / "chapters" / "ch-0001"
        chap_dir.mkdir(parents=True)
        (chap_dir / "v1.md").write_text("正文", encoding="utf-8")

        rc = run_cli(["workspace", "clean"])  # 读 _active
        assert rc == 0
        assert not (workspaces_root / "ws1" / "logs" / "x.log").exists()
        assert (chap_dir / "v1.md").is_file()

    def test_purge_requires_confirm_flag(
        self, capsys: pytest.CaptureFixture
    ) -> None:
        _create_ws_via_cli("ws1")
        rc = run_cli(["workspace", "purge"])
        assert rc == 1
        err = capsys.readouterr().err
        assert "--confirm" in err

    def test_purge_with_confirm_removes_root(
        self, workspaces_root: Path
    ) -> None:
        _create_ws_via_cli("ws1")
        rc = run_cli(["workspace", "purge", "--confirm"])
        assert rc == 0
        assert not (workspaces_root / "ws1").is_dir()

    def test_create_no_activate_skips_symlink(
        self, workspaces_root: Path
    ) -> None:
        rc = run_cli(
            [
                "workspace",
                "create",
                "ws1",
                "--display-name",
                "x",
                "--genre",
                "scifi",
                "--premise",
                "y",
                "--no-activate",
            ]
        )
        assert rc == 0
        assert not (workspaces_root / "_active").exists()

    def test_info_unknown_workspace_returns_business_error(
        self, capsys: pytest.CaptureFixture
    ) -> None:
        rc = run_cli(["workspace", "info", "ghost"])
        assert rc == 1
        err = capsys.readouterr().err
        assert "错误" in err

    def test_create_with_total_chapters(
        self, workspaces_root: Path, capsys: pytest.CaptureFixture
    ) -> None:
        rc = run_cli(
            [
                "workspace",
                "create",
                "ws1",
                "--display-name",
                "x",
                "--genre",
                "scifi",
                "--premise",
                "y",
                "--total-chapters",
                "120",
            ]
        )
        assert rc == 0
        out = capsys.readouterr().out
        assert "total_chapters    : 120" in out


# ---------------------------------------------------------------------------
# 2. 章节命令
# ---------------------------------------------------------------------------


class TestChapterCommands:
    @pytest.fixture
    def prepared(self, workspaces_root: Path) -> str:
        _create_ws_via_cli("ws-demo")
        _save_card_via_service("ws-demo")
        return "ws-demo"

    def test_write_chapter_full_pipeline(
        self,
        prepared: str,
        mock_client: MagicMock,
        workspaces_root: Path,
        capsys: pytest.CaptureFixture,
    ) -> None:
        mock_client.complete.side_effect = [
            _writer_response(content="原稿"),
            _reviewer_response(score=8.0),
        ]
        rc = run_cli(["write-chapter", "ch-0002"])
        assert rc == 0
        out = capsys.readouterr().out
        assert "final_state       : approved" in out
        assert "final_version_id  : 1" in out

        final = (
            workspaces_root
            / "ws-demo"
            / "data"
            / "chapters"
            / "ch-0002"
            / "final.md"
        )
        assert final.read_text(encoding="utf-8") == "原稿"

    def test_write_chapter_with_explicit_workspace_flag(
        self,
        prepared: str,
        mock_client: MagicMock,
        capsys: pytest.CaptureFixture,
    ) -> None:
        # 即便 _active 还指向 ws-demo,显式 flag 也能走通
        mock_client.complete.side_effect = [
            _writer_response(content="x"),
            _reviewer_response(score=8.0),
        ]
        rc = run_cli(["write-chapter", "ch-0002", "--workspace", "ws-demo"])
        assert rc == 0

    def test_rewrite_chapter_with_prior_count_escalates(
        self,
        prepared: str,
        mock_client: MagicMock,
        capsys: pytest.CaptureFixture,
    ) -> None:
        # 把 max_rewrites 改成 1 + 传 prior=1 → 第一次 review verdict=rewrite 直接升人工
        ws = cmd_module.make_workspace_service()
        cs = cmd_module.make_chapter_service(ws)
        cs.save_chapter_card(prepared, _make_card(max_rewrites=1))
        mock_client.complete.side_effect = [
            _writer_response(content="重写稿"),
            _reviewer_response(score=5.0),  # rewrite verdict
        ]
        rc = run_cli(
            ["rewrite-chapter", "ch-0002", "--prior-rewrite-count", "1"]
        )
        assert rc == 0
        out = capsys.readouterr().out
        assert "human_intervention" in out
        assert "rewrite_budget_exhausted" in out

    def test_revise_chapter_reads_issues_file_and_writes_new_version(
        self,
        prepared: str,
        mock_client: MagicMock,
        workspaces_root: Path,
        tmp_path: Path,
        capsys: pytest.CaptureFixture,
    ) -> None:
        # 先 write 出 v1
        mock_client.complete.side_effect = [
            _writer_response(content="原稿"),
            _reviewer_response(score=8.0),
        ]
        assert run_cli(["write-chapter", "ch-0002"]) == 0

        # 准备 issues file
        issues_file = tmp_path / "issues.json"
        issues_file.write_text(
            json.dumps(
                [
                    {
                        "paragraph_anchor": "原稿",
                        "issue_type": "pacing",
                        "description": "节奏过缓",
                        "suggestion": "压缩",
                    }
                ]
            ),
            encoding="utf-8",
        )
        # revise:LLM 只调 Reviser 一次
        mock_client.complete.reset_mock()
        mock_client.complete.side_effect = contract_response_sequence(
            lambda messages: single_target_localized_response(
                messages,
                replacement="修订稿",
            )
        )
        capsys.readouterr()  # 清掉之前的 stdout
        rc = run_cli(
            ["revise-chapter", "ch-0002", "--issues-file", str(issues_file)]
        )
        assert rc == 0
        out = capsys.readouterr().out
        assert "version_id        : 2" in out
        assert "creation_type     : revise" in out

        v2 = (
            workspaces_root
            / "ws-demo"
            / "data"
            / "chapters"
            / "ch-0002"
            / "v2.md"
        )
        assert v2.read_text(encoding="utf-8") == "修订稿"
        # final.md 不动(revise 不参与状态机)
        final = (
            workspaces_root
            / "ws-demo"
            / "data"
            / "chapters"
            / "ch-0002"
            / "final.md"
        )
        assert final.read_text(encoding="utf-8") == "原稿"

    def test_revise_chapter_with_source_version_id(
        self,
        prepared: str,
        mock_client: MagicMock,
        tmp_path: Path,
        capsys: pytest.CaptureFixture,
    ) -> None:
        # 先 write 出 v1
        mock_client.complete.side_effect = [
            _writer_response(content="v1"),
            _reviewer_response(score=8.0),
        ]
        run_cli(["write-chapter", "ch-0002"])

        # 准备 issues file
        issues_file = tmp_path / "issues.json"
        issues_file.write_text(
            json.dumps(
                [
                    {
                        "paragraph_anchor": "v1",
                        "issue_type": "pacing",
                        "description": "x",
                        "suggestion": "y",
                    }
                ]
            ),
            encoding="utf-8",
        )
        # revise --source-version-id 1 → 显式指定源(虽然 v1 也是最新,这里
        # 只验证 flag 解析与透传)
        mock_client.complete.reset_mock()
        mock_client.complete.side_effect = contract_response_sequence(
            lambda messages: single_target_localized_response(
                messages,
                replacement="改 v1",
            )
        )
        rc = run_cli(
            [
                "revise-chapter",
                "ch-0002",
                "--issues-file",
                str(issues_file),
                "--source-version-id",
                "1",
            ]
        )
        assert rc == 0

    def test_revise_missing_issues_file(
        self, prepared: str, capsys: pytest.CaptureFixture
    ) -> None:
        rc = run_cli(
            ["revise-chapter", "ch-0002", "--issues-file", "/no/such/file.json"]
        )
        assert rc == 1
        err = capsys.readouterr().err
        assert "不存在" in err

    def test_revise_invalid_json(
        self,
        prepared: str,
        tmp_path: Path,
        capsys: pytest.CaptureFixture,
    ) -> None:
        issues_file = tmp_path / "bad.json"
        issues_file.write_text("{not json", encoding="utf-8")
        rc = run_cli(
            ["revise-chapter", "ch-0002", "--issues-file", str(issues_file)]
        )
        assert rc == 1
        err = capsys.readouterr().err
        assert "JSON" in err

    def test_revise_issues_must_be_list(
        self,
        prepared: str,
        tmp_path: Path,
        capsys: pytest.CaptureFixture,
    ) -> None:
        issues_file = tmp_path / "obj.json"
        issues_file.write_text(
            json.dumps({"not": "list"}), encoding="utf-8"
        )
        rc = run_cli(
            ["revise-chapter", "ch-0002", "--issues-file", str(issues_file)]
        )
        assert rc == 1
        err = capsys.readouterr().err
        assert "数组" in err

    def test_revise_issues_schema_invalid(
        self,
        prepared: str,
        tmp_path: Path,
        capsys: pytest.CaptureFixture,
    ) -> None:
        issues_file = tmp_path / "bad_schema.json"
        # 缺 description / suggestion 必填字段
        issues_file.write_text(
            json.dumps(
                [{"paragraph_anchor": "x", "issue_type": "pacing"}]
            ),
            encoding="utf-8",
        )
        rc = run_cli(
            ["revise-chapter", "ch-0002", "--issues-file", str(issues_file)]
        )
        assert rc == 1
        err = capsys.readouterr().err
        assert "ReviewIssue schema" in err


# ---------------------------------------------------------------------------
# 2.5 工作区资产导入(P2-B-13)
# ---------------------------------------------------------------------------


_IMPORT_CHARACTERS_MD = """\
## 林衍

| 字段 | 值 |
|------|----|
| character_id | lin-yan |
| name | 林衍 |
| archetype | 萌新剑修 |
| appearance | 黑衣 |
| personality | 沉静 |
| background | 寒门 |
| tone | 冷静 |
| sentence_style | 短句 |
| self_address | 我 |
"""

_IMPORT_PLOT_THREADS_MD = """\
## 玉佩之约

| 字段 | 值 |
|------|----|
| thread_id | yu-pei |
| title | 玉佩之约 |
| status | planted |
| importance | 4 |
| planted_chapter | ch-0001 |
| description | 林衍获赠玉佩 |
"""

_IMPORT_TIMELINE_MD = """\
## 林衍上山

| 字段 | 值 |
|------|----|
| entry_id | lin-yan-shang-shan |
| chapter_id | ch-0001 |
| description | 林衍登青崖山 |
| time_label | 建元三年春 |
| time_order | 100 |
"""

_IMPORT_WORLD_MD = """\
## 修真等级

| 字段 | 值 |
|------|----|
| fact_id | xiu-zhen-deng-ji |
| description | 修真分九境;每三境一阶 |
"""


def _mock_embedding_fn_cli(texts: list[str]) -> list[list[float]]:
    return [[0.1, 0.2, 0.3, 0.4] for _ in texts]


@pytest.fixture
def _patch_import_factories(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    """注入 ``make_vector_store`` 替换避开真模型加载;``make_db_session`` 默认
    走真路径(``Base.metadata.create_all`` + 工作区 SQLite 文件)。

    Chroma 持久化目录用 ``tmp_path / "cli_chroma_<ws>"``,与默认
    ``WorkspacePaths.chroma_dir`` 不同——避免与其他 CLI 章节命令(若无意
    访问 chroma)冲突。
    """
    from src.core.memory.vector import VectorStore

    def _make_vs(ws_id: str, paths) -> VectorStore:
        return VectorStore(
            ws_id,
            embedding_fn=_mock_embedding_fn_cli,
            persist_dir=tmp_path / f"cli_chroma_{ws_id}",
        )

    monkeypatch.setattr(cmd_module, "make_vector_store", _make_vs)


def _write_import_fixture(md_dir: Path, files: tuple[str, ...] | None = None) -> None:
    md_dir.mkdir(parents=True, exist_ok=True)
    target = files if files is not None else (
        "characters.md",
        "plot_threads.md",
        "timeline.md",
        "world.md",
    )
    payloads = {
        "characters.md": _IMPORT_CHARACTERS_MD,
        "plot_threads.md": _IMPORT_PLOT_THREADS_MD,
        "timeline.md": _IMPORT_TIMELINE_MD,
        "world.md": _IMPORT_WORLD_MD,
    }
    for name in target:
        (md_dir / name).write_text(payloads[name], encoding="utf-8")


class TestImportAssetsCommand:
    def test_full_fixture_imports_all_four_types(
        self,
        _patch_import_factories: None,
        tmp_path: Path,
        capsys: pytest.CaptureFixture,
    ) -> None:
        _create_ws_via_cli("ws-demo")
        md_dir = tmp_path / "assets"
        _write_import_fixture(md_dir)
        capsys.readouterr()  # 清掉 create 输出
        rc = run_cli(["workspace", "import-assets", str(md_dir)])
        assert rc == 0
        out = capsys.readouterr().out
        assert "导入资产完成" in out
        assert "workspace_id      : ws-demo" in out
        assert "character   : 1" in out
        assert "plot_thread : 1" in out
        assert "timeline    : 1" in out
        assert "fact        : 1" in out
        # 4 文件都被处理
        for filename in (
            "characters.md",
            "plot_threads.md",
            "timeline.md",
            "world.md",
        ):
            assert f"- {filename}" in out

    def test_only_characters_file(
        self,
        _patch_import_factories: None,
        tmp_path: Path,
        capsys: pytest.CaptureFixture,
    ) -> None:
        _create_ws_via_cli("ws-demo")
        md_dir = tmp_path / "assets"
        _write_import_fixture(md_dir, files=("characters.md",))
        capsys.readouterr()
        rc = run_cli(["workspace", "import-assets", str(md_dir)])
        assert rc == 0
        out = capsys.readouterr().out
        assert "character   : 1" in out
        assert "plot_thread : 0" in out

    def test_idempotent_second_run_overwrites(
        self,
        _patch_import_factories: None,
        tmp_path: Path,
        capsys: pytest.CaptureFixture,
    ) -> None:
        """同名条目幂等覆盖:第二次跑 SQLite 不增条目。"""
        _create_ws_via_cli("ws-demo")
        md_dir = tmp_path / "assets"
        _write_import_fixture(md_dir, files=("characters.md",))
        capsys.readouterr()
        rc = run_cli(["workspace", "import-assets", str(md_dir)])
        assert rc == 0

        # 改 characters.md tone,二次导入
        modified = _IMPORT_CHARACTERS_MD.replace(
            "| tone | 冷静 |", "| tone | 凌厉 |"
        )
        (md_dir / "characters.md").write_text(modified, encoding="utf-8")
        capsys.readouterr()
        rc = run_cli(["workspace", "import-assets", str(md_dir)])
        assert rc == 0
        out = capsys.readouterr().out
        assert "character   : 1" in out  # 仍 1 条,update 路径

        # 直接读 SQLite 验证 tone 已更新
        from src.core.memory.store import list_characters_by_workspace

        ws_service = cmd_module.make_workspace_service()
        paths = WorkspacePaths(
            "ws-demo", workspaces_root=ws_service.workspaces_root
        )
        session = cmd_module.make_db_session(paths)
        try:
            chars = list_characters_by_workspace(session, "ws-demo")
            assert len(chars) == 1
            assert chars[0].voice_profile.tone == "凌厉"
        finally:
            session.close()

    def test_md_dir_not_exists_returns_business_error(
        self,
        _patch_import_factories: None,
        tmp_path: Path,
        capsys: pytest.CaptureFixture,
    ) -> None:
        _create_ws_via_cli("ws-demo")
        capsys.readouterr()
        rc = run_cli(
            [
                "workspace",
                "import-assets",
                str(tmp_path / "nope"),
                "--workspace",
                "ws-demo",
            ]
        )
        assert rc == 1
        err = capsys.readouterr().err
        assert "md_dir" in err

    def test_parse_error_returns_business_error(
        self,
        _patch_import_factories: None,
        tmp_path: Path,
        capsys: pytest.CaptureFixture,
    ) -> None:
        _create_ws_via_cli("ws-demo")
        md_dir = tmp_path / "assets"
        md_dir.mkdir()
        # 缺 archetype
        bad = """## 林衍

| 字段 | 值 |
|------|----|
| character_id | lin-yan |
| name | 林衍 |
| appearance | 黑衣 |
| personality | 沉静 |
| background | 寒门 |
| tone | 冷静 |
| sentence_style | 短句 |
| self_address | 我 |
"""
        (md_dir / "characters.md").write_text(bad, encoding="utf-8")
        capsys.readouterr()
        rc = run_cli(["workspace", "import-assets", str(md_dir)])
        assert rc == 1
        err = capsys.readouterr().err
        assert "archetype" in err

    def test_workspace_flag_overrides_active(
        self,
        _patch_import_factories: None,
        tmp_path: Path,
        capsys: pytest.CaptureFixture,
    ) -> None:
        _create_ws_via_cli("ws-demo")
        _create_ws_via_cli("other-ws")  # _active = other-ws
        md_dir = tmp_path / "assets"
        _write_import_fixture(md_dir, files=("characters.md",))
        capsys.readouterr()
        rc = run_cli(
            [
                "workspace",
                "import-assets",
                str(md_dir),
                "--workspace",
                "ws-demo",
            ]
        )
        assert rc == 0
        out = capsys.readouterr().out
        assert "workspace_id      : ws-demo" in out

    def test_no_active_workspace_raises(
        self,
        _patch_import_factories: None,
        tmp_path: Path,
        capsys: pytest.CaptureFixture,
    ) -> None:
        md_dir = tmp_path / "assets"
        _write_import_fixture(md_dir, files=("characters.md",))
        rc = run_cli(["workspace", "import-assets", str(md_dir)])
        assert rc == 1
        err = capsys.readouterr().err
        assert "无活动工作区" in err


# ---------------------------------------------------------------------------
# 3. 工作区上下文(_active 解析 / --workspace 覆盖 / 缺失上下文)
# ---------------------------------------------------------------------------


class TestActiveContext:
    def test_no_active_no_flag_raises(
        self, capsys: pytest.CaptureFixture
    ) -> None:
        # 没创建任何工作区,直接 info 应抛"无活动工作区"
        rc = run_cli(["workspace", "info"])
        assert rc == 1
        err = capsys.readouterr().err
        assert "无活动工作区" in err

    def test_workspace_id_arg_overrides_active(
        self, capsys: pytest.CaptureFixture
    ) -> None:
        _create_ws_via_cli("ws1")
        _create_ws_via_cli("ws2", display_name="另一本")
        # _active=ws2,显式 ws1
        capsys.readouterr()
        rc = run_cli(["workspace", "info", "ws1"])
        assert rc == 0
        out = capsys.readouterr().out
        assert "workspace_id      : ws1" in out
        assert "另一本" not in out

    def test_chapter_workspace_flag_overrides_active(
        self, mock_client: MagicMock
    ) -> None:
        _create_ws_via_cli("ws-demo")
        _save_card_via_service("ws-demo")
        _create_ws_via_cli("other-ws")  # 现在 _active=other-ws
        mock_client.complete.side_effect = [
            _writer_response(content="x"),
            _reviewer_response(score=8.0),
        ]
        # 用 --workspace 显式指 ws-demo,绕过 _active=other-ws
        rc = run_cli(["write-chapter", "ch-0002", "--workspace", "ws-demo"])
        assert rc == 0


# ---------------------------------------------------------------------------
# 4. dispatch 与退出码
# ---------------------------------------------------------------------------


class TestDispatch:
    def test_unknown_subcommand_exits_via_argparse(self) -> None:
        # argparse 自身抛 SystemExit(code=2),不到 dispatch
        parser = build_parser()
        with pytest.raises(SystemExit) as exc_info:
            parser.parse_args(["non-existent-cmd"])
        assert exc_info.value.code == 2

    def test_business_error_returns_exit_code_1(
        self, capsys: pytest.CaptureFixture
    ) -> None:
        def fail(_args: Namespace) -> int:
            raise WorkspaceServiceError("已存在")

        rc = dispatch(Namespace(handler=fail))
        assert rc == 1
        err = capsys.readouterr().err
        assert "错误: 已存在" in err

    def test_unknown_exception_returns_exit_code_2(
        self, capsys: pytest.CaptureFixture
    ) -> None:
        def boom(_args: Namespace) -> int:
            raise RuntimeError("boom")

        rc = dispatch(Namespace(handler=boom))
        assert rc == 2
        err = capsys.readouterr().err
        # traceback 走 stderr
        assert "RuntimeError" in err
        assert "boom" in err

    def test_handler_zero_propagates(self) -> None:
        rc = dispatch(Namespace(handler=lambda _a: 0))
        assert rc == 0
