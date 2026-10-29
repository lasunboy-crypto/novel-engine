"""v1.0.x wave 4 session 3a 章节完成 hook 接入测试。

覆盖历史 fail-open 摘要/回填函数，以及 R4 reviewer-pass 严格链的
``orchestrate → continuity ledger → ready`` 时序。force-pass 的 R4 严格链
由 ``test_chapter_rewind_service.py`` 覆盖。
"""

from __future__ import annotations

import json
from datetime import UTC, datetime
from pathlib import Path
from typing import Any

import pytest
from sqlalchemy import create_engine
from sqlalchemy.orm import Session, sessionmaker

from src.core.agents.chapter_summary_extractor import (
    extract_and_persist_chapter_summary_safely,
)
from src.core.db import Base, ChapterSummaryORM, WorkspaceORM
from src.core.llm.base import LLMClient, LLMResponse, Message, TokenUsage
from src.core.models.chapter_card import ChapterCard, ContinuityContext


REPO_PROMPTS = Path(__file__).resolve().parents[2] / "prompts"
_EVIDENCE_QUOTE = "林衍站在山门前。"


class _FakeLLMClient(LLMClient):
    """返预设 content 的 fake client。"""

    def __init__(
        self, content: str, *, raise_exc: Exception | None = None
    ) -> None:
        self._content = content
        self._raise_exc = raise_exc

    def complete(
        self,
        messages: list[Message],
        *,
        model: str | None = None,
        **gen_params: Any,
    ) -> LLMResponse:
        if self._raise_exc is not None:
            raise self._raise_exc
        return LLMResponse(
            content=self._content,
            model_used=f"fake-{model or 'default'}",
            usage=TokenUsage(input_tokens=10, output_tokens=20),
            finish_reason="end_turn",
        )


def _valid_payload_json(suffix: str = "") -> str:
    payload = {
        "summary_text": f"林衍在山门前拾起灰羽,玉佩落地撞响青石{suffix}",
        "pov_character": "lin-yan",
        "pov_evidence_quote": _EVIDENCE_QUOTE,
        "scene_setting": "山门外青石阶",
        "scene_setting_evidence_quote": _EVIDENCE_QUOTE,
        "key_events": [
            {
                "description": "林衍拾起灰羽",
                "actors": ["lin-yan"],
                "location": "山门",
                "evidence_quote": _EVIDENCE_QUOTE,
            }
        ],
        "character_interactions": [],
        "unfinished_actions": [
            {
                "description": "林衍欲转身离去未动",
                "is_immediate": False,
                "evidence_quote": _EVIDENCE_QUOTE,
            }
        ],
        "character_states": [
            {
                "character_id": "lin-yan",
                "state": "站在山门前",
                "evidence_quote": _EVIDENCE_QUOTE,
            }
        ],
        "established_facts": [
            {
                "fact": "林衍已经抵达山门",
                "evidence_quote": _EVIDENCE_QUOTE,
            }
        ],
        "revealed_mechanics": [],
    }
    return json.dumps(payload, ensure_ascii=False)


def _make_card(
    chapter_id: str = "ch-0001", chapter_number: int = 1
) -> ChapterCard:
    return ChapterCard(
        chapter_id=chapter_id,
        workspace_id="ws-hook-test",
        volume_id="v1",
        chapter_number=chapter_number,
        title=f"第 {chapter_number} 章",
        arc_phase="opening",
        is_arc_finale=False,
        tension_level=5,
        pov_character="lin-yan",
        setting="山门外",
        word_count_min=2800,
        word_count_max=4500,
        must_achieve=["事件 A"],
        must_unresolved=[],
        forbidden=[],
        related_plot_threads=[],
        related_characters=["lin-yan"],
        continuity_context=ContinuityContext(
            prev_chapter_tail="",
            time_gap="开篇",
            scene_continuation="switch",
            pov_change=False,
            pov_from=None,
            pov_to="lin-yan",
        ),
        emotional_arc="平静 → 紧张",
        next_chapter_seed="留白接续",
    )


