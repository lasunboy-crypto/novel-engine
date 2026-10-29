"""Agent 共享:提示词模板加载 + agent 身份 marker(P1-B-22 扩)。

Writer / Reviewer / Reviser 三个 agent 都需要:
1. 按绝对路径加载提示词模板、按路径缓存(Rule of Three 抽取)。
2. 在 user message 末尾附加 agent 身份 marker(P1-B-22 引入,供 Mock
   LLM client 识别;真 LLM 把 HTML 注释当字面忽略,不影响输出质量)。

为什么用 HTML 注释做 marker(P1-B-22 决策)
============================================
- HTML 注释是 markdown 规范明确忽略的字面文本;Claude / Qwen / GPT 都会
  原样视而不见。P3 启用真 LLM 时极小概率 trailing 复读到 Reviewer JSON
  输出,在 Reviewer ``_strip_code_fence`` 里顺手剥掉(本任务一并处理)。
- 是"调用方表明身份成为协议一部分"的设计,类比 HTTP User-Agent。
- 改提示词模板(P7 调优)时不再需要保留某关键字才能让 mock 识别——
  marker 是 Python 代码层加的,不在 .md 模板里。

cache key 是 ``Path`` 对象本身——调用方应在外层先 ``.resolve()`` 一
次,确保同一物理文件的不同写法(``./a/b.md`` vs 绝对路径)命中同
一缓存槽。三个 agent 现都遵守该约定。
"""

from __future__ import annotations

import re
from functools import lru_cache
from pathlib import Path
from typing import TYPE_CHECKING, Any, Literal, get_args

if TYPE_CHECKING:
    from src.core.llm.base import LLMClient, Message
    from src.core.routing.router import ModelRouter

