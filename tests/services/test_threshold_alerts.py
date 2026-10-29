"""P4-B-08:OBSERVABILITY §3.3 阈值告警 8 条判定 + structlog + 可选 WS 推送测试。

覆盖契约:

- 8 个 ``check_*`` 函数 × 通过 / 失败 / 边界 = 24 单测
- ``emit_threshold_alerts`` 副作用 = 4 单测(structlog level 映射 / WS 推送 /
  WS 异常 swallow / workspace_id 透传)

WS 推送命中表(§3.3 字面"WS 推送"):
- ``check_consecutive_low_scores`` → notify_ws=True
- ``check_api_error_rate`` → notify_ws=True
- 其他 6 条 → notify_ws=False
"""

from __future__ import annotations

from collections.abc import Iterator

import pytest
from structlog.testing import capture_logs

from src.api.websocket.manager import manager
from src.services.threshold_alerts import (
    ThresholdAlert,
    check_api_error_rate,
    check_avg_chapter_latency,
    check_consecutive_low_scores,
    check_frontend_error_count,
    check_hard_constraint_failure_rate,
    check_revise_count,
    check_rewrite_count,
    check_tension_gap,
    emit_threshold_alerts,
)


@pytest.fixture(autouse=True)
def _reset_ws_manager() -> Iterator[None]:
    manager.reset()
    yield
    manager.reset()


# =============================================================================
# rewrite_count / revise_count(无 WS)
# =============================================================================


class TestCheckRewriteCount:
    def test_under_threshold(self) -> None:
        assert check_rewrite_count(1, chapter_id="ch-0001") is None

    def test_equal_threshold_triggers(self) -> None:
        # § 3.3 字面 "≥ 2" 包含等号
        alert = check_rewrite_count(2, chapter_id="ch-0001")
        assert alert is not None
        assert alert.code == "rewrite_count_high"
        assert alert.severity == "warning"
        assert alert.notify_ws is False
        assert alert.context["chapter_id"] == "ch-0001"
        assert alert.context["current"] == 2
        assert alert.context["threshold"] == 2

    def test_above_threshold_triggers(self) -> None:
        alert = check_rewrite_count(5, chapter_id="ch-0002")
        assert alert is not None
        assert alert.context["current"] == 5


class TestCheckReviseCount:
    def test_under_threshold(self) -> None:
        assert check_revise_count(1, chapter_id="ch-0001") is None

    def test_at_threshold_triggers(self) -> None:
        alert = check_revise_count(2, chapter_id="ch-0001")
        assert alert is not None
        assert alert.code == "revise_count_high"
        assert alert.severity == "warning"
        assert alert.notify_ws is False

    def test_above_threshold(self) -> None:
        alert = check_revise_count(3, chapter_id="ch-0001")
        assert alert is not None
        assert alert.context["current"] == 3


# =============================================================================
# low_score_streak(WS)
# =============================================================================


class TestCheckConsecutiveLowScores:
    def test_too_few_scores(self) -> None:
        assert check_consecutive_low_scores([7.0, 7.0]) is None

    def test_all_above_threshold(self) -> None:
        assert check_consecutive_low_scores([8.0, 8.0, 8.0]) is None

    def test_last_three_all_below_triggers(self) -> None:
        alert = check_consecutive_low_scores([8.0, 7.0, 7.2, 7.3])
        assert alert is not None
        assert alert.code == "low_score_streak"
        assert alert.severity == "warning"
        assert alert.notify_ws is True
        assert alert.context["scores"] == [7.0, 7.2, 7.3]

    def test_boundary_seven_point_five_not_triggers(self) -> None:
        # 严格小于(< 7.5),= 7.5 不触发
        assert check_consecutive_low_scores([7.5, 7.5, 7.5]) is None

    def test_only_first_two_below_not_triggers(self) -> None:
        # 末尾不是连续低分(最后一条 8.0)
        assert check_consecutive_low_scores([7.0, 7.0, 8.0]) is None


# =============================================================================
# tension_gap(无 WS)
# =============================================================================


class TestCheckTensionGap:
    def test_none_values_no_trigger(self) -> None:
        assert check_tension_gap(None, 5.0, chapter_id="ch-0001") is None
        assert check_tension_gap(5.0, None, chapter_id="ch-0001") is None

    def test_within_threshold(self) -> None:
        assert check_tension_gap(5.0, 6.5, chapter_id="ch-0001") is None

    def test_boundary_exactly_two_no_trigger(self) -> None:
        assert check_tension_gap(5.0, 7.0, chapter_id="ch-0001") is None

    def test_above_threshold_triggers(self) -> None:
        alert = check_tension_gap(5.0, 8.0, chapter_id="ch-0001")
        assert alert is not None
        assert alert.code == "tension_gap_high"
        assert alert.severity == "warning"
        assert alert.notify_ws is False
        assert alert.context["gap"] == 3.0


# =============================================================================
# avg_chapter_latency(无 WS)
# =============================================================================


class TestCheckAvgChapterLatency:
    def test_none_no_trigger(self) -> None:
        assert check_avg_chapter_latency(None) is None

    def test_within_threshold(self) -> None:
        assert check_avg_chapter_latency(300.0) is None
        assert check_avg_chapter_latency(600.0) is None  # = 阈值不触发

    def test_above_triggers(self) -> None:
        alert = check_avg_chapter_latency(700.0)
        assert alert is not None
        assert alert.code == "avg_chapter_latency_high"
        assert alert.severity == "warning"
        assert alert.notify_ws is False


# =============================================================================
# hard_constraint_failure_rate(无 WS,ERROR level)
# =============================================================================


