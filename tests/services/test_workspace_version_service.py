"""WorkspaceVersionService 单测(P6-B-08)。

覆盖维度(对齐 plan §10 测试约定 5 类):

- **happy**:get_version_status / migrate(走通完整 pre-migration 备份 +
  alembic upgrade + meta.json 落盘 + WS publish)+ startup scan 聚合统计
- **error**:confirm 不匹配 → WorkspaceMigrationConfirmInvalidError;workspace
  major 跨级 → WorkspaceVersionIncompatibleError;backup 失败 →
  WorkspaceMigrationBackupFailedError
- **gate**:get_version_status 旧工作区(schema_version=None / 旧 head 字符串)
  返 migration_required=true
- **rollback**:alembic upgrade 抛错时 pre_migration 备份仍保留(用户能从该
  备份手动 restore)
- **WS event**:happy 路径配套 ws_manager.publish 调用断言;publish 失败
  swallow + structlog 不阻塞主流程

不依赖真 LLM;真 alembic upgrade 跑 tmp_path sqlite db(沿用
``tests/db/test_p4_b_01_metrics.py`` ``monkeypatch.setenv("NOVEL_DB_PATH", ...)``
模式)。
"""

from __future__ import annotations

import json
import sqlite3
import zipfile
from collections.abc import Iterator
from dataclasses import replace
from datetime import UTC, datetime
from pathlib import Path
from typing import Any

import pytest
from sqlalchemy import MetaData, create_engine, inspect, select, text
from sqlalchemy.orm import Session

from src.core.db import (
    Base,
    ChapterCardORM,
    ChapterORM,
    CharacterORM,
    ReviewResultORM,
    TimelineEntryORM,
    WorkspaceORM,
)
from src.core.workspace.workspace_paths import WorkspacePaths
from src.services.backup_service import (
    BackupService,
    BackupServiceError,
)
from src.services.workspace_service import WorkspaceService
from src.services.workspace_version_service import (
    WorkspaceMigrationBackupFailedError,
    WorkspaceMigrationConfirmInvalidError,
    WorkspaceStartupRecoveryAdmissionError,
    WorkspaceVersionIncompatibleError,
    WorkspaceVersionService,
    WorkspaceVersionServiceError,
    _read_sqlite_schema_snapshot,
    _schema_shape_mismatches,
    admit_workspaces_for_startup_recovery,
    get_current_schema_version,
    run_workspace_version_startup_scan,
)
from src.version import ENGINE_VERSION


# ---------------------------------------------------------------------------
# Fakes & fixtures
# ---------------------------------------------------------------------------


class _FakeWSManager:
    """记录所有 publish 调用,供断言。"""

    def __init__(self, *, raise_exc: Exception | None = None) -> None:
        self.events: list[dict[str, Any]] = []
        self._raise_exc = raise_exc

    def publish(self, *, channel: str, event: str, data: dict[str, Any]) -> None:
        if self._raise_exc is not None:
            raise self._raise_exc
        self.events.append({"channel": channel, "event": event, "data": data})


@pytest.fixture
def workspace_service(tmp_path: Path) -> WorkspaceService:
    workspaces_root = tmp_path / "workspaces"
    archives_root = tmp_path / "archives"
    workspaces_root.mkdir()
    archives_root.mkdir()
    return WorkspaceService(
        workspaces_root=workspaces_root, archives_root=archives_root
    )


@pytest.fixture
def backup_service(workspace_service: WorkspaceService) -> BackupService:
    return BackupService(workspace_service=workspace_service, ws_manager=None)


@pytest.fixture
def ws_manager() -> _FakeWSManager:
    return _FakeWSManager()


@pytest.fixture
def version_service(
    workspace_service: WorkspaceService,
    backup_service: BackupService,
    ws_manager: _FakeWSManager,
) -> WorkspaceVersionService:
    return WorkspaceVersionService(
        workspace_service=workspace_service,
        backup_service=backup_service,
        ws_manager=ws_manager,
    )


@pytest.fixture
def workspace_id(workspace_service: WorkspaceService) -> str:
    workspace_service.create(
        "ws-version-test",
        display_name="P6-B-08 version test",
        genre="scifi",
        premise="test premise",
        activate=False,
    )
    return "ws-version-test"


@pytest.fixture
def isolate_alembic_env(monkeypatch: pytest.MonkeyPatch) -> Iterator[None]:
    """确保测试期间 NOVEL_DB_PATH 由本测试控制,不串到其他测试。

    WorkspaceVersionService._run_alembic_upgrade 通过 os.environ 临时设置 +
    finally 还原原值;为防外层若提前设置串入,这里 monkeypatch 清空确保
    清白起点(monkeypatch 在 fixture teardown 时自动恢复)。
    """
    monkeypatch.delenv("NOVEL_DB_PATH", raising=False)
    yield


def _rewrite_meta_schema_version(
    workspace_service: WorkspaceService,
    workspace_id: str,
    *,
    schema_version: str | None,
    engine_version: str | None = None,
) -> None:
    """手工把 meta.json 的 schema_version / engine_version 改成旧值,模拟
    P6-B-08 前创建的工作区。"""
    meta_path = workspace_service.workspaces_root / workspace_id / "meta.json"
    payload = json.loads(meta_path.read_text(encoding="utf-8"))
    if schema_version is None:
        payload.pop("schema_version", None)
        payload["schema_version"] = None
    else:
        payload["schema_version"] = schema_version
    if engine_version is not None:
        payload["engine_version"] = engine_version
    meta_path.write_text(
        json.dumps(payload, ensure_ascii=False, indent=2), encoding="utf-8"
    )


def _create_unversioned_product_db(
    workspace_service: WorkspaceService,
    workspace_id: str,
    *,
    include_operation_lease: bool,
    legacy_missing_defaults: bool = False,
) -> Path:
    """模拟产品历史 ``Base.create_all`` 形态：全业务表但无 Alembic stamp。"""

    db_path = WorkspacePaths(
        workspace_id,
        workspaces_root=workspace_service.workspaces_root,
    ).db_path
    engine = create_engine(f"sqlite:///{db_path}")
    if legacy_missing_defaults:
        if include_operation_lease:
            raise ValueError("legacy 0029 形态不能预含 0030 operation lease")
        legacy_metadata = MetaData()
        for table in Base.metadata.sorted_tables:
            if table.name != "chapter_operation_leases":
                table.to_metadata(legacy_metadata)
        for table_name, column_name in _LEGACY_DEFAULT_COLUMNS:
            legacy_metadata.tables[table_name].columns[
                column_name
            ].server_default = None
        legacy_metadata.create_all(engine)
    else:
        Base.metadata.create_all(engine)
    if not include_operation_lease and not legacy_missing_defaults:
        with engine.begin() as connection:
            connection.exec_driver_sql("DROP TABLE chapter_operation_leases")
    now = datetime.now(UTC)
    with Session(engine) as session:
        session.add(
            WorkspaceORM(
                workspace_id=workspace_id,
                display_name="schema marker",
                genre="scifi",
                premise="preserve me",
                created_at=now,
                last_active_at=now,
                engine_version=ENGINE_VERSION,
                status="active",
                total_chapters=7,
                completed_chapters=2,
                config_overrides={"marker": "r8"},
            )
        )
        session.commit()
    engine.dispose()
    return db_path


_LEGACY_DEFAULT_COLUMNS = frozenset(
    {
        ("chapter_cards", "card_status"),
        ("characters", "identity_facets"),
        ("characters", "state_timeline"),
        ("review_results", "constraint_violations"),
        ("review_results", "constraint_results"),
        ("review_results", "hard_constraint_regressions"),
        ("timeline_entries", "is_flashback"),
    }
)


