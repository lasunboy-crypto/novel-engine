"""核心状态 revision、书内时钟与批准提交水位。

Revision ID: 0032_core_state_book_clock
Revises: 0031_plot_thread_origin_expiry
Create Date: 2026-08-22
"""

from typing import Sequence, Union

import sqlalchemy as sa
from alembic import op


revision: str = "0032_core_state_book_clock"
down_revision: Union[str, Sequence[str], None] = "0031_plot_thread_origin_expiry"
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


_CHAPTER_COLUMNS = {
    "core_state_commit_hash",
    "core_state_commit_status",
    "core_state_commit_details",
}
_SUMMARY_COLUMNS = {"core_state_candidates"}


def _column_names(inspector: sa.Inspector, table: str) -> set[str]:
    return {str(column["name"]) for column in inspector.get_columns(table)}


def _require_all_or_none(*, existing: set[str], expected: set[str], label: str) -> bool:
    overlap = existing & expected
    if overlap and overlap != expected:
        raise RuntimeError(
            f"{label} 核心状态列只允许全部存在或全部缺失，实际存在: {sorted(overlap)}"
        )
    return overlap == expected


def upgrade() -> None:
    bind = op.get_bind()
    inspector = sa.inspect(bind)

    chapter_existing = _column_names(inspector, "chapters")
    chapters_ready = _require_all_or_none(
        existing=chapter_existing,
        expected=_CHAPTER_COLUMNS,
        label="chapters",
    )
    if not chapters_ready:
        with op.batch_alter_table("chapters", schema=None) as batch_op:
            batch_op.add_column(
                sa.Column(
                    "core_state_commit_hash",
                    sa.String(length=64),
                    nullable=False,
                    server_default=sa.text("''"),
                )
            )
            batch_op.add_column(
                sa.Column(
                    "core_state_commit_status",
                    sa.String(),
                    nullable=False,
                    server_default=sa.text("'disabled'"),
                )
            )
            batch_op.add_column(
                sa.Column(
                    "core_state_commit_details",
                    sa.JSON(),
                    nullable=False,
                    server_default=sa.text("'{}'"),
                )
            )

    inspector = sa.inspect(bind)
    summary_existing = _column_names(inspector, "chapter_summaries")
    summaries_ready = _require_all_or_none(
        existing=summary_existing,
        expected=_SUMMARY_COLUMNS,
        label="chapter_summaries",
    )
    if not summaries_ready:
        with op.batch_alter_table("chapter_summaries", schema=None) as batch_op:
            batch_op.add_column(
                sa.Column(
                    "core_state_candidates",
                    sa.JSON(),
                    nullable=False,
                    server_default=sa.text("'[]'"),
                )
            )

    inspector = sa.inspect(bind)
    if "core_state_revisions" in inspector.get_table_names():
        expected = {
            "revision_id",
            "workspace_id",
            "state_key",
            "entity_type",
            "entity_id",
            "attribute",
            "value_json",
            "value_text",
            "source_kind",
            "source_chapter_id",
            "source_version_id",
            "source_path",
            "source_text_hash",
            "evidence_quote",
            "valid_from_chapter",
            "valid_to_chapter",
            "supersedes_revision_id",
            "status",
            "change_reason",
            "ledger_hash",
            "created_at",
            "retracted_at",
        }
        actual = _column_names(inspector, "core_state_revisions")
        if actual != expected:
            raise RuntimeError(
                "既有 core_state_revisions 结构与 0032 不一致，已阻止迁移: "
                f"missing={sorted(expected - actual)} extra={sorted(actual - expected)}"
            )
        existing_indexes = {
            str(index["name"]): bool(index.get("unique"))
            for index in inspector.get_indexes("core_state_revisions")
        }
        required_indexes = {
            "ix_core_state_revisions_workspace_state": False,
            "ix_core_state_revisions_workspace_source_chapter": False,
            "uq_core_state_revisions_one_active": True,
        }
        mismatched = {
            name: (existing_indexes.get(name), unique)
            for name, unique in required_indexes.items()
            if name in existing_indexes and existing_indexes[name] != unique
        }
        if mismatched:
            raise RuntimeError(
                f"既有 core_state_revisions 索引唯一性不一致: {mismatched}"
            )
        if "ix_core_state_revisions_workspace_state" not in existing_indexes:
            op.create_index(
                "ix_core_state_revisions_workspace_state",
                "core_state_revisions",
                ["workspace_id", "state_key", "status"],
                unique=False,
            )
        if "ix_core_state_revisions_workspace_source_chapter" not in existing_indexes:
            op.create_index(
                "ix_core_state_revisions_workspace_source_chapter",
                "core_state_revisions",
                ["workspace_id", "source_chapter_id"],
                unique=False,
            )
        if "uq_core_state_revisions_one_active" not in existing_indexes:
            op.create_index(
                "uq_core_state_revisions_one_active",
                "core_state_revisions",
                ["workspace_id", "state_key"],
                unique=True,
                sqlite_where=sa.text("status = 'active'"),
            )
        return

    op.create_table(
        "core_state_revisions",
        sa.Column("revision_id", sa.String(), nullable=False),
        sa.Column("workspace_id", sa.String(), nullable=False),
        sa.Column("state_key", sa.String(), nullable=False),
        sa.Column("entity_type", sa.String(), nullable=False),
        sa.Column("entity_id", sa.String(), nullable=False),
        sa.Column("attribute", sa.String(), nullable=False),
        sa.Column("value_json", sa.JSON(), nullable=False),
        sa.Column("value_text", sa.Text(), nullable=False),
        sa.Column("source_kind", sa.String(), nullable=False),
        sa.Column("source_chapter_id", sa.String(), nullable=False),
        sa.Column("source_version_id", sa.Integer(), nullable=False),
        sa.Column("source_path", sa.Text(), nullable=False),
        sa.Column("source_text_hash", sa.String(length=64), nullable=False),
        sa.Column("evidence_quote", sa.Text(), nullable=False),
        sa.Column("valid_from_chapter", sa.Integer(), nullable=False),
        sa.Column("valid_to_chapter", sa.Integer(), nullable=True),
        sa.Column("supersedes_revision_id", sa.String(), nullable=True),
        sa.Column("status", sa.String(), nullable=False),
        sa.Column("change_reason", sa.Text(), nullable=False),
        sa.Column("ledger_hash", sa.String(length=64), nullable=False),
        sa.Column("created_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column("retracted_at", sa.DateTime(timezone=True), nullable=True),
        sa.CheckConstraint(
            "status IN ('active', 'superseded', 'retracted')",
            name="ck_core_state_revisions_status",
        ),
        sa.CheckConstraint(
            "entity_type IN ('object', 'book_clock')",
            name="ck_core_state_revisions_entity_type",
        ),
        sa.CheckConstraint(
            "source_version_id >= 1",
            name="ck_core_state_revisions_source_version_positive",
        ),
        sa.CheckConstraint(
            "valid_from_chapter >= 1",
            name="ck_core_state_revisions_valid_from_positive",
        ),
        sa.ForeignKeyConstraint(
            ["workspace_id"], ["workspaces.workspace_id"], ondelete="CASCADE"
        ),
        sa.PrimaryKeyConstraint("revision_id"),
    )
    op.create_index(
        "ix_core_state_revisions_workspace_state",
        "core_state_revisions",
        ["workspace_id", "state_key", "status"],
        unique=False,
    )
    op.create_index(
        "ix_core_state_revisions_workspace_source_chapter",
        "core_state_revisions",
        ["workspace_id", "source_chapter_id"],
        unique=False,
    )
    op.create_index(
        "uq_core_state_revisions_one_active",
        "core_state_revisions",
        ["workspace_id", "state_key"],
        unique=True,
        sqlite_where=sa.text("status = 'active'"),
    )


def downgrade() -> None:
    inspector = sa.inspect(op.get_bind())
    if "core_state_revisions" in inspector.get_table_names():
        op.drop_index(
            "uq_core_state_revisions_one_active",
            table_name="core_state_revisions",
        )
        op.drop_index(
            "ix_core_state_revisions_workspace_source_chapter",
            table_name="core_state_revisions",
        )
        op.drop_index(
            "ix_core_state_revisions_workspace_state",
            table_name="core_state_revisions",
        )
        op.drop_table("core_state_revisions")

    inspector = sa.inspect(op.get_bind())
    summary_existing = _column_names(inspector, "chapter_summaries")
    if "core_state_candidates" in summary_existing:
        with op.batch_alter_table("chapter_summaries", schema=None) as batch_op:
            batch_op.drop_column("core_state_candidates")

    inspector = sa.inspect(op.get_bind())
    chapter_existing = _column_names(inspector, "chapters")
    overlap = chapter_existing & _CHAPTER_COLUMNS
    if overlap and overlap != _CHAPTER_COLUMNS:
        raise RuntimeError(
            f"chapters 核心状态列不完整，拒绝破坏性 downgrade: {sorted(overlap)}"
        )
    if overlap:
        with op.batch_alter_table("chapters", schema=None) as batch_op:
            batch_op.drop_column("core_state_commit_details")
            batch_op.drop_column("core_state_commit_status")
            batch_op.drop_column("core_state_commit_hash")
