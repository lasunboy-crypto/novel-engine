"""阈值告警判定服务(P4-B-08)。

对齐 OBSERVABILITY §3.3(关键阈值与告警表)+ API_CONTRACT §5.3
``system.alerts`` 频道 + structlog WARNING / ERROR level 字面要求。

职责
====

本模块提供 OBSERVABILITY §3.3 字面阈值表 8 条判定函数(单日 cost 超限
归 ``cost_guard.py`` 处理,本表余 8 条):

1. ``check_rewrite_count``           — 单章 rewrite_count ≥ 2 → WARNING(无 WS)
2. ``check_revise_count``            — 单章 revise_count ≥ 2 → WARNING(无 WS)
3. ``check_consecutive_low_scores``  — 连续 3 章 final_score < 7.5 → WARNING + WS
4. ``check_tension_gap``             — |target - actual| > 2 → WARNING(无 WS)
5. ``check_avg_chapter_latency``     — > 600s(10min)→ WARNING(无 WS)
6. ``check_hard_constraint_failure_rate`` — 失败率 > 30% → ERROR(无 WS)
7. ``check_api_error_rate``          — 5min 错误率 > 10% → ERROR + WS
8. ``check_frontend_error_count``    — 1h 前端错误 > 20 → WARNING(无 WS)

记忆健康度 critical(P4-B-09)+ 风格漂移 critical(P4-B-10)由相应后续
任务单独实现,不在本表。

设计要点
========

- **预读数据接口**(P4-B-07 沉淀):每个判定函数接受 caller 预读的纯
  数字 / 列表(``rewrite_count: int`` / ``recent_scores: list[float]`` /
  ``api_requests_5min`` 等),不依赖 SQLAlchemy session。caller(orchestrator
  / 定时巡检 / API 端点)自己 query metrics_daily / system_metrics_daily /
  metrics_per_chapter 再调判定。
- **判定函数返 ``ThresholdAlert | None``**:None 表"未触发",``ThresholdAlert``
  实例含 ``code`` / ``severity`` / ``message`` / ``context`` / ``notify_ws``
  五字段。caller 收集所有非 None 结果 → 调 ``emit_threshold_alerts`` 一次性
  发 structlog + WS publish。
- **structlog level 字面**:``severity="warning"`` → ``_log.warning``;
  ``severity="error"`` → ``_log.error``;严格对齐 OBSERVABILITY §3.3
  "日志 WARNING / 日志 ERROR" 字面要求。
- **WS publish 仅 3 条命中**:``check_consecutive_low_scores`` /
  (cost_guard 由独立模块处理)/ ``check_api_error_rate``。其他 6 条
  ``notify_ws=False``(对齐 §3.3 字面"日志 WARNING"无 "WS 推送")。
- **workspace_id 透传纪律**(P3-B-14 沉淀):caller 显式透传给判定函数 +
  ``emit_threshold_alerts``,本模块不读 metrics 字段。

事件名约定
==========

structlog event 名:``threshold_alert``(单一事件名,``severity`` /
``code`` 作字段)。理由:① OBSERVABILITY §2.4 字面无 ``threshold_alert_*``
分项,统一一个事件 + ``code`` 字段化让 LogsPage 按 code 维度聚合更通用;
② 不增加 §2.4 字面 v2_wishlist 待补数。
WS event 名:对齐 API_CONTRACT §5.3 字面 ``cost_guard_triggered`` /
``memory_health_critical`` 同模式 — 本表用 ``threshold_alert_triggered``
作 WS event 名,data 字段含 code / severity / current / threshold /
message。
"""

from __future__ import annotations

from collections.abc import Iterable, Sequence
from dataclasses import dataclass, field
from typing import Any, Literal

import structlog

from src.api.websocket.channels import system_alerts_channel
from src.api.websocket.manager import WebSocketManager

_log = structlog.get_logger(__name__)


AlertSeverity = Literal["warning", "error"]
"""structlog level 字面(OBSERVABILITY §3.3 "WARNING" / "ERROR" 两档)。"""

LOW_SCORE_THRESHOLD: float = 7.5
"""``check_consecutive_low_scores`` 字面阈值(§3.3 "连续 3 章 < 7.5")。"""

