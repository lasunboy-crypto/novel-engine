"""Writer agent(P1-B-10 简版,P2-B-10 扩 retriever + voice)。

职责:把章节卡 + 文件式记忆段 + 已拼好的衔接桥 → 拼成 system + user 两条
消息的 prompt → 调注入的 ``LLMClient`` → 把响应包成 ``WriterOutput``。

P2-B-10 扩展点
==============

P2 起,Writer 在调用方注入 ``vector_store`` / ``db_session`` 时,自动:

1. **检索记忆段(user message)**:调 ``retrieve_memories_for_chapter``
   按 ``where={"entity_type": "fact"}`` / ``where={"$and": [...character +
   $in entity_id]}`` 两条 query 各拿 ``retriever_top_k`` 条,合并拼装到
   user message 的"## 检索记忆"段(在文件式记忆段之后)。
2. **角色声音档案段(system message)**:从 ``db_session`` 查
   ``card.related_characters`` 各角色的 ``voice_profile``,拼装到 system
   message 末尾的"## 出场角色声音档案"段。

**默认值 ``None`` 时退化为 P1 路径**(单测注入 mock + 不传 vs/db 仍能跑),
让 P1 已有 writer 测试 / Orchestrator 调用 / 章节服务路径零回归。

设计要点(沿用 P1)
====================

- **依赖反转**:函数签名只接 ``LLMClient`` 协议(基类),不 import
  ``ClaudeClient``;具体客户端实例由 P1-B-13 Orchestrator 注入。
- **提示词模板与变量分离**:静态 system 文本来自 ``prompts/writer.md``,
  动态变量(章节卡 / 记忆 / 衔接桥 / 检索段)由 Python 拼到 user 消息;
  P2-B-10 自决项:**voice_profile 数据**沿任务模板字面"system prompt 注入"
  在 system 末尾追加,模板本体仍是静态 markdown,只在调用时段动态拼装,
  改写器规则(如 voice 段描述)走模板,改具体角色 voice 数据走代码,职责
  清晰。
- **提示词路径来自 Settings**:函数参数 ``prompts_dir`` 默认 ``None`` 时
  从 ``settings.prompts_dir`` 取;P3 引入工作区级覆盖时只改 Settings/
  上层注入,Writer 零改动。
- **不在 Writer 内实例化 ``ChapterCard`` / ``ContinuityContext``**:调用方
  ChapterService(P1-B-15)负责拼装,Writer 只消费。

P1 起步阶段不实现的部分能力已在后续周次接线；post-v6 正文生产固定单稿。自动
重试(已在 ClaudeClient 层处理)。P2 不实现:retriever 失败重试(归
caller)、检索段语义重写(P3+)。
"""

from __future__ import annotations

import math
import re
from pathlib import Path
from typing import Any, Literal

import structlog
from pydantic import BaseModel, ConfigDict, Field, JsonValue
from sqlalchemy.orm import Session

from src.config import settings
from src.core.agents.structured_handoff import handoff_user_text
from src.core.rules.prose_length_policy import render_word_count_task
from src.core.agents.writer_narrative_input import active_character_ids, build_narrative_bundle
from src.core.agents._name_lock import (
    RelationshipLockCharacter,
    build_relationship_lock_lines_from_records,
)
from src.core.agents._prompt import (
    append_role_marker,
    load_prompt_template,
    resolve_routing,
)
from src.core.agents._structured_output import prepare_strict_json_output
from src.core.agents.creative_delta import (
    uses_hy3_authority_assembly,
    uses_hy3_creative_delta,
    uses_hy3_draft_v3,
    uses_hy3_draft_v4,
    uses_hy3_draft_v5,
)
from src.core.agents.writer_draft_view import (
    build_writer_draft_view,
    draft_instruction_leakage,
)
from src.core.agents.writer_salience import (
    build_writer_authority_sections,
    render_dramatic_context,
    render_dramatic_source,
)
from src.core.db import PlotThreadORM
from src.core.llm.base import LLMClient, Message, TokenUsage
from src.core.llm.strict_json_schema import normalize_json_string_c0_controls
from src.core.memory.retriever import (
    DEFAULT_COLLECTION,
    RetrievedMemory,
    retrieve_memories_for_chapter,
)
from src.core.memory.store import get_character
from src.core.memory.vector import VectorStore
from src.core.models.chapter_card import (
    ChapterCard,
    ContinuityContext,
    chapter_seq_from_id,
)
from src.core.models.character import (
    Character,
    format_state_snapshot_lines,
    latest_state_snapshot_before,
)
from src.core.models.continuity_ledger import (
    ContinuityLedgerProvenance,
    ProvenanceBoundContinuityCapsule,
)
from src.core.models.core_state import CoreStateProjection
from src.core.models.review import ConstraintResult, ConstraintViolation
from src.core.models.setting_contract import SettingContract
from src.core.models.style_template import STYLE_GENERATION_DEFAULTS
from src.core.routing.router import ModelRouter
from src.core.rules.prose_hard_contract import (
    ExecutableConstraint,
    ExecutableConstraintPlan,
    PARAGRAPH_MEAN_HARD_CONSTRAINT_ID,
    PARAGRAPH_MEAN_HARD_MIN,
    ProseConstraintStage,
    compile_executable_constraint_plan,
    render_executable_constraint_plan,
)
from src.core.rules.prose_revision_contract import (
    LocalizedRevisionResponse,
    ProseRevisionContract,
    apply_revision_response,
    render_revision_contract,
)
from src.core.rules.style_drift_validator import (
    PARAGRAPH_LENGTH_MEAN_DRIFT_THRESHOLD,
    SENTENCE_LENGTH_MEAN_DRIFT_THRESHOLD,
    V1_BASELINE_ANOMALY_THRESHOLD,
    quantize_prompt_visible_style_mean,
    resolve_rewrite_style_constraint_spec,
    v1_baseline_anomaly_details,
)
from src.core.rules.style_fingerprint import PovSummary
from src.core.workspace.meta import read_meta
from src.core.workspace.workspace_paths import WorkspacePaths

_log = structlog.get_logger(__name__)

_WRITER_PROMPT_FILENAME = "writer.md"

# Writer 默认生成参数:写作温度比审稿/修订高,长度足以覆盖 4500 字上限
# 章节(中文 ≈ 6000-7000 token);调用方可显式覆盖。
_DEFAULT_TEMPERATURE = 0.85
_DEFAULT_MAX_TOKENS = 8000

# P2-B-10 retriever 默认 top_k(比 P2-B-08 retriever 默认 20 小):
# 单 query top_K=20 时两条 where(fact + character)合计可能塞 40 条
# 进 prompt,每条 100-300 中文字 → 4000-12000 字仅检索段,挤占正文写作
# 字数预算。10 比较稳:fact 10 + character 10 ≈ 20 条,prompt 体积可控。
# 调用方可显式覆盖。
_DEFAULT_RETRIEVER_TOP_K = 10

_NEXT_EVENT_HOLDBACK_PREFIX = "本章不得完整触发、完成或揭示下一章保留事件："
_NEXT_EVENT_HOLDBACK_SUFFIX = "；只允许不造成结果落地的必要铺垫"


class WriterProseEnvelopeError(ValueError):
    """A historical exact-Hy3 prose envelope cannot be assembled losslessly."""


class WriterProseEnvelope(BaseModel):
    """Historical single-field wire envelope retained for evidence replay.

    Production full drafts no longer use this envelope: REAL-11 showed that a
    decoder-level ``content.minLength`` can keep a semantically complete scene
    open and make constrained generation repeat it.  The type remains solely
    so frozen T5R4M request/response evidence can still be audited byte-for-byte.
    """

    model_config = ConfigDict(extra="forbid", frozen=True, strict=True)

    content: str


def build_writer_prose_envelope_model(
    *, minimum: int, maximum: int
) -> type[WriterProseEnvelope]:
    """Build the historical T5R4M envelope for immutable evidence replay."""

    if (
        isinstance(minimum, bool)
        or isinstance(maximum, bool)
        or not isinstance(minimum, int)
        or not isinstance(maximum, int)
        or minimum <= 0
        or maximum < minimum
    ):
        raise WriterProseEnvelopeError(
            f"Writer 正文长度权威非法:minimum={minimum!r},maximum={maximum!r}"
        )

    class _AuthorityBoundWriterProseEnvelope(WriterProseEnvelope):
        content: str = Field(min_length=minimum, max_length=maximum)

    return _AuthorityBoundWriterProseEnvelope


def extract_writer_prose_content(
    *,
    raw_content: str,
    output_model: type[WriterProseEnvelope],
) -> str:
    """Decode one strict envelope and return the model's prose without rewriting it."""

    try:
        normalized = normalize_json_string_c0_controls(raw_content)
        envelope = output_model.model_validate_json(normalized.normalized_content)
    except ValueError as exc:
        raise WriterProseEnvelopeError(
            "exact Hy3 Writer 响应不是 authority-bound prose envelope"
        ) from exc
    return envelope.content


WriterDraftStepPurpose = Literal[
    "dramatic_actor_goal",
    "dramatic_counteraction",
    "dramatic_choice",
    "dramatic_consequence",
    "must_achieve",
    "arc_beat",
]


class WriterDraftAuthorityQuote(BaseModel):
    """One verbatim ChapterCard authority reference used by a transient plan."""

    model_config = ConfigDict(extra="forbid", frozen=True, strict=True)

    source_path: str
    text: str


class WriterDraftExpansionStep(BaseModel):
    """One non-creative allocation lane in an exact-Hy3 draft plan."""

    model_config = ConfigDict(extra="forbid", frozen=True, strict=True)

    step_id: str
    purpose: WriterDraftStepPurpose
    authority: tuple[WriterDraftAuthorityQuote, ...] = ()
    depends_on: tuple[str, ...] = ()
    soft_min_chars: int = Field(ge=1)
    soft_max_chars: int = Field(ge=1)


class WriterDraftExpansionPlan(BaseModel):
    """Transient authority-only organization for one complete draft request.

    The plan is never persisted and contains no invented event, dialogue,
    detail, or narrative language.  It only gives already-frozen ChapterCard
    authority separate forward-progress space before the single Provider call.
    """

    model_config = ConfigDict(extra="forbid", frozen=True, strict=True)

    target_chars: int = Field(ge=1)
    dramatic_source_hash: str | None = Field(None, pattern=r"^[0-9a-f]{64}$")
    steps: tuple[WriterDraftExpansionStep, ...] = Field(min_length=1)


class WriterDraftIntegrityError(ValueError):
    """An experimental full draft failed a non-mutating local integrity check."""

    def __init__(self, *, reasons: tuple[str, ...], details: dict[str, Any]) -> None:
        self.reasons = reasons
        self.details = details
        super().__init__(f"Writer 正文完整性失败:{','.join(reasons)}")


class WriterDraftAuthorityError(ValueError):
    """An exact-Hy3 draft has no authority-bearing executable action."""


def _writer_authority_key(text: str) -> str:
    """Normalize whitespace only for exact authority de-duplication."""

    return " ".join(text.split())


def _draft_expansion_refs(
    plan: WriterDraftExpansionPlan,
) -> dict[str, str]:
    """Map normalized story authority to its one Writer-visible short reference."""

    refs: dict[str, str] = {}
    for step in plan.steps:
        for quote in step.authority:
            key = _writer_authority_key(quote.text)
            existing = refs.setdefault(key, step.step_id)
            if existing != step.step_id:
                raise WriterDraftAuthorityError(
                    "exact Hy3 Writer 同一自然事实映射到多个展开步骤:"
                    f"{existing},{step.step_id}"
                )
    return refs


def _draft_expansion_ref(
    *,
    plan: WriterDraftExpansionPlan,
    text: str,
) -> str:
    """Resolve one story fact to the canonical expansion-plan reference."""

    ref = _draft_expansion_refs(plan).get(_writer_authority_key(text))
    if ref is None:
        raise WriterDraftAuthorityError(
            "exact Hy3 Writer 内容契约存在未进入展开计划的必达事实"
        )
    return ref


def _next_event_holdback_event(rule: str) -> str | None:
    """Return the frozen future event from the deterministic holdback rule."""

    if not rule.startswith(_NEXT_EVENT_HOLDBACK_PREFIX):
        return None
    event_and_suffix = rule[len(_NEXT_EVENT_HOLDBACK_PREFIX) :]
    if not event_and_suffix.endswith(_NEXT_EVENT_HOLDBACK_SUFFIX):
        return None
    event = event_and_suffix[: -len(_NEXT_EVENT_HOLDBACK_SUFFIX)].strip()
    return event or None


def _constraint_index(item: ExecutableConstraint) -> int:
    """Read one frozen list index without accepting bool/string coercion."""

    value = item.parameter("index")
    if isinstance(value, bool) or not isinstance(value, int) or value < 0:
        raise ValueError(
            f"ExecutableConstraint {item.constraint_id} index 参数非法"
        )
    return value


def _writer_constraint_authority_ref(
    *,
    item: ExecutableConstraint,
    draft_expansion_plan: WriterDraftExpansionPlan,
) -> str | None:
    """Point duplicate Writer HARD rows at the section owning the natural text."""

    if item.kind == "must_achieve":
        return _draft_expansion_ref(
            plan=draft_expansion_plan,
            text=item.rule_text,
        )
    if item.kind == "must_unresolved":
        return f"must-unresolved-{_constraint_index(item) + 1}"
    if item.kind == "forbidden":
        index = _constraint_index(item)
        if _next_event_holdback_event(item.rule_text) is not None:
            return f"chapter-boundary-holdback-{index + 1}"
        return f"forbidden-{index + 1}"
    if item.kind == "continuity_unfinished_actions":
        return "continuity-unfinished-actions"
    if item.kind == "continuity_scene_same":
        return "continuity-scene"
    if item.kind == "setting_unique_rules":
        return "setting-contract-unique-rules"
    return None


def _render_writer_executable_constraint_plan(
    *,
    plan: ExecutableConstraintPlan,
    draft_expansion_plan: WriterDraftExpansionPlan,
) -> str:
    """Render full HARD identity while de-duplicating Writer-facing story prose.

    The immutable ``ExecutableConstraintPlan`` remains unchanged and downstream
    consumers still receive every original ``rule_text``.  Only this exact-Hy3
    draft display replaces a repeated story sentence with a local authority
    reference whose full text is visible in another section of the same prompt.
    """

    lines = [
        "## 本轮正文约束合同(WRITER/VALIDATOR SSOT)\n\n",
        f"- chapter_id:`{plan.chapter_id}`\n",
        f"- stage:`{plan.stage}`\n",
        f"- plan_hash:`{plan.plan_hash}`\n",
        "- 规则：只能执行下表 HARD；Judge 不得新增下表之外的 HARD。\n",
        "- `authority_ref` 指向本消息中唯一一次出现的自然事实原文；引用不会削弱约束。\n\n",
        "| constraint_id | severity | stages | provenance | kind | source | source_hash | HARD 通过条件 |\n",
        "|---|---|---|---|---|---|---|---|\n",
    ]
    for item in plan.constraints:
        ref = _writer_constraint_authority_ref(
            item=item,
            draft_expansion_plan=draft_expansion_plan,
        )
        rule = (
            f"执行 authority_ref=`{ref}` 所指原文"
            if ref is not None
            else item.rule_text
        )
        rule = rule.replace("|", "｜").replace("\n", " ")
        source = item.source_path.replace("|", "｜")
        source_hash = item.source_hash or "-"
        stages = ",".join(item.stages)
        lines.append(
            f"| {item.constraint_id} | {item.severity} | {stages} | "
            f"{item.provenance} | {item.kind} | {source} | {source_hash} | "
            f"{rule} |\n"
        )
    return "".join(lines).rstrip()


def build_writer_draft_expansion_plan(
    chapter_card: ChapterCard,
) -> WriterDraftExpansionPlan:
    """Allocate space only to existing, executable ChapterCard actions.

    Continuity, unresolved questions, and the next-chapter hook remain visible in
    the ordinary Writer prompt, but they are not independent action capacity.
    """

    seeds: list[
        tuple[
            str,
            WriterDraftStepPurpose,
            tuple[WriterDraftAuthorityQuote, ...],
            tuple[str, ...],
            float,
        ]
    ] = []
    used_authority: set[str] = set()
    dramatic_source_hash: str | None = None

    dramatic_source = chapter_card.dramatic_source
    if dramatic_source is not None:
        dramatic_source_hash = dramatic_source.source_hash
        dramatic_steps: tuple[tuple[str, WriterDraftStepPurpose, str], ...] = (
            (
                "dramatic-actor-goal",
                "dramatic_actor_goal",
                "actor_goal",
            ),
            (
                "dramatic-counteraction",
                "dramatic_counteraction",
                "counteraction",
            ),
            (
                "dramatic-choice",
                "dramatic_choice",
                "choice",
            ),
            (
                "dramatic-consequence",
                "dramatic_consequence",
                "consequence",
            ),
        )
        previous_step_id: str | None = None
        for step_id, purpose, field_name in dramatic_steps:
            text = getattr(dramatic_source.beat_contract, field_name)
            key = _writer_authority_key(text)
            if not key or key in used_authority:
                raise WriterDraftAuthorityError(
                    "exact Hy3 Writer typed 戏剧行动为空或重复："
                    f"dramatic_source.beat_contract.{field_name}"
                )
            used_authority.add(key)
            depends_on = (previous_step_id,) if previous_step_id is not None else ()
            seeds.append(
                (
                    step_id,
                    purpose,
                    (
                        WriterDraftAuthorityQuote(
                            source_path=(f"dramatic_source.beat_contract.{field_name}"),
                            text=text,
                        ),
                    ),
                    depends_on,
                    1.0,
                )
            )
            previous_step_id = step_id

    must_count = 0
    for index, must in enumerate(chapter_card.must_achieve):
        key = _writer_authority_key(must)
        if not key or key in used_authority:
            continue
        used_authority.add(key)
        must_count += 1
        step_id = f"must-{must_count}"
        seeds.append(
            (
                step_id,
                "must_achieve",
                (
                    WriterDraftAuthorityQuote(
                        source_path=f"must_achieve[{index}]",
                        text=must,
                    ),
                ),
                (),
                1.4,
            )
        )

    arc_count = 0
    for character_id, arc_beat in chapter_card.arc_beats.items():
        key = _writer_authority_key(arc_beat)
        if not key or key in used_authority:
            continue
        used_authority.add(key)
        arc_count += 1
        seeds.append(
            (
                f"arc-beat-{arc_count}",
                "arc_beat",
                (
                    WriterDraftAuthorityQuote(
                        source_path=f"arc_beats[{character_id}]",
                        text=arc_beat,
                    ),
                ),
                (),
                1.0,
            )
        )

    if not seeds:
        raise WriterDraftAuthorityError(
            "exact Hy3 Writer 首稿缺少可执行动作权威："
            "dramatic_source、must_achieve 与 arc_beats 均为空"
        )

    target = max(
        chapter_card.word_count_min,
        min(
            chapter_card.word_count_max,
            (chapter_card.word_count_min + chapter_card.word_count_max) // 2,
        ),
    )
    total_weight = sum(seed[4] for seed in seeds)
    steps = tuple(
        WriterDraftExpansionStep(
            step_id=step_id,
            purpose=purpose,
            authority=authority,
            depends_on=depends_on,
            soft_min_chars=max(
                80,
                round(chapter_card.word_count_min * weight / total_weight),
            ),
            soft_max_chars=max(
                100,
                round(chapter_card.word_count_max * weight / total_weight),
            ),
        )
        for step_id, purpose, authority, depends_on, weight in seeds
    )
    return WriterDraftExpansionPlan(
        target_chars=target,
        dramatic_source_hash=dramatic_source_hash,
        steps=steps,
    )


