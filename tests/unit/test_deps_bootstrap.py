"""deps 层 bootstrap LLM client 构造单测(v1.0.x post-wave5 H1 routing completion fixup)。

覆盖 ``src/api/deps.py::_resolve_bootstrap_target`` 行为 + 5 个 ``get_*_service``
依赖工厂的 bootstrap LLM client 构造在不同 ``LLM_PROVIDER`` env 下不抛错。

本任务背景
==========

H1 routing completion(轮 172)只改 router 路径显式传 ``provider``,deps.py
5 处 ``make_llm_client(model)`` bootstrap 调用点仍未传 ``provider``/``base_url``,
导致 ``settings.llm_provider=openai_compatible`` 时 fallback 路径 base_url=None
→ ``ValueError`` 500。fixup 修法:bootstrap client 走
``_resolve_bootstrap_target()`` 读引擎默认 ``config.yaml`` 的 default profile
三元组,与 routing completion 设计原则(provider 跟 profile 字面走,不看
settings 全局开关)一致。

本测覆盖:

- ``_resolve_bootstrap_target`` 在合法 config.yaml 下返 default profile 三元组
- ``_resolve_bootstrap_target`` 在 config.yaml 损坏 / 不存在时 fallback 不抛
- 真 bug 回归:``LLM_PROVIDER=openai_compatible`` env 下 5 个 dep getter 都
  能成功构造 service(不撞 ``ValueError: 需要显式 base_url``)
"""

from __future__ import annotations

from pathlib import Path

import pytest

from src.api import deps as deps_module
from src.api.deps import _resolve_bootstrap_target
from src.config import Settings
from src.core.llm.mock_client import MockLLMClient
from src.core.llm.ollama_client import OllamaClient
from src.core.llm.openai_compatible_client import OpenAICompatibleClient


def _build_settings_for(tmp_path: Path, **env: str) -> Settings:
    """构造一份指向 tmp_path 的 Settings 实例。

    与 ``test_llm_factory.py::_build_settings`` 同型,但允许定向控制
    ``config_yaml_path`` 让 ``_resolve_bootstrap_target`` 读 tmp yaml 而非
    主仓 config.yaml。
    """
    import os

    for k, v in env.items():
        os.environ[k] = v
    s = Settings(_env_file=None)  # type: ignore[call-arg]
    return s


@pytest.fixture
def clean_env(monkeypatch: pytest.MonkeyPatch) -> pytest.MonkeyPatch:
    for key in (
        "LLM_PROVIDER",
        "CLAUDE_API_KEY",
        "DASHSCOPE_API_KEY",
        "API_HOST",
        "API_PORT",
        "API_AUTH_ENABLED",
        "API_AUTH_TOKEN",
    ):
        monkeypatch.delenv(key, raising=False)
    return monkeypatch


# 最小合法 engine config.yaml — default = local_qwen_32k(ollama)
_MINIMAL_CONFIG_YAML = """\
writer_profiles:
  local_qwen_32k:
    provider: ollama
    model: qwen-novel
    base_url: http://localhost:11434
    context_window: 24576
    generation_params:
      temperature: 0.7
      num_predict: 4096
    extra_request_kwargs:
      think: false
    cost_per_1k_input: 0.0
    cost_per_1k_output: 0.0
  mock_default:
    provider: mock
    model: mock-engine
    context_window: 8000
    cost_per_1k_input: 0.0
    cost_per_1k_output: 0.0

writer_routing:
  default: local_qwen_32k
  rules: []
  agent_routing: {}
"""

# default = cloud_qwen_max_thinking(openai_compatible)
_CLOUD_DEFAULT_CONFIG_YAML = """\
writer_profiles:
  cloud_qwen_max_thinking:
    provider: openai_compatible
    model: qwen3.7-max
    base_url: https://dashscope.aliyuncs.com/compatible-mode/v1
    context_window: 128000
    generation_params:
      temperature: 0.7
      max_tokens: 8000
    extra_request_kwargs:
      enable_thinking: true
    cost_per_1k_input: 0.006
    cost_per_1k_output: 0.018

writer_routing:
  default: cloud_qwen_max_thinking
  rules: []
  agent_routing: {}
"""


