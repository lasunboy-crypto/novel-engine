"""G22a + G22b reviewer.py / orchestrator post-process 测试
(v1.1 audit G22 治本,2026-05-27 b1-rerun-deep-audit)。
"""

from __future__ import annotations

from datetime import UTC, datetime

from src.core.agents.reviewer import (
    _format_fingerprint_section,
    _format_reviewer_hard_gate_contract,
)
from src.core.models.review import ReviewIssue, ReviewResult
from src.core.orchestrator.chapter_orchestrator import (
    _MIN_CHARS_FOR_COLLAPSE_CHECK,
    _PARAGRAPH_COLLAPSE_THRESHOLD,
    _apply_paragraph_collapse_override,
)
from src.core.rules.prose_hard_contract import (
    PARAGRAPH_MEAN_HARD_CONSTRAINT_ID,
    PARAGRAPH_MEAN_HARD_MIN,
    paragraph_mean_hard_rule,
)


def _make_review(
    *,
    verdict: str = "pass",
    score_tension_match: float = 8.0,
    score_prose_quality: float = 8.0,
    score_overall: float = 8.0,
    failed_constraints: list[str] | None = None,
    issues: list[ReviewIssue] | None = None,
    hard_constraints_passed: bool = True,
) -> ReviewResult:
    return ReviewResult(
        review_id="rev-test1234",
        chapter_id="ch-0005",
        version_reviewed=1,
        reviewed_at=datetime.now(UTC),
        reviewer_model="qwen3.7-max",
        score_plot_progression=8.0,
        score_tension_match=score_tension_match,
        score_prose_quality=score_prose_quality,
        score_consistency=8.0,
        score_hook_management=8.0,
        score_voice_consistency=8.0,
        score_overall=score_overall,
        hard_constraints_passed=hard_constraints_passed,
        failed_constraints=failed_constraints or [],
        issues=issues or [],
        suggestions=[],
        verdict=verdict,  # type: ignore[arg-type]
        revise_targets=[],
    )


def _make_fingerprint(
    *, paragraph_mean: float, total_chars: float = 3000.0
) -> dict[str, float]:
    return {
        "paragraph_length_mean": paragraph_mean,
        "sentence_length_mean": 20.0,
        "simile_density": 0.1,
        "total_chars": total_chars,
    }


# ===========================================================================
# G22a:reviewer.py _format_fingerprint_section
# ===========================================================================


class TestG22aFormatFingerprintSection:
    """渲染 fingerprint 客观参考段(prompt 给 LLM 客观信号)。"""

    def test_renders_when_both_present(self) -> None:
        """current + prev 都给 → 渲染完整段。"""
        current = _make_fingerprint(paragraph_mean=55.0)
        prev = _make_fingerprint(paragraph_mean=53.0)
        result = _format_fingerprint_section(
            current_fingerprint=current,
            prev_chapter_baseline_fingerprint=prev,
        )
        assert "## 风格指纹客观参考(STYLE FINGERPRINT REFERENCE)" in result
        assert "paragraph_mean=55 字" in result
        assert "paragraph_mean=53 字" in result
        assert "仅记跨章 WARN" in result
        assert "不设强制分数上限" in result
        assert "不单独打回" in result

    def test_shared_hard_contract_renders_exact_threshold_and_actual(self) -> None:
        result = _format_reviewer_hard_gate_contract(
            current_fingerprint=_make_fingerprint(paragraph_mean=29.5)
        )
        assert PARAGRAPH_MEAN_HARD_CONSTRAINT_ID in result
        assert paragraph_mean_hard_rule() in result
        assert "本稿实测 paragraph_length_mean=29.5" in result
        assert "不要输出 verdict" in result
        assert "跨章 paragraph_mean 偏差" in result

    def test_returns_empty_when_current_none(self) -> None:
        """current=None → 空字符串(跳过段渲染)。"""
        prev = _make_fingerprint(paragraph_mean=53.0)
        result = _format_fingerprint_section(
            current_fingerprint=None,
            prev_chapter_baseline_fingerprint=prev,
        )
        assert result == ""

    def test_returns_empty_when_prev_none(self) -> None:
        """prev=None → 空字符串(首章 / draft 路径)。"""
        current = _make_fingerprint(paragraph_mean=55.0)
        result = _format_fingerprint_section(
            current_fingerprint=current,
            prev_chapter_baseline_fingerprint=None,
        )
        assert result == ""

    def test_returns_empty_when_both_none(self) -> None:
        """两个都 None → 空字符串。"""
        result = _format_fingerprint_section(
            current_fingerprint=None,
            prev_chapter_baseline_fingerprint=None,
        )
        assert result == ""


# ===========================================================================
# G22b:orchestrator._apply_paragraph_collapse_override
# ===========================================================================


