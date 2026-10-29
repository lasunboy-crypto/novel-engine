"""机制1 角色完整性回填 agent 单测(v2 W3.5-s4-step1,§2.58)。

覆盖(对齐 outline_consistency_validator 同型 mock 测范围):

1. **no-op**:LLM 返 ``missing_characters=[]`` → characters 原样 + added=[]
2. **补卡**:LLM 返 1 个悬空角色 → 追加进 characters + added=[该角色]
3. **去重(id)**:返回的角色 id 已在名册 → 跳过(skipped_dup)
4. **去重(name)**:返回的角色 name 已在名册 → 跳过
5. **封顶 8**:名册 7 + LLM 返 3 个新 → 只追加 1(剩 2 skipped_cap)
6. **已满跳过**:名册 8 → 不调 LLM,直接 no-op
7. **fail-open(LLM 抛)**:client.complete 抛 → characters 原样
8. **fail-open(坏 JSON retry 耗尽)**:返回非法 JSON → characters 原样
9. **mock 端到端**:MockLLMClient 经 marker 返空 → no-op(护 outline happy 路径)

完全 mock ``LLMClient``;用临时目录注入 fake ``outline_builder/character_backfill.md``。
"""

from __future__ import annotations

import json
from pathlib import Path
from unittest.mock import MagicMock

import pytest

from src.core.agents import _prompt as prompt_module
from src.core.agents.character_backfill import (
    BackfillPayload,
    CharacterBackfillError,
    backfill_plot_referenced_characters,
)
from src.core.llm.base import LLMClient, LLMResponse, TokenUsage
from src.core.llm.mock_client import MockLLMClient
from src.core.models.outline import (
    CharactersOutput,
    PlotOutput,
    VolumePlansOutput,
    WorldOutput,
)


# ---------------------------------------------------------------------------
# fixture
# ---------------------------------------------------------------------------


def _make_world() -> WorldOutput:
    return WorldOutput(
        setting="2020 年代西北戈壁,新发现汉代古墓",
        power_system="",
        factions="文保课题组 + 守陵人 + 收购网络",
        culture="半农半牧",
        unique_rules="直视符号 9 秒认知污染",
    )


def _card(cid: str, name: str, role: str = "supporting") -> dict:
    return {
        "character_id": cid,
        "name": name,
        "role": role,
        "archetype": "原型",
        "appearance": "外貌",
        "personality": "性格描述足够长以过校验",
        "background": "背景描述足够长以过校验",
        "motivation": "驱动力描述足够长以过校验",
        "voice_hint": "短句冷峻",
        "initial_core_facts": [],
        "relationships_brief": {},
    }


def _make_characters(n: int = 2) -> CharactersOutput:
    base = [
        _card("lu-chen", "陆沉", "protagonist"),
        _card("wei-zhi-yuan", "魏致远", "antagonist"),
        _card("qin-feng", "秦锋", "deuteragonist"),
        _card("chu-yao", "楚瑶", "deuteragonist"),
        _card("lin-shu", "林恕", "antagonist"),
        _card("su-mian", "苏眠"),
        _card("fang-x", "方某"),
        _card("gu-y", "顾某"),
    ]
    return CharactersOutput(characters=base[:n])


def _make_plot() -> PlotOutput:
    return PlotOutput(
        core_conflict="陆沉追查导师失踪真相 vs 魏致远的绝对抹杀",
        main_phases=[
            {"phase_id": "phase-1", "title": "觉醒", "summary": "导师古墓失踪"},
            {"phase_id": "phase-2", "title": "调查", "summary": "都市暗网博弈"},
            {"phase_id": "phase-3", "title": "真相", "summary": "导师被做成服务器"},
            {"phase_id": "phase-4", "title": "抉择", "summary": "封印符号"},
        ],
        major_turning_points=[
            {"turning_id": "turn-1", "title": "古墓坍缩", "description": "导师失踪"},
            {"turning_id": "turn-2", "title": "导师真相", "description": "人肉服务器"},
            {"turning_id": "turn-3", "title": "封印", "description": "自我封印"},
        ],
        long_threads=[
            {"thread_id": "th-1", "title": "导师笔记", "description": "加密笔记"},
            {"thread_id": "th-2", "title": "哑弹", "description": "退路"},
            {"thread_id": "th-3", "title": "赎罪名单", "description": "汇款"},
        ],
        character_arcs={"lu-chen": "守界凡人", "wei-zhi-yuan": "自毁"},
    )


