"""API key 凭证管理服务(P6-B-16)。

供 ``PUT / GET /api/v1/config/api-keys`` 端点写入 / 回显进程级 API key
(``Settings.claude_api_key`` 等)。本服务**不**走 ``ConfigService`` 的
``project.yaml`` 路径 — API key 与 workspace 解耦,是进程级凭证(全 workspace
共享 ``Settings`` 字段)。

存储介质决策(P6-plan-kickoff §决策 9)
=====================================
维护人 2026-05-17 拍板走 ``.env`` 文件 + 原地刷新 ``Settings`` 单例。理由:

- 与 ``Settings.claude_api_key`` 同源(``src/config.py:31`` 已经从 ``.env``
  通过 ``pydantic-settings`` 加载),本端点是给"已有 env 变量"加 PUT 入口,
  非新增凭证流。
- 无新依赖(``python-dotenv`` 已装,``Settings`` 隐式用)+ 全平台一致(纯文件)
  + ``.env.example`` 字面登记 ``CLAUDE_API_KEY``。
- 安全保护:``.gitignore`` 已配 ``.env`` + 600 文件权限 + 日志侧 ``_mask`` 脱敏。

刷新策略(plan-kickoff §决策 9 子选项 A1)
==========================================
PUT 成功后**原地刷新** ``settings.claude_api_key`` 字段(``object.__setattr__``
绕开 Pydantic v2 BaseSettings 默认 ``frozen=False`` 的潜在校验副作用)。Writer
/ Reviewer 等下次任何调用即用新 key,无需重启后端。代价是单元测试需 fixture
保存原值 + teardown 恢复(mutable global state)。备选 A2 ``requires_restart``
被否决,理由 UX 差(ConfigPage 改完不能立即生效)。

WS event
========
PUT 成功后 publish ``api_key_updated`` 到 ``system.alerts`` 频道(API key
是系统级配置不属任何 workspace)— 多客户端 ConfigPage 实时刷新展示。
publish 失败 swallow + structlog 不影响 PUT 返回(对齐 ``ConfigService``
同型)。

不做什么
========
- 不支持批量 PUT(每次只写一个 provider,失败原子性 — 任一 provider 失败
  不影响其他)。
- 不暴露明文 GET(脱敏列表是 GET 的唯一形态)。
- 不动 LLM client 工厂签名(Writer / Reviewer 仍走 ``from src.config import
  settings; settings.claude_api_key``,不感知 ``ApiKeyService`` 存在)。
- 不开新 Alembic 表(走 ``.env`` 文件存储,无 DB 表)。
"""

from __future__ import annotations

from pathlib import Path
from typing import Any

import structlog
from dotenv import set_key
from pydantic import SecretStr

from src import config as _config_module
from src.api.websocket.channels import system_alerts_channel

_log = structlog.get_logger(__name__)


# =============================================================================
# 业务异常(供 routes 层翻译为 ApiError 子类)
# =============================================================================


class ApiKeyProviderUnknownError(ValueError):
    """``provider`` 不在 ``KNOWN_PROVIDERS`` 枚举内。

    routes 层翻 422 ``API_KEY_PROVIDER_UNKNOWN``。继承 ``ValueError`` 让既有
    Pydantic 异常翻译路径也能兜底(若 schema Literal 校验放行,service 层
    再断一次)。
    """


class ApiKeyWriteFailedError(IOError):
    """``.env`` 文件写入失败(权限 / 磁盘满 / 路径异常)。

    routes 层翻 500 ``API_KEY_WRITE_FAILED``。继承 ``IOError`` 与 ``set_key``
    可能抛的 OS 层异常对齐。
    """


# =============================================================================
# Provider 注册表(扩展点)
# =============================================================================


KNOWN_PROVIDERS: tuple[str, ...] = (
    "claude",
    "dashscope",
    "deepseek",
    "openai_compatible",
)
"""当前支持 PUT / GET 的 API key provider 枚举。

与 ``src/api/schemas/config.py:_KNOWN_API_KEY_PROVIDERS`` + ``ApiKeyIn.provider``
Literal 三处同步。**v1.0.x post-wave5 H1 起追加 ``dashscope``**(阿里云通义
千问云端 API,覆盖 qwen3.8-max 等);**v1.1 wave 2 H2 起追加 ``deepseek``
+ ``openai_compatible`` 通配**(后者覆盖 Moonshot / 智谱 GLM / OpenAI 官方
等所有未单独列字段的兼容 provider)。ollama 本地 HTTP 端点不需要 API key,
不在本枚举。
"""


# provider → (.env key name, Settings 属性名)
_PROVIDER_REGISTRY: dict[str, tuple[str, str]] = {
    "claude": ("CLAUDE_API_KEY", "claude_api_key"),
    "dashscope": ("DASHSCOPE_API_KEY", "dashscope_api_key"),
    "deepseek": ("DEEPSEEK_API_KEY", "deepseek_api_key"),
    "openai_compatible": ("OPENAI_COMPATIBLE_API_KEY", "openai_compatible_api_key"),
}
"""``provider`` → ``(env_var_name, settings_attr_name)`` 映射。

- ``env_var_name`` 用于 ``.env`` 文件键名(``CLAUDE_API_KEY`` /
  ``DASHSCOPE_API_KEY``)。
- ``settings_attr_name`` 用于 ``settings.<attr>`` 原地刷新 + GET 脱敏读取。
"""


# =============================================================================
# 脱敏
# =============================================================================


