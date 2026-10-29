"""ChapterService P2-B-19 wiring 集成测试。

覆盖 wiring 启用/不启用、APPROVED 终态后 extract 调用、HUMAN_INTERVENTION
终态不调 extract、extract 失败不冒泡四个核心契约。

走真 ``MockLLMClient`` + 真 ``ChapterService.write`` + 真 ``orchestrate_chapter``
+ 真 ``memory_extractor.extract`` + 真 ``VectorStore``(in-memory chroma);
mock 仅 LLM provider(对齐 CLAUDE.md §维护人环境约束:无 Claude API key)。

注:run prompt 走真文件(``prompts/`` 目录),与 P2-B-09 单测的"fake prompt
模板"模式不同——本任务是 wiring 集成测,要验整链路 prompt 加载 + marker
透传 + mock JSON 解析全跑通。
"""

from __future__ import annotations

from pathlib import Path

import pytest
from sqlalchemy import create_engine
from sqlalchemy.orm import Session

from src.core.agents.chapter_summary_extractor import (
    continuity_ledger_provenance,
    load_validated_continuity_ledger,
)
from src.core.db import Base
from src.core.db.models import (
    ChapterORM,
    CharacterORM,
    PlotThreadORM,
    TimelineEntryORM,
    WorkspaceORM,
)
from src.core.llm.mock_client import MockLLMClient
from src.core.memory.vector import VectorStore
from src.core.models.chapter_card import ChapterCard, ContinuityContext
from src.core.models.outline import WorldOutput
from src.core.models.setting_contract import SettingContract
from src.core.state_machine.states import ChapterState
from src.core.workspace.workspace_paths import WorkspacePaths
from src.services.chapter_service import (
    ChapterCardSettingStaleError,
    ChapterReReviewInvalidStateError,
    ChapterService,
    ChapterServiceError,
)
from src.services.asset_import import ensure_workspace_orm
from src.services.workspace_service import WorkspaceService

pytestmark = pytest.mark.usefixtures("valid_character_admission")


# ---- fixtures ---------------------------------------------------------------


def _mock_embedding_fn(texts: list[str]) -> list[list[float]]:
    """4 维兜底 embedding;wiring 集成测不验距离语义,只验"落库 + 检索 hit"。"""
    return [[0.1, 0.2, 0.3, 0.4] for _ in texts]


def _make_continuity(**overrides) -> ContinuityContext:
    payload = dict(
        prev_chapter_tail="",
        unfinished_actions=[],
        time_gap="新场景",
        scene_continuation="switch",
        pov_change=False,
        pov_from=None,
        pov_to="林衍",
    )
    payload.update(overrides)
    return ContinuityContext(**payload)


def _make_card(chapter_id: str = "ch-0001", chapter_number: int = 1) -> ChapterCard:
    return ChapterCard(
        chapter_id=chapter_id,
        workspace_id="ws-wiring",
        volume_id="v01",
        chapter_number=chapter_number,
        title=f"第 {chapter_number} 章·测试",
        tension_level=6,
        # P4-B-06:validator 默认放宽,本文件测 chapter_service wiring,validator 不参与。
        word_count_min=1,
        word_count_max=10**8,
        plot_node_budget=2,
        must_achieve=[],
        must_unresolved=[],
        forbidden=[],
        pov_character="林衍",
        setting="测试场景",
        continuity_context=_make_continuity(),
        next_chapter_seed="待续",
        arc_phase="rising",
        is_arc_finale=False,
        related_plot_threads=[],
        related_characters=["lin-yan"],
        review_threshold=7.5,
        revise_threshold=6.5,
        max_rewrites=1,
        max_revises=1,
    )


@pytest.fixture
def workspace_service(tmp_path: Path) -> WorkspaceService:
    workspaces_root = tmp_path / "workspaces"
    archives_root = tmp_path / "archives"
    workspaces_root.mkdir()
    archives_root.mkdir()
    return WorkspaceService(
        workspaces_root=workspaces_root, archives_root=archives_root
    )


