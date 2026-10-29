"""ChapterService P3-B-11 profile_override 集成测试(P3-F-03 类 C 顺延)。

覆盖单次写作 profile 覆盖路径下的 4 个核心契约:

1. **profile_override 透传命中**:``ChapterService.write(profile_override="p_premium")``
   → wrap ``_ProfileOverrideRouter`` → 5 agent 全部走 ``p_premium`` profile,
   不论 ``RoutingConfig.rules`` / ``agent_routing`` 怎么配。
2. **profile_override=None 走原 routing**:不传 ``profile_override`` 时
   ``_orchestrate`` 不构造 ``_ProfileOverrideRouter``,所有 agent 按
   ``agent_routing`` / ``RoutingConfig.rules`` 决定 profile(回归 P3-B-06 行为)。
3. **profile_override 非法 → ProfileNotFoundError**:``profile_id`` 不在
   ``writer_profiles`` 字典声明时,``_ProfileOverrideRouter.__init__`` 调
   ``ModelRouter.has_profile`` 校验抛 ``ProfileNotFoundError``。API 层翻译为
   422 ``CHAPTER_INVALID_PROFILE``(本测试只验抛错路径,API 层翻译有
   独立测试覆盖)。
4. **rewrite 路径同样走 profile_override**:``ChapterService.rewrite(
   profile_override=...)`` 同 write 路径,所有 agent 命中 override profile。

走真 ``MockLLMClient`` + 真 ``ChapterService`` + 真 ``orchestrate_chapter`` +
真 ``ModelRouter``;mock 仅 LLM provider。
"""

from __future__ import annotations

from collections.abc import Callable
from pathlib import Path

import pytest
from sqlalchemy import create_engine
from sqlalchemy.orm import Session

from src.core.db import Base
from src.core.llm.mock_client import MockLLMClient
from src.core.memory.vector import VectorStore
from src.core.models.chapter_card import ChapterCard, ContinuityContext
from src.core.models.routing import RoutingConfig, RoutingRule, WriterProfilesConfig
from src.core.models.writer_profile import WriterProfile
from src.core.routing.router import ModelRouter
from src.core.state_machine.states import ChapterState
from src.core.workspace.workspace_paths import WorkspacePaths
from src.services.chapter_service import ChapterService
from src.services.profile_override import (
    ProfileNotFoundError,
    _ProfileOverrideRouter,
    wrap_router_with_profile_override,
)
from src.services.workspace_service import WorkspaceService

pytestmark = pytest.mark.usefixtures("valid_character_admission")


# ---- fixtures + helpers -----------------------------------------------------


def _mock_embedding_fn(texts: list[str]) -> list[list[float]]:
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
    workspace_id: str = "ws-override",
) -> ChapterCard:
    return ChapterCard(
        chapter_id=chapter_id,
        workspace_id=workspace_id,
        volume_id="v01",
        chapter_number=chapter_number,
        title=f"第 {chapter_number} 章·profile override 集成",
        tension_level=6,
        # P4-B-06:validator 默认放宽,本文件测 profile override,validator 不参与。
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


def _build_config(
    *,
    default: str = "p_default",
    rules: list[RoutingRule] | None = None,
    agent_routing: dict[str, str] | None = None,
) -> WriterProfilesConfig:
    """两 profile 配置:p_default(model=mock-default)+ p_premium(model=mock-premium)。"""
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


def _make_session_factory(
    workspace_service: WorkspaceService,
) -> Callable[[str], Session]:
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
        "ws-override",
        display_name="Profile Override 集成",
        genre="test",
        premise="验证 P3-B-11 profile_override",
        total_chapters=3,
    )
    return "ws-override"


# ---- 单元测:wrap 形态 ------------------------------------------------------