@pytest.fixture
def tmp_workspace_paths(tmp_path: Path) -> Any:
    """构造临时工作区目录 + 写 final.md + card.json,返回 WorkspacePaths。"""
    from src.core.workspace.workspace_paths import WorkspacePaths

    ws_root = tmp_path / "workspaces"
    workspace_id = "ws-hook-test"
    paths = WorkspacePaths(workspace_id, workspaces_root=ws_root)

    # 写 final.md(自动建 chapter 子目录)
    chapter_id = "ch-0001"
    final_path = paths.chapter_version_file(chapter_id, "final.md")
    final_path.parent.mkdir(parents=True, exist_ok=True)
    final_path.write_text("林衍站在山门前。" * 200, encoding="utf-8")

    # 写 card JSON
    card = _make_card(chapter_id=chapter_id, chapter_number=1)
    card_path = paths.cards_dir / f"{chapter_id}.json"
    card_path.parent.mkdir(parents=True, exist_ok=True)
    card_path.write_text(card.model_dump_json(indent=2), encoding="utf-8")

    return paths


@pytest.fixture
def in_memory_session_factory() -> Any:
    """构造 in-memory SQLite + ChapterSummaryORM 表;返回 session_factory。

    用 ``Base.metadata.create_all`` 让 ORM 各表都建出,FK 不强制(SQLite 默认)。
    """
    engine = create_engine("sqlite:///:memory:")
    Base.metadata.create_all(engine)
    SessionLocal = sessionmaker(bind=engine, expire_on_commit=False)

    # 先 seed workspace 行满足 FK
    with SessionLocal() as session:
        now = datetime.now(UTC)
        session.add(
            WorkspaceORM(
                workspace_id="ws-hook-test",
                display_name="hook-test",
                genre="test",
                premise="p",
                created_at=now,
                last_active_at=now,
                engine_version="0.1.0",
                status="active",
                total_chapters=0,
                completed_chapters=0,
                config_overrides={},
            )
        )
        session.commit()

    def factory(workspace_id: str) -> Session:
        return SessionLocal()

    return factory, engine, SessionLocal


