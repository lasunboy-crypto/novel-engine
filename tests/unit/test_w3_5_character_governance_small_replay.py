"""W3.5 角色治理小重放组合验收。

本文件不跑真 LLM,不读/写真实 workspace 数据库。它把 C-4/C-6/C-7a/C-7b
几个已完成的单点护栏串在一起,复现 s4step1-v1 ch-0007 等已知问题的最小
等价切片。
"""

from __future__ import annotations

import json
from pathlib import Path
from unittest.mock import MagicMock

import pytest

from src.core.agents import _prompt as prompt_module
from src.core.agents.character_backfill import backfill_plot_referenced_characters
from src.core.agents.chapter_card_generator import (
    ChapterCardGenerationContext,
    ChapterCardGeneratorError,
    CharacterVoiceHint,
    PlotThreadSummary,
    generate_chapter_card,
)
from src.core.llm.base import LLMClient, LLMResponse, TokenUsage
from src.core.models.outline import CharactersOutput, PlotOutput, VolumePlansOutput, WorldOutput
from src.core.models.setting_contract import SettingContract
from src.services.chapter_card_service import _select_character_voice_hints


@pytest.fixture(autouse=True)
def _clear_prompt_cache() -> None:
    prompt_module.load_prompt_template.cache_clear()


@pytest.fixture
def fake_prompts_dir(tmp_path: Path) -> Path:
    prompts_dir = tmp_path / "prompts"
    prompts_dir.mkdir()
    (prompts_dir / "chapter_card_generator.md").write_text(
        "FAKE_CHAPTER_CARD_GENERATOR_SYSTEM_PROMPT", encoding="utf-8"
    )
    (prompts_dir / "chapter_card_continuity_ledger_addendum_v1.md").write_text(
        "FAKE_CONTINUITY_LEDGER_ADDENDUM", encoding="utf-8"
    )
    (prompts_dir / "outline_builder").mkdir()
    (prompts_dir / "outline_builder" / "character_backfill.md").write_text(
        "FAKE_CHARACTER_BACKFILL_PROMPT", encoding="utf-8"
    )
    return prompts_dir


def _character(
    character_id: str,
    name: str,
    role: str = "supporting",
) -> dict:
    return {
        "character_id": character_id,
        "name": name,
        "role": role,
        "archetype": role,
        "appearance": "外貌占位",
        "personality": "性格占位足够长",
        "background": "背景占位足够长",
        "motivation": "动机占位足够长",
        "voice_hint": f"{name} 的说话方式",
        "initial_core_facts": [],
        "relationships_brief": {},
    }


def _world() -> WorldOutput:
    return WorldOutput(
        setting="近未来城市与裂隙遗迹并存",
        power_system="裂隙会污染记忆",
        factions="调查局、课题组、收购网络",
        culture="现代都市与地下遗迹并行",
        unique_rules="任何进入裂隙的人都会留下回声",
    )


def _plot() -> PlotOutput:
    return PlotOutput(
        core_conflict="楚戈调查裂隙污染 vs 沈确隐瞒的旧案",
        main_phases=[
            {"phase_id": "phase-1", "title": "监视", "summary": "楚戈独自观察裂隙"},
            {"phase_id": "phase-2", "title": "追查", "summary": "顾修交出墓钥"},
            {"phase_id": "phase-3", "title": "反转", "summary": "课题组旧案浮出"},
            {"phase_id": "phase-4", "title": "收束", "summary": "裂隙真相揭开"},
        ],
        major_turning_points=[
            {"turning_id": "turn-1", "title": "电话", "description": "未知教授来电"},
            {"turning_id": "turn-2", "title": "墓钥", "description": "顾修进入主线"},
            {"turning_id": "turn-3", "title": "裂隙", "description": "最终真相"},
        ],
        long_threads=[
            {"thread_id": "thread-rift", "title": "裂隙污染", "description": "裂隙会改写记忆"},
            {"thread_id": "thread-call", "title": "加密来电", "description": "未知电话牵出旧案"},
            {"thread_id": "thread-key", "title": "墓钥", "description": "顾修持有第一枚墓钥"},
        ],
        character_arcs={"chu-ge": "从独行到信任同伴"},
    )


