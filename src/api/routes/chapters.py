"""章节路由(P1-B-19)。

对齐 PROJECT_SPEC §9.4 章节生命周期 + API_CONTRACT §4.3 / §4.4 / §6 +
P1-B-18 沉淀的 URI 风格规范(动作类用 ``:action``,CRUD 用标准 REST)。

10 个端点
==========

| 方法 | 路径 | 作用 |
|------|------|------|
| POST | ``/chapters/{chapter_id}:write`` | 跑一次完整闭环 |
| POST | ``/chapters/{chapter_id}:rewrite`` | 强制重写起跑 |
| POST | ``/chapters/{chapter_id}:revise`` | 局部修订(不走 Orchestrator) |
| POST | ``/chapters/{chapter_id}:switch-version`` | 切 final 指针(决策 7 方案 B) |
| GET  | ``/chapters/{chapter_id}/status`` | P1 简版状态(3 档 + unknown) |
| GET  | ``/chapters/{chapter_id}/card`` | 读章节卡 |
| PUT  | ``/chapters/{chapter_id}/card`` | 写章节卡(完整覆盖) |
| GET  | ``/chapters/{chapter_id}/versions`` | 列出所有版本号 |
| GET  | ``/chapters/{chapter_id}/versions/{version_id}`` | 读单个版本正文 |
| GET  | ``/tasks/{task_id}`` | 见 ``routes/tasks.py`` |

工作区上下文
============

P1-B-19 决策 1:全部 ``/chapters/{id}*`` 端点强制带 ``X-Workspace-Id`` header,
通过 ``Depends(get_workspace_id)`` 解析。缺失 → ``MissingWorkspaceHeader (422)``。
不读 ``_active`` 软链(避免隐式上下文混淆),不嵌入 URL(保留 spec §4.1
"顶级 chapter" 字面契约)。

任务模式
========

P1-B-19 决策 2:``:write`` / ``:rewrite`` / ``:revise`` 同步执行,跑完构造
``TaskInfo(status="done", result=...)`` 写入 ``task_registry`` 后返回 HTTP 200。
``GET /tasks/{task_id}`` 在进程内 LRU 字典(N=64)里查;P3 切后台 worker 时
返回 202 + ``status="pending"``,客户端契约不变。

result 段
=========

决策 6:``:write`` / ``:rewrite`` 的 ``result`` 是 ``WriteResultSummary``——只
带摘要,**不含正文**;正文走 ``GET /versions/{id}`` 单独取。
``review_summary[]`` 每条带 ``revise_targets``(P1 review 没持久化,这是仅有
入口)。``:revise`` 的 result 是 ``ChapterVersionMeta``,同样不含正文。
"""

from __future__ import annotations

from collections.abc import Callable
from typing import Any

import structlog
from fastapi import APIRouter, BackgroundTasks, Body, Depends, status

from src.api import task_registry
from src.api.deps import (
    get_chapter_card_service,
    get_chapter_rewind_service,
    get_chapter_service,
    get_core_state_service,
    get_validator_report_service,
    get_workspace_id,
    get_workspace_service,
)
from src.api.errors import (
    ApiError,
    ChapterCardNotFound,
    ChapterIdMismatch,
    ChapterInvalidState,
    _translate_service_error,
)
from src.api.schemas.chapter_cards import (
    ChapterCardApproveIn,
    ChapterCardGenerateIn,
    ChapterCardOut,
    ChapterCardRejectIn,
    ChapterCardRetryReferencesIn,
    ChapterCardRetryReferencesOut,
    ChapterCardStatusOut,
)
from src.api.schemas.chapters import (
    ChapterForcePassIn,
    ChapterForcePassOut,
    ChapterListItem,
    ChapterReReviewIn,
    ChapterReviseIn,
    ChapterContentUpdateIn,
    ChapterContentUpdateOut,
    ChapterRewindIn,
    ChapterRewindOut,
    ChapterRewriteIn,
    ChapterStatusOut,
    ChapterUnblockIn,
    ChapterUnblockOut,
    ChapterWriteIn,
    RefreshContinuityAllOut,
    RefreshContinuityItem,
    WriteResultSummary,
)
from src.api.schemas.common import SuccessEnvelope
from src.api.schemas.core_state import (
    CoreStateAuditItemOut,
    CoreStateAuditOut,
)
from src.api.schemas.reviews import ReviewIssueOut, ReviewSummaryEntry
from src.api.schemas.tasks import (
    TaskInfo,
    TaskInfoChapterRevise,
    TaskInfoChapterWrite,
)
from src.api.schemas.validators import ValidatorReportOut
from src.api.schemas.versions import (
    ChapterVersionContent,
    ChapterVersionItem,
    ChapterVersionList,
    ChapterVersionMeta,
    StyleDriftSeries,
    StyleFingerprintPoint,
    SwitchVersionIn,
    SwitchVersionResult,
)
from src.api.websocket.channels import (
    chapter_events_channel,
    task_progress_channel,
)
from src.api.websocket.manager import manager as ws_manager
from src.core.llm.cancel import LLMCancelledError
from src.core.models.chapter_card import ChapterCard
from src.core.state_machine.states import ChapterState
from src.services.chapter_card_service import ChapterCardService
from src.services.post_v8_abc_selection_activity import (
    bind_selection_or_production_scope,
)
from src.services.chapter_rewind_service import ChapterRewindService
from src.services.chapter_service import ChapterService
from src.services.core_state_service import CoreStateService
from src.services.validator_report_service import ValidatorReportService
from src.services.workspace_service import WorkspaceService


_log = structlog.get_logger(__name__)

router = APIRouter(prefix="/chapters", tags=["chapters"])


# ---- 内部辅助 ---------------------------------------------------------------


def _ensure_card_exists(
    service: ChapterService, workspace_id: str, chapter_id: str
) -> None:
    """章节卡文件存在性守卫(决策 3,前置抛 404)。

    存在性 vs 合法性双轨:文件不存在 → 此处 404;JSON / schema 不合法 →
    service ``read_chapter_card`` 抛 ``ChapterServiceError("章节卡内容...")``
    → 翻译表翻成 ``CHAPTER_CARD_INVALID (422)``。
    """
    if not service.card_path_for(workspace_id, chapter_id).is_file():
        raise ChapterCardNotFound(
            f"章节卡不存在: workspace={workspace_id} chapter={chapter_id}"
        )


def _make_state_transition_callback(chapter_id: str):
    """构造 ``on_state_transition`` 闭包,推 ``state_transition`` 事件。

    P1-B-20:Orchestrator / ChapterService **不感知 WebSocket**,仅接 callback;
    本闭包是路由层把 ``manager.publish`` 注入业务层的唯一接缝。``manager.publish``
    是 sync ``put_nowait``,无 ``await`` / 阻塞风险,可在 Orchestrator 同步循环
    内直接调用。
    """

    channel = chapter_events_channel(chapter_id)

    def _cb(prev: ChapterState, nxt: ChapterState) -> None:
        ws_manager.publish(
            channel=channel,
            event="state_transition",
            data={"from": prev.value, "to": nxt.value},
        )

    return _cb


# ---- v1.0.x Commit B(2026-05-21):BackgroundTasks + WS progress 改造 -----
#
# 根因:5 个长跑端点(:write / :rewrite / :revise / outline:generate /
# outline:regenerate-section / card:generate)原本是 ``async def`` 但内部
# 同步调 Ollama HTTP + SQLAlchemy(几十秒到几分钟),**直接阻塞 FastAPI
# event loop**(uvicorn 默认单 worker → 唯一 event loop)→ LLM 跑期间
# 前端所有 GET 请求 hang(基线证据见 docs/impl/v1_0_hotfix_commit_b_*.md
# §Phase 0b)。
#
# 修法:每个端点立即返 202 + TaskInfo(status=pending);实际 service.X
# 走 ``BackgroundTasks.add_task`` 投入 starlette anyio threadpool(sync
# 函数 FastAPI 自动派发到 threadpool 跑),event loop 立即释放。后台 wrapper
# 内 try/except 翻 ApiError → error_dict 写 task_registry update_to_failed
# + WS publish ``failed`` 事件;成功则 update_to_done + WS publish ``done``
# 含 result。前端订阅 task.{task_id}.progress 频道接 stage / done /
# failed 事件,done 后 invalidate query。模板对齐 src/api/routes/pipeline.py
# :start ([113](src/api/routes/pipeline.py:113))。


