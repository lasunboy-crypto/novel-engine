"""ChapterCardGenerator agent(P5-B-05 引入)。

职责:基于卷规划上下文 + 上一章衔接 + 角色 voice hints + 当前活跃伏笔,
单次 LLM 调用产**单章 ChapterCard** 的"创意决策"字段(标题 / 视角 /
场景 / 必达-未解-禁止 / 衔接桥 / 草稿策略 / 关联角色 / 关联伏笔 /
下一章种子);系统字段(`chapter_id` / `workspace_id` / `volume_id` /
`chapter_number` / `arc_phase` / `is_arc_finale` / `tension_level`)
由 caller 在 ``ChapterCardGenerationContext`` 内显式注入,**LLM 不产
也不能改**(对齐 v2_wishlist §"ChapterCard `scene_continuation` 枚举沉淀"
+ p5_plan §9 字面"Literal 枚举显式列举,LLM 不脑补"自然语义"产枚举外
字符串")。

设计要点
========

1. **caller 预读注入,agent 不查 DB / 文件**(P4-B-07 沉淀的"预读数据
   接口"纪律):本 agent 接收 ``ChapterCardGenerationContext`` Pydantic
   实例,内含所有上下文(outline / 上一章衔接 / 角色 voice / 伏笔)。
   DB / 文件读取由 P5-B-06 ``chapter_card_service`` 层负责。

2. **依赖反转 + agent_routing 接入**(沿用 P3-B-06 ``resolve_routing`` +
   P5-B-03 outline_builder 模式):``router`` 主路径 → ``agent_routing.chapter_card_generator``
   表查 profile(P5-plan-kickoff 决策 4=B 默认 ``local_qwen_32k``);legacy
   路径 ``client`` + ``model`` 兜底供单测直接喂 fake client。

3. **JSON schema 严格校验,失败不自动降级**(沿用 P3-B-08 reviewer fallback
   + P5-B-03 outline_builder 先例 + p5_plan §9 字面"Literal 字段越界 +
   JSON 解码失败抛清晰异常,不自动降级"):LLM 输出剥代码块 + ``json.loads``
   → ``_ChapterCardDecisionPayload.model_validate``,任一失败抛
   ``ChapterCardGeneratorError``(含 ``raw_output`` / ``model_used``)+
   structlog ``chapter_card_generator_failed`` 事件。caller 可在 UI 上
   手动 retry。

4. **跨字段引用完整性校验**(LLM 后置):``pov_character`` 必须在
   ``character_voices`` 列表内;``related_characters`` 全部在 character_voices
   内;``related_plot_threads`` 全部在 plot_threads 内。任一不通过抛
   ``ChapterCardGeneratorError`` ——避免 LLM 编造未列出的角色 / 伏笔
   ID 渗入 ChapterCard。

5. **agent 身份 marker(P1-B-22 协议扩 ``chapter_card_generator``)**:
   user message 末尾追加 ``<!-- agent_role: chapter_card_generator -->``。
   MockLLMClient 收到对应 marker 返合规 ChapterCard fixture(3 Literal
   字段命中合法枚举);真 LLM 视 HTML 注释为字面忽略。

6. **system 字段注入纪律**:LLM 出参 ``_ChapterCardDecisionPayload``
   不含 system 字段;agent 在 ``_assemble_chapter_card`` 中合并 caller
   ``ChapterCardGenerationContext`` 的系统字段 +
   ``_ChapterCardDecisionPayload`` 的创意字段,组装出最终 ``ChapterCard``。
   ``card_status`` 默认 ``"draft"`` + ``card_approved_at`` 默认 ``None``
   (对齐 P5-B-02 ``ChapterCard.card_status`` 默认值)。

不做什么
========

- 不查 DB / 文件(留 P5-B-06 ``chapter_card_service``)
- 不批量生成多章；beat 批量 Agent 与对应端点已经退役
- 不实现 ``card_status`` 状态机(留 P5-B-07 ``card:approve`` / ``card:reject``)
- 不实现真 Qwen 集成测(留 P5-acceptance ``-m integration`` marker)
- 不派生完整 ``Character`` + ``VoiceProfile``(留 P5-B-06 chapter_card_service
  实施时按 P5-B-03 ``CharactersOutput.characters[*].voice_hint`` 派生)
"""

from __future__ import annotations

from src.core.models.narrative import CharacterParticipation, NarrativeContext, NarrativeEventContract

import hashlib
import json
import re
from collections.abc import Callable
from concurrent.futures import Future, ThreadPoolExecutor, as_completed
from contextvars import copy_context
from copy import deepcopy
from pathlib import Path
from typing import TYPE_CHECKING, Any, Literal

import structlog
from pydantic import (
    BaseModel,
    ConfigDict,
    Field,
    ValidationError,
    field_validator,
    model_validator,
)

from src.config import settings
from src.core.agents.structured_handoff import handoff_user_text
from src.core.agents._json_contract import loads_with_quote_repair
from src.core.agents._prompt import (
    append_role_marker,
    load_prompt_template,
    resolve_routing,
)
from src.core.agents._structured_output import prepare_strict_json_output
from src.core.agents.creative_delta import (
    EXPERIMENTAL_PIPELINE_CONTRACT,
    build_assembly_artifact,
    CreativeDeltaAssemblyError,
    FrozenAuthority,
    uses_hy3_authority_assembly,
    uses_hy3_draft_v3,
    uses_hy3_draft_v4,
    uses_hy3_draft_v5,
    uses_experimental_pipeline,
    uses_hy3_creative_delta,
)
from src.core.agents._name_lock import (
    _COMPOUND_SURNAMES,
    _SURNAMES,
)
from src.core.agents.chapter_summary_extractor import (
    ChapterSummary,
    ContinuityLedgerValidationError,
    build_provenance_bound_continuity_capsule,
)
from src.core.agents.setting_consistency_validator import (
    SettingConsistencyResult,
    validate_setting_consistency,
)
from src.core.llm.base import LLMClient, Message
from src.core.llm.local_json_contract import LocalJsonResponseViolation
from src.core.llm.authority_bound_selection import (
    AuthorityBoundSelectionError,
    AuthorityCatalog,
    AuthorityCatalogEntry,
    AuthorityReferenceBinding,
    AuthoritySelectionContract,
    AuthoritySnapshot,
)
from src.core.llm.cancel import LLMCancelledError
from src.core.models import ID_PATTERN
from src.core.models.chapter_card import (
    ChapterCard,
    ContinuityContext,
    FORBIDDEN_STYLE_KEYWORDS as _FORBIDDEN_STYLE_KEYWORDS,  # noqa: F401 — re-export
    detect_forbidden_style_pollution as _detect_forbidden_style_pollution,
    propagate_forbidden_advance_entry as _propagate_forbidden_advance_entry,
    strengthen_boundary_forbidden as _strengthen_boundary_forbidden,
)
from src.core.models.outline import ArcPhase
from src.core.models.continuity_ledger import (
    FIRST_CHAPTER_TIME_GAP_SENTINELS,
    ContinuityLedgerProvenance,
    ProvenanceBoundContinuityCapsule,
)
from src.core.models.core_state import (
    BookClockConstraint,
    CHARACTER_CORE_ATTRIBUTES,
    CORE_STATE_RELATION_ATTRIBUTES,
    OBJECT_CORE_ATTRIBUTES,
    CoreStateContractError,
    CoreStateProjection,
    CoreStateTransition,
    CoreStateWriteTransition,
    build_typed_book_clock_constraint,
    render_core_state_capsule,
    validate_core_state_transition_sources,
)
from src.core.models.drama import (
    build_dramatic_source_binding,
    DramaticBeatContract,
    LoadBearingObjectContract,
    VolumeDramaticPromise,
)
from src.core.models.review import ConstraintViolation
from src.core.models.setting_contract import SettingContract

if TYPE_CHECKING:
    from src.core.agents.must_vs_forbidden_validator import (
        MustVsForbiddenValidationResult,
    )
    from src.core.routing.router import ModelRouter


_log = structlog.get_logger(__name__)


# ---------------------------------------------------------------------------
# 常量与默认参数
# ---------------------------------------------------------------------------


_PROMPT_FILENAME = "chapter_card_generator.md"
_DELTA_PROMPT_FILENAME = "chapter_card_generator_delta.md"
_DELTA_AUTHORITY_V2_PROMPT_FILENAME = "chapter_card_generator_delta_v2.md"
_CONTINUITY_LEDGER_ADDENDUM_FILENAME = "chapter_card_continuity_ledger_addendum_v1.md"
"""``settings.prompts_dir`` 下的 system prompt 文件名(对齐 writer / reviewer 单文件模式)。"""

_AGENT_NAME = "chapter_card_generator"
"""router.route(agent_name) 入参(对齐 ``agent_routing.chapter_card_generator`` config 表)。"""

# 章节卡输出是结构化 JSON,温度低、长度中等(单章卡片 < 4000 token)。
_DEFAULT_TEMPERATURE = 0.4
_DEFAULT_MAX_TOKENS = 4000

# v2 W3.5 角色治理 C-4:章节卡硬任务文本里的命名角色闭环。
#
# 根因复核(2026-07-03):s4step1-v1 ch-0007 的 "苏教授" 不是 writer 自造,
# 而是出卡层已经把未知命名角色写进 must_achieve/forbidden;writer 只是忠实执行
# 错卡。这里不新增 LLM agent,用确定性校验把"卡层造名"挡在 ChapterCard 生成期。
_NAMED_ROLE_TITLES: tuple[str, ...] = (
    "教授",
    "老师",
    "导师",
    "师父",
    "师傅",
    "师尊",
    "博士",
    "医生",
    "主任",
    "队长",
    "长官",
    "老板",
    "先生",
    "女士",
    "小姐",
    "夫人",
    "长老",
    "掌门",
    "局长",
    "警官",
    "教官",
)
_SINGLE_SURNAME_CLASS = "".join(sorted(re.escape(s) for s in _SURNAMES))
_COMPOUND_SURNAME_ALT = "|".join(
    re.escape(s) for s in sorted(_COMPOUND_SURNAMES, key=len, reverse=True)
)
_NAMED_ROLE_TITLE_ALT = "|".join(
    re.escape(t) for t in sorted(_NAMED_ROLE_TITLES, key=len, reverse=True)
)
# 这里只识别合同字面的“姓氏紧接职称”（如“苏教授”）。不得在姓氏与职称之间
# 吞任意汉字：中文介词“向”也在百家姓表中，旧写法会把“向酒馆老板”整体误判成
# 一个命名称谓，导致普通职业角色无法进入章节卡。
_TITLED_PERSON_RE = re.compile(
    rf"((?:{_COMPOUND_SURNAME_ALT}|[{_SINGLE_SURNAME_CLASS}])"
    rf"(?:{_NAMED_ROLE_TITLE_ALT}))"
)


# v1.0 hot-fix bug ⑤:tension-aware 审稿阈值默认值(2026-05-19)。
# 旧实现走 ChapterCard 字段默认 7.5/6.5,对所有 tension 一刀切;开篇章
# tension=2 也要求 score>=7.5 → Qwen reviewer 系统性给不到这个分 → 死锁。
# 收敛策略:按 tension 4 档分段,低 tension 章节宽松,高 tension / 高潮收紧。
# 维护人可在前端章节卡编辑 UI 单章覆盖(本任务前端段同步提供编辑控件)。
_TENSION_THRESHOLD_BANDS: tuple[tuple[int, int, float, float], ...] = (
    (1, 3, 6.0, 5.0),  # opening / 低张力:开局铺垫,允许节奏放缓
    (4, 6, 6.5, 5.5),  # rising / 中段:发展段中规中矩
    (7, 8, 7.0, 6.0),  # high / 高张力:转折点要求更高完成度
    (9, 10, 7.5, 6.5),  # climax / 终章:最严,沿用历史默认
)


def _default_thresholds_for_tension(tension_level: int) -> tuple[float, float]:
    """按 ``tension_level`` 1-10 分档返 ``(review_threshold, revise_threshold)``。

    返回值用于 ``_assemble_chapter_card`` 注入新章节卡;维护人可在前端 UI
    单章覆盖(``ChapterCardReviewPanel`` 编辑控件)。

    分档表 ``_TENSION_THRESHOLD_BANDS``:
    - tension 1-3 → (6.0, 5.0)
    - tension 4-6 → (6.5, 5.5)
    - tension 7-8 → (7.0, 6.0)
    - tension 9-10 → (7.5, 6.5)
    """
    for low, high, review, revise in _TENSION_THRESHOLD_BANDS:
        if low <= tension_level <= high:
            return review, revise
    # 兜底(理论不可达 — ChapterCardGenerationContext.tension_level 已 ge=1 le=10)
    return 6.5, 5.5


# ---- v1.1 audit G19b:forbidden 字段风格污染检测 -----------------------------
#
# **v1.1 wave 2 临时会话(2026-05-28)上提**:``_FORBIDDEN_STYLE_KEYWORDS`` 常量
# + ``_detect_forbidden_style_pollution`` helper 已移到 ``src/core/models/chapter_card.py``
# 作 SSOT(顶部 import 已 re-export 私有别名)。ChapterCard ``model_validator(mode="after")``
# 自动走净化 — cascade retry / manual seed 等任一绕过本 generator 的 caller
# 都被截获。本模块的 L972 早期检测路径保留,让 LLM caller 决定 retry。


# ---- LLM 输出格式剥离 -------------------------------------------------------


_CODE_FENCE_RE = re.compile(
    r"^\s*```(?:json)?\s*\n(?P<body>.*?)\n```\s*$",
    re.DOTALL,
)
"""剥 ```json ... ``` 代码块包裹(沿用 reviewer / extractor / outline_builder 模式)。"""

_TRAILING_HTML_COMMENT_RE = re.compile(
    r"\s*<!--.*?-->\s*$",
    re.DOTALL,
)
"""剥末尾 HTML 注释(防 LLM 复读 marker 到输出)。"""


def _strip_format_noise(text: str) -> str:
    """剥 trailing HTML 注释(可能多个)→ 代码块包裹。沿用 outline_builder 顺序。"""
    while True:
        stripped = _TRAILING_HTML_COMMENT_RE.sub("", text)
        if stripped == text:
            break
        text = stripped
    match = _CODE_FENCE_RE.match(text)
    if match is not None:
        return match.group("body")
    return text


# ---------------------------------------------------------------------------
# 异常类型
# ---------------------------------------------------------------------------


CHAPTER_CARD_REFERENCE_ERROR_CODE = "CHAPTER_CARD_REFERENCE_INVALID"
"""Agent 内层发现输出引用上下文列表外 ID 时的稳定错误码。

``ChapterCardService`` 用它把内层 fail-closed 结果接回既有一次严格 ID
重试；否则通用 ``ChapterCardGeneratorError`` 会让服务层承诺的引用重试
不可达。
"""

CHAPTER_CARD_CORE_STATE_ERROR_CODE = "CHAPTER_CARD_CORE_STATE_INVALID"
"""章节卡核心状态在付费语义 Judge 前被确定性合同拒绝。"""


class ChapterCardGeneratorError(ValueError):
    """ChapterCardGenerator JSON 解析 / schema 校验 / 跨字段引用失败时抛出。

    保留 ``raw_output`` / ``model_used``,便于 caller(P5-B-06
    ``chapter_card_service``)决定重试策略 + P7 调优时回查根因(对齐
    ``OutlineBuilderError`` / ``MemoryExtractorError`` / ``ReviewerParseError``
    形态)。
    """

    def __init__(
        self,
        message: str,
        *,
        raw_output: str,
        model_used: str,
        error_code: str | None = None,
        details: dict[str, object] | None = None,
    ) -> None:
        super().__init__(message)
        self.raw_output = raw_output
        self.model_used = model_used
        self.error_code = error_code
        self.details = details


# ---------------------------------------------------------------------------
# Caller 注入的上下文 Pydantic 模型
# ---------------------------------------------------------------------------


class CharacterVoiceHint(BaseModel):
    """角色 voice hint(供 LLM 选 ``pov_character`` + 推导 ``forbidden``)。

    与 ``CharacterOutlineItem`` 解耦 — 后者是 outline 阶段产出,本类是
    ChapterCardGenerator 的 caller-provided 入参。caller 通常从
    ``CharactersOutput.characters[*]`` 派生本对象(P5-B-06 chapter_card_service
    层做派生,本任务不实现)。
    """

    character_id: str = Field(
        ...,
        pattern=ID_PATTERN,
        description="角色稳定 ID(供 pov_character / related_characters 引用校验)",
    )
    name: str = Field(..., min_length=1, description="角色显示名(供 prompt 渲染)")
    role: str = Field(
        ...,
        min_length=1,
        description="角色定位自由文本(protagonist / antagonist 等,供 LLM 衡量 pov 选择)",
    )
    voice_hint: str = Field(
        ..., min_length=1, description="一句话说话风格(供 LLM 推导 forbidden)"
    )
    personality: str = Field(default="", description="已有性格与顾忌，创作参考")
    appearance: str = Field(default="", description="已有外貌与基础特征，当前状态优先")
    background: str = Field(default="", description="已有背景，不等于主视角已知")
    abilities: list[str] = Field(default_factory=list)
    goals_short_term: list[str] = Field(default_factory=list)
    goals_long_term: list[str] = Field(default_factory=list)
    relationships: dict[str, str] = Field(default_factory=dict)


class PlotThreadSummary(BaseModel):
    """活跃伏笔摘要(供 LLM 选 ``related_plot_threads``)。

    caller 通常从 ``PlotOutput.long_threads`` + 本工作区 ``PlotThread`` SQLite
    查询派生(P5-B-06 实现派生逻辑)。
    """

    thread_id: str = Field(..., pattern=ID_PATTERN, description="伏笔稳定 ID")
    title: str = Field(..., min_length=1, description="伏笔短标题")
    description: str = Field(..., min_length=1, description="伏笔描述 + 当前进度")
    status: str = Field(
        ...,
        min_length=1,
        description="伏笔状态自由文本(planted / developing / resolved / abandoned 等)",
    )
    origin: Literal["outline", "emergent", "manual"] = Field(
        "outline",
        description="内部来源；emergent 才是本章可选收口的临时伏笔",
    )
    expiry_policy: Literal["arc_end", "volume_end", "never"] = Field(
        "never",
        description="临时伏笔常规候选休眠范围；主伏笔固定 never",
    )
    source_evidence: str | None = Field(
        None,
        description="临时伏笔入库时的正文逐字证据；大纲/人工伏笔通常为空",
    )


