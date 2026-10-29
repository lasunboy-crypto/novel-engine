"""OutlineBuilder agent(P5-B-03 引入)。

职责:把用户**一句话设定**(``premise``)+ 卷数 / 章数 / 张力模板等参数串
行生成长篇大纲 4 段——``world`` / ``characters`` / ``plot`` /
``volume_plans``。每段是一次独立的 LLM 调用,对齐 API_CONTRACT §4.2 的
WebSocket stage 协议(决策 3=B 拍板"4 段独立 prompt 串行")。

设计要点
========

1. **4 段串行,阶段间依赖只读**:``build_world(premise)`` →
   ``build_characters(premise, world)`` → ``build_plot(premise, world,
   characters)`` → ``build_volume_plans(premise, world, characters, plot,
   total_volumes, chapters_per_volume, tension_template)``;上游产出作为
   下游 ``user message`` 的只读 context,不再重跑(对齐 p5_plan.md §6 字面)。

2. **依赖反转**:每个 ``build_*`` 函数只接 ``LLMClient`` 协议(基类),不
   import ``ClaudeClient`` / ``OllamaClient``。具体客户端实例由
   ``ModelRouter`` 注入(主路径)或 caller 显式传 ``client`` + ``model``
   兜底(legacy 路径,对齐 P3-B-06 ``resolve_routing`` 模式)。

3. **agent_routing 接入**:``router.route("outline_builder", ...)`` 走
   ``agent_routing.outline_builder`` 表(P5-plan-kickoff 决策 4=B 默认
   ``local_qwen_32k``);未配置时回退 ``default``。``writer`` 规则匹配
   (``is_arc_finale`` / ``tension_level >= 9``)对 OutlineBuilder 不适用 —
   按 router 实现"非 writer agent 直接走 ``agent_routing`` 表"(见
   ``src/core/routing/router.py:_select_writer_profile_id`` 反向逻辑)。

4. **JSON schema 严格校验,失败不自动降级**:LLM 输出剥代码块 +
   ``json.loads`` → Pydantic ``model_validate``;任一失败抛
   ``OutlineBuilderError``(含 ``stage`` / ``raw_output`` /
   ``model_used``)+ structlog ``outline_builder_failed`` 事件。**不**像
   reviewer P3-B-08 那样自动升级到 Claude;沿用 P3-B-03 接 Qwen 时 JSON
   失败不自动降级的先例(p5_plan.md §6 字面),caller(P5-B-04
   ``outline_service``)可在 UI 上手动 retry 单段。

5. **提示词模板与变量分离**:静态 system 文本来自
   ``prompts/outline_builder/<stage>.md``(目录 + 4 文件,反问引子 ②
   自决);动态 context(premise + 上游产出 + 参数)由 Python 拼到
   user 消息。

6. **agent role marker(P1-B-22 协议扩 4 子角色)**:user message 末尾
   追加 ``<!-- agent_role: outline_world|outline_characters|outline_plot|
   outline_volume_plans -->``。MockLLMClient 收到对应 marker 返各段固定
   JSON fixture(P3 mock 路径模式);真 LLM 视 HTML 注释为字面忽略。

不做什么
========

- 不实现 service / orchestration(留 P5-B-04 ``src/services/outline_service.py``)
- 不实现 outline:generate / regenerate-section / commit 端点(P5-B-04)
- 不实现 [HARD] 副本横跨校验(P5-B-04 ``src/core/rules/outline_validators.py``)
- 不实现 WebSocket stage 推送(P5-B-04)
- 不写文件 SSOT(``workspaces/{id}/data/outline/<stage>.json``,留 P5-B-04)
- 不实现真 Qwen 集成测(留 P5-acceptance ``-m integration`` marker)
- 不抽取嵌套字段(``character_arcs`` 引用上游 character_id 完整性)(留 P5-B-04
  ``outline:commit`` service 层做跨段引用完整性校验)
"""

from __future__ import annotations

from src.core.rules.narrative_handoff import mini_plan_review_design

import hashlib
import json
import re
import unicodedata
from copy import deepcopy
from collections.abc import Callable
from concurrent.futures import Future, ThreadPoolExecutor, as_completed
from contextvars import copy_context
from dataclasses import dataclass, replace
from pathlib import Path
from typing import TYPE_CHECKING, Any, Literal

import structlog
from pydantic import BaseModel, ConfigDict, Field, ValidationError

from src.config import settings
from src.core.agents.structured_handoff import HandoffError, handoff_user_text
from src.core.models.narrative import (
    PlanningScope, ExperiencedStoryIntentDesign, StoryIntent, StoryIntentDesign, UserNarrativeRequest,
)
from src.core.rules.narrative_handoff import reader_source_catalog
from src.core.agents._prompt import (
    append_role_marker,
    load_prompt_template,
    resolve_routing,
)
from src.core.agents._retry_feedback import (
    SchemaRetryFailure,
    SchemaRetryFailureFingerprint,
    SchemaRetryExhausted,
    format_validation_error_feedback,
    run_with_schema_retry,
)
from src.core.agents._structured_output import prepare_strict_json_output
from src.core.agents.story_authority_context import render_story_authority_context
from src.core.agents.creative_delta import (
    assemble_characters_delta,
    assemble_mini_plan_delta,
    assemble_plot_delta,
    assemble_volume_plans_delta_v4,
    assemble_volume_refine_delta,
    build_mini_plan_delta_schema_transform,
    mini_plan_reference_brief,
    build_volume_plans_delta_v5_schema_transform,
    CharactersCreativeDelta,
    CreativeDeltaAssemblyError,
    EXPERIMENTAL_PIPELINE_CONTRACT,
    FrozenAuthority,
    PlotCreativeDelta,
    ScopedPlotCreativeDelta,
    SingleVolumeMiniPlanDelta,
    NarrativeSingleVolumeMiniPlanDelta,
    SingleVolumeRefineDelta,
    uses_hy3_creative_delta,
    uses_experimental_pipeline,
    uses_hy3_draft_v4,
    VolumePlansCreativeDeltaV4,
    FactualVolumePlansDelta,
)
from src.core.agents._generation_checkpoint import GenerationCheckpoint, GenerationCheckpointError
from src.core.agents._name_lock import build_name_lock_header
from src.core.agents.setting_consistency_validator import (
    prewriting_blockers,
    SettingConsistencyTechnicalError,
    validate_setting_consistency,
)
from src.core.llm.base import LLMClient, LLMResponse, Message
from src.core.llm.cancel import LLMCancelledError
from src.core.llm.local_json_contract import LOCAL_JSON_CONTRACT_PARAM, local_contract_from_params
from src.core.timing import StageTimer, log_stage_timing
from src.core.models.outline import (
    CharacterOutlineItem,
    CharactersOutput,
    MiniPlanItem,
    MiniPlanOutput,
    PlotOutput,
    VolumeBeatPlan,
    VolumePlanItem,
    VolumePlansOutput,
    WorldOutput,
)
from src.core.models.model_calibration import (
    canonical_genre_identity,
    GenreCalibration,
)
from src.core.models.review import ConstraintViolation
from src.core.models.setting_contract import SettingContract
from src.core.rules.outline_action_state import (
    find_pending_signature_overlaps, find_repeated_completed_signatures,
)
from src.core.agents.beat_alignment_validator import (
    BeatAlignmentBatchTechnicalError,
    format_validation_failure_for_retry,
    validate_beat_alignment,
    validate_beat_alignment_batch,
)
from src.core.agents.mechanism_consistency_validator import (
    MechanismConsistencyTechnicalError,
    format_mechanism_failure_for_retry,
    validate_mechanism_consistency,
)
from src.core.agents.outline_consistency_validator import (
    apply_name_typo_fixes,
    format_outline_consistency_failure_for_retry,
    validate_outline_consistency,
    validate_outline_consistency_partitioned,
    validate_outline_stage_consistency,
)
from src.core.agents.character_backfill import (
    CharacterBackfillError,
    backfill_plot_referenced_characters,
)
from src.core.agents.identity_coverage_validator import (
    IdentityCoverageCandidate,
    validate_identity_coverage_batch,
)
from src.core.agents.thread_closure_validator import (
    ThreadClosureCandidate,
    validate_thread_closure_batch,
)
from src.core.models.volume import TensionTemplateName
from src.core.rules.outline_validators import (
    format_arc_span_failures_for_retry,
    OutlineValidationFailure,
    validate_arc_chapter_spans,
)
from src.core.rules.outline_contracts import (
    build_volume_plans_contract,
    collect_volume_object_source_failures,
    format_volume_object_source_failures_for_retry,
    format_volume_plans_contract_failures_for_retry,
    format_volume_plans_contract_for_prompt,
    validate_refined_volume_contract,
    validate_volume_plans_contract,
    VolumePlansContract,
    VolumePlansContractFailure,
)
from src.core.rules.drama_contracts import (
    DramaticSourceContractError,
    validate_volume_mini_plan_dramatic_sources,
)

if TYPE_CHECKING:
    from src.core.routing.router import ModelRouter


_log = structlog.get_logger(__name__)


# ---------------------------------------------------------------------------
# 常量与类型
# ---------------------------------------------------------------------------


OutlineStage = Literal["world", "characters", "plot", "volume_plans", "mini_plan"]
"""OutlineBuilder 5 段 stage 名(v1.0.x wave 4 session 1 起从 4 段扩到 5 段;
新增 ``mini_plan`` 章级事件分配,API_CONTRACT §4.2 WS stage 字面同步扩)。"""


_STAGE_PROMPT_FILENAMES: dict[OutlineStage, str] = {
    "world": "world.md",
    "characters": "characters.md",
    "plot": "plot.md",
    "volume_plans": "volume_plans.md",
    "mini_plan": "mini_plan.md",
}

_STAGE_PROMPT_SUBDIR = "outline_builder"
"""``settings.prompts_dir`` 下的 OutlineBuilder 子目录名(对齐目录 + 4 文件自决)。"""

_OUTLINE_AGENT_NAME = "outline_builder"
"""router.route(agent_name) 入参(对齐 ``agent_routing.outline_builder`` config 表)。"""


# ``resolve_routing`` 的 ``agent_name`` 入参细化到 stage:``outline_world`` /
# ``outline_characters`` / ``outline_plot`` / ``outline_volume_plans``。Router
# 实际查 ``agent_routing`` 表时,先回退到统一 key ``outline_builder``(对齐
# config.yaml + decision 4 字面);若 ``agent_routing`` 显式声明 stage 级 key
# 则按 stage 命中(未来 v2_wishlist 可拆 4 段不同 profile)。**当前 P5-B-03
# 不拆**,4 段统一走 ``outline_builder`` 兜底。
_OUTLINE_STAGE_ROLE_MARKERS: dict[OutlineStage, str] = {
    "world": "outline_world",
    "characters": "outline_characters",
    "plot": "outline_plot",
    "volume_plans": "outline_volume_plans",
    "mini_plan": "mini_plan_generator",
}


# ---- LLM 输出格式剥离 -------------------------------------------------------


_CODE_FENCE_RE = re.compile(
    r"^\s*```(?:json)?\s*\n(?P<body>.*?)\n```\s*$",
    re.DOTALL,
)
"""剥 ```json ... ``` 代码块包裹(沿用 P1-B-11 / P2-B-09 reviewer / extractor 模式)。"""

_TRAILING_HTML_COMMENT_RE = re.compile(
    r"\s*<!--.*?-->\s*$",
    re.DOTALL,
)
"""剥末尾 HTML 注释(防 LLM 复读 marker 到输出)。"""


def _strip_format_noise(text: str) -> str:
    """剥 trailing HTML 注释(可能多个)→ 代码块包裹。沿用 memory_extractor 顺序。"""
    while True:
        stripped = _TRAILING_HTML_COMMENT_RE.sub("", text)
        if stripped == text:
            break
        text = stripped
    match = _CODE_FENCE_RE.match(text)
    if match is not None:
        return match.group("body")
    return text


def _strip_characters_format_noise(text: str) -> str:
    """Only remove container trailing commas located by the strict parser.

    Other malformed input returns unchanged. No string, value or bracket is
    invented. The complete candidate must parse strictly; raw output stays
    archived and all downstream schema and authority checks still run.
    """
    original = _strip_format_noise(text)
    candidate = original
    for removed in range(65):
        try:
            json.loads(candidate)
        except json.JSONDecodeError as error:
            if removed == 64 or error.pos >= len(candidate):
                return original
            previous = error.pos - 1
            while previous >= 0 and candidate[previous] in " \t\r\n":
                previous -= 1
            if (candidate[error.pos] not in "}]" or previous < 0
                    or candidate[previous] != ","):
                return original
            candidate = candidate[:previous] + candidate[previous + 1:]
        else:
            if removed:
                _log.warning(
                    "outline_characters_trailing_comma_removed",
                    removed_count=removed, schema_validation_required=True,
                    repair_scope="strict_parser_located_container_trailing_comma_only",
                )
            return candidate
    return original


def _normalize_volume_plans_explicit_trailing_field_pair(data: Any) -> Any:
    """机械恢复卷规划列表中被错放的显式 ``tension_curve`` 键值对。

    Hy3 正式现场连续三次返回合法 JSON，但把卷对象本应为
    ``"tension_curve": [..]`` 的内容写成
    ``"arcs": [.., "tension_curve", [..]]``；后续同一正式 checkpoint 又
    返回 ``"volumes": [{卷对象}, "tension_curve", [..]]``。这里只在以下
    条件全部成立时搬运原响应中已逐字存在的键和值：

    - 根为字典且 ``volumes`` 为列表；
    - ``arcs`` 尾部形态：卷对象尚无该字段、尾两项严格为字面键与列表，且此前
      所有 arc 均为字典；或
    - ``volumes`` 尾部形态：尾两项严格为字面键与列表、此前所有项均为卷字典，
      且其中恰好一个卷缺该字段（否则不能判定归属）。

    任一条件不成立即原样返回，让既有 Pydantic/retry fail closed；不猜数值、
    不删模糊元素，也不放宽后续业务校验。
    """

    if not isinstance(data, dict) or not isinstance(data.get("volumes"), list):
        return data

    repaired: dict[str, Any] | None = None
    repair_scopes: list[str] = []
    raw_volumes = data["volumes"]
    if (
        len(raw_volumes) >= 3
        and raw_volumes[-2] == "tension_curve"
        and isinstance(raw_volumes[-1], list)
        and all(isinstance(item, dict) for item in raw_volumes[:-2])
    ):
        missing_indexes = [
            index
            for index, item in enumerate(raw_volumes[:-2])
            if "tension_curve" not in item
        ]
        if len(missing_indexes) == 1:
            repaired = deepcopy(data)
            repaired_volumes = repaired["volumes"]
            repaired_volumes[missing_indexes[0]]["tension_curve"] = repaired_volumes[-1]
            repaired["volumes"] = repaired_volumes[:-2]
            repair_scopes.append("volumes_trailing_tension_curve_key_and_list_only")

    working = repaired if repaired is not None else data
    repaired_indexes: list[int] = []
    for index, raw_volume in enumerate(working["volumes"]):
        if not isinstance(raw_volume, dict) or "tension_curve" in raw_volume:
            continue
        raw_arcs = raw_volume.get("arcs")
        if (
            not isinstance(raw_arcs, list)
            or len(raw_arcs) < 2
            or raw_arcs[-2] != "tension_curve"
            or not isinstance(raw_arcs[-1], list)
            or not all(isinstance(item, dict) for item in raw_arcs[:-2])
        ):
            continue
        if repaired is None:
            repaired = deepcopy(data)
        volume = repaired["volumes"][index]
        volume["arcs"] = volume["arcs"][:-2]
        volume["tension_curve"] = raw_arcs[-1]
        repaired_indexes.append(index)
    if repaired_indexes:
        repair_scopes.append("arcs_trailing_tension_curve_key_and_list_only")

    if repaired is None:
        return data
    _log.warning(
        "outline_volume_plans_explicit_field_pair_repaired",
        repair_scopes=repair_scopes,
        repaired_volume_indexes=repaired_indexes,
        schema_validation_required=True,
    )
    return repaired


# ---------------------------------------------------------------------------
# 异常类型
# ---------------------------------------------------------------------------


class MiniPlanSettingConflictError(ValueError):
    """单卷 mini plan 与权威世界规则发生结构化 HARD 冲突。"""

    code = "MINI_PLAN_SETTING_CONFLICT"
    failure_layer = "mini_plan"

    def __init__(
        self,
        *,
        volume_id: str,
        violations: list[ConstraintViolation],
        contract_hash: str,
        attempt: int,
    ) -> None:
        self.volume_id = volume_id
        self.violations = list(violations)
        self.contract_hash = contract_hash
        self.attempt = attempt
        ids = ", ".join(v.constraint_id for v in violations)
        details = "\n".join(
            f"- rule_quote=『{v.rule_quote}』\n  evidence_quote=『{v.evidence_quote}』"
            for v in violations
        )
        super().__init__(
            f"{self.code}:volume={volume_id} 与权威世界规则冲突 "
            f"(constraint_ids={ids})\n冲突明细:\n{details}\n"
            "请只重出本卷并移除上述直接互斥承诺；不得改写权威世界规则。"
        )


class PlotSettingConflictError(ValueError):
    """主线 plot 与权威世界规则发生结构化 HARD 冲突。"""

    code = "PLOT_SETTING_CONFLICT"
    failure_layer = "plot"

    def __init__(
        self,
        *,
        violations: list[ConstraintViolation],
        contract_hash: str,
        attempt: int,
    ) -> None:
        self.violations = list(violations)
        self.contract_hash = contract_hash
        self.attempt = attempt
        ids = ", ".join(v.constraint_id for v in violations)
        details = "\n".join(
            f"- rule_quote=『{v.rule_quote}』\n  evidence_quote=『{v.evidence_quote}』"
            for v in violations
        )
        super().__init__(
            f"{self.code}:主线 plot 与权威世界规则冲突 "
            f"(constraint_ids={ids})\n冲突明细:\n{details}\n"
            "请只重出主线并移除上述直接互斥承诺；不得改写权威世界规则。"
        )


VolumeSettingSourceStage = Literal["plot", "volume_plans", "ambiguous"]


@dataclass(frozen=True)
class VolumeSettingEvidenceAttribution:
    """一次 volume Setting HARD 的逐字来源；只活在当前 build retry 内。"""

    constraint_id: str
    canonical_rule: str
    canonical_evidence: str
    source_stage: VolumeSettingSourceStage
    upstream_field: str | None
    candidate_field: str | None


class VolumePlansContractError(ValueError):
    """Pydantic 通过后的 volume business contract 结构化失败。"""

    def __init__(self, failures: list[VolumePlansContractFailure]) -> None:
        self.failures = list(failures)
        super().__init__(format_volume_plans_contract_failures_for_retry(failures))


class VolumePlansSettingConflictError(ValueError):
    """卷规划 volume_plans 与权威世界规则发生结构化 HARD 冲突。"""

    code = "VOLUME_PLANS_SETTING_CONFLICT"
    failure_layer = "volume_plans"

    def __init__(
        self,
        *,
        violations: list[ConstraintViolation],
        contract_hash: str,
        attempt: int,
        attributions: list[VolumeSettingEvidenceAttribution],
    ) -> None:
        self.violations = list(violations)
        self.contract_hash = contract_hash
        self.attempt = attempt
        self.attributions = list(attributions)
        self.retryable_in_volume = all(
            item.source_stage != "ambiguous" and item.candidate_field is not None
            for item in self.attributions
        )
        fingerprint_payload = [
            {
                "constraint_id": item.constraint_id,
                "rule": item.canonical_rule,
                "evidence": item.canonical_evidence,
                "source_stage": item.source_stage,
                "upstream_field": item.upstream_field,
                "candidate_field": item.candidate_field,
            }
            for item in sorted(
                self.attributions,
                key=lambda value: (
                    value.constraint_id,
                    value.source_stage,
                    value.upstream_field or "",
                    value.candidate_field or "",
                    value.canonical_evidence,
                ),
            )
        ]
        self.failure_fingerprint = _canonical_sha256(fingerprint_payload)
        has_inherited = any(item.source_stage == "plot" for item in self.attributions)
        self.no_progress_message = (
            "同一 constraint/rule、canonical evidence 与 field/source 连续无进展；"
            "保留上游事件并停止购买同类 volume 响应。当前上游合同不可满足/需回 plot，"
            "本调用不会自动修改 plot/world/JSON/DB。"
            if has_inherited
            else (
                "同一 constraint/rule、canonical evidence 与 field/source 连续无进展；"
                "停止购买同类 volume 响应，不自动修改 plot/world/JSON/DB。"
            )
        )
        ids = ", ".join(v.constraint_id for v in violations)
        detail_lines: list[str] = []
        for violation, attribution in zip(
            self.violations, self.attributions, strict=True
        ):
            detail_lines.append(
                f"- rule_quote=『{violation.rule_quote}』\n"
                f"  evidence_quote=『{violation.evidence_quote}』\n"
                f"  source_stage={attribution.source_stage};"
                f" upstream_field={attribution.upstream_field or 'null'};"
                f" candidate_field={attribution.candidate_field or 'null'}"
            )
            if attribution.source_stage == "plot":
                if attribution.candidate_field is not None:
                    detail_lines.append(
                        "  保留上游事件，只调整 volume 时序/位置；"
                        "不得删除或改写 plot/world。"
                    )
                else:
                    detail_lines.append(
                        "  当前 volume 已无可调整信息；上游合同不可满足/需回 plot；"
                        "本调用 fail closed，不自动修改 plot/world/JSON/DB。"
                    )
            elif attribution.source_stage == "volume_plans":
                detail_lines.append(
                    "  冲突由当前 volume 新增；只修该候选字段，保留 plot/world。"
                )
            else:
                detail_lines.append(
                    "  evidence 无法逐字定位；不得猜来源，本调用 fail closed。"
                )
        details = "\n".join(detail_lines)
        super().__init__(
            f"{self.code}:卷规划 volume_plans 与权威世界规则冲突 "
            f"(constraint_ids={ids})\n冲突明细:\n{details}\n"
            "请按 source_stage 的责任边界修复；不得降低 Setting HARD。"
        )


class OutlineStageFactConflictError(ValueError):
    """A source-quoted contradiction introduced by the current outline stage."""

    def __init__(self, *, stage: str, result: Any) -> None:
        self.contradictions = list(result.contradictions)
        self.contradiction_details = list(result.contradiction_details)
        self.coverage_gaps = list(result.coverage_gaps)
        self.evidence = result.evidence
        detail = "；".join(self.contradictions[:3])
        quoted_feedback = json.dumps(self.contradiction_details, ensure_ascii=False)
        super().__init__(f"{stage} 与已给来源存在不能共存的事实：{detail}\n"
            f"审核引用的两端原文与理由：{quoted_feedback}\n"
            "只修当前生成段与来源不一致的事实，保留其他已成立的选择和因果；"
            "不能通过改写只读上游来掩盖冲突。")


class OutlineStageReviewTechnicalError(RuntimeError):
    """The stage reviewer did not yield a source-valid verdict; do not redraw."""


class OutlineBuilderError(ValueError):
    """OutlineBuilder JSON 解析 / schema 校验失败时抛出。

    保留 ``stage`` / ``raw_output`` / ``model_used`` 三件,便于 caller
    (P5-B-04 ``outline_service``)决定重试策略 + P7 调优时回查根因
    (对齐 ``MemoryExtractorError`` / ``ReviewerParseError`` 形态)。

    v2 W3.5 UI 缺口1:大纲一致性硬伤(outline_consistency_validator 判定的跨段
    名字/称谓矛盾、角色混淆)抛本错误时,额外带 ``contradictions`` /
    ``coverage_gaps`` / ``evidence`` 结构化字段,供 ``api.errors`` 翻译成
    ``OUTLINE_CONSISTENCY_CONFLICT`` 错误码 + 前端列出每条冲突 + 高亮涉及实体名。
    其余 raise 处(JSON/schema 失败)三字段默认 None,行为不变。
    """

    def __init__(
        self,
        message: str,
        *,
        stage: OutlineStage,
        raw_output: str,
        model_used: str,
        contradictions: list[str] | None = None,
        coverage_gaps: list[str] | None = None,
        evidence: str | None = None,
        violations: list[dict[str, Any]] | None = None,
        error_code: str | None = None,
        contract_hash: str | None = None,
        attempts: int | None = None,
        failure_fingerprint: str | None = None,
        retry_stop_reason: str | None = None,
        contradiction_details: list[dict[str, Any]] | None = None,
        repair_scope: str | None = None,
        candidate_id: str | None = None,
    ) -> None:
        super().__init__(message)
        self.stage = stage
        self.raw_output = raw_output
        self.model_used = model_used
        self.contradictions = contradictions
        self.coverage_gaps = coverage_gaps
        self.evidence = evidence
        self.violations = violations
        self.error_code = error_code
        self.contract_hash = contract_hash
        self.attempts = attempts
        self.failure_fingerprint = failure_fingerprint
        self.retry_stop_reason = retry_stop_reason
        self.contradiction_details = contradiction_details
        self.repair_scope = repair_scope
        self.candidate_id = candidate_id


