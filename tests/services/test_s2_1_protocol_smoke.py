"""S2-1 three-candidate protocol smoke governance (Provider 0 tests only)."""

from __future__ import annotations

import json
from datetime import datetime
from pathlib import Path
from typing import Any

import pytest
from sqlalchemy import create_engine, inspect, select
from sqlalchemy.orm import Session

from src.core.db.models import AttemptLedgerORM
from src.core.llm.attempt_context import (
    ProviderAttemptOutcome,
    run_observed_provider_attempt,
)
from src.core.llm.base import LLMClient, LLMResponse, Message, TokenUsage
from src.core.config_loader import load_writer_profiles_config
from src.core.routing.run_model_manifest_router import (
    RunModelManifestLockError,
    build_model_router,
)
from src.services.s2_1_protocol_smoke import (
    APPROVAL_CONFIRMATION,
    CANDIDATE_ORDER,
    S2_1_TASK_ID,
    SmokePreflightError,
    build_smoke_plan,
    execute_approved_smoke,
    prepare_ephemeral_manifest_config,
    reconcile_provider_billing,
)


class _SmokeMockClient(LLMClient):
    def __init__(
        self,
        model: str,
        calls: list[tuple[str, dict[str, Any]]],
        *,
        fail_first_json: bool = False,
        raise_first_json: bool = False,
        include_request_id: bool = True,
    ) -> None:
        self._model = model
        self._calls = calls
        self._fail_first_json = fail_first_json
        self._raise_first_json = raise_first_json
        self._include_request_id = include_request_id
        self._model_call_count = 0

    def prepare_bounded_non_thinking_call(
        self,
        gen_params: dict[str, Any],
        *,
        max_completion_tokens: int,
    ) -> dict[str, Any]:
        prepared = dict(gen_params)
        for key in (
            "max_tokens",
            "max_completion_tokens",
            "enable_thinking",
            "preserve_thinking",
            "thinking_budget",
            "reasoning_effort",
            "thinking",
        ):
            prepared.pop(key, None)
        prepared["max_completion_tokens"] = max_completion_tokens
        prepared["thinking"] = {"type": "disabled"}
        return prepared

    def complete(
        self,
        messages: list[Message],
        **gen_params: Any,
    ) -> LLMResponse:
        del messages
        response_format = gen_params.get("response_format")
        is_json = response_format == {"type": "json_object"} or (
            isinstance(response_format, dict)
            and response_format.get("type") == "json_schema"
        )
        self._calls.append((self._model, dict(gen_params)))
        call_number = len(self._calls)
        self._model_call_count += 1
        model_call_number = self._model_call_count

        def physical() -> LLMResponse:
            if self._raise_first_json and is_json and model_call_number == 1:
                raise RuntimeError("synthetic transport failure")
            if is_json:
                content = (
                    '{"marker":"wrong"}'
                    if self._fail_first_json and model_call_number == 1
                    else (
                        '{"marker":"s2-1-synthetic-v1",'
                        '"code_name":"青灯","status":"ready"}'
                    )
                )
                reasoning_chars = 0
            else:
                content = "从早到晚的顺序是甲、乙、丙。"
                reasoning_chars = 12
            response = LLMResponse(
                content=content,
                model_used=self._model,
                usage=TokenUsage(input_tokens=17, output_tokens=9),
                finish_reason="stop",
                provider_request_id=(
                    f"req-{self._model}-{call_number}"
                    if self._include_request_id
                    else None
                ),
            )
            response._reasoning_chars = reasoning_chars
            return response

        return run_observed_provider_attempt(
            physical,
            technical_attempt=1,
            outcome_from_result=lambda response: ProviderAttemptOutcome.known(
                input_tokens=response.usage.input_tokens,
                output_tokens=response.usage.output_tokens,
                provider_request_id=response.provider_request_id,
            ),
        )


