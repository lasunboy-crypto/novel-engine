"""v1.1 wave 1 session 6:``workspaces`` 表加 ``default_voice`` 列(G10 治本上游)。

对齐 [`docs/plan/v1_1_candidates.md` §G10](../../docs/plan/v1_1_candidates.md) +
[`docs/plan/v1_1_plan.md` §4 w1-s6](../../docs/plan/v1_1_plan.md) +
[`docs/impl/v1_1_w1_s6_g10_cross_chapter_voice_validator.md`](../../docs/impl/v1_1_w1_s6_g10_cross_chapter_voice_validator.md)。

Revision ID: 0015_workspace_default_voice
Revises: 0014_chapter_versions_style_fingerprint
Create Date: 2026-05-27

设计要点
========

1. **``workspaces.default_voice`` 新列**:
   - 类型 ``String``,``nullable=False``,``server_default='third_person'``。
   - 既有 workspace 行 upgrade 后由 SQLite 自动 backfill 为 ``'third_person'``
     (项目级硬约束默认 — 中文长篇优先)。
   - 语义 3 档(对齐 ``DEFAULT_VOICE_VALUES``):``third_person`` / ``first_person``
     / ``free``;其中 ``free`` = 关闭跨章节视角硬约束只 warn log(用户选 / 实验)。
   - 上游:``Workspace`` Pydantic 模型同步加 ``default_voice`` Literal 字段
     (``src/core/models/workspace.py``);下游 G10 ``cross_chapter_voice_validator``
     在首章(无 baseline)读此字段决定锁定哪个人称。

2. **CHECK 约束 ``ck_workspaces_default_voice``**:确保枚举内合法值,与 ORM
   ``WorkspaceORM.__table_args__`` ``CheckConstraint`` 同源,沿用 P5-B-07
   ``outline_status`` 同型字面策略。

3. **batch_alter_table SQLite 兼容**:对齐 0014 加列同模式
   (``workspaces`` 表 P5-B-07 加 ``outline_status`` 列后未再变,本任务加
   ``default_voice`` 列同样走 batch_alter_table)。

4. **downgrade 反向 drop 列 + drop CHECK 约束**:对齐 P5-B-07 同型,batch
   操作让 SQLite 重建表时移除两者。

5. **CLAUDE.md §"新增 Alembic 迁移前必 grep 旧测试 ``command.upgrade.*head``"
   纪律自检**:本任务启动期 grep 验证所有旧 ``command.downgrade(*, "-1")``
   测试已 pin 自身 revision(0002 / 0003 / 0004 / 0005 / 0006)。
   ``test_p4_b_01_metrics::test_upgrade_downgrade_upgrade_idempotent`` 是
   head+downgrade-1+head 幂等模式,断言 ``P4_TABLES.issubset(tables)`` 弱依赖,
   0015 不删 P4 表也不删 ``workspaces`` 表的既有列,新 head 推进不破坏
   (P4-B-01 self 留观沿用,对齐 0012 / 0013 / 0014 字面)。
"""
from typing import Sequence, Union

from alembic import op
import sqlalchemy as sa


# revision identifiers, used by Alembic.
revision: str = "0015_workspace_default_voice"
down_revision: Union[str, Sequence[str], None] = "0014_chapter_versions_style_fingerprint"
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def upgrade() -> None:
    """Upgrade schema:``workspaces`` 加 ``default_voice`` String 列 + CHECK 约束。

    既有行 backfill 为 ``'third_person'``(项目级硬约束默认)。
    """
    with op.batch_alter_table("workspaces", schema=None) as batch_op:
        batch_op.add_column(
            sa.Column(
                "default_voice",
                sa.String(),
                nullable=False,
                server_default="third_person",
            )
        )
        batch_op.create_check_constraint(
            "ck_workspaces_default_voice",
            "default_voice IN ('third_person', 'first_person', 'free')",
        )


def downgrade() -> None:
    """Downgrade schema:drop ``workspaces.default_voice`` 列 + CHECK 约束。"""
    with op.batch_alter_table("workspaces", schema=None) as batch_op:
        batch_op.drop_constraint("ck_workspaces_default_voice", type_="check")
        batch_op.drop_column("default_voice")
