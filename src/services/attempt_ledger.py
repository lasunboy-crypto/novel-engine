"""W3.5-R5 跨进程 attempt 权威账本与 provider hard gate。

``attempt_ledger`` 保存两类不可刷新事实：

- ``pipeline``：一次章节处理尝试，按工作区+章节跨 run 累计；
- ``provider``：一次真实 provider 的物理请求，client 内部 technical retry
  逐次落行。

production provider hard cap 按活动累计：pipeline 用 ``run_id``，无 run
的调用用稳定 ``task_id``。只有显式 ``selection_test`` scope 按整个专用
workspace 累计，使产品 upstream task 交接到 pipeline run 或 UI 恢复都
不能重置剩余活动余额；校准账本的消耗由专用活动预算桥先行扣除。
pause/resume 与进程重启不会重置同一活动。全部历史仍永久保留，供跨活动
审计和 metrics 派生。
"""

from __future__ import annotations

from collections.abc import Callable, Iterator
from contextlib import contextmanager
from contextvars import ContextVar
from dataclasses import dataclass, replace
from decimal import Decimal
from datetime import UTC, datetime
from typing import Any, Literal, Protocol
from uuid import uuid4

from sqlalchemy import func, select, text
from sqlalchemy.orm import Session

from src.core.db.models import (
    AttemptLedgerORM,
    ChapterVersionORM,
    PipelineRunORM,
)
from src.core.llm.attempt_context import (
    ProviderAttemptOutcome,
    ProviderAttemptObserver,
    bind_provider_attempt_observer,
)
from src.core.llm.base import LLMClient, LLMResponse, Message
from src.core.models.writer_profile import WriterProfile
from src.core.routing.router import RoutedTarget
from src.services.run_model_stage_registry import (
    conservative_rendered_input_tokens,
)


DEFAULT_PROVIDER_CALL_HARD_CAP = 1000

_UPSTREAM_AGENT_NAMES = frozenset(
    {
        "outline_builder",
        "character_backfill",
        "chapter_card_generator",
    }
)

class AttemptLedgerError(RuntimeError):
    """attempt 账本无法安全读写。"""


class ProviderCallCapExceeded(AttemptLedgerError):
    """真实 provider 调用将在网络前超过 hard cap。"""

    code = "PROVIDER_CALL_CAP_EXCEEDED"

    def __init__(self, *, workspace_id: str, used: int, cap: int) -> None:
        self.workspace_id = workspace_id
        self.used = used
        self.cap = cap
        super().__init__(
            f"工作区 {workspace_id} provider 调用已达 hard cap: {used}/{cap}"
        )


class ProviderCostCapExceeded(AttemptLedgerError):
    """Selection test 的下一次最坏费用预留将超过 RMB loss breaker。"""

    code = "PROVIDER_COST_CAP_EXCEEDED"

    def __init__(
        self,
        *,
        workspace_id: str,
        used: Decimal,
        next_reserve: Decimal,
        cap: Decimal,
        currency: str,
    ) -> None:
        self.workspace_id = workspace_id
        self.used = used
        self.next_reserve = next_reserve
        self.cap = cap
        self.currency = currency
        AttemptLedgerError.__init__(
            self,
            f"工作区 {workspace_id} selection-test 配置费用将超过 loss breaker: "
            f"{used}+{next_reserve}>{cap} {currency}"
        )


AttemptActivityScope = Literal["production", "selection_test"]


@dataclass(frozen=True)
class AttemptScope:
    """一次产品任务内 provider attempt 的稳定维度。"""

    workspace_id: str
    run_id: str | None
    task_id: str
    chapter_id: str | None
    provider_call_cap: int
    provider_call_cap_enforced: bool = True
    activity_scope: AttemptActivityScope = "production"
    run_model_manifest_token: str | None = None
    selection_test_manifest_token: str | None = None
    configured_cost_currency: str | None = None
    configured_cost_cap: Decimal | None = None
    # 仅正式 Pipeline worker 置 True；暂停 run 上的手动 Chapter UI 修复虽然
    # 也会携带 run_id，仍不得据此被误判成同步富记忆。
    synchronous_memory_enrichment: bool = False


_SCOPE: ContextVar[AttemptScope | None] = ContextVar(
    "llm_attempt_scope",
    default=None,
)


