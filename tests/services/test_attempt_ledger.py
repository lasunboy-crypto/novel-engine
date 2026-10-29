"""W3.5-R5 持久化 attempt 账本与 provider hard gate 回归。"""

from __future__ import annotations

from datetime import UTC, datetime
from decimal import Decimal
from pathlib import Path
from typing import Any

import pytest
from sqlalchemy import create_engine, select
from sqlalchemy.orm import Session

from src.core.db import Base
from src.core.db.models import (
    AttemptLedgerORM,
    ChapterORM,
    ChapterVersionORM,
    PipelineRunORM,
    WorkspaceORM,
)
from src.core.llm.attempt_context import (
    ProviderAttemptOutcome,
    bind_provider_attempt_observer,
    run_observed_provider_attempt,
)
from src.core.llm.base import LLMClient, LLMResponse, Message, TokenUsage
from src.core.models.writer_profile import WriterProfile
from src.core.routing.router import RoutedTarget
from src.core.routing.run_model_manifest_router import (
    RunModelManifestBinding,
    SelectionTestLossBreakerBinding,
)
from src.services.attempt_ledger import (
    AttemptLedgerError,
    AttemptLedgerStore,
    AttemptScope,
    ProviderCallCapExceeded,
    ProviderCostCapExceeded,
    bind_attempt_scope,
    wrap_router_with_attempt_tracking,
)


class _ObservedPhysicalClient(LLMClient):
    """模拟三个真实 client 的物理请求观察点。"""

    def __init__(self) -> None:
        self.network_calls = 0
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
        self,
        messages: list[Message],
        **gen_params: Any,
    ) -> LLMResponse:
        del messages, gen_params

        def _network_call() -> LLMResponse:
            self.network_calls += 1
            return LLMResponse(
                content="ok",
                model_used="qwen-r5-test",
                usage=TokenUsage(input_tokens=1, output_tokens=1),
                finish_reason="stop",
            )

        return run_observed_provider_attempt(
            _network_call,
            technical_attempt=1,
            outcome_from_result=lambda _response: ProviderAttemptOutcome.known(
                input_tokens=1,
                output_tokens=1,
                provider_request_id="req-offline-sentinel",
            ),
        )

    def prepare_bounded_non_thinking_call(
        self,
        gen_params: dict[str, Any],
        *,
        max_completion_tokens: int,
    ) -> dict[str, Any]:
        return {
            **gen_params,
            "manifest_wire_marker": max_completion_tokens,
        }


class _RouterStub:
    def __init__(
        self,
        *,
        profile: WriterProfile,
        client: LLMClient,
        manifest_binding: RunModelManifestBinding | None = None,
    ) -> None:
        self._profile = profile
        self._client = client
        if manifest_binding is not None:
            self.run_model_manifest_binding = manifest_binding

    def route(
        self,
        agent_name: str,
        *,
        chapter_card: Any = None,
        override_profile_id: str | None = None,
    ) -> RoutedTarget:
        del agent_name, chapter_card, override_profile_id
        return RoutedTarget(self._profile, self._client, {})


def _session_factory(tmp_path: Path):
    engine = create_engine(f"sqlite:///{tmp_path / 'attempt-ledger.db'}")
    Base.metadata.create_all(engine)

    def _factory(workspace_id: str) -> Session:
        del workspace_id
        return Session(engine)

    return engine, _factory


def _seed_workspace_run_and_chapter(
    factory,
    *,
    run_ids: tuple[str, ...] = ("run-1", "run-2"),
) -> None:
    now = datetime.now(UTC)
    with factory("ws-r5") as session:
        session.add(
            WorkspaceORM(
                workspace_id="ws-r5",
                display_name="R5",
                genre="test",
                premise="attempt ledger",
                created_at=now,
                last_active_at=now,
                engine_version="0.1.0",
                status="active",
                total_chapters=1,
                completed_chapters=0,
                config_overrides={},
            )
        )
        session.add(
            ChapterORM(
                chapter_id="ch-0001",
                workspace_id="ws-r5",
                current_version=0,
                status="drafting",
                rewrite_count=0,
                revise_count=0,
                created_at=now,
                approved_at=None,
                total_token_usage={},
                total_cost_usd=0.0,
            )
        )
        for run_id in run_ids:
            session.add(
                PipelineRunORM(
                    run_id=run_id,
                    workspace_id="ws-r5",
                    state="running",
                    scope="single",
                    start_chapter_id="ch-0001",
                    end_chapter_id="ch-0001",
                    volume_id=None,
                    profile_override=None,
                    chapter_ids=["ch-0001"],
                    current_chapter="ch-0001",
                    completed_chapter_ids=[],
                    pause_reason=None,
                    error_details=None,
                    failure_policy="retry",
                    skipped_chapter_ids=[],
                    provider_call_cap=2,
                    provider_call_count=0,
                    transport_retry_count=0,
                    pipeline_attempt_count=0,
                    upstream_generation_attempt_count=0,
                    created_at=now,
                    updated_at=now,
                    completed_at=None,
                )
            )
        session.commit()


