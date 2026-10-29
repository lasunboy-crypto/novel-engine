"""衔接桥(ContinuityContext)抽取(P1 简单规则 + P2-B-12 LLM 路径)。

本模块提供两条抽取路径与一个统一入口:

1. **rule 路径(P1-B-09 简单规则)**:``extract_from_prev_text(prev_text)``
   只暴露 ``prev_chapter_tail`` + ``unfinished_actions`` 两字段,其余
   ``ContinuityContext`` 字段(``time_gap`` / ``scene_continuation`` /
   ``pov_*``)依赖章节卡上下文,**不在此处抽取**;由调用方与章节卡 /
   人工输入合并。
2. **llm 路径(P2-B-12 引入)**:``extract_continuity_with_llm(...)`` 调
   注入的 ``LLMClient`` 让 LLM 从上一章正文 + 当前章节卡上下文一并产出
   完整 ``ContinuityContext``(7 字段全部 LLM 给值,Pydantic 校验)。
3. **rule_plus_llm 路径(v1.1 K1 引入)**:``_build_rule_plus_llm(...)`` 叠加
   模式 —— rule 确定性保住 ``prev_chapter_tail`` 字面 + ``unfinished_actions``
   + ``pov_*``(卡基底),LLM **只增强** ``time_gap`` + ``scene_continuation``
   两个软字段;LLM 失败逐字段降级回卡值,字面 tail 永不丢。比 llm 路径更稳。
4. **统一入口**:``build_continuity_context(prev_content, current_card,
   *, strategy=None, ...)`` 按 ``Settings.continuity_strategy`` 分发。
   ``rule`` 走 P1 规则 + ``card.continuity_context`` 字段融合;``llm``
   走 LLM 抽全 7 字段;``rule_plus_llm`` 走 rule 保底 + LLM 软字段叠加。
"""

from __future__ import annotations

import json
import re
from functools import lru_cache
from pathlib import Path
from typing import TYPE_CHECKING, Literal, TypedDict

import structlog
from pydantic import ValidationError

from src.config import settings
from src.core.agents._prompt import (
    append_role_marker,
    load_prompt_template,
    resolve_routing,
)
from src.core.llm.base import Message
from src.core.models.chapter_card import ChapterCard, ContinuityContext

if TYPE_CHECKING:
    from src.core.llm.base import LLMClient
    from src.core.routing.router import ModelRouter

_log = structlog.get_logger(__name__)

# ---- P1 简单规则路径常量 ----------------------------------------------------

# 末尾抽取窗口大小(字符)。Python 切片在短输入时自动返回全文,无需额外判长。
TAIL_CHARS = 800

# 句末终止符模式:
# - 多字符:`……`(中文双省略号)、`…`(单字符省略号 U+2026)、`...`(三个 ASCII 点)
# - 单字符:`。` `!` `?`(全角与半角问号叹号都覆盖)
# Python `re` 的 lookbehind 只支持定宽,无法直接 lookbehind 多字符终止符,
# 因此改用 split + 捕获组:奇数位是终止符,偶数位是中间文本,后面再拼回。
# 故意**不**把英文单点 `.` 当终止符(中文小说里 `.` 几乎只出现在 `...` 内
# 部或 `e.g.` 缩写中,单独切句会误切)。
_SENTENCE_TERMINATOR_RE = re.compile(r"(……|…|\.\.\.|[。!?!?])")

# 进行时副词集合:出现在句子里则视为"动作未完成"候选信号。
_PROGRESSIVE_ADVERBS = ("还", "正", "仍", "尚未", "依然")

# 末尾省略号判定:句子去尾空白后以这些后缀结尾即视为未完成。
_ELLIPSIS_SUFFIXES = ("……", "…", "...")

# 扫描末尾的最大句子数(只看靠近末尾的几句)。
_TAIL_SENTENCE_WINDOW = 3


# ---- P2-B-12 LLM 路径常量 ---------------------------------------------------

_CONTINUITY_PROMPT_FILENAME = "continuity_extractor.md"

# Continuity 抽取默认生成参数:抽取求稳,温度低;输出只装一份 7 字段 JSON,
# 不需要长文本。
_DEFAULT_TEMPERATURE = 0.2
_DEFAULT_MAX_TOKENS = 2000