def test_plan_freezes_exact_payload_bounds_costs_and_zero_retry() -> None:
    plan = build_smoke_plan(Path("config.yaml"))

    assert plan["provider_calls"] == 0
    assert plan["candidate_order"] == list(CANDIDATE_ORDER)
    assert plan["logical_requests_per_candidate"] == 2
    assert plan["max_physical_requests_per_candidate"] == 2
    assert plan["same_model_retry_limit"] == 0
    assert plan["aggregate"] == {
        "logical_requests": 6,
        "max_physical_requests": 6,
        "configured_rmb_cap": "0.55",
    }
    assert plan["render_input_upper"] == {
        "json_object": 613,
        "hy3_json_schema": 806,
        "thinking": 302,
    }
    assert {
        row["candidate"]: (row["configured_reserve_rmb"], row["rmb_cap"])
        for row in plan["candidates"]
    } == {
        "hy3": ("0.050004", "0.10"),
        "deepseek-v4-flash": ("0.112761", "0.20"),
        "qwen3.8-27b": ("0.149433", "0.25"),
    }
    by_candidate = {row["candidate"]: row for row in plan["candidates"]}
    hy3_json = by_candidate["hy3"]["wire_contract"]["json_object"]
    assert hy3_json["response_format"]["type"] == "json_schema"
    assert hy3_json["response_format"]["json_schema"]["strict"] is True
    assert (
        hy3_json["response_format"]["json_schema"]["schema"]["additionalProperties"]
        is False
    )
    assert by_candidate["hy3"]["wire_contract"]["thinking_plain_text"] == {
        "max_completion_tokens": 12_096,
        "temperature": 0.7,
        "thinking": {"type": "enabled", "budget_tokens": 4096},
        "reasoning_effort": "low",
        "stream": True,
        "stream_options": {"include_usage": True},
    }
    assert by_candidate["deepseek-v4-flash"]["wire_contract"]["json_object"] == {
        "max_tokens": 128,
        "response_format": {"type": "json_object"},
        "stream": True,
        "stream_options": {"include_usage": True},
        "thinking": {"type": "disabled"},
    }
    assert by_candidate["qwen3.8-27b"]["wire_contract"]["thinking_plain_text"] == {
        "max_completion_tokens": 12_096,
        "temperature": 0.7,
        "enable_thinking": True,
        "thinking_budget": 4096,
        "preserve_thinking": False,
        "stream": True,
        "stream_options": {"include_usage": True},
    }


def test_ephemeral_config_does_not_mutate_provider_zero_source(tmp_path: Path) -> None:
    source = Path("config.yaml")
    before = source.read_bytes()
    target = tmp_path / "candidate.yaml"

    prepare_ephemeral_manifest_config(
        source,
        candidate="deepseek-v4-flash",
        destination=target,
    )

    assert source.read_bytes() == before
    import yaml

    raw = yaml.safe_load(target.read_text(encoding="utf-8"))
    assert raw["run_model_manifest"]["active"] == "deepseek-v4-flash"
    assert raw["run_model_manifest"]["manifests"]["deepseek-v4-flash"][
        "selection_test"
    ] == {
        "activity_scope": "selection_test",
        "max_physical_requests": 2,
        "max_configured_rmb": "0.20",
    }
    assert (
        raw["run_model_manifest"]["manifests"]["hy3"]["selection_test"][
            "max_physical_requests"
        ]
        == 0
    )
    assert target.stat().st_mode & 0o777 == 0o600


def test_smoke_route_is_rejected_without_exact_selection_scope(tmp_path: Path) -> None:
    path = tmp_path / "candidate.yaml"
    prepare_ephemeral_manifest_config(
        Path("config.yaml"),
        candidate="hy3",
        destination=path,
    )
    calls: list[tuple[str, dict[str, Any]]] = []
    router = build_model_router(
        load_writer_profiles_config(path),
        config_path=path,
        client_factory=lambda **kwargs: _SmokeMockClient(kwargs["model"], calls),
    )

    with pytest.raises(RunModelManifestLockError, match="selection-test"):
        router.route("smoke")
    assert calls == []


def test_historical_real_smoke_runner_cannot_authorize_new_native_protocol(
    tmp_path: Path,
) -> None:
    with pytest.raises(SmokePreflightError, match="历史 S2-1 批准不能授权"):
        execute_approved_smoke(
            source_config=Path("config.yaml"),
            activity_dir=tmp_path / "activity",
            confirmation=APPROVAL_CONFIRMATION,
            credential_presence={candidate: True for candidate in CANDIDATE_ORDER},
        )

    assert not (tmp_path / "activity").exists()


