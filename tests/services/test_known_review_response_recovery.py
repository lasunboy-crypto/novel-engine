"""Known empty must responses reuse source-bound results, never Writer or new verdicts."""

import json
from datetime import timedelta

import pytest

from src.core.db.models import AttemptLedgerORM, ChapterORM
from src.core.llm.base import LLMResponse, Message, TokenUsage
from src.core.llm.prompt_hash import hash_prompt, render_prompt_text
from src.core.state_machine.states import ChapterState
from src.services.known_review_response_recovery import _RecoveryClient
from tests.services.test_chapter_operation_lease import (
    harness as harness,
    _WORKSPACE_ID,
    _CHAPTER_1,
    _acquire,
    _mark_phase,
    _seed_provider_checkpoint,
    _start_pipeline,
    _set_run_checkpoint,
    _insert_attempt,
)


def seed(harness):
    run = _start_pipeline(harness)
    _set_run_checkpoint(
        harness, run.run_id, state="running", current_chapter=_CHAPTER_1
    )
    lease = _mark_phase(
        harness,
        _acquire(harness, operation_kind="write", run_id=run.run_id),
        "provider_capable_pipeline",
    )
    _set_run_checkpoint(harness, run.run_id, state="paused", current_chapter=_CHAPTER_1)
    _seed_provider_checkpoint(harness)
    paths = []
    for i, agent in enumerate(
        ("reviewer", "must_achieve_validator", "continuity_validator")
    ):
        at = lease.created_at + timedelta(seconds=2 + i)
        _insert_attempt(
            harness,
            lease,
            status="succeeded",
            agent_name=agent,
            created_at=at,
            attempt_id=agent,
            operation_kind="agent_call",
        )
        with harness.session_factory(_WORKSPACE_ID) as session:
            row = session.get(AttemptLedgerORM, agent)
            row.completed_at = at + timedelta(seconds=0.1)
            row.usage_status = "known"
            row.response_status = "completed"
            row.input_tokens, row.output_tokens = 10, 20
            row.provider_request_id = agent
            session.commit()
        messages = [Message(role="user", content=agent)]
        digest = hash_prompt(messages)
        folder = harness.paths.archives_dir / "responses" / digest
        folder.mkdir(parents=True)
        path = folder / "response.json"
        response = LLMResponse(
            content="" if agent == "must_achieve_validator" else "{}",
            model_used="mock-model",
            finish_reason="stop",
            provider_request_id=agent,
            usage=TokenUsage(input_tokens=10, output_tokens=20),
        )
        raw = response.model_dump(mode="json")
        raw.update(
            prompt_hash=digest, captured_at=(at + timedelta(seconds=0.2)).isoformat()
        )
        path.write_text(json.dumps(raw))
        prompt = harness.paths.archives_dir / "prompts" / f"{digest}.txt"
        prompt.parent.mkdir(parents=True, exist_ok=True)
        prompt.write_text(render_prompt_text(messages))
        paths.append(path)
    with harness.session_factory(_WORKSPACE_ID) as session:
        session.add(
            AttemptLedgerORM(
                attempt_id="failed-pipeline",
                workspace_id=_WORKSPACE_ID,
                run_id=run.run_id,
                task_id=lease.task_id,
                chapter_id=_CHAPTER_1,
                attempt_kind="pipeline",
                operation_kind="chapter_execution",
                attempt_number=1,
                attempt_cap=1,
                status="failed",
                error_type="MustAchieveTechnicalError",
                created_at=lease.created_at,
                completed_at=lease.created_at + timedelta(seconds=6),
            )
        )
        session.commit()
    return lease, paths


def load(harness):
    return harness.chapter_service._known_empty_must_recovery(
        _WORKSPACE_ID, _CHAPTER_1, current_version=1
    )


@pytest.mark.parametrize("projection", ["reviewing", "drafting"])
def test_cold_start_and_same_write_recover_existing_results_without_new_version(
    harness, monkeypatch, projection
):
    lease, paths = seed(harness)
    with harness.session_factory(_WORKSPACE_ID) as session:
        session.get(ChapterORM, _CHAPTER_1).status = projection
        session.commit()
    before = [path.read_bytes() for path in paths]
    assert harness.chapter_service.reconcile_operation_leases_on_startup(
        _WORKSPACE_ID, apply=False
    ) == ([_CHAPTER_1], [])
    assert harness.store.get(_WORKSPACE_ID, _CHAPTER_1) == lease
    assert harness.chapter_service.reconcile_operation_leases_on_startup(
        _WORKSPACE_ID
    ) == ([_CHAPTER_1], [])
    monkeypatch.setattr(harness.chapter_service, "_run_gates", lambda *a, **k: None)
    captured = {}
    monkeypatch.setattr(
        harness.chapter_service, "_orchestrate", lambda **k: captured.update(k)
    )
    harness.chapter_service.write(_WORKSPACE_ID, _CHAPTER_1)
    assert captured["initial_state"] == ChapterState.REVIEWING
    assert (
        captured["initial_last_version_id"] == 1
        and captured["initial_rewrite_count"] == 0
    )
    assert len(captured["review_response_recovery"].entries) == 3
    assert [path.read_bytes() for path in paths] == before


