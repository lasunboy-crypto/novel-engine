"""cross_chapter_dedup 正文跨章去重 validator(v1.1 wave 6 session 4,B4 核心)。

职责:在 writer **写完正文后**(审稿期),用**代码相似度初筛**找出本章正文与近
N 章正文的疑似重复片段,再用**一次批量 LLM 调用**判每条"注水复读(抓)"还是
"有意回扣 / 钩子 / 母题呼应(护)"。治 B1 30 章长跑(luo-clean-v2)暴露的 D 类
注水套路 + B 类自我重复:

- **B4(B 类)** ch-25 末 / ch-26 玉佩转凉事件逐句重写(自我重复)
- **D1** 战斗同质化("破空"解围套路 10×/8 章)
- **D2** 身体描写复读(撕裂 62×、右膝 28×、钝痛 20×)
- **D3** 反派独白套路(进化 13×、宿敌 13×)

约束分层(§0 原则二)
====================

**代码初筛(旁移到代码)+ LLM 判意图**:相似度计算是结构化 + 跨书通用判据 →
纯代码(便宜、稳),不进 LLM;"是注水还是有意回扣"需懂语义 / 意图 / 护留白 →
LLM-judge。candidates 为空 → **0 次 LLM 调用**(对齐 thread_closure / must_achieve
批处理省 token 范本)。writer 正文生成完全不加负担(下游兜底)。

与 D4(卡级 must_achieve)分工
=============================

- **must_achieve_validator**(卡级):保证**章节卡列的必达事件**达成,管"卡级
  剧情是否推进",**不看正文文本相似度**。
- **本 agent**(正文级):管"**正文文本**是否跨章逐句重写 / 高频复读",**不看
  卡级必达事件**。两者判据正交,不重复造。

全 SOFT / WARN-only(对齐 wave 6 全 WARN 默认)
==============================================

caller(``chapter_orchestrator._apply_cross_chapter_dedup_validator``)把判为注水
的条目合并进 ``review.suggestions``,**不覆盖 verdict**。**为何不 HARD**:"重复
是注水还是有意回扣"是文学语义判断,LLM-judge 可能误把母题呼应 / 章末钩子 / 有意
对照当注水(§0 第一原则"宁可漏报也不误伤留白 / 有意手法")。HARD 升级留 wave 6 末
约束预算实测后定。

设计要点(沿用 setting_consistency_validator / thread_closure_validator 同型架构)
============================================================================

1. **代码初筛 + 1 次批量 LLM 调用**:全部候选片段一次性喂 judge 综合判定。
2. **沿用 *_validator 路由**:agent_name ``"cross_chapter_dedup_validator"`` →
   ``agent_routing.cross_chapter_dedup_validator``(config.yaml,cloud_qwen_max_thinking);
   未显式配则 ``resolve_routing`` fallback 走 reviewer profile。
3. **LLM 失败 fail-open**:LLM 抛异常 / 非法 JSON / setup 失败 → 全部候选按
   "有意回扣"处理(不产 violation;§0 宁可漏报不误伤)。
4. **skip 边界**:无前序章节 / 本章正文为空 / 初筛 0 候选 → skip(0 LLM 调用)。
5. **agent_role marker = ``cross_chapter_dedup_validator``**:MockLLMClient 识别
   marker 返默认"全部 intentional"JSON,既有审稿 happy 路径不回归。
"""

from __future__ import annotations

import json
import re
from collections import Counter
from dataclasses import dataclass, field
from pathlib import Path
from typing import Literal

import structlog
from pydantic import BaseModel, Field, ValidationError

from src.config import settings
from src.core.agents._json_contract import loads_with_quote_repair
from src.core.agents._prompt import (
    append_role_marker,
    load_prompt_template,
    resolve_routing,
)
from src.core.agents._structured_output import prepare_strict_json_output
from src.core.llm.base import LLMClient, Message
from src.core.routing.router import ModelRouter

