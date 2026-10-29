"""G17/R9 writer rewrite 保序尺与真实失败原因路由测试。

异常 v1 维度只 WARN；健康 v1 维度仍是 rewrite 保序 HARD。
"""

from __future__ import annotations

from src.core.agents.writer import (
    _V1_BASELINE_ANOMALY_THRESHOLD,
    _format_rewrite_baseline,
    _word_count_failure_reason,
)


def _make_fingerprint(*, paragraph_mean: float, **extra: float) -> dict[str, float]:
    """构造 fingerprint 测试 fixture。"""
    base: dict[str, float] = {
        "paragraph_length_mean": paragraph_mean,
        "sentence_length_mean": 20.0,
        "simile_density": 0.1,
        "pronoun_freq_他": 0.005,
        "pronoun_freq_她": 0.003,
        "total_chars": 3000.0,
    }
    base.update(extra)
    return base


class TestG17NormalPath:
    """v1 偏前章 baseline < 50% → 仍用 v1 锚(原 G11 行为)。"""

    def test_v1_normal_uses_v1_as_anchor(self) -> None:
        """v1 paraMean=50 vs 前章 baseline=53 → 偏 5.7% < 50% → 用 v1 锚。"""
        v1_fp = _make_fingerprint(paragraph_mean=50.0)
        prev_fp = _make_fingerprint(paragraph_mean=53.0)
        result = _format_rewrite_baseline(
            prev_version_fingerprint=v1_fp,
            validator_failures=None,
            prev_chapter_baseline_fingerprint=prev_fp,
        )
        assert "### v1 风格指纹(rewrite 保序，不改写真实失败原因)" in result
        # paraMean 渲染应是 v1 值(50 字)而非前章值(53 字)
        assert "约 50 字" in result
        # 不应出现 G17 warn 段
        assert "v1 风格指纹异常维度(G17/R9 因果路由)" not in result

    def test_v1_at_threshold_below_uses_v1(self) -> None:
        """v1 偏前章 49%(< 50% 阈值)→ 仍用 v1。"""
        v1_fp = _make_fingerprint(paragraph_mean=27.0)  # 偏 53 → (53-27)/53 = 49%
        prev_fp = _make_fingerprint(paragraph_mean=53.0)
        result = _format_rewrite_baseline(
            prev_version_fingerprint=v1_fp,
            validator_failures=None,
            prev_chapter_baseline_fingerprint=prev_fp,
        )
        assert "### v1 风格指纹(rewrite 保序，不改写真实失败原因)" in result
        assert "v1 风格指纹异常维度(G17/R9 因果路由)" not in result

    def test_rewrite_prompt_uses_shared_visible_mean_quantization(self) -> None:
        """R8 ch-0003 实值：段均与句均都渲染为 validator 使用的整数锚。"""
        v1_fp = _make_fingerprint(
            paragraph_mean=224.652173913043,
            sentence_length_mean=49.0961538461538,
        )
        result = _format_rewrite_baseline(
            prev_version_fingerprint=v1_fp,
            validator_failures=None,
            prev_chapter_baseline_fingerprint=None,
        )

        assert "paragraph_length_mean):约 225 字" in result
        assert "sentence_length_mean):约 49 字" in result


