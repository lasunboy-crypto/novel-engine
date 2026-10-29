"""v1.1 wave 2 H6:``src/api/deps.py`` invalidate hook 单元测试。

覆盖
====
- ``reset_global_config_caches()`` 清模块级 ``_COST_GUARD_CONFIG`` /
  ``_CARD_GENERATOR_CONFIG`` 缓存,改了引擎默认 ``config.yaml`` 后立即生效
- ``reset_workspace_config_caches(workspace_id)`` 替代分散的 cost_guard
  reset 调用,统一入口语义
- ``_make_router_factory()`` 每次 LLM 调用重读 yaml(P3-B-06 既有行为,本任务
  补防御测试避免回归)

为何独立文件
============
``test_deps_bootstrap.py`` 聚焦 deps 层 bootstrap LLM client 构造(post-wave5
H1 fixup 焦点),本文件专注 H6 invalidate hook 路径,职责清晰。
"""

from __future__ import annotations

from pathlib import Path

import pytest

from src.api import deps
from src.services.cost_guard import (
    CostGuardConfig,
    _WORKSPACE_COST_GUARD_CACHE,
)


_BASE_YAML = """\
writer_profiles:
  local_qwen_32k:
    provider: ollama
    model: qwen-novel
    base_url: http://localhost:11434
    context_window: 24576
    generation_params: {temperature: 0.7}
    extra_request_kwargs: {think: false}

writer_routing:
  default: local_qwen_32k
  agent_routing:
    writer: local_qwen_32k

cost_guard:
  daily_usd_limit: 20.0
  monthly_usd_limit: 300.0
  per_workspace_daily_limit: 10.0
  action_on_exceed: pause
  notify_via: [log, websocket]
"""


@pytest.fixture()
def stash_module_caches() -> object:
    """保存 deps 模块级缓存原值,测试后还原(防止跨测试污染)。"""
    saved_cost = deps._COST_GUARD_CONFIG
    saved_card = deps._CARD_GENERATOR_CONFIG
    yield None
    deps._COST_GUARD_CONFIG = saved_cost
    deps._CARD_GENERATOR_CONFIG = saved_card


class TestResetGlobalConfigCaches:
    """``reset_global_config_caches()`` 重新加载引擎默认派生的模块级缓存。"""

    def test_reset_reloads_cost_guard_from_disk(
        self,
        tmp_path: Path,
        monkeypatch: pytest.MonkeyPatch,
        stash_module_caches: object,
    ) -> None:
        """改了 config.yaml cost_guard 段 → 调 reset → _COST_GUARD_CONFIG 拿到新值。"""
        # 1) 写一个 yaml 让 cost_guard 加载有 daily_usd_limit=20
        cfg_path = tmp_path / "config.yaml"
        cfg_path.write_text(_BASE_YAML, encoding="utf-8")
        from src.config import settings

        monkeypatch.setattr(settings, "config_yaml_path", cfg_path)

        # 模拟 reset 加载初始值
        deps.reset_global_config_caches()
        first = deps._COST_GUARD_CONFIG
        assert isinstance(first, CostGuardConfig)
        assert first.daily_usd_limit == 20.0

        # 2) 改 yaml — 把 daily_usd_limit 改成 50.0
        new_yaml = _BASE_YAML.replace(
            "daily_usd_limit: 20.0", "daily_usd_limit: 50.0"
        )
        cfg_path.write_text(new_yaml, encoding="utf-8")

        # 3) reset → 应拿到新值
        deps.reset_global_config_caches()
        assert deps._COST_GUARD_CONFIG is not None
        assert deps._COST_GUARD_CONFIG.daily_usd_limit == 50.0

    def test_reset_fallback_to_none_on_yaml_error(
        self,
        tmp_path: Path,
        monkeypatch: pytest.MonkeyPatch,
        stash_module_caches: object,
    ) -> None:
        """yaml 损坏(语法错)→ ``_COST_GUARD_CONFIG`` fallback 到 None(不抛错)。"""
        cfg_path = tmp_path / "config.yaml"
        cfg_path.write_text("not: valid: yaml: at all", encoding="utf-8")
        from src.config import settings

        monkeypatch.setattr(settings, "config_yaml_path", cfg_path)

        deps.reset_global_config_caches()
        assert deps._COST_GUARD_CONFIG is None

    def test_reset_card_generator_fallback_to_default_on_error(
        self,
        tmp_path: Path,
        monkeypatch: pytest.MonkeyPatch,
        stash_module_caches: object,
    ) -> None:
        """yaml 损坏 → ``_CARD_GENERATOR_CONFIG`` fallback 到字面默认值(不抛错)。"""
        cfg_path = tmp_path / "config.yaml"
        cfg_path.write_text("garbled yaml :: {{{", encoding="utf-8")
        from src.config import settings

        monkeypatch.setattr(settings, "config_yaml_path", cfg_path)

        deps.reset_global_config_caches()
        # card_generator fallback 不是 None,是字面默认配置对象
        assert deps._CARD_GENERATOR_CONFIG is not None
        assert deps._CARD_GENERATOR_CONFIG.model_dump() == {
            "diversification_threshold": 0.5,
            "style_controller_enabled": True,
        }


