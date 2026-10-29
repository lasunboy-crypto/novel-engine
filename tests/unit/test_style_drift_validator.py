"""v1.1 wave 1 session 4(G5):``check_style_drift`` 单元测。

覆盖:
- 阈值边界(段落 / 句长 / 代词 / 修辞)
- baseline 缺 fail-open
- 短文本兜底(MIN_CHARS_FOR_DRIFT_CHECK)
- 代词噪声 floor(PRONOUN_MIN_ABSOLUTE_COUNT)
- 标准差稳定 floor(STD_STABILITY_MIN_SAMPLES)
- 同 fingerprint 自比 passed
- 极端漂移检测(luo-cloud-test ch-21 v1 vs v4 现场重演 — 第三人称长段切第一人称短句)
"""

from __future__ import annotations

from src.core.rules.style_drift_validator import (
    MAX_REALISTIC_CHAPTER_CHARS,
    MIN_CHARS_FOR_DRIFT_CHECK,
    PARAGRAPH_LENGTH_MEAN_DRIFT_THRESHOLD,
    PRONOUN_FREQ_DRIFT_THRESHOLD,
    SENTENCE_LENGTH_MEAN_DRIFT_THRESHOLD,
    check_style_drift,
    quantize_prompt_visible_style_mean,
)
from src.core.rules.style_fingerprint import compute_style_fingerprint


def _build_baseline_fp(
    *,
    paragraph_length_mean: float = 200.0,
    paragraph_length_std: float = 80.0,
    sentence_length_mean: float = 30.0,
    sentence_length_std: float = 15.0,
    simile_density: float = 0.1,
    exclamation_density: float = 0.05,
    total_chars: float = 3000.0,
    total_sentences: float = 100.0,
    pronoun_freqs: dict[str, float] | None = None,
) -> dict[str, float]:
    """建一个长篇章节级 baseline fingerprint(默认值过 MIN_CHARS / STD floor)。"""
    fp: dict[str, float] = {
        "paragraph_length_mean": paragraph_length_mean,
        "paragraph_length_std": paragraph_length_std,
        "sentence_length_mean": sentence_length_mean,
        "sentence_length_std": sentence_length_std,
        "simile_density": simile_density,
        "exclamation_density": exclamation_density,
        "total_chars": total_chars,
        "total_sentences": total_sentences,
    }
    default_pronouns = {
        p: 0.02 for p in ("我", "你", "他", "她", "它", "我们", "你们", "他们", "她们")
    }
    if pronoun_freqs:
        default_pronouns.update(pronoun_freqs)
    for p, f in default_pronouns.items():
        fp[f"pronoun_freq_{p}"] = f
    return fp


class TestBaselineMissing:
    """baseline_fingerprint=None fail-open(旧数据无指纹)。"""

    def test_baseline_none_returns_passed_with_skipped_marker(self) -> None:
        current = _build_baseline_fp()
        result = check_style_drift(
            baseline_fingerprint=None,
            current_fingerprint=current,
            current_version_id=4,
        )
        assert result.passed is True
        assert result.details["skipped"] == "baseline_missing"


class TestShortTextSafeguard:
    """MIN_CHARS_FOR_DRIFT_CHECK 短文本全 validator 直接 passed。"""

    def test_baseline_short_text_skips(self) -> None:
        baseline = _build_baseline_fp(total_chars=MIN_CHARS_FOR_DRIFT_CHECK - 1)
        current = _build_baseline_fp(total_chars=3000.0)
        result = check_style_drift(
            baseline_fingerprint=baseline,
            current_fingerprint=current,
            current_version_id=2,
        )
        assert result.passed is True
        assert result.details["skipped"] == "short_text"

    def test_current_short_text_skips(self) -> None:
        baseline = _build_baseline_fp(total_chars=3000.0)
        current = _build_baseline_fp(total_chars=MIN_CHARS_FOR_DRIFT_CHECK - 1)
        result = check_style_drift(
            baseline_fingerprint=baseline,
            current_fingerprint=current,
            current_version_id=2,
        )
        assert result.passed is True
        assert result.details["skipped"] == "short_text"

    def test_unrealistic_long_text_skips(self) -> None:
        """MockLLMClient 100M 字 fixture 触发的兜底 — 大于 1M 字直接跳过。"""
        baseline = _build_baseline_fp(total_chars=MAX_REALISTIC_CHAPTER_CHARS + 1.0)
        current = _build_baseline_fp(total_chars=3000.0)
        result = check_style_drift(
            baseline_fingerprint=baseline,
            current_fingerprint=current,
            current_version_id=2,
        )
        assert result.passed is True
        assert result.details["skipped"] == "unrealistic_long_text"

    def test_both_long_text_does_not_skip(self) -> None:
        baseline = _build_baseline_fp(total_chars=3000.0)
        current = _build_baseline_fp(total_chars=3000.0)
        result = check_style_drift(
            baseline_fingerprint=baseline,
            current_fingerprint=current,
            current_version_id=2,
        )
        # 自比 → passed,但不应有 skipped marker
        assert result.passed is True
        assert "skipped" not in result.details


class TestSelfCompare:
    """同 fingerprint 自比 → passed,drifted_dimensions = []。"""

    def test_self_compare_passes(self) -> None:
        fp = _build_baseline_fp()
        result = check_style_drift(
            baseline_fingerprint=fp,
            current_fingerprint=fp,
            current_version_id=4,
        )
        assert result.passed is True
        assert result.details["drifted_dimensions"] == []


