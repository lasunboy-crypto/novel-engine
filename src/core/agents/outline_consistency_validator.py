"""outline_consistency LLM-as-judge validator(v2 W3.5-S3,大纲跨段一致性校验)。

职责:大纲 4 段(world / characters / plot / volume_plans)生成后,**跨段**判定是否
存在自洽硬伤,把 T1 暴露的 deepseek 大纲一致性硬伤(名字矛盾 / 角色混淆)在大纲层
拦截,**防一路污染下游出卡 → 正文**(T1 实证:deepseek 大纲 2/2 有一致性硬伤
〔顾崇山/顾盛礼名字矛盾、小陆/小纪角色混淆〕,3 盲读裁判独立警告会下游污染)。

判据三类(对齐 prompts/outline_consistency_validator_v1.md):

1. **名字 / 称谓矛盾**(contradictions,HARD):同一实体(人/地/物/组织)在不同段被
   写成互斥的名字 / 属性 / 身份 / 关系。
2. **角色混淆 / 张冠李戴**(contradictions,HARD):characters 里都真实存在的两个角色
   被混为一人,或某角色的特征 / 弧光 / 行动被安到错的角色头上。
3. **卷规划覆盖 + 指代悬空**(coverage_gaps,WARN):卷规划没承接 plot 主骨架 / 多卷
   塌缩无推进 / plot 反复提到但 characters 没列的关键角色未刻画。**S3 精度调优(2026-06-24):
   指代悬空从 HARD 降 WARN** —— "是真漏角色还是合法幕后 / 已失踪人物"需情节推断、高 FP
   (T1 实证 qwen 把"陆沉失踪的导师"用"若导师=魏渊则矛盾"的条件式误判 HARD);crisp 名字 /
   属性互斥才留 HARD。

接口签名::

    validate_outline_consistency(
        *, world, characters, plot, volume_plans,
        premise="", genre="", workspace_id=None,
        router=None, client=None, model=None,
        ...
    ) -> OutlineConsistencyResult

调用方(outline_builder.build_outline 在 volume_plans 后、mini_plan 前):
contradictions 非空 → caller 抛 OutlineBuilderError 阻断(在 mini_plan 之前 = 省
mini_plan 成本 + 防下游污染;outline_status 保持 generating,用户可 regenerate-section
修复后重审);coverage_gaps 非空只 log warn 不 raise。

设计要点(沿用 mechanism_consistency_validator 同型架构,不发明新哲学)
====================================================================

1. **1 次综合 LLM 调用**:4 段一次性看完综合判定(跨段矛盾本就需要全局视野)。
2. **沿用 *_validator 路由**:agent_name ``"outline_consistency_validator"`` →
   ``agent_routing.outline_consistency_validator``(config.yaml,**cloud_qwen_max_thinking
   thinking on**:T1 才华vs纪律 — deepseek 漏自己的一致性 bug、同模型自查同盲区,换
   纪律更强的 qwen 当第二双眼);未显式配则 ``resolve_routing`` fallback 走 reviewer profile。
3. **旧路线 LLM 失败 fail-open**(对齐 mechanism_consistency_validator 决策 3)；
   实验 JSON 路线的完整审核和新增局部审核均 fail closed，不能把无效响应当通过。
4. **agent_role marker = ``outline_consistency_validator``(无 chapter_id):
   MockLLMClient 识别 marker 返默认"无矛盾"JSON,既有 outline 生成 happy 路径不回归。
5. **失败语义**(retry / 人工友好):``format_outline_consistency_failure_for_retry``
   把 contradictions 字面 list 格式化进 error msg,供 caller / 用户读到"哪几处跨段矛盾"。
"""

from __future__ import annotations

import json
import re
from dataclasses import dataclass, field
from pathlib import Path
from typing import TYPE_CHECKING, Any, Literal

import structlog
from pydantic import BaseModel, ConfigDict, Field, ValidationError, field_validator

from src.config import settings
from src.core.agents.structured_handoff import HandoffError, handoff_user_text
from src.core.agents._json_contract import loads_with_quote_repair
from src.core.agents._prompt import (
    append_role_marker,
    load_prompt_template,
    resolve_routing,
)
from src.core.agents.creative_delta import uses_experimental_pipeline
from src.core.agents.story_authority_context import render_complete_outline_context
from src.core.agents._structured_output import prepare_strict_json_output
from src.core.llm.base import LLMClient, Message
from src.core.routing.router import ModelRouter

if TYPE_CHECKING:
    from src.core.models.outline import (
        CharactersOutput,
        PlotOutput,
        VolumePlansOutput,
        WorldOutput,
    )

_log = structlog.get_logger(__name__)

_VALIDATOR_PROMPT_FILENAME = "outline_consistency_validator_v2.md"

# 默认生成参数:综合判定对采样稳定性敏感,温度低;输出 JSON 含 2 个 list[str] + str,
# 跨 4 段输入较长 → token 上限略宽。router 路径下由 profile 决定。
_DEFAULT_TEMPERATURE = 0.3
_DEFAULT_MAX_TOKENS = 1200

# 匹配 ```json ... ``` 或 ``` ... ``` 包裹(沿用 mechanism_consistency_validator 同型剥离)。
_CODE_FENCE_RE = re.compile(
    r"^\s*```(?:json)?\s*\n(?P<body>.*?)\n```\s*$",
    re.DOTALL,
)

# trailing HTML 注释剥离(沿用 mechanism_consistency_validator 同型)。
_TRAILING_HTML_COMMENT_RE = re.compile(
    r"\s*<!--.*?-->\s*$",
    re.DOTALL,
)


ContradictionType = Literal["name_typo", "substantive", "ambiguous"]