@pytest.mark.parametrize(
    "damage",
    [
        "missing_archive",
        "duplicate_archive",
        "wrong_usage",
        "wrong_model",
        "length",
        "unknown",
        "started",
        "other_task",
        "other_agent",
        "nonempty_must",
        "disk_drift",
        "missing_prompt",
        "wrong_failure",
        "second_must",
    ],
)
def test_uncertain_or_consumed_results_never_release_the_lease(harness, damage):
    lease, paths = seed(harness)
    path = paths[1]
    raw = json.loads(path.read_text())
    if damage == "missing_archive":
        path.unlink()
    elif damage == "duplicate_archive":
        (path.parent / "duplicate.json").write_bytes(path.read_bytes())
    elif damage in {"wrong_usage", "wrong_model", "length", "nonempty_must"}:
        if damage == "wrong_usage":
            raw["usage"]["output_tokens"] = 21
        if damage == "wrong_model":
            raw["model_used"] = "other"
        if damage == "length":
            raw["finish_reason"] = "length"
        if damage == "nonempty_must":
            raw["content"] = "{}"
        path.write_text(json.dumps(raw))
    elif damage == "disk_drift":
        harness.paths.chapter_version_file(_CHAPTER_1, "v1.md").write_text("changed")
    elif damage == "missing_prompt":
        (harness.paths.archives_dir / "prompts" / f"{path.parent.name}.txt").unlink()
    elif damage == "second_must":
        _insert_attempt(
            harness,
            lease,
            status="succeeded",
            agent_name="must_achieve_validator",
            attempt_id="second-must",
            created_at=lease.created_at + timedelta(seconds=5),
        )
    else:
        with harness.session_factory(_WORKSPACE_ID) as session:
            row = session.get(AttemptLedgerORM, "must_achieve_validator")
            if damage == "unknown":
                row.usage_status = "unknown"
            if damage == "started":
                row.status = "started"
            if damage == "other_task":
                row.task_id = "other"
            if damage == "other_agent":
                row.agent_name = "writer"
            if damage == "wrong_failure":
                session.get(AttemptLedgerORM, "failed-pipeline").error_type = "other"
            session.commit()
    assert load(harness) is None
    assert harness.chapter_service.reconcile_operation_leases_on_startup(
        _WORKSPACE_ID
    ) == ([], [_CHAPTER_1])
    assert harness.store.get(_WORKSPACE_ID, _CHAPTER_1) == lease


def test_exact_request_consumes_archive_once_and_changed_request_never_calls_provider(
    harness,
):
    seed(harness)
    recovery = load(harness)

    class Inner:
        calls = 0

        def messages_for_cost_reserve(self, messages, params):
            return messages

        def complete(self, messages, **params):
            self.calls += 1
            return LLMResponse(
                content="new",
                model_used="mock-model",
                usage=TokenUsage(input_tokens=1, output_tokens=1),
            )

    inner = Inner()
    client = _RecoveryClient(inner, "reviewer", recovery)
    with pytest.raises(RuntimeError, match="不一致"):
        client.complete([Message(role="user", content="changed")], model="mock-model")
    assert inner.calls == 0
    assert (
        client.complete(
            [Message(role="user", content="reviewer")], model="mock-model"
        ).content
        == "{}"
    )
    assert inner.calls == 0
    assert (
        client.complete(
            [Message(role="user", content="new version")], model="mock-model"
        ).content
        == "new"
    )
    assert inner.calls == 1


@pytest.mark.parametrize("damage", [None, "other_error", "archive_missing", "new_provider", "lease"])
def test_local_input_mismatch_can_only_resume_the_verified_same_run(harness, damage):
    from src.core.db.models import PipelineRunORM
    from src.services.known_review_response_recovery import REVIEW_INPUT_MISMATCH_MESSAGE
    from src.services.pipeline_service import PipelineNotRunningError
    from src.services.chapter_service import ContinuityCheckpointCorruptionError

    lease, paths = seed(harness)
    assert harness.chapter_service.reconcile_operation_leases_on_startup(_WORKSPACE_ID) == ([_CHAPTER_1], [])
    with harness.session_factory(_WORKSPACE_ID) as session:
        row = session.get(PipelineRunORM, lease.run_id)
        row.state = "error"
        row.error_details = {
            "error_type": "ValueError", "message": REVIEW_INPUT_MISMATCH_MESSAGE,
            "chapter_id": _CHAPTER_1,
        }
        if damage == "other_error":
            row.error_details = {**row.error_details, "message": "unrelated"}
        session.commit()
    if damage == "archive_missing":
        paths[0].unlink()
    elif damage == "new_provider":
        _insert_attempt(harness, lease, status="started", agent_name="reviewer", attempt_id="new-review", created_at=lease.created_at + timedelta(seconds=8))
    elif damage == "lease":
        # A real lease must prevent duplicate ownership even if all archives match.
        _acquire(harness, source_version=1)
    status = harness.pipeline_service.status(_WORKSPACE_ID)
    if damage is None:
        assert status.error_details["recovery_kind"] == "known_review_archive_checkpoint"
        assert status.error_details["recovery_ready"] is True
        resumed = harness.pipeline_service.resume(_WORKSPACE_ID)
        assert resumed.run_id == lease.run_id and resumed.state == "running"
        assert load(harness) is not None
    elif damage == "other_error":
        assert "recovery_kind" not in status.error_details
        with pytest.raises(PipelineNotRunningError):
            harness.pipeline_service.resume(_WORKSPACE_ID)
    else:
        from src.services.chapter_operation_lease import ChapterOperationLeaseError
        assert status.error_details["recovery_ready"] is False
        with pytest.raises((ContinuityCheckpointCorruptionError, ChapterOperationLeaseError)):
            harness.pipeline_service.resume(_WORKSPACE_ID)
