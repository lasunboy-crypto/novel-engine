"""工作区版本比对与数据迁移服务(P6-B-08)。

对应 PROJECT_SPEC §11.1 "工作区版本管理":每工作区 ``meta.json`` 记录
``engine_version`` + ``schema_version``,引擎升级后扫描比对,不匹配走 doctor
+ migrate 流程。本任务实现版本状态查询 + 受控迁移端点 +``BackupService``
做 pre-migration 备份(对齐 plan §P6-B-08 字面)。

设计要点
========

- **engine_version**:``src/version.py`` 的 ``ENGINE_VERSION``(SEMVER ``X.Y.Z``);
  workspace 侧来自 ``meta.json.engine_version``(P1-B-04 落字段)。**主版本号
  跨级(``major`` 不一致)** 视为 v2 不兼容 → 422 ``WORKSPACE_VERSION_INCOMPATIBLE``。
  次版本 / patch 不一致允许 migrate。
- **schema_version**:动态读 ``alembic.script.ScriptDirectory.get_current_head()``
  字符串(如 ``"0009_pipeline_runs_table"``);workspace 侧来自 ``meta.json.
  schema_version``(P6-B-08 字段,旧工作区 None 视为需迁移)。
- **migrate 流程**(对齐 plan §P6-B-08 字面 + ④ 三步顺序):
  1. 二次确认 ``confirm == workspace_id``(对齐 ``PurgeConfirmMismatch`` /
     ``RestoreConfirmMismatch`` 同模式)
  2. **engine_version major 不兼容拒绝** → 422 ``WORKSPACE_VERSION_INCOMPATIBLE``
  3. 调 ``BackupService.create_manual_backup`` 做 pre-migration 备份;**备份失败 →
     500 ``WORKSPACE_MIGRATION_BACKUP_FAILED`` 不开始迁移**(确保 rollback 后路)
  4. 通过 ``alembic.command.upgrade(cfg, "head")`` 程序化跑 alembic upgrade,
     ``NOVEL_DB_PATH`` 环境变量定向到工作区 db(沿用 ``alembic/env.py`` 既有
     ``_resolve_db_url`` 模式)
  5. 落 ``meta.json.schema_version`` + ``engine_version`` 更新为当前值
  6. WS publish ``version_migrated`` 到 ``workspace.{workspace_id}.events`` 频道
- **启动 hook 配套**:``main.py`` lifespan 扫所有工作区 meta.json,版本不匹配
  落 ``workspace_version_mismatch`` warn 结构化事件,由前端 / CLI doctor 路径
  显式触发 migrate(不主动迁移避免意外覆盖)。
"""

from __future__ import annotations

import os
import json
import re
import sqlite3
import threading
import tempfile
from dataclasses import dataclass
from pathlib import Path
from collections.abc import Callable
from typing import Any, Literal

import structlog
from sqlalchemy import CheckConstraint, UniqueConstraint, create_engine, inspect
from sqlalchemy.dialects import sqlite as sqlite_dialect

from src.api.websocket.channels import workspace_events_channel
from src.core.models import WORKSPACE_ID_PATTERN
from src.core.db import Base
from src.core.models.workspace import Workspace
from src.core.workspace.meta import write_meta
from src.core.workspace.workspace_paths import WorkspacePaths
from src.services.backup_service import BackupService, BackupServiceError
from src.services.workspace_operation_guard import (
    WorkspaceOperationConflictError,
    ensure_workspace_quiescent,
    workspace_operation_lock,
)
from src.services.workspace_service import WorkspaceService
from src.version import ENGINE_VERSION

_log = structlog.get_logger(__name__)

_R8_LEASE_PREVIOUS_SCHEMA = "0029_structured_rewrite_constraint_results"
_R8_LEASE_TABLE = "chapter_operation_leases"
_R8_LEGACY_CREATE_ALL_DEFAULTS = frozenset(
    {
        ("chapter_cards", "card_status"),
        ("characters", "identity_facets"),
        ("characters", "state_timeline"),
        ("review_results", "constraint_violations"),
        ("review_results", "constraint_results"),
        ("review_results", "hard_constraint_regressions"),
        ("timeline_entries", "is_flashback"),
    }
)
_ALEMBIC_MIGRATION_LOCK = threading.RLock()
_RUNTIME_INITIALIZATION_MARKER = ".runtime_initialization_pending.json"
_STARTUP_RESERVED_DIRECTORIES = frozenset({"system_logs"})
_STARTUP_RESERVED_DIRECTORY_PREFIXES = ("_archive_",)


# =============================================================================
# 服务异常
# =============================================================================


class WorkspaceVersionServiceError(Exception):
    """版本服务通用异常(工作区不存在等)。

    路由层翻 WORKSPACE_CONFLICT 409(沿 BackupServiceError 同模式)。
    """


class WorkspaceStartupRecoveryAdmissionError(WorkspaceVersionServiceError):
    """当前版本 meta 与现存 SQLite 结构冲突，启动恢复必须零写入硬停。"""

    def __init__(self, workspace_id: str, reason: str) -> None:
        self.workspace_id = workspace_id
        self.reason = reason
        super().__init__(
            f"工作区启动恢复 admission 失败: workspace={workspace_id} reason={reason}"
        )


class WorkspaceVersionIncompatibleError(WorkspaceVersionServiceError):
    """workspace engine 主版本与当前引擎主版本跨级,不兼容。

    路由层翻 ``WORKSPACE_VERSION_INCOMPATIBLE 422``。``details`` 含 4 字段
    供前端展示 — workspace 应升级或换专门 v2 迁移工具。
    """

    def __init__(
        self,
        workspace_id: str,
        workspace_engine_version: str,
        current_engine_version: str,
    ) -> None:
        self.workspace_id = workspace_id
        self.workspace_engine_version = workspace_engine_version
        self.current_engine_version = current_engine_version
        super().__init__(
            f"工作区主版本不兼容: workspace={workspace_id} "
            f"workspace_engine={workspace_engine_version} "
            f"current_engine={current_engine_version}"
        )


class WorkspaceMigrationConfirmInvalidError(WorkspaceVersionServiceError):
    """``POST :migrate`` 二次确认字段不匹配 ``workspace_id``。

    路由层翻 ``WORKSPACE_MIGRATION_CONFIRM_INVALID 422``。
    """

    def __init__(self, workspace_id: str) -> None:
        self.workspace_id = workspace_id
        super().__init__(f"二次确认字段不匹配: 期望 confirm='{workspace_id}'")


class WorkspaceMigrationBackupFailedError(WorkspaceVersionServiceError):
    """pre-migration 备份失败,迁移未开始(保证 rollback 后路)。

    路由层翻 ``WORKSPACE_MIGRATION_BACKUP_FAILED 500``。``details`` 含
    underlying error type / message。
    """

    def __init__(self, workspace_id: str, reason: str) -> None:
        self.workspace_id = workspace_id
        self.reason = reason
        super().__init__(
            f"pre-migration 备份失败,迁移未开始: workspace={workspace_id} "
            f"reason={reason}"
        )


# =============================================================================
# 出参 dataclass
# =============================================================================


@dataclass(frozen=True)
class VersionStatus:
    """``GET /workspaces/{id}/version-status`` 返回结构。

    对齐 PROJECT_SPEC §11.1 字面 7 字段 — engine / schema 双轨 + match 标识
    + ``migration_required``(任一不匹配即 true,前端据此决定是否提示
    "doctor / migrate")。
    """

    workspace_id: str
    engine_version_match: bool
    schema_version_match: bool
    current_engine: str
    workspace_engine: str
    current_schema: str
    workspace_schema: str | None
    migration_required: bool


