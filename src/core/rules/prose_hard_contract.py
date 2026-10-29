"""正文生产链共用的可执行 HARD 约束计划。

``ChapterCard``、权威世界合同、角色资料和 rewrite 基线仍是各自的数据
SSOT。本模块只做一次**机械编译**：把已经存在、且正文模型能够执行的 HARD
规则冻结为稳定 ID、来源、适用阶段、参数与计划哈希。它不判断文学语义，也不
引入新的 Agent / Provider 调用。

Writer、Reviewer、Rewriter、Reviser 与写后 validator 必须消费同一个
``ExecutableConstraintPlan``。写后裁决若无法回指计划中的 HARD 身份，应在进入
下一次正文请求前 fail closed，而不是临时发明一把新尺。
"""

from __future__ import annotations

import hashlib
import json
from typing import Final, Literal

from pydantic import BaseModel, ConfigDict, Field, model_validator

from src.core.models.chapter_card import ChapterCard, chapter_seq_from_id
from src.core.models.character import Character
from src.core.models.core_state import (
    CoreStateProjection,
    render_core_state_capsule,
    render_typed_book_time_constraint,
)
from src.core.models.review import (
    ConstraintResult,
    ConstraintViolation,
    ReviewResult,
    stable_constraint_id,
)
from src.core.models.setting_contract import SettingContract


#: 长篇正文全章段落均值硬底线。低于该值视作散文诗式 mode collapse，
#: draft / rewrite / revise 任一版本都会被 G22b 写后硬闸打回。
PARAGRAPH_MEAN_HARD_CONSTRAINT_ID: Final[str] = "paragraph_mean_floor"
PARAGRAPH_MEAN_HARD_MIN: Final[float] = 30.0

ProseConstraintStage = Literal["draft", "rewrite", "revise"]
ExecutableConstraintKind = Literal[
    "word_count",
    "paragraph_mean_floor",
    "must_achieve",
    "must_unresolved",
    "forbidden",
    "continuity_unfinished_actions",
    "continuity_scene_same",
    "book_clock",
    "core_state",
    "setting_unique_rules",
    "voice_forbidden",
    "cross_chapter_voice",
    "style_drift",
]
ConstraintResultDomain = Literal["must_achieve", "forbidden", "continuity", "setting"]
HardViolationDomain = Literal[
    "character.voice",
    "chapter.internal",
    "chapter.timeline",
    "chapter.style",
]
ConstraintParameterValue = str | int | float | bool | tuple[str, ...]
ConstraintProvenance = Literal[
    "chapter_card",
    "setting_contract",
    "character_record",
    "chapter_version",
    "workspace_config",
    "engine_rule",
    "core_state_ledger",
]

_DETERMINISTIC_HARD_VIOLATION_DOMAINS: dict[
    ExecutableConstraintKind, HardViolationDomain
] = {
    "word_count": "chapter.internal",
    "paragraph_mean_floor": "chapter.style",
    "must_unresolved": "chapter.internal",
    "voice_forbidden": "character.voice",
    "cross_chapter_voice": "chapter.style",
    "style_drift": "chapter.style",
    "core_state": "chapter.timeline",
}


class ExecutableConstraintPlanError(ValueError):
    """可执行计划无法安全编译或消费者身份发生漂移。"""


class ExecutableConstraintParameter(BaseModel):
    """约束的不可变机器参数；避免在消费者侧重新解析展示文案。"""

    model_config = ConfigDict(extra="forbid", frozen=True)

    name: str = Field(min_length=1)
    value: ConstraintParameterValue


class ExecutableConstraint(BaseModel):
    """一条已存在的正文 HARD 规则在生产链中的稳定身份。"""

    model_config = ConfigDict(extra="forbid", frozen=True)

    constraint_id: str = Field(pattern=r"^[0-9a-f]{64}$")
    severity: Literal["hard"] = "hard"
    kind: ExecutableConstraintKind
    provenance: ConstraintProvenance
    source_path: str = Field(min_length=1)
    source_hash: str = Field(default="", pattern=r"^(?:|[0-9a-f]{64})$")
    rule_text: str = Field(min_length=1)
    stages: tuple[ProseConstraintStage, ...] = Field(min_length=1)
    result_domain: ConstraintResultDomain | None = None
    parameters: tuple[ExecutableConstraintParameter, ...] = ()

    @model_validator(mode="after")
    def _validate_unique_parameters_and_stages(self) -> ExecutableConstraint:
        parameter_names = [item.name for item in self.parameters]
        if len(parameter_names) != len(set(parameter_names)):
            raise ValueError("同一约束的 parameter name 不得重复")
        if len(self.stages) != len(set(self.stages)):
            raise ValueError("同一约束的适用阶段不得重复")
        return self

    def parameter(self, name: str) -> ConstraintParameterValue:
        """按名字读取机器参数；缺失视为编译/消费者合同错误。"""

        for item in self.parameters:
            if item.name == name:
                return item.value
        raise ExecutableConstraintPlanError(
            f"约束 {self.constraint_id} 缺失参数 {name!r}"
        )

    def numeric_parameter(self, name: str) -> float:
        """读取数值参数并拒绝 bool/字符串等隐式转换。"""

        value = self.parameter(name)
        if isinstance(value, bool) or not isinstance(value, (int, float)):
            raise ExecutableConstraintPlanError(
                f"约束 {self.constraint_id} 参数 {name!r} 不是数值"
            )
        return float(value)

    def as_constraint_result(
        self, *, status: Literal["pass", "fail"], details: str = ""
    ) -> ConstraintResult:
        """把可产结构化结果的计划项原样映射成写后事实。"""

        if self.result_domain is None:
            raise ExecutableConstraintPlanError(
                f"约束 {self.constraint_id} 不产 ConstraintResult"
            )
        return ConstraintResult(
            constraint_id=self.constraint_id,
            domain=self.result_domain,
            source_path=self.source_path,
            source_hash=self.source_hash,
            rule_text=self.rule_text,
            status=status,
            details=details,
        )

    def as_hard_violation(
        self,
        *,
        evidence_quote: str,
        source_chapter_id: str | None = None,
        source_final_version: int | None = None,
    ) -> ConstraintViolation:
        """把确定性写后失败绑定回 Writer 前冻结的精确 HARD 身份。"""

        domain = _DETERMINISTIC_HARD_VIOLATION_DOMAINS.get(self.kind)
        if domain is None:
            raise ExecutableConstraintPlanError(
                f"约束 {self.constraint_id} 不产确定性 HARD violation"
            )
        return ConstraintViolation(
            constraint_id=self.constraint_id,
            domain=domain,
            source_path=self.source_path,
            source_hash=self.source_hash,
            rule_quote=self.rule_text,
            evidence_quote=evidence_quote,
            severity="hard",
            source_chapter_id=source_chapter_id,
            source_final_version=source_final_version,
        )


