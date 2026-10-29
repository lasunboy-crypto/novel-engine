"""Claude API 客户端(P1 唯一 LLM provider)。

继承 `LLMClient` 基类,用 anthropic SDK 实现 `complete`。负责:
- 把 provider 无关的 `Message` 列表转换为 anthropic SDK 的格式
  (system 角色 pop 出来塞 SDK 的 system 参数,user/assistant 走 messages 数组)。
- 把 anthropic SDK 返回的 `Message` 对象包装回 `LLMResponse`。
- 对网络/限流/5xx 三类瞬时错误做有限次指数退避重试;其余异常直接抛。

P1 不实现:流式(stream)、tool use、超时配置、多模态。后续按"加新方法不
改 `complete`"的演进路径走。
"""

from __future__ import annotations

import time
from collections.abc import Callable
from typing import Any, TypeVar

import anthropic
import structlog
from pydantic import SecretStr

from src.config import settings
from src.core.agents._prompt import detect_agent_role
from src.core.llm.attempt_context import (
    ProviderAttemptOutcome,
    run_observed_provider_attempt,
)
from src.core.llm.base import (
    MAX_PROVIDER_ATTEMPTS_PER_LOGICAL_CALL,
    LLMAuthError,
    LLMClient,
    LLMResponse,
    Message,
    TokenUsage,
)
from src.core.llm.cancel import raise_if_cancelled
from src.core.llm.prompt_hash import archive_prompt, hash_prompt, render_prompt_text

_log = structlog.get_logger(__name__)

# Writer 章节正文一般 < 6000 token,这里给个稳妥的默认上限,具体 agent
# 可在调用时通过 `complete(..., max_tokens=8000)` 覆盖。
_DEFAULT_MAX_TOKENS = 4096

# 重试参数:与 NEXT_STEPS 拍板一致(3 次、1s 起步指数退避)。
_MAX_ATTEMPTS = MAX_PROVIDER_ATTEMPTS_PER_LOGICAL_CALL
_INITIAL_DELAY_SECONDS = 1.0

# P1-B-24:provider 标识,llm_request / llm_response 事件 ``provider`` 字段。
_PROVIDER = "claude"

T = TypeVar("T")


def _is_retryable(exc: BaseException) -> bool:
    """判断异常是否可重试。

    重试范围限制在三类瞬时故障:
    - `RateLimitError`(429):限流,退避后通常恢复。
    - `APIConnectionError`:网络层抖动(包括 `APITimeoutError`)。
    - `APIStatusError` 中 5xx:服务端临时不可用。

    `BadRequestError`(400)、`AuthenticationError`(401)等 4xx 是请求本身
    问题,重试无意义,直接抛给调用方。
    """
    if isinstance(exc, (anthropic.RateLimitError, anthropic.APIConnectionError)):
        return True
    if isinstance(exc, anthropic.APIStatusError) and 500 <= exc.status_code < 600:
        return True
    return False


def _retry(
    call: Callable[[], T],
    *,
    outcome_from_result: Callable[[T], ProviderAttemptOutcome] | None = None,
    outcome_from_error: Callable[[BaseException], ProviderAttemptOutcome]
    | None = None,
) -> T:
    """对可重试异常按指数退避重试,最多 `_MAX_ATTEMPTS` 次。

    每次失败睡眠 `1s, 2s, 4s, ...`(只在不是最后一次尝试时睡眠);
    不可重试异常立即抛出,不消耗重试预算。
    """
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


def _claude_request_id(value: object) -> str | None:
    return value.strip()[:256] if isinstance(value, str) and value.strip() else None


def _claude_result_outcome(response: Any) -> ProviderAttemptOutcome:
    return ProviderAttemptOutcome.known(
        input_tokens=int(response.usage.input_tokens),
        output_tokens=int(response.usage.output_tokens),
        provider_request_id=_claude_request_id(
            getattr(response, "_request_id", None)
        ),
    )


