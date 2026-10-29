"""v1.0.x B2-01:``plot_threads.planted_chapter`` 改可空。

B2-01 要把 outline 阶段的 long_threads 批落 ``plot_threads`` 表。outline 阶段
没有具体章节,``planted_chapter`` 字段不可强求非空。本迁移把列约束从
``NOT NULL`` 放宽到 ``NULL``,放行后续 ``OutlineService._persist_outline_plot_threads``
落 ``planted_chapter=None`` 的 outline 落表行。

Revision ID: 0011_plot_threads_planted_chapter_nullable
Revises: 0010_backfill_completed_chapters
Create Date: 2026-05-20

实施要点:
- SQLite 不支持 ``ALTER COLUMN ... DROP NOT NULL``。Alembic 提供 ``batch_alter_table``
  自动 recreate table + 复制数据,SQLite 友好。本迁移走 batch 模式。
- ``downgrade`` 把列约束还原 ``NOT NULL``。若回滚时表中已有 ``planted_chapter=NULL``
  行(来自 outline 落表),回滚会失败 — 接受这个边界(对齐 P4-B-01 同型数据
  破坏型 downgrade 设计:downgrade 只对干净的新装库有效)。
- 既有数据 ``planted_chapter`` 都是非空字符串(asset_import 入口 _require),
  upgrade 0 行变更;仅放宽约束。

CLAUDE.md §"新增 Alembic 迁移前必 grep 旧测试 ``command.upgrade.*head``"纪律:
- 已 grep,旧测试 ``tests/db/test_v1_hotfix_a_03_backfill_completed_chapters.py``
  4 处 ``command.upgrade(cfg, "head")`` 同步改成显式 pin ``0010_backfill_completed_chapters``,
  避免新 head 推进后 ``downgrade -1`` 退到 0010 而非 0009 撞 backfill SQL 不重跑 bug。
"""
from typing import Sequence, Union

import sqlalchemy as sa
from alembic import op


# revision identifiers, used by Alembic.
revision: str = "0011_plot_threads_planted_chapter_nullable"
down_revision: Union[str, Sequence[str], None] = "0010_backfill_completed_chapters"
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def upgrade() -> None:
    """``plot_threads.planted_chapter`` ``NOT NULL`` → ``NULL``。"""
    with op.batch_alter_table("plot_threads") as batch_op:
        batch_op.alter_column(
            "planted_chapter",
            existing_type=sa.String(),
            nullable=True,
        )


def downgrade() -> None:
    """还原 ``NOT NULL``。

    注意:回滚前表中若有 ``planted_chapter=NULL`` 行(B2-01 后 outline 自动落表
    产生),回滚会因 ``NOT NULL`` 约束违反失败。这是预期行为 — 数据破坏型回滚,
    实际生产环境不应回滚 0011,只在测试 round-trip 中跑。
    """
    with op.batch_alter_table("plot_threads") as batch_op:
        batch_op.alter_column(
            "planted_chapter",
            existing_type=sa.String(),
            nullable=False,
        )