class ChapterCardGenerationContext(BaseModel):
    """ChapterCardGenerator 入参的 caller-provided 上下文。

    所有字段由 caller 在调用 agent 前完成派生 / 查 DB / 读文件;agent 不
    再做任何 IO(P4-B-07 沉淀)。
    """

    # ---- 章节定位(系统字段) -----------------------------------------------
    workspace_id: str = Field(..., min_length=1, description="工作区 ID")
    chapter_id: str = Field(
        ..., pattern=ID_PATTERN, description="章节稳定 ID(caller 派生 ch-XXXX)"
    )
    volume_id: str = Field(..., pattern=ID_PATTERN, description="所属卷 ID")
    chapter_number: int = Field(..., ge=1, description="卷内 1-based 章序号")

    # ---- 卷规划级硬约束(预注入,LLM 不能改) -------------------------------
    arc_id: str = Field(..., pattern=ID_PATTERN, description="本章所属 arc 的 ID")
    arc_type: str = Field(
        ..., min_length=1, description="弧类型自由文本(主线 / 副本 / 支线 / 番外)"
    )
    arc_phase: ArcPhase = Field(
        ..., description="弧阶段 5 档(opening / rising / climax / falling / resolution)"
    )
    arc_summary: str = Field(..., min_length=1, description="本 arc 概要")
    is_arc_finale: bool = Field(
        ..., description="本章是否为 arc 终章(caller 计算 chapter_number == arc.end)"
    )
    tension_level: int = Field(
        ..., ge=1, le=10, description="本章张力 1-10(caller 从 tension_curve 取)"
    )
    beat_id: str | None = Field(
        default=None,
        description="本章所属 beat 稳定 ID；旧 outline 无 beats 时为 None。",
    )
    beat_summary: str = Field(
        default="",
        description="本章所属 beat 摘要；供普通单卡、引用重试与失效卡恢复同源使用。",
    )
    beat_must_advance: list[str] = Field(
        default_factory=list,
        description=(
            "所属 beat 的组级 must_advance；章节卡生成前必须明确，但不得机械复制为"
            "每章都要完成，须结合本章 mini_plan 分工。"
        ),
    )
    beat_forbidden_advance: list[str] = Field(
        default_factory=list,
        description=(
            "所属 beat 每章都不得触发的 forbidden_advance；任何单卡入口不得漏传。"
        ),
    )

    # ---- outline 上下文 -----------------------------------------------------
    setting_contract: SettingContract = Field(
        ...,
        description="来自 data/outline/world.json 的权威世界契约与 provenance",
    )
    core_conflict: str = Field(..., min_length=1, description="主线核心冲突")
    volume_summary: str = Field(..., min_length=1, description="本卷概要")
    volume_dramatic_promise: VolumeDramaticPromise | None = Field(
        default=None,
        description="本卷 typed 主戏剧承诺；legacy/普通题材可空。",
    )
    beat_dramatic_contract: DramaticBeatContract | None = Field(
        default=None,
        description="本章所属 beat 的可选 typed 戏剧合同。",
    )
    mini_plan_dramatic_contract: DramaticBeatContract | None = Field(
        default=None,
        description="本章 mini-plan 承担高价值 beat 时的逐字段副本。",
    )

    @model_validator(mode="after")
    def _validate_dramatic_source_chain(self) -> "ChapterCardGenerationContext":
        # 卷级 promise 会进入该卷每一章；只有被 mini-plan 指定承担高价值 beat 的
        # 那一章才携带 beat/mini contract。普通章因此合法形如 (promise, None, None)。
        if (
            self.beat_dramatic_contract is None
            and self.mini_plan_dramatic_contract is None
        ):
            return self
        if (
            self.volume_dramatic_promise is None
            or self.beat_dramatic_contract is None
            or self.mini_plan_dramatic_contract is None
            or self.beat_id is None
        ):
            raise ValueError(
                "高价值章节必须同时携带 volume promise、beat/mini typed contract 与 beat_id"
            )
        assert self.volume_dramatic_promise is not None
        assert self.beat_dramatic_contract is not None
        assert self.mini_plan_dramatic_contract is not None
        if self.beat_dramatic_contract != self.mini_plan_dramatic_contract:
            raise ValueError("beat 与 mini-plan dramatic_contract 必须逐字段一致")
        drama = self.mini_plan_dramatic_contract
        required_characters = {drama.actor_id, drama.counterforce_id}
        if not required_characters.issubset(self.mini_plan_linked_characters):
            raise ValueError(
                "dramatic actor/counterforce 必须进入 mini_plan_linked_characters"
            )
        if drama.thread_id and drama.thread_id not in self.mini_plan_linked_threads:
            raise ValueError("dramatic thread_id 必须进入 mini_plan_linked_threads")
        if drama.function == "finale":
            promise = self.volume_dramatic_promise
            if (
                drama.actor_id != promise.actor_id
                or drama.counterforce_id != promise.counterforce_id
                or drama.thread_id != promise.primary_thread_id
                or drama.turning_id != promise.primary_turning_id
            ):
                raise ValueError("finale dramatic_contract 未与 volume promise 对齐")
        return self

    @field_validator("setting_contract")
    @classmethod
    def _setting_contract_must_be_authoritative(
        cls, contract: SettingContract
    ) -> SettingContract:
        if contract.is_empty or not contract.has_provenance:
            raise ValueError("章节卡生成必须携带非空且有 provenance 的权威世界契约")
        return contract

    # ---- 上一章衔接(首章为空) ---------------------------------------------
    prev_chapter_summary: str | None = Field(None, description="上一章概要(首章 None)")
    prev_chapter_tail: str | None = Field(
        None, description="上一章末尾 500-800 字(首章 None)"
    )
    prev_pov_character: str | None = Field(
        None, description="上一章视角角色 ID(首章 None)"
    )
    prev_unfinished_actions: list[str] = Field(
        default_factory=list, description="上一章未完成动作列表(首章 [])"
    )
    continuity_ledger_provenance: ContinuityLedgerProvenance | None = Field(
        None,
        description=(
            "R4 直接前章已验证同步小账本 provenance；outline:commit 预生成草稿"
            "阶段可空，但非首章 approve / Writer 前必须存在且仍匹配。"
        ),
    )
    warning_watchlist_section: str = Field(
        default="",
        description=(
            "仅来自已批准前文终稿的结构化 SOFT WARN，供本章规划参考；"
            "不得覆盖 ChapterCard/世界契约或升级为 HARD。"
        ),
    )
    core_state_enabled: bool = Field(
        default=False,
        description="工作区是否启用核心状态与书内时钟 HARD；legacy caller 默认关闭。",
    )
    core_state_projection: CoreStateProjection | None = Field(
        default=None,
        description="本章写作前的唯一当前投影；启用时由服务层预读注入。",
    )

    # ---- 角色 + 伏笔 --------------------------------------------------------
    character_voices: list[CharacterVoiceHint] = Field(
        ..., min_length=1, description="本章可选角色清单(至少 1 个)"
    )
    plot_threads: list[PlotThreadSummary] = Field(
        default_factory=list, description="本章活跃伏笔清单(可空)"
    )

    # ---- v1.0.x wave 4 session 2:章级 mini_plan 事件分配注入 ----------------
    mini_plan_event: str | None = Field(
        default=None,
        description=(
            "v1.0.x wave 4 session 2 引入。本章在 outline mini_plan 第 5 段"
            "产出的 ``MiniPlanItem.one_line_event``(主语 + 动词 + 宾语三元素"
            "结构,30-60 字一句话)。``ChapterCardService.derive_context`` 读"
            "``data/outline/mini_plan.json`` 对应章注入;mini_plan.json 缺失"
            "(wave 4 前生成的旧工作区)时为 ``None``,prompt 拼装回退到"
            "``arc_summary`` 老路径。LLM 在 user_message 看到该字段时应优先"
            "用它指引本章 must_achieve / setting 派生,避免反复从粗粒度 "
            "arc_summary 抽同一组动作(相邻章节卡雷同 bug 根因 ②)。"
        ),
    )
    mini_plan_info_increment: str = Field(
        default="",
        description="本章小纲已有的信息变化意图；仅作场景设计参考，不自动新增 HARD",
    )
    narrative_context: NarrativeContext | None = None
    word_count_policy_version: Literal[0, 1, 2] = 0
    next_event_contract: NarrativeEventContract | None = Field(None, exclude_if=lambda value: value is None)
    next_dramatic_contract: DramaticBeatContract | None = Field(None, exclude_if=lambda value: value is None)
    next_mini_plan_event: str | None = Field(
        default=None,
        description=(
            "紧邻下一章的 MiniPlanItem.one_line_event。仅作章界保留：本章允许"
            "铺垫，但不得把下一章的独立事件完整触发或完成。末章/旧 outline 为 None。"
        ),
    )
    mini_plan_linked_characters: list[str] = Field(
        default_factory=list,
        description=(
            "v2 W3.5 C-6 引入。本章在 outline mini_plan 第 5 段声明的 "
            "``MiniPlanItem.linked_characters``(本章计划涉及的核心角色 ID)。"
            "``ChapterCardService.derive_context`` 从 ``mini_plan.json`` 对应章"
            "读出注入;出卡 agent 会在校验前把这些 ID **确定性补齐**到 "
            "``related_characters``(非 card LLM 重产,避免上游已声明角色在卡层"
            "丢失)。mini_plan 缺失 / 旧工作区 / 本章无声明角色 → 空 list,与既有"
            "路径零回归。"
        ),
    )
    mini_plan_linked_threads: list[str] = Field(
        default_factory=list,
        description=(
            "本章在 outline mini_plan 第 5 段声明的 ``MiniPlanItem.linked_threads``"
            "(本章必须推进的主伏笔 ID)。``ChapterCardService.derive_context`` 从 "
            "``mini_plan.json`` 对应章读出注入；出卡 agent 会在校验前把这些 ID "
            "**确定性补齐**到 ``related_plot_threads``，避免上游已经分配的伏笔在"
            "卡层被 LLM 换成另一条活跃伏笔。mini_plan 缺失 / 旧工作区 / 本章无"
            "声明伏笔时为空 list。"
        ),
    )

    # ---- v1.1 wave 4 session 2(G2):本章回响伏笔锚 ----
    callback_anchors: list[str] = Field(
        default_factory=list,
        description=(
            "v1.1 wave 4 session 2 / G2 引入。本章在 outline mini_plan 第 5 段"
            "声明的 ``MiniPlanItem.callback_anchors``(本章呼应的伏笔 / 隐喻 "
            "thread_id 列表)。``ChapterCardService.derive_context`` 从 "
            "``mini_plan.json`` 对应章读出注入;``_assemble_chapter_card`` 把本字段"
            "**确定性派生**为 ``ChapterCard.callback_to``(非 LLM 产出,避免变异),"
            "Writer 据此绕 embedding 精确 lookup 伏笔原文。mini_plan 缺失 / 旧工作区"
            "→ 空 list(callback_to 为空,Writer 跳过回响段)。"
        ),
    )

    # ---- v1.1 wave 5 session 6(M4'''):本章结构焦点 ----
    chapter_focus: str = Field(
        default="",
        description=(
            "v1.1 wave 5 session 6 / M4''' 引入。本章在 outline mini_plan 第 5 段"
            "声明的 ``MiniPlanItem.chapter_focus``(本章结构焦点,只在偏离题材默认结构"
            "时非空)。``ChapterCardService.derive_context`` 从 ``mini_plan.json`` 对应"
            "章读出注入;``_assemble_chapter_card`` 把本字段**确定性透传**为 "
            "``ChapterCard.chapter_focus``(非 card LLM 产出,避免重述上游 / 变异),"
            "Writer 据此知道本章别按题材默认结构稀释。mini_plan 缺失 / 旧工作区 / 未偏离"
            "→ 空(ChapterCard.chapter_focus 为空,Writer 跳过本段)。"
        ),
    )
    arc_beats: dict[str, str] = Field(
        default_factory=dict,
        description=(
            "v1.1 wave 5 session 7 / M5 引入。本章在 outline mini_plan 第 5 段声明的"
            "``MiniPlanItem.arc_beats``(逐章角色弧光台阶,key=character_id,value=自包含"
            "一句话'某态→某态 + 动作演出';只在某角色弧光真推进可见台阶时非空)。"
            "``ChapterCardService.derive_context`` 从 ``mini_plan.json`` 对应章读出注入;"
            "``_assemble_chapter_card`` 把本字段**确定性透传**为 ``ChapterCard.arc_beats``"
            "(非 card LLM 产出,避免重述上游 / 变异 —— 同 chapter_focus 范式),Writer "
            "据此用动作演出弧光台阶。mini_plan 缺失 / 旧工作区 / 本章无弧光推进 → 空 "
            "``{}``(ChapterCard.arc_beats 为空,Writer 跳过本段)。"
        ),
    )
    # ---- v1.1 wave 5 session 8(M4''-轴A):本章互动场景中心 ----
    interaction_focus: str = Field(
        default="",
        description=(
            "v1.1 wave 5 session 8 / M4''-轴A 引入。本章在 outline mini_plan 第 5 段"
            "声明的 ``MiniPlanItem.interaction_focus``(本章互动场景中心,治松 beat 题材"
            "'独处内省扎堆';只在本章戏剧核心是人物互动〔尤其安静互动〕时非空)。"
            "``ChapterCardService.derive_context`` 从 ``mini_plan.json`` 对应章读出注入;"
            "``_assemble_chapter_card`` 把本字段**确定性透传**为 "
            "``ChapterCard.interaction_focus``(非 card LLM 产出,避免重述上游 / 变异 —— "
            "同 chapter_focus 范式),Writer 据此把配角写成真人、情感从互动演出。mini_plan "
            "缺失 / 旧工作区 / 非互动中心章 → 空(ChapterCard.interaction_focus 为空,"
            "Writer 跳过本段)。"
        ),
    )
    breakthrough_payoff: str = Field(
        default="",
        description=(
            "v1.1 wave 5 session 10 / 模块① 引入。本章在 outline mini_plan 第 5 段声明的"
            "``MiniPlanItem.breakthrough_payoff``(本章升级可感化要点,只在境界突破/晋级章"
            "非空)。``ChapterCardService.derive_context`` 从 ``mini_plan.json`` 对应章读出"
            "注入;``_assemble_chapter_card`` **确定性透传**为 ``ChapterCard.breakthrough_payoff``"
            "(非 card LLM 产出 —— 同 chapter_focus 范式),Writer 据此把境界突破写成可感的"
            "实力跃迁事件。mini_plan 缺失 / 非升级章 / 非 3 题材 / 开关 OFF → 空(Writer 跳过)。"
        ),
    )

    # ---- v1.0.x wave 4 session 3b:前 N 章 ChapterSummary 长程事件序列注入 ----
    prev_chapter_summaries: list[ChapterSummary] = Field(
        default_factory=list,
        description=(
            "v1.0.x wave 4 session 3b 引入。前 N=10 章已抽取的 ChapterSummary"
            "(LLM 从 final.md 抽出的本章事实摘要 + 关键事件 + 角色互动 +"
            "未完动作)序列。``ChapterCardService.derive_context`` 用 "
            "``_read_prev_chapter_summaries`` helper 从 ``ChapterSummaryORM`` "
            "表按 ``chapter_id`` 升序读最近 N 条注入(首章 / 老工作区无表行 → "
            "空 list)。LLM 在 user_message 看到本字段时,应**全局视角**审视前"
            "若干章已发生的事件 / 角色关系动态变化(``character_interactions``"
            ".dynamic),让本章 must_achieve 与既往章节真差异化,避免单点对照"
            "上一章 next_chapter_seed 出现的'同 arc 5 章三元素轮换雷同'残留。"
            "替代会话 2 '上一章 next_chapter_seed → prev_chapter_summary 偷梁"
            "换柱'的字段映射(那是计划层面映射,本字段是事实层面长程序列)。"
        ),
    )

    # ---- P6-B-10:retry-references 重试上下文 ----------------------------------
    is_retry_with_strict_ids: bool = Field(
        default=False,
        description=(
            "P6-B-10 引入。``True`` 时由 ``ChapterCardService`` 在引用合法性"
            "校验失败后重试调用 — agent prompt 顶部加'重试上下文 — 严格 ID"
            "约束'段,强制 LLM 只用 ``character_voices`` / ``plot_threads``"
            "列表内 ID;``False``(默认)为首次调用,无 retry 提示。"
        ),
    )

    continuity_bridge_retry_actions: list[str] = Field(
        default_factory=list,
        description=(
            "章节卡 approve 的确定性 continuity bridge 校验失败后，由"
            "``ChapterCardService`` 注入的精确即时未完动作。非空时 prompt 顶部"
            "加入定向重试段，要求 ``continuity_bridge`` 逐字复制这些动作并说明"
            "先承接、再转场；首次生成默认为空，不增加 Provider 调用。"
        ),
    )

    continuity_bridge_retry_base_card: ChapterCard | None = Field(
        default=None,
        description=(
            "自动 continuity bridge 定向重试的冻结原卡。非空时，新模型输出只提供"
            " ``continuity_context.continuity_bridge``；其余字段全部保留原卡，禁止"
            "一次补桥顺手改写核心状态、must、forbidden、角色或下一章种子。"
        ),
    )

    core_state_retry_errors: list[str] = Field(
        default_factory=list,
        description=(
            "章节卡核心状态在付费 Judge 前被确定性校验拒绝后的原始错误。与 Setting"
            " 冲突共用同一次既有卡片修复额度；非空时只修这些迁移，不改无关合同。"
        ),
    )

    setting_conflict_retry_violations: list[ConstraintViolation] = Field(
        default_factory=list,
        description=(
            "章节卡 Setting Judge 首次命中阻断证据后，由 ChapterCardService 原样"
            "注入的一次定向重试证据。阻断证据包括 HARD，以及双 quote、来源与"
            "当前权威 world.unique_rules 都可回查但 Judge 尚未消歧的 SOFT 候选。"
            "每项保留稳定 constraint_id、权威规则逐字引文、候选卡冲突引文及来源"
            "hash；非空时 prompt 顶部声明权威世界规则与已批准事实优先，只修复被"
            "引用的冲突且保留其余上游合同。"
        ),
    )

    # ---- v1.0.x wave 4 session 4:同质化 retry banner 注入 -------------------
    forbidden_must_achieve_phrases: list[str] = Field(
        default_factory=list,
        description=(
            "v1.0.x wave 4 session 4 引入。多卡级联恢复时本章被检测器判定与其他"
            "章 ``must_achieve`` 字符集重合超阈值时,``ChapterCardService.``"
            "``_retry_card_for_diversification`` retry 时注入本字段:列表元素 "
            "= 同轮已生成卡的 ``must_achieve`` 字面 — agent prompt 加'同轮已存"
            "在 must_achieve 字面禁词清单'段,LLM 必须重写本章 must_achieve 三"
            "元组,避免与列表内任一字面字符集重合率超阈值。空 list(首次生成 / "
            "未触发同质化 retry)→ 不注入 banner,与既有路径无回归。沿用 P6-B-10 "
            "``is_retry_with_strict_ids`` 同型 banner 注入机制。"
        ),
    )

    # ---- v1.1 wave 5 session 1(M1 动态卡控制器):套话 avoid-list 注入 ----
    forbidden_style_phrases: list[str] = Field(
        default_factory=list,
        description=(
            "v1.1 wave 5 session 1 / M1 引入。**动态卡控制器**(``style_controller``)"
            "据 dedup 护栏跨章累计的套话台账,在某套话近章复发达阈值时把它作 avoid-list "
            "注入本章卡:列表元素 = 近章被判注水复读(padding)的套话短语 —— prompt 加"
            "'近章过度使用套话'专段,LLM 写本章时**主动换表达 / 省略**,不机械复用。与 "
            "``forbidden_must_achieve_phrases``(管**剧情点**同质化)**分两个独立通道不混**:"
            "本字段管 **prose 套话**(逆时针跳动 / 猛地睁开眼 / 毛骨悚然…)。空 list(老工作区 "
            "/ 控制器关 / 无 active 套话)→ 不注入 banner,与既有路径 100% 等价(零回归)。"
            "由 ``ChapterCardService.derive_context`` 调 ``style_controller.active_avoid_list`` "
            "注入(已 CAP 截断保有界)。**护留白**:信号源已滤 intentional / motif 回扣,"
            "核心母题意象(如'逆时针'= 时空机制)不会误入本清单。"
        ),
    )

    # ---- v1.1 L1 Q16:工作区级生成参数默认(caller 从 generation config 注入) ----
    default_word_count_min: int | None = Field(
        default=None,
        ge=1,
        description=(
            "v1.1 L1 引入。工作区级字数下限默认,由 ``ChapterCardService.``"
            "``derive_context`` 读 ``generation:`` 配置注入。``_assemble_chapter_card`` "
            "在 LLM 未给 ``word_count_min`` 时用本值作兜底(而非 ChapterCard 模型"
            "默认 2500)。``None``(老 caller / 无 session)→ 回退 ChapterCard "
            "模型字段默认,与既有路径零回归。"
        ),
    )
    default_word_count_max: int | None = Field(
        default=None,
        ge=1,
        description=(
            "v1.1 L1 引入。工作区级字数上限默认,语义同 ``default_word_count_min``"
            "(LLM 未给 ``word_count_max`` 时兜底,``None`` → ChapterCard 模型默认 4000)。"
        ),
    )
    default_max_rewrites: int | None = Field(
        default=None,
        ge=0,
        description=(
            "v1.1 L1 引入。工作区级整章重写次数上限默认。``_assemble_chapter_card`` "
            "设新卡 ``max_rewrites`` 用本值(LLM 不产该字段)。``None`` → ChapterCard "
            "模型默认 3,与既有路径零回归。"
        ),
    )
    default_plot_node_budget: int | None = Field(
        default=None,
        ge=1,
        description=(
            "v1.1 wave 4 session 3 / I2.1 引入。工作区级剧情节点预算默认,由 "
            "``ChapterCardService.derive_context`` 读 ``generation:`` 配置(风格模板 "
            "``STYLE_GENERATION_DEFAULTS`` 作引擎基线 + project.yaml L1 覆盖优先)注入。"
            "``_assemble_chapter_card`` 在 LLM 未给 ``plot_node_budget`` 时用本值兜底"
            "(语义同 ``default_word_count_min``)。``None`` → ChapterCard 模型默认 3,零回归。"
        ),
    )
    default_must_achieve_max: int | None = Field(
        default=None,
        ge=1,
        description=(
            "v1.1 wave 4 session 3 / I2.1 引入。风格模板的 must_achieve 数量上限**软提示**"
            "(由 ``STYLE_GENERATION_DEFAULTS`` 注入,非 GenerationConfig 字段、无 L1 覆盖)。"
            "``_build_user_message`` 据此加'must_achieve 控制在 N 项以内'引导段;**非硬截断**"
            "(must_achieve 由 LLM 产出且 ``min_length>=1``,硬截断有丢必达事件风险)。"
            "``None``(未选模板)→ 不附该引导段,零回归。"
        ),
    )


# ---------------------------------------------------------------------------
# LLM 出参中间 Pydantic(只校验创意字段,系统字段 caller 注入)
# ---------------------------------------------------------------------------


class _ChapterCardDecisionPayload(BaseModel):
    """LLM 出参的中间模型(只含创意决策字段)。

    系统字段(``chapter_id`` / ``workspace_id`` / ``volume_id`` /
    ``chapter_number`` / ``arc_phase`` / ``is_arc_finale`` /
    ``tension_level``)不在本模型 — caller 注入。Literal 字段
    (``scene_continuation`` / ``pov_change``)由
    Pydantic 严格校验,LLM 产枚举外字符串直接抛 ValidationError。
    """

    model_config = ConfigDict(extra="forbid")

    title: str = Field(..., min_length=1)
    pov_character: str = Field(..., min_length=1)
    setting: str = Field(..., min_length=1)
    # M2 纠正侧(v1.1 wave 5 s3):per-scene 卓越性焦点(情感节点深挖内心 / 关键戏给
    # 画面 / 过场克制留白)。LLM 产出,default="" 护留白 + 向后兼容(旧 LLM 输出无此
    # 字段 → "" → Writer 跳过本段)。所有单卡入口共用本字段。
    craft_focus: str = Field(default="")
    participation: list[CharacterParticipation] = Field(
        default_factory=list, exclude_if=lambda value: not value,
    )
    must_achieve: list[str] = Field(..., min_length=1)
    must_unresolved: list[str] = Field(default_factory=list)
    forbidden: list[str] = Field(default_factory=list)
    continuity_context: ContinuityContext = Field(...)
    core_state_transitions: list[CoreStateWriteTransition] = Field(
        default_factory=list, max_length=2
    )
    next_chapter_seed: str = Field(..., min_length=1)
    related_plot_threads: list[str] = Field(default_factory=list)
    related_characters: list[str] = Field(..., min_length=1)
    # 节奏控制可选 override(默认走 ChapterCard 字段默认值)
    word_count_min: int | None = Field(None, ge=1)
    word_count_max: int | None = Field(None, ge=1)
    plot_node_budget: int | None = Field(None, ge=1)