class TestParagraphLengthDrift:
    """段落长度均值偏移阈值边界。"""

    def test_paragraph_mean_drift_above_threshold_fails(self) -> None:
        baseline = _build_baseline_fp(paragraph_length_mean=200.0)
        # > 30% 偏移
        current = _build_baseline_fp(paragraph_length_mean=80.0)  # 偏移 60%
        result = check_style_drift(
            baseline_fingerprint=baseline,
            current_fingerprint=current,
            current_version_id=4,
        )
        assert result.passed is False
        drifted = [
            d
            for d in result.details["drifted_dimensions"]
            if d["dimension"] == "paragraph_length_mean"
        ]
        assert len(drifted) == 1
        assert drifted[0]["relative_drift"] > PARAGRAPH_LENGTH_MEAN_DRIFT_THRESHOLD

    def test_paragraph_mean_drift_at_threshold_passes(self) -> None:
        baseline = _build_baseline_fp(paragraph_length_mean=200.0)
        # 等于 30% 偏移 → 不超阈值(strict greater)
        current = _build_baseline_fp(paragraph_length_mean=140.0)  # 偏移 30%
        result = check_style_drift(
            baseline_fingerprint=baseline,
            current_fingerprint=current,
            current_version_id=4,
        )
        assert result.passed is True


class TestSentenceLengthDrift:
    """句长均值偏移阈值边界 + std stability floor。"""

    def test_sentence_mean_drift_fails(self) -> None:
        baseline = _build_baseline_fp(sentence_length_mean=30.0)
        current = _build_baseline_fp(sentence_length_mean=10.0)  # 偏移 67%
        result = check_style_drift(
            baseline_fingerprint=baseline,
            current_fingerprint=current,
            current_version_id=4,
        )
        assert result.passed is False
        drifted = [
            d
            for d in result.details["drifted_dimensions"]
            if d["dimension"] == "sentence_length_mean"
        ]
        assert len(drifted) == 1
        assert drifted[0]["relative_drift"] > SENTENCE_LENGTH_MEAN_DRIFT_THRESHOLD

    def test_short_sample_skips_std_check(self) -> None:
        # 句子数 < 5 时 std 不参与检测
        baseline = _build_baseline_fp(
            total_sentences=3,
            sentence_length_std=15.0,
        )
        current = _build_baseline_fp(
            total_sentences=3,
            sentence_length_std=2.0,  # std 偏移巨大,但样本 < 5 跳过
        )
        result = check_style_drift(
            baseline_fingerprint=baseline,
            current_fingerprint=current,
            current_version_id=4,
        )
        # sentence_length_std 不应 drifted(被 floor 跳过)
        std_drifted = [
            d
            for d in result.details["drifted_dimensions"]
            if d["dimension"] == "sentence_length_std"
        ]
        assert len(std_drifted) == 0


class TestPronounDrift:
    """人称代词频次偏移 + noise floor。"""

    def test_pronoun_drift_above_threshold_warn_only(self) -> None:
        """v1.1 audit G24c(2026-05-27):pronoun_freq drift 改 warn-only,
        不进 ``drifted_dimensions`` 导致 fail,改入 ``warn_observed_dimensions``。"""
        baseline = _build_baseline_fp(
            pronoun_freqs={"我": 0.05, "他": 0.005},  # 我:150 次 / 他:15 次
            total_chars=3000.0,
        )
        current = _build_baseline_fp(
            pronoun_freqs={"我": 0.10, "他": 0.005},  # 我:300 次,偏移 100%
            total_chars=3000.0,
        )
        result = check_style_drift(
            baseline_fingerprint=baseline,
            current_fingerprint=current,
            current_version_id=4,
        )
        # G24c:pronoun_freq drift 不再阻塞 fail
        drifted = [
            d
            for d in result.details["drifted_dimensions"]
            if d["dimension"] == "pronoun_freq_我"
        ]
        assert len(drifted) == 0  # 不在硬 fail 维度中
        # 仍记录到 warn_observed 供调试
        warn = [
            d
            for d in result.details["warn_observed_dimensions"]
            if d["dimension"] == "pronoun_freq_我"
        ]
        assert len(warn) == 1
        assert warn[0]["relative_drift"] > PRONOUN_FREQ_DRIFT_THRESHOLD

    def test_sparse_pronoun_skips_drift_check(self) -> None:
        # baseline + current 代词出现都 < 5 次 → 跳过
        baseline = _build_baseline_fp(
            pronoun_freqs={"我": 0.001, "他": 0.0},  # 我:3 次,他:0 次
            total_chars=3000.0,
        )
        current = _build_baseline_fp(
            pronoun_freqs={"我": 0.0, "他": 0.001},  # 我:0 次,他:3 次
            total_chars=3000.0,
        )
        result = check_style_drift(
            baseline_fingerprint=baseline,
            current_fingerprint=current,
            current_version_id=4,
        )
        # 两边都稀疏(< 5),不应被检出漂移
        drifted = [
            d
            for d in result.details["drifted_dimensions"]
            if d["dimension"].startswith("pronoun_freq_")
        ]
        assert len(drifted) == 0


