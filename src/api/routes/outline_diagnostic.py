"""大纲只读对照入口；付费任务沿用持久化任务及取消机制。"""
from fastapi import APIRouter, BackgroundTasks, Depends

from src.api import task_registry
from src.api.errors import ApiError
from src.api.deps import get_workspace_service
from src.api.schemas.common import SuccessEnvelope
from src.api.schemas.outline_diagnostic import (
    DiagnosticOut, DiagnosticStartIn, FailedCandidateRepairPrepareIn,
    RepairCommitIn, RepairOut, RepairPrepareIn, RepairStartIn,
)
from src.api.schemas.tasks import TaskInfo
from src.config import settings
from src.core.llm.cancel import LLMCancelledError
from src.services.outline_review_diagnostic_service import DiagnosticError, OutlineReviewDiagnosticService
from src.services.outline_targeted_repair_service import OutlineTargetedRepairService
from src.services.outline_service import OutlineService
from src.services.workspace_service import WorkspaceService

class DiagnosticApiError(ApiError):
    code = "OUTLINE_DIAGNOSTIC_CONFLICT"
    http_status = 409


router = APIRouter(prefix="/workspaces", tags=["outline"])


def get_repair_service(workspaces: WorkspaceService = Depends(get_workspace_service)) -> OutlineTargetedRepairService:
    return OutlineTargetedRepairService(workspaces, settings.prompts_dir)


@router.get("/{workspace_id}/outline-failed-candidates/{candidate_id}")
def read_failed_candidate(workspace_id: str, candidate_id: str,
                          workspaces: WorkspaceService = Depends(get_workspace_service)):
    try:
        service = OutlineService(workspace_service=workspaces)
        return SuccessEnvelope(data={"candidate_id": candidate_id,
                                     **service.read_failed_volume_candidate(workspace_id, candidate_id)})
    except ValueError as exc:
        raise DiagnosticApiError(str(exc)) from exc


@router.post("/{workspace_id}/outline-failed-candidates/{candidate_id}:prepare-repair",
             response_model=SuccessEnvelope[RepairOut])
def prepare_failed_candidate_repair(
    workspace_id: str, candidate_id: str, body: FailedCandidateRepairPrepareIn,
    service: OutlineTargetedRepairService = Depends(get_repair_service),
):
    try:
        return SuccessEnvelope(data=RepairOut.model_validate(service.prepare_failed_candidate(
            workspace_id, candidate_id=candidate_id,
            conflict_indexes=body.conflict_indexes,
            allowed_paths=body.allowed_paths, direction=body.direction,
            parent_repair_id=body.parent_repair_id, parent_review_id=body.parent_review_id,
        )))
    except ValueError as exc:
        raise DiagnosticApiError(str(exc)) from exc


def get_diagnostic_service(workspaces: WorkspaceService = Depends(get_workspace_service)) -> OutlineReviewDiagnosticService:
    return OutlineReviewDiagnosticService(workspaces, settings.prompts_dir)


def _worker(service, workspace_id: str, diagnostic_id: str, request_hash: str, task_id: str) -> None:
    if task_registry.claim_pending_for_run(task_id) != "claimed":
        return
    try:
        with task_registry.bind_cancel_event(task_id, workspace_id):
            if task_registry.is_workspace_destroyed(workspace_id):
                raise LLMCancelledError("工作区已销毁")
            result = service.run(workspace_id, diagnostic_id, request_hash, task_id)
        task_registry.update_to_done(task_id, result=result.model_dump(mode="json"))
    except LLMCancelledError:
        task_registry.update_to_cancelled(task_id)
    except Exception as exc:
        task_registry.update_to_failed(task_id, error={"code": "OUTLINE_DIAGNOSTIC_FAILED",
            "message": str(exc) if isinstance(exc, DiagnosticError) else "诊断未完成；已保存的请求和结果保留，不自动重放"})


@router.post("/{workspace_id}/outline-diagnostics:prepare", response_model=SuccessEnvelope[DiagnosticOut])
def prepare(workspace_id: str, service: OutlineReviewDiagnosticService = Depends(get_diagnostic_service)):
    try:
        return SuccessEnvelope(data=service.prepare(workspace_id))
    except ValueError as exc:
        raise DiagnosticApiError(str(exc)) from exc


@router.post("/{workspace_id}/outline-diagnostics:prepare-max-hard", response_model=SuccessEnvelope[DiagnosticOut])
def prepare_max_hard(workspace_id: str, service: OutlineReviewDiagnosticService = Depends(get_diagnostic_service)):
    try:
        return SuccessEnvelope(data=service.prepare(workspace_id, mode="max_hard"))
    except ValueError as exc:
        raise DiagnosticApiError(str(exc)) from exc


@router.post("/{workspace_id}/outline-diagnostics:prepare-split-hard", response_model=SuccessEnvelope[DiagnosticOut])
def prepare_split_hard(workspace_id: str, service: OutlineReviewDiagnosticService = Depends(get_diagnostic_service)):
    try:
        return SuccessEnvelope(data=service.prepare(workspace_id, mode="split_hard"))
    except ValueError as exc:
        raise DiagnosticApiError(str(exc)) from exc


