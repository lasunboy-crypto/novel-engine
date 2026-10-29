"""v1.1 acceptance 修复2:``chapters`` 表加 ``escalation_details`` JSON 列。

对齐收官修复2(Fix D + 孤儿章人工解决流程):章节升 ``human_intervention`` 时
的结构化详情(目前 = Fix D 写前 must 可行性预检的 findings —— 哪条 must 不可达 /
judge 证据 / 给人工编辑的处置建议)此前无处持久化(escalation_reason 只活在
``OrchestratorResult`` + WS 事件 + 日志)→ 孤儿章人工裁决 UI(ChapterEditorPage)
拿不到"为什么进人工介入"。本迁移把它落库,``GET /chapters/{id}/status`` 透出。

Revision ID: 0022_chapters_escalation_details
Revises: 0021_review_results_excellence_metrics
Create Date: 2026-06-10

设计要点
========

1. **``nullable=True`` 无 server_default**:``None`` = "无结构化详情"(0022 前的
   旧行 / 非 Fix D 升级路径 / 正常 approved 章)。既有行 upgrade 后由 SQLite
   自动 backfill 为 ``NULL``,语义正确。
2. **JSON 列**(对齐 0020 ``chapters.scene_anchor`` 同表同型先例):dict 结构
   ``{"reason": str, "checked_at_state": str, "findings": [{"must_text",
   "confidence", "evidence", "suggestion"}]}``,由 ``ChapterService`` 在
   ``_orchestrate`` 返回 ``HUMAN_INTERVENTION`` 时写入。
3. **batch_alter_table SQLite 兼容**(对齐 0021 / 0020 / 0019 加列同模式)。
4. **downgrade 反向 drop**(batch 重建表)。
5. **CLAUDE.md §"新增 Alembic 迁移前必 grep 旧测试 ``command.upgrade.*head``"
   纪律自检**(本任务启动期已 grep):旧 ``downgrade(*, "-1")`` 测试均已 pin
   自身显式 revision(``test_p2_models`` pin ``0002_p2_memory_models``);
   ``upgrade head + downgrade base`` 链不受新 head 影响;幂等测
   ``test_p4_b_01_metrics`` 断言 ``P4_TABLES`` subset —— 本迁移 add/drop
   ``chapters`` 列对称、不动 P4 metrics 表,加 0022 不破坏该断言。
"""
from typing import Sequence, Union

from alembic import op
import sqlalchemy as sa


# revision identifiers, used by Alembic.
revision: str = "0022_chapters_escalation_details"
down_revision: Union[str, Sequence[str], None] = (
    "0021_review_results_excellence_metrics"
)
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def upgrade() -> None:
    """Upgrade schema:``chapters`` 加 ``escalation_details`` JSON 列(nullable)。"""
    with op.batch_alter_table("chapters", schema=None) as batch_op:
        batch_op.add_column(
            sa.Column("escalation_details", sa.JSON(), nullable=True)
        )


def downgrade() -> None:
    """Downgrade schema:drop ``chapters.escalation_details``。"""
    with op.batch_alter_table("chapters", schema=None) as batch_op:
        batch_op.drop_column("escalation_details")
