"""Anti-tic trim agent(v2 W3.5 wave-3 引入)。

职责:对 writer 出稿的**单章正文**做一道**独立 LLM 润色 pass**,只清两类
deepseek-v4-pro 固有句式 tic —— ①「不是 A,是 B」否定对比句式上瘾、②演后
盖章金句 —— **绝不改动剧情/人物/对白内容/关键细节/段落结构**。清完的稿再走
正常 review/validator 管线。

为什么是"写后手术"而非"生成时干预"
====================================
W3.5 wave-3 prose 实验(2026-06-17,详
``docs/acceptance/v2_w3_5_prose_anti_tic_experiment.md``)三机制对调盲读钉死:
生成时「列禁令」证伪(tic morphing + backfire)、「正面范例」平局(核心 tic 量化
纹丝不动),唯有**写后独立 trim pass** 成功 —— 悬疑 6 章×2 对调盲读 12:0 横扫、
跨题材(治愈)8:4 偏 trim。关键洞察:这个 tic 在内容 held 恒定时才显,只能事后
手术,不能生成时干预。

工程化与实验 driver 的关系
==========================
实验 driver(``scripts/v2_w3_5_prose_ab_methods.py::TRIM_PROMPT``)的 trim 机制
搬到生产:① prompt 落 ``prompts/anti_tic_trim.md`` 作**单一真相源**(本模块从
此文件加载,内容与验证版 driver 常量字节一致)② 路由**复用 writer profile**
(``resolve_routing(agent_name="writer")``)，但单次强制 ``enable_thinking=false``
并移除思考强度，trim 与 writer 同模型③ 加**三护栏**对应实验暴露的两个真实下行。

三护栏(违反全部回退原文 —— 绝不交残章 / 改坏干净章)
====================================================
- **① tic 密度 gate**(pre-trim):窄计「不是X是Y」< ``tic_threshold`` 则**跳过**
  trim,直接返原文。防在 0-tic 干净章上"手痒"改坏不该碰的好句(实验 ch-0008:
  trim 在 0-tic 章削软了结尾题眼金句 → 读者选原版)。
- **② 截断护栏**(post-trim):``finish_reason == "length"`` → 抬 max_tokens 重试
  一次,仍截断 → 回退原文。防交残章(实验 ch-0007:deepseek profile max_tokens
  8000 被 reasoning + 正文吃光截断成残章;提高预算重跑干净)。floor ≥16000。
- **③ 越界护栏**(post-trim):trim 输出长度越界(过短=summarize 掉内容 / refusal,
  过长=加戏)或 tic 不降反增 → 回退原文。trim 只换 tic 句,字数应几乎不变(实验
  实测变动 <1%),长度带 [0.75, 1.10] 是宽松安全网。

收益 caveat(诚实记)
====================
实验仅 deepseek 2 题材 6+6 章验过,收益随 tic 密度走(悬疑密集大胜、治愈稀疏弱),
非万能;更多题材/模型未验。开关默认关(``GenerationConfig.anti_tic_trim_enabled``)。

设计要点(沿用 writer / reviser agent)
======================================
- **依赖反转**:函数签名只接 ``ModelRouter`` / ``LLMClient`` 协议;具体客户端由
  orchestrator 注入。router 优先 / legacy client+model 兜底(对齐 ``resolve_routing``)。
- **提示词路径来自 Settings**:``prompts_dir=None`` 时从 ``settings.prompts_dir`` 取。
- **零 fail-open / 不阻断生成**:任何异常(路由失败 / LLM 调用失败 / 截断 / 越界)
  都**回退原文**返回,绝不让 trim 失败阻断章节生成 —— trim 是锦上添花,失败=退回
  writer 原稿(= 功能关时的等价行为)。
"""

from __future__ import annotations

import re
from pathlib import Path
from typing import Any

import structlog
from pydantic import BaseModel

from src.config import settings
from src.core.agents.structured_handoff import HandoffError, handoff_user_text
from src.core.agents._prompt import load_prompt_template, resolve_routing
from src.core.llm.base import LLMClient, Message
from src.core.routing.router import ModelRouter

_log = structlog.get_logger(__name__)

_TRIM_PROMPT_FILENAME = "anti_tic_trim.md"

# ── 护栏默认参数(实验数据支持,详 acceptance §3.4 / §4)──────────────────────
#: 窄计「不是X是Y」< 此值则跳过 trim(默认 1 = 0-tic 章跳过,直防实验 ch-0008 越界)。
DEFAULT_TIC_THRESHOLD = 1
#: trim 首跑 max_tokens 下限(deepseek profile 仅 8000,实验 ch-0007 被吃光截断;给足 16000)。
DEFAULT_MAX_TOKENS_FLOOR = 16000
#: 截断重试时的 max_tokens(严格大于首跑预算)。
DEFAULT_MAX_TOKENS_RETRY = 24000
#: trim 输出 / 原文 长度比下限(越界护栏:过短 = summarize 掉内容 / refusal)。
DEFAULT_MIN_LENGTH_RATIO = 0.75
#: trim 输出 / 原文 长度比上限(越界护栏:过长 = 加戏 / 没在 trim)。
DEFAULT_MAX_LENGTH_RATIO = 1.10

