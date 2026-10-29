"""章节卡与衔接桥模型(对应 PROJECT_SPEC §3.2)。"""

import re
from datetime import datetime
from typing import Any, Literal

from pydantic import BaseModel, Field, StrictInt, ValidationInfo, model_validator

from src.core.models import ID_PATTERN
from src.core.models.continuity_ledger import ContinuityLedgerProvenance
from src.core.models.core_state import (
    BookClockConstraint,
    CORE_STATE_TRANSITION_LIMIT,
    CoreStateTransition,
)
from src.core.models.drama import DramaticSourceBinding
from src.core.models.narrative import NarrativeContext

# G1(v1.1 wave 4 session 1):从 chapter_id ``ch-NNNN`` 解析全局章序。
# chapter_id 是全局单调(``ch-{global_idx:04d}``,见 pipeline_service);
# ChapterCard.chapter_number 是**卷内** 1-based 序号,跨卷重置,不能作时序锚。
# G1 state_timeline / retriever ``before_chapter_seq`` 统一用本 helper 解析的
# 全局序。与 orchestrator ``_infer_prev_chapter_id`` 同源解析(那里只关心退一档,
# 不暴露 seq;此处单独取 seq,刻意不动 orchestrator 的 G12 治理代码降低 blast radius)。
_CHAPTER_SEQ_RE = re.compile(r"^ch-(\d+)$")


def chapter_seq_from_id(chapter_id: str) -> int | None:
    """从 ``ch-NNNN`` 形态的 chapter_id 解析全局章序整数;不符合模式返回 ``None``。

    :param chapter_id: 章节 ID,典型 ``ch-0005``。
    :return: 全局序整数(``ch-0005`` → 5);非 ``ch-{int}`` 模式返回 ``None``。
    """
    match = _CHAPTER_SEQ_RE.match(chapter_id)
    return int(match.group(1)) if match is not None else None


# ============================================================================
# G19b forbidden 字段风格污染净化(v1.1 audit G19b 治本)
# ============================================================================
#
# wave 1 治理 G19b:forbidden 字段不应含风格 / 排版 / 句长 / 段数 / 内心戏密度 /
# 修辞密度 / 节奏类约束(风格约束归 writer.md + voice_profile 处理)。塞进 forbidden
# 与 writer 节奏纪律产生矛盾叠加 → mode collapse(luo-clean-v1 ch-5 rerun-4 实证现场)。
#
# **wave 2 临时会话(2026-05-28)上提到 ChapterCard model_validator**:
# 原 src/core/agents/chapter_card_generator.py 内的 _FORBIDDEN_STYLE_KEYWORDS +
# _detect_forbidden_style_pollution 仅在 generate_chapter_card 路径作早期检测;
# 提到 ChapterCard 模型层后,**任何 ChapterCard 实例创建路径**(cascade retry /
# manual seed / 测试 fixture 构造)都自动走 model_validator 净化,
# **无法绕过**。chapter_card_generator.py 早期检测保留(让 LLM caller 决定 retry),
# model_validator 作为最后兜底。
#
# 关键字源:Round 2 §1 G19b + 维护人扩充版列表。luo-clean-v1 ch-1/3/5/7/8/10 实证
# forbidden 污染 100% 命中(对照 ch-2/6/9 纯剧情禁元素 100% 通过)。
FORBIDDEN_STYLE_KEYWORDS: tuple[str, ...] = (
    # 句长 / 字数
    "字的长句", "字以下的", "字以上的", "字以内", "字超过", "字以上", "字以下",
    "单句过长", "单句过短", "句子过长", "句子过短",
    # 段落数 / 段落长度
    "三段的", "段以上", "段以下", "段心理独白", "段对白", "段铺陈",
    "纯心理独白", "段叙述", "段落过长", "段落过短", "段落偏长", "段落偏短",
    "段落冗长", "段落冗短", "单段过长", "单段过短",
    # 排版风格
    "短句", "长句", "散文诗", "短段", "长段",
    # 信息密度类
    "大段堆砌", "大段铺陈", "大段罗列", "大段铺排", "大段渲染",
    "枯燥的", "冗长的", "拖沓的", "啰嗦的", "罗列", "堆砌",
    # 风格类型(部分)
    "氛围的过度", "氛围的轻松", "笔触", "笔法",
    # 节奏类
    "节奏拖沓", "节奏紧凑", "节奏过快", "节奏过慢", "节奏偏快", "节奏偏慢",
    # 修辞密度
    "大量比喻", "大量排比", "大量反问", "修辞密集", "修辞堆砌",
)


