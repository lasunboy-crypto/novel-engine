"""G2(v1.1 wave 4 session 2):ChapterCard.callback_to 字段 + mini_plan
callback_anchors → callback_to 确定性派生 bridge 单测。
"""

from __future__ import annotations

from src.core.agents.chapter_card_generator import (
    CharacterVoiceHint,
    ChapterCardGenerationContext,
    _ChapterCardDecisionPayload,
    _assemble_chapter_card,
)
from src.core.models.chapter_card import ChapterCard, ContinuityContext
from src.core.models.setting_contract import SettingContract


def _continuity() -> ContinuityContext:
    return ContinuityContext(
        prev_chapter_tail="上一章结尾",
        unfinished_actions=[],
        time_gap="立即续接",
        scene_continuation="same",
        pov_change=False,
        pov_from=None,
        pov_to="char-hero",
    )


def _make_context(**overrides: object) -> ChapterCardGenerationContext:
    base: dict[str, object] = dict(
        workspace_id="ws-1",
        chapter_id="ch-0028",
        volume_id="vol-1",
        chapter_number=28,
        arc_id="arc-1",
        arc_type="主线",
        arc_phase="falling",
        arc_summary="收束弧",
        is_arc_finale=False,
        tension_level=5,
        setting_contract=SettingContract(
            setting="架空大陆",
            source_path="data/outline/world.json",
            source_hash="0" * 64,
        ),
        core_conflict="正邪对抗",
        volume_summary="第一卷",
        character_voices=[
            CharacterVoiceHint(
                character_id="char-hero",
                name="主角",
                role="protagonist",
                voice_hint="简洁",
            )
        ],
    )
    base.update(overrides)
    return ChapterCardGenerationContext(**base)


def _make_payload() -> _ChapterCardDecisionPayload:
    return _ChapterCardDecisionPayload(
        title="归乡",
        pov_character="char-hero",
        setting="故乡老宅",
        must_achieve=["主角归乡"],
        must_unresolved=["旧友下落"],
        forbidden=["现代用语"],
        continuity_context=_continuity(),
        next_chapter_seed="种子",
        related_plot_threads=[],
        related_characters=["char-hero"],
    )


def test_chapter_card_callback_to_defaults_empty() -> None:
    card = ChapterCard(
        chapter_id="ch-0001",
        workspace_id="ws-1",
        volume_id="vol-1",
        chapter_number=1,
        title="首章",
        tension_level=5,
        must_achieve=["A"],
        must_unresolved=["B"],
        forbidden=["C"],
        pov_character="char-hero",
        setting="场景",
        continuity_context=_continuity(),
        next_chapter_seed="种子",
        arc_phase="opening",
        is_arc_finale=False,
        related_plot_threads=[],
        related_characters=[],
    )
    assert card.callback_to == []


def test_assemble_card_derives_callback_to_from_anchors() -> None:
    ctx = _make_context(callback_anchors=["thread-rain", "thread-sword"])
    card = _assemble_chapter_card(context=ctx, payload=_make_payload())
    assert card.callback_to == ["thread-rain", "thread-sword"]


def test_assemble_card_callback_to_empty_when_no_anchors() -> None:
    ctx = _make_context(callback_anchors=[])
    card = _assemble_chapter_card(context=ctx, payload=_make_payload())
    assert card.callback_to == []


def test_context_callback_anchors_defaults_empty() -> None:
    assert _make_context().callback_anchors == []
