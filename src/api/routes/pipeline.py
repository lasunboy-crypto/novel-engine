"""流水线运行控制路由(P6-B-02)。

对齐 API_CONTRACT §4.5 + plan §"P6-B-02"字面 + §"P6 端点表"5-9:

| 方法 | 路径 | 作用 |
|------|------|------|
| POST | ``/pipeline:start``   | 启动新 run(202 + run_id;后台跑章节序列)|
| POST | ``/pipeline:pause``   | 暂停 active run(幂等)|
| POST | ``/pipeline:resume``  | 恢复 paused run(幂等;重启后台循环)|
| POST | ``/pipeline:stop``    | 终止 active run(幂等)|
| GET  | ``/pipeline/status``  | 读当前 / 最近一次 run 状态 |

工作区上下文
============

P1-B-19 决策 1 沿用:所有 ``/pipeline:*`` 端点强制带 ``X-Workspace-Id`` header,
通过 ``Depends(get_workspace_id)`` 解析。缺失 → ``MissingWorkspaceHeader (422)``。

任务模式
========

``:start`` 端点返 ``202 ACCEPTED`` + ``{run_id, ...}``(对齐 API_CONTRACT §4.5
字面 ``→ 202, data: { run_id }``);实际章节循环由 ``BackgroundTasks`` 在响应已
发出后调 ``PipelineService.execute(workspace_id, run_id)``(对齐 P5-B-06
``ChapterCardService.generate_first_card_after_commit`` 同模式 — BackgroundTasks
顶层 swallow 防 starlette 二次响应错误)。

``:resume`` 端点同理:翻 state=running 后 BackgroundTasks 重新调 ``execute``
继续未完章节。
"""

from __future__ import annotations

from fastapi import APIRouter, BackgroundTasks, Body, Depends, status

from src.api import task_registry
from src.api.deps import get_pipeline_service, get_workspace_id
from src.api.schemas.common import SuccessEnvelope
from src.api.schemas.pipeline import (
    ActualBilledCostOut,
    AttemptUsageCostOut,
    ConfiguredCostEstimateOut,
    PipelinePauseIn,
    PipelinePauseOut,
    PipelineResumeIn,
    PipelineResumeOut,
    PipelineStartIn,
    PipelineStartOut,
    PipelineStatusOut,
    PipelineStopIn,
    PipelineStopOut,
)
from src.services.attempt_ledger import ProviderUsageCostSummary
from src.services.post_v8_abc_selection_activity import (
    bind_selection_or_production_scope,
    record_product_checkpoint,
)
from src.services.pipeline_service import (
    PipelineRunSnapshot,
    PipelineService,
)

router = APIRouter(prefix="/pipeline", tags=["pipeline"])


def _usage_cost_out(summary: ProviderUsageCostSummary) -> AttemptUsageCostOut:
    return AttemptUsageCostOut(
        usage_known_attempts=summary.usage_known_attempts,
        usage_unknown_attempts=summary.usage_unknown_attempts,
        legacy_unclassified_attempts=summary.legacy_unclassified_attempts,
        input_tokens_known=summary.input_tokens_known,
        output_tokens_known=summary.output_tokens_known,
        provider_request_id_count=summary.provider_request_id_count,
        response_status_counts=summary.response_status_counts,
        configured_cost_basis_counts=summary.configured_cost_basis_counts,
        configured_estimates=[
            ConfiguredCostEstimateOut(currency=currency, amount=amount)
            for currency, amount in summary.configured_estimates.items()
        ],
        actual_billed=ActualBilledCostOut(),
    )


def _snapshot_to_status_out(
    snapshot: PipelineRunSnapshot,
    usage_cost: ProviderUsageCostSummary,
) -> PipelineStatusOut:
    """``PipelineRunSnapshot`` → ``PipelineStatusOut`` 字段映射。

    抽出 helper 让 4 个 mutation 端点(start/pause/resume/stop)与 GET status
    共用同一份字段映射,避免遗漏字段漂移。``state=idle`` 占位 snapshot 的
    ``scope`` / ``run_id`` 字段按对外契约清洗为 None。
    """
    is_idle = snapshot.state == "idle" and not snapshot.run_id
    return PipelineStatusOut(
        run_id=None if is_idle else snapshot.run_id,
        workspace_id=snapshot.workspace_id,
        state=snapshot.state,
        scope=None if is_idle else snapshot.scope,
        chapter_ids=snapshot.chapter_ids,
        current_chapter=snapshot.current_chapter,
        progress=snapshot.progress,
        completed_chapter_ids=snapshot.completed_chapter_ids,
        profile_override=snapshot.profile_override,
        pause_reason=snapshot.pause_reason,
        error_details=snapshot.error_details,
        failure_policy=snapshot.failure_policy,
        skipped_chapter_ids=snapshot.skipped_chapter_ids,
        provider_call_cap=snapshot.provider_call_cap,
        provider_call_count=snapshot.provider_call_count,
        transport_retry_count=snapshot.transport_retry_count,
        pipeline_attempt_count=snapshot.pipeline_attempt_count,
        upstream_generation_attempt_count=(
            snapshot.upstream_generation_attempt_count
        ),
        attempt_usage_cost=_usage_cost_out(usage_cost),
    )


