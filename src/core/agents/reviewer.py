"""Reviewer agent(P1-B-11 简版 + P2-B-11 voice 注入 + P7-B-06 解耦 must_achieve)。

职责:接收正文 + 章节卡 → 拼成 system + user 两条消息 → 调注入的
``LLMClient`` → 解析 LLM 输出的 JSON → 按 PROJECT_SPEC §4.4 公式与规则链
计算综合分与判决 → 返回结构化 ``ReviewResult``。

设计要点:
- **依赖反转**:函数签名只接 ``LLMClient`` 协议,不 import ``ClaudeClient``。
- **JSON 输出契约(P7-B-06 起简化)**:LLM 产 6 个分项分 + issues + suggestions,
  **不再产 hard_constraints**;``score_overall`` / ``verdict`` 由代码确定性算。
  hard_constraints 由 caller(``chapter_orchestrator``)综合
  ``must_achieve_validator`` + ``validator_pipeline`` 给出最终判定。
- **解析失败抛 `ReviewerParseError`**(带 ``raw_output`` / ``model_used``)
  + structlog 一条 ``reviewer_parse_failed`` 事件,P1-B-13 Orchestrator
  收到错可重试或升人工;不降级到 rewrite。
- **escalate_to_human 不在 Reviewer 范围**:它没有 revise_count / rewrite_count,
  这条规则交给 Orchestrator(P1-B-13)。
- **P2-B-11 voice 注入**:可选注入 ``db_session: Session | None``,非空时按
  ``card.related_characters`` 拉每角色 ``voice_profile`` 拼"## 出场角色声音
  档案(参考)"段附到 user message(constraints 块之后、待审正文之前),供
  LLM 对账打 ``score_voice_consistency``。``db_session=None`` 时退化为 P1
  路径,Orchestrator / 历史调用方零改动。
- **P7-B-06 解耦 must_achieve**:Reviewer 不再判定 ``must_achieve``;
  ``_ReviewerLLMPayload`` 删 ``hard_constraints_passed`` / ``failed_constraints``
  字段;reviewer 构造的 ``ReviewResult`` 默认 ``hard_constraints_passed=True``
  + ``failed_constraints=[]`` 让 caller 综合其他 validator 后覆盖。
  ``_decide_verdict`` 只看 ``score_overall`` 与阈值,不再读 hard_constraints。
  对应 ``must_achieve_validator`` 独立 agent(N 次 yes/no LLM 调用,温度 0.3),
  详见 ``src/core/agents/must_achieve_validator.py``。

P1 不实现(已在 P2-B-11 部分覆盖):跨章 voice profile 对照(本任务 voice
段为本章出场角色档案的"当前快照",非历史对照,后者归 P3+)。仍不实现:
风格漂移评分、完整 validator。
"""

from __future__ import annotations

import json
import re
import uuid
from datetime import UTC, datetime
from pathlib import Path
from typing import Annotated, Any, Literal

import json_repair
import structlog
from pydantic import BaseModel, Field, ValidationError
from sqlalchemy.orm import Session

from src.config import settings
from src.core.agents._prompt import (
    append_role_marker,
    load_prompt_template,
    resolve_routing,
)
from src.core.agents._structured_output import prepare_strict_json_output
from src.core.agents.writer_narrative_input import active_character_ids
from src.core.llm.base import LLMClient, Message
from src.core.llm.local_json_contract import LocalJsonContract, LocalJsonResponseViolation
from src.core.memory.store import get_character
from src.core.models.character import Character
from src.core.models.chapter_card import ChapterCard
from src.core.models.review import (
    REVIEWER_ISSUES_MAX_ITEMS,
    REVIEWER_SUGGESTION_MAX_CHARS,
    REVIEWER_SUGGESTIONS_MAX_ITEMS,
    REVIEW_ISSUE_DESCRIPTION_MAX_CHARS,
    REVIEW_ISSUE_PARAGRAPH_ANCHOR_MAX_CHARS,
    REVIEW_ISSUE_SUGGESTION_MAX_CHARS,
    REVIEW_ISSUE_TYPE_MAX_CHARS,
    ReviewIssue,
    ReviewResult,
)
from src.core.routing.router import ModelRouter
from src.core.rules.prose_hard_contract import (
    ExecutableConstraintPlan,
    PARAGRAPH_MEAN_HARD_CONSTRAINT_ID,
    paragraph_mean_hard_rule,
    render_executable_constraint_plan,
)
from src.core.rules.literary_signals import render_contrast_reframe_review_section

_log = structlog.get_logger(__name__)

_REVIEWER_PROMPT_FILENAME = "reviewer.md"

# Reviewer 默认生成参数:审稿求稳,温度低;输出只装一份 JSON,不需长文本。
_DEFAULT_TEMPERATURE = 0.3
_DEFAULT_MAX_TOKENS = 2000

# PROJECT_SPEC §4.4 综合分加权(权重总和 1.0)。
# **无模板路径权重(保持不变,零回归)**:工作区未选 style_template 时,
# score_overall 用本 6 维加权,与 I2.2 之前逐字节一致 → 保护 G8 reviewer 基线。
_SCORE_WEIGHTS: dict[str, float] = {
    "score_plot_progression": 0.25,
    "score_tension_match": 0.20,
    "score_consistency": 0.20,
    "score_voice_consistency": 0.15,
    "score_prose_quality": 0.10,
    "score_hook_management": 0.10,
}

# v1.1 wave 4 s5 / I2.2:**选了模板路径权重(7 维,总和 1.0)**。
# 维护人拍板方案 A(2026-06-01):新维度 ``score_template_conformance`` 占 0.10,
# 从"文笔"(prose 0.10→0.05)+"钩子"(hook 0.10→0.05)各匀 0.05 —— 这两维与
# "模板符合度"同属"读者体感 / 锦上添花"类,取权最自然;**实质维度
# (plot/tension/consistency/voice)原量级不动**,verdict 基线扰动最小。
# **仅当工作区选了 style_template 且 LLM 给出 score_template_conformance 时启用**。
_SCORE_WEIGHTS_WITH_TEMPLATE: dict[str, float] = {
    "score_plot_progression": 0.25,
    "score_tension_match": 0.20,
    "score_consistency": 0.20,
    "score_voice_consistency": 0.15,
    "score_prose_quality": 0.05,
    "score_hook_management": 0.05,
    "score_template_conformance": 0.10,
}

# 匹配 ```json ... ``` 或 ``` ... ``` 包裹;LLM 偶发把 JSON 套进代码块,
# 即使 prompt 里禁止也要兜住。
_CODE_FENCE_RE = re.compile(
    r"^\s*```(?:json)?\s*\n(?P<body>.*?)\n```\s*$",
    re.DOTALL,
)

# P1-B-22:trailing HTML 注释剥离。
# 真 LLM 偶发把 user message 末尾的 ``<!-- agent_role: reviewer -->`` marker
# 复读到 JSON 输出尾部,产生 ``{"verdict": ...}\n<!-- agent_role: reviewer -->``
# 形态导致 ``json.loads`` 失败。在 ``_strip_code_fence`` 里顺手剥掉,与
# 代码块包裹同属"LLM 输出格式杂质"。
_TRAILING_HTML_COMMENT_RE = re.compile(
    r"\s*<!--.*?-->\s*$",
    re.DOTALL,
)


class ReviewerParseError(ValueError):
    """LLM 输出无法解析为合法审稿 JSON 时抛出。

    保留原始输出 + 实际模型名以便 Orchestrator 重试决策与 P7 调优时回查根因。
    """

    def __init__(self, message: str, *, raw_output: str, model_used: str) -> None:
        super().__init__(message)
        self.raw_output = raw_output
        self.model_used = model_used