# Agent 身份 marker(P1-B-22)。Mock LLM client 通过正则
# r'<!--\s*agent_role:\s*(writer|reviewer|reviser|extractor|outline_*|chapter_card_generator)\s*-->'
# 识别;真 LLM 视 HTML 注释为字面忽略。
# P2-B-09 起新增 ``extractor`` role(MemoryExtractor agent)。
# P5-B-03 起新增 ``outline_world`` / ``outline_characters`` / ``outline_plot``
# / ``outline_volume_plans`` 4 子角色(OutlineBuilder 4-stage agent);
# MockLLMClient 在对应分支返各段固定 JSON fixture。
# P5-B-05 起新增 ``chapter_card_generator`` 角色(ChapterCardGenerator agent);
# MockLLMClient 在对应分支返合规 ChapterCard fixture(4 Literal 字段命中合法枚举)。
AGENT_ROLE_MARKER_WRITER = "<!-- agent_role: writer -->"
AGENT_ROLE_MARKER_REVIEWER = "<!-- agent_role: reviewer -->"
AGENT_ROLE_MARKER_REVISER = "<!-- agent_role: reviser -->"
AGENT_ROLE_MARKER_EXTRACTOR = "<!-- agent_role: extractor -->"
AGENT_ROLE_MARKER_OUTLINE_WORLD = "<!-- agent_role: outline_world -->"
AGENT_ROLE_MARKER_OUTLINE_CHARACTERS = "<!-- agent_role: outline_characters -->"
AGENT_ROLE_MARKER_OUTLINE_PLOT = "<!-- agent_role: outline_plot -->"
AGENT_ROLE_MARKER_OUTLINE_VOLUME_PLANS = "<!-- agent_role: outline_volume_plans -->"
AGENT_ROLE_MARKER_CHAPTER_CARD_GENERATOR = "<!-- agent_role: chapter_card_generator -->"
# P7-B-06:must_achieve 独立 validator agent。本 marker 不带 chapter_id —
# MockLLMClient 分支直接返默认 yes JSON,不依赖章节上下文路由。
AGENT_ROLE_MARKER_MUST_ACHIEVE_VALIDATOR = "<!-- agent_role: must_achieve_validator -->"
# v1.0.x wave 3 Commit D:衔接桥 LLM-based 独立 validator agent。本 marker 不带
# chapter_id — 与 must_achieve_validator 同型,3 Q 共享同 marker。
AGENT_ROLE_MARKER_CONTINUITY_VALIDATOR = "<!-- agent_role: continuity_validator -->"
# v1.0.x wave 4 session 1:OutlineBuilder 第 5 段 mini_plan 章级事件分配 agent。
# 分卷生成 — 每次调用 user_text 内含当前卷 volume_plan + 滚动上下文。
AGENT_ROLE_MARKER_MINI_PLAN_GENERATOR = "<!-- agent_role: mini_plan_generator -->"
# v1.0.x wave 4 session 3a:章节摘要 LLM 抽取 agent。每章 approved 时一次性调用,
# 输出 ChapterSummaryORM 行候选(summary_text / pov_character / scene_setting /
# key_events / character_interactions / unfinished_actions 6 字段)。本 marker
# 不带 chapter_id — mock 路径从 user_text 抓 chapter_id 派生差异化 fixture。
AGENT_ROLE_MARKER_CHAPTER_SUMMARY_EXTRACTOR = (
    "<!-- agent_role: chapter_summary_extractor -->"
)
# v1.1 wave 1 session 1:G3 beat 拆分路径 — mini_plan one_line_event 是否对齐
# beat.summary + 推进 must_advance + 不触发 forbidden_advance 的 LLM-as-judge agent。
# 本 marker 不带 chapter_id — mock 路径直接返默认 pass JSON,既有 mini_plan
# happy 路径不回归(实施期通过显式 fixture / 单测路径走专属判定)。
AGENT_ROLE_MARKER_BEAT_ALIGNMENT_VALIDATOR = (
    "<!-- agent_role: beat_alignment_validator -->"
)
# v1.1 audit G20 治本(2026-05-27 b1-rerun-deep-audit):章节卡 must_achieve vs
# forbidden 互斥矛盾判定 LLM-as-judge agent。本 marker 不带 chapter_id — mock 路径
# 直接返默认 no_conflict JSON,既有 chapter_card 生成 happy 路径不回归。
AGENT_ROLE_MARKER_MUST_VS_FORBIDDEN_VALIDATOR = (
    "<!-- agent_role: must_vs_forbidden_validator -->"
)
# v1.1 wave 2 session 5 A3 治本(2026-05-29):plot_thread 语义合并 LLM-as-judge
# agent。本 marker 不带 chapter_id — mock 路径直接返默认 no_merge JSON,既有
# memory_extractor happy 路径不回归。
AGENT_ROLE_MARKER_PLOT_THREAD_MERGER = (
    "<!-- agent_role: plot_thread_merger -->"
)
# v1.1 wave 2 session 5 A3 续(2026-05-29):character 语义消歧 LLM-as-judge agent。
# 本 marker 不带 chapter_id — mock 路径直接返默认 no_disambiguate JSON。
AGENT_ROLE_MARKER_CHARACTER_DISAMBIGUATOR = (
    "<!-- agent_role: character_disambiguator -->"
)
# v1.1 wave 2 session 5 A3 续(2026-05-29):timeline_entry 语义合并 LLM-as-judge agent。
# 本 marker 不带 chapter_id — mock 路径直接返默认 no_merge JSON。
AGENT_ROLE_MARKER_TIMELINE_ENTRY_MERGER = (
    "<!-- agent_role: timeline_entry_merger -->"
)
# v1.1 wave 4 session 5(2026-06-01,I2.2):StyleAuditor 风格模板偏移审计 agent。
# 本 marker 不带 chapter_id — mock 路径直接返低 drift_score 默认 JSON。
AGENT_ROLE_MARKER_STYLE_AUDITOR = "<!-- agent_role: style_auditor -->"
# v1.1 wave 6 session 1(2026-06-04,A1 大纲机制具体化):world 段高概念词是否
# 配可执行规则 + 是否给两个互斥答案的 LLM-as-judge agent(outline 生成期自检)。
# 本 marker 不带 chapter_id — mock 路径直接返"无矛盾"默认 JSON,既有 outline
# 生成 happy 路径不回归(真矛盾 = HARD retry / 缺可执行规则 = WARN 标记)。
AGENT_ROLE_MARKER_MECHANISM_CONSISTENCY_VALIDATOR = (
    "<!-- agent_role: mechanism_consistency_validator -->"
)
# v1.1 wave 6 session 1(2026-06-04,A4 核心目标/伏笔收尾):单条 long_thread
# 在结局区是否收口的 LLM-as-judge agent(closed / open_with_setup 护 C1 留白 /
# dangling 埋了不收)。本 marker 不带 chapter_id — mock 路径直接返 closed 默认
# JSON,既有 mini_plan happy 路径不回归(WARN-only,不 HARD retry,护第一原则)。
AGENT_ROLE_MARKER_THREAD_CLOSURE_VALIDATOR = (
    "<!-- agent_role: thread_closure_validator -->"
)
# v1.1 wave 6 session 2(2026-06-04,A3 角色双界身份完整性):多情境身份角色每个
# 身份侧面在全书 mini_plan 事件中是否至少被交代一次的 LLM-as-judge agent(批处理,
# 治 bug A5 白露现实侧整章丢失)。本 marker 不带 chapter_id — mock 路径按候选编号
# 数返"全覆盖"默认 JSON,既有 mini_plan happy 路径不回归(WARN-only,不 HARD)。
AGENT_ROLE_MARKER_IDENTITY_COVERAGE_VALIDATOR = (
    "<!-- agent_role: identity_coverage_validator -->"
)
# v1.1 wave 6 session 3(2026-06-04,B1 设定一致性校验):本章正文是否与设定契约
# (world 不变规则 + 出场角色固定身份)实质冲突的 LLM-as-judge agent(下游审稿期,
# 治 bug A1/A2/A3/A4/A6)。本 marker 不带 chapter_id — mock 路径返"无违反"默认
# JSON,既有审稿 happy 路径不回归(WARN-only,不覆盖 verdict,护第一原则)。
AGENT_ROLE_MARKER_SETTING_CONSISTENCY_VALIDATOR = (
    "<!-- agent_role: setting_consistency_validator -->"
)
# v1.1 wave 6 session 3(2026-06-04,B2 双时空场景锚点):醒来点与入梦点不一致且
# 无位移交代时,LLM 复核是否真硬跳(还是剧情自然差异)的 LLM-as-judge agent
# (extract 后,治 bug B1/B2/B3 肉体瞬移)。本 marker 不带 chapter_id — mock 路径
# 返"自然过渡"默认 JSON,既有 extract happy 路径不回归(WARN-only)。
AGENT_ROLE_MARKER_SCENE_ANCHOR_VALIDATOR = (
    "<!-- agent_role: scene_anchor_validator -->"
)
# v1.1 wave 6 session 4(2026-06-04,B4 正文跨章去重):代码相似度初筛命中的跨章
# 重复片段是注水复读还是有意回扣的 LLM-as-judge agent(下游审稿期,治 bug D1/D2/
# D3 + B4 玉佩逐句重写)。本 marker 不带 chapter_id — mock 路径返"全部 intentional"
# 默认 JSON,既有审稿 happy 路径不回归(WARN-only,不覆盖 verdict,护第一原则)。
AGENT_ROLE_MARKER_CROSS_CHAPTER_DEDUP_VALIDATOR = (
    "<!-- agent_role: cross_chapter_dedup_validator -->"
)
# v1.1 wave 6 session 4(2026-06-04,B6 终局张力曲线):仅终章触发,判定结局是否
# 与前文张力断裂(强行岁月静好)的 LLM-as-judge agent(治 bug D5)。本 marker 不带
# chapter_id — mock 路径返"earned"(自然收束)默认 JSON,既有审稿 happy 路径不回归
# (WARN-only,不覆盖 verdict,护"平静收尾本身不是 bug"第一原则)。
AGENT_ROLE_MARKER_ENDING_TENSION_VALIDATOR = (
    "<!-- agent_role: ending_tension_validator -->"
)
# v1.1 acceptance 修复1(2026-06-10,卷界事件重演):outline:commit 期判定新卷首章
# 是否重演前卷末章已完结事件的 LLM-as-judge agent(治 luo-accept-v1 ch-0007
# "must 永不可达成 → rewrite budget 耗尽"死锁)。本 marker 不带 chapter_id —
# mock 路径返非 JSON → validator fail-open,既有 mock commit 测不回归。
AGENT_ROLE_MARKER_VOLUME_SEAM_REPLAY_VALIDATOR = (
    "<!-- agent_role: volume_seam_replay_validator -->"
)
# v1.1 acceptance 修复2(2026-06-10,Fix D 写前 must 可行性预检):writer 动笔前
# 判定本章 must_achieve 是否已被前文消费(永不可达成 → budget 必耗尽)的
# LLM-as-judge agent(治 luo-accept-v1 ch-0007 同型死锁的写作期最后一道网)。
# 本 marker 不带 chapter_id — mock 路径返非 JSON → validator fail-open 放行写作,
# 既有 mock 编排测不回归。
AGENT_ROLE_MARKER_MUST_FEASIBILITY_VALIDATOR = (
    "<!-- agent_role: must_feasibility_validator -->"
)
# v2 W3.5-S3(2026-06-23,大纲跨段一致性校验):大纲 4 段(world/characters/plot/
# volume_plans)生成后,跨段判定名字/称谓矛盾 + 角色混淆/张冠李戴 + 卷规划覆盖的
# LLM-as-judge agent(qwen)。本 marker 不带 chapter_id — mock 路径返"无矛盾"默认
# JSON(contradictions=[] / coverage_gaps=[]),既有 outline 生成 happy 路径不回归
# (真矛盾 = HARD 抛 OutlineBuilderError 阻断 / 覆盖缺口 = WARN 标记)。
AGENT_ROLE_MARKER_OUTLINE_CONSISTENCY_VALIDATOR = (
    "<!-- agent_role: outline_consistency_validator -->"
)
# v2 W3.5 补强3(2026-06-26,正文级 forbidden 越界下游硬闸):写后读正文判定本章是否
# 真实发生了卡 forbidden 禁止的事(尤其隐式"进入类"越界)的 LLM-as-judge agent(qwen)。
# 本 marker 不带 chapter_id — mock 路径返"无越界"默认 JSON(violations=[]),既有审稿
# happy 路径不回归(真越界 = HARD 覆盖 verdict=rewrite + 合并 failed_constraints)。
AGENT_ROLE_MARKER_FORBIDDEN_CROSSING_VALIDATOR = (
    "<!-- agent_role: forbidden_crossing_validator -->"
)
# v2 W3.5-s4-step1 机制1(2026-06-28,大纲角色完整性回填):build_outline 在 plot 后、
# volume_plans 前检出"plot 当真实角色引用、但 characters 没建卡"的指代悬空角色 → 补卡。
# 本 marker 不带 chapter_id — mock 路径返 {"missing_characters": []}(no-op),既有
# outline happy 路径不回归(真补卡 = 追加角色卡 + 去重 + 封顶 8)。
AGENT_ROLE_MARKER_CHARACTER_BACKFILL = (
    "<!-- agent_role: character_backfill -->"
)