@contextmanager
def bind_attempt_scope(
    *,
    workspace_id: str,
    run_id: str | None = None,
    task_id: str | None = None,
    chapter_id: str | None = None,
    provider_call_cap: int = DEFAULT_PROVIDER_CALL_HARD_CAP,
    provider_call_cap_enforced: bool = True,
    activity_scope: AttemptActivityScope = "production",
    run_model_manifest_token: str | None = None,
    selection_test_manifest_token: str | None = None,
    configured_cost_currency: str | None = None,
    configured_cost_cap: Decimal | None = None,
    synchronous_memory_enrichment: bool = False,
) -> Iterator[AttemptScope]:
    """绑定一个可跨所有 router agent 共用的 provider 预算范围。"""

    if provider_call_cap < 1:
        raise ValueError("provider_call_cap 必须 >= 1")
    if (configured_cost_currency is None) != (configured_cost_cap is None):
        raise ValueError("configured cost currency/cap 必须同时提供或同时省略")
    if configured_cost_cap is not None and configured_cost_cap <= 0:
        raise ValueError("configured_cost_cap 必须 > 0")
    if activity_scope == "production" and (
        selection_test_manifest_token is not None
        or configured_cost_currency is not None
        or configured_cost_cap is not None
    ):
        raise ValueError("production scope 不得启用 selection-test loss breaker")
    if activity_scope == "selection_test" and not selection_test_manifest_token:
        raise ValueError("selection_test scope 必须绑定专用 manifest token")
    scope = AttemptScope(
        workspace_id=workspace_id,
        run_id=run_id,
        task_id=task_id or f"task_{uuid4().hex}",
        chapter_id=chapter_id,
        provider_call_cap=provider_call_cap,
        provider_call_cap_enforced=provider_call_cap_enforced,
        activity_scope=activity_scope,
        run_model_manifest_token=run_model_manifest_token,
        selection_test_manifest_token=selection_test_manifest_token,
        configured_cost_currency=configured_cost_currency,
        configured_cost_cap=configured_cost_cap,
        synchronous_memory_enrichment=synchronous_memory_enrichment,
    )
    token = _SCOPE.set(scope)
    try:
        yield scope
    finally:
        _SCOPE.reset(token)


def current_attempt_scope() -> AttemptScope | None:
    """返回当前 scope；线程未继承 ContextVar 时返回 ``None``。"""

    return _SCOPE.get()


@dataclass(frozen=True)
class AttemptSummary:
    """指定活动的 attempt 计数；不传 run/task 时返回工作区审计总量。"""

    provider_calls: int
    transport_retries: int
    pipeline_attempts: int
    upstream_generation_attempts: int


@dataclass(frozen=True)
class ProviderUsageCostSummary:
    """物理 Provider attempt 的权威计量汇总。

    ``configured_estimates`` 只按原币种相加，绝不做汇率换算；Provider 实际
    账单不在本地 attempt 协议内，API 会单独明确标成未导入。
    """

    usage_known_attempts: int
    usage_unknown_attempts: int
    legacy_unclassified_attempts: int
    input_tokens_known: int
    output_tokens_known: int
    provider_request_id_count: int
    response_status_counts: dict[str, int]
    configured_cost_basis_counts: dict[str, int]
    configured_estimates: dict[str, float]


@dataclass(frozen=True)
class _ProviderAttemptAccounting:
    reserved_input_tokens: int
    reserved_output_tokens: int
    cost_currency: str
    input_cost_per_1k: float
    output_cost_per_1k: float

    def amount(self, *, input_tokens: int, output_tokens: int) -> float:
        return (
            input_tokens * self.input_cost_per_1k
            + output_tokens * self.output_cost_per_1k
        ) / 1000.0


def _accounting_from_profile(
    profile: WriterProfile,
    messages: list[Message],
    complete_kwargs: dict[str, Any] | None = None,
    *,
    selection_test: bool = False,
) -> _ProviderAttemptAccounting:
    """Freeze the configured accounting basis for one physical attempt.

    Production keeps the pre-existing profile/input-budget calculation
    byte-for-byte.  Only an explicitly bound selection test uses the actual
    rendered UTF-8 Chat upper bound required by its RMB loss breaker.
    """

    params = dict(profile.generation_params)
    params.update(profile.extra_request_kwargs)
    params.update(complete_kwargs or {})

    configured_output = params.get("max_completion_tokens")
    if not isinstance(configured_output, int) or isinstance(configured_output, bool):
        answer_tokens = params.get("max_tokens")
        configured_output = answer_tokens
        thinking_tokens = params.get("thinking_budget")
        if (
            params.get("enable_thinking") is True
            and isinstance(answer_tokens, int)
            and not isinstance(answer_tokens, bool)
            and isinstance(thinking_tokens, int)
            and not isinstance(thinking_tokens, bool)
        ):
            configured_output = answer_tokens + thinking_tokens
    if not isinstance(configured_output, int) or isinstance(configured_output, bool):
        configured_output = profile.context_window
    reserved_output = max(0, min(int(configured_output), profile.context_window))

    if selection_test:
        reserved_input = conservative_rendered_input_tokens(messages)
        input_guard = params.get("input_token_budget")
        if (
            isinstance(input_guard, int)
            and not isinstance(input_guard, bool)
            and reserved_input > input_guard
        ):
            raise ValueError(
                "已渲染请求输入超过 profile 本地 guard:"
                f"upper_bound={reserved_input},guard={input_guard}"
            )
    else:
        configured_input = params.get("input_token_budget")
        if not isinstance(configured_input, int) or isinstance(
            configured_input, bool
        ):
            configured_input = max(profile.context_window - reserved_output, 0)
        reserved_input = max(
            0,
            min(int(configured_input), profile.context_window),
        )

    return _ProviderAttemptAccounting(
        reserved_input_tokens=reserved_input,
        reserved_output_tokens=reserved_output,
        cost_currency=profile.cost_currency,
        input_cost_per_1k=profile.cost_per_1k_input,
        output_cost_per_1k=profile.cost_per_1k_output,
    )


