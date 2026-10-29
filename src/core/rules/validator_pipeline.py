"""[HARD] validators 串联调度(P4-B-06 第一批 + P4-B-07 第二批)。

把 ``validators.py`` 节奏类(§4.1)+ 一致性类(§4.2)规则串成一个 pipeline,
Reviewer 跑完后由 Orchestrator 在 REVIEWING 分支调度,统一产出
``ValidatorPipelineReport``:

- 全部通过:emit ``validator_passed`` (DEBUG level) — 主流程沿 Reviewer ``verdict``
  原路径走;
- 任一失败:emit ``validator_failed`` (INFO level,字段含 ``chapter_id`` /
  ``validator_name`` / ``details``,对齐 OBSERVABILITY §2.4 字面)— Orchestrator
  覆盖 ``review.verdict = "rewrite"`` 后走原 ``_handle_verdict``,自动复用
  rewrite_count + 1 / budget 检查 / escalate 升人工三段逻辑。

P4-B-07 第二批写后 check（**v1.0.x Commit D 起衔接桥
§4.3 两条已从 pipeline 移除,改由 LLM-based ``continuity_validator`` agent
接管**)由 ``run_validator_pipeline`` 在以下条件被激活:

- ``known_character_ids`` 仅保留兼容；角色引用已移到 Writer 前 admission
- ``timeline_entries`` 非 None → 跑 ``check_timeline_monotonic``
- ``character_records`` 非 None → 跑 ``check_voice_forbidden``（可归属命中 HARD、
  歧义命中 WARN）+ ``check_profession_title_blacklist``(A3,WARN-only 粗筛)

向后兼容:P4-B-06 既有 caller 只传 ``chapter_text + chapter_card + workspace_id``
时,所有第二批 ORM 依赖 validator 默认"跳过"(None 入参)。
"""

from __future__ import annotations

import json
from collections.abc import Iterable
from dataclasses import dataclass, field
from typing import TYPE_CHECKING, Any, Literal

import structlog

from src.core.models.chapter_card import ChapterCard
from src.core.models.review import ConstraintViolation
from src.core.rules.cross_chapter_voice_validator import (
    check_cross_chapter_voice,
    resolve_cross_chapter_voice_constraint_spec,
)
from src.core.rules.prose_hard_contract import (
    ExecutableConstraint,
    ExecutableConstraintKind,
    ExecutableConstraintPlan,
    ExecutableConstraintPlanError,
)
from src.core.rules.style_drift_validator import (
    check_style_drift,
    resolve_rewrite_style_constraint_spec,
)
from src.core.rules.style_trend_validator import check_style_trend
from src.core.rules.warning_routing import build_soft_warning
from src.core.rules.validators import (
    LLMFallbackCheck,
    ValidatorResult,
    classify_word_count_acceptance,
    check_forbidden,
    check_must_unresolved,
    check_profession_title_blacklist,
    check_timeline_monotonic,
    check_voice_forbidden,
    check_word_count,
    word_count_acceptance_bounds,
)

# v1.0.x A2-02(2026-05-19 hot-fix wave 2):``check_must_achieve`` 字面 substring
# 比对器从 pipeline results 中移除。P7-B-06 已落地 LLM-based ``must_achieve_validator``
# agent(``src/core/agents/must_achieve_validator.py``),由 ``_apply_must_achieve_validator``
# 在 ``_apply_validator_pipeline`` 之后串行调度。两者并联跑导致:LLM 文学化输出对
# 字面比对必挂(luo2 ch-0001 v3 reviewer score 7.43 / 9240 字语义全覆盖 must_achieve
# 3 条但被字面 substring 比对挂 → verdict 覆盖 rewrite,高分文学化章节 100% 被
# 误拒)。检查 must_achieve 完全交给 LLM validator。``check_must_achieve`` 函数本身
# **保留**(``src/core/rules/validators.py``)作为 v2 LLM fallback 必传场景 stub。
#
# v1.0.x wave 3 Commit D(2026-05-20 hot-fix wave 3):同型扩展,§4.3 两条衔接桥
# 字面 substring 比对器从 pipeline results 中移除调用 —
# ``check_unfinished_actions_continuation`` + ``check_scene_continuation_location``。
# P7-B-06 + Commit D 已落地 LLM-based ``continuity_validator`` agent
# (``src/core/agents/continuity_validator.py``),由 ``_apply_continuity_validator``
# 在 ``_apply_must_achieve_validator`` 之后串行调度。两者并联跑导致:LLM
# continuity_extractor 输出"每条 ≤ 30 字概要短语"与 [HARD] 字面 substring 比对器
# 的契约错位(luo2 ch-0002 v2 reviewer score 6.85 verdict=revise 应走 REVISING
# 小修但被字面比对器翻成 rewrite)。两条函数本身**保留**(``validators.py``)作
# v2 LLM fallback 必传场景 stub。

if TYPE_CHECKING:
    from src.core.models.character import Character
    from src.core.models.timeline_entry import TimelineEntry

_log = structlog.get_logger(__name__)


