"""must_achieve 独立 LLM 判定 agent(P7-B-06 引入,v1.1 wave 2 session 6 G9.3 改批处理)。

职责:把 ``ChapterCard.must_achieve`` 必达事件判定从 Reviewer agent 解耦,
**单次 LLM 调用一次性给出 N 条 must_achieve 的 yes/no/uncertain 判定**
(v1 是逐条 N 次串行调用,G9.3 改为批处理 1 次),综合后给出
``ValidationResult{passed, missing_items, llm_calls_count}``。

设计要点
========

1. **批处理单次 LLM 调用**(v1.1 wave 2 session 6 G9.3 改造,2026-05-29):
   旧 v1 版本是 N 条 must_achieve 独立 LLM 调用(N 次串行 yes/no);新 v2
   版本改单次 LLM 调用一次性返回 N 条 list 判定。**LLM-as-judge 设计意图护栏**
   保持:profile 仍 ``cloud_qwen_max_thinking``(thinking-on),thinking budget
   在批处理下覆盖所有 N 条;verdict 一致性由实证硬护栏验证(``docs/impl/
   v1_1_w2_s6_g9_3_acceptance/compare_report.md`` 100% 一致)。

   保留 v1 历史:``prompts/archive/must_achieve_validator_v1.md`` + 旧
   ``_ValidatorLLMPayload`` schema 已删除(无 caller)。

2. **沿用 cloud_qwen_max_thinking profile**(v1.1 wave 1 切换,wave 2 临时会话
   立 LLM-as-judge hard guard):路由 agent_name ``"must_achieve_validator"`` →
   ``agent_routing.must_achieve_validator`` → ``cloud_qwen_max_thinking``
   (dashscope qwen3.8-max,thinking-on)。LLM-as-judge hard guard
   (``router._assert_llm_judge_thinking_on``)保证 profile 切换不脱轨。

3. **空 must_achieve 跳过**(P7-B-06 决策 3=A):章节卡 ``must_achieve=[]`` 时
   且没有实验核心状态正向证据要求时，直接返 ``passed=True`` +
   ``llm_calls_count=0``。v6 存在已冻结核心迁移时仍执行同一个现有审核。

4. **权威 HARD fail-closed / legacy fail-open**:正文生产携带
   ``ExecutableConstraintPlan`` 时，setup、传输或有界解析重试耗尽抛
   ``MustAchieveTechnicalError``，由流水线技术停止，绝不把“未判定”写成 PASS；
   仅冻结旧活动/legacy caller 没有权威计划时保留历史 fail-open 兼容。

5. **retry-with-feedback**(G9.3 新增):批处理 LLM 输出 schema 错位(results
   长度 ≠ N / item_index 重复 / 缺漏 / 超界 / verdict 非合法值)→ **retry 1 次**,
   prompt 加 "上次输出问题:..." 字面让 LLM 自纠;retry 仍失败 → 权威路径技术停止，
   legacy 路径 fail-open。
   不引入无界 retry(避免单章卡死)。

6. **uncertain 视作 missing**(自决项):LLM 输出 ``verdict=uncertain`` 时计入
   ``missing_items``,推 caller 走 rewrite 路径。

7. **agent_role marker = ``must_achieve_validator``(无 chapter_id):
   user message 末尾追加 ``<!-- agent_role: must_achieve_validator -->`` marker
   供 MockLLMClient 识别。**marker 不携带 chapter_id** — 批处理不分 chapter 级
   状态机分流,mock 路径返合法 list JSON 即可让既有 happy 路径不回归。

v1.1 wave 2 session 6 G9.3 实证留观项
=====================================

- 历史 batch 实测平均时延:dashscope qwen3.7-max thinking-on 单次 ~10-15s(对比 v1
  逐条 ~10s/次,N=3 时 ~30s → batch ~15s,节省 ~50%)
- batch 模式 input tokens:单次 ~3500(prompt + chapter_text + N 条 must_achieve)
  对比 v1 逐条 ~2700 × N = ~8100(N=3),节省 ~57%
- batch 模式 output tokens:单次 ~1500-2000(N 条 evidence)对比 v1 逐条 ~500 × N
  = ~1500(N=3),持平 / 略增
- retry-with-feedback 触发率:实证 5 章 0 次触发(0/5);若未来撞 +2 处实证撞
  上,考虑加 "结构化输出 mode"(dashscope 不支持 strict JSON schema mode,
  prompt 自检三步骤目前足够)
"""

