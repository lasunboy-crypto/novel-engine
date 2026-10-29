"""流水线运行控制服务(P6-B-02 内存版 → P6-B-03 SQLite 持久化)。

对齐 docs/plan/p6_plan.md §"P6-B-02" + §"P6-B-03" + 决策 4=B(P6-B-03 持久化半)
+ API_CONTRACT §4.5 + §5.3 字面。

设计要点
========

1. **state SQLite 持久化**(P6-B-03 决策 4=B 完整版):每工作区单 active run
   作为 ``pipeline_runs`` 表行(state IN ('running','paused'));历史 run
   保留在同表(state IN ('stopped','error'));``GET /status`` 走"active 行
   优先 → 否则取最近 1 行 ORDER BY updated_at DESC → 否则 idle 占位"逻辑。
   进程重启不丢 run state(对齐 PROJECT_SPEC §11 字面"长流程中断丢数据 →
   状态持久化")。

2. **scope 4 档解析**(对齐 plan §"P6-B-02" + 自决项 6):
   - ``single``:返 ``[start_chapter_id]``;
   - ``range``:基于全书章节序列(VolumeORM × chapter_count 全局展开)取
     ``[start_chapter_id, end_chapter_id]`` 闭区间(lex 序与 ``chapter_number``
     一致,对齐 P5-B-06 ``_collect_first_n_chapters`` 同模式);
   - ``volume``:返 ``volume_id`` 卷下所有章节;
   - ``until``:从 ``start_chapter_id`` 到 ``end_chapter_id`` 含;
     ``end_chapter_id=None`` → 默认到 ``start_chapter_id`` 所在卷末。

3. **execute 后台循环**(v2 w1-s1 起补全 Run 循环语义,闭合 v2_plan §2.2
   缺口 1/3/4):由路由层 ``BackgroundTasks`` 在响应已发出后调用;循环内
   每章:① SELECT 当前 active run(被 pause/stop 时退出;选章跳过
   completed ∪ skipped ∪ **章终态 approved/published** — v2 w1-s2 防重写,
   新 run / 重启 resume 场景不重写已过审章)② publish ``chapter_started``
   ③ **上一章富记忆成功门**(在任何下一章卡/正文 Provider 前检查)
   ④ **写前卡保障**
   (缺卡 → ``generate_next_card`` + 自动 approve;rejected 卡
   不绕过)⑤ 调 ``chapter_service.write()``；正式 Run 内沿用既有富记忆
   Worker 但同步等到 succeeded ⑥ 撞
   ``ChapterInvalidatedNeedsUnblockError`` → 自动 pause + 不撤回已完成章节
   ⑦ 其他异常 → 按 ``failure_policy`` 路由(stop=error / skip=记账继续 /
   retry=1 次后回落 stop)⑧ **写后孤儿章感知**:``result.final_state ==
   HUMAN_INTERVENTION`` → 按 ``failure_policy`` 路由(stop = auto_pause
   reason=``chapter_escalated`` + 不 append completed)⑨ ready 与富记忆均成功 → UPDATE
   current_chapter + completed_chapter_ids JSON + publish
   ``chapter_finished``。全章处理完(完成 ∪ 跳过)→ state=stopped +
   completed_at + publish ``pipeline_stopped`` reason=completed。

4. **6 WS event**(对齐 plan §"P6-B-02"字面 + API_CONTRACT §5.3):
   ``pipeline_started`` / ``pipeline_paused`` / ``pipeline_resumed`` /
   ``pipeline_stopped`` / ``chapter_started`` / ``chapter_finished``,全部
   推到 ``pipeline.events`` 频道(跨工作区共享,data 含 ``workspace_id``)。

5. **3 异常类**(对齐 plan §"P6-B-02"字面 3 错误码):
   - ``PipelineAlreadyRunningError`` → 409 ``PIPELINE_ALREADY_RUNNING``
   - ``PipelineNotRunningError`` → 409 ``PIPELINE_NOT_RUNNING``
   - ``PipelineInvalidScopeError`` → 422 ``PIPELINE_INVALID_SCOPE``

6. **线程安全**:``self._lock`` 保护"SELECT state → UPDATE state"原子序列,
   防 Python 端 check-then-act 竞争;SQLite 自身串行化写,lock 主要防多
   端点 / 后台循环同 workspace 并发触发。SQLite 行是真相源。

7. **lifespan 启动 hook**:``mark_running_as_interrupted_on_startup``
   (v2 w1-s2 改判,原 ``mark_running_as_error_on_startup``)在 FastAPI
   ``lifespan`` 中扫所有工作区,把残留的 ``state='running'`` 行按数据完整
   性改判:``chapter_ids`` 非空(可定位断点续跑)→ ``interrupted``(可
   ``:resume``);不完整(理论不可达兜底)→ ``error``。两者均带
   ``error_details={"reason": "engine_restarted_mid_run"}``(不自动续跑 —
   user 手动 resume)。

8. **PipelineRunSnapshot Pydantic 不变**:仍作为对外 API 视图(snapshot
   返回值);ORM ↔ Snapshot 通过 ``_snapshot_from_orm`` / ``_apply_snapshot_to_orm``
   双向转换(字段命名 P6-B-02 已字面对齐 P6-B-03 表字段,无重命名漂移)。

9. **章级看门狗**(v2 w1-s3,闭合 v2_plan §2.2 缺口 5):``write()`` 同步
   阻塞且 Python 线程不可安全强杀 → **不做强杀,只做心跳超时标记**。循环
   每轮选章时在内存心跳表(``self._heartbeats``,workspace_id →
   ``_LoopHeartbeat``)登记本章计时起点;独立 daemon 线程
   (``_watchdog_loop``,lazy 启动)周期扫表,发现单章处理超
   ``PipelineWatchdogConfig.chapter_timeout_seconds``(config.yaml
   ``pipeline:`` 段,默认 2400s)→ 把 run 翻 ``interrupted`` +
   ``error_details={"reason": "chapter_watchdog_timeout"}``。**为何独立
   线程而非循环内检查点**:单章内卡死(LLM 调用挂住)时循环线程自身被
   困在 ``write()``,循环内检查点永远执行不到 — 而这正是缺口 5 的靶场景。
   心跳表内存级即可(无 DB 列):进程死亡场景由 w1-s2
   ``mark_running_as_interrupted_on_startup`` 兜底,watchdog 只管"进程
   活着但单章卡死"。翻 ``interrupted`` 复用 w1-s2 语义:resume 可断点
   续跑,卡死章留非终态由 ``reset_stale`` 自愈 — 两机制设计自洽。

10. **single-flight 循环互斥**(v2 w1-s3 顺手修 pre-existing 双循环缺陷):
    同 workspace 同时只允许一个 execute 循环线程。``:resume`` 端点会经
    BackgroundTasks 重调 ``execute``,而老循环可能仍困在 ``write()``
    (watchdog 翻 interrupted / pause 后均可能)— 不互斥则新老循环并发写
    同一章(数据竞争)。同 run_id 重入 → 静默返回(resume 翻 running 已
    让老循环继续,语义即"接上既有循环");异 run_id 占用(stop 后立刻
    start 新 run)→ 新 run 翻 ``interrupted`` +
    ``error_details={"reason": "pipeline_loop_conflict"}``(可见可处置,
    不静默假死)。

11. **Run 级成本预检**(v2 w1-s3,P4-B-08 留观闭账):循环每章启动前调
    ``cost_guard.trigger_cost_guard_check``(预读本工作区
    ``metrics_daily`` 当日 ``total_cost_usd``,对齐章级
    ``ChapterService._run_gates`` 同款读法);``action=="pause"`` 超限 →
    ``_auto_pause(reason="cost_guard_exceeded")`` 停跑。**不走失败策略
    路由**:成本超限是 Run 级全局态而非单章故障 — skip 会连环跳过全章后
    把 run 标 completed(假完成灾难),retry 立即重撞;对齐 invalidated
    不走策略的先例。``write()`` 内章级 gate 抛 ``CostGuardTriggered`` 时
    同语义特判(两层 gate 行为一致)。paused 可 resume(次日成本日清零 /
    调高限额后续跑)。``cost_guard_config=None`` 表示不启用(对齐
    ``ChapterService`` 同语义;CLI / legacy 测试不受影响)。
"""

from __future__ import annotations

import secrets
import threading
import time
from collections.abc import Callable, Iterable
from dataclasses import dataclass
from datetime import UTC, datetime
from pathlib import Path
from typing import TYPE_CHECKING, Any, Literal

import structlog
import yaml
from pydantic import BaseModel, ConfigDict, Field
from sqlalchemy import func, select, update
from sqlalchemy.orm import Session

from src.api.websocket.channels import pipeline_events_channel
from src.core.db.models import (
    AttemptLedgerORM,
    ChapterCardORM,
    ChapterOperationLeaseORM,
    ChapterORM,
    ChapterVersionORM,
    PipelineRunORM,
    ReviewResultORM,
    VolumeORM,
)
from src.core.llm.base import LLMAuthError
from src.core.models.chapter_card import chapter_seq_from_id
from src.core.orchestrator.chapter_orchestrator import RewriteCheckpointRecoveryError
from src.core.state_machine.states import ChapterState
from src.core.workspace.workspace_paths import WorkspacePaths
from src.services.attempt_ledger import (
    DEFAULT_PROVIDER_CALL_HARD_CAP,
    AttemptLedgerError,
    AttemptLedgerStore,
    ProviderCallCapExceeded,
    ProviderCostCapExceeded,
    ProviderUsageCostSummary,
    bind_attempt_scope,
    current_attempt_scope,
)
from src.services.chapter_rewind_service import (
    ChapterInvalidatedNeedsUnblockError,
)
from src.services.chapter_operation_lease import (
    ChapterOperationLeaseError,
    ChapterOperationLeaseStore,
)
from src.services.chapter_service import (
    ChapterService,
    ContinuityCheckpointCorruptionError,
    ContinuityLedgerNotReadyError,
    MemoryEnrichmentNotReadyError,
)
from src.services.core_state_service import CoreStateService, CoreStateServiceError
from src.services.memory_enrichment_checkpoint import (
    MemoryEnrichmentCheckpointShapeError,
    memory_enrichment_requires_manual_recovery,
    validate_memory_enrichment_checkpoint_shape,
)
from src.services.workspace_service import WorkspaceService

if TYPE_CHECKING:
    from src.api.websocket.manager import WebSocketManager
    from src.services.chapter_card_service import ChapterCardService


_log = structlog.get_logger(__name__)


SessionFactory = Callable[[str], Session]


PipelineScope = Literal["single", "range", "volume", "until"]
PipelineState = Literal["idle", "running", "paused", "interrupted", "stopped", "error"]
PipelineFailurePolicy = Literal["stop", "skip", "retry"]
"""失败策略 3 档(v2 w1-s1,对齐 ``PIPELINE_FAILURE_POLICY_VALUES``):

- ``stop``(默认):孤儿章 / rejected 卡 → auto_pause(人工解决后可
  resume);写章异常 / 卡生成失败 → state=error。
- ``skip``:失败章记入 ``skipped_chapter_ids`` 继续下一章。
- ``retry``:同章重试 1 次,仍失败 → 按 ``stop`` 处置。
"""


_ACTIVE_STATES: tuple[str, ...] = ("running", "paused", "interrupted")
"""active run 状态集合(:start 检测已有 active 时阻塞 409;:pause/:resume
判定;:stop 允许 + error 也可以 :stop)。

v2 w1-s2 扩 ``interrupted``:重启中断的 run 算"未了结"——挡新 :start
(防两行可 resume 并存 → ``one_or_none`` 多行炸;user 须显式 resume 续跑
或 stop 放弃),可被 :resume 断点续跑 / :stop 终结;:pause 对它幂等返回
(已不在跑,保留 interrupted 语义标记不翻 paused)。"""


_RECOVERABLE_ERROR_TYPE = "ContinuityLedgerNotReadyError"
"""可从现有产品 checkpoint 原 run 恢复的窄错误类型。"""

_RECOVERABLE_CARD_ERROR_REASON = "card_generation_failed"
"""章节卡经产品审核修复后可恢复原 run 的写前失败原因。"""

_CARD_REPAIR_RECOVERY_KIND = "chapter_card_repair"
"""只用于 status ``error_details`` 的派生恢复种类，不写回 DB。"""

_PROVIDER_REVIEW_RECOVERY_KIND = "provider_review_checkpoint"
"""Provider 在 Reviewer 前拒绝后，只从已落库正文的审稿断点恢复。"""

_PROVIDER_REVIEW_TRANSPORT_ERROR_TYPES = frozenset(
    {"RemoteProtocolError", "ReadTimeout", "SSEStreamError"}
)
_PROVIDER_REVIEW_ERROR_TYPES = (
    frozenset({"LLMAuthError", "LLMAccessError"})
    | _PROVIDER_REVIEW_TRANSPORT_ERROR_TYPES
)
"""认证/权限错误及有终态 Reviewer 账本可核验的连接中断。"""

_CORE_STATE_ERROR_TYPE = "CoreStateConflictError"
"""正文已批准但核心状态本地提交 fail closed 的原始错误类型。"""

_CORE_STATE_RECOVERY_KIND = "core_state_initial_baseline"
"""只从已重批卡与同源正文前基线恢复 approved checkpoint。"""

_CORE_STATE_TRANSITION_RECOVERY_KIND = "core_state_transition_checkpoint"
"""非首章只从同源批准 transition/正文候选的本地提交断点恢复。"""

_CONTINUITY_BRIDGE_RETRY_ERROR_MARKER = "CardContinuityBridgeRequiredError"
"""允许未批准草稿从同一 run 重新进入确定性 bridge 校验的错误证据。"""


# =============================================================================
# 章级看门狗(v2 w1-s3)
# =============================================================================


_WATCHDOG_TICK_SECONDS = 30.0
"""watchdog daemon 线程扫描间隔(秒)。预算 40min 量级,30s 粒度足够;
内部常量不进配置(单测直调 ``_watchdog_tick(now)`` 不依赖线程节奏)。"""


class PipelineWatchdogConfig(BaseModel):
    """``pipeline:`` 配置段(config.yaml 顶层,v2 w1-s3 引入)。

    字段默认值对齐 v2_plan §4 w1-s3 卡字面(~40min ≈ 13 调用 × 600s 单
    调用超时的保守折算)。
    """

    model_config = {"extra": "forbid"}

    chapter_timeout_seconds: float = Field(
        default=2400.0,
        description=(
            "章级看门狗预算(秒);单章处理(含写前卡生成 + 写章全链路)"
            "超时 → run 翻 interrupted。<= 0 表示禁用看门狗。"
        ),
    )


def load_pipeline_watchdog_config(
    path: Path | None = None,
) -> PipelineWatchdogConfig:
    """从 yaml 加载 ``pipeline:`` 段(对齐 ``load_cost_guard_config`` 容错
    语义:文件 / 段缺失走字面默认不 fail-fast;字段类型错仍抛
    ``ValidationError`` 不静默吞)。

    :param path: yaml 路径;``None`` → ``settings.config_yaml_path``
        (默认仓库根 ``config.yaml``)。
    """
    if path is None:
        # 延迟 import 避开循环依赖(对齐 cost_guard.load_cost_guard_config)
        from src.config import settings

        path = settings.config_yaml_path
    if not path.exists():
        return PipelineWatchdogConfig()
    with path.open("r", encoding="utf-8") as fp:
        raw: dict[str, Any] = yaml.safe_load(fp) or {}
    if not isinstance(raw, dict):
        return PipelineWatchdogConfig()
    section = raw.get("pipeline")
    if not isinstance(section, dict):
        return PipelineWatchdogConfig()
    return PipelineWatchdogConfig.model_validate(section)


@dataclass
class _LoopHeartbeat:
    """execute 循环心跳条目(``PipelineService._heartbeats`` 值,内存级)。

    生命周期:``execute`` 入口登记(single-flight 闸,``chapter_id=None``)
    → 循环每轮选章后覆盖更新(计时起点重置)→ 循环退出 finally 删除。
    ``self._lock`` 保护读写。

    - ``chapter_id=None``:循环已进入但未在处理章(入口 / 章间空隙),
      watchdog 不计超时。
    - ``started_monotonic``:``time.monotonic()`` 计时起点(不受系统时钟
      调整影响)。
    - ``watchdog_flagged``:已翻态标记 — watchdog 翻 interrupted 后置位,
      防后续 tick 对同一卡死章重复翻态 / 重复查 DB;循环推进到下一章时
      随覆盖更新自然清零。
    """

    run_id: str
    chapter_id: str | None
    started_monotonic: float
    watchdog_flagged: bool = False


# =============================================================================
# 异常类型
# =============================================================================


class PipelineServiceError(Exception):
    """pipeline_service 通用错误基类(翻译 INTERNAL_ERROR 500 兜底)。"""


class PipelineAlreadyRunningError(PipelineServiceError):
    """``:start`` 时 workspace 已有 active run。

    路由层翻译为 ``PIPELINE_ALREADY_RUNNING (409)``。details 含
    ``workspace_id`` / ``active_run_id`` / ``active_state`` 帮前端定位。
    """

    def __init__(
        self,
        *,
        workspace_id: str,
        active_run_id: str,
        active_state: PipelineState,
    ) -> None:
        self.workspace_id = workspace_id
        self.active_run_id = active_run_id
        self.active_state = active_state
        super().__init__(
            f"workspace {workspace_id} 已有 active run "
            f"run_id={active_run_id} state={active_state}"
        )


class PipelineNotRunningError(PipelineServiceError):
    """``:pause`` / ``:resume`` / ``:stop`` 时 workspace 无 active run。

    路由层翻译为 ``PIPELINE_NOT_RUNNING (409)``。details 含 ``workspace_id``
    + 期望操作 ``action``(``pause`` / ``resume`` / ``stop``)。
    """

    def __init__(self, *, workspace_id: str, action: str) -> None:
        self.workspace_id = workspace_id
        self.action = action
        super().__init__(f"workspace {workspace_id} 无 active run,无法执行 {action}")


class PipelineInvalidScopeError(PipelineServiceError):
    """``:start`` 时 scope 与字段映射不一致 / 解析后空列表 / chapter 不存在。

    路由层翻译为 ``PIPELINE_INVALID_SCOPE (422)``。details 含
    ``scope`` / ``reason`` / 缺失字段名供前端表单定位。
    """

    def __init__(
        self,
        *,
        scope: str,
        reason: str,
        details: dict[str, Any] | None = None,
    ) -> None:
        self.scope = scope
        self.reason = reason
        self.invalid_details = details or {}
        super().__init__(f"scope={scope} 不合法: {reason}")


# =============================================================================
# 对外快照模型(字段命名严格对齐 PipelineRunORM)
# =============================================================================


class PipelineRunSnapshot(BaseModel):
    """active run 快照(P6-B-03 起 SQLite 持久化背书)。

    字段命名严格对齐 ``PipelineRunORM`` 列(P6-B-02 起字面预约,P6-B-03
    持久化无重命名漂移)。Snapshot 是 ORM 行的"对外不变视图":service 返
    snapshot 给路由层,路由层再 model_dump 出 JSON 响应。
    """

    model_config = ConfigDict(extra="forbid")

    run_id: str
    workspace_id: str
    state: PipelineState
    scope: PipelineScope
    start_chapter_id: str | None
    end_chapter_id: str | None
    volume_id: str | None
    profile_override: str | None
    chapter_ids: list[str]
    current_chapter: str | None
    completed_chapter_ids: list[str]
    pause_reason: str | None
    error_details: dict[str, Any] | None
    failure_policy: PipelineFailurePolicy = "stop"
    skipped_chapter_ids: list[str] = []
    provider_call_cap: int = DEFAULT_PROVIDER_CALL_HARD_CAP
    provider_call_count: int = 0
    transport_retry_count: int = 0
    pipeline_attempt_count: int = 0
    upstream_generation_attempt_count: int = 0
    created_at: datetime
    updated_at: datetime

    @property
    def progress(self) -> float:
        """进度 0.0-1.0(``completed_chapter_ids`` 数 / ``chapter_ids`` 总数)。"""
        total = len(self.chapter_ids)
        if total == 0:
            return 0.0
        return len(self.completed_chapter_ids) / total


