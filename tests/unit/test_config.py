"""src/config.py 单元测试。

策略:不复用模块级 settings 单例(避免污染),而是用 monkeypatch 隔离环境变量
后,直接构造 Settings() 触发校验。env_file 字段固定为不存在的路径,防止本地
.env 干扰测试。
"""

from pathlib import Path

import pytest
from pydantic import ValidationError

from src.config import Settings
from src.version import ENGINE_VERSION


@pytest.fixture
def clean_env(monkeypatch: pytest.MonkeyPatch) -> pytest.MonkeyPatch:
    """清掉所有可能污染 Settings 的环境变量,并禁用 .env 加载。"""
    for key in (
        "CLAUDE_API_KEY",
        "API_HOST",
        "API_PORT",
        "API_AUTH_ENABLED",
        "API_AUTH_TOKEN",
        "DATA_DIR",
        "PROMPTS_DIR",
        "CONFIG_YAML_PATH",
        "LOG_LEVEL",
        "LLM_PROVIDER",
        "CONTINUITY_STRATEGY",
        "MINI_PLAN_BEAT_ALIGNMENT_MAX_CONCURRENCY",
        "CHAPTER_CARD_SEMANTIC_GATE_MAX_CONCURRENCY",
    ):
        monkeypatch.delenv(key, raising=False)
    return monkeypatch


def _make(**env: str) -> Settings:
    """构造 Settings,绕过 .env 文件,避免本地 .env 干扰。"""
    with pytest.MonkeyPatch.context() as patch:
        for k, v in env.items():
            patch.setenv(k, v)
        return Settings(_env_file=None)  # type: ignore[call-arg]


@pytest.mark.parametrize("previous", [None, "/tmp/existing-prompts"])
def test_make_restores_environment_after_success_and_error(monkeypatch, previous):
    import os
    if previous is None:
        monkeypatch.delenv("PROMPTS_DIR", raising=False)
    else:
        monkeypatch.setenv("PROMPTS_DIR", previous)
    assert _make(PROMPTS_DIR="/tmp/test-only-prompts").prompts_dir == Path("/tmp/test-only-prompts")
    assert os.environ.get("PROMPTS_DIR") == previous
    with pytest.raises(ValidationError):
        _make(PROMPTS_DIR="/tmp/test-only-prompts", API_PORT="70000")
    assert os.environ.get("PROMPTS_DIR") == previous


def test_defaults_are_safe(clean_env: pytest.MonkeyPatch) -> None:
    """无任何环境变量时,默认绑 127.0.0.1:8000,鉴权关闭(本机自用安全)。"""
    s = Settings(_env_file=None)  # type: ignore[call-arg]
    assert s.api_host == "127.0.0.1"
    assert s.api_port == 8000
    assert s.api_auth_enabled is False
    assert s.log_level == "INFO"
    assert s.data_dir == Path("./workspaces")
    assert s.prompts_dir == Path("prompts")
    # P1-B-22:llm_provider 默认 mock(对齐 CLAUDE.md §维护人环境约束)。
    assert s.llm_provider == "mock"
    assert s.mini_plan_beat_alignment_max_concurrency == 4
    assert s.chapter_card_semantic_gate_max_concurrency == 2


def test_post_v6_parallel_fallback_envs(clean_env: pytest.MonkeyPatch) -> None:
    s = _make(
        MINI_PLAN_BEAT_ALIGNMENT_MAX_CONCURRENCY="1",
        CHAPTER_CARD_SEMANTIC_GATE_MAX_CONCURRENCY="1",
    )
    assert s.mini_plan_beat_alignment_max_concurrency == 1
    assert s.chapter_card_semantic_gate_max_concurrency == 1


@pytest.mark.parametrize(
    ("key", "value"),
    [
        ("MINI_PLAN_BEAT_ALIGNMENT_MAX_CONCURRENCY", "5"),
        ("CHAPTER_CARD_SEMANTIC_GATE_MAX_CONCURRENCY", "3"),
    ],
)
def test_post_v6_parallel_caps_reject_unbounded_values(
    clean_env: pytest.MonkeyPatch,
    key: str,
    value: str,
) -> None:
    with pytest.raises(ValidationError):
        _make(**{key: value})


def test_llm_provider_env_override(clean_env: pytest.MonkeyPatch) -> None:
    """LLM_PROVIDER=claude 时切到 claude provider。"""
    s = _make(LLM_PROVIDER="claude")
    assert s.llm_provider == "claude"


def test_llm_provider_ollama_accepted(clean_env: pytest.MonkeyPatch) -> None:
    """P3-B-01:LLM_PROVIDER=ollama 加入合法集(本地 Qwen 通过 ollama native API)。"""
    s = _make(LLM_PROVIDER="ollama")
    assert s.llm_provider == "ollama"