# tic 窄计正则(与实验 ``scripts/v2_w3_5_prose_ab_methods.py::_metrics`` butshi 口径一致)。
_BUTSHI_COMMA_RE = re.compile(r"不是[^，。、\n]{1,14}[，,、]\s*(?:而)?是")
_BUTSHI_PERIOD_RE = re.compile(r"不是[^，。、！？\n]{1,14}。\s*是[^，。、！？\n]")


class AntiTicTrimOutput(BaseModel):
    """anti-tic trim pass 结果(provider 无关)。

    ``trimmed=True`` → ``content`` 是采用的 trim 结果;``trimmed=False`` → ``content``
    是**回退的原文**(跳过 / 任何护栏触发 / 异常)。调用方据 ``trimmed`` 决定是否替换
    writer 出稿,``reason`` 供日志 / 测试断言。
    """

    content: str
    trimmed: bool
    reason: str
    model_used: str | None = None
    tic_before: int = 0
    tic_after: int = 0


def count_tics(text: str) -> int:
    """窄计「不是X是Y」tic 数(与 W3.5 实验 ``_metrics`` butshi 口径一致)。

    覆盖逗号变体(「不是 A,(而)是 B」)+ 句号拆分变体(「不是 A。是 B」)。
    不计广义对比框架(也不是/更像是/与其说…),那些是 morphing 变体、不作 gate 依据。
    """
    return len(_BUTSHI_COMMA_RE.findall(text)) + len(_BUTSHI_PERIOD_RE.findall(text))


def load_trim_prompt(prompts_dir: Path | None = None) -> str:
    """加载 ``prompts/anti_tic_trim.md`` 作 trim system prompt(单一真相源)。

    :param prompts_dir: 提示词目录;``None`` → ``settings.prompts_dir``。
    :raises FileNotFoundError: 文件不存在(调用方在更高层兜异常,不应静默)。
    """
    base = prompts_dir if prompts_dir is not None else settings.prompts_dir
    return load_prompt_template((base / _TRIM_PROMPT_FILENAME).resolve()).strip()


