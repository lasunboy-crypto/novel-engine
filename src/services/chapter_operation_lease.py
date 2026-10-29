"""章节付费/改源操作的持久互斥租约。

租约存放在每工作区 ``novel.db``，与章节、attempt 账本同库，而不是进程内
字典。这样后端崩溃后，下一次
write/rewrite/review/revise 仍能看到上一操作可能已经发出 Provider 请求，先
保留现场并硬停，不能因为 Python 线程消失就重复扣费。
"""

from __future__ import annotations

from collections.abc import Callable
from dataclasses import dataclass
from datetime import UTC, datetime
import re
from uuid import uuid4

from sqlalchemy import delete, func, select, text, update
from sqlalchemy.exc import IntegrityError
from sqlalchemy.orm import Session

from src.core.db.models import (
    AttemptLedgerORM,
    ChapterOperationLeaseORM,
    ChapterVersionORM,
    PipelineRunORM,
)


_ACTIVE_PIPELINE_STATES = ("running", "paused", "interrupted")
_ZERO_MATERIALIZATION_PHASES = frozenset(
    {
        "reserved_before_provider",
        "chapter_state_claiming",
        "continuity_refresh_may_call_provider",
        "provider_capable_pipeline",
        "reviser_may_call_provider",
    }
)
_KNOWN_OPERATION_PHASES = {
    "write": frozenset(
        {
            "reserved_before_provider",
            "chapter_state_claiming",
            "continuity_refresh_may_call_provider",
            "provider_capable_pipeline",
            "orchestrator_result_materialized",
            "continuity_ledger_may_call_provider",
            "continuity_ledger_persisted",
            "ready_persisted",
            "operation_complete",
        }
    ),
    "rewrite": frozenset(
        {
            "reserved_before_provider",
            "chapter_state_claiming",
            "continuity_refresh_may_call_provider",
            "provider_capable_pipeline",
            "orchestrator_result_materialized",
            "continuity_ledger_may_call_provider",
            "continuity_ledger_persisted",
            "ready_persisted",
            "operation_complete",
        }
    ),
    "re-review": frozenset(
        {
            "reserved_before_provider",
            "chapter_state_claiming",
            "continuity_refresh_may_call_provider",
            "provider_capable_pipeline",
            "orchestrator_result_materialized",
            "continuity_ledger_may_call_provider",
            "continuity_ledger_persisted",
            "ready_persisted",
            "operation_complete",
        }
    ),
    "revise": frozenset(
        {
            "reserved_before_provider",
            "chapter_state_claiming",
            "reviser_may_call_provider",
            "operation_complete",
        }
    ),
    "checkpoint-resume": frozenset(
        {
            "reserved_before_provider",
            "continuity_ledger_may_call_provider",
            "continuity_ledger_persisted",
            "ready_persisted",
            "operation_complete",
        }
    ),
    "force-pass": frozenset(
        {
            "reserved_before_provider",
            "continuity_ledger_may_call_provider",
            "continuity_ledger_persisted",
            "ready_persisted",
            "operation_complete",
        }
    ),
    "rewind": frozenset(
        {
            "reserved_before_provider",
            "source_mutation_committed",
            "operation_complete",
        }
    ),
    "update-content-manual": frozenset(
        {
            "reserved_before_provider",
            "source_mutation_committed",
            "operation_complete",
        }
    ),
    "switch-version": frozenset(
        {
            "reserved_before_provider",
            "operation_complete",
        }
    ),
}


class ChapterOperationLeaseError(RuntimeError):
    """章节操作租约不能安全取得、验证或释放。"""


class ChapterOperationLeaseConflict(ChapterOperationLeaseError):
    """同一章节已有持久操作占用；必须保留现场，禁止新 Worker/请求。"""

    def __init__(self, existing: "ChapterOperationLease") -> None:
        self.existing = existing
        super().__init__(
            "章节已有未收口操作，已阻止重复 Worker/Provider 请求: "
            f"workspace={existing.workspace_id} chapter={existing.chapter_id} "
            f"operation={existing.operation_kind} task={existing.task_id} "
            f"phase={existing.phase} source_version={existing.source_version}"
        )