def _make_volume_plans() -> VolumePlansOutput:
    return VolumePlansOutput(
        volumes=[
            {
                "volume_id": "vol-1",
                "title": "古墓回声",
                "summary": "陆沉追查导师失踪,并在守陵人顾修的协助下进入古墓。",
                "chapter_count": 4,
                "tension_template": "heros_journey",
                "linked_phases": ["phase-1"],
                "linked_turning_points": ["turn-1"],
                "linked_threads": ["th-1"],
                "arcs": [
                    {
                        "arc_id": "arc-1",
                        "arc_type": "主线",
                        "chapter_range": (1, 2),
                        "phase": "opening",
                        "summary": "顾修带陆沉进入墓道并解释守陵规矩。",
                        "beats": [
                            {
                                "beat_id": "beat-1",
                                "chapter_range": (1, 2),
                                "summary": "顾修试探陆沉是否值得信任。",
                                "must_advance": ["顾修交出第一枚墓钥"],
                                "forbidden_advance": ["提前揭露魏致远真身"],
                            }
                        ],
                    },
                    {
                        "arc_id": "arc-2",
                        "arc_type": "反转",
                        "chapter_range": (3, 4),
                        "phase": "rising",
                        "summary": "守陵人顾修暴露自己也被符号污染。",
                        "beats": [],
                    },
                ],
                "tension_curve": [3, 5, 6, 7],
            }
        ]
    )


def _resp(missing: list[dict], model_used: str = "qwen3.7-max") -> LLMResponse:
    return LLMResponse(
        content=json.dumps({"missing_characters": missing}, ensure_ascii=False),
        model_used=model_used,
        usage=TokenUsage(input_tokens=900, output_tokens=120),
        finish_reason="stop",
    )


@pytest.fixture(autouse=True)
def _clear_prompt_cache() -> None:
    prompt_module.load_prompt_template.cache_clear()


@pytest.fixture
def fake_prompts_dir(tmp_path: Path) -> Path:
    prompts_dir = tmp_path / "prompts"
    (prompts_dir / "outline_builder").mkdir(parents=True)
    (prompts_dir / "outline_builder" / "character_backfill.md").write_text(
        "FAKE_CHARACTER_BACKFILL_PROMPT\n\n单测用 system 标记。", encoding="utf-8"
    )
    return prompts_dir


@pytest.fixture
def mock_client() -> MagicMock:
    client = MagicMock(spec=LLMClient)
    return client


def _call(client, fake_prompts_dir, *, characters=None):
    return backfill_plot_referenced_characters(
        world=_make_world(),
        characters=characters if characters is not None else _make_characters(4),
        plot=_make_plot(),
        premise="考古队古墓符号",
        genre="都市悬疑",
        client=client,
        model="qwen3.7-max",
        prompts_dir=fake_prompts_dir,
    )


# ---------------------------------------------------------------------------
# 测试
# ---------------------------------------------------------------------------


def test_noop_when_empty(mock_client, fake_prompts_dir):
    """LLM 返空 → characters 原样 + added=[]。"""
    mock_client.complete.return_value = _resp([])
    updated, added = _call(mock_client, fake_prompts_dir)
    assert added == []
    assert len(updated.characters) == 4


def test_backfill_adds_missing_character(mock_client, fake_prompts_dir):
    """LLM 返 1 个悬空角色 → 追加 + added 含它。"""
    mock_client.complete.return_value = _resp([_card("dao-shi", "导师")])
    updated, added = _call(mock_client, fake_prompts_dir)
    assert len(added) == 1
    assert added[0].character_id == "dao-shi"
    assert len(updated.characters) == 5
    assert "dao-shi" in {c.character_id for c in updated.characters}


def test_volume_plans_context_can_backfill_missing_character(
    mock_client, fake_prompts_dir
):
    """C-7b:volume_plans 新增关键人物时,同一 backfill 可在 mini_plan 前补卡。"""
    mock_client.complete.return_value = _resp([_card("gu-xiu", "顾修")])
    updated, added = backfill_plot_referenced_characters(
        world=_make_world(),
        characters=_make_characters(4),
        plot=_make_plot(),
        volume_plans=_make_volume_plans(),
        premise="考古队古墓符号",
        genre="都市悬疑",
        client=mock_client,
        model="qwen3.7-max",
        prompts_dir=fake_prompts_dir,
    )

    user_text = mock_client.complete.call_args.kwargs["messages"][1].content
    assert "## 卷级规划(volume_plans" in user_text
    assert "顾修交出第一枚墓钥" in user_text
    assert len(added) == 1
    assert added[0].character_id == "gu-xiu"
    assert len(updated.characters) == 5


