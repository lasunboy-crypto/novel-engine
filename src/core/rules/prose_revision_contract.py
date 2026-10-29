"""正文 rewrite / revise 的代码可执行改动范围合同。

``ExecutableConstraintPlan`` 冻结“本轮必须满足什么”，本模块再冻结“本轮允许
改动哪里”。局部模式只接受带稳定段落锚点的结构化 patch，并由纯代码从源正文
合成候选；全局字数、风格或结构修复则显式进入 ``global`` 模式。两种模式都不
新增 Agent 或 Provider 请求，合成后的完整正文仍交既有 validator / Judge 裁决。
"""

from __future__ import annotations

import hashlib
import json
import re
import unicodedata
from collections import defaultdict
from collections.abc import Sequence
from dataclasses import dataclass
from difflib import SequenceMatcher
from typing import Literal

from pydantic import BaseModel, ConfigDict, Field, model_validator

from src.core.models.review import ConstraintResult, ConstraintViolation, ReviewIssue
from src.core.rules.prose_hard_contract import (
    ExecutableConstraint,
    ExecutableConstraintPlan,
    ExecutableConstraintPlanError,
)

RevisionMode = Literal["localized", "global"]
PatchOperation = Literal["replace", "insert_before", "insert_after"]
RevisionTargetSource = Literal[
    "constraint_result", "constraint_violation", "review_issue"
]
AdjacentDuplicateMatchKind = Literal[
    "exact", "normalized_equal", "contained", "high_overlap"
]

_SHA256_PATTERN = r"^[0-9a-f]{64}$"
_ANCHOR_PATTERN = r"^p\d{4,}-[0-9a-f]{16}$"
_PARAGRAPH_SEPARATOR = re.compile(r"(?:\r?\n[ \t]*){2,}")
_LOCAL_KINDS = {
    "continuity_unfinished_actions",
    "continuity_scene_same",
    "setting_unique_rules",
    "forbidden",
}
_GLOBAL_KINDS = {
    "word_count",
    "paragraph_mean_floor",
    "must_achieve",
    "must_unresolved",
    "voice_forbidden",
    "cross_chapter_voice",
    "style_drift",
}
GLOBAL_NO_PROGRESS_FEEDBACK = (
    "global_no_progress：上一轮 GLOBAL rewrite 返回了与冻结源正文无实质差异的内容。"
    "不要复述原文或只改空白；必须实际修复本轮失败项，同时保留上一轮 PASS HARD。"
)
_GLOBAL_FAILURE_MARKERS = (
    "global_no_progress",
    "word_count",
    "style_drift",
    "paragraph_mean",
    "paragraph_length",
    "cross_chapter_voice",
    "voice_forbidden",
    "must_achieve",
    "must_unresolved",
    "reviewer verdict",
    "persisted reviewer verdict",
    "manual_product_rewrite",
    "hard_constraint_regression",
)
_LOCAL_FAILURE_MARKERS = ("continuity", "unfinished", "setting", "forbidden")
_ADJACENT_DUPLICATE_MIN_CHARS = 120
_ADJACENT_HIGH_OVERLAP_RATIO = 0.65
_ADJACENT_HIGH_OVERLAP_SHORTER_COVERAGE = 0.85
_PARAGRAPH_DIFF_MAX_CELLS = 100_000
_FULL_CANDIDATE_DIFF_MAX_CHARS = 2_000_000


class ProseRevisionContractError(ValueError):
    """改动范围无法安全编译、响应越界或源身份漂移。"""


class ProseRevisionNoProgressError(ProseRevisionContractError):
    """GLOBAL/LOCALIZED 修订没有产生合同要求的实质正文变化。"""


@dataclass(frozen=True, slots=True)
class LocalizedAdjacentDuplicate:
    """LOCALIZED 新增/替换段与一个紧邻保留段的保守重复证据。"""

    changed_anchor_id: str
    changed_text: str
    neighbor_anchor_id: str
    neighbor_text: str
    match_kind: AdjacentDuplicateMatchKind
    similarity_ratio: float
    shorter_coverage: float


def prose_content_equivalent(source: str, candidate: str) -> bool:
    """判断两份正文是否只存在空白或 Unicode 兼容形式差异。

    GLOBAL rewrite 的职责是修复语义、字数、风格或结构问题；原文照抄、只改
    换行/空格都不算有效内容修订。该判定也供历史版本预算恢复复用，避免已经
    落盘的旧 no-op 永久占用 content rewrite 名额。
    """

    if source == candidate:
        return True

    def normalize(value: str) -> str:
        return re.sub(r"\s+", "", unicodedata.normalize("NFKC", value))

    return normalize(source) == normalize(candidate)


def count_effective_content_rewrites(
    versions: Sequence[tuple[str, str, str]],
    *,
    excluded_model_used: str | None = None,
) -> int:
    """按有序 ``(creation_type, model_used, content)`` 计算有效 rewrite 数。

    Provider 物理调用仍由 AttemptLedger 全量保留；这里只计算会消耗章节
    ``max_rewrites`` 的实际正文变化。机械段落重排继续由调用方传入模型标记
    排除，历史相邻 no-op rewrite 也不再占用内容预算。
    """

    count = 0
    previous_content: str | None = None
    for creation_type, model_used, content in versions:
        if (
            creation_type == "rewrite"
            and model_used != excluded_model_used
            and (
                previous_content is None
                or not prose_content_equivalent(previous_content, content)
            )
        ):
            count += 1
        previous_content = content
    return count