class TestExtractAndPersistSafely:
    """``extract_and_persist_chapter_summary_safely`` 独立模块函数。"""

    def test_happy_inserts_chapter_summary_row(
        self, tmp_workspace_paths: Any, in_memory_session_factory: Any
    ) -> None:
        """LLM 合规输出 + ORM 落行 + extraction_trigger=reviewer_pass。"""
        factory, _engine, SessionLocal = in_memory_session_factory
        fake = _FakeLLMClient(_valid_payload_json())

        extract_and_persist_chapter_summary_safely(
            workspace_id="ws-hook-test",
            chapter_id="ch-0001",
            trigger="reviewer_pass",
            paths=tmp_workspace_paths,
            session_factory=factory,
            llm_client=fake,
            llm_model="qwen-novel",
            prompts_dir=REPO_PROMPTS,
        )

        with SessionLocal() as session:
            row = session.get(ChapterSummaryORM, "ch-0001")
            assert row is not None
            assert row.workspace_id == "ws-hook-test"
            assert "林衍" in row.summary_text
            assert row.pov_character == "lin-yan"
            assert row.scene_setting == "山门外青石阶"
            assert row.extraction_trigger == "reviewer_pass"
            assert row.model_used == "fake-qwen-novel"
            assert row.unfinished_actions == ["林衍欲转身离去未动"]

    def test_force_pass_trigger_propagated(
        self, tmp_workspace_paths: Any, in_memory_session_factory: Any
    ) -> None:
        """trigger=force_pass 时 ORM 行 extraction_trigger 字段对应。"""
        factory, _engine, SessionLocal = in_memory_session_factory
        fake = _FakeLLMClient(_valid_payload_json())

        extract_and_persist_chapter_summary_safely(
            workspace_id="ws-hook-test",
            chapter_id="ch-0001",
            trigger="force_pass",
            paths=tmp_workspace_paths,
            session_factory=factory,
            llm_client=fake,
            llm_model="qwen-novel",
            prompts_dir=REPO_PROMPTS,
        )

        with SessionLocal() as session:
            row = session.get(ChapterSummaryORM, "ch-0001")
            assert row is not None
            assert row.extraction_trigger == "force_pass"

    def test_update_when_existing_row(
        self, tmp_workspace_paths: Any, in_memory_session_factory: Any
    ) -> None:
        """已存在 ORM 行(rewind 后重写) → upsert update 字段。"""
        factory, _engine, SessionLocal = in_memory_session_factory
        fake = _FakeLLMClient(_valid_payload_json())

        # 先 insert 一行(模拟之前抽过)
        with SessionLocal() as session:
            old_now = datetime(2020, 1, 1, tzinfo=UTC)
            session.add(
                ChapterSummaryORM(
                    chapter_id="ch-0001",
                    workspace_id="ws-hook-test",
                    summary_text="旧摘要",
                    pov_character=None,
                    scene_setting=None,
                    key_events=[],
                    character_interactions=[],
                    unfinished_actions=[],
                    extraction_trigger="reviewer_pass",
                    model_used="old-model",
                    created_at=old_now,
                    updated_at=old_now,
                )
            )
            session.commit()

        extract_and_persist_chapter_summary_safely(
            workspace_id="ws-hook-test",
            chapter_id="ch-0001",
            trigger="force_pass",
            paths=tmp_workspace_paths,
            session_factory=factory,
            llm_client=fake,
            llm_model="qwen-novel",
            prompts_dir=REPO_PROMPTS,
        )

        with SessionLocal() as session:
            row = session.get(ChapterSummaryORM, "ch-0001")
            assert row is not None
            assert "林衍" in row.summary_text  # 新摘要覆盖旧
            assert row.extraction_trigger == "force_pass"  # 触发源更新
            assert row.created_at.year == 2020  # 保留原 created_at
            assert row.updated_at.year != 2020  # updated_at 翻新

    def test_session_factory_none_returns_silently(
        self, tmp_workspace_paths: Any
    ) -> None:
        """``session_factory=None`` → 静默跳过,不抛。"""
        fake = _FakeLLMClient(_valid_payload_json())
        # 不抛即可,无返回值断言
        extract_and_persist_chapter_summary_safely(
            workspace_id="ws-hook-test",
            chapter_id="ch-0001",
            trigger="reviewer_pass",
            paths=tmp_workspace_paths,
            session_factory=None,
            llm_client=fake,
            llm_model="qwen-novel",
        )

    def test_llm_not_injected_returns_silently(
        self, tmp_workspace_paths: Any, in_memory_session_factory: Any
    ) -> None:
        """LLM client + model 均 None + router None → 静默跳过。"""
        factory, _engine, _SessionLocal = in_memory_session_factory
        extract_and_persist_chapter_summary_safely(
            workspace_id="ws-hook-test",
            chapter_id="ch-0001",
            trigger="reviewer_pass",
            paths=tmp_workspace_paths,
            session_factory=factory,
            llm_client=None,
            llm_model=None,
            router=None,
        )

    def test_no_final_md_returns_silently(
        self, tmp_path: Path, in_memory_session_factory: Any
    ) -> None:
        """final.md 缺失 → swallow + warn,不抛 + 不落 ORM。"""
        from src.core.workspace.workspace_paths import WorkspacePaths

        ws_root = tmp_path / "workspaces"
        paths = WorkspacePaths("ws-hook-test", workspaces_root=ws_root)
        paths.root.mkdir(parents=True, exist_ok=True)
        # 不写 final.md / card

        factory, _engine, SessionLocal = in_memory_session_factory
        fake = _FakeLLMClient(_valid_payload_json())

        extract_and_persist_chapter_summary_safely(
            workspace_id="ws-hook-test",
            chapter_id="ch-no-final",
            trigger="reviewer_pass",
            paths=paths,
            session_factory=factory,
            llm_client=fake,
            llm_model="qwen-novel",
            prompts_dir=REPO_PROMPTS,
        )

        with SessionLocal() as session:
            row = session.get(ChapterSummaryORM, "ch-no-final")
            assert row is None  # 无 final.md → 无 LLM → 无 ORM 行

    def test_llm_exception_swallows(
        self, tmp_workspace_paths: Any, in_memory_session_factory: Any
    ) -> None:
        """LLM 抛异常 → swallow,主流程继续,无 ORM 行。"""
        factory, _engine, SessionLocal = in_memory_session_factory
        fake = _FakeLLMClient(
            "(never returned)", raise_exc=RuntimeError("Qwen 503 sim")
        )

        # 不抛即通过
        extract_and_persist_chapter_summary_safely(
            workspace_id="ws-hook-test",
            chapter_id="ch-0001",
            trigger="reviewer_pass",
            paths=tmp_workspace_paths,
            session_factory=factory,
            llm_client=fake,
            llm_model="qwen-novel",
            prompts_dir=REPO_PROMPTS,
        )

        with SessionLocal() as session:
            row = session.get(ChapterSummaryORM, "ch-0001")
            assert row is None  # LLM 失败 → 无 ORM 行


