"""Word-count trim agent(v2 W3.5-s3rev 引入，R9 v5 收紧为源绑定删除)。

职责:对 writer 出稿的**单章正文**,在字数**超上限(above_max)**时做一道独立
LLM 精简 pass。Provider 只能从每刀冻结源中按原顺序删除字符；代码拒绝回显、
新增、替换、重排、截断与砍秃结果。采用稿再走完整 review/validator 管线。

为什么是"写后 trim"而非"生成时控长度",以及为什么是 trim 不是 rewrite
======================================================================
W3.5-s3rev 控长度实验(2026-06-22,详
``docs/acceptance/v2_w3_5_s3rev_length_ab_findings.md``)钉死:

- **生成时控长度证伪**:writer 盲写数不准字数(prompt 防短/防长、起草克制、
  thinking-on 全失败);RCT 同章草稿横跨 2794~7154。
- **trim 胜 rewrite(对调盲读 2:0)**:超长触发"盲 rewrite 整章重生成"会**丢 v1
  已写出的好剧情 + 把过场水加回来 + 偶尔越压越长(4619→6471)**;而 trim 看着真
  正文砍,**保留好剧情、只砍水**,方向永远变短。
- **带校验有界重试可靠(9/9 ≤cap)**:单次 trim 砍力有方差,但"trim → 代码量字数
  校验 → 出界(>cap / 跑飞)再砍一刀(最多 2 次)"可靠落 ≤cap;弱软下限(只 2500、
  不"贴着上限多留")让极端超长(7154)也能 3/3 砍进。

工程化(沿用 anti_tic_trim 范式)
================================
① 当前 prompt 落 ``prompts/word_count_trim.md``；原实验版逐字归档为
   ``prompts/archive/word_count_trim_v1.md``
② 路由**复用 writer profile**(``resolve_routing(agent_name="writer")``)，但
   单次强制 ``enable_thinking=false`` 并移除思考强度；trim 与 writer 同模型，
   不让 reasoning token 触发既有 5500-token 跑飞护栏。③ 写后 gated pass:在
   ``_call_writer`` 闭包内 anti_tic_trim 之后跑,draft + rewrite 两路出稿都经此
   闭包 → 超长稿当场被压平(review 看到的就是 ≤cap 稿,根本不进 rewrite 循环)。

护栏(零 fail-open —— 任何异常 / 不收敛全回退原文,绝不交残章 / 阻断生成)
========================================================================
- **触发 gate**(pre-trim):``len(content) <= word_count_max`` → **跳过**(开关关 /
  没超长 = 零成本零延迟,行为与本功能前逐字节等价)。
- **源绑定合同**:每刀冻结 source SHA、长度、must 原文片段与章末 80 字；输出必须是
  严格更短的原序子序列。无进展/自由改写不算成功，也不能成为下一刀权威源。
- **bounded-retry 校验**:每次 trim 后由代码量 ``len(输出)``；名义目标仍原样告诉
  trim Agent，本地只在隐藏验收区外才补刀。>区外上界沿安全删稿继续；<区外下界
  回到原稿，最多 ``max_attempts`` 次。
- **跑飞 / 截断护栏**:``output_tokens > spiral_output_tokens``(deepseek 关 thinking
  仍偶发漏大段 reasoning)或 ``finish_reason == "length"`` → 该次作废、重试。
- **不收敛兜底**:N 次仍无合格输出 → **回退原文**(``trimmed=False``)→ 调用方保留
  原稿并进入既有 rewrite 纠偏；低于隐藏下限的瘦稿绝不以 best effort 落盘。

收益 caveat(诚实记)
====================
实验仅 deepseek 1 本书 3 超长章(含极端 7154)× 3 次 = 9/9 ≤cap 验过;质量维度
对调盲读 trim 胜 rewrite。更多题材 / 模型 / 极端分布未验。默认关
(``GenerationConfig.word_count_trim_enabled``)。
"""

from __future__ import annotations

import hashlib
from pathlib import Path

import structlog
from pydantic import BaseModel

from src.core.agents.creative_delta import (
    EXPERIMENTAL_PIPELINE_CONTRACT, uses_experimental_pipeline,
)
from src.config import settings
from src.core.agents.structured_handoff import HandoffError, handoff_user_text
from src.core.agents._prompt import load_prompt_template, resolve_routing
from src.core.llm.base import LLMClient, Message
from src.core.models.chapter_card import ChapterCard
from src.core.routing.router import ModelRouter
from src.core.rules.validators import classify_word_count_acceptance