def render_writer_draft_expansion_plan(plan: WriterDraftExpansionPlan) -> str:
    """Render the byte-stable v1 plan used by frozen exact-Hy3 activities."""

    purpose_rules = {
        "dramatic_actor_goal": "先让行动者以具体行动追求该目标，不用概述替代演出。",
        "dramatic_counteraction": "让反作用实际改变局面，承接目标但不重演目标动作。",
        "dramatic_choice": "让人物在前述冲突下作出该选择，不另造第三种选择。",
        "dramatic_consequence": "只演出该选择已经明示的可见后果，不补造新事实。",
        "must_achieve": "只完成本条必达的新增推进；不要复写已经完成的前一步。",
        "arc_beat": "用该权威原文指定的具体行动演出角色变化，不用旁白宣告替代。",
    }
    rendered_steps: list[str] = []
    for index, step in enumerate(plan.steps, start=1):
        authority_lines = [
            f"  - `{quote.source_path}` 原文：{quote.text}" for quote in step.authority
        ]
        if step.depends_on:
            authority_lines.append(
                "  - 仅回收已完成步骤：" + ", ".join(step.depends_on)
            )
        rendered_steps.append(
            f"{index}. `{step.step_id}`（SOFT 约 {step.soft_min_chars}—"
            f"{step.soft_max_chars} 字符）\n"
            f"{purpose_rules[step.purpose]}\n" + "\n".join(authority_lines)
        )
    return (
        "## exact-Hy3 单次首稿展开计划（TRANSIENT / AUTHORITY-ONLY）\n\n"
        f"全章软目标约 {plan.target_chars} 字符；章节卡字数区间仍由本地产品校验，"
        "不是 decoder 强迫补齐。本计划只重排现有权威，不替你编造正文；具体动作、"
        "对白、感官、心理、互动和叙事语言仍由你自由创作，但不得另起无权威支线或改写事实。\n\n"
        "- 每一步必须产生与前一步不可互换的新推进；已经写完的场景不得换词重演。\n"
        "- 标明依赖的 typed 行动轴须按依赖推进；其余步骤是覆盖与软字数分配，"
        "可按场景自然交织。"
        "正文必须由你直接自然分段，段落之间输出一个空行；不得返回单块正文，"
        "程序不会事后替正文插入空行。\n"
        "- 复用人物姓名、称谓和专名时逐字遵守权威原文，不得改字或混写。\n"
        "- 最终只返回正文，不输出本计划的标题、编号、source path、解释或 JSON。\n\n"
        + "\n\n".join(rendered_steps)
    )


def _render_writer_draft_expansion_plan_v2(
    plan: WriterDraftExpansionPlan,
) -> str:
    """Render the opt-in v2 plan with bounded positive scene expansion."""

    purpose_rules = {
        "dramatic_actor_goal": "让行动者在场景中实际追求该目标，写出启动与推进过程。",
        "dramatic_counteraction": "让反作用进入现场并改变局面，形成新的行动压力。",
        "dramatic_choice": "让人物经过可感的权衡与互动后作出该选择。",
        "dramatic_consequence": "让已明示的后果真实落到环境、关系或人物处境中。",
        "must_achieve": "把本条必达写成可见行动及其即时反馈，与前后动作自然咬合。",
        "arc_beat": "用具体行动、反应或代价让角色变化自然发生。",
    }
    rendered_steps: list[str] = []
    for index, step in enumerate(plan.steps, start=1):
        authority_lines = [
            f"  - `{quote.source_path}` 原文：{quote.text}" for quote in step.authority
        ]
        if step.depends_on:
            authority_lines.append(
                "  - 仅回收已完成步骤：" + ", ".join(step.depends_on)
            )
        rendered_steps.append(
            f"{index}. `{step.step_id}`（SOFT 约 {step.soft_min_chars}—"
            f"{step.soft_max_chars} 字符）\n"
            f"{purpose_rules[step.purpose]}\n" + "\n".join(authority_lines)
        )
    return (
        "## exact-Hy3 单次首稿展开计划（TRANSIENT / AUTHORITY-ONLY）\n\n"
        f"全章软目标约 {plan.target_chars} 字符；章节卡字数区间仍由本地产品校验，"
        "不是 decoder 强迫补齐。下列每条自然事实全文只出现一次；其它区块以 "
        "`authority_ref` 引用它。\n\n"
        "- 在不改变这些事实及结果的前提下，你拥有场景内的演出空间：可展开动作的"
        "尝试与受阻、同伴的即时反应、环境反馈、对话推进、心理权衡和后果余波。"
        "这些细节不得变成新的长期事实、支线任务或额外结局。\n"
        "- 每一步都要带来新的局面、关系或感知变化；同一场景可以自然交织多个步骤，"
        "不必写成逐项答题。标明依赖的行动按依赖顺序推进。\n"
        "- 完成一个动作只表示不要重复解释这个动作；在全部权威事实演足且正文达到"
        "合法字数前，不要提前结束整章。\n"
        "正文必须由你直接自然分段，段落之间输出一个空行；不得返回单块正文，"
        "程序不会事后替正文插入空行。\n"
        "- 复用人物姓名、称谓和专名时逐字遵守权威原文，不得改字或混写。\n"
        "- 最终只返回正文，不输出本计划的标题、编号、source path、解释或 JSON。\n\n"
        + "\n\n".join(rendered_steps)
    )


def _is_full_book_opening(card: ChapterCard, session: Session | None) -> bool:
    from src.core.agents.writer_narrative_input import is_full_book_opening
    from src.core.db import VolumeORM
    from sqlalchemy import select

    if session is None:
        return False
    volumes = list(session.scalars(select(VolumeORM).where(
        VolumeORM.workspace_id == card.workspace_id).order_by(VolumeORM.volume_id)))
    return is_full_book_opening(card, [volume.volume_id for volume in volumes])


def _find_macro_repetition(
    content: str, *, window: int = 240
) -> tuple[int, int] | None:
    """Return two non-overlapping positions for a long exact repeated span."""

    normalized = re.sub(r"\s+", "", content)
    if len(normalized) < window * 2:
        return None
    first_positions: dict[str, int] = {}
    for index in range(0, len(normalized) - window + 1):
        fragment = normalized[index : index + window]
        first = first_positions.setdefault(fragment, index)
        if index - first >= window:
            return first, index
    return None


def diagnose_writer_draft_integrity(
    *,
    content: str,
    minimum: int,
    maximum: int,
) -> tuple[tuple[str, ...], dict[str, Any]]:
    """Collect every local draft defect without changing product first-error order."""

    reasons: list[str] = []
    length = len(content)
    details: dict[str, Any] = {
        "actual": length,
        "minimum": minimum,
        "maximum": maximum,
    }
    if length < minimum:
        reasons.append("length_below_minimum")
    elif length > maximum:
        reasons.append("length_above_maximum")

    paragraphs = [
        part.strip() for part in re.split(r"\n[ \t]*\n+", content) if part.strip()
    ]
    details["paragraph_count"] = len(paragraphs)
    if len(paragraphs) < 2:
        reasons.append("missing_model_paragraphs")

    repetition = _find_macro_repetition(content)
    if repetition is not None:
        reasons.append("macro_repetition")
        details["repetition_positions"] = repetition

    stripped = content.lstrip()
    control_points = sorted(
        {ord(char) for char in content if ord(char) < 32 and char not in "\n\r\t"}
    )
    leaked_envelope = bool(re.match(r'^\{\s*"(?:content|paragraphs)"\s*:', stripped))
    unbalanced_braces = content.count("{") != content.count("}")
    if control_points or leaked_envelope or unbalanced_braces or "\ufffd" in content:
        reasons.append("output_garbage")
        details["control_points"] = control_points
        details["leaked_envelope"] = leaked_envelope
        details["unbalanced_braces"] = unbalanced_braces
    return tuple(reasons), details


def validate_writer_draft_integrity(
    *, content: str, minimum: int, maximum: int, check_length: bool = True
) -> str:
    """Fail closed on an exact-Hy3 full draft without changing model prose."""

    reasons, details = diagnose_writer_draft_integrity(
        content=content,
        minimum=minimum,
        maximum=maximum,
    )
    if not check_length:
        # 正式 v3 的字数交给原有正文 validator/预算；实验默认仍在首错停止。
        reasons = tuple(
            reason for reason in reasons
            if reason not in {"length_below_minimum", "length_above_maximum"}
        )
    if "length_below_minimum" in reasons:
        raise WriterDraftIntegrityError(
            reasons=("length_below_minimum",),
            details={
                key: details[key]
                for key in ("actual", "minimum", "maximum")
            },
        )
    if "length_above_maximum" in reasons:
        raise WriterDraftIntegrityError(
            reasons=("length_above_maximum",),
            details={
                key: details[key]
                for key in ("actual", "minimum", "maximum")
            },
        )
    if reasons:
        product_details: dict[str, Any] = {"actual": details["actual"]}
        if "missing_model_paragraphs" in reasons:
            product_details["paragraph_count"] = details["paragraph_count"]
        if "macro_repetition" in reasons:
            product_details["repetition_positions"] = details[
                "repetition_positions"
            ]
        if "output_garbage" in reasons:
            for key in (
                "control_points",
                "leaked_envelope",
                "unbalanced_braces",
            ):
                product_details[key] = details[key]
        raise WriterDraftIntegrityError(
            reasons=reasons,
            details=product_details,
        )
    return content


class WriterOutput(BaseModel):
    """Writer 单章输出。

    与 ``LLMResponse`` 字段近似但有意改名以反映 agent 维度语义:
    - ``token_usage`` vs LLMResponse 的 ``usage``:agent 层向上暴露的字段
      要更自描述,Orchestrator/Service 拿到的是结构化 agent 结果而非原始
      SDK 响应。``WriterOutput`` 之后会按 agent 关心的维度扩展，不污染
      ``LLMResponse``。
    """

    content: str
    model_used: str
    token_usage: TokenUsage
    finish_reason: str | None = None
    # v2 W3.5-s7:本输出是否被 word_count_trim 改写过(整段砍水 + 衔接改顺,必然
    # 重写句长结构)。orchestrator 据此让 style_drift 句均维度降 warn-only,避免
    # trim 把 deepseek 长句正常化被误判风格漂移(详 check_style_drift §2.47)。
    # 默认 False = 未 trim / 开关关(逐字节等价旧行为);**不落盘**(agent →
    # orchestrator 内存透传,ChapterVersion 落盘 model 不含本字段)。
    word_count_trimmed: bool = False
    # 主动 trim 即使没有收敛也可能已经发起 1—2 次物理请求。两个字段只在单次
    # orchestration 内存中透传，避免 validator 随后报同一 above-max 时又盲发
    # 第三、第四刀；不进入 ChapterVersion / API / DB / checkpoint JSON。
    word_count_trim_attempted: bool = Field(default=False, exclude=True)
    word_count_trim_attempts: int = Field(default=0, ge=0, exclude=True)
    # R9 executable constraint plan 仅在单次 orchestrator 内存中随正文版本透传；
    # 不进入 ChapterVersion / API / DB，避免为内部合同新增产品 schema。
    executable_constraint_plan: ExecutableConstraintPlan | None = Field(
        default=None, exclude=True
    )
    # 与计划相同，仅在本次 orchestration 内存中透传。localized 模式已经把
    # Provider patch 机械合成为完整正文；下游用 mode 决定是否允许无边界 trim。
    revision_contract: ProseRevisionContract | None = Field(default=None, exclude=True)


class RewriteConstraintGuardrail(BaseModel):
    """rewrite 保序账本。

    Orchestrator 从上一轮最终 review + 章节卡中构造本对象,Writer 只负责把它
    渲染进 rewrite prompt。它不重新判断约束是否真实通过,避免在 B 任务引入新
    LLM 成本或第二套裁决。
    """

    must_fix: list[ConstraintResult] = Field(default_factory=list)
    must_preserve: list[ConstraintResult] = Field(default_factory=list)
    constraint_violations: list[ConstraintViolation] = Field(default_factory=list)
    other_failures: list[str] = Field(default_factory=list)


def _resolve_style_writer_temperature(
    workspace_paths: WorkspacePaths | None,
) -> float | None:
    """据工作区所选风格模板解析 Writer 采样温度 override(v1.1 I2.1 / T-A)。

    读 ``workspace_paths`` 的 ``meta.json`` → ``Workspace.style_template`` →
    ``STYLE_GENERATION_DEFAULTS[name].writer_temperature``。**仅** Writer 用此值
    (reviewer / validator / chapter_card_generator 都不经过 ``write_chapter``,
    按构造不受影响 → 保护 G8 reviewer 评分稳定性)。

    任一缺失(无 workspace_paths / meta 缺失 / 未选模板 / 读失败)→ 返 ``None``,
    Writer 退回 profile / legacy 温度,与既有路径零回归。读失败静默吞(对齐 I1
    style_guide 读失败 fallback 同型容错,绝不因风格温度阻断正文生成)。
    """
    if workspace_paths is None:
        return None
    try:
        if not workspace_paths.meta_json.is_file():
            return None
        workspace = read_meta(workspace_paths.meta_json)
    except Exception:  # noqa: BLE001 — 容错兜底,不阻断正文生成
        return None
    style_name = workspace.style_template
    if style_name is None:
        return None
    style_gen = STYLE_GENERATION_DEFAULTS.get(style_name)
    return style_gen.writer_temperature if style_gen is not None else None