@dataclass(frozen=True)
class ValidatorPipelineReport:
    """validator pipeline 的汇总输出。

    - ``passed``:所有 validator 全过 = True;任一失败 = False。
    - ``results``:每个 validator 的 ``ValidatorResult``,**失败的排在前面**便于
      消费方直接展示前 N 条问题。
    - ``failed_constraint_names``:失败 validator 名称列表(进 ReviewResult
      ``failed_constraints`` 字段,便于前端 / CLI 展示)。
    """

    passed: bool
    results: list[ValidatorResult] = field(default_factory=list)
    failed_constraint_names: list[str] = field(default_factory=list)
    hard_violations: list[ConstraintViolation] = field(default_factory=list)
    warning_signals: list[ConstraintViolation] = field(default_factory=list)


def _single_planned_constraint(
    plan: ExecutableConstraintPlan, kind: ExecutableConstraintKind
) -> ExecutableConstraint:
    items = plan.by_kind(kind)
    if len(items) != 1:
        raise ExecutableConstraintPlanError(
            f"{kind} 写后结果无法唯一回指 Writer 前计划"
        )
    return items[0]


def _canonical_failure_evidence(payload: object) -> str:
    """生成可跨版本精确比较的非空确定性失败证据。"""

    return json.dumps(
        payload,
        ensure_ascii=False,
        sort_keys=True,
        separators=(",", ":"),
    )


def build_deterministic_hard_violations(
    *,
    results: Iterable[ValidatorResult],
    executable_constraint_plan: ExecutableConstraintPlan,
    current_version_id: int | None,
) -> list[ConstraintViolation]:
    """把确定性 validator 失败逐条绑定为计划内 HARD observation。

    ``forbidden`` 已由 Orchestrator 映射成四域 ``ConstraintResult``，这里不重复
    造身份。其余确定性失败必须使用 Writer 前计划项本身的稳定 ID，并把精确、
    canonical 的机器证据写入既有 ``constraint_violations`` JSON 字段。
    """

    failed = {result.name: result for result in results if not result.passed}
    violations: list[ConstraintViolation] = []

    def append(item: ExecutableConstraint, payload: object) -> None:
        violations.append(
            item.as_hard_violation(
                evidence_quote=_canonical_failure_evidence(payload),
                source_chapter_id=executable_constraint_plan.chapter_id,
                source_final_version=current_version_id,
            )
        )

    word_count = failed.get("word_count")
    if word_count is not None:
        append(
            _single_planned_constraint(executable_constraint_plan, "word_count"),
            dict(word_count.details),
        )

    must_unresolved = failed.get("must_unresolved")
    if must_unresolved is not None:
        resolved = must_unresolved.details.get("items_resolved", [])
        if not isinstance(resolved, list) or any(
            not isinstance(item, str) for item in resolved
        ):
            raise ExecutableConstraintPlanError(
                "must_unresolved 失败证据不是字符串列表"
            )
        planned_by_rule = {
            item.rule_text: item
            for item in executable_constraint_plan.by_kind("must_unresolved")
        }
        unknown = [rule for rule in resolved if rule not in planned_by_rule]
        if unknown:
            raise ExecutableConstraintPlanError(
                "must_unresolved 写后失败无法回指 Writer 前计划: "
                + "；".join(unknown)
            )
        for rule in resolved:
            append(
                planned_by_rule[rule],
                {"item": rule, "status": "resolved"},
            )

    voice_forbidden = failed.get("voice_forbidden")
    if voice_forbidden is not None:
        hits = voice_forbidden.details.get("attributable_hits", [])
        if not isinstance(hits, list) or any(not isinstance(hit, dict) for hit in hits):
            raise ExecutableConstraintPlanError(
                "voice_forbidden 失败证据不是对象列表"
            )
        planned_voice = executable_constraint_plan.by_kind("voice_forbidden")
        for hit in hits:
            character_id = str(hit.get("character_id", ""))
            phrase = str(hit.get("phrase", ""))
            matches = [
                item
                for item in planned_voice
                if item.parameter("character_id") == character_id
                and item.parameter("phrase") == phrase
            ]
            if len(matches) != 1:
                raise ExecutableConstraintPlanError(
                    "voice_forbidden 写后失败无法唯一回指 Writer 前计划"
                )
            append(matches[0], hit)

    style_drift = failed.get("style_drift")
    if style_drift is not None:
        dimensions = style_drift.details.get("drifted_dimensions", [])
        if not isinstance(dimensions, list) or any(
            not isinstance(item, dict) for item in dimensions
        ):
            raise ExecutableConstraintPlanError(
                "style_drift 失败证据不是对象列表"
            )
        planned_style = executable_constraint_plan.by_kind("style_drift")
        for dimension in dimensions:
            dimension_name = str(dimension.get("dimension", ""))
            matches = [
                item
                for item in planned_style
                if item.parameter("dimension") == dimension_name
            ]
            if len(matches) != 1:
                raise ExecutableConstraintPlanError(
                    "style_drift 写后失败无法唯一回指 Writer 前计划"
                )
            append(matches[0], dimension)

    cross_chapter_voice = failed.get("cross_chapter_voice")
    if cross_chapter_voice is not None:
        append(
            _single_planned_constraint(
                executable_constraint_plan, "cross_chapter_voice"
            ),
            dict(cross_chapter_voice.details),
        )

    # 同一规则不应被同一 validator 重复物化；若未来输入重复，保留首次出现的
    # canonical 证据，避免 review JSON 与无进展比较出现重复身份。
    unique: dict[str, ConstraintViolation] = {}
    for item in violations:
        unique.setdefault(item.constraint_id, item)
    return list(unique.values())


