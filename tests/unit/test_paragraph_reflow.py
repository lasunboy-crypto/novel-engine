"""R9 v5 paragraph_length_mean 单因失败的纯代码重排测试。"""

from src.core.rules.paragraph_reflow import reflow_paragraph_mean_only
from src.core.rules.style_fingerprint import compute_style_fingerprint


def _failure(*dimensions: dict[str, object]) -> list[dict[str, object]]:
    return [
        {
            "name": "style_drift",
            "details": {"drifted_dimensions": list(dimensions)},
        }
    ]


def _assert_only_newlines_inserted(source: str, candidate: str) -> None:
    source_index = 0
    candidate_index = 0
    while source_index < len(source):
        if source[source_index] == "\n":
            source_end = source_index
            while source_end < len(source) and source[source_end] == "\n":
                source_end += 1
            candidate_end = candidate_index
            while candidate_end < len(candidate) and candidate[candidate_end] == "\n":
                candidate_end += 1
            assert candidate_end - candidate_index >= source_end - source_index
            source_index = source_end
            candidate_index = candidate_end
            continue
        while candidate_index < len(candidate) and candidate[candidate_index] == "\n":
            candidate_index += 1
        assert candidate_index < len(candidate)
        assert candidate[candidate_index] == source[source_index]
        source_index += 1
        candidate_index += 1
    assert set(candidate[candidate_index:]) <= {"\n"}


def test_reflow_inserts_only_blank_lines_and_converges() -> None:
    sentence = "字" * 39 + "。"
    content = "\n\n".join(sentence * 5 for _ in range(12))
    before = compute_style_fingerprint(content)["paragraph_length_mean"]
    failures = _failure(
        {
            "dimension": "paragraph_length_mean",
            "baseline_value": 100.0,
            "current_value": before,
            "threshold": 0.5,
        }
    )

    result = reflow_paragraph_mean_only(
        content=content,
        validator_failures=failures,
    )

    assert result.applied
    assert result.after_mean <= 150.0
    assert "".join(result.content.split()) == "".join(content.split())
    _assert_only_newlines_inserted(content, result.content)


def test_reflow_preserves_existing_whitespace_and_quotes_exactly() -> None:
    sentence = "  原有缩进与空格必须保留。”"
    content = (sentence * 12) + "\n \n" + (sentence * 12) + "  "
    before = compute_style_fingerprint(content)["paragraph_length_mean"]
    failures = _failure(
        {
            "dimension": "paragraph_length_mean",
            "baseline_value": 90.0,
            "current_value": before,
            "threshold": 0.5,
        }
    )

    result = reflow_paragraph_mean_only(content=content, validator_failures=failures)

    assert result.applied
    _assert_only_newlines_inserted(content, result.content)


def test_reflow_does_not_run_for_mixed_style_failures() -> None:
    content = ("字" * 39 + "。") * 20
    failures = _failure(
        {
            "dimension": "paragraph_length_mean",
            "baseline_value": 100.0,
            "current_value": 800.0,
            "threshold": 0.5,
        },
        {
            "dimension": "sentence_length_mean",
            "baseline_value": 20.0,
            "current_value": 40.0,
            "threshold": 0.3,
        },
    )

    result = reflow_paragraph_mean_only(
        content=content,
        validator_failures=failures,
    )

    assert not result.applied
    assert result.reason == "not_paragraph_mean_only"
    assert result.content == content


def test_reflow_fails_safe_without_sentence_boundaries() -> None:
    content = "字" * 3000
    failures = _failure(
        {
            "dimension": "paragraph_length_mean",
            "baseline_value": 100.0,
            "current_value": 3000.0,
            "threshold": 0.5,
        }
    )

    result = reflow_paragraph_mean_only(
        content=content,
        validator_failures=failures,
    )

    assert not result.applied
    assert result.reason == "no_sentence_boundary"
    assert result.content == content
