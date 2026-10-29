"""v1.0.x wave 5 session 4:must_achieve 语义重合检测器对照测试。

覆盖 ``_compute_must_achieve_overlap_ratio`` 在 wave 5 session 4 升级后的
三维 pairwise max 行为(字面 char + 关键名词 + 关键词内容词)。

**对照动机**:wave 4 session 4 立的字面 char Jaccard 漏检 luo3 ch-2/3 语义
重合(都"首次记忆置换",字面 0.2626 < 0.5)。本测保证升级后的检测器在该
真 fixture 上**必须命中** ≥ 0.5。

本测纯函数 / 模块级 helper,不依赖 ChapterCardService / DB / WS,放独立文件
便于将来 v1.1+ 语义检测器再扩维度时此文件作"对照锚点"集中升级。
"""

from __future__ import annotations

import pytest

from src.services.chapter_card_service import (
    _compute_must_achieve_overlap_ratio,
    _extract_char_set,
    _extract_keywords,
    _extract_nouns,
    _jaccard,
)


# luo3 工作区 ch-0002/ch-0003 的 must_achieve 真字面(2026-05-23 取样,
# wave 5 启动期维护人人工标注的问题 3 现场)。
LUO3_CH2_MUST_ACHIEVE = [
    "林衍在辐射与机械守卫的夹击下，成功潜入核心区",
    "林衍窃取量子芯片与单兵外骨骼数据，并遭遇时空乱流",
    "林衍确认古玉信标是穿越的关键锚点，引发首次记忆置换",
]
LUO3_CH3_MUST_ACHIEVE = [
    "林衍携带数据穿越返回秦朝途中遭遇时空乱流反噬",
    "林衍在意识模糊中首次经历剧烈记忆置换，闪过秦宫模糊片段",
    "林衍凭借本能将关键数据植入体内锚点，勉强维持穿越通道",
]


class TestSemanticDetectorLuo3Wave5Session4:
    """plan §s4 验收清单"luo3 ch-2/3 语义检测命中"对照锚点。"""

    def test_luo3_ch2_vs_ch3_pairwise_max_hits_threshold(self) -> None:
        """**核心对照**:wave 4 字面 Jaccard 漏检 → wave 5 三维 pairwise max 命中。

        plan §s4 字面验收:"overlap_ratio ≥ 0.5"。
        """
        ratio = _compute_must_achieve_overlap_ratio(
            LUO3_CH2_MUST_ACHIEVE, LUO3_CH3_MUST_ACHIEVE
        )
        assert ratio >= 0.5, (
            f"语义检测器漏检 luo3 ch-2/3(实测 {ratio:.4f},应 ≥ 0.5);"
            "若回到字面 Jaccard 单维(wave 4 设计)实测 0.2626 漏检即此 bug"
        )

    def test_luo3_ch2_vs_ch3_legacy_char_jaccard_alone_misses(self) -> None:
        """**反向锚点**:仅字面 char Jaccard 维度对 luo3 ch-2/3 < 0.5(漏检)。

        本测的 docstring + assert 共同记录"为何 wave 5 升级三维 pairwise":
        若未来回退到字面单维实现,这条仍会 < 0.5,提醒漏检根因。
        """
        # 直接复用 wave 4 同型实现:拼接 + 字符集 Jaccard
        chars_a = set("".join(LUO3_CH2_MUST_ACHIEVE))
        chars_b = set("".join(LUO3_CH3_MUST_ACHIEVE))
        legacy_ratio = len(chars_a & chars_b) / len(chars_a | chars_b)
        assert legacy_ratio < 0.5, (
            f"字面 char Jaccard 单维实测 {legacy_ratio:.4f},预期 < 0.5 "
            "(luo3 ch-2/3 真实采样,体现 wave 4 漏检场景)"
        )