@pytest.mark.parametrize("after_volume", [False, True])
def test_backfill_request_preserves_established_world_and_character_material(
    mock_client, fake_prompts_dir, after_volume
):
    """两个实际回填入口都先见完整前史/关系/规则；同一调用补卡且不改旧卡。"""
    world = _make_world().model_copy(update={"power_system": "使用仪器会暂时失去方向感"})
    characters = _make_characters(4)
    characters.characters[0] = type(characters.characters[0]).model_validate({
        **characters.characters[0].model_dump(),
        "background": "出航前已经失踪；同伴最后在海岸见过他",
        "motivation": "寻找失踪的姐姐并保全同伴",
        "identity_facets": [{"context": "海港身份", "identity": "失踪前的航道测绘员"}],
        "relationships_brief": {"wei-zhi-yuan": "一起长大的堂兄，双方知道对方旧伤"},
        "initial_core_facts": [{
            "attribute": "life_status", "value": "失踪", "source_field": "background",
            "evidence_quote": "出航前已经失踪",
        }],
    })
    original = characters.model_dump(mode="json")
    mock_client.complete.return_value = _resp([_card("new-companion", "同行者")])

    updated, added = backfill_plot_referenced_characters(
        world=world, characters=characters, plot=_make_plot(),
        volume_plans=_make_volume_plans() if after_volume else None,
        premise="失踪者留下航海日志", genre="悬疑",
        client=mock_client, model="qwen3.7-max", prompts_dir=fake_prompts_dir,
    )

    mock_client.complete.assert_called_once()
    user_text = mock_client.complete.call_args.kwargs["messages"][1].content
    for value in world.model_dump().values():
        assert value in user_text
    for character in original["characters"]:
        for field in (
            "name", "role", "archetype", "appearance", "personality", "background",
            "motivation", "voice_hint",
        ):
            assert character[field] in user_text, field
    assert "一起长大的堂兄，双方知道对方旧伤" in user_text
    assert "海港身份" in user_text and "失踪前的航道测绘员" in user_text
    assert '"initial_core_facts"' in user_text and '"evidence_quote"' in user_text
    assert "出航前已经失踪" in user_text
    assert characters.model_dump(mode="json") == original
    assert updated.model_dump(mode="json")["characters"][:-1] == original["characters"]
    assert len(added) == 1



def _characters_with_all_fact_slots_used():
    data = _make_characters(4).model_dump()
    for character in data["characters"]:
        character["initial_core_facts"] = [
            {"attribute": "identity", "value": "原型", "source_field": "archetype", "evidence_quote": "原型"},
            {"attribute": "faction", "value": "背景描述", "source_field": "background", "evidence_quote": "背景描述"},
        ]
    return CharactersOutput.model_validate(data)


def test_full_fact_budget_is_visible_but_does_not_prevent_adding_a_character(mock_client, fake_prompts_dir):
    characters = _characters_with_all_fact_slots_used()
    original = characters.model_dump()
    mock_client.complete.return_value = _resp([_card("new-guide", "向导")])
    updated, added = _call(mock_client, fake_prompts_dir, characters=characters)
    text = mock_client.complete.call_args.kwargs["messages"][1].content
    assert "初始核心事实已用 8 条，剩余 0 条" in text
    assert "人物背景和关系仍应完整写出" in text
    assert len(added) == 1 and len(updated.characters) == 5
    assert sum(len(c.initial_core_facts) for c in updated.characters) == 8
    assert characters.model_dump() == original
    mock_client.complete.assert_called_once()


def test_merged_fact_overflow_reaches_existing_feedback_instead_of_losing_character(mock_client, fake_prompts_dir):
    characters = _characters_with_all_fact_slots_used()
    invalid = _card("new-guide", "向导")
    invalid["initial_core_facts"] = [{"attribute": "identity", "value": "原型", "source_field": "archetype", "evidence_quote": "原型"}]
    mock_client.complete.side_effect = [_resp([invalid]), _resp([_card("new-guide", "向导")])]
    updated, added = _call(mock_client, fake_prompts_dir, characters=characters)
    assert mock_client.complete.call_count == 2
    feedback = mock_client.complete.call_args.kwargs["messages"][1].content
    assert "全书 initial_core_facts 最多 8 条" in feedback
    assert [c.character_id for c in added] == ["new-guide"]
    assert updated.characters[-1].initial_core_facts == []
    assert updated.model_dump()["characters"][:-1] == characters.model_dump()["characters"]


def test_dedup_by_id(mock_client, fake_prompts_dir):
    """返回角色 id 已在名册 → 跳过,不追加。"""
    mock_client.complete.return_value = _resp([_card("lu-chen", "另一个名字")])
    updated, added = _call(mock_client, fake_prompts_dir)
    assert added == []
    assert len(updated.characters) == 4


def test_dedup_by_name(mock_client, fake_prompts_dir):
    """返回角色 name 已在名册(换了 id)→ 跳过。"""
    mock_client.complete.return_value = _resp([_card("new-id", "陆沉")])
    updated, added = _call(mock_client, fake_prompts_dir)
    assert added == []
    assert len(updated.characters) == 4