from __future__ import annotations

import json
import re
from dataclasses import dataclass, field
from pathlib import Path
from typing import Literal

import structlog
from pydantic import BaseModel, ConfigDict, Field, PrivateAttr, ValidationError

from src.config import settings
from src.core.agents._json_contract import loads_with_quote_repair
from src.core.agents._prompt import (
    append_role_marker,
    load_prompt_template,
    resolve_routing,
)
from src.core.agents._structured_output import prepare_strict_json_output
from src.core.agents.creative_delta import uses_experimental_pipeline
from src.core.llm.base import LLMClient, Message
from src.core.llm.local_json_contract import LocalJsonResponseViolation
from src.core.models.review import ConstraintResult, stable_constraint_id
from src.core.routing.router import ModelRouter
from src.core.rules.prose_hard_contract import ExecutableConstraint

_log = structlog.get_logger(__name__)

# One initial schema/business attempt plus one same-model feedback retry.
MUST_ACHIEVE_SCHEMA_MAX_ATTEMPTS = 2

# R9 v5 最小误放行修复:只收紧 yes evidence 为正文连续逐字引文；payload 不变。
_VALIDATOR_PROMPT_FILENAME = "must_achieve_validator_v4.md"
_FROZEN_VALIDATOR_PROMPT_FILENAMES = (
    "must_achieve_validator_v3.md",
    "must_achieve_validator_v2.md",
    "must_achieve_validator_v1.md",
)

# 默认生成参数:批处理 N 条 yes/no 询问对采样稳定性敏感,温度低且 token 上限
# 比 v1 大(单次输出 N 条 evidence)。router 路径下由 profile 决定(本生产路径
# cloud_qwen_max_thinking:temp 0.3 / num_predict 4096 / thinking-on);
# legacy client+model 路径保留本默认值(单测 + 历史 caller 兜底)。
_DEFAULT_TEMPERATURE = 0.3
# 单次输出 N 条 {"item_index", "verdict", "evidence"} JSON list;N=3 时 ~1500 token,
# 留 4×安全余量给极端情况(N=10 + 长 evidence)
_DEFAULT_MAX_TOKENS = 2000

# 匹配 ```json ... ``` 或 ``` ... ``` 包裹;沿用 v1 同型剥离。
_CODE_FENCE_RE = re.compile(
    r"^\s*```(?:json)?\s*\n(?P<body>.*?)\n```\s*$",
    re.DOTALL,
)

# trailing HTML 注释剥离(沿用 v1 同型;本 agent 不发 marker 但 LLM 极端
# 情况可能复读 user 末尾的标点 / 残留杂质)。
_TRAILING_HTML_COMMENT_RE = re.compile(
    r"\s*<!--.*?-->\s*$",
    re.DOTALL,
)


@dataclass(frozen=True)
class CoverageEvidence:
    """本次已验真的分项引文；仅进程内传递，不改变 Provider/API/DB schema。"""

    requirement_part: str
    evidence: str


@dataclass(frozen=True)
class ValidationResult:
    """validator 输出(签名与 v1 保持兼容,caller 不变)。

    - ``passed``:**所有** must_achieve 条目 LLM 判定为 ``yes`` 时 True;任一
      ``no`` / ``uncertain`` 即 False。空 must_achieve / LLM fail → True
      (fail-open / skip 语义)。
    - ``missing_items``:被判 ``no`` / ``uncertain`` 的 must_achieve 字面列表
      (按入参 must_achieve 顺序);供 ``failed_constraints`` 字段展示。
    - ``llm_calls_count``:实际发起的 LLM 调用次数。空 must_achieve → 0;
      批处理成功 → 1;retry 触发 → 2;LLM 第 1 次抛 → 1;fail-open → 介于中间。
    - ``skipped``:为 True 当且仅当 must_achieve=[] 走"跳过"分支(passed=True
      + llm_calls_count=0)。区分"真没条目"与"全过"。
    - ``fail_open``:为 True 当且仅当 LLM 调用抛异常或 schema 错位 retry 后仍
      失败导致整体 fail-open(passed=True + llm_calls_count > 0)。
    """

    passed: bool
    missing_items: list[str] = field(default_factory=list)
    constraint_results: list[ConstraintResult] = field(default_factory=list)
    llm_calls_count: int = 0
    skipped: bool = False
    fail_open: bool = False
    coverage_evidence: dict[str, tuple[CoverageEvidence, ...]] = field(default_factory=dict)


