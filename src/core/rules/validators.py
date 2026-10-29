"""[HARD] validators(PROJECT_SPEC §4.1 / §4.2 / §4.3)。

P4-B-06 第一批(节奏类 §4.1,4 类):

1. ``check_word_count`` — 上游名义区间不变；写后纯代码验收允许隐藏 10% 容差
2. ``check_must_achieve`` — 必达事件全部命中(关键词 / 子串检索 + 兜底 LLM 二次校验)
3. ``check_must_unresolved`` — 必须保持未解决悬念全部未被显式 resolve(LLM 语义校验,
   P4-B-06 简化版:启发式关键词 + 默认放行)
4. ``check_forbidden`` — 禁止元素全不出现(子串匹配)

P4-B-07 第二批(一致性类 §4.2 + 衔接类 §4.3,任务口径"5 类",6 个 check):

5. ``check_character_existence`` — 写前 admission:章节卡 POV、出场角色与
   continuity POV 引用全部在 Character 表中存在
6. ``check_timeline_monotonic`` — §4.2 第 3 条:已落库 ``TimelineEntry.time_order``
   严格单调(P4 简版不识别 flashback,完整 flashback 字段留 P5+)
7. ``check_dead_character_references`` — 写前 admission:``current_state.status
   == "dead"`` 的角色不再被章节卡标作当前出场。它不检查“前 ≥ 2 章铺垫”，
   旧名 ``check_death_foreshadowing`` 只保留兼容别名。
8. ``check_voice_forbidden`` — 角色禁语先做全文候选粗筛；能从角色名 + 发言/
   心声标记归属的命中为 HARD，无法归属的命中仅 WARN。全局禁语另由章节卡
   ``forbidden`` 的统一 HARD 检查负责
9. ``check_unfinished_actions_continuation`` — §4.3 第 1 条:``unfinished_actions``
   非空时,本章前 ``window_chars`` (默认 200)字内必须命中任一未完成动作关键词
10. ``check_scene_continuation_location`` — §4.3 第 2 条:``scene_continuation == "same"``
    时,前 ``window_chars`` (默认 100)字内不允许出现地点切换关键词

每个 validator 返回 ``ValidatorResult``,字段:

- ``name``:validator 名称(structlog 事件 ``validator_name`` 字段透传值)
- ``passed``:是否通过
- ``details``:失败详情(``passed=True`` 时 dict 为空 / 仅含统计)

LLM 兜底接口签名:``Callable[[str, str], bool]``(item, chapter_text → satisfied)。
P4-B-06 默认 ``None``,各 validator 内部用 stub 直接返 True 保守通过。完整 LLM
兜底实现 prompts/解析/重试逻辑登入 v2_wishlist,P5+ 落地。
"""

from __future__ import annotations

from collections.abc import Callable, Iterable
from dataclasses import dataclass, field
import re
from typing import TYPE_CHECKING, Any, Final, Literal

if TYPE_CHECKING:
    from src.core.models.chapter_card import ChapterCard
    from src.core.models.character import Character
    from src.core.models.timeline_entry import TimelineEntry

# LLM 兜底回调签名:(待校验项, 章节正文) → 该项是否在语义层被满足。
# P4-B-06 默认 None,各 validator 走子串匹配为主、关键词 miss 时 stub 返 True 放行。
LLMFallbackCheck = Callable[[str, str], bool]

# R9 v5:章节卡、Writer/Reviser/Rewriter 与 prompt 继续使用名义目标区间；只有
# 写后纯代码验收允许上下各 10% 的隐藏冗余。这个常量不得渲染进任何 Agent
# prompt，也不得改写 ExecutableConstraintPlan 中的名义 minimum/maximum。
WORD_COUNT_ACCEPTANCE_TOLERANCE_RATIO: Final[float] = 0.10
WordCountAcceptanceBand = Literal["target", "tolerated", "rejected"]