def _idle_snapshot(workspace_id: str) -> PipelineRunSnapshot:
    """构造 idle 占位 snapshot(``GET /status`` 无任何行时返此)。"""
    now = datetime.now(UTC)
    return PipelineRunSnapshot(
        run_id="",
        workspace_id=workspace_id,
        state="idle",
        scope="single",  # 占位,前端按 state=idle 忽略 scope
        start_chapter_id=None,
        end_chapter_id=None,
        volume_id=None,
        profile_override=None,
        chapter_ids=[],
        current_chapter=None,
        completed_chapter_ids=[],
        pause_reason=None,
        error_details=None,
        failure_policy="stop",
        skipped_chapter_ids=[],
        provider_call_cap=DEFAULT_PROVIDER_CALL_HARD_CAP,
        provider_call_count=0,
        transport_retry_count=0,
        pipeline_attempt_count=0,
        upstream_generation_attempt_count=0,
        created_at=now,
        updated_at=now,
    )


def _snapshot_from_orm(orm: PipelineRunORM) -> PipelineRunSnapshot:
    """``PipelineRunORM`` → ``PipelineRunSnapshot`` 转换(字段一一对应)。"""
    return PipelineRunSnapshot(
        run_id=orm.run_id,
        workspace_id=orm.workspace_id,
        state=orm.state,  # type: ignore[arg-type]
        scope=orm.scope,  # type: ignore[arg-type]
        start_chapter_id=orm.start_chapter_id,
        end_chapter_id=orm.end_chapter_id,
        volume_id=orm.volume_id,
        profile_override=orm.profile_override,
        chapter_ids=list(orm.chapter_ids),
        current_chapter=orm.current_chapter,
        completed_chapter_ids=list(orm.completed_chapter_ids),
        pause_reason=orm.pause_reason,
        error_details=(
            dict(orm.error_details) if orm.error_details is not None else None
        ),
        failure_policy=orm.failure_policy,  # type: ignore[arg-type]
        skipped_chapter_ids=list(orm.skipped_chapter_ids),
        provider_call_cap=orm.provider_call_cap,
        provider_call_count=orm.provider_call_count,
        transport_retry_count=orm.transport_retry_count,
        pipeline_attempt_count=orm.pipeline_attempt_count,
        upstream_generation_attempt_count=(orm.upstream_generation_attempt_count),
        created_at=orm.created_at,
        updated_at=orm.updated_at,
    )


_KNOWN_REVIEW_RECOVERY_KIND = "known_review_archive_checkpoint"


def _pipeline_error_recovery_kind(row: PipelineRunORM) -> str | None:
    """识别允许保留原 run 的窄 ``error``，不推断其他异常。"""

    if row.state != "error":
        return None
    from src.services.known_review_response_recovery import (
        is_known_review_recovery_input_error,
    )

    details = row.error_details or {}
    if is_known_review_recovery_input_error(row):
        return _KNOWN_REVIEW_RECOVERY_KIND
    if details.get("error_type") == _RECOVERABLE_ERROR_TYPE:
        return _RECOVERABLE_ERROR_TYPE
    if details.get("reason") == _RECOVERABLE_CARD_ERROR_REASON:
        return _CARD_REPAIR_RECOVERY_KIND
    if details.get("error_type") in _PROVIDER_REVIEW_ERROR_TYPES:
        return _PROVIDER_REVIEW_RECOVERY_KIND
    if details.get("error_type") == _CORE_STATE_ERROR_TYPE:
        if row.current_chapter == "ch-0001" and details.get("chapter_id") == "ch-0001":
            return _CORE_STATE_RECOVERY_KIND
        return _CORE_STATE_TRANSITION_RECOVERY_KIND
    return None


def _provider_exception_details(
    exc: BaseException,
    *,
    chapter_id: str | None,
) -> dict[str, Any]:
    """生成可落库的 provider 错误摘要，不保留 headers/body/凭据。"""

    details: dict[str, Any] = {
        "error_type": type(exc).__name__,
        "message": str(exc),
        "chapter_id": chapter_id,
    }
    if not isinstance(exc, LLMAuthError):
        return details
    for field in (
        "status_code",
        "provider_code",
        "provider_message",
        "provider_request_id",
    ):
        value = getattr(exc, field, None)
        if value is not None:
            details[field] = value
    return details


def _memory_enrichment_pause_details(
    exc: MemoryEnrichmentNotReadyError,
) -> dict[str, Any]:
    """生成可供 Run UI 展示的富记忆暂停摘要。

    这里只复制 ChapterService 已经脱敏、结构化的恢复判定，不落 Provider
    原始响应、headers 或正文。这样人工能看见“等待已有 Worker / 可安全重试 /
    必须保留现场”的区别，而不需要肉眼检查 JSON 字符。
    """

    recovery_details = getattr(exc, "recovery_details", None)
    details: dict[str, Any] = {
        "reason": exc.pause_reason,
        "error_type": type(exc).__name__,
        "chapter_id": exc.chapter_id,
        "memory_status": exc.status,
        "message": exc.reason,
    }
    if isinstance(recovery_details, dict):
        details.update(recovery_details)
    return details


def _is_continuity_bridge_card_retry_error(row: PipelineRunORM) -> bool:
    """只识别旧循环已经落库的 continuity bridge 确定性拒绝。"""

    details = row.error_details or {}
    message = details.get("message")
    return (
        row.state == "error"
        and details.get("reason") == _RECOVERABLE_CARD_ERROR_REASON
        and isinstance(message, str)
        and _CONTINUITY_BRIDGE_RETRY_ERROR_MARKER in message
        and details.get("continuity_bridge_retry_exhausted") is not True
    )


def _require_pipeline_resume_scope_safe(
    *,
    row: PipelineRunORM,
    run_id: str,
) -> None:
    """CAS / attempt / Worker 之前验证 run 的全部章节范围账本。"""

    current = row.current_chapter
    checkpoint_chapter_id = (
        current if isinstance(current, str) and current.strip() else "(pipeline-run)"
    )
    evidence = [
        f"run_id={run_id}",
        f"chapter_ids={row.chapter_ids!r}",
        f"completed_chapter_ids={row.completed_chapter_ids!r}",
        f"skipped_chapter_ids={row.skipped_chapter_ids!r}",
        f"current_chapter={current!r}",
    ]

    def require_id_list(
        *, field_name: str, value: object, require_nonempty: bool
    ) -> set[str]:
        if not isinstance(value, list):
            raise ContinuityCheckpointCorruptionError(
                chapter_id=checkpoint_chapter_id,
                reason=f"run {field_name} 必须是 list，禁止恢复 Worker",
                evidence=evidence,
            )
        if require_nonempty and not value:
            raise ContinuityCheckpointCorruptionError(
                chapter_id=checkpoint_chapter_id,
                reason=f"run {field_name} 不得为空，禁止恢复 Worker",
                evidence=evidence,
            )
        if any(
            not isinstance(chapter_id, str) or not chapter_id.strip()
            for chapter_id in value
        ):
            raise ContinuityCheckpointCorruptionError(
                chapter_id=checkpoint_chapter_id,
                reason=(
                    f"run {field_name} 只能包含非空字符串，"
                    "禁止恢复 Worker"
                ),
                evidence=evidence,
            )
        ids = set(value)
        if len(ids) != len(value):
            raise ContinuityCheckpointCorruptionError(
                chapter_id=checkpoint_chapter_id,
                reason=f"run {field_name} 包含重复章节，禁止恢复 Worker",
                evidence=evidence,
            )
        return ids

    scope_ids = require_id_list(
        field_name="chapter_ids",
        value=row.chapter_ids,
        require_nonempty=True,
    )
    completed_ids = require_id_list(
        field_name="completed_chapter_ids",
        value=row.completed_chapter_ids,
        require_nonempty=False,
    )
    skipped_ids = require_id_list(
        field_name="skipped_chapter_ids",
        value=row.skipped_chapter_ids,
        require_nonempty=False,
    )
    if not completed_ids.issubset(scope_ids):
        raise ContinuityCheckpointCorruptionError(
            chapter_id=checkpoint_chapter_id,
            reason=(
                "run completed_chapter_ids 含有 chapter_ids scope 外章节，"
                "禁止恢复 Worker"
            ),
            evidence=evidence,
        )
    if not skipped_ids.issubset(scope_ids):
        raise ContinuityCheckpointCorruptionError(
            chapter_id=checkpoint_chapter_id,
            reason=(
                "run skipped_chapter_ids 含有 chapter_ids scope 外章节，"
                "禁止恢复 Worker"
            ),
            evidence=evidence,
        )
    if completed_ids & skipped_ids:
        raise ContinuityCheckpointCorruptionError(
            chapter_id=checkpoint_chapter_id,
            reason=(
                "run completed_chapter_ids 与 skipped_chapter_ids 必须互斥，"
                "禁止恢复 Worker"
            ),
            evidence=evidence,
        )
    if current is not None and (
        not isinstance(current, str)
        or not current.strip()
        or current not in scope_ids
    ):
        raise ContinuityCheckpointCorruptionError(
            chapter_id=checkpoint_chapter_id,
            reason=(
                "run current_chapter 不属于合法 chapter_ids scope，"
                "禁止恢复 Worker"
            ),
            evidence=evidence,
        )


def _require_memory_enrichment_resume_safe(
    *,
    row: ChapterORM,
    chapter_id: str,
    run_id: str,
) -> None:
    """Run CAS/Worker/attempt 之前拦截富记忆损坏或未隔离现场。"""

    evidence = [
        f"run_id={run_id}",
        f"memory_enrichment_status={row.memory_enrichment_status!r}",
        (
            "memory_enrichment_details_type="
            f"{type(row.memory_enrichment_details).__name__}"
        ),
        f"memory_enriched_at={row.memory_enriched_at!r}",
        f"current_version={row.current_version}",
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
            evidence=evidence,
        ) from exc
    if memory_enrichment_requires_manual_recovery(
        status=row.memory_enrichment_status,
        details=row.memory_enrichment_details,
    ):
        raise ContinuityCheckpointCorruptionError(
            chapter_id=chapter_id,
            reason=(
                "run current checkpoint 的富记忆冷启动审计仍为 "
                "blocked_uncertain；禁止创建恢复 Worker 或继续下一章"
            ),
            evidence=evidence,
        )


# =============================================================================
# Service
# =============================================================================


