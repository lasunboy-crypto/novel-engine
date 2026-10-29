"""章节卡服务(P5-B-06 引入)。

对齐 API_CONTRACT §4.3 字面 ``card:generate`` + p5_plan §"P5-B-06"目标字面
+ P5-B-05 §6 接力 marker(``chapter_card_service`` 派生器职责 / 单卡生成 +
card_status 初值)。

设计要点
========

1. **派生器职责**(P5-B-05 §6 接力 marker 字面):caller 给
   ``(workspace_id, chapter_id, chapter_number, volume_id)`` 四元组,本 service
   ``derive_context`` 从 outline 4 段 SSOT 文件 + ``volumes`` SQLite + 上一章
   ``ChapterVersion.final`` 末尾 500-800 字 + ``characters`` SQLite +
   ``plot_threads`` SQLite 派生 ``ChapterCardGenerationContext`` 入参。

2. **agent wire**(注入 ``chapter_card_generator.generate_chapter_card``):
   sync 调用 + 失败 swallow + structlog 落事件；JSON/schema 等技术失败不自动
   降级。引用错误沿既有严格 ID 重试；Setting Judge 首次返回完整结构化 HARD，
   或双 quote 可回查的权威 unique-rule 未消歧候选时，只允许一次证据绑定的定向
   重试，二次冲突 fail closed。普通单卡与正式 UI rejected-card 重生入口共用同尺。

3. **SSOT 落盘 + ORM 同步**(对齐 P5-B-02 决策 6=C 字面"文件 SSOT + DB
   缓存"):成功生成的 ChapterCard 通过 ``ChapterService.save_chapter_card``
   原子写到 ``data/cards/<chapter_id>.json``;同时 upsert ``chapter_cards``
   表行(card_status 初值由 caller 控制 — 单端点 ``draft``,outline:commit
   首卡为 ``pending_review``)。

4. **outline:commit 后首卡生成**:``generate_first_card_after_commit`` 由
   FastAPI ``BackgroundTasks`` 驱动，固定只生成当前第一张正式卡。
   完成后 publish ``stage`` / ``done`` 到首卡任务频道；
   ``task_registry`` 同步从 ``pending`` → ``done`` 翻新(supplied task_id 复用)。

5. **正式 Run 线性出卡**:``generate_next_card`` 只接受一个 ``chapter_id``；
   caller 必须先验证上一章富记忆 succeeded。旧 beat 批量 Agent、未来 K 卡
   API 与 UI 已退役。引用重试触发的失效卡 cascade 是独立恢复路径，不是正式
   Run 的未来卡预生成入口。
"""

from __future__ import annotations

import json
from collections.abc import Callable
from dataclasses import dataclass
from datetime import UTC, datetime
from functools import lru_cache
from pathlib import Path
from typing import TYPE_CHECKING, Any, Literal

import jieba.posseg as _pseg
import structlog
from pydantic import BaseModel, ConfigDict, Field
from sqlalchemy import select
from sqlalchemy.orm import Session

from src.api.logging import bind_workspace_context
from src.api.websocket.channels import (
    chapter_card_events_channel,
    task_progress_channel,
)
from src.core.agents.chapter_card_generator import (
    CHAPTER_CARD_CORE_STATE_ERROR_CODE,
    CHAPTER_CARD_REFERENCE_ERROR_CODE,
    ChapterCardGenerationContext,
    ChapterCardGeneratorError,
    CharacterVoiceHint,
    PlotThreadSummary,
    generate_chapter_card,
)
from src.core.agents.chapter_summary_extractor import (
    ChapterSummary,
    ContinuityLedgerValidationError,
    continuity_ledger_provenance,
    load_validated_continuity_ledger,
)
from src.core.agents.reference_validator import (
    ReferenceValidationFailedError,
    collect_unknown_ids_from_card,
    load_known_ids_from_workspace,
)
from src.core.db.models import (
    CharacterORM,
    ChapterCardORM,
    ChapterORM,
    ChapterSummaryORM,
    PlotThreadORM,
    VolumeORM,
)
from src.core.models.chapter_card import ChapterCard, chapter_seq_from_id
from src.core.models.continuity_ledger import (
    validate_immediate_action_scope,
    ContinuityEvidenceRecord,
    ContinuityLedgerProvenance,
    validate_continuity_bridge,
    validate_continuity_projection,
    validate_frozen_provenance,
)
from src.core.models.generation_config import GenerationConfig
from src.core.models.genre_mechanism import (
    progression_label,
    resolve_breakthrough_payoff,
    resolve_progression_consistency,
)
from src.core.models.style_template import STYLE_GENERATION_DEFAULTS
from src.core.models.outline import (
    CharactersOutput,
    MiniPlanItem,
    MiniPlanOutput,
    PlotOutput,
    VolumePlansOutput,
)
from src.core.workspace.workspace_paths import WorkspacePaths
from src.core.models.setting_contract import (
    SettingContractLoadError,
    load_setting_contract,
)
from src.core.models.review import ConstraintViolation
from src.core.rules.warning_routing import (
    WarningRoutingError,
    load_approved_final_warnings,
    render_warning_watchlist,
)
from src.core.rules.drama_contracts import (
    DramaticSourceContractError,
    load_and_validate_card_dramatic_source,
)
from src.services.card_generator_config import CardGeneratorConfig
from src.services.core_state_service import CoreStateService, CoreStateServiceError
from src.services.outline_entity_recovery import (
    OutlineEntityRecoveryError,
    load_outline_entity_baselines,
    restore_rewound_outline_entities_in_session,
)
from src.services.workspace_service import WorkspaceService

if TYPE_CHECKING:
    from src.api.websocket.manager import WebSocketManager
    from src.core.llm.base import LLMClient
    from src.core.memory.vector import VectorStore
    from src.core.routing.router import ModelRouter
    from src.services.chapter_service import ChapterService


_log = structlog.get_logger(__name__)

SessionFactory = Callable[[str], Session]
VectorStoreFactory = Callable[[str], "VectorStore"]

_EMERGENT_PLOT_THREAD_TOP_K = 3
_MEMORY_VECTOR_COLLECTION = "memories"

# One product card-generation action may consume each branch at most once.
# These owner constants let the central Provider-0 reachability registry derive
# its cost tree without copying hidden retry literals.
CHAPTER_CARD_SHARED_CONTRACT_REPAIR_MAX_RETRIES = 1
CHAPTER_CARD_REFERENCE_RETRY_MAX_RETRIES = 1


class RetryReferencesResult(BaseModel):
    """v1.0.x wave 5 session 4:``retry_references`` 服务层返回结果。

    含本次重试后的章节卡 + 级联失效的章节 id 列表。``cascade_regenerate_task_id``
    由**路由层**在注册 BackgroundTask 时回填(service 层不持有 ``task_registry``
    依赖,避免循环 import + 维持 service 与 task 调度解耦,对齐
    ``generate_first_card_after_commit`` 同模式)。
    """

    model_config = ConfigDict(extra="forbid")

    card: ChapterCard = Field(..., description="本次 retry 成功生成的章节卡")
    invalidated_chapter_ids: list[str] = Field(
        default_factory=list,
        description=(
            "因本次卡修改而失效、需要 cascade 重生的后续章节 chapter_id 列表"
            "(已字典序排序,空 list = 无后续卡,通常末章场景)"
        ),
    )


@dataclass
class _SettingConflictRetryState:
    """一次产品卡生成动作共享的 Setting/核心状态定向重试额度。"""

    attempts_used: int = 0


_PREV_CHAPTER_SUMMARIES_DEFAULT_N: int = 10
"""``derive_context`` 注入 ``prev_chapter_summaries`` 序列长度(v1.0.x wave 4 session 3b)。

经验值 10:既覆盖一个完整 arc(typical 5-8 章)+ 上一 arc 收口,又控 prompt
长度在 LLM context 内(每条 ChapterSummary.summary_text ~ 200-500 字 → 10 条
约 3000-5000 字注入,~4-7K token,加上 outline / 角色 / 伏笔 / 卷规划仍稳在
16K context window 内)。维护人合并本会话后真 Qwen 实战可再校准。
"""

_CHARACTER_VOICE_HINT_MAX: int = 12
"""C-7:每章出卡角色提示池上限。

排序规则是:本章 mini_plan.linked_characters 优先,其次 outline 核心角色,最后才是
SQLite 里涌现/提取出的额外角色。12 = outline 核心上限 8 + 最多 4 个章级/近期额外
角色余量;超过即不进本章 character_voices,避免长跑角色表无界污染每章出卡上下文。
"""


def _dedupe_character_ids(character_ids: list[str]) -> list[str]:
    """保序去重角色 ID,跳过空白项。"""
    seen: set[str] = set()
    deduped: list[str] = []
    for cid in character_ids:
        normalized = cid.strip()
        if not normalized or normalized in seen:
            continue
        deduped.append(normalized)
        seen.add(normalized)
    return deduped


def _character_voice_from_orm(row: CharacterORM) -> CharacterVoiceHint:
    """按真实存储字段传递已选人物素材，旧 description 仅作兼容回退。"""
    voice = row.voice_profile if isinstance(row.voice_profile, dict) else {}
    parts = [voice.get("tone"), voice.get("sentence_style")]
    voice_hint = "；".join(dict.fromkeys(
        part.strip() for part in parts if isinstance(part, str) and part.strip()
    ))
    legacy_hint = voice.get("description")
    if not voice_hint and isinstance(legacy_hint, str):
        voice_hint = legacy_hint.strip()
    return CharacterVoiceHint(
        character_id=row.character_id,
        name=row.name,
        role=row.archetype or "(未填)",
        voice_hint=voice_hint or "(未填 voice_hint)",
        personality=row.personality or "",
        appearance=row.appearance or "",
        background=row.background or "",
        abilities=list(row.abilities or []),
        goals_short_term=list(row.goals_short_term or []),
        goals_long_term=list(row.goals_long_term or []),
        relationships=dict(row.relationships or {}),
    )


def _outline_character_voice(c: Any) -> CharacterVoiceHint:
    """传递大纲已有角色资料；大纲没有独立能力字段，不作额外推断。"""
    return CharacterVoiceHint(
        character_id=c.character_id,
        name=c.name,
        role=c.role,
        voice_hint=c.voice_hint,
        personality=getattr(c, "personality", ""),
        appearance=getattr(c, "appearance", ""),
        background=getattr(c, "background", ""),
        goals_long_term=[c.motivation] if getattr(c, "motivation", "") else [],
        relationships=dict(getattr(c, "relationships_brief", {})),
    )


def _select_character_voice_hints(
    *,
    workspace_id: str,
    voice_by_id: dict[str, CharacterVoiceHint],
    outline_characters: CharactersOutput,
    required_character_ids: list[str],
) -> list[CharacterVoiceHint]:
    """C-7:按相关性选择每章角色提示池并封顶。

    优先级:
    1. 本章 mini_plan.linked_characters(上游已声明要用的角色)
    2. outline 核心角色(书级主干,顺序沿用 characters.json)
    3. SQLite 角色表里的额外角色(通常是长跑中提取出的 NPC,按 ID 稳定排序)

    若 required ID 不存在于 ``voice_by_id``,这里不猜测补卡;C-6 会把该 ID 补入
    related_characters,随后既有引用校验因 "不在 character_voices" 而确定性拦住。
    """
    candidate_ids: list[str] = []
    seen: set[str] = set()

    def add(candidate_id: str) -> None:
        if candidate_id in seen or candidate_id not in voice_by_id:
            return
        candidate_ids.append(candidate_id)
        seen.add(candidate_id)

    for cid in required_character_ids:
        add(cid)
    for c in outline_characters.characters:
        add(c.character_id)
    for cid in sorted(voice_by_id):
        add(cid)

    selected_ids = candidate_ids[:_CHARACTER_VOICE_HINT_MAX]
    if len(candidate_ids) > _CHARACTER_VOICE_HINT_MAX:
        _log.info(
            "chapter_card_character_voices_capped",
            workspace_id=workspace_id,
            max_count=_CHARACTER_VOICE_HINT_MAX,
            candidate_count=len(candidate_ids),
            selected_count=len(selected_ids),
            required_character_ids=required_character_ids,
            omitted_count=len(candidate_ids) - len(selected_ids),
        )
    return [voice_by_id[cid] for cid in selected_ids]


# v1.0.x wave 5 session 4:语义维度词性标签集(jieba POS tag)
_NOUN_POS_TAGS: frozenset[str] = frozenset(
    {"n", "nr", "ns", "nt", "nz", "ng", "nrfg", "nrt", "vn"}
)
"""名词类:n=普通名词 / nr=人名 / ns=地名 / nt=机构 / nz=其他专名 / ng=名词性
语素 / nrfg + nrt=人名变体 / vn=动名词。"""

_KEYWORD_POS_TAGS: frozenset[str] = _NOUN_POS_TAGS | frozenset(
    {"v", "vd", "vg", "a", "ad", "an", "m"}
)
"""内容词类 = 名词类 ∪ 动词类(v/vd/vg)∪ 形容词类(a/ad/an)∪ 数词(m)。
过滤助词 / 介词 / 连词 / 副词等虚词。"""


_DIVERSIFICATION_MAX_RETRY_ATTEMPTS: int = 3
"""v1.0.x wave 6 s2:批量生成同质化检测自动 retry 最大次数上限。

对齐 outline retry wave 5 s2 ``_retry_feedback.py`` 同型上限(LLM agent 重试次数
经验值 ~3)。3 次仍命中 → 保留首次卡 + ``ChapterCard.auto_retry_status='exhausted'``
+ UI 红色标记,用户手动 retry_references 接力(wave 5 s4 单卡入口)。
"""


_MAX_FORBIDDEN_MUST_ACHIEVE_PHRASES: int = 30
"""v1.1 wave 4 session 10(D5):retry 注入 prompt 的 forbidden_must_achieve
禁词清单上界。

**动机**:``_collect_forbidden_must_achieve_from_volume`` 收同卷所有 approved 章
``must_achieve``、batch 同质化 retry 收同 batch 其他卡 + 前 N-1 次失败 must_achieve
—— 卷内章节多 / batch_size > 10 时清单可膨胀到几十条,塞进 prompt banner 既膨胀
token 又稀释信号(LLM 无法对几十条禁词逐一有效避开)。

**取值**:30 ≈ 10 章 × 3 条 must_achieve;batch_size ≤ 10 的常规批不触发截断,
更大批 / 长卷才截断。截断保留**尾部**(最近章节 / batch retry 的本章前几次失败
must_achieve —— 它们放在拼接尾部,是最该避开的,恒被保留)。
"""


def _truncate_forbidden_must_achieve_phrases(
    phrases: list[str],
    *,
    source: str,
    workspace_id: str,
    chapter_id: str | None = None,
    max_phrases: int = _MAX_FORBIDDEN_MUST_ACHIEVE_PHRASES,
) -> list[str]:
    """v1.1 wave 4 session 10(D5):去重 + 截断 forbidden_must_achieve 禁词清单。

    ① **去重保序**(同卷不同章可能有字面相同的 must_achieve,重复注入无意义);
    ② 去重后超 ``max_phrases`` → **保留尾部** ``max_phrases`` 条(调用方把"最该
    避开"的条目放在拼接尾部 —— volume_collect 尾部 ≈ 最近 approved 章;batch
    retry 尾部 = 本章前 N-1 次失败 must_achieve);
    ③ 发生截断时落 ``chapter_card_forbidden_phrases_truncated`` structlog warn
    (可观测 —— 让维护人看到哪些卷 / 章触发了清单膨胀)。

    :param source: 截断来源标识(``"volume_collect"`` / ``"batch_retry"``),落日志。
    :returns: 去重 +(按需)截断后的禁词清单;``<= max_phrases`` 时只去重不截断。
    """
    seen: set[str] = set()
    deduped: list[str] = []
    for phrase in phrases:
        if phrase not in seen:
            seen.add(phrase)
            deduped.append(phrase)
    if len(deduped) <= max_phrases:
        return deduped

    truncated = deduped[-max_phrases:]
    _log.warning(
        "chapter_card_forbidden_phrases_truncated",
        source=source,
        workspace_id=workspace_id,
        chapter_id=chapter_id,
        original_count=len(phrases),
        deduped_count=len(deduped),
        kept_count=len(truncated),
        max_phrases=max_phrases,
    )
    return truncated


# v1.0.x wave 6 s3:上一章正文不可用时的 prev_chapter_tail 占位字面
_PREV_CHAPTER_TAIL_FIRST_CHAPTER: str = "(首章,无上一章)"
"""全书首章(``chapter_id == ch-0001``)的 ``prev_chapter_tail`` 占位字面。

v1.0.x wave 6 s3 沉淀(plan §s3 Q3=A 拍板"简洁明确组"字面)。LLM 自由生成
该字段时,service 层 post-LLM 强制覆盖为本常量,统一前端 ChapterCardReviewPanel
展示,防 LLM hallucinate ``(开篇,无上一章)`` / ``(第一章,无上一章)`` 等不
规范字面(后者尤为危险 — 实证 ch-3 时 LLM 把非首章写成"第一章")。
"""

_PREV_CHAPTER_TAIL_PREV_NOT_APPROVED: str = (
    "(上一章未 approved,正文未生成 — 后续 Writer 写正文时会自动从 final.md 重抽真字面)"
)
"""非首章且上一章 ``final.md`` 不存在时的占位字面。

v1.0.x wave 6 s3 沉淀。语义:① 用户在 ChapterCardReviewPanel 审章节卡阶段看到
本字面就知道是临时占位;② "后续 Writer 写正文时会自动重抽" 暗示 wave 5 s3
既有 ``_maybe_refresh_continuity`` 路径会接管(``ChapterService`` 写回 JSON
SSOT,刷新页面后用户即可看到真字面 800 字)。
"""


def _override_prev_chapter_tail(
    card: ChapterCard,
    *,
    chapter_number: int,
    prev_chapter_tail_input: str | None,
) -> ChapterCard:
    """v1.0.x wave 6 s3:post-LLM service 层强制覆盖 ``prev_chapter_tail`` 字段。

    三档分支(plan §s3 Q3=A 拍板；R9 v3 补上跨卷边界):

    - 全局 ``chapter_id`` 序号为 1 → ``_PREV_CHAPTER_TAIL_FIRST_CHAPTER``；
      只有无法解析旧 ID 时才回退卷内 ``chapter_number == 1``。
    - 非全书首章 + ``prev_chapter_tail_input is None``(上一章 ``final.md``
      不存在)→ ``_PREV_CHAPTER_TAIL_PREV_NOT_APPROVED``
    - 非全书首章 + ``prev_chapter_tail_input is not None``(上一章已
      approved 有 ``final.md``,``_read_prev_chapter_tail`` 已读到真字面 800 字)
      → 用真字面**入参**覆盖(强制对齐 ``derive_context`` 已读真字面,即使 LLM
      自由篡改字面也会被覆盖)

    覆盖逻辑独立于 ChapterCard 其他字段,只动 ``continuity_context.prev_chapter_tail``。
    若 ``card.continuity_context.prev_chapter_tail`` 已等于 override 值,原样返
    (避免无意义的 ``model_copy`` 开销)。

    设计要点:**不动 wave 5 s3 既有 ``_maybe_refresh_continuity`` 写回路径** —
    本 helper 只修出卡阶段的占位字面(LLM 输出值的覆盖),Writer 实
    际写正文时的真值重抽路径(``ChapterService`` 域)不受影响。两者数据流单向
    互补:缺正文时先用占位 → 写正文时再 refresh。
    """
    chapter_seq = chapter_seq_from_id(card.chapter_id)
    is_first_chapter = (
        chapter_seq == 1 if chapter_seq is not None else chapter_number == 1
    )
    if is_first_chapter:
        override = _PREV_CHAPTER_TAIL_FIRST_CHAPTER
    elif prev_chapter_tail_input is None:
        override = _PREV_CHAPTER_TAIL_PREV_NOT_APPROVED
    else:
        override = prev_chapter_tail_input

    if card.continuity_context.prev_chapter_tail == override:
        return card

    new_continuity = card.continuity_context.model_copy(
        update={"prev_chapter_tail": override}
    )
    return card.model_copy(update={"continuity_context": new_continuity})


@lru_cache(maxsize=4096)
def _extract_char_set(text: str) -> frozenset[str]:
    """字面字符集(cache:同一 must_achieve 字符串在 batch 内多次 pairwise 复用)。"""
    return frozenset(text)


@lru_cache(maxsize=4096)
def _extract_nouns(text: str) -> frozenset[str]:
    """jieba 分词 → 关键名词 frozenset(cache:同 batch pairwise 反复用)。"""
    return frozenset(w.word for w in _pseg.cut(text) if w.flag in _NOUN_POS_TAGS)


