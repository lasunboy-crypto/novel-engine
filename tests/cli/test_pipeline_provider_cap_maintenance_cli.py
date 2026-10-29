"""Provider cap 离线维护 CLI 的产品路径测试。"""

from __future__ import annotations

import json
from datetime import UTC, datetime, timedelta
from pathlib import Path

import pytest
from sqlalchemy import create_engine
from sqlalchemy.orm import Session

from src.cli import commands as cmd_module
from src.cli.commands import dispatch
from src.cli.main import build_parser
from src.core.db import Base
from src.core.db.models import (
    AttemptLedgerORM,
    ChapterORM,
    ChapterVersionORM,
    PipelineRunORM,
    WorkspaceORM,
)
from src.core.workspace.workspace_paths import WorkspacePaths
from src.services.backup_service import BackupService
from src.services.pipeline_provider_cap_maintenance import (
    PipelineProviderCapMaintenanceService,
)
from src.services.workspace_service import WorkspaceService
from src.version import ENGINE_VERSION

_WORKSPACE_ID = "ws-cap-cli"
_RUN_ID = "run_cap_cli"


@pytest.fixture
def cli_environment(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> tuple[WorkspaceService, Path]:
    workspaces_root = tmp_path / "workspaces"
    archives_root = tmp_path / "archives"
    workspaces_root.mkdir()
    archives_root.mkdir()
    workspace_service = WorkspaceService(
        workspaces_root=workspaces_root,
        archives_root=archives_root,
    )
    workspace_service.create(
        _WORKSPACE_ID,
        display_name="cap cli",
        genre="scifi",
        premise="checkpoint",
        activate=False,
    )
    paths = WorkspacePaths(
        _WORKSPACE_ID,
        workspaces_root=workspaces_root,
    )
    engine = create_engine(f"sqlite:///{paths.db_path}")
    Base.metadata.create_all(engine)
    now = datetime.now(UTC)
    with Session(engine) as session:
        session.add(
            WorkspaceORM(
                workspace_id=_WORKSPACE_ID,
                display_name="cap cli",
                genre="scifi",
                premise="checkpoint",
                created_at=now,
                last_active_at=now,
                engine_version=ENGINE_VERSION,
                status="active",
                total_chapters=1,
                completed_chapters=0,
                config_overrides={},
            )
        )
        session.add(
            ChapterORM(
                chapter_id="ch-0006",
                workspace_id=_WORKSPACE_ID,
                current_version=1,
                status="approved",
                rewrite_count=0,
                revise_count=0,
                created_at=now,
                approved_at=now,
                total_token_usage={},
                total_cost_usd=0.0,
                ready_for_next_chapter_at=None,
                memory_enrichment_status="pending",
                memory_enriched_at=None,
                memory_enrichment_details=None,
            )
        )
        session.add(
            PipelineRunORM(
                run_id=_RUN_ID,
                workspace_id=_WORKSPACE_ID,
                state="paused",
                scope="single",
                start_chapter_id="ch-0006",
                end_chapter_id=None,
                volume_id=None,
                profile_override=None,
                chapter_ids=["ch-0006"],
                current_chapter="ch-0006",
                completed_chapter_ids=[],
                pause_reason="continuity_ledger_not_ready",
                error_details=None,
                failure_policy="stop",
                skipped_chapter_ids=[],
                provider_call_cap=2,
                provider_call_count=2,
                transport_retry_count=0,
                pipeline_attempt_count=1,
                upstream_generation_attempt_count=0,
                created_at=now,
                updated_at=now,
                completed_at=None,
            )
        )
        for index, status in enumerate(("succeeded", "started"), start=1):
            session.add(
                AttemptLedgerORM(
                    attempt_id=f"att-cli-{index}",
                    workspace_id=_WORKSPACE_ID,
                    run_id=_RUN_ID,
                    task_id=f"task-cli-{index}",
                    chapter_id="ch-0006",
                    attempt_kind="provider",
                    operation_kind="agent_call",
                    logical_call_id=f"call-cli-{index}",
                    agent_name="reviewer",
                    provider="openai_compatible",
                    model="qwen3.7-max",
                    attempt_number=1,
                    attempt_cap=2,
                    status=status,
                    version_before=1,
                    version_after=1 if status == "succeeded" else None,
                    error_type=None,
                    created_at=now,
                    completed_at=now if status == "succeeded" else None,
                )
            )
        session.commit()
    engine.dispose()

    backup_service = BackupService(
        workspace_service=workspace_service,
        ws_manager=None,
        active_task_checker=lambda _workspace_id: False,
    )
    maintenance_service = PipelineProviderCapMaintenanceService(
        workspace_service=workspace_service,
        backup_service=backup_service,
        active_task_checker=lambda _workspace_id: False,
    )
    monkeypatch.setattr(
        cmd_module,
        "make_workspace_service",
        lambda: workspace_service,
    )
    monkeypatch.setattr(
        cmd_module,
        "make_pipeline_provider_cap_maintenance_service",
        lambda _workspace_service: maintenance_service,
    )

    def _llm_factory_must_not_run(*_args, **_kwargs):
        raise AssertionError("cap maintenance must not construct an LLM service")

    monkeypatch.setattr(
        cmd_module,
        "make_chapter_service",
        _llm_factory_must_not_run,
    )
    return workspace_service, paths.db_path


def _run_cli(*, json_output: bool = False, confirm: str | None = None) -> int:
    args = [
        "pipeline",
        "increase-provider-cap",
        _WORKSPACE_ID,
        "--run-id",
        _RUN_ID,
        "--expected-current-cap",
        "2",
        "--new-cap",
        "4",
        "--confirm",
        confirm or f"{_WORKSPACE_ID}:{_RUN_ID}:2:4",
    ]
    if json_output:
        args.append("--json")
    parser = build_parser()
    return dispatch(parser.parse_args(args))


def test_cli_human_output_uses_maintenance_service_without_llm(
    cli_environment: tuple[WorkspaceService, Path],
    capsys: pytest.CaptureFixture,
) -> None:
    _workspace_service, db_path = cli_environment

    assert _run_cli() == 0

    output = capsys.readouterr().out
    assert f"workspace_id          : {_WORKSPACE_ID}" in output
    assert f"run_id                : {_RUN_ID}" in output
    assert "provider_call_cap     : 2 -> 4" in output
    assert "restart_required      : true" in output
    assert "next_action           : 从现有 Run UI 点击 Continue" in output
    with create_engine(f"sqlite:///{db_path}").connect() as connection:
        cap = connection.exec_driver_sql(
            "SELECT provider_call_cap FROM pipeline_runs WHERE run_id = ?",
            (_RUN_ID,),
        ).scalar_one()
    assert cap == 4


def test_cli_accepts_clean_chapter_escalated_checkpoint(
    cli_environment: tuple[WorkspaceService, Path],
    capsys: pytest.CaptureFixture,
) -> None:
    workspace_service, db_path = cli_environment
    engine = create_engine(f"sqlite:///{db_path}")
    now = datetime.now(UTC)
    content = "第六章待修正文"
    with Session(engine) as session:
        run = session.get(PipelineRunORM, _RUN_ID)
        chapter = session.get(ChapterORM, "ch-0006")
        assert run is not None
        assert chapter is not None
        run.pause_reason = "chapter_escalated"
        run.completed_at = now - timedelta(days=1)
        run.updated_at = now
        chapter.status = "human_intervention"
        chapter.approved_at = None
        chapter.escalation_details = {
            "reason": "repeated_hard_constraint_no_progress"
        }
        session.add(
            ChapterVersionORM(
                chapter_id="ch-0006",
                version_id=1,
                content=content,
                word_count=len(content),
                created_at=now,
                creation_type="rewrite",
                model_used="qwen3.7-max",
                review_score=8.2,
                notes="",
                style_fingerprint=None,
            )
        )
        for attempt in session.query(AttemptLedgerORM).all():
            attempt.status = "succeeded"
            attempt.completed_at = now
        session.commit()
    engine.dispose()
    paths = WorkspacePaths(
        _WORKSPACE_ID,
        workspaces_root=workspace_service.workspaces_root,
    )
    version_path = paths.chapter_dir("ch-0006") / "v1.md"
    version_path.parent.mkdir(parents=True, exist_ok=True)
    version_path.write_text(content, encoding="utf-8")

    assert _run_cli() == 0

    output = capsys.readouterr().out
    assert "pause_reason          : chapter_escalated" in output
    assert "next_action           : 从现有 Chapter UI 点击 Rewrite" in output
    with create_engine(f"sqlite:///{db_path}").connect() as connection:
        cap = connection.exec_driver_sql(
            "SELECT provider_call_cap FROM pipeline_runs WHERE run_id = ?",
            (_RUN_ID,),
        ).scalar_one()
    assert cap == 4


def test_cli_json_and_wrong_confirm_paths(
    cli_environment: tuple[WorkspaceService, Path],
    capsys: pytest.CaptureFixture,
) -> None:
    _workspace_service, db_path = cli_environment

    assert _run_cli(confirm="wrong") == 1
    captured = capsys.readouterr()
    assert "--confirm" in captured.err
    with create_engine(f"sqlite:///{db_path}").connect() as connection:
        cap = connection.exec_driver_sql(
            "SELECT provider_call_cap FROM pipeline_runs WHERE run_id = ?",
            (_RUN_ID,),
        ).scalar_one()
    assert cap == 2

    assert _run_cli(json_output=True) == 0
    output = capsys.readouterr().out
    payload = json.loads(output.splitlines()[-1])
    assert payload["workspace_id"] == _WORKSPACE_ID
    assert payload["previous_cap"] == 2
    assert payload["new_cap"] == 4
    assert payload["restart_required"] is True


def test_cli_requires_confirmation_flag() -> None:
    parser = build_parser()
    with pytest.raises(SystemExit) as exc_info:
        parser.parse_args(
            [
                "pipeline",
                "increase-provider-cap",
                _WORKSPACE_ID,
                "--run-id",
                _RUN_ID,
                "--expected-current-cap",
                "2",
                "--new-cap",
                "4",
            ]
        )
    assert exc_info.value.code == 2