@dataclass(frozen=True)
class NameTypoFix:
    """可确定性自动修复的「名字写岔」(v2 W3.5-s4-step1)。

    语义:同一实体的本名 = ``correct``,在某一两处被手滑写成 ``wrong``(错别字 /
    形近字 / 被旁边角色名带歪)。下游 ``apply_name_typo_fixes`` 在**多数票护栏**
    (``correct`` 全文出现数 > ``wrong``)通过时,确定性把 ``wrong`` 归一为 ``correct``。
    """

    wrong: str
    correct: str
    desc: str = ""


@dataclass(frozen=True)
class OutlineConsistencyResult:
    """validator 输出。

    - ``passed``:``contradictions`` 为空时 True;非空时 False(HARD 触发阻断)。
      LLM fail / setup 失败 → True(fail-open 语义)。
    - ``contradictions``:LLM 判定的跨段自洽硬伤(名字/称谓矛盾 + 角色混淆/张冠李戴),
      每条自然语言描述一处(``desc``)。``passed=False`` 的主要原因在这里。
    - ``coverage_gaps``:LLM 判定的卷规划覆盖缺口(WARN 标记,**不**影响 ``passed``)。
    - ``evidence``:LLM 给出的简短判定依据(中文)。
    - ``llm_calls_count``:实际发起的 LLM 调用次数;正常 → 1;fail-open → 0 或 1。
    - ``fail_open``:为 True 当且仅当 LLM 调用 / setup 抛异常导致整体 fail-open
      (passed=True)。
    - ``name_typo_fixes``(v2 s4-step1):被 LLM 判为 ``name_typo`` 且带完整
      ``{wrong, correct}`` 的子集 —— 可确定性自动修。
    - ``has_non_fixable_contradiction``(v2 s4-step1):任一条冲突非 name_typo
      (substantive / ambiguous),或 name_typo 缺 wrong/correct → True。
      caller 据此决定:**全部可修** → 自动修 + 重验;**含不可修** → 维持 HARD。
    """

    passed: bool
    contradictions: list[str] = field(default_factory=list)
    coverage_gaps: list[str] = field(default_factory=list)
    evidence: str = ""
    llm_calls_count: int = 0
    fail_open: bool = False
    name_typo_fixes: list[NameTypoFix] = field(default_factory=list)
    has_non_fixable_contradiction: bool = False
    # 原审核同一次返回的修复归属；缺失/不明不授权实验管线重写无关卷规划。
    volume_plans_retry_allowed: bool = False
    # Source-bound evidence is needed by failed-candidate repair.  The public
    # descriptions remain for existing callers and error payloads.
    contradiction_details: list[dict[str, Any]] = field(default_factory=list)
    repair_scope: str = "unclear"
    completed_review_stages: list[str] = field(default_factory=list)


class _SourceQuote(BaseModel):
    model_config = ConfigDict(extra="forbid")
    path: str = Field(..., min_length=1)
    quote: str = Field(..., min_length=1)


class _ContradictionItem(BaseModel):
    """LLM 输出的单条跨段冲突(v2:带分类 + 可选修复数据)。

    向后兼容:``_coerce`` 把老格式纯字符串 → ``{desc: str, type: substantive}``
    (substantive = 最安全,不会被误当 name_typo 自动改)。未知 ``type`` 同样
    降为 substantive。
    """

    model_config = ConfigDict(extra="ignore")

    desc: str = Field(..., min_length=1, max_length=600)
    type: ContradictionType = "substantive"
    wrong: str = ""
    correct: str = ""
    reason: str = ""
    source_quotes: list[_SourceQuote] = Field(default_factory=list)

    @field_validator("type", mode="before")
    @classmethod
    def _coerce_type(cls, v: object) -> object:
        if v not in ("name_typo", "substantive", "ambiguous"):
            return "substantive"
        return v


class _OutlineConsistencyLLMPayload(BaseModel):
    """LLM 输出的中间模型(v2:contradictions 升级为结构化对象列表)。"""

    contradictions: list[_ContradictionItem] = Field(default_factory=list)
    coverage_gaps: list[str] = Field(default_factory=list)
    # 说明篇幅不参与故事判定；整次响应仍受既有输出上限约束。
    evidence: str = Field(..., min_length=1)
    repair_scope: Literal["none", "volume_plans_only", "upstream", "unclear"] = Field(
        default="unclear",
        description=(
            "全部矛盾的修复归属：无矛盾none；上游三段可逐字保留且只改卷规划"
            "即可消除全部矛盾volume_plans_only；必须修世界/人物/主线upstream；不确定unclear"
        ),
    )

    @field_validator("contradictions", mode="before")
    @classmethod
    def _coerce_str_items(cls, v: object) -> object:
        # 向后兼容:老 v1 格式 list[str] / LLM 偶发返纯串 → 当 substantive item。
        if isinstance(v, list):
            return [
                {"desc": item, "type": "substantive"} if isinstance(item, str) else item
                for item in v
            ]
        return v


SplitReviewStage = Literal["world", "characters", "plot", "volume_plans", "cross"]
_SPLIT_STAGES: tuple[SplitReviewStage, ...] = (
    "world", "characters", "plot", "volume_plans", "cross",
)


class _SplitReviewPayload(BaseModel):
    """A bounded verdict for one source-bound part of a four-section outline."""

    model_config = ConfigDict(extra="forbid")
    review_stage: SplitReviewStage
    source_hash: str = Field(min_length=64, max_length=64)
    outcome: Literal["compatible", "conflict", "unknown"]
    reviewed_sections: list[Literal["world", "characters", "plot", "volume_plans"]]
    contradictions: list[_ContradictionItem]
    coverage_gaps: list[str]
    requested_paths: list[str]
    evidence: str = Field(min_length=1)
    repair_scope: Literal["none", "volume_plans_only", "upstream", "unclear"]


