"""Metrics 写入服务(P4-B-03)。

`metrics_per_chapter` 表写入路径:在章节状态机进入 ``APPROVED`` 时由
``ChapterService._wrap_state_transition_with_db`` 闭包调本模块
``write_per_chapter_safely`` 一次性落库。

设计要点
--------

- **单一真相源:DB**。回调签名 ``(prev, nxt) -> None`` 拿不到
  ``OrchestratorResult``;写入所需字段全部从已落库的 ``chapters`` /
  ``chapter_versions`` / ``review_results`` 三表反查(P2-B-17 / P4-B-02 已落
  写入路径)。card 由 caller 闭包透传(``target_word_count_min/max`` /
  ``target_tension_level`` 不在 DB 内)。
- **失败 swallow + structlog log**(对齐 P2-B-19 ``_extract_memories_safely``
  / P3-B-07 cost_tracker / P3-B-13 continuity refresh / P4-B-02
  ``persist_chapter_version_safely`` / ``persist_review_result_safely``
  五先例)。metrics 是辅助任务,落库失败不应阻塞主流水线。
- **幂等**:``chapter_id`` 主键已存在 → 静默跳过(防御重跑场景;APPROVED
  状态本身可能被多次 transition 触发,且 ``_upsert_chapter_state`` 已对
  ``approved_at`` 做了 first-only 写入)。
- **workspace_id 透传**:caller 显式传(P3-B-14 沉淀 — 不读 ``card.workspace_id``
  字面;``metrics_per_chapter`` 表 schema 无 ``workspace_id`` 字段,本参数仅
  供 structlog 事件 + chapter_row 防御性校验)。

字段映射(MetricsPerChapterORM ← 数据源)
-----------------------------------------

- ``chapter_id``                     ← caller 入参
- ``drafted_at``                     ← ``ChapterORM.created_at``
- ``approved_at``                    ← ``ChapterORM.approved_at``
- ``total_latency_seconds``          ← ``approved_at - created_at`` 秒
- ``rewrite_count``                  ← content rewrite 版本数（排除本地机械 reflow）
- ``revise_count``                   ← ``COUNT(chapter_versions.creation_type='revise')``
- ``final_score_*``(6 档)          ← latest ``ReviewResultORM`` (按 ``reviewed_at`` desc)
- ``word_count``                     ← latest ``ChapterVersionORM`` (按 ``version_id`` desc) ``.word_count``
- ``target_word_count_min/max``      ← ``card.word_count_min/max``(类 A:OBSERVABILITY 字面 ``target_`` 前缀,ChapterCard 字面无前缀)
- ``target_tension_level``           ← ``card.tension_level``(同上,无 ``target_`` 前缀)
- ``actual_tension_estimate``        ← ``None``(P5+ Reviewer 估计字段未引入)
- ``total_tokens``                   ← ``sum(ChapterORM.total_token_usage[*].input_tokens + output_tokens)``
- ``total_cost_usd``                 ← ``ChapterORM.total_cost_usd``
- ``writer_profile``                 ← ``latest_version.model_used``(类 A:OBSERVABILITY 字面 "[升级]profile id",P3-B-01 未透传 profile_id 到版本行,fallback model_used 占位;P5+ 加 ``ChapterVersionORM.writer_profile_id`` 字段后切换)
- ``writer_model``                   ← ``latest_version.model_used``
- ``writer_calls`` / reviewer/reviser ← ``attempt_ledger`` 对应 agent 的真实
                                        provider 物理调用数；历史无账本行时
                                        回退旧版 version/review 推导
- ``cache_input_tokens``             ← ``None``(P4-plan-kickoff 决策 5=B,Anthropic prompt cache 默认 disable)
- ``cache_hit_ratio``                ← ``None``(同上)
"""

from __future__ import annotations

from collections.abc import Callable
from typing import Any

import structlog
from sqlalchemy import func, select
from sqlalchemy.orm import Session

from src.core.db.models import (
    AttemptLedgerORM,
    ChapterORM,
    ChapterVersionORM,
    MetricsPerChapterORM,
    ReviewResultORM,
)
from src.core.models.chapter_card import ChapterCard
from src.core.rules.paragraph_reflow import PARAGRAPH_REFLOW_MODEL_USED
from src.core.rules.prose_revision_contract import count_effective_content_rewrites

_log = structlog.get_logger(__name__)

MetricsSessionFactory = Callable[[], Session]
"""零参 session 工厂(``ChapterService`` 把"按 workspace_id 开 session"
包成零参再注入,与 P4-B-02 ``persist_*_safely`` helper 同形。"""


