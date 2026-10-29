"""只读取证用的文学表层信号。

本模块不裁决文学质量、不修改正文，也不新增 Provider 调用。它只把容易机械计数的
对照重构句定位给现有 Reviewer；Reviewer 仍需读上下文，真实纠错可以判为无问题。
"""

from __future__ import annotations

import re
from dataclasses import dataclass

_WINDOW_CHARS = 1000
_WINDOW_TRIGGER = 2
_CHAPTER_TRIGGER = 3
_MAX_RENDERED_MATCHES = 6

_LEFT_CLAUSE = r"[^。！？!?\n]{1,56}?"
_RIGHT_CLAUSE = r"[^。！？!?\n]{1,56}"
_PATTERNS: tuple[tuple[str, re.Pattern[str]], ...] = (
    (
        "not_but",
        re.compile(
            rf"不是{_LEFT_CLAUSE}[，,；;]\s*(?:而是|只是|是){_RIGHT_CLAUSE}"
        ),
    ),
    (
        "not_but_sentence",
        re.compile(rf"不是{_LEFT_CLAUSE}[。]\s*(?:而是|只是|是){_RIGHT_CLAUSE}"),
    ),
    (
        "not_really_but",
        re.compile(
            rf"并非{_LEFT_CLAUSE}[，,；;]\s*(?:而是|只是|实则){_RIGHT_CLAUSE}"
        ),
    ),
    (
        "none_only",
        re.compile(
            rf"没有{_LEFT_CLAUSE}[，,；;]\s*(?:只有|而是){_RIGHT_CLAUSE}"
        ),
    ),
    (
        "rather_than",
        re.compile(rf"与其说{_LEFT_CLAUSE}不如说{_RIGHT_CLAUSE}"),
    ),
    (
        "seems_actually",
        re.compile(
            rf"看似{_LEFT_CLAUSE}[，,；;]\s*(?:实则|其实){_RIGHT_CLAUSE}"
        ),
    ),
    (
        "problem_lies",
        re.compile(
            rf"问题不在{_LEFT_CLAUSE}[，,；;]\s*(?:而?在|在于){_RIGHT_CLAUSE}"
        ),
    ),
)


@dataclass(frozen=True)
class ContrastReframeMatch:
    """正文中一个连续、可定位的对照重构句候选。"""

    kind: str
    start: int
    end: int
    text: str


@dataclass(frozen=True)
class ContrastReframeReport:
    """候选及只用于决定是否值得展示的密度统计。"""

    matches: tuple[ContrastReframeMatch, ...]
    max_window_count: int
    triggered: bool


def detect_contrast_reframes(text: str) -> ContrastReframeReport:
    """定位常见对照重构骨架；不判断某次使用是否合理。"""

    candidates: list[ContrastReframeMatch] = []
    for kind, pattern in _PATTERNS:
        for regex_match in pattern.finditer(text):
            candidates.append(
                ContrastReframeMatch(
                    kind=kind,
                    start=regex_match.start(),
                    end=regex_match.end(),
                    text=regex_match.group(0).strip(),
                )
            )

    # 同一长句可能同时命中相邻家族；重叠时只保留覆盖更完整的一条。
    selected: list[ContrastReframeMatch] = []
    for candidate in sorted(
        candidates,
        key=lambda item: (item.start, -(item.end - item.start), item.kind),
    ):
        if any(
            candidate.start < existing.end and candidate.end > existing.start
            for existing in selected
        ):
            continue
        selected.append(candidate)
    selected.sort(key=lambda item: item.start)

    max_window_count = 0
    left = 0
    for right, candidate in enumerate(selected):
        while candidate.start - selected[left].start >= _WINDOW_CHARS:
            left += 1
        max_window_count = max(max_window_count, right - left + 1)

    return ContrastReframeReport(
        matches=tuple(selected),
        max_window_count=max_window_count,
        triggered=(
            len(selected) >= _CHAPTER_TRIGGER
            or max_window_count >= _WINDOW_TRIGGER
        ),
    )


def render_contrast_reframe_review_section(text: str) -> str:
    """达到 SOFT 展示阈值时渲染 Reviewer 定位段，否则返回空串。"""

    report = detect_contrast_reframes(text)
    if not report.triggered:
        return ""

    lines = [
        "## 公式化对照句候选（SOFT；不得单独决定 verdict）",
        "",
        (
            f"纯代码定位 {len(report.matches)} 处；最高 1000 字窗口 "
            f"{report.max_window_count} 处。真实对白纠错或必要反转可以保留；"
            "只有无功能重复、删去前半句不损信息或承担抽象盖章时，才影响"
            "score_formulaic / score_prose_quality，并只报可唯一定位的局部 issue。"
        ),
        "",
    ]
    for index, match in enumerate(report.matches[:_MAX_RENDERED_MATCHES], start=1):
        lines.append(f"{index}. [{match.kind}] {match.text}")
    return "\n".join(lines)


__all__ = [
    "ContrastReframeMatch",
    "ContrastReframeReport",
    "detect_contrast_reframes",
    "render_contrast_reframe_review_section",
]