class _ChapterCardCreativeDelta(BaseModel):
    """T2 exact-Hy3 wire payload; public ChapterCard is unchanged."""

    model_config = ConfigDict(extra="forbid")

    title: str = Field(..., min_length=1)
    pov_character: str = Field(..., min_length=1)
    setting: str = Field(..., min_length=1)
    craft_focus: str = Field(...)
    must_achieve: list[str] = Field(..., min_length=1)
    must_unresolved: list[str] = Field(...)
    forbidden: list[str] = Field(...)
    time_gap: str = Field(..., min_length=1)
    scene_continuation: Literal["same", "switch", "time_jump"]
    continuity_bridge: str = Field(...)
    core_state_transitions: list[CoreStateWriteTransition] = Field(..., max_length=2)
    next_chapter_seed: str = Field(..., min_length=1)
    related_plot_threads: list[str] = Field(...)
    related_characters: list[str] = Field(..., min_length=1)


class _ChapterCardMaterialDelta(_ChapterCardCreativeDelta):
    """T2 exact-Hy3 wire payload; public ChapterCard is unchanged."""

    # 仅当前 v5 材料交接放宽模型新增项；旧协议模型/schema 原样保留。
    # 继承字段和 schema 元数据不变，只有原生请求的 must 下界按上下文选择。
    model_config = ConfigDict(extra="forbid", title="_ChapterCardCreativeDelta")
    must_achieve: list[str] = Field(...)


class _ChapterCardNarrativeDelta(_ChapterCardMaterialDelta):
    """仅新叙事版本在现有出卡调用中分类人物；旧 wire schema 不加字段。"""

    participation: list[CharacterParticipation] = Field(..., min_length=1)


_CHAPTER_CARD_AUTHORITY_BINDINGS = (
    AuthorityReferenceBinding(
        field_path="pov_character",
        catalogs=("characters",),
    ),
    AuthorityReferenceBinding(
        field_path="related_characters[]",
        catalogs=("characters",),
    ),
    AuthorityReferenceBinding(
        field_path="related_plot_threads[]",
        catalogs=("plot_threads",),
        empty_catalog_policy="require_empty_array",
    ),
    AuthorityReferenceBinding(
        field_path="core_state_transitions[].entity_id",
        catalogs=("characters", "objects"),
    ),
)


def _chapter_card_authority_contract(
    context: ChapterCardGenerationContext,
) -> AuthoritySelectionContract:
    """Freeze the only canonical IDs the exact-Hy3 card delta may select."""

    character_entries = [
        AuthorityCatalogEntry(
            stable_id=item.character_id,
            semantic=json.dumps(
                {
                    "name": item.name,
                    "role": item.role,
                    "voice_hint": item.voice_hint,
                },
                ensure_ascii=False,
                sort_keys=True,
                separators=(",", ":"),
            ),
        )
        for item in context.character_voices
    ]
    thread_entries = [
        AuthorityCatalogEntry(
            stable_id=item.thread_id,
            semantic=json.dumps(
                {
                    "title": item.title,
                    "description": item.description,
                    "status": item.status,
                    "origin": item.origin,
                },
                ensure_ascii=False,
                sort_keys=True,
                separators=(",", ":"),
            ),
        )
        for item in context.plot_threads
    ]

    object_semantics: dict[str, dict[str, Any]] = {}
    for object_id, item in _load_bearing_objects_by_id(context).items():
        object_semantics.setdefault(object_id, {})["load_bearing_contract"] = {
            "label": item.label,
            "causal_role": item.causal_role,
            "causal_requirement": item.causal_requirement,
            "relation_attributes": item.relation_attributes,
        }
    if context.core_state_projection is not None:
        for fact in context.core_state_projection.facts:
            if fact.entity_type != "object":
                continue
            object_semantics.setdefault(fact.entity_id, {}).setdefault(
                "current_projection", []
            ).append(
                {
                    "attribute": fact.attribute,
                    "value": fact.value,
                    "evidence_quote": fact.evidence_quote,
                }
            )
    object_entries = [
        AuthorityCatalogEntry(
            stable_id=object_id,
            semantic=json.dumps(
                semantic,
                ensure_ascii=False,
                sort_keys=True,
                separators=(",", ":"),
            ),
        )
        for object_id, semantic in object_semantics.items()
    ]
    snapshot = AuthoritySnapshot.freeze(
        stage="chapter_card_creative_delta",
        catalogs=(
            AuthorityCatalog.freeze(name="characters", entries=character_entries),
            AuthorityCatalog.freeze(name="objects", entries=object_entries),
            AuthorityCatalog.freeze(name="plot_threads", entries=thread_entries),
        ),
        required_catalogs={"characters"},
    )
    return AuthoritySelectionContract(
        snapshot=snapshot,
        bindings=_CHAPTER_CARD_AUTHORITY_BINDINGS,
    )


def _validate_chapter_card_authority_references(
    *,
    delta: _ChapterCardCreativeDelta,
    contract: AuthoritySelectionContract,
) -> None:
    """Enforce union selection plus entity-type and relation-value pairing."""

    contract.validate_payload(delta.model_dump(mode="json"))
    character_ids = set(contract.catalog_ids("characters"))
    object_ids = set(contract.catalog_ids("objects"))
    invalid: list[str] = []
    for index, transition in enumerate(delta.core_state_transitions):
        allowed_entity_ids = (
            character_ids if transition.entity_type == "character" else object_ids
        )
        if transition.entity_id not in allowed_entity_ids:
            invalid.append(
                f"core_state_transitions[{index}].entity_type/entity_id="
                f"{transition.entity_type}/{transition.entity_id}"
            )
        if transition.attribute in CORE_STATE_RELATION_ATTRIBUTES:
            for field, value in (
                ("from_value", transition.from_value),
                ("to_value", transition.to_value),
            ):
                if value is not None and value not in character_ids:
                    invalid.append(
                        f"core_state_transitions[{index}].{field}={value!r}"
                    )
    if invalid:
        raise AuthorityBoundSelectionError(
            "ChapterCard canonical reference type pairing failed: "
            + "; ".join(invalid)
        )


def _bind_chapter_card_state_pairs(
    schema: dict[str, Any], contract: AuthoritySelectionContract,
) -> None:
    """Constrain legal entity/ID/attribute combinations before Hy3 dispatch.

    Keep the public payload and its Pydantic validation unchanged. Branches
    contain complete object schemas; no guessed state or response repair.
    """
    array = schema["properties"]["core_state_transitions"]
    item = array["items"]
    wire_attributes = set(item["properties"]["attribute"]["enum"])
    characters = list(contract.catalog_ids("characters"))
    branches = []
    for kind, catalog, attributes in (
        ("character", "characters", CHARACTER_CORE_ATTRIBUTES),
        ("object", "objects", OBJECT_CORE_ATTRIBUTES - CORE_STATE_RELATION_ATTRIBUTES),
        ("object", "objects", CORE_STATE_RELATION_ATTRIBUTES),
    ):
        ids = list(contract.catalog_ids(catalog))
        allowed = sorted(wire_attributes.intersection(attributes))
        if not ids or not allowed:
            continue
        branch = deepcopy(item)
        fields = branch["properties"]
        fields["entity_type"]["enum"] = [kind]
        fields["entity_id"]["enum"] = ids
        fields["attribute"]["enum"] = allowed
        if attributes == CORE_STATE_RELATION_ATTRIBUTES:
            fields["from_value"]["enum"] = [None, *characters]
            fields["to_value"]["enum"] = characters
        branches.append(branch)
    if not branches:
        array["maxItems"] = 0
    else:
        array["items"] = branches[0] if len(branches) == 1 else {"anyOf": branches}


def _inherited_chapter_requirements(context: ChapterCardGenerationContext) -> list[str]:
    """只搬运原有章级必达，不把信息意图或组级任务另升 HARD。"""
    event = context.narrative_context.event_contract if context.narrative_context is not None else None
    requirements = list(event.required_events) if event is not None else ([context.mini_plan_event] if context.mini_plan_event else [])
    if context.mini_plan_dramatic_contract is not None:
        requirements.extend(context.mini_plan_dramatic_contract.natural_requirements())
    return list(dict.fromkeys(requirements))


def _next_chapter_requirements(context: ChapterCardGenerationContext) -> list[str]:
    if context.next_event_contract is None:
        return [context.next_mini_plan_event] if context.next_mini_plan_event else []
    events = list(context.next_event_contract.required_events)
    if context.next_dramatic_contract is not None:
        events.extend(context.next_dramatic_contract.natural_requirements())
    return list(dict.fromkeys(events))


def _decision_payload_from_delta(
    *,
    delta: _ChapterCardCreativeDelta,
    context: ChapterCardGenerationContext,
    authority: FrozenAuthority,
) -> _ChapterCardDecisionPayload:
    authority.assert_matches(context.model_dump(mode="json"))
    event = context.narrative_context.event_contract if context.narrative_context is not None else None
    if event is not None and any((delta.must_achieve, delta.must_unresolved, delta.forbidden)):
        raise ValueError("叙事 v2 的必达、未决与禁区由小纲原文继承，出卡三个数组必须为空，不能另添表达限制")
    must_achieve = _inherited_chapter_requirements(context) if event is not None else list(delta.must_achieve)
    if event is None and context.mini_plan_event and context.mini_plan_event not in must_achieve:
        must_achieve.insert(0, context.mini_plan_event)
    if not must_achieve:
        must_achieve = _inherited_chapter_requirements(context)
    continuity = ContinuityContext(
        prev_chapter_tail=context.prev_chapter_tail or "",
        unfinished_actions=list(context.prev_unfinished_actions),
        time_gap=delta.time_gap,
        scene_continuation=delta.scene_continuation,
        continuity_bridge=delta.continuity_bridge,
        pov_change=(
            context.prev_pov_character is not None
            and context.prev_pov_character != delta.pov_character
        ),
        pov_from=context.prev_pov_character,
        pov_to=delta.pov_character,
    )
    return _ChapterCardDecisionPayload(
        title=delta.title,
        pov_character=delta.pov_character,
        setting=delta.setting,
        craft_focus=delta.craft_focus,
        participation=list(getattr(delta, "participation", [])),
        must_achieve=must_achieve,
        must_unresolved=list(event.unresolved_questions) if event is not None else list(delta.must_unresolved),
        forbidden=list(event.forbidden_results) if event is not None else list(delta.forbidden),
        continuity_context=continuity,
        core_state_transitions=list(delta.core_state_transitions),
        next_chapter_seed=delta.next_chapter_seed,
        related_plot_threads=list(delta.related_plot_threads),
        related_characters=list(delta.related_characters),
        word_count_min=None,
        word_count_max=None,
        plot_node_budget=None,
    )


def _bind_chapter_card_schema_context(
    schema: dict[str, object],
    context: ChapterCardGenerationContext,
) -> None:
    """把本章可引用 ID 冻成 strict schema enum；本地校验仍保留。"""

    properties = schema.get("properties")
    if not isinstance(properties, dict):
        raise ValueError("ChapterCard strict schema 缺少 properties")

    character_ids = sorted({item.character_id for item in context.character_voices})
    thread_ids = sorted({item.thread_id for item in context.plot_threads})

    pov_schema = properties.get("pov_character")
    if isinstance(pov_schema, dict):
        pov_schema["enum"] = character_ids

    related_characters = properties.get("related_characters")
    if isinstance(related_characters, dict):
        items = related_characters.get("items")
        if isinstance(items, dict):
            items["enum"] = character_ids

    related_threads = properties.get("related_plot_threads")
    if isinstance(related_threads, dict):
        items = related_threads.get("items")
        if isinstance(items, dict) and thread_ids:
            items["enum"] = thread_ids

    participation = properties.get("participation")
    if isinstance(participation, dict):
        items = participation.get("items")
        if isinstance(items, dict):
            item_properties = items.get("properties")
            if isinstance(item_properties, dict) and isinstance(item_properties.get("character_id"), dict):
                item_properties["character_id"]["enum"] = character_ids
        if context.narrative_context is not None:
            participation["minItems"] = 1

    continuity = properties.get("continuity_context")
    if isinstance(continuity, dict):
        continuity_properties = continuity.get("properties")
        if isinstance(continuity_properties, dict):
            scope = continuity_properties.get("immediate_action_indices")
            if isinstance(scope, dict):
                scope.clear()
                scope.update({"type": "null", "enum": [None]})
            pov_to = continuity_properties.get("pov_to")
            if isinstance(pov_to, dict):
                pov_to["enum"] = character_ids
            pov_from = continuity_properties.get("pov_from")
            if isinstance(pov_from, dict):
                allowed_pov_from: list[str | None] = [None]
                if context.prev_pov_character is not None:
                    allowed_pov_from.append(context.prev_pov_character)
                pov_from["enum"] = allowed_pov_from


# ---------------------------------------------------------------------------
# 主入口
# ---------------------------------------------------------------------------


