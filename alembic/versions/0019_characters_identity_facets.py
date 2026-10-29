"""v1.1 wave 6 session 2:``characters`` 表加 ``identity_facets`` JSON 列(A3 治本)。

对齐 [`docs/plan/v1_1_wave6_design.md` §1 A3](../../docs/plan/v1_1_wave6_design.md) +
[`docs/acceptance/luo-clean-v2-content-issues.md`](../../docs/acceptance/luo-clean-v2-content-issues.md)
(bug A1 沈舟双界身份写串 / A5 白露现实侧整章丢失)。

Revision ID: 0019_characters_identity_facets
Revises: 0018_style_audit_severity_check
Create Date: 2026-06-04

设计要点
========

1. **``characters.identity_facets`` 新列**:
   - 类型 ``JSON``,``nullable=False``,``server_default='[]'``(空数组)。
   - 既有 character 行 upgrade 后由 SQLite 自动 backfill 为 ``[]``(无多重身份的
     简单角色也是空,不受影响)。
   - 存 ``list[IdentityFacet]`` 的 JSON 序列化(每条 {context, identity})。
     Writer 注入固定身份上下文(防双界身份写串 / 丢失)+ mini_plan 覆盖自检
     + 职业称谓黑名单粗筛三处消费。
   - 上游:``Character`` / ``CharacterOutlineItem`` Pydantic 模型 + ``CharacterORM``
     同步加字段。

2. **batch_alter_table SQLite 兼容**:对齐 0016 加 JSON 列同模式
   (``characters`` 表上次变更是 0016 加 ``state_timeline``,本任务加
   ``identity_facets`` 同走 batch_alter_table)。

3. **downgrade 反向 drop 列**:对齐 0016 同型,batch 操作让 SQLite 重建表移除。

4. **CLAUDE.md §"新增 Alembic 迁移前必 grep 旧测试 ``command.upgrade.*head``"
   纪律自检**:本任务启动期 grep 验证所有旧 ``command.downgrade(*, "-1")`` 测试
   均已 pin 自身显式 revision(0002 / 0003 / 0004 / 0005 / 0006 / 0007 / 0009 /
   0011 / 0012 等),无"upgrade head + downgrade -1 断言自身退一档"危险组合;
   唯一 ``upgrade head + downgrade -1`` 出现处 = ``test_p4_b_01_metrics``
   ``test_upgrade_downgrade_upgrade_idempotent``(head→-1→head 幂等测,断言
   P4_TABLES subset)——它对新 head 健壮(本迁移 down/up 对称,不动 P4 表),
   加 0019 不破坏。
"""
from typing import Sequence, Union

from alembic import op
import sqlalchemy as sa


# revision identifiers, used by Alembic.
revision: str = "0019_characters_identity_facets"
down_revision: Union[str, Sequence[str], None] = "0018_style_audit_severity_check"
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def upgrade() -> None:
    """Upgrade schema:``characters`` 加 ``identity_facets`` JSON 列。

    既有行 backfill 为 ``[]``(空身份侧写列表)。
    """
    with op.batch_alter_table("characters", schema=None) as batch_op:
        batch_op.add_column(
            sa.Column(
                "identity_facets",
                sa.JSON(),
                nullable=False,
                server_default="[]",
            )
        )


def downgrade() -> None:
    """Downgrade schema:drop ``characters.identity_facets`` 列。"""
    with op.batch_alter_table("characters", schema=None) as batch_op:
        batch_op.drop_column("identity_facets")