@dataclass(frozen=True)
class MigrationResult:
    """``POST /workspaces/{id}:migrate`` 返回结构。"""

    workspace_id: str
    pre_migration_backup_id: str
    from_engine_version: str
    to_engine_version: str
    from_schema_version: str | None
    to_schema_version: str


@dataclass(frozen=True)
class _SQLiteSchemaSnapshot:
    """只读 SQLite 结构快照；不把 Alembic 自身表算作业务表。"""

    business_columns: dict[str, frozenset[str]]
    column_signatures: dict[str, dict[str, tuple[str, bool, int]]]
    column_defaults: dict[str, dict[str, str | None]]
    foreign_keys: dict[
        str,
        frozenset[tuple[tuple[str, ...], str, tuple[str, ...], str]],
    ]
    unique_constraints: dict[str, frozenset[tuple[str, ...]]]
    explicit_indexes: dict[
        str,
        frozenset[tuple[tuple[str, ...], bool, bool]],
    ]
    check_constraints: dict[str, frozenset[tuple[str, str]]]
    integrity_check: tuple[str, ...]
    foreign_key_violations: tuple[tuple[Any, ...], ...]
    has_alembic_version_table: bool
    alembic_revision: str | None


@dataclass(frozen=True)
class _MigrationDatabasePlan:
    """迁移前 SQLite 验真后得到的最小数据库动作。"""

    stamp_revision: str | None
    upgrade_required: bool
    backup_schema_version: str | None


# =============================================================================
# 服务类
# =============================================================================


# 跨大版本比对结果(workspace_major != current_major 视为不兼容)。
_MAJOR_INCOMPATIBLE = "incompatible"
_MAJOR_COMPATIBLE = "compatible"


def get_current_schema_version() -> str:
    """读当前 Alembic head revision_id 字符串(模块级公开函数)。

    供 ``WorkspaceService.create`` 写新工作区 ``meta.json.schema_version`` 用
    (函数级 import 避开循环依赖)+ ``WorkspaceVersionService.get_version_status``
    内部对账用。**与 ``BackupService._get_current_schema_version`` 同源**(共用
    Alembic ScriptDirectory 推断路径,行为对齐;复制 ~6 行实现避免本任务额外
    重构 BackupService 接口耦合)。

    极端情况(``alembic/versions/`` 空)返 ``"unknown"`` — 生产路径不应触发
    (P6 期 head 至少 ``0009_pipeline_runs_table``)。
    """
    from alembic.script import ScriptDirectory

    # src/services/workspace_version_service.py -> repo root -> alembic/
    repo_root = Path(__file__).resolve().parents[2]
    script_location = repo_root / "alembic"
    script_dir = ScriptDirectory(str(script_location))
    head = script_dir.get_current_head()
    if head is None:
        return "unknown"
    return head


def _parse_major(version: str) -> int | None:
    """解析 SEMVER ``X.Y.Z`` 取主版本号 ``X``;非该格式返 ``None``。

    使用容错策略:非 SEMVER 字符串(如 ``"unknown"``)视为不可比对,
    上层按 incompatible 处理。
    """
    parts = version.split(".")
    if len(parts) < 1:
        return None
    try:
        return int(parts[0])
    except ValueError:
        return None


def _compare_major_version(workspace_version: str, current_version: str) -> str:
    """跨大版本兼容性判定 — 主版本号 ``X`` 一致即兼容。"""
    ws_major = _parse_major(workspace_version)
    cur_major = _parse_major(current_version)
    if ws_major is None or cur_major is None:
        return _MAJOR_INCOMPATIBLE
    return _MAJOR_COMPATIBLE if ws_major == cur_major else _MAJOR_INCOMPATIBLE


def _expected_workspace_columns(
    *, include_operation_lease: bool
) -> dict[str, frozenset[str]]:
    """从当前 ORM 生成 0029/0030 的精确业务表和列名集合。"""

    expected: dict[str, frozenset[str]] = {}
    for table_name, table in Base.metadata.tables.items():
        if table_name == _R8_LEASE_TABLE and not include_operation_lease:
            continue
        expected[table_name] = frozenset(column.name for column in table.columns)
    return expected


def _expected_workspace_defaults(
    *, include_operation_lease: bool
) -> dict[str, dict[str, str | None]]:
    """从当前 ORM 生成 canonical 0029/0030 server-default 合同。"""

    expected: dict[str, dict[str, str | None]] = {}
    for table_name, table in Base.metadata.tables.items():
        if table_name == _R8_LEASE_TABLE and not include_operation_lease:
            continue
        expected[table_name] = {
            column.name: _normalize_sql_default(
                None if column.server_default is None else column.server_default.arg
            )
            for column in table.columns
        }
    return expected


def _legacy_create_all_workspace_defaults(
    canonical: dict[str, dict[str, str | None]],
) -> dict[str, dict[str, str | None]]:
    """返回 R8 live 旧 ``Base.create_all`` 的精确 default 形状。

    这七个 ORM ``server_default`` 与 0030 同卡加入；迁移前创建的真实 0029
    无 Alembic stamp 工作区因而没有对应 DDL default。只接受七项同时缺失的
    完整历史签名，不能把任意部分缺失或其他 default 漂移当作兼容形态。
    """

    legacy = {table_name: dict(values) for table_name, values in canonical.items()}
    for table_name, column_name in _R8_LEGACY_CREATE_ALL_DEFAULTS:
        legacy[table_name][column_name] = None
    return legacy


def _strip_outer_parentheses(value: str) -> str:
    text = value.strip()
    while text.startswith("(") and text.endswith(")"):
        depth = 0
        encloses_all = True
        for index, char in enumerate(text):
            if char == "(":
                depth += 1
            elif char == ")":
                depth -= 1
                if depth == 0 and index != len(text) - 1:
                    encloses_all = False
                    break
        if not encloses_all or depth != 0:
            break
        text = text[1:-1].strip()
    return text


def _normalize_sql_default(value: object | None) -> str | None:
    if value is None:
        return None
    text = _strip_outer_parentheses(str(value))
    if len(text) >= 2 and text[0] == text[-1] and text[0] in {"'", '"'}:
        quote = text[0]
        text = text[1:-1].replace(quote * 2, quote)
    return text


def _normalize_check_sql(value: str) -> str:
    return " ".join(_strip_outer_parentheses(value).split()).lower()