class TestE1ChapterSummaryVectorPersist:
    """v1.1 wave 3 session 6(E1):章节摘要落库后同步写 Chroma。"""

    def test_vector_store_given_writes_to_chroma(
        self, tmp_workspace_paths: Any, in_memory_session_factory: Any
    ) -> None:
        """``vector_store`` 给定 → SQLite 落行 + Chroma add 一次,id / metadata /
        document 约定对齐 memory_extractor 五字段。"""
        from unittest.mock import MagicMock

        from src.core.memory.vector import VectorStore

        factory, _engine, SessionLocal = in_memory_session_factory
        fake = _FakeLLMClient(_valid_payload_json())
        vs = MagicMock(spec=VectorStore)

        extract_and_persist_chapter_summary_safely(
            workspace_id="ws-hook-test",
            chapter_id="ch-0001",
            trigger="reviewer_pass",
            paths=tmp_workspace_paths,
            session_factory=factory,
            llm_client=fake,
            llm_model="qwen-novel",
            prompts_dir=REPO_PROMPTS,
            vector_store=vs,
        )

        # SQLite 行仍落
        with SessionLocal() as session:
            assert session.get(ChapterSummaryORM, "ch-0001") is not None

        # Chroma add 一次,collection + id + metadata + document 约定正确
        assert vs.add.call_count == 1
        call = vs.add.call_args
        assert call.args[0] == "memories"
        assert call.kwargs["ids"] == ["chapter_summary-ch-0001"]
        meta = call.kwargs["metadatas"][0]
        assert meta["entity_type"] == "chapter_summary"
        assert meta["entity_id"] == "ch-0001"
        assert meta["chapter_id"] == "ch-0001"
        assert meta["workspace_id"] == "ws-hook-test"
        assert "created_at" in meta
        # document 含摘要正文 + key_events description
        doc = call.kwargs["documents"][0]
        assert "林衍" in doc
        assert "林衍拾起灰羽" in doc  # key_events[0].description 拼入

    def test_vector_store_none_skips_chroma(
        self, tmp_workspace_paths: Any, in_memory_session_factory: Any
    ) -> None:
        """``vector_store=None``(默认)→ 仅落 SQLite,不写 Chroma(向后兼容)。"""
        factory, _engine, SessionLocal = in_memory_session_factory
        fake = _FakeLLMClient(_valid_payload_json())

        # 不传 vector_store → 默认 None;不抛 + SQLite 落行
        extract_and_persist_chapter_summary_safely(
            workspace_id="ws-hook-test",
            chapter_id="ch-0001",
            trigger="reviewer_pass",
            paths=tmp_workspace_paths,
            session_factory=factory,
            llm_client=fake,
            llm_model="qwen-novel",
            prompts_dir=REPO_PROMPTS,
        )
        with SessionLocal() as session:
            assert session.get(ChapterSummaryORM, "ch-0001") is not None

    def test_chroma_write_failure_fail_open(
        self, tmp_workspace_paths: Any, in_memory_session_factory: Any
    ) -> None:
        """Chroma ``add`` 抛异常 → fail-open,不抛 + SQLite 行仍在(向量库非真相源)。"""
        from unittest.mock import MagicMock

        from src.core.memory.vector import VectorStore

        factory, _engine, SessionLocal = in_memory_session_factory
        fake = _FakeLLMClient(_valid_payload_json())
        vs = MagicMock(spec=VectorStore)
        vs.add.side_effect = RuntimeError("chroma down sim")

        # 不抛即通过
        extract_and_persist_chapter_summary_safely(
            workspace_id="ws-hook-test",
            chapter_id="ch-0001",
            trigger="reviewer_pass",
            paths=tmp_workspace_paths,
            session_factory=factory,
            llm_client=fake,
            llm_model="qwen-novel",
            prompts_dir=REPO_PROMPTS,
            vector_store=vs,
        )
        # SQLite 真相源已 commit,不受 Chroma 失败影响
        with SessionLocal() as session:
            assert session.get(ChapterSummaryORM, "ch-0001") is not None

    def test_no_chroma_write_when_sqlite_persist_fails(
        self, tmp_workspace_paths: Any
    ) -> None:
        """SQLite 落库失败(session_factory 抛)→ 不触发 Chroma 写(顺序保证)。"""
        from unittest.mock import MagicMock

        from src.core.memory.vector import VectorStore

        fake = _FakeLLMClient(_valid_payload_json())
        vs = MagicMock(spec=VectorStore)

        def _broken_factory(_ws: str) -> Any:
            raise RuntimeError("db open failed sim")

        # 不抛(persist 段 except 吞)+ Chroma 未被调(SQLite 失败提前 return)
        extract_and_persist_chapter_summary_safely(
            workspace_id="ws-hook-test",
            chapter_id="ch-0001",
            trigger="reviewer_pass",
            paths=tmp_workspace_paths,
            session_factory=_broken_factory,
            llm_client=fake,
            llm_model="qwen-novel",
            prompts_dir=REPO_PROMPTS,
            vector_store=vs,
        )
        vs.add.assert_not_called()