def _review_generation_kwargs(client: LLMClient, model: str, kwargs: dict[str, Any]) -> dict[str, Any]:
    """Apply the explicit review-only DeepSeek ceiling before the cost guard."""
    if not (uses_experimental_pipeline(client) and model == "deepseek-flash"):
        return kwargs
    prepare_max = getattr(client, "prepare_outline_review_max_call", None)
    if prepare_max is None:
        raise RuntimeError("实验大纲审核缺少独立最高强度参数合同")
    return prepare_max(kwargs)


def split_review_sources(
    stage: SplitReviewStage, source: dict[str, Any],
) -> dict[str, Any]:
    """Give each check only its own section and the prior source sections."""
    if stage not in _SPLIT_STAGES:
        raise ValueError("未知拆分审核阶段")
    sections = ("world", "characters", "plot", "volume_plans")
    through = len(sections) if stage == "cross" else sections.index(stage) + 1
    if not all(source.get(name) is not None for name in sections[:through]):
        raise ValueError("拆分审核缺少上游原文")
    return {**{name: source[name] for name in sections[:through]},
            "premise": source.get("premise", ""), "genre": source.get("genre", ""),
            **({"user_request": source["user_request"]} if "user_request" in source else {})}


def split_review_messages(
    stage: SplitReviewStage, source: dict[str, Any], *, prompts_dir: Path,
) -> list[Message]:
    """Freeze identical JSON messages for product review and paid diagnostics."""
    from src.core.agents.outline_review_packets import digest
    from src.core.agents.structured_handoff import build_packet, render_packet

    visible = split_review_sources(stage, source)
    task = (f"只审核 review_stage={stage} 的任务范围；逐字引用可见原文。"
            "缺少判断必需的资料返回 unknown，不能宣布通过。")
    system = load_prompt_template(prompts_dir / "outline_split_consistency_validator_v4.md")
    if (visible.get("world", {}).get("story_intent") or {}).get("authoring_plan") is not None:
        system += "\n\n" + load_prompt_template(prompts_dir / "outline_builder/authoring_review_v1.md")
    data = {**visible, "review_stage": stage, "source_hash": digest(visible)}
    return [Message(role="system", content=system), Message(role="user", content=render_packet(
        build_packet(stage="outline_consistency_validator", data=data,
                     original_task=append_role_marker(task, role="outline_consistency_validator")), "json"))]


def validate_split_review_payload(
    payload: _SplitReviewPayload, *, stage: SplitReviewStage, source: dict[str, Any],
) -> _SplitReviewPayload:
    """Reject incomplete, stale and ungrounded verdicts before a pass is usable."""
    from src.core.agents.outline_review_packets import digest

    visible = split_review_sources(stage, source)
    if payload.review_stage != stage or payload.source_hash != digest(visible):
        raise ValueError("拆分审核结果与来源或任务不一致")
    expected = (set(("world", "characters", "plot", "volume_plans"))
                if stage == "cross" else {stage})
    visible_sections = set(visible) & {"world", "characters", "plot", "volume_plans"}
    if not expected <= set(payload.reviewed_sections):
        raise ValueError("拆分审核未明确覆盖负责的故事段")
    if not set(payload.reviewed_sections) <= visible_sections:
        raise ValueError("拆分审核声称核对了未提供的故事段")
    if len(payload.reviewed_sections) != len(set(payload.reviewed_sections)):
        raise ValueError("拆分审核覆盖段重复")
    _validate_source_quotes(payload, visible)
    if payload.outcome == "conflict":
        if not payload.contradictions or payload.requested_paths or payload.repair_scope not in {
            "volume_plans_only", "upstream", "unclear",
        }:
            raise ValueError("拆分审核硬矛盾结论不完整")
        for item in payload.contradictions:
            roots = {quote.path.split(".", 1)[0] for quote in item.source_quotes}
            if stage == "cross":
                if len(roots & {"world", "characters", "plot", "volume_plans"}) < 2:
                    raise ValueError("跨段硬矛盾没有两个故事段的逐字依据")
            elif stage not in roots:
                raise ValueError("局部硬矛盾没有引用受审故事段")
            if payload.repair_scope == "volume_plans_only" and "volume_plans" not in roots:
                raise ValueError("未引用卷规划的冲突不能只重出卷规划")
        if stage in {"world", "characters", "plot"} and payload.repair_scope != "upstream":
            raise ValueError("上游硬矛盾不能归属卷规划")
    elif payload.outcome == "compatible":
        if payload.contradictions or payload.requested_paths or payload.repair_scope != "none":
            raise ValueError("缺资料或矛盾不能当作通过")
    elif payload.contradictions or payload.repair_scope != "unclear":
        raise ValueError("未决审核不能给出确定修复归属")
    if payload.requested_paths and payload.outcome != "unknown":
        raise ValueError("缺资料必须返回未决")
    return payload