# 复用 reviewer / memory_extractor 同款"代码块包裹"剥离正则——LLM 偶发把
# JSON 套进 ```json ... ``` 即使 prompt 禁止。
# 注:本模块是该模式第三次出现(reviewer / memory_extractor / 此处),
# Rule of Three 留观——下个 agent 再现 JSON 解析时即触发抽到 ``_prompt.py``
# 公共 ``strip_format_noise(text) -> str`` 助手。本任务先复制保持任务粒度。
_CODE_FENCE_RE = re.compile(
    r"^\s*```(?:json)?\s*\n(?P<body>.*?)\n```\s*$",
    re.DOTALL,
)
# 末尾 HTML 注释剥离(防 LLM 复读 marker 到输出)。
_TRAILING_HTML_COMMENT_RE = re.compile(
    r"\s*<!--.*?-->\s*$",
    re.DOTALL,
)


class ContinuityExtraction(TypedDict):
    """``extract_from_prev_text`` 的返回结构(P1 路径)。

    运行时是普通 dict,IDE / mypy 借此校验字段名拼写。
    """

    prev_chapter_tail: str
    unfinished_actions: list[str]


class ContinuityExtractorError(ValueError):
    """LLM 输出无法解析为合法 ``ContinuityContext`` JSON 时抛出(P2-B-12)。

    保留原始输出 + 实际模型名,便于 caller 重试决策与 P7 调优时回查根因
    (形态对齐 ``ReviewerParseError`` / ``MemoryExtractorError``)。
    """

    def __init__(self, message: str, *, raw_output: str, model_used: str) -> None:
        super().__init__(message)
        self.raw_output = raw_output
        self.model_used = model_used


# ---- P1 简单规则路径(保持原 API,P2-B-12 不动) -----------------------------


def extract_from_prev_text(prev_text: str) -> ContinuityExtraction:
    """从上一章正文中抽取衔接桥可诚实得到的字段(P1-B-09 简单规则版)。

    :param prev_text: 上一章完整正文。空 / 全空白时返回空字段(首章场景)。
    :return: 含 ``prev_chapter_tail`` 与 ``unfinished_actions`` 的 dict。
    """
    if not prev_text or not prev_text.strip():
        return {"prev_chapter_tail": "", "unfinished_actions": []}

    tail = prev_text[-TAIL_CHARS:]
    unfinished = _detect_unfinished_actions(tail)
    return {"prev_chapter_tail": tail, "unfinished_actions": unfinished}


def _detect_unfinished_actions(tail: str) -> list[str]:
    """扫描末尾最后几句,挑出"未完成动作"候选。

    命中规则(任一即视为候选):
    1. 句子(去尾空白后)以省略号结尾:`……` / `…` / `...`
    2. 句子含进行时副词:还 / 正 / 仍 / 尚未 / 依然
    """
    sentences = _split_sentences(tail)
    candidates = sentences[-_TAIL_SENTENCE_WINDOW:]

    matched: list[str] = []
    for sent in candidates:
        if sent.endswith(_ELLIPSIS_SUFFIXES) or any(
            adv in sent for adv in _PROGRESSIVE_ADVERBS
        ):
            matched.append(sent)
    return matched


def _split_sentences(text: str) -> list[str]:
    """按句末终止符切句,且保留终止符在句尾(以便末尾省略号判定)。

    ``re.split`` + 捕获组返回 ``[seg0, term0, seg1, term1, ..., tail]``;
    将每个 segment 与其后续终止符拼回,得到完整句子。最后空白与空串被丢掉。
    """
    parts = _SENTENCE_TERMINATOR_RE.split(text)
    sentences: list[str] = []
    buffer = ""
    for index, part in enumerate(parts):
        if index % 2 == 0:
            # 段内文本(终止符之间)。
            buffer = part
        else:
            # 终止符:与之前 buffer 拼成一句完整句。
            sent = (buffer + part).strip()
            if sent:
                sentences.append(sent)
            buffer = ""
    # 末尾如果有未跟终止符的残段(如全文不带句末标点),也算一句。
    tail_remainder = buffer.strip()
    if tail_remainder:
        sentences.append(tail_remainder)
    return sentences


