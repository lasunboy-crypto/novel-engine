"""衔接桥语义判定 LLM agent(v1.0.x wave 3 Commit D 引入)。

职责:把 ``ChapterCard.continuity_context`` 衔接桥判定从 ``validator_pipeline``
字面 substring 比对器解耦,**LLM-based 语义判定**接管 PROJECT_SPEC §4.3 全 3 条
规则。最多 3 次条件 gated 串行 LLM 调用(Q1 / Q2 / Q3 独立 yes/no/uncertain 询问),
综合后给出 ``ContinuityValidationResult{passed, failed_hard_checks, soft_warnings,
llm_calls_count, skipped, fail_open}``。caller(``chapter_orchestrator``)按结果:
HARD fail(Q1 / Q2，以及 clock contract v1 的 frozen typed 书内时间 Q3)→ 覆盖
``review.verdict="rewrite"`` + 合并 ``failed_constraints``；legacy Q3 仍只合并
``review.suggestions``，不动 verdict。

设计要点
========

1. **复刻 must_achieve_validator 同型架构**(P7-B-06):每次 LLM 只回答一个
   yes/no/uncertain 二元问题,避免"1 个 LLM 同时干 3 件事"导致综合输出不稳
   (P7-acceptance-rerun2 实测教训)。

2. **§4.3 全 3 条范围**(2 HARD + 1 SOFT;维护人 2026-05-20 拍板):

   - Q1 unfinished_actions(HARD §4.3 第 1 条):
     ``continuity_context.unfinished_actions`` 非空 → 询问"本章开头是否在语义上
     承接了这些未完成动作"。
   - Q2 scene_continuation(HARD §4.3 第 2 条):
     ``scene_continuation == "same"`` → 询问"本章开头是否还在上一章同一物理空间"。
   - Q3 time_gap(legacy SOFT；clock contract v1 条件 HARD):legacy 卡沿用单章
     ``time_gap`` 语义；新卡询问正文是否直接违反 frozen exact/bounds/closed relation，
     并以同一 ``ExecutableConstraintPlan`` 身份作 HARD 裁决。无法证明时不得拒绝。

3. **沿用 local_qwen_reviewer profile**(对齐 must_achieve_validator 决策 2):
   路由 agent_name ``"continuity_validator"`` → ``agent_routing.continuity_validator``
   → ``config.yaml`` 字面 ``local_qwen_reviewer``(temp 0.3,context 16000,
   num_predict 2048,think:false)。**不新建第 3 个 profile**。

4. **整 validator skip 边界**(自决项,对齐 must_achieve_validator skip 同型):

   - 首章或 ``prev_chapter_tail`` 为空：沿用既有 skip，不为新合同增加调用；
     该类卡片的书内时钟由同一次 Setting Judge 复核。
   - 三 Q condition 全部不满足(无 unfinished_actions / scene_continuation 非 same
     / time_gap 是兜底值)→ skip(无判定对象)。

5. **分级故障语义**：legacy SOFT/Q1/Q2 沿用既有整体 fail-open；一旦本次调用包含
   冻结的书内时钟 HARD，其 setup、transport 或 parse 失败抛
   ``ContinuityTechnicalError``，不得把未判定冒充通过。

6. **uncertain 分轴处理**：Q1/Q2 仍 fail closed；typed book-time Q3 的
   ``uncertain`` 不能证明 frozen fact 被违反，只进入 ``soft_warnings``。
   legacy SOFT Q3 同样只告警。

7. **分段读取**：普通衔接问题仍只读正文前 1000 字；typed 书内时间 HARD 最多读取
   前 6000 字，避免章中后段的明确时间表述绕过约束。

8. **agent_role marker = ``continuity_validator``(无 chapter_id):
   user message 末尾追加 ``<!-- agent_role: continuity_validator -->`` marker
   供 MockLLMClient 识别(返默认 yes JSON 让既有 happy 路径不回归)。3 Q 共享
   同 marker,marker **不携带 chapter_id** — 与 must_achieve_validator 同型。

9. **HARD vs SOFT 字段分离**(自决项):
   ``ContinuityValidationResult.failed_hard_checks`` 与 ``soft_warnings``
   明确分离，contract v1 Q3 与其余 HARD 一样进入前者；SOFT 不影响 passed。

10. **SOFT warning 字串格式**(plan §1.5 字面):
    ``"时间表述与 time_gap 不一致(time_gap=<X>):<evidence>"`` —— 让
    Reviser/Writer 下次拿到具体证据。

v1.0.x wave 3 Commit D 留观项
==============================

- Q1 / Q2 / Q3 LLM 调用串行总耗时:Qwen 36B 估 5-15s 每次,~15-45s/章
  (加上 reviewer 评分 + must_achieve_validator)整章 cycle 增 30-40s。v1.1
  可考虑 3 Q 并行调用(threading.Thread 或 asyncio)。
- 与 ``validator_pipeline.check_unfinished_actions_continuation`` /
  ``check_scene_continuation_location``(substring 字面比对器)语义关系:
  本 agent **接管** §4.3 全 3 条判定,字面比对器在 v1.0.x Commit D 起从
  ``validator_pipeline`` results 中**移除调用**(对齐 A2-02 同型 — 函数本身
  保留 ``src/core/rules/validators.py``)。
- v1.1 决策 5=B 双层 Qwen+Claude 升级时,本 validator 也走同型 escalation。
- legacy Q3 继续 SOFT；只有代码可解析且已冻结为 contract v1 的主时间线累计区间
  才升级 HARD，含混时间仍不升级。

v1.0.x wave 4 session 4 扩展 — Q4 chapter_card 自洽性
======================================================

Q4 验证 **本章 ``continuity_context`` 是否真实反映与上一章 ChapterCard 衔接**
(``prev_chapter_card.next_chapter_seed`` / ``prev_chapter_card.pov_character``
对应到本章 ``unfinished_actions`` / ``pov_from`` 的承接关系)。

- **type = SOFT**(对齐 Q3 同型,启动指令推荐字面):fail → 合并 SOFT warning
  到 ``review.suggestions``,不覆盖 verdict — Q4 容易被短续接 / 跳场景章误拒,
  HARD 风险大于收益。
- **prev_chapter_card 入参**:caller(``chapter_orchestrator._apply_continuity_validator``)
  按需拉上一章 ChapterCard SSOT 文件传入;None 时 Q4 自动 skip(condition gating)。
  **v1.0.x wave 4 session 4**:Q4 schema + prompt + mock + 单测层就位;
  orchestrator caller 实际拉 ``prev_chapter_card`` 留 v1.1 / wave 4 session 5 接入
  (本会话不动 orchestrator 签名,既有所有 caller 传 ``prev_chapter_card=None`` →
  Q4 自动 skip → 既有路径 100% 等价,零回归)。
- **condition gating**:非首章 + ``prev_chapter_card`` 非 None + ``next_chapter_seed``
  非空 OR ``pov_character`` 非空 → Q4 active。
- **数据段**:渲染 ``prev_chapter_card.next_chapter_seed`` + ``pov_character`` +
  本章 ``continuity_context.unfinished_actions`` + ``pov_from`` /``pov_to``
  4 字段做对照。

v1.1 wave 6 session 3 扩展 — Q5 角色到场连续性(B3)
====================================================

Q5 验证 **本章出场角色中、上一章不在场者是否有合理到场交代**(治 B1 30 章长跑
bug B5 陆星野突兀出现)。对齐 wave6_design §2 B3 行 + §0 第一原则"抓矛盾护留白":

- **type = SOFT**(对齐 Q3/Q4 + wave 6 全 WARN 默认):fail → 合并 SOFT warning
  到 ``review.suggestions``,不覆盖 verdict。HARD 升级留 wave 6 末约束预算实测。
- **代码标记(gating)+ LLM 复核**:本章 ``related_characters`` 与上一章 roster
  做 diff,出现"上一章不在场、本章出场"的候选角色才激活 Q5(代码粗筛);LLM
  读正文判断是否有合理到场交代(自然首次登场 / 被召来 / 进入场景 = yes,突兀
  出现零交代 = no)。降误伤靠 LLM 复核 + "拿不准判 yes"护留白。
- **condition gating**:prev_chapter_card 非 None + 上一章 roster 非空 + 本章存在
  "上一章不在场"的出场角色(三条全满足才激活,否则 skip)。
"""

