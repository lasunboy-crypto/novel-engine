"""脱机 Mock LLM 客户端(P1-B-22)。

对齐 ``CLAUDE.md §维护人环境约束``:维护人无 Claude API key,P1-P2 默认
走 mock 不调真 API。本类继承 ``LLMClient`` 协议,通过 ``messages`` 末尾
的 agent 身份 marker(P1-B-22 引入)分发到 Writer / Reviewer / Reviser
分支,各自返回符合下游契约的合成响应。

设计要点
========

1. **Agent 识别走显式 marker,不走 system 关键字 substring**:Writer /
   Reviewer / Reviser 在 user message 末尾追加
   ``<!-- agent_role: writer|reviewer|reviser -->``;Mock 用正则抓取。
   抓不到直接抛 ``ValueError`` —— 测试基础设施 fail loud,不容错。
2. **Reviewer verdict 由环境变量脚本化**:``LLM_MOCK_VERDICT_<CHID>`` >
   ``LLM_MOCK_VERDICT`` > 默认 ``pass``。允许值 ``pass / revise /
   rewrite``,其他抛 ``ValueError``。
3. **不维护 reviewer call count**:收敛由 ChapterCard ``max_revises`` /
   ``max_rewrites`` + Orchestrator(P1-B-13)预算决定;Mock 不僭越业务
   语义,否则 P1-B-23 验收无法测试"预算耗尽升人工"路径。
4. **总调用次数硬上限 128 是安全网**:防止真 bug 导致无限循环把测试卡死,
   不是业务收敛。正常验收每章 < 10 次调用,远低于阈值。
5. **complete 签名对齐 ClaudeClient**:``model: str | None = None`` 显式
   暴露,``model_used`` 反映回 ``mock-{requested_model}`` 便于日志区分。
6. **Reviewer JSON 输出严格按 P1-B-11 ``_ReviewerLLMPayload`` schema +
   PROJECT_SPEC §4.4 反推 verdict 所需的 score**:测试里
   ``ReviewResult.model_validate_json`` 校验,P2/P3 schema 改动会立即报红。

P1 不实现
=========
- Mock token streaming(P1-B-20 WS 不推 token 进度)
- 故意失败模拟(429/5xx)——登记 v2_wishlist 必要时再做
- 多模型分档(mock_quality / mock_premium)——单实例,model_used 用请求
  模型名做后缀
"""

from __future__ import annotations

import json
import os
import re
import time
from typing import Any, Literal

import structlog

from src.core.agents._prompt import AgentRole, detect_agent_role
from src.core.llm.base import LLMClient, LLMResponse, Message, TokenUsage
from src.core.llm.cancel import raise_if_cancelled
from src.core.llm.prompt_hash import hash_prompt

_log = structlog.get_logger(__name__)

# P1-B-24:provider 标识。``llm_request`` / ``llm_response`` 事件 ``provider``
# 字段;archive_prompt 也按 provider 决定是否落原文(mock 不落)。
_PROVIDER = "mock"

# 章节 ID 格式:与 PROJECT_SPEC / 现有 ch-XXXX 命名对齐。
# Writer 分支用于从 user message 抓 chapter_id 当作占位文本里的章节编号(可选)。
_CHAPTER_ID_RE = re.compile(r"\bch-[\w-]+\b")

# 单实例累计调用安全网。正常验收每章 < 10 次调用;真触发即说明 Orchestrator/Agent
# 收敛逻辑有 bug(无限循环冲数千次,本上限照样抓)。
# v2 W3.5-S3:50 → 64。outline_consistency_validator(大纲生成后跨段校验)给 build_outline
# +1 LLM 调用,最重的单测流程(全大纲 3 卷 + 2 卡批量,单实例共享 MockLLMClient)从 ~49
# 推过 50 → 误触安全网(非真循环)。
# v2 W3.5-D2:64 → 128。mock volume_plans 现在产出 beat 级结构与合法 linked_*,
# API 快乐路径可在同一 mock 实例内连续跑 outline generate + commit 自动出卡
# + 显式卡生成;这仍是有限集成链路,不是无限循环。
_TOTAL_CALL_SAFETY_LIMIT = 128

# Verdict 环境变量。
_ENV_GLOBAL_VERDICT = "LLM_MOCK_VERDICT"
_ENV_CHAPTER_VERDICT_PREFIX = "LLM_MOCK_VERDICT_"

_ALLOWED_VERDICTS: tuple[str, ...] = ("pass", "revise", "rewrite")
Verdict = Literal["pass", "revise", "rewrite"]

# Mock 写作占位段落。重复拼接到 word_count_target 附近(默认 3000 字)。
_MOCK_PARAGRAPH = (
    "风从北面吹来,带着远方的尘土。林衍站在山门前,衣袂被风掀起,"
    "目光沉静地望着远处那道身影。剑柄在掌心微微发热,他知道,真正的"
    "考验才刚刚开始。脚下青石被岁月磨得发亮,每一道裂痕都像是过往剑"
    "意留下的痕迹。他深吸一口气,把杂念压回胸腔深处。"
)


