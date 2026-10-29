"""API key 路由集成测试(P6-B-16,GET/PUT ``/api/v1/config/api-keys``)。

覆盖独立路由 ``api_keys_router`` 的两个端点(脱敏列表 GET + 写入 PUT)+ 错误
路径。

测试矩阵
========

GET
---
- ``GET /api/v1/config/api-keys`` 未配置 → 200 + ``configured=False`` /
  ``masked_value=null``
- ``GET`` 已配置 → 200 + ``configured=True`` + 前 8 字符 + ``***``

PUT
---
- ``PUT`` happy provider=claude → 200 + .env 落字符串 + Settings 刷新 + 脱敏返回
- ``PUT`` provider 不在 Literal 枚举(``"qwen"``)→ 422 ``VALIDATION_FAILED``
  (schema Literal 先挡)
- ``PUT`` api_key 长度 < 8 → 422 ``VALIDATION_FAILED``
- ``PUT`` body 含 extra 字段 → 422 ``VALIDATION_FAILED`` (Pydantic extra=forbid)
- ``PUT`` .env 写入失败(模拟 OSError)→ 500 ``API_KEY_WRITE_FAILED``

WS event
--------
- ``PUT`` 成功 → ws_manager publish 1 次 ``api_key_updated`` 到 ``system.alerts``

测试基础设施
============
- ``settings_isolated`` fixture:每测保存 ``settings.claude_api_key`` 原值,
  teardown 恢复(对齐 ``tests/services/test_api_key_service.py``)。
- ``client`` fixture:覆盖 ``get_api_key_service`` 注入 tmp_path .env + mock
  ws_manager 的 service。
"""

from __future__ import annotations

from collections.abc import Iterator
from pathlib import Path
from typing import Any

import pytest
from fastapi.testclient import TestClient
from pydantic import SecretStr

from src import config as _config_module
from src.api.main import create_app
from src.api.routes.config import get_api_key_service
from src.services.api_key_service import ApiKeyService


class _FakeWSManager:
    def __init__(self, *, raise_exc: Exception | None = None) -> None:
        self.events: list[dict[str, object]] = []
        self._raise_exc = raise_exc

    def publish(
        self,
        *,
        channel: str,
        event: str,
        data: dict[str, object],
    ) -> None:
        if self._raise_exc is not None:
            raise self._raise_exc
        self.events.append({"channel": channel, "event": event, "data": data})


@pytest.fixture()
def settings_isolated() -> Iterator[None]:
    """保存 4 个 API key 字段(v1.1 wave 2 H2 起从 1 个扩 4 个)— teardown 恢复。"""
    originals = {
        attr: getattr(_config_module.settings, attr)
        for attr in (
            "claude_api_key",
            "dashscope_api_key",
            "deepseek_api_key",
            "openai_compatible_api_key",
        )
    }
    try:
        yield
    finally:
        for attr, value in originals.items():
            object.__setattr__(_config_module.settings, attr, value)


@pytest.fixture()
def env_path(tmp_path: Path) -> Path:
    return tmp_path / ".env"


@pytest.fixture()
def ws_manager() -> _FakeWSManager:
    return _FakeWSManager()


@pytest.fixture()
def api_key_service(
    env_path: Path, ws_manager: _FakeWSManager
) -> ApiKeyService:
    return ApiKeyService(env_path=env_path, ws_manager=ws_manager)


@pytest.fixture()
def client(
    api_key_service: ApiKeyService,
) -> Iterator[TestClient]:
    app = create_app()
    app.dependency_overrides[get_api_key_service] = lambda: api_key_service
    with TestClient(app) as c:
        yield c
    app.dependency_overrides.clear()


def _set_claude_key(value: str) -> None:
    object.__setattr__(
        _config_module.settings, "claude_api_key", SecretStr(value)
    )


# =============================================================================
# GET /config/api-keys
# =============================================================================


class TestListApiKeys:
    """GET 脱敏列表。"""

    def test_get_unconfigured_returns_null_masked(
        self,
        client: TestClient,
        settings_isolated: None,
    ) -> None:
        """Settings 字段空 → configured=False / masked_value=null。"""
        _set_claude_key("")

        resp = client.get("/api/v1/config/api-keys")

        assert resp.status_code == 200
        items = resp.json()["data"]["api_keys"]
        claude = next(i for i in items if i["provider"] == "claude")
        assert claude["configured"] is False
        assert claude["masked_value"] is None

    def test_get_configured_returns_prefix_stars(
        self,
        client: TestClient,
        settings_isolated: None,
    ) -> None:
        """已配置 → configured=True + 前 8 字符 + ``***``。"""
        _set_claude_key("sk-ant-getmasked1234567890")

        resp = client.get("/api/v1/config/api-keys")

        assert resp.status_code == 200
        items = resp.json()["data"]["api_keys"]
        claude = next(i for i in items if i["provider"] == "claude")
        assert claude["configured"] is True
        assert claude["masked_value"] == "sk-ant-g***"


# =============================================================================
# PUT /config/api-keys
# =============================================================================


