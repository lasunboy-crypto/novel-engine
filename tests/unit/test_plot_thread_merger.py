"""plot_thread_merger agent 单测(v1.1 wave 2 session 5 A3 治本,
2026-05-29 引入)。

9 类覆盖(对齐 must_vs_forbidden_validator 同型测试范围 + A3 独有 luo-clean-v1
ch-1~10 织网者主线现场重演):

1. **happy**:no_merge + 空 decisions list
2. **merge**:LLM 返 merge(luo-clean-v1 织网者主线现场重演)
3. **multiple merge**:多条 extracted 合并到不同 existing
4. **empty extracted**:extracted 空 → 跳过 + skipped=True
5. **empty existing**:existing 空 → 跳过 + skipped=True
6. **LLM fail**:LLM 抛异常 → fail-open
7. **invalid JSON**:LLM 返非法 JSON → fail-open
8. **invalid IDs filtered**:LLM 返不存在的 thread_id → caller 过滤丢弃
9. **top-K candidate selection**:select_top_k_existing_candidates 单测

完全 mock ``LLMClient``;用临时目录注入 fake ``plot_thread_merger_v1.md`` 模板。
"""

from __future__ import annotations

import json
from datetime import UTC, datetime
from pathlib import Path
from unittest.mock import MagicMock

import pytest
import structlog

from src.core.agents import _prompt as prompt_module
from src.core.agents.plot_thread_merger import (
    PlotThreadMergeResult,
    merge_plot_threads_with_existing,
    select_top_k_existing_candidates,
)
from src.core.llm.base import LLMClient, LLMResponse, TokenUsage
from src.core.models.plot_thread import PlotThread


# ---------------------------------------------------------------------------
# 工厂 & 公共 fixture
# ---------------------------------------------------------------------------


def _make_response(
    *,
    verdict: str = "no_merge",
    decisions: list[dict] | None = None,
    evidence: str = "本章 extracted 与 existing 无语义重复",
    model_used: str = "qwen3.7-max",
) -> LLMResponse:
    payload = {
        "verdict": verdict,
        "decisions": decisions or [],
        "evidence": evidence,
    }
    return LLMResponse(
        content=json.dumps(payload, ensure_ascii=False),
        model_used=model_used,
        usage=TokenUsage(input_tokens=800, output_tokens=120),
        finish_reason="stop",
    )


def _make_thread(
    *,
    thread_id: str,
    title: str,
    description: str = "",
    workspace_id: str = "ws-demo",
) -> PlotThread:
    return PlotThread(
        thread_id=thread_id,
        workspace_id=workspace_id,
        title=title,
        status="developing",
        importance=4,
        planted_chapter=None,
        planned_payoff=None,
        actual_payoff=None,
        description=description or f"plot_thread {title} 描述占位",
        related_characters=[],
        created_at=datetime.now(UTC),
        last_updated_chapter=None,
    )


@pytest.fixture
def fake_prompts_dir(tmp_path: Path) -> Path:
    prompts_dir = tmp_path / "prompts"
    prompts_dir.mkdir()
    (prompts_dir / "plot_thread_merger_v1.md").write_text(
        "FAKE_PLOT_THREAD_MERGER_PROMPT\n\n用于单测断言的 system 标记。",
        encoding="utf-8",
    )
    return prompts_dir


@pytest.fixture(autouse=True)
def _clear_prompt_cache() -> None:
    prompt_module.load_prompt_template.cache_clear()


@pytest.fixture(autouse=True)
def _ensure_debug_level_for_capture_logs() -> None:
    structlog.configure(wrapper_class=structlog.make_filtering_bound_logger(0))


@pytest.fixture
def mock_client() -> MagicMock:
    client = MagicMock(spec=LLMClient)
    client.complete.return_value = _make_response()
    return client


def _call(
    mock_client: MagicMock,
    fake_prompts_dir: Path,
    *,
    extracted: list[dict] | None = None,
    existing: list[PlotThread] | None = None,
    chapter_id: str = "ch-0005",
    workspace_id: str = "ws-demo",
    model: str = "qwen3.7-max",
) -> PlotThreadMergeResult:
    default_extracted = extracted if extracted is not None else [
        {
            "thread_id": "th-new-1",
            "title": "新登记伏笔",
            "description": "本章新出现的描述",
        },
    ]
    default_existing = existing if existing is not None else [
        _make_thread(thread_id="th-existing-1", title="既有伏笔"),
    ]
    return merge_plot_threads_with_existing(
        extracted=default_extracted,
        existing=default_existing,
        chapter_id=chapter_id,
        workspace_id=workspace_id,
        client=mock_client,
        model=model,
        prompts_dir=fake_prompts_dir,
    )


