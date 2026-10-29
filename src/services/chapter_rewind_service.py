"""章节人工介入服务(P6-B-01):rewind / force-pass / unblock / unblock-all
+ reset_stale(v2 w1-s2 中断章自愈)+富记忆进程中断收口。

对齐 docs/plan/p6_plan.md §"P6-B-01"字面 + plan-kickoff 决策 2/3(rewind
soft-delete + 后续章节 invalidated 显式 unblock)+ P5-B-07 状态机集中表
模式延续(``_CARD_TRANSITION_RULES`` 同源)。

设计要点
========

1. **rewind 三层副作用**(决策 2=B / 决策 3=A):
   - **目标章节**:``chapters.status`` 翻 ``drafting``(回到未写状态),
     ``approved_at`` 重置 None。``chapter_versions`` 表保留(不删历史)。
   - **后续章节标废**:``chapter_id > target_id`` 同 workspace 行 status
     翻 ``invalidated``(``chapter:write`` / ``:rewrite`` / ``:revise``
     gate 检测翻 ``ChapterInvalidatedNeedsUnblockError`` 409 阻断,要求
     user 显式 ``:unblock``)。
   - **记忆库回退**:① Chroma ``soft_delete_by_chapter_id`` 把
     ``metadata.chapter_id == target_id`` 文档 metadata 合并 ``revoked: true``;
     ② SQLite ``characters`` / ``plot_threads`` / ``timeline_entries`` 表
     按 ``first_appearance_chapter`` / ``planted_chapter`` / ``chapter_id``
     字段匹配 → ``revoked_at = now``。

2. **force-pass 硬水位**:跳过 Reviewer 强制 ``approved`` 并写审计原因后，
   仍必须按 ``final → 同步连续性 ledger 校验 → ready`` 顺序闭合；缺版本、
   抽取或校验失败都保持 ready 关闭。向量和完整记忆只在 ready 后 fail-open。

3. **unblock / unblock-all 单层副作用**:``invalidated`` → ``drafting``;
   ``unblock-all`` 是 workspace 范围批量(自决项 2:空状态 200 + count=0
   幂等,对齐 P3-B-12 batch 端点先例)。

4. **错误类 3 个**:对齐启动指令字面 3 错误码 +1 复用既有:
   - ``ChapterRewindInvalidStateError`` → 422 ``CHAPTER_REWIND_INVALID_STATE``
   - ``ChapterInvalidatedNeedsUnblockError`` → 409 ``CHAPTER_INVALIDATED_NEEDS_UNBLOCK``
     (主要由 ``ChapterService._run_gates`` 抛,本 service 自身不抛)
   - ``ChapterUnblockInvalidStateError`` → 422 ``CHAPTER_UNBLOCK_INVALID_STATE``
   - 力推过失败(``approved`` / ``published`` / ``invalidated``)走既有
     ``ChapterServiceError`` 翻译为 ``CHAPTER_INVALID_STATE`` 409(复用,
     不增独立错误码;对齐 P5-B-09 ``VolumeUpdateInvalid`` 复用既有 422 同模式)。

5. **WS publish 4 事件**:对齐启动指令字面到 ``chapter.{chapter_id}.events``
   频道(沿用 P1-B-20 `chapter_events_channel`,与 P5-B-07 card.events 解耦):
   - ``chapter_rewound``(rewind 目标章节)
   - ``chapter_invalidated``(rewind 触发的每个后续章节)
   - ``chapter_unblocked``(unblock / unblock-all 翻回 drafting 的每章)
   - ``chapter_force_passed``(force-pass)

6. **事务边界**:DB 更新(target row + subsequent rows + extracted_*
   revoked_at)在单 session 内 ``commit``;commit 失败 → ``rollback`` + 抛。
   Chroma soft-delete 在 DB commit 成功后跑。生产路径有 durable operation
   lease 时，删除失败必须保留 ``source_mutation_committed`` checkpoint 并抛错，
   由冷启动幂等补齐后才释放；无 lease 的历史注入路径保留 best-effort 兼容。

7. **依赖注入**:沿用 ``ChapterService`` / ``ChapterCardService`` 模式 —
   ``session_factory`` / ``vector_store_factory`` / ``ws_manager`` /
   ``workspace_service`` 全注入,无任何全局状态;测试通过 ``lambda``
   注入 in-memory engine / 单测 fake VectorStore。

8. **chapter_id 字符串比较**:plan 字面"后续章节"按 ``chapter_id > target_id``
   字符串比较定义(工程实情 ``ch-NNNN`` 4 位 0 填充,lexicographic 序与
   chapter_number 一致;P6+ 若引入跨卷连续编号或 chapter_id 命名变化,
   本规则可能需要改读 ``chapter_cards.chapter_number`` 派生,留观项)。
"""

from __future__ import annotations

from collections.abc import Callable
from contextlib import nullcontext
from datetime import UTC, datetime
from typing import TYPE_CHECKING, Any
from uuid import uuid4

import structlog
from pydantic import BaseModel, ConfigDict, Field
from sqlalchemy import JSON, or_, select, update
from sqlalchemy.orm import Session

from src.api.websocket.channels import chapter_events_channel
from src.core.agents.chapter_summary_extractor import (
    ContinuityLedgerValidationError,
    extract_and_persist_continuity_ledger,
    load_validated_continuity_ledger,
)
from src.core.chapter_versioning import (
    ChapterVersioning,
    ChapterVersioningError,
)
from src.core.db.models import (
    CharacterORM,
    ChapterOperationLeaseORM,
    ChapterORM,
    PipelineRunORM,
    PlotThreadORM,
    TimelineEntryORM,
    WorkspaceORM,
)
from src.core.models.chapter_card import ChapterCard, chapter_seq_from_id
from src.core.memory.retriever import DEFAULT_COLLECTION
from src.core.workspace.workspace_paths import WorkspacePaths
from src.services.memory_enrichment_checkpoint import (
    MemoryEnrichmentCheckpointShapeError,
    memory_enrichment_requires_manual_recovery,
    validate_memory_enrichment_checkpoint_shape,
)
from src.services.memory_enrichment_attempts import (
    MemoryEnrichmentAttemptLedgerError,
    load_unconsumed_memory_provider_attempts,
)
from src.services.memory_enrichment_footprint import (
    audit_sqlite_memory_snapshot,
    validate_sqlite_memory_footprint,
)
from src.services.attempt_ledger import (
    DEFAULT_PROVIDER_CALL_HARD_CAP,
    bind_attempt_scope,
)
from src.services.chapter_operation_lease import (
    ChapterOperationLease,
    ChapterOperationLeaseConflict,
    ChapterOperationLeaseError,
    ChapterOperationLeaseStore,
)
from src.services.cost_tracker import wrap_router_with_cost_tracking
from src.services.core_state_service import CoreStateService
from src.services.outline_entity_recovery import (
    OutlineEntityRecoveryError,
    load_outline_entity_baselines,
    restore_rewound_outline_entities_in_session,
)
from src.services.workspace_service import WorkspaceService

if TYPE_CHECKING:
    from src.api.websocket.manager import WebSocketManager
    from src.core.memory.vector import VectorChapterSnapshot, VectorStore


_log = structlog.get_logger(__name__)


SessionFactory = Callable[[str], Session]
VectorStoreFactory = Callable[[str], "VectorStore"]
VectorCountAuditor = Callable[[str, str], "VectorChapterSnapshot | int"]


REWIND_REWRITE_REQUIRED_REASON = "rewind_requires_content_rewrite"
"""``rewind`` 后 ``:write`` 必须先产出新正文的持久意图标记。

正文版本和审稿历史按产品契约保留，因此不能靠“存在旧版本”推断下次 ``:write``
应该续审。标记绑定 rewind 当时的精确 ``source_version_id``：若 Writer 尚未落
新版本，普通 ``:write`` 也必须从 REWRITING 起跑；一旦新版本已持久化，标记自然
过期，崩溃恢复仍可续审新版本而不会重复调用 Writer。
"""

CANCELLED_REVIEW_REPLAY_REASON = "cancelled_post_review_replay"
"""冷启动已验真的用户取消断点：保留当前正文并只允许重跑审校。"""


# =============================================================================
# 异常类型
# =============================================================================


class ChapterRewindServiceError(Exception):
    """chapter_rewind_service 通用错误。"""


class ForcePassContinuityLedgerNotReadyError(ChapterRewindServiceError):
    """force-pass 已批准，但同步小账本未达到 ready 硬水位。"""

    def __init__(self, *, chapter_id: str, reason: str) -> None:
        self.chapter_id = chapter_id
        self.reason = reason
        super().__init__(f"连续性小账本未就绪: chapter_id={chapter_id}: {reason}")


class ChapterRewindInvalidStateError(ChapterRewindServiceError):
    """``rewind`` 时目标章节状态不允许 rewind。

    P6-B-01 引入。路由层翻译为 ``CHAPTER_REWIND_INVALID_STATE (422)``。
    不允许的源状态:``drafting``(本就未写,无需 rewind)/ ``invalidated``
    (上游已被 rewind,need unblock 而非 rewind)。
    """

    def __init__(
        self,
        *,
        chapter_id: str,
        current_status: str,
        allowed_sources: tuple[str, ...],
    ) -> None:
        self.chapter_id = chapter_id
        self.current_status = current_status
        self.allowed_sources = allowed_sources
        super().__init__(
            f"章节状态不允许 rewind: chapter_id={chapter_id} "
            f"current_status={current_status!r} "
            f"allowed_sources={list(allowed_sources)!r}"
        )


class ChapterInvalidatedNeedsUnblockError(ChapterRewindServiceError):
    """``chapter:write`` / ``:rewrite`` / ``:revise`` gate 检测到本章
    ``status='invalidated'``。

    P6-B-01 引入。路由层翻译为 ``CHAPTER_INVALIDATED_NEEDS_UNBLOCK (409)``。
    user 需先调 ``POST /chapters/{id}:unblock`` 翻 ``drafting`` 才能继续写。
    """

    def __init__(self, *, chapter_id: str, workspace_id: str) -> None:
        self.chapter_id = chapter_id
        self.workspace_id = workspace_id
        super().__init__(
            f"章节 stale 需 unblock: workspace={workspace_id} "
            f"chapter={chapter_id}"
        )


