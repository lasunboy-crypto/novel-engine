"""Exact TokenHub Hy3 Chat JSON Schema compilation and raw validation.

This module is deliberately internal to the LLM transport boundary.  It does
not replace product Pydantic models or business validators.  Instead it keeps
the exact Provider wire schema, its canonical hash, the local Pydantic model,
and an auditable dialect-loss report together until the raw response has been
validated before any JSON/quote repair can run.
"""

from __future__ import annotations

import hashlib
import json
import re
from copy import deepcopy
from dataclasses import dataclass
from decimal import Decimal, InvalidOperation
from typing import Any, Callable, Literal
from urllib.parse import urlsplit

from pydantic import BaseModel, ValidationError

from src.core.llm.base import Message

TOKENHUB_HY3_PROVIDER_AUTHORITY = "tencent_tokenhub"
TOKENHUB_HY3_BASE_URL = "https://tokenhub.tencentmaas.com/v1"
TOKENHUB_HY3_HOST = "tokenhub.tencentmaas.com"
TOKENHUB_HY3_MODEL = "hy3"
TOKENHUB_HY3_TRANSPORT = "openai_chat_completions"
TOKENHUB_HY3_SCHEMA_DIALECT = "tokenhub_hy3_chat_json_schema_v1"
TOKENHUB_HY3_SCHEMA_COMPILER_VERSION = "hy3-chat-json-schema-compiler-v2"

# This key is process-local metadata.  ``OpenAICompatibleClient`` always pops
# it before building the HTTP body, and the manifest wrapper independently
# verifies that the public response_format still matches the frozen contract.
STRICT_JSON_RESPONSE_CONTRACT_PARAM = "_novel_engine_strict_json_contract"

STRICT_JSON_SYSTEM_INSTRUCTION = """

## 结构化输出语法约束（由系统附加）

只输出符合本次 JSON Schema 的 JSON 本体，不要 Markdown 代码块、
解释或前后缀。JSON 键和结构分隔符仍使用标准英文双引号；
文本字符串值内禁止使用未转义的英文双引号 U+0022。需要
引用正文或对话时，改用中文引号“”、「」或改写；不得输出
会提前闭合字符串的裸英文双引号。
""".strip()

SchemaTransform = Callable[[dict[str, Any]], None]
SchemaIssueAction = Literal["dropped", "rewritten", "unsupported"]
StrictValidationLayer = Literal[
    "raw_json_parse",
    "wire_schema",
    "wire_pydantic",
]

_ANNOTATION_KEYWORDS = frozenset(
    {
        "$comment",
        "$id",
        "$schema",
        "default",
        "deprecated",
        "description",
        "examples",
        "readOnly",
        "title",
        "writeOnly",
    }
)
# These keywords can be preserved losslessly by the local schema normalizer.
# Provider support is a narrower, independently audited boundary: in
# particular, REAL-07 proved that TokenHub Hy3 rejects ``uniqueItems`` at its
# downstream grammar compiler even though it is ordinary JSON Schema syntax.
_SCALAR_VALIDATION_KEYWORDS = frozenset(
    {
        "const",
        "enum",
        "exclusiveMaximum",
        "exclusiveMinimum",
        "maxItems",
        "maxLength",
        "maxProperties",
        "maximum",
        "minItems",
        "minLength",
        "minProperties",
        "minimum",
        "multipleOf",
        "pattern",
        "type",
        "uniqueItems",
    }
)
_TOKENHUB_HY3_UNIMPLEMENTED_WIRE_KEYWORDS = {
    "uniqueItems": (
        "TokenHub Hy3 downstream grammar does not implement uniqueItems; "
        "the keyword must be absent from the production wire schema rather "
        "than silently dropped"
    )
}
_RECURSIVE_SINGLE_KEYWORDS = frozenset(
    {
        "additionalProperties",
        "items",
        "not",
        "propertyNames",
    }
)
_RECURSIVE_LIST_KEYWORDS = frozenset(
    {
        "allOf",
        "anyOf",
        "oneOf",
        "prefixItems",
    }
)


@dataclass(frozen=True, slots=True)
class Hy3SchemaDialectIssue:
    """One auditable schema rewrite, safe loss, or unsupported construct."""

    path: str
    keyword: str
    action: SchemaIssueAction
    reason: str
    weakens_shape: bool

    def as_log_dict(self) -> dict[str, str | bool]:
        return {
            "path": self.path,
            "keyword": self.keyword,
            "action": self.action,
            "reason": self.reason,
            "weakens_shape": self.weakens_shape,
        }


class Hy3SchemaUnsupportedError(ValueError):
    """The schema cannot be represented without weakening required/shape."""

    def __init__(self, issues: tuple[Hy3SchemaDialectIssue, ...]) -> None:
        self.issues = issues
        details = "; ".join(
            f"{issue.path}:{issue.keyword}:{issue.reason}" for issue in issues
        )
        super().__init__(f"Hy3 Chat json_schema dialect unsupported: {details}")


@dataclass(frozen=True, slots=True)
class Hy3SchemaCompilation:
    """Compiled Provider schema plus stable audit metadata."""

    schema: dict[str, Any]
    canonical_hash: str
    canonical_bytes: int
    issues: tuple[Hy3SchemaDialectIssue, ...]
    compiler_version: str = TOKENHUB_HY3_SCHEMA_COMPILER_VERSION

    @property
    def loss_report_hash(self) -> str:
        encoded = json.dumps(
            [issue.as_log_dict() for issue in self.issues],
            ensure_ascii=False,
            sort_keys=True,
            separators=(",", ":"),
        ).encode("utf-8")
        return hashlib.sha256(encoded).hexdigest()


