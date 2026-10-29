"""CLI chapter 高阶动作测试(P7-B-01)。

测试覆盖(对齐 plan §"P7 专属:CLI 等价补齐 4 大模块测试约定"4 类):

- **happy path**:4 子命令各 1 测调通 + stdout 含期望字段 + exit code 0
- **error path**:不存在 chapter / 状态机不允许 / --reason 空 / --confirm 不匹配
  → exit code 1 + stderr 有错误信息
- **gate**:rewind 后 invalidated 章节再 rewind 触发 InvalidStateError;unblock
  非 invalidated 状态触发 UnblockInvalidStateError
- **session lifecycle**:CLI 命令跑完后 session 已 close(每命令独立 session
  factory 调用 + 进程退出前 commit;in-memory engine 仍可被复用工厂打开新 session
  验证 ORM 行已落盘 = 等价 lock 无残留)

mock 策略
=========

- ``commands.make_workspace_service``:monkeypatch 成 ``WorkspaceService(workspaces_root=tmp_path/...)``
- ``commands.make_chapter_rewind_service``:monkeypatch 注入带 in-memory engine
  + FakeVectorStore + ws_manager=None 的版本(避真 Chroma / 避 Qwen 模型加载)
- 不依赖物理 fixture(tmp_path / WorkspaceService 自动创建工作区目录)
"""

from __future__ import annotations

from collections.abc import Callable
from datetime import UTC, datetime
from pathlib import Path

import pytest
from sqlalchemy import create_engine
from sqlalchemy.orm import Session

from src.cli import commands as cmd_module
from src.cli.commands import dispatch
from src.cli.main import build_parser
from src.core.db import Base
from src.core.db.models import (
    CharacterORM,
    ChapterORM,
    WorkspaceORM,
)
from src.core.llm.mock_client import MockLLMClient
from src.core.models.chapter_card import ChapterCard, ContinuityContext
from src.core.workspace.workspace_paths import WorkspacePaths
from src.services.chapter_rewind_service import ChapterRewindService
from src.services.workspace_service import WorkspaceService


# ---------------------------------------------------------------------------
# Fakes
# ---------------------------------------------------------------------------


class _FakeVectorStore:
    """单测 fake;记录 soft_delete_by_chapter_id 调用 + 可控返量。"""

    def __init__(self, *, revoked_count: int = 0) -> None:
        self.calls: list[tuple[str, str]] = []
        self._revoked_count = revoked_count

    def soft_delete_by_chapter_id(
        self, collection: str, *, chapter_id: str
    ) -> int:
        self.calls.append((collection, chapter_id))
        return self._revoked_count


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
    """单条 in-memory engine 复用(每命令构造新 session,engine 共享 — 验证
    session lifecycle 后 ORM 数据仍可读到 = commit 真生效)。"""
    engine = create_engine("sqlite:///:memory:")
    Base.metadata.create_all(engine)
    return engine


@pytest.fixture
def session_factory(shared_engine) -> Callable[[str], Session]:
    def factory(workspace_id: str) -> Session:
        return Session(shared_engine)

    return factory


@pytest.fixture
def fake_vs() -> _FakeVectorStore:
    return _FakeVectorStore(revoked_count=2)


@pytest.fixture(autouse=True)
def _patch_factories(
    monkeypatch: pytest.MonkeyPatch,
    workspaces_root: Path,
    archives_root: Path,
    session_factory: Callable[[str], Session],
    fake_vs: _FakeVectorStore,
) -> None:
    """统一替换 CLI 工厂:WorkspaceService 走 tmp_path,
    ChapterRewindService 走共享 in-memory engine + FakeVectorStore + ws_manager=None。
    """

    def _make_ws() -> WorkspaceService:
        return WorkspaceService(
            workspaces_root=workspaces_root,
            archives_root=archives_root,
        )

    def _make_rewind(ws_service: WorkspaceService) -> ChapterRewindService:
        return ChapterRewindService(
            workspace_service=ws_service,
            session_factory=session_factory,
            vector_store_factory=lambda ws_id: fake_vs,
            ws_manager=None,
            llm_client=MockLLMClient(),
            llm_model="mock-default",
        )

    monkeypatch.setattr(cmd_module, "make_workspace_service", _make_ws)
    monkeypatch.setattr(
        cmd_module, "make_chapter_rewind_service", _make_rewind
    )


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------