def run_validator_pipeline(
    chapter_text: str,
    *,
    chapter_card: ChapterCard,
    workspace_id: str,
    must_unresolved_llm_fallback: LLMFallbackCheck | None = None,
    known_character_ids: Iterable[str] | None = None,
    timeline_entries: Iterable["TimelineEntry"] | None = None,
    character_records: Iterable["Character"] | None = None,
    baseline_style_fingerprint: dict[str, float] | None = None,
    current_style_fingerprint: dict[str, float] | None = None,
    baseline_version_id: int | None = None,
    current_version_id: int | None = None,
    cross_chapter_baseline_fingerprint: dict[str, float] | None = None,
    cross_chapter_baseline_chapter_id: str | None = None,
    cross_chapter_current_fingerprint: dict[str, float] | None = None,
    workspace_default_voice: Literal[
        "third_person", "first_person", "free"
    ] = "third_person",
    mini_plan_event: str | None = None,
    is_first_chapter: bool = False,
    style_trend_history: list[tuple[str, dict[str, float] | None]] | None = None,
    current_word_count_trimmed: bool = False,
    executable_constraint_plan: ExecutableConstraintPlan | None = None,
    allow_word_count_tolerance: bool = True,
) -> ValidatorPipelineReport:
    """跑节奏类 + 一致性类 + 衔接类 [HARD] validator 并 emit structlog 事件。

    :param chapter_text: 待校验的章节正文。
    :param chapter_card: 章节卡;读 ``word_count_min`` / ``word_count_max`` /
        ``must_unresolved`` / ``forbidden`` / ``related_characters`` 字段
        (v1.0.x A2-02 起 ``must_achieve`` 字段交 ``must_achieve_validator``
        LLM agent 处理;v1.0.x wave 3 Commit D 起 ``continuity_context``
        交 ``continuity_validator`` LLM agent 处理;本 pipeline 均不再消费)。
    :param workspace_id: P3-B-14 透传纪律 — caller 显式透传当前工作区 ID,
        validator 事件字段不读 ``chapter_card.workspace_id``(模板复用时字段
        可能漂移)。
    :param must_unresolved_llm_fallback: 可选 LLM 兜底;默认 None 走 stub。
    :param known_character_ids: 兼容旧 caller 的预读集合。R9 起角色引用合法性已
        移到 Writer 前 admission，本写后 pipeline 不再消费该参数。
    :param timeline_entries: 当前章节及之前的 TimelineEntry 列表,调用方按
        ``time_order`` ASC 顺序传入;``None`` 时跳过 ``check_timeline_monotonic``。
    :param character_records: 已预读的 Character 完整记录列表;``None`` 时
        跳过 ``check_voice_forbidden`` 粗筛与职业称谓观察。已死亡角色引用已移到
        Writer 前 admission，不再让正文 rewrite 背数据问题。
    :param baseline_style_fingerprint: v1.1 G5 — 首版(v1)抽取的风格指纹;
        ``None`` 时跳过 ``check_style_drift``(向后兼容 / v1 自身审阅 /
        旧数据无指纹)。
    :param current_style_fingerprint: v1.1 G5 — 当前 vN 抽取的风格指纹;
        ``None`` 时跳过 ``check_style_drift``。**两者均非 None** 才触发检测。
    :param baseline_version_id: v1.1 G5 — 用于 style_drift details 字段标识。
    :param current_version_id: v1.1 G5 — 用于 style_drift details 字段标识。
    :param cross_chapter_baseline_fingerprint: v1.1 G10 — 前一章 final approved
        版本风格指纹;``None`` 时:**首章**(``is_first_chapter=True``)走
        default_voice 校验路径;**非首章**则 fail-open + warn(旧数据 / 异常)
    :param cross_chapter_baseline_chapter_id: v1.1 G10 — 前一章 chapter_id,
        用于 details 字段标识(便于 LogsPage 排查)
    :param cross_chapter_current_fingerprint: v1.1 G10 — 当前章 fingerprint;
        ``None`` 时跳过整个 cross_chapter_voice 检测(向后兼容旧路径)
    :param workspace_default_voice: v1.1 G10 — workspace ``default_voice``
        字段(``Workspace.default_voice`` 3 档);``'free'`` 时直接跳过 cross_chapter
        检测;默认 ``'third_person'``(项目级硬约束)
    :param mini_plan_event: v1.1 G10 — 当前章 mini_plan ``one_line_event`` 字面,
        用于例外条款豁免;首版 caller(orchestrator)透传 ``None``(走 chapter_card.setting
        豁免路径,wave 2+ 可扩 mini_plan 文件 IO)
    :param is_first_chapter: v1.1 G10 — 是否为工作区首章(用于 baseline=None
        时分叉 default_voice 校验路径 vs fail-open warn 路径)
    :param style_trend_history: v2 w2-s2 — **早于当前章**的 approved 章
        ``(chapter_id, final 指纹)`` 升序列表(caller 预读);与
        ``cross_chapter_current_fingerprint``(当前章 vN 指纹)组合触发
        ``check_style_trend`` 跨章慢漂检测。``None`` 时跳过(向后兼容旧
        caller)。**WARN-only**:失败只 warn log 留痕,不计入 pipeline
        失败、不派 rewrite(对齐 timeline_monotonic S3 / profession_title
        A3 同型 —— 趋势是全书级信号,单章 rewrite 治不了"近 3 章都重")。

    :return: ``ValidatorPipelineReport``。Orchestrator 消费 ``passed`` +
        ``failed_constraint_names`` 决定是否覆盖 ``review.verdict``。
    """
    if character_records is not None and not isinstance(character_records, list):
        character_records = list(character_records)

    word_count_min = chapter_card.word_count_min
    word_count_max = chapter_card.word_count_max
    must_unresolved = list(chapter_card.must_unresolved)
    forbidden = list(chapter_card.forbidden)
    if executable_constraint_plan is not None:
        executable_constraint_plan.assert_matches_chapter_card(chapter_card)
        executable_constraint_plan.assert_matches_character_records(
            chapter_card=chapter_card,
            character_records=character_records,
        )
        word_count_rules = executable_constraint_plan.by_kind("word_count")
        if chapter_card.word_count_policy_version == 0:
            if len(word_count_rules) != 1:
                raise ExecutableConstraintPlanError("历史计划必须且只能包含一条 word_count")
            minimum = word_count_rules[0].parameter("minimum")
            maximum = word_count_rules[0].parameter("maximum")
            if (not isinstance(minimum, int) or isinstance(minimum, bool)
                    or not isinstance(maximum, int) or isinstance(maximum, bool)):
                raise ExecutableConstraintPlanError("word_count 计划参数必须为整数")
            word_count_min = minimum
            word_count_max = maximum
        elif word_count_rules:
            raise ExecutableConstraintPlanError("新篇幅规则不得包含 word_count HARD")
        must_unresolved = [
            item.rule_text
            for item in executable_constraint_plan.by_kind("must_unresolved")
        ]
        forbidden = [
            item.rule_text for item in executable_constraint_plan.by_kind("forbidden")
        ]

    warning_signals: list[ConstraintViolation] = []
    results: list[ValidatorResult] = [
        # 节奏类(§4.1,第一批 3 类;v1.0.x A2-02 起 must_achieve 移交 LLM validator)
        check_must_unresolved(
            chapter_text,
            must_unresolved=must_unresolved,
            llm_fallback=must_unresolved_llm_fallback,
        ),
        check_forbidden(
            chapter_text,
            forbidden=forbidden,
        ),
        # v1.0.x wave 3 Commit D:衔接类(§4.3)两条字面 substring 比对器
        # (check_unfinished_actions_continuation + check_scene_continuation_location)
        # 已从 pipeline results 中**移除调用**(沿用 A2-02 同型处置)。函数本身
        # 保留 ``validators.py`` 作 v2 LLM fallback 必传场景 stub。衔接桥语义
        # 判定改由 LLM-based ``continuity_validator`` agent 接管(orchestrator
        # ``_apply_continuity_validator`` 在 ``_apply_must_achieve_validator``
        # 之后串行调度,§4.3 全 3 条 = 2 HARD + 1 SOFT)。
    ]

    if chapter_card.word_count_policy_version == 0:
        word_count_result = check_word_count(
            chapter_text, word_count_min=word_count_min, word_count_max=word_count_max,
            allow_word_count_tolerance=allow_word_count_tolerance,
        )
        results.insert(0, word_count_result)
        if classify_word_count_acceptance(
            len(chapter_text), word_count_min=word_count_min, word_count_max=word_count_max,
            allow_word_count_tolerance=allow_word_count_tolerance,
        ) == "tolerated":
            acceptance_min, acceptance_max = word_count_acceptance_bounds(
                word_count_min=word_count_min, word_count_max=word_count_max,
                allow_word_count_tolerance=allow_word_count_tolerance,
            )
            _log.info("word_count_tolerance_accepted", workspace_id=workspace_id,
                      chapter_id=chapter_card.chapter_id, actual_word_count=len(chapter_text),
                      target_min=word_count_min, target_max=word_count_max,
                      acceptance_min=acceptance_min, acceptance_max=acceptance_max,
                      audience="maintainer")
    else:
        _log.info("word_count_observed", workspace_id=workspace_id,
                  chapter_id=chapter_card.chapter_id, actual_word_count=len(chapter_text),
                  target_min=word_count_min, target_max=word_count_max, policy_version=1,
                  audience="maintainer")

    # 一致性类(§4.2,第二批,需要 caller 预读 ORM 数据 — 缺则跳过)。角色
    # existence / dead reference 已移到 Writer 前 admission；写后不再重复执行。
    _ = known_character_ids
    if timeline_entries is not None:
        # S3(v1.1 w4 b1):timeline_monotonic 在正文 review 流程**降级为 warn-only 瞭望哨**。
        # time_order 是已落库的记忆数据,正文 rewrite 改不动它 → 让它触发 rewrite 是错配
        # (数据问题让正文背锅 → 确定性 rewrite 空转 → human_intervention,ch-8 现场)。
        # time_order 正确性已由生产端 ``memory_extractor._persist_timeline`` 用全局章序
        # 硬注入根治(碰撞数学上不可能);此处失败仅 warn 留痕(防生产端将来回归),
        # **不计入 pipeline 失败、不派 rewrite**。passed 时正常计入(validator_passed 汇总)。
        _tl_result = check_timeline_monotonic(timeline_entries=timeline_entries)
        if _tl_result.passed:
            results.append(_tl_result)
        else:
            # 既有语义本来就是 WARN-only；R9 只把原先日志中的证据同步写入
            # 结构化 SOFT 观察，仍不改变 verdict 或 failed_constraints。
            warning_signals.append(
                build_soft_warning(
                    domain="chapter.timeline",
                    source_path="runtime.validator.timeline_monotonic",
                    rule_text="已落库主时间线 time_order 应严格递增",
                    evidence=str(dict(_tl_result.details)),
                    audiences=["maintainer"],
                    scope="maintenance",
                    confidence="high",
                    suggested_action="检查并修复时间线数据；不要重写正文。",
                    explanation="时间线数据顺序异常，正文作者无法通过改写修复。",
                )
            )
            _log.warning(
                "timeline_monotonic_warn_only",
                workspace_id=workspace_id,
                chapter_id=chapter_card.chapter_id,
                details=dict(_tl_result.details),
                note="S3 降级:time_order 由生产端全局基数保证;warn 不触发正文 rewrite",
            )
    if character_records is not None:
        records_list = list(character_records)
        _voice_result = check_voice_forbidden(
            chapter_text,
            chapter_card=chapter_card,
            character_records=records_list,
        )
        results.append(_voice_result)
        ambiguous_voice_hits = _voice_result.details.get("ambiguous_hits", [])
        if ambiguous_voice_hits:
            # 全文候选无法证明由对应角色说出。角色声音档案已在 Writer 上游
            # 明确注入；只有可归属命中才由上面的 result 进入 HARD。
            warning_signals.append(
                build_soft_warning(
                    domain="character.voice",
                    source_path="runtime.validator.voice_forbidden_attribution_unknown",
                    rule_text="角色禁语只有在说话人归属可验证时才构成 HARD",
                    evidence=str(ambiguous_voice_hits),
                    audiences=["writer", "maintainer"],
                    scope="next_chapter",
                    confidence="low",
                    suggested_action="下一章留意角色口吻；维护者可复核本章命中语句的说话人。",
                    explanation="当前全文子串粗筛无法证明禁语由对应角色说出。",
                    ambiguity="说话人归属未知",
                )
            )
            _log.warning(
                "voice_forbidden_attribution_unknown_warn_only",
                workspace_id=workspace_id,
                chapter_id=chapter_card.chapter_id,
                details={"ambiguous_hits": ambiguous_voice_hits},
                note="全文命中无法归属说话人；WARN 不触发 rewrite",
            )
        # A3(v1.1 w6-s2):职业称谓黑名单粗筛 **WARN-only**(对齐 timeline_monotonic
        # 同型降级)。粗筛可能误报(同段两角色职业被正常提及)→ 失败只 log warn 留痕,
        # **不计入 pipeline 失败、不派 rewrite**(真正治本靠 writer identity 注入 +
        # w6-s3 B1 设定一致性 LLM 校验)。passed 时正常计入 validator_passed 汇总。
        _prof_result = check_profession_title_blacklist(
            chapter_text,
            chapter_card=chapter_card,
            character_records=records_list,
        )
        if _prof_result.passed:
            results.append(_prof_result)
        else:
            # 保持既有 WARN-only 裁决，只补可持久化、可定向消费的路由元数据。
            warning_signals.append(
                build_soft_warning(
                    domain="character.profession",
                    source_path="runtime.validator.profession_title_blacklist",
                    rule_text="角色名邻接他角色专属职业词需要身份复核",
                    evidence=str(dict(_prof_result.details)),
                    audiences=["writer", "maintainer"],
                    scope="next_chapter",
                    confidence="low",
                    suggested_action="下一章保持角色身份称谓；维护者复核本章是否只是同场正常提及。",
                    explanation="邻接词粗筛可能误报，故仅作身份 watchlist。",
                )
            )
            _log.warning(
                "profession_title_blacklist_warn_only",
                workspace_id=workspace_id,
                chapter_id=chapter_card.chapter_id,
                details=dict(_prof_result.details),
                note="A3 粗筛:他角色职业词邻接本角色名,疑似身份写串;warn 不触发正文 rewrite",
            )

    # v1.1 wave 1 session 4(G5):风格漂移检测,仅在 baseline + current fingerprint
    # 都非 None 时触发(v1 自身审阅 / 旧数据无指纹 / 向后兼容路径跳过)。失败属
    # heavy(对齐 classify_validator_severity 严格判定 — 非 word_count 失败一律
    # heavy → 派 rewrite,reviser 局部 patch 难补风格结构性问题)。
    if baseline_style_fingerprint is not None and current_style_fingerprint is not None:
        if executable_constraint_plan is not None:
            executable_constraint_plan.assert_matches_rewrite_style(
                resolve_rewrite_style_constraint_spec(
                    v1_fingerprint=baseline_style_fingerprint,
                    cross_chapter_baseline_fingerprint=(
                        cross_chapter_baseline_fingerprint
                    ),
                )
            )
        # v1.1 acceptance 修复1(2026-06-10):透传跨章基线给 G5 闸门 — v1 异常
        # 时闸门与 writer G17 锚定同步替换基线(详 check_style_drift docstring;
        # cross_chapter_baseline_fingerprint 本就是本函数入参,此前只喂 G10)。
        _style_result = check_style_drift(
            baseline_fingerprint=baseline_style_fingerprint,
            current_fingerprint=current_style_fingerprint,
            baseline_version_id=baseline_version_id or 1,
            current_version_id=current_version_id,
            cross_chapter_baseline_fingerprint=cross_chapter_baseline_fingerprint,
            cross_chapter_baseline_chapter_id=cross_chapter_baseline_chapter_id,
            # v2 W3.5-s7:current 被 word_count_trim 改写 → 句均维度降 warn-only
            # (trim 重写句长是 by-design,详 check_style_drift docstring §2.47)。
            current_word_count_trimmed=current_word_count_trimmed,
        )
        if chapter_card.narrative_context is not None:
            # 新合同只观察文学数值，不能为句长或段均调用 rewrite。
            _style_result = ValidatorResult(name="style_drift", passed=True, details={
                "warn_observed_dimensions": [
                    *_style_result.details.get("warn_observed_dimensions", []),
                    *_style_result.details.get("drifted_dimensions", []),
                ],
            })
        if executable_constraint_plan is not None and not _style_result.passed:
            executable_constraint_plan.assert_style_failure_dimensions(
                {
                    str(item.get("dimension", ""))
                    for item in _style_result.details.get("drifted_dimensions", [])
                    if isinstance(item, dict)
                }
            )
        results.append(_style_result)
        for observed in _style_result.details.get("warn_observed_dimensions", []):
            if not isinstance(observed, dict):
                continue
            dimension = str(observed.get("dimension", "unknown"))
            warning_signals.append(
                build_soft_warning(
                    domain="chapter.style",
                    source_path=f"runtime.validator.style_drift.warn.{dimension}",
                    rule_text=f"style_drift 次要观察维度 {dimension} 不参与 HARD verdict",
                    evidence=str(observed),
                    audiences=["writer"],
                    scope="next_chapter",
                    confidence="medium",
                    suggested_action="下一章起草时轻量校准，不为追数值牺牲本章戏剧需要。",
                    explanation="该维度偏移只作风格观察，不能触发当前或后续 rewrite。",
                )
            )

    # v1.1 wave 1 session 6(G10):跨章节视角 / 风格一致性 validator,仅在
    # ``cross_chapter_current_fingerprint`` 非 None 时触发(向后兼容旧 caller)。
    # baseline 为 None 时按 is_first_chapter 分叉(首章走 default_voice 校验,
    # 非首章 fail-open + warn)。失败 → heavy(派 rewrite,reviser 局部 patch
    # 修不到位 — POV 漂移是全章性问题)。
    if cross_chapter_current_fingerprint is not None:
        if executable_constraint_plan is not None:
            executable_constraint_plan.assert_matches_cross_chapter_voice(
                resolve_cross_chapter_voice_constraint_spec(
                    baseline_fingerprint=cross_chapter_baseline_fingerprint,
                    baseline_chapter_id=cross_chapter_baseline_chapter_id,
                    workspace_default_voice=workspace_default_voice,
                    chapter_card=chapter_card,
                    mini_plan_event=mini_plan_event,
                )
            )
        _voice_cross_result = check_cross_chapter_voice(
            baseline_fingerprint=cross_chapter_baseline_fingerprint,
            current_fingerprint=cross_chapter_current_fingerprint,
            workspace_default_voice=workspace_default_voice,
            chapter_card=chapter_card,
            mini_plan_event=mini_plan_event,
            is_first_chapter=is_first_chapter,
            baseline_chapter_id=cross_chapter_baseline_chapter_id,
            current_chapter_id=chapter_card.chapter_id,
        )
        results.append(_voice_cross_result)
        paragraph_observation = _voice_cross_result.details.get(
            "paragraph_drift_observed"
        )
        if isinstance(paragraph_observation, dict):
            warning_signals.append(
                build_soft_warning(
                    domain="chapter.style",
                    source_path="runtime.validator.cross_chapter_paragraph_drift",
                    rule_text="跨章段落均值偏移超过观察阈值仅记 WARN",
                    evidence=str(paragraph_observation),
                    audiences=["writer"],
                    scope="next_chapter",
                    confidence="medium",
                    suggested_action="下一章把已批准成品节奏当软参考；不要据此创建绝对段均硬区间。",
                    explanation="跨章场景差异可自然改变段落节奏，不能单独打回正文。",
                )
            )

    # v2 w2-s2:跨章风格趋势检测(全书锚慢漂层)**WARN-only**(对齐
    # timeline_monotonic S3 / profession_title A3 同型降级)。趋势报警是
    # "全书级慢漂"信号,单章 rewrite 无法治本(该章重写不解决"近 3 章都
    # 重"),HARD 阻断语义错位 + 误杀成本不对称(G10 教训)→ 失败只 warn
    # log 留痕,**不计入 pipeline 失败、不派 rewrite**;passed 时正常计入
    # validator_passed 汇总(details 含逐维度偏移 + 观察维度,供仪表盘)。
    # 当前章指纹复用 cross_chapter_current_fingerprint(同为本次审阅 vN 指纹)。
    if (
        style_trend_history is not None
        and cross_chapter_current_fingerprint is not None
    ):
        _trend_result = check_style_trend(
            chapter_fingerprints=style_trend_history,
            current_chapter_id=chapter_card.chapter_id,
            current_fingerprint=cross_chapter_current_fingerprint,
        )
        if _trend_result.passed:
            results.append(_trend_result)
        else:
            warning_signals.append(
                build_soft_warning(
                    domain="chapter.style",
                    source_path="runtime.validator.style_trend",
                    rule_text="连续多章同向偏离全书开篇风格锚需要观察",
                    evidence=str(dict(_trend_result.details)),
                    audiences=["planner", "writer"],
                    scope="next_chapter",
                    confidence="medium",
                    suggested_action="下一章规划和起草时轻量回看全书锚，不以单章重写追数值。",
                    explanation="这是全书慢漂信号，单章 rewrite 不是可靠修复手段。",
                )
            )
            _log.warning(
                "style_trend_warn_only",
                workspace_id=workspace_id,
                chapter_id=chapter_card.chapter_id,
                details=dict(_trend_result.details),
                note=(
                    "v2 w2-s2 慢漂层:连续多章同向超全书锚偏移带;"
                    "WARN 留痕不触发 rewrite(治本在注入端全书锚 + 人工审阅)"
                ),
            )

    failed_results = [r for r in results if not r.passed]
    passed_results = [r for r in results if r.passed]
    # 失败排前面,便于消费方直接看头几条问题。
    ordered_results = failed_results + passed_results

    failed_names = [r.name for r in failed_results]

    if failed_results:
        # OBSERVABILITY §2.4 字面 ``validator_failed`` — chapter_id /
        # validator_name / details 三字段必含;workspace_id 由 caller 透传(P3-B-14
        # 沉淀,不读 chapter_card.workspace_id)。多个 validator 同时失败时,
        # 每条单独 emit 一次,便于 LogsPage / metrics 按 validator_name 维度聚合。
        for r in failed_results:
            _log.info(
                "validator_failed",
                workspace_id=workspace_id,
                chapter_id=chapter_card.chapter_id,
                validator_name=r.name,
                details=dict(r.details),
            )
    else:
        # OBSERVABILITY §2.4 字面 ``validator_passed`` — DEBUG level + 单事件
        # 汇总(不为 4 个 validator 各 emit 一次,日志噪音过大)。
        _log.debug(
            "validator_passed",
            workspace_id=workspace_id,
            chapter_id=chapter_card.chapter_id,
            validators=[r.name for r in results],
        )

    hard_violations = (
        build_deterministic_hard_violations(
            results=ordered_results,
            executable_constraint_plan=executable_constraint_plan,
            current_version_id=current_version_id,
        )
        if executable_constraint_plan is not None
        else []
    )
    return ValidatorPipelineReport(
        passed=not failed_results,
        results=ordered_results,
        failed_constraint_names=failed_names,
        hard_violations=hard_violations,
        warning_signals=warning_signals,
    )