class PreparedStructuredJsonSchema(dict[str, Any]):
    """A wire-identical dict carrying its local Pydantic source in memory.

    JSON encoders see an ordinary mapping, so existing Qwen request bodies stay
    byte-for-byte equivalent.  The exact Hy3 manifest wrapper can additionally
    recover the pre-loss Pydantic schema and model without changing the public
    ``LLMClient.prepare_strict_json_schema`` signature.
    """

    def __init__(
        self,
        schema: dict[str, Any],
        *,
        output_model: type[BaseModel],
        raw_schema: dict[str, Any],
        transform: SchemaTransform | None,
    ) -> None:
        super().__init__(deepcopy(schema))
        self.output_model = output_model
        self.raw_schema = deepcopy(raw_schema)
        self.transform = transform


@dataclass(frozen=True, slots=True)
class StrictJsonResponseContract:
    """Exact process-local contract for one Hy3 strict response."""

    schema_name: str
    schema: dict[str, Any]
    schema_hash: str
    schema_bytes: int
    loss_report: tuple[Hy3SchemaDialectIssue, ...]
    loss_report_hash: str
    output_model: type[BaseModel] | None
    provider_authority: str = TOKENHUB_HY3_PROVIDER_AUTHORITY
    base_url: str = TOKENHUB_HY3_BASE_URL
    host: str = TOKENHUB_HY3_HOST
    exact_model_id: str = TOKENHUB_HY3_MODEL
    transport: str = TOKENHUB_HY3_TRANSPORT
    dialect: str = TOKENHUB_HY3_SCHEMA_DIALECT
    compiler_version: str = TOKENHUB_HY3_SCHEMA_COMPILER_VERSION


class StrictJsonSchemaDriftError(RuntimeError):
    """Exact host/model/schema/hash/protocol changed before Provider dispatch."""


@dataclass(frozen=True, slots=True)
class JsonStringC0Change:
    """One raw C0 code point normalized inside a JSON string."""

    char_index: int
    utf8_byte_offset: int
    codepoint: str
    replacement: str
    completed_existing_escape: bool

    def as_log_dict(self) -> dict[str, int | str | bool]:
        return {
            "char_index": self.char_index,
            "utf8_byte_offset": self.utf8_byte_offset,
            "codepoint": self.codepoint,
            "replacement": self.replacement,
            "completed_existing_escape": self.completed_existing_escape,
        }


JsonStringC0NormalizationStatus = Literal[
    "unchanged",
    "normalized",
    "rejected_c0_outside_string",
]


@dataclass(frozen=True, slots=True)
class JsonStringC0NormalizationProvenance:
    """Hash- and position-bound evidence for the lexical normalization pass."""

    status: JsonStringC0NormalizationStatus
    raw_sha256: str
    normalized_sha256: str | None
    changed_count: int
    changes: tuple[JsonStringC0Change, ...]
    rejected_char_index: int | None = None
    rejected_utf8_byte_offset: int | None = None
    rejected_codepoint: str | None = None

    def as_log_dict(self) -> dict[str, Any]:
        return {
            "status": self.status,
            "raw_sha256": self.raw_sha256,
            "normalized_sha256": self.normalized_sha256,
            "changed_count": self.changed_count,
            "changes": [change.as_log_dict() for change in self.changes],
            "rejected_char_index": self.rejected_char_index,
            "rejected_utf8_byte_offset": self.rejected_utf8_byte_offset,
            "rejected_codepoint": self.rejected_codepoint,
        }


@dataclass(frozen=True, slots=True)
class JsonStringC0NormalizationResult:
    """Normalized JSON text plus immutable lexical provenance."""

    normalized_content: str
    provenance: JsonStringC0NormalizationProvenance


class JsonStringC0OutsideStringError(ValueError):
    """A raw C0 code point appeared outside a JSON string."""

    def __init__(self, provenance: JsonStringC0NormalizationProvenance) -> None:
        self.provenance = provenance
        super().__init__(
            f"raw {provenance.rejected_codepoint} outside JSON string at "
            f"char_index={provenance.rejected_char_index},"
            f"utf8_byte_offset={provenance.rejected_utf8_byte_offset}"
        )


def normalize_json_string_c0_controls(
    content: str,
) -> JsonStringC0NormalizationResult:
    """Escape only raw C0 characters occurring inside JSON strings.

    The scanner deliberately does not parse, extract, complete, coerce, or
    otherwise repair JSON. Existing escape sequences are preserved byte for
    byte. Any raw C0 character outside a string is an immediate fail-closed
    violation, including whitespace that ``json.loads`` might otherwise allow.
    """

    raw_sha256 = hashlib.sha256(content.encode("utf-8")).hexdigest()
    normalized_parts: list[str] = []
    changes: list[JsonStringC0Change] = []
    in_string = False
    escaped = False
    utf8_byte_offset = 0

    for char_index, character in enumerate(content):
        codepoint_value = ord(character)
        if not in_string:
            if codepoint_value <= 0x1F:
                provenance = JsonStringC0NormalizationProvenance(
                    status="rejected_c0_outside_string",
                    raw_sha256=raw_sha256,
                    normalized_sha256=None,
                    changed_count=len(changes),
                    changes=tuple(changes),
                    rejected_char_index=char_index,
                    rejected_utf8_byte_offset=utf8_byte_offset,
                    rejected_codepoint=f"U+{codepoint_value:04X}",
                )
                raise JsonStringC0OutsideStringError(provenance)
            normalized_parts.append(character)
            if character == '"':
                in_string = True
                escaped = False
        elif codepoint_value <= 0x1F:
            replacement = f"\\u{codepoint_value:04X}"
            if escaped:
                # The preceding backslash is already in ``normalized_parts``;
                # complete it as a standard Unicode escape instead of adding
                # a second backslash and changing the decoded value.
                normalized_parts.append(replacement[1:])
            else:
                normalized_parts.append(replacement)
            changes.append(
                JsonStringC0Change(
                    char_index=char_index,
                    utf8_byte_offset=utf8_byte_offset,
                    codepoint=f"U+{codepoint_value:04X}",
                    replacement=replacement,
                    completed_existing_escape=escaped,
                )
            )
            escaped = False
        else:
            normalized_parts.append(character)
            if escaped:
                escaped = False
            elif character == "\\":
                escaped = True
            elif character == '"':
                in_string = False

        utf8_byte_offset += len(character.encode("utf-8"))

    normalized_content = "".join(normalized_parts)
    normalized_sha256 = hashlib.sha256(normalized_content.encode("utf-8")).hexdigest()
    provenance = JsonStringC0NormalizationProvenance(
        status="normalized" if changes else "unchanged",
        raw_sha256=raw_sha256,
        normalized_sha256=normalized_sha256,
        changed_count=len(changes),
        changes=tuple(changes),
    )
    return JsonStringC0NormalizationResult(
        normalized_content=normalized_content,
        provenance=provenance,
    )


