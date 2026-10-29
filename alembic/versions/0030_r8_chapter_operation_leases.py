"""R8：章节付费/改源操作持久租约。

Revision ID: 0030_r8_chapter_operation_leases
Revises: 0029_structured_rewrite_constraint_results
Create Date: 2026-08-02
"""

from typing import Sequence, Union

import sqlalchemy as sa
from alembic import op
from sqlalchemy.sql.elements import TextClause


revision: str = "0030_r8_chapter_operation_leases"
down_revision: Union[str, Sequence[str], None] = (
    "0029_structured_rewrite_constraint_results"
)
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


_LEGACY_CREATE_ALL_DEFAULTS: dict[
    str,
    tuple[tuple[str, sa.types.TypeEngine, TextClause, str], ...],
] = {
    "chapter_cards": (("card_status", sa.String(), sa.text("'draft'"), "draft"),),
    "characters": (
        ("identity_facets", sa.JSON(), sa.text("'[]'"), "[]"),
        ("state_timeline", sa.JSON(), sa.text("'[]'"), "[]"),
    ),
    "review_results": (
        ("constraint_violations", sa.JSON(), sa.text("'[]'"), "[]"),
        ("constraint_results", sa.JSON(), sa.text("'[]'"), "[]"),
        (
            "hard_constraint_regressions",
            sa.JSON(),
            sa.text("'[]'"),
            "[]",
        ),
    ),
    "timeline_entries": (("is_flashback", sa.Boolean(), sa.text("0"), "0"),),
}


def _normalize_default(value: object | None) -> str | None:
    """归一化 SQLite reflection 的括号和字符串引号。"""

    if value is None:
        return None
    normalized = str(value).strip()
    while normalized.startswith("(") and normalized.endswith(")"):
        normalized = normalized[1:-1].strip()
    if (
        len(normalized) >= 2
        and normalized[0] == normalized[-1]
        and normalized[0] in {"'", '"'}
    ):
        quote = normalized[0]
        normalized = normalized[1:-1].replace(quote * 2, quote)
    return normalized


def _normalize_legacy_create_all_defaults(
    inspector: sa.Inspector,
) -> None:
    """把真实旧 ``Base.create_all`` 0029 的七项 default 收口为 canonical。

    旧库只可能是七项全部存在，或七项同时缺失。部分缺失代表未知漂移，迁移
    必须 fail closed；上层 service 还会在备份前验真完整表、列、约束与数据。
    """

    missing: set[tuple[str, str]] = set()
    expected = {
        (table_name, column_name)
        for table_name, columns in _LEGACY_CREATE_ALL_DEFAULTS.items()
        for column_name, _, _, _ in columns
    }
    for table_name, columns in _LEGACY_CREATE_ALL_DEFAULTS.items():
        reflected = {
            column["name"]: column for column in inspector.get_columns(table_name)
        }
        for column_name, _, _, expected_default in columns:
            if column_name not in reflected:
                raise RuntimeError(
                    f"0029 legacy default 归一化缺少列: {table_name}.{column_name}"
                )
            actual_default = _normalize_default(reflected[column_name].get("default"))
            if actual_default is None:
                missing.add((table_name, column_name))
            elif actual_default != expected_default:
                raise RuntimeError(
                    "0029 legacy default 归一化遇到未知值: "
                    f"{table_name}.{column_name}={actual_default!r}"
                )

    if not missing:
        return
    if missing != expected:
        raise RuntimeError(
            f"0029 legacy default 只允许七项同时缺失，实际缺失: {sorted(missing)}"
        )

    for table_name, columns in _LEGACY_CREATE_ALL_DEFAULTS.items():
        with op.batch_alter_table(table_name, schema=None) as batch_op:
            for column_name, column_type, default_clause, _ in columns:
                batch_op.alter_column(
                    column_name,
                    existing_type=column_type,
                    existing_nullable=False,
                    server_default=default_clause,
                )


