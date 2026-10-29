"""chapters_state_7_levels:给 ``chapters.status`` 加 7 档 CHECK 约束(P2-B-17)。

Revision ID: 0003_chapters_state_7_levels
Revises: 0002_p2_memory_models
Create Date: 2026-05-08

设计要点:

- **不改字段名**(类 A 冲突,详见 ``docs/impl/p2_b_17_chapters_state_7_levels.md``):
  任务模板字面 ``state`` vs ``PROJECT_SPEC §3.5`` ``Chapter.status`` 字面差异,
  按规格源对齐 ``status``,本迁移只给已有 ``status`` 列加 CHECK。
- **SQLite CHECK 约束需 batch_alter_table**:SQLite 不支持 ALTER TABLE ADD
  CONSTRAINT,alembic ``batch_alter_table`` 通过 "create-new-table → copy-rows
  → drop-old → rename-new" 模拟。``env.py`` 已开 ``render_as_batch=True``,
  本脚本走 ``with op.batch_alter_table(...)`` 标准路径。
- **upgrade**:加 ``ck_chapters_status_7_levels`` 约束,枚举值与
  ``src/core/db/models.CHAPTER_STATUS_VALUES`` 一致(7 档)。upgrade 前若已有
  非 7 档值的行,batch_alter_table copy 阶段会因 CHECK 失败抛错;P1 baseline
  从未真实写入 chapters 表(纯文件 IO),fresh db 无脏数据,无需预 fold。
- **downgrade**:**先 fold 4 个新档**到 baseline 3 档(drafting / reviewing /
  approved),再 drop 约束。fold 规则(对齐任务模板字面要求):
  - ``rewriting`` → ``drafting``(仍在写作阶段,起点 = drafting)
  - ``revising`` → ``reviewing``(等待审稿修订完成,概念上仍在审稿循环)
  - ``published`` → ``approved``(终态成功的细分,统一回 approved)
  - ``human_intervention`` → ``drafting``(升人工 = 重置起点,等待重新写)
  fold 与 drop 顺序不能颠倒——若先 drop 约束再 fold,降级运行时也是合法的;
  但 fold 在前更直观体现"7 档归并到 3 档"语义。

回滚验证:``alembic upgrade head`` + ``alembic downgrade -1`` round-trip 测试
覆盖在 ``tests/db/test_p2_b17_chapters_state.py``(P2-B-17 新建)。

"""
from typing import Sequence, Union

from alembic import op


# revision identifiers, used by Alembic.
revision: str = "0003_chapters_state_7_levels"
down_revision: Union[str, Sequence[str], None] = "0002_p2_memory_models"
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


# 7 档枚举值(与 src/core/db/models.CHAPTER_STATUS_VALUES 单一真相源同步)
_STATUS_VALUES_7 = (
    "drafting",
    "reviewing",
    "rewriting",
    "revising",
    "approved",
    "published",
    "human_intervention",
)
_CHECK_NAME = "ck_chapters_status_7_levels"


def _check_predicate() -> str:
    quoted = ", ".join(f"'{v}'" for v in _STATUS_VALUES_7)
    return f"status IN ({quoted})"


def upgrade() -> None:
    """加 7 档 CHECK 约束。"""
    with op.batch_alter_table("chapters", schema=None) as batch_op:
        batch_op.create_check_constraint(_CHECK_NAME, _check_predicate())


def downgrade() -> None:
    """先 fold 4 新档到 3 baseline 档,再 drop CHECK 约束。"""
    # fold:rewriting → drafting / revising → reviewing / published → approved
    # / human_intervention → drafting。op.execute 走 sqlite 原生 SQL,不依赖
    # ORM(ORM 已是 7 档 CHECK,downgrade 时不能再用)。
    op.execute(
        "UPDATE chapters SET status='drafting' WHERE status='rewriting'"
    )
    op.execute(
        "UPDATE chapters SET status='reviewing' WHERE status='revising'"
    )
    op.execute(
        "UPDATE chapters SET status='approved' WHERE status='published'"
    )
    op.execute(
        "UPDATE chapters SET status='drafting' WHERE status='human_intervention'"
    )
    with op.batch_alter_table("chapters", schema=None) as batch_op:
        batch_op.drop_constraint(_CHECK_NAME, type_="check")