def _seed_legacy_default_rows(db_path: Path, workspace_id: str) -> None:
    """写入覆盖七项 default 所在表的非空哨兵，证明 batch migration 不丢行。"""

    engine = create_engine(f"sqlite:///{db_path}")
    now = datetime.now(UTC)
    with Session(engine) as session:
        session.add(
            ChapterORM(
                chapter_id="ch-legacy-defaults",
                workspace_id=workspace_id,
                current_version=1,
                status="approved",
                rewrite_count=0,
                revise_count=0,
                created_at=now,
                approved_at=now,
                total_token_usage={},
                total_cost_usd=0.0,
            )
        )
        session.add_all(
            [
                ChapterCardORM(
                    chapter_id="ch-legacy-defaults",
                    workspace_id=workspace_id,
                    card_status="approved",
                    created_at=now,
                ),
                CharacterORM(
                    character_id="char-legacy-defaults",
                    workspace_id=workspace_id,
                    name="迁移哨兵",
                    aliases=[],
                    archetype="witness",
                    appearance="unchanged",
                    personality="careful",
                    background="legacy",
                    identity_facets=[{"context": "migration", "identity": "witness"}],
                    voice_profile={"tone": "plain"},
                    goals_long_term=[],
                    goals_short_term=[],
                    current_state={},
                    state_timeline=[{"as_of_chapter": "ch-legacy-defaults"}],
                    relationships={},
                    abilities=[],
                    secrets=[],
                    first_appearance_chapter="ch-legacy-defaults",
                    last_updated_chapter="ch-legacy-defaults",
                ),
                TimelineEntryORM(
                    entry_id="timeline-legacy-defaults",
                    workspace_id=workspace_id,
                    chapter_id="ch-legacy-defaults",
                    description="legacy row",
                    time_label="before migration",
                    time_order=1,
                    involved_characters=["char-legacy-defaults"],
                    is_flashback=True,
                ),
            ]
        )
        session.flush()
        session.add(
            ReviewResultORM(
                review_id="review-legacy-defaults",
                chapter_id="ch-legacy-defaults",
                version_reviewed=1,
                reviewed_at=now,
                reviewer_model="legacy-model",
                score_plot_progression=9.0,
                score_tension_match=9.0,
                score_prose_quality=9.0,
                score_consistency=9.0,
                score_hook_management=9.0,
                score_voice_consistency=9.0,
                score_overall=9.0,
                hard_constraints_passed=True,
                failed_constraints=[],
                constraint_violations=[{"constraint_id": "legacy"}],
                constraint_results=[{"constraint_id": "legacy", "passed": True}],
                hard_constraint_regressions=["legacy-regression-marker"],
                suggestions=[],
                verdict="pass",
            )
        )
        session.commit()
    engine.dispose()


def _assert_legacy_default_rows(db_path: Path) -> None:
    """确认迁移哨兵的七项显式值原样保留。"""

    engine = create_engine(f"sqlite:///{db_path}")
    with Session(engine) as session:
        card = session.get(ChapterCardORM, "ch-legacy-defaults")
        character = session.get(CharacterORM, "char-legacy-defaults")
        timeline = session.get(TimelineEntryORM, "timeline-legacy-defaults")
        review = session.get(ReviewResultORM, "review-legacy-defaults")
        assert card is not None and card.card_status == "approved"
        assert character is not None
        assert character.identity_facets == [
            {"context": "migration", "identity": "witness"}
        ]
        assert character.state_timeline == [{"as_of_chapter": "ch-legacy-defaults"}]
        assert timeline is not None and timeline.is_flashback is True
        assert review is not None
        assert review.constraint_violations == [{"constraint_id": "legacy"}]
        assert review.constraint_results == [
            {"constraint_id": "legacy", "passed": True}
        ]
        assert review.hard_constraint_regressions == ["legacy-regression-marker"]
    engine.dispose()


def _prepare_supported_migration_source(
    workspace_service: WorkspaceService,
    workspace_id: str,
) -> str:
    """构造真实 direct-predecessor 产品库，而不是拿空 SQLite 冒充旧库。"""

    previous_schema = "0029_structured_rewrite_constraint_results"
    _rewrite_meta_schema_version(
        workspace_service,
        workspace_id,
        schema_version=previous_schema,
    )
    db_path = _create_unversioned_product_db(
        workspace_service,
        workspace_id,
        include_operation_lease=False,
    )
    connection = sqlite3.connect(db_path)
    try:
        connection.execute(
            "CREATE TABLE alembic_version "
            "(version_num VARCHAR(32) NOT NULL PRIMARY KEY)"
        )
        connection.execute(
            "INSERT INTO alembic_version(version_num) VALUES (?)",
            (previous_schema,),
        )
        connection.commit()
    finally:
        connection.close()
    return previous_schema


# ---------------------------------------------------------------------------
# happy
# ---------------------------------------------------------------------------


class TestGetVersionStatus:
    def test_fresh_workspace_matches_current(
        self,
        version_service: WorkspaceVersionService,
        workspace_id: str,
    ) -> None:
        """新创建工作区 schema_version=当前 head,migration_required=False。"""
        status = version_service.get_version_status(workspace_id)
        assert status.engine_version_match is True
        assert status.schema_version_match is True
        assert status.current_engine == ENGINE_VERSION
        assert status.workspace_engine == ENGINE_VERSION
        assert status.current_schema == get_current_schema_version()
        assert status.workspace_schema == get_current_schema_version()
        assert status.migration_required is False

    def test_old_schema_returns_migration_required(
        self,
        version_service: WorkspaceVersionService,
        workspace_service: WorkspaceService,
        workspace_id: str,
    ) -> None:
        """旧 schema_version → schema_match=False / migration_required=True。"""
        previous_schema = "0008_chapter_invalidated_force_pass"
        _rewrite_meta_schema_version(
            workspace_service,
            workspace_id,
            schema_version=previous_schema,
        )
        status = version_service.get_version_status(workspace_id)
        assert status.schema_version_match is False
        assert status.migration_required is True
        assert status.workspace_schema == previous_schema
        assert status.current_schema == get_current_schema_version()

    def test_null_schema_returns_migration_required(
        self,
        version_service: WorkspaceVersionService,
        workspace_service: WorkspaceService,
        workspace_id: str,
    ) -> None:
        """schema_version=None(P6-B-08 前老工作区)→ migration_required=True。"""
        _rewrite_meta_schema_version(
            workspace_service, workspace_id, schema_version=None
        )
        status = version_service.get_version_status(workspace_id)
        assert status.schema_version_match is False
        assert status.workspace_schema is None
        assert status.migration_required is True