def _claude_error_outcome(exc: BaseException) -> ProviderAttemptOutcome:
    return ProviderAttemptOutcome.unknown(
        response_status="failed",
        provider_request_id=_claude_request_id(getattr(exc, "request_id", None)),
    )


def _split_system_and_turns(
    messages: list[Message],
) -> tuple[str, list[dict[str, str]]]:
    """把 provider 无关的 `Message` 列表拆成 anthropic 需要的两部分。

    - `system`:多条 system 消息按顺序用 `\\n\\n` 拼接成一个字符串。
      Anthropic SDK 把 system 作为顶层参数,不放 messages 数组。
    - `turns`:user / assistant 交替的对话,转成 `{"role": ..., "content": ...}`。

    P1 不做严格的 user/assistant 交替校验(SDK 会自己报错),也不合并相邻
    同 role 消息;调用方(Writer/Reviser/Reviewer)按规范构造即可。
    """
    system_parts: list[str] = []
    turns: list[dict[str, str]] = []
    for msg in messages:
        if msg.role == "system":
            system_parts.append(msg.content)
        else:
            turns.append({"role": msg.role, "content": msg.content})
    return "\n\n".join(system_parts), turns


def _extract_text(response: anthropic.types.Message) -> str:
    """从 anthropic 响应中拼出纯文本正文。

    `response.content` 是 `ContentBlock` 列表;P1 只发起非工具调用,正常
    只含 `TextBlock`,这里仍按 type 过滤,把所有 text block 拼起来,容忍
    将来出现非 text block 也不会崩。
    """
    return "".join(
        block.text
        for block in response.content
        if getattr(block, "type", None) == "text"
    )


