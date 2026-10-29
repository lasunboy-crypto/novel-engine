"""v1.1 wave 6 session 3:``chapters`` 表加 ``scene_anchor`` JSON 列(B2 治本)。

对齐 [`docs/plan/v1_1_wave6_design.md` §2 B2](../../docs/plan/v1_1_wave6_design.md) +
[`docs/acceptance/luo-clean-v2-content-issues.md`](../../docs/acceptance/luo-clean-v2-content-issues.md)
(bug B1 病房入梦→工厂醒 / B2 工厂密室→催眠舱瞬移 / B3 推演室→大桥)。

Revision ID: 0020_chapters_scene_anchor
Revises: 0019_characters_identity_facets
Create Date: 2026-06-04

设计要点
========

1. **``chapters.scene_anchor`` 新列**:
   - 类型 ``JSON``,``nullable=False``,``server_default='{}'``(空 dict)。
   - 既有 chapter 行 upgrade 后由 SQLite 自动 backfill 为 ``{}``(无锚点,B2 比对
     自动 skip)。
   - 存双时空场景锚点 ``{enter_dream_location, wake_location,
     has_transition_explanation}``。``_extract_memories_safely`` 在 extract 后
     写入(非 extract 写事务内,护 Y1-C 不变量);跨章比对"醒来点≠入梦点且无位移
     交代"时按 ``chapter_id`` 升序查最近一次入梦点。
   - 上游:``ChapterORM`` 同步加字段。落点选 ``chapters`` 列而非新表 —— extract
     时 ``_upsert_chapter_state(state=initial_state)`` 已先建 chapter 行(chapter_id
     零填充 ch-NNNN 字典序=章序),按 chapter_id 排序即可查"最近一次入梦点",
     省一张表 + Pydantic + store 模块。

2. **batch_alter_table SQLite 兼容**:对齐 0019 / 0016 加 JSON 列同模式
   (``chapters`` 表上次列变更是 0008 加 ``force_pass_*``,本任务加
   ``scene_anchor`` 同走 batch_alter_table)。

3. **downgrade 反向 drop 列**:对齐 0019 / 0016 同型,batch 操作让 SQLite 重建
   表移除。

4. **CLAUDE.md §"新增 Alembic 迁移前必 grep 旧测试 ``command.upgrade.*head``"
   纪律自检**:本任务启动期 grep 验证所有旧 ``command.downgrade(*, "-1")`` 测试
   均已 pin 自身显式 revision(``test_v1_hotfix_wave4_session3a`` 两处 downgrade -1
   先 pin ``0012_chapter_summary_table`` 再退,对新 head 健壮);唯一
   ``upgrade head + downgrade -1`` = ``test_p4_b_01_metrics``
   ``test_upgrade_downgrade_upgrade_idempotent``(head→-1→head 幂等,断言
   P4_TABLES subset)——本迁移 add/drop ``chapters`` 列对称、不动 P4 表,
   加 0020 不破坏。
"""
from typing import Sequence, Union

from alembic import op
import sqlalchemy as sa


# revision identifiers, used by Alembic.
revision: str = "0020_chapters_scene_anchor"
down_revision: Union[str, Sequence[str], None] = "0019_characters_identity_facets"
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def upgrade() -> None:
    """Upgrade schema:``chapters`` 加 ``scene_anchor`` JSON 列。

    既有行 backfill 为 ``{}``(无双时空场景锚点)。
    """
    with op.batch_alter_table("chapters", schema=None) as batch_op:
        batch_op.add_column(
            sa.Column(
                "scene_anchor",
                sa.JSON(),
                nullable=False,
                server_default="{}",
            )
        )


def downgrade() -> None:
    """Downgrade schema:drop ``chapters.scene_anchor`` 列。"""
    with op.batch_alter_table("chapters", schema=None) as batch_op:
        batch_op.drop_column("scene_anchor")
