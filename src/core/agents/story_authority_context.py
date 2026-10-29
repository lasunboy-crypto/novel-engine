"""Experimental authoring and judge view of already-created story authority."""

from __future__ import annotations

import json

from src.core.models.outline import (
    CharacterOutlineItem,
    CharactersOutput,
    PlotOutput,
    VolumePlansOutput,
    WorldOutput,
)


_OPENING_STATE_GUIDANCE = (
    "world.setting 的开篇时点是各段共同参照。人物 background / initial_core_facts "
    "中明示已经发生的事属于开篇前事实，不得再次安排为主线中的首次发生；"
    "明确标为计划、欲望或未来发展的内容尚未发生。回忆可以呈现前史，必须明确时间层。"
    "不要把关系材料中的将来互动直接当作已完成事件。背景与世界存在实质矛盾时应指出，"
    "不得自行篡改已提交的上游来迁就新情节。\n"
)


def render_story_authority_context(*, world: WorldOutput, characters: CharactersOutput) -> str:
    return (
        "\n\n## 开篇时间与人物既往权威原文\n"
        + _OPENING_STATE_GUIDANCE
        + json.dumps({
            "world": world.model_dump(mode="json"),
            "characters": [c.model_dump(mode="json") for c in characters.characters],
        }, ensure_ascii=False, indent=2)
    )


def render_complete_outline_context(
    *, world: WorldOutput, characters: CharactersOutput,
    plot: PlotOutput, volume_plans: VolumePlansOutput,
    json_handoff: bool = False,
) -> str:
    """终审读取四段原文；JSON 交接由 data 唯一承载原文。"""
    outline_source = (
        "完整原文见本 JSON 资料包 data.world、data.characters、data.plot、"
        "data.volume_plans；请逐字段核对，不能用摘要替代。"
        if json_handoff else json.dumps({
            "world": world.model_dump(mode="json"),
            "characters": characters.model_dump(mode="json"),
            "plot": plot.model_dump(mode="json"),
            "volume_plans": volume_plans.model_dump(mode="json"),
        }, ensure_ascii=False, indent=2)
    )
    return (
        "\n\n## 开篇时间与人物既往权威原文\n"
        "world.setting 给出共同开篇时点。审核对象是这份原样输入：它能否按各字段"
        "定义直接交给后续生成。不要先在理解中移动字段、调整时态或补出事实再宣布通过。\n"
        "\n## 人物字段的开篇档案定义\n"
        "以下定义与人物生成共用。违反字段用途也是输入错误：背景或关系把主线的未来"
        "完成结果写进开篇档案时，必须引用人物原句及主线中同一未来事件的原句作为两端证据，字段定义只作为判断依据，列入"
        "contradictions（substantive）。即使能猜出作者要写未来，也不能在审核中"
        "代为改读后豁免。明确尚未实现的愿望或计划不算已发生；开篇前的真实经历、"
        "回忆和以后才揭晓的旧事可以保留。\n"
        + json.dumps({
            name: CharacterOutlineItem.model_fields[name].description
            for name in ("background", "motivation", "relationships_brief", "initial_core_facts")
        }, ensure_ascii=False, indent=2)
        + "\n## 四段大纲原文\n"
        + outline_source
    )
