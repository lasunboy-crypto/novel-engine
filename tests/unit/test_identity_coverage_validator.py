"""identity_coverage_validator agent 单测(v1.1 wave 6 session 2 / A3,批处理)。

覆盖:

1. **empty candidates**:候选为空 → 0 次 LLM 调用 + 空 items
2. **batch 覆盖/缺侧**:全覆盖(uncovered=[])+ 缺一侧(uncovered=["现实"])→ gaps 只含缺侧角色
3. **missing index fail-safe**:LLM 漏返某候选 → 该候选默认全覆盖(不误产 gap)
4. **invalid context 过滤**:LLM 返回候选未声明的 context → 被收敛掉(防编造标签)
5. **LLM fail**:LLM 抛异常 → 全部全覆盖 + fail_open=True
6. **setup fail**:prompt 文件缺失 → 全部全覆盖 + fail_open=True

完全 mock ``LLMClient``;用临时目录注入 fake ``identity_coverage_validator_v1.md``。
"""

from __future__ import annotations

import json
from pathlib import Path
from unittest.mock import MagicMock

import pytest

from src.core.agents import _prompt as prompt_module
from src.core.agents.identity_coverage_validator import (
    IdentityCoverageCandidate,
    validate_identity_coverage_batch,
)
from src.core.llm.base import LLMClient, LLMResponse, TokenUsage
from src.core.models.character import IdentityFacet


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


def _cand(name: str, *facets: tuple[str, str]) -> IdentityCoverageCandidate:
    return IdentityCoverageCandidate(
        character_id=name,
        character_name=name,
        facets=tuple(IdentityFacet(context=c, identity=i) for c, i in facets),
    )


_EVENTS = [
    "ch-1:沈舟在诊所催眠患者",
    "ch-2:沈舟入梦以锦衣卫潜入",
    "ch-5:白露梦境引路人现身",
]


@pytest.fixture
def fake_prompts_dir(tmp_path: Path) -> Path:
    prompts_dir = tmp_path / "prompts"
    prompts_dir.mkdir()
    (prompts_dir / "identity_coverage_validator_v1.md").write_text(
        "FAKE_IDENTITY_COVERAGE_VALIDATOR_PROMPT\n\n用于单测的 system 标记。",
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
    return validate_identity_coverage_batch(
        candidates=candidates,
        all_events=_EVENTS,
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
        assert result.gaps == []
        mock_client.complete.assert_not_called()


class TestBatchCoverage:
    def test_covered_and_gap(
        self, mock_client: MagicMock, fake_prompts_dir: Path
    ) -> None:
        candidates = [
            _cand("沈舟", ("现实", "心理医生"), ("梦境", "卧底锦衣卫")),
            _cand("白露", ("现实", "诊所助理"), ("梦境", "引路人")),
        ]
        mock_client.complete.return_value = _make_response(
            [
                {"index": 1, "uncovered_contexts": [], "evidence": "两侧均覆盖"},
                {
                    "index": 2,
                    "uncovered_contexts": ["现实"],
                    "evidence": "白露现实侧全书无交代",
                },
            ]
        )
        result = _call(mock_client, fake_prompts_dir, candidates)
        assert result.llm_calls_count == 1
        assert mock_client.complete.call_count == 1
        # gaps 只含缺侧角色
        assert result.gaps == [("白露", ["现实"])]
        by_name = {it.character_name: it.uncovered_contexts for it in result.items}
        assert by_name["沈舟"] == []
        assert by_name["白露"] == ["现实"]


class TestMissingIndexFailSafe:
    def test_missing_result_defaults_covered(
        self, mock_client: MagicMock, fake_prompts_dir: Path
    ) -> None:
        """LLM 漏返候选 2 → 候选 2 默认全覆盖(不误产 gap,护 §0)。"""
        candidates = [
            _cand("甲", ("A", "x"), ("B", "y")),
            _cand("乙", ("A", "x"), ("B", "y")),
        ]
        mock_client.complete.return_value = _make_response(
            [{"index": 1, "uncovered_contexts": ["A"], "evidence": "甲缺 A 侧"}]
        )
        result = _call(mock_client, fake_prompts_dir, candidates)
        assert result.gaps == [("甲", ["A"])]
        by_name = {it.character_name: it.uncovered_contexts for it in result.items}
        assert by_name["乙"] == []  # 漏返默认全覆盖


class TestInvalidContextFiltered:
    def test_llm_invented_context_filtered(
        self, mock_client: MagicMock, fake_prompts_dir: Path
    ) -> None:
        """LLM 返回候选未声明的 context(编造) → 被收敛掉,不误产 gap。"""
        candidates = [_cand("甲", ("现实", "医生"), ("梦境", "卫士"))]
        mock_client.complete.return_value = _make_response(
            [
                {
                    "index": 1,
                    "uncovered_contexts": ["平行宇宙"],  # 候选没有此情境
                    "evidence": "编造的情境",
                }
            ]
        )
        result = _call(mock_client, fake_prompts_dir, candidates)
        # 编造 context 被过滤 → 无 gap
        assert result.gaps == []
        assert result.items[0].uncovered_contexts == []


class TestFailOpen:
    def test_llm_exception_all_covered(
        self, mock_client: MagicMock, fake_prompts_dir: Path
    ) -> None:
        mock_client.complete.side_effect = RuntimeError("LLM 瞬态故障")
        result = _call(
            mock_client,
            fake_prompts_dir,
            [_cand("甲", ("A", "x"), ("B", "y"))],
        )
        assert result.fail_open is True
        assert result.gaps == []
        assert result.items[0].uncovered_contexts == []
        assert result.llm_calls_count == 1

    def test_setup_missing_prompt_all_covered(
        self, mock_client: MagicMock, tmp_path: Path
    ) -> None:
        empty_dir = tmp_path / "empty_prompts"
        empty_dir.mkdir()
        result = validate_identity_coverage_batch(
            candidates=[_cand("甲", ("A", "x"), ("B", "y"))],
            all_events=_EVENTS,
            client=mock_client,
            model="qwen-max",
            prompts_dir=empty_dir,
        )
        assert result.fail_open is True
        assert result.gaps == []
        assert result.llm_calls_count == 0
