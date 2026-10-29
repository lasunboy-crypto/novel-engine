"""beat_alignment LLM-as-judge validator(v1.1 wave 1 session 1 引入,G3 治本核心)。

职责:把 mini_plan `one_line_event` 是否对齐 `beat.summary` + 推进 `must_advance`
+ 不触发 `forbidden_advance` 的判定从 outline_builder 解耦。普通管线仍为
**1 章 1 次综合 LLM 调用**；creative_delta_v6 实验管线同卷批量核对，
仅明确失败的章节再走本文件的单章复核。

接口签名:::

    validate_beat_alignment(
        *, chapter_event, beat,
        chapter_id, workspace_id=None,
        router=None, client=None, model=None,
        ...
    ) -> BeatAlignmentValidationResult

调用方(outline_builder.py `_check_single_volume` post_validate callback):每章查找
归属 beat → 调本 agent → 失败累计 → 抛 ValueError → retry-with-feedback 自动 retry 3 次。

设计要点
========

1. **1 章 1 次综合 LLM 调用**(v1.1 wave 1 session 1 自决项,plan-kickoff 已拍板 G3
   选 B beat 路径但未细化 validator 调用粒度):
   - **1st 质量收益**:LLM 拿到 chapter_event 全字符 + beat 全字段一次性看完,
     综合判断比逐条独立更全面(避免"逐条都过但综合 event 完全跑题"的情况)
   - **3rd 工程便利**:LLM 调用数从"每条 must / forbidden 各 1 次"(可能 3-7 次/章)
     降到 1 次/章,云端账单减 3-7 倍

2. **沿用 local_qwen_reviewer profile**(对齐 must_achieve_validator 决策 2=A):
   路由 agent_name ``"beat_alignment_validator"`` → ``agent_routing.beat_alignment_validator``
   → config.yaml 字面 ``local_qwen_reviewer``(temp 0.3,context 16000,
   num_predict 2048,think:false)。本会话首发用既有路由 fallback 走 reviewer
   profile,生产环境需要在 config.yaml 加 ``agent_routing.beat_alignment_validator``
   显式 routing(留待后续 H 类多 provider 任务扩展)。

3. **空 beat 字段跳过**(对齐 must_achieve_validator 决策 3=A):若 beat 的
   ``must_advance`` 和 ``forbidden_advance`` 同时为空,跳过 LLM 调用直接 ``passed=True``。
   常见情景:outline 旧数据无 beats 字段(向后兼容)/ LLM 输出 beat 时漏填了
   两个字段。

4. **LLM 失败 fail-open**(对齐 must_achieve_validator 决策 4=A):LLM 调用抛
   异常或返非法 JSON → 整个 validator ``passed=True`` + log warn。避免 LLM
   瞬态故障导致 mini_plan 全卷返工。

5. **agent_role marker = ``beat_alignment_validator``(无 chapter_id):
   user message 末尾追加 ``<!-- agent_role: beat_alignment_validator -->`` marker
   供 MockLLMClient 识别。**marker 不携带 chapter_id** — 与 must_achieve_validator
   同型,mock 路径始终返 pass JSON 让既有 mini_plan happy 路径不回归;单测路径
   走专属 fixture 验真。

6. **失败语义**(retry-with-feedback 友好):验证失败时抛 ValueError,error msg
   含 ``forbidden_advance_violated`` + ``must_advance_missing`` 字面 list,LLM
   retry 时能从 error msg 直接读到"哪些 forbidden 触发了 / 哪些 must 没推进",
   再生成 mini_plan 时主动避坑。
"""

from __future__ import annotations

from src.core.rules.narrative_handoff import mini_plan_review_design

import json
import re
from dataclasses import dataclass, field
from pathlib import Path
from typing import TYPE_CHECKING, Any, Literal

import structlog
from pydantic import BaseModel, ConfigDict, Field, ValidationError

from src.config import settings
from src.core.agents.structured_handoff import HandoffError, handoff_user_text
from src.core.agents._json_contract import loads_with_quote_repair
from src.core.agents._prompt import (
    append_role_marker,
    load_prompt_template,
    resolve_routing,
)
from src.core.agents._structured_output import prepare_strict_json_output
from src.core.llm.base import LLMClient, Message
from src.core.llm.cancel import LLMCancelledError
from src.core.routing.router import ModelRouter

