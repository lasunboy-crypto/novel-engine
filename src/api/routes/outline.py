"""Outline 路由(P5-B-04)。

对齐 API_CONTRACT §4.2 关键流程 1 字面 5 端点 + p5_plan.md §8 outline:commit
gate + PROJECT_SPEC §4.1 [HARD] 副本横跨规则。

5 个端点
========

| 方法 | 路径 | 作用 |
|------|------|------|
| POST | ``/workspaces/{id}/outline:generate`` | 4 段串行生成 + WS 5 stage |
| POST | ``/workspaces/{id}/outline:regenerate-section`` | 单段重生 |
| POST | ``/workspaces/{id}/outline:commit`` | 跑 gate + 落 volumes 表 |
| GET  | ``/workspaces/{id}/outline`` | 读 4 段聚合 |
| PUT  | ``/workspaces/{id}/outline`` | 单段或整段 patch |

工作区上下文
============

与 ``memory_health.router`` 同模板:``workspace_id`` 走 URL path,**不要**
``X-Workspace-Id`` header — 端点字面就是"为指定工作区操作大纲"。

任务模式
========

P5-B-04 ``:generate`` 同步执行(对齐 P1-B-19 决策 2 chapters ``:write``
模式),跑完构造 ``TaskInfo(status="done", result=OutlineGenerateResult)``
写入 ``task_registry`` 后返 200。``GET /tasks/{task_id}`` 在进程内 LRU
字典查;P3+ 后台 worker 化时改 202 + ``status="pending"``,客户端契约
不变。
"""

from __future__ import annotations

from collections.abc import Callable
from typing import Any

from fastapi import APIRouter, BackgroundTasks, Body, Depends, status

from src.api import task_registry
from src.api.deps import get_chapter_card_service, get_outline_service
from src.api.errors import ApiError, _translate_service_error
from src.api.schemas.chapter_cards import (
    ChapterCardsResetWordCountIn,
    ChapterCardsResetWordCountResult,
)
from src.api.schemas.common import SuccessEnvelope
from src.api.schemas.outline import (
    OutlineCommitResult,
    OutlineGenerateIn,
    OutlineGenerateResult,
    OutlineOut,
    OutlineRegenerateSectionIn,
    OutlineUpdateIn,
)
from src.api.schemas.tasks import TaskInfo
from src.api.websocket.channels import task_progress_channel
from src.api.websocket.manager import manager as ws_manager
from src.core.llm.cancel import LLMCancelledError
from src.core.workspace.meta import MetaFileError
from src.services.chapter_card_service import (
    ChapterCardService,
    ChapterCardServiceError,
)
from src.core.models.narrative import PlanningMode
from src.services.outline_service import OutlineService
from src.services.post_v8_abc_selection_activity import (
    bind_selection_or_production_scope,
)
from src.services.workspace_service import WorkspaceServiceError

router = APIRouter(prefix="/workspaces", tags=["outline"])


class ChapterCardsResetInvalid(ApiError):
    """批量重置章节卡字数参数不合法。"""

    # 保留既有公开错误码兼容性。它历史上同时服务过已退役的批量生卡端点与
    # 仍保留的字数批量重置端点；移除前者不应顺手破坏后者的调用方。
    code = "CHAPTER_CARDS_BATCH_INVALID"
    http_status = 422
    default_message = "批量重置章节卡参数不合法"


class OutlineGenerationAlreadyRunning(ApiError):
    """同一工作区已有大纲生成 Worker 时拒绝重复付费提交。"""

    code = "OUTLINE_GENERATION_ALREADY_RUNNING"
    http_status = 409
    default_message = "大纲生成任务正在运行，请等待原任务完成"


class OutlineCheckpointAlreadyExists(ApiError):
    """已有同内容 done checkpoint 时拒绝重新生成。"""

    code = "OUTLINE_CHECKPOINT_ALREADY_EXISTS"
    http_status = 409
    default_message = "大纲已生成完成，请恢复原任务并提交，勿重复生成"


# ---- 内部辅助 ---------------------------------------------------------------


def _build_outline_out_from_full(full) -> OutlineOut:  # type: ignore[no-untyped-def]
    """``FullOutline`` → ``OutlineOut`` 转换(v1.0.x wave 4 session 1 起 5 段全填)。"""
    return OutlineOut(
        world=full.world,
        characters=full.characters,
        plot=full.plot,
        volume_plans=full.volume_plans,
        mini_plan=full.mini_plan,
    )


