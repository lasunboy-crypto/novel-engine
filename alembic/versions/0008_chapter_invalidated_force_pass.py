"""P6-B-01 章节人工介入扩 schema:rewind / force-pass / unblock + 后续章节 invalidated 档 + 软删除标记。

对齐 docs/plan/p6_plan.md §"P6-B-01"字面 + plan-kickoff 决策 2/3(rewind
soft-delete + 后续章节 invalidated 显式 unblock)+ 启动指令"必字面硬约束"
4 处加列改动。

Revision ID: 0008_chapter_invalidated_force_pass
Revises: 0007_outline_status_rejection_notes
Create Date: 2026-05-13

设计要点:

1. **``chapters.status`` CHECK 约束扩 ``invalidated`` 第 8 档**
   (P2-B-17 baseline 7 档 + P6-B-01 扩 1 档):
   - drop 旧 CHECK ``ck_chapters_status_7_levels``
   - create 新 CHECK ``ck_chapters_status_8_levels``(IN 8 字面)
   - SQLite batch_alter_table 重建表(SQLite 不支持 ALTER TABLE
     DROP/ADD CONSTRAINT)

2. **``chapters`` 加 ``force_pass_at`` + ``force_pass_reason`` 两列**
   (P6-B-01 force-pass 端点 schema 写入):
   - ``force_pass_at: DateTime(timezone=True), nullable=True``(常态 None)
   - ``force_pass_reason: Text, nullable=True``(常态 None;schema 字面
     强制非空 ``min_length=1``,DB 列允许 NULL 兼容历史行)

3. **``chapter_cards`` 加 ``force_profile`` 一列**
   (P6-B-01 预留 + P6-B-09 真接入):
   - ``force_profile: Text, nullable=True``(常态 None;P6-B-09 实施时
     接 Pydantic ChapterCard 模型 + chapter_service force_profile 优先
     _resolve_profile)

4. **``characters`` / ``plot_threads`` / ``timeline_entries`` 三表加
   ``revoked_at`` 一列**(P6-B-01 rewind soft-delete):
   - ``revoked_at: DateTime(timezone=True), nullable=True``(常态 None)
   - ``ChapterRewindService.rewind`` 时按 ``first_appearance_chapter`` /
     ``planted_chapter`` / ``chapter_id`` 字段匹配目标章节 → 翻
     ``datetime.now(UTC)``

5. **SQLite batch_alter_table 自动重建表**:SQLite 不支持 ``ALTER TABLE
   DROP CONSTRAINT`` / ``ALTER TABLE ADD CONSTRAINT``,Alembic batch 模式
   重建表(复制现有 schema / FK / CHECK / index)+ 写新列 / 改新 CHECK。
   本迁移依赖 batch 模式补 ``ck_chapters_status_8_levels`` CHECK 约束
   (drop 旧 CHECK + create 新 CHECK 由 batch 自动 fold 进单次重建)。

6. **downgrade 完整反向**:drop 新列 + drop 新 CHECK + create 旧 CHECK
   (``ck_chapters_status_7_levels`` 7 档)+ 保留所有数据(rewind invalidated
   章节行在 downgrade 后会撞 CHECK 约束失败 — 这是 design choice,upgrade
   时确保 stash 没有 invalidated 行,downgrade 才能干净跑。生产环境不会
   downgrade;本字段主要给 round-trip 测试用)。

注:沿用 ``00NN_<short>.py`` 命名(对齐 0001-0007);``revision`` 字符串
与文件名前缀一致(``0008_chapter_invalidated_force_pass``),``down_revision``
指向 ``0007_outline_status_rejection_notes``。
"""
from typing import Sequence, Union

from alembic import op
import sqlalchemy as sa


# revision identifiers, used by Alembic.
revision: str = "0008_chapter_invalidated_force_pass"
down_revision: Union[str, Sequence[str], None] = "0007_outline_status_rejection_notes"
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


_CHAPTER_STATUS_8_LEVELS = (
    "drafting",
    "reviewing",
    "rewriting",
    "revising",
    "approved",
    "published",
    "human_intervention",
    "invalidated",
)
"""P6-B-01 8 档(对齐 ``CHAPTER_STATUS_VALUES``;本迁移内嵌字面避免 import
``src.core.db.models`` 在 alembic 上下文里触发副作用)。"""