class StrictJsonResponseViolation(RuntimeError):
    """A strict raw response failed before any repair or model retry."""

    def __init__(
        self,
        *,
        layer: StrictValidationLayer,
        schema_name: str,
        schema_hash: str,
        reason: str,
        normalizer_provenance: JsonStringC0NormalizationProvenance | None = None,
    ) -> None:
        self.layer = layer
        self.schema_name = schema_name
        self.schema_hash = schema_hash
        self.reason = reason
        self.normalizer_provenance = normalizer_provenance
        super().__init__(
            "strict JSON response violation: "
            f"layer={layer},schema={schema_name},hash={schema_hash},reason={reason}"
        )


@dataclass(frozen=True, slots=True)
class StrictJsonValidationResult:
    """Layered success result; business validation remains downstream."""

    normalized_content: str
    normalizer_provenance: JsonStringC0NormalizationProvenance
    raw_json_parse: Literal["passed"] = "passed"
    wire_schema: Literal["passed"] = "passed"
    wire_pydantic: Literal["passed", "not_attached"] = "not_attached"


class _WireSchemaError(ValueError):
    def __init__(self, *, path: str, keyword: str, reason: str) -> None:
        self.path = path
        self.keyword = keyword
        self.reason = reason
        super().__init__(f"{path}:{keyword}:{reason}")


def canonical_schema_bytes(schema: dict[str, Any]) -> bytes:
    return json.dumps(
        schema,
        ensure_ascii=False,
        sort_keys=True,
        separators=(",", ":"),
    ).encode("utf-8")


def canonical_schema_hash(schema: dict[str, Any]) -> str:
    return hashlib.sha256(canonical_schema_bytes(schema)).hexdigest()


def sanitize_schema_name(schema_name: str) -> str:
    safe_name = re.sub(r"[^A-Za-z0-9_-]+", "_", schema_name).strip("_")
    if not safe_name:
        raise ValueError("strict JSON Schema name 不能为空")
    return safe_name[:64]


def append_strict_json_instruction(messages: list[Message]) -> list[Message]:
    """Append the shared strict syntax instruction exactly once."""

    prepared = [message.model_copy() for message in messages]
    if any(STRICT_JSON_SYSTEM_INSTRUCTION in message.content for message in prepared):
        return prepared
    for index, message in enumerate(prepared):
        if message.role == "system":
            prepared[index] = message.model_copy(
                update={
                    "content": (
                        f"{message.content.rstrip()}\n\n"
                        f"{STRICT_JSON_SYSTEM_INSTRUCTION}"
                    )
                }
            )
            return prepared
    return [Message(role="system", content=STRICT_JSON_SYSTEM_INSTRUCTION), *prepared]


def _issue(
    issues: list[Hy3SchemaDialectIssue],
    *,
    path: str,
    keyword: str,
    action: SchemaIssueAction,
    reason: str,
    weakens_shape: bool,
) -> None:
    issues.append(
        Hy3SchemaDialectIssue(
            path=path,
            keyword=keyword,
            action=action,
            reason=reason,
            weakens_shape=weakens_shape,
        )
    )


def _resolve_local_ref(
    ref: object,
    definitions: dict[str, Any],
    *,
    path: str,
    issues: list[Hy3SchemaDialectIssue],
    ref_stack: tuple[str, ...],
) -> dict[str, Any]:
    if not isinstance(ref, str) or not ref.startswith("#/$defs/"):
        _issue(
            issues,
            path=path,
            keyword="$ref",
            action="unsupported",
            reason="只支持本地 #/$defs/<name> 引用",
            weakens_shape=True,
        )
        return {}
    name = ref.removeprefix("#/$defs/")
    target = definitions.get(name)
    if not isinstance(target, dict):
        _issue(
            issues,
            path=path,
            keyword="$ref",
            action="unsupported",
            reason=f"引用目标不存在:{name}",
            weakens_shape=True,
        )
        return {}
    if name in ref_stack:
        _issue(
            issues,
            path=path,
            keyword="$ref",
            action="unsupported",
            reason=f"递归引用不可内联:{' -> '.join((*ref_stack, name))}",
            weakens_shape=True,
        )
        return {}
    _issue(
        issues,
        path=path,
        keyword="$ref",
        action="rewritten",
        reason=f"内联本地定义:{name}",
        weakens_shape=False,
    )
    resolved = deepcopy(target)
    resolved["__hy3_ref_stack__"] = (*ref_stack, name)
    return resolved


def _merge_schema_overlay(
    base: dict[str, Any],
    overlay: dict[str, Any],
    *,
    path: str,
    construct: str,
    issues: list[Hy3SchemaDialectIssue],
) -> dict[str, Any]:
    """Merge sibling constraints without silently replacing an existing rule.

    JSON Schema applies ``$ref``/``allOf`` siblings as an intersection.  A
    plain ``dict.update`` can instead replace ``required``/``properties`` or
    another shape constraint and thereby weaken the source contract.  T1 does
    not invent a general intersection engine: identical or disjoint siblings
    are safe, while conflicting keys are explicit unsupported results.
    """

    merged = deepcopy(base)
    for key, value in overlay.items():
        if key in merged and merged[key] != value:
            _issue(
                issues,
                path=path,
                keyword=key,
                action="unsupported",
                reason=(
                    f"{construct} sibling 与被内联 schema 冲突；"
                    "禁止用覆盖合并削弱 required/shape"
                ),
                weakens_shape=True,
            )
            continue
        merged[key] = deepcopy(value)
    return merged


