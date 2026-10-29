"""``GenerationConfig`` 模型单元测试(v1.1 wave 3 session 3 / L1 Q16)。

覆盖:
- 默认值对齐 ChapterCard 单章字段(2500 / 4000 / 3)
- 交叉校验 ``word_count_min <= word_count_max``
- 字段范围(word_count >= 1 / max_rewrites >= 0)
- extra=forbid
"""

from __future__ import annotations

import pytest
from pydantic import ValidationError

from src.core.models.generation_config import GenerationConfig


def test_defaults_align_chapter_card() -> None:
    cfg = GenerationConfig()
    assert cfg.word_count_min == 2500
    assert cfg.word_count_max == 4000
    assert cfg.max_rewrites == 3


def test_equal_min_max_allowed() -> None:
    """min == max 合法(闭区间)。"""
    cfg = GenerationConfig(word_count_min=3000, word_count_max=3000)
    assert cfg.word_count_min == cfg.word_count_max == 3000


def test_min_greater_than_max_raises() -> None:
    with pytest.raises(ValidationError, match="word_count_min 不能大于 word_count_max"):
        GenerationConfig(word_count_min=5000, word_count_max=3000)


def test_word_count_below_one_raises() -> None:
    with pytest.raises(ValidationError):
        GenerationConfig(word_count_min=0)


def test_max_rewrites_negative_raises() -> None:
    with pytest.raises(ValidationError):
        GenerationConfig(max_rewrites=-1)


def test_max_rewrites_zero_allowed() -> None:
    """max_rewrites=0 合法(不重写)。"""
    assert GenerationConfig(max_rewrites=0).max_rewrites == 0


def test_extra_field_forbidden() -> None:
    with pytest.raises(ValidationError):
        GenerationConfig(unknown_field=1)  # type: ignore[call-arg]


class TestGenreMechanismConfigS10:
    """模块①②(v1.1 wave 5 s10):两开关字段 + genre_mechanism 门控解析防回退。"""

    def test_new_toggles_default_auto(self) -> None:
        cfg = GenerationConfig()
        assert cfg.progression_consistency == "auto"
        assert cfg.breakthrough_payoff == "auto"

    def test_toggles_accept_on_off(self) -> None:
        cfg = GenerationConfig(progression_consistency="on", breakthrough_payoff="off")
        assert cfg.progression_consistency == "on"
        assert cfg.breakthrough_payoff == "off"

    def test_toggle_invalid_raises(self) -> None:
        with pytest.raises(ValidationError):
            GenerationConfig(progression_consistency="maybe")  # type: ignore[arg-type]

    def test_consistency_gating_by_genre(self) -> None:
        from src.core.models.genre_mechanism import resolve_progression_consistency as r
        # 古典仙侠 auto→ON;爽文/热血 auto→OFF(防误报);非 3 题材一律 OFF(防外溢)。
        assert r(style_template="classical_xianxia", mode="auto") is True
        assert r(style_template="comedic_burst", mode="auto") is False
        assert r(style_template="heroic_battle", mode="auto") is False
        assert r(style_template="comedic_burst", mode="on") is True  # 显式覆盖
        assert r(style_template="healing_daily", mode="on") is False  # 非 3 题材防外溢
        assert r(style_template=None, mode="auto") is False

    def test_breakthrough_gating_by_genre(self) -> None:
        from src.core.models.genre_mechanism import resolve_breakthrough_payoff as r
        # 3 题材 auto→全 ON(升级流核心);其余 OFF。
        assert r(style_template="classical_xianxia", mode="auto") is True
        assert r(style_template="comedic_burst", mode="auto") is True
        assert r(style_template="heroic_battle", mode="auto") is True
        assert r(style_template="dark_mystery", mode="auto") is False
        assert r(style_template="heroic_battle", mode="off") is False  # 显式关
        assert r(style_template="grand_epic", mode="on") is False  # 非 3 题材防外溢

    def test_progression_label_by_genre(self) -> None:
        from src.core.models.genre_mechanism import progression_label as lbl
        assert lbl("classical_xianxia") == "境界"
        assert lbl("heroic_battle") == "战力"
        assert lbl("comedic_burst") == "境界"
        assert lbl(None) == "境界"  # 中性兜底