def _publish_task_event(task_id: str, event: str, data: dict[str, Any]) -> None:
    """统一推送到 ``task.{task_id}.progress`` 频道。"""
    ws_manager.publish(
        channel=task_progress_channel(task_id),
        event=event,
        data=data,
    )


def _build_error_dict(exc: BaseException) -> dict[str, Any]:
    """异常 → 结构化 error dict(对齐 ``API_CONTRACT §3.2`` ErrorPayload)。

    优先序:① 已经是 ``ApiError`` 子类(路由层守卫直接抛)→ 用其字段;
    ② service 层异常 → ``_translate_service_error`` 翻译;③ 兜底
    ``INTERNAL_ERROR`` + str(exc)。
    """
    translated: ApiError | None = (
        exc if isinstance(exc, ApiError) else _translate_service_error(exc)
    )
    if translated is None:
        return {
            "code": "INTERNAL_ERROR",
            "message": str(exc) or "未知错误",
            "details": None,
        }
    return {
        "code": translated.code,
        "message": translated.message,
        "details": translated.details,
    }


def _finalize_chapter_task(
    service: ChapterService,
    workspace_id: str,
    chapter_id: str,
    task_id: str,
    *,
    result: dict[str, Any],
) -> None:
    """先完成持久租约，再把外层任务公布为 ``done``。

    章节正文已经物化后若安全租约无法精确释放，本次后台任务仍不完整。此时保留
    租约现场并把 UI task 标为失败，禁止先发 ``done`` 再遗留一把无人处理的锁。
    """

    try:
        service.complete_operation_task(
            workspace_id,
            chapter_id,
            task_id=task_id,
        )
    except Exception as exc:  # noqa: BLE001
        error = _build_error_dict(exc)
        task_registry.update_to_failed(task_id, error=error)
        _publish_task_event(task_id, "failed", {"task_id": task_id, "error": error})
        return
    task_registry.update_to_done(task_id, result=result)
    _publish_task_event(task_id, "done", {"task_id": task_id, "result": result})


def _check_workspace_destroyed_and_cancel(workspace_id: str, task_id: str) -> bool:
    """v1.0.x wave 6 s2.6a:BackgroundTask 入口防御 — 查工作区是否已被销毁。

    返 True → 工作区已销毁,caller 必须 early-return;同时 ``update_to_failed`` 标
    ``WORKSPACE_DESTROYED`` 错误码 + WS publish failed 让前端感知"任务因工作区销毁
    取消"。返 False → 正常,继续跑任务。

    背景:destroy / purge / archive 在 rmtree 之前调 ``mark_workspace_destroyed``,
    异步跑的 BackgroundTask 在入口查到 → 主动 early-return,避免 outline writer
    / chapter_card writer 等写盘路径在 rmtree 后 mkdir 重建子目录残留。
    """
    if not task_registry.is_workspace_destroyed(workspace_id):
        return False
    error = {
        "code": "WORKSPACE_DESTROYED",
        "message": "工作区已被销毁,任务取消",
        "details": {"workspace_id": workspace_id},
    }
    task_registry.update_to_failed(task_id, error=error)
    _publish_task_event(task_id, "failed", {"task_id": task_id, "error": error})
    return True


def _run_cancellable(
    task_id: str,
    workspace_id: str | None,
    fn: Callable[..., None],
    /,
    *args: Any,
    **kwargs: Any,
) -> None:
    """J1:后台任务薄包装 — 绑 cancel Event 进 contextvar + 捕获取消翻 cancelled。

    把 ``_run_*_task`` 包进 ``task_registry.bind_cancel_event``(块内深层 LLM
    client 可读取消信号 + anyio 线程池复用安全 reset)。``LLMCancelledError``
    (BaseException 子类,穿透沿途所有 except Exception 直达此处)在此集中捕获 →
    ``update_to_cancelled`` + WS publish ``cancelled``。其余异常已由 ``fn`` 内部
    except Exception 翻 ``failed``,本包装不重复处理。
    """
    try:
        with task_registry.bind_cancel_event(task_id, workspace_id):
            fn(*args, **kwargs)
    except LLMCancelledError:
        task_registry.update_to_cancelled(task_id)
        _publish_task_event(task_id, "cancelled", {"task_id": task_id})


def _claim_chapter_worker_or_cleanup(
    service: ChapterService,
    workspace_id: str,
    chapter_id: str,
    task_id: str,
) -> bool:
    """让唯一 callback 消费 API 预留的章节租约。"""

    claim = task_registry.claim_pending_for_run(task_id)
    if claim == "claimed":
        return True
    if claim == "cancelled":
        service.release_operation_task_if_never_dispatched(
            workspace_id,
            chapter_id,
            task_id=task_id,
        )
        _publish_task_event(task_id, "cancelled", {"task_id": task_id})
        return False
    _log.warning(
        "duplicate_background_task_callback_blocked",
        task_id=task_id,
        workspace_id=workspace_id,
        chapter_id=chapter_id,
    )
    return False


def _run_chapter_cancellable(
    task_id: str,
    workspace_id: str,
    fn: Callable[..., None],
    service: ChapterService,
    operation_workspace_id: str,
    chapter_id: str,
    operation_task_id: str,
    /,
    *args: Any,
    **kwargs: Any,
) -> None:
    """先唯一认领章节 Worker，再绑 cancel Event，避免重入 ABA。"""

    if workspace_id != operation_workspace_id or task_id != operation_task_id:
        raise RuntimeError("章节 BackgroundTask 的 task/workspace 绑定不一致")
    if not _claim_chapter_worker_or_cleanup(
        service,
        workspace_id,
        chapter_id,
        task_id,
    ):
        return
    try:
        with task_registry.bind_cancel_event(task_id, workspace_id):
            # cancel 可能恰好发生在 pending->running CAS 与 Event
            # 登记之间。登记后复查持久位；更晚的 cancel
            # 则会直接 set 已登记 Event。
            if task_registry.is_cancel_requested(task_id):
                service.release_operation_task_if_never_dispatched(
                    workspace_id,
                    chapter_id,
                    task_id=task_id,
                )
                task_registry.update_to_cancelled(task_id)
                _publish_task_event(task_id, "cancelled", {"task_id": task_id})
                return
            fn(
                service,
                operation_workspace_id,
                chapter_id,
                operation_task_id,
                *args,
                **kwargs,
            )
    except LLMCancelledError:
        task_registry.update_to_cancelled(task_id)
        _publish_task_event(task_id, "cancelled", {"task_id": task_id})


def _run_chapter_write_task(
    service: ChapterService,
    workspace_id: str,
    chapter_id: str,
    task_id: str,
    *,
    profile_override: str | None,
    skip_must_feasibility_precheck: bool = False,
) -> None:
    """BackgroundTasks 后台跑 :write 闭环 + WS publish。

    starlette 把 sync 函数自动派发到 anyio threadpool(不占 event loop)。
    成功 / 失败统一捕获并写 task_registry + WS event。

    ``skip_must_feasibility_precheck``:v2 Fix 2 —— 经 ``:ignore-feasibility``
    端点(人工复核确认 Fix D 误判)透传 True,跳过写前 must 可行性预检;
    写后 must_achieve_validator 仍兜底验真实际内容。
    """
    # v1.0.x wave 6 s2.6a:入口防御 — 工作区已销毁直接 early-return + failed publish
    if _check_workspace_destroyed_and_cancel(workspace_id, task_id):
        service.release_operation_task_if_never_dispatched(
            workspace_id, chapter_id, task_id=task_id
        )
        return
    _publish_task_event(task_id, "stage", {"stage": "running", "task_id": task_id})
    try:
        result = service.write(
            workspace_id,
            chapter_id,
            on_state_transition=_make_state_transition_callback(chapter_id),
            profile_override=profile_override,
            skip_must_feasibility_precheck=skip_must_feasibility_precheck,
            attempt_task_id=task_id,
        )
    except LLMCancelledError:
        service.release_operation_task_if_never_dispatched(
            workspace_id, chapter_id, task_id=task_id
        )
        raise
    except Exception as exc:  # noqa: BLE001
        error = _build_error_dict(exc)
        task_registry.update_to_failed(task_id, error=error)
        service.release_operation_task_if_never_dispatched(
            workspace_id, chapter_id, task_id=task_id
        )
        _publish_task_event(task_id, "failed", {"task_id": task_id, "error": error})
        return
    summary = _build_write_summary(result).model_dump(mode="json")
    _finalize_chapter_task(
        service,
        workspace_id,
        chapter_id,
        task_id,
        result=summary,
    )


