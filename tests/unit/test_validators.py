"""``src.core.rules.validators`` 4 类节奏类 [HARD] validator 单元测(P4-B-06)。

每类 validator 至少 1 通过 + 1 失败用例(启动指令字面"4 类规则 × 通过/失败 8
用例")。``check_word_count`` 的失败路径再细分上下界,共 9 用例。
"""

from __future__ import annotations

from src.core.rules.validators import (
    check_forbidden,
    check_must_achieve,
    check_must_unresolved,
    check_word_count,
)


# ---------------------------------------------------------------------------
# 1. 字数(word_count)
# ---------------------------------------------------------------------------


class TestCheckWordCount:
    def test_word_count_passes_when_in_range(self) -> None:
        text = "字" * 3500
        result = check_word_count(text, word_count_min=2800, word_count_max=4500)
        assert result.passed
        assert result.name == "word_count"
        assert result.details == {"actual_word_count": 3500}

    def test_word_count_fails_below_min(self) -> None:
        text = "字" * 1000
        result = check_word_count(text, word_count_min=2800, word_count_max=4500)
        assert not result.passed
        assert result.details["actual_word_count"] == 1000
        assert result.details["reason"] == "below_min"

    def test_word_count_fails_above_max(self) -> None:
        text = "字" * 6000
        result = check_word_count(text, word_count_min=2800, word_count_max=4500)
        assert not result.passed
        assert result.details["actual_word_count"] == 6000
        assert result.details["reason"] == "above_max"

    def test_hidden_tolerance_accepts_exact_ten_percent_edges(self) -> None:
        lower = check_word_count(
            "字" * 2250, word_count_min=2500, word_count_max=4500
        )
        upper = check_word_count(
            "字" * 4950, word_count_min=2500, word_count_max=4500
        )
        assert lower.passed and lower.details == {"actual_word_count": 2250}
        assert upper.passed and upper.details == {"actual_word_count": 4950}

    def test_hidden_tolerance_rejects_one_character_outside(self) -> None:
        lower = check_word_count(
            "字" * 2249, word_count_min=2500, word_count_max=4500
        )
        upper = check_word_count(
            "字" * 4951, word_count_min=2500, word_count_max=4500
        )
        assert not lower.passed and lower.details["reason"] == "below_min"
        assert not upper.passed and upper.details["reason"] == "above_max"
        assert "acceptance_min" not in lower.details
        assert "acceptance_max" not in upper.details
        assert "acceptance_band" not in lower.details

    def test_hidden_tolerance_rounds_inward_for_non_round_targets(self) -> None:
        lower = check_word_count(
            "字" * 91, word_count_min=101, word_count_max=101
        )
        upper = check_word_count(
            "字" * 111, word_count_min=101, word_count_max=101
        )
        too_low = check_word_count(
            "字" * 90, word_count_min=101, word_count_max=101
        )
        too_high = check_word_count(
            "字" * 112, word_count_min=101, word_count_max=101
        )
        assert lower.passed and upper.passed
        assert not too_low.passed and not too_high.passed


# ---------------------------------------------------------------------------
# 2. must_achieve
# ---------------------------------------------------------------------------


class TestCheckMustAchieve:
    def test_must_achieve_passes_keyword_match(self) -> None:
        text = "林衍突破第一道剑阵后,推开石门,看到苏离消失前留下的玉佩。"
        result = check_must_achieve(
            text, must_achieve=["林衍突破第一道剑阵", "推开石门"]
        )
        assert result.passed
        assert result.details["items_total"] == 2
        assert result.details["items_missing"] == []
        assert "llm_fallback_used" not in result.details

    def test_must_achieve_fails_keyword_miss_strict_mode(self) -> None:
        # P4-B-06 严格模式:llm_fallback=None 时关键词 miss 直接判失败,不放行。
        text = "林衍并未突破剑阵,反而被卷入阵中。"
        result = check_must_achieve(
            text,
            must_achieve=["林衍突破第一道剑阵"],
        )
        assert not result.passed
        assert result.details["items_missing"] == ["林衍突破第一道剑阵"]

    def test_must_achieve_passes_when_llm_fallback_returns_true(self) -> None:
        # 关键词 miss → LLM 兜底显式返 True(LLM 判定语义满足)→ 整体通过。
        text = "他踏破阵眼,化解了第一道考验。"
        result = check_must_achieve(
            text,
            must_achieve=["林衍突破第一道剑阵"],
            llm_fallback=lambda item, body: True,
        )
        assert result.passed
        assert result.details["items_missing"] == []
        assert result.details["llm_fallback_used"] == ["林衍突破第一道剑阵"]


