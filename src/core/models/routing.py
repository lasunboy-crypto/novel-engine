"""模型路由配置(对应 PROJECT_SPEC §7 + ROADMAP §P3 决策 1 / 2)。

P3-B-01 引入。本模块定义 ``RoutingRule`` / ``RoutingConfig`` Pydantic 模型;
不实现规则求值(留 P3-B-05 ``rule_eval.py``)与 ``ModelRouter``(留 P3-B-05)。

决策 1 选 B:扩 ``agent_routing.<agent_name>: <profile_id>`` 段
================================================================
PROJECT_SPEC §7 字面只定义 ``writer_routing`` 段(覆盖 Writer 一个 agent);
ROADMAP §P3 字面"所有 agent 改造为通过 Router 调用"。P3-plan 决策 1 选 B
扩展 ``agent_routing`` 段,所有 agent(``writer`` / ``reviewer`` /
``memory_extractor`` / ``continuity`` / ``reranker`` / ``reviser``)都走
profile 路由。

向后兼容:``writer_routing`` 字段沿用 spec §7 字面命名,**优先级高于
``agent_routing.writer``**(若两段都填,Writer 走 ``writer_routing``);
其他 agent 只走 ``agent_routing``;若 ``agent_routing.<agent>`` 未填,
回退到 ``writer_routing.default``(单一 default profile 兜底所有 agent)。

决策 2 选 B:Router 规则 DSL 走受限 AST 白名单
==============================================
``RoutingRule.condition`` 字段是字符串表达式,如
``"is_arc_finale == true"`` / ``"tension_level >= 9"``。
本模块只存字符串,**不在此校验语法**;具体求值与白名单 AST 节点检查在
P3-B-05 ``rule_eval.py`` 落地(``Compare`` / ``BoolOp`` / ``Name`` /
``Constant`` 四类 AST 节点白名单)。
"""

from __future__ import annotations

from typing import Literal

from pydantic import BaseModel, Field, model_validator

from src.core.models.writer_profile import WriterProfile