def detect_forbidden_style_pollution(forbidden: list[str]) -> list[str]:
    """G19b 风格污染检测:返回 forbidden 列表中含风格类约束的条目。

    返污染条目列表(空 = 无污染);每条匹配条目命中首个关键字即标。
    """
    polluted: list[str] = []
    for entry in forbidden:
        for keyword in FORBIDDEN_STYLE_KEYWORDS:
            if keyword in entry:
                polluted.append(entry)
                break
    return polluted


# v2 W3.5 补强3(fresh-context 实证,2026-06-25):卷末 / 临界点章 forbidden 弱措辞
# "提前[进入类]"只禁"过早做"、不禁"章末跨过临界点做" → 对收尾于阈值的章护栏失效
# (弱措辞下 deepseek/qwen writer 越界率都 ~62%,强措辞"本章绝不进入,无论何时"都 ~0;
# raw-draft RCT + s7-rc 生产复验坐实)。本规则确定性强化该弱措辞 = **纯措辞补强,
# 不新增被禁动作**(进入本就在 forbidden 里、只是禁得软),故不改变 must_vs_forbidden
# 一致性结论(详 docs/impl/v2_w3_5_writer_boundary_supplement3.md §0.6)。
_BOUNDARY_ENTRY_VERBS = "进入|踏入|踏进|穿过|跨入|跨进"
# 仅匹配"提前"紧跟边界进入动词(lookahead);"提前开启/揭示/登场"等不动(精度优先)。
_WEAK_PREMATURE_ENTRY_RE = re.compile(
    rf"提前(?:{_BOUNDARY_ENTRY_VERBS})([^。,，、;；或\n]{{0,8}})"
)
# 去稀释:删"提前"(仅当其后紧跟进入类动词,lookahead);"提前开启/揭示"中的"提前"不动。
# bfix 实证:仅追加强条但保留弱措辞"提前进入",deepseek 仍 ~3/8(弱措辞诱导);去掉
# "提前"后"进入"本身即无条件禁止 → 等价 tightforbid 干净强边界(0/8)。
_PREMATURE_BEFORE_ENTRY_RE = re.compile(rf"提前(?=(?:{_BOUNDARY_ENTRY_VERBS}))")
_BOUNDARY_ENTRY_ANY_RE = re.compile(_BOUNDARY_ENTRY_VERBS)


def strengthen_boundary_forbidden(
    forbidden: list[str], must_achieve: list[str]
) -> list[str]:
    """确定性强化 forbidden 中"提前[进入类]"弱措辞为时间无条件强边界(W3.5 补强3)。

    检测任一 forbidden 条目含"提前(进入/踏入/踏进/穿过/跨入/跨进)"弱措辞 → 两步:
    ① **去稀释**:删该弱措辞里的"提前"("提前进入"→"进入",无条件禁止;"提前开启"
    等非进入类不动)—— bfix 实证:仅追加不去稀释,deepseek 仍被残留"提前进入"诱导
    (~3/8);② **追加**一条自包含强边界项(不在原句内改写,避免 compound 混淆)。

    :param forbidden: 原 forbidden 列表。
    :param must_achieve: 本章必达事件 —— **guard**:若 must_achieve 本身要求某进入类
        动作(含 ``_BOUNDARY_ENTRY_VERBS`` 任一),说明"进入"是本章必达、"提前"是时序
        约束而非边界,**整体跳过不强化**(防把"本章应进入"误锁成"绝不进入"、与
        must_achieve 矛盾)。
    :return: 强化后的 forbidden(未触发时原样返回新 list 副本)。
    """
    must_text = "".join(must_achieve)
    if _BOUNDARY_ENTRY_ANY_RE.search(must_text):
        return list(forbidden)  # 本章必达含进入 → 不强化(防 must/forbidden 矛盾)

    objs: list[str] = []
    matched = False
    de_diluted: list[str] = []
    for entry in forbidden:
        hits = list(_WEAK_PREMATURE_ENTRY_RE.finditer(entry))
        if hits:
            matched = True
            for m in hits:
                obj = m.group(1).strip(" 　")
                if obj:
                    objs.append(obj)
        # 去稀释:仅删"提前+进入类"中的"提前";"提前开启"等保留。未命中条目原样。
        de_diluted.append(_PREMATURE_BEFORE_ENTRY_RE.sub("", entry))
    if not matched:
        return list(forbidden)

    uniq_objs = list(dict.fromkeys(objs))
    obj_phrase = "、".join(uniq_objs) if uniq_objs else "上述被列为禁区的场景或通道"
    strong = (
        f"本章绝不踏入、穿过或进入{obj_phrase}"
        "——无论是否到达其入口或临界点,真正"
        "“进入”的动作一律留到后续章节,本章须止于临界点之前"
    )
    if strong not in de_diluted:
        de_diluted.append(strong)
    return de_diluted


