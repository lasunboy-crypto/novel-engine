"""异步任务持久化 ORM(v1.1 wave 3 session 5 = E3)。

## 为何独立 ``TaskBase`` 而非挂主 ``Base``

``task_registry`` 是**全局进程级单例**(横跨所有工作区,非工作区级),状态落
全局 ``settings.data_dir/_tasks.db``(对齐 ``_system_metrics.db`` P4-B-04 先例 —
跨工作区语义不该塞进任一工作区 ``novel.db``,违反 CLAUDE.md §硬约束 9)。

本表用**独立 metadata**(``TaskBase``)而非主 ``Base``,关键原因:

- alembic ``env.py`` 的 ``target_metadata = Base.metadata`` — 独立 ``TaskBase``
  不在其中,**autogenerate 看不到本表**,故工作区 ``novel.db`` 迁移链(0001-0015)
  完全不受影响,**无需新增 Alembic 迁移**(对齐 CLAUDE.md §实施纪律 Alembic 纪律:
  本任务不触发 ``command.upgrade.*head`` 旧测试断言风险)。
- 本表只在 ``_tasks.db`` 上 ``TaskBase.metadata.create_all``(见
  ``src/services/task_db.py``),不污染工作区 db 也不被工作区 db 反向污染。

## 字段说明

- ``task_id`` 主键(``task_<hex16>``,与 ``new_task_id`` 一致)。
- ``status`` 6 档 CHECK(``pending|running|done|failed|interrupted|cancelled``);
  其中 ``interrupted`` 是 E3 新增 —— 重启后端后进程已死的"未终态"任务由
  lifespan 启动扫描翻成本状态;``cancelled`` 是 v1.1 w4-s6(J1)新增 —— 用户
  主动取消正在跑的任务(对齐 ``schemas/tasks.py`` ``TaskStatus`` Literal)。
  **CHECK 演进**:本表非 alembic 管理(独立 ``TaskBase``,见下),既有
  ``_tasks.db`` 旧 5 档 CHECK 由启动期 ``task_db.reconcile_task_status_check``
  幂等无损重建为 6 档(rename→create_all→copy→drop);新部署 create_all 直接得
  6 档。
- ``workspace_id`` **nullable** —— 持久化层预留,供未来 ``GET /tasks`` 按工作区
  过滤(当前无消费方,故不暴露进 ``TaskInfo`` schema,避免动 OpenAPI / 前端
  gen-types;对齐 YAGNI)。CLI ``workspace_export`` 等跨工作区任务可为 ``None``。
- ``cancel_requested`` **J1 预留位**(candidates §E3 line 706)—— 本 session
  只落列不接逻辑;J1(cancel 跨进程传播)实施时 worker 轮询本列即可,
  ``create_all`` 模式下加列零迁移成本,故现在预留省未来一次改表。
- ``result_json`` / ``error_json`` 用 ``sa.JSON`` 存 ``dict``(对齐
  ``MemoryHealthCheckORM.report_json`` 模式;``_json`` 后缀标示存储形态)。
"""

from __future__ import annotations

from datetime import datetime
from typing import Any

from sqlalchemy import (
    JSON,
    Boolean,
    CheckConstraint,
    DateTime,
    Index,
    Integer,
    String,
)
from sqlalchemy.orm import DeclarativeBase, Mapped, mapped_column


class TaskBase(DeclarativeBase):
    """任务持久化专用 metadata 基类(与主 ``Base`` 物理隔离,详模块 docstring)。"""


# ``TaskRecordORM.status`` 允许值集合(6 档,对齐 schemas/tasks.py ``TaskStatus``
# Literal 字面)。CHECK 约束 SQL 由 ``_task_status_check_sql`` 构造,ORM 与约束
# 单一真相源(对齐 ``PIPELINE_STATE_VALUES`` / ``CHAPTER_STATUS_VALUES`` 模式)。
# ``cancelled`` 是 v1.1 w4-s6(J1)新增 —— 用户经 ``POST /tasks/{id}:cancel`` /
# workspace destroy 级联取消正在跑的任务,后台捕获 ``LLMCancelledError`` 翻本状态
# (区别于 ``failed`` 业务失败 / ``interrupted`` 重启中断,沿用 interrupted 顶层
# 状态档先例 —— 维护人偏好顶层档比嵌套 error 字段更类型安全)。
TASK_STATUS_VALUES: tuple[str, ...] = (
    "pending",
    "running",
    "done",
    "failed",
    "interrupted",
    "cancelled",
)


def _task_status_check_sql() -> str:
    """``TaskRecordORM.status`` CHECK 约束 SQL 谓词(6 档 IN 列表)。"""
    quoted = ", ".join(f"'{v}'" for v in TASK_STATUS_VALUES)
    return f"status IN ({quoted})"


class TaskRecordORM(TaskBase):
    """异步任务持久化记录(E3)。一行 = 一个 ``task_id`` 的最新状态快照。"""

    __tablename__ = "task_records"

    task_id: Mapped[str] = mapped_column(String, primary_key=True)
    type: Mapped[str] = mapped_column(String, nullable=False)
    status: Mapped[str] = mapped_column(String, nullable=False)
    progress: Mapped[int] = mapped_column(Integer, nullable=False, default=0)
    result_json: Mapped[dict[str, Any] | None] = mapped_column(
        JSON, nullable=True
    )
    error_json: Mapped[dict[str, Any] | None] = mapped_column(
        JSON, nullable=True
    )
    workspace_id: Mapped[str | None] = mapped_column(String, nullable=True)
    cancel_requested: Mapped[bool] = mapped_column(
        Boolean, nullable=False, default=False
    )
    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), nullable=False
    )
    updated_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), nullable=False
    )

    __table_args__ = (
        Index("ix_task_records_status", "status"),
        Index("ix_task_records_created_at", "created_at"),
        CheckConstraint(
            _task_status_check_sql(),
            name="ck_task_records_status",
        ),
    )


__all__ = [
    "TASK_STATUS_VALUES",
    "TaskBase",
    "TaskRecordORM",
]
