"""任务状态持久化(v1.1 wave 3 session 5 = E3,替换 P1-B-19 进程内 LRU)。

## E3 改造:进程内 LRU → 全局 SQLite

P1-B-19 起本模块是进程内 ``OrderedDict`` LRU,**本机重启 / 跨进程后 task 状态
全丢** → 前端"运行中"状态错位(用户重启后端后,前端仍显示某 task 在跑,实际
已丢失)。E3 把存储后端换成全局 ``settings.data_dir/_tasks.db``(见
``src/services/task_db.py`` + ``src/core/db/task_models.py``),**公开 API 面
(``record_* / get / update_to_*``)签名与返回值不变** → 路由层 / CLI 调用方
零改动(对齐原模块 docstring "P3 切持久化任务表后接口形态不变,客户端无需迁移")。

## 重启恢复语义(interrupted)

进程崩溃 / 重启时,内存里跑着的 BackgroundTask 已死,落库的 ``pending`` /
``running`` 行是僵尸状态。lifespan 启动钩子调 ``mark_running_as_interrupted_on_startup``
把它们翻成 ``interrupted``(对齐 ``pipeline_service.mark_running_as_error_on_startup``
先例,但 task 用**独立 status 档** ``interrupted`` 而非 pipeline 的 ``error`` +
reason —— 维护人拍板:状态机顶层直接区分"中断 vs 失败"比靠嵌套 ``error`` 字段
区分更类型安全)。前端据 ``status="interrupted"`` 显示"已中断"而非"运行中"。

## workspace 销毁标记仍为进程内 set

``_destroyed_workspaces`` 是进程内协调状态(防 in-flight BackgroundTask 在工作区
被 rmtree 后继续写盘),**不持久化**:重启后所有 in-flight 任务已被标 interrupted,
且被销毁的工作区目录已不存在,销毁标记无需跨重启存活。

## 容量

落库后无内存压力,但仍保留 ``_MAX_TASKS`` 软上限(按 ``created_at`` 淘汰最旧),
避免单机长跑无限增长。单用户本地够覆盖会话内多次重试 + 状态复查。
"""

from __future__ import annotations

import secrets
import threading
from collections.abc import Callable, Iterator
from contextlib import contextmanager
from datetime import datetime, timezone
from typing import Any, Literal

import structlog
from sqlalchemy import or_, select, update
from sqlalchemy.orm import Session

from src.api.schemas.tasks import TaskInfo, TaskStatus
from src.core.db.task_models import TaskRecordORM
from src.core.llm.cancel import bound_cancel_event
from src.services.task_db import make_task_session_factory

_log = structlog.get_logger(__name__)

# 落库软上限:超出时只按 created_at 淘汰最旧终态；pending/running 仍绑定
# BackgroundTask 与持久操作租约，绝不能为控容量而删除。
_MAX_TASKS = 256

# 进程内 session 工厂句柄。None → 惰性用 ``make_task_session_factory()`` 读
# ``settings.data_dir``(生产路径);测试经 ``set_session_factory`` 注入 tmp 工厂。
_session_factory: Callable[[], Session] | None = None

# 进程内 workspace 销毁标记(不持久化,详模块 docstring)。
_destroyed_workspaces: set[str] = set()

# 进程内 cancel 信号真源(J1):``key -> threading.Event``(key = task_id 或
# pipeline run_id)。后台 worker 入口 ``bind_cancel_event`` 把对应 Event 绑进
# contextvar 供 LLM client 查;cancel 请求线程经 ``cancel_task`` /
# ``cancel_workspace_tasks`` 调 ``event.set()``。``_cancel_event_ws`` 并行记每个
# key 的 workspace_id,供 ``cancel_workspace_tasks`` 按 workspace 级联(覆盖
# pipeline 等不进 task_records 的 in-flight 任务)。**不持久化**(与
# ``_destroyed_workspaces`` 同模式):Event 是活进程内 in-flight worker 的协调原语,
# 重启后 in-flight 任务已被标 interrupted,无跨重启意义。``_cancel_lock`` 护两个
# dict 读写(cancel 请求线程 vs worker 线程并发访问)。
_cancel_events: dict[str, threading.Event] = {}
_cancel_event_ws: dict[str, str | None] = {}
_cancel_lock = threading.Lock()

# 本地单进程产品入口的“查 active + 登记 pending”原子区。防两个浏览器提交在
# SQLite 两个 session 间同时通过检查，创建重复付费 Worker。
_registration_lock = threading.Lock()