def _run_chapter_rewrite_task(
    service: ChapterService,
    workspace_id: str,
    chapter_id: str,
    task_id: str,
    *,
    prior_rewrite_count: int,
    profile_override: str | None,
) -> None:
    """BackgroundTasks 后台跑 :rewrite 闭环 + WS publish(与 write 同型)。"""
    # v1.0.x wave 6 s2.6a:入口防御 — 工作区已销毁直接 early-return + failed publish
    if _check_workspace_destroyed_and_cancel(workspace_id, task_id):
        service.release_operation_task_if_never_dispatched(
            workspace_id, chapter_id, task_id=task_id
        )
        return
    _publish_task_event(task_id, "stage", {"stage": "running", "task_id": task_id})
    try:
        result = service.rewrite(
            workspace_id,
            chapter_id,
            prior_rewrite_count=prior_rewrite_count,
            on_state_transition=_make_state_transition_callback(chapter_id),
            profile_override=profile_override,
            attempt_task_id=task_id,
        )
    except LLMCancelledError:
        service.release_operation_task_if_never_dispatched(
            workspace_id, chapter_id, task_id=task_id
        )
        raise
    except Exception as exc:  # noqa: BLE001
        error = _build_error_dict(exc)
        task_registry.update_to_failed(task_id, error=error)
        service.release_operation_task_if_never_dispatched(
            workspace_id, chapter_id, task_id=task_id
        )
        _publish_task_event(task_id, "failed", {"task_id": task_id, "error": error})
        return
    summary = _build_write_summary(result).model_dump(mode="json")
    _finalize_chapter_task(
        service,
        workspace_id,
        chapter_id,
        task_id,
        result=summary,
    )


def _run_chapter_re_review_task(
    service: ChapterService,
    workspace_id: str,
    chapter_id: str,
    task_id: str,
    *,
    profile_override: str | None,
) -> None:
    """BackgroundTasks 后台跑 :re-review(人工改稿后单次重审)+ WS publish。

    v2 W3.5 UI 缺口3。结果摘要复用 ``_build_write_summary``(含 final_state +
    escalation_reason);escalation_details 由 service 同步到 chapters 表,前端
    重审后刷新 status 拿详情。
    """
    # v1.0.x wave 6 s2.6a:入口防御 — 工作区已销毁直接 early-return + failed publish
    if _check_workspace_destroyed_and_cancel(workspace_id, task_id):
        service.release_operation_task_if_never_dispatched(
            workspace_id, chapter_id, task_id=task_id
        )
        return
    _publish_task_event(task_id, "stage", {"stage": "running", "task_id": task_id})
    try:
        result = service.re_review(
            workspace_id,
            chapter_id,
            on_state_transition=_make_state_transition_callback(chapter_id),
            profile_override=profile_override,
            attempt_task_id=task_id,
        )
    except LLMCancelledError:
        service.release_operation_task_if_never_dispatched(
            workspace_id, chapter_id, task_id=task_id
        )
        raise
    except Exception as exc:  # noqa: BLE001
        error = _build_error_dict(exc)
        task_registry.update_to_failed(task_id, error=error)
        service.release_operation_task_if_never_dispatched(
            workspace_id, chapter_id, task_id=task_id
        )
        _publish_task_event(task_id, "failed", {"task_id": task_id, "error": error})
        return
    summary = _build_write_summary(result).model_dump(mode="json")
    _finalize_chapter_task(
        service,
        workspace_id,
        chapter_id,
        task_id,
        result=summary,
    )


def _run_chapter_revise_task(
    service: ChapterService,
    workspace_id: str,
    chapter_id: str,
    task_id: str,
    *,
    issues: list,
    source_version_id: int | None,
) -> None:
    """BackgroundTasks 后台跑 :revise + WS publish。

    revise 不走 Orchestrator,无 state_transition callback;produce 的是
    ``ChapterVersionMeta``(不含正文)而非 ``WriteResultSummary``。
    """
    # v1.0.x wave 6 s2.6a:入口防御 — 工作区已销毁直接 early-return + failed publish
    if _check_workspace_destroyed_and_cancel(workspace_id, task_id):
        service.release_operation_task_if_never_dispatched(
            workspace_id, chapter_id, task_id=task_id
        )
        return
    _publish_task_event(task_id, "stage", {"stage": "running", "task_id": task_id})
    try:
        new_version = service.revise(
            workspace_id,
            chapter_id,
            issues=list(issues),
            source_version_id=source_version_id,
            attempt_task_id=task_id,
        )
    except LLMCancelledError:
        service.release_operation_task_if_never_dispatched(
            workspace_id, chapter_id, task_id=task_id
        )
        raise
    except Exception as exc:  # noqa: BLE001
        error = _build_error_dict(exc)
        task_registry.update_to_failed(task_id, error=error)
        service.release_operation_task_if_never_dispatched(
            workspace_id, chapter_id, task_id=task_id
        )
        _publish_task_event(task_id, "failed", {"task_id": task_id, "error": error})
        return
    meta_dict = ChapterVersionMeta(
        version_id=new_version.version_id,
        word_count=new_version.word_count,
        created_at=new_version.created_at,
        creation_type=new_version.creation_type,
        model_used=new_version.model_used,
        review_score=new_version.review_score,
        notes=new_version.notes,
    ).model_dump(mode="json")
    _finalize_chapter_task(
        service,
        workspace_id,
        chapter_id,
        task_id,
        result=meta_dict,
    )


def _run_chapter_card_generate_task(
    service: ChapterCardService,
    workspace_id: str,
    chapter_id: str,
    task_id: str,
    *,
    chapter_number: int,
    volume_id: str,
) -> None:
    """BackgroundTasks 后台跑单卡 :generate + WS publish。

    返回 ``ChapterCardOut``(整张章节卡 schema)。无 state_transition 回调。
    """
    # v1.0.x wave 6 s2.6a:入口防御 — 工作区已销毁直接 early-return + failed publish
    if _check_workspace_destroyed_and_cancel(workspace_id, task_id):
        return
    _publish_task_event(task_id, "stage", {"stage": "running", "task_id": task_id})
    try:
        with bind_selection_or_production_scope(
            workspace_id,
            task_id=task_id,
            chapter_id=chapter_id,
        ):
            card = service.generate_single_chapter_card(
                workspace_id,
                chapter_id,
                chapter_number=chapter_number,
                volume_id=volume_id,
                card_status="draft",
                task_id=task_id,
            )
    except Exception as exc:  # noqa: BLE001
        error = _build_error_dict(exc)
        task_registry.update_to_failed(task_id, error=error)
        _publish_task_event(task_id, "failed", {"task_id": task_id, "error": error})
        return
    card_dict = ChapterCardOut.model_validate(card.model_dump()).model_dump(mode="json")
    task_registry.update_to_done(task_id, result=card_dict)
    _publish_task_event(task_id, "done", {"task_id": task_id, "result": card_dict})


def _run_chapter_card_cascade_regenerate_task(
    service: ChapterCardService,
    workspace_id: str,
    task_id: str,
    chapter_ids: list[str],
) -> None:
    """v1.0.x wave 6 s2.6a:chapter_card cascade 级联重生 BackgroundTask wrapper。

    service.regenerate_invalidated_cards_batch 内部管 stage / done / batch_partial
    event publish + task_registry update;本 wrapper 加入口防御 + 异常兜底
    publish failed(避免 BackgroundTask 抛错时 cascade task 永远卡 pending)。
    """
    if _check_workspace_destroyed_and_cancel(workspace_id, task_id):
        return
    try:
        with bind_selection_or_production_scope(
            workspace_id,
            task_id=task_id,
        ):
            service.regenerate_invalidated_cards_batch(
                workspace_id, chapter_ids=chapter_ids, task_id=task_id
            )
    except Exception as exc:  # noqa: BLE001
        error = _build_error_dict(exc)
        task_registry.update_to_failed(task_id, error=error)
        _publish_task_event(task_id, "failed", {"task_id": task_id, "error": error})


