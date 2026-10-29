"""系统级 SQLite 工厂(P4-B-04)。

``system_metrics_daily`` 是跨工作区聚合表,放在 ``settings.data_dir/_system_metrics.db``
单文件 db(``_system_metrics`` 前缀对齐 ``_active`` 软链命名约定,与工作区
目录字符集 ``[a-zA-Z0-9_-]{1,64}`` 不冲突 — 工作区是目录,本文件是文件,
PathOS 维度不重叠)。

API 中间件 ``ApiMetricsMiddleware`` + metrics 路由 ``/system/metrics`` 共用
本工厂返回的 session(``Base.metadata.create_all`` idempotent 建表;实际只
用 ``system_metrics_daily`` 表)。

为何不复用工作区 db
-------------------

- 系统级语义(跨工作区计数 / 平均);放进任一工作区 db 会让该 db 承担"代表
  全系统"语义,违反"工作区物理隔离"硬约束(CLAUDE.md §硬约束 9)。
- 单独 ``_system_metrics.db`` 文件读 / 写不依赖具体工作区,API 限流 /
  cost_guard 联动(P4-B-08)+ 系统健康检查(P4-B-09)等后续任务也复用同库
  顺手。

测试隔离
--------

模块级 ``settings.data_dir`` 在 import 时即解析,但本工厂在**调用时**重新读
``settings.data_dir`` 当前值,所以 monkeypatch ``src.config.settings.data_dir``
后 factory 自然指向新路径(测试 fixture 不需 reimport 模块)。
"""

from __future__ import annotations

from collections.abc import Callable
from pathlib import Path

from sqlalchemy import create_engine
from sqlalchemy.orm import Session

from src.config import settings
from src.core.db import Base


def make_system_session_factory(
    data_dir: Path | None = None,
) -> Callable[[], Session]:
    """构造系统级 SQLite ``Session`` 零参工厂。

    ``data_dir=None`` → 用 ``settings.data_dir``(默认 ``./workspaces``)。
    每次调用工厂都新开一个 Session(对齐 P2-B-14 短 session 模式),
    ``Base.metadata.create_all`` idempotent 建表(只用 ``system_metrics_daily``,
    其余表副产 schema 不影响 — SQLite 表为空时无开销)。
    """

    root = Path(data_dir) if data_dir is not None else settings.data_dir
    db_path = root.resolve() / "_system_metrics.db"

    def _factory() -> Session:
        # 确保父目录存在(workspaces_root 在生产路径下通常已建,但单测 / 全
        # 新部署场景需要)
        db_path.parent.mkdir(parents=True, exist_ok=True)
        engine = create_engine(f"sqlite:///{db_path}")
        Base.metadata.create_all(engine)
        return Session(engine)

    return _factory