def _compile_schema_node(
    node: Any,
    *,
    definitions: dict[str, Any],
    path: str,
    issues: list[Hy3SchemaDialectIssue],
    ref_stack: tuple[str, ...] = (),
) -> Any:
    if isinstance(node, list):
        return [
            _compile_schema_node(
                item,
                definitions=definitions,
                path=f"{path}[{index}]",
                issues=issues,
                ref_stack=ref_stack,
            )
            for index, item in enumerate(node)
        ]
    if isinstance(node, bool) or not isinstance(node, dict):
        return deepcopy(node)

    local_ref_stack = ref_stack
    stack_marker = node.get("__hy3_ref_stack__")
    if isinstance(stack_marker, tuple) and all(
        isinstance(value, str) for value in stack_marker
    ):
        local_ref_stack = stack_marker
        node = {key: value for key, value in node.items() if key != "__hy3_ref_stack__"}

    if "$ref" in node:
        resolved = _resolve_local_ref(
            node.get("$ref"),
            definitions,
            path=path,
            issues=issues,
            ref_stack=local_ref_stack,
        )
        overlay = {key: value for key, value in node.items() if key != "$ref"}
        resolved = _merge_schema_overlay(
            resolved,
            overlay,
            path=path,
            construct="$ref",
            issues=issues,
        )
        return _compile_schema_node(
            resolved,
            definitions=definitions,
            path=path,
            issues=issues,
            ref_stack=local_ref_stack,
        )

    all_of = node.get("allOf")
    if isinstance(all_of, list) and len(all_of) == 1:
        branch = deepcopy(all_of[0])
        if not isinstance(branch, dict):
            _issue(
                issues,
                path=path,
                keyword="allOf",
                action="unsupported",
                reason="单分支 allOf 必须包含 object schema",
                weakens_shape=True,
            )
            branch = {}
        branch = _merge_schema_overlay(
            branch,
            {key: value for key, value in node.items() if key != "allOf"},
            path=path,
            construct="allOf",
            issues=issues,
        )
        _issue(
            issues,
            path=path,
            keyword="allOf",
            action="rewritten",
            reason="单分支 allOf 已内联",
            weakens_shape=False,
        )
        return _compile_schema_node(
            branch,
            definitions=definitions,
            path=path,
            issues=issues,
            ref_stack=local_ref_stack,
        )

    precompiled_any_of: list[Any] | None = None
    any_of = node.get("anyOf")
    if isinstance(any_of, list) and any_of:
        normalized_branches = [
            _compile_schema_node(
                branch,
                definitions=definitions,
                path=f"{path}.anyOf[{index}]",
                issues=issues,
                ref_stack=local_ref_stack,
            )
            for index, branch in enumerate(any_of)
        ]
        if all(
            isinstance(branch, dict)
            and isinstance(branch.get("type"), str)
            and set(branch) == {"type"}
            for branch in normalized_branches
        ):
            rewritten = _merge_schema_overlay(
                {"type": [branch["type"] for branch in normalized_branches]},
                {key: value for key, value in node.items() if key != "anyOf"},
                path=path,
                construct="anyOf type union",
                issues=issues,
            )
            _issue(
                issues,
                path=path,
                keyword="anyOf",
                action="rewritten",
                reason="纯类型 union 已改写为 type 数组",
                weakens_shape=False,
            )
            return _compile_schema_node(
                rewritten,
                definitions=definitions,
                path=path,
                issues=issues,
                ref_stack=local_ref_stack,
            )
        non_null = [
            branch
            for branch in normalized_branches
            if not (isinstance(branch, dict) and branch.get("type") == "null")
        ]
        if len(non_null) == 1 and len(non_null) != len(normalized_branches):
            nullable = deepcopy(non_null[0])
            if isinstance(nullable, dict) and isinstance(nullable.get("type"), str):
                nullable["type"] = [nullable["type"], "null"]
                enum_values = nullable.get("enum")
                if isinstance(enum_values, list) and None not in enum_values:
                    nullable["enum"] = [*enum_values, None]
                nullable = _merge_schema_overlay(
                    nullable,
                    {key: value for key, value in node.items() if key != "anyOf"},
                    path=path,
                    construct="nullable anyOf",
                    issues=issues,
                )
                _issue(
                    issues,
                    path=path,
                    keyword="anyOf",
                    action="rewritten",
                    reason="nullable union 已改写为 type[..., null]",
                    weakens_shape=False,
                )
                return _compile_schema_node(
                    nullable,
                    definitions=definitions,
                    path=path,
                    issues=issues,
                    ref_stack=local_ref_stack,
                )
        # The branches above were compiled for union normalization.  Reuse
        # those exact nodes instead of compiling them a second time and
        # duplicating the loss report.
        precompiled_any_of = normalized_branches

    normalized: dict[str, Any] = {}
    for key, value in node.items():
        key_path = f"{path}.{key}"
        if key == "$defs":
            continue
        if key in _ANNOTATION_KEYWORDS or key == "format":
            _issue(
                issues,
                path=path,
                keyword=key,
                action="dropped",
                reason=(
                    "annotation 不进入 Provider wire；本地 Pydantic 仍校验"
                    if key != "format"
                    else "format 方言兼容未证；本地 Pydantic 仍校验"
                ),
                weakens_shape=False,
            )
            continue
        if key == "properties":
            if not isinstance(value, dict):
                _issue(
                    issues,
                    path=path,
                    keyword=key,
                    action="unsupported",
                    reason="properties 必须为 object",
                    weakens_shape=True,
                )
                continue
            normalized[key] = {
                name: _compile_schema_node(
                    child,
                    definitions=definitions,
                    path=f"{path}.properties.{name}",
                    issues=issues,
                    ref_stack=local_ref_stack,
                )
                for name, child in value.items()
            }
            continue
        if key == "required":
            if not isinstance(value, list) or not all(
                isinstance(item, str) for item in value
            ):
                _issue(
                    issues,
                    path=path,
                    keyword=key,
                    action="unsupported",
                    reason="required 必须为字符串数组",
                    weakens_shape=True,
                )
                continue
            normalized[key] = list(value)
            continue
        if key == "anyOf" and precompiled_any_of is not None:
            normalized[key] = deepcopy(precompiled_any_of)
            continue
        if key in _RECURSIVE_SINGLE_KEYWORDS:
            normalized[key] = _compile_schema_node(
                value,
                definitions=definitions,
                path=key_path,
                issues=issues,
                ref_stack=local_ref_stack,
            )
            continue
        if key in _RECURSIVE_LIST_KEYWORDS:
            if not isinstance(value, list):
                _issue(
                    issues,
                    path=path,
                    keyword=key,
                    action="unsupported",
                    reason=f"{key} 必须为 schema 数组",
                    weakens_shape=True,
                )
                continue
            normalized[key] = [
                _compile_schema_node(
                    child,
                    definitions=definitions,
                    path=f"{key_path}[{index}]",
                    issues=issues,
                    ref_stack=local_ref_stack,
                )
                for index, child in enumerate(value)
            ]
            continue
        if key in _SCALAR_VALIDATION_KEYWORDS:
            normalized[key] = deepcopy(value)
            continue
        _issue(
            issues,
            path=path,
            keyword=key,
            action="unsupported",
            reason="未登记的校验 keyword 不得静默裁剪",
            weakens_shape=True,
        )

    node_types = normalized.get("type")
    is_object = node_types == "object" or (
        isinstance(node_types, list) and "object" in node_types
    )
    if is_object:
        properties = normalized.get("properties")
        required = normalized.get("required", [])
        if required and not isinstance(properties, dict):
            _issue(
                issues,
                path=path,
                keyword="required",
                action="unsupported",
                reason="required 存在但 properties 缺失",
                weakens_shape=True,
            )
        elif isinstance(required, list) and isinstance(properties, dict):
            missing = sorted(set(required) - set(properties))
            if missing:
                _issue(
                    issues,
                    path=path,
                    keyword="required",
                    action="unsupported",
                    reason=f"required 引用未知 properties:{missing}",
                    weakens_shape=True,
                )
        if (
            isinstance(properties, dict)
            and normalized.get("additionalProperties") is not False
        ):
            if "additionalProperties" not in normalized:
                previous_kind = "missing"
            elif normalized["additionalProperties"] is True:
                previous_kind = "true"
            else:
                previous_kind = "schema"
            normalized["additionalProperties"] = False
            _issue(
                issues,
                path=path,
                keyword="additionalProperties",
                action="rewritten",
                reason=f"fixed object 递归收紧为 false；previous={previous_kind}",
                weakens_shape=False,
            )
        elif "additionalProperties" not in normalized:
            normalized["additionalProperties"] = False
            _issue(
                issues,
                path=path,
                keyword="additionalProperties",
                action="rewritten",
                reason="无动态键合同的 object 递归补 false",
                weakens_shape=False,
            )
    return normalized


