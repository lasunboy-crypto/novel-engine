"""must_vs_forbidden_validator 三档可执行性合同单测。

7 类覆盖(对齐 beat_alignment_validator 同型测试范围 + G20 独有 ch-5 现场):

1. **happy**:no_conflict + 空 conflicts list → passed=True
2. **三档边界**:自然可共存 / 直接冲突 / 缺少桥梁的歧义
3. **empty must**:must_achieve 空 → 跳过 + skipped=True
4. **empty forbidden**:forbidden 空 → 跳过 + skipped=True
5. **LLM fail**:LLM 抛异常 → fail-open(passed=True + fail_open=True)
6. **invalid JSON**:LLM 返非法 JSON → fail-open(passed=True + fail_open=True)
7. **历史快照**:显式活动 prompts_dir 只有 v2/v1 时仍按冻结 prompt 读取

完全 mock ``LLMClient``；用临时目录注入 v3 prompt 模板。
"""

from __future__ import annotations

import json
from pathlib import Path
from unittest.mock import MagicMock

import pytest
import structlog

from src.core.agents import _prompt as prompt_module
from src.core.agents.must_vs_forbidden_validator import (
    MustVsForbiddenValidationResult,
    validate_must_vs_forbidden,
)
from src.core.llm.base import LLMClient, LLMResponse, TokenUsage


# ---------------------------------------------------------------------------
# 工厂 & 公共 fixture
# ---------------------------------------------------------------------------


def _make_response(
    *,
    verdict: str = "no_conflict",
    conflicts: list[dict] | None = None,
    evidence: str = "must_achieve vs forbidden 无互斥矛盾",
    model_used: str = "qwen3.7-max",
) -> LLMResponse:
    payload = {
        "verdict": verdict,
        "conflicts": conflicts or [],
        "evidence": evidence,
    }
    return LLMResponse(
        content=json.dumps(payload, ensure_ascii=False),
        model_used=model_used,
        usage=TokenUsage(input_tokens=600, output_tokens=80),
        finish_reason="stop",
    )


@pytest.fixture
def fake_prompts_dir(tmp_path: Path) -> Path:
    prompts_dir = tmp_path / "prompts"
    prompts_dir.mkdir()
    (prompts_dir / "must_vs_forbidden_validator_v3.md").write_text(
        "FAKE_MUST_VS_FORBIDDEN_VALIDATOR_PROMPT\n\n用于单测断言的 system 标记。",
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
    must_achieve: list[str] | None = None,
    forbidden: list[str] | None = None,
    chapter_id: str = "ch-0005",
    workspace_id: str = "ws-demo",
    tension_level: int | None = 6,
    setting: str | None = "测试场景",
    model: str = "qwen3.7-max",
) -> MustVsForbiddenValidationResult:
    return validate_must_vs_forbidden(
        must_achieve=must_achieve if must_achieve is not None else ["事件 A", "事件 B"],
        forbidden=forbidden if forbidden is not None else ["现代用语", "跨域穿帮"],
        chapter_id=chapter_id,
        workspace_id=workspace_id,
        tension_level=tension_level,
        setting=setting,
        client=mock_client,
        model=model,
        prompts_dir=fake_prompts_dir,
    )


# ---------------------------------------------------------------------------
# 1. Happy:no_conflict
# ---------------------------------------------------------------------------