def _review_summary_entry_from_result(r) -> ReviewSummaryEntry:  # type: ignore[no-untyped-def]
    """``ReviewResult`` → ``ReviewSummaryEntry``。

    ``revise_targets`` 显式 ``ReviewIssueOut.model_validate``:``ReviewIssueOut``
    是 ``ReviewIssue`` 的空子类(P1-B-18 ``WorkspaceOut`` 同模式),Pydantic v2
    校验不 coerce 父类实例到子类字段,直接 ``list(...)`` 传业务模型实例会触发
    ValidationError(P1-B-23 端到端验收暴露)。

    抽成 helper 让写作结果与其他审稿摘要入口复用同一字段映射，避免 schema
    演进时出现双份 list-comp 漂移。
    """
    return ReviewSummaryEntry(
        version_reviewed=r.version_reviewed,
        verdict=r.verdict,
        score_overall=r.score_overall,
        score_plot_progression=r.score_plot_progression,
        score_tension_match=r.score_tension_match,
        score_prose_quality=r.score_prose_quality,
        score_consistency=r.score_consistency,
        score_hook_management=r.score_hook_management,
        score_voice_consistency=r.score_voice_consistency,
        score_imagery=r.score_imagery,
        score_interiority=r.score_interiority,
        score_formulaic=r.score_formulaic,
        score_immersion=r.score_immersion,
        hard_constraints_passed=r.hard_constraints_passed,
        constraint_violations=list(r.constraint_violations),
        constraint_results=list(r.constraint_results),
        hard_constraint_regressions=list(r.hard_constraint_regressions),
        revise_targets=[
            ReviewIssueOut.model_validate(t.model_dump()) for t in r.revise_targets
        ],
    )


def _build_write_summary(result_obj) -> WriteResultSummary:  # type: ignore[no-untyped-def]
    """把 ``OrchestratorResult`` 转成 ``WriteResultSummary``(决策 6)。

    review_summary 每条带 revise_targets(决策 6 补充);`final_path` 转成
    绝对路径字符串(P1 本机调试场景下安全,P3+ 公网模式应改相对或不暴露,
    与 P1-B-18 工作区 archive_path 同处理)。

    """
    return WriteResultSummary(
        final_state=result_obj.final_state.value,
        final_version_id=result_obj.final_version_id,
        final_path=str(result_obj.final_path) if result_obj.final_path else None,
        version_count=len(result_obj.versions),
        revise_count=result_obj.revise_count,
        rewrite_count=result_obj.rewrite_count,
        escalation_reason=result_obj.escalation_reason,
        review_summary=[
            _review_summary_entry_from_result(r) for r in result_obj.reviews
        ],
    )


# ---- 动作端点(:write / :rewrite / :revise / :switch-version) ---------------


def _core_state_audit_payload(
    service: CoreStateService,
    workspace_id: str,
) -> CoreStateAuditOut:
    return CoreStateAuditOut(
        projection=service.load_projection(workspace_id),
        items=[
            CoreStateAuditItemOut.model_validate(item.model_dump())
            for item in service.list_audit_items(workspace_id)
        ],
    )


@router.get(
    "/core-state",
    response_model=SuccessEnvelope[CoreStateAuditOut],
    summary="查看全书核心状态与书内时钟审计账本",
)
async def get_core_state_audit(
    workspace_id: str = Depends(get_workspace_id),
    service: CoreStateService = Depends(get_core_state_service),
    workspace_service: WorkspaceService = Depends(get_workspace_service),
) -> SuccessEnvelope[CoreStateAuditOut]:
    workspace_service.info(workspace_id)
    return SuccessEnvelope(data=_core_state_audit_payload(service, workspace_id))


def _reserve_chapter_worker(
    *,
    service: ChapterService,
    workspace_id: str,
    chapter_id: str,
    operation: str,
    task_type: str,
) -> tuple[str, TaskInfo]:
    """在创建 BackgroundTask 前持久占住章节，重复点击直接 409。"""

    task_id = task_registry.new_task_id()
    service.reserve_operation_task(
        workspace_id,
        chapter_id,
        operation=operation,
        task_id=task_id,
    )
    try:
        info = task_registry.record_pending(
            task_id,
            type=task_type,
            workspace_id=workspace_id,
        )
    except Exception:
        service.release_operation_task_if_never_dispatched(
            workspace_id,
            chapter_id,
            task_id=task_id,
        )
        raise
    return task_id, info


@router.post(
    "/{chapter_id}:write",
    response_model=SuccessEnvelope[TaskInfoChapterWrite],
    status_code=status.HTTP_202_ACCEPTED,
    summary="写一章(异步,完整闭环)",
    description=(
        "**Commit B(2026-05-21)起改 BackgroundTasks 模式**:立即返 202 + "
        "TaskInfo(status=pending,task_id);实际章节闭环走后台 anyio "
        "threadpool 跑 service.write(write → review → revise/rewrite → "
        "... → pass/升人工)。前端订阅 ``task.{task_id}.progress`` 频道接 "
        "``stage`` / ``done`` / ``failed`` 事件,done 后再 invalidate query。"
        "**根治** LLM 运行期间 event loop 阻塞导致前端所有 GET 请求 hang。"
        "需 X-Workspace-Id header。body 可选携带 profile_override 字段。"
    ),
)
async def write_chapter(
    chapter_id: str,
    background_tasks: BackgroundTasks,
    body: ChapterWriteIn = Body(default_factory=ChapterWriteIn),
    workspace_id: str = Depends(get_workspace_id),
    service: ChapterService = Depends(get_chapter_service),
) -> SuccessEnvelope[TaskInfoChapterWrite]:
    _ensure_card_exists(service, workspace_id, chapter_id)
    task_id, info = _reserve_chapter_worker(
        service=service,
        workspace_id=workspace_id,
        chapter_id=chapter_id,
        operation="write",
        task_type="chapter_write",
    )
    background_tasks.add_task(
        _run_chapter_cancellable,
        task_id,
        workspace_id,
        _run_chapter_write_task,
        service,
        workspace_id,
        chapter_id,
        task_id,
        profile_override=body.profile_override,
    )
    return SuccessEnvelope(data=TaskInfoChapterWrite.model_validate(info.model_dump()))


@router.post(
    "/{chapter_id}:rewrite",
    response_model=SuccessEnvelope[TaskInfoChapterWrite],
    status_code=status.HTTP_202_ACCEPTED,
    summary="强制重写章节(异步)",
    description=(
        "**Commit B(2026-05-21)起改 BackgroundTasks 模式**:立即返 202 + "
        "TaskInfo(status=pending,task_id);实际重写闭环走后台 threadpool。"
        "prior_rewrite_count 对接 max_rewrites 名额预算。前端订阅 "
        "``task.{task_id}.progress`` 频道接事件。需 X-Workspace-Id。"
    ),
)
async def rewrite_chapter(
    chapter_id: str,
    body: ChapterRewriteIn,
    background_tasks: BackgroundTasks,
    workspace_id: str = Depends(get_workspace_id),
    service: ChapterService = Depends(get_chapter_service),
) -> SuccessEnvelope[TaskInfoChapterWrite]:
    _ensure_card_exists(service, workspace_id, chapter_id)
    task_id, info = _reserve_chapter_worker(
        service=service,
        workspace_id=workspace_id,
        chapter_id=chapter_id,
        operation="rewrite",
        task_type="chapter_rewrite",
    )
    background_tasks.add_task(
        _run_chapter_cancellable,
        task_id,
        workspace_id,
        _run_chapter_rewrite_task,
        service,
        workspace_id,
        chapter_id,
        task_id,
        prior_rewrite_count=body.prior_rewrite_count,
        profile_override=body.profile_override,
    )
    return SuccessEnvelope(data=TaskInfoChapterWrite.model_validate(info.model_dump()))