@pytest.fixture
def chapter_service_with_wiring(
    workspace_service: WorkspaceService,
) -> ChapterService:
    """完整 wiring:session_factory + vector_store_factory 都注入。"""

    def _session_factory(workspace_id: str) -> Session:
        paths = WorkspacePaths(
            workspace_id, workspaces_root=workspace_service.workspaces_root
        )
        engine = create_engine(f"sqlite:///{paths.db_path}")
        Base.metadata.create_all(engine)
        return Session(engine)

    def _vector_store_factory(workspace_id: str) -> VectorStore:
        paths = WorkspacePaths(
            workspace_id, workspaces_root=workspace_service.workspaces_root
        )
        return VectorStore(
            workspace_id,
            embedding_fn=_mock_embedding_fn,
            persist_dir=paths.chroma_dir,
        )

    return ChapterService(
        workspace_service=workspace_service,
        client=MockLLMClient(),
        model="claude-sonnet-4-6",
        session_factory=_session_factory,
        vector_store_factory=_vector_store_factory,
    )


@pytest.fixture
def chapter_service_no_wiring(
    workspace_service: WorkspaceService,
) -> ChapterService:
    """P1 兼容路径:不传 vector_store_factory,session_factory 也不传——
    完全等价于 P1 ChapterService。"""

    return ChapterService(
        workspace_service=workspace_service,
        client=MockLLMClient(),
        model="claude-sonnet-4-6",
    )


@pytest.fixture
def prepared_workspace(workspace_service: WorkspaceService) -> str:
    """创建 ws-wiring 工作区。章节卡由各 test 自行落,以便控制 chapter_id。"""
    workspace_service.create(
        "ws-wiring",
        display_name="Wiring 集成测试",
        genre="test",
        premise="验证 P2-B-19 集成",
        total_chapters=3,
    )
    return "ws-wiring"


# ---- 测试用例 ---------------------------------------------------------------