@dataclass(frozen=True)
class RequiredCoreStateEvidence:
    constraint: ExecutableConstraint
    requirements: tuple[str, ...]
    context: str


class _ValidatorItemPayload(BaseModel):
    """批处理输出 list 中单条 item 的 schema(G9.3 引入)。"""

    item_index: int = Field(..., ge=1)  # 1-based,从 prompt 字面对齐
    verdict: Literal["yes", "no", "uncertain"]
    evidence: str = Field(..., min_length=1, max_length=200)
    _coverage_evidence: tuple[CoverageEvidence, ...] = PrivateAttr(default=())


class _ValidatorBatchPayload(BaseModel):
    """批处理输出顶层 schema(G9.3 引入)。

    严格要求(caller 在 ``_parse_and_validate_batch`` 二次校验):
    - ``results`` 长度 = 入参 must_achieve 长度 N
    - ``item_index`` 在 ``[1, 2, ..., N]`` 内一一对应(无重复、无缺漏、无超界)
    """

    results: list[_ValidatorItemPayload]


class _CoverageCheckPayload(BaseModel):
    model_config = ConfigDict(extra="forbid")

    requirement_part: str = Field(..., min_length=1)
    verdict: Literal["yes", "no", "uncertain"]
    evidence: str = Field(..., min_length=1, max_length=200)


class _CoverageItemPayload(BaseModel):
    model_config = ConfigDict(extra="forbid")

    item_index: int = Field(..., ge=1)
    checks: list[_CoverageCheckPayload] = Field(..., min_length=1, max_length=16)


class _CoverageBatchPayload(BaseModel):
    model_config = ConfigDict(extra="forbid")

    results: list[_CoverageItemPayload]


def _parse_coverage_batch(
    *, raw_text: str, model_used: str, must_achieve: list[str], chapter_text: str,
) -> list[_ValidatorItemPayload]:
    """Private v6 wire: account for the complete requirement before aggregation.

    Only a complete yes with exact quotes can pass. A malformed yes quote is a
    technical contract error handled by the existing bounded schema retry; it
    cannot become a fabricated missing event or a content regression.
    """
    data = loads_with_quote_repair(
        _strip_code_fence(raw_text).strip(),
        agent_name="must_achieve_validator", model_used=model_used,
    )
    batch = _CoverageBatchPayload.model_validate(data)
    if sorted(item.item_index for item in batch.results) != list(range(1, len(must_achieve) + 1)):
        raise ValueError("coverage results 必须精确覆盖每个 item_index 一次")
    result: list[_ValidatorItemPayload] = []
    for item in batch.results:
        requirement = must_achieve[item.item_index - 1]
        if "".join(check.requirement_part for check in item.checks) != requirement:
            raise ValueError(f"item_index={item.item_index}: requirement_part 必须顺序完整覆盖原要求，不得遗漏或改写")
        failed = next((check for check in item.checks if check.verdict != "yes"), None)
        invalid = next((check for check in item.checks if check.verdict == "yes" and check.evidence not in chapter_text), None)
        if failed is not None:
            verdict = failed.verdict
            evidence = f"未完成分项[{failed.requirement_part}]: {failed.evidence}"[:200]
        elif invalid is not None:
            raise ValueError(
                f"item_index={item.item_index}: 分项[{invalid.requirement_part}]的 yes "
                f"引文不在正文中: {invalid.evidence!r}；须复制连续逐字原文，"
                "不得加字、改字或拼接；不能证实则返回 no/uncertain"
            )
        else:
            verdict = "yes"
            evidence = item.checks[-1].evidence
        parsed = _ValidatorItemPayload(item_index=item.item_index, verdict=verdict, evidence=evidence)
        if verdict == "yes":
            parsed._coverage_evidence = tuple(
                CoverageEvidence(check.requirement_part, check.evidence)
                for check in item.checks
            )
        result.append(parsed)
    return result


class MustAchieveTechnicalError(RuntimeError):
    """权威 must Judge setup/传输/解析失败且不能安全降级。"""

    def __init__(self, *, stage: str, attempts: int, cause: Exception) -> None:
        self.stage = stage
        self.attempts = attempts
        self.cause_type = type(cause).__name__
        super().__init__(
            "权威必达事件校验技术失败:"
            f"stage={stage},attempts={attempts},cause={self.cause_type}"
        )


