"""p5 chapter card status:创建 P5 范围 ``chapter_cards`` 表(对应
``ChapterCard.card_status`` + ``card_approved_at``,PROJECT_SPEC §3.2 +
ROADMAP §P5 验收"前 3 章进入审核",plan-kickoff 决策 6=C)。

Revision ID: 0006_chapter_card_status
Revises: 0005_volumes_table
Create Date: 2026-05-12

设计要点:

- **最小列设计**(类 C 处置):章节卡完整内容仍走文件 ``data/cards/<chapter_id>.json``
  (SSOT,P1 起约定),本表只缓存 4 列(``chapter_id`` PK / ``workspace_id`` /
  ``card_status`` / ``card_approved_at`` + 审计字段 ``created_at``),支持
  P5-B-06 ``chapter:write`` gate 高效检索 ``card_status != 'approved'``。
  对齐 plan-kickoff 决策 2=C(Volume 主表 + JSON SSOT 在文件)先例。
- ``card_status`` 4 档 CHECK 约束(对齐 PROJECT_SPEC §3.2 字面与
  ``CARD_STATUS_VALUES`` Literal 4 档:``draft / pending_review / approved
  / rejected``);枚举值由 ``CARD_STATUS_VALUES`` 单一真相源提供
  (``src/core/db/models.py``),本迁移字面 IN 列表保持同步(对齐 P4-B-01
  ``MEMORY_HEALTH_SEVERITY_VALUES`` + P5-B-01 ``TENSION_TEMPLATE_VALUES``
  模式)。
- ``workspace_id`` FK CASCADE 到 ``workspaces`` 主表(对齐 P1 ``chapters``
  / P2 ``characters`` / P5 ``volumes`` FK 模式);**不**对 ``chapters``
  加 FK — P5-B-06 outline:commit 后批量生成卡片时 ``chapters`` 行尚未
  存在,弱字符串引用更稳妥(对齐 P2-B-03 ``timeline_entries.chapter_id``
  决策)。
- ``ix_chapter_cards_workspace_id`` 索引(P5-B-06 / P5-B-07 / P5-B-09 按
  workspace 过滤查询需要)。
- **向后兼容 backfill**(对齐反问引子 ②):新建表本身为空(P1-P4 章节卡
  全在 JSON 文件),本迁移内 ``op.execute("UPDATE ...")`` 是 no-op 安全
  网 — 若 service 层在迁移完成前已插入空 ``card_status`` 行(例如双进程
  竞争),自动置 ``approved`` 兜底,对齐 ROADMAP §P5 验收语义"P1-P4
  既有章节卡视为已通过"。

注:本迁移文件名沿用 P1/P2/P3/P4/P5-B-01 ``00NN_<short>.py`` 命名而非
alembic 默认 hash;``revision`` 字符串与文件名前缀一致
(``0006_chapter_card_status``),``down_revision`` 指向 ``0005_volumes_table``。
"""
from typing import Sequence, Union

from alembic import op
import sqlalchemy as sa


# revision identifiers, used by Alembic.
revision: str = "0006_chapter_card_status"
down_revision: Union[str, Sequence[str], None] = "0005_volumes_table"
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def upgrade() -> None:
    """Upgrade schema:创建 ``chapter_cards`` 表 + 1 索引 + 1 CHECK + 1 FK
    + 1 backfill safety UPDATE。"""
    op.create_table(
        "chapter_cards",
        sa.Column("chapter_id", sa.String(), nullable=False),
        sa.Column("workspace_id", sa.String(), nullable=False),
        sa.Column(
            "card_status",
            sa.String(),
            nullable=False,
            server_default=sa.text("'draft'"),
        ),
        sa.Column(
            "card_approved_at", sa.DateTime(timezone=True), nullable=True
        ),
        sa.Column("created_at", sa.DateTime(timezone=True), nullable=False),
        sa.CheckConstraint(
            "card_status IN ('draft', 'pending_review', 'approved', 'rejected')",
            name="ck_chapter_cards_status",
        ),
        sa.ForeignKeyConstraint(
            ["workspace_id"], ["workspaces.workspace_id"], ondelete="CASCADE"
        ),
        sa.PrimaryKeyConstraint("chapter_id"),
    )
    with op.batch_alter_table("chapter_cards", schema=None) as batch_op:
        batch_op.create_index(
            "ix_chapter_cards_workspace_id", ["workspace_id"], unique=False
        )

    # Safety backfill:迁移后兜底任何 NULL ``card_status`` 行(理论上不存在,
    # NOT NULL + server_default 已保证;本句作为审计与文档兜底,对齐
    # 反问引子 ② 推荐 "Alembic 0006 内 op.execute UPDATE" 模式)。
    op.execute(
        "UPDATE chapter_cards SET card_status = 'approved' "
        "WHERE card_status IS NULL"
    )


def downgrade() -> None:
    """Downgrade schema:drop ``chapter_cards`` 表 + 索引。"""
    with op.batch_alter_table("chapter_cards", schema=None) as batch_op:
        batch_op.drop_index("ix_chapter_cards_workspace_id")
    op.drop_table("chapter_cards")