def validate_outline_consistency_partitioned(
    *, world: WorldOutput, characters: CharactersOutput, plot: PlotOutput,
    volume_plans: VolumePlansOutput, premise: str = "", genre: str = "",
    workspace_id: str | None = None, router: ModelRouter | None = None,
    client: LLMClient | None = None, model: str | None = None,
    max_tokens: int = _DEFAULT_MAX_TOKENS,
    temperature: float = _DEFAULT_TEMPERATURE,
    prompts_dir: Path | None = None,
    checkpoint: Any = None,
) -> OutlineConsistencyResult:
    """Five focused calls; all five must finish and agree before a pass."""
    root = (prompts_dir if prompts_dir is not None else settings.prompts_dir).resolve()
    source = {"world": world.model_dump(mode="json"),
              "characters": characters.model_dump(mode="json"),
              "plot": plot.model_dump(mode="json"),
              "volume_plans": volume_plans.model_dump(mode="json"),
              "premise": premise, "genre": genre}
    resolved_client, resolved_model, kwargs = resolve_routing(
        agent_name="outline_consistency_validator", router=router,
        client=client, model=model,
        legacy_kwargs={"max_tokens": max_tokens, "temperature": temperature},
    )
    kwargs = prepare_strict_json_output(
        client=resolved_client, model=resolved_model,
        output_model=_SplitReviewPayload, schema_name="outline_split_consistency",
        complete_kwargs=kwargs,
    )
    kwargs = _review_generation_kwargs(resolved_client, resolved_model, kwargs)
    results: list[_SplitReviewPayload] = []
    physical_calls = 0
    if checkpoint is not None:
        checkpoint.prepare(source)
    for stage in _SPLIT_STAGES:
        result = checkpoint.load(stage, source) if checkpoint is not None else None
        if result is None:
            physical_calls += 1
            response = resolved_client.complete(
                messages=split_review_messages(stage, source, prompts_dir=root),
                model=resolved_model, **kwargs,
            )
            if response.finish_reason not in {"stop", "end_turn"}:
                raise RuntimeError(f"{stage} 审核未正常完成，不能放行")
            try:
                result = validate_split_review_payload(
                    _SplitReviewPayload.model_validate_json(response.content),
                    stage=stage, source=source,
                )
            except (ValidationError, ValueError) as exc:
                raise RuntimeError(f"{stage} 审核没有有效结论，不能放行") from exc
            if checkpoint is not None and result.outcome == "compatible":
                checkpoint.save(stage, source, result, {"source": "product_review",
                    "provider_request_id": response.provider_request_id,
                    "model_used": response.model_used})
        results.append(result)
        if result.outcome == "unknown":
            raise RuntimeError(f"{stage} 审核资料不足，不能当作通过: {result.evidence}")
        if result.outcome == "conflict":
            name_typo_fixes = [
                NameTypoFix(wrong=item.wrong.strip(), correct=item.correct.strip(), desc=item.desc)
                for item in result.contradictions
                if item.type == "name_typo" and item.wrong.strip() and item.correct.strip()
            ]
            has_non_fixable = any(
                item.type != "name_typo" or not item.wrong.strip() or not item.correct.strip()
                for item in result.contradictions
            )
            return OutlineConsistencyResult(
                passed=False,
                contradictions=[item.desc for item in result.contradictions],
                contradiction_details=[item.model_dump(mode="json") for item in result.contradictions],
                coverage_gaps=[gap for earlier in results for gap in earlier.coverage_gaps],
                evidence=result.evidence, llm_calls_count=physical_calls,
                repair_scope=result.repair_scope,
                volume_plans_retry_allowed=result.repair_scope == "volume_plans_only",
                name_typo_fixes=name_typo_fixes,
                has_non_fixable_contradiction=has_non_fixable,
            )
    _log.info("outline_partitioned_review_passed", workspace_id=workspace_id,
              calls=physical_calls)
    return OutlineConsistencyResult(
        passed=True, coverage_gaps=[gap for item in results for gap in item.coverage_gaps],
        evidence="五项同源审核均已完成", llm_calls_count=physical_calls, repair_scope="none",
        completed_review_stages=list(_SPLIT_STAGES),
    )