def write_chapter(
    *,
    chapter_card: ChapterCard,
    memory_section: str,
    setting_contract: SettingContract | None = None,
    router: ModelRouter | None = None,
    client: LLMClient | None = None,
    model: str | None = None,
    max_tokens: int = _DEFAULT_MAX_TOKENS,
    temperature: float = _DEFAULT_TEMPERATURE,
    prompts_dir: Path | None = None,
    vector_store: VectorStore | None = None,
    db_session: Session | None = None,
    workspace_paths: WorkspacePaths | None = None,
    retriever_top_k: int = _DEFAULT_RETRIEVER_TOP_K,
    workspace_id: str | None = None,
    prev_chapter_voice_summary: PovSummary | None = None,
    creation_type: Literal["draft", "rewrite", "revise"] | None = None,
    prev_version_fingerprint: dict[str, float] | None = None,
    validator_failures: list[dict[str, Any]] | None = None,
    rewrite_constraint_guardrail: RewriteConstraintGuardrail | None = None,
    prev_chapter_baseline_fingerprint: dict[str, float] | None = None,
    warning_watchlist_section: str = "",
    progression_consistency_enabled: bool = False,
    progression_label: str = "境界",
    executable_constraint_plan: ExecutableConstraintPlan | None = None,
    core_state_projection: CoreStateProjection | None = None,
    revision_contract: ProseRevisionContract | None = None,
    continuity_capsule: ProvenanceBoundContinuityCapsule | None = None,
    draft_include_audit_ids: bool = True,
) -> WriterOutput:
    """生成单章正文。

    :param chapter_card: 章节卡;``continuity_context`` 字段必须已被
        ChapterService 拼好(包括从上一章 final.md 抽出的 ``prev_chapter_tail``
        与人工/章节卡填的 pov / time_gap / scene_continuation)。
    :param memory_section: 文件式记忆段落,来自
        ``src.core.memory.file_memory.build_memory_section``;允许 ``""``
        (无背景信息)。
    :param setting_contract: 来自 ``data/outline/world.json`` 的权威世界契约；
        产品大纲路径必须传入，且优先级高于 legacy ``memory_section``。
    :param router: P3-B-06 主路径。``ModelRouter`` 实例;给定时按
        ``router.route("writer", chapter_card=card)`` 取 client + model +
        生成参数(profile 来源)。
    :param client: P1 起 legacy 路径 LLM 客户端(``LLMClient`` 协议)。
        ``router`` 给定时此参数被忽略;``router=None`` 时必传。
    :param model: legacy 路径模型名。``router`` 给定时被忽略;``router=None``
        时必传。
    :param max_tokens: legacy 路径输出 token 上限,默认 8000。``router`` 路径
        下由 profile.generation_params 决定,本参数被忽略。
    :param temperature: legacy 路径采样温度,默认 0.85。``router`` 路径下由
        profile 决定,本参数被忽略。
    :param prompts_dir: 提示词模板根目录;``None`` 时取 ``settings.prompts_dir``。
        测试可传临时目录注入 fake 模板。
    :param vector_store: P2-B-10 注入的向量存储。``None`` 时退化为 P1 路径
        (不附检索段);非 ``None`` 时调 retriever 拿 fact + character 两条
        query 的 top_K 条目,拼到 user message 的"## 检索记忆"段。
    :param db_session: P2-B-10 注入的 SQLAlchemy ``Session``,**仅读**用于
        查 ``card.related_characters`` 各角色的 ``voice_profile``。``None``
        时退化为 P1 路径(不附 voice 段);非 ``None`` 时拼到 system message
        末尾的"## 出场角色声音档案"段。
    :param retriever_top_k: P2-B-10 检索每条 where 各拿多少条,默认 10。
    :param workspace_id: P7-B-05 加,透传给 ``retrieve_memories_for_chapter``
        让其在内部解析 workspace 级 retrieval 配置(``retriever_top_k`` 仍是
        Writer 显式 default 10,优先级最高 — 透传到 retriever 的 ``top_k`` 形参;
        ``workspace_id`` 仅当未来 caller 改成 ``top_k=None`` 时生效)。本任务保持
        Writer ``retriever_top_k=10`` 字面值不动(对齐启动指令字面"无新业务逻辑"),
        但端到端透传链路就位,后续 v1.1 想让 Writer 也走 workspace 覆盖时
        只需把 ``retriever_top_k`` 默认值改成 ``None``。
    :param prev_chapter_voice_summary: v1.1 wave 1 session 6 / G10 引入 —
        前一章 final approved 版本的风格基线(主导人称 + 段落均值 + 句长均值
        + 比喻密度)。Orchestrator 在写非首章时透传(由
        ``ChapterOrchestrator`` 从 prev chapter ``style_fingerprint`` 算
        ``summarize_voice_from_fingerprint`` 得);首章为 ``None``(无 baseline,
        prompt 不附该段)。LLM 看到该段后应贴合前一章节奏,避免跨章节人称
        漂移(luo-clean-v1 ch-4~7 现场)。**v1.1 wave 1 session 7 顺手补**:
        本入参的实际 caller 透传已在 ``_call_writer`` 闭包中接通(G10 session
        漏接 → G11 同 _call_writer 改动顺手补)。
    :param creation_type: v1.1 wave 1 session 7 / G11 引入 — 区分本次调用的
        语义 ``"draft"`` / ``"rewrite"`` / ``"revise"``。``"rewrite"`` 时
        ``_build_user_message`` 会附"## rewrite mode 字数收敛指导(SAME
        CHAPTER V1 BASELINE)"段(配 ``prev_version_fingerprint`` +
        ``validator_failures`` 共同生效);其它值或 ``None`` 时该段不附。
        Orchestrator rewrite 路径透传 ``"rewrite"``;draft 路径透传 ``"draft"``
        或 ``None``;revise 路径走 ``revise_chapter`` 不进 Writer。
    :param prev_version_fingerprint: v1.1 wave 1 session 7 / G11 引入 —
        本章 v1(首版)的 ``style_fingerprint`` dict(17 字段,
        ``compute_style_fingerprint`` 产出)。仅在 ``creation_type='rewrite'``
        时使用,被 ``_format_rewrite_baseline`` 抽 4 维数字(段落均值 / 句长
        均值 / 比喻密度 / 主导人称)+ 描述渲染到 user message。``None`` 时
        rewrite 段退化(只附 validator failures + 定向指导但无 v1 数字锚)。
        Orchestrator 从 ``versions[0].style_fingerprint`` 透传(rewrite 路径下
        v1 必在 versions[0])。
    :param validator_failures: v1.1 wave 1 session 7 / G11 引入 — 来自
        ``ReviewResult.validator_failure_details`` 字段(由
        ``_apply_validator_pipeline`` 写入);Orchestrator rewrite 路径透传
        last review 的 ``validator_failure_details``,让 LLM 看到具体哪条
        validator 失败(典型:``word_count`` actual vs min/max + reason)
        以便按失败类型选择压缩、扩写或非字数定向修复。``None`` / 空 → rewrite
        段只附 v1 风格指纹 + 中性定向指导,不附 validator 失败明细。
    :param rewrite_constraint_guardrail: W3.5-B 引入 — Orchestrator 从上一轮
        最终 review 构造的 hard gate 保序账本。``creation_type='rewrite'`` 时
        渲染进 rewrite mode 段:当前失败项必须修;上一轮已通过的 must /
        forbidden / continuity / setting 相关硬约束不得回退。``None`` 时
        保持旧 rewrite prompt 语义。
    :return: ``WriterOutput``,包含正文、实际响应模型名、token 使用量与
        结束原因。
    :raises TypeError: ``router=None`` 且 ``client`` / ``model`` 任一缺失。
    """
    resolved_client, resolved_model, complete_kwargs = resolve_routing(
        agent_name="writer",
        router=router,
        client=client,
        model=model,
        legacy_kwargs={"max_tokens": max_tokens, "temperature": temperature},
        chapter_card=chapter_card,
    )

    # v1.1 wave 4 session 3 / I2.1(T-A):router 路径下,据工作区风格模板覆盖
    # 写作温度(搞笑灵活高温 / 宏大稳重低温)。仅 router 路径生效——legacy 路径
    # (测试 / CLI)用 caller 显式 temperature,不扰动确定性;reviewer / validator
    # 不经过 write_chapter,G8 评分稳定性零影响。
    if router is not None and not getattr(resolved_client, "prose_generation_is_locked", False):
        style_temperature = _resolve_style_writer_temperature(workspace_paths)
        if style_temperature is not None:
            complete_kwargs["temperature"] = style_temperature
            _log.info(
                "writer_style_temperature_override",
                chapter_id=chapter_card.chapter_id,
                workspace_id=workspace_id,
                temperature=style_temperature,
            )

    template_root = (
        prompts_dir if prompts_dir is not None else settings.prompts_dir
    ).resolve()
    draft_v3 = (
        creation_type in {None, "draft"}
        and revision_contract is None
        and uses_hy3_draft_v3(resolved_client)
    )
    causal_handoff = draft_v3 and uses_hy3_draft_v5(resolved_client)
    narrative_handoff = chapter_card.narrative_context is not None
    if not draft_include_audit_ids and not causal_handoff:
        raise WriterDraftAuthorityError("自然事实展示只支持 v5 首稿，不能改变旧版或修订合同")
    system_text = load_prompt_template(
        template_root / (
            "writer_narrative_v7.md" if chapter_card.narrative_context is not None and chapter_card.narrative_context.story_intent.version >= 6 else
            "writer_narrative_v6.md" if narrative_handoff and chapter_card.word_count_policy_version else
            "writer_narrative_v1.md" if narrative_handoff else
            "writer_length_v1.md" if chapter_card.word_count_policy_version and not draft_v3 else
            "writer_draft_hy3_v5.md"
            if causal_handoff
            else
            "writer_draft_hy3_v4.md"
            if draft_v3 and uses_hy3_draft_v4(resolved_client)
            else "writer_draft_hy3_v3.md" if draft_v3 else _WRITER_PROMPT_FILENAME
        )
    )

    prompt_character_ids = _ordered_prompt_character_ids(chapter_card)
    base_system_text = system_text

    # P2-B-10:db_session 非空时附 voice_profile 段到 system message 末尾。
    voice_section = _build_voice_section(
        db_session=db_session, related_characters=prompt_character_ids
    )
    if voice_section and not narrative_handoff:
        system_text = f"{system_text}\n\n{voice_section}"

    # v2 w2-s5:出场角色性格底色段(含主角缺陷底色)。治 wiring 缺口——主角缺陷
    # (characters.personality)此前只在有 arc_beats 的章经 arc_beats 间接到 writer,
    # 无 arc_beats 的普通章主角缺陷底色断在 db→writer 链路(writer 只注 voice/state/
    # identity/arc_beats,无性格段)。本段每章注入,让 w2-s5 SHIP 的"有缺陷的人"在普通章
    # 也立得住(配套 characters.md A5 扩主角缺陷)。db_session=None 时空串(零回归)。
    personality_section = _build_personality_section(
        db_session=db_session,
        related_characters=prompt_character_ids,
        chapter_card=chapter_card,
    )
    if personality_section and not narrative_handoff:
        system_text = f"{system_text}\n\n{personality_section}"
    if not narrative_handoff and getattr(resolved_client, "handoff_format", None) is not None and (voice_section or personality_section):
        # 人物材料原来在 system；保持角色/优先级和既有筛选，不搬到 user 或暴露 secrets。
        system_text = base_system_text + "\n\n" + handoff_user_text(
            resolved_client, stage="writer_character_material",
            data={"voice_material": voice_section, "personality_material": personality_section},
            original_task="\n\n".join(s for s in (voice_section, personality_section) if s),
        )

    # P2-B-10:vector_store 非空时调 retriever 拿 fact + character 两条查询,
    # 拼装"## 检索记忆"段附到 user message。
    # P7-B-05:透传 workspace_id,让 retriever 在 ``top_k=None`` 时按工作区配置
    # 解析(目前 Writer 显式 ``retriever_top_k=10`` 覆盖,workspace_id 暂未生效;
    # v1.1 可改默认 None 接入)。
    retrieved_section = _build_retrieved_memory_section(
        vector_store=vector_store,
        chapter_card=chapter_card,
        top_k=retriever_top_k,
        workspace_id=workspace_id,
    )

    # G1(v1.1 wave 4 session 1):角色截止状态段(SQL lookup state_timeline,绕开
    # embedding),防"断臂复活"。db_session=None / 首章 / 无历史状态时为空串。
    character_state_section = _build_character_state_section(
        db_session=db_session, chapter_card=chapter_card
    )

    # 模块②(v1.1 wave 5 s10):角色进阶等级段(境界一致性,SQL lookup progression_rank,
    # 绕开 embedding)。gated:非 3 题材 / 工作区开关 OFF → enabled=False → 空串(零回归)。
    progression_section = _build_progression_section(
        db_session=db_session,
        chapter_card=chapter_card,
        enabled=progression_consistency_enabled,
        label=progression_label,
    )

    # A3(v1.1 wave 6 session 2):出场角色身份档案段(SQL lookup identity_facets,
    # 固定参考事实),防双界身份写串 / 漏写。无多重身份角色时为空串。
    identity_section = _build_identity_section(
        db_session=db_session, chapter_card=chapter_card
    )

    # W3.5:writer 关系注入最小闭环。复用 outline 关系 hardlock 渲染,只注入
    # POV/related 相关关系,防"上游知道关系、正文写偏"。
    relationship_lock_section = _build_relationship_lock_section(
        db_session=db_session, chapter_card=chapter_card
    )

    # R9 两层伏笔：章节卡已选中的伏笔按 ID 精确读 SQLite。大纲主伏笔继续按
    # 原计划推进；临时伏笔只有章节卡显式写了收口 marker 才在本章兑现。该段与
    # callback_to 的“回响原文”职责分开，不新增向量/Agent/Provider 请求。
    related_plot_thread_section = _build_related_plot_thread_section(
        db_session=db_session,
        chapter_card=chapter_card,
    )

    # G2(v1.1 wave 4 session 2):伏笔 / 隐喻回响段(callback_to 非空时按 thread_id
    # **SQL 精确 lookup PlotThread + 读 planted_chapter 原文片段**,绕开 embedding),
    # 根治"第 3 章暴雨被第 45 章洗澡向量检索覆盖"。callback_to 空 / db_session=None
    # 时为空串。
    callback_section = _build_callback_section(
        db_session=db_session,
        workspace_paths=workspace_paths,
        chapter_card=chapter_card,
    )

    # I1(v1.1 wave 4 session 3):工作区写作风格指南段(据创建时所选 style_template
    # 生成的 prompts/style_guide.md)。未选模板 / 文件缺失时为空串。
    style_guide_section = _build_style_guide_section(
        workspace_paths=workspace_paths, require_selected_guide=narrative_handoff,
    )

    # M-craft(v1.1 wave 5 session 2):引擎内置通用反套话写作手法纪律段。
    # **无条件每章注入**、book-agnostic(不读工作区配置)。与 I1 style_guide
    # (工作区级风格)互补:craft 给"任何书通用的近普世内核(反复读机 / 关键戏别
    # 偷懒 / 克制补刀);整体 showing 密度归 style_template,不在此"。实证见 w5-s1
    # postmortem §5(writer 级 craft 注入降套话 67~91% + 盲读 5→7);走范式 ②
    # (writer 级注入),不走 M1 卡片注入覆辙。
    craft_section = _build_craft_section()

    stage: ProseConstraintStage = creation_type or "draft"
    if executable_constraint_plan is None:
        plan_characters: list[Character] = []
        if db_session is not None:
            character_ids = dict.fromkeys(
                [chapter_card.pov_character, *chapter_card.related_characters]
            )
            for character_id in character_ids:
                record = get_character(db_session, character_id)
                if record is not None:
                    plan_characters.append(record)
        executable_constraint_plan = compile_executable_constraint_plan(
            chapter_card=chapter_card,
            stage=stage,
            setting_contract=setting_contract,
            character_records=plan_characters,
            expected_voice=(
                prev_chapter_voice_summary.dominant_pov
                if prev_chapter_voice_summary is not None
                and prev_chapter_voice_summary.dominant_pov
                in {"first_person", "third_person"}
                else None
            ),
            rewrite_style=resolve_rewrite_style_constraint_spec(
                v1_fingerprint=prev_version_fingerprint,
                cross_chapter_baseline_fingerprint=(prev_chapter_baseline_fingerprint),
            ),
            core_state_projection=core_state_projection,
        )
    executable_constraint_plan.assert_matches_chapter_card(chapter_card)
    if draft_v3:
        executable_constraint_plan.assert_matches_setting_contract(setting_contract)
    executable_constraint_plan.assert_matches_core_state(
        chapter_card=chapter_card,
        projection=core_state_projection,
    )
    if executable_constraint_plan.stage != stage:
        raise ValueError(
            "ExecutableConstraintPlan stage 与 Writer creation_type 不一致"
        )
    if revision_contract is not None:
        if creation_type != "rewrite":
            raise ValueError("正文 revision contract 只允许用于 Writer rewrite")
        revision_contract.assert_compatible(
            source_content=revision_contract.source_content,
            source_version_id=revision_contract.source_version_id,
            executable_constraint_plan=executable_constraint_plan,
        )

    exact_hy3_draft = (
        revision_contract is None
        and creation_type in {None, "draft"}
        and uses_hy3_creative_delta(resolved_client)
    )
    authority_assembly_v2 = exact_hy3_draft and uses_hy3_authority_assembly(
        resolved_client
    )
    draft_expansion_plan = (
        build_writer_draft_expansion_plan(chapter_card)
        if exact_hy3_draft and not draft_v3 else None
    )
    if narrative_handoff:
        records = [get_character(db_session, cid) for cid in chapter_card.related_characters] if db_session is not None else []
        if any(record is None for record in records):
            raise WriterDraftAuthorityError("新叙事卡缺少人物原档，不能猜人物背景")
        repair: dict[str, JsonValue] | None = None
        if creation_type == "rewrite":
            repair = {
                "failures": [failure for failure in validator_failures or []],
                "passed_constraints": (rewrite_constraint_guardrail.model_dump(mode="json")
                                       if rewrite_constraint_guardrail is not None else None),
                "source_revision": (render_revision_contract(revision_contract)
                                    if revision_contract is not None else None),
                "instructions": "修复本轮失败，保留已通过事实和无关段落；段均句长只作观察。",
            }
        bundle = build_narrative_bundle(
            card=chapter_card, plan=executable_constraint_plan,
            characters=[record for record in records if record is not None],
            setting=setting_contract,
            continuity=_format_continuity(
                chapter_card.continuity_context, continuity_capsule=continuity_capsule,
                frozen_provenance=chapter_card.continuity_ledger_provenance,
                authority_only=True, bridge_scope_limited=True,
            ),
            current_state={
                "character_state": character_state_section,
                "progression": progression_section, "identity": identity_section,
                "core_facts": [{"entity_type": fact.entity_type, "entity_id": fact.entity_id,
                                "attribute": fact.attribute, "value": fact.value}
                               for fact in core_state_projection.facts] if core_state_projection is not None else [],
                "allowed_transitions": [{"entity_type": item.entity_type, "entity_id": item.entity_id,
                                         "attribute": item.attribute, "from_value": item.from_value,
                                         "to_value": item.to_value, "reason": item.reason}
                                        for item in chapter_card.core_state_transitions],
            },
            relationships=relationship_lock_section, memory=memory_section,
            retrieved=retrieved_section, plot_threads=related_plot_thread_section,
            callbacks=callback_section, style_guide=style_guide_section,
            warnings=warning_watchlist_section, repair=repair,
            opening_presentation_version=getattr(resolved_client, "opening_presentation_version", 0),
            is_book_opening=(_is_full_book_opening(chapter_card, db_session)
                             if getattr(resolved_client, "opening_presentation_version", 0) else False),
        )
        task = "读取五组写作材料，在事实边界内写出本章。"
        if getattr(resolved_client, "handoff_format", None) is None:
            user_text = bundle.model_dump_json(indent=2)
        else:
            user_text = handoff_user_text(resolved_client, stage="writer",
                data=bundle.model_dump(mode="json"), original_task=task)
    else:
        user_text = _build_user_message(
            chapter_card=chapter_card,
            memory_section=memory_section,
            setting_contract=setting_contract,
            retrieved_section=retrieved_section,
            relationship_lock_section=relationship_lock_section,
            character_state_section=character_state_section,
            progression_section=progression_section,
            identity_section=identity_section,
            related_plot_thread_section=related_plot_thread_section,
            callback_section=callback_section,
            style_guide_section=style_guide_section,
            craft_section=craft_section,
            prev_chapter_voice_summary=prev_chapter_voice_summary,
            creation_type=creation_type,
            prev_version_fingerprint=prev_version_fingerprint,
            validator_failures=validator_failures,
            rewrite_constraint_guardrail=rewrite_constraint_guardrail,
            prev_chapter_baseline_fingerprint=prev_chapter_baseline_fingerprint,
            warning_watchlist_section=warning_watchlist_section,
            executable_constraint_plan=executable_constraint_plan,
            core_state_projection=core_state_projection,
            revision_contract=revision_contract,
            continuity_capsule=continuity_capsule,
            draft_expansion_plan=draft_expansion_plan,
            authority_assembly_v2=authority_assembly_v2,
            draft_v3=draft_v3,
            scene_handoff=draft_v3 and uses_hy3_draft_v4(resolved_client),
            causal_handoff=causal_handoff,
            draft_include_audit_ids=draft_include_audit_ids,
        )
        user_text = handoff_user_text(
            resolved_client, stage="writer",
            data={
                "chapter_card": chapter_card, "setting_contract": setting_contract,
                "constraint_plan": executable_constraint_plan,
                "core_state_projection": core_state_projection,
                "continuity_capsule": continuity_capsule,
                "creation_type": creation_type or "draft",
                "revision_contract": revision_contract,
                "supporting_material": {
                    "memory": memory_section, "retrieved": retrieved_section,
                    "relationships": relationship_lock_section, "character_state": character_state_section,
                    "progression": progression_section, "identity": identity_section,
                    "plot_threads": related_plot_thread_section, "callbacks": callback_section,
                    "style_guide": style_guide_section, "craft": craft_section,
                    "previous_voice": prev_chapter_voice_summary,
                    "warning_watchlist": warning_watchlist_section,
                },
            }, original_task=user_text,
        )
    if revision_contract is not None and revision_contract.mode == "localized":
        complete_kwargs = prepare_strict_json_output(
            client=resolved_client,
            model=resolved_model,
            output_model=LocalizedRevisionResponse,
            schema_name="localized_revision",
            complete_kwargs=complete_kwargs,
        )

    response = resolved_client.complete(
        messages=[
            Message(role="system", content=system_text),
            Message(role="user", content=user_text),
        ],
        model=resolved_model,
        **complete_kwargs,
    )

    if chapter_card.word_count_policy_version and (
        not response.content.strip() or response.finish_reason in {"length", "max_tokens"}
    ):
        raise WriterDraftIntegrityError(
            reasons=("empty_response" if not response.content.strip() else "provider_truncated",),
            details={"finish_reason": response.finish_reason, "actual": len(response.content)},
        )
    raw_content = (
        validate_writer_draft_integrity(
            content=response.content,
            minimum=chapter_card.word_count_min,
            maximum=chapter_card.word_count_max,
            check_length=not draft_v3 and chapter_card.word_count_policy_version == 0,
        )
        if exact_hy3_draft
        else response.content
    )
    if draft_v3:
        leakage = draft_instruction_leakage(raw_content, executable_constraint_plan)
        if leakage:
            raise WriterDraftIntegrityError(
                reasons=("instruction_leakage",),
                details={"signals": list(leakage), "actual": len(raw_content)},
            )
    content = (
        apply_revision_response(
            contract=revision_contract,
            raw_content=raw_content,
        )
        if revision_contract is not None
        else raw_content
    )

    # P1-B-24:OBSERVABILITY §2.4 `chapter_draft_completed` 事件。``version_id``
    # 由调用方(Orchestrator)知晓但本 agent 不知;P1
    # 简版只落字数 + 模型,version_id 从 Orchestrator 透 ``trace_id`` 间接定位。
    word_count = len(content)
    _log.info(
        "chapter_draft_completed",
        chapter_id=chapter_card.chapter_id,
        word_count=word_count,
        model_used=response.model_used,
        finish_reason=response.finish_reason,
    )

    return WriterOutput(
        content=content,
        model_used=response.model_used,
        token_usage=response.usage,
        finish_reason=response.finish_reason,
        executable_constraint_plan=executable_constraint_plan,
        revision_contract=revision_contract,
    )


def _build_draft_v3_user_message(
    *,
    card: ChapterCard,
    plan: ExecutableConstraintPlan,
    setting: SettingContract | None,
    core_state_projection: CoreStateProjection | None,
    continuity_capsule: ProvenanceBoundContinuityCapsule | None,
    auxiliary_sections: tuple[str, ...],
    scene_handoff: bool = False,
    causal_handoff: bool = False,
    draft_include_audit_ids: bool = True,
) -> str:
    """组织完整首稿任务；旧首稿/修订渲染路径保持隔离。"""
    view = build_writer_draft_view(card, plan)
    progress = (
        f"张力：{card.tension_level}/10；弧段：{card.arc_phase}；"
        f"是否弧线终章：{card.is_arc_finale}；剧情节点预算：{card.plot_node_budget}。\n"
    )
    if causal_handoff:
        phase = {
            "opening": "故事开端",
            "rising": "冲突发展",
            "climax": "冲突高潮",
            "falling": "高潮后的回落",
            "resolution": "结局收束",
        }[card.arc_phase]
        ending = "在此收束" if card.is_arc_finale else "仍将延续"
        progress = (
            f"张力：{card.tension_level}/10；叙事进度：{phase}；当前弧段{ending}；"
            f"剧情节点预算：{card.plot_node_budget}。\n"
            "叙事进度只用于安排写法，不是人物所知或正文用语；在既定章界内把眼前"
            "事件的尝试、反馈与选择演完整，不以说明后续安排代替当前行动。\n"
        )
    sections = [
        "## 当前写作任务\n\n"
        f"标题参考：{card.title}；主视角人物：{card.pov_character}。\n"
        f"场景：{card.setting}\n"
        + progress
        + ("节点预算是剧情推进的软上限，不是动作次数、场次数或段落配额。\n"
           if scene_handoff else "")
        +
        (render_word_count_task(card) if card.word_count_policy_version == 2 else
         f"正文 {card.word_count_min}—{card.word_count_max} 字符；"
         f"起草目标约 {(card.word_count_min + card.word_count_max) // 2} 字符。"),
        _format_continuity(
            card.continuity_context,
            continuity_capsule=continuity_capsule,
            frozen_provenance=card.continuity_ledger_provenance,
            authority_only=True,
            bridge_scope_limited=causal_handoff,
        ),
        view.render(include_audit_ids=draft_include_audit_ids),
        render_dramatic_context(card),
    ]
    if setting is not None and not setting.is_empty:
        # unique_rules 的完整原文已与 HARD ID 在 view 中绑定；普通世界事实保留。
        sections.append(
            "## 世界背景\n\n"
            f"{setting.setting}\n\n{setting.power_system}"
            + (f"\n\n组织与权限背景：{setting.factions}" if setting.factions.strip() else "")
            + (f"\n\n社会与习惯背景：{setting.culture}" if setting.culture.strip() else "")
        )
        if setting.unique_rules.strip() and not plan.by_kind("setting_unique_rules"):
            # 来源未冻结时不能静默丢弃原文，也不能自行补成新的 HARD。
            sections.append(f"## 补充世界说明\n\n{setting.unique_rules}")
    if core_state_projection is not None:
        sections.append(build_writer_authority_sections(
            chapter_card=card, core_state_projection=core_state_projection
        ).core_state)
    sections.extend(auxiliary_sections)
    sections.append(f"## 收束时可观察的线索、压力或决定\n\n{card.next_chapter_seed}")
    return append_role_marker(
        "\n\n".join(section for section in sections if section.strip()), role="writer"
    )