# ---- P2-B-12 LLM 抽取路径 ---------------------------------------------------


def extract_continuity_with_llm(
    *,
    prev_content: str,
    current_card: ChapterCard,
    router: ModelRouter | None = None,
    client: LLMClient | None = None,
    model: str | None = None,
    max_tokens: int = _DEFAULT_MAX_TOKENS,
    temperature: float = _DEFAULT_TEMPERATURE,
    prompts_dir: Path | None = None,
) -> ContinuityContext:
    """LLM 抽取完整 ``ContinuityContext``(P2-B-12;P3-B-06 加 router 路径)。

    流程:
    1. 加载 ``prompts/continuity_extractor.md`` system 提示词。
    2. 拼装 user 消息(上一章正文 + 当前章节卡上下文 + extractor marker)。
    3. 通过 ``resolve_routing`` 解析 router / legacy 二选一 → 调
       ``client.complete(...)`` 拿 LLM 响应。
    4. 剥代码块 / trailing 注释 → ``json.loads`` → ``ContinuityContext``
       直接 Pydantic 校验。任一失败抛 ``ContinuityExtractorError``。

    :param prev_content: 上一章完整正文;空字符串视为首章场景,LLM 按
        prompt"首章兜底"规则填值。
    :param current_card: 当前章节卡;Extractor 用 ``chapter_id`` /
        ``chapter_number`` / ``pov_character`` / ``setting`` +
        ``continuity_context.pov_from`` 透传给 LLM 当对照基准。
    :param router: P3-B-06 主路径。``ModelRouter`` 实例;给定时按
        ``router.route("continuity")`` 取 client + model + 生成参数。
    :param client: legacy 路径 ``LLMClient`` 协议实例;``router=None`` 时必传。
    :param model: legacy 路径模型名;``router=None`` 时必传。实际
        ``model_used`` 取响应里的字段。
    :param max_tokens: legacy 路径输出 token 上限,默认 2000;``router`` 路径
        下由 profile 决定。
    :param temperature: legacy 路径采样温度,默认 0.2;``router`` 路径下由
        profile 决定。
    :param prompts_dir: 提示词模板根目录;``None`` 时取 ``settings.prompts_dir``。
    :raises ContinuityExtractorError: LLM 输出非合法 JSON / 字段缺失 /
        类型错。
    :raises TypeError: ``router=None`` 且 ``client`` / ``model`` 任一缺失。
    :return: 完整 ``ContinuityContext``(7 字段全部 LLM 给值)。
    """
    resolved_client, resolved_model, complete_kwargs = resolve_routing(
        agent_name="continuity",
        router=router,
        client=client,
        model=model,
        legacy_kwargs={"max_tokens": max_tokens, "temperature": temperature},
    )

    template_root = (
        prompts_dir if prompts_dir is not None else settings.prompts_dir
    ).resolve()
    system_text = load_prompt_template(template_root / _CONTINUITY_PROMPT_FILENAME)
    user_text = _build_user_message(
        prev_content=prev_content, current_card=current_card
    )

    response = resolved_client.complete(
        messages=[
            Message(role="system", content=system_text),
            Message(role="user", content=user_text),
        ],
        model=resolved_model,
        **complete_kwargs,
    )

    return _parse_llm_payload(
        raw_text=response.content,
        model_used=response.model_used,
        chapter_id=current_card.chapter_id,
    )