class TestMigrateHappy:
    def test_migrate_from_old_schema_runs_alembic_and_updates_meta(
        self,
        version_service: WorkspaceVersionService,
        workspace_service: WorkspaceService,
        workspace_id: str,
        ws_manager: _FakeWSManager,
        isolate_alembic_env: None,
    ) -> None:
        """模拟旧工作区(schema_version=0008)→ migrate 走完整流程:
        ① pre-migration manual backup 落盘
        ② alembic upgrade head 应用(对幂等 db 无副作用)
        ③ meta.json schema_version 翻新
        ④ WS publish version_migrated 到 workspace.events 频道
        """
        previous_schema = _prepare_supported_migration_source(
            workspace_service,
            workspace_id,
        )

        result = version_service.migrate(workspace_id, workspace_id)

        # 返回结构正确
        assert result.workspace_id == workspace_id
        assert result.from_schema_version == previous_schema
        assert result.to_schema_version == get_current_schema_version()
        assert result.from_engine_version == ENGINE_VERSION
        assert result.to_engine_version == ENGINE_VERSION
        assert result.pre_migration_backup_id.startswith("manual_")

        # pre-migration backup zip 落盘
        backup_zip = (
            workspace_service.workspaces_root
            / workspace_id
            / "backups"
            / f"{result.pre_migration_backup_id}.zip"
        )
        assert backup_zip.is_file()

        # meta.json 已落新 schema_version
        meta = workspace_service.info(workspace_id)
        assert meta.schema_version == get_current_schema_version()

        # 再查 version-status 已 match
        status = version_service.get_version_status(workspace_id)
        assert status.migration_required is False

        # WS event 推送
        version_events = [
            e for e in ws_manager.events if e["event"] == "version_migrated"
        ]
        assert len(version_events) == 1
        event = version_events[0]
        assert event["channel"] == f"workspace.{workspace_id}.events"
        assert event["data"]["from_schema_version"] == previous_schema
        assert event["data"]["to_schema_version"] == get_current_schema_version()
        assert (
            event["data"]["pre_migration_backup_id"] == result.pre_migration_backup_id
        )

    def test_existing_empty_old_database_is_rejected_before_backup_or_ddl(
        self,
        version_service: WorkspaceVersionService,
        workspace_service: WorkspaceService,
        workspace_id: str,
    ) -> None:
        previous_schema = "0029_structured_rewrite_constraint_results"
        _rewrite_meta_schema_version(
            workspace_service,
            workspace_id,
            schema_version=previous_schema,
        )
        db_path = WorkspacePaths(
            workspace_id,
            workspaces_root=workspace_service.workspaces_root,
        ).db_path
        connection = sqlite3.connect(db_path)
        try:
            connection.execute("VACUUM")
        finally:
            connection.close()
        before = db_path.read_bytes()

        with pytest.raises(WorkspaceVersionServiceError, match="数据库为空"):
            version_service.migrate(workspace_id, workspace_id)

        assert db_path.read_bytes() == before
        assert not list(
            (workspace_service.workspaces_root / workspace_id / "backups").glob(
                "manual_*_migration_*.zip"
            )
        )

    @pytest.mark.parametrize(
        ("include_operation_lease", "legacy_missing_defaults"),
        [(False, False), (False, True), (True, False)],
        ids=["canonical-0029", "legacy-create-all-0029", "current-shape"],
    )
    def test_migrate_unversioned_0029_product_db_preserves_data(
        self,
        version_service: WorkspaceVersionService,
        backup_service: BackupService,
        workspace_service: WorkspaceService,
        workspace_id: str,
        isolate_alembic_env: None,
        include_operation_lease: bool,
        legacy_missing_defaults: bool,
    ) -> None:
        """真实 create_all/no-stamp 0029 经备份、验真、stamp 后升到 0030。"""

        _rewrite_meta_schema_version(
            workspace_service,
            workspace_id,
            schema_version="0029_structured_rewrite_constraint_results",
        )
        db_path = _create_unversioned_product_db(
            workspace_service,
            workspace_id,
            include_operation_lease=include_operation_lease,
            legacy_missing_defaults=legacy_missing_defaults,
        )
        if legacy_missing_defaults:
            _seed_legacy_default_rows(db_path, workspace_id)

        result = version_service.migrate(workspace_id, workspace_id)

        assert result.from_schema_version == (
            "0029_structured_rewrite_constraint_results"
        )
        engine = create_engine(f"sqlite:///{db_path}")
        assert "chapter_operation_leases" in inspect(engine).get_table_names()
        with Session(engine) as session:
            marker = session.scalars(
                select(WorkspaceORM).where(WorkspaceORM.workspace_id == workspace_id)
            ).one()
            assert marker.total_chapters == 7
            assert marker.config_overrides == {"marker": "r8"}
            revision = session.execute(
                text("SELECT version_num FROM alembic_version")
            ).scalar_one()
        engine.dispose()
        assert revision == get_current_schema_version()
        migrated_snapshot = _read_sqlite_schema_snapshot(db_path)
        assert (
            _schema_shape_mismatches(
                migrated_snapshot,
                include_operation_lease=True,
            )
            == []
        )
        if legacy_missing_defaults:
            _assert_legacy_default_rows(db_path)

        backup_zip = (
            workspace_service.workspaces_root
            / workspace_id
            / "backups"
            / f"{result.pre_migration_backup_id}.zip"
        )
        with zipfile.ZipFile(backup_zip, "r") as zf:
            backup_meta = json.loads(zf.read("backup_meta.json").decode("utf-8"))
        expected_backup_schema = (
            get_current_schema_version()
            if include_operation_lease
            else "0029_structured_rewrite_constraint_results"
        )
        assert backup_meta["schema_version"] == expected_backup_schema

        restored = backup_service.restore_from_backup(
            workspace_id,
            result.pre_migration_backup_id,
        )
        assert restored.backup_id == result.pre_migration_backup_id
        restored_engine = create_engine(f"sqlite:///{db_path}")
        restored_tables = set(inspect(restored_engine).get_table_names())
        restored_engine.dispose()
        assert ("chapter_operation_leases" in restored_tables) is (
            include_operation_lease
        )
        assert workspace_service.info(workspace_id).schema_version == (
            expected_backup_schema
        )
        if legacy_missing_defaults:
            restored_snapshot = _read_sqlite_schema_snapshot(db_path)
            assert not restored_snapshot.has_alembic_version_table
            for table_name, column_name in _LEGACY_DEFAULT_COLUMNS:
                assert (
                    restored_snapshot.column_defaults[table_name][column_name] is None
                )
            _assert_legacy_default_rows(db_path)

    def test_migrate_reads_committed_wal_via_consistent_snapshot(
        self,
        version_service: WorkspaceVersionService,
        workspace_service: WorkspaceService,
        workspace_id: str,
        isolate_alembic_env: None,
    ) -> None:
        """0029 已提交数据只在 WAL 时仍可正常预检、备份并迁移且不丢数据。"""

        previous_schema = "0029_structured_rewrite_constraint_results"
        _rewrite_meta_schema_version(
            workspace_service,
            workspace_id,
            schema_version=previous_schema,
        )
        db_path = _create_unversioned_product_db(
            workspace_service,
            workspace_id,
            include_operation_lease=False,
        )
        live = sqlite3.connect(db_path)
        try:
            assert live.execute("PRAGMA journal_mode=WAL").fetchone() == ("wal",)
            live.execute("PRAGMA wal_autocheckpoint=0")
            live.execute(
                "UPDATE workspaces SET config_overrides = ? WHERE workspace_id = ?",
                ('{"marker":"committed-in-wal"}', workspace_id),
            )
            live.commit()
            wal_path = Path(f"{db_path}-wal")
            assert wal_path.is_file()
            assert wal_path.stat().st_size > 0

            result = version_service.migrate(workspace_id, workspace_id)

            assert result.from_schema_version == previous_schema
            marker = live.execute(
                "SELECT config_overrides FROM workspaces WHERE workspace_id = ?",
                (workspace_id,),
            ).fetchone()
            revision = live.execute(
                "SELECT version_num FROM alembic_version"
            ).fetchone()
        finally:
            live.close()
        assert marker == ('{"marker":"committed-in-wal"}',)
        assert revision == (get_current_schema_version(),)

    def test_migrate_rejects_other_workspace_database_before_backup_or_write(
        self,
        version_service: WorkspaceVersionService,
        workspace_service: WorkspaceService,
        workspace_id: str,
    ) -> None:
        previous_schema = "0029_structured_rewrite_constraint_results"
        _rewrite_meta_schema_version(
            workspace_service,
            workspace_id,
            schema_version=previous_schema,
        )
        db_path = _create_unversioned_product_db(
            workspace_service,
            workspace_id,
            include_operation_lease=False,
        )
        connection = sqlite3.connect(db_path)
        try:
            connection.execute(
                "UPDATE workspaces SET workspace_id = ? WHERE workspace_id = ?",
                ("ws-foreign-source", workspace_id),
            )
            connection.commit()
        finally:
            connection.close()
        before = db_path.read_bytes()
        backups_dir = workspace_service.workspaces_root / workspace_id / "backups"

        with pytest.raises(WorkspaceVersionServiceError, match="异工作区"):
            version_service.migrate(workspace_id, workspace_id)

        assert db_path.read_bytes() == before
        assert list(backups_dir.glob("*.zip")) == []
        assert workspace_service.info(workspace_id).schema_version == (previous_schema)

    def test_head_database_with_previous_meta_only_repairs_meta(
        self,
        version_service: WorkspaceVersionService,
        workspace_service: WorkspaceService,
        workspace_id: str,
        isolate_alembic_env: None,
        monkeypatch: pytest.MonkeyPatch,
        tmp_path: Path,
    ) -> None:
        """0030 DDL 已提交但 meta 仍 0029 时，验真后只补 meta。"""

        previous_schema = "0029_structured_rewrite_constraint_results"
        _rewrite_meta_schema_version(
            workspace_service,
            workspace_id,
            schema_version=previous_schema,
        )
        db_path = _create_unversioned_product_db(
            workspace_service,
            workspace_id,
            include_operation_lease=False,
        )
        WorkspaceVersionService._run_alembic_migration(
            db_path,
            stamp_revision=previous_schema,
        )

        def _unexpected_database_write(*args: Any, **kwargs: Any) -> None:
            raise AssertionError(f"不应重复执行 Alembic: {args=} {kwargs=}")

        monkeypatch.setattr(
            version_service,
            "_run_alembic_upgrade",
            _unexpected_database_write,
        )
        monkeypatch.setattr(
            version_service,
            "_run_alembic_migration",
            _unexpected_database_write,
        )

        result = version_service.migrate(workspace_id, workspace_id)

        assert result.from_schema_version == previous_schema
        assert workspace_service.info(workspace_id).schema_version == (
            get_current_schema_version()
        )
        engine = create_engine(f"sqlite:///{db_path}")
        with Session(engine) as session:
            revision = session.execute(
                text("SELECT version_num FROM alembic_version")
            ).scalar_one()
            marker = session.scalars(
                select(WorkspaceORM).where(WorkspaceORM.workspace_id == workspace_id)
            ).one()
        engine.dispose()
        assert revision == get_current_schema_version()
        assert marker.config_overrides == {"marker": "r8"}

        backup_zip = (
            workspace_service.workspaces_root
            / workspace_id
            / "backups"
            / f"{result.pre_migration_backup_id}.zip"
        )
        extract_root = tmp_path / "partial-meta-repair-backup"
        with zipfile.ZipFile(backup_zip, "r") as zf:
            backup_meta = json.loads(zf.read("backup_meta.json").decode("utf-8"))
            archived_workspace_meta = json.loads(zf.read("meta.json").decode("utf-8"))
            zf.extract("data/novel.db", path=extract_root)
        assert backup_meta["schema_version"] == get_current_schema_version()
        assert archived_workspace_meta["schema_version"] == (
            get_current_schema_version()
        )
        archived_db = sqlite3.connect(extract_root / "data" / "novel.db")
        try:
            archived_revision = archived_db.execute(
                "SELECT version_num FROM alembic_version"
            ).fetchone()
        finally:
            archived_db.close()
        assert archived_revision == (get_current_schema_version(),)

    def test_previous_stamp_with_current_shape_resumes_ddl_stamp_crash(
        self,
        version_service: WorkspaceVersionService,
        backup_service: BackupService,
        workspace_service: WorkspaceService,
        workspace_id: str,
        isolate_alembic_env: None,
    ) -> None:
        """0030 表已提交但 revision 仍 0029 时，复用迁移 validator 续 stamp。"""

        previous_schema = "0029_structured_rewrite_constraint_results"
        _rewrite_meta_schema_version(
            workspace_service,
            workspace_id,
            schema_version=previous_schema,
        )
        db_path = _create_unversioned_product_db(
            workspace_service,
            workspace_id,
            include_operation_lease=True,
        )
        connection = sqlite3.connect(db_path)
        try:
            connection.execute(
                "CREATE TABLE alembic_version (version_num VARCHAR(32) NOT NULL)"
            )
            connection.execute(
                "INSERT INTO alembic_version(version_num) VALUES (?)",
                (previous_schema,),
            )
            connection.commit()
        finally:
            connection.close()

        result = version_service.migrate(workspace_id, workspace_id)

        assert result.from_schema_version == previous_schema
        connection = sqlite3.connect(db_path)
        try:
            revision = connection.execute(
                "SELECT version_num FROM alembic_version"
            ).fetchone()
        finally:
            connection.close()
        assert revision == (get_current_schema_version(),)
        assert workspace_service.info(workspace_id).schema_version == (
            get_current_schema_version()
        )

        restored = backup_service.restore_from_backup(
            workspace_id,
            result.pre_migration_backup_id,
        )
        assert restored.backup_id == result.pre_migration_backup_id
        connection = sqlite3.connect(db_path)
        try:
            restored_revision = connection.execute(
                "SELECT version_num FROM alembic_version"
            ).fetchone()
            lease_table = connection.execute(
                "SELECT name FROM sqlite_master "
                "WHERE type='table' AND name='chapter_operation_leases'"
            ).fetchone()
        finally:
            connection.close()
        assert restored_revision == (previous_schema,)
        assert lease_table == ("chapter_operation_leases",)
        assert workspace_service.info(workspace_id).schema_version == (previous_schema)

    def test_head_revision_with_malformed_shape_does_not_repair_meta(
        self,
        version_service: WorkspaceVersionService,
        workspace_service: WorkspaceService,
        workspace_id: str,
    ) -> None:
        """仅伪造 current revision、但 head 结构不完整时必须保留现场。"""

        previous_schema = "0029_structured_rewrite_constraint_results"
        _rewrite_meta_schema_version(
            workspace_service,
            workspace_id,
            schema_version=previous_schema,
        )
        db_path = _create_unversioned_product_db(
            workspace_service,
            workspace_id,
            include_operation_lease=True,
        )
        connection = sqlite3.connect(db_path)
        try:
            connection.execute(
                "CREATE TABLE alembic_version (version_num VARCHAR(32) NOT NULL)"
            )
            connection.execute(
                "INSERT INTO alembic_version(version_num) VALUES (?)",
                (get_current_schema_version(),),
            )
            connection.execute("DROP INDEX ix_chapter_operation_leases_task_id")
            connection.commit()
        finally:
            connection.close()

        with pytest.raises(
            WorkspaceVersionServiceError,
            match="revision 已到当前 head，但结构不匹配",
        ):
            version_service.migrate(workspace_id, workspace_id)

        assert workspace_service.info(workspace_id).schema_version == (previous_schema)