def compile_hy3_chat_json_schema(
    schema: dict[str, Any],
    *,
    inherited_issues: tuple[Hy3SchemaDialectIssue, ...] = (),
) -> Hy3SchemaCompilation:
    """Compile one schema or return exact unsupported paths without weakening it."""

    raw_definitions = schema.get("$defs")
    definitions = raw_definitions if isinstance(raw_definitions, dict) else {}
    issues = list(inherited_issues)
    compiled = _compile_schema_node(
        schema,
        definitions=definitions,
        path="$",
        issues=issues,
    )
    unsupported = tuple(issue for issue in issues if issue.action == "unsupported")
    if unsupported:
        raise Hy3SchemaUnsupportedError(unsupported)
    if not isinstance(compiled, dict) or compiled.get("type") != "object":
        raise Hy3SchemaUnsupportedError(
            (
                Hy3SchemaDialectIssue(
                    path="$",
                    keyword="type",
                    action="unsupported",
                    reason="顶层必须是 JSON object",
                    weakens_shape=True,
                ),
            )
        )
    compiled.pop("$defs", None)
    encoded = canonical_schema_bytes(compiled)
    return Hy3SchemaCompilation(
        schema=compiled,
        canonical_hash=hashlib.sha256(encoded).hexdigest(),
        canonical_bytes=len(encoded),
        issues=tuple(issues),
    )


def _collect_hy3_wire_schema_issues(
    node: Any,
    *,
    path: str,
    issues: list[Hy3SchemaDialectIssue],
) -> None:
    """Audit the Provider-proven subset without confusing property names.

    Compilation may still reconstruct historical schemas containing standard
    JSON Schema keywords.  Dispatch is stricter: every schema-bearing child is
    walked explicitly and a Provider-proven unsupported keyword is rejected
    before any HTTP client call.  The keyword is never removed here.
    """

    if not isinstance(node, dict):
        return
    for keyword, reason in _TOKENHUB_HY3_UNIMPLEMENTED_WIRE_KEYWORDS.items():
        if keyword in node:
            _issue(
                issues,
                path=path,
                keyword=keyword,
                action="unsupported",
                reason=reason,
                weakens_shape=True,
            )

    properties = node.get("properties")
    if isinstance(properties, dict):
        for name, child in properties.items():
            _collect_hy3_wire_schema_issues(
                child,
                path=f"{path}.properties.{name}",
                issues=issues,
            )
    definitions = node.get("$defs")
    if isinstance(definitions, dict):
        for name, child in definitions.items():
            _collect_hy3_wire_schema_issues(
                child,
                path=f"{path}.$defs.{name}",
                issues=issues,
            )
    for keyword in _RECURSIVE_SINGLE_KEYWORDS:
        child = node.get(keyword)
        if isinstance(child, dict):
            _collect_hy3_wire_schema_issues(
                child,
                path=f"{path}.{keyword}",
                issues=issues,
            )
    for keyword in _RECURSIVE_LIST_KEYWORDS:
        children = node.get(keyword)
        if not isinstance(children, list):
            continue
        for index, child in enumerate(children):
            _collect_hy3_wire_schema_issues(
                child,
                path=f"{path}.{keyword}[{index}]",
                issues=issues,
            )


