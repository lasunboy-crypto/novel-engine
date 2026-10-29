"""Reviser agent 简版(P1-B-12,v1.1 G8 起接 validator_failures 入参)。

职责:接收本章正文 + Reviewer 产出的 ``ReviewIssue`` 列表 + (v1.1 G8 新加)
validator 硬约束失败明细 → 拼成 system + user 两条消息的 prompt → 调注入的
``LLMClient`` → 把响应包成 ``ReviserOutput`` 返回。

设计要点:
- **依赖反转**:函数签名只接 ``LLMClient`` 协议,不 import ``ClaudeClient``;
  具体客户端实例由 P1-B-13 Orchestrator 注入。
- **代码可执行保序**:生产 caller 传入 ``ProseRevisionContract`` 时，安全局部问题
  只接受稳定锚点 patch 并由代码合成整章；显式 global 才接受完整正文。legacy
  caller 未传合同时保留原返回整章行为。
- **提示词模板与变量分离**:静态 system 文本来自 ``prompts/reviser.md``,
  动态变量(原文 / ReviewIssue 列表 / validator_failures)由 Python 拼到 user
  消息;改提示词无需动 Python。
- **提示词路径来自 Settings**:函数参数 ``prompts_dir`` 默认 ``None`` 时
  从 ``settings.prompts_dir`` 取(P3 工作区级覆盖时 Reviser 零改动)。
- **空 issues + 空 validator_failures 兜底**:理论上 Orchestrator 不会在
  两者皆空时调 Reviser,但仍允许调用、原样回原文以避免 LLM 编造改动。

v1.1 G8 新加:``validator_failures`` 入参(Optional list[dict])来自
``ReviewResult.validator_failure_details`` 字段。``_apply_validator_pipeline``
按其共享 light/heavy 阈值把单条 ``word_count`` 轻量失败派到 revise，Reviser
不复制也不重新解释该阈值，只按已路由的真实失败做局部 patch。

本 Agent 不新增自动重试；Provider 响应仍只发生一次，patch 验证和合成均为本地纯
代码。
"""

from __future__ import annotations

from pathlib import Path
from typing import Any

import structlog
from pydantic import BaseModel, Field

from src.config import settings
from src.core.agents._prompt import (
    append_role_marker,
    load_prompt_template,
    resolve_routing,
)
from src.core.agents._structured_output import prepare_strict_json_output
from src.core.agents.writer_salience import build_writer_authority_sections
from src.core.llm.base import LLMClient, Message, TokenUsage
from src.core.models.chapter_card import ChapterCard
from src.core.models.core_state import CoreStateProjection
from src.core.models.review import ConstraintResult, ReviewIssue
from src.core.routing.router import ModelRouter
from src.core.rules.prose_hard_contract import (
    ExecutableConstraintPlan,
    PARAGRAPH_MEAN_HARD_CONSTRAINT_ID,
    compile_executable_constraint_plan,
    paragraph_mean_hard_rule,
    render_executable_constraint_plan,
)
from src.core.rules.prose_revision_contract import (
    LocalizedRevisionResponse,
    ProseRevisionContract,
    ProseRevisionContractError,
    apply_revision_response,
    render_revision_contract,
)

_log = structlog.get_logger(__name__)

_REVISER_PROMPT_FILENAME = "reviser.md"

# Reviser 默认生成参数:温度介于审稿(0.3)与写作(0.85)之间——既要保
# 留原文笔调又要有改动;max_tokens 同 Writer 覆盖 4500 字章节上限。
_DEFAULT_TEMPERATURE = 0.5
_DEFAULT_MAX_TOKENS = 8000