AgentRole = Literal[
    "writer",
    "reviewer",
    "reviser",
    "extractor",
    "outline_world",
    "outline_characters",
    "outline_plot",
    "outline_volume_plans",
    "chapter_card_generator",
    "must_achieve_validator",
    "continuity_validator",
    "mini_plan_generator",
    "chapter_summary_extractor",
    "beat_alignment_validator",
    "must_vs_forbidden_validator",
    "plot_thread_merger",
    "character_disambiguator",
    "timeline_entry_merger",
    "style_auditor",
    "mechanism_consistency_validator",
    "thread_closure_validator",
    "identity_coverage_validator",
    "setting_consistency_validator",
    "scene_anchor_validator",
    "cross_chapter_dedup_validator",
    "ending_tension_validator",
    "volume_seam_replay_validator",
    "must_feasibility_validator",
    "outline_consistency_validator",
    "forbidden_crossing_validator",
    "character_backfill",
]

_MARKER_BY_ROLE: dict[str, str] = {
    "writer": AGENT_ROLE_MARKER_WRITER,
    "reviewer": AGENT_ROLE_MARKER_REVIEWER,
    "reviser": AGENT_ROLE_MARKER_REVISER,
    "extractor": AGENT_ROLE_MARKER_EXTRACTOR,
    "outline_world": AGENT_ROLE_MARKER_OUTLINE_WORLD,
    "outline_characters": AGENT_ROLE_MARKER_OUTLINE_CHARACTERS,
    "outline_plot": AGENT_ROLE_MARKER_OUTLINE_PLOT,
    "outline_volume_plans": AGENT_ROLE_MARKER_OUTLINE_VOLUME_PLANS,
    "chapter_card_generator": AGENT_ROLE_MARKER_CHAPTER_CARD_GENERATOR,
    "must_achieve_validator": AGENT_ROLE_MARKER_MUST_ACHIEVE_VALIDATOR,
    "continuity_validator": AGENT_ROLE_MARKER_CONTINUITY_VALIDATOR,
    "mini_plan_generator": AGENT_ROLE_MARKER_MINI_PLAN_GENERATOR,
    "chapter_summary_extractor": AGENT_ROLE_MARKER_CHAPTER_SUMMARY_EXTRACTOR,
    "beat_alignment_validator": AGENT_ROLE_MARKER_BEAT_ALIGNMENT_VALIDATOR,
    "must_vs_forbidden_validator": AGENT_ROLE_MARKER_MUST_VS_FORBIDDEN_VALIDATOR,
    "plot_thread_merger": AGENT_ROLE_MARKER_PLOT_THREAD_MERGER,
    "character_disambiguator": AGENT_ROLE_MARKER_CHARACTER_DISAMBIGUATOR,
    "timeline_entry_merger": AGENT_ROLE_MARKER_TIMELINE_ENTRY_MERGER,
    "style_auditor": AGENT_ROLE_MARKER_STYLE_AUDITOR,
    "mechanism_consistency_validator": (
        AGENT_ROLE_MARKER_MECHANISM_CONSISTENCY_VALIDATOR
    ),
    "thread_closure_validator": AGENT_ROLE_MARKER_THREAD_CLOSURE_VALIDATOR,
    "identity_coverage_validator": (
        AGENT_ROLE_MARKER_IDENTITY_COVERAGE_VALIDATOR
    ),
    "setting_consistency_validator": (
        AGENT_ROLE_MARKER_SETTING_CONSISTENCY_VALIDATOR
    ),
    "scene_anchor_validator": AGENT_ROLE_MARKER_SCENE_ANCHOR_VALIDATOR,
    "cross_chapter_dedup_validator": (
        AGENT_ROLE_MARKER_CROSS_CHAPTER_DEDUP_VALIDATOR
    ),
    "ending_tension_validator": AGENT_ROLE_MARKER_ENDING_TENSION_VALIDATOR,
    "volume_seam_replay_validator": (
        AGENT_ROLE_MARKER_VOLUME_SEAM_REPLAY_VALIDATOR
    ),
    "must_feasibility_validator": (
        AGENT_ROLE_MARKER_MUST_FEASIBILITY_VALIDATOR
    ),
    "outline_consistency_validator": (
        AGENT_ROLE_MARKER_OUTLINE_CONSISTENCY_VALIDATOR
    ),
    "forbidden_crossing_validator": (
        AGENT_ROLE_MARKER_FORBIDDEN_CROSSING_VALIDATOR
    ),
    "character_backfill": AGENT_ROLE_MARKER_CHARACTER_BACKFILL,
}