class _ReviewerLLMPayload(BaseModel):
    """LLM 输出的中间模型(只校验 LLM 该产出的字段)。

    ``score_overall`` / ``verdict`` / ``revise_targets`` 由代码补,**不**让
    LLM 提供,避免模型给出与 6 个分项分自相矛盾的判决或综合分。

    **P7-B-06 起字段简化**:删 ``hard_constraints_passed`` 与
    ``failed_constraints`` 字段 — Reviewer 不再判定硬约束(must_achieve 走
    独立 validator agent,字数 / 禁止 / 视角 等走 ``validator_pipeline``)。
    LLM 仍可能输出这两个字段(沿用旧 prompt 的真 Claude / mock),Pydantic
    默认 ``extra=ignore``,**多余字段静默丢弃**不抛错;reviewer 构造 ``ReviewResult``
    时填 ``hard_constraints_passed=True`` + ``failed_constraints=[]`` 默认值,
    由 caller 综合其他 validator 后覆盖。
    """

    score_plot_progression: float = Field(ge=0.0, le=10.0)
    score_tension_match: float = Field(ge=0.0, le=10.0)
    score_prose_quality: float = Field(ge=0.0, le=10.0)
    score_consistency: float = Field(ge=0.0, le=10.0)
    score_hook_management: float = Field(ge=0.0, le=10.0)
    score_voice_consistency: float = Field(ge=0.0, le=10.0)
    # v1.1 wave 4 s5 / I2.2:模板符合度分。**optional**(默认 None):
    # ① 无模板工作区 reviewer 不注入"## 风格模板期望"段,LLM 可不产此字段;
    # ② 真 LLM 偶发漏产也不抛错(graceful 退化到 6 维加权)。
    # 只有工作区选了模板 **且** LLM 给出该分项时,才进 7 维加权。
    score_template_conformance: float | None = Field(default=None, ge=0.0, le=10.0)
    # M2(v1.1 wave 5 s2 / G28):画面性分。**任何题材都评**(不像 template
    # 仅选模板才评);未选模板时 LLM 只按近普世底线打分。optional 容错:真 LLM
    # 偶发漏产 → None(下游存 None,不进任何加权、不影响 verdict)。
    score_imagery: float | None = Field(default=None, ge=0.0, le=10.0)
    # M2(v1.1 wave 5 s3):内心戏分。同 imagery —— 任何题材都评(无 gate),
    # 未选模板时只按近普世底线打分;optional 容错(LLM 漏产 → None)。
    score_interiority: float | None = Field(default=None, ge=0.0, le=10.0)
    # M2(v1.1 wave 5 s9):套路化分(**越高越套路=越差**,唯一反极性维度)+ 沉浸感分。
    # 同 imagery/interiority —— 任何题材都评(无 gate),optional 容错(漏产 → None);
    # 均**不进 score_overall**。校验仍 0-10(套路化高=套路重,极性在消费侧处理)。
    score_formulaic: float | None = Field(default=None, ge=0.0, le=10.0)
    score_immersion: float | None = Field(default=None, ge=0.0, le=10.0)
    issues: list[ReviewIssue] = Field(max_length=REVIEWER_ISSUES_MAX_ITEMS)
    suggestions: list[
        Annotated[str, Field(max_length=REVIEWER_SUGGESTION_MAX_CHARS)]
    ] = Field(max_length=REVIEWER_SUGGESTIONS_MAX_ITEMS)


def _apply_reviewer_business_bounds(schema: dict[str, Any]) -> None:
    """把 F5 业务上限写进发给支持方的 strict JSON Schema。

    通用 schema 裁剪器故意只保留结构关键词；这里仅对 Reviewer 的已知形状补回
    ``maxItems`` / ``maxLength``。本地 Pydantic 仍是最终权威，Provider 不支持
    strict schema 时同样会在解析阶段 fail closed。
    """
    properties = schema.get("properties")
    if not isinstance(properties, dict):
        raise ValueError("Reviewer strict schema 缺少 properties")

    issues = properties.get("issues")
    suggestions = properties.get("suggestions")
    if not isinstance(issues, dict) or not isinstance(suggestions, dict):
        raise ValueError("Reviewer strict schema 缺少 issues/suggestions")
    issues["maxItems"] = REVIEWER_ISSUES_MAX_ITEMS
    suggestions["maxItems"] = REVIEWER_SUGGESTIONS_MAX_ITEMS

    issue_items = issues.get("items")
    suggestion_items = suggestions.get("items")
    if not isinstance(issue_items, dict) or not isinstance(suggestion_items, dict):
        raise ValueError("Reviewer strict schema 列表缺少 items")
    issue_properties = issue_items.get("properties")
    if not isinstance(issue_properties, dict):
        raise ValueError("Reviewer strict schema issue 缺少 properties")

    field_limits = {
        "paragraph_anchor": REVIEW_ISSUE_PARAGRAPH_ANCHOR_MAX_CHARS,
        "issue_type": REVIEW_ISSUE_TYPE_MAX_CHARS,
        "description": REVIEW_ISSUE_DESCRIPTION_MAX_CHARS,
        "suggestion": REVIEW_ISSUE_SUGGESTION_MAX_CHARS,
    }
    for field_name, max_chars in field_limits.items():
        field_schema = issue_properties.get(field_name)
        if not isinstance(field_schema, dict):
            raise ValueError(f"Reviewer strict schema issue 缺少 {field_name}")
        field_schema["maxLength"] = max_chars
    suggestion_items["maxLength"] = REVIEWER_SUGGESTION_MAX_CHARS


# P3-B-08:边界升级 Claude 的分数区间(对齐 P3-plan 决策 4 选 A)。
# 主审(Qwen)给出的 ``score_overall`` 落在 ``[ESCALATION_LOWER, ESCALATION_UPPER]``
# 闭区间,且 ``router`` 显式配置了 ``agent_routing.reviewer_escalation``,
# 才触发升级 Claude 复审。default disable:维护人无 API key 时不显式配置
# ``reviewer_escalation``,本路径自动跳过;真要启用须在 project.yaml 写明。
ESCALATION_LOWER_BOUND = 6.0
ESCALATION_UPPER_BOUND = 8.0