def _read_sqlite_schema_snapshot(db_path: Path) -> _SQLiteSchemaSnapshot:
    """从一致性 SQLite snapshot 读取表、列和 Alembic revision。

    主库存在非空 WAL 时不能用 ``immutable=1`` 直接读主文件（会漏掉 WAL 中
    已提交事务），也不能为了预检手工 checkpoint 原库。这里先用 SQLite
    online backup API 把主文件与 WAL 合成到临时数据库，再只读检查临时副本；
    没有 WAL 时直接 immutable 读取主文件，保持启动 admission 零业务写入。
    """

    if not db_path.is_file():
        return _SQLiteSchemaSnapshot({}, {}, {}, {}, {}, {}, {}, (), (), False, None)

    wal_path = Path(f"{db_path}-wal")
    if wal_path.is_file() and wal_path.stat().st_size > 0:
        with tempfile.TemporaryDirectory(
            prefix="novel-engine-schema-snapshot-"
        ) as temp_dir:
            snapshot_path = Path(temp_dir) / "novel.db"
            source_uri = f"{db_path.resolve().as_uri()}?mode=ro"
            try:
                source = sqlite3.connect(source_uri, uri=True)
                try:
                    source.execute("PRAGMA query_only = ON")
                    target = sqlite3.connect(snapshot_path)
                    try:
                        source.backup(target)
                    finally:
                        target.close()
                finally:
                    source.close()
            except sqlite3.Error as exc:
                raise RuntimeError(
                    f"SQLite WAL 一致性 snapshot 失败: {type(exc).__name__}: {exc}"
                ) from exc
            return _read_sqlite_schema_snapshot(snapshot_path)

    uri = f"{db_path.resolve().as_uri()}?mode=ro&immutable=1"
    try:
        connection = sqlite3.connect(uri, uri=True)
        try:
            table_names = {
                str(row[0])
                for row in connection.execute(
                    "SELECT name FROM sqlite_master "
                    "WHERE type = 'table' AND name NOT GLOB 'sqlite_*'"
                )
            }
            has_alembic = "alembic_version" in table_names
            integrity_check = tuple(
                str(row[0]) for row in connection.execute("PRAGMA integrity_check")
            )
            foreign_key_violations = tuple(
                tuple(row) for row in connection.execute("PRAGMA foreign_key_check")
            )
            business_columns: dict[str, frozenset[str]] = {}
            column_signatures: dict[str, dict[str, tuple[str, bool, int]]] = {}
            column_defaults: dict[str, dict[str, str | None]] = {}
            foreign_keys: dict[
                str,
                frozenset[tuple[tuple[str, ...], str, tuple[str, ...], str]],
            ] = {}
            unique_constraints: dict[str, frozenset[tuple[str, ...]]] = {}
            explicit_indexes: dict[
                str,
                frozenset[tuple[tuple[str, ...], bool, bool]],
            ] = {}
            for table_name in sorted(table_names - {"alembic_version"}):
                quoted = table_name.replace('"', '""')
                rows = connection.execute(f'PRAGMA table_xinfo("{quoted}")').fetchall()
                business_columns[table_name] = frozenset(
                    str(row[1]) for row in rows if int(row[6]) == 0
                )
                column_signatures[table_name] = {
                    str(row[1]): (
                        str(row[2]).strip().upper(),
                        not bool(row[3]),
                        int(row[5]),
                    )
                    for row in rows
                    if int(row[6]) == 0
                }
                column_defaults[table_name] = {
                    str(row[1]): _normalize_sql_default(row[4])
                    for row in rows
                    if int(row[6]) == 0
                }

                foreign_key_rows = connection.execute(
                    f'PRAGMA foreign_key_list("{quoted}")'
                ).fetchall()
                grouped_foreign_keys: dict[int, list[tuple[Any, ...]]] = {}
                for row in foreign_key_rows:
                    grouped_foreign_keys.setdefault(int(row[0]), []).append(row)
                foreign_keys[table_name] = frozenset(
                    (
                        tuple(str(row[3]) for row in sorted(group, key=lambda r: r[1])),
                        str(group[0][2]),
                        tuple(str(row[4]) for row in sorted(group, key=lambda r: r[1])),
                        str(group[0][6] or "").upper(),
                    )
                    for group in grouped_foreign_keys.values()
                )

                uniques: set[tuple[str, ...]] = set()
                indexes: set[tuple[tuple[str, ...], bool, bool]] = set()
                for index_row in connection.execute(
                    f'PRAGMA index_list("{quoted}")'
                ).fetchall():
                    index_name = str(index_row[1]).replace('"', '""')
                    origin = str(index_row[3])
                    index_columns = tuple(
                        str(row[2])
                        for row in connection.execute(
                            f'PRAGMA index_info("{index_name}")'
                        ).fetchall()
                    )
                    if origin == "u":
                        uniques.add(index_columns)
                    elif origin == "c":
                        indexes.add(
                            (
                                index_columns,
                                bool(index_row[2]),
                                bool(index_row[4]),
                            )
                        )
                unique_constraints[table_name] = frozenset(uniques)
                explicit_indexes[table_name] = frozenset(indexes)

            alembic_revision: str | None = None
            if has_alembic:
                revisions = connection.execute(
                    "SELECT version_num FROM alembic_version"
                ).fetchall()
                if len(revisions) != 1 or not str(revisions[0][0]).strip():
                    raise RuntimeError("alembic_version 必须且只能有一条非空 revision")
                alembic_revision = str(revisions[0][0])
        finally:
            connection.close()
    except sqlite3.Error as exc:
        raise RuntimeError(f"SQLite 结构读取失败: {type(exc).__name__}: {exc}") from exc

    engine = create_engine(
        "sqlite://",
        creator=lambda: sqlite3.connect(uri, uri=True),
    )
    try:
        inspector = inspect(engine)
        check_constraints = {
            table_name: frozenset(
                (
                    str(constraint.get("name") or ""),
                    _normalize_check_sql(str(constraint.get("sqltext") or "")),
                )
                for constraint in inspector.get_check_constraints(table_name)
            )
            for table_name in business_columns
        }
    except Exception as exc:  # noqa: BLE001
        raise RuntimeError(
            f"SQLite CHECK 约束读取失败: {type(exc).__name__}: {exc}"
        ) from exc
    finally:
        engine.dispose()

    return _SQLiteSchemaSnapshot(
        business_columns=business_columns,
        column_signatures=column_signatures,
        column_defaults=column_defaults,
        foreign_keys=foreign_keys,
        unique_constraints=unique_constraints,
        explicit_indexes=explicit_indexes,
        check_constraints=check_constraints,
        integrity_check=integrity_check,
        foreign_key_violations=foreign_key_violations,
        has_alembic_version_table=has_alembic,
        alembic_revision=alembic_revision,
    )


