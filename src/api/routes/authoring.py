"""选择式小说规划；编辑零调用，付费操作复用任务取消和调用账本。"""
from fastapi import APIRouter, BackgroundTasks, Depends

from src.api import task_registry
from src.api.deps import get_outline_service, get_workspace_service
from src.api.errors import ApiError
from src.api.schemas.common import SuccessEnvelope
from src.api.schemas.tasks import TaskInfo
from src.core.llm.cancel import LLMCancelledError
from src.core.models.authoring import AuthoringActionIn, AuthoringSaveIn, AuthoringView
from src.services.authoring_service import AuthoringService
from src.services.post_v8_abc_selection_activity import bind_selection_or_production_scope
from src.services.workspace_service import WorkspaceService

router = APIRouter(prefix="/workspaces", tags=["authoring"])


class AuthoringConflict(ApiError):
    code = "AUTHORING_CONFLICT"
    http_status = 409


def get_authoring_service(workspaces: WorkspaceService = Depends(get_workspace_service)) -> AuthoringService:
    # GET/PUT 不构造模型客户端，不读取凭据。
    return AuthoringService(workspaces, lambda: get_outline_service(workspaces))


@router.get("/{workspace_id}/authoring", response_model=SuccessEnvelope[AuthoringView])
def read(workspace_id: str, service: AuthoringService = Depends(get_authoring_service)):
    return SuccessEnvelope(data=service.read(workspace_id))


@router.put("/{workspace_id}/authoring", response_model=SuccessEnvelope[AuthoringView])
def save(workspace_id: str, body: AuthoringSaveIn, service: AuthoringService = Depends(get_authoring_service)):
    try:
        if task_registry.has_active_tasks(workspace_id):
            raise ValueError("工作区有运行任务，完成后再修改选择")
        return SuccessEnvelope(data=service.save(workspace_id, body))
    except ValueError as exc:
        raise AuthoringConflict(str(exc)) from exc


def _worker(service: AuthoringService, workspace_id: str, body: AuthoringActionIn, task_id: str) -> None:
    if task_registry.claim_pending_for_run(task_id) != "claimed":
        return
    try:
        with task_registry.bind_cancel_event(task_id, workspace_id), bind_selection_or_production_scope(workspace_id, task_id=task_id):
            if task_registry.is_workspace_destroyed(workspace_id):
                raise LLMCancelledError("工作区已销毁")
            result = service.act(workspace_id, body)
        task_registry.update_to_done(task_id, result=result.model_dump(mode="json"))
    except LLMCancelledError:
        task_registry.update_to_cancelled(task_id)
    except Exception as exc:
        task_registry.update_to_failed(task_id, error={"code": "AUTHORING_FAILED",
            "message": str(exc) if isinstance(exc, ValueError) else "规划未完成；已保存阶段保留，请从当前步骤继续"})


@router.post("/{workspace_id}/authoring:act", response_model=SuccessEnvelope[TaskInfo], status_code=202)
def act(workspace_id: str, body: AuthoringActionIn, background: BackgroundTasks,
        service: AuthoringService = Depends(get_authoring_service)):
    try:
        service._current(workspace_id, body.expected_revision)
        if task_registry.has_active_tasks(workspace_id):
            raise ValueError("工作区已有任务，请继续原任务")
        task, created = task_registry.record_pending_if_no_active(task_registry.new_task_id(), type="authoring", workspace_id=workspace_id)
        if not created:
            raise ValueError("规划任务正在执行")
        from src.services.authoring_service import draft_path
        from src.services.outline_service import _atomic_write_json
        _atomic_write_json(draft_path(service.root, workspace_id).parent / "task.json", {"task_id": task.task_id})
        background.add_task(_worker, service, workspace_id, body, task.task_id)
        return SuccessEnvelope(data=task)
    except ValueError as exc:
        raise AuthoringConflict(str(exc)) from exc