class PipelineService:
    """流水线运行控制业务层(P6-B-03 SQLite 持久化版)。

    依赖:
    - ``chapter_service``:execute 循环里调 ``write(workspace_id, chapter_id, profile_override=...)``
    - ``workspace_service``:校验 workspace 存在
    - ``session_factory`` (**必需**,P6-B-03 起):开 ``data/novel.db`` Session
      读写 ``pipeline_runs`` 表与 VolumeORM(scope 解析)
    - ``ws_manager``:可选;无注入 → WS publish 静默跳过

    线程安全:``self._lock`` 保护"SELECT state → UPDATE state"原子序列,
    防多端点 / 后台循环同 workspace 并发触发竞争(SQLite 自身写串行化,
    lock 是 Python 端语义保护)。
    """

    def __init__(
        self,
        *,
        chapter_service: ChapterService,
        workspace_service: WorkspaceService,
        session_factory: SessionFactory,
        ws_manager: "WebSocketManager | None" = None,
        chapter_card_service: "ChapterCardService | None" = None,
        cost_guard_config: object | None = None,
        watchdog_config: PipelineWatchdogConfig | None = None,
    ) -> None:
        self._chapter_service = chapter_service
        self._workspace_service = workspace_service
        self._session_factory = session_factory
        self._attempt_ledger = AttemptLedgerStore(session_factory)
        self._ws_manager = ws_manager
        # v2 w1-s1 卡生成入循环:None(legacy 测试 / lifespan builder 不注入)
        # 时跳过循环内卡保障,缺卡由 write() 的 card_status gate 自然兜底
        # (CardNotApproved 异常 → 按失败策略处置)。
        self._chapter_card_service = chapter_card_service
        # v2 w1-s3 Run 级成本预检:None=disable(对齐 ChapterService
        # ``cost_guard_config`` 同语义 — CLI / 单测 / legacy fixture 不注入
        # 即不跑 gate);生产路径 deps.py 注入 ``_COST_GUARD_CONFIG`` 单例。
        # 签名用 ``object`` 同 ChapterService:避顶层 import
        # ``services.cost_guard``,实际类型在 ``_evaluate_run_cost_gate``
        # 局部 import 后 isinstance 收窄。
        self._cost_guard_config = cost_guard_config
        # v2 w1-s3 章级看门狗:None → 读 config.yaml ``pipeline:`` 段
        # (缺段走字面默认 2400s)— 生产 / CLI 构造零参数即自动启用;
        # 单测注入小预算配置。``chapter_timeout_seconds <= 0`` 禁用。
        self._watchdog_config = (
            watchdog_config
            if watchdog_config is not None
            else load_pipeline_watchdog_config()
        )
        # 心跳表:workspace_id → _LoopHeartbeat(execute 循环登记;watchdog
        # daemon 线程扫描;single-flight 入口闸复用同一表判"循环已在跑")。
        self._heartbeats: dict[str, _LoopHeartbeat] = {}
        self._watchdog_thread: threading.Thread | None = None
        self._lock = threading.Lock()

    # ---- 公共入口:start --------------------------------------------------

    def start(
        self,
        workspace_id: str,
        *,
        scope: PipelineScope,
        start_chapter_id: str | None,
        end_chapter_id: str | None,
        volume_id: str | None,
        profile_override: str | None,
        failure_policy: PipelineFailurePolicy = "stop",
        provider_call_cap: int = DEFAULT_PROVIDER_CALL_HARD_CAP,
    ) -> PipelineRunSnapshot:
        """启动新 run:校验 + 解析 chapter_ids + INSERT 新行 + publish。

        **注意**:本方法只 INSERT + publish ``pipeline_started``,**不**启动
        execute 循环;循环由调用方(路由层 ``BackgroundTasks``)显式调
        ``execute(workspace_id, run_id)`` 触发。
        """
        # 校验 workspace(不存在 → MetaFileError → WORKSPACE_INVALID 422)
        self._workspace_service.info(workspace_id)
        if provider_call_cap < 1:
            raise PipelineServiceError("provider_call_cap 必须 >= 1")

        binding_loader = getattr(
            self._chapter_service, "active_run_model_manifest_binding", None
        )
        binding = binding_loader(workspace_id) if callable(binding_loader) else None
        if binding is not None:
            if profile_override is not None:
                raise PipelineServiceError(
                    "活动已选择单模型 manifest，禁止另传 profile_override"
                )
            attempt_scope = current_attempt_scope()
            if (
                attempt_scope is not None
                and attempt_scope.activity_scope == "selection_test"
            ):
                selection_test = binding.selection_test
                if (
                    attempt_scope.selection_test_manifest_token
                    != selection_test.token
                ):
                    raise PipelineServiceError(
                        "selection-test scope token 与活动 manifest 不一致"
                    )
                if (
                    selection_test.max_physical_requests <= 0
                    or selection_test.max_configured_rmb <= 0
                ):
                    raise PipelineServiceError(
                        "selection-test loss breakers 仍为 0/0，活动未获授权"
                    )
                profile_override = selection_test.token
                provider_call_cap = selection_test.max_physical_requests
            else:
                # A production single-model lock persists only model identity.
                # It deliberately keeps the existing provider_call_cap.
                profile_override = binding.token

        # scope 解析可先做只读；active run + 全 scope lease + INSERT 必须在
        # 同一个 BEGIN IMMEDIATE 事务，和 UI reserve 双向互斥。
        chapter_ids = self._resolve_chapters(
            workspace_id=workspace_id,
            scope=scope,
            start_chapter_id=start_chapter_id,
            end_chapter_id=end_chapter_id,
            volume_id=volume_id,
        )
        with self._lock:
            run_id = f"run_{secrets.token_hex(8)}"
            now = datetime.now(UTC)
            with self._open_session(workspace_id) as session:
                ChapterOperationLeaseStore.begin_immediate(session)
                active = self._load_active_run(session, workspace_id)
                if active is None:
                    active = self._load_recoverable_error_run(session, workspace_id)
                if active is not None:
                    raise PipelineAlreadyRunningError(
                        workspace_id=workspace_id,
                        active_run_id=active.run_id,
                        active_state=active.state,  # type: ignore[arg-type]
                    )
                conflicting_lease = session.scalar(
                    select(ChapterOperationLeaseORM)
                    .where(ChapterOperationLeaseORM.workspace_id == workspace_id)
                    .where(ChapterOperationLeaseORM.chapter_id.in_(chapter_ids))
                    .limit(1)
                )
                if conflicting_lease is not None:
                    raise ChapterOperationLeaseError(
                        "Pipeline start 遇到未收口章节租约，已阻止创建 run: "
                        f"chapter={conflicting_lease.chapter_id} "
                        f"task={conflicting_lease.task_id} "
                        f"phase={conflicting_lease.phase}"
                    )
                orm = PipelineRunORM(
                    run_id=run_id,
                    workspace_id=workspace_id,
                    state="running",
                    scope=scope,
                    start_chapter_id=start_chapter_id,
                    end_chapter_id=end_chapter_id,
                    volume_id=volume_id,
                    profile_override=profile_override,
                    chapter_ids=list(chapter_ids),
                    current_chapter=None,
                    completed_chapter_ids=[],
                    pause_reason=None,
                    error_details=None,
                    failure_policy=failure_policy,
                    skipped_chapter_ids=[],
                    provider_call_cap=provider_call_cap,
                    provider_call_count=0,
                    transport_retry_count=0,
                    pipeline_attempt_count=0,
                    upstream_generation_attempt_count=0,
                    created_at=now,
                    updated_at=now,
                    completed_at=None,
                )
                session.add(orm)
                session.commit()
                snapshot = _snapshot_from_orm(orm)

        self._publish(
            event="pipeline_started",
            data={
                "run_id": run_id,
                "workspace_id": workspace_id,
                "scope": scope,
                "chapter_ids": chapter_ids,
                "profile_override": profile_override,
            },
        )
        _log.info(
            "pipeline_started",
            workspace_id=workspace_id,
            run_id=run_id,
            scope=scope,
            chapter_count=len(chapter_ids),
        )
        return snapshot

    # ---- 公共入口:pause / resume / stop ------------------------------------

    def pause(
        self,
        workspace_id: str,
        *,
        reason: str = "user_request",
    ) -> PipelineRunSnapshot:
        """暂停 active run。

        - 无 active run → ``PipelineNotRunningError``
        - 已 ``paused`` → **幂等返回当前 snapshot**(不再次 publish event)
        - ``interrupted`` → **幂等返回**(v2 w1-s2:已不在跑,保留
          interrupted 语义标记不翻 paused — resume 仍可断点续跑)
        """
        with self._lock:
            with self._open_session(workspace_id) as session:
                active = self._load_active_run(session, workspace_id)
                if active is None:
                    raise PipelineNotRunningError(
                        workspace_id=workspace_id, action="pause"
                    )
                if active.state in ("paused", "interrupted"):
                    return _snapshot_from_orm(active)
                active.state = "paused"
                active.pause_reason = reason
                active.updated_at = datetime.now(UTC)
                session.commit()
                snapshot = _snapshot_from_orm(active)

        self._publish(
            event="pipeline_paused",
            data={
                "run_id": snapshot.run_id,
                "workspace_id": workspace_id,
                "reason": reason,
                "current_chapter": snapshot.current_chapter,
                "completed_chapter_ids": snapshot.completed_chapter_ids,
            },
        )
        _log.info(
            "pipeline_paused",
            workspace_id=workspace_id,
            run_id=snapshot.run_id,
            reason=reason,
        )
        return snapshot

    def resume(self, workspace_id: str) -> PipelineRunSnapshot:
        """兼容入口；需要 Worker 调度所有权时用 ``resume_with_transition``。"""

        snapshot, _transitioned = self.resume_with_transition(workspace_id)
        return snapshot

    def resume_with_transition(
        self, workspace_id: str
    ) -> tuple[PipelineRunSnapshot, bool]:
        """恢复 paused / interrupted 或已验证可恢复的 error run。

        返回 ``(snapshot, transitioned)``。只有通过 DB 条件 UPDATE 真正把旧态
        翻成 running 的调用拿到 ``transitioned=True``；它也是唯一可以 publish
        resumed 事件、由路由启动 Worker 的所有者。并发 loser 看到 running 时
        幂等返回 ``False``，避免重复 Worker / 重复 Provider 请求。

        - 无 active run → ``PipelineNotRunningError``
        - ``running`` → **幂等返回当前 snapshot + False**
        - ``paused`` / ``interrupted``(v2 w1-s2 扩档,重启中断断点续跑)
          或 ``ContinuityLedgerNotReadyError`` 对应的窄 ``error``，以及章节卡
          已由产品审核修复且正文现场为空净的 ``card_generation_failed``，或
          保留 ``CardContinuityBridgeRequiredError`` 确定性证据、尚未产生
          正文的未批准卡，或 Provider 认证/访问拒绝但最新
          正文已落库、当前版本尚无 Reviewer 结果的审稿断点，或严格同源的
          首章 baseline / 非首章批准 transition 核心状态本地提交断点
          → CAS 翻 ``running`` + publish ``pipeline_resumed``;transition owner
          应紧接着
          调 ``execute()`` 继续循环(选章从 completed ∪ skipped ∪ 章终态
          之后推进,中断章经 reset_stale 自愈后被重新选中)。
        """
        transitioned = False
        reconciled_attempt_ids: list[str] = []
        with self._lock:
            with self._open_session(workspace_id) as session:
                ChapterOperationLeaseStore.begin_immediate(session)
                active = self._load_active_run(session, workspace_id)
                if active is None:
                    active = self._load_recoverable_error_run(session, workspace_id)
                if active is None:
                    raise PipelineNotRunningError(
                        workspace_id=workspace_id, action="resume"
                    )
                if active.state == "running":
                    return _snapshot_from_orm(active), False

                run_id = active.run_id
                old_state = active.state
                error_recovery_kind = _pipeline_error_recovery_kind(active)
                _require_pipeline_resume_scope_safe(
                    row=active,
                    run_id=run_id,
                )
                if active.current_chapter is not None:
                    active_lease = session.get(
                        ChapterOperationLeaseORM,
                        (workspace_id, active.current_chapter),
                    )
                    if active_lease is not None:
                        raise ChapterOperationLeaseError(
                            "Pipeline resume 遇到未收口章节租约，已阻止创建 Worker: "
                            f"chapter={active.current_chapter} "
                            f"task={active_lease.task_id} phase={active_lease.phase}"
                        )
                    current_row = session.get(ChapterORM, active.current_chapter)
                    if current_row is None:
                        db_version_count = int(
                            session.scalar(
                                select(func.count())
                                .select_from(ChapterVersionORM)
                                .where(
                                    ChapterVersionORM.chapter_id
                                    == active.current_chapter
                                )
                            )
                            or 0
                        )
                        paths = WorkspacePaths(
                            workspace_id,
                            workspaces_root=(
                                self._workspace_service.workspaces_root
                            ),
                        )
                        chapter_dir = paths.chapter_dir(active.current_chapter)
                        disk_evidence = (
                            sorted(path.name for path in chapter_dir.glob("v*.md"))
                            if chapter_dir.is_dir()
                            else []
                        )
                        final_exists = paths.chapter_version_file(
                            active.current_chapter, "final.md"
                        ).is_file()
                        if db_version_count or disk_evidence or final_exists:
                            raise ContinuityCheckpointCorruptionError(
                                chapter_id=active.current_chapter,
                                reason=(
                                    "run current_chapter 缺少章节行但留有版本/终稿证据"
                                ),
                                evidence=[
                                    f"run_id={run_id}",
                                    f"db_version_count={db_version_count}",
                                    f"version_files={disk_evidence}",
                                    f"final_exists={final_exists}",
                                ],
                            )
                    elif current_row.workspace_id != workspace_id:
                        raise ContinuityCheckpointCorruptionError(
                            chapter_id=active.current_chapter,
                            reason="run current_chapter 的 workspace checkpoint 串位",
                            evidence=[
                                f"run_id={run_id}",
                                f"row.workspace_id={current_row.workspace_id}",
                            ],
                        )
                    # R8:无论 ready 是否已落，先按统一 shape 合同检查。
                    # 未知 status、坏 details 或 blocked_uncertain 在 CAS/
                    # Worker/attempt 前硬停；合法 ready+running 仍按 §2.71
                    # 降级继续，且本入口绝不重排它。
                    else:
                        _require_memory_enrichment_resume_safe(
                            row=current_row,
                            chapter_id=active.current_chapter,
                            run_id=run_id,
                        )
                if error_recovery_kind == _CARD_REPAIR_RECOVERY_KIND:
                    self._require_repaired_card_resume_safe(
                        session,
                        workspace_id=workspace_id,
                        row=active,
                    )
                if error_recovery_kind == _PROVIDER_REVIEW_RECOVERY_KIND:
                    self._require_provider_review_resume_safe(
                        session,
                        workspace_id=workspace_id,
                        row=active,
                    )
                if error_recovery_kind == _KNOWN_REVIEW_RECOVERY_KIND:
                    self._require_known_review_resume_safe(
                        session, workspace_id=workspace_id, row=active,
                    )
                if error_recovery_kind == _CORE_STATE_RECOVERY_KIND:
                    self._require_core_state_resume_safe(
                        session,
                        workspace_id=workspace_id,
                        row=active,
                    )
                if error_recovery_kind == _CORE_STATE_TRANSITION_RECOVERY_KIND:
                    self._require_core_state_transition_resume_safe(
                        session,
                        workspace_id=workspace_id,
                        row=active,
                    )
                has_live_heartbeat = workspace_id in self._heartbeats
                if (
                    error_recovery_kind
                    in {
                        _CARD_REPAIR_RECOVERY_KIND,
                        _PROVIDER_REVIEW_RECOVERY_KIND,
                        _KNOWN_REVIEW_RECOVERY_KIND,
                        _CORE_STATE_RECOVERY_KIND,
                        _CORE_STATE_TRANSITION_RECOVERY_KIND,
                    }
                    and has_live_heartbeat
                ):
                    raise ChapterOperationLeaseError(
                        "error checkpoint 恢复仍检测到同工作区旧 Worker 心跳，"
                        "已阻止创建重复 Worker"
                    )
                stmt = (
                    update(PipelineRunORM)
                    .where(PipelineRunORM.run_id == run_id)
                    .where(PipelineRunORM.workspace_id == workspace_id)
                    .where(PipelineRunORM.state == old_state)
                )
                if old_state == "error":
                    if error_recovery_kind == _RECOVERABLE_ERROR_TYPE:
                        stmt = stmt.where(
                            PipelineRunORM.error_details["error_type"].as_string()
                            == _RECOVERABLE_ERROR_TYPE
                        )
                    elif error_recovery_kind == _CARD_REPAIR_RECOVERY_KIND:
                        stmt = stmt.where(
                            PipelineRunORM.error_details["reason"].as_string()
                            == _RECOVERABLE_CARD_ERROR_REASON
                        )
                    elif error_recovery_kind == _PROVIDER_REVIEW_RECOVERY_KIND:
                        stmt = stmt.where(
                            PipelineRunORM.error_details[
                                "error_type"
                            ].as_string().in_(_PROVIDER_REVIEW_ERROR_TYPES)
                        )
                    elif error_recovery_kind == _KNOWN_REVIEW_RECOVERY_KIND:
                        from src.services.known_review_response_recovery import (
                            REVIEW_INPUT_MISMATCH_MESSAGE,
                        )
                        stmt = stmt.where(
                            PipelineRunORM.error_details["error_type"].as_string()
                            == "ValueError",
                            PipelineRunORM.error_details["message"].as_string()
                            == REVIEW_INPUT_MISMATCH_MESSAGE,
                        )
                    elif error_recovery_kind == _CORE_STATE_RECOVERY_KIND:
                        stmt = stmt.where(
                            PipelineRunORM.error_details[
                                "error_type"
                            ].as_string()
                            == _CORE_STATE_ERROR_TYPE
                        )
                    elif (
                        error_recovery_kind
                        == _CORE_STATE_TRANSITION_RECOVERY_KIND
                    ):
                        stmt = stmt.where(
                            PipelineRunORM.error_details[
                                "error_type"
                            ].as_string()
                            == _CORE_STATE_ERROR_TYPE
                        )
                    else:
                        raise PipelineNotRunningError(
                            workspace_id=workspace_id,
                            action="resume",
                        )
                result = session.execute(
                    stmt.values(
                        state="running",
                        pause_reason=None,
                        error_details=None,
                        updated_at=datetime.now(UTC),
                    ).execution_options(synchronize_session=False)
                )
                if result.rowcount != 1:
                    session.rollback()
                    current = self._load_run_by_id(
                        session,
                        workspace_id=workspace_id,
                        run_id=run_id,
                    )
                    if current is not None and current.state == "running":
                        return _snapshot_from_orm(current), False
                    raise PipelineNotRunningError(
                        workspace_id=workspace_id,
                        action="resume",
                    )
                if not has_live_heartbeat:
                    # R8 checkpoint recovery:CAS owner 已确认本 Service 没有旧
                    # Worker heartbeat，故同 run 的 started pipeline 行属于进程
                    # 中断遗留。与状态迁移同事务收口；provider started 结果未知，
                    # AttemptLedgerStore 的精确筛选保证绝不触碰。
                    reconciled_attempt_ids = (
                        self._attempt_ledger.reconcile_orphaned_pipeline_attempts(
                            session=session,
                            workspace_id=workspace_id,
                            run_id=run_id,
                        )
                    )
                session.commit()
                session.expire_all()
                current = self._load_run_by_id(
                    session,
                    workspace_id=workspace_id,
                    run_id=run_id,
                )
                if current is None:
                    raise PipelineNotRunningError(
                        workspace_id=workspace_id,
                        action="resume",
                    )
                snapshot = _snapshot_from_orm(current)
                transitioned = True

        if transitioned:
            if reconciled_attempt_ids:
                _log.warning(
                    "orphaned_pipeline_attempts_reconciled",
                    workspace_id=workspace_id,
                    run_id=snapshot.run_id,
                    attempt_ids=reconciled_attempt_ids,
                    error_type="PipelineInterrupted",
                )
            self._publish(
                event="pipeline_resumed",
                data={
                    "run_id": snapshot.run_id,
                    "workspace_id": workspace_id,
                    "current_chapter": snapshot.current_chapter,
                    "completed_chapter_ids": snapshot.completed_chapter_ids,
                },
            )
            _log.info(
                "pipeline_resumed",
                workspace_id=workspace_id,
                run_id=snapshot.run_id,
            )
        return snapshot, transitioned

    def stop(self, workspace_id: str) -> PipelineRunSnapshot:
        """终止 active run。

        - 无 active 且无 error → ``PipelineNotRunningError``
        - 已 ``stopped`` → **幂等返回**(不会到这里:active 不含 stopped)
        - ``running`` / ``paused`` / ``error`` → 翻 ``stopped`` + publish。
        """
        with self._lock:
            with self._open_session(workspace_id) as session:
                row = self._load_active_or_error_run(session, workspace_id)
                if row is None:
                    raise PipelineNotRunningError(
                        workspace_id=workspace_id, action="stop"
                    )
                now = datetime.now(UTC)
                row.state = "stopped"
                row.pause_reason = "user_request"
                row.updated_at = now
                row.completed_at = now
                session.commit()
                snapshot = _snapshot_from_orm(row)

        self._publish(
            event="pipeline_stopped",
            data={
                "run_id": snapshot.run_id,
                "workspace_id": workspace_id,
                "reason": "user_request",
                "current_chapter": snapshot.current_chapter,
                "completed_chapter_ids": snapshot.completed_chapter_ids,
            },
        )
        _log.info(
            "pipeline_stopped",
            workspace_id=workspace_id,
            run_id=snapshot.run_id,
            reason="user_request",
        )
        return snapshot

    def status(self, workspace_id: str) -> PipelineRunSnapshot:
        """读当前 run 状态(active 优先,否则取最近 1 行,否则 idle)。"""
        with self._open_session(workspace_id) as session:
            active = self._load_active_run(session, workspace_id)
            if active is not None:
                return _snapshot_from_orm(active)
            last = self._load_last_run(session, workspace_id)
            if last is not None:
                snapshot = _snapshot_from_orm(last)
                recovery_kind = _pipeline_error_recovery_kind(last)
                if recovery_kind == _CARD_REPAIR_RECOVERY_KIND:
                    return self._with_card_repair_readiness(
                        session,
                        row=last,
                        snapshot=snapshot,
                    )
                if recovery_kind == _PROVIDER_REVIEW_RECOVERY_KIND:
                    return self._with_provider_review_readiness(
                        session,
                        row=last,
                        snapshot=snapshot,
                    )
                if recovery_kind == _KNOWN_REVIEW_RECOVERY_KIND:
                    details = dict(snapshot.error_details or {})
                    details["recovery_kind"] = _KNOWN_REVIEW_RECOVERY_KIND
                    try:
                        _require_pipeline_resume_scope_safe(row=last, run_id=last.run_id)
                        self._require_known_review_resume_safe(
                            session, workspace_id=workspace_id, row=last,
                        )
                    except ContinuityCheckpointCorruptionError:
                        details.update(recovery_ready=False, recovery_block_reason="checkpoint_not_clean")
                    else:
                        details["recovery_ready"] = True
                    return snapshot.model_copy(update={"error_details": details})
                if recovery_kind == _CORE_STATE_RECOVERY_KIND:
                    return self._with_core_state_recovery_readiness(
                        session,
                        row=last,
                        snapshot=snapshot,
                    )
                if recovery_kind == _CORE_STATE_TRANSITION_RECOVERY_KIND:
                    return self._with_core_state_transition_recovery_readiness(
                        session,
                        row=last,
                        snapshot=snapshot,
                    )
                return snapshot
        return _idle_snapshot(workspace_id)

    def summarize_attempt_usage_cost(
        self,
        workspace_id: str,
        *,
        run_id: str | None,
    ) -> ProviderUsageCostSummary:
        """读当前 run 的物理请求 usage/配置估算；idle 返回空汇总。"""

        if not run_id:
            return ProviderUsageCostSummary(
                usage_known_attempts=0,
                usage_unknown_attempts=0,
                legacy_unclassified_attempts=0,
                input_tokens_known=0,
                output_tokens_known=0,
                provider_request_id_count=0,
                response_status_counts={},
                configured_cost_basis_counts={},
                configured_estimates={},
            )
        return self._attempt_ledger.summarize_usage_cost(
            workspace_id,
            run_id=run_id,
        )

    # ---- 后台循环:execute --------------------------------------------------

    def execute(
        self,
        workspace_id: str,
        run_id: str,
        *,
        explicit_resume: bool = False,
    ) -> None:
        """后台循环:逐章调 ``chapter_service.write``。

        由路由层 ``BackgroundTasks`` 在 :start 端点响应已发出后调用;每章前
        后检查 state 是否仍为 ``running``,响应 pause/stop 立即退出。

        异常处理:
        - ``ChapterInvalidatedNeedsUnblockError`` → 自动 pause + reason=
          ``chapter_invalidated``,不撤回已完成章节,不冒泡
        - 其他 Exception → state=error + error_details + structlog
          ``pipeline_chapter_failed``,不冒泡(顶层 BackgroundTasks swallow)
        - 全章完成 → state=stopped + publish ``pipeline_stopped`` reason=
          ``completed``

        single-flight(v2 w1-s3,模块 docstring 第 10 点):同 workspace
        已有循环线程在跑(可能困在 ``write()`` 同步阻塞里)时:

        - 同 run_id 重入(:resume 重调)→ 重入调用静默返回；原 owner 在
          ``_execute_loop`` 退出后原子复核 DB，若 Continue 已把状态翻回
          ``running`` 就直接接手，避免旧 Worker 收尾窗口留下无 Worker run；
        - 异 run_id(:stop 后立刻 :start 新 run)→ 新 run 翻
          ``interrupted`` + reason=``pipeline_loop_conflict``(不静默假死;
          老循环退出后 user 可 :resume)。
        """
        # ---- single-flight 入口闸(锁内只动内存,不开 session) -------------
        conflict_with: str | None = None
        with self._lock:
            existing = self._heartbeats.get(workspace_id)
            if existing is not None:
                conflict_with = existing.run_id
            else:
                self._heartbeats[workspace_id] = _LoopHeartbeat(
                    run_id=run_id,
                    chapter_id=None,
                    started_monotonic=time.monotonic(),
                )
        if conflict_with == run_id:
            _log.info(
                "pipeline_loop_already_active",
                workspace_id=workspace_id,
                run_id=run_id,
            )
            return
        if conflict_with is not None:
            _log.warning(
                "pipeline_loop_conflict",
                workspace_id=workspace_id,
                run_id=run_id,
                occupied_by_run=conflict_with,
            )
            self._mark_interrupted(
                workspace_id=workspace_id,
                run_id=run_id,
                error_details={
                    "reason": "pipeline_loop_conflict",
                    "occupied_by_run": conflict_with,
                    "message": (
                        "同工作区旧 run 的循环线程尚未退出(可能仍阻塞在"
                        "写章调用中),本 run 无法启动循环;等旧循环退出后"
                        "可 :resume 续跑,或重启引擎清理"
                    ),
                },
            )
            return

        self._ensure_watchdog_thread()
        heartbeat_released = False
        try:
            while True:
                self._execute_loop(
                    workspace_id,
                    run_id,
                    explicit_resume=explicit_resume,
                )
                # 同 run 的 Continue 可能恰好撞在旧循环已经决定退出、但
                # finally 尚未注销 heartbeat 的极窄窗口。重入 execute 会因
                # single-flight 返回；这里在同一把锁下看 DB 真状态：若已被
                # Continue CAS 回 running，由原 owner 直接接手，避免留下
                # “DB 显示运行中但没有 Worker”的僵尸 run。若仍非 running，
                # 原子注销 heartbeat，让稍后到达的新 execute 自己取得所有权。
                with self._lock:
                    with self._open_session(workspace_id) as session:
                        row = self._load_run_by_id(
                            session,
                            workspace_id=workspace_id,
                            run_id=run_id,
                        )
                        resume_handoff = row is not None and row.state == "running"
                    if not resume_handoff:
                        hb = self._heartbeats.get(workspace_id)
                        if hb is not None and hb.run_id == run_id:
                            del self._heartbeats[workspace_id]
                        heartbeat_released = True
                if not resume_handoff:
                    break
                explicit_resume = True
                _log.info(
                    "pipeline_loop_resume_handoff",
                    workspace_id=workspace_id,
                    run_id=run_id,
                )
        except Exception as exc:  # noqa: BLE001 — 顶层 swallow
            _log.error(
                "pipeline_execute_unexpected",
                workspace_id=workspace_id,
                run_id=run_id,
                error_type=type(exc).__name__,
                error_message=str(exc),
            )
            # 兜底标 error
            self._mark_error(
                workspace_id=workspace_id,
                run_id=run_id,
                error_details={
                    "error_type": type(exc).__name__,
                    "message": str(exc),
                },
            )
        finally:
            # 心跳注销:仅删本循环自己的条目(防误删后继循环的新条目)
            if not heartbeat_released:
                with self._lock:
                    hb = self._heartbeats.get(workspace_id)
                    if hb is not None and hb.run_id == run_id:
                        del self._heartbeats[workspace_id]

    def _execute_loop(
        self,
        workspace_id: str,
        run_id: str,
        *,
        explicit_resume: bool = False,
    ) -> None:
        """主循环本体(被 ``execute`` 顶层 swallow 包裹)。

        v2 w1-s1 起每章三段语义(闭合 v2_plan §2.2 缺口 1/3/4):

        1. **写前卡保障**(缺口 3):缺卡 → ``generate_next_card``
           补齐 + 自动 approve(对齐 v1.1 acceptance driver 实证模式);
           rejected 卡不自动绕过(用户明确意图)。
        2. **写章**:异常按失败策略处置(缺口 4)。
        3. **写后孤儿章感知**(缺口 1):``result.final_state ==
           HUMAN_INTERVENTION`` → 按失败策略处置(stop 默认 = auto_pause
           reason=``chapter_escalated`` + **不 append completed** + 下一章
           零调用 — 治"带病续跑 / 衔接接计划非实际")。

        失败策略路由统一走 ``_dispose_chapter_failure``；历史 attempt 只作
        append-only 审计，``retry`` 的一次自动重试窗口按本次 execute 调用管理。
        """
        completed_snapshot: PipelineRunSnapshot | None = None
        # 单次 execute/resume invocation 的自动 retry 窗口；历史 ledger 不得
        # 永久剥夺修复后的 checkpoint 恢复机会。
        retried_chapters: set[str] = set()
        # 每次 execute/resume invocation 都重新复核已有 completed/terminal
        # checkpoint；同一次循环内只验一次，避免 O(N²)。skipped 是失败记录，
        # 绝不能混入“已验证完成”集合。
        verified_terminal_ids: set[str] = set()
        while True:
            terminal_validation_failure: tuple[str, Exception] | None = None
            reconciled_checkpoint_progress: tuple[str, int, int] | None = None
            recover_current_memory_checkpoint = False
            # 顶部 SELECT:决定下一章(completed ∪ skipped ∪ 章终态都不
            # 再选 — 前两者防 skip 后死循环重选同章,章终态防新 run /
            # 重启 resume 重写已过审章,v2 w1-s2)
            with self._lock:
                with self._open_session(workspace_id) as session:
                    orm = self._load_run_by_id(
                        session, workspace_id=workspace_id, run_id=run_id
                    )
                    if orm is None:
                        # 被 stop 端点清除 / 被新 run 覆盖
                        return
                    if orm.state != "running":
                        # paused / stopped / error;退出循环等待 resume 重启
                        return
                    next_chapter = None
                    completed_ids = set(orm.completed_chapter_ids)
                    skipped_ids = set(orm.skipped_chapter_ids)
                    taken = completed_ids | skipped_ids
                    # v2 w1-s2 防重写：ready 已落账的 approved/published 在
                    # 本地复核 ledger/hash 后排除。approved+ready 空重选并只补
                    # ledger；published+ready 空也重选，让 checkpoint corruption
                    # 在任何 Provider 前硬停，绝不能静默假完成。
                    terminal_stmt = (
                        select(ChapterORM.chapter_id)
                        .where(ChapterORM.workspace_id == workspace_id)
                        .where(ChapterORM.chapter_id.in_(orm.chapter_ids))
                        .where(ChapterORM.status.in_(("approved", "published")))
                        .where(ChapterORM.ready_for_next_chapter_at.is_not(None))
                    )
                    terminal_ids = set(session.scalars(terminal_stmt))
                    ready_checker = getattr(
                        self._chapter_service,
                        "require_ready_for_next_chapter",
                        None,
                    )
                    memory_checker = getattr(
                        self._chapter_service,
                        "require_memory_enrichment_succeeded",
                        None,
                    )
                    overlap = completed_ids & skipped_ids
                    if overlap:
                        overlap_id = sorted(overlap)[0]
                        terminal_validation_failure = (
                            overlap_id,
                            ContinuityCheckpointCorruptionError(
                                chapter_id=overlap_id,
                                reason=(
                                    "run 账本把同一章节同时记为 completed 和 skipped"
                                ),
                                evidence=[f"run_id={run_id}"],
                            ),
                        )
                    if terminal_validation_failure is None and callable(ready_checker):
                        # completed 不依赖 terminal 查询结果：哪怕 DB 行缺失、
                        # drafting 或 ready 空，也必须调用权威 checker 硬停。
                        for completed_id in completed_ids - verified_terminal_ids:
                            try:
                                ready_checker(workspace_id, completed_id)
                            except (
                                ContinuityCheckpointCorruptionError,
                                ContinuityLedgerNotReadyError,
                            ) as exc:
                                terminal_validation_failure = (completed_id, exc)
                                break
                            except Exception as exc:  # noqa: BLE001
                                terminal_validation_failure = (
                                    completed_id,
                                    ContinuityCheckpointCorruptionError(
                                        chapter_id=completed_id,
                                        reason=(
                                            "completed checkpoint 本地复核异常: "
                                            f"{type(exc).__name__}: {exc}"
                                        ),
                                    ),
                                )
                                break
                            verified_terminal_ids.add(completed_id)
                        if terminal_validation_failure is None:
                            for terminal_id in terminal_ids - verified_terminal_ids:
                                try:
                                    ready_checker(workspace_id, terminal_id)
                                except (
                                    ContinuityCheckpointCorruptionError,
                                    ContinuityLedgerNotReadyError,
                                ) as exc:
                                    terminal_validation_failure = (terminal_id, exc)
                                    break
                                except Exception as exc:  # noqa: BLE001
                                    terminal_validation_failure = (
                                        terminal_id,
                                        ContinuityCheckpointCorruptionError(
                                            chapter_id=terminal_id,
                                            reason=(
                                                "terminal checkpoint 本地复核异常: "
                                                f"{type(exc).__name__}: {exc}"
                                            ),
                                        ),
                                    )
                                    break
                                verified_terminal_ids.add(terminal_id)
                    if terminal_validation_failure is None:
                        # R8 checkpoint recovery:当前章可能由暂停中的 Chapter UI
                        # 正常重写并批准。权威 ready/hash 复核通过后，把这一章补回
                        # 原 run 的 completed 进度；只认 current checkpoint，不能
                        # 把 scope 内原本就终态的其他章节冒充本 run 完成。
                        current_checkpoint = orm.current_chapter
                        if (
                            current_checkpoint is not None
                            and current_checkpoint in terminal_ids
                            and current_checkpoint in verified_terminal_ids
                            and current_checkpoint not in completed_ids
                            and current_checkpoint not in skipped_ids
                        ):
                            if callable(memory_checker):
                                try:
                                    memory_checker(
                                        workspace_id,
                                        current_checkpoint,
                                    )
                                except (
                                    ContinuityCheckpointCorruptionError,
                                    MemoryEnrichmentNotReadyError,
                                ) as exc:
                                    if (
                                        explicit_resume
                                        and isinstance(
                                            exc, MemoryEnrichmentNotReadyError
                                        )
                                        and exc.status in {"pending", "failed"}
                                    ):
                                        # 只有用户显式 Continue 才把当前已通过章
                                        # 重新送入 checkpoint-only attempt。那里会
                                        # 在正式 attempt scope 内复核回滚、来源、
                                        # Provider 账本并只重跑富记忆；普通 start /
                                        # 冷启动仍在这里 fail closed，绝不自动重放。
                                        recover_current_memory_checkpoint = True
                                    else:
                                        terminal_validation_failure = (
                                            current_checkpoint,
                                            exc,
                                        )
                            if terminal_validation_failure is None:
                                if recover_current_memory_checkpoint:
                                    # terminal_ids 原本会把当前章从选章集合排除；
                                    # 显式恢复必须让同一章进入下方既有 pipeline
                                    # attempt + resume_approved_checkpoint 路径。
                                    terminal_ids.discard(current_checkpoint)
                                else:
                                    new_completed = [
                                        *orm.completed_chapter_ids,
                                        current_checkpoint,
                                    ]
                                    orm.completed_chapter_ids = new_completed
                                    completed_ids.add(current_checkpoint)
                                    reconciled_checkpoint_progress = (
                                        current_checkpoint,
                                        len(new_completed),
                                        len(orm.chapter_ids),
                                    )
                        taken |= terminal_ids
                        for ch_id in orm.chapter_ids:
                            if ch_id not in taken:
                                next_chapter = ch_id
                                break
                    if terminal_validation_failure is None and next_chapter is None:
                        # 全章处理完(完成 ∪ 跳过):翻 stopped + completed_at
                        now = datetime.now(UTC)
                        orm.state = "stopped"
                        orm.pause_reason = "completed"
                        orm.updated_at = now
                        orm.completed_at = now
                        session.commit()
                        completed_snapshot = _snapshot_from_orm(orm)
                        break
                    if terminal_validation_failure is None:
                        assert next_chapter is not None
                        orm.current_chapter = next_chapter
                        orm.updated_at = datetime.now(UTC)
                        session.commit()
                        profile_override = orm.profile_override
                        failure_policy: PipelineFailurePolicy = (
                            orm.failure_policy  # type: ignore[assignment]
                        )
                        provider_call_cap = orm.provider_call_cap
                        # v2 w1-s3 心跳:本章计时起点(覆盖式新对象 — watchdog
                        # 用对象身份判"循环是否已推进",flagged 随之清零)。
                        # 含写前卡生成 + write 全链路都算本章预算。
                        self._heartbeats[workspace_id] = _LoopHeartbeat(
                            run_id=run_id,
                            chapter_id=next_chapter,
                            started_monotonic=time.monotonic(),
                        )

            if terminal_validation_failure is not None:
                blocked_chapter_id, terminal_exc = terminal_validation_failure
                if isinstance(terminal_exc, MemoryEnrichmentNotReadyError):
                    reason = terminal_exc.pause_reason
                elif isinstance(
                    terminal_exc, ContinuityCheckpointCorruptionError
                ):
                    reason = "continuity_checkpoint_corruption"
                else:
                    reason = "continuity_ledger_not_ready"
                _log.warning(
                    "pipeline_terminal_checkpoint_paused",
                    workspace_id=workspace_id,
                    run_id=run_id,
                    chapter_id=blocked_chapter_id,
                    reason=reason,
                    error_type=type(terminal_exc).__name__,
                    error_message=str(terminal_exc),
                )
                self._auto_pause(
                    workspace_id,
                    run_id=run_id,
                    reason=reason,
                    blocked_chapter_id=blocked_chapter_id,
                    error_details=(
                        _memory_enrichment_pause_details(terminal_exc)
                        if isinstance(
                            terminal_exc, MemoryEnrichmentNotReadyError
                        )
                        else None
                    ),
                )
                return

            if reconciled_checkpoint_progress is not None:
                checkpoint_id, completed_count, total_count = (
                    reconciled_checkpoint_progress
                )
                _log.info(
                    "pipeline_current_checkpoint_reconciled",
                    workspace_id=workspace_id,
                    run_id=run_id,
                    chapter_id=checkpoint_id,
                    completed_count=completed_count,
                    total_count=total_count,
                )
                self._publish(
                    event="chapter_finished",
                    data={
                        "run_id": run_id,
                        "workspace_id": workspace_id,
                        "chapter_id": checkpoint_id,
                        "completed_count": completed_count,
                        "total_count": total_count,
                    },
                )

            # 下一章卡与 Writer 都会消费记忆库；正式 Run 必须先确认紧邻上一章
            # 的富记忆已经成功，不能在 pending/running/failed 时先发出卡片
            # Provider 请求。首章没有前置章，保持原路径。
            assert next_chapter is not None
            next_seq = chapter_seq_from_id(next_chapter)
            previous_memory_checker = getattr(
                self._chapter_service,
                "require_memory_enrichment_succeeded",
                None,
            )
            if (
                next_seq is not None
                and next_seq > 1
                and callable(previous_memory_checker)
            ):
                previous_chapter_id = f"ch-{next_seq - 1:04d}"
                try:
                    previous_memory_checker(workspace_id, previous_chapter_id)
                except MemoryEnrichmentNotReadyError as exc:
                    _log.warning(
                        "pipeline_previous_memory_enrichment_paused",
                        workspace_id=workspace_id,
                        run_id=run_id,
                        chapter_id=previous_chapter_id,
                        next_chapter_id=next_chapter,
                        memory_enrichment_status=exc.status,
                        reason=exc.pause_reason,
                    )
                    self._auto_pause(
                        workspace_id,
                        run_id=run_id,
                        reason=exc.pause_reason,
                        blocked_chapter_id=previous_chapter_id,
                        error_details=_memory_enrichment_pause_details(exc),
                    )
                    return
                except ContinuityCheckpointCorruptionError as exc:
                    _log.warning(
                        "pipeline_previous_memory_checkpoint_paused",
                        workspace_id=workspace_id,
                        run_id=run_id,
                        chapter_id=previous_chapter_id,
                        next_chapter_id=next_chapter,
                        error_message=str(exc),
                    )
                    self._auto_pause(
                        workspace_id,
                        run_id=run_id,
                        reason="continuity_checkpoint_corruption",
                        blocked_chapter_id=previous_chapter_id,
                    )
                    return

            # ---- Run 级成本预检(v2 w1-s3,P4-B-08 留观闭账) ----------------
            # 超限不走失败策略:成本超限是 Run 级全局态,skip 连环跳全章会
            # 把 run 标成假 completed,retry 立即重撞(模块 docstring 第 11
            # 点)。paused 可 resume(次日成本日清零 / 调高限额后续跑)。
            cost_block = self._evaluate_run_cost_gate(workspace_id)
            if cost_block is not None:
                _log.warning(
                    "pipeline_cost_guard_blocked",
                    workspace_id=workspace_id,
                    run_id=run_id,
                    chapter_id=next_chapter,
                    **cost_block,
                )
                self._auto_pause(
                    workspace_id,
                    run_id=run_id,
                    reason="cost_guard_exceeded",
                    blocked_chapter_id=next_chapter,
                )
                return

            try:
                pipeline_attempt = self._begin_pipeline_attempt_if_running(
                    workspace_id=workspace_id,
                    run_id=run_id,
                    chapter_id=next_chapter,
                )
            except AttemptLedgerError as exc:
                _log.error(
                    "pipeline_attempt_ledger_unavailable",
                    workspace_id=workspace_id,
                    run_id=run_id,
                    chapter_id=next_chapter,
                    error_type=type(exc).__name__,
                )
                self._mark_error(
                    workspace_id=workspace_id,
                    run_id=run_id,
                    error_details={
                        "reason": "ATTEMPT_LEDGER_UNAVAILABLE",
                        "chapter_id": next_chapter,
                    },
                )
                return
            if pipeline_attempt is None:
                # Pause / Stop 可能落在“选出下一章”与“开始该章”之间。
                # 没有 started pipeline attempt 就仍在章边界，必须服从用户
                # 状态，不能继续生成卡片或发出 Provider 请求。
                return

            # publish chapter_started(锁外,避免握锁时阻塞 publish)
            self._publish(
                event="chapter_started",
                data={
                    "run_id": run_id,
                    "workspace_id": workspace_id,
                    "chapter_id": next_chapter,
                },
            )

            # 先尝试 approved checkpoint-only 恢复；不适用时才进入写前卡
            # 保障 + write。这样 checkpoint 损坏会在任何补卡/正文 Provider
            # 请求前 fail closed，且已过审正文绝不会被重新审写。
            card_failure: dict[str, Any] | None = None
            result: Any | None = None
            final_state: ChapterState | None = None
            binding_loader = getattr(
                self._chapter_service, "active_run_model_manifest_binding", None
            )
            run_manifest_binding = (
                binding_loader(workspace_id) if callable(binding_loader) else None
            )
            stored_manifest_token = (
                profile_override
                if isinstance(profile_override, str)
                and (
                    profile_override.startswith("run-model-manifest:")
                    or profile_override.startswith("selection-test:")
                )
                else None
            )
            stored_selection_test = bool(
                run_manifest_binding is not None
                and profile_override
                == run_manifest_binding.selection_test.token
            )
            expected_tokens = (
                {
                    run_manifest_binding.token,
                    run_manifest_binding.selection_test.token,
                }
                if run_manifest_binding is not None
                else set()
            )
            if (
                (run_manifest_binding is None and stored_manifest_token is not None)
                or (
                    run_manifest_binding is not None
                    and profile_override not in expected_tokens
                )
            ):
                self._mark_error(
                    workspace_id=workspace_id,
                    run_id=run_id,
                    error_details={
                        "reason": "RUN_MODEL_MANIFEST_DRIFT",
                        "chapter_id": next_chapter,
                    },
                )
                return
            try:
                with bind_attempt_scope(
                    workspace_id=workspace_id,
                    run_id=run_id,
                    task_id=pipeline_attempt.task_id,
                    chapter_id=next_chapter,
                    provider_call_cap=provider_call_cap,
                    activity_scope=(
                        "selection_test" if stored_selection_test else "production"
                    ),
                    run_model_manifest_token=(
                        run_manifest_binding.token
                        if run_manifest_binding is not None
                        else None
                    ),
                    selection_test_manifest_token=(
                        run_manifest_binding.selection_test.token
                        if stored_selection_test
                        and run_manifest_binding is not None
                        else None
                    ),
                    configured_cost_currency=(
                        run_manifest_binding.selection_test.cost_currency
                        if stored_selection_test
                        and run_manifest_binding is not None
                        else None
                    ),
                    configured_cost_cap=(
                        run_manifest_binding.selection_test.max_configured_rmb
                        if stored_selection_test
                        and run_manifest_binding is not None
                        else None
                    ),
                    synchronous_memory_enrichment=True,
                ):
                    checkpoint_resumer = getattr(
                        self._chapter_service,
                        "resume_approved_checkpoint",
                        None,
                    )
                    checkpoint_resumed = bool(
                        callable(checkpoint_resumer)
                        and checkpoint_resumer(
                            workspace_id,
                            next_chapter,
                            profile_override=profile_override,
                        )
                    )
                    if checkpoint_resumed:
                        final_state = ChapterState.APPROVED
                    else:
                        # ---- 写前卡保障(缺口 3:卡生成入循环) -------
                        card_failure = self._ensure_chapter_card_ready(
                            workspace_id, next_chapter
                        )
                        if card_failure is None:
                            result = self._chapter_service.write(
                                workspace_id,
                                next_chapter,
                                profile_override=profile_override,
                            )
                            final_state = getattr(result, "final_state", None)
                    if (
                        card_failure is None
                        and final_state != ChapterState.HUMAN_INTERVENTION
                    ):
                        ready_checker = getattr(
                            self._chapter_service,
                            "require_ready_for_next_chapter",
                            None,
                        )
                        if callable(ready_checker):
                            ready_checker(workspace_id, next_chapter)
                        memory_checker = getattr(
                            self._chapter_service,
                            "require_memory_enrichment_succeeded",
                            None,
                        )
                        if callable(memory_checker):
                            memory_checker(workspace_id, next_chapter)
            except ChapterInvalidatedNeedsUnblockError:
                self._attempt_ledger.finish_pipeline_attempt(
                    workspace_id=workspace_id,
                    attempt_id=pipeline_attempt.attempt_id,
                    succeeded=False,
                    error_type="ChapterInvalidatedNeedsUnblockError",
                )
                # 撞 invalidated gate → 自动 pause 不撤回。**不随失败策略
                # skip/retry**:invalidated 语义 = rewind 链上游被改写等人
                # unblock,跳过/重试都会放大衔接断裂(P6-B-01 语义保持)。
                _log.info(
                    "pipeline_pause_on_invalidated",
                    workspace_id=workspace_id,
                    run_id=run_id,
                    chapter_id=next_chapter,
                )
                self._auto_pause(
                    workspace_id,
                    run_id=run_id,
                    reason="chapter_invalidated",
                    blocked_chapter_id=next_chapter,
                )
                return
            except MemoryEnrichmentNotReadyError as exc:
                self._attempt_ledger.finish_pipeline_attempt(
                    workspace_id=workspace_id,
                    attempt_id=pipeline_attempt.attempt_id,
                    succeeded=False,
                    error_type=type(exc).__name__,
                )
                # 正文、review、ledger 与 ready 已完整物化；这里只是富记忆终态
                # 未成功。先按既有 operation_complete 合同释放章节操作租约，再
                # 暂停等待/修复，不能把它误报成“Provider 结果未知”。
                complete_operation = getattr(
                    self._chapter_service,
                    "complete_operation_task",
                    None,
                )
                try:
                    if callable(complete_operation):
                        complete_operation(
                            workspace_id,
                            next_chapter,
                            task_id=pipeline_attempt.task_id,
                        )
                except Exception as lease_exc:  # noqa: BLE001
                    _log.error(
                        "pipeline_memory_gate_operation_release_failed",
                        workspace_id=workspace_id,
                        run_id=run_id,
                        chapter_id=next_chapter,
                        error_type=type(lease_exc).__name__,
                        error_message=str(lease_exc),
                    )
                    self._auto_pause(
                        workspace_id,
                        run_id=run_id,
                        reason="chapter_operation_safety_block",
                        blocked_chapter_id=next_chapter,
                    )
                    return
                _log.warning(
                    "pipeline_memory_enrichment_paused",
                    workspace_id=workspace_id,
                    run_id=run_id,
                    chapter_id=next_chapter,
                    memory_enrichment_status=exc.status,
                    reason=exc.pause_reason,
                    error_message=str(exc),
                )
                self._auto_pause(
                    workspace_id,
                    run_id=run_id,
                    reason=exc.pause_reason,
                    blocked_chapter_id=next_chapter,
                    error_details=_memory_enrichment_pause_details(exc),
                )
                return
            except Exception as exc:  # noqa: BLE001
                self._attempt_ledger.finish_pipeline_attempt(
                    workspace_id=workspace_id,
                    attempt_id=pipeline_attempt.attempt_id,
                    succeeded=False,
                    error_type=type(exc).__name__,
                )
                release_if_local = getattr(
                    self._chapter_service,
                    "release_operation_task_if_never_dispatched",
                    None,
                )
                operation_task_released = True
                if callable(release_if_local):
                    operation_task_released = bool(
                        release_if_local(
                            workspace_id,
                            next_chapter,
                            task_id=pipeline_attempt.task_id,
                        )
                    )
                # v2 w1-s3:章级 cost gate(ChapterService._run_gates)抛
                # CostGuardTriggered → 与 Run 级预检同语义停法,不走失败
                # 策略(skip/retry 对全局成本态都是错误路由;两层 gate 行为
                # 一致)。局部 import 对齐 _run_gates 同模式(api.errors ↔
                # services 不顶层互引)。
                from src.api.errors import CostGuardTriggered

                if not operation_task_released:
                    _log.error(
                        "pipeline_chapter_operation_lease_retained",
                        workspace_id=workspace_id,
                        run_id=run_id,
                        chapter_id=next_chapter,
                        error_type=type(exc).__name__,
                        error_message=str(exc),
                    )
                    self._auto_pause(
                        workspace_id,
                        run_id=run_id,
                        reason="chapter_operation_safety_block",
                        blocked_chapter_id=next_chapter,
                    )
                    return
                if isinstance(
                    exc,
                    (RewriteCheckpointRecoveryError, ChapterOperationLeaseError),
                ):
                    _log.error(
                        "pipeline_chapter_operation_safety_paused",
                        workspace_id=workspace_id,
                        run_id=run_id,
                        chapter_id=next_chapter,
                        error_type=type(exc).__name__,
                        error_message=str(exc),
                    )
                    self._auto_pause(
                        workspace_id,
                        run_id=run_id,
                        reason="chapter_operation_safety_block",
                        blocked_chapter_id=next_chapter,
                    )
                    return
                if isinstance(exc, ContinuityCheckpointCorruptionError):
                    _log.error(
                        "pipeline_continuity_checkpoint_corruption_paused",
                        workspace_id=workspace_id,
                        run_id=run_id,
                        chapter_id=next_chapter,
                        error_message=str(exc),
                    )
                    self._auto_pause(
                        workspace_id,
                        run_id=run_id,
                        reason="continuity_checkpoint_corruption",
                        blocked_chapter_id=next_chapter,
                    )
                    return
                if isinstance(exc, ContinuityLedgerNotReadyError):
                    _log.warning(
                        "pipeline_continuity_checkpoint_paused",
                        workspace_id=workspace_id,
                        run_id=run_id,
                        chapter_id=next_chapter,
                        error_message=str(exc),
                    )
                    self._auto_pause(
                        workspace_id,
                        run_id=run_id,
                        reason="continuity_ledger_not_ready",
                        blocked_chapter_id=next_chapter,
                    )
                    return
                if isinstance(exc, CostGuardTriggered):
                    _log.warning(
                        "pipeline_cost_guard_blocked",
                        workspace_id=workspace_id,
                        run_id=run_id,
                        chapter_id=next_chapter,
                        source="chapter_gate",
                        error_message=str(exc),
                    )
                    self._auto_pause(
                        workspace_id,
                        run_id=run_id,
                        reason="cost_guard_exceeded",
                        blocked_chapter_id=next_chapter,
                    )
                    return
                if isinstance(exc, ProviderCostCapExceeded):
                    _log.warning(
                        "pipeline_selection_test_cost_cap_exceeded",
                        workspace_id=workspace_id,
                        run_id=run_id,
                        chapter_id=next_chapter,
                        configured_rmb_used=str(exc.used),
                        configured_rmb_cap=str(exc.cap),
                    )
                    self._auto_pause(
                        workspace_id,
                        run_id=run_id,
                        reason="selection_test_cost_cap_exceeded",
                        blocked_chapter_id=next_chapter,
                    )
                    return
                if isinstance(exc, ProviderCallCapExceeded):
                    _log.warning(
                        "pipeline_provider_call_cap_exceeded",
                        workspace_id=workspace_id,
                        run_id=run_id,
                        chapter_id=next_chapter,
                        provider_calls_used=exc.used,
                        provider_call_cap=exc.cap,
                    )
                    self._auto_pause(
                        workspace_id,
                        run_id=run_id,
                        reason="provider_call_cap_exceeded",
                        blocked_chapter_id=next_chapter,
                    )
                    return
                _log.error(
                    "pipeline_chapter_failed",
                    workspace_id=workspace_id,
                    run_id=run_id,
                    chapter_id=next_chapter,
                    error_type=type(exc).__name__,
                    error_message=str(exc),
                )
                if self._dispose_chapter_failure(
                    workspace_id,
                    run_id=run_id,
                    chapter_id=next_chapter,
                    failure_policy=failure_policy,
                    retried_chapters=retried_chapters,
                    stop_mode="error",
                    pause_reason=None,
                    error_details=_provider_exception_details(
                        exc,
                        chapter_id=next_chapter,
                    ),
                ):
                    return
                continue

            if card_failure is not None:
                self._attempt_ledger.finish_pipeline_attempt(
                    workspace_id=workspace_id,
                    attempt_id=pipeline_attempt.attempt_id,
                    succeeded=False,
                    error_type=str(card_failure["reason"]),
                )
                if self._dispose_chapter_failure(
                    workspace_id,
                    run_id=run_id,
                    chapter_id=next_chapter,
                    failure_policy=failure_policy,
                    retried_chapters=retried_chapters,
                    stop_mode=card_failure["stop_mode"],
                    pause_reason=card_failure["reason"],
                    error_details=card_failure["details"],
                ):
                    return
                continue

            # ---- 写后孤儿章感知(缺口 1:治带病续跑) ----------------------
            # write() 升 human_intervention 时**正常返回不抛错**
            # (chapter_service 只同步 escalation_details 后 return result),
            # 必须读 final_state 才能察觉孤儿章。getattr 兜底:legacy 测试
            # stub 返回 None 时视为成功(向后兼容)。
            if final_state == ChapterState.HUMAN_INTERVENTION:
                self._attempt_ledger.finish_pipeline_attempt(
                    workspace_id=workspace_id,
                    attempt_id=pipeline_attempt.attempt_id,
                    succeeded=False,
                    error_type="ChapterHumanIntervention",
                )
                _log.warning(
                    "pipeline_chapter_escalated",
                    workspace_id=workspace_id,
                    run_id=run_id,
                    chapter_id=next_chapter,
                    escalation_reason=getattr(result, "escalation_reason", None),
                    failure_policy=failure_policy,
                )
                disposition_terminal = self._dispose_chapter_failure(
                    workspace_id,
                    run_id=run_id,
                    chapter_id=next_chapter,
                    failure_policy=failure_policy,
                    retried_chapters=retried_chapters,
                    stop_mode="pause",
                    pause_reason="chapter_escalated",
                    error_details=None,
                )
                # 先把 run 的 pause/skip/retry disposition 持久化，再释放
                # 内层 operation lease；崩在二者之间时 startup 仍能从明确
                # outer checkpoint 分类，不能重入已物化的人工作业。
                complete_operation = getattr(
                    self._chapter_service,
                    "complete_operation_task",
                    None,
                )
                if callable(complete_operation):
                    complete_operation(
                        workspace_id,
                        next_chapter,
                        task_id=pipeline_attempt.task_id,
                    )
                if disposition_terminal:
                    return
                continue

            self._attempt_ledger.finish_pipeline_attempt(
                workspace_id=workspace_id,
                attempt_id=pipeline_attempt.attempt_id,
                succeeded=True,
            )
            # 本 invocation 内刚通过 ready checker；下一轮无需重复查同章。
            verified_terminal_ids.add(next_chapter)

            # 章节成功:append completed_chapter_ids + publish chapter_finished。
            # 已 dispatch 的在途请求可能在用户 Stop 后才返回；正文与账本必须
            # 如实收口，但 stopped run 是终态快照，不能再追加进度或发出一个
            # 晚于 pipeline_stopped 的 chapter_finished。
            completion_recorded = False
            completed_count = 0
            total_count = 0
            with self._lock:
                with self._open_session(workspace_id) as session:
                    orm = self._load_run_by_id(
                        session, workspace_id=workspace_id, run_id=run_id
                    )
                    if orm is not None and orm.state != "stopped":
                        new_completed = list(orm.completed_chapter_ids)
                        if next_chapter not in new_completed:
                            new_completed.append(next_chapter)
                            orm.completed_chapter_ids = new_completed
                            orm.updated_at = datetime.now(UTC)
                            session.commit()
                            completion_recorded = True
                        completed_count = len(new_completed)
                        total_count = len(orm.chapter_ids)

            complete_operation = getattr(
                self._chapter_service,
                "complete_operation_task",
                None,
            )
            if callable(complete_operation):
                complete_operation(
                    workspace_id,
                    next_chapter,
                    task_id=pipeline_attempt.task_id,
                )

            if not completion_recorded:
                return

            self._publish(
                event="chapter_finished",
                data={
                    "run_id": run_id,
                    "workspace_id": workspace_id,
                    "chapter_id": next_chapter,
                    "completed_count": completed_count,
                    "total_count": total_count,
                },
            )

        # 循环退出(全章完成):publish pipeline_stopped(reason=completed)
        assert completed_snapshot is not None
        self._publish(
            event="pipeline_stopped",
            data={
                "run_id": run_id,
                "workspace_id": workspace_id,
                "reason": "completed",
                "completed_chapter_ids": completed_snapshot.completed_chapter_ids,
            },
        )
        _log.info(
            "pipeline_completed",
            workspace_id=workspace_id,
            run_id=run_id,
            completed_count=len(completed_snapshot.completed_chapter_ids),
        )

    def _auto_pause(
        self,
        workspace_id: str,
        *,
        run_id: str,
        reason: str,
        blocked_chapter_id: str | None = None,
        error_details: dict[str, Any] | None = None,
    ) -> None:
        """内部自动暂停(invalidated gate / 其他 service 级暂停信号)。

        与公共 ``pause`` 端点入口分离:不抛 ``PipelineNotRunningError``
        (调用上下文已确认 active run 在跑),直接 UPDATE state + publish。
        """
        with self._lock:
            with self._open_session(workspace_id) as session:
                orm = self._load_run_by_id(
                    session, workspace_id=workspace_id, run_id=run_id
                )
                if orm is None or orm.state != "running":
                    # 用户 Pause/Stop、watchdog interrupted 或另一条内部终态
                    # 已先赢得竞态时，迟到的自动暂停不能覆盖其原因或复活 run。
                    return
                orm.state = "paused"
                orm.pause_reason = reason
                if (
                    blocked_chapter_id is not None
                    and blocked_chapter_id in orm.chapter_ids
                ):
                    # 前置章也在本 run scope 时，current 必须指回真正阻塞章，
                    # 后续显式 Continue 才能进入该章 checkpoint-only 恢复。
                    # scope 外前置章不能写入 current，否则会破坏 resume scope。
                    orm.current_chapter = blocked_chapter_id
                if error_details is not None:
                    orm.error_details = error_details
                orm.updated_at = datetime.now(UTC)
                session.commit()
                snapshot = _snapshot_from_orm(orm)

        self._publish(
            event="pipeline_paused",
            data={
                "run_id": run_id,
                "workspace_id": workspace_id,
                "reason": reason,
                "chapter_id": blocked_chapter_id,
                "current_chapter": snapshot.current_chapter,
                "completed_chapter_ids": snapshot.completed_chapter_ids,
            },
        )

    def _mark_error(
        self,
        *,
        workspace_id: str,
        run_id: str,
        error_details: dict[str, Any],
    ) -> None:
        """把指定 run 翻 ``state='error'`` + 写 ``error_details``(无 publish)。"""
        with self._lock:
            with self._open_session(workspace_id) as session:
                orm = self._load_run_by_id(
                    session, workspace_id=workspace_id, run_id=run_id
                )
                if orm is None or orm.state != "running":
                    return
                orm.state = "error"
                orm.error_details = error_details
                orm.updated_at = datetime.now(UTC)
                session.commit()

    # ---- v2 w1-s3:章级看门狗 + Run 级成本预检 -------------------------------

    def _begin_pipeline_attempt_if_running(
        self,
        *,
        workspace_id: str,
        run_id: str,
        chapter_id: str,
    ) -> AttemptLedgerORM | None:
        """把仍在 running 的复核与 pipeline attempt 开始做成进程内原子边界。

        pause / stop 共用 self._lock，所以一旦本方法在锁内开始账本，该章就
        已经进入在途语义；反之状态已变则返回 None，保证章节卡、Writer 或
        其他 Provider 请求都不会越过用户的章边界操作。
        """

        with self._lock:
            with self._open_session(workspace_id) as session:
                orm = self._load_run_by_id(
                    session,
                    workspace_id=workspace_id,
                    run_id=run_id,
                )
                if orm is None or orm.state != "running":
                    return None
            return self._attempt_ledger.begin_pipeline_attempt(
                workspace_id=workspace_id,
                run_id=run_id,
                chapter_id=chapter_id,
            )

    def _mark_interrupted(
        self,
        *,
        workspace_id: str,
        run_id: str,
        error_details: dict[str, Any],
    ) -> None:
        """把指定 run 从 ``running`` 翻 ``interrupted``(无 publish)。

        **只从 running 翻**:run 已被 pause / stop / error 处置时不覆盖
        (watchdog tick 与 pause / stop 端点存在天然竞争 — DB state 复核
        是兜底闸)。无专属 WS 事件:对齐 w1-s2
        ``mark_running_as_interrupted_on_startup`` 先例(API_CONTRACT §5.3
        六事件契约不扩),前端经 ``GET /status`` 的 ``state`` +
        ``error_details`` 感知(w1-s4 进度面板消费)。
        """
        with self._lock:
            with self._open_session(workspace_id) as session:
                orm = self._load_run_by_id(
                    session, workspace_id=workspace_id, run_id=run_id
                )
                if orm is None or orm.state != "running":
                    return
                orm.state = "interrupted"
                orm.error_details = error_details
                orm.updated_at = datetime.now(UTC)
                session.commit()

    def _ensure_watchdog_thread(self) -> None:
        """lazy 启动 watchdog daemon 线程(首个 execute 调用时;幂等)。

        - ``chapter_timeout_seconds <= 0`` → 禁用,不起线程。
        - daemon=True:进程退出自然回收,无显式 stop 协议(对齐"心跳表
          内存级,进程死亡场景由启动期改判兜底"的分工)。
        - 单测不经 ``execute`` 直调 ``_watchdog_tick(now)`` 验逻辑,不依赖
          线程节奏。
        """
        if self._watchdog_config.chapter_timeout_seconds <= 0:
            return
        with self._lock:
            if self._watchdog_thread is not None and self._watchdog_thread.is_alive():
                return
            self._watchdog_thread = threading.Thread(
                target=self._watchdog_loop,
                name="pipeline-watchdog",
                daemon=True,
            )
            self._watchdog_thread.start()

    def _watchdog_loop(self) -> None:
        """watchdog daemon 线程体:周期 tick,单次异常不杀线程。"""
        while True:
            time.sleep(_WATCHDOG_TICK_SECONDS)
            try:
                self._watchdog_tick(time.monotonic())
            except Exception as exc:  # noqa: BLE001 — 守护线程不死
                _log.error(
                    "pipeline_watchdog_tick_failed",
                    error_type=type(exc).__name__,
                    error_message=str(exc),
                )

    def _watchdog_tick(self, now: float) -> None:
        """单轮扫描:对每个"正在处理章且超预算"的心跳翻 run interrupted。

        翻态前锁内复核条目身份(对象同一性 — 循环已推进会换新对象)+ 置
        ``watchdog_flagged`` 防重复;DB 侧 ``_mark_interrupted`` 只从
        running 翻作第二道闸。章自然留非终态(drafting / reviewing / ...),
        由 w1-s2 ``reset_stale``(lifespan hook / POST /chapters:reset-stale)
        自愈 — 卡死线程不强杀,等其自然退出(其后 run 已非 running,循环
        顶部 SELECT 即退出)。

        :param now: ``time.monotonic()`` 当前值(参数化供单测直调)。
        """
        timeout = self._watchdog_config.chapter_timeout_seconds
        if timeout <= 0:
            return
        with self._lock:
            snapshot = dict(self._heartbeats)
        for ws_id, hb in snapshot.items():
            if hb.chapter_id is None or hb.watchdog_flagged:
                continue
            elapsed = now - hb.started_monotonic
            if elapsed <= timeout:
                continue
            with self._lock:
                cur = self._heartbeats.get(ws_id)
                if cur is not hb or cur.watchdog_flagged:
                    continue  # 循环已推进 / 其他 tick 已处理
                cur.watchdog_flagged = True
            _log.warning(
                "pipeline_chapter_watchdog_timeout",
                workspace_id=ws_id,
                run_id=hb.run_id,
                chapter_id=hb.chapter_id,
                elapsed_seconds=round(elapsed, 1),
                timeout_seconds=timeout,
            )
            self._mark_interrupted(
                workspace_id=ws_id,
                run_id=hb.run_id,
                error_details={
                    "reason": "chapter_watchdog_timeout",
                    "chapter_id": hb.chapter_id,
                    "elapsed_seconds": round(elapsed, 1),
                    "timeout_seconds": timeout,
                },
            )

    def _evaluate_run_cost_gate(self, workspace_id: str) -> dict[str, Any] | None:
        """Run 级成本预检(v2 w1-s3,P4-B-08 留观闭账)。

        预读本工作区 ``SUM(chapters.total_cost_usd)``(**累计真值账**,
        v2 w2-s2 metrics 修复 B 起;原 ``metrics_daily`` 当日行结构性低估
        — 孤儿章不入账 + approved 后处理漏记,w2-s1 报告 §7.1)调
        ``trigger_cost_guard_check``(structlog + WS ``system.alerts``
        ``cost_guard_triggered`` 事件由其内部按 ``notify_via`` 发出)。
        读法对齐章级 ``ChapterService._run_gates``:跨工作区 daily /
        monthly 聚合维度传 0.0(``MetricsService`` 跨工作区聚合留观,
        P4-B-08 §8;**两层 gate 同账同语义** — 本工作区累计成本,
        Run 级护栏语义"本书已花多少"与累计账天然贴合)。

        :returns: ``None`` = 放行(未启用 / 未触发 / action 为 warn 或
            continue — 后两者只通知不停跑);dict = 应停(命中维度详情,
            供 caller log + auto_pause)。
        """
        config = self._cost_guard_config
        # 局部 import 对齐 ChapterService._run_gates 同模式(避顶层 import
        # services.cost_guard 的 websocket 链路副作用)。
        from src.services.cost_guard import (
            CostGuardConfig,
            get_workspace_cost_guard_config,
            trigger_cost_guard_check,
        )

        if not isinstance(config, CostGuardConfig):
            return None  # None=disable(CLI / legacy 测试构造不注入)
        config = get_workspace_cost_guard_config(workspace_id, default=config)
        workspace_cost_usd = 0.0
        with self._open_session(workspace_id) as session:
            from sqlalchemy import func as sa_func
            from sqlalchemy import select as sa_select

            from src.core.db.models import ChapterORM

            total = session.execute(
                sa_select(
                    sa_func.coalesce(sa_func.sum(ChapterORM.total_cost_usd), 0.0)
                ).where(ChapterORM.workspace_id == workspace_id)
            ).scalar_one()
            workspace_cost_usd = float(total)
        attempt_cost = self._attempt_ledger.configured_cost_total(
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
            ws_manager=self._ws_manager,
        )
        if decision.triggered and decision.action == "pause":
            return {
                "scope": decision.scope,
                "current_usd": decision.current_usd,
                "limit_usd": decision.limit_usd,
                "cost_reason": decision.reason,
                "currency": decision.currency,
            }
        return None

    # ---- v2 w1-s1:失败策略路由 + skip 记账 + 写前卡保障 ---------------------

    def _dispose_chapter_failure(
        self,
        workspace_id: str,
        *,
        run_id: str,
        chapter_id: str,
        failure_policy: PipelineFailurePolicy,
        retried_chapters: set[str],
        stop_mode: Literal["pause", "error"],
        pause_reason: str | None,
        error_details: dict[str, Any] | None,
    ) -> bool:
        """章级失败统一路由(v2 w1-s1 缺口 4:失败策略可配)。

        三类失败(孤儿章 / 写章异常 / 卡失败)共用本路由,差异在
        ``stop_mode``(stop 档的停法):

        - ``pause``:孤儿章 / rejected 卡 — "等人工"语义,auto_pause 后
          人工解决可 ``:resume`` 原 run 续跑(completed 进度保留)。
        - ``error``:写章异常 / 卡生成失败 — 技术故障语义,对齐 P6-B-02
          既有行为(error 态 resume 闭环 = w1-s2 缺口 2 范围)。

        策略语义:

        - ``retry``:每次 execute/resume invocation 内每章最多一次自动重试；
          历史 attempt 继续 append-only 保留，但取得新证据或完成修复后的新
          invocation 不会被旧累计值永久终止。
        - ``skip``:记入 ``skipped_chapter_ids``(选章集合排除)继续。
        - ``stop``(默认):按 ``stop_mode`` 停整个 run。

        :returns: ``True`` = run 已停,调用方应 ``return`` 退出循环;
            ``False`` = 已记账 / 已授予重试,调用方 ``continue`` 重选章。
        """
        if failure_policy == "retry" and chapter_id not in retried_chapters:
            retried_chapters.add(chapter_id)
            historical_used = self._attempt_ledger.pipeline_attempts_used(
                workspace_id=workspace_id,
                chapter_id=chapter_id,
            )
            _log.info(
                "pipeline_chapter_retry",
                workspace_id=workspace_id,
                run_id=run_id,
                chapter_id=chapter_id,
                historical_attempts=historical_used,
                invocation_retry=1,
            )
            return False
        if failure_policy == "skip":
            self._record_skip(workspace_id, run_id=run_id, chapter_id=chapter_id)
            return False
        # stop(含 retry 名额耗尽回落)
        if stop_mode == "pause":
            self._auto_pause(
                workspace_id,
                run_id=run_id,
                reason=pause_reason or "chapter_escalated",
                blocked_chapter_id=chapter_id,
            )
        else:
            self._mark_error(
                workspace_id=workspace_id,
                run_id=run_id,
                error_details=error_details or {"chapter_id": chapter_id},
            )
        return True

    def _record_skip(self, workspace_id: str, *, run_id: str, chapter_id: str) -> None:
        """skip 策略:失败章记入 ``skipped_chapter_ids``(幂等)。

        跳过信息不 publish 专属 WS 事件(API_CONTRACT §5.3 的 6 事件契约
        不动):前端通过 ``GET /status`` 的 ``skipped_chapter_ids`` 字段
        感知(w1-s4 进度面板消费);structlog 留观测痕迹。
        """
        with self._lock:
            with self._open_session(workspace_id) as session:
                orm = self._load_run_by_id(
                    session, workspace_id=workspace_id, run_id=run_id
                )
                if orm is None or orm.state == "stopped":
                    return
                if chapter_id not in orm.skipped_chapter_ids:
                    orm.skipped_chapter_ids = list(orm.skipped_chapter_ids) + [
                        chapter_id
                    ]
                    orm.updated_at = datetime.now(UTC)
                    session.commit()
        _log.info(
            "pipeline_chapter_skipped",
            workspace_id=workspace_id,
            run_id=run_id,
            chapter_id=chapter_id,
        )

    def _ensure_chapter_card_ready(
        self, workspace_id: str, chapter_id: str
    ) -> dict[str, Any] | None:
        """写前卡保障(v2 w1-s1 缺口 3:卡生成入循环)。

        ``write()`` 的 card_status gate 要求卡必须 ``approved``(P5-B-06
        决策 6=C),否则抛 ``CardNotApproved`` — Run 一键跑全卷必须在循环
        内补齐"生成 + approve"两步(对齐 v1.1 acceptance driver"写前自动
        approve"实证模式,30 章长跑验证;Run = 用户授权批量自动化,手动
        逐章流程的审卡 gate 不受影响)。

        分支:

        - ``self._chapter_card_service is None``(legacy 测试 / lifespan
          builder 不注入)→ 直接放行,缺卡由 write() gate 兜底为异常路径。
        - ORM 行 ``card_status='approved'`` → 放行(零额外调用)。
        - ``rejected`` → **不自动绕过**(用户明确拒过此卡,自动 approve =
          违背用户意图)→ ``stop_mode=pause`` + reason=``card_rejected``,
          人工重生成 / 改卡后 resume。
        - ``draft`` / ``pending_review`` → 自动 approve。
        - 无 ORM 行(缺卡)→ ``generate_next_card`` 补齐再
          approve。**count=1 逐章生成**:卡上下文吃前 N 章 ChapterSummary
          (derive_context),逐章生成拿到最新摘要,质量优于提前批量。
        - 生成 / approve 异常 → ``stop_mode=error`` +
          reason=``card_generation_failed``。

        :returns: ``None`` = 卡就绪;失败返 ``{"stop_mode", "reason",
            "details"}`` 供 ``_dispose_chapter_failure`` 路由。
        """
        card_service = self._chapter_card_service
        if card_service is None:
            return None

        def _failure(
            reason: str,
            stop_mode: str,
            message: str,
            *,
            continuity_bridge_retry_exhausted: bool = False,
        ) -> dict[str, Any]:
            details: dict[str, Any] = {
                "reason": reason,
                "message": message,
                "chapter_id": chapter_id,
            }
            if continuity_bridge_retry_exhausted:
                details["continuity_bridge_retry_exhausted"] = True
            return {
                "stop_mode": stop_mode,
                "reason": reason,
                "details": details,
            }

        # 提交大纲已先登记自动首卡任务。它结束前不能另生成或批准首卡，
        # 否则两个异步结果会互相覆盖；完成后 Continue 复用已保存卡片。
        from src.api import task_registry

        if chapter_id == "ch-0001" and task_registry.has_active_tasks(
            workspace_id, task_types=("chapter_card_first_after_commit",)
        ):
            return _failure(
                "card_generation_in_progress",
                "pause",
                "提交大纲后的首卡仍在生成，请等待该任务完成后继续；不会重复出卡。",
            )

        try:
            with self._open_session(workspace_id) as session:
                stmt = (
                    select(ChapterCardORM)
                    .where(ChapterCardORM.chapter_id == chapter_id)
                    .where(ChapterCardORM.workspace_id == workspace_id)
                    .limit(1)
                )
                row = session.scalars(stmt).one_or_none()
                card_status = row.card_status if row is not None else None
        except Exception as exc:  # noqa: BLE001
            return _failure(
                "card_generation_failed",
                "error",
                f"读卡状态失败: {type(exc).__name__}: {exc}",
            )

        if card_status == "approved":
            return None
        if card_status == "rejected":
            _log.warning(
                "pipeline_card_rejected_blocks",
                workspace_id=workspace_id,
                chapter_id=chapter_id,
            )
            return _failure(
                "card_rejected",
                "pause",
                "章节卡已被用户拒绝(rejected),Run 不自动绕过;"
                "请重新生成或人工 approve 后 resume",
            )

        if card_status is None:
            # 缺卡:循环内只生成当前章；上一章富记忆闸已在进入本章前通过。
            task_id = task_registry.new_task_id()
            task_registry.record_pending(
                task_id,
                type="chapter_card_generate_next",
                workspace_id=workspace_id,
            )
            try:
                result = card_service.generate_next_card(
                    workspace_id,
                    chapter_id=chapter_id,
                    task_id=task_id,
                )
            except Exception as exc:  # noqa: BLE001
                return _failure(
                    "card_generation_failed",
                    "error",
                    f"循环内卡生成失败: {type(exc).__name__}: {exc}",
                )
            if chapter_id not in result.get("cards_generated", []):
                return _failure(
                    "card_generation_failed",
                    "error",
                    f"循环内卡生成未产出本章卡(cards_failed="
                    f"{result.get('cards_failed')!r})",
                )
            _log.info(
                "pipeline_card_generated_in_loop",
                workspace_id=workspace_id,
                chapter_id=chapter_id,
            )

        # draft / pending_review / 刚生成的 draft → 自动 approve
        try:
            card_service.transition_card_status(
                workspace_id, chapter_id, action="approve"
            )
        except Exception as exc:  # noqa: BLE001
            # 已 approved 的竞态(非法源转移)= 幂等放行;其余 = 失败。
            # 局部 import 规避顶层依赖面扩大(对齐 _run_gates 同模式)。
            from src.services.chapter_card_service import (
                CardContinuityBridgeRequiredError,
                CardContinuityLedgerStaleError,
                CardInvalidStateTransitionError,
            )

            def _retry_continuity_bridge(
                bridge_exc: CardContinuityBridgeRequiredError,
            ) -> None:
                card_service.regenerate_continuity_bridge_unapproved_card(
                    workspace_id,
                    chapter_id,
                    required_actions=bridge_exc.required_actions,
                )
                card_service.transition_card_status(
                    workspace_id,
                    chapter_id,
                    action="approve",
                )
                _log.info(
                    "pipeline_continuity_bridge_card_retry_succeeded",
                    workspace_id=workspace_id,
                    chapter_id=chapter_id,
                    required_action_count=len(bridge_exc.required_actions),
                )

            if isinstance(exc, CardInvalidStateTransitionError):
                return None
            if isinstance(exc, CardContinuityLedgerStaleError):
                bridge_retry_attempted = False
                try:
                    card_service.regenerate_stale_unapproved_card(
                        workspace_id,
                        chapter_id,
                    )
                    try:
                        card_service.transition_card_status(
                            workspace_id,
                            chapter_id,
                            action="approve",
                        )
                    except CardContinuityBridgeRequiredError as bridge_exc:
                        bridge_retry_attempted = True
                        _retry_continuity_bridge(bridge_exc)
                except Exception as retry_exc:  # noqa: BLE001
                    return _failure(
                        "card_generation_failed",
                        "error",
                        "循环内按 continuity ledger 重生成/批准失败: "
                        f"{type(retry_exc).__name__}: {retry_exc}",
                        continuity_bridge_retry_exhausted=(
                            bridge_retry_attempted
                        ),
                    )
                _log.info(
                    "pipeline_stale_card_regenerated_from_ledger",
                    workspace_id=workspace_id,
                    chapter_id=chapter_id,
                )
                return None
            if isinstance(exc, CardContinuityBridgeRequiredError):
                try:
                    _retry_continuity_bridge(exc)
                except Exception as retry_exc:  # noqa: BLE001
                    return _failure(
                        "card_generation_failed",
                        "error",
                        "循环内按 continuity bridge 拒绝证据重生成/批准失败: "
                        f"{type(retry_exc).__name__}: {retry_exc}",
                        continuity_bridge_retry_exhausted=True,
                    )
                return None
            return _failure(
                "card_generation_failed",
                "error",
                f"循环内卡 approve 失败: {type(exc).__name__}: {exc}",
            )
        _log.info(
            "pipeline_card_auto_approved",
            workspace_id=workspace_id,
            chapter_id=chapter_id,
        )
        return None

    # ---- lifespan 启动 hook ------------------------------------------------

    def mark_running_as_interrupted_on_startup(
        self,
        workspace_ids: Iterable[str],
        *,
        apply: bool = True,
    ) -> int:
        """扫给定 workspace 列表,把残留 ``state='running'`` 行改判翻态。

        进程崩溃重启时,active 行的 BackgroundTasks 已死,留在 ``running``
        是僵尸状态。v2 w1-s2 改判(原 ``mark_running_as_error_on_startup``
        一律翻 ``error`` 死路 — resume 不认 error,只能 stop 后重 start、
        completed 进度清零,闭合 v2_plan §2.2 缺口 2):

        - ``chapter_ids`` 非空(断点可定位,循环选章自会从 completed ∪
          skipped ∪ 章终态之后推进)→ ``interrupted``,**可 :resume 续跑**;
        - ``chapter_ids`` 空(行数据不完整,理论不可达 — start 时空列表
          直接抛 ``PipelineInvalidScopeError``)→ ``error`` 兜底。

        两者均带 ``error_details={"reason": "engine_restarted_mid_run",
        "interrupted_chapter": current_chapter}`` 让前端 GET /status 拿到
        清晰中断原因 + 中断时正在写的章;不自动续跑 — user 手动 resume
        (中断章由 lifespan 后续的 reset_stale 扫描自愈回 drafting)。

        ``apply=False`` 只读分类且逐 session rollback，供 lifespan 在任何启动
        写入前完成全工作区 preflight。任一工作区不可读或出现多个 active run
        都直接抛错，不能留下永久 running 僵尸后继续接收请求。

        返回值:将翻态的行数(interrupted + error,供 lifespan 调用方 log)。
        """
        count = 0
        for ws_id in workspace_ids:
            with self._open_session(ws_id) as session:
                active_rows = list(
                    session.scalars(
                        select(PipelineRunORM)
                        .where(PipelineRunORM.workspace_id == ws_id)
                        .where(PipelineRunORM.state.in_(_ACTIVE_STATES))
                    )
                )
                if len(active_rows) > 1:
                    raise PipelineServiceError(
                        "启动恢复发现同一工作区存在多个 active pipeline run，"
                        f"已保留现场并阻止后端启动: workspace={ws_id}"
                    )
                rows = [row for row in active_rows if row.state == "running"]
                for row in rows:
                    now = datetime.now(UTC)
                    resumable = bool(row.chapter_ids)
                    row.state = "interrupted" if resumable else "error"
                    row.error_details = {
                        "reason": "engine_restarted_mid_run",
                        "interrupted_chapter": row.current_chapter,
                    }
                    row.updated_at = now
                    count += 1
                if rows:
                    if apply:
                        session.commit()
                    else:
                        session.rollback()
        if count > 0:
            _log.info("pipeline_engine_restarted_marked", count=count)
        return count

    def reconcile_operation_leases_on_startup(
        self,
        workspace_ids: Iterable[str],
        *,
        apply: bool = True,
        cancelled_task_check: Callable[[str, str, str], bool] | None = None,
    ) -> tuple[int, int]:
        """在 reset_stale 前收口安全孤儿租约，保留一切未知现场。"""

        reconciler = getattr(
            self._chapter_service,
            "reconcile_operation_leases_on_startup",
            None,
        )
        if not callable(reconciler):
            return 0, 0
        released_total = 0
        retained_total = 0
        for workspace_id in workspace_ids:
            if apply:
                released, retained = reconciler(
                    workspace_id,
                    cancelled_task_check=cancelled_task_check,
                )
            else:
                released, retained = reconciler(
                    workspace_id,
                    apply=False,
                    cancelled_task_check=cancelled_task_check,
                )
            released_total += len(released)
            retained_total += len(retained)
        return released_total, retained_total

    # ---- 内部:session + ORM 查询 helper ------------------------------------

    def _open_session(self, workspace_id: str) -> "_SessionContext":
        """开一个工作区 SQLite Session(``with`` 语句自动 close)。"""
        return _SessionContext(self._session_factory(workspace_id))

    def _with_card_repair_readiness(
        self,
        session: Session,
        *,
        row: PipelineRunORM,
        snapshot: PipelineRunSnapshot,
    ) -> PipelineRunSnapshot:
        """给 status 派生卡片修复恢复状态；只读且不改历史错误详情。"""

        details = dict(snapshot.error_details or {})
        details["recovery_kind"] = _CARD_REPAIR_RECOVERY_KIND
        try:
            _require_pipeline_resume_scope_safe(row=row, run_id=row.run_id)
            self._require_repaired_card_resume_safe(
                session,
                workspace_id=row.workspace_id,
                row=row,
            )
        except PipelineNotRunningError:
            details["recovery_ready"] = False
            details["recovery_block_reason"] = "chapter_card_not_approved"
        except ContinuityCheckpointCorruptionError:
            details["recovery_ready"] = False
            details["recovery_block_reason"] = "checkpoint_not_clean"
        else:
            details["recovery_ready"] = True
            details.pop("recovery_block_reason", None)
        return snapshot.model_copy(update={"error_details": details})

    def _with_provider_review_readiness(
        self,
        session: Session,
        *,
        row: PipelineRunORM,
        snapshot: PipelineRunSnapshot,
    ) -> PipelineRunSnapshot:
        """给 status 派生 Reviewer checkpoint 恢复状态；不改历史错误。"""

        details = dict(snapshot.error_details or {})
        details["recovery_kind"] = _PROVIDER_REVIEW_RECOVERY_KIND
        try:
            _require_pipeline_resume_scope_safe(row=row, run_id=row.run_id)
            self._require_provider_review_resume_safe(
                session,
                workspace_id=row.workspace_id,
                row=row,
            )
        except ContinuityCheckpointCorruptionError:
            details["recovery_ready"] = False
            details["recovery_block_reason"] = "checkpoint_not_clean"
        else:
            details["recovery_ready"] = True
            details.pop("recovery_block_reason", None)
        return snapshot.model_copy(update={"error_details": details})

    def _with_core_state_recovery_readiness(
        self,
        session: Session,
        *,
        row: PipelineRunORM,
        snapshot: PipelineRunSnapshot,
    ) -> PipelineRunSnapshot:
        """给 status 派生首章核心基线恢复状态；不改原错误账本。"""

        details = dict(snapshot.error_details or {})
        details["recovery_kind"] = _CORE_STATE_RECOVERY_KIND
        try:
            _require_pipeline_resume_scope_safe(row=row, run_id=row.run_id)
            summary_refresh = self._require_core_state_resume_safe(
                session,
                workspace_id=row.workspace_id,
                row=row,
            )
        except PipelineNotRunningError:
            details["recovery_ready"] = False
            details["recovery_block_reason"] = "card_not_reapproved"
        except (ContinuityCheckpointCorruptionError, CoreStateServiceError):
            details["recovery_ready"] = False
            details["recovery_block_reason"] = "checkpoint_not_clean"
        else:
            details["recovery_ready"] = True
            details["summary_refresh_required"] = summary_refresh
            details.pop("recovery_block_reason", None)
        return snapshot.model_copy(update={"error_details": details})

    def _with_core_state_transition_recovery_readiness(
        self,
        session: Session,
        *,
        row: PipelineRunORM,
        snapshot: PipelineRunSnapshot,
    ) -> PipelineRunSnapshot:
        """派生非首章 transition 候选本地 checkpoint 的恢复状态。"""

        details = dict(snapshot.error_details or {})
        details["recovery_kind"] = _CORE_STATE_TRANSITION_RECOVERY_KIND
        try:
            _require_pipeline_resume_scope_safe(row=row, run_id=row.run_id)
            summary_refresh = self._require_core_state_transition_resume_safe(
                session,
                workspace_id=row.workspace_id,
                row=row,
            )
        except (
            PipelineNotRunningError,
            ContinuityCheckpointCorruptionError,
            CoreStateServiceError,
        ):
            details["recovery_ready"] = False
            details["recovery_block_reason"] = "checkpoint_not_clean"
        else:
            details["recovery_ready"] = True
            details["summary_refresh_required"] = summary_refresh
            details.pop("recovery_block_reason", None)
        return snapshot.model_copy(update={"error_details": details})

    def _require_core_state_resume_safe(
        self,
        session: Session,
        *,
        workspace_id: str,
        row: PipelineRunORM,
    ) -> bool:
        """CAS/Worker 前验证只剩 approved checkpoint 的核心状态本地提交。"""

        chapter_id = row.current_chapter
        details = row.error_details or {}
        evidence = [
            f"run_id={row.run_id}",
            f"current_chapter={chapter_id!r}",
            f"error_type={details.get('error_type')!r}",
            f"error_chapter_id={details.get('chapter_id')!r}",
            f"completed_chapter_ids={row.completed_chapter_ids!r}",
            f"skipped_chapter_ids={row.skipped_chapter_ids!r}",
        ]
        if (
            chapter_id != "ch-0001"
            or details.get("error_type") != _CORE_STATE_ERROR_TYPE
            or details.get("chapter_id") != chapter_id
            or chapter_id in row.completed_chapter_ids
            or chapter_id in row.skipped_chapter_ids
        ):
            raise ContinuityCheckpointCorruptionError(
                chapter_id=chapter_id or "(pipeline-run)",
                reason="核心状态恢复的错误身份或 run 进度账本不一致",
                evidence=evidence,
            )
        if workspace_id in self._heartbeats:
            raise ContinuityCheckpointCorruptionError(
                chapter_id=chapter_id,
                reason="核心状态恢复仍检测到同工作区旧 Worker 心跳",
                evidence=evidence,
            )

        card_row = session.get(ChapterCardORM, chapter_id)
        if (
            card_row is None
            or card_row.workspace_id != workspace_id
            or card_row.card_status != "approved"
            or card_row.card_approved_at is None
        ):
            raise PipelineNotRunningError(
                workspace_id=workspace_id,
                action="resume_core_state_card_not_reapproved",
            )
        chapter = session.get(ChapterORM, chapter_id)
        if (
            chapter is None
            or chapter.workspace_id != workspace_id
            or chapter.status != ChapterState.APPROVED.value
            or not isinstance(chapter.current_version, int)
            or isinstance(chapter.current_version, bool)
            or chapter.current_version < 1
            or chapter.ready_for_next_chapter_at is not None
            or chapter.memory_enrichment_status != "pending"
            or chapter.core_state_commit_status != "pending"
        ):
            raise ContinuityCheckpointCorruptionError(
                chapter_id=chapter_id,
                reason="核心状态恢复章节不是唯一 approved/pending checkpoint",
                evidence=[
                    *evidence,
                    f"chapter_status={getattr(chapter, 'status', None)!r}",
                    f"current_version={getattr(chapter, 'current_version', None)!r}",
                    "ready_at="
                    f"{getattr(chapter, 'ready_for_next_chapter_at', None)!r}",
                    "core_status="
                    f"{getattr(chapter, 'core_state_commit_status', None)!r}",
                ],
            )

        card_reader = getattr(self._chapter_service, "read_chapter_card", None)
        if not callable(card_reader):
            raise ContinuityCheckpointCorruptionError(
                chapter_id=chapter_id,
                reason="核心状态恢复缺少章节卡 SSOT 读取入口",
                evidence=evidence,
            )
        try:
            card = card_reader(workspace_id, chapter_id)
        except Exception as exc:  # noqa: BLE001
            raise ContinuityCheckpointCorruptionError(
                chapter_id=chapter_id,
                reason=(
                    "核心状态恢复无法读取章节卡 SSOT: "
                    f"{type(exc).__name__}: {exc}"
                ),
                evidence=evidence,
            ) from exc
        if (
            getattr(card, "workspace_id", None) != workspace_id
            or getattr(card, "chapter_id", None) != chapter_id
            or getattr(card, "card_status", None) != "approved"
        ):
            raise ContinuityCheckpointCorruptionError(
                chapter_id=chapter_id,
                reason="核心状态恢复章节卡 DB/SSOT 身份或状态不一致",
                evidence=evidence,
            )
        pending_details = dict(chapter.core_state_commit_details or {})
        same_projection_summary_refresh = (
            pending_details.get("card_projection_hash")
            == getattr(card, "core_state_projection_hash", None)
        )

        paths = WorkspacePaths(
            workspace_id,
            workspaces_root=self._workspace_service.workspaces_root,
        )
        version = session.execute(
            select(ChapterVersionORM)
            .where(ChapterVersionORM.chapter_id == chapter_id)
            .order_by(ChapterVersionORM.version_id.desc())
            .limit(1)
        ).scalar_one_or_none()
        final_path = paths.chapter_version_file(chapter_id, "final.md")
        version_path = paths.chapter_version_file(
            chapter_id, f"v{chapter.current_version}.md"
        )
        try:
            final_text = final_path.read_text(encoding="utf-8")
            version_text = version_path.read_text(encoding="utf-8")
        except (OSError, UnicodeError) as exc:
            raise ContinuityCheckpointCorruptionError(
                chapter_id=chapter_id,
                reason="核心状态恢复正文 checkpoint 不可读",
                evidence=[*evidence, str(exc)],
            ) from exc
        if (
            version is None
            or version.version_id != chapter.current_version
            or version.content != final_text
            or version_text != final_text
        ):
            raise ContinuityCheckpointCorruptionError(
                chapter_id=chapter_id,
                reason="核心状态恢复的 DB/vN/final 正文不一致",
                evidence=evidence,
            )

        try:
            core_service = CoreStateService(session_factory=self._session_factory)
            if same_projection_summary_refresh:
                core_service.require_pending_transition_candidate_recovery_safe(
                    session=session, workspace_id=workspace_id, card=card, paths=paths
                )
            else:
                core_service.require_pending_conflict_recovery_safe(
                    session=session, workspace_id=workspace_id, card=card, paths=paths
                )
        except CoreStateServiceError as exc:
            raise ContinuityCheckpointCorruptionError(
                chapter_id=chapter_id,
                reason=f"核心状态恢复合同未闭合: {exc}",
                evidence=evidence,
            ) from exc

        lease = session.get(
            ChapterOperationLeaseORM,
            (workspace_id, chapter_id),
        )
        if lease is not None:
            raise ContinuityCheckpointCorruptionError(
                chapter_id=chapter_id,
                reason="核心状态恢复检测到未收口章节租约",
                evidence=[*evidence, f"lease_id={lease.lease_id}"],
            )
        started_attempt_ids = list(
            session.scalars(
                select(AttemptLedgerORM.attempt_id)
                .where(AttemptLedgerORM.workspace_id == workspace_id)
                .where(AttemptLedgerORM.status == "started")
                .order_by(AttemptLedgerORM.created_at)
            )
        )
        if started_attempt_ids:
            raise ContinuityCheckpointCorruptionError(
                chapter_id=chapter_id,
                reason="核心状态恢复检测到结果未知的 started attempt",
                evidence=[*evidence, f"attempt_ids={started_attempt_ids!r}"],
            )
        duplicate_attempt = session.execute(
            select(
                AttemptLedgerORM.logical_call_id,
                AttemptLedgerORM.attempt_number,
                func.count(),
            )
            .where(AttemptLedgerORM.workspace_id == workspace_id)
            .where(AttemptLedgerORM.attempt_kind == "provider")
            .where(AttemptLedgerORM.logical_call_id.is_not(None))
            .group_by(
                AttemptLedgerORM.logical_call_id,
                AttemptLedgerORM.attempt_number,
            )
            .having(func.count() > 1)
            .limit(1)
        ).first()
        if duplicate_attempt is not None:
            raise ContinuityCheckpointCorruptionError(
                chapter_id=chapter_id,
                reason="核心状态恢复检测到重复 Provider 物理 attempt 账本",
                evidence=[*evidence, f"duplicate_attempt={duplicate_attempt!r}"],
            )
        return same_projection_summary_refresh

    def _require_core_state_transition_resume_safe(
        self,
        session: Session,
        *,
        workspace_id: str,
        row: PipelineRunORM,
    ) -> bool:
        """CAS/Worker 前验证非首章只剩已批准 transition 的本地提交。"""

        chapter_id = row.current_chapter
        details = row.error_details or {}
        evidence = [
            f"run_id={row.run_id}",
            f"current_chapter={chapter_id!r}",
            f"error_type={details.get('error_type')!r}",
            f"error_chapter_id={details.get('chapter_id')!r}",
            f"completed_chapter_ids={row.completed_chapter_ids!r}",
            f"skipped_chapter_ids={row.skipped_chapter_ids!r}",
        ]
        if (
            not isinstance(chapter_id, str)
            or not chapter_id.strip()
            or chapter_id == "ch-0001"
            or details.get("error_type") != _CORE_STATE_ERROR_TYPE
            or details.get("chapter_id") != chapter_id
            or chapter_id in row.completed_chapter_ids
            or chapter_id in row.skipped_chapter_ids
        ):
            raise ContinuityCheckpointCorruptionError(
                chapter_id=chapter_id or "(pipeline-run)",
                reason="非首章核心状态恢复的错误身份或 run 进度账本不一致",
                evidence=evidence,
            )
        if workspace_id in self._heartbeats:
            raise ContinuityCheckpointCorruptionError(
                chapter_id=chapter_id,
                reason="非首章核心状态恢复仍检测到同工作区旧 Worker 心跳",
                evidence=evidence,
            )

        card_row = session.get(ChapterCardORM, chapter_id)
        chapter = session.get(ChapterORM, chapter_id)
        if (
            card_row is None
            or card_row.workspace_id != workspace_id
            or card_row.card_status != "approved"
            or card_row.card_approved_at is None
            or chapter is None
            or chapter.workspace_id != workspace_id
            or chapter.status != ChapterState.APPROVED.value
            or not isinstance(chapter.current_version, int)
            or isinstance(chapter.current_version, bool)
            or chapter.current_version < 1
            or chapter.ready_for_next_chapter_at is not None
            or chapter.memory_enrichment_status != "pending"
            or chapter.core_state_commit_status != "pending"
        ):
            raise ContinuityCheckpointCorruptionError(
                chapter_id=chapter_id,
                reason="非首章核心状态恢复章节不是唯一 approved/pending checkpoint",
                evidence=[
                    *evidence,
                    f"card_status={getattr(card_row, 'card_status', None)!r}",
                    f"chapter_status={getattr(chapter, 'status', None)!r}",
                    f"current_version={getattr(chapter, 'current_version', None)!r}",
                    "ready_at="
                    f"{getattr(chapter, 'ready_for_next_chapter_at', None)!r}",
                    "core_status="
                    f"{getattr(chapter, 'core_state_commit_status', None)!r}",
                ],
            )

        card_reader = getattr(self._chapter_service, "read_chapter_card", None)
        if not callable(card_reader):
            raise ContinuityCheckpointCorruptionError(
                chapter_id=chapter_id,
                reason="非首章核心状态恢复缺少章节卡 SSOT 读取入口",
                evidence=evidence,
            )
        try:
            card = card_reader(workspace_id, chapter_id)
        except Exception as exc:  # noqa: BLE001
            raise ContinuityCheckpointCorruptionError(
                chapter_id=chapter_id,
                reason=(
                    "非首章核心状态恢复无法读取章节卡 SSOT: "
                    f"{type(exc).__name__}: {exc}"
                ),
                evidence=evidence,
            ) from exc
        if (
            getattr(card, "workspace_id", None) != workspace_id
            or getattr(card, "chapter_id", None) != chapter_id
            or getattr(card, "card_status", None) != "approved"
        ):
            raise ContinuityCheckpointCorruptionError(
                chapter_id=chapter_id,
                reason="非首章核心状态恢复章节卡 DB/SSOT 身份或状态不一致",
                evidence=evidence,
            )
        pending_details = dict(chapter.core_state_commit_details or {})
        if (
            pending_details.get("card_projection_hash")
            != getattr(card, "core_state_projection_hash", None)
        ):
            raise ContinuityCheckpointCorruptionError(
                chapter_id=chapter_id,
                reason="非首章核心状态恢复的原错误与当前批准卡不再同源",
                evidence=evidence,
            )

        paths = WorkspacePaths(
            workspace_id,
            workspaces_root=self._workspace_service.workspaces_root,
        )
        version = session.execute(
            select(ChapterVersionORM)
            .where(ChapterVersionORM.chapter_id == chapter_id)
            .order_by(ChapterVersionORM.version_id.desc())
            .limit(1)
        ).scalar_one_or_none()
        final_path = paths.chapter_version_file(chapter_id, "final.md")
        version_path = paths.chapter_version_file(
            chapter_id, f"v{chapter.current_version}.md"
        )
        try:
            final_text = final_path.read_text(encoding="utf-8")
            version_text = version_path.read_text(encoding="utf-8")
        except (OSError, UnicodeError) as exc:
            raise ContinuityCheckpointCorruptionError(
                chapter_id=chapter_id,
                reason="非首章核心状态恢复正文 checkpoint 不可读",
                evidence=[*evidence, str(exc)],
            ) from exc
        if (
            version is None
            or version.version_id != chapter.current_version
            or version.content != final_text
            or version_text != final_text
        ):
            raise ContinuityCheckpointCorruptionError(
                chapter_id=chapter_id,
                reason="非首章核心状态恢复的 DB/vN/final 正文不一致",
                evidence=evidence,
            )

        try:
            summary_refresh = CoreStateService(
                session_factory=self._session_factory
            ).require_pending_transition_candidate_recovery_safe(
                session=session,
                workspace_id=workspace_id,
                card=card,
                paths=paths,
            )
        except CoreStateServiceError as exc:
            raise ContinuityCheckpointCorruptionError(
                chapter_id=chapter_id,
                reason=f"非首章核心状态恢复合同未闭合: {exc}",
                evidence=evidence,
            ) from exc

        lease = session.get(
            ChapterOperationLeaseORM,
            (workspace_id, chapter_id),
        )
        if lease is not None:
            raise ContinuityCheckpointCorruptionError(
                chapter_id=chapter_id,
                reason="非首章核心状态恢复检测到未收口章节租约",
                evidence=[*evidence, f"lease_id={lease.lease_id}"],
            )
        started_attempt_ids = list(
            session.scalars(
                select(AttemptLedgerORM.attempt_id)
                .where(AttemptLedgerORM.workspace_id == workspace_id)
                .where(AttemptLedgerORM.status == "started")
                .order_by(AttemptLedgerORM.created_at)
            )
        )
        if started_attempt_ids:
            raise ContinuityCheckpointCorruptionError(
                chapter_id=chapter_id,
                reason="非首章核心状态恢复检测到结果未知的 started attempt",
                evidence=[*evidence, f"attempt_ids={started_attempt_ids!r}"],
            )
        duplicate_attempt = session.execute(
            select(
                AttemptLedgerORM.logical_call_id,
                AttemptLedgerORM.attempt_number,
                func.count(),
            )
            .where(AttemptLedgerORM.workspace_id == workspace_id)
            .where(AttemptLedgerORM.attempt_kind == "provider")
            .where(AttemptLedgerORM.logical_call_id.is_not(None))
            .group_by(
                AttemptLedgerORM.logical_call_id,
                AttemptLedgerORM.attempt_number,
            )
            .having(func.count() > 1)
            .limit(1)
        ).first()
        if duplicate_attempt is not None:
            raise ContinuityCheckpointCorruptionError(
                chapter_id=chapter_id,
                reason="非首章核心状态恢复检测到重复 Provider 物理 attempt 账本",
                evidence=[*evidence, f"duplicate_attempt={duplicate_attempt!r}"],
            )

        return summary_refresh

    def _require_known_review_resume_safe(
        self, session: Session, *, workspace_id: str, row: PipelineRunORM,
    ) -> None:
        from src.services.known_review_response_recovery import (
            is_known_review_recovery_input_error,
            load_known_empty_must_recovery,
        )

        chapter_id = row.current_chapter
        chapter = session.get(ChapterORM, chapter_id) if chapter_id else None
        paths = WorkspacePaths(
            workspace_id, workspaces_root=self._workspace_service.workspaces_root,
        )
        if (
            not is_known_review_recovery_input_error(row)
            or workspace_id in self._heartbeats
            or chapter is None
            or session.get(ChapterOperationLeaseORM, (workspace_id, chapter_id)) is not None
            or load_known_empty_must_recovery(
                session, paths=paths, workspace_id=workspace_id,
                chapter_id=chapter.chapter_id, current_version=chapter.current_version,
            ) is None
        ):
            raise ContinuityCheckpointCorruptionError(
                chapter_id=chapter_id or "(pipeline-run)",
                reason="已归档审核恢复的正文、响应或物理请求账本无法验真",
                evidence=[f"run_id={row.run_id}"],
            )

    def _require_provider_review_resume_safe(
        self,
        session: Session,
        *,
        workspace_id: str,
        row: PipelineRunORM,
    ) -> None:
        """CAS/Worker 前验证只有最新正文已落库、Reviewer 尚未落账。"""

        chapter_id = row.current_chapter
        details = row.error_details or {}
        evidence = [
            f"run_id={row.run_id}",
            f"current_chapter={chapter_id!r}",
            f"error_type={details.get('error_type')!r}",
            f"error_chapter_id={details.get('chapter_id')!r}",
            f"completed_chapter_ids={row.completed_chapter_ids!r}",
            f"skipped_chapter_ids={row.skipped_chapter_ids!r}",
        ]
        if workspace_id in self._heartbeats:
            raise ContinuityCheckpointCorruptionError(
                chapter_id=chapter_id or "(pipeline-run)",
                reason="Provider Reviewer 恢复仍检测到同工作区旧 Worker 心跳",
                evidence=evidence,
            )
        if (
            not isinstance(chapter_id, str)
            or not chapter_id.strip()
            or details.get("error_type") not in _PROVIDER_REVIEW_ERROR_TYPES
            or details.get("chapter_id") != chapter_id
            or chapter_id in row.completed_chapter_ids
            or chapter_id in row.skipped_chapter_ids
        ):
            raise ContinuityCheckpointCorruptionError(
                chapter_id=chapter_id or "(pipeline-run)",
                reason="Provider Reviewer 错误定位与 run 进度账本不一致",
                evidence=evidence,
            )

        chapter = session.get(ChapterORM, chapter_id)
        if (
            chapter is None
            or chapter.workspace_id != workspace_id
            # lifespan 的 reset_stale 会把进程中断遗留的 reviewing
            # 归一化为 drafting，但保留版本、审稿与磁盘 checkpoint。只有
            # 本方法下方全部证据仍证明“最新正文已落、Reviewer 未落账”时，
            # 这个 drafting 才与 reviewing 等价并可恢复；普通草稿仍会被
            # run 错误身份、版本和账本闸挡住。
            or chapter.status
            not in {
                ChapterState.REVIEWING.value,
                ChapterState.DRAFTING.value,
            }
            or not isinstance(chapter.current_version, int)
            or isinstance(chapter.current_version, bool)
            or chapter.current_version < 1
            or chapter.approved_at is not None
            or chapter.ready_for_next_chapter_at is not None
        ):
            raise ContinuityCheckpointCorruptionError(
                chapter_id=chapter_id,
                reason="Provider 恢复章节不是唯一可重审的正文 checkpoint",
                evidence=[
                    *evidence,
                    f"chapter_exists={chapter is not None}",
                    f"chapter_workspace={getattr(chapter, 'workspace_id', None)!r}",
                    f"chapter_status={getattr(chapter, 'status', None)!r}",
                    f"current_version={getattr(chapter, 'current_version', None)!r}",
                ],
            )

        latest = session.execute(
            select(ChapterVersionORM)
            .where(ChapterVersionORM.chapter_id == chapter_id)
            .order_by(ChapterVersionORM.version_id.desc())
            .limit(1)
        ).scalar_one_or_none()
        if (
            latest is None
            or latest.version_id != chapter.current_version
            or latest.review_score is not None
        ):
            raise ContinuityCheckpointCorruptionError(
                chapter_id=chapter_id,
                reason="Provider 恢复的 DB 最新版本/审稿投影不干净",
                evidence=[
                    *evidence,
                    f"chapter_current_version={chapter.current_version}",
                    f"latest_version={getattr(latest, 'version_id', None)!r}",
                    f"latest_review_score={getattr(latest, 'review_score', None)!r}",
                ],
            )

        if details.get("error_type") in _PROVIDER_REVIEW_TRANSPORT_ERROR_TYPES:
            # 连接错误也可能出现在 Writer/其他阶段；必须由同 run 最新
            # Provider 账本证明它发生于当前正文落库后的 Reviewer。
            last_provider = session.execute(
                select(AttemptLedgerORM)
                .where(AttemptLedgerORM.workspace_id == workspace_id)
                .where(AttemptLedgerORM.run_id == row.run_id)
                .where(AttemptLedgerORM.chapter_id == chapter_id)
                .where(AttemptLedgerORM.attempt_kind == "provider")
                .where(AttemptLedgerORM.created_at >= latest.created_at)
                .order_by(AttemptLedgerORM.created_at.desc())
                .limit(1)
            ).scalar_one_or_none()
            if (
                last_provider is None
                or last_provider.agent_name != "reviewer"
                or last_provider.status != "failed"
                or last_provider.error_type != details.get("error_type")
                or last_provider.completed_at is None
            ):
                raise ContinuityCheckpointCorruptionError(
                    chapter_id=chapter_id,
                    reason="连接中断缺少当前正文的已结束 Reviewer 失败账本",
                    evidence=[
                        *evidence,
                        f"last_provider={getattr(last_provider, 'attempt_id', None)!r}",
                    ],
                )

        current_review = session.execute(
            select(ReviewResultORM.review_id)
            .where(ReviewResultORM.chapter_id == chapter_id)
            .where(ReviewResultORM.version_reviewed == chapter.current_version)
            .limit(1)
        ).scalar_one_or_none()
        if current_review is not None:
            raise ContinuityCheckpointCorruptionError(
                chapter_id=chapter_id,
                reason="Provider 恢复版本已有 Reviewer 结果，禁止重复付费审稿",
                evidence=[*evidence, f"review_id={current_review}"],
            )

        paths = WorkspacePaths(
            workspace_id,
            workspaces_root=self._workspace_service.workspaces_root,
        )
        version_path = paths.chapter_version_file(
            chapter_id,
            f"v{chapter.current_version}.md",
        )
        final_path = paths.chapter_version_file(chapter_id, "final.md")
        try:
            disk_content = version_path.read_text(encoding="utf-8")
        except (OSError, UnicodeError) as exc:
            raise ContinuityCheckpointCorruptionError(
                chapter_id=chapter_id,
                reason="Provider 恢复版本文件缺失或无法按 UTF-8 读取",
                evidence=[*evidence, f"version_path={version_path}"],
            ) from exc
        if disk_content != latest.content or final_path.exists():
            raise ContinuityCheckpointCorruptionError(
                chapter_id=chapter_id,
                reason="Provider 恢复的磁盘/DB 正文或 final checkpoint 不一致",
                evidence=[
                    *evidence,
                    f"version_path={version_path}",
                    f"disk_matches_db={disk_content == latest.content}",
                    f"final_exists={final_path.exists()}",
                ],
            )

        lease = session.get(
            ChapterOperationLeaseORM,
            (workspace_id, chapter_id),
        )
        if lease is not None:
            raise ContinuityCheckpointCorruptionError(
                chapter_id=chapter_id,
                reason="Provider Reviewer 恢复检测到未收口章节租约",
                evidence=[*evidence, f"lease_id={lease.lease_id}"],
            )

        started_attempt_ids = list(
            session.scalars(
                select(AttemptLedgerORM.attempt_id)
                .where(AttemptLedgerORM.workspace_id == workspace_id)
                .where(AttemptLedgerORM.status == "started")
                .order_by(AttemptLedgerORM.created_at)
            )
        )
        if started_attempt_ids:
            raise ContinuityCheckpointCorruptionError(
                chapter_id=chapter_id,
                reason="Provider Reviewer 恢复检测到结果未知的 started attempt",
                evidence=[*evidence, f"attempt_ids={started_attempt_ids!r}"],
            )

    def _require_repaired_card_resume_safe(
        self,
        session: Session,
        *,
        workspace_id: str,
        row: PipelineRunORM,
    ) -> None:
        """在 CAS/Worker/新 attempt 前验证卡修复或 bridge 重试现场为空净。"""

        chapter_id = row.current_chapter
        details = row.error_details or {}
        evidence = [
            f"run_id={row.run_id}",
            f"current_chapter={chapter_id!r}",
            f"error_chapter_id={details.get('chapter_id')!r}",
            f"completed_chapter_ids={row.completed_chapter_ids!r}",
            f"skipped_chapter_ids={row.skipped_chapter_ids!r}",
        ]
        if (
            not isinstance(chapter_id, str)
            or not chapter_id.strip()
            or details.get("reason") != _RECOVERABLE_CARD_ERROR_REASON
            or details.get("chapter_id") != chapter_id
            or chapter_id in row.completed_chapter_ids
            or chapter_id in row.skipped_chapter_ids
        ):
            raise ContinuityCheckpointCorruptionError(
                chapter_id=chapter_id or "(pipeline-run)",
                reason="章节卡修复 run 的错误定位或进度账本不一致",
                evidence=evidence,
            )

        card_row = session.get(ChapterCardORM, chapter_id)
        bridge_retry = _is_continuity_bridge_card_retry_error(row)
        card_is_approved = bool(
            card_row is not None
            and card_row.card_status == "approved"
            and card_row.card_approved_at is not None
        )
        card_is_bridge_retryable = bool(
            bridge_retry
            and card_row is not None
            and card_row.card_status in {"draft", "pending_review"}
            and card_row.card_approved_at is None
        )
        if (
            card_row is None
            or card_row.workspace_id != workspace_id
            or not (card_is_approved or card_is_bridge_retryable)
        ):
            raise PipelineNotRunningError(
                workspace_id=workspace_id,
                action="resume_card_repair_not_approved",
            )

        paths = WorkspacePaths(
            workspace_id,
            workspaces_root=self._workspace_service.workspaces_root,
        )
        card_path = paths.cards_dir / f"{chapter_id}.json"
        if not card_path.is_file():
            raise ContinuityCheckpointCorruptionError(
                chapter_id=chapter_id,
                reason="已批准章节卡缺少文件 SSOT，禁止恢复 Worker",
                evidence=[*evidence, f"card_path={card_path}"],
            )
        card_reader = getattr(self._chapter_service, "read_chapter_card", None)
        if callable(card_reader):
            try:
                card = card_reader(workspace_id, chapter_id)
            except Exception as exc:  # noqa: BLE001
                raise ContinuityCheckpointCorruptionError(
                    chapter_id=chapter_id,
                    reason=(
                        "已批准章节卡文件无法按产品 schema 读取，禁止恢复 Worker: "
                        f"{type(exc).__name__}: {exc}"
                    ),
                    evidence=evidence,
                ) from exc
            if (
                getattr(card, "chapter_id", None) != chapter_id
                or getattr(card, "workspace_id", None) != workspace_id
                or getattr(card, "card_status", None) != card_row.card_status
            ):
                raise ContinuityCheckpointCorruptionError(
                    chapter_id=chapter_id,
                    reason="章节卡 DB 与文件 SSOT 的身份或状态不一致",
                    evidence=evidence,
                )

        chapter_row = session.get(ChapterORM, chapter_id)
        version_count = int(
            session.scalar(
                select(func.count())
                .select_from(ChapterVersionORM)
                .where(ChapterVersionORM.chapter_id == chapter_id)
            )
            or 0
        )
        review_count = int(
            session.scalar(
                select(func.count())
                .select_from(ReviewResultORM)
                .where(ReviewResultORM.chapter_id == chapter_id)
            )
            or 0
        )
        chapter_dir = paths.chapter_dir(chapter_id)
        version_files = (
            sorted(path.name for path in chapter_dir.glob("v*.md"))
            if chapter_dir.is_dir()
            else []
        )
        final_exists = paths.chapter_version_file(chapter_id, "final.md").is_file()
        has_writing_evidence = any(
            (
                chapter_row is not None,
                version_count,
                review_count,
                version_files,
                final_exists,
            )
        )
        if has_writing_evidence:
            raise ContinuityCheckpointCorruptionError(
                chapter_id=chapter_id,
                reason="卡片生成失败之后出现正文、版本或审稿证据，禁止按写前断点恢复",
                evidence=[
                    *evidence,
                    f"chapter_row_exists={chapter_row is not None}",
                    f"version_count={version_count}",
                    f"review_count={review_count}",
                    f"version_files={version_files}",
                    f"final_exists={final_exists}",
                ],
            )

        lease = session.get(
            ChapterOperationLeaseORM,
            (workspace_id, chapter_id),
        )
        if lease is not None:
            raise ContinuityCheckpointCorruptionError(
                chapter_id=chapter_id,
                reason="卡片修复恢复检测到未收口章节租约",
                evidence=[
                    *evidence,
                    f"lease_id={lease.lease_id}",
                    f"lease_phase={lease.phase}",
                ],
            )

        started_attempt_ids = list(
            session.scalars(
                select(AttemptLedgerORM.attempt_id)
                .where(AttemptLedgerORM.workspace_id == workspace_id)
                .where(AttemptLedgerORM.status == "started")
                .order_by(AttemptLedgerORM.created_at)
            )
        )
        if started_attempt_ids:
            raise ContinuityCheckpointCorruptionError(
                chapter_id=chapter_id,
                reason="卡片修复恢复检测到结果未知的 started attempt",
                evidence=[*evidence, f"attempt_ids={started_attempt_ids!r}"],
            )

        duplicate_attempt = session.execute(
            select(
                AttemptLedgerORM.logical_call_id,
                AttemptLedgerORM.attempt_number,
                func.count(),
            )
            .where(AttemptLedgerORM.workspace_id == workspace_id)
            .where(AttemptLedgerORM.attempt_kind == "provider")
            .where(AttemptLedgerORM.logical_call_id.is_not(None))
            .group_by(
                AttemptLedgerORM.logical_call_id,
                AttemptLedgerORM.attempt_number,
            )
            .having(func.count() > 1)
            .limit(1)
        ).first()
        if duplicate_attempt is not None:
            raise ContinuityCheckpointCorruptionError(
                chapter_id=chapter_id,
                reason="卡片修复恢复检测到重复 Provider 物理 attempt 账本",
                evidence=[*evidence, f"duplicate_attempt={duplicate_attempt!r}"],
            )

    def _load_active_run(
        self, session: Session, workspace_id: str
    ) -> PipelineRunORM | None:
        """SELECT active run(state IN ('running','paused'))LIMIT 1。"""
        stmt = (
            select(PipelineRunORM)
            .where(PipelineRunORM.workspace_id == workspace_id)
            .where(PipelineRunORM.state.in_(_ACTIVE_STATES))
            .limit(1)
        )
        return session.scalars(stmt).one_or_none()

    def _load_active_or_error_run(
        self, session: Session, workspace_id: str
    ) -> PipelineRunORM | None:
        """SELECT active 或 error run(:stop 允许把 error 行翻 stopped)。"""
        stmt = (
            select(PipelineRunORM)
            .where(PipelineRunORM.workspace_id == workspace_id)
            .where(PipelineRunORM.state.in_(_ACTIVE_STATES + ("error",)))
            .order_by(PipelineRunORM.updated_at.desc())
            .limit(1)
        )
        return session.scalars(stmt).one_or_none()

    def _load_recoverable_error_run(
        self, session: Session, workspace_id: str
    ) -> PipelineRunORM | None:
        """返回最近 run 中唯一可保留原 run 的窄 error。"""
        row = self._load_last_run(session, workspace_id)
        if row is None or _pipeline_error_recovery_kind(row) is None:
            return None
        return row

    def _load_last_run(
        self, session: Session, workspace_id: str
    ) -> PipelineRunORM | None:
        """SELECT 最近 1 行(ORDER BY updated_at DESC,任何 state)。"""
        stmt = (
            select(PipelineRunORM)
            .where(PipelineRunORM.workspace_id == workspace_id)
            .order_by(PipelineRunORM.updated_at.desc())
            .limit(1)
        )
        return session.scalars(stmt).one_or_none()

    def _load_run_by_id(
        self, session: Session, *, workspace_id: str, run_id: str
    ) -> PipelineRunORM | None:
        """SELECT 按 run_id PK(workspace_id 一致校验)。"""
        stmt = (
            select(PipelineRunORM)
            .where(PipelineRunORM.run_id == run_id)
            .where(PipelineRunORM.workspace_id == workspace_id)
            .limit(1)
        )
        return session.scalars(stmt).one_or_none()

    # ---- scope 解析 ---------------------------------------------------------

    def _resolve_chapters(
        self,
        *,
        workspace_id: str,
        scope: PipelineScope,
        start_chapter_id: str | None,
        end_chapter_id: str | None,
        volume_id: str | None,
    ) -> list[str]:
        """scope 4 档 → chapter_ids 列表。

        - ``single``:[start_chapter_id](需 start_chapter_id 非空)
        - ``range`` / ``volume`` / ``until``:基于 VolumeORM × chapter_count
          全局展开的章节序列再过滤

        非法 → ``PipelineInvalidScopeError 422``。
        """
        if scope == "single":
            if not start_chapter_id:
                raise PipelineInvalidScopeError(
                    scope=scope,
                    reason="scope=single 需提供 start_chapter_id",
                )
            return [start_chapter_id]

        # 需要全局章节展开
        all_chapters = self._enumerate_all_chapters(workspace_id)
        if not all_chapters:
            raise PipelineInvalidScopeError(
                scope=scope,
                reason="workspace 无任何 volume(outline 未 commit?)",
            )
        chapter_ids_in_order = [c["chapter_id"] for c in all_chapters]

        if scope == "range":
            if not start_chapter_id or not end_chapter_id:
                raise PipelineInvalidScopeError(
                    scope=scope,
                    reason="scope=range 需同时提供 start_chapter_id 与 end_chapter_id",
                )
            if start_chapter_id not in chapter_ids_in_order:
                raise PipelineInvalidScopeError(
                    scope=scope,
                    reason=f"start_chapter_id={start_chapter_id} 不存在",
                    details={"start_chapter_id": start_chapter_id},
                )
            if end_chapter_id not in chapter_ids_in_order:
                raise PipelineInvalidScopeError(
                    scope=scope,
                    reason=f"end_chapter_id={end_chapter_id} 不存在",
                    details={"end_chapter_id": end_chapter_id},
                )
            if start_chapter_id > end_chapter_id:
                raise PipelineInvalidScopeError(
                    scope=scope,
                    reason="start_chapter_id 必须 ≤ end_chapter_id(lex 序)",
                )
            return [
                c
                for c in chapter_ids_in_order
                if start_chapter_id <= c <= end_chapter_id
            ]

        if scope == "volume":
            if not volume_id:
                raise PipelineInvalidScopeError(
                    scope=scope,
                    reason="scope=volume 需提供 volume_id",
                )
            chapters_in_volume = [
                c["chapter_id"] for c in all_chapters if c["volume_id"] == volume_id
            ]
            if not chapters_in_volume:
                raise PipelineInvalidScopeError(
                    scope=scope,
                    reason=f"volume_id={volume_id} 不存在或无章节",
                    details={"volume_id": volume_id},
                )
            return chapters_in_volume

        # scope == "until"
        if not start_chapter_id:
            raise PipelineInvalidScopeError(
                scope=scope,
                reason="scope=until 需提供 start_chapter_id",
            )
        if start_chapter_id not in chapter_ids_in_order:
            raise PipelineInvalidScopeError(
                scope=scope,
                reason=f"start_chapter_id={start_chapter_id} 不存在",
                details={"start_chapter_id": start_chapter_id},
            )
        resolved_end = end_chapter_id
        if resolved_end is None:
            # 默认到 start_chapter_id 所在卷末(自决项 6)
            start_volume_id = next(
                c["volume_id"]
                for c in all_chapters
                if c["chapter_id"] == start_chapter_id
            )
            same_volume = [
                c["chapter_id"]
                for c in all_chapters
                if c["volume_id"] == start_volume_id
            ]
            resolved_end = same_volume[-1]
        elif resolved_end not in chapter_ids_in_order:
            raise PipelineInvalidScopeError(
                scope=scope,
                reason=f"end_chapter_id={resolved_end} 不存在",
                details={"end_chapter_id": resolved_end},
            )
        if start_chapter_id > resolved_end:
            raise PipelineInvalidScopeError(
                scope=scope,
                reason="start_chapter_id 必须 ≤ end_chapter_id(lex 序)",
            )
        return [
            c for c in chapter_ids_in_order if start_chapter_id <= c <= resolved_end
        ]

    def _enumerate_all_chapters(self, workspace_id: str) -> list[dict[str, Any]]:
        """读 VolumeORM × chapter_count → 全局 [{chapter_id, chapter_number,
        volume_id}, ...]。

        对齐 P5-B-06 ``_collect_first_n_chapters`` 模式:跨卷连续编号
        ``ch-{global_idx:04d}``;chapter_number 是卷内 1-based。
        """
        session = self._session_factory(workspace_id)
        try:
            volumes = (
                session.query(VolumeORM)
                .filter(VolumeORM.workspace_id == workspace_id)
                .order_by(VolumeORM.volume_id)
                .all()
            )
        finally:
            session.close()

        result: list[dict[str, Any]] = []
        global_idx = 0
        for vol in volumes:
            for chapter_number in range(1, vol.chapter_count + 1):
                global_idx += 1
                result.append(
                    {
                        "chapter_id": f"ch-{global_idx:04d}",
                        "chapter_number": chapter_number,
                        "volume_id": vol.volume_id,
                    }
                )
        return result

    # ---- WS publish helper -------------------------------------------------

    def _publish(self, *, event: str, data: dict[str, Any]) -> None:
        """WS publish 到 ``pipeline.events``;失败 swallow + log(对齐 P6-B-01
        ``_publish_chapter_event`` swallow 模式)。"""
        if self._ws_manager is None:
            return
        try:
            self._ws_manager.publish(
                channel=pipeline_events_channel(),
                event=event,
                data=data,
            )
        except Exception as exc:  # noqa: BLE001
            _log.error(
                "pipeline_event_notify_failed",
                ws_event=event,
                error_type=type(exc).__name__,
                error_message=str(exc),
            )


class _SessionContext:
    """简单 ``with`` 上下文管理器:进入返 Session,退出 close。

    封装是为了让 service 方法用 ``with self._open_session(ws) as session:``
    语法,自动 close 防泄漏。失败路径同样 close。
    """

    def __init__(self, session: Session) -> None:
        self._session = session

    def __enter__(self) -> Session:
        return self._session

    def __exit__(self, *exc: Any) -> None:
        self._session.close()


__all__ = [
    "PipelineAlreadyRunningError",
    "PipelineInvalidScopeError",
    "PipelineNotRunningError",
    "PipelineRunSnapshot",
    "PipelineScope",
    "PipelineService",
    "PipelineServiceError",
    "PipelineState",
]