_log = structlog.get_logger(__name__)

_TRIM_PROMPT_FILENAME = "word_count_trim.md"

# ── 护栏 / 调参默认值(实验数据支持,详 findings §最终验证)──────────────────────
#: 软下限:trim 输出 < 此值视为砍秃(瘦);band = [soft_floor, word_count_max]。
DEFAULT_SOFT_FLOOR = 2500
#: 最多 trim 次数(实验 9/9 均 1.3 次;2 次足够,留 1 次兜极端超长)。
DEFAULT_MAX_ATTEMPTS = 2
#: 首刀 aim = word_count_max - 此值(留余量吸收 trim ~300 欠砍;实验 cap-600 稳)。
DEFAULT_AIM_MARGIN = 600
#: 出界(>cap)补刀时 aim = word_count_max - 此值(砍更狠)。
DEFAULT_AIM_MARGIN_AGGRESSIVE = 1100
#: output_tokens 超此值 = reasoning 跑飞(正常 trim 输出 ~2500-4000 tok)。
DEFAULT_SPIRAL_OUTPUT_TOKENS = 5500
#: trim 首跑 max_tokens 下限(deepseek profile 仅 8000,跑飞 + 正文会吃光截断;给足 16000)。
DEFAULT_MAX_TOKENS_FLOOR = 16000


class WordCountTrimOutput(BaseModel):
    """word-count trim pass 结果(provider 无关)。

    ``trimmed=True`` → ``content`` 是通过源绑定合同且落在本地验收带的压缩结果；
    ``trimmed=False`` → ``content`` 是**回退的原文**(没超长跳过 / 不收敛 / 异常)。调用方
    据 ``trimmed`` 决定是否替换 writer 出稿,``reason`` 供日志 / 测试断言。
    """

    content: str
    trimmed: bool
    reason: str
    model_used: str | None = None
    orig_chars: int = 0
    final_chars: int = 0
    attempts: int = 0
    chain: list[int] = []


class WordCountTrimContract(BaseModel):
    """一次 trim 的冻结源身份；Provider 只能对该源做保序删除。"""

    source_sha256: str
    source_chars: int
    target_min: int
    target_max: int
    protected_fragments: tuple[str, ...] = ()


def _build_trim_contract(
    *, content: str, chapter_card: ChapterCard, target_min: int
) -> WordCountTrimContract:
    protected = [item for item in chapter_card.must_achieve if item in content]
    tail = content.rstrip()[-80:]
    if tail:
        protected.append(tail)
    return WordCountTrimContract(
        source_sha256=hashlib.sha256(content.encode("utf-8")).hexdigest(),
        source_chars=len(content),
        target_min=target_min,
        target_max=chapter_card.word_count_max,
        protected_fragments=tuple(dict.fromkeys(protected)),
    )


def _is_source_bound_deletion(
    *, source: str, candidate: str, contract: WordCountTrimContract
) -> bool:
    """只接受从冻结源逐字删除所得的严格子序列；插入/改写/同文均拒绝。"""

    if (
        len(source) != contract.source_chars
        or hashlib.sha256(source.encode("utf-8")).hexdigest() != contract.source_sha256
    ):
        return False
    if not candidate or len(candidate) >= len(source):
        return False
    cursor = 0
    for char in candidate:
        found = source.find(char, cursor)
        if found < 0:
            return False
        cursor = found + 1
    return all(fragment in candidate for fragment in contract.protected_fragments)


def load_word_count_trim_prompt(prompts_dir: Path | None = None) -> str:
    """加载 ``prompts/word_count_trim.md`` 作 trim system prompt(单一真相源)。

    :param prompts_dir: 提示词目录;``None`` → ``settings.prompts_dir``。
    :raises FileNotFoundError: 文件不存在(调用方在更高层兜异常,不静默)。
    """
    base = prompts_dir if prompts_dir is not None else settings.prompts_dir
    return load_prompt_template((base / _TRIM_PROMPT_FILENAME).resolve()).strip()