def set_session_factory(factory: Callable[[], Session]) -> None:
    """注入 session 工厂(测试隔离用;生产路径走惰性默认工厂)。"""
    global _session_factory
    _session_factory = factory


def reset_session_factory() -> None:
    """清除注入的 session 工厂,恢复惰性默认(测试 teardown 用)。"""
    global _session_factory
    _session_factory = None


def _factory() -> Callable[[], Session]:
    if _session_factory is not None:
        return _session_factory
    # 惰性构造:读 settings.data_dir 当前值(对齐 system_db 调用时解析模式)。
    return make_task_session_factory()


@contextmanager
def _session() -> Iterator[Session]:
    session = _factory()()
    try:
        yield session
    finally:
        session.close()


def new_task_id() -> str:
    """生成 ``task_<random16hex>`` 格式的任务 ID(与 P1-B-19 一致)。"""
    return f"task_{secrets.token_hex(8)}"


def _now() -> datetime:
    return datetime.now(timezone.utc)


def _ensure_utc(dt: datetime) -> datetime:
    """SQLite 取回的 ``DateTime`` 可能丢 tzinfo;naive 视为 UTC 补回 tz。"""
    if dt.tzinfo is None:
        return dt.replace(tzinfo=timezone.utc)
    return dt


def _to_info(orm: TaskRecordORM) -> TaskInfo:
    """ORM 行 → ``TaskInfo``(``workspace_id`` / ``cancel_requested`` 持久化层
    字段不进 schema,详 task_models.py docstring)。"""
    return TaskInfo(
        task_id=orm.task_id,
        type=orm.type,
        status=orm.status,  # type: ignore[arg-type]
        progress=orm.progress,
        result=orm.result_json,
        error=orm.error_json,
        created_at=_ensure_utc(orm.created_at),
        updated_at=_ensure_utc(orm.updated_at),
    )


def _evict_if_needed(session: Session) -> None:
    """超上限时只删最旧终态；活任务不足以腾位时允许暂时超限。"""
    count = session.query(TaskRecordORM).count()
    if count <= _MAX_TASKS:
        return
    overflow = count - _MAX_TASKS
    oldest = list(
        session.scalars(
            select(TaskRecordORM)
            .where(
                TaskRecordORM.status.in_(("done", "failed", "interrupted", "cancelled"))
            )
            .order_by(TaskRecordORM.created_at.asc())
            .limit(overflow)
        )
    )
    for row in oldest:
        session.delete(row)


def _upsert(
    session: Session,
    task_id: str,
    *,
    type: str,
    status: TaskStatus,
    progress: int,
    result: dict[str, Any] | None,
    error: dict[str, Any] | None,
    workspace_id: str | None,
    created_at: datetime,
    updated_at: datetime,
) -> TaskRecordORM:
    orm = session.get(TaskRecordORM, task_id)
    if orm is None:
        orm = TaskRecordORM(
            task_id=task_id,
            type=type,
            status=status,
            progress=progress,
            result_json=result,
            error_json=error,
            workspace_id=workspace_id,
            cancel_requested=False,
            created_at=created_at,
            updated_at=updated_at,
        )
        session.add(orm)
    else:
        orm.type = type
        orm.status = status
        orm.progress = progress
        orm.result_json = result
        orm.error_json = error
        if workspace_id is not None:
            orm.workspace_id = workspace_id
        orm.updated_at = updated_at
    return orm


def record_done(
    task_id: str,
    *,
    type: str,
    result: dict[str, Any],
    workspace_id: str | None = None,
) -> TaskInfo:
    """登记一条 ``status="done"`` 任务(同步执行入口,一开始就 done)。"""
    now = _now()
    with _session() as session:
        orm = _upsert(
            session,
            task_id,
            type=type,
            status="done",
            progress=100,
            result=result,
            error=None,
            workspace_id=workspace_id,
            created_at=now,
            updated_at=now,
        )
        _evict_if_needed(session)
        session.commit()
        return _to_info(orm)


def record_pending(
    task_id: str,
    *,
    type: str,
    workspace_id: str | None = None,
) -> TaskInfo:
    """登记一条 ``status="pending"`` 任务(异步入口占位,后台跑完翻 done/failed)。"""
    now = _now()
    with _session() as session:
        orm = _upsert(
            session,
            task_id,
            type=type,
            status="pending",
            progress=0,
            result=None,
            error=None,
            workspace_id=workspace_id,
            created_at=now,
            updated_at=now,
        )
        _evict_if_needed(session)
        session.commit()
        return _to_info(orm)


