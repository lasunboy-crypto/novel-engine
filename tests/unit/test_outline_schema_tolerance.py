"""OutlineBuilder schema 容错单测(v1.0.x wave 5 session 2 引入)。

对应 luo3 工作区实测 qwen-novel 5 种历史错误模式 + s2 c+d+e+b 四件套
中 e(schema 容错)的覆盖:

| # | 错误模式 | 容错方式 | 本测试覆盖 |
|---|---|---|---|
| 1 | tension_curve 长度数错(29 vs 30) | model_validator(mode='before') pad/trim | ✅ |
| 2 | tension_curve 元素越界(11/12/13) | model_validator(mode='before') clamp [1,10] | ✅ |
| 3 | invalid_json | 走 retry-with-feedback(在 test_retry_feedback) | (跨模块) |
| 4 | description 字段写成 summary | Field(alias="summary") + populate_by_name | ✅ |
| 5 | phase_id/turning_id 等用中文 | 保留 raise + 走 retry-with-feedback | ✅(验 raise) |
"""

from __future__ import annotations

import pytest
from pydantic import ValidationError

from src.core.models.outline import (
    PlotLongThread,
    PlotTurningPoint,
    VolumePlanItem,
)


# ---------------------------------------------------------------------------
# 错误模式 #4:description 字段名错位(LLM 写 summary)
# ---------------------------------------------------------------------------


class TestDescriptionSummaryAlias:
    """单向兼容:input 接受 description 或 summary,output 始终 description。"""

    def test_PlotTurningPoint_用_description_字段名正常通过(self) -> None:
        result = PlotTurningPoint.model_validate(
            {
                "turning_id": "turn-1",
                "title": "师傅之死",
                "description": "师傅在与反派对决中陨落",
            }
        )
        assert result.description == "师傅在与反派对决中陨落"

    def test_PlotTurningPoint_用_summary_字段名也能解析(self) -> None:
        """LLM 写 summary 字段名时容错通过 → 收为 description。"""
        result = PlotTurningPoint.model_validate(
            {
                "turning_id": "turn-1",
                "title": "师傅之死",
                "summary": "师傅在与反派对决中陨落",
            }
        )
        assert result.description == "师傅在与反派对决中陨落"

    def test_PlotTurningPoint_输出统一为_description_单向(self) -> None:
        """无论 input 用 summary 还是 description,output 始终是 description。"""
        result = PlotTurningPoint.model_validate(
            {
                "turning_id": "turn-1",
                "title": "师傅之死",
                "summary": "师傅在与反派对决中陨落",
            }
        )
        dumped = result.model_dump()
        assert "description" in dumped
        assert "summary" not in dumped
        assert dumped["description"] == "师傅在与反派对决中陨落"

    def test_PlotLongThread_summary_alias_同型(self) -> None:
        result = PlotLongThread.model_validate(
            {
                "thread_id": "yu-pei",
                "title": "玉佩之约",
                "summary": "玉佩在序章埋下,在卷三对决时收回",
            }
        )
        assert result.description == "玉佩在序章埋下,在卷三对决时收回"
        dumped = result.model_dump()
        assert dumped["description"] == "玉佩在序章埋下,在卷三对决时收回"
        assert "summary" not in dumped


# ---------------------------------------------------------------------------
# 错误模式 #1:tension_curve 长度数错
# ---------------------------------------------------------------------------