class ReviserOutput(BaseModel):
    """Reviser 单次修订输出。

    字段与 ``WriterOutput`` 对齐(``content`` / ``model_used`` /
    ``token_usage`` / ``finish_reason``)。若带 revision contract，``content`` 已是
    代码验证 patch 后机械合成的整篇候选；调用方仍走同一版本路径。
    """

    content: str
    model_used: str
    token_usage: TokenUsage
    finish_reason: str | None = None
    executable_constraint_plan: ExecutableConstraintPlan | None = Field(
        default=None, exclude=True
    )
    revision_contract: ProseRevisionContract | None = Field(
        default=None, exclude=True
    )


def revise_chapter(
    *,
    chapter_text: str,
    issues: list[ReviewIssue],
    validator_failures: list[dict[str, Any]] | None = None,
    chapter_card: ChapterCard | None = None,
    constraint_results: list[ConstraintResult] | None = None,
    router: ModelRouter | None = None,
    client: LLMClient | None = None,
    model: str | None = None,
    max_tokens: int = _DEFAULT_MAX_TOKENS,
    temperature: float = _DEFAULT_TEMPERATURE,
    prompts_dir: Path | None = None,
    executable_constraint_plan: ExecutableConstraintPlan | None = None,
    revision_contract: ProseRevisionContract | None = None,
    core_state_projection: CoreStateProjection | None = None,
) -> ReviserOutput:
    """按代码预先选择的 localized / global 合同修订单章正文。

    :param chapter_text: 待修订的本章原文(由调用方从 ``ChapterVersion``
        文件读出);Reviser 不再读盘。
    :param issues: 来自 ``ReviewResult.revise_targets`` 的命中问题列表;
        每条带 ``paragraph_anchor`` / ``issue_type`` / ``description`` /
        ``suggestion`` 四个字段,Reviser 据此定位 + 修订。
    :param validator_failures: v1.1 G8 新加 — 来自
        ``ReviewResult.validator_failure_details`` 字段(由 ``_apply_validator_pipeline``
        在 light 失败时写入,典型 = ``[{"name": "word_count", "details": {...}}]``)。
        ``None`` 或空列表 = reviser 走原 ReviewIssue patch 路径(reviewer 自然
        verdict=revise 路径不触发 validator pipeline 失败,无此入参)。
    :param chapter_card: R9 约束共尺新增；生产 caller 透传同一 ChapterCard，让
        Reviser 看见 Writer 与下游 validator 使用的字数、内容和衔接精确来源。
        ``None`` 仅保留 legacy / 单测兼容，此时不得猜阈值。
    :param constraint_results: R9 约束共尺新增；上一轮结构化 HARD 结果。pass 项
        必须保留，fail 项必须修复；不由 Reviser 重判级别。
    :param router: P3-B-06 主路径。``ModelRouter`` 实例;给定时按
        ``router.route("reviser")`` 取 client + model + 生成参数。
    :param client: legacy 路径 LLM 客户端;``router=None`` 时必传。
    :param model: legacy 路径模型名;``router=None`` 时必传。
    :param max_tokens: legacy 路径输出 token 上限,默认 8000;``router``
        路径下由 profile 决定,本参数被忽略。
    :param temperature: legacy 路径采样温度,默认 0.5;``router`` 路径下由
        profile 决定,本参数被忽略。
    :param prompts_dir: 提示词模板根目录;``None`` 时取 ``settings.prompts_dir``。
        测试可传临时目录注入 fake 模板。
    :param core_state_projection: 与 ChapterCard 冻结 hash 匹配的已提交核心状态投影；
        只供 Writer/Reviser 共用权威装配与计划校验，缺失时不得重建或猜测。
    :return: ``ReviserOutput``,包含修订后整篇正文、实际响应模型名、
        token 使用量与结束原因。
    :raises TypeError: ``router=None`` 且 ``client`` / ``model`` 任一缺失。
    """
    if executable_constraint_plan is not None and chapter_card is None:
        raise ValueError("传入 ExecutableConstraintPlan 时必须同时传 ChapterCard")
    if chapter_card is not None:
        executable_constraint_plan = (
            executable_constraint_plan
            or compile_executable_constraint_plan(
                chapter_card=chapter_card,
                stage="revise",
                core_state_projection=core_state_projection,
            )
        )
        executable_constraint_plan.assert_matches_chapter_card(chapter_card)
        executable_constraint_plan.assert_matches_core_state(
            chapter_card=chapter_card,
            projection=core_state_projection,
        )
        if executable_constraint_plan.stage != "revise":
            raise ValueError("ExecutableConstraintPlan stage 必须为 revise")
        for result in constraint_results or []:
            executable_constraint_plan.assert_result_matches(result)
    if revision_contract is not None:
        if executable_constraint_plan is None:
            raise ValueError(
                "传入 revision contract 时必须同时传 ExecutableConstraintPlan"
            )
        revision_contract.assert_compatible(
            source_content=chapter_text,
            source_version_id=revision_contract.source_version_id,
            executable_constraint_plan=executable_constraint_plan,
        )

    resolved_client, resolved_model, complete_kwargs = resolve_routing(
        agent_name="reviser",
        router=router,
        client=client,
        model=model,
        legacy_kwargs={"max_tokens": max_tokens, "temperature": temperature},
    )

    template_root = (
        prompts_dir if prompts_dir is not None else settings.prompts_dir
    ).resolve()
    system_text = load_prompt_template(template_root / (
        "reviser_narrative_v5.md" if chapter_card is not None and chapter_card.narrative_context is not None and chapter_card.narrative_context.story_intent.version >= 6
        else "reviser_narrative_v4.md" if chapter_card is not None and chapter_card.word_count_policy_version and chapter_card.narrative_context is not None
        else "reviser_length_v1.md" if chapter_card is not None and chapter_card.word_count_policy_version
        else "reviser_narrative_v1.md" if chapter_card is not None and chapter_card.narrative_context is not None
        else _REVISER_PROMPT_FILENAME
    ))
    user_text = _build_user_message(
        chapter_text=chapter_text,
        issues=issues,
        validator_failures=validator_failures,
        chapter_card=chapter_card,
        constraint_results=constraint_results,
        executable_constraint_plan=executable_constraint_plan,
        revision_contract=revision_contract,
        core_state_projection=core_state_projection,
    )
    if revision_contract is not None and revision_contract.mode == "localized":
        complete_kwargs = prepare_strict_json_output(
            client=resolved_client,
            model=resolved_model,
            output_model=LocalizedRevisionResponse,
            schema_name="localized_revision",
            complete_kwargs=complete_kwargs,
        )

    response = resolved_client.complete(
        messages=[
            Message(role="system", content=system_text),
            Message(role="user", content=user_text),
        ],
        model=resolved_model,
        **complete_kwargs,
    )

    # 篇幅不再是拒稿条件；空响应和 Provider 明示截断仍是技术失败。
    # 在合同合成与落版之前拒收，不按字数猜测截断，也不新增重试。
    if chapter_card is not None and chapter_card.word_count_policy_version:
        if not response.content.strip():
            raise ProseRevisionContractError(
                "Reviser 返回空响应，拒绝落版（empty_response）"
            )
        if response.finish_reason in {"length", "max_tokens"}:
            raise ProseRevisionContractError(
                "Reviser 响应被 Provider 明示截断，拒绝落版"
                f"（provider_truncated: {response.finish_reason}）"
            )

    content = (
        apply_revision_response(
            contract=revision_contract,
            raw_content=response.content,
        )
        if revision_contract is not None
        else response.content
    )

    # P1-B-24:OBSERVABILITY §2.4 `revision_completed` 事件。为保持既有指标语义，
    # ``revised_paragraphs_count`` 继续用 issues_count；localized 合同可能把多个
    # issue 合并到同一锚点，故这里不是物理 patch 数。
    # v1.1 G8:追加 ``validator_failures_count`` 维度,便于 LogsPage / metrics
    # 区分 reviser revise 触发源(reviewer issues vs validator pipeline light)。
    _log.info(
        "revision_completed",
        revised_paragraphs_count=len(issues),
        validator_failures_count=len(validator_failures or []),
        model_used=response.model_used,
        finish_reason=response.finish_reason,
    )

    return ReviserOutput(
        content=content,
        model_used=response.model_used,
        token_usage=response.usage,
        finish_reason=response.finish_reason,
        executable_constraint_plan=executable_constraint_plan,
        revision_contract=revision_contract,
    )