class ChapterUnblockInvalidStateError(ChapterRewindServiceError):
    """``unblock`` 时目标章节状态非 ``invalidated``。

    P6-B-01 引入。路由层翻译为 ``CHAPTER_UNBLOCK_INVALID_STATE (422)``。
    单章 unblock 仅对 ``invalidated`` 源有意义,其他状态本就可写。
    """

    def __init__(
        self,
        *,
        chapter_id: str,
        current_status: str,
    ) -> None:
        self.chapter_id = chapter_id
        self.current_status = current_status
        super().__init__(
            f"章节状态非 invalidated 不可 unblock: chapter_id={chapter_id} "
            f"current_status={current_status!r}"
        )


# =============================================================================
# 状态机集中表(对齐 P5-B-07 _CARD_TRANSITION_RULES 模式)
# =============================================================================


_REWIND_ALLOWED_SOURCES: tuple[str, ...] = (
    "reviewing",
    "rewriting",
    "revising",
    "approved",
    "published",
    "human_intervention",
)
"""``rewind`` 端点合法源状态(P6-B-01)。

不在列表:``drafting``(未写无需 rewind)/ ``invalidated``(已被上游 rewind
影响,需先 unblock 再决定)。
"""


_FORCE_PASS_ALLOWED_SOURCES: tuple[str, ...] = (
    "drafting",
    "reviewing",
    "rewriting",
    "revising",
    "human_intervention",
)
"""``force-pass`` 端点合法源状态(P6-B-01)。

不在列表:``approved`` / ``published``(已通过,无需 force)/ ``invalidated``
(上游 stale 需先 unblock)。
"""


# =============================================================================
# 出参 Pydantic 模型
# =============================================================================


class RewindResult(BaseModel):
    """``rewind`` 返结果(API_CONTRACT §3 SuccessEnvelope.data 段)。"""

    model_config = ConfigDict(extra="forbid")

    chapter_id: str = Field(..., description="被 rewind 的目标章节 ID")
    from_status: str = Field(..., description="rewind 前的 status")
    to_status: str = Field(
        default="drafting",
        description="rewind 后的 status(固定 drafting,回到未写状态)",
    )
    invalidated_chapter_ids: list[str] = Field(
        ...,
        description="被翻成 invalidated 的后续章节 chapter_id 列表(已排序)",
    )
    vector_revoked_count: int = Field(
        ...,
        ge=0,
        description=(
            "Chroma 软删除条目数(命中 ``metadata.chapter_id == target_id``"
            "的向量条目数;0 = 无注入 vector_store_factory 或确实无命中)"
        ),
    )
    extracted_revoked_count: int = Field(
        ...,
        ge=0,
        description=(
            "SQLite ``characters`` / ``plot_threads`` / ``timeline_entries`` "
            "三表合计 ``revoked_at`` 翻 now 行数"
        ),
    )


class ForcePassResult(BaseModel):
    """``force-pass`` 返结果。"""

    model_config = ConfigDict(extra="forbid")

    chapter_id: str
    from_status: str
    to_status: str = Field(default="approved")
    force_pass_at: datetime
    force_pass_reason: str


class ContentUpdateResult(BaseModel):
    """``update_content_manual`` 返结果(v1.1 acceptance 修复2)。"""

    model_config = ConfigDict(extra="forbid")

    chapter_id: str
    version_id: int
    word_count: int


class UnblockResult(BaseModel):
    """``unblock`` 返结果(单章)。"""

    model_config = ConfigDict(extra="forbid")

    chapter_id: str
    from_status: str = Field(default="invalidated")
    to_status: str = Field(default="drafting")


class UnblockAllResult(BaseModel):
    """``unblock-all`` 返结果(workspace 批量)。"""

    model_config = ConfigDict(extra="forbid")

    workspace_id: str
    unblocked_chapter_ids: list[str] = Field(
        ...,
        description=(
            "本次被 unblock 的章节 chapter_id 列表(已排序);空 = 无 invalidated "
            "章节(200 幂等返回,对齐自决项 2 + P3-B-12 batch 端点模式)"
        ),
    )
    unblocked_count: int = Field(..., ge=0)


_STALE_RESETTABLE_STATUSES: tuple[str, ...] = (
    "reviewing",
    "rewriting",
    "revising",
)
"""``reset_stale`` 自愈扫描范围(v2 w1-s2):中断时会卡住的"流程中间态"。

- ``drafting`` 不扫:它本身就是可写起点,drafting → drafting 是 no-op
  (中断在 drafting 阶段的章无需重置,write 直接覆盖);
- ``human_intervention`` / ``invalidated`` 不扫:语义是"等人"(孤儿章走
  escalation 解决流程 / rewind 链走 unblock),自动重置会丢人工介入上下文;
- ``approved`` / ``published`` 终态不扫:已完成的章自愈无从谈起。
"""


class ResetStaleResult(BaseModel):
    """``reset_stale`` 返结果(workspace 批量,v2 w1-s2 中断章自愈)。"""

    model_config = ConfigDict(extra="forbid")

    workspace_id: str
    reset_chapter_ids: list[str] = Field(
        ...,
        description=(
            "本次被重置回 drafting 的章节 chapter_id 列表(已排序);空 = 无"
            "滞留非终态章(200 幂等返回,对齐 unblock-all 模式)或对账跳过"
            "(看 skipped_reason)"
        ),
    )
    reset_count: int = Field(..., ge=0)
    skipped_reason: str | None = Field(
        None,
        description=(
            "非 None = 本次扫描被对账跳过未执行:``active_pipeline_run``("
            "该 workspace 有 state=running 的 pipeline run,循环正在写章)/ "
            "``active_tasks``(task_registry 有 pending/running 任务,"
            "chapter:write 等 BackgroundTask 正在写)— 防误杀正在生成的章"
        ),
    )


# =============================================================================
# Service
# =============================================================================