def test_mock_execution_records_six_attempts_without_workspace_or_run(
    tmp_path: Path,
) -> None:
    calls: list[tuple[str, dict[str, Any]]] = []
    activity = tmp_path / "activity"

    report = execute_approved_smoke(
        source_config=Path("config.yaml"),
        activity_dir=activity,
        confirmation=APPROVAL_CONFIRMATION,
        credential_presence={candidate: True for candidate in CANDIDATE_ORDER},
        client_factory=lambda **kwargs: _SmokeMockClient(kwargs["model"], calls),
        now=datetime.fromisoformat("2026-08-28T12:30:00+08:00"),
    )

    assert report["status"] == "protocol_pass"
    assert report["provider_calls"] == 6
    assert report["usage_known_attempts"] == 6
    assert report["usage_unknown_attempts"] == 0
    assert report["provider_request_id_count"] == 6
    assert report["input_tokens_known"] == 102
    assert report["output_tokens_known"] == 54
    assert report["total_tokens_known"] == 156
    assert [model for model, _params in calls] == [
        "hy3",
        "hy3",
        "deepseek-v4-flash",
        "deepseek-v4-flash",
        "qwen3.8-27b",
        "qwen3.8-27b",
    ]
    assert len(report["logical_results"]) == 6
    assert report["failed_candidates"] == []
    assert [arm["status"] for arm in report["arm_results"]] == [
        "protocol_pass",
        "protocol_pass",
        "protocol_pass",
    ]
    assert all(arm["provider_calls"] == 2 for arm in report["arm_results"])
    assert all(
        [attempt["kind"] for attempt in arm["attempts"]] == ["json_object", "thinking"]
        for arm in report["arm_results"]
    )
    assert all(
        result["transport"] == "openai_chat_completions_sse"
        and result["sse_terminal_received"] is True
        and result["request_model"] == result["response_model"]
        for result in report["logical_results"]
    )
    assert report["provider_actual_bill"] == {
        "status": "optional_unreconciled",
        "amount_rmb": None,
        "evidence": None,
    }
    assert "control_plane_attestations" not in report

    ledger = activity / "attempt-ledger.sqlite3"
    assert ledger.stat().st_mode & 0o777 == 0o600
    engine = create_engine(f"sqlite:///{ledger}")
    assert inspect(engine).get_table_names() == ["attempt_ledger"]
    with Session(engine) as session:
        rows = list(session.scalars(select(AttemptLedgerORM)))
    assert len(rows) == 6
    assert all(row.run_id is None and row.chapter_id is None for row in rows)
    assert all(row.attempt_number == 1 for row in rows)
    assert all(row.usage_status == "known" for row in rows)


def test_protocol_failure_stops_only_failed_arm_and_later_arms_continue(
    tmp_path: Path,
) -> None:
    calls: list[tuple[str, dict[str, Any]]] = []
    activity = tmp_path / "activity"

    report = execute_approved_smoke(
        source_config=Path("config.yaml"),
        activity_dir=activity,
        confirmation=APPROVAL_CONFIRMATION,
        credential_presence={candidate: True for candidate in CANDIDATE_ORDER},
        client_factory=lambda **kwargs: _SmokeMockClient(
            kwargs["model"],
            calls,
            fail_first_json=kwargs["model"] == "hy3",
        ),
        now=datetime.fromisoformat("2026-08-28T12:30:00+08:00"),
    )

    assert report["status"] == "protocol_failed"
    assert report["provider_calls"] == 5
    assert report["usage_known_attempts"] == 5
    assert report["usage_unknown_attempts"] == 0
    assert report["failed_candidates"] == ["hy3"]
    assert [arm["status"] for arm in report["arm_results"]] == [
        "protocol_failed",
        "protocol_pass",
        "protocol_pass",
    ]
    failed_arm = report["arm_results"][0]
    assert failed_arm["provider_calls"] == 1
    assert failed_arm["failed_logical_kind"] == "json_object"
    assert failed_arm["logical_results"] == []
    assert failed_arm["failure"]["error_type"] == "StrictJsonResponseViolation"
    assert [model for model, _params in calls] == [
        "hy3",
        "deepseek-v4-flash",
        "deepseek-v4-flash",
        "qwen3.8-27b",
        "qwen3.8-27b",
    ]


def test_missing_credential_stops_only_that_arm(tmp_path: Path) -> None:
    calls: list[tuple[str, dict[str, Any]]] = []

    report = execute_approved_smoke(
        source_config=Path("config.yaml"),
        activity_dir=tmp_path / "activity",
        confirmation=APPROVAL_CONFIRMATION,
        credential_presence={
            "hy3": True,
            "deepseek-v4-flash": False,
            "qwen3.8-27b": True,
        },
        client_factory=lambda **kwargs: _SmokeMockClient(kwargs["model"], calls),
        now=datetime.fromisoformat("2026-08-28T12:30:00+08:00"),
    )

    assert report["status"] == "protocol_failed"
    assert report["failed_candidates"] == ["deepseek-v4-flash"]
    assert report["provider_calls"] == 4
    assert [arm["status"] for arm in report["arm_results"]] == [
        "protocol_pass",
        "preflight_failed",
        "protocol_pass",
    ]
    assert report["arm_results"][1]["provider_calls"] == 0
    assert [model for model, _params in calls] == [
        "hy3",
        "hy3",
        "qwen3.8-27b",
        "qwen3.8-27b",
    ]