def validate_outline_consistency(
    *,
    world: WorldOutput,
    characters: CharactersOutput,
    plot: PlotOutput,
    volume_plans: VolumePlansOutput,
    premise: str = "",
    genre: str = "",
    workspace_id: str | None = None,
    router: ModelRouter | None = None,
    client: LLMClient | None = None,
    model: str | None = None,
    max_tokens: int = _DEFAULT_MAX_TOKENS,
    temperature: float = _DEFAULT_TEMPERATURE,
    prompts_dir: Path | None = None,
) -> OutlineConsistencyResult:
    """1 次综合 LLM 调用,判定大纲 4 段跨段是否自洽 + 卷规划是否覆盖主线。

    :param world: ``WorldOutput`` 第 1 段产出。
    :param characters: ``CharactersOutput`` 第 2 段产出。
    :param plot: ``PlotOutput`` 第 3 段产出。
    :param volume_plans: ``VolumePlansOutput`` 第 4 段产出(细化后)。
    :param premise: 用户一句话设定(可选,给 judge 作背景)。
    :param genre: 题材(可选)。
    :param workspace_id: 工作区 ID,落 structlog 字段(可选)。
    :param router: ``ModelRouter`` 实例;给定时按
        ``router.route("outline_consistency_validator")`` 取 client + model +
        生成参数。``None`` 时走 legacy 路径。
    :param client: legacy 路径 LLM 客户端;``router=None`` 时必传。
    :param model: legacy 路径模型名;``router=None`` 时必传。
    :param max_tokens: legacy 路径 token 上限。
    :param temperature: legacy 路径温度。
    :param prompts_dir: 提示词模板根目录;``None`` 时取 ``settings.prompts_dir``。
    :return: ``OutlineConsistencyResult``。
    """
    experimental = client is not None and uses_experimental_pipeline(client)
    # setup 阶段失败(prompt 文件缺失 / 路由错配)走 fail-open。
    try:
        resolved_client, resolved_model, complete_kwargs = resolve_routing(
            agent_name="outline_consistency_validator",
            router=router,
            client=client,
            model=model,
            legacy_kwargs={"max_tokens": max_tokens, "temperature": temperature},
        )
        experimental = uses_experimental_pipeline(resolved_client)
        template_root = (
            prompts_dir if prompts_dir is not None else settings.prompts_dir
        ).resolve()
        system_text = load_prompt_template(template_root / _VALIDATOR_PROMPT_FILENAME)
        complete_kwargs = prepare_strict_json_output(
            client=resolved_client,
            model=resolved_model,
            output_model=_OutlineConsistencyLLMPayload,
            schema_name="outline_consistency",
            complete_kwargs=complete_kwargs,
        )
    except Exception as exc:  # noqa: BLE001 — legacy 保留 fail-open
        if isinstance(exc, HandoffError):
            raise
        if experimental:
            raise RuntimeError("实验大纲一致性审核 setup 失败，不能当作通过") from exc
        _log.warning(
            "outline_consistency_validator_setup_failed_open",
            workspace_id=workspace_id,
            error_type=type(exc).__name__,
            error_message=str(exc),
        )
        return OutlineConsistencyResult(
            passed=True,
            llm_calls_count=0,
            fail_open=True,
        )

    user_text = _build_user_message(
        world=world,
        characters=characters,
        plot=plot,
        volume_plans=volume_plans,
        premise=premise,
        genre=genre,
        full_context=uses_experimental_pipeline(resolved_client),
        json_handoff=getattr(resolved_client, "handoff_format", None) == "json",
    )

    user_text = handoff_user_text(
        resolved_client, stage="outline_consistency_validator",
        data={"world": world, "characters": characters, "plot": plot, "volume_plans": volume_plans, "premise": premise, "genre": genre}, original_task=user_text,
    )
    try:
        response = resolved_client.complete(
            messages=[
                Message(role="system", content=system_text),
                Message(role="user", content=user_text),
            ],
            model=resolved_model,
            **complete_kwargs,
        )
        payload = _parse_llm_payload(
            raw_text=response.content, model_used=response.model_used,
            require_explicit_verdict=experimental,
        )
        if experimental:
            _validate_source_quotes(payload, {
                "world": world.model_dump(mode="json"),
                "characters": characters.model_dump(mode="json"),
                "plot": plot.model_dump(mode="json"),
                "volume_plans": volume_plans.model_dump(mode="json"),
                "premise": premise,
                "genre": genre,
            })
    except Exception as exc:  # noqa: BLE001 — legacy 保留 fail-open
        if isinstance(exc, HandoffError):
            raise
        if experimental:
            raise RuntimeError("实验大纲一致性审核没有有效结论，不能当作通过") from exc
        _log.warning(
            "outline_consistency_validator_failed_open",
            workspace_id=workspace_id,
            error_type=type(exc).__name__,
            error_message=str(exc),
        )
        return OutlineConsistencyResult(
            passed=True,
            llm_calls_count=1,
            fail_open=True,
        )

    passed = len(payload.contradictions) == 0
    # v2 s4-step1:逐条分类 → 可确定性修的 name_typo 子集 + 是否含不可修硬伤。
    contradiction_descs = [c.desc for c in payload.contradictions]
    name_typo_fixes = [
        NameTypoFix(wrong=c.wrong.strip(), correct=c.correct.strip(), desc=c.desc)
        for c in payload.contradictions
        if c.type == "name_typo" and c.wrong.strip() and c.correct.strip()
    ]
    # 不可修 = 任一条非 name_typo,或 name_typo 缺 wrong/correct(信息不全不敢修)。
    has_non_fixable = any(
        c.type != "name_typo" or not c.wrong.strip() or not c.correct.strip()
        for c in payload.contradictions
    )
    if passed:
        _log.debug(
            "outline_consistency_validator_passed",
            workspace_id=workspace_id,
            coverage_gap_count=len(payload.coverage_gaps),
            evidence=payload.evidence,
        )
    else:
        _log.info(
            "outline_consistency_validator_failed",
            workspace_id=workspace_id,
            contradictions=contradiction_descs,
            name_typo_fix_count=len(name_typo_fixes),
            has_non_fixable=has_non_fixable,
            coverage_gaps=payload.coverage_gaps,
            evidence=payload.evidence,
        )
    # coverage_gaps 是 WARN-only 标记(不影响 passed);非空时单独 log warn,
    # 让"卷规划覆盖缺口"在 OBSERVABILITY 可见(护留白:不阻断,仅提示)。
    if payload.coverage_gaps:
        _log.warning(
            "outline_consistency_coverage_gaps",
            workspace_id=workspace_id,
            coverage_gaps=payload.coverage_gaps,
            evidence=payload.evidence,
        )

    return OutlineConsistencyResult(
        passed=passed,
        contradictions=contradiction_descs,
        coverage_gaps=payload.coverage_gaps,
        evidence=payload.evidence,
        llm_calls_count=1,
        fail_open=False,
        name_typo_fixes=name_typo_fixes,
        has_non_fixable_contradiction=has_non_fixable,
        volume_plans_retry_allowed=(
            not passed and payload.repair_scope == "volume_plans_only"
        ),
        contradiction_details=[item.model_dump(mode="json") for item in payload.contradictions],
        repair_scope=payload.repair_scope,
    )