class TestResolveBootstrapTarget:
    """``_resolve_bootstrap_target`` 直接行为测。"""

    def test_returns_engine_default_profile_triple(
        self,
        clean_env: pytest.MonkeyPatch,
        tmp_path: Path,
    ) -> None:
        """合法 config.yaml + default=local_qwen_32k → 返 ollama 三元组。"""
        config_path = tmp_path / "config.yaml"
        config_path.write_text(_MINIMAL_CONFIG_YAML, encoding="utf-8")

        s = _build_settings_for(tmp_path)
        s.config_yaml_path = config_path
        clean_env.setattr("src.config.settings", s)

        provider, model, base_url = _resolve_bootstrap_target()

        assert provider == "ollama"
        assert model == "qwen-novel"
        assert base_url == "http://localhost:11434"

    def test_cloud_default_returns_openai_compatible_triple(
        self,
        clean_env: pytest.MonkeyPatch,
        tmp_path: Path,
    ) -> None:
        """default=cloud_qwen_max_thinking → 返 openai_compatible 三元组(含 base_url)。

        本测覆盖维护人切云端后 bootstrap 自动跟随 profile 字面派发 — fixup 修法
        的真实价值场景。
        """
        config_path = tmp_path / "config.yaml"
        config_path.write_text(_CLOUD_DEFAULT_CONFIG_YAML, encoding="utf-8")

        s = _build_settings_for(tmp_path)
        s.config_yaml_path = config_path
        clean_env.setattr("src.config.settings", s)

        provider, model, base_url = _resolve_bootstrap_target()

        assert provider == "openai_compatible"
        assert model == "qwen3.7-max"
        assert base_url == "https://dashscope.aliyuncs.com/compatible-mode/v1"

    def test_ignores_settings_llm_provider_global_switch(
        self,
        clean_env: pytest.MonkeyPatch,
        tmp_path: Path,
    ) -> None:
        """LLM_PROVIDER=openai_compatible env 但 engine default=local_qwen_32k
        → bootstrap 返 ollama 三元组(不看 settings 全局开关)。

        这是 fixup 的核心回归场景:维护人 .env 残留 ``LLM_PROVIDER=openai_compatible``
        但 engine default 是 ollama → bootstrap 跟 engine default,bug 不复现。
        """
        config_path = tmp_path / "config.yaml"
        config_path.write_text(_MINIMAL_CONFIG_YAML, encoding="utf-8")

        s = _build_settings_for(tmp_path, LLM_PROVIDER="openai_compatible")
        s.config_yaml_path = config_path
        clean_env.setattr("src.config.settings", s)

        provider, model, base_url = _resolve_bootstrap_target()

        # 与 settings.llm_provider=openai_compatible 完全无关
        assert provider == "ollama"
        assert base_url == "http://localhost:11434"

    def test_fallback_when_config_yaml_missing(
        self,
        clean_env: pytest.MonkeyPatch,
        tmp_path: Path,
    ) -> None:
        """config.yaml 不存在 → fallback (settings.llm_provider, _DEFAULT_MODEL, None)。

        fallback 字面对齐 docstring;不抛错保护单测 / 启动早期未初始化场景。
        """
        s = _build_settings_for(tmp_path, LLM_PROVIDER="mock")
        s.config_yaml_path = tmp_path / "nonexistent.yaml"
        clean_env.setattr("src.config.settings", s)

        provider, model, base_url = _resolve_bootstrap_target()

        assert provider == "mock"
        assert model == "claude-sonnet-4-6"  # _DEFAULT_MODEL
        assert base_url is None

    def test_fallback_when_config_yaml_corrupted(
        self,
        clean_env: pytest.MonkeyPatch,
        tmp_path: Path,
    ) -> None:
        """config.yaml 语法损坏 → fallback 不抛,且 log warning。"""
        config_path = tmp_path / "config.yaml"
        config_path.write_text("this is: : : not valid yaml [", encoding="utf-8")

        s = _build_settings_for(tmp_path, LLM_PROVIDER="ollama")
        s.config_yaml_path = config_path
        clean_env.setattr("src.config.settings", s)

        provider, model, base_url = _resolve_bootstrap_target()

        # fallback 到 settings.llm_provider 极端兜底
        assert provider == "ollama"


