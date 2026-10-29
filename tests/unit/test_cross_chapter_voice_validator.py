"""v1.1 wave 1 session 6 / G10:cross_chapter_voice_validator 单元测试。

覆盖:
- happy path(passed)
- 主导人称翻转 HARD；代词频率倍率与段落漂移为 WARN-only 观察信号
- 跨章主视角角色变化不豁免叙述人称；setting / mini_plan 明示章内例外仍豁免
- 首章 default_voice 路径(第三人称硬约束)
- free 模式 / baseline 缺失 fail-open / 短长文本兜底
- luo-clean-v1 ch-6 现场重演(third → first 主导对换)
"""

from __future__ import annotations

from datetime import UTC, datetime
from typing import Literal

import pytest

from src.core.models.chapter_card import ChapterCard, ContinuityContext
from src.core.rules.cross_chapter_voice_validator import (
    MIN_CHARS_FOR_VOICE_CHECK,
    check_cross_chapter_voice,
)


# ---- fingerprint fixtures ------------------------------------------------


def _make_fingerprint(
    *,
    pov: Literal["third_person", "first_person", "mixed"] = "third_person",
    paragraph_mean: float = 100.0,
    total_chars: float = 3000.0,
    extra_pronoun_overrides: dict[str, float] | None = None,
) -> dict[str, float]:
    """构造一份典型 fingerprint(17 字段),pov 决定代词频次分布。"""
    fp: dict[str, float] = {
        "paragraph_length_mean": paragraph_mean,
        "paragraph_length_std": 30.0,
        "sentence_length_mean": 25.0,
        "sentence_length_std": 10.0,
        "simile_density": 0.05,
        "exclamation_density": 0.02,
        "total_chars": total_chars,
        "total_sentences": 120.0,
        # 默认所有代词 0,按 pov 填
        "pronoun_freq_我": 0.0,
        "pronoun_freq_你": 0.001,
        "pronoun_freq_他": 0.0,
        "pronoun_freq_她": 0.0,
        "pronoun_freq_它": 0.001,
        "pronoun_freq_我们": 0.0,
        "pronoun_freq_你们": 0.0,
        "pronoun_freq_他们": 0.0,
        "pronoun_freq_她们": 0.0,
    }
    if pov == "third_person":
        # third dominant: 他/她 高,我 0
        fp["pronoun_freq_他"] = 0.015
        fp["pronoun_freq_她"] = 0.008
    elif pov == "first_person":
        # first dominant: 我 高,他/她 极低
        fp["pronoun_freq_我"] = 0.025
        fp["pronoun_freq_他"] = 0.001
    elif pov == "mixed":
        # both moderate
        fp["pronoun_freq_我"] = 0.010
        fp["pronoun_freq_他"] = 0.012
    if extra_pronoun_overrides:
        fp.update(extra_pronoun_overrides)
    return fp


def _make_chapter_card(
    *,
    chapter_id: str = "ch-0002",
    setting: str = "现代心理诊所候诊室",
    pov_change: bool = False,
    pov_from: str | None = "ch1_pov",
) -> ChapterCard:
    """构造一份最小 ChapterCard(用于例外条款测试,字段最少)。"""
    now = datetime.now(UTC)
    return ChapterCard(
        chapter_id=chapter_id,
        workspace_id="ws-1",
        volume_id="vol-1",
        chapter_number=2,
        title="测试章",
        tension_level=5,
        word_count_min=2500,
        word_count_max=4000,
        plot_node_budget=3,
        must_achieve=["事件 A"],
        must_unresolved=["悬念 X"],
        forbidden=[],
        pov_character="主角",
        setting=setting,
        continuity_context=ContinuityContext(
            prev_chapter_tail="(上一章末尾)",
            unfinished_actions=[],
            time_gap="立即续接",
            scene_continuation="same",
            pov_change=pov_change,
            pov_from=pov_from,
            pov_to="主角",
        ),
        next_chapter_seed="下一章伏笔",
        arc_phase="rising",
        is_arc_finale=False,
        related_plot_threads=[],
        related_characters=[],
        card_status="approved",
        card_approved_at=now,
    )


# ============================================================================
# A. happy path
# ============================================================================


class TestHappyPath:
    def test_third_to_third_same_dominance_passed(self) -> None:
        baseline = _make_fingerprint(pov="third_person")
        current = _make_fingerprint(pov="third_person")
        result = check_cross_chapter_voice(
            baseline_fingerprint=baseline,
            current_fingerprint=current,
        )
        assert result.passed is True
        assert result.name == "cross_chapter_voice"

    def test_first_to_first_same_dominance_passed(self) -> None:
        baseline = _make_fingerprint(pov="first_person")
        current = _make_fingerprint(pov="first_person")
        result = check_cross_chapter_voice(
            baseline_fingerprint=baseline,
            current_fingerprint=current,
        )
        assert result.passed is True


