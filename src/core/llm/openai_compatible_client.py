"""OpenAI 兼容 API 客户端(v1.0.x post-wave5 H1 提前实施)。

继承 ``LLMClient`` 基类,用 ``httpx`` 直接 POST ``{base_url}/chat/completions``。
生产默认保持 **stream=True + SSE 增量解析**；有界校准可显式切换为
``stream=False`` 完整 JSON 响应，两条路径都拆分 ``reasoning_content``。覆盖
dashscope 通义千问云端 API(qwen3.8-max 等)、DeepSeek、Moonshot 等所有
OpenAI 兼容 endpoint。**不引入 openai SDK**(对齐 CLAUDE.md §硬约束 1
"不引入文档外的依赖")—— 直接 httpx 走 HTTP/SSE。

为何走 stream + 自己解析 SSE(非阻塞 sync 等待)
=================================================
长 thinking 模式(qwen3.8-max + ``enable_thinking=true``)的单次响应耗时可达
60-300s,sync 等待整个 JSON 响应在工程上不可接受:① httpx 阻塞会让 worker
连接池长占用;② 用户在 ChapterEditor 看不到中间进度;③ 服务端 504 / 中间
代理超时风险显著。stream 模式 chunk 流式回写,worker 周期性 yield,且能在
SSE 断流时早期检测并 retry。

SSE 协议参考(基于 dashscope OpenAI 兼容模式实测)
==================================================
- 每行 ``data: <json>\\n``,空行(``\\n``)作 chunk 分隔
- 终止符 ``data: [DONE]\\n\\n``
- chunk JSON 形如::

    {"choices": [{"delta": {"content": "...", "reasoning_content": "..."},
                   "index": 0, "finish_reason": null}],
     "usage": null}

- ``usage`` 字段(``prompt_tokens`` / ``completion_tokens``)只在最后一个含
  ``finish_reason`` 的 chunk 出现 —— 需请求体显式传
  ``stream_options.include_usage=true``,否则 usage 永远是 null。
- **thinking 模式**:qwen3.8-max + ``enable_thinking=true`` 时 SSE 先流出
  ``delta.reasoning_content``(思考过程,``delta.content`` 为 null);思考完毕
  后才转为 ``delta.content`` 有值。本客户端把两者拆到独立缓冲:
  ``content_buffer`` 拼到 ``LLMResponse.content`` 返调用方;
  ``reasoning_buffer`` 只入 structlog ``llm_response.reasoning_chars``
  字段(不污染 message_history,防止下次 prompt 把思考过程再发给 LLM)。

Qwen thinking 扩展字段位置
==========================
``extra_body`` 是 OpenAI Python SDK 的 SDK 级 syntactic sugar(SDK 内部把
``extra_body`` dict 平铺到 HTTP body 顶层)。本客户端用 httpx 直接 POST,
**``enable_thinking`` 与 ``preserve_thinking`` 字段直接放 HTTP body 顶层**
(与 cURL 路径一致),
不嵌套 ``extra_body``。

字段映射约定
============
- ``Message`` → OpenAI ``{"role": ..., "content": ...}`` 直接透传(system /
  user / assistant 三角色全支持,与 ollama 同型,与 Claude 系统提示 pop-up
  不同)。
- ``Message.role`` 直接放 messages 数组(无需 split system)。
- 默认 ``stream`` 强制 ``True``；构造时显式 ``stream_response=False`` 才切到
  完整 JSON 响应。
- stream 路径强制 ``stream_options={"include_usage": True}``；非 stream 路径
  显式发送 ``stream=False`` 且省略 ``stream_options``。
- ``enable_thinking`` / ``preserve_thinking`` 只要是显式布尔值就顶层透传(由构造默认或
  ``complete`` 单次 keyword 覆盖);``None`` 才表示兼容未知 provider 而省略。
- Qwen Max 的 ``max_tokens`` 只限制最终回答，不自动缩小模型思考链。
  caller 如确需限制“思维链 + 回答”的完整输出，可显式传
  ``max_completion_tokens``；客户端只做官方单请求上限预检与原样透传。
- ``model`` 取构造时 ``self._model``,``complete(model="...")`` 单次覆盖。
- 其他 OpenAI 标准 params(``temperature`` / ``max_tokens`` / ``top_p`` / ...)
  从 ``**gen_params`` 透传到 body 顶层。

成本字段
========
``Settings.llm_provider == "openai_compatible"`` 路径下,本客户端 ``llm_response``
事件 ``cost_usd`` 字段打 0.0 占位(对齐 ollama / claude 同型),真实成本
计算由 Router 层(P3-B-05 + P3-B-07 cost tracker)按
``WriterProfile.cost_per_1k_input/output`` 表落地到 ``chapters.total_cost_usd``。

P1-P7 累积纪律继承
==================
- OBSERVABILITY §2.4 ``llm_request`` / ``llm_response`` 事件双发(P1-B-24 同型)
- ``archive_prompt`` 走 workspaces/<id>/prompts/<hash>.md(P1-B-24 决策 3)
- ``hash_prompt`` 走 SHA256(P1-B-24 同型)
- ``detect_agent_role`` 从 messages 内嗅探 agent 标识(P1-B-24 同型)
- 4xx 立即抛 + 5xx / 连接错误 / 超时 retry 3 次指数退避(P1-B-08 同型)
- workspace_id 从 structlog contextvars 读;无绑定时跳过归档(P1-B-24 同型)

不实现
======
- tool use(OpenAI function calling,留 v1.2+)
- 多模态(image / audio,留 v1.2+)
- 异步 ``acomplete``(留 v1.2+,base.py 已预留 keyword-only 演进位)
"""

from __future__ import annotations

import json
import hashlib
import re
import time
from collections.abc import Callable
from functools import partial
from typing import Any, TypeVar
from urllib.parse import urlparse

import httpx
import structlog
from pydantic import SecretStr

from src.config import settings
from src.core.agents._prompt import detect_agent_role
from src.core.llm.attempt_context import (
    ProviderAttemptOutcome,
    ProviderResponseStatus,
    run_observed_provider_attempt,
)
from src.core.llm.base import (
    MAX_PROVIDER_ATTEMPTS_PER_LOGICAL_CALL,
    LLMAccessError,
    LLMAuthError,
    LLMClient,
    LLMResponse,
    Message,
    TokenUsage,
)
from src.core.llm.cancel import raise_if_cancelled
from src.core.llm.prompt_hash import archive_prompt, hash_prompt, render_prompt_text
from src.core.llm.strict_json_schema import (
    StrictJsonResponseViolation,
    StrictJsonSchemaDriftError,
    TOKENHUB_HY3_BASE_URL,
    TOKENHUB_HY3_MODEL,
    append_strict_json_instruction,
    assert_hy3_strict_request,
    strict_contract_from_params,
    validate_strict_json_response,
)

_log = structlog.get_logger(__name__)

