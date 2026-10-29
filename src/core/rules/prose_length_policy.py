"""篇幅要求和用户编辑提示；计数不决定正文通过，也不调用模型。"""

from __future__ import annotations

import re
from typing import TYPE_CHECKING

if TYPE_CHECKING:
    from src.core.models.chapter_card import ChapterCard
    from src.core.models.review import ReviewResult

LONG_CHAPTER_FACTOR = 2


def word_count_is_blocking(card: ChapterCard) -> bool:
    """历史卡保持原合同；正式采用 v1 的卡只保留写作要求。"""
    return card.word_count_policy_version == 0


def length_editing_note(actual: int, maximum: int | None) -> str:
    """特别长时在标题旁显示；没有有效上限时只显示实际计数。"""
    if maximum is None or isinstance(maximum, bool) or maximum <= 0:
        return f"约 {actual} 字符" if actual > 0 else ""
    if actual < LONG_CHAPTER_FACTOR * maximum:
        return ""
    return f"约 {actual} 字符，篇幅较长，可考虑人工分章"


def render_word_count_task(card: ChapterCard) -> str:
    """写前要求一次呈现，保持和其他章级任务同等权重。"""
    if card.word_count_policy_version == 2:
        return (f"本章篇幅约 {card.word_count_min} 字，与其他写作任务同等重视。"
                "篇幅以本章当前设置为准。这是安排内容与详略的约数，不是下限；写完本章经历即可自然收束，允许自然长短，"
                "不为保险达数而展开等待、重复动作、对白或心理。")
    return f"本章篇幅要求：{card.word_count_min}–{card.word_count_max} 字符，与其他写作任务同等重视。"


def rebase_historical_length_review(review: ReviewResult, card: ChapterCard) -> ReviewResult:
    """当前执行上下文采用新策略；旧判决、评分和其他失败原样保留。"""
    from src.core.models.review import stable_constraint_id

    if word_count_is_blocking(card):
        return review
    removed: set[str] = set()
    path = f"chapter_cards/{card.chapter_id}.word_count"
    for item in review.constraint_violations:
        if (item.source_path == path and item.domain == "chapter.internal"
                and item.source_hash == "" and item.severity == "hard"
                and re.fullmatch(r"[1-9]\d* <= actual_word_count <= [1-9]\d*", item.rule_quote)
                and item.constraint_id == stable_constraint_id(
                    source_path=path, domain="word_count", rule_text=item.rule_quote)):
            removed.add(item.constraint_id)
    if not removed:
        return review
    markers = {"word_count", *(f"hard_constraint_regression:{key}" for key in removed)}
    return review.model_copy(update={
        "constraint_violations": [v for v in review.constraint_violations if v.constraint_id not in removed],
        "hard_constraint_regressions": [key for key in review.hard_constraint_regressions if key not in removed],
        "failed_constraints": [name for name in review.failed_constraints if name not in markers],
        "validator_failure_details": [f for f in review.validator_failure_details or [] if f.get("name") != "word_count"],
    })