def test_provider_cap_persists_across_store_instances_and_stops_before_network(
    tmp_path: Path,
) -> None:
    engine, factory = _session_factory(tmp_path)
    _seed_workspace_run_and_chapter(factory)
    scope = AttemptScope(
        workspace_id="ws-r5",
        run_id="run-1",
        task_id="task-outline",
        chapter_id="ch-0001",
        provider_call_cap=2,
    )
    store = AttemptLedgerStore(factory)
    observer = store.make_provider_observer(
        scope=scope,
        logical_call_id="logical-outline-1",
        operation_kind="upstream_generation",
        agent_name="outline_builder",
        provider="openai_compatible",
        model="qwen-r5-test",
    )
    network_calls = 0

    def _network_call() -> str:
        nonlocal network_calls
        network_calls += 1
        return "ok"

    first_id = observer.begin(technical_attempt=1)
    assert _network_call() == "ok"
    observer.finish(first_id, succeeded=True, error_type=None)

    second_id = observer.begin(technical_attempt=2)
    assert _network_call() == "ok"
    observer.finish(
        second_id,
        succeeded=False,
        error_type="ReadTimeout",
    )

    restarted_store = AttemptLedgerStore(factory)
    restarted_observer = restarted_store.make_provider_observer(
        scope=scope,
        logical_call_id="logical-outline-1",
        operation_kind="upstream_generation",
        agent_name="outline_builder",
        provider="openai_compatible",
        model="qwen-r5-test",
    )
    with pytest.raises(ProviderCallCapExceeded) as caught:
        restarted_observer.begin(technical_attempt=3)
    assert caught.value.used == 2
    assert network_calls == 2

    summary = restarted_store.summarize("ws-r5", run_id="run-1")
    assert summary.provider_calls == 2
    assert summary.transport_retries == 1
    assert summary.upstream_generation_attempts == 1

    with Session(engine) as session:
        rows = list(
            session.scalars(
                select(AttemptLedgerORM).order_by(AttemptLedgerORM.attempt_number)
            )
        )
        run = session.get(PipelineRunORM, "run-1")
    assert [row.status for row in rows] == ["succeeded", "failed"]
    assert [row.attempt_number for row in rows] == [1, 2]
    assert run is not None
    assert run.provider_call_count == 2
    assert run.transport_retry_count == 1
    assert run.upstream_generation_attempt_count == 1

    # 新 run 代表另一项单独获批活动：历史仍保留，但不能沿用上一 run 的
    # cap 消耗；同 run 的 restart 反证已由上半段覆盖。
    next_scope = AttemptScope(
        workspace_id="ws-r5",
        run_id="run-2",
        task_id="task-new-approved-activity",
        chapter_id="ch-0001",
        provider_call_cap=1,
    )
    next_observer = restarted_store.make_provider_observer(
        scope=next_scope,
        logical_call_id="logical-next-run-1",
        operation_kind="agent_call",
        agent_name="writer",
        provider="openai_compatible",
        model="qwen-r5-test",
    )
    next_id = next_observer.begin(technical_attempt=1)
    assert _network_call() == "ok"
    next_observer.finish(next_id, succeeded=True, error_type=None)
    with pytest.raises(ProviderCallCapExceeded):
        next_observer.begin(technical_attempt=1)

    assert restarted_store.summarize("ws-r5", run_id="run-2").provider_calls == 1
    assert restarted_store.summarize("ws-r5").provider_calls == 3
    with Session(engine) as session:
        next_run = session.get(PipelineRunORM, "run-2")
    assert next_run is not None
    assert next_run.provider_call_count == 1