class RevisionAnchor(BaseModel):
    """源正文中的稳定、可回查段落锚点。"""

    model_config = ConfigDict(extra="forbid", frozen=True)

    anchor_id: str = Field(pattern=_ANCHOR_PATTERN)
    paragraph_index: int = Field(ge=1)
    start: int = Field(ge=0)
    end: int = Field(gt=0)
    text: str = Field(min_length=1)

    @model_validator(mode="after")
    def _validate_span(self) -> RevisionAnchor:
        if self.end <= self.start:
            raise ValueError("段落锚点 end 必须大于 start")
        return self


class RevisionTarget(BaseModel):
    """一次局部修复允许命中的锚点和操作集合。"""

    model_config = ConfigDict(extra="forbid", frozen=True)

    target_id: str = Field(pattern=_SHA256_PATTERN)
    source: RevisionTargetSource
    instruction: str = Field(min_length=1)
    allowed_anchor_ids: tuple[str, ...] = Field(min_length=1)
    allowed_operations: tuple[PatchOperation, ...] = Field(min_length=1)

    @model_validator(mode="after")
    def _validate_permissions(self) -> RevisionTarget:
        if len(self.allowed_anchor_ids) != len(set(self.allowed_anchor_ids)):
            raise ValueError("同一 target 不得重复 anchor_id")
        if len(self.allowed_operations) != len(set(self.allowed_operations)):
            raise ValueError("同一 target 不得重复 operation")
        return self


class ProseRevisionContract(BaseModel):
    """一次正文修复请求的不可变源身份与改动边界。"""

    model_config = ConfigDict(extra="forbid", frozen=True)

    contract_version: Literal["1"] = "1"
    mode: RevisionMode
    source_version_id: int = Field(ge=1)
    source_sha256: str = Field(pattern=_SHA256_PATTERN)
    plan_hash: str = Field(pattern=_SHA256_PATTERN)
    source_content: str = Field(min_length=1, exclude=True)
    anchors: tuple[RevisionAnchor, ...] = Field(min_length=1, exclude=True)
    targets: tuple[RevisionTarget, ...] = ()
    preserve_constraint_ids: tuple[str, ...] = ()
    global_reasons: tuple[str, ...] = ()

    @model_validator(mode="after")
    def _validate_contract(self) -> ProseRevisionContract:
        if _sha256(self.source_content) != self.source_sha256:
            raise ValueError("revision contract 的 source_sha256 与正文不一致")
        anchor_ids = [item.anchor_id for item in self.anchors]
        if len(anchor_ids) != len(set(anchor_ids)):
            raise ValueError("revision contract 存在重复 anchor_id")
        target_ids = [item.target_id for item in self.targets]
        if len(target_ids) != len(set(target_ids)):
            raise ValueError("revision contract 存在重复 target_id")
        if len(self.preserve_constraint_ids) != len(
            set(self.preserve_constraint_ids)
        ):
            raise ValueError("revision contract 存在重复 preserve constraint_id")
        known_anchors = set(anchor_ids)
        if any(
            anchor_id not in known_anchors
            for target in self.targets
            for anchor_id in target.allowed_anchor_ids
        ):
            raise ValueError("revision target 引用了未知 anchor_id")
        expected_anchors = _build_anchors(self.source_content)
        if self.anchors != expected_anchors:
            raise ValueError("revision contract 的段落锚点与源正文不一致")
        if self.mode == "localized":
            if not self.targets or self.global_reasons:
                raise ValueError("localized contract 必须有 target 且不得有全局原因")
        elif self.targets or not self.global_reasons:
            raise ValueError("global contract 必须有原因且不得伪装局部 target")
        return self

    def assert_compatible(
        self,
        *,
        source_content: str,
        source_version_id: int,
        executable_constraint_plan: ExecutableConstraintPlan,
    ) -> None:
        """在请求/应用边界拒绝错源版本、正文或计划。"""

        if source_version_id != self.source_version_id:
            raise ProseRevisionContractError(
                "revision contract 的 source_version_id 已漂移"
            )
        if _sha256(source_content) != self.source_sha256:
            raise ProseRevisionContractError(
                "revision contract 的 source_content 已漂移"
            )
        if executable_constraint_plan.plan_hash != self.plan_hash:
            raise ProseRevisionContractError(
                "revision contract 的 ExecutableConstraintPlan 已漂移"
            )


class RevisionPatch(BaseModel):
    """Provider 在局部模式返回的一项授权 patch。"""

    model_config = ConfigDict(extra="forbid", frozen=True)

    target_ids: tuple[str, ...] = Field(min_length=1)
    anchor_id: str = Field(pattern=_ANCHOR_PATTERN)
    operation: PatchOperation
    replacement: str = Field(min_length=1)

    @model_validator(mode="after")
    def _validate_patch(self) -> RevisionPatch:
        if len(self.target_ids) != len(set(self.target_ids)):
            raise ValueError("同一 patch 不得重复 target_id")
        if self.replacement != self.replacement.strip():
            raise ValueError("patch replacement 不得含首尾空白")
        return self