_log = structlog.get_logger(__name__)

_VALIDATOR_PROMPT_FILENAME = "cross_chapter_dedup_validator_v1.md"

# 默认生成参数:综合判定对采样稳定性敏感,温度低;批量输出含 N 个 {verdict + str}。
_DEFAULT_TEMPERATURE = 0.3
_DEFAULT_MAX_TOKENS = 1200

# === 代码初筛参数(自决项,保守取值;WARN-only + LLM 判意图兜底,宁缺毋滥)===

# 回看窗口:只与最近 N 章比对(控成本 + 重复在临近章最刺眼;全书级 motif 频次
# 留 wave 6 末长跑实测后决定是否扩)。
_LOOKBACK_CHAPTERS = 5

# 句级近重复:字符 shingle 长度 + Jaccard 阈值 + 最短句长(过短句误报多,跳过)。
_SHINGLE_N = 5
_PASSAGE_JACCARD_THRESHOLD = 0.50
_MIN_SENTENCE_CHARS = 14

# 高频短语:n-gram 长度 + 跨窗口最小出现次数 + 最少出现章数(只在 1 章内重复
# 不算跨章复读)。5-gram 比 2 字词(撕裂 / 破空)噪声低得多,抓的是"成句的套话"。
_PHRASE_N = 5
_PHRASE_MIN_COUNT = 4
_PHRASE_MIN_CHAPTERS = 2

# 候选上限(控 LLM 输入;超出按 signal 排序取 top,丢弃的 log 出来不静默)。
_MAX_PASSAGE_CANDIDATES = 8
_MAX_PHRASE_CANDIDATES = 6
_EXCERPT_MAX_CHARS = 80

_SENTENCE_SPLIT_RE = re.compile(r"[。!?！？;；\n]+")
_WHITESPACE_RE = re.compile(r"\s+")

_CODE_FENCE_RE = re.compile(
    r"^\s*```(?:json)?\s*\n(?P<body>.*?)\n```\s*$",
    re.DOTALL,
)
_TRAILING_HTML_COMMENT_RE = re.compile(
    r"\s*<!--.*?-->\s*$",
    re.DOTALL,
)

DedupVerdict = Literal["padding", "intentional"]
DedupKind = Literal["passage", "phrase"]


@dataclass(frozen=True)
class PriorChapter:
    """单个前序章节的正文(供初筛比对)。"""

    chapter_id: str
    text: str


@dataclass(frozen=True)
class DedupCandidate:
    """初筛命中的一条候选重复(待 LLM 判意图)。

    - ``kind="passage"``:本章某句与某前章某句近重复(``signal`` = Jaccard 相似度)。
    - ``kind="phrase"``:某短语在窗口内高频出现(``signal`` = 出现次数)。
    """

    kind: DedupKind
    current_excerpt: str
    prior_excerpt: str
    prior_chapter_id: str
    signal: float


@dataclass(frozen=True)
class DedupResult:
    """validator 输出。

    - ``passed``:``violations`` 为空时 True。LLM / setup 失败 / skip → True(fail-open)。
      **注意**:caller 把 violations 作 SOFT 处置(合并 suggestions),``passed=False``
      不覆盖 verdict(wave 6 全 WARN 默认)。
    - ``violations``:LLM 判为注水复读的条目(**人类可读完整句**,含套话 + "近 N 章
      出现 M 次" 计数 + LLM 理由,作 SOFT suggestions 合并;**非裸短语,且每章渲染串
      不同 → 下游不要拿它当跨章累计 key**)。
    - ``padding_phrases``:v1.1 wave 5 session 1(M1)新增。**裸套话短语**(仅 ``phrase``
      类 padding 候选的 ``current_excerpt``,如「口地喘着粗」5-gram;``passage`` 类整句
      近重复**不入** —— 整句不是可规避的"套话")。供 M1 动态卡控制器**跨章累计**的稳定
      key:同一套话跨章恒定(``violations`` 渲染串嵌了章节相关计数 → 不能作累计 key)。
    - ``candidates_count``:初筛命中候选总数(含被判为有意回扣的)。
    - ``llm_calls_count``:实际 LLM 调用次数;skip / 0 候选 → 0;正常 / fail-open → 1。
    - ``skipped``:无前序章 / 正文空 / 0 候选 → True。
    - ``fail_open``:LLM / setup 异常导致 fail-open(passed=True)时 True。
    """

    passed: bool
    violations: list[str] = field(default_factory=list)
    padding_phrases: list[str] = field(default_factory=list)
    candidates_count: int = 0
    llm_calls_count: int = 0
    skipped: bool = False
    fail_open: bool = False


