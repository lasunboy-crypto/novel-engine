"""程序装配 Writer 的五组叙事材料；不新增模型请求或放宽事实边界。"""

from __future__ import annotations

from pydantic import BaseModel, ConfigDict, JsonValue

from src.core.models.chapter_card import ChapterCard
from src.core.models.character import Character
from src.core.models.setting_contract import SettingContract
from src.core.rules.prose_hard_contract import ExecutableConstraintPlan


class WriterNarrativeBundle(BaseModel):
    """JSON 只组织资料，正文仍由 Writer 自由写成自然语言。"""

    model_config = ConfigDict(extra="forbid", frozen=True)

    task: dict[str, JsonValue]
    reader_and_pov: dict[str, JsonValue]
    characters_and_relationships: dict[str, JsonValue]
    essential_facts_and_boundaries: dict[str, JsonValue]
    available_material_and_expression: dict[str, JsonValue]


def is_full_book_opening(card: ChapterCard, ordered_volume_ids: list[str]) -> bool:
    return bool(ordered_volume_ids and card.chapter_id == "ch-0001"
                and card.chapter_number == 1 and card.volume_id == ordered_volume_ids[0])


def active_character_ids(card: ChapterCard) -> list[str]:
    """仅 present 人物获得当场动作/声音材料；旧卡保持原语义。"""
    if card.narrative_context is None:
        return list(dict.fromkeys([card.pov_character, *card.related_characters]))
    return [p.character_id for p in card.narrative_context.participation if p.mode == "present"]