class TestHappy:
    def test_no_conflict_passes(
        self, mock_client: MagicMock, fake_prompts_dir: Path
    ) -> None:
        mock_client.complete.return_value = _make_response(
            verdict="no_conflict",
            conflicts=[],
            evidence="must_achieve vs forbidden 主题不重叠,无互斥",
        )
        result = _call(mock_client, fake_prompts_dir)

        assert result.passed is True
        assert result.conflicts == []
        assert result.evidence == "must_achieve vs forbidden 主题不重叠,无互斥"
        assert result.llm_calls_count == 1
        assert result.skipped is False
        assert result.fail_open is False
        assert mock_client.complete.call_count == 1

    def test_explicit_legacy_activity_snapshot_reads_frozen_v1_prompt(
        self, mock_client: MagicMock, tmp_path: Path
    ) -> None:
        """历史活动显式 prompt 快照不因生产默认升 v2 而失去可读性。"""
        prompts_dir = tmp_path / "legacy-prompts"
        prompts_dir.mkdir()
        (prompts_dir / "must_vs_forbidden_validator_v1.md").write_text(
            "FROZEN_V1_MUST_VS_FORBIDDEN_PROMPT",
            encoding="utf-8",
        )
        must = ["确认钥匙来源"]
        forbidden = ["不得确认钥匙来源"]
        mock_client.complete.return_value = _make_response(
            verdict="conflict",
            conflicts=[
                {
                    "must_index": 0,
                    "must_text": must[0],
                    "forbidden_index": 0,
                    "forbidden_text": forbidden[0],
                    "reason": "同一结果直接互斥",
                }
            ],
            evidence="v1 二档历史结果",
        )

        result = _call(
            mock_client,
            prompts_dir,
            must_achieve=must,
            forbidden=forbidden,
        )

        assert result.verdict == "conflict"
        assert result.conflicts[0].classification == "conflict"
        assert result.fail_open is False
        messages = mock_client.complete.call_args.kwargs["messages"]
        assert messages[0].content == "FROZEN_V1_MUST_VS_FORBIDDEN_PROMPT"

    def test_explicit_activity_snapshot_reads_frozen_v2_before_v1(
        self, mock_client: MagicMock, tmp_path: Path
    ) -> None:
        """显式历史快照缺 v3 时优先使用较新的冻结 v2。"""
        prompts_dir = tmp_path / "frozen-v2-prompts"
        prompts_dir.mkdir()
        (prompts_dir / "must_vs_forbidden_validator_v2.md").write_text(
            "FROZEN_V2_MUST_VS_FORBIDDEN_PROMPT",
            encoding="utf-8",
        )
        (prompts_dir / "must_vs_forbidden_validator_v1.md").write_text(
            "FROZEN_V1_MUST_VS_FORBIDDEN_PROMPT",
            encoding="utf-8",
        )

        result = _call(mock_client, prompts_dir)

        assert result.passed is True
        assert result.fail_open is False
        messages = mock_client.complete.call_args.kwargs["messages"]
        assert messages[0].content == "FROZEN_V2_MUST_VS_FORBIDDEN_PROMPT"


# ---------------------------------------------------------------------------
# 2. 三档分类边界
# ---------------------------------------------------------------------------