SessionFactory = Callable[[str], Session]


class AttemptLedgerStore:
    """以短事务维护 attempt 账本和 run 查询投影。"""

    def __init__(self, session_factory: SessionFactory) -> None:
        self._session_factory = session_factory

    def summarize(
        self,
        workspace_id: str,
        *,
        run_id: str | None = None,
        task_id: str | None = None,
    ) -> AttemptSummary:
        session = self._open(workspace_id)
        try:
            return self._summarize_in_session(
                session,
                workspace_id,
                run_id=run_id,
                task_id=task_id,
            )
        finally:
            session.close()

    def summarize_usage_cost(
        self,
        workspace_id: str,
        *,
        run_id: str | None = None,
        task_id: str | None = None,
    ) -> ProviderUsageCostSummary:
        """汇总物理请求计量；历史 NULL 与新请求 unknown 分开统计。"""

        session = self._open(workspace_id)
        try:
            stmt = (
                select(AttemptLedgerORM)
                .where(AttemptLedgerORM.workspace_id == workspace_id)
                .where(AttemptLedgerORM.attempt_kind == "provider")
            )
            if run_id is not None:
                stmt = stmt.where(AttemptLedgerORM.run_id == run_id)
            elif task_id is not None:
                stmt = stmt.where(AttemptLedgerORM.task_id == task_id)
            rows = list(session.scalars(stmt))
        finally:
            session.close()

        response_status_counts: dict[str, int] = {}
        configured_cost_basis_counts: dict[str, int] = {}
        configured_estimates: dict[str, float] = {}
        for row in rows:
            if row.response_status is not None:
                response_status_counts[row.response_status] = (
                    response_status_counts.get(row.response_status, 0) + 1
                )
            if (
                row.configured_cost_currency is not None
                and row.configured_cost_amount is not None
            ):
                currency = row.configured_cost_currency
                configured_estimates[currency] = (
                    configured_estimates.get(currency, 0.0)
                    + row.configured_cost_amount
                )
            if row.configured_cost_basis is not None:
                configured_cost_basis_counts[row.configured_cost_basis] = (
                    configured_cost_basis_counts.get(row.configured_cost_basis, 0)
                    + 1
                )

        known_rows = [row for row in rows if row.usage_status == "known"]
        return ProviderUsageCostSummary(
            usage_known_attempts=len(known_rows),
            usage_unknown_attempts=sum(
                row.usage_status == "unknown" for row in rows
            ),
            legacy_unclassified_attempts=sum(
                row.usage_status is None for row in rows
            ),
            input_tokens_known=sum(row.input_tokens or 0 for row in known_rows),
            output_tokens_known=sum(row.output_tokens or 0 for row in known_rows),
            provider_request_id_count=sum(
                row.provider_request_id is not None for row in rows
            ),
            response_status_counts=dict(sorted(response_status_counts.items())),
            configured_cost_basis_counts=dict(
                sorted(configured_cost_basis_counts.items())
            ),
            configured_estimates=dict(sorted(configured_estimates.items())),
        )

    def configured_cost_total(self, workspace_id: str, *, currency: str) -> float:
        """返回工作区指定原币种的 attempt 配置估算，不做换汇。"""

        summary = self.summarize_usage_cost(workspace_id)
        return summary.configured_estimates.get(currency.strip().upper(), 0.0)

    def begin_pipeline_attempt(
        self,
        *,
        workspace_id: str,
        run_id: str,
        chapter_id: str,
    ) -> AttemptLedgerORM:
        """跨 run 原子追加一次章节 technical attempt 审计事实。

        历史 attempt 永久保留并按工作区+章节单调编号，但不再充当未来恢复的
        lifetime hard gate。自动 retry 的局部窗口由 pipeline invocation 管理；
        真正的外部调用停止线仍由 provider call cap 在网络前强制执行。
        """

        session = self._open(workspace_id)
        try:
            self._begin_immediate(session)
            used = self._count(
                session,
                workspace_id=workspace_id,
                attempt_kind="pipeline",
                chapter_id=chapter_id,
            )
            attempt_number = used + 1
            attempt_id = f"att_{uuid4().hex}"
            row = AttemptLedgerORM(
                attempt_id=attempt_id,
                workspace_id=workspace_id,
                run_id=run_id,
                task_id=(f"{run_id}:{chapter_id}:pipeline-attempt-{attempt_number}"),
                chapter_id=chapter_id,
                attempt_kind="pipeline",
                operation_kind="chapter_execution",
                logical_call_id=None,
                agent_name=None,
                provider=None,
                model=None,
                attempt_number=attempt_number,
                # 0028 的共享 schema 要求非空正整数。pipeline 已无 lifetime
                # cap，因此这里记录本行已开放到的审计序号；provider 行仍记录
                # 并执行真实的活动 hard cap。
                attempt_cap=attempt_number,
                status="started",
                version_before=self._latest_version_id(session, chapter_id),
                version_after=None,
                error_type=None,
                created_at=datetime.now(UTC),
                completed_at=None,
            )
            session.add(row)
            session.flush()
            self._refresh_run_projection(
                session,
                workspace_id=workspace_id,
                run_id=run_id,
            )
            session.commit()
            session.refresh(row)
            session.expunge(row)
            return row
        except AttemptLedgerError:
            raise
        except Exception as exc:
            session.rollback()
            raise AttemptLedgerError("pipeline attempt 无法在执行前持久化") from exc
        finally:
            session.close()

    def finish_pipeline_attempt(
        self,
        *,
        workspace_id: str,
        attempt_id: str,
        succeeded: bool,
        error_type: str | None = None,
    ) -> None:
        session = self._open(workspace_id)
        try:
            row = session.get(AttemptLedgerORM, attempt_id)
            if row is None:
                raise AttemptLedgerError(f"attempt 不存在: {attempt_id}")
            row.status = "succeeded" if succeeded else "failed"
            row.error_type = error_type
            row.version_after = (
                self._latest_version_id(session, row.chapter_id)
                if row.chapter_id is not None
                else None
            )
            row.completed_at = datetime.now(UTC)
            session.commit()
        except AttemptLedgerError:
            session.rollback()
            raise
        except Exception as exc:
            session.rollback()
            raise AttemptLedgerError(
                f"pipeline attempt 结果无法持久化: {attempt_id}"
            ) from exc
        finally:
            session.close()

    def reconcile_orphaned_pipeline_attempts(
        self,
        *,
        session: Session,
        workspace_id: str,
        run_id: str,
        error_type: str = "PipelineInterrupted",
    ) -> list[str]:
        """收口已由 caller 证明失去 Worker 的 ``started`` pipeline 行。

        本方法不自行开事务、不 commit；``PipelineService`` 在赢得 resume CAS
        且确认本 Service 没有对应 heartbeat 后，于同一事务调用。筛选严格限定
        ``pipeline``，绝不改写结果未知的 provider attempt。中断点之后可能已有
        章节 UI 修复版本，因此这里同样不猜 ``version_after``，保留 ``None``。
        """

        rows = list(
            session.scalars(
                select(AttemptLedgerORM)
                .where(AttemptLedgerORM.workspace_id == workspace_id)
                .where(AttemptLedgerORM.run_id == run_id)
                .where(AttemptLedgerORM.attempt_kind == "pipeline")
                .where(AttemptLedgerORM.status == "started")
                .order_by(AttemptLedgerORM.created_at)
            )
        )
        if not rows:
            return []

        completed_at = datetime.now(UTC)
        for row in rows:
            row.status = "failed"
            row.error_type = error_type
            row.version_after = None
            row.completed_at = completed_at
        session.flush()
        return [row.attempt_id for row in rows]

    def pipeline_attempts_used(
        self,
        *,
        workspace_id: str,
        chapter_id: str,
    ) -> int:
        session = self._open(workspace_id)
        try:
            return self._count(
                session,
                workspace_id=workspace_id,
                attempt_kind="pipeline",
                chapter_id=chapter_id,
            )
        finally:
            session.close()

    def make_provider_observer(
        self,
        *,
        scope: AttemptScope,
        logical_call_id: str,
        operation_kind: str,
        agent_name: str,
        provider: str,
        model: str,
        accounting: _ProviderAttemptAccounting | None = None,
    ) -> ProviderAttemptObserver:
        return _PersistentProviderAttemptObserver(
            store=self,
            scope=scope,
            logical_call_id=logical_call_id,
            operation_kind=operation_kind,
            agent_name=agent_name,
            provider=provider,
            model=model,
            accounting=accounting,
        )

    def begin_provider_attempt(
        self,
        *,
        scope: AttemptScope,
        logical_call_id: str,
        operation_kind: str,
        agent_name: str,
        provider: str,
        model: str,
        technical_attempt: int,
        accounting: _ProviderAttemptAccounting | None = None,
    ) -> str:
        """物理 provider 请求前原子 cap-check + INSERT。"""

        session = self._open(scope.workspace_id)
        try:
            self._begin_immediate(session)
            aggregate_selection_activity = (
                scope.activity_scope == "selection_test"
            )
            used = self._count(
                session,
                workspace_id=scope.workspace_id,
                attempt_kind="provider",
                run_id=(None if aggregate_selection_activity else scope.run_id),
                task_id=(
                    None
                    if aggregate_selection_activity or scope.run_id is not None
                    else scope.task_id
                ),
            )
            if scope.provider_call_cap_enforced and used >= scope.provider_call_cap:
                session.rollback()
                raise ProviderCallCapExceeded(
                    workspace_id=scope.workspace_id,
                    used=used,
                    cap=scope.provider_call_cap,
                )
            if scope.configured_cost_cap is not None:
                if scope.activity_scope != "selection_test":
                    session.rollback()
                    raise AttemptLedgerError(
                        "production scope 不得启用 selection-test RMB loss breaker"
                    )
                if accounting is None or scope.configured_cost_currency is None:
                    session.rollback()
                    raise AttemptLedgerError(
                        "selection-test RMB loss breaker 已启用，"
                        "但本次 route 缺少可审计费用预留"
                    )
                if accounting.cost_currency != scope.configured_cost_currency:
                    session.rollback()
                    raise AttemptLedgerError(
                        "selection-test 费用币种与 route profile 币种不一致"
                    )
                cost_stmt = (
                    select(
                        func.coalesce(
                            func.sum(AttemptLedgerORM.configured_cost_amount),
                            0.0,
                        )
                    )
                    .where(AttemptLedgerORM.workspace_id == scope.workspace_id)
                    .where(AttemptLedgerORM.attempt_kind == "provider")
                    .where(
                        AttemptLedgerORM.configured_cost_currency
                        == scope.configured_cost_currency
                    )
                )
                if not aggregate_selection_activity:
                    cost_stmt = cost_stmt.where(
                        AttemptLedgerORM.run_id == scope.run_id
                        if scope.run_id is not None
                        else AttemptLedgerORM.task_id == scope.task_id
                    )
                current_amount = session.scalar(cost_stmt)
                used_cost = Decimal(str(current_amount or 0))
                next_reserve = Decimal(
                    str(
                        accounting.amount(
                            input_tokens=accounting.reserved_input_tokens,
                            output_tokens=accounting.reserved_output_tokens,
                        )
                    )
                )
                if used_cost + next_reserve > scope.configured_cost_cap:
                    session.rollback()
                    raise ProviderCostCapExceeded(
                        workspace_id=scope.workspace_id,
                        used=used_cost,
                        next_reserve=next_reserve,
                        cap=scope.configured_cost_cap,
                        currency=scope.configured_cost_currency,
                    )
            attempt_id = f"att_{uuid4().hex}"
            row = AttemptLedgerORM(
                attempt_id=attempt_id,
                workspace_id=scope.workspace_id,
                run_id=scope.run_id,
                task_id=scope.task_id,
                chapter_id=scope.chapter_id,
                attempt_kind="provider",
                operation_kind=operation_kind,
                logical_call_id=logical_call_id,
                agent_name=agent_name,
                provider=provider,
                model=model,
                attempt_number=technical_attempt,
                attempt_cap=scope.provider_call_cap,
                status="started",
                version_before=None,
                version_after=None,
                error_type=None,
                usage_status="unknown",
                input_tokens=None,
                output_tokens=None,
                reserved_input_tokens=(
                    accounting.reserved_input_tokens
                    if accounting is not None
                    else None
                ),
                reserved_output_tokens=(
                    accounting.reserved_output_tokens
                    if accounting is not None
                    else None
                ),
                configured_cost_currency=(
                    accounting.cost_currency if accounting is not None else None
                ),
                configured_input_cost_per_1k=(
                    accounting.input_cost_per_1k
                    if accounting is not None
                    else None
                ),
                configured_output_cost_per_1k=(
                    accounting.output_cost_per_1k
                    if accounting is not None
                    else None
                ),
                configured_cost_amount=(
                    accounting.amount(
                        input_tokens=accounting.reserved_input_tokens,
                        output_tokens=accounting.reserved_output_tokens,
                    )
                    if accounting is not None
                    else None
                ),
                configured_cost_basis=(
                    "conservative_reserve" if accounting is not None else None
                ),
                provider_request_id=None,
                response_status="request_started",
                created_at=datetime.now(UTC),
                completed_at=None,
            )
            session.add(row)
            session.flush()
            if scope.run_id is not None:
                self._refresh_run_projection(
                    session,
                    workspace_id=scope.workspace_id,
                    run_id=scope.run_id,
                )
            session.commit()
            return attempt_id
        except (ProviderCallCapExceeded, AttemptLedgerError):
            raise
        except Exception as exc:
            session.rollback()
            raise AttemptLedgerError("provider attempt 无法在网络调用前持久化") from exc
        finally:
            session.close()

    def finish_provider_attempt(
        self,
        *,
        workspace_id: str,
        attempt_id: str,
        succeeded: bool,
        error_type: str | None,
        outcome: ProviderAttemptOutcome | None = None,
    ) -> None:
        session = self._open(workspace_id)
        try:
            row = session.get(AttemptLedgerORM, attempt_id)
            if row is None:
                raise AttemptLedgerError(f"attempt 不存在: {attempt_id}")
            row.status = "succeeded" if succeeded else "failed"
            row.error_type = error_type
            resolved = outcome or ProviderAttemptOutcome.unknown(
                response_status="completed" if succeeded else "failed"
            )
            row.usage_status = resolved.usage_status
            row.input_tokens = resolved.input_tokens
            row.output_tokens = resolved.output_tokens
            row.provider_request_id = resolved.provider_request_id
            row.response_status = resolved.response_status
            if (
                resolved.usage_status == "known"
                and resolved.input_tokens is not None
                and resolved.output_tokens is not None
                and row.configured_input_cost_per_1k is not None
                and row.configured_output_cost_per_1k is not None
            ):
                row.configured_cost_amount = (
                    resolved.input_tokens * row.configured_input_cost_per_1k
                    + resolved.output_tokens * row.configured_output_cost_per_1k
                ) / 1000.0
                row.configured_cost_basis = "metered_usage"
            row.completed_at = datetime.now(UTC)
            session.commit()
        except AttemptLedgerError:
            session.rollback()
            raise
        except Exception as exc:
            session.rollback()
            raise AttemptLedgerError(
                f"provider attempt 结果无法持久化: {attempt_id}"
            ) from exc
        finally:
            session.close()

    def _open(self, workspace_id: str) -> Session:
        try:
            return self._session_factory(workspace_id)
        except Exception as exc:
            raise AttemptLedgerError(
                f"无法打开 attempt ledger: workspace={workspace_id}"
            ) from exc

    @staticmethod
    def _begin_immediate(session: Session) -> None:
        session.execute(text("BEGIN IMMEDIATE"))

    @staticmethod
    def _latest_version_id(session: Session, chapter_id: str) -> int:
        value = session.scalar(
            select(func.max(ChapterVersionORM.version_id)).where(
                ChapterVersionORM.chapter_id == chapter_id
            )
        )
        return int(value or 0)

    @staticmethod
    def _count(
        session: Session,
        *,
        workspace_id: str,
        attempt_kind: str,
        chapter_id: str | None = None,
        run_id: str | None = None,
        task_id: str | None = None,
    ) -> int:
        stmt = (
            select(func.count())
            .select_from(AttemptLedgerORM)
            .where(AttemptLedgerORM.workspace_id == workspace_id)
            .where(AttemptLedgerORM.attempt_kind == attempt_kind)
        )
        if chapter_id is not None:
            stmt = stmt.where(AttemptLedgerORM.chapter_id == chapter_id)
        if run_id is not None:
            stmt = stmt.where(AttemptLedgerORM.run_id == run_id)
        elif task_id is not None:
            stmt = stmt.where(AttemptLedgerORM.task_id == task_id)
        return int(session.scalar(stmt) or 0)

    @staticmethod
    def _summarize_in_session(
        session: Session,
        workspace_id: str,
        *,
        run_id: str | None = None,
        task_id: str | None = None,
    ) -> AttemptSummary:
        activity_filters = []
        if run_id is not None:
            activity_filters.append(AttemptLedgerORM.run_id == run_id)
        elif task_id is not None:
            activity_filters.append(AttemptLedgerORM.task_id == task_id)
        provider_calls = int(
            session.scalar(
                select(func.count())
                .select_from(AttemptLedgerORM)
                .where(AttemptLedgerORM.workspace_id == workspace_id)
                .where(AttemptLedgerORM.attempt_kind == "provider")
                .where(*activity_filters)
            )
            or 0
        )
        transport_retries = int(
            session.scalar(
                select(func.count())
                .select_from(AttemptLedgerORM)
                .where(AttemptLedgerORM.workspace_id == workspace_id)
                .where(AttemptLedgerORM.attempt_kind == "provider")
                .where(AttemptLedgerORM.attempt_number > 1)
                .where(*activity_filters)
            )
            or 0
        )
        pipeline_attempts = int(
            session.scalar(
                select(func.count())
                .select_from(AttemptLedgerORM)
                .where(AttemptLedgerORM.workspace_id == workspace_id)
                .where(AttemptLedgerORM.attempt_kind == "pipeline")
                .where(*activity_filters)
            )
            or 0
        )
        upstream_generation_attempts = int(
            session.scalar(
                select(func.count(func.distinct(AttemptLedgerORM.logical_call_id)))
                .where(AttemptLedgerORM.workspace_id == workspace_id)
                .where(AttemptLedgerORM.attempt_kind == "provider")
                .where(AttemptLedgerORM.operation_kind == "upstream_generation")
                .where(*activity_filters)
            )
            or 0
        )
        return AttemptSummary(
            provider_calls=provider_calls,
            transport_retries=transport_retries,
            pipeline_attempts=pipeline_attempts,
            upstream_generation_attempts=upstream_generation_attempts,
        )

    def _refresh_run_projection(
        self,
        session: Session,
        *,
        workspace_id: str,
        run_id: str,
    ) -> None:
        row = session.get(PipelineRunORM, run_id)
        if row is None:
            return
        summary = self._summarize_in_session(
            session,
            workspace_id,
            run_id=run_id,
        )
        row.provider_call_count = summary.provider_calls
        row.transport_retry_count = summary.transport_retries
        row.pipeline_attempt_count = summary.pipeline_attempts
        row.upstream_generation_attempt_count = summary.upstream_generation_attempts


