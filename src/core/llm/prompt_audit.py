"""Provider-0 Prompt render 统计。

这里的 token 数只是跨版本可重复的本地启发式，不是 provider tokenizer、
AttemptLedger usage 或账单。生产请求与计费代码不依赖本模块。
"""

from __future__ import annotations

import math
from dataclasses import dataclass

from src.core.llm.base import Message
from src.core.llm.prompt_hash import hash_prompt


def estimate_local_tokens(text: str) -> int:
    """按 CJK 单字 1、其余非空字符每 4 个 1 的固定启发式估算。"""

    cjk_count = 0
    other_count = 0
    for char in text:
        codepoint = ord(char)
        is_cjk = (
            0x3400 <= codepoint <= 0x4DBF
            or 0x4E00 <= codepoint <= 0x9FFF
            or 0xF900 <= codepoint <= 0xFAFF
        )
        if is_cjk:
            cjk_count += 1
        elif not char.isspace():
            other_count += 1
    return cjk_count + math.ceil(other_count / 4)


@dataclass(frozen=True)
class PromptRenderStats:
    """一组最终 messages 的离线 render 指标。"""

    render_chars: int
    local_token_estimate: int
    prompt_hash: str

    @classmethod
    def from_messages(cls, messages: list[Message]) -> "PromptRenderStats":
        return cls(
            render_chars=sum(len(message.content) for message in messages),
            local_token_estimate=sum(
                estimate_local_tokens(message.content) for message in messages
            ),
            prompt_hash=hash_prompt(messages),
        )


__all__ = ["PromptRenderStats", "estimate_local_tokens"]
