"""生成参数工作区级配置(v1.1 wave 3 session 3 / L1 Q16)。

引擎默认 ``config.yaml`` 的 ``generation:`` 段;工作区 ``project.yaml`` 可整段覆盖
(``ConfigService.load_generation_config``)。本模块只定义 Pydantic 模型 + 由
config_service 加载,语义与 ``reviewer:`` / ``retrieval:`` / ``cost_guard:`` 三段同源
(都是工作区级"可随时调的生成阈值",存 project.yaml 而非 DB)。

字段语义
========

- ``word_count_min`` / ``word_count_max`` : 本工作区**新生成**章节卡的目标字数区间
  默认。``ChapterCardService.derive_context`` 读取后作为新卡 ``word_count_min/max``
  的兜底(LLM 未给字数时用本工作区默认,而非 ``ChapterCard`` 模型字段默认 2500/4000)。
  已生成的章节卡通过 ``POST /workspaces/{id}/chapter-cards:reset-word-count``
  (G6.2 backfill)一键应用。
- ``max_rewrites`` : 本工作区新生成章节卡的"整章重写次数上限"默认(一章审稿未过时
  系统整章推倒重写最多几次)。``derive_context`` 作新卡 ``max_rewrites`` 兜底。
字段默认值对齐当前 ChapterCard 实现
==================================

- ``word_count_min`` 2500 ← ChapterCard ``word_count_min`` 默认 2500
- ``word_count_max`` 4000 ← ChapterCard ``word_count_max`` 默认 4000
- ``max_rewrites``   3    ← ChapterCard ``max_rewrites`` 默认 3

为何存 project.yaml 而非 WorkspaceORM DB(plan §4 字面 = DB,grep 验真后改 project.yaml)
======================================================================================
生成参数是"工作区级可调阈值",与 reviewer/retrieval/cost_guard 同类;三者均走
config.yaml 引擎默认 + project.yaml 覆盖 + config_service loader 范式(实证 3 处)。
default_voice/outline_status 那种 DB 列适合"创建期身份字段",不是可调旋钮。
plan-kickoff §4 字面"WorkspaceSettings ORM + alembic"是未 grep 验真的直觉命名
(类 D 变种),实际复用 project.yaml 范式工程量更小、不动 DB。详 impl 文档 §关键决策。

交叉校验
========

- ``word_count_min <= word_count_max``  (区间合法)
"""

from __future__ import annotations

from typing import Literal

from pydantic import BaseModel, ConfigDict, Field, model_validator


