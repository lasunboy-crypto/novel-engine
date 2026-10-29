"""forbidden_crossing LLM-as-judge validator(v2 W3.5 补强3 下游硬闸,2026-06-26)。

职责:在 writer **写完正文后**(审稿期),把"本章正文"与"本章卡 ``forbidden``
禁止情节清单"比对,**只报正文里真实发生了的越界**(禁止的事真的写出来了才报警,
没发生 / 只接近 / 只铺垫 = 不报警 = 护留白)。补强3 上游卡预防(A 出卡措辞 +
propagation + B backstop,`ee04d12`)降低越界**频率**,但卡层预防永远是概率
(writer 是概率体);本 agent 是**唯一读正文的硬闸**,拦"写出来的越界"。

为何独立 agent(而非折进 must_achieve)
=====================================

must_achieve_validator 的 prompt 明文"只判定达成与否……禁止元素等由别的 agent /
validator 处理"——它是被刻意剥成单一职责的判子,且与本判子**极性相反**(must =
要求事件"发生",forbidden = 要求事件"未发生")。折叠会让两套相反目标 + 两套精度
校准挤一个 prompt(forbidden 命门是**零误杀**,与 must_achieve 调性不同),无法独立
调 / 独立 validate / 干净映射重写指令。写后 5 闸**本就并发**(``_run_post_review_
llm_gates``),独立只多回传一次正文(墙钟零增量),换来精度隔离 + 爆炸半径隔离。
故 Feng 2026-06-26 拍板:**独立**(详 impl v2_w3_5_forbidden_crossing_judge.md §1)。

为何 qwen(而非写作的 deepseek)
==============================

deepseek 正是写作时越界的模型(语义优先冲过线),让它自审 = 查自己盲区、易太宽容
判不出越界;qwen 卡线上就停 = 严格第二双眼(同 must_feasibility / outline_consistency
切 qwen 同逻辑,§9.1 符号边界活交 qwen)。

HARD(覆盖 verdict→rewrite,区别 setting_consistency 的 SOFT)
==========================================================

caller(``chapter_orchestrator._merge_forbidden_crossing``)在 ``violations`` 非空时
**覆盖 verdict=rewrite + hard_constraints_passed=False + 合并 failed_constraints**
(克隆 ``_merge_must_achieve`` HARD 语义)→ 触发 reviser 删越界重写;budget 耗尽
→ 升人工。**为何敢 HARD**:越界 = 违反硬剧情边界(跨章自相矛盾 / 提前烧高潮),
非文学留白,确凿发生就该重写。**前提 = validate-first 零误杀达标**(误杀好章 =
rewrite churn / mode collapse,比漏报更伤);精度未达标前不接 HARD。

设计要点(沿用 setting_consistency_validator 同型架构,不发明新哲学)
================================================================

1. **1 次综合 LLM 调用**:本章正文 + forbidden 清单一次性看完综合判定。
2. **沿用 *_validator 路由**:agent_name ``"forbidden_crossing_validator"`` →
   ``agent_routing.forbidden_crossing_validator``(config.yaml,cloud_qwen_max_thinking
   thinking on);未显式配则 ``resolve_routing`` fallback 走 reviewer profile。
3. **LLM 失败 fail-open**:LLM 抛异常 / 返非法 JSON / setup 失败 → ``passed=True``
   + log warn(宁可漏报、不误伤——且 fail-open 不触发误重写,与零误杀同向)。
4. **skip 边界**:``forbidden`` 为空 / 正文为空 → skip(无判定对象),不发起 LLM 调用。
5. **agent_role marker = ``forbidden_crossing_validator``(无 chapter_id):
   MockLLMClient 识别 marker 返默认"无越界"JSON,既有审稿 happy 路径不回归。
"""

from __future__ import annotations

import json
import re
from dataclasses import dataclass, field
from pathlib import Path

import structlog
from pydantic import BaseModel, ConfigDict, Field, ValidationError

from src.config import settings
from src.core.agents._json_contract import loads_with_quote_repair
from src.core.agents._prompt import (
    append_role_marker,
    load_prompt_template,
    resolve_routing,
)
from src.core.agents._structured_output import prepare_strict_json_output
from src.core.llm.base import LLMClient, Message
from src.core.models.review import ConstraintResult, stable_constraint_id
from src.core.routing.router import ModelRouter