@router.post(
    "/{chapter_id}:re-review",
    response_model=SuccessEnvelope[TaskInfoChapterWrite],
    status_code=status.HTTP_202_ACCEPTED,
    summary="人工改稿后重审章节(异步)",
    description=(
        "**v2 W3.5 UI 缺口3**:对孤儿章(``human_intervention``)人工改稿的最新"
        "版本重跑写后审校(Reviewer + 写后 LLM 闸 + validator)。立即返 202 + "
        "TaskInfo;实际重审走后台 threadpool。**通过**→ APPROVED(自动 mark "
        "final + 摘要 + 记忆);**不通过**→ 回 human_intervention + "
        "``escalation_reason=re_review_failed`` + 详情(前端刷新 status 拿)。"
        "状态非 human_intervention → 422 ``CHAPTER_RE_REVIEW_INVALID_STATE``。"
        "前端订阅 ``task.{task_id}.progress`` 频道接事件。需 X-Workspace-Id。"
    ),
)
async def re_review_chapter(
    chapter_id: str,
    background_tasks: BackgroundTasks,
    body: ChapterReReviewIn = Body(default_factory=ChapterReReviewIn),
    workspace_id: str = Depends(get_workspace_id),
    service: ChapterService = Depends(get_chapter_service),
) -> SuccessEnvelope[TaskInfoChapterWrite]:
    _ensure_card_exists(service, workspace_id, chapter_id)
    task_id, info = _reserve_chapter_worker(
        service=service,
        workspace_id=workspace_id,
        chapter_id=chapter_id,
        operation="re-review",
        task_type="chapter_re_review",
    )
    background_tasks.add_task(
        _run_chapter_cancellable,
        task_id,
        workspace_id,
        _run_chapter_re_review_task,
        service,
        workspace_id,
        chapter_id,
        task_id,
        profile_override=body.profile_override,
    )
    return SuccessEnvelope(data=TaskInfoChapterWrite.model_validate(info.model_dump()))


@router.post(
    "/{chapter_id}:revise",
    response_model=SuccessEnvelope[TaskInfoChapterRevise],
    status_code=status.HTTP_202_ACCEPTED,
    summary="局部修订章节(异步)",
    description=(
        "**Commit B(2026-05-21)起改 BackgroundTasks 模式**:立即返 202 + "
        "TaskInfo(status=pending,task_id);实际 Reviser 调 LLM 走后台 "
        "threadpool。不走 Orchestrator,不参与状态机转换;新版本不会自动 "
        "mark final,需要再 :write/:rewrite 或 :switch-version。前端订阅 "
        "``task.{task_id}.progress`` 频道接事件。需 X-Workspace-Id。"
    ),
)
async def revise_chapter(
    chapter_id: str,
    body: ChapterReviseIn,
    background_tasks: BackgroundTasks,
    workspace_id: str = Depends(get_workspace_id),
    service: ChapterService = Depends(get_chapter_service),
) -> SuccessEnvelope[TaskInfoChapterRevise]:
    _ensure_card_exists(service, workspace_id, chapter_id)
    task_id, info = _reserve_chapter_worker(
        service=service,
        workspace_id=workspace_id,
        chapter_id=chapter_id,
        operation="revise",
        task_type="chapter_revise",
    )
    background_tasks.add_task(
        _run_chapter_cancellable,
        task_id,
        workspace_id,
        _run_chapter_revise_task,
        service,
        workspace_id,
        chapter_id,
        task_id,
        issues=list(body.issues),
        source_version_id=body.source_version_id,
    )
    return SuccessEnvelope(data=TaskInfoChapterRevise.model_validate(info.model_dump()))


@router.post(
    "/{chapter_id}:switch-version",
    response_model=SuccessEnvelope[SwitchVersionResult],
    summary="切换 final 指针到指定版本",
    description=(
        "把 final.md 切到指定版本(等价于 ChapterVersioning.mark_final)。"
        "动作作用对象是章节(改 final 指针 = 章节级状态变更),挂在章节级。"
        "需 X-Workspace-Id。"
    ),
)
async def switch_version(
    chapter_id: str,
    body: SwitchVersionIn,
    workspace_id: str = Depends(get_workspace_id),
    service: ChapterService = Depends(get_chapter_service),
) -> SuccessEnvelope[SwitchVersionResult]:
    final_path = service.switch_version(workspace_id, chapter_id, body.version_id)
    return SuccessEnvelope(
        data=SwitchVersionResult(
            chapter_id=chapter_id,
            version_id=body.version_id,
            final_path=str(final_path),
        )
    )


# ---- continuity 批量 backfill(v1.1 w3-s7 E6) ------------------------------


@router.post(
    ":refresh-continuity-all",
    response_model=SuccessEnvelope[RefreshContinuityAllOut],
    summary="批量重抽工作区所有章节卡的 continuity_context",
    description=(
        "一键治理批量生成阶段遗留的 ``continuity_context`` 占位 tail。逐张扫 "
        "``data/cards/*.json``,ch>=2 复用单章 ``_maybe_refresh_continuity`` "
        "(含 LLM→rule fail-open 降级 + 写回卡 JSON);首章 skip,坏卡 warn-skip。"
        "返每章 outcome(refreshed / unchanged / skipped_first)+ refreshed_count。"
        "需 X-Workspace-Id header。"
    ),
)
async def refresh_continuity_all(
    workspace_id: str = Depends(get_workspace_id),
    service: ChapterService = Depends(get_chapter_service),
) -> SuccessEnvelope[RefreshContinuityAllOut]:
    result = service.refresh_continuity_all(workspace_id)
    return SuccessEnvelope(
        data=RefreshContinuityAllOut(
            workspace_id=workspace_id,
            total_scanned=result.total_scanned,
            refreshed_count=result.refreshed_count,
            items=[
                RefreshContinuityItem(
                    chapter_id=e.chapter_id,
                    chapter_number=e.chapter_number,
                    outcome=e.outcome,
                )
                for e in result.items
            ],
        )
    )


# ---- 章节人工介入(P6-B-01) -----------------------------------------------


@router.post(
    "/{chapter_id}:rewind",
    response_model=SuccessEnvelope[ChapterRewindOut],
    summary="rewind 章节(回退到未写状态 + 后续章节标废)",
    description=(
        "把 ``chapters.status`` 翻 ``drafting``(回到未写状态),``approved_at`` "
        "重置 None;``chapter_id > target_id`` 的后续章节翻 ``invalidated`` "
        "(``chapter:write`` gate 阻断,user 需先 ``:unblock``)。Chroma "
        "``soft_delete_by_chapter_id`` 把 ``metadata.chapter_id == target_id`` "
        "向量条目 metadata 合并 ``revoked: true``;SQLite ``characters`` / "
        "``plot_threads`` / ``timeline_entries`` 三表对应章节行 ``revoked_at`` "
        "翻 now。WS publish ``chapter_rewound`` + 每后续章节 ``chapter_invalidated``。"
        "源 status 必须在 ``reviewing/rewriting/revising/approved/published/"
        "human_intervention`` 内,否则 422 ``CHAPTER_REWIND_INVALID_STATE``。"
        "需 X-Workspace-Id header。"
    ),
)
async def rewind_chapter(
    chapter_id: str,
    body: ChapterRewindIn = Body(default_factory=ChapterRewindIn),
    workspace_id: str = Depends(get_workspace_id),
    service: ChapterRewindService = Depends(get_chapter_rewind_service),
) -> SuccessEnvelope[ChapterRewindOut]:
    _ = body
    result = service.rewind(workspace_id, chapter_id)
    return SuccessEnvelope(
        data=ChapterRewindOut(
            chapter_id=result.chapter_id,
            from_status=result.from_status,
            to_status="drafting",
            invalidated_chapter_ids=result.invalidated_chapter_ids,
            vector_revoked_count=result.vector_revoked_count,
            extracted_revoked_count=result.extracted_revoked_count,
        )
    )


@router.post(
    "/{chapter_id}:force-pass",
    response_model=SuccessEnvelope[ChapterForcePassOut],
    summary="强制审稿通过(跳过 Reviewer)",
    description=(
        "把 ``chapters.status`` 翻 ``approved``,``approved_at`` / ``force_pass_at`` "
        "/ ``force_pass_reason`` 持久化。**body.reason 强制非空**(对齐 "
        "PROJECT_SPEC §5.4 字面破坏性操作要 user 显式留痕)。源 status 在 "
        "``drafting/reviewing/rewriting/revising/human_intervention`` 内才能 "
        "force-pass;``approved`` / ``published`` 返 409 ``CHAPTER_INVALID_STATE``;"
        "``invalidated`` 返 409 ``CHAPTER_INVALIDATED_NEEDS_UNBLOCK``(先 unblock)。"
        "WS publish ``chapter_force_passed``。需 X-Workspace-Id header。"
    ),
)
async def force_pass_chapter(
    chapter_id: str,
    body: ChapterForcePassIn,
    workspace_id: str = Depends(get_workspace_id),
    service: ChapterRewindService = Depends(get_chapter_rewind_service),
) -> SuccessEnvelope[ChapterForcePassOut]:
    result = service.force_pass(workspace_id, chapter_id, reason=body.reason)
    return SuccessEnvelope(
        data=ChapterForcePassOut(
            chapter_id=result.chapter_id,
            from_status=result.from_status,
            to_status="approved",
            force_pass_at=result.force_pass_at.isoformat(),
            force_pass_reason=result.force_pass_reason,
        )
    )


