"""状态机测试。

覆盖:
- 9 条合法转换(其中转换 4 的两种触发路径分别独立测试)
- 典型非法转换
- force=True 接口预留
- 与 ``Chapter.status`` Literal 字面量值的一致性
"""

import pytest

from src.core.state_machine.states import ChapterState
from src.core.state_machine.transitions import (
    FORCE_ESCALATE_SOURCES,
    LEGAL_TRANSITIONS,
    transition,
)


class TestChapterStateEnum:
    """枚举值与 Chapter.status Literal 字面量必须完全一致。"""

    def test_enum_values_match_chapter_status_literal(self) -> None:
        # Chapter.status Literal:P2-B-17 起 7 档,P6-B-01 扩 ``invalidated``
        # 第 8 档(对齐 ``CHAPTER_STATUS_VALUES`` 单一真相源)。
        expected = {
            "drafting",
            "reviewing",
            "rewriting",
            "revising",
            "approved",
            "published",
            "human_intervention",
            "invalidated",
        }
        actual = {state.value for state in ChapterState}
        assert actual == expected

    def test_strenum_equals_string(self) -> None:
        # StrEnum 的关键收益:可与字符串字面量直接比较,Pydantic 字段互操作零成本
        assert ChapterState.DRAFTING == "drafting"
        assert ChapterState.HUMAN_INTERVENTION == "human_intervention"


class TestLegalTransitions:
    """9 条合法转换逐一覆盖。"""

    # ---- 转换 1: drafting → reviewing ----
    def test_transition_1_drafting_to_reviewing(self) -> None:
        assert (
            transition(ChapterState.DRAFTING, "draft_done")
            == ChapterState.REVIEWING
        )

    # ---- 转换 2: reviewing → approved ----
    def test_transition_2_reviewing_to_approved(self) -> None:
        assert (
            transition(ChapterState.REVIEWING, "verdict_pass")
            == ChapterState.APPROVED
        )

    # ---- 转换 3: reviewing → revising(条件 A:revise 名额还有) ----
    def test_transition_3_reviewing_to_revising_with_quota(self) -> None:
        result = transition(
            ChapterState.REVIEWING,
            "verdict_revise",
            verdict="revise",
            revise_count=0,
            max_revises=2,
        )
        assert result == ChapterState.REVISING

    # ---- 转换 4 触发条件 A:verdict=rewrite ----
    def test_transition_4_path_a_verdict_rewrite(self) -> None:
        """触发路径 A:Reviewer 直接判 rewrite。"""
        result = transition(ChapterState.REVIEWING, "verdict_rewrite")
        assert result == ChapterState.REWRITING

    # ---- 转换 4 触发条件 B:verdict=revise 但 revise_count >= max_revises ----
    def test_transition_4_path_b_revise_quota_exhausted(self) -> None:
        """触发路径 B:revise 名额耗尽,降级为强制重写。

        验证"虽然到达同一目标状态(REWRITING),但触发路径不同"——本用例事件
        是 ``verdict_revise``,与上一用例的 ``verdict_rewrite`` 形成显式区分。
        """
        result = transition(
            ChapterState.REVIEWING,
            "verdict_revise",
            verdict="revise",
            revise_count=2,
            max_revises=2,
        )
        assert result == ChapterState.REWRITING

    def test_transition_4_two_paths_reach_same_target_via_different_events(
        self,
    ) -> None:
        """显式断言两条路径共目标、异事件。"""
        path_a = transition(ChapterState.REVIEWING, "verdict_rewrite")
        path_b = transition(
            ChapterState.REVIEWING,
            "verdict_revise",
            verdict="revise",
            revise_count=5,
            max_revises=2,
        )
        assert path_a == path_b == ChapterState.REWRITING

    # ---- 转换 5: reviewing → human_intervention(条件触发) ----
    def test_transition_5_escalate_via_verdict(self) -> None:
        result = transition(
            ChapterState.REVIEWING,
            "escalate",
            verdict="escalate_to_human",
            rewrite_count=0,
            max_rewrites=3,
        )
        assert result == ChapterState.HUMAN_INTERVENTION

    def test_transition_5_escalate_via_rewrite_exhausted(self) -> None:
        result = transition(
            ChapterState.REVIEWING,
            "escalate",
            verdict="rewrite",
            rewrite_count=3,
            max_rewrites=3,
        )
        assert result == ChapterState.HUMAN_INTERVENTION

    # ---- 转换 6: revising → reviewing ----
    def test_transition_6_revising_to_reviewing(self) -> None:
        assert (
            transition(ChapterState.REVISING, "revise_done")
            == ChapterState.REVIEWING
        )

    # ---- 转换 7: rewriting → reviewing ----
    def test_transition_7_rewriting_to_reviewing(self) -> None:
        assert (
            transition(ChapterState.REWRITING, "rewrite_done")
            == ChapterState.REVIEWING
        )

    # ---- 转换 8: approved → published ----
    def test_transition_8_approved_to_published(self) -> None:
        assert (
            transition(ChapterState.APPROVED, "publish")
            == ChapterState.PUBLISHED
        )

    # ---- 转换 9: 任意非 human_intervention → human_intervention(显式触发) ----
    @pytest.mark.parametrize("source", list(FORCE_ESCALATE_SOURCES))
    def test_transition_9_force_escalate_from_any_source(
        self, source: ChapterState
    ) -> None:
        """覆盖全部 6 个非 human_intervention 起点。"""
        result = transition(source, "force_escalate")
        assert result == ChapterState.HUMAN_INTERVENTION