class TestWrapHelper:
    """``wrap_router_with_profile_override`` 行为契约。"""

    def test_none_returns_router_unchanged(self) -> None:
        router = ModelRouter(_build_config())
        wrapped = wrap_router_with_profile_override(router, None)
        assert wrapped is router

    def test_valid_profile_wraps(self) -> None:
        router = ModelRouter(_build_config())
        wrapped = wrap_router_with_profile_override(router, "p_premium")
        assert isinstance(wrapped, _ProfileOverrideRouter)

    def test_invalid_profile_raises(self) -> None:
        router = ModelRouter(_build_config())
        with pytest.raises(ProfileNotFoundError) as excinfo:
            wrap_router_with_profile_override(router, "p_unknown")
        assert "p_unknown" in str(excinfo.value)


class TestProfileOverrideRouterRoute:
    """``_ProfileOverrideRouter.route`` 强制注入 override 行为。"""

    def test_route_overrides_writer_rule_matching(self) -> None:
        """Writer 路径 ``is_arc_finale=True`` 命中规则升 p_premium 时,
        wrap profile_override=p_default 应强制回到 p_default。"""
        config = _build_config(
            default="p_default",
            rules=[
                RoutingRule(
                    condition="is_arc_finale == True", profile="p_premium"
                )
            ],
        )
        inner = ModelRouter(config)
        wrap = _ProfileOverrideRouter(inner, "p_default")

        finale_card = _make_card("ch-0001", 1)
        finale_card_dict = finale_card.model_dump()
        finale_card_dict["is_arc_finale"] = True

        target = wrap.route("writer", chapter_card=finale_card_dict)
        # 规则匹配会路由到 p_premium,但 wrap 强制 override 到 p_default
        assert target.profile.profile_id == "p_default"

    def test_route_overrides_agent_routing(self) -> None:
        """``agent_routing.reviewer=p_default`` + wrap profile_override=p_premium
        → reviewer 取 p_premium。"""
        config = _build_config(
            default="p_default",
            agent_routing={"reviewer": "p_default"},
        )
        wrap = _ProfileOverrideRouter(ModelRouter(config), "p_premium")
        target = wrap.route("reviewer")
        assert target.profile.profile_id == "p_premium"

    def test_caller_override_takes_priority(self) -> None:
        """caller 显式传 ``override_profile_id`` 优先于 wrap 内部 override
        (语义:caller 有更高优先级,虽然实际 5 agent 不主动传)。"""
        wrap = _ProfileOverrideRouter(ModelRouter(_build_config()), "p_default")
        target = wrap.route("writer", override_profile_id="p_premium")
        assert target.profile.profile_id == "p_premium"


# ---- 集成测:ChapterService 路径 -------------------------------------------


class TestProfileOverrideEndToEnd:
    """契约 1 + 4:profile_override 透传命中(write + rewrite)。"""

    def test_write_with_profile_override_routes_all_agents(
        self,
        workspace_service: WorkspaceService,
        prepared_workspace: str,
    ) -> None:
        """``service.write(profile_override="p_premium")`` 后所有 agent
        命中 p_premium → ``ModelRouter._client_cache`` 只有 1 个 key
        (mock-premium 三元组),不论 agent_routing 怎么配。"""
        config = _build_config(
            default="p_default",
            agent_routing={
                "writer": "p_default",
                "reviewer": "p_default",
                "memory_extractor": "p_default",
            },
        )
        constructed_models: list[str] = []

        def _factory(**kw):
            constructed_models.append(kw["model"])
            return MockLLMClient()

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
        result = service.write(
            prepared_workspace,
            "ch-0001",
            profile_override="p_premium",
        )

        assert result.final_state == ChapterState.APPROVED
        # 所有 agent 都命中 mock-premium → 只构造一种 client
        assert constructed_models == ["mock-premium"]

    def test_rewrite_with_profile_override_routes_all_agents(
        self,
        workspace_service: WorkspaceService,
        prepared_workspace: str,
    ) -> None:
        """rewrite 路径同样走 profile_override(契约 4)。"""
        config = _build_config(default="p_default")
        constructed_models: list[str] = []
        routed_client = MockLLMClient()

        def _factory(**kw):
            constructed_models.append(kw["model"])
            return routed_client

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
        first = service.write(
            prepared_workspace,
            "ch-0001",
            profile_override="p_premium",
        )
        calls_before_rewrite = routed_client._total_calls
        result = service.rewrite(
            prepared_workspace,
            "ch-0001",
            prior_rewrite_count=0,
            profile_override="p_premium",
        )

        assert result.final_state == ChapterState.APPROVED
        assert constructed_models == ["mock-premium"]
        assert first.final_version_id == 1
        assert routed_client._total_calls > calls_before_rewrite