class RewriteStyleConstraintSpec(BaseModel):
    """由既有 G17/G5 逻辑解析出的 rewrite-only HARD 维度。

    ``None`` 表示该维没有可靠 v1 基线；``*_hard=False`` 表示既有 G17 已把
    异常维降为 WARN。该模型只承载已有判据，不重新解释它们。
    """

    model_config = ConfigDict(extra="forbid", frozen=True)

    paragraph_baseline: float | None = None
    paragraph_threshold: float
    paragraph_hard: bool = False
    sentence_baseline: float | None = None
    sentence_threshold: float
    sentence_hard: bool = False


class CrossChapterVoiceConstraintSpec(BaseModel):
    """由既有 G10/G12 解析出的跨章人称 HARD 精确合同。"""

    model_config = ConfigDict(extra="forbid", frozen=True)

    hard_enabled: bool
    provenance: Literal["chapter_version", "workspace_config"]
    source_path: str = Field(min_length=1)
    source_hash: str = Field(pattern=r"^[0-9a-f]{64}$")
    rule_text: str = Field(min_length=1)
    parameters: tuple[ExecutableConstraintParameter, ...] = ()


class ExecutableConstraintPlan(BaseModel):
    """一个正文版本从生成到写后裁决共用的不可变 HARD 计划。"""

    model_config = ConfigDict(extra="forbid", frozen=True)

    chapter_id: str = Field(min_length=1)
    stage: ProseConstraintStage
    constraints: tuple[ExecutableConstraint, ...]
    plan_hash: str = Field(pattern=r"^[0-9a-f]{64}$")
    narrative_policy_version: Literal[0, 1] = Field(0, exclude_if=lambda value: value == 0)
    word_count_policy_version: Literal[0, 1, 2] = Field(0, exclude_if=lambda value: value == 0)
    word_count_target: tuple[int, int] | None = Field(None, exclude_if=lambda value: value is None)

    @model_validator(mode="after")
    def _validate_identity_and_hash(self) -> ExecutableConstraintPlan:
        ids = [item.constraint_id for item in self.constraints]
        if len(ids) != len(set(ids)):
            raise ValueError("ExecutableConstraintPlan 存在重复 constraint_id")
        if any(self.stage not in item.stages for item in self.constraints):
            raise ValueError("计划包含当前阶段不适用的约束")
        expected = _plan_hash(
            chapter_id=self.chapter_id,
            stage=self.stage,
            constraints=self.constraints,
            narrative_policy_version=self.narrative_policy_version,
            word_count_policy_version=self.word_count_policy_version,
            word_count_target=self.word_count_target,
        )
        if self.plan_hash != expected:
            raise ValueError("ExecutableConstraintPlan plan_hash 不匹配内容")
        return self

    def by_kind(
        self, kind: ExecutableConstraintKind
    ) -> tuple[ExecutableConstraint, ...]:
        """按编译顺序返回同类规则。"""

        return tuple(item for item in self.constraints if item.kind == kind)

    def assert_matches_chapter_card(self, chapter_card: ChapterCard) -> None:
        """校验消费者没有把另一张卡与本计划拼接。"""

        if chapter_card.chapter_id != self.chapter_id:
            raise ExecutableConstraintPlanError(
                "ExecutableConstraintPlan chapter_id 与 ChapterCard 不一致"
            )
        if self.narrative_policy_version != int(chapter_card.narrative_context is not None):
            raise ExecutableConstraintPlanError("正文计划叙事版本与章节卡漂移")
        if self.word_count_policy_version != chapter_card.word_count_policy_version:
            raise ExecutableConstraintPlanError("篇幅规则版本与 ChapterCard 漂移")
        word_count = self.by_kind("word_count")
        if self.word_count_policy_version != 0:
            if word_count or self.word_count_target != (chapter_card.word_count_min, chapter_card.word_count_max):
                raise ExecutableConstraintPlanError("篇幅任务与 ChapterCard 漂移或错误升级为 HARD")
        elif self.word_count_target is not None or len(word_count) != 1 or (
            word_count[0].parameter("minimum") != chapter_card.word_count_min
            or word_count[0].parameter("maximum") != chapter_card.word_count_max
        ):
            raise ExecutableConstraintPlanError("word_count 计划与 ChapterCard 漂移")
        list_contracts = (
            ("must_achieve", chapter_card.must_achieve),
            ("must_unresolved", chapter_card.must_unresolved),
            ("forbidden", chapter_card.forbidden),
        )
        for kind, expected_rules in list_contracts:
            observed_rules = [item.rule_text for item in self.by_kind(kind)]  # type: ignore[arg-type]
            if observed_rules != list(expected_rules):
                raise ExecutableConstraintPlanError(f"{kind} 计划与 ChapterCard 漂移")

        continuity = chapter_card.continuity_context
        expected_unfinished = (
            ["必须承接上一章未完成动作：" + "；".join(continuity.required_actions)]
            if (chapter_seq_from_id(chapter_card.chapter_id) or 0) > 1
            and continuity.required_actions
            else []
        )
        observed_unfinished = [
            item.rule_text for item in self.by_kind("continuity_unfinished_actions")
        ]
        if observed_unfinished != expected_unfinished:
            raise ExecutableConstraintPlanError(
                "unfinished_actions 计划与 ChapterCard 漂移"
            )
        expected_scene_same = (
            [
                "scene_continuation=same：本章开头须保持上一章同一物理空间；"
                "卡片明确的梦境、幻境、回忆或倒叙桥接例外除外。"
            ]
            if (chapter_seq_from_id(chapter_card.chapter_id) or 0) > 1
            and continuity.scene_continuation == "same"
            else []
        )
        observed_scene_same = [
            item.rule_text for item in self.by_kind("continuity_scene_same")
        ]
        if observed_scene_same != expected_scene_same:
            raise ExecutableConstraintPlanError(
                "scene_continuation 计划与 ChapterCard 漂移"
            )
        observed_core = self.by_kind("core_state")
        observed_clock = self.by_kind("book_clock")
        if chapter_card.core_state_contract_version == 0:
            if observed_core or observed_clock:
                raise ExecutableConstraintPlanError(
                    "legacy v0 ChapterCard 不得声明 core_state/book_clock HARD"
                )
            return
        if len(observed_core) != 1:
            raise ExecutableConstraintPlanError(
                "core_state contract v1 必须且只能编译一条共用核心状态 HARD"
            )
        core_item = observed_core[0]
        if (
            core_item.source_hash != chapter_card.core_state_projection_hash
            or core_item.parameter("projection_hash")
            != chapter_card.core_state_projection_hash
            or core_item.parameter("transition_count")
            != len(chapter_card.core_state_transitions)
        ):
            raise ExecutableConstraintPlanError(
                "core_state 计划与 ChapterCard 冻结身份漂移"
            )
        clock = chapter_card.book_clock_constraint
        if clock is not None and clock.is_hard:
            if len(observed_clock) != 1:
                raise ExecutableConstraintPlanError(
                    "明确书内时钟必须且只能编译一条 book_clock HARD"
                )
            expected_rule = book_clock_rule_text(chapter_card)
            if observed_clock[0].rule_text != expected_rule:
                raise ExecutableConstraintPlanError(
                    "book_clock 计划与 ChapterCard 时钟区间漂移"
                )
            if clock.contract_version == 1 and (
                observed_clock[0].parameter("contract_version") != 1
                or observed_clock[0].parameter("kind") != clock.kind
                or observed_clock[0].parameter("relations") != tuple(clock.relations)
                or observed_clock[0].parameter("relation_chain_hash")
                != clock.relation_chain_hash
            ):
                raise ExecutableConstraintPlanError(
                    "book_clock typed 计划与 ChapterCard node/relation 漂移"
                )
        elif observed_clock:
            raise ExecutableConstraintPlanError(
                "含混/非主时间线 time_gap 不得编译 book_clock HARD"
            )

    def assert_matches_core_state(
        self,
        *,
        chapter_card: ChapterCard,
        projection: CoreStateProjection | None,
    ) -> None:
        """校验 Writer/Judge 使用的是出卡时同一份投影，而非另查一套记忆。"""

        self.assert_matches_chapter_card(chapter_card)
        if chapter_card.core_state_contract_version == 0:
            if projection is not None:
                raise ExecutableConstraintPlanError(
                    "legacy v0 卡片不得额外注入核心状态投影"
                )
            return
        if projection is None:
            raise ExecutableConstraintPlanError(
                "core_state contract v1 缺少 Writer/Judge 共用投影"
            )
        if projection.projection_hash != chapter_card.core_state_projection_hash:
            raise ExecutableConstraintPlanError(
                "共用核心状态投影与 ChapterCard projection_hash 漂移"
            )
        render_core_state_capsule(
            projection=projection,
            transitions=chapter_card.core_state_transitions,
            book_clock_constraint=chapter_card.book_clock_constraint,
        )

    def assert_matches_setting_contract(
        self, setting_contract: SettingContract | None
    ) -> None:
        """校验 Judge 没有换用另一份世界合同。"""

        observed = self.by_kind("setting_unique_rules")
        if (
            setting_contract is not None
            and setting_contract.has_provenance
            and setting_contract.unique_rules.strip()
        ):
            if len(observed) != 1:
                raise ExecutableConstraintPlanError("计划缺少权威 setting HARD")
            item = observed[0]
            expected = (
                setting_contract.source_path,
                setting_contract.source_hash,
                setting_contract.unique_rules.strip(),
            )
            if (item.source_path, item.source_hash, item.rule_text) != expected:
                raise ExecutableConstraintPlanError(
                    "setting 计划与权威 SettingContract 漂移"
                )
        elif observed:
            raise ExecutableConstraintPlanError(
                "计划声明了当前 SettingContract 不支持的 setting HARD"
            )

    def assert_matches_character_records(
        self,
        *,
        chapter_card: ChapterCard,
        character_records: list[Character] | None,
    ) -> None:
        """校验可归因禁语 Judge 与 Writer 前角色资料一致。"""

        related = set(chapter_card.related_characters)
        related.add(chapter_card.pov_character)
        expected: list[tuple[str, str]] = []
        for character in sorted(
            character_records or [], key=lambda item: item.character_id
        ):
            if character.character_id not in related:
                continue
            source_path = (
                f"characters/{character.character_id}.voice_profile.forbidden_phrases"
            )
            expected.extend(
                (source_path, f"{character.name} 不得说：{phrase}")
                for phrase in character.voice_profile.forbidden_phrases
            )
        observed = [
            (item.source_path, item.rule_text)
            for item in self.by_kind("voice_forbidden")
        ]
        if observed != expected:
            raise ExecutableConstraintPlanError(
                "voice_forbidden 计划与角色权威资料漂移"
            )

    def assert_matches_expected_voice(self, expected_voice: str | None) -> None:
        """兼容直调路径：校验简化人称目标与计划一致。"""

        observed = self.by_kind("cross_chapter_voice")
        if not expected_voice or expected_voice == "free":
            if observed:
                raise ExecutableConstraintPlanError(
                    "cross_chapter_voice 计划与当前 free/空人称目标漂移"
                )
            return
        if (
            len(observed) != 1
            or observed[0].parameter("expected_voice") != expected_voice
        ):
            raise ExecutableConstraintPlanError(
                "cross_chapter_voice 计划与写后人称目标漂移"
            )

    def assert_matches_cross_chapter_voice(
        self, spec: CrossChapterVoiceConstraintSpec
    ) -> None:
        """校验 G10/G12 没有在 Writer 后换基线、default 或阈值。"""

        observed = self.by_kind("cross_chapter_voice")
        if not spec.hard_enabled:
            if observed:
                raise ExecutableConstraintPlanError(
                    "cross_chapter_voice 计划与当前 skip/free 合同漂移"
                )
            return
        if len(observed) != 1:
            raise ExecutableConstraintPlanError(
                "cross_chapter_voice 计划必须且只能包含一条当前 HARD"
            )
        item = observed[0]
        if (
            item.provenance != spec.provenance
            or item.source_path != spec.source_path
            or item.source_hash != spec.source_hash
            or item.rule_text != spec.rule_text
            or item.parameters != spec.parameters
        ):
            raise ExecutableConstraintPlanError(
                "cross_chapter_voice 计划与写后 G10/G12 解析结果漂移"
            )

    def assert_matches_rewrite_style(
        self, rewrite_style: RewriteStyleConstraintSpec | None
    ) -> None:
        """校验 G17/G5 没有在 Writer 后重新解析出另一组 HARD 维度。"""

        expected: list[tuple[str, float, float]] = []
        if rewrite_style is not None and self.narrative_policy_version == 0:
            if (
                rewrite_style.paragraph_hard
                and rewrite_style.paragraph_baseline is not None
            ):
                expected.append(
                    (
                        "paragraph_length_mean",
                        rewrite_style.paragraph_baseline,
                        rewrite_style.paragraph_threshold,
                    )
                )
            if (
                rewrite_style.sentence_hard
                and rewrite_style.sentence_baseline is not None
            ):
                expected.append(
                    (
                        "sentence_length_mean",
                        rewrite_style.sentence_baseline,
                        rewrite_style.sentence_threshold,
                    )
                )
        observed = [
            (
                str(item.parameter("dimension")),
                item.numeric_parameter("baseline"),
                item.numeric_parameter("threshold"),
            )
            for item in self.by_kind("style_drift")
        ]
        if observed != expected:
            raise ExecutableConstraintPlanError(
                "style_drift 计划与写后 G17/G5 解析结果漂移"
            )

    def assert_style_failure_dimensions(self, dimensions: set[str]) -> None:
        """拒绝 G5 把计划中未声明的风格维度升成 HARD。"""

        planned = {
            str(item.parameter("dimension")) for item in self.by_kind("style_drift")
        }
        unknown = sorted(dimensions - planned)
        if unknown:
            raise ExecutableConstraintPlanError(
                "style_drift 写后新增计划外 HARD 维度: " + "；".join(unknown)
            )

    def result_constraint(self, constraint_id: str) -> ExecutableConstraint:
        """查找 Judge 可产 ``ConstraintResult`` 的精确计划身份。"""

        item = self.constraint(constraint_id)
        if item.result_domain is not None:
            return item
        raise ExecutableConstraintPlanError(
            f"Judge 产出未在计划中声明的 HARD constraint_id={constraint_id}"
        )

    def constraint(self, constraint_id: str) -> ExecutableConstraint:
        """按稳定 ID 查找任意计划项；不存在时 fail closed。"""

        for item in self.constraints:
            if item.constraint_id == constraint_id:
                return item
        raise ExecutableConstraintPlanError(
            f"写后裁决产出未在计划中声明的 HARD constraint_id={constraint_id}"
        )

    def hard_violation_constraint(
        self, violation: ConstraintViolation
    ) -> ExecutableConstraint:
        """验证 HARD violation 并返回它回指的冻结计划项。

        确定性 validator 使用计划项本身的 ID；setting Judge 仍可把权威
        ``unique_rules`` 中的精确子句物化为 child ID。两者都不得换来源、规则或
        hash。
        """

        exact = next(
            (
                item
                for item in self.constraints
                if item.constraint_id == violation.constraint_id
            ),
            None,
        )
        if exact is not None:
            expected_domain = _DETERMINISTIC_HARD_VIOLATION_DOMAINS.get(exact.kind)
            if expected_domain is None:
                raise ExecutableConstraintPlanError(
                    "写后裁决把非确定性计划项伪装成 HARD constraint_violation: "
                    f"constraint_id={violation.constraint_id}"
                )
            observed = (
                violation.domain,
                violation.source_path,
                violation.source_hash,
                violation.rule_quote,
            )
            expected = (
                expected_domain,
                exact.source_path,
                exact.source_hash,
                exact.rule_text,
            )
            if observed != expected:
                raise ExecutableConstraintPlanError(
                    "确定性 HARD violation 与 Writer 前计划身份不一致: "
                    f"constraint_id={violation.constraint_id}"
                )
            return exact

        setting_rules = self.by_kind("setting_unique_rules")
        if len(setting_rules) != 1:
            raise ExecutableConstraintPlanError(
                "写后裁决产出计划外 HARD constraint_violation"
            )
        setting_rule = setting_rules[0]
        if (
            violation.constraint_id
            != stable_constraint_id(
                source_path=setting_rule.source_path,
                domain=violation.domain,
                rule_text=violation.rule_quote,
            )
            or violation.domain != "world.unique_rules"
            or violation.source_path != setting_rule.source_path
            or violation.source_hash != setting_rule.source_hash
            or violation.rule_quote not in setting_rule.rule_text
        ):
            raise ExecutableConstraintPlanError(
                "setting HARD violation 无法回指 Writer 前权威规则"
            )
        return setting_rule

    def assert_result_matches(self, result: ConstraintResult) -> None:
        """阻止 Judge 用已知 ID 搭配另一条来源或规则文本。"""

        planned = self.result_constraint(result.constraint_id)
        observed = (
            result.domain,
            result.source_path,
            result.source_hash,
            result.rule_text,
        )
        expected = (
            planned.result_domain,
            planned.source_path,
            planned.source_hash,
            planned.rule_text,
        )
        if observed != expected:
            raise ExecutableConstraintPlanError(
                "Judge HARD 身份与 Writer 前计划不一致: "
                f"constraint_id={result.constraint_id}"
            )

    def allows_failure_marker(self, marker: str) -> bool:
        """判断展示层失败名是否由本计划中的某条 HARD 支持。"""

        normalized = marker.strip()
        direct_kind_markers = {
            "word_count": "word_count",
            "must_unresolved": "must_unresolved",
            "forbidden": "forbidden",
            "voice_forbidden": "voice_forbidden",
            "style_drift": "style_drift",
            "cross_chapter_voice": "cross_chapter_voice",
            PARAGRAPH_MEAN_HARD_CONSTRAINT_ID: "paragraph_mean_floor",
        }
        direct_kind = direct_kind_markers.get(normalized)
        if direct_kind is not None:
            return bool(self.by_kind(direct_kind))  # type: ignore[arg-type]
        if normalized.startswith("must_achieve: "):
            rule = normalized.removeprefix("must_achieve: ").strip()
            return any(item.rule_text == rule for item in self.by_kind("must_achieve"))
        if normalized.startswith("continuity: "):
            name = normalized.removeprefix("continuity: ").strip()
            mapping = {
                "unfinished_actions": "continuity_unfinished_actions",
                "scene_same": "continuity_scene_same",
                "time_gap": "book_clock",
            }
            kind = mapping.get(name)
            return bool(kind and self.by_kind(kind))  # type: ignore[arg-type]
        if normalized.startswith("hard_constraint_regression:"):
            constraint_id = normalized.removeprefix(
                "hard_constraint_regression:"
            ).strip()
            try:
                self.result_constraint(constraint_id)
            except ExecutableConstraintPlanError:
                return False
            return True
        return False

    def assert_review_uses_only_planned_hard(self, review: ReviewResult) -> None:
        """在下一次正文调用前验证写后 HARD 全部可回指本计划。"""

        for result in review.constraint_results:
            self.assert_result_matches(result)
        hard_setting_violation_ids = {
            violation.constraint_id
            for violation in review.constraint_violations
            if violation.severity == "hard"
            and violation.domain in {"world.unique_rules", "chapter.timeline"}
        }

        def marker_allowed(marker: str) -> bool:
            if marker.startswith("setting:"):
                parts = marker.split(":", 2)
                return (
                    len(parts) == 3
                    and parts[1] in hard_setting_violation_ids
                    and bool(
                        self.by_kind("setting_unique_rules")
                        or self.by_kind("core_state")
                    )
                )
            return self.allows_failure_marker(marker)

        unknown_markers = [
            marker for marker in review.failed_constraints if not marker_allowed(marker)
        ]
        if unknown_markers:
            raise ExecutableConstraintPlanError(
                "写后裁决包含 Writer 前计划未声明的 HARD marker: "
                + "；".join(unknown_markers)
            )
        for violation in review.constraint_violations:
            if violation.severity != "hard":
                continue
            self.hard_violation_constraint(violation)