def test_llm_provider_openai_compatible_accepted(clean_env: pytest.MonkeyPatch) -> None:
    """v1.0.x post-wave5 H1:LLM_PROVIDER=openai_compatible 加入合法集
    (覆盖 dashscope qwen3.8-max / DeepSeek / Moonshot 等 OpenAI 兼容 API)。"""
    s = _make(LLM_PROVIDER="openai_compatible")
    assert s.llm_provider == "openai_compatible"


def test_llm_provider_invalid_value_rejected(clean_env: pytest.MonkeyPatch) -> None:
    """LLM_PROVIDER 非允许值由 Literal 类型校验拒绝。

    合法集:claude / ollama / openai_compatible / mock(v1.0.x post-wave5 H1
    起 4 档)。本测用一个明显非法的字面(``totally_fake_provider``)验证
    Literal 校验生效。
    """
    clean_env.setenv("LLM_PROVIDER", "totally_fake_provider")
    with pytest.raises(ValidationError):
        Settings(_env_file=None)  # type: ignore[call-arg]


def test_config_yaml_path_default(clean_env: pytest.MonkeyPatch) -> None:
    """P3-B-01:config_yaml_path 默认指向仓库根 ``config.yaml``。"""
    s = Settings(_env_file=None)  # type: ignore[call-arg]
    assert s.config_yaml_path == Path("config.yaml")


def test_config_yaml_path_env_override(clean_env: pytest.MonkeyPatch) -> None:
    """P3-B-01:CONFIG_YAML_PATH 可被 env 覆盖,供测试 / 多配置场景。"""
    s = _make(CONFIG_YAML_PATH="/tmp/custom-config.yaml")
    assert s.config_yaml_path == Path("/tmp/custom-config.yaml")


def test_prompts_dir_env_override(clean_env: pytest.MonkeyPatch) -> None:
    """PROMPTS_DIR 环境变量可覆盖默认值,供测试或工作区级覆盖路径配置。"""
    s = _make(PROMPTS_DIR="/tmp/custom-prompts")
    assert s.prompts_dir == Path("/tmp/custom-prompts")


def test_env_overrides_defaults(clean_env: pytest.MonkeyPatch) -> None:
    """环境变量正确覆盖默认值;大小写不敏感(api_host vs API_HOST)。"""
    s = _make(API_HOST="127.0.0.1", API_PORT="9001", LOG_LEVEL="DEBUG")
    assert s.api_host == "127.0.0.1"
    assert s.api_port == 9001
    assert s.log_level == "DEBUG"


def test_public_host_without_auth_raises(clean_env: pytest.MonkeyPatch) -> None:
    """API_HOST=0.0.0.0 + API_AUTH_ENABLED=false → 启动 import 即抛错。"""
    with pytest.raises(ValidationError, match="0.0.0.0"):
        _make(API_HOST="0.0.0.0", API_AUTH_ENABLED="false")


def test_public_host_with_auth_passes(clean_env: pytest.MonkeyPatch) -> None:
    """API_HOST=0.0.0.0 + 鉴权开启 + token 非空 → 允许。"""
    s = _make(
        API_HOST="0.0.0.0",
        API_AUTH_ENABLED="true",
        API_AUTH_TOKEN="a-very-long-random-token",
    )
    assert s.api_host == "0.0.0.0"
    assert s.api_auth_enabled is True


def test_auth_enabled_requires_token(clean_env: pytest.MonkeyPatch) -> None:
    """开了鉴权但 token 留空也应拒绝,避免半残配置。"""
    with pytest.raises(ValidationError, match="API_AUTH_TOKEN"):
        _make(API_AUTH_ENABLED="true", API_AUTH_TOKEN="")


def test_invalid_log_level_raises(clean_env: pytest.MonkeyPatch) -> None:
    with pytest.raises(ValidationError, match="LOG_LEVEL"):
        _make(LOG_LEVEL="VERBOSE")


def test_invalid_port_raises(clean_env: pytest.MonkeyPatch) -> None:
    with pytest.raises(ValidationError):
        _make(API_PORT="70000")


def test_secret_fields_redacted_in_repr(clean_env: pytest.MonkeyPatch) -> None:
    """SecretStr 必须在 repr 里隐藏原值,防止误进日志。"""
    s = _make(CLAUDE_API_KEY="sk-ant-secret-real-value")
    assert "sk-ant-secret-real-value" not in repr(s)
    assert "sk-ant-secret-real-value" not in str(s)
    # 取真实值要显式调用 .get_secret_value()
    assert s.claude_api_key.get_secret_value() == "sk-ant-secret-real-value"


def test_engine_version_is_string() -> None:
    """版本号常量存在且为非空字符串。"""
    assert isinstance(ENGINE_VERSION, str)
    assert ENGINE_VERSION