class TestMigrateUnversionedSafety:
    def test_stamped_0029_malformed_shape_is_rejected_before_backup(
        self,
        version_service: WorkspaceVersionService,
        workspace_service: WorkspaceService,
        workspace_id: str,
    ) -> None:
        previous_schema = "0029_structured_rewrite_constraint_results"
        _rewrite_meta_schema_version(
            workspace_service,
            workspace_id,
            schema_version=previous_schema,
        )
        db_path = _create_unversioned_product_db(
            workspace_service,
            workspace_id,
            include_operation_lease=False,
        )
        connection = sqlite3.connect(db_path)
        try:
            connection.execute(
                "CREATE TABLE alembic_version (version_num VARCHAR(32) NOT NULL)"
            )
            connection.execute(
                "INSERT INTO alembic_version(version_num) VALUES (?)",
                (previous_schema,),
            )
            connection.execute("DROP INDEX ix_pipeline_runs_workspace_id")
            connection.commit()
        finally:
            connection.close()

        with pytest.raises(
            WorkspaceVersionServiceError,
            match="0029 数据库升级前结构不匹配",
        ):
            version_service.migrate(workspace_id, workspace_id)

        assert not list(
            (workspace_service.workspaces_root / workspace_id / "backups").glob("*.zip")
        )

    def test_malformed_hybrid_db_is_not_stamped_or_upgraded(
        self,
        version_service: WorkspaceVersionService,
        workspace_service: WorkspaceService,
        workspace_id: str,
    ) -> None:
        _rewrite_meta_schema_version(
            workspace_service,
            workspace_id,
            schema_version="0029_structured_rewrite_constraint_results",
        )
        db_path = _create_unversioned_product_db(
            workspace_service,
            workspace_id,
            include_operation_lease=False,
        )
        engine = create_engine(f"sqlite:///{db_path}")
        with engine.begin() as connection:
            connection.exec_driver_sql(
                "CREATE TABLE chapter_operation_leases (workspace_id TEXT)"
            )
        engine.dispose()

        with pytest.raises(
            WorkspaceVersionServiceError, match="迁移前 SQLite 结构验真失败"
        ):
            version_service.migrate(workspace_id, workspace_id)

        engine = create_engine(f"sqlite:///{db_path}")
        tables = set(inspect(engine).get_table_names())
        engine.dispose()
        assert "alembic_version" not in tables
        assert workspace_service.info(workspace_id).schema_version == (
            "0029_structured_rewrite_constraint_results"
        )

    def test_missing_0029_column_is_not_stamped_or_upgraded(
        self,
        version_service: WorkspaceVersionService,
        workspace_service: WorkspaceService,
        workspace_id: str,
    ) -> None:
        _rewrite_meta_schema_version(
            workspace_service,
            workspace_id,
            schema_version="0029_structured_rewrite_constraint_results",
        )
        db_path = _create_unversioned_product_db(
            workspace_service,
            workspace_id,
            include_operation_lease=False,
        )
        engine = create_engine(f"sqlite:///{db_path}")
        with engine.begin() as connection:
            connection.exec_driver_sql(
                "ALTER TABLE chapters DROP COLUMN ready_for_next_chapter_at"
            )
        engine.dispose()

        with pytest.raises(
            WorkspaceVersionServiceError,
            match="ready_for_next_chapter_at",
        ):
            version_service.migrate(workspace_id, workspace_id)

        engine = create_engine(f"sqlite:///{db_path}")
        tables = set(inspect(engine).get_table_names())
        engine.dispose()
        assert "alembic_version" not in tables
        assert "chapter_operation_leases" not in tables

    def test_unknown_unversioned_meta_is_not_guessed(
        self,
        version_service: WorkspaceVersionService,
        workspace_service: WorkspaceService,
        workspace_id: str,
    ) -> None:
        _rewrite_meta_schema_version(
            workspace_service,
            workspace_id,
            schema_version="0028_persistent_attempt_ledger",
        )
        db_path = _create_unversioned_product_db(
            workspace_service,
            workspace_id,
            include_operation_lease=False,
        )

        with pytest.raises(
            WorkspaceVersionServiceError,
            match="不是受支持的直接前序",
        ):
            version_service.migrate(workspace_id, workspace_id)

        engine = create_engine(f"sqlite:///{db_path}")
        assert "alembic_version" not in inspect(engine).get_table_names()
        engine.dispose()