class TestWiringEnabledApprovedPath:
    """wiring 启用 + verdict=pass 路径:extract 真跑 + SQLite + Chroma 都有数据。"""

    def test_write_approved_extracts_memories(
        self,
        chapter_service_with_wiring: ChapterService,
        workspace_service: WorkspaceService,
        prepared_workspace: str,
    ) -> None:
        # MockLLMClient 默认 verdict=pass(env 未设)
        chapter_service_with_wiring.save_chapter_card(
            prepared_workspace, _make_card("ch-0001", 1)
        )
        result = chapter_service_with_wiring.write(prepared_workspace, "ch-0001")

        # 主路径正确(不破坏 P1 行为)
        assert result.final_state == ChapterState.APPROVED
        assert result.final_version_id == 1
        assert result.final_path is not None and result.final_path.is_file()
        assert len(result.reviews) == 1
        assert result.reviews[0].verdict == "pass"

        # P2-B-19 wiring 验收:SQLite 三表各 1 行(mock extractor 产)
        paths = WorkspacePaths(
            prepared_workspace,
            workspaces_root=workspace_service.workspaces_root,
        )
        engine = create_engine(f"sqlite:///{paths.db_path}")
        with Session(engine) as session:
            characters = session.query(CharacterORM).all()
            threads = session.query(PlotThreadORM).all()
            timeline = session.query(TimelineEntryORM).all()

        assert len(characters) == 1
        assert characters[0].character_id == "lin-yan"
        assert characters[0].name == "林衍"
        assert characters[0].workspace_id == "ws-wiring"
        # P2-B-09 决策 6:first_appearance_chapter 在 update 路径保留原值;
        # 本章是首次抽取(首次 create),first_appearance_chapter == 本章
        assert characters[0].first_appearance_chapter == "ch-0001"

        assert len(threads) == 1
        assert threads[0].thread_id == "zhu-jue-cheng-zhang"
        assert threads[0].status == "developing"

        assert len(timeline) == 1
        assert timeline[0].entry_id == "mock-event-ch-0001"
        # S3(v1.1 w4 b1):time_order 硬注入 = 全局章序*100 + 章内rank;
        # ch-0001(全局序1)单条 → 100 + 1 = 101。
        assert timeline[0].time_order == 101

        # P2-B-19 wiring 验收:Chroma 6 条(1 char + 1 thread + 1 timeline + 2 facts
        # + v1.1 wave 3 s6 E1: 1 chapter_summary)
        vs = VectorStore(
            "ws-wiring",
            embedding_fn=_mock_embedding_fn,
            persist_dir=paths.chroma_dir,
        )
        assert vs.count("memories") == 6

    def test_two_chapter_extract_produces_distinct_facts(
        self,
        chapter_service_with_wiring: ChapterService,
        workspace_service: WorkspaceService,
        prepared_workspace: str,
    ) -> None:
        """两章连写:character/thread 共享 ID 走 update 路径(SQLite 仍 1 行 +
        1 行),timeline/facts 每章各加(1 timeline + 2 facts)。Chroma 共 10 条
        (1 char + 1 thread + 2 timeline + 4 facts;character/thread 走 upsert 同
        ID 替换;+ v1.1 wave 3 s6 E1: 2 chapter_summary,ch-0001 / ch-0002 各 1)。"""
        chapter_service_with_wiring.save_chapter_card(
            prepared_workspace, _make_card("ch-0001", 1)
        )

        chapter_service_with_wiring.write(prepared_workspace, "ch-0001")

        # R4:第二章卡必须在第一章 ledger ready 后冻结实际 provenance/投影。
        paths = WorkspacePaths(
            prepared_workspace,
            workspaces_root=workspace_service.workspaces_root,
        )
        session_factory = chapter_service_with_wiring._session_factory
        assert session_factory is not None
        ledger = load_validated_continuity_ledger(
            workspace_id=prepared_workspace,
            chapter_id="ch-0001",
            paths=paths,
            session_factory=session_factory,
        )
        card2 = _make_card("ch-0002", 2)
        card2 = card2.model_copy(
            update={
                "continuity_context": card2.continuity_context.model_copy(
                    update={
                        "prev_chapter_tail": ledger.raw_final_tail,
                        "unfinished_actions": ledger.unfinished_actions,
                        "pov_from": ledger.pov_character,
                    }
                ),
                "continuity_ledger_provenance": continuity_ledger_provenance(
                    ledger
                ),
            }
        )
        chapter_service_with_wiring.save_chapter_card(
            prepared_workspace, card2
        )
        chapter_service_with_wiring.write(prepared_workspace, "ch-0002")

        engine = create_engine(f"sqlite:///{paths.db_path}")
        with Session(engine) as session:
            characters = session.query(CharacterORM).all()
            threads = session.query(PlotThreadORM).all()
            timeline = session.query(TimelineEntryORM).order_by(
                TimelineEntryORM.time_order
            ).all()

        # character / thread 跨章共享 ID,走 update 路径
        assert len(characters) == 1
        assert characters[0].first_appearance_chapter == "ch-0001"
        assert characters[0].last_updated_chapter == "ch-0002"
        assert len(threads) == 1
        # timeline 每章独立
        assert len(timeline) == 2
        assert timeline[0].entry_id == "mock-event-ch-0001"
        # S3:time_order 硬注入 = 全局章序*100 + 章内rank(各章单条 → x01)。
        assert timeline[0].time_order == 101  # ch-0001 → 100 + 1
        assert timeline[1].entry_id == "mock-event-ch-0002"
        assert timeline[1].time_order == 201  # ch-0002 → 200 + 1

        # Chroma:character/thread 同 ID upsert 各 1 条 + timeline 2 条 + facts 4 条
        # + v1.1 wave 3 s6 E1: chapter_summary 2 条(每章 1)= 10
        vs = VectorStore(
            "ws-wiring",
            embedding_fn=_mock_embedding_fn,
            persist_dir=paths.chroma_dir,
        )
        assert vs.count("memories") == 10