def _build_user_message(
    *,
    chapter_card: ChapterCard,
    memory_section: str,
    setting_contract: SettingContract | None = None,
    retrieved_section: str,
    relationship_lock_section: str = "",
    character_state_section: str = "",
    progression_section: str = "",
    identity_section: str = "",
    related_plot_thread_section: str = "",
    callback_section: str = "",
    style_guide_section: str = "",
    craft_section: str = "",
    prev_chapter_voice_summary: PovSummary | None = None,
    creation_type: Literal["draft", "rewrite", "revise"] | None = None,
    prev_version_fingerprint: dict[str, float] | None = None,
    validator_failures: list[dict[str, Any]] | None = None,
    rewrite_constraint_guardrail: RewriteConstraintGuardrail | None = None,
    prev_chapter_baseline_fingerprint: dict[str, float] | None = None,
    warning_watchlist_section: str = "",
    executable_constraint_plan: ExecutableConstraintPlan | None = None,
    core_state_projection: CoreStateProjection | None = None,
    revision_contract: ProseRevisionContract | None = None,
    continuity_capsule: ProvenanceBoundContinuityCapsule | None = None,
    draft_expansion_plan: WriterDraftExpansionPlan | None = None,
    authority_assembly_v2: bool = False,
    draft_v3: bool = False,
    scene_handoff: bool = False,
    causal_handoff: bool = False,
    draft_include_audit_ids: bool = True,
) -> str:
    """把章节卡 + 记忆段落 + 衔接桥 + 检索段拼成一条 markdown 风格的 user 消息。

    结构(自顶向下):当前章任务 → 权威世界/连续性/核心状态/本轮 HARD → 唯一文学
    执行块与本章文学焦点 → 身份/关系/状态/伏笔事实 → 全书风格与跨章 WARN →
    rewrite 合同 → 辅助记忆 → 章末钩子。优先级靠明确合同决定，顺序只负责降低
    注意力竞争；辅助层永远不能覆盖前面的权威层。
    """
    if draft_v3:
        if executable_constraint_plan is None or creation_type not in {None, "draft"}:
            raise WriterDraftAuthorityError("v3 首稿缺少匹配的冻结计划")
        return _build_draft_v3_user_message(
            card=chapter_card,
            plan=executable_constraint_plan,
            setting=setting_contract,
            core_state_projection=core_state_projection,
            continuity_capsule=continuity_capsule,
            scene_handoff=scene_handoff,
            causal_handoff=causal_handoff,
            draft_include_audit_ids=draft_include_audit_ids,
            auxiliary_sections=(
                craft_section,
                *((_format_scene_handoff_focus(chapter_card),) if scene_handoff else (
                    _format_chapter_focus(chapter_card),
                    _format_interaction_focus(chapter_card, authority_only=True),
                    _format_breakthrough_payoff(chapter_card),
                    _format_craft_focus(chapter_card),
                )),
                identity_section,
                relationship_lock_section, character_state_section,
                progression_section, related_plot_thread_section, callback_section,
                style_guide_section,
                _format_voice_baseline(prev_chapter_voice_summary)
                if prev_chapter_voice_summary is not None else "",
                warning_watchlist_section,
                f"## 辅助背景\n\n{memory_section}" if memory_section.strip() else "",
                retrieved_section,
            ),
        )
    if authority_assembly_v2 and draft_expansion_plan is None:
        raise WriterDraftAuthorityError(
            "exact Hy3 Writer authority assembly v2 缺少展开计划"
        )
    dramatic_authority_refs: dict[str, str] | None = None
    if (
        authority_assembly_v2
        and draft_expansion_plan is not None
        and chapter_card.dramatic_source is not None
    ):
        beat = chapter_card.dramatic_source.beat_contract
        dramatic_authority_refs = {
            text: _draft_expansion_ref(
                plan=draft_expansion_plan,
                text=text,
            )
            for text in beat.natural_requirements()
        }
    authority = build_writer_authority_sections(
        chapter_card=chapter_card,
        core_state_projection=core_state_projection,
        dramatic_authority_refs=dramatic_authority_refs,
    )
    sections: list[str] = []
    sections.append(_format_card_core(chapter_card))
    if authority.dramatic_source:
        sections.append(authority.dramatic_source)
    if authority_assembly_v2 and draft_expansion_plan is not None:
        sections.append(_render_writer_draft_expansion_plan_v2(draft_expansion_plan))
    sections.append(
        _format_constraint_lists(
            chapter_card,
            draft_expansion_plan=(
                draft_expansion_plan if authority_assembly_v2 else None
            ),
        )
    )

    if setting_contract is not None and not setting_contract.is_empty:
        setting_section = setting_contract.render_for_prompt()
        if (
            authority_assembly_v2
            and draft_expansion_plan is not None
            and setting_contract.unique_rules.strip()
        ):
            setting_section = setting_section.replace(
                "- unique_rules(HARD；只应包含真正不可破例的世界规则):",
                "- [setting-contract-unique-rules] unique_rules"
                "(HARD；只应包含真正不可破例的世界规则):",
                1,
            )
        sections.append(setting_section)

    sections.append(
        _format_continuity(
            chapter_card.continuity_context,
            continuity_capsule=continuity_capsule,
            frozen_provenance=chapter_card.continuity_ledger_provenance,
            authority_only=authority_assembly_v2,
        )
    )
    if authority.core_state:
        sections.append(authority.core_state)
    sections.append(
        _format_writer_hard_gate_contract(
            chapter_card,
            creation_type=creation_type,
            executable_constraint_plan=executable_constraint_plan,
            core_state_projection=core_state_projection,
            draft_expansion_plan=(
                draft_expansion_plan if authority_assembly_v2 else None
            ),
        )
    )
    if not authority_assembly_v2 and draft_expansion_plan is not None:
        sections.append(render_writer_draft_expansion_plan(draft_expansion_plan))

    # Session D:先完整展示本章任务与真实 HARD 通过标准，再紧接唯一、短小的文学
    # 执行块。这样文学目标既不会抢在 HARD 之前，也不会被后续事实胶囊/内部说明淹没。
    # 以下内容都是 SOFT 写法指导，不改变任何约束身份。
    if craft_section.strip():
        sections.append(craft_section)
    for literary_section in (
        _format_chapter_focus(chapter_card),
        _format_arc_beats(
            chapter_card,
            draft_expansion_plan=(
                draft_expansion_plan if authority_assembly_v2 else None
            ),
        ),
        _format_interaction_focus(
            chapter_card,
            authority_only=authority_assembly_v2,
        ),
        _format_breakthrough_payoff(chapter_card),
        _format_craft_focus(chapter_card),
    ):
        if literary_section:
            sections.append(literary_section)

    # A3:出场角色身份档案段(SQL identity_facets,固定 who's who 事实锚,防双界
    # 身份写串 / 漏写)。置于 G1 状态段之前 — "角色是谁"比"角色当前状态"更基础;
    # 无多重身份角色时为空串。全程注入(身份是事实硬约束,与风格收敛目标正交)。
    if identity_section.strip():
        sections.append(identity_section)

    # W3.5:角色关系硬锁段。置于身份档案之后、角色截止状态之前 —— 先钉"谁与谁
    # 是什么关系",再钉"关系/身体/物品截止上一章处于什么状态"。
    if relationship_lock_section.strip():
        sections.append(relationship_lock_section)

    # G1:角色截止状态段(SQL state_timeline,防断臂复活)。draft / rewrite /
    # revise 全程注入 — 状态连贯是事实硬约束,与 G11 风格收敛目标正交。
    if character_state_section.strip():
        sections.append(character_state_section)

    # 模块②(v1.1 wave 5 s10):角色进阶等级段(境界一致性,防莫名倒退/写串)。紧随 G1
    # 状态段 — 同属"角色截止上一章的事实硬约束",与之并列(各管不同维度)。gated 空串
    # 时跳过(非 3 题材 / 开关 OFF / 首章无进阶 → 行为与 s10 前逐字节等价)。也是模块①
    # 升级可感化的"起点境界"来源。
    if progression_section.strip():
        sections.append(progression_section)

    if related_plot_thread_section.strip():
        sections.append(related_plot_thread_section)

    # G2:伏笔 / 隐喻回响段(SQL 精确 lookup,绕 embedding)。同 G1 全程注入 —
    # 显式 ID 回响是作者意图硬约束,与 G11 风格收敛目标正交。
    if callback_section.strip():
        sections.append(callback_section)

    # I1:工作区级风格锚保留，但不重复上方本章文学执行规则。
    if style_guide_section.strip():
        sections.append(style_guide_section)

    if prev_chapter_voice_summary is not None:
        sections.append(_format_voice_baseline(prev_chapter_voice_summary))

    if warning_watchlist_section.strip():
        sections.append(warning_watchlist_section)

    if creation_type == "rewrite":
        sections.append(
            _format_rewrite_baseline(
                prev_version_fingerprint=prev_version_fingerprint,
                validator_failures=validator_failures,
                rewrite_constraint_guardrail=rewrite_constraint_guardrail,
                prev_chapter_baseline_fingerprint=prev_chapter_baseline_fingerprint,
                word_count_policy_version=chapter_card.word_count_policy_version,
            )
        )
        if revision_contract is not None:
            sections.append(render_revision_contract(revision_contract))

    if memory_section.strip():
        heading = (
            "## 背景信息（辅助；不得覆盖上方权威世界契约）"
            if setting_contract is not None and not setting_contract.is_empty
            else "## 背景信息"
        )
        sections.append(f"{heading}\n\n{memory_section}")

    if retrieved_section.strip():
        sections.append(retrieved_section)

    sections.append(
        _format_chapter_boundary(
            chapter_card,
            authority_only=authority_assembly_v2,
        )
    )

    # P1-B-22:追加 agent 身份 marker(HTML 注释,真 LLM 视为字面忽略)。
    return append_role_marker("\n\n".join(sections), role="writer")


#: v1.1 audit G17 治本:v1 paragraph_mean 偏前章 baseline 超过该阈值视作
#: “v1 保序锚不可靠”。R9 起对应维度在 rewrite prompt / validator 同步降为
#: WARN-only，前章数值只作软参考，不再替换成新 HARD 锚。
#:
#: 阈值 50% 对齐 G5 同章 style_drift_validator PARAGRAPH_LENGTH_MEAN_DRIFT_THRESHOLD
#: (G15 后由 30%→50%),代表"真严重漂移"。luo-clean-v1 ch-5 rerun-4 v1 paraMean=13.5
#: vs ch-4 baseline=53.13,偏移 75%,远超 50% 阈值 → 触发异常维降级。
#:
#: 不用 30% 是因为:30% 偏移可能是剧情自然差异(场景 / tension / 角色密度切换),
#: 过早降级会漏掉正常保序;50% 以上才把该 v1 维度视作不可靠。
#:
#: **SSOT 上移(2026-06-10 v1.1 acceptance 修复1)**:常量本体迁至
#: ``style_drift_validator.V1_BASELINE_ANOMALY_THRESHOLD`` — G17 当年只接了
#: writer 锚定一半,style_drift 闸门仍无条件锚 v1,两端不一致酿成 luo-accept-v1
#: ch-0007 死锁(writer 教回归前章 72 字/段、闸门按 v1=165 罚漂移)。此处保留
#: 私名别名供既有引用 / 测试,**两端判定条件必须永远共用同一常量**。
_V1_BASELINE_ANOMALY_THRESHOLD: float = V1_BASELINE_ANOMALY_THRESHOLD


def _word_count_failure_reason(
    validator_failures: list[dict[str, Any]] | None,
) -> str | None:
    """W3.5-s3-revisit(RC2):从 ``validator_failures`` 抽 word_count 越界方向。

    返回 ``"above_max"``(写超上限)/ ``"below_min"``(写不足下限)/ ``None``
    (无 word_count 失败 / reviewer 自然 verdict=rewrite 不带 validator_failures)。
    供 ``_format_rewrite_baseline`` 的收敛指导按方向分支:above_max → **压缩**到
    上限以内;below_min → **扩容**到下限;None → 只修非字数失败或 Reviewer
    反馈,不得凭空扩写。

    **为何(s6 端到端实测根因)**:原 rewrite 注入段单向"扩内容达字数下限"(G11
    为 qwen 偏短调),deepseek overshoot 撞 rewrite 被扩容框架推得越写越长
    (ch-0001 rewrite 4113→4837→5517 反常点);writer.md v12 静态 prompt 已加
    above_max 对称分支,但本动态注入段当时没跟上 → 半接线。
    """
    for failure in validator_failures or []:
        if failure.get("name") == "word_count":
            reason = (failure.get("details") or {}).get("reason")
            if reason in {"above_max", "below_min"}:
                return reason
    return None


