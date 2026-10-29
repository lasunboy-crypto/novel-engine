"""实验增量的本地 JSON 合同，与 Provider 的原生结构输出能力分开。

复用现有无引用 schema 编译/动态约束和实例校验算法，保持程序组装优势。
schema 作为 JSON-object 请求的明文结构说明发送；绝不声称 DeepSeek 原生
支持 TokenHub strict dialect，也不向 Provider 传递进程内的 Pydantic 类型。
"""

from __future__ import annotations

import json
import re
from copy import deepcopy
from dataclasses import dataclass
from typing import Any

from pydantic import BaseModel

from src.core.llm.base import LLMResponse
from src.core.llm.strict_json_schema import (
    _validate_wire_node,
    canonical_schema_hash,
    compile_prepared_hy3_schema,
)

LOCAL_JSON_CONTRACT_PARAM = "_novel_engine_local_json_contract"


def unwrap_json_code_fence(content: str) -> str:
    """Remove only one whole-response Markdown wrapper, never repair JSON."""
    match = re.fullmatch(r"\s*```(?:json)?\s*\n(?P<body>.*?)\n```\s*", content, re.DOTALL)
    return match.group("body") if match is not None else content


@dataclass(frozen=True)
class LocalJsonContract:
    """只在进程内保存；模型身份仍由 manifest 负责。"""

    name: str
    schema: dict[str, Any]
    schema_hash: str
    output_model: type[BaseModel]

    @classmethod
    def build(cls, name: str, schema: dict[str, Any]) -> LocalJsonContract:
        compiled, model = compile_prepared_hy3_schema(schema)
        if model is None:
            raise ValueError("实验 JSON-object 必须绑定本地 Pydantic 合同")
        return cls(name, deepcopy(compiled.schema), compiled.canonical_hash, model)

    def assert_unchanged(self) -> None:
        if canonical_schema_hash(self.schema) != self.schema_hash:
            raise ValueError("本地 JSON schema 在请求准备后漂移")

    def instruction(self) -> str:
        self.assert_unchanged()
        return (
            f"## 本次 JSON 输出结构：{self.name}\n\n"
            "只输出满足以下结构的 JSON object。身份枚举、数组边界与字段约束"
            "由本地程序逐项验证；创作职责仍以任务为准。\n"
            + json.dumps(self.schema, ensure_ascii=False, sort_keys=True, separators=(",", ":"))
        )

    def validate(self, content: str) -> None:
        self.assert_unchanged()
        self.validate_data(json.loads(content))

    def validate_data(self, data: Any) -> None:
        self.assert_unchanged()
        _validate_wire_node(data, self.schema, path="$")
        self.output_model.model_validate(data)


def local_contract_from_params(params: dict[str, Any]) -> LocalJsonContract | None:
    value = params.get(LOCAL_JSON_CONTRACT_PARAM)
    if value is not None and not isinstance(value, LocalJsonContract):
        raise ValueError("本地 JSON response contract 类型非法")
    return value


class LocalJsonResponseViolation(ValueError):
    """A returned response failed the local contract, not the transport.

    Keep the response so the existing bounded schema-feedback loop can consume
    it and validate the normalized wire object before deterministic assembly.
    """

    def __init__(self, response: LLMResponse, contract: LocalJsonContract, cause: ValueError):
        super().__init__(f"Local JSON contract {contract.name}: {cause}")
        self.response = response
        self.contract = contract
        self.cause = cause


def archive_manifest_response(messages: list, response: LLMResponse) -> None:
    """Archive v6 final content before parsing; no headers, keys or reasoning text."""
    import os
    import re
    from datetime import UTC, datetime
    from uuid import uuid4

    import structlog

    from src.config import settings
    from src.core.llm.prompt_hash import hash_prompt

    workspace_id = structlog.contextvars.get_contextvars().get("workspace_id")
    if workspace_id is None:
        return
    if not isinstance(workspace_id, str) or not re.fullmatch(r"[a-zA-Z0-9_-]{1,128}", workspace_id):
        raise ValueError("Unsafe response archive workspace identity")
    prompt_hash = hash_prompt(messages)
    directory = settings.data_dir / workspace_id / "archives" / "responses" / prompt_hash
    directory.mkdir(parents=True, exist_ok=True)
    payload = response.model_dump(mode="json")
    payload.update(prompt_hash=prompt_hash, captured_at=datetime.now(UTC).isoformat(),
                   reasoning_chars=response.reasoning_chars)
    destination = directory / f"{uuid4().hex}.json"
    fd = os.open(destination, os.O_WRONLY | os.O_CREAT | os.O_EXCL, 0o600)
    with os.fdopen(fd, "w") as stream:
        json.dump(payload, stream, ensure_ascii=False, indent=2)
        stream.write("\n")
        stream.flush()
        os.fsync(stream.fileno())