def paragraph_mean_hard_rule() -> str:
    """返回跨 prompt / validator 共用的可读通过条件。"""

    return f"paragraph_length_mean >= {PARAGRAPH_MEAN_HARD_MIN:g}"


def core_state_rule_text(
    *, chapter_card: ChapterCard, projection: CoreStateProjection
) -> str:
    """生成单一共享核心状态判尺；完整事实仍由有界胶囊承载。"""

    if (chapter_card.narrative_context is not None
            and chapter_card.continuity_context.immediate_action_indices is not None):
        relations = []
        for item in chapter_card.core_state_transitions:
            if item.from_value is None:
                description = f"本章应成立={item.to_value}；此前未登记，不推断此前不存在或本章新取得"
            elif item.from_value == item.to_value:
                description = f"继续保持={item.to_value}；无需另演交接"
            else:
                description = f"{item.from_value}→{item.to_value}"
            relations.append(f"{item.state_key}：{description}（{item.reason}）")
        return (
            "遵守当前核心状态；未获许可的当前客观值必须保持；本章状态要求="
            + ("；".join(relations) or "无")
            + "。真实变化按剧情呈现因果，登记和保持关系可自然体现于持有、使用或叙述；"
              "不要求专门宣告、证明对话或逐条复述。审核从自然正文提取证据。"
        )
    transitions = "；".join(
        f"{item.state_key}:{item.from_value or '(未建立)'}→{item.to_value}（{item.reason}）"
        for item in chapter_card.core_state_transitions
    ) or "无"
    projection_label = "当前核心状态" if chapter_card.narrative_context is not None else f"核心状态投影 {projection.projection_hash}"
    return (
        f"遵守{projection_label}；未获许可的当前客观值必须保持；"
        f"本章许可变化={transitions}；许可变化须有明确因果和正文逐字证据"
    )