class TestLuoCloudTestRealScenario:
    """luo-cloud-test ch-21 v1(第三人称长段)vs v4(第一人称短句通篇)现场重演。

    通过 compute_style_fingerprint 端到端验真完整管道(extractor + validator)
    能截获实证漂移场景。
    """

    def test_third_to_first_person_short_burst_drift_caught(self) -> None:
        # v1:第三人称长段叙述,3 段,每段 ~600 字
        v1_para = "叶临渊握紧剑柄目光扫过远方的黑岩山他知道这一战不能输山风裹挟着寒意拂面而来他的衣袂猎猎作响远方传来低沉的号角声他握剑的手微微一紧三十年的修炼今日全在此一战这个时候他的心中充满了决意他凝视着远方的山峰那里是他要去的地方他的命运他的归宿他的一切都在那里等着他风从山间吹来带着一丝寒意他深吸一口气感受着自然的力量然后他迈开脚步朝着山的方向走去每一步都坚定而有力他的眼神中闪烁着光芒那是属于一个战士的光芒一个即将面对命运挑战的战士的光芒他走过荒原走过山涧走过密林一路上他没有停歇他知道时间紧迫他必须在天黑之前到达目的地否则一切都将失去意义夜幕降临时他终于来到了黑岩山脚下他抬起头望向山顶心中默念那个名字这是他要见的人也是他要战胜的人。"  # noqa: E501
        v1_text = "\n\n".join([v1_para] * 3)

        # v4:第一人称短句通篇,30+ 行短句
        v4_text = "\n".join(
            ["我握紧剑柄。我抬起头。我望向远山。风来了！你教给我的我没忘！我会赢！" * 3]
            * 6
        )

        fp1 = compute_style_fingerprint(v1_text)
        fp4 = compute_style_fingerprint(v4_text)
        result = check_style_drift(
            baseline_fingerprint=fp1,
            current_fingerprint=fp4,
            current_version_id=4,
        )
        # 必失败 — 风格剧变(G24c 后由 paragraph_mean + sentence_mean 硬约束截获)
        assert result.passed is False
        drifted_dims = {d["dimension"] for d in result.details["drifted_dimensions"]}
        # G24c(2026-05-27 audit):paragraph_mean + sentence_mean 仍硬约束,
        # 这两维度是 mode collapse 真信号 — luo-cloud-test 现场 100% 截获 ✅
        assert "paragraph_length_mean" in drifted_dims
        assert "sentence_length_mean" in drifted_dims
        # G24c:pronoun_freq 改 warn-only,不再进 drifted_dimensions,
        # 但仍在 warn_observed_dimensions 留观察数据
        warn_dims = {d["dimension"] for d in result.details["warn_observed_dimensions"]}
        assert "pronoun_freq_我" in warn_dims


# ===========================================================================
# G24c warn-only / hard 维度契约(wave 2-5 重构防御 — 2026-05-28 立)
# ===========================================================================
#
# 背景:wave 2-5 任何动 style_drift_validator.py 的 session(典型:w4-s5 I2.2
# reviewer 加风格符合度维度可能联动 G24c 阈值微调)有风险把某个 warn-only 维度
# 改回 hard fail → rewrite 死循环回归。现有测试分散覆盖了部分维度,本组测试
# 把 5 个 warn-only 维度 + 2 个 hard 维度集中显式断言,做 invariant 契约。
class TestG24cWarnOnlyDimensionsInvariant:
    """G24c 治本契约:5 维度 warn-only + 2 维度 hard 的稳定性边界。"""

    def test_paragraph_length_mean_drift_is_hard(self) -> None:
        """paragraph_length_mean 50% 漂移 → hard fail(mode collapse 真信号)。"""
        baseline = _build_baseline_fp(paragraph_length_mean=200.0)
        current = _build_baseline_fp(paragraph_length_mean=80.0)  # 偏移 60%
        result = check_style_drift(
            baseline_fingerprint=baseline,
            current_fingerprint=current,
            current_version_id=4,
        )
        drifted = {d["dimension"] for d in result.details["drifted_dimensions"]}
        assert "paragraph_length_mean" in drifted
        assert result.passed is False

    def test_sentence_length_mean_drift_is_hard(self) -> None:
        """sentence_length_mean 30% 漂移 → hard fail(短句模式 mode collapse 信号)。"""
        baseline = _build_baseline_fp(sentence_length_mean=30.0)
        current = _build_baseline_fp(sentence_length_mean=50.0)  # 偏移 ~67%
        result = check_style_drift(
            baseline_fingerprint=baseline,
            current_fingerprint=current,
            current_version_id=4,
        )
        drifted = {d["dimension"] for d in result.details["drifted_dimensions"]}
        assert "sentence_length_mean" in drifted
        assert result.passed is False

    def test_paragraph_length_std_drift_is_warn_only(self) -> None:
        """paragraph_length_std 100% 漂移 → warn-only(剧情自然方差差异)。"""
        baseline = _build_baseline_fp(paragraph_length_std=80.0)
        current = _build_baseline_fp(paragraph_length_std=160.0)  # 偏移 100%
        result = check_style_drift(
            baseline_fingerprint=baseline,
            current_fingerprint=current,
            current_version_id=4,
        )
        drifted = {d["dimension"] for d in result.details["drifted_dimensions"]}
        warn = {d["dimension"] for d in result.details["warn_observed_dimensions"]}
        assert "paragraph_length_std" not in drifted, (
            "G24c 治本契约破坏:paragraph_length_std 不应进 hard fail"
        )
        assert "paragraph_length_std" in warn

    def test_sentence_length_std_drift_is_warn_only(self) -> None:
        """sentence_length_std 100% 漂移 → warn-only。"""
        baseline = _build_baseline_fp(sentence_length_std=15.0)
        current = _build_baseline_fp(sentence_length_std=30.0)  # 偏移 100%
        result = check_style_drift(
            baseline_fingerprint=baseline,
            current_fingerprint=current,
            current_version_id=4,
        )
        drifted = {d["dimension"] for d in result.details["drifted_dimensions"]}
        warn = {d["dimension"] for d in result.details["warn_observed_dimensions"]}
        assert "sentence_length_std" not in drifted, (
            "G24c 治本契约破坏:sentence_length_std 不应进 hard fail"
        )
        assert "sentence_length_std" in warn

    def test_pronoun_freq_drift_is_warn_only(self) -> None:
        """pronoun_freq 100% 漂移 → warn-only(同章漂移由 G10 cross_chapter 截获)。"""
        baseline = _build_baseline_fp(
            pronoun_freqs={"我": 0.05, "他": 0.005},
            total_chars=3000.0,
        )
        current = _build_baseline_fp(
            pronoun_freqs={"我": 0.10, "他": 0.005},  # 偏移 100%
            total_chars=3000.0,
        )
        result = check_style_drift(
            baseline_fingerprint=baseline,
            current_fingerprint=current,
            current_version_id=4,
        )
        drifted = {d["dimension"] for d in result.details["drifted_dimensions"]}
        warn = {d["dimension"] for d in result.details["warn_observed_dimensions"]}
        assert "pronoun_freq_我" not in drifted, (
            "G24c 治本契约破坏:pronoun_freq_* 不应进 hard fail"
        )
        assert "pronoun_freq_我" in warn

    def test_simile_density_drift_is_warn_only(self) -> None:
        """simile_density 200% 漂移 → warn-only(极小基数自然增减)。"""
        baseline = _build_baseline_fp(simile_density=0.05)
        current = _build_baseline_fp(simile_density=0.15)  # 偏移 200%
        result = check_style_drift(
            baseline_fingerprint=baseline,
            current_fingerprint=current,
            current_version_id=4,
        )
        drifted = {d["dimension"] for d in result.details["drifted_dimensions"]}
        warn = {d["dimension"] for d in result.details["warn_observed_dimensions"]}
        assert "simile_density" not in drifted, (
            "G24c 治本契约破坏:simile_density 不应进 hard fail"
        )
        assert "simile_density" in warn

    def test_exclamation_density_drift_is_warn_only(self) -> None:
        """exclamation_density 200% 漂移 → warn-only。"""
        baseline = _build_baseline_fp(exclamation_density=0.02)
        current = _build_baseline_fp(exclamation_density=0.06)  # 偏移 200%
        result = check_style_drift(
            baseline_fingerprint=baseline,
            current_fingerprint=current,
            current_version_id=4,
        )
        drifted = {d["dimension"] for d in result.details["drifted_dimensions"]}
        warn = {d["dimension"] for d in result.details["warn_observed_dimensions"]}
        assert "exclamation_density" not in drifted, (
            "G24c 治本契约破坏:exclamation_density 不应进 hard fail"
        )
        assert "exclamation_density" in warn

    def test_only_warn_dimensions_drift_passes_overall(self) -> None:
        """5 个 warn-only 维度全漂移 + 2 个 hard 维度不动 → 整体 passed=True。

        反向防御:wave 2-5 若把任一 warn-only 改回 hard,本测立刻失败
        (因为整体 passed 会变 False)。
        """
        baseline = _build_baseline_fp(
            paragraph_length_std=80.0,
            sentence_length_std=15.0,
            simile_density=0.05,
            exclamation_density=0.02,
            pronoun_freqs={"我": 0.05, "他": 0.05},
            total_chars=3000.0,
        )
        current = _build_baseline_fp(
            paragraph_length_std=160.0,
            sentence_length_std=30.0,
            simile_density=0.15,
            exclamation_density=0.06,
            pronoun_freqs={"我": 0.10, "他": 0.10},  # 偏移 100%
            total_chars=3000.0,
        )
        # paragraph_mean / sentence_mean 保持默认 baseline 值不动 → 不触发 hard
        result = check_style_drift(
            baseline_fingerprint=baseline,
            current_fingerprint=current,
            current_version_id=4,
        )
        assert result.passed is True, (
            "G24c 治本契约破坏:5 个 warn-only 维度全漂移但 2 个 hard 维度不动时,"
            "整体应 passed=True。若失败说明某个 warn-only 维度被改回 hard。"
        )