def _matching_completed_outline_task(
    service: OutlineService, workspace_id: str
) -> TaskInfo | None:
    """以完整当前五段 + task done result 精确匹配可恢复 checkpoint。"""
    try:
        outline = _build_outline_out_from_workspace(service, workspace_id)
    except (MetaFileError, WorkspaceServiceError):
        return None  # 保持未知工作区由既有后台任务路径登记 failed 的合同
    if any(
        section is None
        for section in (
            outline.world,
            outline.characters,
            outline.plot,
            outline.volume_plans,
            outline.mini_plan,
        )
    ):
        return None
    expected = outline.model_dump(mode="json")
    for task_type in ("outline_regenerate_section", "outline_generate"):
        for task in task_registry.list_done_tasks(
            type=task_type,
            workspace_id=workspace_id,
            include_unbound=True,
        ):
            result = task.result
            if result is None:
                continue
            if task_type == "outline_generate":
                if result.get("task_id") != task.task_id:
                    continue
                candidate = result.get("outline")
            else:
                candidate = result
            if candidate == expected:
                return task
    return None


def _reserve_outline_generation(
    service: OutlineService, workspace_id: str
) -> TaskInfo:
    """防 active 重复，也封住 active→done 与第二次提交交错的竞态。"""
    completed = _matching_completed_outline_task(service, workspace_id)
    if completed is not None:
        raise OutlineCheckpointAlreadyExists(
            details={"workspace_id": workspace_id, "task_id": completed.task_id}
        )
    task_id = task_registry.new_task_id()
    info, created = task_registry.record_pending_if_no_active(
        task_id, type="outline_generate", workspace_id=workspace_id
    )
    if not created:
        raise OutlineGenerationAlreadyRunning(
            details={"workspace_id": workspace_id, "active_task_id": info.task_id}
        )
    completed = _matching_completed_outline_task(service, workspace_id)
    if completed is not None:
        error = {
            "code": "OUTLINE_CHECKPOINT_ALREADY_EXISTS",
            "message": "预约期间原大纲任务已完成，未启动重复 Worker",
            "details": {"completed_task_id": completed.task_id},
        }
        task_registry.update_to_failed(task_id, error=error)
        raise OutlineCheckpointAlreadyExists(
            details={"workspace_id": workspace_id, "task_id": completed.task_id}
        )
    return info


# ---- v1.0.x Commit B(2026-05-21):BackgroundTasks + WS progress 改造 -----
#
# 见 src/api/routes/chapters.py 顶部同段注释。本模块覆盖 :generate 和
# :regenerate-section 两端点(原 sync 路径阻塞 event loop)。


def _publish_task_event(task_id: str, event: str, data: dict[str, Any]) -> None:
    """统一推送到 ``task.{task_id}.progress`` 频道。"""
    ws_manager.publish(
        channel=task_progress_channel(task_id),
        event=event,
        data=data,
    )


def _build_error_dict(exc: BaseException) -> dict[str, Any]:
    """异常 → 结构化 error dict(对齐 ``API_CONTRACT §3.2`` ErrorPayload)。"""
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