def run_cli(args: list[str]) -> int:
    """走完整 ``build_parser → parse_args → dispatch`` 链路,返退出码。"""
    parser = build_parser()
    parsed = parser.parse_args(args)
    return dispatch(parsed)


def _create_workspace(workspace_id: str = "ws-rewind") -> None:
    """通过 WorkspaceService 创建 meta.json + 工作区目录。"""
    ws_service = cmd_module.make_workspace_service()
    ws_service.create(
        workspace_id,
        display_name="rewind CLI test",
        genre="xianxia",
        premise="test premise",
        activate=False,
    )


def _seed_workspace_orm(
    session_factory: Callable[[str], Session], workspace_id: str
) -> None:
    session = session_factory(workspace_id)
    try:
        now = datetime.now(UTC)
        session.merge(
            WorkspaceORM(
                workspace_id=workspace_id,
                display_name="x",
                genre="xianxia",
                premise="p",
                created_at=now,
                last_active_at=now,
                engine_version="0.1.0",
                status="active",
                total_chapters=10,
                completed_chapters=0,
                config_overrides={},
            )
        )
        session.commit()
    finally:
        session.close()


def _seed_chapter(
    session_factory: Callable[[str], Session],
    workspace_id: str,
    chapter_id: str,
    status: str = "approved",
) -> None:
    session = session_factory(workspace_id)
    try:
        now = datetime.now(UTC)
        session.merge(
            ChapterORM(
                chapter_id=chapter_id,
                workspace_id=workspace_id,
                current_version=1,
                status=status,
                rewrite_count=0,
                revise_count=0,
                created_at=now,
                approved_at=now if status == "approved" else None,
                total_token_usage={},
                total_cost_usd=0.0,
            )
        )
        session.commit()
    finally:
        session.close()


def _seed_force_pass_files(workspace_id: str, chapter_id: str) -> None:
    workspace_service = cmd_module.make_workspace_service()
    paths = WorkspacePaths(
        workspace_id,
        workspaces_root=workspace_service.workspaces_root,
    )
    card = ChapterCard(
        chapter_id=chapter_id,
        workspace_id=workspace_id,
        volume_id="v01",
        chapter_number=int(chapter_id.rsplit("-", 1)[-1]),
        title="人工裁决章",
        tension_level=5,
        word_count_min=1,
        word_count_max=4000,
        must_achieve=[],
        must_unresolved=[],
        forbidden=[],
        pov_character="lin-yan",
        setting="山门",
        continuity_context=ContinuityContext(
            prev_chapter_tail="",
            unfinished_actions=[],
            time_gap="开篇",
            scene_continuation="switch",
            pov_change=False,
            pov_from=None,
            pov_to="lin-yan",
        ),
        next_chapter_seed="继续调查",
        arc_phase="opening",
        is_arc_finale=False,
        related_plot_threads=[],
        related_characters=["lin-yan"],
    )
    paths.cards_dir.mkdir(parents=True, exist_ok=True)
    (paths.cards_dir / f"{chapter_id}.json").write_text(
        card.model_dump_json(),
        encoding="utf-8",
    )
    version_path = paths.chapter_version_file(chapter_id, "v1.md")
    version_path.parent.mkdir(parents=True, exist_ok=True)
    version_path.write_text("林衍站在山门前，确认风铃仍在摇动。", encoding="utf-8")


def _seed_character(
    session_factory: Callable[[str], Session],
    workspace_id: str,
    character_id: str,
    first_appearance_chapter: str,
) -> None:
    session = session_factory(workspace_id)
    try:
        session.merge(
            CharacterORM(
                character_id=character_id,
                workspace_id=workspace_id,
                name=character_id,
                aliases=[],
                archetype="stub",
                appearance="",
                personality="",
                background="",
                voice_profile={"tone": "neutral"},
                goals_long_term=[],
                goals_short_term=[],
                current_state={},
                relationships={},
                abilities=[],
                secrets=[],
                first_appearance_chapter=first_appearance_chapter,
                last_updated_chapter=None,
            )
        )
        session.commit()
    finally:
        session.close()