# v2 W3.5 补强3(健壮主力,2026-06-25):**确定性传播 beat forbidden_advance 的"进入空间"项**。
# 发现:大纲 beat `forbidden_advance` 已编码具体边界(典型 "林晓完全进入裂隙或异空间"),但只
# 用来约束 must_achieve、未确定性写进卡 forbidden 字段 → 靠出卡 LLM 概率写(实测 ~70%,弧光重力
# 章漏更多)。本规则把 forbidden_advance 里"进入类动词+空间名词"项,确定性注入该 beat 每张卡 forbidden
# 为强边界 —— **对象具体(取自 forbidden_advance,根治 regex 抽不准)+ 全 beat 覆盖(根治 A 漏弧光
# 重力章)+ 确定性**。deepseek 语义优先须具体对象才刹住(bfix2 实证 vague object ~5/8),本机制供给。
_FA_SPACE_NOUN = "裂隙|异空间|缝隙|通道|甬道|洞口|密室|结界|裂缝|秘境|墓室深处|核心区"
_FA_ENTRY_VERB = "进入|踏入|踏进|穿过|穿越|跨入|跨进"
_FA_ENTRY_ITEM_RE = re.compile(rf"(?:{_FA_ENTRY_VERB}).*?(?:{_FA_SPACE_NOUN})")


def propagate_forbidden_advance_entry(
    forbidden: list[str],
    must_achieve: list[str],
    beat_forbidden_advance: list[str],
) -> list[str]:
    """把 beat ``forbidden_advance`` 的"进入空间"类项确定性注入卡 ``forbidden`` 为强边界。

    仅处理含"进入类动词 + 空间名词"的 forbidden_advance 项(典型 "林晓完全进入裂隙或异空间");
    其余 forbidden_advance 项(角色出场 / 真相揭示 / 净化等)由既有"严守 forbidden_advance"
    must 约束机制处理,不在此注入。

    :param beat_forbidden_advance: 本章所属 beat 的 forbidden_advance(共享单卡 context)。
    :param must_achieve: **guard** —— 若 must_achieve 本身要求进入该空间(进入是本章必达,
        典型卷二章),则跳过该项(防把本章必达误锁成绝不)。
    :return: 注入后的 forbidden(无可注入项时原样返回新 list 副本)。
    """
    if not beat_forbidden_advance:
        return list(forbidden)
    must_text = "".join(must_achieve)
    must_has_entry = bool(re.search(_FA_ENTRY_VERB, must_text))
    new_forbidden = list(forbidden)
    for item in beat_forbidden_advance:
        if not _FA_ENTRY_ITEM_RE.search(item):
            continue  # 非"进入空间"类 forbidden_advance,跳过
        spaces = list(dict.fromkeys(re.findall(_FA_SPACE_NOUN, item)))
        if not spaces:
            continue
        # guard:must_achieve 要求进入该空间(本章必达含进入动词 + 该空间名词)→ 跳过
        if must_has_entry and any(s in must_text for s in spaces):
            continue
        obj = "、".join(spaces)
        strong = (
            f"本章绝不踏入、穿过或进入{obj}"
            "——无论是否到达其入口或临界点,真正"
            "“进入”的动作一律留到后续章节,本章须止于临界点之前"
        )
        if strong not in new_forbidden:
            new_forbidden.append(strong)
    return new_forbidden


CardStatus = Literal["draft", "pending_review", "approved", "rejected"]
"""章节卡审核状态(P5-B-02 引入,对齐 plan-kickoff 决策 6=C)。

4 档语义:

- ``draft``:新建未审核(P5-B-06 lazy 生成或上传初态)。
- ``pending_review``:进入审核流；outline commit 只送首卡，正式 Run 后续逐章送审。
- ``approved``:通过审核,允许 ``chapter:write``(P5-B-06 gate 检查项)。
- ``rejected``:审核退回(P5-B-07 ``card:reject``),需编辑后重审。

与 ``Chapter.status`` 7 档**完全解耦**(决策 6=C):章节卡生命周期独立于
章节正文生命周期,UI 上 ``ChapterStateBadge`` 与 ``CardStatusBadge`` 双显
(对齐 v2_wishlist + 自决项 14 不合并)。
"""


