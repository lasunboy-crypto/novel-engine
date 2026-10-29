"""Session D 文学表层信号：只定位，不自动裁决或修文。"""

from src.core.rules.literary_signals import (
    detect_contrast_reframes,
    render_contrast_reframe_review_section,
)


def test_detects_contrast_reframe_variants() -> None:
    text = (
        "他不是害怕，而是谨慎。"
        "这并非退让，只是留出余地。"
        "屋里没有争吵，只有钟摆在响。"
        "与其说她原谅了他，不如说她暂时放下刀。"
        "局面看似平静，实则每个人都在等门响。"
        "问题不在证词，而在那十分钟的空白。"
    )
    report = detect_contrast_reframes(text)
    assert {match.kind for match in report.matches} == {
        "not_but",
        "not_really_but",
        "none_only",
        "rather_than",
        "seems_actually",
        "problem_lies",
    }
    assert report.triggered is True


def test_single_real_correction_does_not_render_soft_section() -> None:
    text = "“我不是说你撒谎，而是说这份记录晚了两分钟。”她把表推过去。"
    report = detect_contrast_reframes(text)
    assert len(report.matches) == 1
    assert report.triggered is False
    assert render_contrast_reframe_review_section(text) == ""


def test_two_candidates_in_one_window_render_exact_anchors() -> None:
    first = "他不是害怕，而是谨慎"
    second = "她看似平静，实则一直挡在门前"
    text = f"{first}。" + "风声" * 20 + f"。{second}。"
    section = render_contrast_reframe_review_section(text)
    assert "## 公式化对照句候选（SOFT；不得单独决定 verdict）" in section
    assert "不是害怕，而是谨慎" in section
    assert "看似平静，实则一直挡在门前" in section
    assert "真实对白纠错或必要反转可以保留" in section


def test_three_far_apart_candidates_trigger_chapter_total() -> None:
    spacer = "甲" * 1100
    text = (
        "他不是退缩，而是换路。"
        + spacer
        + "她没有回答，只有指尖敲了一下桌面。"
        + spacer
        + "问题不在钥匙，而在谁最后碰过门。"
    )
    report = detect_contrast_reframes(text)
    assert len(report.matches) == 3
    assert report.max_window_count == 1
    assert report.triggered is True