def validate_must_achieve(
    *,
    chapter_text: str,
    must_achieve: list[str],
    chapter_id: str,
    workspace_id: str | None = None,
    router: ModelRouter | None = None,
    client: LLMClient | None = None,
    model: str | None = None,
    max_tokens: int = _DEFAULT_MAX_TOKENS,
    temperature: float = _DEFAULT_TEMPERATURE,
    prompts_dir: Path | None = None,
    authoritative: bool = False,
    required_core_state: RequiredCoreStateEvidence | None = None,
) -> ValidationResult:
    """批处理 LLM 一次性判定 N 条 must_achieve,综合后返结果(G9.3 改造)。

    入参 / 返参签名与 v1 保持完全一致,caller(``chapter_orchestrator``)无需改动。

    :param chapter_text: 待校验的章节正文(已审稿的版本)。
    :param must_achieve: 章节卡必达列表；与核心状态证据要求都为空时跳过。
    :param chapter_id: 章节 ID,落入 structlog 事件 ``chapter_id`` 字段。
    :param workspace_id: 工作区 ID,落 structlog 字段(P3-B-14 透传纪律,可选)。
    :param router: ``ModelRouter`` 实例;给定时按 ``router.route("must_achieve_validator")``
        取 client + model + 生成参数。``None`` 时走 legacy 路径(``client`` + ``model``
        必传)。
    :param client: legacy 路径 LLM 客户端;``router=None`` 时必传。
    :param model: legacy 路径模型名;``router=None`` 时必传。
    :param max_tokens: legacy 路径 token 上限(``router`` 路径由 profile 决定)。
    :param temperature: legacy 路径温度(``router`` 路径由 profile 决定)。
    :param prompts_dir: 提示词模板根目录;``None`` 时取 ``settings.prompts_dir``。
    :return: ``ValidationResult``。
    """
    # 决策 3:空 must_achieve → 跳过 + passed=True(对齐 v1)
    if not must_achieve and required_core_state is None:
        _log.debug(
            "must_achieve_validator_skipped",
            chapter_id=chapter_id,
            workspace_id=workspace_id,
            reason="empty_must_achieve",
        )
        return ValidationResult(
            passed=True, missing_items=[], llm_calls_count=0, skipped=True
        )

    # 决策 4 扩:setup 阶段失败(prompt 文件缺失 / 路由错配)也走 fail-open。
    try:
        resolved_client, resolved_model, complete_kwargs = resolve_routing(
            agent_name="must_achieve_validator",
            router=router,
            client=client,
            model=model,
            legacy_kwargs={"max_tokens": max_tokens, "temperature": temperature},
        )
        template_root = (
            prompts_dir if prompts_dir is not None else settings.prompts_dir
        ).resolve()
        coverage_mode = uses_experimental_pipeline(resolved_client)
        prompt_path = template_root / ("must_achieve_validator_v6.md" if coverage_mode else _VALIDATOR_PROMPT_FILENAME)
        # 显式 prompts_dir 可能是已冻结历史活动；仅这种快照按新到旧读取，
        # 默认生产 prompt 根缺 v3 时仍 setup fail-open 留痕，不静默降尺。
        if not coverage_mode and prompts_dir is not None and not prompt_path.is_file():
            for frozen_filename in _FROZEN_VALIDATOR_PROMPT_FILENAMES:
                frozen_path = template_root / frozen_filename
                if frozen_path.is_file():
                    prompt_path = frozen_path
                    break
        system_text = load_prompt_template(prompt_path)
        complete_kwargs = prepare_strict_json_output(
            client=resolved_client,
            model=resolved_model,
            output_model=_CoverageBatchPayload if coverage_mode else _ValidatorBatchPayload,
            schema_name="must_achieve",
            complete_kwargs=complete_kwargs,
        )
    except Exception as exc:  # noqa: BLE001 — 权威/legacy 在此分流
        if authoritative:
            _log.error(
                "must_achieve_validator_authoritative_setup_failed",
                chapter_id=chapter_id,
                workspace_id=workspace_id,
                error_type=type(exc).__name__,
                attempt=0,
            )
            raise MustAchieveTechnicalError(
                stage="setup", attempts=0, cause=exc
            ) from exc
        _log.warning(
            "must_achieve_validator_setup_failed_open",
            chapter_id=chapter_id,
            workspace_id=workspace_id,
            error_type=type(exc).__name__,
            error_message=str(exc),
        )
        return ValidationResult(
            passed=True,
            missing_items=[],
            llm_calls_count=0,
            skipped=False,
            fail_open=True,
        )

    # Reuse this same call for the positive evidence part of the already-frozen
    # core-state HARD. Legacy callers keep their original wire and behavior.
    original_count = len(must_achieve)
    core_evidence = required_core_state if coverage_mode else None
    judged_items = list(must_achieve)
    if core_evidence is not None:
        judged_items.extend(core_evidence.requirements)
    if not judged_items:
        return ValidationResult(passed=True, skipped=True)
    result = _run_batch_with_retry(
        chapter_text=chapter_text,
        must_achieve=judged_items,
        chapter_id=chapter_id,
        workspace_id=workspace_id,
        client=resolved_client,
        model=resolved_model,
        complete_kwargs=complete_kwargs,
        system_text=system_text,
        authoritative=authoritative,
        coverage_mode=coverage_mode,
        evidence_context=core_evidence.context if core_evidence else "",
    )
    if core_evidence is None or not core_evidence.requirements or result.fail_open:
        return result
    original_results = result.constraint_results[:original_count]
    core_results = result.constraint_results[original_count:]
    failed_core = [item for item in core_results if item.status == "fail"]
    contract = core_evidence.constraint
    core_result = contract.as_constraint_result(
        status="fail" if failed_core else "pass",
        details="；".join(item.details for item in (failed_core or core_results)),
    )
    return ValidationResult(
        passed=result.passed,
        missing_items=[item.rule_text for item in original_results if item.status == "fail"],
        constraint_results=[*original_results, core_result],
        llm_calls_count=result.llm_calls_count, skipped=result.skipped,
        fail_open=result.fail_open,
        coverage_evidence={
            item.constraint_id: result.coverage_evidence[item.constraint_id]
            for item in original_results if item.constraint_id in result.coverage_evidence
        },
    )



