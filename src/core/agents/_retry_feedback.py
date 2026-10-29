"""LLM JSON 输出 retry-with-feedback 通用驱动(v1.0.x wave 5 session 2 引入)。

为什么需要这个模块
==================

luo3 工作区实测 ``qwen-novel`` 本地模型在 OutlineBuilder 5 段复杂 JSON
schema 任务上失败率 ~50%(6 次重试 5 次 fail,5 种错误模式 — tension_curve
长度数错 / 元素越界 / invalid_json / description 字段名错位 / phase_id 用中文)。
单一根因是 qwen 对复杂结构化 JSON schema 的指令遵循能力不足;治理思路:

1. **单段自动重试 N 次**(治根 + 最便宜):失败率 ~50% → ~5%(1-0.5^3≈87.5%
   成功率,N=3 默认)。
2. **retry-with-error-feedback**(治根 + 比纯重试聪明):失败时把 pydantic
   ``ValidationError.errors()`` / ``json.JSONDecodeError`` / post_validate
   错信息**转中文反馈**追加进下次 prompt,让 LLM 针对性改自己的输出。
   实测对 qwen 效果显著 — LLM 看到具体错误描述后能针对性纠正。

设计要点
========

1. **provider 无关**:本模块只感知 ``LLMClient`` 协议;调用方注入已绑定
   client/model/kwargs 的 ``llm_call`` closure(屏蔽 router 路径细节)。

2. **agent 无关**:OutlineBuilder(5 段共享)、ChapterCardGenerator
   (s4 复用候选)、ChapterSummaryExtractor 等可调用方共用同一驱动。
   structlog 事件名通过 ``agent_name`` 参数化,各 agent 自带 prefix
   (例 ``outline_builder_retry_attempt``)。

3. **三档错误反馈分流**:invalid JSON / Pydantic ValidationError / 自定义
   post_validate Exception 三类,各自有定制的中文反馈模板;反馈消息追加
   在原始 user message 后(而非替换),让 LLM 同时看到原任务 + 上次错误。

4. **失败终态**:N 次 retry 后仍未成功 → 抛 ``SchemaRetryExhausted``,含
   ``stage`` / ``attempts`` / ``last_response_content`` / ``last_model_used``
   / ``root_exc`` 五件证据,caller 自行 catch 并转模块级 error 类
   (例 ``OutlineBuilderError``)。

5. **post_validate 接入**:接受可选 ``post_validate(result) -> None``
   callback,失败抛任意 ``Exception``;helper 转中文反馈 + 进入下一轮 retry。
   用途:OutlineBuilder ``build_volume_plans`` 的"volumes 长度 == total_volumes"
   后置校验 / ``generate_mini_plan`` 单卷"items 长度 == chapter_count + refs
   完整性"校验等"schema 之外但仍想 retry 的"业务规则。

6. **structlog 事件**:每次 retry 写 ``{agent_name}_retry_attempt`` warning
   事件(attempt / reason / model_used / error 摘要);retry 成功(attempt > 1)
   写 ``{agent_name}_retry_success`` info 事件;全部失败由 caller 写。
"""

from __future__ import annotations

import json
from collections.abc import Callable
from dataclasses import dataclass
from typing import Any, Literal, TypeVar

import structlog
from pydantic import BaseModel, ValidationError

from src.core.agents._json_contract import loads_with_quote_repair
from src.core.llm.base import LLMResponse, Message
from src.core.llm.local_json_contract import LocalJsonResponseViolation

_log = structlog.get_logger(__name__)


_TBaseModel = TypeVar("_TBaseModel", bound=BaseModel)

SchemaRetryFailureReason = Literal[
    "invalid_json",
    "schema_mismatch",
    "post_validate_failed",
]
SchemaRetryStopReason = Literal[
    "attempts_exhausted",
    "no_progress",
    "caller_fail_closed",
]


@dataclass(frozen=True)
class SchemaRetryFailure:
    """单次内容失败的调用方可见上下文；不持久化、不跨 retry 生命周期。"""

    reason: SchemaRetryFailureReason
    error: Exception
    data: Any | None


@dataclass(frozen=True)
class SchemaRetryFailureFingerprint:
    """调用方给出的稳定失败身份与当前内容是否仍可安全重试。"""

    value: str
    retryable: bool = True

    def __post_init__(self) -> None:
        if not self.value.strip():
            raise ValueError("SchemaRetryFailureFingerprint.value 不能为空")


