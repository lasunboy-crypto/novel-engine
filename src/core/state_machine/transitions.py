"""章节状态机:转换规则与判定函数。

纯逻辑模块,**不引入任何 IO**(不读文件、不调 LLM、不连数据库)。
PROJECT_SPEC §6 只列了 REVISING 升级部分(3 条转换),完整转换图由
§3.5(7 个状态)、§4.4(verdict 判决规则)、§1.6(人工介入显式)推断而来,
推断决策记录见 ``docs/impl/p1_b_03_state_machine.md``。
"""

from typing import Literal

from src.core.state_machine.states import ChapterState

# ----------------------------------------------------------------------------
# 事件枚举(用 Literal 而非 StrEnum,因事件不需要枚举类的额外能力,
# Literal 在静态类型检查时已经能锁定取值范围)
# ----------------------------------------------------------------------------
TransitionEvent = Literal[
    "draft_done",        # 转换 1: drafting → reviewing
    "verdict_pass",      # 转换 2: reviewing → approved
    "verdict_revise",    # 转换 3 或 4: 见函数内分支
    "verdict_rewrite",   # 转换 4: reviewing → rewriting
    "escalate",          # 转换 5: reviewing → human_intervention(条件触发)
    "revise_done",       # 转换 6: revising → reviewing
    "rewrite_done",      # 转换 7: rewriting → reviewing
    "publish",           # 转换 8: approved → published
    "force_escalate",    # 转换 9: 任意非自身 → human_intervention(显式触发)
]

Verdict = Literal["pass", "revise", "rewrite", "escalate_to_human"]


# ----------------------------------------------------------------------------
# 合法转换表(给 orchestrator 与测试共用,只表达"哪些起点+事件组合是合法的",
# 不表达条件分支内的进一步判定;条件判定在 ``transition()`` 函数里)
# ----------------------------------------------------------------------------
LEGAL_TRANSITIONS: dict[tuple[ChapterState, TransitionEvent], ChapterState] = {
    # 转换 1
    (ChapterState.DRAFTING, "draft_done"): ChapterState.REVIEWING,
    # 转换 2
    (ChapterState.REVIEWING, "verdict_pass"): ChapterState.APPROVED,
    # 转换 3 / 4(verdict_revise 的目标依条件分支决定,这里登记两个候选)
    # 实际目标在 transition() 内根据 revise_count vs max_revises 判定。
    # 转换 4(verdict_rewrite 单独成条)
    (ChapterState.REVIEWING, "verdict_rewrite"): ChapterState.REWRITING,
    # 转换 5
    (ChapterState.REVIEWING, "escalate"): ChapterState.HUMAN_INTERVENTION,
    # 转换 6
    (ChapterState.REVISING, "revise_done"): ChapterState.REVIEWING,
    # 转换 7
    (ChapterState.REWRITING, "rewrite_done"): ChapterState.REVIEWING,
    # 转换 8
    (ChapterState.APPROVED, "publish"): ChapterState.PUBLISHED,
}

# 转换 9:force_escalate 适用于"任意非 human_intervention"的所有起始状态。
# 用集合表达,避免在 LEGAL_TRANSITIONS 里重复登记 6 个键。
FORCE_ESCALATE_SOURCES: set[ChapterState] = {
    ChapterState.DRAFTING,
    ChapterState.REVIEWING,
    ChapterState.REWRITING,
    ChapterState.REVISING,
    ChapterState.APPROVED,
    ChapterState.PUBLISHED,
}


def transition(
    current: ChapterState,
    event: TransitionEvent,
    *,
    verdict: Verdict | None = None,
    revise_count: int = 0,
    max_revises: int = 0,
    rewrite_count: int = 0,
    max_rewrites: int = 0,
    force: bool = False,
) -> ChapterState:
    """根据当前状态与事件计算下一状态。

    参数:
        current: 当前状态。
        event: 触发事件。事件名见 ``TransitionEvent``。
        verdict: 仅 ``event="verdict_revise"`` 时有意义,用于参考(实际分支
            判定靠 revise_count/max_revises;此参数留作日志/观测用)。
        revise_count: 已累计的修订次数(用于 verdict_revise 分支判定)。
        max_revises: 修订上限(用于 verdict_revise 分支判定)。
        rewrite_count: 已累计的重写次数(用于 escalate 分支判定)。
        max_rewrites: 重写上限(用于 escalate 分支判定)。
        force: 跳过条件校验、无视合法转换表强制进入指定目标的开关。
            **P1 不实现 force 的实际使用方,仅预留接口**(给未来人工介入恢复机制)。
            P1 内 force=True 仅支持 ``event="force_escalate"`` 强制进入
            HUMAN_INTERVENTION,其他用法在 P6 完整人工介入流程里再扩展。

    返回:
        转换后的状态。

    抛出:
        ValueError: 当转换非法时(P1-B-17 全局错误体系起来后会升级为 ApiError 子类)。
    """
    # --- 转换 9:显式人工介入(任意非自身,且 P1 也允许 force=True 走这条路径) ---
    if event == "force_escalate":
        if current == ChapterState.HUMAN_INTERVENTION:
            raise ValueError(
                f"非法转换:{current.value} 已处于 human_intervention,不允许 force_escalate"
            )
        if current not in FORCE_ESCALATE_SOURCES:
            raise ValueError(
                f"非法转换:force_escalate 不支持起始状态 {current.value}"
            )
        return ChapterState.HUMAN_INTERVENTION

    # --- force=True 的其他用法:P1 不允许,留给 P6 ---
    if force:
        raise ValueError(
            "force=True 仅支持 event='force_escalate'(P1 范围)。"
            "其他强制转换由 P6 完整人工介入流程实现。"
        )

    # --- 转换 3 / 4:verdict_revise 的二分支(必须分别清晰判定) ---
    if event == "verdict_revise":
        if current != ChapterState.REVIEWING:
            raise ValueError(
                f"非法转换:verdict_revise 仅在 reviewing 状态下合法,当前 {current.value}"
            )
        # 触发条件 A:revise 名额还有 → 走转换 3 进 REVISING
        if revise_count < max_revises:
            return ChapterState.REVISING
        # 触发条件 B:revise 名额耗尽 → 走转换 4 强制升 REWRITING
        return ChapterState.REWRITING

    # --- 转换 5:reviewing → human_intervention(条件触发,与转换 9 显式触发区分) ---
    if event == "escalate":
        if current != ChapterState.REVIEWING:
            raise ValueError(
                f"非法转换:escalate 仅在 reviewing 状态下合法(条件触发);"
                f"任意态显式触发请用 event='force_escalate'。当前 {current.value}"
            )
        # 条件:verdict=escalate_to_human,或 rewrite_count 已达上限
        condition_verdict = verdict == "escalate_to_human"
        condition_rewrite_exhausted = rewrite_count >= max_rewrites
        if not (condition_verdict or condition_rewrite_exhausted):
            raise ValueError(
                "非法转换:escalate 触发条件不满足"
                f"(verdict={verdict}, rewrite_count={rewrite_count}, "
                f"max_rewrites={max_rewrites})"
            )
        return ChapterState.HUMAN_INTERVENTION

    # --- 其他事件直接查表 ---
    key = (current, event)
    if key not in LEGAL_TRANSITIONS:
        raise ValueError(
            f"非法转换:状态 {current.value} 不接受事件 {event}"
        )
    return LEGAL_TRANSITIONS[key]