def validate_outline_stage_consistency(
    *,
    stage: Literal["characters", "plot"],
    world: WorldOutput,
    characters: CharactersOutput,
    plot: PlotOutput | None = None,
    premise: str = "",
    genre: str = "",
    workspace_id: str | None = None,
    router: ModelRouter | None = None,
    client: LLMClient | None = None,
    model: str | None = None,
    max_tokens: int = _DEFAULT_MAX_TOKENS,
    temperature: float = _DEFAULT_TEMPERATURE,
    prompts_dir: Path | None = None,
) -> OutlineConsistencyResult:
    """Review only the newly generated stage against its available sources.

    This reuses the existing consistency route and strict response contract.
    It is an additional physical request in experimental generation; a bad
    response is never interpreted as a pass.  No volume text is invented.
    """
    if (stage == "plot") != (plot is not None):
        raise ValueError("局部审核的阶段与材料不一致")
    resolved_client, resolved_model, kwargs = resolve_routing(
        agent_name="outline_consistency_validator",
        router=router, client=client, model=model,
        legacy_kwargs={"max_tokens": max_tokens, "temperature": temperature},
    )
    root = (prompts_dir if prompts_dir is not None else settings.prompts_dir).resolve()
    system = load_prompt_template(root / "outline_stage_consistency_validator_v3.md")
    kwargs = prepare_strict_json_output(
        client=resolved_client, model=resolved_model,
        output_model=_OutlineConsistencyLLMPayload,
        schema_name="outline_stage_consistency", complete_kwargs=kwargs,
    )
    kwargs = _review_generation_kwargs(resolved_client, resolved_model, kwargs)
    sources: dict[str, Any] = {
        "world": world.model_dump(mode="json"),
        "characters": characters.model_dump(mode="json"),
        "premise": premise,
        "genre": genre,
    }
    if plot is not None:
        sources["plot"] = plot.model_dump(mode="json")
    task = (
        f"只核对刚生成的 {stage} 与本次可见来源之间、以及该段内部的硬事实矛盾。"
        "普通留白和可自然承接的历史不阻断。"
    )
    task = append_role_marker(task, role="outline_consistency_validator")
    # A JSON handoff already carries every source field in data.  Keep the
    # textual fallback complete without duplicating that material on the
    # paid JSON route.
    original_task = (task if getattr(resolved_client, "handoff_format", None) == "json"
                     else task + "\n\n" + json.dumps(sources, ensure_ascii=False))
    user_text = handoff_user_text(
        resolved_client,
        stage="outline_consistency_validator",
        data={**sources, "review_stage": stage},
        original_task=original_task,
    )
    response = resolved_client.complete(
        messages=[Message(role="system", content=system), Message(role="user", content=user_text)],
        model=resolved_model, **kwargs,
    )
    if response.finish_reason not in {"stop", "end_turn"}:
        raise RuntimeError("局部大纲审核未正常完成")
    payload = _parse_llm_payload(
        raw_text=response.content, model_used=response.model_used,
        require_explicit_verdict=True,
    )
    _validate_source_quotes(payload, sources)
    for conflict in payload.contradictions:
        if not any(quote.path.startswith(stage + ".") for quote in conflict.source_quotes):
            raise ValueError("局部审核冲突没有引用新生成段")
    if payload.repair_scope not in {"none", "upstream"}:
        raise ValueError("局部审核修复归属无效")
    if bool(payload.contradictions) != (payload.repair_scope == "upstream"):
        raise ValueError("局部审核结论与修复归属不一致")
    return OutlineConsistencyResult(
        passed=not payload.contradictions,
        contradictions=[item.desc for item in payload.contradictions],
        contradiction_details=[item.model_dump(mode="json") for item in payload.contradictions],
        coverage_gaps=list(payload.coverage_gaps), evidence=payload.evidence,
        llm_calls_count=1, repair_scope=payload.repair_scope,
        has_non_fixable_contradiction=bool(payload.contradictions),
    )


def _validate_source_quotes(payload: _OutlineConsistencyLLMPayload | _SplitReviewPayload, sources: dict) -> None:
    """检验引用存在于指定字段；不声称代码能证明两句语义互斥或没有漏检。"""
    from src.core.agents.structured_handoff import build_packet

    # JSON handoff canonicalizes the same verified user source to data.user_request.
    # Accept that visible path as an alias, without inventing text or changing the
    # frozen source hash. Original pre-canonical paths remain valid for old calls.
    quote_sources = dict(sources)
    request = build_packet(stage="source_quote_validation", data=sources,
                           original_task="").data.get("user_request")
    if request is not None:
        quote_sources["user_request"] = request
    for conflict in payload.contradictions:
        if not conflict.reason.strip() or len({(q.path, q.quote) for q in conflict.source_quotes}) < 2:
            raise ValueError("冲突必须说明同条件关系并绑定两端不同原文")
        for item in conflict.source_quotes:
            segments = item.path.split(".")
            if "story_intent" in segments:
                intent_index = segments.index("story_intent")
                tail = segments[intent_index + 1:]
                confirmed_choice = (len(tail) == 4 and tail[0] == "authoring_plan"
                    and ((tail[1] == "milestones" and tail[3] == "event")
                         or (tail[1] == "entities" and tail[3] == "name"))
                    and tail[2].isdigit()
                    and (sources.get("world", {}).get("story_intent") or {}).get("version") == 7)
                if (len(segments) <= intent_index + 1
                        or (segments[intent_index + 1] != "user_request" and not confirmed_choice)):
                    raise ValueError("故事定位的模型设想不能作为硬矛盾事实引文")
            value = quote_sources
            try:
                for segment in item.path.split("."):
                    value = value[int(segment)] if isinstance(value, list) else value[segment]
            except (KeyError, IndexError, TypeError, ValueError) as exc:
                raise ValueError(f"审核引文位置不存在:{item.path}") from exc
            if not isinstance(value, str) or not item.quote.strip() or item.quote not in value:
                raise ValueError(f"审核引文不在指定原文中:{item.path}")