class TestSettingContractFailClosedR1:
    def test_committed_outline_missing_world_stops_before_llm(
        self,
        chapter_service_with_wiring: ChapterService,
        workspace_service: WorkspaceService,
        prepared_workspace: str,
        monkeypatch: pytest.MonkeyPatch,
    ) -> None:
        chapter_service_with_wiring.save_chapter_card(
            prepared_workspace, _make_card("ch-0001", 1)
        )
        paths = WorkspacePaths(
            prepared_workspace,
            workspaces_root=workspace_service.workspaces_root,
        )
        engine = create_engine(f"sqlite:///{paths.db_path}")
        Base.metadata.create_all(engine)
        with Session(engine) as session:
            ensure_workspace_orm(
                session, workspace_service.info(prepared_workspace)
            )
            row = session.get(WorkspaceORM, prepared_workspace)
            assert row is not None
            row.outline_status = "committed"
            session.commit()

        calls = {"count": 0}

        def fail_if_called(*args, **kwargs):  # type: ignore[no-untyped-def]
            calls["count"] += 1
            raise AssertionError("缺权威合同后不得调用 LLM")

        monkeypatch.setattr(
            chapter_service_with_wiring._client, "complete", fail_if_called
        )
        with pytest.raises(ChapterServiceError) as exc_info:
            chapter_service_with_wiring.write(
                prepared_workspace, "ch-0001"
            )

        assert str(exc_info.value) == (
            "权威世界契约缺失:data/outline/world.json"
        )
        assert calls["count"] == 0


class TestChapterCardSettingStaleR3:
    def test_stale_hash_stops_before_writer_and_chapter_state_write(
        self,
        chapter_service_with_wiring: ChapterService,
        workspace_service: WorkspaceService,
        prepared_workspace: str,
        monkeypatch: pytest.MonkeyPatch,
    ) -> None:
        world = WorldOutput(
            setting="当代中国西北戈壁",
            power_system="青铜镜回溯映射",
            factions="考古队与陆渊势力",
            culture="现代考古调查",
            unique_rules="记忆抹除不可逆。",
        )
        paths = WorkspacePaths(
            prepared_workspace,
            workspaces_root=workspace_service.workspaces_root,
        )
        paths.outline_dir.mkdir(parents=True, exist_ok=True)
        (paths.outline_dir / "world.json").write_text(
            world.model_dump_json(indent=2), encoding="utf-8"
        )
        current_hash = SettingContract.from_world_output(world).source_hash

        calls = {"count": 0}

        def fail_if_called(*args, **kwargs):  # type: ignore[no-untyped-def]
            calls["count"] += 1
            raise AssertionError("stale gate 后不得调用 Writer/任何 LLM")

        monkeypatch.setattr(
            chapter_service_with_wiring._client, "complete", fail_if_called
        )
        for card_hash in ("", "d" * 64):
            stale_card = _make_card("ch-0001", 1).model_copy(
                update={"setting_contract_hash": card_hash}
            )
            chapter_service_with_wiring.save_chapter_card(
                prepared_workspace, stale_card
            )
            with pytest.raises(ChapterCardSettingStaleError) as exc_info:
                chapter_service_with_wiring.write(
                    prepared_workspace, "ch-0001"
                )
            assert exc_info.value.card_contract_hash == card_hash
            assert exc_info.value.current_contract_hash == current_hash
        assert calls["count"] == 0
        engine = create_engine(f"sqlite:///{paths.db_path}")
        with Session(engine) as session:
            assert session.get(ChapterORM, "ch-0001") is None


