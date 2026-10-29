"""Writer 与 Reviser 共用的权威显著性装配。

本模块只重排 ``ChapterCard`` / ``CoreStateProjection`` 已冻结的展示面；不创建
新事实、不重编约束、不截断承重字段，也不发起任何 IO 或 Provider 调用。
"""

from __future__ import annotations

from collections.abc import Mapping
from dataclasses import dataclass

from src.core.models.chapter_card import ChapterCard
from src.core.models.core_state import CoreStateProjection, render_core_state_capsule


@dataclass(frozen=True, slots=True)
class WriterAuthoritySections:
    """同一份 Writer 权威在不同正文生产路径中的两个固定插入面。"""

    dramatic_source: str = ""
    core_state: str = ""

    def combined(self) -> str:
        """按显著性顺序返回非空段，供 Reviser 整体复用。"""

        return "\n\n".join(
            section for section in (self.dramatic_source, self.core_state) if section
        )


def build_writer_authority_sections(
    *,
    chapter_card: ChapterCard,
    core_state_projection: CoreStateProjection | None = None,
    dramatic_authority_refs: Mapping[str, str] | None = None,
) -> WriterAuthoritySections:
    """从现有 typed authority 逐字装配，不另造 Canon 或派生摘要。"""

    core_state = ""
    if core_state_projection is not None:
        core_state = render_core_state_capsule(
            projection=core_state_projection,
            transitions=chapter_card.core_state_transitions,
            book_clock_constraint=chapter_card.book_clock_constraint,
        ).text
    return WriterAuthoritySections(
        dramatic_source=render_dramatic_source(
            chapter_card,
            authority_refs=dramatic_authority_refs,
        ),
        core_state=core_state,
    )


def render_dramatic_context(card: ChapterCard) -> str:
    """v3 只显示行动的支持事实；四项行动原文在首稿事实视图逐字展示。"""
    source = card.dramatic_source
    if source is None:
        return ""
    beat, promise = source.beat_contract, source.promise
    lines = [
        "## 行动背景",
        f"行动者：{beat.actor_id}；反作用来源：{beat.counterforce_id}；"
        f"互动方式：{beat.interaction_mode}；剧情作用：{beat.function}。",
        f"关联主线：{beat.thread_id}；转折：{beat.turning_id}；"
        f"本卷最终结果：{promise.outcome}。",
        "卷末结果只在相应终章完成或明确延期；当前场景不能因此提前收束。"
        "indirect 允许隔空发生反动作，反作用和后果仍需可见。",
    ]
    if promise.outcome == "explicitly_deferred":
        lines.append(
            f"延期目标卷：{promise.deferred_to_volume_id}；"
            f"延期关联线索：{promise.deferred_to_thread_id}。"
        )
    for item in beat.load_bearing_objects:
        lines.append(
            f"承重物 {item.object_id}（{item.label}）：{item.causal_requirement}\n"
            f"因果作用：{item.causal_role}；关系属性：{','.join(item.relation_attributes)}；"
            f"关联转折：{item.linked_turning_id}；关联线索：{item.linked_thread_id}。"
        )
    return "\n\n".join(lines)