# ---------------------------------------------------------------------------
# 1. Happy:no_merge
# ---------------------------------------------------------------------------


class TestHappy:
    def test_no_merge_returns_empty_decisions(
        self, mock_client: MagicMock, fake_prompts_dir: Path
    ) -> None:
        result = _call(mock_client, fake_prompts_dir)
        assert result.merge_decisions == []
        assert result.skipped is False
        assert result.fail_open is False
        assert result.llm_calls_count == 1
        assert mock_client.complete.call_count == 1


# ---------------------------------------------------------------------------
# 2. Merge:LLM 返合并决策(luo-clean-v1 织网者主线现场重演)
# ---------------------------------------------------------------------------


class TestMergeDecision:
    def test_luo_clean_v1_zhi_wang_zhe_scene_reproduced(
        self, mock_client: MagicMock, fake_prompts_dir: Path
    ) -> None:
        """luo-clean-v1 ch-1~10 现场:'织网者之谜' 与 '织网者白莲传承' 合并。"""
        mock_client.complete.return_value = _make_response(
            verdict="merge",
            decisions=[
                {
                    "extracted_thread_id": "zhi-wang-zhe-mi-mi",
                    "target_existing_thread_id": "zhi-wang-zhe-bai-lian-chuan-cheng",
                    "confidence": 0.85,
                    "reason": "同一组织'织网者'的不同侧重(谜团 vs 信仰传承)",
                },
            ],
            evidence="本章 1 条与 existing 高度同主线 — 织网者组织",
        )
        result = _call(
            mock_client,
            fake_prompts_dir,
            extracted=[
                {
                    "thread_id": "zhi-wang-zhe-mi-mi",
                    "title": "织网者之谜",
                    "description": "织网者组织背后的谜团",
                },
            ],
            existing=[
                _make_thread(
                    thread_id="zhi-wang-zhe-bai-lian-chuan-cheng",
                    title="织网者白莲传承",
                    description="织网者作为白莲教传承组织的伏笔",
                ),
            ],
        )
        assert len(result.merge_decisions) == 1
        d = result.merge_decisions[0]
        assert d.extracted_thread_id == "zhi-wang-zhe-mi-mi"
        assert d.target_existing_thread_id == "zhi-wang-zhe-bai-lian-chuan-cheng"
        assert d.confidence == 0.85

    def test_multiple_merge_decisions(
        self, mock_client: MagicMock, fake_prompts_dir: Path
    ) -> None:
        mock_client.complete.return_value = _make_response(
            verdict="merge",
            decisions=[
                {
                    "extracted_thread_id": "th-new-1",
                    "target_existing_thread_id": "th-existing-1",
                    "confidence": 0.8,
                    "reason": "同主线",
                },
                {
                    "extracted_thread_id": "th-new-2",
                    "target_existing_thread_id": "th-existing-2",
                    "confidence": 0.9,
                    "reason": "同主体",
                },
            ],
            evidence="2 条合并",
        )
        result = _call(
            mock_client,
            fake_prompts_dir,
            extracted=[
                {"thread_id": "th-new-1", "title": "T1", "description": "d1"},
                {"thread_id": "th-new-2", "title": "T2", "description": "d2"},
            ],
            existing=[
                _make_thread(thread_id="th-existing-1", title="E1"),
                _make_thread(thread_id="th-existing-2", title="E2"),
            ],
        )
        assert len(result.merge_decisions) == 2


# ---------------------------------------------------------------------------
# 3. Empty extracted/existing:skip
# ---------------------------------------------------------------------------


class TestEmptyExtracted:
    def test_empty_extracted_skips(
        self, mock_client: MagicMock, fake_prompts_dir: Path
    ) -> None:
        result = _call(
            mock_client, fake_prompts_dir, extracted=[]
        )
        assert result.skipped is True
        assert result.llm_calls_count == 0
        assert result.merge_decisions == []
        assert mock_client.complete.call_count == 0


class TestEmptyExisting:
    def test_empty_existing_skips(
        self, mock_client: MagicMock, fake_prompts_dir: Path
    ) -> None:
        result = _call(
            mock_client, fake_prompts_dir, existing=[]
        )
        assert result.skipped is True
        assert result.llm_calls_count == 0
        assert mock_client.complete.call_count == 0


# ---------------------------------------------------------------------------
# 4. LLM fail-open
# ---------------------------------------------------------------------------


class TestLLMFailOpen:
    def test_llm_raises_exception(
        self, mock_client: MagicMock, fake_prompts_dir: Path
    ) -> None:
        mock_client.complete.side_effect = RuntimeError("LLM 故障")
        result = _call(mock_client, fake_prompts_dir)
        assert result.fail_open is True
        assert result.merge_decisions == []
        assert result.llm_calls_count == 1