class _PersistentProviderAttemptObserver(ProviderAttemptObserver):
    def __init__(
        self,
        *,
        store: AttemptLedgerStore,
        scope: AttemptScope,
        logical_call_id: str,
        operation_kind: str,
        agent_name: str,
        provider: str,
        model: str,
        accounting: _ProviderAttemptAccounting | None,
    ) -> None:
        self._store = store
        self._scope = scope
        self._logical_call_id = logical_call_id
        self._operation_kind = operation_kind
        self._agent_name = agent_name
        self._provider = provider
        self._model = model
        self._accounting = accounting

    def begin(self, *, technical_attempt: int) -> str:
        return self._store.begin_provider_attempt(
            scope=self._scope,
            logical_call_id=self._logical_call_id,
            operation_kind=self._operation_kind,
            agent_name=self._agent_name,
            provider=self._provider,
            model=self._model,
            technical_attempt=technical_attempt,
            accounting=self._accounting,
        )

    def finish(
        self,
        attempt_id: str,
        *,
        succeeded: bool,
        error_type: str | None,
        outcome: ProviderAttemptOutcome | None = None,
    ) -> None:
        # 结束态落盘失败必须冒泡；否则 caller 会拿到成功正文，而账本仍停在
        # request_started，恢复时无法区分“成功但漏记”与“结果未知”。
        self._store.finish_provider_attempt(
            workspace_id=self._scope.workspace_id,
            attempt_id=attempt_id,
            succeeded=succeeded,
            error_type=error_type,
            outcome=outcome,
        )


