"""cross_chapter_dedup_validator agent 单测(v1.1 wave 6 session 4,B4)。

覆盖两层:

**纯代码初筛**(``prescreen_duplication``,无 LLM):
1. 句级近重复(玉佩逐句重写)命中 passage 候选
2. 高频套话(破空)命中 phrase 候选
3. 无重复 → 0 候选
4. 重叠 n-gram 归并(同一套话不产一串重复候选)
5. 过短句跳过(噪声防御)
6. 只在单章内重复(跨 <2 章)不算跨章复读

**LLM 判意图**(``validate_cross_chapter_dedup``,mock LLMClient):
7. skip 无前序章 / 8. skip 空正文 / 9. skip 0 候选(0 LLM call)
10. happy 全 intentional → passed=True / 空 violations
11. padding verdict → passed=False + 人类可读 violation
12. fail-open LLM 抛 / 13. fail-open 非法 JSON / 14. setup fail-open
15. index 缺失默认 intentional(不误产 violation,护 §0)
16. user message 含候选列表 + marker
"""

from __future__ import annotations

import json
from pathlib import Path
from unittest.mock import MagicMock

import pytest
import structlog

from src.core.agents import _prompt as prompt_module
from src.core.agents.cross_chapter_dedup_validator import (
    DedupResult,
    PriorChapter,
    prescreen_duplication,
    validate_cross_chapter_dedup,
)
from src.core.llm.base import LLMClient, LLMResponse, TokenUsage


def _make_response(
    *,
    results: list[dict] | None = None,
    model_used: str = "qwen-novel:latest",
) -> LLMResponse:
    payload = {"results": results or []}
    return LLMResponse(
        content=json.dumps(payload, ensure_ascii=False),
        model_used=model_used,
        usage=TokenUsage(input_tokens=900, output_tokens=80),
        finish_reason="stop",
    )


@pytest.fixture
def fake_prompts_dir(tmp_path: Path) -> Path:
    prompts_dir = tmp_path / "prompts"
    prompts_dir.mkdir()
    (prompts_dir / "cross_chapter_dedup_validator_v1.md").write_text(
        "FAKE_CROSS_CHAPTER_DEDUP_VALIDATOR_SYSTEM_PROMPT\n\n单测断言标记。",
        encoding="utf-8",
    )
    return prompts_dir


@pytest.fixture(autouse=True)
def _clear_prompt_cache() -> None:
    prompt_module.load_prompt_template.cache_clear()


@pytest.fixture(autouse=True)
def _ensure_debug_level() -> None:
    structlog.configure(wrapper_class=structlog.make_filtering_bound_logger(0))


@pytest.fixture
def mock_client() -> MagicMock:
    client = MagicMock(spec=LLMClient)
    client.complete.return_value = _make_response()
    return client


# === 测试数据:玉佩逐句重写 + 破空套话 ===

_PRIOR_YUPEI = PriorChapter(
    chapter_id="ch-0025",
    text="玉佩由温热转为冰凉,一阵丧钟般的低鸣在识海深处响起。陆星野皱起眉头。",
)
_CURRENT_YUPEI = (
    "玉佩由温热转为冰凉,一阵丧钟般的低鸣在他识海深处响起。他猛地睁开了眼睛。"
)


# ====================== 纯代码初筛 ======================


def test_prescreen_passage_near_duplicate_hit() -> None:
    """句级近重复(玉佩逐句重写)命中 passage 候选。"""
    cands = prescreen_duplication(
        current_text=_CURRENT_YUPEI, prior_chapters=[_PRIOR_YUPEI]
    )
    passages = [c for c in cands if c.kind == "passage"]
    assert len(passages) >= 1
    top = passages[0]
    assert top.prior_chapter_id == "ch-0025"
    assert top.signal >= 0.5
    assert "玉佩" in top.current_excerpt
    assert "玉佩" in top.prior_excerpt


def test_prescreen_phrase_repetition_hit() -> None:
    """高频套话(破空)跨多章重复命中 phrase 候选。"""
    current = "一道破空之声传来,他立刻警觉。"
    prior = [
        PriorChapter(chapter_id=f"ch-{i:04d}", text="一道破空之声传来,敌袭。")
        for i in range(20, 25)
    ]
    cands = prescreen_duplication(current_text=current, prior_chapters=prior)
    phrases = [c for c in cands if c.kind == "phrase"]
    assert len(phrases) >= 1
    # 代表 n-gram 是"一道破空之声传来"的某个 5-gram 窗口(确定性聚类代表);
    # 不断言具体哪个窗口,只断言它确实是该复读套话的片段。
    assert any(c.current_excerpt in "一道破空之声传来" for c in phrases)
    assert phrases[0].signal >= 4