if TYPE_CHECKING:
    from src.core.models.outline import MiniPlanItem, VolumeBeatPlan

_log = structlog.get_logger(__name__)

_VALIDATOR_PROMPT_FILENAME = "beat_alignment_validator_v1.md"
_BATCH_PROMPT_FILENAME = "beat_alignment_batch_validator_v2.md"
_MAX_BATCH_CHAPTERS = 12

# 默认生成参数:综合判定对采样稳定性敏感,温度低且 token 上限适中(JSON 含 4 个
# list[str] + bool + str,约 200-500 token)。router 路径下由 profile 决定。
_DEFAULT_TEMPERATURE = 0.3
_DEFAULT_MAX_TOKENS = 600

# 匹配 ```json ... ``` 或 ``` ... ``` 包裹;沿用 must_achieve_validator 同型剥离。
_CODE_FENCE_RE = re.compile(
    r"^\s*```(?:json)?\s*\n(?P<body>.*?)\n```\s*$",
    re.DOTALL,
)

# trailing HTML 注释剥离(沿用 must_achieve_validator 同型)。
_TRAILING_HTML_COMMENT_RE = re.compile(
    r"\s*<!--.*?-->\s*$",
    re.DOTALL,
)


@dataclass(frozen=True)
class BeatAlignmentValidationResult:
    """validator 输出。

    - ``passed``:LLM ``verdict=pass`` 时 True;``verdict=fail`` 时 False。空 beat
      字段(must_advance + forbidden_advance 都空)/ LLM fail → True(skip /
      fail-open 语义)。
    - ``must_advance_advanced``:LLM 判定本章推进的 must_advance 条目字面(原文复制)。
    - ``must_advance_missing``:LLM 判定本章未推进的 must_advance 条目字面;
      verdict=pass 时允许部分 missing(beat 跨多章,本章只推进部分是正常的)。
    - ``forbidden_advance_violated``:LLM 判定本章触发的 forbidden_advance 条目字面;
      verdict=fail 的主要原因应该在这里。
    - ``summary_in_range``:LLM 判定 chapter_event 是否落在 beat.summary 范围内。
    - ``evidence``:LLM 给出的简短判定依据(中文)。
    - ``info_increment_substantive``(v1.1 wave 6 session 2 / A2 扩维,**WARN-only**):
      LLM 判定本章 ``info_increment`` 是否构成实质新增信息(新冲突 / 揭示 / 动机 /
      关系变化;战斗章是否有差异化战术)。**不影响 ``passed``**——信息密度是文学判断,
      可能误伤合理慢节奏章(§0 第一原则宁可漏报不误伤留白)。默认 ``True``
      (fail-safe:LLM / mock 未返回该维度时不误报 warn)。
    - ``info_increment_issue``:``info_increment_substantive=False`` 时 LLM 给的简短
      问题说明(为何判为非实质 / 复读 / 空泛);substantive 时为空串。
    - ``llm_calls_count``:实际发起的 LLM 调用次数;空 beat 字段跳过 → 0;正常 → 1;
      fail-open(LLM 抛异常)→ 1。
    - ``skipped``:为 True 当且仅当 must_advance + forbidden_advance 都空走"跳过"分支
      (passed=True + llm_calls_count=0)。
    - ``fail_open``:为 True 当且仅当 LLM 调用抛异常导致整体 fail-open
      (passed=True + llm_calls_count=1)。
    """

    passed: bool
    must_advance_advanced: list[str] = field(default_factory=list)
    must_advance_missing: list[str] = field(default_factory=list)
    forbidden_advance_violated: list[str] = field(default_factory=list)
    summary_in_range: bool = True
    evidence: str = ""
    info_increment_substantive: bool = True
    info_increment_issue: str = ""
    llm_calls_count: int = 0
    skipped: bool = False
    fail_open: bool = False
    uncertain: bool = False