class TestStyleAnchorSourceW2S2:
    """v2 w2-s2 注入端 A/B 开关字段(generation.style_anchor_source)。"""

    def test_default_is_prev_chapter(self) -> None:
        # v2 间隙(2026-06-13)回退默认为 prev_chapter:luo-accept-v2 30 章实证
        # 全书锚要治的漂移病没出现(章号→段均 r=0.149)+ 段长由张力驱动
        # (张力→段均 r=−0.487)+ 全书锚经 G17 在 rewrite 路径把高潮章强制往长
        # (HARD 致伤,确定性证明)→ 回退干净现状,全书级锚留 wave 4 以张力感知锚重做。
        assert GenerationConfig().style_anchor_source == "prev_chapter"

    def test_prev_chapter_accepted(self) -> None:
        cfg = GenerationConfig(style_anchor_source="prev_chapter")
        assert cfg.style_anchor_source == "prev_chapter"

    def test_invalid_value_rejected(self) -> None:
        with pytest.raises(ValidationError):
            GenerationConfig(style_anchor_source="both")


class TestMergeGenerationWhitelistW2S2:
    """v2 w2-s2:``_merge_generation`` 白名单完整性防回退。

    根因现场:w5-s10 的 progression_consistency / breakthrough_payoff 加进
    GenerationConfig 但漏登合并白名单 → project.yaml / L1 UI 工作区覆盖被
    静默丢弃(恒走引擎默认)。本组锚"白名单 = model_fields 全集"——未来加
    字段忘登白名单时 test_whitelist_covers_all_model_fields 直接挂。
    """

    def test_w5s10_fields_override_now_effective(self) -> None:
        from src.services.config_service import _merge_generation

        merged = _merge_generation(
            GenerationConfig(),
            {
                "progression_consistency": "on",
                "breakthrough_payoff": "off",
                "style_anchor_source": "book_anchor",
            },
        )
        assert merged.progression_consistency == "on"
        assert merged.breakthrough_payoff == "off"
        assert merged.style_anchor_source == "book_anchor"

    def test_none_values_still_mean_keep_engine_default(self) -> None:
        from src.services.config_service import _merge_generation

        merged = _merge_generation(
            GenerationConfig(), {"style_anchor_source": None}
        )
        assert merged.style_anchor_source == "prev_chapter"  # v2 间隙回退引擎默认

    def test_whitelist_covers_all_model_fields(self) -> None:
        """结构断言:对每个 GenerationConfig 字段构造非默认覆盖值,经
        _merge_generation 后必须生效 — 白名单漏登任一字段本测试即挂。"""
        from src.services.config_service import _merge_generation

        probe_overrides = {
            "word_count_policy_version": 1,
            "word_count_min": 1111,
            "word_count_max": 9999,
            "max_rewrites": 7,
            "plot_node_budget": 9,
            "core_state_hard_enabled": False,
            "progression_consistency": "on",
            "breakthrough_payoff": "on",
            "style_anchor_source": "book_anchor",
            "anti_tic_trim_enabled": True,  # v2 W3.5 wave-3(默认 False → 非默认探针 True)
            "word_count_trim_enabled": True,  # v2 W3.5-s3rev(默认 False → 非默认探针 True)
            "outline_autofix_enabled": False,  # v2 W3.5-s4-step1(默认 True → 非默认探针 False)
            "character_backfill_enabled": False,  # v2 W3.5-s4-step1 机制1(默认 True → 非默认探针 False)
        }
        assert set(probe_overrides) == set(GenerationConfig.model_fields), (
            "GenerationConfig 字段演进后请同步本测试 probe 表 + "
            "_merge_generation 白名单(两处一并改)"
        )
        merged = _merge_generation(GenerationConfig(), probe_overrides)
        for field, expected in probe_overrides.items():
            assert getattr(merged, field) == expected, (
                f"字段 {field} 的工作区覆盖被 _merge_generation 白名单丢弃"
            )