class LocalizedRevisionResponse(BaseModel):
    """局部模式唯一允许的 Provider 响应。"""

    model_config = ConfigDict(extra="forbid", frozen=True)

    contract_version: Literal["1"]
    mode: Literal["localized"]
    source_version_id: int = Field(ge=1)
    source_sha256: str = Field(pattern=_SHA256_PATTERN)
    plan_hash: str = Field(pattern=_SHA256_PATTERN)
    patches: tuple[RevisionPatch, ...] = Field(min_length=1)


def _sha256(value: str) -> str:
    return hashlib.sha256(value.encode("utf-8")).hexdigest()


def _build_anchors(source_content: str) -> tuple[RevisionAnchor, ...]:
    spans: list[tuple[int, int]] = []
    cursor = 0
    for separator in _PARAGRAPH_SEPARATOR.finditer(source_content):
        if source_content[cursor : separator.start()].strip():
            spans.append((cursor, separator.start()))
        cursor = separator.end()
    if source_content[cursor:].strip():
        spans.append((cursor, len(source_content)))
    if not spans:
        raise ProseRevisionContractError("源正文没有可锚定的非空段落")

    source_sha256 = _sha256(source_content)
    return tuple(
        RevisionAnchor(
            anchor_id=(
                f"p{index:04d}-"
                + _sha256(
                    f"{source_sha256}\x00{index}\x00{source_content[start:end]}"
                )[:16]
            ),
            paragraph_index=index,
            start=start,
            end=end,
            text=source_content[start:end],
        )
        for index, (start, end) in enumerate(spans, start=1)
    )


def _make_candidate_anchor(
    *,
    candidate_sha256: str,
    paragraph_index: int,
    start: int,
    end: int,
    text: str,
) -> RevisionAnchor:
    return RevisionAnchor(
        anchor_id=(
            f"p{paragraph_index:04d}-"
            + _sha256(
                f"{candidate_sha256}\x00{paragraph_index}\x00{text}"
            )[:16]
        ),
        paragraph_index=paragraph_index,
        start=start,
        end=end,
        text=text,
    )


def _bounded_localized_candidate_diff(
    *, contract: ProseRevisionContract, candidate_content: str
) -> tuple[tuple[RevisionAnchor, ...], set[int], set[int]]:
    """只物化 LOCALIZED 授权区及其相邻段，避免扫描超大未改尾文。

    LOCALIZED 候选已经由 :func:`apply_revision_response` 从冻结源机械合成；
    因而所有变化只能落在 target 授权锚点之间。这里利用该既有合同，按候选总
    长度反推出授权区末端，只为授权区和左右各一个保留段生成候选锚点。正常
    小说正文仍走完整段落 diff；本路径只保护极端 mock/防御性大输入。
    """

    source_anchor_by_id = {
        anchor.anchor_id: index
        for index, anchor in enumerate(contract.anchors)
    }
    authorized_indices = sorted(
        {
            source_anchor_by_id[anchor_id]
            for target in contract.targets
            for anchor_id in target.allowed_anchor_ids
        }
    )
    if not authorized_indices:
        raise ProseRevisionContractError("localized contract 没有授权段落")

    first_source_index = authorized_indices[0]
    last_source_index = authorized_indices[-1]
    first_source_anchor = contract.anchors[first_source_index]
    last_source_anchor = contract.anchors[last_source_index]
    suffix_length = len(contract.source_content) - last_source_anchor.end
    candidate_region_start = first_source_anchor.start
    candidate_region_end = len(candidate_content) - suffix_length
    if candidate_region_end <= candidate_region_start:
        raise ProseRevisionContractError(
            "localized 候选长度与冻结源授权区不兼容"
        )

    source_middle = [
        anchor.text
        for anchor in contract.anchors[
            first_source_index : last_source_index + 1
        ]
    ]
    candidate_region = candidate_content[
        candidate_region_start:candidate_region_end
    ]
    candidate_spans: list[tuple[int, int]] = []
    cursor = 0
    for separator in _PARAGRAPH_SEPARATOR.finditer(candidate_region):
        if candidate_region[cursor : separator.start()].strip():
            candidate_spans.append((cursor, separator.start()))
        cursor = separator.end()
    if candidate_region[cursor:].strip():
        candidate_spans.append((cursor, len(candidate_region)))
    candidate_middle = [
        candidate_region[start:end] for start, end in candidate_spans
    ]
    if not candidate_middle:
        raise ProseRevisionContractError("localized 候选授权区没有正文段落")
    if len(source_middle) * len(candidate_middle) > _PARAGRAPH_DIFF_MAX_CELLS:
        raise ProseRevisionContractError(
            "localized 授权区的段落差异面超过有界 source-bound 复核上限"
        )

    candidate_sha256 = _sha256(candidate_content)
    anchors: list[RevisionAnchor] = []
    unchanged: set[int] = set()
    if first_source_index > 0:
        left = contract.anchors[first_source_index - 1]
        anchors.append(
            _make_candidate_anchor(
                candidate_sha256=candidate_sha256,
                paragraph_index=left.paragraph_index,
                start=left.start,
                end=left.end,
                text=left.text,
            )
        )
        unchanged.add(0)

    middle_offset = len(anchors)
    for offset, (start, end) in enumerate(candidate_spans):
        text = candidate_region[start:end]
        anchors.append(
            _make_candidate_anchor(
                candidate_sha256=candidate_sha256,
                paragraph_index=first_source_anchor.paragraph_index + offset,
                start=candidate_region_start + start,
                end=candidate_region_start + end,
                text=text,
            )
        )

    changed: set[int] = set()
    matcher = SequenceMatcher(
        None,
        source_middle,
        candidate_middle,
        autojunk=False,
    )
    for tag, _a_start, _a_end, b_start, b_end in matcher.get_opcodes():
        target = unchanged if tag == "equal" else changed
        target.update(range(middle_offset + b_start, middle_offset + b_end))

    if last_source_index + 1 < len(contract.anchors):
        right = contract.anchors[last_source_index + 1]
        paragraph_delta = len(candidate_middle) - len(source_middle)
        character_delta = len(candidate_content) - len(contract.source_content)
        anchors.append(
            _make_candidate_anchor(
                candidate_sha256=candidate_sha256,
                paragraph_index=right.paragraph_index + paragraph_delta,
                start=right.start + character_delta,
                end=right.end + character_delta,
                text=right.text,
            )
        )
        unchanged.add(len(anchors) - 1)
    return tuple(anchors), changed, unchanged


