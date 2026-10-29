"""WorkspaceService 测试(P1-B-14)。

四组用例:
  - TestLifecycle:happy path 端到端跑一遍 9 个动作的串联
  - TestCreate:create 边界(重复 create、骨架完整、activate=False 解耦)
  - TestArchiveRestore:archive/restore 边界 + status 切换 + restore 后 info 验证
  - TestPurge:confirm 强制二次确认、不动顶级备份
"""

from __future__ import annotations

import os
import sqlite3
import threading
from pathlib import Path

import pytest

from src.core.workspace.workspace_paths import WorkspacePaths
from src.services.workspace_service import (
    WorkspaceDestroyConfirmInvalidError,
    WorkspaceService,
    WorkspaceServiceError,
)
from src.services.memory_enrichment_worker_guard import (
    serialize_memory_enrichment_worker,
)
from src.services.workspace_operation_guard import workspace_operation_lock


@pytest.fixture
def service(tmp_path: Path) -> WorkspaceService:
    """每个测试给一个隔离的 ``workspaces/`` + ``archives/``。"""
    workspaces_root = tmp_path / "workspaces"
    archives_root = tmp_path / "archives"
    workspaces_root.mkdir()
    archives_root.mkdir()
    return WorkspaceService(
        workspaces_root=workspaces_root, archives_root=archives_root
    )


def _create_payload(**overrides):
    base = dict(
        display_name="科幻测试本",
        genre="scifi",
        premise="人类在月球建殖民地",
        total_chapters=120,
    )
    base.update(overrides)
    return base


class TestLifecycle:
    """端到端跑一遍完整生命周期,验证各动作之间不互踩。"""

    def test_create_switch_info_list_backup_archive_restore_purge(
        self, service: WorkspaceService, tmp_path: Path
    ) -> None:
        # 1) create + 自动激活
        ws = service.create("scifi_book_1", **_create_payload())
        assert ws.workspace_id == "scifi_book_1"
        assert ws.status == "active"
        active_link = tmp_path / "workspaces" / "_active"
        assert active_link.is_symlink()
        assert os.readlink(active_link) == "scifi_book_1"

        # 2) 再 create 一个,switch 切过去
        service.create("xianxia_book_1", **_create_payload(genre="xianxia"))
        service.switch("scifi_book_1")
        assert os.readlink(tmp_path / "workspaces" / "_active") == "scifi_book_1"

        # 3) info 拿到 meta
        info = service.info("scifi_book_1")
        assert info.display_name == "科幻测试本"

        # 4) list 列出两个工作区(忽略 _active 软链)
        listed = service.list()
        assert sorted(w.workspace_id for w in listed) == [
            "scifi_book_1",
            "xianxia_book_1",
        ]

        # 5) backup 不删原目录
        backup_file = service.backup("scifi_book_1")
        assert backup_file.is_file()
        assert (tmp_path / "workspaces" / "scifi_book_1").is_dir()

        # 6) archive 删原目录,落项目级 archives/
        archive_file = service.archive("scifi_book_1")
        assert archive_file == tmp_path / "archives" / "scifi_book_1.tar.gz"
        assert archive_file.is_file()
        assert not (tmp_path / "workspaces" / "scifi_book_1").is_dir()

        # 7) restore 回来
        restored = service.restore("scifi_book_1")
        assert restored.status == "active"
        assert (tmp_path / "workspaces" / "scifi_book_1" / "meta.json").is_file()

        # 8) purge 走二次确认
        service.purge("scifi_book_1", confirm=True)
        assert not (tmp_path / "workspaces" / "scifi_book_1").is_dir()
        # 顶级 archives 不动
        assert archive_file.is_file()