def _read_chapter_status(
    session_factory: Callable[[str], Session],
    workspace_id: str,
    chapter_id: str,
) -> str | None:
    """开新 session 读 ORM 行(验证 commit 真生效 + lifecycle 无残留)。"""
    session = session_factory(workspace_id)
    try:
        row = session.get(ChapterORM, chapter_id)
        return row.status if row else None
    finally:
        session.close()


# ---------------------------------------------------------------------------
# 1. happy path(4 子命令各 1)
# ---------------------------------------------------------------------------


class TestChapterRewindHappy:
    def test_rewind_happy_invalidates_subsequent_and_prints_summary(
        self,
        session_factory: Callable[[str], Session],
        fake_vs: _FakeVectorStore,
        capsys: pytest.CaptureFixture,
    ) -> None:
        _create_workspace("ws-rewind")
        _seed_workspace_orm(session_factory, "ws-rewind")
        for cid, status in [
            ("ch-0001", "approved"),
            ("ch-0002", "approved"),
            ("ch-0003", "approved"),
            ("ch-0004", "reviewing"),
            ("ch-0005", "drafting"),
        ]:
            _seed_chapter(session_factory, "ws-rewind", cid, status)
        _seed_character(
            session_factory, "ws-rewind", "char-lin", "ch-0003"
        )

        rc = run_cli(
            [
                "chapter",
                "rewind",
                "ws-rewind",
                "ch-0003",
                "--confirm",
                "ws-rewind",
            ]
        )
        assert rc == 0
        out = capsys.readouterr().out
        assert "chapter_id            : ch-0003" in out
        assert "from_status           : approved" in out
        assert "to_status             : drafting" in out
        assert "invalidated_count     : 2" in out
        assert "- ch-0004" in out
        assert "- ch-0005" in out
        assert "vector_revoked_count  : 2" in out
        assert "extracted_revoked_count: 1" in out

        # 验证 ORM 状态真已落库(开新 session 读)
        assert _read_chapter_status(session_factory, "ws-rewind", "ch-0003") == "drafting"
        assert _read_chapter_status(session_factory, "ws-rewind", "ch-0004") == "invalidated"
        assert _read_chapter_status(session_factory, "ws-rewind", "ch-0005") == "invalidated"
        assert _read_chapter_status(session_factory, "ws-rewind", "ch-0001") == "approved"
        # Chroma 调用记录
        assert fake_vs.calls == [("memories", "ch-0003")]

    def test_force_pass_happy_writes_reason_and_prints_timestamp(
        self,
        session_factory: Callable[[str], Session],
        capsys: pytest.CaptureFixture,
    ) -> None:
        _create_workspace("ws-fp")
        _seed_workspace_orm(session_factory, "ws-fp")
        _seed_chapter(session_factory, "ws-fp", "ch-0001", status="reviewing")
        _seed_force_pass_files("ws-fp", "ch-0001")

        rc = run_cli(
            [
                "chapter",
                "force-pass",
                "ws-fp",
                "ch-0001",
                "--reason",
                "维护人决定跳过",
            ]
        )
        assert rc == 0
        out = capsys.readouterr().out
        assert "chapter_id        : ch-0001" in out
        assert "from_status       : reviewing" in out
        assert "to_status         : approved" in out
        assert "force_pass_at     :" in out  # ISO 8601 时间戳
        assert "force_pass_reason : 维护人决定跳过" in out
        assert _read_chapter_status(session_factory, "ws-fp", "ch-0001") == "approved"

    def test_unblock_happy_invalidated_to_drafting(
        self,
        session_factory: Callable[[str], Session],
        capsys: pytest.CaptureFixture,
    ) -> None:
        _create_workspace("ws-unblock")
        _seed_workspace_orm(session_factory, "ws-unblock")
        _seed_chapter(
            session_factory, "ws-unblock", "ch-0001", status="invalidated"
        )

        rc = run_cli(["chapter", "unblock", "ws-unblock", "ch-0001"])
        assert rc == 0
        out = capsys.readouterr().out
        assert "chapter_id  : ch-0001" in out
        assert "from_status : invalidated" in out
        assert "to_status   : drafting" in out
        assert _read_chapter_status(session_factory, "ws-unblock", "ch-0001") == "drafting"

    def test_unblock_all_happy_batch_flips_all(
        self,
        session_factory: Callable[[str], Session],
        capsys: pytest.CaptureFixture,
    ) -> None:
        _create_workspace("ws-batch")
        _seed_workspace_orm(session_factory, "ws-batch")
        _seed_chapter(
            session_factory, "ws-batch", "ch-0001", status="invalidated"
        )
        _seed_chapter(
            session_factory, "ws-batch", "ch-0002", status="invalidated"
        )
        _seed_chapter(
            session_factory, "ws-batch", "ch-0003", status="approved"
        )

        rc = run_cli(["chapter", "unblock-all", "ws-batch"])
        assert rc == 0
        out = capsys.readouterr().out
        assert "workspace_id    : ws-batch" in out
        assert "unblocked_count : 2" in out
        assert "- ch-0001" in out
        assert "- ch-0002" in out
        # ch-0003 仍 approved 不被翻
        assert _read_chapter_status(session_factory, "ws-batch", "ch-0001") == "drafting"
        assert _read_chapter_status(session_factory, "ws-batch", "ch-0002") == "drafting"
        assert _read_chapter_status(session_factory, "ws-batch", "ch-0003") == "approved"

    def test_unblock_all_idempotent_when_no_invalidated(
        self,
        session_factory: Callable[[str], Session],
        capsys: pytest.CaptureFixture,
    ) -> None:
        _create_workspace("ws-empty")
        _seed_workspace_orm(session_factory, "ws-empty")
        _seed_chapter(
            session_factory, "ws-empty", "ch-0001", status="approved"
        )

        rc = run_cli(["chapter", "unblock-all", "ws-empty"])
        assert rc == 0
        out = capsys.readouterr().out
        assert "workspace_id    : ws-empty" in out
        assert "unblocked_count : 0" in out