def record_pending_if_no_active(
    task_id: str,
    *,
    type: str,
    workspace_id: str,
) -> tuple[TaskInfo, bool]:
    """同 workspace + type 没有 active task 时才登记 pending。

    返回 ``(task, created)``：``created=False`` 时 ``task`` 是已存在的
    pending/running 任务，调用方必须拒绝新 Worker。锁覆盖查询和写入，符合本地
    单 Uvicorn 进程产品部署的并发模型；持久层 workspace_id 同时保留审计绑定。
    """
    now = _now()
    with _registration_lock, _session() as session:
        stmt = (
            select(TaskRecordORM)
            .where(
                TaskRecordORM.workspace_id == workspace_id,
                TaskRecordORM.type == type,
                TaskRecordORM.status.in_(("pending", "running")),
            )
            .order_by(TaskRecordORM.created_at.desc())
            .limit(1)
        )
        active = session.scalars(stmt).first()
        if active is not None:
            return _to_info(active), False
        orm = _upsert(
            session,
            task_id,
            type=type,
            status="pending",
            progress=0,
            result=None,
            error=None,
            workspace_id=workspace_id,
            created_at=now,
            updated_at=now,
        )
        _evict_if_needed(session)
        session.commit()
        return _to_info(orm), True


def claim_pending_for_run(
    task_id: str,
) -> Literal["claimed", "cancelled", "rejected"]:
    """原子把一个 pending BackgroundTask 交给唯一 Worker。

    Starlette callback 若因重入/重复调度被执行两次，只有第一个
    ``pending -> running`` CAS 成功的 Worker 能进入任务体。已预先
    cancel 的 pending 任务在同一事务内翻 ``cancelled``；过期
    callback 返回 ``rejected`` 并静默退出，不得更改 task/lease。
    """

    with _registration_lock, _session() as session:
        row = session.get(TaskRecordORM, task_id)
        if row is None or row.status != "pending":
            return "rejected"
        if row.cancel_requested:
            row.status = "cancelled"
            row.error_json = {
                "code": "TASK_CANCELLED",
                "reason": "user_requested_before_worker_start",
            }
            row.updated_at = _now()
            session.commit()
            return "cancelled"
        result = session.execute(
            update(TaskRecordORM)
            .where(
                TaskRecordORM.task_id == task_id,
                TaskRecordORM.status == "pending",
                TaskRecordORM.cancel_requested.is_(False),
            )
            .values(status="running", updated_at=_now())
        )
        session.commit()
        return "claimed" if result.rowcount == 1 else "rejected"


def is_cancel_requested(task_id: str) -> bool:
    """读取持久 cancel 位，闭合 claim 成功到 Event 绑定之间的窗口。"""

    with _session() as session:
        row = session.get(TaskRecordORM, task_id)
        return bool(row is not None and row.cancel_requested)


def is_user_cancelled_task_checkpoint(
    task_id: str,
    workspace_id: str,
    task_type: str,
) -> bool:
    """严格证明一个旧任务是同工作区、同类型的用户主动取消终态。

    仅供冷启动章节断点分类。普通 ``failed`` / ``interrupted``、跨工作区、
    类型错位、没有持久 cancel 位，或缺少标准取消错误码时一律返回 False。
    """

    try:
        with _session() as session:
            row = session.get(TaskRecordORM, task_id)
            error = row.error_json if row is not None else None
            return bool(
                row is not None
                and row.workspace_id == workspace_id
                and row.type == task_type
                and row.status == "cancelled"
                and row.cancel_requested
                and isinstance(error, dict)
                and error.get("code") == "TASK_CANCELLED"
                and error.get("reason") == "user_requested"
            )
    except Exception:  # noqa: BLE001 — 恢复证据不可读即保守拒绝
        _log.exception(
            "cancelled_task_checkpoint_read_failed",
            task_id=task_id,
            workspace_id=workspace_id,
            task_type=task_type,
        )
        return False


