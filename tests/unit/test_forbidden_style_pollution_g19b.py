"""G19b chapter_card_generator.py `_detect_forbidden_style_pollution` 测试
(v1.1 audit G19b 治本,2026-05-27 b1-rerun-deep-audit)。
"""

from __future__ import annotations

from src.core.agents.chapter_card_generator import _detect_forbidden_style_pollution


class TestG19bCleanCase:
    """纯剧情禁元素 → 空污染列表。"""

    def test_empty_forbidden_is_clean(self) -> None:
        assert _detect_forbidden_style_pollution([]) == []

    def test_pure_drama_elements_clean(self) -> None:
        """跨域穿帮 / 现代用语 / 越界角色 → 通过。"""
        forbidden = [
            "现代用语",
            "现代科技产品(若题材是古代)",
            "禁止角色张三出场",
            "禁止揭示主角真实身份",
        ]
        assert _detect_forbidden_style_pollution(forbidden) == []

    def test_voice_forbidden_phrases_clean(self) -> None:
        """voice_profile 派生的禁词(无风格类语义)→ 通过。"""
        forbidden = [
            "角色 A 自称'本王'",
            "角色 B 使用江湖黑话",
        ]
        assert _detect_forbidden_style_pollution(forbidden) == []


class TestG19bSentenceLengthPollution:
    """句长 / 字数类约束污染检测。"""

    def test_long_sentence_stacking(self) -> None:
        """ch-7 现场实例:超过 40 字的长句堆叠。"""
        polluted = _detect_forbidden_style_pollution([
            "超过 40 字的长句堆叠",
            "现代用语",
        ])
        assert polluted == ["超过 40 字的长句堆叠"]

    def test_sentence_over_chars(self) -> None:
        """各种字数关键字命中。"""
        for entry in (
            "句子过长",
            "句子过短",
            "单句过长",
        ):
            polluted = _detect_forbidden_style_pollution([entry])
            assert polluted == [entry], f"{entry} 未被检测"


class TestG19bParagraphCountPollution:
    """段落数 / 段落长度类约束污染检测。"""

    def test_three_paragraphs_inner_monologue(self) -> None:
        """ch-1/ch-3 现场实例:超过三段的纯心理独白。"""
        polluted = _detect_forbidden_style_pollution([
            "超过三段的纯心理独白堆砌",
            "现代用语",
        ])
        assert polluted == ["超过三段的纯心理独白堆砌"]

    def test_consecutive_paragraphs(self) -> None:
        """连续超过三段的纯心理独白(ch-3 实例)。"""
        polluted = _detect_forbidden_style_pollution([
            "连续超过三段的纯心理独白",
        ])
        assert polluted == ["连续超过三段的纯心理独白"]

    def test_paragraph_too_long_short(self) -> None:
        for entry in (
            "段落过长",
            "段落过短",
            "单段过长",
        ):
            polluted = _detect_forbidden_style_pollution([entry])
            assert polluted == [entry]


class TestG19bRhetoricPollution:
    """修辞密度类约束污染检测。"""

    def test_excessive_simile(self) -> None:
        polluted = _detect_forbidden_style_pollution(["大量比喻和排比修辞"])
        assert polluted == ["大量比喻和排比修辞"]

    def test_rhetoric_dense(self) -> None:
        polluted = _detect_forbidden_style_pollution(["修辞密集到拖累节奏"])
        assert polluted == ["修辞密集到拖累节奏"]


class TestG19bRhythmPollution:
    """节奏类约束污染检测。"""

    def test_rhythm_dragging(self) -> None:
        polluted = _detect_forbidden_style_pollution(["叙事节奏拖沓"])
        assert polluted == ["叙事节奏拖沓"]

    def test_rhythm_too_fast(self) -> None:
        polluted = _detect_forbidden_style_pollution(["节奏过快导致信息密度失控"])
        assert polluted == ["节奏过快导致信息密度失控"]


class TestG19bInformationDensityPollution:
    """信息密度类约束污染检测(ch-4/ch-5 现场)。"""

    def test_data_listing(self) -> None:
        """ch-5 现场实例:大段枯燥的财务数据罗列。"""
        polluted = _detect_forbidden_style_pollution([
            "大段枯燥的财务数据罗列导致叙事拖沓",
        ])
        # 命中 '大段堆砌'? 应该命中 '罗列' / '枯燥的' / '大段铺陈' 等关键字
        assert len(polluted) == 1

    def test_police_documents_stacking(self) -> None:
        """ch-4 现场实例:大段堆砌枯燥的警方办案文书细节。"""
        polluted = _detect_forbidden_style_pollution([
            "大段堆砌枯燥的警方办案程序与文书细节",
        ])
        assert len(polluted) == 1


class TestG19bMultiplePollutions:
    """多条污染条目同时命中。"""

    def test_mixed_clean_and_polluted(self) -> None:
        polluted = _detect_forbidden_style_pollution([
            "现代用语",  # clean
            "超过 40 字的长句堆叠",  # polluted
            "禁止揭示真凶",  # clean
            "连续三段的心理独白",  # polluted(命中"三段的")
        ])
        assert len(polluted) == 2
        assert "超过 40 字的长句堆叠" in polluted
        assert "连续三段的心理独白" in polluted