def book_clock_rule_text(chapter_card: ChapterCard) -> str:
    """生成 continuity Q3 与计划编译共用的明确主时间线通过条件。"""

    clock = chapter_card.book_clock_constraint
    if clock is None or not clock.is_hard:
        raise ExecutableConstraintPlanError("当前卡片没有可编译的 HARD book clock")
    if clock.contract_version == 1:
        return (
            f"本章书内时间：{render_typed_book_time_constraint(clock)}；"
            "只有正文直接违反这些 frozen typed facts 或明确写出主线倒序才冲突；"
            "无法证明矛盾、未写分钟、对白、梦境、回忆、倒叙和插叙均不得判 HARD"
        )
    if clock.current_interval is None:
        raise ExecutableConstraintPlanError("legacy 卡片没有可编译的 HARD numeric clock")
    interval = clock.current_interval
    return (
        f"本章主时间线 time_gap={clock.time_gap}；章内主线距开篇必须落在 "
        f"{interval.minimum_minutes}—{interval.maximum_minutes} 分钟；"
        "对白、梦境、回忆、倒叙和插叙不改写主时间线"
    )


def compile_executable_constraint_plan(
    *,
    chapter_card: ChapterCard,
    stage: ProseConstraintStage,
    setting_contract: SettingContract | None = None,
    character_records: list[Character] | None = None,
    expected_voice: str | None = None,
    cross_chapter_voice: CrossChapterVoiceConstraintSpec | None = None,
    rewrite_style: RewriteStyleConstraintSpec | None = None,
    core_state_projection: CoreStateProjection | None = None,
) -> ExecutableConstraintPlan:
    """把已有正文 HARD 机械编译成一份无副作用、零 Provider 的计划。"""

    chapter_id = chapter_card.chapter_id
    card_root = f"chapter_cards/{chapter_id}"
    constraints: list[ExecutableConstraint] = []

    def add(
        *,
        kind: ExecutableConstraintKind,
        source_path: str,
        rule_text: str,
        stages: tuple[ProseConstraintStage, ...] = ("draft", "rewrite", "revise"),
        id_domain: str | None = None,
        result_domain: ConstraintResultDomain | None = None,
        provenance: ConstraintProvenance = "chapter_card",
        source_hash: str = "",
        parameters: tuple[tuple[str, ConstraintParameterValue], ...] = (),
    ) -> None:
        if stage not in stages:
            return
        constraints.append(
            ExecutableConstraint(
                constraint_id=stable_constraint_id(
                    source_path=source_path,
                    domain=id_domain or kind,
                    rule_text=rule_text,
                ),
                kind=kind,
                provenance=provenance,
                source_path=source_path,
                source_hash=source_hash,
                rule_text=rule_text,
                stages=stages,
                result_domain=result_domain,
                parameters=tuple(
                    ExecutableConstraintParameter(name=name, value=value)
                    for name, value in parameters
                ),
            )
        )

    if chapter_card.word_count_policy_version == 0:
        add(
            kind="word_count",
            source_path=f"{card_root}.word_count",
            rule_text=(
                f"{chapter_card.word_count_min} <= actual_word_count <= "
                f"{chapter_card.word_count_max}"
            ),
            parameters=(
                ("minimum", chapter_card.word_count_min),
                ("maximum", chapter_card.word_count_max),
            ),
        )
    if chapter_card.narrative_context is None:
        add(
            kind="paragraph_mean_floor",
            source_path="engine.prose_hard_contract.paragraph_mean_floor",
            rule_text=paragraph_mean_hard_rule(),
            provenance="engine_rule",
            parameters=(("minimum", PARAGRAPH_MEAN_HARD_MIN),),
        )

    for index, rule in enumerate(chapter_card.must_achieve):
        add(
            kind="must_achieve",
            source_path=f"{card_root}.must_achieve",
            rule_text=rule,
            id_domain="must_achieve",
            result_domain="must_achieve",
            parameters=(("index", index),),
        )
    for index, rule in enumerate(chapter_card.must_unresolved):
        add(
            kind="must_unresolved",
            source_path=f"{card_root}.must_unresolved",
            rule_text=rule,
            parameters=(("index", index),),
        )
    for index, rule in enumerate(chapter_card.forbidden):
        add(
            kind="forbidden",
            source_path=f"{card_root}.forbidden",
            rule_text=rule,
            id_domain="forbidden",
            result_domain="forbidden",
            parameters=(("index", index),),
        )

    if chapter_card.core_state_contract_version == 1:
        if core_state_projection is None:
            raise ExecutableConstraintPlanError(
                "core_state contract v1 编译时缺少当前投影"
            )
        if (
            core_state_projection.projection_hash
            != chapter_card.core_state_projection_hash
        ):
            raise ExecutableConstraintPlanError(
                "core_state 编译投影与 ChapterCard projection_hash 漂移"
            )
        render_core_state_capsule(
            projection=core_state_projection,
            transitions=chapter_card.core_state_transitions,
            book_clock_constraint=chapter_card.book_clock_constraint,
        )
        add(
            kind="core_state",
            source_path=f"{card_root}.core_state_contract",
            source_hash=core_state_projection.projection_hash,
            rule_text=core_state_rule_text(
                chapter_card=chapter_card,
                projection=core_state_projection,
            ),
            id_domain="setting",
            result_domain="setting",
            provenance="core_state_ledger",
            parameters=(
                ("projection_hash", core_state_projection.projection_hash),
                ("transition_count", len(chapter_card.core_state_transitions)),
            ),
        )
        clock = chapter_card.book_clock_constraint
        if clock is not None and clock.is_hard:
            parameters: list[tuple[str, ConstraintParameterValue]] = []
            if clock.contract_version == 1:
                assert clock.kind is not None
                parameters.extend(
                    [
                        ("contract_version", 1),
                        ("kind", clock.kind),
                        ("relations", tuple(clock.relations)),
                        ("day_phase", clock.day_phase or ""),
                        ("relation_chain_hash", clock.relation_chain_hash),
                    ]
                )
                if clock.gap_interval is not None:
                    parameters.extend(
                        [
                            ("explicit_minimum_minutes", clock.gap_interval.minimum_minutes),
                            ("explicit_maximum_minutes", clock.gap_interval.maximum_minutes),
                        ]
                    )
                if clock.approximate_quantity is not None:
                    parameters.append(
                        (
                            "source_approximate_quantity",
                            f"{clock.approximate_quantity.value} "
                            f"{clock.approximate_quantity.unit}",
                        )
                    )
            else:
                assert clock.current_interval is not None
                parameters.extend(
                    [
                        ("minimum_minutes", clock.current_interval.minimum_minutes),
                        ("maximum_minutes", clock.current_interval.maximum_minutes),
                    ]
                )
            add(
                kind="book_clock",
                source_path=f"{card_root}.book_clock_constraint",
                source_hash=core_state_projection.projection_hash,
                rule_text=book_clock_rule_text(chapter_card),
                id_domain="continuity",
                result_domain="continuity",
                provenance="core_state_ledger",
                parameters=tuple(parameters),
            )
    elif core_state_projection is not None:
        raise ExecutableConstraintPlanError(
            "legacy v0 ChapterCard 不得携带额外核心状态投影"
        )

    continuity = chapter_card.continuity_context
    if (chapter_seq_from_id(chapter_id) or 0) > 1 and continuity.required_actions:
        rule = "必须承接上一章未完成动作：" + "；".join(continuity.required_actions)
        add(
            kind="continuity_unfinished_actions",
            source_path=f"{card_root}.continuity_context.unfinished_actions",
            rule_text=rule,
            id_domain="continuity",
            result_domain="continuity",
            parameters=(("actions", tuple(continuity.required_actions)),),
        )
    if (
        chapter_seq_from_id(chapter_id) or 0
    ) > 1 and continuity.scene_continuation == "same":
        add(
            kind="continuity_scene_same",
            source_path=f"{card_root}.continuity_context.scene_continuation",
            rule_text=(
                "scene_continuation=same：本章开头须保持上一章同一物理空间；"
                "卡片明确的梦境、幻境、回忆或倒叙桥接例外除外。"
            ),
            id_domain="continuity",
            result_domain="continuity",
            parameters=(("scene_continuation", "same"),),
        )

    if (
        setting_contract is not None
        and setting_contract.has_provenance
        and setting_contract.unique_rules.strip()
    ):
        add(
            kind="setting_unique_rules",
            source_path=setting_contract.source_path,
            source_hash=setting_contract.source_hash,
            rule_text=setting_contract.unique_rules.strip(),
            id_domain="setting",
            result_domain="setting",
            provenance="setting_contract",
        )

    related = set(chapter_card.related_characters)
    related.add(chapter_card.pov_character)
    for character in sorted(
        character_records or [], key=lambda item: item.character_id
    ):
        if character.character_id not in related:
            continue
        for index, phrase in enumerate(character.voice_profile.forbidden_phrases):
            add(
                kind="voice_forbidden",
                source_path=(
                    f"characters/{character.character_id}.voice_profile.forbidden_phrases"
                ),
                rule_text=f"{character.name} 不得说：{phrase}",
                provenance="character_record",
                parameters=(
                    ("character_id", character.character_id),
                    ("phrase", phrase),
                    ("index", index),
                ),
            )

    if cross_chapter_voice is not None and expected_voice is not None:
        raise ExecutableConstraintPlanError(
            "expected_voice 与 cross_chapter_voice 不得同时提供"
        )
    if cross_chapter_voice is not None and cross_chapter_voice.hard_enabled:
        add(
            kind="cross_chapter_voice",
            source_path=cross_chapter_voice.source_path,
            source_hash=cross_chapter_voice.source_hash,
            rule_text=cross_chapter_voice.rule_text,
            provenance=cross_chapter_voice.provenance,
            parameters=tuple(
                (parameter.name, parameter.value)
                for parameter in cross_chapter_voice.parameters
            ),
        )
    elif cross_chapter_voice is None and expected_voice and expected_voice != "free":
        add(
            kind="cross_chapter_voice",
            source_path="runtime.prev_chapter_voice_summary",
            rule_text=(
                f"本章主导叙述人称须保持 {expected_voice}；pov_change 只记录跨章"
                "主视角角色变化，不构成人称或章内切换豁免。只有卡片 setting / "
                "mini plan 明确梦境、回忆、倒叙或 POV 切换时例外。"
            ),
            provenance="chapter_version",
            parameters=(("expected_voice", expected_voice),),
        )

    if stage in {"rewrite", "revise"} and rewrite_style is not None and chapter_card.narrative_context is None:
        if (
            rewrite_style.paragraph_hard
            and rewrite_style.paragraph_baseline is not None
        ):
            baseline = rewrite_style.paragraph_baseline
            threshold = rewrite_style.paragraph_threshold
            add(
                kind="style_drift",
                source_path=f"chapters/{chapter_id}/v1.style_fingerprint.paragraph_length_mean",
                rule_text=(
                    "健康 v1 paragraph_length_mean 同章 rewrite 保序："
                    f"{baseline * (1 - threshold):.1f} <= actual <= "
                    f"{baseline * (1 + threshold):.1f}"
                ),
                stages=("rewrite", "revise"),
                provenance="chapter_version",
                parameters=(
                    ("dimension", "paragraph_length_mean"),
                    ("baseline", baseline),
                    ("threshold", threshold),
                ),
            )
        if rewrite_style.sentence_hard and rewrite_style.sentence_baseline is not None:
            baseline = rewrite_style.sentence_baseline
            threshold = rewrite_style.sentence_threshold
            add(
                kind="style_drift",
                source_path=f"chapters/{chapter_id}/v1.style_fingerprint.sentence_length_mean",
                rule_text=(
                    "健康 v1 sentence_length_mean 同章 rewrite 保序："
                    f"{baseline * (1 - threshold):.1f} <= actual <= "
                    f"{baseline * (1 + threshold):.1f}；受控 word-count trim 例外仍按"
                    "既有规则降 WARN"
                ),
                stages=("rewrite", "revise"),
                provenance="chapter_version",
                parameters=(
                    ("dimension", "sentence_length_mean"),
                    ("baseline", baseline),
                    ("threshold", threshold),
                ),
            )

    try:
        frozen = tuple(constraints)
        return ExecutableConstraintPlan(
            chapter_id=chapter_id,
            stage=stage,
            constraints=frozen,
            narrative_policy_version=1 if chapter_card.narrative_context is not None else 0,
            word_count_policy_version=chapter_card.word_count_policy_version,
            word_count_target=((chapter_card.word_count_min, chapter_card.word_count_max)
                               if chapter_card.word_count_policy_version else None),
            plan_hash=_plan_hash(
                chapter_id=chapter_id,
                stage=stage,
                constraints=frozen,
                narrative_policy_version=int(chapter_card.narrative_context is not None),
                word_count_policy_version=chapter_card.word_count_policy_version,
                word_count_target=((chapter_card.word_count_min, chapter_card.word_count_max)
                                   if chapter_card.word_count_policy_version else None),
            ),
        )
    except ValueError as exc:
        raise ExecutableConstraintPlanError(
            f"无法编译 {chapter_id}/{stage} 可执行约束计划: {exc}"
        ) from exc