def _build_user_message(
    *, prev_content: str, current_card: ChapterCard
) -> str:
    """user 消息:上一章正文 + 当前章节卡上下文(供 LLM 对照基准 + 兜底规则)。

    ``prev_pov_character`` 取自 ``card.continuity_context.pov_from`` 手填值
    ——LLM 用它直接判断 ``pov_change``(避免 LLM 自行从正文猜上一章视角
    时漂移)。``prev_setting`` 不传字段,由 LLM 从 ``prev_content`` 末尾
    自行推断(prompt"输入说明"段已对齐)。

    首章场景(``card.continuity_context.pov_from is None``)用占位符
    ``"(无,本章为首章)"``,与 prompt"首章兜底"规则呼应。

    末尾追加 P1-B-22 协议的 extractor marker(MockLLMClient extractor 分支
    当前抛 ``NotImplementedError``;本模块单测请用独立 fake LLM client 直
    接返预设 JSON,不走 MockLLMClient)。
    """
    base = current_card.continuity_context
    prev_pov_block = (
        base.pov_from if base.pov_from is not None else "(无,本章为首章)"
    )
    context_block = (
        "## 当前章节卡上下文\n\n"
        f"- 章节 ID(chapter_id):{current_card.chapter_id}\n"
        f"- 卷内章序号(chapter_number):{current_card.chapter_number}\n"
        f"- 标题:{current_card.title}\n"
        f"- 本章视角(pov_character):{current_card.pov_character}\n"
        f"- 本章场景(setting):{current_card.setting}\n"
        f"- 上一章视角(prev_pov_character):{prev_pov_block}\n"
    )
    prev_block = (
        "## 上一章正文\n\n"
        + (prev_content if prev_content.strip() else "(本章为首章,无前章正文)")
    )
    return append_role_marker(
        f"{context_block}\n\n{prev_block}",
        role="extractor",
    )


def _parse_llm_payload(
    *, raw_text: str, model_used: str, chapter_id: str
) -> ContinuityContext:
    """剥代码块 + trailing → ``json.loads`` → ``ContinuityContext`` 校验。

    任一失败抛 ``ContinuityExtractorError`` 并落
    ``continuity_extractor_parse_failed`` 事件。
    """
    text = _strip_format_noise(raw_text).strip()

    try:
        data = json.loads(text)
    except json.JSONDecodeError as exc:
        _log.error(
            "continuity_extractor_parse_failed",
            reason="invalid_json",
            raw_output=raw_text,
            model_used=model_used,
            chapter_id=chapter_id,
            json_error=str(exc),
        )
        raise ContinuityExtractorError(
            "ContinuityExtractor LLM 输出非合法 JSON",
            raw_output=raw_text,
            model_used=model_used,
        ) from exc

    try:
        return ContinuityContext.model_validate(data)
    except ValidationError as exc:
        _log.error(
            "continuity_extractor_parse_failed",
            reason="schema_mismatch",
            raw_output=raw_text,
            model_used=model_used,
            chapter_id=chapter_id,
            validation_errors=exc.errors(),
        )
        raise ContinuityExtractorError(
            "ContinuityExtractor LLM 输出 JSON 结构与 ContinuityContext schema 不符",
            raw_output=raw_text,
            model_used=model_used,
        ) from exc


def _strip_format_noise(text: str) -> str:
    """剥 trailing HTML 注释 + ``` / ```json 包裹(沿用 reviewer / memory_extractor 路径)。

    顺序:**先剥 trailing**(可能多个),**再剥 fence**——若先剥 fence,
    fence 正则要求整字符串末尾为 ``` ,trailing 注释会让 fence 不匹配。
    无杂质时原样返回。

    Rule of Three 留观:本函数与 ``reviewer._strip_code_fence`` /
    ``memory_extractor._strip_format_noise`` 字面一致(三处复制)。下个
    agent 再现 JSON 解析时即触发抽到 ``src/core/agents/_prompt.py``
    公共助手。本任务先保持复制以控任务粒度。
    """
    while True:
        stripped = _TRAILING_HTML_COMMENT_RE.sub("", text)
        if stripped == text:
            break
        text = stripped
    match = _CODE_FENCE_RE.match(text)
    if match is not None:
        return match.group("body")
    return text


# ---- P2-B-12 统一入口 -------------------------------------------------------


@lru_cache(maxsize=1)
def _resolved_default_strategy() -> Literal["rule", "llm", "rule_plus_llm"]:
    """读 ``settings.continuity_strategy`` 并缓存。

    ``settings`` 单例 import 时即定型,缓存一次省去后续重复属性访问。单测
    可通过 ``monkeypatch.setattr(settings, "continuity_strategy", "rule")``
    + ``_resolved_default_strategy.cache_clear()`` 切换。
    """
    return settings.continuity_strategy


