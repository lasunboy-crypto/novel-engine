"""``src/services/api_key_service.py`` 单元测试(P6-B-16)。

测试矩阵
========

happy 路径
----------
- ``get_masked`` 未配置(Settings 字段空)→ ``configured=False`` + ``masked_value=None``
- ``get_masked`` 已配置(Settings 字段已填)→ ``configured=True`` + 前 8 字符 + ``***``
- ``put('claude', key)`` happy:.env 文件落字符串 + Settings 单例原地刷新
- ``put`` 后再 ``get_masked`` 等价新 key 脱敏
- ``put`` 写入 .env 时保留既有非 API_KEY env 变量 / 注释 / 顺序(`python-dotenv.set_key` 行为)

error 路径
----------
- ``put`` provider 不在 ``KNOWN_PROVIDERS`` → ``ApiKeyProviderUnknownError``
- ``put`` .env 写入 OSError(模拟权限不足 → 抛 ``ApiKeyWriteFailedError``)

gate 路径
---------
- ``put`` 时 .env 不存在 → 自动新建(``set_key`` 默认行为)+ Settings 刷新

rollback
--------
- ``put`` 写入抛 OSError 时 Settings 单例**不变**(写盘失败优先于刷新)

WS event
--------
- ``put`` 成功 → publish 1 次 ``api_key_updated`` 到 ``system.alerts`` 频道,
  data 含 ``provider`` + ``masked_value``(已脱敏)
- ``ws_manager.publish`` 抛错 → swallow + 不影响 put 成功返回
- ``ws_manager=None`` → 不 publish,put 正常返回

测试基础设施
============
- ``settings_isolated`` fixture:每测保存 ``settings.claude_api_key`` 原值,
  teardown 时恢复(``ApiKeyService.put`` 原地改 Settings 单例,跨测污染)
"""

from __future__ import annotations

from collections.abc import Iterator
from pathlib import Path

import pytest
from pydantic import SecretStr

from src import config as _config_module
from src.services.api_key_service import (
    KNOWN_PROVIDERS,
    ApiKeyProviderUnknownError,
    ApiKeyService,
    ApiKeyWriteFailedError,
    _mask,
)


# =============================================================================
# Fixtures
# =============================================================================


class _FakeWSManager:
    """记录 publish 调用,供 WS event 类测试断言。

    与 ``tests/services/test_config_service.py:_FakeWSManager`` 同型(P6-B-12
    沉淀模式;此处复刻避免跨文件 import 间接耦合)。
    """

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
    """每测保存 4 个 API key Settings 字段原值,teardown 恢复。

    ``ApiKeyService.put`` 原地改 ``settings.<provider>_api_key`` 字段(mutable
    global state),不隔离会跨测污染(test order dependent failure)。**v1.1
    wave 2 H2 起扩 4 个 provider**(claude / dashscope / deepseek /
    openai_compatible),fixture 同步保护全 4 个字段。
    """
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
    """tmp_path 下的 .env 路径(不预创建文件,测各路径自行控制)。"""
    return tmp_path / ".env"


# =============================================================================
# _mask
# =============================================================================


class TestMask:
    """脱敏函数边界测试。"""

    def test_mask_empty_returns_none(self) -> None:
        assert _mask("") is None

    def test_mask_short_returns_prefix_plus_stars(self) -> None:
        """value 长度 < 前缀 8 → 整体回显 + ``***``(无独立熵泄露)。"""
        assert _mask("abc") == "abc***"

    def test_mask_long_returns_prefix_8_plus_stars(self) -> None:
        """Anthropic key 形态 ``sk-ant-`` 前缀 + 主体 → 回显前 8 字符 + ``***``。"""
        assert _mask("sk-ant-1234567890abcdef") == "sk-ant-1***"


# =============================================================================
# get_masked
# =============================================================================