def _build_user_message(
    *,
    chapter_text: str,
    issues: list[ReviewIssue],
    validator_failures: list[dict[str, Any]] | None = None,
    chapter_card: ChapterCard | None = None,
    constraint_results: list[ConstraintResult] | None = None,
    executable_constraint_plan: ExecutableConstraintPlan | None = None,
    revision_contract: ProseRevisionContract | None = None,
    core_state_projection: CoreStateProjection | None = None,
) -> str:
    """user 消息:审稿意见列表 + (可选)validator 硬约束失败明细 + 待修订原文。

    审稿意见 / validator 明细放前面、原文放后面,符合"先看任务清单再读材料"
    的阅读顺序;LLM 在生成时也会把任务清单留在 attention 里。

    v1.1 G8:validator_failures 段在两类来源都存在时同时渲染;reviser prompt
    指引 LLM 分 A/B 段处理(A 段处理 ReviewIssue,B 段处理 validator failures)。
    """
    contract_block = _format_reviser_hard_gate_contract(
        chapter_card=chapter_card,
        constraint_results=constraint_results,
        executable_constraint_plan=executable_constraint_plan,
        core_state_projection=core_state_projection,
    )
    authority_block = (
        build_writer_authority_sections(
            chapter_card=chapter_card,
            core_state_projection=core_state_projection,
        ).combined()
        if chapter_card is not None
        else ""
    )
    issues_block = "## 审稿意见(只改命中段落)\n\n" + _render_issues(issues)
    validator_block = (
        "## validator 硬约束失败明细(v1.1 G8 局部 patch 路径)\n\n"
        + _render_validator_failures(validator_failures)
    )
    text_block = (
        render_revision_contract(revision_contract)
        if revision_contract is not None
        else "## 本章原文(待修订)\n\n" + chapter_text
    )
    # P1-B-22:追加 agent 身份 marker(HTML 注释,真 LLM 视为字面忽略)。
    user_request_block = ""
    if chapter_card is not None and chapter_card.narrative_context is not None:
        request = chapter_card.narrative_context.story_intent.user_request
        if request is not None:
            from src.core.models.narrative import UserNarrativeRequest
            UserNarrativeRequest.model_validate(request.model_dump(mode="json"))
            user_request_block = (
                "## 用户原始前提与阅读要求（文学方向，修订不提前兑现未来剧情）\n\n"
                + request.premise
                + ("\n\n整体阅读方向（原文）：\n" + request.reading_preferences
                   if request.version == 2 and request.reading_preferences is not None else "")
            )
    sections = [
        section
        for section in (
            user_request_block,
            authority_block,
            contract_block,
            issues_block,
            validator_block,
            text_block,
        )
        if section
    ]
    return append_role_marker("\n\n".join(sections), role="reviser")