class TestProfileOverrideNoneFallback:
    """契约 2:``profile_override=None`` 走原 routing。"""

    def test_write_without_override_uses_agent_routing(
        self,
        workspace_service: WorkspaceService,
        prepared_workspace: str,
    ) -> None:
        """不传 profile_override 时 agent_routing 生效——writer / reviewer /
        memory_extractor 走 p_default,但 reviewer 走 p_premium 时缓存有 2 key。"""
        config = _build_config(
            default="p_default",
            agent_routing={
                "writer": "p_default",
                "reviewer": "p_premium",
                "memory_extractor": "p_default",
            },
        )
        router = ModelRouter(config)
        service = ChapterService(
            workspace_service=workspace_service,
            client=MockLLMClient(),
            model="legacy-model",
            session_factory=_make_session_factory(workspace_service),
            vector_store_factory=_make_vector_store_factory(workspace_service),
            router_factory=lambda ws_id: router,
        )
        service.save_chapter_card(prepared_workspace, _make_card("ch-0001", 1))
        result = service.write(prepared_workspace, "ch-0001")  # 不传 profile_override

        assert result.final_state == ChapterState.APPROVED
        # 缓存有 2 key:p_default(writer/extractor) + p_premium(reviewer)
        assert len(router._client_cache) == 2


class TestProfileOverrideInvalidRaises:
    """契约 3:非法 profile_override 抛 ProfileNotFoundError。"""

    def test_write_with_unknown_profile_raises(
        self,
        workspace_service: WorkspaceService,
        prepared_workspace: str,
    ) -> None:
        """``service.write(profile_override="p_unknown")`` 在 wrap 入口抛
        ProfileNotFoundError(API 层 errors.py 翻译为 422
        CHAPTER_INVALID_PROFILE,本测试只验抛错路径)。"""
        config = _build_config(default="p_default")
        router = ModelRouter(config)
        service = ChapterService(
            workspace_service=workspace_service,
            client=MockLLMClient(),
            model="legacy-model",
            session_factory=_make_session_factory(workspace_service),
            vector_store_factory=_make_vector_store_factory(workspace_service),
            router_factory=lambda ws_id: router,
        )
        service.save_chapter_card(prepared_workspace, _make_card("ch-0001", 1))

        with pytest.raises(ProfileNotFoundError) as excinfo:
            service.write(
                prepared_workspace,
                "ch-0001",
                profile_override="p_unknown",
            )
        assert "p_unknown" in str(excinfo.value)


class TestProfileOverrideLegacyCompat:
    """``router_factory=None`` 时 legacy 路径忽略 profile_override(无 router 可覆盖)。"""

    def test_no_router_factory_ignores_profile_override(
        self,
        workspace_service: WorkspaceService,
        prepared_workspace: str,
    ) -> None:
        """legacy 路径不构造 router,profile_override 被无声忽略
        (走 self._client + self._model)。这是合理的:legacy 是 P1/P2
        测试兼容,无 profile 抽象,override 无意义。"""
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
        result = service.write(
            prepared_workspace,
            "ch-0001",
            profile_override="p_premium",  # 本应非法,但 legacy 路径不校验
        )

        assert result.final_state == ChapterState.APPROVED
        assert legacy_client._total_calls >= 3