class TestCreate:
    def test_creates_full_directory_skeleton(
        self, service: WorkspaceService, tmp_path: Path
    ) -> None:
        service.create("ws1", **_create_payload())
        paths = WorkspacePaths("ws1", workspaces_root=tmp_path / "workspaces")
        for d in (
            paths.root,
            paths.prompts_dir,
            paths.data_dir,
            paths.chapters_dir,
            paths.cards_dir,
            paths.outline_dir,
            paths.exports_dir,
            paths.archives_dir,
            paths.backups_dir,
            paths.logs_dir,
            paths.chroma_dir,  # v1.0.x wave 5 s1:含 P2+ 引入的 chroma/
        ):
            assert d.is_dir(), f"{d} 应在 create 后存在"
        assert paths.meta_json.is_file()

    def test_creates_chroma_dir_for_new_workspace(
        self, service: WorkspaceService, tmp_path: Path
    ) -> None:
        """v1.0.x wave 5 session 1:create 时必建 ``chroma/`` 子目录。

        覆盖根因:原 ``_create_skeleton`` 注释"P1 范围内目录"在 P2 引入
        Chroma 后未同步,导致新工作区不建 ``data/chroma/`` → 健康检查
        ``workspace_chroma_readwrite`` 探针对 ``not exists`` 直接 fail
        (luo2 等"跑过 outline + cards 但未跑正文/MemoryExtractor"工作区
        在向量库懒创建前命中)。
        """
        service.create("ws-chroma", **_create_payload())
        paths = WorkspacePaths("ws-chroma", workspaces_root=tmp_path / "workspaces")
        assert paths.chroma_dir.is_dir()
        # 新工作区 chroma/ 应为空(尚无向量写入)
        assert list(paths.chroma_dir.iterdir()) == []

    def test_meta_json_has_engine_version_and_active_status(
        self, service: WorkspaceService
    ) -> None:
        ws = service.create("ws1", **_create_payload())
        from src.version import ENGINE_VERSION

        assert ws.engine_version == ENGINE_VERSION
        assert ws.status == "active"
        assert ws.completed_chapters == 0

    def test_create_default_voice_defaults_third_person(
        self, service: WorkspaceService
    ) -> None:
        """L2(wave 3 s4):不传 default_voice → 项目级硬约束默认 third_person。"""
        ws = service.create("ws1", **_create_payload())
        assert ws.default_voice == "third_person"

    def test_create_default_voice_first_person_persists(
        self, service: WorkspaceService
    ) -> None:
        """L2:显式传 first_person → 落 meta.json(G10 兜底据此锁第一人称)。"""
        ws = service.create("ws1", **_create_payload(), default_voice="first_person")
        assert ws.default_voice == "first_person"
        # 落盘后回读 meta.json 一致(ORM upsert 链下游读此值)
        reread = service.info("ws1")
        assert reread.default_voice == "first_person"

    def test_create_default_voice_free_persists(
        self, service: WorkspaceService
    ) -> None:
        """L2:free → 关闭 G10 跨章节视角硬约束(只 warn);落盘可回读。"""
        ws = service.create("ws1", **_create_payload(), default_voice="free")
        assert ws.default_voice == "free"

    def test_duplicate_create_raises(self, service: WorkspaceService) -> None:
        service.create("ws1", **_create_payload())
        with pytest.raises(WorkspaceServiceError, match="工作区已存在"):
            service.create("ws1", **_create_payload())

    def test_activate_false_skips_symlink(
        self, service: WorkspaceService, tmp_path: Path
    ) -> None:
        service.create("ws1", **_create_payload(), activate=False)
        active = tmp_path / "workspaces" / "_active"
        assert not active.exists() and not active.is_symlink()

    def test_create_with_activate_true_overwrites_existing_active_link(
        self, service: WorkspaceService, tmp_path: Path
    ) -> None:
        service.create("ws1", **_create_payload())
        service.create("ws2", **_create_payload())
        # 第二次 create 的自动 switch 应当把 _active 指向 ws2
        active = tmp_path / "workspaces" / "_active"
        assert os.readlink(active) == "ws2"