def _schema_shape_mismatches(
    snapshot: _SQLiteSchemaSnapshot,
    *,
    include_operation_lease: bool,
    allow_legacy_create_all_defaults: bool = False,
) -> list[str]:
    """返回实际 SQLite 与目标 ORM 业务表/列集合的差异。"""

    expected = _expected_workspace_columns(
        include_operation_lease=include_operation_lease
    )
    canonical_defaults = _expected_workspace_defaults(
        include_operation_lease=include_operation_lease
    )
    defaults_match = snapshot.column_defaults == canonical_defaults
    if allow_legacy_create_all_defaults:
        defaults_match = defaults_match or snapshot.column_defaults == (
            _legacy_create_all_workspace_defaults(canonical_defaults)
        )
    actual_tables = set(snapshot.business_columns)
    expected_tables = set(expected)
    mismatches: list[str] = []
    if snapshot.integrity_check != ("ok",):
        mismatches.append(f"integrity_check={list(snapshot.integrity_check)}")
    if snapshot.foreign_key_violations:
        mismatches.append(
            "foreign_key_check="
            f"{[tuple(row) for row in snapshot.foreign_key_violations]}"
        )
    missing_tables = sorted(expected_tables - actual_tables)
    extra_tables = sorted(actual_tables - expected_tables)
    if missing_tables:
        mismatches.append(f"missing_tables={missing_tables}")
    if extra_tables:
        mismatches.append(f"extra_tables={extra_tables}")
    for table_name in sorted(expected_tables & actual_tables):
        table = Base.metadata.tables[table_name]
        missing_columns = sorted(
            expected[table_name] - snapshot.business_columns[table_name]
        )
        extra_columns = sorted(
            snapshot.business_columns[table_name] - expected[table_name]
        )
        if missing_columns:
            mismatches.append(f"{table_name}.missing_columns={missing_columns}")
        if extra_columns:
            mismatches.append(f"{table_name}.extra_columns={extra_columns}")

        primary_key_names = [column.name for column in table.primary_key.columns]
        expected_column_signatures = {
            column.name: (
                str(column.type.compile(dialect=sqlite_dialect.dialect()))
                .strip()
                .upper(),
                bool(column.nullable),
                (
                    primary_key_names.index(column.name) + 1
                    if column.name in primary_key_names
                    else 0
                ),
            )
            for column in table.columns
        }
        if snapshot.column_signatures[table_name] != expected_column_signatures:
            mismatches.append(f"{table_name}.column_signatures")

        if not defaults_match and (
            snapshot.column_defaults[table_name] != canonical_defaults[table_name]
        ):
            mismatches.append(f"{table_name}.column_defaults")

        expected_foreign_keys = frozenset(
            (
                tuple(element.parent.name for element in constraint.elements),
                str(constraint.referred_table.name),
                tuple(str(element.column.name) for element in constraint.elements),
                str(constraint.ondelete or "").upper(),
            )
            for constraint in table.foreign_key_constraints
        )
        if snapshot.foreign_keys[table_name] != expected_foreign_keys:
            mismatches.append(f"{table_name}.foreign_keys")

        expected_uniques = frozenset(
            tuple(column.name for column in constraint.columns)
            for constraint in table.constraints
            if isinstance(constraint, UniqueConstraint)
        )
        if snapshot.unique_constraints[table_name] != expected_uniques:
            mismatches.append(f"{table_name}.unique_constraints")

        expected_indexes = frozenset(
            (
                tuple(column.name for column in index.columns),
                bool(index.unique),
                bool(index.dialect_options["sqlite"].get("where") is not None),
            )
            for index in table.indexes
        )
        if snapshot.explicit_indexes[table_name] != expected_indexes:
            mismatches.append(f"{table_name}.explicit_indexes")

        expected_checks = frozenset(
            (
                str(constraint.name or ""),
                _normalize_check_sql(str(constraint.sqltext)),
            )
            for constraint in table.constraints
            if isinstance(constraint, CheckConstraint)
        )
        if snapshot.check_constraints[table_name] != expected_checks:
            mismatches.append(f"{table_name}.check_constraints")
    return mismatches


def list_workspaces_for_startup_admission(
    workspace_service: WorkspaceService,
) -> list[Workspace]:
    """验真所有带 meta 的工作区，并对 ``_active`` 目标 fail-closed。"""

    root = workspace_service.workspaces_root
    if not root.exists():
        return []
    active_path = root / "_active"
    active_target: str | None = None
    if active_path.exists() or active_path.is_symlink():
        if not active_path.is_symlink():
            raise WorkspaceStartupRecoveryAdmissionError(
                "_active",
                "_active 必须是安全的相对符号链接",
            )
        target = os.readlink(active_path)
        if Path(target).is_absolute() or len(Path(target).parts) != 1:
            raise WorkspaceStartupRecoveryAdmissionError(
                "_active",
                f"_active 目标路径非法: {target!r}",
            )
        active_target = target
        if _is_startup_reserved_directory(target):
            raise WorkspaceStartupRecoveryAdmissionError(
                target,
                "_active 不得指向产品保留目录",
            )
        if re.fullmatch(WORKSPACE_ID_PATTERN, target) is None:
            raise WorkspaceStartupRecoveryAdmissionError(
                "_active",
                f"_active 目标路径非法: {target!r}",
            )
        if not (root / target).is_dir():
            raise WorkspaceStartupRecoveryAdmissionError(
                target,
                "_active 目标工作区目录不存在",
            )
        if not (root / target / "meta.json").is_file():
            raise WorkspaceStartupRecoveryAdmissionError(
                target,
                "_active 目标工作区目录缺少 meta.json",
            )
    workspaces: list[Workspace] = []
    for entry in sorted(root.iterdir()):
        if entry.is_symlink() or _is_startup_reserved_directory(entry.name):
            continue
        if not entry.is_dir():
            continue
        meta_path = entry / "meta.json"
        if not meta_path.is_file():
            if entry.name == active_target:
                raise WorkspaceStartupRecoveryAdmissionError(
                    entry.name,
                    "_active 目标工作区目录缺少 meta.json",
                )
            # live root 含历史 evidence/test 保留目录；没有 meta 且非 active
            # 的目录不是可打开工作区，保留但不进入任何 DB 恢复或写入。
            continue
        try:
            workspace = workspace_service.info(entry.name)
        except Exception as exc:  # noqa: BLE001
            raise WorkspaceStartupRecoveryAdmissionError(
                entry.name,
                f"meta.json 无法读取: {type(exc).__name__}: {exc}",
            ) from exc
        if workspace.workspace_id != entry.name:
            raise WorkspaceStartupRecoveryAdmissionError(
                entry.name,
                f"目录名与 meta.workspace_id 不一致: meta={workspace.workspace_id!r}",
            )
        workspaces.append(workspace)
    return workspaces


def _is_startup_reserved_directory(directory_name: str) -> bool:
    """识别不属于可运行 workspace 的产品/证据保留目录。

    ``_archive_*`` 是历史物理快照：目录名有意加入证据标签，内部 meta 仍保留
    原 workspace 身份。启动只跳过这一精确前缀，不读取或迁移快照；普通目录的
    name/meta identity 仍严格验真，且 ``_active`` 明确禁止指向任何保留目录。
    """

    return (
        directory_name.startswith(".")
        or directory_name in _STARTUP_RESERVED_DIRECTORIES
        or directory_name.startswith(_STARTUP_RESERVED_DIRECTORY_PREFIXES)
    )


def _is_frozen_fresh_workspace(
    workspace_service: WorkspaceService,
    workspace: Workspace,
) -> bool:
    paths = WorkspacePaths(
        workspace.workspace_id,
        workspaces_root=workspace_service.workspaces_root,
    )
    marker = paths.root / _RUNTIME_INITIALIZATION_MARKER
    try:
        payload = json.loads(marker.read_text(encoding="utf-8"))
    except (OSError, UnicodeDecodeError, json.JSONDecodeError):
        return False
    if payload.get("workspace_id") != workspace.workspace_id:
        return False
    if workspace.completed_chapters != 0:
        return False
    # ``create(activate=True)`` 会在首次开库前把 lifecycle 事件路由进 logs/；
    # 日志只是可观察性副产物，不能把仍有 marker 的空白工作区误判成已有小说产物。
    evidence_roots = (
        paths.outline_dir,
        paths.cards_dir,
        paths.chapters_dir,
        paths.chroma_dir,
        paths.assets_dir,
        paths.exports_dir,
        paths.archives_dir,
        paths.backups_dir,
    )
    return not any(
        child.is_file()
        for evidence_root in evidence_roots
        if evidence_root.exists()
        for child in evidence_root.rglob("*")
    )