def _run_batch_with_retry(
    *,
    chapter_text: str,
    must_achieve: list[str],
    chapter_id: str,
    workspace_id: str | None,
    client: LLMClient,
    model: str,
    complete_kwargs: dict,
    system_text: str,
    authoritative: bool,
    coverage_mode: bool = False,
    evidence_context: str = "",
) -> ValidationResult:
    """批处理主路径:单次 LLM 调用 + 1 次 retry-with-feedback(G9.3 新增)。

    第 1 次失败有两类:
    - LLM 调用本身抛异常(网络 / 5xx / etc)→ 直接 fail-open,不 retry(retry
      重试一次大概率也失败 + 浪费一次 thinking budget)
    - LLM 输出 schema 错位(results 长度 / item_index 错位 / verdict 非法)
      → retry 1 次,prompt 加 "上次输出问题:..." 字面让 LLM 自纠

    第 2 次仍失败 → 权威路径技术停止，legacy 路径 fail-open。
    """
    llm_calls_count = 0
    last_error_feedback: str | None = None

    for attempt in range(MUST_ACHIEVE_SCHEMA_MAX_ATTEMPTS):
        # 第 0 次主调,第 1 次 same-model feedback retry。
        user_text = _build_user_message(
            chapter_text=chapter_text,
            must_achieve=must_achieve,
            retry_feedback=last_error_feedback,
        )
        if evidence_context:
            user_text += "\n\n## 关系对象身份与已冻结的写前合同\n" + evidence_context
        wire_error: LocalJsonResponseViolation | None = None
        try:
            response = client.complete(
                messages=[
                    Message(role="system", content=system_text),
                    Message(role="user", content=user_text),
                ],
                model=model,
                **complete_kwargs,
            )
            llm_calls_count += 1
        except LocalJsonResponseViolation as exc:
            # A completed but invalid answer consumes the existing schema attempt,
            # not the transport-failure path; never bypass its wire validation.
            response = exc.response
            wire_error = exc
            llm_calls_count += 1
        except Exception as exc:  # noqa: BLE001 — 权威停止/legacy fail-open
            if authoritative:
                raise MustAchieveTechnicalError(
                    stage="transport", attempts=llm_calls_count + 1, cause=exc
                ) from exc
            _log.warning(
                "must_achieve_validator_failed_open",
                chapter_id=chapter_id,
                workspace_id=workspace_id,
                error_type=type(exc).__name__,
                error_message=str(exc),
                attempt=attempt,
                items_total=len(must_achieve),
            )
            return ValidationResult(
                passed=True,
                missing_items=[],
                llm_calls_count=llm_calls_count,
                skipped=False,
                fail_open=True,
            )

        # 解析 + 二次校验(item_index 严格性)
        try:
            if wire_error is not None:
                raise ValueError(str(wire_error)) from wire_error
            payloads = (
                _parse_coverage_batch(
                    raw_text=response.content, model_used=response.model_used,
                    must_achieve=must_achieve, chapter_text=chapter_text,
                ) if coverage_mode else _parse_and_validate_batch(
                    raw_text=response.content,
                    model_used=response.model_used,
                    expected_count=len(must_achieve),
                )
            )
            # 校验通过 → 综合
            return _aggregate_results(
                payloads=payloads,
                must_achieve=must_achieve,
                chapter_id=chapter_id,
                workspace_id=workspace_id,
                llm_calls_count=llm_calls_count,
            )
        except ValueError as exc:
            # schema 错位 / item_index 错位 → retry 1 次(若已 retry 则 fail-open)
            if attempt == 0:
                last_error_feedback = str(exc)
                _log.info(
                    "must_achieve_validator_retry_triggered",
                    chapter_id=chapter_id,
                    workspace_id=workspace_id,
                    error_message=str(exc)[:200],
                    items_total=len(must_achieve),
                )
                continue
            # attempt == 1 (retry 后仍失败)→ 权威停止 / legacy fail-open
            if authoritative:
                raise MustAchieveTechnicalError(
                    stage="parse_or_schema", attempts=llm_calls_count, cause=exc
                ) from exc
            _log.warning(
                "must_achieve_validator_failed_open",
                chapter_id=chapter_id,
                workspace_id=workspace_id,
                error_type="SchemaValidationError",
                error_message=str(exc)[:200],
                attempt=attempt,
                items_total=len(must_achieve),
            )
            return ValidationResult(
                passed=True,
                missing_items=[],
                llm_calls_count=llm_calls_count,
                skipped=False,
                fail_open=True,
            )

    # 理论上不可达(for loop 内每个分支都 return);兜底
    return ValidationResult(
        passed=True,
        missing_items=[],
        llm_calls_count=llm_calls_count,
        skipped=False,
        fail_open=True,
    )