ForceProfile = Literal[
    "cloud_quality", "cloud_premium", "local_qwen_32k", "local_qwen_safe"
]
"""``ChapterCard.force_profile`` 可选枚举(P6-B-09 引入,对齐 FRONTEND_SPEC §4.8
字面"强制使用某 profile")。

4 档语义(对齐 ``config/writer_profiles.yaml`` 默认 4 档 profile_id):

- ``cloud_quality``:Claude Sonnet 4(默认主力)。
- ``cloud_premium``:Claude Opus 4(finale / 高张力章升级)。
- ``local_qwen_32k``:本地 Qwen 32k 主力(P3+)。
- ``local_qwen_safe``:本地 Qwen 安全 fallback。

设计要点:

- ``None`` 表示"沿用默认路由"(ModelRouter 按 ``agent_routing`` / chapter_card
  规则匹配),与 P3-B-11 单次 ``profile_override`` 透传机制语义相同——但本字段
  是**章节卡级持久化**(对齐 plan §字面"force_profile 字段持久化"),与
  ``profile_override`` 单次覆盖语义互补。
- 当两者同时给定时**force_profile 优先**(自决项):章节卡显式声明的 profile
  代表"维护人对本章的决定",优先级高于"本次 write 请求的临时覆盖";Reviewer
  / Reviser 等同章 agent 一律走 force_profile,语义自洽。
- 越界(profile_id 在 Literal 内但不在 ``ModelRouter.writer_profiles`` 声明)
  返 422 ``WRITER_PROFILE_NOT_FOUND``(P6-B-09 新增,与 P3-B-11
  ``CHAPTER_INVALID_PROFILE`` 区分:后者是单次 request body 越界,本错误码
  是章节卡数据越界)。
"""


class ContinuityContext(BaseModel):
    """章节衔接桥(A1)。

    描述本章相对于上一章的衔接信息,由 ``continuity.py`` 抽取或人工填入。
    """

    prev_chapter_tail: str = Field(..., description="上一章末尾 500-800 字")
    unfinished_actions: list[str] = Field(
        default_factory=list, description="上一章未完成动作的描述列表"
    )
    immediate_action_indices: list[StrictInt] | None = Field(
        default=None, exclude_if=lambda value: value is None,
        description=("程序从已验真的前章账本冻结的即时动作索引；null 为历史全列表语义，"
                     "空数组表示均为长期未决。非即时事项仍保留为背景，不要求逐章重演。"),
        json_schema_extra={"readOnly": True},
    )

    @model_validator(mode="after")
    def _validate_action_scope(self) -> "ContinuityContext":
        indices = self.immediate_action_indices
        if indices is not None and (
            indices != sorted(set(indices))
            or any(i < 0 or i >= len(self.unfinished_actions) for i in indices)
        ):
            raise ValueError("即时动作索引须唯一、有序且位于完整前章事项列表内")
        return self

    @property
    def required_actions(self) -> list[str]:
        """Writer、计划、Judge 和修订共用的承接义务；旧卡语义不变。"""
        indices = self.immediate_action_indices
        return list(self.unfinished_actions) if indices is None else [
            self.unfinished_actions[i] for i in indices
        ]

    @property
    def background_unresolved(self) -> list[str]:
        indices = self.immediate_action_indices
        return [] if indices is None else [
            action for i, action in enumerate(self.unfinished_actions) if i not in indices
        ]

    time_gap: str = Field(..., description='时间间隔,如"立即续接"/"数分钟后"/"三日后"')
    scene_continuation: Literal["same", "switch", "time_jump"] = Field(
        ..., description="场景延续模式"
    )
    continuity_bridge: str = Field(
        "",
        description=(
            "R4 跨章承接说明。上一章存在即时未完动作且本章跳时/换景时，必须"
            "逐字包含对应 unfinished_action 并说明先如何承接、再如何转场。"
        ),
    )
    pov_change: bool = Field(..., description="本章相对上一章是否切换视角")
    pov_from: str | None = Field(..., description="上一章视角角色,首章可为 None")
    pov_to: str = Field(..., description="本章视角角色")


