"""退役批量出卡后的 ``card_generator`` 辅助配置测试。"""

from __future__ import annotations

from pathlib import Path

import pytest
from pydantic import ValidationError

from src.services.card_generator_config import (
    CardGeneratorConfig,
    load_card_generator_config,
)


def test_defaults_only_keep_active_single_card_support_fields() -> None:
    config = CardGeneratorConfig()
    assert config.model_dump() == {
        "diversification_threshold": 0.5,
        "style_controller_enabled": True,
    }


def test_load_missing_file_returns_default(tmp_path: Path) -> None:
    config = load_card_generator_config(tmp_path / "missing.yaml")
    assert config == CardGeneratorConfig()


def test_load_missing_section_returns_default(tmp_path: Path) -> None:
    yaml_path = tmp_path / "config.yaml"
    yaml_path.write_text("cost_guard:\n  daily_usd_limit: 20.0\n", encoding="utf-8")
    assert load_card_generator_config(yaml_path) == CardGeneratorConfig()


def test_load_active_fields(tmp_path: Path) -> None:
    yaml_path = tmp_path / "config.yaml"
    yaml_path.write_text(
        "card_generator:\n"
        "  diversification_threshold: 0.35\n"
        "  style_controller_enabled: false\n",
        encoding="utf-8",
    )
    config = load_card_generator_config(yaml_path)
    assert config.diversification_threshold == 0.35
    assert config.style_controller_enabled is False


@pytest.mark.parametrize(
    "retired_field",
    ("batch_size_after_commit", "pending_review_prefix", "batch_granularity"),
)
def test_retired_batch_fields_are_rejected(retired_field: str) -> None:
    with pytest.raises(ValidationError, match=retired_field):
        CardGeneratorConfig.model_validate({retired_field: 1})


def test_threshold_range_is_validated() -> None:
    with pytest.raises(ValidationError):
        CardGeneratorConfig(diversification_threshold=0)
    with pytest.raises(ValidationError):
        CardGeneratorConfig(diversification_threshold=1.1)