def _check_workspace_destroyed_and_cancel(
    workspace_id: str, task_id: str
) -> bool:
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

    详 ``src/api/routes/chapters.py`` 同名函数 docstring(每路由文件各自持有以复用
    本文件的 ``_publish_task_event``;helper 在各路由文件本就重复定义)。
    """
    try:
        with task_registry.bind_cancel_event(task_id, workspace_id):
            fn(*args, **kwargs)
    except LLMCancelledError:
        task_registry.update_to_cancelled(task_id)
        _publish_task_event(task_id, "cancelled", {"task_id": task_id})


def _run_outline_generate_task(
    service: OutlineService,
    workspace_id: str,
    task_id: str,
    *,
    premise: str,
    total_volumes: int,
    chapters_per_volume: int,
    tension_template,
    genre: str,
    planning_mode: PlanningMode = "whole_book",
) -> None:
    """BackgroundTasks 后台跑 5 段串行 outline 生成 + WS publish
    (v1.0.x wave 4 session 1 起从 4 段扩到 5 段加 mini_plan)。

    OutlineService 内部 ``_publish_stage`` 推 5 段 stage event(world /
    characters / plot / volume_plans / mini_plan);**整体完成的 done
    event 由本 wrapper 末尾 publish**(v1.0.x wave 3 Commit B follow-up
    2026-05-21 — 对齐 chapters.py / :regenerate-section 同型 ``event="done"``
    模式,修 NewBookPage 永远卡"生成中"bug。前任 Commit B 注释误判
    "OutlineService 内部已 publish 'done' stage event",实际只 publish
    ``event="stage"`` data.stage="done",前端 ``frame.event === "done"``
    永远不匹配)。
    """
    # v1.0.x wave 6 s2.6a:入口防御 — 工作区已销毁直接 early-return + failed publish
    if _check_workspace_destroyed_and_cancel(workspace_id, task_id):
        return
    try:
        with bind_selection_or_production_scope(
            workspace_id, task_id=task_id
        ):
            full = service.generate_outline(
                workspace_id,
                premise=premise,
                total_volumes=total_volumes,
                chapters_per_volume=chapters_per_volume,
                tension_template=tension_template,
                genre=genre,
                task_id=task_id,
                planning_mode=planning_mode,
            )
    except Exception as exc:  # noqa: BLE001
        error = _build_error_dict(exc)
        task_registry.update_to_failed(task_id, error=error)
        _publish_task_event(task_id, "failed", {"task_id": task_id, "error": error})
        return
    result = OutlineGenerateResult(
        task_id=task_id, outline=_build_outline_out_from_full(full)
    )
    result_dict = result.model_dump(mode="json")
    task_registry.update_to_done(task_id, result=result_dict)
    _publish_task_event(task_id, "done", {"task_id": task_id, "result": result_dict})


def _run_outline_regenerate_section_task(
    service: OutlineService,
    workspace_id: str,
    task_id: str,
    body: OutlineRegenerateSectionIn,
) -> None:
    """BackgroundTasks 后台跑单段重生 + WS publish。

    service.regenerate_section 本身不感知 task_id / ws_manager,这里
    wrapper 自管 stage / done / failed。
    """
    # v1.0.x wave 6 s2.6a:入口防御 — 工作区已销毁直接 early-return + failed publish
    if _check_workspace_destroyed_and_cancel(workspace_id, task_id):
        return
    _publish_task_event(task_id, "stage", {"stage": "running", "task_id": task_id})
    try:
        with bind_selection_or_production_scope(
            workspace_id, task_id=task_id
        ):
            service.regenerate_section(
                workspace_id,
                section=body.section,
                premise=body.premise,
                total_volumes=body.total_volumes,
                chapters_per_volume=body.chapters_per_volume,
                tension_template=body.tension_template,
                genre=body.genre,
                task_id=task_id,
                planning_mode=body.planning_mode,
            )
    except Exception as exc:  # noqa: BLE001
        error = _build_error_dict(exc)
        task_registry.update_to_failed(task_id, error=error)
        _publish_task_event(task_id, "failed", {"task_id": task_id, "error": error})
        return
    # 重生成成功后读取 4 段聚合作为 result(等价老路由返回体)
    outline = _build_outline_out_from_workspace(service, workspace_id)
    result_dict = outline.model_dump(mode="json")
    task_registry.update_to_done(task_id, result=result_dict)
    _publish_task_event(task_id, "done", {"task_id": task_id, "result": result_dict})


def _run_first_chapter_card_after_commit_task(
    card_service: ChapterCardService,
    workspace_id: str,
    task_id: str,
) -> None:
    """commit_outline 后首张章节卡生成 BackgroundTask wrapper。

    service 内部管 stage / done event publish + task_registry update；本 wrapper
    加入口防御 + 异常兜底
    publish failed(避免 BackgroundTask 抛错时 task 永远卡 pending)。
    """
    if _check_workspace_destroyed_and_cancel(workspace_id, task_id):
        return
    try:
        with bind_selection_or_production_scope(
            workspace_id,
            task_id=task_id,
            chapter_id="ch-0001",
        ):
            card_service.generate_first_card_after_commit(
                workspace_id,
                task_id=task_id,
            )
    except Exception as exc:  # noqa: BLE001
        error = _build_error_dict(exc)
        task_registry.update_to_failed(task_id, error=error)
        _publish_task_event(
            task_id, "failed", {"task_id": task_id, "error": error}
        )


# ---- 动作端点 ---------------------------------------------------------------


@router.post(
    "/{workspace_id}/outline:generate",
    response_model=SuccessEnvelope[TaskInfo],
    status_code=status.HTTP_202_ACCEPTED,
    summary="生成大纲(4 段串行,异步)",
    description=(
        "**Commit B(2026-05-21)起改 BackgroundTasks 模式**:立即返 202 + "
        "TaskInfo(status=pending,task_id);实际 5 段串行 OutlineBuilder LLM "
        "调用走后台 threadpool(v1.0.x wave 4 session 1 起从 4 段扩到 5 段加 "
        "mini_plan)。OutlineService 内部推每段完成的 ``event=stage`` "
        "(stage ∈ {world, characters, plot, volume_plans, mini_plan});全部 "
        "完成由路由层 wrapper publish ``event=done``(对齐 chapters.py 同型;"
        "Commit B follow-up 2026-05-21 修)。result = {task_id, outline:OutlineOut}。"
        "前端订阅 ``task.{task_id}.progress`` 频道接事件,done 后跳 outline "
        "编辑页。"
    ),
)
async def generate_outline(
    workspace_id: str,
    body: OutlineGenerateIn,
    background_tasks: BackgroundTasks,
    service: OutlineService = Depends(get_outline_service),
) -> SuccessEnvelope[TaskInfo]:
    info = _reserve_outline_generation(service, workspace_id)
    task_id = info.task_id
    background_tasks.add_task(
        _run_cancellable,
        task_id,
        workspace_id,
        _run_outline_generate_task,
        service,
        workspace_id,
        task_id,
        premise=body.premise,
        total_volumes=body.total_volumes,
        chapters_per_volume=body.chapters_per_volume,
        tension_template=body.tension_template,
        genre=body.genre,
        planning_mode=body.planning_mode,
    )
    return SuccessEnvelope(data=info)


@router.post(
    "/{workspace_id}/outline:regenerate-section",
    response_model=SuccessEnvelope[TaskInfo],
    status_code=status.HTTP_202_ACCEPTED,
    summary="单段重新生成(异步)",
    description=(
        "**Commit B(2026-05-21)起改 BackgroundTasks 模式**:立即返 202 + "
        "TaskInfo(status=pending,task_id);实际单段 LLM 调用走后台 threadpool。"
        "复用同段 prompt 单段重生;上游产出从已落盘 JSON 文件读取作为只读 "
        "context 透传(不重跑上游)。result = OutlineOut 4 段聚合(其他段"
        "保留落盘版本)。前端订阅 ``task.{task_id}.progress`` 频道接事件。"
    ),
)
async def regenerate_outline_section(
    workspace_id: str,
    body: OutlineRegenerateSectionIn,
    background_tasks: BackgroundTasks,
    service: OutlineService = Depends(get_outline_service),
) -> SuccessEnvelope[TaskInfo]:
    task_id = task_registry.new_task_id()
    info = task_registry.record_pending(
        task_id,
        type="outline_regenerate_section",
        workspace_id=workspace_id,
    )
    background_tasks.add_task(
        _run_cancellable,
        task_id,
        workspace_id,
        _run_outline_regenerate_section_task,
        service,
        workspace_id,
        task_id,
        body,
    )
    return SuccessEnvelope(data=info)


def _build_outline_out_from_workspace(
    service: OutlineService, workspace_id: str
) -> OutlineOut:
    """读 5 段聚合(v1.0.x wave 4 session 1 起);底层 service.read_outline 已校验工作区存在。"""
    data = service.read_outline(workspace_id)
    return OutlineOut.model_validate(
        {
            "world": data["world"],
            "characters": data["characters"],
            "plot": data["plot"],
            "volume_plans": data["volume_plans"],
            "mini_plan": data.get("mini_plan"),
        }
    )


@router.post(
    "/{workspace_id}/outline:commit",
    response_model=SuccessEnvelope[OutlineCommitResult],
    summary="提交大纲(跑硬约束 gate + 落 volumes 表 + 异步生成首卡)",
    description=(
        "前置跑 [HARD] gate 校验(目前包含'副本类剧情 ≥ 3 章');全过则把 "
        "volume_plans.volumes 各项映射 VolumePlanItem → VolumeORM 落 "
        "volumes 表(upsert)。任一 gate 失败返 422 OUTLINE_ARC_TOO_SHORT "
        "+ error.details.violations 列违规 arc 区间。"
        "落库成功后只启动一次异步首卡任务，生成 ch-0001 并进入 "
        "pending_review。后续卡必须由正式 Run 在上一章富记忆 succeeded 后逐章生成。"
        "返回体含 ``first_card_task_id``，客户端可订阅对应任务进度。"
    ),
)
async def commit_outline(
    workspace_id: str,
    background_tasks: BackgroundTasks,
    service: OutlineService = Depends(get_outline_service),
    card_service: ChapterCardService = Depends(get_chapter_card_service),
) -> SuccessEnvelope[OutlineCommitResult]:
    volumes = service.commit_outline(workspace_id)
    first_card_task_id = task_registry.new_task_id()
    task_registry.record_pending(
        first_card_task_id,
        type="chapter_card_first_after_commit",
        workspace_id=workspace_id,
    )
    background_tasks.add_task(
        _run_cancellable,
        first_card_task_id,
        workspace_id,
        _run_first_chapter_card_after_commit_task,
        card_service,
        workspace_id,
        first_card_task_id,
    )
    return SuccessEnvelope(
        data=OutlineCommitResult(
            workspace_id=workspace_id,
            volumes_count=len(volumes),
            gates_passed=True,
            first_card_task_id=first_card_task_id,
        )
    )


@router.post(
    "/{workspace_id}/chapter-cards:reset-word-count",
    response_model=SuccessEnvelope[ChapterCardsResetWordCountResult],
    summary="工作区级批量重置章节卡 word_count_min/max(v1.1 G6.2)",
    description=(
        "v1.1 wave 1 session 2(G6.2):把工作区已生成的章节卡 "
        "``word_count_min/max`` 批量重置到当前 ChapterCard model default 或 "
        "用户指定值,治理 wave 6 s2.6d 实证暴露的"
        "'调全局 default 但老工作区卡片不自动更新 → 反复撞 below_min'根因。"
        "默认 ``scope='unapproved'``(保护用户审过字数预算的 approved 卡);"
        "``word_count_min/max`` 留空时回退到 ChapterCard model default(2500/4000)。"
        "**SSOT 写盘**:只改 ``cards/<id>.json``,不动 ChapterCardORM。"
        "**部分失败 = 部分成功**:某章节读写盘失败 swallow + log + 计入 "
        "failed_chapter_ids,不阻断其他章节重置。"
        "错误:CHAPTER_CARDS_BATCH_INVALID 422 = ``word_count_min > "
        "word_count_max`` / ``session_factory`` 未注入 / workspace 不存在。"
    ),
)
async def reset_chapter_cards_word_count(
    workspace_id: str,
    body: ChapterCardsResetWordCountIn,
    card_service: ChapterCardService = Depends(get_chapter_card_service),
) -> SuccessEnvelope[ChapterCardsResetWordCountResult]:
    try:
        result = card_service.reset_word_count_for_workspace(
            workspace_id,
            scope=body.scope,
            word_count_min=body.word_count_min,
            word_count_max=body.word_count_max,
            word_count_policy_version=body.word_count_policy_version,
        )
    except ChapterCardServiceError as exc:
        raise ChapterCardsResetInvalid(str(exc)) from exc
    return SuccessEnvelope(
        data=ChapterCardsResetWordCountResult.model_validate(result)
    )


@router.get(
    "/{workspace_id}/outline",
    response_model=SuccessEnvelope[OutlineOut],
    summary="读取大纲(4 段聚合)",
    description="返 4 段产出聚合;某段未生成时该字段为 null。",
)
async def get_outline(
    workspace_id: str,
    service: OutlineService = Depends(get_outline_service),
) -> SuccessEnvelope[OutlineOut]:
    return SuccessEnvelope(
        data=_build_outline_out_from_workspace(service, workspace_id)
    )


@router.put(
    "/{workspace_id}/outline",
    response_model=SuccessEnvelope[OutlineOut],
    summary="更新大纲(单段或整段 patch)",
    description=(
        "body 4 段全 optional;仅非 None 段写入(原子文件覆盖);"
        "每段写入前 Pydantic 校验 schema。"
    ),
)
async def update_outline(
    workspace_id: str,
    body: OutlineUpdateIn = Body(...),
    service: OutlineService = Depends(get_outline_service),
) -> SuccessEnvelope[OutlineOut]:
    service.write_outline(
        workspace_id,
        world=body.world.model_dump(mode="json") if body.world else None,
        characters=(
            body.characters.model_dump(mode="json") if body.characters else None
        ),
        plot=body.plot.model_dump(mode="json") if body.plot else None,
        volume_plans=(
            body.volume_plans.model_dump(mode="json") if body.volume_plans else None
        ),
        mini_plan=(
            body.mini_plan.model_dump(mode="json") if body.mini_plan else None
        ),
    )
    return SuccessEnvelope(
        data=_build_outline_out_from_workspace(service, workspace_id)
    )