def admit_workspaces_for_startup_recovery(
    workspace_service: WorkspaceService,
) -> list[str]:
    """只让当前版本且 SQLite 结构可安全访问的工作区进入启动恢复。

    旧版本只记录并跳过，绝不打开其数据库；current meta + 无数据库视作尚未
    初始化并跳过；current meta + 现存异常数据库则在任何恢复写入前硬停。
    """

    current_schema = get_current_schema_version()
    admitted: list[str] = []
    for workspace in list_workspaces_for_startup_admission(workspace_service):
        workspace_id = workspace.workspace_id
        if (
            workspace.engine_version != ENGINE_VERSION
            or workspace.schema_version != current_schema
        ):
            _log.warning(
                "workspace_startup_recovery_skipped_migration_required",
                workspace_id=workspace_id,
                workspace_engine=workspace.engine_version,
                current_engine=ENGINE_VERSION,
                workspace_schema=workspace.schema_version,
                current_schema=current_schema,
            )
            continue

        db_path = WorkspacePaths(
            workspace_id,
            workspaces_root=workspace_service.workspaces_root,
        ).db_path
        frozen_fresh = _is_frozen_fresh_workspace(
            workspace_service,
            workspace,
        )
        if not db_path.is_file():
            if not frozen_fresh:
                raise WorkspaceStartupRecoveryAdmissionError(
                    workspace_id,
                    "数据库缺失且没有可证明的首次初始化 marker，或工作区已含产品产物",
                )
            _log.info(
                "workspace_startup_recovery_skipped_uninitialized",
                workspace_id=workspace_id,
                reason="database_missing",
            )
            continue

        try:
            snapshot = _read_sqlite_schema_snapshot(db_path)
            if not snapshot.business_columns and not (
                snapshot.has_alembic_version_table
            ):
                if not frozen_fresh:
                    raise RuntimeError(
                        "数据库为空且没有可证明的首次初始化 marker，"
                        "或工作区已含产品产物"
                    )
                _log.info(
                    "workspace_startup_recovery_skipped_uninitialized",
                    workspace_id=workspace_id,
                    reason="database_empty",
                )
                continue
            if (
                snapshot.has_alembic_version_table
                and snapshot.alembic_revision != current_schema
            ):
                raise RuntimeError(
                    "meta 标为 current，但 alembic_version="
                    f"{snapshot.alembic_revision!r}，期望 {current_schema!r}"
                )
            mismatches = _schema_shape_mismatches(
                snapshot,
                include_operation_lease=True,
            )
            if mismatches:
                raise RuntimeError("; ".join(mismatches))
            _validate_database_workspace_identity(
                db_path,
                expected_workspace_id=workspace_id,
            )
        except Exception as exc:  # noqa: BLE001
            raise WorkspaceStartupRecoveryAdmissionError(
                workspace_id,
                f"{type(exc).__name__}: {exc}",
            ) from exc
        admitted.append(workspace_id)
    return admitted


def validate_workspace_for_runtime_open(
    workspace_service: WorkspaceService,
    workspace_id: str,
) -> bool:
    """在普通 session factory 打开数据库前只读验真 meta 与 schema。

    返回 ``True`` 表示现存数据库已初始化且结构与 current 完全一致；返回
    ``False`` 表示 current 新工作区尚无数据库（或数据库为空），允许正常
    ``create_all`` 首次初始化。旧 meta、旧 Alembic revision 或结构漂移一律
    在创建 engine/表之前抛 ``WorkspaceStartupRecoveryAdmissionError``。
    """

    try:
        workspace = workspace_service.info(workspace_id)
        current_schema = get_current_schema_version()
        if (
            workspace.engine_version != ENGINE_VERSION
            or workspace.schema_version != current_schema
        ):
            raise RuntimeError(
                "workspace meta 不是 current: "
                f"engine={workspace.engine_version!r}/{ENGINE_VERSION!r} "
                f"schema={workspace.schema_version!r}/{current_schema!r}"
            )

        db_path = WorkspacePaths(
            workspace_id,
            workspaces_root=workspace_service.workspaces_root,
        ).db_path
        frozen_fresh = _is_frozen_fresh_workspace(
            workspace_service,
            workspace,
        )
        if not db_path.is_file():
            if not frozen_fresh:
                raise RuntimeError(
                    "数据库缺失且没有可证明的首次初始化 marker，或工作区已含产品产物"
                )
            return False
        snapshot = _read_sqlite_schema_snapshot(db_path)
        if not snapshot.business_columns and not (snapshot.has_alembic_version_table):
            if not frozen_fresh:
                raise RuntimeError(
                    "数据库为空且没有可证明的首次初始化 marker，或工作区已含产品产物"
                )
            return False
        if (
            snapshot.has_alembic_version_table
            and snapshot.alembic_revision != current_schema
        ):
            raise RuntimeError(
                "alembic_version 不是 current: "
                f"actual={snapshot.alembic_revision!r} "
                f"expected={current_schema!r}"
            )
        mismatches = _schema_shape_mismatches(
            snapshot,
            include_operation_lease=True,
        )
        if mismatches:
            raise RuntimeError("; ".join(mismatches))
        _validate_database_workspace_identity(
            db_path,
            expected_workspace_id=workspace_id,
        )
    except WorkspaceStartupRecoveryAdmissionError:
        raise
    except Exception as exc:  # noqa: BLE001
        raise WorkspaceStartupRecoveryAdmissionError(
            workspace_id,
            f"{type(exc).__name__}: {exc}",
        ) from exc
    return True


def validate_r8_migration_rollback_database(
    db_path: Path,
    *,
    declared_schema: str,
    expected_workspace_id: str,
) -> None:
    """验真 0029 回滚库，含 DDL 已落但 revision 未推进的窄崩溃形态。"""

    if declared_schema != _R8_LEASE_PREVIOUS_SCHEMA:
        raise WorkspaceVersionServiceError(
            "迁移回滚只接受直接前序 schema: "
            f"actual={declared_schema!r} "
            f"expected={_R8_LEASE_PREVIOUS_SCHEMA!r}"
        )
    try:
        snapshot = _read_sqlite_schema_snapshot(db_path)
        if not snapshot.business_columns:
            raise RuntimeError("归档 novel.db 没有业务表")
        if snapshot.has_alembic_version_table and (
            snapshot.alembic_revision != _R8_LEASE_PREVIOUS_SCHEMA
        ):
            raise RuntimeError(
                f"归档数据库 revision 不是直接前序: {snapshot.alembic_revision!r}"
            )
        predecessor_mismatches = _schema_shape_mismatches(
            snapshot,
            include_operation_lease=False,
            allow_legacy_create_all_defaults=(not snapshot.has_alembic_version_table),
        )
        if not predecessor_mismatches:
            _validate_database_workspace_identity(
                db_path,
                expected_workspace_id=expected_workspace_id,
            )
            return
        # 0030 的 create-table 已提交、但 Alembic stamp 尚停 0029 时，归档
        # 的声明与 revision 都仍是 0029；只接受 exact current shape 这一条
        # 可由 0030 migration 自身 existing-table validator 安全续跑的签名。
        if snapshot.has_alembic_version_table:
            current_mismatches = _schema_shape_mismatches(
                snapshot,
                include_operation_lease=True,
            )
            if not current_mismatches:
                _validate_database_workspace_identity(
                    db_path,
                    expected_workspace_id=expected_workspace_id,
                )
                return
        raise RuntimeError("; ".join(predecessor_mismatches))
    except Exception as exc:  # noqa: BLE001
        raise WorkspaceVersionServiceError(
            f"迁移回滚归档数据库验真失败: {type(exc).__name__}: {exc}"
        ) from exc