def _aggregate_results(
    *,
    payloads: list[_ValidatorItemPayload],
    must_achieve: list[str],
    chapter_id: str,
    workspace_id: str | None,
    llm_calls_count: int,
) -> ValidationResult:
    """从 LLM batch 输出综合 ValidationResult。

    payloads 已通过 _parse_and_validate_batch 严格校验(长度 = N,item_index
    在 1..N 一一对应)。按 item_index 1-based 排序,跟 must_achieve list 0-based
    对齐(item_index=K 对应 must_achieve[K-1])。
    """
    # 按 item_index 排序(防 LLM 乱序输出但 index 正确)
    sorted_payloads = sorted(payloads, key=lambda p: p.item_index)
    missing_items: list[str] = []
    constraint_results: list[ConstraintResult] = []
    coverage_evidence: dict[str, tuple[CoverageEvidence, ...]] = {}
    source_path = f"chapter_cards/{chapter_id}.must_achieve"

    for payload in sorted_payloads:
        item = must_achieve[payload.item_index - 1]  # 1-based → 0-based
        status: Literal["pass", "fail"] = "pass" if payload.verdict == "yes" else "fail"
        constraint_results.append(
            ConstraintResult(
                constraint_id=stable_constraint_id(
                    source_path=source_path,
                    domain="must_achieve",
                    rule_text=item,
                ),
                domain="must_achieve",
                source_path=source_path,
                rule_text=item,
                status=status,
                details=payload.evidence,
            )
        )
        if payload._coverage_evidence:
            coverage_evidence[constraint_results[-1].constraint_id] = payload._coverage_evidence
        if payload.verdict != "yes":
            missing_items.append(item)
            _log.info(
                "must_achieve_validator_item_missing",
                chapter_id=chapter_id,
                workspace_id=workspace_id,
                item=item,
                item_index=payload.item_index,
                verdict=payload.verdict,
                evidence=payload.evidence,
            )
        else:
            _log.debug(
                "must_achieve_validator_item_passed",
                chapter_id=chapter_id,
                workspace_id=workspace_id,
                item=item,
                item_index=payload.item_index,
                evidence=payload.evidence,
            )

    passed = not missing_items
    _log.info(
        "must_achieve_validator_completed",
        chapter_id=chapter_id,
        workspace_id=workspace_id,
        passed=passed,
        missing_count=len(missing_items),
        items_total=len(must_achieve),
        llm_calls_count=llm_calls_count,
    )
    return ValidationResult(
        passed=passed,
        missing_items=missing_items,
        constraint_results=constraint_results,
        llm_calls_count=llm_calls_count,
        skipped=False,
        fail_open=False,
        coverage_evidence=coverage_evidence,
    )


