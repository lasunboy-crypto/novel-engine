"""Provider 物理请求计量、币种与终态。

Revision ID: 0033_provider_attempt_outcomes
Revises: 0032_core_state_book_clock
Create Date: 2026-08-25

历史 attempt 必须保持未知：本迁移只追加 nullable 列与约束，不做 UPDATE、
不猜 token、币种、request id 或响应终态。
"""

from typing import Sequence, Union

import sqlalchemy as sa
from alembic import op


revision: str = "0033_provider_attempt_outcomes"
down_revision: Union[str, Sequence[str], None] = "0032_core_state_book_clock"
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


_OUTCOME_COLUMNS = {
    "usage_status",
    "input_tokens",
    "output_tokens",
    "reserved_input_tokens",
    "reserved_output_tokens",
    "configured_cost_currency",
    "configured_input_cost_per_1k",
    "configured_output_cost_per_1k",
    "configured_cost_amount",
    "configured_cost_basis",
    "provider_request_id",
    "response_status",
}


def _column_names() -> set[str]:
    return {
        str(column["name"])
        for column in sa.inspect(op.get_bind()).get_columns("attempt_ledger")
    }


def _require_all_or_none(existing: set[str]) -> bool:
    overlap = existing & _OUTCOME_COLUMNS
    if overlap and overlap != _OUTCOME_COLUMNS:
        raise RuntimeError(
            "attempt_ledger 0033 列只允许全部存在或全部缺失，实际存在: "
            f"{sorted(overlap)}"
        )
    return overlap == _OUTCOME_COLUMNS


def upgrade() -> None:
    if _require_all_or_none(_column_names()):
        return

    with op.batch_alter_table("attempt_ledger", schema=None) as batch_op:
        batch_op.add_column(sa.Column("usage_status", sa.String(), nullable=True))
        batch_op.add_column(sa.Column("input_tokens", sa.Integer(), nullable=True))
        batch_op.add_column(sa.Column("output_tokens", sa.Integer(), nullable=True))
        batch_op.add_column(
            sa.Column("reserved_input_tokens", sa.Integer(), nullable=True)
        )
        batch_op.add_column(
            sa.Column("reserved_output_tokens", sa.Integer(), nullable=True)
        )
        batch_op.add_column(
            sa.Column("configured_cost_currency", sa.String(length=3), nullable=True)
        )
        batch_op.add_column(
            sa.Column("configured_input_cost_per_1k", sa.Float(), nullable=True)
        )
        batch_op.add_column(
            sa.Column("configured_output_cost_per_1k", sa.Float(), nullable=True)
        )
        batch_op.add_column(
            sa.Column("configured_cost_amount", sa.Float(), nullable=True)
        )
        batch_op.add_column(
            sa.Column("configured_cost_basis", sa.String(), nullable=True)
        )
        batch_op.add_column(
            sa.Column("provider_request_id", sa.String(length=256), nullable=True)
        )
        batch_op.add_column(sa.Column("response_status", sa.String(), nullable=True))
        batch_op.create_check_constraint(
            "ck_attempt_ledger_usage_status_2_levels",
            "usage_status IS NULL OR usage_status IN ('known', 'unknown')",
        )
        batch_op.create_check_constraint(
            "ck_attempt_ledger_response_status_5_levels",
            "response_status IS NULL OR response_status IN "
            "('request_started', 'completed', 'failed', 'stream_incomplete', "
            "'usage_missing')",
        )
        batch_op.create_check_constraint(
            "ck_attempt_ledger_cost_basis_2_levels",
            "configured_cost_basis IS NULL OR configured_cost_basis IN "
            "('conservative_reserve', 'metered_usage')",
        )
        for name in ("input_tokens", "output_tokens", "reserved_input_tokens", "reserved_output_tokens"):
            batch_op.create_check_constraint(
                f"ck_attempt_ledger_{name}_nonnegative",
                f"{name} IS NULL OR {name} >= 0",
            )
        batch_op.create_check_constraint(
            "ck_attempt_ledger_input_rate_nonnegative",
            "configured_input_cost_per_1k IS NULL OR "
            "configured_input_cost_per_1k >= 0",
        )
        batch_op.create_check_constraint(
            "ck_attempt_ledger_output_rate_nonnegative",
            "configured_output_cost_per_1k IS NULL OR "
            "configured_output_cost_per_1k >= 0",
        )
        batch_op.create_check_constraint(
            "ck_attempt_ledger_cost_amount_nonnegative",
            "configured_cost_amount IS NULL OR configured_cost_amount >= 0",
        )
        batch_op.create_check_constraint(
            "ck_attempt_ledger_known_usage_has_tokens",
            "usage_status != 'known' OR "
            "(input_tokens IS NOT NULL AND output_tokens IS NOT NULL)",
        )


def downgrade() -> None:
    existing = _column_names()
    if not (existing & _OUTCOME_COLUMNS):
        return
    if not _require_all_or_none(existing):  # pragma: no cover - raises on partial state
        return

    with op.batch_alter_table("attempt_ledger", schema=None) as batch_op:
        batch_op.drop_constraint(
            "ck_attempt_ledger_known_usage_has_tokens", type_="check"
        )
        batch_op.drop_constraint(
            "ck_attempt_ledger_cost_amount_nonnegative", type_="check"
        )
        batch_op.drop_constraint(
            "ck_attempt_ledger_output_rate_nonnegative", type_="check"
        )
        batch_op.drop_constraint(
            "ck_attempt_ledger_input_rate_nonnegative", type_="check"
        )
        for name in reversed(
            ("input_tokens", "output_tokens", "reserved_input_tokens", "reserved_output_tokens")
        ):
            batch_op.drop_constraint(
                f"ck_attempt_ledger_{name}_nonnegative", type_="check"
            )
        batch_op.drop_constraint(
            "ck_attempt_ledger_cost_basis_2_levels", type_="check"
        )
        batch_op.drop_constraint(
            "ck_attempt_ledger_response_status_5_levels", type_="check"
        )
        batch_op.drop_constraint(
            "ck_attempt_ledger_usage_status_2_levels", type_="check"
        )
        for name in reversed(tuple(_OUTCOME_COLUMNS)):
            batch_op.drop_column(name)
