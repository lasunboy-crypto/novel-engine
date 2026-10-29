"""W3.5-R5:版本投影回填与跨进程 attempt 权威账本。

Revision ID: 0028_persistent_attempt_ledger
Revises: 0027_continuity_ledger_provenance
Create Date: 2026-07-31
"""

from typing import Sequence, Union

import sqlalchemy as sa
from alembic import op


revision: str = "0028_persistent_attempt_ledger"
down_revision: Union[str, Sequence[str], None] = (
    "0027_continuity_ledger_provenance"
)
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def upgrade() -> None:
    """创建 attempt ledger、run 投影，并从历史版本回填章节投影。"""

    with op.batch_alter_table("pipeline_runs", schema=None) as batch_op:
        batch_op.add_column(
            sa.Column(
                "provider_call_cap",
                sa.Integer(),
                nullable=False,
                server_default=sa.text("1000"),
            )
        )
        batch_op.add_column(
            sa.Column(
                "provider_call_count",
                sa.Integer(),
                nullable=False,
                server_default=sa.text("0"),
            )
        )
        batch_op.add_column(
            sa.Column(
                "transport_retry_count",
                sa.Integer(),
                nullable=False,
                server_default=sa.text("0"),
            )
        )
        batch_op.add_column(
            sa.Column(
                "pipeline_attempt_count",
                sa.Integer(),
                nullable=False,
                server_default=sa.text("0"),
            )
        )
        batch_op.add_column(
            sa.Column(
                "upstream_generation_attempt_count",
                sa.Integer(),
                nullable=False,
                server_default=sa.text("0"),
            )
        )

    op.create_table(
        "attempt_ledger",
        sa.Column("attempt_id", sa.String(), nullable=False),
        sa.Column("workspace_id", sa.String(), nullable=False),
        sa.Column("run_id", sa.String(), nullable=True),
        sa.Column("task_id", sa.String(), nullable=False),
        sa.Column("chapter_id", sa.String(), nullable=True),
        sa.Column("attempt_kind", sa.String(), nullable=False),
        sa.Column("operation_kind", sa.String(), nullable=False),
        sa.Column("logical_call_id", sa.String(), nullable=True),
        sa.Column("agent_name", sa.String(), nullable=True),
        sa.Column("provider", sa.String(), nullable=True),
        sa.Column("model", sa.String(), nullable=True),
        sa.Column("attempt_number", sa.Integer(), nullable=False),
        sa.Column("attempt_cap", sa.Integer(), nullable=False),
        sa.Column("status", sa.String(), nullable=False),
        sa.Column("version_before", sa.Integer(), nullable=True),
        sa.Column("version_after", sa.Integer(), nullable=True),
        sa.Column("error_type", sa.String(), nullable=True),
        sa.Column("created_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column("completed_at", sa.DateTime(timezone=True), nullable=True),
        sa.CheckConstraint(
            "attempt_kind IN ('pipeline', 'provider')",
            name="ck_attempt_ledger_kind_2_levels",
        ),
        sa.CheckConstraint(
            "status IN ('started', 'succeeded', 'failed')",
            name="ck_attempt_ledger_status_3_levels",
        ),
        sa.CheckConstraint(
            "attempt_number >= 1",
            name="ck_attempt_ledger_attempt_number_positive",
        ),
        sa.CheckConstraint(
            "attempt_cap >= 1",
            name="ck_attempt_ledger_attempt_cap_positive",
        ),
        sa.PrimaryKeyConstraint("attempt_id"),
    )
    op.create_index(
        "ix_attempt_ledger_workspace_kind_created",
        "attempt_ledger",
        ["workspace_id", "attempt_kind", "created_at"],
        unique=False,
    )
    op.create_index(
        "ix_attempt_ledger_run_kind",
        "attempt_ledger",
        ["run_id", "attempt_kind"],
        unique=False,
    )
    op.create_index(
        "ix_attempt_ledger_chapter_kind",
        "attempt_ledger",
        ["chapter_id", "attempt_kind"],
        unique=False,
    )
    op.create_index(
        "ix_attempt_ledger_task_agent_model",
        "attempt_ledger",
        ["task_id", "agent_name", "model"],
        unique=False,
    )

    # chapter_versions 是历史正文权威；旧 chapters 投影无条件重算，包含
    # F ch-0006 的 7 版本 / 5 rewrite 反证形状。
    op.execute(
        """
        UPDATE chapters
        SET current_version = COALESCE(
                (
                    SELECT MAX(cv.version_id)
                    FROM chapter_versions AS cv
                    WHERE cv.chapter_id = chapters.chapter_id
                ),
                0
            ),
            rewrite_count = (
                SELECT COUNT(*)
                FROM chapter_versions AS cv
                WHERE cv.chapter_id = chapters.chapter_id
                  AND cv.creation_type = 'rewrite'
            ),
            revise_count = (
                SELECT COUNT(*)
                FROM chapter_versions AS cv
                WHERE cv.chapter_id = chapters.chapter_id
                  AND cv.creation_type = 'revise'
            )
        """
    )


def downgrade() -> None:
    """移除 R5 attempt 表与 run 展示字段；不逆转已纠正的章节投影。"""

    op.drop_index(
        "ix_attempt_ledger_task_agent_model",
        table_name="attempt_ledger",
    )
    op.drop_index(
        "ix_attempt_ledger_chapter_kind",
        table_name="attempt_ledger",
    )
    op.drop_index(
        "ix_attempt_ledger_run_kind",
        table_name="attempt_ledger",
    )
    op.drop_index(
        "ix_attempt_ledger_workspace_kind_created",
        table_name="attempt_ledger",
    )
    op.drop_table("attempt_ledger")

    with op.batch_alter_table("pipeline_runs", schema=None) as batch_op:
        batch_op.drop_column("upstream_generation_attempt_count")
        batch_op.drop_column("pipeline_attempt_count")
        batch_op.drop_column("transport_retry_count")
        batch_op.drop_column("provider_call_count")
        batch_op.drop_column("provider_call_cap")