class TestChapterServiceIntegration:
    """ChapterService._extract_chapter_summary_hook 接入点测试。"""

    def test_chapter_service_hook_method_exists(self) -> None:
        """ChapterService 有 ``_extract_chapter_summary_hook`` 方法(签名校验)。"""
        from src.services.chapter_service import ChapterService

        assert hasattr(ChapterService, "_extract_chapter_summary_hook")
        # 签名:(self, workspace_id, chapter_id, *, trigger)
        import inspect

        sig = inspect.signature(ChapterService._extract_chapter_summary_hook)
        params = list(sig.parameters.keys())
        assert "workspace_id" in params
        assert "chapter_id" in params
        assert "trigger" in params


class TestChapterRewindServiceIntegration:
    """ChapterRewindService._extract_chapter_summary_hook 接入点测试。"""

    def test_rewind_service_hook_method_exists(self) -> None:
        """ChapterRewindService 有 ``_extract_chapter_summary_hook`` + LLM 注入字段。"""
        from src.services.chapter_rewind_service import ChapterRewindService

        assert hasattr(ChapterRewindService, "_extract_chapter_summary_hook")
        import inspect

        sig = inspect.signature(ChapterRewindService.__init__)
        params = list(sig.parameters.keys())
        # 新增 3 注入字段
        assert "llm_client" in params
        assert "llm_model" in params
        assert "router_factory" in params

    def test_rewind_service_default_llm_none_does_not_break_init(
        self,
    ) -> None:
        """ChapterRewindService 不传 LLM 字段时 __init__ 正常(默认 None)。"""
        from src.services.chapter_rewind_service import ChapterRewindService
        from src.services.workspace_service import WorkspaceService

        # 不实际调用 force_pass,只测构造不抛
        ws_svc = WorkspaceService(workspaces_root=Path("/tmp/dummy"))
        svc = ChapterRewindService(workspace_service=ws_svc)
        assert svc._llm_client is None  # type: ignore[attr-defined]
        assert svc._llm_model is None  # type: ignore[attr-defined]
        assert svc._router_factory is None  # type: ignore[attr-defined]


