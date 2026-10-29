"""W3.5-R6:持久化 HARD constraint pass/fail 与回退 ID。

``review_results.constraint_results`` 保存四个 HARD 域逐条结构化结果；
``hard_constraint_regressions`` 只保存同一 constraint ID 的 pass→fail。
旧行升级后均为 ``[]``，不会从自然语言历史字段反解析或伪造结构化事实。

Revision ID: 0029_structured_rewrite_constraint_results
Revises: 0028_persistent_attempt_ledger
Create Date: 2026-07-31
"""

from typing import Sequence, Union

import sqlalchemy as sa
from alembic import op


revision: str = "0029_structured_rewrite_constraint_results"
down_revision: Union[str, Sequence[str], None] = "0028_persistent_attempt_ledger"
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def upgrade() -> None:
    """增加两个非空 JSON 数组列；旧 review 保持未知而不是伪造 pass。"""
    with op.batch_alter_table("review_results", schema=None) as batch_op:
        batch_op.add_column(
            sa.Column(
                "constraint_results",
                sa.JSON(),
                nullable=False,
                server_default=sa.text("'[]'"),
            )
        )
        batch_op.add_column(
            sa.Column(
                "hard_constraint_regressions",
                sa.JSON(),
                nullable=False,
                server_default=sa.text("'[]'"),
            )
        )


def downgrade() -> None:
    """对称移除 R6 两列，不改 R2 violation 或 R5 账本。"""
    with op.batch_alter_table("review_results", schema=None) as batch_op:
        batch_op.drop_column("hard_constraint_regressions")
        batch_op.drop_column("constraint_results")