def assert_hy3_schema_dialect_supported(schema: dict[str, Any]) -> None:
    """在外发前拒绝 Provider 实证不支持的 keyword。

    本函数只审计、不做有损变换。调用方必须构造方言可表达的生产 Schema，
    并在本地 Pydantic/authority validation 保留 wire 未表达的业务规则。
    """

    issues: list[Hy3SchemaDialectIssue] = []
    _collect_hy3_wire_schema_issues(schema, path="$", issues=issues)
    if issues:
        raise Hy3SchemaUnsupportedError(tuple(issues))


def compile_prepared_hy3_schema(
    schema: dict[str, Any],
) -> tuple[Hy3SchemaCompilation, type[BaseModel] | None]:
    """Compile a prepared agent schema while preserving its dynamic transform."""

    if not isinstance(schema, PreparedStructuredJsonSchema):
        return compile_hy3_chat_json_schema(schema), None

    initial = compile_hy3_chat_json_schema(schema.raw_schema)
    transformed = deepcopy(initial.schema)
    if schema.transform is not None:
        schema.transform(transformed)
    final = compile_hy3_chat_json_schema(
        transformed,
        inherited_issues=initial.issues,
    )
    return final, schema.output_model


def build_hy3_strict_contract(
    *,
    schema_name: str,
    schema: dict[str, Any],
) -> StrictJsonResponseContract:
    compilation, output_model = compile_prepared_hy3_schema(schema)
    return StrictJsonResponseContract(
        schema_name=sanitize_schema_name(schema_name),
        schema=deepcopy(compilation.schema),
        schema_hash=compilation.canonical_hash,
        schema_bytes=compilation.canonical_bytes,
        loss_report=compilation.issues,
        loss_report_hash=compilation.loss_report_hash,
        output_model=output_model,
    )


def strict_contract_from_params(
    gen_params: dict[str, Any],
    *,
    pop: bool = False,
) -> StrictJsonResponseContract | None:
    value = (
        gen_params.pop(STRICT_JSON_RESPONSE_CONTRACT_PARAM, None)
        if pop
        else gen_params.get(STRICT_JSON_RESPONSE_CONTRACT_PARAM)
    )
    if value is None:
        return None
    if not isinstance(value, StrictJsonResponseContract):
        raise StrictJsonSchemaDriftError("strict response contract 类型漂移")
    return value


def assert_hy3_strict_request(
    *,
    contract: StrictJsonResponseContract,
    base_url: str,
    requested_model: str,
    response_format: object,
) -> None:
    """Fail before network if any exact identity or schema byte has drifted."""

    if contract.output_model is None:
        raise StrictJsonSchemaDriftError("Hy3 strict Pydantic validator 未绑定")
    if (
        contract.provider_authority != TOKENHUB_HY3_PROVIDER_AUTHORITY
        or contract.base_url != TOKENHUB_HY3_BASE_URL
        or contract.host != TOKENHUB_HY3_HOST
        or contract.exact_model_id != TOKENHUB_HY3_MODEL
        or contract.transport != TOKENHUB_HY3_TRANSPORT
        or contract.dialect != TOKENHUB_HY3_SCHEMA_DIALECT
        or contract.compiler_version != TOKENHUB_HY3_SCHEMA_COMPILER_VERSION
    ):
        raise StrictJsonSchemaDriftError("Hy3 strict capability identity 漂移")
    normalized_base_url = base_url.rstrip("/")
    actual_host = (urlsplit(normalized_base_url).hostname or "").lower().rstrip(".")
    if normalized_base_url != contract.base_url or actual_host != contract.host:
        raise StrictJsonSchemaDriftError("Hy3 strict endpoint host/path 漂移")
    if requested_model != contract.exact_model_id:
        raise StrictJsonSchemaDriftError("Hy3 strict requested model 漂移")
    if (
        not isinstance(response_format, dict)
        or response_format.get("type") != "json_schema"
    ):
        raise StrictJsonSchemaDriftError("Hy3 strict response_format.type 漂移")
    config = response_format.get("json_schema")
    if not isinstance(config, dict) or config.get("strict") is not True:
        raise StrictJsonSchemaDriftError("Hy3 strict json_schema.strict 漂移")
    if config.get("name") != contract.schema_name:
        raise StrictJsonSchemaDriftError("Hy3 strict schema name 漂移")
    wire_schema = config.get("schema")
    if not isinstance(wire_schema, dict):
        raise StrictJsonSchemaDriftError("Hy3 strict wire schema 缺失")
    assert_hy3_schema_dialect_supported(wire_schema)
    actual_hash = canonical_schema_hash(wire_schema)
    if actual_hash != contract.schema_hash:
        raise StrictJsonSchemaDriftError(
            "Hy3 strict schema/hash 漂移: "
            f"expected={contract.schema_hash},actual={actual_hash}"
        )


def _json_type_matches(value: Any, type_name: str) -> bool:
    if type_name == "null":
        return value is None
    if type_name == "object":
        return isinstance(value, dict)
    if type_name == "array":
        return isinstance(value, list)
    if type_name == "string":
        return isinstance(value, str)
    if type_name == "boolean":
        return isinstance(value, bool)
    if type_name == "integer":
        return isinstance(value, int) and not isinstance(value, bool)
    if type_name == "number":
        return isinstance(value, (int, float)) and not isinstance(value, bool)
    return False