class TestResetWorkspaceConfigCaches:
    """``reset_workspace_config_caches(workspace_id)`` 工作区级 invalidate 统一入口。"""

    def test_reset_clears_cost_guard_workspace_cache(self) -> None:
        """调 reset 后 ``_WORKSPACE_COST_GUARD_CACHE[workspace_id]`` 被清。"""
        # 先填入一个伪缓存项
        cache_key = "ws_test_invalidate"
        _WORKSPACE_COST_GUARD_CACHE[cache_key] = CostGuardConfig(
            daily_usd_limit=999.0,
            monthly_usd_limit=999.0,
            per_workspace_daily_limit=999.0,
            action_on_exceed="warn",
            notify_via=["log"],
        )
        assert cache_key in _WORKSPACE_COST_GUARD_CACHE

        deps.reset_workspace_config_caches(cache_key)

        assert cache_key not in _WORKSPACE_COST_GUARD_CACHE

    def test_reset_other_workspace_caches_not_affected(self) -> None:
        """只清指定 workspace_id 的缓存,其他工作区不受影响(对齐 cost_guard reset 语义)。"""
        cache_key_target = "ws_target"
        cache_key_other = "ws_other"

        cfg = CostGuardConfig(
            daily_usd_limit=999.0,
            monthly_usd_limit=999.0,
            per_workspace_daily_limit=999.0,
            action_on_exceed="warn",
            notify_via=["log"],
        )
        _WORKSPACE_COST_GUARD_CACHE[cache_key_target] = cfg
        _WORKSPACE_COST_GUARD_CACHE[cache_key_other] = cfg

        deps.reset_workspace_config_caches(cache_key_target)

        assert cache_key_target not in _WORKSPACE_COST_GUARD_CACHE
        assert cache_key_other in _WORKSPACE_COST_GUARD_CACHE
        # 清理
        _WORKSPACE_COST_GUARD_CACHE.pop(cache_key_other, None)

    def test_reset_missing_workspace_id_does_not_raise(self) -> None:
        """workspace_id 没在缓存里 → 静默 pop None,不抛错(沿用 cost_guard reset 语义)。"""
        deps.reset_workspace_config_caches("never_cached_workspace")
        # 没抛错即通过


class TestRouterFactoryReloadsOnEveryCall:
    """``_make_router_factory()`` 每次调用都重读 yaml(P3-B-06 既有行为,本测试是
    H6 防御 — 避免未来重构引入"缓存 ModelRouter" 优化破坏 hot-reload 语义)。"""

    def test_factory_returns_fresh_router_on_each_call(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        """两次调 factory 返回的 ModelRouter 实例是不同对象(确保每次重读 yaml)。"""
        cfg_path = tmp_path / "config.yaml"
        cfg_path.write_text(_BASE_YAML, encoding="utf-8")
        from src.config import settings

        monkeypatch.setattr(settings, "config_yaml_path", cfg_path)

        # workspaces_root 让 load_workspace_config 不抛错
        ws_root = tmp_path / "workspaces"
        ws_root.mkdir()
        monkeypatch.setattr(settings, "data_dir", tmp_path)

        factory = deps._make_router_factory()
        router_a = factory("ws_demo")
        router_b = factory("ws_demo")

        assert router_a is not router_b, (
            "_make_router_factory 应每次构造新 ModelRouter,确保 hot-reload 工作区"
            "配置生效(P3-B-06 设计 + v1.1 wave 2 H6 防御)"
        )