def test_cap_at_eight(mock_client, fake_prompts_dir):
    """名册 7 + LLM 返 3 新 → 只追加 1(封顶 8),其余 skipped_cap。"""
    mock_client.complete.return_value = _resp(
        [_card("x1", "新一"), _card("x2", "新二"), _card("x3", "新三")]
    )
    updated, added = _call(
        mock_client, fake_prompts_dir, characters=_make_characters(7)
    )
    assert len(added) == 1
    assert len(updated.characters) == 8


def test_skip_when_already_full(mock_client, fake_prompts_dir):
    """名册已 8 → 不调 LLM,直接 no-op。"""
    updated, added = _call(
        mock_client, fake_prompts_dir, characters=_make_characters(8)
    )
    assert added == []
    assert len(updated.characters) == 8
    mock_client.complete.assert_not_called()


def test_fail_open_on_llm_error(mock_client, fake_prompts_dir):
    """client.complete 抛 → fail-open,characters 原样。"""
    mock_client.complete.side_effect = RuntimeError("网络炸了")
    updated, added = _call(mock_client, fake_prompts_dir)
    assert added == []
    assert len(updated.characters) == 4


def test_fail_open_on_bad_json(mock_client, fake_prompts_dir):
    """返回非法 JSON / 非 schema → retry 耗尽 fail-open。"""
    mock_client.complete.return_value = LLMResponse(
        content="这不是 JSON 而是一段废话",
        model_used="qwen3.7-max",
        usage=TokenUsage(input_tokens=10, output_tokens=10),
        finish_reason="stop",
    )
    updated, added = _call(mock_client, fake_prompts_dir)
    assert added == []
    assert len(updated.characters) == 4


def test_setup_fail_open_missing_prompt(mock_client, tmp_path):
    """prompt 文件缺失 → setup fail-open,characters 原样(不抛)。"""
    empty_prompts = tmp_path / "empty_prompts"
    empty_prompts.mkdir()
    updated, added = _call(mock_client, empty_prompts)
    assert added == []
    assert len(updated.characters) == 4
    mock_client.complete.assert_not_called()


@pytest.mark.parametrize("failure", ["transport", "json", "fact_quote", "missing_verdict"])
def test_experimental_backfill_failure_cannot_become_empty_success(
    mock_client, fake_prompts_dir, failure,
):
    mock_client.creative_delta_capability = "creative_delta_v6"
    original = _make_characters(4)
    frozen = original.model_dump()
    if failure == "transport":
        mock_client.complete.side_effect = RuntimeError("network")
    else:
        invalid = _card("new-guide", "新向导")
        invalid["initial_core_facts"] = [{
            "attribute": "identity", "value": "已退休，现居海港",
            "source_field": "background", "evidence_quote": "半年前退休后搬往海港",
        }]
        text = {
            "json": '{"missing_characters":[{"name":"新向导"}]}缺尾部',
            "fact_quote": _resp([invalid]).content,
            "missing_verdict": "{}",
        }[failure]
        mock_client.complete.return_value = _resp([]).model_copy(update={"content": text})
    with pytest.raises(CharacterBackfillError) as caught:
        _call(mock_client, fake_prompts_dir, characters=original)
    assert original.model_dump() == frozen
    assert mock_client.complete.call_count == (1 if failure == "transport" else 3)
    assert caught.value.attempts == mock_client.complete.call_count
    if failure != "transport":
        assert caught.value.raw_output == text


def test_experimental_backfill_missing_prompt_stops_before_provider(mock_client, tmp_path):
    mock_client.creative_delta_capability = "creative_delta_v6"
    with pytest.raises(CharacterBackfillError, match="准备失败"):
        _call(mock_client, tmp_path)
    mock_client.complete.assert_not_called()


def test_experimental_backfill_explicit_empty_is_valid(mock_client, fake_prompts_dir):
    mock_client.creative_delta_capability = "creative_delta_v6"
    mock_client.complete.return_value = _resp([])
    original = _make_characters(4)
    updated, added = _call(mock_client, fake_prompts_dir, characters=original)
    assert updated is original and added == []
    mock_client.complete.assert_called_once()


def test_mock_client_end_to_end_noop():
    """MockLLMClient 经 marker 返空 missing_characters → no-op(护 outline happy 路径)。"""
    updated, added = backfill_plot_referenced_characters(
        world=_make_world(),
        characters=_make_characters(4),
        plot=_make_plot(),
        premise="考古队古墓符号",
        genre="都市悬疑",
        client=MockLLMClient(),
        model="mock",
    )
    assert added == []
    assert len(updated.characters) == 4


def test_backfill_payload_defaults_empty():
    """BackfillPayload 缺字段 → 空列表(LLM 偶发漏键不崩)。"""
    assert BackfillPayload().missing_characters == []
    assert BackfillPayload.model_validate({}).missing_characters == []