def _format_reviser_hard_gate_contract(
    *,
    chapter_card: ChapterCard | None,
    constraint_results: list[ConstraintResult] | None,
    executable_constraint_plan: ExecutableConstraintPlan | None = None,
    core_state_projection: CoreStateProjection | None = None,
) -> str:
    """渲染 Reviser 必须修 / 必须保序的同源合同。"""
    if chapter_card is None:
        rendered_plan = (
            "- ChapterCard 未透传（legacy 路径）：不得猜字数或内容阈值；只做命中 patch，"
            "保持原文其余内容与长度。"
        )
    else:
        plan = executable_constraint_plan or compile_executable_constraint_plan(
            chapter_card=chapter_card,
            stage="revise",
            core_state_projection=core_state_projection,
        )
        plan.assert_matches_chapter_card(chapter_card)
        plan.assert_matches_core_state(
            chapter_card=chapter_card,
            projection=core_state_projection,
        )
        if plan.stage != "revise":
            raise ValueError("ExecutableConstraintPlan stage 必须为 revise")
        rendered_plan = render_executable_constraint_plan(plan).replace(
            "## 可执行 HARD 约束计划(EXECUTABLE CONSTRAINT PLAN)",
            "## 局部修订约束合同(REVISER/VALIDATOR SSOT)",
            1,
        )
    if chapter_card is not None and chapter_card.word_count_policy_version:
        from src.core.rules.prose_length_policy import render_word_count_task
        rendered_plan = render_word_count_task(chapter_card) + "\n\n" + rendered_plan
    structured_lines = [
        (
            f"- [{result.status.upper()}][{result.domain}] "
            f"`{result.constraint_id}`:{result.rule_text}"
        )
        for result in constraint_results or []
    ]
    structured = "\n".join(structured_lines) if structured_lines else "- 无结构化结果"
    paragraph_rule = (
        "- 段均、句长和修辞密度只供观察，不是修订的硬门槛。"
        if chapter_card is not None and chapter_card.narrative_context is not None else
        f"- `{PARAGRAPH_MEAN_HARD_CONSTRAINT_ID}` 仍按计划表中的共享规则 "
        f"`{paragraph_mean_hard_rule()}` 执行。"
    )
    return (
        f"{rendered_plan}\n\n"
        "### 局部修复边界\n\n"
        "- 同一约束的身份、级别、数值、来源与例外不得改变；只能修复上一轮"
        "失败 ID 指向的结果，上一轮 PASS 必须保留。\n"
        "- WARN/SOFT 不得升级为 HARD；不得为了一个局部修复回退其他已通过项。\n"
        f"{paragraph_rule}"
        "\n\n### 上一轮结构化 HARD 结果\n"
        f"{structured}"
    )