class SchemaRetryExhausted(ValueError):
    """N 次 retry 后仍未通过 JSON 解析 / schema validation / post_validate。

    caller 自行 catch 并转模块级 error 类(例 ``OutlineBuilderError`` /
    ``ChapterCardGeneratorError``),便于上层路由(P5-B-04
    ``outline_service`` / s4 ``retry_references``)走特定降级逻辑。
    """

    def __init__(
        self,
        message: str,
        *,
        stage: str,
        attempts: int,
        last_response_content: str,
        last_model_used: str,
        root_exc: Exception,
        stop_reason: SchemaRetryStopReason = "attempts_exhausted",
        failure_fingerprint: str | None = None,
    ) -> None:
        super().__init__(message)
        self.stage = stage
        self.attempts = attempts
        self.last_response_content = last_response_content
        self.last_model_used = last_model_used
        self.root_exc = root_exc
        self.stop_reason = stop_reason
        self.failure_fingerprint = failure_fingerprint


# ---------------------------------------------------------------------------
# 中文反馈消息模板(本地 qwen-novel 对中文反馈响应明显优于英文 pydantic 字面)
# ---------------------------------------------------------------------------


def format_json_decode_feedback(error: json.JSONDecodeError) -> str:
    """``json.JSONDecodeError`` 转中文反馈消息(供下次 prompt 注入)。"""
    return (
        "## ⚠️ 上次输出问题:JSON 解析失败\n\n"
        f"你上次输出的内容**不是合法的 JSON**(解析错误:{error.msg};"
        f"位置 line {error.lineno} col {error.colno})。\n\n"
        "常见原因:\n"
        "1. 引号 / 括号未闭合\n"
        "2. 多余的尾部逗号(JSON 不允许)\n"
        "3. 输出了 markdown 代码块 ```json ... ``` 包裹(必须裸 JSON)\n"
        "4. 输出前后有解释性文字(不允许任何前后缀)\n\n"
        "**请重新输出 JSON 本体,不要任何前后缀、不要 markdown 包裹、"
        "不要 thinking。**"
    )


_VALIDATION_ERROR_TYPE_TRANSLATIONS: dict[str, str] = {
    "missing": "缺失,必须提供",
    "string_pattern_mismatch": "格式不符(只允许字母/数字/下划线/连字符,不能用中文/空格/标点)",
    "string_too_short": "字符串太短",
    "string_too_long": "字符串太长",
    "literal_error": "取值不在允许枚举内",
    "value_error": "取值不合法",
    "list_type": "类型应为列表",
    "dict_type": "类型应为字典",
    "int_type": "类型应为整数",
    "string_type": "类型应为字符串",
    "too_short": "数组元素过少",
    "too_long": "数组元素过多",
    "greater_than_equal": "数值小于允许最小值",
    "less_than_equal": "数值大于允许最大值",
}


def format_validation_error_feedback(error: ValidationError) -> str:
    """Pydantic ``ValidationError`` 转中文反馈消息。

    把 ``error.errors()`` 每条转为 ``- 字段 `path`:中文翻译 + 原 msg``,
    LLM 看到具体哪个字段哪个错后通常能针对性纠正。
    """
    lines = [
        "## ⚠️ 上次输出问题:JSON 通过解析但 schema 校验失败",
        "",
        "你上次输出的 JSON 在以下字段上不符合 schema 要求:",
        "",
    ]
    for err in error.errors():
        loc = ".".join(str(x) for x in err["loc"]) if err["loc"] else "<root>"
        err_type = err["type"]
        translation = _VALIDATION_ERROR_TYPE_TRANSLATIONS.get(err_type, "")
        if translation:
            lines.append(f"- 字段 `{loc}`:{translation}({err['msg']})")
        else:
            lines.append(f"- 字段 `{loc}`:{err['msg']}({err_type})")
    lines.append("")
    lines.append(
        "**请按上述要求修正后重新输出完整 JSON,只输出 JSON 本体,"
        "不要任何前后缀。**"
    )
    return "\n".join(lines)


def format_post_validate_feedback(error: Exception) -> str:
    """post_validate callback 抛出的业务规则错误转中文反馈消息。

    用于 schema validation 通过但业务规则(如"volumes 长度必须 == total_volumes")
    仍不满足的场景。
    """
    return (
        "## ⚠️ 上次输出问题:JSON schema 通过但业务规则不满足\n\n"
        f"- {error}\n\n"
        "**请按上述要求修正后重新输出完整 JSON。**"
    )