def test_prescreen_no_duplication_empty() -> None:
    """无重复 → 0 候选(不误报)。"""
    current = "春风拂过山岗,他独自走在回家的小路上,心里盘算明天的计划。"
    prior = [
        PriorChapter(
            chapter_id="ch-0001",
            text="昨夜大雨,整座城市笼罩阴霾,警报声此起彼伏不绝于耳。",
        )
    ]
    cands = prescreen_duplication(current_text=current, prior_chapters=prior)
    assert cands == []


def test_prescreen_overlapping_ngrams_merged() -> None:
    """同一套话的相邻 / 偏移 n-gram 归并成一条代表候选(不产一串重复)。"""
    current = "一道破空之声传来。"
    prior = [
        PriorChapter(chapter_id=f"ch-{i:04d}", text="一道破空之声传来。")
        for i in range(20, 26)
    ]
    cands = prescreen_duplication(current_text=current, prior_chapters=prior)
    phrases = [c for c in cands if c.kind == "phrase"]
    # "一道破空之声传来" 的多个 5-gram 共享 3+ 连续字 → 应归并成 1 条
    assert len(phrases) == 1


def test_prescreen_short_sentence_skipped() -> None:
    """过短句(< 最短句长)不参与 passage 比对(噪声防御)。"""
    current = "好的。是吗。走吧。"  # 全是短句
    prior = [PriorChapter(chapter_id="ch-0001", text="好的。是吗。走吧。")]
    cands = prescreen_duplication(current_text=current, prior_chapters=prior)
    passages = [c for c in cands if c.kind == "passage"]
    assert passages == []


def test_prescreen_phrase_single_chapter_not_flagged() -> None:
    """同一短语只在单章(<2 章)内重复 → 不算跨章复读(phrase 要求跨 ≥2 章)。"""
    # 短语只在本章重复多次,前章不含 → 不命中 phrase。
    current = "他笑了笑笑笑。" + "幽蓝色的火焰升腾。" * 5
    prior = [PriorChapter(chapter_id="ch-0001", text="完全不相干的另一段内容文字。")]
    cands = prescreen_duplication(current_text=current, prior_chapters=prior)
    phrases = [c for c in cands if c.kind == "phrase"]
    assert phrases == []


# ====================== LLM 判意图 ======================


def test_skip_no_prior_chapters(
    mock_client: MagicMock, fake_prompts_dir: Path
) -> None:
    """无前序章 → skipped + 0 LLM call。"""
    result = validate_cross_chapter_dedup(
        current_text=_CURRENT_YUPEI,
        prior_chapters=[],
        chapter_id="ch-0026",
        client=mock_client,
        model="mock",
        prompts_dir=fake_prompts_dir,
    )
    assert result.passed is True
    assert result.skipped is True
    assert result.llm_calls_count == 0
    mock_client.complete.assert_not_called()


def test_skip_empty_current_text(
    mock_client: MagicMock, fake_prompts_dir: Path
) -> None:
    """空正文 → skipped。"""
    result = validate_cross_chapter_dedup(
        current_text="   ",
        prior_chapters=[_PRIOR_YUPEI],
        chapter_id="ch-0026",
        client=mock_client,
        model="mock",
        prompts_dir=fake_prompts_dir,
    )
    assert result.skipped is True
    assert result.llm_calls_count == 0


def test_skip_zero_candidates_no_llm_call(
    mock_client: MagicMock, fake_prompts_dir: Path
) -> None:
    """初筛 0 候选(正文与前章无重复)→ skip + 0 LLM call(省 token)。"""
    result = validate_cross_chapter_dedup(
        current_text="春风拂过山岗,他独自走在回家的小路上,盘算明天的计划。",
        prior_chapters=[
            PriorChapter(
                chapter_id="ch-0001",
                text="昨夜大雨,城市笼罩阴霾,警报声此起彼伏不绝于耳响彻。",
            )
        ],
        chapter_id="ch-0026",
        client=mock_client,
        model="mock",
        prompts_dir=fake_prompts_dir,
    )
    assert result.passed is True
    assert result.skipped is True
    assert result.llm_calls_count == 0
    mock_client.complete.assert_not_called()