def _run_pipeline_execute_task(
    service: PipelineService,
    workspace_id: str,
    run_id: str,
    *,
    explicit_resume: bool = False,
) -> None:
    """v1.0.x wave 6 s2.6a:pipeline execute BackgroundTask wrapper(入口防御)。

    工作区已销毁 → 直接 early-return,不调 service.execute(避免 SQLite / chroma
    路径已 rmtree 触发抛错)。pipeline 走 ``_active_runs`` 状态机 + ``pipeline.events``
    频道,不依赖 task_registry;workspace 销毁场景下 user 重启工作区即可。

    正常路径(工作区健在)→ 调 service.execute,内部已 swallow 异常 + 翻
    state=error,顶层 BackgroundTasks 不会因异常二次响应。
    """
    if task_registry.is_workspace_destroyed(workspace_id):
        return
    service.execute(
        workspace_id,
        run_id,
        explicit_resume=explicit_resume,
    )


def _snapshot_to_pause_out(
    snapshot: PipelineRunSnapshot,
    usage_cost: ProviderUsageCostSummary,
) -> PipelinePauseOut:
    """``PipelineRunSnapshot`` → ``PipelinePauseOut`` (与 ``PipelineResumeOut``
    / ``PipelineStopOut`` 同形)。"""
    is_idle = snapshot.state == "idle" and not snapshot.run_id
    return PipelinePauseOut(
        run_id=None if is_idle else snapshot.run_id,
        workspace_id=snapshot.workspace_id,
        state=snapshot.state,
        scope=None if is_idle else snapshot.scope,
        chapter_ids=snapshot.chapter_ids,
        current_chapter=snapshot.current_chapter,
        progress=snapshot.progress,
        completed_chapter_ids=snapshot.completed_chapter_ids,
        profile_override=snapshot.profile_override,
        pause_reason=snapshot.pause_reason,
        error_details=snapshot.error_details,
        failure_policy=snapshot.failure_policy,
        skipped_chapter_ids=snapshot.skipped_chapter_ids,
        provider_call_cap=snapshot.provider_call_cap,
        provider_call_count=snapshot.provider_call_count,
        transport_retry_count=snapshot.transport_retry_count,
        pipeline_attempt_count=snapshot.pipeline_attempt_count,
        upstream_generation_attempt_count=(
            snapshot.upstream_generation_attempt_count
        ),
        attempt_usage_cost=_usage_cost_out(usage_cost),
    )


@router.post(
    ":start",
    response_model=SuccessEnvelope[PipelineStartOut],
    status_code=status.HTTP_202_ACCEPTED,
    summary="启动流水线 run",
    description=(
        "启动新 pipeline run。scope 4 档(``single`` / ``range`` / ``volume`` "
        "/ ``until``)— 见 ``PipelineStartIn`` schema。返 202 + ``{run_id, ...}``。"
        "实际章节循环走 ``BackgroundTasks`` 在响应已发出后执行;前端 RunControlPage "
        "订阅 WS ``pipeline.events`` 频道接 6 类事件实时刷新进度。"
        "已有 active run → 409 ``PIPELINE_ALREADY_RUNNING``;scope 字段不合法 "
        "→ 422 ``PIPELINE_INVALID_SCOPE``。需 X-Workspace-Id header。"
    ),
)
async def start_pipeline(
    background_tasks: BackgroundTasks,
    body: PipelineStartIn,
    workspace_id: str = Depends(get_workspace_id),
    service: PipelineService = Depends(get_pipeline_service),
) -> SuccessEnvelope[PipelineStartOut]:
    with bind_selection_or_production_scope(
        workspace_id,
        task_id=f"pipeline-start:{workspace_id}",
        provider_call_cap=body.provider_call_cap,
    ):
        snapshot = service.start(
            workspace_id,
            scope=body.scope,
            start_chapter_id=body.start_chapter_id,
            end_chapter_id=body.end_chapter_id,
            volume_id=body.volume_id,
            profile_override=body.profile_override,
            failure_policy=body.failure_policy,
            provider_call_cap=body.provider_call_cap,
        )
    try:
        record_product_checkpoint(workspace_id=workspace_id, run_id=snapshot.run_id)
    except Exception:
        # 已创建的 run 不得在活动 checkpoint 未落盘时进入后台请求；暂停保留
        # DB 现场，修复活动文件后可从正常 Resume UI 继续。
        service.pause(
            workspace_id,
            reason="selection_activity_checkpoint_persist_failed",
        )
        raise
    # 后台跑章节循环(响应已发出后启动;BackgroundTasks 顶层 swallow 在
    # PipelineService.execute 内)
    background_tasks.add_task(
        _run_pipeline_execute_task, service, workspace_id, snapshot.run_id
    )
    return SuccessEnvelope(
        data=PipelineStartOut(
            run_id=snapshot.run_id,
            workspace_id=snapshot.workspace_id,
            state=snapshot.state,
            scope=snapshot.scope,
            chapter_ids=snapshot.chapter_ids,
            failure_policy=snapshot.failure_policy,
            provider_call_cap=snapshot.provider_call_cap,
        )
    )