def review_chapter(
    *,
    chapter_card: ChapterCard,
    chapter_text: str,
    chapter_id: str,
    version_reviewed: int,
    router: ModelRouter | None = None,
    client: LLMClient | None = None,
    model: str | None = None,
    max_tokens: int = _DEFAULT_MAX_TOKENS,
    temperature: float = _DEFAULT_TEMPERATURE,
    prompts_dir: Path | None = None,
    db_session: Session | None = None,
    workspace_id: str | None = None,
    previous_attempt_errors: list[str] | None = None,
    current_fingerprint: dict[str, float] | None = None,
    prev_chapter_baseline_fingerprint: dict[str, float] | None = None,
    executable_constraint_plan: ExecutableConstraintPlan | None = None,
) -> ReviewResult:
    """审稿单章(P3-B-08 起含可选边界升 Claude 复审路径)。

    v1.0.x reviewer-resilience(2026-05-20):``previous_attempt_errors`` 由
    ``_review_with_retry`` 累积传入,retry 时把上一次 / 上几次解析失败的简短
    错误描述附到 user message 末尾,引导 LLM 自我修正格式问题(不重写算法,
    只让 LLM"知道上次错在哪")。``None`` / 空列表 → user message 与原路径一致。

    :param chapter_card: 章节卡;Reviewer 用其硬约束(必达 / 未解决 / 禁止 /
        视角 / 场景 / 字数区间)与阈值(``review_threshold`` / ``revise_threshold``)。
        **P7-B-05 起**:``workspace_id`` 给定时工作区级 ``ReviewerConfig`` 字段
        (``pass_threshold`` / ``revise_threshold`` / ``escalation_*_bound``)
        覆盖 ChapterCard 同语义字段 + 模块级常量。
    :param chapter_text: 待审正文;由 Orchestrator 从 ``ChapterVersion`` 文件
        读出后传入。
    :param chapter_id: 所审章节 ID,落入 ``ReviewResult.chapter_id``。
    :param version_reviewed: 本次审稿对应的版本号,落入 ``ReviewResult.version_reviewed``。
    :param router: P3-B-06 主路径。``ModelRouter`` 实例;给定时按
        ``router.route("reviewer")`` 取 client + model + 生成参数。
        **P3-B-08 起**:若 ``router.has_agent_routing("reviewer_escalation")``
        为真且首次审稿 ``score_overall ∈ [escalation_lower, escalation_upper]``,
        自动二次调用 ``router.route("reviewer_escalation")`` 复审,返复审结果
        (替换首次)。区间默认 ``[6.0, 8.0]``,P7-B-05 起可被工作区 reviewer 配置
        覆盖。
    :param client: legacy 路径 LLM 客户端;``router=None`` 时必传。
    :param model: legacy 路径模型名;``router=None`` 时必传。``reviewer_model``
        取响应里的 ``model_used``(应对 fallback 返回带日期后缀的完整 ID)。
    :param max_tokens: legacy 路径输出 token 上限,默认 2000;``router`` 路径
        下由 profile 决定。
    :param temperature: legacy 路径采样温度,默认 0.3;``router`` 路径下由
        profile 决定。
    :param prompts_dir: 提示词模板根目录;``None`` 时取 ``settings.prompts_dir``。
    :param db_session: P2-B-11 注入的 SQLAlchemy ``Session``,**仅读**用于
        查 ``card.related_characters`` 各角色的 ``voice_profile``。``None``
        时退化为 P1 路径(user message 不附 voice 段);非 ``None`` 时拼
        "## 出场角色声音档案(参考)"段附到 user message,供 LLM 对账打分。
    :param workspace_id: P7-B-05 加。给定时调
        ``ConfigService.load_reviewer_config(workspace_id)`` 取工作区级阈值
        (``pass_threshold`` / ``revise_threshold`` / ``escalation_lower_bound``
        / ``escalation_upper_bound``),覆盖 ChapterCard 单章字段 + 模块级
        ``ESCALATION_*_BOUND`` 常量;``None`` 时退化到 P3-P6 既有路径(ChapterCard
        字段 + 模块常量;ChapterService 内调用入口未透传 workspace_id 的 legacy
        / 测试场景)。配置加载失败 swallow + ChapterCard 字段兜底(对齐 P2-B-19
        / P3-B-07 "辅助任务失败 swallow"先例)。
    :return: ``ReviewResult``;``verdict`` 取值 ``pass`` / ``revise`` / ``rewrite``。
        ``escalate_to_human`` 由 Orchestrator(P1-B-13)按 count 耗尽决定,
        Reviewer 不直接产出。
    :raises ReviewerParseError: LLM 输出非 JSON、JSON 缺字段或字段类型错。
    :raises TypeError: ``router=None`` 且 ``client`` / ``model`` 任一缺失。
    """
    if executable_constraint_plan is not None:
        executable_constraint_plan.assert_matches_chapter_card(chapter_card)

    # P7-B-05:workspace 级 reviewer 配置一次解析(避免主审 + escalation 路径
    # 重复加载;失败 fall back 字面默认值)。
    pass_threshold, revise_threshold, escalation_lower, escalation_upper = (
        _resolve_reviewer_thresholds(
            chapter_card=chapter_card, workspace_id=workspace_id
        )
    )

    # v1.1 wave 4 s5 / I2.2:一次解析工作区写作风格模板(主审 + escalation 共用)。
    # None → reviewer 退化无模板路径(不注入模板期望段、不进 7 维加权)。
    style_template = _resolve_style_template(workspace_id)

    first_result = _perform_review(
        agent_name="reviewer",
        style_template=style_template,
        chapter_card=chapter_card,
        chapter_text=chapter_text,
        chapter_id=chapter_id,
        version_reviewed=version_reviewed,
        router=router,
        client=client,
        model=model,
        max_tokens=max_tokens,
        temperature=temperature,
        prompts_dir=prompts_dir,
        db_session=db_session,
        pass_threshold=pass_threshold,
        revise_threshold=revise_threshold,
        previous_attempt_errors=previous_attempt_errors,
        current_fingerprint=current_fingerprint,
        prev_chapter_baseline_fingerprint=prev_chapter_baseline_fingerprint,
        executable_constraint_plan=executable_constraint_plan,
    )

    # P3-B-08:边界升级 Claude 复审路径(决策 4 选 A,默认 disable)。
    # 三个条件全满足才触发:① router 在位(legacy 单测路径不触发);
    # ② 首审 score_overall 落在 ``[escalation_lower, escalation_upper]`` 闭区间;
    # ③ ``agent_routing.reviewer_escalation`` 显式配置(``has_agent_routing``
    # 严格判断"键存在性",未配置不会误把 ``default`` 当 escalation profile 路由
    # 进来)。
    # P7-B-05 起区间端点可由工作区 reviewer 配置覆盖。
    if (
        router is not None
        and escalation_lower <= first_result.score_overall <= escalation_upper
        and router.has_agent_routing("reviewer_escalation")
    ):
        _log.info(
            "reviewer_escalation_triggered",
            chapter_id=chapter_id,
            version_reviewed=version_reviewed,
            initial_score_overall=round(first_result.score_overall, 4),
            initial_verdict=first_result.verdict,
            initial_reviewer_model=first_result.reviewer_model,
        )
        return _perform_review(
            agent_name="reviewer_escalation",
            style_template=style_template,
            chapter_card=chapter_card,
            chapter_text=chapter_text,
            chapter_id=chapter_id,
            version_reviewed=version_reviewed,
            router=router,
            client=client,
            model=model,
            max_tokens=max_tokens,
            temperature=temperature,
            prompts_dir=prompts_dir,
            db_session=db_session,
            pass_threshold=pass_threshold,
            revise_threshold=revise_threshold,
            previous_attempt_errors=None,
            current_fingerprint=current_fingerprint,
            prev_chapter_baseline_fingerprint=prev_chapter_baseline_fingerprint,
            executable_constraint_plan=executable_constraint_plan,
        )

    return first_result