class TestGetMasked:
    """``ApiKeyService.get_masked`` 状态列表生成。"""

    def test_get_masked_unconfigured(
        self, settings_isolated: None
    ) -> None:
        """Settings.claude_api_key 为空 → configured=False / masked_value=None。"""
        object.__setattr__(
            _config_module.settings, "claude_api_key", SecretStr("")
        )
        service = ApiKeyService()
        items = service.get_masked()

        assert len(items) == len(KNOWN_PROVIDERS)
        claude = next(i for i in items if i["provider"] == "claude")
        assert claude["configured"] is False
        assert claude["masked_value"] is None

    def test_get_masked_configured_returns_prefix_and_stars(
        self, settings_isolated: None
    ) -> None:
        """Settings 字段已填 → configured=True + 前 8 字符 + ``***``。"""
        object.__setattr__(
            _config_module.settings,
            "claude_api_key",
            SecretStr("sk-ant-01abcdefghijklmnop"),
        )
        service = ApiKeyService()
        items = service.get_masked()

        claude = next(i for i in items if i["provider"] == "claude")
        assert claude["configured"] is True
        assert claude["masked_value"] == "sk-ant-0***"

    def test_get_masked_includes_all_known_providers(
        self, settings_isolated: None
    ) -> None:
        """无论 Settings 字段是否填,KNOWN_PROVIDERS 每项都出现在列表中。"""
        service = ApiKeyService()
        items = service.get_masked()

        provider_names = {i["provider"] for i in items}
        assert provider_names == set(KNOWN_PROVIDERS)


# =============================================================================
# put — happy + gate
# =============================================================================


class TestPutHappyPath:
    """``ApiKeyService.put`` 正常写入路径。"""

    def test_put_writes_env_file_and_refreshes_settings(
        self,
        env_path: Path,
        settings_isolated: None,
    ) -> None:
        """put → .env 落字符串 + Settings.claude_api_key 立即更新。"""
        service = ApiKeyService(env_path=env_path)

        status = service.put("claude", "sk-ant-new-12345678")

        # .env 文件写入
        assert env_path.exists()
        content = env_path.read_text(encoding="utf-8")
        assert "CLAUDE_API_KEY" in content
        assert "sk-ant-new-12345678" in content

        # Settings 单例原地刷新
        assert (
            _config_module.settings.claude_api_key.get_secret_value()
            == "sk-ant-new-12345678"
        )

        # 返回脱敏状态
        assert status == {
            "provider": "claude",
            "configured": True,
            "masked_value": "sk-ant-n***",
        }

    def test_put_then_get_masked_reflects_new_key(
        self,
        env_path: Path,
        settings_isolated: None,
    ) -> None:
        """put 后 get_masked 立即反映新 key 脱敏(不需要重启)。"""
        service = ApiKeyService(env_path=env_path)

        service.put("claude", "sk-ant-changed-9abcdef0")

        items = service.get_masked()
        claude = next(i for i in items if i["provider"] == "claude")
        assert claude["masked_value"] == "sk-ant-c***"

    def test_put_preserves_other_env_vars_comments_and_order(
        self,
        env_path: Path,
        settings_isolated: None,
    ) -> None:
        """关键接力 marker:写入 .env 保留既有非 API_KEY 类 env 变量 / 注释 / 顺序。"""
        env_path.write_text(
            "# Top comment\n"
            "CLAUDE_API_KEY=sk-ant-old-value\n"
            "\n"
            "# Backend section\n"
            "API_HOST=127.0.0.1\n"
            "API_PORT=8000\n"
            "\n"
            "# Data\n"
            "DATA_DIR=./workspaces\n",
            encoding="utf-8",
        )
        service = ApiKeyService(env_path=env_path)

        service.put("claude", "sk-ant-fresh-1234567890")

        content = env_path.read_text(encoding="utf-8")
        # 顶部注释保留
        assert "# Top comment" in content
        # CLAUDE_API_KEY 替换为新值
        assert "sk-ant-fresh-1234567890" in content
        assert "sk-ant-old-value" not in content
        # 其它 env 变量保留
        assert "API_HOST=127.0.0.1" in content
        assert "API_PORT=8000" in content
        assert "DATA_DIR=./workspaces" in content
        # 中部注释保留
        assert "# Backend section" in content
        assert "# Data" in content

    def test_put_creates_env_file_when_missing(
        self,
        env_path: Path,
        settings_isolated: None,
    ) -> None:
        """gate:.env 不存在时 set_key 自动新建(python-dotenv 行为)。"""
        assert not env_path.exists()
        service = ApiKeyService(env_path=env_path)

        service.put("claude", "sk-ant-firsttime-1234")

        assert env_path.exists()
        content = env_path.read_text(encoding="utf-8")
        assert "CLAUDE_API_KEY" in content
        assert "sk-ant-firsttime-1234" in content