def _json_equal(left: Any, right: Any) -> bool:
    if type(left) is not type(right):  # noqa: E721 - JSON bool/int differ
        return False
    return left == right


def _as_decimal(value: Any, *, path: str, keyword: str) -> Decimal:
    try:
        return Decimal(str(value))
    except (InvalidOperation, ValueError) as exc:
        raise _WireSchemaError(
            path=path,
            keyword=keyword,
            reason="数值约束不是合法 decimal",
        ) from exc


def _validate_wire_node(value: Any, schema: Any, *, path: str) -> None:
    if schema is True:
        return
    if schema is False:
        raise _WireSchemaError(path=path, keyword="schema", reason="false schema")
    if not isinstance(schema, dict):
        raise _WireSchemaError(path=path, keyword="schema", reason="schema 非 object")

    all_of = schema.get("allOf")
    if isinstance(all_of, list):
        for index, branch in enumerate(all_of):
            _validate_wire_node(value, branch, path=f"{path}.allOf[{index}]")
    any_of = schema.get("anyOf")
    if isinstance(any_of, list):
        if not any(_wire_branch_passes(value, branch, path=path) for branch in any_of):
            raise _WireSchemaError(path=path, keyword="anyOf", reason="无匹配分支")
    one_of = schema.get("oneOf")
    if isinstance(one_of, list):
        passed = sum(_wire_branch_passes(value, branch, path=path) for branch in one_of)
        if passed != 1:
            raise _WireSchemaError(
                path=path,
                keyword="oneOf",
                reason=f"匹配分支数必须为 1，实际 {passed}",
            )
    not_schema = schema.get("not")
    if isinstance(not_schema, (dict, bool)) and _wire_branch_passes(
        value, not_schema, path=path
    ):
        raise _WireSchemaError(path=path, keyword="not", reason="命中禁止分支")

    type_value = schema.get("type")
    if isinstance(type_value, str):
        allowed_types = [type_value]
    elif isinstance(type_value, list) and all(
        isinstance(item, str) for item in type_value
    ):
        allowed_types = type_value
    else:
        allowed_types = []
    if allowed_types and not any(
        _json_type_matches(value, type_name) for type_name in allowed_types
    ):
        raise _WireSchemaError(
            path=path,
            keyword="type",
            reason=f"expected={allowed_types},actual={type(value).__name__}",
        )

    enum_values = schema.get("enum")
    if isinstance(enum_values, list) and not any(
        _json_equal(value, expected) for expected in enum_values
    ):
        raise _WireSchemaError(path=path, keyword="enum", reason="值不在枚举内")
    if "const" in schema and not _json_equal(value, schema["const"]):
        raise _WireSchemaError(path=path, keyword="const", reason="值不等于 const")

    if isinstance(value, dict):
        properties = schema.get("properties")
        property_map = properties if isinstance(properties, dict) else {}
        required = schema.get("required")
        if isinstance(required, list):
            missing = [name for name in required if name not in value]
            if missing:
                raise _WireSchemaError(
                    path=path,
                    keyword="required",
                    reason=f"缺失字段:{missing}",
                )
        min_properties = schema.get("minProperties")
        max_properties = schema.get("maxProperties")
        if isinstance(min_properties, int) and len(value) < min_properties:
            raise _WireSchemaError(
                path=path, keyword="minProperties", reason="object 字段过少"
            )
        if isinstance(max_properties, int) and len(value) > max_properties:
            raise _WireSchemaError(
                path=path, keyword="maxProperties", reason="object 字段过多"
            )
        property_names = schema.get("propertyNames")
        if isinstance(property_names, (dict, bool)):
            for name in value:
                _validate_wire_node(name, property_names, path=f"{path}.<key>")
        additional = schema.get("additionalProperties", True)
        for name, child in value.items():
            if name in property_map:
                _validate_wire_node(child, property_map[name], path=f"{path}.{name}")
            elif additional is False:
                raise _WireSchemaError(
                    path=path,
                    keyword="additionalProperties",
                    reason=f"额外字段:{name}",
                )
            elif isinstance(additional, dict):
                _validate_wire_node(child, additional, path=f"{path}.{name}")

    if isinstance(value, list):
        min_items = schema.get("minItems")
        max_items = schema.get("maxItems")
        if isinstance(min_items, int) and len(value) < min_items:
            raise _WireSchemaError(path=path, keyword="minItems", reason="数组过短")
        if isinstance(max_items, int) and len(value) > max_items:
            raise _WireSchemaError(path=path, keyword="maxItems", reason="数组过长")
        if schema.get("uniqueItems") is True:
            encoded_items = [
                json.dumps(
                    item, ensure_ascii=False, sort_keys=True, separators=(",", ":")
                )
                for item in value
            ]
            if len(set(encoded_items)) != len(encoded_items):
                raise _WireSchemaError(
                    path=path, keyword="uniqueItems", reason="数组含重复项"
                )
        prefix_items = schema.get("prefixItems")
        prefix_count = 0
        if isinstance(prefix_items, list):
            prefix_count = len(prefix_items)
            for index, child_schema in enumerate(prefix_items):
                if index < len(value):
                    _validate_wire_node(
                        value[index], child_schema, path=f"{path}[{index}]"
                    )
        items = schema.get("items")
        if items is False and len(value) > prefix_count:
            raise _WireSchemaError(
                path=path, keyword="items", reason="tuple 含额外元素"
            )
        if isinstance(items, dict):
            start = prefix_count if isinstance(prefix_items, list) else 0
            for index in range(start, len(value)):
                _validate_wire_node(value[index], items, path=f"{path}[{index}]")

    if isinstance(value, str):
        min_length = schema.get("minLength")
        max_length = schema.get("maxLength")
        if isinstance(min_length, int) and len(value) < min_length:
            raise _WireSchemaError(path=path, keyword="minLength", reason="字符串过短")
        if isinstance(max_length, int) and len(value) > max_length:
            raise _WireSchemaError(path=path, keyword="maxLength", reason="字符串过长")
        pattern = schema.get("pattern")
        if isinstance(pattern, str) and re.search(pattern, value) is None:
            raise _WireSchemaError(path=path, keyword="pattern", reason="字符串不匹配")

    if isinstance(value, (int, float)) and not isinstance(value, bool):
        numeric = _as_decimal(value, path=path, keyword="type")
        comparisons = (
            ("minimum", lambda actual, limit: actual >= limit),
            ("maximum", lambda actual, limit: actual <= limit),
            ("exclusiveMinimum", lambda actual, limit: actual > limit),
            ("exclusiveMaximum", lambda actual, limit: actual < limit),
        )
        for keyword, predicate in comparisons:
            limit_value = schema.get(keyword)
            if limit_value is not None:
                limit = _as_decimal(limit_value, path=path, keyword=keyword)
                if not predicate(numeric, limit):
                    raise _WireSchemaError(
                        path=path, keyword=keyword, reason="数值越界"
                    )
        multiple_of = schema.get("multipleOf")
        if multiple_of is not None:
            divisor = _as_decimal(multiple_of, path=path, keyword="multipleOf")
            if divisor == 0 or numeric % divisor != 0:
                raise _WireSchemaError(
                    path=path, keyword="multipleOf", reason="数值不是合法倍数"
                )