LOW_SCORE_STREAK_LENGTH: int = 3
"""连续低分章节数阈值(§3.3 "连续 3 章")。"""

TENSION_GAP_THRESHOLD: float = 2.0
"""张力差阈值(§3.3 "张力实际 vs 目标差 > 2")。"""

AVG_LATENCY_SECONDS_THRESHOLD: float = 600.0
"""平均章节耗时阈值秒数(§3.3 "> 10 分钟")。"""

HARD_CONSTRAINT_FAILURE_RATE_THRESHOLD: float = 0.30
"""硬约束失败率阈值(§3.3 "> 30%")。"""

API_ERROR_RATE_THRESHOLD: float = 0.10
"""5min API 错误率阈值(§3.3 "> 10%")。"""

FRONTEND_ERROR_COUNT_THRESHOLD: int = 20
"""1h 前端错误数阈值(§3.3 "> 20")。"""

REWRITE_COUNT_THRESHOLD: int = 2
"""单章 rewrite_count 阈值(§3.3 "≥ 2")。"""

REVISE_COUNT_THRESHOLD: int = 2
"""单章 revise_count 阈值(§3.3 "≥ 2")。"""


@dataclass(frozen=True)
class ThresholdAlert:
    """单条阈值触发结果。

    - ``code``:字符串 ID(``"rewrite_count_high"`` / ``"low_score_streak"``
      等),供 LogsPage 按 code 维度聚合 + 前端徽章渲染。
    - ``severity``:``"warning"`` / ``"error"``(§3.3 字面对齐 structlog
      level)。
    - ``message``:可读字符串,作 structlog 与 WS data 字段透传。
    - ``context``:字典(``{"current": ..., "threshold": ...}`` 等),
      结构化字段。
    - ``notify_ws``:是否 WS 推送(对齐 §3.3 字面"WS 推送 system.alerts"
      标记;只有 3 条命中)。
    """

    code: str
    severity: AlertSeverity
    message: str
    context: dict[str, Any] = field(default_factory=dict)
    notify_ws: bool = False


# =============================================================================
# §3.3 字面阈值判定函数(纯函数,无 IO)
# =============================================================================


def check_rewrite_count(rewrite_count: int, *, chapter_id: str) -> ThresholdAlert | None:
    """单章 ``rewrite_count >= 2`` → WARNING(无 WS,§3.3 字面"日志 WARNING +
    前端徽章")。

    前端徽章渲染由 P4-F-03 / 04 通过 ``metrics_per_chapter`` 端点拉数据
    显示,不走 WS 推送。
    """
    if rewrite_count < REWRITE_COUNT_THRESHOLD:
        return None
    return ThresholdAlert(
        code="rewrite_count_high",
        severity="warning",
        message=(
            f"chapter {chapter_id} rewrite_count={rewrite_count} "
            f">= {REWRITE_COUNT_THRESHOLD}"
        ),
        context={
            "chapter_id": chapter_id,
            "current": rewrite_count,
            "threshold": REWRITE_COUNT_THRESHOLD,
        },
        notify_ws=False,
    )


def check_revise_count(revise_count: int, *, chapter_id: str) -> ThresholdAlert | None:
    """单章 ``revise_count >= 2`` → WARNING(无 WS,§3.3 字面)。"""
    if revise_count < REVISE_COUNT_THRESHOLD:
        return None
    return ThresholdAlert(
        code="revise_count_high",
        severity="warning",
        message=(
            f"chapter {chapter_id} revise_count={revise_count} "
            f">= {REVISE_COUNT_THRESHOLD}"
        ),
        context={
            "chapter_id": chapter_id,
            "current": revise_count,
            "threshold": REVISE_COUNT_THRESHOLD,
        },
        notify_ws=False,
    )