from __future__ import annotations

import contextvars
import hashlib
import json
import re
from concurrent.futures import Executor
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
from src.core.models.chapter_card import (
    ChapterCard,
    ContinuityContext,
    chapter_seq_from_id,
)
from src.core.models.continuity_ledger import ProvenanceBoundContinuityCapsule
from src.core.models.review import ConstraintResult, stable_constraint_id
from src.core.routing.router import ModelRouter
from src.core.rules.prose_hard_contract import book_clock_rule_text

_log = structlog.get_logger(__name__)

_VALIDATOR_PROMPT_FILENAME = "continuity_validator_v1.md"

# 默认生成参数(legacy 路径;router 路径由 local_qwen_reviewer profile 决定:
# temp 0.3 / num_predict 2048 / context 16000)。
_DEFAULT_TEMPERATURE = 0.3
_DEFAULT_MAX_TOKENS = 300  # 单次只产 {"verdict": "...", "evidence": "..."} JSON

# 本章正文前 N 字字数(自决项,plan §3 第 2 条):衔接桥语义只在开头判定。
_CHAPTER_HEAD_CHARS = 1000
_HARD_CLOCK_CHARS = 6000

# 兜底 time_gap 字面集合(对齐 prompts/continuity_extractor.md 第 3 字段兜底规则):
# 这些值代表"无判定对象"或"无可比较的时间跨度",Q3 不发起 LLM 调用。
_TIME_GAP_SENTINELS = frozenset({"立即续接", "无(本章为首章)", ""})

# 沿用 must_achieve_validator 同型 LLM 输出清理(剥 ```json / ``` 包裹 + 尾部
# HTML 注释复读)。
_CODE_FENCE_RE = re.compile(
    r"^\s*```(?:json)?\s*\n(?P<body>.*?)\n```\s*$",
    re.DOTALL,
)
_TRAILING_HTML_COMMENT_RE = re.compile(
    r"\s*<!--.*?-->\s*$",
    re.DOTALL,
)


class ContinuityTechnicalError(RuntimeError):
    """明确主时间线 HARD 的技术判定失败；不得伪装成通过。"""

    def __init__(self, *, stage: str, cause: Exception) -> None:
        self.stage = stage
        self.cause_type = type(cause).__name__
        super().__init__(
            f"书内时钟连续性校验技术失败:stage={stage},cause={self.cause_type}"
        )


def uses_existing_q3_for_hard_book_clock(chapter_card: ChapterCard) -> bool:
    """本卡能否在不增加调用的前提下由既有 continuity Q3 判硬时钟。"""

    clock = chapter_card.book_clock_constraint
    cc = chapter_card.continuity_context
    return bool(
        clock is not None
        and clock.is_hard
        and chapter_seq_from_id(chapter_card.chapter_id) != 1
        and cc.prev_chapter_tail.strip()
        and cc.time_gap
        and cc.time_gap not in _TIME_GAP_SENTINELS
    )