class AttemptTrackingClient(LLMClient):
    """给 routed client 绑定持久化 physical-attempt observer。"""

    def __init__(
        self,
        *,
        inner: LLMClient,
        store: AttemptLedgerStore,
        scope: AttemptScope,
        agent_name: str,
        provider: str,
        model: str,
        operation_kind: str,
        profile: WriterProfile,
        logical_call_id: str | None = None,
    ) -> None:
        self._inner = inner
        self._store = store
        self._scope = scope
        self._agent_name = agent_name
        self._provider = provider
        self._model = model
        self._operation_kind = operation_kind
        self._profile = profile
        self._logical_call_id = logical_call_id

    def prepare_strict_json_schema(
        self,
        *,
        model: str,
        schema_name: str,
        schema: dict[str, Any],
        gen_params: dict[str, Any],
    ) -> dict[str, Any]:
        """透传 inner 的结构化输出能力；attempt 包装只观察物理请求。"""

        return self._inner.prepare_strict_json_schema(
            model=model,
            schema_name=schema_name,
            schema=schema,
            gen_params=gen_params,
        )

    def complete(
        self,
        messages: list[Message],
        **gen_params: Any,
    ) -> LLMResponse:
        reserve_renderer = getattr(self._inner, "messages_for_cost_reserve", None)
        reserve_messages = (
            reserve_renderer(messages, gen_params)
            if callable(reserve_renderer)
            else messages
        )
        accounting = _accounting_from_profile(
            self._profile,
            reserve_messages,
            gen_params,
            selection_test=self._scope.activity_scope == "selection_test",
        )
        observer = self._store.make_provider_observer(
            scope=self._scope,
            logical_call_id=self._logical_call_id or f"call_{uuid4().hex}",
            operation_kind=self._operation_kind,
            agent_name=self._agent_name,
            provider=self._provider,
            model=self._model,
            accounting=accounting,
        )
        with bind_provider_attempt_observer(observer):
            return self._inner.complete(messages, **gen_params)

    def __getattr__(self, name: str) -> Any:
        """Preserve non-observation client capabilities through the wrapper.

        The manifest client owns provider-specific bounded trim wire mapping.
        Attempt tracking must observe the resulting physical request without
        hiding that capability and sending legacy Qwen-shaped kwargs instead.
        """

        return getattr(self._inner, name)