def check_consecutive_low_scores(
    recent_scores: Sequence[float],
) -> ThresholdAlert | None:
    """连续 3 章 ``final_score_overall < 7.5`` → WARNING + WS(§3.3 字面)。

    ``recent_scores`` 按时间 DESC 或 ASC 都可(本判定只看"最后 3 条全 <
    阈值");caller 通常传按 ``approved_at`` 排序的最近 N 条章节 score 列表。
    长度 < 3 时静默返 None(数据不足)。
    """
    if len(recent_scores) < LOW_SCORE_STREAK_LENGTH:
        return None
    last_n = list(recent_scores)[-LOW_SCORE_STREAK_LENGTH:]
    if any(s >= LOW_SCORE_THRESHOLD for s in last_n):
        return None
    return ThresholdAlert(
        code="low_score_streak",
        severity="warning",
        message=(
            f"last {LOW_SCORE_STREAK_LENGTH} chapters all scored < "
            f"{LOW_SCORE_THRESHOLD} (scores={last_n})"
        ),
        context={
            "streak_length": LOW_SCORE_STREAK_LENGTH,
            "threshold": LOW_SCORE_THRESHOLD,
            "scores": last_n,
        },
        notify_ws=True,
    )


def check_tension_gap(
    target_tension: float | None,
    actual_tension: float | None,
    *,
    chapter_id: str,
) -> ThresholdAlert | None:
    """``|target - actual| > 2`` → WARNING(无 WS,§3.3 字面)。

    任一为 None 时静默返 None(数据缺失不触发)。
    """
    if target_tension is None or actual_tension is None:
        return None
    gap = abs(target_tension - actual_tension)
    if gap <= TENSION_GAP_THRESHOLD:
        return None
    return ThresholdAlert(
        code="tension_gap_high",
        severity="warning",
        message=(
            f"chapter {chapter_id} tension gap {gap:.2f} > "
            f"{TENSION_GAP_THRESHOLD} (target={target_tension}, "
            f"actual={actual_tension})"
        ),
        context={
            "chapter_id": chapter_id,
            "target": target_tension,
            "actual": actual_tension,
            "gap": gap,
            "threshold": TENSION_GAP_THRESHOLD,
        },
        notify_ws=False,
    )


def check_avg_chapter_latency(
    avg_latency_seconds: float | None,
) -> ThresholdAlert | None:
    """平均章节耗时 > 600s(10 min)→ WARNING(无 WS,§3.3 字面)。"""
    if avg_latency_seconds is None:
        return None
    if avg_latency_seconds <= AVG_LATENCY_SECONDS_THRESHOLD:
        return None
    return ThresholdAlert(
        code="avg_chapter_latency_high",
        severity="warning",
        message=(
            f"avg chapter latency {avg_latency_seconds:.1f}s > "
            f"{AVG_LATENCY_SECONDS_THRESHOLD:.0f}s"
        ),
        context={
            "current": avg_latency_seconds,
            "threshold": AVG_LATENCY_SECONDS_THRESHOLD,
        },
        notify_ws=False,
    )


def check_hard_constraint_failure_rate(
    total_runs: int,
    failed_runs: int,
) -> ThresholdAlert | None:
    """任一硬约束失败率 > 30% → ERROR(无 WS,§3.3 字面)。

    ``total_runs == 0`` 时静默 None(无数据,避免除零)。
    """
    if total_runs <= 0:
        return None
    rate = failed_runs / total_runs
    if rate <= HARD_CONSTRAINT_FAILURE_RATE_THRESHOLD:
        return None
    return ThresholdAlert(
        code="hard_constraint_failure_rate_high",
        severity="error",
        message=(
            f"hard constraint failure rate {rate:.2%} > "
            f"{HARD_CONSTRAINT_FAILURE_RATE_THRESHOLD:.0%} "
            f"({failed_runs}/{total_runs})"
        ),
        context={
            "total_runs": total_runs,
            "failed_runs": failed_runs,
            "rate": rate,
            "threshold": HARD_CONSTRAINT_FAILURE_RATE_THRESHOLD,
        },
        notify_ws=False,
    )