@lru_cache(maxsize=4096)
def _extract_keywords(text: str) -> frozenset[str]:
    """jieba 分词 → 内容词 frozenset(名词+动词+形容词+数词,过滤虚词)。

    plan §s4 字面"三元组(SVO)"维度的工程化简化 — jieba 无依存解析,严格
    SVO 抽取在 LLM 输出风格下不稳定;改用"内容词集合 Jaccard"语义同型
    (动词+名词共现高 → 关键词集合重合高),实证 luo3 ch-2/3 仍命中。
    """
    return frozenset(w.word for w in _pseg.cut(text) if w.flag in _KEYWORD_POS_TAGS)


def _jaccard(a: frozenset[str], b: frozenset[str]) -> float:
    """普通 Jaccard;两侧均空 → 0.0(避空集 / 1.0 误判)。"""
    if not (a | b):
        return 0.0
    return len(a & b) / len(a | b)


def _compute_must_achieve_overlap_ratio(a: list[str], b: list[str]) -> float:
    """v1.0.x wave 4 session 4 立 → wave 5 session 4 升级:must_achieve 三维语义重合率。

    **wave 5 session 4 升级**(plan §s4 j 件):字面 Jaccard 漏检语义重合(luo3
    ch-2/3 都 "首次记忆置换" 字面 Jaccard 0.2626 < 0.5 漏检);改成**逐条
    pairwise 三维 max**:

    - 维度 1 — **字面字符 Jaccard**(wave 4 同型,保留同源校准锚点)
    - 维度 2 — **关键名词 Jaccard**(jieba.posseg 抽名词类 ``_NOUN_POS_TAGS``)
    - 维度 3 — **关键词 Jaccard**(名词+动词+形容词+数词 ``_KEYWORD_POS_TAGS``,
      plan §s4 字面"三元组"维度的工程化简化 — jieba 无依存解析,严格 SVO
      抽取在 LLM 输出风格下不稳定)

    最终 = max over 所有 (a_i, b_j) 条对的三维 max(任一维度任一对超阈值即
    flagged;比"集合聚合维度"更敏感,避免同主题章节的高频共享名词稀释配对
    信号)。

    重合率 ∈ [0, 1],越大同质化越严重。两侧任一为空 list → 0.0(无内容,
    无判定意义)。接口签名向后兼容(wave 4 batch 检测 + retry 路径零改动),
    单测 / batch 检测在新设计下行为同 wave 4(实证 ``test_partial_overlap``
    + ``test_disjoint_chinese`` 仍稳)。

    沿用 wave 4 ``scripts/wave4_session3b_real_llm_verify.py:_char_overlap_ratio``
    同型字面维度,与实证脚本对照校准时维度 1 仍可直接对齐。
    """
    if not a or not b:
        return 0.0

    best = 0.0
    for sa in a:
        chars_sa = _extract_char_set(sa)
        nouns_sa = _extract_nouns(sa)
        keywords_sa = _extract_keywords(sa)
        for sb in b:
            ratio = max(
                _jaccard(chars_sa, _extract_char_set(sb)),
                _jaccard(nouns_sa, _extract_nouns(sb)),
                _jaccard(keywords_sa, _extract_keywords(sb)),
            )
            if ratio > best:
                best = ratio
                if best >= 1.0:
                    return best
    return best


def _detect_must_achieve_diversification(
    cards: list[ChapterCard],
    *,
    threshold: float,
) -> set[int]:
    """v1.0.x wave 4 session 4:后置同质化检测器。

    给定同批 batch 内已生成卡列表,配对计算 must_achieve 字符集 Jaccard 重合率;
    任一配对 ≥ ``threshold`` 视为同质化,两端章索引(``cards`` 列表 0-based 位置)
    都进 ``flagged`` set 返回。

    :param cards: 同 batch 已生成的 ``ChapterCard`` 列表。
    :param threshold: 重合率阈值(0..1),典型 0.5(对应
        ``CardGeneratorConfig.diversification_threshold`` 默认)。
    :return: 需 retry 的章 0-based 索引集合(``set[int]``);空 set = 全 batch
        通过检测,无需 retry。

    注意:不在此处直接落 structlog 事件,留给 caller 的 retry 流程一并落事件
    (含 retry 前 / 后 overlap 数据,语义更连贯)。
    """
    if len(cards) < 2 or threshold <= 0:
        return set()
    flagged: set[int] = set()
    for i in range(len(cards)):
        for j in range(i + 1, len(cards)):
            ratio = _compute_must_achieve_overlap_ratio(
                cards[i].must_achieve, cards[j].must_achieve
            )
            if ratio >= threshold:
                flagged.add(i)
                flagged.add(j)
    return flagged


# =============================================================================
# 异常类型
# =============================================================================


class ChapterCardServiceError(Exception):
    """chapter_card_service 通用错误(派生上下文失败 / 落库失败)。"""


class ChapterCardContextDerivationError(ChapterCardServiceError):
    """``derive_context`` 派生失败(outline 缺段 / volumes 表缺行 /
    chapter_number 越界 / arc 未覆盖等)。"""


class CardNotFoundError(ChapterCardServiceError):
    """``transition_card_status`` 时 ``chapter_cards`` ORM 行不存在(章节卡尚未生成)。

    P5-B-07 引入。路由层翻译为 ``CHAPTER_CARD_NOT_FOUND (404)``(对齐既有
    ``ChapterCardNotFound`` 文件 SSOT 缺失 404 语义,二者并集覆盖)。
    """


class CardInvalidStateTransitionError(ChapterCardServiceError):
    """``transition_card_status`` 时当前 ``card_status`` 不允许该操作。

    P5-B-07 引入(决策 6=C 状态机)。路由层翻译为
    ``CARD_INVALID_STATE_TRANSITION (409)``。``details`` 字段:
    ``chapter_id`` / ``current_status`` / ``action`` / ``allowed_transitions``。
    """

    def __init__(
        self,
        *,
        chapter_id: str,
        current_status: str,
        action: str,
        allowed_sources: tuple[str, ...],
    ) -> None:
        self.chapter_id = chapter_id
        self.current_status = current_status
        self.action = action
        self.allowed_sources = allowed_sources
        super().__init__(
            f"章节卡状态不允许该操作: chapter_id={chapter_id} "
            f"current_status={current_status!r} action={action!r} "
            f"allowed_sources={list(allowed_sources)!r}"
        )


class CardContinuityLedgerStaleError(ChapterCardServiceError):
    """卡 approve 前 ledger 缺失或 provenance 与当前上一章不一致。"""

    def __init__(
        self,
        *,
        chapter_id: str,
        reason: str,
        frozen: ContinuityLedgerProvenance | None,
        current: ContinuityLedgerProvenance | None,
    ) -> None:
        self.chapter_id = chapter_id
        self.reason = reason
        self.frozen = frozen
        self.current = current
        super().__init__(f"章节卡连续性小账本已陈旧: chapter_id={chapter_id}: {reason}")


class CardContinuityBridgeRequiredError(ChapterCardServiceError):
    """卡 approve 前发现即时未完动作跨跳转缺少明确承接。"""

    def __init__(
        self,
        *,
        chapter_id: str,
        reason: str,
        required_actions: list[str] | None = None,
    ) -> None:
        self.chapter_id = chapter_id
        self.reason = reason
        self.required_actions = list(required_actions or [])
        super().__init__(f"章节卡缺少跨章承接: chapter_id={chapter_id}: {reason}")


# =============================================================================
# 状态机转换规则集中表(P5-B-07,对齐决策 6=C + 启动指令字面)
# =============================================================================

CardAction = Literal["approve", "reject"]
"""审核流入口动作枚举(P5-B-07)。"""

_CARD_TRANSITION_RULES: dict[CardAction, tuple[tuple[str, ...], str]] = {
    "approve": (("draft", "pending_review", "rejected"), "approved"),
    "reject": (("draft", "pending_review", "approved"), "rejected"),
}
"""``card_status`` 4 档转换规则集中表(决策 6=C 字面 + 启动指令字面)。

``{action: (allowed_sources, target_status)}``:

- ``approve``:合法源 ``draft / pending_review / rejected``;目标 ``approved``。
  非法源 ``approved`` → ``CardInvalidStateTransitionError`` (重复操作)。
- ``reject``:合法源 ``draft / pending_review / approved``;目标 ``rejected``。
  非法源 ``rejected`` → ``CardInvalidStateTransitionError`` (重复操作)。

未来 P6+ 引入 ``force_review`` / ``unblock`` 等动作时,扩本字典即可
(对齐 ``CHAPTER_STATUS_VALUES`` + Reviewer ``_REVIEWER_VERDICT_TRANSITIONS``
同源单一真相表模式)。
"""


# =============================================================================
# Service
# =============================================================================