class TestList:
    def test_empty_workspaces_root(self, tmp_path: Path) -> None:
        # 不预创建 workspaces/ 也不抛错
        empty_root = tmp_path / "nonexistent_workspaces"
        service = WorkspaceService(
            workspaces_root=empty_root, archives_root=tmp_path / "archives"
        )
        assert service.list() == []

    def test_skips_dir_without_meta_json(
        self, service: WorkspaceService, tmp_path: Path
    ) -> None:
        service.create("ws1", **_create_payload())
        # 手建一个无 meta 的目录,不应被算成工作区
        (tmp_path / "workspaces" / "stray_dir").mkdir()
        listed = service.list()
        assert [w.workspace_id for w in listed] == ["ws1"]

    def test_active_symlink_does_not_double_count(
        self, service: WorkspaceService
    ) -> None:
        service.create("ws1", **_create_payload())
        # _active → ws1,list 应只返回 ws1 一次(软链跳过、真目录扫到)
        listed = service.list()
        assert len(listed) == 1
        assert listed[0].workspace_id == "ws1"


class TestSwitch:
    def test_switch_updates_last_active_at(self, service: WorkspaceService) -> None:
        ws_initial = service.create("ws1", **_create_payload(), activate=False)
        ws_switched = service.switch("ws1")
        assert ws_switched.last_active_at >= ws_initial.last_active_at

    def test_switch_waits_for_workspace_maintenance_lock(
        self,
        service: WorkspaceService,
        tmp_path: Path,
    ) -> None:
        workspace_id = "ws-switch-serialized"
        service.create(workspace_id, **_create_payload(), activate=False)
        call_started = threading.Event()
        call_done = threading.Event()
        failures: list[BaseException] = []

        def _switch() -> None:
            call_started.set()
            try:
                service.switch(workspace_id)
            except BaseException as exc:  # noqa: BLE001
                failures.append(exc)
            finally:
                call_done.set()

        with workspace_operation_lock(service.workspaces_root, workspace_id):
            worker = threading.Thread(target=_switch)
            worker.start()
            assert call_started.wait(timeout=5)
            assert not call_done.wait(timeout=0.2)
            assert not (tmp_path / "workspaces" / "_active").exists()

        worker.join(timeout=5)
        assert not worker.is_alive()
        assert failures == []
        assert os.readlink(tmp_path / "workspaces" / "_active") == workspace_id

    def test_switch_missing_workspace_raises(self, service: WorkspaceService) -> None:
        with pytest.raises(WorkspaceServiceError, match="工作区不存在"):
            service.switch("ghost")


class TestArchiveRestore:
    def test_archive_creates_tarball_and_removes_root(
        self, service: WorkspaceService, tmp_path: Path
    ) -> None:
        service.create("ws1", **_create_payload())
        archive_file = service.archive("ws1")
        assert archive_file.is_file()
        assert not (tmp_path / "workspaces" / "ws1").is_dir()

    def test_archive_already_exists_raises(self, service: WorkspaceService) -> None:
        service.create("ws1", **_create_payload())
        service.archive("ws1")
        # 再 create 一次同名(原目录不在了,可创建)
        service.create("ws1", **_create_payload())
        # 但归档已存在,第二次 archive 应抛错
        with pytest.raises(WorkspaceServiceError, match="归档文件已存在"):
            service.archive("ws1")

    def test_restore_missing_archive_raises(self, service: WorkspaceService) -> None:
        with pytest.raises(WorkspaceServiceError, match="归档文件不存在"):
            service.restore("ghost")

    def test_restore_when_root_exists_raises(self, service: WorkspaceService) -> None:
        service.create("ws1", **_create_payload())
        service.archive("ws1")
        service.create("ws1", **_create_payload())
        # 现在工作区目录与归档同时存在,restore 应拒绝
        with pytest.raises(WorkspaceServiceError, match="工作区目录已存在"):
            service.restore("ws1")

    def test_restore_resets_status_and_info_works(
        self, service: WorkspaceService, tmp_path: Path
    ) -> None:
        """archive 把 status 切到 archived;restore 应切回 active 且 info 可读。"""
        ws_before = service.create("ws1", **_create_payload())
        service.archive("ws1")
        restored = service.restore("ws1")

        # restore 返回值:status=active, last_active_at 比 archive 前更新
        assert restored.status == "active"
        assert restored.last_active_at >= ws_before.last_active_at

        # info() 能正常读出 meta(防 restore 漏改 meta 落盘)
        info = service.info("ws1")
        assert info.status == "active"
        assert info.workspace_id == "ws1"
        assert info.display_name == "科幻测试本"