# ---------------------------------------------------------------------------
# error path:3 错误码
# ---------------------------------------------------------------------------


class TestMigrateConfirmInvalid:
    def test_confirm_mismatch_raises(
        self,
        version_service: WorkspaceVersionService,
        workspace_id: str,
    ) -> None:
        with pytest.raises(WorkspaceMigrationConfirmInvalidError) as exc_info:
            version_service.migrate(workspace_id, "wrong-confirm")
        assert exc_info.value.workspace_id == workspace_id

    def test_confirm_empty_raises(
        self,
        version_service: WorkspaceVersionService,
        workspace_id: str,
    ) -> None:
        with pytest.raises(WorkspaceMigrationConfirmInvalidError):
            version_service.migrate(workspace_id, "")


class TestMigrateVersionIncompatible:
    def test_major_version_mismatch_raises(
        self,
        version_service: WorkspaceVersionService,
        workspace_service: WorkspaceService,
        workspace_id: str,
    ) -> None:
        """workspace_engine_version 主版本号与当前 ENGINE_VERSION 跨级 →
        WorkspaceVersionIncompatibleError(v2 跨大版本拒绝自动迁移)。"""
        _rewrite_meta_schema_version(
            workspace_service,
            workspace_id,
            schema_version="0008_chapter_invalidated_force_pass",
            engine_version="2.0.0",  # 主版本号跨级
        )
        with pytest.raises(WorkspaceVersionIncompatibleError) as exc_info:
            version_service.migrate(workspace_id, workspace_id)
        assert exc_info.value.workspace_engine_version == "2.0.0"
        assert exc_info.value.current_engine_version == ENGINE_VERSION


class TestMigrateBackupFailed:
    def test_backup_failure_aborts_migration(
        self,
        workspace_service: WorkspaceService,
        ws_manager: _FakeWSManager,
        workspace_id: str,
        monkeypatch: pytest.MonkeyPatch,
    ) -> None:
        """pre-migration backup 失败时 WorkspaceMigrationBackupFailedError +
        meta.json schema_version 不变(rollback 后路)。"""

        class _BrokenBackupService:
            def create_migration_backup(
                self,
                workspace_id: str,  # noqa: ARG002
                *,
                source_schema_version: str | None,  # noqa: ARG002
                source_engine_version: str,  # noqa: ARG002
            ) -> Any:
                raise BackupServiceError("disk full simulation")

        version_service = WorkspaceVersionService(
            workspace_service=workspace_service,
            backup_service=_BrokenBackupService(),  # type: ignore[arg-type]
            ws_manager=ws_manager,
        )

        # 先把 schema_version 调成旧值,确保实际会进入 backup 步骤
        previous_schema = _prepare_supported_migration_source(
            workspace_service,
            workspace_id,
        )

        with pytest.raises(WorkspaceMigrationBackupFailedError) as exc_info:
            version_service.migrate(workspace_id, workspace_id)

        assert "disk full" in exc_info.value.reason
        # rollback:meta.json 未被修改
        meta = workspace_service.info(workspace_id)
        assert meta.schema_version == previous_schema
        # 不发 WS 事件(迁移未开始)
        assert not [e for e in ws_manager.events if e["event"] == "version_migrated"]


# ---------------------------------------------------------------------------
# rollback:alembic upgrade 抛错时 pre_migration backup 保留
# ---------------------------------------------------------------------------


