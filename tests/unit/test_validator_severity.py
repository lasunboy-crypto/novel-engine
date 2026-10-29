"""v1.1 G8:`classify_validator_severity` 纯函数单测。

覆盖目标:
- light 路径:**严格** 单条 word_count 失败；below_min 偏差 < 10%，above_max
  无论比例都交既有 word_count_trim 有界收缩
- heavy 路径:多 validator 失败 / 非 word_count 失败 / below_min 偏差 ≥ 10% /
  details 字段缺失保守判 heavy
- 边界值:below_min 的 10% 阈值严格小于(>= 10% = heavy,避免误判)

luo-cloud-test 21 章实证场景重演(see candidates §G8):
- 22 次 word_count below_min 失败 — 假设 G6 hard floor 治后差距收到 0-5% 内,
  这部分应派 revise(reviser 局部 patch)
- 50 次 timeline_monotonic 失败 — 派 rewrite(无法 patch)
"""

from __future__ import annotations

import pytest

from src.core.rules.validator_pipeline import (
    WORD_COUNT_LIGHT_DEVIATION_THRESHOLD,
    ValidatorPipelineReport,
    classify_validator_severity,
)
from src.core.rules.validators import ValidatorResult


def _make_word_count_failure(
    *, actual: int, word_count_min: int, word_count_max: int
) -> ValidatorResult:
    reason = "below_min" if actual < word_count_min else "above_max"
    return ValidatorResult(
        name="word_count",
        passed=False,
        details={
            "actual_word_count": actual,
            "word_count_min": word_count_min,
            "word_count_max": word_count_max,
            "reason": reason,
        },
    )


def _make_report(*results: ValidatorResult) -> ValidatorPipelineReport:
    failed = [r for r in results if not r.passed]
    return ValidatorPipelineReport(
        passed=not failed,
        results=list(results),
        failed_constraint_names=[r.name for r in failed],
    )


class TestLightPath:
    """light 路径:单条 word_count 失败 + 偏差 < 10%(v1.1 audit G21,2026-05-27 阈值 5%→10%)。"""

    def test_word_count_below_min_lt_10pct_is_light(self) -> None:
        # min=2500, actual=2450 → 缺 50 字 / 2500 = 2% < 10% → light
        report = _make_report(
            _make_word_count_failure(actual=2450, word_count_min=2500, word_count_max=4000)
        )
        assert classify_validator_severity(report) == "light"

    def test_word_count_below_min_at_8pct_is_light(self) -> None:
        # min=2500, actual=2300 → 缺 200 字 / 2500 = 8% < 10% → light(G21 治本核心覆盖区)
        # luo-clean-v1 ch-5 v1 现场 deficit=9% 走本路径,reviser 局部补字数,
        # 避开 G11 锚 v1 collapsed fingerprint 风险(对照 5% 旧阈值 9% 永走 heavy)。
        report = _make_report(
            _make_word_count_failure(actual=2300, word_count_min=2500, word_count_max=4000)
        )
        assert classify_validator_severity(report) == "light"

    def test_word_count_above_max_lt_10pct_is_light(self) -> None:
        # max=4000, actual=4300 → 多 300 字 / 4000 = 7.5% < 10% → light
        report = _make_report(
            _make_word_count_failure(actual=4300, word_count_min=2500, word_count_max=4000)
        )
        assert classify_validator_severity(report) == "light"


class TestHeavyPath:
    """heavy 路径:多 validator 失败 / 非 word_count 失败 / 偏差 ≥ 10%。"""

    def test_word_count_below_min_eq_10pct_is_heavy(self) -> None:
        # min=2500, actual=2250 → 缺 250 字 / 2500 = 10% → 严格小于,= 10% 判 heavy
        report = _make_report(
            _make_word_count_failure(actual=2250, word_count_min=2500, word_count_max=4000)
        )
        assert classify_validator_severity(report) == "heavy"

    def test_word_count_below_min_gt_10pct_is_heavy(self) -> None:
        # min=2500, actual=2200 → 缺 300 字 / 2500 = 12% > 10% → heavy
        report = _make_report(
            _make_word_count_failure(actual=2200, word_count_min=2500, word_count_max=4000)
        )
        assert classify_validator_severity(report) == "heavy"

    def test_word_count_above_max_ge_10pct_still_uses_bounded_trim(self) -> None:
        # 专用 trim 会逐刀代码量字数并在不收敛时自动转 rewrite；单独超上限
        # 不再因比例大而直接盲 rewrite。
        report = _make_report(
            _make_word_count_failure(actual=4500, word_count_min=2500, word_count_max=4000)
        )
        assert classify_validator_severity(report) == "light"

    def test_extreme_above_max_still_uses_bounded_trim(self) -> None:
        report = _make_report(
            _make_word_count_failure(actual=7154, word_count_min=2500, word_count_max=4500)
        )
        assert classify_validator_severity(report) == "light"

    def test_forbidden_failure_is_heavy(self) -> None:
        # 非 word_count 失败 = 任何严重度均判 heavy(POV / 设定错乱必须重写)
        report = _make_report(
            ValidatorResult(
                name="forbidden",
                passed=False,
                details={"forbidden_hits": ["手机"]},
            )
        )
        assert classify_validator_severity(report) == "heavy"

    def test_timeline_monotonic_failure_is_heavy(self) -> None:
        # luo-cloud-test 现场重演:50 次 timeline 失败 → 必判 heavy → rewrite
        report = _make_report(
            ValidatorResult(
                name="timeline_monotonic",
                passed=False,
                details={"violation": {"prev_time": "T+5", "curr_time": "T+3"}},
            )
        )
        assert classify_validator_severity(report) == "heavy"

    def test_must_unresolved_failure_is_heavy(self) -> None:
        report = _make_report(
            ValidatorResult(
                name="must_unresolved",
                passed=False,
                details={"missing": ["阵眼真凶身份"]},
            )
        )
        assert classify_validator_severity(report) == "heavy"

    def test_multiple_validators_failed_is_heavy(self) -> None:
        # 即使 word_count 偏差 < 5%,只要叠加任何另一条 validator 失败 → heavy
        # (复杂失败 patch 难补,保守 rewrite)
        report = _make_report(
            _make_word_count_failure(actual=2450, word_count_min=2500, word_count_max=4000),
            ValidatorResult(
                name="forbidden",
                passed=False,
                details={"forbidden_hits": ["手机"]},
            ),
        )
        assert classify_validator_severity(report) == "heavy"