class GenerationConfig(BaseModel):
    """``generation:`` 配置段(v1.1 L1 工作区级生成参数默认)。

    Pydantic v2 model;字段默认值对齐当前 ChapterCard 单章字段。

    与 ChapterCard 字段并列但层级不同
    --------------------------------
    本模型是**工作区级默认**(项目级,影响后续新生成卡);ChapterCard
    ``word_count_min`` / ``word_count_max`` / ``max_rewrites`` 是**单章级值**。
    新卡生成时 ``derive_context`` 用本工作区默认作兜底;已生成卡通过 G6.2
    backfill 端点应用。
    """

    model_config = ConfigDict(extra="forbid")

    word_count_min: int = Field(
        default=2500,
        ge=1,
        description="新生成章节卡字数下限默认;对齐 ChapterCard.word_count_min 默认 2500。",
    )
    word_count_max: int = Field(
        default=4000,
        ge=1,
        description="新生成章节卡字数上限默认;对齐 ChapterCard.word_count_max 默认 4000。",
    )
    word_count_policy_version: Literal[0, 1, 2] = Field(
        default=0,
        description="新卡篇幅规则版本；2 为约数目标（min=max），1 为范围要求，均不作写后硬拦截；0 保留历史合同。",
    )
    max_rewrites: int = Field(
        default=3,
        ge=0,
        description=(
            "新生成章节卡整章重写次数上限默认(审稿未过时整章重写最多几次);"
            "对齐 ChapterCard.max_rewrites 默认 3。"
        ),
    )
    plot_node_budget: int = Field(
        default=3,
        ge=1,
        description=(
            "新生成章节卡剧情节点预算默认(v1.1 wave 4 session 3 / I2.1 加);"
            "对齐 ChapterCard.plot_node_budget 默认 3。风格模板("
            "``STYLE_GENERATION_DEFAULTS``)作引擎默认基线注入,project.yaml "
            "L1 用户覆盖仍优先(经 ``ConfigService.load_generation_config`` 合并)。"
        ),
    )
    core_state_hard_enabled: bool = Field(
        default=True,
        description=(
            "全书核心状态与书内时钟 HARD 主开关。关闭时新卡写 contract_version=0，"
            "不回填历史卡、不提交核心账，Writer/Judge 退回既有行为。"
        ),
    )
    progression_consistency: Literal["auto", "on", "off"] = Field(
        default="auto",
        description=(
            "v1.1 wave 5 s10 加。**模块② 境界一致性**(题材中性 progression_rank:"
            "每章追踪角色进阶等级入库 + 注入 writer 防写串/莫名倒退,WARN 安全网校验)。"
            "**工作区级 UI 开关**:\n"
            "- ``auto``(默认):按题材定——古典仙侠 ON(境界必守)、搞笑爽文/热血战斗 OFF"
            "(防强加在无等级体系的爽文上误报)、其余题材 OFF。\n"
            "- ``on`` / ``off``:用户显式覆盖(仅 3 题材〔古典仙侠/搞笑爽文/热血战斗〕生效;"
            "其余题材即便填 on 也按 off,防外溢)。\n"
            "**OFF = 子管线 no-op = 逐字节零回归**。由 ``ChapterCardService."
            "resolve_effective_progression_consistency`` 解析 auto→题材默认。"
        ),
    )
    breakthrough_payoff: Literal["auto", "on", "off"] = Field(
        default="auto",
        description=(
            "v1.1 wave 5 s10 加。**模块① 升级可感化**(mini_plan 标升级章 + writer 注入"
            "'突破要可感化:起点→终点实力差 + 新能力展示 + 破同质化痛苦模板')。"
            "**工作区级 UI 开关**:\n"
            "- ``auto``(默认):按题材定——古典仙侠/搞笑爽文/热血战斗 ON(升级流核心)、"
            "其余题材 OFF。\n"
            "- ``on`` / ``off``:用户显式覆盖(仅 3 题材生效)。\n"
            "**OFF = breakthrough_payoff 字段恒空 = 逐字节零回归**。由 ``ChapterCardService."
            "resolve_effective_breakthrough_payoff`` 解析 auto→题材默认。"
        ),
    )

    style_anchor_source: Literal["prev_chapter", "book_anchor"] = Field(
        default="prev_chapter",
        description=(
            "v2 w2-s2 加。writer prompt 风格基线段 + 跨章闸门(cross_chapter_voice "
            "段均 / style_drift G17)统一用的段均/句均**数字锚源**:\n"
            "- ``prev_chapter``(**默认**,v2 间隙 2026-06-13 起回退为默认):锚前一"
            "章 final 指纹。\n"
            "- ``book_anchor``:锚全书开篇基线(ch1-3 final 指纹中位数,与检测端 "
            "style_trend_validator 同锚同源)。**只换段均/句均两个数字**,主导人称"
            "判定 / 比喻密度仍来自前一章(POV 一致性语义不变)。w2-s1 标定理论:"
            "前章锚是慢漂的结构性放大器(温水煮青蛙)→ 全书锚把拉力锚点固定在开篇。\n"
            "锚窗口未就位(全书前 3 章)时 book_anchor 自动退化为 prev_chapter 行为。\n\n"
            "**为何 v2 间隙回退默认为 prev_chapter(2026-06-13)**:w2-s2 拍板"
            "默认打开 book_anchor 时基于'A/B 负结果 = 没用但无害'的认知;v2 间隙"
            "会话用 luo-accept-v2 30 章实证发现:① 全书锚要治的'单向段落漂移'病"
            "**基本没出现**(章号→段均 r=0.149);② 段长真正由**章节张力**驱动"
            "(张力→段均 r=−0.487,高潮章短段是戏剧规律);③ 全书锚固定数字与张力"
            "规律系统性冲突,**在 rewrite 路径经 G17 把高潮章强制往长 = HARD 致伤**"
            "(确定性证明)。故回退默认为 prev_chapter(干净现状),把'全书级锚'概念"
            "留 wave 4 以**张力感知锚**(按章张力调锚值,而非固定全书锚)正确重新引入。"
            "开关基建保留:工作区仍可显式设 book_anchor。详见 docs/impl v2 间隙会话。"
        ),
    )

    anti_tic_trim_enabled: bool = Field(
        default=False,
        description=(
            "v2 W3.5 wave-3 加。**anti-tic trim 返工 pass 工作区级开关**(默认关、"
            "逐字节零回归)。``True`` 时 writer 出稿后(``chapter_orchestrator._call_writer``)"
            "加一道独立 LLM pass(``prompts/anti_tic_trim.md``,路由复用 writer profile)"
            "只清两类 deepseek 固有 tic(①「不是 A,是 B」否定对比句式上瘾 ②演后盖章金句),"
            "**绝不动剧情/对白/细节**,清完的稿再走 review/validator 管线。``False``(默认)"
            "= **engine 级 kill-switch**,完全不调用(零成本零延迟零风险,行为与本功能前等价)。\n"
            "**三护栏**(W3.5 wave-3 实验暴露的两个真实下行各对应):① **tic 密度 gate** —— "
            "窄计「不是X是Y」< 阈值则跳过 trim(防在 0-tic 干净章上手痒改坏题眼金句,实验 "
            "ch-0008 下行);② **截断护栏** —— trim 后 ``finish_reason==length`` 抬 max_tokens "
            "重试,仍截断回退原文(防交残章,实验 ch-0007 撞 max_tokens 下行;floor ≥16000);"
            "③ **越界护栏** —— trim 输出长度越界(过短/过长)或 tic 不降反增则回退原文。\n"
            "**收益 caveat(诚实记)**:实验仅 deepseek 2 题材 6+6 章验过,收益随 tic 密度走"
            "(悬疑密集 12:0 大胜、治愈稀疏 8:4 弱),非万能;更多题材/模型未验。"
        ),
    )

    word_count_trim_enabled: bool = Field(
        default=False,
        description=(
            "v2 W3.5-s3rev 加。**超长 trim 返工 pass 工作区级开关**(默认关、逐字节零回归)。"
            "``True`` 时 writer 出稿后(``chapter_orchestrator._call_writer``,anti-tic 之后)"
            "若正文仍 > ``word_count_max``,加一道独立 LLM 精简 pass"
            "(``prompts/word_count_trim.md``,路由复用 writer profile)看着真正文把 must 之外的水"
            "(过场/盖章/重复)整段砍掉、压到 ≤上限,再走 review/validator 管线 → **不进 rewrite 循环**。"
            "``False``(默认)= **engine 级 kill-switch**,完全不调用(零成本零延迟,等价)。\n"
            "**为何 trim 不 rewrite**:实验(docs/acceptance/v2_w3_5_s3rev_length_ab_findings.md)钉死"
            "盲 rewrite 整章重生成会丢好剧情 + 加回过场 + 偶尔越压越长(4619→6471);trim 看真正文砍、"
            "保留好剧情(对调盲读 2:0 胜 rewrite)。**bounded-retry 校验**:trim 后代码量字数,出界"
            "(>cap/跑飞)再砍一刀(最多 2 次),弱软下限 2500 防砍秃 —— 实验 9/9 ≤cap(含极端 7154)。\n"
            "**零 fail-open**:不收敛 / 异常回退原稿,交既有 rewrite 循环兜底,绝不交残章 / 阻断生成。"
            "**caveat**:实验仅 deepseek 1 本书 3 超长章验过,更多题材 / 模型未验。"
        ),
    )

    outline_autofix_enabled: bool = Field(
        default=True,
        description=(
            "v2 W3.5-s4-step1 加。**大纲「名字写岔」确定性自动修工作区级开关**(默认开)。"
            "``True`` 时 outline_consistency_validator 判出跨段硬伤后,若**全部**冲突都是"
            "可确定性修的 name_typo(同一实体本名被手滑写岔,无 substantive/ambiguous),"
            "对 LLM 给的 ``{wrong,correct}`` 走**多数票护栏**(correct 全文出现数 > wrong)"
            "确定性归一 → 重持久化 → **重跑 validator** 确认干净才放行。"
            "``False`` = kill-switch,任何跨段硬伤一律 HARD 抛 OutlineBuilderError(= v1 行为)。\n"
            "**为何默认开**:s4-step1 实证 deepseek 大纲常'几乎干净却被 1 字 typo 连坐 HARD 毙'"
            "(骆秉渊 vs 陆秉渊),精度探针 11/11 验证 LLM 能稳分 typo / distinct 角色 / substantive。\n"
            "**零 fail-open**:含不可修硬伤 / 多数票不过 / 修后仍不过 → 维持 HARD 拦,绝不带病放行。"
        ),
    )

    character_backfill_enabled: bool = Field(
        default=True,
        description=(
            "v2 W3.5-s4-step1 机制1 加。**大纲角色完整性回填工作区级开关**(默认开)。"
            "``True`` 时 build_outline 在 plot 后、volume_plans 前检出'plot 当真实角色"
            "引用、但 characters 没建卡'的指代悬空角色 → 补卡,让 volume_plans 拿到完整"
            "角色表,从源头掐断'抓邻近角色顶替'的张冠李戴混淆。"
            "``False`` = kill-switch,跳过回填(= 机制1 前行为)。\n"
            "**为何默认开**:s4-step1 实证 deepseek/qwen 大纲两次失败共此根因(博拉尔/导师"
            "未建卡 → volume_plans 混淆 → outline_consistency_validator HARD 拦)。\n"
            "**fail-open**:回填是补强非硬闸,setup/LLM/解析/合并任何异常返回 characters "
            "原样不阻断大纲生成(残留交 outline_consistency_validator §2.50 兜底)。"
        ),
    )

    @model_validator(mode="after")
    def _validate_word_count_ordering(self) -> "GenerationConfig":
        if self.word_count_policy_version == 2 and self.word_count_min != self.word_count_max:
            raise ValueError("约数篇幅模式需要 min/max 同存一个目标值，不表示精确字数或下限")
        if self.word_count_min > self.word_count_max:
            raise ValueError(
                "word_count_min 不能大于 word_count_max "
                f"(当前 min={self.word_count_min} > max={self.word_count_max})"
            )
        return self