class ChapterCardService:
    """章节卡 service(派生上下文 + 单卡 agent + SSOT/ORM 同步)。

    依赖:
    - ``workspace_service``:校验 workspace + 读 ``outline/`` 文件根
    - ``chapter_service``:复用 ``save_chapter_card``(文件 SSOT 原子写)+
      ``card_path_for`` (路径)。本 service **不**重复实现 SSOT 写。
    - ``session_factory``:开 ``data/novel.db`` Session(读 volumes /
      characters / plot_threads / 写 chapter_cards)。
    - ``router_factory`` / ``client`` + ``model``:wire ``ModelRouter`` /
      legacy LLMClient(对齐 ``OutlineService`` 同源)。
    - ``ws_manager``:任务 stage event 推送(可选)。
    - ``card_config``:``CardGeneratorConfig``，控制相似度阈值与样式控制器。
    - ``prompts_dir``:prompt 根目录覆盖(测试用)。
    """

    def __init__(
        self,
        *,
        workspace_service: WorkspaceService,
        chapter_service: ChapterService,
        session_factory: SessionFactory | None = None,
        client: LLMClient | None = None,
        model: str | None = None,
        router_factory: Callable[[str], ModelRouter] | None = None,
        vector_store_factory: VectorStoreFactory | None = None,
        ws_manager: WebSocketManager | None = None,
        card_config: CardGeneratorConfig | None = None,
        prompts_dir: Path | None = None,
    ) -> None:
        self._workspace_service = workspace_service
        self._chapter_service = chapter_service
        self._session_factory = session_factory
        self._client = client
        self._model = model
        self._router_factory = router_factory
        self._vector_store_factory = vector_store_factory
        self._ws_manager = ws_manager
        self._card_config = card_config or CardGeneratorConfig()
        self._prompts_dir = prompts_dir

    # ---- 路径辅助 -----------------------------------------------------------

    def _outline_dir(self, workspace_id: str) -> Path:
        return (
            self._workspace_service.workspaces_root / workspace_id / "data" / "outline"
        )

    def _build_router(self, workspace_id: str):  # type: ignore[no-untyped-def]
        if self._router_factory is None:
            return None
        return self._router_factory(workspace_id)

    def _load_generation_config(
        self,
        workspace_id: str,
        *,
        engine_base_override: GenerationConfig | None = None,
    ) -> GenerationConfig:
        """读工作区级 generation 生成参数配置(v1.1 L1,新卡 word_count / max_rewrites / plot_node_budget 兜底来源)。

        构造一个临时 ``ConfigService``(共用 ``WorkspaceService.workspaces_root``,
        engine config 走默认 ``settings.config_yaml_path``)读取合并后配置。
        ``engine_base_override``(v1.1 I2.1)给定时把风格模板生成默认作为引擎基线
        注入,project.yaml L1 用户覆盖仍字段级优先。
        任何异常(project.yaml 语法错 / 字段非法等)→ 退回 ``GenerationConfig()``
        引擎/模型默认 + warn log,**绝不因生成参数配置问题阻断章节卡生成**
        (对齐 mini_plan / prev_card 读失败 fallback 同型容错策略)。
        """
        # 局部 import 避免模块级循环依赖(config_service 不依赖本模块,但保持对称)。
        from src.services.config_service import ConfigService

        try:
            cfg_service = ConfigService(
                workspaces_root=self._workspace_service.workspaces_root
            )
            return cfg_service.load_generation_config(
                workspace_id, engine_base_override=engine_base_override
            )
        except Exception as exc:  # noqa: BLE001 — 容错兜底,不阻断生成
            _log.warning(
                "generation_config_load_failed_fallback_default",
                workspace_id=workspace_id,
                error_type=type(exc).__name__,
                error_message=str(exc),
            )
            # I2.1:有风格基线时退回风格基线(而非纯模型默认),保留模板生成默认。
            return engine_base_override or GenerationConfig()

    def resolve_effective_progression_consistency(
        self, workspace_id: str
    ) -> tuple[bool, str]:
        """模块②(w5-s10)境界一致性是否对本工作区生效 + 题材标签。

        门控 = style_template ∈ 3 题材(古典仙侠/搞笑爽文/热血战斗)且配置 mode 解析为 on
        (auto→题材默认:古典仙侠 ON、爽文/热血 OFF)。返回 ``(enabled, label)``,
        label = 境界/战力(供 writer 注入 + extractor 抽取标签)。供 ChapterService 解析后
        显式传入 ``write_chapter`` / ``extract``。
        """
        workspace = self._workspace_service.info(workspace_id)
        style = getattr(workspace, "style_template", None)
        mode = self._load_generation_config(workspace_id).progression_consistency
        enabled = resolve_progression_consistency(style_template=style, mode=mode)
        return enabled, progression_label(style)

    def resolve_effective_breakthrough_payoff(self, workspace_id: str) -> bool:
        """模块①(w5-s10)升级可感化是否对本工作区生效。

        门控 = style_template ∈ 3 题材且配置 mode 解析为 on(auto→3 题材全 ON)。供
        outline_service(mini_plan gating)解析后决定是否注入约束 12 + 填 breakthrough_payoff。
        """
        workspace = self._workspace_service.info(workspace_id)
        style = getattr(workspace, "style_template", None)
        mode = self._load_generation_config(workspace_id).breakthrough_payoff
        return resolve_breakthrough_payoff(style_template=style, mode=mode)

    # ---- 派生器 -------------------------------------------------------------

    def derive_context(
        self,
        workspace_id: str,
        chapter_id: str,
        *,
        chapter_number: int,
        volume_id: str,
    ) -> ChapterCardGenerationContext:
        """从 outline + volumes / characters / plot_threads / 上一章 final
        派生 ``ChapterCardGenerationContext``。

        :raises ChapterCardContextDerivationError: outline 缺段 / volumes 表
            缺行 / chapter_number 不在任何 arc 范围内。
        """
        workspace = self._workspace_service.info(workspace_id)

        world_path = self._outline_dir(workspace_id) / "world.json"
        characters_path = self._outline_dir(workspace_id) / "characters.json"
        plot_path = self._outline_dir(workspace_id) / "plot.json"
        volume_plans_path = self._outline_dir(workspace_id) / "volume_plans.json"

        try:
            setting_contract = load_setting_contract(world_path)
        except SettingContractLoadError as exc:
            raise ChapterCardContextDerivationError(str(exc)) from exc

        for p in (characters_path, plot_path, volume_plans_path):
            if not p.is_file():
                raise ChapterCardContextDerivationError(
                    f"outline 4 段未生成完整:缺 {p.name}"
                )

        characters_outline = CharactersOutput.model_validate(
            json.loads(characters_path.read_text(encoding="utf-8"))
        )
        plot = PlotOutput.model_validate(
            json.loads(plot_path.read_text(encoding="utf-8"))
        )
        volume_plans = VolumePlansOutput.model_validate(
            json.loads(volume_plans_path.read_text(encoding="utf-8"))
        )

        # 找到 volume_plan 项 + 计算 chapter_number 所在 arc + tension。
        volume_plan = next(
            (v for v in volume_plans.volumes if v.volume_id == volume_id), None
        )
        if volume_plan is None:
            raise ChapterCardContextDerivationError(
                f"volume_plans 内无 volume_id={volume_id!r}"
            )
        if not (1 <= chapter_number <= volume_plan.chapter_count):
            raise ChapterCardContextDerivationError(
                f"chapter_number={chapter_number} 不在 volume {volume_id} "
                f"的 [1, {volume_plan.chapter_count}] 范围"
            )

        arc = next(
            (
                a
                for a in volume_plan.arcs
                if a.chapter_range[0] <= chapter_number <= a.chapter_range[1]
            ),
            None,
        )
        if arc is None:
            raise ChapterCardContextDerivationError(
                f"chapter_number={chapter_number} 未被 volume {volume_id} "
                f"的任何 arc 覆盖(outline 数据异常)"
            )

        # §2.137:beat 合同必须进入共享单卡 context，供生成与重试一致消费。
        # 旧 outline 允许 arc.beats 为空，保持空合同兼容；新 outline 的结构合同另行
        # 保证 beats 连续覆盖，本处只选择当前章所属项，不新造状态或 Judge。
        beat = next(
            (
                item
                for item in arc.beats
                if item.chapter_range[0] <= chapter_number <= item.chapter_range[1]
            ),
            None,
        )

        tension_level = volume_plan.tension_curve[chapter_number - 1]
        is_arc_finale = chapter_number == arc.chapter_range[1]

        # v1.0.x wave 4 session 2:读 mini_plan.json 对应章项(可选)。
        # 缺失(wave 4 前生成的旧工作区)→ None,prompt 拼装走 arc_summary 老路径
        # (对齐维护人 2026-05-21 Q1=A 拍板:Fallback + warn)。
        mini_plan_item = self._read_mini_plan_item(
            workspace_id=workspace_id, chapter_id=chapter_id
        )
        if setting_contract.story_intent is not None and (
            mini_plan_item is None or mini_plan_item.narrative_context is None
            or mini_plan_item.narrative_context.story_intent != setting_contract.story_intent
        ):
            raise ChapterCardContextDerivationError("新叙事大纲缺少同源小纲材料，不能回退旧出卡路径")
        chapter_seq_for_plan = chapter_seq_from_id(chapter_id)
        total_planned_chapters = sum(v.chapter_count for v in volume_plans.volumes)
        next_mini_plan_item = (
            self._read_mini_plan_item(
                workspace_id=workspace_id,
                chapter_id=f"ch-{chapter_seq_for_plan + 1:04d}",
            )
            if chapter_seq_for_plan is not None
            and chapter_seq_for_plan < total_planned_chapters
            else None
        )
        mini_plan_linked_characters = (
            list(mini_plan_item.linked_characters) if mini_plan_item else []
        )
        mini_plan_linked_threads = (
            list(mini_plan_item.linked_threads) if mini_plan_item else []
        )

        # 派生 character_voices:优先 SQLite Character 表(更稳),回退 outline。
        # C-7:DB 角色表可能随长跑涌现 NPC 膨胀,不得每章全量塞入出卡上下文;
        # mini_plan 已声明角色优先进入有限池,缺卡/超量时后续引用校验会确定性拦住。
        character_voices = self._derive_character_voices(
            workspace_id=workspace_id,
            outline_characters=characters_outline,
            required_character_ids=mini_plan_linked_characters,
        )
        plot_threads = self._derive_plot_threads(
            workspace_id=workspace_id,
            outline_plot=plot,
            current_volume_id=volume_id,
            current_arc_id=arc.arc_id,
            query_text=" | ".join(
                part
                for part in (
                    mini_plan_item.one_line_event if mini_plan_item else "",
                    arc.summary,
                    volume_plan.summary,
                    plot.core_conflict,
                    " ".join(mini_plan_linked_characters),
                )
                if part
            ),
        )

        # R4:若上一章同步小账本已就绪，直接前章上下文只读实际 final 事实并冻结
        # provenance。outline:commit 预生成草稿时 ledger 尚不存在，保留旧计划
        # fallback 仅供草稿展示；approve / Writer 两处硬闸会拒绝无 provenance 卡。
        chapter_seq = chapter_seq_from_id(chapter_id)
        is_first_chapter = (
            chapter_seq == 1 if chapter_seq is not None else chapter_number == 1
        )
        prev_chapter_id = (
            f"ch-{chapter_seq - 1:04d}"
            if chapter_seq is not None and chapter_seq > 1
            else f"ch-{chapter_number - 1:04d}"
        )
        prev_ledger: ChapterSummary | None = None
        prev_ledger_provenance: ContinuityLedgerProvenance | None = None
        if not is_first_chapter and self._session_factory is not None:
            paths = WorkspacePaths(
                workspace_id,
                workspaces_root=self._workspace_service.workspaces_root,
            )
            from src.services.chapter_service import ChapterServiceError

            try:
                self._chapter_service.require_ready_for_next_chapter(
                    workspace_id,
                    prev_chapter_id,
                )
                prev_ledger = load_validated_continuity_ledger(
                    workspace_id=workspace_id,
                    chapter_id=prev_chapter_id,
                    paths=paths,
                    session_factory=self._session_factory,
                )
                prev_ledger_provenance = continuity_ledger_provenance(prev_ledger)
            except (ChapterServiceError, ContinuityLedgerValidationError) as exc:
                _log.info(
                    "chapter_card_prev_ledger_not_ready_for_draft",
                    workspace_id=workspace_id,
                    chapter_id=chapter_id,
                    previous_chapter_id=prev_chapter_id,
                    reason=str(exc),
                )

        if is_first_chapter:
            prev_chapter_summary = None
            prev_chapter_tail = None
            prev_pov_character = None
            prev_unfinished_actions: list[str] = []
        elif prev_ledger is not None:
            prev_chapter_summary = prev_ledger.summary_text
            prev_chapter_tail = prev_ledger.raw_final_tail
            prev_pov_character = prev_ledger.pov_character
            prev_unfinished_actions = list(prev_ledger.unfinished_actions)
        else:
            prev_chapter_tail = self._read_prev_chapter_tail(
                workspace_id=workspace_id, prev_chapter_id=prev_chapter_id
            )
            prev_card = self._read_prev_chapter_card_ssot(
                workspace_id=workspace_id, prev_chapter_id=prev_chapter_id
            )
            if prev_card is not None:
                prev_chapter_summary = prev_card.next_chapter_seed
                prev_pov_character = prev_card.pov_character
                prev_unfinished_actions = list(prev_card.must_unresolved)
            else:
                prev_chapter_summary = None
                prev_pov_character = None
                prev_unfinished_actions = []

        # v1.0.x wave 4 session 3b:读前 N=10 章 ChapterSummary 序列(长程事实
        # 上下文)。首章 / 无 session_factory / 表无行 → 空 list,对齐 wave 4 前
        # 老语义。
        prev_chapter_summaries = self._read_prev_chapter_summaries(
            workspace_id=workspace_id,
            chapter_id=chapter_id,
            n=_PREV_CHAPTER_SUMMARIES_DEFAULT_N,
        )
        if prev_ledger is not None:
            # 上一章同步 ledger 是权威值：同 ID 就地替换；若旧工作区摘要缺行则
            # 补入后仍只保留最近 N 条。不能把“查询 N 条 + append 上一章”悄悄
            # 膨胀成 N+1。
            summaries_by_id = {
                summary.chapter_id: summary for summary in prev_chapter_summaries
            }
            summaries_by_id[prev_ledger.chapter_id] = prev_ledger
            prev_chapter_summaries = sorted(
                summaries_by_id.values(), key=lambda summary: summary.chapter_id
            )[-_PREV_CHAPTER_SUMMARIES_DEFAULT_N:]

        warning_watchlist_section = self._read_planner_warning_watchlist(
            workspace_id=workspace_id,
            chapter_id=chapter_id,
        )

        # v1.1 L1 Q16:读工作区级 generation 默认,作新卡 word_count / max_rewrites / plot_node_budget 兜底。
        # I2.1:工作区选了风格模板 → 把模板生成默认作引擎基线注入(project.yaml L1 覆盖仍优先);
        # must_achieve_max 是软提示(非 GenerationConfig 字段),单独从注册表读注入 prompt。
        style_gen = (
            STYLE_GENERATION_DEFAULTS.get(workspace.style_template)
            if workspace.style_template is not None
            else None
        )
        book_generation = self._load_generation_config(workspace_id)
        style_base = (
            book_generation.model_copy(update={
                **({"word_count_min": style_gen.word_count_min,
                    "word_count_max": style_gen.word_count_max}
                   if book_generation.word_count_policy_version != 2 else {}),
                "plot_node_budget": style_gen.plot_node_budget,
            })
            if style_gen is not None
            else None
        )
        generation_config = self._load_generation_config(
            workspace_id, engine_base_override=style_base
        )
        core_state_enabled = (
            generation_config.core_state_hard_enabled
            and self._session_factory is not None
        )
        core_state_projection = None
        if core_state_enabled:
            assert self._session_factory is not None
            core_state_projection = CoreStateService(
                session_factory=self._session_factory
            ).load_projection(
                workspace_id,
                before_chapter=chapter_seq_from_id(chapter_id),
            )
        elif generation_config.core_state_hard_enabled:
            _log.warning(
                "core_state_contract_disabled_without_session",
                workspace_id=workspace_id,
                chapter_id=chapter_id,
                reason="legacy service path has no durable SQLite session",
            )

        # v1.1 wave 5 s1(M1)avoid-list 注入已断开(2026-06-05):原型 A/B 实证 M1
        # 信号源 + 注入点双错位,对读者套话无效 → M1 从管线下线(详 postmortem)。

        return ChapterCardGenerationContext(
            workspace_id=workspace_id,
            chapter_id=chapter_id,
            volume_id=volume_id,
            chapter_number=chapter_number,
            arc_id=arc.arc_id,
            arc_type=arc.arc_type,
            arc_phase=arc.phase,
            arc_summary=arc.summary,
            is_arc_finale=is_arc_finale,
            tension_level=tension_level,
            beat_id=beat.beat_id if beat is not None else None,
            beat_summary=beat.summary if beat is not None else "",
            beat_must_advance=(list(beat.must_advance) if beat is not None else []),
            beat_forbidden_advance=(
                list(beat.forbidden_advance) if beat is not None else []
            ),
            setting_contract=setting_contract,
            core_conflict=plot.core_conflict,
            volume_summary=volume_plan.summary,
            volume_dramatic_promise=volume_plan.dramatic_promise,
            beat_dramatic_contract=(
                beat.dramatic_contract
                if beat is not None
                and mini_plan_item is not None
                and mini_plan_item.dramatic_contract is not None
                else None
            ),
            mini_plan_dramatic_contract=(
                mini_plan_item.dramatic_contract if mini_plan_item else None
            ),
            prev_chapter_summary=prev_chapter_summary,
            prev_chapter_tail=prev_chapter_tail,
            prev_pov_character=prev_pov_character,
            prev_unfinished_actions=prev_unfinished_actions,
            continuity_ledger_provenance=prev_ledger_provenance,
            warning_watchlist_section=warning_watchlist_section,
            core_state_enabled=core_state_enabled,
            core_state_projection=core_state_projection,
            character_voices=character_voices,
            plot_threads=plot_threads,
            mini_plan_event=mini_plan_item.one_line_event if mini_plan_item else None,
            narrative_context=mini_plan_item.narrative_context if mini_plan_item else None,
            mini_plan_info_increment=mini_plan_item.info_increment if mini_plan_item else "",
            next_dramatic_contract=next_mini_plan_item.dramatic_contract if next_mini_plan_item else None,
            next_event_contract=(
                next_mini_plan_item.narrative_context.event_contract
                if next_mini_plan_item and next_mini_plan_item.narrative_context else None
            ),
            next_mini_plan_event=(
                next_mini_plan_item.one_line_event if next_mini_plan_item else None
            ),
            # v2 W3.5 C-6:mini_plan linked_characters → ctx,下游出卡 agent
            # 在校验前确定性补齐进 ChapterCard.related_characters。
            mini_plan_linked_characters=mini_plan_linked_characters,
            # mini_plan 已经为本章分配的主伏笔必须确定性进入 card；不能让 card LLM
            # 从所有活跃主伏笔中另选一条后静默丢失上游身份。
            mini_plan_linked_threads=mini_plan_linked_threads,
            # G2(v1.1 wave 4 session 2):mini_plan callback_anchors → ctx,
            # 下游 _assemble_chapter_card 确定性派生为 ChapterCard.callback_to。
            callback_anchors=(
                list(mini_plan_item.callback_anchors) if mini_plan_item else []
            ),
            # M4'''(v1.1 wave 5 session 6):mini_plan chapter_focus → ctx,
            # 下游 _assemble_chapter_card 确定性透传为 ChapterCard.chapter_focus。
            chapter_focus=(mini_plan_item.chapter_focus if mini_plan_item else ""),
            # M5(v1.1 wave 5 session 7):mini_plan arc_beats → ctx,
            # 下游 _assemble_chapter_card 确定性透传为 ChapterCard.arc_beats。
            arc_beats=(dict(mini_plan_item.arc_beats) if mini_plan_item else {}),
            # M4''-轴A(v1.1 wave 5 session 8):mini_plan interaction_focus → ctx,
            # 下游 _assemble_chapter_card 确定性透传为 ChapterCard.interaction_focus。
            interaction_focus=(
                mini_plan_item.interaction_focus if mini_plan_item else ""
            ),
            # 模块①(v1.1 wave 5 session 10):mini_plan breakthrough_payoff → ctx,
            # 下游 _assemble_chapter_card 确定性透传为 ChapterCard.breakthrough_payoff。
            breakthrough_payoff=(
                mini_plan_item.breakthrough_payoff if mini_plan_item else ""
            ),
            prev_chapter_summaries=prev_chapter_summaries,
            word_count_policy_version=generation_config.word_count_policy_version,
            default_word_count_min=generation_config.word_count_min,
            default_word_count_max=generation_config.word_count_max,
            default_max_rewrites=generation_config.max_rewrites,
            default_plot_node_budget=generation_config.plot_node_budget,
            default_must_achieve_max=(
                style_gen.must_achieve_max if style_gen is not None else None
            ),
        )

    def _read_planner_warning_watchlist(
        self, *, workspace_id: str, chapter_id: str
    ) -> str:
        """读取已批准终稿、仍在作用域内的 Planner SOFT WARN。

        无 session 的 legacy/纯单测路径保持空；真实产品路径一旦发现 final WARN
        账本损坏就硬停出卡，避免把未知历史信号静默塞进新的付费提示词。
        """
        if self._session_factory is None:
            return ""
        try:
            session = self._session_factory(workspace_id)
        except Exception as exc:  # noqa: BLE001 - 产品账本不可读必须硬停
            raise ChapterCardContextDerivationError(
                f"无法读取已批准终稿 WARN 账本: {chapter_id}"
            ) from exc
        try:
            warnings = load_approved_final_warnings(
                session,
                workspace_id=workspace_id,
                target_chapter_id=chapter_id,
                audience="planner",
            )
        except WarningRoutingError as exc:
            raise ChapterCardContextDerivationError(str(exc)) from exc
        except Exception as exc:  # noqa: BLE001 - 查询失败也不能 fail-open
            raise ChapterCardContextDerivationError(
                f"无法读取已批准终稿 WARN 账本: {chapter_id}"
            ) from exc
        finally:
            session.close()
        return render_warning_watchlist(warnings, audience="planner")

    def _derive_character_voices(
        self,
        *,
        workspace_id: str,
        outline_characters: CharactersOutput,
        required_character_ids: list[str] | None = None,
    ) -> list[CharacterVoiceHint]:
        """先从 ``characters`` SQLite 表读;表为空或 session 无 → 用 outline 派生。

        SQLite 表存在时 (P2-B-13 之后) 才会有行,P5-B-06 mock 流程下表常空。
        C-7 起对每章提示池做相关性排序 + 封顶,防长跑角色表全量污染出卡上下文。
        """
        required_ids = _dedupe_character_ids(required_character_ids or [])
        if self._session_factory is not None:
            session = self._session_factory(workspace_id)
            try:
                rows = (
                    session.query(CharacterORM)
                    .filter(CharacterORM.workspace_id == workspace_id)
                    .order_by(CharacterORM.character_id.asc())
                    .all()
                )
            finally:
                session.close()
            if rows:
                voice_by_id = {
                    c.character_id: _outline_character_voice(c)
                    for c in outline_characters.characters
                }
                voice_by_id.update(
                    {row.character_id: _character_voice_from_orm(row) for row in rows}
                )
                return _select_character_voice_hints(
                    workspace_id=workspace_id,
                    voice_by_id=voice_by_id,
                    outline_characters=outline_characters,
                    required_character_ids=required_ids,
                )
        # 回退到 outline 阶段产出
        voice_by_id = {
            c.character_id: _outline_character_voice(c)
            for c in outline_characters.characters
        }
        return _select_character_voice_hints(
            workspace_id=workspace_id,
            voice_by_id=voice_by_id,
            outline_characters=outline_characters,
            required_character_ids=required_ids,
        )

    def _derive_plot_threads(
        self,
        *,
        workspace_id: str,
        outline_plot: PlotOutput,
        current_volume_id: str,
        current_arc_id: str,
        query_text: str,
    ) -> list[PlotThreadSummary]:
        """大纲主伏笔全保留，正文临时伏笔只取当前作用域内语义 top-K。

        ``outline/manual`` 行沿用旧行为：所有仍活跃项都进入出卡上下文，不参加
        临时层门槛、过期或 top-K。并且始终与 ``plot.json.long_threads`` 取并集，
        防 outline commit 某一行局部落库失败后主伏笔从章节卡消失。

        ``emergent`` 行才应用 ``arc_end / volume_end / never`` 休眠与本地向量
        top-3。向量不可用时按重要度/最近更新有界回退，绝不把全部临时条目灌进卡。
        """
        if self._session_factory is None:
            return [
                PlotThreadSummary(
                    thread_id=t.thread_id,
                    title=t.title,
                    description=t.description,
                    status="planted",
                    origin="outline",
                    expiry_policy="never",
                    source_evidence=None,
                )
                for t in outline_plot.long_threads
            ]

        session = self._session_factory(workspace_id)
        try:
            all_rows = (
                session.query(PlotThreadORM)
                .filter(PlotThreadORM.workspace_id == workspace_id)
                .order_by(PlotThreadORM.created_at.asc(), PlotThreadORM.thread_id.asc())
                .all()
            )
        finally:
            session.close()

        known_row_ids = {row.thread_id for row in all_rows}
        active_rows = [
            row
            for row in all_rows
            if row.status not in {"resolved", "abandoned"} and row.revoked_at is None
        ]
        planned_rows = [
            row
            for row in active_rows
            if getattr(row, "origin", "outline") != "emergent"
        ]
        emergent_rows = [
            row
            for row in active_rows
            if getattr(row, "origin", "outline") == "emergent"
        ]

        scope_by_chapter = self._read_mini_plan_scope_index(workspace_id)
        scoped_emergent_rows = [
            row
            for row in emergent_rows
            if self._emergent_plot_thread_is_in_scope(
                row=row,
                current_volume_id=current_volume_id,
                current_arc_id=current_arc_id,
                scope_by_chapter=scope_by_chapter,
            )
        ]
        selected_emergent_rows = self._select_emergent_plot_threads(
            workspace_id=workspace_id,
            rows=scoped_emergent_rows,
            query_text=query_text,
        )

        summaries = [self._plot_thread_summary_from_orm(row) for row in planned_rows]
        # 数据库没有对应行时才用 outline 补齐；已有 resolved/abandoned/revoked 行
        # 代表生命周期已有权威状态，不能被 plot.json 的静态 planted 值复活。
        summaries.extend(
            PlotThreadSummary(
                thread_id=thread.thread_id,
                title=thread.title,
                description=thread.description,
                status="planted",
                origin="outline",
                expiry_policy="never",
                source_evidence=None,
            )
            for thread in outline_plot.long_threads
            if thread.thread_id not in known_row_ids
        )
        summaries.extend(
            self._plot_thread_summary_from_orm(row) for row in selected_emergent_rows
        )
        return summaries

    @staticmethod
    def _plot_thread_summary_from_orm(row: PlotThreadORM) -> PlotThreadSummary:
        return PlotThreadSummary(
            thread_id=row.thread_id,
            title=row.title,
            description=row.description,
            status=row.status,
            origin=getattr(row, "origin", "outline"),
            expiry_policy=getattr(row, "expiry_policy", "never"),
            source_evidence=getattr(row, "source_evidence", None),
        )

    def _read_mini_plan_scope_index(
        self, workspace_id: str
    ) -> dict[str, tuple[str, str]]:
        """一次读取 mini_plan，返回 chapter_id -> (volume_id, arc_id)。

        旧工作区缺文件或文件损坏时返回空；临时伏笔随后 fail-safe 保留，而不是
        因无法证明已过期就静默丢失。
        """
        path = self._outline_dir(workspace_id) / "mini_plan.json"
        if not path.is_file():
            return {}
        try:
            plan = MiniPlanOutput.model_validate(
                json.loads(path.read_text(encoding="utf-8"))
            )
        except Exception as exc:  # noqa: BLE001 - 仅休眠优化，失败时保留候选
            _log.warning(
                "emergent_plot_thread_scope_index_unavailable",
                workspace_id=workspace_id,
                error_type=type(exc).__name__,
                error_message=str(exc),
            )
            return {}
        return {item.chapter_id: (item.volume_id, item.arc_id) for item in plan.items}

    @staticmethod
    def _emergent_plot_thread_is_in_scope(
        *,
        row: PlotThreadORM,
        current_volume_id: str,
        current_arc_id: str,
        scope_by_chapter: dict[str, tuple[str, str]],
    ) -> bool:
        policy = getattr(row, "expiry_policy", "volume_end")
        if policy == "never":
            return True
        planted_scope = scope_by_chapter.get(row.planted_chapter or "")
        if planted_scope is None:
            return True
        planted_volume_id, planted_arc_id = planted_scope
        if policy == "arc_end":
            return (
                planted_volume_id == current_volume_id
                and planted_arc_id == current_arc_id
            )
        return planted_volume_id == current_volume_id

    def _select_emergent_plot_threads(
        self,
        *,
        workspace_id: str,
        rows: list[PlotThreadORM],
        query_text: str,
    ) -> list[PlotThreadORM]:
        if len(rows) <= _EMERGENT_PLOT_THREAD_TOP_K:
            return rows

        fallback = sorted(
            rows,
            key=lambda row: (
                row.importance,
                row.last_updated_chapter or row.planted_chapter or "",
                row.thread_id,
            ),
            reverse=True,
        )
        if self._vector_store_factory is None:
            return fallback[:_EMERGENT_PLOT_THREAD_TOP_K]

        row_by_id = {row.thread_id: row for row in rows}
        try:
            hits = self._vector_store_factory(workspace_id).query(
                _MEMORY_VECTOR_COLLECTION,
                query_text=query_text or "当前章节剧情与待收口临时伏笔",
                top_k=_EMERGENT_PLOT_THREAD_TOP_K,
                where={
                    "$and": [
                        {"entity_type": "plot_thread"},
                        {"entity_id": {"$in": sorted(row_by_id)}},
                    ]
                },
            )
        except Exception as exc:  # noqa: BLE001 - 本地检索失败有界降级
            _log.warning(
                "emergent_plot_thread_vector_fallback",
                workspace_id=workspace_id,
                candidate_count=len(rows),
                error_type=type(exc).__name__,
                error_message=str(exc),
            )
            return fallback[:_EMERGENT_PLOT_THREAD_TOP_K]

        selected: list[PlotThreadORM] = []
        selected_ids: set[str] = set()
        for hit in hits:
            thread_id = str(hit.metadata.get("entity_id", ""))
            row = row_by_id.get(thread_id)
            if row is None or thread_id in selected_ids:
                continue
            selected.append(row)
            selected_ids.add(thread_id)
        # Chroma 可能只有部分候选（例如迁移前条目尚未重写向量），用有界排序补齐。
        for row in fallback:
            if len(selected) >= _EMERGENT_PLOT_THREAD_TOP_K:
                break
            if row.thread_id not in selected_ids:
                selected.append(row)
                selected_ids.add(row.thread_id)
        return selected

    def _read_prev_chapter_tail(
        self, *, workspace_id: str, prev_chapter_id: str
    ) -> str | None:
        """读上一章 ``final.md`` 末尾 500-800 字。无文件 → None(首章或未写完)。"""
        paths = self._workspace_service.workspaces_root / workspace_id
        final_path = paths / "chapters" / prev_chapter_id / "final.md"
        if not final_path.is_file():
            return None
        content = final_path.read_text(encoding="utf-8")
        # 取末尾 800 字(中文 / 英文混合按字符算,简化 P1 模式)
        tail_len = min(800, len(content))
        return content[-tail_len:] if tail_len > 0 else None

    def _read_mini_plan_item(
        self, *, workspace_id: str, chapter_id: str
    ) -> MiniPlanItem | None:
        """v1.0.x wave 4 session 2:读 ``data/outline/mini_plan.json`` 找对应章项。

        - 文件不存在(wave 4 前生成的旧工作区)→ ``None`` + structlog
          ``chapter_card_mini_plan_missing`` warn(对齐 Q1=A 拍板)
        - JSON 损坏 / schema 不符 / chapter_id 不在 items → ``None`` + warn
        - 找到 → 返 ``MiniPlanItem`` 对象,caller 取 ``one_line_event`` 注入 ctx
        """
        mini_plan_path = self._outline_dir(workspace_id) / "mini_plan.json"
        if not mini_plan_path.is_file():
            _log.warning(
                "chapter_card_mini_plan_missing",
                workspace_id=workspace_id,
                chapter_id=chapter_id,
                reason="mini_plan_file_not_found",
            )
            return None
        try:
            raw = json.loads(mini_plan_path.read_text(encoding="utf-8"))
            mini_plan = MiniPlanOutput.model_validate(raw)
        except (OSError, json.JSONDecodeError, ValueError) as exc:
            _log.warning(
                "chapter_card_mini_plan_missing",
                workspace_id=workspace_id,
                chapter_id=chapter_id,
                reason="mini_plan_parse_failed",
                error_type=type(exc).__name__,
                error_message=str(exc),
            )
            return None
        for item in mini_plan.items:
            if item.chapter_id == chapter_id:
                return item
        _log.warning(
            "chapter_card_mini_plan_missing",
            workspace_id=workspace_id,
            chapter_id=chapter_id,
            reason="chapter_id_not_in_items",
        )
        return None

    def _read_prev_chapter_card_ssot(
        self, *, workspace_id: str, prev_chapter_id: str
    ) -> ChapterCard | None:
        """v1.0.x wave 4 session 2:读上一章 ``data/cards/<id>.json`` SSOT。

        - 文件不存在(批量生成中第 1 张卡 / 还没生成) → ``None``(静默,
          属正常场景,不打 warn)
        - JSON 损坏 / Pydantic 校验失败 → ``None`` + warn
        - 兑现 P5-B-06 derive_context 原注释承诺"由上一章 next_chapter_seed
          间接传递" — caller 取 ``next_chapter_seed`` → prev_chapter_summary /
          ``pov_character`` → prev_pov_character / ``must_unresolved`` →
          prev_unfinished_actions(对齐 Q4=A 拍板)
        """
        card_path = self._chapter_service.card_path_for(workspace_id, prev_chapter_id)
        if not card_path.is_file():
            return None
        try:
            raw = json.loads(card_path.read_text(encoding="utf-8"))
            return ChapterCard.model_validate_storage(raw)
        except (OSError, json.JSONDecodeError, ValueError) as exc:
            _log.warning(
                "chapter_card_prev_card_read_failed",
                workspace_id=workspace_id,
                prev_chapter_id=prev_chapter_id,
                error_type=type(exc).__name__,
                error_message=str(exc),
            )
            return None

    def _read_prev_chapter_summaries(
        self, *, workspace_id: str, chapter_id: str, n: int
    ) -> list[ChapterSummary]:
        """v1.0.x wave 4 session 3b:读前 N 章 ChapterSummary 序列(长程事实)。

        从 ``chapter_summaries`` 表查 ``workspace_id == self`` 且
        ``chapter_id < self`` 的行,先按 ``chapter_id`` 倒序取最近 n 条，再反转为
        升序交给模型。这样模型看到的是“最近 N 章”而不是小说开头 N 章，同时仍按
        正常阅读顺序消费上下文。

        语义边界:

        - ``session_factory is None``(legacy 单测 / CLI 路径)→ 空 list(对齐
          ``_derive_character_voices`` 同型 fallback)
        - 表无行(老工作区 / backfill 未跑 / 本章实为首章 / 仅前 0 章已抽取)
          → 空 list(静默,LLM prompt 拼装自然降级到 session 2 单卡映射)
        - ``DBAPI / sqlalchemy`` 异常 → 空 list + warn(对齐 fail-open 同型)
        - ``ChapterSummary`` Pydantic 校验失败(理论不可达 — ORM 列约束已保证)
          → 跳过该条 + warn,其余条目继续返回

        :param n: 上限条数,典型 10(``_PREV_CHAPTER_SUMMARIES_DEFAULT_N``)。
        :returns: 至多 ``n`` 条 ``ChapterSummary``,按 ``chapter_id`` 升序。
        """
        if self._session_factory is None:
            return []
        try:
            session = self._session_factory(workspace_id)
        except Exception as exc:  # noqa: BLE001 — fail-open
            _log.warning(
                "chapter_card_prev_summaries_session_failed",
                workspace_id=workspace_id,
                chapter_id=chapter_id,
                error_type=type(exc).__name__,
                error_message=str(exc),
            )
            return []
        try:
            rows = (
                session.query(ChapterSummaryORM)
                .filter(ChapterSummaryORM.workspace_id == workspace_id)
                .filter(ChapterSummaryORM.chapter_id < chapter_id)
                .order_by(ChapterSummaryORM.chapter_id.desc())
                .limit(n)
                .all()
            )
        except Exception as exc:  # noqa: BLE001 — fail-open
            _log.warning(
                "chapter_card_prev_summaries_query_failed",
                workspace_id=workspace_id,
                chapter_id=chapter_id,
                error_type=type(exc).__name__,
                error_message=str(exc),
            )
            return []
        finally:
            session.close()

        rows.reverse()
        results: list[ChapterSummary] = []
        for row in rows:
            try:
                results.append(
                    ChapterSummary(
                        chapter_id=row.chapter_id,
                        workspace_id=row.workspace_id,
                        summary_text=row.summary_text,
                        pov_character=row.pov_character,
                        scene_setting=row.scene_setting,
                        key_events=list(row.key_events or []),
                        character_interactions=list(row.character_interactions or []),
                        unfinished_actions=list(row.unfinished_actions or []),
                        character_states=list(row.character_states or []),
                        established_facts=list(row.established_facts or []),
                        revealed_mechanics=list(row.revealed_mechanics or []),
                        raw_final_tail=row.raw_final_tail,
                        source_version_id=row.source_version_id,
                        source_text_hash=row.source_text_hash,
                        evidence_records=list(row.evidence_records or []),
                        ledger_hash=row.ledger_hash,
                        validated_at=row.validated_at,
                        extraction_trigger=row.extraction_trigger,
                        model_used=row.model_used,
                        extracted_at=row.created_at,
                    )
                )
            except Exception as exc:  # noqa: BLE001 — 单条解析失败不影响其余
                _log.warning(
                    "chapter_card_prev_summary_row_parse_failed",
                    workspace_id=workspace_id,
                    chapter_id=chapter_id,
                    row_chapter_id=getattr(row, "chapter_id", None),
                    error_type=type(exc).__name__,
                    error_message=str(exc),
                )
                continue
        return results

    # ---- 单章生成 -----------------------------------------------------------

    def _generate_card_with_setting_conflict_retry(
        self,
        *,
        workspace_id: str,
        chapter_id: str,
        generation_context: ChapterCardGenerationContext,
        router: ModelRouter,
        retry_state: _SettingConflictRetryState,
        on_parallel_status: Callable[[dict[str, Any]], None] | None = None,
    ) -> tuple[ChapterCard, ChapterCardGenerationContext]:
        """生成卡，并在 Setting/核心状态阻断后共用一次定向重试。

        ``retry_state`` 由顶层产品动作持有：普通生成里的初次/引用分支共用一个，
        正式 UI 的 rejected-card 重生动作另有一个。这样所有可写卡入口同尺，
        同一动作不会因先后命中两类合同而叠出第二次修复额度。
        """

        try:
            with bind_workspace_context(workspace_id):
                generated_card = generate_chapter_card(
                    generation_context,
                    router=router,
                    client=self._client,
                    model=self._model,
                    prompts_dir=self._prompts_dir,
                    on_parallel_status=on_parallel_status,
                )
            return generated_card, generation_context
        except ChapterCardGeneratorError as exc:
            retryable_codes = {
                "CHAPTER_CARD_SETTING_CONFLICT",
                CHAPTER_CARD_CORE_STATE_ERROR_CODE,
            }
            if exc.error_code not in retryable_codes:
                raise
            if (
                retry_state.attempts_used
                >= CHAPTER_CARD_SHARED_CONTRACT_REPAIR_MAX_RETRIES
            ):
                exhausted_event = (
                    "chapter_card_setting_conflict_retry_exhausted"
                    if exc.error_code == "CHAPTER_CARD_SETTING_CONFLICT"
                    else "chapter_card_core_state_retry_exhausted"
                )
                _log.error(
                    exhausted_event,
                    workspace_id=workspace_id,
                    chapter_id=chapter_id,
                    reason="retry_already_used",
                    error_code=exc.error_code,
                )
                raise

            violations: list[ConstraintViolation] = []
            core_state_errors: list[str] = []
            if exc.error_code == "CHAPTER_CARD_SETTING_CONFLICT":
                violations = self._setting_violations_from_generator_error(exc)
                if not violations:
                    _log.error(
                        "chapter_card_setting_conflict_retry_exhausted",
                        workspace_id=workspace_id,
                        chapter_id=chapter_id,
                        reason="missing_or_invalid_structured_evidence",
                        error_code=exc.error_code,
                    )
                    raise
            else:
                raw_errors = (exc.details or {}).get("errors")
                if isinstance(raw_errors, list):
                    core_state_errors = [
                        item.strip()
                        for item in raw_errors
                        if isinstance(item, str) and item.strip()
                    ]
                if not core_state_errors:
                    _log.error(
                        "chapter_card_core_state_retry_exhausted",
                        workspace_id=workspace_id,
                        chapter_id=chapter_id,
                        reason="missing_or_invalid_structured_evidence",
                        error_code=exc.error_code,
                    )
                    raise

            retry_state.attempts_used += 1
            retry_update: dict[str, object]
            if violations:
                retry_update = {"setting_conflict_retry_violations": violations}
            else:
                retry_update = {"core_state_retry_errors": core_state_errors}
            retry_context = generation_context.model_copy(update=retry_update)
            started_event = (
                "chapter_card_setting_conflict_retry_started"
                if violations
                else "chapter_card_core_state_retry_started"
            )
            _log.info(
                started_event,
                workspace_id=workspace_id,
                chapter_id=chapter_id,
                constraint_ids=[item.constraint_id for item in violations],
                core_state_errors=core_state_errors,
                error_code=exc.error_code,
                contract_hash=generation_context.setting_contract.source_hash,
            )
            try:
                with bind_workspace_context(workspace_id):
                    repaired_card = generate_chapter_card(
                        retry_context,
                        router=router,
                        client=self._client,
                        model=self._model,
                        prompts_dir=self._prompts_dir,
                        on_parallel_status=on_parallel_status,
                    )
            except ChapterCardGeneratorError as retry_exc:
                if retry_exc.error_code in retryable_codes:
                    exhausted_event = (
                        "chapter_card_setting_conflict_retry_exhausted"
                        if retry_exc.error_code == "CHAPTER_CARD_SETTING_CONFLICT"
                        else "chapter_card_core_state_retry_exhausted"
                    )
                    _log.error(
                        exhausted_event,
                        workspace_id=workspace_id,
                        chapter_id=chapter_id,
                        reason="second_contract_failure",
                        error_code=retry_exc.error_code,
                        constraint_ids=[
                            item.constraint_id for item in violations
                        ],
                        core_state_errors=core_state_errors,
                    )
                raise
            succeeded_event = (
                "chapter_card_setting_conflict_retry_succeeded"
                if violations
                else "chapter_card_core_state_retry_succeeded"
            )
            _log.info(
                succeeded_event,
                workspace_id=workspace_id,
                chapter_id=chapter_id,
                constraint_ids=[item.constraint_id for item in violations],
                core_state_errors=core_state_errors,
                repaired_error_code=exc.error_code,
            )
            return repaired_card, retry_context

    def _restore_legacy_rewound_outline_entities(
        self,
        *,
        workspace_id: str,
        chapter_id: str,
    ) -> None:
        """Provider 前修复旧 rewind 对书级大纲实体留下的严格误撤销现场。"""

        if self._session_factory is None:
            return
        try:
            baselines = load_outline_entity_baselines(
                workspaces_root=self._workspace_service.workspaces_root,
                workspace_id=workspace_id,
            )
        except OutlineEntityRecoveryError as exc:
            raise ChapterCardContextDerivationError(str(exc)) from exc

        session = self._session_factory(workspace_id)
        try:
            restored = restore_rewound_outline_entities_in_session(
                session,
                workspace_id=workspace_id,
                chapter_id=chapter_id,
                baselines=baselines,
                require_revoked=True,
            )
            session.commit()
        except Exception:
            session.rollback()
            raise
        finally:
            session.close()

        if restored.character_ids or restored.plot_thread_ids:
            _log.info(
                "legacy_rewound_outline_entities_restored_before_card_generation",
                workspace_id=workspace_id,
                chapter_id=chapter_id,
                character_ids=list(restored.character_ids),
                plot_thread_ids=list(restored.plot_thread_ids),
            )

    def generate_single_chapter_card(
        self,
        workspace_id: str,
        chapter_id: str,
        *,
        chapter_number: int,
        volume_id: str,
        card_status: str = "draft",
        auto_retry_on_reference_failure: bool = True,
        forbidden_must_achieve_phrases: list[str] | None = None,
        continuity_bridge_retry_actions: list[str] | None = None,
        continuity_bridge_retry_base_card: ChapterCard | None = None,
        task_id: str | None = None,
    ) -> ChapterCard:
        """单章生成 + SSOT 落盘 + ORM upsert(``POST /chapters/{id}/card:generate``)。

        :param card_status: 初始状态(``draft`` / ``pending_review`` 等);单端点
            默认 ``draft``，outline commit 首卡显式传 ``pending_review``。
        :param auto_retry_on_reference_failure: P6-B-10 引入。``True``(默认)时
            若 agent 首次输出引用了工作区 ``characters`` / ``plot_threads`` 表外
            ID,自动重试一次(``is_retry_with_strict_ids=True`` + 仅 DB 已知 ID
            列入 context);两次都失败抛 ``ReferenceValidationFailedError``。
            ``False`` 时首次失败直接抛(供 ``retry_references`` 手动触发场景
            用 — 该路径自身就是一次重试,不再叠重试)。``session_factory=None``
            (P1/legacy 路径)时本字段被忽略 — 无 DB session 跳过校验。
        :param forbidden_must_achieve_phrases: v1.0.x wave 4 session 4 引入。
            非空 list 时由 ``_retry_card_for_diversification`` 调用注入 — 派生
            的 ``ChapterCardGenerationContext`` 末加该字段(via
            ``model_copy``),agent prompt 顶部加"同质化 retry 禁词清单"banner,
            LLM 必须重写 must_achieve 三元组避开列表字面。``None`` /  空 list
            (首次生成 / 老 caller)→ 不注入 banner,与既有路径等价(零回归)。
        :raises ReferenceValidationFailedError: 引用合法性校验失败(自动重试已
            穷尽 / ``auto_retry_on_reference_failure=False`` / DB 表确有行但
            agent 仍输出未知 ID)。**抛错时不落 SSOT 文件 + 不 upsert ORM**
            (对齐 rollback 测试约定)。
        """
        self._restore_legacy_rewound_outline_entities(
            workspace_id=workspace_id,
            chapter_id=chapter_id,
        )
        context = self.derive_context(
            workspace_id,
            chapter_id,
            chapter_number=chapter_number,
            volume_id=volume_id,
        )
        if continuity_bridge_retry_actions:
            context = context.model_copy(
                update={
                    "continuity_bridge_retry_actions": list(
                        continuity_bridge_retry_actions
                    ),
                    "continuity_bridge_retry_base_card": (
                        continuity_bridge_retry_base_card
                    ),
                }
            )
        # v1.0.x wave 4 session 4:同质化 retry 时注入 forbidden_must_achieve_phrases
        if forbidden_must_achieve_phrases:
            context = context.model_copy(
                update={
                    "forbidden_must_achieve_phrases": list(
                        forbidden_must_achieve_phrases
                    ),
                }
            )
        router = self._build_router(workspace_id)
        parallel_status_callback = (
            None
            if task_id is None
            else lambda snapshot: self._publish_card_parallel_status(
                task_id=task_id,
                snapshot=snapshot,
            )
        )
        # v2 间隙 A3②(2026-06-13):卡生成链(generator + 内嵌 mvf judge +
        # 引用重试)的 llm_response 此前缺 workspace_id → 落 system_logs;
        # 工作区级绑定让日志归属正确分流。普通单卡 / 引用重试 / 失效卡恢复
        # 均经共享 _generate_card_with_setting_conflict_retry,一处绑定全覆盖。
        setting_retry_state = _SettingConflictRetryState()
        last_successful_generation_context = context

        def _generate_with_setting_conflict_retry(
            generation_context: ChapterCardGenerationContext,
        ) -> ChapterCard:
            """执行一次生成，并在 Setting 阻断证据后最多定向重试一次。

            重试证据完全来自 Judge 的结构化 ``ConstraintViolation``；若 details
            缺失、格式无效或二次仍冲突则 fail closed。局部状态覆盖本次产品调用
            的引用重试等所有分支，保证 Setting 定向重试全链合计最多一次。
            """

            nonlocal last_successful_generation_context
            generated_card, successful_context = (
                self._generate_card_with_setting_conflict_retry(
                    workspace_id=workspace_id,
                    chapter_id=chapter_id,
                    generation_context=generation_context,
                    router=router,
                    retry_state=setting_retry_state,
                    on_parallel_status=parallel_status_callback,
                )
            )
            last_successful_generation_context = successful_context
            return generated_card

        with bind_workspace_context(workspace_id):
            try:
                card = _generate_with_setting_conflict_retry(context)
            except ChapterCardGeneratorError as exc:
                if exc.error_code != CHAPTER_CARD_REFERENCE_ERROR_CODE:
                    raise
                unknown_chars, unknown_threads = (
                    self._reference_ids_from_generator_error(exc)
                )
                _log.warning(
                    "chapter_card_reference_invalid_inside_generator",
                    workspace_id=workspace_id,
                    chapter_id=chapter_id,
                    unknown_character_ids=unknown_chars,
                    unknown_thread_ids=unknown_threads,
                    allow_retry=auto_retry_on_reference_failure,
                )
                if (
                    not auto_retry_on_reference_failure
                    or CHAPTER_CARD_REFERENCE_RETRY_MAX_RETRIES < 1
                ):
                    raise ReferenceValidationFailedError(
                        workspace_id=workspace_id,
                        chapter_id=chapter_id,
                        unknown_character_ids=unknown_chars,
                        unknown_thread_ids=unknown_threads,
                    ) from exc

                retry_context = self._make_retry_context(
                    workspace_id=workspace_id,
                    context=context,
                )
                try:
                    card = _generate_with_setting_conflict_retry(retry_context)
                except ChapterCardGeneratorError as retry_exc:
                    if (
                        retry_exc.error_code
                        != CHAPTER_CARD_REFERENCE_ERROR_CODE
                    ):
                        raise
                    retry_unknown_chars, retry_unknown_threads = (
                        self._reference_ids_from_generator_error(retry_exc)
                    )
                    raise ReferenceValidationFailedError(
                        workspace_id=workspace_id,
                        chapter_id=chapter_id,
                        unknown_character_ids=retry_unknown_chars,
                        unknown_thread_ids=retry_unknown_threads,
                    ) from retry_exc
                _log.info(
                    "chapter_card_inner_reference_retry_succeeded",
                    workspace_id=workspace_id,
                    chapter_id=chapter_id,
                )

            # P6-B-10:引用合法性后处理(若 session_factory 在 + DB 表非空)。
            card = self._run_reference_validation_with_retry(
                workspace_id=workspace_id,
                chapter_id=chapter_id,
                chapter_number=chapter_number,
                volume_id=volume_id,
                initial_card=card,
                initial_context=last_successful_generation_context,
                router=router,
                allow_retry=auto_retry_on_reference_failure,
                on_parallel_status=parallel_status_callback,
                generate_card_call=_generate_with_setting_conflict_retry,
            )

        # v1.0.x wave 6 s3:post-LLM 强制覆盖 prev_chapter_tail 占位字面,
        # 防 LLM hallucinate(对齐 plan §s3 Q3=A 三档分支)。
        card = _override_prev_chapter_tail(
            card,
            chapter_number=context.chapter_number,
            prev_chapter_tail_input=context.prev_chapter_tail,
        )

        # ChapterCard 的 card_status 默认 "draft";按 caller 传值覆盖。
        # v1.1 wave 4 B1 acceptance(2026-06-02 修根因):身份字段用 authoritative
        # caller 值回填,不信 LLM。
        card_with_status = card.model_copy(
            update={
                "card_status": card_status,
                "chapter_id": chapter_id,
                "chapter_number": chapter_number,
                "volume_id": volume_id,
            }
        )
        # 文件 SSOT 落盘(复用 ChapterService.save_chapter_card 原子写)
        self._chapter_service.save_chapter_card(workspace_id, card_with_status)
        # ORM upsert
        self._upsert_chapter_card_orm(
            workspace_id=workspace_id,
            chapter_id=chapter_id,
            card_status=card_status,
        )
        _log.info(
            "chapter_card_generated",
            workspace_id=workspace_id,
            chapter_id=chapter_id,
            chapter_number=chapter_number,
            volume_id=volume_id,
            card_status=card_status,
        )
        return card_with_status

    # ---- P6-B-10:引用合法性后处理 + 手动重试入口 -----------------------------

    @staticmethod
    def _reference_ids_from_generator_error(
        exc: ChapterCardGeneratorError,
    ) -> tuple[list[str], list[str]]:
        """读取 Agent 引用错误的稳定 details，保持 ID 顺序并去重。"""

        details = exc.details or {}
        raw_chars = details.get("unknown_character_ids", [])
        raw_threads = details.get("unknown_thread_ids", [])
        chars = (
            [item for item in raw_chars if isinstance(item, str)]
            if isinstance(raw_chars, list)
            else []
        )
        threads = (
            [item for item in raw_threads if isinstance(item, str)]
            if isinstance(raw_threads, list)
            else []
        )
        return list(dict.fromkeys(chars)), list(dict.fromkeys(threads))

    @staticmethod
    def _setting_violations_from_generator_error(
        exc: ChapterCardGeneratorError,
    ) -> list[ConstraintViolation]:
        """严格读取 Setting Judge 的章节卡阻断证据；异常时返回空并停止重试。

        HARD 与来源完整的 ``world.unique_rules`` SOFT 都可由生成器作为章节卡
        修复证据返回；SOFT 不会在这里被改写成 HARD。其他域或无权威 hash 的 SOFT
        不能取得定向重试资格。
        """

        details = exc.details or {}
        raw_violations = details.get("constraint_violations", [])
        if not isinstance(raw_violations, list) or not raw_violations:
            return []
        parsed: list[ConstraintViolation] = []
        try:
            for payload in raw_violations:
                violation = ConstraintViolation.model_validate(payload)
                if violation.severity == "soft" and (
                    violation.domain != "world.unique_rules"
                    or not violation.source_hash
                ):
                    return []
                parsed.append(violation)
        except (TypeError, ValueError):
            return []
        return parsed

    def retry_references(
        self,
        workspace_id: str,
        chapter_id: str,
        *,
        chapter_number: int,
        volume_id: str,
        card_status: str = "draft",
    ) -> RetryReferencesResult:
        """手动触发 ``POST /chapters/{id}:retry-references`` 重新生成章节卡。

        语义"前端 ChapterCardReviewPage 在 reject 状态下用户点'重试引用'按钮"
        (对齐 plan §"P6-B-10" 字面 + FRONTEND_SPEC §4.8 字面"重试引用")。

        **v1.0.x wave 5 session 4 升级**(plan §s4 h + k 件):

        - **h:接入 wave 4 forbidden_must_achieve_phrases 修复** — retry 前从
          同卷其他 ``card_status='approved'`` 章卡收集 ``must_achieve`` 字面
          作 forbidden 注入 prompt;retry 后跑升级后 ``_compute_must_achieve_overlap_ratio``
          单卡 vs 同卷其他配对校验,>= threshold 接受 + log warning(对齐 wave
          4 session 4 batch 检测同型决策"接受 + 降级 log",不阻断 user 修复
          流程)
        - **k:cascade 失效后续章节卡** — retry 成功后 query ChapterCardORM 找
          workspace 内 ``chapter_id > target_chapter_id`` 行,返 chapter_id
          列表供路由层 BackgroundTask 触发批量重生(``regenerate_invalidated_cards_batch``)

        **类 A 处置**(plan §s4 字面"调 ChapterRewindService.rewind"vs 既有
        rewind 状态校验冲突):rewind 校验 target ``_REWIND_ALLOWED_SOURCES``
        (只允许 approved/published),但 retry_references 触发时 target chapter
        通常仍 ``drafting``(卡审核 ≠ 正文已写)→ 直接调 rewind 必抛
        ``ChapterRewindInvalidStateError``。按 plan 立法精神"级联失效后续卡"
        走,实施在 ``ChapterCardService`` 域内自治 — 不调 ChapterRewindService,
        只标后续章节卡 chapter_id 列表 + BackgroundTask 重生覆盖 SSOT/ORM,
        不动 ChapterORM.status(避破章节正文 gate + 职责清晰"卡-driven cascade
        限定卡")。

        :returns: ``RetryReferencesResult(card, invalidated_chapter_ids)``;
            ``cascade_regenerate_task_id`` 由路由层注册 BackgroundTask 时回填,
            service 层不返。
        :raises ReferenceValidationFailedError: 重试后仍失败(LLM 仍输出列表外
            ID)。``details`` 含 ``unknown_character_ids`` / ``unknown_thread_ids``
            供前端引导用户编辑章节卡或检查 SQLite 表。
        """
        if self._session_factory is not None:
            current_seq = chapter_seq_from_id(chapter_id)
            recovery_paths = WorkspacePaths(
                workspace_id,
                workspaces_root=self._workspace_service.workspaces_root,
            )
            current_card_path = recovery_paths.cards_dir / f"{chapter_id}.json"
            previous_chapter_id = (
                f"ch-{current_seq - 1:04d}"
                if current_seq is not None and current_seq > 1
                else ""
            )
            previous_card_path = (
                recovery_paths.cards_dir / f"{previous_chapter_id}.json"
                if previous_chapter_id
                else recovery_paths.cards_dir / "__no_previous_card__.json"
            )
            # 历史兼容修复只在两张真实 SSOT 卡都存在时参与；普通 retry 的
            # 旧测试/fixture 可能只有 ORM 缓存，不能因此改变既有产品行为。
            if current_card_path.is_file() and previous_card_path.is_file():
                current_card = self._chapter_service.read_chapter_card(
                    workspace_id,
                    chapter_id,
                )
                previous_card = self._chapter_service.read_chapter_card(
                    workspace_id,
                    previous_chapter_id,
                )
                repaired = CoreStateService(
                    session_factory=self._session_factory
                ).repair_previous_soft_object_identity_for_rejected_card(
                    workspace_id=workspace_id,
                    target_card=current_card,
                    previous_card=previous_card,
                    paths=recovery_paths,
                )
                if repaired:
                    _log.warning(
                        "chapter_card_retry_references_repaired_previous_object_identity",
                        workspace_id=workspace_id,
                        chapter_id=chapter_id,
                        previous_chapter_id=previous_chapter_id,
                    )
        self._restore_legacy_rewound_outline_entities(
            workspace_id=workspace_id,
            chapter_id=chapter_id,
        )
        context = self.derive_context(
            workspace_id,
            chapter_id,
            chapter_number=chapter_number,
            volume_id=volume_id,
        )
        continuity_bridge_retry_actions = (
            self._continuity_bridge_actions_for_reference_retry(
                workspace_id=workspace_id,
                chapter_id=chapter_id,
            )
        )
        if continuity_bridge_retry_actions:
            context = context.model_copy(
                update={
                    "continuity_bridge_retry_actions": (
                        continuity_bridge_retry_actions
                    )
                }
            )
        # v1.0.x wave 5 session 4 (h):同卷其他 approved 章 must_achieve 注入
        forbidden_phrases = self._collect_forbidden_must_achieve_from_volume(
            workspace_id,
            volume_id=volume_id,
            exclude_chapter_id=chapter_id,
        )
        # 重试 context:把 is_retry_with_strict_ids 设 True(banner 注入)+
        # 把 character_voices / plot_threads 限定到 DB 已知 ID(避免 outline
        # 阶段的"幽灵 ID"再次进入 prompt)+ v1.0.x wave 5 session 4 forbidden 注入
        retry_context = self._make_retry_context(
            workspace_id=workspace_id,
            context=context,
            forbidden_must_achieve_phrases=forbidden_phrases,
        )
        router = self._build_router(workspace_id)
        # 手动 retry-references 同样是工作区归属的 Provider 链。若缺少绑定，
        # llm_request/llm_response 会被路由到 system_logs，导致工作区用量少计。
        try:
            card, retry_context = self._generate_card_with_setting_conflict_retry(
                workspace_id=workspace_id,
                chapter_id=chapter_id,
                generation_context=retry_context,
                router=router,
                retry_state=_SettingConflictRetryState(),
            )
        except ChapterCardGeneratorError as exc:
            if exc.error_code != CHAPTER_CARD_REFERENCE_ERROR_CODE:
                raise
            unknown_chars, unknown_threads = (
                self._reference_ids_from_generator_error(exc)
            )
            raise ReferenceValidationFailedError(
                workspace_id=workspace_id,
                chapter_id=chapter_id,
                unknown_character_ids=unknown_chars,
                unknown_thread_ids=unknown_threads,
            ) from exc
        # 单次重试 — 仍校验,失败直接抛(不再叠自动重试)。
        self._validate_card_refs_against_db_or_raise(
            workspace_id=workspace_id, card=card
        )
        # v1.0.x wave 6 s3:post-LLM 强制覆盖 prev_chapter_tail 占位字面
        # (与 generate_card 路径一致 — retry 路径同样需要覆盖,防 LLM 在 retry
        # 时再次 hallucinate)。
        card = _override_prev_chapter_tail(
            card,
            chapter_number=retry_context.chapter_number,
            prev_chapter_tail_input=retry_context.prev_chapter_tail,
        )
        card_with_status = card.model_copy(update={"card_status": card_status})
        self._chapter_service.save_chapter_card(workspace_id, card_with_status)
        self._upsert_chapter_card_orm(
            workspace_id=workspace_id,
            chapter_id=chapter_id,
            card_status=card_status,
        )
        _log.info(
            "chapter_card_references_retried",
            workspace_id=workspace_id,
            chapter_id=chapter_id,
            chapter_number=chapter_number,
            volume_id=volume_id,
            card_status=card_status,
            forbidden_phrases_injected=len(forbidden_phrases),
            continuity_bridge_retry_action_count=len(
                continuity_bridge_retry_actions
            ),
        )

        # v1.0.x wave 5 session 4 (h post-retry):同卷雷同校验 + log
        self._check_post_retry_diversification_and_warn(
            workspace_id=workspace_id,
            chapter_id=chapter_id,
            volume_id=volume_id,
            card=card_with_status,
        )

        # v1.0.x wave 5 session 4 (k):cascade 收集后续章节卡 id
        invalidated_ids = self._collect_subsequent_chapter_card_ids(
            workspace_id=workspace_id, target_chapter_id=chapter_id
        )
        if invalidated_ids:
            _log.info(
                "chapter_card_retry_cascade_collected",
                workspace_id=workspace_id,
                chapter_id=chapter_id,
                cascade_count=len(invalidated_ids),
            )

        return RetryReferencesResult(
            card=card_with_status,
            invalidated_chapter_ids=invalidated_ids,
        )

    def _continuity_bridge_actions_for_reference_retry(
        self,
        *,
        workspace_id: str,
        chapter_id: str,
    ) -> list[str]:
        """引用重生同时继承当前卡已证实的 continuity bridge 拒绝证据。

        UI 的 ``retry-references`` 会整卡重生。若现有未批准卡同时因即时未完
        动作未逐字承接而不能 approve，仅开启严格 ID 仍可能生成第二张同样
        不能批准的卡；这里复用同一确定性 approval 校验取得精确动作，让一次
        产品重生同时满足两类已知约束。缺卡或 ledger 陈旧时保持原路径，由新
        ``derive_context`` 自行冻结当前账本，不猜测动作。
        """

        from src.services.chapter_service import ChapterServiceError

        try:
            self._validate_card_continuity_for_approval(
                workspace_id=workspace_id,
                chapter_id=chapter_id,
            )
        except CardContinuityBridgeRequiredError as exc:
            return list(exc.required_actions)
        except (
            CardContinuityLedgerStaleError,
            ChapterServiceError,
        ):
            return []
        return []

    def _run_reference_validation_with_retry(
        self,
        *,
        workspace_id: str,
        chapter_id: str,
        chapter_number: int,
        volume_id: str,
        initial_card: ChapterCard,
        initial_context: ChapterCardGenerationContext,
        router,  # type: ignore[no-untyped-def]
        allow_retry: bool,
        on_parallel_status: Callable[[dict[str, Any]], None] | None = None,
        generate_card_call: Callable[[ChapterCardGenerationContext], ChapterCard]
        | None = None,
    ) -> ChapterCard:
        """P6-B-10 共用核心:首次生成后校验,失败时自动重试一次。

        - ``session_factory=None``(P1/legacy 路径)→ noop 直接返 initial_card
        - DB 表对当前 workspace 全空 → ``collect_unknown_ids_from_card`` 跳过
          对应类目,返空 unknown(legacy 兼容,见
          ``reference_validator.collect_unknown_ids_from_card``)
        - 首次有 unknown_ids + ``allow_retry=True`` → 重新派生 retry_context +
          再调 agent,二次校验仍 unknown 抛 ``ReferenceValidationFailedError``
        - ``allow_retry=False`` 时首次失败直接抛
        """
        if self._session_factory is None:
            return initial_card

        session = self._session_factory(workspace_id)
        try:
            known_char_ids, known_thread_ids = load_known_ids_from_workspace(
                session, workspace_id
            )
        finally:
            session.close()

        unknown_chars, unknown_threads = collect_unknown_ids_from_card(
            initial_card,
            known_character_ids=known_char_ids,
            known_thread_ids=known_thread_ids,
        )
        if not unknown_chars and not unknown_threads:
            return initial_card

        _log.warning(
            "chapter_card_reference_invalid_first_attempt",
            workspace_id=workspace_id,
            chapter_id=chapter_id,
            unknown_character_ids=unknown_chars,
            unknown_thread_ids=unknown_threads,
            allow_retry=allow_retry,
        )
        if not allow_retry or CHAPTER_CARD_REFERENCE_RETRY_MAX_RETRIES < 1:
            raise ReferenceValidationFailedError(
                workspace_id=workspace_id,
                chapter_id=chapter_id,
                unknown_character_ids=unknown_chars,
                unknown_thread_ids=unknown_threads,
            )

        # 自动重试:派生 retry context(DB-strict character_voices / plot_threads
        # + is_retry_with_strict_ids=True banner)+ 再调 agent。
        retry_context = self._make_retry_context(
            workspace_id=workspace_id, context=initial_context
        )
        if generate_card_call is None:
            retry_card = generate_chapter_card(
                retry_context,
                router=router,
                client=self._client,
                model=self._model,
                prompts_dir=self._prompts_dir,
                on_parallel_status=on_parallel_status,
            )
        else:
            retry_card = generate_card_call(retry_context)
        self._validate_card_refs_against_db_or_raise(
            workspace_id=workspace_id, card=retry_card
        )
        _log.info(
            "chapter_card_reference_retry_succeeded",
            workspace_id=workspace_id,
            chapter_id=chapter_id,
        )
        return retry_card

    def _validate_card_refs_against_db_or_raise(
        self, *, workspace_id: str, card: ChapterCard
    ) -> None:
        """读 DB 已知 ID + 扫 card 4 字段;命中未知 → 抛
        ``ReferenceValidationFailedError``。``session_factory=None`` 时 noop。"""
        if self._session_factory is None:
            return
        session = self._session_factory(workspace_id)
        try:
            known_char_ids, known_thread_ids = load_known_ids_from_workspace(
                session, workspace_id
            )
        finally:
            session.close()
        unknown_chars, unknown_threads = collect_unknown_ids_from_card(
            card,
            known_character_ids=known_char_ids,
            known_thread_ids=known_thread_ids,
        )
        if unknown_chars or unknown_threads:
            raise ReferenceValidationFailedError(
                workspace_id=workspace_id,
                chapter_id=card.chapter_id,
                unknown_character_ids=unknown_chars,
                unknown_thread_ids=unknown_threads,
            )

    def _make_retry_context(
        self,
        *,
        workspace_id: str,
        context: ChapterCardGenerationContext,
        forbidden_must_achieve_phrases: list[str] | None = None,
    ) -> ChapterCardGenerationContext:
        """从首次 context 派生重试 context:① 把 ``character_voices`` /
        ``plot_threads`` 过滤到只剩 DB 已知 ID(去掉 outline-only 的幽灵 ID);
        ② 设置 ``is_retry_with_strict_ids=True``(agent prompt 顶部插入 banner)。

        :param forbidden_must_achieve_phrases: v1.0.x wave 5 session 4 引入。
            非空时同时设置 ``ChapterCardGenerationContext.forbidden_must_achieve_phrases``
            字段(沿用 wave 4 session 4 已立的 prompt 顶部"同质化 retry 禁词清单"
            banner — agent 必须重写 must_achieve 三元组避开列表字面)。供
            ``retry_references`` 走"同卷其他 approved 章 must_achieve"防同质化
            注入。``None`` /  空 list → 不注入 banner,与既有路径等价(零回归)。

        DB 表为空时:① character_voices 至少保留 1 个(``min_length=1`` Pydantic
        约束;只能复用首次 context 的全列表 — 该情况下重试也会失败,但保证
        Pydantic 构造不抛);② plot_threads 保留首次的全列表(无 ``min_length``
        约束,允许空)。
        """
        if self._session_factory is None:
            # P1/legacy 路径不该到这里(_run_reference_validation_with_retry
            # 前置已 short-circuit),保留兜底防御。
            update: dict[str, Any] = {"is_retry_with_strict_ids": True}
            if forbidden_must_achieve_phrases:
                update["forbidden_must_achieve_phrases"] = list(
                    forbidden_must_achieve_phrases
                )
            return context.model_copy(update=update)

        session = self._session_factory(workspace_id)
        try:
            known_char_ids, known_thread_ids = load_known_ids_from_workspace(
                session, workspace_id
            )
        finally:
            session.close()

        # 过滤 character_voices:保留 DB 已知 ID。若全被过滤(DB 空 / 全不匹配)
        # 保留首次 context 的列表(Pydantic ``min_length=1`` 约束,且这种情况
        # retry 必失败,但保 Pydantic 构造能通过)。
        filtered_voices = [
            v for v in context.character_voices if v.character_id in known_char_ids
        ]
        if not filtered_voices:
            filtered_voices = list(context.character_voices)

        # 过滤 plot_threads:若 DB 无伏笔行(known_thread_ids 空),保留首次的;
        # 否则只保留 DB 已知。
        if known_thread_ids:
            filtered_threads = [
                t for t in context.plot_threads if t.thread_id in known_thread_ids
            ]
        else:
            filtered_threads = list(context.plot_threads)

        update_payload: dict[str, Any] = {
            "character_voices": filtered_voices,
            "plot_threads": filtered_threads,
            "is_retry_with_strict_ids": True,
        }
        if forbidden_must_achieve_phrases:
            update_payload["forbidden_must_achieve_phrases"] = list(
                forbidden_must_achieve_phrases
            )
        return context.model_copy(update=update_payload)

    def _collect_forbidden_must_achieve_from_volume(
        self,
        workspace_id: str,
        *,
        volume_id: str,
        exclude_chapter_id: str,
    ) -> list[str]:
        """v1.0.x wave 5 session 4:收集同卷其他已 approved 章 ``must_achieve`` 字面。

        供 ``retry_references`` 走"防同卷雷同"语义注入。

        实施:① ``ChapterCardORM`` 查 workspace 所有 ``card_status='approved'``
        章 id;② 对每个 id 读 SSOT JSON 取 ``volume_id`` + ``must_achieve``;
        ③ 同卷 ≠ ``exclude_chapter_id`` 的聚合 ``must_achieve`` 字面 list;
        ④ ORM / JSON 缺失 best-effort swallow + structlog warn 不阻塞主路径。

        :returns: 同卷其他 approved 章的 ``must_achieve`` 字面列表(扁平 list,
            不保章节边界 — 供注入 prompt 顶部"禁词清单"banner 即可);
            ``session_factory=None`` /  无 approved 章 → 空 list(等价于"不
            注入 forbidden",``_make_retry_context`` 默认路径)。
        """
        if self._session_factory is None:
            return []

        session = self._session_factory(workspace_id)
        try:
            approved_ids = list(
                session.execute(
                    select(ChapterCardORM.chapter_id).where(
                        ChapterCardORM.workspace_id == workspace_id,
                        ChapterCardORM.card_status == "approved",
                    )
                ).scalars()
            )
        finally:
            session.close()

        forbidden: list[str] = []
        for cid in approved_ids:
            if cid == exclude_chapter_id:
                continue
            try:
                other_card = self._chapter_service.read_chapter_card(workspace_id, cid)
            except Exception as exc:  # noqa: BLE001 — best-effort
                _log.warning(
                    "chapter_card_forbidden_collect_read_failed",
                    workspace_id=workspace_id,
                    chapter_id=cid,
                    error_type=type(exc).__name__,
                    error_message=str(exc),
                )
                continue
            if other_card.volume_id != volume_id:
                continue
            forbidden.extend(other_card.must_achieve)
        # D5(w4-s10):去重 + 截断,防长卷 approved 章多时禁词清单膨胀塞爆 prompt。
        return _truncate_forbidden_must_achieve_phrases(
            forbidden,
            source="volume_collect",
            workspace_id=workspace_id,
        )

    def _check_post_retry_diversification_and_warn(
        self,
        *,
        workspace_id: str,
        chapter_id: str,
        volume_id: str,
        card: ChapterCard,
    ) -> None:
        """v1.0.x wave 5 session 4 (h post-retry):单卡 vs 同卷其他章配对校验。

        使用升级后的 ``_compute_must_achieve_overlap_ratio`` 三维 pairwise max,
        逐对计算 ``card`` 与同卷其他**任何**章节卡的 must_achieve 重合率;若
        最大值 ≥ ``diversification_threshold``,**接受 + log warning**(对照
        wave 4 session 4 batch 检测器同型决策"接受 + 降级 log,不阻断主路径"
        — 手动 retry 入口由用户判断是否再点)。

        ``session_factory=None`` /  threshold ≤ 0 → noop。
        """
        threshold = self._card_config.diversification_threshold
        if threshold <= 0 or self._session_factory is None:
            return

        session = self._session_factory(workspace_id)
        try:
            all_card_ids = list(
                session.execute(
                    select(ChapterCardORM.chapter_id).where(
                        ChapterCardORM.workspace_id == workspace_id,
                    )
                ).scalars()
            )
        finally:
            session.close()

        max_overlap = 0.0
        paired_chapter_id: str | None = None
        for cid in all_card_ids:
            if cid == chapter_id:
                continue
            try:
                other_card = self._chapter_service.read_chapter_card(workspace_id, cid)
            except Exception as exc:  # noqa: BLE001 — best-effort
                _log.warning(
                    "chapter_card_post_retry_check_read_failed",
                    workspace_id=workspace_id,
                    chapter_id=cid,
                    error_type=type(exc).__name__,
                    error_message=str(exc),
                )
                continue
            if other_card.volume_id != volume_id:
                continue
            ratio = _compute_must_achieve_overlap_ratio(
                card.must_achieve, other_card.must_achieve
            )
            if ratio > max_overlap:
                max_overlap = ratio
                paired_chapter_id = cid

        if max_overlap >= threshold:
            _log.warning(
                "chapter_card_retry_references_still_diversification_flagged",
                workspace_id=workspace_id,
                chapter_id=chapter_id,
                paired_chapter_id=paired_chapter_id,
                overlap_ratio=max_overlap,
                threshold=threshold,
            )
        else:
            _log.info(
                "chapter_card_retry_references_diversification_passed",
                workspace_id=workspace_id,
                chapter_id=chapter_id,
                max_overlap=max_overlap,
                threshold=threshold,
            )

    def _collect_subsequent_chapter_card_ids(
        self,
        *,
        workspace_id: str,
        target_chapter_id: str,
    ) -> list[str]:
        """v1.0.x wave 5 session 4 (k):收集 workspace 内 chapter_id > target 的
        章节卡 chapter_id 列表(字典序,已排序)。

        ``chapter_id`` ``ch-NNNN`` 4 位定宽设计 → 字典序与 chapter_number 一致
        (对照 P6-B-01 ``ChapterRewindService`` 同型字符串字典序比较)。
        ``session_factory=None`` → 空 list(P1/legacy 路径无 DB)。
        """
        if self._session_factory is None:
            return []

        session = self._session_factory(workspace_id)
        try:
            cascade_ids = sorted(
                session.execute(
                    select(ChapterCardORM.chapter_id).where(
                        ChapterCardORM.workspace_id == workspace_id,
                        ChapterCardORM.chapter_id > target_chapter_id,
                    )
                ).scalars()
            )
        finally:
            session.close()
        return cascade_ids

    # ---- v1.0.x wave 4 session 4:后置同质化检测 + retry ---------------------

    def _run_diversification_check_with_retry(
        self,
        *,
        workspace_id: str,
        task_id: str,
        target_meta: list[tuple[str, int, str, str]],
        cards_by_id: dict[str, ChapterCard],
    ) -> tuple[dict[str, ChapterCard], dict[str, dict[str, Any]]]:
        """v1.0.x wave 4 session 4 立 → wave 6 s2 升级:后置同质化检测 + **最多
        3 次**自动 retry(原"单次 retry / 对齐 P6-B-10 单 retry 决策"提升)。

        wave 6 s2 升级要点(对齐 plan §s2 + 4 项待审项拍板 Q1/Q2/Q3/Q4=A):

        - **最大 retry 3 次**(``_DIVERSIFICATION_MAX_RETRY_ATTEMPTS``,对齐
          outline retry wave 5 s2 同型上限)
        - **forbidden 累加**(Q1=A):第 N 次 retry 时 ``forbidden_must_achieve_phrases``
          = 同 batch 其他卡 must_achieve ∪ 前 N-1 次 retry 失败卡的 must_achieve,
          forbidden 列表 retry 越多越具体,LLM 路径越窄,真正强制避开
        - **WS event 实时推送**(Q3=B):每次 retry 启动前 ``_publish_card_task_stage``
          推 ``auto_retry_attempt`` stage(含 ``attempt`` / ``max_attempts`` 字段)
          供前端展示"自动避免雷同 (N/3) 进行中"过程透明
        - **auto_retry_status 写回卡 + SSOT/ORM**(Q2=A):成功 → 设 ``passed``,
          3 次都失败 → 首次卡设 ``exhausted`` + UI 红色标记入口
        - **返回 ``auto_retry_stats``**(Q3=A 末尾聚合):``{chapter_id: {"attempts":
          int, "succeeded": bool}}`` 给 caller 写入 result 字段供前端 debug

        - 跑 ``_detect_must_achieve_diversification`` 找 flagged 章索引
        - 对每个 flagged 章按上述新流程 retry 至多 3 次

        :param target_meta: ``[(chapter_id, chapter_number, volume_id,
            card_status)]`` 列表(按 batch 顺序),retry 时需要 chapter_number /
            volume_id / card_status 复用
        :param cards_by_id: ``chapter_id`` → 已生成 ``ChapterCard`` 映射
        :return: ``(updated_cards, auto_retry_stats)``。``updated_cards`` =
            检测 + retry 后的最终 ``chapter_id`` → ``ChapterCard`` 映射;
            ``auto_retry_stats`` = ``{chapter_id: {"attempts": int, "succeeded":
            bool}}``,空 dict = 未触发任何 retry。
        """
        threshold = self._card_config.diversification_threshold
        if len(cards_by_id) < 2 or threshold <= 0:
            return cards_by_id, {}

        # batch 顺序 cards list,索引对齐 target_meta
        chapter_ids_in_order = [meta[0] for meta in target_meta]
        cards_in_order: list[ChapterCard] = []
        for cid in chapter_ids_in_order:
            if cid in cards_by_id:
                cards_in_order.append(cards_by_id[cid])
        if len(cards_in_order) < 2:
            return cards_by_id, {}

        flagged_idx = _detect_must_achieve_diversification(
            cards_in_order, threshold=threshold
        )
        if not flagged_idx:
            _log.debug(
                "chapter_card_batch_diversification_passed",
                workspace_id=workspace_id,
                task_id=task_id,
                batch_size=len(cards_in_order),
                threshold=threshold,
            )
            return cards_by_id, {}

        _log.info(
            "chapter_card_batch_diversification_flagged",
            workspace_id=workspace_id,
            task_id=task_id,
            flagged_count=len(flagged_idx),
            batch_size=len(cards_in_order),
            threshold=threshold,
            flagged_chapter_ids=[chapter_ids_in_order[i] for i in sorted(flagged_idx)],
        )

        updated_cards = dict(cards_by_id)
        auto_retry_stats: dict[str, dict[str, Any]] = {}
        max_attempts = _DIVERSIFICATION_MAX_RETRY_ATTEMPTS

        for flagged in sorted(flagged_idx):
            target_cid = chapter_ids_in_order[flagged]
            target_meta_row = next((m for m in target_meta if m[0] == target_cid), None)
            if target_meta_row is None:
                continue
            _, target_chapter_number, target_volume_id, target_status = target_meta_row

            first_card = updated_cards[target_cid]
            # Q1=A 累加:第 N 次 retry forbidden = 同 batch 其他卡 must_achieve
            # ∪ 前 N-1 次 retry 失败的 must_achieve
            failed_retry_phrases: list[str] = []
            succeeded_attempt: int | None = None

            for attempt in range(1, max_attempts + 1):
                # Q3=B:每次 retry 启动前推 WS event 供前端展示过程
                self._publish_card_task_stage(
                    task_id=task_id,
                    stage="auto_retry_attempt",
                    chapter_id=target_cid,
                    progress=attempt / max_attempts,
                    card_status=target_status,
                    attempt=attempt,
                    max_attempts=max_attempts,
                )
                _log.info(
                    "chapter_card_batch_generation_auto_retry_diversification",
                    workspace_id=workspace_id,
                    task_id=task_id,
                    chapter_id=target_cid,
                    attempt=attempt,
                    max_attempts=max_attempts,
                    threshold=threshold,
                )

                # forbidden = 同 batch 其他**当前**卡 must_achieve(含首次 +
                # 其他 flagged 章首次)+ 本章前 N-1 次 retry 失败 must_achieve
                forbidden_phrases: list[str] = []
                for other_cid, other_card in updated_cards.items():
                    if other_cid == target_cid:
                        continue
                    forbidden_phrases.extend(other_card.must_achieve)
                # failed_retry_phrases 放尾部:D5 截断保尾,本章前 N-1 次失败
                # must_achieve(最该避开的)恒被保留。
                forbidden_phrases.extend(failed_retry_phrases)
                # D5(w4-s10):去重 + 截断,防 batch_size > 10 时禁词清单膨胀。
                forbidden_phrases = _truncate_forbidden_must_achieve_phrases(
                    forbidden_phrases,
                    source="batch_retry",
                    workspace_id=workspace_id,
                    chapter_id=target_cid,
                )

                try:
                    retry_card = self.generate_single_chapter_card(
                        workspace_id,
                        target_cid,
                        chapter_number=target_chapter_number,
                        volume_id=target_volume_id,
                        card_status=target_status,
                        forbidden_must_achieve_phrases=forbidden_phrases,
                    )
                except Exception as exc:  # noqa: BLE001 — 本次 retry 失败 swallow,继续下一次
                    _log.warning(
                        "chapter_card_diversification_retry_exception",
                        workspace_id=workspace_id,
                        task_id=task_id,
                        chapter_id=target_cid,
                        attempt=attempt,
                        error_type=type(exc).__name__,
                        error_message=str(exc),
                    )
                    continue

                # 校验 retry 后与其他**当前**章配对最大重合率(三维 jieba max)
                new_max_overlap = 0.0
                for other_cid, other_card in updated_cards.items():
                    if other_cid == target_cid:
                        continue
                    ratio = _compute_must_achieve_overlap_ratio(
                        retry_card.must_achieve, other_card.must_achieve
                    )
                    if ratio > new_max_overlap:
                        new_max_overlap = ratio

                if new_max_overlap < threshold:
                    # Q2=A:retry 成功 → 设 passed + 写回 SSOT/ORM
                    retry_card = retry_card.model_copy(
                        update={"auto_retry_status": "passed"}
                    )
                    try:
                        self._chapter_service.save_chapter_card(
                            workspace_id, retry_card
                        )
                        self._upsert_chapter_card_orm(
                            workspace_id=workspace_id,
                            chapter_id=target_cid,
                            card_status=retry_card.card_status,
                        )
                    except Exception as exc:  # noqa: BLE001
                        _log.error(
                            "chapter_card_auto_retry_status_persist_failed",
                            workspace_id=workspace_id,
                            task_id=task_id,
                            chapter_id=target_cid,
                            error_type=type(exc).__name__,
                            error_message=str(exc),
                        )
                    updated_cards[target_cid] = retry_card
                    succeeded_attempt = attempt
                    _log.info(
                        "chapter_card_batch_generation_auto_retry_succeeded",
                        workspace_id=workspace_id,
                        task_id=task_id,
                        chapter_id=target_cid,
                        attempt=attempt,
                        max_attempts=max_attempts,
                        new_max_overlap=new_max_overlap,
                        threshold=threshold,
                    )
                    break

                # 本次 retry 仍超阈值 → 累加本次输出到 forbidden(Q1=A)
                failed_retry_phrases.extend(retry_card.must_achieve)
                _log.info(
                    "chapter_card_diversification_retry_attempt_failed",
                    workspace_id=workspace_id,
                    task_id=task_id,
                    chapter_id=target_cid,
                    attempt=attempt,
                    max_attempts=max_attempts,
                    retry_max_overlap=new_max_overlap,
                    threshold=threshold,
                )

            if succeeded_attempt is not None:
                auto_retry_stats[target_cid] = {
                    "attempts": succeeded_attempt,
                    "succeeded": True,
                }
            else:
                # Q2=A:3 次都失败 → 首次卡设 exhausted + 写回 SSOT/ORM
                exhausted_card = first_card.model_copy(
                    update={"auto_retry_status": "exhausted"}
                )
                try:
                    self._chapter_service.save_chapter_card(
                        workspace_id, exhausted_card
                    )
                    self._upsert_chapter_card_orm(
                        workspace_id=workspace_id,
                        chapter_id=target_cid,
                        card_status=exhausted_card.card_status,
                    )
                except Exception as exc:  # noqa: BLE001
                    _log.error(
                        "chapter_card_diversification_first_card_restore_failed",
                        workspace_id=workspace_id,
                        task_id=task_id,
                        chapter_id=target_cid,
                        error_type=type(exc).__name__,
                        error_message=str(exc),
                    )
                updated_cards[target_cid] = exhausted_card
                auto_retry_stats[target_cid] = {
                    "attempts": max_attempts,
                    "succeeded": False,
                }
                _log.error(
                    "chapter_card_batch_generation_auto_retry_exhausted_kept_original",
                    workspace_id=workspace_id,
                    task_id=task_id,
                    chapter_id=target_cid,
                    max_attempts=max_attempts,
                    threshold=threshold,
                    note=(
                        "auto retry 3 次均仍超阈值,保留首次卡 + 标 exhausted;"
                        "UI 显示红色标记 + 用户可手动 retry_references"
                    ),
                )

        return updated_cards, auto_retry_stats

    # ---- 上传整 JSON(P5-B-08,POST /chapters/{id}/card)---------------------

    def save_uploaded_card(
        self,
        workspace_id: str,
        chapter_id: str,
        card: ChapterCard,
    ) -> ChapterCard:
        """``POST /chapters/{id}/card`` 端点 service 入口(P5-B-08)。

        语义"用户上传整 ChapterCard JSON"(对齐 ROADMAP v1.1 §P1 验收清单 2
        "浏览器手动上传第 1 章章节卡 JSON"+ 决策 7=C 摊到
        ChapterCardReviewPage UI)。

        副作用:

        1. SSOT 文件原子写到 ``data/cards/<chapter_id>.json``(复用
           ``ChapterService.save_chapter_card`` 同 PUT 端点路径)。
        2. ORM upsert ``chapter_cards`` 行,``card_status`` 取自 ``card``
           本身(默认 ``draft`` — 见 ``ChapterCard.card_status`` 字段默认);
           上传场景的 ``card_approved_at`` 一律为 ``None``(approve 走专用
           端点)。

        **不**触发 WS event(对齐 P5-B-07 §6 接力 marker 字面 — 上传是创建
        / 覆盖,非状态翻转;前端如需感知会通过 mutation onSuccess 自行刷新)。

        与 PUT ``/chapters/{id}/card`` 区别:PUT 仅写 SSOT 文件,本方法同步
        写 ORM(让审核流的 ``transition_card_status`` 能直接读到行);
        chapter_id 一致性由路由层 ``ChapterIdMismatch`` 守卫,本 service 不
        校验(对齐 PUT 路由职责划分)。

        :returns: ``card`` 入参原物(便于路由层包装响应,不二次读盘)。
        """
        if self._session_factory is not None:
            card = CoreStateService(
                session_factory=self._session_factory
            ).freeze_card_contract(
                card=card,
                enabled=self._load_generation_config(
                    workspace_id
                ).core_state_hard_enabled,
            )
        self._chapter_service.save_chapter_card(workspace_id, card)
        self._upsert_chapter_card_orm(
            workspace_id=workspace_id,
            chapter_id=chapter_id,
            card_status=card.card_status,
        )
        _log.info(
            "chapter_card_uploaded",
            workspace_id=workspace_id,
            chapter_id=chapter_id,
            card_status=card.card_status,
        )
        return card

    # ---- 状态机入口(P5-B-07,decision 6=C)---------------------------------

    def transition_card_status(
        self,
        workspace_id: str,
        chapter_id: str,
        *,
        action: CardAction,
        rejection_notes: str | None = None,
    ) -> dict[str, Any]:
        """``card_status`` 状态机入口(``card:approve`` / ``card:reject`` 端点 service 层)。

        转换规则集中表 ``_CARD_TRANSITION_RULES``;不在合法源时抛
        ``CardInvalidStateTransitionError``(路由翻译为
        ``CARD_INVALID_STATE_TRANSITION (409)``);ORM 行不存在抛
        ``CardNotFoundError``(路由翻译为 ``CHAPTER_CARD_NOT_FOUND (404)``)。

        副作用顺序(成功时):

        1. 更新 ``chapter_cards`` ORM 行(``card_status`` + 视 action 写
           ``card_approved_at`` / ``rejection_notes``)
        2. 若 SSOT 文件 ``data/cards/<chapter_id>.json`` 存在,同步 patch
           ``card_status`` / ``card_approved_at``(避免 ORM 与 SSOT 漂移;
           SSOT 不含 ``rejection_notes`` — 字段只在 ORM)
        3. WS publish 2 个事件到 ``chapter.{chapter_id}.card.events`` 频道:
           ``card_status_changed`` (通用) + ``card_approved`` / ``card_rejected``
           (动作专属);publish 失败 swallow + structlog(对齐
           ``_publish_card_task_stage`` 同源)。

        :returns: ``{"chapter_id", "from_status", "to_status",
            "card_approved_at"(approve 时), "rejection_notes"(reject 时)}``
        """
        if self._session_factory is None:
            raise ChapterCardServiceError(
                "transition_card_status 需注入 session_factory"
            )

        # post-v6:只有用户把首章卡明确 reject 后再次 approve，且该章正停在
        # 首次不可逆状态冲突，才尝试零 Provider 的旧基线机械恢复。普通批准、
        # 普通 rejected 卡和后续章节完全不进入该入口。
        if action == "approve" and chapter_id == "ch-0001":
            preflight = self._session_factory(workspace_id)
            try:
                preflight_card = preflight.get(ChapterCardORM, chapter_id)
                preflight_chapter = preflight.get(ChapterORM, chapter_id)
                details = (
                    dict(preflight_chapter.core_state_commit_details or {})
                    if preflight_chapter is not None
                    else {}
                )
                needs_initial_baseline_recovery = bool(
                    preflight_card is not None
                    and preflight_card.workspace_id == workspace_id
                    and preflight_card.card_status == "rejected"
                    and preflight_chapter is not None
                    and preflight_chapter.workspace_id == workspace_id
                    and preflight_chapter.core_state_commit_status == "pending"
                    and details.get("conflicts")
                )
            finally:
                preflight.close()
            if needs_initial_baseline_recovery:
                CoreStateService(
                    session_factory=self._session_factory
                ).prepare_legacy_initial_baseline_recovery(
                    workspace_id=workspace_id,
                    chapter_id=chapter_id,
                    paths=WorkspacePaths(
                        workspace_id,
                        workspaces_root=self._workspace_service.workspaces_root,
                    ),
                )

        allowed_sources, target_status = _CARD_TRANSITION_RULES[action]
        now = datetime.now(UTC)
        from_status: str
        to_status = target_status
        applied_approved_at: datetime | None = None
        applied_rejection_notes: str | None = None

        session = self._session_factory(workspace_id)
        try:
            row = session.get(ChapterCardORM, chapter_id)
            if row is None:
                raise CardNotFoundError(
                    f"章节卡 ORM 行不存在: workspace={workspace_id} chapter={chapter_id}"
                )
            from_status = row.card_status
            if from_status not in allowed_sources:
                raise CardInvalidStateTransitionError(
                    chapter_id=chapter_id,
                    current_status=from_status,
                    action=action,
                    allowed_sources=allowed_sources,
                )
            if action == "approve":
                self._validate_card_dramatic_source_for_approval(
                    workspace_id=workspace_id,
                    chapter_id=chapter_id,
                )
                self._refresh_core_state_contract_for_approval(
                    workspace_id=workspace_id,
                    chapter_id=chapter_id,
                )
                self._validate_card_continuity_for_approval(
                    workspace_id=workspace_id,
                    chapter_id=chapter_id,
                )
            # 更新 ORM
            row.card_status = to_status
            if action == "approve":
                row.card_approved_at = now
                applied_approved_at = now
                # approve 时清除上一次的 rejection_notes(避免历史遗留)
                row.rejection_notes = None
            else:  # reject
                row.rejection_notes = rejection_notes
                applied_rejection_notes = rejection_notes
            session.commit()
        except Exception:
            session.rollback()
            raise
        finally:
            session.close()

        # ---- SSOT 文件 patch(可选,文件不存在时跳过)----
        self._patch_ssot_card_status(
            workspace_id=workspace_id,
            chapter_id=chapter_id,
            card_status=to_status,
            card_approved_at=applied_approved_at,
        )

        # ---- WS publish 2 事件 ----
        event_data: dict[str, Any] = {
            "chapter_id": chapter_id,
            "workspace_id": workspace_id,
            "from_status": from_status,
            "to_status": to_status,
        }
        if applied_approved_at is not None:
            event_data["card_approved_at"] = applied_approved_at.isoformat()
        if action == "reject":
            event_data["rejection_notes"] = applied_rejection_notes
        self._publish_card_event(
            chapter_id=chapter_id,
            event="card_status_changed",
            data=event_data,
        )
        self._publish_card_event(
            chapter_id=chapter_id,
            event="card_approved" if action == "approve" else "card_rejected",
            data=event_data,
        )

        _log.info(
            "chapter_card_status_transitioned",
            workspace_id=workspace_id,
            chapter_id=chapter_id,
            action=action,
            from_status=from_status,
            to_status=to_status,
            rejection_notes_present=applied_rejection_notes is not None,
        )

        result: dict[str, Any] = {
            "chapter_id": chapter_id,
            "from_status": from_status,
            "to_status": to_status,
        }
        if applied_approved_at is not None:
            result["card_approved_at"] = applied_approved_at
        if action == "reject":
            result["rejection_notes"] = applied_rejection_notes
        return result

    def regenerate_stale_unapproved_card(
        self,
        workspace_id: str,
        chapter_id: str,
    ) -> ChapterCard:
        """pipeline 到章时按当前 ledger 重生成预生成的无 provenance 草稿卡。"""

        current = self._chapter_service.read_chapter_card(
            workspace_id,
            chapter_id,
        )
        if current.card_status == "approved":
            raise CardContinuityLedgerStaleError(
                chapter_id=chapter_id,
                reason="approved 卡不得静默重生成",
                frozen=current.continuity_ledger_provenance,
                current=None,
            )
        return self.generate_single_chapter_card(
            workspace_id,
            chapter_id,
            chapter_number=current.chapter_number,
            volume_id=current.volume_id,
            card_status=current.card_status,
        )

    def regenerate_continuity_bridge_unapproved_card(
        self,
        workspace_id: str,
        chapter_id: str,
        *,
        required_actions: list[str],
    ) -> ChapterCard:
        """用 approve 的确定性失败证据定向重生成未批准卡。"""

        current = self._chapter_service.read_chapter_card(
            workspace_id,
            chapter_id,
        )
        if current.card_status == "approved":
            raise CardContinuityBridgeRequiredError(
                chapter_id=chapter_id,
                reason="approved 卡不得静默重生成",
                required_actions=required_actions,
            )
        if not required_actions:
            raise CardContinuityBridgeRequiredError(
                chapter_id=chapter_id,
                reason="定向重试缺少即时未完动作证据",
                required_actions=[],
            )
        return self.generate_single_chapter_card(
            workspace_id,
            chapter_id,
            chapter_number=current.chapter_number,
            volume_id=current.volume_id,
            card_status=current.card_status,
            continuity_bridge_retry_actions=required_actions,
            continuity_bridge_retry_base_card=current,
        )

    def _refresh_core_state_contract_for_approval(
        self,
        *,
        workspace_id: str,
        chapter_id: str,
    ) -> None:
        """批准前按当前权威重冻卡片，并立刻复核同一份合同。

        人工只改 ``time_gap`` 时，时钟区间会随卡片重算；但若上游已经提交
        同一状态的新值，旧 ``from_value`` 仍会失败，不能被静默改写。
        """

        if self._session_factory is None:
            raise ChapterCardServiceError("核心状态合同复核缺少 session_factory")
        paths = WorkspacePaths(
            workspace_id,
            workspaces_root=self._workspace_service.workspaces_root,
        )
        card_path = paths.cards_dir / f"{chapter_id}.json"
        if not card_path.is_file():
            _log.warning(
                "core_state_contract_skipped_missing_legacy_card",
                workspace_id=workspace_id,
                chapter_id=chapter_id,
            )
            return
        try:
            raw_card = json.loads(card_path.read_text(encoding="utf-8"))
        except (OSError, json.JSONDecodeError) as exc:
            raise ChapterCardServiceError(
                f"章节卡核心状态合同复核无法读取 SSOT: {exc}"
            ) from exc
        # Session 3/C 以前的磁盘卡没有版本字段；它们必须保持 legacy v0，
        # 不能因为后来打开了默认开关就被批准动作静默回填成第二套历史事实。
        if not isinstance(raw_card, dict) or "core_state_contract_version" not in raw_card:
            _log.info(
                "core_state_contract_skipped_legacy_card",
                workspace_id=workspace_id,
                chapter_id=chapter_id,
            )
            return
        service = CoreStateService(session_factory=self._session_factory)
        card = self._chapter_service.read_chapter_card(workspace_id, chapter_id)
        try:
            refreshed = service.freeze_card_contract(
                card=card,
                enabled=self._load_generation_config(
                    workspace_id
                ).core_state_hard_enabled,
            )
            service.validate_card_contract(
                workspace_id=workspace_id,
                card=refreshed,
            )
        except CoreStateServiceError as exc:
            raise ChapterCardServiceError(
                f"章节卡核心状态合同不能批准: {exc}"
            ) from exc
        if refreshed != card:
            self._chapter_service.save_chapter_card(workspace_id, refreshed)

    def _validate_card_dramatic_source_for_approval(
        self,
        *,
        workspace_id: str,
        chapter_id: str,
    ) -> None:
        """批准前复核 typed source；普通/legacy 卡不凭空补合同。"""

        paths = WorkspacePaths(
            workspace_id,
            workspaces_root=self._workspace_service.workspaces_root,
        )
        card_path = paths.cards_dir / f"{chapter_id}.json"
        if not card_path.is_file():
            return
        try:
            raw_card = json.loads(card_path.read_text(encoding="utf-8"))
        except (OSError, json.JSONDecodeError) as exc:
            raise ChapterCardServiceError(
                f"章节卡 typed 戏剧源复核无法读取 SSOT:{exc}"
            ) from exc
        requires_validation = bool(
            isinstance(raw_card, dict) and raw_card.get("dramatic_source") is not None
        )
        mini_path = paths.outline_dir / "mini_plan.json"
        if not requires_validation and mini_path.is_file():
            try:
                raw_mini = json.loads(mini_path.read_text(encoding="utf-8"))
                items = raw_mini.get("items", []) if isinstance(raw_mini, dict) else []
                requires_validation = any(
                    isinstance(item, dict)
                    and item.get("chapter_id") == chapter_id
                    and item.get("dramatic_contract") is not None
                    for item in items
                )
            except (OSError, json.JSONDecodeError) as exc:
                raise ChapterCardServiceError(
                    f"章节卡 typed 戏剧源复核无法读取 mini_plan:{exc}"
                ) from exc
        if not requires_validation:
            return
        card = self._chapter_service.read_chapter_card(workspace_id, chapter_id)
        try:
            load_and_validate_card_dramatic_source(
                card=card,
                outline_dir=paths.outline_dir,
                genre=self._workspace_service.info(workspace_id).genre,
            )
        except DramaticSourceContractError as exc:
            _log.error(
                "prewriting_semantic_gate_failed",
                failure_layer="card_approval_drama_stale",
                workspace_id=workspace_id,
                chapter_id=chapter_id,
                constraint_ids=["chapter_card_dramatic_source"],
                contract_hash=(
                    card.dramatic_source.source_hash
                    if card.dramatic_source is not None
                    else None
                ),
                attempt=0,
            )
            raise ChapterCardServiceError(
                f"章节卡 typed 戏剧源不能批准:{exc}"
            ) from exc

    def _validate_card_continuity_for_approval(
        self,
        *,
        workspace_id: str,
        chapter_id: str,
    ) -> None:
        """非首章 approve 前复核上一章 ledger provenance 与 bridge。"""

        chapter_seq = chapter_seq_from_id(chapter_id)
        if chapter_seq is None or chapter_seq <= 1:
            return
        if self._session_factory is None:
            raise CardContinuityLedgerStaleError(
                chapter_id=chapter_id,
                reason="缺少 session_factory",
                frozen=None,
                current=None,
            )
        card = self._chapter_service.read_chapter_card(workspace_id, chapter_id)
        prev_chapter_id = f"ch-{chapter_seq - 1:04d}"
        paths = WorkspacePaths(
            workspace_id,
            workspaces_root=self._workspace_service.workspaces_root,
        )
        from src.services.chapter_service import ChapterServiceError

        try:
            self._chapter_service.require_ready_for_next_chapter(
                workspace_id,
                prev_chapter_id,
            )
            ledger = load_validated_continuity_ledger(
                workspace_id=workspace_id,
                chapter_id=prev_chapter_id,
                paths=paths,
                session_factory=self._session_factory,
            )
            current = continuity_ledger_provenance(ledger)
        except (ChapterServiceError, ContinuityLedgerValidationError) as exc:
            raise CardContinuityLedgerStaleError(
                chapter_id=chapter_id,
                reason=str(exc),
                frozen=card.continuity_ledger_provenance,
                current=None,
            ) from exc
        try:
            validate_immediate_action_scope(
                actions=card.continuity_context.unfinished_actions,
                records=[ContinuityEvidenceRecord.model_validate(item) for item in ledger.evidence_records],
                frozen_indices=card.continuity_context.immediate_action_indices,
            )
            validate_frozen_provenance(
                frozen=card.continuity_ledger_provenance,
                current=current,
            )
            validate_continuity_projection(
                ledger_raw_final_tail=ledger.raw_final_tail,
                ledger_unfinished_actions=ledger.unfinished_actions,
                ledger_pov_character=ledger.pov_character,
                card_prev_chapter_tail=card.continuity_context.prev_chapter_tail,
                card_unfinished_actions=card.continuity_context.unfinished_actions,
                card_pov_from=card.continuity_context.pov_from,
            )
        except ValueError as exc:
            raise CardContinuityLedgerStaleError(
                chapter_id=chapter_id,
                reason=str(exc),
                frozen=card.continuity_ledger_provenance,
                current=current,
            ) from exc

        evidence_records = [
            ContinuityEvidenceRecord.model_validate(item)
            for item in ledger.evidence_records
        ]
        required_actions = [
            record.fact_text
            for record in evidence_records
            if record.field == "unfinished_actions" and record.is_immediate
        ]
        try:
            validate_continuity_bridge(
                evidence_records=evidence_records,
                time_gap=card.continuity_context.time_gap,
                scene_continuation=card.continuity_context.scene_continuation,
                continuity_bridge=card.continuity_context.continuity_bridge,
            )
        except ValueError as exc:
            raise CardContinuityBridgeRequiredError(
                chapter_id=chapter_id,
                reason=str(exc),
                required_actions=required_actions,
            ) from exc

    def _patch_ssot_card_status(
        self,
        *,
        workspace_id: str,
        chapter_id: str,
        card_status: str,
        card_approved_at: datetime | None,
    ) -> None:
        """同步 patch ``data/cards/<chapter_id>.json`` 的 ``card_status``
        / ``card_approved_at`` 字段;文件不存在 / JSON 损坏时静默跳过(只 log)。

        SSOT 不含 ``rejection_notes`` 字段(审核元数据走 ORM,卡 SSOT 是创意
        决策字段);因此 reject 时 SSOT 仅 patch ``card_status="rejected"``。
        """
        card_path = self._chapter_service.card_path_for(workspace_id, chapter_id)
        if not card_path.is_file():
            return
        try:
            data = json.loads(card_path.read_text(encoding="utf-8"))
            data["card_status"] = card_status
            data["card_approved_at"] = (
                card_approved_at.isoformat() if card_approved_at else None
            )
            card_path.write_text(
                json.dumps(data, ensure_ascii=False, indent=2),
                encoding="utf-8",
            )
        except (OSError, json.JSONDecodeError, ValueError) as exc:
            _log.warning(
                "chapter_card_ssot_patch_failed",
                workspace_id=workspace_id,
                chapter_id=chapter_id,
                error_type=type(exc).__name__,
                error_message=str(exc),
            )

    def _publish_card_event(
        self,
        *,
        chapter_id: str,
        event: str,
        data: dict[str, Any],
    ) -> None:
        """WS publish ``card_*`` event 到 ``chapter.{id}.card.events``;失败
        swallow + structlog(对齐 ``_publish_card_task_stage`` 同源)。"""
        if self._ws_manager is None:
            return
        try:
            self._ws_manager.publish(
                channel=chapter_card_events_channel(chapter_id),
                event=event,
                data=data,
            )
        except Exception as exc:  # noqa: BLE001
            _log.error(
                "chapter_card_event_notify_failed",
                chapter_id=chapter_id,
                event=event,
                error_type=type(exc).__name__,
                error_message=str(exc),
            )

    def _upsert_chapter_card_orm(
        self,
        *,
        workspace_id: str,
        chapter_id: str,
        card_status: str,
        card_approved_at: datetime | None = None,
    ) -> None:
        """upsert ``chapter_cards`` 表行;``session_factory=None`` 时 no-op。"""
        if self._session_factory is None:
            return
        session = self._session_factory(workspace_id)
        try:
            from src.services.asset_import import ensure_workspace_orm

            existing = session.get(ChapterCardORM, chapter_id)
            now = datetime.now(UTC)
            if existing is None:
                # 满足 FK:先 sync WorkspaceORM(对齐 ``ChapterService._upsert_chapter_state``)
                workspace = self._workspace_service.info(workspace_id)
                ensure_workspace_orm(session, workspace)
                session.add(
                    ChapterCardORM(
                        chapter_id=chapter_id,
                        workspace_id=workspace_id,
                        card_status=card_status,
                        card_approved_at=card_approved_at,
                        created_at=now,
                    )
                )
            else:
                existing.workspace_id = workspace_id
                existing.card_status = card_status
                if card_approved_at is not None:
                    existing.card_approved_at = card_approved_at
            session.commit()
        except Exception:
            session.rollback()
            raise
        finally:
            session.close()

    # ---- outline:commit 后异步生成首卡 ------------------------------------

    def generate_first_card_after_commit(
        self,
        workspace_id: str,
        *,
        task_id: str,
    ) -> dict[str, Any]:
        """outline:commit 后异步生成首张章节卡。

        本方法由 FastAPI ``BackgroundTasks`` 在响应已发出后执行,故**顶层
        try/except 兜底**(对齐 P4-B-08 ``cost_guard_notify_failed`` swallow
        模式):任何未预期异常 swallow + structlog + ``update_to_failed`` 改
        任务状态,**不**冒泡到 ASGI 服务器(冒泡会让上游接到二次响应错误,
        见 ``test_commit_success`` 走查)。

        - 固定只生成 ``ch-0001``，card_status = ``pending_review``。
        - 不接受批量大小或 beat 粒度配置；后续卡只能由正式 Run 在上一章
          富记忆 succeeded 后逐章生成。
        - 成功:WS publish ``stage`` event 到 ``task.{task_id}.progress``。
        - 失败:swallow + structlog ``chapter_card_first_generation_failed``；
          ORM/文件均不写 → 后续 chapter:write 触发 CARD_NOT_APPROVED；
          客户端可调 ``POST /chapters/{id}/card:generate`` 单章重试。
        - 完成:``task_registry.update_to_done`` + publish ``done`` 或
          ``card_failed`` event(含 ``cards_generated`` / ``cards_failed`` 列表)。

        :returns: ``{"cards_generated": [chapter_id, ...], "cards_failed":
            [{chapter_id, reason}, ...]}``
        """
        # 局部 import 规避 outline_service ↔ chapter_card_service 循环
        from src.api import task_registry

        chapter_id = "ch-0001"
        try:
            target = self._resolve_chapter_target(
                workspace_id, chapter_id=chapter_id
            )
            if target is None:
                raise ChapterCardServiceError(
                    "outline 已提交，但 volumes 未包含 ch-0001"
                )
        except Exception as exc:  # noqa: BLE001 — 顶层 swallow,见方法 docstring
            _log.error(
                "chapter_card_first_generation_setup_failed",
                workspace_id=workspace_id,
                task_id=task_id,
                error_type=type(exc).__name__,
                error_message=str(exc),
            )
            task_registry.update_to_failed(
                task_id,
                error={
                    "code": "FIRST_CARD_SETUP_FAILED",
                    "message": str(exc),
                },
            )
            return {
                "task_id": task_id,
                "workspace_id": workspace_id,
                "chapter_id": chapter_id,
                "cards_generated": [],
                "cards_failed": [],
                "setup_error": str(exc),
            }

        cards_generated: list[str] = []
        cards_failed: list[dict[str, str]] = []
        _, chapter_number, volume_id = target
        self._generate_one_card_for_task(
            workspace_id=workspace_id,
            task_id=task_id,
            chapter_id=chapter_id,
            chapter_number=chapter_number,
            volume_id=volume_id,
            initial_status="pending_review",
            cards_generated=cards_generated,
            cards_failed=cards_failed,
        )

        result = {
            "task_id": task_id,
            "workspace_id": workspace_id,
            "chapter_id": chapter_id,
            "cards_generated": cards_generated,
            "cards_failed": cards_failed,
        }

        final_stage = "card_failed" if cards_failed else "done"
        self._publish_card_task_stage(
            task_id=task_id,
            stage=final_stage,
            chapter_id=None,
            progress=1.0,
            card_status=None,
        )
        task_registry.update_to_done(task_id, result=result)

        _log.info(
            "chapter_card_first_generation_completed",
            workspace_id=workspace_id,
            task_id=task_id,
            generated_count=len(cards_generated),
            failed_count=len(cards_failed),
            generation_mode="single_first_card",
        )
        return result

    # ---- 单卡任务 helper ---------------------------------------------------

    def _generate_one_card_for_task(
        self,
        *,
        workspace_id: str,
        task_id: str,
        chapter_id: str,
        chapter_number: int,
        volume_id: str,
        initial_status: str,
        cards_generated: list[str],
        cards_failed: list[dict[str, str]],
    ) -> None:
        """单章生成 + try/except swallow + publish + 累加任务结果。

        从首卡生成路径抽出的 helper。任一异常 swallow +
        structlog ``chapter_card_generation_failed`` + cards_failed 累加(对齐方法
        docstring 顶层兜底语义)。
        """
        try:
            self.generate_single_chapter_card(
                workspace_id,
                chapter_id,
                chapter_number=chapter_number,
                volume_id=volume_id,
                card_status=initial_status,
                task_id=task_id,
            )
            cards_generated.append(chapter_id)
            self._publish_card_task_stage(
                task_id=task_id,
                stage="card_generated",
                chapter_id=chapter_id,
                progress=1.0,
                card_status=initial_status,
            )
        except ChapterCardGeneratorError as exc:
            reason = exc.error_code or "llm_output_invalid"
            _log.error(
                "chapter_card_generation_failed",
                workspace_id=workspace_id,
                task_id=task_id,
                chapter_id=chapter_id,
                chapter_number=chapter_number,
                volume_id=volume_id,
                reason=reason,
                model_used=exc.model_used,
            )
            cards_failed.append({"chapter_id": chapter_id, "reason": reason})
        except ChapterCardServiceError as exc:
            _log.error(
                "chapter_card_generation_failed",
                workspace_id=workspace_id,
                task_id=task_id,
                chapter_id=chapter_id,
                chapter_number=chapter_number,
                volume_id=volume_id,
                reason="context_derivation_failed",
                error=str(exc),
            )
            cards_failed.append(
                {
                    "chapter_id": chapter_id,
                    "reason": "context_derivation_failed",
                }
            )
        except Exception as exc:  # noqa: BLE001 — 顶层 swallow,见方法 docstring
            _log.error(
                "chapter_card_generation_failed",
                workspace_id=workspace_id,
                task_id=task_id,
                chapter_id=chapter_id,
                chapter_number=chapter_number,
                volume_id=volume_id,
                reason="unexpected_error",
                error_type=type(exc).__name__,
                error=str(exc),
            )
            cards_failed.append(
                {
                    "chapter_id": chapter_id,
                    "reason": "unexpected_error",
                }
            )

    def _resolve_chapter_target(
        self,
        workspace_id: str,
        *,
        chapter_id: str,
    ) -> tuple[str, int, str] | None:
        """把全书 ``chapter_id`` 解析为单章 ``(id,卷内序号,volume_id)``。

        - ``chapter_id`` 必须形如 ``ch-NNNN``。
        - 只解析这一章，不提供“从此开始再取 K 章”的预生成能力。
        - 跨卷连续编号，与卷表的 chapter_count 顺序一致。
        """
        if self._session_factory is None:
            raise ChapterCardServiceError("generate_next_card 需注入 session_factory")
        if not chapter_id.startswith("ch-"):
            raise ChapterCardServiceError(
                f"chapter_id 格式不合法,期望 'ch-NNNN': {chapter_id!r}"
            )
        try:
            target_idx = int(chapter_id[3:])
        except ValueError:
            raise ChapterCardServiceError(
                f"chapter_id 数字部分不合法: {chapter_id!r}"
            ) from None
        if target_idx < 1:
            raise ChapterCardServiceError(
                f"chapter_id 全书章序号必须 >= 1: {chapter_id!r}"
            )

        session = self._session_factory(workspace_id)
        try:
            volumes = (
                session.query(VolumeORM)
                .filter(VolumeORM.workspace_id == workspace_id)
                .all()
            )
        finally:
            session.close()
        if not volumes:
            raise ChapterCardServiceError(
                f"workspace {workspace_id} 没有 volumes 行(outline 未 commit?)"
            )

        global_chapter_idx = 0
        for vol in volumes:
            for chapter_number in range(1, vol.chapter_count + 1):
                global_chapter_idx += 1
                if global_chapter_idx == target_idx:
                    return chapter_id, chapter_number, vol.volume_id
        return None

    def generate_next_card(
        self,
        workspace_id: str,
        *,
        chapter_id: str,
        task_id: str,
    ) -> dict[str, Any]:
        """为正式 Run 生成当前唯一目标章的章节卡。

        该入口不接受 count，也不会继续生成后续卡。正式 Run 的 caller 在调用前
        已验证上一章富记忆 succeeded；本方法只负责当前章单卡生成并记任务结果。
        """
        from src.api import task_registry

        target = self._resolve_chapter_target(workspace_id, chapter_id=chapter_id)
        if target is None:
            raise ChapterCardServiceError(
                f"chapter_id={chapter_id} 超出全书章节范围,无可生成"
            )

        cards_generated: list[str] = []
        cards_failed: list[dict[str, str]] = []
        target_chapter_id, chapter_number, volume_id = target
        self._generate_one_card_for_task(
            workspace_id=workspace_id,
            task_id=task_id,
            chapter_id=target_chapter_id,
            chapter_number=chapter_number,
            volume_id=volume_id,
            initial_status="draft",
            cards_generated=cards_generated,
            cards_failed=cards_failed,
        )

        result = {
            "task_id": task_id,
            "workspace_id": workspace_id,
            "chapter_id": chapter_id,
            "cards_generated": cards_generated,
            "cards_failed": cards_failed,
        }
        final_stage = "card_failed" if cards_failed else "done"
        self._publish_card_task_stage(
            task_id=task_id,
            stage=final_stage,
            chapter_id=None,
            progress=1.0,
            card_status=None,
        )
        task_registry.update_to_done(task_id, result=result)
        _log.info(
            "chapter_card_next_generation_completed",
            workspace_id=workspace_id,
            task_id=task_id,
            chapter_id=chapter_id,
            generated_count=len(cards_generated),
            failed_count=len(cards_failed),
        )
        return result

    def regenerate_invalidated_cards_batch(
        self,
        workspace_id: str,
        *,
        chapter_ids: list[str],
        task_id: str,
    ) -> dict[str, Any]:
        """v1.0.x wave 5 session 4 (k):对 cascade 失效章节卡批量重生。

        由 ``retry_references`` 触发的 cascade 场景 — invalidated 章节卡需要
        覆盖重生(基于更新后的上下文派生新 must_achieve 等)。**不动 ChapterORM.status**
        (对齐 retry_references 类 A 处置 — 卡-driven cascade 限定卡)。

        和正式 Run 的 ``generate_next_card`` 区别:
        - **入参带 chapter_ids list**(而非 start_chapter_id + count) — cascade
          的章节 id 列表已由 ``_collect_subsequent_chapter_card_ids`` 给出
        - **顶层 try/except 兜底**(对齐 ``generate_first_card_after_commit`` 同型
          — 本方法由 FastAPI BackgroundTasks 在响应已发出后跑,任何未预期异常
          swallow + update_to_failed,不冒泡到 ASGI 服务器)
        - **全部 card_status='draft'** — cascade 重生不入 pending_review(用户
          需重新点 approve 走审核流)
        """
        # 局部 import 规避 outline_service ↔ chapter_card_service 循环
        from src.api import task_registry

        try:
            meta_list = self._collect_meta_for_chapter_ids(
                workspace_id, chapter_ids=chapter_ids
            )
        except Exception as exc:  # noqa: BLE001 — 顶层 swallow,见方法 docstring
            _log.error(
                "chapter_card_cascade_setup_failed",
                workspace_id=workspace_id,
                task_id=task_id,
                error_type=type(exc).__name__,
                error_message=str(exc),
            )
            task_registry.update_to_failed(
                task_id,
                error={"code": "CASCADE_SETUP_FAILED", "message": str(exc)},
            )
            return {
                "task_id": task_id,
                "workspace_id": workspace_id,
                "cards_generated": [],
                "cards_failed": [],
                "cascade_count": len(chapter_ids),
                "setup_error": str(exc),
            }

        cards_generated: list[str] = []
        cards_failed: list[dict[str, str]] = []
        # v1.0.x wave 6 s2 (Q4=A):cascade 路径接入自动 retry 检测,需要
        # 收集 cards_by_id + target_meta 供后置 _run_diversification_check_with_retry
        cards_by_id: dict[str, ChapterCard] = {}
        target_meta: list[tuple[str, int, str, str]] = []

        for idx, (chapter_id, chapter_number, volume_id) in enumerate(meta_list):
            try:
                card = self.generate_single_chapter_card(
                    workspace_id,
                    chapter_id,
                    chapter_number=chapter_number,
                    volume_id=volume_id,
                    card_status="draft",
                )
                cards_generated.append(chapter_id)
                cards_by_id[chapter_id] = card
                target_meta.append((chapter_id, chapter_number, volume_id, "draft"))
                self._publish_card_task_stage(
                    task_id=task_id,
                    stage="card_generated",
                    chapter_id=chapter_id,
                    progress=(idx + 1) / max(len(meta_list), 1),
                    card_status="draft",
                )
            except ChapterCardGeneratorError as exc:
                _log.error(
                    "chapter_card_cascade_partial",
                    workspace_id=workspace_id,
                    task_id=task_id,
                    chapter_id=chapter_id,
                    reason="llm_output_invalid",
                    model_used=exc.model_used,
                )
                cards_failed.append(
                    {"chapter_id": chapter_id, "reason": "llm_output_invalid"}
                )
            except ChapterCardServiceError as exc:
                _log.error(
                    "chapter_card_cascade_partial",
                    workspace_id=workspace_id,
                    task_id=task_id,
                    chapter_id=chapter_id,
                    reason="context_derivation_failed",
                    error=str(exc),
                )
                cards_failed.append(
                    {
                        "chapter_id": chapter_id,
                        "reason": "context_derivation_failed",
                    }
                )
            except Exception as exc:  # noqa: BLE001 — 顶层 swallow
                _log.error(
                    "chapter_card_cascade_partial",
                    workspace_id=workspace_id,
                    task_id=task_id,
                    chapter_id=chapter_id,
                    reason="unexpected_error",
                    error_type=type(exc).__name__,
                    error=str(exc),
                )
                cards_failed.append(
                    {"chapter_id": chapter_id, "reason": "unexpected_error"}
                )

        # v1.0.x wave 6 s2 (Q4=A):cascade 重生完毕 → 后置同质化检测 + 自动 retry
        # (与初始批量同型行为,避免 cascade 路径上保留'手动 retry'体验不一致)
        auto_retry_stats: dict[str, dict[str, Any]] = {}
        try:
            cards_by_id, auto_retry_stats = self._run_diversification_check_with_retry(
                workspace_id=workspace_id,
                task_id=task_id,
                target_meta=target_meta,
                cards_by_id=cards_by_id,
            )
        except Exception as exc:  # noqa: BLE001 — 检测层兜底 swallow
            _log.error(
                "chapter_card_cascade_diversification_setup_failed",
                workspace_id=workspace_id,
                task_id=task_id,
                error_type=type(exc).__name__,
                error_message=str(exc),
            )

        result = {
            "task_id": task_id,
            "workspace_id": workspace_id,
            "cards_generated": cards_generated,
            "cards_failed": cards_failed,
            "cascade_count": len(chapter_ids),
            "auto_retry_stats": auto_retry_stats,
        }
        final_stage = "batch_partial" if cards_failed else "done"
        self._publish_card_task_stage(
            task_id=task_id,
            stage=final_stage,
            chapter_id=None,
            progress=1.0,
            card_status=None,
        )
        task_registry.update_to_done(task_id, result=result)
        _log.info(
            "chapter_card_cascade_completed",
            workspace_id=workspace_id,
            task_id=task_id,
            generated_count=len(cards_generated),
            failed_count=len(cards_failed),
            cascade_count=len(chapter_ids),
        )
        return result

    def _collect_meta_for_chapter_ids(
        self,
        workspace_id: str,
        *,
        chapter_ids: list[str],
    ) -> list[tuple[str, int, str]]:
        """v1.0.x wave 5 session 4 (k):对指定 chapter_id list 跨卷查
        ``(chapter_id, chapter_number, volume_id)``。

        实施:扫 volumes 表构造 ``chapter_id → (chapter_number, volume_id)``
        全书映射(跨卷连续编号 ``ch-NNNN``，按卷表 chapter_count 展开)，
        然后按入参顺序查找;查不到的 id swallow + structlog warn,
        不阻断主路径(cascade list 来自 ChapterCardORM,理论上应都能命中,
        warn 仅作健壮防御)。
        """
        if self._session_factory is None:
            raise ChapterCardServiceError(
                "regenerate_invalidated_cards_batch 需注入 session_factory"
            )
        session = self._session_factory(workspace_id)
        try:
            volumes = (
                session.query(VolumeORM)
                .filter(VolumeORM.workspace_id == workspace_id)
                .all()
            )
        finally:
            session.close()
        if not volumes:
            raise ChapterCardServiceError(
                f"workspace {workspace_id} 没有 volumes 行(outline 未 commit?)"
            )

        chapter_id_meta: dict[str, tuple[int, str]] = {}
        global_idx = 0
        for vol in volumes:
            for chapter_number in range(1, vol.chapter_count + 1):
                global_idx += 1
                cid = f"ch-{global_idx:04d}"
                chapter_id_meta[cid] = (chapter_number, vol.volume_id)

        result: list[tuple[str, int, str]] = []
        for cid in chapter_ids:
            meta = chapter_id_meta.get(cid)
            if meta is None:
                _log.warning(
                    "chapter_card_cascade_meta_missing",
                    workspace_id=workspace_id,
                    chapter_id=cid,
                )
                continue
            chapter_number, volume_id = meta
            result.append((cid, chapter_number, volume_id))
        return result

    def _publish_card_task_stage(
        self,
        *,
        task_id: str,
        stage: str,
        chapter_id: str | None,
        progress: float,
        card_status: str | None,
        attempt: int | None = None,
        max_attempts: int | None = None,
    ) -> None:
        """WS publish 批量任务 stage event(失败 swallow + structlog)。

        :param attempt: v1.0.x wave 6 s2 引入。``auto_retry_attempt`` stage 专用,
            标记当前第几次 retry(1-based)。其他 stage(``card_generated`` / ``done``
            / ``batch_partial``)留 ``None`` 不写入 payload。
        :param max_attempts: 同上,``auto_retry_attempt`` stage 标记最大 retry 次数
            (典型 3)。
        """
        if self._ws_manager is None:
            return
        try:
            payload: dict[str, Any] = {
                "stage": stage,
                "chapter_id": chapter_id,
                "progress": progress,
                "card_status": card_status,
            }
            if attempt is not None:
                payload["attempt"] = attempt
            if max_attempts is not None:
                payload["max_attempts"] = max_attempts
            self._ws_manager.publish(
                channel=task_progress_channel(task_id),
                event="stage",
                data=payload,
            )
        except Exception as exc:  # noqa: BLE001
            _log.error(
                "chapter_card_task_notify_failed",
                task_id=task_id,
                stage=stage,
                error_type=type(exc).__name__,
                error_message=str(exc),
            )

    def _publish_card_parallel_status(
        self,
        *,
        task_id: str,
        snapshot: dict[str, Any],
    ) -> None:
        """把双 Judge 子任务全量快照投影到既有 task WS 频道。"""

        if self._ws_manager is None:
            return
        try:
            self._ws_manager.publish(
                channel=task_progress_channel(task_id),
                event="stage",
                data={
                    "stage": "chapter_card_semantic_gates",
                    "progress": 0.8,
                    **snapshot,
                },
            )
        except Exception as exc:  # noqa: BLE001 — UI 投影不得阻断卡片生成
            _log.error(
                "chapter_card_parallel_status_notify_failed",
                task_id=task_id,
                error_type=type(exc).__name__,
                error_message=str(exc),
            )

    # ---- v1.1 wave 1 session 2 (G6.2):字数下限/上限工作区级 backfill -----

    def reset_word_count_for_workspace(
        self,
        workspace_id: str,
        *,
        scope: Literal["unapproved", "all"] = "unapproved",
        word_count_min: int | None = None,
        word_count_max: int | None = None,
        word_count_policy_version: Literal[0, 1, 2] | None = None,
    ) -> dict[str, Any]:
        """v1.1 wave 1 session 2 (G6.2):工作区级批量重置已生成章节卡的字数下限/上限。

        痛场景(luo-cloud-test 21 章 wave 6 s2.6d 实证):wave 6 s2.6d-prep 把
        全局 default 调到 ``word_count_min=2500 / max=4000``(commit 8088055)后,
        老工作区已生成的章节卡仍用 prep 之前 default(``min=2800 / max=4500``),
        writer 实测稳定输出 ~2200-2800 字 → 反复撞 ``word_count|below_min``
        validator(21 章 74 次失败中 22 次 = 30% 都是 below_min)。

        本方法把指定 scope 下章节卡的 ``word_count_min/max`` 批量重置到 ``ChapterCard``
        model default 或用户指定值。

        设计要点
        ========

        1. **SSOT 写盘复用** ``ChapterService.save_chapter_card`` 原子写
           ``cards/<chapter_id>.json``;**不动 ``ChapterCardORM``**(P5-B-02
           最小列设计,表无 ``word_count_min/max`` 字段,只有审核状态)。
        2. **scope 默认 ``'unapproved'``**(自决项 Q1,1st 质量风险对冲):
           已 ``approved`` 卡用户审过字数预算,默认不动;用户明确需要时显式
           传 ``scope='all'`` 覆盖。
        3. **applied min/max 取自 ``ChapterCard`` model SSOT**(自决项 Q4,
           1st 质量收益):None 入参时回退到
           ``ChapterCard.model_fields["word_count_min"/"word_count_max"].default``,
           与 ``ChapterCardGenerator`` default 同源,改 model default 一处生效。
        4. **写盘失败 swallow + log + 计入 ``failed_chapter_ids``**(自决项 Q5):
           对齐 wave 6 s2.6a ``save_chapter_card`` race 防御 pattern,保持
           service 可用(部分失败也返回成功的列表)。

        :param workspace_id: 工作区 ID
        :param scope: ``'unapproved'``(默认,跳过 ``card_status='approved'``)
            / ``'all'``(含 approved)
        :param word_count_min: 应用的字数下限;``None`` = 用 ``ChapterCard``
            model default(2500)
        :param word_count_max: 应用的字数上限;``None`` = 用 ``ChapterCard``
            model default(4000)

        :returns: dict 含 ``workspace_id / scope /
            applied_word_count_min / applied_word_count_max /
            reset_chapter_ids / skipped_chapter_ids / failed_chapter_ids``

        :raises ChapterCardServiceError: workspace 不存在 / ``word_count_min >
            word_count_max`` / ``session_factory`` 未注入
        """
        # 校验 workspace 存在(WorkspaceServiceError 在 caller 路由层翻 404)
        self._workspace_service.info(workspace_id)

        # 取 applied 值(入参 → model default 回退)
        if word_count_min is None:
            applied_min = int(ChapterCard.model_fields["word_count_min"].default)
        else:
            applied_min = word_count_min
        if word_count_max is None:
            applied_max = int(ChapterCard.model_fields["word_count_max"].default)
        else:
            applied_max = word_count_max

        if word_count_policy_version not in (None, 0, 1, 2):
            raise ChapterCardServiceError("未知篇幅规则版本")
        if word_count_policy_version == 2 and applied_min != applied_max:
            raise ChapterCardServiceError("约数篇幅需要一个目标值，min/max 必须相同")
        if applied_min < 1 or applied_max < 1:
            raise ChapterCardServiceError(
                f"word_count_min / word_count_max 必须 >= 1,"
                f"实际 min={applied_min} / max={applied_max}"
            )
        if applied_min > applied_max:
            raise ChapterCardServiceError(
                f"word_count_min({applied_min}) 不能大于 word_count_max({applied_max})"
            )

        if self._session_factory is None:
            raise ChapterCardServiceError(
                "reset_word_count_for_workspace 需注入 session_factory"
            )

        # 取该 workspace 所有章节卡 ORM 行(chapter_id + card_status)
        session = self._session_factory(workspace_id)
        try:
            rows = [tuple(row) for row in session.execute(
                    select(
                        ChapterCardORM.chapter_id,
                        ChapterCardORM.card_status,
                    ).where(ChapterCardORM.workspace_id == workspace_id)
                ).all()]
        finally:
            session.close()

        reset_chapter_ids: list[str] = []
        skipped_chapter_ids: list[str] = []
        failed_chapter_ids: list[str] = []

        for chapter_id, card_status in sorted(rows, key=lambda r: r[0]):
            # scope='unapproved' → 跳过 approved
            if scope == "unapproved" and card_status == "approved":
                skipped_chapter_ids.append(chapter_id)
                continue
            try:
                card = self._chapter_service.read_chapter_card(workspace_id, chapter_id)
                updated = card.model_copy(
                    update={
                        "word_count_min": applied_min,
                        "word_count_max": applied_max,
                        **({"word_count_policy_version": word_count_policy_version}
                           if word_count_policy_version is not None else {}),
                    }
                )
                updated = ChapterCard.model_validate(updated.model_dump(mode="json"))
                self._chapter_service.save_chapter_card(workspace_id, updated)
                reset_chapter_ids.append(chapter_id)
            except Exception as exc:  # noqa: BLE001
                _log.error(
                    "chapter_card_reset_word_count_failed",
                    workspace_id=workspace_id,
                    chapter_id=chapter_id,
                    error_type=type(exc).__name__,
                    error=str(exc),
                )
                failed_chapter_ids.append(chapter_id)

        _log.info(
            "chapter_card_reset_word_count_done",
            workspace_id=workspace_id,
            scope=scope,
            applied_word_count_min=applied_min,
            applied_word_count_max=applied_max,
            reset_count=len(reset_chapter_ids),
            skipped_count=len(skipped_chapter_ids),
            failed_count=len(failed_chapter_ids),
        )

        return {
            "workspace_id": workspace_id,
            "scope": scope,
            "applied_word_count_min": applied_min,
            "applied_word_count_max": applied_max,
            **({"applied_word_count_policy_version": word_count_policy_version}
               if word_count_policy_version is not None else {}),
            "reset_chapter_ids": reset_chapter_ids,
            "skipped_chapter_ids": skipped_chapter_ids,
            "failed_chapter_ids": failed_chapter_ids,
        }


__all__ = [
    "CardAction",
    "CardInvalidStateTransitionError",
    "CardNotFoundError",
    "ChapterCardContextDerivationError",
    "ChapterCardService",
    "ChapterCardServiceError",
    "RetryReferencesResult",
    "SessionFactory",
]