def _build_user_message(
    *,
    world: WorldOutput,
    characters: CharactersOutput,
    plot: PlotOutput,
    volume_plans: VolumePlansOutput,
    premise: str,
    genre: str,
    full_context: bool = False,
    json_handoff: bool = False,
) -> str:
    """实验终审见四段完整原文，普通路线保留既有身份/结构摘要。

    只渲染跨段一致性判定需要的字段(名字 / 角色身份 / 主线骨架 / 卷覆盖),
    不灌全文细节,控 token + 聚焦判定。
    """
    premise_text = premise.strip() if premise.strip() else "(未提供)"
    genre_text = genre.strip() if genre.strip() else "(未指定)"

    if full_context:
        body = (
            "## 背景\n\n"
            f"- 一句话设定(premise):{premise_text}\n"
            f"- 题材(genre):{genre_text}\n"
            + render_complete_outline_context(
                world=world, characters=characters,
                plot=plot, volume_plans=volume_plans,
                json_handoff=json_handoff,
            )
        )
        return append_role_marker(body, role="outline_consistency_validator")

    # characters:id + name + role + archetype + motivation(身份判定相关)。
    char_lines = []
    for c in characters.characters:
        rels = (
            "、".join(f"{k}:{v}" for k, v in c.relationships_brief.items())
            if c.relationships_brief
            else "(无)"
        )
        char_lines.append(
            f"  - {c.character_id} | 名:{c.name} | 定位:{c.role} | "
            f"原型:{c.archetype} | 动机:{c.motivation} | 关系:{rels}"
        )
    char_block = "\n".join(char_lines) if char_lines else "  (无)"

    # plot:核心冲突 + 阶段 + 转折 + 长伏笔 + 角色弧光(覆盖判定 + 角色一致性相关)。
    phase_lines = "\n".join(
        f"  - {p.phase_id}:{p.title} —— {p.summary}" for p in plot.main_phases
    )
    turning_lines = "\n".join(
        f"  - {t.turning_id}:{t.title} —— {t.description}"
        for t in plot.major_turning_points
    )
    thread_lines = "\n".join(
        f"  - {th.thread_id}:{th.title} —— {th.description}" for th in plot.long_threads
    )
    arc_lines = (
        "\n".join(f"  - {cid}:{arc}" for cid, arc in plot.character_arcs.items())
        if plot.character_arcs
        else "  (无)"
    )

    # volume_plans:卷 id + 标题 + 概要 + 章数 + 关联 plot 引用 + 卷内弧/戏剧源。
    vol_lines = []
    for v in volume_plans.volumes:
        linked = (
            f"phases={v.linked_phases} turning={v.linked_turning_points} "
            f"threads={v.linked_threads}"
        )
        arc_summ = "; ".join(f"{a.arc_type}:{a.summary}" for a in v.arcs)
        promise = (
            v.dramatic_promise.model_dump(mode="json")
            if v.dramatic_promise is not None
            else None
        )
        dramatic_beats = [
            {
                "beat_id": beat.beat_id,
                "chapter_range": list(beat.chapter_range),
                "dramatic_contract": beat.dramatic_contract.model_dump(mode="json"),
            }
            for arc in v.arcs
            for beat in arc.beats
            if beat.dramatic_contract is not None
        ]
        vol_lines.append(
            f"  - {v.volume_id} | 题:{v.title} | {v.chapter_count}章 | "
            f"概要:{v.summary}\n    关联:{linked}\n    卷内弧:{arc_summ}\n"
            f"    dramatic_promise:{promise}\n"
            f"    dramatic_beats:{dramatic_beats}"
        )
    vol_block = "\n".join(vol_lines) if vol_lines else "  (无)"

    body = (
        "## 背景\n\n"
        f"- 一句话设定(premise):{premise_text}\n"
        f"- 题材(genre):{genre_text}\n\n"
        "## 第1段 世界观(world)\n\n"
        f"- setting:{world.setting}\n"
        f"- power_system:{world.power_system}\n"
        f"- factions:{world.factions}\n"
        f"- culture:{world.culture}\n"
        f"- unique_rules:{world.unique_rules}\n\n"
        "## 第2段 角色(characters)\n\n"
        f"{char_block}\n\n"
        "## 第3段 主线(plot)\n\n"
        f"- core_conflict:{plot.core_conflict}\n"
        f"- main_phases:\n{phase_lines}\n"
        f"- major_turning_points:\n{turning_lines}\n"
        f"- long_threads:\n{thread_lines}\n"
        f"- character_arcs:\n{arc_lines}\n\n"
        "## 第4段 卷规划(volume_plans)\n\n"
        f"{vol_block}\n"
    )
    return append_role_marker(body, role="outline_consistency_validator")


def _parse_llm_payload(
    *, raw_text: str, model_used: str, require_explicit_verdict: bool = False,
) -> _OutlineConsistencyLLMPayload:
    """剥代码块 → ``json.loads`` → Pydantic 校验。失败抛 ``ValueError``。"""
    text = _strip_code_fence(raw_text).strip()
    try:
        data = loads_with_quote_repair(
            text,
            agent_name="outline_consistency_validator",
            model_used=model_used,
        )
    except json.JSONDecodeError as exc:
        raise ValueError(
            f"outline_consistency_validator LLM 输出非合法 JSON(model={model_used}): "
            f"{raw_text[:200]!r}"
        ) from exc
    try:
        payload = _OutlineConsistencyLLMPayload.model_validate(data)
    except ValidationError as exc:
        raise ValueError(
            f"outline_consistency_validator LLM 输出 schema 不符(model={model_used}): "
            f"{exc.errors()}"
        ) from exc
    if require_explicit_verdict and "contradictions" not in payload.model_fields_set:
        raise ValueError("实验终审必须显式返回 contradictions；无矛盾也须明确给空数组")
    return payload


