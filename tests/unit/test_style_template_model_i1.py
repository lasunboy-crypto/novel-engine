"""I1(v1.1 wave 4 session 3):写作风格模板注册表 + 正文文件完整性单测。

覆盖 ``src/core/models/style_template.py`` 注册表与 ``prompts/style_guide_templates/``
6 份正文文件的一致性(名称同源、文件存在、维度齐全)。
"""

from __future__ import annotations

from pathlib import Path

from src.config import settings
from src.core.models.style_template import (
    STYLE_TEMPLATE_VALUES,
    STYLE_TEMPLATES,
    StyleTemplateMeta,
    style_guide_filename,
)

_EXPECTED_NAMES = (
    "comedic_burst",
    "grand_epic",
    "heroic_battle",
    "dark_mystery",
    "healing_daily",
    "classical_xianxia",
    "xianxia_growth",
)


def test_six_templates_and_values_align() -> None:
    assert STYLE_TEMPLATE_VALUES == _EXPECTED_NAMES
    assert tuple(STYLE_TEMPLATES.keys()) == _EXPECTED_NAMES
    assert len(STYLE_TEMPLATES) == 7


def test_registry_meta_self_consistent() -> None:
    for name, meta in STYLE_TEMPLATES.items():
        assert isinstance(meta, StyleTemplateMeta)
        # name 字段与 dict key 一致(防注册表手抖)。
        assert meta.name == name
        assert meta.label.strip()
        assert meta.description.strip()


def test_ten_dimensions_all_filled() -> None:
    for meta in STYLE_TEMPLATES.values():
        dims = meta.dimensions.model_dump()
        # 10 个可控维度(candidates §I1)。
        assert len(dims) == 10
        for value in dims.values():
            assert isinstance(value, str) and value.strip()


def test_prose_files_exist_and_nonempty() -> None:
    templates_dir = settings.prompts_dir.resolve() / "style_guide_templates"
    for name in STYLE_TEMPLATE_VALUES:
        path: Path = templates_dir / style_guide_filename(name)
        assert path.is_file(), f"缺风格指南正文: {path}"
        assert path.read_text(encoding="utf-8").strip()


def test_style_guide_filename_format() -> None:
    assert style_guide_filename("comedic_burst") == "comedic_burst.md"
