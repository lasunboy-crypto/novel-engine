"""R1 离线产品路径：同一 world.json 契约贯通四个消费者。"""

from __future__ import annotations

import json
from pathlib import Path
from unittest.mock import MagicMock

from src.core.agents.chapter_card_generator import (
    ChapterCardGenerationContext,
    CharacterVoiceHint,
    _build_user_message as build_card_user_message,
)
from src.core.agents.outline_builder import (
    _build_mini_plan_user_message_per_volume,
)
from src.core.agents.setting_consistency_validator import (
    validate_setting_consistency,
)
from src.core.agents.writer import write_chapter
from src.core.llm.base import LLMClient, LLMResponse, TokenUsage
from src.core.memory.file_memory import build_memory_section
from src.core.models.outline import VolumeArcPlan, VolumePlanItem, WorldOutput
from src.core.models.setting_contract import (
    SETTING_CONTRACT_SOURCE_PATH,
    SettingContract,
    load_setting_contract,
)
from src.core.workspace.workspace_paths import WorkspacePaths
from tests.unit.test_outline_builder import _make_characters, _make_plot
from tests.unit.test_writer import _make_card


def _response(content: str) -> LLMResponse:
    return LLMResponse(
        content=content,
        model_used="mock-r1",
        usage=TokenUsage(input_tokens=10, output_tokens=10),
        finish_reason="stop",
    )


def _write_world_json(paths: WorkspacePaths) -> tuple[WorldOutput, Path]:
    world = WorldOutput(
        setting="所有可读记录都会在离开地宫后衰变",
        power_system="青铜镜只能在完全无光环境触发",
        factions="守陵人与调查局",
        culture="纸面档案高于口述证词",
        unique_rules=(
            "规则一：距青铜镜一米内的物品不衰变；"
            "规则二：记忆抹除不可逆；规则三：活人不能被直接抽离记忆"
        ),
    )
    paths.outline_dir.mkdir(parents=True, exist_ok=True)
    world_path = paths.outline_dir / "world.json"
    world_path.write_text(
        json.dumps(world.model_dump(mode="json"), ensure_ascii=False, indent=2),
        encoding="utf-8",
    )
    return world, world_path


def test_world_json_contract_reaches_all_consumers_without_legacy_world_md(
    tmp_path: Path,
) -> None:
    paths = WorkspacePaths(
        "ws-r1", workspaces_root=tmp_path / "workspaces"
    )
    world, world_path = _write_world_json(paths)
    contract = load_setting_contract(world_path)

    assert contract == SettingContract.from_world_output(world)
    assert contract.source_path == SETTING_CONTRACT_SOURCE_PATH
    assert len(contract.source_hash) == 64
    assert not (paths.data_dir / "world.md").exists()
    assert build_memory_section(paths) == ""

    volume = VolumePlanItem(
        volume_id="vol-1",
        title="第一卷",
        summary="调查青铜镜规则",
        chapter_count=2,
        tension_template="heros_journey",
        arcs=[
            VolumeArcPlan(
                arc_id="arc-1",
                arc_type="主线",
                chapter_range=(1, 1),
                phase="opening",
                summary="发现规则冲突",
            ),
            VolumeArcPlan(
                arc_id="arc-2",
                arc_type="主线",
                chapter_range=(2, 2),
                phase="rising",
                summary="验证规则边界",
            ),
        ],
        tension_curve=[5, 7],
    )
    mini_plan_message = _build_mini_plan_user_message_per_volume(
        world=world,
        characters=_make_characters(),
        plot=_make_plot(),
        volume=volume,
        chapter_id_map={
            ("vol-1", 1): "ch-0001",
            ("vol-1", 2): "ch-0002",
        },
        prev_volumes_tail=[],
    )

    card_context = ChapterCardGenerationContext(
        workspace_id="ws-r1",
        chapter_id="ch-0001",
        volume_id="vol-1",
        chapter_number=1,
        arc_id="arc-1",
        arc_type="主线",
        arc_phase="opening",
        arc_summary="发现规则冲突",
        is_arc_finale=True,
        tension_level=5,
        setting_contract=contract,
        core_conflict="调查者必须在记录衰变前确认真相",
        volume_summary="调查青铜镜规则",
        character_voices=[
            CharacterVoiceHint(
                character_id="lin-yan",
                name="林衍",
                role="protagonist",
                voice_hint="冷静、短句",
            )
        ],
    )
    card_message = build_card_user_message(card_context)

    prompt_dir = tmp_path / "prompts"
    prompt_dir.mkdir()
    (prompt_dir / "writer.md").write_text("R1 WRITER", encoding="utf-8")
    (prompt_dir / "setting_consistency_validator_v2.md").write_text(
        "R1 SETTING VALIDATOR", encoding="utf-8"
    )

    writer_client = MagicMock(spec=LLMClient)
    writer_client.complete.return_value = _response("离线正文")
    write_chapter(
        chapter_card=_make_card(
            workspace_id="ws-r1",
            chapter_id="ch-0001",
            chapter_number=1,
        ),
        memory_section=build_memory_section(paths),
        setting_contract=contract,
        client=writer_client,
        model="mock-r1",
        prompts_dir=prompt_dir,
    )
    writer_message = writer_client.complete.call_args.kwargs["messages"][1].content

    validator_client = MagicMock(spec=LLMClient)
    validator_client.complete.return_value = _response('{"violations":[]}')
    validate_setting_consistency(
        chapter_text="林衍在完全黑暗中触发青铜镜。",
        contract=contract,
        chapter_id="ch-0001",
        workspace_id="ws-r1",
        client=validator_client,
        model="mock-r1",
        prompts_dir=prompt_dir,
    )
    validator_message = (
        validator_client.complete.call_args.kwargs["messages"][1].content
    )

    expected_contract_block = contract.render_for_prompt()
    for message in (
        mini_plan_message,
        card_message,
        writer_message,
        validator_message,
    ):
        assert expected_contract_block in message
        assert contract.source_path in message
        assert contract.source_hash in message
        assert contract.unique_rules in message