# Agent 名枚举:与 ``src/core/agents/`` 现有模块一一对应。
#
# 历史分组(按引入阶段)
# ----------------------
# - **P1-P2 已就位**:writer / reviewer / reviser / memory_extractor / continuity
# - **P3-B-08 引入**:reranker(retriever top_20 → top_5 精排)+
#   reviewer_escalation(决策 4 选 A:边界升 Claude,默认 disable,
#   工作区 ``project.yaml`` 显式开启时才生效)
# - **P4-P5 引入**:outline_builder(整套大纲生成,卷-章双层)+
#   chapter_card_generator(章节卡设计)+ mini_plan_generator(卷内节奏分配,
#   字面留观:实际行为受 outline_builder fallback 控制,v1.1 G3 后续按 stage
#   派发)+ memory_health_checker(记忆健康度 LLM 检查)
# - **v1.0.x wave 4 引入**:chapter_summary_extractor(章节摘要长程记忆)
# - **v1.1 wave 1 引入(LLM-driven validator 族)**:continuity_validator
#   (衔接桥校验)+ must_achieve_validator(must_achieve 项命中校验)+
#   beat_alignment_validator(G3 治本 — mini_plan 是否对齐 beat.summary)+
#   must_vs_forbidden_validator(G20 治本 — must_achieve vs forbidden 互斥校验)
# - **v1.1 wave 2 session 5 引入(A3 治本)**:plot_thread_merger
#   (memory_extractor 抽出的本章 plot_thread 与已有 plot_thread 语义合并判定,
#   防止 luo-clean-v1 ch-1~10 累积 27 条 plot_thread 里 70% 冗余的同型问题)
# - **v1.1 wave 2 session 5 续引入(A3 续)**:character_disambiguator
#   (描述性指代 vs 真名披露 / 简称别名场景的 LLM 兜底,P7-B-05 规则版 0.85
#   相似度漏抓场景的补强)+ timeline_entry_merger(预防性兜底 — 跨章节回忆 /
#   倒叙场景同事件重复登记防御)
#
# **纯规则 validator 不在本枚举**(``src/core/rules/`` 下):
# - style_drift_validator(G5)/ cross_chapter_voice_validator(G10)— 纯规则
#   抽取 + 比对,不调 LLM,不需要 ``agent_routing`` 路由。
# - style_drift_validator(G5)纯规则,不需路由。
#   (注:``style_auditor`` v1.1 w4-s5 / I2.2 落地为真 LLM agent,**已入下方枚举**,
#    走 ``router.route("style_auditor")``;未配置时由 ``default`` profile 兜底。)
#
# 修改本枚举时同步:
# 1. ``src/api/schemas/config.py`` agent_routing 字段 description
# 2. ``src/api/routes/config.py`` ``routable-agents`` 端点元数据
# 3. ``config.yaml`` 顶层 ``agent_routing`` 段(可选;未列出 agent 走 default 兜底)
AGENT_NAME = Literal[
    # P1-P2
    "writer",
    "reviewer",
    "reviser",
    "memory_extractor",
    "continuity",
    # P3
    "reranker",
    "reviewer_escalation",
    # P4-P5
    "outline_builder",
    "chapter_card_generator",
    "mini_plan_generator",
    "memory_health_checker",
    # v1.0.x wave 4
    "chapter_summary_extractor",
    # v1.1 wave 1(LLM-driven validator 族)
    "continuity_validator",
    "must_achieve_validator",
    "beat_alignment_validator",
    "must_vs_forbidden_validator",
    # v1.1 wave 2 session 5 引入(A3 治本)
    "plot_thread_merger",
    # v1.1 wave 2 session 5 续引入(A3 续)
    "character_disambiguator",
    "timeline_entry_merger",
    # v1.1 wave 4 session 5 引入(I2.2 StyleAuditor 落地)
    "style_auditor",
    # v1.1 wave 6 session 1 引入(A1 + A4 outline 生成期内容护栏 LLM-as-judge)
    "mechanism_consistency_validator",
    "thread_closure_validator",
    # v1.1 wave 6 session 2 引入(A3 角色双界身份覆盖度 LLM-as-judge)
    "identity_coverage_validator",
    # v1.1 wave 6 session 3 引入(B1 设定一致性 + B2 双时空场景锚点 下游 LLM-as-judge)
    "setting_consistency_validator",
    "scene_anchor_validator",
    # v1.1 wave 6 session 4 引入(B4 正文跨章去重 + B6 终局张力曲线 下游 LLM-as-judge)
    "cross_chapter_dedup_validator",
    "ending_tension_validator",
    # v1.1 acceptance 修复1 引入(outline:commit 期卷界事件重演 LLM-as-judge)
    "volume_seam_replay_validator",
    # v1.1 acceptance 修复2 引入(Fix D 写前 must 可行性预检 LLM-as-judge)
    "must_feasibility_validator",
    # v2 W3.5-S3 引入(大纲生成后跨段一致性校验 LLM-as-judge,qwen)
    "outline_consistency_validator",
    # v2 W3.5 补强3 引入(写后正文级 forbidden 越界判定 LLM-as-judge,qwen,下游硬闸)
    "forbidden_crossing_validator",
]