class TestClean:
    def test_clean_removes_runtime_dirs_keeps_user_data(
        self, service: WorkspaceService, tmp_path: Path
    ) -> None:
        paths = WorkspacePaths("ws1", workspaces_root=tmp_path / "workspaces")
        service.create("ws1", **_create_payload())

        # 模拟运行时产物
        (paths.chroma_dir).mkdir(parents=True, exist_ok=True)
        (paths.chroma_dir / "vectors.bin").write_text("x")
        (paths.archives_dir / "prompt_001.json").write_text("{}")
        (paths.logs_dir / "run.log").write_text("hello")

        # 模拟用户数据(应保留)
        (paths.chapters_dir / "v1c001").mkdir(parents=True)
        (paths.chapters_dir / "v1c001" / "v1.md").write_text("正文")
        (paths.cards_dir / "v1c001.json").write_text("{}")
        connection = sqlite3.connect(paths.db_path)
        try:
            connection.execute("CREATE TABLE preserved (value TEXT)")
            connection.commit()
        finally:
            connection.close()

        service.clean("ws1")

        # 运行时数据被清(目录重建为空)
        assert paths.chroma_dir.is_dir()
        assert list(paths.chroma_dir.iterdir()) == []
        assert list(paths.archives_dir.iterdir()) == []
        assert list(paths.logs_dir.iterdir()) == []

        # 用户数据保留
        assert (paths.chapters_dir / "v1c001" / "v1.md").read_text() == "正文"
        assert (paths.cards_dir / "v1c001.json").is_file()
        assert paths.db_path.is_file()

    def test_clean_missing_workspace_raises(self, service: WorkspaceService) -> None:
        with pytest.raises(WorkspaceServiceError, match="工作区不存在"):
            service.clean("ghost")


@pytest.mark.parametrize(
    "operation",
    ["clean", "archive", "purge", "destroy", "restore"],
)
def test_destructive_workspace_operations_wait_for_memory_worker(
    service: WorkspaceService,
    tmp_path: Path,
    operation: str,
) -> None:
    workspace_id = "ws-memory-race"
    paths = WorkspacePaths(
        workspace_id,
        workspaces_root=tmp_path / "workspaces",
    )
    service.create(workspace_id, **_create_payload(), activate=False)
    chroma_sentinel = paths.chroma_dir / "worker-sentinel.bin"
    chroma_sentinel.parent.mkdir(parents=True, exist_ok=True)
    chroma_sentinel.write_bytes(b"worker owns this")
    if operation == "restore":
        service.archive(workspace_id)

    worker_holds_lock = threading.Event()
    release_worker = threading.Event()
    operation_started = threading.Event()
    operation_done = threading.Event()
    failures: list[BaseException] = []

    def _memory_worker() -> None:
        with serialize_memory_enrichment_worker(
            service.workspaces_root,
            workspace_id,
        ):
            worker_holds_lock.set()
            assert release_worker.wait(timeout=5)

    def _destructive_operation() -> None:
        operation_started.set()
        try:
            if operation == "clean":
                service.clean(workspace_id)
            elif operation == "archive":
                service.archive(workspace_id)
            elif operation == "purge":
                service.purge(workspace_id, confirm=True)
            elif operation == "destroy":
                service.destroy(workspace_id, confirm=workspace_id)
            else:
                service.restore(workspace_id)
        except BaseException as exc:  # noqa: BLE001 - thread evidence relay
            failures.append(exc)
        finally:
            operation_done.set()

    worker = threading.Thread(target=_memory_worker)
    worker.start()
    assert worker_holds_lock.wait(timeout=5)
    operation_thread = threading.Thread(target=_destructive_operation)
    operation_thread.start()
    assert operation_started.wait(timeout=5)
    assert not operation_done.wait(timeout=0.2)
    if operation == "restore":
        assert not paths.root.exists()
    else:
        assert chroma_sentinel.read_bytes() == b"worker owns this"

    release_worker.set()
    worker.join(timeout=5)
    operation_thread.join(timeout=5)
    assert not worker.is_alive()
    assert not operation_thread.is_alive()
    assert failures == []
    if operation in {"archive", "purge", "destroy"}:
        assert not paths.root.exists()
    else:
        assert paths.root.exists()
    if operation == "clean":
        assert not chroma_sentinel.exists()
    if operation == "restore":
        assert chroma_sentinel.read_bytes() == b"worker owns this"