class TestMigrateRollback:
    def test_alembic_failure_keeps_pre_migration_backup(
        self,
        version_service: WorkspaceVersionService,
        workspace_service: WorkspaceService,
        workspace_id: str,
        monkeypatch: pytest.MonkeyPatch,
    ) -> None:
        """模拟 alembic upgrade 抛错时:
        ① pre-migration backup zip 已落盘且完整
        ② meta.json schema_version 未被修改(还原后路)
        ③ 异常向上抛(由 service 异常 → ApiError 翻译表统一处理)
        """
        previous_schema = _prepare_supported_migration_source(
            workspace_service,
            workspace_id,
        )

        # monkeypatch _run_alembic_upgrade 抛 RuntimeError
        def _broken_upgrade(db_path: Path) -> None:  # noqa: ARG001
            raise RuntimeError("simulated alembic failure")

        monkeypatch.setattr(
            version_service,
            "_run_alembic_upgrade",
            _broken_upgrade,
        )

        with pytest.raises(RuntimeError, match="simulated alembic failure"):
            version_service.migrate(workspace_id, workspace_id)

        # pre-migration backup 仍然保留
        backups_dir = workspace_service.workspaces_root / workspace_id / "backups"
        manual_backups = list(backups_dir.glob("manual_*.zip"))
        assert len(manual_backups) >= 1

        # meta.json 未被修改
        meta = workspace_service.info(workspace_id)
        assert meta.schema_version == previous_schema

    def test_failure_after_stamp_keeps_honest_openable_source_backup(
        self,
        version_service: WorkspaceVersionService,
        workspace_service: WorkspaceService,
        workspace_id: str,
        isolate_alembic_env: None,
        monkeypatch: pytest.MonkeyPatch,
        tmp_path: Path,
    ) -> None:
        """stamp 成功、upgrade 失败后，备份仍是未 stamp 的真实 0029 快照。"""

        previous_schema = "0029_structured_rewrite_constraint_results"
        _rewrite_meta_schema_version(
            workspace_service,
            workspace_id,
            schema_version=previous_schema,
        )
        db_path = _create_unversioned_product_db(
            workspace_service,
            workspace_id,
            include_operation_lease=False,
        )

        def _broken_upgrade(*args: Any, **kwargs: Any) -> None:
            raise RuntimeError(f"upgrade failed after stamp: {args=} {kwargs=}")

        monkeypatch.setattr("alembic.command.upgrade", _broken_upgrade)

        with pytest.raises(RuntimeError, match="upgrade failed after stamp"):
            version_service.migrate(workspace_id, workspace_id)

        backup_paths = list(
            (workspace_service.workspaces_root / workspace_id / "backups").glob(
                "manual_*_migration_*.zip"
            )
        )
        assert len(backup_paths) == 1
        extract_root = tmp_path / "failed-migration-source-backup"
        with zipfile.ZipFile(backup_paths[0], "r") as zf:
            backup_meta = json.loads(zf.read("backup_meta.json").decode("utf-8"))
            assert backup_meta["purpose"] == "pre_migration"
            assert backup_meta["schema_version"] == previous_schema
            zf.extract("data/novel.db", path=extract_root)

        snapshot = sqlite3.connect(extract_root / "data" / "novel.db")
        try:
            tables = {
                str(row[0])
                for row in snapshot.execute(
                    "SELECT name FROM sqlite_master WHERE type='table'"
                )
            }
            marker = snapshot.execute(
                "SELECT total_chapters FROM workspaces WHERE workspace_id = ?",
                (workspace_id,),
            ).fetchone()
        finally:
            snapshot.close()
        assert "alembic_version" not in tables
        assert "chapter_operation_leases" not in tables
        assert marker == (7,)

        live = sqlite3.connect(db_path)
        try:
            live_revision = live.execute(
                "SELECT version_num FROM alembic_version"
            ).fetchone()
        finally:
            live.close()
        assert live_revision == (previous_schema,)
        assert workspace_service.info(workspace_id).schema_version == (previous_schema)

    def test_already_current_migrate_does_not_create_backup(
        self,
        version_service: WorkspaceVersionService,
        workspace_service: WorkspaceService,
        workspace_id: str,
    ) -> None:
        backups_dir = workspace_service.workspaces_root / workspace_id / "backups"
        before = list(backups_dir.glob("*.zip"))

        with pytest.raises(WorkspaceVersionServiceError, match="已经是当前版本"):
            version_service.migrate(workspace_id, workspace_id)

        assert list(backups_dir.glob("*.zip")) == before

    def test_post_upgrade_shape_failure_keeps_previous_meta_and_backup(
        self,
        version_service: WorkspaceVersionService,
        workspace_service: WorkspaceService,
        workspace_id: str,
        monkeypatch: pytest.MonkeyPatch,
    ) -> None:
        previous_schema = _prepare_supported_migration_source(
            workspace_service,
            workspace_id,
        )
        original_upgrade = version_service._run_alembic_upgrade

        def _upgrade_then_corrupt(db_path: Path) -> None:
            original_upgrade(db_path)
            connection = sqlite3.connect(db_path)
            try:
                connection.execute("DROP INDEX ix_chapter_operation_leases_task_id")
                connection.commit()
            finally:
                connection.close()

        monkeypatch.setattr(
            version_service,
            "_run_alembic_upgrade",
            _upgrade_then_corrupt,
        )

        with pytest.raises(
            WorkspaceVersionServiceError,
            match="迁移后 SQLite 结构验真失败",
        ):
            version_service.migrate(workspace_id, workspace_id)

        assert workspace_service.info(workspace_id).schema_version == previous_schema
        assert (
            len(
                list(
                    (workspace_service.workspaces_root / workspace_id / "backups").glob(
                        "manual_*_migration_*.zip"
                    )
                )
            )
            == 1
        )


# ---------------------------------------------------------------------------
# WS event:publish 失败 swallow + structlog
# ---------------------------------------------------------------------------


class TestWSEventSwallow:
    def test_publish_failure_does_not_break_migrate(
        self,
        workspace_service: WorkspaceService,
        backup_service: BackupService,
        workspace_id: str,
        isolate_alembic_env: None,
    ) -> None:
        """ws_manager.publish 抛错时 migrate 主流程不受影响:
        - 返回结果正常
        - meta.json 已更新
        - 异常被 swallow + 结构化日志(不向上抛)
        """
        broken_ws = _FakeWSManager(raise_exc=RuntimeError("ws broken"))
        version_service = WorkspaceVersionService(
            workspace_service=workspace_service,
            backup_service=backup_service,
            ws_manager=broken_ws,
        )

        _prepare_supported_migration_source(
            workspace_service,
            workspace_id,
        )

        # 不抛 RuntimeError("ws broken")
        result = version_service.migrate(workspace_id, workspace_id)
        assert result.to_schema_version == get_current_schema_version()

        # meta.json 已更新(WS 错误不阻塞)
        meta = workspace_service.info(workspace_id)
        assert meta.schema_version == get_current_schema_version()


# ---------------------------------------------------------------------------
# 启动 hook scan
# ---------------------------------------------------------------------------


class TestStartupScan:
    def test_scan_returns_matched_for_fresh_workspace(
        self,
        workspace_service: WorkspaceService,
        workspace_id: str,
    ) -> None:
        """新创建工作区 schema_version=current → matched 计数 1。"""
        stats = run_workspace_version_startup_scan(workspace_service)
        assert stats["scanned"] == 1
        assert stats["matched"] == 1
        assert stats["mismatched"] == 0

    def test_scan_detects_mismatched_workspace(
        self,
        workspace_service: WorkspaceService,
        workspace_id: str,
    ) -> None:
        """旧 schema_version → mismatched 计数 1。"""
        _rewrite_meta_schema_version(
            workspace_service,
            workspace_id,
            schema_version="0008_chapter_invalidated_force_pass",
        )
        stats = run_workspace_version_startup_scan(workspace_service)
        assert stats["scanned"] == 1
        assert stats["matched"] == 0
        assert stats["mismatched"] == 1

    def test_scan_empty_workspaces_root(
        self, workspace_service: WorkspaceService
    ) -> None:
        """无工作区时 scan 返 0 计数。"""
        stats = run_workspace_version_startup_scan(workspace_service)
        assert stats["scanned"] == 0
        assert stats["matched"] == 0
        assert stats["mismatched"] == 0


