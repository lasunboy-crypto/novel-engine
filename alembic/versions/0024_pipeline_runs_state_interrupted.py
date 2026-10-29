"""v2 wave 1 session 2:``pipeline_runs.state`` CHECK 扩 ``interrupted``(5 → 6 档)。

对齐 [`docs/plan/v2_plan.md` §4 w1-s2](../../docs/plan/v2_plan.md)(断点续跑
闭环 — 重启后 run 可恢复)+
[`docs/impl/v2_w1_s2_resume_and_reset_stale.md`](../../docs/impl/v2_w1_s2_resume_and_reset_stale.md)。

Revision ID: 0024_pipeline_runs_state_interrupted
Revises: 0023_pipeline_runs_failure_policy
Create Date: 2026-06-12

设计要点
========

1. **``interrupted`` 第 6 档语义**(对齐 ``task_records.status`` 同名档先例):
   进程重启时残留 ``running`` 行且数据完整可恢复(``chapter_ids`` 非空)→
   lifespan 启动 hook(``mark_running_as_interrupted_on_startup``)翻本档;
   **可 ``:resume`` 断点续跑**(区别于 ``error`` 死路只能 stop 后重 start、
   completed 进度清零)。

2. **CHECK 约束改名** ``ck_pipeline_runs_state_5_levels`` →
   ``ck_pipeline_runs_state_6_levels``:SQLite 不支持 ALTER CONSTRAINT,走
   ``batch_alter_table`` drop + create(重建表时既有 scope / failure_policy
   CHECK 由反射保留 — 0015 / 0023 同场景已实证)。新档字面与 ORM
   ``PIPELINE_STATE_VALUES``(``src/core/db/models.py``)单一真相源一致。

3. **downgrade 先翻数据再收约束**:存量 ``interrupted`` 行先 UPDATE 为
   ``error``(语义最近邻 — v2 w1-s2 之前重启中断本来就标 error),否则
   重建表回 5 档 CHECK 时存量行违反约束直接报错。

4. **CLAUDE.md §"新增 Alembic 迁移前必 grep 旧测试 ``command.upgrade.*head``"
   纪律自检**(本任务启动期已 grep):所有旧 ``command.downgrade(*, "-1")``
   测试均已 pin 自身显式 revision(0002 / 0003 / 0004 / 0007 / 0009 / 0011 /
   0012 / 0023);``test_p6_b_03_pipeline_runs.py::TestAlembicMigration::
   test_upgrade_head_creates_check_constraints`` 是"单独 upgrade head"型但
   **断言旧约束名字面** ``ck_pipeline_runs_state_5_levels`` → 本任务同步
   改该断言为 ``6_levels``(同文件 ``TestStateCheckConstraint`` parametrize
   ``PIPELINE_STATE_VALUES`` 自动覆盖第 6 档,越界用例 ``completed`` 仍
   越界不受影响);``test_p4_b_01_metrics`` 幂等模式弱依赖不动 pipeline_runs
   表,加 0024 不破坏(对齐 0023 字面)。
"""
from typing import Sequence, Union

from alembic import op


# revision identifiers, used by Alembic.
revision: str = "0024_pipeline_runs_state_interrupted"
down_revision: Union[str, Sequence[str], None] = (
    "0023_pipeline_runs_failure_policy"
)
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def upgrade() -> None:
    """Upgrade schema:``state`` CHECK 5 档 → 6 档(加 ``interrupted``)。"""
    with op.batch_alter_table("pipeline_runs", schema=None) as batch_op:
        batch_op.drop_constraint(
            "ck_pipeline_runs_state_5_levels", type_="check"
        )
        batch_op.create_check_constraint(
            "ck_pipeline_runs_state_6_levels",
            "state IN ('idle', 'running', 'paused', 'interrupted', "
            "'stopped', 'error')",
        )


def downgrade() -> None:
    """Downgrade schema:存量 ``interrupted`` 行翻 ``error`` 后收回 5 档 CHECK。"""
    op.execute(
        "UPDATE pipeline_runs SET state = 'error' WHERE state = 'interrupted'"
    )
    with op.batch_alter_table("pipeline_runs", schema=None) as batch_op:
        batch_op.drop_constraint(
            "ck_pipeline_runs_state_6_levels", type_="check"
        )
        batch_op.create_check_constraint(
            "ck_pipeline_runs_state_5_levels",
            "state IN ('idle', 'running', 'paused', 'stopped', 'error')",
        )
