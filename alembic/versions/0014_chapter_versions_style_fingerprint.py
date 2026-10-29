"""v1.1 wave 1 session 4:``chapter_versions`` 表加 ``style_fingerprint`` 列(G5 治本)。

对齐 [`docs/plan/v1_1_candidates.md` §G5](../../docs/plan/v1_1_candidates.md) +
[`docs/plan/v1_1_plan.md` §4 w1-s4](../../docs/plan/v1_1_plan.md) +
[`docs/impl/v1_1_w1_s4_g5_style_drift_validator.md`](../../docs/impl/v1_1_w1_s4_g5_style_drift_validator.md)。

Revision ID: 0014_chapter_versions_style_fingerprint
Revises: 0013_timeline_is_flashback
Create Date: 2026-05-26

设计要点
========

1. **``chapter_versions.style_fingerprint`` 新列**:
   - 类型 ``JSON``,``nullable=True``,旧行 upgrade 后保持 NULL(向后兼容旧
     版本数据无指纹的语义)。新版本 INSERT 时由 ``persist_chapter_version_safely``
     从 ``ChapterVersion.style_fingerprint`` 写入(``None`` = 未抽取)。
   - 语义:首版(v1,``creation_type="draft"``)抽取 4 维度风格统计后落盘
     作 baseline;后续 vN(``creation_type="rewrite"``)抽取后由
     ``style_drift_validator`` 跟 v1 对比偏移度。
   - 4 维度:段落长度均值/标准差、人称代词频次(9 种)、句长均值/标准差、
     修辞密度。详 ``src/core/agents/style_fingerprint_extractor.py``。

2. **不动其他字段**:本任务只加单列,不改既有列字面 / type / nullable。

3. **batch_alter_table SQLite 兼容**:对齐 0013 加列同模式
   (``chapter_versions`` 表上次加列在初始 schema)。

4. **downgrade 反向 drop 列**:对齐 0013 同模式。

5. **CLAUDE.md §"新增 Alembic 迁移前必 grep 旧测试 ``command.upgrade.*head``"
   纪律自检**:本任务启动期 grep 验证所有旧 ``command.downgrade(*, "-1")``
   测试已 pin 自身 revision(0002 / 0003 / 0004 / 0005 / 0006)。
   ``test_p4_b_01_metrics::test_upgrade_downgrade_upgrade_idempotent`` 是
   head+downgrade-1+head 幂等模式,断言 ``P4_TABLES.issubset(tables)`` 弱依赖,
   0014 不删 P4 表也不删 ``chapter_versions`` 表的既有列,新 head 推进不破坏
   (P4-B-01 self 留观沿用,对齐 0012 / 0013 字面)。其他 ``command.upgrade(*, "head")``
   单独使用的 fixture 不配 downgrade-1,不受新 head 影响。
"""
from typing import Sequence, Union

from alembic import op
import sqlalchemy as sa


# revision identifiers, used by Alembic.
revision: str = "0014_chapter_versions_style_fingerprint"
down_revision: Union[str, Sequence[str], None] = "0013_timeline_is_flashback"
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def upgrade() -> None:
    """Upgrade schema:``chapter_versions`` 加 ``style_fingerprint`` JSON 列。"""
    with op.batch_alter_table("chapter_versions", schema=None) as batch_op:
        batch_op.add_column(
            sa.Column(
                "style_fingerprint",
                sa.JSON(),
                nullable=True,
            )
        )


def downgrade() -> None:
    """Downgrade schema:drop ``chapter_versions.style_fingerprint`` 列。"""
    with op.batch_alter_table("chapter_versions", schema=None) as batch_op:
        batch_op.drop_column("style_fingerprint")
