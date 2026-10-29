"""Alembic 运行时配置(P1-B-16)。

**关键约定**:本项目每工作区独立 ``novel.db``,不存在"默认 db"概念。
``alembic.ini`` 中 ``sqlalchemy.url`` 留空占位;真实连接 URL 由调用方
通过环境变量 ``NOVEL_DB_PATH`` 注入(指向工作区内的 ``novel.db`` 文件)。

调用方式:

    NOVEL_DB_PATH=/abs/path/to/workspaces/<id>/data/novel.db \\
        uv run alembic upgrade head

未设置 ``NOVEL_DB_PATH`` 时直接抛 ``RuntimeError``,避免误连到不正确的 db
或当前 cwd 下凭空创建文件。
"""

from __future__ import annotations

import os
from logging.config import fileConfig
from pathlib import Path

from sqlalchemy import engine_from_config, pool

from alembic import context

# 引入 ORM metadata,为 autogenerate 提供 schema 真相。
from src.core.db import Base  # noqa: E402  (alembic 习惯顶部 import)


def _emit_database_migration_event(db_url: str) -> None:
    """落 OBSERVABILITY §2.4 ``database_migration`` 事件(P4-B-05)。

    ``from_version`` / ``to_version`` 在 alembic env 入口阶段无法稳定拿到
    (取决于 ``script_directory`` 计算 + offline / online 模式差异),仅
    记 ``url`` + ``mode``;具体 revision 信息由 alembic 自身 stdout 输出
    互补。失败 swallow(structlog 未配置 / logging.config 未就位时不应
    阻断 alembic 命令),对齐 P2-B-19 / P3-B-07 / P4-B-02/03/04 失败 swallow
    五先例。
    """
    try:
        import structlog

        structlog.get_logger(__name__).info(
            "database_migration",
            db_url=db_url,
            mode="offline" if context.is_offline_mode() else "online",
        )
    except Exception:  # noqa: BLE001 — alembic env 不能因日志失败阻断
        pass

config = context.config

if config.config_file_name is not None:
    fileConfig(config.config_file_name)

target_metadata = Base.metadata


def _resolve_db_url() -> str:
    """从环境变量 ``NOVEL_DB_PATH`` 解析为 SQLite URL。

    缺失或为空时抛 ``RuntimeError``,提示调用方显式指定路径。
    """
    db_path = os.environ.get("NOVEL_DB_PATH", "").strip()
    if not db_path:
        raise RuntimeError(
            "NOVEL_DB_PATH 未设置:本项目每工作区独立 novel.db,需通过环境"
            "变量显式指定目标 db 文件路径。例:"
            "NOVEL_DB_PATH=/abs/path/workspaces/<id>/data/novel.db "
            "uv run alembic upgrade head"
        )
    abs_path = Path(db_path).resolve()
    return f"sqlite:///{abs_path}"


def run_migrations_offline() -> None:
    """offline 模式:生成 SQL 脚本不实际连接 db。"""
    url = _resolve_db_url()
    _emit_database_migration_event(url)
    context.configure(
        url=url,
        target_metadata=target_metadata,
        literal_binds=True,
        dialect_opts={"paramstyle": "named"},
        render_as_batch=True,
    )
    with context.begin_transaction():
        context.run_migrations()


def run_migrations_online() -> None:
    """online 模式:连接 db 执行 DDL。

    ``render_as_batch=True``:SQLite 不支持 ALTER COLUMN 等大量 DDL,
    alembic 用"批量模式"绕开(重建表 + 拷贝数据)。P1 只跑首次 upgrade
    暂时不需要,但开启后续任意版本 schema 演进都不会卡。
    """
    section = config.get_section(config.config_ini_section, {}) or {}
    db_url = _resolve_db_url()
    section["sqlalchemy.url"] = db_url
    _emit_database_migration_event(db_url)

    connectable = engine_from_config(
        section,
        prefix="sqlalchemy.",
        poolclass=pool.NullPool,
    )

    with connectable.connect() as connection:
        context.configure(
            connection=connection,
            target_metadata=target_metadata,
            render_as_batch=True,
        )
        with context.begin_transaction():
            context.run_migrations()


if context.is_offline_mode():
    run_migrations_offline()
else:
    run_migrations_online()
