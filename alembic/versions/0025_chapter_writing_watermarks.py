"""W3.5-C:章节写作水位拆分字段。

对齐 ``docs/plan/v2_w3_5_pipeline_architecture_refactor_plan.md`` 任务 C:

1. ``ready_for_next_chapter_at``:硬水位。章节终稿已落盘、状态已 approved、
   下一章可读取上章 final.md + chapter card 继续推进时写入。
2. ``memory_enrichment_status`` / ``memory_enriched_at`` /
   ``memory_enrichment_details``:富记忆后台加工状态。摘要、向量、语义标签、
   指标二次归档等失败时记录为 ``failed`` + details,不再静默表现为"没有记忆"。

AGENTS.md 迁移自检:本任务新增 head 前已 grep 旧测试中
``command.upgrade.*head`` / ``downgrade -1`` 模式;历史自测均已 pin 自身
revision 或属于单独 upgrade head 夹具。本迁移只给 ``chapters`` 加列,
不改旧 CHECK 约束。
"""

from typing import Sequence, Union

import sqlalchemy as sa
from alembic import op


# revision identifiers, used by Alembic.
revision: str = "0025_chapter_writing_watermarks"
down_revision: Union[str, Sequence[str], None] = (
    "0024_pipeline_runs_state_interrupted"
)
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def upgrade() -> None:
    """Upgrade schema:给 ``chapters`` 增加写作水位与富记忆状态列。"""
    with op.batch_alter_table("chapters", schema=None) as batch_op:
        batch_op.add_column(
            sa.Column(
                "ready_for_next_chapter_at",
                sa.DateTime(timezone=True),
                nullable=True,
            )
        )
        batch_op.add_column(
            sa.Column(
                "memory_enrichment_status",
                sa.String(),
                nullable=False,
                server_default="pending",
            )
        )
        batch_op.add_column(
            sa.Column(
                "memory_enriched_at",
                sa.DateTime(timezone=True),
                nullable=True,
            )
        )
        batch_op.add_column(
            sa.Column(
                "memory_enrichment_details",
                sa.JSON(),
                nullable=True,
            )
        )


def downgrade() -> None:
    """Downgrade schema:移除 W3.5-C 写作水位列。"""
    with op.batch_alter_table("chapters", schema=None) as batch_op:
        batch_op.drop_column("memory_enrichment_details")
        batch_op.drop_column("memory_enriched_at")
        batch_op.drop_column("memory_enrichment_status")
        batch_op.drop_column("ready_for_next_chapter_at")
