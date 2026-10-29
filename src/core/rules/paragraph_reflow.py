"""paragraph_length_mean 单因失败的纯代码段落重排。

只插入空行，不改变任何非空白字符。它不是 Agent，不调用 Provider；只有上一轮
唯一确定性失败为向上膨胀的 ``paragraph_length_mean`` 时才启用。
"""

from __future__ import annotations

from dataclasses import dataclass
import re
from typing import Any

from src.core.rules.style_fingerprint import compute_style_fingerprint

PARAGRAPH_REFLOW_MODEL_USED = "local:paragraph_reflow"

_PARAGRAPH_SPLIT = re.compile(r"(?:\r?\n[ \t]*){2,}")
_SENTENCE_END = re.compile(r"[。！？!?](?:[”’」』])?")


@dataclass(frozen=True, slots=True)
class ParagraphReflowResult:
    content: str
    applied: bool
    reason: str
    before_mean: float
    after_mean: float
    upper_bound: float | None = None


def _paragraph_mean_upper_bound(
    validator_failures: list[dict[str, Any]] | None,
) -> float | None:
    if not validator_failures or len(validator_failures) != 1:
        return None
    failure = validator_failures[0]
    if failure.get("name") != "style_drift":
        return None
    details = failure.get("details")
    if not isinstance(details, dict):
        return None
    dimensions = details.get("drifted_dimensions")
    if not isinstance(dimensions, list) or len(dimensions) != 1:
        return None
    dimension = dimensions[0]
    if not isinstance(dimension, dict):
        return None
    if dimension.get("dimension") != "paragraph_length_mean":
        return None
    baseline = dimension.get("baseline_value")
    current = dimension.get("current_value")
    threshold = dimension.get("threshold")
    if not isinstance(baseline, (int, float)):
        return None
    if not isinstance(current, (int, float)):
        return None
    if not isinstance(threshold, (int, float)):
        return None
    baseline_value = float(baseline)
    current_value = float(current)
    threshold_value = float(threshold)
    if baseline_value <= 0 or threshold_value <= 0:
        return None
    upper = baseline_value * (1.0 + threshold_value)
    # 插入空行只会把均值往下推；向下漂移不能机械修。
    if current_value <= upper:
        return None
    return upper


def reflow_paragraph_mean_only(
    *, content: str, validator_failures: list[dict[str, Any]] | None
) -> ParagraphReflowResult:
    """把过长段落按原有句末边界拆开；不能收敛则逐字返回源正文。"""

    before = compute_style_fingerprint(content)["paragraph_length_mean"]
    upper = _paragraph_mean_upper_bound(validator_failures)
    if upper is None:
        return ParagraphReflowResult(
            content=content,
            applied=False,
            reason="not_paragraph_mean_only",
            before_mean=before,
            after_mean=before,
        )
    if before <= upper:
        return ParagraphReflowResult(
            content=content,
            applied=False,
            reason="already_within_bound",
            before_mean=before,
            after_mean=before,
            upper_bound=upper,
        )

    candidate = content
    while candidate:
        current_mean = compute_style_fingerprint(candidate)["paragraph_length_mean"]
        if current_mean <= upper:
            break
        paragraph_spans: list[tuple[int, int]] = []
        start = 0
        for separator in _PARAGRAPH_SPLIT.finditer(candidate):
            if separator.start() > start:
                paragraph_spans.append((start, separator.start()))
            start = separator.end()
        if start < len(candidate):
            paragraph_spans.append((start, len(candidate)))

        splittable: list[tuple[int, int, list[int]]] = []
        for paragraph_start, paragraph_end in paragraph_spans:
            paragraph = candidate[paragraph_start:paragraph_end]
            boundaries = [
                match.end()
                for match in _SENTENCE_END.finditer(paragraph)
                if 0 < match.end() < len(paragraph)
            ]
            if boundaries:
                splittable.append((paragraph_start, paragraph_end, boundaries))
        if not splittable:
            return ParagraphReflowResult(
                content=content,
                applied=False,
                reason="no_sentence_boundary",
                before_mean=before,
                after_mean=before,
                upper_bound=upper,
            )
        paragraph_start, paragraph_end, boundaries = max(
            splittable,
            key=lambda item: item[1] - item[0],
        )
        paragraph_midpoint = (paragraph_end - paragraph_start) / 2
        split_at = min(
            boundaries,
            key=lambda offset: abs(paragraph_midpoint - offset),
        )
        absolute_split = paragraph_start + split_at
        # 唯一变更：在源文本已有句末边界插入两个换行。所有原有空白、引号、
        # 缩进和段落分隔均逐字保留。
        candidate = candidate[:absolute_split] + "\n\n" + candidate[absolute_split:]

    if "".join(candidate.split()) != "".join(content.split()):
        return ParagraphReflowResult(
            content=content,
            applied=False,
            reason="non_whitespace_changed",
            before_mean=before,
            after_mean=before,
            upper_bound=upper,
        )
    after = compute_style_fingerprint(candidate)["paragraph_length_mean"]
    if after > upper:
        return ParagraphReflowResult(
            content=content,
            applied=False,
            reason="not_converged",
            before_mean=before,
            after_mean=before,
            upper_bound=upper,
        )
    return ParagraphReflowResult(
        content=candidate,
        applied=candidate != content,
        reason="reflowed" if candidate != content else "unchanged",
        before_mean=before,
        after_mean=after,
        upper_bound=upper,
    )