class TestChapterSummaryHookTimingFix:
    """v1.0.x wave 6 s2.6d:chapter_summary hook 时序修复回归测试。

    背景 — 修法前 hook 由 `_compose_state_callback` 的 `_combined` state callback
    在 `nxt == APPROVED` 时立即调,但 `_fire(prev, APPROVED)` 的时机**早于**
    `orchestrate_chapter` 循环出口的 `versioning.mark_final` — final.md 还没写
    时 hook 启动,撞 `chapter_summary_hook_no_final_md` warning 并 swallow 退出。
    luo-cloud-test 21 章实测 11 次 reviewer pass 9 次撞,只 ch-0010 因后续
    reviewer 二次 pass 时 final.md 已存才走通。

    修法 — hook 从 `_combined` 中移除,改在 `_orchestrate` 函数尾部、
    `orchestrate_chapter` 返回 OrchestratorResult **之后**调,与 orchestrator
    APPROVED 分支的 `_extract_memories_safely`(L644 mark_final 之后)时序对齐。

    本测试 mock orchestrate_chapter 验证:
    - APPROVED 返回 → hook 调一次
    - HUMAN_INTERVENTION 返回 → hook 不调
    - hook 调用时机晚于 on_state_transition 回调(state callback 内不再调 hook)
    """

    def test_summary_extractor_called_after_orchestrate_when_approved(
        self,
        monkeypatch: pytest.MonkeyPatch,
        tmp_path: Path,
    ) -> None:
        """APPROVED 后先同步小账本，再写 ready；两者都晚于 orchestrate 返回。"""
        from unittest.mock import MagicMock

        from src.core.orchestrator.chapter_orchestrator import OrchestratorResult
        from src.core.state_machine.states import ChapterState
        from src.services.chapter_service import ChapterService
        from src.services.workspace_service import WorkspaceService

        ws_root = tmp_path / "ws_root"
        ws_root.mkdir()
        ws_svc = WorkspaceService(workspaces_root=ws_root)
        ws_svc.create(
            "ws-timing",
            display_name="时序测试本",
            genre="scifi",
            premise="测试 race condition",
            total_chapters=10,
        )
        paths = ws_svc._paths("ws-timing")
        card = _make_card(chapter_id="ch-0001", chapter_number=1)
        (paths.cards_dir / "ch-0001.json").write_text(
            card.model_dump_json(indent=2), encoding="utf-8"
        )

        empty_session = MagicMock()
        empty_session.get.return_value = None
        empty_session.scalar.return_value = None
        empty_session.execute.return_value.all.return_value = []
        svc = ChapterService(
            workspace_service=ws_svc,
            client=_FakeLLMClient(content="ignored"),
            model="fake-model",
            # 保持小账本 hook 的有 DB 分支，但精确模拟“无章节行、
            # 无版本预算”；裸 MagicMock 会被权威预算读取当成伪造行。
            session_factory=lambda _workspace_id: empty_session,
        )
        monkeypatch.setattr(svc, "_upsert_chapter_state", lambda *a, **kw: None)
        monkeypatch.setattr(svc, "_run_gates", lambda *a, **kw: None)

        call_order: list[str] = []
        extractor_spy = MagicMock(
            side_effect=lambda *a, **kw: call_order.append("continuity_ledger")
        )
        monkeypatch.setattr(
            "src.services.chapter_service.extract_and_persist_continuity_ledger",
            extractor_spy,
        )
        monkeypatch.setattr(
            "src.services.chapter_service.CoreStateService.commit_approved_chapter",
            lambda _self, **_kwargs: call_order.append("core_state_commit"),
        )
        monkeypatch.setattr(
            svc,
            "_mark_ready_for_next_chapter",
            lambda **_kwargs: call_order.append("ready"),
        )
        monkeypatch.setattr(
            svc,
            "_schedule_memory_enrichment_after_ready",
            lambda **_kwargs: None,
        )

        def _fake_orchestrate(**_kwargs):
            call_order.append("orchestrate_return")
            return OrchestratorResult(
                final_state=ChapterState.APPROVED,
                escalation_reason=None,
                versions=[],
                reviews=[],
                final_path=None,
                final_version_id=7,
                revise_count=0,
                rewrite_count=0,
            )

        monkeypatch.setattr(
            "src.services.chapter_service.orchestrate_chapter", _fake_orchestrate
        )

        svc.write("ws-timing", "ch-0001")

        extractor_spy.assert_called_once()
        kwargs = extractor_spy.call_args.kwargs
        assert kwargs["workspace_id"] == "ws-timing"
        assert kwargs["chapter_id"] == "ch-0001"
        assert kwargs["source_version_id"] == 7
        assert call_order == [
            "orchestrate_return",
            "continuity_ledger",
            "core_state_commit",
            "ready",
        ], (
            "小账本与核心状态提交必须在 orchestrate 返回后、ready 前依次完成，"
            f"实测: {call_order}"
        )

    def test_summary_extractor_not_called_when_human_intervention(
        self,
        monkeypatch: pytest.MonkeyPatch,
        tmp_path: Path,
    ) -> None:
        """orchestrate_chapter 返 HUMAN_INTERVENTION → extractor 不调(无 final.md)。"""
        from unittest.mock import MagicMock

        from src.core.orchestrator.chapter_orchestrator import OrchestratorResult
        from src.core.state_machine.states import ChapterState
        from src.services.chapter_service import ChapterService
        from src.services.workspace_service import WorkspaceService

        ws_root = tmp_path / "ws_root"
        ws_root.mkdir()
        ws_svc = WorkspaceService(workspaces_root=ws_root)
        ws_svc.create(
            "ws-noapprove",
            display_name="升人工测试本",
            genre="scifi",
            premise="测试 HUMAN_INTERVENTION 不调 extractor",
            total_chapters=10,
        )
        paths = ws_svc._paths("ws-noapprove")
        card = _make_card(chapter_id="ch-0001", chapter_number=1)
        (paths.cards_dir / "ch-0001.json").write_text(
            card.model_dump_json(indent=2), encoding="utf-8"
        )

        svc = ChapterService(
            workspace_service=ws_svc,
            client=_FakeLLMClient(content="ignored"),
            model="fake-model",
        )

        extractor_spy = MagicMock()
        monkeypatch.setattr(
            "src.core.agents.chapter_summary_extractor."
            "extract_and_persist_chapter_summary_safely",
            extractor_spy,
        )

        def _fake_orchestrate(**_kwargs):
            return OrchestratorResult(
                final_state=ChapterState.HUMAN_INTERVENTION,
                escalation_reason="rewrite_budget_exhausted",
                versions=[],
                reviews=[],
                final_path=None,
                final_version_id=None,
                revise_count=0,
                rewrite_count=3,
            )

        monkeypatch.setattr(
            "src.services.chapter_service.orchestrate_chapter", _fake_orchestrate
        )

        svc.write("ws-noapprove", "ch-0001")

        # 升人工路径无 final.md,extractor 不调
        extractor_spy.assert_not_called()

    def test_state_callback_does_not_invoke_extractor_on_approved(
        self,
        monkeypatch: pytest.MonkeyPatch,
        tmp_path: Path,
    ) -> None:
        """state callback `_combined` 在 nxt=APPROVED 时**不再**调 extractor(防回归)。

        修法前 _combined 在 nxt==APPROVED 时调 chapter_summary hook,撞 race;
        修法后改在 _orchestrate 尾部 inline 调 extractor。本测试 mock
        orchestrate_chapter 让它**只调 state callback**(模拟 _fire(prev, APPROVED)),
        验证此时 extractor 不被调;然后让 mock 抛 RuntimeError 退出,确认即便
        orchestrator 中途异常,extractor 也未在 state callback 中触发。
        """
        from unittest.mock import MagicMock

        from src.core.state_machine.states import ChapterState
        from src.services.chapter_service import ChapterService
        from src.services.workspace_service import WorkspaceService

        ws_root = tmp_path / "ws_root"
        ws_root.mkdir()
        ws_svc = WorkspaceService(workspaces_root=ws_root)
        ws_svc.create(
            "ws-cbtest",
            display_name="state callback 测试本",
            genre="scifi",
            premise="测试 state callback 不再调 extractor",
            total_chapters=10,
        )
        paths = ws_svc._paths("ws-cbtest")
        card = _make_card(chapter_id="ch-0001", chapter_number=1)
        (paths.cards_dir / "ch-0001.json").write_text(
            card.model_dump_json(indent=2), encoding="utf-8"
        )

        svc = ChapterService(
            workspace_service=ws_svc,
            client=_FakeLLMClient(content="ignored"),
            model="fake-model",
        )

        extractor_spy = MagicMock()
        monkeypatch.setattr(
            "src.core.agents.chapter_summary_extractor."
            "extract_and_persist_chapter_summary_safely",
            extractor_spy,
        )

        def _fake_orchestrate(*, on_state_transition=None, **_kwargs):
            # 模拟 _fire(prev, APPROVED):调 state callback
            if on_state_transition is not None:
                on_state_transition(ChapterState.REVIEWING, ChapterState.APPROVED)
            # state callback 调完后 extractor **必须**仍未被调(本断言是核心防回归)
            assert extractor_spy.call_count == 0, (
                f"_combined state callback 不应再调 extractor (修法前 race 源头),"
                f"call_count={extractor_spy.call_count}"
            )
            # 抛错让 write 早退,不走 _orchestrate 尾部那段(避免污染本断言)
            raise RuntimeError("test_only_early_exit")

        monkeypatch.setattr(
            "src.services.chapter_service.orchestrate_chapter", _fake_orchestrate
        )

        with pytest.raises(RuntimeError, match="test_only_early_exit"):
            svc.write("ws-cbtest", "ch-0001")

        # _combined 内没调 extractor;_orchestrate 尾部因 raise 也未走到 → 0 次
        extractor_spy.assert_not_called()


