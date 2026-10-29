"""验证 P5-B-02 ``ChapterCard.card_status`` + ``card_approved_at`` Pydantic 字段。

覆盖维度:

- ``card_status`` Literal 4 档允许值(``draft / pending_review / approved / rejected``)
- ``card_status`` 默认值 ``"draft"``(新建未审核)
- ``card_status`` 越档枚举抛 ``ValidationError``(对齐 v2_wishlist 沉淀:严格枚举不脑补)
- ``card_approved_at`` 默认 ``None``(未通过时)
- ``card_approved_at`` 接受 ``datetime`` 实例(UTC)
- 既有 ``ChapterCard`` 构造(``_chapter_card_payload`` 不带这 2 字段)
  仍能成功 — 验证默认值向后兼容 P1-P4 既有调用方
- ``model_dump`` / ``model_validate_json`` round-trip 保留 2 字段
"""

from __future__ import annotations

import datetime as _dt

import pytest
from pydantic import ValidationError

from src.core.models.chapter_card import (
    CardStatus,
    ChapterCard,
    ContinuityContext,
)

UTC_NOW = _dt.datetime(2026, 5, 12, 10, 0, 0, tzinfo=_dt.timezone.utc)

ALLOWED_CARD_STATUSES: tuple[str, ...] = (
    "draft",
    "pending_review",
    "approved",
    "rejected",
)


def _continuity_payload(**overrides: object) -> dict[str, object]:
    base: dict[str, object] = {
        "prev_chapter_tail": "(开篇,无上一章)",
        "unfinished_actions": [],
        "time_gap": "立即续接",
        "scene_continuation": "same",
        "pov_change": False,
        "pov_from": None,
        "pov_to": "主角",
    }
    base.update(overrides)
    return base


def _card_payload(**overrides: object) -> dict[str, object]:
    """构造一份**不带** ``card_status`` / ``card_approved_at`` 的 payload,
    用于验证 Pydantic 默认值在 P1-P4 既有调用方下的向后兼容。"""
    base: dict[str, object] = {
        "chapter_id": "ch_001",
        "workspace_id": "ws_demo",
        "volume_id": "v1",
        "chapter_number": 1,
        "title": "开局",
        "tension_level": 5,
        "must_achieve": ["介绍主角"],
        "must_unresolved": ["主角身世"],
        "forbidden": ["金手指外挂"],
        "pov_character": "李白",
        "setting": "长安",
        "continuity_context": ContinuityContext(**_continuity_payload()),
        "next_chapter_seed": "下一章引子",
        "arc_phase": "opening",
        "is_arc_finale": False,
        "related_plot_threads": ["pt_main"],
        "related_characters": ["李白"],
    }
    base.update(overrides)
    return base


class TestCardStatusDefault:
    """``card_status`` 默认值与向后兼容。"""

    def test_default_is_draft_when_omitted(self) -> None:
        """既有调用方不传 ``card_status`` 时,默认 ``"draft"``。"""
        card = ChapterCard(**_card_payload())
        assert card.card_status == "draft"

    def test_default_card_approved_at_is_none(self) -> None:
        """未审核时 ``card_approved_at`` 默认 ``None``。"""
        card = ChapterCard(**_card_payload())
        assert card.card_approved_at is None

    def test_existing_payload_no_breakage(self) -> None:
        """完全兼容 P1-P4 既有 ``_chapter_card_payload`` 结构(不带 2 新字段)。"""
        card = ChapterCard(**_card_payload())
        # 所有原字段仍保留
        assert card.chapter_id == "ch_001"
        assert card.tension_level == 5
        assert "draft_strategy" not in card.model_dump()


class TestCardStatusLiteralEnum:
    """``card_status`` Literal 4 档严格枚举。"""

    @pytest.mark.parametrize("status", ALLOWED_CARD_STATUSES)
    def test_each_allowed_status_accepted(self, status: str) -> None:
        card = ChapterCard(**_card_payload(card_status=status))
        assert card.card_status == status

    @pytest.mark.parametrize(
        "bad_status",
        [
            "DRAFT",  # 大写不接受
            "Approved",  # 首字母大写不接受
            "pending",  # 简写不接受
            "in_review",  # 自然语义近似词不接受
            "deleted",  # 完全不在枚举内
            "",  # 空字符串
        ],
    )
    def test_disallowed_status_raises_validation_error(
        self, bad_status: str
    ) -> None:
        with pytest.raises(ValidationError) as excinfo:
            ChapterCard(**_card_payload(card_status=bad_status))
        # 校验错误应针对 ``card_status`` 字段
        msg = str(excinfo.value)
        assert "card_status" in msg

    def test_card_status_type_alias_exposed(self) -> None:
        """``CardStatus`` Literal 类型别名作为公共 API 暴露,供 service /
        ORM 层 type hint 复用。"""
        # 简单 sanity:类型别名包含 4 档 IS 集合
        from typing import get_args

        assert set(get_args(CardStatus)) == set(ALLOWED_CARD_STATUSES)


class TestCardApprovedAt:
    """``card_approved_at`` 时间字段。"""

    def test_accepts_utc_datetime(self) -> None:
        card = ChapterCard(
            **_card_payload(
                card_status="approved",
                card_approved_at=UTC_NOW,
            )
        )
        assert card.card_approved_at == UTC_NOW

    def test_accepts_none_when_status_is_approved(self) -> None:
        """Pydantic 层不强制 status=approved → approved_at 非空;
        强制语义留 P5-B-07 service 层处理(本任务只落数据结构)。"""
        card = ChapterCard(
            **_card_payload(
                card_status="approved",
                card_approved_at=None,
            )
        )
        assert card.card_approved_at is None


class TestCardStatusRoundTrip:
    """``model_dump`` / ``model_validate_json`` round-trip 保留 2 字段。"""

    def test_model_dump_includes_card_status_fields(self) -> None:
        card = ChapterCard(
            **_card_payload(
                card_status="pending_review",
                card_approved_at=None,
            )
        )
        dumped = card.model_dump()
        assert dumped["card_status"] == "pending_review"
        assert dumped["card_approved_at"] is None

    def test_json_round_trip_preserves_card_status_fields(self) -> None:
        original = ChapterCard(
            **_card_payload(
                card_status="approved",
                card_approved_at=UTC_NOW,
            )
        )
        as_json = original.model_dump_json()
        rebuilt = ChapterCard.model_validate_json(as_json)
        assert rebuilt.card_status == "approved"
        assert rebuilt.card_approved_at == UTC_NOW

    def test_json_round_trip_with_default_card_status(self) -> None:
        """从 P1-P4 既有 JSON 文件载入(不带 2 字段)时,默认 ``"draft"``。"""
        legacy_payload = _card_payload()
        # 模拟历史 JSON 文件:dump 后剥去 2 新字段再 validate
        original = ChapterCard(**legacy_payload)
        as_dict = original.model_dump()
        as_dict.pop("card_status")
        as_dict.pop("card_approved_at")
        import json

        rebuilt = ChapterCard.model_validate_json(json.dumps(as_dict, default=str))
        assert rebuilt.card_status == "draft"
        assert rebuilt.card_approved_at is None