class _ValidatorLLMPayload(BaseModel):
    """LLM 输出的中间模型。

    v1.1 wave 6 session 2 / A2 扩 ``info_increment_substantive`` 维度(WARN-only)。
    两个 info_increment 字段给默认值(``substantive=True`` / ``issue=""``):旧 mock /
    旧 LLM 输出不含这两字段时不报 schema 错、不误产 warn(fail-safe 护 §0)。
    """

    verdict: Literal["pass", "fail"]
    must_advance_advanced: list[str] = Field(default_factory=list)
    must_advance_missing: list[str] = Field(default_factory=list)
    forbidden_advance_violated: list[str] = Field(default_factory=list)
    summary_in_range: bool
    evidence: str = Field(..., min_length=1, max_length=300)
    info_increment_substantive: bool = True
    info_increment_issue: str = Field(default="", max_length=300)


class BeatAlignmentBatchTechnicalError(RuntimeError):
    """整卷核对未完成；不得把缺失的章节视为通过或重生小纲。"""


class _BatchItemLLMPayload(BaseModel):
    model_config = ConfigDict(extra="forbid")

    chapter_id: str
    beat_id: str
    verdict: Literal["pass", "fail", "uncertain"]
    must_advance_advanced: list[str] = Field(default_factory=list)
    must_advance_missing: list[str] = Field(default_factory=list)
    forbidden_advance_violated: list[str] = Field(default_factory=list)
    summary_in_range: bool
    evidence: str = Field(..., min_length=1, max_length=300)
    chapter_evidence_quote: str = Field(..., min_length=4)
    beat_evidence_quote: str = Field(..., min_length=4)
    info_increment_substantive: bool = True
    info_increment_issue: str = Field(default="", max_length=300)


class _BatchLLMPayload(BaseModel):
    model_config = ConfigDict(extra="forbid")

    results: list[_BatchItemLLMPayload]


def validate_beat_alignment_batch(
    *,
    entries: list[tuple[MiniPlanItem, VolumeBeatPlan]],
    character_context: list[dict[str, str]] | None = None,
    router: ModelRouter | None = None,
    client: LLMClient | None = None,
    model: str | None = None,
    max_tokens: int = 8192,
    temperature: float = _DEFAULT_TEMPERATURE,
    prompts_dir: Path | None = None,
) -> dict[str, BeatAlignmentValidationResult]:
    """十二章以内一次核对；更长的卷按十二章分组，逐组完整核验。"""
    if not entries:
        return {}
    expected = [(item.chapter_id, beat.beat_id) for item, beat in entries]
    if len({chapter_id for chapter_id, _ in expected}) != len(expected):
        raise BeatAlignmentBatchTechnicalError("整卷节拍核对含重复 chapter_id")
    results: dict[str, BeatAlignmentValidationResult] = {}
    for start in range(0, len(entries), _MAX_BATCH_CHAPTERS):
        group = entries[start:start + _MAX_BATCH_CHAPTERS]
        group_results = _validate_beat_alignment_group(
            entries=group, character_context=character_context,
            router=router, client=client, model=model,
            max_tokens=max_tokens, temperature=temperature,
            prompts_dir=prompts_dir,
        )
        results.update(group_results)
    if list(results) != [chapter_id for chapter_id, _ in expected]:
        raise BeatAlignmentBatchTechnicalError("整卷节拍核对没有完整覆盖各章")
    return results


