"""P3-B-07 成本累加测试。

覆盖契约:

1. **compute_cost 公式**:mock profile(cost=0)返 0;claude profile 按
   ``(input/1000 * rate_in) + (output/1000 * rate_out)`` 计。
2. **CostTrackingClient 包装语义**:inner ``complete`` 透传 → recorder 收到
   profile + response + cost;recorder 抛异常时 ``complete`` 不向上冒泡。
3. **chapters 表 round-trip(mock 路径)**:完整 write → ``total_cost_usd``
   恒 0(mock profile cost=0),``total_token_usage`` 按 model_used 维度
   累加各次 LLM 调用 token。
4. **chapters 表 round-trip(claude profile)**:mock client + claude profile
   单价表 → ``total_cost_usd`` ≈ 累加各次 LLM 调用 cost。
5. **revise 路径累加**:write 后再 revise,同一 chapter row 的
   ``total_cost_usd`` / ``total_token_usage`` 在 revise 后继续递增。
6. **legacy 路径无 cost 写入**:router_factory=None 时,走 P1/P2 兼容路径,
   ``ChapterService`` 不构造 ``_CostTrackingRouter``,chapters 表
   ``total_cost_usd`` 保持 0、``total_token_usage`` 保持 ``{}``。
"""

from __future__ import annotations

from collections.abc import Callable
from pathlib import Path
from typing import Any

import pytest
from sqlalchemy import create_engine
from sqlalchemy.orm import Session

from src.core.db import Base
from src.core.db.models import ChapterORM
from src.core.llm.base import LLMClient, LLMResponse, Message, TokenUsage
from src.core.llm.mock_client import MockLLMClient
from src.core.memory.vector import VectorStore
from src.core.models.chapter_card import ChapterCard, ContinuityContext
from src.core.models.review import ReviewIssue
from src.core.models.routing import RoutingConfig, WriterProfilesConfig
from src.core.models.writer_profile import WriterProfile
from src.core.routing.router import ModelRouter
from src.core.state_machine.states import ChapterState
from src.core.workspace.workspace_paths import WorkspacePaths
from src.services.chapter_service import ChapterService
from src.services.cost_tracker import (
    CostTrackingClient,
    _CostTrackingRouter,
    compute_cost,
    make_chapter_recorder,
    wrap_router_with_cost_tracking,
)
from src.services.workspace_service import WorkspaceService

pytestmark = pytest.mark.usefixtures("valid_character_admission")


# ---- 单元测:compute_cost / CostTrackingClient -------------------------------


class TestComputeCost:
    def test_mock_profile_returns_zero(self) -> None:
        profile = WriterProfile(
            profile_id="p_mock",
            provider="mock",
            model="mock-default",
            context_window=16000,
        )
        usage = TokenUsage(input_tokens=1000, output_tokens=500)
        assert compute_cost(profile, usage) == 0.0

    def test_claude_profile_applies_rate_table(self) -> None:
        profile = WriterProfile(
            profile_id="p_claude",
            provider="claude",
            model="claude-sonnet-4-6",
            context_window=200000,
            cost_per_1k_input=0.003,
            cost_per_1k_output=0.015,
        )
        # 1000 input * 0.003/1k + 2000 output * 0.015/1k
        # = 0.003 + 0.030 = 0.033
        usage = TokenUsage(input_tokens=1000, output_tokens=2000)
        assert compute_cost(profile, usage) == pytest.approx(0.033, abs=1e-9)

    def test_zero_tokens_returns_zero(self) -> None:
        profile = WriterProfile(
            profile_id="p_claude",
            provider="claude",
            model="claude-sonnet-4-6",
            context_window=200000,
            cost_per_1k_input=0.003,
            cost_per_1k_output=0.015,
        )
        # TokenUsage 强校验非负;0 token 合法。
        usage = TokenUsage(input_tokens=0, output_tokens=0)
        assert compute_cost(profile, usage) == 0.0