class TestWiringEnabledHumanInterventionPath:
    """wiring 启用但 verdict=rewrite 升人工:不调 extract → SQLite + Chroma 空。"""

    def test_rewrite_budget_exhausted_skips_extract(
        self,
        chapter_service_with_wiring: ChapterService,
        workspace_service: WorkspaceService,
        prepared_workspace: str,
        monkeypatch: pytest.MonkeyPatch,
    ) -> None:
        # 强制 verdict=rewrite + max_rewrites=1 → 2 次写后升人工
        monkeypatch.setenv("LLM_MOCK_VERDICT", "rewrite")
        chapter_service_with_wiring.save_chapter_card(
            prepared_workspace, _make_card("ch-0001", 1)
        )
        result = chapter_service_with_wiring.write(prepared_workspace, "ch-0001")

        assert result.final_state == ChapterState.HUMAN_INTERVENTION
        assert result.escalation_reason == "rewrite_budget_exhausted"
        assert result.final_path is None  # 未通过审稿,无 final.md

        # P2-B-19 wiring 验收:HUMAN_INTERVENTION 路径不应触发 extract
        paths = WorkspacePaths(
            prepared_workspace,
            workspaces_root=workspace_service.workspaces_root,
        )
        engine = create_engine(f"sqlite:///{paths.db_path}")
        with Session(engine) as session:
            assert session.query(CharacterORM).count() == 0
            assert session.query(PlotThreadORM).count() == 0
            assert session.query(TimelineEntryORM).count() == 0

        # Chroma collection 不存在或为 0
        vs = VectorStore(
            "ws-wiring",
            embedding_fn=_mock_embedding_fn,
            persist_dir=paths.chroma_dir,
        )
        # vs.count 对不存在的 collection 兜底返 0
        assert vs.count("memories") == 0


class TestWiringDisabledP1Compat:
    """wiring 不启用(任一 factory=None):退化为 P1 路径,无 voice / 检索 / extract。"""

    def test_no_factory_p1_path_works(
        self,
        chapter_service_no_wiring: ChapterService,
        prepared_workspace: str,
    ) -> None:
        chapter_service_no_wiring.save_chapter_card(
            prepared_workspace, _make_card("ch-0001", 1)
        )
        result = chapter_service_no_wiring.write(prepared_workspace, "ch-0001")
        assert result.final_state == ChapterState.APPROVED
        assert result.final_path is not None and result.final_path.is_file()
        # P1 路径不动 SQLite / Chroma,无可断言副作用——只验证主路径不抛错。


class TestWiringExtractFailureSwallowed:
    """extract 失败(模拟 LLM 抛错)→ structlog error + 不冒泡;final_state 仍 APPROVED。"""

    def test_extract_llm_failure_does_not_propagate(
        self,
        workspace_service: WorkspaceService,
        prepared_workspace: str,
    ) -> None:
        """让 LLM 在 extractor 分支抛 RuntimeError(走"非 mock 失败"路径)。

        事件捕获:用 ``structlog.testing.capture_logs``(对齐
        ``tests/unit/test_observability_events.py`` 模式;P1 已验 caplog 抓不
        到 structlog stream events)。
        """
        from structlog.testing import capture_logs

        class FailExtractorMockLLM(MockLLMClient):
            """重写 complete:writer/reviewer 走原路径,extractor 分支抛错。"""

            def complete(self, messages, *, model=None, **gen_params):
                from src.core.agents._prompt import detect_agent_role

                role, _ = detect_agent_role(messages)
                if role == "extractor":
                    raise RuntimeError("[test] extractor 故意失败")
                return super().complete(messages, model=model, **gen_params)

        def _session_factory(workspace_id: str) -> Session:
            paths = WorkspacePaths(
                workspace_id, workspaces_root=workspace_service.workspaces_root
            )
            engine = create_engine(f"sqlite:///{paths.db_path}")
            Base.metadata.create_all(engine)
            return Session(engine)

        def _vector_store_factory(workspace_id: str) -> VectorStore:
            paths = WorkspacePaths(
                workspace_id, workspaces_root=workspace_service.workspaces_root
            )
            return VectorStore(
                workspace_id,
                embedding_fn=_mock_embedding_fn,
                persist_dir=paths.chroma_dir,
            )

        svc = ChapterService(
            workspace_service=workspace_service,
            client=FailExtractorMockLLM(),
            model="claude-sonnet-4-6",
            session_factory=_session_factory,
            vector_store_factory=_vector_store_factory,
        )

        svc.save_chapter_card(prepared_workspace, _make_card("ch-0001", 1))

        with capture_logs() as logs:
            result = svc.write(prepared_workspace, "ch-0001")

        assert result.final_state == ChapterState.APPROVED
        assert result.final_path is not None and result.final_path.is_file()

        extraction_failed = [
            log
            for log in logs
            if log.get("event") == "memory_extraction_failed"
        ]
        assert len(extraction_failed) == 1, (
            f"应有 1 条 memory_extraction_failed 事件,实得 {len(extraction_failed)};"
            f"全部事件: {[log.get('event') for log in logs]}"
        )
        evt = extraction_failed[0]
        assert evt["error_type"] == "RuntimeError"
        assert "[test] extractor 故意失败" in evt["error_message"]
        assert evt["stage"] == "extract"
        assert evt["chapter_id"] == "ch-0001"


