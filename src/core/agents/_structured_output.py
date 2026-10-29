"""结构化 Agent 的严格 JSON Schema 请求组装。

Pydantic 仍是本地权威合同；本模块保留既有 Qwen 窄 Schema，同时把
pre-loss Pydantic schema/model 作为只在进程内存在的 dict metadata 交给
exact Hy3 manifest wrapper。未验证的 client 仍完全保持旧请求。
"""

from __future__ import annotations

from copy import deepcopy
from typing import Any, Callable, cast

from pydantic import BaseModel

from src.core.llm.base import LLMClient
from src.core.llm.strict_json_schema import PreparedStructuredJsonSchema

SchemaTransform = Callable[[dict[str, Any]], None]

_STRUCTURAL_KEYS = {
    "type",
    "properties",
    "required",
    "items",
    "enum",
    "additionalProperties",
    "anyOf",
    "allOf",
    "$ref",
}


def _resolve_ref(ref: str, definitions: dict[str, Any]) -> dict[str, Any]:
    prefix = "#/$defs/"
    if not ref.startswith(prefix):
        raise ValueError(f"不支持的 JSON Schema $ref:{ref}")
    name = ref[len(prefix) :]
    target = definitions.get(name)
    if not isinstance(target, dict):
        raise ValueError(f"JSON Schema $ref 未找到:{ref}")
    return deepcopy(target)


def _normalize_schema_node(
    node: Any,
    *,
    definitions: dict[str, Any],
) -> Any:
    if isinstance(node, list):
        return [_normalize_schema_node(item, definitions=definitions) for item in node]
    if not isinstance(node, dict):
        return node

    if "$ref" in node:
        resolved = _resolve_ref(str(node["$ref"]), definitions)
        overlay = {key: value for key, value in node.items() if key != "$ref"}
        resolved.update(overlay)
        return _normalize_schema_node(resolved, definitions=definitions)

    all_of = node.get("allOf")
    if isinstance(all_of, list) and len(all_of) == 1:
        merged = deepcopy(all_of[0])
        if isinstance(merged, dict):
            merged.update({key: value for key, value in node.items() if key != "allOf"})
            return _normalize_schema_node(merged, definitions=definitions)

    any_of = node.get("anyOf")
    if isinstance(any_of, list) and any_of:
        normalized_branches = [
            _normalize_schema_node(branch, definitions=definitions) for branch in any_of
        ]
        if all(
            isinstance(branch, dict)
            and isinstance(branch.get("type"), str)
            and set(branch).issubset({"type"})
            for branch in normalized_branches
        ):
            return {"type": [branch["type"] for branch in normalized_branches]}
        # T | None 是 Pydantic 可选字段的常见形态。百炼文档明确展示
        # ``type: ["string", "null"]``；同一 JSON Schema 形式也适用于
        # object/array，避免把未列入支持类型的 anyOf 发给 provider。
        non_null = [
            branch
            for branch in normalized_branches
            if not (isinstance(branch, dict) and branch.get("type") == "null")
        ]
        has_null = len(non_null) != len(normalized_branches)
        if has_null and len(non_null) == 1 and isinstance(non_null[0], dict):
            nullable = deepcopy(non_null[0])
            value_type = nullable.get("type")
            if isinstance(value_type, str):
                nullable["type"] = [value_type, "null"]
                enum_values = nullable.get("enum")
                if isinstance(enum_values, list) and None not in enum_values:
                    nullable["enum"] = [*enum_values, None]
                return nullable
        return {"anyOf": normalized_branches}

    normalized: dict[str, Any] = {}
    for key, value in node.items():
        if key not in _STRUCTURAL_KEYS:
            continue
        if key == "properties" and isinstance(value, dict):
            normalized[key] = {
                name: _normalize_schema_node(child, definitions=definitions)
                for name, child in value.items()
            }
        elif key in {"items", "additionalProperties"}:
            normalized[key] = _normalize_schema_node(value, definitions=definitions)
        elif key == "anyOf":
            normalized[key] = [
                _normalize_schema_node(child, definitions=definitions)
                for child in value
            ]
        else:
            normalized[key] = deepcopy(value)

    if normalized.get("type") == "object" and "additionalProperties" not in normalized:
        # Pydantic 明确声明的动态 dict 会自带 true/子 schema，不覆盖；
        # 其他已知字段对象禁止模型临时加键。
        normalized["additionalProperties"] = False
    return normalized


def strict_schema_from_model(output_model: type[BaseModel]) -> dict[str, Any]:
    """把 Pydantic schema 裁成无外部 ``$ref`` 的窄结构合同。"""

    raw = output_model.model_json_schema()
    raw_definitions = raw.get("$defs")
    definitions = (
        cast(dict[str, Any], raw_definitions)
        if isinstance(raw_definitions, dict)
        else {}
    )
    normalized = _normalize_schema_node(raw, definitions=definitions)
    if not isinstance(normalized, dict) or normalized.get("type") != "object":
        raise ValueError(f"{output_model.__name__} 顶层必须是 JSON object")
    normalized.pop("$defs", None)
    return normalized


def prepare_strict_json_output(
    *,
    client: LLMClient,
    model: str,
    output_model: type[BaseModel],
    schema_name: str,
    complete_kwargs: dict[str, Any],
    transform: SchemaTransform | None = None,
) -> dict[str, Any]:
    """按 client 能力附加 strict JSON Schema，否则原样返回。"""

    raw_schema = output_model.model_json_schema()
    schema = strict_schema_from_model(output_model)
    if transform is not None:
        transform(schema)
    prepared_schema = PreparedStructuredJsonSchema(
        schema,
        output_model=output_model,
        raw_schema=raw_schema,
        transform=transform,
    )
    prepared = client.prepare_strict_json_schema(
        model=model,
        schema_name=schema_name,
        schema=prepared_schema,
        gen_params=complete_kwargs,
    )
    # 大量既有单测用 ``MagicMock(spec=LLMClient)`` 注入 client；mock 方法的
    # 默认返回值不是 dict。把这类“未声明 capability”视为不支持，保留旧参数。
    # 真 provider 若覆写本钩子，合同仍要求明确返回 dict。
    if not isinstance(prepared, dict):
        return dict(complete_kwargs)
    return prepared


__all__ = ["prepare_strict_json_output", "strict_schema_from_model"]
