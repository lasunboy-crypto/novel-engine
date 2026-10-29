"""Ollama 原生 API 客户端(P3-B-03 引入)。

继承 ``LLMClient`` 基类,直接 POST ``http://localhost:11434/api/chat``,
顶层 ``think`` 字段默认 ``False``(对齐 P3-plan 决策 6 + docs/local_llm_config.md
"thinking mode 必须关闭"实测结论)。负责:

- 把 provider 无关的 ``Message`` 列表转成 ollama 接受的
  ``[{"role": ..., "content": ...}, ...]`` 数组(ollama 原生支持
  ``role: system``,无需像 Claude SDK 那样把 system pop 出来)。
- 把 ollama 响应包装回 ``LLMResponse``;``input_tokens`` / ``output_tokens``
  取自 ollama 的 ``prompt_eval_count`` / ``eval_count``。
- 顶层透传 ``think: <bool>`` —— ollama 原生扩展字段(OpenAI 兼容路径不支持,
  这是选 ollama 原生而非 OpenAI 兼容 SDK 的关键原因)。
- 对网络 / 5xx / ReadTimeout 三类瞬时错误做有限次指数退避重试;4xx 直接抛。

为何走 ollama 原生而非 OpenAI 兼容(P3-plan 决策 6)
==================================================
Qwen 3.5 35B 是带 thinking mode 的推理模型。``docs/local_llm_config.md``
实测表明:不传 ``think`` 时模型把 token 全部花在思考过程,正文输出为空被
截断。``think`` 是 ollama 原生 API 顶层扩展字段,**不在 ``options`` 内**;
OpenAI 兼容路径(``/v1/chat/completions``)不支持透传此字段。本客户端走
``/api/chat`` 直接拼请求体,稳定性最优。

字段映射约定
============
- ``Message.role`` 直接透传到 ollama messages 数组(三种角色 system /
  user / assistant 全支持)。
- ``Message.content`` 直接透传(ollama 不要求 system 单独参数)。
- ``stream`` 固定 ``False``(同步等待完整 JSON 响应,不做流式聚合)。
- ``options`` 字典 = caller 传入的 ``**gen_params``;``temperature`` /
  ``num_predict`` / ``top_p`` / ``top_k`` 等 ollama options 字段直接
  落入此 dict。
- ``think`` 顶层字段 = ``enable_thinking`` 显式参数 > ``__init__``
  默认(``False``)。

P3 不实现:流式聚合(stream=True chunked decoding)、tool use(ollama 0.1.20+
有原生 tool call 支持但非 P3 范围)、multimodal、并发 batch。
"""

from __future__ import annotations

import time
from collections.abc import Callable
from typing import Any, TypeVar

import httpx
import structlog

from src.config import settings
from src.core.agents._prompt import detect_agent_role
from src.core.llm.attempt_context import (
    ProviderAttemptOutcome,
    ProviderResponseStatus,
    run_observed_provider_attempt,
)
from src.core.llm.base import (
    MAX_PROVIDER_ATTEMPTS_PER_LOGICAL_CALL,
    LLMClient,
    LLMResponse,
    Message,
    TokenUsage,
)
from src.core.llm.cancel import raise_if_cancelled
from src.core.llm.prompt_hash import archive_prompt, hash_prompt, render_prompt_text

_log = structlog.get_logger(__name__)

# 默认 ollama 服务端点(对齐 docs/local_llm_config.md 验证过的请求模板)。
# WriterProfile.base_url 可覆盖(仅 ollama / OpenAI 兼容 provider 用)。
_DEFAULT_BASE_URL = "http://localhost:11434"

# 本地推理 Qwen 3.5 35B 一次完整生成实测 ~3-30s(对齐 docs/local_llm_config.md
# "think:false 模式 140 token 完整产出 200 字小说,3.47 秒")。给 120s 默认
# timeout 容纳长 prompt + 大 num_predict 场景;caller 可在构造时覆盖。
_DEFAULT_TIMEOUT_SECONDS = 120.0

# 重试参数对齐 ClaudeClient(P1-B-08):3 次、1s 起步指数退避。
_MAX_ATTEMPTS = MAX_PROVIDER_ATTEMPTS_PER_LOGICAL_CALL
_INITIAL_DELAY_SECONDS = 1.0

# OBSERVABILITY §2.4 ``llm_request`` / ``llm_response`` 事件 ``provider`` 字段。
_PROVIDER = "ollama"

T = TypeVar("T")


def _is_retryable(exc: BaseException) -> bool:
    """判断异常是否可重试。

    重试范围:
    - ``httpx.ConnectError`` / ``httpx.ConnectTimeout``:本地 ollama 服务
      可能正在启动 / 重启,短暂等待后重试。
    - ``httpx.ReadTimeout`` / ``httpx.RemoteProtocolError``:网络 / 协议
      抖动,通常一次重试即恢复。
    - ``httpx.HTTPStatusError`` 5xx:服务端临时不可用(ollama runner
      crash + 自动 reload 等)。

    4xx(model 不存在 / 参数非法 / etc.)是请求本身问题,重试无意义。
    """
    if isinstance(
        exc,
        (
            httpx.ConnectError,
            httpx.ConnectTimeout,
            httpx.ReadTimeout,
            httpx.RemoteProtocolError,
        ),
    ):
        return True
    if (
        isinstance(exc, httpx.HTTPStatusError)
        and 500 <= exc.response.status_code < 600
    ):
        return True
    return False