@router.post(
    "/{chapter_id}:ignore-feasibility",
    response_model=SuccessEnvelope[TaskInfoChapterWrite],
    status_code=status.HTTP_202_ACCEPTED,
    summary="忽略写前可行性预检并写作(人工确认 Fix D 误判)",
    description=(
        "**v2 Fix 2**:写前 must 可行性预检(Fix D)把某章误判为「must 已被前文"
        "消费」升人工时,维护人在 EscalationDetailsBanner 复核确认是误判后,经本"
        "端点**跳过写前预检直接重写**——章节带完整 must_achieve 写作,写后 "
        "must_achieve_validator 仍兜底验真实际内容(**非无脑放过**)。立即返 202 + "
        "TaskInfo,实际闭环走后台 threadpool,前端订阅 ``task.{task_id}.progress``。"
        "**仅 ``human_intervention``(孤儿章)可调**;其余 status 返 409 "
        "``CHAPTER_INVALID_STATE``(防误用于健康 / 已完成章)。需 X-Workspace-Id header。"
    ),
)
async def ignore_feasibility_and_write(
    chapter_id: str,
    background_tasks: BackgroundTasks,
    body: ChapterWriteIn = Body(default_factory=ChapterWriteIn),
    workspace_id: str = Depends(get_workspace_id),
    service: ChapterService = Depends(get_chapter_service),
) -> SuccessEnvelope[TaskInfoChapterWrite]:
    _ensure_card_exists(service, workspace_id, chapter_id)
    # 守卫:只对 human_intervention 孤儿章可"忽略预检"——防误用于健康 / 已完成章。
    current = service.status(workspace_id, chapter_id)
    if current.status != "human_intervention":
        raise ChapterInvalidState(
            message=(
                "仅 human_intervention(孤儿章)可忽略写前可行性预检并重写;"
                f"当前 status={current.status}"
            ),
            details={"chapter_id": chapter_id, "current_status": current.status},
        )
    task_id, info = _reserve_chapter_worker(
        service=service,
        workspace_id=workspace_id,
        chapter_id=chapter_id,
        operation="write",
        task_type="chapter_write",
    )
    background_tasks.add_task(
        _run_chapter_cancellable,
        task_id,
        workspace_id,
        _run_chapter_write_task,
        service,
        workspace_id,
        chapter_id,
        task_id,
        profile_override=body.profile_override,
        skip_must_feasibility_precheck=True,
    )
    return SuccessEnvelope(data=TaskInfoChapterWrite.model_validate(info.model_dump()))


@router.post(
    "/{chapter_id}:unblock",
    response_model=SuccessEnvelope[ChapterUnblockOut],
    summary="unblock 章节(invalidated → drafting)",
    description=(
        "把 ``chapters.status`` 从 ``invalidated`` 翻回 ``drafting``。非 "
        "``invalidated`` 源返 422 ``CHAPTER_UNBLOCK_INVALID_STATE``。WS publish "
        "``chapter_unblocked``。批量 unblock 走 ``POST /workspaces/{id}:unblock-all``。"
        "需 X-Workspace-Id header。"
    ),
)
async def unblock_chapter(
    chapter_id: str,
    body: ChapterUnblockIn = Body(default_factory=ChapterUnblockIn),
    workspace_id: str = Depends(get_workspace_id),
    service: ChapterRewindService = Depends(get_chapter_rewind_service),
) -> SuccessEnvelope[ChapterUnblockOut]:
    _ = body
    result = service.unblock(workspace_id, chapter_id)
    return SuccessEnvelope(
        data=ChapterUnblockOut(
            chapter_id=result.chapter_id,
            from_status="invalidated",
            to_status="drafting",
        )
    )


@router.put(
    "/{chapter_id}/content",
    response_model=SuccessEnvelope[ChapterContentUpdateOut],
    summary="人工编辑章节正文(孤儿章人工解决流程)",
    description=(
        "v1.1 acceptance 修复2:维护人对 ``human_intervention`` 章人工编辑正文后"
        "保存 — 落新版本(``creation_type='manual_edit'``,版本号自增、LLM 旧版"
        "全保留)+ 切 final 指针 + 同步 ``current_version``(保证后续 "
        "``:force-pass`` 批准的是人工编辑版)。**仅 ``human_intervention`` 状态"
        "可调**,其余状态返 409 ``CHAPTER_INVALID_STATE``(防误改 approved / "
        "进行中章节)。批准走既有 ``POST :force-pass``(批准后自动补完整记忆"
        "抽取)。WS publish ``chapter_content_updated``。需 X-Workspace-Id header。"
    ),
)
async def update_chapter_content(
    chapter_id: str,
    body: ChapterContentUpdateIn,
    workspace_id: str = Depends(get_workspace_id),
    service: ChapterRewindService = Depends(get_chapter_rewind_service),
) -> SuccessEnvelope[ChapterContentUpdateOut]:
    result = service.update_content_manual(
        workspace_id, chapter_id, content=body.content
    )
    return SuccessEnvelope(
        data=ChapterContentUpdateOut(
            chapter_id=result.chapter_id,
            version_id=result.version_id,
            word_count=result.word_count,
        )
    )


# ---- 状态查询 / 章节卡 / 版本列表与正文 -------------------------------------


@router.get(
    "/{chapter_id}/status",
    response_model=SuccessEnvelope[ChapterStatusOut],
    summary="查询章节状态",
    description=(
        "P1 简版从文件系统推断 3 档(not_started / approved / unknown)。"
        "P2 落 chapters 表后升级为完整 7 档,前端因已强制处理 unknown 不需重写。"
        "需 X-Workspace-Id。"
    ),
)
async def get_chapter_status(
    chapter_id: str,
    workspace_id: str = Depends(get_workspace_id),
    service: ChapterService = Depends(get_chapter_service),
) -> SuccessEnvelope[ChapterStatusOut]:
    info = service.status(workspace_id, chapter_id)
    return SuccessEnvelope(data=ChapterStatusOut.model_validate(info.model_dump()))


@router.get(
    "/{chapter_id}/card",
    response_model=SuccessEnvelope[ChapterCardOut],
    summary="读章节卡",
    description="读 data/cards/<chapter_id>.json。需 X-Workspace-Id。",
)
async def get_chapter_card(
    chapter_id: str,
    workspace_id: str = Depends(get_workspace_id),
    service: ChapterService = Depends(get_chapter_service),
) -> SuccessEnvelope[ChapterCardOut]:
    _ensure_card_exists(service, workspace_id, chapter_id)
    card = service.read_chapter_card(workspace_id, chapter_id)
    return SuccessEnvelope(data=ChapterCardOut.model_validate(card.model_dump()))


@router.get(
    "/{chapter_id}/review-history",
    response_model=SuccessEnvelope[list[ReviewSummaryEntry]],
    summary="读章节审稿历史(从 review_results 表)",
    description=(
        "v1.0 hot-fix B-01:从 ``review_results`` 表查持久化的审稿历史 "
        "(替代 v1 期前端走进程内 task_registry LRU 的丢失路径 — 服务器重启 / "
        "页面刷新都丢)。返回按 ``reviewed_at`` 升序的 ``ReviewSummaryEntry`` "
        "列表,等价 ``:write`` / ``:rewrite`` 任务 result.review_summary 字段。"
        "工作区 / 章节不存在 / 无审稿历史 → 返空列表(不抛 404,前端能区分"
        "“没跑过” vs “任务过期”)。需 X-Workspace-Id。"
    ),
)
async def get_chapter_review_history(
    chapter_id: str,
    workspace_id: str = Depends(get_workspace_id),
    service: ChapterService = Depends(get_chapter_service),
) -> SuccessEnvelope[list[ReviewSummaryEntry]]:
    entries = service.list_review_history(workspace_id, chapter_id)
    return SuccessEnvelope(data=[ReviewSummaryEntry.model_validate(e) for e in entries])