class TestG17GateAnomalyDowngrade:
    """R9:G17 异常维退出 rewrite 保序 HARD，前章 WARN 不得临时升级。

    luo-accept-v1 ch-0007 现场重演:卡超载 + 不可达成 must 把 v1 挤成 13 个巨段
    (段均 165.2)，旧 G17 把前章基线升级成 rewrite HARD；R9 ch-0002 又证明
    这会改写初稿真实打回原因。本类锁定:异常维只 WARN，健康维仍按 v1 保序。
    """

    def test_anomalous_v1_with_regressing_current_passes(self) -> None:
        """ch-0007 真实数值重演:v1=165.2 / 前章=72.05 / v2=65.35 → 应 pass。"""
        v1 = _build_baseline_fp(paragraph_length_mean=165.2)
        cross = _build_baseline_fp(paragraph_length_mean=72.05)
        current = _build_baseline_fp(paragraph_length_mean=65.35)
        result = check_style_drift(
            baseline_fingerprint=v1,
            current_fingerprint=current,
            current_version_id=2,
            cross_chapter_baseline_fingerprint=cross,
            cross_chapter_baseline_chapter_id="ch-0006",
        )
        assert result.passed is True
        assert result.details["baseline_substituted"] is False
        assert result.details["baseline_exempted_dimensions"] == [
            "paragraph_length_mean"
        ]
        assert result.details["baseline_source"] == (
            "v1;g17_warn_only_dimensions=paragraph_length_mean;"
            "cross_chapter_reference=ch-0006"
        )

    def test_both_anomalous_means_are_warn_only(self) -> None:
        """段均 / 句均同时异常时都退出 HARD，不改锚前章。"""
        v1 = _build_baseline_fp(paragraph_length_mean=165.2, sentence_length_mean=50.0)
        cross = _build_baseline_fp(
            paragraph_length_mean=72.05, sentence_length_mean=28.0
        )
        # current 两维都偏离 v1 的常规 HARD 阈值，但 v1 两维本身不可靠，
        # 所以只记 WARN，不得借 rewrite 把前章数值升级成 HARD。
        current = _build_baseline_fp(
            paragraph_length_mean=65.35, sentence_length_mean=30.0
        )
        result = check_style_drift(
            baseline_fingerprint=v1,
            current_fingerprint=current,
            current_version_id=2,
            cross_chapter_baseline_fingerprint=cross,
            cross_chapter_baseline_chapter_id="ch-0006",
        )
        assert result.passed is True
        assert result.details["baseline_substituted"] is False
        assert result.details["baseline_exempted_dimensions"] == [
            "paragraph_length_mean",
            "sentence_length_mean",
        ]

    def test_paragraph_only_anomaly_preserves_healthy_sentence_and_warn_dims(
        self,
    ) -> None:
        """段均单独异常时，健康句均和 WARN 维度都必须继续锚定本章 v1。"""
        v1 = _build_baseline_fp(
            paragraph_length_mean=165.2,
            sentence_length_mean=37.0,
            simile_density=0.10,
        )
        cross = _build_baseline_fp(
            paragraph_length_mean=72.05,
            # 37 vs 25 向上 48%，未超过 G17 的 50% 异常阈值；若错误地
            # 错把前章值升级成 HARD，current=37 会相对 25 漂移 48%。
            sentence_length_mean=25.0,
            simile_density=0.50,
        )
        current = _build_baseline_fp(
            paragraph_length_mean=72.0,
            sentence_length_mean=37.0,
            simile_density=0.10,
        )

        result = check_style_drift(
            baseline_fingerprint=v1,
            current_fingerprint=current,
            current_version_id=2,
            cross_chapter_baseline_fingerprint=cross,
            cross_chapter_baseline_chapter_id="ch-0006",
        )

        assert result.passed is True
        assert result.details["baseline_exempted_dimensions"] == [
            "paragraph_length_mean",
        ]
        assert result.details["baseline_source"] == (
            "v1;g17_warn_only_dimensions=paragraph_length_mean;"
            "cross_chapter_reference=ch-0006"
        )
        assert result.details["prompt_visible_mean_anchors"][
            "sentence_length_mean"
        ] == {"raw_baseline_value": 37.0, "anchor_value": 37.0}
        warn_dims = {
            item["dimension"] for item in result.details["warn_observed_dimensions"]
        }
        assert "simile_density" not in warn_dims

    def test_anomalous_dimension_does_not_become_cross_chapter_hard(self) -> None:
        """异常段均即使仍偏前章，也只能由跨章 WARN 观察，不能在这里 HARD。"""
        v1 = _build_baseline_fp(paragraph_length_mean=165.2)
        cross = _build_baseline_fp(paragraph_length_mean=72.05)
        current = _build_baseline_fp(paragraph_length_mean=150.0)
        result = check_style_drift(
            baseline_fingerprint=v1,
            current_fingerprint=current,
            current_version_id=2,
            cross_chapter_baseline_fingerprint=cross,
            cross_chapter_baseline_chapter_id="ch-0006",
        )
        assert result.passed is True
        assert result.details["drifted_dimensions"] == []
        assert result.details["baseline_exempted_dimensions"] == [
            "paragraph_length_mean"
        ]

    def test_r9_setting_rewrite_is_not_blocked_by_new_paragraph_ruler(self) -> None:
        """R9 ch-0002:v1 因 setting 打回；122 vs 62 的跨章 WARN 不得拦 v4=104.79。"""
        v1 = _build_baseline_fp(
            paragraph_length_mean=122.1875, sentence_length_mean=28.4222
        )
        cross = _build_baseline_fp(
            paragraph_length_mean=62.296875, sentence_length_mean=24.70625
        )
        current = _build_baseline_fp(
            paragraph_length_mean=104.79, sentence_length_mean=28.0
        )

        result = check_style_drift(
            baseline_fingerprint=v1,
            current_fingerprint=current,
            current_version_id=4,
            cross_chapter_baseline_fingerprint=cross,
            cross_chapter_baseline_chapter_id="ch-0001",
        )

        assert result.passed is True
        assert result.details["baseline_exempted_dimensions"] == [
            "paragraph_length_mean"
        ]
        assert result.details["drifted_dimensions"] == []

    def test_healthy_v1_keeps_v1_baseline_unchanged(self) -> None:
        """v1 健康(偏前章 <50%)→ 不替换,G5 原行为原样保留。"""
        v1 = _build_baseline_fp(paragraph_length_mean=75.0)
        cross = _build_baseline_fp(paragraph_length_mean=72.05)
        current = _build_baseline_fp(paragraph_length_mean=30.0)
        result = check_style_drift(
            baseline_fingerprint=v1,
            current_fingerprint=current,
            current_version_id=2,
            cross_chapter_baseline_fingerprint=cross,
            cross_chapter_baseline_chapter_id="ch-0006",
        )
        assert result.passed is False
        assert result.details["baseline_substituted"] is False
        assert result.details["baseline_source"] == "v1"
        drifted = result.details["drifted_dimensions"]
        assert abs(drifted[0]["baseline_value"] - 75.0) < 1e-9

    def test_no_cross_baseline_keeps_original_behavior(self) -> None:
        """首章 / wiring off / 旧 caller(cross=None)→ 行为与修复前逐字节等价。"""
        v1 = _build_baseline_fp(paragraph_length_mean=165.2)
        current = _build_baseline_fp(paragraph_length_mean=65.35)
        result = check_style_drift(
            baseline_fingerprint=v1,
            current_fingerprint=current,
            current_version_id=2,
        )
        assert result.passed is False  # 旧行为:按 v1 判漂移
        assert result.details["baseline_substituted"] is False

    def test_threshold_shared_with_writer_downgrade(self) -> None:
        """两端一致性铁律:Writer 与闸门降级必须共用同一阈值常量。

        本测试是修复1 的核心防回退 — 两端阈值/条件分叉正是 ch-0007 死锁根因。
        """
        from src.core.agents.writer import _V1_BASELINE_ANOMALY_THRESHOLD
        from src.core.rules.style_drift_validator import (
            V1_BASELINE_ANOMALY_THRESHOLD,
        )

        assert _V1_BASELINE_ANOMALY_THRESHOLD == V1_BASELINE_ANOMALY_THRESHOLD
        assert V1_BASELINE_ANOMALY_THRESHOLD == 0.50