class TestStartupRecoveryAdmission:
    def test_startup_ignores_reserved_system_logs_directory(
        self,
        workspace_service: WorkspaceService,
        workspace_id: str,
    ) -> None:
        system_logs = workspace_service.workspaces_root / "system_logs"
        system_logs.mkdir(parents=True)
        (system_logs / "2026-08-09.jsonl").write_text(
            '{"event":"startup"}\n',
            encoding="utf-8",
        )

        assert admit_workspaces_for_startup_recovery(workspace_service) == []

    def test_startup_preserves_archive_snapshot_with_original_identity(
        self,
        workspace_service: WorkspaceService,
        workspace_id: str,
    ) -> None:
        archive = (
            workspace_service.workspaces_root
            / "_archive_ws-version-test_pre_fix_20260809"
        )
        archive.mkdir()
        source_meta = (
            workspace_service.workspaces_root / workspace_id / "meta.json"
        ).read_bytes()
        archived_meta = archive / "meta.json"
        archived_meta.write_bytes(source_meta)
        sentinel = archive / "frozen-evidence.txt"
        sentinel.write_text("preserve exactly", encoding="utf-8")

        assert admit_workspaces_for_startup_recovery(workspace_service) == []
        assert archived_meta.read_bytes() == source_meta
        assert sentinel.read_text(encoding="utf-8") == "preserve exactly"

    @pytest.mark.parametrize("failure_kind", ["corrupt", "wrong_id"])
    def test_startup_strictly_rejects_unaccounted_workspace_directory(
        self,
        workspace_service: WorkspaceService,
        workspace_id: str,
        failure_kind: str,
    ) -> None:
        root = workspace_service.workspaces_root / f"ws-bad-{failure_kind}"
        root.mkdir(parents=True)
        meta_path = root / "meta.json"
        if failure_kind == "corrupt":
            meta_path.write_text("{broken", encoding="utf-8")
        elif failure_kind == "wrong_id":
            payload = json.loads(
                (
                    workspace_service.workspaces_root / workspace_id / "meta.json"
                ).read_text(encoding="utf-8")
            )
            payload["workspace_id"] = "ws-other"
            meta_path.write_text(json.dumps(payload), encoding="utf-8")

        with pytest.raises(WorkspaceStartupRecoveryAdmissionError):
            admit_workspaces_for_startup_recovery(workspace_service)

    def test_non_workspace_evidence_directory_is_preserved_but_not_opened(
        self,
        workspace_service: WorkspaceService,
        workspace_id: str,
    ) -> None:
        evidence = workspace_service.workspaces_root / "retained-evidence"
        evidence.mkdir()
        sentinel = evidence / "sample.txt"
        sentinel.write_text("keep", encoding="utf-8")

        assert admit_workspaces_for_startup_recovery(workspace_service) == []
        assert sentinel.read_text(encoding="utf-8") == "keep"

    def test_valid_active_workspace_coexists_with_retained_no_meta_roots(
        self,
        workspace_service: WorkspaceService,
        workspace_id: str,
    ) -> None:
        (workspace_service.workspaces_root / "system_logs").mkdir()
        retained = workspace_service.workspaces_root / "r7-evidence-root"
        retained.mkdir()
        (retained / "report.json").write_text("{}", encoding="utf-8")
        (workspace_service.workspaces_root / "_active").symlink_to(workspace_id)

        assert admit_workspaces_for_startup_recovery(workspace_service) == []
        assert (retained / "report.json").is_file()

    def test_active_target_without_meta_fails_closed(
        self,
        workspace_service: WorkspaceService,
    ) -> None:
        broken = workspace_service.workspaces_root / "active-broken"
        broken.mkdir()
        (workspace_service.workspaces_root / "_active").symlink_to(broken.name)

        with pytest.raises(
            WorkspaceStartupRecoveryAdmissionError,
            match="_active.*meta",
        ):
            admit_workspaces_for_startup_recovery(workspace_service)

    def test_active_target_cannot_point_to_reserved_system_logs(
        self,
        workspace_service: WorkspaceService,
    ) -> None:
        (workspace_service.workspaces_root / "system_logs").mkdir()
        (workspace_service.workspaces_root / "_active").symlink_to("system_logs")

        with pytest.raises(
            WorkspaceStartupRecoveryAdmissionError,
            match="保留目录",
        ):
            admit_workspaces_for_startup_recovery(workspace_service)

    def test_active_target_cannot_point_to_archive_snapshot(
        self,
        workspace_service: WorkspaceService,
        workspace_id: str,
    ) -> None:
        archive = workspace_service.workspaces_root / "_archive_frozen_snapshot"
        archive.mkdir()
        archive.joinpath("meta.json").write_bytes(
            (
                workspace_service.workspaces_root / workspace_id / "meta.json"
            ).read_bytes()
        )
        (workspace_service.workspaces_root / "_active").symlink_to(archive.name)

        with pytest.raises(
            WorkspaceStartupRecoveryAdmissionError,
            match="保留目录",
        ):
            admit_workspaces_for_startup_recovery(workspace_service)

    @pytest.mark.parametrize("database_state", ["missing", "empty"])
    def test_workspace_with_product_artifact_is_not_treated_as_fresh(
        self,
        workspace_service: WorkspaceService,
        workspace_id: str,
        database_state: str,
    ) -> None:
        paths = WorkspacePaths(
            workspace_id,
            workspaces_root=workspace_service.workspaces_root,
        )
        (paths.outline_dir / "sentinel.json").write_text(
            '{"existing":true}',
            encoding="utf-8",
        )
        if database_state == "empty":
            connection = sqlite3.connect(paths.db_path)
            try:
                connection.execute("VACUUM")
            finally:
                connection.close()

        with pytest.raises(
            WorkspaceStartupRecoveryAdmissionError,
            match="产品产物",
        ):
            admit_workspaces_for_startup_recovery(workspace_service)

    def test_fresh_current_workspace_does_not_create_database(
        self,
        workspace_service: WorkspaceService,
        workspace_id: str,
    ) -> None:
        db_path = WorkspacePaths(
            workspace_id,
            workspaces_root=workspace_service.workspaces_root,
        ).db_path
        assert not db_path.exists()

        assert admit_workspaces_for_startup_recovery(workspace_service) == []

        assert not db_path.exists()

    def test_fresh_current_workspace_ignores_observability_log(
        self,
        workspace_service: WorkspaceService,
        workspace_id: str,
    ) -> None:
        paths = WorkspacePaths(
            workspace_id,
            workspaces_root=workspace_service.workspaces_root,
        )
        (paths.logs_dir / "creation.jsonl").write_text(
            '{"event":"workspace_lifecycle"}\n',
            encoding="utf-8",
        )

        assert admit_workspaces_for_startup_recovery(workspace_service) == []
        assert not paths.db_path.exists()

    def test_current_unversioned_create_all_shape_is_admitted(
        self,
        workspace_service: WorkspaceService,
        workspace_id: str,
    ) -> None:
        _create_unversioned_product_db(
            workspace_service,
            workspace_id,
            include_operation_lease=True,
        )

        assert admit_workspaces_for_startup_recovery(workspace_service) == [
            workspace_id
        ]

    def test_real_alembic_head_without_workspace_identity_is_rejected(
        self,
        workspace_service: WorkspaceService,
        workspace_id: str,
        isolate_alembic_env: None,
    ) -> None:
        db_path = WorkspacePaths(
            workspace_id,
            workspaces_root=workspace_service.workspaces_root,
        ).db_path
        (db_path.parents[1] / ".runtime_initialization_pending.json").unlink()
        WorkspaceVersionService._run_alembic_upgrade(db_path)

        before = db_path.read_bytes()
        with pytest.raises(
            WorkspaceStartupRecoveryAdmissionError,
            match="主身份",
        ):
            admit_workspaces_for_startup_recovery(workspace_service)
        assert db_path.read_bytes() == before

    def test_fresh_marker_does_not_admit_unbound_exact_current_database(
        self,
        workspace_service: WorkspaceService,
        workspace_id: str,
        isolate_alembic_env: None,
    ) -> None:
        db_path = WorkspacePaths(
            workspace_id,
            workspaces_root=workspace_service.workspaces_root,
        ).db_path
        marker = db_path.parents[1] / ".runtime_initialization_pending.json"
        assert marker.is_file()
        WorkspaceVersionService._run_alembic_upgrade(db_path)

        before = db_path.read_bytes()
        with pytest.raises(
            WorkspaceStartupRecoveryAdmissionError,
            match="主身份",
        ):
            admit_workspaces_for_startup_recovery(workspace_service)

        assert marker.is_file()
        assert db_path.read_bytes() == before

    def test_current_database_with_committed_wal_is_admitted_without_checkpoint(
        self,
        workspace_service: WorkspaceService,
        workspace_id: str,
    ) -> None:
        db_path = _create_unversioned_product_db(
            workspace_service,
            workspace_id,
            include_operation_lease=True,
        )
        live = sqlite3.connect(db_path)
        try:
            assert live.execute("PRAGMA journal_mode=WAL").fetchone() == ("wal",)
            live.execute("PRAGMA wal_autocheckpoint=0")
            live.execute(
                "UPDATE workspaces SET config_overrides = ? WHERE workspace_id = ?",
                ('{"marker":"admission-wal"}', workspace_id),
            )
            live.commit()
            wal_path = Path(f"{db_path}-wal")
            assert wal_path.is_file()
            before_wal_size = wal_path.stat().st_size
            assert before_wal_size > 0

            assert admit_workspaces_for_startup_recovery(workspace_service) == [
                workspace_id
            ]

            marker = live.execute(
                "SELECT config_overrides FROM workspaces WHERE workspace_id = ?",
                (workspace_id,),
            ).fetchone()
            assert wal_path.stat().st_size == before_wal_size
        finally:
            live.close()
        assert marker == ('{"marker":"admission-wal"}',)

    def test_migrated_real_unversioned_0029_shape_is_admitted(
        self,
        workspace_service: WorkspaceService,
        backup_service: BackupService,
        workspace_id: str,
        isolate_alembic_env: None,
    ) -> None:
        previous_schema = "0029_structured_rewrite_constraint_results"
        _rewrite_meta_schema_version(
            workspace_service,
            workspace_id,
            schema_version=previous_schema,
        )
        _create_unversioned_product_db(
            workspace_service,
            workspace_id,
            include_operation_lease=False,
        )
        service = WorkspaceVersionService(
            workspace_service=workspace_service,
            backup_service=backup_service,
        )
        service.migrate(workspace_id, workspace_id)

        assert admit_workspaces_for_startup_recovery(workspace_service) == [
            workspace_id
        ]

    def test_mismatched_workspace_db_is_not_opened_or_changed(
        self,
        workspace_service: WorkspaceService,
        workspace_id: str,
    ) -> None:
        _rewrite_meta_schema_version(
            workspace_service,
            workspace_id,
            schema_version="0029_structured_rewrite_constraint_results",
        )
        db_path = WorkspacePaths(
            workspace_id,
            workspaces_root=workspace_service.workspaces_root,
        ).db_path
        db_path.parent.mkdir(parents=True, exist_ok=True)
        sentinel = b"historical evidence; do not open as sqlite"
        db_path.write_bytes(sentinel)

        assert admit_workspaces_for_startup_recovery(workspace_service) == []

        assert db_path.read_bytes() == sentinel
        assert not Path(f"{db_path}-wal").exists()
        assert not Path(f"{db_path}-shm").exists()

    def test_current_meta_with_old_shape_fails_before_creating_lease(
        self,
        workspace_service: WorkspaceService,
        workspace_id: str,
    ) -> None:
        db_path = _create_unversioned_product_db(
            workspace_service,
            workspace_id,
            include_operation_lease=False,
        )

        with pytest.raises(WorkspaceStartupRecoveryAdmissionError):
            admit_workspaces_for_startup_recovery(workspace_service)

        engine = create_engine(f"sqlite:///{db_path}")
        assert "chapter_operation_leases" not in inspect(engine).get_table_names()
        engine.dispose()

    def test_same_columns_with_broken_lease_keys_and_indexes_are_rejected(
        self,
        workspace_service: WorkspaceService,
        workspace_id: str,
    ) -> None:
        db_path = _create_unversioned_product_db(
            workspace_service,
            workspace_id,
            include_operation_lease=True,
        )
        engine = create_engine(f"sqlite:///{db_path}")
        with engine.begin() as connection:
            connection.exec_driver_sql("DROP TABLE chapter_operation_leases")
            connection.exec_driver_sql(
                """
                CREATE TABLE chapter_operation_leases (
                    workspace_id VARCHAR NOT NULL,
                    chapter_id VARCHAR NOT NULL,
                    lease_id VARCHAR NOT NULL,
                    operation_kind VARCHAR NOT NULL,
                    source_version INTEGER NOT NULL,
                    task_id VARCHAR NOT NULL,
                    run_id VARCHAR,
                    phase VARCHAR NOT NULL,
                    created_at DATETIME NOT NULL,
                    updated_at DATETIME NOT NULL
                )
                """
            )
        engine.dispose()
        before = db_path.read_bytes()

        with pytest.raises(
            WorkspaceStartupRecoveryAdmissionError,
            match="chapter_operation_leases",
        ):
            admit_workspaces_for_startup_recovery(workspace_service)

        assert db_path.read_bytes() == before
        assert not Path(f"{db_path}-wal").exists()

    def test_foreign_key_violation_is_rejected(
        self,
        workspace_service: WorkspaceService,
        workspace_id: str,
    ) -> None:
        db_path = _create_unversioned_product_db(
            workspace_service,
            workspace_id,
            include_operation_lease=True,
        )
        connection = sqlite3.connect(db_path)
        try:
            connection.execute("PRAGMA foreign_keys = OFF")
            connection.execute(
                "INSERT INTO chapter_operation_leases "
                "(workspace_id, chapter_id, lease_id, operation_kind, "
                "source_version, task_id, run_id, phase, created_at, updated_at) "
                "VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?)",
                (
                    "missing-workspace",
                    "ch-0001",
                    "lease_fk_violation",
                    "rewrite",
                    1,
                    "task-1",
                    None,
                    "acquired",
                    "2026-08-08 00:00:00",
                    "2026-08-08 00:00:00",
                ),
            )
            connection.commit()
        finally:
            connection.close()

        with pytest.raises(
            WorkspaceStartupRecoveryAdmissionError,
            match="foreign_key_check",
        ):
            admit_workspaces_for_startup_recovery(workspace_service)

    def test_defaults_checks_integrity_and_fk_are_part_of_exact_contract(
        self,
        workspace_service: WorkspaceService,
        workspace_id: str,
    ) -> None:
        db_path = _create_unversioned_product_db(
            workspace_service,
            workspace_id,
            include_operation_lease=False,
        )
        snapshot = _read_sqlite_schema_snapshot(db_path)

        legacy_defaults = {
            table: dict(values) for table, values in snapshot.column_defaults.items()
        }
        for table_name, column_name in _LEGACY_DEFAULT_COLUMNS:
            legacy_defaults[table_name][column_name] = None
        legacy_snapshot = replace(snapshot, column_defaults=legacy_defaults)
        assert (
            _schema_shape_mismatches(
                legacy_snapshot,
                include_operation_lease=False,
                allow_legacy_create_all_defaults=True,
            )
            == []
        )
        assert any(
            mismatch.endswith(".column_defaults")
            for mismatch in _schema_shape_mismatches(
                legacy_snapshot,
                include_operation_lease=False,
            )
        )

        partial_defaults = {
            table: dict(values) for table, values in snapshot.column_defaults.items()
        }
        partial_defaults["chapter_cards"]["card_status"] = None
        partial_snapshot = replace(snapshot, column_defaults=partial_defaults)
        assert "chapter_cards.column_defaults" in _schema_shape_mismatches(
            partial_snapshot,
            include_operation_lease=False,
            allow_legacy_create_all_defaults=True,
        )

        defaults = {
            table: dict(values) for table, values in snapshot.column_defaults.items()
        }
        defaults["pipeline_runs"]["provider_call_cap"] = "999"
        default_bad = replace(snapshot, column_defaults=defaults)
        assert "pipeline_runs.column_defaults" in _schema_shape_mismatches(
            default_bad,
            include_operation_lease=False,
        )

        checks = dict(snapshot.check_constraints)
        checks["pipeline_runs"] = frozenset()
        check_bad = replace(snapshot, check_constraints=checks)
        assert "pipeline_runs.check_constraints" in _schema_shape_mismatches(
            check_bad,
            include_operation_lease=False,
        )

        indexes = dict(snapshot.explicit_indexes)
        indexes["core_state_revisions"] = frozenset(
            (columns, unique, False)
            for columns, unique, _partial in indexes["core_state_revisions"]
        )
        partial_index_bad = replace(snapshot, explicit_indexes=indexes)
        assert "core_state_revisions.explicit_indexes" in _schema_shape_mismatches(
            partial_index_bad,
            include_operation_lease=False,
        )

        integrity_bad = replace(snapshot, integrity_check=("corrupt",))
        assert any(
            mismatch.startswith("integrity_check=")
            for mismatch in _schema_shape_mismatches(
                integrity_bad,
                include_operation_lease=False,
            )
        )

        foreign_key_bad = replace(
            snapshot,
            foreign_key_violations=(("child", 1, "parent", 0),),
        )
        assert any(
            mismatch.startswith("foreign_key_check=")
            for mismatch in _schema_shape_mismatches(
                foreign_key_bad,
                include_operation_lease=False,
            )
        )