# P1-B-24:从 user message 抓 agent 身份 marker(供 LLM client 写
# `llm_request` / `llm_response` 事件的 `agent` 字段;mock_client 沿用
# 同正则做 verdict 路由)。与 mock_client._AGENT_ROLE_MARKER_RE 同源,
# 提到此模块复用,避免两份正则漂移。
#
# v2 间隙 A3①(2026-06-13):alternation 从 ``AgentRole`` Literal 动态构建,
# 替换原手写清单 —— 手写清单曾漏列 ``volume_seam_replay_validator`` /
# ``must_feasibility_validator`` 两角色(Literal / _MARKER_BY_ROLE 都有、
# 正则没有),后果:① 真 LLM client 的 llm_request/llm_response 事件
# ``agent=None``(逐工作区成本画像归属断裂);② mock 模式下这两个
# fail-open validator 因 marker 探测 ValueError 被吞而**静默跳过**。
# 按长度降序拼接,规避 alternation 前缀截断(若未来出现互为前缀的角色名)。
_AGENT_ROLE_MARKER_RE = re.compile(
    r"<!--\s*agent_role:\s*"
    rf"(?P<role>{'|'.join(sorted(get_args(AgentRole), key=len, reverse=True))})"
    r"(?:\s+chapter_id:\s*(?P<chapter_id>ch-[\w-]+))?\s*-->"
)