@dataclass(frozen=True)
class ContinuityValidationResult:
    """validator 输出。

    HARD vs SOFT 分字段返回(自决项,plan §3 第 8 条):caller 按字段语义分流处置。

    - ``passed``:**只看 HARD**(Q1 / Q2)— 任一 HARD fail → ``False``;SOFT(Q3)
      fail 不影响 ``passed``(仍 ``True``)。skip / fail-open 路径 ``passed=True``。
    - ``failed_hard_checks``:Q1 / Q2 失败的标识列表:``"unfinished_actions"`` /
      ``"scene_same"``;caller 拼成 ``failed_constraints +=
      ["continuity: <name>"]``(prefix 让 LogsPage / metrics 能按维度聚合)。
    - ``soft_warnings``:Q3 失败时拼好的人类可读警告短句列表
      ``["时间表述与 time_gap 不一致(time_gap=<X>):<evidence>"]``;caller
      合并进 ``review.suggestions``。
    - ``llm_calls_count``:实际发起的 LLM 调用次数(0-3)。
    - ``skipped``:为 ``True`` 当且仅当整 validator skip(首章 / 空
      prev_chapter_tail / 3 Q 条件全不满足)— ``passed=True`` +
      ``llm_calls_count=0``。
    - ``fail_open``:为 ``True`` 当且仅当 LLM 调用抛异常或返非法 JSON 导致整
      validator fail-open — ``passed=True``,可能 ``llm_calls_count > 0``。
    """

    passed: bool
    failed_hard_checks: list[str] = field(default_factory=list)
    constraint_results: list[ConstraintResult] = field(default_factory=list)
    soft_warnings: list[str] = field(default_factory=list)
    soft_warning_details: list[dict[str, str]] = field(default_factory=list)
    llm_calls_count: int = 0
    skipped: bool = False
    fail_open: bool = False
    provider0_q4_substituted: bool = False
    q4_proof_hash: str = ""


@dataclass(frozen=True)
class Q4Provider0Proof:
    """Q4 是否能由已批准卡片与同源 ledger 纯代码证明。"""

    substituted: bool
    reason: str
    proof_hash: str = ""


def evaluate_q4_provider0_substitution(
    *,
    chapter_card: ChapterCard,
    prev_chapter_card: ChapterCard | None,
    continuity_capsule: ProvenanceBoundContinuityCapsule | None,
) -> Q4Provider0Proof:
    """只接受逐字同一的单一 seed/action 与未变化 POV；其余保留原 Q4。

    本函数不做关键词或语义猜测。clean 路径必须同时证明：两张卡已批准；
    当前卡绑定的 source/version/text/ledger hash 与 capsule 相同；前章唯一 seed
    逐字等于 ledger 唯一 unfinished action；章节边界 POV 未变化且五处 stable
    ID 完全相同。contradiction、POV change、seed absent、legacy 或 coverage
    漂移均返回 ``substituted=False``。
    """

    if prev_chapter_card is None:
        return Q4Provider0Proof(False, "legacy_or_missing_previous_card")
    if continuity_capsule is None:
        return Q4Provider0Proof(False, "legacy_or_missing_capsule")
    if (
        chapter_card.card_status != "approved"
        or chapter_card.card_approved_at is None
        or prev_chapter_card.card_status != "approved"
        or prev_chapter_card.card_approved_at is None
    ):
        return Q4Provider0Proof(False, "card_not_approved")
    if prev_chapter_card.chapter_id != continuity_capsule.provenance.chapter_id:
        return Q4Provider0Proof(False, "previous_card_source_mismatch")
    try:
        continuity_capsule.assert_matches_projection(
            frozen_provenance=chapter_card.continuity_ledger_provenance,
            prev_chapter_tail=chapter_card.continuity_context.prev_chapter_tail,
            unfinished_actions=(
                chapter_card.continuity_context.unfinished_actions
            ),
            pov_from=chapter_card.continuity_context.pov_from,
        )
    except ValueError:
        return Q4Provider0Proof(False, "capsule_projection_mismatch")

    seed = (prev_chapter_card.next_chapter_seed or "").strip()
    if not seed:
        return Q4Provider0Proof(False, "seed_absent")
    if continuity_capsule.unfinished_actions != (seed,):
        return Q4Provider0Proof(False, "seed_not_exact_single_ledger_action")

    cc = chapter_card.continuity_context
    pov_ids = (
        continuity_capsule.pov_character,
        prev_chapter_card.pov_character,
        cc.pov_from,
        cc.pov_to,
        chapter_card.pov_character,
    )
    if cc.pov_change or any(not value for value in pov_ids):
        return Q4Provider0Proof(False, "pov_change_or_missing")
    if len(set(pov_ids)) != 1:
        return Q4Provider0Proof(False, "pov_identity_mismatch")

    proof_payload = {
        "chapter_id": chapter_card.chapter_id,
        "previous_chapter_id": prev_chapter_card.chapter_id,
        "provenance": continuity_capsule.provenance.model_dump(mode="json"),
        "capsule_hash": continuity_capsule.capsule_hash,
        "seed": seed,
        "unfinished_actions": list(continuity_capsule.unfinished_actions),
        "pov_character": pov_ids[0],
        "pov_change": cc.pov_change,
    }
    proof_hash = hashlib.sha256(
        json.dumps(
            proof_payload,
            ensure_ascii=False,
            sort_keys=True,
            separators=(",", ":"),
        ).encode("utf-8")
    ).hexdigest()
    return Q4Provider0Proof(True, "exact_approved_card_ledger_identity", proof_hash)


class _ValidatorLLMPayload(BaseModel):
    """LLM 输出的中间模型(只校验 ``verdict`` + ``evidence``)。"""

    verdict: Literal["yes", "no", "uncertain"]
    evidence: str = Field(..., min_length=1, max_length=200)


_QType = Literal["hard", "soft"]


@dataclass(frozen=True)
class _QSpec:
    """单 Q 描述(供主流程统一调度 Q1 / Q2 / Q3)。

    - ``name``:HARD 时进 ``failed_hard_checks`` 列表的标识(``"unfinished_actions"``
      / ``"scene_same"``);SOFT 时不进 ``failed_hard_checks``。
    - ``question``:本 Q 的问题文本(渲染进 user message ``## 问题`` 段)。
    - ``q_data``:本 Q 的数据段(渲染进 user message ``## 数据`` 段)。
    - ``q_type``:hard 或 soft;hard fail 计 ``failed_hard_checks``,soft fail
      拼 ``soft_warnings`` 短句。
    - ``soft_warning_template``:soft 时拼 warning 短句的格式化字符串(占位
      ``{time_gap}`` / ``{evidence}``);hard 时为空字符串不用。
    """

    name: str
    question: str
    q_data: str
    q_type: _QType
    soft_warning_template: str = ""


