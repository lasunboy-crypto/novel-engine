"""W3.5-R4:连续性小账本硬水位与逐条原文 provenance。

在既有 ``chapter_summaries`` 上补齐同步小账本字段。旧行使用空值/0 回填，
只能作为历史摘要读取；未通过 R4 校验的旧行不能设置下一章 ready 水位。

Revision ID: 0027_continuity_ledger_provenance
Revises: 0026_review_results_constraint_violations
Create Date: 2026-07-31
"""

from typing import Sequence, Union

import sqlalchemy as sa
from alembic import op


revision: str = "0027_continuity_ledger_provenance"
down_revision: Union[str, Sequence[str], None] = (
    "0026_review_results_constraint_violations"
)
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def upgrade() -> None:
    """给 ``chapter_summaries`` 增加 R4 事实、来源与校验列。"""

    with op.batch_alter_table("chapter_summaries", schema=None) as batch_op:
        batch_op.add_column(
            sa.Column(
                "character_states",
                sa.JSON(),
                nullable=False,
                server_default=sa.text("'[]'"),
            )
        )
        batch_op.add_column(
            sa.Column(
                "established_facts",
                sa.JSON(),
                nullable=False,
                server_default=sa.text("'[]'"),
            )
        )
        batch_op.add_column(
            sa.Column(
                "revealed_mechanics",
                sa.JSON(),
                nullable=False,
                server_default=sa.text("'[]'"),
            )
        )
        batch_op.add_column(
            sa.Column(
                "raw_final_tail",
                sa.Text(),
                nullable=False,
                server_default="",
            )
        )
        batch_op.add_column(
            sa.Column(
                "source_version_id",
                sa.Integer(),
                nullable=False,
                server_default="0",
            )
        )
        batch_op.add_column(
            sa.Column(
                "source_text_hash",
                sa.String(length=64),
                nullable=False,
                server_default="",
            )
        )
        batch_op.add_column(
            sa.Column(
                "evidence_records",
                sa.JSON(),
                nullable=False,
                server_default=sa.text("'[]'"),
            )
        )
        batch_op.add_column(
            sa.Column(
                "ledger_hash",
                sa.String(length=64),
                nullable=False,
                server_default="",
            )
        )
        batch_op.add_column(
            sa.Column(
                "validated_at",
                sa.DateTime(timezone=True),
                nullable=True,
            )
        )


def downgrade() -> None:
    """对称移除 R4 连续性小账本列。"""

    with op.batch_alter_table("chapter_summaries", schema=None) as batch_op:
        batch_op.drop_column("validated_at")
        batch_op.drop_column("ledger_hash")
        batch_op.drop_column("evidence_records")
        batch_op.drop_column("source_text_hash")
        batch_op.drop_column("source_version_id")
        batch_op.drop_column("raw_final_tail")
        batch_op.drop_column("revealed_mechanics")
        batch_op.drop_column("established_facts")
        batch_op.drop_column("character_states")