class TestClassificationBoundaries:
    def test_financial_comparison_without_long_listing_is_executable(
        self, mock_client: MagicMock, fake_prompts_dir: Path
    ) -> None:
        """剧情动作不依赖被禁止的冗长呈现方式时不能误判冲突。"""
        mock_client.complete.return_value = _make_response(
            verdict="no_conflict",
            conflicts=[],
            evidence="可用少量关键异常和角色动作完成比较，无需大段罗列",
        )
        result = _call(
            mock_client,
            fake_prompts_dir,
            must_achieve=[
                "沈舟在梦中以锦衣卫身份潜入文官私宴,窃听暗桩与朝廷命官勾结的密谋",
                "沈舟将梦中暗桩线索与现实资金流向交叉比对",
                "确认梦境密谋细节与现实资金流向存在高度映射",
            ],
            forbidden=[
                "梦境场景出现现代科技产品",
                "现实场景出现明朝服饰",
                "大段枯燥的财务数据罗列导致叙事拖沓",
            ],
        )

        assert result.passed is True
        assert result.verdict == "no_conflict"
        assert result.conflicts == []
        assert result.llm_calls_count == 1
        assert result.skipped is False
        assert result.fail_open is False

    def test_direct_same_outcome_conflict(
        self, mock_client: MagicMock, fake_prompts_dir: Path
    ) -> None:
        must = ["本章确认密室钥匙来自管家"]
        forbidden = ["本章不得确认或暗示钥匙来源"]
        mock_client.complete.return_value = _make_response(
            verdict="conflict",
            conflicts=[
                {
                    "must_index": 0,
                    "must_text": must[0],
                    "forbidden_index": 0,
                    "forbidden_text": forbidden[0],
                    "reason": "同一结论被要求确认又被完全禁止",
                    "classification": "conflict",
                },
            ],
            evidence="确认钥匙来源与禁止确认直接互斥",
        )
        result = _call(
            mock_client,
            fake_prompts_dir,
            must_achieve=must,
            forbidden=forbidden,
        )

        assert result.passed is False
        assert result.verdict == "conflict"
        assert result.conflicts[0].classification == "conflict"

    def test_victim_experience_without_full_perpetrator_view_is_executable(
        self, mock_client: MagicMock, fake_prompts_dir: Path
    ) -> None:
        """事件经历不要求完整展示施害者，普通叙事选择不能触发歧义。"""
        must = ["展示受害者为保护同伴而死前最后十秒的第一人称经历"]
        forbidden = ["不得清晰展示施害实体的完整外观"]
        mock_client.complete.return_value = _make_response(
            verdict="no_conflict",
            conflicts=[],
            evidence="must 要求受害者经历，未要求施害者完整可见",
        )

        result = _call(
            mock_client,
            fake_prompts_dir,
            must_achieve=must,
            forbidden=forbidden,
        )

        assert result.passed is True
        assert result.verdict == "no_conflict"
        assert result.conflicts == []

    def test_inference_result_without_full_chain_is_executable(
        self, mock_client: MagicMock, fake_prompts_dir: Path
    ) -> None:
        """只要求形成结论时，不披露完整证据链属于自然可共存。"""
        must = ["比较现有线索并推断目标所属类别"]
        forbidden = ["不得揭示具体判断方法和完整证据链"]
        mock_client.complete.return_value = _make_response(
            verdict="no_conflict",
            conflicts=[],
            evidence="可呈现推断结论而不展开完整方法与证据链",
        )

        result = _call(
            mock_client,
            fake_prompts_dir,
            must_achieve=must,
            forbidden=forbidden,
        )

        assert result.passed is True
        assert result.verdict == "no_conflict"
        assert result.conflicts == []

    def test_overlapping_vague_disclosure_scope_is_ambiguous(
        self, mock_client: MagicMock, fake_prompts_dir: Path
    ) -> None:
        """must/forbidden 同时以模糊程度词约束同一必要披露时才属歧义。"""
        must = ["深入、完整展示仪式的运作机制"]
        forbidden = ["本章不得披露过多仪式原理，只能有限揭示"]
        mock_client.complete.return_value = _make_response(
            verdict="ambiguous",
            conflicts=[
                {
                    "must_index": 0,
                    "must_text": must[0],
                    "forbidden_index": 0,
                    "forbidden_text": forbidden[0],
                    "reason": "完整、过多和有限没有共同尺度，Writer 必须猜实质边界",
                    "classification": "ambiguous",
                }
            ],
            evidence="同一必要披露同时受相反的模糊程度词约束",
        )

        result = _call(
            mock_client,
            fake_prompts_dir,
            must_achieve=must,
            forbidden=forbidden,
        )

        assert result.passed is False
        assert result.verdict == "ambiguous"
        assert result.conflicts[0].classification == "ambiguous"


# ---------------------------------------------------------------------------
# 3. Empty must_achieve → skipped
# ---------------------------------------------------------------------------


class TestEmptyMust:
    def test_empty_must_skips(
        self, mock_client: MagicMock, fake_prompts_dir: Path
    ) -> None:
        result = _call(
            mock_client,
            fake_prompts_dir,
            must_achieve=[],
            forbidden=["现代用语"],
        )

        assert result.passed is True
        assert result.skipped is True
        assert result.llm_calls_count == 0
        assert result.fail_open is False
        # 关键:LLM 不应被调用(skip 路径)
        assert mock_client.complete.call_count == 0