# =============================================================================
# put — error + rollback
# =============================================================================


class TestPutErrorAndRollback:
    """``put`` 错误路径与 rollback 不变性。"""

    def test_put_unknown_provider_raises_provider_unknown_error(
        self,
        env_path: Path,
        settings_isolated: None,
    ) -> None:
        """provider 不在 KNOWN_PROVIDERS → ApiKeyProviderUnknownError + .env 不动。"""
        service = ApiKeyService(env_path=env_path)

        with pytest.raises(ApiKeyProviderUnknownError, match="qwen"):
            service.put("qwen", "qwen-api-key-12345")

        # .env 不应被创建
        assert not env_path.exists()

    def test_put_write_failed_raises_write_failed_error(
        self,
        env_path: Path,
        settings_isolated: None,
        monkeypatch: pytest.MonkeyPatch,
    ) -> None:
        """模拟 set_key 抛 OSError → ApiKeyWriteFailedError。"""

        def _raise_oserror(*_args: object, **_kwargs: object) -> None:
            raise PermissionError("simulated permission denied")

        monkeypatch.setattr(
            "src.services.api_key_service.set_key", _raise_oserror
        )
        service = ApiKeyService(env_path=env_path)

        with pytest.raises(
            ApiKeyWriteFailedError, match="permission denied"
        ):
            service.put("claude", "sk-ant-will-fail-12345")

    def test_put_write_failure_does_not_mutate_settings(
        self,
        env_path: Path,
        settings_isolated: None,
        monkeypatch: pytest.MonkeyPatch,
    ) -> None:
        """rollback:.env 写入失败时 Settings.claude_api_key 不变。"""
        # 设定初始 settings 字段
        object.__setattr__(
            _config_module.settings,
            "claude_api_key",
            SecretStr("sk-ant-preexisting-key"),
        )

        def _raise_oserror(*_args: object, **_kwargs: object) -> None:
            raise OSError("disk full")

        monkeypatch.setattr(
            "src.services.api_key_service.set_key", _raise_oserror
        )
        service = ApiKeyService(env_path=env_path)

        with pytest.raises(ApiKeyWriteFailedError):
            service.put("claude", "sk-ant-replacement-fail")

        # Settings 字段保持原值(写盘失败优先于刷新)
        assert (
            _config_module.settings.claude_api_key.get_secret_value()
            == "sk-ant-preexisting-key"
        )


# =============================================================================
# WS event
# =============================================================================


class TestPutWebSocketEvent:
    """``put`` 成功后 publish ``api_key_updated`` 到 ``system.alerts``。"""

    def test_put_publishes_api_key_updated_event(
        self,
        env_path: Path,
        settings_isolated: None,
    ) -> None:
        """publish 1 次 + channel=system.alerts + data 含 provider + 脱敏值。"""
        ws_manager = _FakeWSManager()
        service = ApiKeyService(env_path=env_path, ws_manager=ws_manager)

        service.put("claude", "sk-ant-wsevent-12345678")

        assert len(ws_manager.events) == 1
        evt = ws_manager.events[0]
        assert evt["channel"] == "system.alerts"
        assert evt["event"] == "api_key_updated"
        assert evt["data"] == {
            "provider": "claude",
            "masked_value": "sk-ant-w***",
        }

    def test_put_publish_failure_swallowed(
        self,
        env_path: Path,
        settings_isolated: None,
    ) -> None:
        """ws_manager.publish 抛错 → swallow + put 仍正常返回 + Settings 刷新。"""
        ws_manager = _FakeWSManager(raise_exc=RuntimeError("ws broken"))
        service = ApiKeyService(env_path=env_path, ws_manager=ws_manager)

        status = service.put("claude", "sk-ant-swallow-12345678")

        # 返回状态正常
        assert status["configured"] is True
        # Settings 已刷新(写盘 + 刷新都在 publish 之前)
        assert (
            _config_module.settings.claude_api_key.get_secret_value()
            == "sk-ant-swallow-12345678"
        )
        # 没有记录到事件(publish 直接抛错)
        assert ws_manager.events == []

    def test_put_no_ws_manager_skips_publish_silently(
        self,
        env_path: Path,
        settings_isolated: None,
    ) -> None:
        """ws_manager=None → 不 publish,put 正常返回。"""
        service = ApiKeyService(env_path=env_path, ws_manager=None)

        status = service.put("claude", "sk-ant-nomanager-1234")

        assert status["configured"] is True