# ============================================================================
# LLM-as-judge profile hard guard(v1.1 wave 2 临时会话 2026-05-28 立)
# ============================================================================
#
# wave 1 治理依赖以下 4 个 LLM-as-judge agent 走 thinking-on profile(深度斟酌
# 互斥矛盾 / 剧透 / 衔接错位)。任一切到 thinking-off profile 会让 LLM-as-judge
# 准确度漂移 → wave 1 治理失效。本集合作为 ModelRouter.select_profile 出口
# hard guard 的输入,resolve 出非 thinking-on profile 时 raise ValueError。
#
# 防御场景:
# - w2-s7 G9.4 本地 small model 实验 — 误切到非 thinking-on profile
# - w3-s2 H5 UI routing 切换器 — 用户误切 LLM-judge agent 到 thinking off
# - 工作区 project.yaml agent_routing 整段覆盖 — 漏列 LLM-judge agent 走 default 兜底
#   (若 default 是 thinking off)
LLM_AS_JUDGE_AGENTS: frozenset[str] = frozenset(
    {
        "must_vs_forbidden_validator",  # G20 治本(must vs forbidden 互斥)
        "beat_alignment_validator",      # G3 治本(mini_plan 对齐 beat.summary)
        "must_achieve_validator",        # must_achieve 项命中
        "continuity_validator",          # 衔接桥校验
        # v1.1 wave 2 session 5 A3 治本(2026-05-29 加入):plot_thread_merger
        # 是 LLM-as-judge 模式(LLM 综合判定本章 extracted 与既有 plot_thread 语义
        # 重复对),与 wave 1 治理 4 个 validator 同型保护栏 — thinking on profile
        # 保证合并判定准确度,避免误合并 / 漏合并导致数据损失。
        "plot_thread_merger",            # A3 治本(plot_thread 语义合并)
        # v1.1 wave 2 session 5 A3 续(2026-05-29 加入):character_disambiguator
        # 治描述性指代 vs 真名披露场景(P7-B-05 规则版 0.85 相似度漏抓);
        # timeline_entry_merger 治跨章节回忆 / 倒叙同事件重复登记。
        # 两者都是 LLM-as-judge 同型保护栏 — thinking on 保证判定准确度。
        "character_disambiguator",       # A3 续(character 描述性指代消歧)
        "timeline_entry_merger",         # A3 续(timeline_entry 语义合并)
        # v1.1 wave 6 session 1(2026-06-04 加入):大纲生成期两个内容质量自检
        # LLM-as-judge agent,与上述同型保护栏 — thinking on 保证语义判定准确度。
        "mechanism_consistency_validator",  # A1(world 高概念机制矛盾 / 缺规则)
        "thread_closure_validator",         # A4(long_thread 结局区收口判定)
        # v1.1 wave 6 session 2(2026-06-04 加入):A3 角色双界身份覆盖度判定,
        # 与上述同型保护栏 — thinking on 保证"某身份侧面是否被交代"语义判定准确度。
        "identity_coverage_validator",      # A3(多情境身份侧面全书覆盖度)
        # v1.1 wave 6 session 3(2026-06-04 加入):下游一致性校验两个 LLM-as-judge,
        # 与上述同型保护栏 — thinking on 保证"正文是否违反设定 / 是否真硬跳"语义判定准确度。
        "setting_consistency_validator",    # B1(本章正文违反设定契约判定)
        "scene_anchor_validator",           # B2(醒来点≠入梦点是否真硬跳复核)
        # v1.1 wave 6 session 4(2026-06-04 加入):B4 正文跨章去重意图判定 + B6
        # 终局张力断裂判定,与上述同型保护栏 — thinking on 保证语义判定准确度。
        "cross_chapter_dedup_validator",    # B4(跨章重复是注水还是有意回扣判定)
        "ending_tension_validator",         # B6(终章结局是否与前文张力断裂判定)
        # v1.1 acceptance 修复1(2026-06-10 加入):outline:commit 期卷界事件重演
        # 判定(新卷首章是否重新安排/重新解决前卷末章已完结事件 — luo-accept-v1
        # ch-0007 死锁现场),与上述同型保护栏 — thinking on 保证"重演 vs 后果
        # 延续 vs 行动线下一阶段"语义区分准确度(标定实证:表面重叠分不开,全靠
        # 语义判)。
        "volume_seam_replay_validator",     # acceptance 修复1(卷界事件重演判定)
        # v1.1 acceptance 修复2 / Fix D(2026-06-10 加入):写前逐条判定本章
        # must_achieve 是否已被前文消费(同一事件已在前章发生并解决完毕 → 本章
        # 永不可达成 → budget 必耗尽)。thinking on 保证"同一事件 vs 后果延续
        # vs 可转述达成"语义区分准确度(高置信才升人工,误杀代价高)。
        "must_feasibility_validator",       # acceptance 修复2(写前 must 可行性预检)
        # v2 W3.5-S3(2026-06-23 加入):大纲生成后跨段一致性校验(名字/称谓矛盾 +
        # 角色混淆/张冠李戴 + 卷规划覆盖)。T1 实证 deepseek 大纲 2/2 有一致性硬伤
        # (会下游污染出卡→正文)→ deepseek 生成 + qwen 校验组合拳。thinking on 保证
        # "同一实体跨段是否矛盾 / 两角色是否被混为一人"语义判定准确度(才华vs纪律:
        # deepseek 漏自己的一致性 bug,换纪律更强的 qwen 当第二双眼)。
        "outline_consistency_validator",    # W3.5-S3(大纲跨段一致性校验,qwen)
        # v2 W3.5 补强3(2026-06-26 加入):写后读正文判定本章是否真实发生卡 forbidden
        # 禁止的事(尤其隐式"进入类"越界 = 终点 overshoot)。卡层预防(A/propagation/B)
        # 永远是概率,本判子是唯一读正文的硬闸。thinking on 保证"接近临界点 vs 真跨过 /
        # 隐式越界 vs 留白"语义区分准确度(命门=零误杀,误杀好章触发 rewrite churn)。
        # 走 qwen:deepseek 正是越界的写作模型、自审易太宽容;qwen 卡线上停=严格第二双眼。
        "forbidden_crossing_validator",     # W3.5 补强3(正文级 forbidden 越界,qwen 硬闸)
    }
)


