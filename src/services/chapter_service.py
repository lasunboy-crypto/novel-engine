"""章节服务(P1-B-15)。

对应 PROJECT_SPEC §9.4 章节生命周期与 API_CONTRACT 章节路由的业务层。
负责把"工作区合法性校验 + 章节卡 IO + 拼记忆段 + 调 Orchestrator/Reviser
agent + 状态推断 + 版本切换"集中到一处,API 路由(P1-B-19)与 CLI(P1-B-21)
共用同一份实例。

设计要点
--------

- **依赖反转**:Service 构造时注入 ``WorkspaceService`` / ``LLMClient`` /
  默认模型名;不在内部 ``new()``,P1-B-19 路由层与 CLI 共用同一份实例。
- **章节卡走文件**:P1 阶段章节卡是文件式(``data/cards/<chapter_id>.json``);
  ``read_chapter_card`` / ``save_chapter_card`` 内部辅助。P2 才进 SQLite。
- **write / rewrite 走 Orchestrator,revise 不走**:revise 走 Orchestrator
  会被 ``REVISING`` 分支的 ``reviews[-1]`` 依赖锁死(P1 没持久化 review),
  P1 阶段直接调 ``revise_chapter`` agent + ``write_version`` 落新版本,
  ``status`` 由调用方/上层路由决定(详见 ``revise`` docstring)。
- **R5 权威预算**:``write`` / ``rewrite`` 都从 immutable
  ``chapter_versions`` 派生版本、rewrite 与 revise 消耗；兼容参数
  ``prior_rewrite_count`` 不再参与判定。已有正文从最新版本重审，不会因
  重启或换 run 获得免费 draft/rewrite。
- **状态查询 3 档 + unknown**:文件系统推断只能给"目录不存在 / 有 final.md
  / 其他"这种 unknown-friendly 的判断;不合并成 drafting 掩盖信息缺失。
  P1-B-16 落 chapters 表后改成读 ``chapters.status`` 字段,前端代码不需重写
  (P1 阶段已被强制处理 unknown 分支)。
- **异常**:统一抛 ``ChapterServiceError(Exception)``;``MetaFileError``
  从 ``WorkspaceService.info`` 透传时翻译成 ``WorkspaceServiceError``
  (本质是工作区不合法,而非章节问题)。

P1 起步阶段未实现的能力已在后续周次逐步接线；post-v6 起正文生产固定单稿。
"""

from __future__ import annotations

import hashlib
import json
import re
import uuid
from collections.abc import Callable
from contextlib import nullcontext
from contextvars import copy_context
from dataclasses import dataclass
from datetime import datetime, timezone
from pathlib import Path
from threading import Thread
from typing import Any, Literal, NoReturn, cast

import structlog
from pydantic import BaseModel, Field, ValidationError
from sqlalchemy import JSON, func, or_, select, update
from sqlalchemy.orm import Session

from src.api.logging import bind_chapter_context
from src.core.agents.chapter_summary_extractor import (
    ContinuityLedgerValidationError,
    build_provenance_bound_continuity_capsule,
    continuity_ledger_provenance,
    extract_and_persist_continuity_ledger,
    load_validated_continuity_ledger,
    persist_continuity_ledger_vector_safely,
)
from src.core.agents.reviser import revise_chapter
from src.core.chapter_versioning import (
    ChapterVersioning,
    ChapterVersioningError,
    collect_canonical_version_files,
)
from src.core.continuity import build_continuity_context, extract_from_prev_text
from src.core.db.models import (
    AttemptLedgerORM,
    ChapterCardORM,
    ChapterORM,
    ChapterOperationLeaseORM,
    ChapterVersionORM,
    PipelineRunORM,
    ReviewResultORM,
    WorkspaceORM,
)
from src.core.llm.base import LLMClient
from src.core.memory.file_memory import build_memory_section
from src.core.memory.store import list_characters_by_workspace
from src.core.memory.retriever import DEFAULT_COLLECTION
from src.core.memory.vector import VectorStore
from src.core.models.chapter import ChapterVersion
from src.core.models.chapter_card import ChapterCard, chapter_seq_from_id
from src.core.models.continuity_ledger import (
    validate_immediate_action_scope,
    ContinuityEvidenceRecord,
    ContinuityLedgerProvenance,
    ProvenanceBoundContinuityCapsule,
    validate_continuity_bridge,
    validate_continuity_projection,
    validate_frozen_provenance,
)
from src.core.models.review import ConstraintResult, ReviewIssue
from src.core.models.setting_contract import (
    SettingContract,
    SettingContractLoadError,
    load_setting_contract,
)
from src.core.timing import StageTimer, log_stage_timing
from src.core.orchestrator.chapter_orchestrator import (
    MemoryExtractionOutcome,
    OrchestratorResult,
    RewriteCheckpointContext,
    RewriteCheckpointRecoveryError,
    StateTransitionCallback,
    _check_plot_thread_payoff_safely,
    _extract_memories_safely,
    _resolve_cross_chapter_voice_context,
    load_rewrite_checkpoint_context,
    persist_chapter_version_safely,
    orchestrate_chapter,
)
from src.core.orchestrator.trace import make_trace_id
from src.core.routing.router import ModelRouter
from src.core.rules.cross_chapter_voice_validator import (
    resolve_cross_chapter_voice_constraint_spec,
)
from src.core.rules.drama_contracts import (
    DramaticSourceContractError,
    load_and_validate_card_dramatic_source,
)
from src.core.rules.prose_hard_contract import compile_executable_constraint_plan
from src.core.rules.prose_revision_contract import (
    ProseRevisionContractError,
    build_reviser_revision_contract,
    count_effective_content_rewrites,
    find_localized_adjacent_duplicates,
    prose_content_equivalent,
)
from src.core.rules.paragraph_reflow import PARAGRAPH_REFLOW_MODEL_USED
from src.core.rules.style_drift_validator import resolve_rewrite_style_constraint_spec
from src.core.rules.style_fingerprint import (
    compute_style_fingerprint,
    summarize_voice_from_fingerprint,
)
from src.core.rules.validators import (
    check_character_existence,
    check_dead_character_references,
)
from src.core.state_machine.states import ChapterState
from src.core.workspace.meta import MetaFileError
from src.core.workspace.workspace_paths import WorkspacePaths
from src.services.asset_import import ensure_workspace_orm
from src.services.attempt_ledger import (
    ProviderCallCapExceeded,
    bind_attempt_scope,
    current_attempt_scope,
)
from src.services.chapter_operation_lease import (
    ChapterOperationLease,
    ChapterOperationLeaseConflict,
    ChapterOperationLeaseError,
    ChapterOperationLeaseStore,
)
from src.services.chapter_rewind_service import (
    CANCELLED_REVIEW_REPLAY_REASON,
    REWIND_REWRITE_REQUIRED_REASON,
)
from src.services.cost_tracker import wrap_router_with_cost_tracking
from src.services.known_review_response_recovery import (
    KnownReviewResponseRecovery, KnownReviewRecoveryRouter, load_known_empty_must_recovery,
)
from src.services.core_state_service import (
    CoreStateConflictError,
    CoreStateService,
    CoreStateServiceError,
)
from src.services.metrics_aggregator import aggregate_daily_safely
from src.services.metrics_writer import write_per_chapter_safely
from src.services.memory_enrichment_checkpoint import (
    MemoryEnrichmentCheckpointShapeError,
    memory_enrichment_requires_manual_recovery,
    memory_enrichment_retryable_after_explicit_continue,
    validate_memory_enrichment_checkpoint_shape,
)
from src.services.memory_enrichment_attempts import (
    MemoryEnrichmentAttemptLedgerError,
    RICH_MEMORY_PROVIDER_AGENTS,
    load_unconsumed_memory_provider_attempts,
)
from src.services.memory_enrichment_footprint import (
    audit_sqlite_memory_snapshot,
)
from src.services.memory_enrichment_worker_guard import (
    serialize_memory_enrichment_worker,
)
from src.services.profile_override import (
    WriterProfileNotFoundError,
    wrap_router_with_profile_override,
)
from src.services.workspace_service import (
    WorkspaceService,
    WorkspaceServiceError,
)

ChapterStateValue = Literal[
    "drafting",
    "reviewing",
    "rewriting",
    "revising",
    "approved",
    "published",
    "human_intervention",
    "invalidated",
]

CancelledTaskCheckpointCheck = Callable[[str, str, str], bool]

_POST_REVIEW_GATE_AGENTS = frozenset(
    {
        "must_achieve_validator",
        "continuity_validator",
        "forbidden_crossing_validator",
        "setting_consistency_validator",
        "cross_chapter_dedup_validator",
        "ending_tension_validator",
    }
)
FAILED_FIRST_REVIEW_REPLAY_REASON = "failed_first_review_gate_replay"
"""章节 8 档状态字面量(P2-B-17 起 7 档,P6-B-01 扩 ``invalidated`` 第 8 档)。

对齐 PROJECT_SPEC §3.5 Chapter.status + ``ChapterState`` StrEnum;
ChapterStatusInfo / ChapterListEntry / ``ChapterListItem`` / ``ChapterStatusOut``
均切到本 Literal。``invalidated`` 由 ``ChapterRewindService.rewind`` 给
``chapter_id > target_id`` 的后续章节写入,``unblock`` 端点翻回 ``drafting``。"""


@dataclass(frozen=True)
class _ChapterBudget:
    """由 ``chapter_versions`` 权威派生的章节预算快照。"""

    current_version: int
    rewrite_count: int
    revise_count: int


@dataclass(frozen=True)
class _CardContinuityPreflight:
    """Writer 出网前一次性选定的卡片与可选短版来源合同。"""

    card: ChapterCard
    capsule: ProvenanceBoundContinuityCapsule | None


def _derive_chapter_budget(session: Session, chapter_id: str) -> _ChapterBudget:
    """从有序 immutable 版本账本派生版本号与有效修订预算。"""

    rows = session.execute(
        select(
            ChapterVersionORM.version_id,
            ChapterVersionORM.creation_type,
            ChapterVersionORM.model_used,
            ChapterVersionORM.content,
        )
        .where(ChapterVersionORM.chapter_id == chapter_id)
        .order_by(ChapterVersionORM.version_id)
    ).all()
    return _ChapterBudget(
        current_version=int(rows[-1].version_id) if rows else 0,
        rewrite_count=count_effective_content_rewrites(
            [
                (str(row.creation_type), str(row.model_used), str(row.content))
                for row in rows
            ],
            excluded_model_used=PARAGRAPH_REFLOW_MODEL_USED,
        ),
        revise_count=sum(1 for row in rows if row.creation_type == "revise"),
    )


@dataclass(frozen=True)
class _MemoryEnrichmentLease:
    """富记忆 Worker 抢占成功后用于终态 CAS 的不可变租约。"""

    final_version_id: int
    ready_at: datetime | None
    running_details: dict[str, Any]


ChapterSessionFactory = Callable[[str], Session]
MemoryEnrichmentScheduler = Callable[[Callable[[], None]], None]

ChapterVectorStoreFactory = Callable[[str], VectorStore]
"""P2-B-19:按 ``workspace_id`` 构造 ``VectorStore`` 的工厂签名。

注入到 ``ChapterService.__init__`` 后,write/rewrite 路径在 ``_orchestrate``
入口调 ``factory(workspace_id)`` 拿当前工作区的 chroma 实例,透传给
Orchestrator → Writer / extract。``revise`` 不调(局部修订不抽 voice / 不触发
extract,P1 简版,与 ``orchestrate_chapter`` 不参与 revise 的语义对齐)。

测试可直接传 lambda(``lambda ws_id: in_memory_vector_store``)。生产路径
``api/deps`` + ``cli/commands`` 都注入复用 ``make_vector_store``。
"""

ChapterRouterFactory = Callable[[str], ModelRouter]
"""P3-B-06:按 ``workspace_id`` 构造 ``ModelRouter`` 的工厂签名。

注入到 ``ChapterService.__init__`` 后,write / rewrite / revise 三个路径都
按工作区调 ``factory(workspace_id)`` 拿合并(引擎默认 + project.yaml 覆盖)
后的 router 实例。每次写作请求重新构造 router(成本极低:一次 yaml 读取 +
WriterProfilesConfig 校验),保证 hot-reload 工作区配置也能立即生效。

生产路径 ``api/deps`` + ``cli/commands`` 默认从
``services.config_service.load_workspace_config`` 取配置，再由中央
``build_model_router`` 选择 legacy router 或活动 manifest lock；测试可直接传
lambda(``lambda ws_id: stub_router``)。

``None`` 时 ``ChapterService`` 回退 P1/P2 legacy 路径(用 ``__init__`` 注入
的 ``client`` + ``model``);避免对既有调用方一刀切。
"""

_log = structlog.get_logger(__name__)


class ChapterServiceError(Exception):
    """章节服务操作失败时抛出。

    P1-B-17 全局错误体系起来后会升级为 ``ApiError`` 子类。
    """


class ChapterCardSettingStaleError(ChapterServiceError):
    """章节卡保存的世界合同 hash 与当前权威合同不一致。"""

    code = "CHAPTER_CARD_SETTING_STALE"

    def __init__(
        self,
        *,
        chapter_id: str,
        card_contract_hash: str,
        current_contract_hash: str,
    ) -> None:
        self.chapter_id = chapter_id
        self.card_contract_hash = card_contract_hash
        self.current_contract_hash = current_contract_hash
        self.constraint_id = hashlib.sha256(
            (
                f"chapter_card_setting_stale\n{chapter_id}\n"
                f"{card_contract_hash}\n{current_contract_hash}"
            ).encode("utf-8")
        ).hexdigest()
        super().__init__(
            f"{self.code}:章节卡世界合同已陈旧，必须按当前 world 重新生成 "
            f"(chapter_id={chapter_id})"
        )


class ContinuityLedgerNotReadyError(ChapterServiceError):
    """approved 后同步小账本未通过硬水位。"""

    code = "CONTINUITY_LEDGER_NOT_READY"

    def __init__(self, *, chapter_id: str, reason: str) -> None:
        self.chapter_id = chapter_id
        self.reason = reason
        super().__init__(f"连续性小账本未就绪: chapter_id={chapter_id}: {reason}")


class MemoryEnrichmentNotReadyError(ChapterServiceError):
    """正式 Run 的上一章富记忆尚未成功，不得先生成下一章卡。"""

    code = "MEMORY_ENRICHMENT_NOT_READY"

    def __init__(
        self,
        *,
        chapter_id: str,
        status: str,
        reason: str,
        recovery_details: dict[str, Any] | None = None,
    ) -> None:
        self.chapter_id = chapter_id
        self.status = status
        self.reason = reason
        self.recovery_details = dict(recovery_details or {})
        self.pause_reason = (
            "memory_enrichment_failed"
            if status in {"failed", "skipped"}
            else "memory_enrichment_not_ready"
        )
        super().__init__(
            "富记忆尚未成功，正式 Run 不得生成下一章卡或正文: "
            f"chapter_id={chapter_id} status={status!r}: {reason}"
        )


class ContinuityCheckpointCorruptionError(ChapterServiceError):
    """章节通过痕迹与 DB/checkpoint 真相互相冲突，必须保留现场。"""

    code = "CONTINUITY_CHECKPOINT_CORRUPTION"

    def __init__(
        self,
        *,
        chapter_id: str,
        reason: str,
        evidence: list[str] | None = None,
    ) -> None:
        self.chapter_id = chapter_id
        self.reason = reason
        self.evidence = list(evidence or [])
        evidence_text = ", ".join(self.evidence) if self.evidence else "(无)"
        super().__init__(
            f"连续性 checkpoint 损坏: chapter_id={chapter_id}: {reason}; "
            f"evidence={evidence_text}"
        )


def _is_known_retry_core_hash_omission(row: ChapterORM) -> bool:
    """识别旧记忆重试投影漏字段；不覆盖非空错值，不代表富记忆已完成。"""
    details = row.memory_enrichment_details
    if not isinstance(details, dict) or "core_state_commit_hash" in details:
        return False
    if not memory_enrichment_retryable_after_explicit_continue(
        status=row.memory_enrichment_status,
        details=details,
        enriched_at=row.memory_enriched_at,
    ):
        return False
    history = details.get("retry_history")
    return bool(
        isinstance(history, list)
        and history
        and all(
            isinstance(record, dict)
            and record.get("trigger") == "pipeline_explicit_continue"
            and record.get("automatic_provider_replay") is False
            and record.get("run_id")
            and record.get("task_id")
            and record.get("previous_sqlite_digest")
            and isinstance(record.get("consumed_provider_attempt_ids"), list)
            for record in history
        )
    )


def _require_memory_enrichment_checkpoint_safe(
    *,
    row: ChapterORM,
    chapter_id: str,
    evidence: list[str] | None = None,
    block_running: bool,
) -> None:
    """校验富记忆 checkpoint；仅会重排任务的恢复入口拦 ``running``。"""

    checkpoint_evidence = [
        f"memory_enrichment_status={row.memory_enrichment_status!r}",
        (
            "memory_enrichment_details_type="
            f"{type(row.memory_enrichment_details).__name__}"
        ),
        f"memory_enriched_at={row.memory_enriched_at!r}",
        *(evidence or []),
    ]
    try:
        validate_memory_enrichment_checkpoint_shape(
            status=row.memory_enrichment_status,
            details=row.memory_enrichment_details,
            enriched_at=row.memory_enriched_at,
        )
    except MemoryEnrichmentCheckpointShapeError as exc:
        raise ContinuityCheckpointCorruptionError(
            chapter_id=chapter_id,
            reason=f"富记忆 checkpoint 结构损坏，必须原样保留现场: {exc}",
            evidence=checkpoint_evidence,
        ) from exc

    if block_running and row.memory_enrichment_status == "running":
        raise ContinuityCheckpointCorruptionError(
            chapter_id=chapter_id,
            reason=(
                "approved checkpoint 的富记忆仍为 running；请等待当前 Worker "
                "终态或先完成唯一后端冷启动收口，禁止自动重放未知请求"
            ),
            evidence=checkpoint_evidence,
        )
    if memory_enrichment_requires_manual_recovery(
        status=row.memory_enrichment_status,
        details=row.memory_enrichment_details,
    ):
        raise ContinuityCheckpointCorruptionError(
            chapter_id=chapter_id,
            reason=(
                "富记忆冷启动审计发现未知 SQLite 副作用或 Chroma 未完成隔离；"
                "必须保留现场并人工裁决，禁止继续写章或改源"
            ),
            evidence=checkpoint_evidence,
        )


class ChapterCardContinuityStaleError(ChapterServiceError):
    """Writer 前发现章节卡冻结的上一章 ledger provenance 已陈旧。"""

    code = "CHAPTER_CARD_CONTINUITY_STALE"

    def __init__(
        self,
        *,
        chapter_id: str,
        reason: str,
        frozen: ContinuityLedgerProvenance | None,
        current: ContinuityLedgerProvenance | None,
    ) -> None:
        self.chapter_id = chapter_id
        self.reason = reason
        self.frozen = frozen
        self.current = current
        super().__init__(f"章节卡连续性小账本已陈旧: chapter_id={chapter_id}: {reason}")


class ChapterCardContinuityBridgeError(ChapterServiceError):
    """Writer 前发现即时未完动作跨跳转缺少明确 bridge。"""

    code = "CHAPTER_CARD_CONTINUITY_BRIDGE_REQUIRED"

    def __init__(self, *, chapter_id: str, reason: str) -> None:
        self.chapter_id = chapter_id
        self.reason = reason
        super().__init__(f"章节卡缺少跨章承接: chapter_id={chapter_id}: {reason}")


class ChapterReReviewInvalidStateError(ChapterServiceError):
    """``re_review`` 时目标章节状态非 ``human_intervention``。

    v2 W3.5 UI 缺口3 引入。重审(对人工改稿重跑写后审校)只在孤儿章
    ``human_intervention`` 状态有意义;其余状态调用 = 误操作。路由层翻译为
    ``CHAPTER_RE_REVIEW_INVALID_STATE (422)``。
    """

    def __init__(self, *, chapter_id: str, current_status: str) -> None:
        self.chapter_id = chapter_id
        self.current_status = current_status
        super().__init__(
            f"章节状态不允许重审: chapter_id={chapter_id} "
            f"current_status={current_status!r}(需 human_intervention)"
        )


class ChapterReReviewNoVersionError(ChapterServiceError):
    """``re_review`` 时该章无任何已落盘版本(无正文可审)。

    v2 W3.5 UI 缺口3 引入。正常流程下 human_intervention 章必有版本;此错误
    防御 fixture / 异常态。路由层翻译为 ``CHAPTER_RE_REVIEW_NO_VERSION (422)``。
    """

    def __init__(self, *, chapter_id: str) -> None:
        self.chapter_id = chapter_id
        super().__init__(f"章节无版本可重审: chapter_id={chapter_id}")


class ChapterStatusInfo(BaseModel):
    """章节状态查询结果(P2-B-17 起 7 档,对齐 PROJECT_SPEC §3.5 ``Chapter.status``)。

    ``status`` 取值规则(优先级):

    1. **chapters 表存在该章节行**(P2-B-17 起 ``ChapterService.write/rewrite``
       入口 upsert 行 + ``on_state_transition`` 钩子写库):直接读 SQLite
       ``chapters.status``,反映状态机当前节点。
    2. **chapters 表无该章节行**(章节卡已建但尚未跑过 ``write``/``rewrite``,
       或 ``session_factory=None`` 即 P1 兼容模式):从文件系统推 7 档:

       - 有 ``final.md`` → ``approved``
       - 无版本文件 → ``drafting``(P1 ``not_started`` 语义在 7 档中折算到
         ``drafting``,即"状态机起点");前端区分"是否有版本"看
         ``version_count == 0``
       - 有 ``v*.md`` 但无 ``final.md`` → ``drafting``(P1 ``unknown`` 折算;
         无 chapters 行说明状态机未启动,以 ``drafting`` 起点近似)

    P1 ``not_started`` / ``unknown`` 两档已不再返回(P2-B-17 升 7 档 Literal);
    前端语义可通过 ``version_count == 0`` 区分"未开工"(等价旧 not_started),
    通过 ``has_final`` 区分"已通过审稿"。
    """

    chapter_id: str
    status: ChapterStateValue
    latest_version_id: int | None = Field(..., description="最新版本号;无版本时为 None")
    has_final: bool
    version_count: int = Field(..., ge=0)
    escalation_details: dict[str, Any] | None = Field(
        None,
        description=(
            "升 human_intervention 时的结构化详情(v1.1 acceptance 修复2 / "
            "Fix D 写前预检 findings:哪条 must 不可达 / judge 证据 / 处置建议);"
            "无 chapters 行 / 非该路径升级 → None"
        ),
    )


class ChapterListEntry(BaseModel):
    """章节列表项(P1-B-25 引入,P2-B-17 ``status`` 升 7 档)。

    `list_chapters` 单条返。字段是 P1-F-10 看板 + P1-F-11 列表行的并集:

    - ``chapter_id`` / ``title`` / ``chapter_number`` / ``volume_id``:取自
      ``ChapterCard`` 字面字段(类 A 冲突按规格源对齐——P1-F-10 任务模板字面
      ``display_name`` / ``order_index`` 在 ``ChapterCard`` 实际是 ``title`` /
      ``chapter_number``,本任务 P1-B-25 沿用 ``ChapterCard`` 字段名)。
    - ``status`` / ``latest_version_id`` / ``has_final`` / ``version_count``:
      复用 ``ChapterStatusInfo`` 同字段(P2-B-17 起 ``status`` 7 档:优先读
      chapters 表,fallback 文件系统推断)。
    - ``last_modified_at``:章节卡 JSON 文件 mtime 转 ISO 8601 UTC 字符串
      ("最近修改"看板字段)。
    """

    chapter_id: str
    title: str = Field(..., description="章节标题(取自 ChapterCard.title)")
    chapter_number: int = Field(
        ..., ge=1, description="卷内章序号(取自 ChapterCard.chapter_number)"
    )
    volume_id: str
    status: ChapterStateValue
    latest_version_id: int | None
    has_final: bool
    version_count: int = Field(..., ge=0)
    last_modified_at: str = Field(..., description="章节卡文件 mtime,ISO 8601 UTC")


class ContinuityRefreshEntry(BaseModel):
    """``refresh_continuity_all`` 单章结果(v1.1 w3-s7 E6 service 层)。"""

    chapter_id: str
    chapter_number: int = Field(..., ge=1)
    outcome: Literal["refreshed", "unchanged", "skipped_first"]


class ContinuityRefreshResult(BaseModel):
    """``refresh_continuity_all`` 批量结果(v1.1 w3-s7 E6 service 层)。"""

    total_scanned: int = Field(..., ge=0)
    refreshed_count: int = Field(..., ge=0)
    items: list[ContinuityRefreshEntry]


class StyleDriftPoint(BaseModel):
    """单版本风格指纹时序点(v1.1 w3-s7 H7 service 层)。

    从 ``ChapterVersionORM.style_fingerprint``(G5 落盘的 17 字段 dict)派生读者
    最能感知的 4 个数值维度 + 主导人称。旧版本 fingerprint 为 null 时
    ``has_fingerprint=False`` + 各维度 None(诚实标注缺数据,不伪造 0)。
    """

    version_id: int = Field(..., ge=1)
    creation_type: str
    has_fingerprint: bool
    paragraph_length_mean: float | None = None
    sentence_length_mean: float | None = None
    simile_density: float | None = None
    exclamation_density: float | None = None
    dominant_pov: Literal["third_person", "first_person", "mixed"] | None = None


class ChapterService:
    """章节生命周期服务。

    实例化时注入 ``WorkspaceService`` + ``LLMClient`` + 默认模型名 +
    可选 ``session_factory``;7 个公共方法对接 API 路由(P1-B-19)与
    CLI(P1-B-21)。

    ``session_factory``(P2-B-17 起):每次调用 ``factory(workspace_id)`` 返回
    新 ``Session``,绑定到本工作区 ``data/novel.db``。提供 → write/rewrite
    入口 upsert ``chapters`` 行,``on_state_transition`` 钩子同步写状态;
    ``status``/``list_chapters`` 优先读库,无行 fallback 文件系统推断。
    ``None`` → P1 兼容模式,所有 DB 操作 no-op,纯文件推断(用于不依赖
    持久化状态的场景:CLI 不传 / 早期单测;**生产路径 ``api.deps`` +
    ``cli.commands`` 都注入工厂**,以让状态历史进库)。
    """

    def __init__(
        self,
        *,
        workspace_service: WorkspaceService,
        client: LLMClient,
        model: str,
        session_factory: ChapterSessionFactory | None = None,
        vector_store_factory: ChapterVectorStoreFactory | None = None,
        router_factory: ChapterRouterFactory | None = None,
        cost_guard_config: object | None = None,
        ws_manager: object | None = None,
        backup_service: object | None = None,
        memory_enrichment_scheduler: MemoryEnrichmentScheduler
        | Literal["inline", "background"] = "inline",
        operation_lease_store: ChapterOperationLeaseStore | None = None,
    ) -> None:
        self._workspace_service = workspace_service
        self._client = client
        self._model = model
        self._session_factory = session_factory
        # P2-B-19:wiring 入口。两个 factory 都给定时,write/rewrite 启用
        # voice / 检索段 + extract;任一为 None → P1 兼容路径(orchestrator
        # 内部按 ``wiring_enabled`` 二态判断,见 ``_orchestrate``)。
        self._vector_store_factory = vector_store_factory
        # P3-B-06:router 入口。给定时 _orchestrate / revise 路径都改走
        # router 拿 client + model + 生成参数(profile 为源头);未给定 →
        # 回退到 ``client`` + ``model`` 直接注入参数(P1/P2 legacy 兼容)。
        self._router_factory = router_factory
        # P4-acceptance 主流水线 gate(cost_guard / memory_health):
        # 两参数都可选,``None`` 时对应 gate 跳过(P1/P2/P3 兼容路径不变)。
        # ``cost_guard_config`` 类型在运行时为 ``CostGuardConfig``;签名用
        # ``object`` 是为了避免顶层 import ``services.cost_guard`` 触发可能
        # 的 chroma / pyyaml 顶层副作用,实际类型由 ``_cost_guard_gate``
        # 内部局部 import 验证。``ws_manager`` 类型同理为 ``WebSocketManager``
        # (``src/api/websocket/manager.py``)。
        self._cost_guard_config = cost_guard_config
        self._ws_manager = ws_manager
        # P6-B-07:每章 approved transaction 后同步调 BackupService.
        # create_auto_backup(workspace_id, chapter_id)。``None`` 时跳过
        # (CLI / 单测 legacy 不注入即 disable)。签名 ``object`` 避免顶层
        # import 循环 — 实际类型由 ``_run_auto_backup`` 局部 isinstance 验证。
        self._backup_service = backup_service
        # W3.5-C:approved 后先落 ready_for_next_chapter 硬水位,再调度富记忆
        # 加工。默认 inline 是为了保持低层 service 单测 deterministic；生产
        # API/CLI 工厂显式传 "background"。正式 Pipeline worker 会在 attempt
        # scope 显式标记同步消费同一任务，保证下一章卡读取到最新富记忆；手动
        # Chapter UI 即使附着暂停 run 也仍保持后台加工。
        self._memory_enrichment_scheduler = self._resolve_memory_enrichment_scheduler(
            memory_enrichment_scheduler
        )
        # R8:生产路径注入全局 _tasks.db 持久租约。进程崩溃后租约仍在，
        # 下一次入口会在 Provider 前硬停，不能把已付费但尚未物化的响应重发。
        # None 仅保留低层 legacy 单测/无 DB 调用方兼容。
        self._operation_lease_store = operation_lease_store

    def active_run_model_manifest_binding(self, workspace_id: str) -> Any | None:
        """Return the central manifest binding without constructing a client.

        Pipeline start uses this read-only identity to persist the exact token
        in its existing ``profile_override`` checkpoint field.  No config
        selection means legacy behaviour and returns ``None``.
        """

        if self._router_factory is None:
            return None
        router = self._router_factory(workspace_id)
        return getattr(router, "run_model_manifest_binding", None)

    def allows_word_count_tolerance(self, workspace_id: str) -> bool:
        """只读解析当前管线的字数口径，不构造或调用模型客户端。"""
        from src.core.agents.creative_delta import (
            EXPERIMENTAL_PIPELINE_CONTRACT, uses_experimental_pipeline,
        )

        router = self._router_factory(workspace_id) if self._router_factory else None
        return not (
            getattr(router, "pipeline_contract", None) == EXPERIMENTAL_PIPELINE_CONTRACT
            or (self._client is not None and uses_experimental_pipeline(self._client))
        )

    # ---- 内部辅助 ----------------------------------------------------------

    @staticmethod
    def _resolve_memory_enrichment_scheduler(
        scheduler: MemoryEnrichmentScheduler | Literal["inline", "background"],
    ) -> MemoryEnrichmentScheduler:
        """解析 W3.5-C 富记忆加工调度器。

        ``inline`` 用于单测与旧调用方,保持 write 返回前完成富加工;``background``
        用 daemon thread 让生产主链路只等待 ready 水位。测试也可传自定义
        callable 捕获任务,精确验证"已 ready 但富记忆未跑"的状态。
        """
        if scheduler == "inline":
            return lambda task: task()
        if scheduler == "background":
            return ChapterService._run_memory_enrichment_in_background
        return scheduler

    @staticmethod
    def _run_memory_enrichment_in_background(task: Callable[[], None]) -> None:
        """用 daemon thread 调度富记忆任务并继承调用方 contextvars。

        ``Thread`` 不会自动传播 structlog workspace/chapter/trace 绑定。若裸提交，
        后台 memory extractor 及其子 agent 的逐次 token/费用日志会落到 stdout，
        而非当前 workspace JSONL；Provider attempt ledger 虽仍由 router 的默认
        scope 正确记账，产品审计面却缺逐次 usage。提交点必须在当前线程先
        ``copy_context()``，让后台任务继承同一日志与 attempt 归属。
        """
        Thread(
            target=copy_context().run,
            args=(task,),
            name="chapter-memory-enrichment",
            daemon=True,
        ).start()

    def _paths(self, workspace_id: str) -> WorkspacePaths:
        """构造与注入的 ``WorkspaceService`` 同源的 ``WorkspacePaths``。"""
        return WorkspacePaths(
            workspace_id,
            workspaces_root=self._workspace_service.workspaces_root,
        )

    def _validate_card_continuity_preflight(
        self,
        *,
        workspace_id: str,
        card: ChapterCard,
        paths: WorkspacePaths,
    ) -> ChapterCard:
        """兼容既有调用方；生产链由同一实现另取瞬态 capsule。"""

        return self._prepare_card_continuity_preflight(
            workspace_id=workspace_id,
            card=card,
            paths=paths,
        ).card

    def _prepare_card_continuity_preflight(
        self,
        *,
        workspace_id: str,
        card: ChapterCard,
        paths: WorkspacePaths,
    ) -> _CardContinuityPreflight:
        """Writer/router/状态写入前复核并收敛上一章 ledger 投影与 bridge。

        旧 ``_maybe_refresh_continuity`` 曾在批准后用 rule 结果覆盖
        ``unfinished_actions`` 但保留 provenance。这里只识别“尾文/POV 已与
        ledger 一致，actions 又逐字等于同一尾文的旧 rule 输出”这一精确签名，
        再为当前 invocation 构造 ledger actions 的内存卡并复核 bridge；不写回
        文件，避免把 read/compare/write 伪装成 CAS。其他投影异常、来源/hash
        变化、ledger 不可用或 bridge 不成立仍 fail closed。
        """

        chapter_seq = chapter_seq_from_id(card.chapter_id)
        if chapter_seq is None or chapter_seq <= 1:
            return _CardContinuityPreflight(card=card, capsule=None)
        if self._session_factory is None:
            if card.continuity_ledger_provenance is not None:
                stale = ChapterCardContinuityStaleError(
                    chapter_id=card.chapter_id,
                    reason="缺少 session_factory，无法复核连续性小账本",
                    frozen=card.continuity_ledger_provenance,
                    current=None,
                )
                _log.error(
                    "continuity_ledger_gate_failed",
                    failure_layer="writer_preflight_ledger_unavailable",
                    workspace_id=workspace_id,
                    chapter_id=card.chapter_id,
                    reason=stale.reason,
                )
                raise stale
            return _CardContinuityPreflight(card=card, capsule=None)
        prev_chapter_id = f"ch-{chapter_seq - 1:04d}"
        try:
            self.require_ready_for_next_chapter(
                workspace_id,
                prev_chapter_id,
            )
            ledger = load_validated_continuity_ledger(
                workspace_id=workspace_id,
                chapter_id=prev_chapter_id,
                paths=paths,
                session_factory=self._session_factory,
            )
            current = continuity_ledger_provenance(ledger)
        except (ChapterServiceError, ContinuityLedgerValidationError) as exc:
            stale = ChapterCardContinuityStaleError(
                chapter_id=card.chapter_id,
                reason=str(exc),
                frozen=card.continuity_ledger_provenance,
                current=None,
            )
            _log.error(
                "continuity_ledger_gate_failed",
                failure_layer="writer_preflight_ledger_unavailable",
                workspace_id=workspace_id,
                chapter_id=card.chapter_id,
                previous_chapter_id=prev_chapter_id,
                reason=str(exc),
            )
            raise stale from exc

        try:
            validate_frozen_provenance(
                frozen=card.continuity_ledger_provenance,
                current=current,
            )
        except ValueError as exc:
            stale = ChapterCardContinuityStaleError(
                chapter_id=card.chapter_id,
                reason=str(exc),
                frozen=card.continuity_ledger_provenance,
                current=current,
            )
            _log.error(
                "continuity_ledger_gate_failed",
                failure_layer="writer_preflight_ledger_stale",
                workspace_id=workspace_id,
                chapter_id=card.chapter_id,
                previous_chapter_id=prev_chapter_id,
                frozen=(
                    card.continuity_ledger_provenance.model_dump(mode="json")
                    if card.continuity_ledger_provenance is not None
                    else None
                ),
                current=current.model_dump(mode="json"),
            )
            raise stale from exc

        projection_repair_reason: str | None = None
        validated_card = card
        try:
            validate_continuity_projection(
                ledger_raw_final_tail=ledger.raw_final_tail,
                ledger_unfinished_actions=ledger.unfinished_actions,
                ledger_pov_character=ledger.pov_character,
                card_prev_chapter_tail=card.continuity_context.prev_chapter_tail,
                card_unfinished_actions=card.continuity_context.unfinished_actions,
                card_pov_from=card.continuity_context.pov_from,
            )
        except ValueError as exc:
            legacy_projection = extract_from_prev_text(ledger.raw_final_tail)
            is_known_legacy_rule_overwrite = (
                card.continuity_context.prev_chapter_tail == ledger.raw_final_tail
                and card.continuity_context.pov_from == ledger.pov_character
                and card.continuity_context.unfinished_actions
                == legacy_projection["unfinished_actions"]
            )
            if not is_known_legacy_rule_overwrite:
                stale = ChapterCardContinuityStaleError(
                    chapter_id=card.chapter_id,
                    reason=str(exc),
                    frozen=card.continuity_ledger_provenance,
                    current=current,
                )
                _log.error(
                    "continuity_ledger_gate_failed",
                    failure_layer="writer_preflight_ledger_stale",
                    workspace_id=workspace_id,
                    chapter_id=card.chapter_id,
                    previous_chapter_id=prev_chapter_id,
                    frozen=(
                        card.continuity_ledger_provenance.model_dump(mode="json")
                        if card.continuity_ledger_provenance is not None
                        else None
                    ),
                    current=current.model_dump(mode="json"),
                    legacy_rule_signature_match=False,
                )
                raise stale from exc
            projection_repair_reason = str(exc)
            repaired_context = card.continuity_context.model_copy(
                update={
                    "unfinished_actions": list(ledger.unfinished_actions),
                }
            )
            validated_card = card.model_copy(
                update={"continuity_context": repaired_context}
            )
            validate_continuity_projection(
                ledger_raw_final_tail=ledger.raw_final_tail,
                ledger_unfinished_actions=ledger.unfinished_actions,
                ledger_pov_character=ledger.pov_character,
                card_prev_chapter_tail=(
                    validated_card.continuity_context.prev_chapter_tail
                ),
                card_unfinished_actions=(
                    validated_card.continuity_context.unfinished_actions
                ),
                card_pov_from=validated_card.continuity_context.pov_from,
            )

        evidence_records = [
            ContinuityEvidenceRecord.model_validate(item)
            for item in ledger.evidence_records
        ]
        try:
            validate_immediate_action_scope(
                actions=validated_card.continuity_context.unfinished_actions,
                records=evidence_records,
                frozen_indices=validated_card.continuity_context.immediate_action_indices,
            )
            validate_continuity_bridge(
                evidence_records=evidence_records,
                time_gap=validated_card.continuity_context.time_gap,
                scene_continuation=(
                    validated_card.continuity_context.scene_continuation
                ),
                continuity_bridge=(validated_card.continuity_context.continuity_bridge),
            )
        except ValueError as exc:
            bridge_error = ChapterCardContinuityBridgeError(
                chapter_id=card.chapter_id,
                reason=str(exc),
            )
            _log.error(
                "continuity_ledger_gate_failed",
                failure_layer="writer_preflight_bridge",
                workspace_id=workspace_id,
                chapter_id=card.chapter_id,
                previous_chapter_id=prev_chapter_id,
                reason=str(exc),
            )
            raise bridge_error from exc

        if projection_repair_reason is not None:
            latest_card = self.read_chapter_card(workspace_id, card.chapter_id)
            if latest_card != card:
                stale = ChapterCardContinuityStaleError(
                    chapter_id=card.chapter_id,
                    reason="确定性恢复前章节卡发生并发变化",
                    frozen=card.continuity_ledger_provenance,
                    current=current,
                )
                _log.error(
                    "continuity_ledger_gate_failed",
                    failure_layer=(
                        "writer_preflight_projection_repair_concurrent_change"
                    ),
                    workspace_id=workspace_id,
                    chapter_id=card.chapter_id,
                    previous_chapter_id=prev_chapter_id,
                )
                raise stale
            _log.warning(
                "continuity_projection_repaired",
                workspace_id=workspace_id,
                chapter_id=card.chapter_id,
                previous_chapter_id=prev_chapter_id,
                reason=projection_repair_reason,
                repaired_fields=[
                    "unfinished_actions",
                ],
                persisted=False,
                provenance=current.model_dump(mode="json"),
            )
        capsule: ProvenanceBoundContinuityCapsule | None = None
        try:
            capsule = build_provenance_bound_continuity_capsule(
                ledger,
                frozen_provenance=validated_card.continuity_ledger_provenance,
            )
            capsule.assert_matches_projection(
                frozen_provenance=validated_card.continuity_ledger_provenance,
                prev_chapter_tail=(
                    validated_card.continuity_context.prev_chapter_tail
                ),
                unfinished_actions=(
                    validated_card.continuity_context.unfinished_actions
                ),
                pov_from=validated_card.continuity_context.pov_from,
            )
        except Exception as exc:  # noqa: BLE001 - 优化层任何不可证输入都回完整 Prompt
            # 既有 Writer ledger gate 已通过；这里只关闭短版优化并在首次出网前
            # 选择完整 Prompt，不把优化层异常改写成 Provider retry 或新业务失败。
            _log.warning(
                "continuity_capsule_full_prompt_fallback",
                workspace_id=workspace_id,
                chapter_id=card.chapter_id,
                previous_chapter_id=prev_chapter_id,
                reason=str(exc),
            )
            capsule = None
        return _CardContinuityPreflight(card=validated_card, capsule=capsule)

    @staticmethod
    def _load_world_contract(
        paths: WorkspacePaths, *, required: bool
    ) -> SettingContract | None:
        """加载权威 ``world.json``；大纲路径缺失/损坏时在 LLM 前 fail closed。"""

        world_path = paths.outline_dir / "world.json"
        if not required and not world_path.is_file():
            return None
        try:
            return load_setting_contract(world_path)
        except SettingContractLoadError as exc:
            raise ChapterServiceError(str(exc)) from exc

    def _validate_card_dramatic_source_preflight(
        self,
        *,
        workspace_id: str,
        paths: WorkspacePaths,
        card: ChapterCard,
    ) -> None:
        """Writer/Reviser 前将卡片 source 与当前 outline 逐字段复核。"""

        try:
            load_and_validate_card_dramatic_source(
                card=card,
                outline_dir=paths.outline_dir,
                genre=self._workspace_service.info(workspace_id).genre,
            )
        except DramaticSourceContractError as exc:
            _log.error(
                "prewriting_semantic_gate_failed",
                failure_layer="writer_preflight_drama_stale",
                workspace_id=workspace_id,
                chapter_id=card.chapter_id,
                constraint_ids=["chapter_card_dramatic_source"],
                contract_hash=(
                    card.dramatic_source.source_hash
                    if card.dramatic_source is not None
                    else None
                ),
                attempt=0,
            )
            raise ChapterServiceError(
                f"Writer 前 typed 戏剧源复核失败:{exc}"
            ) from exc

    def _outline_contract_required(
        self, workspace_id: str, paths: WorkspacePaths
    ) -> bool:
        """已提交或已进入 outline 产品链路的工作区必须有权威合同。"""

        sibling_stages = ("characters.json", "plot.json", "volume_plans.json")
        if any((paths.outline_dir / name).is_file() for name in sibling_stages):
            return True
        if self._session_factory is None:
            return False
        session = self._session_factory(workspace_id)
        try:
            row = session.get(WorkspaceORM, workspace_id)
            return row is not None and row.outline_status == "committed"
        except Exception as exc:  # noqa: BLE001 — 状态未知时不能假定未提交
            raise ChapterServiceError(
                "无法确认大纲提交状态，拒绝在权威世界契约状态未知时调用下游"
            ) from exc
        finally:
            session.close()

    def _validate_workspace(self, workspace_id: str) -> WorkspacePaths:
        """通过 ``WorkspaceService.info`` 校验工作区合法,返回 ``WorkspacePaths``。

        ``MetaFileError`` 翻译成 ``WorkspaceServiceError``——本质是"工作区
        不存在或损坏",归 WorkspaceService 错误域,与 P1-B-14 错误一致。
        """
        try:
            self._workspace_service.info(workspace_id)
        except MetaFileError as exc:
            raise WorkspaceServiceError(
                f"工作区不存在或 meta.json 损坏: {workspace_id}: {exc}"
            ) from exc
        return self._paths(workspace_id)

    # ---- 主流水线 gate(P4-acceptance) -----------------------------------

    def _run_gates(self, workspace_id: str, chapter_id: str | None = None) -> None:
        """write / rewrite / revise 入口前置 gate(P4-acceptance + P5-B-06)。

        对齐 OBSERVABILITY §3.3 字面"成本守卫超限 / 记忆健康度 critical →
        流水线暂停" + p5_plan §"P5-B-06"字面 ``card_status != "approved"``
        gate(决策 6=C 处置)。

        三档独立判定,任一命中即 raise:

        - **cost_guard**:用本工作区 ``SUM(chapters.total_cost_usd)``
          (**累计真值账**,v2 w2-s2 metrics 修复 B 起;原 ``metrics_daily``
          当日行结构性低估 — 孤儿章不入账 + approved 后处理漏记,w2-s1
          报告 §7.1)作 ``workspace_today_cost_usd`` 入参调
          ``trigger_cost_guard_check``;``decision.action=="pause"`` 抛
          ``CostGuardTriggered``(402)。**语义 = 本工作区(本书)累计成本
          上限**,非单日 — 入参名 / config 字段名(``per_workspace_daily_
          limit``)保留是为 P6-B-15 工作区 yaml 覆盖向后兼容。跨工作区单日
          / 月度合计维度本版本留 0.0(``SystemMetricsDailyORM`` 无 cost
          字段;``MetricsService.get_total_cost_today`` 跨工作区聚合留
          v2_wishlist,对齐 P4-B-08 §8 留观项)。
        - **memory_health**:读本工作区 ``memory_health_history`` 表最新一行,
          ``severity=="critical"`` 抛 ``MemoryHealthBlocked``(409)。**只读
          不主动跑 checker**(避免每次 :write 引入 LLM 几十秒开销;新鲜度依
          赖外部 ``POST /memory/check`` 显式触发);无行(工作区从未跑过
          check)→ 放行。
        - **card_status**(P5-B-06 引入):``chapter_id`` 给定时读
          ``chapter_cards`` 表本章行,``card_status != "approved"`` 抛
          ``CardNotApproved``(409,决策 6=C);**无行**(章节卡尚未生成)→
          同样抛(对齐"无卡不能写"语义,outline:commit 批量后已写行)。
          ``chapter_id=None`` 时本 gate 跳过(向后兼容 ``revise`` 路径
          + 非章节级调用方;``revise`` 不读 chapter_id 而读 source_version_id,
          但 P5-B-06 已在 ``revise`` 主流程里同样传入 chapter_id)。
        - **style_audit**:本版本 noop(``StyleAuditor stub`` ``severity="stub"``
          永不触发 critical;P7 完整版 + ``style_audit_history.severity``
          升 4 档 + CHECK 约束接入时打开本 gate 同模式)。

        所有 gate 都在 ``self._session_factory is None`` / 配置未注入时静
        默跳过(P1/P2/P3 兼容路径不变;legacy 测试 / CLI 不传 factory 时
        gate 全 noop)。
        """
        # 局部 import 规避 errors.py ↔ chapter_service.py 循环(errors.py 已
        # 顶层 import ``ChapterServiceError``,反向若顶层 import errors 会循环;
        # 局部 import 在函数体内执行时模块已初始化,无 ImportError)。
        from src.api.errors import (
            CardNotApproved,
            CostGuardTriggered,
            MemoryHealthBlocked,
        )
        from src.services.cost_guard import (
            CostGuardConfig,
            get_workspace_cost_guard_config,
            trigger_cost_guard_check,
        )
        from src.services.attempt_ledger import AttemptLedgerStore

        if self._session_factory is None:
            return  # P1/P2 兼容路径,无 DB 不跑 gate

        # ---- cost_guard gate ------------------------------------------------
        # ``cost_guard_config=None`` 显式表"不启用"(对齐其他可选 factory
        # 语义:None=disable,非 None=enable)。生产路径 ``deps.py`` 模块级
        # 加载 ``load_cost_guard_config()`` 注入;CLI / 单测 / legacy fixture
        # 不注入即 disable,不读 yaml 也不跑 trigger,保持 P1/P2/P3 既有行为。
        # **P7-B-05 起**:enable 时通过 ``get_workspace_cost_guard_config`` 改
        # 走每工作区 lazy 缓存,生产路径 ``self._cost_guard_config``(deps.py 单例)
        # 仍作为加载失败的回退 default,保持启动期任意工作区 cost_guard 行为
        # 与 P1-P6 静态配置一致;PUT /workspaces/{id}/config 改 yaml 后由路由层
        # 调 ``reset_workspace_cost_guard_config_cache(workspace_id)`` 让新值
        # 即时生效(对齐 P6-B-15 字段热加载语义)。
        config = self._cost_guard_config
        if isinstance(config, CostGuardConfig):
            config = get_workspace_cost_guard_config(workspace_id, default=config)
            # v2 w2-s2 metrics 修复 B:读数源从 ``metrics_daily`` 当日行改为
            # ``SUM(chapters.total_cost_usd)``(工作区**累计真值账**)。
            # 为何:w2-s1 报告 §7.1 逐章对账实证 metrics_daily 结构性低估
            # (非 approved 孤儿章零入账 + approved 后处理 hook 成本漏记,
            # run-accept-v1 差额 $5.85/33%);chapters 账由 cost_tracker 对
            # 全部 LLM 调用实时累加,①② 两层漏记天然全覆盖。**语义随之
            # 从"当日成本"变"全书累计成本"**(护栏本就主防"本书烧穿预算
            # /章循环跑飞",该场景成本全在 chapters 账上;限额配置已同步
            # 调整,见仓库根 config.yaml cost_guard 段注释)。
            workspace_cost_usd = 0.0
            session = self._open_session(workspace_id)
            if session is not None:
                try:
                    # ChapterORM 用模块顶层 import(本函数后段 invalidated
                    # gate 同名引用 — 局部 import 会遮蔽成 UnboundLocalError)
                    from sqlalchemy import func as sa_func
                    from sqlalchemy import select as sa_select

                    total = session.execute(
                        sa_select(
                            sa_func.coalesce(
                                sa_func.sum(ChapterORM.total_cost_usd), 0.0
                            )
                        ).where(ChapterORM.workspace_id == workspace_id)
                    ).scalar_one()
                    workspace_cost_usd = float(total)
                finally:
                    session.close()
            attempt_cost = AttemptLedgerStore(
                self._session_factory
            ).configured_cost_total(
                workspace_id,
                currency=config.cost_currency,
            )
            workspace_cost_usd = (
                max(workspace_cost_usd, attempt_cost)
                if config.cost_currency == "USD"
                else attempt_cost
            )
            decision = trigger_cost_guard_check(
                config=config,
                daily_cost_usd=0.0,
                monthly_cost_usd=0.0,
                workspace_today_cost_usd=workspace_cost_usd,
                workspace_id=workspace_id,
                ws_manager=self._ws_manager,  # type: ignore[arg-type]
            )
            if decision.triggered and decision.action == "pause":
                raise CostGuardTriggered(
                    f"workspace {workspace_id} cost guard 触发: {decision.reason}",
                    details={
                        "scope": decision.scope,
                        "current_usd": decision.current_usd,
                        "limit_usd": decision.limit_usd,
                        "reason": decision.reason,
                        "workspace_id": workspace_id,
                        "currency": decision.currency,
                    },
                )

        # ---- memory_health gate --------------------------------------------
        session = self._open_session(workspace_id)
        if session is None:
            return
        try:
            from sqlalchemy import desc, select

            from src.core.db.models import MemoryHealthCheckORM

            stmt = (
                select(MemoryHealthCheckORM)
                .order_by(desc(MemoryHealthCheckORM.checked_at))
                .limit(1)
            )
            row = session.execute(stmt).scalar_one_or_none()
            if row is not None and row.severity == "critical":
                raise MemoryHealthBlocked(
                    f"workspace {workspace_id} 记忆健康度 critical "
                    f"(check_id={row.check_id}),流水线已阻断",
                    details={
                        "check_id": row.check_id,
                        "severity": row.severity,
                        "checked_at": row.checked_at.isoformat(),
                        "workspace_id": workspace_id,
                    },
                )
        finally:
            session.close()

        # ---- invalidated gate(P6-B-01,rewind 链上游) --------------------
        # 检测 ``chapters.status == 'invalidated'`` → 抛
        # ``ChapterInvalidatedNeedsUnblockError`` 翻译 409
        # ``CHAPTER_INVALIDATED_NEEDS_UNBLOCK``。**先于 card_status gate**:
        # rewind 链 stale 是显著阻断,user 必须先 unblock(语义优先级 > 章节卡
        # 审核状态)。
        if chapter_id is not None:
            session = self._open_session(workspace_id)
            if session is not None:
                try:
                    chapter_row = session.get(ChapterORM, chapter_id)
                finally:
                    session.close()
                if (
                    chapter_row is not None
                    and chapter_row.workspace_id == workspace_id
                    and chapter_row.status == "invalidated"
                ):
                    from src.services.chapter_rewind_service import (
                        ChapterInvalidatedNeedsUnblockError,
                    )

                    raise ChapterInvalidatedNeedsUnblockError(
                        chapter_id=chapter_id,
                        workspace_id=workspace_id,
                    )

        # ---- card_status gate(P5-B-06) ------------------------------------
        if chapter_id is None:
            return
        session = self._open_session(workspace_id)
        if session is None:
            return
        try:
            from src.core.db.models import ChapterCardORM

            card_row = session.get(ChapterCardORM, chapter_id)
        finally:
            session.close()

        if card_row is None:
            # P1-P4 向后兼容(P5-B-02 迁移注 §"既有 ChapterCard 行默认
            # approved"):章节卡 SSOT 在文件 ``data/cards/<chapter_id>.json``,
            # ``chapter_cards`` 表只是 P5 起新增的索引缓存。若文件已存在(P1-P4
            # legacy 工作区或单测 fixture 走文件)且 ORM 行尚未落 → 视为
            # legacy approved,放行;文件 + ORM 都缺 → 抛 CardNotApproved。
            paths = self._paths(workspace_id)
            legacy_card_file = paths.cards_dir / f"{chapter_id}.json"
            if not legacy_card_file.is_file():
                raise CardNotApproved(
                    f"workspace {workspace_id} chapter {chapter_id} "
                    f"章节卡尚未生成 (chapter_cards 表无行 + 文件 SSOT 缺失),"
                    f"无法触发写作",
                    details={
                        "workspace_id": workspace_id,
                        "chapter_id": chapter_id,
                        "card_status": None,
                    },
                )
            # legacy 文件存在视为 approved，但仍必须继续经过 reference / 角色
            # admission；不能用兼容分支绕过 Reviser/Writer 前的数据硬闸。
        elif card_row.card_status != "approved":
            current_status: str = card_row.card_status
            raise CardNotApproved(
                f"workspace {workspace_id} chapter {chapter_id} "
                f"章节卡未通过审核 (card_status={current_status!r}),"
                f"无法触发写作",
                details={
                    "workspace_id": workspace_id,
                    "chapter_id": chapter_id,
                    "card_status": current_status,
                },
            )

        # ---- reference_validator gate(P6-B-10,Writer 前置)----------------
        # 章节卡 approved 后,正式进入 Writer 前再做一次引用合法性校验 ——
        # 卡可能在 approve 后角色 / 伏笔 CRUD 删行(或 rewind 撤销)导致引用
        # stale。DB 表对当前 workspace 全空 → noop(legacy 兼容);任一类目
        # 有行 → 扫 card 4 字段,命中未知 ID 抛
        # ``ReferenceValidationFailedError``(翻译为 422 ``REFERENCE_VALIDATION_FAILED``)。
        # **不**自动重试 — 章节卡是持久化数据,重试 LLM 无意义;用户必须先调
        # ``:retry-references`` 端点或手动编辑卡再 ``:write``。
        self._run_reference_validation_gate(
            workspace_id=workspace_id, chapter_id=chapter_id
        )
        self._run_character_state_admission_gate(
            workspace_id=workspace_id, chapter_id=chapter_id
        )

    def _run_reference_validation_gate(
        self, *, workspace_id: str, chapter_id: str
    ) -> None:
        """Writer / Reviser / Rewrite 入口的 reference_validator 轻量预检
        (P6-B-10,对齐 plan §"P6-B-10"字面"Writer 主流程接入 hook")。

        与 ``ChapterCardService.generate_single_chapter_card`` 的 hook 区别:
        本 gate **不重试**(章节卡是持久化数据,自动重试 LLM 无意义),命中
        未知引用直接抛错;用户必须显式调 ``:retry-references`` 重新生成卡
        或手动编辑卡才能再 :write / :rewrite / :revise。

        ``session_factory=None`` / 章节卡 SSOT 文件不存在 / DB 表全空时
        noop;保证 P1-P5 legacy 测试 + 未 import 资产的工作区零回归。
        """
        if self._session_factory is None:
            return

        from src.core.agents.reference_validator import (
            collect_unknown_ids_from_card,
            load_known_ids_from_workspace,
        )

        # 读卡:先文件 SSOT(P1-P5 路径);文件不存在则 noop(无法校验)。
        paths = self._paths(workspace_id)
        card_file = paths.cards_dir / f"{chapter_id}.json"
        if not card_file.is_file():
            return
        try:
            card = self.read_chapter_card(workspace_id, chapter_id)
        except ChapterServiceError:
            # 卡文件存在但解析失败 — 让既有 card_status gate / 后续 _orchestrate
            # 抛对应错误(``CHAPTER_CARD_INVALID`` 等),本 gate noop。
            return

        session = self._session_factory(workspace_id)
        try:
            known_char_ids, known_thread_ids = load_known_ids_from_workspace(
                session, workspace_id
            )
        finally:
            session.close()
        unknown_chars, unknown_threads = collect_unknown_ids_from_card(
            card,
            known_character_ids=known_char_ids,
            known_thread_ids=known_thread_ids,
        )
        if unknown_chars or unknown_threads:
            from src.core.agents.reference_validator import (
                ReferenceValidationFailedError,
            )

            raise ReferenceValidationFailedError(
                workspace_id=workspace_id,
                chapter_id=chapter_id,
                unknown_character_ids=unknown_chars,
                unknown_thread_ids=unknown_threads,
            )

    def _run_character_state_admission_gate(
        self, *, workspace_id: str, chapter_id: str
    ) -> None:
        """所有正文 agent 调用前拦角色缺失与已死亡角色引用。"""
        if self._session_factory is None:
            return
        card = self.read_chapter_card(workspace_id, chapter_id)
        session = self._session_factory(workspace_id)
        try:
            characters = list_characters_by_workspace(session, workspace_id)
        finally:
            session.close()
        existence = check_character_existence(
            chapter_card=card,
            known_character_ids={item.character_id for item in characters},
        )
        dead_reference = check_dead_character_references(
            chapter_card=card, character_records=characters
        )
        failures = [
            result for result in (existence, dead_reference) if not result.passed
        ]
        if failures:
            raise ChapterServiceError(
                "CHAPTER_CARD_CHARACTER_STATE_INVALID:"
                "请先修章节卡/角色状态；details="
                f"{[{'name': item.name, 'details': dict(item.details)} for item in failures]}"
            )

    # ---- 审稿历史查询(B-01 hot-fix 2026-05-19)----------------------------

    def list_review_history(self, workspace_id: str, chapter_id: str) -> list[dict]:
        """从 ``review_results`` 表查章节审稿历史,返与 ``ReviewSummaryEntry``
        schema 对齐的 dict 列表(按 ``reviewed_at`` 升序)。

        替代 v1 期前端走 ``task_registry`` LRU 字典 + zustand task_id 的丢失
        路径(服务器重启 + 页面刷新都会丢)。表是 P2-B-17 起持久化的真源。

        - 工作区不存在 → 抛 ``WorkspaceServiceError``(路由翻 422 / 404)
        - 章节不存在 / 无 ``session_factory`` → 返空列表(不抛,前端能区分
          "没跑过"vs"任务过期",statefully 比基线 zustand TASK_NOT_FOUND 友好)
        - 章节存在但无 review_results 行 → 返空列表

        ``revise_targets`` 仅在 ``verdict == "revise"`` 时填充,沿用
        ``ReviewSummaryEntry`` schema 字面契约。其他 verdict 也包括了
        role=='revise_target' 行(数据库语义),但 schema 字面只 revise 必填。
        """
        # 工作区合法性校验(透传 MetaFileError → WORKSPACE_INVALID)
        self._workspace_service.info(workspace_id)
        if self._session_factory is None:
            return []
        session = self._session_factory(workspace_id)
        try:
            chapter = session.get(ChapterORM, chapter_id)
            if chapter is None or chapter.workspace_id != workspace_id:
                return []
            rows = (
                session.query(ReviewResultORM)
                .filter(ReviewResultORM.chapter_id == chapter_id)
                .order_by(ReviewResultORM.reviewed_at)
                .all()
            )
            entries: list[dict] = []
            for row in rows:
                revise_targets = [
                    {
                        "paragraph_anchor": iss.paragraph_anchor,
                        "issue_type": iss.issue_type,
                        "description": iss.description,
                        "suggestion": iss.suggestion,
                    }
                    for iss in sorted(row.issues, key=lambda i: i.position)
                    if iss.role == "revise_target"
                ]
                entries.append(
                    {
                        "version_reviewed": row.version_reviewed,
                        "verdict": row.verdict,
                        "score_overall": row.score_overall,
                        "score_plot_progression": row.score_plot_progression,
                        "score_tension_match": row.score_tension_match,
                        "score_prose_quality": row.score_prose_quality,
                        "score_consistency": row.score_consistency,
                        "score_hook_management": row.score_hook_management,
                        "score_voice_consistency": row.score_voice_consistency,
                        # wave 5 卓越性纯度量(0021 持久化):None = 未度量(旧审稿 /
                        # LLM 漏产)→ 前端 ExcellenceMetrics 优雅不渲染。
                        "score_imagery": row.score_imagery,
                        "score_interiority": row.score_interiority,
                        "score_formulaic": row.score_formulaic,
                        "score_immersion": row.score_immersion,
                        "hard_constraints_passed": row.hard_constraints_passed,
                        "constraint_violations": list(row.constraint_violations or []),
                        "constraint_results": list(row.constraint_results or []),
                        "hard_constraint_regressions": list(
                            row.hard_constraint_regressions or []
                        ),
                        "revise_targets": revise_targets,
                    }
                )
            return entries
        finally:
            session.close()

    def style_fingerprint_series(
        self, workspace_id: str, chapter_id: str
    ) -> list[StyleDriftPoint]:
        """读章节所有版本的 ``style_fingerprint`` 时序(v1.1 w3-s7 H7)。

        供前端折线图展示 v1→vN 风格漂移趋势。每个版本派生 4 个数值维度
        (段落字符均值 / 句子字符均值 / 比喻密度 / 感叹密度)+ 主导人称
        ``dominant_pov``(类别,由 ``summarize_voice_from_fingerprint`` 算)。

        - 工作区不存在 → ``WorkspaceServiceError``
        - 章节不存在 / 无 ``session_factory`` / 无版本行 → 返空列表(前端区分
          "无数据",对齐 ``get_review_summary`` 空列表语义)。
        - 某版本 ``style_fingerprint`` 为 null(G5 落地前的旧版本)→
          ``has_fingerprint=False`` + 4 维度 + ``dominant_pov`` 全 None(诚实
          标注缺数据,不伪造 0 误导漂移判断)。

        只读端点:不调 LLM,纯 SQLite 查询(对齐 ``get_review_summary`` 先例);
        ``summarize_voice_from_fingerprint`` 用项目级默认 ``third_person`` 作
        全零代词兜底(仅影响空 / 极短文本极端值,可视化场景可接受)。
        """
        self._workspace_service.info(workspace_id)
        if self._session_factory is None:
            return []
        session = self._session_factory(workspace_id)
        try:
            chapter = session.get(ChapterORM, chapter_id)
            if chapter is None or chapter.workspace_id != workspace_id:
                return []
            rows = (
                session.query(ChapterVersionORM)
                .filter(ChapterVersionORM.chapter_id == chapter_id)
                .order_by(ChapterVersionORM.version_id)
                .all()
            )
            points: list[StyleDriftPoint] = []
            for row in rows:
                fp = row.style_fingerprint
                if not fp:
                    points.append(
                        StyleDriftPoint(
                            version_id=row.version_id,
                            creation_type=row.creation_type,
                            has_fingerprint=False,
                        )
                    )
                    continue
                summary = summarize_voice_from_fingerprint(fp)
                points.append(
                    StyleDriftPoint(
                        version_id=row.version_id,
                        creation_type=row.creation_type,
                        has_fingerprint=True,
                        paragraph_length_mean=summary.paragraph_length_mean,
                        sentence_length_mean=summary.sentence_length_mean,
                        simile_density=summary.simile_density,
                        exclamation_density=float(fp.get("exclamation_density", 0.0)),
                        dominant_pov=summary.dominant_pov,
                    )
                )
            return points
        finally:
            session.close()

    # ---- chapters 表状态同步(P2-B-17) ------------------------------------

    def _open_session(self, workspace_id: str) -> Session | None:
        """打开 ``chapters`` 表 session;``session_factory=None`` 返 None。

        调用方 ``with self._open_session(ws) as session:`` 直接判 ``None``
        分支兜底(不抛错——P1 兼容模式)。
        """
        if self._session_factory is None:
            return None
        return self._session_factory(workspace_id)

    def _read_chapter_state(
        self, workspace_id: str, chapter_id: str
    ) -> ChapterStateValue | None:
        """读 ``chapters.status``;无行 / 无 session_factory 返 None。

        无行场景:章节卡已建但尚未跑过 write/rewrite——调用方走文件系统
        fallback 推断 7 档(``_fs_derive_state``)。
        """
        session = self._open_session(workspace_id)
        if session is None:
            return None
        try:
            row = session.get(ChapterORM, chapter_id)
            if row is None or row.workspace_id != workspace_id:
                return None
            return row.status  # type: ignore[return-value]
        finally:
            session.close()

    def _read_authoritative_budget(
        self,
        workspace_id: str,
        chapter_id: str,
    ) -> _ChapterBudget:
        """只读 immutable ``chapter_versions`` 预算，不修正任何查询投影。"""

        session = self._open_session(workspace_id)
        if session is None:
            return _ChapterBudget(
                current_version=0,
                rewrite_count=0,
                revise_count=0,
            )
        try:
            row = session.get(ChapterORM, chapter_id)
            if row is not None and row.workspace_id != workspace_id:
                raise ChapterServiceError(
                    "章节预算 workspace checkpoint 串位: "
                    f"chapter_id={chapter_id} row.workspace_id={row.workspace_id}"
                )
            return _derive_chapter_budget(session, chapter_id)
        except Exception:
            session.rollback()
            raise
        finally:
            session.close()

    def _rewind_requires_content_rewrite(
        self,
        workspace_id: str,
        chapter_id: str,
        *,
        current_version: int,
    ) -> bool:
        """识别 rewind 后仍绑定原源版本的“必须先重写”意图。

        ``rewind`` 保留版本与审稿历史；普通 crash/reset-stale 又必须允许复用完整
        审稿 checkpoint。两者只能靠 rewind 同事务写下的精确源版本标记区分。
        标记源版本等于当前权威版本时返回 True；``drafting`` 是正常 rewind
        起点，``rewriting`` 只覆盖一次任务已进编排、却在 Provider attempt 建立前
        fail-closed 的同源重试。API 预留租约、``_acquire_chapter_operation_state``
        和 AttemptLedger 恢复闸仍先拦活动租约及 unknown Provider 现场。若 Writer
        已落新版本，标记过期并返回 False，让 ``:write`` 继续审新版本，避免重复
        Writer 请求。
        """

        if self._session_factory is None or current_version < 1:
            return False
        session = self._open_session(workspace_id)
        if session is None:
            return False
        try:
            row = session.get(ChapterORM, chapter_id)
            if row is None or row.workspace_id != workspace_id:
                return False
            details = row.escalation_details
            if (
                row.status
                not in {
                    ChapterState.DRAFTING.value,
                    ChapterState.REWRITING.value,
                }
                or not isinstance(details, dict)
                or details.get("reason") != REWIND_REWRITE_REQUIRED_REASON
            ):
                return False
            source_version_id = details.get("source_version_id")
            if (
                isinstance(source_version_id, bool)
                or not isinstance(source_version_id, int)
                or source_version_id < 1
            ):
                raise RewriteCheckpointRecoveryError(
                    "rewind 重写意图 checkpoint 损坏，已在 Provider 前停止: "
                    f"{chapter_id}/source_version_id={source_version_id!r}"
                )
            if row.current_version != current_version:
                raise RewriteCheckpointRecoveryError(
                    "rewind 重写意图与章节版本投影漂移，已在 Provider 前停止: "
                    f"{chapter_id}/row-v{row.current_version}/ledger-v{current_version}"
                )
            if source_version_id > current_version:
                raise RewriteCheckpointRecoveryError(
                    "rewind 重写意图指向未来版本，已在 Provider 前停止: "
                    f"{chapter_id}/source-v{source_version_id}/current-v{current_version}"
                )
            if source_version_id < current_version:
                _log.info(
                    "rewind_rewrite_intent_superseded_by_new_version",
                    workspace_id=workspace_id,
                    chapter_id=chapter_id,
                    source_version_id=source_version_id,
                    current_version=current_version,
                )
                return False
            return True
        finally:
            session.close()

    def prepare_failed_first_review_replay(
        self,
        workspace_id: str,
        chapter_id: str,
        *,
        failed_task_id: str,
    ) -> dict[str, Any]:
        """After an externally verified failed task, retain v1 and reopen review.

        This maintenance transition never replays Writer.  It requires a single
        completed Writer and Reviewer, no final review row, no unresolved Provider
        attempts, and byte-identical DB/disk prose.  The caller must separately
        verify that the durable task registry marks ``failed_task_id`` failed.
        """
        if self._session_factory is None or self._operation_lease_store is None:
            raise RewriteCheckpointRecoveryError("首稿审稿恢复缺少持久化账本")
        paths = self._validate_workspace(workspace_id)
        card = self.read_chapter_card(workspace_id, chapter_id)
        session = self._open_session(workspace_id)
        if session is None:
            raise RewriteCheckpointRecoveryError("首稿审稿恢复无法打开工作区账本")
        try:
            self._operation_lease_store.begin_immediate(session)
            chapter = session.get(ChapterORM, chapter_id)
            lease_row = session.get(ChapterOperationLeaseORM, (workspace_id, chapter_id))
            card_row = session.get(ChapterCardORM, chapter_id)
            versions = list(session.scalars(
                select(ChapterVersionORM)
                .where(ChapterVersionORM.chapter_id == chapter_id)
                .order_by(ChapterVersionORM.version_id)
            ))
            reviews = list(session.scalars(
                select(ReviewResultORM)
                .where(ReviewResultORM.chapter_id == chapter_id)
            ))
            if (
                chapter is None or chapter.workspace_id != workspace_id
                or chapter.status != ChapterState.REVIEWING.value
                or chapter.current_version != 1 or chapter.approved_at is not None
                or chapter.ready_for_next_chapter_at is not None
                or chapter.memory_enrichment_status != "pending"
                or chapter.escalation_details is not None
                or card_row is None or card_row.workspace_id != workspace_id
                or card_row.card_status != "approved" or card.card_status != "approved"
                or lease_row is None or lease_row.task_id != failed_task_id
                or lease_row.run_id is not None or lease_row.operation_kind != "write"
                or lease_row.phase != "provider_capable_pipeline"
                or lease_row.source_version != 0
                or len(versions) != 1 or versions[0].version_id != 1
                or versions[0].creation_type != "draft" or reviews
            ):
                raise RewriteCheckpointRecoveryError(
                    "首稿审稿恢复现场形状不符，已保留原租约"
                )
            version = versions[0]
            disk_versions = collect_canonical_version_files(paths.chapter_dir(chapter_id))
            final_path = paths.chapter_version_file(chapter_id, "final.md")
            if (
                set(disk_versions) != {1}
                or disk_versions[1].read_text(encoding="utf-8") != version.content
                or final_path.exists()
            ):
                raise RewriteCheckpointRecoveryError(
                    "首稿审稿恢复磁盘/正文账本不符，已保留原租约"
                )
            attempts = list(session.scalars(
                select(AttemptLedgerORM)
                .where(AttemptLedgerORM.workspace_id == workspace_id)
                .where(AttemptLedgerORM.chapter_id == chapter_id)
                .where(AttemptLedgerORM.attempt_kind == "provider")
                .where(AttemptLedgerORM.created_at >= version.created_at)
                .order_by(AttemptLedgerORM.created_at)
            ))
            writer = [a for a in attempts if a.agent_name == "writer"]
            reviewer = [a for a in attempts if a.agent_name == "reviewer"]
            if (
                len(writer) != 0  # Writer completed before v1 was materialized.
                or len(reviewer) != 1
                or reviewer[0].created_at < version.created_at
                or any(
                    a.task_id != failed_task_id or a.run_id is not None
                    or a.operation_kind != "agent_call"
                    or a.agent_name not in ({"reviewer"} | _POST_REVIEW_GATE_AGENTS)
                    or a.status != "succeeded" or a.usage_status != "known"
                    or a.completed_at is None
                    for a in attempts
                )
            ):
                raise RewriteCheckpointRecoveryError(
                    "首稿审稿恢复 Provider 账本不符，已保留原租约"
                )
            writer_attempts = list(session.scalars(
                select(AttemptLedgerORM)
                .where(AttemptLedgerORM.workspace_id == workspace_id)
                .where(AttemptLedgerORM.chapter_id == chapter_id)
                .where(AttemptLedgerORM.task_id == failed_task_id)
                .where(AttemptLedgerORM.agent_name == "writer")
            ))
            if (
                len(writer_attempts) != 1
                or writer_attempts[0].status != "succeeded"
                or writer_attempts[0].usage_status != "known"
                or writer_attempts[0].completed_at is None
                or writer_attempts[0].completed_at > version.created_at
            ):
                raise RewriteCheckpointRecoveryError(
                    "首稿审稿恢复 Writer 账本不符，已保留原租约"
                )
            marker = {
                "reason": FAILED_FIRST_REVIEW_REPLAY_REASON,
                "source_task_id": failed_task_id,
                "source_lease_id": lease_row.lease_id,
                "version_reviewed": 1,
                "content_sha256": hashlib.sha256(version.content.encode("utf-8")).hexdigest(),
                "reviewer_attempt_id": reviewer[0].attempt_id,
                "attempt_fingerprint": self._provider_attempt_fingerprint(attempts),
                "classified_at": datetime.now(timezone.utc).isoformat(),
            }
            lease = ChapterOperationLeaseStore._to_value(lease_row)
            chapter.escalation_details = marker
            self._operation_lease_store.release_in_session(session, lease)
            session.commit()
            return marker
        except Exception:
            session.rollback()
            raise
        finally:
            session.close()

    def _cancelled_review_replay_task_id(
        self,
        workspace_id: str,
        chapter_id: str,
        *,
        current_version: int,
    ) -> str | None:
        """验真冷启动标记，返回唯一获准重放 Reviewer 的旧 task id。

        标记只授权对当前不可变正文重跑 Reviewer 与后续 gates；不授权 Writer、
        新版本、跳过 schema/HARD 或复用旧内存 verdict。
        """

        if self._session_factory is None or current_version < 1:
            return None
        if current_version == 1:
            return self._failed_first_review_replay_task_id(workspace_id, chapter_id)
        session = self._open_session(workspace_id)
        if session is None:
            return None
        try:
            chapter = session.get(ChapterORM, chapter_id)
            details = chapter.escalation_details if chapter is not None else None
            if (
                chapter is None
                or chapter.workspace_id != workspace_id
                or chapter.status != ChapterState.REVIEWING.value
                or chapter.current_version != current_version
                or chapter.ready_for_next_chapter_at is not None
                or not isinstance(details, dict)
                or details.get("reason") != CANCELLED_REVIEW_REPLAY_REASON
                or details.get("version_reviewed") != current_version
                or details.get("source_version_id") != current_version - 1
            ):
                return None
            required_strings = {
                key: details.get(key)
                for key in (
                    "source_task_id",
                    "source_lease_id",
                    "run_id",
                    "content_sha256",
                    "reviewer_attempt_id",
                    "cancelled_gate_attempt_id",
                    "attempt_fingerprint",
                )
            }
            if any(
                not isinstance(value, str) or not value
                for value in required_strings.values()
            ):
                raise RewriteCheckpointRecoveryError(
                    "受控重审 checkpoint 字段缺失，已在 Provider 前停止: "
                    f"{chapter_id}"
                )
            source_task_id = required_strings["source_task_id"]
            run_id = required_strings["run_id"]
            assert isinstance(source_task_id, str)
            assert isinstance(run_id, str)
            if details.get("source_operation_kind") != "write":
                raise RewriteCheckpointRecoveryError(
                    "受控重审 checkpoint operation 错位，已在 Provider 前停止: "
                    f"{chapter_id}"
                )
            current = session.get(ChapterVersionORM, (chapter_id, current_version))
            source = session.get(
                ChapterVersionORM,
                (chapter_id, current_version - 1),
            )
            if (
                current is None
                or source is None
                or current.creation_type != "rewrite"
                or hashlib.sha256(current.content.encode("utf-8")).hexdigest()
                != required_strings["content_sha256"]
                or session.scalar(
                    select(ReviewResultORM)
                    .where(ReviewResultORM.chapter_id == chapter_id)
                    .where(ReviewResultORM.version_reviewed == current_version)
                    .limit(1)
                )
                is not None
            ):
                raise RewriteCheckpointRecoveryError(
                    "受控重审正文或审稿账本已变化，已在 Provider 前停止: "
                    f"{chapter_id}/v{current_version}"
                )
            attempts = list(
                session.scalars(
                    select(AttemptLedgerORM)
                    .where(
                        AttemptLedgerORM.workspace_id == workspace_id,
                        AttemptLedgerORM.chapter_id == chapter_id,
                        AttemptLedgerORM.task_id == source_task_id,
                        AttemptLedgerORM.attempt_kind == "provider",
                    )
                    .order_by(AttemptLedgerORM.created_at)
                )
            )
            if (
                not attempts
                or self._provider_attempt_fingerprint(attempts)
                != required_strings["attempt_fingerprint"]
                or not any(
                    item.attempt_id == required_strings["reviewer_attempt_id"]
                    and item.agent_name == "reviewer"
                    and item.status == "succeeded"
                    for item in attempts
                )
                or not any(
                    item.attempt_id == required_strings["cancelled_gate_attempt_id"]
                    and item.agent_name in _POST_REVIEW_GATE_AGENTS
                    and item.status == "failed"
                    and item.error_type == "LLMCancelledError"
                    for item in attempts
                )
                or any(item.status == "started" for item in attempts)
            ):
                raise RewriteCheckpointRecoveryError(
                    "受控重审 attempt 账本已变化，已在 Provider 前停止: "
                    f"{chapter_id}/task={source_task_id}"
                )
            run = session.get(PipelineRunORM, run_id)
            if (
                run is None
                or run.workspace_id != workspace_id
                or run.state not in {"paused", "interrupted"}
                or run.current_chapter != chapter_id
                or chapter_id in set(run.completed_chapter_ids)
                or chapter_id in set(run.skipped_chapter_ids)
            ):
                raise RewriteCheckpointRecoveryError(
                    "受控重审与原 pipeline checkpoint 不一致，已在 Provider 前停止: "
                    f"{chapter_id}/run={run_id}"
                )
            try:
                paths = self._validate_workspace(workspace_id)
                disk_content = ChapterVersioning(paths).read_version_content(
                    chapter_id,
                    current_version,
                )
                final_path = paths.chapter_version_file(chapter_id, "final.md")
                if disk_content != current.content or (
                    final_path.is_file()
                    and final_path.read_text(encoding="utf-8") != source.content
                ):
                    raise RewriteCheckpointRecoveryError(
                        "受控重审磁盘正文/final 指针已变化，已在 Provider 前停止: "
                        f"{chapter_id}/v{current_version}"
                    )
            except (OSError, UnicodeDecodeError, ChapterVersioningError) as exc:
                raise RewriteCheckpointRecoveryError(
                    "受控重审磁盘 checkpoint 无法验真，已在 Provider 前停止: "
                    f"{chapter_id}/v{current_version}"
                ) from exc
            return source_task_id
        finally:
            session.close()

    def _failed_first_review_replay_task_id(
        self, workspace_id: str, chapter_id: str
    ) -> str | None:
        """Revalidate the maintenance marker before allowing v1 Reviewer replay."""
        session = self._open_session(workspace_id)
        if session is None:
            return None
        try:
            chapter = session.get(ChapterORM, chapter_id)
            details = chapter.escalation_details if chapter is not None else None
            if not isinstance(details, dict) or details.get("reason") != FAILED_FIRST_REVIEW_REPLAY_REASON:
                return None
            task_id = details.get("source_task_id")
            attempt_id = details.get("reviewer_attempt_id")
            version = session.get(ChapterVersionORM, (chapter_id, 1))
            if (
                chapter is None or chapter.workspace_id != workspace_id
                or chapter.status not in {
                    ChapterState.REVIEWING.value,
                    ChapterState.DRAFTING.value,
                }
                or chapter.current_version != 1
                or chapter.ready_for_next_chapter_at is not None
                or not isinstance(task_id, str) or not task_id
                or not isinstance(attempt_id, str) or not attempt_id
                or version is None or version.creation_type != "draft"
                or details.get("version_reviewed") != 1
                or hashlib.sha256(version.content.encode("utf-8")).hexdigest()
                != details.get("content_sha256")
                or session.scalar(
                    select(ReviewResultORM).where(ReviewResultORM.chapter_id == chapter_id)
                ) is not None
            ):
                raise RewriteCheckpointRecoveryError(
                    "首稿受控重审标记或正文已变化，已在 Provider 前停止"
                )
            attempts = list(session.scalars(
                select(AttemptLedgerORM)
                .where(AttemptLedgerORM.workspace_id == workspace_id)
                .where(AttemptLedgerORM.chapter_id == chapter_id)
                .where(AttemptLedgerORM.task_id == task_id)
                .where(AttemptLedgerORM.attempt_kind == "provider")
                .where(AttemptLedgerORM.created_at >= version.created_at)
            ))
            if (
                self._provider_attempt_fingerprint(attempts)
                != details.get("attempt_fingerprint")
                or len([a for a in attempts if a.agent_name == "reviewer"]) != 1
                or not any(
                    a.attempt_id == attempt_id and a.agent_name == "reviewer"
                    and a.status == "succeeded" and a.usage_status == "known"
                    for a in attempts
                )
                or any(a.status != "succeeded" or a.usage_status != "known" for a in attempts)
            ):
                raise RewriteCheckpointRecoveryError(
                    "首稿受控重审 Provider 账本已变化，已在 Provider 前停止"
                )
            paths = self._validate_workspace(workspace_id)
            if ChapterVersioning(paths).read_version_content(chapter_id, 1) != version.content:
                raise RewriteCheckpointRecoveryError(
                    "首稿受控重审磁盘正文已变化，已在 Provider 前停止"
                )
            return task_id
        finally:
            session.close()

    def _is_legacy_no_progress_rewrite_checkpoint(
        self,
        workspace_id: str,
        chapter_id: str,
    ) -> bool:
        """识别修复上线前已落盘的相邻 GLOBAL no-op rewrite 现场。

        仅当章节正因 ``repeated_hard_constraint_no_progress`` 停在人工态、最新
        版本是 rewrite、最新两版正文只有空白差异且最新审稿未通过时返回真。
        这是窄恢复分类，不会复活普通预算耗尽、用户暂停或已通过章节。
        """

        session = self._open_session(workspace_id)
        if session is None:
            return False
        try:
            chapter = session.get(ChapterORM, chapter_id)
            if (
                chapter is None
                or chapter.workspace_id != workspace_id
                or chapter.status != ChapterState.HUMAN_INTERVENTION.value
                or not isinstance(chapter.escalation_details, dict)
                or chapter.escalation_details.get("reason")
                != "repeated_hard_constraint_no_progress"
            ):
                return False
            versions = (
                session.execute(
                    select(ChapterVersionORM)
                    .where(ChapterVersionORM.chapter_id == chapter_id)
                    .order_by(ChapterVersionORM.version_id.desc())
                    .limit(2)
                )
                .scalars()
                .all()
            )
            if len(versions) != 2:
                return False
            latest, previous = versions
            if (
                latest.creation_type != "rewrite"
                or latest.model_used == PARAGRAPH_REFLOW_MODEL_USED
                or chapter.escalation_details.get("version_reviewed")
                != latest.version_id
                or not prose_content_equivalent(previous.content, latest.content)
            ):
                return False
            latest_review = session.execute(
                select(ReviewResultORM)
                .where(
                    ReviewResultORM.chapter_id == chapter_id,
                    ReviewResultORM.version_reviewed == latest.version_id,
                )
                .order_by(ReviewResultORM.reviewed_at.desc())
                .limit(1)
            ).scalar_one_or_none()
            recoverable = (
                latest_review is not None and latest_review.verdict != "pass"
            )
            if recoverable:
                _log.info(
                    "legacy_global_no_progress_checkpoint_recoverable",
                    workspace_id=workspace_id,
                    chapter_id=chapter_id,
                    source_version_id=latest.version_id,
                    duplicate_of_version_id=previous.version_id,
                )
            return recoverable
        finally:
            session.close()

    def _sync_authoritative_budget_projection(
        self,
        workspace_id: str,
        chapter_id: str,
        *,
        expected: _ChapterBudget,
    ) -> None:
        """完整 checkpoint 验真后，按未变化的版本账本修正章节查询投影。"""
        session = self._open_session(workspace_id)
        if session is None:
            return
        try:
            current = _derive_chapter_budget(session, chapter_id)
            if current != expected:
                raise RewriteCheckpointRecoveryError(
                    "显式 Rewrite 的版本预算在验真后发生变化，已阻止状态写入: "
                    f"{chapter_id}/expected={expected}/actual={current}"
                )
            row = session.get(ChapterORM, chapter_id)
            if row is None and expected.current_version == 0:
                return
            if row is None or row.workspace_id != workspace_id:
                raise RewriteCheckpointRecoveryError(
                    f"显式 Rewrite 的章节投影缺失或串位: {chapter_id}"
                )
            row.current_version = current.current_version
            row.rewrite_count = current.rewrite_count
            row.revise_count = current.revise_count
            session.commit()
        except Exception:
            session.rollback()
            raise
        finally:
            session.close()

    def _reserve_chapter_operation_lease(
        self,
        *,
        workspace_id: str,
        chapter_id: str,
        operation: str,
        source_version: int,
    ) -> ChapterOperationLease | None:
        """在任何章节状态/文件/Provider 副作用前取得持久唯一租约。"""

        if self._operation_lease_store is None:
            return None
        scope = current_attempt_scope()
        task_id = scope.task_id if scope is not None else f"task_{uuid.uuid4().hex}"
        run_id = scope.run_id if scope is not None else None
        existing = self._operation_lease_store.get(workspace_id, chapter_id)
        if existing is not None:
            if (
                existing.task_id == task_id
                and existing.operation_kind == operation
                and existing.source_version == source_version
                and existing.run_id == run_id
                and existing.phase == "reserved_before_provider"
            ):
                return existing
            raise RewriteCheckpointRecoveryError(
                str(ChapterOperationLeaseConflict(existing))
            )
        try:
            return self._operation_lease_store.acquire(
                workspace_id=workspace_id,
                chapter_id=chapter_id,
                operation_kind=operation,
                source_version=source_version,
                task_id=task_id,
                run_id=run_id,
            )
        except (ChapterOperationLeaseConflict, ChapterOperationLeaseError) as exc:
            raise RewriteCheckpointRecoveryError(str(exc)) from exc

    def reserve_operation_task(
        self,
        workspace_id: str,
        chapter_id: str,
        *,
        operation: str,
        task_id: str,
    ) -> str | None:
        """API 在创建 BackgroundTask 前原子预约一章，拒绝重复 Worker。"""

        if self._operation_lease_store is None:
            return None
        self._run_gates(workspace_id, chapter_id=chapter_id)
        budget = self._read_authoritative_budget(workspace_id, chapter_id)
        try:
            lease = self._operation_lease_store.acquire(
                workspace_id=workspace_id,
                chapter_id=chapter_id,
                operation_kind=operation,
                source_version=budget.current_version,
                task_id=task_id,
                run_id=None,
            )
        except (ChapterOperationLeaseConflict, ChapterOperationLeaseError) as exc:
            raise RewriteCheckpointRecoveryError(str(exc)) from exc
        return lease.lease_id

    def complete_operation_task(
        self,
        workspace_id: str,
        chapter_id: str,
        *,
        task_id: str,
    ) -> None:
        """外层任务账本公布 ``done`` 前，精确释放已完成的章节租约。"""

        if self._operation_lease_store is None:
            return
        lease = self._operation_lease_store.get(workspace_id, chapter_id)
        if lease is None:
            return
        if lease.task_id != task_id or lease.phase != "operation_complete":
            raise RewriteCheckpointRecoveryError(
                "章节操作尚未完整物化或 task 不匹配，禁止释放安全租约: "
                f"chapter={chapter_id} lease_task={lease.task_id} "
                f"caller_task={task_id} phase={lease.phase}"
            )
        self._operation_lease_store.release(lease)

    def release_operation_task_if_never_dispatched(
        self,
        workspace_id: str,
        chapter_id: str,
        *,
        task_id: str,
    ) -> bool:
        """取消/本地失败时只在“零 Provider attempt + 零新版本”下释放。

        允许 store 认定的早期 phase；一旦已有 attempt、正文版本或更晚阶段，
        原租约原样保留，避免把用户取消误当成“请求肯定没发出去”。
        """

        if self._operation_lease_store is None:
            return True
        lease = self._operation_lease_store.get(workspace_id, chapter_id)
        if lease is None:
            return True
        if lease.task_id != task_id:
            return False
        try:
            paths = self._paths(workspace_id)
            disk_latest = ChapterVersioning(paths).next_version_id(chapter_id) - 1
            safe = self._operation_lease_store.lease_has_no_materialized_work(
                lease,
                disk_latest_version=disk_latest,
            )
            if not safe:
                safe = self._operation_lease_has_recoverable_checkpoint(
                    lease,
                    paths=paths,
                )
        except Exception:  # noqa: BLE001 — 无法验真就必须保留租约
            return False
        if not safe:
            return False
        return self._operation_lease_store.release_if_phase(
            lease,
            expected_phase=lease.phase,
        )

    def require_no_active_operation_lease(
        self, workspace_id: str, chapter_id: str
    ) -> None:
        """Pipeline/改源入口创建 Worker 前拦截任何未对账持久租约。"""

        if self._operation_lease_store is None:
            return
        lease = self._operation_lease_store.get(workspace_id, chapter_id)
        if lease is None:
            return
        raise RewriteCheckpointRecoveryError(
            "章节存在未完成或尚未对账的持久操作租约，禁止创建新 Worker: "
            f"chapter={chapter_id} operation={lease.operation_kind} "
            f"task={lease.task_id} phase={lease.phase}"
        )

    def reconcile_operation_leases_on_startup(
        self,
        workspace_id: str,
        *,
        apply: bool = True,
        cancelled_task_check: CancelledTaskCheckpointCheck | None = None,
    ) -> tuple[list[str], list[str]]:
        """冷启动按持久产物分类孤儿租约；只释放可严格续跑的现场。

        释放只代表允许用户从原产品 checkpoint 再进入，不会在启动期自动
        调 Provider。任一未知 attempt、正文/ledger/hash 不一致或无法验真都
        保留原租约并硬拦。
        """

        if self._operation_lease_store is None:
            return [], []
        paths = self._validate_workspace(workspace_id)
        released: list[str] = []
        retained: list[str] = []
        for lease in self._operation_lease_store.list_workspace(workspace_id):
            self._require_operation_lease_relational_binding(lease)
            if (
                lease.phase == "operation_complete"
                and self._operation_complete_lease_is_safe(
                    lease,
                    apply=apply,
                )
            ):
                if apply:
                    self._operation_lease_store.release(lease)
                released.append(lease.chapter_id)
                continue
            disk_latest = ChapterVersioning(paths).next_version_id(lease.chapter_id) - 1
            if self._operation_lease_store.lease_has_no_materialized_work(
                lease,
                disk_latest_version=disk_latest,
            ):
                if apply:
                    self._operation_lease_store.release(lease)
                released.append(lease.chapter_id)
                continue
            if self._reconcile_local_source_mutation_lease(
                lease,
                paths=paths,
                apply=apply,
            ):
                if apply:
                    self._operation_lease_store.release(lease)
                released.append(lease.chapter_id)
                continue
            cancelled_replay_marker = self._cancelled_post_review_replay_marker(
                lease,
                paths=paths,
                cancelled_task_check=cancelled_task_check,
            )
            if cancelled_replay_marker is not None:
                if apply:
                    assert cancelled_task_check is not None
                    if not self._mark_cancelled_post_review_replay_and_release(
                        lease,
                        paths=paths,
                        cancelled_task_check=cancelled_task_check,
                    ):
                        retained.append(lease.chapter_id)
                        continue
                released.append(lease.chapter_id)
                continue
            if self._reconcile_approved_finalization_lease(
                lease,
                paths=paths,
                apply=apply,
            ):
                if apply:
                    self._operation_lease_store.release(lease)
                released.append(lease.chapter_id)
                continue
            if self._operation_lease_has_recoverable_checkpoint(
                lease,
                paths=paths,
            ):
                if apply:
                    self._operation_lease_store.release(lease)
                released.append(lease.chapter_id)
                continue
            retained.append(lease.chapter_id)
            if apply:
                _log.error(
                    "chapter_operation_lease_retained_on_startup",
                    workspace_id=workspace_id,
                    chapter_id=lease.chapter_id,
                    operation=lease.operation_kind,
                    task_id=lease.task_id,
                    run_id=lease.run_id,
                    phase=lease.phase,
                    source_version=lease.source_version,
                    automatic_provider_replay=False,
                )
        return released, retained

    def _require_operation_lease_relational_binding(
        self,
        lease: ChapterOperationLease,
    ) -> None:
        """验真 lease 与可恢复 Pipeline checkpoint 的关系语义。

        ``run_id=None`` 是独立 Chapter UI 操作的合法形状。只要 lease 声称
        归属某个 run，就必须能在同一工作区找到该 run，并且章节仍位于该
        run 的唯一可恢复 checkpoint；否则不能把语法合法但串账的 lease 当成
        “零调用早期现场”自动释放。
        """

        if lease.run_id is None:
            return
        session = self._open_session(lease.workspace_id)
        if session is None:
            raise RewriteCheckpointRecoveryError(
                "持久操作租约声明 run_id，但无法打开 DB 验真关系绑定: "
                f"run={lease.run_id} chapter={lease.chapter_id}"
            )
        try:
            run = session.get(PipelineRunORM, lease.run_id)
            if run is None or run.workspace_id != lease.workspace_id:
                raise RewriteCheckpointRecoveryError(
                    "持久操作租约指向缺失或跨工作区的 pipeline run，保留现场: "
                    f"run={lease.run_id} chapter={lease.chapter_id}"
                )
            chapter_ids = run.chapter_ids
            completed_ids = run.completed_chapter_ids
            skipped_ids = run.skipped_chapter_ids
            if not all(
                isinstance(values, list)
                and all(
                    isinstance(value, str) and bool(value.strip()) for value in values
                )
                and len(values) == len(set(values))
                for values in (chapter_ids, completed_ids, skipped_ids)
            ):
                raise RewriteCheckpointRecoveryError(
                    "持久操作租约关联的 pipeline 章节账本形状损坏，保留现场: "
                    f"run={lease.run_id}"
                )
            scope = set(chapter_ids)
            completed = set(completed_ids)
            skipped = set(skipped_ids)
            if (
                not scope
                or not completed.issubset(scope)
                or not skipped.issubset(scope)
                or completed & skipped
                or lease.chapter_id not in scope
            ):
                raise RewriteCheckpointRecoveryError(
                    "持久操作租约关联的 pipeline 章节范围损坏，保留现场: "
                    f"run={lease.run_id} chapter={lease.chapter_id}"
                )
            dispositioned = lease.chapter_id in completed | skipped
            if dispositioned:
                if lease.phase != "operation_complete":
                    raise RewriteCheckpointRecoveryError(
                        "pipeline 已处置章节却保留非终态操作租约，保留现场: "
                        f"run={lease.run_id} chapter={lease.chapter_id} "
                        f"phase={lease.phase}"
                    )
                return
            if run.current_chapter != lease.chapter_id:
                raise RewriteCheckpointRecoveryError(
                    "持久操作租约不属于 pipeline 当前 checkpoint，保留现场: "
                    f"run={lease.run_id} lease.chapter={lease.chapter_id} "
                    f"run.current={run.current_chapter!r}"
                )
            if run.state in {"running", "paused", "interrupted"}:
                return
            details = run.error_details
            if (
                run.state == "error"
                and isinstance(details, dict)
                and details.get("error_type") == "ContinuityLedgerNotReadyError"
            ):
                return
            if run.state == "stopped" and lease.phase == "operation_complete":
                return
            raise RewriteCheckpointRecoveryError(
                "持久操作租约关联的 pipeline 状态不可恢复，保留现场: "
                f"run={lease.run_id} state={run.state!r} phase={lease.phase}"
            )
        finally:
            session.close()

    def _reconcile_local_source_mutation_lease(
        self,
        lease: ChapterOperationLease,
        *,
        paths: WorkspacePaths,
        apply: bool = True,
    ) -> bool:
        """完成可由持久本地产物唯一重建的 rewind/manual-edit crash。

        这里只补 SQLite/文件/本地向量索引，不调用 Provider。无法逐项证明时
        返回 False，调用方保留租约现场。
        """

        if lease.operation_kind == "rewind":
            if lease.phase != "source_mutation_committed":
                return False
            session = self._open_session(lease.workspace_id)
            if session is None:
                return False
            try:
                row = session.get(ChapterORM, lease.chapter_id)
                workspace_row = session.get(WorkspaceORM, lease.workspace_id)
                if (
                    row is None
                    or row.workspace_id != lease.workspace_id
                    or row.status != ChapterState.DRAFTING.value
                    or row.ready_for_next_chapter_at is not None
                    or workspace_row is None
                ):
                    return False
                db_completed = workspace_row.completed_chapters
            finally:
                session.close()
            if not apply:
                return True
            try:
                if self._vector_store_factory is None:
                    return False
                vector_store = self._vector_store_factory(lease.workspace_id)
                vector_store.soft_delete_by_chapter_id(
                    DEFAULT_COLLECTION,
                    chapter_id=lease.chapter_id,
                )
                meta = self._workspace_service.info(lease.workspace_id)
                self._workspace_service.sync_completed_chapters_to_meta(
                    lease.workspace_id,
                    delta=db_completed - meta.completed_chapters,
                )
                self._operation_lease_store.mark_phase(
                    lease,
                    phase="operation_complete",
                )
            except Exception:  # noqa: BLE001 — 本地副作用未闭合即保留
                return False
            return True

        if lease.operation_kind == "switch-version":
            target_match = re.fullmatch(
                r"local_final_mutation:v([1-9][0-9]*)",
                lease.phase,
            )
            if target_match is None:
                # 旧 phase 没有记录用户选择的目标版本，无法猜测，必须保留。
                return False
            target_version = int(target_match.group(1))
            try:
                if self._lease_provider_attempt_count(lease) != 0:
                    return False
            except Exception:  # noqa: BLE001 — attempt 账不可读即保留
                return False
            session = self._open_session(lease.workspace_id)
            if session is None:
                return False
            try:
                row = session.get(ChapterORM, lease.chapter_id)
                versions = list(
                    session.scalars(
                        select(ChapterVersionORM)
                        .where(ChapterVersionORM.chapter_id == lease.chapter_id)
                        .order_by(ChapterVersionORM.version_id)
                    )
                )
                versions_by_id = {version.version_id: version for version in versions}
                if (
                    row is None
                    or row.workspace_id != lease.workspace_id
                    or row.status
                    in (
                        ChapterState.APPROVED.value,
                        ChapterState.PUBLISHED.value,
                    )
                    or row.ready_for_next_chapter_at is not None
                    or row.current_version != lease.source_version
                    or not versions
                    or max(versions_by_id) != lease.source_version
                    or target_version not in versions_by_id
                ):
                    return False
                _require_memory_enrichment_checkpoint_safe(
                    row=row,
                    chapter_id=lease.chapter_id,
                    evidence=[
                        "operation=switch-version-startup-recovery",
                        f"target_version={target_version}",
                    ],
                    block_running=True,
                )
            except Exception:  # noqa: BLE001 — 章节/富记忆现场未验明即保留
                return False
            finally:
                session.close()

            chapter_dir = paths.chapter_dir(lease.chapter_id)
            try:
                canonical_versions = collect_canonical_version_files(chapter_dir)
                disk_versions = {
                    version_id: path.read_bytes()
                    for version_id, path in canonical_versions.items()
                }
            except (ChapterVersioningError, OSError):
                return False
            if set(disk_versions) != set(versions_by_id):
                return False
            for version_id, version in versions_by_id.items():
                if disk_versions[version_id] != version.content.encode("utf-8"):
                    return False

            final_path = paths.chapter_version_file(lease.chapter_id, "final.md")
            if final_path.exists():
                try:
                    final_before = final_path.read_bytes()
                except OSError:
                    return False
                if final_before not in disk_versions.values():
                    return False
            if not apply:
                return True
            try:
                ChapterVersioning(paths).mark_final(
                    lease.chapter_id,
                    target_version,
                )
                if final_path.read_bytes() != disk_versions[target_version]:
                    return False
                self._operation_lease_store.mark_phase(
                    lease,
                    phase="operation_complete",
                )
            except Exception:  # noqa: BLE001 — 本地切换未闭合即保留
                return False
            return True

        if lease.operation_kind != "update-content-manual":
            return False
        if lease.phase not in {
            "reserved_before_provider",
            "source_mutation_committed",
        }:
            return False
        try:
            if self._lease_provider_attempt_count(lease) != 0:
                return False
        except Exception:  # noqa: BLE001 — attempt 账不可读即保留
            return False
        target_version = lease.source_version + 1
        if (
            ChapterVersioning(paths).next_version_id(lease.chapter_id) - 1
            != target_version
        ):
            return False
        target_path = paths.chapter_version_file(
            lease.chapter_id,
            f"v{target_version}.md",
        )
        try:
            content = target_path.read_text(encoding="utf-8")
        except OSError:
            return False
        session = self._open_session(lease.workspace_id)
        if session is None:
            return False
        try:
            row = session.get(ChapterORM, lease.chapter_id)
            db_latest = int(
                session.scalar(
                    select(func.max(ChapterVersionORM.version_id)).where(
                        ChapterVersionORM.chapter_id == lease.chapter_id
                    )
                )
                or 0
            )
            if (
                row is None
                or row.workspace_id != lease.workspace_id
                or row.status != ChapterState.HUMAN_INTERVENTION.value
                or db_latest not in {lease.source_version, target_version}
            ):
                return False
        finally:
            session.close()
        if not apply:
            return True
        version = ChapterVersion(
            version_id=target_version,
            content=content,
            word_count=len(content),
            created_at=lease.updated_at,
            creation_type="manual_edit",
            model_used="manual",
            review_score=None,
            notes="",
            style_fingerprint=compute_style_fingerprint(content),
        )
        try:
            persist_chapter_version_safely(
                lambda: self._session_factory(lease.workspace_id),  # type: ignore[misc]
                chapter_id=lease.chapter_id,
                version=version,
            )
            ChapterVersioning(paths).mark_final(
                lease.chapter_id,
                target_version,
            )
            self._operation_lease_store.mark_phase(
                lease,
                phase="operation_complete",
            )
        except Exception:  # noqa: BLE001 — 任一重建不一致即保留
            return False
        return True

    def _reconcile_approved_finalization_lease(
        self,
        lease: ChapterOperationLease,
        *,
        paths: WorkspacePaths,
        apply: bool = True,
    ) -> bool:
        """补完 ``APPROVED`` 已落库但 ``mark_final`` 未完成的崩溃窗口。

        Orchestrator 会先持久化完整审稿、再通过状态回调落
        ``chapters.status=approved``，最后才把最新 ``vN.md`` 标成
        ``final.md``。进程若恰在最后一步前退出，不能只释放租约：普通
        approved checkpoint 恢复会把缺失/旧 ``final.md`` 视为损坏，永远
        到不了可复用审稿的写作恢复路径。

        本恢复只接受本租约后的完整 PASS 审稿、approved 水位、未落 ready、
        无未知 Provider attempt、DB/盘版本全集逐字一致，以及缺失或仍指向
        某个已知旧版本的 final。任一条件不满足都保留租约和现场。
        """

        if lease.phase != "provider_capable_pipeline" or lease.operation_kind not in {
            "write",
            "rewrite",
            "re-review",
        }:
            return False
        if not self._provider_stage_lease_has_recoverable_version(
            lease,
            paths=paths,
            allow_approved_final_gap=True,
        ):
            return False

        try:
            checkpoint = load_rewrite_checkpoint_context(
                (
                    (lambda: self._session_factory(lease.workspace_id))
                    if self._session_factory is not None
                    else None
                ),
                paths=paths,
                chapter_id=lease.chapter_id,
                version_id=(
                    ChapterVersioning(paths).next_version_id(lease.chapter_id) - 1
                ),
                allow_stale_chapter_projection=True,
            )
        except Exception:  # noqa: BLE001 — 任一审稿/版本异常都保留现场
            return False
        if (
            checkpoint.review is None
            or checkpoint.review_result is None
            or checkpoint.review.verdict != "pass"
            or not checkpoint.review.hard_constraints_passed
        ):
            return False

        session = self._open_session(lease.workspace_id)
        if session is None:
            return False
        try:
            row = session.get(ChapterORM, lease.chapter_id)
            versions = list(
                session.scalars(
                    select(ChapterVersionORM)
                    .where(ChapterVersionORM.chapter_id == lease.chapter_id)
                    .order_by(ChapterVersionORM.version_id)
                )
            )
            versions_by_id = {version.version_id: version for version in versions}
            latest_version_id = max(versions_by_id) if versions_by_id else 0
            if (
                row is None
                or row.workspace_id != lease.workspace_id
                or row.status != ChapterState.APPROVED.value
                or row.approved_at is None
                or self._datetime_before(row.approved_at, lease.created_at)
                or row.current_version != latest_version_id
                or row.ready_for_next_chapter_at is not None
                or row.memory_enrichment_status != "pending"
                or row.memory_enrichment_details is not None
                or row.force_pass_at is not None
                or checkpoint.version_id != latest_version_id
            ):
                return False
            known_version_bytes = {
                version.content.encode("utf-8") for version in versions_by_id.values()
            }
            latest_bytes = versions_by_id[latest_version_id].content.encode("utf-8")
        finally:
            session.close()

        final_path = paths.chapter_version_file(lease.chapter_id, "final.md")
        if final_path.exists():
            try:
                final_before = final_path.read_bytes()
            except OSError:
                return False
            if final_before not in known_version_bytes:
                return False
        if not apply:
            return True
        try:
            ChapterVersioning(paths).mark_final(
                lease.chapter_id,
                latest_version_id,
            )
            if final_path.read_bytes() != latest_bytes:
                return False
        except Exception:  # noqa: BLE001 — final 未精确闭合就保留租约
            return False
        _log.info(
            "chapter_approved_finalization_recovered_on_startup",
            workspace_id=lease.workspace_id,
            chapter_id=lease.chapter_id,
            operation=lease.operation_kind,
            task_id=lease.task_id,
            final_version_id=latest_version_id,
            provider_replay=False,
        )
        return True

    def _operation_complete_lease_is_safe(
        self,
        lease: ChapterOperationLease,
        *,
        apply: bool = True,
    ) -> bool:
        """确认内层完成结果已被外层 UI/run checkpoint 接住。

        UI task 没有 ``run_id``，内层 operation_complete 已足够。Pipeline
        若崩在章节结果返回与 run disposition 之间，approved+ready 可由 Resume
        回填；human_intervention 则在启动期保守归一为 paused，避免 Resume
        把已经物化的人工作业当普通未完成章再次送 Provider。
        """

        if lease.run_id is None:
            return True
        session = self._open_session(lease.workspace_id)
        if session is None:
            return False
        try:
            run = session.get(PipelineRunORM, lease.run_id)
            row = session.get(ChapterORM, lease.chapter_id)
            if (
                run is None
                or run.workspace_id != lease.workspace_id
                or row is None
                or row.workspace_id != lease.workspace_id
            ):
                return False
            if lease.operation_kind in {
                "rewind",
                "switch-version",
                "update-content-manual",
            }:
                # 这些同步人工操作不会把章节结果交给 Pipeline 的
                # completed/failure disposition；operation_complete 本身就是
                # 它们的外层 durable terminal。
                return True
            if (
                lease.chapter_id in run.completed_chapter_ids
                or lease.chapter_id in run.skipped_chapter_ids
            ):
                return True
            if row.status in (
                ChapterState.APPROVED.value,
                ChapterState.PUBLISHED.value,
            ):
                if row.ready_for_next_chapter_at is None:
                    return False
                try:
                    self.require_ready_for_next_chapter(
                        lease.workspace_id,
                        lease.chapter_id,
                    )
                except Exception:  # noqa: BLE001 — 未验明即保留
                    return False
                return True
            if row.status != ChapterState.HUMAN_INTERVENTION.value:
                return False
            if run.current_chapter != lease.chapter_id:
                return False
            if run.state == "interrupted":
                if not apply:
                    return True
                run.state = "paused"
                run.pause_reason = "chapter_escalated"
                run.error_details = {
                    "reason": "chapter_escalated",
                    "chapter_id": lease.chapter_id,
                    "recovered_from": ("operation_complete_without_run_disposition"),
                }
                run.updated_at = datetime.now(timezone.utc)
                session.commit()
                return True
            return run.state in ("paused", "error", "stopped")
        except Exception:
            session.rollback()
            return False
        finally:
            session.close()

    def _lease_provider_attempt_count(
        self,
        lease: ChapterOperationLease,
        *,
        agent_name: str | None = None,
        statuses: tuple[str, ...] | None = None,
    ) -> int:
        """统计租约建立后同章匹配条件的 Provider attempt。

        不按 task_id 过滤：跨线程上下文丢绑或错绑本身就是必须保留的账本异常，
        不能因此把实际已出网现场误报为零调用。
        """

        session = self._open_session(lease.workspace_id)
        if session is None:
            raise RewriteCheckpointRecoveryError("无法读取操作租约 attempt 账本")
        try:
            stmt = (
                select(func.count())
                .select_from(AttemptLedgerORM)
                .where(
                    AttemptLedgerORM.workspace_id == lease.workspace_id,
                    AttemptLedgerORM.chapter_id == lease.chapter_id,
                    AttemptLedgerORM.attempt_kind == "provider",
                    AttemptLedgerORM.created_at >= lease.created_at,
                )
            )
            if agent_name is not None:
                stmt = stmt.where(AttemptLedgerORM.agent_name == agent_name)
            if statuses is not None:
                stmt = stmt.where(AttemptLedgerORM.status.in_(statuses))
            return int(session.scalar(stmt) or 0)
        finally:
            session.close()

    def _relation_recovery_lease_is_safe(
        self, lease: ChapterOperationLease, *, paths: WorkspacePaths,
    ) -> bool:
        """旧关系复核漏推进 phase 的窄恢复；启动只释放锁，不调用模型。"""
        if self._session_factory is None:
            return False
        session = self._open_session(lease.workspace_id)
        if session is None:
            return False
        try:
            row = session.get(ChapterORM, lease.chapter_id)
            if row is None or row.current_version != lease.source_version:
                return False
            attempts = list(session.scalars(select(AttemptLedgerORM).where(
                AttemptLedgerORM.workspace_id == lease.workspace_id,
                AttemptLedgerORM.chapter_id == lease.chapter_id,
                AttemptLedgerORM.attempt_kind == "provider",
                AttemptLedgerORM.created_at >= lease.created_at,
            )))
            if any(attempt.status != "succeeded" or attempt.completed_at is None
                   or attempt.agent_name != "chapter_card_generator"
                   or attempt.task_id != lease.task_id or attempt.run_id != lease.run_id
                   for attempt in attempts):
                return False
            if session.scalar(select(func.count()).select_from(AttemptLedgerORM).where(
                AttemptLedgerORM.workspace_id == lease.workspace_id,
                AttemptLedgerORM.chapter_id == lease.chapter_id,
                AttemptLedgerORM.attempt_kind == "provider",
                AttemptLedgerORM.status == "started",
            )):
                return False
            card = ChapterCard.model_validate_json(
                (paths.cards_dir / f"{lease.chapter_id}.json").read_text())
            service = CoreStateService(session_factory=self._session_factory)
            if service.require_pending_transition_candidate_recovery_safe(
                session=session, workspace_id=lease.workspace_id, card=card, paths=paths,
            ):
                return False  # 摘要重提取不属于此关系响应恢复。
            from src.services.relation_registration_recovery import recovery_payload
            from src.core.db.models import ChapterSummaryORM
            summary = session.get(ChapterSummaryORM, lease.chapter_id)
            if summary is None:
                return False
            payload = recovery_payload(card=card, candidates=service._parse_candidates(summary),
                projection=service._projection_from_session(session,
                    workspace_id=lease.workspace_id, before_chapter=card.chapter_number),
                paths=paths, text=paths.chapter_version_file(lease.chapter_id, "final.md").read_text(),
                ledger_hash=summary.ledger_hash, source_version_id=lease.source_version)
            if payload is None:
                return False
            if not attempts:
                return True
            # 成功账本也不能代替响应文件：只释放可确认已经落盘的那次条件复核。
            if len(attempts) != 1:
                return False
            from src.services.relation_registration_recovery import decision_path
            sources = [payload]
            if payload.get("schema_version") == 2:
                legacy = {key: value for key, value in payload.items() if key != "transient_eligible_keys"}
                legacy["schema_version"] = 1
                sources.append(legacy)
            for source in sources:
                path = decision_path(paths=paths, payload=source)
                if not path.exists():
                    continue
                record = json.loads(path.read_text())
                if record.get("source") != source or record.get("status") != "responded":
                    continue
                prepared_at = datetime.fromisoformat(record["prepared_at"])
                if (not self._datetime_before(prepared_at, lease.created_at)
                    and not self._datetime_before(attempts[0].created_at, prepared_at)):
                    return True
            return False
        except Exception:  # noqa: BLE001 — 来源、响应或账本无法验明时保留锁
            return False
        finally:
            session.close()

    def _operation_lease_has_recoverable_checkpoint(
        self,
        lease: ChapterOperationLease,
        *,
        paths: WorkspacePaths,
    ) -> bool:
        """判定已物化 phase 是否可由原产品入口无盲重地接管。

        本方法只读 DB/文件，不修补任何产物。返回 ``True`` 后 startup 仅删
        租约；真正的 ledger/ready/富记忆恢复仍由用户 Resume 后的正常
        ChapterService/Pipeline 路径执行。
        """

        if lease.phase in {
            "provider_capable_pipeline",
            "reviser_may_call_provider",
        }:
            return self._provider_stage_lease_has_recoverable_version(
                lease,
                paths=paths,
            )

        if lease.phase == "ready_persisted":
            try:
                self.require_ready_for_next_chapter(
                    lease.workspace_id,
                    lease.chapter_id,
                )
            except Exception:  # noqa: BLE001 — 未验明即保留
                return False
            return True

        if lease.operation_kind == "checkpoint-resume" and lease.phase in {
            "reserved_before_provider", "continuity_ledger_may_call_provider",
        }:
            session = self._open_session(lease.workspace_id)
            if session is None:
                return False
            try:
                row = session.get(ChapterORM, lease.chapter_id)
                relation_checkpoint = row is not None and bool(
                    (row.core_state_commit_details or {}).get("conflicts"))
            finally:
                session.close()
            if relation_checkpoint:
                return self._relation_recovery_lease_is_safe(lease, paths=paths)

        checkpoint_phases = {
            "orchestrator_result_materialized",
            "continuity_ledger_may_call_provider",
            "continuity_ledger_persisted",
        }
        if lease.phase not in checkpoint_phases:
            return False

        session = self._open_session(lease.workspace_id)
        if session is None:
            return False
        try:
            row = session.get(ChapterORM, lease.chapter_id)
            authoritative_version = session.scalar(
                select(ChapterVersionORM)
                .where(ChapterVersionORM.chapter_id == lease.chapter_id)
                .order_by(ChapterVersionORM.version_id.desc())
                .limit(1)
            )
            if row is None or row.workspace_id != lease.workspace_id:
                return False
            # Orchestrator 已明确产出人工介入终态时没有后续 ledger/ready
            # Provider 阶段；释放后由 UI 的正常人工流程接管。
            if (
                lease.phase == "orchestrator_result_materialized"
                and row.status == ChapterState.HUMAN_INTERVENTION.value
            ):
                return True
            if authoritative_version is None:
                return False
            version_id = authoritative_version.version_id
            content = authoritative_version.content
            if row.current_version != version_id:
                return False
            if row.status not in (
                ChapterState.APPROVED.value,
                ChapterState.PUBLISHED.value,
                ChapterState.HUMAN_INTERVENTION.value,
            ):
                return False
        finally:
            session.close()

        # force-pass 在最终 approved 事务前仍保持 human_intervention；其
        # final/ledger 同样必须和当前不可变版本逐字一致。
        try:
            self._validated_checkpoint_body_snapshot(
                workspace_id=lease.workspace_id,
                paths=paths,
                chapter_id=lease.chapter_id,
                source_version_id=version_id,
                authoritative_content=content,
            )
        except Exception:  # noqa: BLE001 — 文件未验明即保留
            return False

        ledger_valid = False
        try:
            load_validated_continuity_ledger(
                workspace_id=lease.workspace_id,
                chapter_id=lease.chapter_id,
                paths=paths,
                session_factory=self._session_factory,
                expected_version_id=version_id,
            )
        except ContinuityLedgerValidationError:
            ledger_valid = False
        else:
            ledger_valid = True

        if lease.phase == "continuity_ledger_persisted":
            return ledger_valid
        if ledger_valid:
            return True
        # 已知 failed 响应没有可消费结果，可释放租约后由原产品 checkpoint
        # 再试；started 结果未知、succeeded 响应尚未物化 ledger 时必须保留。
        try:
            return (
                self._lease_provider_attempt_count(
                    lease,
                    agent_name="chapter_summary_extractor",
                    statuses=("started", "succeeded"),
                )
                == 0
            )
        except Exception:  # noqa: BLE001 — attempt 账不可读即保留
            return False

    @staticmethod
    def _as_utc(value: datetime) -> datetime:
        """SQLite 可能丢 timezone；恢复比较统一按 UTC，避免 naive/aware 异常。"""

        if value.tzinfo is None:
            return value.replace(tzinfo=timezone.utc)
        return value.astimezone(timezone.utc)

    @classmethod
    def _datetime_after(cls, value: datetime, baseline: datetime) -> bool:
        return cls._as_utc(value) > cls._as_utc(baseline)

    @classmethod
    def _datetime_before(cls, value: datetime, baseline: datetime) -> bool:
        return cls._as_utc(value) < cls._as_utc(baseline)

    def _known_empty_must_recovery(
        self, workspace_id: str, chapter_id: str, *, current_version: int,
    ) -> KnownReviewResponseRecovery | None:
        session = self._open_session(workspace_id)
        if session is None:
            return None
        try:
            return load_known_empty_must_recovery(
                session, paths=self._validate_workspace(workspace_id),
                workspace_id=workspace_id, chapter_id=chapter_id,
                current_version=current_version,
            )
        finally:
            session.close()

    def _known_invalid_local_review_retry(
        self, workspace_id: str, chapter_id: str, *, current_version: int,
    ) -> tuple[str, str] | None:
        """只读识别旧 Router 截断解析流程的格式失败，延续原有剩余格式尝试。

        必须有同 run/task 的 failed pipeline 记录及唯一完整 Provider 原响应。
        有效 JSON、未知 usage、截断、额外写后响应、已有 review 或正文漂移均不开放。
        此方法不更改账本，不把旧响应当审核通过，不发 Provider。
        """
        if current_version < 1:
            return None
        paths = self._validate_workspace(workspace_id)
        session = self._open_session(workspace_id)
        if session is None:
            return None
        try:
            chapter = session.get(ChapterORM, chapter_id)
            latest = session.scalar(select(ChapterVersionORM).where(
                ChapterVersionORM.chapter_id == chapter_id).order_by(
                ChapterVersionORM.version_id.desc()).limit(1))
            if (chapter is None or latest is None or chapter.workspace_id != workspace_id
                or chapter.status not in {"reviewing", "drafting"} or chapter.current_version != current_version
                or latest.version_id != current_version or chapter.approved_at is not None
                or chapter.ready_for_next_chapter_at is not None
                or chapter.memory_enrichment_status != "pending"
                or session.scalar(select(ReviewResultORM.review_id).where(
                    ReviewResultORM.chapter_id == chapter_id,
                    ReviewResultORM.version_reviewed == current_version).limit(1)) is not None):
                return None
            # 全部版本都由原 loader 另行验证；这里先防当前正文与 final 漂移。
            if (paths.chapter_version_file(chapter_id, f"v{current_version}.md").read_text()
                != latest.content or paths.chapter_version_file(chapter_id, "final.md").exists()):
                return None
            attempts = list(session.scalars(select(AttemptLedgerORM).where(
                AttemptLedgerORM.workspace_id == workspace_id,
                AttemptLedgerORM.chapter_id == chapter_id,
                AttemptLedgerORM.attempt_kind == "provider",
                AttemptLedgerORM.created_at >= latest.created_at)))
            if len(attempts) != 1:
                return None
            attempt = attempts[0]
            if (attempt.agent_name != "reviewer" or attempt.status != "succeeded"
                or attempt.usage_status != "known" or attempt.response_status != "completed"
                or attempt.completed_at is None or not attempt.provider_request_id
                or attempt.run_id is None):
                return None
            failed = list(session.scalars(select(AttemptLedgerORM).where(
                AttemptLedgerORM.workspace_id == workspace_id,
                AttemptLedgerORM.chapter_id == chapter_id,
                AttemptLedgerORM.task_id == attempt.task_id,
                AttemptLedgerORM.run_id == attempt.run_id,
                AttemptLedgerORM.attempt_kind == "pipeline",
                AttemptLedgerORM.status == "failed",
                AttemptLedgerORM.error_type == "LocalJsonResponseViolation")))
            run = session.get(PipelineRunORM, attempt.run_id)
            if (len(failed) != 1 or failed[0].completed_at is None
                or self._datetime_before(failed[0].completed_at, attempt.completed_at)
                or run is None or run.workspace_id != workspace_id
                or run.current_chapter != chapter_id or run.state not in {"paused", "running", "interrupted"}
                or chapter_id not in run.chapter_ids or chapter_id in run.completed_chapter_ids
                or chapter_id in run.skipped_chapter_ids):
                return None
            matches = []
            for path in (paths.archives_dir / "responses").glob("*/*.json"):
                value = json.loads(path.read_text())
                if not isinstance(value, dict):
                    return None
                if value.get("provider_request_id") == attempt.provider_request_id:
                    matches.append((path, value))
            if len(matches) != 1:
                return None
            archive_path, raw = matches[0]
            usage = raw.get("usage")
            captured = datetime.fromisoformat(raw["captured_at"])
            if (raw.get("model_used") != attempt.model or raw.get("finish_reason") != "stop"
                or not isinstance(usage, dict)
                or usage.get("input_tokens") != attempt.input_tokens
                or usage.get("output_tokens") != attempt.output_tokens
                or raw.get("prompt_hash") != archive_path.parent.name
                or not re.fullmatch(r"[a-f0-9]{12}", archive_path.parent.name)
                or self._datetime_before(captured, attempt.completed_at)
                or self._datetime_after(captured, failed[0].completed_at)
                or not isinstance(raw.get("content"), str) or not raw["content"].lstrip().startswith("{")):
                return None
            try:
                json.loads(raw["content"])
            except json.JSONDecodeError as exc:
                feedback = (f"第 1 次尝试失败:本地 Reviewer JSON 格式无效:{exc}"
                            f"(原始输出前 300 字预览:{raw['content'][:300]})")
                return attempt.task_id, feedback
            return None
        except (OSError, ValueError, KeyError, TypeError):
            return None
        finally:
            session.close()

    def _provider_stage_lease_has_recoverable_version(
        self,
        lease: ChapterOperationLease,
        *,
        paths: WorkspacePaths,
        allow_approved_final_gap: bool = False,
    ) -> bool:
        """Writer/Reviser 正文已落盘时，区分可续审与未知响应。

        已知 failed Provider attempt 可从持久版本重试后续阶段；
        ``started``、任一写后成功响应未落完整 review row，或成功
        Writer/Reviser 响应晚于最新版本，都代表仍有未消费结果，必须保留。
        ``latest == source`` 只允许恢复本租约期间新落下的完整审稿 checkpoint。
        """

        session = self._open_session(lease.workspace_id)
        if session is None:
            return False
        try:
            chapter = session.get(ChapterORM, lease.chapter_id)
            latest = session.scalar(
                select(ChapterVersionORM)
                .where(ChapterVersionORM.chapter_id == lease.chapter_id)
                .order_by(ChapterVersionORM.version_id.desc())
                .limit(1)
            )
            attempts = list(
                session.scalars(
                    select(AttemptLedgerORM)
                    .where(
                        AttemptLedgerORM.workspace_id == lease.workspace_id,
                        AttemptLedgerORM.chapter_id == lease.chapter_id,
                        AttemptLedgerORM.attempt_kind == "provider",
                        AttemptLedgerORM.created_at >= lease.created_at,
                    )
                    .order_by(AttemptLedgerORM.created_at)
                )
            )
            if (
                chapter is None
                or chapter.workspace_id != lease.workspace_id
                or latest is None
                or latest.version_id < lease.source_version
                or chapter.current_version != latest.version_id
                or any(attempt.status == "started" for attempt in attempts)
            ):
                return False
            latest_version_id = latest.version_id
            chapter_status = chapter.status
            latest_created_at = latest.created_at
            latest_creation_type = latest.creation_type
            latest_content = latest.content
            review_row = (
                session.execute(
                    select(ReviewResultORM)
                    .where(ReviewResultORM.chapter_id == lease.chapter_id)
                    .where(ReviewResultORM.version_reviewed == latest_version_id)
                    .order_by(
                        ReviewResultORM.reviewed_at.desc(),
                        ReviewResultORM.review_id.desc(),
                    )
                )
                .scalars()
                .first()
            )
            review_created_at = (
                review_row.reviewed_at if review_row is not None else None
            )
            if any(
                attempt.status == "succeeded"
                and attempt.agent_name in {"writer", "reviser"}
                and self._datetime_after(attempt.created_at, latest_created_at)
                for attempt in attempts
            ):
                return False
        finally:
            session.close()

        format_retry = self._known_invalid_local_review_retry(
            lease.workspace_id, lease.chapter_id, current_version=latest_version_id,
        ) if lease.phase == "provider_capable_pipeline" else None
        if format_retry is not None and format_retry[0] != lease.task_id:
            return False
        response_recovery = self._known_empty_must_recovery(
            lease.workspace_id, lease.chapter_id, current_version=latest_version_id,
        ) if lease.phase == "provider_capable_pipeline" else None
        if response_recovery is not None and response_recovery.source_task_id != lease.task_id:
            return False
        try:
            checkpoint = load_rewrite_checkpoint_context(
                (
                    (lambda: self._session_factory(lease.workspace_id))
                    if self._session_factory is not None
                    else None
                ),
                paths=paths,
                chapter_id=lease.chapter_id,
                version_id=latest_version_id,
                allow_stale_chapter_projection=True,
                allow_unpersisted_reviewer_task_id=(
                    response_recovery.source_task_id if response_recovery is not None
                    else (format_retry[0] if format_retry else None)
                ),
            )
        except Exception:  # noqa: BLE001 — 任一 DB/盘/审稿不一致都保留
            return False

        if chapter_status in (
            ChapterState.APPROVED.value,
            ChapterState.PUBLISHED.value,
        ):
            if (
                checkpoint.review is None
                or checkpoint.review_result is None
                or checkpoint.review.verdict != "pass"
                or not checkpoint.review.hard_constraints_passed
            ):
                return False
            final_path = paths.chapter_version_file(lease.chapter_id, "final.md")
            try:
                final_matches_latest = final_path.read_bytes() == latest_content.encode(
                    "utf-8"
                )
            except OSError:
                final_matches_latest = False
            if not final_matches_latest and not allow_approved_final_gap:
                return False

        if lease.phase == "reviser_may_call_provider":
            # UI revise 一次只允许新增一个、且 DB/磁盘逐字一致的 revise 版本。
            # loader 已验证完整版本集合；任何 Reviewer attempt 都不属于这条
            # 单步入口，出现即保留现场。
            return (
                latest_version_id == lease.source_version + 1
                and latest_creation_type == "revise"
                and not any(
                    attempt.agent_name in {"reviewer", "reviewer_escalation"}
                    for attempt in attempts
                )
            )

        if (format_retry is not None or response_recovery is not None) and checkpoint.review is None:
            return True
        if checkpoint.review is not None:
            # latest==source 时可能读到本操作之前的旧 review；只有本租约
            # 开始后新落下的 review 才能证明本次 Reviewer/写后 gate 已消费。
            return review_created_at is not None and not self._datetime_before(
                review_created_at,
                lease.created_at,
            )
        # 只要本租约有 Reviewer 成功而 review row 尚未落库，就仍有未消费
        # 结果，不能按时间窗口误放并重复审稿。此保守判断也覆盖旧 workspace
        # 可能遗留的历史多稿 attempt，但不会重启任何多稿 Provider 调用。
        if any(
            attempt.status == "succeeded"
            and attempt.agent_name in {"reviewer", "reviewer_escalation"}
            for attempt in attempts
        ):
            return False
        if latest_version_id == lease.source_version:
            return False
        # 新正文已逐字落 DB/盘但尚无 review：只有最新版本落盘后没有任何
        # succeeded Provider 响应时，才可从该正文继续审。Reviewer、写后 LLM
        # gate 或其他成功响应一旦未进入 review row，都必须保留租约。
        return not any(
            attempt.status == "succeeded"
            and self._datetime_after(attempt.created_at, latest_created_at)
            for attempt in attempts
        )

    @staticmethod
    def _provider_attempt_fingerprint(attempts: list[AttemptLedgerORM]) -> str:
        """冻结恢复现场的 Provider attempt 身份与终态，不含 prompt/正文。"""

        payload = [
            {
                "attempt_id": item.attempt_id,
                "task_id": item.task_id,
                "run_id": item.run_id,
                "agent_name": item.agent_name,
                "status": item.status,
                "error_type": item.error_type,
                "created_at": item.created_at.isoformat(),
                "completed_at": (
                    item.completed_at.isoformat()
                    if item.completed_at is not None
                    else None
                ),
            }
            for item in sorted(attempts, key=lambda value: value.attempt_id)
        ]
        encoded = json.dumps(
            payload,
            ensure_ascii=False,
            sort_keys=True,
            separators=(",", ":"),
        ).encode("utf-8")
        return hashlib.sha256(encoded).hexdigest()

    def _cancelled_post_review_replay_marker(
        self,
        lease: ChapterOperationLease,
        *,
        paths: WorkspacePaths,
        cancelled_task_check: CancelledTaskCheckpointCheck | None,
        session: Session | None = None,
    ) -> dict[str, Any] | None:
        """识别“Reviewer 已返回、最终 ReviewResult 未提交、用户取消”的精确现场。

        这里只覆盖 R9 rewind 后新 rewrite 正文的单稿 ``write`` 路径。普通崩溃、
        interrupted/failed task、多个 Reviewer、started/unknown attempt、
        跨 task/run、版本或磁盘漂移一律不分类，继续保留安全锁。
        """

        if (
            cancelled_task_check is None
            or lease.operation_kind != "write"
            or lease.phase != "provider_capable_pipeline"
            or lease.run_id is None
            or not cancelled_task_check(
                lease.task_id,
                lease.workspace_id,
                "chapter_write",
            )
        ):
            return None

        owned_session = session is None
        active_session = session or self._open_session(lease.workspace_id)
        if active_session is None:
            return None
        try:
            chapter = active_session.get(ChapterORM, lease.chapter_id)
            versions = list(
                active_session.scalars(
                    select(ChapterVersionORM)
                    .where(ChapterVersionORM.chapter_id == lease.chapter_id)
                    .order_by(ChapterVersionORM.version_id)
                )
            )
            latest = versions[-1] if versions else None
            source = next(
                (
                    item
                    for item in versions
                    if item.version_id == lease.source_version
                ),
                None,
            )
            review_row = active_session.scalar(
                select(ReviewResultORM)
                .where(ReviewResultORM.chapter_id == lease.chapter_id)
                .where(
                    ReviewResultORM.version_reviewed
                    == (latest.version_id if latest is not None else -1)
                )
                .limit(1)
            )
            attempts = list(
                active_session.scalars(
                    select(AttemptLedgerORM)
                    .where(
                        AttemptLedgerORM.workspace_id == lease.workspace_id,
                        AttemptLedgerORM.chapter_id == lease.chapter_id,
                        AttemptLedgerORM.attempt_kind == "provider",
                        AttemptLedgerORM.created_at >= lease.created_at,
                    )
                    .order_by(AttemptLedgerORM.created_at)
                )
            )
            details = chapter.escalation_details if chapter is not None else None
            if (
                chapter is None
                or chapter.workspace_id != lease.workspace_id
                or chapter.status != ChapterState.REVIEWING.value
                or chapter.current_version != lease.source_version + 1
                or chapter.ready_for_next_chapter_at is not None
                or chapter.approved_at is not None
                or chapter.memory_enrichment_status != "pending"
                or chapter.memory_enriched_at is not None
                or not isinstance(details, dict)
                or details.get("reason") != REWIND_REWRITE_REQUIRED_REASON
                or details.get("source_version_id") != lease.source_version
                or source is None
                or latest is None
                or latest.version_id != lease.source_version + 1
                or latest.creation_type != "rewrite"
                or self._datetime_before(latest.created_at, lease.created_at)
                or review_row is not None
                or not attempts
            ):
                return None

            if any(
                item.task_id != lease.task_id
                or item.run_id != lease.run_id
                or item.operation_kind != "agent_call"
                or item.status not in {"succeeded", "failed"}
                or item.completed_at is None
                for item in attempts
            ):
                return None
            writer_attempts = [
                item
                for item in attempts
                if item.agent_name == "writer" and item.status == "succeeded"
            ]
            reviewer_attempts = [
                item
                for item in attempts
                if item.agent_name == "reviewer" and item.status == "succeeded"
            ]
            cancelled_gate_attempts = [
                item
                for item in attempts
                if item.agent_name in _POST_REVIEW_GATE_AGENTS
                and item.status == "failed"
                and item.error_type == "LLMCancelledError"
            ]
            if (
                len(writer_attempts) != 1
                or len(reviewer_attempts) != 1
                or len(cancelled_gate_attempts) != 1
                or any(
                    item.status == "failed" and item not in cancelled_gate_attempts
                    for item in attempts
                )
                or any(item.agent_name == "reviewer_escalation" for item in attempts)
                or writer_attempts[0].completed_at is None
                or self._datetime_after(
                    writer_attempts[0].completed_at,
                    latest.created_at,
                )
                or self._datetime_before(
                    reviewer_attempts[0].created_at,
                    latest.created_at,
                )
            ):
                return None

            db_version_ids = {item.version_id for item in versions}
            try:
                disk_versions = collect_canonical_version_files(
                    paths.chapter_dir(lease.chapter_id)
                )
                if set(disk_versions) != db_version_ids:
                    return None
                for version in versions:
                    if (
                        disk_versions[version.version_id].read_text(encoding="utf-8")
                        != version.content
                    ):
                        return None
                final_path = paths.chapter_version_file(lease.chapter_id, "final.md")
                if final_path.is_file() and final_path.read_text(
                    encoding="utf-8"
                ) != source.content:
                    return None
            except (OSError, UnicodeDecodeError, ChapterVersioningError):
                return None

            return {
                "reason": CANCELLED_REVIEW_REPLAY_REASON,
                "source_task_id": lease.task_id,
                "source_lease_id": lease.lease_id,
                "source_operation_kind": lease.operation_kind,
                "source_version_id": lease.source_version,
                "version_reviewed": latest.version_id,
                "run_id": lease.run_id,
                "content_sha256": hashlib.sha256(
                    latest.content.encode("utf-8")
                ).hexdigest(),
                "reviewer_attempt_id": reviewer_attempts[0].attempt_id,
                "cancelled_gate_attempt_id": cancelled_gate_attempts[0].attempt_id,
                "attempt_fingerprint": self._provider_attempt_fingerprint(attempts),
                "classified_at": datetime.now(timezone.utc).isoformat(),
            }
        finally:
            if owned_session:
                active_session.close()

    def _mark_cancelled_post_review_replay_and_release(
        self,
        lease: ChapterOperationLease,
        *,
        paths: WorkspacePaths,
        cancelled_task_check: CancelledTaskCheckpointCheck,
    ) -> bool:
        """同一 SQLite 事务写受控重审标记并释放旧租约。"""

        if self._operation_lease_store is None:
            return False
        session = self._open_session(lease.workspace_id)
        if session is None:
            return False
        try:
            self._operation_lease_store.begin_immediate(session)
            marker = self._cancelled_post_review_replay_marker(
                lease,
                paths=paths,
                cancelled_task_check=cancelled_task_check,
                session=session,
            )
            if marker is None:
                session.rollback()
                return False
            chapter = session.get(ChapterORM, lease.chapter_id)
            if chapter is None:
                session.rollback()
                return False
            chapter.escalation_details = marker
            self._operation_lease_store.release_in_session(session, lease)
            session.commit()
            _log.warning(
                "cancelled_post_review_checkpoint_replay_enabled",
                workspace_id=lease.workspace_id,
                chapter_id=lease.chapter_id,
                task_id=lease.task_id,
                run_id=lease.run_id,
                version_reviewed=marker["version_reviewed"],
                automatic_provider_replay=False,
                writer_replay_allowed=False,
            )
            return True
        except Exception:
            session.rollback()
            raise
        finally:
            session.close()

    def _acquire_chapter_operation_state(
        self,
        workspace_id: str,
        chapter_id: str,
        *,
        expected: _ChapterBudget,
        target_state: ChapterStateValue,
        operation: str,
    ) -> ChapterOperationLease | None:
        """在 Provider/文件写入前原子取消旧富记忆并占住章节改源权。

        Worker 的 ``pending -> running`` CAS 与这里竞争同一行：Worker 先赢，
        本入口因 ``running`` 硬停；本入口先赢，则 ready 被关闭、状态离开旧终态
        且 pending provenance 被清空，旧 Worker 的 CAS 必败。这样不存在“先读
        非 running，随后双方同时写”的 TOCTOU 窗口。
        """

        operation_lease = self._reserve_chapter_operation_lease(
            workspace_id=workspace_id,
            chapter_id=chapter_id,
            operation=operation,
            source_version=expected.current_version,
        )
        session = self._open_session(workspace_id)
        if session is None:
            return operation_lease
        try:
            if self._operation_lease_store is not None:
                self._operation_lease_store.begin_immediate(session)
                # API 预约只能被一个 Worker 消费一次。把
                # reserved -> claiming 与下方 Chapter CAS 放在同一
                # BEGIN IMMEDIATE 事务；同 task 的第二个 callback 即使
                # 同时读到旧 reserved 快照，也会在这里 CAS 失败，
                # 无法倒退 phase 或越过锁调 Provider。
                if operation_lease is not None:
                    operation_lease = self._operation_lease_store.mark_phase_in_session(
                        session,
                        operation_lease,
                        phase="chapter_state_claiming",
                    )
            row = session.get(ChapterORM, chapter_id)
            if row is None and expected.current_version == 0:
                session.commit()
                return operation_lease
            if row is None or row.workspace_id != workspace_id:
                raise RewriteCheckpointRecoveryError(
                    f"{operation} 的章节投影缺失或串位: {chapter_id}"
                )
            _require_memory_enrichment_checkpoint_safe(
                row=row,
                chapter_id=chapter_id,
                evidence=[f"operation={operation}"],
                block_running=True,
            )
            current = _derive_chapter_budget(session, chapter_id)
            if current != expected:
                raise RewriteCheckpointRecoveryError(
                    f"{operation} 的版本预算在验真后发生变化，已阻止请求: "
                    f"{chapter_id}/expected={expected}/actual={current}"
                )
            original_status = row.status
            original_ready_at = row.ready_for_next_chapter_at
            original_memory_status = row.memory_enrichment_status
            original_memory_details = row.memory_enrichment_details
            original_enriched_at = row.memory_enriched_at
            stmt = (
                update(ChapterORM)
                .where(ChapterORM.chapter_id == chapter_id)
                .where(ChapterORM.workspace_id == workspace_id)
                .where(ChapterORM.current_version == row.current_version)
                .where(ChapterORM.status == original_status)
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
                # SQLAlchemy ``JSON`` 默认把 Python ``None`` 编码成 JSON 文本
                # ``null``；少量历史/手工 fixture 则可能是真 SQL NULL。两种都
                # 表示同一个业务快照，CAS 必须同时兼容，不能用 ``IS NULL``
                # 把正常生产行误判成并发变化。
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
            result = session.execute(
                stmt.values(
                    current_version=current.current_version,
                    rewrite_count=current.rewrite_count,
                    revise_count=current.revise_count,
                    status=target_state,
                    ready_for_next_chapter_at=None,
                    memory_enrichment_status="pending",
                    memory_enriched_at=None,
                    memory_enrichment_details=None,
                )
            )
            if result.rowcount != 1:
                raise RewriteCheckpointRecoveryError(
                    f"{operation} 未取得章节原子改源权，可能已有富记忆 Worker "
                    f"或并发操作抢占: {chapter_id}"
                )
            session.commit()
            return operation_lease
        except Exception:
            session.rollback()
            if operation_lease is not None and self._operation_lease_store is not None:
                # rollback 后真正的 owner 若已把同一 lease 推进，
                # 这个过期 callback 只能看到 CAS=False，不得删锁。
                self._operation_lease_store.release_if_phase(
                    operation_lease,
                    expected_phase="reserved_before_provider",
                )
            raise
        finally:
            session.close()

    def _load_authoritative_budget(
        self,
        workspace_id: str,
        chapter_id: str,
    ) -> _ChapterBudget:
        """读取 immutable 预算并修正章节查询投影（非显式 Rewrite 路径）。"""
        budget = self._read_authoritative_budget(workspace_id, chapter_id)
        self._sync_authoritative_budget_projection(
            workspace_id,
            chapter_id,
            expected=budget,
        )
        return budget

    def _resolve_manual_attempt_scope(
        self,
        workspace_id: str,
        chapter_id: str,
        *,
        task_id: str | None,
    ) -> dict[str, Any]:
        """把暂停中 run 的章节 UI 修复归回原活动 attempt scope。

        R8 ch-0003 需要先在章节 UI 显式重写，再回 Run UI 续跑。旧入口会新建
        ``run_id=None`` 的独立 scope，导致调用脱离原 run/cap。这里只在唯一
        active run 已暂停/中断且 current chapter 匹配时附着；running 时以
        数据库状态拒绝并发手工任务。调用方仍须保证产品进程唯一，不能把这里
        当成跨进程的通用任务租约。
        """

        binding = self.active_run_model_manifest_binding(workspace_id)
        from src.services.post_v8_abc_selection_activity import (
            selection_attempt_scope_kwargs,
        )

        default_scope = selection_attempt_scope_kwargs(
            workspace_id,
            task_id=task_id,
            chapter_id=chapter_id,
        )

        def _with_manifest_lock(
            scope: dict[str, Any],
            *,
            checkpoint_run: PipelineRunORM | None,
        ) -> dict[str, Any]:
            stored = (
                checkpoint_run.profile_override
                if checkpoint_run is not None
                else None
            )
            stored_is_manifest = isinstance(stored, str) and (
                stored.startswith("run-model-manifest:")
                or stored.startswith("selection-test:")
            )
            if checkpoint_run is not None and (binding is not None or stored_is_manifest):
                expected_tokens = (
                    {binding.token, binding.selection_test.token}
                    if binding is not None
                    else set()
                )
                if binding is None or stored not in expected_tokens:
                    raise ChapterServiceError(
                        "章节 checkpoint 的单模型 manifest identity 已漂移，"
                        "已在 Provider 前停止"
                    )
            if binding is None:
                return scope
            locked_scope = {
                **scope,
                "run_model_manifest_token": binding.token,
            }
            if stored != binding.selection_test.token:
                return locked_scope
            selection_test = binding.selection_test
            if (
                selection_test.max_physical_requests <= 0
                or selection_test.max_configured_rmb <= 0
            ):
                raise ChapterServiceError(
                    "selection-test loss breakers 仍为 0/0，活动未获授权"
                )
            return {
                **locked_scope,
                "provider_call_cap": selection_test.max_physical_requests,
                "activity_scope": "selection_test",
                "selection_test_manifest_token": selection_test.token,
                "configured_cost_currency": selection_test.cost_currency,
                "configured_cost_cap": selection_test.max_configured_rmb,
            }

        session = self._open_session(workspace_id)
        if session is None:
            return _with_manifest_lock(default_scope, checkpoint_run=None)
        try:
            active_rows = list(
                session.scalars(
                    select(PipelineRunORM)
                    .where(PipelineRunORM.workspace_id == workspace_id)
                    .where(
                        PipelineRunORM.state.in_(("running", "paused", "interrupted"))
                    )
                    .order_by(PipelineRunORM.created_at.desc())
                )
            )
            if len(active_rows) > 1:
                raise ChapterServiceError(
                    "同一工作区存在多个 active pipeline run，拒绝启动章节任务"
                )
            if not active_rows:
                latest_error = session.scalar(
                    select(PipelineRunORM)
                    .where(PipelineRunORM.workspace_id == workspace_id)
                    .where(PipelineRunORM.state == "error")
                    .order_by(PipelineRunORM.updated_at.desc())
                    .limit(1)
                )
                details = latest_error.error_details if latest_error else None
                if not (
                    latest_error is not None
                    and isinstance(details, dict)
                    and details.get("error_type") == "ContinuityLedgerNotReadyError"
                    and latest_error.current_chapter == chapter_id
                    and chapter_id in set(latest_error.chapter_ids or [])
                    and chapter_id not in set(latest_error.completed_chapter_ids or [])
                    and chapter_id not in set(latest_error.skipped_chapter_ids or [])
                ):
                    return _with_manifest_lock(
                        default_scope,
                        checkpoint_run=None,
                    )
                return _with_manifest_lock(
                    {
                        "workspace_id": workspace_id,
                        "run_id": latest_error.run_id,
                        "task_id": task_id,
                        "chapter_id": chapter_id,
                        "provider_call_cap": latest_error.provider_call_cap,
                    },
                    checkpoint_run=latest_error,
                )
            run = active_rows[0]
            if run.state == "running":
                raise ChapterServiceError(
                    "pipeline 正在运行，拒绝并发启动章节任务；请先从 Run UI 暂停"
                )
            if chapter_id not in set(run.chapter_ids or []):
                return _with_manifest_lock(default_scope, checkpoint_run=None)
            if chapter_id in set(run.completed_chapter_ids or []) or (
                chapter_id in set(run.skipped_chapter_ids or [])
            ):
                raise ChapterServiceError(
                    "章节已在 pipeline 完成/跳过账本中，拒绝重复启动修复任务"
                )
            if run.current_chapter != chapter_id:
                raise ChapterServiceError(
                    "章节不是暂停中 pipeline 的当前 checkpoint，拒绝跨章修复"
                )
            return _with_manifest_lock(
                {
                    "workspace_id": workspace_id,
                    "run_id": run.run_id,
                    "task_id": task_id,
                    "chapter_id": chapter_id,
                    "provider_call_cap": run.provider_call_cap,
                },
                checkpoint_run=run,
            )
        finally:
            session.close()

    def _upsert_chapter_state(
        self,
        workspace_id: str,
        chapter_id: str,
        *,
        state: ChapterStateValue,
    ) -> None:
        """upsert ``chapters`` 行 ``status`` 字段。

        - 无 session_factory → no-op(P1 兼容)。
        - 行不存在 → ``ensure_workspace_orm`` 先把 WorkspaceORM 同步落库
          (满足 FK),再 insert ChapterORM(必需字段用合理默认:
          ``current_version=0``、计数=0、``approved_at`` 仅 ``approved`` 时
          填 now)。
        - 行已存在 → 更新 ``status``;``approved_at`` 在首次进 ``approved``
          时填 now(已填则不覆盖)。

        commit/rollback 边界全在本方法内,失败抛原异常给 caller。
        """
        session = self._open_session(workspace_id)
        if session is None:
            return
        try:
            row = session.get(ChapterORM, chapter_id)
            is_first_approve = False
            if row is None:
                # 满足 FK:先 sync WorkspaceORM 行(P2-B-13 ensure_workspace_orm)
                workspace = self._workspace_service.info(workspace_id)
                ensure_workspace_orm(session, workspace)
                now = datetime.now(timezone.utc)
                row = ChapterORM(
                    chapter_id=chapter_id,
                    workspace_id=workspace_id,
                    current_version=0,
                    status=state,
                    rewrite_count=0,
                    revise_count=0,
                    created_at=now,
                    approved_at=now if state == "approved" else None,
                    total_token_usage={},
                    total_cost_usd=0.0,
                )
                session.add(row)
                # 新章节首次落 approved → 计入 workspace 完成数
                if state == "approved":
                    is_first_approve = True
            else:
                row.status = state
                if state == "approved" and row.approved_at is None:
                    row.approved_at = datetime.now(timezone.utc)
                    is_first_approve = True
            # A-03 hot-fix:首次进 approved 时同步 workspace.completed_chapters += 1
            # (同事务,失败一起 rollback,避免计数与状态漂移)
            if is_first_approve:
                ws_row = session.get(WorkspaceORM, workspace_id)
                if ws_row is not None:
                    ws_row.completed_chapters = ws_row.completed_chapters + 1
            session.commit()
        except Exception:
            session.rollback()
            raise
        finally:
            session.close()

        # v1.0.x A2-03 续修:ORM commit 成功后同步 meta.json,保持两源一致
        # (轮 152 A-03 只修 ORM,导致前端 list_workspaces 读 meta.json 永显 0/N)
        if is_first_approve:
            self._workspace_service.sync_completed_chapters_to_meta(
                workspace_id, delta=1
            )

    def _mark_ready_for_next_chapter(
        self,
        workspace_id: str,
        chapter_id: str,
        *,
        paths: WorkspacePaths,
        final_path: Path | None,
        final_version_id: int | None,
    ) -> None:
        """写入 W3.5-C ``ready_for_next_chapter`` 硬水位。

        该方法是硬账本,失败必须冒泡:下一章不能在终稿缺失、DB 状态未 approved、
        或 ready 时间戳落库失败时继续。富记忆状态同时重置为 ``pending``,
        后台任务再推进到 running/succeeded/failed。
        """
        if self._session_factory is None:
            log_stage_timing(
                _log,
                "chapter_stage_timing",
                workspace_id=workspace_id,
                chapter_id=chapter_id,
                stage="ready_for_next_chapter",
                reason="session_factory_missing",
            )
            return
        if final_path is None or not final_path.is_file():
            raise ChapterServiceError(
                f"章节已通过但终稿缺失,无法进入下一章: chapter_id={chapter_id}"
            )
        if final_version_id is None:
            raise ContinuityLedgerNotReadyError(
                chapter_id=chapter_id,
                reason="orchestrator 未返回 final_version_id",
            )
        try:
            ledger = load_validated_continuity_ledger(
                workspace_id=workspace_id,
                chapter_id=chapter_id,
                paths=paths,
                session_factory=self._session_factory,
                expected_version_id=final_version_id,
            )
        except ContinuityLedgerValidationError as exc:
            raise ContinuityLedgerNotReadyError(
                chapter_id=chapter_id,
                reason=str(exc),
            ) from exc
        card = self.read_chapter_card(workspace_id, chapter_id)
        try:
            core_state_commit_hash = CoreStateService(
                session_factory=self._session_factory
            ).require_committed(
                workspace_id=workspace_id,
                card=card,
                source_version_id=final_version_id,
            )
        except CoreStateServiceError as exc:
            raise ContinuityLedgerNotReadyError(
                chapter_id=chapter_id,
                reason=f"核心状态提交未闭合: {exc}",
            ) from exc
        session = self._open_session(workspace_id)
        if session is None:
            log_stage_timing(
                _log,
                "chapter_stage_timing",
                workspace_id=workspace_id,
                chapter_id=chapter_id,
                stage="ready_for_next_chapter",
                reason="session_unavailable",
            )
            return
        with StageTimer(
            _log,
            "chapter_stage_timing",
            {
                "workspace_id": workspace_id,
                "chapter_id": chapter_id,
                "stage": "ready_for_next_chapter",
                "final_version_id": final_version_id,
            },
        ):
            try:
                row = session.get(ChapterORM, chapter_id)
                if row is None or row.workspace_id != workspace_id:
                    raise ChapterServiceError(
                        f"章节 ready 水位缺少 DB 行: chapter_id={chapter_id}"
                    )
                if row.status != ChapterState.APPROVED.value:
                    raise ChapterServiceError(
                        f"章节 ready 水位状态不正确: chapter_id={chapter_id} "
                        f"status={row.status!r}"
                    )
                now = datetime.now(timezone.utc)
                row.ready_for_next_chapter_at = now
                row.memory_enrichment_status = "pending"
                row.memory_enriched_at = None
                row.memory_enrichment_details = {
                    "schema_version": 1,
                    "ready_at": now.isoformat(),
                    "final_version_id": final_version_id,
                    "final_path": str(final_path),
                    "continuity_ledger_hash": ledger.ledger_hash,
                    "continuity_ledger_source_text_hash": ledger.source_text_hash,
                    "core_state_commit_hash": core_state_commit_hash,
                }
                session.commit()
                _log.info(
                    "chapter_ready_for_next",
                    workspace_id=workspace_id,
                    chapter_id=chapter_id,
                    final_version_id=final_version_id,
                    continuity_ledger_hash=ledger.ledger_hash,
                    core_state_commit_hash=core_state_commit_hash,
                )
            except Exception:
                session.rollback()
                raise
            finally:
                session.close()

    def _validated_checkpoint_body_snapshot(
        self,
        *,
        workspace_id: str,
        paths: WorkspacePaths,
        chapter_id: str,
        source_version_id: int,
        authoritative_content: str,
    ) -> tuple[Path, str]:
        """复核 DB 全版本集合、规范 vN.md 与 final.md 三方逐字节一致。"""

        final_path = paths.chapter_version_file(chapter_id, "final.md")
        try:
            canonical_versions = collect_canonical_version_files(
                paths.chapter_dir(chapter_id)
            )
            disk_versions = {
                version_id: path.read_bytes()
                for version_id, path in canonical_versions.items()
            }
            final_bytes = final_path.read_bytes()
        except (ChapterVersioningError, OSError) as exc:
            raise ContinuityCheckpointCorruptionError(
                chapter_id=chapter_id,
                reason=(
                    "正文版本目录含非规范别名、文件缺失或不可读: "
                    f"{type(exc).__name__}: {exc}"
                ),
                evidence=[
                    f"final_path={final_path}",
                    f"source_version_id={source_version_id}",
                ],
            ) from exc

        db_versions: dict[int, str] = {}
        if self._session_factory is not None:
            session = self._open_session(workspace_id)
            if session is None:
                raise ContinuityCheckpointCorruptionError(
                    chapter_id=chapter_id,
                    reason="正文 checkpoint 无法打开 DB session",
                )
            try:
                rows = list(
                    session.scalars(
                        select(ChapterVersionORM)
                        .where(ChapterVersionORM.chapter_id == chapter_id)
                        .order_by(ChapterVersionORM.version_id)
                    )
                )
                db_versions = {row.version_id: row.content for row in rows}
            finally:
                session.close()
            if not db_versions:
                raise ContinuityCheckpointCorruptionError(
                    chapter_id=chapter_id,
                    reason="正文 checkpoint 缺少 DB 权威版本集合",
                )
            if set(disk_versions) != set(db_versions):
                raise ContinuityCheckpointCorruptionError(
                    chapter_id=chapter_id,
                    reason="磁盘 vN.md 集合与 DB immutable 版本集合不一致，拒绝出网",
                    evidence=[
                        f"disk_versions={sorted(disk_versions)}",
                        f"db_versions={sorted(db_versions)}",
                    ],
                )
            for version_id, db_content in db_versions.items():
                if disk_versions[version_id] != db_content.encode("utf-8"):
                    raise ContinuityCheckpointCorruptionError(
                        chapter_id=chapter_id,
                        reason=(
                            "磁盘 vN.md 与 DB immutable 正文不一致，拒绝出网: "
                            f"version_id={version_id}"
                        ),
                    )

        version_bytes = disk_versions.get(source_version_id)
        if version_bytes is None:
            raise ContinuityCheckpointCorruptionError(
                chapter_id=chapter_id,
                reason=f"正文 checkpoint 缺少源版本 v{source_version_id}.md",
            )
        authoritative_bytes = authoritative_content.encode("utf-8")
        if db_versions and db_versions.get(source_version_id) != authoritative_content:
            raise ContinuityCheckpointCorruptionError(
                chapter_id=chapter_id,
                reason=(
                    "调用方正文快照与 DB 权威正文不一致，拒绝出网: "
                    f"version_id={source_version_id}"
                ),
            )
        if version_bytes != authoritative_bytes:
            raise ContinuityCheckpointCorruptionError(
                chapter_id=chapter_id,
                reason=(
                    "vN.md 与 DB 权威正文不一致，拒绝出网: "
                    f"version_id={source_version_id}"
                ),
                evidence=[f"version_path={canonical_versions[source_version_id]}"],
            )
        if final_bytes != authoritative_bytes:
            raise ContinuityCheckpointCorruptionError(
                chapter_id=chapter_id,
                reason=(
                    "final.md 与 DB 权威正文不一致，拒绝出网: "
                    f"version_id={source_version_id}"
                ),
                evidence=[f"final_path={final_path}"],
            )
        return final_path, authoritative_content

    def resume_approved_checkpoint(
        self,
        workspace_id: str,
        chapter_id: str,
        *,
        profile_override: str | None = None,
    ) -> bool:
        """只恢复 ``approved -> continuity ledger -> ready`` 后处理断点。

        ``True`` 表示目标章已经是 approved checkpoint，本方法已确认或补齐
        ``ready_for_next_chapter``；``False`` 表示目标章不是该 checkpoint，
        caller 应继续走正常 ``write``。恢复路径绝不进入 Orchestrator，也不
        生成、重审或覆盖正文版本。

        在任何 Provider 调用前必须同时确认：DB 状态为 ``approved``、immutable
        ``chapter_versions`` 有唯一最新版本投影、``final.md`` 与该 ``vN.md``
        都逐字节等于 DB 权威正文、章节卡身份正确。任一不一致都按 checkpoint
        损坏 fail closed。
        """
        if self._session_factory is None:
            return False

        paths = self._validate_workspace(workspace_id)
        final_path = paths.chapter_version_file(chapter_id, "final.md")
        version_paths = sorted(
            path
            for path in paths.chapter_dir(chapter_id).glob("v*.md")
            if path.is_file()
        )
        file_evidence: list[str] = []
        if final_path.is_file():
            file_evidence.append("final.md")
        if version_paths:
            file_evidence.append(
                "version_files=" + ",".join(path.name for path in version_paths)
            )

        session = self._session_factory(workspace_id)
        try:
            row = session.get(ChapterORM, chapter_id)
            if row is None:
                if file_evidence:
                    raise ContinuityCheckpointCorruptionError(
                        chapter_id=chapter_id,
                        reason="DB 章节行缺失但磁盘留有正文版本",
                        evidence=file_evidence,
                    )
                return False
            if row.workspace_id != workspace_id:
                raise ContinuityCheckpointCorruptionError(
                    chapter_id=chapter_id,
                    reason="DB 章节行 workspace_id 不匹配",
                    evidence=[f"row.workspace_id={row.workspace_id}", *file_evidence],
                )
            if row.status not in (
                ChapterState.APPROVED.value,
                ChapterState.PUBLISHED.value,
            ):
                pass_evidence = list(file_evidence if final_path.is_file() else [])
                if row.approved_at is not None:
                    pass_evidence.append(f"approved_at={row.approved_at.isoformat()}")
                if pass_evidence:
                    raise ContinuityCheckpointCorruptionError(
                        chapter_id=chapter_id,
                        reason=(
                            "DB 状态不是 approved/published，但留有已通过痕迹 "
                            f"status={row.status}"
                        ),
                        evidence=pass_evidence,
                    )
                return False
            is_force_pass_checkpoint = row.force_pass_at is not None
            checkpoint_approved_at = row.approved_at or row.created_at
            ready_checkpoint_exists = row.ready_for_next_chapter_at is not None
            # R8:这道门必须位于 ready 分支之前。approved + ready=None 若错误
            # 出现 running，说明来源水位互相矛盾，必须在补 ledger/ready 前硬停。
            # ready 已存在时，running 可能是当前进程的合法后台 Worker；pending
            # 则由下面的 checkpoint 重排路径复核 provenance 后交给原子 CAS。
            _require_memory_enrichment_checkpoint_safe(
                row=row,
                chapter_id=chapter_id,
                evidence=[f"current_version={row.current_version}", *file_evidence],
                block_running=not ready_checkpoint_exists,
            )
            if ready_checkpoint_exists:
                # 并发/重复 resume 的幂等路径：已有水位先完整复核。只有合法
                # pending 才重新排队；已知、完整回滚的 runtime extractor
                # failure 仅允许正式 Pipeline 的显式 Continue 走窄恢复。running /
                # succeeded、冷启动收口和未知副作用仍不得重排。
                already_ready = True
                source_version_id = row.current_version
                requeue_pending_memory = row.memory_enrichment_status == "pending"
                retry_known_failed_memory = (
                    row.memory_enrichment_status == "failed"
                )
            else:
                already_ready = False
                requeue_pending_memory = False
                retry_known_failed_memory = False
                if row.status != ChapterState.APPROVED.value:
                    raise ContinuityCheckpointCorruptionError(
                        chapter_id=chapter_id,
                        reason="published 章节缺少 ready_for_next_chapter 水位",
                        evidence=[
                            f"status={row.status}",
                            f"current_version={row.current_version}",
                            *file_evidence,
                        ],
                    )
                authoritative_version = session.scalar(
                    select(ChapterVersionORM)
                    .where(ChapterVersionORM.chapter_id == chapter_id)
                    .order_by(ChapterVersionORM.version_id.desc())
                    .limit(1)
                )
                if authoritative_version is None:
                    raise ContinuityCheckpointCorruptionError(
                        chapter_id=chapter_id,
                        reason="approved checkpoint 缺少 DB 权威正文版本",
                        evidence=file_evidence,
                    )
                source_version_id = authoritative_version.version_id
                authoritative_content = authoritative_version.content
                if row.current_version != source_version_id:
                    raise ContinuityCheckpointCorruptionError(
                        chapter_id=chapter_id,
                        reason=(
                            "approved checkpoint 版本投影与权威账本不一致: "
                            f"projection={row.current_version} "
                            f"authoritative={source_version_id}"
                        ),
                        evidence=file_evidence,
                    )
        finally:
            session.close()

        if already_ready:
            self.require_ready_for_next_chapter(workspace_id, chapter_id)
            if requeue_pending_memory:
                self._schedule_pending_memory_enrichment_checkpoint(
                    workspace_id=workspace_id,
                    chapter_id=chapter_id,
                    final_version_id=source_version_id,
                )
            elif retry_known_failed_memory:
                self._retry_known_failed_memory_enrichment_checkpoint(
                    workspace_id=workspace_id,
                    chapter_id=chapter_id,
                    final_version_id=source_version_id,
                )
            return True

        def _raise_local_recovery_error(stage: str, exc: Exception) -> NoReturn:
            """approved checkpoint 后本地失败不可退回普通 skip/retry。"""

            from src.api.errors import CostGuardTriggered

            if isinstance(
                exc,
                (
                    ContinuityCheckpointCorruptionError,
                    ContinuityLedgerNotReadyError,
                    CostGuardTriggered,
                    ProviderCallCapExceeded,
                ),
            ):
                raise exc
            raise ContinuityCheckpointCorruptionError(
                chapter_id=chapter_id,
                reason=f"checkpoint recovery {stage} 本地失败: "
                f"{type(exc).__name__}: {exc}",
                evidence=file_evidence,
            ) from exc

        try:
            card = self.read_chapter_card(workspace_id, chapter_id)
        except Exception as exc:  # noqa: BLE001 — checkpoint 本地错误统一硬停
            _raise_local_recovery_error("read_card", exc)
        if card.chapter_id != chapter_id or card.workspace_id != workspace_id:
            raise ContinuityCheckpointCorruptionError(
                chapter_id=chapter_id,
                reason=(
                    "approved checkpoint 章节卡身份不匹配: "
                    f"workspace={workspace_id} card.workspace={card.workspace_id} "
                    f"card.chapter={card.chapter_id}"
                ),
                evidence=file_evidence,
            )

        final_path, authoritative_content = self._validated_checkpoint_body_snapshot(
            workspace_id=workspace_id,
            paths=paths,
            chapter_id=chapter_id,
            source_version_id=source_version_id,
            authoritative_content=authoritative_content,
        )

        try:
            self._run_gates(workspace_id, chapter_id=chapter_id)
        except Exception as exc:  # noqa: BLE001 — 保留 cost，其余本地错误硬停
            _raise_local_recovery_error("card_gate", exc)
        try:
            router = (
                self._router_factory(workspace_id)
                if self._router_factory is not None
                else None
            )
            if router is not None and card.force_profile is not None:
                if not router.has_profile(card.force_profile):
                    known = sorted(router._config.profiles.keys())
                    exc = WriterProfileNotFoundError(
                        f"章节卡 force_profile={card.force_profile!r} 未在 "
                        f"writer_profiles 声明; 已声明: {known}"
                    )
                    exc.details = {
                        "profile_id": card.force_profile,
                        "known_profiles": known,
                    }
                    raise exc
            if router is not None:
                effective_override = card.force_profile or profile_override
                router = wrap_router_with_profile_override(router, effective_override)
                session_factory_local = self._session_factory
                router = wrap_router_with_cost_tracking(
                    router,
                    workspace_id=workspace_id,
                    chapter_id=chapter_id,
                    session_factory=lambda: session_factory_local(workspace_id),
                )
        except Exception as exc:  # noqa: BLE001 — router 构造属于本地恢复
            _raise_local_recovery_error("router_setup", exc)

        operation_budget = self._read_authoritative_budget(workspace_id, chapter_id)
        operation_lease = self._reserve_chapter_operation_lease(
            workspace_id=workspace_id,
            chapter_id=chapter_id,
            operation="checkpoint-resume",
            source_version=operation_budget.current_version,
        )

        with bind_chapter_context(
            workspace_id=workspace_id,
            chapter_id=chapter_id,
            trace_id=make_trace_id(chapter_id, "write", 0),
        ):
            try:
                try:
                    load_validated_continuity_ledger(
                        workspace_id=workspace_id,
                        chapter_id=chapter_id,
                        paths=paths,
                        session_factory=self._session_factory,
                        expected_version_id=source_version_id,
                    )
                    ledger_already_materialized = True
                except ContinuityLedgerValidationError:
                    ledger_already_materialized = False

                summary_refresh = False
                if ledger_already_materialized and not is_force_pass_checkpoint:
                    recovery_session = self._session_factory(workspace_id)
                    try:
                        checkpoint = recovery_session.get(ChapterORM, chapter_id)
                        if checkpoint is not None and (
                            checkpoint.core_state_commit_details or {}
                        ).get("conflicts"):
                            summary_refresh = CoreStateService(
                                session_factory=self._session_factory
                            ).require_pending_transition_candidate_recovery_safe(
                                session=recovery_session,
                                workspace_id=workspace_id,
                                card=card,
                                paths=paths,
                            )
                            if not summary_refresh:
                                from src.services.relation_registration_recovery import recovery_payload, reconcile_registration, registration_request_not_dispatched
                                from src.core.db.models import ChapterSummaryORM
                                core_service = CoreStateService(session_factory=self._session_factory)
                                old_summary = recovery_session.get(ChapterSummaryORM, chapter_id)
                                assert old_summary is not None
                                registration_payload = recovery_payload(card=card,
                                    candidates=core_service._parse_candidates(old_summary),
                                    projection=core_service._projection_from_session(recovery_session,
                                        workspace_id=workspace_id, before_chapter=card.chapter_number),
                                    paths=paths, text=authoritative_content,
                                    ledger_hash=old_summary.ledger_hash, source_version_id=source_version_id)
                                if registration_payload is not None:
                                    if operation_lease is not None and self._operation_lease_store is not None:
                                        operation_lease = self._operation_lease_store.mark_phase(
                                            operation_lease, phase="continuity_ledger_may_call_provider")
                                    reconcile_registration(payload=registration_payload, paths=paths,
                                        router=router, client=self._client, model=self._model,
                                        no_dispatch_proof=lambda path, record: registration_request_not_dispatched(
                                            session=recovery_session, workspace_id=workspace_id,
                                            chapter_id=chapter_id, path=path, record=record))
                            if summary_refresh:
                                from src.core.db.models import ChapterSummaryORM

                                old_summary = recovery_session.get(
                                    ChapterSummaryORM, chapter_id
                                )
                                assert old_summary is not None
                                archive_path = paths.chapter_version_file(
                                    chapter_id,
                                    f"summary-recovery-{old_summary.ledger_hash}.json",
                                )
                                with archive_path.open("x", encoding="utf-8") as archive:
                                    json.dump(
                                        {column.name: getattr(old_summary, column.name)
                                         for column in old_summary.__table__.columns},
                                        archive, ensure_ascii=False, indent=2, default=str,
                                    )
                    finally:
                        recovery_session.close()

                if not ledger_already_materialized or summary_refresh:
                    if is_force_pass_checkpoint:
                        raise ContinuityCheckpointCorruptionError(
                            chapter_id=chapter_id,
                            reason=(
                                "force-pass approved checkpoint 缺少此前已持久化的"
                                "连续性小账本，禁止重复 Provider 请求"
                            ),
                            evidence=[
                                f"source_version_id={source_version_id}",
                                *file_evidence,
                            ],
                        )
                    attempt_session = self._session_factory(workspace_id)
                    try:
                        previous_attempt_count = int(
                            attempt_session.scalar(
                                select(func.count())
                                .select_from(AttemptLedgerORM)
                                .where(
                                    AttemptLedgerORM.workspace_id == workspace_id,
                                    AttemptLedgerORM.chapter_id == chapter_id,
                                    AttemptLedgerORM.attempt_kind == "provider",
                                    AttemptLedgerORM.agent_name
                                    == "chapter_summary_extractor",
                                    AttemptLedgerORM.status.in_(
                                        ("started", "succeeded")
                                    ),
                                    AttemptLedgerORM.created_at
                                    >= checkpoint_approved_at,
                                )
                            )
                            or 0
                        )
                    finally:
                        attempt_session.close()
                    if previous_attempt_count and not summary_refresh:
                        raise ContinuityCheckpointCorruptionError(
                            chapter_id=chapter_id,
                            reason=(
                                "连续性小账本未能验真，但本 checkpoint 已有 "
                                "Provider attempt；结果未知，禁止重复请求"
                            ),
                            evidence=[
                                f"provider_attempt_count={previous_attempt_count}",
                                f"source_version_id={source_version_id}",
                            ],
                        )
                    if (
                        operation_lease is not None
                        and self._operation_lease_store is not None
                    ):
                        operation_lease = self._operation_lease_store.mark_phase(
                            operation_lease,
                            phase="continuity_ledger_may_call_provider",
                        )
                    with StageTimer(
                        _log,
                        "chapter_stage_timing",
                        {
                            "workspace_id": workspace_id,
                            "chapter_id": chapter_id,
                            "stage": "continuity_ledger_checkpoint_resume",
                            "final_version_id": source_version_id,
                        },
                    ):
                        extract_and_persist_continuity_ledger(
                            workspace_id=workspace_id,
                            chapter_id=chapter_id,
                            source_version_id=source_version_id,
                            paths=paths,
                            session_factory=self._session_factory,
                            llm_client=self._client,
                            llm_model=self._model,
                            router=router,
                            trigger="reviewer_pass",
                            chapter_text_snapshot=authoritative_content,
                            chapter_card_snapshot=card,
                            non_retryable_exceptions=(ProviderCallCapExceeded,),
                        )
            except ContinuityLedgerValidationError as exc:
                raise ContinuityLedgerNotReadyError(
                    chapter_id=chapter_id,
                    reason=str(exc),
                ) from exc
            except ProviderCallCapExceeded:
                raise
            except ContinuityCheckpointCorruptionError:
                # started/succeeded 但无 ledger 是未消费响应现场，不得降格成
                # 普通 not-ready；保留 corruption 分类让 Pipeline 硬拦。
                raise
            except ContinuityLedgerNotReadyError:
                raise
            except Exception as exc:  # noqa: BLE001 — ledger 阶段保持 not-ready 分类
                raise ContinuityLedgerNotReadyError(
                    chapter_id=chapter_id,
                    reason=f"{type(exc).__name__}: {exc}",
                ) from exc

            try:
                CoreStateService(
                    session_factory=self._session_factory
                ).commit_approved_chapter(
                    workspace_id=workspace_id,
                    card=card,
                    source_version_id=source_version_id,
                    paths=paths,
                )
                self._mark_ready_for_next_chapter(
                    workspace_id=workspace_id,
                    chapter_id=chapter_id,
                    paths=paths,
                    final_path=final_path,
                    final_version_id=source_version_id,
                )
                progression_enabled, progression_label = (
                    self._resolve_progression_consistency(workspace_id)
                )
                session_factory_local = self._session_factory
            except CoreStateConflictError:
                # 已知候选证据不足仍是业务冲突，不能伪装成文件/账本损坏。
                raise
            except Exception as exc:  # noqa: BLE001 — ready 本地错误统一硬停
                _raise_local_recovery_error("mark_ready", exc)

            def pipeline_session_factory() -> Session:
                return session_factory_local(workspace_id)

            try:
                self._schedule_memory_enrichment_after_ready(
                    workspace_id=workspace_id,
                    chapter_id=chapter_id,
                    final_version_id=source_version_id,
                    paths=paths,
                    card=card,
                    router=router,
                    final_path=final_path,
                    session_factory=pipeline_session_factory,
                    progression_consistency_enabled=progression_enabled,
                    progression_label=progression_label,
                )
            except Exception as exc:  # noqa: BLE001 — 调度失败保留 checkpoint
                _raise_local_recovery_error("memory_schedule", exc)

            if operation_lease is not None and self._operation_lease_store is not None:
                operation_lease = self._operation_lease_store.mark_phase(
                    operation_lease,
                    phase="operation_complete",
                )
                if current_attempt_scope() is None:
                    self._operation_lease_store.release(operation_lease)

        _log.info(
            "approved_checkpoint_resumed",
            workspace_id=workspace_id,
            chapter_id=chapter_id,
            final_version_id=source_version_id,
        )
        return True

    def _schedule_pending_memory_enrichment_checkpoint(
        self,
        *,
        workspace_id: str,
        chapter_id: str,
        final_version_id: int,
    ) -> None:
        """复核并重排 ``ready + pending`` 的失去调度器 checkpoint。

        ready 落库后、后台线程真正执行 ``pending -> running`` CAS 前也存在进程
        崩溃窗。Continue 只对仍然严格匹配 final/ledger provenance 的 pending
        状态重新提交原产品 Worker；重复 Continue 即使提交多个闭包，也只有一个
        能取得 CAS。running/failed/succeeded 等并发新状态一律不重排。
        """

        if self._session_factory is None:
            return
        paths = self._paths(workspace_id)
        progression_enabled, progression_label = (
            self._resolve_progression_consistency(workspace_id)
        )
        expected_ledger_hash, expected_source_text_hash = (
            self._validated_memory_enrichment_source_checkpoint(
                workspace_id=workspace_id,
                chapter_id=chapter_id,
                final_version_id=final_version_id,
                paths=paths,
            )
        )
        session = self._session_factory(workspace_id)
        try:
            row = session.get(ChapterORM, chapter_id)
            if row is None or row.workspace_id != workspace_id:
                raise ContinuityCheckpointCorruptionError(
                    chapter_id=chapter_id,
                    reason="pending 富记忆重排时章节行缺失或 workspace 串位",
                )
            if row.memory_enrichment_status != "pending":
                return
            try:
                self._validate_memory_enrichment_provenance(
                    row=row,
                    paths=paths,
                    expected_final_version_id=final_version_id,
                    expected_ledger_hash=expected_ledger_hash,
                    expected_ledger_source_text_hash=expected_source_text_hash,
                )
            except MemoryEnrichmentCheckpointShapeError as exc:
                raise ContinuityCheckpointCorruptionError(
                    chapter_id=chapter_id,
                    reason=f"pending 富记忆来源损坏，禁止重排: {exc}",
                    evidence=[f"current_version={row.current_version}"],
                ) from exc
            ready_at = row.ready_for_next_chapter_at
            assert ready_at is not None
            try:
                unconsumed_attempts = load_unconsumed_memory_provider_attempts(
                    session,
                    workspace_id=workspace_id,
                    chapter_id=chapter_id,
                    anchor=ready_at,
                )
            except MemoryEnrichmentAttemptLedgerError as exc:
                raise ContinuityCheckpointCorruptionError(
                    chapter_id=chapter_id,
                    reason=f"pending 富记忆 attempt 账本损坏，禁止重排: {exc}",
                ) from exc
            # 显式 Continue 的已知失败恢复会先留下 durable consumption receipt，
            # 再 CAS 到 pending。若进程恰好死在 pending->running 之前，后续
            # Continue 只忽略 receipt 精确列出的旧终态请求；任何新 started /
            # succeeded attempt 仍会原样阻断，不能借历史记录吞掉未知响应。
            consumed_attempt_ids: set[str] = set()
            details = row.memory_enrichment_details
            if isinstance(details, dict):
                history = details.get("retry_history")
                if isinstance(history, list):
                    for record in history:
                        if (
                            not isinstance(record, dict)
                            or record.get("trigger")
                            != "pipeline_explicit_continue"
                            or record.get("automatic_provider_replay") is not False
                        ):
                            continue
                        attempt_ids = record.get("consumed_provider_attempt_ids")
                        if isinstance(attempt_ids, list) and all(
                            isinstance(attempt_id, str) and attempt_id
                            for attempt_id in attempt_ids
                        ):
                            consumed_attempt_ids.update(attempt_ids)
            unconsumed_attempts = [
                attempt
                for attempt in unconsumed_attempts
                if attempt["attempt_id"] not in consumed_attempt_ids
            ]
            if unconsumed_attempts:
                raise ContinuityCheckpointCorruptionError(
                    chapter_id=chapter_id,
                    reason=(
                        "pending 富记忆已有可能未消费的 Provider 响应/请求，"
                        "禁止重复排 Worker"
                    ),
                    evidence=[
                        "unconsumed_attempts="
                        + json.dumps(unconsumed_attempts, ensure_ascii=False)
                    ],
                )
        finally:
            session.close()

        def _task() -> None:
            self.run_memory_enrichment_for_ready_checkpoint(
                workspace_id,
                chapter_id,
                final_version_id=final_version_id,
                progression_consistency_enabled=progression_enabled,
                progression_label=progression_label,
            )

        scope = current_attempt_scope()
        if (
            scope is not None
            and scope.workspace_id == workspace_id
            and scope.chapter_id == chapter_id
            and scope.run_id is not None
            and scope.synchronous_memory_enrichment
        ):
            # 正式 Run 的 pending 恢复必须和首次 ready 路径同尺：同步消费
            # 同一个既有 Worker/CAS。否则生产配置的后台 scheduler 会让
            # Continue 立即撞回 pending，形成无法前进的恢复死循环。
            _task()
            return
        self._memory_enrichment_scheduler(_task)

    def _retry_known_failed_memory_enrichment_checkpoint(
        self,
        *,
        workspace_id: str,
        chapter_id: str,
        final_version_id: int,
    ) -> bool:
        """显式 Continue 后原子重试一次已知、无副作用的 extractor 失败。

        这里只接受正式 Pipeline attempt scope。它先复核终稿/ledger provenance、
        runtime 失败形状、SQLite 回滚后的当前摘要，以及账本中不存在 started
        富记忆请求；随后以 CAS 把 ``failed`` 还原成同一来源的 ``pending``，并
        同步消费原产品 Worker。Writer、Reviewer、章节版本和章节卡均不进入。
        """

        scope = current_attempt_scope()
        if (
            scope is None
            or scope.workspace_id != workspace_id
            or scope.chapter_id != chapter_id
            or scope.run_id is None
            or not scope.synchronous_memory_enrichment
        ):
            return False
        if self._session_factory is None:
            return False

        paths = self._paths(workspace_id)
        progression_enabled, progression_label = (
            self._resolve_progression_consistency(workspace_id)
        )
        expected_ledger_hash, expected_source_text_hash = (
            self._validated_memory_enrichment_source_checkpoint(
                workspace_id=workspace_id,
                chapter_id=chapter_id,
                final_version_id=final_version_id,
                paths=paths,
            )
        )
        session = self._session_factory(workspace_id)
        try:
            row = session.get(ChapterORM, chapter_id)
            if row is None or row.workspace_id != workspace_id:
                raise ContinuityCheckpointCorruptionError(
                    chapter_id=chapter_id,
                    reason="已知富记忆失败恢复时章节行缺失或 workspace 串位",
                )
            self._validate_memory_enrichment_provenance(
                row=row,
                paths=paths,
                expected_final_version_id=final_version_id,
                expected_ledger_hash=expected_ledger_hash,
                expected_ledger_source_text_hash=expected_source_text_hash,
            )
            original_details = row.memory_enrichment_details
            if not memory_enrichment_retryable_after_explicit_continue(
                status=row.memory_enrichment_status,
                details=original_details,
                enriched_at=row.memory_enriched_at,
            ):
                return False
            assert isinstance(original_details, dict)

            side_effect_baseline = original_details.get("side_effect_baseline")
            if not isinstance(side_effect_baseline, dict):
                raise ContinuityCheckpointCorruptionError(
                    chapter_id=chapter_id,
                    reason="已知富记忆失败缺少 SQLite 副作用基线，禁止重试",
                )
            current_sqlite = audit_sqlite_memory_snapshot(
                session=session,
                workspace_id=workspace_id,
                row=row,
            )
            if (
                current_sqlite.footprint
                != side_effect_baseline.get("sqlite_footprint")
                or current_sqlite.digest
                != side_effect_baseline.get("sqlite_digest")
            ):
                raise ContinuityCheckpointCorruptionError(
                    chapter_id=chapter_id,
                    reason=(
                        "富记忆失败后 SQLite 现场已偏离回滚基线，禁止重试"
                    ),
                    evidence=[
                        f"baseline_digest={side_effect_baseline.get('sqlite_digest')}",
                        f"current_digest={current_sqlite.digest}",
                    ],
                )

            ready_at = row.ready_for_next_chapter_at
            assert ready_at is not None
            try:
                prior_attempts = load_unconsumed_memory_provider_attempts(
                    session,
                    workspace_id=workspace_id,
                    chapter_id=chapter_id,
                    anchor=ready_at,
                )
            except MemoryEnrichmentAttemptLedgerError as exc:
                raise ContinuityCheckpointCorruptionError(
                    chapter_id=chapter_id,
                    reason=f"已知富记忆失败的 attempt 账本损坏: {exc}",
                ) from exc
            started_attempts = [
                attempt
                for attempt in prior_attempts
                if attempt.get("status") == "started"
                and attempt.get("agent_name") in RICH_MEMORY_PROVIDER_AGENTS
            ]
            if started_attempts:
                raise ContinuityCheckpointCorruptionError(
                    chapter_id=chapter_id,
                    reason="已知富记忆失败仍有 started Provider 请求，禁止重试",
                    evidence=[
                        "started_attempts="
                        + json.dumps(started_attempts, ensure_ascii=False)
                    ],
                )

            retry_record = {
                "schema_version": 1,
                "trigger": "pipeline_explicit_continue",
                "automatic_provider_replay": False,
                "requested_at": datetime.now(timezone.utc).isoformat(),
                "run_id": scope.run_id,
                "task_id": scope.task_id,
                "previous_started_at": original_details.get("started_at"),
                "previous_finished_at": original_details.get("finished_at"),
                "previous_lease_id": original_details.get("lease_id"),
                "previous_tasks": list(original_details.get("tasks", [])),
                "previous_failures": list(original_details.get("failures", [])),
                "memory_extraction_outcome": original_details.get(
                    "memory_extraction_outcome"
                ),
                "chapter_summary_vector_outcome": original_details.get(
                    "chapter_summary_vector_outcome"
                ),
                "previous_sqlite_digest": side_effect_baseline.get(
                    "sqlite_digest"
                ),
                "consumed_provider_attempt_ids": [
                    attempt["attempt_id"] for attempt in prior_attempts
                ],
            }
            prior_history = original_details.get("retry_history", [])
            assert isinstance(prior_history, list)
            pending_details = {
                key: original_details[key]
                for key in (
                    "schema_version",
                    "ready_at",
                    "final_version_id",
                    "final_path",
                    "continuity_ledger_hash",
                    "continuity_ledger_source_text_hash",
                )
            }
            # ready 已验真正文、ledger 与核心提交；完整保留稳定来源字段。
            # 历史重试漏了此键时，只重投影同源已提交 hash，不生成新核心状态。
            pending_details["core_state_commit_hash"] = original_details.get(
                "core_state_commit_hash", row.core_state_commit_hash
            )
            pending_details["retry_history"] = [
                *prior_history,
                retry_record,
            ]
            result = session.execute(
                update(ChapterORM)
                .where(ChapterORM.chapter_id == chapter_id)
                .where(ChapterORM.workspace_id == workspace_id)
                .where(ChapterORM.current_version == final_version_id)
                .where(
                    ChapterORM.status.in_(
                        (
                            ChapterState.APPROVED.value,
                            ChapterState.PUBLISHED.value,
                        )
                    )
                )
                .where(ChapterORM.ready_for_next_chapter_at == ready_at)
                .where(ChapterORM.memory_enrichment_status == "failed")
                .where(ChapterORM.memory_enriched_at.is_(None))
                .where(ChapterORM.memory_enrichment_details == original_details)
                .values(
                    memory_enrichment_status="pending",
                    memory_enriched_at=None,
                    memory_enrichment_details=pending_details,
                )
            )
            affected_rows = int(getattr(result, "rowcount", 0) or 0)
            if affected_rows != 1:
                session.rollback()
                return False
            session.commit()
        finally:
            session.close()

        _log.info(
            "memory_enrichment_explicit_retry_started",
            workspace_id=workspace_id,
            chapter_id=chapter_id,
            final_version_id=final_version_id,
            run_id=scope.run_id,
            task_id=scope.task_id,
        )
        self.run_memory_enrichment_for_ready_checkpoint(
            workspace_id,
            chapter_id,
            final_version_id=final_version_id,
            progression_consistency_enabled=progression_enabled,
            progression_label=progression_label,
        )
        return True

    def require_ready_for_next_chapter(
        self, workspace_id: str, chapter_id: str
    ) -> None:
        """PipelineService 写下一章前的显式等待条件校验。

        ``ChapterService.write`` 正常返回 APPROVED 前已经同步落 ready 水位,
        因此这里通常只是读库断言;若未来误改导致 write 提前返回,run loop 会
        在这里进入明确失败路径,不会带病续跑。
        """
        if self._session_factory is None:
            return
        session = self._open_session(workspace_id)
        if session is None:
            return
        try:
            row = session.get(ChapterORM, chapter_id)
            if row is None:
                raise ContinuityCheckpointCorruptionError(
                    chapter_id=chapter_id,
                    reason="ready 复核时 DB 章节行缺失",
                )
            if row.workspace_id != workspace_id:
                raise ContinuityCheckpointCorruptionError(
                    chapter_id=chapter_id,
                    reason="ready 复核时 DB workspace_id 不匹配",
                    evidence=[f"row.workspace_id={row.workspace_id}"],
                )
            if row.status not in (
                ChapterState.APPROVED.value,
                ChapterState.PUBLISHED.value,
            ):
                raise ContinuityCheckpointCorruptionError(
                    chapter_id=chapter_id,
                    reason=f"ready 复核时章节状态非 terminal: status={row.status}",
                )
            if row.ready_for_next_chapter_at is None:
                raise ContinuityCheckpointCorruptionError(
                    chapter_id=chapter_id,
                    reason="terminal 章节缺少 ready_for_next_chapter 水位",
                    evidence=[f"status={row.status}"],
                )
            # 本方法只验证 final + continuity ledger 的基础 ready 水位；合法
            # running 不在这里误判成孤儿 Worker。正式 Run 会紧接着调用
            # require_memory_enrichment_succeeded 再收紧到 succeeded；手动章节
            # 操作仍可独立使用这个基础水位。未知 status / 坏 details 继续硬停。
            _require_memory_enrichment_checkpoint_safe(
                row=row,
                chapter_id=chapter_id,
                evidence=[f"current_version={row.current_version}"],
                block_running=False,
            )
            authoritative_version = session.scalar(
                select(ChapterVersionORM)
                .where(ChapterVersionORM.chapter_id == chapter_id)
                .order_by(ChapterVersionORM.version_id.desc())
                .limit(1)
            )
            if authoritative_version is None:
                raise ContinuityCheckpointCorruptionError(
                    chapter_id=chapter_id,
                    reason="ready 复核时 DB 权威正文版本缺失",
                )
            expected_version_id = authoritative_version.version_id
            authoritative_content = authoritative_version.content
            ready_details = dict(row.memory_enrichment_details or {})
            known_retry_core_hash_omission = _is_known_retry_core_hash_omission(row)
            if row.current_version != expected_version_id:
                raise ContinuityCheckpointCorruptionError(
                    chapter_id=chapter_id,
                    reason=(
                        "ready 复核时 current_version 与 DB 最新版本不一致: "
                        f"projection={row.current_version} "
                        f"authoritative={expected_version_id}"
                    ),
                )
        finally:
            session.close()
        paths = self._paths(workspace_id)
        self._validated_checkpoint_body_snapshot(
            workspace_id=workspace_id,
            paths=paths,
            chapter_id=chapter_id,
            source_version_id=expected_version_id,
            authoritative_content=authoritative_content,
        )
        try:
            load_validated_continuity_ledger(
                workspace_id=workspace_id,
                chapter_id=chapter_id,
                paths=paths,
                session_factory=self._session_factory,
                expected_version_id=expected_version_id,
            )
        except ContinuityLedgerValidationError as exc:
            raise ContinuityCheckpointCorruptionError(
                chapter_id=chapter_id,
                reason=str(exc),
            ) from exc
        card_path = self._card_path(paths, chapter_id)
        if card_path.is_file():
            try:
                card = self.read_chapter_card(workspace_id, chapter_id)
                expected_core_hash = CoreStateService(
                    session_factory=self._session_factory
                ).require_committed(
                    workspace_id=workspace_id,
                    card=card,
                    source_version_id=expected_version_id,
                )
            except Exception as exc:  # noqa: BLE001 — ready 的核心来源必须验真
                raise ContinuityCheckpointCorruptionError(
                    chapter_id=chapter_id,
                    reason=f"ready 复核核心状态失败: {type(exc).__name__}: {exc}",
                ) from exc
            if (
                ready_details.get("core_state_commit_hash", "") != expected_core_hash
                and not known_retry_core_hash_omission
            ):
                raise ContinuityCheckpointCorruptionError(
                    chapter_id=chapter_id,
                    reason="ready 水位携带的核心状态 commit hash 与权威账本不一致",
                    evidence=[
                        "checkpoint="
                        + str(ready_details.get("core_state_commit_hash", "")),
                        "authoritative=" + expected_core_hash,
                    ],
                )
        elif ready_details.get("core_state_commit_hash"):
            raise ContinuityCheckpointCorruptionError(
                chapter_id=chapter_id,
                reason="ready 声明了核心状态 commit，但章节卡已丢失",
            )

    def require_memory_enrichment_succeeded(
        self, workspace_id: str, chapter_id: str
    ) -> None:
        """正式 Run 生成下一章卡前，复核上一章富记忆成功且来源仍权威。

        只读取现有 ``memory_enrichment_*`` checkpoint，不新增状态、轮询或
        Provider 请求。``pending/running`` 表示旧后台任务仍未结束；``failed`` /
        ``skipped`` 表示本轮不能保证最新记忆。调用方应暂停，成功后再 Continue，
        而不是降级到旧向量或盲目重放富记忆任务。
        """
        if self._session_factory is None:
            return
        session = self._open_session(workspace_id)
        if session is None:
            raise ContinuityCheckpointCorruptionError(
                chapter_id=chapter_id,
                reason="富记忆成功复核无法打开 DB session",
            )
        try:
            row = session.get(ChapterORM, chapter_id)
            if row is None:
                raise ContinuityCheckpointCorruptionError(
                    chapter_id=chapter_id,
                    reason="富记忆成功复核时 DB 章节行缺失",
                )
            if row.workspace_id != workspace_id:
                raise ContinuityCheckpointCorruptionError(
                    chapter_id=chapter_id,
                    reason="富记忆成功复核时 DB workspace_id 不匹配",
                    evidence=[f"row.workspace_id={row.workspace_id}"],
                )
            _require_memory_enrichment_checkpoint_safe(
                row=row,
                chapter_id=chapter_id,
                evidence=[f"current_version={row.current_version}"],
                block_running=False,
            )
            status = row.memory_enrichment_status
            if status != "succeeded":
                details = row.memory_enrichment_details
                retryable = memory_enrichment_retryable_after_explicit_continue(
                    status=status,
                    details=details,
                    enriched_at=row.memory_enriched_at,
                )
                manual_recovery_required = (
                    memory_enrichment_requires_manual_recovery(
                        status=status,
                        details=details,
                    )
                )
                failure_stages: list[str] = []
                failure_types: list[str] = []
                result_unknown = False
                if isinstance(details, dict):
                    result_unknown = details.get("result_unknown") is True
                    failures = details.get("failures")
                    if isinstance(failures, list):
                        for failure in failures:
                            if not isinstance(failure, dict):
                                continue
                            stage = failure.get("stage")
                            error_type = failure.get("error_type")
                            if (
                                isinstance(stage, str)
                                and stage
                                and stage not in failure_stages
                            ):
                                failure_stages.append(stage)
                            if (
                                isinstance(error_type, str)
                                and error_type
                                and error_type not in failure_types
                            ):
                                failure_types.append(error_type)
                            result_unknown = (
                                result_unknown
                                or failure.get("result_unknown") is True
                            )
                if status == "pending":
                    recovery_kind = "memory_pending_checkpoint"
                    recovery_ready = True
                    recovery_block_reason = None
                elif status == "running":
                    recovery_kind = "memory_worker_running"
                    recovery_ready = False
                    recovery_block_reason = "existing_worker_running"
                elif status == "failed" and retryable:
                    recovery_kind = "memory_retry_safe"
                    recovery_ready = True
                    recovery_block_reason = None
                else:
                    recovery_kind = "memory_manual_review"
                    recovery_ready = False
                    recovery_block_reason = (
                        "unknown_side_effects"
                        if manual_recovery_required or result_unknown
                        else "checkpoint_not_strictly_retryable"
                    )
                raise MemoryEnrichmentNotReadyError(
                    chapter_id=chapter_id,
                    status=status,
                    reason=(
                        "等待现有富记忆 Worker 完成"
                        if status in {"pending", "running"}
                        else "富记忆未成功，需先处理失败原因"
                    ),
                    recovery_details={
                        "recovery_kind": recovery_kind,
                        "recovery_ready": recovery_ready,
                        "recovery_block_reason": recovery_block_reason,
                        "failure_stages": failure_stages,
                        "failure_types": failure_types,
                        "result_unknown": result_unknown,
                    },
                )
            details = row.memory_enrichment_details
            assert isinstance(details, dict)
            final_version_id = row.current_version
            ready_at = row.ready_for_next_chapter_at
            if (
                type(final_version_id) is not int
                or final_version_id < 1
                or ready_at is None
            ):
                raise ContinuityCheckpointCorruptionError(
                    chapter_id=chapter_id,
                    reason="富记忆 succeeded 但终稿版本或 ready 水位缺失",
                )
            if details.get("final_version_id") != final_version_id:
                raise ContinuityCheckpointCorruptionError(
                    chapter_id=chapter_id,
                    reason="富记忆 succeeded 来源版本不是当前权威正文",
                    evidence=[
                        f"details.final_version_id={details.get('final_version_id')!r}",
                        f"current_version={final_version_id}",
                    ],
                )
            frozen_ready_at = datetime.fromisoformat(str(details["ready_at"]))
            if self._as_utc(frozen_ready_at) != self._as_utc(ready_at):
                raise ContinuityCheckpointCorruptionError(
                    chapter_id=chapter_id,
                    reason="富记忆 succeeded 的 ready 来源与章节水位不一致",
                )
        finally:
            session.close()

        paths = self._paths(workspace_id)
        expected_final_path = str(
            paths.chapter_version_file(chapter_id, "final.md")
        )
        if details.get("final_path") != expected_final_path:
            raise ContinuityCheckpointCorruptionError(
                chapter_id=chapter_id,
                reason="富记忆 succeeded 的终稿路径不是当前权威路径",
            )
        ledger_hash, source_text_hash = (
            self._validated_memory_enrichment_source_checkpoint(
                workspace_id=workspace_id,
                chapter_id=chapter_id,
                final_version_id=final_version_id,
                paths=paths,
            )
        )
        if (
            details.get("continuity_ledger_hash") != ledger_hash
            or details.get("continuity_ledger_source_text_hash")
            != source_text_hash
        ):
            raise ContinuityCheckpointCorruptionError(
                chapter_id=chapter_id,
                reason="富记忆 succeeded 的连续性来源已漂移",
            )

    @staticmethod
    def _validate_memory_enrichment_provenance(
        *,
        row: ChapterORM,
        paths: WorkspacePaths,
        expected_final_version_id: int,
        expected_ledger_hash: str,
        expected_ledger_source_text_hash: str,
    ) -> None:
        """校验 pending/running 状态及 ready 冻结来源，不洗掉坏 checkpoint。"""
        validate_memory_enrichment_checkpoint_shape(
            status=row.memory_enrichment_status,
            details=row.memory_enrichment_details,
            enriched_at=row.memory_enriched_at,
        )
        if (
            row.memory_enrichment_status != "pending"
            or row.ready_for_next_chapter_at is None
        ):
            return
        details = row.memory_enrichment_details
        if not isinstance(details, dict):
            raise MemoryEnrichmentCheckpointShapeError(
                "ready+pending 必须保留结构化来源"
            )
        expected_final_path = str(
            paths.chapter_version_file(row.chapter_id, "final.md")
        )
        if details.get("final_version_id") != expected_final_version_id:
            raise MemoryEnrichmentCheckpointShapeError(
                "pending.final_version_id 与章节权威版本不一致"
            )
        if details.get("final_path") != expected_final_path:
            raise MemoryEnrichmentCheckpointShapeError(
                "pending.final_path 与工作区权威终稿路径不一致"
            )
        ready_at_text = details.get("ready_at")
        if not isinstance(ready_at_text, str):
            raise MemoryEnrichmentCheckpointShapeError(
                "pending.ready_at 必须是 ISO-8601 字符串"
            )
        try:
            frozen_ready_at = datetime.fromisoformat(ready_at_text)
        except ValueError as exc:
            raise MemoryEnrichmentCheckpointShapeError(
                "pending.ready_at 必须是合法 ISO-8601 时间"
            ) from exc
        if (
            frozen_ready_at.tzinfo is None
            or frozen_ready_at.utcoffset() is None
            or row.ready_for_next_chapter_at is None
            or ChapterService._as_utc(frozen_ready_at)
            != ChapterService._as_utc(row.ready_for_next_chapter_at)
        ):
            raise MemoryEnrichmentCheckpointShapeError(
                "pending.ready_at 与章节 ready_for_next_chapter_at 不一致"
            )
        for field in (
            "continuity_ledger_hash",
            "continuity_ledger_source_text_hash",
        ):
            value = details.get(field)
            if (
                not isinstance(value, str)
                or re.fullmatch(r"[0-9a-f]{64}", value) is None
            ):
                raise MemoryEnrichmentCheckpointShapeError(
                    f"pending.{field} 必须是 64 位 SHA-256"
                )
        if details["continuity_ledger_hash"] != expected_ledger_hash:
            raise MemoryEnrichmentCheckpointShapeError(
                "pending.continuity_ledger_hash 与权威小账本不一致"
            )
        if (
            details["continuity_ledger_source_text_hash"]
            != expected_ledger_source_text_hash
        ):
            raise MemoryEnrichmentCheckpointShapeError(
                "pending.continuity_ledger_source_text_hash 与权威小账本不一致"
            )

    def _validated_memory_enrichment_source_checkpoint(
        self,
        *,
        workspace_id: str,
        chapter_id: str,
        final_version_id: int,
        paths: WorkspacePaths,
    ) -> tuple[str, str]:
        """逐字节复核 DB/vN/final 与连续性小账本，返回两项权威 hash。"""
        if self._session_factory is None:
            return "", ""
        session = self._open_session(workspace_id)
        if session is None:
            raise ContinuityCheckpointCorruptionError(
                chapter_id=chapter_id,
                reason="富记忆源 checkpoint 无法打开 DB session",
            )
        try:
            version_row = session.get(
                ChapterVersionORM,
                (chapter_id, final_version_id),
            )
            if version_row is None:
                raise ContinuityCheckpointCorruptionError(
                    chapter_id=chapter_id,
                    reason=f"富记忆源版本缺失: v{final_version_id}",
                )
            authoritative_content = version_row.content
        finally:
            session.close()
        self._validated_checkpoint_body_snapshot(
            workspace_id=workspace_id,
            paths=paths,
            chapter_id=chapter_id,
            source_version_id=final_version_id,
            authoritative_content=authoritative_content,
        )
        try:
            ledger = load_validated_continuity_ledger(
                workspace_id=workspace_id,
                chapter_id=chapter_id,
                paths=paths,
                session_factory=self._session_factory,
                expected_version_id=final_version_id,
            )
        except ContinuityLedgerValidationError as exc:
            raise ContinuityCheckpointCorruptionError(
                chapter_id=chapter_id,
                reason=str(exc),
            ) from exc
        return ledger.ledger_hash, ledger.source_text_hash

    def _acquire_memory_enrichment_worker(
        self,
        workspace_id: str,
        chapter_id: str,
        *,
        final_version_id: int,
        started_at: datetime,
        paths: WorkspacePaths,
        expected_ledger_hash: str,
        expected_ledger_source_text_hash: str,
    ) -> _MemoryEnrichmentLease | None:
        """原子抢占 ``pending -> running``，失败时绝不进入富记忆 Provider。

        调度器可能因重复回调、进程恢复或调用方 bug 把同一闭包执行多次。这里用
        DB 条件更新把章节版本、ready 水位和 pending 状态绑定为唯一 Worker
        租约；只有真正改到一行的执行者能继续。无 DB 的旧单测/CLI 兼容路径
        没有生产 Provider wiring，保留原有 inline 行为。
        """
        lease_id = uuid.uuid4().hex
        if self._session_factory is None:
            return _MemoryEnrichmentLease(
                final_version_id=final_version_id,
                ready_at=None,
                running_details={
                    "started_at": started_at.isoformat(),
                    "tasks": [],
                },
            )
        try:
            session = self._open_session(workspace_id)
        except Exception:  # noqa: BLE001 — 抢占失败必须 fail-closed
            _log.exception(
                "memory_enrichment_worker_acquire_failed",
                workspace_id=workspace_id,
                chapter_id=chapter_id,
                final_version_id=final_version_id,
                reason="session_open_failed",
            )
            return None
        if session is None:
            _log.error(
                "memory_enrichment_worker_acquire_failed",
                workspace_id=workspace_id,
                chapter_id=chapter_id,
                final_version_id=final_version_id,
                reason="session_unavailable",
            )
            return None
        try:
            row = session.get(ChapterORM, chapter_id)
            if (
                row is None
                or row.workspace_id != workspace_id
                or row.current_version != final_version_id
                or row.status
                not in (ChapterState.APPROVED.value, ChapterState.PUBLISHED.value)
                or row.ready_for_next_chapter_at is None
            ):
                raise MemoryEnrichmentCheckpointShapeError(
                    "富记忆 Worker 的章节/version/terminal/ready checkpoint 不匹配"
                )
            self._validate_memory_enrichment_provenance(
                row=row,
                paths=paths,
                expected_final_version_id=final_version_id,
                expected_ledger_hash=expected_ledger_hash,
                expected_ledger_source_text_hash=(expected_ledger_source_text_hash),
            )
            if row.memory_enrichment_status != "pending":
                raise MemoryEnrichmentCheckpointShapeError(
                    "富记忆 Worker 只能从 pending 抢占"
                )
            original_details = row.memory_enrichment_details
            assert isinstance(original_details, dict)
            ready_at = row.ready_for_next_chapter_at
            chapter_status = row.status
            side_effect_baseline: dict[str, Any] | None = None
            if self._vector_store_factory is not None:
                sqlite_snapshot = audit_sqlite_memory_snapshot(
                    session=session,
                    workspace_id=workspace_id,
                    row=row,
                )
                vector_store = self._vector_store_factory(workspace_id)
                vector_snapshot = vector_store.snapshot_by_chapter_id(
                    DEFAULT_COLLECTION,
                    chapter_id=chapter_id,
                )
                side_effect_baseline = {
                    "schema_version": 1,
                    "lease_id": lease_id,
                    "sqlite_footprint": sqlite_snapshot.footprint,
                    "sqlite_digest": sqlite_snapshot.digest,
                    "vector_count": vector_snapshot.count,
                    "vector_digest": vector_snapshot.digest,
                }
            running_details = {
                **original_details,
                "started_at": started_at.isoformat(),
                "tasks": [],
                "lease_id": lease_id,
            }
            if side_effect_baseline is not None:
                running_details["side_effect_baseline"] = side_effect_baseline
            result = session.execute(
                update(ChapterORM)
                .where(ChapterORM.chapter_id == chapter_id)
                .where(ChapterORM.workspace_id == workspace_id)
                .where(ChapterORM.current_version == final_version_id)
                .where(ChapterORM.status == chapter_status)
                .where(ChapterORM.ready_for_next_chapter_at == ready_at)
                .where(ChapterORM.memory_enrichment_status == "pending")
                .where(ChapterORM.memory_enriched_at.is_(None))
                .where(ChapterORM.memory_enrichment_details == original_details)
                .values(
                    memory_enrichment_status="running",
                    memory_enriched_at=None,
                    memory_enrichment_details=running_details,
                )
            )
            if result.rowcount != 1:
                session.rollback()
                _log.error(
                    "memory_enrichment_worker_acquire_rejected",
                    workspace_id=workspace_id,
                    chapter_id=chapter_id,
                    final_version_id=final_version_id,
                    affected_rows=result.rowcount,
                )
                return None
            session.commit()
            return _MemoryEnrichmentLease(
                final_version_id=final_version_id,
                ready_at=ready_at,
                running_details=running_details,
            )
        except Exception as exc:  # noqa: BLE001 — 抢占失败必须 fail-closed
            session.rollback()
            _log.exception(
                "memory_enrichment_worker_acquire_failed",
                workspace_id=workspace_id,
                chapter_id=chapter_id,
                final_version_id=final_version_id,
                reason="conditional_update_failed",
                error_type=type(exc).__name__,
            )
            return None
        finally:
            session.close()

    def _finish_memory_enrichment_worker_safely(
        self,
        workspace_id: str,
        chapter_id: str,
        *,
        lease: _MemoryEnrichmentLease,
        status: Literal["succeeded", "failed"],
        details: dict[str, Any],
        enriched_at: datetime | None,
    ) -> bool:
        """仅允许仍持有同版本 ``running`` 租约的 Worker 落终态。"""
        session = self._open_session(workspace_id)
        if session is None:
            return False
        try:
            result = session.execute(
                update(ChapterORM)
                .where(ChapterORM.chapter_id == chapter_id)
                .where(ChapterORM.workspace_id == workspace_id)
                .where(ChapterORM.current_version == lease.final_version_id)
                .where(
                    ChapterORM.status.in_(
                        (
                            ChapterState.APPROVED.value,
                            ChapterState.PUBLISHED.value,
                        )
                    )
                )
                .where(ChapterORM.ready_for_next_chapter_at == lease.ready_at)
                .where(ChapterORM.memory_enrichment_status == "running")
                .where(ChapterORM.memory_enrichment_details == lease.running_details)
                .values(
                    memory_enrichment_status=status,
                    memory_enriched_at=enriched_at,
                    memory_enrichment_details=details,
                )
            )
            if result.rowcount != 1:
                session.rollback()
                _log.error(
                    "memory_enrichment_worker_finish_rejected",
                    workspace_id=workspace_id,
                    chapter_id=chapter_id,
                    final_version_id=lease.final_version_id,
                    status=status,
                    affected_rows=result.rowcount,
                )
                return False
            session.commit()
            return True
        except Exception:  # noqa: BLE001 — 已 approved 的富加工终态仍保持 fail-open
            session.rollback()
            _log.warning(
                "memory_enrichment_worker_finish_failed",
                workspace_id=workspace_id,
                chapter_id=chapter_id,
                final_version_id=lease.final_version_id,
                status=status,
            )
            return False
        finally:
            session.close()

    def _read_memory_enrichment_status(
        self, workspace_id: str, chapter_id: str
    ) -> tuple[str | None, dict[str, Any] | None]:
        """读取富记忆状态;用于下一章降级日志。"""
        session = self._open_session(workspace_id)
        if session is None:
            return None, None
        try:
            row = session.get(ChapterORM, chapter_id)
            if row is None or row.workspace_id != workspace_id:
                return None, None
            return row.memory_enrichment_status, row.memory_enrichment_details
        except Exception:  # noqa: BLE001 — 高频辅助读,失败只降级为未知
            _log.warning(
                "memory_enrichment_status_read_failed",
                workspace_id=workspace_id,
                chapter_id=chapter_id,
            )
            return None, None
        finally:
            session.close()

    @staticmethod
    def _fs_derive_state(*, has_final: bool, version_count: int) -> ChapterStateValue:
        """文件系统信号推断 7 档 state(无 chapters 行时的 fallback)。

        映射:
        - 有 ``final.md`` → ``approved``
        - 无版本 / 有 ``v*.md`` 但无 ``final.md`` → ``drafting``
          (状态机起点;P1 ``not_started`` / ``unknown`` 两档统一折算)

        前端可通过 ``version_count == 0`` 区分"未开工",通过 ``has_final``
        区分"已通过审稿"——本字段只反映状态机层面的当前节点。
        """
        if has_final:
            return "approved"
        return "drafting"

    @staticmethod
    def _card_path(paths: WorkspacePaths, chapter_id: str) -> Path:
        return paths.cards_dir / f"{chapter_id}.json"

    def card_path_for(self, workspace_id: str, chapter_id: str) -> Path:
        """章节卡文件位置(``data/cards/<chapter_id>.json``)。

        路由层 ``:write`` / ``:rewrite`` / ``:revise`` / ``GET /card`` 的
        ``ChapterCardNotFound`` 前置存在性守卫用;不读不写、不校验工作区,
        只算路径(类比 P1-B-18 ``WorkspaceService.archive_file_for``)。

        工作区不存在时返回的路径在 ``is_file()`` 检查里自然为 ``False``,
        守卫抛 404,无需在此先校验工作区——避免重复校验。
        """
        return self._card_path(self._paths(workspace_id), chapter_id)

    # ---- 章节卡 IO ---------------------------------------------------------

    def read_chapter_card(self, workspace_id: str, chapter_id: str) -> ChapterCard:
        """从 ``data/cards/<chapter_id>.json`` 读出章节卡。

        - 工作区不存在 → ``WorkspaceServiceError``
        - 章节卡文件不存在 → ``ChapterServiceError``
        - JSON 损坏 / schema 不符 → ``ChapterServiceError``
        """
        paths = self._validate_workspace(workspace_id)
        card_path = self._card_path(paths, chapter_id)
        if not card_path.is_file():
            raise ChapterServiceError(f"章节卡不存在: {card_path}")
        try:
            data = json.loads(card_path.read_text(encoding="utf-8"))
            return ChapterCard.model_validate_storage(data)
        except json.JSONDecodeError as exc:
            raise ChapterServiceError(
                f"章节卡内容不是合法 JSON: {card_path}: {exc}"
            ) from exc
        except ValidationError as exc:
            raise ChapterServiceError(
                f"章节卡字段不符合 ChapterCard schema: {card_path}: {exc}"
            ) from exc

    def save_chapter_card(self, workspace_id: str, card: ChapterCard) -> Path:
        """把章节卡原子写到 ``data/cards/<card.chapter_id>.json``。

        已存在则覆盖(章节卡在 P1 是可编辑的)。``cards_dir`` 不存在则
        自动 mkdir(防御:正常情况下 ``WorkspaceService.create`` 已建好骨架)。

        - 工作区不存在 → ``WorkspaceServiceError``
        """
        paths = self._validate_workspace(workspace_id)
        card_path = self._card_path(paths, card.chapter_id)
        # v1.0.x wave 6 s2.6a:race 防御 — 工作区目录在异步任务跑中途被 rmtree
        # (destroy/purge/archive),此时 mkdir(parents=True) 会重建被删的子目录
        # 残留章节卡 JSON;swallow + log + return card_path(语义上写入失败但
        # caller 不抛错,与 BackgroundTask 入口防御协同形成两层防御)。
        if not paths.root.is_dir():
            _log.warning(
                "workspace_writer_skipped",
                writer="save_chapter_card",
                workspace_id=workspace_id,
                chapter_id=card.chapter_id,
                target_path=str(card_path),
                reason="workspace_destroyed_during_async_task",
            )
            return card_path
        card_path.parent.mkdir(parents=True, exist_ok=True)
        # 原子写:tmp → replace,POSIX rename 保证不留半截文件
        tmp_path = card_path.with_suffix(card_path.suffix + ".tmp")
        tmp_path.write_text(card.model_dump_json(indent=2), encoding="utf-8")
        tmp_path.replace(card_path)
        return card_path

    # ---- 写作动作:write / rewrite ----------------------------------------

    def _wrap_state_transition_with_db(
        self,
        workspace_id: str,
        chapter_id: str,
        on_state_transition: StateTransitionCallback | None,
        card: ChapterCard | None = None,
    ) -> StateTransitionCallback | None:
        """组合 DB 写入 + 用户传入的 ``on_state_transition``(P2-B-17)。

        Orchestrator 每次 ``transition`` 后调本回调:**先**写 ``chapters.status``
        DB(同步),**再**调 caller 传的 ``on_state_transition``(WS 推送等)。
        DB 写失败抛原异常,Orchestrator 主循环冒泡——状态机已落,但 DB 滞后,
        与 P1-B-20 文档"回调实现方应自己 try/except 兜底"一致(本回调不
        try/except,让数据真实失败显式可见,而不是静默)。

        P4-B-03 起:``nxt == APPROVED`` 且 ``card`` 给定时,在 ``_upsert_chapter_state``
        之后调 ``write_per_chapter_safely`` 落 ``metrics_per_chapter`` 行。
        ``write_per_chapter_safely`` 自身 swallow + structlog log,不冒泡,
        不影响主流水线。

        P4-B-04 起:同一 APPROVED 分支在 ``write_per_chapter_safely`` 之后调
        ``aggregate_daily_safely`` 全量重算当日 ``metrics_daily`` 聚合行;聚合
        日期取 ``datetime.now(UTC).date()`` 与 metrics_per_chapter
        ``approved_at`` 落库时刻同源(``approved_at`` 由 ``_upsert_chapter_state``
        在本闭包之前刚刚填,本聚合扫描的 ``approved_at IN [day_start, day_end)``
        条件将命中刚写的 metrics_per_chapter 行)。

        ``session_factory=None`` 时本方法直接返 ``on_state_transition`` 原物
        (无 DB 写,无 metrics 写),P1 兼容路径。
        """
        if self._session_factory is None:
            return on_state_transition

        session_factory_local = self._session_factory  # closure narrow

        def _combined(prev: ChapterState, nxt: ChapterState) -> None:
            self._upsert_chapter_state(
                workspace_id,
                chapter_id,
                state=nxt.value,  # type: ignore[arg-type]
            )
            if nxt == ChapterState.APPROVED and card is not None:
                write_per_chapter_safely(
                    lambda: session_factory_local(workspace_id),
                    workspace_id=workspace_id,
                    chapter_id=chapter_id,
                    card=card,
                )
                aggregate_daily_safely(
                    lambda: session_factory_local(workspace_id),
                    workspace_id=workspace_id,
                    date=datetime.now(timezone.utc).date(),
                )
                # P6-B-07:每章 approved 后同步自动备份(只 db + outline +
                # cards,~50KB);失败 swallow + structlog 不阻塞 approve
                # (对齐 plan §决策 7=A 字面"approve transaction 后立即跑,
                # 失败不阻塞 approve")。
                self._run_auto_backup(workspace_id, chapter_id)
                # v1.0.x wave 6 s2.6d:chapter_summary hook **不在此 state 回调内调** —
                # `_fire(prev, APPROVED)` 触发 `_combined` 的时机**早于**
                # orchestrator 在循环出口调 `versioning.mark_final`(`chapter_orchestrator.py:638`),
                # 在此调 hook 会撞 `chapter_summary_hook_no_final_md` race condition
                # (luo-cloud-test 21 章实测 11 次 pass 9 次撞)。修法:hook 移到
                # `_orchestrate` 函数尾部、`orchestrate_chapter` 返回 OrchestratorResult
                # 之后调,与 `_extract_memories_safely` 在 orchestrator 内 mark_final
                # 之后调的时序对齐。详 docs/impl/v1_0_hotfix_wave6_s2.6d.md。
            if on_state_transition is not None:
                on_state_transition(prev, nxt)

        return _combined

    def _run_auto_backup(self, workspace_id: str, chapter_id: str) -> None:
        """approve transaction 后调 ``BackupService.create_auto_backup``。

        失败 swallow + structlog ``backup_failed`` warn,不阻塞 approve
        (对齐 plan §决策 7=A 字面)。``backup_service=None`` 时静默跳过
        (CLI / 单测 legacy 兼容)。
        """
        if self._backup_service is None:
            return
        # 局部 import 规避 backup_service ↔ chapter_service 顶层循环
        from src.services.backup_service import BackupService

        if not isinstance(self._backup_service, BackupService):
            return
        try:
            self._backup_service.create_auto_backup(workspace_id, chapter_id)
        except Exception as exc:  # noqa: BLE001
            structlog.get_logger(__name__).warning(
                "backup_failed",
                workspace_id=workspace_id,
                chapter_id=chapter_id,
                error_type=type(exc).__name__,
                error_message=str(exc),
            )

    def _extract_chapter_summary_hook(
        self,
        workspace_id: str,
        chapter_id: str,
        *,
        trigger: Literal["reviewer_pass", "force_pass"],
    ) -> None:
        """approve transaction 后调 LLM 抽取章节摘要落 ``ChapterSummaryORM``。

        v1.0.x wave 4 session 3a 引入。**Trigger="reviewer_pass"** 由
        ``_combined`` APPROVED 分支调,**Trigger="force_pass"** 由
        ``ChapterRewindService.force_pass`` 调(独立 caller,不走本方法)。

        全程 fail-open + structlog,失败不阻塞 approve(对齐
        ``_run_auto_backup`` + ``sync_completed_chapters_to_meta`` 等"辅助任务
        失败 swallow"先例)。委托给 ``extract_and_persist_chapter_summary_safely``
        独立模块函数;``session_factory=None`` / LLM 未注入时静默跳过(CLI /
        单测 legacy 兼容)。
        """
        # 局部 import 规避 agents ↔ services 顶层循环
        from src.core.agents.chapter_summary_extractor import (
            extract_and_persist_chapter_summary_safely,
        )

        paths = self._paths(workspace_id)
        # router_factory 优先(对齐 ChapterService _orchestrate 同型),legacy
        # 兜底走 self._client + self._model;extract_and_persist_chapter_summary_safely
        # 内部按"router 给定 → 优先 / 否则 client+model"分发。
        router = (
            self._router_factory(workspace_id)
            if self._router_factory is not None
            else None
        )
        # v1.1 wave 3 s6(E1):构造 vector_store 让摘要同步入 Chroma;工厂未注入
        # (CLI / 单测 legacy)→ None,hook 内部跳过 Chroma 写只落 SQLite。
        vector_store = (
            self._vector_store_factory(workspace_id)
            if self._vector_store_factory is not None
            else None
        )
        extract_and_persist_chapter_summary_safely(
            workspace_id=workspace_id,
            chapter_id=chapter_id,
            trigger=trigger,
            paths=paths,
            session_factory=self._session_factory,
            llm_client=self._client,
            llm_model=self._model,
            router=router,
            vector_store=vector_store,
        )

    def _schedule_memory_enrichment_after_ready(
        self,
        *,
        workspace_id: str,
        chapter_id: str,
        final_version_id: int,
        paths: WorkspacePaths,
        card: ChapterCard,
        router: ModelRouter | None,
        final_path: Path | None,
        session_factory: Callable[[], Session] | None,
        progression_consistency_enabled: bool,
        progression_label: str,
    ) -> None:
        """ready 后调度富记忆；正式 Run 内同步完成，手动写章仍可后台。"""

        def _task() -> None:
            self._run_memory_enrichment_task(
                workspace_id=workspace_id,
                chapter_id=chapter_id,
                final_version_id=final_version_id,
                paths=paths,
                card=card,
                router=router,
                final_path=final_path,
                session_factory=session_factory,
                progression_consistency_enabled=progression_consistency_enabled,
                progression_label=progression_label,
            )

        attempt_scope = current_attempt_scope()
        if (
            attempt_scope is not None
            and attempt_scope.synchronous_memory_enrichment
        ):
            # 正式 Run 的下一章卡会读取本章刚写入的 SQLite/Chroma 记忆。
            # 直接同步消费同一个既有 Worker/CAS/账本任务，不新增调用或 retry。
            _task()
            return
        self._memory_enrichment_scheduler(_task)

    def run_memory_enrichment_for_ready_checkpoint(
        self,
        workspace_id: str,
        chapter_id: str,
        *,
        final_version_id: int,
        progression_consistency_enabled: bool = False,
        progression_label: str = "境界",
    ) -> None:
        """同步消费一个已落 ``pending`` 的 ready 富记忆 checkpoint。

        ``force-pass`` 不经过 orchestrator 的 approved 返回链，但它必须复用
        与普通 reviewer-pass 完全相同的 Worker 租约、来源复核和终态分类。
        本入口只负责从工作区真相源重新装配 card/router/final/session，再委托
        ``_run_memory_enrichment_task``；不另造抽取、retry 或持久化语义。
        """

        paths = self._validate_workspace(workspace_id)
        card = self.read_chapter_card(workspace_id, chapter_id)
        router = (
            self._router_factory(workspace_id)
            if self._router_factory is not None
            else None
        )
        final_path = paths.chapter_version_file(chapter_id, "final.md")
        session_factory = (
            (lambda: self._session_factory(workspace_id))
            if self._session_factory is not None
            else None
        )
        if router is not None and session_factory is not None:
            router = wrap_router_with_cost_tracking(
                router,
                workspace_id=workspace_id,
                chapter_id=chapter_id,
                session_factory=session_factory,
            )
        self._run_memory_enrichment_task(
            workspace_id=workspace_id,
            chapter_id=chapter_id,
            final_version_id=final_version_id,
            paths=paths,
            card=card,
            router=router,
            final_path=final_path,
            session_factory=session_factory,
            progression_consistency_enabled=progression_consistency_enabled,
            progression_label=progression_label,
        )

    def _run_memory_enrichment_task(
        self,
        *,
        workspace_id: str,
        chapter_id: str,
        final_version_id: int,
        paths: WorkspacePaths,
        card: ChapterCard,
        router: ModelRouter | None,
        final_path: Path | None,
        session_factory: Callable[[], Session] | None,
        progression_consistency_enabled: bool,
        progression_label: str,
    ) -> None:
        """串行化同 workspace 富记忆，保证 baseline 可归因。"""

        with serialize_memory_enrichment_worker(
            self._workspace_service.workspaces_root,
            workspace_id,
        ):
            self._run_memory_enrichment_task_locked(
                workspace_id=workspace_id,
                chapter_id=chapter_id,
                final_version_id=final_version_id,
                paths=paths,
                card=card,
                router=router,
                final_path=final_path,
                session_factory=session_factory,
                progression_consistency_enabled=progression_consistency_enabled,
                progression_label=progression_label,
            )

    def _run_memory_enrichment_task_locked(
        self,
        *,
        workspace_id: str,
        chapter_id: str,
        final_version_id: int,
        paths: WorkspacePaths,
        card: ChapterCard,
        router: ModelRouter | None,
        final_path: Path | None,
        session_factory: Callable[[], Session] | None,
        progression_consistency_enabled: bool,
        progression_label: str,
    ) -> None:
        """执行富记忆后台加工,失败落 ``memory_enrichment_status=failed``。"""
        started_at = datetime.now(timezone.utc)
        overall_timer = StageTimer(
            _log,
            "chapter_stage_timing",
            {
                "workspace_id": workspace_id,
                "chapter_id": chapter_id,
                "stage": "memory_enriched",
            },
        )
        overall_timer.__enter__()
        try:
            (
                expected_ledger_hash,
                expected_ledger_source_text_hash,
            ) = self._validated_memory_enrichment_source_checkpoint(
                workspace_id=workspace_id,
                chapter_id=chapter_id,
                final_version_id=final_version_id,
                paths=paths,
            )
        except Exception as exc:  # noqa: BLE001 — 源 checkpoint 未验明前禁止出网
            _log.exception(
                "memory_enrichment_source_checkpoint_invalid",
                workspace_id=workspace_id,
                chapter_id=chapter_id,
                final_version_id=final_version_id,
                error_type=type(exc).__name__,
                error_message=str(exc),
                preserved_pending=True,
            )
            overall_timer.finish(
                status="failed",
                reason="memory_enrichment_source_checkpoint_invalid",
                error_type=type(exc).__name__,
            )
            return
        lease = self._acquire_memory_enrichment_worker(
            workspace_id,
            chapter_id,
            final_version_id=final_version_id,
            started_at=started_at,
            paths=paths,
            expected_ledger_hash=expected_ledger_hash,
            expected_ledger_source_text_hash=(expected_ledger_source_text_hash),
        )
        if lease is None:
            overall_timer.finish(
                status="skipped",
                reason="memory_enrichment_worker_not_acquired",
            )
            return
        tasks: list[dict[str, Any]] = []
        failures: list[dict[str, Any]] = []
        memory_outcome: MemoryExtractionOutcome | None = None
        summary_vector_outcome: dict[str, bool] | None = None
        unknown_side_effects = False

        try:
            # 抢占成功后、任何 vector factory / Provider 之前再验一次。同章
            # Rewrite/Rewind 若在两次检查之间改变正文或 ledger，这里必须让
            # 当前租约失败收口，不能拿旧闭包继续富加工。
            actual_ledger_hash, actual_source_text_hash = (
                self._validated_memory_enrichment_source_checkpoint(
                    workspace_id=workspace_id,
                    chapter_id=chapter_id,
                    final_version_id=final_version_id,
                    paths=paths,
                )
            )
            if (
                actual_ledger_hash != expected_ledger_hash
                or actual_source_text_hash != expected_ledger_source_text_hash
            ):
                raise ContinuityCheckpointCorruptionError(
                    chapter_id=chapter_id,
                    reason=("富记忆 Worker 抢占前后连续性来源发生变化，禁止使用旧闭包"),
                    evidence=[
                        f"expected_ledger_hash={expected_ledger_hash}",
                        f"actual_ledger_hash={actual_ledger_hash}",
                        (
                            "expected_source_text_hash="
                            f"{expected_ledger_source_text_hash}"
                        ),
                        f"actual_source_text_hash={actual_source_text_hash}",
                    ],
                )
            vector_store: VectorStore | None = None
            if self._vector_store_factory is not None:
                try:
                    with StageTimer(
                        _log,
                        "chapter_stage_timing",
                        {
                            "workspace_id": workspace_id,
                            "chapter_id": chapter_id,
                            "stage": "vector_store_factory",
                        },
                    ):
                        vector_store = self._vector_store_factory(workspace_id)
                except Exception as exc:  # noqa: BLE001 — 富记忆失败显性化
                    failures.append(
                        {
                            "stage": "vector_store_factory",
                            "error_type": type(exc).__name__,
                            "message": str(exc),
                        }
                    )
                    tasks.append(
                        {
                            "name": "vector_store_factory",
                            "status": "failed",
                            "error_type": type(exc).__name__,
                        }
                    )
            else:
                log_stage_timing(
                    _log,
                    "chapter_stage_timing",
                    workspace_id=workspace_id,
                    chapter_id=chapter_id,
                    stage="vector_store_factory",
                    reason="factory_missing",
                )

            if (
                session_factory is not None
                and vector_store is not None
                and final_path is not None
            ):
                memory_setting_contract = self._load_world_contract(
                    paths,
                    required=self._outline_contract_required(workspace_id, paths),
                )
                with StageTimer(
                    _log,
                    "chapter_stage_timing",
                    {
                        "workspace_id": workspace_id,
                        "chapter_id": chapter_id,
                        "stage": "memory_extractor",
                    },
                ) as memory_timer:
                    raw_memory_outcome = _extract_memories_safely(
                        session_factory=session_factory,
                        vector_store=vector_store,
                        router=router,
                        client=self._client,
                        chapter_card=card,
                        workspace_id=workspace_id,
                        final_path=final_path,
                        model=self._model,
                        setting_contract=memory_setting_contract,
                        progression_consistency_enabled=progression_consistency_enabled,
                        progression_label=progression_label,
                    )
                    if isinstance(raw_memory_outcome, MemoryExtractionOutcome):
                        memory_outcome = raw_memory_outcome
                    else:
                        # 兼容旧插件/聚焦测试仍返回 bool；生产实现始终返回
                        # MemoryExtractionOutcome，不能凭 bool 猜测未知副作用。
                        memory_ok_legacy = bool(raw_memory_outcome)
                        memory_outcome = MemoryExtractionOutcome(
                            succeeded=memory_ok_legacy,
                            failure_stage=(
                                None if memory_ok_legacy else "legacy_boolean_result"
                            ),
                            vector_write_started=False,
                            sqlite_rollback_succeeded=True,
                            result_unknown=False,
                        )
                    memory_ok = memory_outcome.succeeded
                    unknown_side_effects = (
                        unknown_side_effects
                        or memory_outcome.result_unknown
                        or not memory_outcome.sqlite_rollback_succeeded
                    )
                    if not memory_ok:
                        memory_timer.finish(status="failed")
                assert memory_outcome is not None
                tasks.append(
                    {
                        "name": "memory_extractor",
                        "status": "succeeded" if memory_ok else "failed",
                    }
                )
                if not memory_ok:
                    failures.append(
                        {
                            "stage": "memory_extractor",
                            "error_type": "MemoryExtractionFailed",
                            "message": "memory_extraction_failed 事件见日志",
                            "result_unknown": memory_outcome.result_unknown,
                        }
                    )
                else:
                    _check_plot_thread_payoff_safely(
                        session_factory=session_factory,
                        chapter_card=card,
                        workspace_id=workspace_id,
                        router=router,
                        client=self._client,
                        model=self._model,
                    )
            else:
                tasks.append(
                    {
                        "name": "memory_extractor",
                        "status": "skipped",
                        "reason": "wiring_incomplete",
                    }
                )
                log_stage_timing(
                    _log,
                    "chapter_stage_timing",
                    workspace_id=workspace_id,
                    chapter_id=chapter_id,
                    stage="memory_extractor",
                    reason="wiring_incomplete",
                )

            if unknown_side_effects:
                tasks.append(
                    {
                        "name": "chapter_summary_vector",
                        "status": "skipped",
                        "reason": "prior_unknown_side_effects",
                    }
                )
            else:
                summary_vector_write_started = False

                def _mark_summary_vector_write_started() -> None:
                    nonlocal summary_vector_write_started
                    summary_vector_write_started = True

                with StageTimer(
                    _log,
                    "chapter_stage_timing",
                    {
                        "workspace_id": workspace_id,
                        "chapter_id": chapter_id,
                        "stage": "chapter_summary_vector",
                    },
                ) as summary_timer:
                    if vector_store is not None and self._session_factory is not None:
                        summary_vector_ok = persist_continuity_ledger_vector_safely(
                            workspace_id=workspace_id,
                            chapter_id=chapter_id,
                            paths=paths,
                            session_factory=self._session_factory,
                            vector_store=vector_store,
                            on_vector_write_started=(
                                _mark_summary_vector_write_started
                            ),
                        )
                        summary_vector_outcome = {
                            "succeeded": summary_vector_ok,
                            "vector_write_started": (summary_vector_write_started),
                            "result_unknown": (
                                not summary_vector_ok and summary_vector_write_started
                            ),
                        }
                    else:
                        summary_vector_ok = True
                        summary_timer.finish(
                            status="skipped",
                            reason="vector_or_session_unavailable",
                        )
                    if not summary_vector_ok:
                        summary_timer.finish(status="failed")
                tasks.append(
                    {
                        "name": "chapter_summary_vector",
                        "status": ("succeeded" if summary_vector_ok else "failed"),
                    }
                )
                if not summary_vector_ok:
                    failures.append(
                        {
                            "stage": "chapter_summary_vector",
                            "error_type": "ChapterSummaryVectorFailed",
                            "message": "chapter_summary_vector_* 事件见日志",
                            "result_unknown": summary_vector_write_started,
                        }
                    )
                    unknown_side_effects = (
                        unknown_side_effects or summary_vector_write_started
                    )

            if unknown_side_effects:
                tasks.append(
                    {
                        "name": "metrics_refresh",
                        "status": "skipped",
                        "reason": "prior_unknown_side_effects",
                    }
                )
            elif self._session_factory is not None:
                session_factory_for_refresh = self._session_factory
                with StageTimer(
                    _log,
                    "chapter_stage_timing",
                    {
                        "workspace_id": workspace_id,
                        "chapter_id": chapter_id,
                        "stage": "metrics_refresh",
                    },
                ):
                    write_per_chapter_safely(
                        lambda: session_factory_for_refresh(workspace_id),
                        workspace_id=workspace_id,
                        chapter_id=chapter_id,
                        card=card,
                        refresh_existing=True,
                    )
                    aggregate_daily_safely(
                        lambda: session_factory_for_refresh(workspace_id),
                        workspace_id=workspace_id,
                        date=datetime.now(timezone.utc).date(),
                    )
                tasks.append({"name": "metrics_refresh", "status": "scheduled"})
            else:
                log_stage_timing(
                    _log,
                    "chapter_stage_timing",
                    workspace_id=workspace_id,
                    chapter_id=chapter_id,
                    stage="metrics_refresh",
                    reason="session_factory_missing",
                )

            finished_at = datetime.now(timezone.utc)
            details: dict[str, Any] = {
                **lease.running_details,
                "finished_at": finished_at.isoformat(),
                "tasks": tasks,
            }
            if memory_outcome is not None:
                details["memory_extraction_outcome"] = {
                    "succeeded": memory_outcome.succeeded,
                    "failure_stage": memory_outcome.failure_stage,
                    "vector_write_started": memory_outcome.vector_write_started,
                    "sqlite_rollback_succeeded": (
                        memory_outcome.sqlite_rollback_succeeded
                    ),
                    "result_unknown": memory_outcome.result_unknown,
                }
            if summary_vector_outcome is not None:
                details["chapter_summary_vector_outcome"] = summary_vector_outcome
            if failures:
                details["result_unknown"] = unknown_side_effects
                details["manual_recovery_required"] = unknown_side_effects
                details["safe_to_continue"] = not unknown_side_effects
                details["failure_disposition"] = (
                    "blocked_unknown_side_effects"
                    if unknown_side_effects
                    else "known_failure_degraded"
                )
                details["failures"] = failures
                committed = self._finish_memory_enrichment_worker_safely(
                    workspace_id,
                    chapter_id,
                    lease=lease,
                    status="failed",
                    details=details,
                    enriched_at=None,
                )
                if not committed:
                    _log.error(
                        "memory_enrichment_result_not_committed",
                        workspace_id=workspace_id,
                        chapter_id=chapter_id,
                        final_version_id=final_version_id,
                        intended_status="failed",
                    )
                _log.warning(
                    "memory_enrichment_failed",
                    workspace_id=workspace_id,
                    chapter_id=chapter_id,
                    failures=failures,
                )
                overall_timer.finish(status="failed", failures_count=len(failures))
                return
            committed = self._finish_memory_enrichment_worker_safely(
                workspace_id,
                chapter_id,
                lease=lease,
                status="succeeded",
                details=details,
                enriched_at=finished_at,
            )
            if not committed:
                _log.error(
                    "memory_enrichment_result_not_committed",
                    workspace_id=workspace_id,
                    chapter_id=chapter_id,
                    final_version_id=final_version_id,
                    intended_status="succeeded",
                )
                overall_timer.finish(
                    status="failed",
                    reason="memory_enrichment_finish_rejected",
                )
                return
            _log.info(
                "memory_enrichment_succeeded",
                workspace_id=workspace_id,
                chapter_id=chapter_id,
            )
            overall_timer.finish(status="succeeded")
        except Exception as exc:  # noqa: BLE001 — 后台任务失败必须显性落库
            details = {
                **lease.running_details,
                "finished_at": datetime.now(timezone.utc).isoformat(),
                "tasks": tasks,
                "result_unknown": unknown_side_effects,
                "manual_recovery_required": unknown_side_effects,
                "safe_to_continue": not unknown_side_effects,
                "failure_disposition": (
                    "blocked_unknown_side_effects"
                    if unknown_side_effects
                    else "known_failure_degraded"
                ),
                "failures": [
                    *failures,
                    {
                        "stage": "memory_enrichment_task",
                        "error_type": type(exc).__name__,
                        "message": str(exc),
                        "result_unknown": unknown_side_effects,
                    },
                ],
            }
            if memory_outcome is not None:
                details["memory_extraction_outcome"] = {
                    "succeeded": memory_outcome.succeeded,
                    "failure_stage": memory_outcome.failure_stage,
                    "vector_write_started": memory_outcome.vector_write_started,
                    "sqlite_rollback_succeeded": (
                        memory_outcome.sqlite_rollback_succeeded
                    ),
                    "result_unknown": memory_outcome.result_unknown,
                }
            if summary_vector_outcome is not None:
                details["chapter_summary_vector_outcome"] = summary_vector_outcome
            committed = self._finish_memory_enrichment_worker_safely(
                workspace_id,
                chapter_id,
                lease=lease,
                status="failed",
                details=details,
                enriched_at=None,
            )
            if not committed:
                _log.error(
                    "memory_enrichment_result_not_committed",
                    workspace_id=workspace_id,
                    chapter_id=chapter_id,
                    final_version_id=final_version_id,
                    intended_status="failed",
                )
            _log.exception(
                "memory_enrichment_task_failed",
                workspace_id=workspace_id,
                chapter_id=chapter_id,
                error_type=type(exc).__name__,
                error_message=str(exc),
            )
            overall_timer.finish(
                status="failed",
                error_type=type(exc).__name__,
                error_message=str(exc),
            )

    def write(
        self,
        workspace_id: str,
        chapter_id: str,
        *,
        on_state_transition: StateTransitionCallback | None = None,
        profile_override: str | None = None,
        skip_must_feasibility_precheck: bool = False,
        attempt_task_id: str | None = None,
    ) -> OrchestratorResult:
        """跑一次完整章节闭环(write → review → revise/rewrite → ... → pass/升人工)。

        W3.5-R5 起先从 ``chapter_versions`` 读取真实 revise/rewrite 预算。已有
        版本但尚未终态时通常从 ``REVIEWING`` 复用最新审稿；只有被严格识别为
        上线前 GLOBAL no-op 的 checkpoint 才从 ``REWRITING`` 自动恢复，且该
        重复版本不占 content rewrite 预算。版本账本为空才从 ``DRAFTING``
        生成新稿，因此进程重启不会赠送免费 draft。

        :param on_state_transition: P1-B-20 加。每次 Orchestrator 内 ``transition``
            后回调;``None`` 表示不推送。本服务不 import WebSocket manager,
            实际推送语义由调用方(``api.routes.chapters`` 闭包)实现。
        :param profile_override: P3-B-11 加(P3-F-03 类 C 顺延)。单次写作
            ``profile_id`` 覆盖,``None`` 时走原 routing 规则。``router_factory``
            未注入时本参数被忽略(legacy 路径无 router 可覆盖);非法
            ``profile_id`` 抛 ``ProfileNotFoundError``。**不持久化**——单次
            生效,与 P3-plan §F-2 字面对齐。

        P1-B-24 起:本方法在入口 ``bind_chapter_context``,``trace_id`` 用
        ``make_trace_id(chapter_id, "write", 0)``——write 总是从 0 起跑,
        与 OBSERVABILITY §2.3 ``v1c012-rewrite0`` 同形态。

        - 工作区不存在 → ``WorkspaceServiceError``
        - 章节卡不存在 / 损坏 → ``ChapterServiceError``
        - ``profile_override`` 引用未声明 profile → ``ProfileNotFoundError``
        """
        existing_scope = current_attempt_scope()
        scope_cm = (
            nullcontext(existing_scope)
            if existing_scope is not None
            else bind_attempt_scope(
                **self._resolve_manual_attempt_scope(
                    workspace_id,
                    chapter_id,
                    task_id=attempt_task_id,
                )
            )
        )
        with scope_cm:
            self._run_gates(workspace_id, chapter_id=chapter_id)
            budget = self._read_authoritative_budget(workspace_id, chapter_id)
            rewind_requires_rewrite = self._rewind_requires_content_rewrite(
                workspace_id,
                chapter_id,
                current_version=budget.current_version,
            )
            cancelled_review_replay_task_id = (
                None
                if rewind_requires_rewrite
                else self._cancelled_review_replay_task_id(
                    workspace_id,
                    chapter_id,
                    current_version=budget.current_version,
                )
            )
            format_retry = (
                None if rewind_requires_rewrite else self._known_invalid_local_review_retry(
                    workspace_id, chapter_id, current_version=budget.current_version,
                )
            )
            response_recovery = (
                None if rewind_requires_rewrite else self._known_empty_must_recovery(
                    workspace_id, chapter_id, current_version=budget.current_version,
                )
            )
            if rewind_requires_rewrite:
                _log.info(
                    "rewind_rewrite_intent_applied",
                    workspace_id=workspace_id,
                    chapter_id=chapter_id,
                    source_version_id=budget.current_version,
                    existing_rewrite_count=budget.rewrite_count,
                )
            legacy_no_progress_recovery = (
                budget.current_version > 0
                and self._is_legacy_no_progress_rewrite_checkpoint(
                    workspace_id,
                    chapter_id,
                )
                and budget.rewrite_count
                < self.read_chapter_card(workspace_id, chapter_id).max_rewrites
            )
            initial_state = (
                ChapterState.REWRITING
                if legacy_no_progress_recovery or rewind_requires_rewrite
                else (
                    ChapterState.REVIEWING
                    if budget.current_version > 0
                    else ChapterState.DRAFTING
                )
            )
            resume_checkpoint_context: RewriteCheckpointContext | None = None
            if self._session_factory is not None:
                paths = self._validate_workspace(workspace_id)
                try:
                    disk_latest_version = (
                        ChapterVersioning(paths).next_version_id(chapter_id) - 1
                    )
                except (OSError, ChapterVersioningError) as exc:
                    raise RewriteCheckpointRecoveryError(
                        "章节恢复的正文 checkpoint 目录无法验真，已阻止请求: "
                        f"{chapter_id}: {exc}"
                    ) from exc
                if disk_latest_version != budget.current_version:
                    raise RewriteCheckpointRecoveryError(
                        "章节恢复的磁盘/DB 最新版本不一致，已阻止请求: "
                        f"{chapter_id}/disk-v{disk_latest_version}/"
                        f"db-v{budget.current_version}"
                    )
                if budget.current_version > 0:
                    session_factory_local = self._session_factory

                    def resume_session_factory() -> Session:
                        return session_factory_local(workspace_id)

                    resume_checkpoint_context = load_rewrite_checkpoint_context(
                        resume_session_factory,
                        paths=paths,
                        chapter_id=chapter_id,
                        version_id=budget.current_version,
                        allow_stale_chapter_projection=True,
                        allow_unpersisted_reviewer_task_id=(
                            cancelled_review_replay_task_id
                            or (response_recovery.source_task_id if response_recovery else None)
                            or (format_retry[0] if format_retry else None)
                        ),
                    )
                    if format_retry is not None:
                        resume_checkpoint_context = resume_checkpoint_context.model_copy(
                            update={"previous_reviewer_parse_errors": [format_retry[1]]}
                        )
                    if rewind_requires_rewrite:
                        # rewind 保留旧正文/风格作为 source checkpoint，但 Card
                        # Review 可能已经生成并批准了新合同。旧 ReviewResult 的
                        # constraint_id 只属于旧卡，必须从本次重写输入中撤销；当前
                        # 卡及其重新编译的 ExecutableConstraintPlan 才是新正文权威。
                        resume_checkpoint_context = (
                            resume_checkpoint_context.model_copy(
                                update={
                                    "review": None,
                                    "review_result": None,
                                    "review_contract_rebase_reason": (
                                        REWIND_REWRITE_REQUIRED_REASON
                                    ),
                                }
                            )
                        )
                    if (
                        resume_checkpoint_context.review is not None
                        and resume_checkpoint_context.review.verdict == "parse_failed"
                    ):
                        raise RewriteCheckpointRecoveryError(
                            "当前版本已有 Reviewer parse_failed checkpoint；"
                            "请通过产品“重写本章”按现有失败记录恢复，禁止盲目重审: "
                            f"{chapter_id}/v{budget.current_version}"
                        )
            with bind_chapter_context(
                workspace_id=workspace_id,
                chapter_id=chapter_id,
                trace_id=make_trace_id(
                    chapter_id,
                    "write",
                    budget.rewrite_count,
                ),
            ):
                return self._orchestrate(
                    workspace_id=workspace_id,
                    chapter_id=chapter_id,
                    initial_state=initial_state,
                    initial_revise_count=budget.revise_count,
                    initial_rewrite_count=(
                        budget.rewrite_count + 1
                        if legacy_no_progress_recovery or rewind_requires_rewrite
                        else budget.rewrite_count
                    ),
                    on_state_transition=on_state_transition,
                    profile_override=profile_override,
                    skip_must_feasibility_precheck=(skip_must_feasibility_precheck),
                    initial_last_version_id=(budget.current_version or None),
                    rewrite_checkpoint_context=resume_checkpoint_context,
                    review_response_recovery=response_recovery,
                    operation_budget=budget,
                    operation_name="write",
                    defer_operation_release=(
                        attempt_task_id is not None
                        or (
                            existing_scope is not None
                            and existing_scope.run_id is not None
                        )
                    ),
                )

    def rewrite(
        self,
        workspace_id: str,
        chapter_id: str,
        *,
        prior_rewrite_count: int = 0,
        on_state_transition: StateTransitionCallback | None = None,
        profile_override: str | None = None,
        attempt_task_id: str | None = None,
    ) -> OrchestratorResult:
        """强制以"重写"方式再跑一次完整章节闭环。

        Orchestrator 从 ``REWRITING`` 起跑:直接调 Writer 写新版本(版本号
        自动递增,旧版本保留),然后进入 ``REVIEWING`` → ... → pass/升人工
        的标准循环。

        ``prior_rewrite_count`` 仅保留请求兼容；R5 起完全忽略调用方值，预算
        由 ``chapter_versions.creation_type`` 权威派生。

        - 工作区不存在 → ``WorkspaceServiceError``
        - 章节卡不存在 / 损坏 → ``ChapterServiceError``
        - 版本账本中的 rewrite 次数已达 ``ChapterCard.max_rewrites`` →
          Orchestrator 第一次审稿后按预算耗尽逻辑升人工
          (``escalation_reason="rewrite_budget_exhausted"``);本方法不预先
          校验,把判定权完整留给 Orchestrator/状态机。

        :param on_state_transition: 同 ``write``。
        :param profile_override: 同 ``write``(P3-B-11 加)。

        P1-B-24 起:本方法在入口 ``bind_chapter_context``,``trace_id`` 用
        ``make_trace_id(chapter_id, "rewrite", rewrite_count)``——
        本次重写序号来自版本账本(0-based,与
        OBSERVABILITY §2.3 例 ``v1c012-rewrite0`` / ``rewrite1`` 一致)。
        """
        del prior_rewrite_count
        existing_scope = current_attempt_scope()
        if existing_scope is None:
            scope_cm = bind_attempt_scope(
                **self._resolve_manual_attempt_scope(
                    workspace_id,
                    chapter_id,
                    task_id=attempt_task_id,
                )
            )
        else:
            scope_cm = nullcontext(existing_scope)
        with scope_cm:
            self._run_gates(workspace_id, chapter_id=chapter_id)
            budget = self._read_authoritative_budget(workspace_id, chapter_id)
            rewrite_checkpoint_context: RewriteCheckpointContext | None = None
            if self._session_factory is not None:
                paths = self._validate_workspace(workspace_id)
                try:
                    disk_latest_version = (
                        ChapterVersioning(paths).next_version_id(chapter_id) - 1
                    )
                except (OSError, ChapterVersioningError) as exc:
                    raise RewriteCheckpointRecoveryError(
                        "显式 Rewrite 的正文 checkpoint 目录无法验真，已阻止请求: "
                        f"{chapter_id}: {exc}"
                    ) from exc
                if disk_latest_version != budget.current_version:
                    raise RewriteCheckpointRecoveryError(
                        "显式 Rewrite 的磁盘/DB 最新版本不一致，已阻止请求: "
                        f"{chapter_id}/disk-v{disk_latest_version}/"
                        f"db-v{budget.current_version}"
                    )
                if budget.current_version > 0:
                    session_factory_local = self._session_factory

                    def rewrite_session_factory() -> Session:
                        return session_factory_local(workspace_id)

                    rewrite_checkpoint_context = load_rewrite_checkpoint_context(
                        rewrite_session_factory,
                        paths=paths,
                        chapter_id=chapter_id,
                        version_id=budget.current_version,
                        allow_stale_chapter_projection=True,
                    )
            with bind_chapter_context(
                workspace_id=workspace_id,
                chapter_id=chapter_id,
                trace_id=make_trace_id(
                    chapter_id,
                    "rewrite",
                    budget.rewrite_count,
                ),
            ):
                return self._orchestrate(
                    workspace_id=workspace_id,
                    chapter_id=chapter_id,
                    initial_state=ChapterState.REWRITING,
                    initial_revise_count=budget.revise_count,
                    # REWRITING 入口即将创建一份 rewrite 版本；先把本次
                    # content rewrite 计入状态机，成功落库后的版本表计数
                    # 与 service response 才能保持一致。若 provider 在正文
                    # 持久化前失败，下一次仍从未增长的版本账本重算。
                    initial_rewrite_count=budget.rewrite_count + 1,
                    on_state_transition=on_state_transition,
                    profile_override=profile_override,
                    initial_last_version_id=(budget.current_version or None),
                    rewrite_checkpoint_context=rewrite_checkpoint_context,
                    operation_budget=budget,
                    operation_name="rewrite",
                    defer_operation_release=(
                        attempt_task_id is not None
                        or (
                            existing_scope is not None
                            and existing_scope.run_id is not None
                        )
                    ),
                )

    def re_review(
        self,
        workspace_id: str,
        chapter_id: str,
        *,
        on_state_transition: StateTransitionCallback | None = None,
        profile_override: str | None = None,
        attempt_task_id: str | None = None,
    ) -> OrchestratorResult:
        """v2 W3.5 UI 缺口3:对人工改稿(human_intervention 章)重跑写后审校。

        孤儿章人工介入后,维护人手改正文(``PUT /chapters/{id}/content`` 落新
        版本)→ 调本方法对**最新版本**单次重审:Reviewer + 写后 LLM 闸
        (must_achieve / continuity / forbidden_crossing / setting / dedup /
        ending)+ validator pipeline 全跑一遍。

        - **通过**(verdict=pass)→ 走 Orchestrator 正常 APPROVED 终态:
          mark_final + 摘要 + 记忆抽取(与 reviewer 自然通过路径同一套副作用)。
        - **不通过**→ 不自动改写(人工已动手),回 ``human_intervention`` +
          ``escalation_reason="re_review_failed"`` + 详情(审校维度 + 反馈),
          供 UI 列出让维护人再改 / 改用「强制通过」逃生口。

        与 ``:force-pass``(跳过审校直接批准)互补:本方法是"改完想再过一遍质量
        闸"的路径。``single_review_pass=True`` 让 Orchestrator 入 REVIEWING 单
        次审、非 pass 即升人工(不进重写循环)。

        - 工作区不存在 → ``WorkspaceServiceError``
        - 章节状态非 ``human_intervention`` → ``ChapterReReviewInvalidStateError``
        - 该章无任何版本 → ``ChapterReReviewNoVersionError``
        """
        existing_scope = current_attempt_scope()
        scope_cm = (
            nullcontext(existing_scope)
            if existing_scope is not None
            else bind_attempt_scope(
                **self._resolve_manual_attempt_scope(
                    workspace_id,
                    chapter_id,
                    task_id=attempt_task_id,
                )
            )
        )
        with scope_cm:
            paths = self._validate_workspace(workspace_id)
            # 状态闸:只在孤儿章(human_intervention)允许重审。
            current_state = self._read_chapter_state(workspace_id, chapter_id)
            if current_state != ChapterState.HUMAN_INTERVENTION.value:
                raise ChapterReReviewInvalidStateError(
                    chapter_id=chapter_id,
                    current_status=str(current_state),
                )
            # 取最新已落盘版本(= 人工改稿版本);无版本 = 异常态,早抛。
            budget = self._load_authoritative_budget(workspace_id, chapter_id)
            versioning = ChapterVersioning(paths)
            latest_version_id = budget.current_version
            if self._session_factory is None:
                latest_version_id = versioning.next_version_id(chapter_id) - 1
            if latest_version_id < 1:
                raise ChapterReReviewNoVersionError(chapter_id=chapter_id)
            with bind_chapter_context(
                workspace_id=workspace_id,
                chapter_id=chapter_id,
                trace_id=make_trace_id(
                    chapter_id,
                    "re-review",
                    budget.rewrite_count,
                ),
            ):
                return self._orchestrate(
                    workspace_id=workspace_id,
                    chapter_id=chapter_id,
                    initial_state=ChapterState.REVIEWING,
                    initial_revise_count=budget.revise_count,
                    initial_rewrite_count=budget.rewrite_count,
                    on_state_transition=on_state_transition,
                    profile_override=profile_override,
                    initial_last_version_id=latest_version_id,
                    single_review_pass=True,
                    operation_budget=budget,
                    operation_name="re-review",
                    defer_operation_release=(
                        attempt_task_id is not None
                        or (
                            existing_scope is not None
                            and existing_scope.run_id is not None
                        )
                    ),
                )

    def _resolve_progression_consistency(self, workspace_id: str) -> tuple[bool, str]:
        """模块②(v1.1 w5-s10)境界一致性是否对本工作区生效 + 题材标签。

        门控 = style_template ∈ 3 题材(古典仙侠/搞笑爽文/热血战斗)且 generation
        配置 progression_consistency 解析为 on(auto→古典仙侠 ON、爽文/热血 OFF)。
        返回 ``(enabled, label)``,label = 境界/战力(供 writer 注入 + extract 抽取
        标签),解析后透传 ``orchestrate_chapter`` → ``write_chapter`` / ``extract``。

        镜像 ``outline_service._resolve_breakthrough_payoff_enabled``(两 service 各
        薄封装,共用 ``genre_mechanism`` 纯函数、不互相依赖)。任何读失败(工作区/配置
        异常)→ ``(False, "境界")``(进阶子管线 no-op,零回归 + 不阻断章节生成)。
        """
        try:
            from src.core.models.genre_mechanism import (
                progression_label,
                resolve_progression_consistency,
            )
            from src.services.config_service import ConfigService

            workspace = self._workspace_service.info(workspace_id)
            style = getattr(workspace, "style_template", None)
            cfg = ConfigService(
                workspaces_root=self._workspace_service.workspaces_root
            ).load_generation_config(workspace_id)
            enabled = resolve_progression_consistency(
                style_template=style, mode=cfg.progression_consistency
            )
            return enabled, progression_label(style)
        except Exception:  # noqa: BLE001 — 门控读失败退 (False, "境界"),绝不阻断生成
            return False, "境界"

    def _resolve_style_anchor_book(self, workspace_id: str) -> bool:
        """v2 w2-s2 注入端 A/B:工作区 ``generation.style_anchor_source``
        是否为 ``book_anchor``(writer prompt 段均/句均锚全书 ch1-3 中位)。

        镜像 ``_resolve_progression_consistency`` 薄封装模式;任何读失败 →
        ``False``(前章锚现状行为,零回归 + 不阻断章节生成)。
        """
        try:
            from src.services.config_service import ConfigService

            cfg = ConfigService(
                workspaces_root=self._workspace_service.workspaces_root
            ).load_generation_config(workspace_id)
            return cfg.style_anchor_source == "book_anchor"
        except Exception:  # noqa: BLE001 — 门控读失败退前章锚现状,绝不阻断生成
            return False

    def _calibration_default_trim(
        self, router: ModelRouter | None, *, kind: str
    ) -> bool:
        """W3.5 校准联动:当前 writer 模型校准是否**默认开**指定 trim(三态 None 态取值)。

        keyed on **writer 模型**(谁写正文 = trim 作用对象;对齐
        ``OutlineService._resolve_length_calibration``):``router.select_profile("writer").model``
        → ``load_model_calibration`` → ``GlobalCalibration.wants_*_trim()``(映射单一真相源)。

        以下情形一律返 ``False``(= 默认关 = 零回归,不阻断生成):① router 缺失
        (legacy client+model 路径 / 测试)② writer 模型无 stored 校准(qwen 等)
        ③ 解析 / 加载任何异常。

        :param kind: ``"word_count"`` 或 ``"anti_tic"``。
        """
        if router is None:
            return False
        try:
            model_name = router.select_profile("writer").model
            from src.config import settings as _settings
            from src.core.config_loader import load_model_calibration

            calibrations = load_model_calibration(_settings.config_yaml_path)
        except Exception:  # noqa: BLE001 — 解析/加载失败退默认关,绝不阻断生成
            return False
        cal = calibrations.get(model_name)
        if cal is None:
            return False  # 未校准(qwen 等)→ 默认关
        if kind == "anti_tic":
            return cal.global_.wants_anti_tic_trim()
        return cal.global_.wants_word_count_trim()

    def _resolve_trim_tristate(
        self,
        workspace_id: str,
        router: ModelRouter | None,
        *,
        field: str,
        kind: str,
    ) -> bool:
        """W3.5 校准联动三态解析:工作区显式 ``True``/``False`` 尊重;``None``(未设)
        → 当前 writer 模型校准默认(``_calibration_default_trim``)。

        三态来源 = ``ConfigService.load_generation_override``(project.yaml 原始 override,
        保留 None/True/False 三态),**非** ``load_generation_config``(合并后塌成 bool 丢
        三态)。override 读失败 → 退校准默认(不阻断);校准解析失败 → 退 ``False``。整链
        零 fail-open(任何失败最终退默认关)。
        """
        from src.services.config_service import ConfigService

        try:
            override = (
                ConfigService(workspaces_root=self._workspace_service.workspaces_root)
                .load_generation_override(workspace_id)
                .get(field)
            )
        except Exception:  # noqa: BLE001 — override 读失败退校准默认(下方),不阻断
            override = None
        if override is not None:
            return bool(override)  # 工作区显式覆盖(强制开/关)
        return self._calibration_default_trim(router, kind=kind)  # None → 跟随校准

    def _resolve_anti_tic_trim(
        self, workspace_id: str, router: ModelRouter | None
    ) -> bool:
        """v2 W3.5 wave-3 + 校准联动:是否对本工作区开 anti-tic trim(**三态**)。

        工作区 ``generation.anti_tic_trim_enabled`` 显式 ``True``/``False`` 尊重;**未设
        (None)→ 当前 writer 模型校准默认**(``anti_stamp_strength==high`` → 开,如
        deepseek;qwen 无校准 → 关)。开 → orchestrator 在 writer 出稿后加 anti-tic trim
        pass(清「不是A是B」+ 演后盖章,带三护栏)。任何读失败 → ``False``(零回归)。
        """
        return self._resolve_trim_tristate(
            workspace_id, router, field="anti_tic_trim_enabled", kind="anti_tic"
        )

    def _resolve_word_count_trim(
        self, workspace_id: str, router: ModelRouter | None
    ) -> bool:
        """v2 W3.5-s3rev + 校准联动:是否对本工作区开超长 trim(**三态**)。

        工作区 ``generation.word_count_trim_enabled`` 显式 ``True``/``False`` 尊重;**未设
        (None)→ 当前 writer 模型校准默认**(``length_tendency==overshoot`` → 开,如
        deepseek;qwen 无校准 → 关)。开 → orchestrator 在 writer 出稿后(anti-tic 之后)
        若正文超 ``word_count_max`` 加 trim 精简 pass(bounded-retry + 零 fail-open)。
        任何读失败 → ``False``(零回归)。
        """
        return self._resolve_trim_tristate(
            workspace_id, router, field="word_count_trim_enabled", kind="word_count"
        )

    def _orchestrate(
        self,
        *,
        workspace_id: str,
        chapter_id: str,
        initial_state: ChapterState,
        initial_revise_count: int,
        initial_rewrite_count: int,
        on_state_transition: StateTransitionCallback | None = None,
        profile_override: str | None = None,
        skip_must_feasibility_precheck: bool = False,
        initial_last_version_id: int | None = None,
        single_review_pass: bool = False,
        rewrite_checkpoint_context: RewriteCheckpointContext | None = None,
        review_response_recovery: KnownReviewResponseRecovery | None = None,
        operation_budget: _ChapterBudget | None = None,
        operation_name: str | None = None,
        defer_operation_release: bool = False,
    ) -> OrchestratorResult:
        """write / rewrite / re-review 共用的内部路径:校验工作区 → 读章节卡 → upsert
        chapters 行(P2-B-17)→ 拼记忆段 → 调 Orchestrator(state callback
        组合写库)。

        P2-B-17 起 ``session_factory`` 提供时:
        1. 入口 ``upsert_chapter_state(state=initial_state)`` 把"已开工"事件
           落库——Orchestrator 第一次 transition 之前的状态也有记录,前端
           ``GET /chapters/{id}/status`` 不会看到"中间空窗"。
        2. ``on_state_transition`` 被 ``_wrap_state_transition_with_db`` 包成
           "先写 DB 再调用户回调",Orchestrator 每次 transition 后状态机
           节点立即落库。最终态(APPROVED / HUMAN_INTERVENTION)也是一次
           transition,同样会落库。
        """
        paths = self._validate_workspace(workspace_id)
        card = self.read_chapter_card(workspace_id, chapter_id)
        if card.chapter_id != chapter_id:
            # 防御:章节卡内 chapter_id 应与传入一致(save 时由 card.chapter_id
            # 决定文件名,但若维护人手工放入文件名不匹配的 json 也要早抛错)。
            raise ChapterServiceError(
                f"章节卡内 chapter_id={card.chapter_id} 与传入 {chapter_id} 不一致"
            )
        world_contract = self._load_world_contract(
            paths,
            required=self._outline_contract_required(workspace_id, paths),
        )
        if (
            world_contract is not None
            and card.setting_contract_hash != world_contract.source_hash
        ):
            stale = ChapterCardSettingStaleError(
                chapter_id=chapter_id,
                card_contract_hash=card.setting_contract_hash,
                current_contract_hash=world_contract.source_hash,
            )
            _log.error(
                "prewriting_semantic_gate_failed",
                failure_layer="writer_preflight_setting_stale",
                workspace_id=workspace_id,
                chapter_id=chapter_id,
                constraint_ids=[stale.constraint_id],
                contract_hash=world_contract.source_hash,
                card_contract_hash=card.setting_contract_hash,
                attempt=0,
            )
            raise stale
        self._validate_card_dramatic_source_preflight(
            workspace_id=workspace_id,
            paths=paths,
            card=card,
        )
        continuity_preflight = self._prepare_card_continuity_preflight(
            workspace_id=workspace_id,
            card=card,
            paths=paths,
        )
        card = continuity_preflight.card
        continuity_capsule = continuity_preflight.capsule
        core_state_projection = None
        if card.core_state_contract_version == 1:
            if self._session_factory is None:
                raise ChapterServiceError(
                    "core_state contract v1 缺少 session_factory，已阻止 Provider 请求"
                )
            try:
                core_state_projection = CoreStateService(
                    session_factory=self._session_factory
                ).validate_card_contract(
                    workspace_id=workspace_id,
                    card=card,
                )
            except CoreStateServiceError as exc:
                raise ChapterServiceError(
                    f"Writer 前核心状态合同复核失败: {exc}"
                ) from exc
        # R8 安全前置:显式 Rewrite 的审稿 checkpoint 必须在 router/runtime
        # 构造、章节状态写入和任何 Provider 请求之前读取并校验。坏 DB / 坏
        # constraint_result 由 loader fail-closed，原章节现场保持不动。
        pipeline_session_factory: Callable[[], Session] | None = None
        if self._session_factory is not None:
            session_factory_local = self._session_factory  # closure narrow

            def pipeline_session_factory() -> Session:
                return session_factory_local(workspace_id)

        if (
            initial_state == ChapterState.REWRITING
            and initial_last_version_id is not None
            and initial_last_version_id >= 1
            and rewrite_checkpoint_context is None
        ):
            rewrite_checkpoint_context = load_rewrite_checkpoint_context(
                pipeline_session_factory,
                paths=paths,
                chapter_id=chapter_id,
                version_id=initial_last_version_id,
            )

        # P3-B-06 / P6-B-09:router 构造前置到 _upsert_chapter_state 之前。
        # force_profile 越界(章节卡持久化字段引用未声明 profile_id)在 DB
        # 推进 chapter.status 之前抛 ``WriterProfileNotFoundError`` → 422
        # ``WRITER_PROFILE_NOT_FOUND``,确保 SQLite 行**未被修改**(rollback
        # 测试约定 — P6-B-09 §10 字面)。router=None 路径(P1 legacy / 测试
        # fallback)force_profile 静默失效,与 profile_override 行为一致。
        router = (
            self._router_factory(workspace_id)
            if self._router_factory is not None
            else None
        )
        if router is not None and card.force_profile is not None:
            if not router.has_profile(card.force_profile):
                known = sorted(router._config.profiles.keys())
                exc = WriterProfileNotFoundError(
                    f"章节卡 force_profile={card.force_profile!r} 未在 "
                    f"writer_profiles 声明; 已声明: {known}"
                )
                exc.details = {
                    "profile_id": card.force_profile,
                    "known_profiles": known,
                }
                raise exc
        operation_lease: ChapterOperationLease | None = None
        if operation_budget is not None:
            operation_lease = self._acquire_chapter_operation_state(
                workspace_id,
                chapter_id,
                expected=operation_budget,
                target_state=initial_state.value,
                operation=operation_name or initial_state.value,
            )
        # P2-B-17:入口 upsert chapters 行,记录"已开工"。
        self._upsert_chapter_state(
            workspace_id,
            chapter_id,
            state=initial_state.value,  # type: ignore[arg-type]
        )
        # P4-B-03:wrap 闭包透传 card,APPROVED 时调 metrics_writer 落
        # ``metrics_per_chapter`` 行。card 此处是文件版本或 preflight 只改
        # ``continuity_context.unfinished_actions`` 的内存归一化副本;
        # legacy ``_maybe_refresh_continuity`` 也只更新 ``continuity_context``，
        # 均不影响 metrics_writer 消费的 ``word_count_min/max`` /
        # ``tension_level``,顺序无副作用。
        wrapped_cb = self._wrap_state_transition_with_db(
            workspace_id, chapter_id, on_state_transition, card=card
        )
        memory_section = build_memory_section(paths)

        # P2-B-19 / P4-B-02:wiring 双 gate——
        # ① pipeline_session_factory:只要 ``self._session_factory`` 在,就透传
        #   给 orchestrator。orchestrator 用其落 chapter_versions / review_results
        #   表(P4-B-02)+ 在 wiring_enabled(两 factory 都在)时附加 voice 段 /
        #   memory_extractor commit(P2-B-19)。
        # ② pipeline_vector_store:仅在 ``self._vector_store_factory`` 也给定时
        #   构造,与 session_factory 在 orchestrator 内部组成 ``wiring_enabled``。
        # 把"按 workspace_id 开 session / 构造 vector_store"包成 0-arg
        # (orchestrator 不感知 workspace_id),与 ``_wrap_state_transition_with_db``
        # 短 session 路径串行不冲突。
        pipeline_vector_store: VectorStore | None = None
        if self._vector_store_factory is not None:
            pipeline_vector_store = self._vector_store_factory(workspace_id)

        # P3-B-06:router 给定 → 走 router 路径;否则 fallback legacy
        # ``client`` + ``model``(对齐 ``orchestrate_chapter`` 二选一签名)。
        # P3-B-11:router 在位 + profile_override 给定时,先 wrap
        # ``_ProfileOverrideRouter`` 强制注入 override profile_id;非法
        # ``profile_id`` 由 wrap 入口抛 ``ProfileNotFoundError``(API 层翻译
        # 为 422 ``CHAPTER_INVALID_PROFILE``)。
        # P3-B-07:wrap 顺序 outer→inner = ``_CostTrackingRouter`` →
        # ``_ProfileOverrideRouter`` → ``ModelRouter``。cost wrap 在外才能
        # 拿到已 override 的 profile 计费(``RoutedTarget.profile`` 是
        # override profile,cost 按其 ``cost_per_1k_*`` 计费)。
        # session_factory 缺位 → 无写库目标,跳过 cost 包装(测试 / P1 兼容
        # 路径);profile_override 仍可生效(单 wrap)。
        # P6-B-09:章节卡 ``force_profile`` 字段持久化覆盖。优先级:
        # ``card.force_profile`` > 请求 body ``profile_override`` > None(默认
        # 路由)。router 构造与 force_profile 越界前置校验已上移到本方法开头
        # (上文 router = self._router_factory(...) + has_profile 校验段),
        # 此处只负责 wrap 顺序串联。
        if router is not None:
            effective_override = card.force_profile or profile_override
            router = wrap_router_with_profile_override(router, effective_override)
        if router is not None and self._session_factory is not None:
            session_factory_local = self._session_factory  # closure narrow
            router = wrap_router_with_cost_tracking(
                router,
                workspace_id=workspace_id,
                chapter_id=chapter_id,
                session_factory=lambda: session_factory_local(workspace_id),
            )

        if operation_lease is not None and self._operation_lease_store is not None:
            operation_lease = self._operation_lease_store.mark_phase(
                operation_lease,
                phase="continuity_refresh_may_call_provider",
            )

        # P3-B-13:ch>=2 时从上一章 final.md 重抽 continuity_context 覆盖卡片
        # (P2-B-12 模块层落地兑现 caller;P3-acceptance 观察 2 还清)。失败 /
        # 跳过路径返原 card,不阻塞主流水线。
        card = self._maybe_refresh_continuity(paths, card, router)
        if continuity_capsule is not None:
            try:
                continuity_capsule.assert_matches_projection(
                    frozen_provenance=card.continuity_ledger_provenance,
                    prev_chapter_tail=card.continuity_context.prev_chapter_tail,
                    unfinished_actions=card.continuity_context.unfinished_actions,
                    pov_from=card.continuity_context.pov_from,
                )
            except ValueError as exc:
                _log.warning(
                    "continuity_capsule_full_prompt_fallback",
                    workspace_id=workspace_id,
                    chapter_id=card.chapter_id,
                    reason=str(exc),
                    stage="post_legacy_refresh",
                )
                continuity_capsule = None
        if card.core_state_contract_version == 1:
            # legacy continuity refresh 可能改写 time_gap；v1 卡的时钟已经在批准时
            # 冻结，因此 Writer Provider 前再做一次零调用验真，防止旧辅助路径把
            # Writer/Judge 共尺合同悄悄改掉。
            assert self._session_factory is not None
            try:
                core_state_projection = CoreStateService(
                    session_factory=self._session_factory
                ).validate_card_contract(
                    workspace_id=workspace_id,
                    card=card,
                )
            except CoreStateServiceError as exc:
                raise ChapterServiceError(
                    f"continuity refresh 后核心状态合同复核失败: {exc}"
                ) from exc

        # 模块②(v1.1 w5-s10):解析本工作区境界一致性门控(style_template ∈ 3 题材
        # + generation 配置)→ 透传 orchestrate_chapter → writer 注入防漂移 + extract
        # 锚定抽取。读失败退 (False, "境界")(进阶子管线 no-op,零回归)。
        prog_enabled, prog_label = self._resolve_progression_consistency(workspace_id)

        # v2 w2-s2 注入端 A/B:解析工作区 generation.style_anchor_source →
        # 透传 orchestrate_chapter(book_anchor 时 writer prompt 段均/句均锚
        # 全书 ch1-3 中位)。读失败退 False(前章锚现状,零回归)。
        style_anchor_book = self._resolve_style_anchor_book(workspace_id)

        # v2 W3.5 wave-3 + 校准联动:三态解析工作区 generation.anti_tic_trim_enabled
        # → 透传 orchestrate_chapter。显式 True/False 尊重;未设(None)→ 当前 writer
        # 模型校准默认(deepseek anti_stamp_strength=high → 开;qwen 无校准 → 关)。
        # 读失败退 False(功能关,零回归 + 不阻断生成)。
        anti_tic_trim_enabled = self._resolve_anti_tic_trim(workspace_id, router)

        # v2 W3.5-s3rev + 校准联动:三态解析工作区 generation.word_count_trim_enabled
        # → 透传 orchestrate_chapter。显式 True/False 尊重;未设(None)→ 当前 writer
        # 模型校准默认(deepseek length_tendency=overshoot → 开;qwen 无校准 → 关)。
        # 读失败退 False(功能关,零回归 + 不阻断生成)。
        word_count_trim_enabled = self._resolve_word_count_trim(workspace_id, router)

        if operation_lease is not None and self._operation_lease_store is not None:
            operation_lease = self._operation_lease_store.mark_phase(
                operation_lease,
                phase="provider_capable_pipeline",
            )

        if review_response_recovery is not None:
            if router is None:
                raise RewriteCheckpointRecoveryError("已知审核响应恢复需要原模型路由")
            router = cast(ModelRouter, KnownReviewRecoveryRouter(router, review_response_recovery))

        result = orchestrate_chapter(
            router=router,
            client=self._client,
            model=self._model,
            chapter_card=card,
            paths=paths,
            memory_section=memory_section,
            workspace_id=workspace_id,
            initial_state=initial_state,
            initial_revise_count=initial_revise_count,
            initial_rewrite_count=initial_rewrite_count,
            on_state_transition=wrapped_cb,
            session_factory=pipeline_session_factory,
            vector_store=pipeline_vector_store,
            world_contract=world_contract,
            progression_consistency_enabled=prog_enabled,
            progression_label=prog_label,
            skip_must_feasibility_precheck=skip_must_feasibility_precheck,
            style_anchor_book=style_anchor_book,
            anti_tic_trim_enabled=anti_tic_trim_enabled,
            word_count_trim_enabled=word_count_trim_enabled,
            initial_last_version_id=initial_last_version_id,
            single_review_pass=single_review_pass,
            post_approval_memory_enabled=False,
            rewrite_checkpoint_context=rewrite_checkpoint_context,
            core_state_projection=core_state_projection,
            continuity_capsule=continuity_capsule,
        )
        if operation_lease is not None and self._operation_lease_store is not None:
            operation_lease = self._operation_lease_store.mark_phase(
                operation_lease,
                phase="orchestrator_result_materialized",
            )
        # W3.5-C:写作水位拆分。orchestrator 此次被显式要求不在 APPROVED 后
        # 同步抽 memory;先落 ready_for_next_chapter 硬账本,保证下一章能用
        # 上章 final.md + 已验证小账本继续写,再调度 memory extractor /
        # 向量 / 指标二次归档等富记忆加工。force_pass 由
        # ChapterRewindService 走同一 ledger → ready 顺序。
        if result.final_state == ChapterState.APPROVED:
            if result.final_version_id is None:
                raise ContinuityLedgerNotReadyError(
                    chapter_id=chapter_id,
                    reason="orchestrator 未返回 final_version_id",
                )
            if self._session_factory is not None:
                try:
                    if (
                        operation_lease is not None
                        and self._operation_lease_store is not None
                    ):
                        operation_lease = self._operation_lease_store.mark_phase(
                            operation_lease,
                            phase="continuity_ledger_may_call_provider",
                        )
                    with StageTimer(
                        _log,
                        "chapter_stage_timing",
                        {
                            "workspace_id": workspace_id,
                            "chapter_id": chapter_id,
                            "stage": "continuity_ledger",
                            "final_version_id": result.final_version_id,
                        },
                    ):
                        extract_and_persist_continuity_ledger(
                            workspace_id=workspace_id,
                            chapter_id=chapter_id,
                            source_version_id=result.final_version_id,
                            paths=paths,
                            session_factory=self._session_factory,
                            llm_client=self._client,
                            llm_model=self._model,
                            router=router,
                            trigger="reviewer_pass",
                        )
                    CoreStateService(
                        session_factory=self._session_factory
                    ).commit_approved_chapter(
                        workspace_id=workspace_id,
                        card=card,
                        source_version_id=result.final_version_id,
                        paths=paths,
                    )
                except ContinuityLedgerValidationError as exc:
                    raise ContinuityLedgerNotReadyError(
                        chapter_id=chapter_id,
                        reason=str(exc),
                    ) from exc
                if (
                    operation_lease is not None
                    and self._operation_lease_store is not None
                ):
                    operation_lease = self._operation_lease_store.mark_phase(
                        operation_lease,
                        phase="continuity_ledger_persisted",
                    )
            self._mark_ready_for_next_chapter(
                workspace_id=workspace_id,
                chapter_id=chapter_id,
                paths=paths,
                final_path=result.final_path,
                final_version_id=result.final_version_id,
            )
            if operation_lease is not None and self._operation_lease_store is not None:
                operation_lease = self._operation_lease_store.mark_phase(
                    operation_lease,
                    phase="ready_persisted",
                )
            self._schedule_memory_enrichment_after_ready(
                workspace_id=workspace_id,
                chapter_id=chapter_id,
                final_version_id=result.final_version_id,
                paths=paths,
                card=card,
                router=router,
                final_path=result.final_path,
                session_factory=pipeline_session_factory,
                progression_consistency_enabled=prog_enabled,
                progression_label=prog_label,
            )
        # v1.1 acceptance 修复2 / Fix D:同步 ``chapters.escalation_details``
        # 列 = 本次编排的升级详情(升人工 + 有详情 → 写入含 reason 的 dict;
        # 其余 → 清 None,防上一轮 Fix D 详情在新一轮 budget 耗尽 / 正常通过后
        # 残留误导 UI)。失败吞(详情属附加信息,不让 :write 整体报错)。
        self._sync_escalation_details_safely(
            workspace_id,
            chapter_id,
            details=(
                {"reason": result.escalation_reason, **result.escalation_details}
                if (
                    result.final_state == ChapterState.HUMAN_INTERVENTION
                    and result.escalation_details is not None
                )
                else None
            ),
        )
        if operation_lease is not None and self._operation_lease_store is not None:
            self._operation_lease_store.require_held(operation_lease)
            operation_lease = self._operation_lease_store.mark_phase(
                operation_lease,
                phase="operation_complete",
            )
            if not defer_operation_release:
                self._operation_lease_store.release(operation_lease)
        return result

    def _maybe_refresh_continuity(
        self,
        paths: WorkspacePaths,
        card: ChapterCard,
        router: ModelRouter | None,
    ) -> ChapterCard:
        """ch>=2 时尝试从上一章 final.md 重抽 ``continuity_context`` 覆盖 ``card``。

        分支:
          - 带 ``continuity_ledger_provenance`` → preflight 已完成 ledger/projection/
            bridge 复核；保留前章富记忆降级日志后以 ``ledger_managed_card`` 早退，
            不再调用旧 rule/LLM refresh，也不写回卡文件。
          - ``card.chapter_number < 2`` → 首章,直接返原 card(不调 LLM)。
          - 上一章卡(``chapter_number = N-1``)未找到 → 返原 card + log。
          - 上一章卡存在但无 ``final.md``(尚未 approved)→ 返原 card + log。
          - LLM 抽提失败(``ContinuityExtractorError`` / 其他 ``ValueError``)→
            **降级到 rule 路径**(``build_continuity_context(strategy="rule")``,
            截上一章末尾 800 字字面);rule 路径再失败 → 返原 card + warn
            (v1.0.x wave 5 session 3 主题 l;原始 swallow 语义保留为兜底)。
          - 其他异常(OSError / 未知 Exception)→ 返原 card + warn,**不冒泡**
            (对齐 P2-B-19 memory_extractor / P3-B-07 cost_tracker "辅助任务失败
            swallow + log" 先例)。

        无 provenance 的 legacy 路径成功重抽后,refreshed ``ContinuityContext``
        通过 ``model_copy`` 覆盖到 card 并**同步写回
        ``data/cards/<chapter_id>.json``**(v1.0.x wave 5
        session 3 主题 m;让 UI 读章节卡 JSON 时看到 Writer 实际拿到的真值,
        而非批量生成时的占位字符串)。写回失败 swallow + warn,不阻塞主流程。

        ``router`` 优先(若 ``_router_factory`` 已注入);否则走 legacy
        ``self._client`` + ``self._model``(P1/P2 兼容路径)。
        """
        if card.continuity_ledger_provenance is not None:
            prev_chapter_id = card.continuity_ledger_provenance.chapter_id
            enrichment_status, enrichment_details = self._read_memory_enrichment_status(
                paths.workspace_id,
                prev_chapter_id,
            )
            if enrichment_status in {"failed", "pending", "running"}:
                _log.warning(
                    "continuity_refresh_degraded_memory_enrichment",
                    chapter_id=card.chapter_id,
                    chapter_number=card.chapter_number,
                    prev_chapter_id=prev_chapter_id,
                    memory_enrichment_status=enrichment_status,
                    memory_enrichment_details=enrichment_details,
                    fallback="prev_final_md_and_chapter_card",
                )
            _log.info(
                "continuity_refresh_skipped",
                chapter_id=card.chapter_id,
                chapter_number=card.chapter_number,
                reason="ledger_managed_card",
            )
            return card

        # 首章判断 + 上一章推导都改用全局 chapter_id(不用卷内 chapter_number 作
        # 时序锚,见 _find_prev_chapter_id docstring / chapter_card.py 契约)。
        m = re.fullmatch(r"ch-(\d+)", card.chapter_id)
        if m is None or int(m.group(1)) < 2:
            return card

        prev_chapter_id = self._find_prev_chapter_id(paths, card.chapter_id)
        if prev_chapter_id is None:
            _log.info(
                "continuity_refresh_skipped",
                chapter_id=card.chapter_id,
                chapter_number=card.chapter_number,
                reason="prev_card_not_found",
            )
            return card

        prev_final = paths.chapter_version_file(
            prev_chapter_id, ChapterVersioning.FINAL_FILENAME
        )
        if not prev_final.is_file():
            _log.info(
                "continuity_refresh_skipped",
                chapter_id=card.chapter_id,
                chapter_number=card.chapter_number,
                prev_chapter_id=prev_chapter_id,
                reason="prev_final_missing",
            )
            return card

        try:
            prev_content = prev_final.read_text(encoding="utf-8")
        except OSError as exc:
            _log.warning(
                "continuity_refresh_failed",
                chapter_id=card.chapter_id,
                chapter_number=card.chapter_number,
                prev_chapter_id=prev_chapter_id,
                reason="prev_final_read_failed",
                error_type=type(exc).__name__,
                error=str(exc),
            )
            return card

        enrichment_status, enrichment_details = self._read_memory_enrichment_status(
            paths.workspace_id, prev_chapter_id
        )
        if enrichment_status in {"failed", "pending", "running"}:
            _log.warning(
                "continuity_refresh_degraded_memory_enrichment",
                chapter_id=card.chapter_id,
                chapter_number=card.chapter_number,
                prev_chapter_id=prev_chapter_id,
                memory_enrichment_status=enrichment_status,
                memory_enrichment_details=enrichment_details,
                fallback="prev_final_md_and_chapter_card",
            )

        try:
            refreshed = build_continuity_context(
                prev_content=prev_content,
                current_card=card,
                router=router,
                client=self._client,
                model=self._model,
            )
        except ValueError as exc:
            # v1.0.x wave 5 session 3 主题 l:LLM 路径失败(含 ContinuityExtractorError
            # 即 ValueError 子类 + router/client 校验抛出的 ValueError)→ 降级走
            # rule 路径(extract_from_prev_text 截字面 800 字),保证 Writer 拿到
            # 真字面 tail,不会因 LLM 抽提崩而退化到 batch 阶段的占位字符串。
            _log.warning(
                "continuity_refresh_llm_failed_fallback_rule",
                chapter_id=card.chapter_id,
                chapter_number=card.chapter_number,
                prev_chapter_id=prev_chapter_id,
                error_type=type(exc).__name__,
                error=str(exc),
            )
            try:
                refreshed = build_continuity_context(
                    prev_content=prev_content,
                    current_card=card,
                    strategy="rule",
                )
            except Exception as fallback_exc:  # noqa: BLE001 — rule 兜底兜底
                _log.warning(
                    "continuity_refresh_rule_fallback_failed",
                    chapter_id=card.chapter_id,
                    chapter_number=card.chapter_number,
                    prev_chapter_id=prev_chapter_id,
                    error_type=type(fallback_exc).__name__,
                    error=str(fallback_exc),
                )
                return card
        except Exception as exc:  # noqa: BLE001 — swallow per P2-B-19 / P3-B-07 先例
            _log.warning(
                "continuity_refresh_failed",
                chapter_id=card.chapter_id,
                chapter_number=card.chapter_number,
                prev_chapter_id=prev_chapter_id,
                error_type=type(exc).__name__,
                error=str(exc),
            )
            return card

        if (
            card.core_state_contract_version == 1
            and refreshed.time_gap != card.continuity_context.time_gap
        ):
            _log.warning(
                "continuity_refresh_preserved_frozen_book_clock",
                chapter_id=card.chapter_id,
                original_time_gap=card.continuity_context.time_gap,
                ignored_time_gap=refreshed.time_gap,
            )
            refreshed = refreshed.model_copy(
                update={"time_gap": card.continuity_context.time_gap}
            )
        refreshed_card = card.model_copy(update={"continuity_context": refreshed})

        # v1.0.x wave 5 session 3 主题 m:写回 chapter card JSON SSOT,让 UI 读
        # 时拿到的就是 Writer 实际消费的真值(原子写,对齐 save_chapter_card)。
        # 失败 swallow + warn —— 不阻塞主流水线(Writer 仍能拿到 refreshed_card
        # 写正文,只是 UI 暂时看不到新 tail,下次 :write/:rewrite 会再尝试落盘)。
        # v1.0.x wave 6 s2.6a:race 防御 — 工作区已销毁则 skip(避免 mkdir 重建残留)。
        if not paths.root.is_dir():
            _log.warning(
                "workspace_writer_skipped",
                writer="_maybe_refresh_continuity_persist",
                chapter_id=card.chapter_id,
                chapter_number=card.chapter_number,
                reason="workspace_destroyed_during_async_task",
            )
            return refreshed_card
        try:
            card_path = self._card_path(paths, card.chapter_id)
            card_path.parent.mkdir(parents=True, exist_ok=True)
            tmp_path = card_path.with_suffix(card_path.suffix + ".tmp")
            tmp_path.write_text(
                refreshed_card.model_dump_json(indent=2), encoding="utf-8"
            )
            tmp_path.replace(card_path)
        except OSError as exc:
            _log.warning(
                "continuity_refresh_persist_failed",
                chapter_id=card.chapter_id,
                chapter_number=card.chapter_number,
                error_type=type(exc).__name__,
                error=str(exc),
            )

        _log.info(
            "continuity_refreshed",
            chapter_id=card.chapter_id,
            chapter_number=card.chapter_number,
            prev_chapter_id=prev_chapter_id,
        )
        return refreshed_card

    def _find_prev_chapter_id(
        self, paths: WorkspacePaths, current_chapter_id: str
    ) -> str | None:
        """从全局 ``chapter_id``(``ch-{global_idx:04d}`` 单调)推导上一章 id。

        v1.1 wave 4 B1 acceptance(2026-06-02 修根因):原实现按
        ``chapter_number = current - 1`` 扫卡,**违反 ChapterCard.chapter_number
        "卷内 1-based,跨卷重置,不能作时序锚" 契约**(chapter_card.py:13)——多卷
        工作区 chapter_number 跨卷重复 → glob 命中错卷同号章(B1 ch-5 撞 ch-16)。
        改用全局单调 ``chapter_id``(chapter_card.py:12)推导:上一章 = 全局序号 - 1,
        天然唯一且正确跨卷边界(ch-0007 的上一章 = ch-0006)。

        - ``chapter_id`` 非 ``ch-NNNN`` 格式 / 全局序号 <= 1 → ``None``(首章)。
        - 推导出的上一章卡 JSON 不存在 → ``None``。
        """
        m = re.fullmatch(r"ch-(\d+)", current_chapter_id)
        if m is None:
            return None
        idx = int(m.group(1))
        if idx <= 1:
            return None
        prev_id = f"ch-{idx - 1:04d}"
        if not (paths.cards_dir / f"{prev_id}.json").is_file():
            return None
        return prev_id

    def refresh_continuity_all(self, workspace_id: str) -> ContinuityRefreshResult:
        """批量重抽工作区所有章节卡的 ``continuity_context``(v1.1 w3-s7 E6)。

        一键治理批量生成阶段遗留在 ``continuity_context`` 里的占位 tail —— 扫
        ``cards_dir/*.json`` 逐张:

        - JSON 损坏 / schema 不符 → 按 ``list_chapters`` 惯例 warn-skip,不入
          ``items`` 也不计 ``total_scanned``(整批不被一张坏卡阻塞)。
        - ``chapter_number < 2`` → ``skipped_first``(首章无上一章)。
        - ``ch >= 2`` → 复用 ``_maybe_refresh_continuity``(已含 LLM→rule fail-open
          降级 + 写回卡 JSON SSOT)。比对重抽前后 ``continuity_context`` 是否变化:
          变了 → ``refreshed``;未变 → ``unchanged``(上一章未 approved / 找不到 /
          抽提降级返原值)。

        ``router`` 走 ``self._router_factory(workspace_id)``(若注入),与 ``:write``
        路径同源;未注入 → ``None``,``_maybe_refresh_continuity`` 内走 legacy
        client/model 或 rule 兜底。

        - 工作区不存在 → ``WorkspaceServiceError``
        - ``cards_dir`` 不存在 / 空 → 返空结果(无章节卡是合法状态)
        """
        paths = self._validate_workspace(workspace_id)
        router = (
            self._router_factory(workspace_id)
            if self._router_factory is not None
            else None
        )

        loaded: list[ChapterCard] = []
        cards_dir = paths.cards_dir
        if cards_dir.is_dir():
            for card_path in sorted(cards_dir.glob("*.json")):
                try:
                    data = json.loads(card_path.read_text(encoding="utf-8"))
                    loaded.append(ChapterCard.model_validate_storage(data))
                except (json.JSONDecodeError, ValidationError, OSError) as exc:
                    _log.warning(
                        "refresh_continuity_all_skip_invalid_card",
                        workspace_id=workspace_id,
                        card_path=str(card_path),
                        error=str(exc),
                    )
                    continue

        # 全局 chapter_id 排序(卷内 chapter_number 跨卷重复,不能作全局序)。
        loaded.sort(key=lambda c: c.chapter_id)
        items: list[ContinuityRefreshEntry] = []
        refreshed_count = 0
        for card in loaded:
            _m = re.fullmatch(r"ch-(\d+)", card.chapter_id)
            if _m is None or int(_m.group(1)) < 2:
                outcome: Literal["refreshed", "unchanged", "skipped_first"] = (
                    "skipped_first"
                )
            else:
                refreshed_card = self._maybe_refresh_continuity(paths, card, router)
                if refreshed_card.continuity_context != card.continuity_context:
                    outcome = "refreshed"
                    refreshed_count += 1
                else:
                    outcome = "unchanged"
            items.append(
                ContinuityRefreshEntry(
                    chapter_id=card.chapter_id,
                    chapter_number=card.chapter_number,
                    outcome=outcome,
                )
            )

        _log.info(
            "refresh_continuity_all_completed",
            workspace_id=workspace_id,
            total_scanned=len(items),
            refreshed_count=refreshed_count,
        )
        return ContinuityRefreshResult(
            total_scanned=len(items),
            refreshed_count=refreshed_count,
            items=items,
        )

    def _load_reviser_constraint_results(
        self,
        *,
        workspace_id: str,
        chapter_id: str,
        source_version_id: int,
    ) -> list[ConstraintResult]:
        """读取源版本最新审稿的结构化 HARD 结果；坏账本在 Reviser 前硬停。"""
        if self._session_factory is None:
            return []
        session = self._session_factory(workspace_id)
        try:
            row = (
                session.execute(
                    select(ReviewResultORM)
                    .where(ReviewResultORM.chapter_id == chapter_id)
                    .where(ReviewResultORM.version_reviewed == source_version_id)
                    .order_by(
                        ReviewResultORM.reviewed_at.desc(),
                        ReviewResultORM.review_id.desc(),
                    )
                )
                .scalars()
                .first()
            )
        finally:
            session.close()
        if row is None:
            return []
        if not isinstance(row.constraint_results, list):
            raise RewriteCheckpointRecoveryError(
                f"revise 的 hard-gate checkpoint 形状损坏: {chapter_id}/v{source_version_id}"
            )
        try:
            results = [
                ConstraintResult.model_validate(payload)
                for payload in row.constraint_results
            ]
        except Exception as exc:  # noqa: BLE001 - 坏账本不得进入 Provider
            raise RewriteCheckpointRecoveryError(
                f"revise 的 hard-gate checkpoint 损坏: {chapter_id}/v{source_version_id}"
            ) from exc
        ids = [result.constraint_id for result in results]
        if len(ids) != len(set(ids)):
            raise RewriteCheckpointRecoveryError(
                f"revise 的 hard-gate checkpoint 含重复 ID: {chapter_id}/v{source_version_id}"
            )
        return results

    def _validate_revise_source_checkpoint(
        self,
        *,
        workspace_id: str,
        chapter_id: str,
        paths: WorkspacePaths,
        budget: _ChapterBudget,
        source_version_id: int,
        source_content: str,
    ) -> None:
        """Reviser 出网前逐字冻结 DB/盘版本集合与显式源正文。"""

        session = self._open_session(workspace_id)
        if session is None:
            return
        try:
            row = session.get(ChapterORM, chapter_id)
            versions = list(
                session.scalars(
                    select(ChapterVersionORM)
                    .where(ChapterVersionORM.chapter_id == chapter_id)
                    .order_by(ChapterVersionORM.version_id)
                )
            )
            versions_by_id = {version.version_id: version for version in versions}
            source_row = versions_by_id.get(source_version_id)
            if (
                row is None
                or row.workspace_id != workspace_id
                or row.current_version != budget.current_version
                or not versions_by_id
                or max(versions_by_id) != budget.current_version
                or source_row is None
                or source_row.content != source_content
            ):
                raise RewriteCheckpointRecoveryError(
                    "revise 的不可变 DB 源版本与当前请求不一致，已阻止 Provider: "
                    f"chapter={chapter_id} source=v{source_version_id}"
                )
        finally:
            session.close()

        chapter_dir = paths.chapter_dir(chapter_id)
        try:
            disk_versions = {
                version_id: path.read_text(encoding="utf-8")
                for version_id, path in collect_canonical_version_files(
                    chapter_dir
                ).items()
            }
        except (OSError, UnicodeDecodeError, ChapterVersioningError) as exc:
            raise RewriteCheckpointRecoveryError(
                f"revise 无法验真磁盘版本集合，已阻止 Provider: {exc}"
            ) from exc
        if set(disk_versions) != set(versions_by_id) or any(
            disk_versions[version_id] != version.content
            for version_id, version in versions_by_id.items()
        ):
            raise RewriteCheckpointRecoveryError(
                "revise 的磁盘/DB 版本集合或正文不一致，已阻止 Provider: "
                f"chapter={chapter_id} source=v{source_version_id}"
            )

    # ---- 局部修订:revise(不走 Orchestrator) -----------------------------

    def revise(
        self,
        workspace_id: str,
        chapter_id: str,
        *,
        issues: list[ReviewIssue],
        source_version_id: int | None = None,
        attempt_task_id: str | None = None,
    ) -> ChapterVersion:
        """基于人工指定的 ``ReviewIssue`` 列表调 Reviser agent 落新版本。

        **本方法不参与状态机转换**:产出的新版本不会被自动 mark 成 final,
        也不会触发审稿;新版本对应的 chapter status(drafting / reviewing /
        approved 等)由调用方/上层路由(P1-B-19)显式决定。理由:Orchestrator
        的 ``REVISING`` 分支依赖 ``reviews[-1].revise_targets``,而 P1 阶段
        review 没持久化,硬塞 ``initial_state=REVISING`` 会 assert 挂掉;
        P1 简版让 revise 单独成一个"局部修订一版"的纯动作,审稿留给后续
        ``write`` / ``rewrite`` 调用走完整闭环。

        :param issues: 要修订的问题列表(由调用方从最近一次 ``ReviewResult``
            的 ``revise_targets`` 取);空列表时 Reviser 配合 prompt 兜底原样
            回原文(不推荐这样调用)。
        :param source_version_id: 修订源版本号;``None`` 时取最新版本
            (``next_version_id - 1``)。显式指定可用于"基于历史版本拉新分支"
            的非主路径场景。
        :return: 新落盘的 ``ChapterVersion``,``creation_type="revise"``。

        P1-B-24 起:本方法在入口 ``bind_chapter_context``,``trace_id`` 用
        ``make_trace_id(chapter_id, "revise", 0)``——P1 阶段 ChapterService
        没有 chapters 表可查"已用了几次 revise",revise 调用全部 trace 到
        ``-revise0``(同章节多次 revise 共享 trace_id 是 P1 简化,P2 落库后
        改为按 ``chapters.revise_count`` 自增)。

        - 工作区不存在 → ``WorkspaceServiceError``
        - 章节卡不存在 / 损坏 → ``ChapterServiceError``
        - 章节目录无任何版本(``source_version_id=None`` 默认场景)→
          ``ChapterServiceError``("尚无任何版本,请先 write")
        - 显式 ``source_version_id`` 对应版本文件不存在 → ``ChapterServiceError``
        """
        existing_scope = current_attempt_scope()
        scope_cm = (
            nullcontext(existing_scope)
            if existing_scope is not None
            else bind_attempt_scope(
                **self._resolve_manual_attempt_scope(
                    workspace_id,
                    chapter_id,
                    task_id=attempt_task_id,
                )
            )
        )
        with (
            scope_cm,
            bind_chapter_context(
                workspace_id=workspace_id,
                chapter_id=chapter_id,
                trace_id=make_trace_id(chapter_id, "revise", 0),
            ),
        ):
            self._run_gates(workspace_id, chapter_id=chapter_id)
            paths = self._validate_workspace(workspace_id)
            # Reviser 与 Writer/validator 共用同一 ChapterCard 约束来源。
            card = self.read_chapter_card(workspace_id, chapter_id)

            versioning = ChapterVersioning(paths)
            budget: _ChapterBudget | None = None
            if self._session_factory is not None:
                budget = self._read_authoritative_budget(workspace_id, chapter_id)
                if source_version_id is None:
                    source_version_id = budget.current_version
            elif source_version_id is None:
                next_id = versioning.next_version_id(chapter_id)
                source_version_id = next_id - 1
            if source_version_id < 1:
                raise ChapterServiceError(
                    f"章节 {chapter_id} 尚无任何版本,revise 无源可修;请先 write"
                )

            try:
                chapter_text = versioning.read_version_content(
                    chapter_id, source_version_id
                )
            except (OSError, UnicodeDecodeError) as exc:
                raise RewriteCheckpointRecoveryError(
                    "revise 无法读取 UTF-8 源版本，已阻止 Provider: "
                    f"chapter={chapter_id} version={source_version_id}"
                ) from exc
            except ChapterVersioningError as exc:
                raise ChapterServiceError(
                    f"源版本不存在: chapter={chapter_id} version={source_version_id}: {exc}"
                ) from exc

            # 先冻结并验真 DB/磁盘版本目录，再读取其余 prompt 合同。这样目录或
            # 正文漂移仍在最早的零 Provider 边界报原有 typed 错误；计划编译则
            # 仍位于取得状态改源权/租约和 Provider 调用之前。
            if budget is not None:
                self._validate_revise_source_checkpoint(
                    workspace_id=workspace_id,
                    chapter_id=chapter_id,
                    paths=paths,
                    budget=budget,
                    source_version_id=source_version_id,
                    source_content=chapter_text,
                )

            constraint_results = self._load_reviser_constraint_results(
                workspace_id=workspace_id,
                chapter_id=chapter_id,
                source_version_id=source_version_id,
            )

            # R9 可执行约束计划：显式产品 Revise 与 Orchestrator 内 revise 必须
            # 消费同一个机械合同。全部输入在 Provider/租约状态变更前读取；不新增
            # Agent、DB 字段或网络调用。
            world_contract = self._load_world_contract(
                paths,
                required=self._outline_contract_required(workspace_id, paths),
            )
            if (
                world_contract is not None
                and card.setting_contract_hash != world_contract.source_hash
            ):
                raise ChapterCardSettingStaleError(
                    chapter_id=chapter_id,
                    card_contract_hash=card.setting_contract_hash,
                    current_contract_hash=world_contract.source_hash,
                )
            self._validate_card_dramatic_source_preflight(
                workspace_id=workspace_id,
                paths=paths,
                card=card,
            )

            core_state_projection = None
            if card.core_state_contract_version == 1:
                if self._session_factory is None:
                    raise ChapterServiceError(
                        "显式 Revise 的 core_state v1 合同缺少 session_factory"
                    )
                core_state_projection = CoreStateService(
                    session_factory=self._session_factory
                ).validate_card_contract(
                    workspace_id=workspace_id,
                    card=card,
                )

            character_records = None
            cross_chapter_baseline_fingerprint = None
            cross_chapter_baseline_chapter_id = None
            workspace_default_voice: (
                Literal["third_person", "first_person", "free"] | None
            ) = None
            persisted_v1_fingerprint = None
            if self._session_factory is not None:
                plan_session = self._session_factory(workspace_id)
                try:
                    character_records = list_characters_by_workspace(
                        plan_session, workspace_id
                    )
                    v1_row = plan_session.get(ChapterVersionORM, (chapter_id, 1))
                    if v1_row is not None:
                        persisted_v1_fingerprint = v1_row.style_fingerprint
                    (
                        cross_chapter_baseline_fingerprint,
                        cross_chapter_baseline_chapter_id,
                        workspace_default_voice,
                    ) = _resolve_cross_chapter_voice_context(
                        preread_session=plan_session,
                        workspace_id=workspace_id,
                        chapter_card=card,
                    )
                finally:
                    plan_session.close()
            if persisted_v1_fingerprint is None:
                try:
                    persisted_v1_fingerprint = compute_style_fingerprint(
                        versioning.read_version_content(chapter_id, 1)
                    )
                except (OSError, UnicodeDecodeError, ChapterVersioningError):
                    persisted_v1_fingerprint = None

            cross_chapter_voice = (
                resolve_cross_chapter_voice_constraint_spec(
                    baseline_fingerprint=cross_chapter_baseline_fingerprint,
                    baseline_chapter_id=cross_chapter_baseline_chapter_id,
                    workspace_default_voice=workspace_default_voice,
                    chapter_card=card,
                )
                if workspace_default_voice is not None
                else None
            )
            executable_constraint_plan = compile_executable_constraint_plan(
                chapter_card=card,
                stage="revise",
                setting_contract=world_contract,
                character_records=character_records,
                cross_chapter_voice=cross_chapter_voice,
                rewrite_style=resolve_rewrite_style_constraint_spec(
                    v1_fingerprint=persisted_v1_fingerprint,
                    cross_chapter_baseline_fingerprint=(
                        cross_chapter_baseline_fingerprint
                    ),
                ),
                core_state_projection=core_state_projection,
            )
            revision_contract = build_reviser_revision_contract(
                source_content=chapter_text,
                source_version_id=source_version_id,
                executable_constraint_plan=executable_constraint_plan,
                issues=issues,
                validator_failures=(),
                constraint_results=constraint_results,
            )

            operation_lease: ChapterOperationLease | None = None
            if budget is not None:
                current_state = self._read_chapter_state(workspace_id, chapter_id)
                if current_state is None:
                    raise ChapterServiceError(
                        f"章节状态缺失，无法安全取得 revise 改源权: {chapter_id}"
                    )
                operation_lease = self._acquire_chapter_operation_state(
                    workspace_id,
                    chapter_id,
                    expected=budget,
                    target_state=ChapterState.REVISING.value,
                    operation="revise",
                )

            # P3-B-06:revise 局部修订路径同样走 router(若给定);否则
            # fallback legacy ``self._client`` + ``self._model``。
            # P3-B-07:同 ``_orchestrate`` 路径,router + session_factory 都
            # 在位时外包 cost tracking。revise 不走 orchestrator 闭环,但
            # 入口前序 ``write`` 已建 chapters 行,recorder 可正常 update。
            router = (
                self._router_factory(workspace_id)
                if self._router_factory is not None
                else None
            )
            if router is not None and self._session_factory is not None:
                session_factory_local = self._session_factory  # closure narrow
                router = wrap_router_with_cost_tracking(
                    router,
                    workspace_id=workspace_id,
                    chapter_id=chapter_id,
                    session_factory=lambda: session_factory_local(workspace_id),
                )
            if operation_lease is not None and self._operation_lease_store is not None:
                operation_lease = self._operation_lease_store.mark_phase(
                    operation_lease,
                    phase="reviser_may_call_provider",
                )
            reviser_out = revise_chapter(
                router=router,
                client=self._client,
                model=self._model,
                chapter_text=chapter_text,
                issues=issues,
                chapter_card=card,
                constraint_results=constraint_results,
                executable_constraint_plan=executable_constraint_plan,
                revision_contract=revision_contract,
                core_state_projection=core_state_projection,
            )
            deterministic_duplicates = [
                finding
                for finding in find_localized_adjacent_duplicates(
                    contract=revision_contract,
                    candidate_content=reviser_out.content,
                )
                if finding.match_kind != "high_overlap"
            ]
            if deterministic_duplicates:
                # 显式产品 Revise 本身不含 Reviewer 调用，不能为高相似候选
                # 偷加一次审稿。只有 exact/规范化相同/完整包含这三类纯代码
                # 证据在落版前拒绝；Provider 物理账照常保留，不自动 retry。
                _log.warning(
                    "explicit_revise_adjacent_duplicate_rejected",
                    workspace_id=workspace_id,
                    chapter_id=chapter_id,
                    source_version_id=source_version_id,
                    match_kinds=[
                        item.match_kind for item in deterministic_duplicates
                    ],
                    changed_anchor_ids=[
                        item.changed_anchor_id for item in deterministic_duplicates
                    ],
                )
                raise ProseRevisionContractError(
                    "显式产品 Revise 生成了可代码确认的相邻长段重复，"
                    "已在新版本持久化前拒绝"
                )
            new_version = versioning.write_version(
                chapter_id,
                reviser_out.content,
                creation_type="revise",
                model_used=reviser_out.model_used,
            )
            # P4-B-02:revise 不走 Orchestrator,但落 chapter_versions 表与
            # ``_orchestrate`` 路径保持一致(P3-acceptance §观察 3 "P3+ 落库"
            # 字面覆盖所有版本路径,不限于状态机闭环)。``session_factory=None``
            # 时静默 no-op；R5 起 FK/commit 失败由 helper fail closed，
            # 禁止返回未进入版本权威账本的免费正文。
            if self._session_factory is not None:
                session_factory_local = self._session_factory  # closure narrow
                persist_chapter_version_safely(
                    lambda: session_factory_local(workspace_id),
                    chapter_id=chapter_id,
                    version=new_version,
                )
            if operation_lease is not None and self._operation_lease_store is not None:
                self._operation_lease_store.require_held(operation_lease)
                operation_lease = self._operation_lease_store.mark_phase(
                    operation_lease,
                    phase="operation_complete",
                )
                if attempt_task_id is None and (
                    existing_scope is None or existing_scope.run_id is None
                ):
                    self._operation_lease_store.release(operation_lease)
            return new_version

    # ---- 列表查询 ----------------------------------------------------------

    def list_chapters(self, workspace_id: str) -> list[ChapterListEntry]:
        """列出工作区下所有章节(扫 ``data/cards/*.json``)。

        每张章节卡解析后合并 ``status`` 推断结果,按 ``chapter_number`` 升序
        返。**单张坏卡(JSON 损坏 / schema 不符)structlog warn 跳过,不阻塞
        整个列表**——列表端点用于工作区看板与章节列表页,被一张坏卡 fail 整页
        体验更差;``GET /chapters/{id}/card`` 单卡端点仍按存在性 / 合法性双轨
        抛 422 让维护人显式修复(行为分开,P1-B-25 决策)。

        - 工作区不存在 → ``WorkspaceServiceError``
        - ``cards_dir`` 不存在 / 空目录 → 返空列表(章节卡尚未导入是合法状态)
        """
        paths = self._validate_workspace(workspace_id)
        cards_dir = paths.cards_dir
        if not cards_dir.is_dir():
            return []

        entries: list[ChapterListEntry] = []
        for card_path in sorted(cards_dir.glob("*.json")):
            try:
                data = json.loads(card_path.read_text(encoding="utf-8"))
                card = ChapterCard.model_validate_storage(data)
            except (json.JSONDecodeError, ValidationError) as exc:
                _log.warning(
                    "list_chapters_skip_invalid_card",
                    workspace_id=workspace_id,
                    card_path=str(card_path),
                    error=str(exc),
                )
                continue

            info = self.status(workspace_id, card.chapter_id)
            mtime = datetime.fromtimestamp(card_path.stat().st_mtime, tz=timezone.utc)
            entries.append(
                ChapterListEntry(
                    chapter_id=card.chapter_id,
                    title=card.title,
                    chapter_number=card.chapter_number,
                    volume_id=card.volume_id,
                    status=info.status,
                    latest_version_id=info.latest_version_id,
                    has_final=info.has_final,
                    version_count=info.version_count,
                    last_modified_at=mtime.isoformat().replace("+00:00", "Z"),
                )
            )

        entries.sort(key=lambda e: e.chapter_number)
        return entries

    # ---- 状态查询 + 版本切换 ----------------------------------------------

    def status(self, workspace_id: str, chapter_id: str) -> ChapterStatusInfo:
        """章节状态查询(P2-B-17 起 7 档)。

        ``status`` 取值优先级:

        1. **chapters 表行存在**(write/rewrite 跑过 → upsert 入口 + state
           callback 写入)→ ``chapters.status`` 直接返,反映状态机当前节点
        2. **行不存在 / ``session_factory=None``**(尚未跑过 / P1 兼容模式)→
           ``_fs_derive_state``:有 ``final.md`` → ``approved``,无 final →
           ``drafting``(P1 ``not_started`` / ``unknown`` 在 7 档中折算到
           状态机起点 ``drafting``)

        ``latest_version_id`` / ``has_final`` / ``version_count`` 始终从文件
        系统推:版本/最终文件就是事实源,不复制到 chapters 表(P2 阶段
        chapters 表只追踪 ``status`` 状态机节点,版本元数据走
        ``chapter_versions`` 表 P3+ 落库)。

        - 工作区不存在 → ``WorkspaceServiceError``
        """
        paths = self._validate_workspace(workspace_id)
        versioning = ChapterVersioning(paths)

        # next_version_id - 1 = 当前最新已存在版本号(空目录返回 0)
        version_count = versioning.next_version_id(chapter_id) - 1
        latest_version_id = version_count if version_count > 0 else None

        final_path = paths.chapter_version_file(
            chapter_id, ChapterVersioning.FINAL_FILENAME
        )
        has_final = final_path.is_file()

        db_state = self._read_chapter_state(workspace_id, chapter_id)
        status: ChapterStateValue = (
            db_state
            if db_state is not None
            else self._fs_derive_state(has_final=has_final, version_count=version_count)
        )

        return ChapterStatusInfo(
            chapter_id=chapter_id,
            status=status,
            latest_version_id=latest_version_id,
            has_final=has_final,
            version_count=version_count,
            escalation_details=self._read_escalation_details(workspace_id, chapter_id),
        )

    def _sync_escalation_details_safely(
        self,
        workspace_id: str,
        chapter_id: str,
        *,
        details: dict[str, Any] | None,
    ) -> None:
        """同步 ``chapters.escalation_details`` 列(v1.1 acceptance 修复2 / Fix D)。

        ``details`` 语义 = "本次编排结束后该列应有的值"(None = 清除)。
        None→None 跳过写;无 session / 无行 / 异常 → swallow + log(详情属
        附加信息,失败不应影响 :write 主流程结果)。
        """
        session = self._open_session(workspace_id)
        if session is None:
            return
        try:
            row = session.get(ChapterORM, chapter_id)
            if row is None or row.workspace_id != workspace_id:
                return
            if row.escalation_details == details:
                return
            row.escalation_details = details
            session.commit()
        except Exception:  # noqa: BLE001 — swallow per 辅助任务先例
            session.rollback()
            _log.warning(
                "escalation_details_sync_failed",
                workspace_id=workspace_id,
                chapter_id=chapter_id,
            )
        finally:
            session.close()

    def _read_escalation_details(
        self, workspace_id: str, chapter_id: str
    ) -> dict[str, Any] | None:
        """读 ``chapters.escalation_details``;无行 / 无 session / 异常 → None。

        v1.1 acceptance 修复2 / Fix D:供 ``status()`` 透出"为什么进人工介入"
        (孤儿章人工裁决 UI 展示)。读失败 swallow(状态查询是高频轻接口,
        附加详情拿不到不应让整个 status 报错)。
        """
        session = self._open_session(workspace_id)
        if session is None:
            return None
        try:
            row = session.get(ChapterORM, chapter_id)
            if row is None or row.workspace_id != workspace_id:
                return None
            return row.escalation_details
        except Exception:  # noqa: BLE001 — swallow,详情属附加信息
            _log.warning(
                "escalation_details_read_failed",
                workspace_id=workspace_id,
                chapter_id=chapter_id,
            )
            return None
        finally:
            session.close()

    def read_version_content(
        self, workspace_id: str, chapter_id: str, version_id: int
    ) -> str:
        """读 ``v{version_id}.md`` 正文。

        ``ChapterVersioningError``(版本文件不存在等)自然冒泡;P1-B-17 全局
        翻译表已映射为 ``CHAPTER_VERSION_CONFLICT (409)``,路由层无需 try/except。

        - 工作区不存在 → ``WorkspaceServiceError``
        - 版本文件不存在 → ``ChapterVersioningError``
        """
        paths = self._validate_workspace(workspace_id)
        versioning = ChapterVersioning(paths)
        return versioning.read_version_content(chapter_id, version_id)

    def switch_version(
        self, workspace_id: str, chapter_id: str, version_id: int
    ) -> Path:
        """把指定版本的 ``v{N}.md`` 拷贝成 ``final.md``,返回 ``final.md`` 路径。

        等价于"回退/前进 final 指针到指定版本"。直接调
        ``ChapterVersioning.mark_final``。

        ``ChapterVersioningError``(版本文件不存在等)自然冒泡:语义本质是
        "版本问题",P1-B-17 翻译表已映射为 ``CHAPTER_VERSION_CONFLICT (409)``。
        不再外包成 ``ChapterServiceError("版本切换失败...")``——那会让消息含
        "不存在"被翻译成 ``CHAPTER_NOT_FOUND (404)``,丢失"版本 vs 章节"
        语义精度。本设计与 ``read_version_content`` 一致(P1-B-19 修正)。

        - 工作区不存在 → ``WorkspaceServiceError``
        - 源版本文件不存在 → ``ChapterVersioningError``
        """
        paths = self._validate_workspace(workspace_id)
        versioning = ChapterVersioning(paths)
        # 缺源是确定零写入的普通 409，必须在租约/phase 前发现，不能把一次
        # 错版本点击变成永久安全锁。
        source_path = paths.chapter_version_file(chapter_id, f"v{version_id}.md")
        if not source_path.is_file():
            raise ChapterVersioningError(
                f"无法标记 final:源版本文件不存在 {source_path}"
            )
        budget = self._read_authoritative_budget(workspace_id, chapter_id)
        operation_lease = self._reserve_chapter_operation_lease(
            workspace_id=workspace_id,
            chapter_id=chapter_id,
            operation="switch-version",
            source_version=budget.current_version,
        )
        session = self._open_session(workspace_id)
        try:
            if session is not None:
                if self._operation_lease_store is not None:
                    self._operation_lease_store.begin_immediate(session)
                    source_row = session.get(
                        ChapterVersionORM,
                        (chapter_id, version_id),
                    )
                    try:
                        source_content = source_path.read_text(encoding="utf-8")
                    except OSError as exc:
                        raise ChapterVersioningError(
                            f"无法读取待切换版本: {source_path}: {exc}"
                        ) from exc
                    if source_row is None or source_row.content != source_content:
                        raise RewriteCheckpointRecoveryError(
                            "switch-version 的磁盘版本与不可变 DB 版本不一致，"
                            "已阻止切换 final"
                        )
                row = session.get(ChapterORM, chapter_id)
                if row is not None:
                    if row.workspace_id != workspace_id:
                        raise RewriteCheckpointRecoveryError(
                            "switch-version 的章节 workspace 投影不匹配"
                        )
                    if (
                        row.status
                        in (
                            ChapterState.APPROVED.value,
                            ChapterState.PUBLISHED.value,
                        )
                        or row.ready_for_next_chapter_at is not None
                    ):
                        raise RewriteCheckpointRecoveryError(
                            "已 approved/published 或已落 ready 的章节不能直接"
                            "切换 final 版本；请先走产品 Rewind/人工编辑流程，"
                            "避免旧 ledger、富记忆和正文指向不同版本"
                        )
                    _require_memory_enrichment_checkpoint_safe(
                        row=row,
                        chapter_id=chapter_id,
                        evidence=["operation=switch-version"],
                        block_running=True,
                    )
                if (
                    operation_lease is not None
                    and self._operation_lease_store is not None
                ):
                    operation_lease = self._operation_lease_store.mark_phase_in_session(
                        session,
                        operation_lease,
                        phase=f"local_final_mutation:v{version_id}",
                    )
                session.commit()
        except Exception:
            if session is not None:
                session.rollback()
            if (
                operation_lease is not None
                and operation_lease.phase == "reserved_before_provider"
                and self._operation_lease_store is not None
            ):
                self._operation_lease_store.release_if_phase(
                    operation_lease,
                    expected_phase="reserved_before_provider",
                )
            raise
        finally:
            if session is not None:
                session.close()
        final_path = versioning.mark_final(chapter_id, version_id)
        if operation_lease is not None and self._operation_lease_store is not None:
            operation_lease = self._operation_lease_store.mark_phase(
                operation_lease,
                phase="operation_complete",
            )
            self._operation_lease_store.release(operation_lease)
        return final_path