# ---------------------------------------------------------------------------
# 内部辅助:统一调用 + JSON 解析
# ---------------------------------------------------------------------------


_TBaseModel = type[BaseModel]


@dataclass(frozen=True, slots=True)
class _OutlineDeltaSpec:
    """Experimental wire model and deterministic full-object join."""

    output_model: type[BaseModel]
    load_system_text: Callable[[], str]
    assemble_data: Callable[[Any], Any]
    schema_transform: Callable[[dict[str, Any]], None] | None = None
    load_user_text: Callable[[], str] | None = None
    load_scene_system_text: Callable[[], str] | None = None
    load_complete_system_text: Callable[[], str] | None = None
    load_complete_user_text: Callable[[], str] | None = None
    complete_schema_transform: Callable[[dict[str, Any]], None] | None = None


def _outline_timing_fields(
    timing_context: dict[str, Any] | None,
    *,
    stage: str,
    substage: str,
    **fields: Any,
) -> dict[str, Any]:
    return {
        **(timing_context or {}),
        "stage": stage,
        "substage": substage,
        **fields,
    }


def _emit_outline_skipped(
    timing_context: dict[str, Any] | None,
    *,
    stage: str,
    substage: str,
    reason: str,
    **fields: Any,
) -> None:
    if timing_context is None:
        return
    log_stage_timing(
        _log,
        "outline_stage_timing",
        status="skipped",
        reason=reason,
        **_outline_timing_fields(
            timing_context, stage=stage, substage=substage, **fields
        ),
    )


def _run_stage(
    *,
    stage: OutlineStage,
    system_text: str,
    user_text: str,
    output_model: _TBaseModel,
    router: ModelRouter | None,
    client: LLMClient | None,
    model: str | None,
    max_tokens: int,
    temperature: float,
    post_validate: Callable[[BaseModel], None] | None = None,
    schema_validation_feedback: Callable[[ValidationError, Any], str] | None = None,
    failure_fingerprint: (
        Callable[[SchemaRetryFailure], SchemaRetryFailureFingerprint | None] | None
    ) = None,
    non_retryable_exceptions: tuple[type[Exception], ...] = (),
    timing_context: dict[str, Any] | None = None,
    timing_fields: dict[str, Any] | None = None,
    delta_spec: _OutlineDeltaSpec | None = None,
    load_experimental_system_text: Callable[[], str] | None = None,
    handoff_data: dict[str, Any] | None = None,
    candidate_checkpoint: GenerationCheckpoint | None = None,
) -> BaseModel:
    """OutlineBuilder 5 段共享的"调 LLM + 解析 JSON + 校验 schema + retry"流水线。

    ``router=None`` 时走 legacy 路径(``client`` + ``model`` + ``max_tokens``
    + ``temperature`` 必传);否则走 ``ModelRouter`` 路径,生成参数从
    profile 取。

    v1.0.x wave 5 session 2 起接入 ``_retry_feedback.run_with_schema_retry``
    通用 helper(根治 qwen-novel 复杂 JSON schema 失败率 ~50% 问题):
    - 单段自动 retry ``settings.outline_builder_max_retry_attempts`` 次(默认 3)
    - 每次失败把 ``ValidationError`` / ``JSONDecodeError`` / ``post_validate`` 错
      转**中文反馈消息追加进下次 prompt**,让 LLM 自己改自己输出
    - structlog 事件 ``outline_builder_retry_attempt`` 含 attempt / reason /
      model_used,排障时可观测重试链
    - N 次仍失败 → 抛 ``OutlineBuilderError`` 含末次响应证据(行为兼容旧接口)

    :param post_validate: 可选业务规则校验 callback(例 "volumes 长度 ==
        total_volumes" / "mini_plan items refs 完整性"等 schema 之外的硬
        约束)。抛任意 ``Exception`` 视为 retry 触发条件,而非直接 raise —
        让 LLM 看到具体业务规则错并自行纠正。
    :param non_retryable_exceptions: post_validate 内已经自行耗尽技术重试的异常；
        立即向上抛出，但仍完成本 stage 的 failed timing。
    """
    resolved_client, resolved_model, complete_kwargs = resolve_routing(
        agent_name=_OUTLINE_AGENT_NAME,
        router=router,
        client=client,
        model=model,
        legacy_kwargs={"max_tokens": max_tokens, "temperature": temperature},
    )
    if delta_spec is not None and uses_hy3_creative_delta(resolved_client):
        use_delta = True
        wire_output_model = delta_spec.output_model
        selected_system_text = (
            delta_spec.load_complete_system_text()
            if uses_experimental_pipeline(resolved_client)
            and delta_spec.load_complete_system_text is not None
            else delta_spec.load_scene_system_text()
            if uses_hy3_draft_v4(resolved_client)
            and delta_spec.load_scene_system_text is not None
            else delta_spec.load_system_text()
        )
        selected_user_text = (
            delta_spec.load_complete_user_text()
            if uses_experimental_pipeline(resolved_client)
            and delta_spec.load_complete_user_text is not None
            else delta_spec.load_user_text()
            if delta_spec.load_user_text is not None
            else user_text
        )
    else:
        use_delta = False
        wire_output_model = output_model
        selected_system_text = system_text
        selected_user_text = user_text
    if uses_experimental_pipeline(resolved_client) and load_experimental_system_text is not None:
        selected_system_text = load_experimental_system_text()
    if handoff_data:
        world_data = handoff_data.get("world")
        intent_data = handoff_data.get("story_intent") or (world_data.get("story_intent") if isinstance(world_data, dict) else getattr(world_data, "story_intent", None))
        authoring_data = intent_data.get("authoring_plan") if isinstance(intent_data, dict) else getattr(intent_data, "authoring_plan", None)
        if authoring_data is not None:
            selected_system_text += "\n\n" + _load_delta_stage_prompt("authoring_authority_v1.md", None)
    if getattr(resolved_client, "handoff_format", None) is not None and handoff_data is None:
        raise HandoffError(f"{stage} 缺少结构化资料接线")
    selected_user_text = handoff_user_text(
        resolved_client,
        stage="volume_refine" if handoff_data and "original_volume" in handoff_data else stage,
        data=handoff_data or {}, original_task=selected_user_text,
    )
    complete_kwargs = prepare_strict_json_output(
        client=resolved_client,
        model=resolved_model,
        output_model=wire_output_model,
        schema_name=f"outline_{stage}",
        complete_kwargs=complete_kwargs,
        transform=(
            delta_spec.complete_schema_transform
            if use_delta and delta_spec is not None
            and uses_experimental_pipeline(resolved_client)
            and delta_spec.complete_schema_transform is not None
            else
            delta_spec.schema_transform
            if use_delta and delta_spec is not None
            else None
        ),
    )

    fingerprint_parameters = dict(complete_kwargs)
    from src.core.agents.authoring_cache import completed_stage_key, load_completed_stage, store_completed_stage
    local_contract = local_contract_from_params(complete_kwargs)
    if local_contract is not None:
        local_contract.assert_unchanged()
        fingerprint_parameters[LOCAL_JSON_CONTRACT_PARAM] = {
            "name": local_contract.name, "schema_hash": local_contract.schema_hash,
            "schema": local_contract.schema,
        }
    request_identity = {
        "stage": stage, "model": resolved_model, "system": selected_system_text,
        "user": selected_user_text, "parameters": fingerprint_parameters,
        "assembled_schema": output_model.model_json_schema(),
    }
    completed_binding = completed_stage_key(request_identity)
    completed_output = load_completed_stage(completed_binding, output_model)
    if completed_output is not None:
        _log.info("authoring_completed_stage_reused", stage=stage, provider_called=False)
        return completed_output
    request_hash = _canonical_sha256(request_identity) if candidate_checkpoint is not None else ""
    cached_response = candidate_checkpoint.load(request_hash) if candidate_checkpoint is not None else None
    latest_response: LLMResponse | None = None

    def llm_call(messages: list[Message]) -> LLMResponse:
        nonlocal cached_response, latest_response
        if cached_response is not None:
            latest_response, cached_response = cached_response, None
            _log.info("mini_plan_unverified_candidate_reused", request_hash=request_hash,
                      validation_required=True, provider_called=False)
            return latest_response
        latest_response = resolved_client.complete(
            messages=messages, model=resolved_model, **complete_kwargs,
        )
        return latest_response

    def checked_candidate(output: BaseModel) -> None:
        # Only parsed/assembled output is eligible; this checkpoint certifies no gate.
        if candidate_checkpoint is not None and latest_response is not None:
            candidate_checkpoint.store(request_hash, latest_response)
        try:
            if post_validate is not None:
                post_validate(output)
        except Exception as exc:
            if candidate_checkpoint is not None and not isinstance(
                exc, (*non_retryable_exceptions, LLMCancelledError, GenerationCheckpointError)
            ):
                candidate_checkpoint.reject(request_hash)
            raise

    timer: StageTimer | None = None
    if timing_context is not None:
        timer = StageTimer(
            _log,
            "outline_stage_timing",
            _outline_timing_fields(
                timing_context,
                stage=stage,
                substage=str((timing_fields or {}).get("substage", "llm")),
                **{k: v for k, v in (timing_fields or {}).items() if k != "substage"},
            ),
        )
        timer.__enter__()

    try:
        result, _attempts, _model_used = run_with_schema_retry(
            agent_name="outline_builder",
            stage=stage,
            system_text=selected_system_text,
            initial_user_text=selected_user_text,
            output_model=output_model,
            llm_call=llm_call,
            strip_format_noise=(
                _strip_characters_format_noise if use_delta and stage == "characters"
                else _strip_format_noise
            ),
            normalize_data=(
                delta_spec.assemble_data
                if use_delta and delta_spec is not None
                else (
                    _normalize_volume_plans_explicit_trailing_field_pair
                    if stage == "volume_plans"
                    else None
                )
            ),
            normalization_retryable=(
                lambda exc: (
                    isinstance(exc, CreativeDeltaAssemblyError)
                    and (
                        (exc.code == "initial_core_fact_budget_exceeded"
                         and exc.field_path == "characters.initial_core_facts")
                        or (exc.code == "duplicate_initial_core_fact_attribute"
                            and re.fullmatch(
                                r"characters\[\d+\]\.initial_core_facts\[\d+\]\.attribute",
                                exc.field_path) is not None)
                        or (exc.code == "ungrounded_model_delta"
                            and re.fullmatch(
                                r"characters\[\d+\]\.initial_core_facts\[\d+\]\.value",
                                exc.field_path) is not None)
                    )
                )
            ) if use_delta and stage == "characters" else (
                lambda exc: (
                    isinstance(exc, CreativeDeltaAssemblyError)
                    and (
                        exc.code in {"ambiguous_local_reference", "unknown_local_reference"}
                        or (
                            exc.code == "missing_creative_reference"
                            and re.fullmatch(
                                r"volume_plans_v4\.volumes\[\d+\]\.dramatic_moments\[\d+\]",
                                exc.field_path,
                            ) is not None
                        )
                    )
                    and exc.field_path.startswith("volume_plans_v4.volumes[")
                    and ".dramatic_moments[" in exc.field_path
                )
            ) if use_delta and stage == "volume_plans" else None,
            normalization_feedback=(
                lambda exc: f"初始事实登记不满足现有合同：{exc}。"
                "程序没有裁剪或替你选择事实。请修正后重新输出完整 JSON。"
            ) if use_delta and stage == "characters" else None,
            post_validate=checked_candidate if candidate_checkpoint is not None else post_validate,
            schema_validation_feedback=schema_validation_feedback,
            failure_fingerprint=failure_fingerprint,
            non_retryable_exceptions=(*non_retryable_exceptions, HandoffError, GenerationCheckpointError),
            max_attempts=settings.outline_builder_max_retry_attempts,
            preserve_candidate_on_retry=uses_experimental_pipeline(resolved_client),
        )
    except (CreativeDeltaAssemblyError, ValidationError) as exc:
        error_code = (
            exc.code
            if isinstance(exc, CreativeDeltaAssemblyError)
            else "creative_delta_schema_mismatch"
        )
        if timer is not None:
            timer.finish(
                status="failed",
                reason=error_code,
                attempts=1,
                model_used=resolved_model,
                retry_exhausted=False,
            )
        raise OutlineBuilderError(
            f"OutlineBuilder stage={stage}:creative delta 确定性组装失败:{exc}",
            stage=stage,
            raw_output="",
            model_used=resolved_model,
            error_code=error_code,
            attempts=1,
            retry_stop_reason="caller_fail_closed",
        ) from exc
    except SchemaRetryExhausted as exc:
        # 把通用 helper 的终态错误转模块级 OutlineBuilderError(保留旧接口)。
        root = exc.root_exc
        conflict: (
            MiniPlanSettingConflictError
            | PlotSettingConflictError
            | VolumePlansSettingConflictError
            | None
        ) = None
        if isinstance(
            root,
            (
                MiniPlanSettingConflictError,
                PlotSettingConflictError,
                VolumePlansSettingConflictError,
            ),
        ):
            conflict = root
            reason = f"{conflict.failure_layer}_setting_conflict"
            msg = str(root)
            if (
                isinstance(conflict, VolumePlansSettingConflictError)
                and exc.stop_reason == "no_progress"
            ):
                msg += "\n" + conflict.no_progress_message
        elif isinstance(root, OutlineStageFactConflictError):
            reason = "stage_fact_conflict"
            msg = str(root)
        elif isinstance(root, json.JSONDecodeError):
            reason = "invalid_json"
            msg = f"OutlineBuilder stage={stage}:N 次 retry 后 LLM 仍输出非合法 JSON"
        elif isinstance(root, ValidationError):
            reason = "schema_mismatch"
            msg = (
                f"OutlineBuilder stage={stage}:N 次 retry 后 LLM 输出 JSON 结构"
                f"仍与 schema 不符"
            )
        else:
            reason = "post_validate_failed"
            msg = f"OutlineBuilder stage={stage}:N 次 retry 后业务规则校验仍失败"
        if (
            exc.stop_reason in {"no_progress", "caller_fail_closed"}
            and conflict is None
        ):
            msg += (
                f"；retry_stop_reason={exc.stop_reason}，"
                "同一稳定 failure fingerprint 不再继续购买响应"
            )
        _log.error(
            "outline_builder_failed",
            stage=stage,
            reason=reason,
            attempts=exc.attempts,
            retry_stop_reason=exc.stop_reason,
            failure_fingerprint=exc.failure_fingerprint,
            raw_output=exc.last_response_content,
            model_used=exc.last_model_used,
            constraint_ids=(
                [v.constraint_id for v in conflict.violations] if conflict else []
            ),
            contract_hash=conflict.contract_hash if conflict else None,
            attempt=exc.attempts,
            failure_layer=conflict.failure_layer if conflict else stage,
        )
        if timer is not None:
            timer.finish(
                status="failed",
                reason=reason,
                attempts=exc.attempts,
                model_used=exc.last_model_used,
                retry_exhausted=True,
            )
        raise OutlineBuilderError(
            msg,
            stage=stage,
            raw_output=exc.last_response_content,
            model_used=exc.last_model_used,
            violations=(
                [v.model_dump(mode="json") for v in conflict.violations]
                if conflict
                else None
            ),
            error_code=conflict.code if conflict else None,
            contract_hash=conflict.contract_hash if conflict else None,
            attempts=exc.attempts,
            failure_fingerprint=exc.failure_fingerprint,
            retry_stop_reason=exc.stop_reason,
            contradictions=(root.contradictions if isinstance(root, OutlineStageFactConflictError) else None),
            contradiction_details=(root.contradiction_details if isinstance(root, OutlineStageFactConflictError) else None),
            coverage_gaps=(root.coverage_gaps if isinstance(root, OutlineStageFactConflictError) else None),
            evidence=(root.evidence if isinstance(root, OutlineStageFactConflictError) else None),
            repair_scope=("upstream" if isinstance(root, OutlineStageFactConflictError) else None),
        ) from root
    except Exception as exc:
        if not isinstance(exc, non_retryable_exceptions):
            raise
        technical_attempts = int(getattr(exc, "attempts", 1))
        _log.error(
            "outline_builder_failed",
            stage=stage,
            reason="non_retryable_post_validate_failed",
            attempts=technical_attempts,
            model_used=resolved_model,
            error_type=type(exc).__name__,
            attempt=technical_attempts,
            failure_layer=stage,
        )
        if timer is not None:
            timer.finish(
                status="failed",
                reason="non_retryable_post_validate_failed",
                attempts=technical_attempts,
                model_used=resolved_model,
                retry_exhausted=technical_attempts > 0,
            )
        raise

    if timer is not None:
        timer.finish(
            status="succeeded",
            attempts=_attempts,
            model_used=_model_used,
            retried=_attempts > 1,
        )
    store_completed_stage(completed_binding, result)
    return result


def _load_stage_prompt(stage: OutlineStage, prompts_dir: Path | None) -> str:
    """按 stage 加载 ``prompts/outline_builder/<stage>.md`` 系统提示词。

    ``prompts_dir=None`` 时取 ``settings.prompts_dir``;P3 工作区级覆盖时
    OutlineBuilder 零改动(对齐 writer / reviser / extractor 模式)。
    """
    template_root = (
        prompts_dir if prompts_dir is not None else settings.prompts_dir
    ).resolve()
    filename = _STAGE_PROMPT_FILENAMES[stage]
    return load_prompt_template(template_root / _STAGE_PROMPT_SUBDIR / filename)


def _load_delta_stage_prompt(filename: str, prompts_dir: Path | None) -> str:
    """Load a T2-only prompt without changing any legacy prompt bytes."""

    template_root = (
        prompts_dir if prompts_dir is not None else settings.prompts_dir
    ).resolve()
    return load_prompt_template(template_root / _STAGE_PROMPT_SUBDIR / filename)


def _append_stage_marker(user_text: str, stage: OutlineStage) -> str:
    """OutlineBuilder 4 段各自的 agent role marker(P1-B-22 协议扩 4 子角色)。

    Marker 形态::

        <!-- agent_role: outline_world -->
        <!-- agent_role: outline_characters -->
        <!-- agent_role: outline_plot -->
        <!-- agent_role: outline_volume_plans -->

    MockLLMClient 通过 ``_prompt.detect_agent_role`` 抓 marker 决定返哪段
    fixture(P3 mock 路径模式)。真 LLM 视 HTML 注释为字面忽略。
    """
    role = _OUTLINE_STAGE_ROLE_MARKERS[stage]
    # 复用 ``append_role_marker`` 集中点(P5-B-03 扩 _MARKER_BY_ROLE 4 子角色)。
    return append_role_marker(user_text, role=role)  # type: ignore[arg-type]


# OutlineBuilder 默认生成参数(legacy 路径用,router 路径下被 profile 覆盖)。
# Outline 输出是结构化 JSON,温度低、长度中等。
_DEFAULT_TEMPERATURE = 0.4
_DEFAULT_MAX_TOKENS = 6000


# ---------------------------------------------------------------------------
# Stage 1: build_world
# ---------------------------------------------------------------------------


def load_story_intent_prompt(prompts_dir: Path | None = None, *, scoped: bool = False) -> str:
    """生成与缓存共用实际提示，避免版本更新后误复用旧定位。"""
    return _load_delta_stage_prompt("story_intent_v9.md" if scoped else "story_intent_v6.md", prompts_dir)


def build_story_intent(
    *, premise: str, genre: str, total_volumes: int, chapters_per_volume: int,
    preferences: str = "", planning_scope: PlanningScope | None = None,
    reading_preferences: str | None = None,
    router: ModelRouter | None = None,
    client: LLMClient | None = None, model: str | None = None,
    prompts_dir: Path | None = None,
) -> StoryIntent:
    """现有大纲生成器的一次前置调用；不增加独立 Agent 或审核。"""
    text = load_story_intent_prompt(prompts_dir, scoped=planning_scope is not None)
    data = {"premise": premise, "genre": genre, "preferences": preferences,
            "total_volumes": total_volumes, "chapters_per_volume": chapters_per_volume}
    if planning_scope is not None:
        data.update(planning_scope=planning_scope.model_dump(mode="json"),
                    reading_reference=planning_scope.reading_reference())
    result = _run_stage(
        stage="world", system_text=text,
        user_text="从用户原始设定选择全书故事定位，补全内容不是用户硬要求。\n"
        + json.dumps(data, ensure_ascii=False),
        output_model=ExperiencedStoryIntentDesign if planning_scope is not None else StoryIntentDesign,
        handoff_data={"story_intent_input": data},
        router=router, client=client, model=model, max_tokens=_DEFAULT_MAX_TOKENS,
        temperature=_DEFAULT_TEMPERATURE,
        timing_fields={"substage": "story_intent"},
    )
    assert isinstance(result, StoryIntentDesign)
    return StoryIntent.model_validate({
        **result.model_dump(mode="json"), "version": 6 if planning_scope is not None else 3,
        **({"planning_scope": planning_scope} if planning_scope is not None else {}),
        "user_request": UserNarrativeRequest.from_source(premise=premise, genre=genre,
            reading_preferences=(reading_preferences if reading_preferences is not None else preferences)
            if planning_scope is not None else None),
    })


class _WorldFactsOutput(BaseModel):
    """新世界请求只生成原五项；故事定位由程序继承。"""

    model_config = ConfigDict(extra="forbid")

    setting: str = Field(min_length=1)
    power_system: str
    factions: str = Field(min_length=1)
    culture: str = Field(min_length=1)
    unique_rules: str


