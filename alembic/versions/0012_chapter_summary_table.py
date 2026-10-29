"""v1.0.x wave 4 session 3a:新建 ``chapter_summaries`` 表。

长程记忆基础设施。会话 3a 完成后,每章 approved 时由 ``MemoryExtractor
.extract_chapter_summary`` LLM 抽取一次,落本表;会话 3b 章节卡端
``chapter_card_service.derive_context`` 读"前 N 章 ChapterSummary 序列"注入
下一章卡 LLM 上下文。

字段集对齐 ``src/core/db/models.py:ChapterSummaryORM``(单一真相源)+ P5-B-06
``chapter_card_service.derive_context`` 注释承诺(会话 2 兑现了字段映射,会话
3a/3b 兑现完整摘要)。

Revision ID: 0012_chapter_summary_table
Revises: 0011_plot_threads_planted_chapter_nullable
Create Date: 2026-05-21

设计要点:

1. **``chapter_summaries`` 新表 11 列**(对齐 ``ChapterSummaryORM`` Pydantic 字段):
   - ``chapter_id`` PK / ``workspace_id`` FK CASCADE 到 ``workspaces``
   - ``summary_text`` Text 非空(~200-500 字 LLM 输出)
   - ``pov_character`` String Nullable / ``scene_setting`` Text Nullable
   - ``key_events`` JSON 非空 / ``character_interactions`` JSON 非空 /
     ``unfinished_actions`` JSON 非空(三个集合字段默认空 list)
   - ``extraction_trigger`` String CHECK 2 档(``reviewer_pass`` / ``force_pass``)
   - ``model_used`` String 非空(qwen-novel 等)
   - ``created_at`` / ``updated_at`` 非空

2. **CHECK 约束 1 条**:
   - ``ck_chapter_summaries_extraction_trigger``:extraction_trigger IN 2 档
   - 单一真相源:``CHAPTER_SUMMARY_TRIGGER_VALUES`` 在 ``src/core/db/models.py``,
     本迁移内嵌字面避免 import 副作用(对齐 0009 / 0008 / 0006 / 0005 同模式)。

3. **索引 1 条**:
   - ``ix_chapter_summaries_workspace_id``:list-by-workspace 查询(会话 3b
     章节卡端 derive_context 拉"前 N 章摘要"时按工作区过滤)

4. **不对 chapters.chapter_id 加 FK**:章节摘要在 approved 时落库,此时
   chapters 行已存在,但弱字符串引用避免插入顺序耦合 + 与 ``ChapterCardORM``
   / ``timeline_entries`` 同模式(对齐 P2-B-03 决策)。

5. **downgrade 反向 drop 表 + 索引**(SQLite drop_table 自动级联约束;索引
   显式 drop 对齐 0009 同模式)。

CLAUDE.md §"新增 Alembic 迁移前必 grep 旧测试 ``command.upgrade.*head``"纪律:
本任务自检 — pre-flight grep ``command\\.upgrade.*head + command\\.downgrade.*-1``
全部已在前序任务 pin 到自身 revision(P2-B-17 0003 / P4-B-01 0004 / P5-B-01
0005 / P5-B-02 0006 / P5-B-07 0007 / P6-B-03 0009 / A2-03 0010 backfill /
B2-01 0011)。唯一例外 ``test_p4_b_01_metrics.py::test_upgrade_downgrade_upgrade_idempotent``
是 head + downgrade-1 + head 幂等模式,断言 ``P4_TABLES.issubset(tables)`` 弱
依赖,新 head 推进不破坏(CLAUDE.md §纪律字面适用但语义不影响)— **不 pin**,
登记 impl 文档自决项(类 B 工程判断,纪律字面适用但断言弱依赖证据闭环)。
"""
from typing import Sequence, Union

from alembic import op
import sqlalchemy as sa


# revision identifiers, used by Alembic.
revision: str = "0012_chapter_summary_table"
down_revision: Union[str, Sequence[str], None] = (
    "0011_plot_threads_planted_chapter_nullable"
)
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


_CHAPTER_SUMMARY_TRIGGER_VALUES = (
    "reviewer_pass",
    "force_pass",
)
"""``chapter_summaries.extraction_trigger`` 2 档(对齐 ``src/core/db/models.py``
``CHAPTER_SUMMARY_TRIGGER_VALUES`` 单一真相源;迁移内嵌字面避免 import 副作用)。"""


def _check_sql(column: str, values: tuple[str, ...]) -> str:
    quoted = ", ".join(f"'{v}'" for v in values)
    return f"{column} IN ({quoted})"


def upgrade() -> None:
    """Upgrade schema:新建 ``chapter_summaries`` 表 + 1 索引 + 1 CHECK 约束。"""
    op.create_table(
        "chapter_summaries",
        sa.Column("chapter_id", sa.String(), nullable=False),
        sa.Column("workspace_id", sa.String(), nullable=False),
        sa.Column("summary_text", sa.Text(), nullable=False),
        sa.Column("pov_character", sa.String(), nullable=True),
        sa.Column("scene_setting", sa.Text(), nullable=True),
        sa.Column("key_events", sa.JSON(), nullable=False),
        sa.Column("character_interactions", sa.JSON(), nullable=False),
        sa.Column("unfinished_actions", sa.JSON(), nullable=False),
        sa.Column("extraction_trigger", sa.String(), nullable=False),
        sa.Column("model_used", sa.String(), nullable=False),
        sa.Column(
            "created_at", sa.DateTime(timezone=True), nullable=False
        ),
        sa.Column(
            "updated_at", sa.DateTime(timezone=True), nullable=False
        ),
        sa.ForeignKeyConstraint(
            ["workspace_id"],
            ["workspaces.workspace_id"],
            ondelete="CASCADE",
        ),
        sa.PrimaryKeyConstraint("chapter_id"),
        sa.CheckConstraint(
            _check_sql("extraction_trigger", _CHAPTER_SUMMARY_TRIGGER_VALUES),
            name="ck_chapter_summaries_extraction_trigger",
        ),
    )
    op.create_index(
        "ix_chapter_summaries_workspace_id",
        "chapter_summaries",
        ["workspace_id"],
    )


def downgrade() -> None:
    """Downgrade schema:drop ``chapter_summaries`` 表(CHECK 约束自动级联)。"""
    op.drop_index(
        "ix_chapter_summaries_workspace_id", table_name="chapter_summaries"
    )
    op.drop_table("chapter_summaries")