class ClaudeClient(LLMClient):
    """Anthropic Claude API 客户端。

    支持模型:`claude-sonnet-4-6`(默认/cloud_quality)、`claude-opus-4-7`
    (cloud_premium,finale 等高张力章节用)。模型名由调用方在 `__init__`
    传入,本类不维护 Profile 路由(留给 P3)。

    Args:
        model: 默认请求模型名,如 `claude-sonnet-4-6`。`complete` 时也可
            通过 `model="..."` 覆盖单次调用。
        api_key: Anthropic API key。**默认从 `settings.claude_api_key` 取**;
            显式传入会覆盖配置(便于测试或多账号场景)。

    Raises:
        ValueError: API key 为空字符串时抛出,避免无凭据空跑被 401 卡住。
    """

    def __init__(
        self,
        model: str,
        api_key: SecretStr | None = None,
    ) -> None:
        resolved_key = api_key if api_key is not None else settings.claude_api_key
        if not resolved_key.get_secret_value():
            raise ValueError(
                "ClaudeClient 缺少 API key:请设置 CLAUDE_API_KEY 环境变量,"
                "或在构造时显式传入 api_key 参数。"
            )
        self._model = model
        # anthropic SDK 内部维护 httpx 连接池,客户端实例可复用。
        self._sdk = anthropic.Anthropic(api_key=resolved_key.get_secret_value())

    def complete(
        self,
        messages: list[Message],
        **gen_params: Any,
    ) -> LLMResponse:
        """同步生成补全。

        Args:
            messages: 对话消息序列;system 角色会被 pop 成 anthropic SDK 的
                顶层 `system` 参数,user/assistant 走 messages 数组。
            **gen_params: 透传给 anthropic SDK `messages.create` 的额外参数
                (如 `temperature`、`top_p`、`top_k`、`stop_sequences`、
                `max_tokens`、`model`)。`max_tokens` 缺省值 4096;`model`
                缺省取构造时的 `self._model`。

        Returns:
            含正文、实际响应模型、token 使用量、结束原因的 `LLMResponse`。
            `model_used` 取自 `response.model`(应对 fallback);`finish_reason`
            原样透传 `response.stop_reason`(`end_turn` / `max_tokens` /
            `stop_sequence` / `tool_use` 之一)。

        Raises:
            anthropic.BadRequestError / AuthenticationError / 等 4xx:不重试,
                直接抛给调用方。
            anthropic.RateLimitError / APIConnectionError / 5xx:经过 3 次指数
                退避重试后仍失败,抛最后一次异常。
        """
        system, turns = _split_system_and_turns(messages)

        requested_model = gen_params.pop("model", self._model)
        request_kwargs: dict[str, Any] = {
            "model": requested_model,
            "max_tokens": gen_params.pop("max_tokens", _DEFAULT_MAX_TOKENS),
            "messages": turns,
            **gen_params,
        }
        if system:
            request_kwargs["system"] = system

        # P1-B-24:OBSERVABILITY §2.4 `llm_request` 事件 + prompt 原文归档
        # (决策 3 折中方案)。``input_tokens`` 此时是预估值——粗略按字符 / 3
        # 估算,响应后的 `llm_response` 事件用 SDK 实际 ``usage.input_tokens``。
        agent_role, _ = detect_agent_role(messages)
        prompt_hash = hash_prompt(messages)
        estimated_input_tokens = max(
            1, sum(len(m.content) for m in messages) // 3
        )
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
        # ``ClaudeClient.complete``),此时 workspace_id 为 None,跳过归档。
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

        # J1:pre-flight 取消检查。anthropic SDK 同步 ``messages.create`` 是单次
        # 阻塞调用、P1 故意非流式(无 chunk 间中断点),故只能在发起调用前查取消
        # 信号 → 命中多 agent 流水线"串行调用间隙"中断(最大 token 浪费点)。真
        # chunk 级中断需改写 ``messages.stream()``,因维护人无 Claude key 跑 mock、
        # 转流式零 token 收益 + 推翻 P1 非流式设计风险,推 P3(详 cancel.py 模块
        # docstring + 候选 §J1 留观债)。
        raise_if_cancelled()
        start = time.perf_counter()
        try:
            response = _retry(
                lambda: self._sdk.messages.create(**request_kwargs),
                outcome_from_result=_claude_result_outcome,
                outcome_from_error=_claude_error_outcome,
            )
        except anthropic.AuthenticationError as exc:
            # S1 ②(2026-06-03):云端认证失败 = key 无效/失效 → fail-fast 清晰
            # 报错(非自动降级,维护人主动选择 provider)。
            raise LLMAuthError(
                f"云端 provider '{_PROVIDER}'"
                f"(model={request_kwargs.get('model')})认证失败:"
                f"API key 无效或已失效。请检查 .env 的 CLAUDE_API_KEY,"
                f"或在配置里切换到本地 ollama / 其他云端 provider。"
            ) from exc
        latency_ms = (time.perf_counter() - start) * 1000.0

        usage = TokenUsage(
            input_tokens=response.usage.input_tokens,
            output_tokens=response.usage.output_tokens,
        )
        # P1-B-24:OBSERVABILITY §2.4 `llm_response`。``cost_usd`` 当前打 0.0
        # 占位—— P1 维护人无 Claude API key,真实定价表延后到 P3 真 LLM
        # 联调时落地(届时按 `response.model` 实际值查表算 cost)。
        _log.info(
            "llm_response",
            provider=_PROVIDER,
            model=response.model,
            agent=agent_role,
            input_tokens=usage.input_tokens,
            output_tokens=usage.output_tokens,
            cost_usd=0.0,
            latency_ms=round(latency_ms, 3),
            finish_reason=response.stop_reason,
            prompt_hash=prompt_hash,
        )

        return LLMResponse(
            content=_extract_text(response),
            model_used=response.model,
            usage=usage,
            finish_reason=response.stop_reason,
            provider_request_id=_claude_request_id(
                getattr(response, "_request_id", None)
            ),
        )