# ============================================================================
# B. dominant_pov_flipped
# ============================================================================


class TestDominantPovFlipped:
    def test_third_to_first_flipped_fail(self) -> None:
        baseline = _make_fingerprint(pov="third_person")
        current = _make_fingerprint(pov="first_person")
        result = check_cross_chapter_voice(
            baseline_fingerprint=baseline,
            current_fingerprint=current,
        )
        assert result.passed is False
        # 应至少含 dominant_pov_flipped 类型 failure
        types = [f["type"] for f in result.details["failures"]]
        assert "dominant_pov_flipped" in types

    def test_first_to_third_flipped_fail(self) -> None:
        baseline = _make_fingerprint(pov="first_person")
        current = _make_fingerprint(pov="third_person")
        result = check_cross_chapter_voice(
            baseline_fingerprint=baseline,
            current_fingerprint=current,
        )
        assert result.passed is False
        types = [f["type"] for f in result.details["failures"]]
        assert "dominant_pov_flipped" in types

    def test_mixed_to_third_not_flipped_passed(self) -> None:
        # mixed 不算 flip(过渡态),其他维度也无漂移 → passed
        baseline = _make_fingerprint(pov="mixed")
        # mixed fixture 默认只有"我"+"他",但 third fixture 含"她";手动给
        # baseline 加"她"+ 给 current 加"我"避免 0 vs nonzero 触 inf 比例。
        baseline["pronoun_freq_她"] = 0.006
        current = _make_fingerprint(pov="third_person", paragraph_mean=100.0)
        current["pronoun_freq_我"] = 0.005  # baseline mixed 我=0.010 → ratio 2x < 10x
        result = check_cross_chapter_voice(
            baseline_fingerprint=baseline,
            current_fingerprint=current,
        )
        # 我 0.010 → 0.005 = 2x;他 0.012 → 0.015 = 1.25x;她 0.006 → 0.008 = 1.33x。
        # 均 < 10x;mixed 与 third 不构成 dominant_pov_flipped。passed。
        assert result.passed is True


# ============================================================================
# C. pronoun frequency ratio (10x)
# ============================================================================


class TestPronounFreqRatio:
    def test_pronoun_freq_10x_increase_warns_without_failure(self) -> None:
        """baseline 我=0.003 vs current 我=0.030：10x 只观察，不阻断。

        G14 后(2026-05-27 w1-s9):fixture freq 提到 0.003(count=9 >= 5)+
        current 0.030(count=90 >= 5)双侧达 PRONOUN_MIN_ABSOLUTE_COUNT=5 floor,
        ratio = 0.030/0.003 = 10x → WARN。原 fixture(0.001 vs 0.020)baseline
        count=3 < 5 触发 G14 兜底跳过(剧情自然差异而非漂移,语义正确)。"""
        baseline = _make_fingerprint(pov="third_person")
        baseline["pronoun_freq_我"] = 0.003  # 9 次/3000 字,绝对计数 >= 5
        current = _make_fingerprint(pov="third_person")
        current["pronoun_freq_我"] = 0.030  # 90 次/3000 字,绝对计数 >> 5
        result = check_cross_chapter_voice(
            baseline_fingerprint=baseline,
            current_fingerprint=current,
        )
        assert result.passed is True
        assert result.details["failures"] == []
        types = [
            f["type"] for f in result.details["pronoun_frequency_warnings"]
        ]
        assert "pronoun_freq_ratio_exceeded" in types

    def test_both_sparse_pronouns_skipped_passed(self) -> None:
        """双方 pronoun freq < floor 0.002 → 跳过该代词不算失败。"""
        baseline = _make_fingerprint(pov="third_person")
        baseline["pronoun_freq_我"] = 0.0001
        current = _make_fingerprint(pov="third_person")
        current["pronoun_freq_我"] = 0.0
        result = check_cross_chapter_voice(
            baseline_fingerprint=baseline,
            current_fingerprint=current,
        )
        # 该代词跳过 + 其他不漂移 → passed
        assert result.passed is True


# ============================================================================
# D. paragraph mean drift — G15 起 warn-only(v1.1 wave 1 session 10)
# ============================================================================