class TestReReviewWiring:
    """v2 W3.5 UI 缺口3:wired re_review 端到端(状态从 DB 读、详情落库)。

    路由层(test_chapters_routes)无 wiring 只能验 guard plumbing;本类用
    完整 wiring(MockLLMClient)验真实 pass→APPROVED / 非 pass→human +
    re_review_failed + 详情同步到 status 的全链路。
    """

    def _drive_to_human_intervention(
        self,
        svc: ChapterService,
        workspace_id: str,
        monkeypatch: pytest.MonkeyPatch,
    ) -> None:
        """env=rewrite + max_rewrites=1 → write 直接升 human_intervention(留版本)。"""
        svc.save_chapter_card(workspace_id, _make_card("ch-0001", 1))
        monkeypatch.setenv("LLM_MOCK_VERDICT", "rewrite")
        wr = svc.write(workspace_id, "ch-0001")
        assert wr.final_state == ChapterState.HUMAN_INTERVENTION

    def test_re_review_pass_approves(
        self,
        chapter_service_with_wiring: ChapterService,
        prepared_workspace: str,
        monkeypatch: pytest.MonkeyPatch,
    ) -> None:
        svc = chapter_service_with_wiring
        self._drive_to_human_intervention(svc, prepared_workspace, monkeypatch)
        # 人工改稿后重审,这次 reviewer 判 pass(去掉 rewrite 强制)。
        monkeypatch.delenv("LLM_MOCK_VERDICT", raising=False)
        rr = svc.re_review(prepared_workspace, "ch-0001")

        assert rr.final_state == ChapterState.APPROVED
        assert rr.final_path is not None and rr.final_path.is_file()
        assert rr.escalation_reason is None
        # 状态机节点落库 = approved。
        assert svc.status(prepared_workspace, "ch-0001").status == "approved"

    def test_re_review_fail_back_to_human_with_details(
        self,
        chapter_service_with_wiring: ChapterService,
        prepared_workspace: str,
        monkeypatch: pytest.MonkeyPatch,
    ) -> None:
        svc = chapter_service_with_wiring
        self._drive_to_human_intervention(svc, prepared_workspace, monkeypatch)
        # 重审仍 rewrite(env 未撤)→ re_review_failed,不自动重写。
        rr = svc.re_review(prepared_workspace, "ch-0001")

        assert rr.final_state == ChapterState.HUMAN_INTERVENTION
        assert rr.escalation_reason == "re_review_failed"
        assert rr.escalation_details is not None
        assert rr.escalation_details["checked_at_state"] == "re_review"
        assert rr.final_path is None
        # 详情同步到 status(含 reason),供前端 banner 渲染。
        info = svc.status(prepared_workspace, "ch-0001")
        assert info.status == "human_intervention"
        assert info.escalation_details is not None
        assert info.escalation_details["reason"] == "re_review_failed"
        assert "issues" in info.escalation_details

    def test_re_review_wrong_state_raises(
        self,
        chapter_service_with_wiring: ChapterService,
        prepared_workspace: str,
    ) -> None:
        svc = chapter_service_with_wiring
        svc.save_chapter_card(prepared_workspace, _make_card("ch-0001", 1))
        # 默认 verdict=pass → write 直接 APPROVED。
        wr = svc.write(prepared_workspace, "ch-0001")
        assert wr.final_state == ChapterState.APPROVED
        # 非 human_intervention 重审 → guard 抛错。
        with pytest.raises(ChapterReReviewInvalidStateError):
            svc.re_review(prepared_workspace, "ch-0001")