@pytest.mark.parametrize("projection_drift", [False, True])
def test_summary_hook_passes_bound_current_core_values_before_call(
    tmp_workspace_paths, in_memory_session_factory, monkeypatch, projection_drift
):
    from types import SimpleNamespace
    from src.services.core_state_service import CoreStateService

    factory, _, _ = in_memory_session_factory
    card = _make_card().model_copy(update={
        "core_state_contract_version": 1, "core_state_projection_hash": "a" * 64,
    })
    monkeypatch.setattr(CoreStateService, "load_projection", lambda *a, **k: SimpleNamespace(
        projection_hash=("b" if projection_drift else "a") * 64,
        facts=[SimpleNamespace(state_key="character:lin-yan:permanent_injury", value="左臂旧伤")],
    ))
    calls = []

    class CapturingClient(_FakeLLMClient):
        def complete(self, messages, **kwargs):
            calls.append(messages)
            return super().complete(messages, **kwargs)

    result = extract_and_persist_chapter_summary_safely(
        workspace_id="ws-hook-test", chapter_id="ch-0001", trigger="reviewer_pass",
        paths=tmp_workspace_paths, session_factory=factory,
        llm_client=CapturingClient(_valid_payload_json()), llm_model="qwen-novel",
        prompts_dir=REPO_PROMPTS, chapter_card_snapshot=card,
    )
    if projection_drift:
        assert result is False
        assert calls == []
    else:
        assert result is True
        assert len(calls) == 1
        assert "左臂旧伤" in calls[0][-1].content