def _candidate_paragraph_diff(
    *, contract: ProseRevisionContract, candidate_content: str
) -> tuple[tuple[RevisionAnchor, ...], set[int], set[int]]:
    """返回候选段落及相对冻结源的 changed/unchanged 0-based 索引。"""

    if not candidate_content.strip():
        raise ProseRevisionContractError("修订候选正文为空，无法核对 source-bound 变化")
    if (
        contract.mode == "localized"
        and max(len(contract.source_content), len(candidate_content))
        > _FULL_CANDIDATE_DIFF_MAX_CHARS
    ):
        return _bounded_localized_candidate_diff(
            contract=contract,
            candidate_content=candidate_content,
        )
    candidate_anchors = _build_anchors(candidate_content)
    source_texts = [anchor.text for anchor in contract.anchors]
    candidate_texts = [anchor.text for anchor in candidate_anchors]
    source_start = candidate_start = 0
    source_end = len(source_texts)
    candidate_end = len(candidate_texts)
    unchanged: set[int] = set()

    # LOCALIZED 机械合成通常只改一小块。先线性剥离公共前后缀，避免大量相同
    # mock/刻意排比段让 difflib 在全序列上进入平方级；这些逐位相同段可确定未改。
    while (
        source_start < source_end
        and candidate_start < candidate_end
        and source_texts[source_start] == candidate_texts[candidate_start]
    ):
        unchanged.add(candidate_start)
        source_start += 1
        candidate_start += 1
    while (
        source_start < source_end
        and candidate_start < candidate_end
        and source_texts[source_end - 1] == candidate_texts[candidate_end - 1]
    ):
        candidate_end -= 1
        source_end -= 1
        unchanged.add(candidate_end)

    source_middle = source_texts[source_start:source_end]
    candidate_middle = candidate_texts[candidate_start:candidate_end]
    if len(source_middle) * len(candidate_middle) > _PARAGRAPH_DIFF_MAX_CELLS:
        raise ProseRevisionContractError(
            "修订候选的段落差异面超过有界 source-bound 复核上限"
        )
    matcher = SequenceMatcher(
        None,
        source_middle,
        candidate_middle,
        autojunk=False,
    )
    changed: set[int] = set()
    for tag, _a_start, _a_end, b_start, b_end in matcher.get_opcodes():
        target = unchanged if tag == "equal" else changed
        target.update(
            range(candidate_start + b_start, candidate_start + b_end)
        )
    return candidate_anchors, changed, unchanged


def revision_evidence_is_source_bound(
    *,
    contract: ProseRevisionContract,
    candidate_content: str,
    evidence: str,
) -> bool:
    """判定逐字引文是否能唯一落入相对冻结源发生变化的候选段。

    这里按段落 diff，而不是只看新增字符，因此在同一段内删除旧错误、保留能证明
    完成动作的上下文时仍可通过。引文在全文重复时，只有恰好一个 occurrence 位于
    changed paragraph 才算可稳定定位。
    """

    quote = evidence.strip()
    if not quote or quote not in candidate_content:
        return False
    candidate_anchors, changed, _unchanged = _candidate_paragraph_diff(
        contract=contract,
        candidate_content=candidate_content,
    )
    changed_spans = [
        (candidate_anchors[index].start, candidate_anchors[index].end)
        for index in sorted(changed)
    ]
    occurrences: list[tuple[int, int]] = []
    cursor = 0
    while True:
        start = candidate_content.find(quote, cursor)
        if start < 0:
            break
        occurrences.append((start, start + len(quote)))
        cursor = start + 1
    source_bound = [
        occurrence
        for occurrence in occurrences
        if any(
            span_start <= occurrence[0] and occurrence[1] <= span_end
            for span_start, span_end in changed_spans
        )
    ]
    return len(source_bound) == 1


def _normalize_duplicate_text(value: str) -> str:
    normalized = unicodedata.normalize("NFKC", value).casefold()
    return "".join(
        char
        for char in normalized
        if not char.isspace()
        and not unicodedata.category(char).startswith(("P", "S"))
    )