# v1.1 G8:轻量失败"字数偏差"上限比例,小于该比例 → reviser 可 patch(派 revise);
# ≥ 该比例 → patch 难补,派 rewrite。对齐 candidates §G8 实证语义。
#
# v1.1 audit G21 治本(2026-05-27 b1-rerun-deep-audit):阈值 5% → 10%。
# 原 5% 阈值过严 — luo-cloud-test 21 章 71 次 verdict 0 次 revise(reviser dead code in
# practice);luo-clean-v1 ch-5 v1 deficit=8.92% > 5% → 永走 rewrite 路径触发 G11 锚
# v1 collapsed fingerprint → 越改越偏撞 budget human_intervention。
# 10% 阈值覆盖 ch-5 类 8-15% deficit case → reviser 用 prompt 的 5 类增量手段
# (加场景层数 / 心理独白 / 角色互动 / 感官描写 / 伏笔铺垫)补字数,**避开 writer
# rewrite 整章 + 锚 v1 collapsed fingerprint 的 mode collapse 风险**。
# 选 10% 而非 15% 的理由:① ch-5 deficit 9% 已覆盖 ② 留 15-30% 极端 deficit 走 rewrite
# (deficit > 10% 多半是 writer 严重压缩输出,reviser 补 250+ 字风险高)③ 简单整数化阈值。
WORD_COUNT_LIGHT_DEVIATION_THRESHOLD = 0.10