class TestParagraphMeanDriftWarnOnly:
    """G15(2026-05-27 w1-s10):paragraph_length_mean_drift 从 G10 cross_chapter_voice
    硬失败拆出为 warn-only(留 ``details.paragraph_drift_observed`` + structlog
    warn,**不入 ``failures`` 不阻塞流水**)。POV identity 由 dominant_pov_flipped
    + pronoun_freq_ratio 独立兜底。
    """

    def test_paragraph_mean_below_threshold_passed_no_observation(self) -> None:
        """偏移 25% < 30% → passed + paragraph_drift_observed=None(未观察到漂移)。"""
        baseline = _make_fingerprint(pov="third_person", paragraph_mean=100.0)
        current = _make_fingerprint(pov="third_person", paragraph_mean=75.0)
        result = check_cross_chapter_voice(
            baseline_fingerprint=baseline,
            current_fingerprint=current,
        )
        assert result.passed is True
        assert result.details["paragraph_drift_observed"] is None

    def test_paragraph_mean_above_threshold_now_warn_only_passed(self) -> None:
        """偏移 40% > 30% → G15 后 **passed**(warn-only,不入 failures)+ details
        含 ``paragraph_drift_observed`` 字段供调试。"""
        baseline = _make_fingerprint(pov="third_person", paragraph_mean=100.0)
        current = _make_fingerprint(pov="third_person", paragraph_mean=60.0)
        result = check_cross_chapter_voice(
            baseline_fingerprint=baseline,
            current_fingerprint=current,
        )
        assert result.passed is True  # G15 拆 warn-only
        # failures 不含 paragraph_length_mean_drift
        types = [f["type"] for f in result.details["failures"]]
        assert "paragraph_length_mean_drift" not in types
        # 但 details.paragraph_drift_observed 仍保留观察数据
        observed = result.details["paragraph_drift_observed"]
        assert observed is not None
        assert observed["type"] == "paragraph_length_mean_drift"
        assert observed["baseline_mean"] == 100.0
        assert observed["current_mean"] == 60.0
        assert observed["relative_drift"] == 0.4

    def test_paragraph_mean_baseline_zero_current_nonzero_now_warn_only_passed(
        self,
    ) -> None:
        """baseline=0 + current=80 边界 case → G15 后 passed + 仍 warn observation。"""
        baseline = _make_fingerprint(pov="third_person", paragraph_mean=0.0)
        current = _make_fingerprint(pov="third_person", paragraph_mean=80.0)
        result = check_cross_chapter_voice(
            baseline_fingerprint=baseline,
            current_fingerprint=current,
        )
        assert result.passed is True  # G15
        observed = result.details["paragraph_drift_observed"]
        assert observed is not None
        assert observed["relative_drift"] == 1.0

    def test_g15_rerun3_ch4_ch5_real_data_now_passes(self) -> None:
        """luo-clean-v1 rerun-3 ch-5 v1 现场重演:G15 后 paragraph drift 77% warn-only,passed。

        现场实测:
        - ch-4 baseline paraMean = 53.1
        - ch-5 v1 paraMean = 94.0(双世界场景切换,77% drift)
        - dominant_pov 两侧都 third_person(POV 没漂)
        - 代词频次 G14 兜底通过(她/我 ch-5 count=0 < 5 skip)

        G15 应让本 case passed,LLM 不必为剧情驱动的节奏自然差异反复 rewrite。"""
        ch4 = _make_fingerprint(
            pov="third_person", paragraph_mean=53.1, total_chars=2920.0
        )
        ch4["pronoun_freq_我"] = 0.0038
        ch4["pronoun_freq_她"] = 0.0031
        ch4["pronoun_freq_他"] = 0.0065
        ch5 = _make_fingerprint(
            pov="third_person", paragraph_mean=94.0, total_chars=2781.0
        )
        ch5["pronoun_freq_我"] = 0.0004  # 1 次 / 2781 字
        ch5["pronoun_freq_她"] = 0.0
        ch5["pronoun_freq_他"] = 0.0126  # 35 次
        result = check_cross_chapter_voice(
            baseline_fingerprint=ch4,
            current_fingerprint=ch5,
            workspace_default_voice="third_person",
            baseline_chapter_id="ch-0004",
            current_chapter_id="ch-0005",
        )
        assert result.passed is True, (
            f"G15 应让该 case passed,实际 failures={result.details.get('failures')}"
        )
        # paragraph drift 观察项仍记录(供 LogsPage 调试)
        observed = result.details["paragraph_drift_observed"]
        assert observed is not None
        assert observed["baseline_mean"] == 53.1
        assert observed["current_mean"] == 94.0
        assert 0.7 <= observed["relative_drift"] <= 0.8  # 77% drift