class _DedupItemPayload(BaseModel):
    """LLM 输出的单条结论(批量 results 数组元素)。"""

    index: int = Field(..., ge=1, description="对应候选列表 1-based 序号")
    verdict: DedupVerdict
    reason: str = Field(..., min_length=1, max_length=300)


class _DedupBatchPayload(BaseModel):
    """LLM 输出的批量中间模型。"""

    results: list[_DedupItemPayload] = Field(default_factory=list)


# ====================== 纯代码初筛(无 LLM)======================


def _normalize(text: str) -> str:
    return _WHITESPACE_RE.sub("", text)


def _ngram_list(text: str, n: int) -> list[str]:
    """字符 n-gram 列表(保留重复,供频次统计)。"""
    t = _normalize(text)
    if len(t) < n:
        return [t] if t else []
    return [t[i : i + n] for i in range(len(t) - n + 1)]


def _shingles(text: str, n: int) -> set[str]:
    """字符 n-gram 集合(去重,供 Jaccard)。"""
    return set(_ngram_list(text, n))


def _jaccard(a: set[str], b: set[str]) -> float:
    if not a or not b:
        return 0.0
    inter = len(a & b)
    if inter == 0:
        return 0.0
    return inter / len(a | b)


def _split_sentences(text: str) -> list[str]:
    return [s.strip() for s in _SENTENCE_SPLIT_RE.split(text) if s.strip()]


def _passage_candidates(
    *, current_text: str, prior_chapters: list[PriorChapter]
) -> list[DedupCandidate]:
    """句级近重复:本章每句 vs 前章每句 Jaccard,超阈值 → 候选(治玉佩逐句重写)。"""
    # 预算前章句 shingle(只留够长的句,过短句噪声大)。
    prior_sents: list[tuple[str, str, set[str]]] = []  # (chapter_id, sent, shingles)
    for pc in prior_chapters:
        for sent in _split_sentences(pc.text):
            if len(_normalize(sent)) < _MIN_SENTENCE_CHARS:
                continue
            prior_sents.append((pc.chapter_id, sent, _shingles(sent, _SHINGLE_N)))
    if not prior_sents:
        return []

    candidates: list[DedupCandidate] = []
    seen_current: set[str] = set()
    for sent in _split_sentences(current_text):
        norm = _normalize(sent)
        if len(norm) < _MIN_SENTENCE_CHARS or norm in seen_current:
            continue
        sh = _shingles(sent, _SHINGLE_N)
        best_sim, best_cid, best_sent = 0.0, "", ""
        for cid, psent, psh in prior_sents:
            sim = _jaccard(sh, psh)
            if sim > best_sim:
                best_sim, best_cid, best_sent = sim, cid, psent
        if best_sim >= _PASSAGE_JACCARD_THRESHOLD:
            seen_current.add(norm)
            candidates.append(
                DedupCandidate(
                    kind="passage",
                    current_excerpt=sent[:_EXCERPT_MAX_CHARS],
                    prior_excerpt=best_sent[:_EXCERPT_MAX_CHARS],
                    prior_chapter_id=best_cid,
                    signal=round(best_sim, 3),
                )
            )
    candidates.sort(key=lambda c: c.signal, reverse=True)
    return candidates