def classify_validator_severity(
    report: ValidatorPipelineReport,
) -> Literal["light", "heavy"]:
    """v1.1 G8:按 validator 失败模式分级,决定 verdict 派 revise 还是 rewrite。

    判定规则(严格):

    - **light**:**恰好 1 条** validator 失败 + 失败 validator 名 = ``"word_count"``；
      ``above_max`` 无论超出比例都派 ``revise``，由已有 ``word_count_trim``
      做代码量字数 + 最多两刀的专用收缩；``below_min`` 仍仅在偏差比例 <
      ``WORD_COUNT_LIGHT_DEVIATION_THRESHOLD``(10%) 时派通用 Reviser 增补。
    - **heavy**:其余全部情况(多 validator 同时失败 / 失败 validator 非
      word_count / below_min 偏差 ≥ 10% / 等)。patch 难补,派 ``rewrite``
      (沿用原行为)。单独超上限不在此列，因为专用 trim 会在不收敛时自动转
      现有 rewrite。

    设计意图(对齐 v1.1 G8 candidates):

    - luo-cloud-test 21 章实证 71 次 reviewer verdict 0 次 revise → reviser
      agent 路径 dead code in practice。根因 = ``_apply_validator_pipeline``
      硬约束失败时**一律**覆盖 verdict=rewrite(即便只差 50 字)。
    - G8 原修法仅对**只差一点字数**的失败派通用 revise；R9 v4 证明同一套
      Reviser 在 4824→4737 后第二次逐字回显，检测虽正确但纠正不收敛。现把
      单独 ``above_max`` 接到既有专用 trim；每刀由代码量字数，仍超上限才
      自动补刀，不收敛再转 rewrite。
    - 多 validator 失败 → 严重程度无法仅由 word_count 单条断言,**保守
      派 rewrite**(避免误判)。

    :param report: pipeline 报告,**调用方应仅在 ``report.passed=False``
        时调用**;``passed=True`` 时 Orchestrator 不该走 verdict 覆盖路径。
    :return: ``"light"`` 或 ``"heavy"``。
    """
    failed_results = [r for r in report.results if not r.passed]
    if len(failed_results) != 1:
        return "heavy"
    only = failed_results[0]
    if only.name != "word_count":
        return "heavy"
    actual = only.details.get("actual_word_count")
    word_count_min = only.details.get("word_count_min")
    word_count_max = only.details.get("word_count_max")
    reason = only.details.get("reason")
    # details 字段缺失 → 数据层 bug,保守判 heavy。
    if (
        actual is None
        or word_count_min is None
        or word_count_max is None
        or reason not in {"below_min", "above_max"}
    ):
        return "heavy"
    if reason == "below_min" and word_count_min > 0:
        deficit_ratio = (word_count_min - actual) / word_count_min
        return (
            "light" if deficit_ratio < WORD_COUNT_LIGHT_DEVIATION_THRESHOLD else "heavy"
        )
    if reason == "above_max" and word_count_max > 0:
        # 上限纠偏不再依赖 LLM 自行估算字符数。只要它是唯一失败，就交已有
        # word_count_trim 用同一 len() 口径有界收缩；失败后仍会自动转 rewrite。
        return "light"
    return "heavy"


