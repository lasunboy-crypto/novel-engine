"""PlotThread 大纲主伏笔 / 正文临时伏笔内部来源与休眠策略。

Revision ID: 0031_plot_thread_origin_expiry
Revises: 0030_r8_chapter_operation_leases
Create Date: 2026-08-16
"""

from typing import Sequence, Union

import sqlalchemy as sa
from alembic import op


revision: str = "0031_plot_thread_origin_expiry"
down_revision: Union[str, Sequence[str], None] = (
    "0030_r8_chapter_operation_leases"
)
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


_NEW_COLUMNS = {"origin", "expiry_policy", "source_evidence"}
_ORIGIN_CHECK = "ck_plot_threads_origin"
_EXPIRY_CHECK = "ck_plot_threads_expiry_policy"


def _normalize_default(value: object | None) -> str | None:
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
        normalized = normalized[1:-1]
    return normalized


def _validate_existing_columns(inspector: sa.Inspector) -> None:
    columns = {
        column["name"]: column for column in inspector.get_columns("plot_threads")
    }
    mismatches: list[str] = []
    expected = {
        "origin": (sa.String, False, "outline"),
        "expiry_policy": (sa.String, False, "never"),
        "source_evidence": (sa.Text, True, None),
    }
    for name, (column_type, nullable, default) in expected.items():
        column = columns[name]
        if not isinstance(column["type"], column_type):
            mismatches.append(f"type:{name}")
        if bool(column["nullable"]) is not nullable:
            mismatches.append(f"nullable:{name}")
        if _normalize_default(column.get("default")) != default:
            mismatches.append(f"default:{name}")

    check_names = {
        check.get("name")
        for check in inspector.get_check_constraints("plot_threads")
    }
    for name in (_ORIGIN_CHECK, _EXPIRY_CHECK):
        if name not in check_names:
            mismatches.append(f"check:{name}")
    if mismatches:
        raise RuntimeError(
            "既有 plot_threads 临时伏笔结构与 0031 不一致，已阻止迁移: "
            f"{sorted(mismatches)}"
        )


def upgrade() -> None:
    """保守把历史伏笔视为大纲主伏笔，再增加临时层内部元数据。"""

    inspector = sa.inspect(op.get_bind())
    columns = {
        column["name"] for column in inspector.get_columns("plot_threads")
    }
    existing_new = columns & _NEW_COLUMNS
    if existing_new:
        if existing_new != _NEW_COLUMNS:
            raise RuntimeError(
                "plot_threads 临时伏笔列只允许全部存在或全部缺失，实际存在: "
                f"{sorted(existing_new)}"
            )
        _validate_existing_columns(inspector)
        return

    with op.batch_alter_table("plot_threads", schema=None) as batch_op:
        batch_op.add_column(
            sa.Column(
                "origin",
                sa.String(),
                nullable=False,
                server_default=sa.text("'outline'"),
            )
        )
        batch_op.add_column(
            sa.Column(
                "expiry_policy",
                sa.String(),
                nullable=False,
                server_default=sa.text("'never'"),
            )
        )
        batch_op.add_column(
            sa.Column("source_evidence", sa.Text(), nullable=True)
        )
        batch_op.create_check_constraint(
            _ORIGIN_CHECK,
            "origin IN ('outline', 'emergent', 'manual')",
        )
        batch_op.create_check_constraint(
            _EXPIRY_CHECK,
            "expiry_policy IN ('arc_end', 'volume_end', 'never')",
        )


def downgrade() -> None:
    """移除内部来源/休眠元数据，不删除任何 PlotThread 行。"""

    inspector = sa.inspect(op.get_bind())
    columns = {
        column["name"] for column in inspector.get_columns("plot_threads")
    }
    existing_new = columns & _NEW_COLUMNS
    if not existing_new:
        return
    if existing_new != _NEW_COLUMNS:
        raise RuntimeError(
            "plot_threads 临时伏笔列不完整，拒绝破坏性 downgrade: "
            f"{sorted(existing_new)}"
        )
    with op.batch_alter_table("plot_threads", schema=None) as batch_op:
        batch_op.drop_constraint(_EXPIRY_CHECK, type_="check")
        batch_op.drop_constraint(_ORIGIN_CHECK, type_="check")
        batch_op.drop_column("source_evidence")
        batch_op.drop_column("expiry_policy")
        batch_op.drop_column("origin")
