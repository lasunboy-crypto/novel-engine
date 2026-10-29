"""ChapterService P3-B-06 router 集成测试。

覆盖 router 注入路径下的 5 个核心契约:

1. **router 注入生效**:``ChapterService.write`` → ``orchestrate_chapter`` →
   Writer / Reviewer / MemoryExtractor 都通过 ``router.route(<agent>)`` 取
   ``LLMClient``,而非 ``ChapterService.__init__`` 注入的 ``self._client``。
2. **agent 维度路由分流**:同一 router 配置不同 ``agent_routing`` 时,Writer
   / Reviewer / MemoryExtractor 命中各自 profile;``LLMClient`` 缓存按
   ``(provider, model, base_url)`` 三元组区分,不同 agent 使用不同 client
   实例(若 profile 三元组不同)。
3. **mock 链路完整流水线不破**:维护人默认 LLM_PROVIDER=mock 时,所有
   agent 都拿 ``MockLLMClient`` 跑 verdict=pass 完整链路 → APPROVED + extract。
4. **revise 路径同样走 router**:``ChapterService.revise`` 调
   ``revise_chapter`` 时 router 给定 → reviser agent 走 router。
5. **router_factory=None 时回退 legacy**:不注入 router_factory 时,
   ``ChapterService`` 仍走 ``self._client + self._model`` legacy 路径
   (P1/P2 兼容,P2-B-19 wiring 测试已经覆盖)。

走真 ``MockLLMClient`` + 真 ``ChapterService`` + 真 ``orchestrate_chapter`` +
真 ``ModelRouter``;mock 仅 LLM provider(对齐 CLAUDE.md §维护人环境约束)。
"""

from __future__ import annotations

from collections.abc import Callable
from pathlib import Path

import pytest
from sqlalchemy import create_engine
from sqlalchemy.orm import Session

from src.core.db import Base
from src.core.llm.base import LLMClient
from src.core.llm.mock_client import MockLLMClient
from src.core.memory.vector import VectorStore
from src.core.models.chapter_card import ChapterCard, ContinuityContext
from src.core.models.routing import RoutingConfig, RoutingRule, WriterProfilesConfig
from src.core.models.writer_profile import WriterProfile
from src.core.routing.router import ModelRouter
from src.core.state_machine.states import ChapterState
from src.core.workspace.workspace_paths import WorkspacePaths
from src.services.chapter_service import ChapterService
from src.services.workspace_service import WorkspaceService

pytestmark = pytest.mark.usefixtures("valid_character_admission")


# ---- fixtures + helpers -----------------------------------------------------


def _mock_embedding_fn(texts: list[str]) -> list[list[float]]:
    """4 维兜底 embedding;集成测不验距离语义。"""
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


def _make_card(
    chapter_id: str = "ch-0001",
    chapter_number: int = 1,
    *,
    workspace_id: str = "ws-router",
    is_arc_finale: bool = False,
    tension_level: int = 6,
) -> ChapterCard:
    return ChapterCard(
        chapter_id=chapter_id,
        workspace_id=workspace_id,
        volume_id="v01",
        chapter_number=chapter_number,
        title=f"第 {chapter_number} 章·router 集成",
        tension_level=tension_level,
        # 本文件测路由，不以一亿字上限让真实 MockLLMClient 合成巨型正文。
        # 使用正常章节上限，仍完整执行 Writer/Reviewer/Memory 管线。
        word_count_min=1,
        word_count_max=4500,
        plot_node_budget=2,
        must_achieve=[],
        must_unresolved=[],
        forbidden=[],
        pov_character="林衍",
        setting="测试场景",
        continuity_context=_make_continuity(),
        next_chapter_seed="待续",
        arc_phase="climax" if is_arc_finale else "rising",
        is_arc_finale=is_arc_finale,
        related_plot_threads=[],
        related_characters=["lin-yan"],
        review_threshold=7.5,
        revise_threshold=6.5,
        max_rewrites=1,
        max_revises=1,
    )


