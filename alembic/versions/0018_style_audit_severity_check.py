"""v1.1 wave 4 session 5:``style_audit_history.severity`` 加 4 档 CHECK 约束(I2.2)。

对齐 [`docs/plan/v1_1_candidates.md` §I2](../../docs/plan/v1_1_candidates.md) +
[`docs/plan/v1_1_plan.md` §4 w4-s5](../../docs/plan/v1_1_plan.md) +
[`docs/impl/v1_1_w4_s5_i2_2_reviewer_template_conformance_style_auditor.md`](../../docs/impl/v1_1_w4_s5_i2_2_reviewer_template_conformance_style_auditor.md)。

Revision ID: 0018_style_audit_severity_check
Revises: 0017_plot_threads_thread_type
Create Date: 2026-06-01

设计要点
========

1. **背景**:P4-B-10 StyleAuditor 是 stub(``severity="stub"``,无 CHECK 约束);
   v1.1 w4-s5 / I2.2 落地为真 LLM agent,severity 升正式 4 档
   ``ok|minor|warning|critical``。维护人拍板(2026-06-01)DB 层加锁。

2. **数据 fold(upgrade 先做)**:既有 ``style_audit_history`` 行可能含旧
   ``severity='stub'``(P4 stub 阶段手动触发审计的历史快照)。加 CHECK 约束前
   先 ``UPDATE`` 把任何不在 4 档内的 severity fold 成 ``'ok'``(stub 占位本就
   无真实偏移信号,fold 成 ok = "无偏移基线" 最贴近语义);否则旧行违反新约束
   导致 SQLite 重建表失败。

3. **batch_alter_table create_check_constraint(SQLite 兼容)**:SQLite 不支持
   ``ALTER TABLE ADD CONSTRAINT``,alembic batch 模式重建表加约束(对齐 0006
   ``ck_chapter_cards_status`` / 0004 memory_health severity 同型 CHECK)。
   CHECK 谓词字面与 ``src/core/db/models.py`` ``STYLE_AUDIT_SEVERITY_VALUES`` +
   ``_style_audit_severity_check_sql()`` 单一真相源对齐(迁移自包含 inline 字面,
   与 0004 / 0005 / 0006 同例)。

4. **downgrade**:batch 模式 drop CHECK 约束(重建表移除);**不** un-fold
   stub(fold 不可逆且无意义 — 旧 stub 语义已并入 ok)。

5. **CLAUDE.md §"新增 Alembic 迁移前必 grep 旧测试 ``command.upgrade.*head``"
   纪律自检**:本任务启动期 grep 验证 ``tests/db/`` 旧 ``command.downgrade(*, "-1")``
   测试已 pin 自身显式 revision(0004 用 ``"0004_p4_metrics_tables"`` 等);
   ``upgrade head + downgrade -1 + upgrade head`` 幂等往返测(test_p4_b_01_metrics)
   断言弱依赖(``P4_TABLES.issubset(tables)`` / 表存在性),0018 不删任何表也不动
   ``style_audit_history`` 既有列,只加 severity CHECK,新 head 推进不破坏旧测试。
   并已实测全 ``tests/db/`` 套件通过(详 impl 文档)。
"""
from typing import Sequence, Union

from alembic import op

# revision identifiers, used by Alembic.
revision: str = "0018_style_audit_severity_check"
down_revision: Union[str, Sequence[str], None] = "0017_plot_threads_thread_type"
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None

# CHECK 谓词字面(与 ``src/core/db/models.py`` SSOT 对齐;迁移自包含 inline)。
_SEVERITY_CHECK_SQL = (
    "severity IN ('ok', 'minor', 'warning', 'critical')"
)
_SEVERITY_CHECK_NAME = "ck_style_audit_history_severity"


def upgrade() -> None:
    """Upgrade schema:fold 旧 stub severity → ok,再加 severity 4 档 CHECK。"""
    # 1. 数据 fold:任何不在 4 档内的 severity(典型 'stub')→ 'ok'。
    op.execute(
        "UPDATE style_audit_history SET severity = 'ok' "
        "WHERE severity NOT IN ('ok', 'minor', 'warning', 'critical')"
    )
    # 2. batch 重建表加 CHECK 约束(SQLite 兼容)。
    with op.batch_alter_table("style_audit_history", schema=None) as batch_op:
        batch_op.create_check_constraint(
            _SEVERITY_CHECK_NAME,
            _SEVERITY_CHECK_SQL,
        )


def downgrade() -> None:
    """Downgrade schema:drop severity CHECK 约束(fold 不 un-do)。"""
    with op.batch_alter_table("style_audit_history", schema=None) as batch_op:
        batch_op.drop_constraint(_SEVERITY_CHECK_NAME, type_="check")
