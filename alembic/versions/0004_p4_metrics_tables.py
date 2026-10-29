"""p4 metrics tables:创建 P4 范围 5 张指标表(metrics_daily /
metrics_per_chapter / system_metrics_daily / memory_health_history /
style_audit_history)。

Revision ID: 0004_p4_metrics_tables
Revises: 0003_chapters_state_7_levels
Create Date: 2026-05-10

设计要点:

- **5 张表与 OBSERVABILITY §3.1 字面 ``CREATE TABLE`` 严格对齐**:列名 / 类型
  / 主键完全对齐 spec;计数字段 ``NOT NULL DEFAULT 0`` (无样本时 0 比 NULL
  更合理),平均值 / 平均分 / drift_score 等 ``NULL``(无样本时确实未定义),
  时间戳 / 主键 / severity 等"写入即知"字段 ``NOT NULL``。
- ``metrics_per_chapter`` 包含 P4-plan-kickoff 决策 5 拍板的 prompt cache
  2 字段(``cache_input_tokens INTEGER NULL`` / ``cache_hit_ratio REAL NULL``);
  默认 disable,CostTrackingClient 钩子启用时填入。
- ``memory_health_history.severity`` 4 档 CHECK 约束(对齐 spec §3.1 字面
  ``ok|minor|warning|critical``);枚举值由 ``MEMORY_HEALTH_SEVERITY_VALUES``
  单一真相源提供(``src/core/db/models.py``)。
- ``style_audit_history.severity`` **不加 CHECK**(对齐 ORM 自决项):P4 stub
  时 severity="stub",P7 升级为正式 4 档枚举,CHECK 约束限制 P7 平滑升级;
  service 层语义保证 P4 阶段只写 stub。
- ``metrics_per_chapter.chapter_id`` **不加 FK 到 chapters**:metrics 是只读
  历史样本,即使章节后续被删(P5 OutlineBuilder 重建)也应保留;弱字符串
  引用(对齐 P2-B-03 ``timeline_entries.chapter_id`` 决策)。
- 索引(任务模板要求 + spec 隐含):
  - ``ix_memory_health_history_checked_at``(按时间倒序查近期记录)
  - ``ix_style_audit_history_audited_at``(同上)
  - ``metrics_daily.date`` / ``system_metrics_daily.date`` PK 自带索引,无需另加
  - ``metrics_per_chapter.chapter_id`` PK 自带索引

注:本迁移文件名沿用 P1/P2 ``00NN_<short>.py`` 命名而非 alembic 默认 hash
形式;``revision`` 字符串与文件名前缀一致(``0004_p4_metrics_tables``),
``down_revision`` 指向 ``0003_chapters_state_7_levels``。
"""
from typing import Sequence, Union

from alembic import op
import sqlalchemy as sa