class TestG22bParagraphCollapseOverride:
    """paragraph_mean < 30 + total_chars >= 500 → 强制覆盖 verdict=rewrite。"""

    def test_collapse_overrides_pass_verdict(self) -> None:
        """reviewer 主观 pass + paragraph_mean=13.5(ch-5 现场)→ 兜底覆盖 rewrite。"""
        review = _make_review(verdict="pass", score_tension_match=8.0, score_prose_quality=8.0)
        fp = _make_fingerprint(paragraph_mean=13.5, total_chars=3000.0)
        result = _apply_paragraph_collapse_override(
            review=review,
            current_fingerprint=fp,
            chapter_id="ch-0005",
            version_reviewed=1,
        )
        # 覆盖后:verdict=rewrite,score 降到 3.0,加 pacing issue
        assert result.verdict == "rewrite"
        assert result.score_tension_match == 3.0
        assert result.score_prose_quality == 3.0
        assert result.hard_constraints_passed is False
        assert "paragraph_mean_floor" in result.failed_constraints
        assert len(result.issues) == 1
        assert result.issues[0].issue_type == "pacing"
        assert "散文诗式 mode collapse" in result.issues[0].description

    def test_collapse_below_threshold_overrides(self) -> None:
        """paragraph_mean=29.5(< 30 阈值)→ 触发覆盖。"""
        review = _make_review(verdict="pass")
        fp = _make_fingerprint(paragraph_mean=29.5, total_chars=3000.0)
        result = _apply_paragraph_collapse_override(
            review=review,
            current_fingerprint=fp,
            chapter_id="ch-0005",
            version_reviewed=1,
        )
        assert result.verdict == "rewrite"

    def test_normal_paragraph_not_overridden(self) -> None:
        """paragraph_mean=55(自然范围)→ 不覆盖。"""
        review = _make_review(verdict="pass", score_tension_match=8.0)
        fp = _make_fingerprint(paragraph_mean=55.0, total_chars=3000.0)
        result = _apply_paragraph_collapse_override(
            review=review,
            current_fingerprint=fp,
            chapter_id="ch-0005",
            version_reviewed=1,
        )
        # 不变
        assert result.verdict == "pass"
        assert result.score_tension_match == 8.0

    def test_short_text_skips_override(self) -> None:
        """total_chars < 500(mock fixture / 单测异常路径)→ 跳过 G22b。"""
        review = _make_review(verdict="pass")
        # paragraph_mean=3(异常短)但 total_chars=10(< 500)→ 跳过
        fp = _make_fingerprint(paragraph_mean=3.0, total_chars=10.0)
        result = _apply_paragraph_collapse_override(
            review=review,
            current_fingerprint=fp,
            chapter_id="ch-0005",
            version_reviewed=1,
        )
        assert result.verdict == "pass"  # 不变

    def test_no_fingerprint_skips(self) -> None:
        """current_fingerprint=None → 返原 review。"""
        review = _make_review(verdict="pass")
        result = _apply_paragraph_collapse_override(
            review=review,
            current_fingerprint=None,
            chapter_id="ch-0005",
            version_reviewed=1,
        )
        assert result.verdict == "pass"

    def test_paragraph_mean_zero_skips(self) -> None:
        """paragraph_mean=0(异常 fingerprint)→ 跳过。"""
        review = _make_review(verdict="pass")
        fp = _make_fingerprint(paragraph_mean=0.0, total_chars=3000.0)
        result = _apply_paragraph_collapse_override(
            review=review,
            current_fingerprint=fp,
            chapter_id="ch-0005",
            version_reviewed=1,
        )
        assert result.verdict == "pass"

    def test_collapse_merges_existing_failed_constraints(self) -> None:
        """已有 failed_constraints 时合并 paragraph_mean_floor,不覆盖原有。"""
        review = _make_review(
            verdict="rewrite",
            failed_constraints=["word_count"],
        )
        fp = _make_fingerprint(paragraph_mean=15.0, total_chars=3000.0)
        result = _apply_paragraph_collapse_override(
            review=review,
            current_fingerprint=fp,
            chapter_id="ch-0005",
            version_reviewed=1,
        )
        assert "word_count" in result.failed_constraints
        assert "paragraph_mean_floor" in result.failed_constraints

    def test_collapse_appends_issue_to_existing(self) -> None:
        """已有 issues 时追加 G22b 兜底 issue,不覆盖原有。"""
        existing_issue = ReviewIssue(
            paragraph_anchor="he said",
            issue_type="voice",
            description="角色 voice 偏离",
            suggestion="调整对白",
        )
        review = _make_review(verdict="pass", issues=[existing_issue])
        fp = _make_fingerprint(paragraph_mean=15.0, total_chars=3000.0)
        result = _apply_paragraph_collapse_override(
            review=review,
            current_fingerprint=fp,
            chapter_id="ch-0005",
            version_reviewed=1,
        )
        assert len(result.issues) == 2
        assert result.issues[0] == existing_issue
        assert result.issues[1].issue_type == "pacing"

    def test_collapse_score_min_preserves_lower(self) -> None:
        """若 reviewer score 已经低于 3.0,保持原值(min 不抬高)。"""
        review = _make_review(
            verdict="pass",
            score_tension_match=2.5,
            score_prose_quality=2.0,
        )
        fp = _make_fingerprint(paragraph_mean=15.0, total_chars=3000.0)
        result = _apply_paragraph_collapse_override(
            review=review,
            current_fingerprint=fp,
            chapter_id="ch-0005",
            version_reviewed=1,
        )
        assert result.score_tension_match == 2.5  # 保留原值(< 3.0)
        assert result.score_prose_quality == 2.0