@lru_cache(maxsize=8)
def load_prompt_template(path: Path) -> str:
    """按绝对路径加载提示词模板,内容以 utf-8 解码并按 path 缓存。

    :param path: 模板文件绝对路径(调用方应已 ``.resolve()``);路径变
        即自动失效(cache key 是 ``Path`` 对象)。
    :return: 模板文本内容。
    :raises FileNotFoundError: 路径不存在;调用方应在更高层兜异常。
    """
    return path.read_text(encoding="utf-8")


def append_role_marker(
    user_text: str,
    role: AgentRole,
    *,
    chapter_id: str | None = None,
) -> str:
    """在 user message 末尾追加 agent 身份 marker。

    P1-B-22 引入,供 Mock LLM client 识别 agent 类型 + reviewer 业务上下文。
    三个 agent 在 ``_build_user_message`` 末尾统一调用本函数。

    Marker 形态
    ===========
    - Writer / Reviser:``<!-- agent_role: writer -->`` /
      ``<!-- agent_role: reviser -->``
    - Reviewer 必带 chapter_id:``<!-- agent_role: reviewer chapter_id: ch-0001 -->``

    Reviewer chapter_id 进 marker 而非 user message 主体的设计意图:
    P1-B-11 Reviewer ``_build_user_message`` 的契约只渲染 pov/setting/
    tension/字数/必达-未解-禁止 三组硬约束,**不渲染 chapter_id 字面**。
    Mock LLM client 需要 chapter_id 决定 verdict 路径,把它放 marker 既不
    污染审稿提示词业务结构,也让"调用方 metadata" 集中在协议层。真 LLM
    仍把 HTML 注释当字面忽略,无副作用。

    :param user_text: 已拼好的 user message 主体。
    :param role: agent 角色,允许 ``writer`` / ``reviewer`` / ``reviser``。
    :param chapter_id: reviewer 业务上下文。``role="reviewer"`` 时必传;
        其他 role 时该参数被忽略(未来可能扩展 writer/reviser 也用)。
    :return: 末尾追加 marker 的 user message(用 ``\\n\\n`` 分隔)。
    :raises ValueError: ``role`` 不在允许集合中,或 reviewer 时 chapter_id 缺失。
    """
    if role not in _MARKER_BY_ROLE:
        raise ValueError(
            f"未知的 agent role: {role!r},允许值 writer / reviewer / reviser"
            f" / extractor / outline_world / outline_characters / outline_plot"
            f" / outline_volume_plans / chapter_card_generator"
            f" / must_achieve_validator / continuity_validator"
            f" / mini_plan_generator / chapter_summary_extractor"
            f" / beat_alignment_validator / must_vs_forbidden_validator"
            f" / plot_thread_merger / character_disambiguator"
            f" / timeline_entry_merger"
        )
    if role == "reviewer":
        if not chapter_id:
            raise ValueError(
                "reviewer marker 必须带 chapter_id(Mock LLM client 需要它"
                "决定 verdict 路径)"
            )
        marker = f"<!-- agent_role: reviewer chapter_id: {chapter_id} -->"
    else:
        marker = _MARKER_BY_ROLE[role]
    return f"{user_text}\n\n{marker}"