# 长 thinking 模式响应实测可达 60-300s,给 600s 默认 timeout 容纳极端长 prompt
# + 高 max_tokens 场景;caller 可在构造时覆盖。httpx 的 float timeout 会分别用于
# connect/read/write/pool；其中 read 是等待下一批网络数据的最长静默时间，不是
# 整次请求的总墙钟。
_DEFAULT_TIMEOUT_SECONDS = 600.0

# xhigh 规划调用已有约 748s 的正常完成实证，因此总墙钟不能复用 600s read
# timeout。1200s 为持续 SSE 保留长规划余量，同时把“只要一直吐 chunk 就永不结束”
# 从 2400s pipeline watchdog 之前收口。caller/测试仍可单独覆盖。
_DEFAULT_TOTAL_TIMEOUT_SECONDS = 1200.0

# 重试参数对齐 ClaudeClient / OllamaClient(P1-B-08 + P3-B-03 同型):3 次、
# 1s 起步指数退避(1s → 2s → 4s)。
_MAX_ATTEMPTS = MAX_PROVIDER_ATTEMPTS_PER_LOGICAL_CALL
_INITIAL_DELAY_SECONDS = 1.0

# OBSERVABILITY §2.4 ``llm_request`` / ``llm_response`` 事件 ``provider`` 字段。
_PROVIDER = "openai_compatible"

_MAX_PROVIDER_ERROR_FIELD_CHARS = 500
"""Provider 错误字段的最大保留长度，避免把整段 HTML/body 落账。"""


def _safe_provider_error_field(value: object) -> str | None:
    """只保留 provider JSON 白名单字段中的短文本，并二次脱敏。"""

    if not isinstance(value, str):
        return None
    text = " ".join(value.split())
    if not text:
        return None
    text = re.sub(
        r"(?i)(authorization\s*[:=]\s*bearer\s+)[^\s,;]+",
        r"\1[REDACTED]",
        text,
    )
    text = re.sub(r"\bsk-[A-Za-z0-9_-]{8,}\b", "[REDACTED]", text)
    return text[:_MAX_PROVIDER_ERROR_FIELD_CHARS]


def _extract_provider_error_fields(
    response: httpx.Response,
) -> tuple[str | None, str | None, str | None]:
    """从 401/403 JSON 中只提取 code/message/request_id 白名单。"""

    try:
        payload = response.json()
    except (json.JSONDecodeError, UnicodeDecodeError, httpx.ResponseNotRead):
        return None, None, None
    if not isinstance(payload, dict):
        return None, None, None
    return _provider_error_fields_from_payload(payload)


def _provider_error_fields_from_payload(
    payload: dict[str, Any],
) -> tuple[str | None, str | None, str | None]:
    """Extract the same bounded error fields from HTTP or SSE payloads."""

    nested = payload.get("error")
    error = nested if isinstance(nested, dict) else {}

    def first(*values: object) -> str | None:
        for value in values:
            safe = _safe_provider_error_field(value)
            if safe is not None:
                return safe
        return None

    return (
        first(error.get("code"), payload.get("code")),
        first(error.get("message"), payload.get("message")),
        first(
            error.get("request_id"),
            error.get("requestId"),
            payload.get("request_id"),
            payload.get("requestId"),
        ),
    )


def _provider_error_suffix(
    *,
    provider_code: str | None,
    provider_message: str | None,
    provider_request_id: str | None,
) -> str:
    """把安全结构化字段编成可操作的短错误文本。"""

    parts: list[str] = []
    if provider_code is not None:
        parts.append(f"provider_code={provider_code}")
    if provider_message is not None:
        parts.append(f"provider_message={provider_message}")
    if provider_request_id is not None:
        parts.append(f"provider_request_id={provider_request_id}")
    return f" ({', '.join(parts)})" if parts else ""


# 2026-08-16 阿里云百炼官方公开 API 口径。只对本仓库已接入的 Qwen Max
# 系列启用，避免把 DashScope 专属限制错误套给其它兼容 provider。3.8 的
# 输入边界与 3.7 相同，输出由 64K 提升为 128K；历史 3.7 路径仍按原上限预检。
_QWEN_MAX_MODEL_LIMITS: dict[str, tuple[str, int, int, int]] = {
    "qwen3.8-max": ("Qwen3.8 Max", 991_808, 983_616, 131_072),
    "qwen3.8-27b": ("Qwen3.8-27B", 991_808, 983_616, 131_072),
    "qwen3.7-max": ("Qwen3.7 Max", 991_808, 983_616, 65_536),
    "qwen3.7-max-2026-05-20": ("Qwen3.7 Max", 991_808, 983_616, 65_536),
    "qwen3.7-max-2026-06-08": ("Qwen3.7 Max", 991_808, 983_616, 65_536),
}
_QWEN_MAX_THINKING_BUDGET = 262_144

_QWEN_STRICT_JSON_SCHEMA_MODEL_RE = re.compile(
    r"^qwen3\.(?:8-max|7-(?:max|plus))(?:$|-)",
    re.IGNORECASE,
)


def _is_dashscope_compatible_endpoint(base_url: str) -> bool:
    host = (urlparse(base_url).hostname or "").lower()
    return host.endswith(".aliyuncs.com") and (
        "dashscope" in host or ".maas." in f".{host}."
    )


def _strict_json_schema_metadata(
    response_format: object,
) -> tuple[str | None, str | None]:
    if (
        not isinstance(response_format, dict)
        or response_format.get("type") != "json_schema"
    ):
        return None, None
    config = response_format.get("json_schema")
    if not isinstance(config, dict) or config.get("strict") is not True:
        return None, None
    name = config.get("name") if isinstance(config.get("name"), str) else None
    schema = config.get("schema")
    if not isinstance(schema, dict):
        return name, None
    encoded = json.dumps(
        schema,
        ensure_ascii=False,
        sort_keys=True,
        separators=(",", ":"),
    ).encode("utf-8")
    return name, hashlib.sha256(encoded).hexdigest()


def _message_token_upper_bound(messages: list[Message]) -> int:
    """以 UTF-8 bytes + 消息模板余量保守上界估算输入 token。

    Qwen tokenizer 的每个 token 至少承载一个输入 byte，因此 byte 数是比
    ``字符数 // 3`` 更保守的网络前上界；每条消息再预留 32 token，并给 chat
    template 固定预留 64 token。该值只用于接近约 1M 上限时拒绝明显超大请求，
    provider 返回的 usage 仍是实际计量权威。
    """

    return 64 + sum(len(message.content.encode("utf-8")) + 32 for message in messages)


# SSE 行前缀(OpenAI 标准),用于解析 chunked transfer 中的有效数据行。
_SSE_DATA_PREFIX = "data: "
# SSE 流终止符(OpenAI 标准),收到此行后停止解析。
_SSE_DONE_MARKER = "[DONE]"

T = TypeVar("T")


class OpenAICompatibleClientError(RuntimeError):
    """OpenAI 兼容 client 自定义异常基类。

    routes / service 层捕获 ``OpenAICompatibleClientError`` 子类即可,无需
    再细分 SSE / JSON / 协议三类。子类:

    - ``SSEStreamError``:SSE 中途断流 / 协议非法
    - ``InvalidChunkJSONError``:某个 chunk 的 ``data: <...>`` 非 JSON
    """

    def __init__(
        self,
        message: str,
        *,
        provider_request_id: str | None = None,
    ) -> None:
        self.provider_request_id = provider_request_id
        super().__init__(message)