def _volume_plans() -> VolumePlansOutput:
    return VolumePlansOutput(
        volumes=[
            {
                "volume_id": "vol-1",
                "title": "裂隙监视",
                "summary": "楚戈监视裂隙,顾修作为守门人交出第一枚墓钥。",
                "chapter_count": 4,
                "tension_template": "steady_daily",
                "linked_phases": ["phase-1", "phase-2"],
                "linked_turning_points": ["turn-1", "turn-2"],
                "linked_threads": ["thread-rift"],
                "arcs": [
                    {
                        "arc_id": "arc-1",
                        "arc_type": "主线",
                        "chapter_range": [1, 2],
                        "phase": "rising",
                        "summary": "顾修试探楚戈是否值得信任。",
                        "beats": [
                            {
                                "beat_id": "beat-1",
                                "chapter_range": [1, 2],
                                "summary": "顾修带楚戈进入墓道外围。",
                                "must_advance": ["顾修交出第一枚墓钥"],
                                "forbidden_advance": ["提前揭露顾修已被裂隙污染"],
                            }
                        ],
                    },
                    {
                        "arc_id": "arc-2",
                        "arc_type": "关系线",
                        "chapter_range": [3, 4],
                        "phase": "rising",
                        "summary": "沈确通过远程指令测试楚戈的判断。",
                        "beats": [],
                    },
                ],
                "tension_curve": [3, 5, 6, 7],
            }
        ]
    )


def _llm_client_returning(payload: dict) -> MagicMock:
    client = MagicMock(spec=LLMClient)
    client.complete.return_value = LLMResponse(
        content=json.dumps(payload, ensure_ascii=False),
        model_used="mock-default",
        usage=TokenUsage(input_tokens=1, output_tokens=1),
        finish_reason="stop",
    )
    return client


def _backfill_client(missing: list[dict]) -> MagicMock:
    return _llm_client_returning({"missing_characters": missing})


def _voice(character_id: str, name: str, role: str = "supporting") -> CharacterVoiceHint:
    return CharacterVoiceHint(
        character_id=character_id,
        name=name,
        role=role,
        voice_hint=f"{name} 的说话方式",
    )


def _context(
    *,
    character_voices: list[CharacterVoiceHint],
    mini_plan_linked_characters: list[str],
    chapter_id: str = "ch-0007",
    mini_plan_event: str = "楚戈独自监视裂隙电话,不得新增卡外汇报角色",
) -> ChapterCardGenerationContext:
    return ChapterCardGenerationContext(
        workspace_id="small-replay",
        chapter_id=chapter_id,
        volume_id="vol-1",
        chapter_number=int(chapter_id.removeprefix("ch-")),
        arc_id="arc-1",
        arc_type="主线",
        arc_phase="rising",
        arc_summary="楚戈独自监视裂隙",
        is_arc_finale=False,
        tension_level=7,
        setting_contract=SettingContract(
            setting="近未来城市与裂隙遗迹并存",
            source_path="data/outline/world.json",
            source_hash="0" * 64,
        ),
        core_conflict="楚戈调查裂隙污染 vs 沈确隐瞒旧案",
        volume_summary="楚戈监视裂隙,顾修作为守门人交出墓钥",
        prev_chapter_summary="上一章楚戈接近裂隙外围。",
        prev_chapter_tail="上一章末尾,楚戈没有进入裂隙。",
        prev_pov_character="chu-ge",
        prev_unfinished_actions=[],
        mini_plan_event=mini_plan_event,
        mini_plan_linked_characters=mini_plan_linked_characters,
        character_voices=character_voices,
        plot_threads=[
            PlotThreadSummary(
                thread_id="thread-rift",
                title="裂隙污染",
                description="裂隙会改写记忆",
                status="planted",
            )
        ],
    )


def _card_payload(
    *,
    pov_character: str,
    related_characters: list[str],
    must_achieve: list[str],
) -> dict:
    return {
        "title": "裂隙电话",
        "pov_character": pov_character,
        "setting": "裂隙监控室",
        "must_achieve": must_achieve,
        "must_unresolved": ["电话来源仍未确认"],
        "forbidden": ["不要提前进入裂隙"],
        "continuity_context": {
            "prev_chapter_tail": "上一章末尾,楚戈没有进入裂隙。",
            "unfinished_actions": [],
            "time_gap": "同日夜间",
            "scene_continuation": "same",
            "pov_change": False,
            "pov_from": "chu-ge",
            "pov_to": pov_character,
        },
        "next_chapter_seed": "楚戈追查电话来源。",
        "related_plot_threads": ["thread-rift"],
        "related_characters": related_characters,
    }