class MockLLMClient(LLMClient):
    """脱机 Mock LLM 客户端。

    无 ``api_key`` / ``model`` 构造参数(无端点);每次 ``complete`` 调用
    根据 user message 末尾的 marker 分发到 writer / reviewer / reviser
    三个分支,各自返回合成响应。
    """

    def __init__(self) -> None:
        self._total_calls: int = 0

    def complete(
        self,
        messages: list[Message],
        *,
        model: str | None = None,
        **gen_params: Any,
    ) -> LLMResponse:
        """同步生成补全。

        :param messages: 对话消息;user message 末尾必须含 agent 身份 marker。
        :param model: 请求的模型名(用于 ``model_used`` 后缀,默认
            ``"mock-default"``)。
        :param gen_params: 透传忽略(``temperature`` / ``max_tokens`` 等
            mock 不消费)。
        :return: 合成 ``LLMResponse``。
        :raises RuntimeError: 单实例累计调用 ≥ 128(安全网)。
        :raises ValueError: marker 缺失 / verdict env 取值非法 / reviewer
            分支抓不到 chapter_id。
        """
        # J1:pre-flight 取消检查(与真 client 对齐)。让单测可用
        # ``bound_cancel_event`` + ``event.set()`` 模拟取消而无需真 LLM,且后台
        # 任务跑 mock 路径时也尊重 cancel 信号。
        raise_if_cancelled()
        self._total_calls += 1
        if self._total_calls > _TOTAL_CALL_SAFETY_LIMIT:
            raise RuntimeError(
                f"MockLLMClient 单实例累计调用 ≥ {_TOTAL_CALL_SAFETY_LIMIT},"
                "疑似无限循环 bug,请检查 Orchestrator/Agent 收敛逻辑。"
            )

        role, marker_chapter_id = _detect_role_and_chapter_id(messages)
        user_text = _join_user_messages(messages)

        # P1-B-24:OBSERVABILITY §2.4 `llm_request` 事件。``prompt_hash`` 落
        # 字段;mock 不调 archive_prompt(决策:mock 阶段无 prompt 调优需求)。
        # ``input_tokens`` 是预估值(响应前不知道实际 token);响应后的
        # `llm_response` 事件用 ``usage`` 内的实际值再发一遍。
        prompt_hash = hash_prompt(messages)
        estimated_input_tokens = max(1, sum(len(m.content) for m in messages) // 3)
        requested_model = model or "default"
        _log.info(
            "llm_request",
            provider=_PROVIDER,
            model=requested_model,
            agent=role,
            input_tokens=estimated_input_tokens,
            prompt_hash=prompt_hash,
        )

        start = time.perf_counter()

        if role == "writer":
            content = _generate_writer_content(user_text)
        elif role == "reviser":
            content = _generate_reviser_content(user_text)
        elif role == "reviewer":
            if marker_chapter_id is None:
                raise ValueError(
                    "Mock reviewer 分支:marker 未携带 chapter_id。P1-B-22 协议"
                    "要求 reviewer marker 形态 "
                    "<!-- agent_role: reviewer chapter_id: ch-XXXX -->,请检查 "
                    "src.core.agents.reviewer._build_user_message 拼接。"
                )
            verdict = _resolve_verdict(marker_chapter_id)
            content = _generate_reviewer_json(marker_chapter_id, verdict)
        elif role == "extractor":
            # P2-B-19:MemoryExtractor 真接入 Orchestrator 后,mock 必须返合规
            # 4 类 JSON 让 verdict=pass 路径下 :write 整链路跑通。沿 P1-B-22
            # 协议从 user_text 抓 chapter_id;抓不到走 "ch-unknown" 兜底。
            content = _generate_extractor_json(user_text)
        elif role == "chapter_card_generator":
            # P5-B-05:ChapterCardGenerator agent 返合规 ChapterCard "决策"
            # JSON(只含 LLM 创意字段;系统字段 chapter_id / workspace_id /
            # volume_id / chapter_number / arc_phase / is_arc_finale /
            # tension_level 由 caller 注入)。3 Literal 字段
            # (continuity_context.scene_continuation / pov_change +
            # arc_phase 显式列举)严格命中合法枚举,
            # 让单测 + 跨字段引用校验路径可走通。
            content = _generate_chapter_card_generator_json(user_text)
        elif role == "must_achieve_validator":
            # P7-B-06:must_achieve 独立 LLM 判定 agent。
            # G9.3 改批处理(v1.1 wave 2 session 6,2026-05-29):单次 LLM 调用
            # 一次性返回 N 条 must_achieve 的 list 判定,mock 解析 user_text 里
            # ``## 必达事件列表`` 段数有几条编号生成对应 results list。
            # Mock 始终返全 yes,不破坏既有 happy 路径(must_achieve 非空的
            # fixture 走 reviewer 自然 verdict,validator 不该误判 false 让 happy
            # 测变 escalate)。
            content = _generate_must_achieve_validator_json(user_text)
        elif role == "continuity_validator":
            # v1.0.x wave 3 Commit D:衔接桥 LLM-based 独立 validator agent。
            # 3 Q 条件 gated 独立 yes/no 调用。Mock 始终返 ``verdict=yes`` 不
            # 破坏既有 happy 路径(continuity_context 非空 fixture 走 reviewer
            # 自然 verdict,validator 不该误判让 happy 测变 rewrite)。
            # 不依赖 chapter_id(对齐 must_achieve_validator 同型)。
            content = _generate_continuity_validator_json()
        elif role == "mini_plan_generator":
            # v1.0.x wave 4 session 1:OutlineBuilder 第 5 段章级事件分配 agent。
            # 单卷 mini_plan 调用 — user_text 内含本卷 chapter_id 映射表 +
            # arc_id 列表 + character_voices + thread_ids。fixture 按 arc
            # 切分 + 动词池轮换 + 主语 / 宾语切片差异化派生 one_line_event,
            # **杜绝按 chapter_number 简单派生占位字面**(避免 mock 假递进
            # 盲区,wave 4 session 1 立差异化测试纪律)。
            content = _generate_mini_plan_generator_json(user_text)
        elif role == "chapter_summary_extractor":
            # v1.0.x wave 4 session 3a:章节摘要 LLM 抽取 agent。每章 approved
            # 时一次性调用。fixture 按 chapter_number 轮换动词 / 宾语 / 场景修
            # 饰差异化派生 summary_text + key_events,**杜绝按 chapter_number
            # 简单派生占位字面**(继承 wave 4 立的差异化测试纪律,Q4 阈值会话
            # 3a 起收紧到 < 50% 生产级标准)。
            content = _generate_chapter_summary_extractor_json(user_text)
        elif role == "beat_alignment_validator":
            # v1.1 wave 1 session 1:G3 beat 拆分路径 — mini_plan one_line_event
            # 是否对齐 beat.summary + 推进 must_advance + 不触发 forbidden_advance
            # 的 LLM-as-judge agent。Mock 始终返 ``verdict=pass`` 不破坏既有
            # mini_plan happy 路径(测试期通过显式 fixture / 单测路径走专属判定)。
            # 不依赖 chapter_id(对齐 must_achieve_validator 同型;marker 不带
            # chapter_id)。
            content = (
                _generate_beat_alignment_batch_validator_json(user_text)
                if "[beat_alignment_batch]" in user_text
                else _generate_beat_alignment_validator_json()
            )
        elif role == "must_vs_forbidden_validator":
            # v1.1 audit G20 治本(2026-05-27 b1-rerun-deep-audit):章节卡
            # must_achieve vs forbidden 互斥矛盾判定 LLM-as-judge agent。Mock 始终
            # 返 ``verdict=no_conflict`` 不破坏既有 chapter_card 生成 happy 路径
            # (测试期通过显式 fixture / 单测路径走专属判定)。不依赖 chapter_id
            # (对齐 beat_alignment_validator / must_achieve_validator 同型)。
            content = _generate_must_vs_forbidden_validator_json()
        elif role == "style_auditor":
            # v1.1 wave 4 session 5 / I2.2:StyleAuditor 风格模板偏移审计 agent。
            # Mock 始终返低 drift_score(2.0 → severity=ok),不破坏既有 happy
            # 路径(选了模板的工作区审计默认"契合")。不依赖 chapter_id。
            content = _generate_style_auditor_json()
        elif role == "mechanism_consistency_validator":
            # v1.1 wave 6 session 1 / A1:world 高概念词机制具体化 LLM-as-judge。
            # Mock 始终返"无矛盾 + 无缺规则"(contradictions=[] / vague=[]),不破坏
            # 既有 outline 生成 happy 路径(build_world post_validate 不误触 HARD
            # retry)。不依赖 chapter_id(对齐 beat_alignment_validator 同型)。
            content = _generate_mechanism_consistency_validator_json()
        elif role == "outline_consistency_validator":
            # v2 W3.5-S3:大纲 4 段跨段一致性校验 LLM-as-judge。Mock 始终返"无矛盾 +
            # 无覆盖缺口"(contradictions=[] / coverage_gaps=[]),不破坏既有 outline
            # 生成 happy 路径(build_outline volume_plans 后接入,不误触 HARD 阻断)。
            # 不依赖 chapter_id(对齐 mechanism_consistency_validator 同型)。
            content = (_generate_outline_split_consistency_validator_json(user_text)
                       or _generate_outline_consistency_validator_json())
        elif role == "thread_closure_validator":
            # v1.1 wave 6 session 1 / A4:long_thread 结局区收口**批处理** LLM-as-judge。
            # Mock 按 ``## 候选伏笔列表`` 编号数返对应数量 ``verdict=closed``(沿用
            # must_achieve_validator G9.3 批处理 mock 自适应)。closed 不破坏既有
            # mini_plan happy 路径(WARN-only 本就不 raise,closed 还避免误产 dangling
            # warn log)。不依赖 chapter_id(对齐 beat_alignment_validator 同型)。
            content = _generate_thread_closure_validator_json(user_text)
        elif role == "identity_coverage_validator":
            # v1.1 wave 6 session 2 / A3:多情境身份角色每侧全书覆盖度**批处理**
            # LLM-as-judge。Mock 按 ``## 候选角色`` 编号数返对应数量
            # ``uncovered_contexts=[]``(全覆盖,沿用 thread_closure 批处理 mock
            # 自适应)。全覆盖不破坏既有 mini_plan happy 路径(WARN-only 本不 raise,
            # 空 uncovered 还避免误产 gaps warn log)。不依赖 chapter_id。
            content = _generate_identity_coverage_validator_json(user_text)
        elif role == "setting_consistency_validator":
            # v1.1 wave 6 session 3 / B1:本章正文是否违反设定契约 LLM-as-judge
            # (下游审稿期)。Mock 始终返 ``violations=[]``(本章无违反),不破坏既有
            # 审稿 happy 路径(WARN-only 本不覆盖 verdict,空 violations 还避免误产
            # warn log)。不依赖 chapter_id(对齐 mechanism_consistency_validator 同型)。
            content = _generate_setting_consistency_validator_json()
        elif role == "forbidden_crossing_validator":
            # v2 W3.5 补强3:本章正文是否真实发生卡 forbidden 禁止的事 LLM-as-judge
            # (下游审稿期硬闸)。Mock 始终返 ``violations=[]``(本章无越界),不破坏
            # 既有审稿 happy 路径(HARD 但空 violations → passed=True 不覆盖 verdict)。
            # 单测路径走专属 fixture 注入越界。不依赖 chapter_id(对齐 setting_consistency 同型)。
            content = _generate_forbidden_crossing_validator_json()
        elif role == "scene_anchor_validator":
            # v1.1 wave 6 session 3 / B2:醒来点≠入梦点时 LLM 复核是否真硬跳
            # (extract 后)。Mock 始终返 ``verdict=natural``(自然过渡 / 有交代),
            # 不破坏既有 extract happy 路径(WARN-only,natural 还避免误产 warn log)。
            # 不依赖 chapter_id(对齐 continuity_validator 同型单 verdict)。
            content = _generate_scene_anchor_validator_json()
        elif role == "cross_chapter_dedup_validator":
            # v1.1 wave 6 session 4 / B4:代码初筛命中的跨章重复是注水复读还是
            # 有意回扣的**批处理** LLM-as-judge(下游审稿期)。Mock 按
            # ``## 候选重复片段列表`` 编号数返对应数量 ``verdict=intentional``
            # (默认全判有意回扣,护第一原则)。intentional 不破坏既有审稿 happy
            # 路径(WARN-only 本不覆盖 verdict,全 intentional 还避免误产 warn log)。
            # 不依赖 chapter_id(对齐 thread_closure 批处理 mock 自适应)。
            content = _generate_cross_chapter_dedup_validator_json(user_text)
        elif role == "ending_tension_validator":
            # v1.1 wave 6 session 4 / B6:仅终章判定结局是否与前文张力断裂。Mock
            # 始终返 ``verdict=earned``(自然收束),不破坏既有审稿 happy 路径(WARN-only,
            # earned 还避免误产 warn log)。不依赖 chapter_id(对齐 scene_anchor 单 verdict)。
            content = _generate_ending_tension_validator_json()
        elif role == "must_feasibility_validator":
            # v2 间隙 A3①(2026-06-13):Fix D 写前预检(批处理单次,fail-open)。
            # 此前 marker 正则漏列该角色 → mock 模式下探测 ValueError 被
            # fail-open 吞掉 = 静默跳过。Mock 按 user_text ``## 待判定`` 段编号
            # 数自适应返对应条数(条数不齐会被调用方判 fail-open),每条
            # verdict=feasible 护既有 happy 路径(不升人工)。
            content = _generate_must_feasibility_validator_json(user_text)
        elif role == "volume_seam_replay_validator":
            # v2 间隙 A3①(2026-06-13):卷界事件重演判定(粗筛 + LLM,fail-open)。
            # 同上,正则漏列致 mock 静默跳过。Mock 返 ``replay=false``(无重演)
            # 护既有 outline commit happy 路径。粗筛(char-bigram 包含度)对短
            # fixture 文本天然返 0 不触发 LLM,本分支是真触发时的兜底。
            content = _generate_volume_seam_replay_validator_json()
        elif role == "character_backfill":
            # v2 W3.5-s4-step1 机制1:大纲角色完整性回填(plot 后、volume_plans 前)。
            # Mock 始终返 ``missing_characters=[]``(无悬空角色 → no-op),不破坏既有
            # outline 生成 happy 路径(回填 fail-open + 空列表 = characters 原样直通
            # volume_plans)。不依赖 chapter_id(对齐 outline_consistency_validator 同型)。
            content = '{"missing_characters": []}'
        else:
            # P5-B-03:OutlineBuilder 4 stages —— outline_world / outline_characters
            # / outline_plot / outline_volume_plans。每 stage 返合规固定 JSON
            # fixture(对齐 ``src/core/models/outline.py`` 4 个 ``*Output``
            # Pydantic schema),让单测 / Orchestrator 集成测可跑通 4 段串行。
            content = _generate_outline_stage_json(role, user_text)

        latency_ms = (time.perf_counter() - start) * 1000.0

        model_used = f"mock-{requested_model}"
        # 估算 token 用量(粗略:中英文字符 / 3 ≈ token);
        # 用于 OBSERVABILITY metrics 不为零即可,不要求精确。
        usage = TokenUsage(
            input_tokens=estimated_input_tokens,
            output_tokens=max(1, len(content) // 3),
        )

        # P1-B-24:OBSERVABILITY §2.4 `llm_response` 事件。``cost_usd=0.0``
        # —— mock 无成本;真 provider 定价表延后到 P3 真 LLM 联调时落地。
        _log.info(
            "llm_response",
            provider=_PROVIDER,
            model=model_used,
            agent=role,
            input_tokens=usage.input_tokens,
            output_tokens=usage.output_tokens,
            cost_usd=0.0,
            latency_ms=round(latency_ms, 3),
            finish_reason="end_turn",
            prompt_hash=prompt_hash,
        )

        return LLMResponse(
            content=content,
            model_used=model_used,
            usage=usage,
            finish_reason="end_turn",
        )


# ---- 辅助函数 ---------------------------------------------------------------


def _join_user_messages(messages: list[Message]) -> str:
    """拼接所有 user message 内容,供 marker / chapter_id 抓取使用。

    Mock 不关心 system / assistant 内容(marker 协议规定 user 末尾)。
    """
    return "\n\n".join(m.content for m in messages if m.role == "user")


def _detect_role_and_chapter_id(
    messages: list[Message],
) -> tuple[AgentRole, str | None]:
    """从 user messages 抓 agent 身份 marker + reviewer chapter_id。

    抓不到 → ``ValueError``。Mock 是测试基础设施,fail loud。

    P1-B-24 起复用 ``src.core.agents._prompt.detect_agent_role``(soft 失败,
    返回 ``None``),本函数在外层把 ``None`` 翻成 ``ValueError`` 维持原契约。
    P2-B-09 起 ``extractor`` 加入返回值合集(MockLLMClient 自身分支为
    ``NotImplementedError``,留 P2-B-12+ 真接入时实现);P5-B-03 起 4 个
    ``outline_*`` 子角色加入(OutlineBuilder 4-stage agent);P5-B-05 起
    ``chapter_card_generator`` 加入(ChapterCardGenerator agent);P7-B-06 起
    ``must_achieve_validator`` 加入(must_achieve 独立 LLM 判定 agent,N 次
    yes/no 调用,mock 始终返 yes 不影响既有 happy 路径);v1.0.x wave 3 Commit D
    起 ``continuity_validator`` 加入(衔接桥 §4.3 LLM 判定 agent,3 Q 条件 gated
    yes/no 调用,mock 始终返 yes 不影响既有 happy 路径)。

    :return: ``(role, chapter_id)`` 二元组。``chapter_id`` 仅 reviewer 分支
        非 ``None``;writer / reviser / extractor / outline_* /
        chapter_card_generator / must_achieve_validator / continuity_validator
        分支为 ``None``。
    """
    role, chapter_id = detect_agent_role(messages)
    if role is None:
        raise ValueError(
            "Mock 无法识别 agent 类型,user message 须含 "
            "<!-- agent_role: writer|reviewer|reviser|extractor"
            "|outline_world|outline_characters|outline_plot|outline_volume_plans"
            "|chapter_card_generator|must_achieve_validator|continuity_validator"
            "|mini_plan_generator|chapter_summary_extractor|beat_alignment_validator"
            "|must_vs_forbidden_validator"
            " --> marker(reviewer 还需带 chapter_id)。"
            "P1-B-22 / P2-B-09 / P5-B-03 / P5-B-05 / P7-B-06 / v1.0.x Commit D"
            " / wave 4 session 1+3a 各 agent 已统一在 _build_user_message 末尾追加。"
        )
    return role, chapter_id


def _normalize_chapter_id_for_env(chapter_id: str) -> str:
    """``ch-0001`` → ``CH_0001``(env 变量名:大写 + 连字符替连下划线)。"""
    return chapter_id.upper().replace("-", "_")


def _resolve_verdict(chapter_id: str) -> Verdict:
    """章节级 env > 全局 env > 默认 ``pass``。

    非法值抛 ``ValueError``。
    """
    chapter_env = (
        f"{_ENV_CHAPTER_VERDICT_PREFIX}{_normalize_chapter_id_for_env(chapter_id)}"
    )
    raw = os.environ.get(chapter_env) or os.environ.get(_ENV_GLOBAL_VERDICT) or "pass"
    raw_lower = raw.strip().lower()
    if raw_lower not in _ALLOWED_VERDICTS:
        raise ValueError(
            f"Mock verdict 取值非法: {raw!r}(来源 env "
            f"{chapter_env} / {_ENV_GLOBAL_VERDICT})。"
            f"允许值 {list(_ALLOWED_VERDICTS)}。"
        )
    return raw_lower  # type: ignore[return-value]


# ---- 内容生成 ---------------------------------------------------------------


def _extract_word_count_target(user_text: str) -> int:
    """尝试从 Writer user message 抓 word_count_max 作为正文目标字数。

    抓不到默认 3000(中位数);章节卡 P1 范围内字数区间约 [2800, 4500]。
    """
    match = re.search(r"word_count_max=(\d+)", user_text)
    if match is None:
        match = re.search(r'"word_count_max"\s*:\s*(\d+)', user_text)
    if match:
        # This client synthesizes placeholder prose, not a model-capacity probe.
        # Broad validator fixtures may specify 10**8 to mean "no upper gate";
        # do not allocate and repeatedly review a hundred-million-character stub.
        return min(int(match.group(1)), 10_000)
    return 3000


def _generate_writer_content(user_text: str) -> str:
    """合成 Writer 输出:含 chapter_id / 标题占位 + 重复段落凑字数。

    不真生成创意正文——脱机测试只需"形似一篇章"即可。字数 ~ word_count_max。
    """
    if "正文修复执行合同（LOCALIZED PATCH；代码机械合成）" in user_text:
        return _generate_localized_revision_json(user_text)

    chapter_id_match = _CHAPTER_ID_RE.search(user_text)
    chapter_id = chapter_id_match.group(0) if chapter_id_match else "ch-unknown"
    target = _extract_word_count_target(user_text)
    global_rewrite = "正文修复执行合同（代码已显式授权 GLOBAL）" in user_text

    title_match = re.search(r"-\s*标题:(.+)", user_text)
    title = title_match.group(1).strip() if title_match else "(mock 章节)"

    header = (
        f"# [{'mock-rewrite' if global_rewrite else 'mock'}] {title}\n\n"
        f"> Mock {'GLOBAL 重写' if global_rewrite else '写作'}输出 - "
        f"chapter_id={chapter_id} target≈{target} 字。\n\n"
    )
    body_lines: list[str] = []
    accumulated = len(header)
    while accumulated < target:
        para = _MOCK_PARAGRAPH
        # P4-B-06:严格不超 target,避免 [HARD] word_count validator 因 mock
        # 输出"略超"而触发 above_max。剩余空间不够整段时按字符截断。
        remaining = target - accumulated
        if len(para) + 2 > remaining:
            para = para[: max(0, remaining)]
            body_lines.append(para)
            accumulated += len(para)
            break
        body_lines.append(para)
        accumulated += len(para) + 2
    return header + "\n\n".join(body_lines)


def _generate_reviser_content(user_text: str) -> str:
    """合成 Reviser 输出:按动态合同返 patch，legacy 路径仍返整章。

    §2.124 LOCALIZED 使用与 Writer 相同的严格 JSON；未带合同的 P1 legacy
    caller 保留旧整章 fixture。
    """
    if "正文修复执行合同（LOCALIZED PATCH；代码机械合成）" in user_text:
        return _generate_localized_revision_json(user_text)

    chapter_id_match = _CHAPTER_ID_RE.search(user_text)
    chapter_id = chapter_id_match.group(0) if chapter_id_match else "ch-unknown"
    header = (
        f"# [mock-revised] chapter_id={chapter_id}\n\n"
        "> Mock 修订输出(已按审稿意见调整若干段落)。\n\n"
    )
    body = "\n\n".join([_MOCK_PARAGRAPH] * 12)
    return header + body


def _generate_localized_revision_json(user_text: str) -> str:
    """从动态合同生成一次合法、确定性的 mock 局部 patch 响应。"""

    try:
        identity_tail = user_text.split("### 冻结身份与允许范围", 1)[1]
        identity_json = identity_tail.split("```json\n", 1)[1].split(
            "\n```", 1
        )[0]
        identity = json.loads(identity_json)
        source_tail = user_text.split("### 带稳定锚点的源正文", 1)[1]
        source_json = source_tail.split("```json\n", 1)[1].split("\n```", 1)[0]
        source = json.loads(source_json)
    except (IndexError, TypeError, json.JSONDecodeError) as exc:
        raise ValueError("Mock localized revision 无法解析动态合同") from exc

    anchors = {item["anchor_id"]: item["text"] for item in source}
    slots: dict[tuple[str, str], list[dict[str, Any]]] = {}
    for target in identity["targets"]:
        anchor_id = target["allowed_anchor_ids"][0]
        operation = target["allowed_operations"][0]
        slots.setdefault((anchor_id, operation), []).append(target)

    patches: list[dict[str, Any]] = []
    for (anchor_id, operation), targets in slots.items():
        if operation == "replace":
            original = anchors[anchor_id]
            replacement = original
            for target in targets:
                instruction = str(target.get("instruction") or "")
                if instruction and instruction in replacement:
                    replacement = replacement.replace(instruction, "[mock 已移除]")
            if replacement == original:
                replacement += "（mock 局部修订已应用）"
        else:
            replacement = "[mock] 已在授权锚点补入必要承接。"
        patches.append(
            {
                "target_ids": [target["target_id"] for target in targets],
                "anchor_id": anchor_id,
                "operation": operation,
                "replacement": replacement,
            }
        )

    return json.dumps(
        {
            "contract_version": identity["contract_version"],
            "mode": "localized",
            "source_version_id": identity["source_version_id"],
            "source_sha256": identity["source_sha256"],
            "plan_hash": identity["plan_hash"],
            "patches": patches,
        },
        ensure_ascii=False,
    )


def _generate_extractor_json(user_text: str) -> str:
    """合成 MemoryExtractor JSON 输出(P2-B-19)。

    严格匹配 P2-B-09 ``_ExtractorLLMPayload`` schema(``characters`` /
    ``plot_threads`` / ``timeline`` / ``facts`` 四类必出现)+ 各类元素字段对齐
    `Extracted*` / `FactEntry`。

    设计目标:让 P2 acceptance 验收 #2/#3/#4 可通过——
    - **跨章共享 character_id / thread_id**:让 ``get-or-update-or-create``
      路径上"第二章抽取时 character/thread 已存在"分支被覆盖;Writer 在第
      二章 :write 时拼 voice 段能查到。
    - **timeline / facts 每章独立 ID**:让 retriever 在第 N+1 章检索能拿到
      第 1..N 章的累积 fact,验收 #3"检索结果包含上一章相关信息"。
    - ``time_order`` 从 chapter_id 数字尾段派生(``ch-0001`` → 100,
      ``ch-0002`` → 200)以保单调;非标准格式回退到 ``100``。

    Mock 不感知章节卡 / 上下文,只从 user_text 抓 chapter_id 派生 ID;真 LLM
    路径不走本函数。
    """
    chapter_id_match = _CHAPTER_ID_RE.search(user_text)
    chapter_id = chapter_id_match.group(0) if chapter_id_match else "ch-unknown"

    # 从 chapter_id 尾段派生 time_order:ch-0001 → 100, ch-0002 → 200
    suffix_match = re.search(r"ch-(\d+)", chapter_id)
    if suffix_match:
        try:
            time_order = int(suffix_match.group(1)) * 100
        except ValueError:
            time_order = 100
    else:
        time_order = 100

    text_after_heading = user_text.split("## 待抽取正文", 1)[-1]
    mock_body = text_after_heading.split("<!-- agent_role:", 1)[0].strip()
    mock_evidence = mock_body[:80].strip() if len(mock_body) >= 12 else None

    payload = {
        "characters": [
            {
                "character_id": "lin-yan",
                "name": "林衍",
                "archetype": "萌新剑修",
                "appearance": "白衣青年,腰间挂朴素长剑",
                "personality": "沉静内敛,关键时刻果决",
                "background": "出身寒门,刚入山门",
                "voice_profile": {
                    "tone": "冷峻",
                    "sentence_style": "短句直白",
                    "self_address": "我",
                    "addresses_others": {},
                    "catchphrases": [],
                    "forbidden_phrases": [],
                },
                "goals_long_term": ["问鼎剑道"],
                "goals_short_term": [f"完成 {chapter_id} 任务"],
                "aliases": [],
                "current_state": {"位置": "山门"},
                "relationships": {},
                "abilities": [],
                "secrets": [],
            }
        ],
        "plot_threads": [
            {
                "thread_id": "zhu-jue-cheng-zhang",
                "title": "主角成长",
                "status": "developing",
                "importance": 4,
                "planted_chapter": chapter_id,
                "description": (
                    f"正文明确留下主角成长所造成的跨章后果(更新于 {chapter_id})"
                ),
                "planned_payoff": None,
                "actual_payoff": None,
                "related_characters": ["lin-yan"],
                "evidence_quote": mock_evidence,
                "emergent_kind": "unresolved_consequence",
                "expiry_policy": "volume_end",
            }
        ],
        "timeline": [
            {
                "entry_id": f"mock-event-{chapter_id}",
                "description": f"[mock] {chapter_id} 章中林衍经历的事件",
                "time_label": "本章",
                "time_order": time_order,
                "involved_characters": ["lin-yan"],
            }
        ],
        "facts": [
            {
                "fact_id": f"mock-fact-{chapter_id}-1",
                "description": (
                    f"[mock fact] {chapter_id} 章中,林衍在山门前展露剑意,"
                    "感受到来自远方的考验。这条事实由 mock extractor 产生,"
                    "供后续章节 retriever 检索验证。"
                ),
                "involved_characters": ["lin-yan"],
            },
            {
                "fact_id": f"mock-fact-{chapter_id}-2",
                "description": (
                    f"[mock fact] {chapter_id} 章核心信息:剑柄发热,"
                    "脚下青石磨亮,伏笔延续到下一章。"
                ),
                "involved_characters": ["lin-yan"],
            },
        ],
    }
    return json.dumps(payload, ensure_ascii=False, indent=2)


def _generate_must_achieve_validator_json(user_text: str) -> str:
    """合成 must_achieve_validator JSON 输出(P7-B-06 引入,G9.3 改批处理 list)。

    严格匹配 ``src.core.agents.must_achieve_validator._ValidatorBatchPayload`` schema:
    ``{"results": [{"item_index": int, "verdict": "yes"/"no"/"uncertain", "evidence": str}, ...]}``。

    G9.3(v1.1 wave 2 session 6,2026-05-29):从 user_text 解析 ``## 必达事件列表``
    段,数有几条编号(``1. `` / ``2. `` / 等)生成对应数量的 results list,
    每条 verdict=yes。这样 mock 自适应任意 N 条 must_achieve 输入。

    解析失败兜底:返 1 条占位 result(item_index=1 verdict=yes),既有单测
    走 fake prompt 时不会撞这条(单测用真 schema fixture)。
    """
    # 从 user_text 解析 must_achieve 条数(数 "## 必达事件列表" 段下的 "1. " / "2. " 编号)
    n = _count_must_achieve_items(user_text)
    results = [
        {
            "item_index": idx + 1,
            "verdict": "yes",
            "evidence": f"[mock] 第 {idx + 1} 条必达事件已在正文中明确达成",
        }
        for idx in range(n)
    ]
    return json.dumps({"results": results}, ensure_ascii=False, indent=2)


def _count_must_achieve_items(user_text: str) -> int:
    """数 user_text 里 ``## 必达事件列表`` 段下的编号(``1. `` / ``2. `` / 等)。

    G9.3 mock 自适应:批处理输入含 N 条编号 must_achieve,mock 必须返对应数量
    的 list。解析失败兜底 → 1。
    """
    import re as _re

    # 截取 "## 必达事件列表" 到下一个 "## " 之间的段
    match = _re.search(
        r"## 必达事件列表\s*\n(.*?)(?=\n## |\Z)",
        user_text,
        flags=_re.DOTALL,
    )
    if match is None:
        return 1  # 兜底:无法解析 → 1 条
    section = match.group(1)
    # 数行首的 "N. " 编号(N 是正整数)
    numbers = _re.findall(r"^\s*(\d+)\.\s", section, flags=_re.MULTILINE)
    if not numbers:
        return 1  # 兜底:无编号 → 1 条
    return len(numbers)


def _generate_must_feasibility_validator_json(user_text: str) -> str:
    """合成 must_feasibility_validator JSON 输出(v2 间隙 A3①,2026-06-13)。

    严格匹配 ``src.core.agents.must_feasibility_validator._FeasibilityLLMPayload``
    schema:``{"items": [{"must": str, "verdict": "feasible"/"consumed"/"uncertain",
    "confidence": float, "evidence": str, "suggestion": str}, ...]}``。

    自适应条数:调用方以 ``len(payload.items) != len(must_achieve)`` 判 LLM 输出
    不可信 → fail-open,故 mock 必须按输入条数返回。从 user_text ``## 待判定``
    段抓行首编号条目(``_build_user_message`` 字面 ``{i}. {must}``),每条回显
    原文 + verdict=feasible(护 happy 路径:findings 空 → passed=True 不升人工)。
    解析失败兜底 → 1 条占位(对齐 ``_count_must_achieve_items`` 兜底惯例)。
    """
    import re as _re

    match = _re.search(
        r"## 待判定:[^\n]*\n(.*?)(?=\n请按系统提示词|\Z)",
        user_text,
        flags=_re.DOTALL,
    )
    musts: list[str] = []
    if match is not None:
        musts = [
            m.group(1).strip()
            for m in _re.finditer(
                r"^\s*\d+\.\s*(.+)$", match.group(1), flags=_re.MULTILINE
            )
        ]
    if not musts:
        musts = ["(mock 占位条目)"]
    items = [
        {
            "must": must,
            "verdict": "feasible",
            "confidence": 0.9,
            "evidence": "[mock] 前文未消费该事件,本章可自然达成",
            "suggestion": "",
        }
        for must in musts
    ]
    return json.dumps({"items": items}, ensure_ascii=False, indent=2)


def _generate_volume_seam_replay_validator_json() -> str:
    """合成 volume_seam_replay_validator JSON 输出(v2 间隙 A3①,2026-06-13)。

    严格匹配 ``src.core.agents.volume_seam_replay_validator._SeamLLMPayload``
    schema:``{"replay": bool, "confidence": float, "evidence": str}``。Mock 始终
    返 ``replay=false``(卷界无重演),不阻断既有 outline commit happy 路径。
    """
    return json.dumps(
        {
            "replay": False,
            "confidence": 0.9,
            "evidence": "[mock] 次卷开篇为新事件推进,与上卷结尾无重演",
        },
        ensure_ascii=False,
        indent=2,
    )


def _generate_beat_alignment_validator_json() -> str:
    """合成 beat_alignment_validator JSON 输出(v1.1 wave 1 session 1)。

    严格匹配 ``src.core.agents.beat_alignment_validator._ValidatorLLMPayload`` schema:
    ``verdict ∈ {"pass", "fail"}`` + list[str] / bool / str 字段 +(v1.1 wave 6
    session 2 / A2)``info_increment_substantive`` bool + ``info_increment_issue`` str。

    Mock 始终返 ``pass`` + ``info_increment_substantive=True`` + 占位字段,语义:
    "本章 event 符合 beat 范围 + 推进 ≥ 1 条 must_advance + 未触发 forbidden_advance
    + info_increment 实质"。既有 OutlineBuilder mini_plan 集成测多走 ``arc.beats=[]``
    跳过 validator(beats 空 → caller 不调本 agent);若 fixture 含 beat 字段且 caller
    接入 validator,默认 pass + substantive 保持 happy 路径(不误产 info_increment warn)。
    单测路径走专属判定(test_beat_alignment_validator.py 独立 LLM 调用 fixture)。
    """
    payload = {
        "verdict": "pass",
        "must_advance_advanced": ["[mock] 本章推进了 beat must_advance 中的第 1 条"],
        "must_advance_missing": [],
        "forbidden_advance_violated": [],
        "summary_in_range": True,
        "evidence": "[mock] 本章 event 字面符合 beat.summary 范围 + 未触发 forbidden",
        "info_increment_substantive": True,
        "info_increment_issue": "",
    }
    return json.dumps(payload, ensure_ascii=False, indent=2)


def _generate_beat_alignment_batch_validator_json(user_text: str) -> str:
    """按真实输入章节列表返回同序结果，供整卷核对离线测试使用。"""
    decoder = json.JSONDecoder()
    try:
        packet = json.loads(user_text)
        tasks = packet["data"]["tasks"]
    except (ValueError, KeyError, TypeError):
        start = user_text.find("{", user_text.index("[beat_alignment_batch]"))
        data, _ = decoder.raw_decode(user_text[start:])
        tasks = data["tasks"]
    return json.dumps({"results": [
        {
            "chapter_id": task["chapter_id"],
            "beat_id": task["beat_id"],
            "verdict": "pass",
            "must_advance_advanced": task["beat"]["must_advance"][:1],
            "must_advance_missing": [],
            "forbidden_advance_violated": [],
            "summary_in_range": True,
            "evidence": "[mock] 本章设计与归属节拍相符",
            "chapter_evidence_quote": task["one_line_event"][:8],
            "beat_evidence_quote": task["beat"]["summary"][:8],
            "info_increment_substantive": True,
            "info_increment_issue": "",
        }
        for task in tasks
    ]}, ensure_ascii=False)


def _generate_mechanism_consistency_validator_json() -> str:
    """合成 mechanism_consistency_validator JSON 输出(v1.1 wave 6 session 1 / A1)。

    严格匹配 ``src.core.agents.mechanism_consistency_validator._MechanismLLMPayload``
    schema:``contradictions`` list[str] + ``vague_mechanisms`` list[str] + ``evidence``。

    Mock 始终返空 contradictions + 空 vague_mechanisms,语义:"world 机制设定自洽 +
    高概念词均配可执行规则"。既有 outline 生成 happy 路径(build_world post_validate
    接入本 validator)默认 passed=True 不误触 HARD retry;单测路径走专属判定
    (test_mechanism_consistency_validator.py 独立 LLM 调用 fixture)。
    """
    payload = {
        "contradictions": [],
        "vague_mechanisms": [],
        "evidence": "[mock] world 机制设定自洽,高概念词均配可执行规则",
    }
    return json.dumps(payload, ensure_ascii=False, indent=2)


def _generate_outline_consistency_validator_json() -> str:
    """合成 outline_consistency_validator JSON 输出(v2 W3.5-S3,大纲跨段一致性)。

    严格匹配 ``src.core.agents.outline_consistency_validator._OutlineConsistencyLLMPayload``
    schema:``contradictions`` + ``coverage_gaps`` + ``evidence`` + ``repair_scope``。

    Mock 始终返空 contradictions + 空 coverage_gaps,语义:"4 段跨段一致,卷规划覆盖
    主线"。既有 outline 生成 happy 路径(build_outline volume_plans 后接入本 validator)
    默认 passed=True 不误触 HARD 阻断;单测路径走专属判定(test_outline_consistency_
    validator.py 独立 LLM 调用 fixture)。
    """
    payload = {
        "contradictions": [],
        "coverage_gaps": [],
        "evidence": "[mock] 大纲 4 段跨段一致,卷规划覆盖主线骨架",
        "repair_scope": "none",
    }
    return json.dumps(payload, ensure_ascii=False, indent=2)


def _generate_outline_split_consistency_validator_json(user_text: str) -> str | None:
    """Mock the five-stage contract only when the actual JSON handoff asks for it."""
    try:
        packet, _ = json.JSONDecoder().raw_decode(user_text.lstrip())
        data = packet["data"]
        stage = data["review_stage"]
        source_hash = data["source_hash"]
    except (KeyError, TypeError, ValueError):
        return None
    if stage not in {"world", "characters", "plot", "volume_plans", "cross"}:
        return None
    payload = {
        "review_stage": stage,
        "source_hash": source_hash,
        "outcome": "compatible",
        "reviewed_sections": (["world", "characters", "plot", "volume_plans"]
                              if stage == "cross" else [stage]),
        "contradictions": [],
        "coverage_gaps": [],
        "requested_paths": [],
        "evidence": "[mock] 本项事实可承接",
        "repair_scope": "none",
    }
    return json.dumps(payload, ensure_ascii=False)


def _generate_thread_closure_validator_json(user_text: str) -> str:
    """合成 thread_closure_validator 批处理 JSON 输出(v1.1 wave 6 session 1 / A4)。

    严格匹配 ``src.core.agents.thread_closure_validator._ThreadClosureBatchPayload``
    schema:``{"results": [{"index": int, "verdict": "closed"/..., "evidence": str}, ...]}``。

    沿用 must_achieve_validator G9.3 批处理 mock 自适应:从 user_text 解析
    ``## 候选伏笔列表`` 段编号(``1. `` / ``2. `` / 等)数有几条候选,生成对应数量的
    results,每条 verdict=closed。closed 语义:"本伏笔在结局区已收口"。A4 是 WARN-only
    不 raise,closed 还避免既有 mini_plan happy 路径误产 dangling warn log;单测路径走
    专属判定(test_thread_closure_validator.py 独立 LLM 调用 fixture)。
    """
    n = _count_thread_closure_candidates(user_text)
    results = [
        {
            "index": idx + 1,
            "verdict": "closed",
            "evidence": f"[mock] 第 {idx + 1} 条伏笔在结局区已明确收口",
        }
        for idx in range(n)
    ]
    return json.dumps({"results": results}, ensure_ascii=False, indent=2)


def _count_thread_closure_candidates(user_text: str) -> int:
    """数 user_text 里 ``## 候选伏笔列表`` 段下行首的编号(``1. `` / ``2. `` / 等)。

    批处理 mock 自适应:输入含 N 条编号候选 → mock 返对应数量 results。解析失败兜底 → 1。
    """
    import re as _re

    match = _re.search(
        r"## 候选伏笔列表[^\n]*\n(.*?)(?=\n## |\Z)",
        user_text,
        flags=_re.DOTALL,
    )
    if match is None:
        return 1
    section = match.group(1)
    numbers = _re.findall(r"^\s*(\d+)\.\s", section, flags=_re.MULTILINE)
    if not numbers:
        return 1
    return len(numbers)


def _generate_identity_coverage_validator_json(user_text: str) -> str:
    """合成 identity_coverage_validator 批处理 JSON 输出(v1.1 wave 6 session 2 / A3)。

    严格匹配 ``src.core.agents.identity_coverage_validator._IdentityCoverageBatchPayload``
    schema:``{"results": [{"index": int, "uncovered_contexts": [...], "evidence": str}, ...]}``。

    沿用 thread_closure_validator G9.3 批处理 mock 自适应:从 user_text 解析
    ``## 候选角色`` 段编号数有几个候选,生成对应数量的 results,每条
    ``uncovered_contexts=[]``(全覆盖)。A3 是 WARN-only 不 raise,空 uncovered 还避免
    既有 mini_plan happy 路径误产 gaps warn log;单测路径走专属判定
    (test_identity_coverage_validator.py 独立 LLM 调用 fixture)。
    """
    n = _count_identity_coverage_candidates(user_text)
    results = [
        {
            "index": idx + 1,
            "uncovered_contexts": [],
            "evidence": f"[mock] 第 {idx + 1} 个角色各身份侧面全书均有交代",
        }
        for idx in range(n)
    ]
    return json.dumps({"results": results}, ensure_ascii=False, indent=2)


def _count_identity_coverage_candidates(user_text: str) -> int:
    """数 user_text 里 ``## 候选角色`` 段下行首的编号(``1. `` / ``2. `` / 等)。

    批处理 mock 自适应:输入含 N 个编号候选 → mock 返对应数量 results。解析失败兜底 → 1。
    """
    import re as _re

    match = _re.search(
        r"## 候选角色[^\n]*\n(.*?)(?=\n## |\Z)",
        user_text,
        flags=_re.DOTALL,
    )
    if match is None:
        return 1
    section = match.group(1)
    numbers = _re.findall(r"^\s*(\d+)\.\s", section, flags=_re.MULTILINE)
    if not numbers:
        return 1
    return len(numbers)


def _generate_must_vs_forbidden_validator_json() -> str:
    """合成 must_vs_forbidden_validator JSON 输出(v1.1 audit G20,2026-05-27)。

    严格匹配 ``src.core.agents.must_vs_forbidden_validator._ValidatorLLMPayload`` schema:
    三档 ``verdict`` + 兼容名 ``conflicts`` findings + ``evidence``。

    Mock 始终返 ``no_conflict`` + 空 conflicts,语义:"必达 vs 禁止无互斥矛盾"。
    既有 chapter_card_generator 测试 fixture 走 happy 路径(default no_conflict
    不破坏既有 chapter_card 生成);单测路径走专属判定
    (test_must_vs_forbidden_validator.py 独立 LLM 调用 fixture)。
    """
    payload = {
        "verdict": "no_conflict",
        "conflicts": [],
        "evidence": "[mock] must_achieve vs forbidden 无互斥矛盾",
    }
    return json.dumps(payload, ensure_ascii=False, indent=2)


def _generate_continuity_validator_json() -> str:
    """合成 continuity_validator JSON 输出(v1.0.x wave 3 Commit D)。

    严格匹配 ``src.core.agents.continuity_validator._ValidatorLLMPayload`` schema:
    ``verdict ∈ {"yes", "no", "uncertain"}`` + ``evidence`` 字符串。

    Mock 始终返 ``yes`` + 占位 evidence,语义:"本章开头已承接 / 同空间 / 时间
    一致"。3 Q 共享同 mock 输出,既有 orchestrator 测试 fixture 多走 chapter_number=1
    或空 continuity_context 跳过 validator;若 fixture 进入 LLM 调用,默认 yes
    保持 happy 路径(continuity_context 非空 fixture 走 reviewer 自然 verdict,
    validator 不该误判让 happy 测变 rewrite)。
    """
    payload = {
        "verdict": "yes",
        "evidence": "[mock] 本章开头已承接上一章衔接桥语义",
    }
    return json.dumps(payload, ensure_ascii=False, indent=2)


def _generate_setting_consistency_validator_json() -> str:
    """合成 setting_consistency_validator JSON 输出(v1.1 wave 6 session 3,B1)。

    严格匹配 ``src.core.agents.setting_consistency_validator._SettingLLMPayload``
    schema:``violations`` 数组(空 = 本章无设定违反)。

    Mock 始终返 ``violations=[]``,既有审稿 happy 路径不回归(WARN-only 本不覆盖
    verdict,空 violations 还避免误产 warn log)。单测路径走专属 fixture 注入违反。
    """
    payload: dict[str, list[object]] = {"violations": []}
    return json.dumps(payload, ensure_ascii=False, indent=2)


def _generate_forbidden_crossing_validator_json() -> str:
    """合成 forbidden_crossing_validator JSON 输出(v2 W3.5 补强3,2026-06-26)。

    严格匹配 ``src.core.agents.forbidden_crossing_validator._ForbiddenLLMPayload``
    schema:``violations`` 数组(空 = 本章无越界,最常见合格输出)。

    Mock 始终返 ``violations=[]``,既有审稿 happy 路径不回归(HARD 闸但空 violations
    → passed=True 不覆盖 verdict)。单测路径走专属 fixture 注入越界。
    """
    payload: dict[str, list[object]] = {"violations": []}
    return json.dumps(payload, ensure_ascii=False, indent=2)


def _generate_scene_anchor_validator_json() -> str:
    """合成 scene_anchor_validator JSON 输出(v1.1 wave 6 session 3,B2)。

    严格匹配 ``src.core.agents.scene_anchor_validator._SceneAnchorLLMPayload``
    schema:``verdict ∈ {"natural", "teleport", "uncertain"}`` + ``evidence``。

    Mock 始终返 ``natural``(醒来点与入梦点不一致但属自然过渡 / 有交代),既有
    extract happy 路径不回归(WARN-only,natural 还避免误产 warn log)。单测路径
    走专属 fixture 注入 teleport。
    """
    payload = {
        "verdict": "natural",
        "evidence": "[mock] 场景转换属自然过渡 / 有位移交代",
    }
    return json.dumps(payload, ensure_ascii=False, indent=2)


def _generate_cross_chapter_dedup_validator_json(user_text: str) -> str:
    """合成 cross_chapter_dedup_validator 批处理 JSON 输出(v1.1 wave 6 session 4 / B4)。

    严格匹配 ``src.core.agents.cross_chapter_dedup_validator._DedupBatchPayload``
    schema:``{"results": [{"index": int, "verdict": "intentional"/..., "reason": str}, ...]}``。

    沿用 thread_closure_validator G9.3 批处理 mock 自适应:从 user_text 解析
    ``## 候选重复片段列表`` 段编号数有几条候选,生成对应数量的 results,每条
    verdict=intentional(默认全判有意回扣,护第一原则)。B4 是 WARN-only 不覆盖 verdict,
    全 intentional 还避免既有审稿 happy 路径误产 violation warn log;单测路径走专属
    判定(test_cross_chapter_dedup_validator.py 独立 LLM 调用 fixture)。
    """
    n = _count_cross_chapter_dedup_candidates(user_text)
    results = [
        {
            "index": idx + 1,
            "verdict": "intentional",
            "reason": f"[mock] 第 {idx + 1} 条重复属有意回扣 / 母题呼应",
        }
        for idx in range(n)
    ]
    return json.dumps({"results": results}, ensure_ascii=False, indent=2)


def _count_cross_chapter_dedup_candidates(user_text: str) -> int:
    """数 user_text 里 ``## 候选重复片段列表`` 段下行首的编号(``1. `` / ``2. `` / 等)。

    批处理 mock 自适应:输入含 N 条编号候选 → mock 返对应数量 results。解析失败兜底 → 1。
    """
    import re as _re

    match = _re.search(
        r"## 候选重复片段列表[^\n]*\n(.*?)(?=\n## |\Z)",
        user_text,
        flags=_re.DOTALL,
    )
    if match is None:
        return 1
    section = match.group(1)
    numbers = _re.findall(r"^\s*(\d+)\.\s", section, flags=_re.MULTILINE)
    if not numbers:
        return 1
    return len(numbers)


def _generate_ending_tension_validator_json() -> str:
    """合成 ending_tension_validator JSON 输出(v1.1 wave 6 session 4 / B6)。

    严格匹配 ``src.core.agents.ending_tension_validator._EndingTensionPayload``
    schema:``verdict ∈ {"earned", "abrupt"}`` + ``evidence``。

    Mock 始终返 ``earned``(结局自然收束),既有审稿 happy 路径不回归(WARN-only,
    earned 还避免误产 warn log)。单测路径走专属 fixture 注入 abrupt。
    """
    payload = {
        "verdict": "earned",
        "evidence": "[mock] 结局张力自然收束,有铺垫",
    }
    return json.dumps(payload, ensure_ascii=False, indent=2)


def _generate_reviewer_json(chapter_id: str, verdict: Verdict) -> str:
    """合成 Reviewer JSON 输出。

    严格匹配 P7-B-06 后的 ``_ReviewerLLMPayload`` schema(LLM 该产的字段:
    6 个分项分 + issues + suggestions,**已删 hard_constraints_passed /
    failed_constraints**);``score_overall`` / ``verdict`` 由代码确定性算/判,
    Mock 通过分项分的取值控制最终 verdict。

    Verdict → 分项分映射(配合 ``_decide_verdict`` 默认阈值
    ``review_threshold=7.5`` / ``revise_threshold=6.5``):

    - ``pass``: 分项分全 9.0,综合分 ~9.0 ≥ review_threshold → verdict=pass
    - ``revise``: 分项分全 7.0,综合分 ~7.0 落在 [revise, review) → verdict=revise
    - ``rewrite``: 分项分全 5.0,综合分 ~5.0 < revise_threshold → verdict=rewrite

    P7-B-06 起 rewrite 不再走"硬约束失败"路径(reviewer 不再判此项);仅靠
    score < revise_threshold。Orchestrator 的 ``must_achieve_validator`` +
    ``validator_pipeline`` 综合后可独立把高分章节覆盖为 rewrite。
    """
    if verdict == "pass":
        score = 9.0
        issues: list[dict[str, str]] = []
        suggestions = ["[mock] 各项指标稳健,可继续。"]
    elif verdict == "revise":
        score = 7.0
        issues = [
            {
                "paragraph_anchor": f"Mock 写作输出 - chapter_id={chapter_id}",
                "issue_type": "pacing",
                "description": "[mock] 开篇节奏偏慢,信息密度不足。",
                "suggestion": "[mock] 压缩前两段,把人物动机前移。",
            }
        ]
        suggestions = ["[mock] 调整开篇节奏后再审。"]
    else:  # rewrite
        score = 5.0
        issues = []
        suggestions = ["[mock] 综合分过低,建议整章重写。"]

    payload = {
        "score_plot_progression": score,
        "score_tension_match": score,
        "score_prose_quality": score,
        "score_consistency": score,
        "score_hook_management": score,
        "score_voice_consistency": score,
        # v1.1 wave 4 s5 / I2.2:模板符合度分。mock 始终随 verdict 同分输出;
        # reviewer 仅在工作区选了模板时进 7 维加权(无模板路径忽略该字段 →
        # 既有无模板 reviewer 测断言 score_overall 不变,零回归)。
        "score_template_conformance": score,
        # M2(v1.1 wave 5 s2 / G28):画面性分。mock 随 verdict 同分输出 —— 画面性
        # 维度对任何题材都评(reviewer prompt"任何情况都要输出"),故 mock 始终给,
        # 模拟真 LLM 行为。不进 score_overall → 无模板 reviewer 测断言零回归。
        "score_imagery": score,
        # M2(v1.1 wave 5 s3):内心戏分。同 imagery —— 任何题材都评,mock 随
        # verdict 同分输出;不进 score_overall → 无模板 reviewer 测断言零回归。
        "score_interiority": score,
        # M2(v1.1 wave 5 s9):套路化(**反极性**,越高越套路=越差)+ 沉浸感分。
        # 沉浸感随 verdict 同分(越高越好,同 imagery);套路化用 10-score(好 verdict
        # → 低套路,语义一致)。均不进 score_overall → 无模板 reviewer 测零回归。
        "score_formulaic": round(10.0 - score, 1),
        "score_immersion": score,
        "issues": issues,
        "suggestions": suggestions,
    }
    # 不包代码块 fence(P1-B-11 解析器能兜两种,但 Mock 走"干净 JSON"路径,
    # 保留代码块测试的覆盖给 reviewer 单测)。
    return json.dumps(payload, ensure_ascii=False, indent=2)


def _generate_style_auditor_json() -> str:
    """合成 StyleAuditor JSON 输出(v1.1 wave 4 s5 / I2.2)。

    严格匹配 ``_StyleAuditorLLMPayload`` schema(``drift_score`` +
    ``dimension_notes`` + ``summary``);``severity`` 由 agent 按 drift_score
    阈值派生,mock 不产。固定返低 drift(2.0 → ``ok``),不破坏 happy 路径。
    """
    payload = {
        "drift_score": 2.0,
        "dimension_notes": [
            "[mock] ③ 修辞密度:契合模板期望",
            "[mock] ④ 对白比例:基本符合",
            "[mock] ① 调性:整体契合",
        ],
        "summary": "[mock] 整体契合所选写作风格模板,无显著偏移。",
    }
    return json.dumps(payload, ensure_ascii=False, indent=2)


# ---- P5-B-03:OutlineBuilder 4 stages 固定 JSON fixture ---------------------


_TOTAL_VOLUMES_RE = re.compile(r"total_volumes(?:[^0-9]+)(\d+)")
"""volume_plans 段从 user_text 抓 ``total_volumes``,决定 ``volumes`` 数组长度。"""

_CHAPTERS_PER_VOLUME_RE = re.compile(r"chapters_per_volume(?:[^0-9]+)(\d+)")
"""volume_plans 段从 user_text 抓 ``chapters_per_volume``,决定每卷 ``chapter_count``。"""

_TENSION_TEMPLATE_RE = re.compile(
    r"tension_template(?:[^a-z_]+)(heros_journey|steady_daily|strong_stimulation|free)"
)
"""volume_plans 段从 user_text 抓张力模板;命中失败兜底 ``heros_journey``。"""

_VOLUME_PLAN_CHARACTER_RE = re.compile(
    r"^\s*-\s+([\w-]+)\((protagonist|deuteragonist|antagonist|supporting)\)\s*$",
    re.MULTILINE,
)
_VOLUME_PLAN_REQUIRES_DRAMA = "本题材每卷必须给出唯一 dramatic_promise"


def _generate_outline_stage_json(
    role: Literal[
        "outline_world",
        "outline_characters",
        "outline_plot",
        "outline_volume_plans",
    ],
    user_text: str,
) -> str:
    """OutlineBuilder 4 stages 固定 JSON fixture(P5-B-03)。

    每段固定 JSON 对齐 ``src/core/models/outline.py`` 4 个 ``*Output`` Pydantic
    schema。``outline_volume_plans`` 段从 ``user_text`` 抓 ``total_volumes`` /
    ``chapters_per_volume`` / ``tension_template`` 动态裁剪 fixture,让单测可
    用不同入参验证 schema 严格性 + agent 出参校验路径。
    """
    if role == "outline_world":
        return json.dumps(_OUTLINE_WORLD_FIXTURE, ensure_ascii=False, indent=2)
    if role == "outline_characters":
        return json.dumps(_OUTLINE_CHARACTERS_FIXTURE, ensure_ascii=False, indent=2)
    if role == "outline_plot":
        return json.dumps(_OUTLINE_PLOT_FIXTURE, ensure_ascii=False, indent=2)
    # role == "outline_volume_plans"
    return _generate_outline_volume_plans_json(user_text)


_OUTLINE_WORLD_FIXTURE: dict[str, str] = {
    "setting": (
        "[mock] 架空大陆九州,分九大州,正值修真盛世末年。山门林立、"
        "灵气衰退,人间与魔域的界限正在崩塌。"
    ),
    "power_system": (
        "[mock] 修炼分九境(炼气、筑基、金丹、元婴、化神、合体、大乘、渡劫、真仙),"
        "每三境一道天劫。灵根分五属(金木水火土),罕见者得双灵根或异灵根。"
    ),
    "factions": (
        "[mock] 九大正道宗门(青云、太一、紫府、玉清等),三大魔域(血煞、幽冥、"
        "天魔),以及散修联盟与朝廷修真司。正魔之间维持脆弱平衡两百年。"
    ),
    "culture": (
        "[mock] 修真者地位高于俗世,但仍需依赖凡间灵石矿脉。各宗门门规森严,"
        "弟子等级森严分内门外门。"
    ),
    "unique_rules": (
        "[mock] 渡劫失败必死;契约一立不可背叛(灵魂烙印);魔域之物不可入"
        "宗门(查出即逐出)。"
    ),
}


_OUTLINE_CHARACTERS_FIXTURE: dict[str, list[dict[str, Any]]] = {
    "characters": [
        {
            "character_id": "lin-yan",
            "name": "林衍",
            "role": "protagonist",
            "archetype": "废柴重生剑修",
            "appearance": "[mock] 白衣青年,腰间一柄朴素长剑,目光沉静",
            "personality": "[mock] 沉静内敛,关键时刻果决;前世孤傲,今生学会忍耐",
            "background": "[mock] 末法时代废柴炼丹师,在与魔头同归于尽时重生回到入山门那年",
            "motivation": "[mock] 改变末法时代,问鼎修真巅峰,守护今生所爱",
            "voice_hint": "[mock] 短句直白,冷峻",
            "relationships_brief": {
                "shi-jie": "前世师姐,今生重逢",
                "wei-xiao": "前世挚友",
            },
        },
        {
            "character_id": "shi-jie",
            "name": "沈雪",
            "role": "deuteragonist",
            "archetype": "天才师姐",
            "appearance": "[mock] 雪白长裙,身披素纱,腰悬玉佩",
            "personality": "[mock] 温柔坚毅,外冷内热;对林衍若即若离",
            "background": "[mock] 青云门首席弟子,与林衍前世有玉佩之约",
            "motivation": "[mock] 探查师傅遇害真相,庇护林衍成长",
            "voice_hint": "[mock] 中长句优雅,带轻叹气声",
            "relationships_brief": {"lin-yan": "师弟,前世玉佩之约对象"},
        },
        {
            "character_id": "wei-xiao",
            "name": "魏萧",
            "role": "deuteragonist",
            "archetype": "纨绔大少",
            "appearance": "[mock] 玄衣华服,腰挂金扇",
            "personality": "[mock] 看似纨绔实则深沉,世家公子的伪装",
            "background": "[mock] 太一门嫡传弟子,与林衍前世结义",
            "motivation": "[mock] 调查家族阴谋,寻找真相",
            "voice_hint": "[mock] 慵懒拉长音,夹带嘲讽",
            "relationships_brief": {"lin-yan": "前世义兄"},
        },
        {
            "character_id": "mo-zun",
            "name": "墨尊",
            "role": "antagonist",
            "archetype": "魔域大尊",
            "appearance": "[mock] 黑袍蒙面,声如金石",
            "personality": "[mock] 阴狠多谋,布局百年",
            "background": "[mock] 血煞域之主,前世杀死林衍的元凶",
            "motivation": "[mock] 打破封印让魔域吞噬人间,夺取真仙之位",
            "voice_hint": "[mock] 低沉冷峻,字字阴森",
            "relationships_brief": {"lin-yan": "前世杀手,今世死敌"},
        },
        {
            "character_id": "zhu-zhang-lao",
            "name": "朱长老",
            "role": "supporting",
            "archetype": "授业恩师",
            "appearance": "[mock] 白须长袍,慈眉善目",
            "personality": "[mock] 严慈相济,洞察世事",
            "background": "[mock] 青云门授业长老,林衍重生后的恩师",
            "motivation": "[mock] 培养下一代弟子守护九州",
            "voice_hint": "[mock] 中长句温和,带训导意味",
            "relationships_brief": {"lin-yan": "授业恩师"},
        },
    ]
}

for _outline_character_fixture in _OUTLINE_CHARACTERS_FIXTURE["characters"]:
    # 新生角色必须显式裁决正文前核心状态；mock 默认无承重基线。
    _outline_character_fixture["initial_core_facts"] = []


_OUTLINE_PLOT_FIXTURE: dict[str, Any] = {
    "core_conflict": (
        "[mock] 重生废柴炼丹师林衍 vs 魔域大尊墨尊及其布局百年的魔域吞噬阴谋;"
        "贯穿全书的'人界 vs 魔域 + 真相揭露 + 真仙之位归属'三重叠加冲突。"
    ),
    "main_phases": [
        {
            "phase_id": "phase-1-awakening",
            "title": "[mock] 觉醒",
            "summary": "[mock] 林衍重生入门,凭借前世记忆迅速成长,初遇师姐与义兄",
        },
        {
            "phase_id": "phase-2-trial",
            "title": "[mock] 试炼",
            "summary": "[mock] 内门弟子选拔大试 + 第一次入秘境,初尝魔域阴影",
        },
        {
            "phase_id": "phase-3-revelation",
            "title": "[mock] 真相",
            "summary": "[mock] 师傅遇害真相揭露,魔域势力开始浮出水面",
        },
        {
            "phase_id": "phase-4-war",
            "title": "[mock] 大战",
            "summary": "[mock] 正魔大战爆发,林衍渡劫为合体期",
        },
        {
            "phase_id": "phase-5-ascension",
            "title": "[mock] 飞升",
            "summary": "[mock] 终极对决 + 真仙之位归属揭晓",
        },
    ],
    "major_turning_points": [
        {
            "turning_id": "turn-1-shifu-death",
            "title": "[mock] 师傅之死",
            "description": "[mock] 朱长老遇害,林衍发现遗物中线索",
        },
        {
            "turning_id": "turn-2-mozun-reveal",
            "title": "[mock] 墨尊现身",
            "description": "[mock] 墨尊本尊出现,正魔平衡彻底破裂",
        },
        {
            "turning_id": "turn-3-jiemei-betrayal",
            "title": "[mock] 师姐立场",
            "description": "[mock] 沈雪被指控为魔域奸细,林衍信任受冲击",
        },
        {
            "turning_id": "turn-4-final-duel",
            "title": "[mock] 终极决战",
            "description": "[mock] 林衍 vs 墨尊,关乎九州存亡",
        },
    ],
    "long_threads": [
        {
            "thread_id": "yu-pei-zhi-yue",
            "title": "[mock] 玉佩之约",
            "description": "[mock] 沈雪玉佩刻有云纹,藏着上一世约定与今生身份秘密;埋于卷 1,收于卷 5",
        },
        {
            "thread_id": "shi-fu-fu-bi",
            "title": "[mock] 师傅遗物",
            "description": "[mock] 朱长老遗物中藏有魔域名册;埋于卷 2,收于卷 4",
        },
        {
            "thread_id": "zhen-xian-zhi-wei",
            "title": "[mock] 真仙之位",
            "description": "[mock] 九州唯一真仙之位归属,涉及天道选择;贯穿全书",
        },
        {
            "thread_id": "wei-xiao-jia-zu",
            "title": "[mock] 魏家阴谋",
            "description": "[mock] 魏萧家族与魔域秘密交易;埋于卷 2,收于卷 5",
        },
    ],
    "character_arcs": {
        "lin-yan": (
            "[mock] 从重生执念报仇 → 学会信任与放下 → 选择守护九州而非追求个人"
            "巅峰的成长"
        ),
        "mo-zun": ("[mock] 从隐忍布局 → 公开露面 → 终极对决暴露真实动机的反派弧光"),
        "shi-jie": "[mock] 从守护者到并肩战友,逐步揭开身世秘密",
        "wei-xiao": "[mock] 从纨绔伪装到家族叛逆者,在正魔间艰难抉择",
    },
}


def _generate_outline_volume_plans_json(user_text: str) -> str:
    """volume_plans 段固定 JSON fixture,按 user_text 动态裁剪。

    从 user_text 抓 ``total_volumes`` / ``chapters_per_volume`` / ``tension_template``:
    - ``volumes`` 数组长度 == ``total_volumes``
    - 每个 volume 的 ``chapter_count`` == ``chapters_per_volume``
    - ``tension_template`` 直接透传
    - 普通产品 ``arcs`` 在 4—5 章 mock 场景构造 2 个、6 章以上构造 3 个，
      确保每个 arc 都能容纳 2—4 个不重叠 dramatic beats；精确
      ``selection_short_arc=true`` 仍构造 3 个单章 arc、每 arc 恰好 1 beat。
      **首 arc start=1,末 arc end=chapter_count,arc 连续不重叠**
      (对齐 ``VolumePlanItem._validate_arcs_cover_chapter_count``)。
    - ``tension_curve`` 长度 == ``chapter_count``,值线性 3 → 9 → 6 模拟英雄之旅。
    """
    total_volumes = _extract_int(user_text, _TOTAL_VOLUMES_RE, default=3)
    chapters_per_volume = _extract_int(user_text, _CHAPTERS_PER_VOLUME_RE, default=15)
    tension_template = _extract_tension_template(user_text)
    phase_ids = _extract_contract_id_list(
        user_text,
        "linked_phases",
        default=["phase-1-awakening"],
    )
    turning_ids = _extract_contract_id_list(
        user_text,
        "linked_turning_points",
        default=["turn-1-shifu-death"],
    )
    thread_ids = _extract_contract_id_list(
        user_text,
        "linked_threads",
        default=["yu-pei-zhi-yue"],
    )
    character_roles = _VOLUME_PLAN_CHARACTER_RE.findall(user_text)
    actor_id = next(
        (
            character_id
            for character_id, role in character_roles
            if role == "protagonist"
        ),
        character_roles[0][0] if character_roles else "lin-yan",
    )
    counterforce_id = next(
        (
            character_id
            for character_id, role in character_roles
            if role == "antagonist" and character_id != actor_id
        ),
        next(
            (
                character_id
                for character_id, _role in character_roles
                if character_id != actor_id
            ),
            "mo-zun",
        ),
    )
    requires_drama = _VOLUME_PLAN_REQUIRES_DRAMA in user_text
    selection_short_arc = "selection_short_arc=true" in user_text

    volumes: list[dict[str, Any]] = []
    for vol_idx in range(1, total_volumes + 1):
        vol_id = f"v{vol_idx}"
        arcs = _build_volume_arcs(
            vol_id=vol_id,
            chapter_count=chapters_per_volume,
            selection_short_arc=selection_short_arc,
        )
        tension_curve = _build_curve(chapters_per_volume)
        linked_turnings = _pick_contract_ids(turning_ids, vol_idx)
        linked_threads = _pick_contract_ids(thread_ids, vol_idx)
        volume_payload: dict[str, Any] = {
            "volume_id": vol_id,
            "title": f"[mock] 卷{vol_idx}",
            "summary": f"[mock] 第 {vol_idx} 卷在主线中扮演阶段位",
            "chapter_count": chapters_per_volume,
            "tension_template": tension_template,
            "linked_phases": _pick_contract_ids(phase_ids, vol_idx),
            "linked_turning_points": linked_turnings,
            "linked_threads": linked_threads,
            "arcs": arcs,
            "tension_curve": tension_curve,
        }
        if requires_drama:
            primary_thread_id = linked_threads[0] if linked_threads else None
            primary_turning_id = linked_turnings[0] if linked_turnings else None
            volume_payload["dramatic_promise"] = {
                "primary_thread_id": primary_thread_id,
                "primary_turning_id": primary_turning_id,
                "actor_id": actor_id,
                "counterforce_id": counterforce_id,
                "outcome": "resolved",
                "deferred_to_volume_id": None,
                "deferred_to_thread_id": None,
            }
            arcs[-1]["beats"][-1]["dramatic_contract"] = {
                "function": "finale",
                "actor_id": actor_id,
                "counterforce_id": counterforce_id,
                "interaction_mode": "indirect",
                "thread_id": primary_thread_id,
                "turning_id": primary_turning_id,
                "actor_goal": f"{actor_id}公开本卷核心事实并终止控制方案",
                "counteraction": (
                    f"{counterforce_id}隔空冻结公开权限并切断见证路径"
                ),
                "choice": f"{actor_id}放弃独占证据并邀请同伴共同见证",
                "consequence": (
                    f"{counterforce_id}失去控制筹码，核心事实进入公开记录"
                ),
            }
        volumes.append(volume_payload)

    return json.dumps({"volumes": volumes}, ensure_ascii=False, indent=2)


def _extract_int(text: str, pattern: re.Pattern[str], *, default: int) -> int:
    match = pattern.search(text)
    if match is None:
        return default
    try:
        return int(match.group(1))
    except (ValueError, IndexError):
        return default


def _extract_tension_template(text: str) -> str:
    match = _TENSION_TEMPLATE_RE.search(text)
    if match is None:
        return "heros_journey"
    return match.group(1)


def _extract_contract_id_list(
    text: str,
    field_name: str,
    *,
    default: list[str],
) -> list[str]:
    pattern = re.compile(rf"{re.escape(field_name)}\s*只能引用:(?P<ids>[^\n。]+)")
    match = pattern.search(text)
    if match is None:
        return default
    raw_ids = match.group("ids").strip()
    if raw_ids == "(无)":
        return []
    ids = [value.strip(" ,.;；。") for value in raw_ids.split(",")]
    return [value for value in ids if value] or default


def _pick_contract_ids(ids: list[str], index: int) -> list[str]:
    if not ids:
        return []
    return [ids[(index - 1) % len(ids)]]


def _build_volume_arcs(
    *,
    vol_id: str,
    chapter_count: int,
    selection_short_arc: bool,
) -> list[dict[str, Any]]:
    """按普通或 selection 短弧合同构造可满足 beat 数量的 mock arcs。"""

    if selection_short_arc:
        arcs = _build_three_arcs(vol_id=vol_id, chapter_count=chapter_count)
        for arc, arc_type in zip(
            arcs,
            ("起因", "升级", "高潮/交代"),
            strict=True,
        ):
            arc["arc_type"] = arc_type
        return arcs
    if chapter_count >= 6:
        return _build_three_arcs(vol_id=vol_id, chapter_count=chapter_count)

    # 普通产品公开入口至少 6 章；4—5 章仅供既有隔离服务测试。这里用两个
    # 至少 2 章的 arc，避免 mock 自己制造无法承载 2 个不重叠 beat 的单章 arc。
    if chapter_count < 4:
        return _build_three_arcs(vol_id=vol_id, chapter_count=chapter_count)
    midpoint = chapter_count // 2
    return [
        {
            "arc_id": f"{vol_id}-arc-1-opening",
            "arc_type": "主线",
            "chapter_range": [1, midpoint],
            "phase": "opening",
            "summary": "[mock] 开篇起势,引入本卷核心矛盾",
            "beats": _build_mock_arc_beats(
                arc_id=f"{vol_id}-arc-1-opening",
                chapter_range=(1, midpoint),
                label="开篇",
                forbidden="提前解决本卷高潮",
            ),
        },
        {
            "arc_id": f"{vol_id}-arc-2-climax",
            "arc_type": "主线",
            "chapter_range": [midpoint + 1, chapter_count],
            "phase": "climax",
            "summary": "[mock] 本卷高潮,关键转折发生",
            "beats": _build_mock_arc_beats(
                arc_id=f"{vol_id}-arc-2-climax",
                chapter_range=(midpoint + 1, chapter_count),
                label="高潮",
                forbidden="开启下一卷核心副本",
            ),
        },
    ]


def _build_three_arcs(*, vol_id: str, chapter_count: int) -> list[dict[str, Any]]:
    """构造每卷 3 个 arc:opening / rising / climax,首末对齐,连续不重叠。

    chapter_count 较小时(< 3)mock 仍返 3 段但段长允许 1-2 — 单测构造场景
    chapter_count == 1 会因 model 校验失败抛出,**caller 应至少传 3**;
    本函数兜底但不保证 [HARD] 副本横跨规则(留 P5-B-04 outline_validators 校验)。
    """
    one_third = max(1, chapter_count // 3)
    arc_1_end = one_third
    arc_2_end = one_third * 2
    arc_3_end = chapter_count
    return [
        {
            "arc_id": f"{vol_id}-arc-1-opening",
            "arc_type": "主线",
            "chapter_range": [1, arc_1_end],
            "phase": "opening",
            "summary": "[mock] 开篇起势,引入本卷核心矛盾",
            "beats": _build_mock_arc_beats(
                arc_id=f"{vol_id}-arc-1-opening",
                chapter_range=(1, arc_1_end),
                label="开篇",
                forbidden="提前解决本卷高潮",
            ),
        },
        {
            "arc_id": f"{vol_id}-arc-2-rising",
            "arc_type": "副本",
            "chapter_range": [arc_1_end + 1, arc_2_end],
            "phase": "rising",
            "summary": "[mock] 副本类弧(≥ 3 章满足 [HARD]),冲突升级",
            "beats": _build_mock_arc_beats(
                arc_id=f"{vol_id}-arc-2-rising",
                chapter_range=(arc_1_end + 1, arc_2_end),
                label="副本推进",
                forbidden="提前触发本卷终局",
            ),
        },
        {
            "arc_id": f"{vol_id}-arc-3-climax",
            "arc_type": "主线",
            "chapter_range": [arc_2_end + 1, arc_3_end],
            "phase": "climax",
            "summary": "[mock] 本卷高潮,关键转折发生",
            "beats": _build_mock_arc_beats(
                arc_id=f"{vol_id}-arc-3-climax",
                chapter_range=(arc_2_end + 1, arc_3_end),
                label="高潮",
                forbidden="开启下一卷核心副本",
            ),
        },
    ]


def _build_mock_arc_beats(
    *,
    arc_id: str,
    chapter_range: tuple[int, int],
    label: str,
    forbidden: str,
) -> list[dict[str, Any]]:
    """按 arc 区间构造连续覆盖 beats,对齐 D2 新生成期契约。"""

    start, end = chapter_range
    span = end - start + 1
    if span <= 1:
        return [
            {
                "beat_id": f"{arc_id}-beat-1",
                "chapter_range": [start, end],
                "summary": f"[mock] {label}单章推进",
                "must_advance": [f"{label}建立关键处境"],
                "forbidden_advance": [forbidden],
            }
        ]

    split = start + max(1, span // 2) - 1
    return [
        {
            "beat_id": f"{arc_id}-beat-1",
            "chapter_range": [start, split],
            "summary": f"[mock] {label}前半推进",
            "must_advance": [f"{label}建立关键处境"],
            "forbidden_advance": [forbidden],
        },
        {
            "beat_id": f"{arc_id}-beat-2",
            "chapter_range": [split + 1, end],
            "summary": f"[mock] {label}后半收束",
            "must_advance": [f"{label}形成清晰转折"],
            "forbidden_advance": [forbidden],
        },
    ]


def _build_curve(chapter_count: int) -> list[int]:
    """构造长度 == chapter_count 的张力曲线,3 → 9 → 6 简化英雄之旅形态。"""
    if chapter_count == 1:
        return [5]
    if chapter_count == 2:
        return [4, 8]
    # 三段线性:前 1/3 从 3 升到 5;中 1/3 从 5 升到 9;末 1/3 从 9 降到 6。
    third = max(1, chapter_count // 3)
    head = [3 + round((5 - 3) * i / max(1, third - 1)) for i in range(third)]
    mid_len = third
    mid = [5 + round((9 - 5) * i / max(1, mid_len - 1)) for i in range(mid_len)]
    tail_len = chapter_count - 2 * third
    tail = [9 + round((6 - 9) * i / max(1, tail_len - 1)) for i in range(tail_len)]
    values = head + mid + tail
    return [max(1, min(10, int(v))) for v in values]


# ---- P5-B-05:ChapterCardGenerator 合规 ChapterCard fixture ------------------


_CHAPTER_NUMBER_RE = re.compile(r"chapter_number(?:[^0-9]+)(\d+)")
"""ChapterCardGenerator 段从 user_text 抓 ``chapter_number``,决定首章 vs 续章分支。"""

_PREV_POV_RE = re.compile(r"prev_pov_character(?:[^a-zA-Z0-9_-]+)([a-zA-Z0-9_-]+)")
"""从 user_text 抓 ``prev_pov_character``(命中 ``null`` 兜底返字面 ``"null"``)。"""

# v1.0.x wave 4 session 2:从 user_text 抓 mini_plan_event 字面 +
# arc_summary fallback,用于派生差异化 must_achieve / setting / next_chapter_seed。
_CARD_MINI_PLAN_EVENT_RE = re.compile(r"- mini_plan_event:(.+?)(?:\n|$)")
_CARD_ARC_SUMMARY_RE = re.compile(r"- arc_summary:(.+?)(?:\n|$)")
_CARD_PREV_SUMMARY_RE = re.compile(r"- prev_chapter_summary:(.+?)(?:\n|$)")

_CARD_CHAR_SECTION_HEADING = "## 角色 voice hints"
_CARD_THREAD_SECTION_HEADING = "## 当前活跃伏笔"
_CARD_TRAILER = "请按 system prompt"

# v1.0.x wave 4 session 4:抓 user_text 内"同质化 retry"banner 的 must_achieve
# 禁词字面(forbidden phrases),用于 mock 路径 retry 时强制产出差异化输出 — 让
# chapter_card_service 后置同质化检测器 + 自动 retry 的单测可重放(retry 后字符
# 集重合率必显著降低)。
_CARD_FORBIDDEN_HEADING = "## 同质化 retry — 必须避开的 must_achieve 字面"
# 字面行格式 `  - 「<phrase>」`(对齐 _build_forbidden_must_achieve_block 拼装)
_CARD_FORBIDDEN_PHRASE_RE = re.compile(r"^\s{2}-\s+「(.+?)」\s*$", re.MULTILINE)

# v1.0.x wave 4 session 3b:抓 user_text 内 "前 N 章长程事实序列" 段的
# key_events[*].description 字面集合,派生 must_achieve / next_chapter_seed 时
# 主动避开这些字面,确保 mock 输出与前 N 章不重合(差异化测试纪律继承)。
_CARD_PREV_SUMMARIES_HEADING = "## 前 N 章长程事实序列"
# 每条 key_events 项:`  - <description>  actors=[...] loc=...` 或 `  - <description>`
# 抓两个空格起首 + "- " 后到首个双空格(actors=/loc= prefix)或行尾,作 description
_CARD_PREV_SUMMARY_KEY_EVENT_RE = re.compile(
    r"^\s{2}-\s+(.+?)(?:\s{2,}actors=|\s{2,}loc=|$)", re.MULTILINE
)
# 单条 character_interactions.dynamic 抓取(用于 LLM 真路径角色关系动态延续提示)
_CARD_PREV_SUMMARY_INTERACTION_RE = re.compile(
    r"^\s{2}-\s+\S+\s*<→>\s*\S+:(.+?)$", re.MULTILINE
)


def _extract_chapter_card_context(
    user_text: str,
) -> tuple[list[tuple[str, str]], list[str]]:
    """从 ChapterCardGenerator user_text 抓角色(id,name)列表 + thread_id 列表。

    section heading 之后的列表行形如 ``  - <id> | <name> | <role> | ...`` /
    ``  - <thread_id> | <title> | <status>``。本函数按 heading 切片 + 行首
    ``-`` 抓首段 ``id`` 与第二段 ``name``;不 sanitize 命中失败的兜底由
    caller(返空 list)。
    """
    char_section, thread_section = "", ""
    if _CARD_CHAR_SECTION_HEADING in user_text:
        after_char = user_text.split(_CARD_CHAR_SECTION_HEADING, 1)[1]
        if _CARD_THREAD_SECTION_HEADING in after_char:
            char_section, after_thread = after_char.split(
                _CARD_THREAD_SECTION_HEADING, 1
            )
            thread_section = after_thread.split(_CARD_TRAILER, 1)[0]
        else:
            char_section = after_char

    characters: list[tuple[str, str]] = []
    for line in char_section.splitlines():
        stripped = line.strip()
        if stripped.startswith("- ") and "|" in stripped:
            parts = [part.strip() for part in stripped[2:].split("|")]
            if parts[0]:
                characters.append((parts[0], parts[1] if len(parts) > 1 else ""))

    thread_ids: list[str] = []
    for line in thread_section.splitlines():
        stripped = line.strip()
        if stripped.startswith("- ") and "|" in stripped:
            first = stripped[2:].split("|", 1)[0].strip()
            if first:
                thread_ids.append(first)

    return characters, thread_ids


def _append_mentioned_character_ids(
    *,
    related: list[str],
    characters: list[tuple[str, str]],
    text: str,
) -> list[str]:
    """把事件文本中点名的角色补进 mock related 列表。

    C-4 后,章节卡硬任务提到已知角色全名时必须把角色 ID 放进
    ``related_characters``。Mock 也要遵守这个闭环,否则 service happy path
    会被正确拦截。
    """
    seen = set(related)
    for character_id, name in characters:
        if character_id in seen or not name:
            continue
        if name in text:
            related.append(character_id)
            seen.add(character_id)
    return related[:5]


def _split_event_into_pieces(text: str) -> list[str]:
    """v1.0.x wave 4 session 2:把 mini_plan_event / arc_summary 按中文标点切片。

    用于派生 ``must_achieve`` 多条事件 — 每条复用源文本不同子句,保证不同
    源文本下 must_achieve 集合字符集差异化(差异化测试纪律继承自 wave 4
    session 1 OutlineBuilder mini_plan fixture)。
    """
    pieces = [p.strip() for p in re.split(r"[,。;、:]", text) if p.strip()]
    return pieces


def _extract_prev_summary_key_events(user_text: str) -> list[str]:
    """v1.0.x wave 4 session 3b:从 user_text 抓"前 N 章长程事实"段的
    ``key_events[*].description`` 字面集合(用于 must_achieve / next_chapter_seed
    派生时主动避开,确保 mock 输出与前 N 章字面差异化)。

    返空 list 表示 prev_summaries 段不存在 / 段内无 key_events / 老工作区。
    """
    if _CARD_PREV_SUMMARIES_HEADING not in user_text:
        return []
    section = user_text.split(_CARD_PREV_SUMMARIES_HEADING, 1)[1]
    # 切到下个 "## " heading(角色 / 伏笔 / trailer)之前
    next_heading_idx = section.find("\n## ")
    if next_heading_idx > 0:
        section = section[:next_heading_idx]
    descriptions: list[str] = []
    for match in _CARD_PREV_SUMMARY_KEY_EVENT_RE.finditer(section):
        desc = match.group(1).strip()
        # 跳过非 key_events 行(summary_text / unfinished_actions 等其他列表项;
        # 简化:含中文动词池的关键短句通常都是 key_events,但容忍噪声)
        if desc and len(desc) < 200:  # 单条 key_events.description 通常 < 80 字
            descriptions.append(desc)
    return descriptions


def _extract_forbidden_must_achieve_phrases(user_text: str) -> list[str]:
    """v1.0.x wave 4 session 4:从 user_text 抓"同质化 retry — 必须避开的 "
    ``must_achieve`` 字面"banner 的禁词字面集合(用于 mock 路径 retry 时强制产出
    差异化输出)。

    返空 list 表示首次生成(无 banner)/ banner 段无禁词行。"""
    if _CARD_FORBIDDEN_HEADING not in user_text:
        return []
    section = user_text.split(_CARD_FORBIDDEN_HEADING, 1)[1]
    next_heading_idx = section.find("\n## ")
    if next_heading_idx > 0:
        section = section[:next_heading_idx]
    phrases: list[str] = []
    for match in _CARD_FORBIDDEN_PHRASE_RE.finditer(section):
        phrase = match.group(1).strip()
        if phrase:
            phrases.append(phrase)
    return phrases


def _derive_must_achieve_from_event(
    mini_plan_event: str | None,
    arc_summary: str,
    chapter_number: int,
    prev_key_event_descriptions: list[str] | None = None,
    forbidden_phrases: list[str] | None = None,
) -> list[str]:
    """v1.0.x wave 4 session 2:派生 2-3 条差异化 must_achieve。

    - **mini_plan_event 非空**:直接复用作 must_achieve[0](保留全句字面),
      把切片片段作 must_achieve[1] / [2](展开细节,字符集与 [0] 高度相关
      但保证不同源文本不雷同)
    - **mini_plan_event 空**:fallback 从 arc_summary 切片 + chapter_number
      轮换不同切片(差异化保护 — 杜绝按 ``f"章 {N} 必达事件"`` 占位字面)

    **v1.0.x wave 4 session 3b 升级**:可选 ``prev_key_event_descriptions``
    入参为前 N 章 ``key_events[*].description`` 字面集合;若候选 must_achieve
    与任一 prev 字面字符级重合超过 60%,加 chapter_id 后缀强制差异化(对齐
    会话 1+2+3a 累积承诺到期 — Q4 阈值 < 50% 真 LLM 收紧准入)。
    """
    if mini_plan_event:
        pieces = _split_event_into_pieces(mini_plan_event)
        if len(pieces) >= 2:
            candidates = [
                mini_plan_event,
                f"{pieces[1]} —— 兑现 mini_plan 三元素",
            ]
        else:
            candidates = [
                mini_plan_event,
                f"延展 {mini_plan_event[:12]} 的细节铺陈",
            ]
    else:
        # Fallback 路径(老工作区无 mini_plan):从 arc_summary 切片差异化
        pieces = _split_event_into_pieces(arc_summary)
        if not pieces:
            # 极端 fallback,arc_summary 也无切片可分时按 chapter_number 派生
            candidates = [
                f"推进 arc 主线,涉及第 {chapter_number} 章关键节点",
                f"延展第 {chapter_number} 章的角色互动细节",
            ]
        else:
            # chapter_number 轮换不同切片,杜绝雷同字面
            n = max(1, chapter_number)
            first = pieces[(n - 1) % len(pieces)]
            second = pieces[n % len(pieces)] if len(pieces) > 1 else pieces[0]
            candidates = [
                f"{first}(章 {n} 主推进)",
                f"{second}(章 {n} 延展细节)",
            ]

    # v1.0.x wave 4 session 3b:与 prev_key_event_descriptions 字面重合检测
    if prev_key_event_descriptions:
        prev_chars = set("".join(prev_key_event_descriptions))
        result: list[str] = []
        for c in candidates:
            c_chars = set(c)
            if not c_chars:
                result.append(c)
                continue
            overlap = len(c_chars & prev_chars) / len(c_chars)
            if overlap > 0.6:
                # 字符级重合超 60% → 加 chapter_number 标识差异化后缀
                result.append(f"{c} ✦ 章 {chapter_number} 独有视角(避前 N 章字面重合)")
            else:
                result.append(c)
        candidates = result

    # v1.0.x wave 4 session 4:与 forbidden_phrases 字面重合检测(同质化 retry
    # 入参)— 候选 must_achieve 与任一 forbidden 字面字符集重合超 50% 则强制
    # 重写,加"同质化 retry 重写 / 章 N 视角切换"后缀确保 retry 后字符集重合率
    # 显著降低。本路径让 chapter_card_service 同质化检测器 + 自动 retry 单测
    # 可重放(retry 后能产出真正差异化的 must_achieve 字面)。
    if forbidden_phrases:
        result_with_forbidden: list[str] = []
        for c in candidates:
            c_chars = set(c)
            max_overlap = 0.0
            for fp in forbidden_phrases:
                fp_chars = set(fp)
                if not (c_chars | fp_chars):
                    continue
                overlap = len(c_chars & fp_chars) / len(c_chars | fp_chars)
                if overlap > max_overlap:
                    max_overlap = overlap
            if max_overlap >= 0.5:
                # ≥ 50% 触发字面重写:用 chapter_number 派生全新维度后缀
                result_with_forbidden.append(
                    f"章 {chapter_number} 重写视角:绕开同批字面"
                    f"({c[:8]}…)切换主语 + 动词 + 宾语三元素"
                )
            else:
                result_with_forbidden.append(c)
        return result_with_forbidden
    return candidates


def _generate_chapter_card_generator_json(user_text: str) -> str:
    """ChapterCardGenerator 合规 ChapterCard "决策" JSON fixture(P5-B-05,v1.0.x wave 4 session 2 升级)。

    设计目标:
    - 3 Literal 字段(``scene_continuation`` / ``pov_change`` + caller 注入的
      ``arc_phase``)严格命中合法枚举(对齐 v2_wishlist
      §"ChapterCard `scene_continuation` 枚举沉淀" + p5_plan §9 强制),
      让单测断言 fixture 字面合规 + agent 跨字段引用校验路径可走通。
    - ``pov_character`` 必从 user_text 抓出的 character_voices 列表选首位,
      ``related_characters`` 含 pov + 至多前 3 个 chars(对齐 prompt "1-5 个");
      ``related_plot_threads`` 命中前 1 条 thread(空时省略)。
    - ``continuity_context.pov_to`` == ``pov_character``(agent 跨字段校验项)。
    - ``chapter_number == 1`` 时:``scene_continuation="switch"`` /
      ``pov_change=false`` / ``pov_from=null`` / ``time_gap="开篇"``(首章语义);
      ``> 1`` 时:``scene_continuation="same"`` / ``pov_change=false`` /
      ``pov_from=<pov_character>`` / ``time_gap="立即续接"``(简化连续:同 pov 维持)。
    - **v1.0.x wave 4 session 2 升级**:must_achieve / setting / next_chapter_seed
      改读 user_text 内 ``mini_plan_event`` 字段派生(优先) / 缺失时读
      ``arc_summary`` 字面切片 + chapter_number 轮换(fallback)。**杜绝**按
      ``chapter_number`` 简单派生 ``f"[mock] 章 {N} 必达事件"`` 占位字面
      (会话 1 立的差异化测试纪律继承)。
    """
    characters, thread_ids = _extract_chapter_card_context(user_text)
    char_ids = [character_id for character_id, _name in characters]
    if not char_ids:
        # 兜底:user_text 解析失败时填占位 ID,Pydantic 会校验通过但 agent
        # 跨字段校验会在 caller 实测时拦截(单测可控制 char_ids 注入路径)。
        char_ids = ["mock-protagonist"]
        characters = [("mock-protagonist", "")]

    pov = char_ids[0]

    chapter_number = _extract_int(user_text, _CHAPTER_NUMBER_RE, default=1)
    chapter_id_match = _CHAPTER_ID_RE.search(user_text)
    chapter_id = chapter_id_match.group(0) if chapter_id_match else "ch-unknown"

    # v1.0.x wave 4 session 2:抓 mini_plan_event(优先)/ arc_summary(fallback)
    # / prev_chapter_summary(非首章 continuity_context 桥接素材)
    mini_plan_match = _CARD_MINI_PLAN_EVENT_RE.search(user_text)
    mini_plan_event = mini_plan_match.group(1).strip() if mini_plan_match else None
    arc_summary_match = _CARD_ARC_SUMMARY_RE.search(user_text)
    arc_summary = (
        arc_summary_match.group(1).strip() if arc_summary_match else "未知 arc 概要"
    )
    prev_summary_match = _CARD_PREV_SUMMARY_RE.search(user_text)
    prev_summary = (
        prev_summary_match.group(1).strip() if prev_summary_match else "(首章,无上一章)"
    )

    # v1.0.x wave 4 session 3b:抓前 N 章 key_events.description 字面集合,
    # 派生 must_achieve / next_chapter_seed 时主动避开。
    prev_key_event_descriptions = _extract_prev_summary_key_events(user_text)

    # v1.0.x wave 4 session 4:抓同质化 retry banner 的 forbidden 字面,
    # retry 时 mock 路径强制产出差异化输出(让 service 层单测可重放)。
    forbidden_phrases = _extract_forbidden_must_achieve_phrases(user_text)

    must_achieve = _derive_must_achieve_from_event(
        mini_plan_event=mini_plan_event,
        arc_summary=arc_summary,
        chapter_number=chapter_number,
        prev_key_event_descriptions=prev_key_event_descriptions,
        forbidden_phrases=forbidden_phrases,
    )

    # setting + next_chapter_seed 同步派生(优先 mini_plan_event,空时 arc_summary)
    event_source = mini_plan_event or arc_summary
    related = char_ids[:3] if len(char_ids) >= 1 else [pov]
    if pov not in related:
        related = [pov, *related]
    related = _append_mentioned_character_ids(
        related=related,
        characters=characters,
        text=event_source,
    )
    setting = f"{event_source[:18]} 的场景铺陈(由 {chapter_id} 派生)"
    # next_chapter_seed 也加 chapter_id 标识 + prev_key_event 避重(简化:
    # 用 chapter_id 后缀强制不同章 seed 字面差异化)
    next_seed = (
        f"延续本章 {event_source[-15:]} 的悬念,推进至下一章 — "
        f"勿与本章 must_achieve 字面雷同(seed-{chapter_id})"
    )

    # title 也从 event 派生(前 8 字,保证不同章 title 不雷同)
    title = event_source[:10] if event_source else f"章 {chapter_number} 占位"

    if chapter_number == 1:
        continuity = {
            "prev_chapter_tail": "(开篇,无上一章)",
            "unfinished_actions": [],
            "time_gap": "开篇",
            "scene_continuation": "switch",
            "pov_change": False,
            "pov_from": None,
            "pov_to": pov,
        }
    else:
        # 非首章 mock 保留 legacy/人工单卡缺正文时的占位兼容。正式 Run 会由
        # ChapterCardService 用上一章真实 final.md 机械覆盖。
        continuity = {
            "prev_chapter_tail": (
                "(上一章正文待生成 — 参考 prev_chapter_summary 推断衔接)"
            ),
            "unfinished_actions": [f"延续 {prev_summary[:15]} 的悬念"],
            "time_gap": "立即续接",
            "scene_continuation": "same",
            "pov_change": False,
            "pov_from": pov,
            "pov_to": pov,
        }

    payload = {
        "title": title,
        "pov_character": pov,
        "setting": setting,
        # M2 纠正侧(v1.1 wave 5 s3):per-scene 卓越性焦点。mock 给一条含"情感节点 /
        # 关键戏 / 克制"结构的占位,模拟真 LLM 行为(default="" 已保后向兼容,但 mock
        # 给非空 → 可端到端验证 writer craft_focus 注入路径)。
        "craft_focus": (
            f"情感节点:{event_source[:10]} 处深挖角色内心动摇;"
            "关键戏给具体画面;其余过场克制简笔。"
        ),
        "must_achieve": must_achieve,
        "must_unresolved": [
            f"{event_source[:12]} 留待后续揭晓的疑点",
        ],
        # v1.1 audit G19b 治本(2026-05-27):forbidden 不再含风格类约束(原
        # "[mock] 长句堆叠 > 40 字" 被 _detect_forbidden_style_pollution 拒绝);
        # 替代为纯剧情禁元素 fixture(对齐"forbidden 字段语义边界" 段)。
        "forbidden": [
            "[mock] 现代用语",
            "[mock] 跨域穿帮元素",
        ],
        "continuity_context": continuity,
        "next_chapter_seed": next_seed,
        "related_plot_threads": thread_ids[:1] if thread_ids else [],
        "related_characters": related,
    }
    return json.dumps(payload, ensure_ascii=False, indent=2)


# ---------------------------------------------------------------------------
# v1.0.x wave 4 session 1: mini_plan_generator fixture
# ---------------------------------------------------------------------------


# 单卷 mini_plan 解析 user_text 用的正则:
#   - chapter_id 映射表行:`chapter_number=N → chapter_id=ch-NNNN`
#   - 本卷 volume_id:`volume_id:vN`
#   - 本卷 arcs:`<arc_id> | <arc_type> | chapter_range=[<s>, <e>] | phase=<phase>`
#   - tension_curve:`[<int>, <int>, ...]`
#   - linked_characters / linked_threads:命中已知 ID 集合
_MINI_PLAN_CHAPTER_MAP_RE = re.compile(
    r"chapter_number=(\d+)\s*→\s*chapter_id=(ch-[\w-]+)"
)
_MINI_PLAN_VOLUME_ID_RE = re.compile(r"volume_id:\s*([\w-]+)")
_MINI_PLAN_ARC_RE = re.compile(
    r"-\s+([\w-]+)\s*\|\s*([^|]+?)\s*\|\s*chapter_range=\[(\d+),\s*(\d+)\]\s*\|\s*phase=(\w+)"
)
_MINI_PLAN_TENSION_RE = re.compile(r"tension_curve\(每章 1-10\):\[([0-9,\s]+)\]")
_MINI_PLAN_CHAR_ID_RE = re.compile(
    r"-\s+([\w-]+)\s*\|\s*[^|]+?\(.+?\)\s*\|\s*voice_hint:"
)
_MINI_PLAN_THREAD_ID_RE = re.compile(r"-\s+(thread-[\w-]+)\s*\|")
_MINI_PLAN_DRAMATIC_CONTRACT_RE = re.compile(
    r"dramatic_contract:\s*(\{[^\n]+\})"
)

# fixture 动词池(8 个),按 chapter_number 轮换确保章间差异
_MINI_PLAN_VERB_POOL: tuple[str, ...] = (
    "察觉",
    "对抗",
    "潜入",
    "揭露",
    "守护",
    "试探",
    "突破",
    "回忆",
)


def _generate_mini_plan_generator_json(user_text: str) -> str:
    """mini_plan_generator 合规单卷 mini_plan JSON fixture(v1.0.x wave 4 session 1)。

    设计目标:
    - 严格按 user_text 内 chapter_id 映射表派生 items(不自己编 chapter_id)
    - 每章 ``one_line_event`` 主语 + 动词 + 宾语 三元素差异化:
        * 主语:从 character_voices 列表轮换(chapter_number % len)
        * 动词:从预置 ``_MINI_PLAN_VERB_POOL`` 8 词轮换
        * 宾语:从本章所属 arc summary 抓出后按 chapter_number % N 切片
    - ``linked_characters`` 按 chapter_number 轮换取 1-2 个(过渡章可空)
    - ``linked_threads`` 按 chapter_number 取 1 个或空
    - 杜绝按 ``chapter_number`` 简单派生 ``f"章 {N} 事件"`` 占位字面(避免假递进)
    - mock fixture 必须能通过 wave 4 立的"差异化测试"断言(三元组重合率 ≤ 30%)
    """
    # 抓 chapter_id 映射
    chapter_map = _MINI_PLAN_CHAPTER_MAP_RE.findall(user_text)
    if not chapter_map:
        # 兜底:user_text 没有映射表 → 返最小合法 1 章 fixture
        return json.dumps({"items": []}, ensure_ascii=False)
    chapter_num_to_id = [(int(n), cid) for n, cid in chapter_map]

    # 抓 volume_id
    volume_match = _MINI_PLAN_VOLUME_ID_RE.search(user_text)
    volume_id = volume_match.group(1) if volume_match else "v1"

    # 抓 arcs(arc_id, arc_type, range_start, range_end, phase)
    arcs_raw = _MINI_PLAN_ARC_RE.findall(user_text)
    arcs: list[tuple[str, int, int]] = []  # (arc_id, start, end)
    for arc_id, _arc_type, range_s, range_e, _phase in arcs_raw:
        arcs.append((arc_id, int(range_s), int(range_e)))
    if not arcs:
        # 兜底:无 arc 数据时,所有章归到单 arc
        max_ch = max(n for n, _ in chapter_num_to_id) if chapter_num_to_id else 1
        arcs = [(f"{volume_id}-arc-default", 1, max_ch)]

    # 抓 character_voices
    char_ids = _MINI_PLAN_CHAR_ID_RE.findall(user_text)
    if not char_ids:
        char_ids = ["mock-char"]

    # 抓 thread_ids
    thread_ids = _MINI_PLAN_THREAD_ID_RE.findall(user_text)

    # 抓 tension_curve(用于决定 linked_characters 数量 — 高 tension 多角色)
    tension_match = _MINI_PLAN_TENSION_RE.search(user_text)
    if tension_match:
        tensions = [int(t.strip()) for t in tension_match.group(1).split(",")]
    else:
        tensions = [5] * len(chapter_num_to_id)

    dramatic_contract: dict[str, Any] | None = None
    for raw_contract in _MINI_PLAN_DRAMATIC_CONTRACT_RE.findall(user_text):
        try:
            candidate = json.loads(raw_contract)
        except json.JSONDecodeError:
            continue
        if candidate.get("function") == "finale":
            dramatic_contract = candidate
            break
    designated_drama_chapter = (
        max(chapter_number for chapter_number, _chapter_id in chapter_num_to_id)
        if dramatic_contract is not None
        else None
    )

    items: list[dict[str, Any]] = []
    for idx, (chapter_number, chapter_id) in enumerate(chapter_num_to_id):
        # 找本章所属 arc
        arc_id = arcs[0][0]
        for a_id, a_start, a_end in arcs:
            if a_start <= chapter_number <= a_end:
                arc_id = a_id
                break

        # 派生主语 + 动词 + 宾语
        subject = char_ids[(chapter_number - 1) % len(char_ids)]
        verb = _MINI_PLAN_VERB_POOL[(chapter_number - 1) % len(_MINI_PLAN_VERB_POOL)]
        # 宾语:把 arc_id 字面切片(每 arc 内不同章拿不同切片)
        # arc_id 形如 v1-arc-1-opening,取尾部 hash 派生
        arc_hash = sum(ord(c) for c in arc_id)
        obj_seed = (arc_hash + chapter_number * 17) % 12
        objects = (
            "封印之谜",
            "权力对峙",
            "记忆碎片",
            "古老遗物",
            "组织内部",
            "敌方阵营",
            "盟友身份",
            "禁忌真相",
            "下一阶段计划",
            "意外变故",
            "童年往事",
            "命运抉择",
        )
        obj = objects[obj_seed]

        tension = tensions[idx] if idx < len(tensions) else 5
        # 高 tension(>=7) 多角色,低 tension 单角色
        chars_count = 2 if tension >= 7 else 1
        linked_chars = [
            char_ids[(chapter_number - 1 + k) % len(char_ids)]
            for k in range(min(chars_count, len(char_ids)))
        ]
        # 去重(轮换可能重复)
        linked_chars = list(dict.fromkeys(linked_chars))

        # linked_threads:每隔 2 章带一个(过渡章空)
        linked_threads_for_chapter = (
            [thread_ids[chapter_number % len(thread_ids)]]
            if thread_ids and chapter_number % 2 == 1
            else []
        )

        one_line = (
            f"{subject}在{obj}的阴影下,{verb}此前隐藏的线索,推进{arc_id} 的"
            f"第 {chapter_number} 个关键节点。"
        )

        item: dict[str, Any] = {
            "chapter_id": chapter_id,
            "chapter_number": chapter_number,
            "volume_id": volume_id,
            "arc_id": arc_id,
            "one_line_event": one_line,
            "linked_characters": linked_chars,
            "linked_threads": linked_threads_for_chapter,
        }
        if chapter_number == designated_drama_chapter and dramatic_contract is not None:
            item["one_line_event"] = "；".join(
                str(dramatic_contract[field])
                for field in (
                    "actor_goal",
                    "counteraction",
                    "choice",
                    "consequence",
                )
            )
            for character_id in (
                dramatic_contract["actor_id"],
                dramatic_contract["counterforce_id"],
            ):
                if character_id not in item["linked_characters"]:
                    item["linked_characters"].append(character_id)
            drama_thread = dramatic_contract.get("thread_id")
            if drama_thread and drama_thread not in item["linked_threads"]:
                item["linked_threads"].append(drama_thread)
            item["dramatic_contract"] = dramatic_contract
        items.append(item)

    return json.dumps({"items": items}, ensure_ascii=False, indent=2)


# ---- v1.0.x wave 4 session 3a: chapter_summary_extractor fixture -----------

# fixture 动词池(8 个),按 chapter_number 轮换确保章间 summary_text 差异
_CHAPTER_SUMMARY_VERB_POOL: tuple[str, ...] = (
    "察觉",
    "对峙",
    "潜入",
    "揭露",
    "守护",
    "试探",
    "突破",
    "回忆",
)

# fixture 事件宾语池(12 个),按 chapter_number * 7 + 3 派生差异化字符集
_CHAPTER_SUMMARY_OBJECT_POOL: tuple[str, ...] = (
    "封印之谜",
    "权位风波",
    "记忆碎片",
    "古老遗物",
    "组织暗流",
    "敌方阵营",
    "盟友身份",
    "禁忌真相",
    "下一阶段计划",
    "意外变故",
    "童年往事",
    "命运抉择",
)

# fixture 场景修饰池(6 个),给 scene_setting + 事件 location 差异化
_CHAPTER_SUMMARY_SCENE_POOL: tuple[str, ...] = (
    "剑光晃动的演武场",
    "灯火摇曳的廊外",
    "夜风卷过的山门",
    "雨幕笼罩的密室",
    "晨光初照的庭院",
    "残月隐没的暗道",
)

# fixture 未完动作池(8 个),按 chapter_number 与轮换 idx 派生
_CHAPTER_SUMMARY_UNFINISHED_POOL: tuple[str, ...] = (
    "灰雀飞向琉璃瓦未达",
    "苏离收剑后未明示立场",
    "玉佩落地的回音未散",
    "密信传递的下落未明",
    "暗流的源头未追溯",
    "古谜的最后一层未揭",
    "权位风波的余波未平",
    "剑势相试的胜负未定",
)

# 从 user_text 抓 chapter_summary fixture 派生所需字段(章节元数据段)
_SUMMARY_CHAPTER_NUMBER_RE = re.compile(r"chapter_number(?:[^0-9]+)(\d+)")
_SUMMARY_POV_RE = re.compile(r"视角角色\(pov_character\):\s*([^\n]+)")
_SUMMARY_TITLE_RE = re.compile(r"标题:\s*([^\n]+)")
_SUMMARY_SOURCE_ID_RE = re.compile(r'"source_id"\s*:\s*"(E\d{4,})"')
_SUMMARY_RELATED_CHARS_RE = re.compile(r"出场角色\(related_characters\):\s*([^\n]+)")


def _generate_chapter_summary_extractor_json(user_text: str) -> str:
    """chapter_summary_extractor 合规 JSON fixture(v1.0.x wave 4 session 3a)。

    设计目标(继承 wave 4 差异化测试纪律 + Q4 阈值 < 50% 生产级):

    - 严格从 user_text 抓 chapter_id / chapter_number / pov_character / title /
      related_characters 派生差异化字段;不依赖 LLM 真智能,但**必须**避免按
      chapter_number 简单派生 ``f"章 N 摘要"`` 占位字面(否则 wave 4 立的差异
      化纪律覆盖测试通不过)。
    - summary_text 用 actor + verb + obj + scene + chapter_id 多维拼接,5 章
      字符集重合率 < 50%。
    - key_events 按动宾结构 + actors / location 派生;2-3 条按 chapter_number
      奇偶差异化。
    - character_interactions:0-1 条(related_characters ≥ 2 才产)。
    - unfinished_actions:1-2 条按 chapter_number 轮换。
    - pov_character / scene_setting LLM 返字符串(非 None);Pydantic 校验通过。

    严格匹配 ``src.core.agents.chapter_summary_extractor._ExtractorLLMPayload`` schema。
    """
    chapter_id_match = _CHAPTER_ID_RE.search(user_text)
    chapter_id = chapter_id_match.group(0) if chapter_id_match else "ch-unknown"

    cn_match = _SUMMARY_CHAPTER_NUMBER_RE.search(user_text)
    chapter_number = int(cn_match.group(1)) if cn_match else 1

    pov_match = _SUMMARY_POV_RE.search(user_text)
    pov_character = pov_match.group(1).strip() if pov_match else ""

    title_match = _SUMMARY_TITLE_RE.search(user_text)
    title = title_match.group(1).strip() if title_match else f"第 {chapter_number} 章"

    rc_match = _SUMMARY_RELATED_CHARS_RE.search(user_text)
    if rc_match:
        rc_block = rc_match.group(1).strip()
        if rc_block.startswith("("):
            related_chars: list[str] = []
        else:
            related_chars = [c.strip() for c in rc_block.split(",") if c.strip()]
    else:
        related_chars = []

    source_ref_mode = "## 章节正文证据目录\n\n" in user_text
    source_id_match = _SUMMARY_SOURCE_ID_RE.search(user_text)
    evidence_source_id = (
        source_id_match.group(1) if source_id_match is not None else "E0001"
    )
    chapter_text = user_text.split("## 章节正文\n\n", 1)[-1]
    chapter_text = chapter_text.split("<!-- agent_role:", 1)[0].strip()
    evidence_quote = chapter_text[:120].strip()
    if not evidence_quote:
        evidence_quote = "正文无可抽取事实"

    # 派生动词 / 宾语 / 场景 / 未完动作(按 chapter_number 轮换 + 多维偏移)
    verb = _CHAPTER_SUMMARY_VERB_POOL[
        (chapter_number - 1) % len(_CHAPTER_SUMMARY_VERB_POOL)
    ]
    obj = _CHAPTER_SUMMARY_OBJECT_POOL[
        (chapter_number * 7 + 3) % len(_CHAPTER_SUMMARY_OBJECT_POOL)
    ]
    scene = _CHAPTER_SUMMARY_SCENE_POOL[
        (chapter_number - 1) % len(_CHAPTER_SUMMARY_SCENE_POOL)
    ]

    # 主角 + 次角选取(pov 优先,无则取 related_chars 首角)
    actor = (
        pov_character
        if pov_character
        else (related_chars[0] if related_chars else "未署名角色")
    )
    secondary = (
        related_chars[1]
        if len(related_chars) >= 2
        else (related_chars[0] if related_chars else "")
    )

    # summary_text:多维拼接,5 章字符集重合率 < 50% 目标
    summary_text = (
        f"{actor}在{scene}里{verb}了与「{obj}」相关的线索,"
        f"本章于「{title}」节点推进 —— {actor}的{verb}动作直接触发了下一阶段的转折。"
        f"围绕「{obj}」的暗流在 {chapter_id} 段落里呈现出与既往不同的形态:"
        f"过去的铺垫被{verb}所验证,新的疑虑由此展开。"
    )

    # key_events:2-3 条
    key_events: list[dict[str, Any]] = [
        {
            "description": f"{actor}{verb}「{obj}」的关键线索",
            "actors": [actor] if actor else [],
            "location": scene,
            "evidence_quote": evidence_quote,
        }
    ]
    if secondary and secondary != actor:
        key_events.append(
            {
                "description": f"{actor}与{secondary}就「{obj}」展开试探",
                "actors": [actor, secondary],
                "location": scene,
                "evidence_quote": evidence_quote,
            }
        )
    # 第 3 条按 chapter_number 偶数加(差异化:并非每章都有)
    if chapter_number % 2 == 0:
        third_verb = _CHAPTER_SUMMARY_VERB_POOL[
            chapter_number % len(_CHAPTER_SUMMARY_VERB_POOL)
        ]
        third_obj = _CHAPTER_SUMMARY_OBJECT_POOL[
            (chapter_number * 5 + 1) % len(_CHAPTER_SUMMARY_OBJECT_POOL)
        ]
        key_events.append(
            {
                "description": f"{actor}{third_verb}「{third_obj}」的余波",
                "actors": [actor] if actor else [],
                "location": scene,
                "evidence_quote": evidence_quote,
            }
        )

    # character_interactions:0-1 条(双角色才产)
    character_interactions: list[dict[str, Any]] = []
    if secondary and secondary != actor:
        character_interactions.append(
            {
                "char_a": actor,
                "char_b": secondary,
                "dynamic": f"由{verb}前的疏离转向{verb}后的试探",
                "evidence_quote": evidence_quote,
            }
        )

    # unfinished_actions:1-2 条
    unfinished_idx_1 = (chapter_number - 1) % len(_CHAPTER_SUMMARY_UNFINISHED_POOL)
    unfinished_actions = [
        {
            "description": _CHAPTER_SUMMARY_UNFINISHED_POOL[unfinished_idx_1],
            "is_immediate": False,
            "evidence_quote": evidence_quote,
        }
    ]
    if chapter_number % 3 == 0:
        unfinished_idx_2 = (chapter_number * 2 + 1) % len(
            _CHAPTER_SUMMARY_UNFINISHED_POOL
        )
        # 去重(避免轮换偶发同 idx)
        candidate = _CHAPTER_SUMMARY_UNFINISHED_POOL[unfinished_idx_2]
        if all(item["description"] != candidate for item in unfinished_actions):
            unfinished_actions.append(
                {
                    "description": candidate,
                    "is_immediate": False,
                    "evidence_quote": evidence_quote,
                }
            )

    payload = {
        "summary_text": summary_text,
        "pov_character": pov_character,
        "pov_evidence_quote": evidence_quote if pov_character else "",
        "scene_setting": scene,
        "scene_setting_evidence_quote": evidence_quote,
        "key_events": key_events,
        "character_interactions": character_interactions,
        "unfinished_actions": unfinished_actions,
        "character_states": [
            {
                "character_id": actor,
                "state": f"完成{verb}后保持警觉",
                "evidence_quote": evidence_quote,
            }
        ],
        "established_facts": [
            {
                "fact": f"{chapter_id} 已发生正文所述事件",
                "evidence_quote": evidence_quote,
            }
        ],
        "revealed_mechanics": [],
    }
    if source_ref_mode:
        payload["pov_evidence_source_id"] = evidence_source_id if pov_character else ""
        payload.pop("pov_evidence_quote")
        payload["scene_setting_evidence_source_id"] = evidence_source_id
        payload.pop("scene_setting_evidence_quote")
        for field in (
            "key_events",
            "character_interactions",
            "unfinished_actions",
            "character_states",
            "established_facts",
            "revealed_mechanics",
        ):
            for item in payload[field]:
                item["evidence_source_id"] = evidence_source_id
                item.pop("evidence_quote", None)
    return json.dumps(payload, ensure_ascii=False, indent=2)