class TestG17SentenceMeanAnomaly:
    """v2 w2-s2:G17 异常检测扩句均维度(drift-ab-off ch-0005 句均盲区死锁)。

    现场:v1 句均 60.6(畸形超长句)偏前章 37.9 达 +60% > 50%,但段均 147.8
    偏前章 175.1 仅 -16% < 50% → 原"只看段均"的 G17 漏判 → 闸门锚畸形 v1 句均
    → writer 补字数时句均反复横跳(36/96/31)永够不着 v1=60.6 的 ±30% 窗口 →
    rewrite budget 耗尽 human_intervention。R9 后异常句均退出保序 HARD，
    不锁畸形 v1，也不把前章参考升级成新 HARD。
    """

    def test_v1_sentence_anomaly_alone_triggers_warn_only(self) -> None:
        """段均健康、句均异常时只降句均，健康段均仍按 v1 保序。"""
        v1 = _build_baseline_fp(paragraph_length_mean=147.8, sentence_length_mean=60.6)
        cross = _build_baseline_fp(
            paragraph_length_mean=175.1, sentence_length_mean=37.9
        )
        # current 句均 36:vs v1=60.6 偏 -41% >30%，但异常 v1 句均不能作为
        # 保序 HARD；段均仍相对健康 v1 处于 ±50% 内。
        current = _build_baseline_fp(
            paragraph_length_mean=120.0, sentence_length_mean=36.0
        )
        result = check_style_drift(
            baseline_fingerprint=v1,
            current_fingerprint=current,
            current_version_id=2,
            cross_chapter_baseline_fingerprint=cross,
            cross_chapter_baseline_chapter_id="ch-0004",
        )
        assert result.passed is True
        assert result.details["baseline_substituted"] is False
        assert result.details["baseline_exempted_dimensions"] == [
            "sentence_length_mean"
        ]
        assert result.details["baseline_source"] == (
            "v1;g17_warn_only_dimensions=sentence_length_mean;"
            "cross_chapter_reference=ch-0004"
        )
        assert result.details["prompt_visible_mean_anchors"][
            "paragraph_length_mean"
        ] == {"raw_baseline_value": 147.8, "anchor_value": 148.0}

    def test_is_v1_baseline_anomalous_helper_both_dims(self) -> None:
        """共享 helper:段均或句均任一**向上超阈**即异常;健康 / 超短不异常。"""
        from src.core.rules.style_drift_validator import is_v1_baseline_anomalous

        prev = {"paragraph_length_mean": 80.0, "sentence_length_mean": 35.0}
        # 句均异常(+60%)段均健康(+12%)
        assert is_v1_baseline_anomalous(
            {"paragraph_length_mean": 90.0, "sentence_length_mean": 56.0}, prev
        )
        # 段均异常(+88%)句均健康(+3%)
        assert is_v1_baseline_anomalous(
            {"paragraph_length_mean": 150.0, "sentence_length_mean": 36.0}, prev
        )
        # 两维都健康
        assert not is_v1_baseline_anomalous(
            {"paragraph_length_mean": 90.0, "sentence_length_mean": 38.0}, prev
        )
        # 维度缺失 / 0 值 → 该维度跳过(不计异常)
        assert not is_v1_baseline_anomalous(
            {"paragraph_length_mean": 0.0, "sentence_length_mean": 0.0}, prev
        )
        # v2 间隙问题② 修(2026-06-13):**只向上(超长)触发**。超短不触发 —
        # 根治"高潮章戏剧短段 v1 被全书锚替换 → rewrite 强制往长"HARD 致伤。
        # 段均超短 −60%(高潮 58.9 vs 全书锚 147.9 同型)→ 不异常
        assert not is_v1_baseline_anomalous(
            {"paragraph_length_mean": 58.9, "sentence_length_mean": 34.0},
            {"paragraph_length_mean": 147.9, "sentence_length_mean": 34.0},
        )
        # 句均超短 −54% 同样不触发
        assert not is_v1_baseline_anomalous(
            {"paragraph_length_mean": 90.0, "sentence_length_mean": 16.0}, prev
        )
        # 边界回归:超长病态 v1 仍触发(G17 真正该治的 = 卡超载挤巨段)
        assert is_v1_baseline_anomalous(
            {"paragraph_length_mean": 165.0, "sentence_length_mean": 35.0},
            {"paragraph_length_mean": 72.0, "sentence_length_mean": 35.0},
        )  # luo-accept-v1 ch-0007 v1=165 vs 前章 72 +129% 超长病态

    def test_writer_and_gate_share_anomaly_helper(self) -> None:
        """两端一致性铁律升级:writer G17 与闸门共用带维度明细的 SSOT。

        本测试是 w2-s2 债① 核心防回退 — 两端判定条件分叉(一端看段均、一端看
        段均+句均)会重演 ch-0007 / drift-ab-off ch-0005 两把尺打架。
        """
        import inspect

        from src.core.agents import writer as writer_module
        from src.core.rules import style_drift_validator as gate_module

        # 同一函数对象(writer import 自 gate),不是各自实现。它同时供判定与
        # Writer 异常文案使用，杜绝句均触发却错报段均。
        assert (
            writer_module.v1_baseline_anomaly_details
            is gate_module.v1_baseline_anomaly_details
        )
        # writer _format_rewrite_baseline 源码确实调用它并消费明细。
        src = inspect.getsource(writer_module._format_rewrite_baseline)
        assert "v1_baseline_anomaly_details" in src
        assert "baseline_anomalies" in src

    def test_anomaly_details_are_exact_and_zero_denominator_safe(self) -> None:
        """只返回真触发维度；无效段均基线不影响句均触发，也不发生除零。"""
        from src.core.rules.style_drift_validator import (
            v1_baseline_anomaly_details,
        )

        details = v1_baseline_anomaly_details(
            {"paragraph_length_mean": 80.0, "sentence_length_mean": 60.0},
            {"paragraph_length_mean": 0.0, "sentence_length_mean": 30.0},
        )

        assert len(details) == 1
        assert details[0].dimension == "sentence_length_mean"
        assert details[0].reason == "inflated"
        assert details[0].relative_delta == 1.0

    def test_climax_short_v1_not_substituted_no_forced_lengthen(self) -> None:
        """v2 间隙问题② 端到端:高潮章短段 v1 + 长基线 → 不替换基线 →
        重写版仍写短段时 G17 不判漂移(不被强制往长)。

        固化确定性致伤验证(2026-06-13):全书锚 ON 时高潮章 v1=58.9 偏全书锚
        147.9 −60%,**修复前** is_v1_baseline_anomalous 误判异常 → 闸门改锚
        全书锚 147.9 → v2 写短段(60)偏 147.9 −59% > 50% → fail → 强制往长。
        **修复后**(只超长触发):v1 短不替换 → v2(60)vs v1(58.9)偏 +2% → pass。
        """
        v1_climax = _build_baseline_fp(
            paragraph_length_mean=58.9, sentence_length_mean=34.0
        )
        book_anchor = _build_baseline_fp(
            paragraph_length_mean=147.9, sentence_length_mean=34.1
        )
        v2_still_short = _build_baseline_fp(
            paragraph_length_mean=60.0, sentence_length_mean=34.0
        )
        result = check_style_drift(
            baseline_fingerprint=v1_climax,
            current_fingerprint=v2_still_short,
            current_version_id=2,
            cross_chapter_baseline_fingerprint=book_anchor,
            cross_chapter_baseline_chapter_id="ch-0003",
        )
        assert result.passed is True, (
            "问题② 致伤复发:高潮章短段重写版被 G17 判漂移强制往长 — "
            "is_v1_baseline_anomalous 应只对超长 v1 触发基线替换"
        )
        assert result.details.get("baseline_substituted") is False

    def test_overlong_v1_dimension_is_exempted_regression(self) -> None:
        """回归保护:超长 v1 不锁死 rewrite，也不把前章值升级成 HARD。"""
        v1_overlong = _build_baseline_fp(
            paragraph_length_mean=165.0, sentence_length_mean=35.0
        )
        prev = _build_baseline_fp(paragraph_length_mean=72.0, sentence_length_mean=35.0)
        v2_regressed = _build_baseline_fp(
            paragraph_length_mean=70.0, sentence_length_mean=35.0
        )
        result = check_style_drift(
            baseline_fingerprint=v1_overlong,
            current_fingerprint=v2_regressed,
            current_version_id=2,
            cross_chapter_baseline_fingerprint=prev,
            cross_chapter_baseline_chapter_id="ch-0006",
        )
        assert result.passed is True
        assert result.details.get("baseline_substituted") is False
        assert result.details["baseline_exempted_dimensions"] == [
            "paragraph_length_mean"
        ]