def merge_into_review_verdict_overrides(
    *,
    report: ValidatorPipelineReport,
    existing_failed_constraints: list[str],
    verdict: Literal["revise", "rewrite"] = "rewrite",
) -> dict[str, Any]:
    """根据 pipeline 报告构造 ``ReviewResult.model_copy(update=...)`` 的 update
    字典(集中此处供 Orchestrator 调用)。

    返回字段:
    - ``verdict``:由 caller 派(默认 ``"rewrite"`` 保 backward compat;v1.1 G8
      light 失败时由 caller 显式传 ``"revise"``)
    - ``hard_constraints_passed``:覆盖为 ``False``
    - ``failed_constraints``:Reviewer 自报失败约束 + validator 失败的去重并集
    - ``validator_failure_details``:v1.1 G8 — 失败 validator 明细列表
      (每条 = ``{name, details}``),供 Reviser 在 revise 路径局部 patch 用

    仅在 ``report.passed=False`` 时调用;``passed=True`` 时 Orchestrator 不该走此路径。
    """
    assert not report.passed, (
        "merge_into_review_verdict_overrides 仅在 validator 失败时调用"
    )
    merged_failed = list(
        dict.fromkeys(existing_failed_constraints + report.failed_constraint_names)
    )
    validator_failure_details: list[dict[str, Any]] = [
        {"name": r.name, "details": dict(r.details)}
        for r in report.results
        if not r.passed
    ]
    return {
        "verdict": verdict,
        "hard_constraints_passed": False,
        "failed_constraints": merged_failed,
        "validator_failure_details": validator_failure_details,
    }