class SSEStreamError(OpenAICompatibleClientError):
    """SSE 流意外终止(未见 [DONE] 即 EOF)或行协议非法。

    重试逻辑会捕获并 retry(等同瞬时网络抖动);3 次后仍失败抛给 caller。
    """


class SSEProviderError(OpenAICompatibleClientError):
    """Provider emitted an explicit top-level SSE error frame.

    TokenHub can terminate a nominal HTTP-200 stream with ``{"error": ...}``
    followed by ``[DONE]``.  Treating that frame as an empty choices chunk
    would lose the real failure and often degrade into a misleading missing-
    usage error.  The bounded provider fields are safe to persist and this
    protocol error is never retried automatically.
    """

    def __init__(
        self,
        message: str,
        *,
        provider_code: str | None = None,
        provider_message: str | None = None,
        provider_request_id: str | None = None,
    ) -> None:
        self.provider_code = provider_code
        self.provider_message = provider_message
        super().__init__(
            message,
            provider_request_id=provider_request_id,
        )


class InvalidChunkJSONError(OpenAICompatibleClientError):
    """单个 ``data: <payload>`` 行的 payload 非合法 JSON。

    通常是上游服务端返了非标准响应(rate-limit 提示纯文本插进 SSE 流等)。
    本类**不**进重试 — 服务端正在以非标准方式响应,重试无意义,直接抛。
    """


class NonStreamingResponseError(OpenAICompatibleClientError):
    """非流式响应不是可审计的完整 OpenAI Chat completion。

    该错误通常表示对端或链路返回了截断/缺字段 JSON。它属于 technical failure，
    与连接断流同样进入有界 retry；上层仍须把每次物理请求分别计数。
    """


class ProviderUsageMissingError(OpenAICompatibleClientError):
    """Provider 已结束响应，但缺少可审计 usage。

    此时请求可能已经完整计费，自动重试只会制造第二笔费用；因此本异常永不
    重试，由 attempt 账本保留最坏预留并把 usage 明确记成 unknown。
    """


class SSETotalTimeoutError(OpenAICompatibleClientError):
    """SSE 持续有数据但整次请求超过本地总墙钟。

    与瞬时断流不同，本错误说明同一物理请求已经长时间占用 Provider；自动重试会
    放大未知 usage，因此故意不进入 transport retry，由上层安全暂停并等待恢复。
    """


def _is_retryable(exc: BaseException) -> bool:
    """判断异常是否可重试。

    重试范围:
    - ``httpx.ConnectError`` / ``httpx.ConnectTimeout``:网络抖动
    - ``httpx.ReadTimeout`` / ``httpx.RemoteProtocolError``:中途断流
    - ``httpx.HTTPStatusError`` 5xx:服务端临时不可用
    - ``SSEStreamError``:SSE 中途断流(等同 ReadTimeout)

    不重试:
    - ``httpx.HTTPStatusError`` 4xx(api key / 参数 / model 不存在)
    - ``InvalidChunkJSONError``(服务端返非 OpenAI 兼容响应,重试无意义)
    - ``SSETotalTimeoutError``(同一长请求 usage 未知，禁止自动放大)
    - 其他业务异常
    """
    if isinstance(
        exc,
        (
            httpx.ConnectError,
            httpx.ConnectTimeout,
            httpx.ReadTimeout,
            httpx.RemoteProtocolError,
            SSEStreamError,
            NonStreamingResponseError,
        ),
    ):
        return True
    if isinstance(exc, httpx.HTTPStatusError) and 500 <= exc.response.status_code < 600:
        return True
    return False


def _retry(
    call: Callable[[], T],
    *,
    max_attempts: int = _MAX_ATTEMPTS,
    outcome_from_result: Callable[[T], ProviderAttemptOutcome] | None = None,
    outcome_from_error: Callable[[BaseException], ProviderAttemptOutcome] | None = None,
) -> T:
    """对可重试异常按指数退避重试,最多 ``max_attempts`` 次。"""
    last_exc: BaseException | None = None
    for attempt in range(max_attempts):
        try:
            return run_observed_provider_attempt(
                call,
                technical_attempt=attempt + 1,
                outcome_from_result=outcome_from_result,
                outcome_from_error=outcome_from_error,
            )
        except Exception as exc:
            if not _is_retryable(exc):
                raise
            last_exc = exc
            if attempt + 1 < max_attempts:
                time.sleep(_INITIAL_DELAY_SECONDS * (2**attempt))
    assert last_exc is not None  # 循环结构保证至少触发过一次异常
    raise last_exc


def _format_messages(messages: list[Message]) -> list[dict[str, str]]:
    """``Message`` 序列 → OpenAI 兼容 messages 数组。

    OpenAI 兼容 ``/v1/chat/completions`` 接 ``[{"role": "system|user|assistant",
    "content": "..."}]``,三角色全在 messages 数组内(与 Anthropic SDK 把
    system 单独提到顶层不同;与 ollama 同型)。
    """
    return [{"role": m.role, "content": m.content} for m in messages]


class _StreamResult:
    """SSE 流解析结果的简单容器(避免返多元组难读)。

    Attributes:
        content: 拼接后的正文(送回 ``LLMResponse.content``)。
        reasoning: 拼接后的思考过程(仅入 structlog,不入正文)。
        input_tokens: 服务端报告的 ``usage.prompt_tokens``；缺 usage 时不会
            返回本对象，而是抛 ``ProviderUsageMissingError``。
        output_tokens: 服务端报告的 ``usage.completion_tokens``;同上。
        finish_reason: 最后一个含 ``finish_reason`` 的 chunk 值
            (``stop`` / ``length`` / ``tool_calls`` / ``content_filter``);
            流中途断时为 ``None``。
        model_used: 实际响应模型名(从 chunk ``model`` 字段读);默认请求模型名。
    """

    def __init__(self, requested_model: str) -> None:
        self.content_chunks: list[str] = []
        self.reasoning_chunks: list[str] = []
        self.input_tokens: int = 0
        self.output_tokens: int = 0
        self.finish_reason: str | None = None
        self.model_used: str = requested_model
        self.usage_received: bool = False
        self.provider_request_id: str | None = None

    @property
    def content(self) -> str:
        return "".join(self.content_chunks)

    @property
    def reasoning(self) -> str:
        return "".join(self.reasoning_chunks)


def _provider_request_id_from_response(
    response: httpx.Response,
    *,
    payload_id: object = None,
) -> str | None:
    """提取短 request/completion id；不保留 body、header 集或凭据。"""

    for name in ("x-request-id", "request-id", "x-dashscope-request-id"):
        value = response.headers.get(name)
        if value and value.strip():
            return value.strip()[:256]
    if isinstance(payload_id, str) and payload_id.strip():
        return payload_id.strip()[:256]
    return None