class TestCheckHardConstraintFailureRate:
    def test_zero_runs_no_trigger(self) -> None:
        assert check_hard_constraint_failure_rate(0, 0) is None

    def test_under_threshold(self) -> None:
        # 2/10 = 20% < 30%
        assert check_hard_constraint_failure_rate(10, 2) is None

    def test_boundary_thirty_percent_no_trigger(self) -> None:
        # 3/10 = 30% ≤ 30% 不触发(严格 >)
        assert check_hard_constraint_failure_rate(10, 3) is None

    def test_above_triggers_error(self) -> None:
        alert = check_hard_constraint_failure_rate(10, 4)
        assert alert is not None
        assert alert.code == "hard_constraint_failure_rate_high"
        assert alert.severity == "error"
        assert alert.notify_ws is False
        assert alert.context["rate"] == pytest.approx(0.4)


# =============================================================================
# api_error_rate(WS,ERROR level)
# =============================================================================


class TestCheckApiErrorRate:
    def test_zero_requests_no_trigger(self) -> None:
        assert check_api_error_rate(0, 0) is None

    def test_under_threshold(self) -> None:
        # 5/100 = 5% < 10%
        assert check_api_error_rate(100, 5) is None

    def test_boundary_ten_percent_no_trigger(self) -> None:
        # 10/100 = 10% ≤ 10% 不触发(严格 >)
        assert check_api_error_rate(100, 10) is None

    def test_above_triggers_error_with_ws(self) -> None:
        alert = check_api_error_rate(100, 15)
        assert alert is not None
        assert alert.code == "api_error_rate_high"
        assert alert.severity == "error"
        assert alert.notify_ws is True
        assert alert.context["rate"] == pytest.approx(0.15)


# =============================================================================
# frontend_error_count(无 WS)
# =============================================================================


class TestCheckFrontendErrorCount:
    def test_under_threshold(self) -> None:
        assert check_frontend_error_count(15) is None

    def test_boundary_no_trigger(self) -> None:
        # = 20 不触发(严格 >)
        assert check_frontend_error_count(20) is None

    def test_above_triggers(self) -> None:
        alert = check_frontend_error_count(25)
        assert alert is not None
        assert alert.code == "frontend_error_count_high"
        assert alert.severity == "warning"
        assert alert.notify_ws is False


# =============================================================================
# emit_threshold_alerts
# =============================================================================


class TestEmitThresholdAlerts:
    def test_severity_maps_to_log_level(self) -> None:
        alerts = [
            ThresholdAlert(
                code="rewrite_count_high",
                severity="warning",
                message="m1",
                context={"a": 1},
            ),
            ThresholdAlert(
                code="api_error_rate_high",
                severity="error",
                message="m2",
                context={"b": 2},
                notify_ws=True,
            ),
        ]
        with capture_logs() as logs:
            emit_threshold_alerts(alerts, workspace_id="ws-a", ws_manager=None)

        events = [e for e in logs if e.get("event") == "threshold_alert"]
        assert len(events) == 2
        codes = {e["code"]: e["log_level"] for e in events}
        assert codes["rewrite_count_high"] == "warning"
        assert codes["api_error_rate_high"] == "error"
        # workspace_id 透传
        assert all(e["workspace_id"] == "ws-a" for e in events)
        # context 字段被展开
        e1 = next(e for e in events if e["code"] == "rewrite_count_high")
        assert e1["a"] == 1

    def test_ws_publish_only_notify_ws_true(self) -> None:
        alerts = [
            ThresholdAlert(
                code="rewrite_count_high",
                severity="warning",
                message="m1",
                notify_ws=False,
            ),
            ThresholdAlert(
                code="low_score_streak",
                severity="warning",
                message="m2",
                context={"scores": [7.0, 7.1, 7.2]},
                notify_ws=True,
            ),
            ThresholdAlert(
                code="api_error_rate_high",
                severity="error",
                message="m3",
                context={"rate": 0.15},
                notify_ws=True,
            ),
        ]
        published: list[dict] = []
        orig_publish = manager.publish

        def _spy(channel: str, event: str, data: dict) -> None:
            published.append({"channel": channel, "event": event, "data": data})
            orig_publish(channel, event, data)

        manager.publish = _spy  # type: ignore[method-assign]
        try:
            emit_threshold_alerts(alerts, workspace_id="ws-a", ws_manager=manager)
        finally:
            manager.publish = orig_publish  # type: ignore[method-assign]

        # 仅 2 条 notify_ws=True 推送
        assert len(published) == 2
        assert all(p["channel"] == "system.alerts" for p in published)
        codes = {p["data"]["code"] for p in published}
        assert codes == {"low_score_streak", "api_error_rate_high"}
        # data 字段含完整 workspace_id / severity / message / context
        for p in published:
            assert p["event"] == "threshold_alert_triggered"
            assert p["data"]["workspace_id"] == "ws-a"
            assert "severity" in p["data"]
            assert "message" in p["data"]
            assert "context" in p["data"]

    def test_ws_exception_swallowed_per_alert(self) -> None:
        class _BoomManager:
            def publish(self, channel: str, event: str, data: dict) -> None:
                raise RuntimeError("boom")

        alerts = [
            ThresholdAlert(
                code="low_score_streak",
                severity="warning",
                message="m1",
                notify_ws=True,
            ),
        ]
        with capture_logs() as logs:
            emit_threshold_alerts(
                alerts, workspace_id="ws-a", ws_manager=_BoomManager()  # type: ignore[arg-type]
            )
        # 主 event 仍 emit + notify_failed 配套 emit;主 emit 不被 WS 异常打断
        assert any(e.get("event") == "threshold_alert" for e in logs)
        assert any(e.get("event") == "threshold_alert_notify_failed" for e in logs)

    def test_empty_alerts_noop(self) -> None:
        with capture_logs() as logs:
            emit_threshold_alerts([], ws_manager=manager)
        assert not logs