@pytest.mark.parametrize(
    "operation",
    ["clean", "archive", "purge", "destroy", "restore"],
)
def test_destructive_workspace_operations_reject_active_normal_task_before_delete(
    service: WorkspaceService,
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    operation: str,
) -> None:
    workspace_id = "ws-active-task"
    paths = WorkspacePaths(
        workspace_id,
        workspaces_root=tmp_path / "workspaces",
    )
    service.create(workspace_id, **_create_payload(), activate=False)
    sentinel = paths.root / "must-survive.txt"
    sentinel.write_text("preserved", encoding="utf-8")
    if operation == "restore":
        service.archive(workspace_id)
    monkeypatch.setattr(
        "src.services.workspace_service.task_registry.has_active_tasks",
        lambda _workspace_id: True,
    )

    with pytest.raises(WorkspaceServiceError, match="active_task"):
        if operation == "clean":
            service.clean(workspace_id)
        elif operation == "archive":
            service.archive(workspace_id)
        elif operation == "purge":
            service.purge(workspace_id, confirm=True)
        elif operation == "destroy":
            service.destroy(workspace_id, confirm=workspace_id)
        else:
            service.restore(workspace_id)

    if operation == "restore":
        assert not paths.root.exists()
        assert service.archive_file_for(workspace_id).is_file()
    else:
        assert sentinel.read_text(encoding="utf-8") == "preserved"


@pytest.mark.parametrize(
    "operation",
    ["clean", "archive", "purge", "destroy"],
)
def test_destructive_workspace_operations_reject_chapter_lease_before_delete(
    service: WorkspaceService,
    tmp_path: Path,
    operation: str,
) -> None:
    workspace_id = "ws-active-lease"
    paths = WorkspacePaths(
        workspace_id,
        workspaces_root=tmp_path / "workspaces",
    )
    service.create(workspace_id, **_create_payload(), activate=False)
    sentinel = paths.root / "must-survive.txt"
    sentinel.write_text("preserved", encoding="utf-8")
    connection = sqlite3.connect(paths.db_path)
    try:
        connection.execute("CREATE TABLE chapter_operation_leases (lease_id TEXT)")
        connection.execute(
            "INSERT INTO chapter_operation_leases(lease_id) VALUES (?)",
            ("lease-live",),
        )
        connection.commit()
    finally:
        connection.close()

    with pytest.raises(WorkspaceServiceError, match="lease-live"):
        if operation == "clean":
            service.clean(workspace_id)
        elif operation == "archive":
            service.archive(workspace_id)
        elif operation == "purge":
            service.purge(workspace_id, confirm=True)
        else:
            service.destroy(workspace_id, confirm=workspace_id)

    assert sentinel.read_text(encoding="utf-8") == "preserved"


class TestBackup:
    def test_backup_creates_tarball_in_workspace_backups_dir(
        self, service: WorkspaceService, tmp_path: Path
    ) -> None:
        service.create("ws1", **_create_payload())
        backup_file = service.backup("ws1")
        # 在工作区内 backups/ 下,文件名形如 ws1-YYYYMMDDTHHMMSSZ.tar.gz
        assert backup_file.parent == tmp_path / "workspaces" / "ws1" / "backups"
        assert backup_file.name.startswith("ws1-")
        assert backup_file.name.endswith(".tar.gz")
        # 时间戳紧凑,不含 ":" / "+"(跨平台兼容)
        assert ":" not in backup_file.name
        assert "+" not in backup_file.name
        # 原目录还在
        assert (tmp_path / "workspaces" / "ws1").is_dir()

    def test_backup_missing_workspace_raises(self, service: WorkspaceService) -> None:
        with pytest.raises(WorkspaceServiceError, match="工作区不存在"):
            service.backup("ghost")


