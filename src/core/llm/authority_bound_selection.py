"""Program-owned authority catalogs for model-selected canonical references.

The model may write free creative prose, but it may only select canonical IDs
that the program froze for the current stage.  One immutable snapshot drives
the prompt catalog, dynamic JSON Schema enums, and the post-response local
validator.  This module performs no IO and knows nothing about story content.
"""

from __future__ import annotations

import hashlib
import json
from dataclasses import dataclass
from typing import Any, Literal


AUTHORITY_SELECTION_VERSION = "authority-selection-v1"


class AuthorityBoundSelectionError(ValueError):
    """Authority is missing, drifted, or a response selected an unknown ID."""


@dataclass(frozen=True, slots=True)
class AuthorityCatalogEntry:
    """One program-owned stable ID plus the minimum human-readable meaning."""

    stable_id: str
    semantic: str

    def __post_init__(self) -> None:
        if not self.stable_id or self.stable_id != self.stable_id.strip():
            raise AuthorityBoundSelectionError(
                "authority stable_id 必须非空且不得依赖空白修复"
            )
        if not self.semantic or not self.semantic.strip():
            raise AuthorityBoundSelectionError(
                f"authority stable_id={self.stable_id!r} 缺少人读语义"
            )

    def as_payload(self) -> dict[str, str]:
        return {"stable_id": self.stable_id, "semantic": self.semantic}


@dataclass(frozen=True, slots=True)
class AuthorityCatalog:
    """A named, deterministic namespace of canonical choices."""

    name: str
    entries: tuple[AuthorityCatalogEntry, ...]

    @classmethod
    def freeze(
        cls,
        *,
        name: str,
        entries: list[AuthorityCatalogEntry] | tuple[AuthorityCatalogEntry, ...],
    ) -> AuthorityCatalog:
        if not name or name != name.strip():
            raise AuthorityBoundSelectionError("authority catalog name 不合法")
        seen: set[str] = set()
        ordered: list[AuthorityCatalogEntry] = []
        for entry in sorted(entries, key=lambda item: item.stable_id):
            if entry.stable_id in seen:
                raise AuthorityBoundSelectionError(
                    f"authority catalog={name!r} 存在重复 stable_id={entry.stable_id!r}"
                )
            seen.add(entry.stable_id)
            ordered.append(entry)
        return cls(name=name, entries=tuple(ordered))

    @property
    def ids(self) -> tuple[str, ...]:
        return tuple(entry.stable_id for entry in self.entries)

    def as_payload(self) -> dict[str, Any]:
        return {
            "name": self.name,
            "entries": [entry.as_payload() for entry in self.entries],
        }


@dataclass(frozen=True, slots=True)
class AuthoritySnapshot:
    """Canonical, hash-bound authority source shared by all request layers."""

    stage: str
    catalogs: tuple[AuthorityCatalog, ...]
    snapshot_hash: str
    version: str = AUTHORITY_SELECTION_VERSION

    @classmethod
    def freeze(
        cls,
        *,
        stage: str,
        catalogs: list[AuthorityCatalog] | tuple[AuthorityCatalog, ...],
        required_catalogs: set[str] | frozenset[str] = frozenset(),
    ) -> AuthoritySnapshot:
        if not stage or stage != stage.strip():
            raise AuthorityBoundSelectionError("authority stage 不合法")
        ordered = tuple(sorted(catalogs, key=lambda item: item.name))
        names = [catalog.name for catalog in ordered]
        if len(names) != len(set(names)):
            raise AuthorityBoundSelectionError("authority catalog name 重复")
        missing = sorted(set(required_catalogs) - set(names))
        if missing:
            raise AuthorityBoundSelectionError(
                f"required authority catalog 缺失:{missing}"
            )
        empty = sorted(
            catalog.name
            for catalog in ordered
            if catalog.name in required_catalogs and not catalog.entries
        )
        if empty:
            raise AuthorityBoundSelectionError(
                f"required authority catalog 为空:{empty}"
            )

        # Stable IDs are global identities inside one stage.  Reject collisions
        # instead of guessing a namespace or repairing an ID.
        owners: dict[str, str] = {}
        for catalog in ordered:
            for stable_id in catalog.ids:
                previous = owners.setdefault(stable_id, catalog.name)
                if previous != catalog.name:
                    raise AuthorityBoundSelectionError(
                        "authority stable_id 跨 catalog 冲突:"
                        f"{stable_id!r} in {previous!r}/{catalog.name!r}"
                    )

        payload = {
            "version": AUTHORITY_SELECTION_VERSION,
            "stage": stage,
            "catalogs": [catalog.as_payload() for catalog in ordered],
        }
        encoded = json.dumps(
            payload,
            ensure_ascii=False,
            sort_keys=True,
            separators=(",", ":"),
        ).encode("utf-8")
        return cls(
            stage=stage,
            catalogs=ordered,
            snapshot_hash=hashlib.sha256(encoded).hexdigest(),
        )

    def as_payload(self) -> dict[str, Any]:
        return {
            "version": self.version,
            "stage": self.stage,
            "catalogs": [catalog.as_payload() for catalog in self.catalogs],
        }

    def catalog(self, name: str) -> AuthorityCatalog:
        for catalog in self.catalogs:
            if catalog.name == name:
                return catalog
        raise AuthorityBoundSelectionError(f"authority catalog 未冻结:{name!r}")

    def assert_matches(self, current: AuthoritySnapshot) -> None:
        if (
            self.snapshot_hash != current.snapshot_hash
            or self.as_payload() != current.as_payload()
        ):
            raise AuthorityBoundSelectionError(
                "authority snapshot/hash 在 prompt、schema 或本地校验之间漂移:"
                f"frozen={self.snapshot_hash},current={current.snapshot_hash}"
            )