def _openai_result_outcome(result: _StreamResult) -> ProviderAttemptOutcome:
    return ProviderAttemptOutcome.known(
        input_tokens=result.input_tokens,
        output_tokens=result.output_tokens,
        provider_request_id=result.provider_request_id,
    )


def _openai_error_outcome(exc: BaseException) -> ProviderAttemptOutcome:
    request_id = getattr(exc, "provider_request_id", None)
    if isinstance(exc, httpx.HTTPStatusError):
        _code, _message, payload_request_id = _extract_provider_error_fields(
            exc.response
        )
        request_id = payload_request_id or _provider_request_id_from_response(
            exc.response
        )
    response_status: ProviderResponseStatus
    if isinstance(exc, ProviderUsageMissingError):
        response_status = "usage_missing"
    elif isinstance(
        exc,
        (
            SSEStreamError,
            SSETotalTimeoutError,
            httpx.ReadTimeout,
            httpx.RemoteProtocolError,
        ),
    ):
        response_status = "stream_incomplete"
    else:
        response_status = "failed"
    return ProviderAttemptOutcome.unknown(
        response_status=response_status,
        provider_request_id=request_id,
    )


class OpenAICompatibleClient(LLMClient):
    """OpenAI 兼容 API 客户端(stream + SSE + reasoning_content 拆分)。

    Args:
        model: 默认请求模型名,如 ``qwen3.8-max``。``complete`` 时也可通过
            ``model="..."`` 覆盖单次调用。
        base_url: OpenAI 兼容 endpoint 前缀(不含 ``/chat/completions`` 尾部),
            如 ``https://dashscope.aliyuncs.com/compatible-mode/v1``。
        api_key: provider 的 API key(``SecretStr`` 包装)。空字符串直接抛
            ``ValueError`` 防止无凭据空跑被 401 卡住。
        enable_thinking: 默认 thinking mode 开关。``True`` / ``False`` 都会
            显式进入 HTTP body；**默认 ``None``**，避免向未知兼容 provider
            注入它不认识的扩展。Qwen Max 的 factory/Router 路径会显式给出布尔值。
        preserve_thinking: 默认思维链历史保留开关。语义与
            ``enable_thinking`` 相同：布尔值显式进入 HTTP body，``None`` 省略。
            活动 Qwen3.8 profile 固定为 ``False``，保持既有 Agent 不复用历史
            思维链的行为。
        timeout_seconds: httpx connect/read/write/pool timeout，默认 600s；不是
            整次请求的总墙钟。
        total_timeout_seconds: SSE 整次请求总墙钟，默认 1200s；即使持续收到
            chunk 也不能越过。
        stream_response: ``True`` 走 SSE；``False`` 显式请求完整 JSON。默认
            ``True``，因此现有生产调用行为不变。
        http_client: 自定义 ``httpx.Client``(测试 mock 注入点)。传入时
            视为外部托管,本类不在 ``close`` 时关闭它。
    """

    def __init__(
        self,
        model: str,
        base_url: str,
        api_key: SecretStr,
        enable_thinking: bool | None = None,
        timeout_seconds: float = _DEFAULT_TIMEOUT_SECONDS,
        stream_response: bool = True,
        http_client: httpx.Client | None = None,
        preserve_thinking: bool | None = None,
        total_timeout_seconds: float = _DEFAULT_TOTAL_TIMEOUT_SECONDS,
        max_attempts: int = _MAX_ATTEMPTS,
    ) -> None:
        if not api_key.get_secret_value():
            raise ValueError(
                "OpenAICompatibleClient 缺少 API key:请设置 provider 对应的"
                " env 变量(如 DASHSCOPE_API_KEY),或在构造时显式传入 api_key。"
            )
        if not base_url:
            raise ValueError(
                "OpenAICompatibleClient 缺少 base_url:请显式传入"
                " WriterProfile.base_url 或构造参数 base_url。"
            )
        self._model = model
        self._base_url = base_url.rstrip("/")
        self._api_key = api_key
        self._default_enable_thinking = enable_thinking
        self._default_preserve_thinking = preserve_thinking
        self._stream_response = stream_response
        if total_timeout_seconds <= 0:
            raise ValueError("total_timeout_seconds 必须为正数")
        self._total_timeout_seconds = float(total_timeout_seconds)
        if (
            isinstance(max_attempts, bool)
            or not isinstance(max_attempts, int)
            or not 1 <= max_attempts <= _MAX_ATTEMPTS
        ):
            raise ValueError(f"max_attempts 必须为 1..{_MAX_ATTEMPTS} 的整数")
        self._max_attempts = max_attempts
        if http_client is not None:
            self._http = http_client
            self._owns_http = False
        else:
            self._http = httpx.Client(timeout=timeout_seconds)
            self._owns_http = True

    def prepare_strict_json_schema(
        self,
        *,
        model: str,
        schema_name: str,
        schema: dict[str, Any],
        gen_params: dict[str, Any],
    ) -> dict[str, Any]:
        """仅对官方已列明支持的百炼 Qwen 模型开启 strict schema。"""

        params = dict(gen_params)
        if not (
            _is_dashscope_compatible_endpoint(self._base_url)
            and _QWEN_STRICT_JSON_SCHEMA_MODEL_RE.match(model)
        ):
            return params

        safe_name = re.sub(r"[^A-Za-z0-9_-]+", "_", schema_name).strip("_")
        if not safe_name:
            raise ValueError("strict JSON Schema name 不能为空")
        safe_name = safe_name[:64]
        # 百炼结构化输出文档要求禁用旧 ``max_tokens``，否则回答 JSON 可能被
        # 中途截断；但 Qwen3.7 Max 及之后同时明确支持 ``max_completion_tokens``
        # 限制“思维链 + 回答”的总输出。历史实现把两者一起删掉，导致 profile
        # 原本的 8K 回答 + bounded thinking 预算退化成模型 128K 默认输出上限。
        answer_limit = params.pop("max_tokens", None)
        total_limit = params.get("max_completion_tokens")
        effective_thinking = params.get(
            "enable_thinking", self._default_enable_thinking
        )
        if total_limit is not None and (
            not isinstance(total_limit, int)
            or isinstance(total_limit, bool)
            or total_limit <= 0
        ):
            raise ValueError("strict JSON max_completion_tokens 必须为正整数")
        derived_total_limit: int | None = None
        if total_limit is None:
            if (
                not isinstance(answer_limit, int)
                or isinstance(answer_limit, bool)
                or answer_limit <= 0
            ):
                raise ValueError(
                    "strict JSON 必须配置 max_completion_tokens，或提供可转换的"
                    "正整数 max_tokens"
                )
            if effective_thinking is False:
                derived_total_limit = answer_limit
            else:
                thinking_limit = params.get("thinking_budget")
                if (
                    not isinstance(thinking_limit, int)
                    or isinstance(thinking_limit, bool)
                    or thinking_limit < 0
                ):
                    raise ValueError(
                        "strict JSON 思考模式必须配置 thinking_budget，或直接配置"
                        " max_completion_tokens"
                    )
                derived_total_limit = answer_limit + thinking_limit
            params["max_completion_tokens"] = derived_total_limit
        params["response_format"] = {
            "type": "json_schema",
            "json_schema": {
                "name": safe_name,
                "strict": True,
                "schema": schema,
            },
        }
        _, schema_hash = _strict_json_schema_metadata(params["response_format"])
        _log.info(
            "structured_json_schema_prepared",
            provider=_PROVIDER,
            model=model,
            schema_name=safe_name,
            schema_hash=schema_hash,
            suppressed_generation_limits=(
                ["max_tokens"] if answer_limit is not None else []
            ),
            max_completion_tokens=params.get("max_completion_tokens"),
            derived_max_completion_tokens=derived_total_limit,
            reason=(
                "provider_guidance_disables_max_tokens_but_total_output_stays_bounded"
                if answer_limit is not None
                else None
            ),
        )
        return params

    def close(self) -> None:
        """关闭内部 ``httpx.Client``(仅当本类构造时自创建)。

        外部注入的 ``http_client`` 由 caller 自行管理,不在此关闭。
        """
        if self._owns_http:
            self._http.close()

    def complete(
        self,
        messages: list[Message],
        *,
        model: str | None = None,
        enable_thinking: bool | None = None,
        preserve_thinking: bool | None = None,
        input_token_budget: int | None = None,
        disable_total_timeout: bool = False,
        **gen_params: Any,
    ) -> LLMResponse:
        """同步生成补全，对外统一返回完整 ``LLMResponse``。

        Args:
            messages: 对话消息序列;system / user / assistant 三角色直接透传
                OpenAI 兼容 messages 数组,无需 split。
            model: 单次覆盖模型名;``None`` 时取构造时的 ``self._model``。
            enable_thinking: 单次覆盖 thinking mode;``None`` 时取构造时的
                ``self._default_enable_thinking``。最终值为 ``True`` / ``False``
                时均显式写入 body；只有构造时明确将默认值设为 ``None``，最终值
                才会为 ``None`` 并省略字段。
            preserve_thinking: 单次覆盖是否在后续请求保留历史思维链；``None``
                时取构造默认。最终为布尔值时显式写入 body，最终为 ``None``
                时省略。
            input_token_budget: profile 级本地输入保险丝。使用与官方输入预检相同的
                UTF-8 保守上界，超限时在网络前拒绝；本字段不会进入 HTTP body，
                也不会截断或压缩 prompt。
            disable_total_timeout: 仅上游长思考生成使用的本地 SSE 总墙钟豁免；
                不进入 HTTP body，也不改变等待下一个网络数据的 read timeout。
            **gen_params: 透传到请求 body 顶层的 OpenAI 标准 params(如
                ``temperature`` / ``max_tokens`` / ``top_p`` / ``top_k`` /
                ``stop``)。**注意**:``stream`` / ``stream_options`` /
                ``model`` 由本类内部固定,gen_params 中传入会被覆盖。响应模式由
                构造参数 ``stream_response`` 固定，单次调用不能偷换。

        Returns:
            含正文、实际响应模型、token 使用量、结束原因的 ``LLMResponse``。
            ``content`` 字段**只含正文不含思考过程**;思考过程通过
            ``llm_response`` 事件 ``reasoning_chars`` 字段入 structlog。

        Raises:
            ValueError: api_key / base_url 为空(``__init__`` 时已校验,
                此处不再触发,留文档对称)。
            LLMAuthError: HTTP 401 认证失败，不重试。
            LLMAccessError: HTTP 403 模型/workspace/配额等访问被拒，
                保留安全 provider code/message/request_id，不重试。
            httpx.HTTPStatusError: 其他 4xx 直接抛；5xx 经 3 次指数退避
                重试后仍失败抛最后一次。
            SSEStreamError: SSE 中途断流且 3 次重试后仍失败。
            SSETotalTimeoutError: SSE 总墙钟超限；usage 未知，单次即停止且不重试。
            InvalidChunkJSONError: 服务端返非 OpenAI 兼容响应(不重试)。
            httpx.ConnectError / ReadTimeout / ...: 网络层瞬时故障 3 次重试后
                仍失败抛最后一次。
        """
        requested_model = model if model is not None else self._model
        strict_contract = strict_contract_from_params(gen_params, pop=True)
        use_thinking = (
            enable_thinking
            if enable_thinking is not None
            else self._default_enable_thinking
        )
        use_preserve_thinking = (
            preserve_thinking
            if preserve_thinking is not None
            else self._default_preserve_thinking
        )
        structured_schema_name, structured_schema_hash = _strict_json_schema_metadata(
            gen_params.get("response_format")
        )
        strict_json_schema_enabled = structured_schema_name is not None
        exact_hy3_endpoint = (
            requested_model == TOKENHUB_HY3_MODEL
            and self._base_url.rstrip("/") == TOKENHUB_HY3_BASE_URL
        )
        if (
            exact_hy3_endpoint
            and gen_params.get("response_format") is not None
            and strict_contract is None
        ):
            raise StrictJsonSchemaDriftError(
                "exact TokenHub hy3 structured request 缺少 process-local Pydantic contract"
            )
        if strict_contract is not None:
            assert_hy3_strict_request(
                contract=strict_contract,
                base_url=self._base_url,
                requested_model=requested_model,
                response_format=gen_params.get("response_format"),
            )
        if strict_json_schema_enabled:
            messages = append_strict_json_instruction(messages)

        if input_token_budget is not None and (
            not isinstance(input_token_budget, int)
            or isinstance(input_token_budget, bool)
            or input_token_budget <= 0
        ):
            raise ValueError("input_token_budget 必须为正整数")
        if not isinstance(disable_total_timeout, bool):
            raise ValueError("disable_total_timeout 必须为布尔值")

        input_token_upper_bound: int | None = None
        qwen_limits = _QWEN_MAX_MODEL_LIMITS.get(requested_model)
        if qwen_limits is not None or input_token_budget is not None:
            input_token_upper_bound = _message_token_upper_bound(messages)
        if (
            input_token_budget is not None
            and input_token_upper_bound is not None
            and input_token_upper_bound > input_token_budget
        ):
            raise ValueError(
                "请求输入超过 profile 本地冗余预算:"
                f"upper_bound={input_token_upper_bound},budget={input_token_budget}"
            )

        if qwen_limits is not None:
            assert input_token_upper_bound is not None
            model_label, standard_input_limit, thinking_input_limit, output_limit = (
                qwen_limits
            )
            # Qwen Max 省略 enable_thinking 时 provider 默认开启思考；只有
            # 显式 False 才可使用较宽的 standard 输入上限。
            effective_qwen_thinking = use_thinking is not False
            input_limit = (
                thinking_input_limit
                if effective_qwen_thinking
                else standard_input_limit
            )
            if input_token_upper_bound > input_limit:
                raise ValueError(
                    f"{model_label} 输入超过官方单请求安全上限:"
                    f"upper_bound={input_token_upper_bound},limit={input_limit},"
                    f"thinking={effective_qwen_thinking}"
                )

            max_tokens = gen_params.get("max_tokens")
            max_completion_tokens = gen_params.get("max_completion_tokens")
            for name, value in (
                ("max_tokens", max_tokens),
                ("max_completion_tokens", max_completion_tokens),
            ):
                if (
                    isinstance(value, int)
                    and not isinstance(value, bool)
                    and value > output_limit
                ):
                    raise ValueError(
                        f"{model_label} {name} 超过官方输出上限:{value}>{output_limit}"
                    )

            thinking_budget = gen_params.get("thinking_budget")
            reasoning_effort = gen_params.get("reasoning_effort")
            if thinking_budget is not None and reasoning_effort is not None:
                raise ValueError(
                    f"{model_label} thinking_budget 与 reasoning_effort 不得同时设置"
                )
            if thinking_budget is not None:
                if (
                    not isinstance(thinking_budget, int)
                    or isinstance(thinking_budget, bool)
                    or not 0 <= thinking_budget <= _QWEN_MAX_THINKING_BUDGET
                ):
                    raise ValueError(
                        f"{model_label} thinking_budget 必须为 0.."
                        f"{_QWEN_MAX_THINKING_BUDGET} 的整数"
                    )
                if use_thinking is False and thinking_budget > 0:
                    raise ValueError(
                        f"{model_label} enable_thinking=false 时不得配置正"
                        " thinking_budget"
                    )
            if reasoning_effort is not None and (
                not isinstance(reasoning_effort, str)
                or reasoning_effort not in {"low", "medium", "xhigh"}
            ):
                raise ValueError(
                    f"{model_label} reasoning_effort 只允许 low/medium/xhigh"
                )

        body: dict[str, Any] = {
            "model": requested_model,
            "messages": _format_messages(messages),
            **gen_params,
        }
        # 强制覆盖 caller 误传的响应模式与 model。stream 时 include_usage 保证
        # 最后一个 chunk 带 usage；非 stream 时官方协议要求不带 stream_options。
        body["stream"] = self._stream_response
        if self._stream_response:
            body["stream_options"] = {"include_usage": True}
        else:
            body.pop("stream_options", None)
        body["model"] = requested_model
        # 百炼 Qwen Max 省略 enable_thinking 时会采用 provider 默认值
        # (当前为 thinking on),所以 False 不能被当作“省略”。只有调用方与
        # 构造默认均为 None 时才为兼容未知 provider 省略扩展字段。
        if use_thinking is not None:
            body["enable_thinking"] = use_thinking
        if use_preserve_thinking is not None:
            body["preserve_thinking"] = use_preserve_thinking

        # OBSERVABILITY §2.4 ``llm_request`` 事件 + prompt 原文归档
        # (与 ClaudeClient / OllamaClient 同源,P1-B-24 决策 3 折中方案)。
        agent_role, _ = detect_agent_role(messages)
        prompt_hash = hash_prompt(messages)
        estimated_input_tokens = max(1, sum(len(m.content) for m in messages) // 3)
        _log.info(
            "llm_request",
            provider=_PROVIDER,
            model=requested_model,
            agent=agent_role,
            input_tokens=estimated_input_tokens,
            input_token_upper_bound=input_token_upper_bound,
            input_token_budget=input_token_budget,
            prompt_hash=prompt_hash,
            enable_thinking=use_thinking,
            preserve_thinking=use_preserve_thinking,
            thinking_budget=body.get("thinking_budget"),
            reasoning_effort=body.get("reasoning_effort"),
            max_tokens=body.get("max_tokens"),
            max_completion_tokens=body.get("max_completion_tokens"),
            structured_output_type=(
                "json_schema" if strict_json_schema_enabled else None
            ),
            structured_schema_name=structured_schema_name,
            structured_schema_hash=structured_schema_hash,
            structured_schema_strict=(True if strict_json_schema_enabled else None),
            structured_schema_dialect=(
                strict_contract.dialect if strict_contract is not None else None
            ),
            structured_schema_compiler_version=(
                strict_contract.compiler_version
                if strict_contract is not None
                else None
            ),
            structured_schema_bytes=(
                strict_contract.schema_bytes if strict_contract is not None else None
            ),
            structured_schema_loss_report_hash=(
                strict_contract.loss_report_hash
                if strict_contract is not None
                else None
            ),
        )
        # workspace_id 从 structlog contextvars 读;Orchestrator/ChapterService
        # 入口 ``bind_chapter_context`` 已绑定。单测可能没绑(直接调
        # ``OpenAICompatibleClient.complete``),此时 workspace_id 为 None,
        # 跳过归档。
        ctx = structlog.contextvars.get_contextvars()
        workspace_id = ctx.get("workspace_id")
        if workspace_id is not None:
            archive_prompt(
                workspaces_root=settings.data_dir,
                workspace_id=workspace_id,
                prompt_hash=prompt_hash,
                content=render_prompt_text(messages),
                provider=_PROVIDER,
            )

        url = f"{self._base_url}/chat/completions"
        headers = {
            "Authorization": f"Bearer {self._api_key.get_secret_value()}",
            "Content-Type": "application/json",
            "Accept": (
                "text/event-stream" if self._stream_response else "application/json"
            ),
        }

        start = time.perf_counter()
        try:
            if self._stream_response:
                total_timeout = (
                    None if disable_total_timeout else self._total_timeout_seconds
                )
                collector = partial(
                    self._stream_and_collect,
                    url, headers, body, requested_model,
                    total_timeout_seconds=total_timeout,
                )
            else:
                collector = partial(self._post_and_collect, url, headers, body, requested_model)
            result = _retry(
                collector,
                max_attempts=self._max_attempts,
                outcome_from_result=_openai_result_outcome,
                outcome_from_error=_openai_error_outcome,
            )
        except httpx.HTTPStatusError as exc:
            status_code = exc.response.status_code
            provider_code, provider_message, provider_request_id = (
                _extract_provider_error_fields(exc.response)
            )
            if strict_json_schema_enabled:
                _log.error(
                    "structured_json_schema_provider_rejected",
                    provider=_PROVIDER,
                    model=requested_model,
                    agent=agent_role,
                    schema_name=structured_schema_name,
                    schema_hash=structured_schema_hash,
                    status_code=status_code,
                    provider_code=provider_code,
                    provider_request_id=provider_request_id,
                    dialect=(
                        strict_contract.dialect if strict_contract is not None else None
                    ),
                    compiler_version=(
                        strict_contract.compiler_version
                        if strict_contract is not None
                        else None
                    ),
                    fallback_used=False,
                    model_retry_allowed=False,
                )
            suffix = _provider_error_suffix(
                provider_code=provider_code,
                provider_message=provider_message,
                provider_request_id=provider_request_id,
            )
            # 401 才是认证失败；403 可能是模型/workspace 权限、
            # 服务未开通、账户策略或配额限制，不得伪报成 key 失效。
            if status_code == 401:
                raise LLMAuthError(
                    f"云端 provider '{_PROVIDER}'(base_url={self._base_url}, "
                    f"model={requested_model})认证失败 (HTTP 401):"
                    f"请检查当前 API key 是否无效、失效或不属于该账户。"
                    f"{suffix}",
                    status_code=status_code,
                    provider_code=provider_code,
                    provider_message=provider_message,
                    provider_request_id=provider_request_id,
                ) from exc
            if status_code == 403:
                raise LLMAccessError(
                    f"云端 provider '{_PROVIDER}'(base_url={self._base_url}, "
                    f"model={requested_model})拒绝访问 (HTTP 403):"
                    "请按 provider 错误码检查模型/workspace 权限、"
                    "服务开通状态、账户策略或配额；本状态不等同于 API key 失效。"
                    f"{suffix}",
                    status_code=status_code,
                    provider_code=provider_code,
                    provider_message=provider_message,
                    provider_request_id=provider_request_id,
                ) from exc
            raise
        latency_ms = (time.perf_counter() - start) * 1000.0

        if strict_contract is not None:
            try:
                strict_validation = validate_strict_json_response(
                    content=result.content,
                    response_model=result.model_used,
                    contract=strict_contract,
                )
            except (StrictJsonResponseViolation, StrictJsonSchemaDriftError) as exc:
                failure_layer = (
                    exc.layer
                    if isinstance(exc, StrictJsonResponseViolation)
                    else "response_identity"
                )
                _log.error(
                    "structured_json_schema_raw_validation_failed",
                    provider=_PROVIDER,
                    model=result.model_used,
                    agent=agent_role,
                    schema_name=structured_schema_name,
                    schema_hash=structured_schema_hash,
                    dialect=strict_contract.dialect,
                    compiler_version=strict_contract.compiler_version,
                    finish_reason=result.finish_reason,
                    content_chars=len(result.content),
                    failure_layer=failure_layer,
                    raw_json_parse=(
                        "failed" if failure_layer == "raw_json_parse" else "passed"
                    ),
                    wire_schema=(
                        "failed"
                        if failure_layer == "wire_schema"
                        else (
                            "not_run"
                            if failure_layer in {"raw_json_parse", "response_identity"}
                            else "passed"
                        )
                    ),
                    wire_pydantic=(
                        "failed"
                        if failure_layer == "wire_pydantic"
                        else (
                            "not_run"
                            if failure_layer
                            in {"raw_json_parse", "wire_schema", "response_identity"}
                            else "passed"
                        )
                    ),
                    violation_reason=str(exc)[:500],
                    repair_attempted=False,
                    fallback_used=False,
                    model_retry_allowed=False,
                )
                raise
            _log.info(
                "structured_json_schema_response_received",
                provider=_PROVIDER,
                model=result.model_used,
                agent=agent_role,
                schema_name=structured_schema_name,
                schema_hash=structured_schema_hash,
                dialect=strict_contract.dialect,
                compiler_version=strict_contract.compiler_version,
                finish_reason=result.finish_reason,
                content_chars=len(result.content),
                raw_json_parse=strict_validation.raw_json_parse,
                wire_schema=strict_validation.wire_schema,
                wire_pydantic=strict_validation.wire_pydantic,
                repair_attempted=False,
                fallback_used=False,
                model_retry_allowed=False,
                downstream_business_validation_required=True,
            )
        elif strict_json_schema_enabled:
            try:
                json.loads(result.content)
            except json.JSONDecodeError as exc:
                _log.error(
                    "structured_json_schema_syntax_violation",
                    provider=_PROVIDER,
                    model=result.model_used,
                    agent=agent_role,
                    schema_name=structured_schema_name,
                    schema_hash=structured_schema_hash,
                    finish_reason=result.finish_reason,
                    content_chars=len(result.content),
                    json_error=exc.msg,
                    json_error_line=exc.lineno,
                    json_error_column=exc.colno,
                )
            else:
                _log.info(
                    "structured_json_schema_response_received",
                    provider=_PROVIDER,
                    model=result.model_used,
                    agent=agent_role,
                    schema_name=structured_schema_name,
                    schema_hash=structured_schema_hash,
                    finish_reason=result.finish_reason,
                    content_chars=len(result.content),
                    syntax_valid=True,
                    downstream_pydantic_validation_required=True,
                )

        usage = TokenUsage(
            input_tokens=result.input_tokens,
            output_tokens=result.output_tokens,
        )
        # OBSERVABILITY §2.4 ``llm_response``。``cost_usd=0.0`` 占位 —— 真实
        # 成本由 Router 层(P3-B-07 cost tracker)按
        # ``WriterProfile.cost_per_1k_input/output`` 落到 chapters.total_cost_usd。
        # ``reasoning_chars`` 字段是本 client 独有的(thinking 模式专属) —
        # 让运维能在 grep 日志时区分 thinking vs non-thinking 调用。
        _log.info(
            "llm_response",
            provider=_PROVIDER,
            model=result.model_used,
            agent=agent_role,
            input_tokens=usage.input_tokens,
            output_tokens=usage.output_tokens,
            cost_usd=0.0,
            latency_ms=round(latency_ms, 3),
            finish_reason=result.finish_reason,
            prompt_hash=prompt_hash,
            reasoning_chars=len(result.reasoning),
        )

        response = LLMResponse(
            content=result.content,
            model_used=result.model_used,
            usage=usage,
            finish_reason=result.finish_reason,
            provider_request_id=result.provider_request_id,
        )
        response._reasoning_chars = len(result.reasoning)
        return response

    def _post_and_collect(
        self,
        url: str,
        headers: dict[str, str],
        body: dict[str, Any],
        requested_model: str,
    ) -> _StreamResult:
        """读取非流式 OpenAI Chat completion，并拆分正文、思考与 usage。"""

        response = self._http.post(url, headers=headers, json=body)
        response.raise_for_status()
        try:
            payload = response.json()
        except json.JSONDecodeError as exc:
            raise NonStreamingResponseError("非流式响应不是完整 JSON") from exc
        if not isinstance(payload, dict):
            raise NonStreamingResponseError("非流式响应顶层必须是 JSON object")

        choices = payload.get("choices")
        if not isinstance(choices, list) or not choices:
            raise NonStreamingResponseError("非流式响应缺少 choices[0]")
        choice = choices[0]
        if not isinstance(choice, dict):
            raise NonStreamingResponseError("非流式响应 choices[0] 不是 object")
        message = choice.get("message")
        if not isinstance(message, dict):
            raise NonStreamingResponseError("非流式响应缺少 choices[0].message")

        result = _StreamResult(requested_model)
        result.provider_request_id = _provider_request_id_from_response(
            response,
            payload_id=payload.get("id"),
        )
        content = message.get("content")
        if content is not None and not isinstance(content, str):
            raise NonStreamingResponseError("非流式 response content 类型非法")
        if content:
            result.content_chunks.append(content)
        reasoning = message.get("reasoning_content")
        if reasoning is not None and not isinstance(reasoning, str):
            raise NonStreamingResponseError(
                "非流式 response reasoning_content 类型非法"
            )
        if reasoning:
            result.reasoning_chunks.append(reasoning)

        finish_reason = choice.get("finish_reason")
        if finish_reason is not None:
            result.finish_reason = str(finish_reason)
        model = payload.get("model")
        if isinstance(model, str):
            result.model_used = model

        usage = payload.get("usage")
        if not isinstance(usage, dict):
            raise ProviderUsageMissingError(
                "非流式响应缺少 usage；请求不自动重试",
                provider_request_id=result.provider_request_id,
            )
        prompt_tokens = usage.get("prompt_tokens")
        completion_tokens = usage.get("completion_tokens")
        if not isinstance(prompt_tokens, int) or not isinstance(completion_tokens, int):
            raise ProviderUsageMissingError(
                "非流式 usage token 字段非法；请求不自动重试",
                provider_request_id=result.provider_request_id,
            )
        result.input_tokens = prompt_tokens
        result.output_tokens = completion_tokens
        result.usage_received = True
        return result

    def _stream_and_collect(
        self,
        url: str,
        headers: dict[str, str],
        body: dict[str, Any],
        requested_model: str,
        *,
        total_timeout_seconds: float | None,
    ) -> _StreamResult:
        """打开 SSE 流 + 逐行解析 + 拼接 chunks。

        异常路径:
        - ``response.raise_for_status()`` 把 4xx/5xx 翻成 ``HTTPStatusError``
          交给 ``_retry`` 决定;4xx 不重试 / 5xx 重试 3 次。
        - SSE 流意外 EOF(未见 [DONE]):抛 ``SSEStreamError`` 进重试。
        - 单 chunk JSON parse 失败:抛 ``InvalidChunkJSONError`` 直接给
          caller(服务端返了非标准响应,重试无意义)。
        """
        result = _StreamResult(requested_model)
        saw_done = False
        stream_started_at = time.monotonic()

        with self._http.stream("POST", url, headers=headers, json=body) as response:
            # stream 响应的 body 默认未读；只在错误状态下先读取短错误
            # JSON，供上层提取 code/message/request_id。正常 SSE 仍逐行流式。
            if response.is_error:
                response.read()
            response.raise_for_status()
            result.provider_request_id = _provider_request_id_from_response(response)
            for raw_line in response.iter_lines():
                # J1:每个 SSE chunk 间查取消信号 → 真中断正在跑的流式调用、立即
                # 停止接收 + 省下游 token(``raise_if_cancelled`` 是 O(1) atomic
                # bool 读,无性能问题)。``with self._http.stream`` 退出时 httpx
                # 关闭连接,服务端侧生成也随之中止。
                raise_if_cancelled()
                elapsed = time.monotonic() - stream_started_at
                if total_timeout_seconds is not None and elapsed > total_timeout_seconds:
                    raise SSETotalTimeoutError(
                        "SSE 请求超过本地总墙钟且 Provider 尚未结束:"
                        f" elapsed={elapsed:.3f}s,"
                        f" limit={total_timeout_seconds:.3f}s",
                        provider_request_id=result.provider_request_id,
                    )
                # httpx.iter_lines 已 strip 行尾 \n;空行(SSE chunk 分隔)
                # 直接跳过
                if not raw_line:
                    continue
                # 服务端可能发其他事件类型(``event: ping`` 等),只关心
                # ``data: ...`` 行
                if not raw_line.startswith(_SSE_DATA_PREFIX):
                    continue
                payload = raw_line[len(_SSE_DATA_PREFIX) :].strip()
                if payload == _SSE_DONE_MARKER:
                    saw_done = True
                    break
                try:
                    chunk = json.loads(payload)
                except json.JSONDecodeError as exc:
                    raise InvalidChunkJSONError(
                        f"SSE chunk payload 非合法 JSON: {payload[:200]!r} ({exc})"
                    ) from exc
                self._consume_chunk(chunk, result)

        if not saw_done:
            # 未见 [DONE] 即流结束 —— SSE 协议异常,可能服务端在 chunk 中间
            # 关闭连接 / 代理超时 / etc.;抛进 retry(等同 ReadTimeout)
            raise SSEStreamError(
                "SSE 流未见 [DONE] 终止符即结束 (服务端可能中途断流)",
                provider_request_id=result.provider_request_id,
            )

        if not result.usage_received:
            raise ProviderUsageMissingError(
                "SSE 已见 [DONE] 但没有 usage；请求不自动重试",
                provider_request_id=result.provider_request_id,
            )

        return result

    @staticmethod
    def _consume_chunk(chunk: dict[str, Any], result: _StreamResult) -> None:
        """消费单个 SSE chunk 的 JSON。

        - ``choices[0].delta.content`` → ``content_chunks``(正文)
        - ``choices[0].delta.reasoning_content`` → ``reasoning_chunks``(思考)
        - ``choices[0].finish_reason`` → ``result.finish_reason``(末 chunk)
        - ``model`` → ``result.model_used``(响应模型,可能与请求不同)
        - ``usage.prompt_tokens`` / ``completion_tokens`` → token 计数
          (仅最后一个 ``stream_options.include_usage=true`` chunk 有值)
        """
        if "error" in chunk:
            provider_code, provider_message, provider_request_id = (
                _provider_error_fields_from_payload(chunk)
            )
            request_id = provider_request_id or result.provider_request_id
            suffix = _provider_error_suffix(
                provider_code=provider_code,
                provider_message=provider_message,
                provider_request_id=request_id,
            )
            raise SSEProviderError(
                f"Provider SSE error frame{suffix}",
                provider_code=provider_code,
                provider_message=provider_message,
                provider_request_id=request_id,
            )

        # model 字段(每个 chunk 都返,以最后一次为准 — 通常与请求一致)
        if result.provider_request_id is None:
            chunk_id = chunk.get("id")
            if isinstance(chunk_id, str) and chunk_id.strip():
                result.provider_request_id = chunk_id.strip()[:256]
        if "model" in chunk and isinstance(chunk["model"], str):
            result.model_used = chunk["model"]

        choices = chunk.get("choices") or []
        if choices:
            choice = choices[0]
            delta = choice.get("delta") or {}
            content = delta.get("content")
            if isinstance(content, str) and content:
                result.content_chunks.append(content)
            reasoning = delta.get("reasoning_content")
            if isinstance(reasoning, str) and reasoning:
                result.reasoning_chunks.append(reasoning)
            finish_reason = choice.get("finish_reason")
            if finish_reason is not None:
                result.finish_reason = str(finish_reason)

        # usage 只在最后一个含 ``stream_options.include_usage`` 的 chunk 出现
        usage = chunk.get("usage")
        if isinstance(usage, dict):
            prompt_tokens = usage.get("prompt_tokens")
            completion_tokens = usage.get("completion_tokens")
            if isinstance(prompt_tokens, int):
                result.input_tokens = prompt_tokens
            if isinstance(completion_tokens, int):
                result.output_tokens = completion_tokens
            if isinstance(prompt_tokens, int) and isinstance(completion_tokens, int):
                result.usage_received = True


__all__ = [
    "InvalidChunkJSONError",
    "NonStreamingResponseError",
    "OpenAICompatibleClient",
    "OpenAICompatibleClientError",
    "ProviderUsageMissingError",
    "SSETotalTimeoutError",
    "SSEProviderError",
    "SSEStreamError",
]