class TestG17GateWiringInvariant:
    """call site 不变量(对齐 TestG22bCallSiteInvariant 模式):wiring 不掉线。

    [[feedback_test_full_wiring_not_just_mechanism_ends]]:G17 当年只接 writer
    一半的教训 — 机制两端都对,中间没接通照样生产静默失效。
    """

    def test_pipeline_passes_cross_baseline_to_gate(self) -> None:
        from pathlib import Path

        src = Path("src/core/rules/validator_pipeline.py").read_text(encoding="utf-8")
        # check_style_drift 调用块内必须透传跨章基线两参
        call_start = src.index("check_style_drift(")
        call_block = src[call_start : call_start + 600]
        assert (
            "cross_chapter_baseline_fingerprint=cross_chapter_baseline_fingerprint"
            in call_block
        ), "validator_pipeline 不再向 G5 闸门透传跨章基线 — G17 闸门补全被回退"
        assert (
            "cross_chapter_baseline_chapter_id=cross_chapter_baseline_chapter_id"
            in call_block
        )

    def test_orchestrator_single_path_keeps_cross_baseline_without_draft_pool(
        self,
    ) -> None:
        from pathlib import Path

        src = Path("src/core/orchestrator/chapter_orchestrator.py").read_text(
            encoding="utf-8"
        )
        # v2 w2-s2 债②:透传变量改 effective 基线 cross_chapter_anchor_fp
        # (OFF 时 == 前章基线零回归,ON 时全书锚;闸门与 writer 同源)。
        needle = "cross_chapter_baseline_fingerprint=cross_chapter_anchor_fp"
        count = src.count(needle)
        assert count == 4, (
            "orchestrator 的 checkpoint 重算、恢复计划、revise 计划和唯一主审路径"
            f"都应透传跨章基线，实际 {count} — G17 闸门接线漂移"
        )
        assert "draft_pool" not in src