@router.get("/{workspace_id}/outline-diagnostics/{diagnostic_id}", response_model=SuccessEnvelope[DiagnosticOut])
def read(workspace_id: str, diagnostic_id: str, service: OutlineReviewDiagnosticService = Depends(get_diagnostic_service)):
    try:
        return SuccessEnvelope(data=service.read(workspace_id, diagnostic_id))
    except ValueError as exc:
        raise DiagnosticApiError(str(exc)) from exc


@router.post("/{workspace_id}/outline-diagnostics/{diagnostic_id}:start", response_model=SuccessEnvelope[TaskInfo], status_code=202)
def start(workspace_id: str, diagnostic_id: str, body: DiagnosticStartIn, background_tasks: BackgroundTasks,
          service: OutlineReviewDiagnosticService = Depends(get_diagnostic_service)):
    try:
        service.preflight(workspace_id, diagnostic_id, body.request_hash)
    except ValueError as exc:
        raise DiagnosticApiError(str(exc)) from exc
    task, created = task_registry.record_pending_if_no_active(task_registry.new_task_id(), type="outline_diagnostic", workspace_id=workspace_id)
    if not created:
        raise DiagnosticApiError("已有诊断任务运行中")
    background_tasks.add_task(_worker, service, workspace_id, diagnostic_id, body.request_hash, task.task_id)
    return SuccessEnvelope(data=task)


def _repair_worker(service: OutlineTargetedRepairService, workspace_id: str, repair_id: str,
                   request_hash: str, task_id: str) -> None:
    if task_registry.claim_pending_for_run(task_id) != "claimed":
        return
    try:
        with task_registry.bind_cancel_event(task_id, workspace_id):
            if task_registry.is_workspace_destroyed(workspace_id):
                raise LLMCancelledError("工作区已销毁")
            result = service.run_patch(workspace_id, repair_id, request_hash, task_id)
        task_registry.update_to_done(task_id, result=result)
    except LLMCancelledError:
        task_registry.update_to_cancelled(task_id)
    except Exception as exc:
        task_registry.update_to_failed(task_id, error={"code": "OUTLINE_REPAIR_FAILED",
            "message": str(exc) if isinstance(exc, ValueError) else "修复请求未完成，已保存证据；请先核对账本"})


@router.post("/{workspace_id}/outline-repairs:prepare", response_model=SuccessEnvelope[RepairOut])
def prepare_repair(workspace_id: str, body: RepairPrepareIn,
                   service: OutlineTargetedRepairService = Depends(get_repair_service)):
    try:
        return SuccessEnvelope(data=RepairOut.model_validate(service.prepare(workspace_id,
            diagnostic_id=body.diagnostic_id, conflict_index=body.conflict_index,
            allowed_paths=body.allowed_paths, direction=body.direction)))
    except ValueError as exc:
        raise DiagnosticApiError(str(exc)) from exc


@router.get("/{workspace_id}/outline-repairs/{repair_id}", response_model=SuccessEnvelope[RepairOut])
def read_repair(workspace_id: str, repair_id: str,
                service: OutlineTargetedRepairService = Depends(get_repair_service)):
    try:
        return SuccessEnvelope(data=RepairOut.model_validate(service.read(workspace_id, repair_id)))
    except ValueError as exc:
        raise DiagnosticApiError(str(exc)) from exc


@router.post("/{workspace_id}/outline-repairs/{repair_id}:start", response_model=SuccessEnvelope[TaskInfo], status_code=202)
def start_repair(workspace_id: str, repair_id: str, body: RepairStartIn, background_tasks: BackgroundTasks,
                 service: OutlineTargetedRepairService = Depends(get_repair_service)):
    try:
        service.preflight_patch(workspace_id, repair_id, body.request_hash)
    except ValueError as exc:
        raise DiagnosticApiError(str(exc)) from exc
    task, created = task_registry.record_pending_if_no_active(task_registry.new_task_id(), type="outline_repair", workspace_id=workspace_id)
    if not created:
        raise DiagnosticApiError("本工作区已有任务运行中")
    background_tasks.add_task(_repair_worker, service, workspace_id, repair_id, body.request_hash, task.task_id)
    return SuccessEnvelope(data=task)


@router.post("/{workspace_id}/outline-repairs/{repair_id}:prepare-review", response_model=SuccessEnvelope[DiagnosticOut])
def prepare_repair_review(workspace_id: str, repair_id: str,
                          service: OutlineReviewDiagnosticService = Depends(get_diagnostic_service)):
    try:
        return SuccessEnvelope(data=service.prepare_repair_review(workspace_id, repair_id))
    except ValueError as exc:
        raise DiagnosticApiError(str(exc)) from exc


@router.post("/{workspace_id}/outline-repairs/{repair_id}:commit", response_model=SuccessEnvelope[RepairOut])
def commit_repair(workspace_id: str, repair_id: str, body: RepairCommitIn,
                  service: OutlineTargetedRepairService = Depends(get_repair_service)):
    try:
        return SuccessEnvelope(data=RepairOut.model_validate(service.commit(workspace_id, repair_id, body.review_id)))
    except ValueError as exc:
        raise DiagnosticApiError(str(exc)) from exc
