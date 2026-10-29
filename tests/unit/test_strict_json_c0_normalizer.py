from __future__ import annotations

import hashlib
import json

import pytest
from pydantic import BaseModel, ConfigDict

from src.core.llm.strict_json_schema import (
    JsonStringC0OutsideStringError,
    PreparedStructuredJsonSchema,
    StrictJsonResponseContract,
    StrictJsonResponseViolation,
    build_hy3_strict_contract,
    normalize_json_string_c0_controls,
    validate_strict_json_response,
)


class _Payload(BaseModel):
    model_config = ConfigDict(strict=True)

    value: str
    count: int


def _contract() -> StrictJsonResponseContract:
    raw_schema = _Payload.model_json_schema()
    prepared = PreparedStructuredJsonSchema(
        raw_schema,
        output_model=_Payload,
        raw_schema=raw_schema,
        transform=None,
    )
    return build_hy3_strict_contract(schema_name="c0_fixture", schema=prepared)


def test_all_raw_c0_codepoints_inside_string_are_escaped_with_provenance() -> None:
    raw_value = "".join(chr(value) for value in range(0x20))
    content = f'{{"value":"{raw_value}","count":1}}'

    result = normalize_json_string_c0_controls(content)

    assert json.loads(result.normalized_content) == {
        "value": raw_value,
        "count": 1,
    }
    assert result.provenance.status == "normalized"
    assert result.provenance.changed_count == 32
    assert [change.codepoint for change in result.provenance.changes] == [
        f"U+{value:04X}" for value in range(0x20)
    ]
    assert result.provenance.raw_sha256 == hashlib.sha256(
        content.encode("utf-8")
    ).hexdigest()
    assert result.provenance.normalized_sha256 == hashlib.sha256(
        result.normalized_content.encode("utf-8")
    ).hexdigest()


@pytest.mark.parametrize("codepoint", range(0x20))
def test_every_raw_c0_codepoint_outside_strings_is_rejected(codepoint: int) -> None:
    content = f'{{"value":"ok","count":1}}{chr(codepoint)}'

    with pytest.raises(JsonStringC0OutsideStringError) as raised:
        normalize_json_string_c0_controls(content)

    provenance = raised.value.provenance
    assert provenance.status == "rejected_c0_outside_string"
    assert provenance.rejected_codepoint == f"U+{codepoint:04X}"
    assert provenance.rejected_char_index == len(content) - 1
    assert provenance.normalized_sha256 is None


def test_valid_compact_json_and_existing_unicode_escape_remain_byte_identical() -> None:
    content = r'{"value":"保留\\n与\u0000","count":1}'

    result = normalize_json_string_c0_controls(content)

    assert result.normalized_content == content
    assert result.provenance.status == "unchanged"
    assert result.provenance.changed_count == 0
    assert result.provenance.raw_sha256 == result.provenance.normalized_sha256


def test_raw_c0_after_escape_prefix_uses_existing_backslash() -> None:
    content = '{"value":"before\\\nafter","count":1}'

    result = normalize_json_string_c0_controls(content)

    assert result.normalized_content == r'{"value":"before\u000Aafter","count":1}'
    assert json.loads(result.normalized_content)["value"] == "before\nafter"
    assert result.provenance.changes[0].completed_existing_escape is True


def test_strict_layers_accept_normalized_content_and_preserve_raw_hash() -> None:
    content = '{"value":"before\x00after","count":1}'

    validation = validate_strict_json_response(
        content=content,
        response_model="hy3",
        contract=_contract(),
    )

    assert validation.raw_json_parse == "passed"
    assert validation.wire_schema == "passed"
    assert validation.wire_pydantic == "passed"
    assert json.loads(validation.normalized_content)["value"] == "before\x00after"
    assert validation.normalizer_provenance.raw_sha256 == hashlib.sha256(
        content.encode("utf-8")
    ).hexdigest()


@pytest.mark.parametrize(
    ("content", "layer"),
    [
        ('{"value":"truncated\x00",', "raw_json_parse"),
        ('{"value":"missing count\x00"}', "wire_schema"),
        ('{"value":"wrong type\x00","count":"1"}', "wire_schema"),
    ],
)
def test_normalizer_does_not_repair_structure_or_types(
    content: str, layer: str
) -> None:
    with pytest.raises(StrictJsonResponseViolation) as raised:
        validate_strict_json_response(
            content=content,
            response_model="hy3",
            contract=_contract(),
        )

    assert raised.value.layer == layer
    provenance = raised.value.normalizer_provenance
    assert provenance is not None
    assert provenance.changed_count == 1


def test_strict_boundary_rejects_raw_c0_outside_string() -> None:
    content = '{"value":"ok","count":1}\n'

    with pytest.raises(StrictJsonResponseViolation) as raised:
        validate_strict_json_response(
            content=content,
            response_model="hy3",
            contract=_contract(),
        )

    assert raised.value.layer == "raw_json_parse"
    assert raised.value.normalizer_provenance is not None
    assert (
        raised.value.normalizer_provenance.status
        == "rejected_c0_outside_string"
    )
