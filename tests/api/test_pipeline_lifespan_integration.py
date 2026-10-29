"""P6-B-03 lifespan 启动 hook 集成测试(v2 w1-s2 改判后语义)。

对齐 ``src/api/main.py`` ``_run_pipeline_startup_scan``:扫所有工作区
``pipeline_runs.state='running'`` 行,数据完整可恢复(``chapter_ids``
非空)→ 标 ``interrupted``(可 :resume 断点续跑,v2 w1-s2 改判,原
P6-B-03 一律标 error)。

测试范围:

- **`_run_pipeline_startup_scan` fail-closed**(deps/DB 失败必须阻止启动)
- **真实路径**:tmp data_dir + 一个 workspace + 一个 active running 行 →
  hook 跑后 DB 行标 interrupted
"""

from __future__ import annotations

import asyncio
from datetime import UTC, datetime
from pathlib import Path

import pytest
from sqlalchemy import create_engine, select
from sqlalchemy.orm import Session

from src.config import settings as _settings
from src.core.db import Base, PipelineRunORM, WorkspaceORM
from src.core.workspace.workspace_paths import WorkspacePaths
from src.services.workspace_version_service import get_current_schema_version


@pytest.fixture
def tmp_data_dir(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> Path:
    """临时 data_dir + 替 ``settings.data_dir`` 让 WorkspaceService 走 tmp。"""
    data_dir = tmp_path / "data"
    data_dir.mkdir()
    monkeypatch.setattr(_settings, "data_dir", data_dir)
    return data_dir


def _create_workspace_with_running_run(workspace_id: str, data_dir: Path) -> str:
    """在 data_dir 下手工建一个 workspace 目录 + 落 meta.json + novel.db +
    1 行 state='running' 的 pipeline_runs。"""
    ws_dir = data_dir / workspace_id
    ws_dir.mkdir(parents=True, exist_ok=True)
    data_subdir = ws_dir / "data"
    data_subdir.mkdir(exist_ok=True)

    # meta.json(WorkspaceService.list() 走 meta.json 判定合法工作区)
    import json

    now_iso = datetime.now(UTC).isoformat()
    meta = {
        "workspace_id": workspace_id,
        "display_name": "x",
        "genre": "x",
        "premise": "p",
        "created_at": now_iso,
        "last_active_at": now_iso,
        "engine_version": "0.1.0",
        "schema_version": get_current_schema_version(),
        "status": "active",
        "total_chapters": 1,
        "completed_chapters": 0,
        "config_overrides": {},
    }
    (ws_dir / "meta.json").write_text(json.dumps(meta), encoding="utf-8")

    # novel.db + pipeline_runs 表 + 1 行 running
    paths = WorkspacePaths(workspace_id, workspaces_root=data_dir)
    engine = create_engine(f"sqlite:///{paths.db_path}")
    Base.metadata.create_all(engine)
    now = datetime.now(UTC)
    with Session(engine) as session:
        session.merge(
            WorkspaceORM(
                workspace_id=workspace_id,
                display_name="x",
                genre="x",
                premise="p",
                created_at=now,
                last_active_at=now,
                engine_version="0.1.0",
                status="active",
                total_chapters=1,
                completed_chapters=0,
                config_overrides={},
            )
        )
        session.add(
            PipelineRunORM(
                run_id="run-stuck",
                workspace_id=workspace_id,
                state="running",
                scope="single",
                start_chapter_id="ch-0001",
                end_chapter_id=None,
                volume_id=None,
                profile_override=None,
                chapter_ids=["ch-0001"],
                current_chapter="ch-0001",
                completed_chapter_ids=[],
                pause_reason=None,
                error_details=None,
                created_at=now,
                updated_at=now,
                completed_at=None,
            )
        )
        session.commit()
    return workspace_id


def test_run_pipeline_startup_scan_marks_running_row_as_interrupted(
    tmp_data_dir: Path,
) -> None:
    """lifespan hook 集成路径(v2 w1-s2 改判):发现 running 行且
    ``chapter_ids`` 非空 → 标 interrupted(可 resume 断点续跑)。"""
    from src.api.main import _run_pipeline_startup_scan

    _create_workspace_with_running_run("ws-stuck", tmp_data_dir)

    # 调 lifespan hook
    _run_pipeline_startup_scan()

    # 验证 DB 行已翻 interrupted + 中断章可见
    paths = WorkspacePaths("ws-stuck", workspaces_root=tmp_data_dir)
    engine = create_engine(f"sqlite:///{paths.db_path}")
    with Session(engine) as session:
        row = session.scalars(
            select(PipelineRunORM).where(PipelineRunORM.run_id == "run-stuck")
        ).one()
        assert row.state == "interrupted"
        assert row.error_details == {
            "reason": "engine_restarted_mid_run",
            "interrupted_chapter": "ch-0001",
        }


def test_run_pipeline_startup_scan_no_workspaces_no_op(
    tmp_data_dir: Path,
) -> None:
    """空 data_dir 不抛错(workspace_service.list 返空)。"""
    from src.api.main import _run_pipeline_startup_scan

    _run_pipeline_startup_scan()  # 无 workspace,无 active row,no-op


def test_run_pipeline_startup_scan_propagates_builder_failure(
    tmp_data_dir: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """启动恢复不可读时必须阻止应用启动，不能留下 running 僵尸。"""

    from src.api.main import _run_pipeline_startup_scan

    monkeypatch.setattr(
        "src.api.deps.build_pipeline_components_for_lifespan",
        lambda: (_ for _ in ()).throw(RuntimeError("broken recovery builder")),
    )
    with pytest.raises(RuntimeError, match="broken recovery builder"):
        _run_pipeline_startup_scan()


def test_lifespan_task_db_corruption_blocks_later_recovery_writes(
    tmp_data_dir: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    from sqlalchemy.exc import DatabaseError

    from src.api import main as main_module
    from src.api import task_registry
    from src.services.task_db import make_task_session_factory

    (tmp_data_dir / "_tasks.db").write_bytes(b"not a sqlite database")
    task_registry.set_session_factory(make_task_session_factory(data_dir=tmp_data_dir))
    original_pipeline_scan = main_module._run_pipeline_startup_scan
    applied_calls: list[bool] = []
    admission_calls: list[bool] = []

    def _track_admission_scan() -> list[str]:
        admission_calls.append(True)
        return []

    def _track_pipeline_scan(
        admitted_workspace_ids: list[str] | None = None,
        *,
        apply: bool = True,
    ) -> None:
        if apply:
            applied_calls.append(True)
        original_pipeline_scan(admitted_workspace_ids, apply=apply)

    monkeypatch.setattr(
        main_module,
        "_run_pipeline_startup_scan",
        _track_pipeline_scan,
    )
    monkeypatch.setattr(
        main_module,
        "_run_workspace_recovery_admission_scan",
        _track_admission_scan,
    )

    async def _start() -> None:
        async with main_module._lifespan(main_module.app):
            pass

    try:
        with pytest.raises(DatabaseError):
            asyncio.run(_start())
    finally:
        task_registry.reset_session_factory()

    assert admission_calls == []
    assert applied_calls == []


def test_lifespan_local_recovery_never_unwraps_provider_secret(
    tmp_data_dir: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    from pydantic import SecretStr

    from src.api import main as main_module
    from src.api import task_registry
    from src.services.task_db import make_task_session_factory

    def _forbid_secret_read(_self: SecretStr) -> str:
        raise AssertionError("lifespan must not read Provider credentials")

    monkeypatch.setattr(SecretStr, "get_secret_value", _forbid_secret_read)
    task_registry.set_session_factory(make_task_session_factory(data_dir=tmp_data_dir))

    async def _start() -> None:
        async with main_module._lifespan(main_module.app):
            pass

    try:
        asyncio.run(_start())
    finally:
        task_registry.reset_session_factory()