def generate_chapter_card(
    context: ChapterCardGenerationContext,
    *,
    router: ModelRouter | None = None,
    client: LLMClient | None = None,
    model: str | None = None,
    max_tokens: int = _DEFAULT_MAX_TOKENS,
    temperature: float = _DEFAULT_TEMPERATURE,
    prompts_dir: Path | None = None,
    on_parallel_status: Callable[[dict[str, Any]], None] | None = None,
) -> ChapterCard:
    """单次 LLM 调用产单章 ``ChapterCard``。

    :param context: caller-provided 上下文(章节定位 + 卷规划级硬约束 +
        outline + 上一章衔接 + 角色 + 伏笔)。**caller 必须保证字段完整且
        合法**(Pydantic 模型层做基础校验)。
    :param router: P3-B-06 主路径 ``ModelRouter``。
    :param client: legacy 路径 LLMClient(``router=None`` 时必传)。
    :param model: legacy 路径模型名。
    :param max_tokens: legacy 路径输出 token 上限,默认 4000。
    :param temperature: legacy 路径采样温度,默认 0.4。
    :param prompts_dir: 提示词模板根目录;``None`` 时取 ``settings.prompts_dir``。
    :return: 完整 ``ChapterCard`` Pydantic 实例(``card_status="draft"`` /
        ``card_approved_at=None``)。
    :raises ChapterCardGeneratorError: LLM 输出非 JSON / schema 不符 / Literal
        字段越界 / 跨字段引用完整性失败。
    :raises TypeError: ``router=None`` 且 legacy 路径缺 ``client`` / ``model``。
    """
    _frozen_immediate_indices(context)  # 在任何 Provider 请求前检验来源完整性。
    if context.narrative_context is not None:
        NarrativeContext.model_validate(context.narrative_context.model_dump(mode="json"))
        if context.narrative_context.version == 2 and context.next_mini_plan_event and context.next_event_contract is None:
            raise ValueError("下一章缺少同版本事件合同，不能把剧情摘要当成禁止表达的边界")
    _preflight_object_capacity(context)
    # Keep the new contract exclusive to the exact Hy3 experiment.  When the
    # caller supplies that client directly, invalid authority stops even before
    # routing; router-selected Hy3 freezes the same contract before any schema
    # preparation or Provider request.
    authority_selection: AuthoritySelectionContract | None = None
    if client is not None and uses_hy3_creative_delta(client):
        authority_selection = _chapter_card_authority_contract(context)
    resolved_client, resolved_model, complete_kwargs = resolve_routing(
        agent_name=_AGENT_NAME,
        router=router,
        client=client,
        model=model,
        legacy_kwargs={"max_tokens": max_tokens, "temperature": temperature},
    )
    use_delta = uses_hy3_creative_delta(resolved_client)
    material_handoff = use_delta and uses_hy3_draft_v5(resolved_client)
    delta_model = (
        _ChapterCardNarrativeDelta if context.narrative_context is not None else
        _ChapterCardMaterialDelta if material_handoff else _ChapterCardCreativeDelta
    )
    authority_assembly_v2 = use_delta and (
        uses_hy3_authority_assembly(resolved_client)
        or uses_hy3_draft_v3(resolved_client)
    )
    if use_delta and authority_selection is None:
        authority_selection = _chapter_card_authority_contract(context)
    delta_authority = FrozenAuthority.freeze(
        stage="chapter_card",
        payload=context.model_dump(mode="json"),
    )

    def legacy_schema_transform(schema: dict[str, Any]) -> None:
        _bind_chapter_card_schema_context(schema, context)

    def delta_schema_transform(schema: dict[str, Any]) -> None:
        assert authority_selection is not None
        authority_selection.transform_schema(schema)
        if context.narrative_context is not None:
            participation = schema["properties"]["participation"]
            participation["minItems"] = 1
            participation["items"]["properties"]["character_id"]["enum"] = sorted(
                item.character_id for item in context.character_voices
            )
        if material_handoff:
            schema["properties"]["must_achieve"]["minItems"] = (
                0 if _inherited_chapter_requirements(context) else 1
            )
        if context.narrative_context is not None and context.narrative_context.version == 2:
            for field in ("must_achieve", "must_unresolved", "forbidden"):
                schema["properties"][field]["minItems"] = 0
                schema["properties"][field]["maxItems"] = 0
        if uses_hy3_draft_v4(resolved_client):
            _bind_chapter_card_state_pairs(schema, authority_selection)

    if use_delta:
        assert authority_selection is not None
        schema_transform = delta_schema_transform
    else:
        schema_transform = legacy_schema_transform
    complete_kwargs = prepare_strict_json_output(
        client=resolved_client,
        model=resolved_model,
        output_model=(
            delta_model if use_delta else _ChapterCardDecisionPayload
        ),
        schema_name="chapter_card",
        complete_kwargs=complete_kwargs,
        transform=schema_transform,
    )

    template_root = (
        prompts_dir if prompts_dir is not None else settings.prompts_dir
    ).resolve()
    prompt_filename = (
        "chapter_card_generator_narrative_v9.md" if context.narrative_context is not None and context.narrative_context.story_intent.version >= 6 else
        "chapter_card_generator_narrative_v8.md" if context.narrative_context is not None and context.narrative_context.story_intent.version == 5 else
        "chapter_card_generator_narrative_v7.md" if context.narrative_context is not None and context.narrative_context.story_intent.version == 4 else
        "chapter_card_generator_narrative_v6.md" if context.narrative_context is not None and context.narrative_context.version == 2 else
        "chapter_card_generator_narrative_v3.md" if context.narrative_context is not None else
        "chapter_card_generator_delta_v6.md"
        if uses_experimental_pipeline(resolved_client)
        else "chapter_card_generator_delta_v5.md"
        if uses_hy3_draft_v5(resolved_client)
        else
        "chapter_card_generator_delta_v3.md"
        if uses_hy3_draft_v4(resolved_client)
        else _DELTA_AUTHORITY_V2_PROMPT_FILENAME
        if authority_assembly_v2
        else (_DELTA_PROMPT_FILENAME if use_delta else _PROMPT_FILENAME)
    )
    system_text = load_prompt_template(template_root / prompt_filename)
    if not use_delta:
        system_text = (
            system_text
            + "\n\n"
            + load_prompt_template(
                template_root / _CONTINUITY_LEDGER_ADDENDUM_FILENAME
            )
        )
    base_user_text = _build_user_message(
        context,
        authority_assembly_v2=authority_assembly_v2,
        scene_handoff=uses_hy3_draft_v4(resolved_client),
    )
    if material_handoff and not uses_experimental_pipeline(resolved_client):
        base_user_text += (
            "\n\n本章已有上游必达由程序原文保留，must_achieve 只填确有必要的新增局部结果。"
            "没有新增结果时返回 []，不要改写既有任务凑数。已有事件仍需由你设计"
            "关键尝试、可观察反馈和人物取舍，写在 setting/craft_focus 中供 Writer 演出；"
            "只保留结果原文不等于已经完成过程设计。"
            if _inherited_chapter_requirements(context)
            else "\n\n本章没有可继承的章级必达，must_achieve 至少提供一项本章事件或结果。"
        )
    if use_delta:
        assert authority_selection is not None
        base_user_text = (
            base_user_text
            + "\n\n"
            + authority_selection.render_prompt_catalog()
            + "\n\n- core_state_transitions[].entity_type 必须与同名 catalog 配对；"
            "关系属性的 from_value/to_value 只能使用 characters stable_id。"
        )
        if uses_hy3_draft_v4(resolved_client):
            base_user_text += (
                "\n\n核心状态变化的字段组合必须匹配：人物只允许 "
                + ", ".join(sorted(CHARACTER_CORE_ATTRIBUTES))
                + "；物件属性只用于 objects 清单中的实体。possession/control/access "
                "是物件的持有/控制/使用权限，from_value/to_value 引用人物 stable_id。"
                "人物之间的信任、指挥、协作或主导权变化属于互动过程，应写在 "
                "craft_focus 等创作字段，不能填为人物的 control 核心属性。"
                "承重物件仍须登记获准关系；已有同值关系可再次确认，未登记属性从 null 首建。"
                "没有本章承重关系且没有实际合法状态变化时，core_state_transitions 返回 []。"
            )
    user_text = append_role_marker(
        base_user_text,
        role="chapter_card_generator",
    )
    user_text = handoff_user_text(
        resolved_client, stage="chapter_card", data={"context": context},
        original_task=user_text,
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
    except LocalJsonResponseViolation as exc:
        failures = exc.cause.errors() if isinstance(exc.cause, ValidationError) else []
        core_only = bool(failures) and all(
            item.get("loc", ())[:1] == ("core_state_transitions",)
            for item in failures
        )
        raise ChapterCardGeneratorError(
            str(exc),
            raw_output=exc.response.content,
            model_used=exc.response.model_used,
            error_code=CHAPTER_CARD_CORE_STATE_ERROR_CODE if core_only else None,
            details={"errors": [str(exc.cause)]} if core_only else None,
        ) from exc

    parsed_delta: _ChapterCardCreativeDelta | None = None
    if use_delta:
        assert authority_selection is not None
        try:
            raw_data = json.loads(response.content)
            parsed_delta = delta_model.model_validate(raw_data)
            authority_selection.assert_matches(
                _chapter_card_authority_contract(context)
            )
            _validate_chapter_card_authority_references(
                delta=parsed_delta,
                contract=authority_selection,
            )
            payload = _decision_payload_from_delta(
                delta=parsed_delta,
                context=context,
                authority=delta_authority,
            )
        except (
            json.JSONDecodeError,
            ValidationError,
            CreativeDeltaAssemblyError,
            AuthorityBoundSelectionError,
        ) as exc:
            error_code = (
                exc.code
                if isinstance(exc, CreativeDeltaAssemblyError)
                else (
                    "authority_bound_reference_violation"
                    if isinstance(exc, AuthorityBoundSelectionError)
                    else "creative_delta_schema_mismatch"
                )
            )
            raise ChapterCardGeneratorError(
                "ChapterCardGenerator creative delta 无法确定性组装:"
                f"{type(exc).__name__}:{exc}",
                raw_output=response.content,
                model_used=response.model_used,
                error_code=error_code,
            ) from exc
    else:
        payload = _parse_llm_payload(
            raw_text=response.content,
            model_used=response.model_used,
            chapter_id=context.chapter_id,
        )
    payload = _payload_with_closed_related_characters(
        payload=payload,
        context=context,
        model_used=response.model_used,
        source="chapter_card_generator",
    )
    payload = _payload_with_closed_related_plot_threads(
        payload=payload,
        context=context,
        model_used=response.model_used,
        source="chapter_card_generator",
    )
    _validate_cross_field_references(
        payload=payload,
        context=context,
        raw_output=response.content,
        model_used=response.model_used,
    )
    _validate_emergent_plot_thread_selection(
        payload=payload,
        context=context,
        raw_output=response.content,
        model_used=response.model_used,
    )

    assembled = _assemble_chapter_card(payload=payload, context=context)
    if parsed_delta is not None:

        def _classify_card_provenance(
            path: str, leaf: Any
        ) -> tuple[
            Literal[
                "copied_authority", "deterministic_derived", "model_delta"
            ],
            str,
            str | None,
        ]:
            model_roots = (
                "title",
                "pov_character",
                "setting",
                "craft_focus",
                "must_unresolved",
                "core_state_transitions",
                "next_chapter_seed",
            )
            if path.startswith("narrative_context.participation"):
                return "model_delta", f"chapter_card_delta.{path.removeprefix('narrative_context.')}", None
            if path.startswith("narrative_context."):
                return "copied_authority", f"generation_context.{path}", "narrative_source_exact_copy"
            if path.startswith(model_roots):
                return "model_delta", f"chapter_card_delta.{path}", None
            if path.startswith("continuity_context."):
                field = path.rsplit(".", 1)[-1]
                if field in {"time_gap", "scene_continuation", "continuity_bridge"}:
                    return "model_delta", f"chapter_card_delta.{field}", None
                if field == "pov_change":
                    return "deterministic_derived", path, "pov_identity_comparison"
                return "copied_authority", f"generation_context.{path}", "exact_copy"
            if path.startswith("must_achieve["):
                if leaf in parsed_delta.must_achieve:
                    return "model_delta", "chapter_card_delta.must_achieve", None
                return "copied_authority", "generation_context.inherited_must", "exact_union"
            if path.startswith("forbidden["):
                if leaf in parsed_delta.forbidden:
                    return "model_delta", "chapter_card_delta.forbidden", None
                return "deterministic_derived", "generation_context.forbidden", "exact_union"
            if path.startswith(("related_characters[", "related_plot_threads[")):
                return "deterministic_derived", path, "validated_exact_union"
            if path.startswith(("chapter_id", "workspace_id", "volume_id")):
                return "copied_authority", f"generation_context.{path}", "exact_copy"
            return "deterministic_derived", path, "product_rule_or_default"

        artifact = build_assembly_artifact(
            stage="chapter_card",
            value=assembled,
            authority=delta_authority,
            delta=parsed_delta,
            classifier=_classify_card_provenance,
        )
        _log.info(
            "creative_delta_assembled",
            stage="chapter_card",
            chapter_id=context.chapter_id,
            authority_hash=artifact.authority_hash,
            delta_hash=artifact.delta_hash,
            full_leaf_count=artifact.full_leaf_count,
            model_delta_leaf_count=artifact.model_delta_leaf_count,
        )
    _validate_core_state_card_contract(
        card=assembled,
        context=context,
        raw_output=response.content,
        model_used=response.model_used,
    )
    _validate_chapter_card_semantics(
        card=assembled,
        context=context,
        raw_output=response.content,
        model_used=response.model_used,
        router=router,
        client=client,
        model=model,
        on_parallel_status=on_parallel_status,
    )
    return assembled


def _validate_must_vs_forbidden_consistency(
    *,
    card: ChapterCard,
    context: ChapterCardGenerationContext,
    raw_output: str,
    model_used: str,
    router: "ModelRouter | None",
    client: LLMClient | None,
    model: str | None,
) -> None:
    """调用既有 must_vs_forbidden_validator；直接冲突或执行边界歧义均在
    正文 Writer 前打回卡片。

    fail-open 路径(LLM 抛异常 / setup 失败)→ ``MustVsForbiddenValidationResult``
    passed=True,本函数不抛异常,章节卡生成继续(对齐 must_achieve_validator
    同型 fail-open 决策)。
    """
    result = _judge_must_vs_forbidden_consistency(
        card=card,
        context=context,
        router=router,
        client=client,
        model=model,
    )
    _enforce_must_vs_forbidden_consistency(
        result=result,
        context=context,
        raw_output=raw_output,
        model_used=model_used,
    )


def _judge_must_vs_forbidden_consistency(
    *,
    card: ChapterCard,
    context: ChapterCardGenerationContext,
    router: "ModelRouter | None",
    client: LLMClient | None,
    model: str | None,
) -> "MustVsForbiddenValidationResult":
    """只读判定阶段；不记录业务失败、不修改卡片。"""

    # 延迟 import 规避 chapter_card_generator ↔ must_vs_forbidden_validator
    # 跨模块 import 顺序问题(后者依赖前者已落地的 prompt 资源加载约定)。
    from src.core.agents.must_vs_forbidden_validator import (
        validate_must_vs_forbidden,
    )

    return validate_must_vs_forbidden(
        must_achieve=list(card.must_achieve),
        forbidden=list(card.forbidden),
        chapter_id=context.chapter_id,
        workspace_id=context.workspace_id,
        tension_level=context.tension_level,
        setting=card.setting,
        craft_focus=card.craft_focus,
        next_chapter_seed=card.next_chapter_seed,
        **({"user_request": card.narrative_context.story_intent.user_request}
           if card.narrative_context is not None and card.narrative_context.story_intent.user_request is not None else {}),
        router=router,
        client=client,
        model=model,
    )


def _enforce_must_vs_forbidden_consistency(
    *,
    result: "MustVsForbiddenValidationResult",
    context: ChapterCardGenerationContext,
    raw_output: str,
    model_used: str,
) -> None:
    """固定合并阶段；保持旧错误、日志与结构化 details 逐字段不变。"""

    if result.passed:
        return

    # 不可执行合同命中 → raise 含结构化 finding，供既有卡片 retry 修复。
    judge_attempt = max(result.llm_calls_count, 1)
    structured_conflicts = []
    conflict_details = []
    for conflict in result.conflicts:
        constraint_id = hashlib.sha256(
            (
                f"chapter_card_contract\n{context.chapter_id}\n"
                f"{conflict.must_text}\n{conflict.forbidden_text}"
            ).encode("utf-8")
        ).hexdigest()
        structured_conflicts.append(
            {
                "constraint_id": constraint_id,
                "must_index": conflict.must_index,
                "must_text": conflict.must_text,
                "forbidden_index": conflict.forbidden_index,
                "forbidden_text": conflict.forbidden_text,
                "reason": conflict.reason,
                "classification": conflict.classification,
            }
        )
        conflict_details.append(
            f"  必达[{conflict.must_index}]『{conflict.must_text}』 ↔ "
            f"禁止[{conflict.forbidden_index}]『{conflict.forbidden_text}』"
            f"[{conflict.classification}]:"
            f"{conflict.reason}"
        )
    _log.error(
        "prewriting_semantic_gate_failed",
        failure_layer="chapter_card_contract",
        chapter_id=context.chapter_id,
        workspace_id=context.workspace_id,
        constraint_ids=[c["constraint_id"] for c in structured_conflicts],
        contract_hash=context.setting_contract.source_hash,
        attempt=judge_attempt,
    )
    _log.error(
        "chapter_card_generator_failed",
        reason=f"must_vs_forbidden_{result.verdict}",
        chapter_id=context.chapter_id,
        conflicts_count=len(result.conflicts),
        evidence=result.evidence,
        model_used=model_used,
    )
    raise ChapterCardGeneratorError(
        f"ChapterCardGenerator chapter_id={context.chapter_id}:"
        f"must_achieve vs forbidden 存在 {len(result.conflicts)} 处不可执行或"
        f"歧义合同({result.verdict}):\n"
        + "\n".join(conflict_details)
        + f"\n判定依据:{result.evidence}\n"
        "请重新生成章节卡：直接冲突须改写；歧义项只针对完成 must 所必需的同一"
        "证据、过程或披露边界，须把该实质边界写清。普通视角、遮挡、感官或叙述"
        "方式属于 Writer 的正常创作选择，不得要求卡片预先写死。",
        raw_output=raw_output,
        model_used=model_used,
        error_code="CHAPTER_CARD_CONTRACT_CONFLICT",
        details={
            "failure_layer": "chapter_card_contract",
            "contract_hash": context.setting_contract.source_hash,
            "attempt": judge_attempt,
            "conflicts": structured_conflicts,
            "classification": result.verdict,
        },
    )


def _render_chapter_card_setting_evidence(card: ChapterCard) -> str:
    """稳定渲染完整语义合同，供 world Judge 跨批准前文逐字回查。

    ``continuity_context`` 不是普通背景：其中的上一章真实尾文来自已批准正文。
    若只给 Judge 当前章 must/forbidden，它无法判断“再次启用”等依赖前态的规则。
    """

    return json.dumps(
        {
            "title": card.title,
            "setting": card.setting,
            "craft_focus": card.craft_focus,
            "must_achieve": card.must_achieve,
            "must_unresolved": card.must_unresolved,
            "forbidden": card.forbidden,
            "continuity_context": card.continuity_context.model_dump(mode="json"),
            "core_state_transitions": [
                item.model_dump(mode="json") for item in card.core_state_transitions
            ],
            "next_chapter_seed": card.next_chapter_seed,
        },
        ensure_ascii=False,
        sort_keys=True,
        separators=(",", ":"),
    )


def _validate_chapter_card_semantics(
    *,
    card: ChapterCard,
    context: ChapterCardGenerationContext,
    raw_output: str,
    model_used: str,
    router: "ModelRouter | None",
    client: LLMClient | None,
    model: str | None,
    on_parallel_status: Callable[[dict[str, Any]], None] | None = None,
) -> None:
    """R3 最终 assemble 后统一执行 world gate 与 must/forbidden gate。

    post-v6 B:两个 Judge 只读判定可并行；两路都结束后，主线程仍按旧链
    ``setting → must-vs-forbidden`` 固定合并。配置为 1 时不创建 executor，
    恢复旧串行 fail-fast，后一个 Judge 在前一个失败时不会启动。
    """

    experimental = (
        getattr(router, "pipeline_contract", None) == EXPERIMENTAL_PIPELINE_CONTRACT
        if router is not None else client is not None and uses_experimental_pipeline(client)
    )
    has_setting_gate = bool(context.setting_contract.unique_rules.strip()) or bool(
        experimental and (context.setting_contract.setting.strip() or context.setting_contract.power_system.strip())
    )
    configured_concurrency = settings.chapter_card_semantic_gate_max_concurrency
    # setting 最多 2 次 agent 尝试、每次 transport 最多 3 次；must 一次
    # logical call、transport 最多 3 次，总最坏物理尝试 9。余量不足时保持
    # setting → must 的旧串行 cap 优先级，避免最后几个额度被后一个 Judge 抢走。
    checker = getattr(router, "can_afford_provider_attempts", None)
    if configured_concurrency > 1 and callable(checker):
        try:
            cap_allows_parallel = bool(checker(9))
        except Exception as exc:  # noqa: BLE001 — 余量不可证实时安全退回串行
            cap_allows_parallel = False
            _log.error(
                "chapter_card_parallel_cap_headroom_check_failed",
                chapter_id=context.chapter_id,
                workspace_id=context.workspace_id,
                error_type=type(exc).__name__,
            )
        if not cap_allows_parallel:
            configured_concurrency = 1
            _log.info(
                "chapter_card_parallel_fallback",
                chapter_id=context.chapter_id,
                workspace_id=context.workspace_id,
                reason="provider_cap_headroom",
                required_attempts=9,
            )
    active_keys = (["setting"] if has_setting_gate else []) + ["must_vs_forbidden"]
    max_concurrency = min(configured_concurrency, len(active_keys))
    states: list[dict[str, Any]] = [
        {
            "subtask_id": f"chapter-card:{context.chapter_id}:{key}",
            "chapter_id": context.chapter_id,
            "judge": key,
            "status": "queued",
        }
        for key in active_keys
    ]
    state_by_key = {state["judge"]: state for state in states}

    def _emit_snapshot() -> None:
        if on_parallel_status is None:
            return
        snapshot = {
            "parallel_group": "chapter_card_semantic_gates",
            "chapter_id": context.chapter_id,
            "max_concurrency": max_concurrency,
            "tasks": [dict(state) for state in states],
        }
        try:
            on_parallel_status(snapshot)
        except Exception as exc:  # noqa: BLE001 — 状态投影不能改变业务语义
            _log.warning(
                "chapter_card_parallel_status_publish_failed",
                chapter_id=context.chapter_id,
                workspace_id=context.workspace_id,
                error_type=type(exc).__name__,
            )

    rendered_card_evidence = _render_chapter_card_setting_evidence(card)

    def _blocking_setting_violations(
        result: SettingConsistencyResult,
    ) -> list[ConstraintViolation]:
        """返回落盘前必须先修复的最高权威证据。

        正文阶段的 SOFT 仍只是观察；章节卡是尚未批准、可重生的写作合同。若
        Judge 已给出可逐字回查的 ``world.unique_rules`` 与当前卡双 quote，不能
        再让其自身 ``severity`` 位的一次采样漂移决定是否把歧义交给 Writer。
        这里只改变章节卡落盘资格，不把 SOFT 改写成 HARD。实验普通机制仅采用
        同次审核已证实双引文且无歧义的 authoring_conflicts；普通提醒不阻断。
        """

        contract = context.setting_contract
        blockers: list[ConstraintViolation] = []
        for violation in result.violations:
            if violation.severity == "hard":
                blockers.append(violation)
                continue
            if (
                violation.domain == "world.unique_rules"
                and contract.has_provenance
                and violation.source_path == contract.source_path
                and violation.source_hash == contract.source_hash
                and violation.rule_quote in contract.unique_rules
                and violation.evidence_quote in rendered_card_evidence
            ):
                blockers.append(violation)
        return blockers + [v for v in result.authoring_conflicts if v not in blockers]

    def _missing_required_recheck_ids(
        result: SettingConsistencyResult,
    ) -> list[str]:
        required_ids = {
            violation.constraint_id
            for violation in context.setting_conflict_retry_violations
        }
        if not required_ids:
            return []
        accounted_ids = {
            violation.constraint_id for violation in result.violations
        } | set(result.resolved_recheck_constraint_ids)
        return sorted(required_ids - accounted_ids)

    def _set_setting_state(result: SettingConsistencyResult) -> None:
        blockers = _blocking_setting_violations(result)
        state = state_by_key["setting"]
        if result.fail_open:
            state["status"] = "failed"
            state["reason"] = "technical_fail_open"
        elif _missing_required_recheck_ids(result):
            state["status"] = "failed"
            state["reason"] = "required_recheck_missing"
        elif blockers:
            state["status"] = "failed"
            state["reason"] = "semantic_failure"
        else:
            state["status"] = "succeeded"
            if result.skipped:
                state["reason"] = "empty_contract_skipped"

    def _set_must_state(result: "MustVsForbiddenValidationResult") -> None:
        state = state_by_key["must_vs_forbidden"]
        if result.fail_open:
            state["status"] = "failed"
            state["reason"] = "technical_fail_open"
        elif result.passed:
            state["status"] = "succeeded"
            if result.skipped:
                state["reason"] = "empty_contract_skipped"
        else:
            state["status"] = "failed"
            state["reason"] = "semantic_failure"

    def _judge_setting(
        card_snapshot: ChapterCard,
        context_snapshot: ChapterCardGenerationContext,
    ) -> SettingConsistencyResult:
        return validate_setting_consistency(
            chapter_text=_render_chapter_card_setting_evidence(card_snapshot),
            contract=context_snapshot.setting_contract,
            chapter_id=context_snapshot.chapter_id,
            workspace_id=context_snapshot.workspace_id,
            router=router,
            client=client,
            model=model,
            validation_scope="chapter_card",
            required_rechecks=list(
                context_snapshot.setting_conflict_retry_violations
            ),
        )

    def _merge_setting(result: SettingConsistencyResult) -> None:
        judge_attempt = max(result.llm_calls_count, 1)
        missing_recheck_ids = _missing_required_recheck_ids(result)
        if missing_recheck_ids:
            prior_by_id = {
                violation.constraint_id: violation
                for violation in context.setting_conflict_retry_violations
            }
            missing_violations = [prior_by_id[item] for item in missing_recheck_ids]
            _log.error(
                "prewriting_semantic_gate_failed",
                failure_layer="chapter_card_setting_recheck",
                chapter_id=context.chapter_id,
                workspace_id=context.workspace_id,
                constraint_ids=missing_recheck_ids,
                contract_hash=context.setting_contract.source_hash,
                attempt=judge_attempt,
                reason="positive_recheck_missing",
            )
            raise ChapterCardGeneratorError(
                "CHAPTER_CARD_SETTING_CONFLICT:"
                "定向修复后的 Setting Judge 未逐项证明原冲突已闭环",
                raw_output=raw_output,
                model_used=model_used,
                error_code="CHAPTER_CARD_SETTING_CONFLICT",
                details={
                    "failure_layer": "chapter_card_setting_recheck",
                    "contract_hash": context.setting_contract.source_hash,
                    "attempt": judge_attempt,
                    "blocking_policy": "required_positive_recheck",
                    "constraint_violations": [
                        item.model_dump(mode="json")
                        for item in missing_violations
                    ],
                },
            )
        blockers = _blocking_setting_violations(result)
        if blockers:
            hard_count = sum(v.severity == "hard" for v in blockers)
            unresolved_soft_count = len(blockers) - hard_count
            _log.error(
                "prewriting_semantic_gate_failed",
                failure_layer="chapter_card_setting",
                chapter_id=context.chapter_id,
                workspace_id=context.workspace_id,
                constraint_ids=[v.constraint_id for v in blockers],
                contract_hash=context.setting_contract.source_hash,
                attempt=judge_attempt,
                hard_count=hard_count,
                unresolved_soft_count=unresolved_soft_count,
            )
            raise ChapterCardGeneratorError(
                "CHAPTER_CARD_SETTING_CONFLICT:"
                "章节卡与权威世界规则存在 "
                f"{len(blockers)} 处 HARD 或未消歧的双引文冲突候选",
                raw_output=raw_output,
                model_used=model_used,
                error_code="CHAPTER_CARD_SETTING_CONFLICT",
                details={
                    "failure_layer": "chapter_card_setting",
                    "contract_hash": context.setting_contract.source_hash,
                    "attempt": judge_attempt,
                    "blocking_policy": "authoritative_unique_rule_exact_quotes",
                    "constraint_violations": [
                        v.model_dump(mode="json") for v in blockers
                    ],
                },
            )
        non_blocking = [v for v in result.violations if v not in blockers]
        if non_blocking:
            _log.warning(
                "prewriting_semantic_gate_soft",
                failure_layer="chapter_card_setting",
                chapter_id=context.chapter_id,
                workspace_id=context.workspace_id,
                constraint_ids=[v.constraint_id for v in non_blocking],
                contract_hash=context.setting_contract.source_hash,
                attempt=judge_attempt,
            )

    _emit_snapshot()

    # 串行回退或仅一个有效 Judge：保留旧 fail-fast 和调用数。
    if max_concurrency == 1:
        if has_setting_gate:
            setting_state = state_by_key["setting"]
            setting_state["status"] = "running"
            _emit_snapshot()
            try:
                setting_result = _judge_setting(
                    card.model_copy(deep=True),
                    context.model_copy(deep=True),
                )
            except (LLMCancelledError, Exception) as exc:
                setting_state["status"] = "failed"
                setting_state["reason"] = type(exc).__name__
                must_state = state_by_key["must_vs_forbidden"]
                must_state["status"] = "failed"
                must_state["reason"] = "not_run_after_earlier_failure"
                _emit_snapshot()
                raise
            _set_setting_state(setting_result)
            _emit_snapshot()
            try:
                _merge_setting(setting_result)
            except ChapterCardGeneratorError:
                must_state = state_by_key["must_vs_forbidden"]
                must_state["status"] = "failed"
                must_state["reason"] = "not_run_after_earlier_failure"
                _emit_snapshot()
                raise

        must_state = state_by_key["must_vs_forbidden"]
        must_state["status"] = "running"
        _emit_snapshot()
        try:
            must_result = _judge_must_vs_forbidden_consistency(
                card=card.model_copy(deep=True),
                context=context.model_copy(deep=True),
                router=router,
                client=client,
                model=model,
            )
        except (LLMCancelledError, Exception) as exc:
            must_state["status"] = "failed"
            must_state["reason"] = type(exc).__name__
            _emit_snapshot()
            raise
        _set_must_state(must_result)
        _emit_snapshot()
        _enforce_must_vs_forbidden_consistency(
            result=must_result,
            context=context,
            raw_output=raw_output,
            model_used=model_used,
        )
        return

    # 两路判定各自拿独立 Pydantic 深拷贝与独立 contextvars Context。
    outcomes: dict[str, Any] = {}
    errors: dict[str, BaseException] = {}
    futures: dict[Future[Any], str] = {}
    with ThreadPoolExecutor(
        max_workers=max_concurrency,
        thread_name_prefix="chapter-card-gate",
    ) as executor:
        setting_state = state_by_key["setting"]
        setting_state["status"] = "running"
        setting_context = copy_context()
        futures[
            executor.submit(
                setting_context.run,
                _judge_setting,
                card.model_copy(deep=True),
                context.model_copy(deep=True),
            )
        ] = "setting"

        must_state = state_by_key["must_vs_forbidden"]
        must_state["status"] = "running"
        must_context = copy_context()
        futures[
            executor.submit(
                must_context.run,
                _judge_must_vs_forbidden_consistency,
                card=card.model_copy(deep=True),
                context=context.model_copy(deep=True),
                router=router,
                client=client,
                model=model,
            )
        ] = "must_vs_forbidden"
        _emit_snapshot()

        for future in as_completed(futures):
            key = futures[future]
            try:
                result = future.result()
            except (LLMCancelledError, Exception) as exc:
                errors[key] = exc
                state_by_key[key]["status"] = "failed"
                state_by_key[key]["reason"] = type(exc).__name__
            else:
                outcomes[key] = result
                if key == "setting":
                    _set_setting_state(result)
                else:
                    _set_must_state(result)
            _emit_snapshot()

    # 用户取消优先于同批业务失败，确保顶层仍能把任务写成 cancelled；两路
    # 都已完整回收，因此不会遗留晚返回线程。无取消时才进入旧 setting-first 合并。
    for key in ("setting", "must_vs_forbidden"):
        error = errors.get(key)
        if isinstance(error, LLMCancelledError):
            raise error

    # 固定合并：与旧链一样 setting 永远先裁决，future 返回顺序不参与业务语义。
    if "setting" in errors:
        raise errors["setting"]
    setting_result = outcomes["setting"]
    _merge_setting(setting_result)

    if "must_vs_forbidden" in errors:
        raise errors["must_vs_forbidden"]
    must_result = outcomes["must_vs_forbidden"]
    _enforce_must_vs_forbidden_consistency(
        result=must_result,
        context=context,
        raw_output=raw_output,
        model_used=model_used,
    )


# ---------------------------------------------------------------------------
# user message 拼装
# ---------------------------------------------------------------------------


def _frozen_immediate_indices(ctx: ChapterCardGenerationContext) -> list[int] | None:
    """叙事新卡从有效前章账本冻结范围；不依赖模型重判。"""
    from src.core.models.continuity_ledger import immediate_action_indices

    if ctx.narrative_context is None:
        return None
    if ctx.chapter_id == "ch-0001":
        return []
    if ctx.continuity_ledger_provenance is None:
        return None  # 非首章预览尚无事实；现有批准入口仍要求账本。
    capsule = _continuity_capsule_for_card_render(ctx)
    if capsule is None:
        raise ChapterCardGeneratorError(
            "新叙事卡缺少可验真的即时动作分类来源", raw_output="",
            model_used="deterministic_preflight",
        )
    return immediate_action_indices(
        actions=list(capsule.unfinished_actions), records=list(capsule.evidence_records),
    )


def _continuity_capsule_for_card_render(
    ctx: ChapterCardGenerationContext,
) -> ProvenanceBoundContinuityCapsule | None:
    """只在直接前章 ledger 可逐字段验真时开启 Card render 去重。

    任何来源缺失、hash 漂移、证据 coverage 不全或直接投影不一致都在首次
    Provider 请求前回到旧完整 Prompt；这里不调用 Provider，也不安排 retry。
    """

    provenance = ctx.continuity_ledger_provenance
    if provenance is None:
        return None
    matches = [
        summary
        for summary in ctx.prev_chapter_summaries
        if summary.chapter_id == provenance.chapter_id
    ]
    if len(matches) != 1:
        return None
    summary = matches[0]
    try:
        capsule = build_provenance_bound_continuity_capsule(
            summary,
            frozen_provenance=provenance,
        )
    except ContinuityLedgerValidationError:
        return None
    if (
        ctx.prev_chapter_summary != capsule.summary_text
        or (ctx.prev_chapter_tail or "") != capsule.raw_final_tail
        or ctx.prev_pov_character != capsule.pov_character
        or ctx.prev_unfinished_actions != list(capsule.unfinished_actions)
    ):
        return None
    return capsule


def _build_prev_summaries_block(
    summaries: list[ChapterSummary],
    *,
    continuity_capsule: ProvenanceBoundContinuityCapsule | None = None,
) -> str:
    """v1.0.x wave 4 session 3b:拼装"前 N 章 ChapterSummary 长程事实"段。

    每条摘要按 chapter_id 升序展示:
    - 一行标题(chapter_id + pov_character + scene_setting)
    - summary_text(~200-500 字概要)
    - key_events 列表(每条 description + actors + location)
    - character_interactions 列表(每条 char_a / char_b / dynamic — 角色关系动态)
    - unfinished_actions 列表(未完动作)

    空 list(老工作区 / 首章 / backfill 未跑)→ 返显式占位说明段(让 LLM 明确
    本字段为空,自然回退到 prev_chapter_summary 单卡映射)。
    """
    if not summaries:
        return (
            "\n## 前 N 章长程事实序列(ChapterSummary,v1.0.x wave 4 session 3b)\n\n"
            "**(空列表)**:本工作区 ChapterSummary 表无前置摘要 — 老工作区 "
            "(wave 4 session 3a 前生成)或 backfill 脚本未对本工作区跑过,或本"
            "章是工作区首章。本章 must_achieve / next_chapter_seed 派生回退到 "
            "`prev_chapter_summary` 单卡映射(向后兼容)。\n\n"
        )

    lines: list[str] = [
        "\n## 前 N 章长程事实序列(ChapterSummary,v1.0.x wave 4 session 3b)\n",
        f"**共 {len(summaries)} 条**,按 chapter_id 升序;LLM 据此审视前若干章已"
        "发生的事件 + 角色关系动态变化,确保本章 `must_achieve` / `next_chapter_seed` "
        "与既往**字面差异化**(`key_events[*].description` 重合率 < 50%),并优先"
        "承接 `character_interactions[*].dynamic` / `unfinished_actions` 的语境延续。\n",
    ]
    for s in summaries:
        is_capsule_source = bool(
            continuity_capsule is not None
            and s.chapter_id == continuity_capsule.provenance.chapter_id
        )
        evidence_by_key = {
            (str(item.get("field", "")), int(item.get("index", -1))): item
            for item in s.evidence_records
            if isinstance(item, dict)
        }
        header = (
            f"### {s.chapter_id}"
            + (
                f"(pov={s.pov_character})"
                if s.pov_character and not is_capsule_source
                else ""
            )
            + (f" — 场景:{s.scene_setting}" if s.scene_setting else "")
        )
        lines.append(header)
        if not is_capsule_source:
            lines.append(f"- summary_text:{s.summary_text}")
        if s.key_events:
            key_lines = "\n".join(
                f"  - {ev.get('description', '(无描述)')}"
                + (f"  actors={ev['actors']}" if ev.get("actors") else "")
                + (f"  loc={ev['location']}" if ev.get("location") else "")
                + (f"  证据={ev['evidence_quote']}" if ev.get("evidence_quote") else "")
                for ev in s.key_events
            )
            lines.append(f"- key_events:\n{key_lines}")
        if s.character_interactions:
            ci_lines = "\n".join(
                f"  - {ci.get('char_a', '?')} <→> {ci.get('char_b', '?')}:"
                f"{ci.get('dynamic', '(无 dynamic)')}"
                + (f"  证据={ci['evidence_quote']}" if ci.get("evidence_quote") else "")
                for ci in s.character_interactions
            )
            lines.append(f"- character_interactions(角色关系本章变化):\n{ci_lines}")
        if s.unfinished_actions and not is_capsule_source:
            uf_lines = "\n".join(
                f"  - {action}"
                + (
                    f"  证据={evidence_by_key[('unfinished_actions', index)].get('evidence_quote', '')}"
                    if ("unfinished_actions", index) in evidence_by_key
                    else ""
                )
                for index, action in enumerate(s.unfinished_actions)
            )
            lines.append(f"- unfinished_actions:\n{uf_lines}")
        if s.character_states:
            state_lines = "\n".join(
                f"  - {state.get('character_id', '?')}:{state.get('state', '')}"
                f"  证据={state.get('evidence_quote', '')}"
                for state in s.character_states
            )
            lines.append(f"- character_states:\n{state_lines}")
        if s.established_facts:
            fact_lines = "\n".join(
                f"  - {fact}"
                + (
                    f"  证据={evidence_by_key[('established_facts', index)].get('evidence_quote', '')}"
                    if ("established_facts", index) in evidence_by_key
                    else ""
                )
                for index, fact in enumerate(s.established_facts)
            )
            lines.append(f"- established_facts:\n{fact_lines}")
        if s.revealed_mechanics:
            mechanic_lines = "\n".join(
                f"  - {mechanic}"
                + (
                    f"  证据={evidence_by_key[('revealed_mechanics', index)].get('evidence_quote', '')}"
                    if ("revealed_mechanics", index) in evidence_by_key
                    else ""
                )
                for index, mechanic in enumerate(s.revealed_mechanics)
            )
            lines.append(f"- revealed_mechanics:\n{mechanic_lines}")
        if s.source_text_hash and s.ledger_hash and not is_capsule_source:
            lines.append(
                "- provenance:"
                f"version={s.source_version_id} "
                f"source_text_hash={s.source_text_hash} "
                f"ledger_hash={s.ledger_hash}"
            )
        lines.append("")  # 空行分隔条目
    return "\n".join(lines) + "\n"


def _build_forbidden_must_achieve_block(phrases: list[str]) -> str:
    """v1.0.x wave 4 session 4:同质化 retry 时拼装"禁词清单"段。

    空 list(首次生成 / 未触发同质化 retry)→ 返空字符串,user_message 无 banner
    与既有路径 100% 等价(零回归)。非空 list 时拼装专段:列表元素 = 同轮已生成
    卡 ``must_achieve`` 字面,LLM 必须重写本章 must_achieve 三元组,避免与列表
    内任一字面字符集重合率超阈值。沿用 P6-B-10 ``is_retry_with_strict_ids``
    同型 banner 注入机制。
    """
    if not phrases:
        return ""
    phrase_lines = "\n".join(f"  - 「{p}」" for p in phrases)
    return (
        "## 同质化 retry — 必须避开的 must_achieve 字面"
        "(v1.0.x wave 4 session 4)\n\n"
        "**注意**:上次生成本章 ``must_achieve`` 三元组与同轮级联恢复的其他卡"
        "字符集重合率超阈值,系统判定为内容雷同。本次"
        "请**完全重写**本章 ``must_achieve`` 列表元素,避免与下方字面任一项字符集"
        "重合率超 50%;字面**禁止**沿用同批其他章已用的(主语 + 动词 + 宾语)三"
        "元素组合,需切换主语角色 / 动词类别 / 宾语对象至少 2 个维度,实现真正的"
        "差异化。\n\n"
        "同轮已存在 must_achieve 字面禁词清单:\n"
        f"{phrase_lines}\n\n"
    )


def _build_forbidden_style_block(phrases: list[str]) -> str:
    """v1.1 wave 5 session 1(M1):拼装"近章过度使用套话 avoid-list"段。

    空 list(老工作区 / 控制器关 / 无 active 套话)→ 返空字符串,user_message 无
    banner,与既有路径 100% 等价(零回归)。非空 list 时拼装专段:列表元素 = 动态卡
    控制器据 dedup 台账判定"近章注水复读"的套话短语,LLM 写本章时**主动换表达 / 省略**。

    与 ``_build_forbidden_must_achieve_block``(管剧情点同质化)**分两个独立通道**:
    本段管 **prose 套话**,措辞强调"可换表达 / 省略"(非"完全重写剧情"),且
    **护留白** —— 提示 LLM 这是"避免机械复用",不是禁止该意象出现(留白 / 母题回扣合法)。
    """
    if not phrases:
        return ""
    phrase_lines = "\n".join(f"  - 「{p}」" for p in phrases)
    return (
        "## 套话避免 — 近章过度使用,本章主动换表达(v1.1 wave 5 / M1)\n\n"
        "**注意**:下列短语 / 句式在最近若干章已被反复机械使用(系统判定为注水复读"
        "padding,非有意母题回扣)。本章**避免机械复用**:可换更贴合本章情境的表达、"
        "用更具体的细节替代,或在不损叙事时**直接省略**。这是「避免套话堆砌」,**不是**"
        "禁止相关意象出现 —— 若某短语确为本作核心母题的有意回扣,可保留但务必换写法,"
        "不要逐字复读。优先护「留白 / 节制」,宁可少写一笔,不要硬凑同一套话。\n\n"
        "近章过度使用套话清单:\n"
        f"{phrase_lines}\n\n"
    )


def _load_bearing_objects_by_id(
    ctx: ChapterCardGenerationContext,
) -> dict[str, LoadBearingObjectContract]:
    """返回 plot→beat→mini 已严格对账的承重物件。

    这个 helper 故意不读 mini/beat 自然文本：机器身份只认 typed
    ``object_id``，不再使用 substring 、位置词或自由文本 marker。
    """

    drama = ctx.mini_plan_dramatic_contract
    if drama is None:
        return {}
    return {item.object_id: item for item in drama.load_bearing_objects}


def _preflight_object_capacity(context: ChapterCardGenerationContext) -> None:
    """付费前验证 typed 来源和必要关系容量，避免不相容合同反复生成。"""
    from src.core.models.core_state import CORE_STATE_TRANSITION_LIMIT
    from src.core.models.drama import DramaticSourceBinding

    if not context.core_state_enabled or context.mini_plan_dramatic_contract is None:
        return
    try:
        if (context.volume_dramatic_promise is None or context.beat_dramatic_contract is None
                or context.beat_id is None):
            raise ValueError("承重物件缺少完整来源链")
        source = build_dramatic_source_binding(
            volume_id=context.volume_id, beat_id=context.beat_id,
            promise=context.volume_dramatic_promise,
            beat_contract=context.beat_dramatic_contract,
            mini_plan_contract=context.mini_plan_dramatic_contract,
        )
        source = DramaticSourceBinding.model_validate(source.model_dump(mode="json"))
        if len(source.beat_contract.load_bearing_objects) > CORE_STATE_TRANSITION_LIMIT:
            raise ValueError("必要承重物件关系登记超出章节卡容量")
    except ValueError as exc:
        raise ChapterCardGeneratorError(
            str(exc), raw_output="", model_used="deterministic_preflight",
            error_code=CHAPTER_CARD_CORE_STATE_ERROR_CODE,
            details={"errors": [str(exc)]},
        ) from exc


def _validate_core_state_card_contract(
    *,
    card: ChapterCard,
    context: ChapterCardGenerationContext,
    raw_output: str,
    model_used: str,
) -> None:
    """在 Setting/must Judge 前验证迁移来源与新物件稳定身份。"""

    if not context.core_state_enabled:
        return
    projection = context.core_state_projection
    if projection is None:
        raise ChapterCardGeneratorError(
            "启用核心状态合同时缺少当前投影",
            raw_output=raw_output,
            model_used=model_used,
            error_code=CHAPTER_CARD_CORE_STATE_ERROR_CODE,
            details={"errors": ["启用核心状态合同时缺少当前投影"]},
        )

    errors: list[str] = []
    try:
        validate_core_state_transition_sources(
            transitions=card.core_state_transitions,
            projection=projection,
            dramatic_source=card.dramatic_source,
            related_character_ids=card.related_characters,
        )
    except CoreStateContractError as exc:
        errors.append(str(exc))

    current_object_ids = {
        fact.entity_id for fact in projection.facts if fact.entity_type == "object"
    }
    reserved_non_object_ids = {
        *(item.character_id for item in context.character_voices),
        *(item.thread_id for item in context.plot_threads),
    }
    load_bearing_objects = _load_bearing_objects_by_id(context)
    character_ids = {item.character_id for item in context.character_voices}
    transitioned_relation_object_ids: set[str] = set()
    for transition in card.core_state_transitions:
        if transition.entity_type != "object":
            continue
        object_contract = load_bearing_objects.get(transition.entity_id)
        if (
            transition.entity_id not in current_object_ids
            and transition.entity_id in reserved_non_object_ids
        ):
            errors.append(
                f"新物件核心身份 {transition.entity_id!r} 与角色/伏笔稳定 ID 冲突"
            )
            continue
        if transition.entity_id not in current_object_ids and object_contract is None:
            errors.append(
                f"新物件核心身份 {transition.entity_id!r} 未进入本章 "
                "plot→beat→mini typed 承重物件合同"
            )
        if transition.attribute in CORE_STATE_RELATION_ATTRIBUTES:
            if object_contract is None:
                errors.append(
                    "物件关系迁移必须命中本章 typed 承重物件:"
                    f"{transition.entity_id}/{transition.attribute}"
                )
            elif transition.attribute not in object_contract.relation_attributes:
                errors.append(
                    "物件关系属性未由 typed 承重合同授权:"
                    f"{transition.entity_id}/{transition.attribute}"
                )
            else:
                transitioned_relation_object_ids.add(transition.entity_id)
            if transition.to_value not in character_ids:
                errors.append(
                    "物件关系 to_value 必须是 character_voices 稳定 ID:"
                    f"{transition.entity_id}/{transition.to_value}"
                )
    missing_relation_objects = sorted(
        set(load_bearing_objects) - transitioned_relation_object_ids
    )
    if missing_relation_objects:
        errors.append(
            "本章承重物件每个都必须至少冻结一条获准 "
            "possession/control/access transition:"
            f"{missing_relation_objects}"
        )

    if not errors:
        return
    _log.error(
        "chapter_card_core_state_contract_failed_before_semantic_judges",
        workspace_id=context.workspace_id,
        chapter_id=context.chapter_id,
        errors=errors,
        projection_hash=projection.projection_hash,
    )
    raise ChapterCardGeneratorError(
        f"ChapterCardGenerator chapter_id={context.chapter_id}:"
        "核心状态合同在付费语义 Judge 前失败:\n- " + "\n- ".join(errors),
        raw_output=raw_output,
        model_used=model_used,
        error_code=CHAPTER_CARD_CORE_STATE_ERROR_CODE,
        details={
            "errors": errors,
            "projection_hash": projection.projection_hash,
            "transition_state_keys": [
                item.state_key for item in card.core_state_transitions
            ],
        },
    )


def _render_character_material(characters: list[CharacterVoiceHint]) -> str:
    """完整呈现已选人物的可用材料，不读取秘密、不推断额外故事事实。"""
    selected = {value for c in characters for value in (c.character_id, c.name)}
    blocks: list[str] = []
    for character in characters:
        lines: list[str] = []
        if character.personality.strip():
            lines.append("性格与顾忌：" + character.personality)
        if character.appearance.strip():
            lines.append("外貌与基础特征（初始资料，当前状态优先）：" + character.appearance)
        if character.background.strip():
            lines.append("已有背景（作者参考，不等于视角人物已知）：" + character.background)
        lines.extend(
            "已有能力（不要求本章全部使用）：" + ability
            for ability in character.abilities if ability.strip()
        )
        for label, goals in (
            ("当前短期目标", character.goals_short_term),
            ("长期动机", character.goals_long_term),
        ):
            lines.extend(f"{label}：{goal}" for goal in goals if goal.strip())
        lines.extend(
            f"关系资料（{other}）：{relation}"
            for other, relation in character.relationships.items()
            if other in selected and relation.strip()
        )
        if lines:
            blocks.append(f"### {character.name}（{character.character_id}）\n" + "\n".join(lines))
    if not blocks:
        return ""
    return (
        "\n\n## 人物性格、目标与关系参考（SOFT）\n\n"
        "以下可能包含全书动机和关系发展设想，不代表本章已经发生或主视角已经知晓。"
        "既定身份须准确；从本章处境选择人物此刻的欲望、顾忌及相互制约，设计为"
        "setting/craft_focus 中可演出的尝试、回应与选择，不把人物简介抄成剧情结果。\n\n"
        + "\n\n".join(blocks)
    )


def _build_user_message(
    ctx: ChapterCardGenerationContext,
    *,
    provider0_dedup_enabled: bool = True,
    authority_assembly_v2: bool = False,
    scene_handoff: bool = False,
) -> str:
    """user 消息:章节定位 + 卷规划级硬约束(预注入,LLM 不能改)+
    outline 上下文 + 上一章衔接 + 前 N 章长程事实序列 + 角色清单 + 伏笔清单。

    末尾追加 P1-B-22 协议的 ``chapter_card_generator`` marker(由 caller
    在 ``generate_chapter_card`` 内通过 ``append_role_marker`` 完成)。
    """
    char_lines = [
        f"  - {c.character_id} | {c.name} | {c.role} | voice_hint: {c.voice_hint}"
        for c in ctx.character_voices
    ]
    character_material = _render_character_material(ctx.character_voices)
    canonical_name_csv = "、".join(c.name for c in ctx.character_voices)
    name_guard_block = (
        "\n\n## 角色命名闭环(C-4 — 章节卡不得造名)\n\n"
        f"- 已确立角色名:{canonical_name_csv}\n\n"
        "你在 `title` / `setting` / `craft_focus` / `must_achieve` / "
        "`must_unresolved` / `forbidden` / `next_chapter_seed` 中提到任何命名角色时,"
        "必须使用上方角色清单里的确切姓名,并把该角色 ID 放进 `related_characters`。"
        "**禁止**另起 `苏教授` / `陈博士` / `某老师` 这类清单外命名称谓;"
        "如果剧情只需要路人或群众,保持无名即可。\n"
    )
    planned_threads = [t for t in ctx.plot_threads if t.origin != "emergent"]
    emergent_threads = [t for t in ctx.plot_threads if t.origin == "emergent"]
    thread_lines: list[str] = []
    if planned_threads:
        thread_lines.append("### 大纲/人工主伏笔（沿用原规划，不受临时层门槛影响）")
        thread_lines.extend(
            f"  - {t.thread_id} | {t.title} | {t.status}\n    desc: {t.description}"
            for t in planned_threads
        )
    if emergent_threads:
        thread_lines.append("### 正文临时伏笔（仅供本章自选 0-1 条收口）")
        thread_lines.extend(
            f"  - {t.thread_id} | {t.title} | {t.status} | "
            f"expiry={t.expiry_policy}\n    desc: {t.description}\n"
            f"    入库正文证据:{(t.source_evidence or '(无)').replace(chr(10), ' ')}"
            for t in emergent_threads
        )
    if not thread_lines:
        thread_lines = ["  (无活跃伏笔)"]

    emergent_selection_block = (
        "\n\n## 临时伏笔可选收口合同（不改大纲主伏笔规划）\n\n"
        "- 上方“大纲/人工主伏笔”仍按原规则选择 0-3 条推进，不参加临时伏笔的"
        "过期、top-K 或单条限制。\n"
        "- 上方“正文临时伏笔”只是系统从当前作用域检出的少量候选；本章可以一条"
        "也不收。若剧情自然适合，最多选 1 条，把其 ID 放进 `related_plot_threads`，"
        "并在 `must_achieve` 增加且仅增加一条以 "
        "`[临时伏笔收口:<thread_id>]` 开头的硬任务，后面明确写出谁以什么行动"
        "处理什么悬念、读者最终得到什么结果。\n"
        "- 不选就不要写该临时 ID，也不要写收口 marker；不得为了清库存扭曲本章"
        "mini_plan，不得拿临时伏笔替换大纲主伏笔。"
        if emergent_threads
        else ""
    )

    continuity_capsule = (
        _continuity_capsule_for_card_render(ctx)
        if provider0_dedup_enabled
        else None
    )
    evidence_by_action_index = (
        {
            record.index: record.evidence_quote
            for record in continuity_capsule.evidence_records
            if record.field == "unfinished_actions"
        }
        if continuity_capsule is not None
        else {}
    )
    immediate_indices = _frozen_immediate_indices(ctx)
    prev_actions_block = (
        "\n".join(
            f"  - {action}"
            + (" [即时动作，须承接]" if immediate_indices is not None and index in immediate_indices
               else " [长期未决背景，不要求重演或当章解决]" if immediate_indices is not None else "")
            + (
                f"  证据={evidence_by_action_index[index]}"
                if index in evidence_by_action_index
                else ""
            )
            for index, action in enumerate(ctx.prev_unfinished_actions)
        )
        if ctx.prev_unfinished_actions
        else "  (无)"
    )
    provenance_line = (
        "- provenance:"
        f"version={continuity_capsule.provenance.source_version_id} "
        f"source_text_hash={continuity_capsule.provenance.source_text_hash} "
        f"ledger_hash={continuity_capsule.provenance.ledger_hash}\n"
        if continuity_capsule is not None
        else ""
    )

    prev_block = (
        "## 上一章衔接\n\n"
        f"- prev_chapter_summary:{ctx.prev_chapter_summary or '(首章,无上一章)'}\n"
        f"- prev_chapter_tail:{ctx.prev_chapter_tail or '(首章,无上一章)'}\n"
        f"- prev_pov_character:{ctx.prev_pov_character or 'null'}\n"
        f"- prev_unfinished_actions:\n{prev_actions_block}\n"
        f"{provenance_line}"
    )

    if ctx.setting_conflict_retry_violations:
        violation_blocks = []
        for index, violation in enumerate(
            ctx.setting_conflict_retry_violations, start=1
        ):
            violation_blocks.append(
                "\n".join(
                    [
                        f"### 冲突 {index}",
                        f"- constraint_id:{violation.constraint_id}",
                        f"- domain:{violation.domain}",
                        f"- source_path:{violation.source_path}",
                        f"- source_hash:{violation.source_hash}",
                        f"- severity:{violation.severity}",
                        f"- 权威规则逐字引文:「{violation.rule_quote}」",
                        f"- 上一候选冲突逐字引文:「{violation.evidence_quote}」",
                        f"- ambiguity:{violation.ambiguity or 'null'}",
                    ]
                )
            )
        setting_conflict_retry_banner = (
            "## 定向重试 — 章节卡与权威世界规则冲突\n\n"
            "上一候选已被 Setting Judge 以结构化阻断证据拒绝：它是 HARD，或是"
            "权威 unique_rules 与当前卡双引文均可回查但尚未消歧的 SOFT 候选。"
            "本次是唯一一次定向重试，必须同时遵守以下边界：\n\n"
            "1. `world.unique_rules` 与已经批准的前文事实优先于 beat、mini_plan、"
            "候选卡措辞；只在它们彼此兼容时执行低层合同。\n"
            "2. 只修复下列逐字证据指出的冲突。若某条 beat/mini_plan 低层措辞会"
            "直接阻止权威规则成立，不得机械复制或扩大该措辞；应给出满足权威规则"
            "且保留其余剧情意图的可执行安排。\n"
            "3. 未被下列证据点名的 must、forbidden、下一章保留、角色、时间、地点"
            "和连续性合同全部保持，不得借重试改写无关剧情。\n"
            "4. 输出仍会重新经过完整 Setting Judge 与 must-vs-forbidden Judge；"
            "不得用含糊、留白或改名规避。\n\n"
            + "\n\n".join(violation_blocks)
            + "\n\n"
        )
    else:
        setting_conflict_retry_banner = ""

    if ctx.core_state_retry_errors:
        error_lines = "\n".join(
            f"  - 「{error}」" for error in ctx.core_state_retry_errors
        )
        core_state_retry_banner = (
            "## 定向重试 — 核心状态代码合同拒绝证据\n\n"
            "上一候选在 Setting/must 付费 Judge 前已被确定性代码拒绝。本次与"
            " Setting 修复共用唯一一次既有重试额度，只能修复下列迁移错误；"
            "must、forbidden、角色、地点、下一章种子及其他未点名字段保持原意。"
            "已有事实的 `from_value` 必须逐字使用当前投影；当前没有该事实则必须"
            "写 `null`，不得从摘要或正文猜旧值。新物件 ID 必须逐字取自本章"
            " mini_plan/beat。\n\n"
            "代码拒绝证据:\n"
            f"{error_lines}\n\n"
        )
    else:
        core_state_retry_banner = ""

    if ctx.continuity_bridge_retry_actions:
        action_lines = "\n".join(
            f"  - 「{action}」" for action in ctx.continuity_bridge_retry_actions
        )
        continuity_retry_banner = (
            "## 定向重试 — continuity bridge 代码拒绝证据\n\n"
            "上一轮章节卡已被确定性校验拒绝：`continuity_bridge` 没有逐字承接"
            "下列即时未完动作。本次必须在 "
            "`continuity_context.continuity_bridge` 中**逐字复制每一条**，并对"
            "每条说明本章开场先如何承接或收束，之后才可跳时、换景或切换 POV。"
            "只写缩写、概括、同义改写、‘暂时搁置’或信息盲区都不合格。"
            "`unfinished_actions` 仍须逐字复用系统注入列表。\n\n"
            "必须逐字承接的即时未完动作：\n"
            f"{action_lines}\n\n"
        )
    else:
        continuity_retry_banner = ""

    # P6-B-10:retry-references 重试时顶部插一段"严格 ID 约束"banner。
    # 仅在 caller(ChapterCardService)首次引用合法性校验失败后重试调用时设
    # True;首次调用时为空字符串。
    if ctx.is_retry_with_strict_ids:
        char_id_csv = ", ".join(f"`{c.character_id}`" for c in ctx.character_voices)
        if ctx.plot_threads:
            thread_id_csv = ", ".join(f"`{t.thread_id}`" for t in ctx.plot_threads)
            thread_line = (
                f"- 已知伏笔 ID(共 {len(ctx.plot_threads)} 个):{thread_id_csv}\n\n"
            )
        else:
            thread_line = (
                "- 已知伏笔 ID:(无活跃伏笔,本章 `related_plot_threads` 必为 `[]`)\n\n"
            )
        retry_banner = (
            "## 重试上下文 — 严格 ID 约束(已知 ID 列表)\n\n"
            "**注意**:上次生成中你输出的 ChapterCard 引用了下方列表外的角色 / "
            "伏笔 ID(系统已校验为'工作区不存在')。本次请**严格**只从下方"
            "`character_voices` 和 `plot_threads` 列表的 ID 中选取 "
            "`pov_character` / `related_characters` / `related_plot_threads` / "
            "`continuity_context.pov_to`;**禁止**编造列表外 ID,**禁止**复用"
            "上次的非法 ID。\n\n"
            f"- 已知角色 ID(共 {len(ctx.character_voices)} 个):{char_id_csv}\n"
            f"{thread_line}"
        )
    else:
        retry_banner = ""

    linked_names_by_id = {c.character_id: c.name for c in ctx.character_voices}
    linked_characters_block = (
        "\n".join(
            f"  - {cid} | {linked_names_by_id.get(cid, '(角色名未在当前清单中)')}"
            for cid in ctx.mini_plan_linked_characters
        )
        if ctx.mini_plan_linked_characters
        else "  (无)"
    )
    linked_threads_by_id = {thread.thread_id: thread for thread in ctx.plot_threads}
    linked_thread_lines: list[str] = []
    for thread_id in ctx.mini_plan_linked_threads:
        thread = linked_threads_by_id.get(thread_id)
        title = thread.title if thread is not None else "(伏笔未在当前清单中)"
        linked_thread_lines.append(f"  - {thread_id} | {title}")
    linked_threads_block = (
        "\n".join(linked_thread_lines) if linked_thread_lines else "  (无)"
    )

    drama_block = ""
    if ctx.mini_plan_dramatic_contract is not None:
        if (
            ctx.volume_dramatic_promise is None
            or ctx.beat_dramatic_contract is None
            or ctx.beat_id is None
        ):
            raise ValueError("dramatic typed source 不完整")
        drama = ctx.mini_plan_dramatic_contract
        promise = ctx.volume_dramatic_promise
        deferred_lines = ""
        if promise.outcome == "explicitly_deferred":
            deferred_lines = (
                f"- deferred_to_volume_id:{promise.deferred_to_volume_id}\n"
                f"- deferred_to_thread_id:{promise.deferred_to_thread_id}\n"
            )
        drama_block = (
            ("## 本章 typed 戏剧事实与表达意图\n\n" if drama.contract_version == 2 else
             "## 本章 typed 戏剧合同(HARD，逐字段执行)\n\n")
            +
            f"- beat_id:{ctx.beat_id}\n"
            f"- function:{drama.function}\n"
            f"- actor_id:{drama.actor_id}\n"
            f"- counterforce_id:{drama.counterforce_id}\n"
            f"- interaction_mode:{drama.interaction_mode}\n"
            f"- thread_id:{drama.thread_id}\n"
            f"- turning_id:{drama.turning_id}\n"
            f"- volume_final_outcome:{promise.outcome}\n"
            f"{deferred_lines}"
            "- volume_final_outcome 是本卷最终裁决；非 finale 章不得据此提前收束。\n"
            + (
                "- 程序将以下原文逐项继承到 must_achieve；请据此设计场景条件，"
                "不要在局部创意任务中回显或改写。人物行动与可见后果仍须可演出：\n"
                if scene_handoff else
                "- 本章必须把以下自然文本逐项放进 must_achieve，不能用查表、推断或"
                "旁白判断替代人物行动与可见后果：\n"
            )
            +
            "".join(f"  - {event}\n" for event in drama.natural_requirements())
            + ("- 以下为表达意图与人物利害，不整段加入must，也不规定心理或对白写法：\n"
               + "".join(f"  - {name}:{getattr(drama, name)}\n"
                         for name in ("actor_goal", "counteraction", "choice", "consequence"))
               + "间接对抗允许不同场；必要事件和结果仍由事实合同保证。\n\n"
               if drama.contract_version == 2 else
               "`interaction_mode=indirect` 表示反动作可隔空发生，但 counteraction 与"
               "consequence 仍必须在本章可见；不要求双方物理同场。\n\n")
        )

    event_contract = ctx.narrative_context.event_contract if ctx.narrative_context is not None else None
    # v1.0.x wave 4 session 2:章级 mini_plan 事件分配段(优先于 arc_summary)
    if ctx.mini_plan_event:
        next_event_holdback = (
            "\n- next_mini_plan_event(下一章保留事件，HARD):"
            f"{ctx.next_mini_plan_event}\n"
            "  本章只允许必要铺垫，禁止完整触发、完成或揭示该事件；必须把主要"
            "动作与结果留给下一章。\n"
            if ctx.next_mini_plan_event
            else ""
        )
        mini_plan_derivation_instruction = (
            "派生 ``must_achieve`` / ``setting`` 时**优先**围绕本字段,避免反复"
            "从 `arc_summary` 抽同组动作 — `arc_summary` 在本会话起降级为补充"
            "背景,**不再作章级事件主指引**。``next_chapter_seed`` 只写本章末尾"
            "已经可观察、但尚未完成后续动作或得出后续结论的线索 / 压力 / 人物"
            "决定；若下方有 next_mini_plan_event，不得把该保留事件的完整动作或"
            "结果改写进 seed。\n"
            if authority_assembly_v2
            else (
                "派生 ``must_achieve`` / ``setting`` / ``next_chapter_seed`` 时"
                "**优先**围绕本字段,避免反复从 `arc_summary` 抽同组动作 — "
                "`arc_summary` 在本会话起降级为补充背景,"
                "**不再作章级事件主指引**。\n"
            )
        )
        mini_plan_block = (
            "## 本章事件分配(mini_plan,v1.0.x wave 4 优先来源)\n\n"
            f"- mini_plan_event:{ctx.mini_plan_event}\n\n"
            "- mini_plan_linked_characters(本章计划角色,必须进入 related_characters):\n"
            f"{linked_characters_block}\n\n"
            "- mini_plan_linked_threads(本章计划推进的主伏笔,必须进入 "
            "related_plot_threads):\n"
            f"{linked_threads_block}\n\n"
            "**本字段是 outline mini_plan 第 5 段为本章独立分配的事件**"
            "(主语 + 动词 + 宾语三元素一句话,与同 arc 其他章已显式差异化)。"
            f"{mini_plan_derivation_instruction}"
            f"{next_event_holdback}\n"
        )
    else:
        mini_plan_block = (
            "## 本章事件分配\n\n"
            "**本工作区 mini_plan 未生成(wave 4 前的旧工作区)**;本章须从 "
            "`arc_summary` 推断事件分配,需主动与同 arc 其他章在(主语,动词,宾语)"
            "三元素上差异化。\n\n"
        )

    if ctx.mini_plan_info_increment.strip():
        mini_plan_block += (
            "\n## 本章小纲的信息变化意图（原文，创作参考）\n\n"
            + ctx.mini_plan_info_increment
            + "\n\n结合本章事件设计 setting/craft_focus：让信息如何被获得、验证和改变人物"
            "选择有可演出的过程。此意图不自动增加 HARD，不授权越过既定章界或世界规则。\n"
        )

    if event_contract is not None:
        mini_plan_block = (
            "## 本章事实合同（程序继承，三个输出数组均填空数组）\n"
            + event_contract.model_dump_json()
            + "\n必达事件、章末未决与禁止结果原样继承；不把摘要、体验方向或普通表达另升为 HARD。\n"
            + f"剧情概览（参考）：{ctx.mini_plan_event or ''}\n信息变化（参考）：{ctx.mini_plan_info_increment}\n"
            + f"相关人物：{linked_characters_block}\n相关伏笔：{linked_threads_block}\n"
        )
        if ctx.next_event_contract is not None:
            mini_plan_block += (
                "下一章保留的必要事件与结果："
                + json.dumps(_next_chapter_requirements(ctx), ensure_ascii=False)
                + "；本章可讨论、想象和铺垫，不可提前完成其结果。\n"
            )
    if scene_handoff:
        # 这些原文已由 CardService 放进 ctx；出卡前同样要看见，不能只在
        # 装配后交给 Writer。只搬运已有意图，不把文学焦点升为新的 HARD。
        design_fields = {
            "chapter_focus": ctx.chapter_focus,
            "arc_beats": ctx.arc_beats,
            "interaction_focus": ctx.interaction_focus,
            "breakthrough_payoff": ctx.breakthrough_payoff,
        }
        design_fields = {key: value for key, value in design_fields.items() if value}
        if design_fields:
            mini_plan_block += (
                "\n## 小纲已有的场景设计意图（逐字来源）\n\n"
                + json.dumps(design_fields, ensure_ascii=False, sort_keys=True)
                + "\n\n据此设计本章 setting 与 craft_focus；这些字段由程序原样透传，"
                "不在你的 JSON 中重述。约束级别沿用已有合同，文学焦点不另增 HARD。\n"
            )

    if ctx.beat_id or ctx.beat_must_advance or ctx.beat_forbidden_advance:
        beat_must_block = (
            "\n".join(f"  - {item}" for item in ctx.beat_must_advance)
            if ctx.beat_must_advance
            else "  (无)"
        )
        beat_forbidden_block = (
            "\n".join(f"  - {item}" for item in ctx.beat_forbidden_advance)
            if ctx.beat_forbidden_advance
            else "  (无)"
        )
        beat_contract_block = (
            "## 本章所属 beat 写前合同(HARD)\n\n"
            f"- beat_id:{ctx.beat_id or '(无)'}\n"
            f"- beat_summary:{ctx.beat_summary or '(无)'}\n"
            "- beat.must_advance(组级 HARD；本 beat 覆盖的章节合计必须推进，"
            "本章只按 mini_plan 分工承担自己的部分，禁止机械复制成每章全做):\n"
            f"{beat_must_block}\n"
            "- beat.forbidden_advance(逐章 HARD；本 beat 内每一章都不得提前触发):\n"
            f"{beat_forbidden_block}\n\n"
        )
    else:
        beat_contract_block = ""

    # v1.0.x wave 4 session 3b:前 N 章 ChapterSummary 长程事实序列段
    prev_summaries_block = _build_prev_summaries_block(
        ctx.prev_chapter_summaries,
        continuity_capsule=continuity_capsule,
    )

    # v1.0.x wave 4 session 4:同质化 retry 注入 forbidden must_achieve banner
    forbidden_block = _build_forbidden_must_achieve_block(
        ctx.forbidden_must_achieve_phrases
    )

    # v1.1 wave 5 session 1(M1):动态卡控制器注入"近章套话 avoid-list" banner
    style_block = _build_forbidden_style_block(ctx.forbidden_style_phrases)

    # v1.1 wave 4 session 3 / I2.1:风格模板 must_achieve 数量上限软提示。
    # 仅在工作区选了风格模板(ctx.default_must_achieve_max 非 None)时注入;
    # **软引导非硬截断**(must_achieve min_length>=1,硬截断有丢必达事件风险)。
    if ctx.default_must_achieve_max is not None:
        style_pref_block = (
            "## 风格模板 — 事件密度引导(I2.1)\n\n"
            f"本工作区选了写作风格模板,期望每章 ``must_achieve`` 控制在 "
            f"**{ctx.default_must_achieve_max} 项以内**(对齐模板的事件密度倾向)。"
            "请在保证本章核心事件完整的前提下,把 ``must_achieve`` 收敛到该数量级"
            "(宁可少而精,不堆砌);若剧情确需更多必达事件可适度超出,但不要无谓拆细。\n\n"
        )
    else:
        style_pref_block = ""

    if ctx.core_state_enabled:
        if ctx.core_state_projection is None:
            raise ValueError("core_state_enabled=true 时必须注入当前核心状态投影")
        current_object_ids = sorted(
            {
                fact.entity_id
                for fact in ctx.core_state_projection.facts
                if fact.entity_type == "object"
            }
        )
        current_object_id_text = (
            "、".join(current_object_ids) if current_object_ids else "(无)"
        )
        load_bearing_objects = list(_load_bearing_objects_by_id(ctx).values())
        load_bearing_object_text = (
            json.dumps(
                [item.model_dump(mode="json") for item in load_bearing_objects],
                ensure_ascii=False,
                sort_keys=True,
            )
            if load_bearing_objects
            else "[]"
        )
        core_state_block = (
            render_core_state_capsule(projection=ctx.core_state_projection).text
            + "\n\n### 承重物件 typed 稳定身份（HARD）\n\n"
            f"- 当前投影已有物件 ID:{current_object_id_text}\n"
            "- 本章 plot→beat→mini 逐字段对齐的承重物件:\n"
            f"  {load_bearing_object_text}\n"
            + "\n\n承重物件的 possession/control/access 是关系登记：本章每个 typed "
            "承重物件至少填写一条获准关系。投影尚未登记该属性时，from_value 必须为 null，"
            "即使上一章正文已提到持有人，也不能把推测的旧关系当成投影当前值。"
            "首次登记不代表此前无主或本章刚获得；只登记本章来源明确支持的关系，"
            "reason 说明该义当前依据。全书目录中的所有属性不必一次填齐，"
            "照料不推出持有，使用不推出控制，未来搬走不提前登记持有。"
            "取得经过与章末关系分开：当章取得后消耗、归还或终止的关系，不得描述为章末仍持有。"
            "优先登记章末仍成立的那一义；若必需事件本身包含短暂取得，reason 须说明随后结束，"
            "不得为了状态登记改变事件或让消耗品凭空保留。"
            "不据此强加交接、归属宣告或证明对话。本章 typed 来源明确两个物件时，"
            "允许同时首次登记这两个 ID；其余情况仍默认最多一个新物件。"
            "已有关系且本章仍保持时，from_value 与 to_value 同为投影中的角色 ID，"
            "reason 明说维持原关系；这是再次确认，不要求编造交接或争夺。"
            "实际换手才填写不同的角色 ID。关系登记仍须在正文有逐字证据，不能凭空编造。"
            "\n\n非关系核心状态只在本章 mini_plan/beat 明确要求发生客观、持久、承重的因果变化时，"
            "才可输出 `core_state_transitions`。每项必须完整输出 `entity_type`、"
            "`entity_id`、`attribute`、`from_value`、`to_value`、`reason`；不要输出"
            " `state_key`，系统会由前三项生成。`entity_type=object` 只允许属性 "
            "`physical_form/surface/location/integrity/seal_status/open_state/"
            "possession/control/access`；`holder` 只供历史卡读取，新输出禁止；"
            "`entity_type=character` 只允许属性 "
            "`identity/life_status/permanent_injury/amnesia/faction`，且 `entity_id`"
            " 必须取上方 character_voices 的稳定 ID。已有当前值时 `from_value` 必须"
            "逐字等于上方值；该实体属性尚无当前事实时必须写 `from_value=null`，不得"
            "猜造旧值。关系属性的 `from_value/to_value` 只写角色 stable ID，"
            "其中 `to_value` 必须来自 character_voices；不得合并三义或写位置协议。"
            "非关系 `to_value` 写变化后的客观状态并说明上游规划理由；普通外观描写、"
            "情绪、猜测、对白、梦境或临时动作不得申报。"
            "每章最多 2 项；没有本章承重关系且没有明确变化时输出空数组。\n\n"
        )
    else:
        core_state_block = ""

    word_min = ctx.default_word_count_min or ChapterCard.model_fields["word_count_min"].default
    word_max = ctx.default_word_count_max or ChapterCard.model_fields["word_count_max"].default
    length_design = (
        "## 本章实际篇幅与设计职责\n\n"
        + (f"本章参考篇幅约 {word_min} 字，不是最低字数；不把约数变成必须填满的容量。"
           if ctx.word_count_policy_version == 2 else
           f"正文名义区间 {word_min}–{word_max} 字，起草中心约 {(word_min + word_max) // 2} 字。")
        + "这是你设计完整章节的容量背景；数值由程序继承，不要在 JSON 中另设字数。"
        "围绕已有本章事件安排足以展开的因果过程、人物回应和局面变化，"
        "让 Writer 有依据把关键过程写透。不能靠增加 must 条数、重复概括、"
        "新增支线或提前兑现后章内容凑篇幅；也不要把篇幅均摊给每条 must。\n\n"
    )
    return (
        setting_conflict_retry_banner
        + core_state_retry_banner
        + continuity_retry_banner
        + retry_banner
        + forbidden_block
        + style_block
        + length_design
        + "## 章节定位(系统字段,LLM 不产)\n\n"
        f"- workspace_id:{ctx.workspace_id}\n"
        f"- chapter_id:{ctx.chapter_id}\n"
        f"- volume_id:{ctx.volume_id}\n"
        f"- chapter_number:{ctx.chapter_number}(卷内 1-based)\n\n"
        "## 卷规划级硬约束(预注入,LLM 不能改 + 不要在输出 JSON 里产)\n\n"
        f"- arc_id:{ctx.arc_id}\n"
        f"- arc_type:{ctx.arc_type}\n"
        f"- arc_phase:{ctx.arc_phase}(5 档之一)\n"
        f"- arc_summary:{ctx.arc_summary}\n"
        f"- is_arc_finale:{str(ctx.is_arc_finale).lower()}\n"
        f"- tension_level:{ctx.tension_level}(1-10)\n\n"
        + beat_contract_block
        + drama_block
        + mini_plan_block
        + core_state_block
        + style_pref_block
        + "## outline 上下文\n\n"
        + ctx.setting_contract.render_for_prompt()
        + "\n"
        f"- core_conflict:{ctx.core_conflict}\n"
        f"- volume_summary:{ctx.volume_summary}\n\n"
        + prev_block
        + prev_summaries_block
        + ctx.warning_watchlist_section
        + ("\n\n" if ctx.warning_watchlist_section else "")
        + "\n## 角色 voice hints(pov_character / related_characters 必从此清单选)\n\n"
        + "\n".join(char_lines)
        + character_material
        + name_guard_block
        + "\n\n## 当前活跃伏笔(related_plot_threads 必从此清单选)\n\n"
        + "\n".join(thread_lines)
        + emergent_selection_block
        + "\n\n请按 system prompt 字面 JSON schema 产单章卡片;"
        f"chapter_id={ctx.chapter_id} / chapter_number={ctx.chapter_number}。"
    )


# ---------------------------------------------------------------------------
# JSON 解析 + Pydantic 校验
# ---------------------------------------------------------------------------


def _parse_llm_payload(
    *,
    raw_text: str,
    model_used: str,
    chapter_id: str,
) -> _ChapterCardDecisionPayload:
    """剥代码块 + trailing → ``json.loads`` → ``_ChapterCardDecisionPayload``
    校验。任一失败抛 ``ChapterCardGeneratorError`` + 落 ``chapter_card_generator_failed``
    structlog 事件。
    """
    text = _strip_format_noise(raw_text).strip()

    try:
        data = loads_with_quote_repair(
            text,
            agent_name=_AGENT_NAME,
            model_used=model_used,
        )
    except json.JSONDecodeError as exc:
        _log.error(
            "chapter_card_generator_failed",
            reason="invalid_json",
            chapter_id=chapter_id,
            raw_output=raw_text,
            model_used=model_used,
            json_error=str(exc),
        )
        raise ChapterCardGeneratorError(
            f"ChapterCardGenerator chapter_id={chapter_id}:LLM 输出非合法 JSON",
            raw_output=raw_text,
            model_used=model_used,
        ) from exc

    try:
        return _ChapterCardDecisionPayload.model_validate(data)
    except ValidationError as exc:
        _log.error(
            "chapter_card_generator_failed",
            reason="schema_mismatch",
            chapter_id=chapter_id,
            raw_output=raw_text,
            model_used=model_used,
            validation_errors=exc.errors(),
        )
        raise ChapterCardGeneratorError(
            f"ChapterCardGenerator chapter_id={chapter_id}:"
            "LLM 输出 JSON 结构与 schema 不符(可能 Literal 字段越界)",
            raw_output=raw_text,
            model_used=model_used,
        ) from exc


# ---------------------------------------------------------------------------
# 跨字段引用完整性校验 + 装配
# ---------------------------------------------------------------------------


def _iter_card_character_reference_texts(
    payload: _ChapterCardDecisionPayload,
) -> list[tuple[str, str]]:
    """返回章节卡中会下沉给 writer / 后续卡的硬任务文本。

    C-4 只扫这些"任务书字段",不扫 outline 背景全文:目标是拦住卡层把未知
    命名角色写进 must/forbidden/seed,避免 writer 忠实执行错卡。
    """
    rows: list[tuple[str, str]] = [
        ("title", payload.title),
        ("setting", payload.setting),
        ("craft_focus", payload.craft_focus),
        ("next_chapter_seed", payload.next_chapter_seed),
    ]
    rows.extend(
        (f"must_achieve[{idx}]", value)
        for idx, value in enumerate(payload.must_achieve)
    )
    rows.extend(
        (f"must_unresolved[{idx}]", value)
        for idx, value in enumerate(payload.must_unresolved)
    )
    rows.extend(
        (f"forbidden[{idx}]", value) for idx, value in enumerate(payload.forbidden)
    )
    return [(field, text) for field, text in rows if text]


def _surname_prefix(text: str) -> str | None:
    """从一个中文命名称谓里取姓氏前缀(复姓优先)。"""
    for surname in _COMPOUND_SURNAMES:
        if text.startswith(surname):
            return surname
    if text and text[0] in _SURNAMES:
        return text[0]
    return None


def _validate_card_named_character_references(
    *,
    payload: _ChapterCardDecisionPayload,
    context: ChapterCardGenerationContext,
    raw_output: str,
    model_used: str,
) -> None:
    """C-4:章节卡硬任务文本里的命名角色必须闭环到角色清单。

    两条低误杀规则:
    1. 硬任务文本里出现已知角色**全名**时,该角色必须在
       ``related_characters`` / ``pov_character`` 内。否则 writer 看不到完整角色
       档案,容易把任务书里的名字写串。
    2. 硬任务文本里出现"姓氏+职称"命名称谓(如"苏教授")时,该姓氏必须能对应
       本章出场角色;否则视为卡层凭空造名,直接失败。普通无名路人不受影响。
    """
    id_by_name = {
        voice.name: voice.character_id
        for voice in context.character_voices
        if voice.name
    }
    allowed_ids = set(payload.related_characters)
    allowed_ids.add(payload.pov_character)
    allowed_names = {name for name, cid in id_by_name.items() if cid in allowed_ids}
    allowed_surnames = {
        surname
        for name in allowed_names
        if (surname := _surname_prefix(name)) is not None
    }

    missing_related: list[str] = []
    unknown_titled: list[str] = []
    for field, text in _iter_card_character_reference_texts(payload):
        for name, cid in id_by_name.items():
            if name in text and cid not in allowed_ids:
                missing_related.append(f"{field}: {name}({cid})")

        for match in _TITLED_PERSON_RE.finditer(text):
            mention = match.group(1)
            # Directional compounds end in 向, which is also a surname.
            # “通向导师去过的地方” contains a generic mentor, not 向导师.
            # Bare “向导师来信” remains a titled-name candidate; full-name
            # membership above is always checked, including after compounds.
            if mention.startswith("向") and text[:match.start()].endswith(
                ("通", "指", "朝", "面", "走", "转", "奔", "驶", "望")
            ):
                continue
            if any(name in mention for name in id_by_name):
                continue
            # “关于导师陆明轩……”会从介词“于”起误命中“于导师”。职称后
            # 紧接本章已知全名时，这是“泛称 + 确切姓名”的同位语，不是新造
            # “姓氏 + 职称”。只放行紧邻的已知全名；单独的“于导师”仍须拦截。
            if any(text.startswith(name, match.end()) for name in allowed_names):
                continue
            surname = _surname_prefix(mention)
            if surname is not None and surname not in allowed_surnames:
                unknown_titled.append(f"{field}: {mention}")

    if not missing_related and not unknown_titled:
        return

    _log.error(
        "chapter_card_generator_failed",
        reason="named_character_reference_invalid",
        chapter_id=context.chapter_id,
        missing_related=missing_related,
        unknown_titled_mentions=unknown_titled,
        related_characters=list(payload.related_characters),
        known_character_names=sorted(id_by_name),
        model_used=model_used,
    )
    raise ChapterCardGeneratorError(
        f"ChapterCardGenerator chapter_id={context.chapter_id}:"
        "章节卡硬任务文本引用了未闭环的命名角色。"
        f"已知角色未列入 related_characters: {missing_related}; "
        f"疑似未知命名称谓: {unknown_titled}。"
        "请改用 character_voices 中已确立角色的确切姓名,并把该角色加入 related_characters; "
        "不得在 must_achieve / forbidden / setting / next_chapter_seed 中另起新名。",
        raw_output=raw_output,
        model_used=model_used,
    )


def _validate_cross_field_references(
    *,
    payload: _ChapterCardDecisionPayload,
    context: ChapterCardGenerationContext,
    raw_output: str,
    model_used: str,
) -> None:
    """LLM 后置校验:``pov_character`` / ``related_characters`` 必在
    ``character_voices`` 内;``related_plot_threads`` 必在 ``plot_threads`` 内。

    任一不通过抛 ``ChapterCardGeneratorError``,避免 LLM 编造未列出的 ID
    渗入 ``ChapterCard`` 落库后破坏跨表引用完整性。
    """
    valid_character_ids = {c.character_id for c in context.character_voices}
    valid_thread_ids = {t.thread_id for t in context.plot_threads}
    if context.narrative_context is not None:
        modes = {p.character_id: p.mode for p in payload.participation}
        if (len(modes) != len(payload.participation)
                or set(modes) != set(payload.related_characters)
                or modes.get(payload.pov_character) != "present"):
            raise ChapterCardGeneratorError(
                "人物参与分类必须逐一覆盖 related_characters 且主视角必须 present；"
                "禁止把提及、回忆或限制人物默认为当场出场",
                raw_output=raw_output, model_used=model_used,
                error_code=CHAPTER_CARD_REFERENCE_ERROR_CODE,
                details={"participation_ids": list(modes),
                         "required_character_ids": list(payload.related_characters)},
            )

    if payload.pov_character not in valid_character_ids:
        _log.error(
            "chapter_card_generator_failed",
            reason="pov_character_unknown",
            chapter_id=context.chapter_id,
            pov_character=payload.pov_character,
            valid_character_ids=sorted(valid_character_ids),
            model_used=model_used,
        )
        raise ChapterCardGeneratorError(
            f"ChapterCardGenerator chapter_id={context.chapter_id}:"
            f"pov_character={payload.pov_character!r} 不在 character_voices 列表内",
            raw_output=raw_output,
            model_used=model_used,
            error_code=CHAPTER_CARD_REFERENCE_ERROR_CODE,
            details={
                "unknown_character_ids": [payload.pov_character],
                "unknown_thread_ids": [],
            },
        )

    unknown_chars = [
        cid for cid in payload.related_characters if cid not in valid_character_ids
    ]
    if unknown_chars:
        _log.error(
            "chapter_card_generator_failed",
            reason="related_characters_unknown",
            chapter_id=context.chapter_id,
            unknown_character_ids=unknown_chars,
            valid_character_ids=sorted(valid_character_ids),
            model_used=model_used,
        )
        raise ChapterCardGeneratorError(
            f"ChapterCardGenerator chapter_id={context.chapter_id}:"
            f"related_characters 含未知 ID {unknown_chars}(必须在 character_voices 内)",
            raw_output=raw_output,
            model_used=model_used,
            error_code=CHAPTER_CARD_REFERENCE_ERROR_CODE,
            details={
                "unknown_character_ids": unknown_chars,
                "unknown_thread_ids": [],
            },
        )

    unknown_transition_chars = list(
        dict.fromkeys(
            transition.entity_id
            for transition in payload.core_state_transitions
            if transition.entity_type == "character"
            and transition.entity_id not in valid_character_ids
        )
    )
    if unknown_transition_chars:
        _log.error(
            "chapter_card_generator_failed",
            reason="core_state_transition_character_unknown",
            chapter_id=context.chapter_id,
            unknown_character_ids=unknown_transition_chars,
            valid_character_ids=sorted(valid_character_ids),
            model_used=model_used,
        )
        raise ChapterCardGeneratorError(
            f"ChapterCardGenerator chapter_id={context.chapter_id}:"
            "core_state_transitions 的 character entity_id 含未知 ID "
            f"{unknown_transition_chars}(必须在 character_voices 内)",
            raw_output=raw_output,
            model_used=model_used,
            error_code=CHAPTER_CARD_REFERENCE_ERROR_CODE,
            details={
                "unknown_character_ids": unknown_transition_chars,
                "unknown_thread_ids": [],
            },
        )

    if payload.pov_character not in payload.related_characters:
        _log.error(
            "chapter_card_generator_failed",
            reason="pov_character_not_in_related",
            chapter_id=context.chapter_id,
            pov_character=payload.pov_character,
            related_characters=payload.related_characters,
            model_used=model_used,
        )
        raise ChapterCardGeneratorError(
            f"ChapterCardGenerator chapter_id={context.chapter_id}:"
            f"pov_character={payload.pov_character!r} 必须出现在 related_characters 列表内",
            raw_output=raw_output,
            model_used=model_used,
        )

    unknown_threads = [
        tid for tid in payload.related_plot_threads if tid not in valid_thread_ids
    ]
    if unknown_threads:
        _log.error(
            "chapter_card_generator_failed",
            reason="related_plot_threads_unknown",
            chapter_id=context.chapter_id,
            unknown_thread_ids=unknown_threads,
            valid_thread_ids=sorted(valid_thread_ids),
            model_used=model_used,
        )
        raise ChapterCardGeneratorError(
            f"ChapterCardGenerator chapter_id={context.chapter_id}:"
            f"related_plot_threads 含未知 ID {unknown_threads}(必须在 plot_threads 内)",
            raw_output=raw_output,
            model_used=model_used,
            error_code=CHAPTER_CARD_REFERENCE_ERROR_CODE,
            details={
                "unknown_character_ids": [],
                "unknown_thread_ids": unknown_threads,
            },
        )

    # 衔接桥 pov_to 必须 == 顶层 pov_character(系统约束)
    if payload.continuity_context.pov_to != payload.pov_character:
        _log.error(
            "chapter_card_generator_failed",
            reason="continuity_pov_to_mismatch",
            chapter_id=context.chapter_id,
            pov_character=payload.pov_character,
            continuity_pov_to=payload.continuity_context.pov_to,
            model_used=model_used,
        )
        raise ChapterCardGeneratorError(
            f"ChapterCardGenerator chapter_id={context.chapter_id}:"
            f"continuity_context.pov_to={payload.continuity_context.pov_to!r} 必须 == "
            f"pov_character={payload.pov_character!r}",
            raw_output=raw_output,
            model_used=model_used,
        )

    # v1.1 audit G19b 治本(2026-05-27 b1-rerun-deep-audit):forbidden 字段风格污染检测。
    # 风格 / 排版 / 句长 / 段数 / 内心戏密度 / 修辞密度 / 节奏类约束归 writer.md +
    # voice_profile 处理,塞进 forbidden 会与 writer 节奏纪律产生矛盾叠加触发 mode
    # collapse(luo-clean-v1 ch-5 rerun-4 实证现场,详 _FORBIDDEN_STYLE_KEYWORDS 注释)。
    polluted = _detect_forbidden_style_pollution(payload.forbidden)
    if polluted:
        _log.error(
            "chapter_card_generator_failed",
            reason="forbidden_style_pollution",
            chapter_id=context.chapter_id,
            polluted_entries=polluted,
            model_used=model_used,
        )
        raise ChapterCardGeneratorError(
            f"ChapterCardGenerator chapter_id={context.chapter_id}:"
            f"forbidden 字段含风格 / 排版 / 节奏类约束(G19 治本):{polluted}。"
            "forbidden 只放剧情禁元素(现代用语 / 跨域穿帮 / 越界角色 / 越界设定 / "
            "voice forbidden 等),风格约束归 writer.md + voice_profile 处理。"
            "请改写 forbidden 移除上述污染条目,或改用纯剧情禁元素描述。",
            raw_output=raw_output,
            model_used=model_used,
        )

    _validate_card_named_character_references(
        payload=payload,
        context=context,
        raw_output=raw_output,
        model_used=model_used,
    )


_EMERGENT_PAYOFF_MARKER_RE = re.compile(
    r"^\[临时伏笔收口:(?P<thread_id>[A-Za-z0-9_-]+)\](?P<body>.*)$"
)


def _validate_emergent_plot_thread_selection(
    *,
    payload: _ChapterCardDecisionPayload,
    context: ChapterCardGenerationContext,
    raw_output: str,
    model_used: str,
) -> None:
    """临时伏笔只能自选 0-1 条收口，且必须显式硬化为本章 must。

    大纲/人工伏笔完全不参加本校验，仍沿用既有 ``related_plot_threads`` 规划；
    因此临时层不会重审、降级或挤掉主伏笔。
    """
    emergent_ids = {
        thread.thread_id
        for thread in context.plot_threads
        if thread.origin == "emergent"
    }
    selected_emergent = [
        thread_id
        for thread_id in payload.related_plot_threads
        if thread_id in emergent_ids
    ]
    markers: list[tuple[str, str]] = []
    for must in payload.must_achieve:
        match = _EMERGENT_PAYOFF_MARKER_RE.match(must.strip())
        if match is not None:
            markers.append((match.group("thread_id"), match.group("body").strip()))

    violations: list[str] = []
    if len(selected_emergent) > 1:
        violations.append(f"临时伏笔一次最多收口 1 条，实际选择 {selected_emergent}")
    marker_ids = [thread_id for thread_id, _ in markers]
    if len(marker_ids) != len(set(marker_ids)):
        violations.append("同一临时伏笔收口 marker 重复")
    for thread_id in selected_emergent:
        if thread_id not in marker_ids:
            violations.append(
                f"选择临时伏笔 {thread_id} 后缺少 "
                f"[临时伏笔收口:{thread_id}] must_achieve"
            )
    for thread_id, body in markers:
        if thread_id not in selected_emergent:
            violations.append(
                f"收口 marker {thread_id} 未同时出现在 related_plot_threads"
            )
        if thread_id not in emergent_ids:
            violations.append(f"收口 marker {thread_id} 不是临时伏笔候选")
        if len(body) < 8:
            violations.append(f"收口 marker {thread_id} 后必须写明具体行动与结果")
    if not violations:
        return

    _log.error(
        "chapter_card_generator_failed",
        reason="emergent_plot_thread_selection_invalid",
        chapter_id=context.chapter_id,
        selected_emergent_ids=selected_emergent,
        marker_ids=marker_ids,
        violations=violations,
        model_used=model_used,
    )
    raise ChapterCardGeneratorError(
        f"ChapterCardGenerator chapter_id={context.chapter_id}:"
        "临时伏笔收口合同不合法:" + "；".join(violations),
        raw_output=raw_output,
        model_used=model_used,
        error_code="EMERGENT_PLOT_THREAD_SELECTION_INVALID",
        details={
            "selected_emergent_ids": selected_emergent,
            "marker_ids": marker_ids,
            "violations": violations,
        },
    )


def _merge_related_characters(
    related_characters: list[str],
    required_characters: list[str],
) -> list[str]:
    """保序合并 LLM related 与上游必含角色,不引入未声明角色。"""
    merged: list[str] = []
    for cid in [*related_characters, *required_characters]:
        if cid and cid not in merged:
            merged.append(cid)
    return merged


def _referenced_known_character_ids(
    *,
    payload: _ChapterCardDecisionPayload,
    context: ChapterCardGenerationContext,
) -> list[str]:
    """从章节卡硬任务文本里抽取已知角色全名,用于补齐 related_characters。"""
    id_by_name = {
        voice.name: voice.character_id
        for voice in context.character_voices
        if voice.name
    }
    referenced: list[str] = []
    for _, text in _iter_card_character_reference_texts(payload):
        for name, cid in id_by_name.items():
            if cid == payload.pov_character:
                continue
            if name in text and cid not in referenced:
                referenced.append(cid)
    return referenced


def _payload_with_closed_related_characters(
    *,
    payload: _ChapterCardDecisionPayload,
    context: ChapterCardGenerationContext,
    model_used: str,
    source: str,
) -> _ChapterCardDecisionPayload:
    """C-6/C-4:上游声明角色 + hard task 已知命名角色必须闭环到 related。"""
    required = _merge_related_characters(
        list(context.mini_plan_linked_characters),
        _referenced_known_character_ids(payload=payload, context=context),
    )
    if context.narrative_context is not None:
        extra_ids = []
        if context.mini_plan_dramatic_contract is not None:
            extra_ids.extend([context.mini_plan_dramatic_contract.actor_id,
                              context.mini_plan_dramatic_contract.counterforce_id])
        reference_text = "\n".join([*_inherited_chapter_requirements(context), *context.beat_forbidden_advance,
                                    context.mini_plan_event or ""])
        extra_ids.extend(v.character_id for v in context.character_voices if v.name in reference_text)
        required = _merge_related_characters(required, extra_ids)
    if not required:
        return payload

    merged = _merge_related_characters(list(payload.related_characters), required)
    if merged == list(payload.related_characters):
        return payload

    added = [cid for cid in required if cid not in payload.related_characters]
    _log.info(
        "chapter_card_related_characters_completed_from_context",
        source=source,
        chapter_id=context.chapter_id,
        workspace_id=context.workspace_id,
        added_character_ids=added,
        mini_plan_linked_characters=list(context.mini_plan_linked_characters),
        related_characters_before=list(payload.related_characters),
        related_characters_after=merged,
        model_used=model_used,
    )
    return payload.model_copy(update={"related_characters": merged})


def _payload_with_closed_related_plot_threads(
    *,
    payload: _ChapterCardDecisionPayload,
    context: ChapterCardGenerationContext,
    model_used: str,
    source: str,
) -> _ChapterCardDecisionPayload:
    """上游 mini-plan 已分配的主伏笔必须闭环到章节卡。"""
    required = list(context.mini_plan_linked_threads)
    if not required:
        return payload

    merged = _merge_related_characters(list(payload.related_plot_threads), required)
    if merged == list(payload.related_plot_threads):
        return payload

    added = [
        thread_id
        for thread_id in required
        if thread_id not in payload.related_plot_threads
    ]
    _log.info(
        "chapter_card_related_plot_threads_completed_from_context",
        source=source,
        chapter_id=context.chapter_id,
        workspace_id=context.workspace_id,
        added_thread_ids=added,
        mini_plan_linked_threads=required,
        related_plot_threads_before=list(payload.related_plot_threads),
        related_plot_threads_after=merged,
        model_used=model_used,
    )
    return payload.model_copy(update={"related_plot_threads": merged})


def _assemble_chapter_card(
    *,
    payload: _ChapterCardDecisionPayload,
    context: ChapterCardGenerationContext,
) -> ChapterCard:
    """合并 caller 系统字段 + LLM 创意字段为最终 ``ChapterCard``。

    可选 override 字段(``word_count_min`` / ``word_count_max`` /
    ``plot_node_budget``)若 LLM 未给则走 ``ChapterCard`` 默认值;
    ``card_status`` 默认 ``"draft"``,``card_approved_at`` 默认 ``None``
    (对齐 P5-B-02 ``ChapterCard.card_status`` 字段语义)。
    """
    extra: dict = {}
    # word_count 兜底优先级:LLM payload > 工作区 generation 默认(L1)> ChapterCard 模型默认。
    if payload.word_count_min is not None:
        extra["word_count_min"] = payload.word_count_min
    elif context.default_word_count_min is not None:
        extra["word_count_min"] = context.default_word_count_min
    if payload.word_count_max is not None:
        extra["word_count_max"] = payload.word_count_max
    elif context.default_word_count_max is not None:
        extra["word_count_max"] = context.default_word_count_max
    # max_rewrites:LLM 不产该字段;工作区 generation 默认(L1)> ChapterCard 模型默认。
    if context.default_max_rewrites is not None:
        extra["max_rewrites"] = context.default_max_rewrites
    # plot_node_budget 兜底优先级:LLM payload > 工作区 generation 默认(I2.1)> ChapterCard 模型默认。
    if payload.plot_node_budget is not None:
        extra["plot_node_budget"] = payload.plot_node_budget
    elif context.default_plot_node_budget is not None:
        extra["plot_node_budget"] = context.default_plot_node_budget

    # v1.0 hot-fix bug ⑤:按 tension_level 注入 tension-aware 阈值,替代
    # ChapterCard 字段默认 7.5/6.5。max_rewrites / max_revises 沿用字段默认值。
    review_threshold, revise_threshold = _default_thresholds_for_tension(
        context.tension_level
    )

    dramatic_source = None
    must_achieve = list(payload.must_achieve)
    related_characters = list(payload.related_characters)
    related_plot_threads = list(payload.related_plot_threads)
    if context.mini_plan_dramatic_contract is not None:
        if (
            context.volume_dramatic_promise is None
            or context.beat_dramatic_contract is None
            or context.beat_id is None
        ):
            raise ChapterCardGeneratorError(
                "dramatic typed source 不完整",
                raw_output="",
                model_used="deterministic_assembly",
            )
        dramatic_source = build_dramatic_source_binding(
            volume_id=context.volume_id,
            beat_id=context.beat_id,
            promise=context.volume_dramatic_promise,
            beat_contract=context.beat_dramatic_contract,
            mini_plan_contract=context.mini_plan_dramatic_contract,
        )
        for requirement in context.mini_plan_dramatic_contract.natural_requirements():
            if requirement not in must_achieve:
                must_achieve.append(requirement)
        for character_id in (
            context.mini_plan_dramatic_contract.actor_id,
            context.mini_plan_dramatic_contract.counterforce_id,
        ):
            if character_id not in related_characters:
                related_characters.append(character_id)
        drama_thread = context.mini_plan_dramatic_contract.thread_id
        if drama_thread and drama_thread not in related_plot_threads:
            related_plot_threads.append(drama_thread)

    # v2 W3.5 补强3(确定性后处理 B):强化 forbidden 中"提前[进入类]"弱措辞为
    # 时间无条件强边界(纯措辞补强,不新增被禁动作;must_achieve 含进入类动词时跳过)。
    # 所有单卡入口共用本组装函数;变更时落 structlog 供观测。
    event_contract = context.narrative_context.event_contract if context.narrative_context is not None else None
    if event_contract is not None:
        if (list(payload.must_achieve) != _inherited_chapter_requirements(context)
                or list(payload.must_unresolved) != event_contract.unresolved_questions
                or list(payload.forbidden) != event_contract.forbidden_results):
            raise ValueError("叙事事实合同漂移：不能在出卡中增删上游事实")
    _strengthened_forbidden = (list(payload.forbidden) if event_contract is not None else
        _strengthen_boundary_forbidden(list(payload.forbidden), list(payload.must_achieve)))
    if _strengthened_forbidden != list(payload.forbidden):
        _log.info(
            "chapter_card_boundary_forbidden_strengthened",
            chapter_id=context.chapter_id,
            workspace_id=context.workspace_id,
            before=list(payload.forbidden),
            after=_strengthened_forbidden,
        )

    # §2.137:该确定性注入统一在共享 assemble 里消费 context，普通单卡、引用
    # 重试与失效卡恢复都先在 prompt 看见原文，再以同一原文执行既有窄传播规则。
    _final_forbidden = (
        list(dict.fromkeys([*_strengthened_forbidden, *context.beat_forbidden_advance]))
        if event_contract is not None else _propagate_forbidden_advance_entry(
            _strengthened_forbidden, list(payload.must_achieve), list(context.beat_forbidden_advance)))
    if _final_forbidden != _strengthened_forbidden:
        _log.info(
            "chapter_card_forbidden_advance_propagated",
            chapter_id=context.chapter_id,
            workspace_id=context.workspace_id,
            beat_id=context.beat_id,
            before=_strengthened_forbidden,
            after=_final_forbidden,
        )

    next_event = "；".join(_next_chapter_requirements(context))
    if next_event:
        holdback = (
            "本章不得完整触发、完成或揭示下一章保留事件："
            f"{next_event}；"
            + ("允许不造成这些结果落地的讨论、感受与必要铺垫" if event_contract is not None else
               "只允许不造成结果落地的必要铺垫")
        )
        if holdback not in _final_forbidden:
            _final_forbidden.append(holdback)
            _log.info(
                "chapter_card_next_event_holdback_propagated",
                chapter_id=context.chapter_id,
                workspace_id=context.workspace_id,
                next_mini_plan_event=context.next_mini_plan_event,
            )

    continuity_context = payload.continuity_context
    if context.continuity_ledger_provenance is not None:
        # R4:直接前章事实不是创意字段。即使 LLM 改写或漏抄，也以已验证 ledger
        # 投影覆盖；approve / Writer 仍会复核这些直接投影，覆盖上传卡和落盘后
        # 篡改路径。这里的 POV 是否切换只表示章节边界两侧的主视角角色是否
        # 不同，不授权章内切换或叙述人称变化；它由前章 ledger POV 与本章权威
        # POV 机械派生，不能让卷内 chapter_number=1 诱导模型把跨卷续章误写成
        # 全书首章。
        pov_change = (
            context.prev_pov_character is not None
            and context.prev_pov_character != payload.pov_character
        )
        normalized_time_gap = continuity_context.time_gap
        if normalized_time_gap.strip() in FIRST_CHAPTER_TIME_GAP_SENTINELS:
            if continuity_context.scene_continuation == "same":
                normalized_time_gap = "立即续接"
            elif continuity_context.scene_continuation == "switch":
                normalized_time_gap = "紧接上一章（跨场景续接）"
            else:
                normalized_time_gap = "承接上一章后的时间跳跃"
            _log.warning(
                "chapter_card_nonfirst_time_gap_normalized",
                chapter_id=context.chapter_id,
                workspace_id=context.workspace_id,
                before=continuity_context.time_gap,
                after=normalized_time_gap,
                scene_continuation=continuity_context.scene_continuation,
            )
        continuity_context = continuity_context.model_copy(
            update={
                "prev_chapter_tail": context.prev_chapter_tail or "",
                "unfinished_actions": list(context.prev_unfinished_actions),
                "time_gap": normalized_time_gap,
                "pov_from": context.prev_pov_character,
                "pov_to": payload.pov_character,
                "pov_change": pov_change,
            }
        )

    continuity_context = continuity_context.model_copy(update={
        "immediate_action_indices": _frozen_immediate_indices(context),
    })
    core_state_contract_version: Literal[0, 1] = 0
    core_state_projection_hash = ""
    core_state_transitions: list[CoreStateTransition] = []
    book_clock_constraint: BookClockConstraint | None = None
    if context.core_state_enabled:
        if context.core_state_projection is None:
            raise ChapterCardGeneratorError(
                "启用核心状态合同时缺少当前投影",
                raw_output="",
                model_used="deterministic_assembly",
            )
        book_clock_constraint = build_typed_book_clock_constraint(
            chapter_id=context.chapter_id,
            time_gap=continuity_context.time_gap,
            previous_projection=context.core_state_projection.book_time,
            previous_projection_hash=context.core_state_projection.projection_hash,
            is_first_chapter=context.chapter_id == "ch-0001",
        )
        # 把候选迁移也计入同一注意力预算；超限直接失败，禁止静默截断。
        render_core_state_capsule(
            projection=context.core_state_projection,
            transitions=payload.core_state_transitions,
            book_clock_constraint=book_clock_constraint,
        )
        core_state_contract_version = 1
        core_state_projection_hash = context.core_state_projection.projection_hash
        core_state_transitions = list(payload.core_state_transitions)

    assembled = ChapterCard(
        chapter_id=context.chapter_id,
        workspace_id=context.workspace_id,
        volume_id=context.volume_id,
        chapter_number=context.chapter_number,
        title=payload.title,
        tension_level=context.tension_level,
        word_count_policy_version=context.word_count_policy_version,
        must_achieve=must_achieve,
        must_unresolved=list(payload.must_unresolved),
        forbidden=_final_forbidden,
        pov_character=payload.pov_character,
        setting=payload.setting,
        setting_contract_hash=context.setting_contract.source_hash,
        dramatic_source=dramatic_source,
        continuity_ledger_provenance=context.continuity_ledger_provenance,
        core_state_contract_version=core_state_contract_version,
        core_state_projection_hash=core_state_projection_hash,
        core_state_transitions=core_state_transitions,
        book_clock_constraint=book_clock_constraint,
        # M2 纠正侧(v1.1 wave 5 s3):per-scene 卓越性焦点直接透传(LLM 产出,
        # 旧输出无此字段 → payload default "" → Writer 跳过本段,零回归)。
        craft_focus=payload.craft_focus,
        narrative_context=(context.narrative_context.model_copy(update={
            "participation": list(payload.participation),
        }) if context.narrative_context is not None else None),
        continuity_context=continuity_context,
        next_chapter_seed=payload.next_chapter_seed,
        arc_phase=context.arc_phase,
        is_arc_finale=context.is_arc_finale,
        related_plot_threads=related_plot_threads,
        # G2(v1.1 wave 4 session 2):callback_to 确定性派生自 mini_plan
        # callback_anchors(非 LLM 产出),Writer 据此绕 embedding 精确 lookup 伏笔。
        callback_to=list(context.callback_anchors),
        # M4'''(v1.1 wave 5 session 6):chapter_focus 确定性透传自 mini_plan
        # chapter_focus(非 card LLM 产出,避免重述上游 / 变异 —— 轴B 教训:结构形态
        # 早在 outline 层定,card 只透传)。空 → Writer 跳过本段,逐字节零回归。
        chapter_focus=context.chapter_focus,
        # M5(v1.1 wave 5 session 7):arc_beats 确定性透传自 mini_plan arc_beats
        # (非 card LLM 产出,避免变异 —— 同 chapter_focus 范式)。空 dict → Writer
        # 跳过本段,逐字节零回归。
        arc_beats=dict(context.arc_beats),
        # M4''-轴A(v1.1 wave 5 session 8):interaction_focus 确定性透传自 mini_plan
        # interaction_focus(非 card LLM 产出,避免变异 —— 同 chapter_focus 范式)。
        # 空 → Writer 跳过本段,逐字节零回归。
        interaction_focus=context.interaction_focus,
        # 模块①(v1.1 wave 5 session 10):breakthrough_payoff 确定性透传自 mini_plan
        # breakthrough_payoff(非 card LLM 产出 —— 同 chapter_focus 范式)。空 → Writer
        # 跳过本段,逐字节零回归。
        breakthrough_payoff=context.breakthrough_payoff,
        related_characters=related_characters,
        review_threshold=review_threshold,
        revise_threshold=revise_threshold,
        # card_status / card_approved_at / max_rewrites / max_revises 走
        # ChapterCard 字段默认值(分别 "draft" / None / 3 / 2)。
        **extra,
    )

    frozen = context.continuity_bridge_retry_base_card
    if frozen is None:
        return assembled
    if not context.continuity_bridge_retry_actions:
        raise ChapterCardGeneratorError(
            "continuity bridge 定向重试缺少确定性失败证据",
            raw_output="",
            model_used="deterministic_assembly",
            error_code="CHAPTER_CARD_CONTINUITY_RETRY_INVALID",
        )
    if frozen.card_status == "approved":
        raise ChapterCardGeneratorError(
            "approved 章节卡不得作为自动补桥重试基线",
            raw_output="",
            model_used="deterministic_assembly",
            error_code="CHAPTER_CARD_CONTINUITY_RETRY_INVALID",
        )
    frozen_identity = (
        frozen.workspace_id,
        frozen.chapter_id,
        frozen.volume_id,
        frozen.chapter_number,
    )
    context_identity = (
        context.workspace_id,
        context.chapter_id,
        context.volume_id,
        context.chapter_number,
    )
    if frozen_identity != context_identity:
        raise ChapterCardGeneratorError(
            "continuity bridge 定向重试基线与当前章节身份不一致",
            raw_output="",
            model_used="deterministic_assembly",
            error_code="CHAPTER_CARD_CONTINUITY_RETRY_INVALID",
        )
    if frozen.continuity_ledger_provenance != context.continuity_ledger_provenance:
        raise ChapterCardGeneratorError(
            "continuity bridge 定向重试期间前章账本来源发生漂移",
            raw_output="",
            model_used="deterministic_assembly",
            error_code="CHAPTER_CARD_CONTINUITY_RETRY_INVALID",
        )
    expected_projection_hash = (
        context.core_state_projection.projection_hash
        if context.core_state_enabled and context.core_state_projection is not None
        else frozen.core_state_projection_hash
    )
    if frozen.core_state_projection_hash != expected_projection_hash:
        raise ChapterCardGeneratorError(
            "continuity bridge 定向重试期间核心状态投影发生漂移",
            raw_output="",
            model_used="deterministic_assembly",
            error_code="CHAPTER_CARD_CONTINUITY_RETRY_INVALID",
        )
    if frozen.dramatic_source != assembled.dramatic_source:
        raise ChapterCardGeneratorError(
            "continuity bridge 定向重试期间 dramatic source 发生漂移",
            raw_output="",
            model_used="deterministic_assembly",
            error_code="CHAPTER_CARD_CONTINUITY_RETRY_INVALID",
        )

    frozen_continuity = frozen.continuity_context.model_copy(
        update={
            "continuity_bridge": assembled.continuity_context.continuity_bridge,
        }
    )
    return frozen.model_copy(update={"continuity_context": frozen_continuity})
