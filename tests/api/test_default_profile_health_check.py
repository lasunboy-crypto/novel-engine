"""v1.1 wave 2 session 3 H2.6 启动期 default profile health check 单测。

对齐 ``src/api/main.py::_run_default_profile_health_check`` 的 R8 启动安全口径：
本地 provider 可直接报告可用，需要凭据的 provider 延迟到首次真实 runtime
检查；lifespan 不解密凭据。

测试覆盖:

- **本地路径**:info 事件 ``default_profile_health_check_complete``
- **外部路径**:info 事件 ``default_profile_health_check_deferred``
- **配置加载失败**:swallow + ``default_profile_health_check_failed`` error
  事件,不抛(对齐既有 startup scan fail-soft 模式)
"""

from __future__ import annotations

from types import SimpleNamespace
from typing import Any

import pytest
from structlog.testing import capture_logs


@pytest.fixture(autouse=True)
def _ensure_info_level_for_capture_logs() -> None:
    """structlog.testing.capture_logs 默认抓 INFO+,本测 info / warn / error 全抓。"""
    import logging

    logging.getLogger().setLevel(logging.INFO)


def _find_event(logs: list[dict[str, Any]], event_name: str) -> dict[str, Any] | None:
    for entry in logs:
        if entry.get("event") == event_name:
            return entry
    return None


def test_default_profile_health_check_usable_path(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """本地 provider 不涉及凭据，可在启动期直接确认。"""
    from src.api.main import _run_default_profile_health_check
    from src.core import config_loader

    cfg = SimpleNamespace(
        routing=SimpleNamespace(default="local"),
        profiles={"local": SimpleNamespace(provider="ollama")},
    )
    monkeypatch.setattr(
        config_loader,
        "load_writer_profiles_config",
        lambda _path: cfg,
    )

    with capture_logs() as logs:
        _run_default_profile_health_check()

    complete = _find_event(logs, "default_profile_health_check_complete")
    assert complete is not None, f"未抓到 complete 事件 / logs={logs}"
    assert complete["usable"] is True
    assert complete["secret_access"] is False
    assert "default_profile_id" in complete
    assert "provider" in complete

    assert _find_event(logs, "default_profile_health_check_deferred") is None


def test_default_profile_health_check_external_provider_is_deferred(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """外部 provider 启动期不读 key，也不猜可用性。"""
    from pydantic import SecretStr

    from src.api.main import _run_default_profile_health_check

    monkeypatch.setattr(
        SecretStr,
        "get_secret_value",
        lambda _self: (_ for _ in ()).throw(
            AssertionError("startup must not unwrap secrets")
        ),
    )

    with capture_logs() as logs:
        _run_default_profile_health_check()

    deferred = _find_event(logs, "default_profile_health_check_deferred")
    assert deferred is not None, f"未抓到 deferred 事件 / logs={logs}"
    assert deferred["usable"] is None
    assert deferred["secret_access"] is False
    assert deferred["deferred_until"] == "first_provider_runtime"


def test_default_profile_health_check_config_load_failure_swallowed(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """配置加载抛 → swallow + error 事件 + 主流程不抛(fail-soft 兜底)。"""
    from src.api.main import _run_default_profile_health_check

    # mock load_writer_profiles_config 抛
    def _fail_load(path: Any) -> Any:
        raise FileNotFoundError("simulated missing config.yaml")

    import src.core.config_loader as _config_loader_mod

    monkeypatch.setattr(_config_loader_mod, "load_writer_profiles_config", _fail_load)

    with capture_logs() as logs:
        # 关键:不抛 — 对齐既有 startup scan fail-soft 模式
        _run_default_profile_health_check()

    error_event = _find_event(logs, "default_profile_health_check_failed")
    assert error_event is not None, f"未抓到 error 事件 / logs={logs}"
    assert error_event["error_type"] == "FileNotFoundError"
    assert "simulated missing config.yaml" in error_event["error_message"]