# ============================================================================
# D2. G14 pronoun_freq_ratio 绝对计数兜底(v1.1 wave 1 session 9 引入)
# ============================================================================


class TestG14PronounAbsoluteCountFloor:
    """G14 治本:任一侧代词绝对计数 < PRONOUN_MIN_ABSOLUTE_COUNT (5) 跳过 ratio 检测。

    rerun-2 实证发现 G10 在"剧情自然差异"边界 case 误判:ch-5 沈舟独自行动场景
    导致 ``我/她`` 频次=0,vs ch-4 baseline 频次>floor 但绝对计数仍小(0.0031 *
    2920 = 9 次)→ 10x ratio 触发误判。G14 加绝对计数兜底解决。
    """

    def test_rerun2_ch4_ch5_real_data_now_passes(self) -> None:
        """luo-clean-v1 rerun-2 ch-5 v1 现场重演:G14 后 pronoun_freq_ratio 跳过,passed。

        实测数据:
        - ch-4 baseline: 我=0.0038 (≈11 次/2920 字), 她=0.0031 (≈9 次), 他=0.0065 (≈19 次)
        - ch-5 v1 current: 我=0.0 (0 次), 她=0.0 (0 次), 他=0.0100 (≈29 次)

        G10 旧逻辑:我/她 minor=0 + major>=floor → 双 inf ratio fail
        G14 新逻辑:current 我/她 count=0 < 5 → 跳过 ratio 检测 → only 他 比较 →
        ratio = 0.0100/0.0065 ≈ 1.54x < 10x → passed
        """
        ch4 = _make_fingerprint(pov="third_person", total_chars=2920.0)
        ch4["pronoun_freq_我"] = 0.0038
        ch4["pronoun_freq_她"] = 0.0031
        ch4["pronoun_freq_他"] = 0.0065
        ch5 = _make_fingerprint(pov="third_person", total_chars=2903.0)
        ch5["pronoun_freq_我"] = 0.0
        ch5["pronoun_freq_她"] = 0.0
        ch5["pronoun_freq_他"] = 0.0100
        result = check_cross_chapter_voice(
            baseline_fingerprint=ch4,
            current_fingerprint=ch5,
            workspace_default_voice="third_person",
            baseline_chapter_id="ch-0004",
            current_chapter_id="ch-0005",
        )
        assert result.passed is True, (
            f"G14 应让该 case passed,实际 failures={result.details.get('failures')}"
        )

    def test_baseline_sparse_current_heavy_skips_passed(self) -> None:
        """baseline 我=0.001 (3 次) → current 我=0.020 (60 次):baseline count < 5 → skip。"""
        baseline = _make_fingerprint(pov="third_person", total_chars=3000.0)
        baseline["pronoun_freq_我"] = 0.001  # 3 次
        current = _make_fingerprint(pov="third_person", total_chars=3000.0)
        current["pronoun_freq_我"] = 0.020  # 60 次
        result = check_cross_chapter_voice(
            baseline_fingerprint=baseline, current_fingerprint=current
        )
        # 我 ratio check skip (baseline 3 < 5),无其他失败 → passed
        assert result.passed is True

    def test_baseline_heavy_current_sparse_skips_passed(self) -> None:
        """对称 case:baseline 我=0.020 (60 次) → current 我=0.001 (3 次):current count < 5 → skip。"""
        baseline = _make_fingerprint(pov="third_person", total_chars=3000.0)
        baseline["pronoun_freq_我"] = 0.020  # 60 次
        current = _make_fingerprint(pov="third_person", total_chars=3000.0)
        current["pronoun_freq_我"] = 0.001  # 3 次
        result = check_cross_chapter_voice(
            baseline_fingerprint=baseline, current_fingerprint=current
        )
        assert result.passed is True

    def test_both_sides_above_floor_still_observes_10x_ratio(self) -> None:
        """双侧 count >= 5 时仍记录 10x，但不成为 HARD。"""
        baseline = _make_fingerprint(pov="third_person", total_chars=3000.0)
        baseline["pronoun_freq_我"] = 0.003  # 9 次
        current = _make_fingerprint(pov="third_person", total_chars=3000.0)
        current["pronoun_freq_我"] = 0.030  # 90 次
        result = check_cross_chapter_voice(
            baseline_fingerprint=baseline, current_fingerprint=current
        )
        assert result.passed is True
        types = [
            f["type"] for f in result.details["pronoun_frequency_warnings"]
        ]
        assert "pronoun_freq_ratio_exceeded" in types

    def test_dominant_pov_flip_still_caught_independently(self) -> None:
        """真 POV flip 由 dominant_pov_flipped 独立检测,G14 兜底不影响。

        baseline 第三人称主导(他>>我),current 第一人称主导(我>>他):
        我 freq 比 baseline 大幅升 + dominant pov 翻转 → 即使 G14 让 pronoun_freq
        check skip 部分代词,dominant_pov_flipped 仍 fail 兜底捕获。
        """
        # 让 ch-3 我 count = 3 (< 5,G14 skip 该代词 ratio),ch-4 我 count = 60
        ch3 = _make_fingerprint(pov="third_person", total_chars=3000.0)
        ch3["pronoun_freq_我"] = 0.001  # 3 次,触发 G14 skip
        ch3["pronoun_freq_他"] = 0.018  # 主导
        ch4 = _make_fingerprint(pov="first_person", total_chars=3000.0)
        ch4["pronoun_freq_我"] = 0.020  # 60 次,主导
        ch4["pronoun_freq_他"] = 0.003
        result = check_cross_chapter_voice(
            baseline_fingerprint=ch3,
            current_fingerprint=ch4,
            workspace_default_voice="third_person",
        )
        assert result.passed is False
        types = [f["type"] for f in result.details["failures"]]
        # dominant_pov_flipped 独立检测,不受 G14 兜底影响
        assert "dominant_pov_flipped" in types

    def test_boundary_count_exactly_5_observes_ratio(self) -> None:
        """绝对计数 = 5 边界：>= floor 后记录 ratio WARN。"""
        # count = freq * total_chars
        # freq=0.0017, total_chars=3000 → count = 5.1 ≈ 5(>= floor)
        baseline = _make_fingerprint(pov="third_person", total_chars=3000.0)
        baseline["pronoun_freq_我"] = 0.0017  # 5.1 次
        current = _make_fingerprint(pov="third_person", total_chars=3000.0)
        current["pronoun_freq_我"] = 0.017  # 51 次,10x
        result = check_cross_chapter_voice(
            baseline_fingerprint=baseline, current_fingerprint=current
        )
        assert result.passed is True
        types = [
            f["type"] for f in result.details["pronoun_frequency_warnings"]
        ]
        assert "pronoun_freq_ratio_exceeded" in types


