"""P4-B-08:cost_guard 配置加载 + 判定 + structlog + WS publish 单元测试。

覆盖契约:

1. ``load_cost_guard_config``:
   - yaml 含 ``cost_guard:`` 段 → 解析字段(+ 边界:omitted 字段走默认)
   - yaml 无 ``cost_guard:`` 段 → 返字面默认(20/300/10/pause/[log,ws])
   - yaml 文件不存在 → 返字面默认
   - 字段类型错(``daily_usd_limit: "abc"``)→ ``ValidationError``
   - ``notify_via=[]`` 合法 + 仅 ``["log"]`` 合法 + 仅 ``["websocket"]`` 合法

2. ``evaluate_cost_guard`` 纯函数判定(scope 优先级 + 边界):
   - 全部 0 → ``triggered=False`` + ``scope=None``
   - 单工作区今日 > 上限 → triggered + scope=workspace_daily(优先级最高)
   - 全工作区今日 > 上限(workspace 未超 / 未传 ws_id)→ scope=daily
   - 全工作区当月 > 上限(其他不超)→ scope=monthly
   - 边界:= 上限 不触发(> 严格大于)
   - workspace_id=None 时不查 workspace_daily(防误触发跨工作区聚合)

3. ``emit_cost_guard_event`` 副作用:
   - triggered=False → 静默 no-op(无 log / 无 WS)
   - triggered=True + ``notify_via=["log"]`` → structlog ``cost_guard_triggered``
     event 字段 (workspace_id / current_usd / limit_usd / action / scope / reason)
   - triggered=True + ``notify_via=["websocket"]`` → WS publish 到
     ``"system.alerts"`` 频道,event=``cost_guard_triggered``,data 同字段
   - ws_manager.publish 抛异常 → swallow + structlog
     ``cost_guard_notify_failed``,不冒泡

4. ``trigger_cost_guard_check`` 组合 helper:
   - 一行调用 evaluate + emit
   - mock 配置 ``daily_usd_limit=0.01`` + ``daily_cost_usd=0.05`` 触发
     "暂停"路径单测(action="pause" + WS 推送)— 任务模板字面验收路径
"""

from __future__ import annotations

from collections.abc import Iterator
from pathlib import Path

import pytest
import yaml
from pydantic import ValidationError
from structlog.testing import capture_logs

from src.api.websocket.manager import WebSocketManager, manager
from src.services.cost_guard import (
    CostGuardConfig,
    CostGuardDecision,
    emit_cost_guard_event,
    evaluate_cost_guard,
    load_cost_guard_config,
    trigger_cost_guard_check,
)


@pytest.fixture(autouse=True)
def _reset_ws_manager() -> Iterator[None]:
    """每用例清空全局 ws manager,避免连接 / 订阅跨用例污染。"""
    manager.reset()
    yield
    manager.reset()


# =============================================================================
# 1. load_cost_guard_config
# =============================================================================


class TestLoadCostGuardConfig:
    def test_full_section(self, tmp_path: Path) -> None:
        path = tmp_path / "config.yaml"
        path.write_text(
            yaml.safe_dump(
                {
                    "cost_guard": {
                        "cost_currency": "rmb",
                        "daily_usd_limit": 5.0,
                        "monthly_usd_limit": 100.0,
                        "per_workspace_daily_limit": 2.0,
                        "action_on_exceed": "warn",
                        "notify_via": ["log"],
                    }
                }
            ),
            encoding="utf-8",
        )
        cfg = load_cost_guard_config(path)
        assert cfg.cost_currency == "RMB"
        assert cfg.daily_usd_limit == 5.0
        assert cfg.monthly_usd_limit == 100.0
        assert cfg.per_workspace_daily_limit == 2.0
        assert cfg.action_on_exceed == "warn"
        assert cfg.notify_via == ["log"]

    def test_partial_section_uses_defaults(self, tmp_path: Path) -> None:
        path = tmp_path / "config.yaml"
        path.write_text(
            yaml.safe_dump({"cost_guard": {"daily_usd_limit": 5.0}}),
            encoding="utf-8",
        )
        cfg = load_cost_guard_config(path)
        assert cfg.daily_usd_limit == 5.0
        # 其他字段走默认值
        assert cfg.monthly_usd_limit == 300.0
        assert cfg.per_workspace_daily_limit == 10.0
        assert cfg.action_on_exceed == "pause"
        assert cfg.notify_via == ["log", "websocket"]

    def test_missing_section_returns_defaults(self, tmp_path: Path) -> None:
        path = tmp_path / "config.yaml"
        path.write_text(yaml.safe_dump({"writer_profiles": {}}), encoding="utf-8")
        cfg = load_cost_guard_config(path)
        assert cfg == CostGuardConfig()

    def test_missing_file_returns_defaults(self, tmp_path: Path) -> None:
        path = tmp_path / "nope.yaml"
        cfg = load_cost_guard_config(path)
        assert cfg == CostGuardConfig()

    def test_invalid_field_type_raises(self, tmp_path: Path) -> None:
        path = tmp_path / "config.yaml"
        path.write_text(
            yaml.safe_dump({"cost_guard": {"daily_usd_limit": "abc"}}),
            encoding="utf-8",
        )
        with pytest.raises(ValidationError):
            load_cost_guard_config(path)

    def test_invalid_action_raises(self, tmp_path: Path) -> None:
        path = tmp_path / "config.yaml"
        path.write_text(
            yaml.safe_dump({"cost_guard": {"action_on_exceed": "bogus"}}),
            encoding="utf-8",
        )
        with pytest.raises(ValidationError):
            load_cost_guard_config(path)

    def test_empty_notify_via_allowed(self, tmp_path: Path) -> None:
        path = tmp_path / "config.yaml"
        path.write_text(
            yaml.safe_dump({"cost_guard": {"notify_via": []}}),
            encoding="utf-8",
        )
        cfg = load_cost_guard_config(path)
        assert cfg.notify_via == []