def word_count_acceptance_bounds(
    *,
    word_count_min: int,
    word_count_max: int,
    allow_word_count_tolerance: bool = True,
) -> tuple[int, int]:
    """返回本地验收边界；整数运算避免浮点边界漂移。"""

    if word_count_min < 0 or word_count_max < word_count_min:
        raise ValueError("word-count target 边界非法")
    if not allow_word_count_tolerance:
        return word_count_min, word_count_max
    tolerance_percent = int(WORD_COUNT_ACCEPTANCE_TOLERANCE_RATIO * 100)
    lower = (
        word_count_min * (100 - tolerance_percent) + 99
    ) // 100
    # 下界取 ceil、上界取 floor：离散字符数必须真正落在连续的 ±10% 闭区间内。
    upper = word_count_max * (100 + tolerance_percent) // 100
    return lower, upper


def classify_word_count_acceptance(
    actual_word_count: int,
    *,
    word_count_min: int,
    word_count_max: int,
    allow_word_count_tolerance: bool = True,
) -> WordCountAcceptanceBand:
    """按名义目标、隐藏容差、拒绝三档分类，不调用 Provider。"""

    if word_count_min <= actual_word_count <= word_count_max:
        return "target"
    acceptance_min, acceptance_max = word_count_acceptance_bounds(
        word_count_min=word_count_min,
        word_count_max=word_count_max,
        allow_word_count_tolerance=allow_word_count_tolerance,
    )
    if acceptance_min <= actual_word_count <= acceptance_max:
        return "tolerated"
    return "rejected"


@dataclass(frozen=True)
class ValidatorResult:
    """单个 validator 的输出。

    ``passed=False`` 时 ``details`` 必含具体诊断字段(如 ``actual_word_count`` /
    ``missing_items`` / ``forbidden_hits`` 等),便于 ``validator_failed`` 事件
    透出 + 后续 ``failed_constraints`` 列表展示。
    """

    name: str
    passed: bool
    details: dict[str, Any] = field(default_factory=dict)


def check_word_count(
    chapter_text: str,
    *,
    word_count_min: int,
    word_count_max: int,
    allow_word_count_tolerance: bool = True,
) -> ValidatorResult:
    """[HARD] 名义目标不变；本地验收允许上下各 10% 的隐藏容差。

    中文字数按 ``len(chapter_text)`` 计——本项目以中文长篇为主,代码点计数与
    "字符数"语义一致;西文场景下"词数"语义需另算,P4 范围内不区分。
    """
    actual = len(chapter_text)
    acceptance_min, acceptance_max = word_count_acceptance_bounds(
        word_count_min=word_count_min,
        word_count_max=word_count_max,
        allow_word_count_tolerance=allow_word_count_tolerance,
    )
    band = classify_word_count_acceptance(
        actual,
        word_count_min=word_count_min,
        word_count_max=word_count_max,
        allow_word_count_tolerance=allow_word_count_tolerance,
    )
    if band == "target":
        return ValidatorResult(
            name="word_count",
            passed=True,
            details={"actual_word_count": actual},
        )
    if band == "tolerated":
        return ValidatorResult(
            name="word_count",
            passed=True,
            # 与名义区间内通过保持同形，避免任何上游消费者从结果反推出
            # 隐藏容差。maintainer 日志由 validator pipeline 本地重算。
            details={"actual_word_count": actual},
        )
    return ValidatorResult(
        name="word_count",
        passed=False,
        details={
            "actual_word_count": actual,
            "word_count_min": word_count_min,
            "word_count_max": word_count_max,
            # 不持久化/回流隐藏验收边界；Writer/Rewriter/Reviser 只看名义
            # ChapterCard 区间和传统 below_min/above_max 结果。
            "reason": "below_min" if actual < acceptance_min else "above_max",
        },
    )