@router.post(
    "/{chapter_id}/card:generate",
    response_model=SuccessEnvelope[TaskInfo],
    status_code=status.HTTP_202_ACCEPTED,
    summary="生成章节卡(单章 LLM,异步)",
    description=(
        "**Commit B(2026-05-21)起改 BackgroundTasks 模式**:立即返 202 + "
        "TaskInfo(status=pending,task_id);实际 ChapterCardGenerator LLM "
        "调用走后台 threadpool。result.data = ChapterCardOut(整张章节卡)。"
        "从 outline 4 段 + volumes 表 + characters/plot_threads + 上一章 "
        "final.md 派生上下文,LLM 产创意决策字段后落 ``data/cards/<chapter_id>"
        ".json`` SSOT + upsert ``chapter_cards`` 表行(card_status='draft')。"
        "outline:commit 只生成首卡；正式 Run 在上一章富记忆 succeeded 后，"
        "通过本单章路径生成当前下一张卡，不预生成未来卡。前端订阅 "
        "``task.{task_id}.progress`` 频道接"
        "事件。需 X-Workspace-Id。"
    ),
)
async def generate_chapter_card_endpoint(
    chapter_id: str,
    body: ChapterCardGenerateIn,
    background_tasks: BackgroundTasks,
    workspace_id: str = Depends(get_workspace_id),
    service: ChapterCardService = Depends(get_chapter_card_service),
) -> SuccessEnvelope[TaskInfo]:
    task_id = task_registry.new_task_id()
    info = task_registry.record_pending(task_id, type="chapter_card_generate")
    background_tasks.add_task(
        _run_cancellable,
        task_id,
        workspace_id,
        _run_chapter_card_generate_task,
        service,
        workspace_id,
        chapter_id,
        task_id,
        chapter_number=body.chapter_number,
        volume_id=body.volume_id,
    )
    return SuccessEnvelope(data=info)


@router.post(
    "/{chapter_id}:retry-references",
    response_model=SuccessEnvelope[ChapterCardRetryReferencesOut],
    summary="重试章节卡引用合法性 + cascade 级联重生(P6-B-10 → v1.0.x wave 5 session 4)",
    description=(
        "重新生成章节卡(走 ChapterCardGenerator agent + 严格 ID 约束 banner)。"
        "供前端 ChapterCardReviewPage 在 reject 状态下用户点'重试引用'按钮触发,"
        "或 chapter:write 触发 REFERENCE_VALIDATION_FAILED 422 时用户手动修复 — "
        "服务层调 ``ChapterCardService.retry_references`` 走严格 ID 约束模式,"
        "agent prompt 顶部注入'已知 ID 列表'banner + ``character_voices`` / "
        "``plot_threads`` 过滤到只剩工作区 SQLite 表实有 ID;单次重试仍失败"
        "返 422 ``REFERENCE_VALIDATION_FAILED``,``details`` 含 "
        "``unknown_character_ids`` / ``unknown_thread_ids``。新生卡默认 "
        "``card_status='draft'`` 覆盖既有卡。\n\n"
        "**v1.0.x wave 5 session 4 升级**:① 注入同卷其他 approved 章 must_achieve "
        "作 forbidden_phrases 防同质化;② 收集后续章节卡 chapter_id 列表 + 注册 "
        "BackgroundTask 跑级联重生(``regenerate_invalidated_cards_batch``),"
        "前端按 ``invalidated_chapter_ids`` 提示用户级联范围 + 订阅 "
        "``task.{cascade_regenerate_task_id}.progress`` WS 频道接进度。"
        "需 X-Workspace-Id。"
    ),
)
async def retry_chapter_card_references(
    chapter_id: str,
    body: ChapterCardRetryReferencesIn,
    background_tasks: BackgroundTasks,
    workspace_id: str = Depends(get_workspace_id),
    service: ChapterCardService = Depends(get_chapter_card_service),
) -> SuccessEnvelope[ChapterCardRetryReferencesOut]:
    with bind_selection_or_production_scope(
        workspace_id,
        chapter_id=chapter_id,
    ):
        result = service.retry_references(
            workspace_id,
            chapter_id,
            chapter_number=body.chapter_number,
            volume_id=body.volume_id,
            card_status="draft",
        )
    # v1.0.x wave 5 session 4 (k):cascade 重生
    cascade_task_id: str | None = None
    if result.invalidated_chapter_ids:
        cascade_task_id = task_registry.new_task_id()
        task_registry.record_pending(
            cascade_task_id, type="chapter_card_cascade_regenerate"
        )
        background_tasks.add_task(
            _run_cancellable,
            cascade_task_id,
            workspace_id,
            _run_chapter_card_cascade_regenerate_task,
            service,
            workspace_id,
            cascade_task_id,
            result.invalidated_chapter_ids,
        )
    return SuccessEnvelope(
        data=ChapterCardRetryReferencesOut(
            card=ChapterCardOut.model_validate(result.card.model_dump()),
            invalidated_chapter_ids=result.invalidated_chapter_ids,
            cascade_regenerate_task_id=cascade_task_id,
        )
    )


@router.post(
    "/{chapter_id}/card:approve",
    response_model=SuccessEnvelope[ChapterCardStatusOut],
    summary="章节卡审核通过",
    description=(
        "把 ``chapter_cards.card_status`` 从 ``draft`` / ``pending_review`` / "
        "``rejected`` 翻到 ``approved``,记 ``card_approved_at = now``;**清除**"
        "上一次的 ``rejection_notes``(避免历史遗留)。从 ``approved`` 再 approve "
        "返 409 ``CARD_INVALID_STATE_TRANSITION``。同步 patch SSOT 文件 "
        "``data/cards/<chapter_id>.json`` 的 ``card_status`` / ``card_approved_at`` "
        "(若文件存在);WS publish ``card_status_changed`` + ``card_approved`` 2 "
        "事件到 ``chapter.{id}.card.events`` 频道。需 X-Workspace-Id。"
    ),
)
async def approve_chapter_card(
    chapter_id: str,
    body: ChapterCardApproveIn = Body(default_factory=ChapterCardApproveIn),
    workspace_id: str = Depends(get_workspace_id),
    card_service: ChapterCardService = Depends(get_chapter_card_service),
) -> SuccessEnvelope[ChapterCardStatusOut]:
    # body 当前无字段(空 schema),保留入参位 P6+ 扩 approver_id / reason。
    _ = body
    result = card_service.transition_card_status(
        workspace_id,
        chapter_id,
        action="approve",
    )
    return SuccessEnvelope(
        data=ChapterCardStatusOut(
            chapter_id=result["chapter_id"],
            from_status=result["from_status"],
            to_status=result["to_status"],
            card_approved_at=result.get("card_approved_at"),
            rejection_notes=None,
        )
    )


@router.post(
    "/{chapter_id}/card:reject",
    response_model=SuccessEnvelope[ChapterCardStatusOut],
    summary="章节卡审核退回",
    description=(
        "把 ``chapter_cards.card_status`` 从 ``draft`` / ``pending_review`` / "
        "``approved`` 翻到 ``rejected``;持久化可选 ``rejection_notes`` 到 "
        "ORM。从 ``rejected`` 再 reject 返 409 ``CARD_INVALID_STATE_TRANSITION``。"
        "同步 patch SSOT 文件 ``card_status``(SSOT 不含 ``rejection_notes``);"
        "WS publish ``card_status_changed`` + ``card_rejected`` 2 事件到 "
        "``chapter.{id}.card.events`` 频道。需 X-Workspace-Id。"
    ),
)
async def reject_chapter_card(
    chapter_id: str,
    body: ChapterCardRejectIn = Body(default_factory=ChapterCardRejectIn),
    workspace_id: str = Depends(get_workspace_id),
    card_service: ChapterCardService = Depends(get_chapter_card_service),
) -> SuccessEnvelope[ChapterCardStatusOut]:
    result = card_service.transition_card_status(
        workspace_id,
        chapter_id,
        action="reject",
        rejection_notes=body.rejection_notes,
    )
    return SuccessEnvelope(
        data=ChapterCardStatusOut(
            chapter_id=result["chapter_id"],
            from_status=result["from_status"],
            to_status=result["to_status"],
            card_approved_at=None,
            rejection_notes=result.get("rejection_notes"),
        )
    )


