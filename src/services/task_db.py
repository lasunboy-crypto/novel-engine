"""任务持久化 SQLite 工厂(v1.1 wave 3 session 5 = E3)。

``task_records`` 是**全局进程级**任务状态表,放在 ``settings.data_dir/_tasks.db``
单文件 db。设计与 ``system_db.make_system_session_factory``(P4-B-04)同构,
但用**独立文件 + 独立 metadata**(``TaskBase``),原因:

- **独立文件而非复用 ``_system_metrics.db``**:metrics 是低频日聚合写,task
  状态是高频翻转写(pending → done/failed 秒级),独立文件避免 SQLite 单写锁
  竞争(维护人拍板,2026-05-29)。
- **独立 metadata 而非主 ``Base``**:不进 alembic ``target_metadata``,故不触发
  工作区 ``novel.db`` 迁移链变更(详 ``src/core/db/task_models.py`` 模块 docstring)。

测试隔离
--------

``make_task_session_factory(data_dir=...)`` 可显式传 tmp 路径;``tests/conftest.py``
全局 autouse fixture ``_isolate_task_registry`` 调本工厂建 per-test tmp ``_tasks.db``
+ ``task_registry.set_session_factory`` 注入,避免单测写真实 ``./workspaces``。
"""

from __future__ import annotations

from collections.abc import Callable
from pathlib import Path

import structlog
from sqlalchemy import Engine, create_engine, text
from sqlalchemy.orm import Session

from src.config import settings
from src.core.db.task_models import TASK_STATUS_VALUES, TaskBase

_log = structlog.get_logger(__name__)

# reconcile 搬数据时显式列出列名(避免 ``SELECT *`` 列序依赖;与
# ``TaskRecordORM`` 字段一一对应)。
_TASK_COLUMNS = (
    "task_id",
    "type",
    "status",
    "progress",
    "result_json",
    "error_json",
    "workspace_id",
    "cancel_requested",
    "created_at",
    "updated_at",
)


def make_task_session_factory(
    data_dir: Path | None = None,
) -> Callable[[], Session]:
    """构造任务持久化 SQLite ``Session`` 零参工厂。

    ``data_dir=None`` → 用 ``settings.data_dir``(默认 ``./workspaces``)。
    每次调用工厂新开一个 Session(对齐 P2-B-14 短 session 模式),
    ``TaskBase.metadata.create_all`` idempotent 建 ``task_records`` 表。
    """

    root = Path(data_dir) if data_dir is not None else settings.data_dir
    db_path = root.resolve() / "_tasks.db"

    def _factory() -> Session:
        # 确保父目录存在(生产路径下 workspaces_root 通常已建,单测 / 全新部署需要)。
        db_path.parent.mkdir(parents=True, exist_ok=True)
        engine = create_engine(f"sqlite:///{db_path}")
        TaskBase.metadata.create_all(engine)
        return Session(engine)

    return _factory


def reconcile_task_status_check(data_dir: Path | None = None) -> bool:
    """无损重建既有 ``_tasks.db`` 的 ``status`` CHECK 约束到当前 6 档(J1)。

    **为何需要**:``task_records`` 非 alembic 管理(独立 ``TaskBase``,见模块
    docstring),表由 ``create_all`` 建。``create_all`` 只建缺失的表、**不改既有
    表的 CHECK 约束** → J1 把 ``cancelled`` 加进 ``TASK_STATUS_VALUES`` 后,既有
    ``_tasks.db`` 仍是旧 5 档 CHECK,写 ``status='cancelled'`` 会 IntegrityError。

    **做法**(SQLite 不支持 ``ALTER TABLE ... DROP/ADD CONSTRAINT``,经表重建):
    rename 旧表 → ``create_all`` 按新 ORM 定义建 6 档 CHECK 新表 → 显式列搬数据
    → drop 旧表。复用 ORM 定义而非手写 DDL,与 SSOT(``TASK_STATUS_VALUES``)对齐。

    **幂等**:读 ``sqlite_master`` 表定义,所有当前 ``TASK_STATUS_VALUES`` 字面都
    已在 CHECK 里 → 直接返回(不重建)。``_tasks.db`` / ``task_records`` 不存在
    (全新部署)→ 返回(``create_all`` 会建新 6 档表,无需 reconcile)。

    在 lifespan 启动 hook(``_run_task_startup_scan`` 前)调用一次即可;单测用
    per-test tmp db(``create_all`` 直接得 6 档)无需调本函数。

    Returns:
        是否实际重建了表(``True`` = 旧 CHECK 已无损升级;``False`` = 无需重建)。
    """
    root = Path(data_dir) if data_dir is not None else settings.data_dir
    db_path = root.resolve() / "_tasks.db"
    if not db_path.exists():
        return False

    engine = create_engine(f"sqlite:///{db_path}")
    try:
        return _reconcile_on_engine(engine)
    finally:
        engine.dispose()


def _reconcile_on_engine(engine: Engine) -> bool:
    """``reconcile_task_status_check`` 的引擎级实现(便于测试注入临时引擎)。"""
    with engine.begin() as conn:
        row = conn.execute(
            text(
                "SELECT sql FROM sqlite_master "
                "WHERE type='table' AND name='task_records'"
            )
        ).fetchone()
        if row is None:
            return False  # 表不存在(全新部署)→ create_all 直接建 6 档
        table_sql = row[0] or ""
        # 幂等:所有当前允许值都已在表定义的 CHECK 里 → 无需重建
        if all(f"'{v}'" in table_sql for v in TASK_STATUS_VALUES):
            return False

        # 表重建:rename → create_all 建新 6 档表 → 搬数据 → drop 旧表
        conn.execute(
            text("ALTER TABLE task_records RENAME TO _task_records_j1_old")
        )
    # create_all 在新连接上按当前 ORM 定义(6 档 CHECK)重建 task_records
    TaskBase.metadata.create_all(engine)
    with engine.begin() as conn:
        cols = ", ".join(_TASK_COLUMNS)
        conn.execute(
            text(
                f"INSERT INTO task_records ({cols}) "
                f"SELECT {cols} FROM _task_records_j1_old"
            )
        )
        conn.execute(text("DROP TABLE _task_records_j1_old"))
    _log.info("task_status_check_reconciled", new_values=list(TASK_STATUS_VALUES))
    return True