def is_thinking_on_profile(profile: WriterProfile) -> bool:
    """判定 WriterProfile 是否 thinking-on(LLM-as-judge hard guard 用)。

    各 provider thinking 字段映射:

    - ``mock``:always pass(单测不调真 LLM,thinking 无意义)
    - ``claude``:always pass(Claude 默认 reasoning 较强,不强求显式 thinking on)
    - ``ollama``:看 ``extra_request_kwargs.think`` 字面值
    - ``openai_compatible``:看 ``extra_request_kwargs.enable_thinking`` 字面值
    - 未知 provider:默认 pass(向后兼容,新 provider 接入时显式扩本函数)

    :param profile: 已通过 ``WriterProfilesConfig`` 交叉校验的 profile
    :return: ``True`` 当且仅当 profile 满足 thinking-on 语义
    """
    provider = profile.provider
    if provider in ("mock", "claude"):
        return True
    extra = profile.extra_request_kwargs
    if provider == "ollama":
        return bool(extra.get("think"))
    if provider == "openai_compatible":
        return bool(extra.get("enable_thinking"))
    return True


class RoutingRule(BaseModel):
    """单条路由规则(对齐 PROJECT_SPEC §7 字面)。

    每条规则匹配一个布尔条件;命中规则返回对应 ``profile_id``。规则按
    ``RoutingConfig.rules`` 列表顺序优先匹配,首个 match 即返,后续不再求值。
    """

    condition: str = Field(
        ...,
        description=(
            "布尔表达式字符串,在受限 AST 白名单下求值(P3-B-05 ``rule_eval``)。"
            "可访问字段名约定:``is_arc_finale`` / ``tension_level`` / "
            "``arc_phase`` 等 ChapterCard 公开字段。"
            "示例:``is_arc_finale == True`` / ``tension_level >= 9``。"
        ),
        min_length=1,
    )
    profile: str = Field(
        ...,
        description="命中本规则时使用的 ``WriterProfile.profile_id``",
        min_length=1,
        max_length=64,
    )