def build_continuity_context(
    *,
    prev_content: str,
    current_card: ChapterCard,
    strategy: Literal["rule", "llm", "rule_plus_llm"] | None = None,
    router: ModelRouter | None = None,
    client: LLMClient | None = None,
    model: str | None = None,
    max_tokens: int = _DEFAULT_MAX_TOKENS,
    temperature: float = _DEFAULT_TEMPERATURE,
    prompts_dir: Path | None = None,
) -> ContinuityContext:
    """衔接桥抽取统一入口(P2-B-12;v1.1 K1 加 ``rule_plus_llm`` 第三档)。

    按 ``strategy`` 分发到 rule / llm / rule_plus_llm 路径;``strategy=None``
    时取 ``settings.continuity_strategy``。

    rule 路径
    ---------
    1. 调 ``extract_from_prev_text(prev_content)`` 抽 ``prev_chapter_tail``
       + ``unfinished_actions`` 两字段。
    2. 用 ``current_card.continuity_context`` 作基底
       (``time_gap`` / ``scene_continuation`` / ``pov_*`` 沿用手填值),
       ``model_copy(update=...)`` 覆盖上述两字段,得到完整
       ``ContinuityContext``。
    3. 不调 LLM,``client`` / ``model`` 参数被忽略。

    llm 路径
    --------
    1. 调 ``extract_continuity_with_llm(...)`` 让 LLM 一并产出 7 字段
       (含 ``prev_chapter_tail`` —— LLM 会把上一章末尾改写后塞回)。
    2. ``router`` 或 ``client`` + ``model`` 二者其一必传,缺则抛 ``ValueError``。

    rule_plus_llm 路径(v1.1 K1 叠加模式)
    -------------------------------------
    **rule 确定性字段 + LLM 增强软字段,字面 tail 永不被 LLM 覆盖**:

    1. rule 确定性产出 ``prev_chapter_tail``(字面 800 字)+
       ``unfinished_actions``(信号检测打底);卡基底产出 ``pov_from`` /
       ``pov_to`` / ``pov_change``(均不经 LLM)。
    2. 调 ``extract_continuity_with_llm(...)`` 只采纳其 ``time_gap`` +
       ``scene_continuation`` 两个需语义理解、Writer 确实消费的软字段
       (writer.py ``_format_continuity`` 注入这两字段)。
    3. **逐字段降级**:LLM 调用失败(``ContinuityExtractorError`` / 网络 /
       任意异常)→ ``time_gap`` + ``scene_continuation`` 退回卡基底手填值,
       字面 tail 与 pov_* 仍保底,**不冒泡**(对齐 rule 路径的"总有真值"保证)。
       因此比 llm 路径"全有或全无"更稳。
    4. ``router`` 或 ``client`` + ``model`` 二者其一必传(配置错误 fail loud,
       区别于步骤 3 的运行时降级)。

    :param prev_content: 上一章完整正文;空 / 全空白视为首章场景。
    :param current_card: 当前章节卡;rule 路径用其 ``continuity_context``
        基底字段,llm 路径用其 ``chapter_id`` / ``pov_character`` / ``setting``
        + ``continuity_context.pov_from`` 等做对照基准。
    :param strategy: 显式指定 ``"rule"`` / ``"llm"`` / ``"rule_plus_llm"``;
        ``None`` 时按 ``settings.continuity_strategy`` 分发。
    :param client: ``strategy="llm"`` / ``"rule_plus_llm"`` 时与 ``router``
        二选一必传(``LLMClient`` 协议实例)。
    :param model: ``strategy="llm"`` / ``"rule_plus_llm"`` 时与 ``router``
        二选一必传。
    :param max_tokens / temperature / prompts_dir: 仅 llm / rule_plus_llm
        路径用,rule 忽略。
    :return: 完整 ``ContinuityContext``。
    :raises ValueError: ``strategy`` 含 LLM 但 ``router`` / ``client`` /
        ``model`` 均缺失(配置错误)。
    :raises ContinuityExtractorError: ``strategy="llm"`` 下 LLM 输出无法解析
        (``rule_plus_llm`` 下该错被捕获并降级,不冒泡)。
    """
    chosen = strategy if strategy is not None else _resolved_default_strategy()
    if chosen == "llm":
        # P3-B-06:router 给定 → 走 router 路径(无需 client + model);否则
        # 沿用 P2-B-12 legacy 校验,要求 client + model 同时给定。
        if router is None and (client is None or model is None):
            raise ValueError(
                "build_continuity_context: strategy='llm' 需提供 router 或 "
                "client + model 二者其一"
            )
        return extract_continuity_with_llm(
            router=router,
            client=client,
            prev_content=prev_content,
            current_card=current_card,
            model=model,
            max_tokens=max_tokens,
            temperature=temperature,
            prompts_dir=prompts_dir,
        )

    if chosen == "rule_plus_llm":
        return _build_rule_plus_llm(
            prev_content=prev_content,
            current_card=current_card,
            router=router,
            client=client,
            model=model,
            max_tokens=max_tokens,
            temperature=temperature,
            prompts_dir=prompts_dir,
        )

    # rule 路径
    extracted = extract_from_prev_text(prev_content)
    base = current_card.continuity_context
    return base.model_copy(
        update={
            "prev_chapter_tail": extracted["prev_chapter_tail"],
            "unfinished_actions": extracted["unfinished_actions"],
        }
    )