def check_must_achieve(
    chapter_text: str,
    *,
    must_achieve: list[str],
    llm_fallback: LLMFallbackCheck | None = None,
) -> ValidatorResult:
    """[HARD] PROJECT_SPEC §4.1:must_achieve 必达事件全部满足。

    实现策略:
    1. **关键词检索(主路径)**:每条 phrase 直接子串匹配 ``chapter_text``;
       命中即视为该项达成。
    2. **LLM 兜底(可选)**:关键词 miss 时调 ``llm_fallback(item, chapter_text)``
       做语义校验;``llm_fallback`` 为 ``None`` 时走 stub(P4-B-06 默认行为,
       保守判定通过 = True 放行,不阻塞主流程)。

    边界情况:``must_achieve`` 为空时直接通过。
    """
    if not must_achieve:
        return ValidatorResult(
            name="must_achieve",
            passed=True,
            details={"items_total": 0},
        )

    missing: list[str] = []
    fallback_used: list[str] = []
    for item in must_achieve:
        if item in chapter_text:
            continue
        # 关键词 miss → LLM 兜底
        if llm_fallback is None:
            # P4-B-06 严格模式:LLM 兜底未注入时关键词 miss 直接判失败,与
            # PROJECT_SPEC §4.1 [HARD] "必达事件" 语义一致。同义词 / 隐含表达
            # 的 LLM 二次校验完整实现登入 v2_wishlist,P5+ 落地。
            missing.append(item)
            continue
        # LLM 兜底被调用 → 计入 llm_fallback_used(无论返回值,便于诊断 LLM
        # 实际处理量 / cost 估算)。
        fallback_used.append(item)
        try:
            satisfied = llm_fallback(item, chapter_text)
        except Exception:
            # LLM 兜底**异常** swallow + 保守通过(对齐 P3-B-13 continuity
            # refresh 失败 swallow 模式;LLM 调用失败不应放大成 validator 误判)。
            # 注意区分:LLM **不可用**(llm_fallback=None)走严格模式,LLM
            # **可用但调用抛错** swallow 放行,语义不同。
            satisfied = True
        if not satisfied:
            missing.append(item)

    passed = not missing
    details: dict[str, Any] = {
        "items_total": len(must_achieve),
        "items_missing": list(missing),
    }
    if fallback_used:
        details["llm_fallback_used"] = list(fallback_used)
    return ValidatorResult(name="must_achieve", passed=passed, details=details)


def check_must_unresolved(
    chapter_text: str,
    *,
    must_unresolved: list[str],
    llm_fallback: LLMFallbackCheck | None = None,
) -> ValidatorResult:
    """[HARD] PROJECT_SPEC §4.1:must_unresolved 必须保持未解决。

    实现策略(P4-B-06 简化版):
    1. **启发式关键词检测**:对每条 unresolved item,组合 ``item`` + 一组
       "解决态关键词"(如 ``解决`` / ``真相大白`` / ``终于明白`` /
       ``水落石出`` / ``揭晓`` / ``查明``)做共现检测——item 在文中出现,
       且周围 N 字符窗口内出现解决态关键词 → 视为该悬念被解决,违反 [HARD]。
    2. **LLM 兜底(可选)**:启发式 hit 时调 ``llm_fallback`` 二次确认;默认 stub。

    边界:``must_unresolved`` 为空时直接通过;item 未在文中出现时**默认通过**
    (悬念在文外保持未解决;启动指令字面"在产出中保持悬而未决"语义同此处置)。

    P4-B-06 简化版意识到的局限:启发式关键词组易误判(角色对白用了"解决"
    但语义指别的事);完整 LLM 兜底实现登入 v2_wishlist。
    """
    if not must_unresolved:
        return ValidatorResult(
            name="must_unresolved",
            passed=True,
            details={"items_total": 0},
        )

    resolution_keywords = (
        "解决",
        "真相大白",
        "终于明白",
        "水落石出",
        "揭晓",
        "查明",
        "找到答案",
    )
    window = 80  # item 周围 80 字符窗口检测解决态关键词共现

    resolved: list[str] = []
    fallback_used: list[str] = []
    for item in must_unresolved:
        idx = chapter_text.find(item)
        if idx < 0:
            # item 未出现 → 默认保持未解决
            continue
        start = max(0, idx - window)
        end = min(len(chapter_text), idx + len(item) + window)
        window_text = chapter_text[start:end]
        if not any(kw in window_text for kw in resolution_keywords):
            continue
        # 启发式 hit → LLM 兜底二次确认
        if llm_fallback is None:
            # 严格模式:LLM 兜底未注入时,启发式关键词共现 hit 直接判失败,
            # 与 [HARD] 语义一致。启发式可能误判(角色对白用了"解决"但语义
            # 指别的事),完整 LLM 兜底实现登入 v2_wishlist。
            resolved.append(item)
            continue
        fallback_used.append(item)
        try:
            still_unresolved = llm_fallback(item, chapter_text)
        except Exception:
            # LLM 异常 swallow + 保守通过(item 视为仍未解决,不进 resolved)。
            still_unresolved = True
        if not still_unresolved:
            resolved.append(item)

    passed = not resolved
    details: dict[str, Any] = {
        "items_total": len(must_unresolved),
        "items_resolved": list(resolved),
    }
    if fallback_used:
        details["llm_fallback_used"] = list(fallback_used)
    return ValidatorResult(name="must_unresolved", passed=passed, details=details)