# ---------------------------------------------------------------------------
# 4. Empty forbidden → skipped
# ---------------------------------------------------------------------------


class TestEmptyForbidden:
    def test_empty_forbidden_skips(
        self, mock_client: MagicMock, fake_prompts_dir: Path
    ) -> None:
        result = _call(
            mock_client,
            fake_prompts_dir,
            must_achieve=["事件 A"],
            forbidden=[],
        )

        assert result.passed is True
        assert result.skipped is True
        assert result.llm_calls_count == 0
        assert mock_client.complete.call_count == 0


# ---------------------------------------------------------------------------
# 5. LLM fail → fail-open
# ---------------------------------------------------------------------------


class TestLLMFailOpen:
    def test_llm_raises_exception(
        self, mock_client: MagicMock, fake_prompts_dir: Path
    ) -> None:
        mock_client.complete.side_effect = RuntimeError("network timeout")
        result = _call(mock_client, fake_prompts_dir)

        assert result.passed is True
        assert result.skipped is False
        assert result.fail_open is True
        assert result.llm_calls_count == 1


# ---------------------------------------------------------------------------
# 6. Invalid JSON → fail-open
# ---------------------------------------------------------------------------


class TestInvalidJSON:
    def test_non_json_output_fails_open(
        self, mock_client: MagicMock, fake_prompts_dir: Path
    ) -> None:
        mock_client.complete.return_value = LLMResponse(
            content="<<<NOT JSON>>>",
            model_used="qwen3.7-max",
            usage=TokenUsage(input_tokens=600, output_tokens=10),
            finish_reason="stop",
        )
        result = _call(mock_client, fake_prompts_dir)

        assert result.passed is True
        assert result.fail_open is True

    def test_schema_mismatch_fails_open(
        self, mock_client: MagicMock, fake_prompts_dir: Path
    ) -> None:
        """JSON 合法但 schema 不符(缺 evidence 字段)→ fail-open。"""
        mock_client.complete.return_value = LLMResponse(
            content=json.dumps({"verdict": "no_conflict", "conflicts": []}),
            model_used="qwen3.7-max",
            usage=TokenUsage(input_tokens=600, output_tokens=10),
            finish_reason="stop",
        )
        result = _call(mock_client, fake_prompts_dir)

        assert result.passed is True
        assert result.fail_open is True

    def test_unescaped_quote_in_evidence_is_repaired(
        self, mock_client: MagicMock, fake_prompts_dir: Path
    ) -> None:
        """说明文本引用输入短语漏转义时，保留裁决而不是 failed-open。"""
        mock_client.complete.return_value = LLMResponse(
            content=(
                '{"verdict":"no_conflict","conflicts":[],'
                '"evidence":"forbidden 已写明"无论是否推算坐标"，边界清晰"}'
            ),
            model_used="qwen3.7-max",
            usage=TokenUsage(input_tokens=600, output_tokens=80),
            finish_reason="stop",
        )

        result = _call(mock_client, fake_prompts_dir)

        assert result.passed is True
        assert result.verdict == "no_conflict"
        assert result.fail_open is False
        assert "无论是否推算坐标" in result.evidence

    def test_overlong_evidence_is_truncated_without_changing_verdict(
        self, mock_client: MagicMock, fake_prompts_dir: Path
    ) -> None:
        """evidence 只作说明；超长时收敛到契约上限，裁决字段保持原样。"""
        mock_client.complete.return_value = _make_response(
            verdict="no_conflict",
            conflicts=[],
            evidence="证" * 301,
        )

        result = _call(mock_client, fake_prompts_dir)

        assert result.passed is True
        assert result.verdict == "no_conflict"
        assert result.conflicts == []
        assert result.fail_open is False
        assert result.evidence == "证" * 300