def _phrase_candidates(
    *, current_text: str, prior_chapters: list[PriorChapter]
) -> list[DedupCandidate]:
    """高频短语:n-gram 跨窗口出现 ≥ 阈值且跨 ≥2 章 → 候选(治破空/撕裂/进化套话)。"""
    sources = [("__current__", current_text)] + [
        (pc.chapter_id, pc.text) for pc in prior_chapters
    ]
    total_count: Counter[str] = Counter()
    chapters_with: dict[str, set[str]] = {}
    for cid, text in sources:
        grams = _ngram_list(text, _PHRASE_N)
        total_count.update(grams)
        for g in set(grams):
            chapters_with.setdefault(g, set()).add(cid)

    current_grams = _shingles(current_text, _PHRASE_N)
    flagged: list[tuple[str, int]] = []
    for g in current_grams:
        cnt = total_count[g]
        if (
            cnt >= _PHRASE_MIN_COUNT
            and len(chapters_with.get(g, set())) >= _PHRASE_MIN_CHAPTERS
        ):
            flagged.append((g, cnt))
    # 确定性排序(count 降 + 字面升):set 迭代序受 hash-seed 影响,排序后聚类
    # 代表选取稳定可复现(避免 flaky)。
    flagged.sort(key=lambda x: (-x[1], x[0]))

    # 同一套话("一道破空之声传来")会触发一串首尾偏移的 5-gram,首尾两端共享字
    # 不足 → 用并查集按"共享 ≥ n-2 连续字"做传递性聚类,每簇只留 count 最高的代表。
    clustered = _cluster_phrases(flagged)
    clustered.sort(key=lambda x: (-x[1], x[0]))

    return [
        DedupCandidate(
            kind="phrase",
            current_excerpt=g,
            prior_excerpt="",
            prior_chapter_id="(窗口内多章)",
            signal=float(cnt),
        )
        for g, cnt in clustered
    ]


def _shares_run(a: str, b: str) -> bool:
    """两 n-gram 是否共享 ≥ n-2 连续字(同一套话的相邻 / 偏移窗口判定)。"""
    if a == b:
        return True
    k = _PHRASE_N - 2
    return any(a[i : i + k] in b for i in range(len(a) - k + 1))


def _cluster_phrases(flagged: list[tuple[str, int]]) -> list[tuple[str, int]]:
    """并查集:把"共享 ≥ n-2 连续字"的 n-gram 归一簇,每簇留 count 最高代表。

    传递性聚类(链式相连的首尾 n-gram 也归一簇),避免同一套话产一串候选。
    """
    n = len(flagged)
    if n <= 1:
        return list(flagged)
    parent = list(range(n))

    def find(x: int) -> int:
        while parent[x] != x:
            parent[x] = parent[parent[x]]
            x = parent[x]
        return x

    for i in range(n):
        for j in range(i + 1, n):
            if _shares_run(flagged[i][0], flagged[j][0]):
                parent[find(i)] = find(j)

    best_by_root: dict[int, tuple[str, int]] = {}
    for idx, (g, c) in enumerate(flagged):
        root = find(idx)
        cur = best_by_root.get(root)
        # tie-break 确定性:count 高优先,count 相等取字面小的(避免 hash-seed flaky)。
        if cur is None or c > cur[1] or (c == cur[1] and g < cur[0]):
            best_by_root[root] = (g, c)
    return list(best_by_root.values())


def prescreen_duplication(
    *, current_text: str, prior_chapters: list[PriorChapter]
) -> list[DedupCandidate]:
    """纯代码初筛:返回句级近重复 + 高频短语候选(无 LLM)。

    超候选上限时按 signal 排序取 top,丢弃数量由 caller log(不静默截断)。
    """
    passages = _passage_candidates(
        current_text=current_text, prior_chapters=prior_chapters
    )
    phrases = _phrase_candidates(
        current_text=current_text, prior_chapters=prior_chapters
    )
    return passages[:_MAX_PASSAGE_CANDIDATES] + phrases[:_MAX_PHRASE_CANDIDATES]


