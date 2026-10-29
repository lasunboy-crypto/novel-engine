"""v1.1 wave 4 session 2:``plot_threads`` 表加 ``thread_type`` 列(G2 治本)。

对齐 [`docs/plan/v1_1_candidates.md` §G2](../../docs/plan/v1_1_candidates.md) +
[`docs/plan/v1_1_plan.md` §4 w4-s2](../../docs/plan/v1_1_plan.md) +
[`docs/impl/v1_1_w4_s2_g2_callback_explicit_id_chain.md`](../../docs/impl/v1_1_w4_s2_g2_callback_explicit_id_chain.md)。

Revision ID: 0017_plot_threads_thread_type
Revises: 0016_characters_state_timeline
Create Date: 2026-05-30

设计要点
========

1. **``plot_threads.thread_type`` 新列**:
   - 类型 ``String``,``nullable=False``,``server_default='plot_event'``。
   - 既有 plot_thread 行 upgrade 后由 ``server_default`` backfill 为
     ``plot_event``(向后兼容,旧伏笔无类型分类视作普通剧情线)。
   - 存 ``PlotThread.thread_type`` Literal 4 档(``plot_event`` /
     ``foreshadowing`` / ``metaphor`` / ``callback``)。
   - 上游:``PlotThread`` Pydantic 模型 + ``PlotThreadORM`` 同步加字段。
   - 辅助 UI 分类展示;G2 callback 链路(ChapterCard.callback_to → Writer
     SQL lookup)按 thread_id 精确 lookup,不依赖本列。

2. **batch_alter_table SQLite 兼容**:对齐 0016 加列同模式
   (``plot_threads`` 表 P6-B-01 加 ``revoked_at`` 后未再变,本任务加
   ``thread_type`` 同走 batch_alter_table)。

3. **downgrade 反向 drop 列**:对齐 0016 同型,batch 操作让 SQLite 重建表移除。

4. **CLAUDE.md §"新增 Alembic 迁移前必 grep 旧测试 ``command.upgrade.*head``"
   纪律自检**:本任务启动期 grep 验证 ``test_alembic_roundtrip`` 走
   ``upgrade head + downgrade base`` 全拆(不是 ``downgrade -1`` 危险组合),
   其余 ``command.upgrade head`` 测试均配 ``downgrade base`` 或 fixture 初始化
   / 显式 pin 自身 revision;加 0017 不破坏任何旧测试。
"""
from typing import Sequence, Union

from alembic import op
import sqlalchemy as sa


# revision identifiers, used by Alembic.
revision: str = "0017_plot_threads_thread_type"
down_revision: Union[str, Sequence[str], None] = "0016_characters_state_timeline"
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def upgrade() -> None:
    """Upgrade schema:``plot_threads`` 加 ``thread_type`` 列。

    既有行 backfill 为 ``plot_event``(普通剧情线,无伏笔 / 隐喻分类)。
    """
    with op.batch_alter_table("plot_threads", schema=None) as batch_op:
        batch_op.add_column(
            sa.Column(
                "thread_type",
                sa.String(),
                nullable=False,
                server_default="plot_event",
            )
        )


def downgrade() -> None:
    """Downgrade schema:drop ``plot_threads.thread_type`` 列。"""
    with op.batch_alter_table("plot_threads", schema=None) as batch_op:
        batch_op.drop_column("thread_type")
