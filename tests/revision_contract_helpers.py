"""正文修订合同测试夹具。

MagicMock 驱动的 API / CLI 测试需要根据当次 prompt 中的动态身份返回严格
LOCALIZED patch，不能再预先塞一段伪造的全文正文。
"""

from __future__ import annotations

import json
from collections.abc import Callable

from src.core.llm.base import LLMResponse, Message, TokenUsage

RevisionResponse = LLMResponse | Callable[[list[Message]], LLMResponse]


def contract_response_sequence(
    *responses: RevisionResponse,
) -> Callable[..., LLMResponse]:
    """按调用顺序返回静态响应，或把当次 messages 交给动态响应工厂。"""

    iterator = iter(responses)

    def _next(*args: object, **kwargs: object) -> LLMResponse:
        del args
        response = next(iterator)
        if not callable(response):
            return response
        messages = kwargs.get("messages")
        if not isinstance(messages, list) or not all(
            isinstance(message, Message) for message in messages
        ):
            raise AssertionError("合同响应夹具未收到 Message 列表")
        return response(messages)

    return _next


def single_target_localized_response(
    messages: list[Message],
    *,
    replacement: str,
    model_used: str = "claude-sonnet-4-6-20250101",
    input_tokens: int = 25,
    output_tokens: int = 22,
) -> LLMResponse:
    """读取 prompt 中的冻结身份并返回合法的单目标 LOCALIZED patch。"""

    user_text = next(message.content for message in messages if message.role == "user")
    try:
        identity_tail = user_text.split("### 冻结身份与允许范围", 1)[1]
        identity_json = identity_tail.split("```json\n", 1)[1].split("\n```", 1)[0]
        identity = json.loads(identity_json)
        (target,) = identity["targets"]
        (anchor_id,) = target["allowed_anchor_ids"]
        operation = target["allowed_operations"][0]
    except (IndexError, KeyError, TypeError, ValueError, json.JSONDecodeError) as exc:
        raise AssertionError("无法从 prompt 解析单目标正文修订合同") from exc

    payload = {
        "contract_version": identity["contract_version"],
        "mode": "localized",
        "source_version_id": identity["source_version_id"],
        "source_sha256": identity["source_sha256"],
        "plan_hash": identity["plan_hash"],
        "patches": [
            {
                "target_ids": [target["target_id"]],
                "anchor_id": anchor_id,
                "operation": operation,
                "replacement": replacement,
            }
        ],
    }
    return LLMResponse(
        content=json.dumps(payload, ensure_ascii=False),
        model_used=model_used,
        usage=TokenUsage(input_tokens=input_tokens, output_tokens=output_tokens),
        finish_reason="end_turn",
    )


def single_target_localized_noop_response(
    messages: list[Message],
    *,
    model_used: str = "mock-noop-reviser",
) -> LLMResponse:
    """读取冻结 source anchor，并故意返回 replacement=原锚文本。"""

    user_text = next(message.content for message in messages if message.role == "user")
    try:
        identity_tail = user_text.split("### 冻结身份与允许范围", 1)[1]
        identity_json = identity_tail.split("```json\n", 1)[1].split(
            "\n```", 1
        )[0]
        identity = json.loads(identity_json)
        (target,) = identity["targets"]
        (anchor_id,) = target["allowed_anchor_ids"]
        source_tail = user_text.split("### 带稳定锚点的源正文", 1)[1]
        source_json = source_tail.split("```json\n", 1)[1].split("\n```", 1)[0]
        source = json.loads(source_json)
        replacement = next(
            anchor["text"]
            for anchor in source
            if anchor["anchor_id"] == anchor_id
        )
    except (IndexError, KeyError, StopIteration, TypeError, ValueError) as exc:
        raise AssertionError("无法从 prompt 解析 no-op LOCALIZED 合同") from exc
    return single_target_localized_response(
        messages,
        replacement=replacement,
        model_used=model_used,
    )