def _duplicate_match(
    changed: str, neighbor: str
) -> tuple[AdjacentDuplicateMatchKind, float, float] | None:
    changed_normalized = _normalize_duplicate_text(changed)
    neighbor_normalized = _normalize_duplicate_text(neighbor)
    shorter_length = min(len(changed_normalized), len(neighbor_normalized))
    if shorter_length < _ADJACENT_DUPLICATE_MIN_CHARS:
        return None
    if changed == neighbor:
        return "exact", 1.0, 1.0
    if changed_normalized == neighbor_normalized:
        return "normalized_equal", 1.0, 1.0
    if (
        changed_normalized in neighbor_normalized
        or neighbor_normalized in changed_normalized
    ):
        ratio = (2 * shorter_length) / (
            len(changed_normalized) + len(neighbor_normalized)
        )
        return "contained", ratio, 1.0

    matcher = SequenceMatcher(
        None, changed_normalized, neighbor_normalized, autojunk=False
    )
    matched = sum(block.size for block in matcher.get_matching_blocks())
    ratio = matcher.ratio()
    shorter_coverage = matched / shorter_length
    if (
        ratio >= _ADJACENT_HIGH_OVERLAP_RATIO
        and shorter_coverage >= _ADJACENT_HIGH_OVERLAP_SHORTER_COVERAGE
    ):
        return "high_overlap", ratio, shorter_coverage
    return None


def find_localized_adjacent_duplicates(
    *, contract: ProseRevisionContract, candidate_content: str
) -> list[LocalizedAdjacentDuplicate]:
    """只比较 LOCALIZED changed paragraph 与左右紧邻 unchanged paragraph。"""

    if contract.mode != "localized":
        return []
    candidate_anchors, changed, unchanged = _candidate_paragraph_diff(
        contract=contract,
        candidate_content=candidate_content,
    )
    findings: list[LocalizedAdjacentDuplicate] = []
    seen_pairs: set[tuple[int, int]] = set()
    for changed_index in sorted(changed):
        for neighbor_index in (changed_index - 1, changed_index + 1):
            if neighbor_index not in unchanged:
                continue
            pair = (changed_index, neighbor_index)
            if pair in seen_pairs:
                continue
            seen_pairs.add(pair)
            changed_anchor = candidate_anchors[changed_index]
            neighbor_anchor = candidate_anchors[neighbor_index]
            match = _duplicate_match(changed_anchor.text, neighbor_anchor.text)
            if match is None:
                continue
            match_kind, ratio, shorter_coverage = match
            findings.append(
                LocalizedAdjacentDuplicate(
                    changed_anchor_id=changed_anchor.anchor_id,
                    changed_text=changed_anchor.text,
                    neighbor_anchor_id=neighbor_anchor.anchor_id,
                    neighbor_text=neighbor_anchor.text,
                    match_kind=match_kind,
                    similarity_ratio=ratio,
                    shorter_coverage=shorter_coverage,
                )
            )
    return findings


def _validate_results_against_plan(
    *,
    executable_constraint_plan: ExecutableConstraintPlan,
    results: Sequence[ConstraintResult],
) -> dict[str, ExecutableConstraint]:
    planned: dict[str, ExecutableConstraint] = {}
    for result in results:
        try:
            executable_constraint_plan.assert_result_matches(result)
            planned[result.constraint_id] = (
                executable_constraint_plan.result_constraint(result.constraint_id)
            )
        except ExecutableConstraintPlanError as exc:
            raise ProseRevisionContractError(str(exc)) from exc
    return planned


def _validate_violations_against_plan(
    *,
    executable_constraint_plan: ExecutableConstraintPlan,
    violations: Sequence[ConstraintViolation],
) -> dict[str, ExecutableConstraint]:
    planned: dict[str, ExecutableConstraint] = {}
    for violation in violations:
        if violation.severity != "hard":
            continue
        try:
            item = executable_constraint_plan.hard_violation_constraint(
                violation
            )
        except ExecutableConstraintPlanError as exc:
            raise ProseRevisionContractError(str(exc)) from exc
        # setting child 使用自己的派生 ID，但修订目标仍必须回到计划中的聚合
        # setting 身份；确定性 violation 的二者本来就是同一个 ID。
        planned[item.constraint_id] = item
    return planned


def _global_failure_reasons(
    *,
    failed_items: dict[str, ExecutableConstraint],
    other_failures: Sequence[str],
    validator_failures: Sequence[dict[str, object]],
) -> list[str]:
    reasons = [
        f"constraint:{item.kind}:{constraint_id}"
        for constraint_id, item in failed_items.items()
        if item.kind in _GLOBAL_KINDS
    ]
    has_local_failure = any(item.kind in _LOCAL_KINDS for item in failed_items.values())
    for validator_failure in validator_failures:
        name = str(validator_failure.get("name") or "<unknown>").strip()
        normalized = name.casefold()
        if has_local_failure and any(marker in normalized for marker in _LOCAL_FAILURE_MARKERS):
            continue
        reasons.append(f"validator:{name}")
    for other_failure in other_failures:
        normalized = other_failure.strip().casefold()
        if not normalized:
            continue
        if any(marker in normalized for marker in _GLOBAL_FAILURE_MARKERS):
            reasons.append(f"failure:{other_failure.strip()}")
            continue
        if has_local_failure and any(
            marker in normalized for marker in _LOCAL_FAILURE_MARKERS
        ):
            continue
        reasons.append(f"failure:{other_failure.strip()}")
    return list(dict.fromkeys(reasons))


def _unique_evidence_anchor(
    *,
    anchors: Sequence[RevisionAnchor],
    evidence_candidates: Sequence[str],
) -> RevisionAnchor | None:
    for evidence in evidence_candidates:
        candidate = evidence.strip()
        if not candidate:
            continue
        matches = [anchor for anchor in anchors if candidate in anchor.text]
        if len(matches) == 1:
            return matches[0]
    return None