def check_forbidden(
    chapter_text: str,
    *,
    forbidden: list[str],
) -> ValidatorResult:
    """[HARD] PROJECT_SPEC §4.1:forbidden 禁止元素全不出现(子串匹配)。

    任一 forbidden phrase 出现即失败;``details.forbidden_hits`` 列出全部命中
    项(便于一次返回让作者一并修)。

    边界:``forbidden`` 为空时直接通过。
    """
    if not forbidden:
        return ValidatorResult(
            name="forbidden",
            passed=True,
            details={"items_total": 0},
        )

    hits = [item for item in forbidden if item in chapter_text]
    passed = not hits
    return ValidatorResult(
        name="forbidden",
        passed=passed,
        details={
            "items_total": len(forbidden),
            "forbidden_hits": list(hits),
        },
    )


# ---------------------------------------------------------------------------
# 第二批(P4-B-07):一致性类 §4.2 + 衔接类 §4.3
# ---------------------------------------------------------------------------


def check_character_existence(
    *,
    chapter_card: "ChapterCard",
    known_character_ids: Iterable[str],
) -> ValidatorResult:
    """[HARD] PROJECT_SPEC §4.2 第 1 条:出场角色必须在人设卡中存在。

    ``pov_character``、``related_characters`` 与 ``continuity_context.pov_to``
    全部在 ``known_character_ids`` 集合内即通过；有任一 ID 不存在 → 失败，
    ``details.missing_character_ids`` 列出全部缺失项。

    ``known_character_ids`` 由 caller 预读 Character 表(SQLAlchemy session
    or 测试 fixture)再传入 — validator 模块本身不依赖持久层(类 B 决策:
    工程判断不让 ``src.core.rules`` 引入 ORM 依赖)。

    本检查仍不解析正文中的自然语言人名；它检查的是 Writer 调用前可确定的
    章节卡引用闭环。
    """
    related = list(
        dict.fromkeys(
            [
                chapter_card.pov_character,
                *chapter_card.related_characters,
                chapter_card.continuity_context.pov_to,
            ]
        )
    )
    known_set = set(known_character_ids)
    missing = [cid for cid in related if cid not in known_set]
    passed = not missing
    details: dict[str, Any] = {
        "related_characters_total": len(related),
        "referenced_character_ids": related,
        "missing_character_ids": missing,
    }
    return ValidatorResult(name="character_existence", passed=passed, details=details)