def _retry(
    call: Callable[[], T],
    *,
    outcome_from_result: Callable[[T], ProviderAttemptOutcome] | None = None,
    outcome_from_error: Callable[[BaseException], ProviderAttemptOutcome]
    | None = None,
) -> T:
    """对可重试异常按指数退避重试,最多 ``_MAX_ATTEMPTS`` 次。"""
    last_exc: BaseException | None = None
    for attempt in range(_MAX_ATTEMPTS):
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
            if attempt + 1 < _MAX_ATTEMPTS:
                time.sleep(_INITIAL_DELAY_SECONDS * (2**attempt))
    assert last_exc is not None  # 循环结构保证至少触发过一次异常
    raise last_exc


def _ollama_result_outcome(response: dict[str, Any]) -> ProviderAttemptOutcome:
    input_tokens = response.get("prompt_eval_count")
    output_tokens = response.get("eval_count")
    if (
        isinstance(input_tokens, int)
        and not isinstance(input_tokens, bool)
        and input_tokens >= 0
        and isinstance(output_tokens, int)
        and not isinstance(output_tokens, bool)
        and output_tokens >= 0
    ):
        return ProviderAttemptOutcome.known(
            input_tokens=input_tokens,
            output_tokens=output_tokens,
        )
    return ProviderAttemptOutcome.unknown(response_status="completed")


def _ollama_error_outcome(exc: BaseException) -> ProviderAttemptOutcome:
    status: ProviderResponseStatus = (
        "stream_incomplete"
        if isinstance(exc, (httpx.ReadTimeout, httpx.RemoteProtocolError))
        else "failed"
    )
    return ProviderAttemptOutcome.unknown(response_status=status)


def _format_messages(messages: list[Message]) -> list[dict[str, str]]:
    """``Message`` 序列 → ollama 接受的 dict 数组。

    ollama 原生 API ``/api/chat`` 直接接 ``[{"role": ..., "content": ...}]``,
    三种角色 system / user / assistant 全部支持,无需像 anthropic SDK 把
    system 单独提到顶层参数。
    """
    return [{"role": m.role, "content": m.content} for m in messages]


