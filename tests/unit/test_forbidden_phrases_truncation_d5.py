"""v1.1 wave 4 session 10(D5)forbidden_must_achieve 禁词清单截断单测。

被测:``_truncate_forbidden_must_achieve_phrases`` —— retry 注入 prompt 前对
禁词清单去重 + 截断,防长卷 / batch_size > 10 时清单膨胀塞爆 prompt。

不变式:
- 去重保序;
- ``<= max_phrases`` 时只去重不截断;
- 超界时保留**尾部**(调用方把最该避开的条目放尾部);
- 截断时落 ``chapter_card_forbidden_phrases_truncated`` warn,未截断时不落。
"""

from __future__ import annotations

from structlog.testing import capture_logs

from src.services.chapter_card_service import (
    _MAX_FORBIDDEN_MUST_ACHIEVE_PHRASES,
    _truncate_forbidden_must_achieve_phrases,
)


def _call(phrases: list[str], **kw: object) -> list[str]:
    return _truncate_forbidden_must_achieve_phrases(
        phrases,
        source=kw.pop("source", "volume_collect"),  # type: ignore[arg-type]
        workspace_id=kw.pop("workspace_id", "ws-test"),  # type: ignore[arg-type]
        **kw,  # type: ignore[arg-type]
    )


def test_empty_returns_empty() -> None:
    assert _call([]) == []


def test_under_cap_no_dup_unchanged() -> None:
    phrases = ["甲", "乙", "丙"]
    assert _call(phrases) == ["甲", "乙", "丙"]


def test_dedup_preserves_order_no_truncation() -> None:
    """重复条目去重,保留首次出现顺序,不触发截断。"""
    phrases = ["甲", "乙", "甲", "丙", "乙"]
    assert _call(phrases) == ["甲", "乙", "丙"]


def test_over_cap_keeps_tail() -> None:
    """超 max_phrases 时保留尾部(最近 / 最该避开的条目)。"""
    phrases = [f"item-{i}" for i in range(40)]
    out = _call(phrases, max_phrases=30)
    assert len(out) == 30
    # 保留尾部 30 条(item-10 .. item-39),顺序不变
    assert out == [f"item-{i}" for i in range(10, 40)]


def test_dedup_then_truncate_tail_priority() -> None:
    """先去重再截断:放尾部的"优先保留"条目(模拟 batch retry 的本章失败
    must_achieve)在去重 + 截断后仍存活。"""
    # 32 条 other-card 字面 + 2 条尾部"失败 must_achieve"
    others = [f"other-{i}" for i in range(32)]
    failed_tail = ["本章失败-A", "本章失败-B"]
    out = _call(others + failed_tail, max_phrases=30)
    assert len(out) == 30
    # 尾部失败条目必须存活
    assert "本章失败-A" in out
    assert "本章失败-B" in out
    # 最早的 others 被截掉
    assert "other-0" not in out


def test_truncation_emits_warn_log() -> None:
    phrases = [f"item-{i}" for i in range(40)]
    with capture_logs() as logs:
        _call(phrases, max_phrases=30, source="batch_retry", chapter_id="ch-0007")
    events = [e for e in logs if e["event"] == "chapter_card_forbidden_phrases_truncated"]
    assert len(events) == 1
    ev = events[0]
    assert ev["source"] == "batch_retry"
    assert ev["chapter_id"] == "ch-0007"
    assert ev["original_count"] == 40
    assert ev["deduped_count"] == 40
    assert ev["kept_count"] == 30
    assert ev["max_phrases"] == 30


def test_no_truncation_no_warn_log() -> None:
    """未超界(仅去重)不落截断日志。"""
    with capture_logs() as logs:
        _call(["甲", "乙", "甲"], max_phrases=30)
    events = [e for e in logs if e["event"] == "chapter_card_forbidden_phrases_truncated"]
    assert events == []


def test_default_cap_value() -> None:
    """默认上界 = 模块常量(30),与文档一致。"""
    assert _MAX_FORBIDDEN_MUST_ACHIEVE_PHRASES == 30
    phrases = [f"item-{i}" for i in range(_MAX_FORBIDDEN_MUST_ACHIEVE_PHRASES + 5)]
    assert len(_call(phrases)) == _MAX_FORBIDDEN_MUST_ACHIEVE_PHRASES