def build_world(
    *,
    premise: str,
    genre: str = "",
    total_volumes: int = 5,
    chapters_per_volume: int = 30,
    router: ModelRouter | None = None,
    client: LLMClient | None = None,
    model: str | None = None,
    max_tokens: int = _DEFAULT_MAX_TOKENS,
    temperature: float = _DEFAULT_TEMPERATURE,
    prompts_dir: Path | None = None,
    timing_context: dict[str, Any] | None = None,
    story_intent: StoryIntent | None = None,
) -> WorldOutput:
    """OutlineBuilder 第 1 段:从 premise 生成世界观。

    :param premise: 用户一句话设定(必传,>= 1 字)。
    :param genre: 题材(``仙侠 / 奇幻 / 都市 / 科幻 / 历史 / 推理 / 其他``;
        空字符串视为未指定,LLM 按 premise 自行推断)。
    :param total_volumes: 目标卷数(只作宏观参考,默认 5)。
    :param chapters_per_volume: 每卷章数(只作宏观参考,默认 30)。
    :param router: P3-B-06 主路径 ``ModelRouter``。
    :param client: legacy 路径 LLMClient(``router=None`` 时必传)。
    :param model: legacy 路径模型名。
    :param max_tokens: legacy 路径输出上限。
    :param temperature: legacy 路径温度。
    :param prompts_dir: 提示词模板根目录;``None`` 时取 ``settings.prompts_dir``。
    :return: ``WorldOutput`` Pydantic 实例。
    :raises OutlineBuilderError: LLM 输出非 JSON / schema 不符。
    :raises TypeError: ``router=None`` 且 legacy 路径缺 ``client`` / ``model``。
    """
    if not premise.strip():
        raise ValueError("OutlineBuilder.build_world: premise 不能为空")

    system_text = _load_stage_prompt("world", prompts_dir)
    user_text = _append_stage_marker(
        _build_world_user_message(
            premise=premise,
            genre=genre,
            total_volumes=total_volumes,
            chapters_per_volume=chapters_per_volume,
        ),
        stage="world",
    )

    def _check_mechanism(out: BaseModel) -> None:
        """v1.1 wave 6 session 1 / A1:world 段机制一致性自检(post_validate)。

        调 mechanism_consistency_validator LLM-as-judge:contradictions(真互斥)
        非空 → 抛 ``ValueError`` → ``_run_stage`` retry-with-feedback 让 LLM 消除
        互斥(HARD);vague_mechanisms(高概念缺规则)只在 validator 内 log warn
        不 raise(WARN 标记,护 §0 第一原则)。LLM 失败 fail-open(passed=True)。
        """
        out = WorldOutput.model_validate(out.model_dump(mode="json"))
        mech = validate_mechanism_consistency(
            world=out,
            premise=premise,
            genre=genre,
            router=router,
            client=client,
            model=model,
            prompts_dir=prompts_dir,
        )
        if not mech.passed:
            raise ValueError(format_mechanism_failure_for_retry(result=mech))

    result = _run_stage(
        stage="world",
        system_text=system_text,
        user_text=user_text,
        output_model=_WorldFactsOutput if story_intent is not None else WorldOutput,
        handoff_data={"premise": premise, "genre": genre, "total_volumes": total_volumes,
                      "chapters_per_volume": chapters_per_volume,
                      **({"story_intent": story_intent} if story_intent is not None else {})},
        load_experimental_system_text=lambda: _load_delta_stage_prompt(
            "world_narrative_v8.md" if story_intent is not None and story_intent.version >= 6 else "world_narrative_v7.md" if story_intent is not None and story_intent.version == 5 else "world_narrative_v6.md" if story_intent is not None and story_intent.version == 4 else "world_narrative_v5.md" if story_intent is not None else "world_v6.md", prompts_dir),
        router=router,
        client=client,
        model=model,
        max_tokens=max_tokens,
        temperature=temperature,
        post_validate=_check_mechanism,
        non_retryable_exceptions=(MechanismConsistencyTechnicalError,),
        timing_context=timing_context,
    )
    return WorldOutput.model_validate({
        **result.model_dump(mode="json"),
        **({"story_intent": story_intent} if story_intent is not None else {}),
    })


def _build_world_user_message(
    *,
    premise: str,
    genre: str,
    total_volumes: int,
    chapters_per_volume: int,
) -> str:
    genre_text = genre if genre.strip() else "(未指定,按 premise 自行推断)"
    return (
        "## 用户输入\n\n"
        f"- 一句话设定(premise):{premise}\n"
        f"- 题材(genre):{genre_text}\n"
        f"- 目标卷数(total_volumes):{total_volumes}(宏观参考)\n"
        f"- 每卷章数(chapters_per_volume):{chapters_per_volume}(宏观参考)\n"
        "\n"
        "请生成本书的世界观段。"
    )


# ---------------------------------------------------------------------------
# Stage 2: build_characters
# ---------------------------------------------------------------------------


def build_characters(
    *,
    premise: str,
    world: WorldOutput,
    genre: str = "",
    router: ModelRouter | None = None,
    client: LLMClient | None = None,
    model: str | None = None,
    max_tokens: int = _DEFAULT_MAX_TOKENS,
    temperature: float = _DEFAULT_TEMPERATURE,
    prompts_dir: Path | None = None,
    timing_context: dict[str, Any] | None = None,
) -> CharactersOutput:
    """OutlineBuilder 第 2 段:基于 premise + world 生成核心人设。

    :param premise: 用户一句话设定(必传)。
    :param world: 上一阶段产出(只读 context)。
    :param genre: 题材(可空)。
    其余参数同 ``build_world``。

    :return: ``CharactersOutput`` Pydantic 实例(``characters`` 长度 4-8)。
    """
    if not premise.strip():
        raise ValueError("OutlineBuilder.build_characters: premise 不能为空")

    system_text = _load_stage_prompt("characters", prompts_dir)
    user_text = _append_stage_marker(
        _build_characters_user_message(premise=premise, genre=genre, world=world),
        stage="characters",
    )
    delta_authority_payload = {
        "premise": premise,
        "genre": genre,
        "world": world.model_dump(mode="json"),
    }
    delta_authority = FrozenAuthority.freeze(
        stage="characters",
        payload=delta_authority_payload,
    )

    def _assemble_characters_delta(data: Any) -> Any:
        artifact = assemble_characters_delta(
            delta=CharactersCreativeDelta.model_validate(data),
            authority=delta_authority,
            current_authority=delta_authority_payload,
        )
        _log.info(
            "creative_delta_assembled",
            stage="characters",
            authority_hash=artifact.authority_hash,
            delta_hash=artifact.delta_hash,
            full_leaf_count=artifact.full_leaf_count,
            model_delta_leaf_count=artifact.model_delta_leaf_count,
        )
        return artifact.value.model_dump(mode="json")

    def _check_initial_core_facts(out: BaseModel) -> None:
        """新生角色必须显式声明正文前核心基线，空数组也是有效裁决。"""

        assert isinstance(out, CharactersOutput)
        if world.story_intent is not None and any(
            not item.present_desire.strip() or not item.personal_stakes.strip() for item in out.characters
        ):
            raise ValueError("新叙事人物必须填写眼前愿望和切身利害")
        missing = [
            item.character_id
            for item in out.characters
            if "initial_core_facts" not in item.model_fields_set
        ]
        if missing:
            raise ValueError(
                "每个角色都必须显式输出 initial_core_facts（没有则 []）: "
                + ", ".join(missing)
            )
        if uses_experimental_outline(router, client):
            try:
                review = validate_outline_stage_consistency(
                    stage="characters", world=world, characters=out,
                    premise=premise, genre=genre,
                    workspace_id=(str(timing_context.get("workspace_id"))
                                  if timing_context and timing_context.get("workspace_id") else None),
                    router=router, client=client, model=model, prompts_dir=prompts_dir,
                )
            except Exception as exc:
                raise OutlineStageReviewTechnicalError("人物来源事实核对没有有效结论") from exc
            if not review.passed:
                raise OutlineStageFactConflictError(stage="characters", result=review)

    result = _run_stage(
        stage="characters",
        system_text=system_text,
        user_text=user_text,
        output_model=CharactersOutput,
        handoff_data=delta_authority_payload,
        router=router,
        client=client,
        model=model,
        max_tokens=max_tokens,
        temperature=temperature,
        post_validate=_check_initial_core_facts,
        non_retryable_exceptions=(OutlineStageReviewTechnicalError,),
        timing_context=timing_context,
        delta_spec=_OutlineDeltaSpec(
            output_model=CharactersCreativeDelta,
            load_system_text=lambda: _load_delta_stage_prompt(
                "characters_delta.md", prompts_dir
            ),
            load_complete_system_text=lambda: _load_delta_stage_prompt(
                "characters_narrative_v9.md" if world.story_intent is not None and world.story_intent.version >= 6 else "characters_narrative_v8.md" if world.story_intent is not None and world.story_intent.version == 5 else "characters_narrative_v7.md" if world.story_intent is not None and world.story_intent.version == 4 else "characters_narrative_v6.md" if world.story_intent is not None else "characters_delta_v6.md", prompts_dir
            ),
            assemble_data=_assemble_characters_delta,
        ),
    )
    assert isinstance(result, CharactersOutput)
    return result


def _build_characters_user_message(
    *, premise: str, genre: str, world: WorldOutput
) -> str:
    genre_text = genre if genre.strip() else "(未指定)"
    return (
        "## 用户输入\n\n"
        f"- 一句话设定(premise):{premise}\n"
        f"- 题材(genre):{genre_text}\n\n"
        "## 上游产出:世界观(world,只读)\n\n"
        f"- setting:{world.setting}\n"
        f"- power_system:{world.power_system}\n"
        f"- factions:{world.factions}\n"
        f"- culture:{world.culture}\n"
        f"- unique_rules(HARD；只应包含真正不可破例的世界规则):"
        f"{world.unique_rules}\n\n"
        "## 核心人设写前世界规则合同(HARD)\n\n"
        "- 角色背景、身份、动机、能力和特殊道具都不得为 `unique_rules` 创造"
        "规则原文没有的例外、绕过方式或指定对象能力。\n"
        "- 血统特殊、主角光环、重大代价、反派计划或人物误判都不自动获得豁免；"
        "只有 `unique_rules` 原文明示的例外才可使用。\n"
        "- `setting` / `power_system` 是普通机制上下文，不自动升级成 HARD；"
        "在不直接违反 `unique_rules` 的前提下，可以为人物塑造保留合理解释空间。\n\n"
        "请基于以上世界观生成 4-8 个核心角色(主角 1 + 配角 2-4 + 反派 1-2 + 次要 0-2)。"
    )


# ---------------------------------------------------------------------------
# Stage 3: build_plot
# ---------------------------------------------------------------------------


def _render_plot_setting_evidence(plot: PlotOutput) -> str:
    """把主线全部语义字段稳定渲染为 setting judge 可逐字回查的证据。"""

    return "\n".join(f"{field}:{value}" for field, value in _plot_setting_fields(plot))


def _plot_setting_fields(plot: PlotOutput) -> list[tuple[str, str]]:
    fields: list[tuple[str, str]] = [("plot.core_conflict", plot.core_conflict)]
    for phase in plot.main_phases:
        prefix = f"plot.main_phases[phase_id={phase.phase_id}]"
        fields.extend(
            ((f"{prefix}.title", phase.title), (f"{prefix}.summary", phase.summary))
        )
    for turning in plot.major_turning_points:
        prefix = f"plot.major_turning_points[turning_id={turning.turning_id}]"
        fields.extend(
            (
                (f"{prefix}.title", turning.title),
                (f"{prefix}.description", turning.description),
            )
        )
    for thread in plot.long_threads:
        prefix = f"plot.long_threads[thread_id={thread.thread_id}]"
        fields.extend(
            (
                (f"{prefix}.title", thread.title),
                (f"{prefix}.description", thread.description),
            )
        )
    fields.extend(
        (f"plot.character_arcs[character_id={character_id}]", value)
        for character_id, value in sorted(plot.character_arcs.items())
    )
    for item in plot.load_bearing_objects:
        prefix = f"plot.load_bearing_objects[object_id={item.object_id}]"
        fields.extend(
            (
                (f"{prefix}.label", item.label),
                (f"{prefix}.causal_requirement", item.causal_requirement),
            )
        )
    return [(field, value) for field, value in fields if value.strip()]


def build_plot(
    *,
    premise: str,
    world: WorldOutput,
    characters: CharactersOutput,
    genre: str = "",
    router: ModelRouter | None = None,
    client: LLMClient | None = None,
    model: str | None = None,
    max_tokens: int = _DEFAULT_MAX_TOKENS,
    temperature: float = _DEFAULT_TEMPERATURE,
    prompts_dir: Path | None = None,
    timing_context: dict[str, Any] | None = None,
) -> PlotOutput:
    """OutlineBuilder 第 3 段:基于 premise + world + characters 生成主线。

    :param characters: 上一阶段产出。
    其余参数同前两段。

    :return: ``PlotOutput`` Pydantic 实例。
    """
    if not premise.strip():
        raise ValueError("OutlineBuilder.build_plot: premise 不能为空")

    system_text = _load_stage_prompt("plot", prompts_dir)
    user_text = _append_stage_marker(
        _build_plot_user_message(
            premise=premise, genre=genre, world=world, characters=characters
        ),
        stage="plot",
    )
    delta_authority_payload = {
        "premise": premise,
        "genre": genre,
        "world": world.model_dump(mode="json"),
        "characters": characters.model_dump(mode="json"),
    }
    delta_authority = FrozenAuthority.freeze(
        stage="plot",
        payload=delta_authority_payload,
    )

    plot_delta_model = (
        ScopedPlotCreativeDelta
        if world.story_intent is not None and world.story_intent.planning_scope is not None
        else PlotCreativeDelta
    )

    def _assemble_plot_delta(data: Any) -> Any:
        artifact = assemble_plot_delta(
            delta=plot_delta_model.model_validate(data),
            characters=characters,
            authority=delta_authority,
            current_authority=delta_authority_payload,
        )
        _log.info(
            "creative_delta_assembled",
            stage="plot",
            authority_hash=artifact.authority_hash,
            delta_hash=artifact.delta_hash,
            full_leaf_count=artifact.full_leaf_count,
            model_delta_leaf_count=artifact.model_delta_leaf_count,
        )
        return artifact.value.model_dump(mode="json")

    resolved_setting_contract = SettingContract.from_world_output(world)
    setting_gate_attempt = 0

    def _check_plot_setting(out: BaseModel) -> None:
        """主线写盘前复用 R9 权威合同；HARD 进入既有有界反馈重试。"""

        nonlocal setting_gate_attempt
        assert isinstance(out, PlotOutput)
        character_ids = {item.character_id for item in characters.characters}
        collisions = sorted(
            character_ids & {item.object_id for item in out.load_bearing_objects}
        )
        if collisions:
            raise ValueError(
                "load_bearing_object.object_id 与 character stable ID 冲突:"
                f"{collisions}"
            )
        if uses_experimental_outline(router, client):
            try:
                review = validate_outline_stage_consistency(
                    stage="plot", world=world, characters=characters, plot=out,
                    premise=premise, genre=genre,
                    workspace_id=(str(timing_context.get("workspace_id"))
                                  if timing_context and timing_context.get("workspace_id") else None),
                    router=router, client=client, model=model, prompts_dir=prompts_dir,
                )
            except Exception as exc:
                raise OutlineStageReviewTechnicalError("主线来源事实核对没有有效结论") from exc
            if not review.passed:
                raise OutlineStageFactConflictError(stage="plot", result=review)
        if not (resolved_setting_contract.unique_rules.strip() or (
            uses_experimental_outline(router, client)
            and (resolved_setting_contract.setting.strip() or resolved_setting_contract.power_system.strip())
        )):
            return
        setting_gate_attempt += 1
        result = validate_setting_consistency(
            chapter_text=_render_plot_setting_evidence(out),
            contract=resolved_setting_contract,
            chapter_id="outline-plot",
            workspace_id=(
                str(timing_context.get("workspace_id"))
                if timing_context and timing_context.get("workspace_id")
                else None
            ),
            router=router,
            client=client,
            model=model,
            prompts_dir=prompts_dir,
            validation_scope="outline_plot",
        )
        hard = prewriting_blockers(result)
        if hard:
            _log.error(
                "prewriting_semantic_gate_failed",
                failure_layer="plot",
                constraint_ids=[v.constraint_id for v in hard],
                contract_hash=resolved_setting_contract.source_hash,
                attempt=setting_gate_attempt,
            )
            raise PlotSettingConflictError(
                violations=hard,
                contract_hash=resolved_setting_contract.source_hash,
                attempt=setting_gate_attempt,
            )
        if result.violations:
            _log.warning(
                "prewriting_semantic_gate_soft",
                failure_layer="plot",
                constraint_ids=[v.constraint_id for v in result.violations],
                contract_hash=resolved_setting_contract.source_hash,
                attempt=setting_gate_attempt,
            )

    result = _run_stage(
        stage="plot",
        system_text=system_text,
        user_text=user_text,
        output_model=PlotOutput,
        handoff_data=delta_authority_payload,
        router=router,
        client=client,
        model=model,
        max_tokens=max_tokens,
        temperature=temperature,
        post_validate=_check_plot_setting,
        non_retryable_exceptions=(SettingConsistencyTechnicalError, OutlineStageReviewTechnicalError),
        timing_context=timing_context,
        delta_spec=_OutlineDeltaSpec(
            output_model=plot_delta_model,
            load_system_text=lambda: _load_delta_stage_prompt(
                "plot_delta.md", prompts_dir
            ),
            load_complete_system_text=lambda: _load_delta_stage_prompt(
                "plot_narrative_v10.md" if world.story_intent is not None and world.story_intent.version >= 6 else "plot_narrative_v9.md" if world.story_intent is not None and world.story_intent.version == 5 else "plot_narrative_v8.md" if world.story_intent is not None and world.story_intent.version == 4 else "plot_narrative_v7.md" if world.story_intent is not None else "plot_delta_v6.md", prompts_dir
            ),
            load_complete_user_text=lambda: user_text + render_story_authority_context(
                world=world, characters=characters,
            ),
            assemble_data=_assemble_plot_delta,
        ),
    )
    assert isinstance(result, PlotOutput)
    return result


def _build_plot_user_message(
    *,
    premise: str,
    genre: str,
    world: WorldOutput,
    characters: CharactersOutput,
) -> str:
    genre_text = genre if genre.strip() else "(未指定)"
    character_lines = [
        f"  - {c.character_id}({c.name},{c.role}):{c.archetype} — {c.motivation}"
        for c in characters.characters
    ]
    # 补强1(v2 W3.5):命名锁定表前置注入(漂移源头 —— plot 不传 background,
    # 父亲名等 buried 命名在此被 deepseek 改写)。无 buried 名时返 "" 不扰动。
    return (
        build_name_lock_header(characters) + "## 用户输入\n\n"
        f"- 一句话设定(premise):{premise}\n"
        f"- 题材(genre):{genre_text}\n\n"
        "## 上游产出:世界观摘要(world,只读)\n\n"
        f"- setting:{world.setting}\n"
        f"- factions:{world.factions}\n"
        f"- unique_rules:{world.unique_rules}\n\n"
        "## 主线写前世界规则合同(HARD)\n\n"
        f"- 权威规则原文:{world.unique_rules}\n"
        "- 每次使用机制都要逐项保持原规则的触发条件、作用对象、数量 / 范围和效果。\n"
        "- 若规则自动决定对象,角色不得选择自己、同伴、指定人物、群体、类别或概念;"
        "完成触发条件不等于可以指定结果。\n"
        "- 多次使用均须遵守原文明示的单次与累计数量范围、对象选择方式；"
        "原文允许群体效果时不得擅自缩成单对象，也不得凭空扩展范围。\n"
        "- 反派计划也受规则约束;除非主线明确写成误判、妄想或因规则不允许而无法启动,"
        "否则失败的计划也不能承诺规则外能力。\n\n"
        "## 上游产出:核心人设(characters,只读)\n\n"
        + "\n".join(character_lines)
        + "\n\n请生成本书的主线段(核心冲突 + 4-7 阶段 + 3-6 关键转折 + 3-6 长伏笔种子 + character_arcs)。"
    )


# ---------------------------------------------------------------------------
# Stage 4: build_volume_plans
# ---------------------------------------------------------------------------


def _render_volume_plans_setting_evidence(
    volume_plans: VolumePlansOutput,
) -> str:
    """稳定渲染卷规划语义字段，供写盘前 setting judge 逐字回查。

    ``forbidden_advance`` 也保留原文，但显式标成当前 beat 的否定边界；judge
    不得仅因被禁止事件本身违反规则，就把它误认成已经承诺会发生。
    """

    return "\n".join(
        f"{field}:{value}"
        for field, value in _volume_plans_setting_fields(volume_plans)
    )


def _volume_plans_setting_fields(
    volume_plans: VolumePlansOutput,
) -> list[tuple[str, str]]:
    fields: list[tuple[str, str]] = []
    for volume in volume_plans.volumes:
        volume_prefix = f"volume_plans.volumes[volume_id={volume.volume_id}]"
        fields.extend(
            (
                (f"{volume_prefix}.title", volume.title),
                (f"{volume_prefix}.summary", volume.summary),
            )
        )
        for arc in volume.arcs:
            arc_prefix = f"{volume_prefix}.arcs[arc_id={arc.arc_id}]"
            fields.extend(
                (
                    (f"{arc_prefix}.arc_type", arc.arc_type),
                    (f"{arc_prefix}.phase", arc.phase),
                    (f"{arc_prefix}.summary", arc.summary),
                )
            )
            for beat in arc.beats:
                beat_prefix = f"{arc_prefix}.beats[beat_id={beat.beat_id}]"
                fields.append((f"{beat_prefix}.summary", beat.summary))
                fields.extend(
                    (f"{beat_prefix}.must_advance[{index}]", item)
                    for index, item in enumerate(beat.must_advance)
                )
                fields.extend(
                    (
                        f"{beat_prefix}.forbidden_advance[{index}]"
                        "(本 beat 不得发生，不代表未来必然发生)",
                        item,
                    )
                    for index, item in enumerate(beat.forbidden_advance)
                )
    return [(field, value) for field, value in fields if value.strip()]


def _canonical_text(value: str) -> str:
    return " ".join(unicodedata.normalize("NFKC", value).split())


def _canonical_sha256(value: Any) -> str:
    payload = json.dumps(
        value,
        ensure_ascii=False,
        sort_keys=True,
        separators=(",", ":"),
    ).encode("utf-8")
    return hashlib.sha256(payload).hexdigest()


def _find_verbatim_evidence_field(
    evidence_quote: str,
    fields: list[tuple[str, str]],
) -> str | None:
    quote = evidence_quote.strip()
    if not quote:
        return None
    for field, value in fields:
        if quote in value or quote in f"{field}:{value}":
            return field
    return None


def _attribute_volume_setting_violations(
    *,
    violations: list[ConstraintViolation],
    plot: PlotOutput,
    volume_plans: VolumePlansOutput,
) -> list[VolumeSettingEvidenceAttribution]:
    """仅按逐字回查给 volume Setting HARD 标来源，不做语义猜测。"""

    plot_fields = _plot_setting_fields(plot)
    volume_fields = _volume_plans_setting_fields(volume_plans)
    attributions: list[VolumeSettingEvidenceAttribution] = []
    for violation in violations:
        upstream_field = _find_verbatim_evidence_field(
            violation.evidence_quote,
            plot_fields,
        )
        candidate_field = _find_verbatim_evidence_field(
            violation.evidence_quote,
            volume_fields,
        )
        source_stage: VolumeSettingSourceStage
        if upstream_field is not None:
            source_stage = "plot"
        elif candidate_field is not None:
            source_stage = "volume_plans"
        else:
            source_stage = "ambiguous"
        attributions.append(
            VolumeSettingEvidenceAttribution(
                constraint_id=violation.constraint_id,
                canonical_rule=_canonical_text(violation.rule_quote),
                canonical_evidence=_canonical_text(violation.evidence_quote),
                source_stage=source_stage,
                upstream_field=upstream_field,
                candidate_field=candidate_field,
            )
        )
    return attributions


def _canonical_failure_input(value: Any) -> Any:
    if isinstance(value, str):
        return _canonical_text(value)
    if value is None or isinstance(value, (bool, int, float)):
        return value
    if isinstance(value, list | tuple):
        return [_canonical_failure_input(item) for item in value]
    if isinstance(value, dict):
        return {
            str(key): _canonical_failure_input(item)
            for key, item in sorted(value.items(), key=lambda pair: str(pair[0]))
        }
    return type(value).__name__


def _volume_contract_failure_payload(
    failure: VolumePlansContractFailure,
) -> dict[str, Any]:
    return {
        "rule": failure.rule,
        "evidence": _canonical_text(failure.message),
        "field": failure.field,
        "volume_id": failure.volume_id,
        "arc_id": failure.arc_id,
        "beat_id": failure.beat_id,
        "object_id": failure.object_id,
        "actual_turning_id": failure.actual_turning_id,
        "actual_thread_id": failure.actual_thread_id,
        "allowed_turning_id": failure.allowed_turning_id,
        "allowed_thread_id": failure.allowed_thread_id,
        "source": "volume_candidate",
    }