EmptyCatalogPolicy = Literal["reject", "require_empty_array"]


@dataclass(frozen=True, slots=True)
class AuthorityReferenceBinding:
    """Map one JSON output path to one or more frozen catalogs."""

    field_path: str
    catalogs: tuple[str, ...]
    allow_empty_value: bool = False
    empty_catalog_policy: EmptyCatalogPolicy = "reject"

    def __post_init__(self) -> None:
        if not self.field_path or not self.catalogs:
            raise AuthorityBoundSelectionError("authority reference binding 不完整")
        if len(self.catalogs) != len(set(self.catalogs)):
            raise AuthorityBoundSelectionError(
                f"authority binding catalog 重复:{self.field_path}"
            )


@dataclass(frozen=True, slots=True)
class AuthoritySelectionContract:
    """Immutable materialization contract for one structured model request."""

    snapshot: AuthoritySnapshot
    bindings: tuple[AuthorityReferenceBinding, ...]

    def __post_init__(self) -> None:
        paths = [binding.field_path for binding in self.bindings]
        if len(paths) != len(set(paths)):
            raise AuthorityBoundSelectionError("authority binding field_path 重复")
        available = {catalog.name for catalog in self.snapshot.catalogs}
        for binding in self.bindings:
            missing = sorted(set(binding.catalogs) - available)
            if missing:
                raise AuthorityBoundSelectionError(
                    f"authority binding={binding.field_path!r} 引用了缺失 catalog:{missing}"
                )
            if not self._allowed_ids(binding) and binding.empty_catalog_policy == "reject":
                raise AuthorityBoundSelectionError(
                    f"authority binding={binding.field_path!r} 的合法集合为空"
                )

    @property
    def snapshot_hash(self) -> str:
        return self.snapshot.snapshot_hash

    def catalog_ids(self, name: str) -> tuple[str, ...]:
        return self.snapshot.catalog(name).ids

    def assert_matches(self, current: AuthoritySelectionContract) -> None:
        self.snapshot.assert_matches(current.snapshot)
        if self.bindings != current.bindings:
            raise AuthorityBoundSelectionError(
                "authority reference bindings 在 request 生命周期内漂移"
            )

    def render_prompt_catalog(self) -> str:
        """Render the exact frozen snapshot; no IDs are parsed from prose."""

        lines = [
            "## 稳定 ID 选择权威（程序冻结）",
            "",
            f"- authority_contract_version:{self.snapshot.version}",
            f"- authority_stage:{self.snapshot.stage}",
            f"- authority_snapshot_hash:{self.snapshot.snapshot_hash}",
            "- 只能逐字选择下列 stable_id；禁止新造、改写、大小写修复、别名映射或补全。",
            "- 未选择时只能使用字段 schema 明示允许的空值或空数组。",
            "",
            "### authority_catalog_jsonl",
        ]
        for catalog in self.snapshot.catalogs:
            if not catalog.entries:
                lines.append(
                    json.dumps(
                        {"catalog": catalog.name, "entries": []},
                        ensure_ascii=False,
                        sort_keys=True,
                        separators=(",", ":"),
                    )
                )
                continue
            for entry in catalog.entries:
                lines.append(
                    json.dumps(
                        {
                            "catalog": catalog.name,
                            "stable_id": entry.stable_id,
                            "semantic": entry.semantic,
                        },
                        ensure_ascii=False,
                        sort_keys=True,
                        separators=(",", ":"),
                    )
                )
        return "\n".join(lines)

    def transform_schema(self, schema: dict[str, Any]) -> None:
        """Apply enums to scalars and array items in the Hy3-safe dialect."""

        for binding in self.bindings:
            target, nearest_array = _schema_node_for_path(schema, binding.field_path)
            allowed = list(self._allowed_ids(binding))
            if not allowed:
                if (
                    binding.empty_catalog_policy == "require_empty_array"
                    and nearest_array is not None
                ):
                    nearest_array["maxItems"] = 0
                    continue
                raise AuthorityBoundSelectionError(
                    f"authority schema enum 为空:{binding.field_path}"
                )
            if binding.allow_empty_value:
                allowed.insert(0, "")
            target["enum"] = allowed

    def validate_payload(self, payload: dict[str, Any]) -> None:
        """Validate selections again before deterministic product assembly."""

        violations: list[str] = []
        for binding in self.bindings:
            allowed = set(self._allowed_ids(binding))
            if binding.allow_empty_value:
                allowed.add("")
            values = _payload_values_for_path(payload, binding.field_path)
            invalid = [value for value in values if value not in allowed]
            if invalid:
                violations.append(
                    f"{binding.field_path}={invalid!r};allowed={sorted(allowed)!r}"
                )
        if violations:
            raise AuthorityBoundSelectionError(
                "model selected canonical reference outside frozen authority: "
                + "; ".join(violations)
            )

    def _allowed_ids(self, binding: AuthorityReferenceBinding) -> tuple[str, ...]:
        values = {
            stable_id
            for name in binding.catalogs
            for stable_id in self.snapshot.catalog(name).ids
        }
        return tuple(sorted(values))