class ChapterCard(BaseModel):
    """章节卡。

    描述单章的节奏控制、内容契约、衔接信息与审稿配置。
    P1 阶段由人工填入,P2+ 由 ``ChapterCardGenerator`` 自动生成。
    """

    chapter_id: str = Field(
        ...,
        pattern=ID_PATTERN,
        description="章节唯一 ID,跨工作区不冲突(字符集见 ID_PATTERN)",
    )
    workspace_id: str = Field(..., description="所属工作区 ID")
    volume_id: str = Field(..., description="所属卷 ID")
    chapter_number: int = Field(..., ge=1, description="卷内章序号(从 1 起)")
    title: str = Field(..., description="章节标题")

    # 节奏控制
    tension_level: int = Field(..., ge=1, le=10, description="本章张力等级(1-10)")
    word_count_min: int = Field(2500, ge=1, description="正文目标篇幅下限（写作要求）")
    word_count_max: int = Field(4000, ge=1, description="正文目标篇幅上限（写作要求）")
    word_count_policy_version: Literal[0, 1, 2] = Field(
        0, exclude_if=lambda value: value == 0,
        description="篇幅规则版本：0 只兼容历史 HARD；1 为范围要求；2 为约数目标（min=max）；两者均不因字数拒稿、扣分或自动修改。",
    )
    plot_node_budget: int = Field(3, ge=1, description="本章剧情节点预算")

    # 内容契约
    must_achieve: list[str] = Field(..., description="必达事件列表")
    must_unresolved: list[str] = Field(..., description="必须保持未解决的悬念列表")
    forbidden: list[str] = Field(..., description="禁止出现的元素列表")
    pov_character: str = Field(..., description="本章视角角色")
    setting: str = Field(..., description="场景设置")
    setting_contract_hash: str = Field(
        "",
        pattern=r"^(?:|[0-9a-f]{64})$",
        description=(
            "生成本卡时权威 data/outline/world.json 的语义 SHA-256；空串只兼容"
            "无权威 outline 的 legacy/import 卡。Writer 启动前必须与当前合同比对。"
        ),
    )
    dramatic_source: DramaticSourceBinding | None = Field(
        default=None,
        description=(
            "高价值章节从 volume/beat/mini-plan 确定性冻结的 typed 戏剧源；"
            "普通独处、治愈、留白与 legacy 卡为 null。"
        ),
    )
    continuity_ledger_provenance: ContinuityLedgerProvenance | None = Field(
        None,
        description=(
            "生成本卡时冻结的上一章同步连续性小账本来源版本、正文 hash 与"
            "ledger hash；首章为 null。approve 与 Writer 前必须复核。"
        ),
    )
    core_state_contract_version: Literal[0, 1] = Field(
        0,
        description=(
            "核心状态/书内时钟合同版本。0=legacy 或工作区 kill-switch 关闭，"
            "不自动回填；1=卡已冻结同一份核心投影，Writer/Judge/ready 必须验真。"
        ),
    )
    core_state_projection_hash: str = Field(
        "",
        pattern=r"^(?:|[0-9a-f]{64})$",
        description=(
            "生成本卡时当前有效核心状态与上一章末书内时钟的语义 SHA-256。"
            "合同版本 1 时必为 64 位，并在 approve 与 Writer Provider 前复核。"
        ),
    )
    core_state_transitions: list[CoreStateTransition] = Field(
        default_factory=list,
        max_length=CORE_STATE_TRANSITION_LIMIT,
        description=(
            "本章获准发生的承重状态 from→to 变化。它是未来意图，不是当前事实；"
            "仅批准正文存在逐字证据时才提交；每章最多 2 项，避免挤占 Writer 注意力。"
        ),
    )
    book_clock_constraint: BookClockConstraint | None = Field(
        None,
        description=(
            "versioned 书内时间冻结合同。v0 是 legacy numeric interval；v1 区分"
            "exact/bounded_interval/ordered_relative/genuinely_unanchored，并保存"
            "closed relation、phase、anchor 与 chain hash。Writer/Judge 只消费原始"
            "自然语言及关系含义；近似量不派生分钟 bounds。"
        ),
    )
    narrative_context: NarrativeContext | None = Field(
        None, exclude_if=lambda value: value is None,
        description="叙事交接版本与人物参与方式；旧卡按原行为读取。",
    )
    craft_focus: str = Field(
        default="",
        description=(
            "本章 per-scene 卓越性焦点(M2 纠正侧,v1.1 wave 5 s3):一句到几句话点明"
            "**本章哪几处场景 / 节拍是情感节点该深挖内心(interiority)、哪几处是关键戏"
            "该给具体画面(showing)、其余过场 / 推进处保持克制留白**,让 Writer 规划期"
            "就知道把笔墨投在哪。**护留白**:不是每章都要 —— 动作章 / 过场章可整体克制"
            "(写'本章以动作推进为主,内心戏克制'或留空),只标真正吃重的少数场景。卡生成"
            "期 LLM 据 ``must_achieve`` 节拍产出;``default=''`` 向后兼容(旧卡 / 未产出 → "
            "Writer 跳过本段,行为与 s3 前逐字节等价)。与 reviewer 的 score_imagery / "
            "score_interiority(M2 度量侧)配对:本字段告诉 writer 哪里投入,度量侧测是否交付。"
        ),
    )
    chapter_focus: str = Field(
        default="",
        description=(
            "本章**结构焦点**(v1.1 wave 5 session 6 / M4''':治'章节结构不跟戏剧功能走')。"
            "从 ``MiniPlanItem.chapter_focus`` **确定性透传**(非 card LLM 产出,避免变异 / "
            "重述上游 —— 轴B 教训:结构形态早在 outline 层决定,card 只能透传不能重决)。"
            "一句话点明本章注意力 / 分量投向哪 + 结构形态;**只在偏离题材默认结构时非空**"
            "(情感峰单焦 / 角色单焦 / backstory / 纯单线困局),默认结构(双界互证 / 单线"
            "推进)留空。``default=''`` 向后兼容(旧卡 / 未偏离 → Writer 跳过本段,逐字节"
            "零回归)。Writer ``_format_chapter_focus`` 非空才注入'本章别按默认结构稀释'指令。"
            "与 craft_focus 正交:craft_focus 管'哪处深挖内心 / 给画面'(笔墨投放),"
            "chapter_focus 管'本章整体结构形态'(双线 vs 单焦)。"
        ),
    )
    arc_beats: dict[str, str] = Field(
        default_factory=dict,
        description=(
            "本章**逐章角色弧光台阶**(v1.1 wave 5 session 7 / M5:治'人物弧光近乎为零 / "
            "配角零成长 / 弧光全靠旁白声明')。从 ``MiniPlanItem.arc_beats`` **确定性透传**"
            "(非 card LLM 产出,避免变异 —— 同 chapter_focus 范式)。key=``character_id``,"
            "value=自包含一句话(该角色本章从什么态→什么态 + 通过什么动作 / 抉择演出)。"
            "**只在某角色弧光真推进可见台阶时非空**,绝大多数章留空 ``{}``(内容驱动非配额)。"
            "``default={}`` 向后兼容(旧卡 / 本章无弧光推进 → Writer 跳过本段,逐字节零回归)。"
            "Writer ``_format_arc_beats`` 非空才注入'本章用动作演出弧光台阶、别旁白声明'指令。"
            "与 chapter_focus 正交:chapter_focus 管'整体结构形态'(双线 vs 单焦),arc_beats "
            "管'本章谁演哪步弧光'(内容)。与 identity_facets(A3 谁是谁)/ G1 累积状态"
            "(别倒退)互补:arc_beats 是本章该迈的那一步(向前)。"
        ),
    )
    interaction_focus: str = Field(
        default="",
        description=(
            "本章**互动场景中心**(v1.1 wave 5 session 8 / M4''-轴A:治松 beat 题材〔治愈"
            "日常 / 慢生活类〕'独处内省扎堆' —— 戏剧引擎默认让主角独自做事 / 感慨,配角在场"
            "也被写成背景板)。从 ``MiniPlanItem.interaction_focus`` **确定性透传**(非 card "
            "LLM 产出,避免变异 —— 同 chapter_focus 范式)。一句话点明本章戏剧核心是主角与谁"
            "的一场真实互动 + 那个配角要被写成有主体性的真人(声音 / 动作 / 目的 / 温和摩擦),"
            "情感从互动里长出来非旁白。**只在本章戏剧核心是人物互动(尤其安静互动)时非空**,"
            "默认留空(独处留白章 / 高戏剧自带场景章)。``default=''`` 向后兼容(旧卡 / 非互动"
            "中心 → Writer 跳过本段,逐字节零回归)。Writer ``_format_interaction_focus`` 非空"
            "才注入'把配角写成真人、情感从互动演出'指令。与 chapter_focus(整体结构形态)/ "
            "arc_beats(谁演哪步弧光)/ craft_focus(哪几处深挖内心 / 画面)正交互补。"
        ),
    )
    breakthrough_payoff: str = Field(
        default="",
        description=(
            "本章**升级可感化要点**(v1.1 wave 5 session 10 / 模块①:治升级流'突破爽点失约' "
            "—— 当前引擎把境界突破写成痛苦氛围 + 一个境界名词,无可感实力跃迁)。从 "
            "``MiniPlanItem.breakthrough_payoff`` **确定性透传**(非 card LLM 产出,避免变异 "
            "—— 同 chapter_focus 范式)。一句话点明本章突破到什么 + 要落地的可感化点(新能力 / "
            "碾压旧强敌 / 感知跃升)。**只在境界突破/晋级章非空**,默认留空(非升级章 / 非 3 "
            "题材 / 开关 OFF)。``default=''`` 向后兼容(旧卡 → Writer 跳过本段,逐字节零回归)。"
            "Writer ``_format_breakthrough_payoff`` 非空才注入'升级可感化:对比起点给跃迁落点 + "
            "新能力展示 + 破痛苦模板'指令(起点境界由模块②注入)。与模块②(境界一致性,防倒退)"
            "互补:②管地板别倒退 + 起点,①管本章这步跃迁写得爽。"
        ),
    )

    # 衔接(升级)
    continuity_context: ContinuityContext = Field(..., description="衔接桥上下文")
    next_chapter_seed: str = Field(..., description="给下一章的种子线索")

    # 元数据
    arc_phase: Literal["opening", "rising", "climax", "falling", "resolution"] = Field(
        ..., description="本章在弧线中的阶段"
    )
    is_arc_finale: bool = Field(..., description="本章是否为弧线终章")
    related_plot_threads: list[str] = Field(..., description="关联的剧情线 ID 列表")
    callback_to: list[str] = Field(
        default_factory=list,
        description=(
            "本章显式呼应的伏笔 / 隐喻 thread_id 列表(v1.1 wave 4 session 2 / "
            "G2 引入,作者意图标记)。非空时 Writer **绕过 embedding 直接按 ID "
            "SQL lookup PlotThread** + 读 planted_chapter 原文片段注入 prompt,"
            "根治'第 3 章暴雨被第 45 章洗澡向量检索覆盖'。生成时由 "
            "chapter_card_service 从 mini_plan ``callback_anchors`` 确定性派生,"
            "用户后续可手动编辑。"
        ),
    )
    related_characters: list[str] = Field(..., description="出场角色 ID 列表")

    # 审稿配置
    review_threshold: float = Field(7.5, description="通过阈值(score_overall)")
    revise_threshold: float = Field(6.5, description="走 revise 而非 rewrite 的阈值")
    max_rewrites: int = Field(3, ge=0, description="重写次数上限")
    max_revises: int = Field(2, ge=0, description="修订次数上限")

    # 审核状态(P5-B-02 新增,决策 6=C)
    card_status: CardStatus = Field(
        "draft",
        description="章节卡审核状态(draft / pending_review / approved / rejected),与 chapter.status 解耦",
    )
    card_approved_at: datetime | None = Field(
        None,
        description="审核通过时间(UTC);未通过时为 None",
    )

    # v1.0.x wave 6 s2:多卡恢复同质化自动 retry 结果元数据(Q2=A 显式字段)
    auto_retry_status: Literal["passed", "exhausted"] | None = Field(
        None,
        description=(
            "多卡级联恢复时同质化检测自动 retry 的结果状态(wave 6 s2 引入):"
            "``passed`` = 触发 retry 后 < 3 次内成功避免雷同;"
            "``exhausted`` = 3 次 retry 都仍 ≥ 阈值,保留首次卡 + UI 红色标记"
            "+ 用户可手动 retry_references;``None`` = 未触发检测(单生成 / 单 retry "
            "路径 / 同轮天然差异化)。与 ``card_status`` 完全独立 — exhausted "
            "不等于 rejected,用户可正常审 approve / reject;但 UI 应区分展示。"
        ),
    )

    # 强制 profile 覆盖(P6-B-09,Alembic 0008 已落 DB 列预留)
    force_profile: ForceProfile | None = Field(
        None,
        description=(
            "强制本章所有 agent 走指定 Writer profile(对齐 FRONTEND_SPEC §4.8 "
            "字面);None 表示走默认路由。与单次 profile_override 同时给定时"
            "force_profile 优先(章节卡级持久化优先于单次请求覆盖)。"
            "越界时 chapter:write/:rewrite 触发 422 WRITER_PROFILE_NOT_FOUND。"
        ),
    )

    @model_validator(mode="after")
    def _validate_approximate_length(self) -> "ChapterCard":
        if self.word_count_policy_version == 2 and self.word_count_min != self.word_count_max:
            raise ValueError("约数篇幅模式需要 min/max 同存一个目标值，不表示精确字数或下限")
        return self

    @model_validator(mode="before")
    @classmethod
    def _reject_retired_draft_strategy(
        cls, data: Any, info: ValidationInfo
    ) -> Any:
        """拒绝新写入的旧多稿键，并为磁盘历史提供窄只读折叠。

        正常 ``ChapterCard`` 校验代表 API、Prompt 或代码新建卡片：即使值为
        ``single``，携带已退休的 ``draft_strategy`` 也会 fail closed。只有显式
        ``model_validate*_storage`` 读取历史 JSON 时，三个已知旧值才会在校验后
        被丢弃；未知值仍拒绝。这样旧 workspace 可读，但不可能把旧多稿策略带回
        运行时或新序列化结果。
        """
        if not isinstance(data, dict) or "draft_strategy" not in data:
            return data

        context = info.context if isinstance(info.context, dict) else {}
        if not context.get("legacy_chapter_card_storage"):
            raise ValueError(
                "ChapterCard.draft_strategy 已退休；新生产只支持单稿，"
                "不得提交 single/parallel_2/parallel_3 旧字段"
            )

        legacy_value = data.get("draft_strategy")
        if legacy_value not in {"single", "parallel_2", "parallel_3"}:
            raise ValueError(
                "历史 ChapterCard.draft_strategy 只能是 "
                "single/parallel_2/parallel_3"
            )
        normalized = dict(data)
        normalized.pop("draft_strategy", None)
        return normalized

    @model_validator(mode="after")
    def _validate_continuity_scope_source(self) -> "ChapterCard":
        indices = self.continuity_context.immediate_action_indices
        if indices is not None:
            if self.narrative_context is None:
                raise ValueError("即时动作子集只适用于显式叙事合同的新卡")
            if self.chapter_id == "ch-0001":
                if indices or self.continuity_context.unfinished_actions:
                    raise ValueError("首章不能携带前章动作子集")
            elif self.continuity_ledger_provenance is None:
                raise ValueError("即时动作子集缺少前章账本来源")
        return self

    @model_validator(mode="after")
    def _validate_narrative_participation(self) -> "ChapterCard":
        if self.narrative_context is None:
            return self
        roles = {item.character_id: item.mode for item in self.narrative_context.participation}
        if set(roles) != set(self.related_characters):
            raise ValueError("新卡人物参与方式必须覆盖且只覆盖所有相关人物")
        if roles.get(self.pov_character) != "present":
            raise ValueError("本章主视角必须是当前参与人物")
        return self

    @model_validator(mode="after")
    def _validate_core_state_contract_shape(self) -> "ChapterCard":
        if self.core_state_contract_version == 0:
            if (
                self.core_state_projection_hash
                or self.core_state_transitions
                or self.book_clock_constraint is not None
            ):
                raise ValueError(
                    "core_state_contract_version=0 时不得携带核心投影、迁移或时钟合同"
                )
            return self
        if len(self.core_state_projection_hash) != 64:
            raise ValueError(
                "core_state_contract_version=1 时必须携带 64 位 projection hash"
            )
        if self.book_clock_constraint is None:
            raise ValueError(
                "core_state_contract_version=1 时必须携带 book_clock_constraint"
            )
        keys = [item.state_key for item in self.core_state_transitions]
        if len(keys) != len(set(keys)):
            raise ValueError("同一章节卡不得对同一 core state_key 声明多次迁移")
        return self

    @classmethod
    def model_validate_storage(cls, data: Any) -> "ChapterCard":
        """读取已持久化章节卡；只在这里折叠已知旧多稿字段。"""
        return cls.model_validate(
            data, context={"legacy_chapter_card_storage": True}
        )

    @classmethod
    def model_validate_json_storage(
        cls, data: str | bytes | bytearray
    ) -> "ChapterCard":
        """读取已持久化章节卡 JSON；语义同 :meth:`model_validate_storage`。"""
        return cls.model_validate_json(
            data, context={"legacy_chapter_card_storage": True}
        )

    @model_validator(mode="after")
    def _validate_no_forbidden_style_pollution(self) -> "ChapterCard":
        """G19b 兜底 invariant(v1.1 wave 2 临时会话 2026-05-28 上提):任何创建
        ChapterCard 实例的路径都必须经过 forbidden 风格污染净化。

        **为何在 model_validator 层**:src/core/agents/chapter_card_generator.py
        的早期检测路径只覆盖 generate_chapter_card 调用,cascade retry /
        manual seed / 测试 fixture 等任一绕过 generator 的 caller 都需要
        本兜底。Pydantic model_validator 在每次实例创建时触发,**无法绕过**。

        **错误信息要明确给出 fix 路径**:让 caller 知道是哪些条目污染 + 如何改。
        """
        polluted = detect_forbidden_style_pollution(self.forbidden)
        if polluted:
            raise ValueError(
                f"ChapterCard.forbidden 含风格 / 排版 / 节奏类污染条目 (G19b 兜底): "
                f"{polluted}. forbidden 只放剧情禁元素 (现代用语 / 跨域穿帮 / 越界 "
                f"角色 / 越界设定 / voice forbidden 等), 风格约束 (段落长度 / 句长 / "
                f"比喻密度等) 归 writer.md + voice_profile 处理 — 塞进 forbidden 会与 "
                f"writer 节奏纪律矛盾叠加触发 mode collapse (luo-clean-v1 ch-5 现场)."
            )
        return self