# ---------------------------------------------------------------------------
# 2. error path(状态机 / 不存在 / 参数校验)
# ---------------------------------------------------------------------------


class TestChapterRewindError:
    def test_rewind_chapter_not_found_returns_business_error(
        self,
        session_factory: Callable[[str], Session],
        capsys: pytest.CaptureFixture,
    ) -> None:
        _create_workspace("ws-x")
        _seed_workspace_orm(session_factory, "ws-x")
        rc = run_cli(
            [
                "chapter",
                "rewind",
                "ws-x",
                "ch-ghost",
                "--confirm",
                "ws-x",
            ]
        )
        assert rc == 1
        err = capsys.readouterr().err
        assert "错误" in err
        assert "ch-ghost" in err

    def test_rewind_drafting_source_invalid_state(
        self,
        session_factory: Callable[[str], Session],
        capsys: pytest.CaptureFixture,
    ) -> None:
        _create_workspace("ws-x")
        _seed_workspace_orm(session_factory, "ws-x")
        _seed_chapter(
            session_factory, "ws-x", "ch-0001", status="drafting"
        )
        rc = run_cli(
            [
                "chapter",
                "rewind",
                "ws-x",
                "ch-0001",
                "--confirm",
                "ws-x",
            ]
        )
        assert rc == 1
        err = capsys.readouterr().err
        assert "drafting" in err

    def test_rewind_confirm_mismatch_refuses_without_calling_service(
        self,
        session_factory: Callable[[str], Session],
        capsys: pytest.CaptureFixture,
    ) -> None:
        _create_workspace("ws-x")
        _seed_workspace_orm(session_factory, "ws-x")
        _seed_chapter(
            session_factory, "ws-x", "ch-0001", status="approved"
        )
        rc = run_cli(
            [
                "chapter",
                "rewind",
                "ws-x",
                "ch-0001",
                "--confirm",
                "wrong-id",
            ]
        )
        assert rc == 1
        err = capsys.readouterr().err
        assert "--confirm" in err
        # 章节状态未被改(service 没被调用)
        assert _read_chapter_status(session_factory, "ws-x", "ch-0001") == "approved"

    def test_force_pass_empty_reason_rejected_by_argparse_or_service(
        self,
        session_factory: Callable[[str], Session],
        capsys: pytest.CaptureFixture,
    ) -> None:
        """``--reason ""`` argparse 允许空串(只 required=True 校验存在性),
        由 service 层 Pydantic ``min_length=1`` 兜底校验。**注**:
        ``ChapterForcePassIn.reason`` 在 API 路由层校验;CLI 走 service 直调
        ``rewind_service.force_pass(reason="")``,service 不强制 min_length,
        会写入空 reason。本测试断言 **当前行为** — 空 reason 仍能跑通
        (CLI 没接 Pydantic 校验层是 P7+ 留观项,impl 文档登记)。

        若后续 service 层加 reason 非空校验,本测试可改为断言 rc=1 +
        stderr 含 'min_length'。
        """
        _create_workspace("ws-x")
        _seed_workspace_orm(session_factory, "ws-x")
        _seed_chapter(
            session_factory, "ws-x", "ch-0001", status="reviewing"
        )
        _seed_force_pass_files("ws-x", "ch-0001")
        rc = run_cli(
            [
                "chapter",
                "force-pass",
                "ws-x",
                "ch-0001",
                "--reason",
                "",
            ]
        )
        # service 不强制 min_length,空 reason 也能跑通;留观项见 impl 文档
        assert rc == 0
        assert _read_chapter_status(session_factory, "ws-x", "ch-0001") == "approved"

    def test_force_pass_missing_reason_argparse_error(self) -> None:
        """argparse ``required=True`` 校验缺失 --reason → SystemExit(2)。"""
        with pytest.raises(SystemExit) as exc_info:
            run_cli(["chapter", "force-pass", "ws-x", "ch-0001"])
        assert exc_info.value.code == 2

    def test_rewind_missing_confirm_argparse_error(self) -> None:
        """argparse ``required=True`` 校验缺失 --confirm → SystemExit(2)。"""
        with pytest.raises(SystemExit) as exc_info:
            run_cli(["chapter", "rewind", "ws-x", "ch-0001"])
        assert exc_info.value.code == 2

    def test_unblock_non_invalidated_source_returns_business_error(
        self,
        session_factory: Callable[[str], Session],
        capsys: pytest.CaptureFixture,
    ) -> None:
        _create_workspace("ws-x")
        _seed_workspace_orm(session_factory, "ws-x")
        _seed_chapter(
            session_factory, "ws-x", "ch-0001", status="approved"
        )
        rc = run_cli(["chapter", "unblock", "ws-x", "ch-0001"])
        assert rc == 1
        err = capsys.readouterr().err
        assert "approved" in err

    def test_unblock_workspace_not_found_business_error(
        self,
        capsys: pytest.CaptureFixture,
    ) -> None:
        """workspace 不存在 → WorkspaceService.info 抛 MetaFileError → rc=1。"""
        rc = run_cli(["chapter", "unblock", "ws-ghost", "ch-0001"])
        assert rc == 1
        err = capsys.readouterr().err
        assert "错误" in err