# ============================================================================
# E. 例外条款豁免
# ============================================================================


class TestExemption:
    def test_cross_chapter_pov_character_change_does_not_exempt_person_drift(
        self,
    ) -> None:
        baseline = _make_fingerprint(pov="third_person")
        current = _make_fingerprint(pov="first_person")
        card = _make_chapter_card(pov_change=True)
        result = check_cross_chapter_voice(
            baseline_fingerprint=baseline,
            current_fingerprint=current,
            chapter_card=card,
        )
        assert result.passed is False
        assert "skipped" not in result.details
        assert "dominant_pov_flipped" in {
            failure["type"] for failure in result.details["failures"]
        }

    def test_cross_chapter_pov_character_change_keeps_same_person_valid(
        self,
    ) -> None:
        baseline = _make_fingerprint(pov="third_person")
        current = _make_fingerprint(pov="third_person")
        card = _make_chapter_card(pov_change=True)
        result = check_cross_chapter_voice(
            baseline_fingerprint=baseline,
            current_fingerprint=current,
            chapter_card=card,
        )
        assert result.passed is True
        assert "skipped" not in result.details

    def test_setting_dream_exempted_passed(self) -> None:
        baseline = _make_fingerprint(pov="third_person")
        current = _make_fingerprint(pov="first_person")
        card = _make_chapter_card(setting="主角进入梦境,化身锦衣卫巡夜")
        result = check_cross_chapter_voice(
            baseline_fingerprint=baseline,
            current_fingerprint=current,
            chapter_card=card,
        )
        assert result.passed is True
        assert "梦境" in result.details["exemption_reason"]

    def test_setting_recall_exempted_passed(self) -> None:
        baseline = _make_fingerprint(pov="third_person")
        current = _make_fingerprint(pov="first_person")
        card = _make_chapter_card(setting="酒馆,主角回忆童年往事")
        result = check_cross_chapter_voice(
            baseline_fingerprint=baseline,
            current_fingerprint=current,
            chapter_card=card,
        )
        assert result.passed is True
        assert "回忆" in result.details["exemption_reason"]

    def test_mini_plan_event_flashback_exempted_passed(self) -> None:
        baseline = _make_fingerprint(pov="third_person")
        current = _make_fingerprint(pov="first_person")
        result = check_cross_chapter_voice(
            baseline_fingerprint=baseline,
            current_fingerprint=current,
            mini_plan_event="主角倒叙交代背景故事",
        )
        assert result.passed is True
        assert "倒叙" in result.details["exemption_reason"]

    def test_no_exemption_keyword_runs_full_check(self) -> None:
        """卡 setting 不含豁免词 + mini_plan 不含 → 走规则,flip 应 fail。"""
        baseline = _make_fingerprint(pov="third_person")
        current = _make_fingerprint(pov="first_person")
        card = _make_chapter_card(setting="现代心理诊所候诊室")
        result = check_cross_chapter_voice(
            baseline_fingerprint=baseline,
            current_fingerprint=current,
            chapter_card=card,
            mini_plan_event="主角与患者深度访谈",
        )
        assert result.passed is False


