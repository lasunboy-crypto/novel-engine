"""p5 volumes table:创建 P5 范围 ``volumes`` 表(对应 ``Volume`` Pydantic
模型,PROJECT_SPEC §3 + ROADMAP §P5)。

Revision ID: 0005_volumes_table
Revises: 0004_p4_metrics_tables
Create Date: 2026-05-12

设计要点:

- **Volume 主表 + Arc / TensionCurvePoint JSON 嵌套**(plan-kickoff 决策 2=C):
  ``arcs_json`` / ``tension_curve_json`` 用 ``sa.JSON`` 列(SQLAlchemy 自动
  dict ↔ json.dumps 序列化,对齐 P4-B-01 ``report_json`` 先例);outline
  整体 SSOT 仍在 ``workspaces/<id>/data/outline/volume_plans.json`` 文件,
  DB 是查询缓存。
- ``tension_template`` 4 档 CHECK 约束(对齐 FRONTEND_SPEC §4.2 字面
  "英雄之旅 / 平稳日常 / 强刺激爽文 / 自由"→ 枚举 ``heros_journey /
  steady_daily / strong_stimulation / free``);枚举值由
  ``TENSION_TEMPLATE_VALUES`` 单一真相源提供(``src/core/db/models.py``),
  本迁移字面 IN 列表保持同步(对齐 P4-B-01 ``MEMORY_HEALTH_SEVERITY_VALUES``
  + P2-B-17 ``CHAPTER_STATUS_VALUES`` 模式)。
- ``workspace_id`` FK CASCADE 到 ``workspaces`` 主表(对齐 P1 ``chapters``
  / P2 ``characters`` / ``plot_threads`` FK 模式);Volume 是工作区从属
  实体,工作区删除时连带 cascade。
- ``ix_volumes_workspace_id`` 索引(P5-B-09 ``GET /workspaces/{id}/volumes``
  按 workspace 过滤查询需要)。

注:本迁移文件名沿用 P1/P2/P3/P4 ``00NN_<short>.py`` 命名而非 alembic
默认 hash;``revision`` 字符串与文件名前缀一致(``0005_volumes_table``),
``down_revision`` 指向 ``0004_p4_metrics_tables``。
"""
from typing import Sequence, Union

from alembic import op
import sqlalchemy as sa


# revision identifiers, used by Alembic.
revision: str = "0005_volumes_table"
down_revision: Union[str, Sequence[str], None] = "0004_p4_metrics_tables"
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def upgrade() -> None:
    """Upgrade schema:创建 ``volumes`` 表 + 1 索引 + 1 CHECK + 1 FK。"""
    op.create_table(
        "volumes",
        sa.Column("volume_id", sa.String(), nullable=False),
        sa.Column("workspace_id", sa.String(), nullable=False),
        sa.Column("title", sa.String(), nullable=False),
        sa.Column("summary", sa.Text(), nullable=False),
        sa.Column("chapter_count", sa.Integer(), nullable=False),
        sa.Column("tension_template", sa.String(), nullable=False),
        sa.Column("arcs_json", sa.JSON(), nullable=False),
        sa.Column("tension_curve_json", sa.JSON(), nullable=False),
        sa.Column("created_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column("last_updated_at", sa.DateTime(timezone=True), nullable=False),
        sa.CheckConstraint(
            "tension_template IN ('heros_journey', 'steady_daily', "
            "'strong_stimulation', 'free')",
            name="ck_volumes_tension_template",
        ),
        sa.ForeignKeyConstraint(
            ["workspace_id"], ["workspaces.workspace_id"], ondelete="CASCADE"
        ),
        sa.PrimaryKeyConstraint("volume_id"),
    )
    with op.batch_alter_table("volumes", schema=None) as batch_op:
        batch_op.create_index(
            "ix_volumes_workspace_id", ["workspace_id"], unique=False
        )


def downgrade() -> None:
    """Downgrade schema:drop ``volumes`` 表 + 索引。"""
    with op.batch_alter_table("volumes", schema=None) as batch_op:
        batch_op.drop_index("ix_volumes_workspace_id")
    op.drop_table("volumes")