class TestG17AnomalyDowngradePath:
    """v1 异常维只 WARN，不把前章参考升级成 rewrite HARD。"""

    def test_v1_collapsed_paragraph_uses_actual_collapse_rule(self) -> None:
        """v1 段均 13.5 已命中初稿 collapse HARD，不能再把前章值另造硬尺。"""
        v1_fp = _make_fingerprint(paragraph_mean=13.5)
        prev_fp = _make_fingerprint(paragraph_mean=53.13)
        result = _format_rewrite_baseline(
            prev_version_fingerprint=v1_fp,
            validator_failures=None,
            prev_chapter_baseline_fingerprint=prev_fp,
        )
        assert "### ⚠️ v1 风格指纹异常维度(G17/R9 因果路由)" in result
        assert "偏移 75%" in result
        assert "命中 paragraph collapse 初稿硬闸" in result
        assert "v1 约 14 字；前章约 53 字" in result
        assert "本轮 WARN-only，不设同章数值硬区间" in result
        assert "段落均值 [HARD] 绝对允许区间" not in result

    def test_v1_far_above_baseline_is_warn_only(self) -> None:
        """v1 段均 200 vs 前章 53 是跨章 WARN，不因 unrelated rewrite 变 HARD。"""
        v1_fp = _make_fingerprint(paragraph_mean=200.0)
        prev_fp = _make_fingerprint(paragraph_mean=53.0)
        result = _format_rewrite_baseline(
            prev_version_fingerprint=v1_fp,
            validator_failures=None,
            prev_chapter_baseline_fingerprint=prev_fp,
        )
        assert "### ⚠️ v1 风格指纹异常维度(G17/R9 因果路由)" in result
        assert "v1 约 200 字；前章约 53 字" in result
        assert "段落均值 [HARD] 绝对允许区间" not in result

    def test_r9_setting_rewrite_does_not_invent_paragraph_hard_gate(self) -> None:
        """R9 ch-0002:v1 因 setting 打回，段均跨章 WARN 不得改写失败原因。"""
        v1_fp = _make_fingerprint(
            paragraph_mean=122.1875,
            sentence_length_mean=28.4222,
            total_chars=3972.0,
        )
        prev_fp = _make_fingerprint(
            paragraph_mean=62.296875,
            sentence_length_mean=24.70625,
            total_chars=4113.0,
        )

        result = _format_rewrite_baseline(
            prev_version_fingerprint=v1_fp,
            validator_failures=[
                {"name": "setting_consistency", "details": {"reason": "location"}}
            ],
            prev_chapter_baseline_fingerprint=prev_fp,
        )

        assert "`paragraph_length_mean`：v1 **122.2**，前章 baseline **62.3**" in result
        assert "paragraph_length_mean):v1 约 122 字；前章约 62 字" in result
        assert "本轮 WARN-only，不设同章数值硬区间" in result
        assert "name`:setting_consistency" in result
        assert "段落均值 [HARD] 绝对允许区间" not in result
        assert "31.0–93.0" not in result
        assert "至少 52 个非空正文段" not in result
        # 健康句均仍是 rewrite 保序 HARD；只降真正异常的段均。
        assert "句长均值 [HARD] 绝对允许区间:19.6–36.4 字" in result

    def test_sentence_only_anomaly_reports_sentence_only(self) -> None:
        """句均单独触发时不得继续套用旧模板错报“段均偏离”。"""
        v1_fp = _make_fingerprint(
            paragraph_mean=80.0,
            sentence_length_mean=60.0,
        )
        prev_fp = _make_fingerprint(
            paragraph_mean=0.0,
            sentence_length_mean=30.0,
        )

        result = _format_rewrite_baseline(
            prev_version_fingerprint=v1_fp,
            validator_failures=None,
            prev_chapter_baseline_fingerprint=prev_fp,
        )

        anomaly_section = result.split("### v1 风格指纹(rewrite 保序", 1)[0]
        assert "`sentence_length_mean`" in anomaly_section
        assert "`paragraph_length_mean`" not in anomaly_section
        assert "向上偏移 100%" in anomaly_section
        # 只降真正异常的句均；健康段均继续按 v1 保序。
        assert "paragraph_length_mean):约 80 字" in result
        assert "sentence_length_mean):v1 约 60 字；前章约 30 字" in result
        assert "段落均值 [HARD] 绝对允许区间" in result
        assert "句长均值 [HARD] 绝对允许区间" not in result

    def test_downgrade_keeps_v1_pov_and_simile_reference(self) -> None:
        """异常降级不能把上一章 POV/比喻偷换成本章要求。"""
        v1_fp = _make_fingerprint(
            paragraph_mean=200.0,
            simile_density=0.222,
            pronoun_freq_他=0.02,
            pronoun_freq_我=0.0,
        )
        prev_fp = _make_fingerprint(
            paragraph_mean=50.0,
            simile_density=0.999,
            pronoun_freq_他=0.0,
            pronoun_freq_我=0.02,
        )

        result = _format_rewrite_baseline(
            prev_version_fingerprint=v1_fp,
            validator_failures=None,
            prev_chapter_baseline_fingerprint=prev_fp,
        )

        assert "dominant_pov):third_person" in result
        assert "simile_density):约 0.222" in result
        assert "约 0.999" not in result
        assert "偏移只记 WARN，不单独打回" in result