def _validate_existing_table(inspector: sa.Inspector) -> None:
    """确认 ``create_all`` 预建表与 0030 完全同形，否则 fail closed。"""

    required_columns: dict[str, tuple[type[sa.types.TypeEngine], bool]] = {
        "workspace_id": (sa.String, False),
        "chapter_id": (sa.String, False),
        "lease_id": (sa.String, False),
        "operation_kind": (sa.String, False),
        "source_version": (sa.Integer, False),
        "task_id": (sa.String, False),
        "run_id": (sa.String, True),
        "phase": (sa.String, False),
        "created_at": (sa.DateTime, False),
        "updated_at": (sa.DateTime, False),
    }
    columns = {
        column["name"]: column
        for column in inspector.get_columns("chapter_operation_leases")
    }
    mismatches: list[str] = []
    if set(columns) != set(required_columns):
        mismatches.append("columns")
    else:
        for name, (expected_type, expected_nullable) in required_columns.items():
            column = columns[name]
            if not isinstance(column["type"], expected_type):
                mismatches.append(f"type:{name}")
            if bool(column["nullable"]) is not expected_nullable:
                mismatches.append(f"nullable:{name}")
            if column.get("default") is not None:
                mismatches.append(f"default:{name}")

    primary_key = inspector.get_pk_constraint("chapter_operation_leases")
    if primary_key.get("constrained_columns") != [
        "workspace_id",
        "chapter_id",
    ]:
        mismatches.append("primary_key")

    unique_columns = {
        tuple(constraint.get("column_names") or [])
        for constraint in inspector.get_unique_constraints("chapter_operation_leases")
    }
    if unique_columns != {("lease_id",)}:
        mismatches.append("unique_constraints")

    foreign_keys = inspector.get_foreign_keys("chapter_operation_leases")
    expected_foreign_key = {
        "constrained_columns": ["workspace_id"],
        "referred_table": "workspaces",
        "referred_columns": ["workspace_id"],
    }
    if len(foreign_keys) != 1:
        mismatches.append("foreign_keys")
    else:
        foreign_key = foreign_keys[0]
        if any(
            foreign_key.get(key) != value for key, value in expected_foreign_key.items()
        ) or str(foreign_key.get("options", {}).get("ondelete", "")).upper() != (
            "CASCADE"
        ):
            mismatches.append("foreign_keys")

    indexes = {
        (
            index.get("name"),
            tuple(index.get("column_names") or []),
            bool(index.get("unique")),
        )
        for index in inspector.get_indexes("chapter_operation_leases")
    }
    if indexes != {("ix_chapter_operation_leases_task_id", ("task_id",), False)}:
        mismatches.append("indexes")
    if inspector.get_check_constraints("chapter_operation_leases"):
        mismatches.append("check_constraints")

    if mismatches:
        raise RuntimeError(
            "既有 chapter_operation_leases 结构与 0030 不一致，"
            f"已阻止迁移: {sorted(set(mismatches))}"
        )


def upgrade() -> None:
    """创建每工作区、每章节唯一的 crash-persistent 操作租约。"""

    bind = op.get_bind()
    inspector = sa.inspect(bind)
    _normalize_legacy_create_all_defaults(inspector)
    inspector = sa.inspect(bind)
    if "chapter_operation_leases" in inspector.get_table_names():
        _validate_existing_table(inspector)
        return

    op.create_table(
        "chapter_operation_leases",
        sa.Column("workspace_id", sa.String(), nullable=False),
        sa.Column("chapter_id", sa.String(), nullable=False),
        sa.Column("lease_id", sa.String(), nullable=False),
        sa.Column("operation_kind", sa.String(), nullable=False),
        sa.Column("source_version", sa.Integer(), nullable=False),
        sa.Column("task_id", sa.String(), nullable=False),
        sa.Column("run_id", sa.String(), nullable=True),
        sa.Column("phase", sa.String(), nullable=False),
        sa.Column("created_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column("updated_at", sa.DateTime(timezone=True), nullable=False),
        sa.ForeignKeyConstraint(
            ["workspace_id"],
            ["workspaces.workspace_id"],
            ondelete="CASCADE",
        ),
        sa.PrimaryKeyConstraint("workspace_id", "chapter_id"),
        sa.UniqueConstraint("lease_id", name="uq_chapter_operation_lease_id"),
    )
    op.create_index(
        "ix_chapter_operation_leases_task_id",
        "chapter_operation_leases",
        ["task_id"],
        unique=False,
    )


def downgrade() -> None:
    """移除 R8 操作租约，不触碰章节、attempt 或正文账本。"""

    inspector = sa.inspect(op.get_bind())
    if "chapter_operation_leases" in inspector.get_table_names():
        indexes = {
            index["name"] for index in inspector.get_indexes("chapter_operation_leases")
        }
        if "ix_chapter_operation_leases_task_id" in indexes:
            op.drop_index(
                "ix_chapter_operation_leases_task_id",
                table_name="chapter_operation_leases",
            )
        op.drop_table("chapter_operation_leases")