@router.post(
    ":pause",
    response_model=SuccessEnvelope[PipelinePauseOut],
    summary="暂停 active 流水线",
    description=(
        "暂停 active run(running → paused)。**幂等**:已 paused 二次 pause "
        "200 返当前 snapshot 不重复 publish。无 active run → 409 "
        "``PIPELINE_NOT_RUNNING``。需 X-Workspace-Id。"
    ),
)
async def pause_pipeline(
    body: PipelinePauseIn = Body(default_factory=PipelinePauseIn),
    workspace_id: str = Depends(get_workspace_id),
    service: PipelineService = Depends(get_pipeline_service),
) -> SuccessEnvelope[PipelinePauseOut]:
    _ = body
    snapshot = service.pause(workspace_id, reason="user_request")
    usage_cost = service.summarize_attempt_usage_cost(
        workspace_id,
        run_id=snapshot.run_id,
    )
    return SuccessEnvelope(data=_snapshot_to_pause_out(snapshot, usage_cost))


@router.post(
    ":resume",
    response_model=SuccessEnvelope[PipelineResumeOut],
    summary="恢复可续跑流水线",
    description=(
        "恢复 paused / interrupted run(→ running,v2 w1-s2 起 interrupted "
        "断点续跑)，或最近一条 error_type=ContinuityLedgerNotReadyError 的"
        "历史 run；仅赢得原子状态迁移的请求通过 BackgroundTasks 重启 execute，"
        "避免重复 Worker/请求。续跑前会复核 terminal 章 ledger/hash；"
        "continuity_ledger_not_ready 或 continuity_checkpoint_corruption 会暂停"
        "并保留现场。**幂等**:已 running 二次 resume 200 返当前 snapshot。"
        "无 active run → 409 ``PIPELINE_NOT_RUNNING``。需 X-Workspace-Id。"
    ),
)
async def resume_pipeline(
    background_tasks: BackgroundTasks,
    body: PipelineResumeIn = Body(default_factory=PipelineResumeIn),
    workspace_id: str = Depends(get_workspace_id),
    service: PipelineService = Depends(get_pipeline_service),
) -> SuccessEnvelope[PipelineResumeOut]:
    _ = body
    snapshot, transitioned = service.resume_with_transition(workspace_id)
    # DB CAS transition owner 才能启动 execute；幂等 loser 只返回 snapshot。
    if transitioned and snapshot.state == "running":
        try:
            record_product_checkpoint(
                workspace_id=workspace_id,
                run_id=snapshot.run_id,
            )
        except Exception:
            service.pause(
                workspace_id,
                reason="selection_activity_checkpoint_persist_failed",
            )
            raise
        background_tasks.add_task(
            _run_pipeline_execute_task,
            service,
            workspace_id,
            snapshot.run_id,
            explicit_resume=True,
        )
    usage_cost = service.summarize_attempt_usage_cost(
        workspace_id,
        run_id=snapshot.run_id,
    )
    return SuccessEnvelope(data=_snapshot_to_pause_out(snapshot, usage_cost))


@router.post(
    ":stop",
    response_model=SuccessEnvelope[PipelineStopOut],
    summary="终止 active 流水线",
    description=(
        "终止 active run(running / paused / interrupted / error → stopped),"
        "清除 active 状态(允许后续 ``:start`` 启新 run)。**幂等**:已 stopped "
        "不能再 stop(``_active_runs`` 已清除)→ 409 ``PIPELINE_NOT_RUNNING``;"
        "无 active run 同样 409。需 X-Workspace-Id。"
    ),
)
async def stop_pipeline(
    body: PipelineStopIn = Body(default_factory=PipelineStopIn),
    workspace_id: str = Depends(get_workspace_id),
    service: PipelineService = Depends(get_pipeline_service),
) -> SuccessEnvelope[PipelineStopOut]:
    _ = body
    snapshot = service.stop(workspace_id)
    usage_cost = service.summarize_attempt_usage_cost(
        workspace_id,
        run_id=snapshot.run_id,
    )
    return SuccessEnvelope(data=_snapshot_to_pause_out(snapshot, usage_cost))


@router.get(
    "/status",
    response_model=SuccessEnvelope[PipelineStatusOut],
    summary="读流水线 active 状态",
    description=(
        "返当前 active run 状态(或最近一次 last_run snapshot;无任何 run → "
        "state=idle + run_id=None)。state ∈ ``idle | running | paused | "
        "interrupted | stopped | error``(interrupted = 进程重启中断,可 "
        "``:resume`` 断点续跑)。需 X-Workspace-Id。"
    ),
)
async def get_pipeline_status(
    workspace_id: str = Depends(get_workspace_id),
    service: PipelineService = Depends(get_pipeline_service),
) -> SuccessEnvelope[PipelineStatusOut]:
    snapshot = service.status(workspace_id)
    usage_cost = service.summarize_attempt_usage_cost(
        workspace_id,
        run_id=snapshot.run_id,
    )
    return SuccessEnvelope(data=_snapshot_to_status_out(snapshot, usage_cost))