def render_dramatic_source(
    card: ChapterCard,
    *,
    authority_refs: Mapping[str, str] | None = None,
) -> str:
    """渲染 A1/C1 typed source；可把自然文本压成 Writer 内部稳定引用。

    ``authority_refs`` 只用于 exact-Hy3 单次首稿：行动原文会在紧邻的展开计划
    中各出现一次，本段保留 typed 元数据和稳定引用。其它 Writer/Reviser 路径不传
    该映射，继续得到既有逐字文本。
    """

    source = card.dramatic_source
    if source is None:
        return ""
    promise = source.promise
    beat = source.beat_contract
    deferred = ""
    if promise.outcome == "explicitly_deferred":
        deferred = (
            f"- deferred_to_volume_id:{promise.deferred_to_volume_id}\n"
            f"- deferred_to_thread_id:{promise.deferred_to_thread_id}\n"
        )
    object_lines = ""
    if beat.load_bearing_objects:
        rendered_objects = "\n".join(
            "  - "
            f"object_id={item.object_id}; label={item.label}; "
            f"causal_role={item.causal_role}; "
            f"relation_attributes={','.join(item.relation_attributes)}; "
            f"linked_turning_id={item.linked_turning_id}; "
            f"linked_thread_id={item.linked_thread_id}; "
            f"causal_requirement={item.causal_requirement}"
            for item in beat.load_bearing_objects
        )
        object_lines = "- load_bearing_objects:\n" + rendered_objects + "\n"
    if authority_refs is None:
        action_intro = (
            "反动作、选择与可见代价，再消费后面的支持事实；"
            "自然文本不得改写或省略。\n\n"
        )
        action_lines = (
            f"- actor_id:{beat.actor_id}\n"
            f"- actor_goal:{beat.actor_goal}\n"
            f"- counterforce_id:{beat.counterforce_id}\n"
            f"- counteraction:{beat.counteraction}\n"
            f"- choice:{beat.choice}\n"
            f"- consequence:{beat.consequence}\n\n"
        )
        action_boundary = (
            "正文必须把人物动作、反动作、选择与可见后果演出来。"
        )
    else:
        action_intro = "反动作、选择与可见代价，再消费后面的支持事实。\n\n"
        action_pairs = (
            ("actor_goal", beat.actor_goal),
            ("counteraction", beat.counteraction),
            ("choice", beat.choice),
            ("consequence", beat.consequence),
        )
        missing = [name for name, text in action_pairs if text not in authority_refs]
        if missing:
            raise ValueError(
                "exact Hy3 Writer typed 行动缺少展开计划引用:"
                + ",".join(missing)
            )
        action_lines = (
            f"- actor_id:{beat.actor_id}\n"
            f"- actor_goal_ref:`{authority_refs[beat.actor_goal]}`\n"
            f"- counterforce_id:{beat.counterforce_id}\n"
            f"- counteraction_ref:`{authority_refs[beat.counteraction]}`\n"
            f"- choice_ref:`{authority_refs[beat.choice]}`\n"
            f"- consequence_ref:`{authority_refs[beat.consequence]}`\n\n"
        )
        action_boundary = (
            "四项行动原文只在后面的展开计划各展示一次；这里的引用与 typed "
            "source_hash 共同锁定身份，不是让正文复述字段名。"
        )
    return (
        "## 高价值场景行动轴(HARD)\n\n"
        "以下不是通用口号，而是本章已冻结的具体人物行动。先按顺序演出目标、"
        f"{action_intro}"
        f"{action_lines}"
        "### 支持事实与不可变来源身份\n\n"
        f"- source_hash:{source.source_hash}\n"
        f"- volume_id:{source.volume_id}\n"
        f"- beat_id:{source.beat_id}\n"
        f"- function:{beat.function}\n"
        f"- interaction_mode:{beat.interaction_mode}\n"
        f"- thread_id:{beat.thread_id}\n"
        f"- turning_id:{beat.turning_id}\n"
        f"- volume_final_outcome:{promise.outcome}\n"
        f"{deferred}"
        f"{object_lines}"
        "volume_final_outcome 是本卷最终裁决；只有 finale 章在本章完成或明确延期，"
        "其它高价值章不得据此提前收束。以上四项自然文本已经确定性进入 "
        f"must_achieve；{action_boundary}"
        "indirect 允许隔空反动作，不等于可以省略反作用或只写主角查表推断。"
        "load_bearing_objects 是机器锁定的承重身份；正文必须演出 "
        "causal_requirement 与卡片中 possession/control/access 迁移，不得用"
        "自由文本别名或位置说明替代。"
    )


__all__ = [
    "build_writer_authority_sections",
    "render_dramatic_source",
    "WriterAuthoritySections",
]
