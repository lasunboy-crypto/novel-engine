"""v1.1 wave 7:``review_results`` 表加 4 个卓越性纯度量列(M2 持久化)。

对齐 [`docs/plan/v1_1_wave7_plan.md` §2.4 B](../../docs/plan/v1_1_wave7_plan.md):
wave 5 给 reviewer 加的画面感 / 内心戏 / 套路化 / 沉浸感 4 个**卓越性纯度量**
(``score_imagery`` / ``score_interiority`` / ``score_formulaic`` / ``score_immersion``)
此前只活在内存 ``ReviewResult`` + 草稿池选优,**从未持久化** → 主审稿历史界面
(读 ``review_results`` 表)永远拿不到 → 前端展示不出来。本迁移把它们落库,
让 ChapterEditorPage 的审稿历史能持久展示每章质感读数。

Revision ID: 0021_review_results_excellence_metrics
Revises: 0020_chapters_scene_anchor
Create Date: 2026-06-10

设计要点
========

1. **4 个新列均 ``nullable=True``**(对比同表 6 基础维 ``nullable=False``):
   - 语义:``None`` = "本次审稿未度量"(LLM 漏产 / 主审退化 / 0021 前的旧审稿)。
     与 ``ReviewResult`` Pydantic 模型字段 ``float | None`` 默认 ``None`` 对齐。
   - 既有 ``review_results`` 行 upgrade 后由 SQLite 自动 backfill 为 ``NULL``
     (无 server_default —— None 是合法且语义正确的"未度量",前端 ExcellenceMetrics
     组件 4 个全 None → 整组件不渲染,优雅退化)。
   - 上游:``ReviewResultORM`` 同步加 4 字段;``persist_review_result_safely``
     写入;``ChapterService.get_review_summary`` 读出映射到 ``ReviewSummaryEntry``。

2. **治理不变量(关键)**:这 4 列**永不进 ``score_overall`` 加权、永不影响
   verdict**。verdict 由 reviewer ``_compute_score_overall``(仅 6 基础维加权)
   在**写库之前**算定,本迁移只加**存储列**、不碰任何计算逻辑 → 与 verdict 计算
   正交,持久化不可能反过来改变判定。详 governance §2.16/2.17/2.21 + 防回退测
   ``test_reviewer.py`` 断言权重表不含这 4 维。

3. **batch_alter_table SQLite 兼容**:对齐 0020 / 0019 / 0016 加列同模式
   (``review_results`` 表加 4 列,batch 重建表)。

4. **downgrade 反向 drop 4 列**:对齐 0020 同型,batch 操作让 SQLite 重建表移除。

5. **CLAUDE.md §"新增 Alembic 迁移前必 grep 旧测试 ``command.upgrade.*head``"
   纪律自检**(本任务启动期已 grep):旧 ``downgrade(*, "-1")`` 测试均已 pin
   自身显式 revision;唯一 ``upgrade head + downgrade -1`` 幂等测
   ``test_p4_b_01_metrics::test_upgrade_downgrade_upgrade_idempotent``
   断言 ``P4_TABLES`` subset —— 本迁移 add/drop ``review_results`` 列对称、
   不动 P4 metrics 表、不改表存在性,加 0021 不破坏该幂等断言。
"""
from typing import Sequence, Union

from alembic import op
import sqlalchemy as sa


# revision identifiers, used by Alembic.
revision: str = "0021_review_results_excellence_metrics"
down_revision: Union[str, Sequence[str], None] = "0020_chapters_scene_anchor"
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


_EXCELLENCE_COLUMNS = (
    "score_imagery",
    "score_interiority",
    "score_formulaic",
    "score_immersion",
)


def upgrade() -> None:
    """Upgrade schema:``review_results`` 加 4 个卓越性度量 Float 列(nullable)。

    既有行 backfill 为 ``NULL``(无 server_default —— None = 未度量,语义正确)。
    """
    with op.batch_alter_table("review_results", schema=None) as batch_op:
        for col in _EXCELLENCE_COLUMNS:
            batch_op.add_column(sa.Column(col, sa.Float(), nullable=True))


def downgrade() -> None:
    """Downgrade schema:drop ``review_results`` 4 个卓越性度量列。"""
    with op.batch_alter_table("review_results", schema=None) as batch_op:
        for col in _EXCELLENCE_COLUMNS:
            batch_op.drop_column(col)