class RoutingConfig(BaseModel):
    """路由配置(对齐 PROJECT_SPEC §7 + 决策 1 选 B 扩 agent_routing)。

    加载顺序与覆盖层级:
    1. 引擎默认 ``config.yaml`` 顶层 ``writer_routing`` / ``agent_routing``
    2. 工作区 ``project.yaml`` 同字段(P3-B-02 三层配置覆盖)
    3. 运行时覆盖(P3-F-03 WriterProfileSelector 单次写作 profile_override)

    后者覆盖前者(整段替换,不字段级合并)。
    """

    default: str = Field(
        ...,
        description=(
            "默认 profile_id;``writer_routing.rules`` 全部不命中时 Writer 走此 profile。"
            "也作为 ``agent_routing`` 未配置 agent 的兜底(回退路径)。"
        ),
        min_length=1,
        max_length=64,
    )
    rules: list[RoutingRule] = Field(
        default_factory=list,
        description=(
            "Writer 路由规则列表,按顺序优先匹配。空列表表示 Writer 始终走 ``default``。"
            "对齐 spec §7 字面 ``writer_routing.rules``。"
        ),
    )
    agent_routing: dict[str, str] = Field(
        default_factory=dict,
        description=(
            "P3-plan 决策 1 选 B 引入。其他 agent 路由表,key=agent_name,value=profile_id。"
            "未填的 agent 回退到 ``default`` profile。"
            "key 取值集与 ``AGENT_NAME`` Literal 对齐;此处用 ``dict[str, str]`` "
            "而非 Literal key,保留 P5+ 加新 agent 时不破坏向后兼容的灵活性。"
        ),
    )

    def resolve_for_writer(self, context: dict) -> str:
        """按规则匹配返回 Writer 应使用的 profile_id。

        :param context: 求值上下文(通常是 ChapterCard 字段 dict)。
        :return: ``rules`` 首个命中规则的 ``profile`` 字段;无命中返 ``default``。

        **实际规则求值(condition 字符串 → bool)在 P3-B-05 ``rule_eval`` 落地**;
        本方法是签名占位,P3-B-01 不实现求值逻辑,直接返 ``default`` 让所有
        Writer 调用走默认 profile。P3-B-05 替换实现时签名不变,caller 不需改。
        """
        # P3-B-01 桩:统一返 default。P3-B-05 替换为受限 AST 求值。
        del context  # 显式忽略,提示 reviewer 这里是 stub
        return self.default

    def resolve_for_agent(self, agent_name: str) -> str:
        """按 agent 名返回应使用的 profile_id。

        :param agent_name: agent 名,期望取值见 ``AGENT_NAME`` Literal。
        :return: ``agent_routing.<agent_name>`` 字段值;未配置时返 ``default``。

        Writer agent 也支持走本方法(返 ``agent_routing.writer`` 或 ``default``);
        但通常 Writer 走 ``resolve_for_writer`` 路径以触发条件规则匹配。
        """
        return self.agent_routing.get(agent_name, self.default)


class WriterProfilesConfig(BaseModel):
    """整套 profile + routing 配置容器(从 ``config.yaml`` 加载顶层两段)。

    P3-B-01 引入。``src/core/config_loader.py:load_writer_profiles_config``
    解析 yaml 后返回本对象;P3-B-02 工作区覆盖也走同样数据结构(整段替换语义)。

    交叉校验
    ========
    ``routing.default`` / ``routing.rules[*].profile`` /
    ``routing.agent_routing.values()`` 引用的所有 ``profile_id`` 必须存在
    ``profiles`` 字典里。任一引用未声明 → 加载时抛 ValidationError,避免
    运行时 Router 命中"未定义 profile"再排查。
    """

    profiles: dict[str, WriterProfile] = Field(
        ...,
        description=(
            "profile 字典,key=``profile_id``,value=``WriterProfile``。"
            "yaml 顶层 ``writer_profiles`` 段映射到此字段。"
            "构造时 Pydantic 自动校验 key 与 value.profile_id 是否一致(本字段不强制,"
            "但 ``load_writer_profiles_config`` 会落地校验)。"
        ),
        min_length=1,
    )
    routing: RoutingConfig = Field(
        ...,
        description="路由配置,yaml 顶层 ``writer_routing`` 段映射到此字段",
    )

    @model_validator(mode="after")
    def _validate_routing_references(self) -> "WriterProfilesConfig":
        """所有 routing 引用的 profile_id 必须在 profiles 字典里。"""
        all_profile_ids = set(self.profiles.keys())

        if self.routing.default not in all_profile_ids:
            raise ValueError(
                f"writer_routing.default={self.routing.default!r} 引用的 profile 未在 "
                f"writer_profiles 里声明;已声明 profile: {sorted(all_profile_ids)}"
            )

        for idx, rule in enumerate(self.routing.rules):
            if rule.profile not in all_profile_ids:
                raise ValueError(
                    f"writer_routing.rules[{idx}].profile={rule.profile!r} 引用的 "
                    f"profile 未在 writer_profiles 里声明"
                )

        for agent, profile_id in self.routing.agent_routing.items():
            if profile_id not in all_profile_ids:
                raise ValueError(
                    f"agent_routing[{agent!r}]={profile_id!r} 引用的 profile 未在 "
                    f"writer_profiles 里声明"
                )

        for key, profile in self.profiles.items():
            if profile.profile_id != key:
                raise ValueError(
                    f"writer_profiles[{key!r}].profile_id={profile.profile_id!r} 与字典 key 不一致;"
                    "yaml 写法应让 key 与每个 profile 的 profile_id 字段相等"
                )

        return self