def check_timeline_monotonic(
    *,
    timeline_entries: Iterable["TimelineEntry"],
) -> ValidatorResult:
    """[HARD] PROJECT_SPEC §4.2 第 3 条:时间线单调(``time_order`` 严格递增)。

    检测策略:按 ``timeline_entries`` 调用方传入的顺序逐对检查相邻 ``time_order``
    严格 < 后者。``timeline_entries`` 通常由 caller 按 ``chapter_id`` ASC + 卷内
    顺序预读 — 若调用方需要按其他键排序,自行排好再传入。

    边界:
    - 空 / 单条 → 直接通过(无相邻对可比)
    - 任一相邻对违反严格递增 → 失败,``details.violation`` 给出第一个违反位置
      ``(prev_entry_id, prev_time_order, next_entry_id, next_time_order)``。

    v1.1 wave 1 session 1 引入 ``is_flashback`` 字段后(E2 治本下游,Alembic 0013):
    本 validator 迭代时**跳过 ``is_flashback=True`` 的 entry**,只在主时间线内做单调
    校验。语义:flashback 段是叙事回溯,其 ``time_order`` 可以小于主时间线但不算
    违规;``MemoryExtractor`` 抽 timeline entry 时由 LLM prompt 引导标 ``is_flashback``
    字段(``prompts/memory_extractor.md``)。

    根因背景(luo-cloud-test 2026-05-25 真实战):ch-10 上游 G3 治理失败后,mini_plan
    把 arc-3 的"双时空穿梭"提前到 arc-1 末章触发 → ch-11-16 进入双时空叙事(主角在
    地球和未来之间交替活动)→ 本 validator 旧版 100% 误判 ch-11+ 为时间线违规,
    每章 reviewer 重写 4 次仍挂,工作流接近瘫痪。E2 让 validator 识别 flashback 后,
    即使作品本身需要回忆段也不再误判;G3 + E2 配套治本。
    """
    # v1.1 E2:跳过 flashback 条目(只在主时间线内做单调校验)
    entries = [e for e in timeline_entries if not getattr(e, "is_flashback", False)]
    if len(entries) < 2:
        return ValidatorResult(
            name="timeline_monotonic",
            passed=True,
            details={"entries_total": len(entries)},
        )

    for prev, curr in zip(entries, entries[1:], strict=False):
        if curr.time_order <= prev.time_order:
            return ValidatorResult(
                name="timeline_monotonic",
                passed=False,
                details={
                    "entries_total": len(entries),
                    "violation": {
                        "prev_entry_id": prev.entry_id,
                        "prev_time_order": prev.time_order,
                        "next_entry_id": curr.entry_id,
                        "next_time_order": curr.time_order,
                    },
                },
            )

    return ValidatorResult(
        name="timeline_monotonic",
        passed=True,
        details={"entries_total": len(entries)},
    )


def check_dead_character_references(
    *,
    chapter_card: "ChapterCard",
    character_records: Iterable["Character"],
) -> ValidatorResult:
    """[PRE-WRITE HARD] 章节卡不得把已死亡角色标作当前出场。

    本函数只读章节卡与角色当前状态，属于 Writer 无法修复的数据/规划缺陷，
    必须在任何正文调用前拦截。真正的“死亡前至少两章铺垫”需要规划期历史卡
    序列，不能用本检查的旧名字冒充已实现。

    类 A 决策:plan 字面"角色 status=dead 字段" vs 数据模型 ``Character.current_state``
    dict(无顶层 ``status`` 字段),按数据模型走 — 读 ``current_state.get("status")``
    约定字段。impl 文档登记。

    ``character_records`` 由 caller 预读(覆盖 ``related_characters`` 即可,
    其他角色不影响本 validator 判定)。
    """
    related = (
        {p.character_id for p in chapter_card.narrative_context.participation if p.mode == "present"}
        if chapter_card.narrative_context is not None else set(chapter_card.related_characters)
    )
    if chapter_card.pov_character:
        related.add(chapter_card.pov_character)
    if chapter_card.continuity_context.pov_to:
        related.add(chapter_card.continuity_context.pov_to)
    dead_in_related: list[dict[str, str]] = []
    for ch in character_records:
        if ch.character_id not in related:
            continue
        status = ch.current_state.get("status") if isinstance(ch.current_state, dict) else None
        if status == "dead":
            dead_in_related.append({"character_id": ch.character_id, "name": ch.name})

    passed = not dead_in_related
    details: dict[str, Any] = {
        "related_characters_total": len(related),
        "dead_characters_in_related": dead_in_related,
    }
    return ValidatorResult(
        name="dead_character_reference", passed=passed, details=details
    )


