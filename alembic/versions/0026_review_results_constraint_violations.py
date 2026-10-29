"""W3.5-R2:审稿结果持久化结构化 setting 约束违规。

``review_results.constraint_violations`` 保存
``ConstraintViolation.model_dump(mode="json")`` 的数组，使来源规则、正文证据、
HARD/SOFT 与 ambiguity 可在页面刷新后复查，不再从 ``suggestions`` 展示字符串
反解析。

旧行升级后取 ``[]``；downgrade 对称移除列。新增 head 前已检索旧测试中的
``command.upgrade(..., "head")`` / ``command.downgrade(..., "-1")`` 组合：
历史 ``-1`` 自测均 pin 自身 revision；通用 head fixture 允许跟随最新 head。

Revision ID: 0026_review_results_constraint_violations
Revises: 0025_chapter_writing_watermarks
Create Date: 2026-07-30
"""

from typing import Sequence, Union

import sqlalchemy as sa
from alembic import op


revision: str = "0026_review_results_constraint_violations"
down_revision: Union[str, Sequence[str], None] = (
    "0025_chapter_writing_watermarks"
)
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def upgrade() -> None:
    """给 ``review_results`` 增加非空 JSON 数组列，旧行回填空数组。"""
    with op.batch_alter_table("review_results", schema=None) as batch_op:
        batch_op.add_column(
            sa.Column(
                "constraint_violations",
                sa.JSON(),
                nullable=False,
                server_default=sa.text("'[]'"),
            )
        )


def downgrade() -> None:
    """移除 R2 结构化违规列。"""
    with op.batch_alter_table("review_results", schema=None) as batch_op:
        batch_op.drop_column("constraint_violations")