def _build_user_message(
    *,
    chapter_text: str,
    must_achieve: list[str],
    retry_feedback: str | None = None,
) -> str:
    """拼装 user message:必达事件列表(编号 1..N)+ 章节正文 + (可选)retry feedback + agent_role marker。

    G9.3 改批处理:user message 含全部 must_achieve 列表(不再每次只塞 1 条)。
    retry 时 prepend "上次输出问题"字面让 LLM 自纠。
    """
    items_section = "\n".join(
        f"{idx + 1}. {item}" for idx, item in enumerate(must_achieve)
    )

    body_parts = []
    if retry_feedback is not None:
        body_parts.append(
            f"## ⚠️ 上次输出问题(请自纠后重新输出)\n\n{retry_feedback}\n\n"
            "请严格按 system prompt 的 strict JSON 格式输出,**自检三步骤**后再提交。\n"
        )
    body_parts.append(f"## 必达事件列表\n\n{items_section}\n")
    body_parts.append(f"## 章节正文\n\n{chapter_text}")
    body = "\n".join(body_parts)
    return append_role_marker(body, role="must_achieve_validator")


def _parse_and_validate_batch(
    *, raw_text: str, model_used: str, expected_count: int
) -> list[_ValidatorItemPayload]:
    """剥代码块 → json.loads → Pydantic 校验 → item_index 严格性二次校验(G9.3)。

    严格性二次校验:
    - results 长度 = expected_count
    - item_index 集合 = {1, 2, ..., expected_count}(无重复、无缺漏、无超界)

    失败抛 ``ValueError``,caller 在 ``_run_batch_with_retry`` 决定 retry / fail-open。
    """
    text = _strip_code_fence(raw_text).strip()
    try:
        data = loads_with_quote_repair(
            text,
            agent_name="must_achieve_validator",
            model_used=model_used,
        )
    except json.JSONDecodeError as exc:
        raise ValueError(
            f"LLM 输出非合法 JSON(model={model_used}): {exc.msg};raw={raw_text[:200]!r}"
        ) from exc

    try:
        batch = _ValidatorBatchPayload.model_validate(data)
    except ValidationError as exc:
        raise ValueError(
            f"LLM 输出 schema 不符顶层 results 列表(model={model_used}): "
            f"{exc.errors()[:3]}"
        ) from exc

    # item_index 严格性二次校验
    if len(batch.results) != expected_count:
        raise ValueError(
            f"LLM 输出 results 长度 {len(batch.results)} ≠ 期望 {expected_count} "
            f"(model={model_used})"
        )
    indices = [item.item_index for item in batch.results]
    expected_indices = set(range(1, expected_count + 1))
    actual_indices = set(indices)
    if actual_indices != expected_indices:
        missing = expected_indices - actual_indices
        extra = actual_indices - expected_indices
        duplicates = [i for i in indices if indices.count(i) > 1]
        raise ValueError(
            f"LLM 输出 item_index 错位(model={model_used}): "
            f"missing={sorted(missing)} extra={sorted(extra)} duplicates={sorted(set(duplicates))}"
        )

    return batch.results


def _strip_code_fence(text: str) -> str:
    """剥 trailing HTML 注释 + ``` / ```json 包裹(沿用 v1 同型清理)。"""
    while True:
        stripped = _TRAILING_HTML_COMMENT_RE.sub("", text)
        if stripped == text:
            break
        text = stripped
    match = _CODE_FENCE_RE.match(text)
    if match is not None:
        return match.group("body")
    return text


__all__ = [
    "MustAchieveTechnicalError",
    "ValidationResult",
    "validate_must_achieve",
]