def check_death_foreshadowing(
    *,
    chapter_card: "ChapterCard",
    character_records: Iterable["Character"],
) -> ValidatorResult:
    """兼容旧调用名；实际只做 ``dead_character_reference`` 写前检查。"""
    return check_dead_character_references(
        chapter_card=chapter_card,
        character_records=character_records,
    )


def check_voice_forbidden(
    chapter_text: str,
    *,
    chapter_card: "ChapterCard",
    character_records: Iterable["Character"],
) -> ValidatorResult:
    """角色禁语候选粗筛 + 保守说话人归属。
    ``voice_profile.forbidden_phrases``。

    只有同一语句窗口能看到“角色名 + 说/道/问/暗想/暗骂等归属词 + 禁语”时，
    才把命中视为该角色可归属的 HARD。全文只有禁语子串、无法证明谁说的命中
    留在 ``ambiguous_hits``，由 pipeline 记结构化 WARN。若某短语是全局禁用，
    应进入 ChapterCard ``forbidden``，由 ``check_forbidden`` 的统一 HARD 身份
    检查，不在这里另造第二把尺。

    ``details.hits`` 形如:``[{"character_id": ..., "name": ..., "phrase": ...}, ...]``
    — 多角色多 phrase 全部命中项一次性列出便于作者批量修。
    """
    related = set(chapter_card.related_characters)
    related.add(chapter_card.pov_character)
    hits: list[dict[str, str]] = []
    attributable_hits: list[dict[str, str]] = []
    ambiguous_hits: list[dict[str, str]] = []
    for ch in character_records:
        if ch.character_id not in related:
            continue
        for phrase in ch.voice_profile.forbidden_phrases:
            if phrase and phrase in chapter_text:
                hit = {
                    "character_id": ch.character_id,
                    "name": ch.name,
                    "phrase": phrase,
                }
                hits.append(hit)
                if _voice_phrase_is_attributable(
                    chapter_text, character_name=ch.name, phrase=phrase
                ):
                    attributable_hits.append(hit)
                else:
                    ambiguous_hits.append(hit)

    passed = not attributable_hits
    return ValidatorResult(
        name="voice_forbidden",
        passed=passed,
        details={
            "related_characters_total": len(related),
            "hits": hits,
            "attributable_hits": attributable_hits,
            "ambiguous_hits": ambiguous_hits,
        },
    )


_VOICE_ATTRIBUTION_CUE_RE = re.compile(
    r"(?:说|道|问|答|喊|叫|骂|嘀咕|低声|沉声|开口|心想|暗想|暗骂|念道|喃喃)"
)
_VOICE_ATTRIBUTION_WINDOW = 48


def _voice_phrase_is_attributable(
    text: str, *, character_name: str, phrase: str
) -> bool:
    """保守判定禁语是否能归属到指定角色，不跨段、不跨完整句。"""
    start = 0
    while True:
        phrase_index = text.find(phrase, start)
        if phrase_index < 0:
            return False
        window_start = max(0, phrase_index - _VOICE_ATTRIBUTION_WINDOW)
        prefix = text[window_start:phrase_index]
        local_prefix = re.split(r"[。！？!?\n]", prefix)[-1]
        name_index = local_prefix.rfind(character_name)
        if name_index >= 0:
            after_name = local_prefix[name_index + len(character_name) :]
            if _VOICE_ATTRIBUTION_CUE_RE.search(after_name):
                return True
        start = phrase_index + max(len(phrase), 1)