class TestPurge:
    def test_purge_without_confirm_raises(self, service: WorkspaceService) -> None:
        service.create("ws1", **_create_payload())
        with pytest.raises(WorkspaceServiceError, match="confirm=True"):
            service.purge("ws1")

    def test_purge_with_confirm_removes_root_keeps_archive(
        self, service: WorkspaceService, tmp_path: Path
    ) -> None:
        service.create("ws1", **_create_payload())
        service.archive("ws1")
        service.restore("ws1")
        service.purge("ws1", confirm=True)
        assert not (tmp_path / "workspaces" / "ws1").is_dir()
        # 顶级 archives/ 备份保留
        assert (tmp_path / "archives" / "ws1.tar.gz").is_file()

    def test_purge_missing_workspace_raises(self, service: WorkspaceService) -> None:
        with pytest.raises(WorkspaceServiceError, match="工作区不存在"):
            service.purge("ghost", confirm=True)


class _RecordingWsManager:
    """轻量 ws_manager stub:把 publish 调用记到 ``events`` 列表用于断言。"""

    def __init__(self) -> None:
        self.events: list[dict[str, object]] = []

    def publish(self, channel: str, event: str, data: dict[str, object]) -> None:
        self.events.append({"channel": channel, "event": event, "data": data})


class TestDestroy:
    """P7-B-07:WorkspaceService.destroy 三方全删 + strict confirm 二次确认。"""

    def test_destroy_happy_three_targets(
        self, service: WorkspaceService, tmp_path: Path
    ) -> None:
        """工作区目录 + 顶级 archive 都存在 → 两项全删。"""
        service.create("ws1", **_create_payload())
        # 先 archive 再 restore,留下顶级 archive + workspace 目录双双在场
        service.archive("ws1")
        service.restore("ws1")
        archive_file = tmp_path / "archives" / "ws1.tar.gz"
        workspace_dir = tmp_path / "workspaces" / "ws1"
        assert workspace_dir.is_dir()
        assert archive_file.is_file()

        result = service.destroy("ws1", confirm="ws1")

        assert result.workspace_id == "ws1"
        assert len(result.deleted_paths) == 2
        assert str(workspace_dir) in result.deleted_paths
        assert str(archive_file) in result.deleted_paths
        assert not workspace_dir.is_dir()
        assert not archive_file.is_file()

    def test_destroy_workspace_only_no_archive(
        self, service: WorkspaceService, tmp_path: Path
    ) -> None:
        """只有工作区目录,无顶级 archive → 只删工作区,deleted_paths 1 项。"""
        service.create("ws1", **_create_payload())
        workspace_dir = tmp_path / "workspaces" / "ws1"
        archive_file = tmp_path / "archives" / "ws1.tar.gz"
        assert workspace_dir.is_dir()
        assert not archive_file.is_file()

        result = service.destroy("ws1", confirm="ws1")

        assert result.workspace_id == "ws1"
        assert len(result.deleted_paths) == 1
        assert str(workspace_dir) in result.deleted_paths
        assert not workspace_dir.is_dir()

    def test_destroy_confirm_mismatch_no_filesystem_change(
        self, service: WorkspaceService, tmp_path: Path
    ) -> None:
        """confirm ≠ workspace_id → 抛 WorkspaceDestroyConfirmInvalidError 且文件系统未改动。"""
        service.create("ws1", **_create_payload())
        workspace_dir = tmp_path / "workspaces" / "ws1"

        with pytest.raises(WorkspaceDestroyConfirmInvalidError) as excinfo:
            service.destroy("ws1", confirm="wrong-id")

        assert excinfo.value.workspace_id == "ws1"
        assert excinfo.value.got == "wrong-id"
        assert workspace_dir.is_dir()  # 文件系统未改动

    def test_destroy_missing_workspace_raises_not_found(
        self, service: WorkspaceService, tmp_path: Path
    ) -> None:
        """工作区目录不存在 → 抛 WorkspaceServiceError("工作区不存在"),即使顶级 archive 存在。"""
        # 制造场景:archive 后不 restore 留 ghost archive 但无工作区目录
        service.create("ws1", **_create_payload())
        service.archive("ws1")
        archive_file = tmp_path / "archives" / "ws1.tar.gz"
        assert archive_file.is_file()
        assert not (tmp_path / "workspaces" / "ws1").is_dir()

        with pytest.raises(WorkspaceServiceError, match="工作区不存在"):
            service.destroy("ws1", confirm="ws1")

        # 顶级 archive 也不被动(destroy SSOT 是工作区目录,不存在直接拒绝整动作)
        assert archive_file.is_file()

    def test_destroy_publishes_workspace_destroyed_event(self, tmp_path: Path) -> None:
        """destroy 成功后 ws_manager 收到 workspace_destroyed event,data 含 deleted_paths。"""
        workspaces_root = tmp_path / "workspaces"
        archives_root = tmp_path / "archives"
        workspaces_root.mkdir()
        archives_root.mkdir()
        ws_manager = _RecordingWsManager()
        svc = WorkspaceService(
            workspaces_root=workspaces_root,
            archives_root=archives_root,
            ws_manager=ws_manager,
        )
        svc.create("ws1", **_create_payload())

        result = svc.destroy("ws1", confirm="ws1")

        assert len(ws_manager.events) == 1
        event = ws_manager.events[0]
        assert event["event"] == "workspace_destroyed"
        assert event["channel"] == "workspace.ws1.events"
        data = event["data"]
        assert isinstance(data, dict)
        assert data["workspace_id"] == "ws1"
        assert data["deleted_paths"] == result.deleted_paths
        assert "deleted_at" in data  # ISO 8601 timestamp

    def test_destroy_ws_manager_none_does_not_raise(
        self, service: WorkspaceService
    ) -> None:
        """ws_manager=None 时 publish noop,不影响 destroy 主流程(CLI 路径)。"""
        service.create("ws1", **_create_payload())
        # service fixture 默认 ws_manager=None,destroy 应正常完成
        result = service.destroy("ws1", confirm="ws1")
        assert result.workspace_id == "ws1"

    def test_destroy_workspace_dir_fully_gone_no_logs_residue(
        self, service: WorkspaceService, tmp_path: Path
    ) -> None:
        """B-02 hot-fix:旧实现 log 在 rmtree 之后,per-workspace JSONL handler
        会重建 ``workspaces/<id>/logs/`` 目录留 jsonl 行,导致工作区目录残留
        → 重提同 ID 撞 409。修后日志在 rmtree 之前打,目录彻底消失。
        """
        # 装上 per-workspace logger handler 模拟生产环境
        import logging as _stdlib_logging

        from src.api.logging import WorkspaceAwareJSONLHandler

        handler = WorkspaceAwareJSONLHandler(data_dir=tmp_path / "workspaces")
        root_logger = _stdlib_logging.getLogger()
        root_logger.addHandler(handler)
        try:
            service.create("ws-fully-gone", **_create_payload())
            workspace_dir = tmp_path / "workspaces" / "ws-fully-gone"
            assert workspace_dir.is_dir()

            service.destroy("ws-fully-gone", confirm="ws-fully-gone")

            # 工作区目录必须彻底消失 — 不能残留 logs/ 子目录
            assert not workspace_dir.exists(), (
                f"destroy 后工作区目录残留: {list(workspace_dir.rglob('*')) if workspace_dir.is_dir() else workspace_dir}"
            )
        finally:
            root_logger.removeHandler(handler)

    def test_destroy_clears_dangling_active_link(
        self, service: WorkspaceService, tmp_path: Path
    ) -> None:
        """B-02 hot-fix:destroy active workspace 后 _active 软链同步清掉,
        避免后续 list/info 撞 dangling target。"""
        service.create("ws-active", **_create_payload(), activate=True)
        active_link = tmp_path / "workspaces" / "_active"
        assert active_link.is_symlink()
        assert os.readlink(active_link) == "ws-active"

        service.destroy("ws-active", confirm="ws-active")

        assert not active_link.exists()
        assert not active_link.is_symlink()

    def test_destroy_preserves_other_workspace_active_link(
        self, service: WorkspaceService, tmp_path: Path
    ) -> None:
        """destroy 工作区 A 时,若 _active 指向工作区 B,不清 B 的软链。"""
        service.create("ws-keep", **_create_payload(), activate=True)
        service.create("ws-destroy", **_create_payload(), activate=False)
        active_link = tmp_path / "workspaces" / "_active"
        assert os.readlink(active_link) == "ws-keep"

        service.destroy("ws-destroy", confirm="ws-destroy")

        assert active_link.is_symlink()
        assert os.readlink(active_link) == "ws-keep"

    def test_destroy_publish_failure_swallowed(self, tmp_path: Path) -> None:
        """ws_manager.publish 抛错时 destroy 主流程仍成功(swallow + log)。"""

        class _BoomWsManager:
            def publish(
                self, channel: str, event: str, data: dict[str, object]
            ) -> None:
                raise RuntimeError("simulated publish failure")

        workspaces_root = tmp_path / "workspaces"
        archives_root = tmp_path / "archives"
        workspaces_root.mkdir()
        archives_root.mkdir()
        svc = WorkspaceService(
            workspaces_root=workspaces_root,
            archives_root=archives_root,
            ws_manager=_BoomWsManager(),
        )
        svc.create("ws1", **_create_payload())

        # publish 抛错被 swallow,destroy 仍返回结果 + 文件已删
        result = svc.destroy("ws1", confirm="ws1")
        assert result.workspace_id == "ws1"
        assert not (workspaces_root / "ws1").is_dir()