def validate_current_restore_database(
    db_path: Path,
    *,
    declared_schema: str,
    expected_workspace_id: str,
) -> None:
    """破坏性 restore 前验真 current DB 的 revision/完整结构/完整性。"""

    current_schema = get_current_schema_version()
    if declared_schema != current_schema:
        raise WorkspaceVersionServiceError(
            "current restore 声明版本不匹配: "
            f"actual={declared_schema!r} expected={current_schema!r}"
        )
    try:
        snapshot = _read_sqlite_schema_snapshot(db_path)
        if not snapshot.business_columns:
            raise RuntimeError("归档 novel.db 没有业务表")
        if snapshot.has_alembic_version_table and (
            snapshot.alembic_revision != current_schema
        ):
            raise RuntimeError(
                f"current 归档数据库 revision 不匹配: {snapshot.alembic_revision!r}"
            )
        mismatches = _schema_shape_mismatches(
            snapshot,
            include_operation_lease=True,
        )
        if mismatches:
            raise RuntimeError("; ".join(mismatches))
        _validate_database_workspace_identity(
            db_path,
            expected_workspace_id=expected_workspace_id,
        )
    except Exception as exc:  # noqa: BLE001
        raise WorkspaceVersionServiceError(
            f"current restore 归档数据库验真失败: {type(exc).__name__}: {exc}"
        ) from exc


def _validate_database_workspace_identity(
    db_path: Path,
    *,
    expected_workspace_id: str,
) -> None:
    """确认单工作区 DB 的主身份及所有 workspace_id 域没有异源数据。"""

    uri = f"{db_path.resolve().as_uri()}?mode=ro"
    connection = sqlite3.connect(uri, uri=True)
    try:
        connection.execute("PRAGMA query_only = ON")
        workspace_rows = connection.execute(
            "SELECT workspace_id FROM workspaces"
        ).fetchall()
        tables = [
            str(row[0])
            for row in connection.execute(
                "SELECT name FROM sqlite_master "
                "WHERE type='table' AND name NOT GLOB 'sqlite_*'"
            )
        ]
        for table_name in tables:
            columns = {
                str(row[1])
                for row in connection.execute(f'PRAGMA table_info("{table_name}")')
            }
            if "workspace_id" not in columns:
                continue
            rows = connection.execute(
                f'SELECT DISTINCT workspace_id FROM "{table_name}" '
                "WHERE workspace_id IS NOT NULL"
            ).fetchall()
            unexpected = [row[0] for row in rows if row[0] != expected_workspace_id]
            if unexpected:
                raise RuntimeError(
                    f"{table_name}.workspace_id 含异工作区: {unexpected!r}"
                )
        if workspace_rows == [(expected_workspace_id,)]:
            return
        raise RuntimeError(
            "workspaces 主身份不匹配: "
            f"actual={workspace_rows!r} expected={expected_workspace_id!r}"
        )
    finally:
        connection.close()