def _resolve_reviewer_thresholds(
    *, chapter_card: ChapterCard, workspace_id: str | None
) -> tuple[float, float, float, float]:
    """v1.0.x Commit D:解析 (pass / revise / escalation_lower / escalation_upper)
    4 阈值,**三级 fallback**:

    1. **工作区 project.yaml 显式 reviewer 段** → 用工作区配置(P7-B-05 工作区级
       统一覆盖,例如"轻小说" workspace 设全章 pass=6.5)
    2. **ChapterCard.review_threshold / revise_threshold 字段** → 用章节卡字段
       (bug ⑤ tension-aware 4 档单章细粒度,例如开篇低张力章 pass=6.0)
    3. **引擎默认 config.yaml reviewer 段** → 用引擎默认(fixture 没填章节卡字段
       时兜底,对齐既有 caller 语义)

    Escalation 上下界(``escalation_lower`` / ``escalation_upper``)只走"工作区
    显式 → 引擎默认",**不读 chapter_card 字段**(章节卡无 escalation 字段)。

    **v1.0.x Commit D 修法 A**(2026-05-20 维护人拍板):
    原 P7-B-05 实现总是用 ``ConfigService.load_reviewer_config`` 返"合并后默认",
    工作区无显式 reviewer 段时也吞掉 chapter_card 字段意图(bug ⑤ tension-aware
    在 luo2 这类无 project.yaml 工作区实际不生效)。新 ``load_reviewer_config_explicit``
    返 ``None`` 区分"工作区显式 vs 引擎默认",让 chapter_card 字段在工作区无显式
    覆盖时作为中层 fallback 生效。

    延迟 import ``ConfigService`` 规避 ``src.services.config_service`` ↔
    ``src.core.agents.reviewer`` 跨层倒挂(reviewer 是 core 层,config_service
    是 services 层)。
    """
    # 引擎默认(底层 fallback),与 escalation 模块常量一起作为最终兜底。
    engine_pass_threshold: float | None = None
    engine_revise_threshold: float | None = None
    escalation_lower = ESCALATION_LOWER_BOUND
    escalation_upper = ESCALATION_UPPER_BOUND

    # 优先级 1:工作区显式 ReviewerConfig(P7-B-05 工作区级统一覆盖)
    workspace_cfg = None
    if workspace_id is not None:
        try:
            from src.services.config_service import ConfigService

            workspace_cfg = ConfigService().load_reviewer_config_explicit(workspace_id)
        except Exception:  # noqa: BLE001 — swallow per "辅助任务失败兜底"先例
            workspace_cfg = None

    if workspace_cfg is not None:
        return (
            workspace_cfg.pass_threshold,
            workspace_cfg.revise_threshold,
            workspace_cfg.escalation_lower_bound,
            workspace_cfg.escalation_upper_bound,
        )

    # 优先级 2:ChapterCard 字段(bug ⑤ tension-aware 单章细粒度)
    # 也尝试拿引擎默认 escalation 上下界(工作区无显式 reviewer 段时);引擎读
    # 失败 swallow + 用模块级常量兜底。
    if workspace_id is not None:
        try:
            from src.services.config_service import ConfigService

            engine_cfg = ConfigService().load_reviewer_config(workspace_id)
            engine_pass_threshold = engine_cfg.pass_threshold
            engine_revise_threshold = engine_cfg.revise_threshold
            escalation_lower = engine_cfg.escalation_lower_bound
            escalation_upper = engine_cfg.escalation_upper_bound
        except Exception:  # noqa: BLE001
            pass

    pass_threshold = chapter_card.review_threshold
    revise_threshold = chapter_card.revise_threshold

    # 优先级 3:chapter_card 字段缺失 / 越界兜底走引擎默认 pass / revise
    # (实际中 ChapterCard schema 强制字段必填且有默认值 7.5/6.5,本分支主要给
    # 极端 fixture 兜底)。
    if pass_threshold is None and engine_pass_threshold is not None:
        pass_threshold = engine_pass_threshold
    if revise_threshold is None and engine_revise_threshold is not None:
        revise_threshold = engine_revise_threshold

    return pass_threshold, revise_threshold, escalation_lower, escalation_upper


def _resolve_style_template(workspace_id: str | None) -> str | None:
    """v1.1 wave 4 s5 / I2.2:解析工作区所选写作风格模板枚举值。

    读 ``meta.json`` 的 ``Workspace.style_template``(I1 落地);``None`` /
    工作区不存在 / 读失败 → 返 ``None``(reviewer 退化为无模板路径,不注入
    "## 风格模板期望"段,不进 7 维加权)。

    延迟 import ``read_meta`` / ``WorkspacePaths`` 规避跨层倒挂(对齐
    ``_resolve_reviewer_thresholds`` 延迟 import ``ConfigService`` 模式)。
    """
    if workspace_id is None:
        return None
    try:
        from src.core.workspace.meta import read_meta
        from src.core.workspace.workspace_paths import WorkspacePaths

        paths = WorkspacePaths(workspace_id)
        workspace = read_meta(paths.meta_json)
        return workspace.style_template
    except Exception:  # noqa: BLE001 — swallow per "辅助任务失败兜底"先例
        return None


def _format_style_template_section(style_template: str | None) -> str:
    """v1.1 wave 4 s5 / I2.2:渲染"## 风格模板期望"对照段(供模板符合度评分)。

    复用 I1 ``STYLE_TEMPLATES`` 10 维定性文案作"模板期望"对照源。``None`` /
    非法模板名 → 返空字符串(user message 不附该段,reviewer 退化无模板路径)。
    """
    if style_template is None:
        return ""
    from src.core.models.style_template import STYLE_TEMPLATES

    meta = STYLE_TEMPLATES.get(style_template)
    if meta is None:
        return ""
    d = meta.dimensions
    return (
        "## 风格模板期望(TEMPLATE STYLE EXPECTATION)\n\n"
        f"本工作区选定写作风格模板:**{meta.label}**({meta.description})\n\n"
        "请按以下 10 维模板期望评 `score_template_conformance`(模板符合度,"
        "0-10),重点看修辞密度 / 对白比例 / 题材调性 / 情感色调是否贴合:\n\n"
        f"- ① 题材调性:{d.genre_tone}\n"
        f"- ② 叙事密度:{d.narrative_density}\n"
        f"- ③ 修辞密度:{d.rhetoric_density}\n"
        f"- ④ 对白比例:{d.dialogue_ratio}\n"
        f"- ⑤ POV 切换:{d.pov_switching}\n"
        f"- ⑥ 金手指节奏:{d.power_pacing}\n"
        f"- ⑦ 世界观密度:{d.worldbuilding_density}\n"
        f"- ⑧ 情感色调:{d.emotional_tone}\n"
        f"- ⑨ 章节长度倾向:{d.chapter_length}\n"
        f"- ⑩ 事件密度倾向:{d.event_density}\n\n"
        "**评分纪律**:契合度高(修辞 / 对白 / 调性贴合模板)→ 8-10;部分偏离"
        "(如朴素模板写得过于华丽、对话主导模板写成大段描写)→ 5-7;严重背离"
        "模板调性 → ≤ 4。本维度**只评模板契合**,不重复评文笔好坏(那是 "
        "prose_quality)。"
    )