class TestInvalidJSON:
    def test_non_json_output_fails_open(
        self, mock_client: MagicMock, fake_prompts_dir: Path
    ) -> None:
        mock_client.complete.return_value = LLMResponse(
            content="非 JSON 输出",
            model_used="qwen3.7-max",
            usage=TokenUsage(input_tokens=800, output_tokens=20),
            finish_reason="stop",
        )
        result = _call(mock_client, fake_prompts_dir)
        assert result.fail_open is True

    def test_schema_mismatch_fails_open(
        self, mock_client: MagicMock, fake_prompts_dir: Path
    ) -> None:
        mock_client.complete.return_value = LLMResponse(
            content='{"verdict": "merge", "wrong_field": []}',
            model_used="qwen3.7-max",
            usage=TokenUsage(input_tokens=800, output_tokens=20),
            finish_reason="stop",
        )
        result = _call(mock_client, fake_prompts_dir)
        assert result.fail_open is True


# ---------------------------------------------------------------------------
# 5. Invalid IDs filtered(LLM 返不存在的 thread_id → caller 过滤丢弃)
# ---------------------------------------------------------------------------


class TestInvalidIDs:
    def test_unknown_extracted_id_filtered(
        self, mock_client: MagicMock, fake_prompts_dir: Path
    ) -> None:
        """LLM 返的 extracted_thread_id 不在 extracted 集合 → 过滤。"""
        mock_client.complete.return_value = _make_response(
            verdict="merge",
            decisions=[
                {
                    "extracted_thread_id": "th-not-in-extracted",  # 不存在
                    "target_existing_thread_id": "th-existing-1",
                    "confidence": 0.9,
                    "reason": "虚构",
                },
            ],
            evidence="LLM 编造了不存在的 extracted ID",
        )
        result = _call(mock_client, fake_prompts_dir)
        assert result.merge_decisions == []
        assert result.fail_open is False  # 不是 fail-open;只是 invalid IDs filtered

    def test_unknown_existing_id_filtered(
        self, mock_client: MagicMock, fake_prompts_dir: Path
    ) -> None:
        mock_client.complete.return_value = _make_response(
            verdict="merge",
            decisions=[
                {
                    "extracted_thread_id": "th-new-1",
                    "target_existing_thread_id": "th-not-in-existing",  # 不存在
                    "confidence": 0.9,
                    "reason": "虚构",
                },
            ],
            evidence="LLM 编造了不存在的 existing ID",
        )
        result = _call(mock_client, fake_prompts_dir)
        assert result.merge_decisions == []
        assert result.fail_open is False


# ---------------------------------------------------------------------------
# 6. Top-K candidate selection
# ---------------------------------------------------------------------------


class TestTopKCandidateSelection:
    def test_high_similarity_thread_selected(self) -> None:
        """title 高度相似的 existing 被选入候选。"""
        extracted = [
            {"thread_id": "th-new", "title": "织网者之谜", "description": "d"},
        ]
        existing = [
            _make_thread(thread_id="th-1", title="织网者白莲传承"),
            _make_thread(thread_id="th-2", title="完全无关的伏笔"),
        ]
        result = select_top_k_existing_candidates(
            extracted=extracted, existing=existing
        )
        assert len(result) == 1
        assert result[0].thread_id == "th-1"

    def test_low_similarity_all_filtered(self) -> None:
        """title 全不相似(< 0.3) → 全过滤。"""
        extracted = [
            {"thread_id": "th-new", "title": "织网者", "description": "d"},
        ]
        existing = [
            _make_thread(thread_id="th-1", title="完全无关 ABC"),
            _make_thread(thread_id="th-2", title="另一个不相干 XYZ"),
        ]
        result = select_top_k_existing_candidates(
            extracted=extracted, existing=existing
        )
        assert result == []

    def test_top_k_limit_respected(self) -> None:
        """超过 top_k 上限的 existing 按相似度降序截断。"""
        extracted = [
            {"thread_id": "th-new", "title": "织网者", "description": "d"},
        ]
        existing = [
            _make_thread(thread_id=f"th-{i}", title="织网者变体" + str(i))
            for i in range(15)
        ]
        result = select_top_k_existing_candidates(
            extracted=extracted, existing=existing, top_k=5
        )
        assert len(result) == 5

    def test_empty_inputs_return_empty(self) -> None:
        assert select_top_k_existing_candidates(extracted=[], existing=[]) == []
        assert (
            select_top_k_existing_candidates(
                extracted=[{"thread_id": "t", "title": "T", "description": "d"}],
                existing=[],
            )
            == []
        )
        assert (
            select_top_k_existing_candidates(
                extracted=[],
                existing=[_make_thread(thread_id="t", title="T")],
            )
            == []
        )