def _make_local_target(
    *,
    item: ExecutableConstraint,
    result: ConstraintResult | None,
    violations: Sequence[ConstraintViolation],
    anchors: tuple[RevisionAnchor, ...],
) -> RevisionTarget:
    if item.kind not in _LOCAL_KINDS:
        raise ProseRevisionContractError(
            "HARD 类型尚未证明可安全局部化，已阻止无边界修订: "
            f"kind={item.kind} constraint_id={item.constraint_id}"
        )
    if item.kind in {"continuity_unfinished_actions", "continuity_scene_same"}:
        return RevisionTarget(
            target_id=item.constraint_id,
            source="constraint_result",
            instruction=item.rule_text,
            allowed_anchor_ids=(anchors[0].anchor_id,),
            allowed_operations=("insert_before",),
        )

    evidence = [
        violation.evidence_quote
        for violation in violations
        if violation.severity == "hard"
        and violation.constraint_id == item.constraint_id
    ]
    # forbidden 的机器规则本身就是确定性 substring validator 使用的原文；
    # 唯一命中时可直接作为稳定正文证据。setting 的规则文本不是违规正文，
    # 不得照搬此逻辑。
    if item.kind == "forbidden":
        evidence.append(item.rule_text)
    if result is not None and result.details:
        evidence.append(result.details)
    anchor = _unique_evidence_anchor(
        anchors=anchors,
        evidence_candidates=evidence,
    )
    if anchor is None:
        raise ProseRevisionContractError(
            "局部 HARD 缺少唯一正文证据锚点，已阻止静默全文重写: "
            f"kind={item.kind} constraint_id={item.constraint_id}"
        )
    return RevisionTarget(
        target_id=item.constraint_id,
        source=(
            "constraint_violation"
            if any(
                violation.constraint_id == item.constraint_id
                for violation in violations
            )
            else "constraint_result"
        ),
        instruction=item.rule_text,
        allowed_anchor_ids=(anchor.anchor_id,),
        allowed_operations=("replace",),
    )


def _new_contract(
    *,
    mode: RevisionMode,
    source_content: str,
    source_version_id: int,
    executable_constraint_plan: ExecutableConstraintPlan,
    targets: Sequence[RevisionTarget] = (),
    preserve_constraint_ids: Sequence[str] = (),
    global_reasons: Sequence[str] = (),
) -> ProseRevisionContract:
    return ProseRevisionContract(
        mode=mode,
        source_version_id=source_version_id,
        source_sha256=_sha256(source_content),
        plan_hash=executable_constraint_plan.plan_hash,
        source_content=source_content,
        anchors=_build_anchors(source_content),
        targets=tuple(targets),
        preserve_constraint_ids=tuple(dict.fromkeys(preserve_constraint_ids)),
        global_reasons=tuple(dict.fromkeys(global_reasons)),
    )


def build_rewrite_revision_contract(
    *,
    source_content: str,
    source_version_id: int,
    executable_constraint_plan: ExecutableConstraintPlan,
    must_fix: Sequence[ConstraintResult],
    must_preserve: Sequence[ConstraintResult],
    constraint_violations: Sequence[ConstraintViolation],
    other_failures: Sequence[str],
    validator_failures: Sequence[dict[str, object]],
) -> ProseRevisionContract:
    """为自动/显式 Writer rewrite 编译局部或显式全局合同。"""

    all_results = [*must_fix, *must_preserve]
    planned_results = _validate_results_against_plan(
        executable_constraint_plan=executable_constraint_plan,
        results=all_results,
    )
    planned_violations = _validate_violations_against_plan(
        executable_constraint_plan=executable_constraint_plan,
        violations=constraint_violations,
    )
    failed_items = {
        result.constraint_id: planned_results[result.constraint_id]
        for result in must_fix
    }
    failed_items.update(planned_violations)
    global_reasons = _global_failure_reasons(
        failed_items=failed_items,
        other_failures=other_failures,
        validator_failures=validator_failures,
    )
    preserve_ids = [result.constraint_id for result in must_preserve]
    if global_reasons:
        return _new_contract(
            mode="global",
            source_content=source_content,
            source_version_id=source_version_id,
            executable_constraint_plan=executable_constraint_plan,
            preserve_constraint_ids=preserve_ids,
            global_reasons=global_reasons,
        )

    anchors = _build_anchors(source_content)
    results_by_id = {result.constraint_id: result for result in must_fix}
    targets = [
        _make_local_target(
            item=item,
            result=results_by_id.get(constraint_id),
            violations=constraint_violations,
            anchors=anchors,
        )
        for constraint_id, item in failed_items.items()
    ]
    if not targets:
        return _new_contract(
            mode="global",
            source_content=source_content,
            source_version_id=source_version_id,
            executable_constraint_plan=executable_constraint_plan,
            preserve_constraint_ids=preserve_ids,
            global_reasons=("manual_or_unstructured_rewrite",),
        )
    return _new_contract(
        mode="localized",
        source_content=source_content,
        source_version_id=source_version_id,
        executable_constraint_plan=executable_constraint_plan,
        targets=targets,
        preserve_constraint_ids=preserve_ids,
    )


def _review_issue_target_id(issue: ReviewIssue) -> str:
    payload = json.dumps(
        issue.model_dump(mode="json"),
        ensure_ascii=False,
        sort_keys=True,
        separators=(",", ":"),
    )
    return _sha256("review_issue\n" + payload)