def list_done_tasks(
    *,
    type: str,
    workspace_id: str,
    include_unbound: bool = False,
) -> list[TaskInfo]:
    """列出同 workspace/type 的 done 任务，可纳入旧版未绑定 workspace 行。

    ``include_unbound`` 只用于恢复旧任务：新任务均应写 ``workspace_id``。
    结果按最新优先，调用方再对业务 result 做精确匹配。
    """
    with _session() as session:
        workspace_filter = TaskRecordORM.workspace_id == workspace_id
        if include_unbound:
            workspace_filter = or_(
                workspace_filter,
                TaskRecordORM.workspace_id.is_(None),
            )
        stmt = (
            select(TaskRecordORM)
            .where(
                workspace_filter,
                TaskRecordORM.type == type,
                TaskRecordORM.status == "done",
            )
            .order_by(TaskRecordORM.updated_at.desc())
        )
        return [_to_info(orm) for orm in session.scalars(stmt)]


def record_failed(
    task_id: str,
    *,
    type: str,
    error: dict[str, Any],
    workspace_id: str | None = None,
) -> TaskInfo:
    """登记一条 ``status="failed"`` 任务(业务可恢复但本次失败语义占位)。"""
    now = _now()
    with _session() as session:
        orm = _upsert(
            session,
            task_id,
            type=type,
            status="failed",
            progress=0,
            result=None,
            error=error,
            workspace_id=workspace_id,
            created_at=now,
            updated_at=now,
        )
        _evict_if_needed(session)
        session.commit()
        return _to_info(orm)


def update_to_done(
    task_id: str,
    *,
    result: dict[str, Any],
) -> TaskInfo | None:
    """把已存在的 pending / running 任务翻 ``status="done"``。

    保留原 ``type`` / ``created_at`` / ``workspace_id``,只更新状态相关字段。
    task_id 不存在 → ``None``(已被淘汰 / 从未登记,静默放弃)。
    """
    with _session() as session:
        orm = session.get(TaskRecordORM, task_id)
        if orm is None:
            return None
        orm.status = "done"
        orm.progress = 100
        orm.result_json = result
        orm.error_json = None
        orm.updated_at = _now()
        session.commit()
        return _to_info(orm)


def update_to_failed(
    task_id: str,
    *,
    error: dict[str, Any],
) -> TaskInfo | None:
    """把已存在的 pending / running 任务翻 ``status="failed"``。

    task_id 不存在 → ``None``。
    """
    with _session() as session:
        orm = session.get(TaskRecordORM, task_id)
        if orm is None:
            return None
        orm.status = "failed"
        orm.progress = 0
        orm.result_json = None
        orm.error_json = error
        orm.updated_at = _now()
        session.commit()
        return _to_info(orm)


def update_to_cancelled(
    task_id: str,
    *,
    error: dict[str, Any] | None = None,
) -> TaskInfo | None:
    """把已存在的 pending / running 任务翻 ``status="cancelled"``(J1)。

    用户主动取消(``POST /tasks/{id}:cancel`` / workspace destroy 级联)后,后台
    worker 捕获 ``LLMCancelledError`` 调本方法。``error`` 缺省填
    ``{"code":"TASK_CANCELLED","reason":"user_requested"}`` 让前端区分"已取消"
    与业务 ``failed``;caller 可覆盖(典型 workspace destroy 级联填
    ``reason="workspace_destroyed"``)。task_id 不存在 → ``None``。
    """
    with _session() as session:
        orm = session.get(TaskRecordORM, task_id)
        if orm is None:
            return None
        orm.status = "cancelled"
        orm.result_json = None
        orm.error_json = error or {
            "code": "TASK_CANCELLED",
            "reason": "user_requested",
        }
        orm.updated_at = _now()
        session.commit()
        return _to_info(orm)


def get(task_id: str) -> TaskInfo | None:
    """读取任务;命中 → ``TaskInfo``;未命中 → ``None``。"""
    with _session() as session:
        orm = session.get(TaskRecordORM, task_id)
        if orm is None:
            return None
        return _to_info(orm)


def mark_running_as_interrupted_on_startup() -> int:
    """lifespan 启动钩子:把残留 ``pending`` / ``running`` 行翻 ``interrupted``。

    进程重启后 in-flight BackgroundTask 已死,这些"未终态"行是僵尸状态;翻
    ``interrupted`` + ``error={"code":"TASK_INTERRUPTED","reason":
    "engine_restarted_mid_run"}`` 让前端 ``GET /tasks/{id}`` 拿到清晰中断态
    (区别于业务失败 ``failed``)。返回翻新的行数(供 lifespan log)。
    """
    count = 0
    with _session() as session:
        stmt = select(TaskRecordORM).where(
            TaskRecordORM.status.in_(("pending", "running"))
        )
        rows = list(session.scalars(stmt))
        for row in rows:
            row.status = "interrupted"
            row.error_json = {
                "code": "TASK_INTERRUPTED",
                "reason": "engine_restarted_mid_run",
            }
            row.updated_at = _now()
            count += 1
        if rows:
            session.commit()
    if count > 0:
        _log.info("task_engine_restarted_marked", count=count)
    return count