def _volume_schema_failure_fingerprint(
    failure: SchemaRetryFailure,
    *,
    contract: VolumePlansContract,
) -> SchemaRetryFailureFingerprint | None:
    """把 volume 调用方可证的 rule/evidence/field/source 冻结成瞬态身份。"""

    error = failure.error
    if isinstance(error, VolumePlansSettingConflictError):
        return SchemaRetryFailureFingerprint(
            value=error.failure_fingerprint,
            retryable=error.retryable_in_volume,
        )
    if isinstance(error, VolumePlansContractError):
        contract_payload = [
            _volume_contract_failure_payload(item) for item in error.failures
        ]
        return SchemaRetryFailureFingerprint(value=_canonical_sha256(contract_payload))
    if isinstance(error, ValidationError):
        schema_failures = [
            {
                "rule": item["type"],
                "evidence": _canonical_text(str(item["msg"])),
                "field": ".".join(str(part) for part in item["loc"]),
                "source": "volume_candidate",
                "input": _canonical_failure_input(item.get("input")),
            }
            for item in error.errors()
        ]
        object_failures = collect_volume_object_source_failures(
            failure.data,
            contract,
        )
        schema_payload: dict[str, Any] = {"schema": schema_failures}
        if object_failures:
            schema_payload["object_source"] = [
                _volume_contract_failure_payload(item) for item in object_failures
            ]
        return SchemaRetryFailureFingerprint(value=_canonical_sha256(schema_payload))
    return None


def build_volume_plans(
    *,
    premise: str,
    world: WorldOutput,
    characters: CharactersOutput,
    plot: PlotOutput,
    total_volumes: int,
    chapters_per_volume: int,
    tension_template: TensionTemplateName,
    genre: str = "",
    router: ModelRouter | None = None,
    client: LLMClient | None = None,
    model: str | None = None,
    max_tokens: int = _DEFAULT_MAX_TOKENS,
    temperature: float = _DEFAULT_TEMPERATURE,
    prompts_dir: Path | None = None,
    consistency_retry_feedback: str | None = None,
    selection_short_arc: bool = False,
    timing_context: dict[str, Any] | None = None,
) -> VolumePlansOutput:
    """OutlineBuilder 第 4 段:基于 premise + world + characters + plot
    生成**全书卷规划骨架**(每卷 ≈ chapters_per_volume,保跨卷一致)。

    v2 W3.5-s3b:本段只产骨架;长度感知拆章(重头戏多分章)由
    ``refine_volume_plans_length_aware`` 在本段后做聚焦逐卷细化。

    :param plot: 上一阶段产出。
    :param total_volumes: 目标卷数(>= 1,严格 == ``volumes`` 数组长度;
        本 agent 不前置校验,留 caller 检查或在 ``output_model`` 层校验)。
    :param chapters_per_volume: 每卷章数(>= 1)。
    :param tension_template: 张力曲线模板(4 档之一,作为 LLM 建议参数,
        不严格校验输出每个 volume 的 ``tension_template`` 字段命中此模板)。
    其余参数同前三段。

    :return: ``VolumePlansOutput`` Pydantic 实例。
    """
    if not premise.strip():
        raise ValueError("OutlineBuilder.build_volume_plans: premise 不能为空")
    if total_volumes < 1:
        raise ValueError(
            f"OutlineBuilder.build_volume_plans: total_volumes must be >= 1, "
            f"got {total_volumes}"
        )
    if chapters_per_volume < 1:
        raise ValueError(
            f"OutlineBuilder.build_volume_plans: chapters_per_volume must be >= 1, "
            f"got {chapters_per_volume}"
        )

    contract = build_volume_plans_contract(
        characters=characters,
        plot=plot,
        total_volumes=total_volumes,
        chapters_per_volume=chapters_per_volume,
        genre=genre,
        selection_short_arc=selection_short_arc,
    )
    if world.story_intent is not None and world.story_intent.authoring_plan is not None:
        contract = replace(contract, chapter_count_min=1, chapter_count_max=200)
    resolved_setting_contract = SettingContract.from_world_output(world)
    setting_gate_attempt = 0
    system_text = _load_stage_prompt("volume_plans", prompts_dir)
    user_text = _append_stage_marker(
        _build_volume_plans_user_message(
            premise=premise,
            genre=genre,
            world=world,
            characters=characters,
            plot=plot,
            total_volumes=total_volumes,
            chapters_per_volume=chapters_per_volume,
            tension_template=tension_template,
            contract=contract,
            consistency_retry_feedback=consistency_retry_feedback,
        ),
        stage="volume_plans",
    )
    delta_authority_payload = {
        "premise": premise,
        "genre": genre,
        "world": world.model_dump(mode="json"),
        "characters": characters.model_dump(mode="json"),
        "plot": plot.model_dump(mode="json"),
        "total_volumes": total_volumes,
        "chapters_per_volume": chapters_per_volume,
        "tension_template": tension_template,
        "selection_short_arc": selection_short_arc,
    }
    delta_authority = FrozenAuthority.freeze(
        stage="volume_plans",
        payload=delta_authority_payload,
    )

    def _assemble_volume_delta(data: Any) -> Any:
        artifact = assemble_volume_plans_delta_v4(
            delta=VolumePlansCreativeDeltaV4.model_validate(data),
            characters=characters,
            plot=plot,
            total_volumes=total_volumes,
            chapters_per_volume=chapters_per_volume,
            tension_template=tension_template,
            selection_short_arc=selection_short_arc,
            authority=delta_authority,
            current_authority=delta_authority_payload,
        )
        _log.info(
            "creative_delta_assembled",
            stage="volume_plans",
            authority_hash=artifact.authority_hash,
            delta_hash=artifact.delta_hash,
            full_leaf_count=artifact.full_leaf_count,
            model_delta_leaf_count=artifact.model_delta_leaf_count,
        )
        return artifact.value.model_dump(mode="json")

    def _format_volume_schema_feedback(
        error: ValidationError,
        raw_data: Any,
    ) -> str:
        base = format_validation_error_feedback(error)
        source_failures = collect_volume_object_source_failures(raw_data, contract)
        return base + format_volume_object_source_failures_for_retry(source_failures)

    def _check_volume_contract(out: BaseModel) -> None:
        """post-validation:结构契约 + 权威世界规则写盘前闸。

        继承旧 volumes 长度检查,并前移 linked_* 来源、骨架章数范围、
        arc/beat 覆盖等可确定约束。失败时进入 ``_run_stage`` 现有有界
        retry,只重出 ``volume_plans``,不重跑 world / characters / plot。
        """
        assert isinstance(out, VolumePlansOutput)
        authoring = world.story_intent.authoring_plan if world.story_intent else None
        if authoring is not None:
            from src.core.rules.authoring_plan import validate_authoring_layout
            validate_authoring_layout(out, authoring)
        if world.story_intent is not None and world.story_intent.version >= 5:
            for volume in out.volumes:
                for arc in volume.arcs:
                    for beat in arc.beats:
                        if beat.dramatic_contract is not None and beat.dramatic_contract.contract_version != 2:
                            raise ValueError("新经历规划必须使用事实／表达分离的戏剧合同v2")
        failures = validate_volume_plans_contract(out, replace(contract, chapter_count_min=1, chapter_count_max=200) if authoring else contract)
        if failures:
            raise VolumePlansContractError(failures)
        signature_overlaps = find_pending_signature_overlaps(
            volume_plans=out, characters=characters,
        )
        if signature_overlaps:
            raise ValueError(signature_overlaps[0].feedback())
        repeated_signatures = find_repeated_completed_signatures(
            volume_plans=out, characters=characters,
        )
        if repeated_signatures:
            raise ValueError(repeated_signatures[0].feedback())

        nonlocal setting_gate_attempt
        if not (resolved_setting_contract.unique_rules.strip() or (
            uses_experimental_outline(router, client)
            and (resolved_setting_contract.setting.strip() or resolved_setting_contract.power_system.strip())
        )):
            return
        setting_gate_attempt += 1
        setting_result = validate_setting_consistency(
            chapter_text=_render_volume_plans_setting_evidence(out),
            contract=resolved_setting_contract,
            chapter_id="outline-volume-plans",
            workspace_id=(
                str(timing_context.get("workspace_id"))
                if timing_context and timing_context.get("workspace_id")
                else None
            ),
            router=router,
            client=client,
            model=model,
            prompts_dir=prompts_dir,
            validation_scope="volume_plans",
        )
        hard = prewriting_blockers(setting_result)
        if hard:
            attributions = _attribute_volume_setting_violations(
                violations=hard,
                plot=plot,
                volume_plans=out,
            )
            _log.error(
                "prewriting_semantic_gate_failed",
                failure_layer="volume_plans",
                constraint_ids=[v.constraint_id for v in hard],
                contract_hash=resolved_setting_contract.source_hash,
                attempt=setting_gate_attempt,
                source_stages=[item.source_stage for item in attributions],
                candidate_fields=[item.candidate_field for item in attributions],
            )
            raise VolumePlansSettingConflictError(
                violations=hard,
                contract_hash=resolved_setting_contract.source_hash,
                attempt=setting_gate_attempt,
                attributions=attributions,
            )
        if setting_result.violations:
            _log.warning(
                "prewriting_semantic_gate_soft",
                failure_layer="volume_plans",
                constraint_ids=[v.constraint_id for v in setting_result.violations],
                contract_hash=resolved_setting_contract.source_hash,
                attempt=setting_gate_attempt,
            )

    result = _run_stage(
        stage="volume_plans",
        system_text=system_text,
        user_text=user_text,
        output_model=VolumePlansOutput,
        handoff_data={**delta_authority_payload, "volume_contract": contract,
                      "consistency_retry_feedback": consistency_retry_feedback},
        router=router,
        client=client,
        model=model,
        max_tokens=max_tokens,
        temperature=temperature,
        post_validate=_check_volume_contract,
        schema_validation_feedback=_format_volume_schema_feedback,
        failure_fingerprint=lambda failure: _volume_schema_failure_fingerprint(
            failure,
            contract=contract,
        ),
        non_retryable_exceptions=(SettingConsistencyTechnicalError,),
        timing_context=timing_context,
        timing_fields={
            "retry_feedback": consistency_retry_feedback is not None,
        },
        load_experimental_system_text=(lambda: _load_delta_stage_prompt("volume_plans_authoring_v2.md", prompts_dir)) if world.story_intent and world.story_intent.authoring_plan else None,
        delta_spec=None if world.story_intent and world.story_intent.authoring_plan else _OutlineDeltaSpec(
            output_model=FactualVolumePlansDelta if world.story_intent is not None and world.story_intent.version >= 5 else VolumePlansCreativeDeltaV4,
            load_system_text=lambda: _load_delta_stage_prompt(
                "volume_plans_delta_range_v1.md", prompts_dir
            ),
            load_complete_system_text=lambda: _load_delta_stage_prompt(
                "volume_plans_narrative_v9.md" if world.story_intent is not None and world.story_intent.version >= 6 else "volume_plans_narrative_v8.md" if world.story_intent is not None and world.story_intent.version == 5 else "volume_plans_narrative_v7.md" if world.story_intent is not None and world.story_intent.version == 4 else "volume_plans_narrative_v6.md" if world.story_intent is not None else "volume_plans_delta_v7.md", prompts_dir
            ),
            load_user_text=lambda: _append_stage_marker(
                _build_volume_plans_user_message(
                    premise=premise,
                    genre=genre,
                    world=world,
                    characters=characters,
                    plot=plot,
                    total_volumes=total_volumes,
                    chapters_per_volume=chapters_per_volume,
                    tension_template=tension_template,
                    contract=contract,
                    consistency_retry_feedback=consistency_retry_feedback,
                    required_slot_delta=True,
                    range_feasibility_delta=True,
                ),
                stage="volume_plans",
            ),
            load_complete_user_text=lambda: _append_stage_marker(
                _build_volume_plans_user_message(
                    premise=premise,
                    genre=genre,
                    world=world,
                    characters=characters,
                    plot=plot,
                    total_volumes=total_volumes,
                    chapters_per_volume=chapters_per_volume,
                    tension_template=tension_template,
                    contract=contract,
                    consistency_retry_feedback=consistency_retry_feedback,
                    required_slot_delta=True,
                    range_feasibility_delta=True,
                ),
                stage="volume_plans",
            ) + render_story_authority_context(world=world, characters=characters),
            assemble_data=_assemble_volume_delta,
            schema_transform=build_volume_plans_delta_v5_schema_transform(
                total_volumes=contract.total_volumes,
                chapters_per_volume=chapters_per_volume,
                allowed_phase_ids=contract.allowed_phase_ids,
                allowed_turning_ids=contract.allowed_turning_ids,
                allowed_thread_ids=contract.allowed_thread_ids,
                allowed_character_ids=contract.allowed_character_ids,
                allowed_object_ids=(
                    item.object_id for item in contract.load_bearing_objects
                ),
                require_dramatic_promise=contract.require_dramatic_promise,
                selection_short_arc=contract.selection_short_arc,
            ),
        ),
    )
    assert isinstance(result, VolumePlansOutput)
    return result


def _build_length_section(
    length_calibration: GenreCalibration,
    target_word_count_max: int,
) -> str:
    """长度感知参考段(W3.5-s3a 机制;s3b 从全书卷规划移到卷内细化复用)。

    用归一化 chars_per_beat(每核心情节单元字数,拆章前后稳定),不用裸
    chars_per_chapter(隐含"未拆章每章单元数"密度、拆章后失效)。caller 已判
    (length_calibration, target_word_count_max) 均非 None 才调本函数。
    """
    cpb = length_calibration.chars_per_beat
    if cpb <= 0 or target_word_count_max <= 0:
        raise ValueError("长度校准与目标上限必须为正")
    units_cap = target_word_count_max / cpb
    observed = length_calibration.chars_per_chapter
    relation = "超过" if observed > target_word_count_max else "未超过"
    return (
        "\n\n## 长度感知参考（本模型本题材样本，非质量证明或硬公式）\n\n"
        f"- 样本每核心情节单元约 {cpb:.0f} 字，"
        f"每章约 {length_calibration.content_units_per_chapter:.1f} 个单元，"
        f"平均正文 {observed:.0f} 字，{relation}当前上限 {target_word_count_max} 字。\n"
        f"- 按样本密度折算，{units_cap:.1f} 个单元接近上限；"
        "这不证明每个单元已充分演出，也不要求每章都达到同一密度。\n"
        "- 依据既有事件的戏剧分量判断是否重排：密集且难以完整展开时可分章，"
        "稀薄时应避免把同一事件继续拆细；无需重排时保持原范围。"
        "不能把校准观察当成必须增章的命令，也不能靠预算反推并虚构情节数量。"
    )