def _wire_branch_passes(value: Any, schema: Any, *, path: str) -> bool:
    try:
        _validate_wire_node(value, schema, path=path)
    except _WireSchemaError:
        return False
    return True


def validate_strict_json_response(
    *,
    content: str,
    response_model: str,
    contract: StrictJsonResponseContract,
) -> StrictJsonValidationResult:
    """Lexically normalize allowed string C0, then validate without repair."""

    if response_model != contract.exact_model_id:
        raise StrictJsonSchemaDriftError(
            "Hy3 strict response model 漂移: "
            f"expected={contract.exact_model_id!r},actual={response_model!r}"
        )
    try:
        normalization = normalize_json_string_c0_controls(content)
    except JsonStringC0OutsideStringError as exc:
        raise StrictJsonResponseViolation(
            layer="raw_json_parse",
            schema_name=contract.schema_name,
            schema_hash=contract.schema_hash,
            reason=str(exc),
            normalizer_provenance=exc.provenance,
        ) from exc
    try:
        parsed = json.loads(normalization.normalized_content)
    except json.JSONDecodeError as exc:
        raise StrictJsonResponseViolation(
            layer="raw_json_parse",
            schema_name=contract.schema_name,
            schema_hash=contract.schema_hash,
            reason=f"{exc.msg}@{exc.lineno}:{exc.colno}",
            normalizer_provenance=normalization.provenance,
        ) from exc
    try:
        _validate_wire_node(parsed, contract.schema, path="$")
    except _WireSchemaError as exc:
        raise StrictJsonResponseViolation(
            layer="wire_schema",
            schema_name=contract.schema_name,
            schema_hash=contract.schema_hash,
            reason=f"{exc.path}:{exc.keyword}:{exc.reason}",
            normalizer_provenance=normalization.provenance,
        ) from exc
    if contract.output_model is None:
        return StrictJsonValidationResult(
            normalized_content=normalization.normalized_content,
            normalizer_provenance=normalization.provenance,
            wire_pydantic="not_attached",
        )
    try:
        contract.output_model.model_validate(parsed)
    except ValidationError as exc:
        first = exc.errors(
            include_url=False, include_context=False, include_input=False
        )[0]
        location = ".".join(str(item) for item in first.get("loc", ())) or "$"
        reason = (
            f"{location}:{first.get('type', 'validation_error')}:{first.get('msg', '')}"
        )
        raise StrictJsonResponseViolation(
            layer="wire_pydantic",
            schema_name=contract.schema_name,
            schema_hash=contract.schema_hash,
            reason=reason[:500],
            normalizer_provenance=normalization.provenance,
        ) from exc
    return StrictJsonValidationResult(
        normalized_content=normalization.normalized_content,
        normalizer_provenance=normalization.provenance,
        wire_pydantic="passed",
    )


__all__ = [
    "Hy3SchemaCompilation",
    "Hy3SchemaDialectIssue",
    "Hy3SchemaUnsupportedError",
    "JsonStringC0Change",
    "JsonStringC0NormalizationProvenance",
    "JsonStringC0NormalizationResult",
    "JsonStringC0OutsideStringError",
    "PreparedStructuredJsonSchema",
    "STRICT_JSON_RESPONSE_CONTRACT_PARAM",
    "StrictJsonResponseContract",
    "StrictJsonResponseViolation",
    "StrictJsonSchemaDriftError",
    "StrictJsonValidationResult",
    "TOKENHUB_HY3_BASE_URL",
    "TOKENHUB_HY3_HOST",
    "TOKENHUB_HY3_MODEL",
    "TOKENHUB_HY3_PROVIDER_AUTHORITY",
    "TOKENHUB_HY3_SCHEMA_COMPILER_VERSION",
    "TOKENHUB_HY3_SCHEMA_DIALECT",
    "TOKENHUB_HY3_TRANSPORT",
    "assert_hy3_schema_dialect_supported",
    "assert_hy3_strict_request",
    "append_strict_json_instruction",
    "build_hy3_strict_contract",
    "canonical_schema_hash",
    "compile_hy3_chat_json_schema",
    "compile_prepared_hy3_schema",
    "normalize_json_string_c0_controls",
    "sanitize_schema_name",
    "strict_contract_from_params",
    "validate_strict_json_response",
]
