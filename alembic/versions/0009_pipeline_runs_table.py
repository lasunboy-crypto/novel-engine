"""P6-B-03 pipeline state SQLite 持久化:新建 ``pipeline_runs`` 表。

对齐 docs/plan/p6_plan.md §"P6-B-03"字面 + plan-kickoff 决策 4=B SQLite
持久化半 + P6-B-02 ``PipelineRunSnapshot`` 字段一一映射(避免 P6-B-03
重命名漂移)。

Revision ID: 0009_pipeline_runs_table
Revises: 0008_chapter_invalidated_force_pass
Create Date: 2026-05-13

设计要点:

1. **``pipeline_runs`` 新表 15 列**(对齐 P6-B-02 ``PipelineRunSnapshot``
   Pydantic 字段):
   - ``run_id`` PK / ``workspace_id`` FK CASCADE
   - ``state`` CHECK 5 档(``idle/running/paused/stopped/error``)
   - ``scope`` CHECK 4 档(``single/range/volume/until``)
   - ``start_chapter_id`` / ``end_chapter_id`` / ``volume_id`` /
     ``profile_override`` Nullable(scope 字段映射)
   - ``chapter_ids: JSON`` / ``completed_chapter_ids: JSON`` 非空(resolved
     chapter 列表与累计完成列表)
   - ``current_chapter`` Nullable / ``pause_reason`` Nullable /
     ``error_details: JSON`` Nullable
   - ``created_at`` / ``updated_at`` 非空 / ``completed_at`` Nullable
     (state=stopped 时落 timestamp)

2. **CHECK 约束 2 条**:
   - ``ck_pipeline_runs_state_5_levels``:state IN 5 档
   - ``ck_pipeline_runs_scope_4_levels``:scope IN 4 档
   - 单一真相源:``PIPELINE_STATE_VALUES`` / ``PIPELINE_SCOPE_VALUES``
     在 ``src/core/db/models.py``,本迁移内嵌字面避免 import 副作用(对齐
     0008 / 0006 / 0005 同模式)。

3. **索引 2 条**:
   - ``ix_pipeline_runs_workspace_id``:list-by-workspace 查询
   - ``ix_pipeline_runs_state``:lifespan 启动 hook 扫 ``state='running'``
     行(全工作区扫,加索引降全表扫成本)

4. **downgrade 反向 drop 表 + 索引**(SQLite drop_table 自动级联索引)。

5. **CLAUDE.md §Alembic head 纪律本任务自检**:Pre-flight grep
   ``command\\.upgrade.*head + command\\.downgrade.*-1`` 全部已在前序任务
   pin 到自身 revision(P5-B-01 0005 / P5-B-02 0006 / P5-B-07 0007 /
   P4-B-01 0004 / P2-B-17 0003 / P2 0002);P4-B-01
   ``test_upgrade_downgrade_upgrade_idempotent`` 验"head 幂等"语义本身,
   P4 表 0004 创建后从未删 + 本迁移仅新建 pipeline_runs 表,**0009 推进
   后无需修改任何旧测试**。
"""
from typing import Sequence, Union

from alembic import op
import sqlalchemy as sa


# revision identifiers, used by Alembic.
revision: str = "0009_pipeline_runs_table"
down_revision: Union[str, Sequence[str], None] = (
    "0008_chapter_invalidated_force_pass"
)
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


_PIPELINE_STATE_VALUES = (
    "idle",
    "running",
    "paused",
    "stopped",
    "error",
)
"""``pipeline_runs.state`` 5 档(对齐 ``src/core/db/models.py``
``PIPELINE_STATE_VALUES`` 单一真相源;迁移内嵌字面避免 import 副作用)。"""


_PIPELINE_SCOPE_VALUES = (
    "single",
    "range",
    "volume",
    "until",
)
"""``pipeline_runs.scope`` 4 档(对齐 ``src/core/db/models.py``
``PIPELINE_SCOPE_VALUES`` 单一真相源;迁移内嵌字面避免 import 副作用)。"""


def _check_sql(column: str, values: tuple[str, ...]) -> str:
    quoted = ", ".join(f"'{v}'" for v in values)
    return f"{column} IN ({quoted})"


def upgrade() -> None:
    """Upgrade schema:新建 ``pipeline_runs`` 表 + 2 索引 + 2 CHECK 约束。"""
    op.create_table(
        "pipeline_runs",
        sa.Column("run_id", sa.String(), nullable=False),
        sa.Column("workspace_id", sa.String(), nullable=False),
        sa.Column("state", sa.String(), nullable=False),
        sa.Column("scope", sa.String(), nullable=False),
        sa.Column("start_chapter_id", sa.String(), nullable=True),
        sa.Column("end_chapter_id", sa.String(), nullable=True),
        sa.Column("volume_id", sa.String(), nullable=True),
        sa.Column("profile_override", sa.Text(), nullable=True),
        sa.Column("chapter_ids", sa.JSON(), nullable=False),
        sa.Column("current_chapter", sa.String(), nullable=True),
        sa.Column("completed_chapter_ids", sa.JSON(), nullable=False),
        sa.Column("pause_reason", sa.Text(), nullable=True),
        sa.Column("error_details", sa.JSON(), nullable=True),
        sa.Column(
            "created_at", sa.DateTime(timezone=True), nullable=False
        ),
        sa.Column(
            "updated_at", sa.DateTime(timezone=True), nullable=False
        ),
        sa.Column(
            "completed_at", sa.DateTime(timezone=True), nullable=True
        ),
        sa.ForeignKeyConstraint(
            ["workspace_id"],
            ["workspaces.workspace_id"],
            ondelete="CASCADE",
        ),
        sa.PrimaryKeyConstraint("run_id"),
        sa.CheckConstraint(
            _check_sql("state", _PIPELINE_STATE_VALUES),
            name="ck_pipeline_runs_state_5_levels",
        ),
        sa.CheckConstraint(
            _check_sql("scope", _PIPELINE_SCOPE_VALUES),
            name="ck_pipeline_runs_scope_4_levels",
        ),
    )
    op.create_index(
        "ix_pipeline_runs_workspace_id",
        "pipeline_runs",
        ["workspace_id"],
    )
    op.create_index(
        "ix_pipeline_runs_state",
        "pipeline_runs",
        ["state"],
    )


def downgrade() -> None:
    """Downgrade schema:drop ``pipeline_runs`` 表(索引自动级联)。"""
    op.drop_index("ix_pipeline_runs_state", table_name="pipeline_runs")
    op.drop_index(
        "ix_pipeline_runs_workspace_id", table_name="pipeline_runs"
    )
    op.drop_table("pipeline_runs")
