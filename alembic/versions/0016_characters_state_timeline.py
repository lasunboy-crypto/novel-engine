"""v1.1 wave 4 session 1:``characters`` 表加 ``state_timeline`` JSON 列(G1 治本)。

对齐 [`docs/plan/v1_1_candidates.md` §G1](../../docs/plan/v1_1_candidates.md) +
[`docs/plan/v1_1_plan.md` §4 w4-s1](../../docs/plan/v1_1_plan.md) +
[`docs/impl/v1_1_w4_s1_g1_character_state_timeline.md`](../../docs/impl/v1_1_w4_s1_g1_character_state_timeline.md)。

Revision ID: 0016_characters_state_timeline
Revises: 0015_workspace_default_voice
Create Date: 2026-05-29

设计要点
========

1. **``characters.state_timeline`` 新列**:
   - 类型 ``JSON``,``nullable=False``,``server_default='[]'``(空数组)。
   - 既有 character 行 upgrade 后由 SQLite 自动 backfill 为 ``[]``(无历史状态
     快照,首次有 state_change 的章节抽取后开始累积)。
   - 存 ``list[CharacterStateSnapshot]`` 的 JSON 序列化(每章一条,按
     ``as_of_chapter`` 全局序升序);Writer 写第 N 章时取截止 N-1 章最新一条
     注入 prompt 绕开 embedding,防"断臂复活"。
   - 上游:``Character`` Pydantic 模型 + ``CharacterORM`` 同步加字段。

2. **batch_alter_table SQLite 兼容**:对齐 0014 加 JSON 列同模式
   (``characters`` 表 P6-B-01 加 ``revoked_at`` 后未再变,本任务加
   ``state_timeline`` 同走 batch_alter_table)。

3. **downgrade 反向 drop 列**:对齐 0014 同型,batch 操作让 SQLite 重建表移除。

4. **CLAUDE.md §"新增 Alembic 迁移前必 grep 旧测试 ``command.upgrade.*head``"
   纪律自检**:本任务启动期 grep 验证所有旧 ``command.downgrade(*, "-1")``
   测试已 pin 自身显式 revision(0002 / 0003 / 0004 / 0005 / 0012 等),
   无 ``upgrade head + downgrade -1`` 危险组合;``upgrade head`` 均配
   ``downgrade base`` 全拆或 fixture 初始化,加 0016 不破坏任何旧测试。
   ``test_p4_b_01_metrics`` 幂等测断言 ``P4_TABLES.issubset(tables)`` 弱依赖,
   0016 不删 P4 表也不动 ``characters`` 既有列,新 head 推进不破坏。
"""
from typing import Sequence, Union

from alembic import op
import sqlalchemy as sa


# revision identifiers, used by Alembic.
revision: str = "0016_characters_state_timeline"
down_revision: Union[str, Sequence[str], None] = "0015_workspace_default_voice"
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def upgrade() -> None:
    """Upgrade schema:``characters`` 加 ``state_timeline`` JSON 列。

    既有行 backfill 为 ``[]``(空状态时间线)。
    """
    with op.batch_alter_table("characters", schema=None) as batch_op:
        batch_op.add_column(
            sa.Column(
                "state_timeline",
                sa.JSON(),
                nullable=False,
                server_default="[]",
            )
        )


def downgrade() -> None:
    """Downgrade schema:drop ``characters.state_timeline`` 列。"""
    with op.batch_alter_table("characters", schema=None) as batch_op:
        batch_op.drop_column("state_timeline")