def validate_continuity(
    *,
    chapter_text: str,
    chapter_card: ChapterCard,
    chapter_id: str,
    workspace_id: str | None = None,
    router: ModelRouter | None = None,
    client: LLMClient | None = None,
    model: str | None = None,
    max_tokens: int = _DEFAULT_MAX_TOKENS,
    temperature: float = _DEFAULT_TEMPERATURE,
    prompts_dir: Path | None = None,
    prev_chapter_card: ChapterCard | None = None,
    executor: Executor | None = None,
    continuity_capsule: ProvenanceBoundContinuityCapsule | None = None,
) -> ContinuityValidationResult:
    """LLM-based 衔接桥语义判定(§4.3 全 3 条 + v1.0.x wave 4 session 4 Q4)。

    最多 5 次条件 gated 串行 LLM 调用(Q1 + Q2 + Q3 + Q4 + Q5),每次问一个
    yes/no/uncertain 二元问题。HARD fail(Q1 / Q2)→ caller 覆盖
    ``verdict=rewrite``;SOFT fail(Q3 / Q4 / Q5)→ caller 合并 ``review.suggestions``
    不动 verdict。

    v2 间隙 A1(2026-06-13):``executor`` 给定时各 Q 并发判定(每 Q 经
    ``contextvars.copy_context().run`` 提交,structlog workspace 上下文随行)。
    **verdict 语义与串行逐字一致**:``failed_hard_checks`` / ``soft_warnings``
    仍按 Q 规格原序收集(非完成序);"任一 Q 异常 → 整体 fail-open 不部分
    通过"(决策 4)保持 —— 唯一可观测差异是 fail-open 时 ``llm_calls_count``
    = 实际完成数(串行版 = 首个异常前的完成数),日志诚实非契约字段。

    :param chapter_text: 待校验的章节正文(已审稿的版本);本 agent 只看前
        ``_CHAPTER_HEAD_CHARS`` 字。
    :param chapter_card: 章节卡;读 ``continuity_context`` / ``chapter_number``
        / ``setting`` / ``pov_character`` 字段。
    :param chapter_id: 章节 ID,落入 structlog 事件 ``chapter_id`` 字段。
    :param workspace_id: 工作区 ID,落 structlog 字段(P3-B-14 透传纪律,可选)。
    :param router: ``ModelRouter`` 实例;给定时按
        ``router.route("continuity_validator")`` 取 client + model + 生成参数。
        ``None`` 时走 legacy 路径(``client`` + ``model`` 必传)。
    :param client: legacy 路径 LLM 客户端;``router=None`` 时必传。
    :param model: legacy 路径模型名;``router=None`` 时必传。
    :param max_tokens: legacy 路径 token 上限。
    :param temperature: legacy 路径温度。
    :param prompts_dir: 提示词模板根目录;``None`` 时取 ``settings.prompts_dir``。
    :param prev_chapter_card: v1.0.x wave 4 session 4 引入。上一章 ``ChapterCard``;
        ``None``(首章 / caller 未拉)→ Q4 自动 skip(condition gating)。
        Q4 用本字段的 ``next_chapter_seed`` + ``pov_character`` 与本章
        ``continuity_context`` 做承接对照。
    :return: ``ContinuityValidationResult``。
    """
    cc = chapter_card.continuity_context
    effective_capsule = continuity_capsule
    if effective_capsule is not None:
        try:
            effective_capsule.assert_matches_projection(
                frozen_provenance=chapter_card.continuity_ledger_provenance,
                prev_chapter_tail=cc.prev_chapter_tail,
                unfinished_actions=cc.unfinished_actions,
                pov_from=cc.pov_from,
            )
        except ValueError as exc:
            _log.warning(
                "continuity_capsule_full_prompt_fallback",
                chapter_id=chapter_id,
                workspace_id=workspace_id,
                reason=str(exc),
                consumer="continuity_validator",
            )
            effective_capsule = None
    book_clock_hard = (
        chapter_card.book_clock_constraint is not None
        and chapter_card.book_clock_constraint.is_hard
    )

    # skip 边界 1:全书首章 — 无衔接对象。用全局 chapter_id(ch-0001)判,
    # **不用卷内 chapter_number==1**(多卷下每卷开头卷内号都=1,会被误当首章
    # 跳过校验 → 卷边界 continuity 漏检;v1.1 wave 4 B1 acceptance 修)。
    if chapter_seq_from_id(chapter_card.chapter_id) == 1:
        _log.debug(
            "continuity_validator_skipped",
            chapter_id=chapter_id,
            workspace_id=workspace_id,
            reason="first_chapter",
        )
        return ContinuityValidationResult(passed=True, skipped=True)

    # skip 边界 2:prev_chapter_tail 空字符串(continuity_extractor 兜底,
    # prompts/continuity_extractor.md §1 字面"上一章正文为空(首章场景)→ 填
    # 空字符串")。
    if not cc.prev_chapter_tail.strip():
        _log.debug(
            "continuity_validator_skipped",
            chapter_id=chapter_id,
            workspace_id=workspace_id,
            reason="empty_prev_chapter_tail",
        )
        return ContinuityValidationResult(passed=True, skipped=True)

    # 根据 ContinuityContext 字段值 + 可选 prev_chapter_card 构造活跃 Q 列表。
    # v1.1 wave 6 session 3(B3):额外传本章 related_characters 激活 Q5 角色到场。
    q_specs = _build_q_specs(
        cc,
        prev_chapter_card=prev_chapter_card,
        current_related_characters=chapter_card.related_characters,
        book_clock_hard=book_clock_hard,
        book_clock_rule=(
            book_clock_rule_text(chapter_card)
            if book_clock_hard
            else ""
        ),
    )
    q4_proof = evaluate_q4_provider0_substitution(
        chapter_card=chapter_card,
        prev_chapter_card=prev_chapter_card,
        continuity_capsule=effective_capsule,
    )
    if q4_proof.substituted:
        assert effective_capsule is not None
        q_specs = [
            spec
            for spec in q_specs
            if spec.name != "chapter_card_self_consistency"
        ]
        _log.info(
            "continuity_q4_provider0_substituted",
            chapter_id=chapter_id,
            workspace_id=workspace_id,
            proof_hash=q4_proof.proof_hash,
            source_text_hash=effective_capsule.provenance.source_text_hash,
            ledger_hash=effective_capsule.provenance.ledger_hash,
        )
    hard_clock_required = any(
        spec.name == "time_gap" and spec.q_type == "hard" for spec in q_specs
    )

    # skip 边界 3:三 Q condition 全不满足(无判定对象)
    if not q_specs:
        _log.debug(
            "continuity_validator_skipped",
            chapter_id=chapter_id,
            workspace_id=workspace_id,
            reason="no_active_questions",
        )
        return ContinuityValidationResult(
            passed=True,
            skipped=not q4_proof.substituted,
            provider0_q4_substituted=q4_proof.substituted,
            q4_proof_hash=q4_proof.proof_hash,
        )

    # router / prompt setup — 失败 fail-open(对齐 must_achieve_validator 决策 4 扩):
    # 测试 fixture(``fake_prompts_dir`` 不含 continuity_validator_v1.md)走本路径,
    # 生产环境 prompt 已 commit 入仓不会缺。
    try:
        resolved_client, resolved_model, complete_kwargs = resolve_routing(
            agent_name="continuity_validator",
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
            schema_name="continuity_validator",
            complete_kwargs=complete_kwargs,
        )
    except Exception as exc:  # noqa: BLE001 — fail-open 包括 setup 阶段
        if hard_clock_required:
            raise ContinuityTechnicalError(stage="setup", cause=exc) from exc
        _log.warning(
            "continuity_validator_setup_failed_open",
            chapter_id=chapter_id,
            workspace_id=workspace_id,
            error_type=type(exc).__name__,
            error_message=str(exc),
        )
        return ContinuityValidationResult(
            passed=True,
            fail_open=True,
            llm_calls_count=0,
            provider0_q4_substituted=q4_proof.substituted,
            q4_proof_hash=q4_proof.proof_hash,
        )

    chapter_head = chapter_text[:_CHAPTER_HEAD_CHARS]

    def _ask_single_question(spec: _QSpec) -> _ValidatorLLMPayload:
        """单 Q 的 LLM 调用 + 出参解析(串行 / 并发两路径共用;异常冒泡给
        caller 统一按决策 4 fail-open)。"""
        # 衔接 Q1/Q2/Q4/Q5 只看开头；明确主时间线累计时，Q3 必须覆盖
        # 常规整章长度，避免章中后段一句“三个月后”绕过书内时钟 HARD。
        checked_text = (
            chapter_text[:_HARD_CLOCK_CHARS]
            if spec.name == "time_gap" and spec.q_type == "hard"
            else chapter_head
        )
        excerpt_label = (
            f"本章正文(最多前 {_HARD_CLOCK_CHARS} 字)"
            if spec.name == "time_gap" and spec.q_type == "hard"
            else f"本章开头(前 {_CHAPTER_HEAD_CHARS} 字)"
        )
        user_text = _build_user_message(
            question=spec.question,
            prev_chapter_tail=(
                effective_capsule.raw_final_tail
                if effective_capsule is not None
                else cc.prev_chapter_tail
            ),
            chapter_head=checked_text,
            chapter_excerpt_label=excerpt_label,
            chapter_card=chapter_card,
            q_data=spec.q_data,
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
            raw_text=response.content, model_used=response.model_used
        )

    failed_hard: list[str] = []
    soft_warnings: list[str] = []
    soft_warning_details: list[dict[str, str]] = []
    llm_calls_count = 0
    outcomes: list[tuple[_QSpec, _ValidatorLLMPayload]] = []

    if executor is not None:
        # v2 间隙 A1:各 Q 并发判定。futures 按 q_specs 原序建,收集时同序 →
        # 后续 verdict 处理顺序与串行版一致。任一 Q 异常 → 整体 fail-open
        # (决策 4 原样保持;与串行差异仅在其余 Q 已被调用,llm_calls_count
        # 记实际完成数)。
        futures = [
            executor.submit(contextvars.copy_context().run, _ask_single_question, spec)
            for spec in q_specs
        ]
        first_error: Exception | None = None
        first_error_spec: _QSpec | None = None
        hard_clock_error: Exception | None = None
        for spec, future in zip(q_specs, futures, strict=True):
            try:
                payload = future.result()
            except Exception as exc:  # noqa: BLE001 — 决策 4:fail-open
                if first_error is None:
                    first_error = exc
                    first_error_spec = spec
                if spec.name == "time_gap" and spec.q_type == "hard":
                    hard_clock_error = exc
                continue
            llm_calls_count += 1
            outcomes.append((spec, payload))
        if first_error is not None:
            assert first_error_spec is not None
            if hard_clock_required:
                raise ContinuityTechnicalError(
                    stage="transport_or_parse",
                    cause=hard_clock_error or first_error,
                ) from (hard_clock_error or first_error)
            _log.warning(
                "continuity_validator_failed_open",
                chapter_id=chapter_id,
                workspace_id=workspace_id,
                error_type=type(first_error).__name__,
                error_message=str(first_error),
                q_name=first_error_spec.name,
                q_completed=llm_calls_count,
                q_total=len(q_specs),
            )
            return ContinuityValidationResult(
                passed=True,
                fail_open=True,
                llm_calls_count=llm_calls_count,
                provider0_q4_substituted=q4_proof.substituted,
                q4_proof_hash=q4_proof.proof_hash,
            )
    else:
        for spec in q_specs:
            try:
                payload = _ask_single_question(spec)
                llm_calls_count += 1
            except Exception as exc:  # noqa: BLE001 — 决策 4:fail-open
                # 任一 Q 的 LLM 调用失败 → 整体 fail-open(passed=True)。**不部分通过**:
                # 否则 LLM 偶发故障会让部分章节 escalate 部分 pass,语义不一致;
                # 统一 fail-open 让 LLM 故障"透明"过(失败靠 reviewer 评分 + pipeline
                # 其余 substring 兜底)。
                if hard_clock_required:
                    raise ContinuityTechnicalError(
                        stage="transport_or_parse", cause=exc
                    ) from exc
                _log.warning(
                    "continuity_validator_failed_open",
                    chapter_id=chapter_id,
                    workspace_id=workspace_id,
                    error_type=type(exc).__name__,
                    error_message=str(exc),
                    q_name=spec.name,
                    q_completed=llm_calls_count,
                    q_total=len(q_specs),
                )
                return ContinuityValidationResult(
                    passed=True,
                    fail_open=True,
                    llm_calls_count=llm_calls_count,
                    provider0_q4_substituted=q4_proof.substituted,
                    q4_proof_hash=q4_proof.proof_hash,
                )
            outcomes.append((spec, payload))

    constraint_results: list[ConstraintResult] = []
    for spec, payload in outcomes:
        if spec.q_type == "hard":
            source_path, source_hash, rule_text = _hard_constraint_source(
                spec_name=spec.name,
                chapter_card=chapter_card,
            )
            constraint_results.append(
                ConstraintResult(
                    constraint_id=stable_constraint_id(
                        source_path=source_path,
                        domain="continuity",
                        rule_text=rule_text,
                    ),
                    domain="continuity",
                    source_path=source_path,
                    source_hash=source_hash,
                    rule_text=rule_text,
                    status=(
                        "fail"
                        if payload.verdict == "no"
                        or (
                            payload.verdict == "uncertain"
                            and spec.name != "time_gap"
                        )
                        else "pass"
                    ),
                    details=payload.evidence,
                )
            )
        if payload.verdict != "yes":
            # Q1/Q2 的 uncertain 仍 fail closed；typed Q3 只有直接 no 才证明
            # frozen time fact 冲突，uncertain 只能保留 SOFT 提示。
            proven_hard_failure = bool(
                spec.q_type == "hard"
                and (
                    payload.verdict == "no"
                    or (payload.verdict == "uncertain" and spec.name != "time_gap")
                )
            )
            if proven_hard_failure:
                failed_hard.append(spec.name)
                _log.info(
                    "continuity_validator_hard_failed",
                    chapter_id=chapter_id,
                    workspace_id=workspace_id,
                    q_name=spec.name,
                    verdict=payload.verdict,
                    evidence=payload.evidence,
                )
            else:
                warning = spec.soft_warning_template.format(
                    time_gap=cc.time_gap, evidence=payload.evidence
                )
                soft_warnings.append(warning)
                soft_warning_details.append(
                    {
                        "q_name": spec.name,
                        "evidence": payload.evidence,
                        "message": warning,
                    }
                )
                _log.info(
                    "continuity_validator_soft_warning",
                    chapter_id=chapter_id,
                    workspace_id=workspace_id,
                    q_name=spec.name,
                    verdict=payload.verdict,
                    evidence=payload.evidence,
                )
        else:
            _log.debug(
                "continuity_validator_q_passed",
                chapter_id=chapter_id,
                workspace_id=workspace_id,
                q_name=spec.name,
                evidence=payload.evidence,
            )

    passed = not failed_hard
    _log.info(
        "continuity_validator_completed",
        chapter_id=chapter_id,
        workspace_id=workspace_id,
        passed=passed,
        failed_hard_count=len(failed_hard),
        soft_warnings_count=len(soft_warnings),
        llm_calls_count=llm_calls_count,
        q_total=len(q_specs),
    )
    return ContinuityValidationResult(
        passed=passed,
        failed_hard_checks=failed_hard,
        constraint_results=constraint_results,
        soft_warnings=soft_warnings,
        soft_warning_details=soft_warning_details,
        llm_calls_count=llm_calls_count,
        provider0_q4_substituted=q4_proof.substituted,
        q4_proof_hash=q4_proof.proof_hash,
    )