def _perform_review(
    *,
    agent_name: str,
    style_template: str | None = None,
    chapter_card: ChapterCard,
    chapter_text: str,
    chapter_id: str,
    version_reviewed: int,
    router: ModelRouter | None,
    client: LLMClient | None,
    model: str | None,
    max_tokens: int,
    temperature: float,
    prompts_dir: Path | None,
    db_session: Session | None,
    pass_threshold: float,
    revise_threshold: float,
    previous_attempt_errors: list[str] | None = None,
    current_fingerprint: dict[str, float] | None = None,
    prev_chapter_baseline_fingerprint: dict[str, float] | None = None,
    executable_constraint_plan: ExecutableConstraintPlan | None = None,
) -> ReviewResult:
    """单次完整审稿调用(P3-B-08 抽出复用)。

    与 P1-P2 ``review_chapter`` 主流程逻辑一一对应,只多一个 ``agent_name``
    参数:``"reviewer"``(主审,Qwen)/ ``"reviewer_escalation"``(边界升
    级,Claude)。除路由 agent 名不同,user / system message 拼装与 JSON
    解析与 verdict 计算完全一致——升级路径只是"换 profile 再审一次",
    不改 prompt / 不改契约。
    """
    resolved_client, resolved_model, complete_kwargs = resolve_routing(
        agent_name=agent_name,
        router=router,
        client=client,
        model=model,
        legacy_kwargs={"max_tokens": max_tokens, "temperature": temperature},
    )
    complete_kwargs = prepare_strict_json_output(
        client=resolved_client,
        model=resolved_model,
        output_model=_ReviewerLLMPayload,
        schema_name=agent_name,
        complete_kwargs=complete_kwargs,
        transform=_apply_reviewer_business_bounds,
    )

    template_root = (
        prompts_dir if prompts_dir is not None else settings.prompts_dir
    ).resolve()
    system_text = load_prompt_template(template_root / _REVIEWER_PROMPT_FILENAME)
    if chapter_card.word_count_policy_version:
        system_text = load_prompt_template(template_root / (
            "reviewer_narrative_v5.md" if chapter_card.narrative_context is not None and chapter_card.narrative_context.story_intent.version >= 6 else
            "reviewer_narrative_v4.md" if chapter_card.narrative_context is not None else "reviewer_length_v1.md"
        ))
    elif chapter_card.narrative_context is not None:
        system_text = load_prompt_template(template_root / "reviewer_narrative_v1.md")

    free_expression = (chapter_card.narrative_context is not None
                       and chapter_card.narrative_context.story_intent.version >= 6)
    # P2-B-11:db_session 非空时拼"## 出场角色声音档案(参考)"段到 user message
    # (位置:constraints 块之后、待审正文之前),供 LLM 对账 score_voice_consistency。
    voice_section = "" if free_expression else _build_voice_section(
        db_session=db_session,
        related_characters=(active_character_ids(chapter_card)
                            if chapter_card.narrative_context is not None else chapter_card.related_characters),
    )

    # v1.1 wave 4 s5 / I2.2:工作区选了模板时注入"## 风格模板期望"段供
    # score_template_conformance 评分;None / 非法 → 空字符串(无模板路径)。
    style_template_section = (
        "## 整体风格参照\n\n按用户原文中的 reading_preferences 整体评价 "
        "score_template_conformance；人物情绪表达和详略由具体阅读效果判断，"
        "不拆成十项固定写法、比例或字数要求。"
        if free_expression and style_template is not None else
        _format_style_template_section(style_template)
    )

    user_text = _build_user_message(
        chapter_card=chapter_card,
        chapter_text=chapter_text,
        chapter_id=chapter_id,
        voice_section=voice_section,
        style_template_section=style_template_section,
        previous_attempt_errors=previous_attempt_errors,
        current_fingerprint=current_fingerprint,
        prev_chapter_baseline_fingerprint=prev_chapter_baseline_fingerprint,
        executable_constraint_plan=executable_constraint_plan,
    )

    rejected_contract = None
    try:
        response = resolved_client.complete(
            messages=[
                Message(role="system", content=system_text),
                Message(role="user", content=user_text),
            ],
            model=resolved_model,
            **complete_kwargs,
        )
    except LocalJsonResponseViolation as exc:
        exc.contract.assert_unchanged()
        # Router 已归档原始响应。格式故障进入原有解析/有界反馈，
        # 不能被当作未知副作用；修正后的 wire 仍须满足同一严格合同。
        response, rejected_contract = exc.response, exc.contract
        if response.finish_reason == "length":
            raise ReviewerParseError(
                "Reviewer 响应被 Provider 截断，不能补符号当作完整审核",
                raw_output=response.content, model_used=response.model_used,
            ) from exc

    payload = _parse_llm_payload(
        raw_text=response.content,
        model_used=response.model_used,
        chapter_id=chapter_id,
        version_reviewed=version_reviewed,
        rejected_contract=rejected_contract,
    )

    # v1.1 wave 4 s5 / I2.2:仅当工作区选了模板 **且** LLM 给出
    # score_template_conformance 时进 7 维加权;否则 6 维(零回归)。
    template_dimension_active = _is_template_dimension_active(
        style_template=style_template, payload=payload
    )
    score_overall = _compute_score_overall(
        payload, template_dimension_active=template_dimension_active
    )
    # P7-B-05:阈值由 caller(``review_chapter``)解析后透传 — 优先级见
    # ``_resolve_reviewer_thresholds`` docstring。
    # P7-B-06:verdict 只看 score_overall(reviewer 不再判 hard_constraints);
    # hard_constraints 由 caller(``chapter_orchestrator``)综合
    # must_achieve_validator + validator_pipeline 后覆盖 ReviewResult。
    verdict = _decide_verdict(
        score_overall=score_overall,
        review_threshold=pass_threshold,
        revise_threshold=revise_threshold,
    )
    revise_targets = list(payload.issues) if verdict == "revise" else []

    # P1-B-24:OBSERVABILITY §2.4 `review_completed` 事件。P3-B-08 起加
    # ``agent_name`` 字段,便于诊断本次审稿是主审还是 escalation 复审。
    # P7-B-06:删 ``hard_constraints_passed`` 字段(reviewer 不再判此项),
    # caller 综合后的 hard_constraints_passed 在 orchestrator 自己的事件落。
    _log.info(
        "review_completed",
        chapter_id=chapter_id,
        version_reviewed=version_reviewed,
        score_overall=round(score_overall, 4),
        verdict=verdict,
        issues_count=len(payload.issues),
        reviewer_model=response.model_used,
        agent_name=agent_name,
    )

    # P7-B-06:hard_constraints_passed=True / failed_constraints=[] 默认填,
    # 由 ``chapter_orchestrator._apply_must_achieve_validator`` +
    # ``_apply_validator_pipeline`` 综合后覆盖。Reviewer 自己**不持**对
    # 硬约束的意见。
    return ReviewResult(
        review_id=f"rev-{uuid.uuid4().hex[:8]}",
        chapter_id=chapter_id,
        version_reviewed=version_reviewed,
        reviewed_at=datetime.now(UTC),
        reviewer_model=response.model_used,
        score_plot_progression=payload.score_plot_progression,
        score_tension_match=payload.score_tension_match,
        score_prose_quality=payload.score_prose_quality,
        score_consistency=payload.score_consistency,
        score_hook_management=payload.score_hook_management,
        score_voice_consistency=payload.score_voice_consistency,
        # v1.1 wave 4 s5 / I2.2:仅 7 维加权生效时落该分项;无模板 / LLM 漏产
        # → None(ReviewResult 该字段 optional,前端见 null = 未评模板符合度)。
        score_template_conformance=(
            payload.score_template_conformance if template_dimension_active else None
        ),
        # M2(v1.1 wave 5 s2 / G28):画面性分直接透传(任何题材都评,无 gate)。
        # **不进 score_overall**(上方 _compute_score_overall 不含本维)→ 永不影响
        # verdict;仅供唯一稿质量观察 + 慢调报表。
        score_imagery=payload.score_imagery,
        # M2(v1.1 wave 5 s3):内心戏分同 imagery 直接透传,**不进 score_overall**
        # → 永不影响 verdict;仅供唯一稿质量观察 + 慢调报表。
        score_interiority=payload.score_interiority,
        # M2(v1.1 wave 5 s9):套路化(越高越差)+ 沉浸感分同 imagery 直接透传,
        # **均不进 score_overall** → 永不影响 verdict;仅供唯一稿质量观察 +
        # 慢调报表(套路化配 M3 看是否在降)。
        score_formulaic=payload.score_formulaic,
        score_immersion=payload.score_immersion,
        score_overall=score_overall,
        hard_constraints_passed=True,
        failed_constraints=[],
        issues=list(payload.issues),
        suggestions=list(payload.suggestions),
        verdict=verdict,
        revise_targets=revise_targets,
    )