def check_api_error_rate(
    api_requests_5min: int,
    api_errors_5min: int,
) -> ThresholdAlert | None:
    """5min API 错误率 > 10% → ERROR + WS(§3.3 字面"日志 ERROR + WS 推送")。

    ``api_requests_5min == 0`` 时静默 None(避免除零)。
    """
    if api_requests_5min <= 0:
        return None
    rate = api_errors_5min / api_requests_5min
    if rate <= API_ERROR_RATE_THRESHOLD:
        return None
    return ThresholdAlert(
        code="api_error_rate_high",
        severity="error",
        message=(
            f"API error rate (5min) {rate:.2%} > "
            f"{API_ERROR_RATE_THRESHOLD:.0%} "
            f"({api_errors_5min}/{api_requests_5min})"
        ),
        context={
            "requests_5min": api_requests_5min,
            "errors_5min": api_errors_5min,
            "rate": rate,
            "threshold": API_ERROR_RATE_THRESHOLD,
        },
        notify_ws=True,
    )


def check_frontend_error_count(
    frontend_errors_last_hour: int,
) -> ThresholdAlert | None:
    """1h 前端错误数 > 20 → WARNING(无 WS,§3.3 字面)。"""
    if frontend_errors_last_hour <= FRONTEND_ERROR_COUNT_THRESHOLD:
        return None
    return ThresholdAlert(
        code="frontend_error_count_high",
        severity="warning",
        message=(
            f"frontend errors in last hour {frontend_errors_last_hour} > "
            f"{FRONTEND_ERROR_COUNT_THRESHOLD}"
        ),
        context={
            "current": frontend_errors_last_hour,
            "threshold": FRONTEND_ERROR_COUNT_THRESHOLD,
        },
        notify_ws=False,
    )


# =============================================================================
# 副作用 helper(structlog + 可选 WS publish)
# =============================================================================


def emit_threshold_alerts(
    alerts: Iterable[ThresholdAlert],
    *,
    workspace_id: str | None = None,
    ws_manager: WebSocketManager | None = None,
) -> None:
    """批量发 structlog + 对 ``notify_ws=True`` 的告警 WS publish。

    OBSERVABILITY §3.3 字面:WARNING 走 ``_log.warning`` / ERROR 走
    ``_log.error``;``severity`` 字段直接映射,structlog event 名统一为
    ``threshold_alert``(``code`` 字段化区分)。

    ``workspace_id`` 透传纪律(P3-B-14):caller 显式传,本函数不读
    alert.context 中字段自动猜。``workspace_id=None`` 表跨工作区告警
    (``api_error_rate`` / ``frontend_error_count`` 系统级)。

    ``ws_manager=None`` → 不 WS publish(测试 / 无 WS 上下文)。WS publish
    单条告警失败 swallow + 单独 structlog error,不影响其他告警的推送。
    """
    for alert in alerts:
        log_method = _log.error if alert.severity == "error" else _log.warning
        log_method(
            "threshold_alert",
            workspace_id=workspace_id,
            code=alert.code,
            severity=alert.severity,
            message=alert.message,
            **alert.context,
        )
        if alert.notify_ws and ws_manager is not None:
            try:
                ws_manager.publish(
                    channel=system_alerts_channel(),
                    event="threshold_alert_triggered",
                    data={
                        "workspace_id": workspace_id,
                        "code": alert.code,
                        "severity": alert.severity,
                        "message": alert.message,
                        "context": dict(alert.context),
                    },
                )
            except Exception as exc:  # noqa: BLE001 — swallow per *_safely 先例
                _log.error(
                    "threshold_alert_notify_failed",
                    stage="ws_publish",
                    code=alert.code,
                    error_type=type(exc).__name__,
                    error_message=str(exc),
                )


__all__ = [
    "API_ERROR_RATE_THRESHOLD",
    "AVG_LATENCY_SECONDS_THRESHOLD",
    "AlertSeverity",
    "FRONTEND_ERROR_COUNT_THRESHOLD",
    "HARD_CONSTRAINT_FAILURE_RATE_THRESHOLD",
    "LOW_SCORE_STREAK_LENGTH",
    "LOW_SCORE_THRESHOLD",
    "REVISE_COUNT_THRESHOLD",
    "REWRITE_COUNT_THRESHOLD",
    "TENSION_GAP_THRESHOLD",
    "ThresholdAlert",
    "check_api_error_rate",
    "check_avg_chapter_latency",
    "check_consecutive_low_scores",
    "check_frontend_error_count",
    "check_hard_constraint_failure_rate",
    "check_revise_count",
    "check_rewrite_count",
    "check_tension_gap",
    "emit_threshold_alerts",
]