class TestWordCountTrimSentenceMeanExemptionW35S7:
    """v2 W3.5-s7:trim×style_drift 冲突修复(governance §2.47)。

    根因(s7-accept ch-0010 一手实证):``word_count_trim`` 是受控 LLM 后处理
    (整段砍水 + 衔接改顺),必然重写句长结构(deepseek 长句被正常化到接近健康
    锚);而 baseline(v1)若未触发 trim(自然 ≤cap)则保留原始长句 → 两版"长度
    治理状态"不一致 → 句均偏移被误判风格漂移 → rewrite → deepseek 又写长 →
    trim 再正常化 → 死循环耗尽 budget 升人工(v1 句均 47.1 未 trim,v2/v3 trim 后
    29.9/28.7 偏移 0.365/0.392 > 0.3 全 fail,而未 trim 的 v4 句均 36.5 反而过)。

    修复:current 被 word_count_trim 改写过 → ``sentence_length_mean`` 维度降
    warn-only。**仅句均豁免**(段落均值硬约束保留 mode collapse 真信号),
    **仅 trim 版本豁免**(未 trim 版本全维度照常,防真句长漂移漏放)。
    """

    def test_trim_version_sentence_mean_drift_becomes_warn_only(self) -> None:
        """ch-0010 现场:baseline 句均 47.1 vs trim 后 29.9(偏移 0.365 > 0.3)。
        current_word_count_trimmed=True → 句均进 warn_observed 不进 drifted,
        passed=True。"""
        baseline = _build_baseline_fp(sentence_length_mean=47.1)
        current = _build_baseline_fp(sentence_length_mean=29.9)
        result = check_style_drift(
            baseline_fingerprint=baseline,
            current_fingerprint=current,
            current_version_id=2,
            current_word_count_trimmed=True,
        )
        assert result.passed is True
        drifted = [d["dimension"] for d in result.details["drifted_dimensions"]]
        assert "sentence_length_mean" not in drifted
        warned = [d["dimension"] for d in result.details["warn_observed_dimensions"]]
        assert "sentence_length_mean" in warned
        assert result.details["sentence_mean_warn_only_due_to_trim"] is True

    def test_non_trim_version_sentence_mean_drift_still_fails(self) -> None:
        """同样句均漂移,但 current_word_count_trimmed=False(默认)→ 句均仍硬
        约束,passed=False(回退前行为不变,防过度放宽 / 真漂移漏放)。"""
        baseline = _build_baseline_fp(sentence_length_mean=47.1)
        current = _build_baseline_fp(sentence_length_mean=29.9)
        result = check_style_drift(
            baseline_fingerprint=baseline,
            current_fingerprint=current,
            current_version_id=2,
        )
        assert result.passed is False
        drifted = [d["dimension"] for d in result.details["drifted_dimensions"]]
        assert "sentence_length_mean" in drifted
        assert result.details["sentence_mean_warn_only_due_to_trim"] is False

    def test_trim_exemption_does_not_cover_paragraph_mean(self) -> None:
        """trim 版本句均豁免,但段落均值漂移(200→80,偏移 0.6 > 0.5)仍 fail —
        段落维度不豁免(mode collapse 真信号保留,防过度豁免)。"""
        baseline = _build_baseline_fp(
            sentence_length_mean=47.1, paragraph_length_mean=200.0
        )
        current = _build_baseline_fp(
            sentence_length_mean=29.9, paragraph_length_mean=80.0
        )
        result = check_style_drift(
            baseline_fingerprint=baseline,
            current_fingerprint=current,
            current_version_id=2,
            current_word_count_trimmed=True,
        )
        assert result.passed is False
        drifted = [d["dimension"] for d in result.details["drifted_dimensions"]]
        assert "paragraph_length_mean" in drifted
        assert "sentence_length_mean" not in drifted  # 句均仍豁免

    def test_trim_version_without_sentence_drift_still_passes(self) -> None:
        """trim 版本但句均没漂移(30→30)→ passed=True,豁免不影响正常通过。"""
        baseline = _build_baseline_fp(sentence_length_mean=30.0)
        current = _build_baseline_fp(sentence_length_mean=30.0)
        result = check_style_drift(
            baseline_fingerprint=baseline,
            current_fingerprint=current,
            current_version_id=2,
            current_word_count_trimmed=True,
        )
        assert result.passed is True

    def test_orchestrator_passes_trim_flag_to_single_pipeline(self) -> None:
        """防回退(全链路接线):orchestrator 唯一稿
        ``_apply_validator_pipeline`` 调用点透传 ``current_word_count_trimmed``
        (从 ``wc_trimmed_by_version`` 查)。否则生产中 trim 版本 style_drift 句均
        豁免静默失效 → ch-0010 死循环复发。"""
        from pathlib import Path

        src = Path("src/core/orchestrator/chapter_orchestrator.py").read_text(
            encoding="utf-8"
        )
        count = src.count("current_word_count_trimmed=wc_trimmed_by_version.get(")
        assert count == 1, (
            f"orchestrator → 唯一稿 pipeline trim flag 透传调用点应为 1,"
            f"实际 {count} — trim×style_drift 豁免接线漂移"
        )