class TestBootstrapClientTypeMatchesEngineDefault:
    """``_resolve_bootstrap_target`` + ``make_llm_client`` 联动:bootstrap client
    类型跟 engine default profile.provider 字面派发,不受 ``settings.llm_provider``
    全局开关影响。
    """

    def test_engine_default_ollama_produces_ollama_client(
        self,
        clean_env: pytest.MonkeyPatch,
        tmp_path: Path,
    ) -> None:
        config_path = tmp_path / "config.yaml"
        config_path.write_text(_MINIMAL_CONFIG_YAML, encoding="utf-8")

        # 故意把 settings.llm_provider 设成 openai_compatible(真实 bug env)
        s = _build_settings_for(tmp_path, LLM_PROVIDER="openai_compatible")
        s.config_yaml_path = config_path
        clean_env.setattr("src.config.settings", s)

        # 同步 factory 模块的 settings 单例
        from src.core.llm import factory as factory_module

        clean_env.setattr(factory_module, "settings", s)

        provider, model, base_url = _resolve_bootstrap_target()
        from src.core.llm.factory import make_llm_client

        client = make_llm_client(model, base_url=base_url, provider=provider)

        # bootstrap 跟 engine default(ollama),不撞 base_url 缺失 ValueError
        assert isinstance(client, OllamaClient)

    def test_engine_default_cloud_produces_openai_compatible_client(
        self,
        clean_env: pytest.MonkeyPatch,
        tmp_path: Path,
    ) -> None:
        config_path = tmp_path / "config.yaml"
        config_path.write_text(_CLOUD_DEFAULT_CONFIG_YAML, encoding="utf-8")

        s = _build_settings_for(
            tmp_path,
            LLM_PROVIDER="mock",  # settings 是 mock 但 bootstrap 不看它
            DASHSCOPE_API_KEY="sk-fake-test-key",
        )
        s.config_yaml_path = config_path
        clean_env.setattr("src.config.settings", s)

        from src.core.llm import factory as factory_module

        clean_env.setattr(factory_module, "settings", s)

        provider, model, base_url = _resolve_bootstrap_target()
        from src.core.llm.factory import make_llm_client

        client = make_llm_client(model, base_url=base_url, provider=provider)

        # bootstrap 跟 engine default(openai_compatible),即使 settings.llm_provider=mock
        assert isinstance(client, OpenAICompatibleClient)

    def test_corrupted_config_fallback_to_mock_client_under_mock_env(
        self,
        clean_env: pytest.MonkeyPatch,
        tmp_path: Path,
    ) -> None:
        """config.yaml 不存在 + LLM_PROVIDER=mock → fallback 兜底产 MockLLMClient。"""
        s = _build_settings_for(tmp_path, LLM_PROVIDER="mock")
        s.config_yaml_path = tmp_path / "nonexistent.yaml"
        clean_env.setattr("src.config.settings", s)

        from src.core.llm import factory as factory_module

        clean_env.setattr(factory_module, "settings", s)

        provider, model, base_url = _resolve_bootstrap_target()
        from src.core.llm.factory import make_llm_client

        client = make_llm_client(model, base_url=base_url, provider=provider)

        assert isinstance(client, MockLLMClient)