def write_per_chapter_safely(
    session_factory: MetricsSessionFactory | None,
    *,
    workspace_id: str,
    chapter_id: str,
    card: ChapterCard,
    refresh_existing: bool = False,
) -> None:
    """落 ``metrics_per_chapter`` 表,失败 swallow + structlog log。

    ``session_factory=None`` → no-op(P1 兼容路径,无 DB 写)。给定时打开短
    session、读 ``chapters`` / ``chapter_versions`` / ``review_results``
    三表反查 metrics 字段、写一行、commit、close。事务边界全在本函数内,
    **不冒泡**——主流水线已 APPROVED,metrics 持久化是辅助任务,失败不应
    回滚状态机(对齐 P2-B-19 / P3-B-07 / P3-B-13 / P4-B-02 五先例)。

    幂等:``chapter_id`` 主键已存在 → 静默跳过(默认);
    ``refresh_existing=True`` 时改为**刷新成本字段**(v2 w2-s2 metrics
    修复 A):重读 ``chapters.total_cost_usd`` / ``total_token_usage``
    更新行 ``total_cost_usd`` / ``total_tokens``。**为何**(w2-s1 报告
    §7.1 漏记②):APPROVED 转移瞬间快照后,章摘要 + 记忆抽取两个 LLM
    hook 在 orchestrator 出口才跑,成本继续累进 chapters 账而 per_chapter
    快照不再更新(逐章稳定差 $0.21-0.38)→ caller 在 hook 链结束后带
    ``refresh_existing=True`` 再调一次补快照。**只刷成本两字段**不重算
    评分/计数(hook 链不产生新 version / review,其余字段快照仍准)。

    必要前置:``ChapterORM`` 行已存在且 ``approved_at`` 已填(由
    ``_upsert_chapter_state`` 在 APPROVED transition 时填);``chapter_versions``
    至少有 1 行(由 ``persist_chapter_version_safely`` P4-B-02 填);
    ``review_results`` 至少有 1 行(由 ``persist_review_result_safely``
    P4-B-02 填)。任一缺失 → log warning + 静默 return,不写 metrics 行。
    """
    if session_factory is None:
        return
    try:
        session = session_factory()
    except Exception as exc:  # noqa: BLE001 — swallow per P2-B-19 / P3-B-07 / P4-B-02 先例
        _log.error(
            "metrics_per_chapter_persist_failed",
            workspace_id=workspace_id,
            chapter_id=chapter_id,
            stage="open_session",
            error_type=type(exc).__name__,
            error_message=str(exc),
        )
        return
    try:
        existing = session.get(MetricsPerChapterORM, chapter_id)
        if existing is not None:
            if not refresh_existing:
                return
            # v2 w2-s2 metrics 修复 A:hook 链(summary+memory)后补刷新
            # 成本快照 — 只动成本两字段,不重算评分/计数。
            chapter_row = session.get(ChapterORM, chapter_id)
            if chapter_row is None or chapter_row.workspace_id != workspace_id:
                return
            existing.total_cost_usd = float(chapter_row.total_cost_usd)
            existing.total_tokens = _sum_tokens(chapter_row.total_token_usage)
            session.commit()
            _log.info(
                "metrics_per_chapter_cost_refreshed",
                workspace_id=workspace_id,
                chapter_id=chapter_id,
                total_cost_usd=float(chapter_row.total_cost_usd),
            )
            return

        chapter_row = session.get(ChapterORM, chapter_id)
        if chapter_row is None:
            _log.warning(
                "metrics_per_chapter_chapter_row_missing",
                workspace_id=workspace_id,
                chapter_id=chapter_id,
            )
            return
        if chapter_row.workspace_id != workspace_id:
            _log.warning(
                "metrics_per_chapter_workspace_mismatch",
                workspace_id=workspace_id,
                chapter_id=chapter_id,
                row_workspace_id=chapter_row.workspace_id,
            )
            return
        if chapter_row.approved_at is None:
            _log.warning(
                "metrics_per_chapter_not_approved",
                workspace_id=workspace_id,
                chapter_id=chapter_id,
            )
            return

        version_rows = session.execute(
            select(ChapterVersionORM)
            .where(ChapterVersionORM.chapter_id == chapter_id)
            .order_by(ChapterVersionORM.version_id)
        ).scalars().all()
        if not version_rows:
            _log.warning(
                "metrics_per_chapter_no_version",
                workspace_id=workspace_id,
                chapter_id=chapter_id,
            )
            return
        latest_version = version_rows[-1]

        latest_review = session.execute(
            select(ReviewResultORM)
            .where(ReviewResultORM.chapter_id == chapter_id)
            .order_by(ReviewResultORM.reviewed_at.desc())
            .limit(1)
        ).scalar_one_or_none()
        if latest_review is None:
            _log.warning(
                "metrics_per_chapter_no_review",
                workspace_id=workspace_id,
                chapter_id=chapter_id,
            )
            return

        rewrite_count = count_effective_content_rewrites(
            [
                (row.creation_type, row.model_used, row.content)
                for row in version_rows
            ],
            excluded_model_used=PARAGRAPH_REFLOW_MODEL_USED,
        )
        revise_count = session.scalar(
            select(func.count())
            .select_from(ChapterVersionORM)
            .where(
                ChapterVersionORM.chapter_id == chapter_id,
                ChapterVersionORM.creation_type == "revise",
            )
        ) or 0
        legacy_writer_calls = session.scalar(
            select(func.count())
            .select_from(ChapterVersionORM)
            .where(
                ChapterVersionORM.chapter_id == chapter_id,
                ChapterVersionORM.creation_type.in_(("draft", "rewrite")),
                ChapterVersionORM.model_used != PARAGRAPH_REFLOW_MODEL_USED,
            )
        ) or 0
        legacy_reviewer_calls = session.scalar(
            select(func.count())
            .select_from(ReviewResultORM)
            .where(ReviewResultORM.chapter_id == chapter_id)
        ) or 0
        provider_attempt_count = session.scalar(
            select(func.count())
            .select_from(AttemptLedgerORM)
            .where(
                AttemptLedgerORM.chapter_id == chapter_id,
                AttemptLedgerORM.attempt_kind == "provider",
            )
        ) or 0
        if provider_attempt_count:
            calls_by_agent: dict[str, int] = {
                str(agent_name): int(call_count)
                for agent_name, call_count in session.execute(
                    select(
                        AttemptLedgerORM.agent_name,
                        func.count(),
                    )
                    .where(
                        AttemptLedgerORM.chapter_id == chapter_id,
                        AttemptLedgerORM.attempt_kind == "provider",
                        AttemptLedgerORM.agent_name.in_(
                            ("writer", "reviewer", "reviser")
                        ),
                    )
                    .group_by(AttemptLedgerORM.agent_name)
                ).all()
                if agent_name is not None
            }
            writer_calls = int(calls_by_agent.get("writer", 0))
            reviewer_calls = int(calls_by_agent.get("reviewer", 0))
            reviser_calls = int(calls_by_agent.get("reviser", 0))
        else:
            writer_calls = int(legacy_writer_calls)
            reviewer_calls = int(legacy_reviewer_calls)
            reviser_calls = int(revise_count)

        total_tokens = _sum_tokens(chapter_row.total_token_usage)
        total_latency_seconds = (
            chapter_row.approved_at - chapter_row.created_at
        ).total_seconds()
        row = MetricsPerChapterORM(
            chapter_id=chapter_id,
            drafted_at=chapter_row.created_at,
            approved_at=chapter_row.approved_at,
            total_latency_seconds=total_latency_seconds,
            rewrite_count=int(rewrite_count),
            revise_count=int(revise_count),
            final_score_overall=latest_review.score_overall,
            final_score_plot_progression=latest_review.score_plot_progression,
            final_score_tension_match=latest_review.score_tension_match,
            final_score_prose_quality=latest_review.score_prose_quality,
            final_score_consistency=latest_review.score_consistency,
            final_score_voice_consistency=latest_review.score_voice_consistency,
            final_score_hook_management=latest_review.score_hook_management,
            word_count=latest_version.word_count,
            target_word_count_min=card.word_count_min,
            target_word_count_max=card.word_count_max,
            target_tension_level=card.tension_level,
            actual_tension_estimate=None,
            total_tokens=total_tokens,
            total_cost_usd=float(chapter_row.total_cost_usd),
            writer_profile=latest_version.model_used,
            writer_model=latest_version.model_used,
            writer_calls=writer_calls,
            reviewer_calls=reviewer_calls,
            reviser_calls=reviser_calls,
            cache_input_tokens=None,
            cache_hit_ratio=None,
        )
        session.add(row)
        session.commit()
        _log.info(
            "metrics_per_chapter_persisted",
            workspace_id=workspace_id,
            chapter_id=chapter_id,
            final_score_overall=latest_review.score_overall,
            total_cost_usd=float(chapter_row.total_cost_usd),
            total_tokens=total_tokens,
            total_latency_seconds=total_latency_seconds,
        )
    except Exception as exc:  # noqa: BLE001 — swallow per P2-B-19 / P3-B-07 / P4-B-02 先例
        session.rollback()
        _log.error(
            "metrics_per_chapter_persist_failed",
            workspace_id=workspace_id,
            chapter_id=chapter_id,
            stage="commit",
            error_type=type(exc).__name__,
            error_message=str(exc),
        )
    finally:
        session.close()


def _sum_tokens(total_token_usage: dict[str, Any] | None) -> int:
    """累加 ``ChapterORM.total_token_usage`` JSON dict 内所有 model 桶的
    ``input_tokens + output_tokens``。

    Shape(P3-B-07 cost_tracker 落库格式):
    ``{"<model_used>": {"input_tokens": int, "output_tokens": int}, ...}``。
    空 dict / None 返 0;非 dict 桶静默跳过(防御坏数据)。
    """
    if not total_token_usage:
        return 0
    total = 0
    for bucket in total_token_usage.values():
        if not isinstance(bucket, dict):
            continue
        total += int(bucket.get("input_tokens", 0))
        total += int(bucket.get("output_tokens", 0))
    return total