def _strip_code_fence(text: str) -> str:
    """剥 trailing HTML 注释 + ``` / ```json 包裹(沿用 mechanism_consistency_validator 同型)。"""
    while True:
        stripped = _TRAILING_HTML_COMMENT_RE.sub("", text)
        if stripped == text:
            break
        text = stripped
    match = _CODE_FENCE_RE.match(text)
    if match is not None:
        return match.group("body")
    return text


def format_outline_consistency_failure_for_retry(
    *, result: OutlineConsistencyResult
) -> str:
    """格式化 validator 失败信息,供 caller 抛 OutlineBuilderError / 用户读懂。

    设计:让用户 / 生成端能直接读出"哪几处跨段矛盾",再 regenerate-section 时主动
    消除(让同一实体名字一致 / 不混淆角色)。
    """
    parts = ["大纲一致性校验失败 —— 存在有原文依据的矛盾(必须修复):"]
    for i, c in enumerate(result.contradictions, start=1):
        parts.append(f"  {i}. {c}")
    parts.append(
        "  请回到冲突原文所在的 world / characters / plot / volume_plans 修订，"
        "统一身份、关系、开篇前态与事件顺序；不能只改卷规划掩盖上游矛盾。"
    )
    if result.coverage_gaps:
        parts.append("  (另有卷规划覆盖提示,非阻断):")
        for g in result.coverage_gaps:
            parts.append(f"    - {g}")
    if result.evidence:
        parts.append(f"  - LLM 判定依据:{result.evidence}")
    return "\n".join(parts)


def apply_name_typo_fixes(
    *,
    world: WorldOutput,
    characters: CharactersOutput,
    plot: PlotOutput,
    volume_plans: VolumePlansOutput,
    fixes: list[NameTypoFix],
    workspace_id: str | None = None,
) -> tuple[
    tuple["WorldOutput", "CharactersOutput", "PlotOutput", "VolumePlansOutput"],
    list[NameTypoFix],
    list[dict],
]:
    """确定性应用「名字写岔」修复(v2 W3.5-s4-step1),返 (修后4段, 已应用, 已跳过)。

    **多数票护栏(防 LLM 把 canonical 和 typo 标反)**:仅当 ``correct`` 在 4 段
    序列化全文出现次数 **>** ``wrong`` 时才应用。另外跳过:空串 / 相等 / 互为子串
    (防部分替换乱套)。在 4 段 ``model_dump_json`` 文本上做**全名串替换**,再
    ``model_validate_json`` 反序列化回模型(名字是 JSON 字段值,CJK 全名串不与 JSON
    语法冲突)。

    设计纪律:**零 fail-open** —— 任何 fix 未过护栏都进 ``skipped`` 不应用;
    caller(build_outline)修后**必须重跑 validator** 确认干净才放行,否则维持 HARD。
    一个 fix 都没应用时,原样返回 4 段(不反序列化,零开销)。
    """
    from src.core.models.outline import (
        CharactersOutput,
        PlotOutput,
        VolumePlansOutput,
        WorldOutput,
    )

    dumps = {
        "world": world.model_dump_json(),
        "characters": characters.model_dump_json(),
        "plot": plot.model_dump_json(),
        "volume_plans": volume_plans.model_dump_json(),
    }
    applied: list[NameTypoFix] = []
    skipped: list[dict] = []
    for fix in fixes:
        w, c = fix.wrong.strip(), fix.correct.strip()
        if not w or not c or w == c:
            skipped.append({"wrong": w, "correct": c, "reason": "空串或相等"})
            continue
        if w in c or c in w:
            skipped.append({"wrong": w, "correct": c, "reason": "互为子串(防部分替换)"})
            continue
        combined = "".join(dumps.values())
        w_count, c_count = combined.count(w), combined.count(c)
        if not (w_count > 0 and c_count > w_count):
            skipped.append(
                {
                    "wrong": w,
                    "correct": c,
                    "reason": f"多数票不过(correct={c_count}/wrong={w_count})",
                }
            )
            _log.warning(
                "outline_autofix_name_typo_skipped",
                workspace_id=workspace_id,
                wrong=w,
                correct=c,
                correct_count=c_count,
                wrong_count=w_count,
                reason="majority_vote_failed",
            )
            continue
        for name in dumps:
            dumps[name] = dumps[name].replace(w, c)
        applied.append(NameTypoFix(wrong=w, correct=c, desc=fix.desc))
        _log.info(
            "outline_autofix_name_typo_applied",
            workspace_id=workspace_id,
            wrong=w,
            correct=c,
            correct_count=c_count,
            wrong_count=w_count,
        )
    for s in skipped:
        if s["reason"].startswith("多数票"):
            continue  # 已在上面 warn
        _log.warning(
            "outline_autofix_name_typo_skipped",
            workspace_id=workspace_id,
            wrong=s["wrong"],
            correct=s["correct"],
            reason=s["reason"],
        )
    if not applied:
        return (world, characters, plot, volume_plans), applied, skipped
    return (
        (
            WorldOutput.model_validate_json(dumps["world"]),
            CharactersOutput.model_validate_json(dumps["characters"]),
            PlotOutput.model_validate_json(dumps["plot"]),
            VolumePlansOutput.model_validate_json(dumps["volume_plans"]),
        ),
        applied,
        skipped,
    )


__all__ = [
    "OutlineConsistencyResult",
    "NameTypoFix",
    "validate_outline_consistency",
    "format_outline_consistency_failure_for_retry",
    "apply_name_typo_fixes",
]