class TestG22bThresholds:
    """阈值常量字面契约。"""

    def test_paragraph_collapse_threshold(self) -> None:
        """G22b 与跨 prompt 共享同一 paragraph floor。"""
        assert _PARAGRAPH_COLLAPSE_THRESHOLD == PARAGRAPH_MEAN_HARD_MIN

    def test_min_chars_for_collapse_check(self) -> None:
        """total_chars >= 500 阈值固定(对齐 G5 / G10 短文本 guard)。"""
        assert _MIN_CHARS_FOR_COLLAPSE_CHECK == 500.0


# ===========================================================================
# G22b call site invariant(wave 2-5 重构防御 — 2026-05-28 立)
# ===========================================================================
#
# 背景:wave 2-5 后续 sessions 会改 reviewer 评分公式(w4-s5 I2.2 加风格符合度
# 维度)/ orchestrator rewrite 路径等。G22b 双层防御中 post-process
# 一层依赖"_apply_paragraph_collapse_override 在 reviewer 调用后被调用"
# 这一调用站点契约。若 wave 2-5 重构时不小心删掉或挪位置,helper 单测全过但
# 真路径已绕过 G22b 兜底,wave 1 治理形同虚设。
#
# 本组测试用源码字面 grep 验证调用站点存在性,不替代 e2e 集成测,但能在 CI 期
# 第一时间发现"caller 被删/挪"。
class TestG22bCallSiteInvariant:
    """orchestrator 唯一稿路径接到 G22b helper 的调用站点契约。"""

    @staticmethod
    def _orchestrator_source() -> str:
        from pathlib import Path

        src = (
            Path(__file__).resolve().parent.parent.parent
            / "src" / "core" / "orchestrator" / "chapter_orchestrator.py"
        )
        return src.read_text(encoding="utf-8")

    def test_helper_called_on_single_production_path(self) -> None:
        """chapter_orchestrator.py 内唯一稿 review 路径保留 helper 调用。

        post-v6 已删除草稿池；若唯一 caller 被删 / 注释 / 重命名，本测立刻失败。
        """
        source = self._orchestrator_source()
        # 仅算函数调用形式 `_apply_paragraph_collapse_override(`,不算 def / import
        call_count = source.count("_apply_paragraph_collapse_override(")
        # def 行 + 唯一稿调用站点行 → 总数精确为 2。
        assert call_count == 2, (
            f"G22b helper 期望唯一稿调用 1 次,"
            f"实测源码出现 {call_count} 次(含 def 1 次)。"
            f"wave 2-5 重构可能误删 caller,请重新接入。"
        )

    def test_helper_called_after_reviewer(self) -> None:
        """每条 G22b 调用都在 reviewer 信号源之后(post-process 顺序契约)。

        G22b 是 post-process 兜底,设计上必须发生在 review 已存在之后。
        唯一稿路径在 review_chapter 后走 validator 与写后 gates。
        若 wave 2-5 重构把 caller 挪到所有 reviewer 信号之前 → review 未生成
        G22b 拿空 review 报错或无意义。
        """
        source = self._orchestrator_source()
        lines = source.splitlines()
        # reviewer 信号源 = 4 类 helper / 函数调用站点(任一在 G22b 之前即可)
        reviewer_signal_markers = (
            "review_chapter(",
            "_apply_validator_pipeline(",
            "_apply_must_achieve_validator(",
            "_apply_continuity_validator(",
        )
        reviewer_signal_lines = [
            i for i, line in enumerate(lines, 1)
            if any(m in line for m in reviewer_signal_markers)
            and not line.lstrip().startswith("def ")
        ]
        g22b_call_lines = [
            i for i, line in enumerate(lines, 1)
            if "_apply_paragraph_collapse_override(" in line
            and not line.lstrip().startswith("def ")
        ]
        assert reviewer_signal_lines, "源码必须含 reviewer 信号源调用"
        assert g22b_call_lines, "源码必须含 G22b 调用站点"
        for g22b_line in g22b_call_lines:
            prior_signals = [r for r in reviewer_signal_lines if r < g22b_line]
            assert prior_signals, (
                f"G22b 调用在 line {g22b_line} 但其前无 reviewer 信号源。"
                f"post-process 顺序契约破坏 — wave 2-5 重构请把 G22b 放在 reviewer/"
                f"validator pipeline 之后。"
            )