class OllamaClient(LLMClient):
    """Ollama 原生 API 客户端(``POST /api/chat``,顶层 ``think:false`` 默认)。

    Args:
        model: 默认请求模型名,如 ``qwen-novel``(基于 ``qwen3.5:35b`` +
            ``docs/modelfiles/qwen-novel.modelfile``)。``complete`` 时也可
            通过 ``model="..."`` 覆盖单次调用。
        base_url: ollama 服务端点,默认 ``http://localhost:11434``;
            ``WriterProfile.base_url`` 覆盖时透传。
        enable_thinking: 默认 thinking mode 开关。**默认 ``False``**
            (对齐 P3-plan 决策 6 + docs/local_llm_config.md 实测)。
            ``complete`` 时可通过 ``enable_thinking=True`` 单次切换。
        timeout_seconds: HTTP 请求超时,默认 120s(对齐本地推理实测)。
        http_client: 自定义 ``httpx.Client``(测试 mock 注入点)。传入时
            视为外部托管,本类不在 ``close`` 时关闭它。
    """

    def __init__(
        self,
        model: str,
        base_url: str = _DEFAULT_BASE_URL,
        enable_thinking: bool = False,
        timeout_seconds: float = _DEFAULT_TIMEOUT_SECONDS,
        http_client: httpx.Client | None = None,
    ) -> None:
        self._model = model
        self._base_url = base_url.rstrip("/")
        self._default_enable_thinking = enable_thinking
        if http_client is not None:
            self._http = http_client
            self._owns_http = False
        else:
            self._http = httpx.Client(timeout=timeout_seconds)
            self._owns_http = True

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
        **gen_params: Any,
    ) -> LLMResponse:
        """同步生成补全。

        Args:
            messages: 对话消息序列;system / user / assistant 三种角色直接
                透传 ollama messages 数组。
            model: 单次覆盖模型名;``None`` 时取构造时的 ``self._model``。
            enable_thinking: 单次覆盖 thinking mode;``None`` 时取构造时
                的 ``self._default_enable_thinking``(默认 ``False``)。
            **gen_params: 透传到 ollama 请求体的 ``options`` dict。常见键:
                ``temperature`` / ``num_predict`` / ``top_p`` / ``top_k`` /
                ``stop``。注意 ollama 用 ``num_predict`` 而非 OpenAI
                ``max_tokens``。

        Returns:
            含正文、实际响应模型、token 使用量、结束原因的 ``LLMResponse``。
            ``input_tokens`` 取自 ollama ``prompt_eval_count``;
            ``output_tokens`` 取自 ``eval_count``;``finish_reason`` 取自
            ``done_reason``,旧版本回退到 ``"stop"``(``done=True`` 时)。

        Raises:
            httpx.HTTPStatusError: 4xx 直接抛(model 不存在 / 参数非法等);
                5xx 经过 3 次指数退避重试后仍失败,抛最后一次。
            httpx.ConnectError / ConnectTimeout / ReadTimeout / RemoteProtocolError:
                经过 3 次指数退避重试后仍失败,抛最后一次。
            RuntimeError: ollama 响应体非 JSON 对象(理论上不该发生,留兜底)。
        """
        requested_model = model if model is not None else self._model
        use_thinking = (
            enable_thinking
            if enable_thinking is not None
            else self._default_enable_thinking
        )

        body: dict[str, Any] = {
            "model": requested_model,
            "messages": _format_messages(messages),
            "stream": False,
            "think": use_thinking,
        }
        # ``options`` dict 仅在 caller 传入时附加,空 dict 不写入避免污染请求体
        if gen_params:
            body["options"] = dict(gen_params)

        # OBSERVABILITY §2.4 ``llm_request`` 事件 + prompt 原文归档
        # (与 ClaudeClient 同源,P1-B-24 决策 3 折中方案)。
        agent_role, _ = detect_agent_role(messages)
        prompt_hash = hash_prompt(messages)
        estimated_input_tokens = max(1, sum(len(m.content) for m in messages) // 3)
        _log.info(
            "llm_request",
            provider=_PROVIDER,
            model=requested_model,
            agent=agent_role,
            input_tokens=estimated_input_tokens,
            prompt_hash=prompt_hash,
        )
        # workspace_id 从 structlog contextvars 读;Orchestrator/ChapterService
        # 入口 ``bind_chapter_context`` 已绑定。单测可能没绑(直接调
        # ``OllamaClient.complete``),此时 workspace_id 为 None,跳过归档。
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

        url = f"{self._base_url}/api/chat"
        # J1:pre-flight 取消检查。ollama 当前 ``stream=False`` 单次阻塞 httpx.post
        # (无 chunk 间中断点),故只能发起前查取消信号 → 命中串行调用间隙中断。
        # ollama 是 P3 本地非当前热路径(J1 云端目标走 openai_compatible 真 chunk
        # 中断),真 chunk 中断(切 stream=True)推 P3(详 cancel.py + 候选 §J1)。
        raise_if_cancelled()
        start = time.perf_counter()
        response_data = _retry(
            lambda: self._post_chat(url, body),
            outcome_from_result=_ollama_result_outcome,
            outcome_from_error=_ollama_error_outcome,
        )
        latency_ms = (time.perf_counter() - start) * 1000.0

        message = response_data.get("message", {})
        content = message.get("content", "") if isinstance(message, dict) else ""
        model_used = response_data.get("model", requested_model)
        input_tokens = int(response_data.get("prompt_eval_count", 0) or 0)
        output_tokens = int(response_data.get("eval_count", 0) or 0)
        # 新版 ollama 返 ``done_reason`` 字段(``stop`` / ``length`` / ...);
        # 旧版仅 ``done: true``,回退 ``"stop"`` 当兜底。
        finish_reason: str | None
        done_reason = response_data.get("done_reason")
        if done_reason:
            finish_reason = str(done_reason)
        elif response_data.get("done"):
            finish_reason = "stop"
        else:
            finish_reason = None

        usage = TokenUsage(input_tokens=input_tokens, output_tokens=output_tokens)
        # OBSERVABILITY §2.4 ``llm_response``。``cost_usd=0.0`` —— 本地推理
        # 无 API 成本(WriterProfile.cost_per_1k_* 默认 0.0,P3-B-07 cost
        # tracker 据此累加 chapters.total_cost_usd,本地始终 0)。
        _log.info(
            "llm_response",
            provider=_PROVIDER,
            model=model_used,
            agent=agent_role,
            input_tokens=usage.input_tokens,
            output_tokens=usage.output_tokens,
            cost_usd=0.0,
            latency_ms=round(latency_ms, 3),
            finish_reason=finish_reason,
            prompt_hash=prompt_hash,
        )

        return LLMResponse(
            content=content,
            model_used=model_used,
            usage=usage,
            finish_reason=finish_reason,
        )

    def _post_chat(self, url: str, body: dict[str, Any]) -> dict[str, Any]:
        """POST ``/api/chat``,raise_for_status 后返 JSON dict。

        ``raise_for_status`` 把 4xx/5xx 翻成 ``httpx.HTTPStatusError``,
        交给 ``_retry`` 决定是否重试(5xx 重试,4xx 立即抛)。
        """
        resp = self._http.post(url, json=body)
        resp.raise_for_status()
        data = resp.json()
        if not isinstance(data, dict):
            raise RuntimeError(
                f"OllamaClient: 响应体应为 JSON 对象,收到 {type(data).__name__}"
            )
        return data