@router.put(
    "/{chapter_id}/card",
    response_model=SuccessEnvelope[ChapterCardOut],
    summary="写章节卡(完整覆盖)",
    description=(
        "完整覆盖写入章节卡。请求体内 chapter_id 必须与 URL 一致,否则抛"
        " CHAPTER_ID_MISMATCH(422)。需 X-Workspace-Id。"
    ),
)
async def put_chapter_card(
    chapter_id: str,
    body: ChapterCard,
    workspace_id: str = Depends(get_workspace_id),
    service: ChapterService = Depends(get_chapter_service),
) -> SuccessEnvelope[ChapterCardOut]:
    if body.chapter_id != chapter_id:
        raise ChapterIdMismatch(
            f"请求体内 chapter_id='{body.chapter_id}' 与 URL '{chapter_id}' 不一致"
        )
    service.save_chapter_card(workspace_id, body)
    return SuccessEnvelope(data=ChapterCardOut.model_validate(body.model_dump()))


@router.post(
    "/{chapter_id}/card",
    response_model=SuccessEnvelope[ChapterCardOut],
    summary="上传章节卡 JSON(P5-B-08)",
    description=(
        "上传整 ChapterCard JSON。落 SSOT 文件 ``data/cards/<chapter_id>.json`` "
        "+ upsert ``chapter_cards`` ORM 行(``card_status`` 从 body 取,默认 "
        "``draft``)。请求体内 chapter_id 必须与 URL 一致,否则抛 "
        "``CHAPTER_ID_MISMATCH (422)``。**不**触发 WS event(对齐 P5-B-07 "
        "§6 接力 marker — 上传是创建/覆盖,非状态翻转)。与 PUT 互补:PUT 仅写"
        "文件,本端点同步写 ORM。需 X-Workspace-Id。"
    ),
)
async def post_chapter_card(
    chapter_id: str,
    body: ChapterCard,
    workspace_id: str = Depends(get_workspace_id),
    card_service: ChapterCardService = Depends(get_chapter_card_service),
) -> SuccessEnvelope[ChapterCardOut]:
    if body.chapter_id != chapter_id:
        raise ChapterIdMismatch(
            f"请求体内 chapter_id='{body.chapter_id}' 与 URL '{chapter_id}' 不一致"
        )
    saved = card_service.save_uploaded_card(workspace_id, chapter_id, body)
    return SuccessEnvelope(data=ChapterCardOut.model_validate(saved.model_dump()))


@router.get(
    "/{chapter_id}/validators/latest",
    response_model=SuccessEnvelope[ValidatorReportOut],
    summary="读章节最近一次 validator 跑批报告(P6-B-11)",
    description=(
        "现跑现产 [HARD] validator pipeline 报告(P4-B-06 / P4-B-07 落地的 4+6 个 "
        "validator),供前端 ``ChapterCardReviewPage`` "
        "'关键字段达成对比'块消费(must_achieve 逐项打勾 / forbidden 命中红色 / "
        "must_unresolved 守住悬念 / 字数范围 / 角色存在性 / 时间线单调 / 死亡铺垫 / "
        "voice forbidden / 衔接桥未完成动作 / 场景延续地点)。"
        "**纯函数 + 子串检索 + ORM 查询无 LLM 调用**,~ms 级开销;不持久化,每次 "
        "GET 都重跑(数据与最新 final.md + 章节卡 + 角色 / 时间线对齐)。"
        "前置:章节卡必存在(否则 404 ``CHAPTER_CARD_NOT_FOUND``);"
        "``final.md`` 必存在(否则 404 ``VALIDATOR_REPORT_NOT_FOUND``,"
        "前端可据此提示用户先 :write)。需 X-Workspace-Id。"
    ),
)
async def get_chapter_validator_report(
    chapter_id: str,
    workspace_id: str = Depends(get_workspace_id),
    chapter_service: ChapterService = Depends(get_chapter_service),
    service: ValidatorReportService = Depends(get_validator_report_service),
) -> SuccessEnvelope[ValidatorReportOut]:
    _ensure_card_exists(chapter_service, workspace_id, chapter_id)
    payload = service.get_latest_report(workspace_id, chapter_id)
    return SuccessEnvelope(data=ValidatorReportOut.model_validate(payload))


@router.get(
    "/{chapter_id}/versions",
    response_model=SuccessEnvelope[ChapterVersionList],
    summary="列出章节所有版本号",
    description=(
        "P1 阶段从文件系统推断,只返 version_id 数组(元数据未持久化)。"
        "P2 落 chapter_versions 表后扩字段。需 X-Workspace-Id。"
    ),
)
async def list_chapter_versions(
    chapter_id: str,
    workspace_id: str = Depends(get_workspace_id),
    service: ChapterService = Depends(get_chapter_service),
) -> SuccessEnvelope[ChapterVersionList]:
    info = service.status(workspace_id, chapter_id)
    items = [ChapterVersionItem(version_id=i) for i in range(1, info.version_count + 1)]
    return SuccessEnvelope(
        data=ChapterVersionList(chapter_id=chapter_id, versions=items)
    )


@router.get(
    "/{chapter_id}/versions/{version_id}",
    response_model=SuccessEnvelope[ChapterVersionContent],
    summary="读单个章节版本正文",
    description=(
        "读 v{version_id}.md。文件不存在 → ChapterVersioningError → 全局翻译表"
        "映射为 CHAPTER_VERSION_CONFLICT(409)。需 X-Workspace-Id。"
    ),
)
async def get_chapter_version(
    chapter_id: str,
    version_id: int,
    workspace_id: str = Depends(get_workspace_id),
    service: ChapterService = Depends(get_chapter_service),
) -> SuccessEnvelope[ChapterVersionContent]:
    content = service.read_version_content(workspace_id, chapter_id, version_id)
    return SuccessEnvelope(
        data=ChapterVersionContent(
            chapter_id=chapter_id,
            version_id=version_id,
            content=content,
            word_count=len(content),
        )
    )


@router.get(
    "/{chapter_id}/style-drift",
    response_model=SuccessEnvelope[StyleDriftSeries],
    summary="章节各版本风格指纹时序(v1.1 w3-s7 H7)",
    description=(
        "读 ``chapter_versions.style_fingerprint``(G5 落盘),按 version_id 升序"
        "返每版本 4 数值维度(段落均值 / 句长均值 / 比喻密度 / 感叹密度)+ "
        "主导人称。供前端折线图展示 v1→vN 风格漂移。无 session_factory / 无版本"
        "行 → points 空列表;旧版本无 fingerprint → has_fingerprint=false。"
        "需 X-Workspace-Id。"
    ),
)
async def get_chapter_style_drift(
    chapter_id: str,
    workspace_id: str = Depends(get_workspace_id),
    service: ChapterService = Depends(get_chapter_service),
) -> SuccessEnvelope[StyleDriftSeries]:
    points = service.style_fingerprint_series(workspace_id, chapter_id)
    return SuccessEnvelope(
        data=StyleDriftSeries(
            chapter_id=chapter_id,
            points=[
                StyleFingerprintPoint(
                    version_id=p.version_id,
                    creation_type=p.creation_type,
                    has_fingerprint=p.has_fingerprint,
                    paragraph_length_mean=p.paragraph_length_mean,
                    sentence_length_mean=p.sentence_length_mean,
                    simile_density=p.simile_density,
                    exclamation_density=p.exclamation_density,
                    dominant_pov=p.dominant_pov,
                )
                for p in points
            ],
        )
    )


# ---- 列表查询(P1-B-25) ----------------------------------------------------


@router.get(
    "",
    response_model=SuccessEnvelope[list[ChapterListItem]],
    summary="列出工作区所有章节",
    description=(
        "扫工作区 data/cards/*.json,合并 status 推断结果按 chapter_number 升序返。"
        "P1 阶段无 chapters 表,只能从文件系统推断,字段并集对齐 P1-F-10 看板 + "
        "P1-F-11 列表行需求。单张坏卡 structlog warn 跳过(看板/列表页不被一张坏卡 "
        "fail);单卡端点 GET /card 行为不变,仍按存在性/合法性双轨抛 422。需 "
        "X-Workspace-Id。"
    ),
)
async def list_chapters(
    workspace_id: str = Depends(get_workspace_id),
    service: ChapterService = Depends(get_chapter_service),
) -> SuccessEnvelope[list[ChapterListItem]]:
    entries = service.list_chapters(workspace_id)
    items = [ChapterListItem.model_validate(e.model_dump()) for e in entries]
    return SuccessEnvelope(data=items)