class TestExtractorsHelpers:
    """三维度 token extractor 与 Jaccard helper 单元覆盖。"""

    def test_extract_nouns_picks_chinese_nouns_proper_nouns_v_n(self) -> None:
        """jieba.posseg 抽 n/nr/vn 等标签的内容词。"""
        nouns = _extract_nouns("林衍窃取量子芯片与单兵外骨骼数据")
        # 至少含人名 林衍 + 名词 数据 / 芯片 之一
        assert "林衍" in nouns
        assert nouns & {"数据", "芯片", "量子", "外骨骼", "单兵"}

    def test_extract_keywords_superset_of_nouns(self) -> None:
        """关键词集合 ⊇ 名词集合(关键词 = 名词 ∪ 动词类 ∪ 形容词类 ∪ 数词)。"""
        text = "林衍迅速完成首次记忆置换"
        nouns = _extract_nouns(text)
        keywords = _extract_keywords(text)
        assert nouns.issubset(keywords)
        # 关键词应额外含动词(完成 / 置换)或形容词(迅速)
        assert keywords - nouns

    def test_extract_char_set_pure_char_dedup(self) -> None:
        """字面字符集 = unique chars(无去标点逻辑,Jaccard 自然处理)。"""
        chars = _extract_char_set("AABB")
        assert chars == frozenset({"A", "B"})

    def test_jaccard_empty_returns_zero(self) -> None:
        """两侧均空 frozenset → 0.0(避空集除零)。"""
        assert _jaccard(frozenset(), frozenset()) == 0.0

    def test_jaccard_identical_returns_one(self) -> None:
        assert _jaccard(frozenset({"a", "b"}), frozenset({"a", "b"})) == 1.0

    def test_jaccard_partial(self) -> None:
        ratio = _jaccard(frozenset({"a", "b"}), frozenset({"b", "c"}))
        assert ratio == pytest.approx(1 / 3)


class TestPairwiseMaxBehavior:
    """``_compute_must_achieve_overlap_ratio`` 升级后 pairwise max 行为。"""

    def test_single_pair_identical_returns_one(self) -> None:
        """同字面单条 — 三维都满 1.0,early-return 命中。"""
        assert _compute_must_achieve_overlap_ratio(["林衍踏入山门"], ["林衍踏入山门"]) == 1.0

    def test_pairwise_max_dominates_over_aggregation(self) -> None:
        """逐条 pairwise 比"全池聚合"更敏感:

        构造 a 全 3 条,其中 a[1] 与 b[1] 高度相同;池聚合会被其他低相似条目稀释,
        pairwise max 仍命中。
        """
        a = [
            "毫不相关的角色背景描述一段一段",
            "林衍首次完成记忆置换",
            "另一段散场对话",
        ]
        b = [
            "其他角色无关动作",
            "林衍首次完成记忆置换",
            "完全不同的场景结束",
        ]
        ratio = _compute_must_achieve_overlap_ratio(a, b)
        assert ratio == 1.0  # a[1] vs b[1] 完全同字面

    def test_empty_either_side_returns_zero(self) -> None:
        assert _compute_must_achieve_overlap_ratio([], ["x"]) == 0.0
        assert _compute_must_achieve_overlap_ratio(["x"], []) == 0.0
        assert _compute_must_achieve_overlap_ratio([], []) == 0.0

    def test_disjoint_chinese_subject_returns_below_threshold(self) -> None:
        """完全不同主题 — 三维 max 远低于 0.5。"""
        ratio = _compute_must_achieve_overlap_ratio(
            ["林衍踏入山门"], ["王沧海潜入丹道阁"]
        )
        assert ratio < 0.5

    def test_synonymous_topic_different_wording_hits_threshold(self) -> None:
        """同主题不同字面 — 关键名词维度命中。

        两侧共享:"林衍"(人名)+ "时空"+"乱流"+"穿越" 等核心名词。
        """
        ratio = _compute_must_achieve_overlap_ratio(
            ["林衍遭遇时空乱流穿越"],
            ["林衍穿越时空乱流"],
        )
        # 同主题不同字面 — 至少名词或关键词维度命中
        assert ratio >= 0.5