class ChapterRewindService:
    """章节 rewind / force-pass / unblock 业务层(P6-B-01)。

    依赖:
    - ``workspace_service``:校验 workspace 存在 + 合并 ``ensure_workspace_orm``
      (满足 ChapterORM workspace_id FK)
    - ``session_factory``:开 ``data/novel.db`` Session(必须;无注入则
      所有方法抛 ``ChapterRewindServiceError``,对齐 P5-B-07 同源)
    - ``vector_store_factory``:可选;无注入 → Chroma soft-delete 跳过(
      ``vector_revoked_count=0``,生产路径必注入,测试可省)
    - ``ws_manager``:可选;无注入 → WS publish 静默跳过(对齐
      ``ChapterCardService._publish_card_event`` 模式)
    """

    def __init__(
        self,
        *,
        workspace_service: WorkspaceService,
        session_factory: SessionFactory | None = None,
        vector_store_factory: VectorStoreFactory | None = None,
        vector_count_auditor: VectorCountAuditor | None = None,
        ws_manager: "WebSocketManager | None" = None,
        llm_client: object | None = None,
        llm_model: str | None = None,
        router_factory: object | None = None,
        active_task_checker: Callable[[str], bool] | None = None,
        operation_lease_store: ChapterOperationLeaseStore | None = None,
    ) -> None:
        self._workspace_service = workspace_service
        self._session_factory = session_factory
        self._vector_store_factory = vector_store_factory
        self._vector_count_auditor = vector_count_auditor
        self._ws_manager = ws_manager
        # v1.0.x wave 4 session 3a:force_pass 路径接入 chapter_summary 抽取 hook
        # 所需的 LLM 依赖。``None`` → hook 静默跳过(CLI / 单测 legacy 兼容,
        # 沿用 ``_run_auto_backup`` / ``sync_meta`` 等"未注入即 disable"模式)。
        # ``object`` 签名规避顶层 import 循环(实际类型由 caller 注入)。
        self._llm_client = llm_client
        self._llm_model = llm_model
        self._router_factory = router_factory
        # v2 w1-s2:reset_stale 对账 task_registry 的注入点(api 层组装时给
        # ``task_registry.has_active_tasks``;services 层不直接 import api 层
        # 全局单例)。``None`` → 视为无活跃任务(单测默认可重置)。
        self._active_task_checker = active_task_checker
        self._operation_lease_store = operation_lease_store

    def _acquire_source_operation_in_session(
        self,
        session: Session,
        *,
        row: ChapterORM,
        operation: str,
        allow_completed_current: bool = False,
    ) -> ChapterOperationLease | None:
        """在同一 BEGIN IMMEDIATE 事务内让人工改源与 Worker/Pipeline 竞争。"""

        if self._operation_lease_store is None:
            return None
        try:
            return self._operation_lease_store.acquire_in_session(
                session,
                workspace_id=row.workspace_id,
                chapter_id=row.chapter_id,
                operation_kind=operation,
                source_version=row.current_version,
                task_id=f"task_{uuid4().hex}",
                run_id=None,
                allow_completed_current=allow_completed_current,
            )
        except (ChapterOperationLeaseConflict, ChapterOperationLeaseError) as exc:
            raise ChapterRewindServiceError(str(exc)) from exc

    def _finish_source_operation(
        self, lease: ChapterOperationLease | None
    ) -> None:
        """全部副作用完成后写 durable terminal，再精确释放。"""

        if lease is None or self._operation_lease_store is None:
            return
        completed = self._operation_lease_store.mark_phase(
            lease,
            phase="operation_complete",
        )
        self._operation_lease_store.release(completed)

    def _attempt_scope_for_operation(
        self, lease: ChapterOperationLease | None
    ):  # type: ignore[no-untyped-def]
        """让 force-pass Provider 调用继承暂停 run 的原 cap 与账本。"""

        if lease is None or self._session_factory is None:
            return nullcontext()
        provider_call_cap = DEFAULT_PROVIDER_CALL_HARD_CAP
        if lease.run_id is not None:
            session = self._session_factory(lease.workspace_id)
            try:
                run = session.get(PipelineRunORM, lease.run_id)
                if run is None or run.workspace_id != lease.workspace_id:
                    raise ChapterRewindServiceError(
                        "force-pass 租约绑定的 pipeline run 已丢失"
                    )
                provider_call_cap = run.provider_call_cap
            finally:
                session.close()
        return bind_attempt_scope(
            workspace_id=lease.workspace_id,
            run_id=lease.run_id,
            task_id=lease.task_id,
            chapter_id=lease.chapter_id,
            provider_call_cap=provider_call_cap,
        )

    @staticmethod
    def _require_source_mutation_safe(
        *, row: ChapterORM, operation: str
    ) -> None:
        """改正文/版本/终稿前拦 running、坏 shape 与隔离未完成现场。"""

        try:
            validate_memory_enrichment_checkpoint_shape(
                status=row.memory_enrichment_status,
                details=row.memory_enrichment_details,
                enriched_at=row.memory_enriched_at,
            )
        except MemoryEnrichmentCheckpointShapeError as exc:
            raise ChapterRewindServiceError(
                f"{operation} 遇到损坏的富记忆 checkpoint，已保留现场: "
                f"chapter={row.chapter_id}: {exc}"
            ) from exc
        if row.memory_enrichment_status == "running":
            raise ChapterRewindServiceError(
                f"{operation} 遇到仍在 running 的富记忆 Worker，禁止并发改源: "
                f"chapter={row.chapter_id}"
            )
        if memory_enrichment_requires_manual_recovery(
            status=row.memory_enrichment_status,
            details=row.memory_enrichment_details,
        ):
            raise ChapterRewindServiceError(
                f"{operation} 遇到 blocked_uncertain 富记忆现场，禁止改源: "
                f"chapter={row.chapter_id}"
            )

    @staticmethod
    def _rewind_bound_pipeline_progress_in_session(
        session: Session,
        *,
        lease: ChapterOperationLease | None,
        target_chapter_id: str,
        affected_chapter_ids: list[str],
    ) -> None:
        """暂停 run 回退 current 已完成章时，同事务撤回 completed 进度。"""

        if lease is None or lease.run_id is None:
            return
        run = session.get(PipelineRunORM, lease.run_id)
        if (
            run is None
            or run.workspace_id != lease.workspace_id
            or run.state not in ("paused", "interrupted")
            or run.current_chapter != target_chapter_id
        ):
            raise ChapterRewindServiceError(
                "rewind 绑定的暂停 run checkpoint 已漂移，禁止撤回完成进度: "
                f"run_id={lease.run_id} chapter={target_chapter_id}"
            )
        if target_chapter_id in run.skipped_chapter_ids:
            raise ChapterRewindServiceError(
                "rewind 不得把 skipped current 冒充 completed 修复: "
                f"run_id={lease.run_id} chapter={target_chapter_id}"
            )
        affected = set(affected_chapter_ids)
        if target_chapter_id not in run.completed_chapter_ids:
            return
        run.completed_chapter_ids = [
            item for item in run.completed_chapter_ids if item not in affected
        ]
        run.skipped_chapter_ids = [
            item for item in run.skipped_chapter_ids if item not in affected
        ]
        run.completed_at = None
        run.updated_at = datetime.now(UTC)

    @staticmethod
    def _claim_source_mutation_row(
        *,
        session: Session,
        row: ChapterORM,
        target_status: str,
        clear_approved_at: bool,
        operation: str,
    ) -> None:
        """用原行完整水位做条件 UPDATE；与 Worker acquire 只能一方成功。"""

        original_ready_at = row.ready_for_next_chapter_at
        original_memory_status = row.memory_enrichment_status
        original_memory_details = row.memory_enrichment_details
        original_enriched_at = row.memory_enriched_at
        stmt = (
            update(ChapterORM)
            .where(ChapterORM.chapter_id == row.chapter_id)
            .where(ChapterORM.workspace_id == row.workspace_id)
            .where(ChapterORM.current_version == row.current_version)
            .where(ChapterORM.status == row.status)
            .where(ChapterORM.rewrite_count == row.rewrite_count)
            .where(ChapterORM.revise_count == row.revise_count)
            .where(ChapterORM.memory_enrichment_status == original_memory_status)
        )
        if original_ready_at is None:
            stmt = stmt.where(ChapterORM.ready_for_next_chapter_at.is_(None))
        else:
            stmt = stmt.where(
                ChapterORM.ready_for_next_chapter_at == original_ready_at
            )
        if original_memory_details is None:
            # ORM 的 Python None 通常持久化成 JSON 文本 ``null``，但历史库
            # 也可能留有 SQL NULL；两者都是同一业务值，CAS 同时兼容。
            stmt = stmt.where(
                or_(
                    ChapterORM.memory_enrichment_details.is_(None),
                    ChapterORM.memory_enrichment_details == JSON.NULL,
                )
            )
        else:
            stmt = stmt.where(
                ChapterORM.memory_enrichment_details == original_memory_details
            )
        if original_enriched_at is None:
            stmt = stmt.where(ChapterORM.memory_enriched_at.is_(None))
        else:
            stmt = stmt.where(ChapterORM.memory_enriched_at == original_enriched_at)
        values: dict[str, Any] = {
            "status": target_status,
            "ready_for_next_chapter_at": None,
            "memory_enrichment_status": "pending",
            "memory_enriched_at": None,
            "memory_enrichment_details": None,
        }
        if operation == "rewind" and row.current_version > 0:
            values["escalation_details"] = {
                "reason": REWIND_REWRITE_REQUIRED_REASON,
                "source_version_id": row.current_version,
                "source_status": row.status,
                "marked_at": datetime.now(UTC).isoformat(),
            }
        if clear_approved_at:
            values["approved_at"] = None
        result = session.execute(stmt.values(**values))
        if result.rowcount != 1:
            raise ChapterRewindServiceError(
                f"{operation} 未取得章节原子改源权，可能已有富记忆 Worker "
                f"或并发操作抢占: chapter={row.chapter_id}"
            )

    # ---- 公共入口:rewind --------------------------------------------------

    def rewind(self, workspace_id: str, chapter_id: str) -> RewindResult:
        """rewind 目标章节 + 标废后续章节 + 记忆库回退。

        flow:
        1. 校验 workspace + 章节 ORM 行(无行 → 既有 ``ChapterServiceError``
           "不存在" 翻译路径)
        2. 校验 ``current_status in _REWIND_ALLOWED_SOURCES``
        3. 开 session,事务内更新 3 类:
           - target ChapterORM:status='drafting', approved_at=None
           - 后续 ChapterORM rows (chapter_id > target_id):status='invalidated'
           - characters/plot_threads/timeline_entries: revoked_at=now
        4. commit
        5. Chroma soft-delete；生产 lease 路径失败时保留恢复 checkpoint
        6. WS publish ``chapter_rewound`` + ``chapter_invalidated`` (每章)
        """
        self._workspace_service.info(workspace_id)  # 抛 MetaFileError 翻 422
        if self._session_factory is None:
            raise ChapterRewindServiceError(
                "rewind 需注入 session_factory"
            )

        try:
            outline_entity_baselines = load_outline_entity_baselines(
                workspaces_root=self._workspace_service.workspaces_root,
                workspace_id=workspace_id,
            )
        except OutlineEntityRecoveryError as exc:
            raise ChapterRewindServiceError(str(exc)) from exc

        now = datetime.now(UTC)
        invalidated_ids: list[str] = []
        extracted_revoked = 0
        from_status: str
        # v1.0.x A2-03:在 try 外预先定义,commit 后用于 meta.json sync
        total_loss = 0
        operation_lease: ChapterOperationLease | None = None

        session = self._session_factory(workspace_id)
        try:
            if self._operation_lease_store is not None:
                self._operation_lease_store.begin_immediate(session)
            row = session.get(ChapterORM, chapter_id)
            if row is None or row.workspace_id != workspace_id:
                raise ChapterRewindServiceError(
                    f"章节不存在: workspace={workspace_id} chapter={chapter_id}"
                )
            from_status = row.status
            if from_status not in _REWIND_ALLOWED_SOURCES:
                raise ChapterRewindInvalidStateError(
                    chapter_id=chapter_id,
                    current_status=from_status,
                    allowed_sources=_REWIND_ALLOWED_SOURCES,
                )
            # 先把 rewind 影响范围全部读出并验真；任一章仍有富记忆 Worker
            # 或隔离未完成，整笔事务在任何状态/记忆撤销前硬停。
            retained_lease_ids = set(
                session.scalars(
                    select(ChapterOperationLeaseORM.chapter_id).where(
                        ChapterOperationLeaseORM.workspace_id == workspace_id
                    )
                )
            )
            stmt = (
                select(ChapterORM)
                .where(ChapterORM.workspace_id == workspace_id)
                .where(ChapterORM.chapter_id > chapter_id)
                .order_by(ChapterORM.chapter_id)
            )
            subsequent_rows = list(session.execute(stmt).scalars())
            for affected in [row, *subsequent_rows]:
                if affected.chapter_id in retained_lease_ids:
                    raise ChapterRewindServiceError(
                        "rewind 影响范围存在未收口章节操作租约，已保留现场: "
                        f"chapter={affected.chapter_id}"
                    )
                self._require_source_mutation_safe(
                    row=affected,
                    operation="rewind",
                )
            operation_lease = self._acquire_source_operation_in_session(
                session,
                row=row,
                operation="rewind",
                allow_completed_current=True,
            )
            self._rewind_bound_pipeline_progress_in_session(
                session,
                lease=operation_lease,
                target_chapter_id=chapter_id,
                affected_chapter_ids=[
                    chapter_id,
                    *(item.chapter_id for item in subsequent_rows),
                ],
            )

            # 1. 目标章节回退。条件 UPDATE 与富记忆 pending->running 抢占
            # 同一行；谁先赢，另一方 rowcount 必为 0。
            # A-03 hot-fix:from_status in {approved, published} → 计数 -1
            target_approved_loss = (
                1 if from_status in ("approved", "published") else 0
            )
            self._claim_source_mutation_row(
                session=session,
                row=row,
                target_status="drafting",
                clear_approved_at=True,
                operation="rewind",
            )

            # 2. 后续章节标废(chapter_id > target_id 同 workspace)
            #    A-03 hot-fix:统计被标 invalidated 之前在 approved/published 的
            #    数量(作为 workspace.completed_chapters 的额外减计数)
            subsequent_approved_loss = 0
            for subsequent in subsequent_rows:
                if subsequent.status != "invalidated":
                    if subsequent.status in ("approved", "published"):
                        subsequent_approved_loss += 1
                self._claim_source_mutation_row(
                    session=session,
                    row=subsequent,
                    target_status="invalidated",
                    clear_approved_at=False,
                    operation="rewind",
                )
                invalidated_ids.append(subsequent.chapter_id)

            # A-03 hot-fix:同事务减 workspace.completed_chapters(floor 0)
            total_loss = target_approved_loss + subsequent_approved_loss
            if total_loss > 0:
                ws_row = session.get(WorkspaceORM, workspace_id)
                if ws_row is not None:
                    ws_row.completed_chapters = max(
                        0, ws_row.completed_chapters - total_loss
                    )

            # 3. extracted_* 三表 revoked_at 写入(按 chapter_id 关联)
            restored_outline_entities = restore_rewound_outline_entities_in_session(
                session,
                workspace_id=workspace_id,
                chapter_id=chapter_id,
                baselines=outline_entity_baselines,
            )
            character_revoke_stmt = (
                update(CharacterORM)
                .where(CharacterORM.workspace_id == workspace_id)
                .where(CharacterORM.first_appearance_chapter == chapter_id)
                .where(CharacterORM.revoked_at.is_(None))
            )
            if outline_entity_baselines.characters:
                character_revoke_stmt = character_revoke_stmt.where(
                    CharacterORM.character_id.not_in(
                        tuple(outline_entity_baselines.characters)
                    )
                )
            extracted_revoked += session.execute(
                character_revoke_stmt.values(revoked_at=now)
            ).rowcount

            plot_revoke_stmt = (
                update(PlotThreadORM)
                .where(PlotThreadORM.workspace_id == workspace_id)
                .where(PlotThreadORM.planted_chapter == chapter_id)
                .where(PlotThreadORM.revoked_at.is_(None))
            )
            if outline_entity_baselines.plot_threads:
                plot_revoke_stmt = plot_revoke_stmt.where(
                    PlotThreadORM.thread_id.not_in(
                        tuple(outline_entity_baselines.plot_threads)
                    )
                )
            extracted_revoked += session.execute(
                plot_revoke_stmt.values(revoked_at=now)
            ).rowcount
            extracted_revoked += session.execute(
                update(TimelineEntryORM)
                .where(TimelineEntryORM.workspace_id == workspace_id)
                .where(TimelineEntryORM.chapter_id == chapter_id)
                .where(TimelineEntryORM.revoked_at.is_(None))
                .values(revoked_at=now)
            ).rowcount
            target_seq = chapter_seq_from_id(chapter_id)
            if target_seq is None:
                raise ChapterRewindServiceError(
                    f"rewind 无法解析规范 chapter_id: {chapter_id!r}"
                )
            CoreStateService.retract_from_chapter_in_session(
                session,
                workspace_id=workspace_id,
                target_chapter_seq=target_seq,
                affected_chapter_ids=[
                    chapter_id,
                    *(item.chapter_id for item in subsequent_rows),
                ],
                now=now,
            )
            if (
                restored_outline_entities.character_ids
                or restored_outline_entities.plot_thread_ids
            ):
                _log.info(
                    "rewind_outline_entities_restored_to_baseline",
                    workspace_id=workspace_id,
                    chapter_id=chapter_id,
                    character_ids=list(restored_outline_entities.character_ids),
                    plot_thread_ids=list(
                        restored_outline_entities.plot_thread_ids
                    ),
                )

            if operation_lease is not None and self._operation_lease_store is not None:
                operation_lease = self._operation_lease_store.mark_phase_in_session(
                    session,
                    operation_lease,
                    phase="source_mutation_committed",
                )
            session.commit()
        except Exception:
            session.rollback()
            raise
        finally:
            session.close()

        # v1.0.x A2-03 续修:ORM commit 成功后同步 meta.json completed_chapters
        # (total_loss 在 try 块内已计算,commit 成功后必有值)
        if total_loss > 0:
            self._workspace_service.sync_completed_chapters_to_meta(
                workspace_id, delta=-total_loss
            )

        # 4. Chroma soft-delete。生产 durable lease 路径只有这一步成功后才可
        # 发布完成并释放；失败保留 source_mutation_committed 供冷启动幂等补齐。
        vector_revoked_count = self._soft_delete_chroma(
            workspace_id=workspace_id,
            chapter_id=chapter_id,
            require_success=operation_lease is not None,
        )

        # 5. WS publish
        self._publish_chapter_event(
            chapter_id=chapter_id,
            event="chapter_rewound",
            data={
                "chapter_id": chapter_id,
                "workspace_id": workspace_id,
                "from_status": from_status,
                "to_status": "drafting",
                "invalidated_chapter_ids": invalidated_ids,
                "vector_revoked_count": vector_revoked_count,
                "extracted_revoked_count": extracted_revoked,
            },
        )
        for invalidated_id in invalidated_ids:
            self._publish_chapter_event(
                chapter_id=invalidated_id,
                event="chapter_invalidated",
                data={
                    "chapter_id": invalidated_id,
                    "workspace_id": workspace_id,
                    "trigger_chapter_id": chapter_id,
                    "to_status": "invalidated",
                },
            )

        _log.info(
            "chapter_rewound",
            workspace_id=workspace_id,
            chapter_id=chapter_id,
            from_status=from_status,
            invalidated_count=len(invalidated_ids),
            vector_revoked=vector_revoked_count,
            extracted_revoked=extracted_revoked,
        )

        self._finish_source_operation(operation_lease)

        return RewindResult(
            chapter_id=chapter_id,
            from_status=from_status,
            to_status="drafting",
            invalidated_chapter_ids=invalidated_ids,
            vector_revoked_count=vector_revoked_count,
            extracted_revoked_count=extracted_revoked,
        )

    # ---- 公共入口:force-pass ----------------------------------------------

    def force_pass(
        self,
        workspace_id: str,
        chapter_id: str,
        *,
        reason: str,
    ) -> ForcePassResult:
        """强制审稿通过(跳过 Reviewer)。

        - 校验源状态在 ``_FORCE_PASS_ALLOWED_SOURCES`` 内,否则抛
          ``ChapterRewindServiceError``(路由翻译 CHAPTER_INVALID_STATE 409,
          复用既有错误码;不增独立 ``CHAPTER_FORCE_PASS_INVALID_STATE``,
          对齐 P5-B-09 ``VolumeUpdateInvalid`` 复用既有 422 同模式)。
        - 行不存在 → ``ChapterRewindServiceError`` (翻译 CHAPTER_NOT_FOUND 404)。
        - ``invalidated`` 源 → 抛 ``ChapterInvalidatedNeedsUnblockError``
          (路由翻 CHAPTER_INVALIDATED_NEEDS_UNBLOCK 409),引导先 unblock。
        - 成功:status='approved', force-pass 审计字段持久化，final 与同步
          连续性小账本校验通过，并写 ``ready_for_next_chapter_at``。
        """
        self._workspace_service.info(workspace_id)
        if self._session_factory is None:
            raise ChapterRewindServiceError(
                "force_pass 需注入 session_factory"
            )

        now = datetime.now(UTC)
        from_status: str
        operation_lease: ChapterOperationLease | None = None

        session = self._session_factory(workspace_id)
        try:
            if self._operation_lease_store is not None:
                self._operation_lease_store.begin_immediate(session)
            row = session.get(ChapterORM, chapter_id)
            if row is None or row.workspace_id != workspace_id:
                raise ChapterRewindServiceError(
                    f"章节不存在: workspace={workspace_id} chapter={chapter_id}"
                )
            from_status = row.status
            if from_status == "invalidated":
                raise ChapterInvalidatedNeedsUnblockError(
                    chapter_id=chapter_id,
                    workspace_id=workspace_id,
                )
            if from_status not in _FORCE_PASS_ALLOWED_SOURCES:
                raise ChapterRewindServiceError(
                    f"章节状态不允许 force-pass: chapter_id={chapter_id} "
                    f"current_status={from_status!r} "
                    f"allowed_sources={list(_FORCE_PASS_ALLOWED_SOURCES)!r}"
                )
            self._require_source_mutation_safe(row=row, operation="force-pass")
            operation_lease = self._acquire_source_operation_in_session(
                session,
                row=row,
                operation="force-pass",
            )
            current_version_for_final = row.current_version
            # 这里只提交互斥租约，不提前把章节写成 approved。final/ledger 任一
            # 失败时源章仍保持原人工状态，可在严格 artifact 对账后重进；避免
            # “已 approved 但 force-pass 入口和 checkpoint resumer 都不再接”
            # 的永久死锁。
            session.commit()
        except Exception:
            session.rollback()
            raise
        finally:
            session.close()

        # R4:force-pass 与 reviewer-pass 共用同一硬水位语义。先准备 final 与
        # 已验证 ledger；最后在单一 DB 事务里同时落 approved + ready + phase。
        paths = WorkspacePaths(
            workspace_id,
            workspaces_root=self._workspace_service.workspaces_root,
        )
        try:
            versioning = ChapterVersioning(paths)
            final_path = versioning.mark_final(
                chapter_id, current_version_for_final
            )
            _log.info(
                "force_pass_mark_final_done",
                workspace_id=workspace_id,
                chapter_id=chapter_id,
                version_id=current_version_for_final,
            )
        except ChapterVersioningError as exc:
            raise ForcePassContinuityLedgerNotReadyError(
                chapter_id=chapter_id,
                reason=f"final 版本标定失败: {exc}",
            ) from exc

        if operation_lease is not None and self._operation_lease_store is not None:
            operation_lease = self._operation_lease_store.mark_phase(
                operation_lease,
                phase="continuity_ledger_may_call_provider",
            )

        try:
            try:
                ledger = load_validated_continuity_ledger(
                    workspace_id=workspace_id,
                    chapter_id=chapter_id,
                    paths=paths,
                    session_factory=self._session_factory,
                    expected_version_id=current_version_for_final,
                )
            except ContinuityLedgerValidationError:
                with self._attempt_scope_for_operation(operation_lease):
                    router = (
                        self._router_factory(workspace_id)
                        if self._router_factory is not None
                        else None
                    )
                    if router is not None:
                        session_factory_local = self._session_factory
                        router = wrap_router_with_cost_tracking(
                            router,  # type: ignore[arg-type]
                            workspace_id=workspace_id,
                            chapter_id=chapter_id,
                            session_factory=lambda: session_factory_local(
                                workspace_id
                            ),
                        )
                    ledger = extract_and_persist_continuity_ledger(
                        workspace_id=workspace_id,
                        chapter_id=chapter_id,
                        source_version_id=current_version_for_final,
                        paths=paths,
                        session_factory=self._session_factory,
                        llm_client=self._llm_client,  # type: ignore[arg-type]
                        llm_model=self._llm_model,
                        router=router,  # type: ignore[arg-type]
                        trigger="force_pass",
                    )
        except Exception as exc:
            raise ForcePassContinuityLedgerNotReadyError(
                chapter_id=chapter_id,
                reason=(
                    str(exc)
                    if isinstance(exc, ContinuityLedgerValidationError)
                    else f"{type(exc).__name__}: {exc}"
                ),
            ) from exc

        if operation_lease is not None and self._operation_lease_store is not None:
            operation_lease = self._operation_lease_store.mark_phase(
                operation_lease,
                phase="continuity_ledger_persisted",
            )

        # 卡片存在时，force-pass 与普通 reviewer-pass 共用完全相同的核心
        # 合同；旧测试/历史孤儿章可能没有卡片，按 legacy v0 保留兼容。
        core_card: ChapterCard | None = None
        card_path = paths.cards_dir / f"{chapter_id}.json"
        if card_path.is_file():
            try:
                core_card = ChapterCard.model_validate_json_storage(
                    card_path.read_text(encoding="utf-8")
                )
            except Exception as exc:  # noqa: BLE001 — 损坏卡片必须关闭 ready
                raise ForcePassContinuityLedgerNotReadyError(
                    chapter_id=chapter_id,
                    reason=f"核心状态章节卡不可验证: {type(exc).__name__}: {exc}",
                ) from exc
            if (
                core_card.workspace_id != workspace_id
                or core_card.chapter_id != chapter_id
            ):
                raise ForcePassContinuityLedgerNotReadyError(
                    chapter_id=chapter_id,
                    reason="核心状态章节卡身份与 force-pass 目标不一致",
                )

        # 第一笔事务只落 approved/audit。核心账本要求来源章节已经 approved，
        # 但 ready 仍保持关闭；崩在这里可由 approved checkpoint 原样续接，
        # 不会重放 Writer/Reviewer/已成功的小账本抽取。
        session = self._session_factory(workspace_id)
        try:
            if self._operation_lease_store is not None:
                self._operation_lease_store.begin_immediate(session)
            row = session.get(ChapterORM, chapter_id)
            if (
                row is None
                or row.workspace_id != workspace_id
                or row.status != from_status
                or row.current_version != current_version_for_final
            ):
                raise ForcePassContinuityLedgerNotReadyError(
                    chapter_id=chapter_id,
                    reason="ready 写入前章节源状态或版本已发生变化",
                )
            self._require_source_mutation_safe(row=row, operation="force-pass")
            self._claim_source_mutation_row(
                session=session,
                row=row,
                target_status="approved",
                clear_approved_at=False,
                operation="force-pass",
            )
            row.approved_at = now
            row.force_pass_at = now
            row.force_pass_reason = reason
            row.ready_for_next_chapter_at = None
            row.memory_enrichment_status = "pending"
            row.memory_enriched_at = None
            row.memory_enrichment_details = None
            # A-03:源状态白名单排除 approved/published，本次落 approved 必为
            # 首次；和 approved checkpoint 同事务更新完成数。
            ws_row = session.get(WorkspaceORM, workspace_id)
            if ws_row is not None:
                ws_row.completed_chapters = ws_row.completed_chapters + 1
            session.commit()
        except Exception as exc:
            session.rollback()
            if isinstance(exc, ForcePassContinuityLedgerNotReadyError):
                raise
            raise ForcePassContinuityLedgerNotReadyError(
                chapter_id=chapter_id,
                reason=f"ready 水位持久化失败: {type(exc).__name__}: {exc}",
            ) from exc
        finally:
            session.close()

        # approved 计数先同步；即使核心提交失败，DB/文件两侧都准确表示
        # “正文已批准但尚不可生成下一章”的可恢复 checkpoint。
        self._workspace_service.sync_completed_chapters_to_meta(
            workspace_id, delta=1
        )

        core_state_commit_hash = ""
        if (
            core_card is not None
            and core_card.core_state_contract_version == 1
        ):
            try:
                core_state_commit_hash = CoreStateService(
                    session_factory=self._session_factory
                ).commit_approved_chapter(
                    workspace_id=workspace_id,
                    card=core_card,
                    source_version_id=current_version_for_final,
                    paths=paths,
                ).commit_hash
            except Exception as exc:  # noqa: BLE001 — 核心账本失败必须关闭 ready
                raise ForcePassContinuityLedgerNotReadyError(
                    chapter_id=chapter_id,
                    reason=f"核心状态提交未闭合: {type(exc).__name__}: {exc}",
                ) from exc

        ready_at = datetime.now(UTC)
        session = self._session_factory(workspace_id)
        try:
            if self._operation_lease_store is not None:
                self._operation_lease_store.begin_immediate(session)
            row = session.get(ChapterORM, chapter_id)
            if (
                row is None
                or row.workspace_id != workspace_id
                or row.status != "approved"
                or row.current_version != current_version_for_final
                or row.force_pass_at is None
                or row.force_pass_reason != reason
                or row.ready_for_next_chapter_at is not None
            ):
                raise ForcePassContinuityLedgerNotReadyError(
                    chapter_id=chapter_id,
                    reason="核心提交后 ready 写入前章节 checkpoint 已发生变化",
                )
            row.ready_for_next_chapter_at = ready_at
            row.memory_enrichment_status = "pending"
            row.memory_enriched_at = None
            row.memory_enrichment_details = {
                "schema_version": 1,
                "ready_at": ready_at.isoformat(),
                "final_version_id": current_version_for_final,
                "final_path": str(final_path),
                "continuity_ledger_hash": ledger.ledger_hash,
                "continuity_ledger_source_text_hash": ledger.source_text_hash,
                "core_state_commit_hash": core_state_commit_hash,
                "trigger": "force_pass",
            }
            if operation_lease is not None and self._operation_lease_store is not None:
                operation_lease = self._operation_lease_store.mark_phase_in_session(
                    session,
                    operation_lease,
                    phase="ready_persisted",
                )
            session.commit()
        except Exception as exc:
            session.rollback()
            if isinstance(exc, ForcePassContinuityLedgerNotReadyError):
                raise
            raise ForcePassContinuityLedgerNotReadyError(
                chapter_id=chapter_id,
                reason=f"ready 水位持久化失败: {type(exc).__name__}: {exc}",
            ) from exc
        finally:
            session.close()

        # force-pass 不经过 orchestrator 的 approved 返回链，但必须消费上面
        # 同事务写下的 pending checkpoint。统一 Worker 会先 CAS 为 running，
        # 再按成功/已知失败/未知副作用写明确终态；进程崩溃则让 cold-start
        # scanner 从 running 现场审计，绝不能回到旧的“直接抽取后吞异常”。
        with self._attempt_scope_for_operation(operation_lease):
            self._run_force_pass_memory_enrichment_checkpoint(
                workspace_id,
                chapter_id,
                final_version_id=current_version_for_final,
            )

        self._publish_chapter_event(
            chapter_id=chapter_id,
            event="chapter_force_passed",
            data={
                "chapter_id": chapter_id,
                "workspace_id": workspace_id,
                "from_status": from_status,
                "to_status": "approved",
                "force_pass_at": now.isoformat(),
                "force_pass_reason": reason,
            },
        )

        _log.info(
            "chapter_force_passed",
            workspace_id=workspace_id,
            chapter_id=chapter_id,
            from_status=from_status,
            reason_length=len(reason),
        )

        self._finish_source_operation(operation_lease)

        return ForcePassResult(
            chapter_id=chapter_id,
            from_status=from_status,
            to_status="approved",
            force_pass_at=now,
            force_pass_reason=reason,
        )

    # ---- 公共入口:unblock + unblock-all ----------------------------------

    def unblock(self, workspace_id: str, chapter_id: str) -> UnblockResult:
        """单章 unblock:``invalidated`` → ``drafting``。

        非 ``invalidated`` 源抛 ``ChapterUnblockInvalidStateError`` (422)。
        无 ORM 行抛 ``ChapterRewindServiceError`` (翻 CHAPTER_NOT_FOUND 404)。
        """
        self._workspace_service.info(workspace_id)
        if self._session_factory is None:
            raise ChapterRewindServiceError(
                "unblock 需注入 session_factory"
            )

        from_status: str
        session = self._session_factory(workspace_id)
        try:
            row = session.get(ChapterORM, chapter_id)
            if row is None or row.workspace_id != workspace_id:
                raise ChapterRewindServiceError(
                    f"章节不存在: workspace={workspace_id} chapter={chapter_id}"
                )
            from_status = row.status
            if from_status != "invalidated":
                raise ChapterUnblockInvalidStateError(
                    chapter_id=chapter_id,
                    current_status=from_status,
                )
            row.status = "drafting"
            session.commit()
        except Exception:
            session.rollback()
            raise
        finally:
            session.close()

        self._publish_chapter_event(
            chapter_id=chapter_id,
            event="chapter_unblocked",
            data={
                "chapter_id": chapter_id,
                "workspace_id": workspace_id,
                "from_status": from_status,
                "to_status": "drafting",
            },
        )

        _log.info(
            "chapter_unblocked",
            workspace_id=workspace_id,
            chapter_id=chapter_id,
        )

        return UnblockResult(
            chapter_id=chapter_id,
            from_status=from_status,
            to_status="drafting",
        )

    def unblock_all(self, workspace_id: str) -> UnblockAllResult:
        """批量 unblock 整个 workspace 的 ``invalidated`` 章节。

        - 空状态(workspace 内无 ``invalidated`` 章节)→ 200 + count=0
          + ``unblocked_chapter_ids=[]``(自决项 2 幂等,对齐 P3-B-12
          batch 端点模式)。
        - 非空 → 全部翻 ``drafting`` + WS publish ``chapter_unblocked``
          每章。
        """
        self._workspace_service.info(workspace_id)
        if self._session_factory is None:
            raise ChapterRewindServiceError(
                "unblock_all 需注入 session_factory"
            )

        unblocked_ids: list[str] = []
        session = self._session_factory(workspace_id)
        try:
            stmt = (
                select(ChapterORM)
                .where(ChapterORM.workspace_id == workspace_id)
                .where(ChapterORM.status == "invalidated")
                .order_by(ChapterORM.chapter_id)
            )
            for row in session.execute(stmt).scalars():
                row.status = "drafting"
                unblocked_ids.append(row.chapter_id)
            session.commit()
        except Exception:
            session.rollback()
            raise
        finally:
            session.close()

        for unblocked_id in unblocked_ids:
            self._publish_chapter_event(
                chapter_id=unblocked_id,
                event="chapter_unblocked",
                data={
                    "chapter_id": unblocked_id,
                    "workspace_id": workspace_id,
                    "from_status": "invalidated",
                    "to_status": "drafting",
                    "batch": True,
                },
            )

        _log.info(
            "chapter_unblock_all",
            workspace_id=workspace_id,
            unblocked_count=len(unblocked_ids),
        )

        return UnblockAllResult(
            workspace_id=workspace_id,
            unblocked_chapter_ids=unblocked_ids,
            unblocked_count=len(unblocked_ids),
        )

    def mark_running_memory_enrichment_as_failed_on_startup(
        self,
        workspace_id: str,
        *,
        apply: bool = True,
    ) -> list[str]:
        """审计后收口上个进程遗留的富记忆 ``running``。

        本入口只由 FastAPI lifespan 在**单进程、唯一后端的冷启动**时调用。
        在这个部署合同下，daemon memory Worker 不可能跨进程存活，因此严格
        合法的残留 ``running`` 是僵尸投影，不能继续对外宣称正在运行，也不能
        自动重放可能已有未知响应的 Provider 阶段。本扫描器不会探测另一个后端
        进程或活 Worker；若破坏唯一后端前提，不能据此把 ``running`` 判死。

        先审计本章可能由 MemoryExtractor 落下的 SQLite footprint，并严格统计
        本章 Chroma 向量。只有 SQLite footprint 为零、Chroma 本章向量为零且
        final/ledger 来源验真，才标记 ``safe_to_continue=true``，允许后续 Run
        走 ``final + ledger`` 降级。started Provider attempt 仍原样保留并禁止
        自动重放，但 unknown 远端结果本身不等于本地副作用；任一本地非零或来源
        不明都落 ``blocked_uncertain`` 并保留现场。
        """

        self._workspace_service.info(workspace_id)
        if self._session_factory is None:
            raise ChapterRewindServiceError(
                "memory enrichment startup recovery 需注入 session_factory"
            )

        now = datetime.now(UTC)
        interrupted_ids: list[str] = []
        paths = WorkspacePaths(
            workspace_id,
            workspaces_root=self._workspace_service.workspaces_root,
        )
        session = self._session_factory(workspace_id)
        try:
            rows = session.scalars(
                select(ChapterORM)
                .where(ChapterORM.workspace_id == workspace_id)
                .order_by(ChapterORM.chapter_id)
            ).all()
            for row in rows:
                try:
                    validate_memory_enrichment_checkpoint_shape(
                        status=row.memory_enrichment_status,
                        details=row.memory_enrichment_details,
                        enriched_at=row.memory_enriched_at,
                    )
                except MemoryEnrichmentCheckpointShapeError as exc:
                    _log.error(
                        "memory_enrichment_startup_checkpoint_corrupt",
                        workspace_id=workspace_id,
                        chapter_id=row.chapter_id,
                        memory_enrichment_status=row.memory_enrichment_status,
                        details_type=type(row.memory_enrichment_details).__name__,
                        reason=str(exc),
                        preserved_unchanged=True,
                    )
                    raise ChapterRewindServiceError(
                        "富记忆启动扫描发现损坏 checkpoint，已原样保留并阻止后端启动: "
                        f"chapter={row.chapter_id}: {exc}"
                    ) from exc
                if row.memory_enrichment_status != "running":
                    continue
                if (
                    row.status not in ("approved", "published")
                    or row.ready_for_next_chapter_at is None
                ):
                    _log.error(
                        "memory_enrichment_startup_shape_invalid",
                        workspace_id=workspace_id,
                        chapter_id=row.chapter_id,
                        chapter_status=row.status,
                        ready_for_next_chapter=(
                            row.ready_for_next_chapter_at is not None
                        ),
                    )
                    raise ChapterRewindServiceError(
                        "富记忆 running 与章节 terminal/ready 水位矛盾，"
                        f"已阻止后端启动: chapter={row.chapter_id}"
                    )
                # 上面的统一合同已经证明 running details 是 object、tasks
                # 是 list、started_at 非空；此处只复制，不做任何容错归一化。
                assert isinstance(row.memory_enrichment_details, dict)
                previous = dict(row.memory_enrichment_details)
                tasks = list(previous["tasks"])
                failures = list(previous.get("failures", []))
                source_provenance_verified = False
                source_provenance_error: str | None = None
                final_version_id = row.current_version
                final_path = paths.chapter_version_file(row.chapter_id, "final.md")
                ledger_hash: str | None = None
                ledger_source_text_hash: str | None = None
                try:
                    ledger = load_validated_continuity_ledger(
                        workspace_id=workspace_id,
                        chapter_id=row.chapter_id,
                        paths=paths,
                        session_factory=self._session_factory,
                        expected_version_id=final_version_id,
                    )
                    if not final_path.is_file():
                        raise ChapterRewindServiceError("final.md 不存在")
                    ledger_hash = ledger.ledger_hash
                    ledger_source_text_hash = ledger.source_text_hash
                    source_provenance_verified = True
                except Exception as exc:  # noqa: BLE001 — 证据不足必须硬拦
                    source_provenance_error = f"{type(exc).__name__}: {exc}"
                sqlite_snapshot = audit_sqlite_memory_snapshot(
                    session=session,
                    workspace_id=workspace_id,
                    row=row,
                )
                sqlite_footprint = sqlite_snapshot.footprint
                sqlite_digest = sqlite_snapshot.digest
                baseline_raw = previous.get("side_effect_baseline")
                sqlite_baseline: dict[str, int] | None = None
                sqlite_baseline_digest: str | None = None
                vector_baseline_count: int | None = None
                vector_baseline_digest: str | None = None
                if baseline_raw is not None:
                    if not isinstance(baseline_raw, dict):
                        raise ChapterRewindServiceError(
                            "富记忆 running.side_effect_baseline 形状损坏，"
                            f"已保留现场: chapter={row.chapter_id}"
                        )
                    try:
                        sqlite_baseline = validate_sqlite_memory_footprint(
                            baseline_raw.get("sqlite_footprint")
                        )
                    except ValueError as exc:
                        raise ChapterRewindServiceError(
                            "富记忆 running SQLite baseline 损坏，已保留现场: "
                            f"chapter={row.chapter_id}: {exc}"
                        ) from exc
                    lease_id = previous.get("lease_id")
                    sqlite_digest_raw = baseline_raw.get("sqlite_digest")
                    vector_digest_raw = baseline_raw.get("vector_digest")
                    if (
                        baseline_raw.get("schema_version") != 1
                        or not isinstance(lease_id, str)
                        or not lease_id
                        or baseline_raw.get("lease_id") != lease_id
                        or not self._is_sha256(sqlite_digest_raw)
                        or not self._is_sha256(vector_digest_raw)
                    ):
                        raise ChapterRewindServiceError(
                            "富记忆 running side-effect digest/lease 绑定损坏，"
                            f"已保留现场: chapter={row.chapter_id}"
                        )
                    sqlite_baseline_digest = sqlite_digest_raw
                    vector_baseline_digest = vector_digest_raw
                    vector_baseline_raw = baseline_raw.get("vector_count")
                    if (
                        type(vector_baseline_raw) is not int
                        or vector_baseline_raw < 0
                    ):
                        raise ChapterRewindServiceError(
                            "富记忆 running Chroma baseline 损坏，已保留现场: "
                            f"chapter={row.chapter_id}"
                        )
                    vector_baseline_count = vector_baseline_raw
                running_started_at = datetime.fromisoformat(
                    str(previous["started_at"])
                )
                try:
                    unconsumed_provider_attempts = (
                        load_unconsumed_memory_provider_attempts(
                            session,
                            workspace_id=workspace_id,
                            chapter_id=row.chapter_id,
                            anchor=running_started_at,
                        )
                    )
                except MemoryEnrichmentAttemptLedgerError as exc:
                    raise ChapterRewindServiceError(
                        "富记忆启动扫描发现 attempt 账本损坏，已原样保留并"
                        f"阻止后端启动: chapter={row.chapter_id}: {exc}"
                    ) from exc
                started_provider_attempt_ids = [
                    attempt["attempt_id"]
                    for attempt in unconsumed_provider_attempts
                    if attempt["status"] == "started"
                ]
                vector_count: int | None = None
                vector_digest: str | None = None
                vector_quarantined_count: int | None = None
                vector_quarantine_error: str | None = None
                try:
                    if self._vector_count_auditor is not None:
                        vector_audit = self._vector_count_auditor(
                            workspace_id,
                            row.chapter_id,
                        )
                        if type(vector_audit) is int:
                            vector_count = vector_audit
                        else:
                            vector_count = vector_audit.count
                            vector_digest = vector_audit.digest
                    elif self._vector_store_factory is None:
                        raise ChapterRewindServiceError(
                            "富记忆冷启动恢复缺少 vector_store_factory，"
                            "无法证明 Chroma 已隔离"
                        )
                    else:
                        vector_store = self._vector_store_factory(workspace_id)
                        vector_snapshot = vector_store.snapshot_by_chapter_id(
                            DEFAULT_COLLECTION,
                            chapter_id=row.chapter_id,
                        )
                        vector_count = vector_snapshot.count
                        vector_digest = vector_snapshot.digest
                    if baseline_raw is None and vector_count != 0:
                        raise ChapterRewindServiceError(
                            "Chroma 存在本章向量，无法证明中断 Worker 零副作用；"
                            f"已保留现场: count={vector_count}"
                        )
                    if baseline_raw is not None and not self._is_sha256(
                        vector_digest
                    ):
                        raise ChapterRewindServiceError(
                            "Chroma 内容摘要缺失，无法证明中断 Worker无新增副作用"
                        )
                    vector_quarantined_count = 0
                except Exception as exc:  # noqa: BLE001 — 失败需结构化硬拦
                    vector_quarantine_error = f"{type(exc).__name__}: {exc}"

                if sqlite_baseline is None:
                    # 兼容本修复前已持久化的 running（含 live R8 ch3）：没有
                    # baseline 时只能接受当前全零，绝不反推已有记录是合法旧值。
                    sqlite_side_effect_count = sum(sqlite_footprint.values())
                    vector_side_effect_count = vector_count
                    recovery_disposition = (
                        "safe_degraded_no_sqlite_or_vector_persist"
                    )
                else:
                    sqlite_side_effect_count = sum(
                        abs(sqlite_footprint[field] - sqlite_baseline[field])
                        for field in sqlite_footprint
                    )
                    if sqlite_digest != sqlite_baseline_digest:
                        sqlite_side_effect_count += 1
                    assert vector_baseline_count is not None
                    vector_side_effect_count = (
                        None
                        if vector_count is None
                        else abs(vector_count - vector_baseline_count)
                    )
                    if (
                        vector_side_effect_count is not None
                        and vector_digest != vector_baseline_digest
                    ):
                        vector_side_effect_count += 1
                    recovery_disposition = (
                        "safe_degraded_no_new_sqlite_or_vector_persist"
                    )
                safe_to_continue = (
                    sqlite_side_effect_count == 0
                    and vector_side_effect_count == 0
                    and vector_quarantine_error is None
                    and source_provenance_verified
                )
                failures.append(
                    {
                        "stage": "memory_enrichment_task",
                        "error_type": "EngineRestartedMidMemoryEnrichment",
                        "result_unknown": True,
                        "message": (
                            "后端进程在富记忆任务终态落库前结束；保留 Provider "
                            "attempt 现场，禁止自动重放未知请求"
                        ),
                    }
                )
                ready_at = row.ready_for_next_chapter_at
                assert ready_at is not None
                if ready_at.tzinfo is None or ready_at.utcoffset() is None:
                    ready_at = ready_at.replace(tzinfo=UTC)
                recovered_details = {
                    **previous,
                    "schema_version": 1,
                    "ready_at": ready_at.isoformat(),
                    "final_version_id": final_version_id,
                    "final_path": str(final_path),
                    "continuity_ledger_hash": ledger_hash,
                    "continuity_ledger_source_text_hash": (
                        ledger_source_text_hash
                    ),
                    "finished_at": now.isoformat(),
                    "tasks": tasks,
                    "failures": failures,
                    "startup_recovery": {
                        "reason": "engine_restarted_mid_memory_enrichment",
                        "previous_status": "running",
                        "result_unknown": True,
                        "automatic_provider_replay": False,
                        "started_provider_attempt_ids": (
                            started_provider_attempt_ids
                        ),
                        "unconsumed_provider_attempts": (
                            unconsumed_provider_attempts
                        ),
                        "provider_result_unknown": bool(
                            unconsumed_provider_attempts
                        ),
                        "source_provenance_verified": (
                            source_provenance_verified
                        ),
                        "source_provenance_error": source_provenance_error,
                        "sqlite_baseline": sqlite_baseline,
                        "sqlite_baseline_digest": sqlite_baseline_digest,
                        "sqlite_footprint": sqlite_footprint,
                        "sqlite_digest": sqlite_digest,
                        "sqlite_side_effect_count": sqlite_side_effect_count,
                        "vector_baseline_count": vector_baseline_count,
                        "vector_baseline_digest": vector_baseline_digest,
                        "vector_count": vector_count,
                        "vector_digest": vector_digest,
                        "vector_side_effect_count": vector_side_effect_count,
                        "vector_quarantined_count": vector_quarantined_count,
                        "vector_quarantine_succeeded": (
                            vector_quarantine_error is None
                        ),
                        "vector_quarantine_error": vector_quarantine_error,
                        "side_effects_verified_absent": safe_to_continue,
                        "safe_to_continue": safe_to_continue,
                        "quarantine_required": not safe_to_continue,
                        "recovery_disposition": (
                            recovery_disposition
                            if safe_to_continue
                            else "blocked_uncertain"
                        ),
                    },
                }
                if apply:
                    row.memory_enrichment_status = "failed"
                    row.memory_enriched_at = None
                    row.memory_enrichment_details = recovered_details
                interrupted_ids.append(row.chapter_id)
            if apply:
                session.commit()
            else:
                session.rollback()
        except Exception:
            session.rollback()
            raise
        finally:
            session.close()

        if interrupted_ids and apply:
            _log.warning(
                "memory_enrichment_startup_interrupted",
                workspace_id=workspace_id,
                chapter_ids=interrupted_ids,
                interrupted_count=len(interrupted_ids),
                automatic_provider_replay=False,
            )
        return interrupted_ids

    def reset_stale(self, workspace_id: str) -> ResetStaleResult:
        """批量重置 workspace 内滞留非终态的中断章(v2 w1-s2 章自愈)。

        进程中断(kill / 崩溃)时正在写的章会停在流程中间态
        (``reviewing`` / ``rewriting`` / ``revising``),无人重置则状态
        与实际(无任何 task 在处理它)不符:UI 徽章撒谎、人工不敢动。
        本方法扫这些章翻回 ``drafting``(对齐 ``rewind`` 目标章语义 —
        版本历史 / scene_anchor 等中间产物不删,下次 write 全流程自然
        覆盖推进)。

        **防误杀对账两层**(任一命中 → 整 workspace 跳过,
        ``skipped_reason`` 标注原因):

        1. ``pipeline_runs.state='running'``(同 db 直查):execute 循环
           正在写章 — pipeline 不进 task_registry,必须单独覆盖;
        2. ``active_task_checker``(注入,生产 = ``task_registry.
           has_active_tasks``):chapter:write 等 API BackgroundTask 在写。

        ``human_intervention`` / ``invalidated`` 不在自愈范围(语义是
        "等人",见 ``_STALE_RESETTABLE_STATUSES`` docstring);``drafting``
        无需重置(本身就是可写起点)。空扫描 → 200 + count=0 幂等(对齐
        unblock-all 模式)。

        入口两个:lifespan 启动 hook(重启自动自愈,排在 task 启动扫描
        之后 — 僵尸 task 已翻 interrupted 不挡对账)+ ``POST
        /workspaces/{id}:reset-stale``(UI 一键)。
        """
        self._workspace_service.info(workspace_id)
        if self._session_factory is None:
            raise ChapterRewindServiceError(
                "reset_stale 需注入 session_factory"
            )

        reset_ids: list[str] = []
        session = self._session_factory(workspace_id)
        try:
            # 对账层 1:active pipeline run(循环正在写章,跳过防误杀)
            running_run = session.scalars(
                select(PipelineRunORM)
                .where(PipelineRunORM.workspace_id == workspace_id)
                .where(PipelineRunORM.state == "running")
                .limit(1)
            ).first()
            if running_run is not None:
                _log.info(
                    "chapter_reset_stale_skipped",
                    workspace_id=workspace_id,
                    reason="active_pipeline_run",
                    run_id=running_run.run_id,
                )
                return ResetStaleResult(
                    workspace_id=workspace_id,
                    reset_chapter_ids=[],
                    reset_count=0,
                    skipped_reason="active_pipeline_run",
                )
            # 对账层 2:task_registry 活跃任务(单章 BackgroundTask 在写)
            if self._active_task_checker is not None and (
                self._active_task_checker(workspace_id)
            ):
                _log.info(
                    "chapter_reset_stale_skipped",
                    workspace_id=workspace_id,
                    reason="active_tasks",
                )
                return ResetStaleResult(
                    workspace_id=workspace_id,
                    reset_chapter_ids=[],
                    reset_count=0,
                    skipped_reason="active_tasks",
                )

            retained_lease_ids = set(
                session.scalars(
                    select(ChapterOperationLeaseORM.chapter_id).where(
                        ChapterOperationLeaseORM.workspace_id == workspace_id
                    )
                )
            )
            stmt = (
                select(ChapterORM)
                .where(ChapterORM.workspace_id == workspace_id)
                .where(
                    ChapterORM.status.in_(_STALE_RESETTABLE_STATUSES)
                )
                .order_by(ChapterORM.chapter_id)
            )
            for row in session.execute(stmt).scalars():
                if row.chapter_id in retained_lease_ids:
                    _log.error(
                        "chapter_reset_stale_retained_operation_lease",
                        workspace_id=workspace_id,
                        chapter_id=row.chapter_id,
                    )
                    continue
                if (
                    isinstance(row.escalation_details, dict)
                    and row.escalation_details.get("reason")
                    == CANCELLED_REVIEW_REPLAY_REASON
                ):
                    _log.info(
                        "chapter_reset_stale_preserved_review_replay_checkpoint",
                        workspace_id=workspace_id,
                        chapter_id=row.chapter_id,
                        current_version=row.current_version,
                    )
                    continue
                row.status = "drafting"
                reset_ids.append(row.chapter_id)
            session.commit()
        except Exception:
            session.rollback()
            raise
        finally:
            session.close()

        for reset_id in reset_ids:
            self._publish_chapter_event(
                chapter_id=reset_id,
                event="chapter_stale_reset",
                data={
                    "chapter_id": reset_id,
                    "workspace_id": workspace_id,
                    "to_status": "drafting",
                },
            )

        if reset_ids:
            _log.info(
                "chapter_reset_stale",
                workspace_id=workspace_id,
                reset_count=len(reset_ids),
                reset_chapter_ids=reset_ids,
            )

        return ResetStaleResult(
            workspace_id=workspace_id,
            reset_chapter_ids=reset_ids,
            reset_count=len(reset_ids),
            skipped_reason=(
                "retained_operation_lease"
                if retained_lease_ids and not reset_ids
                else None
            ),
        )

    # ---- 内部辅助 -----------------------------------------------------------

    @staticmethod
    def _is_sha256(value: object) -> bool:
        return (
            isinstance(value, str)
            and len(value) == 64
            and all(character in "0123456789abcdef" for character in value)
        )

    def _soft_delete_chroma(
        self,
        *,
        workspace_id: str,
        chapter_id: str,
        require_success: bool = False,
    ) -> int:
        """软删除本章向量；durable lease 路径失败时必须保留 checkpoint。"""
        if self._vector_store_factory is None:
            if require_success:
                raise ChapterRewindServiceError(
                    "rewind 已提交 SQLite，但缺少 Chroma 恢复依赖；"
                    "已保留 source_mutation_committed 租约"
                )
            return 0
        try:
            vector_store = self._vector_store_factory(workspace_id)
            return vector_store.soft_delete_by_chapter_id(
                DEFAULT_COLLECTION, chapter_id=chapter_id
            )
        except Exception as exc:  # noqa: BLE001
            _log.warning(
                "chapter_rewind_chroma_failed",
                workspace_id=workspace_id,
                chapter_id=chapter_id,
                error_type=type(exc).__name__,
                error_message=str(exc),
            )
            if require_success:
                raise ChapterRewindServiceError(
                    "rewind 已提交 SQLite，但 Chroma 软删除失败；"
                    "已保留 source_mutation_committed 租约供冷启动重试"
                ) from exc
            return 0

    def _publish_chapter_event(
        self,
        *,
        chapter_id: str,
        event: str,
        data: dict[str, Any],
    ) -> None:
        """WS publish 到 ``chapter.{chapter_id}.events``;失败 swallow + log。"""
        if self._ws_manager is None:
            return
        try:
            self._ws_manager.publish(
                channel=chapter_events_channel(chapter_id),
                event=event,
                data=data,
            )
        except Exception as exc:  # noqa: BLE001
            # structlog ``event`` 是首参,业务 ``event`` 名重命名 ``ws_event``
            # 避免 kwarg 冲突(对齐 ``ChapterCardService._publish_card_event``
            # 同型修复)。
            _log.error(
                "chapter_rewind_event_notify_failed",
                chapter_id=chapter_id,
                ws_event=event,
                error_type=type(exc).__name__,
                error_message=str(exc),
            )

    def _extract_chapter_summary_hook(
        self,
        workspace_id: str,
        chapter_id: str,
        *,
        trigger: str,
    ) -> None:
        """历史兼容的 force-pass 摘要 hook（R4 生产批准链不再调用）。

        委托给 ``chapter_summary_extractor.extract_and_persist_chapter_summary_safely``
        独立模块函数;全程 fail-open，只供旧测试/兼容调用。
        ``self._llm_client`` / ``self._llm_model`` / ``self._router_factory``
        均未注入时静默跳过(CLI / 单测 legacy 兼容)。
        """
        # 局部 import 规避 agents ↔ services 顶层循环
        from src.core.agents.chapter_summary_extractor import (
            extract_and_persist_chapter_summary_safely,
        )
        from src.core.workspace.workspace_paths import WorkspacePaths

        paths = WorkspacePaths(
            workspace_id,
            workspaces_root=self._workspace_service.workspaces_root,
        )
        router = (
            self._router_factory(workspace_id)
            if self._router_factory is not None
            else None
        )
        # v1.1 wave 3 s6(E1):构造 vector_store 让 force_pass 后的摘要也同步入
        # Chroma;工厂未注入(CLI / 单测 legacy)→ None,hook 内部跳过 Chroma 写。
        vector_store = (
            self._vector_store_factory(workspace_id)
            if self._vector_store_factory is not None
            else None
        )
        extract_and_persist_chapter_summary_safely(
            workspace_id=workspace_id,
            chapter_id=chapter_id,
            trigger=trigger,  # type: ignore[arg-type]
            paths=paths,
            session_factory=self._session_factory,
            llm_client=self._llm_client,  # type: ignore[arg-type]
            llm_model=self._llm_model,
            router=router,  # type: ignore[arg-type]
            vector_store=vector_store,
        )

    def _run_force_pass_memory_enrichment_checkpoint(
        self,
        workspace_id: str,
        chapter_id: str,
        *,
        final_version_id: int,
    ) -> None:
        """让 force-pass 复用 reviewer-pass 的持久富记忆 Worker。

        生产 API/CLI 都注入 router + vector factory。缺依赖仅可能来自 legacy
        直接构造或聚焦测试，此时保留合法 ``pending``，不把“没执行”伪写成
        succeeded。依赖齐全时临时装配 ChapterService，只调用它公开的 ready
        checkpoint 入口；Provider attempt 继续继承外层 force-pass scope。
        """
        has_llm = self._router_factory is not None or (
            self._llm_client is not None and self._llm_model is not None
        )
        if (
            self._session_factory is None
            or self._vector_store_factory is None
            or not has_llm
        ):
            _log.info(
                "force_pass_memory_enrichment_skipped",
                workspace_id=workspace_id,
                chapter_id=chapter_id,
                reason="dependencies_not_injected",
                preserved_pending=True,
            )
            return

        # 局部 import 避免 ChapterService 文档中的反向类型引用形成顶层循环。
        from src.services.chapter_service import ChapterService

        memory_service = ChapterService(
            workspace_service=self._workspace_service,
            client=self._llm_client,  # type: ignore[arg-type]
            model=self._llm_model or "router-managed",
            session_factory=self._session_factory,
            vector_store_factory=self._vector_store_factory,
            router_factory=self._router_factory,  # type: ignore[arg-type]
            memory_enrichment_scheduler="inline",
        )
        memory_service.run_memory_enrichment_for_ready_checkpoint(
            workspace_id,
            chapter_id,
            final_version_id=final_version_id,
            progression_consistency_enabled=False,
            progression_label="境界",
        )

    # ---- 公共入口:手动编辑正文(v1.1 acceptance 修复2,孤儿章人工解决) ----

    def update_content_manual(
        self,
        workspace_id: str,
        chapter_id: str,
        *,
        content: str,
    ) -> ContentUpdateResult:
        """人工编辑章节正文(孤儿章人工解决流程第一步,批准走 ``force_pass``)。

        仅 ``human_intervention`` 状态可调(防误改 approved / 进行中章节),
        其余状态抛 ``ChapterRewindServiceError``(路由翻 409
        ``CHAPTER_INVALID_STATE``)。

        flow:
        1. 校验 workspace + ORM 行存在 + ``status == 'human_intervention'``
        2. ``ChapterVersioning.write_version(creation_type='manual_edit')``
           落新版本文件(版本号自增,LLM 旧版全保留)
        3. ``persist_chapter_version_safely`` 落 ``chapter_versions`` 行并
           同事务重算版本/rewrite/revise 查询投影；失败 fail closed
        4. ``mark_final`` 切 final 指针到新版本(后续 ``force_pass`` 用
           ``current_version`` 标 final)
        5. WS publish ``chapter_content_updated``
        """
        self._workspace_service.info(workspace_id)
        if self._session_factory is None:
            raise ChapterRewindServiceError(
                "update_content_manual 需注入 session_factory"
            )

        # 1. 状态校验(短 session 读)
        operation_lease: ChapterOperationLease | None = None
        session = self._session_factory(workspace_id)
        try:
            if self._operation_lease_store is not None:
                self._operation_lease_store.begin_immediate(session)
            row = session.get(ChapterORM, chapter_id)
            if row is None or row.workspace_id != workspace_id:
                raise ChapterRewindServiceError(
                    f"章节不存在: workspace={workspace_id} chapter={chapter_id}"
                )
            if row.status != "human_intervention":
                raise ChapterRewindServiceError(
                    f"章节状态不允许人工编辑正文: chapter_id={chapter_id} "
                    f"current_status={row.status!r} "
                    f"allowed_sources=['human_intervention']"
                )
            self._require_source_mutation_safe(
                row=row,
                operation="update-content-manual",
            )
            operation_lease = self._acquire_source_operation_in_session(
                session,
                row=row,
                operation="update-content-manual",
            )
            self._claim_source_mutation_row(
                session=session,
                row=row,
                target_status="human_intervention",
                clear_approved_at=False,
                operation="update-content-manual",
            )
            # 人工正文尚未落文件/版本账本，租约保持 reserved。若进程在
            # write_version→DB→final 任一点中断，startup 可用 v{source+1}
            # 的不可变正文确定性补齐；不能提前声称 source mutation 已完成。
            session.commit()
        except Exception:
            session.rollback()
            raise
        finally:
            session.close()

        # 2-4. 落新版本文件 + 版本行 + 切 final 指针
        from src.core.chapter_versioning import ChapterVersioning
        from src.core.orchestrator.chapter_orchestrator import (
            persist_chapter_version_safely,
        )
        from src.core.workspace.workspace_paths import WorkspacePaths

        paths = WorkspacePaths(
            workspace_id,
            workspaces_root=self._workspace_service.workspaces_root,
        )
        versioning = ChapterVersioning(paths)
        cv = versioning.write_version(
            chapter_id,
            content,
            creation_type="manual_edit",
            model_used="manual",
        )
        persist_chapter_version_safely(
            lambda: self._session_factory(workspace_id),  # type: ignore[misc]
            chapter_id=chapter_id,
            version=cv,
        )
        versioning.mark_final(chapter_id, cv.version_id)

        self._publish_chapter_event(
            chapter_id=chapter_id,
            event="chapter_content_updated",
            data={
                "chapter_id": chapter_id,
                "workspace_id": workspace_id,
                "version_id": cv.version_id,
                "word_count": cv.word_count,
            },
        )
        _log.info(
            "chapter_content_updated_manual",
            workspace_id=workspace_id,
            chapter_id=chapter_id,
            version_id=cv.version_id,
            word_count=cv.word_count,
        )
        self._finish_source_operation(operation_lease)
        return ContentUpdateResult(
            chapter_id=chapter_id,
            version_id=cv.version_id,
            word_count=cv.word_count,
        )


__all__ = [
    "ChapterRewindService",
    "ChapterRewindServiceError",
    "ChapterRewindInvalidStateError",
    "ChapterInvalidatedNeedsUnblockError",
    "ChapterUnblockInvalidStateError",
    "RewindResult",
    "ForcePassResult",
    "ContentUpdateResult",
    "UnblockResult",
    "UnblockAllResult",
]