class TestIllegalTransitions:
    """典型非法转换:跨状态跳跃、终态出向、人工介入自动出向。"""

    def test_drafting_cannot_jump_to_approved(self) -> None:
        with pytest.raises(ValueError, match="非法转换"):
            transition(ChapterState.DRAFTING, "verdict_pass")

    def test_drafting_cannot_jump_to_rewriting(self) -> None:
        with pytest.raises(ValueError, match="非法转换"):
            transition(ChapterState.DRAFTING, "verdict_rewrite")

    def test_drafting_cannot_jump_to_revising(self) -> None:
        with pytest.raises(ValueError, match="verdict_revise 仅在 reviewing"):
            transition(
                ChapterState.DRAFTING,
                "verdict_revise",
                revise_count=0,
                max_revises=2,
            )

    def test_reviewing_cannot_skip_to_published(self) -> None:
        with pytest.raises(ValueError, match="非法转换"):
            transition(ChapterState.REVIEWING, "publish")

    def test_approved_cannot_revert_to_drafting(self) -> None:
        with pytest.raises(ValueError, match="非法转换"):
            transition(ChapterState.APPROVED, "draft_done")

    def test_approved_cannot_be_reviewed_again(self) -> None:
        with pytest.raises(ValueError, match="非法转换"):
            transition(ChapterState.APPROVED, "verdict_pass")

    def test_published_is_terminal(self) -> None:
        with pytest.raises(ValueError, match="非法转换"):
            transition(ChapterState.PUBLISHED, "publish")
        with pytest.raises(ValueError, match="非法转换"):
            transition(ChapterState.PUBLISHED, "draft_done")

    def test_human_intervention_has_no_auto_exit(self) -> None:
        """P1 内人工介入态不允许任何自动转出。"""
        with pytest.raises(ValueError, match="非法转换"):
            transition(ChapterState.HUMAN_INTERVENTION, "draft_done")
        with pytest.raises(ValueError, match="非法转换"):
            transition(ChapterState.HUMAN_INTERVENTION, "verdict_pass")

    def test_human_intervention_rejects_force_escalate(self) -> None:
        """显式触发也不允许从 human_intervention 自指。"""
        with pytest.raises(ValueError, match="已处于 human_intervention"):
            transition(ChapterState.HUMAN_INTERVENTION, "force_escalate")

    def test_escalate_condition_not_met_raises(self) -> None:
        """escalate 条件触发但条件不满足时必须拒绝。"""
        with pytest.raises(ValueError, match="触发条件不满足"):
            transition(
                ChapterState.REVIEWING,
                "escalate",
                verdict="revise",
                rewrite_count=1,
                max_rewrites=3,
            )

    def test_escalate_only_from_reviewing(self) -> None:
        """条件触发的 escalate 只在 reviewing 合法,其他态请用 force_escalate。"""
        with pytest.raises(ValueError, match="仅在 reviewing"):
            transition(
                ChapterState.DRAFTING,
                "escalate",
                verdict="escalate_to_human",
            )


class TestForceParameter:
    """force=True 的接口预留行为。"""

    def test_force_escalate_works_without_force_flag(self) -> None:
        """force_escalate 事件本身就是显式逃生路径,默认 force=False 即可工作。"""
        result = transition(ChapterState.DRAFTING, "force_escalate")
        assert result == ChapterState.HUMAN_INTERVENTION

    def test_force_true_with_force_escalate_event_works(self) -> None:
        """P1 内 force=True 仅支持 event='force_escalate'。"""
        result = transition(
            ChapterState.REVIEWING,
            "force_escalate",
            force=True,
        )
        assert result == ChapterState.HUMAN_INTERVENTION

    def test_force_true_with_other_events_rejected_in_p1(self) -> None:
        """P1 不允许 force=True 配其他事件,留给 P6。"""
        with pytest.raises(ValueError, match="P6"):
            transition(
                ChapterState.APPROVED,
                "draft_done",
                force=True,
            )


class TestLegalTransitionsTable:
    """暴露给 orchestrator 用的 LEGAL_TRANSITIONS 常量结构稳定性。"""

    def test_legal_transitions_table_size(self) -> None:
        """登记 7 条静态查表项(转换 1/2/4/5/6/7/8;转换 3 与 9 走条件分支)。"""
        assert len(LEGAL_TRANSITIONS) == 7

    def test_force_escalate_sources_excludes_human_intervention(self) -> None:
        assert ChapterState.HUMAN_INTERVENTION not in FORCE_ESCALATE_SOURCES
        assert len(FORCE_ESCALATE_SOURCES) == 6