_MASK_PREFIX_LEN = 8
"""脱敏保留前缀长度。Anthropic key 前缀 ``sk-ant-`` 7 字符 + 版本号 1 字符 = 8。

只回显前 8 字符无独立熵(前缀均同),后端可读但前端无法反推完整 key。
对齐 ``OBSERVABILITY §3.3`` 字面 "API key 必须脱敏 ``***REDACTED***``" —
本服务用前缀回显 + ``***`` 取代纯 ``***REDACTED***`` 是为前端能视觉对账
"key 是否写对"。
"""


def _mask(value: str) -> str | None:
    """脱敏字符串:空 → None;非空 → ``<前 8 字符>***``。

    用于 GET 响应 + WS event ``data`` + structlog 字段。
    """
    if not value:
        return None
    return f"{value[:_MASK_PREFIX_LEN]}***"


# =============================================================================
# 服务类
# =============================================================================


class ApiKeyService:
    """API key 写入 / 读取脱敏视图(P6-B-16)。

    构造参数
    --------
    - ``env_path``:``.env`` 文件路径;默认 ``Path(".env")`` 相对工作目录
      (与 ``Settings.model_config["env_file"]`` 字面同步)。测试场景注入
      ``tmp_path / ".env"``。
    - ``ws_manager``:可选 WS manager;给 PUT 成功后 publish ``api_key_updated``
      到 ``system.alerts``。None → 静默跳过 publish。
    """

    def __init__(
        self,
        *,
        env_path: Path | None = None,
        ws_manager: object | None = None,
    ) -> None:
        self._env_path = Path(env_path) if env_path is not None else Path(".env")
        self._ws_manager = ws_manager

    # ---- 读 ------------------------------------------------------------------

    def get_masked(self) -> list[dict[str, Any]]:
        """返回所有 ``KNOWN_PROVIDERS`` 的脱敏状态列表。

        ``configured=False`` + ``masked_value=None`` 表 ``Settings`` 字段为
        空字符串(未配置);True 时回显 ``<前 8 字符>***``。
        """
        result: list[dict[str, Any]] = []
        for provider in KNOWN_PROVIDERS:
            _, attr = _PROVIDER_REGISTRY[provider]
            secret = getattr(_config_module.settings, attr)
            raw = secret.get_secret_value() if isinstance(secret, SecretStr) else str(secret)
            masked = _mask(raw)
            result.append(
                {
                    "provider": provider,
                    "configured": masked is not None,
                    "masked_value": masked,
                }
            )
        return result

    # ---- 写 ------------------------------------------------------------------

    def put(self, provider: str, api_key: str) -> dict[str, Any]:
        """写入指定 provider 的 API key。

        步骤:
        1. 校验 provider in KNOWN_PROVIDERS,否则抛 ``ApiKeyProviderUnknownError``。
        2. ``python-dotenv.set_key`` 写入 ``.env`` 文件(保留其他 env 变量、
           注释、顺序;.env 不存在时自动新建)。失败抛 ``ApiKeyWriteFailedError``。
        3. 原地刷新 ``settings.<attr>`` 字段(``SecretStr(new_key)`` 包装)—
           Writer / Reviewer 下次调用即用新 key。
        4. publish WS ``api_key_updated`` 事件到 ``system.alerts``(失败 swallow)。

        返回:更新后该 provider 的脱敏状态 dict(对齐 ``get_masked`` 单条元素)。
        """
        if provider not in KNOWN_PROVIDERS:
            raise ApiKeyProviderUnknownError(
                f"未知的 API key provider: {provider!r};可选 {list(KNOWN_PROVIDERS)}"
            )

        env_var, attr = _PROVIDER_REGISTRY[provider]

        # 步骤 2:写 .env(set_key 保留其它行 / 注释 / 顺序;文件不存在自动建)
        try:
            set_key(str(self._env_path), env_var, api_key)
        except OSError as exc:
            _log.error(
                "api_key_write_failed",
                provider=provider,
                env_path=str(self._env_path),
                error_type=type(exc).__name__,
                error_message=str(exc),
            )
            raise ApiKeyWriteFailedError(
                f".env 文件写入失败({self._env_path}):{exc}"
            ) from exc

        # 步骤 3:原地刷新 Settings 单例(mutable global state — 单元测试隔离用 fixture)
        object.__setattr__(_config_module.settings, attr, SecretStr(api_key))

        masked = _mask(api_key)
        _log.info(
            "api_key_updated",
            provider=provider,
            masked_value=masked,
        )

        # 步骤 4:WS publish(失败 swallow + structlog)
        self._publish_system_event(
            event="api_key_updated",
            data={"provider": provider, "masked_value": masked},
        )

        return {
            "provider": provider,
            "configured": True,
            "masked_value": masked,
        }

    # ---- 内部 ----------------------------------------------------------------

    def _publish_system_event(
        self,
        *,
        event: str,
        data: dict[str, Any],
    ) -> None:
        """WS publish 到 ``system.alerts``;失败 swallow + log。

        对齐 ``ConfigService._publish_workspace_event`` 同型(structlog ``event``
        首参冲突 → 业务 event 改名 ``ws_event``)。
        """
        if self._ws_manager is None:
            return
        try:
            self._ws_manager.publish(  # type: ignore[attr-defined]
                channel=system_alerts_channel(),
                event=event,
                data=data,
            )
        except Exception as exc:  # noqa: BLE001
            _log.error(
                "api_key_event_notify_failed",
                ws_event=event,
                error_type=type(exc).__name__,
                error_message=str(exc),
            )


__all__ = [
    "ApiKeyProviderUnknownError",
    "ApiKeyService",
    "ApiKeyWriteFailedError",
    "KNOWN_PROVIDERS",
]