def _build_config(
    *,
    default: str = "p_default",
    rules: list[RoutingRule] | None = None,
    agent_routing: dict[str, str] | None = None,
    extra_profiles: dict[str, WriterProfile] | None = None,
) -> WriterProfilesConfig:
    """构造测试用 WriterProfilesConfig。

    默认包含两个 mock profile(``p_default`` / ``p_premium``);agent_routing
    可指定不同 agent 走不同 profile,触发缓存维度区分。
    """
    profiles: dict[str, WriterProfile] = {
        "p_default": WriterProfile(
            profile_id="p_default",
            provider="mock",
            model="mock-default",
            context_window=16000,
        ),
        "p_premium": WriterProfile(
            profile_id="p_premium",
            provider="mock",
            model="mock-premium",
            context_window=32000,
        ),
    }
    if extra_profiles:
        profiles.update(extra_profiles)

    return WriterProfilesConfig(
        profiles=profiles,
        routing=RoutingConfig(
            default=default,
            rules=rules or [],
            agent_routing=agent_routing or {},
        ),
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


def _make_session_factory(workspace_service: WorkspaceService) -> Callable[[str], Session]:
    def factory(workspace_id: str) -> Session:
        paths = WorkspacePaths(
            workspace_id, workspaces_root=workspace_service.workspaces_root
        )
        engine = create_engine(f"sqlite:///{paths.db_path}")
        Base.metadata.create_all(engine)
        return Session(engine)

    return factory


def _make_vector_store_factory(
    workspace_service: WorkspaceService,
) -> Callable[[str], VectorStore]:
    def factory(workspace_id: str) -> VectorStore:
        paths = WorkspacePaths(
            workspace_id, workspaces_root=workspace_service.workspaces_root
        )
        return VectorStore(
            workspace_id,
            embedding_fn=_mock_embedding_fn,
            persist_dir=paths.chroma_dir,
        )

    return factory


@pytest.fixture
def prepared_workspace(workspace_service: WorkspaceService) -> str:
    workspace_service.create(
        "ws-router",
        display_name="Router 集成测试",
        genre="test",
        premise="验证 P3-B-06 router",
        total_chapters=3,
    )
    return "ws-router"


# ---- 测试用例 ---------------------------------------------------------------


class TestRouterPathInjection:
    """契约 1:router 注入生效——agent 拿 router 派出的 client,而非
    ``ChapterService.__init__`` 的 ``self._client``。"""

    def test_write_uses_router_client_not_self_client(
        self,
        workspace_service: WorkspaceService,
        prepared_workspace: str,
    ) -> None:
        # 注入两个不同的 MockLLMClient:一个给 ``self._client``(应被忽略),
        # 一个由 router.client_factory 产出(应被实际使用)。通过累计调用计数
        # 区分。
        ignored_client = MockLLMClient()
        router_client = MockLLMClient()

        config = _build_config(default="p_default")
        router = ModelRouter(
            config, client_factory=lambda **kw: router_client
        )

        service = ChapterService(
            workspace_service=workspace_service,
            client=ignored_client,
            model="legacy-model-name",
            session_factory=_make_session_factory(workspace_service),
            vector_store_factory=_make_vector_store_factory(workspace_service),
            router_factory=lambda ws_id: router,
        )

        service.save_chapter_card(prepared_workspace, _make_card("ch-0001", 1))
        result = service.write(prepared_workspace, "ch-0001")

        assert result.final_state == ChapterState.APPROVED
        # router_client 至少被调过 3 次(writer + reviewer + memory_extractor);
        # ignored_client 完全没有被调过。
        assert router_client._total_calls >= 3
        assert ignored_client._total_calls == 0


class TestAgentRoutingDimension:
    """契约 2:agent_routing 维度分流 + LLMClient 缓存按三元组区分。"""

    def test_distinct_profiles_yield_distinct_clients(
        self,
        workspace_service: WorkspaceService,
        prepared_workspace: str,
    ) -> None:
        """Writer 走 p_default(model=mock-default),Reviewer / MemoryExtractor 走
        p_premium(model=mock-premium)→ ``ModelRouter._client_cache`` 应有
        2 个不同 key。"""

        config = _build_config(
            default="p_default",
            agent_routing={
                "writer": "p_default",
                "reviewer": "p_premium",
                "memory_extractor": "p_premium",
            },
        )
        # 每次 client_factory 调用产新实例,以让缓存 miss 时可区分
        constructed: list[LLMClient] = []

        def _factory(**kw):
            client = MockLLMClient()
            constructed.append(client)
            return client

        router = ModelRouter(config, client_factory=_factory)

        service = ChapterService(
            workspace_service=workspace_service,
            client=MockLLMClient(),
            model="legacy-model",
            session_factory=_make_session_factory(workspace_service),
            vector_store_factory=_make_vector_store_factory(workspace_service),
            router_factory=lambda ws_id: router,
        )
        service.save_chapter_card(prepared_workspace, _make_card("ch-0001", 1))
        result = service.write(prepared_workspace, "ch-0001")

        assert result.final_state == ChapterState.APPROVED
        # 缓存应有 2 个 key:(mock, mock-default, None) + (mock, mock-premium, None)
        assert len(router._client_cache) == 2
        assert len(constructed) == 2

    def test_writer_rule_matches_finale(
        self,
        workspace_service: WorkspaceService,
        prepared_workspace: str,
    ) -> None:
        """Writer 路径 ``is_arc_finale == True`` 命中规则升级 p_premium。"""
        config = _build_config(
            default="p_default",
            rules=[
                RoutingRule(
                    condition="is_arc_finale == True", profile="p_premium"
                )
            ],
            agent_routing={"writer": "p_default"},  # 兜底但 finale 章规则先命中
        )

        # 验证:Writer 命中 finale 规则后取 p_premium profile
        router = ModelRouter(config)
        finale_card = _make_card("ch-0001", 1, is_arc_finale=True)
        target = router.route("writer", chapter_card=finale_card)
        assert target.profile.profile_id == "p_premium"
        assert target.profile.model == "mock-premium"

        # 非 finale 章回退 agent_routing.writer = p_default
        normal_card = _make_card("ch-0002", 2, is_arc_finale=False)
        target2 = router.route("writer", chapter_card=normal_card)
        assert target2.profile.profile_id == "p_default"


class TestMockChainEndToEnd:
    """契约 3:mock 链路完整流水线 — router 注入下整链路 verdict=pass + extract。"""

    def test_write_approved_with_router_full_pipeline(
        self,
        workspace_service: WorkspaceService,
        prepared_workspace: str,
    ) -> None:
        config = _build_config(default="p_default")
        router_client = MockLLMClient()
        router = ModelRouter(
            config, client_factory=lambda **kw: router_client
        )

        service = ChapterService(
            workspace_service=workspace_service,
            client=MockLLMClient(),
            model="legacy-only",
            session_factory=_make_session_factory(workspace_service),
            vector_store_factory=_make_vector_store_factory(workspace_service),
            router_factory=lambda ws_id: router,
        )

        service.save_chapter_card(prepared_workspace, _make_card("ch-0001", 1))
        result = service.write(prepared_workspace, "ch-0001")

        # 主路径
        assert result.final_state == ChapterState.APPROVED
        assert result.final_version_id == 1
        assert result.final_path is not None and result.final_path.is_file()
        assert len(result.reviews) == 1
        assert result.reviews[0].verdict == "pass"

        # extract 跑完(对齐 P2-B-19 wiring 行为)
        paths = WorkspacePaths(
            prepared_workspace,
            workspaces_root=workspace_service.workspaces_root,
        )
        from src.core.db.models import (
            CharacterORM,
            PlotThreadORM,
            TimelineEntryORM,
        )

        engine = create_engine(f"sqlite:///{paths.db_path}")
        with Session(engine) as session:
            assert session.query(CharacterORM).count() == 1
            assert session.query(PlotThreadORM).count() == 1
            assert session.query(TimelineEntryORM).count() == 1


class TestReviseRouterPath:
    """契约 4:revise 局部修订路径同样走 router。"""

    def test_revise_uses_router(
        self,
        workspace_service: WorkspaceService,
        prepared_workspace: str,
    ) -> None:
        from src.core.models.review import ReviewIssue

        config = _build_config(default="p_default")
        router_client = MockLLMClient()
        ignored_client = MockLLMClient()
        router = ModelRouter(
            config, client_factory=lambda **kw: router_client
        )

        service = ChapterService(
            workspace_service=workspace_service,
            client=ignored_client,
            model="legacy-only",
            session_factory=_make_session_factory(workspace_service),
            vector_store_factory=_make_vector_store_factory(workspace_service),
            router_factory=lambda ws_id: router,
        )

        # 先 write 一章产 v1.md(走 router_client)
        service.save_chapter_card(prepared_workspace, _make_card("ch-0001", 1))
        service.write(prepared_workspace, "ch-0001")
        calls_after_write = router_client._total_calls

        # 再 revise:reviser agent 必须也走 router_client
        issue = ReviewIssue(
            paragraph_anchor="Mock 写作输出",
            issue_type="prose",
            description="待修订",
            suggestion="改语气",
        )
        service.revise(prepared_workspace, "ch-0001", issues=[issue])

        # router_client 调用计数增加(reviser 调用产生);ignored_client 仍 0
        assert router_client._total_calls > calls_after_write
        assert ignored_client._total_calls == 0


class TestLegacyFallback:
    """契约 5:router_factory=None 时回退 legacy 路径。"""

    def test_no_router_factory_uses_self_client(
        self,
        workspace_service: WorkspaceService,
        prepared_workspace: str,
    ) -> None:
        legacy_client = MockLLMClient()
        service = ChapterService(
            workspace_service=workspace_service,
            client=legacy_client,
            model="claude-sonnet-4-6",
            session_factory=_make_session_factory(workspace_service),
            vector_store_factory=_make_vector_store_factory(workspace_service),
            # router_factory 故意不传
        )

        service.save_chapter_card(prepared_workspace, _make_card("ch-0001", 1))
        result = service.write(prepared_workspace, "ch-0001")

        assert result.final_state == ChapterState.APPROVED
        # 只用 legacy_client,与 P2-B-19 wiring 路径行为完全一致
        assert legacy_client._total_calls >= 3
