"""StyleAuditor 真 agent 单元测(v1.1 wave 4 s5 / I2.2)。

覆盖契约:

- ``_severity_from_drift`` 4 档阈值边界(<2.5 ok / <5 minor / <7.5 warning / else critical)
- 短路:未选模板 → ok / drift=0,**不调 LLM**
- 短路:无章节正文 → ok / drift=0,**不调 LLM**
- LLM 路径(legacy client):mock 返低 drift 2.0 → severity=ok + dimension_notes
  + model_used 透传 + structlog ``style_audit`` event
- fail-open:client.complete 抛 → ok / drift=0 + summary 标 fail-open
- 解析失败:非法 JSON → fail-open
"""

from __future__ import annotations

from unittest.mock import MagicMock

from structlog.testing import capture_logs

from src.core.agents.style_auditor import (
    StyleAuditReport,
    _severity_from_drift,
    audit_style,
)
from src.core.llm.base import LLMClient, LLMResponse, TokenUsage
from src.core.llm.mock_client import MockLLMClient


def _make_response(content: str, model_used: str = "mock-model") -> LLMResponse:
    return LLMResponse(
        content=content,
        model_used=model_used,
        usage=TokenUsage(input_tokens=10, output_tokens=10),
        finish_reason="end_turn",
    )


class TestSeverityFromDrift:
    def test_thresholds(self) -> None:
        assert _severity_from_drift(0.0) == "ok"
        assert _severity_from_drift(2.49) == "ok"
        assert _severity_from_drift(2.5) == "minor"
        assert _severity_from_drift(4.99) == "minor"
        assert _severity_from_drift(5.0) == "warning"
        assert _severity_from_drift(7.49) == "warning"
        assert _severity_from_drift(7.5) == "critical"
        assert _severity_from_drift(10.0) == "critical"


class TestShortCircuit:
    def test_no_template_returns_ok_without_llm(self) -> None:
        client = MagicMock(spec=LLMClient)
        report = audit_style(
            workspace_id="ws",
            style_template=None,
            chapter_excerpts=["正文"],
            client=client,
            model="m",
        )
        assert isinstance(report, StyleAuditReport)
        assert report.severity == "ok"
        assert report.drift_score == 0.0
        assert report.model_used is None
        assert "未选写作风格模板" in report.summary
        client.complete.assert_not_called()

    def test_no_excerpts_returns_ok_without_llm(self) -> None:
        client = MagicMock(spec=LLMClient)
        report = audit_style(
            workspace_id="ws",
            style_template="comedic_burst",
            chapter_excerpts=[],
            client=client,
            model="m",
        )
        assert report.severity == "ok"
        assert report.drift_score == 0.0
        client.complete.assert_not_called()

    def test_invalid_template_name_short_circuits(self) -> None:
        client = MagicMock(spec=LLMClient)
        report = audit_style(
            workspace_id="ws",
            style_template="not_a_real_template",
            chapter_excerpts=["正文"],
            client=client,
            model="m",
        )
        assert report.severity == "ok"
        client.complete.assert_not_called()


class TestLLMPath:
    def test_mock_client_low_drift_ok(self) -> None:
        report = audit_style(
            workspace_id="ws",
            style_template="comedic_burst",
            chapter_excerpts=["“哈哈哈!”少年大笑。"],
            client=MockLLMClient(),
            model="mock",
        )
        assert report.severity == "ok"  # mock drift 2.0 → ok
        assert report.drift_score == 2.0
        assert report.model_used == "mock-mock"
        assert len(report.dimension_notes) == 3

    def test_emits_style_audit_event(self) -> None:
        with capture_logs() as logs:
            audit_style(
                workspace_id="ws-xyz",
                style_template="grand_epic",
                chapter_excerpts=["正文片段"],
                client=MockLLMClient(),
                model="mock",
            )
        events = [e for e in logs if e["event"] == "style_audit"]
        assert len(events) == 1
        assert events[0]["workspace_id"] == "ws-xyz"
        assert events[0]["severity"] == "ok"
        assert events[0]["style_template"] == "grand_epic"

    def test_high_drift_maps_to_severity(self) -> None:
        client = MagicMock(spec=LLMClient)
        client.complete.return_value = _make_response(
            '{"drift_score": 8.0, "dimension_notes": ["严重偏离"], '
            '"summary": "整体背离模板"}'
        )
        report = audit_style(
            workspace_id="ws",
            style_template="comedic_burst",
            chapter_excerpts=["正文"],
            client=client,
            model="m",
        )
        assert report.drift_score == 8.0
        assert report.severity == "critical"


class TestFailOpen:
    def test_llm_error_fails_open(self) -> None:
        client = MagicMock(spec=LLMClient)
        client.complete.side_effect = RuntimeError("boom")
        report = audit_style(
            workspace_id="ws",
            style_template="comedic_burst",
            chapter_excerpts=["正文"],
            client=client,
            model="m",
        )
        assert report.severity == "ok"
        assert report.drift_score == 0.0
        assert "fail-open" in report.summary

    def test_bad_json_fails_open(self) -> None:
        client = MagicMock(spec=LLMClient)
        client.complete.return_value = _make_response("这不是 JSON")
        report = audit_style(
            workspace_id="ws",
            style_template="comedic_burst",
            chapter_excerpts=["正文"],
            client=client,
            model="m",
        )
        assert report.severity == "ok"
        assert "fail-open" in report.summary