class WorkspaceVersionService:
    """工作区版本比对与迁移服务。

    实例化时绑定 ``WorkspaceService`` + ``BackupService``(共用 workspaces_root)
    + 可选 ``ws_manager``(``None`` 时 publish noop,对齐 ChapterRewindService
    / BackupService 同模式)。

    2 公共方法 + 5 内部辅助:

    - ``get_version_status(workspace_id)`` — 读 meta.json + 当前引擎 → 比对返
      ``VersionStatus``(读操作,无副作用)
    - ``migrate(workspace_id, confirm)`` — 二次确认 + major 校验 + pre-migration
      备份 + alembic upgrade + meta.json 落盘 + WS publish
    """

    def __init__(
        self,
        *,
        workspace_service: WorkspaceService,
        backup_service: BackupService,
        ws_manager: object | None = None,
        active_task_checker: Callable[[str], bool] | None = None,
        restart_required_marker: Callable[[str], None] | None = None,
    ) -> None:
        self._workspace_service = workspace_service
        self._backup_service = backup_service
        self._ws_manager = ws_manager
        self._active_task_checker = active_task_checker
        self._restart_required_marker = restart_required_marker

    # ---- 公共方法 ---------------------------------------------------------

    def get_version_status(self, workspace_id: str) -> VersionStatus:
        """读 workspace meta.json + 当前引擎,返 ``VersionStatus``。

        无副作用;``WorkspaceServiceError`` (workspace 不存在 / meta.json 损坏)
        透传 — 路由层翻 WORKSPACE_CONFLICT / WORKSPACE_INVALID。
        """
        workspace = self._workspace_service.info(workspace_id)
        current_engine = ENGINE_VERSION
        current_schema = get_current_schema_version()
        ws_engine = workspace.engine_version
        ws_schema = workspace.schema_version

        engine_match = ws_engine == current_engine
        schema_match = ws_schema == current_schema
        migration_required = not (engine_match and schema_match)

        return VersionStatus(
            workspace_id=workspace_id,
            engine_version_match=engine_match,
            schema_version_match=schema_match,
            current_engine=current_engine,
            workspace_engine=ws_engine,
            current_schema=current_schema,
            workspace_schema=ws_schema,
            migration_required=migration_required,
        )

    def migrate(self, workspace_id: str, confirm: str) -> MigrationResult:
        """对工作区执行受控迁移(pre-migration 备份 → alembic upgrade head
        → 更新 meta.json)。

        - ``confirm != workspace_id`` → ``WorkspaceMigrationConfirmInvalidError 422``
        - workspace engine major != current engine major →
          ``WorkspaceVersionIncompatibleError 422``
        - pre-migration backup 失败 → ``WorkspaceMigrationBackupFailedError 500``
          (迁移**不开始**)
        - workspace 不存在 / meta.json 损坏 → ``WorkspaceServiceError`` 透传

        返 ``MigrationResult``(含 pre-migration backup_id 供前端撤销)。
        """
        if confirm != workspace_id:
            raise WorkspaceMigrationConfirmInvalidError(workspace_id)

        workspace_before = self._workspace_service.info(workspace_id)
        current_schema = get_current_schema_version()
        if (
            workspace_before.engine_version == ENGINE_VERSION
            and workspace_before.schema_version == current_schema
        ):
            raise WorkspaceVersionServiceError(
                f"工作区已经是当前版本，无需重复迁移: {workspace_id}"
            )
        if (
            _compare_major_version(
                workspace_before.engine_version,
                ENGINE_VERSION,
            )
            == _MAJOR_INCOMPATIBLE
        ):
            raise WorkspaceVersionIncompatibleError(
                workspace_id=workspace_id,
                workspace_engine_version=workspace_before.engine_version,
                current_engine_version=ENGINE_VERSION,
            )

        workspaces_root = self._workspace_service.workspaces_root
        paths = WorkspacePaths(
            workspace_id,
            workspaces_root=workspaces_root,
        )
        with _ALEMBIC_MIGRATION_LOCK:
            try:
                with workspace_operation_lock(workspaces_root, workspace_id):
                    ensure_workspace_quiescent(
                        workspace_id=workspace_id,
                        db_path=paths.db_path,
                        active_task_checker=self._active_task_checker,
                    )
                    result = self._migrate_locked(workspace_id, confirm)
                    if self._restart_required_marker is not None:
                        self._restart_required_marker(workspace_id)
                    return result
            except WorkspaceOperationConflictError as exc:
                raise WorkspaceVersionServiceError(str(exc)) from exc

    def _migrate_locked(
        self,
        workspace_id: str,
        confirm: str,
    ) -> MigrationResult:
        """在单进程迁移锁内完成读取、备份、DDL、meta 与事件全链。"""

        if confirm != workspace_id:
            raise WorkspaceMigrationConfirmInvalidError(workspace_id)

        # 读 meta.json 拿迁移前状态(同时校验 workspace 存在 + 解析合法)
        workspace_before = self._workspace_service.info(workspace_id)
        from_engine = workspace_before.engine_version
        from_schema = workspace_before.schema_version
        current_engine = ENGINE_VERSION
        current_schema = get_current_schema_version()
        if from_engine == current_engine and from_schema == current_schema:
            raise WorkspaceVersionServiceError(
                f"工作区已经是当前版本，无需重复迁移: {workspace_id}"
            )

        # major 不兼容直接拒绝(v2 跨大版本需专门迁移工具)
        if _compare_major_version(from_engine, current_engine) == _MAJOR_INCOMPATIBLE:
            raise WorkspaceVersionIncompatibleError(
                workspace_id=workspace_id,
                workspace_engine_version=from_engine,
                current_engine_version=current_engine,
            )

        paths = WorkspacePaths(
            workspace_id,
            workspaces_root=self._workspace_service.workspaces_root,
        )
        # 先只读验真 SQLite 的实际 revision/shape，再据此创建来源版本诚实的
        # 备份；任何 stamp/upgrade/meta 写入仍严格发生在备份成功之后。
        database_plan = self._prepare_database_for_migration(
            workspace_id=workspace_id,
            db_path=paths.db_path,
            from_schema=from_schema,
            current_schema=current_schema,
        )

        # pre-migration 备份(失败 → migrate 不开始,保证 rollback 后路)
        try:
            backup_info = self._backup_service.create_migration_backup(
                workspace_id,
                source_schema_version=database_plan.backup_schema_version,
                source_engine_version=from_engine,
            )
        except BackupServiceError as exc:
            raise WorkspaceMigrationBackupFailedError(
                workspace_id=workspace_id,
                reason=f"{type(exc).__name__}: {exc}",
            ) from exc
        except Exception as exc:  # noqa: BLE001
            raise WorkspaceMigrationBackupFailedError(
                workspace_id=workspace_id,
                reason=f"{type(exc).__name__}: {exc}",
            ) from exc

        # 真产品历史库可能由 Base.create_all 初始化而没有 alembic_version。
        # 备份完成后才允许对精确 0029/0030 shape 补 stamp，随后正常 upgrade。
        if database_plan.upgrade_required:
            if database_plan.stamp_revision is None:
                self._run_alembic_upgrade(paths.db_path)
            else:
                self._run_alembic_migration(
                    paths.db_path,
                    stamp_revision=database_plan.stamp_revision,
                )

        self._validate_database_at_current_head(
            workspace_id=workspace_id,
            db_path=paths.db_path,
            current_schema=current_schema,
        )

        # 落 meta.json 新 engine_version + schema_version
        workspace_after = workspace_before.model_copy(
            update={
                "engine_version": current_engine,
                "schema_version": current_schema,
            }
        )
        write_meta(paths.meta_json, workspace_after)

        result = MigrationResult(
            workspace_id=workspace_id,
            pre_migration_backup_id=backup_info.backup_id,
            from_engine_version=from_engine,
            to_engine_version=current_engine,
            from_schema_version=from_schema,
            to_schema_version=current_schema,
        )

        self._publish_workspace_event(
            workspace_id=workspace_id,
            event="version_migrated",
            data={
                "workspace_id": workspace_id,
                "pre_migration_backup_id": backup_info.backup_id,
                "from_engine_version": from_engine,
                "to_engine_version": current_engine,
                "from_schema_version": from_schema,
                "to_schema_version": current_schema,
            },
        )
        _log.info(
            "workspace_migrated",
            workspace_id=workspace_id,
            pre_migration_backup_id=backup_info.backup_id,
            from_engine_version=from_engine,
            to_engine_version=current_engine,
            from_schema_version=from_schema,
            to_schema_version=current_schema,
        )
        return result

    # ---- 内部辅助 ---------------------------------------------------------

    def _prepare_database_for_migration(
        self,
        *,
        workspace_id: str,
        db_path: Path,
        from_schema: str | None,
        current_schema: str,
    ) -> _MigrationDatabasePlan:
        """验真历史库并返回 stamp/upgrade 的最小动作计划。

        若 Alembic 已到当前 head、但 meta 仍停在直接前序版本，说明上一次
        迁移可能在 DDL 提交后、meta 落盘前崩溃。只有数据库结构与当前 ORM
        完全同形时才允许跳过重复 DDL并只补 meta。
        """

        try:
            snapshot = _read_sqlite_schema_snapshot(db_path)
            if not snapshot.business_columns and not (
                snapshot.has_alembic_version_table
            ):
                raise RuntimeError(
                    "现存待迁移工作区数据库为空；这不是 runtime epoch 冻结的"
                    "首次初始化路径，拒绝以迁移重建"
                )
            _validate_database_workspace_identity(
                db_path,
                expected_workspace_id=workspace_id,
            )
            if snapshot.has_alembic_version_table:
                if snapshot.alembic_revision == from_schema:
                    if from_schema == _R8_LEASE_PREVIOUS_SCHEMA:
                        predecessor_mismatches = _schema_shape_mismatches(
                            snapshot,
                            include_operation_lease=False,
                        )
                        if predecessor_mismatches:
                            current_mismatches = _schema_shape_mismatches(
                                snapshot,
                                include_operation_lease=True,
                            )
                            if current_mismatches:
                                raise RuntimeError(
                                    "0029 数据库升级前结构不匹配，且不是"
                                    "0030 DDL/stamp 崩溃中点: previous="
                                    + "; ".join(predecessor_mismatches)
                                    + " current="
                                    + "; ".join(current_mismatches)
                                )
                    elif snapshot.integrity_check != ("ok",) or (
                        snapshot.foreign_key_violations
                    ):
                        raise RuntimeError(
                            "历史数据库完整性检查失败: "
                            f"integrity={snapshot.integrity_check!r} "
                            "foreign_keys="
                            f"{snapshot.foreign_key_violations!r}"
                        )
                    return _MigrationDatabasePlan(
                        stamp_revision=None,
                        upgrade_required=True,
                        backup_schema_version=snapshot.alembic_revision,
                    )
                if (
                    from_schema == _R8_LEASE_PREVIOUS_SCHEMA
                    and snapshot.alembic_revision == current_schema
                ):
                    mismatches = _schema_shape_mismatches(
                        snapshot,
                        include_operation_lease=True,
                    )
                    if mismatches:
                        raise RuntimeError(
                            "数据库 revision 已到当前 head，但结构不匹配: "
                            + "; ".join(mismatches)
                        )
                    return _MigrationDatabasePlan(
                        stamp_revision=None,
                        upgrade_required=False,
                        backup_schema_version=snapshot.alembic_revision,
                    )
                raise RuntimeError(
                    "meta schema 与 alembic_version 不一致: "
                    f"meta={from_schema!r}, db={snapshot.alembic_revision!r}"
                )
            if from_schema != _R8_LEASE_PREVIOUS_SCHEMA:
                raise RuntimeError(
                    "现存业务库没有 alembic_version，且 meta 不是受支持的"
                    f"直接前序 {_R8_LEASE_PREVIOUS_SCHEMA!r}: {from_schema!r}"
                )

            includes_lease = _R8_LEASE_TABLE in snapshot.business_columns
            mismatches = _schema_shape_mismatches(
                snapshot,
                include_operation_lease=includes_lease,
                allow_legacy_create_all_defaults=not includes_lease,
            )
            if mismatches:
                raise RuntimeError("; ".join(mismatches))
            _validate_database_workspace_identity(
                db_path,
                expected_workspace_id=workspace_id,
            )
        except Exception as exc:  # noqa: BLE001
            raise WorkspaceVersionServiceError(
                "工作区迁移前 SQLite 结构验真失败: "
                f"workspace={workspace_id} reason={type(exc).__name__}: {exc}"
            ) from exc

        return _MigrationDatabasePlan(
            stamp_revision=_R8_LEASE_PREVIOUS_SCHEMA,
            upgrade_required=True,
            # 无 stamp 但已是 exact current shape 时，归档必须按真实结构标
            # current；restore 后 current meta + unversioned current DB 可被
            # startup admission 精确验真。标成 0029 会制造不可回滚假版本。
            backup_schema_version=(current_schema if includes_lease else from_schema),
        )

    @staticmethod
    def _validate_database_at_current_head(
        *,
        workspace_id: str,
        db_path: Path,
        current_schema: str,
    ) -> None:
        """DDL 完成后、meta 写入前复核 current revision 与完整 ORM contract。"""

        try:
            snapshot = _read_sqlite_schema_snapshot(db_path)
            if (
                not snapshot.has_alembic_version_table
                or snapshot.alembic_revision != current_schema
            ):
                raise RuntimeError(
                    "迁移后 alembic revision 不匹配: "
                    f"actual={snapshot.alembic_revision!r} "
                    f"expected={current_schema!r}"
                )
            mismatches = _schema_shape_mismatches(
                snapshot,
                include_operation_lease=True,
            )
            if mismatches:
                raise RuntimeError("; ".join(mismatches))
            _validate_database_workspace_identity(
                db_path,
                expected_workspace_id=workspace_id,
            )
        except Exception as exc:  # noqa: BLE001
            raise WorkspaceVersionServiceError(
                "工作区迁移后 SQLite 结构验真失败，meta 未更新: "
                f"workspace={workspace_id} reason={type(exc).__name__}: {exc}"
            ) from exc

    @staticmethod
    def _run_alembic_migration(
        db_path: Path,
        *,
        stamp_revision: str | None,
    ) -> None:
        """同一进程锁和同一目标环境内按需 stamp，再 upgrade head。"""

        from alembic import command
        from alembic.config import Config

        repo_root = Path(__file__).resolve().parents[2]
        cfg = Config(str(repo_root / "alembic.ini"))
        cfg.set_main_option("script_location", str(repo_root / "alembic"))

        with _ALEMBIC_MIGRATION_LOCK:
            previous_db_path = os.environ.get("NOVEL_DB_PATH")
            os.environ["NOVEL_DB_PATH"] = str(db_path)
            try:
                if stamp_revision is not None:
                    command.stamp(cfg, stamp_revision)
                command.upgrade(cfg, "head")
            finally:
                if previous_db_path is None:
                    os.environ.pop("NOVEL_DB_PATH", None)
                else:
                    os.environ["NOVEL_DB_PATH"] = previous_db_path

    @staticmethod
    def _run_alembic_upgrade(db_path: Path) -> None:
        """对已有 revision 或空库程序化跑 ``alembic upgrade head``。

        ``alembic/env.py`` 强制走 ``NOVEL_DB_PATH`` 环境变量解析目标 db(P1-B-16
        既有契约)。本辅助通过 ``os.environ`` 临时设置 + ``try/finally`` 还原
        原值,避免污染并发请求 / 测试环境。

        ``Config.set_main_option("script_location", ...)`` 显式设置以兼容
        worktree / 测试 cwd(对齐 ``tests/db/test_p4_b_01_metrics.py``
        ``_alembic_config`` 模式)。
        """
        WorkspaceVersionService._run_alembic_migration(
            db_path,
            stamp_revision=None,
        )

    def _publish_workspace_event(
        self,
        *,
        workspace_id: str,
        event: str,
        data: dict[str, Any],
    ) -> None:
        """WS publish 到 ``workspace.{workspace_id}.events``;失败 swallow + log。

        对齐 ``BackupService._publish_workspace_event`` 同型(structlog
        ``event`` 关键字冲突 → 业务 event 改名 ``ws_event``)。
        """
        if self._ws_manager is None:
            return
        try:
            self._ws_manager.publish(  # type: ignore[attr-defined]
                channel=workspace_events_channel(workspace_id),
                event=event,
                data=data,
            )
        except Exception as exc:  # noqa: BLE001
            _log.error(
                "version_migrated_event_notify_failed",
                workspace_id=workspace_id,
                ws_event=event,
                error_type=type(exc).__name__,
                error_message=str(exc),
            )