def test_ch_0007_su_professor_replay_c6_then_c4_blocks(
    fake_prompts_dir: Path,
) -> None:
    """已知样本 ch-0007:先补 linked 角色 related,再由 C-4 拦卡层造名。"""
    payload = _card_payload(
        pov_character="chu-ge",
        related_characters=["shen-que"],
        must_achieve=["楚戈收到苏教授的加密来电,但不得新增汇报戏"],
    )
    ctx = _context(
        character_voices=[
            _voice("chu-ge", "楚戈", "protagonist"),
            _voice("shen-que", "沈确", "deuteragonist"),
        ],
        mini_plan_linked_characters=["chu-ge"],
    )

    with pytest.raises(ChapterCardGeneratorError) as exc:
        generate_chapter_card(
            ctx,
            client=_llm_client_returning(payload),
            model="mock-default",
            prompts_dir=fake_prompts_dir,
        )

    assert "疑似未知命名称谓" in str(exc.value)
    assert "苏教授" in str(exc.value)
    assert "pov_character" not in str(exc.value)


def test_volume_backfill_character_voices_cap_replay(
    fake_prompts_dir: Path,
) -> None:
    """C-7b 补出的卷级角色进入 C-7a 筛选,且提示池仍封顶 12。"""
    base = CharactersOutput(
        characters=[
            _character("chu-ge", "楚戈", "protagonist"),
            _character("shen-que", "沈确", "deuteragonist"),
            _character("lin-xiao", "林晓", "supporting"),
            _character("wang-zhong", "王忠", "antagonist"),
        ]
    )
    updated, added = backfill_plot_referenced_characters(
        world=_world(),
        characters=base,
        plot=_plot(),
        volume_plans=_volume_plans(),
        premise="裂隙监视与墓钥",
        genre="都市悬疑",
        client=_backfill_client([_character("gu-xiu", "顾修", "supporting")]),
        model="mock-default",
        prompts_dir=fake_prompts_dir,
    )

    voice_by_id = {
        c.character_id: _voice(c.character_id, c.name, c.role)
        for c in updated.characters
    }
    voice_by_id.update(
        {
            f"npc-{i:02d}": _voice(f"npc-{i:02d}", f"涌现角色{i:02d}")
            for i in range(20)
        }
    )
    selected = _select_character_voice_hints(
        workspace_id="small-replay",
        voice_by_id=voice_by_id,
        outline_characters=updated,
        required_character_ids=["gu-xiu", "chu-ge"],
    )

    ids = [v.character_id for v in selected]
    assert [c.character_id for c in added] == ["gu-xiu"]
    assert len(ids) == 12
    assert ids[:2] == ["gu-xiu", "chu-ge"]
    assert {"shen-que", "lin-xiao", "wang-zhong"}.issubset(ids)
    assert "npc-19" not in ids


def test_backfilled_linked_character_reaches_related_without_role_explosion(
    fake_prompts_dir: Path,
) -> None:
    """补卡后的卷级角色被 mini_plan linked 使用时,C-6 补 related 且不塞额外 NPC。"""
    voices = [_voice("chu-ge", "楚戈", "protagonist"), _voice("gu-xiu", "顾修")]
    voices.extend(_voice(f"npc-{i:02d}", f"涌现角色{i:02d}") for i in range(20))
    selected = voices[:12]
    payload = _card_payload(
        pov_character="chu-ge",
        related_characters=["chu-ge"],
        must_achieve=["楚戈听顾修交代墓钥来历"],
    )
    ctx = _context(
        character_voices=selected,
        mini_plan_linked_characters=["gu-xiu"],
        chapter_id="ch-0002",
        mini_plan_event="顾修把第一枚墓钥交给楚戈",
    )

    card = generate_chapter_card(
        ctx,
        client=_llm_client_returning(payload),
        model="mock-default",
        prompts_dir=fake_prompts_dir,
    )

    assert card.related_characters == ["chu-ge", "gu-xiu"]
    assert len(ctx.character_voices) == 12
    assert all(cid not in card.related_characters for cid in ["npc-00", "npc-19"])