class TestRealBugRegression:
    """真 bug 回归:``LLM_PROVIDER=openai_compatible`` env 下,deps 层 service
    构造路径不再撞 ``ValueError: 需要显式 base_url``。

    本测组覆盖维护人本机实测撞 500 的精确路径 — fixup 后必须 0 抛错。
    """

    def test_get_outline_service_does_not_raise_under_openai_compatible_env(
        self,
        clean_env: pytest.MonkeyPatch,
        tmp_path: Path,
    ) -> None:
        """``get_outline_service`` 是维护人 luo-cloud-test 实测撞 500 的 caller。

        fixup 后:即使 ``LLM_PROVIDER=openai_compatible`` 且 settings 没显式
        base_url,bootstrap 走 engine default profile(local_qwen_32k,ollama)
        → 不撞 base_url 缺失 ValueError。
        """
        config_path = tmp_path / "config.yaml"
        config_path.write_text(_MINIMAL_CONFIG_YAML, encoding="utf-8")

        s = _build_settings_for(tmp_path, LLM_PROVIDER="openai_compatible")
        s.config_yaml_path = config_path
        s.data_dir = tmp_path / "workspaces"
        s.data_dir.mkdir()
        clean_env.setattr("src.config.settings", s)

        from src.core.llm import factory as factory_module

        clean_env.setattr(factory_module, "settings", s)

        # 直接调 dep getter — 必须不抛 ValueError
        # (fixup 前撞 ValueError: openai_compatible provider 需要显式 base_url)
        ws = deps_module.get_workspace_service()
        service = deps_module.get_outline_service(workspace_service=ws)

        assert service is not None

    def test_get_memory_health_service_does_not_raise_under_openai_compatible_env(
        self,
        clean_env: pytest.MonkeyPatch,
        tmp_path: Path,
    ) -> None:
        """``get_memory_health_service`` 无 router_factory,bootstrap client 即真实使用。

        fixup 前:``LLM_PROVIDER=openai_compatible`` + 走 factory fallback +
        无 base_url → 必抛。fixup 后:走 engine default(ollama)→ 不抛。
        """
        config_path = tmp_path / "config.yaml"
        config_path.write_text(_MINIMAL_CONFIG_YAML, encoding="utf-8")

        s = _build_settings_for(tmp_path, LLM_PROVIDER="openai_compatible")
        s.config_yaml_path = config_path
        s.data_dir = tmp_path / "workspaces"
        s.data_dir.mkdir()
        clean_env.setattr("src.config.settings", s)

        from src.core.llm import factory as factory_module

        clean_env.setattr(factory_module, "settings", s)

        ws = deps_module.get_workspace_service()
        service = deps_module.get_memory_health_service(workspace_service=ws)

        assert service is not None

    def test_get_chapter_card_service_does_not_raise_under_openai_compatible_env(
        self,
        clean_env: pytest.MonkeyPatch,
        tmp_path: Path,
    ) -> None:
        config_path = tmp_path / "config.yaml"
        config_path.write_text(_MINIMAL_CONFIG_YAML, encoding="utf-8")

        s = _build_settings_for(tmp_path, LLM_PROVIDER="openai_compatible")
        s.config_yaml_path = config_path
        s.data_dir = tmp_path / "workspaces"
        s.data_dir.mkdir()
        clean_env.setattr("src.config.settings", s)

        from src.core.llm import factory as factory_module

        clean_env.setattr(factory_module, "settings", s)

        ws = deps_module.get_workspace_service()
        chapter = deps_module.get_chapter_service(workspace_service=ws)
        service = deps_module.get_chapter_card_service(
            workspace_service=ws, chapter_service=chapter
        )

        assert service is not None

    def test_get_chapter_service_does_not_raise_under_openai_compatible_env(
        self,
        clean_env: pytest.MonkeyPatch,
        tmp_path: Path,
    ) -> None:
        config_path = tmp_path / "config.yaml"
        config_path.write_text(_MINIMAL_CONFIG_YAML, encoding="utf-8")

        s = _build_settings_for(tmp_path, LLM_PROVIDER="openai_compatible")
        s.config_yaml_path = config_path
        s.data_dir = tmp_path / "workspaces"
        s.data_dir.mkdir()
        clean_env.setattr("src.config.settings", s)

        from src.core.llm import factory as factory_module

        clean_env.setattr(factory_module, "settings", s)

        ws = deps_module.get_workspace_service()
        service = deps_module.get_chapter_service(workspace_service=ws)

        assert service is not None

    def test_build_pipeline_components_for_lifespan_never_reads_provider_config(
        self,
        clean_env: pytest.MonkeyPatch,
        tmp_path: Path,
    ) -> None:
        """lifespan 本地恢复不解析 provider，也不构造会读密钥的 client。"""
        config_path = tmp_path / "config.yaml"
        config_path.write_text(_MINIMAL_CONFIG_YAML, encoding="utf-8")

        s = _build_settings_for(tmp_path, LLM_PROVIDER="openai_compatible")
        s.config_yaml_path = config_path
        s.data_dir = tmp_path / "workspaces"
        s.data_dir.mkdir()
        clean_env.setattr("src.config.settings", s)

        from src.core.llm import factory as factory_module

        clean_env.setattr(factory_module, "settings", s)
        clean_env.setattr(
            deps_module,
            "_resolve_bootstrap_target",
            lambda: (_ for _ in ()).throw(
                AssertionError("lifespan 不得解析 Provider profile")
            ),
        )
        clean_env.setattr(
            deps_module,
            "make_llm_client",
            lambda *_args, **_kwargs: (_ for _ in ()).throw(
                AssertionError("lifespan 不得构造 Provider client")
            ),
        )

        service, workspace_ids = deps_module.build_pipeline_components_for_lifespan()

        assert service is not None
        assert isinstance(workspace_ids, list)

    def test_lifespan_session_factory_never_creates_database_or_schema(
        self,
        tmp_path: Path,
        monkeypatch: pytest.MonkeyPatch,
    ) -> None:
        from sqlalchemy import create_engine, text

        from src.core.db import Base
        from src.services.workspace_service import WorkspaceService

        workspaces_root = tmp_path / "workspaces"
        workspace_service = WorkspaceService(
            workspaces_root=workspaces_root,
            archives_root=tmp_path / "archives",
        )
        workspace_service.create(
            "ws-existing",
            display_name="lifespan existing session",
            genre="scifi",
            premise="test",
            activate=False,
        )
        workspace_service.create(
            "ws-missing",
            display_name="lifespan missing database",
            genre="scifi",
            premise="test",
            activate=False,
        )
        db_path = workspaces_root / "ws-existing" / "data" / "novel.db"
        engine = create_engine(f"sqlite:///{db_path}")
        Base.metadata.create_all(engine)
        engine.dispose()
        deps_module.configure_runtime_workspace_admission(
            workspaces_root,
            admitted_workspace_ids=["ws-existing"],
            restart_required_workspace_ids=[],
            uninitialized_workspace_ids=["ws-missing"],
        )

        monkeypatch.setattr(
            Base.metadata,
            "create_all",
            lambda *_args, **_kwargs: (_ for _ in ()).throw(
                AssertionError("lifespan 不得 create_all")
            ),
        )
        factory = deps_module._make_existing_session_factory(workspaces_root)
        session = factory("ws-existing")
        try:
            assert session.execute(text("SELECT 1")).scalar_one() == 1
        finally:
            session.close()

        missing_path = workspaces_root / "ws-missing" / "data" / "novel.db"
        with pytest.raises(FileNotFoundError):
            factory("ws-missing")
        assert not missing_path.exists()