def _build_user_message(
    *,
    chapter_card: ChapterCard,
    chapter_text: str,
    chapter_id: str,
    voice_section: str = "",
    style_template_section: str = "",
    previous_attempt_errors: list[str] | None = None,
    current_fingerprint: dict[str, float] | None = None,
    prev_chapter_baseline_fingerprint: dict[str, float] | None = None,
    executable_constraint_plan: ExecutableConstraintPlan | None = None,
) -> str:
    """user 消息:章节卡硬约束块 + (P2-B-11)voice 对账段 + 待审正文。

    Reviewer 不需要 ``ContinuityContext``——P1 简版只审"本章是否符合章节卡硬
    约束";voice_consistency 凭本章语境 + (P2-B-11)voice_profile 对账数据
    判断,跨章历史档案对照留给 P3+。

    P2-B-11 voice 段位置约定:**constraints 块之后、待审正文之前**——LLM
    顺序读"硬约束 → 角色声音对账数据 → 正文",符合校验流程。空字符串
    (``db_session=None`` 或全部角色未入库)时省略此段,user message 与
    P1 路径字面一致。

    ``chapter_id`` 不渲染进 user message 主体(保留 P1-B-11 原契约不动),
    只通过 P1-B-22 引入的 marker 透传给 Mock LLM client。
    """
    constraints_block = (
        "## 章节卡硬约束(用于审稿对照)\n\n"
        f"- 视角角色(pov_character):{chapter_card.pov_character}\n"
        f"- 场景设置(setting):{chapter_card.setting}\n"
        f"- 张力等级(tension_level):{chapter_card.tension_level}/10\n"
        + ("- 篇幅不参与评分、issue、拒稿或修订；不能按字数推定重复或剧情缺失。\n\n"
         if chapter_card.word_count_policy_version else
         f"- 字数区间:[{chapter_card.word_count_min}, {chapter_card.word_count_max}]\n\n") +
        "### 必达事件(must_achieve)\n"
        f"{_render_bullets(chapter_card.must_achieve)}\n\n"
        "### 必须保持未解决(must_unresolved)\n"
        f"{_render_bullets(chapter_card.must_unresolved)}\n\n"
        "### 禁止元素(forbidden)\n"
        f"{_render_bullets(chapter_card.forbidden)}"
    )
    text_block = "## 待审正文\n\n" + chapter_text

    user_request_block = ""
    if chapter_card.narrative_context is not None:
        request = chapter_card.narrative_context.story_intent.user_request
        if request is not None:
            from src.core.models.narrative import UserNarrativeRequest
            UserNarrativeRequest.model_validate(request.model_dump(mode="json"))
            user_request_block = (
                "## 用户原始前提与阅读要求（文学方向，不逐条要求证据）\n\n"
                + request.premise
                + ("\n\n整体阅读方向（原文）：\n" + request.reading_preferences
                   if request.version == 2 and request.reading_preferences is not None else "")
            )
    sections = [
        user_request_block,
        constraints_block,
        _format_reviewer_hard_gate_contract(
            current_fingerprint=current_fingerprint,
            executable_constraint_plan=executable_constraint_plan,
        ),
    ]
    if voice_section.strip():
        sections.append(voice_section)
    # v1.1 wave 4 s5 / I2.2:风格模板期望段(放 voice 段后、fingerprint 段前),
    # 仅工作区选了 style_template 时非空 → 供 score_template_conformance 评分。
    if style_template_section.strip():
        sections.append(style_template_section)
    # v1.1 audit G22:fingerprint 客观参考段(放 voice 段后、正文前),仅在
    # 当前章 + 前一章 baseline fingerprint 都就位时附(首章 / draft 路径 /
    # wiring off / 异常路径 → 空字符串跳过)。reviewer prompt v5 "风格指纹
    # 客观参考" 段会消费此段引导 LLM 客观信号兜底打分。
    fingerprint_section = _format_fingerprint_section(
        current_fingerprint=current_fingerprint,
        prev_chapter_baseline_fingerprint=prev_chapter_baseline_fingerprint,
        narrative_policy=chapter_card.narrative_context is not None,
    )
    if fingerprint_section:
        sections.append(fingerprint_section)
    formulaic_section = render_contrast_reframe_review_section(chapter_text)
    if formulaic_section:
        sections.append(formulaic_section)
    sections.append(text_block)

    # v1.0.x reviewer-resilience:retry 时附"上次输出错误反馈"段,引导 LLM
    # 自我修正格式问题(JSON 漏逗号 / 字段重复 / schema 不符等)。
    if previous_attempt_errors:
        feedback_lines = [
            "## 上次输出格式错误反馈(请严格按 JSON schema 输出)",
            "",
            "你之前对本章的审稿输出存在以下解析失败,请这次严格输出合法 JSON:",
            "",
        ]
        for idx, err in enumerate(previous_attempt_errors, start=1):
            feedback_lines.append(f"{idx}. {err}")
        feedback_lines.append("")
        feedback_lines.append(
            "请确保:① 输出**只有一份 JSON**,不要任何前导/尾随文字或代码块包裹;"
            "② 6 个 score 字段 + issues + suggestions 全部出现;"
            "③ 每条 issue 包含 paragraph_anchor + issue_type + description + suggestion 4 字段无重复;"
            "④ JSON 语法严格(逗号 / 引号 / 大括号正确闭合)。"
        )
        sections.append("\n".join(feedback_lines))

    # P1-B-22:追加 agent 身份 marker(HTML 注释,真 LLM 视为字面忽略;
    # chapter_id 通过 marker 透传给 Mock LLM client 决定 verdict 路径)。
    # 极小概率 trailing 复读到 JSON 输出由 ``_strip_code_fence`` 兜。
    return append_role_marker(
        "\n\n".join(sections),
        role="reviewer",
        chapter_id=chapter_id,
    )