def _hard_constraint_source(
    *,
    spec_name: str,
    chapter_card: ChapterCard,
) -> tuple[str, str, str]:
    """把 HARD Q 映射回章节卡权威字段；question/evidence 文案不参与 ID。"""
    chapter_id = chapter_card.chapter_id
    continuity_context = chapter_card.continuity_context
    if spec_name == "unfinished_actions":
        return (
            f"chapter_cards/{chapter_id}.continuity_context.unfinished_actions",
            "",
            "必须承接上一章未完成动作："
            + "；".join(continuity_context.required_actions),
        )
    if spec_name == "scene_same":
        return (
            f"chapter_cards/{chapter_id}.continuity_context.scene_continuation",
            "",
            "scene_continuation=same：本章开头须保持上一章同一物理空间；"
            "卡片明确的梦境、幻境、回忆或倒叙桥接例外除外。",
        )
    if spec_name == "time_gap":
        return (
            f"chapter_cards/{chapter_id}.book_clock_constraint",
            chapter_card.core_state_projection_hash,
            book_clock_rule_text(chapter_card),
        )
    raise ValueError(f"未知 continuity HARD constraint: {spec_name}")


def _build_q_specs(
    cc: ContinuityContext,
    *,
    prev_chapter_card: ChapterCard | None = None,
    current_related_characters: list[str] | None = None,
    book_clock_hard: bool = False,
    book_clock_rule: str = "",
) -> list[_QSpec]:
    """根据 ContinuityContext 字段值 + 可选 prev_chapter_card 构造活跃的 Q 列表。

    返回顺序固定 Q1 → Q2 → Q3 → Q4(v1.0.x wave 4 session 4 起)→ Q5(v1.1 wave 6
    session 3 起),让 structlog 事件 q_completed 字段可预测;同时 failed_hard_checks
    列表的顺序也由此决定(unfinished_actions 在前,scene_same 在后)。Q4 / Q5 都是
    SOFT,不进 failed_hard_checks。

    :param current_related_characters: v1.1 wave 6 session 3(B3)。本章
        ``chapter_card.related_characters``,与上一章 roster 做 diff 激活 Q5
        角色到场连续性。``None`` / 空 → Q5 自动 skip。
    """
    specs: list[_QSpec] = []

    # Q1: unfinished_actions 非空 — HARD §4.3 第 1 条
    if cc.required_actions:
        unfinished_list = "、".join(repr(a) for a in cc.required_actions)
        specs.append(
            _QSpec(
                name="unfinished_actions",
                question=(
                    "本章开头是否在语义上承接了上一章的未完成动作?"
                    "(允许同义改写、转述、间接体现;实质承接即 yes,完全没承接 = no)"
                ),
                q_data=f"unfinished_actions = [{unfinished_list}]",
                q_type="hard",
            )
        )

    # Q2: scene_continuation == "same" — HARD §4.3 第 2 条
    #
    # v1.1 收官重跑(2026-06-11,luo-accept-v2 ch-0003 实证)加梦境/醒来例外:
    # 双世界 / 梦境题材(典型"现代心理医生每夜梦回明朝锦衣卫")中,上一章结尾在
    # 梦境世界、本章开头"从梦中醒来切回现实"是**核心叙事装置**,writer 已承接上章
    # 结尾感官细节做桥接(教科书级 match-cut)。但 scene_continuation 抽取因两章
    # 共享现实场景而判 "same" → Q2 judge 读开头判"切到新空间"→ HARD fail → 反复
    # override 耗尽 rewrite budget 误杀好章为孤儿章。voice 校验早有由 setting /
    # mini plan 明示的梦境/POV 例外，continuity scene_same 缺对称例外。
    # 修法 = 给 Q2 judge 加梦境/醒来/回忆/倒叙转场例外(question + prompt 双层);
    # **自门控**:只对正文真有梦境桥接的转场放行,普通无理由跳切仍 no,不放水。
    if cc.scene_continuation == "same":
        specs.append(
            _QSpec(
                name="scene_same",
                question=(
                    "本章开头是否还在上一章同一物理空间?"
                    "(对照上一章末尾的环境描写与本章 setting / 本章开头的环境描写;"
                    "同一空间内的不同位置仍算 yes;切到新空间 / 跨地点 = no。"
                    "⚠️ 例外:若本章开头是**从上一章的梦境 / 幻境 / 回忆 / 倒叙中"
                    "醒来或切入**(双线叙事的世界切换、做梦醒来、闪回等),且正文承接了"
                    "上一章结尾的情境 / 感官细节后自然过渡到现实 / 另一世界 —— 这是合法的"
                    "叙事转场,不算无理由的物理空间断裂,判 yes;只有**毫无桥接的无理由"
                    "跳切**才判 no)"
                ),
                q_data='scene_continuation = "same"',
                q_type="hard",
            )
        )

    # Q3 保持原调用条件，避免首章/“立即续接”凭空增加一次 Provider 请求；
    # 已经会执行的 Q3 在 contract v1 下升级 HARD。兜底 gap 的累计时钟由
    # 每章本来就会执行的 Setting Judge 同一次请求复核。
    if cc.time_gap and cc.time_gap not in _TIME_GAP_SENTINELS:
        specs.append(
            _QSpec(
                name="time_gap",
                question=(
                    "本章正文是否直接违反给定的 frozen 书内时间关系?"
                    "只有明确 exact 冲突、明确 bounds 越界、AFTER/同夜/同日/"
                    "次日/恢复锚被直接违反或主线真正倒序才判 no。"
                    "近似量不生成分钟上下界；正文未写分钟、语义留白或无法证明"
                    "矛盾时判 yes，需提醒但证据不足时判 uncertain。"
                ),
                q_data=(
                    f'time_gap = "{cc.time_gap}"\n'
                    + (f"frozen typed HARD = {book_clock_rule}" if book_clock_hard else "")
                ),
                q_type="hard" if book_clock_hard else "soft",
                soft_warning_template=(
                    "时间表述与 time_gap 不一致(time_gap={time_gap}):{evidence}"
                ),
            )
        )

    # Q4: chapter_card 自洽性 — SOFT(v1.0.x wave 4 session 4)
    # condition gating:prev_chapter_card 给定且 next_chapter_seed / pov_character
    # 任一非空(否则无可对照基线)
    #
    # v1.1 wave 6 / Y3 口径修正(2026-06-03,B1 30 章长跑实证~50% 续跑章高频 soft warn):
    # 根因 = 本校验在 continuity refresh 阶段运行(本章正文尚未生成),unfinished_actions
    # 来自上一章结尾文本(``continuity._detect_unfinished_actions(tail)`` 正则信号),
    # 而原 question 要求它"已体现上一章 next_chapter_seed 的悬念"—— 悬念是规划层字段、
    # 由本章正文/卡 must_achieve 承接,不在上一章结尾文本职责内 → 把"尚未体现"误判成
    # "未承接" → 高频误报。修法 = 把判定口径从"承接验证(未体现=no)"收窄为"矛盾检测
    # (仅实质矛盾/pov 直接冲突=no,'尚未体现 next_chapter_seed'≠矛盾)",消除噪音、
    # 保留真矛盾检测。诚实标注:逻辑层单测覆盖,真实降频效果待下次长跑验证。
    if prev_chapter_card is not None:
        prev_seed = (prev_chapter_card.next_chapter_seed or "").strip()
        prev_pov = (prev_chapter_card.pov_character or "").strip()
        if prev_seed or prev_pov:
            unfinished_list = (
                "、".join(repr(a) for a in cc.unfinished_actions)
                if cc.unfinished_actions
                else "(空)"
            )
            specs.append(
                _QSpec(
                    name="chapter_card_self_consistency",
                    question=(
                        "本章 continuity_context 与上一章 ChapterCard 之间是否存在"
                        "**明显矛盾**?⚠️ 重要前提:本校验在 continuity refresh 阶段"
                        "运行、本章正文尚未生成,故 unfinished_actions / "
                        "prev_chapter_tail 反映的是上一章结尾状态,**不应**期望它们"
                        "已经体现上一章 next_chapter_seed 的悬念——该悬念由本章正文 / "
                        "卡 must_achieve 承接,不在本字段职责内,"
                        "『尚未提及 / 尚未体现 next_chapter_seed』**不算矛盾**。"
                        "仅在以下情况判 no:① 本章 pov_from 与上一章 pov_character "
                        "直接冲突且无合理过渡;② continuity 字段与 next_chapter_seed "
                        "出现实质性内容矛盾(人物 / 地点 / 状态直接对立,非『未涉及』)。"
                        "无明显矛盾 = yes。"
                    ),
                    q_data=(
                        f'prev_next_chapter_seed = "{prev_seed}"\n'
                        f'prev_pov_character = "{prev_pov}"\n'
                        f"本章 unfinished_actions = [{unfinished_list}]\n"
                        f'本章 pov_from = "{cc.pov_from or "(空)"}"\n'
                        f'本章 pov_to = "{cc.pov_to}"'
                    ),
                    q_type="soft",
                    # 主流程 .format(time_gap=..., evidence=...) 只两个占位 — prev_seed
                    # / prev_pov 固化到字面,time_gap 字面留着但实际不变量化
                    soft_warning_template=(
                        f"本章 continuity_context 与上一章 ChapterCard 存在明显矛盾"
                        f"(prev next_chapter_seed=「{prev_seed}」"
                        f" prev pov=「{prev_pov}」):{{evidence}}"
                    ),
                )
            )

    # Q5: 角色到场连续性 — SOFT(v1.1 wave 6 session 3,B3)
    # 治 B1 30 章长跑 bug B5(陆星野突兀出现:ch-23 全程沈舟独自 → ch-24 陆星野
    # 已站在白板另一侧,无任何到场交代)。
    #
    # 代码标记(粗筛 gating)+ LLM 复核(降 WARN 给剧情自然差异),对齐 wave6_design
    # §0 第一原则"抓矛盾护留白":本章出场角色 roster 与上一章 roster 做 diff,出现
    # "上一章不在场、本章出场"的候选才激活 Q5;LLM 读正文判断是否有合理到场交代
    # (自然首次登场介绍 / 被叫来 / 进入场景等都算有交代 = yes),只对"突兀出现、
    # 零到场交代"判 no → SOFT warning。WARN-only(不进 failed_hard_checks),对齐
    # Q3/Q4 同型 + wave6 全 WARN 默认(HARD 升级留 wave 6 末约束预算实测)。
    #
    # condition gating:prev_chapter_card 给定 + 上一章 roster 非空(否则无对照基线,
    # 首章 / 无 roster 章不误激活)+ 本章存在"上一章不在场"的出场角色。
    if prev_chapter_card is not None and current_related_characters:
        prev_roster = set(prev_chapter_card.related_characters or [])
        if prev_roster:
            newly_present = [
                c for c in current_related_characters if c not in prev_roster
            ]
            if newly_present:
                newly_list = "、".join(repr(c) for c in newly_present)
                specs.append(
                    _QSpec(
                        name="character_presence",
                        question=(
                            f"本章出场角色中,以下角色在上一章不在场:[{newly_list}]。"
                            "请判断本章正文是否为这些角色的出现提供了**合理的到场 / "
                            "在场交代**(进入场景、被召来、自然的首次登场介绍、正文"
                            "说明其本就在场的缘由等,任意一种说得通即可)?"
                            "有合理交代 / 自然引入 = yes;角色突兀出现、正文完全没有"
                            "任何到场或在场交代 = no。⚠️ 护留白:只要正文给了说得通的"
                            "在场理由就算 yes,**不要求**出现『他走了进来』这类显式字面;"
                            "拿不准时判 yes。"
                        ),
                        q_data=(
                            f"上一章在场角色(roster)= "
                            f"[{'、'.join(repr(c) for c in sorted(prev_roster))}]\n"
                            f"本章出场、上一章不在场的角色 = [{newly_list}]"
                        ),
                        q_type="soft",
                        soft_warning_template=(
                            f"角色到场连续性:角色 [{newly_list}] 在上一章不在场、"
                            f"本章出场却无到场交代:{{evidence}}"
                        ),
                    )
                )

    return specs