#: rule_plus_llm 模式下从 LLM 抽取结果里**只**采纳的软字段集。其余 5 字段
#: (prev_chapter_tail / unfinished_actions / pov_from / pov_to / pov_change)
#: 由 rule + 卡确定性产出,LLM 永不覆盖。选这两字段的依据:① Writer
#: ``_format_continuity`` 确实把它们注入 prompt(writer.py:722-723);② rule
#: 路径对这两字段只能沿用卡占位值,LLM 是真增量。情绪基调等新软字段需先扩
#: ``ContinuityContext`` schema(碰 continuity_validator + G10 数据源),留 K1
#: follow-up 待 B1 长跑实证后评估(candidates §K1)。
_LLM_ENHANCED_SOFT_FIELDS = ("time_gap", "scene_continuation")


def _build_rule_plus_llm(
    *,
    prev_content: str,
    current_card: ChapterCard,
    router: ModelRouter | None,
    client: LLMClient | None,
    model: str | None,
    max_tokens: int,
    temperature: float,
    prompts_dir: Path | None,
) -> ContinuityContext:
    """rule_plus_llm 叠加:rule 保字面 tail + LLM 只增强软字段 + 逐字段降级。

    见 ``build_continuity_context`` docstring "rule_plus_llm 路径"段。配置错误
    (router / client+model 均缺)fail loud;运行时 LLM 失败降级保底。
    """
    if router is None and (client is None or model is None):
        raise ValueError(
            "build_continuity_context: strategy='rule_plus_llm' 需提供 router "
            "或 client + model 二者其一"
        )

    # rule + 卡确定性字段:字面 tail + unfinished_actions + pov_* 永远保底。
    extracted = extract_from_prev_text(prev_content)
    base = current_card.continuity_context
    deterministic_update = {
        "prev_chapter_tail": extracted["prev_chapter_tail"],
        "unfinished_actions": extracted["unfinished_actions"],
    }

    # LLM 增强软字段:成功则采纳 time_gap + scene_continuation,失败逐字段降级
    # 到卡基底手填值(不冒泡,保证 Writer 总拿到字面 tail)。
    try:
        llm_ctx = extract_continuity_with_llm(
            router=router,
            client=client,
            prev_content=prev_content,
            current_card=current_card,
            model=model,
            max_tokens=max_tokens,
            temperature=temperature,
            prompts_dir=prompts_dir,
        )
    except Exception as exc:  # noqa: BLE001 — rule_plus_llm 总有 rule 保底
        _log.warning(
            "continuity_rule_plus_llm_enhance_failed_degrade",
            chapter_id=current_card.chapter_id,
            chapter_number=current_card.chapter_number,
            error_type=type(exc).__name__,
            error=str(exc),
            degraded_fields=list(_LLM_ENHANCED_SOFT_FIELDS),
        )
        return base.model_copy(update=deterministic_update)

    soft_update = {
        field: getattr(llm_ctx, field) for field in _LLM_ENHANCED_SOFT_FIELDS
    }
    return base.model_copy(update={**deterministic_update, **soft_update})