def _format_fingerprint_section(
    *,
    current_fingerprint: dict[str, float] | None,
    prev_chapter_baseline_fingerprint: dict[str, float] | None,
    narrative_policy: bool = False,
) -> str:
    """v1.1 audit G22 治本(2026-05-27 b1-rerun-deep-audit):渲染当前版本 +
    前一章 baseline fingerprint 客观参考段。

    返空字符串 → user message 不附该段(首章 / draft 路径 / wiring off / 数据缺失);
    两者都就位时附段供 reviewer prompt v5 "风格指纹客观参考" 段消费(LLM 看到
    跨章段均只作 WARN/评分参考；真正 paragraph collapse HARD 由紧邻的共享
    合同 + Orchestrator 确定性后处理消费）。

    **为何**:luo-clean-v1 ch-5 rerun-4 现场 reviewer 给 mode collapsed 稿
    (paragraph_mean=13.5)主观高分 8.375+,根因 = reviewer 不读 fingerprint。
    本段让 reviewer 信号与 [HARD] validator 信号对齐,杜绝"reviewer 高分 +
    validator 截获 + rewrite 循环耗 budget"悖论。
    """
    if current_fingerprint is None or prev_chapter_baseline_fingerprint is None:
        return ""

    c_para = current_fingerprint.get("paragraph_length_mean", 0.0)
    c_sent = current_fingerprint.get("sentence_length_mean", 0.0)
    c_simile = current_fingerprint.get("simile_density", 0.0)
    p_para = prev_chapter_baseline_fingerprint.get("paragraph_length_mean", 0.0)
    p_sent = prev_chapter_baseline_fingerprint.get("sentence_length_mean", 0.0)
    p_simile = prev_chapter_baseline_fingerprint.get("simile_density", 0.0)

    if narrative_policy:
        return (
            "## 叙述节奏观察（SOFT）\n\n"
            f"当前段均 {c_para:.0f} 字、句均 {c_sent:.0f} 字；"
            f"前章段均 {p_para:.0f} 字、句均 {p_sent:.0f} 字。\n"
            "数值差异本身不构成缺陷或扣分理由。按正文判断节奏是否适合此章；"
            "短对白、背景交代、直接心理均可正常使用。不要求维持固定比例。"
        )

    return (
        "## 风格指纹客观参考(STYLE FINGERPRINT REFERENCE)\n\n"
        f"- 当前版本(vN):paragraph_mean={c_para:.0f} 字 / "
        f"sentence_mean={c_sent:.0f} 字 / simile_density={c_simile:.3f} 次/句\n"
        f"- 前一章 baseline:paragraph_mean={p_para:.0f} 字 / "
        f"sentence_mean={p_sent:.0f} 字 / simile_density={p_simile:.3f} 次/句\n\n"
        '请按 reviewer prompt 中 "风格指纹客观参考" 段判定纪律调整评分:\n'
        f"- `{PARAGRAPH_MEAN_HARD_CONSTRAINT_ID}` 的精确 HARD 判定只取上方共享合同；"
        "Reviewer 只同步降分并记 pacing issue，不输出 verdict\n"
        "- 若 |当前 paragraph_mean - 前章 paragraph_mean| / 前章 paragraph_mean > 0.50 → "
        "仅记跨章 WARN；只有正文实际节奏受损时才按主观质量扣分，不设强制分数上限\n"
        "- 若当前 paragraph_mean 位于共享硬底线到 60 字之间 → 作为偏短 SOFT 信号，"
        "可结合正文实际节奏主观扣分\n"
        "- 60-110 字是默认自然参考带；高于 110 也只作 SOFT/WARN 观察，不单独打回"
    )


def _format_reviewer_hard_gate_contract(
    *,
    current_fingerprint: dict[str, float] | None,
    executable_constraint_plan: ExecutableConstraintPlan | None = None,
) -> str:
    """给 Reviewer 显示与 Writer / G22b 同源的硬闸，不让其另造 verdict 尺。"""
    actual = (
        current_fingerprint.get("paragraph_length_mean", 0.0)
        if current_fingerprint is not None
        else 0.0
    )
    actual_line = (
        f"；本稿实测 paragraph_length_mean={actual:.1f}"
        if actual > 0.0
        else "；本稿没有可用 paragraph fingerprint，勿猜测"
    )
    shared_plan = ""
    if executable_constraint_plan is not None:
        shared_plan = render_executable_constraint_plan(
            executable_constraint_plan
        ).replace(
            "## 可执行 HARD 约束计划(EXECUTABLE CONSTRAINT PLAN)",
            "## 审稿约束级别合同(REVIEWER/VALIDATOR SSOT)",
            1,
        )
    else:
        shared_plan = "## 审稿约束级别合同(REVIEWER/VALIDATOR SSOT)"
    if executable_constraint_plan is not None and executable_constraint_plan.narrative_policy_version == 1:
        return shared_plan + (
            "\n\n只按正文评价人物、因果与叙述效果。正常背景交代、直接心理、短对白均允许。"
            "段均、句均、心理比例与句式不是 HARD，不能把观察指标变成拒稿条件。"
            "Reviewer 只产评分和 issues；明确剧情承诺与事实由共享合同核对。"
        )
    return (
        f"{shared_plan}\n\n"
        "- Reviewer 只产评分与 issues，不产硬约束 verdict；真正 HARD 由共享"
        " validator / Orchestrator 按上方同一 plan_hash 确定性合并；Reviewer"
        "不得新增计划外 HARD。\n"
        f"- `{PARAGRAPH_MEAN_HARD_CONSTRAINT_ID}` [HARD]:唯一通过条件是 "
        f"`{paragraph_mean_hard_rule()}`{actual_line}。命中时同步把 tension/prose "
        "降至 ≤5.0 并加 pacing issue，但不要输出 verdict。\n"
        "- 跨章 paragraph_mean 偏差、默认自然带、句均、比喻密度与 style trend "
        "均是 WARN/SOFT；不得因数值偏差本身强制 rewrite，也不得改写 HARD 底线。"
    )


def _build_voice_section(
    *,
    db_session: Session | None,
    related_characters: list[str],
) -> str:
    """构造 user message 的"## 出场角色声音档案(参考)"段(P2-B-11)。

    与 Writer P2-B-10 ``_build_voice_section`` 在数据获取与字段渲染上一致,
    差异在于:
    - **段标题措辞**:Reviewer 用"(参考)"尾缀,体现"对账数据"语义,而
      Writer 用"## 出场角色声音档案"(规则数据)。
    - **位置**:Reviewer 拼到 user message(评分输入),Writer 拼到 system
      message(写作硬约束)。

    退化条件(任一命中则返回空字符串,user message 与 P1 路径字面一致):
    1. ``db_session is None`` —— P1 路径或调用方未注入。
    2. ``related_characters`` 为空 —— 章节卡未登记出场角色。
    3. ``related_characters`` 非空但全部 ID 在 SQLite 查不到 —— 静态资产
       导入未跑 / 抽取尚未触发等(P2 阶段允许此态)。

    voice_profile 6 字段全展开:``catchphrases`` / ``forbidden_phrases``
    / ``tone`` / ``sentence_style`` / ``self_address`` / ``addresses_others``
    ;空集合字段渲染成"无",空 dict 渲染成"无"。

    Rule of Three 留观:本任务是 voice 段第二次出现(Writer 是第一次)。
    P2-B-12 衔接桥升级 / 后续 Reviser 扩展若再次需要 voice 段,即触发抽到
    ``src/core/agents/_prompt.py`` 的条件;本任务先复制 + 调整,理由是段
    标题与位置语义不同,共享前提需要再积累一轮使用证据。
    """
    if db_session is None or not related_characters:
        return ""

    voice_blocks: list[str] = []
    for character_id in related_characters:
        character: Character | None = get_character(db_session, character_id)
        if character is None:
            continue
        voice_blocks.append(_format_voice_profile(character))

    if not voice_blocks:
        return ""

    return "## 出场角色声音档案(参考)\n\n" + "\n\n".join(voice_blocks)


def _format_voice_profile(character: Character) -> str:
    """单个角色的 voice_profile 渲染块(P2-B-11)。

    与 Writer P2-B-10 ``_format_voice_profile`` 字面一致(6 字段渲染规则
    相同);独立维护以让 Reviewer 单测不依赖 writer 内部模块,Rule of Three
    第三次再考虑抽公共。
    """
    voice = character.voice_profile
    addresses_block = (
        ", ".join(f"{k}→{v}" for k, v in voice.addresses_others.items())
        if voice.addresses_others
        else "无"
    )
    catchphrases_block = ", ".join(voice.catchphrases) if voice.catchphrases else "无"
    forbidden_block = (
        ", ".join(voice.forbidden_phrases) if voice.forbidden_phrases else "无"
    )
    return (
        f"### {character.name}({character.character_id})\n"
        f"- tone(语气):{voice.tone}\n"
        f"- sentence_style(句式):{voice.sentence_style}\n"
        f"- self_address(自称):{voice.self_address}\n"
        f"- addresses_others(对他人称呼):{addresses_block}\n"
        f"- catchphrases(口头禅):{catchphrases_block}\n"
        f"- forbidden_phrases(禁止用语):{forbidden_block}"
    )