# 职业称谓黑名单邻接窗口(字符):他角色专属职业词出现在本角色名前后该窗口内 →
# 标记。取小窗口(紧邻)减少"同场景他角色职业被正常提及"的误报;粗筛只标记不阻断。
_PROFESSION_ADJACENCY_WINDOW = 12


def _term_adjacent_to_name(
    text: str, *, name: str, term: str, window: int
) -> bool:
    """``term`` 是否出现在 ``name`` 任一出现处前后 ``window`` 字窗口内。"""
    start = 0
    while True:
        idx = text.find(name, start)
        if idx == -1:
            return False
        lo = max(0, idx - window)
        hi = idx + len(name) + window
        if term in text[lo:hi]:
            return True
        start = idx + len(name)


def check_profession_title_blacklist(
    chapter_text: str,
    *,
    chapter_card: "ChapterCard",
    character_records: Iterable["Character"],
) -> ValidatorResult:
    """[WARN-only 粗筛] A3 职业称谓黑名单(v1.1 wave 6 session 2)。

    治 B1 bug A1(沈舟现实=心理医生却被写成"刑警" —— 把他角色的职业安到本角色头上)。
    **纯代码邻接窗口检测**(不调 LLM):对每个出场(related / pov)且有
    ``identity_facets`` 的角色,若**他角色专属的职业 / 身份词**出现在该角色名邻近
    窗口内(如"现代刑警沈舟"),标记为疑似"身份写串"。

    黑名单构造:全工作区角色 ``identity_facets`` 的 ``identity`` 词全集中,**不属于
    本角色自己**(也不是本角色任一 own term 的子串、不是本角色名子串)的词 = 本角色的
    "错位职业词"。

    **粗筛 + 只标记**(对齐 wave6_design §1 A3"职业称谓黑名单代码粗筛只标记"):
    - 邻接窗口紧邻(±N 字)减少同场景他角色职业被正常提及的误报;
    - 仍可能误报(同段两角色)→ 故 caller 接为 **WARN-only,不阻断 rewrite**;
    - 真正治本靠 writer identity_section 注入(A3 上游)+ B1 设定一致性 LLM 校验
      (w6-s3 下游)。本粗筛是廉价的第三道兜底。

    ``details.hits`` 形如:``[{"character_id", "name", "wrong_title", "title_owner"}, ...]``。
    """
    related = set(chapter_card.related_characters)
    if chapter_card.pov_character:
        related.add(chapter_card.pov_character)

    records = list(character_records)
    # 全工作区职业词 → 拥有它的角色名集合(供 evidence);本角色 own terms 集合
    term_owners: dict[str, set[str]] = {}
    own_terms: dict[str, set[str]] = {}
    for ch in records:
        terms = {
            f.identity.strip()
            for f in ch.identity_facets
            if f.identity and f.identity.strip()
        }
        own_terms[ch.character_id] = terms
        for t in terms:
            term_owners.setdefault(t, set()).add(ch.name)

    all_terms = set(term_owners)
    hits: list[dict[str, str]] = []
    for ch in records:
        if ch.character_id not in related:
            continue
        own = own_terms.get(ch.character_id) or set()
        if not own:
            continue  # 无 facets 角色不检(普通角色)
        name = ch.name
        if not name or name not in chapter_text:
            continue
        # 错位词 = 全集 − 本角色 own;排除"本角色 own term 的子串"(如 own 含
        # "心理医生" 时不把他角色的"医生"误判)+ "本角色名的子串"。
        wrong_terms = {
            t
            for t in (all_terms - own)
            if not any(t in o for o in own)
            and t not in name
        }
        for term in wrong_terms:
            if _term_adjacent_to_name(
                chapter_text,
                name=name,
                term=term,
                window=_PROFESSION_ADJACENCY_WINDOW,
            ):
                hits.append(
                    {
                        "character_id": ch.character_id,
                        "name": name,
                        "wrong_title": term,
                        "title_owner": "、".join(sorted(term_owners[term])),
                    }
                )

    passed = not hits
    return ValidatorResult(
        name="profession_title_blacklist",
        passed=passed,
        details={
            "related_with_facets": sum(
                1 for cid in related if own_terms.get(cid)
            ),
            "hits": hits,
        },
    )