class TestG17EdgeCases:
    """边界 case:无前章 baseline / 双 None / paragraph_mean=0。"""

    def test_no_prev_chapter_baseline_uses_v1(self) -> None:
        """无前章 baseline → 仍用 v1 锚(向后兼容首章 / 异常路径)。"""
        v1_fp = _make_fingerprint(paragraph_mean=13.5)
        result = _format_rewrite_baseline(
            prev_version_fingerprint=v1_fp,
            validator_failures=None,
            prev_chapter_baseline_fingerprint=None,
        )
        assert "### v1 风格指纹(rewrite 保序，不改写真实失败原因)" in result
        assert "v1 风格指纹异常维度(G17/R9 因果路由)" not in result
        # paraMean 渲染 v1 值
        assert "约 14 字" in result  # 13.5 round to 14 with :.0f

    def test_both_none_renders_fallback_text(self) -> None:
        """v1 + prev 都 None → 走 fingerprint 缺失分支。"""
        result = _format_rewrite_baseline(
            prev_version_fingerprint=None,
            validator_failures=None,
            prev_chapter_baseline_fingerprint=None,
        )
        # 应有 fingerprint 缺失提示
        assert "v1 fingerprint 缺失" in result

    def test_prev_paragraph_mean_zero_uses_v1(self) -> None:
        """前章 paraMean=0(异常)→ 跳过 G17 异常检测,仍用 v1。"""
        v1_fp = _make_fingerprint(paragraph_mean=13.5)
        prev_fp = _make_fingerprint(paragraph_mean=0.0)
        result = _format_rewrite_baseline(
            prev_version_fingerprint=v1_fp,
            validator_failures=None,
            prev_chapter_baseline_fingerprint=prev_fp,
        )
        assert "v1 风格指纹异常维度(G17/R9 因果路由)" not in result

    def test_v1_paragraph_mean_zero_uses_v1(self) -> None:
        """v1 paraMean=0(异常)→ 跳过 G17 异常检测,走 v1 缺失分支语义保持。"""
        v1_fp = _make_fingerprint(paragraph_mean=0.0)
        prev_fp = _make_fingerprint(paragraph_mean=53.0)
        result = _format_rewrite_baseline(
            prev_version_fingerprint=v1_fp,
            validator_failures=None,
            prev_chapter_baseline_fingerprint=prev_fp,
        )
        assert "v1 风格指纹异常维度(G17/R9 因果路由)" not in result


class TestG17ThresholdConstant:
    """阈值常量字面契约。"""

    def test_threshold_value(self) -> None:
        """阈值固定 50%(对齐 G5 同章 PARAGRAPH_LENGTH_MEAN_DRIFT_THRESHOLD)。"""
        assert _V1_BASELINE_ANOMALY_THRESHOLD == 0.50


def _wc_failure(reason: str) -> list[dict]:
    return [
        {
            "name": "word_count",
            "details": {
                "actual_word_count": 5517 if reason == "above_max" else 2100,
                "word_count_min": 2500,
                "word_count_max": 4500,
                "reason": reason,
            },
        }
    ]


def _style_failure() -> list[dict]:
    """R8 ch-0003 v2 的真实非字数失败形状。"""
    return [
        {
            "name": "style_drift",
            "details": {
                "baseline_version_id": 1,
                "current_version_id": 2,
                "drifted_dimensions": [
                    {
                        "dimension": "sentence_length_mean",
                        "baseline_value": 49.0961538461538,
                        "current_value": 34.3584905660377,
                        "relative_drift": 0.30017958894086866,
                        "threshold": 0.3,
                    }
                ],
            },
        }
    ]


