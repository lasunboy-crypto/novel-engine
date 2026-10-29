"""v1.1 wave 1 session 1:``timeline_entries`` 表加 ``is_flashback`` 列(E2 治本下游)。

对齐 [`docs/plan/v1_1_candidates.md` §E2](../../docs/plan/v1_1_candidates.md) +
[`docs/plan/v1_1_plan.md` §4 w1-s1](../../docs/plan/v1_1_plan.md) +
[`docs/impl/v1_1_w1_s1_g3_beat_and_e2_flashback.md`](../../docs/impl/v1_1_w1_s1_g3_beat_and_e2_flashback.md)。

Revision ID: 0013_timeline_is_flashback
Revises: 0012_chapter_summary_table
Create Date: 2026-05-26

设计要点
========

1. **``timeline_entries.is_flashback`` 新列**:
   - 类型 ``Boolean``,``nullable=False``,``server_default="0"``(SQLite Boolean
     底层是 INTEGER 0/1,字面 "0" = False)。
   - 旧行 upgrade 后自动回填 0(``False`` 语义);新 INSERT 时 ORM
     ``default=False`` 起作用(``TimelineEntryORM.is_flashback``)。
   - 语义:本 entry 是否描述 flashback / 回忆 / 穿越回过去 / 双时空穿梭的
     另一时空。``check_timeline_monotonic`` 校验时跳过
     ``is_flashback=True`` 的 entry,只在主时间线内做单调校验。
     ``MemoryExtractor`` LLM prompt 引导 LLM 标此字段。

2. **G3 beat 数据模型不动 SQL schema**:
   - ``Arc.beats: list[Beat]`` 字段嵌套到既有 ``volumes.arcs_json`` 列内
     (``sa.JSON`` 反序列化由 Pydantic ``Arc`` 模型负责),**不需要新增列**。
   - 旧 ``arcs_json`` 数据反序列化时 ``beats`` 字段默认 ``[]``(向后兼容,
     Pydantic ``Arc.beats`` 字段 ``default_factory=list``)。
   - 因此本迁移**只针对 E2** 加 ``is_flashback`` 列;G3 数据模型变化在 ORM
     模型层 + Pydantic 模型层完成,无需 SQL 迁移。

3. **batch_alter_table SQLite 兼容**:对齐 0008 加列同模式(``timeline_entries``
   表上次加列在 0008 ``revoked_at``)。

4. **downgrade 反向 drop 列**:对齐 0008 同模式。

5. **CLAUDE.md §"新增 Alembic 迁移前必 grep 旧测试 ``command.upgrade.*head``"
   纪律自检**:本任务启动期 grep 验证所有旧测试已 pin 到自身 revision(0002 /
   0003 / 0004 / 0005 / 0006 / 0007 / 0009 / 0010 / 0011 / 0012 / B2-01 /
   wave4-session3a)。``test_p4_b_01_metrics::test_upgrade_downgrade_upgrade_idempotent``
   是 head+downgrade-1+head 幂等模式,断言 ``P4_TABLES.issubset(tables)`` 弱依赖,
   0013 不删 P4 表,新 head 推进不破坏(P4-B-01 self 留观沿用,对齐 0012 字面)。
"""
from typing import Sequence, Union

from alembic import op
import sqlalchemy as sa


# revision identifiers, used by Alembic.
revision: str = "0013_timeline_is_flashback"
down_revision: Union[str, Sequence[str], None] = "0012_chapter_summary_table"
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def upgrade() -> None:
    """Upgrade schema:``timeline_entries`` 加 ``is_flashback`` Boolean 列。"""
    with op.batch_alter_table("timeline_entries", schema=None) as batch_op:
        batch_op.add_column(
            sa.Column(
                "is_flashback",
                sa.Boolean(),
                nullable=False,
                server_default=sa.text("0"),
            )
        )


def downgrade() -> None:
    """Downgrade schema:drop ``timeline_entries.is_flashback`` 列。"""
    with op.batch_alter_table("timeline_entries", schema=None) as batch_op:
        batch_op.drop_column("is_flashback")