# ---------------------------------------------------------------------------
# 通用 retry-with-feedback 驱动
# ---------------------------------------------------------------------------


def run_with_schema_retry(
    *,
    agent_name: str,
    stage: str,
    system_text: str,
    initial_user_text: str,
    output_model: type[_TBaseModel],
    llm_call: Callable[[list[Message]], LLMResponse],
    strip_format_noise: Callable[[str], str] | None = None,
    normalize_data: Callable[[Any], Any] | None = None,
    normalization_retryable: Callable[[Exception], bool] | None = None,
    normalization_feedback: Callable[[Exception], str] | None = None,
    schema_validation_feedback: Callable[[ValidationError, Any], str] | None = None,
    post_validate: Callable[[_TBaseModel], None] | None = None,
    failure_fingerprint: (
        Callable[[SchemaRetryFailure], SchemaRetryFailureFingerprint | None] | None
    ) = None,
    non_retryable_exceptions: tuple[type[Exception], ...] = (),
    max_attempts: int = 3,
    preserve_candidate_on_retry: bool = False,
) -> tuple[_TBaseModel, int, str]:
    """通用 LLM JSON 输出 retry-with-feedback 驱动。

    内部循环每轮做 4 件事:
    1. 用当前 ``user_text`` 调 ``llm_call(messages)`` 拿 ``LLMResponse``
    2. ``strip_format_noise`` 剥代码块包裹 / 尾部 HTML 注释(可选)
    3. ``json.loads`` → 可选的确定性 ``normalize_data`` →
       ``output_model.model_validate``
    4. 若 ``post_validate`` 给定,跑业务规则校验

    任一步失败 → 把错误转中文反馈消息,**追加**进 user_text(保留原任务 +
    历次错误反馈累加;system_text 不变),进入下一轮 retry。

    全部成功 → 返 ``(result, attempts_used, model_used)``。
    所有 attempt 都失败 → 抛 ``SchemaRetryExhausted`` 含最末次错误证据。

    :param agent_name: 日志事件 prefix(例 ``"outline_builder"`` → 写
        ``outline_builder_retry_attempt`` / ``outline_builder_retry_success``)。
    :param stage: agent 内的 stage 子标识(例 ``"world"`` / ``"plot"`` /
        ``"volume_plans"``);写进日志事件供排障。
    :param system_text: LLM system 消息文本(不变)。
    :param initial_user_text: 首轮 LLM user 消息文本(retry 时累加反馈)。
    :param output_model: Pydantic 模型类,LLM 输出解析目标。
    :param llm_call: 已绑定 ``client`` / ``model`` / ``complete_kwargs`` 的
        closure;接 ``messages`` 列表返 ``LLMResponse``。
    :param strip_format_noise: 可选剥 LLM 输出格式噪声(代码块 / 尾部 HTML
        注释)的函数。
    :param normalize_data: 可选、调用方限定范围的确定性结构补救。它只能搬运
        原响应里已经显式存在的字段/值；返回后仍必须通过完整 Pydantic 与业务
        校验。未命中时应原样返回，禁止猜值或补造内容。
    :param normalization_feedback: 仅给 normalization_retryable 已允许的错误
        生成具体反馈；不修改响应或增加重试次数。未提供时保留原局部引用反馈。
    :param schema_validation_feedback: 可选 schema 反馈 formatter；调用方可在
        通用 Pydantic 错误后追加其上游权威可证的字段明细。它只生成反馈，
        不得修改 ``data``，最终 Pydantic/business validator 仍是准入权威。
    :param post_validate: 可选业务规则校验 callback;抛任意 ``Exception``
        视为 retry 触发条件。
    :param failure_fingerprint: 可选、由调用方根据稳定 rule/evidence/field/source
        生成的失败身份。连续两次相同非空身份视为无进展，在下一次 LLM 调用前
        停止；不同身份继续。返回 ``retryable=False`` 时当前响应后立即 fail
        closed。状态只存在于本函数调用，不落库、不跨 checkpoint。
    :param non_retryable_exceptions: ``post_validate`` 抛出这些异常时立即向上
        传播，不消费外层 schema retry。用于已经在内部耗尽技术重试的
        fail-closed judge，避免把技术故障误当成生成内容错误再次付费调用。
    :param preserve_candidate_on_retry: 显式实验调用方可保留最近一次原始响应，
        以 assistant 消息交回模型局部修复；不累积旧候选，不将候选当成已通过权威。
        使用原始 wire 输出，不能用程序装配结果替代。默认关闭以保持旧调用协议。
    :param max_attempts: 最大尝试次数(含首次,>= 1);默认 3。
    :returns: ``(parsed_result, attempts_used, model_used)`` 三元组。
    :raises SchemaRetryExhausted: N 次 retry 后仍失败。
    :raises ValueError: ``max_attempts < 1``。
    """
    if max_attempts < 1:
        raise ValueError(
            f"run_with_schema_retry: max_attempts must be >= 1, got {max_attempts}"
        )

    current_user_text = initial_user_text
    last_response: LLMResponse | None = None
    last_root_exc: Exception | None = None
    previous_fingerprint: str | None = None
    consecutive_fingerprint_count = 0

    def _stop_if_needed(
        *,
        reason: SchemaRetryFailureReason,
        error: Exception,
        data: Any | None,
        attempt: int,
        response: LLMResponse,
    ) -> None:
        nonlocal previous_fingerprint, consecutive_fingerprint_count
        if failure_fingerprint is None:
            return
        classified = failure_fingerprint(
            SchemaRetryFailure(reason=reason, error=error, data=data)
        )
        if classified is None:
            previous_fingerprint = None
            consecutive_fingerprint_count = 0
            return
        if classified.value == previous_fingerprint:
            consecutive_fingerprint_count += 1
        else:
            previous_fingerprint = classified.value
            consecutive_fingerprint_count = 1
        stop_reason: SchemaRetryStopReason | None = None
        if not classified.retryable:
            stop_reason = "caller_fail_closed"
        elif consecutive_fingerprint_count >= 2:
            stop_reason = "no_progress"
        if stop_reason is None:
            return
        _log.warning(
            f"{agent_name}_retry_stopped",
            stage=stage,
            attempt=attempt,
            max_attempts=max_attempts,
            reason=stop_reason,
            model_used=response.model_used,
            failure_fingerprint=classified.value,
        )
        raise SchemaRetryExhausted(
            f"{agent_name} stage={stage}:内容失败 {stop_reason}，停止继续 retry",
            stage=stage,
            attempts=attempt,
            last_response_content=response.content,
            last_model_used=response.model_used,
            root_exc=error,
            stop_reason=stop_reason,
            failure_fingerprint=classified.value,
        )

    for attempt in range(1, max_attempts + 1):
        messages = [
            Message(role="system", content=system_text),
            Message(role="user", content=current_user_text),
        ]
        if preserve_candidate_on_retry and last_response is not None and last_response.content:
            messages.extend([
                Message(role="assistant", content=last_response.content),
                Message(role="user", content=(
                    "上一条 assistant 是本段最近一版未通过的候选，不是已审核权威。"
                    "基于这份候选修复本轮反馈指出的问题及必要关联处，保留其它内容与标识；"
                    "不要重新设计无关人物或历史，不得改写原任务的只读上游。"
                    "资料包中历次反馈用于追溯，已经解决的问题不必再次修改；本轮最后反馈仍须解决。"
                    "按原输出合同返回修正后的完整 JSON，不输出差异补丁。"
                )),
            ])
        rejected_contract = None
        try:
            response = llm_call(messages)
        except LocalJsonResponseViolation as exc:
            response = exc.response
            rejected_contract = exc.contract
        last_response = response

        text = response.content
        if strip_format_noise is not None:
            text = strip_format_noise(text)
        text = text.strip()

        # 1. JSON 解析
        try:
            data = loads_with_quote_repair(
                text,
                agent_name=agent_name,
                model_used=response.model_used,
            )
        except json.JSONDecodeError as exc:
            last_root_exc = exc
            _log.warning(
                f"{agent_name}_retry_attempt",
                stage=stage,
                attempt=attempt,
                max_attempts=max_attempts,
                reason="invalid_json",
                model_used=response.model_used,
                error=str(exc),
            )
            _stop_if_needed(
                reason="invalid_json",
                error=exc,
                data=None,
                attempt=attempt,
                response=response,
            )
            if attempt < max_attempts:
                feedback = format_json_decode_feedback(exc)
                current_user_text = _append_feedback(current_user_text, feedback)
                continue
            break

        # Local JSON-object failures must reach the same bounded feedback loop.
        # Validate the wire data before any program assembly or normalization;
        # a permissive output model must not hide an invalid enum/extra field.
        if rejected_contract is not None:
            try:
                rejected_contract.validate_data(data)
            except ValueError as exc:
                last_root_exc = exc
                _log.warning(
                    f"{agent_name}_retry_attempt", stage=stage, attempt=attempt,
                    max_attempts=max_attempts, reason="schema_mismatch",
                    model_used=response.model_used, error=str(exc),
                )
                _stop_if_needed(reason="schema_mismatch", error=exc, data=data,
                                attempt=attempt, response=response)
                if attempt < max_attempts:
                    current_user_text = _append_feedback(
                        current_user_text, f"JSON 输出不满足本次字段合同：{exc}。请按原结构纠正。"
                    )
                    continue
                break

        if normalize_data is not None:
            try:
                data = normalize_data(data)
            except Exception as exc:
                if normalization_retryable is None or not normalization_retryable(exc):
                    raise
                last_root_exc = exc
                _log.warning(
                    f"{agent_name}_retry_attempt", stage=stage, attempt=attempt,
                    max_attempts=max_attempts, reason="post_validate_failed",
                    model_used=response.model_used, error=str(exc),
                )
                _stop_if_needed(reason="post_validate_failed", error=exc, data=data,
                                attempt=attempt, response=response)
                if attempt < max_attempts:
                    current_user_text = _append_feedback(
                        current_user_text,
                        normalization_feedback(exc) if normalization_feedback else (
                            f"本次局部引用无法装配：{exc}。程序没有修改你的内容。"
                            "请修正引用分配，遵守每个位置的已有合同；不要删除既有必达或虚构新权威。"
                        ),
                    )
                    continue
                break

        # 2. Schema 校验
        try:
            result = output_model.model_validate(data)
        except ValidationError as exc:
            last_root_exc = exc
            _log.warning(
                f"{agent_name}_retry_attempt",
                stage=stage,
                attempt=attempt,
                max_attempts=max_attempts,
                reason="schema_mismatch",
                model_used=response.model_used,
                validation_errors=exc.errors(),
            )
            _stop_if_needed(
                reason="schema_mismatch",
                error=exc,
                data=data,
                attempt=attempt,
                response=response,
            )
            if attempt < max_attempts:
                feedback = (
                    schema_validation_feedback(exc, data)
                    if schema_validation_feedback is not None
                    else format_validation_error_feedback(exc)
                )
                current_user_text = _append_feedback(current_user_text, feedback)
                continue
            break

        # 3. 业务规则校验
        if post_validate is not None:
            try:
                post_validate(result)
            except Exception as exc:  # noqa: BLE001 — 业务规则错误任意类
                if isinstance(exc, non_retryable_exceptions):
                    raise
                last_root_exc = exc
                _log.warning(
                    f"{agent_name}_retry_attempt",
                    stage=stage,
                    attempt=attempt,
                    max_attempts=max_attempts,
                    reason="post_validate_failed",
                    model_used=response.model_used,
                    error=str(exc),
                )
                _stop_if_needed(
                    reason="post_validate_failed",
                    error=exc,
                    data=result,
                    attempt=attempt,
                    response=response,
                )
                if attempt < max_attempts:
                    feedback = format_post_validate_feedback(exc)
                    current_user_text = _append_feedback(current_user_text, feedback)
                    continue
                break

        # 成功
        if attempt > 1:
            _log.info(
                f"{agent_name}_retry_success",
                stage=stage,
                attempt=attempt,
                model_used=response.model_used,
            )
        return result, attempt, response.model_used

    # 全部 attempt 用尽
    assert last_root_exc is not None
    assert last_response is not None
    raise SchemaRetryExhausted(
        f"{agent_name} stage={stage}:{max_attempts} 次 retry 后仍未通过校验",
        stage=stage,
        attempts=max_attempts,
        last_response_content=last_response.content,
        last_model_used=last_response.model_used,
        root_exc=last_root_exc,
    )


def _append_feedback(user_text: str, feedback: str) -> str:
    """把反馈消息追加在 user_text 末尾。

    用 ``\\n\\n---\\n\\n`` 分隔,让 LLM 视觉上能清楚区分"原任务"和"上次错误反馈"。
    多轮 retry 时反馈累加(不替换),让 LLM 同时看到所有历史错误以避免重蹈
    覆辙。
    """
    from src.core.agents.structured_handoff import append_packet_feedback
    structured = append_packet_feedback(user_text, feedback)
    if structured is not None:
        return structured
    return f"{user_text}\n\n---\n\n{feedback}"