_log = structlog.get_logger(__name__)

_VALIDATOR_PROMPT_FILENAME = "forbidden_crossing_validator_v4.md"

# 默认生成参数:综合判定对采样稳定性敏感,温度低;输出含 0-N 个
# {item_index + evidence},token 上限适中。router 路径下由 profile 决定。
_DEFAULT_TEMPERATURE = 0.3
_DEFAULT_MAX_TOKENS = 1000

# 本章正文判定窗口上限(自决项):越界最常在**章末** overshoot(终点 overshoot),
# 但也可能中段;故取较大窗口。封顶控成本——单章硬上限 ≤5000 字(Feng 商业上限 +
# deepseek word_count_trim 强制),6000 字 = 全章覆盖含结尾。
_CHAPTER_MAX_CHARS = 6000

_CODE_FENCE_RE = re.compile(
    r"^\s*```(?:json)?\s*\n(?P<body>.*?)\n```\s*$",
    re.DOTALL,
)
_TRAILING_HTML_COMMENT_RE = re.compile(
    r"\s*<!--.*?-->\s*$",
    re.DOTALL,
)

# 这些词即使同时出现在规则和正文，也不能唯一绑定到具体人物、对象或事件。
# zero-false-positive 优先：主体只能给出代词/占位词时，保守丢弃而不形成 HARD。
_AMBIGUOUS_SUBJECTS = frozenset(
    {
        "他",
        "她",
        "它",
        "其",
        "他们",
        "她们",
        "它们",
        "此人",
        "此物",
        "人物",
        "角色",
        "对象",
        "事件",
        "本章",
        "正文",
    }
)


@dataclass(frozen=True)
class ForbiddenCrossingResult:
    """validator 输出。

    - ``passed``:``violations`` 为空时 True;非空时 False。LLM / setup 失败 → True
      (fail-open)。**注意**:caller 把 violations 作 HARD 处置(覆盖 verdict=rewrite),
      ``passed=False`` → 触发重写(区别 setting_consistency 的 SOFT)。
    - ``violations``:LLM 判定本章正文里**真实发生了**的 forbidden 越界(每条 =
      人类可读短句,含被违反的 forbidden 整句 + 正文证据)。
    - ``llm_calls_count``:实际 LLM 调用次数;正常 → 1;skip → 0;fail-open → 0 或 1。
    - ``skipped``:forbidden 为空 / 正文为空 → True(passed=True + 0 调用)。
    - ``fail_open``:LLM / setup 异常导致 fail-open(passed=True)时 True。
    """

    passed: bool
    violations: list[str] = field(default_factory=list)
    constraint_results: list[ConstraintResult] = field(default_factory=list)
    llm_calls_count: int = 0
    skipped: bool = False
    fail_open: bool = False


class _CrossingItem(BaseModel):
    """LLM 输出的单条越界；编号只映射到输入权威清单。"""

    model_config = ConfigDict(extra="forbid")

    item_index: int = Field(..., ge=1)
    subject: str = Field(..., min_length=1, max_length=80)
    evidence: str = Field(..., min_length=1, max_length=300)


class _ForbiddenLLMPayload(BaseModel):
    """LLM 输出的中间模型。``violations`` 空 = 本章无越界(合格,最常见)。"""

    model_config = ConfigDict(extra="forbid")

    violations: list[_CrossingItem] = Field(default_factory=list)