def has_active_tasks(
    workspace_id: str, *, task_types: tuple[str, ...] | None = None
) -> bool:
    """查某 workspace 是否有 ``pending`` / ``running`` 的持久层任务行。

    v2 w1-s2 供 ``ChapterRewindService.reset_stale`` 对账防误杀:章停在
    非终态但该 workspace 有活跃任务(chapter:write 等 BackgroundTask 正在
    写)时跳过自愈。**注意 pipeline execute 循环不进 task_records**(不走
    ``bind_cancel_event`` / ``record_pending``)— 它由 reset_stale 内的
    ``pipeline_runs.state='running'`` 判定另行覆盖,两层对账缺一不可。

    ``task_types`` 可收窄任务种类，供 Run 等待提交后的自动首卡；未传保持原语义。
    查询异常(_tasks.db 不可读等)→ **返 True 保守跳过**(自愈是 best-effort,
    宁可漏扫不可误杀)+ structlog error。
    """
    try:
        with _session() as session:
            stmt = (
                select(TaskRecordORM)
                .where(
                    TaskRecordORM.workspace_id == workspace_id,
                    TaskRecordORM.status.in_(("pending", "running")),
                )
                .limit(1)
            )
            if task_types is not None:
                stmt = stmt.where(TaskRecordORM.type.in_(task_types))
            return session.scalars(stmt).first() is not None
    except Exception as exc:  # noqa: BLE001
        _log.error(
            "task_active_check_failed",
            workspace_id=workspace_id,
            error_type=type(exc).__name__,
            error_message=str(exc),
        )
        return True


def clear() -> None:
    """清空 task 表 + 进程内销毁标记 + cancel Event 真源(测试用;生产路径不调)。"""
    with _session() as session:
        session.query(TaskRecordORM).delete()
        session.commit()
    _destroyed_workspaces.clear()
    with _cancel_lock:
        _cancel_events.clear()
        _cancel_event_ws.clear()


# ---------------------------------------------------------------------------
# J1:task-level cancel signal(threading.Event 真源 + worker 入口绑定)
# ---------------------------------------------------------------------------


def register_cancel_event(
    task_id: str, workspace_id: str | None = None
) -> threading.Event:
    """为 task 登记一个 cancel ``Event`` 并返回(后台 worker 入口调用)。

    ``workspace_id`` 记入 ``_cancel_event_ws`` 供 ``cancel_workspace_tasks`` 按
    workspace 级联(pipeline 等不进 task_records 的任务靠此被级联取消)。

    幂等:同 task_id 重复登记返回同一 Event(防同一任务多次绑定生成多个 Event
    导致 cancel 落空)。
    """
    with _cancel_lock:
        event = _cancel_events.get(task_id)
        if event is None:
            event = threading.Event()
            _cancel_events[task_id] = event
        _cancel_event_ws[task_id] = workspace_id
        return event


def unregister_cancel_event(task_id: str) -> None:
    """清除 task 的 cancel Event(worker 任务结束时调用;幂等)。"""
    with _cancel_lock:
        _cancel_events.pop(task_id, None)
        _cancel_event_ws.pop(task_id, None)


def cancel_task(task_id: str) -> bool:
    """请求取消正在跑的 task(``POST /tasks/{id}:cancel`` 入口)。

    ① 置持久层 ``cancel_requested=True``(E3 预留列,供可观测 / 跨进程复查);
    ② 若进程内有该 task 的 cancel Event(in-flight worker 在跑)→ ``set()`` 它,
    worker 的 LLM client 下次 ``raise_if_cancelled`` 即中断。

    Returns:
        ``True`` = 进程内有 in-flight Event 且已 set(取消信号已送达 worker);
        ``False`` = 无 in-flight Event(任务已终态 / 未登记 / 跨重启已死)——
        此时仅置了 ``cancel_requested`` 列(若 task 行存在),不报错。
    """
    # 与 pending->running claim 共用同一把锁，消除 claim 先读、cancel 提交、
    # claim CAS rejected 后 callback 静默退出并遗留章节租约的窗口。
    with _registration_lock, _session() as session:
        orm = session.get(TaskRecordORM, task_id)
        if orm is not None and orm.status in ("pending", "running"):
            orm.cancel_requested = True
            orm.updated_at = _now()
            session.commit()
    with _cancel_lock:
        event = _cancel_events.get(task_id)
    if event is None:
        return False
    event.set()
    _log.info("task_cancel_requested", task_id=task_id)
    return True