@dataclass(frozen=True)
class ChapterOperationLease:
    """调用方持有的不可变租约快照。"""

    workspace_id: str
    chapter_id: str
    lease_id: str
    operation_kind: str
    source_version: int
    task_id: str
    run_id: str | None
    phase: str
    created_at: datetime
    updated_at: datetime


SessionFactory = Callable[[str], Session]


class ChapterOperationLeaseStore:
    """以短事务维护一章唯一的持久操作租约。"""

    def __init__(self, session_factory: SessionFactory) -> None:
        self._session_factory = session_factory

    @staticmethod
    def begin_immediate(session: Session) -> None:
        """SQLite 协调事务在任何读之前抢写锁，消除 check→insert 竞态。"""

        bind = session.get_bind()
        if bind.dialect.name != "sqlite":
            raise ChapterOperationLeaseError(
                "章节操作租约当前只支持 workspace SQLite 协调事务"
            )
        session.execute(text("BEGIN IMMEDIATE"))

    @staticmethod
    def _validate_value(lease: ChapterOperationLease) -> None:
        """拒绝未知 operation/phase 与损坏标识，绝不容错洗掉租约现场。"""

        ids_valid = (
            all(
                isinstance(value, str) and bool(value.strip())
                for value in (
                    lease.workspace_id,
                    lease.chapter_id,
                    lease.task_id,
                )
            )
            and re.fullmatch(r"chapter_op_[0-9a-f]{32}", lease.lease_id)
            is not None
            and (
                lease.run_id is None
                or (isinstance(lease.run_id, str) and bool(lease.run_id.strip()))
            )
        )
        allowed_phases = _KNOWN_OPERATION_PHASES.get(lease.operation_kind)
        phase_valid = bool(
            allowed_phases is not None
            and (
                lease.phase in allowed_phases
                or (
                    lease.operation_kind == "switch-version"
                    and re.fullmatch(
                        r"local_final_mutation:v[1-9][0-9]*",
                        lease.phase,
                    )
                    is not None
                )
            )
        )
        timestamps_valid = (
            isinstance(lease.created_at, datetime)
            and isinstance(lease.updated_at, datetime)
            and (
                lease.updated_at.replace(tzinfo=UTC)
                if lease.updated_at.tzinfo is None
                else lease.updated_at.astimezone(UTC)
            )
            >= (
                lease.created_at.replace(tzinfo=UTC)
                if lease.created_at.tzinfo is None
                else lease.created_at.astimezone(UTC)
            )
        )
        if (
            not ids_valid
            or type(lease.source_version) is not int
            or lease.source_version < 0
            or not phase_valid
            or not timestamps_valid
        ):
            raise ChapterOperationLeaseError(
                "章节操作租约持久形状损坏，已保留现场并禁止自动释放: "
                f"workspace={lease.workspace_id!r} chapter={lease.chapter_id!r} "
                f"operation={lease.operation_kind!r} phase={lease.phase!r}"
            )

    @classmethod
    def _to_value(cls, row: ChapterOperationLeaseORM) -> ChapterOperationLease:
        lease = ChapterOperationLease(
            workspace_id=row.workspace_id,
            chapter_id=row.chapter_id,
            lease_id=row.lease_id,
            operation_kind=row.operation_kind,
            source_version=row.source_version,
            task_id=row.task_id,
            run_id=row.run_id,
            phase=row.phase,
            created_at=row.created_at,
            updated_at=row.updated_at,
        )
        cls._validate_value(lease)
        return lease

    @staticmethod
    def _require_operation_phase(operation_kind: str, phase: str) -> None:
        allowed = _KNOWN_OPERATION_PHASES.get(operation_kind)
        if allowed is None or not (
            phase in allowed
            or (
                operation_kind == "switch-version"
                and re.fullmatch(r"local_final_mutation:v[1-9][0-9]*", phase)
                is not None
            )
        ):
            raise ChapterOperationLeaseError(
                "章节操作租约 operation/phase 组合不合法，已禁止写入: "
                f"operation={operation_kind!r} phase={phase!r}"
            )

    @staticmethod
    def _require_source_version_current(
        session: Session,
        *,
        chapter_id: str,
        source_version: int,
    ) -> None:
        """在取得 SQLite 写锁后复核不可变版本水位。"""

        actual = int(
            session.scalar(
                select(func.max(ChapterVersionORM.version_id)).where(
                    ChapterVersionORM.chapter_id == chapter_id
                )
            )
            or 0
        )
        if actual != source_version:
            raise ChapterOperationLeaseError(
                "章节操作预约的 source_version 已过期，已在创建租约前"
                "拒绝: "
                f"chapter={chapter_id} expected={source_version} actual={actual}"
            )

    def get(
        self, workspace_id: str, chapter_id: str
    ) -> ChapterOperationLease | None:
        session = self._session_factory(workspace_id)
        try:
            row = session.get(
                ChapterOperationLeaseORM,
                (workspace_id, chapter_id),
            )
            return self._to_value(row) if row is not None else None
        finally:
            session.close()

    def list_workspace(self, workspace_id: str) -> list[ChapterOperationLease]:
        """返回一个工作区的全部未收口租约；启动恢复只读该持久现场。"""

        session = self._session_factory(workspace_id)
        try:
            rows = list(
                session.scalars(
                    select(ChapterOperationLeaseORM)
                    .where(ChapterOperationLeaseORM.workspace_id == workspace_id)
                    .order_by(ChapterOperationLeaseORM.created_at)
                )
            )
            return [self._to_value(row) for row in rows]
        finally:
            session.close()

    @staticmethod
    def _recoverable_pipeline_for_manual_operation(
        session: Session,
        *,
        workspace_id: str,
        chapter_id: str,
    ) -> PipelineRunORM | None:
        """找出会与 UI 改源竞争的唯一当前 run，并拒绝损坏的多 active 行。"""

        active_rows = list(
            session.scalars(
                select(PipelineRunORM)
                .where(PipelineRunORM.workspace_id == workspace_id)
                .where(PipelineRunORM.state.in_(_ACTIVE_PIPELINE_STATES))
                .order_by(PipelineRunORM.updated_at.desc())
            )
        )
        if len(active_rows) > 1:
            raise ChapterOperationLeaseError(
                "同一工作区存在多个 active pipeline run，已保留现场并禁止改源"
            )
        if active_rows:
            return active_rows[0]

        latest_error = session.scalar(
            select(PipelineRunORM)
            .where(PipelineRunORM.workspace_id == workspace_id)
            .where(PipelineRunORM.state == "error")
            .order_by(PipelineRunORM.updated_at.desc())
            .limit(1)
        )
        if latest_error is None:
            return None
        details = latest_error.error_details or {}
        if (
            details.get("error_type") == "ContinuityLedgerNotReadyError"
            and latest_error.current_chapter == chapter_id
        ):
            return latest_error
        return None

    @classmethod
    def _resolve_pipeline_binding(
        cls,
        session: Session,
        *,
        workspace_id: str,
        chapter_id: str,
        requested_run_id: str | None,
        allow_completed_current: bool = False,
    ) -> str | None:
        """在同一写事务内冻结 UI/Pipeline 的 run 归属。"""

        if requested_run_id is not None:
            run = session.get(PipelineRunORM, requested_run_id)
            if (
                run is None
                or run.workspace_id != workspace_id
                or run.state != "running"
                or run.current_chapter != chapter_id
                or chapter_id not in run.chapter_ids
                or chapter_id in run.completed_chapter_ids
                or chapter_id in run.skipped_chapter_ids
            ):
                raise ChapterOperationLeaseError(
                    "Pipeline Worker 的 run/current chapter 归属已变化，"
                    "禁止取得章节操作租约: "
                    f"workspace={workspace_id} chapter={chapter_id} "
                    f"run_id={requested_run_id}"
                )
            return requested_run_id

        run = cls._recoverable_pipeline_for_manual_operation(
            session,
            workspace_id=workspace_id,
            chapter_id=chapter_id,
        )
        if run is None or chapter_id not in run.chapter_ids:
            return None
        if run.state == "running":
            raise ChapterOperationLeaseError(
                "Pipeline 正在运行，已阻止 UI 创建并发章节 Worker: "
                f"run_id={run.run_id} chapter={chapter_id}"
            )
        if (
            run.current_chapter != chapter_id
            or (
                chapter_id in run.completed_chapter_ids
                and not allow_completed_current
            )
            or chapter_id in run.skipped_chapter_ids
        ):
            raise ChapterOperationLeaseError(
                "暂停/中断 run 只允许修复尚未完成的 current chapter: "
                f"run_id={run.run_id} current={run.current_chapter} "
                f"requested={chapter_id}"
            )
        return run.run_id

    def acquire(
        self,
        *,
        workspace_id: str,
        chapter_id: str,
        operation_kind: str,
        source_version: int,
        task_id: str,
        run_id: str | None,
    ) -> ChapterOperationLease:
        """用复合主键 INSERT 抢占；冲突时只读返回原现场并拒绝覆盖。"""

        self._require_operation_phase(operation_kind, "reserved_before_provider")
        if source_version < 0:
            raise ValueError("source_version 必须 >= 0")
        now = datetime.now(UTC)
        row = ChapterOperationLeaseORM(
            workspace_id=workspace_id,
            chapter_id=chapter_id,
            lease_id=f"chapter_op_{uuid4().hex}",
            operation_kind=operation_kind,
            source_version=source_version,
            task_id=task_id,
            run_id=run_id,
            phase="reserved_before_provider",
            created_at=now,
            updated_at=now,
        )
        session = self._session_factory(workspace_id)
        try:
            self.begin_immediate(session)
            self._require_source_version_current(
                session,
                chapter_id=chapter_id,
                source_version=source_version,
            )
            row.run_id = self._resolve_pipeline_binding(
                session,
                workspace_id=workspace_id,
                chapter_id=chapter_id,
                requested_run_id=run_id,
                allow_completed_current=False,
            )
            session.add(row)
            session.commit()
            session.refresh(row)
            return self._to_value(row)
        except IntegrityError as exc:
            session.rollback()
            existing = session.scalar(
                select(ChapterOperationLeaseORM).where(
                    ChapterOperationLeaseORM.workspace_id == workspace_id,
                    ChapterOperationLeaseORM.chapter_id == chapter_id,
                )
            )
            if existing is None:
                raise ChapterOperationLeaseError(
                    "章节操作租约 INSERT 冲突，但无法读取现有租约；已禁止继续"
                ) from exc
            raise ChapterOperationLeaseConflict(self._to_value(existing)) from exc
        except Exception:
            session.rollback()
            raise
        finally:
            session.close()

    def acquire_in_session(
        self,
        session: Session,
        *,
        workspace_id: str,
        chapter_id: str,
        operation_kind: str,
        source_version: int,
        task_id: str,
        run_id: str | None,
        allow_completed_current: bool = False,
    ) -> ChapterOperationLease:
        """在 caller 的 BEGIN IMMEDIATE 事务内插入租约，不自行 commit。"""

        self._require_operation_phase(operation_kind, "reserved_before_provider")
        existing = session.get(
            ChapterOperationLeaseORM,
            (workspace_id, chapter_id),
        )
        if existing is not None:
            raise ChapterOperationLeaseConflict(self._to_value(existing))
        self._require_source_version_current(
            session,
            chapter_id=chapter_id,
            source_version=source_version,
        )
        resolved_run_id = self._resolve_pipeline_binding(
            session,
            workspace_id=workspace_id,
            chapter_id=chapter_id,
            requested_run_id=run_id,
            allow_completed_current=allow_completed_current,
        )
        now = datetime.now(UTC)
        row = ChapterOperationLeaseORM(
            workspace_id=workspace_id,
            chapter_id=chapter_id,
            lease_id=f"chapter_op_{uuid4().hex}",
            operation_kind=operation_kind,
            source_version=source_version,
            task_id=task_id,
            run_id=resolved_run_id,
            phase="reserved_before_provider",
            created_at=now,
            updated_at=now,
        )
        session.add(row)
        session.flush()
        return self._to_value(row)

    def mark_phase_in_session(
        self,
        session: Session,
        lease: ChapterOperationLease,
        *,
        phase: str,
    ) -> ChapterOperationLease:
        """在 caller 事务内推进 phase，使业务 commit 与 crash 证据同落。"""

        self._require_operation_phase(lease.operation_kind, phase)
        self._validate_value(lease)
        row = session.get(
            ChapterOperationLeaseORM,
            (lease.workspace_id, lease.chapter_id),
        )
        if (
            row is None
            or row.lease_id != lease.lease_id
            or row.phase != lease.phase
        ):
            raise ChapterOperationLeaseError(
                "章节操作租约事务内 phase CAS 失败，已禁止继续"
            )
        row.phase = phase
        row.updated_at = datetime.now(UTC)
        session.flush()
        return self._to_value(row)

    def lease_has_no_materialized_work(
        self,
        lease: ChapterOperationLease,
        *,
        disk_latest_version: int,
    ) -> bool:
        """严格证明早期阶段“零出网、零新版本”时允许安全释放。

        ``chapter_state_claiming`` 与章节状态 CAS 在同一事务提交；
        ``provider_capable_pipeline`` 只表示下一步可能出网。只要本 task 没有
        ``started``（结果未知）或 ``succeeded``（响应已返回但尚未物化）的
        Provider attempt，且 DB/磁盘版本都没有越过 source，就没有可重复消费
        的结果或正文可丢。完整失败的历史 attempt 继续留账，但不会把一次已知
        失败永久写成不可恢复终态。租约建立后，同章任何 task 的出网记录都必须
        纳入；task 错绑本身就是账本异常，不能用 task_id 过滤后误报零调用。
        """

        if lease.phase not in _ZERO_MATERIALIZATION_PHASES:
            return False
        session = self._session_factory(lease.workspace_id)
        try:
            unsafe_provider_attempts = int(
                session.scalar(
                    select(func.count())
                    .select_from(AttemptLedgerORM)
                    .where(
                        AttemptLedgerORM.workspace_id == lease.workspace_id,
                        AttemptLedgerORM.chapter_id == lease.chapter_id,
                        AttemptLedgerORM.attempt_kind == "provider",
                        AttemptLedgerORM.status.in_(("started", "succeeded")),
                        AttemptLedgerORM.created_at >= lease.created_at,
                    )
                )
                or 0
            )
            db_latest_version = int(
                session.scalar(
                    select(func.max(ChapterVersionORM.version_id)).where(
                        ChapterVersionORM.chapter_id == lease.chapter_id
                    )
                )
                or 0
            )
        finally:
            session.close()
        return (
            unsafe_provider_attempts == 0
            and db_latest_version == lease.source_version
            and disk_latest_version == lease.source_version
        )

    # 兼容当前调用点与历史测试命名；语义已扩成全部严格零物化早期 phase。
    reserved_lease_has_no_materialized_work = lease_has_no_materialized_work

    def mark_phase(
        self, lease: ChapterOperationLease, *, phase: str
    ) -> ChapterOperationLease:
        """仅租约所有者可更新阶段；用于保留 crash 发生点。"""

        self._require_operation_phase(lease.operation_kind, phase)
        self._validate_value(lease)
        now = datetime.now(UTC)
        session = self._session_factory(lease.workspace_id)
        try:
            result = session.execute(
                update(ChapterOperationLeaseORM)
                .where(
                    ChapterOperationLeaseORM.workspace_id == lease.workspace_id,
                    ChapterOperationLeaseORM.chapter_id == lease.chapter_id,
                    ChapterOperationLeaseORM.lease_id == lease.lease_id,
                    ChapterOperationLeaseORM.phase == lease.phase,
                )
                .values(phase=phase, updated_at=now)
            )
            if result.rowcount != 1:
                session.rollback()
                raise ChapterOperationLeaseError(
                    "章节操作租约已丢失、被替换或已由另一 Worker 推进，"
                    "禁止继续写入: "
                    f"workspace={lease.workspace_id} chapter={lease.chapter_id}"
                )
            session.commit()
            updated = session.get(
                ChapterOperationLeaseORM,
                (lease.workspace_id, lease.chapter_id),
            )
            if updated is None:
                raise ChapterOperationLeaseError("章节操作租约更新后无法读取")
            return self._to_value(updated)
        except Exception:
            session.rollback()
            raise
        finally:
            session.close()

    def require_held(self, lease: ChapterOperationLease) -> None:
        """在关键持久化点复核调用方仍是唯一所有者。"""

        current = self.get(lease.workspace_id, lease.chapter_id)
        if current is None or current.lease_id != lease.lease_id:
            raise ChapterOperationLeaseError(
                "章节操作租约已丢失或被替换，禁止继续写入: "
                f"workspace={lease.workspace_id} chapter={lease.chapter_id}"
            )

    def release(self, lease: ChapterOperationLease) -> None:
        """仅精确所有者可删租约；ABA/stale callback 不能释放新租约。"""

        session = self._session_factory(lease.workspace_id)
        try:
            result = session.execute(
                delete(ChapterOperationLeaseORM).where(
                    ChapterOperationLeaseORM.workspace_id == lease.workspace_id,
                    ChapterOperationLeaseORM.chapter_id == lease.chapter_id,
                    ChapterOperationLeaseORM.lease_id == lease.lease_id,
                )
            )
            if result.rowcount != 1:
                session.rollback()
                raise ChapterOperationLeaseError(
                    "章节操作租约释放失败；现场保持不变: "
                    f"workspace={lease.workspace_id} chapter={lease.chapter_id}"
                )
            session.commit()
        except Exception:
            session.rollback()
            raise
        finally:
            session.close()

    def release_in_session(
        self,
        session: Session,
        lease: ChapterOperationLease,
    ) -> None:
        """在 caller 的事务内按 lease id 删除，不自行 commit。

        用于“恢复标记 + 释放旧锁”必须原子同落的冷启动收口；任何 owner / phase
        漂移都抛错并由 caller 回滚整笔事务。
        """

        self._validate_value(lease)
        result = session.execute(
            delete(ChapterOperationLeaseORM).where(
                ChapterOperationLeaseORM.workspace_id == lease.workspace_id,
                ChapterOperationLeaseORM.chapter_id == lease.chapter_id,
                ChapterOperationLeaseORM.lease_id == lease.lease_id,
                ChapterOperationLeaseORM.phase == lease.phase,
            )
        )
        if getattr(result, "rowcount", 0) != 1:
            raise ChapterOperationLeaseError(
                "章节操作租约事务内释放失败；恢复标记未提交: "
                f"workspace={lease.workspace_id} chapter={lease.chapter_id}"
            )

    def release_if_phase(
        self,
        lease: ChapterOperationLease,
        *,
        expected_phase: str,
    ) -> bool:
        """仅在 lease id 与 phase 都未变时释放。

        这是失败/取消回调的必备 CAS：同 task 的过期回调即使还
        持有旧 ``lease_id``，也不能删掉已被真正 Worker 推进到下一 phase
        的安全锁。
        """

        session = self._session_factory(lease.workspace_id)
        try:
            result = session.execute(
                delete(ChapterOperationLeaseORM).where(
                    ChapterOperationLeaseORM.workspace_id == lease.workspace_id,
                    ChapterOperationLeaseORM.chapter_id == lease.chapter_id,
                    ChapterOperationLeaseORM.lease_id == lease.lease_id,
                    ChapterOperationLeaseORM.phase == expected_phase,
                )
            )
            session.commit()
            return result.rowcount == 1
        except Exception:
            session.rollback()
            raise
        finally:
            session.close()


__all__ = [
    "ChapterOperationLease",
    "ChapterOperationLeaseConflict",
    "ChapterOperationLeaseError",
    "ChapterOperationLeaseStore",
]