# revision identifiers, used by Alembic.
revision: str = "0004_p4_metrics_tables"
down_revision: Union[str, Sequence[str], None] = "0003_chapters_state_7_levels"
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def upgrade() -> None:
    """Upgrade schema:创建 5 张 metrics 表(顺序:metrics_daily →
    metrics_per_chapter → system_metrics_daily → memory_health_history →
    style_audit_history,对齐 OBSERVABILITY §3.1 字面顺序)。"""
    op.create_table(
        "metrics_daily",
        sa.Column("date", sa.Date(), nullable=False),
        sa.Column("chapters_drafted", sa.Integer(), nullable=False),
        sa.Column("chapters_approved", sa.Integer(), nullable=False),
        sa.Column("chapters_rewritten", sa.Integer(), nullable=False),
        sa.Column("chapters_revised", sa.Integer(), nullable=False),
        sa.Column("chapters_escalated", sa.Integer(), nullable=False),
        sa.Column("avg_review_score", sa.Float(), nullable=True),
        sa.Column("avg_rewrites_per_chapter", sa.Float(), nullable=True),
        sa.Column("avg_revises_per_chapter", sa.Float(), nullable=True),
        sa.Column("total_input_tokens", sa.Integer(), nullable=False),
        sa.Column("total_output_tokens", sa.Integer(), nullable=False),
        sa.Column("total_cost_usd", sa.Float(), nullable=False),
        sa.Column("avg_chapter_latency_seconds", sa.Float(), nullable=True),
        sa.Column("qwen_calls", sa.Integer(), nullable=False),
        sa.Column("claude_sonnet_calls", sa.Integer(), nullable=False),
        sa.Column("claude_opus_calls", sa.Integer(), nullable=False),
        sa.PrimaryKeyConstraint("date"),
    )

    op.create_table(
        "metrics_per_chapter",
        sa.Column("chapter_id", sa.String(), nullable=False),
        sa.Column("drafted_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column("approved_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column("total_latency_seconds", sa.Float(), nullable=False),
        sa.Column("rewrite_count", sa.Integer(), nullable=False),
        sa.Column("revise_count", sa.Integer(), nullable=False),
        sa.Column("parallel_drafts_used", sa.Boolean(), nullable=False),
        sa.Column("final_score_overall", sa.Float(), nullable=False),
        sa.Column("final_score_plot_progression", sa.Float(), nullable=False),
        sa.Column("final_score_tension_match", sa.Float(), nullable=False),
        sa.Column("final_score_prose_quality", sa.Float(), nullable=False),
        sa.Column("final_score_consistency", sa.Float(), nullable=False),
        sa.Column("final_score_voice_consistency", sa.Float(), nullable=False),
        sa.Column("final_score_hook_management", sa.Float(), nullable=False),
        sa.Column("word_count", sa.Integer(), nullable=False),
        sa.Column("target_word_count_min", sa.Integer(), nullable=False),
        sa.Column("target_word_count_max", sa.Integer(), nullable=False),
        sa.Column("target_tension_level", sa.Integer(), nullable=False),
        sa.Column("actual_tension_estimate", sa.Float(), nullable=True),
        sa.Column("total_tokens", sa.Integer(), nullable=False),
        sa.Column("total_cost_usd", sa.Float(), nullable=False),
        sa.Column("writer_profile", sa.String(), nullable=False),
        sa.Column("writer_model", sa.String(), nullable=False),
        sa.Column("writer_calls", sa.Integer(), nullable=False),
        sa.Column("reviewer_calls", sa.Integer(), nullable=False),
        sa.Column("reviser_calls", sa.Integer(), nullable=False),
        sa.Column("cache_input_tokens", sa.Integer(), nullable=True),
        sa.Column("cache_hit_ratio", sa.Float(), nullable=True),
        sa.PrimaryKeyConstraint("chapter_id"),
    )

    op.create_table(
        "system_metrics_daily",
        sa.Column("date", sa.Date(), nullable=False),
        sa.Column("api_requests", sa.Integer(), nullable=False),
        sa.Column("api_errors", sa.Integer(), nullable=False),
        sa.Column("avg_api_latency_ms", sa.Float(), nullable=True),
        sa.Column("ws_connections", sa.Integer(), nullable=False),
        sa.Column("frontend_errors", sa.Integer(), nullable=False),
        sa.Column("pipeline_runs", sa.Integer(), nullable=False),
        sa.PrimaryKeyConstraint("date"),
    )

    op.create_table(
        "memory_health_history",
        sa.Column("check_id", sa.String(), nullable=False),
        sa.Column("checked_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column("severity", sa.String(), nullable=False),
        sa.Column("stale_threads_count", sa.Integer(), nullable=False),
        sa.Column("inactive_characters_count", sa.Integer(), nullable=False),
        sa.Column("conflicts_count", sa.Integer(), nullable=False),
        sa.Column("report_json", sa.JSON(), nullable=False),
        sa.CheckConstraint(
            "severity IN ('ok', 'minor', 'warning', 'critical')",
            name="ck_memory_health_history_severity",
        ),
        sa.PrimaryKeyConstraint("check_id"),
    )
    with op.batch_alter_table("memory_health_history", schema=None) as batch_op:
        batch_op.create_index(
            "ix_memory_health_history_checked_at", ["checked_at"], unique=False
        )

    op.create_table(
        "style_audit_history",
        sa.Column("audit_id", sa.String(), nullable=False),
        sa.Column("audited_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column("chapter_range_start", sa.String(), nullable=True),
        sa.Column("chapter_range_end", sa.String(), nullable=True),
        sa.Column("drift_score", sa.Float(), nullable=True),
        sa.Column("severity", sa.String(), nullable=False),
        sa.Column("report_json", sa.JSON(), nullable=False),
        sa.PrimaryKeyConstraint("audit_id"),
    )
    with op.batch_alter_table("style_audit_history", schema=None) as batch_op:
        batch_op.create_index(
            "ix_style_audit_history_audited_at", ["audited_at"], unique=False
        )


def downgrade() -> None:
    """Downgrade schema:逆序 drop 5 张表 + 索引(style_audit_history →
    memory_health_history → system_metrics_daily → metrics_per_chapter →
    metrics_daily)。"""
    with op.batch_alter_table("style_audit_history", schema=None) as batch_op:
        batch_op.drop_index("ix_style_audit_history_audited_at")
    op.drop_table("style_audit_history")

    with op.batch_alter_table("memory_health_history", schema=None) as batch_op:
        batch_op.drop_index("ix_memory_health_history_checked_at")
    op.drop_table("memory_health_history")

    op.drop_table("system_metrics_daily")
    op.drop_table("metrics_per_chapter")
    op.drop_table("metrics_daily")
