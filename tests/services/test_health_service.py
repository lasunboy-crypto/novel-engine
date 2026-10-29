"""``run_health_checks`` 单测(P4-acceptance 步 ③)。

覆盖 6 项最简健康检查的结构契约 + 关键字段 + 总判定逻辑(ok / degraded /
down)。具体单项的 ``outcome`` 依赖 runtime 环境(磁盘 / ollama / config),
本测试只验"结构 + 关键字段 + 总判定"路径,不验单项的具体 pass/fail。
"""

from __future__ import annotations

from src.services.health_service import run_health_checks


class TestRunHealthChecks:
    def test_returns_dict_with_status_engine_version_checks_summary(
        self,
    ) -> None:
        report = run_health_checks()
        assert "status" in report
        assert report["status"] in {"ok", "degraded", "down"}
        assert isinstance(report["engine_version"], str)
        assert isinstance(report["checks"], list)
        assert len(report["checks"]) == 6
        assert "summary" in report
        summary = report["summary"]
        assert summary["pass_count"] + summary["fail_count"] + summary[
            "skip_count"
        ] == 6

    def test_each_check_has_name_outcome_optional_detail(self) -> None:
        report = run_health_checks()
        seen_names = set()
        for c in report["checks"]:
            assert "name" in c
            assert c["outcome"] in {"pass", "fail", "skip"}
            assert isinstance(c.get("detail"), (str, type(None)))
            seen_names.add(c["name"])
        # 6 项命名固定
        expected_names = {
            "config_yaml_loaded",
            "data_dir_readable",
            "logs_dir_writable",
            "ollama_endpoint_reachable",
            "disk_space_above_1gb",
            "engine_version_present",
        }
        assert seen_names == expected_names

    def test_critical_failure_triggers_status_down(
        self, monkeypatch, tmp_path
    ) -> None:
        """``data_dir_readable`` 是关键项,fail 时 status=down。"""
        # monkeypatch settings.data_dir 指向不存在路径
        from src import config as cfg_module

        bogus = tmp_path / "no-such-dir"
        monkeypatch.setattr(cfg_module.settings, "data_dir", bogus)
        report = run_health_checks()
        names_fail = {
            c["name"] for c in report["checks"] if c["outcome"] == "fail"
        }
        assert "data_dir_readable" in names_fail
        assert report["status"] == "down"


class TestRunHealthChecksWithWorkspace:
    """P4-acceptance-followup:``workspace_id`` 给定时附带 5 项工作区检查。"""

    def test_workspace_id_given_appends_five_workspace_checks(
        self, monkeypatch, tmp_path
    ) -> None:
        """给定 workspace_id 时返回 11 项,5 项工作区命名固定。"""
        from src import config as cfg_module

        monkeypatch.setattr(cfg_module.settings, "data_dir", tmp_path)
        report = run_health_checks(workspace_id="ws-nonexistent")
        assert len(report["checks"]) == 11
        names = {c["name"] for c in report["checks"]}
        # 工作区 5 项命名
        ws_names = {
            "workspace_sqlite_readwrite",
            "workspace_chroma_readwrite",
            "embedding_model_loadable",
            "backup_dir_health",
            "engine_version_compat",
        }
        assert ws_names.issubset(names)

    def test_workspace_id_none_skips_workspace_checks(self) -> None:
        """``workspace_id=None``(默认)仅跑 6 项全局检查,与 P4-acceptance 行为一致。"""
        report = run_health_checks()
        assert len(report["checks"]) == 6
        names = {c["name"] for c in report["checks"]}
        ws_names = {
            "workspace_sqlite_readwrite",
            "workspace_chroma_readwrite",
            "embedding_model_loadable",
            "backup_dir_health",
            "engine_version_compat",
        }
        assert ws_names.isdisjoint(names)

    def test_workspace_sqlite_missing_marks_fail_and_down(
        self, monkeypatch, tmp_path
    ) -> None:
        """工作区 ``novel.db`` 不存在 → workspace_sqlite_readwrite=fail →
        关键项 fail → status=down。"""
        from src import config as cfg_module

        monkeypatch.setattr(cfg_module.settings, "data_dir", tmp_path)
        report = run_health_checks(workspace_id="ws-nodb")
        ws_sqlite = next(
            c
            for c in report["checks"]
            if c["name"] == "workspace_sqlite_readwrite"
        )
        assert ws_sqlite["outcome"] == "fail"
        assert report["status"] == "down"


class TestWorkspaceChromaReadwriteAutoInit:
    """v1.0.x wave 5 session 1:``workspace_chroma_readwrite`` 探针对
    ``chroma/`` 目录不存在场景从"直接 fail"改为"顺便初始化 + pass"。

    覆盖根因:luo2 等"跑过 outline + cards 但未跑正文/MemoryExtractor"
    的工作区(向量库懒创建未触发)→ 健康检查直接 fail → UI 显示降级。
    本测试组验证修法生效。
    """

    def test_chroma_dir_missing_gets_auto_initialized_and_passes(
        self, monkeypatch, tmp_path
    ) -> None:
        """chroma/ 不存在 → 自动 mkdir + PersistentClient 探针 → pass +
        detail 标 'auto-initialized'。"""
        from src import config as cfg_module
        from src.core.workspace.workspace_paths import WorkspacePaths
        from src.services.health_service import _check_workspace_chroma_readwrite

        monkeypatch.setattr(cfg_module.settings, "data_dir", tmp_path)
        # 模拟"跑过 outline 但 chroma 缺失"的工作区:只建 data_dir,不建 chroma_dir
        paths = WorkspacePaths("ws-no-chroma")
        paths.data_dir.mkdir(parents=True, exist_ok=True)
        assert not paths.chroma_dir.exists()

        item = _check_workspace_chroma_readwrite("ws-no-chroma")

        # 顺便初始化后探针应该 pass
        assert item.outcome == "pass"
        assert "auto-initialized" in (item.detail or "")
        # chroma_dir 应被建出
        assert paths.chroma_dir.is_dir()

    def test_chroma_dir_existing_no_auto_init_marker(
        self, monkeypatch, tmp_path
    ) -> None:
        """chroma/ 已存在 → 探针走原路径(无 'auto-initialized' 标记)。"""
        from src import config as cfg_module
        from src.core.workspace.workspace_paths import WorkspacePaths
        from src.services.health_service import _check_workspace_chroma_readwrite

        monkeypatch.setattr(cfg_module.settings, "data_dir", tmp_path)
        paths = WorkspacePaths("ws-has-chroma")
        paths.chroma_dir.mkdir(parents=True, exist_ok=True)

        item = _check_workspace_chroma_readwrite("ws-has-chroma")

        assert item.outcome == "pass"
        assert "auto-initialized" not in (item.detail or "")