def resolve_routing(
    *,
    agent_name: str,
    router: ModelRouter | None,
    client: LLMClient | None,
    model: str | None,
    legacy_kwargs: dict[str, Any],
    chapter_card: Any = None,
) -> tuple[LLMClient, str, dict[str, Any]]:
    """P3-B-06:agent 入口的"router 优先 / legacy 兜底"分发助手。

    每个 agent 入口都需要把"router= 路径"和"client= + model= 路径"统一收口
    到 ``(client, model, complete_kwargs)`` 三元组。本函数集中分发逻辑,避免
    Writer / Reviewer / Reviser / MemoryExtractor / continuity 五个 agent
    各自重复同一段 if/elif/else。

    分发规则
    ========
    - ``router`` 给定 → 调 ``router.route(agent_name, chapter_card=...)``,
      从 ``RoutedTarget`` 取 ``client`` / ``profile.model`` /
      ``complete_kwargs``。**Profile 是生成参数源头,agent 自带的
      ``temperature`` / ``max_tokens`` 默认值在此路径不参与**——profile 不
      声明则下沉到 client 默认(ClaudeClient ``max_tokens=4096`` /
      OllamaClient 不传 ``options``)。
    - ``router=None`` 且 ``client`` + ``model`` 都给定 → legacy 路径,沿用
      旧 agent 调用契约 + 调用方传入的 ``legacy_kwargs``(各 agent 自带的
      ``temperature`` / ``max_tokens`` 等)。
    - 二者都未给(或 legacy 缺一)→ 抛 ``TypeError``,让调用错误显式可见。

    类 B 冲突登记(P3-B-06)
    ========================
    任务模板字面"改注入参数:从 LLMClient 改成 ModelRouter"暗示**替换**;
    工程判断保留 legacy ``client + model`` 路径作为可选兜底,理由:5 个
    agent 各自有 30+ 单测直接以 ``client=fake, model=...`` 调用,纯替换会
    使 ~209 个既有单测失败(P3-B-06 90 分钟粒度无法承担)。本助手把 router
    路径设为新主路径,legacy 留给既有单测继续验"agent 内部 prompt 拼装"
    维度——agent → router → client 的 wiring 维度由新增 ``test_chapter_service_router.py``
    集成测覆盖。具体冲突分类与理由见 ``docs/impl/p3_b_06_agent_router.md``。

    :param agent_name: agent 名,直接传给 ``router.route(agent_name, ...)``;
        ``writer`` 触发规则匹配,其他直接 ``agent_routing`` 表查。
    :param router: ``ModelRouter`` 实例或 ``None``。
    :param client: legacy 路径 LLM 客户端;``router`` 给定时被忽略。
    :param model: legacy 路径模型名;``router`` 给定时被忽略。
    :param legacy_kwargs: legacy 路径 ``client.complete(messages, **kwargs)``
        要透传的额外 kwargs(``temperature`` / ``max_tokens`` 等);agent 内
        部按需要在调用前组好。``router`` 给定时被忽略。
    :param chapter_card: 仅 ``router`` 路径下传给 Writer 规则匹配上下文(其
        他 agent 路径忽略)。
    :returns: ``(client, model, complete_kwargs)`` 三元组,可直接
        ``client.complete(messages, model=model, **complete_kwargs)``。
    :raises TypeError: 二者皆未给定(或 legacy 路径缺 ``client`` / ``model``)。
    """
    if router is not None:
        target = router.route(agent_name, chapter_card=chapter_card)
        return target.client, target.profile.model, dict(target.complete_kwargs)
    if client is None or model is None:
        raise TypeError(
            f"{agent_name}: 必须传入 router= 或 (client= + model=);二者均缺失"
        )
    return client, model, dict(legacy_kwargs)