def _make_arcs_for_chapter_count(n: int) -> list[dict]:
    """构造覆盖 [1, n] 的最小 arc 集合(2 个 arc)。"""
    mid = max(2, n // 2)
    return [
        {
            "arc_id": "arc-1",
            "arc_type": "主线",
            "chapter_range": [1, mid],
            "phase": "opening",
            "summary": "开端",
        },
        {
            "arc_id": "arc-2",
            "arc_type": "主线",
            "chapter_range": [mid + 1, n],
            "phase": "climax",
            "summary": "高潮",
        },
    ]


class TestTensionCurveLengthTolerance:
    """schema 容错:tension_curve 长度 != chapter_count 时 pad/trim。"""

    def test_长度过短_自动_pad_用_5_补齐(self) -> None:
        """luo3 错误模式 #1 同型:长度 29 vs chapter_count 30 → 补 1 个 5。"""
        data = {
            "volume_id": "v1",
            "title": "卷一",
            "summary": "本卷概要",
            "chapter_count": 5,
            "tension_template": "heros_journey",
            "arcs": _make_arcs_for_chapter_count(5),
            "tension_curve": [3, 4, 5, 6],  # 长度 4,缺 1
        }
        result = VolumePlanItem.model_validate(data)
        assert result.tension_curve == [3, 4, 5, 6, 5]
        assert len(result.tension_curve) == 5

    def test_长度过长_自动_trim_截断(self) -> None:
        """长度 7 vs chapter_count 5 → 截掉末尾 2 个。"""
        data = {
            "volume_id": "v1",
            "title": "卷一",
            "summary": "本卷概要",
            "chapter_count": 5,
            "tension_template": "heros_journey",
            "arcs": _make_arcs_for_chapter_count(5),
            "tension_curve": [3, 4, 5, 6, 7, 8, 9],  # 长度 7,多 2
        }
        result = VolumePlanItem.model_validate(data)
        assert result.tension_curve == [3, 4, 5, 6, 7]
        assert len(result.tension_curve) == 5

    def test_长度恰好等于_chapter_count_不动(self) -> None:
        """合规输入完全不动。"""
        data = {
            "volume_id": "v1",
            "title": "卷一",
            "summary": "本卷概要",
            "chapter_count": 5,
            "tension_template": "heros_journey",
            "arcs": _make_arcs_for_chapter_count(5),
            "tension_curve": [3, 4, 5, 6, 7],
        }
        result = VolumePlanItem.model_validate(data)
        assert result.tension_curve == [3, 4, 5, 6, 7]


# ---------------------------------------------------------------------------
# 错误模式 #2:tension_curve 元素越界
# ---------------------------------------------------------------------------


class TestTensionCurveElementClamp:
    """schema 容错:tension_curve 元素超出 [1, 10] 时自动 clamp。"""

    def test_元素越界_clamp_到上界(self) -> None:
        """luo3 错误模式 #2 同型:11/12/13 → 钳制到 10。"""
        data = {
            "volume_id": "v1",
            "title": "卷一",
            "summary": "本卷概要",
            "chapter_count": 5,
            "tension_template": "heros_journey",
            "arcs": _make_arcs_for_chapter_count(5),
            "tension_curve": [3, 11, 12, 13, 7],
        }
        result = VolumePlanItem.model_validate(data)
        assert result.tension_curve == [3, 10, 10, 10, 7]

    def test_元素越界_clamp_到下界(self) -> None:
        """0 / 负数 → 钳制到 1。"""
        data = {
            "volume_id": "v1",
            "title": "卷一",
            "summary": "本卷概要",
            "chapter_count": 5,
            "tension_template": "heros_journey",
            "arcs": _make_arcs_for_chapter_count(5),
            "tension_curve": [0, -5, 5, 6, 7],
        }
        result = VolumePlanItem.model_validate(data)
        assert result.tension_curve == [1, 1, 5, 6, 7]

    def test_越界与长度同时错_两者都修(self) -> None:
        """组合错:长度 4 vs chapter_count 5,且元素含 11、0。"""
        data = {
            "volume_id": "v1",
            "title": "卷一",
            "summary": "本卷概要",
            "chapter_count": 5,
            "tension_template": "heros_journey",
            "arcs": _make_arcs_for_chapter_count(5),
            "tension_curve": [0, 11, 5, 6],  # 长度 4,缺 1;含越界
        }
        result = VolumePlanItem.model_validate(data)
        # clamp + pad
        assert result.tension_curve == [1, 10, 5, 6, 5]


# ---------------------------------------------------------------------------
# 错误模式 #5:phase_id / turning_id / thread_id 用中文(保留 raise 让 retry 接管)
# ---------------------------------------------------------------------------


class TestIdPatternRaisesForRetry:
    """schema 容错决策:ID pattern 保留 raise,由 retry-with-feedback 让 LLM 自改。

    决策理由(plan §s2 待审项 4 拍板 A):中文 ID slugify 没有好 fallback
    (强行变 phase-N 丢语义),靠 retry-with-feedback 中文反馈让 LLM 自改
    是最聪明的方式;3 次都改不对再 fail。
    """

    def test_PlotTurningPoint_中文_turning_id_拒绝(self) -> None:
        with pytest.raises(ValidationError, match="string_pattern_mismatch"):
            PlotTurningPoint.model_validate(
                {
                    "turning_id": "师傅之死",  # 中文,违反 ID_PATTERN
                    "title": "师傅之死",
                    "description": "师傅在与反派对决中陨落",
                }
            )

    def test_PlotLongThread_中文_thread_id_拒绝(self) -> None:
        with pytest.raises(ValidationError, match="string_pattern_mismatch"):
            PlotLongThread.model_validate(
                {
                    "thread_id": "玉佩之约",  # 中文
                    "title": "玉佩之约",
                    "description": "...",
                }
            )

    def test_含标点_ID_拒绝(self) -> None:
        """标点(空格 / 中文标点)同型拒绝。"""
        with pytest.raises(ValidationError, match="string_pattern_mismatch"):
            PlotTurningPoint.model_validate(
                {
                    "turning_id": "turn 1",  # 空格
                    "title": "x",
                    "description": "x",
                }
            )


# ---------------------------------------------------------------------------
# 既有合规约束不破回归(防御:不能因加 before validator 把好输入也搞坏)
# ---------------------------------------------------------------------------


class TestRegressionGuard:
    def test_既有_happy_path_仍然通过(self) -> None:
        """volume_plans happy path 完全合规输入,加 before validator 后仍正常解析。"""
        data = {
            "volume_id": "v1",
            "title": "卷一·觉醒",
            "summary": "本卷在主线中扮演的位置",
            "chapter_count": 5,
            "tension_template": "heros_journey",
            "arcs": _make_arcs_for_chapter_count(5),
            "tension_curve": [3, 4, 5, 6, 7],
        }
        result = VolumePlanItem.model_validate(data)
        assert result.chapter_count == 5
        assert result.tension_curve == [3, 4, 5, 6, 7]

    def test_既有_description_字段名_单_description_输入仍通过(self) -> None:
        """加 alias 后,只给 description 字段名也照常解析(单向兼容应保留原路径)。"""
        result = PlotTurningPoint.model_validate(
            {
                "turning_id": "turn-1",
                "title": "x",
                "description": "x",
            }
        )
        assert result.description == "x"