class _StubInnerClient(LLMClient):
    """单元测专用 inner client:返预设 LLMResponse,记录调用次数。"""

    def __init__(self, response: LLMResponse) -> None:
        self.response = response
        self.calls = 0
        self.prepared_schemas: list[tuple[str, str, dict[str, Any]]] = []

    def prepare_strict_json_schema(
        self,
        *,
        model: str,
        schema_name: str,
        schema: dict[str, Any],
        gen_params: dict[str, Any],
    ) -> dict[str, Any]:
        self.prepared_schemas.append((model, schema_name, schema))
        prepared = dict(gen_params)
        prepared["response_format"] = {
            "type": "json_schema",
            "json_schema": {"name": schema_name, "schema": schema},
        }
        return prepared

    def complete(
        self, messages: list[Message], **gen_params: object
    ) -> LLMResponse:
        self.calls += 1
        return self.response

    def prepare_bounded_non_thinking_call(
        self,
        gen_params: dict[str, Any],
        *,
        max_completion_tokens: int,
    ) -> dict[str, Any]:
        del gen_params
        return {"manifest_wire_marker": max_completion_tokens}


class TestCostTrackingClient:
    def _make_response(self) -> LLMResponse:
        return LLMResponse(
            content="hi",
            model_used="claude-sonnet-4-6",
            usage=TokenUsage(input_tokens=2000, output_tokens=1000),
            finish_reason="end_turn",
        )

    def _make_profile(self) -> WriterProfile:
        return WriterProfile(
            profile_id="p_claude",
            provider="claude",
            model="claude-sonnet-4-6",
            context_window=200000,
            cost_per_1k_input=0.003,
            cost_per_1k_output=0.015,
        )

    def test_recorder_receives_profile_response_and_cost(self) -> None:
        recorded: list[tuple[WriterProfile, LLMResponse, float]] = []
        inner = _StubInnerClient(self._make_response())
        wrapped = CostTrackingClient(
            inner,
            self._make_profile(),
            recorder=lambda p, r, c: recorded.append((p, r, c)),
        )

        out = wrapped.complete([Message(role="user", content="ping")])

        # 透传 inner 响应
        assert out.content == "hi"
        assert inner.calls == 1
        # recorder 被调过一次,参数三元组对齐
        assert len(recorded) == 1
        prof, resp, cost = recorded[0]
        assert prof.profile_id == "p_claude"
        assert resp.usage.input_tokens == 2000
        # 2000 * 0.003/1k + 1000 * 0.015/1k = 0.006 + 0.015 = 0.021
        assert cost == pytest.approx(0.021, abs=1e-9)

    def test_recorder_exception_swallowed(self) -> None:
        inner = _StubInnerClient(self._make_response())

        def _bad_recorder(*args: object, **kwargs: object) -> None:
            raise RuntimeError("db down")

        wrapped = CostTrackingClient(
            inner, self._make_profile(), recorder=_bad_recorder
        )

        # 不抛 — recorder 失败不该让 LLM 响应丢失
        out = wrapped.complete([Message(role="user", content="ping")])
        assert out.content == "hi"

    def test_strict_json_schema_capability_delegates_to_inner(self) -> None:
        inner = _StubInnerClient(self._make_response())
        wrapped = CostTrackingClient(
            inner,
            self._make_profile(),
            recorder=lambda *_args: None,
        )
        schema: dict[str, Any] = {
            "type": "object",
            "properties": {"summary": {"type": "string"}},
        }

        prepared = wrapped.prepare_strict_json_schema(
            model="qwen3.8-max",
            schema_name="reviewer",
            schema=schema,
            gen_params={"temperature": 0.2},
        )

        assert inner.prepared_schemas == [("qwen3.8-max", "reviewer", schema)]
        assert prepared["response_format"] == {
            "type": "json_schema",
            "json_schema": {"name": "reviewer", "schema": schema},
        }

    def test_manifest_trim_wire_capability_delegates_to_inner(self) -> None:
        inner = _StubInnerClient(self._make_response())
        wrapped = CostTrackingClient(
            inner,
            self._make_profile(),
            recorder=lambda *_args: None,
        )

        prepare = getattr(wrapped, "prepare_bounded_non_thinking_call")

        assert prepare({}, max_completion_tokens=16_000) == {
            "manifest_wire_marker": 16_000
        }