def _render_bullets(items: list[str]) -> str:
    """字符串列表 → markdown 项目符号;空列表渲染成"无"。"""
    if not items:
        return "无"
    return "\n".join(f"- {item}" for item in items)


def _parse_llm_payload(
    *,
    raw_text: str,
    model_used: str,
    chapter_id: str,
    version_reviewed: int,
    rejected_contract: LocalJsonContract | None = None,
) -> _ReviewerLLMPayload:
    """剥代码块 → ``json.loads``(失败时 ``json_repair`` 兜底)→ Pydantic 校验。

    任一失败抛 ``ReviewerParseError`` 并落 ``reviewer_parse_failed`` 事件。

    v1.0.x reviewer-resilience(2026-05-20):本地 Qwen 自由输出 JSON 偶发常见
    格式错误(漏 ``,`` / 字段重复 / 末尾多 ``,`` / 字符串未转义引号 / 大括号
    未闭合等),原 ``json.loads`` 严格解析必挂。引入 ``json_repair.loads`` 作
    兜底层:``json.loads`` 失败 → 尝试 ``json_repair.loads`` 修正常见错误 →
    成功用其结果走 Pydantic 校验;若 repair 后仍非 JSON 才抛 ``ReviewerParseError``。
    Pydantic 阶段同理 — repair 成功但字段缺失/重复(后者 repair 会保留最后值,
    可能仍触发 schema_mismatch)仍按既有路径报错升人工。
    """
    text = _strip_code_fence(raw_text).strip()

    repaired = False
    try:
        data = json.loads(text)
    except json.JSONDecodeError as exc:
        json_error = str(exc)
        # v1.0.x:json_repair 兜底。修复成功则继续走 Pydantic 校验;
        # 修复后仍非 dict / 仍非合法 JSON 才抛 ReviewerParseError。
        try:
            data = json_repair.loads(text)
            if not isinstance(data, dict):
                raise ValueError(
                    f"json_repair returned non-dict: {type(data).__name__}"
                )
            repaired = True
            _log.warning(
                "reviewer_parse_repaired",
                raw_output=raw_text,
                model_used=model_used,
                chapter_id=chapter_id,
                version_reviewed=version_reviewed,
                original_json_error=json_error,
            )
        except Exception as repair_exc:
            _log.error(
                "reviewer_parse_failed",
                reason="invalid_json",
                raw_output=raw_text,
                model_used=model_used,
                chapter_id=chapter_id,
                version_reviewed=version_reviewed,
                json_error=json_error,
                repair_error=str(repair_exc),
            )
            raise ReviewerParseError(
                f"Reviewer LLM 输出非合法 JSON(json_repair 兜底亦失败):{json_error}",
                raw_output=raw_text,
                model_used=model_used,
            ) from exc

    try:
        if rejected_contract is not None:
            rejected_contract.validate_data(data)
        return _ReviewerLLMPayload.model_validate(data)
    except ValueError as exc:
        _log.error(
            "reviewer_parse_failed",
            reason="schema_mismatch",
            raw_output=raw_text,
            model_used=model_used,
            chapter_id=chapter_id,
            version_reviewed=version_reviewed,
            validation_errors=(exc.errors() if isinstance(exc, ValidationError) else [str(exc)]),
            json_repaired=repaired,
        )
        error_summary = (
            "; ".join(
                f"{'.'.join(str(p) for p in e['loc'])}: {e['msg']}"
                for e in exc.errors()[:3]
            ) if isinstance(exc, ValidationError) else str(exc)
        )
        raise ReviewerParseError(
            f"Reviewer LLM 输出 JSON 结构与 schema 不符:{error_summary}",
            raw_output=raw_text,
            model_used=model_used,
        ) from exc


def _strip_code_fence(text: str) -> str:
    """剥掉 trailing HTML 注释 + ``` / ```json 包裹(P1-B-22)。

    LLM 输出格式杂质两类:
    1. **Trailing HTML 注释**(P1-B-22 引入):真 LLM 极小概率把 user
       message 末尾的 ``<!-- agent_role: reviewer -->`` marker 复读到 JSON
       输出尾部。
    2. 代码块包裹(``` 或 ```json):即使 prompt 禁止 LLM 偶发包。

    顺序:**先剥 trailing**(可能多个),**再剥 fence**——若先剥 fence,
    fence 正则要求整字符串末尾为 ``` ,trailing 注释会让 fence 不匹配。

    无杂质时原样返回。
    """
    # 先反复剥 trailing 注释(防止多个 trailing,虽然正常不会发生)。
    while True:
        stripped = _TRAILING_HTML_COMMENT_RE.sub("", text)
        if stripped == text:
            break
        text = stripped
    # 再剥 fence(此时末尾是 ``` ,fence 正则可正常匹配)。
    match = _CODE_FENCE_RE.match(text)
    if match is not None:
        return match.group("body")
    return text


def _is_template_dimension_active(
    *, style_template: object | None, payload: _ReviewerLLMPayload
) -> bool:
    """v1.1 wave 4 s5 / I2.2:本次审稿是否启用"模板符合度"7 维加权。

    两条件**同时**满足才启用(否则退化到 6 维 = I2.2 之前行为,零回归):
    1. 工作区选了风格模板(``style_template`` 非 ``None``)→ reviewer 注入了
       "## 风格模板期望"段,LLM 有对照源;
    2. LLM 实际产出了 ``score_template_conformance``(非 ``None``)→ 真 LLM
       偶发漏产时 graceful 退化,不让综合分被 0 拖低。
    """
    return style_template is not None and payload.score_template_conformance is not None


def _compute_score_overall(
    payload: _ReviewerLLMPayload, *, template_dimension_active: bool = False
) -> float:
    """按 PROJECT_SPEC §4.4 公式算综合分(权重总和 1.0,结果天然落在 [0, 10])。

    v1.1 wave 4 s5 / I2.2:``template_dimension_active=True`` 时用 7 维权重
    (含 ``score_template_conformance`` 0.10);否则用 6 维权重(与 I2.2 之前
    逐字节一致)。
    """
    weights = (
        _SCORE_WEIGHTS_WITH_TEMPLATE if template_dimension_active else _SCORE_WEIGHTS
    )
    return sum(getattr(payload, name) * weight for name, weight in weights.items())


def _decide_verdict(
    *,
    score_overall: float,
    review_threshold: float,
    revise_threshold: float,
) -> Literal["pass", "revise", "rewrite"]:
    """PROJECT_SPEC §4.4 判决规则链(P1 Reviewer 不产 ``escalate_to_human``)。

    **P7-B-06 起删 ``hard_constraints_passed`` 参数**:Reviewer 不再判定硬约束,
    硬约束综合(``must_achieve_validator`` + ``validator_pipeline``)在
    ``chapter_orchestrator`` 内做。本函数只看 ``score_overall``:
    - < revise_threshold → rewrite
    - [revise_threshold, review_threshold) → revise
    - >= review_threshold → pass
    """
    if score_overall < revise_threshold:
        return "rewrite"
    if score_overall < review_threshold:
        return "revise"
    return "pass"