# ====================== LLM 判意图 ======================


def validate_cross_chapter_dedup(
    *,
    current_text: str,
    prior_chapters: list[PriorChapter],
    chapter_id: str,
    workspace_id: str | None = None,
    router: ModelRouter | None = None,
    client: LLMClient | None = None,
    model: str | None = None,
    max_tokens: int = _DEFAULT_MAX_TOKENS,
    temperature: float = _DEFAULT_TEMPERATURE,
    prompts_dir: Path | None = None,
) -> DedupResult:
    """代码初筛 + 1 次批量 LLM 调用,判每条候选是注水复读还是有意回扣。

    :param current_text: 本章正文。
    :param prior_chapters: 近 N 章正文(``PriorChapter`` list,空 → skip)。
    :param chapter_id: 章节 ID,落 structlog 字段。
    :param workspace_id: 工作区 ID,落 structlog 字段(可选)。
    :param router: ``ModelRouter`` 实例;给定时按
        ``router.route("cross_chapter_dedup_validator")`` 取 client + model + 生成参数。
    :param client: legacy 路径 LLM 客户端;``router=None`` 时必传。
    :param model: legacy 路径模型名;``router=None`` 时必传。
    :return: ``DedupResult``。
    """
    if not current_text.strip() or not prior_chapters:
        return DedupResult(passed=True, skipped=True, llm_calls_count=0)

    candidates = prescreen_duplication(
        current_text=current_text, prior_chapters=prior_chapters
    )
    if not candidates:
        _log.debug(
            "cross_chapter_dedup_no_candidates",
            chapter_id=chapter_id,
            workspace_id=workspace_id,
        )
        return DedupResult(passed=True, skipped=True, llm_calls_count=0)

    # setup 阶段失败走 fail-open。
    try:
        resolved_client, resolved_model, complete_kwargs = resolve_routing(
            agent_name="cross_chapter_dedup_validator",
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
            output_model=_DedupBatchPayload,
            schema_name="cross_chapter_dedup",
            complete_kwargs=complete_kwargs,
        )
    except Exception as exc:  # noqa: BLE001 — fail-open 包括 setup 阶段
        _log.warning(
            "cross_chapter_dedup_setup_failed_open",
            chapter_id=chapter_id,
            workspace_id=workspace_id,
            candidate_count=len(candidates),
            error_type=type(exc).__name__,
            error_message=str(exc),
        )
        return DedupResult(
            passed=True,
            candidates_count=len(candidates),
            llm_calls_count=0,
            fail_open=True,
        )

    user_text = _build_user_message(candidates=candidates)

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
    except Exception as exc:  # noqa: BLE001 — fail-open
        _log.warning(
            "cross_chapter_dedup_failed_open",
            chapter_id=chapter_id,
            workspace_id=workspace_id,
            candidate_count=len(candidates),
            error_type=type(exc).__name__,
            error_message=str(exc),
        )
        return DedupResult(
            passed=True,
            candidates_count=len(candidates),
            llm_calls_count=1,
            fail_open=True,
        )

    # 按 index 对齐;缺的候选默认 intentional(不误产 violation,护 §0)。
    verdict_by_index: dict[int, _DedupItemPayload] = {
        r.index: r for r in payload.results
    }
    violations: list[str] = []
    padding_phrases: list[str] = []
    for i, cand in enumerate(candidates, start=1):
        hit = verdict_by_index.get(i)
        if hit is None or hit.verdict != "padding":
            continue
        violations.append(_render_violation(cand, reason=hit.reason))
        # v1.1 wave 5 s1(M1):裸套话短语供动态卡控制器跨章累计(稳定 key)。
        # 仅 phrase 类(current_excerpt = n-gram 套话);passage 类整句近重复不是
        # 可规避的"套话",不喂 M1 avoid-list。
        if cand.kind == "phrase":
            padding_phrases.append(cand.current_excerpt)

    passed = not violations
    if violations:
        _log.info(
            "cross_chapter_dedup_violations",
            chapter_id=chapter_id,
            workspace_id=workspace_id,
            candidate_count=len(candidates),
            violation_count=len(violations),
            violations=violations,
        )
    else:
        _log.debug(
            "cross_chapter_dedup_ok",
            chapter_id=chapter_id,
            workspace_id=workspace_id,
            candidate_count=len(candidates),
        )

    return DedupResult(
        passed=passed,
        violations=violations,
        padding_phrases=padding_phrases,
        candidates_count=len(candidates),
        llm_calls_count=1,
        fail_open=False,
    )