def _validate_beat_alignment_group(
    *,
    entries: list[tuple[MiniPlanItem, VolumeBeatPlan]],
    character_context: list[dict[str, str]] | None,
    router: ModelRouter | None,
    client: LLMClient | None,
    model: str | None,
    max_tokens: int,
    temperature: float,
    prompts_dir: Path | None,
) -> dict[str, BeatAlignmentValidationResult]:
    expected = [(item.chapter_id, beat.beat_id) for item, beat in entries]

    try:
        resolved_client, resolved_model, complete_kwargs = resolve_routing(
            agent_name="beat_alignment_validator",
            router=router,
            client=client,
            model=model,
            legacy_kwargs={"max_tokens": max_tokens, "temperature": temperature},
        )
        template_root = (
            prompts_dir if prompts_dir is not None else settings.prompts_dir
        ).resolve()
        system_text = load_prompt_template(template_root / _BATCH_PROMPT_FILENAME)
        complete_kwargs = prepare_strict_json_output(
            client=resolved_client,
            model=resolved_model,
            output_model=_BatchLLMPayload,
            schema_name="beat_alignment_batch",
            complete_kwargs=complete_kwargs,
        )
    except HandoffError:
        raise
    except Exception as exc:  # noqa: BLE001 — 配置错误不允许伪装成审核通过
        raise BeatAlignmentBatchTechnicalError(
            f"整卷节拍核对无法启动：{type(exc).__name__}: {exc}"
        ) from exc
    tasks = [
        {
            "chapter_id": item.chapter_id,
            "chapter_number": item.chapter_number,
            "arc_id": item.arc_id,
            "beat_id": beat.beat_id,
            "one_line_event": item.one_line_event,
            "info_increment": item.info_increment,
            "chapter_design": mini_plan_review_design(item),
            "beat": beat.model_dump(mode="json", include={
                "summary", "must_advance", "forbidden_advance", "dramatic_contract",
            }),
        }
        for item, beat in entries
    ]
    original_task = (
        "## 整卷节拍核对 [beat_alignment_batch]\n"
        "按 chapter_id 逐章对照，不得合并章节或漏掉任何一章。\n"
        + json.dumps({"characters": character_context or [], "tasks": tasks}, ensure_ascii=False)
        + "\n"
    )
    user_text = handoff_user_text(
        resolved_client,
        stage="beat_alignment_validator",
        data={"characters": character_context or [], "tasks": tasks},
        original_task=append_role_marker(
            original_task, role="beat_alignment_validator"
        ),
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
        raw = loads_with_quote_repair(
            _strip_code_fence(response.content).strip(),
            agent_name="beat_alignment_validator",
            model_used=response.model_used,
        )
        payload = _BatchLLMPayload.model_validate(raw)
    except (HandoffError, LLMCancelledError):
        raise
    except Exception as exc:  # noqa: BLE001 — Provider/格式/额度故障均不得放行
        raise BeatAlignmentBatchTechnicalError(
            f"整卷节拍核对未完成：{type(exc).__name__}: {exc}"
        ) from exc

    actual = [(row.chapter_id, row.beat_id) for row in payload.results]
    if actual != expected:
        raise BeatAlignmentBatchTechnicalError(
            f"整卷节拍核对章节不齐或顺序错误；预期 {expected}，实际 {actual}"
        )
    results: dict[str, BeatAlignmentValidationResult] = {}
    for (item, beat), row in zip(entries, payload.results, strict=True):
        chapter_source = json.dumps(mini_plan_review_design(item), ensure_ascii=False)
        beat_source = json.dumps(beat.model_dump(mode="json", include={
            "summary", "must_advance", "forbidden_advance", "dramatic_contract",
        }), ensure_ascii=False)
        if (row.chapter_evidence_quote not in chapter_source
                or row.beat_evidence_quote not in beat_source):
            raise BeatAlignmentBatchTechnicalError(
                f"{item.chapter_id} 的依据不能在本章设计和归属节拍原文中定位"
            )
        if (
            not set(row.must_advance_advanced).issubset(beat.must_advance)
            or not set(row.must_advance_missing).issubset(beat.must_advance)
            or not set(row.forbidden_advance_violated).issubset(beat.forbidden_advance)
            or (row.verdict == "pass" and (
                row.forbidden_advance_violated or not row.summary_in_range
            ))
        ):
            raise BeatAlignmentBatchTechnicalError(
                f"{item.chapter_id} 的核对结果引用了规划外条目或自相矛盾"
            )
        substantive = (
            True if not item.info_increment.strip()
            else row.info_increment_substantive
        )
        if not substantive:
            _log.info(
                "beat_alignment_validator_info_increment_weak",
                chapter_id=item.chapter_id,
                beat_id=beat.beat_id,
                info_increment=item.info_increment,
                issue=row.info_increment_issue,
            )
        results[item.chapter_id] = BeatAlignmentValidationResult(
            passed=row.verdict == "pass",
            must_advance_advanced=row.must_advance_advanced,
            must_advance_missing=row.must_advance_missing,
            forbidden_advance_violated=row.forbidden_advance_violated,
            summary_in_range=row.summary_in_range,
            evidence=row.evidence,
            info_increment_substantive=substantive,
            info_increment_issue=("" if substantive else row.info_increment_issue),
            llm_calls_count=1,
            uncertain=row.verdict == "uncertain",
        )
    return results


def validate_beat_alignment(
    *,
    chapter_event: str,
    beat: VolumeBeatPlan,
    chapter_id: str,
    info_increment: str = "",
    character_context: str = "",
    chapter_design: dict[str, Any] | None = None,
    neighboring_events: str = "",
    workspace_id: str | None = None,
    router: ModelRouter | None = None,
    client: LLMClient | None = None,
    model: str | None = None,
    max_tokens: int = _DEFAULT_MAX_TOKENS,
    temperature: float = _DEFAULT_TEMPERATURE,
    prompts_dir: Path | None = None,
) -> BeatAlignmentValidationResult:
    """1 章 1 次综合 LLM 调用,判定 chapter_event 是否对齐 beat 设计。

    :param chapter_event: mini_plan 的 ``one_line_event`` 一句话(30-60 字)。
    :param beat: 本章归属的 ``VolumeBeatPlan``(由 caller 按 chapter_number 查找
        归属 beat,落 ``arc.beats[i].chapter_range`` 区间内的 beat)。
    :param chapter_id: 章节 ID,落 structlog 字段。
    :param info_increment: mini_plan 的 ``info_increment`` 一句话(A2,本章新增信息
        增量)。空串 → 跳过 info_increment 维度判定(``substantive=True`` 不报 warn,
        护后向兼容 + 过渡章)。非空 → judge 评估是否实质(WARN-only,不影响 passed)。
    :param workspace_id: 工作区 ID,落 structlog 字段(可选)。
    :param router: ``ModelRouter`` 实例;给定时按 ``router.route("beat_alignment_validator")``
        取 client + model + 生成参数。``None`` 时走 legacy 路径。
    :param client: legacy 路径 LLM 客户端;``router=None`` 时必传。
    :param model: legacy 路径模型名;``router=None`` 时必传。
    :param max_tokens: legacy 路径 token 上限。
    :param temperature: legacy 路径温度。
    :param prompts_dir: 提示词模板根目录;``None`` 时取 ``settings.prompts_dir``。
    :return: ``BeatAlignmentValidationResult``。
    """
    # 决策 3:must + forbidden 都空时沿用旧行为跳过，不因 A1 增加请求。
    # typed 高价值 beat 必须先通过纯代码门证明 must_advance 非空，因此不会
    # 借 dramatic_contract 把原本跳过的路径变成新 Provider 调用。
    if not beat.must_advance and not beat.forbidden_advance:
        _log.debug(
            "beat_alignment_validator_skipped",
            chapter_id=chapter_id,
            workspace_id=workspace_id,
            beat_id=beat.beat_id,
            reason="empty_must_and_forbidden",
        )
        return BeatAlignmentValidationResult(
            passed=True,
            llm_calls_count=0,
            skipped=True,
        )

    # 决策 4 扩:setup 阶段失败(prompt 文件缺失 / 路由错配)也走 fail-open。
    try:
        resolved_client, resolved_model, complete_kwargs = resolve_routing(
            agent_name="beat_alignment_validator",
            router=router,
            client=client,
            model=model,
            legacy_kwargs={"max_tokens": max_tokens, "temperature": temperature},
        )
        template_root = (
            prompts_dir if prompts_dir is not None else settings.prompts_dir
        ).resolve()
        system_text = load_prompt_template(template_root / _VALIDATOR_PROMPT_FILENAME)
        complete_kwargs = prepare_strict_json_output(
            client=resolved_client,
            model=resolved_model,
            output_model=_ValidatorLLMPayload,
            schema_name="beat_alignment",
            complete_kwargs=complete_kwargs,
        )
    except Exception as exc:  # noqa: BLE001 — fail-open 包括 setup 阶段
        if isinstance(exc, HandoffError):
            raise
        _log.warning(
            "beat_alignment_validator_setup_failed_open",
            chapter_id=chapter_id,
            workspace_id=workspace_id,
            beat_id=beat.beat_id,
            error_type=type(exc).__name__,
            error_message=str(exc),
        )
        return BeatAlignmentValidationResult(
            passed=True,
            llm_calls_count=0,
            fail_open=True,
        )

    user_text = _build_user_message(
        chapter_event=chapter_event,
        beat=beat,
        chapter_id=chapter_id,
        info_increment=info_increment,
        character_context=character_context,
        chapter_design=chapter_design,
        neighboring_events=neighboring_events,
    )

    handoff_data = {
        "chapter_event": chapter_event,
        "beat": beat,
        "chapter_id": chapter_id,
        "info_increment": info_increment,
        "character_context": character_context,
        "chapter_design": chapter_design,
    }
    if neighboring_events:
        handoff_data["neighboring_events"] = neighboring_events
    user_text = handoff_user_text(
        resolved_client, stage="beat_alignment_validator",
        data=handoff_data, original_task=user_text,
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
            raw_text=response.content, model_used=response.model_used
        )
    except Exception as exc:  # noqa: BLE001 — 决策 4:fail-open
        if isinstance(exc, HandoffError):
            raise
        _log.warning(
            "beat_alignment_validator_failed_open",
            chapter_id=chapter_id,
            workspace_id=workspace_id,
            beat_id=beat.beat_id,
            error_type=type(exc).__name__,
            error_message=str(exc),
        )
        return BeatAlignmentValidationResult(
            passed=True,
            llm_calls_count=1,
            fail_open=True,
        )

    passed = payload.verdict == "pass"
    if passed:
        _log.debug(
            "beat_alignment_validator_passed",
            chapter_id=chapter_id,
            workspace_id=workspace_id,
            beat_id=beat.beat_id,
            advanced_count=len(payload.must_advance_advanced),
            evidence=payload.evidence,
        )
    else:
        _log.info(
            "beat_alignment_validator_failed",
            chapter_id=chapter_id,
            workspace_id=workspace_id,
            beat_id=beat.beat_id,
            forbidden_violated=payload.forbidden_advance_violated,
            must_missing=payload.must_advance_missing,
            summary_in_range=payload.summary_in_range,
            evidence=payload.evidence,
        )

    # A2 info_increment 维度(WARN-only,不影响 passed)。空 info_increment 强制
    # substantive=True(不判 / 不报 warn);非空且 LLM 判非实质 → log warn(护 §0:
    # 信息密度是文学判断,只观测不阻断重试)。
    info_increment_substantive = (
        True if not info_increment.strip() else payload.info_increment_substantive
    )
    info_increment_issue = (
        "" if info_increment_substantive else payload.info_increment_issue
    )
    if not info_increment_substantive:
        _log.info(
            "beat_alignment_validator_info_increment_weak",
            chapter_id=chapter_id,
            workspace_id=workspace_id,
            beat_id=beat.beat_id,
            info_increment=info_increment,
            issue=info_increment_issue,
        )

    return BeatAlignmentValidationResult(
        passed=passed,
        must_advance_advanced=payload.must_advance_advanced,
        must_advance_missing=payload.must_advance_missing,
        forbidden_advance_violated=payload.forbidden_advance_violated,
        summary_in_range=payload.summary_in_range,
        evidence=payload.evidence,
        info_increment_substantive=info_increment_substantive,
        info_increment_issue=info_increment_issue,
        llm_calls_count=1,
        skipped=False,
        fail_open=False,
    )


def _build_user_message(
    *,
    chapter_event: str,
    beat: VolumeBeatPlan,
    chapter_id: str,
    info_increment: str = "",
    character_context: str = "",
    chapter_design: dict[str, Any] | None = None,
    neighboring_events: str = "",
) -> str:
    """拼装 user message:chapter_event + beat 全字段 + info_increment + 上下文 + marker。"""
    must_block = (
        "\n".join(f"  - {m}" for m in beat.must_advance)
        if beat.must_advance
        else "  (空)"
    )
    forbidden_block = (
        "\n".join(f"  - {f}" for f in beat.forbidden_advance)
        if beat.forbidden_advance
        else "  (空)"
    )
    drama = (
        beat.dramatic_contract.model_dump(mode="json")
        if beat.dramatic_contract is not None
        else None
    )
    increment_block = info_increment.strip() if info_increment.strip() else "(未提供)"
    body = (
        "## 本章事件(chapter_event)\n\n"
        f"{chapter_event}\n\n"
        "## 本章新增信息增量(info_increment,A2)\n\n"
        f"{increment_block}\n\n"
        "## 本章归属 beat\n\n"
        f"- beat_id: {beat.beat_id}\n"
        f"- chapter_range: [{beat.chapter_range[0]}, {beat.chapter_range[1]}]\n"
        f"- summary: {beat.summary}\n"
        "- must_advance:\n"
        f"{must_block}\n"
        "- forbidden_advance:\n"
        f"{forbidden_block}\n\n"
        "## typed 戏剧合同(post-v8 A1)\n\n"
        f"{drama if drama is not None else '(无)'}\n\n"
        "## 上下文\n\n"
        f"- chapter_id: {chapter_id}\n"
    )
    if character_context:
        body += "\n## 上游人物身份（只用于辨认实体，不授权提前演出后续事件）\n\n" + character_context + "\n"
    if chapter_design is not None:
        body += (
            "\n## 本章完整创意设计（与摘要共同审核，不得忽略互动与人物弧光）\n\n"
            + json.dumps(chapter_design, ensure_ascii=False, sort_keys=True) + "\n"
        )
    if neighboring_events:
        body += (
            "\n## 同卷其他章事件（仅用于核对事件是否提前或重复，"
            "不得替本章补动作）\n\n"
            + neighboring_events + "\n"
        )
    return append_role_marker(body, role="beat_alignment_validator")


def _parse_llm_payload(*, raw_text: str, model_used: str) -> _ValidatorLLMPayload:
    """剥代码块 → ``json.loads`` → Pydantic 校验。失败抛 ``ValueError``。"""
    text = _strip_code_fence(raw_text).strip()
    try:
        data = loads_with_quote_repair(
            text,
            agent_name="beat_alignment_validator",
            model_used=model_used,
        )
    except json.JSONDecodeError as exc:
        raise ValueError(
            f"beat_alignment_validator LLM 输出非合法 JSON(model={model_used}): "
            f"{raw_text[:200]!r}"
        ) from exc
    try:
        return _ValidatorLLMPayload.model_validate(data)
    except ValidationError as exc:
        raise ValueError(
            f"beat_alignment_validator LLM 输出 schema 不符(model={model_used}): "
            f"{exc.errors()}"
        ) from exc


def _strip_code_fence(text: str) -> str:
    """剥 trailing HTML 注释 + ``` / ```json 包裹(沿用 must_achieve_validator 同型)。"""
    while True:
        stripped = _TRAILING_HTML_COMMENT_RE.sub("", text)
        if stripped == text:
            break
        text = stripped
    match = _CODE_FENCE_RE.match(text)
    if match is not None:
        return match.group("body")
    return text


def format_validation_failure_for_retry(
    *, result: BeatAlignmentValidationResult, chapter_id: str, beat_id: str
) -> str:
    """格式化 validator 失败信息,供 retry-with-feedback 反馈给 LLM。

    设计:让 LLM 在 retry 时能从 error msg 直接读出"哪些 forbidden 触发了 /
    哪些 must 没推进 / summary 是否跑题",再生成 mini_plan 时主动避坑。
    """
    parts = [
        f"章节 {chapter_id} 在 beat {beat_id} 内的 alignment 校验失败:",
    ]
    if result.forbidden_advance_violated:
        parts.append(
            f"  - 被判定发生的 forbidden_advance（核对同一人物、动作和条件，不以删词代替修正）:"
            f" {result.forbidden_advance_violated}"
        )
    if result.must_advance_missing and not result.must_advance_advanced:
        parts.append(
            f"  - 未推进任何 must_advance(本章 event 应至少推进 1 条):"
            f" {result.must_advance_missing}"
        )
    if not result.summary_in_range:
        parts.append("  - chapter_event 跑出 beat.summary 范围(请按 beat 节奏单元收敛)")
    parts.append(f"  - LLM 判定依据:{result.evidence}")
    return "\n".join(parts)


__all__ = [
    "BeatAlignmentValidationResult",
    "validate_beat_alignment",
    "format_validation_failure_for_retry",
]
