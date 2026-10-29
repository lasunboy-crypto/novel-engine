"""任务状态查询路由(P1-B-19;v1.1 wave 3 session 5 = E3 持久化)。

对齐 API_CONTRACT §6 异步任务模式。**E3 起任务状态落全局 ``_tasks.db`` SQLite**
(见 ``src/api/task_registry.py``,替换 P1-B-19 进程内 LRU);本端点是
``:write`` / ``:rewrite`` / ``:revise`` 等任务跑完后客户端复查状态的入口。

语义(E3 起)
============

- 命中 → 200 + ``TaskInfo``(``done`` / ``failed`` / ``interrupted`` / 进行中的
  ``pending`` / ``running``)
- 重启后:进程崩溃 / 重启前残留的 ``pending`` / ``running`` 行已被 lifespan
  启动扫描翻成 ``interrupted``(``error.code="TASK_INTERRUPTED"``),客户端据此
  显示"已中断"而非永久"运行中"(E3 根治"重启后前端状态错位")
- 未命中 → ``TASK_NOT_FOUND (404)``:任务从未登记,或超 ``_MAX_TASKS`` 软上限
  被淘汰(E3 起任务跨重启持久,404 不再因重启产生)

v1.1 w4-s6(J1)起实现 ``POST /tasks/{id}:cancel``(取消正在跑的任务,见下)。
本阶段仍不实现:``GET /tasks`` 列表(未来按 ``workspace_id`` 过滤,ORM 已预留列)。
"""

from __future__ import annotations

from fastapi import APIRouter

from src.api import task_registry
from src.api.errors import TaskNotFound
from src.api.schemas.common import SuccessEnvelope
from src.api.schemas.tasks import TaskInfo

router = APIRouter(prefix="/tasks", tags=["tasks"])


@router.get(
    "/{task_id}",
    response_model=SuccessEnvelope[TaskInfo],
    summary="查询任务状态",
    description=(
        "读取任务最新状态(E3 起落全局 _tasks.db,跨重启持久)。状态机:pending "
        "/ running / done / failed / interrupted(后者为后端重启中断的未终态任务)。"
        "未命中(任务从未登记或超容量上限被淘汰)返回 TASK_NOT_FOUND(404)。"
    ),
)
async def get_task(task_id: str) -> SuccessEnvelope[TaskInfo]:
    info = task_registry.get(task_id)
    if info is None:
        raise TaskNotFound(f"任务不存在或已过期: {task_id}")
    return SuccessEnvelope(data=info)


@router.post(
    "/{task_id}:cancel",
    response_model=SuccessEnvelope[TaskInfo],
    summary="取消正在跑的任务(J1)",
    description=(
        "请求取消一个 pending / running 任务(v1.1 w4-s6 = J1)。**尽力中断语义**:"
        "置 ``cancel_requested`` + 向 in-flight worker 发取消信号 → worker 的 LLM "
        "client 在下一个 SSE chunk 间(openai_compatible 真流式)/ 下一次调用前 "
        "(claude / ollama 单次阻塞)中断,捕获后翻 ``status=cancelled``。**非瞬时**:"
        "调用立即返回当前 ``TaskInfo``(此刻通常仍是 running),前端轮询 / 订阅 "
        "``task.{id}.progress`` 的 cancelled 事件确认终态。已终态(done / failed / "
        "interrupted / cancelled)任务幂等放过(返回当前状态,不报错)。未命中(从未"
        "登记 / 超容量淘汰)返回 ``TASK_NOT_FOUND``(404)。"
    ),
)
async def cancel_task(task_id: str) -> SuccessEnvelope[TaskInfo]:
    info = task_registry.get(task_id)
    if info is None:
        raise TaskNotFound(f"任务不存在或已过期: {task_id}")
    # 仅对未终态任务发取消信号;已终态幂等放过(返回当前状态)。
    if info.status in ("pending", "running"):
        task_registry.cancel_task(task_id)
        info = task_registry.get(task_id) or info
    return SuccessEnvelope(data=info)
