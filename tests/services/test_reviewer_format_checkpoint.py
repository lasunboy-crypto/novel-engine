"""Known malformed Reviewer responses resume only the same source review."""

import json
from datetime import timedelta

import pytest

from src.core.db.models import AttemptLedgerORM, ChapterORM, ChapterVersionORM
from src.core.state_machine.states import ChapterState
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
    _insert_review,
    _write_version_file,
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
    at = lease.created_at + timedelta(seconds=2)
    _insert_attempt(
        harness,
        lease,
        status="succeeded",
        agent_name="reviewer",
        created_at=at,
        attempt_id="bad-review",
        operation_kind="agent_call",
    )
    with harness.session_factory(_WORKSPACE_ID) as session:
        attempt = session.get(AttemptLedgerORM, "bad-review")
        attempt.completed_at = at + timedelta(seconds=1)
        attempt.usage_status = "known"
        attempt.response_status = "completed"
        attempt.input_tokens = 10
        attempt.output_tokens = 20
        attempt.provider_request_id = "request-bad-format"
        session.add(
            AttemptLedgerORM(
                attempt_id="failed-pipeline",
                workspace_id=_WORKSPACE_ID,
                run_id=run.run_id,
                task_id=lease.task_id,
                chapter_id=_CHAPTER_1,
                attempt_kind="pipeline",
                operation_kind="pipeline",
                logical_call_id="failed-pipeline-call",
                agent_name="pipeline",
                provider="mock",
                model="mock-model",
                attempt_number=1,
                attempt_cap=3,
                status="failed",
                version_before=0,
                error_type="LocalJsonResponseViolation",
                created_at=lease.created_at,
                completed_at=at + timedelta(seconds=2),
            )
        )
        session.commit()
    folder = harness.paths.archives_dir / "responses/123456789abc"
    folder.mkdir(parents=True)
    path = folder / "response.json"
    path.write_text(
        json.dumps(
            {
                "content": '{"score_plot_progression":8',
                "model_used": "mock-model",
                "provider_request_id": "request-bad-format",
                "usage": {"input_tokens": 10, "output_tokens": 20},
                "finish_reason": "stop",
                "prompt_hash": "123456789abc",
                "captured_at": (at + timedelta(seconds=1.1)).isoformat(),
            }
        )
    )
    return lease, path


@pytest.mark.parametrize("projection", ["reviewing", "drafting"])
def test_known_invalid_response_releases_only_after_body_and_archive_proof(
    harness, monkeypatch, projection
):
    lease, path = seed(harness)
    with harness.session_factory(_WORKSPACE_ID) as session:
        session.get(ChapterORM, _CHAPTER_1).status = projection
        session.commit()
    before = path.read_bytes()
    assert harness.chapter_service.reconcile_operation_leases_on_startup(
        _WORKSPACE_ID, apply=False
    ) == ([_CHAPTER_1], [])
    assert harness.store.get(_WORKSPACE_ID, _CHAPTER_1) == lease
    assert harness.chapter_service.reconcile_operation_leases_on_startup(
        _WORKSPACE_ID
    ) == ([_CHAPTER_1], [])
    assert path.read_bytes() == before
    monkeypatch.setattr(harness.chapter_service, "_run_gates", lambda *a, **k: None)
    captured = {}
    monkeypatch.setattr(
        harness.chapter_service, "_orchestrate", lambda **k: captured.update(k)
    )
    harness.chapter_service.write(_WORKSPACE_ID, _CHAPTER_1)
    assert captured["initial_state"] == ChapterState.REVIEWING
    assert captured["initial_last_version_id"] == 1
    assert captured["initial_rewrite_count"] == 0
    checkpoint = captured["rewrite_checkpoint_context"]
    assert checkpoint.review is None and checkpoint.review_result is None
    assert len(checkpoint.previous_reviewer_parse_errors) == 1
    with harness.session_factory(_WORKSPACE_ID) as session:
        assert session.get(ChapterVersionORM, (_CHAPTER_1, 1)).content == "正文"
        assert session.get(ChapterORM, _CHAPTER_1).current_version == 1


@pytest.mark.parametrize(
    "damage",
    [
        "valid_response",
        "missing_archive",
        "duplicate_archive",
        "wrong_usage",
        "wrong_model",
        "length",
        "unknown_usage",
        "started_other",
        "success_gate",
        "wrong_failure",
        "disk_drift",
    ],
)
def test_uncertain_or_valid_unconsumed_review_stays_locked(harness, damage):
    lease, path = seed(harness)
    raw = json.loads(path.read_text())
    if damage == "valid_response":
        raw["content"] = '{"score_plot_progression":8}'
    elif damage == "wrong_usage":
        raw["usage"]["output_tokens"] = 21
    elif damage == "wrong_model":
        raw["model_used"] = "another-model"
    elif damage == "length":
        raw["finish_reason"] = "length"
    path.write_text(json.dumps(raw))
    if damage == "missing_archive":
        path.unlink()
    elif damage == "duplicate_archive":
        (path.parent / "duplicate.json").write_bytes(path.read_bytes())
    elif damage in {"started_other", "success_gate"}:
        _insert_attempt(
            harness,
            lease,
            status="started" if damage == "started_other" else "succeeded",
            agent_name="setting_consistency_validator",
            attempt_id="other-response",
            created_at=lease.created_at + timedelta(seconds=4),
        )
    elif damage in {"unknown_usage", "wrong_failure"}:
        with harness.session_factory(_WORKSPACE_ID) as session:
            if damage == "unknown_usage":
                session.get(AttemptLedgerORM, "bad-review").usage_status = "unknown"
            else:
                session.get(
                    AttemptLedgerORM, "failed-pipeline"
                ).error_type = "UnexpectedCrash"
            session.commit()
    elif damage == "existing_review":
        _insert_review(
            harness,
            version_id=1,
            reviewed_at=lease.created_at + timedelta(seconds=5),
            review_id="stored-review",
        )
    elif damage == "disk_drift":
        _write_version_file(harness, version_id=1, content="漂移")
    assert harness.chapter_service.reconcile_operation_leases_on_startup(
        _WORKSPACE_ID
    ) == ([], [_CHAPTER_1])
    assert harness.store.get(_WORKSPACE_ID, _CHAPTER_1) == lease


def test_persisted_review_uses_normal_checkpoint_instead_of_format_retry(harness):
    lease, _ = seed(harness)
    _insert_review(
        harness,
        version_id=1,
        reviewed_at=lease.created_at + timedelta(seconds=5),
        review_id="stored-review",
    )
    assert (
        harness.chapter_service._known_invalid_local_review_retry(
            _WORKSPACE_ID, _CHAPTER_1, current_version=1
        )
        is None
    )
    assert harness.chapter_service.reconcile_operation_leases_on_startup(
        _WORKSPACE_ID
    ) == ([_CHAPTER_1], [])