def _format_rewrite_baseline(
    *,
    prev_version_fingerprint: dict[str, float] | None,
    validator_failures: list[dict[str, Any]] | None,
    rewrite_constraint_guardrail: RewriteConstraintGuardrail | None = None,
    prev_chapter_baseline_fingerprint: dict[str, float] | None = None,
    word_count_policy_version: int = 0,
) -> str:
    """v1.1 wave 1 session 7 / G11:渲染本章 v1 风格基线 + 待修版本失败明细。
    v1.1 audit G17 治本(2026-05-27):加 v1 异常检测；R9 把旧“前章 HARD
    fallback”修正为“异常维 WARN-only”。

    在 rewrite 路径(``creation_type='rewrite'``)由 ``_build_user_message``
    调用,目的让 LLM 在重写时:

    - 看 v1 fingerprint 4 维数字 + 描述(段落均值 / 句长均值 / 比喻密度 /
      主导人称)→ **保持节奏密度**(避免 luo-clean-v1 ch-7 v1→v4 段落变长 +
      比喻密度大幅波动现象)
    - 看 validator 失败明细 → 按 ``above_max`` / ``below_min`` / 非字数失败
      三路定向修复,不把任意失败都误当成欠长
    - 按 prompt v16"## rewrite mode 字数收敛指导"段落的定向策略行动；真正
      失败项优先，健康 v1 维度只承担 rewrite 保序，不能把跨章 WARN 偷换成
      新的同章 HARD(R9 ch-0002 现场)

    **G17 异常维度降级路径**:当 v1 段均 / 句均命中共享异常明细时:

    - v1 已被 mode collapsed,锚定 v1 fingerprint 会让 LLM 越改越偏
      (luo-clean-v1 ch-5 rerun-4 现场:v1 paraMean=13.5 → v2~v4 paraMean
      振荡 10.4 / 393 / 9.2 不收敛);
    - 真正触发的维度不再作为本轮同章保序 HARD；前章值只作 WARN/软参考，
      不得因为进入 rewrite 就升级成硬闸;
    - 未触发的健康维度仍以本章 v1 为保序 HARD，POV 仍取本章 v1，比喻密度
      继续 WARN-only；Writer 与 validator 共用同一异常明细。

    **fingerprint=None 路径**:rewrite 段仍附,只是没有 4 维数字锚(prompt
    v15 段落仍会按失败类型给出定向修复,但不知道具体保持哪个节奏值)。

    **fingerprint+failures 都 None 的情况**:理论上 rewrite 路径不应该
    出现(rewrite 必然由 reviewer/validator 触发,validator_failures 至少
    有一条;但 reviewer 自然 verdict=rewrite 不附 validator_failures 的
    路径下,失败明细段渲染"无")。本函数仍输出 rewrite 段标题 + 中性定向
    指导,不凭空假定 below_min。
    """
    # ===== R9:G17 因果路由(段均或句均任一异常)=====
    # 调 style_drift_validator 的共享明细函数一次，得到不可靠的 v1 保序维度。
    # 这些维度只降 WARN，不换成前章 HARD；否则会把初稿跨章 WARN 偷换成 rewrite
    # 新硬闸。提示文字不得另按段均重算，否则 Writer 与 validator 又会两把尺。
    baseline_anomalies = (
        v1_baseline_anomaly_details(
            prev_version_fingerprint, prev_chapter_baseline_fingerprint
        )
        if (
            prev_version_fingerprint is not None
            and prev_chapter_baseline_fingerprint is not None
        )
        else ()
    )
    anomalous_dimensions = {item.dimension for item in baseline_anomalies}

    # W3.5-s3-revisit(RC2):word_count 越界方向 → 收敛指导按方向分支(above_max
    # 压缩 / below_min 扩容)。R8 ch-0003 进一步证明 style_drift 等非字数失败
    # 不能落入扩容分支:v2 已在合法字数带内,却被旧提示推成超长 v3。
    word_count_reason = (_word_count_failure_reason(validator_failures)
                         if word_count_policy_version == 0 else None)
    is_above_max = word_count_reason == "above_max"
    is_below_min = word_count_reason == "below_min"

    lines = ["## 定向修复与文风保留\n" if word_count_policy_version else
             "## rewrite mode 字数收敛指导(SAME CHAPTER V1 BASELINE)\n"]

    # v1.1 audit G26 治本(2026-05-27 b1-rerun-deep-audit + C 路径对照实验):
    # 在 rewrite mode 段最顶部加显眼 pov 硬约束,**让 LLM 第一眼就看到**(避免
    # 被下方 v1 fingerprint 数字 / validator 失败明细 / 扩展指导分散注意力)。
    # luo-clean-v1 ch-5 rerun-5 v4 实证现场:LLM 在 rewrite 压力下把 third_person
    # 切到 first_person 触发 G10 dominant_pov_flipped → rewrite 死循环。
    if prev_version_fingerprint is not None:
        v1_dominant_pov = _infer_dominant_pov_label(prev_version_fingerprint)
        lines.append(
            f"### ⚠️ rewrite 硬约束 — 主导人称保持(G26)\n\n"
            f"本章 v1 已确立的主导人称是 **{v1_dominant_pov}**,**rewrite 必须严格保持,"
            f"不切换人称**。改写或增减内容时,正文的主语 / 自称必须与 v1 一致。\n"
        )

    if baseline_anomalies:
        lines.append("### ⚠️ v1 风格指纹异常维度(G17/R9 因果路由)\n\n")
        lines.append(
            "下列维度不能充当本轮 rewrite 的可靠保序硬尺；未列出的健康维度"
            "仍按本章 v1 保序：\n"
        )
        for anomaly in baseline_anomalies:
            if anomaly.reason == "paragraph_collapse":
                reason_text = (
                    f"向下偏移 {abs(anomaly.relative_delta) * 100:.0f}% 且 v1 段均低于 "
                    f"{PARAGRAPH_MEAN_HARD_MIN:g} 字，v1 已命中 paragraph collapse "
                    "初稿硬闸"
                )
            else:
                reason_text = (
                    f"向上偏移 {anomaly.relative_delta * 100:.0f}%，超过异常观察阈值"
                )
            lines.append(
                f"- `{anomaly.dimension}`：v1 **{anomaly.v1_value:.1f}**，"
                f"前章 baseline **{anomaly.baseline_value:.1f}**；{reason_text}\n"
            )
        lines.append(
            "**这些维度本轮降为 WARN-only，前章值仅作软参考，绝不升级为 HARD。**"
            "先修下面列出的真实失败项；主导人称及未列出的健康维度仍须保序。\n"
        )

    if is_above_max:
        lines.append(
            "上一轮待修正文因 [HARD] word_count **越上限(above_max,当前版本超长)**"
            "触发 rewrite。以下是本章 v1 风格锚与待修版本失败明细。**核心目标:把正文"
            "收敛到 `word_count_max` 以内,同时保持 v1 的节奏密度与 show 不变**——砍冗余"
            " tell / 水 show,**绝不砍精彩 show / 核心剧情 / must_achieve**(详见 prompt "
            "'rewrite mode 字数收敛指导' 段 above_max 分支)。\n"
        )
    elif is_below_min:
        lines.append(
            "上一轮待修正文因 [HARD] word_count **低于下限(below_min,当前版本欠长)**"
            "触发 rewrite。以下是本章 v1 风格锚与待修版本失败明细。**核心目标:"
            "扩内容达字数下限的同时,"
            "保持 v1 的节奏密度不变**(详见 prompt 'rewrite mode 字数收敛指导' 段)。\n"
        )
    else:
        lines.append(
            "上一轮待修正文因非字数 [HARD] validator 或 Reviewer 反馈触发 rewrite。"
            "以下是本章 v1 风格锚与待修版本失败明细。**核心目标:只修复列出的失败项,并把正文"
            "保持在章节卡字数区间内;没有 `below_min` 就不得为了凑长度新增场景、对白、"
            "心理或环境描写**。若失败明细为空,按本消息中的审稿与 hard gate 保序信息"
            "定向改写,目标字数取区间中线偏下。\n"
        )

    lines.append("### v1 风格指纹(rewrite 保序，不改写真实失败原因)\n")
    lines.append(
        "**未被上方 G17 异常段列出的段落均值 / 句长均值，是本轮 rewrite 的"
        "同章保序 [HARD] 尺；被列出的维度仅 WARN，不得拿前章值另造硬闸。**"
        "段落均值正常保序阈值 ±50%，句长均值 ±30%；主导人称必须保持本章 v1。"
        "比喻密度仅作软性风格参考。真实打回原因仍以下方失败明细和 hard gate"
        " 保序表为准。\n"
    )
    if prev_version_fingerprint is not None:
        para_mean = quantize_prompt_visible_style_mean(
            prev_version_fingerprint.get("paragraph_length_mean", 0.0)
        )
        sent_mean = quantize_prompt_visible_style_mean(
            prev_version_fingerprint.get("sentence_length_mean", 0.0)
        )
        simile_density = prev_version_fingerprint.get("simile_density", 0.0)
        source_total_chars = prev_version_fingerprint.get("total_chars", 0.0)
        dominant_pov = _infer_dominant_pov_label(prev_version_fingerprint)
        lines.append(
            f"- 主导人称(dominant_pov):{dominant_pov} — 保持人称,**不要切换**\n"
        )
        if "paragraph_length_mean" in anomalous_dimensions:
            paragraph_reference = next(
                item
                for item in baseline_anomalies
                if item.dimension == "paragraph_length_mean"
            )
            lines.append(
                f"- 段落均值(paragraph_length_mean):v1 约 {para_mean:.0f} 字；"
                f"前章约 {paragraph_reference.baseline_value:.0f} 字 — "
                "**本轮 WARN-only，不设同章数值硬区间；不得把它当成实际打回原因**\n"
            )
        else:
            lines.append(
                f"- 段落均值(paragraph_length_mean):约 {para_mean:.0f} 字 — "
                "保持在相对 ±50% 内,**如需增减内容就增删完整段落,不要只拉长单段**\n"
            )
        if "sentence_length_mean" in anomalous_dimensions:
            sentence_reference = next(
                item
                for item in baseline_anomalies
                if item.dimension == "sentence_length_mean"
            )
            lines.append(
                f"- 句长均值(sentence_length_mean):v1 约 {sent_mean:.0f} 字；"
                f"前章约 {sentence_reference.baseline_value:.0f} 字 — "
                "**本轮 WARN-only，不设同章数值硬区间；不得把它当成实际打回原因**\n"
            )
        else:
            lines.append(
                f"- 句长均值(sentence_length_mean):约 {sent_mean:.0f} 字 — "
                "保持在相对 ±30% 内,**不要统一压成短句或堆成长句**\n"
            )
        lines.append(
            f"- 比喻密度(simile_density):约 {simile_density:.3f} 次/句 — "
            '软性参考,**不要为修复无关项大幅增减"像/如同/仿佛/犹如"等比喻词**;'
            "偏移只记 WARN，不单独打回\n"
        )
        if para_mean > 0.0 and "paragraph_length_mean" not in anomalous_dimensions:
            paragraph_hard_min = para_mean * (
                1.0 - PARAGRAPH_LENGTH_MEAN_DRIFT_THRESHOLD
            )
            paragraph_hard_max = para_mean * (
                1.0 + PARAGRAPH_LENGTH_MEAN_DRIFT_THRESHOLD
            )
            # 不让模型贴着健康 v1 的保序 HARD 边缘赌博：取阈值一半作安全余量。
            paragraph_safe_max = para_mean * (
                1.0 + PARAGRAPH_LENGTH_MEAN_DRIFT_THRESHOLD / 2.0
            )
            lines.append(
                f"- 段落均值 [HARD] 绝对允许区间:{paragraph_hard_min:.1f}–"
                f"{paragraph_hard_max:.1f} 字；**绝对上限就是 "
                f"{paragraph_hard_max:.1f}，不要贴边，安全目标不高于 "
                f"{paragraph_safe_max:.1f}**\n"
            )
            if source_total_chars > 0.0 and paragraph_safe_max > 0.0:
                minimum_paragraphs = math.ceil(source_total_chars / paragraph_safe_max)
                lines.append(
                    f"- 段落数自检:按本章 v1 约 {source_total_chars:.0f} 字估算，"
                    f"至少 {minimum_paragraphs} 个非空正文段；若最终正文长度变化，"
                    f"按 `正文字符数 ÷ {paragraph_safe_max:.1f}` 向上取整重算。"
                    "**先拆分长段再交稿，不得用空段凑数**\n"
                )
        if sent_mean > 0.0 and "sentence_length_mean" not in anomalous_dimensions:
            sentence_hard_min = sent_mean * (1.0 - SENTENCE_LENGTH_MEAN_DRIFT_THRESHOLD)
            sentence_hard_max = sent_mean * (1.0 + SENTENCE_LENGTH_MEAN_DRIFT_THRESHOLD)
            lines.append(
                f"- 句长均值 [HARD] 绝对允许区间:{sentence_hard_min:.1f}–"
                f"{sentence_hard_max:.1f} 字\n"
            )
    else:
        lines.append(
            "_v1 fingerprint 缺失(异常路径,正常情况下 orchestrator 透传 17 项 "
            "指纹 dict)_。请按章节卡 ``setting`` / ``pov_character`` 字段保持人称,"
            "段落节奏 / 句长节奏 / 比喻密度参照章节卡的整体风格暗示。\n"
        )

    lines.append("\n### 上一轮待修版本 validator 失败明细(必须修正)\n")
    lines.append(_render_validator_failures_for_writer(validator_failures))
    lines.append("\n### hard gate 保序表(W3.5-B,本轮 rewrite 必须同时满足)\n")
    lines.append(_render_rewrite_constraint_guardrail(rewrite_constraint_guardrail))

    # W3.5-s3-revisit(RC2)+R8 ch-0003:above_max → 收敛;below_min → 扩容;
    # 非字数 / 自然 Reviewer rewrite → 只修命中项,不得伪造欠长目标。
    if is_above_max:
        lines.append("\n### 收敛方式(砍到 word_count_max 以内,保持节奏密度)\n")
        lines.append(
            "v1 写**超上限**,本次 rewrite 目标是**更短**。按以下优先级砍内容收敛到"
            "上限以内(**判断轴是『精彩 vs 水』,不是『tell vs show』**):\n"
            "1. **先砍 tell 盖章水** — 设问自答 / 情绪命名标签 / 排比主题金句(见反盖章"
            "纪律)——删了既降字数又提质量,免费收益\n"
            "2. **砍水 show** — 同一意象 / 情绪反复嚼、为演而演、不推进剧情的环境 / 动作"
            "流水账,只演一次\n"
            "3. **次要过场场景概述** — 非关键过渡段落压缩成概述\n"
            "4. **绝不砍** — 精彩 / 推进剧情的 show、核心剧情、must_achieve 事件、关键对白\n"
        )
        lines.append("\n### ⚠️ 反例(绝对不要做)\n")
        lines.append(
            "- ❌ **不要继续写更长** — 你在 rewrite 一篇 above_max 的稿,目标是**收敛变短**,"
            "**不是补内容**;别把『保持节奏密度』误读成『再加段落 / 加场景 / 加心理』\n"
            "- ❌ **不要砍精彩 show / 核心剧情 / must_achieve 来凑字数** — 砍的是冗余 tell / "
            "水 show,不是删关键演出\n"
            "- ❌ **不要改节奏 / 切人称 / 删到散文诗式** — 段落均值 / 句长的同章"
            " HARD 尺与人称 HARD 约束必须满足；比喻密度按 v1 作 WARN-only 参考\n"
        )
    elif is_below_min:
        lines.append("\n### 扩展方式(达字数下限,保持节奏密度)\n")
        lines.append(
            "通过以下 4 类**保持节奏密度**的内容扩展达字数下限,**逐类思考是否"
            "可用**(不必每类都用,挑契合本章 setting 的):\n"
            "1. **对话细节** — 角色之间的对话(直接引语 + 反应描写),每段对话仍"
            "遵守 v1 段落均值字数\n"
            "2. **内心独白** — 视角角色的心理活动(感受 + 思考 + 联想),保持 v1 句长\n"
            "3. **场景描写** — 场景的视觉/听觉/嗅觉感官细节(环境 / 空气 / 远处"
            "声音),避免堆砌比喻\n"
            "4. **动作细节** — 角色微动作(手指 / 眼神 / 步伐 / 姿态变化),保持"
            "动词密度\n"
        )

        lines.append("\n### ⚠️ 反例(绝对不要做)\n")
        lines.append(
            "- ❌ **不要拉长单个段落** — 段落均值偏离 ±50% 会被 [HARD] style_drift "
            "validator 截获\n"
            "- ❌ **不要加大量新比喻** — 比喻密度是 WARN-only 风格参考；虽不会"
            "单独打回，突变仍会污染文风\n"
            "- ❌ **不要切换人称视角** — 代词频次主导对换会被 [HARD] "
            "cross_chapter_voice_validator 截获\n"
        )
    else:
        lines.append("\n### 定向修复方式(非字数失败不得扩写)\n")
        lines.append(
            "逐条修正上方 validator / Reviewer / hard gate 保序表命中的问题。"
            "**没有 `below_min` 就不启动扩写策略**:不为凑长度新增场景、支线、对白、"
            "内心独白、环境或动作细节;只改与失败证据直接相关的句段,并保留已通过的"
            "剧情与连续性约束。"
            + ("\n" if word_count_policy_version else
               "改完后正文必须仍在章节卡 `word_count_min..word_count_max` 内,优先中线偏下。\n")
        )

        lines.append("\n### ⚠️ 反例(绝对不要做)\n")
        lines.append(
            "- ❌ **不要把 style_drift / setting / continuity / forbidden 等失败解释成欠长**"
            " — 它们要求修对应事实或风格,不是增加篇幅\n"
            "- ❌ **不要顺手重写已通过内容** — 只改命中问题,避免制造新的 hard gate 回退\n"
            "- ❌ **不要切换人称或脱离风格锚** — 非字数修复仍须保持既有节奏和 POV\n"
        )

    return "".join(lines)


def _render_rewrite_constraint_guardrail(
    guardrail: RewriteConstraintGuardrail | None,
) -> str:
    """把 W3.5-B rewrite 保序账本渲染给 Writer。

    空账本不阻断 rewrite;它只表示上一轮没有可结构化保序状态。真正的失败仍
    由 reviewer / validator 在下一轮审稿时裁决。
    """
    if guardrail is None:
        return "无 — 上一轮没有可结构化的 hard gate 状态,按本章卡与失败明细重写。\n"

    lines: list[str] = []
    lines.append("#### 本轮 HARD 失败（必须修复；结构化表）\n")
    lines.append(
        _render_constraint_table(
            guardrail.must_fix,
            violations=guardrail.constraint_violations,
        )
    )
    lines.append("\n#### 上一轮 HARD 已通过（本轮必须保留；结构化表）\n")
    lines.append(_render_constraint_table(guardrail.must_preserve))
    lines.append("\n#### 其他审稿失败（只供写作参考，不参与 HARD 回退映射）\n")
    lines.append(_render_bullets(guardrail.other_failures))
    lines.append(
        "\n#### 冲突处理\n"
        "如果“本轮失败项”和“上一轮已通过项”无法同时满足,不要靠猜测新增设定或"
        "硬编转折;优先保持章节卡事实与已通过 hard gates,让下一轮审稿明确暴露为"
        "卡/大纲问题。\n"
    )
    if any(
        violation.severity == "hard" and violation.domain == "world.unique_rules"
        for violation in guardrail.constraint_violations
    ):
        lines.append(
            "\n#### 权威世界规则修复纪律\n"
            "`world.unique_rules` 高于章节卡措辞、累积角色状态、检索记忆和源正文。"
            "若这些派生内容与本轮命中的权威规则冲突，冲突部分视为污染，不属于"
            "必须保序的事实。必须删除或改写表中证据表达的冲突命题，使语义与权威"
            "规则一致；只换同义词、补充原因或把冲突说得更明确，仍然是失败。"
            "‘本章仍处于某状态’只描述当前时点，不得自行升级为‘永久／不可逆’。\n"
        )
    return "".join(lines)


def _render_constraint_table(
    results: list[ConstraintResult],
    *,
    violations: list[ConstraintViolation] | None = None,
) -> str:
    """渲染稳定 ID 表；解释/证据只展示，不参与身份或回退比较。"""
    lines = [
        "| constraint_id | domain | status | 权威规则 | 解释/证据 |\n",
        "|---|---|---|---|---|\n",
    ]
    hard_violations = [item for item in (violations or []) if item.severity == "hard"]
    if not results and not hard_violations:
        lines.append("| （无） | - | - | - | - |\n")
        return "".join(lines)
    for result in results:
        rule = result.rule_text.replace("|", "｜").replace("\n", " ")
        details = result.details.replace("|", "｜").replace("\n", " ")
        lines.append(
            f"| {result.constraint_id} | {result.domain} | {result.status} | "
            f"{rule} | {details or '-'} |\n"
        )
    for violation in hard_violations:
        rule = violation.rule_quote.replace("|", "｜").replace("\n", " ")
        evidence = violation.evidence_quote.replace("|", "｜").replace("\n", " ")
        if violation.explanation:
            explanation = violation.explanation.replace("|", "｜").replace("\n", " ")
            evidence = f"{evidence}；原因：{explanation}"
        if violation.suggested_action:
            action = violation.suggested_action.replace("|", "｜").replace("\n", " ")
            evidence = f"{evidence}；必须修法：{action}"
        lines.append(
            f"| {violation.constraint_id} | {violation.domain} | fail | "
            f"{rule} | {evidence} |\n"
        )
    return "".join(lines)


def _infer_dominant_pov_label(fingerprint: dict[str, float]) -> str:
    """v1.1 G11:从 fingerprint 的 9 项代词频次推断主导人称的可读 label。

    与 ``style_fingerprint.summarize_voice_from_fingerprint`` 同型逻辑(避免
    循环引用 + 让 writer 模块自包含),但只返 LLM 友好的字符串而非 PovSummary
    对象。
    """
    third_pronouns = ("他", "她", "它", "他们", "她们", "它们")
    first_pronouns = ("我", "我们")
    third_total = sum(fingerprint.get(f"pronoun_freq_{p}", 0.0) for p in third_pronouns)
    first_total = sum(fingerprint.get(f"pronoun_freq_{p}", 0.0) for p in first_pronouns)
    if third_total == 0.0 and first_total == 0.0:
        return "third_person(默认,代词频次极低)"
    # 阈值与 POV_DOMINANCE_RATIO 一致;详见 style_fingerprint.py
    if first_total >= third_total * 2.0:
        return "first_person(我为主)"
    if third_total >= first_total * 2.0:
        return "third_person(他/她为主)"
    return "mixed(双方都不超阈值,可能是 free 视角 / 章内切换)"


def _render_validator_failures_for_writer(
    failures: list[dict[str, Any]] | None,
) -> str:
    """v1.1 G11:把 validator_failures 列表渲染成 writer 友好的 markdown。

    与 reviser ``_render_validator_failures`` 同型(每条 dict
    ``{"name": <validator>, "details": <dict>}``),但 writer 视角写"必须修正"
    指引而非 reviser 的"局部 patch"指引。空列表只陈述“没有可用明细”，
    不猜成 Reviewer 自然 rewrite；产品 UI 的人工强制 Rewrite 同样可能没有失败。
    """
    if not failures:
        return (
            "无 — 当前 checkpoint 没有可用的 validator 失败明细；可能是人工强制"
            " Rewrite，或 Reviewer 自然 rewrite。不要据此假定字数不足。\n"
        )

    lines: list[str] = []
    for index, failure in enumerate(failures, start=1):
        name = failure.get("name", "<unknown>")
        details = failure.get("details") or {}
        details_lines = "\n".join(
            f"  - `{key}`:{value}" for key, value in details.items()
        )
        lines.append(
            f"#### 失败 {index}\n\n- `name`:{name}\n- `details`:\n{details_lines}"
        )
    return "\n\n".join(lines) + "\n"


def _format_voice_baseline(summary: PovSummary) -> str:
    """v1.1 wave 1 session 6 / G10:渲染前一章风格基线段。

    与 ``prompts/writer.md`` v5 "## 跨章节视角 / 风格基线"段对齐,LLM 看到
    本段后应贴合前一章的主导人称 / 节奏密度。**首章无 baseline** 时 caller
    传 ``None``,本函数不被调用(_build_user_message 跳过该段)。段均 / 句均在
    初稿阶段是跨章参考 / WARN，不得提前伪装成 rewrite 才适用的同章保序 HARD。

    v2 w2-s2:``summary.anchor_source == "book_anchor"``(注入端 A/B ON 臂)
    时段均/句均数字来自**全书开篇锚**(ch1-3 final 指纹中位数)而非前一章
    — 渲染字面同步换为"全书风格锚",防 prompt 段名与数字来源不符(prompt
    自己撒谎);主导人称 / 比喻密度行仍如实标注来自前一章。
    """
    pov_text_map = {
        "third_person": "third_person(他/她为主)",
        "first_person": "first_person(我为主)",
        "mixed": "mixed(双方都不超阈值,可能是 free 视角 / 章内切换)",
    }
    pov_text = pov_text_map[summary.dominant_pov]
    if summary.anchor_source == "book_anchor":
        return (
            "## 跨章节视角 / 风格基线(BOOK VOICE ANCHOR)\n\n"
            f"- 主导人称(dominant_pov,来自前一章):{pov_text}\n"
            f"- 段落均值(paragraph_length_mean,全书开篇锚):约 "
            f"{summary.paragraph_length_mean:.0f} 字\n"
            f"- 句长均值(sentence_length_mean,全书开篇锚):约 "
            f"{summary.sentence_length_mean:.0f} 字\n"
            f"- 比喻密度(simile_density,来自前一章):约 {summary.simile_density:.3f}"
            '(每句出现"像/如同/仿佛/犹如"等比喻词的平均次数)\n\n'
            "**真实约束层级**:主导叙述人称是跨章节 [HARD] 约束；`pov_change` 只"
            "记录跨章主视角角色变化，不授权人称漂移或章内切换。只有卡片 setting / "
            "mini plan 明确梦境、回忆、倒叙或 POV 切换时例外。段落均值 / 句长"
            "均值锚定的是全书"
            "开篇基线,用于起草时拉住长期节奏；跨章段均偏移只记 WARN，句均 / 比喻"
            "密度不由跨章闸单独硬拒。**若后面还有 `SAME CHAPTER V1 BASELINE` rewrite"
            " 段，只有未被 G17 点名的健康 v1 段均 / 句均才是本轮同章保序"
            " [HARD] 尺；异常维与比喻密度仍为 WARN-only。**"
        )
    return (
        "## 跨章节视角 / 风格基线(PREV CHAPTER VOICE BASELINE)\n\n"
        f"- 主导人称(dominant_pov):{pov_text}\n"
        f"- 段落均值(paragraph_length_mean):约 {summary.paragraph_length_mean:.0f} 字\n"
        f"- 句长均值(sentence_length_mean):约 {summary.sentence_length_mean:.0f} 字\n"
        f"- 比喻密度(simile_density):约 {summary.simile_density:.3f}"
        '(每句出现"像/如同/仿佛/犹如"等比喻词的平均次数)\n\n'
        "**真实约束层级**:主导叙述人称是跨章节 [HARD] 约束；`pov_change` 只记录"
        "跨章主视角角色变化，不授权人称漂移或章内切换。只有卡片 setting / mini "
        "plan 明确梦境、回忆、倒叙或 POV 切换时例外。段落均值用于跨章 WARN "
        "观察，句长均值"
        " / 比喻密度是起草参考，不由跨章闸单独硬拒。**若后面还有 "
        "`SAME CHAPTER V1 BASELINE` rewrite 段，只有未被 G17 点名的健康 v1"
        " 段均 / 句均才是本轮同章保序 [HARD] 尺；异常维与比喻密度仍为"
        " WARN-only。**"
    )


def _format_card_core(card: ChapterCard) -> str:
    """章节卡的标量字段块。"""
    return (
        "## 章节卡\n\n"
        f"- 标题:{card.title}\n"
        f"- 视角角色(pov_character):{card.pov_character}\n"
        f"- 场景设置(setting):{card.setting}\n"
        f"- 张力等级(tension_level):{card.tension_level}/10\n"
        + (f"- {render_word_count_task(card)}\n" if card.word_count_policy_version == 2 else
           f"- 字数区间:word_count_min={card.word_count_min},word_count_max={card.word_count_max}\n")
        + f"- 剧情节点预算(plot_node_budget):{card.plot_node_budget}\n"
        f"- 弧段(arc_phase):{card.arc_phase}"
        + (",本章为弧线终章" if card.is_arc_finale else "")
    )


def _format_constraint_lists(
    card: ChapterCard,
    *,
    draft_expansion_plan: WriterDraftExpansionPlan | None = None,
) -> str:
    """渲染三组内容合同；exact-Hy3 首稿只引用已经展示的必达原文。"""

    if draft_expansion_plan is None:
        must_block = _render_bullets(card.must_achieve)
        unresolved_block = _render_bullets(card.must_unresolved)
        forbidden_block = _render_bullets(card.forbidden)
    else:
        must_block = (
            "\n".join(
                "- [authority_ref:"
                f"{_draft_expansion_ref(plan=draft_expansion_plan, text=item)}]"
                for item in card.must_achieve
            )
            if card.must_achieve
            else "无"
        )
        unresolved_block = (
            "\n".join(
                f"- [must-unresolved-{index + 1}] {item}"
                for index, item in enumerate(card.must_unresolved)
            )
            if card.must_unresolved
            else "无"
        )
        forbidden_lines: list[str] = []
        for index, item in enumerate(card.forbidden):
            if _next_event_holdback_event(item) is not None:
                forbidden_lines.append(
                    "- [authority_ref:"
                    f"chapter-boundary-holdback-{index + 1}]"
                )
            else:
                forbidden_lines.append(f"- [forbidden-{index + 1}] {item}")
        forbidden_block = "\n".join(forbidden_lines) if forbidden_lines else "无"
    return (
        "## 内容契约\n\n"
        "### 必达事件(must_achieve)\n"
        f"{must_block}\n\n"
        "### 必须保持未解决(must_unresolved)\n"
        f"{unresolved_block}\n\n"
        "### 禁止元素(forbidden)\n"
        f"{forbidden_block}"
    )