def _render_issues(issues: list[ReviewIssue]) -> str:
    """把 ``ReviewIssue`` 列表渲染成 markdown。

    空列表渲染成"无"——理论上不该走到 Reviser,但兜底:此时配合提示词
    "审稿意见为空时原文一字不改原样输出"的指令,LLM 会原样回原文。
    """
    if not issues:
        return "无"

    lines: list[str] = []
    for index, issue in enumerate(issues, start=1):
        lines.append(
            f"### 意见 {index}\n\n"
            f"- `paragraph_anchor`:{issue.paragraph_anchor}\n"
            f"- `issue_type`:{issue.issue_type}\n"
            f"- `description`:{issue.description}\n"
            f"- `suggestion`:{issue.suggestion}"
        )
    return "\n\n".join(lines)


def _render_validator_failures(
    failures: list[dict[str, Any]] | None,
) -> str:
    """v1.1 G8:把 validator_failures 列表渲染成 markdown。

    每条 = ``{"name": <validator>, "details": <dict>}``;details 字段集因
    validator 而异(word_count:actual_word_count / word_count_min /
    word_count_max / reason)。渲染成 LLM 友好的"键值列表"格式,prompt 内
    会指引 LLM 按 name 派 patch 路径。

    空列表(或 None)渲染成"无"——典型场景 = reviewer 自然 verdict=revise 路径
    (无 validator 失败),配合 prompt"validator_failures 为空时只处理审稿意见"
    指令,LLM 走原 ReviewIssue patch 路径。
    """
    if not failures:
        return "无"

    lines: list[str] = []
    for index, failure in enumerate(failures, start=1):
        name = failure.get("name", "<unknown>")
        details = failure.get("details") or {}
        details_lines = "\n".join(
            f"  - `{key}`:{value}" for key, value in details.items()
        )
        lines.append(
            f"### 失败 {index}\n\n- `name`:{name}\n- `details`:\n{details_lines}"
        )
    return "\n\n".join(lines)