def _review_issue_anchor(
    *, issue: ReviewIssue, anchors: tuple[RevisionAnchor, ...]
) -> RevisionAnchor | None:
    """解析普通逐字 anchor，或本模块生成的稳定 paragraph anchor_id。"""

    by_id = {anchor.anchor_id: anchor for anchor in anchors}
    if issue.paragraph_anchor in by_id:
        return by_id[issue.paragraph_anchor]
    return _unique_evidence_anchor(
        anchors=anchors,
        evidence_candidates=(issue.paragraph_anchor,),
    )


def build_reviser_revision_contract(
    *,
    source_content: str,
    source_version_id: int,
    executable_constraint_plan: ExecutableConstraintPlan,
    issues: Sequence[ReviewIssue],
    validator_failures: Sequence[dict[str, object]],
    constraint_results: Sequence[ConstraintResult],
) -> ProseRevisionContract:
    """为 Orchestrator 与显式产品 Revise 编译同一份保序合同。"""

    planned_results = _validate_results_against_plan(
        executable_constraint_plan=executable_constraint_plan,
        results=constraint_results,
    )
    failed_items = {
        result.constraint_id: planned_results[result.constraint_id]
        for result in constraint_results
        if result.status == "fail"
    }
    global_reasons = _global_failure_reasons(
        failed_items=failed_items,
        other_failures=(),
        validator_failures=validator_failures,
    )
    preserve_ids = [
        result.constraint_id
        for result in constraint_results
        if result.status == "pass"
    ]
    if global_reasons:
        return _new_contract(
            mode="global",
            source_content=source_content,
            source_version_id=source_version_id,
            executable_constraint_plan=executable_constraint_plan,
            preserve_constraint_ids=preserve_ids,
            global_reasons=global_reasons,
        )

    anchors = _build_anchors(source_content)
    targets: list[RevisionTarget] = []
    for issue in issues:
        anchor = _review_issue_anchor(issue=issue, anchors=anchors)
        if anchor is None:
            raise ProseRevisionContractError(
                "ReviewIssue paragraph_anchor 未唯一命中源正文，已阻止模糊修订: "
                f"{issue.paragraph_anchor!r}"
            )
        targets.append(
            RevisionTarget(
                target_id=_review_issue_target_id(issue),
                source="review_issue",
                instruction=f"{issue.description}；{issue.suggestion}",
                allowed_anchor_ids=(anchor.anchor_id,),
                allowed_operations=("replace",),
            )
        )

    for constraint_id, item in failed_items.items():
        targets.append(
            _make_local_target(
                item=item,
                result=next(
                    result
                    for result in constraint_results
                    if result.constraint_id == constraint_id
                ),
                violations=(),
                anchors=anchors,
            )
        )
    if not targets:
        raise ProseRevisionContractError(
            "Revise 没有可执行的局部 target，也没有获准的全局失败原因"
        )
    return _new_contract(
        mode="localized",
        source_content=source_content,
        source_version_id=source_version_id,
        executable_constraint_plan=executable_constraint_plan,
        targets=targets,
        preserve_constraint_ids=preserve_ids,
    )


def render_revision_contract(contract: ProseRevisionContract) -> str:
    """把不可变合同与源正文渲染为动态 prompt 段。"""

    identity = {
        "contract_version": contract.contract_version,
        "mode": contract.mode,
        "source_version_id": contract.source_version_id,
        "source_sha256": contract.source_sha256,
        "plan_hash": contract.plan_hash,
        "preserve_constraint_ids": list(contract.preserve_constraint_ids),
        "global_reasons": list(contract.global_reasons),
        "targets": [target.model_dump(mode="json") for target in contract.targets],
    }
    identity_json = json.dumps(identity, ensure_ascii=False, indent=2)
    if contract.mode == "global":
        return (
            "## 正文修复执行合同（代码已显式授权 GLOBAL）\n\n"
            "以下身份已在请求前冻结；本轮确因字数、风格或整体结构问题允许整章"
            "修复。只输出修订后的完整正文，不要输出 JSON、解释或代码围栏。"
            "下游仍会重新验证全部上一轮 PASS HARD。**必须产生与源正文不同的实质"
            "修订；原样返回或只改空格、换行都会由代码拒绝，不会形成新版本。**\n\n"
            f"```json\n{identity_json}\n```\n\n"
            "### 冻结源正文（修订输入，不是可直接照抄的答案）\n\n"
            f"{contract.source_content}"
        )

    anchored_source = [
        {"anchor_id": anchor.anchor_id, "text": anchor.text}
        for anchor in contract.anchors
    ]
    source_json = json.dumps(anchored_source, ensure_ascii=False, indent=2)
    return (
        "## 正文修复执行合同（LOCALIZED PATCH；代码机械合成）\n\n"
        "只允许修改 targets 授权的 anchor/operation。每个 target_id 必须且只能"
        "出现一次；同一 anchor/operation 不得重复。不得返回整章正文。只输出一个"
        " JSON 对象（可用单个 json 代码围栏），字段必须严格如下：\n\n"
        "```json\n"
        "{\n"
        '  "contract_version": "1",\n'
        '  "mode": "localized",\n'
        f'  "source_version_id": {contract.source_version_id},\n'
        f'  "source_sha256": "{contract.source_sha256}",\n'
        f'  "plan_hash": "{contract.plan_hash}",\n'
        '  "patches": [{"target_ids": ["..."], "anchor_id": "...", '
        '"operation": "replace|insert_before|insert_after", '
        '"replacement": "修订后的一个段落块"}]\n'
        "}\n"
        "```\n\n"
        "### 冻结身份与允许范围\n\n"
        f"```json\n{identity_json}\n```\n\n"
        "### 带稳定锚点的源正文\n\n"
        f"```json\n{source_json}\n```"
    )