# ============================================================================
# F. 首章 default_voice
# ============================================================================


class TestFirstChapterDefaultVoice:
    def test_first_chapter_third_person_with_third_default_passed(self) -> None:
        current = _make_fingerprint(pov="third_person")
        result = check_cross_chapter_voice(
            baseline_fingerprint=None,
            current_fingerprint=current,
            workspace_default_voice="third_person",
            is_first_chapter=True,
        )
        assert result.passed is True
        # G12 改名:旧 ``details["first_chapter"]=True`` → 新 ``details["fallback_reason"]="first_chapter"``
        assert result.details["fallback_reason"] == "first_chapter"

    def test_first_chapter_first_person_violates_third_default_fail(self) -> None:
        current = _make_fingerprint(pov="first_person")
        result = check_cross_chapter_voice(
            baseline_fingerprint=None,
            current_fingerprint=current,
            workspace_default_voice="third_person",
            is_first_chapter=True,
        )
        assert result.passed is False
        assert result.details["failure_type"] == "first_chapter_violates_default_voice"

    def test_first_chapter_third_person_violates_first_default_fail(self) -> None:
        current = _make_fingerprint(pov="third_person")
        result = check_cross_chapter_voice(
            baseline_fingerprint=None,
            current_fingerprint=current,
            workspace_default_voice="first_person",
            is_first_chapter=True,
        )
        assert result.passed is False

    def test_first_chapter_free_default_passed(self) -> None:
        """free 模式直接 Layer 0 passed,不进首章校验。"""
        current = _make_fingerprint(pov="first_person")
        result = check_cross_chapter_voice(
            baseline_fingerprint=None,
            current_fingerprint=current,
            workspace_default_voice="free",
            is_first_chapter=True,
        )
        assert result.passed is True
        assert result.details["skipped"] == "workspace_default_voice_free"

    def test_first_chapter_mixed_pov_not_enforced_passed(self) -> None:
        current = _make_fingerprint(pov="mixed")
        result = check_cross_chapter_voice(
            baseline_fingerprint=None,
            current_fingerprint=current,
            workspace_default_voice="third_person",
            is_first_chapter=True,
        )
        assert result.passed is True
        # G12 改名:旧 ``first_chapter_mixed_pov`` → 新 ``fallback_mixed_pov``(统一首章 + 非首章 baseline 缺失场景)
        assert result.details["skipped"] == "fallback_mixed_pov"
        assert result.details["fallback_reason"] == "first_chapter"


# ============================================================================
# G. free 模式
# ============================================================================


class TestFreeMode:
    def test_free_mode_skips_everything(self) -> None:
        """即使 third → first flip,free 模式直接 passed。"""
        baseline = _make_fingerprint(pov="third_person")
        current = _make_fingerprint(pov="first_person")
        result = check_cross_chapter_voice(
            baseline_fingerprint=baseline,
            current_fingerprint=current,
            workspace_default_voice="free",
        )
        assert result.passed is True
        assert result.details["skipped"] == "workspace_default_voice_free"


# ============================================================================
# H. baseline missing non-first chapter — G12 fallback to default_voice
# ============================================================================