def _path_tokens(field_path: str) -> tuple[tuple[str, bool], ...]:
    tokens: list[tuple[str, bool]] = []
    for raw in field_path.split("."):
        is_array = raw.endswith("[]")
        name = raw[:-2] if is_array else raw
        if not name:
            raise AuthorityBoundSelectionError(
                f"authority field_path 不合法:{field_path!r}"
            )
        tokens.append((name, is_array))
    return tuple(tokens)


def _schema_node_for_path(
    schema: dict[str, Any], field_path: str
) -> tuple[dict[str, Any], dict[str, Any] | None]:
    node = schema
    nearest_array: dict[str, Any] | None = None
    for name, is_array in _path_tokens(field_path):
        properties = node.get("properties")
        if not isinstance(properties, dict):
            raise AuthorityBoundSelectionError(
                f"authority schema path 缺少 properties:{field_path!r}/{name!r}"
            )
        child = properties.get(name)
        if not isinstance(child, dict):
            raise AuthorityBoundSelectionError(
                f"authority schema path 不存在:{field_path!r}/{name!r}"
            )
        node = child
        if is_array:
            if node.get("type") != "array" or not isinstance(node.get("items"), dict):
                raise AuthorityBoundSelectionError(
                    f"authority schema path 不是 array:{field_path!r}/{name!r}"
                )
            nearest_array = node
            node = node["items"]
    return node, nearest_array


def _payload_values_for_path(payload: dict[str, Any], field_path: str) -> list[str]:
    values: list[Any] = [payload]
    for name, is_array in _path_tokens(field_path):
        next_values: list[Any] = []
        for value in values:
            if not isinstance(value, dict) or name not in value:
                raise AuthorityBoundSelectionError(
                    f"authority payload path 缺失:{field_path!r}/{name!r}"
                )
            child = value[name]
            if is_array:
                if not isinstance(child, list):
                    raise AuthorityBoundSelectionError(
                        f"authority payload path 不是 array:{field_path!r}/{name!r}"
                    )
                next_values.extend(child)
            else:
                next_values.append(child)
        values = next_values
    invalid_types = [value for value in values if not isinstance(value, str)]
    if invalid_types:
        raise AuthorityBoundSelectionError(
            f"authority payload canonical reference 必须是 string:{field_path!r}"
        )
    return values


__all__ = [
    "AUTHORITY_SELECTION_VERSION",
    "AuthorityBoundSelectionError",
    "AuthorityCatalog",
    "AuthorityCatalogEntry",
    "AuthorityReferenceBinding",
    "AuthoritySelectionContract",
    "AuthoritySnapshot",
]