def test_terminal_persistence_failure_never_returns_paid_response(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    engine, factory = _session_factory(tmp_path)
    _seed_workspace_run_and_chapter(factory, run_ids=("run-1",))
    store = AttemptLedgerStore(factory)
    observer = store.make_provider_observer(
        scope=AttemptScope(
            workspace_id="ws-r5",
            run_id="run-1",
            task_id="task-terminal-persist",
            chapter_id="ch-0001",
            provider_call_cap=10,
        ),
        logical_call_id="call-terminal-persist",
        operation_kind="agent_call",
        agent_name="writer",
        provider="openai_compatible",
        model="qwen-r5-test",
    )

    def _fail_terminal_write(**_kwargs: Any) -> None:
        raise AttemptLedgerError("terminal write failed")

    monkeypatch.setattr(store, "finish_provider_attempt", _fail_terminal_write)
    with bind_provider_attempt_observer(observer):
        with pytest.raises(AttemptLedgerError, match="terminal write failed"):
            run_observed_provider_attempt(
                lambda: "paid-response",
                technical_attempt=1,
            )

    with Session(engine) as session:
        row = session.scalar(
            select(AttemptLedgerORM).where(
                AttemptLedgerORM.logical_call_id == "call-terminal-persist"
            )
        )
    assert row is not None
    assert row.status == "started"
    assert row.response_status == "request_started"


def test_provider_cap_can_be_observational_without_blocking_attempt_insert(
    tmp_path: Path,
) -> None:
    engine, factory = _session_factory(tmp_path)
    _seed_workspace_run_and_chapter(factory)
    scope = AttemptScope(
        workspace_id="ws-r5",
        run_id=None,
        task_id="task-maintainer-controlled",
        chapter_id="ch-0001",
        provider_call_cap=1,
        provider_call_cap_enforced=False,
    )
    store = AttemptLedgerStore(factory)

    first = store.begin_provider_attempt(
        scope=scope,
        logical_call_id="logical-1",
        operation_kind="agent_call",
        agent_name="reviewer",
        provider="openai_compatible",
        model="qwen-r5-test",
        technical_attempt=1,
    )
    second = store.begin_provider_attempt(
        scope=scope,
        logical_call_id="logical-2",
        operation_kind="agent_call",
        agent_name="reviewer",
        provider="openai_compatible",
        model="qwen-r5-test",
        technical_attempt=1,
    )

    assert first != second
    assert store.summarize("ws-r5").provider_calls == 2
    with Session(engine) as session:
        rows = list(session.scalars(select(AttemptLedgerORM)))
    assert len(rows) == 2
    assert {row.attempt_cap for row in rows} == {1}


def test_pipeline_attempt_history_survives_new_run_without_blocking_recovery(
    tmp_path: Path,
) -> None:
    engine, factory = _session_factory(tmp_path)
    _seed_workspace_run_and_chapter(factory)
    first_store = AttemptLedgerStore(factory)

    first = first_store.begin_pipeline_attempt(
        workspace_id="ws-r5",
        run_id="run-1",
        chapter_id="ch-0001",
    )
    assert first.attempt_number == 1
    assert first.version_before == 0

    with factory("ws-r5") as session:
        session.add(
            ChapterVersionORM(
                chapter_id="ch-0001",
                version_id=1,
                content="正文已持久化",
                word_count=7,
                created_at=datetime.now(UTC),
                creation_type="draft",
                model_used="mock",
                review_score=None,
                notes="",
                style_fingerprint={},
            )
        )
        session.commit()
    first_store.finish_pipeline_attempt(
        workspace_id="ws-r5",
        attempt_id=first.attempt_id,
        succeeded=False,
        error_type="ReadTimeout",
    )

    restarted_store = AttemptLedgerStore(factory)
    second = restarted_store.begin_pipeline_attempt(
        workspace_id="ws-r5",
        run_id="run-2",
        chapter_id="ch-0001",
    )
    assert second.attempt_number == 2
    assert second.version_before == 1
    restarted_store.finish_pipeline_attempt(
        workspace_id="ws-r5",
        attempt_id=second.attempt_id,
        succeeded=False,
        error_type="RuntimeError",
    )

    # 历史两次失败仍完整保留，但修复后的同一 run 可以追加第 3 次恢复尝试。
    third = restarted_store.begin_pipeline_attempt(
        workspace_id="ws-r5",
        run_id="run-2",
        chapter_id="ch-0001",
    )
    assert third.attempt_number == 3
    assert third.version_before == 1
    restarted_store.finish_pipeline_attempt(
        workspace_id="ws-r5",
        attempt_id=third.attempt_id,
        succeeded=True,
    )

    with Session(engine) as session:
        rows = list(
            session.scalars(
                select(AttemptLedgerORM)
                .where(AttemptLedgerORM.attempt_kind == "pipeline")
                .order_by(AttemptLedgerORM.attempt_number)
            )
        )
        run = session.get(PipelineRunORM, "run-2")
    assert [row.attempt_number for row in rows] == [1, 2, 3]
    assert [row.attempt_cap for row in rows] == [1, 2, 3]
    assert [(row.version_before, row.version_after) for row in rows] == [
        (0, 1),
        (1, 1),
        (1, 1),
    ]
    assert [row.status for row in rows] == ["failed", "failed", "succeeded"]
    assert run is not None
    assert run.pipeline_attempt_count == 2
    assert restarted_store.summarize("ws-r5").pipeline_attempts == 3


def test_reconcile_orphaned_attempts_closes_pipeline_only(tmp_path: Path) -> None:
    """进程恢复只收口 pipeline started；未知 provider 事实必须原样保留。"""
    engine, factory = _session_factory(tmp_path)
    _seed_workspace_run_and_chapter(factory)
    store = AttemptLedgerStore(factory)
    pipeline = store.begin_pipeline_attempt(
        workspace_id="ws-r5",
        run_id="run-1",
        chapter_id="ch-0001",
    )
    other_run_pipeline = store.begin_pipeline_attempt(
        workspace_id="ws-r5",
        run_id="run-2",
        chapter_id="ch-0001",
    )
    provider_id = store.begin_provider_attempt(
        scope=AttemptScope(
            workspace_id="ws-r5",
            run_id="run-1",
            task_id="task-reviewer",
            chapter_id="ch-0001",
            provider_call_cap=10,
        ),
        logical_call_id="logical-reviewer",
        operation_kind="agent_call",
        agent_name="reviewer",
        provider="openai_compatible",
        model="qwen-r5-test",
        technical_attempt=1,
    )

    # 模拟 Worker 中断后先从章节 UI 产生恢复稿，再点击 Run Continue。
    # 迟到的 orphan 收口不能把这份 UI 稿冒记成旧 Worker attempt 的结果。
    with factory("ws-r5") as session:
        session.add(
            ChapterVersionORM(
                chapter_id="ch-0001",
                version_id=1,
                content="UI recovery version",
                word_count=3,
                created_at=datetime.now(UTC),
                creation_type="rewrite",
                model_used="qwen-r5-test",
                review_score=None,
                notes="",
                style_fingerprint=None,
            )
        )
        session.commit()

    with factory("ws-r5") as session:
        reconciled = store.reconcile_orphaned_pipeline_attempts(
            session=session,
            workspace_id="ws-r5",
            run_id="run-1",
        )
        session.commit()

    assert reconciled == [pipeline.attempt_id]
    with Session(engine) as session:
        pipeline_row = session.get(AttemptLedgerORM, pipeline.attempt_id)
        provider_row = session.get(AttemptLedgerORM, provider_id)
        other_row = session.get(
            AttemptLedgerORM,
            other_run_pipeline.attempt_id,
        )
    assert pipeline_row is not None
    assert pipeline_row.status == "failed"
    assert pipeline_row.error_type == "PipelineInterrupted"
    assert pipeline_row.completed_at is not None
    assert pipeline_row.version_before == 0
    assert pipeline_row.version_after is None
    assert provider_row is not None
    assert provider_row.status == "started"
    assert provider_row.completed_at is None
    assert other_row is not None
    assert other_row.status == "started"


def test_router_tracks_every_agent_and_shared_cap(tmp_path: Path) -> None:
    engine, factory = _session_factory(tmp_path)
    _seed_workspace_run_and_chapter(factory, run_ids=("run-1",))
    physical_client = _ObservedPhysicalClient()
    profile = WriterProfile(
        profile_id="r5-test",
        provider="openai_compatible",
        model="qwen-r5-test",
        base_url="https://example.invalid/v1",
        context_window=8192,
        generation_params={"max_tokens": 20},
        extra_request_kwargs={"input_token_budget": 100},
        cost_per_1k_input=0.1,
        cost_per_1k_output=0.2,
        cost_currency="RMB",
    )
    router = wrap_router_with_attempt_tracking(
        _RouterStub(  # type: ignore[arg-type]
            profile=profile,
            client=physical_client,
        ),
        workspace_id="ws-r5",
        session_factory=factory,
    )

    with bind_attempt_scope(
        workspace_id="ws-r5",
        run_id="run-1",
        task_id="task-all-agents",
        chapter_id="ch-0001",
        provider_call_cap=3,
    ):
        for agent_name in ("writer", "reviewer", "outline_builder"):
            target = router.route(agent_name)
            assert target.client.complete([]).content == "ok"
        with pytest.raises(ProviderCallCapExceeded):
            router.route("reviser").client.complete([])

    assert physical_client.network_calls == 3
    with Session(engine) as session:
        rows = list(
            session.scalars(
                select(AttemptLedgerORM)
                .where(AttemptLedgerORM.attempt_kind == "provider")
                .order_by(AttemptLedgerORM.created_at)
            )
        )
    assert [row.agent_name for row in rows] == [
        "writer",
        "reviewer",
        "outline_builder",
    ]
    assert [row.operation_kind for row in rows] == [
        "agent_call",
        "agent_call",
        "upstream_generation",
    ]
    assert all(row.usage_status == "known" for row in rows)
    assert all(row.input_tokens == 1 for row in rows)
    assert all(row.output_tokens == 1 for row in rows)
    # Ordinary production keeps the pre-existing profile input-budget basis.
    assert all(row.reserved_input_tokens == 100 for row in rows)
    assert all(row.reserved_output_tokens == 20 for row in rows)
    assert all(row.configured_cost_currency == "RMB" for row in rows)
    assert all(row.configured_input_cost_per_1k == 0.1 for row in rows)
    assert all(row.configured_output_cost_per_1k == 0.2 for row in rows)
    assert all(row.configured_cost_amount == pytest.approx(0.0003) for row in rows)
    assert all(row.configured_cost_basis == "metered_usage" for row in rows)
    assert all(row.provider_request_id == "req-offline-sentinel" for row in rows)
    assert all(row.response_status == "completed" for row in rows)

    summary = AttemptLedgerStore(factory).summarize_usage_cost(
        "ws-r5", run_id="run-1"
    )
    assert summary.usage_known_attempts == 3
    assert summary.usage_unknown_attempts == 0
    assert summary.legacy_unclassified_attempts == 0
    assert summary.input_tokens_known == 3
    assert summary.output_tokens_known == 3
    assert summary.provider_request_id_count == 3
    assert summary.response_status_counts == {"completed": 3}
    assert summary.configured_cost_basis_counts == {"metered_usage": 3}
    assert summary.configured_estimates == {"RMB": pytest.approx(0.0009)}


def test_configured_cost_cap_reserves_actual_render_before_network(
    tmp_path: Path,
) -> None:
    engine, factory = _session_factory(tmp_path)
    _seed_workspace_run_and_chapter(factory, run_ids=("run-1",))
    physical_client = _ObservedPhysicalClient()
    profile = WriterProfile(
        profile_id="cost-cap-test",
        provider="openai_compatible",
        model="qwen-r5-test",
        base_url="https://example.invalid/v1",
        context_window=8192,
        generation_params={"max_tokens": 20},
        extra_request_kwargs={"input_token_budget": 100},
        cost_per_1k_input=1.0,
        cost_per_1k_output=1.0,
        cost_currency="CNY",
    )
    manifest_token = "run-model-manifest:cost-cap-test"
    selection_token = f"selection-test:{manifest_token}:v1"
    binding = RunModelManifestBinding(
        manifest_id="deepseek-v4-flash",
        token=manifest_token,
        selection_test=SelectionTestLossBreakerBinding(
            activity_scope="selection_test",
            token=selection_token,
            max_physical_requests=10,
            max_configured_rmb=Decimal("0.085"),
        ),
    )
    router = wrap_router_with_attempt_tracking(
        _RouterStub(  # type: ignore[arg-type]
            profile=profile,
            client=physical_client,
            manifest_binding=binding,
        ),
        workspace_id="ws-r5",
        session_factory=factory,
    )

    with bind_attempt_scope(
        workspace_id="ws-r5",
        run_id="run-1",
        task_id="task-cost-cap",
        chapter_id="ch-0001",
        provider_call_cap=10,
        activity_scope="selection_test",
        run_model_manifest_token=manifest_token,
        selection_test_manifest_token=selection_token,
        configured_cost_currency="CNY",
        configured_cost_cap=Decimal("0.085"),
    ):
        target = router.route("writer")
        assert target.client.complete([]).content == "ok"
        with pytest.raises(ProviderCostCapExceeded):
            router.route("reviewer").client.complete([])

    assert physical_client.network_calls == 1
    with Session(engine) as session:
        rows = list(
            session.scalars(
                select(AttemptLedgerORM).where(
                    AttemptLedgerORM.attempt_kind == "provider"
                )
            )
        )
    assert len(rows) == 1
    assert rows[0].reserved_input_tokens == 64
    assert rows[0].reserved_output_tokens == 20
    assert rows[0].configured_cost_basis == "metered_usage"


def test_selection_scope_caps_aggregate_across_task_and_run_ids(
    tmp_path: Path,
) -> None:
    """选型产品的 upstream task / pipeline run 不能各自重置余额。"""

    _engine, factory = _session_factory(tmp_path)
    _seed_workspace_run_and_chapter(factory, run_ids=("run-1", "run-2"))
    physical_client = _ObservedPhysicalClient()
    profile = WriterProfile(
        profile_id="selection-aggregate",
        provider="openai_compatible",
        model="qwen-r5-test",
        base_url="https://example.invalid/v1",
        context_window=8192,
        generation_params={"max_tokens": 20},
        extra_request_kwargs={"input_token_budget": 100},
        cost_per_1k_input=1.0,
        cost_per_1k_output=1.0,
        cost_currency="CNY",
    )
    manifest_token = "run-model-manifest:selection-aggregate"
    selection_token = f"selection-test:{manifest_token}:v1"
    binding = RunModelManifestBinding(
        manifest_id="hy3",
        token=manifest_token,
        selection_test=SelectionTestLossBreakerBinding(
            activity_scope="selection_test",
            token=selection_token,
            max_physical_requests=2,
            max_configured_rmb=Decimal("1"),
        ),
    )
    router = wrap_router_with_attempt_tracking(
        _RouterStub(  # type: ignore[arg-type]
            profile=profile,
            client=physical_client,
            manifest_binding=binding,
        ),
        workspace_id="ws-r5",
        session_factory=factory,
    )

    def run_one(*, run_id: str, task_id: str, agent: str) -> None:
        with bind_attempt_scope(
            workspace_id="ws-r5",
            run_id=run_id,
            task_id=task_id,
            provider_call_cap=2,
            activity_scope="selection_test",
            run_model_manifest_token=manifest_token,
            selection_test_manifest_token=selection_token,
            configured_cost_currency="CNY",
            configured_cost_cap=Decimal("1"),
        ):
            router.route(agent).client.complete([])

    run_one(run_id="run-1", task_id="outline-task", agent="outline_builder")
    run_one(run_id="run-2", task_id="pipeline-task", agent="writer")
    with pytest.raises(ProviderCallCapExceeded) as caught:
        run_one(run_id="run-2", task_id="manual-recovery", agent="reviewer")

    assert caught.value.used == 2
    assert physical_client.network_calls == 2
    assert AttemptLedgerStore(factory).summarize("ws-r5").provider_calls == 2


def test_production_manifest_keeps_existing_provider_cap_and_accounting(
    tmp_path: Path,
) -> None:
    engine, factory = _session_factory(tmp_path)
    _seed_workspace_run_and_chapter(factory, run_ids=("run-1",))
    physical_client = _ObservedPhysicalClient()
    profile = WriterProfile(
        profile_id="production-manifest-test",
        provider="openai_compatible",
        model="qwen-r5-test",
        base_url="https://example.invalid/v1",
        context_window=8192,
        generation_params={"max_tokens": 20},
        extra_request_kwargs={"input_token_budget": 100},
        cost_per_1k_input=1.0,
        cost_per_1k_output=1.0,
        cost_currency="CNY",
    )
    manifest_token = "run-model-manifest:production-test"
    binding = RunModelManifestBinding(
        manifest_id="deepseek-v4-flash",
        token=manifest_token,
        selection_test=SelectionTestLossBreakerBinding(
            activity_scope="selection_test",
            token=f"selection-test:{manifest_token}:v1",
            max_physical_requests=1,
            max_configured_rmb=Decimal("0.001"),
        ),
    )
    router = wrap_router_with_attempt_tracking(
        _RouterStub(  # type: ignore[arg-type]
            profile=profile,
            client=physical_client,
            manifest_binding=binding,
        ),
        workspace_id="ws-r5",
        session_factory=factory,
    )

    with bind_attempt_scope(
        workspace_id="ws-r5",
        run_id="run-1",
        task_id="task-production-manifest",
        chapter_id="ch-0001",
        provider_call_cap=2,
        run_model_manifest_token=manifest_token,
    ):
        assert router.route("writer").client.complete([]).content == "ok"
        assert router.route("reviewer").client.complete([]).content == "ok"
        with pytest.raises(ProviderCallCapExceeded):
            router.route("reviser").client.complete([])

    assert physical_client.network_calls == 2
    with Session(engine) as session:
        rows = list(
            session.scalars(
                select(AttemptLedgerORM).where(
                    AttemptLedgerORM.attempt_kind == "provider"
                )
            )
        )
    assert len(rows) == 2
    assert all(row.reserved_input_tokens == 100 for row in rows)


def test_attempt_tracking_preserves_manifest_trim_wire_capability(
    tmp_path: Path,
) -> None:
    _engine, factory = _session_factory(tmp_path)
    profile = WriterProfile(
        profile_id="manifest-wire-capability",
        provider="openai_compatible",
        model="qwen-r5-test",
        base_url="https://example.invalid/v1",
        context_window=8192,
    )
    router = wrap_router_with_attempt_tracking(
        _RouterStub(profile=profile, client=_ObservedPhysicalClient()),  # type: ignore[arg-type]
        workspace_id="ws-r5",
        session_factory=factory,
    )

    target = router.route("writer")
    prepare = getattr(target.client, "prepare_bounded_non_thinking_call")

    assert prepare({}, max_completion_tokens=16000) == {
        "manifest_wire_marker": 16000
    }


def test_production_scope_cannot_enable_selection_test_rmb_breaker() -> None:
    with pytest.raises(ValueError, match="production scope"):
        with bind_attempt_scope(
            workspace_id="ws-r5",
            configured_cost_currency="CNY",
            configured_cost_cap=Decimal("1"),
        ):
            pass


def test_selection_test_scope_requires_manifest_router(tmp_path: Path) -> None:
    _engine, factory = _session_factory(tmp_path)
    physical_client = _ObservedPhysicalClient()
    profile = WriterProfile(
        profile_id="selection-without-manifest",
        provider="openai_compatible",
        model="qwen-r5-test",
        base_url="https://example.invalid/v1",
        context_window=8192,
    )
    router = wrap_router_with_attempt_tracking(
        _RouterStub(  # type: ignore[arg-type]
            profile=profile,
            client=physical_client,
        ),
        workspace_id="ws-r5",
        session_factory=factory,
    )

    with bind_attempt_scope(
        workspace_id="ws-r5",
        activity_scope="selection_test",
        selection_test_manifest_token="selection-test:missing-manifest",
    ):
        with pytest.raises(AttemptLedgerError, match="manifest router"):
            router.route("writer")
    assert physical_client.network_calls == 0


def test_attempt_tracking_preserves_strict_json_schema_capability(
    tmp_path: Path,
) -> None:
    _engine, factory = _session_factory(tmp_path)
    _seed_workspace_run_and_chapter(factory, run_ids=("run-1",))
    physical_client = _ObservedPhysicalClient()
    profile = WriterProfile(
        profile_id="r5-schema-test",
        provider="openai_compatible",
        model="qwen3.8-max",
        base_url="https://dashscope.aliyuncs.com/compatible-mode/v1",
        context_window=1_000_000,
    )
    router = wrap_router_with_attempt_tracking(
        _RouterStub(  # type: ignore[arg-type]
            profile=profile,
            client=physical_client,
        ),
        workspace_id="ws-r5",
        session_factory=factory,
    )
    schema: dict[str, Any] = {
        "type": "object",
        "properties": {"summary": {"type": "string"}},
    }

    prepared = router.route("chapter_card_generator").client.prepare_strict_json_schema(
        model=profile.model,
        schema_name="chapter_card",
        schema=schema,
        gen_params={"temperature": 0.2},
    )

    assert physical_client.prepared_schemas == [("qwen3.8-max", "chapter_card", schema)]
    assert prepared["response_format"] == {
        "type": "json_schema",
        "json_schema": {"name": "chapter_card", "schema": schema},
    }
    assert physical_client.network_calls == 0