def cancel_workspace_tasks(workspace_id: str) -> int:
    """级联取消某 workspace 所有 in-flight 任务(workspace destroy/purge/archive)。

    与 ``mark_workspace_destroyed`` 协同:destroy 先 mark(防新任务入口),再调本
    函数 set 所有正在跑的 in-flight 任务的 cancel Event → 让它们的 LLM 调用尽早
    中断(省 token)而非靠 writer 层 ``paths.root.exists`` 防御被动失败。

    ① 遍历 in-flight ``_cancel_event_ws`` 取 workspace 匹配的 key,``set()`` 其
    Event(覆盖 pipeline 等不进 task_records 的任务);② 同步把持久层 ``pending`` /
    ``running`` 且 workspace 匹配的 task_records 行置 ``cancel_requested``(可观测 /
    跨进程复查)。

    Returns:
        实际 set 的 in-flight Event 数量。
    """
    # 与单任务 cancel 一样，先和 pending->running claim 共锁持久化取消位。
    # 否则 workspace destroy 可插入 claim 的读取/CAS 中间，使 callback 被拒
    # 却遗留尚未释放的章节操作租约。
    with _registration_lock, _session() as session:
        rows = list(
            session.scalars(
                select(TaskRecordORM).where(
                    TaskRecordORM.workspace_id == workspace_id,
                    TaskRecordORM.status.in_(("pending", "running")),
                )
            )
        )
        for r in rows:
            r.cancel_requested = True
            r.updated_at = _now()
        if rows:
            session.commit()
    count = 0
    with _cancel_lock:
        for key, ws in list(_cancel_event_ws.items()):
            if ws == workspace_id:
                event = _cancel_events.get(key)
                if event is not None:
                    event.set()
                    count += 1
    if count > 0:
        _log.info("workspace_tasks_cancelled", workspace_id=workspace_id, count=count)
    return count


@contextmanager
def bind_cancel_event(task_id: str, workspace_id: str | None = None) -> Iterator[None]:
    """后台 worker 入口:登记 + 绑定 cancel Event 进 contextvar,退出清理。

    ``with bind_cancel_event(task_id, workspace_id):`` 包住后台任务体 —— 块内深层
    LLM client ``raise_if_cancelled()`` 能读到本 task 的 cancel Event。``workspace_id``
    供 ``cancel_workspace_tasks`` 级联。退出(正常 / 异常 / ``LLMCancelledError``)
    ``finally`` 经 ``bound_cancel_event`` reset contextvar(防 anyio threadpool
    线程复用泄漏给下一个任务)+ ``unregister_cancel_event`` 清 dict 真源。
    """
    event = register_cancel_event(task_id, workspace_id)
    try:
        with bound_cancel_event(event):
            yield
    finally:
        unregister_cancel_event(task_id)


def mark_workspace_destroyed(workspace_id: str) -> None:
    """标记 workspace 已被销毁(进程内,详模块 docstring)。幂等。"""
    _destroyed_workspaces.add(workspace_id)


def is_workspace_destroyed(workspace_id: str) -> bool:
    """检查 workspace 是否被销毁(BackgroundTask 入口防御调用)。"""
    return workspace_id in _destroyed_workspaces


def clear_workspace_destroyed_mark(workspace_id: str) -> None:
    """清除销毁标记(workspace create / restore 调用)。幂等(discard 语义)。"""
    _destroyed_workspaces.discard(workspace_id)


__all__ = [
    "TaskStatus",
    "bind_cancel_event",
    "cancel_task",
    "cancel_workspace_tasks",
    "claim_pending_for_run",
    "clear",
    "clear_workspace_destroyed_mark",
    "get",
    "is_cancel_requested",
    "is_user_cancelled_task_checkpoint",
    "is_workspace_destroyed",
    "mark_running_as_interrupted_on_startup",
    "mark_workspace_destroyed",
    "new_task_id",
    "record_done",
    "record_failed",
    "record_pending",
    "register_cancel_event",
    "reset_session_factory",
    "set_session_factory",
    "unregister_cancel_event",
    "update_to_cancelled",
    "update_to_done",
    "update_to_failed",
]