def test_unknown_transport_usage_is_preserved_and_later_arm_continues(
    tmp_path: Path,
) -> None:
    calls: list[tuple[str, dict[str, Any]]] = []

    report = execute_approved_smoke(
        source_config=Path("config.yaml"),
        activity_dir=tmp_path / "activity",
        confirmation=APPROVAL_CONFIRMATION,
        credential_presence={candidate: True for candidate in CANDIDATE_ORDER},
        client_factory=lambda **kwargs: _SmokeMockClient(
            kwargs["model"],
            calls,
            raise_first_json=kwargs["model"] == "deepseek-v4-flash",
        ),
        now=datetime.fromisoformat("2026-08-28T12:30:00+08:00"),
    )

    assert report["status"] == "protocol_failed"
    assert report["failed_candidates"] == ["deepseek-v4-flash"]
    assert report["provider_calls"] == 5
    assert report["usage_known_attempts"] == 4
    assert report["usage_unknown_attempts"] == 1
    deepseek = report["arm_results"][1]
    assert deepseek["status"] == "protocol_failed"
    assert deepseek["failed_logical_kind"] == "json_object"
    assert deepseek["usage_known_attempts"] == 0
    assert deepseek["usage_unknown_attempts"] == 1
    assert deepseek["configured_rmb"] != "0"
    assert deepseek["attempts"][0]["usage_status"] == "unknown"
    assert report["arm_results"][2]["status"] == "protocol_pass"


def test_missing_provider_request_id_is_observed_but_not_a_pipeline_gate(
    tmp_path: Path,
) -> None:
    calls: list[tuple[str, dict[str, Any]]] = []

    report = execute_approved_smoke(
        source_config=Path("config.yaml"),
        activity_dir=tmp_path / "activity",
        confirmation=APPROVAL_CONFIRMATION,
        credential_presence={candidate: True for candidate in CANDIDATE_ORDER},
        client_factory=lambda **kwargs: _SmokeMockClient(
            kwargs["model"],
            calls,
            include_request_id=False,
        ),
        now=datetime.fromisoformat("2026-08-28T12:30:00+08:00"),
    )

    assert report["status"] == "protocol_pass"
    assert report["provider_calls"] == 6
    assert report["provider_request_id_count"] == 0


def test_billing_reconciliation_keeps_unavailable_distinct_from_zero(
    tmp_path: Path,
) -> None:
    calls: list[tuple[str, dict[str, Any]]] = []
    activity = tmp_path / "activity"
    execute_approved_smoke(
        source_config=Path("config.yaml"),
        activity_dir=activity,
        confirmation=APPROVAL_CONFIRMATION,
        credential_presence={candidate: True for candidate in CANDIDATE_ORDER},
        client_factory=lambda **kwargs: _SmokeMockClient(kwargs["model"], calls),
        now=datetime.fromisoformat("2026-08-28T12:30:00+08:00"),
    )
    bill_file = tmp_path / "bill.json"
    evidence_file = tmp_path / "sanitized-bill.txt"
    evidence_file.write_text("sanitized bill evidence", encoding="utf-8")
    bill_file.write_text(
        json.dumps(
            {
                "schema_version": "s2-1-provider-billing-v1",
                "task_id": S2_1_TASK_ID,
                "observed_at": "2026-08-28T15:00:00+08:00",
                "records": [
                    {
                        "candidate": "hy3",
                        "status": "known",
                        "currency": "CNY",
                        "amount_rmb": "0.01",
                        "evidence_files": [str(evidence_file)],
                    },
                    {
                        "candidate": "deepseek-v4-flash",
                        "status": "unavailable",
                        "currency": None,
                        "amount_rmb": None,
                    },
                    {
                        "candidate": "qwen3.8-27b",
                        "status": "known",
                        "currency": "CNY",
                        "amount_rmb": "0.02",
                        "evidence_files": [str(evidence_file)],
                    },
                ],
            },
            ensure_ascii=False,
        ),
        encoding="utf-8",
    )

    reconciliation = reconcile_provider_billing(
        report_path=activity / "s2-1-smoke-report.json",
        billing_evidence_path=bill_file,
    )

    assert reconciliation["status"] == "billing_reconciled_with_unknown"
    deepseek = next(
        row
        for row in reconciliation["records"]
        if row["candidate"] == "deepseek-v4-flash"
    )
    assert deepseek["provider_bill_amount_rmb"] is None
    assert deepseek["provider_bill_status"] == "unavailable"
    assert deepseek["evidence"] == []