def _format_dramatic_source(card: ChapterCard) -> str:
    """兼容既有测试/调用点；实际装配由共享 salience 模块唯一实现。"""

    return render_dramatic_source(card)


def _format_writer_hard_gate_contract(
    card: ChapterCard,
    *,
    creation_type: Literal["draft", "rewrite", "revise"] | None,
    executable_constraint_plan: ExecutableConstraintPlan | None = None,
    core_state_projection: CoreStateProjection | None = None,
    draft_expansion_plan: WriterDraftExpansionPlan | None = None,
) -> str:
    """把正文下游裁决的同一套约束身份显式前置给 Writer。

    本段不复制章节卡具体列表；列表仍以紧邻上方的 ChapterCard / ContinuityContext
    为 SSOT。本段只声明级别、适用阶段、来源与例外，防 draft、rewrite、Reviewer
    各自发明另一把尺。``revise`` 不走 Writer，防御性渲染仍保持同一语义。
    """
    stage: ProseConstraintStage = creation_type or "draft"
    plan = executable_constraint_plan or compile_executable_constraint_plan(
        chapter_card=card,
        stage=stage,
        core_state_projection=core_state_projection,
    )
    plan.assert_matches_chapter_card(card)
    plan.assert_matches_core_state(
        chapter_card=card,
        projection=core_state_projection,
    )
    if plan.stage != stage:
        raise ValueError("ExecutableConstraintPlan stage 与 Writer 合同阶段不一致")
    rendered = (
        _render_writer_executable_constraint_plan(
            plan=plan,
            draft_expansion_plan=draft_expansion_plan,
        )
        if draft_expansion_plan is not None
        else render_executable_constraint_plan(plan).replace(
            "## 可执行 HARD 约束计划(EXECUTABLE CONSTRAINT PLAN)",
            "## 本轮正文约束合同(WRITER/VALIDATOR SSOT)",
            1,
        )
    )
    style_boundary = (
        "rewrite 只对动态 `SAME CHAPTER V1 BASELINE` 中已经编译的健康 v1 "
        "`style_drift` 执行 HARD；"
        "未列入计划的异常维仍为 WARN。"
        if stage == "rewrite"
        else "初稿尚无 v1，不得预造 rewrite 保序尺。"
    )
    return (
        f"{rendered}\n\n"
        "### 级别边界\n\n"
        f"- 当前阶段:`{stage}`。`{PARAGRAPH_MEAN_HARD_CONSTRAINT_ID}` 只取计划表"
        "中的共享阈值；所有消费者按同一 plan_hash 执行。\n"
        f"- {style_boundary}\n"
        "- 角色/卡片数据合法性仍由 Provider 前 admission 负责，不伪装成正文 rewrite。\n"
        "- 跨章段均/句均参考、style trend、去重、章末 tension、time_gap、动机、"
        "氛围与文学留白均是 WARN/SOFT；任何 Reviewer、rewrite 或 revise 都"
        "不得把它们临时升级成 HARD。"
    )


def _format_scene_handoff_focus(card: ChapterCard) -> str:
    """原样展示上游场景意图，避免通用简笔提示稀释卡片指定的核心过程。"""
    fields = (
        ("结构", card.chapter_focus),
        ("互动", card.interaction_focus),
        ("成长落点", card.breakthrough_payoff),
        ("展开重点", card.craft_focus),
    )
    parts = [f"### {name}\n\n{text}" for name, text in fields if text.strip()]
    return "## 本章场景重点\n\n" + "\n\n".join(parts) if parts else ""


def _format_chapter_focus(card: ChapterCard) -> str:
    """渲染"## 本章结构焦点"段(M4''',v1.1 wave 5 s6 / 结构跟戏剧功能走)。

    ``card.chapter_focus`` 为空 → 返回 ``""``(Writer 跳过本段,行为与 s6 前逐字节
    等价 —— 默认结构章 / 旧卡留空均走此路径)。非空 → 把 outline mini_plan 层据本章
    戏剧功能定的"结构形态"(情感单焦 / 角色单焦 / backstory / 纯单线困局)交给 Writer,
    让其**别按题材默认结构对半稀释**本章。

    **连贯硬约束**(本段措辞核心):应用焦点时,被弱化的那一界 / 线**退为简短回响或侧写
    一笔带过**,而非凭空消失 —— 结构跟功能走的前提是剧情连贯不塌(双界设定逻辑不留洞)。
    与 ``_format_craft_focus`` 正交:chapter_focus 管"本章整体形态",craft_focus 管
    "形态之内笔墨投哪几处"。
    """
    focus = card.chapter_focus.strip()
    if not focus:
        return ""
    return (
        "## 本章结构焦点(chapter_focus)\n\n"
        f"{focus}\n\n"
        "按上面的结构形态写本章:**别按题材默认结构(如双线并行 / 多线交织)对半稀释"
        "本章焦点**。被弱化的那一界 / 线退为简短回响或侧写一笔带过即可,把分量集中到"
        "焦点上(情感峰就让读者在一个世界里把情绪走完,别切走;角色单焦就守住那个角色"
        "的处境与内心)。**但连贯优先**:被弱化的一界不能凭空消失或留逻辑断点 —— 用一两"
        "句交代它的去向 / 状态衔接,既不平行铺开稀释、也不制造设定漏洞。"
    )


def _format_craft_focus(card: ChapterCard) -> str:
    """渲染"## 本章卓越性焦点"段(M2 纠正侧,v1.1 wave 5 s3 / card per-scene 标注)。

    ``card.craft_focus`` 为空 → 返回 ``""``(Writer 跳过本段,行为与 s3 前逐字节
    等价 —— 旧卡 / 动作章留空均走此路径)。非空 → 把卡生成期 LLM 规划的"本章哪几处
    深挖内心 / 给画面、其余克制"per-scene 指引交给 writer,让其规划期就知道把笔墨投
    在哪。配套一句通用提醒(详略有致 / 别抽象词带过),与静态 `_CRAFT_SECTION` 近普世
    内核互补 —— craft_focus 是本章特化的"具体投在哪几处"。
    """
    focus = card.craft_focus.strip()
    if not focus:
        return ""
    return (
        "## 本章卓越性焦点(craft_focus)\n\n"
        f"{focus}\n\n"
        "按上面的指引把笔墨投在吃重的场景:情感节点 / 抉择处给具体内心活动(在权衡 / "
        "害怕 / 动摇什么,别用'他很纠结''下定决心'一笔带过),关键戏给可感画面(别用"
        "抽象词总结);其余过场 / 推进处保持克制简笔 —— 详略有致,不必处处铺陈。"
    )


def _format_arc_beats(
    card: ChapterCard,
    *,
    draft_expansion_plan: WriterDraftExpansionPlan | None = None,
) -> str:
    """渲染"## 本章角色弧光台阶"段(M5,v1.1 wave 5 s7 / 逐章弧光轨迹)。

    ``card.arc_beats`` 为空 dict → 返回 ``""``(Writer 跳过本段,行为与 s7 前逐字节
    等价 —— 绝大多数章无弧光推进 / 旧卡均走此路径)。非空 → 把 outline mini_plan 层
    据本章戏剧功能定的"某角色本章推进的弧光台阶 + 动作演出方式"交给 Writer。

    **命门(本段措辞核心):动作演出非旁白声明** —— B1 读者实测病根正是"角色成长全靠
    旁白反复声明('理性防线崩塌'≥5 次)而非行动体现"。本段逼 writer 用具体动作 / 抉择 /
    代价演出弧光台阶,**禁止**用"他开始动摇 / 他成长了"式旁白概述带过。与 G1 累积状态
    互补:G1 = 地板(已确立状态别倒退),arc_beats = 本章该迈的那一步(向前)。value
    自包含(点名角色 + 台阶 + 动作),按值渲染。
    """
    beats = {k: v.strip() for k, v in (card.arc_beats or {}).items() if v and v.strip()}
    if not beats:
        return ""
    if draft_expansion_plan is None:
        lines = "\n".join(f"- {v}" for v in beats.values())
    else:
        lines = "\n".join(
            f"- character_id={character_id}; authority_ref:"
            f"`{_draft_expansion_ref(plan=draft_expansion_plan, text=text)}`"
            for character_id, text in beats.items()
        )
        return (
            "## 本章角色弧光台阶(arc_beats)\n\n"
            f"{lines}\n\n"
            "原文已在展开计划中唯一展示。把对应变化放进人物当场的动作、反应、"
            "取舍或代价，让它和主线同场发生；不要在正文复述引用或用旁白宣布成长。"
        )
    return (
        "## 本章角色弧光台阶(arc_beats)\n\n"
        f"{lines}\n\n"
        "把上面每条角色弧光台阶**用动作 / 抉择 / 代价演出**,让读者从'角色做了什么、"
        "怎么反应'里**看出**这一步成长。**绝不允许**用旁白概述声明(❌'他理性的防线开始"
        "崩塌''她动摇了对规则的信仰''他变得独立了')—— 那是本书弧光最大的病根。"
        "✅ 正确做法:给一个**具体的、能体现这一步的动作或抉择**(他第一次没按惯例处理、"
        "她亲手做了以前绝不会做的事、他在某个选择前停顿后选了相反的方向),用细节落地。"
        "**在角色截止上一章的累积状态基础上向前一步**,与已确立事实不矛盾;这一步该是本章"
        "自然长出来的,不必喧宾夺主压过主线事件。"
    )


def _format_interaction_focus(
    card: ChapterCard,
    *,
    authority_only: bool = False,
) -> str:
    """渲染"## 本章互动场景中心"段(M4''-轴A,v1.1 wave 5 s8 / 互动驱动 show-not-tell)。

    ``card.interaction_focus`` 为空 → 返回 ``""``(Writer 跳过本段,行为与 s8 前逐字节
    等价 —— 独处留白章 / 非互动中心章 / 旧卡均走此路径)。非空 → 把 outline mini_plan 层
    据本章戏剧功能定的"本章互动中心 + 配角当真人"交给 Writer。

    **命门(本段措辞核心):配角是真人非背景板 + 情感从互动里长出来非旁白。** 治松 beat
    题材(治愈日常 / 慢生活)读者实测病根:戏剧引擎默认让主角独自做事 / 感慨,即便配角在场
    也被写成"催化主角内心感悟的符号",情感靠旁白灌输。本段逼 writer 把这场互动写成**双向
    对手戏**(配角有自己的声音 / 动作 / 目的 / 与主角的温和摩擦),让情感**从两人你来我往
    里自然显现**,而非主角观察 + 大段内心独白。与 ``_format_craft_focus`` 正交:craft_focus
    管"哪几处深挖内心 / 给画面"(笔墨投放),本段管"这场互动里配角的主体性 + 情感靠演非独白"。
    与治愈基调不冲突:温和摩擦 ≠ 尖锐冲突(护治愈底色)。
    """
    focus = card.interaction_focus.strip()
    if not focus:
        return ""
    if authority_only:
        return (
            "## 本章互动执行(interaction_focus)\n\n"
            f"{focus}\n\n"
            "把这条本章特定要求写成双向行动：在场人物各自有目标、动作和即时反应，"
            "互动要实际改变眼前局面或彼此判断。可自由选择冲突、协作、迟疑、沉默或"
            "对话的具体写法；细节须留在当前场景，不额外建立背景、长期关系或剧情结果。"
        )
    return (
        "## 本章互动场景中心(interaction_focus)\n\n"
        f"{focus}\n\n"
        "把本章这场互动**写成一场真实的双向对手戏**:在场的那个配角要是个**有主体性的"
        "真人** —— 有自己的声音 / 动作 / 目的 / 此刻的状态,会主动说话、追问、做事、与主角"
        "产生温和的分歧或碰撞,**不是一个只为触发主角感悟而存在的背景符号**。让本章的情感"
        "**从两人你来我往的对话和行动里自然长出来**(读者从他们说了什么、做了什么、怎么"
        "反应里**体会到**情绪),**别用主角大段内心独白 + 旁白直接说'她心头一暖 / 焦虑被"
        "化解了'来代替演出**。\n"
        "**尤其禁止三种'替读者盖章意义'的写法**(本题材互动戏最易犯、最毁味道):\n"
        "① **别用比喻去总结某句话 / 某个瞬间对主角的意义** —— 例如'(那句话)像一颗石子投入"
        "她心底的死水,荡起一圈圈涟漪''像一记轻锤敲在她心中的盲区''像一把柔软的刷子扫过"
        "她坚硬的逻辑外壳'。让那句话、那个动作**自己留在那里**,后面**别再追加一句旁白替"
        "读者解释它多有分量** —— 读者自会体会。\n"
        "② **别写直接点破主题的总结句** —— 例如'这种顺其自然的小镇哲学,与她目标导向的都市"
        "思维产生了温和的碰撞''她终于学会享受那些无需向任何人证明的微小瞬间'。主题靠戏"
        "本身承载,**别让叙述者跳出来贴标签**。\n"
        "③ **配角的台词要像真人随口说的家常话** —— 具体、带自己的生活质感、可以有点俏皮或"
        "拌嘴(如'你们城里来的孩子就是太较真''这有啥好琢磨的,我去看看灶上的汤'),"
        "**别句句都是打磨光滑、可以裱起来的人生格言金句**。智慧藏在家常里,不在说教里。\n"
        "**护治愈基调**:温和的摩擦 / 分歧可以,但不要为戏剧性制造尖锐残酷的冲突。**护留白**:"
        "这是本章互动该吃重之处,不必把全章填满对话 —— 该安静的间隙仍可留白,只是别让"
        "'独处独白'吞掉本该演出来的互动。"
    )


def _format_chapter_boundary(
    card: ChapterCard,
    *,
    authority_only: bool = False,
) -> str:
    """Render one natural chapter boundary without duplicating holdback prose."""

    if not authority_only:
        return (
            "## 章末钩子\n\n"
            f"为下一章留下接入点,主线索:{card.next_chapter_seed}\n"
            "(不要直接复述这段文字,用情节/伏笔体现即可。)"
        )

    holdback_lines = [
        f"- [chapter-boundary-holdback-{index + 1}] 本章范围外的保留事件：{event}"
        for index, item in enumerate(card.forbidden)
        if (event := _next_event_holdback_event(item)) is not None
    ]
    holdback_block = (
        "\n".join(holdback_lines)
        if holdback_lines
        else "- 本章没有另行冻结的后续事件。"
    )
    return (
        "## 章节收束边界\n\n"
        f"- [chapter-boundary-hook] 收束方向：{card.next_chapter_seed}\n"
        f"{holdback_block}\n\n"
        "正文只写到足以让收束方向自然成立的可观察线索、压力或人物决定。若收束"
        "方向本身写成了尚未发生的行动或结论，只留下促使它发生的当前线索，不把"
        "该行动或结论提前写完。上列保留事件的主要动作和结果均不进入本章；若两者"
        "语义相近，以保留边界为准。只在正文中呈现故事，不写这些标签或规则名称。"
    )


def _format_breakthrough_payoff(card: ChapterCard) -> str:
    """渲染"## 本章升级可感化"段(模块①,v1.1 wave 5 s10 / 治升级流'突破爽点失约')。

    ``card.breakthrough_payoff`` 为空 → ``""``(跳过;非突破章 / 非 3 题材 / 开关 OFF /
    旧卡均走此路径,逐字节零回归)。非空 → 把 mini_plan 层标的"本章突破到什么 + 可感化
    落点"交给 Writer,逼它把境界突破写成**可感的实力跃迁事件**而非"痛苦氛围 + 一个境界
    名词"。

    **命门(grounding 实证)**:升级是内部状态变化、无内生反差;打脸是外部对抗事件、反差
    自带,故打脸已 work、升级失约。本段补的正是缺失的"让升级可感"指令。**起点境界由模块②
    的'## 角色截止状态'段注入,本段不重复**,只逼写出 起点→终点 的跃迁落点 + 新能力。
    """
    payoff = card.breakthrough_payoff.strip()
    if (
        not payoff
        or payoff == "无"
        or payoff == "无（本卷无突破体系事件）"
        or payoff == "无(本卷无突破体系事件)"
    ):
        return ""
    return (
        "## 本章升级可感化(breakthrough_payoff)\n\n"
        f"{payoff}\n\n"
        "本章有境界 / 实力突破——**必须让读者真切感到'变强了'这件事**,而不是只写一句"
        "'他突破到了 X 境'就过。具体要求:\n"
        "① **对比起点给出可感的实力跃迁**:结合上方'## 角色截止状态'里的起点境界,让读者"
        "感到这一跃的**落差**(例:之前需险胜 / 拼命才能应付的对手 / 处境,突破后能轻松"
        "碾压 / 化解)。\n"
        "② **展示新能力,不只报名词**:给一个**突破后能做到、之前做不到**的具体动作 / 感知 /"
        "战力的画面(灵识外放范围翻倍、一击碎之前碎不动的护体、感官 / 速度的质变),让'强'"
        "是被**演出来**的。\n"
        "③ **痛苦 / 异象只作铺垫,不作落点**:经脉撕裂 / 能量暴走 / 心魔那类突破过程的痛苦"
        "渲染最多是**铺垫**,**真正的落点必须是可感的变强** —— 别让一章突破从头到尾只有"
        "痛苦氛围 + 结尾一个境界名词。\n"
        "**护节制**:不必堆砌浮夸的旁白吹嘘,实力跃迁靠**具体的对比和动作**显现,不靠"
        "叙述者喊'他变得无比强大'。"
    )


def _build_progression_section(
    *,
    db_session: Session | None,
    chapter_card: ChapterCard,
    enabled: bool,
    label: str,
) -> str:
    """构造 user message 的"## 角色进阶等级"段(模块②,v1.1 wave 5 s10 / 境界一致性)。

    防"莫名倒退 / 写串"的**主力**(注入↔写出闭环的'注入'端):对 ``pov_character`` +
    ``related_characters``,显式 SQL lookup 各角色截止上一章(全局序 < 本章)最新快照的
    ``progression_rank``,注入"角色截止上一章{label}=X + 单调规则"。

    **gated**:``enabled=False``(非 3 题材 / 开关 OFF)→ ``""``(跳过,逐字节零回归);
    无任何角色有进阶等级(首章 / 旧数据)→ 也 ``""``。题材标签 ``label`` = 境界 / 战力 /
    段位(由 caller 按题材给)。与 G1 状态段并列(各管不同维度),放其后。
    """
    if not enabled or db_session is None:
        return ""
    current_seq = chapter_seq_from_id(chapter_card.chapter_id)
    if current_seq is None:
        return ""
    char_ids: list[str] = []
    if chapter_card.pov_character:
        char_ids.append(chapter_card.pov_character)
    for cid in chapter_card.related_characters:
        if cid not in char_ids:
            char_ids.append(cid)
    blocks: list[str] = []
    for character_id in char_ids:
        character: Character | None = get_character(db_session, character_id)
        if character is None:
            continue
        snapshot = latest_state_snapshot_before(character.state_timeline, current_seq)
        if snapshot is None or not snapshot.progression_rank:
            continue
        blocks.append(
            f"### {character.name}({character_id})"
            f"(截止第 {snapshot.as_of_chapter} 章):{snapshot.progression_rank}"
        )
    if not blocks:
        return ""
    header = (
        f"## 角色进阶{label}(PROGRESSION AS OF PREVIOUS CHAPTER)\n\n"
        f"以下是出场角色**截止上一章的{label}**。本章正文写到这些角色时**必须与之保持一致**:\n"
        f"- **{label}单调不倒退**:已达成的{label}**只随突破上升,不会无故倒退**;\n"
        f"- **倒退必须有交代**:只有本章正文**明写**了原因(突破失败 / 被强敌打落 / 被废 /"
        f"封印 / 重伤 / 自爆反噬等)时,{label}才可下降——**不许凭空跌档**;\n"
        f"- **越阶可以、但顺设定**:以低{label}击败 / 压制高{label}者(越级战)是允许的爽点,"
        f"但要符合本书力量体系自己的规则(若设定有'吞噬 / 越阶'限制,给出对应的方法或代价)。\n\n"
    )
    return header + "\n\n".join(blocks)