_CHAPTER_STATUS_7_LEVELS = (
    "drafting",
    "reviewing",
    "rewriting",
    "revising",
    "approved",
    "published",
    "human_intervention",
)
"""P2-B-17 baseline 7 档(downgrade 时恢复;rewind 引入前的字面)。"""


def _check_sql(values: tuple[str, ...]) -> str:
    quoted = ", ".join(f"'{v}'" for v in values)
    return f"status IN ({quoted})"


def upgrade() -> None:
    """Upgrade schema:
    - ``chapters``:加 ``force_pass_at`` + ``force_pass_reason`` 两列 +
      drop 旧 7 档 CHECK + create 新 8 档 CHECK
    - ``chapter_cards``:加 ``force_profile`` 一列
    - ``characters`` / ``plot_threads`` / ``timeline_entries``:各加
      ``revoked_at`` 一列
    """
    # ---- chapters: 加 force_pass 字段 + CHECK 8 档 ------------------------
    with op.batch_alter_table("chapters", schema=None) as batch_op:
        batch_op.add_column(
            sa.Column(
                "force_pass_at",
                sa.DateTime(timezone=True),
                nullable=True,
            )
        )
        batch_op.add_column(
            sa.Column("force_pass_reason", sa.Text(), nullable=True)
        )
        batch_op.drop_constraint(
            "ck_chapters_status_7_levels", type_="check"
        )
        batch_op.create_check_constraint(
            "ck_chapters_status_8_levels",
            _check_sql(_CHAPTER_STATUS_8_LEVELS),
        )

    # ---- chapter_cards: 加 force_profile 字段 -----------------------------
    with op.batch_alter_table("chapter_cards", schema=None) as batch_op:
        batch_op.add_column(
            sa.Column("force_profile", sa.Text(), nullable=True)
        )

    # ---- characters: 加 revoked_at 字段 -----------------------------------
    with op.batch_alter_table("characters", schema=None) as batch_op:
        batch_op.add_column(
            sa.Column(
                "revoked_at",
                sa.DateTime(timezone=True),
                nullable=True,
            )
        )

    # ---- plot_threads: 加 revoked_at 字段 ---------------------------------
    with op.batch_alter_table("plot_threads", schema=None) as batch_op:
        batch_op.add_column(
            sa.Column(
                "revoked_at",
                sa.DateTime(timezone=True),
                nullable=True,
            )
        )

    # ---- timeline_entries: 加 revoked_at 字段 -----------------------------
    with op.batch_alter_table("timeline_entries", schema=None) as batch_op:
        batch_op.add_column(
            sa.Column(
                "revoked_at",
                sa.DateTime(timezone=True),
                nullable=True,
            )
        )


def downgrade() -> None:
    """Downgrade schema:
    - ``timeline_entries`` / ``plot_threads`` / ``characters``:drop
      ``revoked_at``
    - ``chapter_cards``:drop ``force_profile``
    - ``chapters``:drop ``force_pass_reason`` + ``force_pass_at`` +
      drop 8 档 CHECK + create 旧 7 档 CHECK
    """
    with op.batch_alter_table("timeline_entries", schema=None) as batch_op:
        batch_op.drop_column("revoked_at")

    with op.batch_alter_table("plot_threads", schema=None) as batch_op:
        batch_op.drop_column("revoked_at")

    with op.batch_alter_table("characters", schema=None) as batch_op:
        batch_op.drop_column("revoked_at")

    with op.batch_alter_table("chapter_cards", schema=None) as batch_op:
        batch_op.drop_column("force_profile")

    with op.batch_alter_table("chapters", schema=None) as batch_op:
        batch_op.drop_constraint(
            "ck_chapters_status_8_levels", type_="check"
        )
        batch_op.create_check_constraint(
            "ck_chapters_status_7_levels",
            _check_sql(_CHAPTER_STATUS_7_LEVELS),
        )
        batch_op.drop_column("force_pass_reason")
        batch_op.drop_column("force_pass_at")