# =============================================================================
# 2. evaluate_cost_guard
# =============================================================================


class TestEvaluateCostGuard:
    def _cfg(
        self,
        daily: float = 20.0,
        monthly: float = 300.0,
        ws: float = 10.0,
        action: str = "pause",
    ) -> CostGuardConfig:
        return CostGuardConfig(
            daily_usd_limit=daily,
            monthly_usd_limit=monthly,
            per_workspace_daily_limit=ws,
            action_on_exceed=action,  # type: ignore[arg-type]
            notify_via=["log", "websocket"],
        )

    def test_all_zero_not_triggered(self) -> None:
        d = evaluate_cost_guard(config=self._cfg())
        assert d.triggered is False
        assert d.scope is None
        assert d.reason == "under all limits"

    def test_workspace_daily_takes_priority(self) -> None:
        # daily 也超,但 workspace_daily 优先级最高
        d = evaluate_cost_guard(
            config=self._cfg(daily=20.0, ws=10.0),
            daily_cost_usd=30.0,
            workspace_today_cost_usd=15.0,
            workspace_id="ws-a",
        )
        assert d.triggered is True
        assert d.scope == "workspace_daily"
        assert d.current_usd == 15.0
        assert d.limit_usd == 10.0
        assert d.workspace_id == "ws-a"
        assert "ws-a" in d.reason

    def test_daily_when_workspace_under(self) -> None:
        d = evaluate_cost_guard(
            config=self._cfg(daily=20.0, ws=10.0),
            daily_cost_usd=25.0,
            workspace_today_cost_usd=5.0,
            workspace_id="ws-a",
        )
        assert d.triggered is True
        assert d.scope == "daily"
        assert d.current_usd == 25.0
        assert d.limit_usd == 20.0

    def test_monthly(self) -> None:
        d = evaluate_cost_guard(
            config=self._cfg(daily=20.0, monthly=300.0),
            daily_cost_usd=10.0,
            monthly_cost_usd=500.0,
        )
        assert d.triggered is True
        assert d.scope == "monthly"
        assert d.current_usd == 500.0
        assert d.limit_usd == 300.0

    def test_strict_greater_than_not_equal(self) -> None:
        d = evaluate_cost_guard(
            config=self._cfg(daily=20.0),
            daily_cost_usd=20.0,
        )
        assert d.triggered is False

    def test_workspace_id_none_skips_workspace_check(self) -> None:
        # 没传 workspace_id 时不查 workspace_daily(避免拿全局合计当单工作区判)
        d = evaluate_cost_guard(
            config=self._cfg(daily=20.0, ws=5.0),
            daily_cost_usd=10.0,
            workspace_today_cost_usd=99.0,  # 即使数字超过 ws limit
            workspace_id=None,
        )
        # daily=10 < 20 不触发,workspace_id=None 不查 ws_daily
        assert d.triggered is False

    def test_action_propagated_from_config(self) -> None:
        d = evaluate_cost_guard(
            config=self._cfg(action="warn"),
            daily_cost_usd=25.0,
        )
        assert d.action == "warn"


# =============================================================================
# 3. emit_cost_guard_event
# =============================================================================


