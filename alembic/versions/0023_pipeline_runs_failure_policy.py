"""v2 wave 1 session 1:``pipeline_runs`` 表加 ``failure_policy`` + ``skipped_chapter_ids``。

对齐 [`docs/plan/v2_plan.md` §4 w1-s1](../../docs/plan/v2_plan.md)(Run 循环
语义补全 — 失败策略可配 + skip 记账)+
[`docs/impl/v2_w1_s1_run_loop_semantics.md`](../../docs/impl/v2_w1_s1_run_loop_semantics.md)。

Revision ID: 0023_pipeline_runs_failure_policy
Revises: 0022_chapters_escalation_details
Create Date: 2026-06-12

设计要点
========

1. **``pipeline_runs.failure_policy`` 新列**:
   - 类型 ``String``,``nullable=False``,``server_default='stop'``。
   - 既有 run 行 upgrade 后由 SQLite 自动 backfill 为 ``'stop'``(v2 前的
     run 全停语义不变,向后兼容)。
   - 语义 3 档(对齐 ``PIPELINE_FAILURE_POLICY_VALUES``):``stop``(默认,
     孤儿章 pause / 异常 error)/ ``skip``(失败章记账继续)/ ``retry``
     (重试 1 次后按 stop)。
   - 上游:``PipelineStartIn`` / ``PipelineRunSnapshot`` 同步加字段;
     下游 ``PipelineService._execute_loop`` 按此路由失败处置。

2. **``pipeline_runs.skipped_chapter_ids`` 新列**:
   - 类型 ``JSON``,``nullable=False``,``server_default='[]'``(对齐
     ``chapter_ids`` / ``completed_chapter_ids`` 同表 JSON 列表同型)。
   - skip 策略下失败章记入本列;``_execute_loop`` 选章时
     ``completed ∪ skipped`` 都不再选(防 skip 后死循环重选同章)。

3. **CHECK 约束 ``ck_pipeline_runs_failure_policy_3_levels``**:确保枚举内
   合法值,与 ORM ``PipelineRunORM.__table_args__`` ``CheckConstraint``
   同源(``_pipeline_failure_policy_check_sql``),沿用 0015
   ``default_voice`` 同型字面策略。

4. **batch_alter_table SQLite 兼容**:对齐 0015 加列 + CHECK 同模式
   (``pipeline_runs`` 表 0009 建表后未再变,本任务加 2 列 + 1 CHECK 走
   batch_alter_table,重建表时既有 state/scope CHECK 由反射保留 — 0015
   workspaces 表同场景已实证)。

5. **downgrade 反向 drop 2 列 + drop CHECK 约束**:对齐 0015 同型。

6. **CLAUDE.md §"新增 Alembic 迁移前必 grep 旧测试 ``command.upgrade.*head``"
   纪律自检**(本任务启动期已 grep):所有旧 ``command.downgrade(*, "-1")``
   测试均已 pin 自身显式 revision(0002 / 0003 / 0004 / 0007 / 0011 / 0012);
   ``test_p4_b_01_metrics::test_upgrade_downgrade_upgrade_idempotent`` 是
   ``head + downgrade -1 + head`` 幂等模式,断言 ``P4_TABLES.issubset`` 弱
   依赖 — 本迁移 add/drop ``pipeline_runs`` 列对称、不动 P4 metrics 表,
   加 0023 不破坏该断言;``upgrade head + downgrade base`` 链不受新 head
   影响(对齐 0022 字面)。
"""
from typing import Sequence, Union

from alembic import op
import sqlalchemy as sa


# revision identifiers, used by Alembic.
revision: str = "0023_pipeline_runs_failure_policy"
down_revision: Union[str, Sequence[str], None] = (
    "0022_chapters_escalation_details"
)
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def upgrade() -> None:
    """Upgrade schema:``pipeline_runs`` 加 ``failure_policy`` + ``skipped_chapter_ids``。

    既有行 backfill 为 ``'stop'`` / ``[]``(v2 前 run 全停语义不变)。
    """
    with op.batch_alter_table("pipeline_runs", schema=None) as batch_op:
        batch_op.add_column(
            sa.Column(
                "failure_policy",
                sa.String(),
                nullable=False,
                server_default="stop",
            )
        )
        batch_op.add_column(
            sa.Column(
                "skipped_chapter_ids",
                sa.JSON(),
                nullable=False,
                server_default="[]",
            )
        )
        batch_op.create_check_constraint(
            "ck_pipeline_runs_failure_policy_3_levels",
            "failure_policy IN ('stop', 'skip', 'retry')",
        )


def downgrade() -> None:
    """Downgrade schema:drop 2 列 + CHECK 约束。"""
    with op.batch_alter_table("pipeline_runs", schema=None) as batch_op:
        batch_op.drop_constraint(
            "ck_pipeline_runs_failure_policy_3_levels", type_="check"
        )
        batch_op.drop_column("skipped_chapter_ids")
        batch_op.drop_column("failure_policy")