def _build_user_message(
    *,
    question: str,
    prev_chapter_tail: str,
    chapter_head: str,
    chapter_excerpt_label: str,
    chapter_card: ChapterCard,
    q_data: str,
) -> str:
    """拼装 user message:问题 + 上一章末尾 + 本章开头 + 章节卡上下文 + 数据 +
    marker。

    prompt 字面已在 system message 内说明输入格式 + 输出 JSON schema(参见
    ``prompts/continuity_validator_v1.md``),user message 只承载本次询问的
    具体数据。末尾追加 ``continuity_validator`` marker 供 MockLLMClient 识别
    (P1-B-22 协议)。"""
    body = (
        "## 问题\n\n"
        f"{question}\n\n"
        "## 上一章末尾\n\n"
        f"{prev_chapter_tail}\n\n"
        f"## {chapter_excerpt_label}\n\n"
        f"{chapter_head}\n\n"
        "## 章节卡上下文\n\n"
        f"- chapter_number: {chapter_card.chapter_number}\n"
        f"- setting: {chapter_card.setting}\n"
        f"- pov_character: {chapter_card.pov_character}\n\n"
        "## 数据\n\n"
        f"{q_data}"
    )
    return append_role_marker(body, role="continuity_validator")


def _parse_llm_payload(*, raw_text: str, model_used: str) -> _ValidatorLLMPayload:
    """剥代码块 → ``json.loads`` → Pydantic 校验。失败抛 ``ValueError``(caller
    在 ``validate_continuity`` 主循环 except 兜底统一 fail-open)。"""
    text = _strip_code_fence(raw_text).strip()
    try:
        data = loads_with_quote_repair(
            text,
            agent_name="continuity_validator",
            model_used=model_used,
        )
    except json.JSONDecodeError as exc:
        raise ValueError(
            f"continuity_validator LLM 输出非合法 JSON(model={model_used}): "
            f"{raw_text[:200]!r}"
        ) from exc
    try:
        return _ValidatorLLMPayload.model_validate(data)
    except ValidationError as exc:
        raise ValueError(
            f"continuity_validator LLM 输出 schema 不符(model={model_used}): "
            f"{exc.errors()}"
        ) from exc


def _strip_code_fence(text: str) -> str:
    """剥 trailing HTML 注释 + ``` / ```json 包裹(沿用 must_achieve_validator
    同型清理)。"""
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
    "ContinuityTechnicalError",
    "ContinuityValidationResult",
    "Q4Provider0Proof",
    "evaluate_q4_provider0_substitution",
    "uses_existing_q3_for_hard_book_clock",
    "validate_continuity",
]