class TestEmitCostGuardEvent:
    def _triggered(self) -> CostGuardDecision:
        return CostGuardDecision(
            triggered=True,
            action="pause",
            scope="daily",
            current_usd=25.0,
            limit_usd=20.0,
            reason="daily cost $25.0000 > $20.0000 limit",
            workspace_id="ws-a",
        )

    def test_not_triggered_noop(self) -> None:
        decision = CostGuardDecision(
            triggered=False,
            action="pause",
            scope=None,
            current_usd=0.0,
            limit_usd=0.0,
            reason="under all limits",
            workspace_id=None,
        )
        ws = WebSocketManager()
        with capture_logs() as logs:
            emit_cost_guard_event(decision, ws_manager=ws)
        # 无 emit
        assert not any(e.get("event") == "cost_guard_triggered" for e in logs)

    def test_log_only(self) -> None:
        with capture_logs() as logs:
            emit_cost_guard_event(self._triggered(), notify_via=("log",))
        events = [e for e in logs if e.get("event") == "cost_guard_triggered"]
        assert len(events) == 1
        e = events[0]
        assert e["workspace_id"] == "ws-a"
        assert e["current_usd"] == 25.0
        assert e["limit_usd"] == 20.0
        assert e["action"] == "pause"
        assert e["scope"] == "daily"
        assert "daily cost" in e["reason"]
        assert e["log_level"] == "warning"

    def test_websocket_publish(self) -> None:
        # 用全局 manager(WS publish helper 内部要求 set 内 channel 匹配)
        published: list[dict] = []
        orig_publish = manager.publish

        def _spy(channel: str, event: str, data: dict) -> None:
            published.append({"channel": channel, "event": event, "data": data})
            orig_publish(channel, event, data)

        manager.publish = _spy  # type: ignore[method-assign]
        try:
            emit_cost_guard_event(
                self._triggered(),
                notify_via=("websocket",),
                ws_manager=manager,
            )
        finally:
            manager.publish = orig_publish  # type: ignore[method-assign]

        assert len(published) == 1
        msg = published[0]
        assert msg["channel"] == "system.alerts"
        assert msg["event"] == "cost_guard_triggered"
        assert msg["data"] == {
            "workspace_id": "ws-a",
            "current_usd": 25.0,
            "limit_usd": 20.0,
            "currency": "USD",
            "action": "pause",
            "scope": "daily",
            "reason": "daily cost $25.0000 > $20.0000 limit",
        }

    def test_websocket_publish_exception_swallowed(self) -> None:
        class _BoomManager:
            def publish(self, channel: str, event: str, data: dict) -> None:
                raise RuntimeError("boom")

        with capture_logs() as logs:
            # 不应冒泡
            emit_cost_guard_event(
                self._triggered(),
                notify_via=("websocket",),
                ws_manager=_BoomManager(),  # type: ignore[arg-type]
            )
        assert any(e.get("event") == "cost_guard_notify_failed" for e in logs)

    def test_no_ws_when_manager_none(self) -> None:
        with capture_logs() as logs:
            emit_cost_guard_event(
                self._triggered(),
                notify_via=("log", "websocket"),
                ws_manager=None,
            )
        # 仍 log,但无 WS 异常
        assert any(e.get("event") == "cost_guard_triggered" for e in logs)


# =============================================================================
# 4. trigger_cost_guard_check 组合 helper(任务模板字面 acceptance 路径)
# =============================================================================


class TestTriggerCostGuardCheck:
    def test_acceptance_template_path(self) -> None:
        """任务模板字面 acceptance:``daily_usd_limit=0.01`` + cost > 0.01
        触发 ``action=pause`` + WS 推送 ``system.alerts``。

        本测试用 mock 配置 + 直接调 trigger 验证完整链路;真 LLM 端到端
        由 P4-acceptance 任务承担。
        """
        cfg = CostGuardConfig(
            daily_usd_limit=0.01,
            monthly_usd_limit=10.0,
            per_workspace_daily_limit=0.005,
            action_on_exceed="pause",
            notify_via=["log", "websocket"],
        )
        published: list[dict] = []
        orig_publish = manager.publish

        def _spy(channel: str, event: str, data: dict) -> None:
            published.append({"channel": channel, "event": event, "data": data})
            orig_publish(channel, event, data)

        manager.publish = _spy  # type: ignore[method-assign]
        try:
            with capture_logs() as logs:
                decision = trigger_cost_guard_check(
                    config=cfg,
                    daily_cost_usd=0.05,
                    workspace_today_cost_usd=0.05,
                    workspace_id="ws-a",
                    ws_manager=manager,
                )
        finally:
            manager.publish = orig_publish  # type: ignore[method-assign]

        # 决策对
        assert decision.triggered is True
        assert decision.action == "pause"
        assert decision.scope == "workspace_daily"
        # structlog event 发了
        assert any(e.get("event") == "cost_guard_triggered" for e in logs)
        # WS 推送了 1 条到 system.alerts
        assert len(published) == 1
        assert published[0]["channel"] == "system.alerts"
        assert published[0]["event"] == "cost_guard_triggered"

    def test_under_limit_no_emit(self) -> None:
        cfg = CostGuardConfig()  # 默认 20/300/10
        with capture_logs() as logs:
            decision = trigger_cost_guard_check(
                config=cfg,
                daily_cost_usd=1.0,
                workspace_today_cost_usd=0.5,
                workspace_id="ws-a",
                ws_manager=manager,
            )
        assert decision.triggered is False
        assert not any(e.get("event") == "cost_guard_triggered" for e in logs)