# ---------------------------------------------------------------------------
# 3. must_unresolved
# ---------------------------------------------------------------------------


class TestCheckMustUnresolved:
    def test_must_unresolved_passes_when_no_resolution_keywords(self) -> None:
        text = "阵眼真凶身份依然成谜,玉佩在掌心微微发烫,他还没找到任何线索。"
        result = check_must_unresolved(
            text, must_unresolved=["阵眼真凶身份"]
        )
        assert result.passed
        assert result.details["items_total"] == 1
        assert result.details["items_resolved"] == []

    def test_must_unresolved_fails_strict_mode_when_resolution_keyword_in_window(
        self,
    ) -> None:
        # "阵眼真凶身份" 周围 80 字符内出现 "真相大白" → 启发式 hit;严格模式
        # (llm_fallback=None)直接判该项失败。
        text = "阵眼真凶身份此刻真相大白,他终于看清楚那双眼睛。"
        result = check_must_unresolved(
            text,
            must_unresolved=["阵眼真凶身份"],
        )
        assert not result.passed
        assert result.details["items_resolved"] == ["阵眼真凶身份"]

    def test_must_unresolved_passes_when_llm_fallback_returns_true(self) -> None:
        # 启发式 hit + LLM 兜底返 True(语义层确认仍未解决,启发式误判)→ 通过。
        text = "阵眼真凶身份此刻真相大白(此处误判),他终于看清楚那双眼睛。"
        result = check_must_unresolved(
            text,
            must_unresolved=["阵眼真凶身份"],
            llm_fallback=lambda item, body: True,  # 仍未解决
        )
        assert result.passed
        assert result.details["items_resolved"] == []
        assert result.details["llm_fallback_used"] == ["阵眼真凶身份"]


# ---------------------------------------------------------------------------
# 4. forbidden
# ---------------------------------------------------------------------------


class TestCheckForbidden:
    def test_forbidden_passes_when_no_hits(self) -> None:
        text = "他握紧剑柄,目光不离对面那道身影。"
        result = check_forbidden(text, forbidden=["现代科技元素", "手机"])
        assert result.passed
        assert result.details == {
            "items_total": 2,
            "forbidden_hits": [],
        }

    def test_forbidden_fails_when_phrase_present(self) -> None:
        text = "他从背包里掏出手机查看消息,目光不离对面那道身影。"
        result = check_forbidden(text, forbidden=["现代科技元素", "手机"])
        assert not result.passed
        assert result.details["forbidden_hits"] == ["手机"]


# ---------------------------------------------------------------------------
# 5. 边界(空列表 / LLM fallback 异常 swallow)
# ---------------------------------------------------------------------------


class TestEdgeCases:
    def test_empty_lists_all_pass(self) -> None:
        text = "随便写点。"
        assert check_must_achieve(text, must_achieve=[]).passed
        assert check_must_unresolved(text, must_unresolved=[]).passed
        assert check_forbidden(text, forbidden=[]).passed

    def test_must_achieve_llm_fallback_exception_swallowed(self) -> None:
        # LLM 兜底抛异常 → swallow + 保守判定通过(对齐 P3-B-13 swallow 模式)。
        def _raise(item: str, body: str) -> bool:
            raise RuntimeError("LLM fallback failed")

        text = "正文中没有这个 phrase。"
        result = check_must_achieve(
            text,
            must_achieve=["关键事件"],
            llm_fallback=_raise,
        )
        assert result.passed
        assert result.details["items_missing"] == []
        assert result.details["llm_fallback_used"] == ["关键事件"]