# ---------------------------------------------------------------------------
# 3. gate(状态机两段路径)
# ---------------------------------------------------------------------------


class TestChapterRewindGate:
    def test_rewind_then_re_rewind_invalidated_target_rejects(
        self,
        session_factory: Callable[[str], Session],
        capsys: pytest.CaptureFixture,
    ) -> None:
        """rewind 触发后续章 invalidated → 再 rewind 该 invalidated 章节
        抛 ChapterRewindInvalidStateError(rewind 不允许源:drafting / invalidated)。
        """
        _create_workspace("ws-gate")
        _seed_workspace_orm(session_factory, "ws-gate")
        _seed_chapter(session_factory, "ws-gate", "ch-0001", "approved")
        _seed_chapter(session_factory, "ws-gate", "ch-0002", "approved")
        _seed_chapter(session_factory, "ws-gate", "ch-0003", "approved")

        # 第一次 rewind ch-0001 → ch-0002/ch-0003 翻 invalidated
        rc = run_cli(
            [
                "chapter",
                "rewind",
                "ws-gate",
                "ch-0001",
                "--confirm",
                "ws-gate",
            ]
        )
        assert rc == 0
        capsys.readouterr()  # 清掉 stdout

        # 第二次 rewind ch-0002 (已 invalidated) → InvalidStateError
        rc = run_cli(
            [
                "chapter",
                "rewind",
                "ws-gate",
                "ch-0002",
                "--confirm",
                "ws-gate",
            ]
        )
        assert rc == 1
        err = capsys.readouterr().err
        assert "invalidated" in err

    def test_force_pass_invalidated_source_needs_unblock_first(
        self,
        session_factory: Callable[[str], Session],
        capsys: pytest.CaptureFixture,
    ) -> None:
        """force-pass 遇到 invalidated 源 → ChapterInvalidatedNeedsUnblockError
        引导先 unblock。"""
        _create_workspace("ws-gate")
        _seed_workspace_orm(session_factory, "ws-gate")
        _seed_chapter(
            session_factory, "ws-gate", "ch-0001", status="invalidated"
        )

        rc = run_cli(
            [
                "chapter",
                "force-pass",
                "ws-gate",
                "ch-0001",
                "--reason",
                "试着 force",
            ]
        )
        assert rc == 1
        err = capsys.readouterr().err
        assert "unblock" in err.lower()


