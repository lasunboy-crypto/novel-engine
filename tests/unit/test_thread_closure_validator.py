"""thread_closure_validator agent 单测(v1.1 wave 6 session 1 / A4,批处理)。

覆盖:

1. **empty candidates**:候选为空 → 0 次 LLM 调用 + 空 items
2. **batch 三档**:closed / open_with_setup / dangling → dangling_titles 只含 dangling
3. **C1 留白保护**:open_with_setup **不**计入 dangling
4. **missing index fail-safe**:LLM 漏返某候选 → 该候选默认 closed(不误产 dangling)
5. **LLM fail**:LLM 抛异常 → 全部 closed + fail_open=True
6. **setup fail**:prompt 文件缺失 → 全部 closed + fail_open=True

完全 mock ``LLMClient``;用临时目录注入 fake ``thread_closure_validator_v1.md``。
"""

from __future__ import annotations

import json
from pathlib import Path
from unittest.mock import MagicMock

import pytest

from src.core.agents import _prompt as prompt_module
from src.core.agents.thread_closure_validator import (
    ThreadClosureCandidate,
    validate_thread_closure_batch,
)
from src.core.llm.base import LLMClient, LLMResponse, TokenUsage


def _make_response(
    results: list[dict],
    *,
    model_used: str = "qwen-max",
) -> LLMResponse:
    return LLMResponse(
        content=json.dumps({"results": results}, ensure_ascii=False),
        model_used=model_used,
        usage=TokenUsage(input_tokens=700, output_tokens=120),
        finish_reason="stop",
    )


def _cand(title: str, *, last_hooked: str | None = "ch-0024") -> ThreadClosureCandidate:
    return ThreadClosureCandidate(
        thread_title=title,
        thread_description=f"{title}的埋 / 收意图描述",
        last_hooked_chapter=last_hooked,
    )


_ENDING = ["ch-29:主角与反派决战", "ch-30:主线收束,各线收尾"]


@pytest.fixture
def fake_prompts_dir(tmp_path: Path) -> Path:
    prompts_dir = tmp_path / "prompts"
    prompts_dir.mkdir()
    (prompts_dir / "thread_closure_validator_v1.md").write_text(
        "FAKE_THREAD_CLOSURE_VALIDATOR_PROMPT\n\n用于单测的 system 标记。",
        encoding="utf-8",
    )
    return prompts_dir


@pytest.fixture(autouse=True)
def _clear_prompt_cache() -> None:
    prompt_module.load_prompt_template.cache_clear()


@pytest.fixture
def mock_client() -> MagicMock:
    return MagicMock(spec=LLMClient)


def _call(mock_client, fake_prompts_dir, candidates):
    return validate_thread_closure_batch(
        candidates=candidates,
        ending_events=_ENDING,
        workspace_id="ws-demo",
        client=mock_client,
        model="qwen-max",
        prompts_dir=fake_prompts_dir,
    )


class TestEmptyCandidates:
    def test_empty_skips_llm(
        self, mock_client: MagicMock, fake_prompts_dir: Path
    ) -> None:
        result = _call(mock_client, fake_prompts_dir, [])
        assert result.items == []
        assert result.llm_calls_count == 0
        assert result.dangling_titles == []
        mock_client.complete.assert_not_called()


class TestBatchVerdicts:
    def test_three_verdicts(
        self, mock_client: MagicMock, fake_prompts_dir: Path
    ) -> None:
        candidates = [_cand("身世之谜"), _cand("妹妹能否救回"), _cand("界外真局")]
        mock_client.complete.return_value = _make_response(
            [
                {"index": 1, "verdict": "closed", "evidence": "ch-30 归宗了结"},
                {
                    "index": 2,
                    "verdict": "open_with_setup",
                    "evidence": "主线已收,妹妹线留希望",
                },
                {
                    "index": 3,
                    "verdict": "dangling",
                    "evidence": "界外真局一句臆想草率否认",
                },
            ]
        )
        result = _call(mock_client, fake_prompts_dir, candidates)
        assert result.llm_calls_count == 1
        assert mock_client.complete.call_count == 1
        # C1 留白保护:open_with_setup 不计入 dangling
        assert result.dangling_titles == ["界外真局"]
        verdicts = {it.thread_title: it.verdict for it in result.items}
        assert verdicts["身世之谜"] == "closed"
        assert verdicts["妹妹能否救回"] == "open_with_setup"
        assert verdicts["界外真局"] == "dangling"


class TestMissingIndexFailSafe:
    def test_missing_result_defaults_closed(
        self, mock_client: MagicMock, fake_prompts_dir: Path
    ) -> None:
        """LLM 漏返候选 2 → 候选 2 默认 closed(不误产 dangling,护 §0)。"""
        candidates = [_cand("线 A"), _cand("线 B")]
        mock_client.complete.return_value = _make_response(
            [{"index": 1, "verdict": "dangling", "evidence": "线 A 埋了不收"}]
        )
        result = _call(mock_client, fake_prompts_dir, candidates)
        assert result.dangling_titles == ["线 A"]
        verdicts = {it.thread_title: it.verdict for it in result.items}
        assert verdicts["线 B"] == "closed"  # 漏返默认 closed


class TestFailOpen:
    def test_llm_exception_all_closed(
        self, mock_client: MagicMock, fake_prompts_dir: Path
    ) -> None:
        mock_client.complete.side_effect = RuntimeError("LLM 瞬态故障")
        result = _call(mock_client, fake_prompts_dir, [_cand("线 A"), _cand("线 B")])
        assert result.fail_open is True
        assert result.dangling_titles == []
        assert all(it.verdict == "closed" for it in result.items)
        assert len(result.items) == 2

    def test_setup_missing_prompt_all_closed(
        self, mock_client: MagicMock, tmp_path: Path
    ) -> None:
        empty_dir = tmp_path / "empty_prompts"
        empty_dir.mkdir()
        result = validate_thread_closure_batch(
            candidates=[_cand("线 A")],
            ending_events=_ENDING,
            client=mock_client,
            model="qwen-max",
            prompts_dir=empty_dir,
        )
        assert result.fail_open is True
        assert result.dangling_titles == []
        assert result.llm_calls_count == 0