class TestPromptVisibleMeanAnchorR8:
    """R8 ch-0003:Writer 可见整数锚与 HARD 判分必须使用同一把尺。"""

    def test_real_sentence_mean_pair_passes_without_trim_exemption(self) -> None:
        baseline = _build_baseline_fp(
            sentence_length_mean=49.0961538461538,
        )
        current = _build_baseline_fp(
            sentence_length_mean=34.3584905660377,
        )

        result = check_style_drift(
            baseline_fingerprint=baseline,
            current_fingerprint=current,
            current_version_id=2,
            current_word_count_trimmed=False,
        )

        assert result.passed is True
        assert result.details["sentence_mean_warn_only_due_to_trim"] is False
        anchor = result.details["prompt_visible_mean_anchors"]["sentence_length_mean"]
        assert anchor["raw_baseline_value"] == 49.0961538461538
        assert anchor["anchor_value"] == 49.0

    def test_real_sentence_anchor_still_rejects_true_overage(self) -> None:
        baseline = _build_baseline_fp(
            sentence_length_mean=49.0961538461538,
        )
        current = _build_baseline_fp(sentence_length_mean=34.0)

        result = check_style_drift(
            baseline_fingerprint=baseline,
            current_fingerprint=current,
            current_version_id=2,
        )

        assert result.passed is False
        sentence = next(
            item
            for item in result.details["drifted_dimensions"]
            if item["dimension"] == "sentence_length_mean"
        )
        assert sentence["baseline_value"] == 49.0
        assert sentence["raw_baseline_value"] == 49.0961538461538

    def test_paragraph_boundary_uses_same_prompt_visible_anchor(self) -> None:
        baseline = _build_baseline_fp(paragraph_length_mean=100.49)
        current = _build_baseline_fp(paragraph_length_mean=50.0)

        result = check_style_drift(
            baseline_fingerprint=baseline,
            current_fingerprint=current,
            current_version_id=2,
        )

        assert result.passed is True
        anchor = result.details["prompt_visible_mean_anchors"]["paragraph_length_mean"]
        assert anchor == {
            "raw_baseline_value": 100.49,
            "anchor_value": 100.0,
        }

    def test_quantizer_matches_writer_zero_decimal_format(self) -> None:
        for value in (13.5, 49.0961538461538, 100.49, 224.652173913043):
            assert quantize_prompt_visible_style_mean(value) == float(
                format(value, ".0f")
            )