# =============================================================================
# v1.1 wave 2 H2 — multi-provider 扩展(deepseek + openai_compatible 通配)
# =============================================================================


class TestPutMultiProviderH2:
    """v1.1 wave 2 H2 引入的新 provider PUT / GET 路径。

    sourceing
    ---------
    KNOWN_PROVIDERS 从 2 ("claude", "dashscope") 扩到 4
    ("+deepseek", "+openai_compatible"),覆盖维护人 plan-kickoff Q6 拍板的
    "首批 5 家" 中除已实施的 claude / dashscope 外的剩余路径(ollama 不走 key)。
    """

    @pytest.mark.parametrize(
        ("provider", "env_var", "settings_attr", "fake_key"),
        [
            (
                "deepseek",
                "DEEPSEEK_API_KEY",
                "deepseek_api_key",
                "sk-deepseek-1234567890abcdef",
            ),
            (
                "openai_compatible",
                "OPENAI_COMPATIBLE_API_KEY",
                "openai_compatible_api_key",
                "sk-oai-compat-1234567890abcd",
            ),
        ],
    )
    def test_put_new_provider_writes_env_and_refreshes_settings(
        self,
        env_path: Path,
        settings_isolated: None,
        provider: str,
        env_var: str,
        settings_attr: str,
        fake_key: str,
    ) -> None:
        """新 provider PUT → 正确的 .env key 名 + 对应 Settings 字段刷新。"""
        service = ApiKeyService(env_path=env_path)
        status = service.put(provider, fake_key)

        # .env 文件落对应 env_var 名(不串到 CLAUDE_API_KEY / DASHSCOPE_API_KEY)
        content = env_path.read_text(encoding="utf-8")
        assert env_var in content
        assert fake_key in content
        assert "CLAUDE_API_KEY" not in content
        assert "DASHSCOPE_API_KEY" not in content

        # Settings 对应字段原地刷新
        assert (
            getattr(_config_module.settings, settings_attr).get_secret_value()
            == fake_key
        )

        # 返回脱敏状态对齐 provider
        assert status["provider"] == provider
        assert status["configured"] is True
        assert status["masked_value"] is not None
        assert status["masked_value"].endswith("***")

    def test_get_masked_lists_all_4_providers(
        self,
        settings_isolated: None,
    ) -> None:
        """v1.1 wave 2 H2 起 get_masked 返回 4 项(claude + dashscope + deepseek +
        openai_compatible)— 防 KNOWN_PROVIDERS 与 _PROVIDER_REGISTRY 漂移。
        """
        service = ApiKeyService()
        items = service.get_masked()
        names = {i["provider"] for i in items}
        assert names == {
            "claude",
            "dashscope",
            "deepseek",
            "openai_compatible",
        }

    def test_put_then_get_masked_isolated_per_provider(
        self,
        env_path: Path,
        settings_isolated: None,
    ) -> None:
        """4 个 provider PUT 互不串(写 deepseek 不影响 claude / dashscope /
        openai_compatible 字段状态)。"""
        # 主动清空 4 字段(fixture 只 save+restore 不清,主仓 .env 可能
        # 已配 claude/dashscope key 让断言"未配置"失真)
        for attr in (
            "claude_api_key",
            "dashscope_api_key",
            "deepseek_api_key",
            "openai_compatible_api_key",
        ):
            object.__setattr__(_config_module.settings, attr, SecretStr(""))

        service = ApiKeyService(env_path=env_path)
        service.put("deepseek", "sk-deepseek-isolated-12345")

        items = service.get_masked()
        deepseek = next(i for i in items if i["provider"] == "deepseek")
        claude = next(i for i in items if i["provider"] == "claude")
        dashscope = next(i for i in items if i["provider"] == "dashscope")
        openai_compat = next(
            i for i in items if i["provider"] == "openai_compatible"
        )

        assert deepseek["configured"] is True
        # 其他三家保持未配置(本测前置主动清空 4 字段)
        assert claude["configured"] is False
        assert dashscope["configured"] is False
        assert openai_compat["configured"] is False