class TestBaselineMissingNonFirstChapterG12Fallback:
    """v1.1 wave 1 session 8 / G12:baseline 缺失 + 非首章场景**不再 fail-open**,
    统一回退 workspace.default_voice 校验。"""

    def test_baseline_none_non_first_chapter_first_person_violates_third_default_fail(
        self,
    ) -> None:
        """G12 治本核心场景:luo-clean-v1 ch-6 现场重演 — ch-5 human_intervention
        baseline=None + ch-6 写成 first_person + workspace default=third_person
        → G12 应拦截(对照 G10 旧版 fail-open 漏抓)。"""
        current = _make_fingerprint(pov="first_person")
        result = check_cross_chapter_voice(
            baseline_fingerprint=None,
            current_fingerprint=current,
            workspace_default_voice="third_person",
            is_first_chapter=False,
        )
        assert result.passed is False
        assert (
            result.details["failure_type"]
            == "no_approved_ancestor_violates_default_voice"
        )
        assert result.details["fallback_reason"] == "no_approved_ancestor_found"
        assert result.details["default_voice"] == "third_person"
        assert result.details["current_pov"] == "first_person"

    def test_baseline_none_non_first_chapter_third_person_with_third_default_passed(
        self,
    ) -> None:
        """default=third_person + current=third_person → passed(无漂移)。"""
        current = _make_fingerprint(pov="third_person")
        result = check_cross_chapter_voice(
            baseline_fingerprint=None,
            current_fingerprint=current,
            workspace_default_voice="third_person",
            is_first_chapter=False,
        )
        assert result.passed is True
        assert result.details["fallback_reason"] == "no_approved_ancestor_found"

    def test_baseline_none_non_first_chapter_third_person_violates_first_default_fail(
        self,
    ) -> None:
        """对称 case:default=first_person + current=third_person → fail。"""
        current = _make_fingerprint(pov="third_person")
        result = check_cross_chapter_voice(
            baseline_fingerprint=None,
            current_fingerprint=current,
            workspace_default_voice="first_person",
            is_first_chapter=False,
        )
        assert result.passed is False
        assert (
            result.details["failure_type"]
            == "no_approved_ancestor_violates_default_voice"
        )
        assert result.details["fallback_reason"] == "no_approved_ancestor_found"

    def test_baseline_none_non_first_chapter_mixed_pov_not_enforced(self) -> None:
        """mixed POV 过渡态不强拦,passed + warn(fallback_mixed_pov)。"""
        current = _make_fingerprint(pov="mixed")
        result = check_cross_chapter_voice(
            baseline_fingerprint=None,
            current_fingerprint=current,
            workspace_default_voice="third_person",
            is_first_chapter=False,
        )
        assert result.passed is True
        assert result.details["skipped"] == "fallback_mixed_pov"
        assert result.details["fallback_reason"] == "no_approved_ancestor_found"

    def test_baseline_none_non_first_chapter_free_mode_skipped(self) -> None:
        """free 模式直接 Layer 0 放行,不进 G12 兜底路径。"""
        current = _make_fingerprint(pov="first_person")
        result = check_cross_chapter_voice(
            baseline_fingerprint=None,
            current_fingerprint=current,
            workspace_default_voice="free",
            is_first_chapter=False,
        )
        assert result.passed is True
        assert result.details["skipped"] == "workspace_default_voice_free"

    def test_baseline_none_non_first_chapter_explicit_exemption_skipped(self) -> None:
        """例外条款(setting 含'梦境'字面)在 Layer 1 提前放行,不进 G12 兜底。"""
        card = _make_chapter_card(setting="梦境中楚江操控图腾")
        current = _make_fingerprint(pov="first_person")
        result = check_cross_chapter_voice(
            baseline_fingerprint=None,
            current_fingerprint=current,
            workspace_default_voice="third_person",
            is_first_chapter=False,
            chapter_card=card,
        )
        assert result.passed is True
        assert result.details["skipped"] == "exempted"


# ============================================================================
# I. 短 / 长文本兜底
# ============================================================================


class TestTextLengthBounds:
    def test_short_current_skipped(self) -> None:
        baseline = _make_fingerprint(pov="third_person")
        current = _make_fingerprint(pov="first_person", total_chars=300.0)
        # total_chars 300 < MIN_CHARS_FOR_VOICE_CHECK=500 → skip
        result = check_cross_chapter_voice(
            baseline_fingerprint=baseline,
            current_fingerprint=current,
        )
        assert result.passed is True
        assert result.details["skipped"] == "short_text"
        assert result.details["threshold"] == MIN_CHARS_FOR_VOICE_CHECK

    def test_unrealistic_long_current_skipped(self) -> None:
        baseline = _make_fingerprint(pov="third_person")
        current = _make_fingerprint(pov="first_person", total_chars=2_000_000.0)
        result = check_cross_chapter_voice(
            baseline_fingerprint=baseline,
            current_fingerprint=current,
        )
        assert result.passed is True
        assert result.details["skipped"] == "unrealistic_long_text"

    def test_short_baseline_skipped(self) -> None:
        baseline = _make_fingerprint(pov="third_person", total_chars=300.0)
        current = _make_fingerprint(pov="first_person")
        result = check_cross_chapter_voice(
            baseline_fingerprint=baseline,
            current_fingerprint=current,
        )
        assert result.passed is True
        assert result.details["skipped"] == "baseline_short_or_unrealistic_long"