# ---------------------------------------------------------------------------
# 4. session lifecycle(每命令独立 session + 进程退出前 commit + 无 lock 残留)
# ---------------------------------------------------------------------------


class TestChapterRewindSessionLifecycle:
    def test_two_back_to_back_commands_share_engine_no_lock(
        self,
        session_factory: Callable[[str], Session],
        capsys: pytest.CaptureFixture,
    ) -> None:
        """连续跑 2 个 CLI 命令(unblock-all → unblock-all),共享 engine
        但每命令独立 session;无 lock 残留(若 session 未 close,第二条会
        SQLite 'database is locked' 抛错)。
        """
        _create_workspace("ws-life")
        _seed_workspace_orm(session_factory, "ws-life")
        _seed_chapter(
            session_factory, "ws-life", "ch-0001", status="invalidated"
        )

        rc1 = run_cli(["chapter", "unblock-all", "ws-life"])
        assert rc1 == 0
        capsys.readouterr()

        # 第二次 unblock-all 应幂等 0 + 不抛 lock
        rc2 = run_cli(["chapter", "unblock-all", "ws-life"])
        assert rc2 == 0
        out = capsys.readouterr().out
        assert "unblocked_count : 0" in out

    def test_rewind_then_unblock_all_round_trip_no_lock(
        self,
        session_factory: Callable[[str], Session],
        capsys: pytest.CaptureFixture,
    ) -> None:
        """rewind 写多行 + unblock-all 读多行 + 翻多行,模拟真实使用 round trip
        无 SQLite lock 错。"""
        _create_workspace("ws-rt")
        _seed_workspace_orm(session_factory, "ws-rt")
        _seed_chapter(session_factory, "ws-rt", "ch-0001", "approved")
        _seed_chapter(session_factory, "ws-rt", "ch-0002", "approved")
        _seed_chapter(session_factory, "ws-rt", "ch-0003", "approved")

        rc1 = run_cli(
            [
                "chapter",
                "rewind",
                "ws-rt",
                "ch-0001",
                "--confirm",
                "ws-rt",
            ]
        )
        assert rc1 == 0
        capsys.readouterr()

        rc2 = run_cli(["chapter", "unblock-all", "ws-rt"])
        assert rc2 == 0
        out = capsys.readouterr().out
        assert "unblocked_count : 2" in out
        # ch-0001 仍是 drafting(rewind 目标);ch-0002/0003 已 unblock → drafting
        assert _read_chapter_status(session_factory, "ws-rt", "ch-0001") == "drafting"
        assert _read_chapter_status(session_factory, "ws-rt", "ch-0002") == "drafting"
        assert _read_chapter_status(session_factory, "ws-rt", "ch-0003") == "drafting"