def _parse_localized_response(raw_content: str) -> LocalizedRevisionResponse:
    text = raw_content.strip()
    if text.startswith("```"):
        match = re.fullmatch(
            r"```(?:json)?[ \t]*\r?\n(?P<body>.*)\r?\n```",
            text,
            flags=re.DOTALL | re.IGNORECASE,
        )
        if match is None:
            raise ProseRevisionContractError("localized patch 代码围栏形状非法")
        text = match.group("body")
    try:
        payload = json.loads(text)
        return LocalizedRevisionResponse.model_validate(payload)
    except Exception as exc:  # noqa: BLE001 - Provider 响应必须统一 fail closed
        raise ProseRevisionContractError(
            "localized patch 不是严格合同 JSON"
        ) from exc


def apply_revision_response(
    *, contract: ProseRevisionContract, raw_content: str
) -> str:
    """验证 Provider 响应并从源正文确定性合成候选完整正文。"""

    if contract.mode == "global":
        if not raw_content.strip():
            raise ProseRevisionContractError("global revision 返回空正文")
        if prose_content_equivalent(contract.source_content, raw_content):
            raise ProseRevisionNoProgressError(
                "global revision 与冻结源正文无实质变化"
            )
        return raw_content

    response = _parse_localized_response(raw_content)
    observed_identity = (
        response.source_version_id,
        response.source_sha256,
        response.plan_hash,
    )
    expected_identity = (
        contract.source_version_id,
        contract.source_sha256,
        contract.plan_hash,
    )
    if observed_identity != expected_identity:
        raise ProseRevisionContractError(
            "localized patch 的源版本、source hash 或 plan hash 不匹配"
        )

    targets = {target.target_id: target for target in contract.targets}
    anchors = {anchor.anchor_id: anchor for anchor in contract.anchors}
    seen_targets: set[str] = set()
    substantively_changed_targets: set[str] = set()
    seen_patch_slots: set[tuple[str, PatchOperation]] = set()
    patches_by_anchor: dict[str, dict[PatchOperation, RevisionPatch]] = defaultdict(dict)
    for patch in response.patches:
        slot = (patch.anchor_id, patch.operation)
        if slot in seen_patch_slots:
            raise ProseRevisionContractError(
                "localized patch 重复修改同一 anchor/operation"
            )
        seen_patch_slots.add(slot)
        if patch.anchor_id not in anchors:
            raise ProseRevisionContractError("localized patch 引用了未知 anchor")
        anchor = anchors[patch.anchor_id]
        for target_id in patch.target_ids:
            target = targets.get(target_id)
            if target is None:
                raise ProseRevisionContractError(
                    "localized patch 引用了非目标 target_id"
                )
            if target_id in seen_targets:
                raise ProseRevisionContractError(
                    "localized patch 重复消费同一 target_id"
                )
            if (
                patch.anchor_id not in target.allowed_anchor_ids
                or patch.operation not in target.allowed_operations
            ):
                raise ProseRevisionContractError(
                    "localized patch 尝试修改 target 未授权的范围"
                )
            seen_targets.add(target_id)
        effective_anchor_text = (
            patch.replacement
            if patch.operation == "replace"
            else patch.replacement + anchor.text
            if patch.operation == "insert_before"
            else anchor.text + patch.replacement
        )
        if not prose_content_equivalent(anchor.text, effective_anchor_text):
            substantively_changed_targets.update(patch.target_ids)
        patches_by_anchor[patch.anchor_id][patch.operation] = patch

    if seen_targets != set(targets):
        missing = sorted(set(targets) - seen_targets)
        raise ProseRevisionContractError(
            "localized patch 未覆盖全部 target_id: " + "；".join(missing)
        )
    if substantively_changed_targets != set(targets):
        unchanged = sorted(set(targets) - substantively_changed_targets)
        raise ProseRevisionNoProgressError(
            "localized patch 未让每个授权 target 产生实质变化: "
            + "；".join(unchanged)
        )

    separator = "\r\n\r\n" if "\r\n" in contract.source_content else "\n\n"
    pieces: list[str] = []
    cursor = 0
    for anchor in contract.anchors:
        operations = patches_by_anchor.get(anchor.anchor_id, {})
        pieces.append(contract.source_content[cursor : anchor.start])
        before = operations.get("insert_before")
        if before is not None:
            pieces.extend((before.replacement, separator))
        replacement = operations.get("replace")
        pieces.append(replacement.replacement if replacement else anchor.text)
        after = operations.get("insert_after")
        if after is not None:
            pieces.extend((separator, after.replacement))
        cursor = anchor.end
    pieces.append(contract.source_content[cursor:])
    candidate = "".join(pieces)
    if prose_content_equivalent(contract.source_content, candidate):
        raise ProseRevisionNoProgressError(
            "localized patch 合成结果与冻结源正文无实质变化"
        )
    return candidate