# =============================================================================
# 启动 hook
# =============================================================================


def run_workspace_version_startup_scan(
    workspace_service: WorkspaceService,
) -> dict[str, int]:
    """扫所有工作区 meta.json,版本不匹配落 ``workspace_version_mismatch`` warn。

    P6-B-08 启动 hook 入口(由 ``src/api/main.py`` lifespan 调)。**不主动迁移**
    — 仅落结构化告警事件,前端 / CLI doctor 路径显式触发 ``:migrate`` 才迁移
    (避免意外覆盖)。

    返聚合统计 dict (``scanned`` / ``matched`` / ``mismatched`` / ``errors``)
    供 lifespan 落顶层 ``workspace_version_startup_scan_complete`` info 事件。
    """
    current_engine = ENGINE_VERSION
    try:
        current_schema = get_current_schema_version()
    except Exception as exc:  # noqa: BLE001
        _log.error(
            "workspace_version_startup_scan_schema_load_failed",
            error_type=type(exc).__name__,
            error_message=str(exc),
        )
        current_schema = "unknown"

    scanned = 0
    matched = 0
    mismatched = 0
    errors = 0
    for workspace in workspace_service.list():
        scanned += 1
        ws_engine = workspace.engine_version
        ws_schema = workspace.schema_version
        if ws_engine == current_engine and ws_schema == current_schema:
            matched += 1
            continue
        mismatched += 1
        _log.warning(
            "workspace_version_mismatch",
            workspace_id=workspace.workspace_id,
            workspace_engine=ws_engine,
            current_engine=current_engine,
            workspace_schema=ws_schema,
            current_schema=current_schema,
            engine_match=ws_engine == current_engine,
            schema_match=ws_schema == current_schema,
        )
    _ = errors  # 占位:meta.json IO 异常已由 workspace_service.list() 静默跳过

    return {
        "scanned": scanned,
        "matched": matched,
        "mismatched": mismatched,
    }


_MajorCompareResult = Literal["compatible", "incompatible"]
_ = _MajorCompareResult  # 占位防 ruff F401(供后续 typing 引用)


__all__ = [
    "MigrationResult",
    "VersionStatus",
    "WorkspaceMigrationBackupFailedError",
    "WorkspaceMigrationConfirmInvalidError",
    "WorkspaceVersionIncompatibleError",
    "WorkspaceStartupRecoveryAdmissionError",
    "WorkspaceVersionService",
    "WorkspaceVersionServiceError",
    "admit_workspaces_for_startup_recovery",
    "get_current_schema_version",
    "run_workspace_version_startup_scan",
    "validate_r8_migration_rollback_database",
    "validate_current_restore_database",
    "validate_workspace_for_runtime_open",
]
