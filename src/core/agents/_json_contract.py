"""LLM 结构化 JSON 合同的保守语法补救。

正式管线仍以严格 ``json.loads`` 为第一真源。这里只补救一种已在 Qwen
正式现场重复出现、且不改变结构化裁决字段语义的格式错误：字符串值内引用
正文时漏转义的英文双引号。补救后，调用方仍必须继续执行各自的 Pydantic、
编号、输入回指和逐字证据校验；结构损坏、字段缺漏或枚举越界不会在这里放宽。
"""

from __future__ import annotations

import json
from typing import Any

import structlog

_log = structlog.get_logger(__name__)


def _repair_unescaped_inner_quotes(text: str) -> str:
    """保守转义 JSON 字符串值内疑似内容引用的裸英文双引号。

    仅供严格解析失败后的单次补救。字符串内的 ``"`` 若后接 JSON 结构
    分隔符，仍视为真正结尾；否则按内容引号转义。已转义序列原样保留。
    """

    out: list[str] = []
    in_string = False
    i = 0
    while i < len(text):
        char = text[i]
        if not in_string:
            out.append(char)
            if char == '"':
                in_string = True
            i += 1
            continue
        if char == "\\" and i + 1 < len(text):
            out.extend((char, text[i + 1]))
            i += 2
            continue
        if char == '"':
            j = i + 1
            while j < len(text) and text[j] in " \t\r\n":
                j += 1
            if j >= len(text) or text[j] in ",}]:":
                in_string = False
                out.append(char)
            else:
                out.append('\\"')
            i += 1
            continue
        out.append(char)
        i += 1
    return "".join(out)


def loads_with_quote_repair(
    text: str,
    *,
    agent_name: str,
    model_used: str,
) -> Any:
    """严格解析 JSON；仅对值内裸双引号尝试一次保守补救。

    修复成功只代表 JSON 语法恢复。所有业务 schema 和裁决护栏由调用方
    继续执行。修复仍失败时抛出第二次 ``JSONDecodeError``，保持原调用方
    的 retry / technical-stop / fail-open 策略不变。
    """

    try:
        return json.loads(text)
    except json.JSONDecodeError as strict_error:
        _log.warning(
            "structured_json_quote_repair_attempted",
            agent_name=agent_name,
            model_used=model_used,
            repair_scope="unescaped_inner_ascii_quotes_only",
            strict_error=strict_error.msg,
            strict_error_line=strict_error.lineno,
            strict_error_column=strict_error.colno,
        )
        repaired = _repair_unescaped_inner_quotes(text)
        try:
            data = json.loads(repaired)
        except json.JSONDecodeError as repaired_error:
            _log.error(
                "structured_json_quote_repair_failed",
                agent_name=agent_name,
                model_used=model_used,
                repair_scope="unescaped_inner_ascii_quotes_only",
                repaired_error=repaired_error.msg,
                repaired_error_line=repaired_error.lineno,
                repaired_error_column=repaired_error.colno,
            )
            raise repaired_error from strict_error
        _log.warning(
            "structured_json_quote_repair_applied",
            agent_name=agent_name,
            model_used=model_used,
            repair_scope="unescaped_inner_ascii_quotes_only",
            schema_validation_required=True,
        )
        return data


__all__ = ["loads_with_quote_repair"]