# ---- 集成测共享 fixtures ----------------------------------------------------


def _mock_embedding_fn(texts: list[str]) -> list[list[float]]:
    return [[0.1, 0.2, 0.3, 0.4] for _ in texts]


def _make_continuity(**overrides: object) -> ContinuityContext:
    payload: dict[str, object] = dict(
        prev_chapter_tail="",
        unfinished_actions=[],
        time_gap="新场景",
        scene_continuation="switch",
        pov_change=False,
        pov_from=None,
        pov_to="林衍",
    )
    payload.update(overrides)
    return ContinuityContext(**payload)  # type: ignore[arg-type]


def _make_card(
    chapter_id: str = "ch-0001",
    chapter_number: int = 1,
    *,
    workspace_id: str = "ws-cost",
) -> ChapterCard:
    return ChapterCard(
        chapter_id=chapter_id,
        workspace_id=workspace_id,
        volume_id="v01",
        chapter_number=chapter_number,
        title=f"第 {chapter_number} 章·cost 集成",
        tension_level=6,
        # P4-B-06:validator 默认放宽,本文件测 cost tracking,validator 不参与。
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


def _build_mock_config() -> WriterProfilesConfig:
    profiles = {
        "p_mock": WriterProfile(
            profile_id="p_mock",
            provider="mock",
            model="mock-default",
            context_window=16000,
        ),
    }
    return WriterProfilesConfig(
        profiles=profiles,
        routing=RoutingConfig(default="p_mock", rules=[], agent_routing={}),
    )


def _build_claude_priced_config() -> WriterProfilesConfig:
    """mock client + claude 单价表:验 cost 公式真累加。

    所有 agent 都路由到同一 claude profile,profile.provider 字面 ``claude``
    但 ``LLM_PROVIDER=mock``(全局)→ factory 仍派 MockLLMClient(对齐
    P3-B-05 router 文档:provider 字面与 settings.llm_provider 解耦)。
    """
    profiles = {
        "p_claude": WriterProfile(
            profile_id="p_claude",
            provider="claude",
            model="claude-sonnet-4-6",
            context_window=200000,
            cost_per_1k_input=0.003,
            cost_per_1k_output=0.015,
        ),
    }
    return WriterProfilesConfig(
        profiles=profiles,
        routing=RoutingConfig(
            default="p_claude", rules=[], agent_routing={}
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
        "ws-cost",
        display_name="Cost 集成测试",
        genre="test",
        premise="验证 P3-B-07 cost 累加",
        total_chapters=3,
    )
    return "ws-cost"


def _read_chapter_row(
    workspace_service: WorkspaceService,
    workspace_id: str,
    chapter_id: str,
) -> ChapterORM | None:
    paths = WorkspacePaths(
        workspace_id, workspaces_root=workspace_service.workspaces_root
    )
    engine = create_engine(f"sqlite:///{paths.db_path}")
    with Session(engine) as session:
        row = session.get(ChapterORM, chapter_id)
        if row is None:
            return None
        # detach 避免 caller 操作触碰 closed session
        session.expunge(row)
        return row


# ---- 集成测:chapters 表 round-trip ------------------------------------------


class TestMockPathCostZero:
    """mock profile 的 cost_per_1k_*=0.0 → cost 累加恒为 0,但 token usage
    仍按 model_used 维度累加。"""

    def test_write_full_pipeline_cost_zero_tokens_recorded(
        self,
        workspace_service: WorkspaceService,
        prepared_workspace: str,
    ) -> None:
        config = _build_mock_config()
        router = ModelRouter(
            config, client_factory=lambda **kw: MockLLMClient()
        )
        service = ChapterService(
            workspace_service=workspace_service,
            client=MockLLMClient(),
            model="legacy-only",
            session_factory=_make_session_factory(workspace_service),
            vector_store_factory=_make_vector_store_factory(workspace_service),
            router_factory=lambda ws_id: router,
        )

        service.save_chapter_card(
            prepared_workspace, _make_card("ch-0001", 1)
        )
        result = service.write(prepared_workspace, "ch-0001")
        assert result.final_state == ChapterState.APPROVED

        row = _read_chapter_row(
            workspace_service, prepared_workspace, "ch-0001"
        )
        assert row is not None
        # mock cost 恒 0
        assert row.total_cost_usd == 0.0
        # token 仍累加;MockLLMClient 在 model_used 前加 ``mock-`` 前缀
        # (见 MockLLMClient.complete:profile.model="mock-default" → response.model_used="mock-mock-default")
        assert "mock-mock-default" in row.total_token_usage
        bucket = row.total_token_usage["mock-mock-default"]
        assert bucket["input_tokens"] > 0
        assert bucket["output_tokens"] > 0


class TestClaudeProfileCostAccumulation:
    """claude profile 单价表 + mock client 实际 LLM 调用 → cost 按公式累加。"""

    def test_write_pipeline_cost_matches_token_arithmetic(
        self,
        workspace_service: WorkspaceService,
        prepared_workspace: str,
    ) -> None:
        config = _build_claude_priced_config()
        # 用 MockLLMClient 跑(provider=claude 字面 + settings.llm_provider=mock 现状)
        router = ModelRouter(
            config, client_factory=lambda **kw: MockLLMClient()
        )
        service = ChapterService(
            workspace_service=workspace_service,
            client=MockLLMClient(),
            model="legacy-only",
            session_factory=_make_session_factory(workspace_service),
            vector_store_factory=_make_vector_store_factory(workspace_service),
            router_factory=lambda ws_id: router,
        )

        service.save_chapter_card(
            prepared_workspace, _make_card("ch-0001", 1)
        )
        service.write(prepared_workspace, "ch-0001")

        row = _read_chapter_row(
            workspace_service, prepared_workspace, "ch-0001"
        )
        assert row is not None
        # cost > 0(claude profile 单价表 + mock client 返非零 token)
        assert row.total_cost_usd > 0.0
        # token bucket 在 ``model_used`` 维度;MockLLMClient 的 model_used
        # 拼了 ``mock-`` 前缀,不一定与 profile.model 字面一致。直接验
        # 至少一个 bucket 存在 + input/output 均正。
        assert len(row.total_token_usage) >= 1
        for bucket in row.total_token_usage.values():
            assert bucket["input_tokens"] > 0
            assert bucket["output_tokens"] > 0
        # 公式自洽校验:重新按累加 token 反算 cost(0.003 / 0.015 单价)
        total_in = sum(
            b["input_tokens"] for b in row.total_token_usage.values()
        )
        total_out = sum(
            b["output_tokens"] for b in row.total_token_usage.values()
        )
        expected_cost = total_in / 1000 * 0.003 + total_out / 1000 * 0.015
        assert row.total_cost_usd == pytest.approx(expected_cost, rel=1e-6)


class TestRevisePathAccumulates:
    """revise 路径同样走 router → cost 在 write 之上继续累加。"""

    def test_write_then_revise_accumulates_into_same_row(
        self,
        workspace_service: WorkspaceService,
        prepared_workspace: str,
    ) -> None:
        config = _build_claude_priced_config()
        router = ModelRouter(
            config, client_factory=lambda **kw: MockLLMClient()
        )
        service = ChapterService(
            workspace_service=workspace_service,
            client=MockLLMClient(),
            model="legacy-only",
            session_factory=_make_session_factory(workspace_service),
            vector_store_factory=_make_vector_store_factory(workspace_service),
            router_factory=lambda ws_id: router,
        )

        service.save_chapter_card(
            prepared_workspace, _make_card("ch-0001", 1)
        )
        service.write(prepared_workspace, "ch-0001")

        row_after_write = _read_chapter_row(
            workspace_service, prepared_workspace, "ch-0001"
        )
        assert row_after_write is not None
        cost_after_write = row_after_write.total_cost_usd
        assert cost_after_write > 0.0

        # revise 一次:reviser agent 的 LLM 调用应继续累加
        issue = ReviewIssue(
            paragraph_anchor="Mock 写作输出",
            issue_type="prose",
            description="待修订",
            suggestion="改语气",
        )
        service.revise(prepared_workspace, "ch-0001", issues=[issue])

        row_after_revise = _read_chapter_row(
            workspace_service, prepared_workspace, "ch-0001"
        )
        assert row_after_revise is not None
        assert row_after_revise.total_cost_usd > cost_after_write


class TestLegacyPathNoCostTracking:
    """router_factory=None 时不构造 cost wrap,chapters 表 cost 不被累加。"""

    def test_legacy_path_keeps_cost_zero_and_token_empty(
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
            # router_factory 故意不传 → legacy 路径
        )

        service.save_chapter_card(
            prepared_workspace, _make_card("ch-0001", 1)
        )
        result = service.write(prepared_workspace, "ch-0001")
        assert result.final_state == ChapterState.APPROVED

        row = _read_chapter_row(
            workspace_service, prepared_workspace, "ch-0001"
        )
        assert row is not None
        # legacy 路径 cost_tracker 不参与 → 字段保持初始值
        assert row.total_cost_usd == 0.0
        assert row.total_token_usage == {}


# ---- 单元测:make_chapter_recorder + wrap_router_with_cost_tracking ----------


class TestRecorderWritesToChapterRow:
    """``make_chapter_recorder`` 直接写库语义(不经过 ChapterService)。"""

    def test_recorder_updates_existing_row(
        self,
        workspace_service: WorkspaceService,
        prepared_workspace: str,
    ) -> None:
        # 先用 ChapterService 建 chapter 行
        service = ChapterService(
            workspace_service=workspace_service,
            client=MockLLMClient(),
            model="m",
            session_factory=_make_session_factory(workspace_service),
        )
        service.save_chapter_card(
            prepared_workspace, _make_card("ch-0001", 1)
        )
        service.write(prepared_workspace, "ch-0001")  # legacy 路径建 row

        session_factory = _make_session_factory(workspace_service)
        recorder = make_chapter_recorder(
            workspace_id=prepared_workspace,
            chapter_id="ch-0001",
            session_factory=lambda: session_factory(prepared_workspace),
        )

        profile = WriterProfile(
            profile_id="p_claude",
            provider="claude",
            model="claude-sonnet-4-6",
            context_window=200000,
            cost_per_1k_input=0.003,
            cost_per_1k_output=0.015,
        )
        response = LLMResponse(
            content="x",
            model_used="claude-sonnet-4-6-20251110",
            usage=TokenUsage(input_tokens=1000, output_tokens=2000),
            finish_reason="end_turn",
        )
        recorder(profile, response, 0.033)

        row = _read_chapter_row(
            workspace_service, prepared_workspace, "ch-0001"
        )
        assert row is not None
        assert row.total_cost_usd == pytest.approx(0.033, abs=1e-9)
        bucket = row.total_token_usage["claude-sonnet-4-6-20251110"]
        assert bucket["input_tokens"] == 1000
        assert bucket["output_tokens"] == 2000

    def test_recorder_silent_on_missing_row(
        self,
        workspace_service: WorkspaceService,
        prepared_workspace: str,
    ) -> None:
        """chapter row 不存在时 recorder 不抛(只落 structlog warn)。"""
        # 工作区有,但章节卡没 write 过 → chapters 表无 row
        session_factory = _make_session_factory(workspace_service)
        recorder = make_chapter_recorder(
            workspace_id=prepared_workspace,
            chapter_id="ch-missing",
            session_factory=lambda: session_factory(prepared_workspace),
        )
        profile = WriterProfile(
            profile_id="p_mock",
            provider="mock",
            model="mock-default",
            context_window=16000,
        )
        response = LLMResponse(
            content="x",
            model_used="mock-default",
            usage=TokenUsage(input_tokens=10, output_tokens=20),
        )
        # 不抛
        recorder(profile, response, 0.0)

    def test_non_usd_profile_never_enters_legacy_usd_projection(
        self,
        workspace_service: WorkspaceService,
        prepared_workspace: str,
    ) -> None:
        service = ChapterService(
            workspace_service=workspace_service,
            client=MockLLMClient(),
            model="m",
            session_factory=_make_session_factory(workspace_service),
        )
        service.save_chapter_card(
            prepared_workspace, _make_card("ch-0001", 1)
        )
        service.write(prepared_workspace, "ch-0001")

        session_factory = _make_session_factory(workspace_service)
        recorder = make_chapter_recorder(
            workspace_id=prepared_workspace,
            chapter_id="ch-0001",
            session_factory=lambda: session_factory(prepared_workspace),
        )
        profile = WriterProfile(
            profile_id="qwen-rmb",
            provider="openai_compatible",
            model="qwen3.8-max",
            base_url="https://example.invalid/v1",
            context_window=1_000_000,
            cost_per_1k_input=0.012,
            cost_per_1k_output=0.036,
            cost_currency="RMB",
        )
        response = LLMResponse(
            content="x",
            model_used="qwen3.8-max",
            usage=TokenUsage(input_tokens=1000, output_tokens=1000),
        )

        recorder(profile, response, 0.048)

        row = _read_chapter_row(
            workspace_service, prepared_workspace, "ch-0001"
        )
        assert row is not None
        assert row.total_cost_usd == 0.0
        assert row.total_token_usage["qwen3.8-max"] == {
            "input_tokens": 1000,
            "output_tokens": 1000,
        }


class TestWrapRouterReturnsCostTrackingRouter:
    """wrap_router_with_cost_tracking 便捷构造器返 ``_CostTrackingRouter``。"""

    def test_wrapped_router_routes_via_cost_tracking_client(
        self,
        workspace_service: WorkspaceService,
        prepared_workspace: str,
    ) -> None:
        config = _build_mock_config()
        router = ModelRouter(
            config, client_factory=lambda **kw: MockLLMClient()
        )
        session_factory = _make_session_factory(workspace_service)

        wrapped = wrap_router_with_cost_tracking(
            router,
            workspace_id=prepared_workspace,
            chapter_id="ch-0001",
            session_factory=lambda: session_factory(prepared_workspace),
        )
        assert isinstance(wrapped, _CostTrackingRouter)

        # route() 返回的 client 已是 CostTrackingClient
        target = wrapped.route("writer")
        assert isinstance(target.client, CostTrackingClient)
        assert target.profile.profile_id == "p_mock"

    def test_wrapped_router_select_profile_delegates_to_inner(
        self,
        workspace_service: WorkspaceService,
        prepared_workspace: str,
    ) -> None:
        """W3.5-s7 端到端验收回归:cost-wrap router 必须透传 ``select_profile``。

        ``ChapterService._calibration_default_trim`` 用
        ``router.select_profile("writer").model`` 查 writer 模型校准决定 trim 三态默认。
        cost-wrap 是 duck-type 包装,原只重写 route + has_agent_routing,**漏 select_profile**
        → 生成期(``_orchestrate`` 把 router cost-wrap 后)调用抛 ``AttributeError`` 被
        ``_calibration_default_trim`` 的 try/except 吞 → trim / anti-tic 校准联动**生产
        生成期静默退默认关**(curl + 单元测都走未 cost-wrap router 漏测;W3.5-s7 端到端
        验收抓到的真 bug)。本测钉死透传,防回归。
        """
        config = _build_mock_config()
        router = ModelRouter(config, client_factory=lambda **kw: MockLLMClient())
        session_factory = _make_session_factory(workspace_service)
        wrapped = wrap_router_with_cost_tracking(
            router,
            workspace_id=prepared_workspace,
            chapter_id="ch-0001",
            session_factory=lambda: session_factory(prepared_workspace),
        )
        # select_profile 透传内层(不抛 AttributeError),与内层返回同 profile
        inner_profile = router.select_profile("writer")
        wrapped_profile = wrapped.select_profile("writer")
        assert wrapped_profile.profile_id == inner_profile.profile_id
        assert wrapped_profile.model == inner_profile.model