def _format_continuity(
    ctx: ContinuityContext,
    *,
    continuity_capsule: ProvenanceBoundContinuityCapsule | None = None,
    frozen_provenance: ContinuityLedgerProvenance | None = None,
    authority_only: bool = False,
    bridge_scope_limited: bool = False,
) -> str:
    """衔接桥块。``prev_chapter_tail`` 为空(首章场景)时仍输出但标注无前文。"""
    if continuity_capsule is not None:
        try:
            continuity_capsule.assert_matches_projection(
                frozen_provenance=frozen_provenance,
                prev_chapter_tail=ctx.prev_chapter_tail,
                unfinished_actions=ctx.unfinished_actions,
                pov_from=ctx.pov_from,
            )
        except ValueError:
            continuity_capsule = None
    if continuity_capsule is not None:
        tail_source = continuity_capsule.raw_final_tail
        pov_from = continuity_capsule.pov_character
    else:
        tail_source = ctx.prev_chapter_tail
        pov_from = ctx.pov_from
    tail = tail_source.strip()
    tail_block = tail if tail else "(本章为首章或无可衔接的前文)"
    bridge_block = (
        ctx.continuity_bridge
        if ctx.continuity_bridge.strip()
        else "(本章没有单独批准的过桥路线)"
    )
    unfinished_block = _render_bullets(ctx.required_actions)
    background_block = (
        "\n\n### 前章仍未解决的背景\n"
        "这些事项保留为背景，按本章剧情需要使用；不要求逐项回想、重复场景或当章解决。\n"
        + _render_bullets(ctx.background_unresolved)
        if ctx.background_unresolved else ""
    )
    pov_change_text = "切换" if ctx.pov_change else "未切换"
    pov_from_text = pov_from if pov_from is not None else "无(首章)"

    heading = "## 开场承接" if authority_only else "## 衔接桥(ContinuityContext)"
    tail_heading = (
        "### 前章已发生的尾文（阅读背景，不是本章开场待办）"
        if authority_only and bridge_scope_limited
        else "### 开场时仍然有效的最后画面"
        if authority_only
        else "### 上一章末尾(prev_chapter_tail)"
    )
    bridge_heading = (
        "### 即时承接说明"
        if bridge_scope_limited
        else "### 开场动作顺序"
        if authority_only
        else "### 已批准的具体过桥路线(continuity_bridge)"
    )
    unfinished_heading = (
        "### 尚未完成、必须继续处理的动作 [continuity-unfinished-actions]"
        if authority_only
        else "### 上一章未完成动作(unfinished_actions)"
    )
    bridge_instruction = (
        "以下说明只用于承接上一章即时未完动作，不为本章其余行动增加开场完成或"
        "排序要求。桥接中若概括了本章后续选择与结果，不得在相应压力、尝试和"
        "因果前提出现前提前兑现；按本章既定因果关系让它们在现场发生："
        if bridge_scope_limited
        else "以下路线由章节卡上游批准，正文必须执行其承接次序；不要另造替代路线："
    )
    return (
        f"{heading}\n\n"
        f"- 时间间隔(time_gap):{ctx.time_gap}\n"
        f"- {'[continuity-scene] ' if authority_only else ''}"
        f"场景延续(scene_continuation):{ctx.scene_continuation}\n"
        f"- 跨章主视角角色变化(pov_change):{pov_change_text}"
        f"(从 {pov_from_text} → {ctx.pov_to})\n\n"
        "  - 说明:该标记只描述章节边界，不授权本章内部切换其他角色视角或改变叙述人称。\n\n"
        f"{tail_heading}\n\n"
        f"{tail_block}\n\n"
        f"{bridge_heading}\n\n"
        f"{bridge_instruction}\n\n"
        f"{bridge_block}\n\n"
        f"{unfinished_heading}\n"
        f"{unfinished_block}{background_block}"
    )


def _render_bullets(items: list[str]) -> str:
    """把字符串列表渲染成 markdown 项目符号;空列表返回"无"。"""
    if not items:
        return "无"
    return "\n".join(f"- {item}" for item in items)


# ---- P2-B-10:检索记忆 + 角色声音档案 ----------------------------------------


#: v1.1 wave 3 session 6(E1):chapter_summary 检索维度的 top_k 上限。章节摘要
#: 单条文本远长于 fact / character 档案(~200-500 字),不封顶会撑爆 Writer prompt
#: 并稀释 fact / character 维度;取 3 条最相关历史章节摘要作"长程事件回响"补充,
#: 与 chapter_card_generator 的 ``prev_chapter_summaries``(按近 N 章 recency 注入)
#: 互补 —— 本维度按向量相关性召回,跨越"最近"窗口拿回真正相关的早期章节摘要。
_CHAPTER_SUMMARY_TOP_K = 3


def _build_retrieved_memory_section(
    *,
    vector_store: VectorStore | None,
    chapter_card: ChapterCard,
    top_k: int,
    workspace_id: str | None = None,
) -> str:
    """构造 user message 的"## 检索记忆"段(P2-B-10;v1.1 wave 3 s6 E1 加摘要维度)。

    - ``vector_store=None`` → 返回空字符串(P1 路径,user message 不附该段)。
    - ``vector_store`` 非空时调三次 retriever:
      1. ``where={"entity_type": "fact"}`` → 拿 top_K fact 类语义记忆
      2. ``where={"$and": [{"entity_type": "character"}, {"entity_id":
         {"$in": card.related_characters}}]}`` → 拿出场角色最新档案
         (``related_characters`` 为空时跳过,避免传空 ``$in`` 列表)
      3. ``where={"entity_type": "chapter_summary"}`` → 拿 top-N(``_CHAPTER_SUMMARY_TOP_K``)
         最相关历史章节摘要(v1.1 wave 3 s6 E1 加;摘要文本长,top_k 单独封顶)
    - 三条结果合并展示;全空时仍输出该段标题但写"暂无相关检索",防止上游不一致
      (本章卡可能值得检索但库里空,显式告诉 LLM "无可参考"而非缺省)。
    - 失败语义:retriever 错误自然冒泡,由 caller 决定降级策略。

    **E1 设计说明**:章节摘要以 ``entity_type="chapter_summary"`` 写进同一
    ``"memories"`` collection(由 ``chapter_summary_extractor`` 落 SQLite 时同步落
    Chroma)。因本函数按 ``entity_type`` 显式过滤,新增维度对既有 fact / character
    两维度**零影响**;摘要维度只在本函数显式发起第三条 query 时才被召回。
    """
    if vector_store is None:
        return ""

    # G1(v1.1 wave 4 session 1):时序过滤本章全局序之前的记忆,防止"未来 / 本章"
    # 记忆 + 过期状态泄漏进 prompt。**只过 fact + chapter_summary**(它们每章累积,
    # 有未来泄漏风险);**character 维度不过**——角色档案是单条 upsert 的"最新档案",
    # 时序过滤反而可能把刚更新的最新档案排除掉(精确状态由下方"角色截止状态"段的
    # SQL state_timeline 提供,不依赖向量检索)。``ch-NNNN`` 模式外的 chapter_id
    # 解析为 None → before_chapter_seq=None 退化为不过滤(向后兼容)。
    before_chapter_seq = chapter_seq_from_id(chapter_card.chapter_id)

    fact_hits = retrieve_memories_for_chapter(
        chapter_card,
        vector_store=vector_store,
        collection=DEFAULT_COLLECTION,
        top_k=top_k,
        where={"entity_type": "fact"},
        workspace_id=workspace_id,
        before_chapter_seq=before_chapter_seq,
    )

    character_hits: list[RetrievedMemory] = []
    if chapter_card.related_characters:
        character_hits = retrieve_memories_for_chapter(
            chapter_card,
            vector_store=vector_store,
            collection=DEFAULT_COLLECTION,
            top_k=top_k,
            where={
                "$and": [
                    {"entity_type": "character"},
                    {"entity_id": {"$in": list(chapter_card.related_characters)}},
                ]
            },
            workspace_id=workspace_id,
        )

    # v1.1 wave 3 s6(E1):chapter_summary 检索维度。摘要文本长,top_k 单独封顶
    # ``_CHAPTER_SUMMARY_TOP_K``(并尊重 caller 更小的 top_k,故取 min)。
    summary_hits = retrieve_memories_for_chapter(
        chapter_card,
        vector_store=vector_store,
        collection=DEFAULT_COLLECTION,
        top_k=min(top_k, _CHAPTER_SUMMARY_TOP_K),
        where={"entity_type": "chapter_summary"},
        workspace_id=workspace_id,
        before_chapter_seq=before_chapter_seq,
    )
    immediate_previous_id = _immediate_previous_chapter_id(chapter_card.chapter_id)
    if immediate_previous_id is not None:
        summary_hits = [
            hit
            for hit in summary_hits
            if not _is_summary_for_chapter(hit, immediate_previous_id)
        ]

    # P4-B-05:OBSERVABILITY §2.4 ``retrieval_completed`` 事件。``after_rerank_count``
    # 在本调用栈未做 rerank(P3-B-08 reranker 仅在内部专项链路启用,此处不接),
    # 字面字段 = ``candidates_count``。``top_similarity`` = ``1 - min(distance)``;
    # 命中为空时 None。
    all_hits = list(fact_hits) + list(character_hits) + list(summary_hits)
    candidates_count = len(all_hits)
    top_similarity: float | None = (
        round(1.0 - min(h.distance for h in all_hits), 4) if all_hits else None
    )
    _log.info(
        "retrieval_completed",
        chapter_id=chapter_card.chapter_id,
        query_summary=chapter_card.title,
        candidates_count=candidates_count,
        after_rerank_count=candidates_count,
        top_similarity=top_similarity,
    )

    lines: list[str] = ["## 检索记忆"]

    if not fact_hits and not character_hits and not summary_hits:
        lines.append("")
        lines.append("(暂无相关检索)")
        return "\n".join(lines)

    if fact_hits:
        lines.append("")
        lines.append("### fact(关键事实)")
        for hit in fact_hits:
            lines.append(f"- {hit.content}")

    if character_hits:
        lines.append("")
        lines.append("### character(出场角色档案)")
        for hit in character_hits:
            lines.append(f"- {hit.content}")

    if summary_hits:
        lines.append("")
        lines.append("### chapter_summary(相关历史章节摘要)")
        for hit in summary_hits:
            lines.append(f"- {hit.content}")

    return "\n".join(lines)


def _immediate_previous_chapter_id(chapter_id: str) -> str | None:
    """按原数字宽度返回精确上一章 ID；首章/非标准 ID 不裁剪。"""

    if not chapter_id.startswith("ch-"):
        return None
    raw_number = chapter_id.removeprefix("ch-")
    if not raw_number.isdigit():
        return None
    number = int(raw_number)
    if number <= 1:
        return None
    return f"ch-{number - 1:0{len(raw_number)}d}"


def _is_summary_for_chapter(
    hit: RetrievedMemory,
    chapter_id: str,
) -> bool:
    """只认 retriever 元数据中的精确 stable chapter ID；缺元数据保守保留。"""

    metadata_chapter_id = hit.metadata.get("chapter_id")
    metadata_entity_id = hit.metadata.get("entity_id")
    return metadata_chapter_id == chapter_id or metadata_entity_id == chapter_id


def _build_relationship_lock_section(
    *,
    db_session: Session | None,
    chapter_card: ChapterCard,
) -> str:
    """构造 user message 的"## 角色关系硬锁"段(v2 W3.5 writer 最小闭环)。

    复用大纲层 ``build_relationship_lock_lines`` 的同源渲染规则,但输入来自
    SQLite ``Character.relationships``(由 outline ``relationships_brief`` commit
    而来)。只注入本章相关关系:

    - POV 角色的关系可指向卡外角色(上司 / 恩师 / 叔伯等关系链事实);
    - 非 POV 的 related 角色只注入与本章 POV/related 集合内部的关系,避免把侧角
      与卡外人物的大量关系带进正文上下文。
    """
    if db_session is None:
        return ""

    relevant_ids: list[str] = []
    if chapter_card.pov_character:
        relevant_ids.append(chapter_card.pov_character)
    for cid in chapter_card.related_characters:
        if cid not in relevant_ids:
            relevant_ids.append(cid)
    if not relevant_ids:
        return ""

    relevant_set = set(relevant_ids)
    records: dict[str, RelationshipLockCharacter] = {}

    def _ensure_name_record(character_id: str) -> None:
        if character_id in records:
            return
        character: Character | None = get_character(db_session, character_id)
        name = character.name if character is not None else character_id
        records[character_id] = RelationshipLockCharacter(
            character_id=character_id,
            name=name,
            relationships_brief={},
        )

    for character_id in relevant_ids:
        character: Character | None = get_character(db_session, character_id)
        if character is None:
            continue
        relation_items: dict[str, str] = {}
        for other_id, relation in character.relationships.items():
            rel_text = (relation or "").strip()
            if not rel_text:
                continue
            if (
                character_id != chapter_card.pov_character
                and other_id not in relevant_set
            ):
                continue
            relation_items[other_id] = rel_text
            _ensure_name_record(other_id)
        records[character_id] = RelationshipLockCharacter(
            character_id=character.character_id,
            name=character.name,
            relationships_brief=relation_items,
        )

    lines = build_relationship_lock_lines_from_records(records.values())
    if not lines:
        return ""
    header = (
        "## 角色关系硬锁（仅身份定义）与关系发展参考\n\n"
        "以下是原有关系资料，可能同时含有身份和发展设想。**已经确立的关系身份定义"
        "不能写错**:上司就是上司,恩师就是恩师,叔伯就是叔伯;不要把既有关系统一改成"
        "父子 / 兄弟 / 陌生人,也不要把关系对象拆成另一个新人物。发展设想不代表"
        "本章已发生，不构成新的必达或角色已知事实；让变化随本章动作发生，"
        "不要把人物简介或后续走向复述为旁白。\n\n"
    )
    return header + "\n".join(lines)


def _build_character_state_section(
    *,
    db_session: Session | None,
    chapter_card: ChapterCard,
) -> str:
    """构造 user message 的"## 角色截止状态"段(G1,v1.1 wave 4 session 1)。

    防"断臂复活":对 ``pov_character`` + ``related_characters``,**显式 SQL
    lookup** 各角色 ``state_timeline`` 中截止上一章(全局序 < 本章)的最新一条
    状态快照,渲染身体状态 / 伤势 / 持有物品 / 关系注入 prompt——**完全绕开
    embedding**(向量检索可能把过期状态检索出来)。

    - ``db_session=None`` / 本章 chapter_id 非 ``ch-NNNN`` 模式 / 无任何角色有
      截止上一章的状态 → 返回空字符串(``_build_user_message`` 据此省略该段;
      首章 / 角色首次出场场景)。
    """
    if db_session is None:
        return ""
    current_seq = chapter_seq_from_id(chapter_card.chapter_id)
    if current_seq is None:
        return ""

    char_ids: list[str] = []
    if chapter_card.pov_character:
        char_ids.append(chapter_card.pov_character)
    for cid in chapter_card.related_characters:
        if cid not in char_ids:
            char_ids.append(cid)

    blocks: list[str] = []
    for character_id in char_ids:
        character: Character | None = get_character(db_session, character_id)
        if character is None:
            continue
        snapshot = latest_state_snapshot_before(character.state_timeline, current_seq)
        if snapshot is None:
            continue
        lines = format_state_snapshot_lines(snapshot)
        if not lines:
            continue
        body = "\n".join(f"  - {line}" for line in lines)
        blocks.append(
            f"### {character.name}({character_id})"
            f"(截止第 {snapshot.as_of_chapter} 章)\n{body}"
        )

    if not blocks:
        return ""
    header = (
        "## 角色截止状态(CHARACTER STATE AS OF PREVIOUS CHAPTER)\n\n"
        "以下是出场角色**截止上一章的累积状态**(身体 / 伤势 / 持有物品 / 关系)。"
        "该段是从已批准正文抽取的**派生记忆**；若本消息包含权威世界契约，"
        "它的优先级低于该契约。如二者冲突，必须忽略冲突状态并按权威规则写，不能用后出现的状态"
        "覆盖上方规则。‘上一章末仍处于某状态’只说明当时状态，不自动证明其永久"
        "或不可逆。"
        "本章正文**必须与之保持连贯**:已断的肢体不会无故复原,已失去的物品不会"
        "凭空出现,已确立的关系不会无故倒退——除非本章正文给出明确的剧情解释"
        "(治疗 / 失而复得 / 关系转折)。\n\n"
    )
    return header + "\n\n".join(blocks)


def _build_identity_section(
    *,
    db_session: Session | None,
    chapter_card: ChapterCard,
) -> str:
    """构造 user message 的"## 出场角色身份档案"段(A3,v1.1 wave 6 session 2)。

    治 B1 暴露的双界身份 bug:① 沈舟现实=心理医生却被写成"刑警"(身份写串);
    ② 白露现实侧身份整章丢失(只写梦境侧)。机制:对 ``pov_character`` +
    ``related_characters``,**显式 SQL lookup** 各角色 ``identity_facets``,把
    "该角色在每个情境下分别是什么身份"作**固定参考事实**注入 prompt——这是
    who's who 的事实锚(不是新的写作规则,不加重 writer 负担,对齐
    [wave6_design](../../../docs/plan/v1_1_wave6_design.md) §0 原则二"约束上移")。

    - 只渲染 ``identity_facets`` 非空的角色(无多重身份的简单角色跳过 → 普通
      角色完全不受影响)。
    - ``db_session=None`` / 所有出场角色都无 identity_facets → 返回空字符串
      (``_build_user_message`` 据此省略该段)。
    """
    if db_session is None:
        return ""

    char_ids: list[str] = []
    if chapter_card.pov_character:
        char_ids.append(chapter_card.pov_character)
    for cid in chapter_card.related_characters:
        if cid not in char_ids:
            char_ids.append(cid)

    blocks: list[str] = []
    for character_id in char_ids:
        character: Character | None = get_character(db_session, character_id)
        if character is None or not character.identity_facets:
            continue
        facet_lines = "、".join(
            f"{f.context}={f.identity}" for f in character.identity_facets
        )
        blocks.append(f"### {character.name}({character_id})\n  - {facet_lines}")

    if not blocks:
        return ""
    header = (
        "## 出场角色身份档案(CHARACTER IDENTITY — 固定事实,不可写串 / 漏写)\n\n"
        "以下出场角色拥有**多重情境身份**(同一角色在不同情境下是不同身份)。"
        "本章正文**必须严格按此身份**写每个角色:\n"
        "- **绝不写串**:不要把某角色在某情境的身份,写成另一个身份 / 另一个角色的"
        "职业(例:现实身份是医生的角色,不能在现实场景被叙述 / 称呼成警察)。\n"
        "- **按本章情境取对应那一面**:本章发生在哪个情境(现实 / 梦境 / …),"
        "就用该角色在该情境下的身份;跨情境时身份随之切换,但**不能张冠李戴**。\n\n"
    )
    return header + "\n\n".join(blocks)