class TestS3RevisitWordCountDirection:
    """W3.5-s3-revisit RC2:rewrite 收敛指导按 word_count 越界方向分支。

    s6 端到端根因:原 ``_format_rewrite_baseline`` 单向"扩内容达下限"(qwen undershoot
    时代 G11),deepseek overshoot(above_max)撞扩容框架越写越长(ch-0001 rewrite
    4113→4837→5517 反常点)。R8 ch-0003 又证明非字数失败不能落入扩容分支。
    本组锁定 above_max → **压缩**、below_min → **扩容**、其余 → **定向修复**。"""

    def test_reason_detection(self) -> None:
        assert _word_count_failure_reason(_wc_failure("above_max")) == "above_max"
        assert _word_count_failure_reason(_wc_failure("below_min")) == "below_min"
        assert _word_count_failure_reason([]) is None
        assert _word_count_failure_reason(None) is None
        # reviewer 自然 verdict=rewrite(只带非 word_count failure)→ None
        assert (
            _word_count_failure_reason([{"name": "must_achieve", "details": {}}])
            is None
        )

    def test_above_max_emits_compression_not_expansion(self) -> None:
        """above_max(写超长)→ 收敛/压缩指导,**不出现**扩容到下限的框架。"""
        v1_fp = _make_fingerprint(paragraph_mean=50.0)
        result = _format_rewrite_baseline(
            prev_version_fingerprint=v1_fp,
            validator_failures=_wc_failure("above_max"),
            prev_chapter_baseline_fingerprint=None,
        )
        # 收敛框架 + above_max 核心目标
        assert "收敛方式" in result
        assert "收敛到 `word_count_max` 以内" in result
        assert "above_max" in result
        # 反例针对超长:别继续写更长
        assert "不要继续写更长" in result
        # 不出现扩容到下限的框架(原单向 bug 的标志)
        assert "### 扩展方式(达字数下限" not in result
        assert "核心目标:扩内容达字数下限" not in result

    def test_below_min_emits_expansion_existing_behavior(self) -> None:
        """below_min(写不足)→ 扩容指导(既有 qwen undershoot 行为,零回归)。"""
        v1_fp = _make_fingerprint(paragraph_mean=50.0)
        result = _format_rewrite_baseline(
            prev_version_fingerprint=v1_fp,
            validator_failures=_wc_failure("below_min"),
            prev_chapter_baseline_fingerprint=None,
        )
        assert "### 扩展方式(达字数下限" in result
        assert "核心目标:扩内容达字数下限" in result
        assert "收敛方式" not in result

    def test_real_style_failure_emits_targeted_fix_not_expansion(self) -> None:
        """R8 v2:style_drift 且字数合法 → 只修风格,不得注入四类扩写。"""
        v1_fp = _make_fingerprint(
            paragraph_mean=224.652173913043,
            sentence_length_mean=49.0961538461538,
        )
        result = _format_rewrite_baseline(
            prev_version_fingerprint=v1_fp,
            validator_failures=_style_failure(),
            prev_chapter_baseline_fingerprint=None,
        )

        assert "style_drift" in result
        assert "### 定向修复方式(非字数失败不得扩写)" in result
        assert "没有 `below_min` 就不启动扩写策略" in result
        assert "### 扩展方式(达字数下限" not in result
        assert "核心目标:扩内容达字数下限" not in result
        assert "### 收敛方式" not in result

    def test_reviewer_natural_rewrite_defaults_to_targeted_fix(self) -> None:
        """无 word_count 证据时按 Reviewer 定向改,不凭空假定正文欠长。"""
        v1_fp = _make_fingerprint(paragraph_mean=50.0)
        result = _format_rewrite_baseline(
            prev_version_fingerprint=v1_fp,
            validator_failures=None,
            prev_chapter_baseline_fingerprint=None,
        )
        assert "### 定向修复方式(非字数失败不得扩写)" in result
        assert "### 扩展方式(达字数下限" not in result
        assert "收敛方式" not in result