def _render_violation(cand: DedupCandidate, *, reason: str) -> str:
    if cand.kind == "passage":
        return (
            f"正文跨章去重:本章「{cand.current_excerpt}」与 {cand.prior_chapter_id}"
            f"「{cand.prior_excerpt}」高度重复(相似度 {cand.signal:.0%})——{reason}"
        )
    return (
        f"正文跨章去重:套话「{cand.current_excerpt}」近 {_LOOKBACK_CHAPTERS} 章内"
        f"出现 {int(cand.signal)} 次疑似复读——{reason}"
    )


def _build_user_message(*, candidates: list[DedupCandidate]) -> str:
    """拼装 user message:候选重复列表 + agent_role marker。"""
    lines: list[str] = []
    for i, cand in enumerate(candidates, start=1):
        if cand.kind == "passage":
            lines.append(
                f"{i}. [类型:句级近重复] 本章片段:{cand.current_excerpt}\n"
                f"   前章({cand.prior_chapter_id})片段:{cand.prior_excerpt}\n"
                f"   字面相似度:{cand.signal:.0%}"
            )
        else:
            lines.append(
                f"{i}. [类型:高频套话] 短语:{cand.current_excerpt}\n"
                f"   近 {_LOOKBACK_CHAPTERS} 章内出现次数:{int(cand.signal)}"
            )
    candidates_block = "\n".join(lines)
    body = (
        "## 候选重复片段列表(逐条判定:注水复读 padding / 有意回扣 intentional)\n\n"
        f"{candidates_block}\n\n"
        "## strict JSON 提交前自检\n\n"
        "reason 引用正文时使用中文书名号式括号「」；禁止在 JSON 字符串值内"
        "直接写未转义的英文双引号。只输出一份可由标准 json.loads 解析的 JSON。\n"
    )
    return append_role_marker(body, role="cross_chapter_dedup_validator")


def _parse_llm_payload(*, raw_text: str, model_used: str) -> _DedupBatchPayload:
    """剥代码块 → ``json.loads`` → Pydantic 校验。失败抛 ``ValueError``。"""
    text = _strip_code_fence(raw_text).strip()
    try:
        data = loads_with_quote_repair(
            text,
            agent_name="cross_chapter_dedup_validator",
            model_used=model_used,
        )
    except json.JSONDecodeError as exc:
        raise ValueError(
            f"cross_chapter_dedup_validator LLM 输出非合法 JSON(model={model_used}): "
            f"{raw_text[:200]!r}"
        ) from exc
    try:
        return _DedupBatchPayload.model_validate(data)
    except ValidationError as exc:
        raise ValueError(
            f"cross_chapter_dedup_validator LLM 输出 schema 不符(model={model_used}): "
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
    "PriorChapter",
    "DedupCandidate",
    "DedupResult",
    "DedupVerdict",
    "DedupKind",
    "prescreen_duplication",
    "validate_cross_chapter_dedup",
]