def _format_user_message(
    *,
    content: str,
    musts: str,
    word_count_max: int,
    aim: int,
    contract: WordCountTrimContract,
) -> str:
    """拼 trim user message:must(绝不删)+ 硬上限/瞄准 + 待精简正文。"""
    cur_len = len(content)
    cut_to_cap = max(0, cur_len - word_count_max)
    return (
        f"## 本章必达事件(must_achieve,绝不删)\n{musts}\n\n"
        f"## 硬上限:全文必须 ≤ {word_count_max} 字(当前 {cur_len} 字,"
        f"至少要砍掉 {cut_to_cap} 字才达标),瞄准 ~{aim} 字落点\n\n"
        f"## 名义目标下限\n{contract.target_min} 字（不得砍到更短）\n\n"
        f"## 冻结源 SHA-256\n{contract.source_sha256}\n\n"
        f"## 待精简正文:\n{content}"
    )


def trim_to_word_count(
    *,
    content: str,
    chapter_card: ChapterCard,
    router: ModelRouter | None = None,
    client: LLMClient | None = None,
    model: str | None = None,
    prompts_dir: Path | None = None,
    chapter_id: str | None = None,
    soft_floor: int = DEFAULT_SOFT_FLOOR,
    max_attempts: int = DEFAULT_MAX_ATTEMPTS,
    aim_margin: int = DEFAULT_AIM_MARGIN,
    aim_margin_aggressive: int = DEFAULT_AIM_MARGIN_AGGRESSIVE,
    spiral_output_tokens: int = DEFAULT_SPIRAL_OUTPUT_TOKENS,
    max_tokens_floor: int = DEFAULT_MAX_TOKENS_FLOOR,
) -> WordCountTrimOutput:
    """超长正文 → 源绑定删除并做 bounded retry；异常或不收敛逐字回退。

    路由复用 writer profile(``resolve_routing(agent_name="writer")``)。**任何异常 / 不
    收敛 → 回退原文**(``trimmed=False``),绝不交残章 / 阻断生成。

    :param content: writer 出稿的单章正文。
    :param chapter_card: 取 ``word_count_max`` + ``must_achieve``(must 绝不删)。
    :returns: ``WordCountTrimOutput``;``trimmed`` 决定是否采用。
    """
    # 新实验按 ChapterCard 闭区间验收；旧管线保留历史容差。
    allow_word_count_tolerance = not (
        getattr(router, "pipeline_contract", None) == EXPERIMENTAL_PIPELINE_CONTRACT
        or (client is not None and uses_experimental_pipeline(client))
    )
    cap = chapter_card.word_count_max
    orig_len = len(content)

    def _fallback(
        reason: str,
        model_used: str | None = None,
        chain: list[int] | None = None,
        attempts: int = 0,
    ) -> WordCountTrimOutput:
        return WordCountTrimOutput(
            content=content,
            trimmed=False,
            reason=reason,
            model_used=model_used,
            orig_chars=orig_len,
            final_chars=orig_len,
            attempts=attempts,
            chain=chain or [orig_len],
        )

    # ── 触发 gate:没超上限 → 跳过(零成本) ────────────────────────────────────
    if orig_len <= cap:
        return _fallback("skipped_within_cap")

    # 路由解析(失败回退原文,不阻断)。
    try:
        resolved_client, resolved_model, kwargs = resolve_routing(
            agent_name="writer",
            router=router,
            client=client,
            model=model,
            legacy_kwargs={},
        )
    except Exception as exc:  # noqa: BLE001 — 路由失败回退原文,绝不阻断生成
        if isinstance(exc, HandoffError):
            raise
        _log.warning(
            "word_count_trim_routing_failed", chapter_id=chapter_id, error=str(exc)
        )
        return _fallback("fallback_error")

    kwargs = dict(kwargs)
    profile_max_tokens = max(
        int(kwargs.get("max_tokens") or 0),
        int(kwargs.get("max_completion_tokens") or 0),
    )
    call_max_tokens = max(profile_max_tokens, max_tokens_floor)
    prepare_manifest_call = getattr(
        resolved_client,
        "prepare_bounded_non_thinking_call",
        None,
    )
    if callable(prepare_manifest_call):
        call_kwargs = prepare_manifest_call(
            kwargs,
            max_completion_tokens=call_max_tokens,
        )
    else:
        if "enable_thinking" in kwargs:
            kwargs["enable_thinking"] = False
        if "preserve_thinking" in kwargs:
            kwargs["preserve_thinking"] = False
        kwargs.pop("thinking_budget", None)
        kwargs.pop("reasoning_effort", None)
        call_kwargs = {**kwargs, "max_tokens": call_max_tokens}

    try:
        prompt = load_word_count_trim_prompt(prompts_dir)
    except Exception as exc:  # noqa: BLE001 — prompt 缺失回退原文,绝不阻断生成
        if isinstance(exc, HandoffError):
            raise
        _log.warning(
            "word_count_trim_prompt_load_failed", chapter_id=chapter_id, error=str(exc)
        )
        return _fallback("fallback_error")

    musts = "\n".join(f"- {m}" for m in (chapter_card.must_achieve or [])) or "(无)"
    chain = [orig_len]
    cur = content
    aim = cap - aim_margin
    last_model: str | None = None
    attempts_made = 0

    for attempt in range(1, max_attempts + 1):
        attempts_made = attempt
        contract = _build_trim_contract(
            content=cur,
            chapter_card=chapter_card,
            target_min=soft_floor,
        )
        user = _format_user_message(
            content=cur,
            musts=musts,
            word_count_max=cap,
            aim=aim,
            contract=contract,
        )
        user = handoff_user_text(
            resolved_client, stage="word_count_trim",
            data={"content": cur, "must_achieve": chapter_card.must_achieve,
                  "word_count_max": cap, "aim": aim, "contract": contract},
            original_task=user,
        )
        try:
            resp = resolved_client.complete(
                messages=[
                    Message(role="system", content=prompt),
                    Message(role="user", content=user),
                ],
                model=resolved_model,
                **call_kwargs,
            )
        except Exception as exc:  # noqa: BLE001 — LLM 失败:用已有 best 或回退原文
            if isinstance(exc, HandoffError):
                raise
            _log.warning(
                "word_count_trim_call_failed",
                chapter_id=chapter_id,
                attempt=attempt,
                error=str(exc),
            )
            break

        last_model = resp.model_used
        out = (resp.content or "").strip()
        n = len(out)
        chain.append(n)
        out_tokens = int(getattr(resp.usage, "output_tokens", 0) or 0)
        spiraled = out_tokens > spiral_output_tokens
        truncated = resp.finish_reason == "length"

        # 跑飞 / 截断 / 空 → 该次作废,拿原稿重砍。
        if not out or spiraled or truncated:
            _log.warning(
                "word_count_trim_attempt_bad",
                chapter_id=chapter_id,
                attempt=attempt,
                chars=n,
                output_tokens=out_tokens,
                spiraled=spiraled,
                truncated=truncated,
            )
            cur, aim = content, cap - aim_margin
            continue

        source_bound = _is_source_bound_deletion(
            source=cur,
            candidate=out,
            contract=contract,
        )
        if not source_bound:
            _log.warning(
                "word_count_trim_contract_rejected",
                chapter_id=chapter_id,
                attempt=attempt,
                source_sha256=contract.source_sha256,
                source_chars=len(cur),
                candidate_chars=n,
                reason=(
                    "no_progress" if out == cur else "not_order_preserving_deletion"
                ),
            )
            cur, aim = content, cap - aim_margin_aggressive
            continue

        acceptance_band = classify_word_count_acceptance(
            n,
            word_count_min=soft_floor,
            word_count_max=cap,
            allow_word_count_tolerance=allow_word_count_tolerance,
        )
        if acceptance_band != "rejected":
            _log.info(
                "word_count_trim_applied",
                chapter_id=chapter_id,
                attempt=attempt,
                orig=orig_len,
                final=n,
                chain=chain,
                acceptance_band=acceptance_band,
            )
            return WordCountTrimOutput(
                content=out,
                trimmed=True,
                reason=(
                    "trimmed" if acceptance_band == "target" else "trimmed_tolerated"
                ),
                model_used=last_model,
                orig_chars=orig_len,
                final_chars=n,
                attempts=attempt,
                chain=chain,
            )
        # 仍在隐藏验收区外：上方过长则沿本轮安全删稿继续补刀；下方过短
        # 回到原稿重试，绝不把砍秃稿当 best effort 落盘。
        if n > cap:
            cur, aim = out, cap - aim_margin_aggressive
        else:
            cur, aim = content, cap - 100

    _log.warning(
        "word_count_trim_not_converged",
        chapter_id=chapter_id,
        orig=orig_len,
        chain=chain,
    )
    return _fallback(
        "fallback_not_converged",
        model_used=last_model,
        chain=chain,
        attempts=attempts_made,
    )