def validate_forbidden_crossing(
    *,
    chapter_text: str,
    forbidden: list[str],
    must_achieve: list[str] | None = None,
    chapter_id: str,
    workspace_id: str | None = None,
    router: ModelRouter | None = None,
    client: LLMClient | None = None,
    model: str | None = None,
    max_tokens: int = _DEFAULT_MAX_TOKENS,
    temperature: float = _DEFAULT_TEMPERATURE,
    prompts_dir: Path | None = None,
) -> ForbiddenCrossingResult:
    """1 次综合 LLM 调用,判定本章正文是否真实发生了卡 ``forbidden`` 里禁止的事。

    语义比对(非字面子串):forbidden 是整句剧情禁元素,正文用别的措辞写出同一件事
    (含隐式越界:被光吞没 / 空间在身后合拢 / 视角已在禁区内)仍算越界;接近 / 止于
    临界点 / 仅铺垫 / 想象回忆梦境 = 不算(护留白、零误杀第一)。

    :param chapter_text: 本章正文(取前 ``_CHAPTER_MAX_CHARS`` 字判定)。
    :param forbidden: 本章卡 ``forbidden`` 清单(2-4 条整句剧情禁元素);空 → skip。
    :param must_achieve: 本章卡 ``must_achieve`` 清单。只作为人物、对象与事件身份
        消歧上下文，不豁免真正发生的 forbidden；空清单不影响既有判定。
    :param chapter_id: 章节 ID,落 structlog 字段。
    :param workspace_id: 工作区 ID,落 structlog 字段(可选)。
    :param router: ``ModelRouter`` 实例;给定时按
        ``router.route("forbidden_crossing_validator")`` 取 client + model + 生成参数。
    :param client: legacy 路径 LLM 客户端;``router=None`` 时必传。
    :param model: legacy 路径模型名;``router=None`` 时必传。
    :param max_tokens: legacy 路径 token 上限。
    :param temperature: legacy 路径温度。
    :param prompts_dir: 提示词模板根目录;``None`` 时取 ``settings.prompts_dir``。
    :return: ``ForbiddenCrossingResult``。
    """
    forbidden_items = [f.strip() for f in (forbidden or []) if f and f.strip()]

    # skip 边界:forbidden 为空(无判定对象)/ 正文为空。
    if not forbidden_items or not chapter_text.strip():
        _log.debug(
            "forbidden_crossing_validator_skipped",
            chapter_id=chapter_id,
            workspace_id=workspace_id,
            reason=(
                "empty_chapter_text" if not chapter_text.strip() else "empty_forbidden"
            ),
        )
        return ForbiddenCrossingResult(passed=True, skipped=True, llm_calls_count=0)

    # setup 阶段失败(prompt 缺失 / 路由错配)走 fail-open。
    try:
        resolved_client, resolved_model, complete_kwargs = resolve_routing(
            agent_name="forbidden_crossing_validator",
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
            output_model=_ForbiddenLLMPayload,
            schema_name="forbidden_crossing",
            complete_kwargs=complete_kwargs,
        )
    except Exception as exc:  # noqa: BLE001 — fail-open 包括 setup 阶段
        _log.warning(
            "forbidden_crossing_validator_setup_failed_open",
            chapter_id=chapter_id,
            workspace_id=workspace_id,
            error_type=type(exc).__name__,
            error_message=str(exc),
        )
        return ForbiddenCrossingResult(passed=True, fail_open=True, llm_calls_count=0)

    visible_chapter_text = chapter_text[:_CHAPTER_MAX_CHARS]
    user_text = _build_user_message(
        chapter_text=visible_chapter_text,
        forbidden_items=forbidden_items,
        must_achieve_items=[
            item.strip() for item in (must_achieve or []) if item and item.strip()
        ],
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
        indices = [item.item_index for item in payload.violations]
        if len(indices) != len(set(indices)) or any(
            index > len(forbidden_items) for index in indices
        ):
            raise ValueError(
                f"forbidden_crossing_validator item_index 重复或越界: {indices!r}"
            )
    except Exception as exc:  # noqa: BLE001 — fail-open
        _log.warning(
            "forbidden_crossing_validator_failed_open",
            chapter_id=chapter_id,
            workspace_id=workspace_id,
            error_type=type(exc).__name__,
            error_message=str(exc),
        )
        return ForbiddenCrossingResult(passed=True, fail_open=True, llm_calls_count=1)

    bound_violations: list[_CrossingItem] = []
    for item in payload.violations:
        rule = forbidden_items[item.item_index - 1]
        subject_is_ambiguous = item.subject.strip() in _AMBIGUOUS_SUBJECTS
        if (
            item.evidence not in visible_chapter_text
            or item.subject not in rule
            or item.subject not in item.evidence
            or subject_is_ambiguous
        ):
            _log.warning(
                "forbidden_crossing_unbound_evidence_dropped",
                chapter_id=chapter_id,
                workspace_id=workspace_id,
                item_index=item.item_index,
                subject=item.subject,
                evidence_in_text=item.evidence in visible_chapter_text,
                subject_in_rule=item.subject in rule,
                subject_in_evidence=item.subject in item.evidence,
                subject_is_ambiguous=subject_is_ambiguous,
                note="zero-false-positive guard: ambiguous subject/evidence is not HARD",
            )
            continue
        bound_violations.append(item)

    violation_by_index = {item.item_index: item for item in bound_violations}
    source_path = f"chapter_cards/{chapter_id}.forbidden"
    constraint_results = [
        ConstraintResult(
            constraint_id=stable_constraint_id(
                source_path=source_path,
                domain="forbidden",
                rule_text=rule,
            ),
            domain="forbidden",
            source_path=source_path,
            rule_text=rule,
            status="fail" if index in violation_by_index else "pass",
            details=(
                "subject="
                f"{violation_by_index[index].subject}; evidence="
                f"{violation_by_index[index].evidence}"
                if index in violation_by_index
                else ""
            ),
        )
        for index, rule in enumerate(forbidden_items, start=1)
    ]
    violations = [
        (
            f"forbidden 越界:「{forbidden_items[item.item_index - 1]}」"
            f"在本章正文真实发生——{item.evidence}"
        )
        for item in bound_violations
    ]
    passed = not violations
    if passed:
        _log.debug(
            "forbidden_crossing_validator_passed",
            chapter_id=chapter_id,
            workspace_id=workspace_id,
        )
    else:
        _log.info(
            "forbidden_crossing_validator_violations",
            chapter_id=chapter_id,
            workspace_id=workspace_id,
            violation_count=len(violations),
            violations=violations,
        )

    return ForbiddenCrossingResult(
        passed=passed,
        violations=violations,
        constraint_results=constraint_results,
        llm_calls_count=1,
        fail_open=False,
    )


def _build_user_message(
    *,
    chapter_text: str,
    forbidden_items: list[str],
    must_achieve_items: list[str],
) -> str:
    """拼装 user message:must 消歧上下文 + forbidden 清单 + 正文 + marker。"""
    forbidden_block = "\n".join(
        f"{i}. {item}" for i, item in enumerate(forbidden_items, start=1)
    )
    must_achieve_block = (
        "\n".join(f"{i}. {item}" for i, item in enumerate(must_achieve_items, start=1))
        if must_achieve_items
        else "（无）"
    )
    body = (
        "## 本章必须发生的情节(must_achieve;仅作人物/对象/事件身份消歧，"
        "不豁免真正越界)\n\n"
        f"{must_achieve_block}\n\n"
        "## 本章禁止发生的情节(forbidden;判定正文是否真实发生了其中某条所禁的事)\n\n"
        f"{forbidden_block}\n\n"
        "## 本章正文\n\n"
        f"{chapter_text}\n"
    )
    return append_role_marker(body, role="forbidden_crossing_validator")


def _parse_llm_payload(*, raw_text: str, model_used: str) -> _ForbiddenLLMPayload:
    """剥代码块 → ``json.loads`` → Pydantic 校验。失败抛 ``ValueError``。"""
    text = _strip_code_fence(raw_text).strip()
    try:
        data = loads_with_quote_repair(
            text,
            agent_name="forbidden_crossing_validator",
            model_used=model_used,
        )
    except json.JSONDecodeError as exc:
        raise ValueError(
            f"forbidden_crossing_validator LLM 输出非合法 JSON(model={model_used}): "
            f"{raw_text[:200]!r}"
        ) from exc
    try:
        return _ForbiddenLLMPayload.model_validate(data)
    except ValidationError as exc:
        raise ValueError(
            f"forbidden_crossing_validator LLM 输出 schema 不符(model={model_used}): "
            f"{exc.errors()}"
        ) from exc


def _strip_code_fence(text: str) -> str:
    """剥 trailing HTML 注释 + ``` / ```json 包裹(沿用同型清理)。"""
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
    "ForbiddenCrossingResult",
    "validate_forbidden_crossing",
]
