"""p5 b 07 outline_status + rejection_notes 加列(P5-B-07)。

对齐 p5_plan.md §"P5-B-07"字面 + 启动指令待审 ①/②(维护人 2026-05-12 拍 A
+ 待审 ③ 拍 A 一并加 2 列单次迁移):

- ``workspaces`` 加 ``outline_status: String NOT NULL DEFAULT 'not_started'``
  + CHECK 3 档(``not_started`` / ``generating`` / ``committed``);P5-B-04
  outline:generate 改 → ``generating``,outline:commit 改 → ``committed``。
- ``chapter_cards`` 加 ``rejection_notes: Text NULL``;``POST /chapters/{id}/
  card:reject`` 可选 body 字段持久化(对齐 P5-B-02 "最小列"扩展模式,
  P6+ 引入审核历史表前足够)。

Revision ID: 0007_outline_status_rejection_notes
Revises: 0006_chapter_card_status
Create Date: 2026-05-12

设计要点:

- **两列在不同表,单次迁移**(待审 ③ 拍 A):语义都属 P5-B-07 范围,
  两列互不冲突;round-trip 测试合一(对齐 P5-B-02 单文件 CHECK + 加列
  组合模式 + 命名 ``00NN_<short>.py`` 沿用)。
- **SQLite batch_alter_table 自动重建表**:SQLite 不支持 ``ALTER TABLE
  ADD CONSTRAINT``,Alembic batch 模式重建表(复制现有 schema / FK /
  CHECK / index)+ 写新列。本迁移依赖 batch 模式补 ``ck_workspaces_outline_status``
  CHECK 约束。
- **outline_status 默认值 not_started**:既有 P1-P4 工作区升级后所有
  workspaces 行 ``outline_status='not_started'``(P5 引入前没有 outline 概念);
  outline:commit 成功后才翻 ``committed``。
- **rejection_notes Nullable**:无审核退回时为 ``NULL``;``POST card:reject``
  body 不带 ``rejection_notes`` 字段时也 ``NULL``(可选字段语义)。

注:沿用 ``00NN_<short>.py`` 命名(对齐 0001-0006);``revision`` 字符串
与文件名前缀一致(``0007_outline_status_rejection_notes``),``down_revision``
指向 ``0006_chapter_card_status``。
"""
from typing import Sequence, Union

from alembic import op
import sqlalchemy as sa


# revision identifiers, used by Alembic.
revision: str = "0007_outline_status_rejection_notes"
down_revision: Union[str, Sequence[str], None] = "0006_chapter_card_status"
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def upgrade() -> None:
    """Upgrade schema:``workspaces`` 加 ``outline_status`` 列 + CHECK +
    ``chapter_cards`` 加 ``rejection_notes`` 列。"""
    with op.batch_alter_table("workspaces", schema=None) as batch_op:
        batch_op.add_column(
            sa.Column(
                "outline_status",
                sa.String(),
                nullable=False,
                server_default=sa.text("'not_started'"),
            )
        )
        batch_op.create_check_constraint(
            "ck_workspaces_outline_status",
            "outline_status IN ('not_started', 'generating', 'committed')",
        )

    with op.batch_alter_table("chapter_cards", schema=None) as batch_op:
        batch_op.add_column(
            sa.Column("rejection_notes", sa.Text(), nullable=True)
        )


def downgrade() -> None:
    """Downgrade schema:drop ``rejection_notes`` 列 + drop ``outline_status``
    列 + CHECK 约束。"""
    with op.batch_alter_table("chapter_cards", schema=None) as batch_op:
        batch_op.drop_column("rejection_notes")

    with op.batch_alter_table("workspaces", schema=None) as batch_op:
        batch_op.drop_constraint(
            "ck_workspaces_outline_status", type_="check"
        )
        batch_op.drop_column("outline_status")
