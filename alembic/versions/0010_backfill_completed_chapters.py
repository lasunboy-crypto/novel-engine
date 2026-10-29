"""v1.0 hot-fix A-03:backfill ``workspaces.completed_chapters``。

体检报告 §A-03 实证:luo2 已 5 章 approved 但 ``workspace.completed_chapters
== 0`` —— 字段从未 += 路径。本次 hot-fix 在 service 层加同步翻新逻辑,本
迁移负责把现有所有工作区的累计 approved 章数一次性回填。

Revision ID: 0010_backfill_completed_chapters
Revises: 0009_pipeline_runs_table
Create Date: 2026-05-19

执行语义:
- ``UPDATE workspaces SET completed_chapters = (SELECT COUNT(*) FROM chapters
  WHERE chapters.workspace_id = workspaces.workspace_id AND status='approved')``
- 仅统计 ``approved`` 一档(``published`` 是 P1-P6 占位档,本期 B-04 hot-fix
  会删,这里保守只统计 approved 与 A-03 service hook +1 路径一致)。
- Workspace 表为空时 UPDATE 0 行(无害)。

回滚语义:
- ``downgrade`` 无意义回滚(没法知道原 ``completed_chapters`` 是 0 还是历史值);
  对齐 P4-B-01 同型纯数据 migration 设计:``downgrade`` 设 noop pass。
- 测试沿用 P6-B-03 ``test_p6_b_03_pipeline_runs.py`` 显式 revision pin 模式
  (``upgrade 到 0010 → downgrade -1 → upgrade 到 0010``),避免后续 head 漂移
  撞 CLAUDE.md §Alembic head 纪律。
"""
from typing import Sequence, Union

from alembic import op


# revision identifiers, used by Alembic.
revision: str = "0010_backfill_completed_chapters"
down_revision: Union[str, Sequence[str], None] = "0009_pipeline_runs_table"
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def upgrade() -> None:
    """Backfill ``workspaces.completed_chapters`` 从 ``chapters`` 表聚合查。"""
    op.execute(
        """
        UPDATE workspaces
        SET completed_chapters = (
            SELECT COUNT(*) FROM chapters
            WHERE chapters.workspace_id = workspaces.workspace_id
            AND chapters.status = 'approved'
        )
        """
    )


def downgrade() -> None:
    """Downgrade noop(纯数据回填,原值不可知,对齐 P4-B-01 同型数据迁移)。"""
    pass