def detect_agent_role(
    messages: list[Message],
) -> tuple[AgentRole | None, str | None]:
    """读取显式身份；纯 JSON 用户材料可把身份放在 system 消息。

    与 ``mock_client._detect_role_and_chapter_id`` 使用同一正则
    (``_AGENT_ROLE_MARKER_RE``);区别在 **soft 失败**:

    - mock_client:抓不到 marker 抛 ``ValueError``(测试基础设施 fail loud)
    - 本函数:抓不到返回 ``(None, None)``——LLM client 的 ``llm_request``
      事件即使 ``agent=None`` 也能落,不强制(单测可能不带 marker)

    Args:
        messages: ``LLMClient.complete`` 收到的消息序列。

    Returns:
        ``(role, chapter_id)`` 二元组:
        - ``role``:``"writer"`` / ``"reviewer"`` / ``"reviser"`` /
          ``"extractor"`` 或 ``None``
        - ``chapter_id``:仅 reviewer marker 携带时非 ``None``
    """
    user_text = "\n\n".join(m.content for m in messages if m.role == "user")
    match = _AGENT_ROLE_MARKER_RE.search(user_text)
    if match is None:
        system_text = "\n\n".join(m.content for m in messages if m.role == "system")
        match = _AGENT_ROLE_MARKER_RE.search(system_text)
    if match is None:
        return None, None
    role = match.group("role")
    chapter_id = match.group("chapter_id")
    return role, chapter_id  # type: ignore[return-value]