def trim_chapter_tics(
    *,
    content: str,
    router: ModelRouter | None = None,
    client: LLMClient | None = None,
    model: str | None = None,
    prompts_dir: Path | None = None,
    chapter_id: str | None = None,
    tic_threshold: int = DEFAULT_TIC_THRESHOLD,
    max_tokens_floor: int = DEFAULT_MAX_TOKENS_FLOOR,
    max_tokens_retry: int = DEFAULT_MAX_TOKENS_RETRY,
    min_length_ratio: float = DEFAULT_MIN_LENGTH_RATIO,
    max_length_ratio: float = DEFAULT_MAX_LENGTH_RATIO,
) -> AntiTicTrimOutput:
    """对一章正文做 anti-tic trim pass(独立 LLM 调用 + 三护栏)。

    路由复用 writer profile(``resolve_routing(agent_name="writer")``);不传
    ``chapter_card`` → 走基础 writer profile(不触发 finale/张力等 writer 规则,
    trim 始终用同一基础模型,与实验一致)。

    **任何护栏触发 / 异常 → 回退原文**(``trimmed=False``),绝不交残章 / 改坏干净章 /
    阻断生成。

    :param content: writer 出稿的单章正文。
    :param router: ``ModelRouter``(主路径);``None`` 时走 ``client`` + ``model`` legacy。
    :returns: ``AntiTicTrimOutput``;``trimmed`` 决定是否采用,``content`` 是采用值或原文。
    """
    tic_before = count_tics(content)

    # ── 护栏①:tic 密度 gate(pre-trim)────────────────────────────────────────
    if tic_before < tic_threshold:
        _log.info(
            "anti_tic_trim_skipped",
            chapter_id=chapter_id,
            reason="below_tic_threshold",
            tic_before=tic_before,
            threshold=tic_threshold,
        )
        return AntiTicTrimOutput(
            content=content,
            trimmed=False,
            reason="skipped_below_tic_threshold",
            tic_before=tic_before,
            tic_after=tic_before,
        )

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
            "anti_tic_trim_routing_failed", chapter_id=chapter_id, error=str(exc)
        )
        return AntiTicTrimOutput(
            content=content,
            trimmed=False,
            reason="fallback_error",
            tic_before=tic_before,
            tic_after=tic_before,
        )

    kwargs = dict(kwargs)
    profile_max_tokens = max(
        int(kwargs.get("max_tokens") or 0),
        int(kwargs.get("max_completion_tokens") or 0),
    )
    prepare_manifest_call = getattr(
        resolved_client,
        "prepare_bounded_non_thinking_call",
        None,
    )
    if not callable(prepare_manifest_call):
        if "enable_thinking" in kwargs:
            kwargs["enable_thinking"] = False
        if "preserve_thinking" in kwargs:
            kwargs["preserve_thinking"] = False
        kwargs.pop("thinking_budget", None)
        kwargs.pop("reasoning_effort", None)

    try:
        prompt = load_trim_prompt(prompts_dir)
    except Exception as exc:  # noqa: BLE001 — prompt 缺失回退原文,绝不阻断生成
        if isinstance(exc, HandoffError):
            raise
        _log.warning(
            "anti_tic_trim_prompt_load_failed", chapter_id=chapter_id, error=str(exc)
        )
        return AntiTicTrimOutput(
            content=content,
            trimmed=False,
            reason="fallback_error",
            tic_before=tic_before,
            tic_after=tic_before,
        )

    messages = [
        Message(role="system", content=prompt),
        Message(role="user", content=handoff_user_text(
            resolved_client, stage="anti_tic_trim", data={"content": content}, original_task=content,
        )),
    ]

    # max_tokens 预算:首跑 ≥floor(profile 8000 不够);重试严格大于首跑。
    first_max_tokens = max(profile_max_tokens, max_tokens_floor)
    retry_max_tokens = max(max_tokens_retry, first_max_tokens + 8000)

    def _complete(max_tokens: int) -> Any:
        if callable(prepare_manifest_call):
            call_kwargs = prepare_manifest_call(
                kwargs,
                max_completion_tokens=max_tokens,
            )
        else:
            call_kwargs = {**kwargs, "max_tokens": max_tokens}
        return resolved_client.complete(
            messages=messages, model=resolved_model, **call_kwargs
        )

    # ── 护栏②:截断(post-trim)────────────────────────────────────────────────
    try:
        resp = _complete(first_max_tokens)
        if resp.finish_reason == "length":
            _log.warning(
                "anti_tic_trim_truncated_retry",
                chapter_id=chapter_id,
                first_max_tokens=first_max_tokens,
                retry_max_tokens=retry_max_tokens,
            )
            resp = _complete(retry_max_tokens)
            if resp.finish_reason == "length":
                _log.warning(
                    "anti_tic_trim_truncated_fallback",
                    chapter_id=chapter_id,
                    retry_max_tokens=retry_max_tokens,
                )
                return AntiTicTrimOutput(
                    content=content,
                    trimmed=False,
                    reason="fallback_truncated",
                    model_used=resp.model_used,
                    tic_before=tic_before,
                    tic_after=tic_before,
                )
    except Exception as exc:  # noqa: BLE001 — LLM 调用失败回退原文,绝不阻断生成
        if isinstance(exc, HandoffError):
            raise
        _log.warning("anti_tic_trim_call_failed", chapter_id=chapter_id, error=str(exc))
        return AntiTicTrimOutput(
            content=content,
            trimmed=False,
            reason="fallback_error",
            tic_before=tic_before,
            tic_after=tic_before,
        )

    trimmed_text = resp.content or ""
    tic_after = count_tics(trimmed_text)

    # ── 护栏③:越界(post-trim)────────────────────────────────────────────────
    orig_len = len(content.strip())
    new_len = len(trimmed_text.strip())
    ratio = (new_len / orig_len) if orig_len else 0.0
    out_of_bounds = (
        new_len == 0
        or orig_len == 0
        or ratio < min_length_ratio
        or ratio > max_length_ratio
        or tic_after > tic_before
    )
    if out_of_bounds:
        _log.warning(
            "anti_tic_trim_out_of_bounds_fallback",
            chapter_id=chapter_id,
            ratio=round(ratio, 3),
            tic_before=tic_before,
            tic_after=tic_after,
        )
        return AntiTicTrimOutput(
            content=content,
            trimmed=False,
            reason="fallback_out_of_bounds",
            model_used=resp.model_used,
            tic_before=tic_before,
            tic_after=tic_after,
        )

    _log.info(
        "anti_tic_trim_applied",
        chapter_id=chapter_id,
        tic_before=tic_before,
        tic_after=tic_after,
        ratio=round(ratio, 3),
    )
    return AntiTicTrimOutput(
        content=trimmed_text,
        trimmed=True,
        reason="trimmed",
        model_used=resp.model_used,
        tic_before=tic_before,
        tic_after=tic_after,
    )