# ---------------------------------------------------------------------------
# v1.0.x A2-03(2026-05-19 wave 2):sync_completed_chapters_to_meta helper
# ---------------------------------------------------------------------------


class TestSyncCompletedChaptersToMeta:
    """v1.0.x A2-03:把 ORM 改动同步落 meta.json。

    覆盖:
    - delta=+1 → meta.json completed_chapters +1
    - delta=-1 → -1
    - delta=0 → noop
    - 多次累加幂等
    - floor 0(不下溢)
    - meta.json 不存在 → swallow,不抛
    """

    def test_delta_plus_one(self, service: WorkspaceService) -> None:
        service.create("ws1", **_create_payload())
        service.sync_completed_chapters_to_meta("ws1", delta=1)
        assert service.info("ws1").completed_chapters == 1

    def test_delta_minus_one(self, service: WorkspaceService) -> None:
        service.create("ws1", **_create_payload())
        service.sync_completed_chapters_to_meta("ws1", delta=3)
        service.sync_completed_chapters_to_meta("ws1", delta=-1)
        assert service.info("ws1").completed_chapters == 2

    def test_delta_zero_is_noop(self, service: WorkspaceService) -> None:
        service.create("ws1", **_create_payload())
        service.sync_completed_chapters_to_meta("ws1", delta=0)
        assert service.info("ws1").completed_chapters == 0

    def test_accumulates_correctly(self, service: WorkspaceService) -> None:
        service.create("ws1", **_create_payload())
        for _ in range(5):
            service.sync_completed_chapters_to_meta("ws1", delta=1)
        service.sync_completed_chapters_to_meta("ws1", delta=-2)
        assert service.info("ws1").completed_chapters == 3

    def test_floor_zero_no_underflow(self, service: WorkspaceService) -> None:
        service.create("ws1", **_create_payload())
        # 已是 0,再 -5 不下溢
        service.sync_completed_chapters_to_meta("ws1", delta=-5)
        assert service.info("ws1").completed_chapters == 0

    def test_missing_meta_swallows_silently(
        self, service: WorkspaceService, tmp_path: Path
    ) -> None:
        # 不 create,直接调 sync — meta.json 不存在,不应抛错
        service.sync_completed_chapters_to_meta("ws-nonexistent", delta=1)