def test_happy_all_intentional_passed(
    mock_client: MagicMock, fake_prompts_dir: Path
) -> None:
    """初筛有候选 + LLM 全判 intentional → passed=True / 空 violations / 1 LLM call。"""
    mock_client.complete.return_value = _make_response(
        results=[{"index": 1, "verdict": "intentional", "reason": "母题呼应"}]
    )
    result = validate_cross_chapter_dedup(
        current_text=_CURRENT_YUPEI,
        prior_chapters=[_PRIOR_YUPEI],
        chapter_id="ch-0026",
        client=mock_client,
        model="mock",
        prompts_dir=fake_prompts_dir,
    )
    assert result.passed is True
    assert result.violations == []
    assert result.candidates_count >= 1
    assert result.llm_calls_count == 1


def test_padding_verdict_produces_violation(
    mock_client: MagicMock, fake_prompts_dir: Path
) -> None:
    """LLM 判 padding → passed=False + 人类可读 violation。"""
    mock_client.complete.return_value = _make_response(
        results=[
            {"index": 1, "verdict": "padding", "reason": "玉佩转凉逐句重写纯充字数"}
        ]
    )
    result = validate_cross_chapter_dedup(
        current_text=_CURRENT_YUPEI,
        prior_chapters=[_PRIOR_YUPEI],
        chapter_id="ch-0026",
        client=mock_client,
        model="mock",
        prompts_dir=fake_prompts_dir,
    )
    assert result.passed is False
    assert len(result.violations) == 1
    assert "正文跨章去重" in result.violations[0]
    assert "充字数" in result.violations[0]
    # passage 类 padding → padding_phrases **不含**(整句近重复不是可规避套话)。
    assert result.padding_phrases == []


def test_padding_phrase_populates_bare_padding_phrases(
    mock_client: MagicMock, fake_prompts_dir: Path
) -> None:
    """phrase 类 padding → ``padding_phrases`` 是**裸套话短语**(供 M1 跨章累计的稳定
    key),非渲染串;``violations`` 仍是完整渲染句。w5-s1 原型 A/B 真 LLM 跑暴露
    "M1 误把渲染串当 key → 永不累计"后的修复防回退。"""
    current = "一道破空之声传来,他立刻警觉。"
    prior = [
        PriorChapter(chapter_id=f"ch-{i:04d}", text="一道破空之声传来,敌袭。")
        for i in range(20, 25)
    ]
    # 前若干候选全判 padding(漏的默认 intentional)。
    mock_client.complete.return_value = _make_response(
        results=[
            {"index": i, "verdict": "padding", "reason": "破空套话机械复读"}
            for i in range(1, 7)
        ]
    )
    result = validate_cross_chapter_dedup(
        current_text=current,
        prior_chapters=prior,
        chapter_id="ch-0026",
        client=mock_client,
        model="mock",
        prompts_dir=fake_prompts_dir,
    )
    # 裸短语:非空、短(≈5-gram)、不含渲染前缀、是复读套话的片段。
    assert len(result.padding_phrases) >= 1
    assert all("正文跨章去重" not in p for p in result.padding_phrases)
    assert all(len(p) <= 12 for p in result.padding_phrases)
    assert any(p in "一道破空之声传来" for p in result.padding_phrases)
    # 渲染 violations 仍是完整句(含前缀),与裸短语分离两条通道。
    assert any("正文跨章去重" in v for v in result.violations)
    # 裸短语来源一致:被嵌在某条渲染 violation 里。
    assert any(result.padding_phrases[0] in v for v in result.violations)


def test_fail_open_llm_raises(
    mock_client: MagicMock, fake_prompts_dir: Path
) -> None:
    """LLM 抛异常 → fail-open(passed=True + fail_open=True)。"""
    mock_client.complete.side_effect = RuntimeError("boom")
    result = validate_cross_chapter_dedup(
        current_text=_CURRENT_YUPEI,
        prior_chapters=[_PRIOR_YUPEI],
        chapter_id="ch-0026",
        client=mock_client,
        model="mock",
        prompts_dir=fake_prompts_dir,
    )
    assert result.passed is True
    assert result.fail_open is True
    assert result.violations == []


def test_fail_open_invalid_json(
    mock_client: MagicMock, fake_prompts_dir: Path
) -> None:
    """LLM 返非法 JSON → fail-open。"""
    mock_client.complete.return_value = LLMResponse(
        content="这不是 JSON",
        model_used="qwen-novel:latest",
        usage=TokenUsage(input_tokens=10, output_tokens=5),
        finish_reason="stop",
    )
    result = validate_cross_chapter_dedup(
        current_text=_CURRENT_YUPEI,
        prior_chapters=[_PRIOR_YUPEI],
        chapter_id="ch-0026",
        client=mock_client,
        model="mock",
        prompts_dir=fake_prompts_dir,
    )
    assert result.passed is True
    assert result.fail_open is True