def build_narrative_bundle(
    *, card: ChapterCard, plan: ExecutableConstraintPlan,
    characters: list[Character], setting: SettingContract | None,
    continuity: str = "", current_state: JsonValue = "", relationships: str = "",
    memory: str = "", retrieved: str = "", plot_threads: str = "",
    callbacks: str = "", style_guide: str = "", warnings: str = "",
    repair: dict[str, JsonValue] | None = None,
    opening_presentation_version: int = 0,
    is_book_opening: bool = False,
) -> WriterNarrativeBundle:
    context = card.narrative_context
    if context is None:
        raise ValueError("五组叙事材料只能用于有版本标记的新卡")
    from src.core.models.narrative import NarrativeContext
    NarrativeContext.model_validate(context.model_dump(mode="json"))
    plan.assert_matches_chapter_card(card)
    plan.assert_matches_setting_contract(setting)
    if setting is None or setting.story_intent != context.story_intent:
        raise ValueError("叙事材料与本书故事定位不一致")
    free_expression = context.story_intent.version >= 6
    records = {c.character_id: c for c in characters}
    materials: list[JsonValue] = []
    for participation in context.participation:
        record = records.get(participation.character_id)
        if record is None:
            raise ValueError(f"叙事材料缺少人物原档：{participation.character_id}")
        material: dict[str, JsonValue] = {
            "character_id": record.character_id, "name": record.name,
            "participation": participation.mode,
        }
        if participation.mode == "present":
            voice = record.voice_profile.model_dump(mode="json")
            if voice.get("sentence_style") == voice.get("tone"):
                voice.pop("sentence_style", None)
            motive = context.character_motives.get(record.character_id)
            material.update({
                "personality": record.personality,
                "appearance_reference": record.appearance,
                "existing_abilities": list(record.abilities),
                "opening_motive_reference": {
                    "desire": motive.present_desire if motive else "",
                    "personal_stakes": motive.personal_stakes if motive else "",
                },
                "long_term_motivation": [goal for goal in record.goals_long_term],
                "voice_shape": voice,
                "identity_facets": [facet.model_dump(mode="json") for facet in record.identity_facets],
            })
        # 回忆/提及仅有引用身份，详细背景由有源 reader_information 提供。
        materials.append(material)

    groups: dict[str, list[JsonValue]] = {
        "required_events_and_results": [], "questions_left_open": [],
        "events_not_allowed": [], "other_conditions": [],
    }
    owners: dict[str, str] = {}
    for item in plan.constraints:
        group = {
            "must_achieve": "required_events_and_results",
            "must_unresolved": "questions_left_open",
            "forbidden": "events_not_allowed",
        }.get(item.kind, "other_conditions")
        if item.rule_text in owners:
            continue
        owners[item.rule_text] = group
        groups[group].append(item.rule_text)

    reader_material: dict[str, JsonValue] = {}
    for use in ("introduce", "background", "defer"):
        reader_material[use] = [f.text for f in context.reader_information if f.use == use]
    reader_material.update({
        "meaning": "交代安排是可用素材，不是逐条朗诵清单。计划过不等于读者已读过；"
                   "作者资料不等于视角人物已知。defer 中的事实本章暂不揭晓。",
        "history_use": {
            "continuity": "前章已发生的尾文与标明的未完事项；只承接真正即时未完动作，已完成场景不重新演出。",
            "prior_story": "已发生的经历、结果与关系现状，供接续；不是本章待办。",
            "retrieved_reference": "更早背景与长期未决参考，不要求逐项复述。",
        },
        "continuity": continuity,
        "prior_story": memory,
        "retrieved_reference": retrieved,
    })
    # 全书推进表供上游规划使用；Writer 只承接本章安排，避免开篇任务复活或提前演未来。
    intent = context.story_intent.model_dump(
        mode="json", exclude={"version", "user_request", "progression_design", "planning_scope", "scope_outcome", "scope_experience", "authoring_plan"},
    )
    if setting.unique_rules.strip() and not plan.by_kind("setting_unique_rules"):
        raise ValueError("新叙事输入必须先冻结世界硬规则的来源")
    expression: dict[str, JsonValue] = {
        "story_intent": intent,
        "reading_focus": card.chapter_focus,
        "scene_focus": card.craft_focus,
        "interaction_direction": card.interaction_focus,
        "character_change_direction": dict(card.arc_beats),
        "breakthrough_payoff": card.breakthrough_payoff,
        "ending_connection": card.next_chapter_seed,
        "world_reference": {
            key: getattr(setting, key) for key in ("setting", "power_system", "factions", "culture")
        },
        "world_reference_use": "作者完整规则用于保持一致；人物可逐步发现，本章只需展现必要事件所依赖的观察。没有要求的其他触发条件、代价和极限可保留到后续，不必首次接触就演完说明书。",
        "plot_thread_reference": plot_threads,
        "callbacks": callbacks,
        "style_guide": style_guide,
        "warnings": warnings,
        "freedom": "在事实与披露边界内，自主安排开头铺垫、动作、对白、表情、心理、情绪、"
                   "场景细节与详略。允许简洁说明已有背景、直接写人物想法和感受。"
                   "参考不要求全用，未解决的问题不要求人物逐条念出不知道。",
    }
    if card.dramatic_source is not None:
        drama = card.dramatic_source.beat_contract
        expression["causal_context"] = {
            "actor_id": drama.actor_id, "counterforce_id": drama.counterforce_id,
            "interaction_mode": drama.interaction_mode,
            "load_bearing_objects": [item.model_dump(mode="json") for item in drama.load_bearing_objects],
            **({"dramatic_intent": {"actor_goal": drama.actor_goal, "counteraction": drama.counteraction,
                "choice": drama.choice, "consequence": drama.consequence},
                "meaning": "必要事件已独立列入事实合同。这里是人物利害与戏剧意图，具体心理、动作和对白由你选择；全书物件目录不代表本章已经取得或须展示全部规则。"}
                if drama.contract_version == 2 else
                {"meaning": "目标、阻力、选择和后果已列入必要事实，不在此重复成写作配额。"}),
        }
    if repair is not None:
        expression["revision_task"] = repair
    if free_expression:
        # 用户方向只出现一次；不再用全书起点和模型补充笔调反复指导当章。
        request = context.story_intent.user_request
        assert request is not None and request.reading_preferences is not None
        scene_reference: dict[str, JsonValue] = {}
        seen: set[str] = set()
        for key in ("reading_focus", "scene_focus", "interaction_direction",
                    "character_change_direction", "breakthrough_payoff", "ending_connection"):
            value = expression.pop(key)
            if isinstance(value, str):
                if not value.strip() or value in seen:
                    continue
                seen.add(value)
            elif not value:
                continue
            scene_reference[key] = value
        expression["scene_reference"] = scene_reference
        expression.pop("story_intent")
        expression["book_reading_direction"] = context.story_intent.reader_interest
        # 旧伏笔描述可能混有规划与历史，明确降为有时点的参考，不能作本章任务。
        reader_material["plot_thread_history"] = expression.pop("plot_thread_reference")
        if style_guide.strip() and style_guide.strip() in request.reading_preferences:
            expression.pop("style_guide")
        for character_material in materials:
            if isinstance(character_material, dict) and character_material.get("participation") == "present":
                character_material["personality_reference"] = character_material.pop("personality")
                character_material.pop("voice_shape", None)
        expression["freedom"] = "按整体阅读方向和人物当下处境自主创作。人物资料是来由与利害参考，情绪、心理、对白、表情、动作、详略和收束方式由你决定。"
    opening_task: dict[str, JsonValue] = {}
    if opening_presentation_version == 1 and not free_expression:
        opening_task["reading_focus"] = expression.pop("reading_focus")
        opening_direction = intent.pop("opening_orientation", "")
        opening_task["book_position"] = "全书首章；读者初次接触这些人物与世界" if is_book_opening else "全书后续章节"
        if is_book_opening:
            opening_task["opening_orientation"] = opening_direction
            opening_task["presentation_freedom"] = "用既有背景让读者进入人物处境，交代时机、形式和详略由你决定，不必一次讲完。"
    phase_names = {"opening": "开端", "rising": "发展", "climax": "高潮",
                   "falling": "余波", "resolution": "收束"}
    return WriterNarrativeBundle(
        task={
            **({"user_requirements": {
                "original_premise_and_preferences": context.story_intent.user_request.premise,
                "genre": context.story_intent.user_request.genre,
                **({"reading_preferences": context.story_intent.user_request.reading_preferences}
                   if free_expression else {}),
                "meaning": "用户原文优先于模板默认和模型设计；其中前提含未来方向，不表示本章全部发生。文学偏好整体落实，不逐条朗诵。",
            }} if context.story_intent.user_request is not None else {}),
            **opening_task,
            "title_reference": card.title,
            "pov_character": card.pov_character,
            "pov_name": records[card.pov_character].name,
            "scene_conditions": card.setting,
            "tension": card.tension_level,
            "story_position": phase_names[card.arc_phase],
            **({"word_count_target": card.word_count_min}
               if card.word_count_policy_version == 2 else
               {"word_count_min": card.word_count_min, "word_count_max": card.word_count_max}),
            **({"length_requirement": (
                "篇幅以当前章节卡设置为准，不沿用前提或风格材料里的旧数字。约数目标与其他写作任务同等重视；不是最低字数。不为保险达数增加等待、重复动作或心理，经历完整即可自然收束。"
                if card.word_count_policy_version == 2 else
                "篇幅要求与其他写作任务同等重视，按人物经历与叙事节奏组织正文。") }
               if card.word_count_policy_version else {}),
            "output": "只输出完整小说正文，不输出资料字段、检查报告或意义总结。",
        },
        reader_and_pov=reader_material,
        characters_and_relationships={
            "characters": materials, "relationships": relationships, "current_state": current_state,
            "motive_time_scope": "opening_motive_reference 源自全书开篇人物档案，仅解释来路。"
                                "本章所求结合本章事件、current_state 与已发生经历理解；"
                                "已结束的机会不能重新等待，长期牵挂可以延续。",
            **({"personality_use": "价值取向、经历与动机解释人物来由；不是固定情绪或表演规则。本章表达按此时的处境与关系创作。"}
               if free_expression else {}),
            "participation_meaning": "present 为当场参与；recalled 只在回忆中；mentioned 仅提及；"
                                     "restricted 本章不能当场出场。相关人物不等于全部登场。",
        },
        essential_facts_and_boundaries={
            **groups,
            "meaning": "这些事件、条件和边界必须成立。结果可以在同一经历中交织，"
                       "无需按条数分场、分段或平均分配字数。保留问题只约束章末状态。",
        },
        available_material_and_expression=expression,
    )