# ============================================================================
# J. luo-clean-v1 现场重演(third → first 跨章节漂移)
# ============================================================================


class TestLuoCleanV1Scenario:
    def test_ch3_third_to_ch4_first_dominant_pov_flip_caught(self) -> None:
        """重演 luo-clean-v1 ch-3 (third dominant) → ch-4 (first dominant) 跨章漂移。

        现场数据:
        - ch-3 fingerprint: 我=0~2 频次, 他=11(third dominant)
        - ch-4 fingerprint: 我=10~12, 他=2~3(first dominant)
        - reviewer 单章 voice_consistency 给 ch-4 高分(单章内一致),不抓跨章
        - G10 backstop 应抓住 dominant_pov_flipped + 我频次 6x ~ 12x 失败

        本测试验证 G10 100% 截获该现场。
        """
        # G14 (2026-05-27 w1-s9):fixture freq 调到双侧绝对计数 >= 5 让 pronoun_freq_ratio
        # 检测也触发。原 fixture(ch-3 我=0.001 = 3 次)在 G14 兜底下 skip,但
        # dominant_pov_flipped 独立检测仍 100% 截获真 POV flip(本测试核心 assertion)
        ch3 = _make_fingerprint(pov="third_person")
        ch3["pronoun_freq_我"] = 0.002  # 6 次 / 3000 字,刚过 G14 floor (5)
        ch3["pronoun_freq_他"] = 0.018
        ch3["pronoun_freq_她"] = 0.005

        ch4 = _make_fingerprint(pov="first_person")
        ch4["pronoun_freq_我"] = 0.020  # 60 次 / 3000 字,ratio = 10x 命中
        ch4["pronoun_freq_他"] = 0.003
        ch4["pronoun_freq_她"] = 0.001

        result = check_cross_chapter_voice(
            baseline_fingerprint=ch3,
            current_fingerprint=ch4,
            workspace_default_voice="third_person",
            baseline_chapter_id="ch-0003",
            current_chapter_id="ch-0004",
        )
        assert result.passed is False
        # dominant flip 是 HARD；我 freq ratio 只进观察字段。
        types = [f["type"] for f in result.details["failures"]]
        assert "dominant_pov_flipped" in types
        warning_types = [
            f["type"] for f in result.details["pronoun_frequency_warnings"]
        ]
        assert "pronoun_freq_ratio_exceeded" in warning_types
        # 验证 details 字段含 baseline / current 章节 ID
        assert result.details["baseline_chapter_id"] == "ch-0003"
        assert result.details["current_chapter_id"] == "ch-0004"


# ============================================================================
# K. 边界测试:exempted + 多失败混合(确保例外条款优先级)
# ============================================================================


class TestExemptionPriorityOverFailures:
    def test_exemption_short_circuits_before_pov_check(self) -> None:
        """例外条款应在跨章 pov check 之前命中(Layer 1 优先)。"""
        baseline = _make_fingerprint(pov="third_person")
        current = _make_fingerprint(pov="first_person")
        card = _make_chapter_card(setting="POV切换:转换至次要角色视角")
        result = check_cross_chapter_voice(
            baseline_fingerprint=baseline,
            current_fingerprint=current,
            chapter_card=card,
        )
        assert result.passed is True
        # 即便 pov flip 客观存在,例外豁免优先
        assert "exempted" in result.details["skipped"]


# ============================================================================
# L. validator name + 返值契约
# ============================================================================


class TestValidatorContract:
    def test_validator_name_field_is_cross_chapter_voice(self) -> None:
        baseline = _make_fingerprint(pov="third_person")
        current = _make_fingerprint(pov="third_person")
        result = check_cross_chapter_voice(
            baseline_fingerprint=baseline,
            current_fingerprint=current,
        )
        assert result.name == "cross_chapter_voice"

    @pytest.mark.parametrize(
        "default_voice,current_pov,expected_passed",
        [
            ("third_person", "third_person", True),
            ("third_person", "first_person", False),
            ("first_person", "first_person", True),
            ("first_person", "third_person", False),
        ],
    )
    def test_first_chapter_default_voice_matrix(
        self, default_voice: str, current_pov: str, expected_passed: bool
    ) -> None:
        current = _make_fingerprint(pov=current_pov)  # type: ignore[arg-type]
        result = check_cross_chapter_voice(
            baseline_fingerprint=None,
            current_fingerprint=current,
            workspace_default_voice=default_voice,  # type: ignore[arg-type]
            is_first_chapter=True,
        )
        assert result.passed is expected_passed