def render_executable_constraint_plan(plan: ExecutableConstraintPlan) -> str:
    """以相同顺序和完整 ID 渲染给所有正文生产消费者。"""

    lines = [
        "## 可执行 HARD 约束计划(EXECUTABLE CONSTRAINT PLAN)\n\n",
        f"- chapter_id:`{plan.chapter_id}`\n",
        f"- stage:`{plan.stage}`\n",
        f"- plan_hash:`{plan.plan_hash}`\n",
        "- 规则：只能执行下表 HARD；Judge 不得新增下表之外的 HARD。\n\n",
        "| constraint_id | severity | stages | provenance | kind | source | source_hash | HARD 通过条件 |\n",
        "|---|---|---|---|---|---|---|---|\n",
    ]
    for item in plan.constraints:
        rule = item.rule_text.replace("|", "｜").replace("\n", " ")
        source = item.source_path.replace("|", "｜")
        source_hash = item.source_hash or "-"
        stages = ",".join(item.stages)
        lines.append(
            f"| {item.constraint_id} | {item.severity} | {stages} | "
            f"{item.provenance} | {item.kind} | {source} | {source_hash} | {rule} |\n"
        )
    return "".join(lines).rstrip()


def _plan_hash(
    *,
    chapter_id: str,
    stage: ProseConstraintStage,
    constraints: tuple[ExecutableConstraint, ...],
    narrative_policy_version: int = 0,
    word_count_policy_version: int = 0,
    word_count_target: tuple[int, int] | None = None,
) -> str:
    payload = {
        "chapter_id": chapter_id,
        "stage": stage,
        "constraints": [item.model_dump(mode="json") for item in constraints],
        **({"narrative_policy_version": narrative_policy_version} if narrative_policy_version else {}),
        **({"word_count_policy_version": word_count_policy_version, "word_count_target": word_count_target}
           if word_count_policy_version else {}),
    }
    encoded = json.dumps(
        payload,
        ensure_ascii=False,
        sort_keys=True,
        separators=(",", ":"),
    ).encode("utf-8")
    return hashlib.sha256(encoded).hexdigest()