def _build_related_plot_thread_section(
    *,
    db_session: Session | None,
    chapter_card: ChapterCard,
) -> str:
    """渲染章节卡已选伏笔的身份、来源和本章动作边界。"""
    if db_session is None or not chapter_card.related_plot_threads:
        return ""

    blocks: list[str] = []
    time_scoped = (chapter_card.narrative_context is not None
                   and chapter_card.narrative_context.story_intent.version >= 6)
    for thread_id in dict.fromkeys(chapter_card.related_plot_threads):
        row = db_session.get(PlotThreadORM, thread_id)
        if row is None or row.workspace_id != chapter_card.workspace_id:
            continue
        origin = getattr(row, "origin", "outline")
        if origin == "emergent":
            marker = f"[临时伏笔收口:{thread_id}]"
            source_evidence = getattr(row, "source_evidence", None) or "(无)"
            selected_for_payoff = any(
                must.strip().startswith(marker) for must in chapter_card.must_achieve
            )
            lines = [
                f"### 临时伏笔:{row.title}({thread_id})",
                f"- 当前状态:{row.status}",
                f"- 悬念内容:{row.description}",
                f"- 入库正文证据:{source_evidence}",
                f"- 常规提醒范围:{getattr(row, 'expiry_policy', 'volume_end')}",
                "- 本章处理:"
                + (
                    f"章节卡已用 {marker} 明确选择收口；必须把 must 中的行动与"
                    "结果写实，并让读者得到答案。"
                    if selected_for_payoff
                    else "未写收口 marker；只可自然承接，不得擅自宣布收口。"
                ),
            ]
        elif time_scoped:
            # 不猜混合原文中哪一句已经发生；只使用数据库确知的来源时间与状态。
            lines = [
                f"### 长线参考:{row.title}({thread_id})",
                f"- 当前记录状态:{row.status}",
                f"- 埋设登记章节:{row.planted_chapter or '(全书规划，尚无正文来源章)'}",
                f"- 记录最近更新章节:{row.last_updated_chapter or '(原始全书规划)'}",
                f"- 尚属计划的兑现章节:{row.planned_payoff or '(未排定)'}",
                f"- 已登记兑现章节:{row.actual_payoff or '(尚未登记兑现)'}",
                "- 有源规划／历史混合材料（保留原文，未逐句判定实际完成）:",
                row.description,
                "- 使用范围:描述中的本章指其来源章节；全书规划和历史记录都不是当前章待办。"
                "本章实际事件以章节卡事实合同为准，不重演旧发现或旧生活交代。",
            ]
        else:
            lines = [
                f"### 大纲/人工主伏笔:{row.title}({thread_id})",
                f"- 当前状态:{row.status}",
                f"- 规划内容:{row.description}",
                f"- 预定收口:{row.planned_payoff or '(由大纲/章节计划另行安排)'}",
                "- 本章处理:按章节卡 must 与原大纲节奏推进；临时伏笔规则不改变"
                "本主伏笔的安排。",
            ]
        blocks.append("\n".join(lines))

    if not blocks:
        return ""
    if time_scoped:
        return ("## 有来源时间的长线材料（历史与计划参考）\n\n"
                "状态、已登记兑现与计划兑现分开。混合描述不自动证明事件发生；"
                "未标即时的旧动作不作为本章任务。\n\n" + "\n\n".join(blocks))
    return (
        "## 章节卡关联伏笔（按稳定 ID 精确读取）\n\n"
        "大纲/人工主伏笔与正文临时伏笔是两层：前者按既有全书规划推进；后者"
        "只有章节卡显式选择时才在本章收口。不得互相替换，也不要为了清理临时"
        "条目打乱本章主任务。\n\n" + "\n\n".join(blocks)
    )


#: G2:planted_chapter 原文片段注入长度上限(字符)。对照 continuity prev_chapter_tail
#: 500-800 字,取更短的 400 字头部片段——回响只需唤起原场景意象,非全文复现;过长会
#: 稀释 prompt 关键约束。超长截断 + 省略号。
_CALLBACK_EXCERPT_MAX_CHARS = 400


def _build_callback_section(
    *,
    db_session: Session | None,
    workspace_paths: WorkspacePaths | None,
    chapter_card: ChapterCard,
) -> str:
    """构造 user message 的"## 伏笔 / 隐喻回响"段(G2,v1.1 wave 4 session 2)。

    根治"第 3 章暴雨被第 45 章洗澡向量检索覆盖":对 ``chapter_card.callback_to``
    列出的每个 thread_id,**显式 SQL lookup PlotThread by ID**(完全绕开 embedding),
    渲染伏笔标题 + 类型 + 描述 + ``planted_chapter`` 原文片段(头部
    ``_CALLBACK_EXCERPT_MAX_CHARS`` 字)注入 prompt。

    - ``callback_to`` 空 / ``db_session=None`` → 返回空字符串(``_build_user_message``
      据此省略该段)。
    - 某 thread_id 查不到 / 跨工作区(物理隔离硬约束)→ 跳过该条。
    - ``workspace_paths=None`` / planted_chapter 为 None / final.md 不存在 →
      仍注入伏笔元信息(标题 + 类型 + 描述),只是缺原文片段(优雅降级)。
    """
    if db_session is None or not chapter_card.callback_to:
        return ""

    blocks: list[str] = []
    for thread_id in chapter_card.callback_to:
        row = db_session.get(PlotThreadORM, thread_id)
        if row is None or row.workspace_id != chapter_card.workspace_id:
            continue
        lines = [
            f"### {row.title}({thread_id},类型:{row.thread_type})",
            f"- 伏笔描述:{row.description}",
        ]
        excerpt = _read_planted_excerpt(
            workspace_paths=workspace_paths,
            planted_chapter=row.planted_chapter,
        )
        if excerpt:
            lines.append(f"- 埋设章节({row.planted_chapter})原文片段:")
            lines.append(excerpt)
        blocks.append("\n".join(lines))

    if not blocks:
        return ""
    header = (
        "## 伏笔 / 隐喻回响(CALLBACK TO EARLIER FORESHADOWING / METAPHOR)\n\n"
        "以下是本章**作者显式标注要呼应**的伏笔 / 隐喻(按 thread_id 精确锁定,"
        "非向量近似检索)。本章正文应**自然回响**这些早先埋下的线索 / 意象——与"
        "原始场景形成呼应、推进或兑现;**不要**生硬复述原文,用情节 / 意象 / 人物"
        "感受自然唤起。\n\n"
    )
    return header + "\n\n".join(blocks)


#: I1(v1.1 wave 4 session 3):工作区风格指南文件名(``WorkspaceService.create``
#: 据所选 style_template 写入工作区 ``prompts/`` 下)。
_STYLE_GUIDE_FILENAME = "style_guide.md"
#: 风格指南注入 prompt 的长度上限(字符)。预设模板正文约 600-900 字,留足余量;
#: 超长截断防异常大文件稀释 prompt。
_STYLE_GUIDE_MAX_CHARS = 4000


def _build_style_guide_section(
    *,
    workspace_paths: WorkspacePaths | None,
    require_selected_guide: bool = False,
) -> str:
    """构造 user message 的"## 写作风格指南"段(I1,v1.1 wave 4 session 3)。

    读工作区 ``prompts/style_guide.md``(创建工作区时据所选 ``style_template``
    生成)注入 prompt,作为本书**全程一致**的写作风格锚点。

    - ``workspace_paths=None`` / 文件不存在(未选模板)/ 读失败 / 内容为空 →
      返回空字符串(``_build_user_message`` 据此省略该段,Writer 退化为既有
      前章风格指纹跨章基线隐式控制,向后兼容)。
    - 风格指南**全程一致注入**(draft / rewrite / revise),与 G10 前章视角基线、
      G5 章间风格漂移护栏方向一致(强化跨章一致性,非对冲):它给的是定性风格
      引导,不下硬性数字指令。超长截断到 ``_STYLE_GUIDE_MAX_CHARS`` 字。
    """
    if workspace_paths is None:
        return ""
    guide_path = workspace_paths.prompts_dir / _STYLE_GUIDE_FILENAME
    selected = (
        require_selected_guide and workspace_paths.meta_json.is_file()
        and read_meta(workspace_paths.meta_json).style_template is not None
    )
    try:
        text = guide_path.read_text(encoding="utf-8").strip()
    except OSError as exc:
        if selected:
            raise WriterDraftAuthorityError("已选风格指南缺失或不可读，不能静默忽略模板") from exc
        return ""
    if not text:
        if selected:
            raise WriterDraftAuthorityError("已选风格指南为空，不能静默忽略模板")
        return ""
    if len(text) > _STYLE_GUIDE_MAX_CHARS:
        text = text[:_STYLE_GUIDE_MAX_CHARS] + "……"
    header = (
        "## 写作风格指南(WRITING STYLE GUIDE — 全书一致风格锚点)\n\n"
        "以下是本书的写作风格模板,**贯穿全书每一章**。本章正文应贴合该风格的"
        "题材调性 / 叙事密度 / 修辞 / 对白比例 / 情感色调等基调;**风格的跨章"
        "一致性优先**(不要某章突然偏离整体风格)。这是定性引导,具体字数 / 节点"
        "数仍以章节卡为准。\n\n"
    )
    if require_selected_guide:
        header = (
            "## 写作风格指南（题材底色）\n\n"
            "采用下面指南中与本书方向相容的风格；故事定位中的明确偏好优先，"
            "篇幅遵循章节卡。模板不是逐条展示任务。\n\n"
        )
    return header + text


#: M-craft(v1.1 wave 5 session 2):引擎内置**通用反套话写作手法纪律**。
#: **无条件每章注入**(draft / rewrite / revise 全程),book-agnostic —— 不读
#: 工作区配置、不依赖所选 style_template,对任何题材任何书都生效。
#:
#: 内容**只放近普世内核**(2026-06-06 维护人"三层归位"更正后定 —— 见下"为何
#: 不放整体 showing 密度"):
#: ① 避免无效重复与公式化句式；自然回返不强制新增事件或风险。
#: ② **关键戏别偷懒**:吃重时刻别用抽象词一笔带过,给具体表现;**过场可简笔**
#:    —— 这是 postmortem §5"写具体"活性成分的**近普世内核**(限定关键戏,**非
#:    处处多写**)。
#: ③ **克制解释性补刀** + 跨书通用模型口癖清单(瞳孔 / 醒来连招 / 死寂 等 qwen
#:    **跨书固定**毛病,非某本书特有)。
#:
#: **为何不放整体 showing 密度**("多写光影 / 气味 / 质地"式指令):那是**题材
#: 相关**,归 ``style_template``(``style_template.py`` 第②③④维已编码:爽文模板=
#: 朴素 / 对话主导 = **精简 showing**;文学模板=华丽 / 描写主导 = **丰富 showing**)。
#: 硬编码"多写细节"进引擎会**误伤爽文**(逼朴素 / 快节奏的爽文处处铺陈,毁节奏 +
#: 违背其风格意图)。**也不放**本书专属套话(偏头痛 / 锦衣卫黑话等,归工作区级
#: ``style_guide``)。整体密度配比由 M2 reviewer **相对 style_template 度量**(右栏),
#: 关键戏 per-scene 标注由 card 上游(未来 w5 session),与本静态段(近普世内核)三层分工。
_CRAFT_SECTION = (
    "## 本章文学执行（SOFT；不覆盖 HARD）\n\n"
    "- 关键情绪和判断用可见动作、对白、感官或选择呈现，让人物的反应自然展开。"
    "过场可以简笔。\n"
    "- 对照重构句只留给真实纠错或事实反转；避免短距离复用「不是/并非……而是……」"
    "「没有……只有……」「与其说……不如说……」「看似……实则……」"
    "「问题不在……而在……」。短反例：『他不是害怕，而是谨慎』→"
    "『他把门闩又推了一遍，手没有离开刀柄』。\n"
    "- 结合上方 system 中的性格、当章动机和关系，让主要角色在先注意什么、想保住什么、"
    "不肯说什么、做何选择与付何代价上可区分；不要让所有人都按观察、分析、汇报的"
    "办案路径反应，也不要靠统一添加心跳、落泪或创伤回忆制造温度。\n"
)


def _build_craft_section() -> str:
    """构造 user message 的唯一通用“本章文学执行”段。

    引擎内置的**通用反套话 craft 指南**,**无条件**注入每章 user message
    (draft / rewrite / revise 全程),book-agnostic —— 不读工作区配置、不依赖
    所选 ``style_template``,对任何题材任何书都生效。

    与 ``_build_style_guide_section``(I1,工作区级风格基调,可空)**互补且独立**:

    - style_guide 给"**这本书**选定的风格模板"(题材调性 / 修辞密度 / 对白比例…),
      工作区级、文件驱动、未选模板时为空。
    - 本段给"**任何书**都该遵守的写作手法"(反复读机 / 关键戏别偷懒 / 克制补刀
      等近普世内核;**整体 showing 密度归 style_template,不在此** —— 见 ``_CRAFT_SECTION``
      常量注释的"三层归位"),引擎级、硬编码、永远注入。

    v1.1 w5-s9 / M3(深层 AI 味文笔)**三方法真 LLM 实测全 parked**,故本段
    当时保留 w5-s2 方向 —— craft 禁令(TELL)/书内自锚范本(SHOW)/frequency_penalty
    (解码)各撞不同反噬(写僵 / 污染 / 字数失控),深层质感是模型天花板(非可点名的
    具体表层套话,本段只治后者)。详 postmortem ``docs/decisions/v1_1_w5_s9_m3_postmortem.md``。

    机制依据:v1.1 w5-s1 真 LLM A/B + 盲读实证
    (``docs/decisions/v1_1_w5_s1_m1_postmortem.md`` §3-§5)—— **writer 级**注入
    craft 方向把目标套话降 67~91%、盲读可读性 5→7,且非"打地鼠"(非目标套话也
    降)、非"为避词写僵"(确认是真写具体了)。**走范式 ②(writer 级注入,与 A3
    同型),不走 M1 的"下游检测→卡片注入"覆辙(注入点错位,见 postmortem §2)。**

    Session D 把历史长说明收敛为“一句话要求 + 一个短反例”，并增加公式化对照句
    与人物反应同质化的写前提醒。它仍是 SOFT 文学指导，不进入约束计划。
    V4.1 提示适配删除“演完即停”和“一次”限制；重复与展开的统一判断放在
    当前首稿提示中，避免共享段再次要求每次回返都增加新的剧情变化。

    返回固定静态串(永不为空),由 ``_build_user_message`` 无条件 append。
    """
    return _CRAFT_SECTION


def _read_planted_excerpt(
    *,
    workspace_paths: WorkspacePaths | None,
    planted_chapter: str | None,
) -> str:
    """读 ``planted_chapter`` 的 ``final.md`` 头部片段(G2 helper)。

    缺 paths / planted_chapter / final.md 不存在 / 读失败 → 返回空字符串(caller
    据此优雅降级为只注入伏笔元信息)。超长截断到 ``_CALLBACK_EXCERPT_MAX_CHARS``
    字 + 省略号。
    """
    if workspace_paths is None or not planted_chapter:
        return ""
    final_path = workspace_paths.chapter_dir(planted_chapter) / "final.md"
    try:
        text = final_path.read_text(encoding="utf-8").strip()
    except OSError:
        return ""
    if len(text) > _CALLBACK_EXCERPT_MAX_CHARS:
        text = text[:_CALLBACK_EXCERPT_MAX_CHARS] + "……"
    return text


def _build_voice_section(
    *,
    db_session: Session | None,
    related_characters: list[str],
) -> str:
    """构造 system message 的"## 出场角色声音档案"段(P2-B-10)。

    - ``db_session=None`` → 返回空字符串(P1 路径,system 不附 voice 段)。
    - ``db_session`` 非空时,按 ``related_characters`` 列表查每个角色的
      ``voice_profile``;查不到的角色(``related_characters`` 含尚未入库
      ID)跳过——P2 阶段允许"出场角色 ID 在章节卡里登记但 SQLite 还没
      落库"的情况(P2-B-13 静态资产导入工具未跑 / 抽取尚未触发等)。
    - 任一角色查到都附段;全部查不到返回空字符串(等同 P1 路径)。
    - voice_profile 6 字段全展开:``catchphrases`` / ``forbidden_phrases``
      / ``tone`` / ``sentence_style`` / ``self_address`` / ``addresses_others``
      ;空集合字段渲染成"无",空 dict 渲染成"无"。
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

    return "## 出场角色声音档案\n\n" + "\n\n".join(voice_blocks)


def _build_personality_section(
    *,
    db_session: Session | None,
    related_characters: list[str],
    chapter_card: ChapterCard | None = None,
) -> str:
    """完整渲染已选人物材料；相关性仍筛选，体积交既有整请求预算预检。

    只使用既有 ``Character`` 字段，不读取或暴露 ``secrets``，也不把人物材料升级为
    新 must。POV 会由 caller 显式加入 ID 列表，防止章节卡 related 列表异常时漏掉主角。
    """
    if db_session is None or not related_characters:
        return ""

    ordered_character_ids = list(dict.fromkeys(related_characters))
    if chapter_card is not None:
        priority_ids = _ordered_prompt_character_ids(chapter_card)
        requested = set(ordered_character_ids)
        ordered_character_ids = [
            character_id for character_id in priority_ids if character_id in requested
        ] + [
            character_id
            for character_id in ordered_character_ids
            if character_id not in priority_ids
        ]

    characters: list[Character] = []
    for character_id in ordered_character_ids:
        character = get_character(db_session, character_id)
        if character is not None:
            characters.append(character)
    current_ids = {character.character_id for character in characters}
    current_names = {character.name for character in characters}
    name_by_id = {character.character_id: character.name for character in characters}

    blocks: list[str] = []
    for character in characters:
        appearance = character.appearance.strip()
        background = character.background.strip()
        abilities = [ability.strip() for ability in character.abilities if ability.strip()]
        personality = character.personality.strip()
        short_goals = [
            goal.strip() for goal in character.goals_short_term if goal.strip()
        ]
        long_goals = [
            goal.strip() for goal in character.goals_long_term if goal.strip()
        ]
        relationship_candidates: list[tuple[str, str]] = []
        for other, relation in character.relationships.items():
            other_key = str(other).strip()
            relation_text = str(relation).strip()
            if not other_key or not relation_text:
                continue
            if other_key not in current_ids and other_key not in current_names:
                continue
            if other_key in {character.character_id, character.name}:
                continue
            relationship_candidates.append(
                (
                    other_key,
                    f"{name_by_id.get(other_key, other_key)}:{relation_text}",
                )
            )
        relationship_priority = _relationship_counterpart_priority(
            chapter_card=chapter_card,
            character_id=character.character_id,
        )
        priority_index = {
            character_id: index
            for index, character_id in enumerate(relationship_priority)
        }
        relationship_candidates.sort(
            key=lambda item: priority_index.get(item[0], len(priority_index))
        )
        relationships = [
            rendered
            for _, rendered in relationship_candidates
        ]
        if not any((appearance, background, abilities, personality,
                    short_goals, long_goals, relationships)):
            continue
        lines = [f"### {character.name}({character.character_id})"]
        if appearance:
            lines.append(f"- 外貌与基础特征（初始资料，当前状态优先）:{appearance}")
        if background:
            lines.append(f"- 已有背景（作者参考，不等于视角人物已知）:{background}")
        if abilities:
            lines.append("- 已有能力（不要求本章全部使用）:" + "；".join(abilities))
        if personality:
            lines.append(f"- 性格底色:{personality}")
        if short_goals:
            lines.append("- 当前短期目标:" + "；".join(short_goals))
        if long_goals:
            lines.append(
                "- 长期动机（上游 motivation）:" + "；".join(long_goals)
            )
        if relationships:
            lines.append("- 本章出场关系:" + "；".join(relationships))
        block = "\n".join(lines)
        blocks.append(block)

    if not blocks:
        return ""

    return (
        "## 出场角色性格与可用动机（SOFT 演出材料）\n\n"
        "先用当前欲望定位角色此刻要什么，再从关系原文演出其保留与关系动作；"
        "性格底色只决定怎么做，不得替代当章目标。不得读取或猜测 secrets，不得"
        "新造背景，也不得覆盖章节卡、核心状态或 HARD 合同。长期动机和关系发展"
        "是作者可用的创作背景，不代表主视角已知，也不要求在本章解释人物全貌。"
        "让它们影响人物的注意、顾忌和选择，不把资料原句或未来走向写成旁白。\n\n"
        + "\n\n".join(blocks)
    )


def _ordered_prompt_character_ids(card: ChapterCard) -> list[str]:
    """高价值章按 actor/counterforce 优先，其余保持既有 POV/卡片顺序。"""

    if card.narrative_context is not None:
        return active_character_ids(card)
    dramatic_ids: list[str] = []
    if card.dramatic_source is not None:
        beat = card.dramatic_source.beat_contract
        dramatic_ids.extend([beat.actor_id, beat.counterforce_id])
    return list(
        dict.fromkeys(
            [
                *dramatic_ids,
                card.pov_character,
                *card.related_characters,
            ]
        )
    )


def _relationship_counterpart_priority(
    *,
    chapter_card: ChapterCard | None,
    character_id: str,
) -> list[str]:
    """把本章 actor/counterforce 的互相施压关系排在普通关系之前。"""

    if chapter_card is None or chapter_card.dramatic_source is None:
        return []
    beat = chapter_card.dramatic_source.beat_contract
    if character_id == beat.actor_id:
        return [beat.counterforce_id]
    if character_id == beat.counterforce_id:
        return [beat.actor_id]
    return [beat.actor_id, beat.counterforce_id]


def _format_voice_profile(character: Character) -> str:
    """单个角色的 voice_profile 渲染块。"""
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
    sentence_style = (
        f"- sentence_style(句式):{voice.sentence_style}\n"
        if voice.sentence_style.strip() and voice.sentence_style != voice.tone else ""
    )
    return (
        f"### {character.name}({character.character_id})\n"
        f"- tone(语气):{voice.tone}\n"
        + sentence_style +
        f"- self_address(自称):{voice.self_address}\n"
        f"- addresses_others(对他人称呼):{addresses_block}\n"
        f"- catchphrases(口头禅):{catchphrases_block}\n"
        f"- forbidden_phrases(禁止用语):{forbidden_block}"
    )