def test_live_qwen_unescaped_reason_quotes_are_repaired_without_fail_open(
    mock_client: MagicMock, fake_prompts_dir: Path
) -> None:
    """R9 v6 ch-0003 实际坏样本只修 JSON 引号，仍走完整 dedup schema。"""
    mock_client.complete.return_value = LLMResponse(
        content=(
            '{"results":[{"index":1,"verdict":"intentional",'
            '"reason":"对话归属标记"林澈说"属基础叙事语法结构，非内容重复"}]}'
        ),
        model_used="qwen3.8-max",
        usage=TokenUsage(input_tokens=10, output_tokens=5),
        finish_reason="stop",
    )

    result = validate_cross_chapter_dedup(
        current_text=_CURRENT_YUPEI,
        prior_chapters=[_PRIOR_YUPEI],
        chapter_id="ch-0003",
        client=mock_client,
        model="qwen3.8-max",
        prompts_dir=fake_prompts_dir,
    )

    assert result.passed is True
    assert result.fail_open is False
    assert result.llm_calls_count == 1
    assert result.candidates_count >= 1


def test_setup_fail_open_prompt_missing(
    mock_client: MagicMock, tmp_path: Path
) -> None:
    """prompt 模板缺失 → setup fail-open + 0 LLM call。"""
    empty_dir = tmp_path / "empty_prompts"
    empty_dir.mkdir()
    result = validate_cross_chapter_dedup(
        current_text=_CURRENT_YUPEI,
        prior_chapters=[_PRIOR_YUPEI],
        chapter_id="ch-0026",
        client=mock_client,
        model="mock",
        prompts_dir=empty_dir,
    )
    assert result.passed is True
    assert result.fail_open is True
    assert result.llm_calls_count == 0
    mock_client.complete.assert_not_called()


def test_missing_index_defaults_intentional(
    mock_client: MagicMock, fake_prompts_dir: Path
) -> None:
    """LLM 漏返某候选 index → 默认 intentional(不误产 violation,护 §0)。"""
    # 构造确定有 ≥2 候选的输入:玉佩逐句重写 + 破空套话。
    current = "玉佩由温热转为冰凉,一阵丧钟般的低鸣响起。一道破空之声传来。"
    prior = [
        PriorChapter(
            chapter_id=f"ch-{i:04d}",
            text="玉佩由温热转为冰凉,一阵丧钟般的低鸣响起。一道破空之声传来。",
        )
        for i in range(20, 25)
    ]
    # LLM 只返第 1 条 padding,漏返其余 → 漏的默认 intentional 不产 violation。
    mock_client.complete.return_value = _make_response(
        results=[{"index": 1, "verdict": "padding", "reason": "逐句重写"}]
    )
    result = validate_cross_chapter_dedup(
        current_text=current,
        prior_chapters=prior,
        chapter_id="ch-0026",
        client=mock_client,
        model="mock",
        prompts_dir=fake_prompts_dir,
    )
    assert result.candidates_count >= 2
    # 只有第 1 条被判 padding,漏返的其余候选默认 intentional → violation 数 = 1
    assert len(result.violations) == 1


def test_user_message_contains_candidates_and_marker(
    mock_client: MagicMock, fake_prompts_dir: Path
) -> None:
    """user message 含候选列表段 + agent_role marker。"""
    validate_cross_chapter_dedup(
        current_text=_CURRENT_YUPEI,
        prior_chapters=[_PRIOR_YUPEI],
        chapter_id="ch-0026",
        client=mock_client,
        model="mock",
        prompts_dir=fake_prompts_dir,
    )
    call = mock_client.complete.call_args
    user_msg = call.kwargs["messages"][1].content
    assert "## 候选重复片段列表" in user_msg
    assert "使用中文书名号式括号「」" in user_msg
    assert "标准 json.loads" in user_msg
    assert "agent_role: cross_chapter_dedup_validator" in user_msg


def test_result_dataclass_defaults() -> None:
    """DedupResult 默认值契约(skip 路径返回形态)。"""
    r = DedupResult(passed=True, skipped=True)
    assert r.violations == []
    assert r.padding_phrases == []
    assert r.candidates_count == 0
    assert r.llm_calls_count == 0
    assert r.fail_open is False