# 衔接桥(§4.3)地点切换关键词集 — 用于 ``check_scene_continuation_location``
# 启发式判定。完整 LLM 语义判定留 v2_wishlist(误报场景:角色已在原场景内
# 自然移动也命中"走到"/"前往"等关键词)。
_SCENE_SWITCH_KEYWORDS: tuple[str, ...] = (
    "来到",
    "抵达",
    "转移到",
    "前往",
    "进入",
    "换到",
    "切换至",
    "回到",
    "现身于",
    "走入",
    "踏入",
)


def check_unfinished_actions_continuation(
    chapter_text: str,
    *,
    chapter_card: "ChapterCard",
    window_chars: int = 200,
) -> ValidatorResult:
    """[HARD] PROJECT_SPEC §4.3 第 1 条:``unfinished_actions`` 非空时,本章前
    ``window_chars`` (默认 200)字必须解决或显式延续。

    P4 简版判定:若 ``continuity_context.unfinished_actions`` 非空,正文前
    ``window_chars`` 字内必须出现至少一条 unfinished_action 的子串(算"提及 = 解决
    或显式延续")。零命中 → 失败。完整 LLM 语义判定(区分"解决"vs"显式延续"
    vs"继续推进")留 v2_wishlist。

    边界:``unfinished_actions`` 为空 → 直接通过。
    """
    actions = list(chapter_card.continuity_context.unfinished_actions)
    if not actions:
        return ValidatorResult(
            name="unfinished_actions_continuation",
            passed=True,
            details={"actions_total": 0},
        )

    head = chapter_text[:window_chars]
    matched = [a for a in actions if a in head]
    passed = bool(matched)
    return ValidatorResult(
        name="unfinished_actions_continuation",
        passed=passed,
        details={
            "actions_total": len(actions),
            "window_chars": window_chars,
            "matched_actions": matched,
            "missing_actions": [a for a in actions if a not in matched],
        },
    )


def check_scene_continuation_location(
    chapter_text: str,
    *,
    chapter_card: "ChapterCard",
    window_chars: int = 100,
) -> ValidatorResult:
    """[HARD] PROJECT_SPEC §4.3 第 2 条:``scene_continuation == "same"`` 时,
    前 ``window_chars`` (默认 100)字不允许出现地点切换关键词。

    P4 简版判定:若 ``continuity_context.scene_continuation == "same"``,正文
    前 ``window_chars`` 字内不应出现 ``_SCENE_SWITCH_KEYWORDS``(``来到`` /
    ``抵达`` / ``转移到`` 等启发式集);命中即失败。完整 LLM 语义判定
    (区分"角色在原场景自然移动" vs"切换到新地点")留 v2_wishlist。

    边界:``scene_continuation`` 不是 ``same``(``switch`` / ``time_jump``)
    → 直接通过,不检查关键词。
    """
    scene_mode = chapter_card.continuity_context.scene_continuation
    if scene_mode != "same":
        return ValidatorResult(
            name="scene_continuation_location",
            passed=True,
            details={"scene_continuation": scene_mode, "skipped": True},
        )

    head = chapter_text[:window_chars]
    hits = [kw for kw in _SCENE_SWITCH_KEYWORDS if kw in head]
    passed = not hits
    return ValidatorResult(
        name="scene_continuation_location",
        passed=passed,
        details={
            "scene_continuation": scene_mode,
            "window_chars": window_chars,
            "location_switch_keywords_hit": hits,
        },
    )
