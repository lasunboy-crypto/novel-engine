from __future__ import annotations

import re
import tomllib
from pathlib import Path


def _filterwarnings() -> list[str]:
    pyproject = Path(__file__).resolve().parents[2] / "pyproject.toml"
    data = tomllib.loads(pyproject.read_text(encoding="utf-8"))
    return data["tool"]["pytest"]["ini_options"].get("filterwarnings", [])


def test_jieba_warning_filters_are_source_scoped() -> None:
    filters = _filterwarnings()

    assert (
        r"ignore:invalid escape sequence.*:DeprecationWarning:jieba(\.|$).*"
        in filters
    )
    assert (
        r"ignore:pkg_resources is deprecated as an API.*:UserWarning:jieba\._compat"
        in filters
    )

    ignore_filters = [item for item in filters if item.startswith("ignore")]
    assert ignore_filters
    for item in ignore_filters:
        parts = item.split(":")
        message = parts[1] if len(parts) > 1 else ""
        category = parts[2] if len(parts) > 2 else ""
        module = parts[3] if len(parts) > 3 else ""

        assert message, item
        assert category in {"DeprecationWarning", "UserWarning"}, item
        assert module.startswith("jieba"), item
        assert re.match(module, "src.services.chapter_card_service") is None
        assert re.match(module, "tests.unit.test_chapter_card_generator") is None