class _RoutableModelRouter(Protocol):
    """Structural router contract shared by legacy and manifest routers."""

    def route(
        self,
        agent_name: str,
        *,
        chapter_card: Any = None,
        override_profile_id: str | None = None,
    ) -> RoutedTarget: ...


class AttemptTrackingRouter:
    """ModelRouter duck-type 包装；每个 route 结果装 attempt client。"""

    def __init__(
        self,
        *,
        inner: _RoutableModelRouter,
        workspace_id: str,
        session_factory: SessionFactory,
    ) -> None:
        self._inner = inner
        self._store = AttemptLedgerStore(session_factory)
        self._default_scope = current_attempt_scope() or AttemptScope(
            workspace_id=workspace_id,
            run_id=None,
            task_id=f"task_{uuid4().hex}",
            chapter_id=None,
            provider_call_cap=DEFAULT_PROVIDER_CALL_HARD_CAP,
        )

    def _effective_scope(self) -> AttemptScope:
        scope = current_attempt_scope() or self._default_scope
        binding = getattr(self._inner, "run_model_manifest_binding", None)
        if binding is None:
            if scope.activity_scope == "selection_test":
                raise AttemptLedgerError(
                    "selection-test scope 必须绑定活动 manifest router"
                )
            return scope
        if (
            scope.run_model_manifest_token is not None
            and scope.run_model_manifest_token != binding.token
        ):
            raise AttemptLedgerError(
                "attempt scope manifest token 与当前中央配置不一致"
            )
        effective = replace(
            scope,
            run_model_manifest_token=(
                scope.run_model_manifest_token or binding.token
            ),
        )
        if scope.activity_scope != "selection_test":
            return effective

        selection_test = binding.selection_test
        if scope.selection_test_manifest_token != selection_test.token:
            raise AttemptLedgerError(
                "selection-test scope token 与当前 manifest 配置不一致"
            )
        if (
            selection_test.max_physical_requests <= 0
            or selection_test.max_configured_rmb <= 0
        ):
            raise AttemptLedgerError(
                "selection-test loss breakers 仍为 0/0，活动未获授权"
            )
        if (
            scope.configured_cost_cap is not None
            and (
                scope.configured_cost_cap != selection_test.max_configured_rmb
                or scope.configured_cost_currency != selection_test.cost_currency
            )
        ):
            raise AttemptLedgerError(
                "attempt scope selection-test RMB loss breaker 已漂移"
            )
        return replace(
            effective,
            provider_call_cap=selection_test.max_physical_requests,
            selection_test_manifest_token=selection_test.token,
            configured_cost_currency=selection_test.cost_currency,
            configured_cost_cap=selection_test.max_configured_rmb,
        )

    def route(
        self,
        agent_name: str,
        *,
        chapter_card: Any = None,
        override_profile_id: str | None = None,
    ) -> RoutedTarget:
        target = self._inner.route(
            agent_name,
            chapter_card=chapter_card,
            override_profile_id=override_profile_id,
        )
        scope = self._effective_scope()
        operation_kind = (
            "upstream_generation"
            if agent_name in _UPSTREAM_AGENT_NAMES
            else "agent_call"
        )
        return RoutedTarget(
            profile=target.profile,
            client=AttemptTrackingClient(
                inner=target.client,
                store=self._store,
                scope=scope,
                agent_name=agent_name,
                provider=target.profile.provider,
                model=target.profile.model,
                operation_kind=operation_kind,
                profile=target.profile,
            ),
            complete_kwargs=target.complete_kwargs,
        )

    def can_afford_provider_attempts(self, required_attempts: int) -> bool:
        """只读检查当前 activity 是否容得下一个并行批的最坏物理尝试数。

        真正的 hard cap 仍由 ``begin_provider_attempt`` 的 SQLite 原子写事务
        执行；本方法只用于在启动并行 worker 前选择并行或串行，避免额度尾部
        因线程抢锁顺序改变旧串行调用优先级。
        """

        if required_attempts < 1:
            raise ValueError("required_attempts 必须 >= 1")
        scope = self._effective_scope()
        if not scope.provider_call_cap_enforced:
            return True
        summary = self._store.summarize(
            scope.workspace_id,
            run_id=(
                None if scope.activity_scope == "selection_test" else scope.run_id
            ),
            task_id=(
                None
                if scope.activity_scope == "selection_test" or scope.run_id is not None
                else scope.task_id
            ),
        )
        remaining = scope.provider_call_cap - summary.provider_calls
        return remaining >= required_attempts

    def __getattr__(self, name: str) -> Any:
        return getattr(self._inner, name)


def wrap_router_with_attempt_tracking(
    router: _RoutableModelRouter,
    *,
    workspace_id: str,
    session_factory: SessionFactory,
) -> AttemptTrackingRouter:
    """生产 router 统一装配入口。"""

    return AttemptTrackingRouter(
        inner=router,
        workspace_id=workspace_id,
        session_factory=session_factory,
    )