class TestUpdateApiKey:
    """PUT 写入 + 错误路径。"""

    def test_put_happy_writes_env_and_returns_masked(
        self,
        client: TestClient,
        env_path: Path,
        settings_isolated: None,
    ) -> None:
        """PUT happy → 200 + .env 落 + Settings 刷新 + 脱敏返回。"""
        resp = client.put(
            "/api/v1/config/api-keys",
            json={"provider": "claude", "api_key": "sk-ant-route-12345678"},
        )

        assert resp.status_code == 200, resp.text
        data = resp.json()["data"]
        assert data == {
            "provider": "claude",
            "configured": True,
            "masked_value": "sk-ant-r***",
        }
        # .env 落字符串
        assert env_path.exists()
        assert "sk-ant-route-12345678" in env_path.read_text(encoding="utf-8")
        # Settings 刷新
        assert (
            _config_module.settings.claude_api_key.get_secret_value()
            == "sk-ant-route-12345678"
        )

    def test_put_unknown_provider_returns_422_validation_failed(
        self,
        client: TestClient,
        env_path: Path,
        settings_isolated: None,
    ) -> None:
        """provider 不在 Literal 枚举 → 422 VALIDATION_FAILED(schema 先挡)。"""
        resp = client.put(
            "/api/v1/config/api-keys",
            json={"provider": "qwen", "api_key": "qwen-key-12345678"},  # gitleaks:allow -- invalid provider 测试使用的固定虚构凭据
        )

        assert resp.status_code == 422
        assert resp.json()["error"]["code"] == "VALIDATION_FAILED"
        # .env 未创建
        assert not env_path.exists()

    def test_put_api_key_too_short_returns_422(
        self,
        client: TestClient,
        env_path: Path,
        settings_isolated: None,
    ) -> None:
        """api_key 长度 < 8 → 422 VALIDATION_FAILED(Pydantic min_length)。"""
        resp = client.put(
            "/api/v1/config/api-keys",
            json={"provider": "claude", "api_key": "short"},
        )

        assert resp.status_code == 422
        assert resp.json()["error"]["code"] == "VALIDATION_FAILED"
        assert not env_path.exists()

    def test_put_extra_field_returns_422(
        self,
        client: TestClient,
        env_path: Path,
        settings_isolated: None,
    ) -> None:
        """body 含 extra 字段 → 422 VALIDATION_FAILED(extra=forbid)。"""
        resp = client.put(
            "/api/v1/config/api-keys",
            json={
                "provider": "claude",
                "api_key": "sk-ant-extra-12345678",
                "rogue_field": "should_reject",
            },
        )

        assert resp.status_code == 422
        assert resp.json()["error"]["code"] == "VALIDATION_FAILED"
        assert not env_path.exists()

    def test_put_write_failed_returns_500_api_key_write_failed(
        self,
        env_path: Path,
        ws_manager: _FakeWSManager,
        settings_isolated: None,
        monkeypatch: pytest.MonkeyPatch,
    ) -> None:
        """.env 写入失败(OSError)→ 500 API_KEY_WRITE_FAILED + 失败响应包络。"""

        def _raise_oserror(*_args: object, **_kwargs: object) -> None:
            raise PermissionError("simulated EACCES")

        monkeypatch.setattr(
            "src.services.api_key_service.set_key", _raise_oserror
        )

        # 注:client fixture 已构造 service,这里需重新装配 + override
        app = create_app()
        service = ApiKeyService(env_path=env_path, ws_manager=ws_manager)
        app.dependency_overrides[get_api_key_service] = lambda: service

        with TestClient(app) as c:
            resp = c.put(
                "/api/v1/config/api-keys",
                json={
                    "provider": "claude",
                    "api_key": "sk-ant-willfail-12345678",
                },
            )

        assert resp.status_code == 500
        assert resp.json()["error"]["code"] == "API_KEY_WRITE_FAILED"


# =============================================================================
# WS event
# =============================================================================


class TestPutEmitsWebSocketEvent:
    """PUT 成功 → ws_manager publish ``api_key_updated`` 1 次。"""

    def test_put_publishes_api_key_updated_to_system_alerts(
        self,
        client: TestClient,
        ws_manager: _FakeWSManager,
        settings_isolated: None,
    ) -> None:
        resp = client.put(
            "/api/v1/config/api-keys",
            json={"provider": "claude", "api_key": "sk-ant-wsroute-1234567"},
        )

        assert resp.status_code == 200
        assert len(ws_manager.events) == 1
        evt: dict[str, Any] = ws_manager.events[0]
        assert evt["channel"] == "system.alerts"
        assert evt["event"] == "api_key_updated"
        assert evt["data"] == {
            "provider": "claude",
            "masked_value": "sk-ant-w***",
        }


# =============================================================================
# v1.1 wave 2 H2 — multi-provider 路由覆盖(deepseek + openai_compatible 通配)
# =============================================================================


class TestPutMultiProviderRoutesH2:
    """v1.1 wave 2 H2 起 ApiKeyIn.provider Literal 从 2 项扩 4 项;路由层接收
    + .env 落对应 env_var + Settings 对应字段刷新。"""

    @pytest.mark.parametrize(
        ("provider", "settings_attr", "fake_key", "expected_masked"),
        [
            (
                "deepseek",
                "deepseek_api_key",
                "sk-deepseek-route-12345",
                "sk-deeps***",
            ),
            (
                "openai_compatible",
                "openai_compatible_api_key",
                "sk-oai-compat-route-1234",
                "sk-oai-c***",
            ),
        ],
    )
    def test_put_new_provider_returns_200_and_refreshes_settings(
        self,
        client: TestClient,
        env_path: Path,
        settings_isolated: None,
        provider: str,
        settings_attr: str,
        fake_key: str,
        expected_masked: str,
    ) -> None:
        """PUT 新 provider → 200 + Settings 对应字段 + 返回脱敏对齐。"""
        resp = client.put(
            "/api/v1/config/api-keys",
            json={"provider": provider, "api_key": fake_key},
        )
        assert resp.status_code == 200, resp.text
        data = resp.json()["data"]
        assert data["provider"] == provider
        assert data["configured"] is True
        assert data["masked_value"] == expected_masked

        # Settings 对应字段(deepseek_api_key / openai_compatible_api_key)刷新
        assert (
            getattr(_config_module.settings, settings_attr).get_secret_value()
            == fake_key
        )