class TestEdgeCases:
    """details 字段缺失 / 异常输入 → 保守判 heavy(数据层 bug 时不应误派 revise)。"""

    def test_word_count_missing_actual_is_heavy(self) -> None:
        report = ValidatorPipelineReport(
            passed=False,
            results=[
                ValidatorResult(
                    name="word_count",
                    passed=False,
                    details={
                        # actual_word_count 缺失 — 模拟 details schema 漂移
                        "word_count_min": 2500,
                        "word_count_max": 4000,
                        "reason": "below_min",
                    },
                )
            ],
            failed_constraint_names=["word_count"],
        )
        assert classify_validator_severity(report) == "heavy"

    def test_word_count_missing_reason_is_heavy(self) -> None:
        report = ValidatorPipelineReport(
            passed=False,
            results=[
                ValidatorResult(
                    name="word_count",
                    passed=False,
                    details={
                        "actual_word_count": 2450,
                        "word_count_min": 2500,
                        "word_count_max": 4000,
                        # reason 缺失
                    },
                )
            ],
            failed_constraint_names=["word_count"],
        )
        assert classify_validator_severity(report) == "heavy"

    def test_word_count_min_zero_is_heavy(self) -> None:
        # word_count_min=0 防御除零(章节卡 word_count_min=0 = 无下限) → 不该走
        # below_min 分支,但若 reason=below_min + min=0 时函数应保守判 heavy
        report = ValidatorPipelineReport(
            passed=False,
            results=[
                ValidatorResult(
                    name="word_count",
                    passed=False,
                    details={
                        "actual_word_count": -1,  # 非法但 details 字面如此
                        "word_count_min": 0,
                        "word_count_max": 4000,
                        "reason": "below_min",
                    },
                )
            ],
            failed_constraint_names=["word_count"],
        )
        assert classify_validator_severity(report) == "heavy"


class TestThresholdConstant:
    """阈值常量字面契约(v1.1 audit G21,2026-05-27:5% → 10%,便于跨任务对账)。"""

    def test_threshold_value(self) -> None:
        # v1.1 audit G21 治本(2026-05-27 b1-rerun-deep-audit):阈值放宽 5%→10%
        # 让 luo-clean-v1 ch-5 类 8-15% deficit case 走 reviser 局部补字数,
        # 避开 G11 锚 v1 collapsed fingerprint 触发 mode collapse 循环。
        assert WORD_COUNT_LIGHT_DEVIATION_THRESHOLD == 0.10


class TestCallerContract:
    """`merge_into_review_verdict_overrides` 仅在 report.passed=False 时调用契约。

    (本契约由 caller _apply_validator_pipeline 保;
    classify_validator_severity 无此 assert,passed report 传入也应不抛
    — 模拟防御性调用)
    """

    def test_passed_report_does_not_raise(self) -> None:
        # report.passed=True 时 classify 应不抛(防御性);但实际 caller 不会
        # 走这条路径(_apply_validator_pipeline 在 report.passed 时早 return)
        report = ValidatorPipelineReport(
            passed=True,
            results=[
                ValidatorResult(
                    name="word_count", passed=True, details={"actual_word_count": 3000}
                )
            ],
            failed_constraint_names=[],
        )
        # 无失败 → 0 条 != 1 条 → 判 heavy(防御性保守)
        assert classify_validator_severity(report) == "heavy"


@pytest.mark.parametrize(
    "actual,word_count_min,expected_severity",
    [
        (2500, 2500, "heavy"),  # 等于 min — 实际 validator.passed=True 不会失败,
                                # 但函数防御性输入仍判 heavy(reason 不在 below/above)
        (2400, 2500, "light"),  # 缺 4%
        (2300, 2500, "light"),  # 缺 8%(G21 治本核心 — ch-5 现场覆盖区)
        (2275, 2500, "light"),  # 缺 9%(luo-clean-v1 ch-5 v1 实测 deficit 8.92%)
        (2250, 2500, "heavy"),  # 缺 10%(新阈值边界)
        (2249, 2500, "heavy"),  # 缺 10.04%
        (2100, 2500, "heavy"),  # 缺 16%
    ],
)
def test_below_min_threshold_parametric(
    actual: int, word_count_min: int, expected_severity: str
) -> None:
    """10% 阈值边界参数化扫描(v1.1 audit G21,2026-05-27 阈值 5%→10%)。"""
    if actual >= word_count_min:
        # 不应进入失败路径(reason 字面会是 above_max 或不存在);构造防御性场景
        result = ValidatorResult(
            name="word_count",
            passed=False,
            details={
                "actual_word_count": actual,
                "word_count_min": word_count_min,
                "word_count_max": 4000,
                "reason": "unknown",  # 触发 fallback heavy
            },
        )
    else:
        result = _make_word_count_failure(
            actual=actual, word_count_min=word_count_min, word_count_max=4000
        )
    report = _make_report(result)
    assert classify_validator_severity(report) == expected_severity