def _build_volume_plans_user_message(
    *,
    premise: str,
    genre: str,
    world: WorldOutput,
    characters: CharactersOutput,
    plot: PlotOutput,
    total_volumes: int,
    chapters_per_volume: int,
    tension_template: TensionTemplateName,
    contract: VolumePlansContract | None = None,
    consistency_retry_feedback: str | None = None,
    include_promise_outcome_contract: bool = True,
    required_slot_delta: bool = False,
    range_feasibility_delta: bool = False,
) -> str:
    """volume_plans 段 user message — 字段精简引用上游 ID,避免上下文爆炸。

    v2 W3.5-s3b:本段产**全书骨架**(每卷 chapter_count ≈ chapters_per_volume),
    保跨卷一致性(arc 衔接 / forbidden_advance 防剧透 / tension 形状)。长度感知
    拆章(重头戏多分章)由 ``refine_volume_plans_length_aware`` 在本段之后做聚焦
    逐卷细化 —— s3a 实证全书一次拆维度B 摇摆,聚焦单卷才稳(维护人 2026-06-16 拍板)。
    """
    genre_text = genre if genre.strip() else "(未指定)"
    character_id_lines = [
        f"  - {c.character_id}({c.role})" for c in characters.characters
    ]
    phase_lines = [f"  - {p.phase_id}:{p.title}｜{p.summary}" for p in plot.main_phases]
    turning_lines = [
        f"  - {t.turning_id}:{t.title}｜{t.description}"
        for t in plot.major_turning_points
    ]
    thread_lines = [
        f"  - {th.thread_id}:{th.title}｜{th.description}" for th in plot.long_threads
    ]
    # 补强1(v2 W3.5):命名锁定表前置注入(纵深 —— volume_plans 引用 plot
    # 内容可能已含错名,再次声明 canonical 命名)。无 buried 名时返 "" 不扰动。
    retry_section = _build_outline_consistency_retry_section(
        consistency_retry_feedback=consistency_retry_feedback,
        scope="本次只能重出 volume_plans;world / characters / plot 是上游事实,"
        "不可推翻或改写。请调整卷规划的地点、角色行动、牺牲归属、linked_* "
        "引用等,让它们严格对齐上游 plot。",
    )
    contract_section = (
        format_volume_plans_contract_for_prompt(
            contract,
            include_promise_outcome_contract=include_promise_outcome_contract,
            required_slot_delta=required_slot_delta,
            range_feasibility_delta=range_feasibility_delta,
        )
        if contract is not None
        else ""
    )
    feasible_normal_arc_max = (
        min(4, chapters_per_volume // 2) if range_feasibility_delta else 4
    )
    output_shape_instruction = (
        "\n\n- selection_short_arc=true（仅本次内部 selection 合同）。\n"
        "请生成 selection_test 短活动卷规划:`volumes` 长度严格 == "
        f"{total_volumes}，chapter_count 严格 == {chapters_per_volume}；"
        "每卷恰好 3 个非副本单章 arc，顺序覆盖第1/2/3章，"
        "arc_type 必须依次逐字为“起因”、“升级”、“高潮/交代”，phase 必须"
        "依次为 opening/rising/climax；每 arc 恰好 1 个同范围 beat，"
        "tension_curve 长度严格 == 3。"
        if contract is not None and contract.selection_short_arc
        else "\n\n- selection_short_arc=false（普通产品合同）。\n"
        "请生成卷规划:`volumes` 长度严格 == "
        + f"{total_volumes},每卷 chapter_count ≈ {chapters_per_volume}"
        + f",每卷 2-{feasible_normal_arc_max} 个 arc"
        + ",arc 间连续不重叠覆盖全卷,tension_curve 长度严格 == chapter_count。"
    )
    return (
        build_name_lock_header(characters) + "## 用户输入\n\n"
        f"- 一句话设定(premise):{premise}\n"
        f"- 题材(genre):{genre_text}\n"
        f"- 目标卷数(total_volumes):{total_volumes}\n"
        f"- 每卷章数(chapters_per_volume):{chapters_per_volume}\n"
        f"- 张力曲线模板(tension_template):{tension_template}\n\n"
        "## 上游产出:世界观摘要(world,只读)\n\n"
        f"- setting:{world.setting}\n"
        f"- factions:{world.factions}\n\n"
        "## 卷规划写前世界规则合同(HARD)\n\n"
        f"- 权威规则原文:{world.unique_rules}\n"
        "- 每个卷、arc、beat 都必须逐项保持原规则的触发条件、作用对象、"
        "数量 / 范围和效果。\n"
        "- 封存地点、击败反派、完成调查或进入收束，不等于世界规则本身失效；"
        "不得让规则被改变、移除、逆转、暂停、重置或绕过，除非权威规则原文明示"
        "这种条件。\n"
        "- 若规则自动决定对象，卷规划不得改成角色选择自己、同伴、指定人物、"
        "群体、类别或概念；角色安排触发条件不等于能指定结果。\n"
        "- 多次使用必须是多次独立合规触发，每次仍守原对象选择和单次数量；"
        "不得合并成范围效果。\n\n"
        "## 上游产出:核心角色 ID(characters,只读)\n\n"
        + "\n".join(character_id_lines)
        + "\n\n## 上游产出:主线核心(plot,只读)\n\n"
        f"- core_conflict:{plot.core_conflict}\n\n"
        "### main_phases(供 linked_phases 引用)\n"
        + "\n".join(phase_lines)
        + "\n\n### major_turning_points(供 linked_turning_points 引用)\n"
        + "\n".join(turning_lines)
        + "\n\n### long_threads(供 linked_threads 引用)\n"
        + "\n".join(thread_lines)
        + contract_section
        + retry_section
        + output_shape_instruction
    )


def _build_outline_consistency_retry_section(
    *, consistency_retry_feedback: str | None, scope: str
) -> str:
    """把 outline_consistency 的硬伤反馈注入下一轮卷规划生成。

    只在主流程一致性校验失败后的有界重试使用。空反馈返回空串,保持首轮
    prompt 逐字节兼容既有 happy path。
    """
    if not consistency_retry_feedback:
        return ""
    return (
        "\n\n## 上次大纲跨段一致性校验失败(本轮必须修复)\n\n"
        f"{consistency_retry_feedback}\n\n"
        f"## 本轮修订边界\n\n{scope}\n"
    )


def _format_arc_span_failures_for_retry(
    failures: list[OutlineValidationFailure],
) -> str:
    """兼容旧私有 helper 名;实现委托给 outline rules 层单一 formatter。"""
    return format_arc_span_failures_for_retry(failures)


def _density_anchored_target_chapters(
    skeleton_cc: int,
    length_calibration: GenreCalibration,
    target_word_count_max: int,
) -> int:
    """W3.5-s3b 密度锚定:把骨架卷章数按 (模型,题材) 演出密度折算成细化目标章数。

    量纲自洽推导:骨架每章约 ``content_units_per_chapter`` 个核心情节单元(实测 ≈3,
    渲染超上限);要把每章压到接近上限的 ``units_cap = word_count_max / chars_per_beat``
    个单元(deepseek action ≈2.5),所需章数 = 总单元数 / units_cap =
    ``skeleton_cc × content_units_per_chapter / units_cap``(deepseek action ≈ ×1.2;
    introspection chars_per_beat 低 → units_cap 高 → 系数 ≈1.0 自动少拆)。

    **为何需要这个数(s3b 实证)**:细化器无总量锚时对每个重头戏慷慨多分 → 小卷过度拆分
    翻倍(真实 5卷×6章 30→61/68)。给一个**正面瞄准目标 N**(非反面"别超 X" —— deepseek
    会锚定反面数字,guard 已踩坑),让细化把扩出的章优先给重头戏、整卷贴着密度走。

    返回值 **下限钳到 skeleton_cc**(细化只增不减:若某模型写得短到 units_cap ≥ 实测密度
    使 factor < 1,仍保骨架结构最小章数,不缩卷)。
    """
    units_cap = target_word_count_max / length_calibration.chars_per_beat
    target = round(
        skeleton_cc * length_calibration.content_units_per_chapter / units_cap
    )
    return max(target, skeleton_cc)


def uses_experimental_outline(
    router: ModelRouter | None, client: LLMClient | None = None
) -> bool:
    """The explicit v6 contract plans from story material, not sample density."""
    if router is not None:
        return getattr(router, "pipeline_contract", None) == EXPERIMENTAL_PIPELINE_CONTRACT
    return client is not None and uses_experimental_pipeline(client)


def uses_split_outline_review(
    router: ModelRouter | None, client: LLMClient | None, model: str | None,
) -> bool:
    """Keep the V4.1 review experiment off other frozen model pipelines."""
    if not uses_experimental_outline(router, client):
        return False
    if router is not None:
        return router.select_profile("outline_consistency_validator").model == "deepseek-flash"
    return model == "deepseek-flash"


def _build_volume_refine_user_message(
    *,
    volume: VolumePlanItem,
    genre: str,
    length_calibration: GenreCalibration | None,
    target_word_count_max: int,
    consistency_retry_feedback: str | None = None,
    selection_short_arc: bool = False,
    calibrated_density_anchor: bool = True,
) -> str:
    """卷内细化 user message(W3.5-s3b):输入单卷完整结构 + 密度参考 → 重新拆章。

    渲染输入卷的 arcs / beats(含 forbidden_advance 防剧透字面)供 LLM **保留骨架
    原样、只重分配章数**;复用 ``_build_length_section`` 的密度参考(s3a 注入逻辑
    的单卷版)+ 注入**逐卷密度锚定目标章数**(``_density_anchored_target_chapters``,
    s3b 修复:正面给 N 防多卷过度拆分翻倍)。
    """
    if calibrated_density_anchor and length_calibration is None:
        raise ValueError("密度锚定的旧细化路径需要真实长度校准")
    genre_text = genre if genre.strip() else "(未指定)"
    arc_blocks: list[str] = []
    for arc in volume.arcs:
        a_s, a_e = arc.chapter_range
        beat_lines: list[str] = []
        for beat in arc.beats:
            b_s, b_e = beat.chapter_range
            drama = (
                beat.dramatic_contract.model_dump_json()
                if beat.dramatic_contract is not None
                else None
            )
            beat_lines.append(
                f"    - beat `{beat.beat_id}` 原章[{b_s},{b_e}]:{beat.summary}\n"
                f"      must_advance={list(beat.must_advance)}\n"
                f"      forbidden_advance={list(beat.forbidden_advance)}\n"
                f"      dramatic_contract={drama}"
            )
        beats_text = "\n".join(beat_lines) if beat_lines else "    (无 beats)"
        arc_blocks.append(
            f"- arc `{arc.arc_id}`(type={arc.arc_type} / phase={arc.phase} / "
            f"原章[{a_s},{a_e}]):{arc.summary}\n{beats_text}"
        )
    if length_calibration is None:
        length_section = (
            "\n\n## 本次章节篇幅\n\n"
            f"本次规划参考篇幅约 {target_word_count_max} 字。当前写作版本没有可用的长度实测，"
            "不提供样本均值或换算出的目标章数。依据本卷已有事件的戏剧分量重排；"
            "没有长度实测不代表故事材料已足够，也不代表字数或文学质量已经通过。"
        )
    elif calibrated_density_anchor:
        length_section = _build_length_section(length_calibration, target_word_count_max)
    else:
        length_section = (
            "\n\n## 当前 Writer 长度观察（不代表故事密度）\n\n"
            f"同题材 {length_calibration.sample_chapters} 份固定场景样本，平均正文 "
            f"{length_calibration.chars_per_chapter:.0f} 字；本次规划参考篇幅约 "
            f"{target_word_count_max} 字。样本预算不是实际情节单元计数，"
            "不得据此折算本卷故事密度或目标章数。依据已有事件的戏剧分量重排。"
        )
    skeleton_cc = volume.chapter_count
    target_cc = (
        _density_anchored_target_chapters(
            skeleton_cc, length_calibration, target_word_count_max
        )
        if calibrated_density_anchor and length_calibration is not None else skeleton_cc
    )
    target_section = (
        "\n\n## selection_test 固定三章复核（优先于密度扩章）\n\n"
        "- selection_short_arc=true（仅本次内部 selection 合同）。\n"
        "- 本卷必须继续严格为 3 章，不得按密度增加或减少章数。\n"
        "- 3 个 arc 与各自唯一 beat 的范围必须逐字保持 [1,1]/[2,2]/[3,3]。\n"
        "- 每个 arc 保持输入的唯一 beat（恰好 1 个），不得增删。\n"
        "- 本调用仍执行正式 volume refine，但只允许复核并保持冻结短结构。\n"
        if selection_short_arc
        else (
            "\n\n## 本卷密度锚定目标章数(内容驱动的瞄准点 — 你拆章的总量锚)\n\n"
            "- selection_short_arc=false；普通产品每 arc 仍为 2—4 beats。\n"
            f"- 综合上面的演出密度,本卷(原 {skeleton_cc} 章)**瞄准约 {target_cc} 章**。\n"
            f"- 在约 {target_cc} 章这个目标内分配:把多出来的章**优先给重头戏 arc**"
            "(高潮 / 重大转折 / 多线交汇 / 情感峰 → 多分章 + 宽 beat 演透),"
            "过场 / 铺垫 / 收束 arc 保持紧凑。\n"
            f"- {target_cc} 是内容密度折算出的参考瞄准点(允许 ±1 小幅浮动),请贴着它走。\n"
        )
    )
    if not calibrated_density_anchor and not selection_short_arc:
        target_section = (
            "\n\n## 依据本卷材料判断章幅\n\n"
            f"原结构为 {skeleton_cc} 章。校准样本不代表本卷拥有相同的事件密度，"
            "不要用原章数乘样本单元数，虚构本卷总剧情量，再将它作为增章目标。"
            "在保持既有 arc/beat 身份、剧情原文及范围完整性的前提下判断是否重排："
            "完整过程已足够展开则保留原范围；单个事件被切薄时不要继续细拆。"
            "本阶段只能重排，不能补写缺失剧情，也不能把重排当成质量已通过。\n"
        )
    retry_section = _build_outline_consistency_retry_section(
        consistency_retry_feedback=consistency_retry_feedback,
        scope="本次只细化当前单卷;不要改写上游 plot,也不要把终局地点、角色牺牲、"
        "关键道具完成者写成与上游相反。若反馈涉及其他卷,保持本卷与全书上游事实兼容。",
    )
    return (
        "## 待细化的单卷(全书骨架已定,保留身份 + 防剧透字面不动,只重分配章数)\n\n"
        f"- 题材(genre):{genre_text}\n"
        f"- volume_id:{volume.volume_id}\n"
        f"- title:{volume.title}\n"
        f"- summary:{volume.summary}\n"
        f"- tension_template:{volume.tension_template}\n"
        f"- linked_phases:{list(volume.linked_phases)}\n"
        f"- linked_turning_points:{list(volume.linked_turning_points)}\n"
        f"- linked_threads:{list(volume.linked_threads)}\n"
        "- dramatic_promise:"
        f"{volume.dramatic_promise.model_dump_json() if volume.dramatic_promise else None}\n"
        f"- 原 chapter_count:{volume.chapter_count}\n\n"
        "### 卷内 arcs / beats(原样保留剧情与 forbidden_advance,只重新分配章数 / beat 区间)\n\n"
        + "\n".join(arc_blocks)
        + length_section
        + target_section
        + retry_section
        + "\n\n请输出细化后的本卷:`volumes` 数组**恰好 1 个卷**(volume_id 必须 == "
        + f"{volume.volume_id}),"
        + (
            "chapter_count 严格 == 3，arc/beat 范围保持 [1,1]/[2,2]/[3,3]，"
            "tension_curve 长度严格 == 3。"
            if selection_short_arc
            else "依据已有事件重排范围，保留 arc/beat 数量与原文，并让范围连续覆盖新章数。"
            if not calibrated_density_anchor
            else f"**瞄准约 {target_cc} 章**:重头戏多分章 + 少而宽 beat、"
            "过场紧凑;arcs 数量不变、连续不重叠覆盖全卷(首 arc start=1 / "
            "末 arc end=新 chapter_count),tension_curve 长度严格 == 新 chapter_count。"
        )
    )


# ---------------------------------------------------------------------------
# Stage 4.5: refine_volume_plans_length_aware(W3.5-s3b — 卷内拆章细化)
# ---------------------------------------------------------------------------


def refine_volume_plans_length_aware(
    *,
    volume_plans: VolumePlansOutput,
    length_calibration: GenreCalibration | None,
    target_word_count_max: int,
    genre: str = "",
    router: ModelRouter | None = None,
    client: LLMClient | None = None,
    model: str | None = None,
    max_tokens: int = _DEFAULT_MAX_TOKENS,
    temperature: float = _DEFAULT_TEMPERATURE,
    prompts_dir: Path | None = None,
    on_volume_refined: Any = None,
    plot: PlotOutput | None = None,
    consistency_retry_feedback: str | None = None,
    selection_short_arc: bool = False,
    timing_context: dict[str, Any] | None = None,
    story_intent: StoryIntent | None = None,
    world: WorldOutput | None = None,
    characters: CharactersOutput | None = None,
) -> VolumePlansOutput:
    """W3.5-s3b:聚焦逐卷重新拆章(长度感知 + 戏剧权重),返回细化后 volume_plans。

    架构(维护人 2026-06-16 拍板):全书 ``build_volume_plans`` 产骨架(≈N/卷,保跨
    卷一致),本函数对**每个卷单独**做一次聚焦细化 —— s3a 实证全书一次拆"重头戏加权"
    摇摆,聚焦单卷才稳。给重头戏多分章 + 少而宽 beat、过场紧凑;**保留 arc/beat 身份
    与 forbidden_advance 防剧透字面**,只重分配 chapter_count + 章数区间 + tension。

    超原章数 → 全书 ch-NNNN 章号在 ``generate_mini_plan`` 的
    ``_derive_default_chapter_ids`` 里遍历细化后 volumes 自动顺延(后续卷章号顺移)。
    下游 commit / VolumeORM / 导出 / Run 全读细化后 volume_plans(caller 须重持久化
    volume_plans.json),章号全链路自洽。

    :param volume_plans: ``build_volume_plans`` 产的骨架(只读)。
    :param length_calibration: 实测观察；只有显式实验v6允许缺失，仍执行细化。
    :param target_word_count_max: 单章字数上限(非 None)。
    :param on_volume_refined: 单卷细化完成回调 ``(volume_id, refined_item) -> None``。
    :return: 细化后 ``VolumePlansOutput``(volumes 数量 == 输入;每卷 chapter_count
        可 >= 原值)。
    :raises OutlineBuilderError: 单卷 LLM 输出非法 / volume_id 不一致 / arc 数变化。
    """
    authoring = story_intent.authoring_plan if story_intent else None
    experimental = uses_experimental_outline(router, client)
    if length_calibration is None and not experimental:
        raise ValueError("旧细化路径缺少真实长度校准")
    template_root = (
        prompts_dir if prompts_dir is not None else settings.prompts_dir
    ).resolve()
    system_text = load_prompt_template(
        template_root / _STAGE_PROMPT_SUBDIR / "volume_refine.md"
    )

    refined: list[VolumePlanItem] = []
    for volume in volume_plans.volumes:
        user_text = _append_stage_marker(
            _build_volume_refine_user_message(
                volume=volume,
                genre=genre,
                length_calibration=length_calibration,
                target_word_count_max=target_word_count_max,
                consistency_retry_feedback=consistency_retry_feedback,
                selection_short_arc=selection_short_arc,
                calibrated_density_anchor=not experimental,
            ),
            stage="volume_plans",
        )
        delta_authority_payload = {
            "original_volume": volume.model_dump(mode="json"),
            **({"world": world.model_dump(mode="json") if world else None,
                "characters": characters.model_dump(mode="json") if characters else None,
                "plot": plot.model_dump(mode="json") if plot else None} if authoring else {}),
            **({"phase_development": [phase.model_dump(mode="json") for phase in plot.main_phases
                                      if phase.phase_id in volume.linked_phases]}
               if plot is not None and story_intent is not None and story_intent.version >= 4 else {}),
            "length_calibration": (
                length_calibration.model_dump(mode="json")
                if length_calibration is not None else None
            ),
            "target_word_count_max": target_word_count_max,
            "genre": genre,
            "selection_short_arc": selection_short_arc,
            **({"story_intent": story_intent.model_dump(mode="json")} if story_intent is not None else {}),
        }
        delta_authority = FrozenAuthority.freeze(
            stage="volume_refine",
            payload=delta_authority_payload,
        )

        def _assemble_refine_delta(
            data: Any,
            original: VolumePlanItem = volume,
            frozen: FrozenAuthority = delta_authority,
            current: dict[str, Any] = delta_authority_payload,
        ) -> Any:
            artifact = assemble_volume_refine_delta(
                delta=SingleVolumeRefineDelta.model_validate(data),
                original=original,
                authority=frozen,
                current_authority=current,
            )
            _log.info(
                "creative_delta_assembled",
                stage="volume_refine",
                volume_id=original.volume_id,
                authority_hash=artifact.authority_hash,
                delta_hash=artifact.delta_hash,
                full_leaf_count=artifact.full_leaf_count,
                model_delta_leaf_count=artifact.model_delta_leaf_count,
            )
            return artifact.value.model_dump(mode="json")

        def _check_refined(out: BaseModel, vol: VolumePlanItem = volume) -> None:
            assert isinstance(out, VolumePlansOutput)
            if len(out.volumes) != 1:
                raise ValueError(
                    f"卷内细化必须输出恰好 1 个卷,得 {len(out.volumes)} 个;"
                    f"请只输出 volume_id={vol.volume_id} 的细化结果(volumes 数组 1 元素)"
                )
            r = out.volumes[0]
            if r.volume_id != vol.volume_id:
                raise ValueError(
                    f"卷内细化输出 volume_id={r.volume_id} 与输入 {vol.volume_id} "
                    f"不一致;请保留原 volume_id"
                )
            if authoring is not None:
                from src.core.rules.authoring_plan import validate_authoring_layout, validate_rearranged_volume
                validate_rearranged_volume(vol, r, plot)
                candidate = VolumePlansOutput(volumes=refined + [r] + volume_plans.volumes[len(refined) + 1:])
                validate_authoring_layout(candidate, authoring)
                return
            if len(r.arcs) != len(vol.arcs):
                raise ValueError(
                    f"卷内细化 arc 数量 ({len(r.arcs)}) 必须 == 输入 ({len(vol.arcs)});"
                    f"只重新分配章数 / beat 区间,不增删 arc"
                )
            if selection_short_arc and (
                r.chapter_count != 3
                or [arc.chapter_range for arc in r.arcs]
                != [arc.chapter_range for arc in vol.arcs]
                or [beat.chapter_range for arc in r.arcs for beat in arc.beats]
                != [beat.chapter_range for arc in vol.arcs for beat in arc.beats]
            ):
                raise ValueError(
                    "selection_test volume refine 不得改变固定 3 章或 arc/beat 范围"
                )
            failures = validate_refined_volume_contract(
                original=vol,
                refined=r,
            )
            if failures:
                raise ValueError(
                    format_volume_plans_contract_failures_for_retry(failures)
                )

        result = _run_stage(
            stage="volume_plans",
            system_text=system_text,
            user_text=user_text,
            output_model=VolumePlansOutput,
            handoff_data={**delta_authority_payload,
                          "consistency_retry_feedback": consistency_retry_feedback},
            router=router,
            client=client,
            model=model,
            max_tokens=max_tokens,
            temperature=temperature,
            post_validate=_check_refined,
            timing_context=timing_context,
            timing_fields={
                "substage": "volume_refine",
                "volume_id": volume.volume_id,
                "retry_feedback": consistency_retry_feedback is not None,
            },
            load_experimental_system_text=(lambda: _load_delta_stage_prompt("volume_refine_authoring_v2.md", prompts_dir)) if authoring else None,
            delta_spec=None if authoring else _OutlineDeltaSpec(
                output_model=SingleVolumeRefineDelta,
                load_system_text=lambda: _load_delta_stage_prompt(
                    "volume_refine_delta.md",
                    prompts_dir,
                ),
                load_complete_system_text=lambda: _load_delta_stage_prompt(
                    "volume_refine_narrative_v8.md" if story_intent is not None and story_intent.version >= 6 else "volume_refine_narrative_v7.md" if story_intent is not None and story_intent.version == 5 else "volume_refine_narrative_v6.md" if story_intent is not None and story_intent.version == 4 else "volume_refine_narrative_v5.md" if story_intent is not None else "volume_refine_delta_v6.md", prompts_dir
                ),
                load_complete_user_text=lambda: _append_stage_marker(
                    _build_volume_refine_user_message(
                        volume=volume, genre=genre, length_calibration=length_calibration,
                        target_word_count_max=target_word_count_max,
                        consistency_retry_feedback=consistency_retry_feedback,
                        selection_short_arc=selection_short_arc,
                        calibrated_density_anchor=False,
                    ), stage="volume_plans",
                ),
                assemble_data=_assemble_refine_delta,
            ),
        )
        assert isinstance(result, VolumePlansOutput)
        refined_item = result.volumes[0]
        refined.append(refined_item)
        _log.info(
            "volume_refine_completed",
            volume_id=volume.volume_id,
            chapter_count_before=volume.chapter_count,
            chapter_count_after=refined_item.chapter_count,
        )
        if on_volume_refined is not None:
            on_volume_refined(volume.volume_id, refined_item)

    return VolumePlansOutput(volumes=refined)


# ---------------------------------------------------------------------------
# Stage 5: generate_mini_plan(v1.0.x wave 4 session 1 — 章级事件分配)
# ---------------------------------------------------------------------------


# mini_plan 单卷 LLM 调用的 max_tokens 上限(单卷 30 章 × 60 字 + linked
# arrays = ~3K char ≈ 4-5K token,留 2x 余量)。
_MINI_PLAN_PER_VOLUME_MAX_TOKENS = 6000


def _structural_profile_for_genre(genre: str) -> str:
    """据 genre 返回**题材结构 profile** 块,注入 mini_plan user message(M4''',Q2)。

    题材感知核心(postmortem §4 Q2):每题材戏剧引擎不同 → 该有的结构多样性 + 红线
    不同,通用"结构多样杠杆"是错的(一刀切)。本函数把题材的"默认结构 / 偏离加分项 /
    红线"参数化,与 system prompt 的**通用** function→focus 原则配对(static 原则 +
    dynamic 题材 profile,镜像 craft_section + craft_focus 分工)。

    **防过度建造(M4 教训)**:w5-s6 原型**只内化悬疑 profile**(B1 验证题材),w5-s8 加
    **治愈日常 / 慢生活 profile**(M4''-轴A 验证题材);其余题材走通用兜底(守核心钩子 +
    情感峰别稀释),具体 profile 等机制盲读验证通过再逐题材铺(避免凭空建 6 题材配额重蹈 M4)。

    **题材分两类引擎(w5-s8)**:**紧 beat**(悬疑 / 推理:强剧情引擎,病在轴B 程序形态,由
    约束 9 chapter_focus 处理)vs **松 beat**(治愈日常 / 慢生活:无强剧情引擎,病在轴A
    "独处内省扎堆",由约束 11 interaction_focus 处理)。profile 据此标注本书属哪类引擎,
    让 mini_plan 知道启用哪条约束(避免悬疑 profile 把治愈日常往"内心崩溃单焦"误推)。

    :param genre: 工作区 genre 文本(可能 ``"都市悬疑"`` / ``"治愈日常"`` / ``""`` 等)。
        空 → 返回通用兜底(不假设题材)。
    """
    g = genre.strip()
    genre_identity = canonical_genre_identity(g)
    # 治愈日常 / 慢生活:松 beat,情感流引擎(M4''-轴A 验证题材,profile 已内化)
    if genre_identity == "healing_daily":
        return (
            "## 题材结构 profile(治愈日常 / 慢生活 — 务必遵守,M4''-轴A 题材感知)\n\n"
            "**戏剧引擎类型:松 beat / 互动驱动**(无悬疑那种'破案'强剧情引擎)。读者的"
            "爽点 = 人与人之间的温暖互动、被治愈的暖意、生活质感。**因此本书启用约束 11"
            "(interaction_focus 互动驱动),不启用悬疑式'内心崩溃单焦'。**\n\n"
            "**默认导向(本题材核心:戏剧引擎默认走向真实互动)**:松 beat 没有强剧情引擎"
            "拽着走,引擎一偷懒就让主角**独自做事 / 独自感慨**,且反复刷同一内省母题 → "
            "**'独处内省扎堆'是本题材最大的结构病**。默认导向 = **优先让另一个角色作为真人"
            "在场**(把'主角独自做某事'尽量改成'和某人一起做 / 被某人见证反应 / 温和分歧'),"
            "互动中心章填 `interaction_focus`(见约束 11)。\n\n"
            "**偏离加分(高价值章)**:\n"
            "- **关系积累 / 情感峰** → **互动聚焦 / 对手戏**:把分量放在主角与关键角色(知己 / "
            "暖心长者 / 反派)的一场真实双向戏上,情感从互动里长出来。**注意:本题材的情感峰"
            "是'两个人之间的暖意 / 和解',不是悬疑式'独处内心崩溃'——别把情感峰写成主角一个人"
            "崩溃 / 内省**(那会放大本题材的病)。\n"
            "- **群像** → 小镇集体场景(夜读会 / 集市 / 节庆)让多个角色同台,展现人情味。\n\n"
            "**红线(违反 = 毁题材,绝不可做)**:\n"
            "- **护治愈基调**:**别为戏剧性制造尖锐残酷的冲突 / 反转**抓眼球 —— 矛盾多源于"
            "误解 / 成长,温和分歧可以,残酷对立不行。\n"
            "- **护留白**:治愈日常需要安静的留白(独自顿悟 / 一顿饭 / 一个黄昏)。**内容驱动"
            "非配额**:'默认走互动'是治'独处扎堆',不是'每章都热闹'——该独处留白的章保留,"
            "`interaction_focus` 留空。\n"
        )
    # 悬疑 / 推理 / 侦探:谜题 + 误导引擎(B1 验证题材,profile 已内化)
    if genre_identity == "mystery":
        return (
            "## 题材结构 profile(悬疑 / 推理 — 务必遵守,M4''' 题材感知)\n\n"
            "**戏剧引擎**:谜题 + 误导。读者的爽点 = 跟主角一起拼线索、被误导、最后恍然。\n\n"
            "**默认结构(本题材核心钩子,绝大多数章保持 → chapter_focus 留空)**:若本书是"
            "双时空 / 多线设定,默认是**双线互证**(两界 / 多线线索互相呼应印证,这是悬疑"
            "张力来源);若单线推理,默认**单线递进**(线索逐步投放)。\n\n"
            "**偏离加分(chapter_focus 该非空的少数高价值章)**:\n"
            "- **情感峰**(核心角色死亡 / 重大牺牲 / 情感重击)→ **情感单焦**:把分量集中"
            "在读者的情感锚点(一个世界 / 一条线),另一线退为简短回响 —— **别用双线 / 多线"
            "对半并行稀释全书最该重击的情感**(这是本题材最常见的结构性质量损失)。\n"
            "- **反派动机 / 前史揭示** → **backstory 单焦**:给反派或关键角色一章把'他为何"
            "如此'的前史 / 动机讲透,别永远只当幕后符号。\n"
            "- **绝境 / 困局升级** → **纯单线压迫**:把主角锁在一个高压情境里,单线写到底升"
            "高张力。\n\n"
            "**红线(违反 = 毁题材,绝不可做)**:\n"
            "- **POV 克制**:**绝不**新增'反派视角章' / '知情者视角章'去解释谜底 —— 悬疑靠"
            "信息差,提前从反派 / 知情者视角揭示 = 泄底,毁掉推理张力。结构偏离只在**主角"
            "视角内**调整焦点,不切换到会泄底的视角。\n"
            "- **别过度纯单线**:双线 / 多线互证是本题材核心钩子,偶尔单焦服务情感峰可以,但"
            "**别把大多数章改成纯单线**而丢掉两界 / 多线呼应的悬疑张力。\n"
        )
    # 通用兜底(题材未内化 profile / genre 空):守核心钩子 + 情感峰别稀释
    genre_label = g if g else "(未指定)"
    return (
        f"## 题材结构 profile(genre={genre_label} — 通用兜底,M4''')\n\n"
        "本题材尚未内化专属结构 profile,按**通用原则**:结构跟戏剧功能走,但**守住本题材"
        "的核心钩子**(默认结构,绝大多数章保持 → chapter_focus 留空)。**偏离加分**(少数"
        "高价值章 → chapter_focus 非空):情感峰(角色死亡 / 重大牺牲)→ 情感单焦,**别被"
        "多线 / 双线对半稀释**;角色重大转折(信仰崩塌 / 黑化等)→ 角色单焦给专属戏。"
        "**保守**:无具体红线时,偏离只做'把分量集中到该集中处',不引入新视角 / 新结构"
        "实验(避免破坏题材默认体验)。\n"
    )


class _SingleVolumeMiniPlanOutput(BaseModel):
    """单卷 mini_plan LLM 出参中间模型(分卷生成内部用)。

    items 长度由 caller 校验(== 本卷 chapter_count)。复用
    ``MiniPlanItem`` 字段校验 + chapter_id 唯一性。
    """

    items: list[MiniPlanItem]


def _render_mini_plan_setting_evidence(items: list[MiniPlanItem]) -> str:
    """把单卷临时结果稳定渲染为写前 setting judge 可逐字回查的证据文本。"""

    payload = [
        {
            "chapter_id": item.chapter_id,
            "one_line_event": item.one_line_event,
            "info_increment": item.info_increment,
            "chapter_focus": item.chapter_focus,
            "interaction_focus": item.interaction_focus,
            "breakthrough_payoff": item.breakthrough_payoff,
            **({"event_contract": item.narrative_context.event_contract.model_dump(mode="json")}
               if item.narrative_context is not None and item.narrative_context.event_contract is not None else {}),
        }
        for item in items
    ]
    return json.dumps(
        payload,
        ensure_ascii=False,
        sort_keys=True,
        separators=(",", ":"),
    )


def generate_mini_plan(
    *,
    world: WorldOutput,
    setting_contract: SettingContract | None = None,
    characters: CharactersOutput,
    plot: PlotOutput,
    volume_plans: VolumePlansOutput,
    genre: str = "",
    chapter_id_by_volume_and_number: dict[tuple[str, int], str] | None = None,
    router: ModelRouter | None = None,
    client: LLMClient | None = None,
    model: str | None = None,
    max_tokens: int = _MINI_PLAN_PER_VOLUME_MAX_TOKENS,
    temperature: float = _DEFAULT_TEMPERATURE,
    prompts_dir: Path | None = None,
    on_volume_complete: Any = None,
    on_parallel_status: Callable[[dict[str, Any]], None] | None = None,
    breakthrough_payoff_enabled: bool = False,
    selection_short_arc: bool = False,
    timing_context: dict[str, Any] | None = None,
    consistency_coverage_gaps: list[str] | None = None,
    candidate_checkpoint: GenerationCheckpoint | None = None,
) -> MiniPlanOutput:
    """OutlineBuilder 第 5 段:分卷生成章级事件分配。

    架构:N 卷 N 次 LLM 调用,**滚动上下文** — 下一卷生成时把上一卷
    mini_plan items 的末尾 5 章作为"承接语境"注入,让 LLM 在分卷边界
    保持衔接(对齐维护人 2026-05-21 拍板决策 1 = A 分卷生成 + 滚动上下文)。

    :param world: 第 1 段产出(只读)
    :param characters: 第 2 段产出(只读)
    :param plot: 第 3 段产出(只读)
    :param volume_plans: 第 4 段产出(只读) — 内含 volumes + arcs + tension_curve
    :param chapter_id_by_volume_and_number: caller-provided (volume_id,
        chapter_number) → 全书 chapter_id 映射。``None`` 时按
        ``ch-{n:04d}`` 全书 1-based 派生(对齐既有 chapter_id 命名约定)。
    :param on_volume_complete: 单卷完成回调,签名
        ``(volume_id: str, items: list[MiniPlanItem]) -> None``。用于 caller
        在长链调用中做进度日志(非 WS,WS 只发 stage="mini_plan" 一次)。
    :return: ``MiniPlanOutput`` 含全书全章 items(按 (volume, chapter_number)
        排序)。
    :raises OutlineBuilderError: 单卷 LLM 输出非合法 JSON / schema 不符 /
        items 长度不等于本卷 chapter_count / chapter_id 越界。
    """
    chapter_id_map = chapter_id_by_volume_and_number
    if chapter_id_map is None:
        chapter_id_map = _derive_default_chapter_ids(volume_plans)
    resolved_setting_contract = setting_contract or SettingContract.from_world_output(
        world
    )

    system_text = _load_stage_prompt("mini_plan", prompts_dir)

    all_items: list[MiniPlanItem] = []
    prev_tail: list[MiniPlanItem] = []
    setting_gate_attempts: dict[str, int] = {}

    for volume in volume_plans.volumes:
        # 派生本卷可用 character_voices(取 outline 全集 — 单卷裁剪由 LLM
        # 凭 plot.linked_threads 自行判断;p5_plan §6 字面"上游产出作为
        # 下游 user message 只读 context")。
        # 派生本卷 active plot_threads(取全集 — 简化版,后会话可优化)。
        user_text = _append_stage_marker(
            _build_mini_plan_user_message_per_volume(
                world=world,
                setting_contract=resolved_setting_contract,
                characters=characters,
                plot=plot,
                volume=volume,
                genre=genre,
                chapter_id_map=chapter_id_map,
                prev_volumes_tail=prev_tail,
                breakthrough_payoff_enabled=breakthrough_payoff_enabled,
                selection_short_arc=selection_short_arc,
            ),
            stage="mini_plan",
        )
        if consistency_coverage_gaps:
            user_text += (
                "\n\n## 上游审核的待核对意见（不是新增事实或硬性规定）\n"
                "这些意见可能误判。先结合上面的原文核对，只在现有事实范围内"
                "安排必要承接；依据不足的疑问保留为未定。不要把疑问写成事实，"
                "不要为提醒发明关键前史或规则，也不要把每条提醒变成必达事件。\n"
                + json.dumps(consistency_coverage_gaps, ensure_ascii=False)
            )
        volume_chapter_map = {
            f"{volume.volume_id}:{chapter_number}": chapter_id_map[
                (volume.volume_id, chapter_number)
            ]
            for chapter_number in range(1, volume.chapter_count + 1)
        }
        delta_authority_payload = {
            "volume": volume.model_dump(mode="json"),
            **({"world": world.model_dump(mode="json")} if world.story_intent is not None else {}),
            "chapter_id_map": volume_chapter_map,
            "characters": characters.model_dump(mode="json"),
            "plot": plot.model_dump(mode="json"),
        }
        delta_authority = FrozenAuthority.freeze(
            stage="mini_plan",
            payload=delta_authority_payload,
        )

        def _assemble_single_volume_mini_delta(
            data: Any,
            current_volume: VolumePlanItem = volume,
            frozen: FrozenAuthority = delta_authority,
        ) -> Any:
            current_volume_chapter_map = {
                f"{current_volume.volume_id}:{chapter_number}": chapter_id_map[
                    (current_volume.volume_id, chapter_number)
                ]
                for chapter_number in range(1, current_volume.chapter_count + 1)
            }
            current = {
                "volume": current_volume.model_dump(mode="json"),
                **({"world": world.model_dump(mode="json")} if world.story_intent is not None else {}),
                "chapter_id_map": current_volume_chapter_map,
                "characters": characters.model_dump(mode="json"),
                "plot": plot.model_dump(mode="json"),
            }
            artifact = assemble_mini_plan_delta(
                delta=(NarrativeSingleVolumeMiniPlanDelta if world.story_intent is not None and world.story_intent.version >= 2 else SingleVolumeMiniPlanDelta).model_validate(data),
                volume=current_volume,
                chapter_id_map=chapter_id_map,
                characters=characters,
                plot=plot,
                authority=frozen,
                current_authority=current,
                world=world,
            )
            _log.info(
                "creative_delta_assembled",
                stage="mini_plan",
                volume_id=current_volume.volume_id,
                authority_hash=artifact.authority_hash,
                delta_hash=artifact.delta_hash,
                full_leaf_count=artifact.full_leaf_count,
                model_delta_leaf_count=artifact.model_delta_leaf_count,
            )
            return {"items": artifact.value.model_dump(mode="json")["items"]}

        def _check_single_volume(out: BaseModel, vol: VolumePlanItem = volume) -> None:
            """单卷 mini_plan 业务规则校验:items 长度 + 跨字段引用完整性 + beat alignment。

            v1.0.x wave 5 session 2 起改成 post_validate callback,失败抛
            ``ValueError`` → ``_run_stage`` 内的 retry helper 把错误反馈给
            LLM 让其自行纠正 items / arc_id / chapter_id 等。

            v1.1 wave 1 session 1 起新增 beat alignment 校验:每章查找归属 beat
            (落 arc.beats[i].chapter_range 内的 beat)→ 调 beat_alignment_validator
            LLM-as-judge → 失败 raise ValueError(含 forbidden 触发 + missing must
            字面)让 LLM retry 时主动避坑。空 arc.beats(向后兼容旧数据)/ 空
            must+forbidden 自动跳过(validator 内部 ``skipped=True``)。
            """
            assert isinstance(out, _SingleVolumeMiniPlanOutput)
            if len(out.items) != vol.chapter_count:
                raise ValueError(
                    f"volume={vol.volume_id}:items 长度 ({len(out.items)}) "
                    f"必须 == chapter_count ({vol.chapter_count});请重新生成,"
                    f"确保 items 数组恰好 {vol.chapter_count} 条"
                )
            # 跨字段引用完整性 — chapter_id / arc_id / linked_chars /
            # linked_threads 必命中已知集合;_validate_mini_plan_volume_refs
            # 原抛 OutlineBuilderError,这里转 ValueError 让 retry 接管。
            try:
                _validate_mini_plan_volume_refs(
                    items=out.items,
                    volume=vol,
                    characters=characters,
                    plot=plot,
                    chapter_id_map=chapter_id_map,
                )
            except OutlineBuilderError as ref_err:
                raise ValueError(str(ref_err)) from ref_err

            # v1.1 wave 1 session 1:beat alignment 校验(G3 治本核心)
            _validate_mini_plan_beat_alignment(
                items=out.items,
                volume=vol,
                characters=characters,
                router=router,
                client=client,
                model=model,
                prompts_dir=prompts_dir,
                on_parallel_status=on_parallel_status,
            )

            # W3.5-R3:结构校验之后、临时单卷结果汇入全书 / 写盘之前复用
            # R2 ConstraintViolation 与 HARD 资格。只有当前失败卷进入既有
            # _run_stage retry，健康卷不会重生。
            # 只有 world.unique_rules 具备 HARD 资格。实验写前还核对普通机制的
            # 当前草稿直接互斥；旧路线仍按原有 unique_rules 条件调用。
            if resolved_setting_contract.unique_rules.strip() or (
                uses_experimental_outline(router, client)
                and (resolved_setting_contract.setting.strip() or resolved_setting_contract.power_system.strip())
            ):
                attempt = setting_gate_attempts.get(vol.volume_id, 0) + 1
                setting_gate_attempts[vol.volume_id] = attempt
                result = validate_setting_consistency(
                    chapter_text=_render_mini_plan_setting_evidence(out.items),
                    contract=resolved_setting_contract,
                    chapter_id=f"mini-plan:{vol.volume_id}",
                    workspace_id=(
                        str(timing_context.get("workspace_id"))
                        if timing_context and timing_context.get("workspace_id")
                        else None
                    ),
                    router=router,
                    client=client,
                    model=model,
                    validation_scope="mini_plan",
                )
                hard = prewriting_blockers(result)
                if hard:
                    _log.error(
                        "prewriting_semantic_gate_failed",
                        failure_layer="mini_plan",
                        volume_id=vol.volume_id,
                        constraint_ids=[v.constraint_id for v in hard],
                        contract_hash=resolved_setting_contract.source_hash,
                        attempt=attempt,
                    )
                    raise MiniPlanSettingConflictError(
                        volume_id=vol.volume_id,
                        violations=hard,
                        contract_hash=resolved_setting_contract.source_hash,
                        attempt=attempt,
                    )
                if result.violations:
                    _log.warning(
                        "prewriting_semantic_gate_soft",
                        failure_layer="mini_plan",
                        volume_id=vol.volume_id,
                        constraint_ids=[v.constraint_id for v in result.violations],
                        contract_hash=resolved_setting_contract.source_hash,
                        attempt=attempt,
                    )

        result = _run_stage(
            stage="mini_plan",
            system_text=system_text,
            user_text=user_text,
            output_model=_SingleVolumeMiniPlanOutput,
            handoff_data={**delta_authority_payload, "world": world,
                          "setting_contract": resolved_setting_contract, "genre": genre,
                          "prev_volumes_tail": prev_tail,
                          "breakthrough_payoff_enabled": breakthrough_payoff_enabled,
                          "selection_short_arc": selection_short_arc,
                          "consistency_coverage_gaps": consistency_coverage_gaps or []},
            router=router,
            client=client,
            model=model,
            max_tokens=max_tokens,
            temperature=temperature,
            post_validate=_check_single_volume,
            candidate_checkpoint=candidate_checkpoint,
            non_retryable_exceptions=(
                SettingConsistencyTechnicalError,
                BeatAlignmentBatchTechnicalError,
            ),
            timing_context=timing_context,
            timing_fields={
                "substage": "mini_plan_volume",
                "volume_id": volume.volume_id,
            },
            delta_spec=_OutlineDeltaSpec(
                output_model=(NarrativeSingleVolumeMiniPlanDelta if world.story_intent is not None and world.story_intent.version >= 2 else SingleVolumeMiniPlanDelta),
                load_system_text=lambda: _load_delta_stage_prompt(
                    "mini_plan_delta.md", prompts_dir
                ),
                load_complete_system_text=lambda: _load_delta_stage_prompt(
                    "mini_plan_narrative_v9.md" if world.story_intent is not None and world.story_intent.version >= 6 else "mini_plan_narrative_v8.md" if world.story_intent is not None and world.story_intent.version == 5 else "mini_plan_narrative_v7.md" if world.story_intent is not None and world.story_intent.version == 4 else "mini_plan_narrative_v6.md" if world.story_intent is not None and world.story_intent.version >= 2 else "mini_plan_narrative_v3.md" if world.story_intent is not None else "mini_plan_delta_v6.md", prompts_dir
                ),
                load_scene_system_text=lambda: _load_delta_stage_prompt(
                    "mini_plan_delta_v2.md", prompts_dir
                ),
                assemble_data=_assemble_single_volume_mini_delta,
                schema_transform=build_mini_plan_delta_schema_transform(
                    volume=volume,
                ),
                complete_schema_transform=build_mini_plan_delta_schema_transform(
                    volume=volume,
                    allowed_character_ids=[c.character_id for c in characters.characters],
                    allowed_thread_ids=[t.thread_id for t in plot.long_threads],
                    reader_sources=(list(reader_source_catalog(world, characters))
                                    if world.story_intent is not None else None),
                ),
                load_complete_user_text=lambda: (
                    user_text + "\n\n" + mini_plan_reference_brief(volume)
                    + render_story_authority_context(world=world, characters=characters)
                    + ("\n\n叙事交接 v1：每章 chapter_focus 说明阅读重点，普通章也填写；"
                       "不是 HARD 或每章成长配额。reader_information 从下列源字段逐字选择，"
                       "use 为 introduce/background/defer；计划此前安排过不等于正文已交代。\n"
                       + json.dumps(reader_source_catalog(world, characters), ensure_ascii=False)
                       if world.story_intent is not None else "")
                ),
            ),
        )
        assert isinstance(result, _SingleVolumeMiniPlanOutput)

        all_items.extend(result.items)
        prev_tail = result.items[-5:] if len(result.items) >= 5 else result.items

        _log.info(
            "mini_plan_volume_completed",
            volume_id=volume.volume_id,
            items_count=len(result.items),
        )

        if on_volume_complete is not None:
            on_volume_complete(volume.volume_id, result.items)

    # v1.1 wave 6 session 1 / A4:全书伏笔收尾自检(WARN-only,不阻断)。
    # 全书 mini_plan 组装完才能判"每条 long_thread 在结局区是否收口";放单卷
    # post_validate 看不到结局区,故置于全卷循环之后。
    _check_thread_closure(
        plot=plot,
        all_items=all_items,
        volume_plans=volume_plans,
        router=router,
        client=client,
        model=model,
        prompts_dir=prompts_dir,
    )

    # v1.1 wave 6 session 2 / A3:全书角色身份覆盖度自检(WARN-only,不阻断)。
    # 全书 mini_plan 组装完才能判"每个多情境身份角色每侧是否至少交代一次";同
    # _check_thread_closure 置于全卷循环之后。无多情境身份角色 → 0 次 LLM 调用。
    _check_identity_coverage(
        characters=characters,
        all_items=all_items,
        router=router,
        client=client,
        model=model,
        prompts_dir=prompts_dir,
    )

    return MiniPlanOutput(items=all_items)


def _derive_default_chapter_ids(
    volume_plans: VolumePlansOutput,
) -> dict[tuple[str, int], str]:
    """默认 chapter_id 派生:全书 1-based 顺序 + ``ch-NNNN`` 4 位 0 填充。

    对齐既有命名约定(``ChapterCard.chapter_id`` / outline_service /
    chapter_service / chapter_card_service 全用 ``ch-XXXX``)。
    """
    mapping: dict[tuple[str, int], str] = {}
    global_n = 1
    for volume in volume_plans.volumes:
        for vol_ch_num in range(1, volume.chapter_count + 1):
            mapping[(volume.volume_id, vol_ch_num)] = f"ch-{global_n:04d}"
            global_n += 1
    return mapping


def _validate_mini_plan_volume_refs(
    *,
    items: list[MiniPlanItem],
    volume: VolumePlanItem,
    characters: CharactersOutput,
    plot: PlotOutput,
    chapter_id_map: dict[tuple[str, int], str],
) -> None:
    """单卷 mini_plan items 跨字段引用完整性校验(LLM 后置)。

    每个 item 的 ``arc_id`` 必须在 ``volume.arcs`` 内;``chapter_id`` 必须
    == ``chapter_id_map[(volume.volume_id, item.chapter_number)]``;
    ``linked_characters`` 全部在 outline ``characters`` 内;
    ``linked_threads`` 全部在 outline ``plot.long_threads`` 内。
    """
    valid_arc_ids = {a.arc_id for a in volume.arcs}
    valid_char_ids = {c.character_id for c in characters.characters}
    valid_thread_ids = {t.thread_id for t in plot.long_threads}
    seen_chapter_numbers: set[int] = set()
    for item in items:
        if item.volume_id != volume.volume_id:
            raise OutlineBuilderError(
                f"mini_plan item.volume_id={item.volume_id!r} 与本卷 "
                f"volume_id={volume.volume_id!r} 不匹配",
                stage="mini_plan",
                raw_output=item.model_dump_json(),
                model_used="<post-validation>",
            )
        if item.arc_id not in valid_arc_ids:
            raise OutlineBuilderError(
                f"mini_plan item.arc_id={item.arc_id!r} 不在本卷 arcs 列表内 "
                f"{sorted(valid_arc_ids)}",
                stage="mini_plan",
                raw_output=item.model_dump_json(),
                model_used="<post-validation>",
            )
        if not (1 <= item.chapter_number <= volume.chapter_count):
            raise OutlineBuilderError(
                f"mini_plan item.chapter_number={item.chapter_number} 超出本卷 "
                f"[1, {volume.chapter_count}] 范围",
                stage="mini_plan",
                raw_output=item.model_dump_json(),
                model_used="<post-validation>",
            )
        expected_chapter_id = chapter_id_map.get(
            (volume.volume_id, item.chapter_number)
        )
        if expected_chapter_id and item.chapter_id != expected_chapter_id:
            raise OutlineBuilderError(
                f"mini_plan item.chapter_id={item.chapter_id!r} 与映射表 "
                f"({volume.volume_id}, {item.chapter_number}) → "
                f"{expected_chapter_id!r} 不匹配",
                stage="mini_plan",
                raw_output=item.model_dump_json(),
                model_used="<post-validation>",
            )
        unknown_chars = [
            cid for cid in item.linked_characters if cid not in valid_char_ids
        ]
        if unknown_chars:
            raise OutlineBuilderError(
                f"mini_plan item.linked_characters 含未知 ID {unknown_chars}",
                stage="mini_plan",
                raw_output=item.model_dump_json(),
                model_used="<post-validation>",
            )
        unknown_threads = [
            tid for tid in item.linked_threads if tid not in valid_thread_ids
        ]
        if unknown_threads:
            raise OutlineBuilderError(
                f"mini_plan item.linked_threads 含未知 ID {unknown_threads}",
                stage="mini_plan",
                raw_output=item.model_dump_json(),
                model_used="<post-validation>",
            )
        # G2(v1.1 wave 4 session 2):callback_anchors 同 linked_threads 校验,
        # 必须命中 outline ``plot.long_threads`` 内 thread_id(下游确定性派生为
        # ChapterCard.callback_to → Writer SQL lookup,未知 ID 会导致 lookup 落空)。
        unknown_anchors = [
            tid for tid in item.callback_anchors if tid not in valid_thread_ids
        ]
        if unknown_anchors:
            raise OutlineBuilderError(
                f"mini_plan item.callback_anchors 含未知 ID {unknown_anchors}",
                stage="mini_plan",
                raw_output=item.model_dump_json(),
                model_used="<post-validation>",
            )
        if item.chapter_number in seen_chapter_numbers:
            raise OutlineBuilderError(
                f"mini_plan 单卷内 chapter_number={item.chapter_number} 重复",
                stage="mini_plan",
                raw_output=item.model_dump_json(),
                model_used="<post-validation>",
            )
        seen_chapter_numbers.add(item.chapter_number)

    try:
        validate_volume_mini_plan_dramatic_sources(
            items=items,
            volume=volume,
            characters=characters,
            plot=plot,
        )
    except DramaticSourceContractError as exc:
        raise OutlineBuilderError(
            str(exc),
            stage="mini_plan",
            raw_output=volume.model_dump_json(),
            model_used="<post-validation>",
        ) from exc


def _validate_mini_plan_beat_alignment(
    *,
    items: list[MiniPlanItem],
    volume: VolumePlanItem,
    router: ModelRouter | None,
    client: LLMClient | None,
    model: str | None,
    prompts_dir: Path | None,
    characters: CharactersOutput | None = None,
    on_parallel_status: Callable[[dict[str, Any]], None] | None = None,
) -> None:
    """v1.1 wave 1 session 1:mini_plan items 的 beat alignment 校验(G3 治本核心)。

    对每个 item:
    1. 在 ``volume.arcs`` 内查找该 item 归属的 arc(``item.arc_id`` 匹配)
    2. 在 arc.beats 内查找该 item 归属的 beat(``item.chapter_number`` 落
       ``beat.chapter_range`` 内)
    3. 若找到归属 beat → 调 ``validate_beat_alignment`` LLM-as-judge 校验
    4. validator ``passed=False`` → 累积失败明细,**首失败立即 raise ValueError**
       (失败信息含 forbidden 触发字面 + missing must,LLM retry 时主动避坑)

    **向后兼容**:item 找不到归属 arc / arc.beats=[] / beat 找不到归属(章节落
    arc 内 beats 间的"孤章")→ **跳过该 item 的 beat 校验**(不报错),沿用
    旧 arc-level fallback 路径。让旧 outline 数据(无 beats)和"LLM 自由拆
    2-4 beats/arc 不完整覆盖"场景都能 happy 通过。

    **fail-open**:validator 内部 LLM fail-open(瞬态故障 / setup 错配 / JSON
    schema 失败)→ ``passed=True / fail_open=True``,本函数视为通过(不影响
    既有 happy 路径)。

    post-v6 B:同卷最多 4 章组成一个严格波次并行；当前波次全部结束后仍按
    原 ``items`` 顺序合并并抛首失败，之后波次不会提前启动。卷与卷仍由
    ``generate_mini_plan`` 外层保持串行。配置为 1 时恢复旧逐章 fail-fast。

    **失败信息聚合**:固定合并顺序中每个 item 失败立即 raise,不累积全卷再报。
    并行波次内已启动的调用会先被完整回收，避免晚返回跨入 retry / Continue。

    creative_delta_v6 实验管线改为同卷一次结构化核对。程序验证每章都归属
    一个节拍、结果逐章齐全；只对批量疑点作单章复核。技术失败绝不放行。
    """

    def _emit_snapshot(
        states: list[dict[str, Any]],
        *,
        max_concurrency: int,
    ) -> None:
        if on_parallel_status is None:
            return
        snapshot = {
            "parallel_group": "mini_plan_beat_alignment",
            "volume_id": volume.volume_id,
            "max_concurrency": max_concurrency,
            "tasks": [dict(state) for state in states],
        }
        try:
            on_parallel_status(snapshot)
        except Exception as exc:  # noqa: BLE001 — 状态投影不能改变业务语义
            _log.warning(
                "mini_plan_beat_parallel_status_publish_failed",
                volume_id=volume.volume_id,
                error_type=type(exc).__name__,
            )

    def _run_one(
        item: MiniPlanItem,
        beat: VolumeBeatPlan,
        *,
        neighboring_events: str = "",
    ) -> Any:
        # 每个 worker 只拿深拷贝快照；不共享可变 Pydantic payload。
        return validate_beat_alignment(
            chapter_event=item.one_line_event,
            beat=beat,
            chapter_id=item.chapter_id,
            info_increment=item.info_increment,
            character_context=(json.dumps([
                {"character_id": c.character_id, "name": c.name,
                 "archetype": c.archetype, "background": c.background}
                for c in characters.characters
            ], ensure_ascii=False) if characters is not None else ""),
            chapter_design=mini_plan_review_design(item),
            neighboring_events=neighboring_events,
            workspace_id=None,  # outline 阶段不绑 workspace_id(commit 时落库)
            router=router,
            client=client,
            model=model,
            prompts_dir=prompts_dir,
        )

    def _raise_failure(
        *,
        item: MiniPlanItem,
        beat: VolumeBeatPlan,
        result: Any,
    ) -> None:
        feedback = format_validation_failure_for_retry(
            result=result,
            chapter_id=item.chapter_id,
            beat_id=beat.beat_id,
        )
        drama_direction = (
            "④ 本章是 designated typed 戏剧承担章，整份本章设计必须自然演出 "
            "actor_goal/counteraction/choice/consequence；可以由事件摘要、互动重点和人物弧光共同承载，不必全部挤进摘要；"
            if beat.dramatic_contract is not None
            else ""
        )
        raise ValueError(
            f"mini_plan beat alignment 失败,请重新生成该章 one_line_event;"
            f"具体失败明细:\n{feedback}\n\n"
            f"修正方向:① 核对禁止项指向的同一人物、实际动作和条件，修正实际违规，"
            f"不得仅删除人名或关键词以避检;② 让本章明确推进 beat must_advance 中至少 "
            f"1 条;③ 确保 chapter_event 落在 beat.summary 范围内；"
            f"{drama_direction}"
        )

    def _cap_allows_parallel(required_attempts: int) -> bool:
        checker = getattr(router, "can_afford_provider_attempts", None)
        if not callable(checker):
            return True
        try:
            return bool(checker(required_attempts))
        except Exception as exc:  # noqa: BLE001 — 余量不可证实时安全退回串行
            _log.error(
                "mini_plan_beat_parallel_cap_headroom_check_failed",
                volume_id=volume.volume_id,
                error_type=type(exc).__name__,
            )
            return False

    arcs_by_id = {a.arc_id: a for a in volume.arcs}
    experimental_batch = uses_experimental_outline(router, client)
    tasks: list[tuple[MiniPlanItem, VolumeBeatPlan]] = []
    for item in items:
        arc = arcs_by_id.get(item.arc_id)
        if arc is None:
            # 已被 _validate_mini_plan_volume_refs 拦截,这里只是防御
            if experimental_batch:
                raise ValueError(f"{item.chapter_id} 找不到归属故事段 {item.arc_id}")
            continue
        if not arc.beats:
            # 向后兼容:arc.beats=[] 走 arc-level fallback,跳过 beat 校验
            if experimental_batch:
                raise ValueError(f"{item.chapter_id} 的故事段没有章节节拍")
            continue
        # 查找归属 beat(item.chapter_number 落 beat.chapter_range 内)
        matching_beats = [
            beat for beat in arc.beats
            if beat.chapter_range[0] <= item.chapter_number <= beat.chapter_range[1]
        ]
        if experimental_batch and len(matching_beats) > 1:
            raise ValueError(
                f"{item.chapter_id} 同时归属多个节拍，不能选第一条蒙混通过"
            )
        owning_beat = matching_beats[0] if matching_beats else None
        if owning_beat is None:
            # 孤章 — chapter 落 arc 内但不在任何 beat 内(prompt 字面引导 LLM
            # 完整覆盖,但 Q2=A LLM 自由 2-4 beats 允许例外)。跳过校验。
            if experimental_batch:
                raise ValueError(
                    f"{item.chapter_id} 在故事段 {item.arc_id} 内找不到覆盖本章的节拍"
                )
            continue
        tasks.append(
            (
                item.model_copy(deep=True),
                # 同一高价值 beat 只由 mini-plan 指定的一章承担 typed 戏剧合同；
                # 其它章仍只校验既有 summary/must/forbidden，避免整段重复演终局。
                owning_beat.model_copy(
                    update={"dramatic_contract": item.dramatic_contract},
                    deep=True,
                ),
            )
        )

    if not tasks:
        return

    if experimental_batch:
        # 同卷一次返回每章独立 verdict；技术失败与缺章停在小纲写盘前，
        # 不把额度不足/非法 JSON 当作通过，也不因此重生整卷小纲。
        results = validate_beat_alignment_batch(
            entries=tasks,
            character_context=[
                {"character_id": c.character_id, "name": c.name,
                 "archetype": c.archetype, "background": c.background}
                for c in characters.characters
            ] if characters is not None else [],
            router=router,
            client=client,
            model=model,
            prompts_dir=prompts_dir,
        )
        for item, beat in tasks:
            result = results[item.chapter_id]
            if result.passed:
                continue
            # 只有批量结果明确指出的疑点才使用旧逐章核对器复核一次。
            confirmed = _run_one(
                item, beat,
                neighboring_events=json.dumps([
                    {"chapter_id": other.chapter_id,
                     "one_line_event": other.one_line_event}
                    for other, _ in tasks if other.chapter_id != item.chapter_id
                ], ensure_ascii=False),
            )
            if confirmed.fail_open:
                raise BeatAlignmentBatchTechnicalError(
                    f"{item.chapter_id} 定点复核调用失败，不能视为通过"
                )
            if not confirmed.passed:
                _raise_failure(item=item, beat=beat, result=confirmed)
        return

    configured_concurrency = settings.mini_plan_beat_alignment_max_concurrency
    # 三个正式 Provider client 的 transport retry 上限均为每 logical call
    # 3 次。要并行本卷，先证明余量容得下所有 Beat 的最坏物理尝试；否则恢复
    # 旧逐章顺序，让 cap 尾部仍由最早章节优先消费。
    required_headroom = len(tasks) * 3
    if configured_concurrency > 1 and not _cap_allows_parallel(required_headroom):
        configured_concurrency = 1
        _log.info(
            "mini_plan_beat_parallel_fallback",
            volume_id=volume.volume_id,
            reason="provider_cap_headroom",
            required_attempts=required_headroom,
        )
    max_concurrency = min(configured_concurrency, len(tasks))
    states: list[dict[str, Any]] = [
        {
            "subtask_id": f"beat:{volume.volume_id}:{item.chapter_id}",
            "chapter_id": item.chapter_id,
            "chapter_number": item.chapter_number,
            "beat_id": beat.beat_id,
            "status": "queued",
        }
        for item, beat in tasks
    ]
    _emit_snapshot(states, max_concurrency=max_concurrency)

    def _set_finished_state(index: int, result: Any) -> None:
        if result.fail_open:
            states[index]["status"] = "failed"
            states[index]["reason"] = "technical_fail_open"
        elif result.passed:
            states[index]["status"] = "succeeded"
            if result.skipped:
                states[index]["reason"] = "empty_contract_skipped"
        else:
            states[index]["status"] = "failed"
            states[index]["reason"] = "semantic_failure"

    def _mark_not_run(start_index: int) -> None:
        changed = False
        for state in states[start_index:]:
            if state["status"] == "queued":
                state["status"] = "failed"
                state["reason"] = "not_run_after_earlier_failure"
                changed = True
        if changed:
            _emit_snapshot(states, max_concurrency=max_concurrency)

    # 回退开关必须恢复旧逐章 fail-fast：不预启动后续章，不创建 executor。
    if max_concurrency == 1:
        for index, (item, beat) in enumerate(tasks):
            states[index]["status"] = "running"
            _emit_snapshot(states, max_concurrency=max_concurrency)
            try:
                result = _run_one(item, beat)
            except (LLMCancelledError, Exception) as exc:
                states[index]["status"] = "failed"
                states[index]["reason"] = type(exc).__name__
                _emit_snapshot(states, max_concurrency=max_concurrency)
                _mark_not_run(index + 1)
                raise
            _set_finished_state(index, result)
            _emit_snapshot(states, max_concurrency=max_concurrency)
            if not result.passed:
                _mark_not_run(index + 1)
                _raise_failure(item=item, beat=beat, result=result)
        return

    # 严格波次：失败波次之外的任务绝不启动；波次内全部回收后按输入顺序合并。
    for batch_start in range(0, len(tasks), max_concurrency):
        batch_end = min(batch_start + max_concurrency, len(tasks))
        outcomes: dict[int, Any] = {}
        errors: dict[int, BaseException] = {}
        futures: dict[Future[Any], int] = {}
        with ThreadPoolExecutor(
            max_workers=max_concurrency,
            thread_name_prefix="mini-plan-beat",
        ) as executor:
            for index in range(batch_start, batch_end):
                item, beat = tasks[index]
                states[index]["status"] = "running"
                context = copy_context()
                future = executor.submit(context.run, _run_one, item, beat)
                futures[future] = index
            _emit_snapshot(states, max_concurrency=max_concurrency)

            for future in as_completed(futures):
                index = futures[future]
                try:
                    result = future.result()
                except (LLMCancelledError, Exception) as exc:
                    errors[index] = exc
                    states[index]["status"] = "failed"
                    states[index]["reason"] = type(exc).__name__
                else:
                    outcomes[index] = result
                    _set_finished_state(index, result)
                _emit_snapshot(states, max_concurrency=max_concurrency)

        # 用户取消是外部终态意图：即使同波更早章节同时返回语义失败，也必须
        # 让取消穿透到后台任务入口，避免 cancelled 被随后写成普通 failed。
        for index in range(batch_start, batch_end):
            error = errors.get(index)
            if isinstance(error, LLMCancelledError):
                _mark_not_run(batch_end)
                raise error

        for index in range(batch_start, batch_end):
            if index in errors:
                _mark_not_run(batch_end)
                raise errors[index]
            item, beat = tasks[index]
            result = outcomes[index]
            if not result.passed:
                _mark_not_run(batch_end)
                _raise_failure(item=item, beat=beat, result=result)


# 结局区窗口:取全书 mini_plan 末尾 N 章作为"结局区"喂 thread_closure_validator
# (控 prompt 体量;一卷可达 30 章,全卷事件喂 judge 过长)。
_THREAD_CLOSURE_ENDING_WINDOW = 8


def _check_thread_closure(
    *,
    plot: PlotOutput,
    all_items: list[MiniPlanItem],
    volume_plans: VolumePlansOutput,
    router: ModelRouter | None,
    client: LLMClient | None,
    model: str | None,
    prompts_dir: Path | None,
) -> list[str]:
    """v1.1 wave 6 session 1 / A4:全书 long_thread 结局收尾自检(WARN-only)。

    治 B1 暴露的**大纲坑③**(伏笔只设计"揭露"不设计"闭合";bug D4 界外真局
    埋了不收)。流程:

    1. 建挂线图:thread_id → 全书所有挂线章(``linked_threads`` ∪ ``callback_anchors``)。
    2. 结局区 = 最后一卷的所有章;候选 = 在结局区**未挂线**的 long_thread。
    3. 对每个候选调 ``thread_closure_validator`` LLM-as-judge(末尾 N 章事件作上下文):
       ``closed`` / ``open_with_setup``(有交代开放,护 C1 留白)→ 合格;``dangling``
       (埋了不收)→ 累计 + log warn。
    4. **WARN-only**:全程**不 raise**(护 §0 第一原则:开放结局合法,LLM 文学判断
       可能误判;真正治本靠 mini_plan prompt 上移要求设计收口。HARD 升级留 wave 6
       末约束预算实测后定)。

    :return: dangling 伏笔标题 list(供 caller / 测试观测;同时已 log warn)。
    """
    if not plot.long_threads or not all_items:
        return []

    # 1. 全书挂线图:thread_id → [(chapter_number, chapter_id), ...]
    hook_map: dict[str, list[tuple[int, str]]] = {}
    for item in all_items:
        for thread_id in (*item.linked_threads, *item.callback_anchors):
            hook_map.setdefault(thread_id, []).append(
                (item.chapter_number, item.chapter_id)
            )

    # 2. 结局区 = 最后一卷的全部章 chapter_id 集合
    if not volume_plans.volumes:
        return []
    last_volume_id = volume_plans.volumes[-1].volume_id
    closure_region_chapter_ids = {
        item.chapter_id for item in all_items if item.volume_id == last_volume_id
    }

    # 结局区末尾 N 章事件(按全书顺序;all_items 已按 (volume, chapter_number) 排序)
    ending_events = [
        item.one_line_event for item in all_items[-_THREAD_CLOSURE_ENDING_WINDOW:]
    ]

    candidates: list[ThreadClosureCandidate] = []
    for thread in plot.long_threads:
        hooks = hook_map.get(thread.thread_id, [])
        hooked_in_closure = any(cid in closure_region_chapter_ids for _, cid in hooks)
        if hooked_in_closure:
            # 结局区已挂线 → 视为按计划收口,不进候选(省 token)
            continue
        # 候选:结局区未挂线(含全书孤儿)。last_hooked = 最后挂线章(无则 None)
        last_hooked_chapter = max(hooks, key=lambda h: h[0])[1] if hooks else None
        candidates.append(
            ThreadClosureCandidate(
                thread_title=thread.title,
                thread_description=thread.description,
                last_hooked_chapter=last_hooked_chapter,
            )
        )

    # 批处理:一次 LLM 调用判全部候选(候选为空 → validator 内部 0 次调用)。
    batch = validate_thread_closure_batch(
        candidates=candidates,
        ending_events=ending_events,
        workspace_id=None,  # outline 阶段不绑 workspace_id(commit 时落库)
        router=router,
        client=client,
        model=model,
        prompts_dir=prompts_dir,
    )
    dangling_titles = batch.dangling_titles

    if dangling_titles:
        _log.warning(
            "outline_thread_closure_dangling",
            dangling_count=len(dangling_titles),
            dangling_titles=dangling_titles,
            total_long_threads=len(plot.long_threads),
        )
    else:
        _log.debug(
            "outline_thread_closure_ok",
            total_long_threads=len(plot.long_threads),
        )
    return dangling_titles


def _check_identity_coverage(
    *,
    characters: CharactersOutput,
    all_items: list[MiniPlanItem],
    router: ModelRouter | None,
    client: LLMClient | None,
    model: str | None,
    prompts_dir: Path | None,
) -> list[tuple[str, list[str]]]:
    """v1.1 wave 6 session 2 / A3:全书角色身份覆盖度自检(WARN-only)。

    治 B1 暴露的 **bug A5**(白露现实侧整章丢失 —— writer 全程只写她梦境侧)。流程:

    1. 候选 = ``characters`` 里 ``identity_facets`` ≥ 2 的**多情境身份角色**(普通
       单一身份角色 / 空 facets 角色不进候选)。
    2. 候选为空(无多情境身份角色 = 普通故事)→ 直接返 ``[]``,**0 次 LLM 调用**。
    3. 把全书 ``one_line_event`` 作上下文,批处理调 ``identity_coverage_validator``
       LLM-as-judge,判每个候选角色的**每一侧身份**全书是否至少交代一次。
    4. **WARN-only**:某侧未覆盖 → log warn **不 raise**(护 §0 第一原则:覆盖判断是
       文学语义判断 LLM 可能误判;真正治本靠 mini_plan prompt 上移要求每侧分配 ≥ 1 章。
       HARD 升级留 wave 6 末约束预算实测后定)。

    :return: 有未覆盖侧面的角色 list ``[(character_name, uncovered_contexts), ...]``
        (供 caller / 测试观测;同时已 log warn)。
    """
    candidates = [
        IdentityCoverageCandidate(
            character_id=c.character_id,
            character_name=c.name,
            facets=tuple(c.identity_facets),
        )
        for c in characters.characters
        if len(c.identity_facets) >= 2
    ]
    if not candidates:
        return []

    all_events = [item.one_line_event for item in all_items]

    # 批处理:一次 LLM 调用判全部候选(候选为空已在上面短路 → 0 次调用)。
    batch = validate_identity_coverage_batch(
        candidates=candidates,
        all_events=all_events,
        workspace_id=None,  # outline 阶段不绑 workspace_id(commit 时落库)
        router=router,
        client=client,
        model=model,
        prompts_dir=prompts_dir,
    )
    gaps = batch.gaps

    if gaps:
        _log.warning(
            "outline_identity_coverage_gaps",
            gap_count=len(gaps),
            gaps=gaps,
            multi_facet_character_count=len(candidates),
        )
    else:
        _log.debug(
            "outline_identity_coverage_ok",
            multi_facet_character_count=len(candidates),
        )
    return gaps


def _build_mini_plan_user_message_per_volume(
    *,
    world: WorldOutput,
    setting_contract: SettingContract | None = None,
    characters: CharactersOutput,
    plot: PlotOutput,
    volume: VolumePlanItem,
    genre: str = "",
    chapter_id_map: dict[tuple[str, int], str],
    prev_volumes_tail: list[MiniPlanItem],
    breakthrough_payoff_enabled: bool = False,
    selection_short_arc: bool = False,
) -> str:
    """单卷 mini_plan user message。

    包含:
    - 当前卷 volume_plan 完整内容(arcs + tension_curve)
    - 当前卷 character_voices(取 outline characters 全集 — 单卷裁剪由 LLM 判断)
    - 当前卷 active plot_threads(取 outline plot.long_threads 全集)
    - 滚动上下文:前序卷 mini_plan items 末尾 5 章(首卷为空)
    - chapter_id 派生表(让 LLM 严格使用映射 ID,不自己编)
    - **M4''' 题材结构 profile + 戏剧功能信号**(v1.1 wave 5 s6):题材的默认结构 /
      偏离加分 / 红线(``_structural_profile_for_genre``)+ major_turning_points +
      character_arcs(让 LLM 判每章戏剧功能 → 决定 chapter_focus,治结构不跟功能走)
    """
    resolved_setting_contract = setting_contract or SettingContract.from_world_output(
        world
    )

    def _format_arc(a: Any) -> str:
        """单 arc → prompt 字面块(含 beats 子结构,v1.1 G3)。

        beats 空时退化为旧 arc-summary-only 输出(向后兼容旧 outline 数据);
        beats 非空时追加"beats:" 子段 + 每 beat 5 字段。
        """
        head = (
            f"  - {a.arc_id} | {a.arc_type} | chapter_range=[{a.chapter_range[0]}, "
            f"{a.chapter_range[1]}] | phase={a.phase}\n"
            f"    summary: {a.summary}"
        )
        if not a.beats:
            return head
        beat_lines = []
        for b in a.beats:
            must_list = "; ".join(b.must_advance) if b.must_advance else "(无)"
            forbidden_list = (
                "; ".join(b.forbidden_advance) if b.forbidden_advance else "(无)"
            )
            drama = (
                b.dramatic_contract.model_dump_json()
                if b.dramatic_contract is not None
                else None
            )
            beat_lines.append(
                f"      - {b.beat_id} | chapter_range=[{b.chapter_range[0]}, "
                f"{b.chapter_range[1]}]\n"
                f"        summary: {b.summary}\n"
                f"        must_advance: {must_list}\n"
                f"        forbidden_advance: {forbidden_list}\n"
                f"        dramatic_contract: {drama}"
            )
        return head + "\n    beats:\n" + "\n".join(beat_lines)

    arcs_block = "\n".join(_format_arc(a) for a in volume.arcs)
    tension_block = ", ".join(str(t) for t in volume.tension_curve)

    def _format_character(c: Any) -> str:
        """单角色 → prompt 字面块。A3:多情境身份角色追加 identity_facets 行,
        让 mini_plan LLM 知道该给每一侧分配章(覆盖自检上移治本)。"""
        line = f"  - {c.character_id} | {c.name}({c.role}) | voice_hint: {c.voice_hint}"
        if c.identity_facets:
            facets = "; ".join(f"{f.context}={f.identity}" for f in c.identity_facets)
            line += f"\n    身份侧面(每一侧都要在全书安排至少 1 章交代):{facets}"
        return line

    character_block = "\n".join(_format_character(c) for c in characters.characters)
    thread_block = (
        "\n".join(
            f"  - {t.thread_id} | {t.title}: {t.description}" for t in plot.long_threads
        )
        if plot.long_threads
        else "  (无活跃伏笔)"
    )

    chapter_id_lines = [
        f"  - chapter_number={n} → chapter_id={chapter_id_map[(volume.volume_id, n)]}"
        for n in range(1, volume.chapter_count + 1)
        if (volume.volume_id, n) in chapter_id_map
    ]

    # M4'''(v1.1 wave 5 s6):戏剧功能信号 —— major_turning_points + character_arcs。
    # 让 LLM 判每章戏剧功能(情感峰 / 角色转折 / 前史)→ 决定 chapter_focus。旧 mini_plan
    # user message 只给 core_conflict,看不到 turning_point 描述 / 角色弧光 → 无脑默认双线。
    turning_block = (
        "\n".join(
            f"  - {tp.turning_id} | {tp.title}: {tp.description}"
            for tp in plot.major_turning_points
        )
        if plot.major_turning_points
        else "  (无)"
    )
    arcs_arc_block = (
        "\n".join(
            f"  - {char_id}: {arc_desc}"
            for char_id, arc_desc in plot.character_arcs.items()
        )
        if plot.character_arcs
        else "  (无)"
    )
    structural_profile = _structural_profile_for_genre(genre)

    if prev_volumes_tail:
        # acceptance 修复1(2026-06-10)卷界双极化:
        # ① 渲染补 arc_beats"已完结的收束" — 此前只渲染 one_line_event,其悬念式
        #   措辞(典型"险些掐死流浪汉",结局只写在 arc_beats)让下一卷 LLM 误以为
        #   悬念未解决,把"解决它"当新卷开篇职责重演一遍 → 与前卷已写正文重叠 →
        #   该章 must_achieve 永不可达成死锁(luo-accept-v1 ch-0007 现场)。
        # ② 指令补反重演极 — 原指令只防"断裂/重启"一极,不防"重演"一极。
        tail_lines: list[str] = []
        for item in prev_volumes_tail:
            tail_lines.append(
                f"  - {item.chapter_id}({item.volume_id} ch-{item.chapter_number}):"
                f" {item.one_line_event}"
            )
            if item.arc_beats:
                resolved = ";".join(item.arc_beats.values())
                tail_lines.append(f"    ↳ 本章内已完结的收束:{resolved}")
        prev_block = (
            "## 滚动上下文:前序卷 mini_plan 末尾 5 章(只读,用于跨卷衔接)\n\n"
            + "\n".join(tail_lines)
            + "\n\n本卷首章应自然承接前一卷末章事件的**后果与新事态**,不应「重启」"
            "主线;上述末章事件(含「已完结的收束」)在前一卷内**已经发生完毕**,"
            "**不得在本卷重演、复述或重新解决**。\n"
        )
    else:
        prev_block = (
            "## 滚动上下文:前序卷 mini_plan 末尾 5 章\n\n"
            "  (首卷,无前序上下文 — 本卷首章承担全书起点事件)\n"
        )

    # 补强1(v2 W3.5):命名锁定表前置注入(纵深 —— mini_plan 引用 character_arcs
    # /long_threads 可能已含错名,再次声明 canonical)。无 buried 名时返 "" 不扰动。
    selection_short_section = (
        "\n\n## selection_test 三章剧情功能合同（不得改数量或顺序）\n\n"
        "- ch-0001：起因。停电封闭档案馆，修复师发现三份互相矛盾的"
        "失踪记录，建立必须在天亮前判断篡改者的问题。\n"
        "- ch-0002：升级。三份记录的证据冲突变成现实风险，候选解释收窄，"
        "但不得提前公布最终篡改者。\n"
        "- ch-0003：高潮/交代。修复师以前两章已呈现的证据作出判断，"
        "揭示谁改写了档案及方法/动机，并交代失踪记录的真相。\n"
        if selection_short_arc
        else ""
    )
    _msg = (
        build_name_lock_header(characters) + "## 任务背景\n\n"
        "为本卷生成章级事件分配 — 每章 1 条 mini_plan item,共 "
        f"{volume.chapter_count} 条。\n\n"
        "## 当前卷规划(只读)\n\n"
        f"- volume_id:{volume.volume_id}\n"
        f"- title:{volume.title}\n"
        f"- summary:{volume.summary}\n"
        f"- chapter_count:{volume.chapter_count}\n"
        "- dramatic_promise:"
        f"{volume.dramatic_promise.model_dump_json() if volume.dramatic_promise else None}\n"
        f"- tension_curve(每章 1-10):[{tension_block}]\n\n"
        "### 本卷 arcs(arc_id 必命中下列之一;每 arc 内 beats 给 mini_plan 细粒度引导,v1.1 G3)\n"
        + arcs_block
        + "\n\n"
        + prev_block
        + "\n"
        + resolved_setting_contract.render_for_prompt()
        + "\n\n"
        "## 上游产出:主线核心(plot,只读)\n\n"
        f"- core_conflict:{plot.core_conflict}\n\n"
        "### 关键转折(major_turning_points,只读 — 判每章戏剧功能用,M4''')\n"
        + turning_block
        + "\n\n### 角色弧光(character_arcs,只读 — 判'角色单焦'结构〔M4'''〕"
        "+ 逐章弧光台阶 arc_beats〔M5〕用)\n"
        + arcs_arc_block
        + "\n\n## 当前可用角色清单(linked_characters 必从此清单选)\n\n"
        + character_block
        + "\n\n## 当前活跃伏笔清单(linked_threads 必从此清单选,可空)\n\n"
        + thread_block
        + "\n\n## 章节 ID 映射(chapter_id 必严格使用下表值,不自己编)\n\n"
        + "\n".join(chapter_id_lines)
        + selection_short_section
        + "\n\n"
        + structural_profile
        + "\n请按 system prompt 字面 JSON schema 产**本卷** mini_plan;"
        f"items 长度严格 == {volume.chapter_count};"
        "每章 one_line_event 必须独立差异、含具体场景 + 主语 + 动词 + 宾语结构。"
        "**M4''' 结构跟功能走**:对每章,先据 tension_curve + 所属 beat + 上方 turning_points "
        "/ character_arcs 判**本章戏剧功能**,再据题材 profile 决定**结构焦点**——绝大多数"
        "章保持题材默认结构(``chapter_focus`` 留空 ''),只在情感峰 / 角色转折 / 前史揭示 / "
        "困局等戏剧功能真需要偏离时,把 ``one_line_event`` 写成那个焦点形态 + 在 "
        "``chapter_focus`` 一句话声明(形态 + 被弱化界如何退场)。**内容驱动非配额**:别为"
        "凑多样硬套,硬套减分。"
        "**M5 角色弧光逐章落地**:对每章,看上方 character_arcs + 本章出场角色,判**有没有"
        "谁的弧光在这一章真推进一个可见台阶**;有 → 在 ``arc_beats`` 加一条(key=角色 "
        "character_id,value=自包含一句话:从某态→某态 + **通过什么动作/抉择/代价演出**),"
        "没有 → 留空 ``{}``。**命门=动作演出非旁白声明**(✅'她亲手按下证物销毁键'/❌'她"
        "开始动摇');**内容驱动非配额**(绝大多数章空,全书个位数关键蜕变+若干轻台阶);"
        "**配角/反派优先**(最易写成工具人/纸片人,关键蜕变章务必标台阶)。详见 system "
        "prompt 约束 10。"
        "**M4''-轴A 互动驱动(仅松 beat 题材)**:**先看上方题材结构 profile 是否标注本书是"
        "'松 beat / 互动驱动'类(治愈日常 / 慢生活)**——若是,本题材最大的病是'独处内省"
        "扎堆'(引擎默认让主角独自做事/感慨),安排每章 ``one_line_event`` 时**默认走向真实"
        "互动**(把'主角独自做某事'尽量改成'和某人一起做/被某人见证反应/温和分歧',当戏剧"
        "功能允许时),互动中心章在 ``interaction_focus`` 一句话点明'本章互动核心是主角与谁 "
        "+ 那个配角的主体性';**护留白**(该独处的顿悟/崩溃/留白章留空)+ **护治愈基调**"
        "(别尖锐冲突)+ **内容驱动非配额**(非每章必须互动)。**若本书是紧 beat 题材(悬疑等),"
        "普通线索、独处推理与留白章的 ``interaction_focus`` 留空；只有 designated 转折、"
        "对抗、情感峰或 finale 真由人物互动承重时才可填写**。indirect 反动作不要求双方"
        "物理同场，但必须让反动作与后果可见。详见 system prompt 约束 11。"
        "**A1 typed 戏剧同源**:所属 beat 的 ``dramatic_contract`` 非空时,该 beat 内恰好"
        "一章逐字段复制对象,其余章为 null；承担章必须自然演出 actor_goal/counteraction/"
        "choice/consequence，并闭合 actor/counterforce/thread 引用。finale 只放 beat 末章。"
        "普通独处、治愈、休整与留白 beat 保持 null；不得在自由文本埋机器 marker。"
    )
    # 模块①(v1.1 wave 5 s10)约束 12 升级可感化:**gated 条件块**(仅 3 题材 + 开关 ON 时
    # 追加;OFF 时 _msg 逐字节不变 = 零回归)。让 LLM 给境界突破/晋级章填 breakthrough_payoff。
    if breakthrough_payoff_enabled:
        _msg += (
            "\n\n**【模块①】升级可感化(breakthrough_payoff,本书已启用)**:本书是升级流"
            "题材,读者核心爽点之一是**境界/实力突破的可感跃迁**。安排每章 ``one_line_event`` "
            "时,**判本章是不是境界突破/晋级章**(主角或重要角色修为/战力跨上一个台阶):\n"
            "- **是突破章** → 在 ``breakthrough_payoff`` 一句话点明**突破到什么 + 要落地的"
            "可感化点**(典型:'突破炼气巅峰——展示新能力:灵识外放感知翻倍/当场碾压之前险胜"
            "的对手,让读者感到实力跃迁,别只写一个境界名词')。**起点境界下游会自动注入写手,"
            "本字段只标终点 + 可感化落点**。\n"
            "- **不是突破章** → ``breakthrough_payoff`` 留空 ''。\n"
            "- **内容驱动非配额**:只在真有境界跃迁的章填,绝不每章硬塞突破(那是注水);"
            "护节奏:突破章本就该是少数高光章。"
        )
    return _msg


# ---------------------------------------------------------------------------
# 一键串行入口(供 service 层 P5-B-04 调用)
# ---------------------------------------------------------------------------


class FullOutline(BaseModel):
    """OutlineBuilder 5 段产出的复合 Pydantic(供 service 一键串行后返一个对象)。

    P5-B-04 ``outline_service.generate_outline`` 在收 caller 一次性调用时
    可用本对象当返回值;若 caller 要逐段 WS 推送,继续调 ``build_*`` 5 次。

    v1.0.x wave 4 session 1 起从 4 段扩到 5 段(加 ``mini_plan``)。
    """

    world: WorldOutput
    characters: CharactersOutput
    plot: PlotOutput
    volume_plans: VolumePlansOutput
    mini_plan: MiniPlanOutput


def build_outline(
    *,
    premise: str,
    total_volumes: int,
    chapters_per_volume: int,
    tension_template: TensionTemplateName,
    genre: str = "",
    length_calibration: GenreCalibration | None = None,
    target_word_count_max: int | None = None,
    router: ModelRouter | None = None,
    client: LLMClient | None = None,
    model: str | None = None,
    max_tokens: int = _DEFAULT_MAX_TOKENS,
    temperature: float = _DEFAULT_TEMPERATURE,
    prompts_dir: Path | None = None,
    on_stage_complete: Any = None,
    on_review_failure: Callable[[WorldOutput, CharactersOutput, PlotOutput, VolumePlansOutput, Any], str] | None = None,
    on_review_complete: Callable[[WorldOutput, CharactersOutput, PlotOutput, VolumePlansOutput, Any], None] | None = None,
    on_parallel_status: Callable[[dict[str, Any]], None] | None = None,
    breakthrough_payoff_enabled: bool = False,
    outline_autofix_enabled: bool = True,
    outline_consistency_retry_enabled: bool = True,
    outline_arc_span_retry_enabled: bool = True,
    character_backfill_enabled: bool = True,
    selection_short_arc: bool = False,
    timing_context: dict[str, Any] | None = None,
    story_intent: StoryIntent | None = None,
    mini_candidate_checkpoint: GenerationCheckpoint | None = None,
    pre_mini_review_checkpoint: Any = None,
    prepared_outline: tuple[WorldOutput, CharactersOutput, PlotOutput] | None = None,
) -> FullOutline:
    """4 段串行一键调用。

    ``on_stage_complete`` 回调签名 ``(stage: OutlineStage, output: BaseModel) -> None``;
    每段完成时同步调一次,供 caller 推 WS stage 进度(P5-B-04 用)。``None``
    时跳过(纯单测 / CLI 调用)。任一段失败抛 ``OutlineBuilderError``,后续段
    不再调(对齐 p5_plan.md §6"中间阶段失败不进入下游")。
    """
    world = prepared_outline[0] if prepared_outline is not None else build_world(
        premise=premise,
        genre=genre,
        total_volumes=total_volumes,
        chapters_per_volume=chapters_per_volume,
        router=router,
        client=client,
        model=model,
        max_tokens=max_tokens,
        temperature=temperature,
        prompts_dir=prompts_dir,
        timing_context=timing_context,
        story_intent=story_intent,
    )
    if on_stage_complete is not None:
        on_stage_complete("world", world)

    characters = prepared_outline[1] if prepared_outline is not None else build_characters(
        premise=premise,
        world=world,
        genre=genre,
        router=router,
        client=client,
        model=model,
        max_tokens=max_tokens,
        temperature=temperature,
        prompts_dir=prompts_dir,
        timing_context=timing_context,
    )
    if on_stage_complete is not None:
        on_stage_complete("characters", characters)

    plot = prepared_outline[2] if prepared_outline is not None else build_plot(
        premise=premise,
        world=world,
        characters=characters,
        genre=genre,
        router=router,
        client=client,
        model=model,
        max_tokens=max_tokens,
        temperature=temperature,
        prompts_dir=prompts_dir,
        timing_context=timing_context,
    )
    if on_stage_complete is not None:
        on_stage_complete("plot", plot)

    # v2 W3.5-s4-step1 机制1:角色完整性回填(plot 后、volume_plans 前)。
    # 检出 plot 当真实角色引用、但 characters 没建卡的"指代悬空"角色 → 补卡,
    # 让 volume_plans 拿到完整角色表,从源头掐断"抓邻近角色顶替"的张冠李戴混淆
    # (s4-step1 实证:deepseek 博拉尔 / qwen 导师 两次失败的公共根因)。
    # 实验回填失败走既有大纲错误路径，不当作无需补人；legacy 行为保留。
    # 补到卡 → 重持久化 characters.json(再调 on_stage_complete)。
    def _backfill_characters(
        current_characters: CharactersOutput,
        current_volume_plans: VolumePlansOutput | None = None,
    ) -> tuple[CharactersOutput, list[CharacterOutlineItem]]:
        try:
            return backfill_plot_referenced_characters(
                world=world, characters=current_characters, plot=plot,
                volume_plans=current_volume_plans, premise=premise, genre=genre,
                router=router, client=client, model=model, max_tokens=max_tokens,
                temperature=temperature, prompts_dir=prompts_dir,
            )
        except CharacterBackfillError as exc:
            raise OutlineBuilderError(
                str(exc), stage="characters", raw_output=exc.raw_output,
                model_used=exc.model_used, attempts=exc.attempts,
            ) from exc

    if character_backfill_enabled:
        with StageTimer(
            _log,
            "outline_stage_timing",
            _outline_timing_fields(
                timing_context,
                stage="characters",
                substage="character_backfill_after_plot",
            ),
        ):
            characters, _backfilled = _backfill_characters(characters)
        if _backfilled and on_stage_complete is not None:
            on_stage_complete("characters", characters)
    else:
        _emit_outline_skipped(
            timing_context,
            stage="characters",
            substage="character_backfill_after_plot",
            reason="disabled",
        )

    published_upstream = {
        "world": world.model_dump(mode="json"),
        "characters": characters.model_dump(mode="json"),
        "plot": plot.model_dump(mode="json"),
    }

    def _generate_volume_plans_round(
        current_characters: CharactersOutput,
        *,
        consistency_retry_feedback: str | None = None,
    ) -> tuple[VolumePlansOutput, CharactersOutput]:
        """生成/细化卷规划,并跑 volume 后角色回填。

        首轮 ``consistency_retry_feedback`` 为空;若 outline_consistency 判出
        实质冲突,第二轮把冲突反馈注入 volume_plans + volume_refine,只重出卷规划,
        不改 world / plot。
        """
        next_volume_plans = build_volume_plans(
            premise=premise,
            world=world,
            characters=current_characters,
            plot=plot,
            total_volumes=total_volumes,
            chapters_per_volume=chapters_per_volume,
            tension_template=tension_template,
            genre=genre,
            router=router,
            client=client,
            model=model,
            max_tokens=max_tokens,
            temperature=temperature,
            prompts_dir=prompts_dir,
            consistency_retry_feedback=consistency_retry_feedback,
            selection_short_arc=selection_short_arc,
            timing_context=timing_context,
        )
        # 实验v6不依赖样本密度，但仍必须细化并重持久化。旧路径保留校准条件。
        if target_word_count_max is not None and (
            length_calibration is not None or uses_experimental_outline(router, client)
        ):
            next_volume_plans = refine_volume_plans_length_aware(
                volume_plans=next_volume_plans,
                length_calibration=length_calibration,
                target_word_count_max=target_word_count_max,
                genre=genre,
                router=router,
                client=client,
                model=model,
                max_tokens=max_tokens,
                temperature=temperature,
                prompts_dir=prompts_dir,
                consistency_retry_feedback=consistency_retry_feedback,
                selection_short_arc=selection_short_arc,
                timing_context=timing_context,
                story_intent=world.story_intent,
                world=world, characters=current_characters,
                plot=plot,
            )
        else:
            _emit_outline_skipped(
                timing_context,
                stage="volume_plans",
                substage="volume_refine",
                reason="length_calibration_unavailable",
            )

        next_characters = current_characters
        # v2 W3.5 C-7b:同一 backfill 机制在 volume_plans 后再跑一次。
        # 目标是检出卷级规划新增使用、但 characters 仍未建卡的关键人物,让后续
        # consistency validator / mini_plan 开跑前看到更完整的角色池。护栏沿用机制1:
        # 实验失败阻断、legacy兼容，ID+name 去重、总数封顶 8、只追加不改既有卡。
        if character_backfill_enabled:
            with StageTimer(
                _log,
                "outline_stage_timing",
                _outline_timing_fields(
                    timing_context,
                    stage="volume_plans",
                    substage="character_backfill_after_volume_plans",
                    retry_feedback=consistency_retry_feedback is not None,
                ),
            ):
                next_characters, _volume_backfilled = _backfill_characters(
                    current_characters, next_volume_plans,
                )
        else:
            _emit_outline_skipped(
                timing_context,
                stage="volume_plans",
                substage="character_backfill_after_volume_plans",
                reason="disabled",
                retry_feedback=consistency_retry_feedback is not None,
            )
        return next_volume_plans, next_characters

    volume_plans, characters = _generate_volume_plans_round(characters)

    # v2 W3.5-S3:大纲 4 段跨段一致性校验(名字/称谓矛盾 + 角色混淆/张冠李戴 +
    # 卷规划覆盖)。**放此处(volume_plans 后、mini_plan 前)**:① 名字矛盾此时全段
    # 可见;② 在昂贵的 mini_plan(N 卷 N 次 LLM)之前拦截 = 省成本;③ 防 deepseek
    # 一致性硬伤(T1 实证 2/2)下游污染出卡→正文。真矛盾(contradictions 非空且非
    # fail-open)→ 抛 OutlineBuilderError 阻断(outline_status 保持 generating,用户
    # regenerate-section 修复后重审);覆盖缺口 WARN-only(validator 内部 log,不阻断)。
    # fail-open(LLM/setup 异常 passed=True)→ 不阻断,护"宁可漏报不误伤大纲"。
    def _run_consistency() -> Any:
        review = (validate_outline_consistency_partitioned
                  if uses_split_outline_review(router, client, model)
                  else validate_outline_consistency)
        return review(
            world=world,
            characters=characters,
            plot=plot,
            volume_plans=volume_plans,
            premise=premise,
            genre=genre,
            router=router,
            client=client,
            model=model,
            max_tokens=max_tokens,
            temperature=temperature,
            prompts_dir=prompts_dir,
            **({"checkpoint": pre_mini_review_checkpoint} if review is validate_outline_consistency_partitioned
               and pre_mini_review_checkpoint is not None else {}),
        )

    def _apply_name_typo_autofix_if_possible(consistency_result: Any) -> Any:
        """全 name_typo 时确定性自动修 + 重验;否则原样返回。"""
        nonlocal world, characters, plot, volume_plans
        # v2 W3.5-s4-step1:大纲「名字写岔」确定性自动修(gated, 默认开, 零 fail-open)。
        # 仅当**全部**冲突都是可修 name_typo(无 substantive/ambiguous、无信息不全)时,
        # 对 LLM 给的 {wrong,correct} 走多数票护栏确定性归一 → **重跑
        # validator** 确认干净才放行。任何含不可修硬伤 / 修后仍不过 → 落到下面 HARD 拦
        # (行为同前)。根因(s4-step1 实证):deepseek 大纲常"几乎干净却被 1 字 typo 连坐
        # HARD 毙"(骆秉渊 vs 陆秉渊),精度探针 11/11 验证 LLM 能分清 typo / distinct / substantive。
        if not (
            outline_autofix_enabled
            and not consistency_result.passed
            and consistency_result.name_typo_fixes
            and not consistency_result.has_non_fixable_contradiction
        ):
            return consistency_result
        (world, characters, plot, volume_plans), applied, skipped = (
            apply_name_typo_fixes(
                world=world,
                characters=characters,
                plot=plot,
                volume_plans=volume_plans,
                fixes=consistency_result.name_typo_fixes,
            )
        )
        if not applied:
            return consistency_result
        # Keep the candidate in memory until all pre-mini-plan gates pass.
        _log.info(
            "outline_autofix_applied_revalidate",
            applied_count=len(applied),
            skipped_count=len(skipped),
            fixes=[(f.wrong, f.correct) for f in applied],
        )
        # 重跑 validator 确认归一后干净(修后仍有冲突 → 下面照常 HARD 拦)。
        return _run_consistency()

    with StageTimer(
        _log,
        "outline_stage_timing",
        _outline_timing_fields(
            timing_context,
            stage="volume_plans",
            substage="outline_consistency_gate",
        ),
    ):
        consistency = _apply_name_typo_autofix_if_possible(_run_consistency())

    # v2 W3.5 followup:若 consistency 判出实质冲突,把冲突清单作为 retry feedback
    # 接回 volume_plans / volume_refine 有界重出一次。只重出卷规划,不重跑 world /
    # characters / plot,避免手工救样本和无界成本膨胀。
    if (
        outline_consistency_retry_enabled
        and not consistency.passed
        and consistency.has_non_fixable_contradiction
        and (
            not uses_experimental_outline(router, client)
            or consistency.volume_plans_retry_allowed
        )
    ):
        retry_feedback = format_outline_consistency_failure_for_retry(
            result=consistency
        )
        _log.warning(
            "outline_consistency_volume_plans_retry_triggered",
            contradiction_count=len(consistency.contradictions),
            coverage_gap_count=len(consistency.coverage_gaps),
        )
        volume_plans, characters = _generate_volume_plans_round(
            characters,
            consistency_retry_feedback=retry_feedback,
        )
        with StageTimer(
            _log,
            "outline_stage_timing",
            _outline_timing_fields(
                timing_context,
                stage="volume_plans",
                substage="outline_consistency_gate_retry",
            ),
        ):
            consistency = _apply_name_typo_autofix_if_possible(_run_consistency())

    if not consistency.passed:
        candidate_id = (on_review_failure(world, characters, plot, volume_plans, consistency)
                        if on_review_failure is not None and consistency.contradiction_details
                        else None)
        raise OutlineBuilderError(
            format_outline_consistency_failure_for_retry(result=consistency),
            stage="volume_plans",
            raw_output="; ".join(consistency.contradictions),
            model_used="outline_consistency_validator",
            # v2 W3.5 UI 缺口1:透出结构化矛盾 + 依据,供 API 翻译 + 前端高亮实体名。
            contradictions=list(consistency.contradictions),
            coverage_gaps=list(consistency.coverage_gaps),
            evidence=consistency.evidence,
            contradiction_details=list(consistency.contradiction_details),
            repair_scope=consistency.repair_scope,
            candidate_id=candidate_id,
        )

    with StageTimer(
        _log,
        "outline_stage_timing",
        _outline_timing_fields(
            timing_context,
            stage="volume_plans",
            substage="arc_span_gate",
        ),
    ):
        arc_span_failures = validate_arc_chapter_spans(volume_plans.volumes)
    if arc_span_failures and outline_arc_span_retry_enabled:
        retry_feedback = _format_arc_span_failures_for_retry(arc_span_failures)
        _log.warning(
            "outline_arc_span_volume_plans_retry_triggered",
            failure_count=len(arc_span_failures),
            violations=[f.model_dump(mode="json") for f in arc_span_failures],
        )
        volume_plans, characters = _generate_volume_plans_round(
            characters,
            consistency_retry_feedback=retry_feedback,
        )
        consistency = _apply_name_typo_autofix_if_possible(_run_consistency())
        if not consistency.passed:
            candidate_id = (on_review_failure(world, characters, plot, volume_plans, consistency)
                            if on_review_failure is not None and consistency.contradiction_details
                            else None)
            raise OutlineBuilderError(
                format_outline_consistency_failure_for_retry(result=consistency),
                stage="volume_plans",
                raw_output="; ".join(consistency.contradictions),
                model_used="outline_consistency_validator",
                contradictions=list(consistency.contradictions),
                coverage_gaps=list(consistency.coverage_gaps),
                evidence=consistency.evidence,
                contradiction_details=list(consistency.contradiction_details),
                repair_scope=consistency.repair_scope,
                candidate_id=candidate_id,
            )
        with StageTimer(
            _log,
            "outline_stage_timing",
            _outline_timing_fields(
                timing_context,
                stage="volume_plans",
                substage="arc_span_gate_retry",
            ),
        ):
            arc_span_failures = validate_arc_chapter_spans(volume_plans.volumes)

    if arc_span_failures:
        raise OutlineBuilderError(
            format_arc_span_failures_for_retry(arc_span_failures),
            stage="volume_plans",
            raw_output="; ".join(
                f"{f.volume_id}:{f.arc_id}:span={f.span_length}"
                for f in arc_span_failures
            ),
            model_used="outline_arc_span_gate",
            violations=[f.model_dump(mode="json") for f in arc_span_failures],
        )

    if on_stage_complete is not None:
        for stage_name, output in (("world", world), ("characters", characters), ("plot", plot)):
            if output.model_dump(mode="json") != published_upstream[stage_name]:
                on_stage_complete(stage_name, output)
        on_stage_complete("volume_plans", volume_plans)
    if on_review_complete is not None and consistency.passed:
        on_review_complete(world, characters, plot, volume_plans, consistency)

    # v1.0.x wave 4 session 1:Stage 5 mini_plan 章级事件分配。
    # 分卷生成内部 N 卷 N 次 LLM 调用 + 滚动上下文;caller 看到的是单一
    # "mini_plan" stage event(不拆 sub-stage)。
    mini_plan = generate_mini_plan(
        candidate_checkpoint=mini_candidate_checkpoint,
        world=world,
        characters=characters,
        plot=plot,
        volume_plans=volume_plans,
        genre=genre,
        router=router,
        client=client,
        model=model,
        max_tokens=_MINI_PLAN_PER_VOLUME_MAX_TOKENS,
        temperature=temperature,
        prompts_dir=prompts_dir,
        on_parallel_status=on_parallel_status,
        breakthrough_payoff_enabled=breakthrough_payoff_enabled,
        selection_short_arc=selection_short_arc,
        timing_context=timing_context,
        consistency_coverage_gaps=(
            list(consistency.coverage_gaps)
            if uses_experimental_outline(router, client)
            else None
        ),
    )
    if on_stage_complete is not None:
        on_stage_complete("mini_plan", mini_plan)

    return FullOutline(
        world=world,
        characters=characters,
        plot=plot,
        volume_plans=volume_plans,
        mini_plan=mini_plan,
    )
