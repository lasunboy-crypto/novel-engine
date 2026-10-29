"""P3-B-02:``src/services/config_service.py`` 单元测试(P6-B-13 reviewer / P6-B-14 retrieval / P6-B-15 cost_guard)。

测试矩阵
========

回退路径
--------
- 工作区目录不存在 → 返引擎默认配置(不抛错)
- ``project.yaml`` 不存在 → 返引擎默认配置
- ``project.yaml`` 为空 / 只有注释 → 返引擎默认配置

合并路径(P3-B-02 维护人 2026-05-09 拍 A:dict-level merge by key)
------------------------------------------------------------------
- 覆盖 ``writer_routing.default`` → default 生效,profiles + rules + agent_routing 保留默认
- 覆盖 ``writer_profiles.<key>`` 替换已有 profile → 该 key 替换,其余默认保留
- ``writer_profiles`` 新增 key → 新 profile 加入,默认全部保留
- 覆盖 ``writer_routing.rules`` 整段替换
- 覆盖 ``writer_routing.agent_routing`` 部分 key → 部分覆盖,其余默认保留
- 模块级 ``load_workspace_config(workspace_id)`` thin-wrapper 行为等价

错误路径
--------
- 引擎默认 ``config.yaml`` 不存在 → ``FileNotFoundError`` 透传
- ``project.yaml`` 顶层非 dict → ``ValueError``
- ``project.yaml`` ``writer_profiles`` 段非 dict → ``ValueError``
- 工作区单 profile 半残必填字段 → ``ValidationError``
- 覆盖后 ``routing.default`` 引用未定义 profile → ``ValidationError``

P6-B-13 reviewer 段测试(``TestLoadReviewerConfig`` + ``TestSaveReviewerConfig``)
--------------------------------------------------------------------------------
- happy:加载引擎默认 / 工作区整段覆盖 / 工作区字段级部分覆盖 / 保存后再加载等价
- error:工作区 reviewer 段非 dict / revise >= pass 交叉校验 / save 时校验失败不落盘
- gate:ReviewerConfig 默认值对齐 ChapterCard 单章字段 + reviewer.py 模块级常量
- rollback:save 时校验失败,原 project.yaml 不动
- WS event:save 含 reviewer 段时 publish config_updated(沿用 P6-B-12 同事件名)

P6-B-14 retrieval 段测试(``TestLoadRetrievalConfig`` + ``TestSaveRetrievalConfig``)
--------------------------------------------------------------------------------
- happy:加载引擎默认 / 工作区整段覆盖 / 工作区字段级部分覆盖 / 同时覆盖 writer+retrieval
- error:工作区 retrieval 段非 dict / top_k <= 0 / min_similarity 越界 / save 时校验失败不落盘
- gate:RetrievalConfig 默认值对齐 retriever.DEFAULT_TOP_K + 不过滤(0.0)
- rollback:save 时校验失败,原 project.yaml 不动
- WS event:save 含 retrieval 段时 publish config_updated(沿用 P6-B-12 同事件名)

P6-B-15 cost_guard 段测试(``TestLoadCostGuardConfigForWorkspace`` + ``TestSaveCostGuardConfig``)
-----------------------------------------------------------------------------------------------
- happy:加载引擎默认 / 工作区整段覆盖 / 工作区字段级部分覆盖 / 同时覆盖 writer+cost_guard
- error:工作区 cost_guard 段非 dict / *_usd_limit 负值 / action_on_exceed 越界 / save 时校验失败不落盘
- gate:CostGuardConfig 默认值对齐 cost_guard.py 字面 + notify_via 空列表显式禁用
- rollback:save 时校验失败,原 project.yaml 不动
- WS event:save 含 cost_guard 段时 publish config_updated(沿用 P6-B-12 同事件名)
"""

from __future__ import annotations

from pathlib import Path

import pytest
from pydantic import ValidationError

from src.core.models.generation_config import GenerationConfig
from src.core.models.retrieval_config import RetrievalConfig
from src.core.models.reviewer_config import ReviewerConfig
from src.core.models.routing import WriterProfilesConfig
from src.services.config_service import (
    BUILTIN_PROFILE_IDS,
    ConfigService,
)
from src.services.cost_guard import CostGuardConfig


class _FakeWSManager:
    """记录 publish 调用,供 P6-B-12 WS event 类测试断言。"""

    def __init__(self, *, raise_exc: Exception | None = None) -> None:
        self.events: list[dict[str, object]] = []
        self._raise_exc = raise_exc

    def publish(
        self,
        *,
        channel: str,
        event: str,
        data: dict[str, object],
    ) -> None:
        if self._raise_exc is not None:
            raise self._raise_exc
        self.events.append({"channel": channel, "event": event, "data": data})


# ---------- 辅助 fixtures ----------


_ENGINE_YAML = """\
writer_profiles:
  local_qwen_32k:
    provider: ollama
    model: qwen-novel
    base_url: http://localhost:11434
    context_window: 24576
    generation_params: {temperature: 0.7}
    extra_request_kwargs: {think: false}
  cloud_quality:
    provider: claude
    model: claude-sonnet-4-6
    context_window: 200000
    generation_params: {temperature: 0.85}
    cost_per_1k_input: 0.003
    cost_per_1k_output: 0.015
  cloud_premium:
    provider: claude
    model: claude-opus-4-7
    context_window: 200000
    generation_params: {temperature: 0.85}
    cost_per_1k_input: 0.015
    cost_per_1k_output: 0.075

writer_routing:
  default: local_qwen_32k
  rules:
    - condition: "is_arc_finale == True"
      profile: cloud_premium
  agent_routing:
    writer: local_qwen_32k
    reviewer: local_qwen_32k
    memory_extractor: local_qwen_32k

reviewer:
  pass_threshold: 7.5
  revise_threshold: 6.5
  escalation_lower_bound: 6.0
  escalation_upper_bound: 8.0

retrieval:
  top_k: 20
  min_similarity: 0.0

cost_guard:
  daily_usd_limit: 20.0
  monthly_usd_limit: 300.0
  per_workspace_daily_limit: 10.0
  action_on_exceed: pause
  notify_via: [log, websocket]

generation:
  word_count_min: 2200
  word_count_max: 3800
  max_rewrites: 4
"""


@pytest.fixture
def engine_config_path(tmp_path: Path) -> Path:
    """临时引擎默认 ``config.yaml``,3 个 profile + 1 rule + 3 agent_routing。"""
    p = tmp_path / "config.yaml"
    p.write_text(_ENGINE_YAML, encoding="utf-8")
    return p


@pytest.fixture
def workspaces_root(tmp_path: Path) -> Path:
    """临时 ``workspaces/`` 根。"""
    root = tmp_path / "workspaces"
    root.mkdir()
    return root


@pytest.fixture
def service(
    engine_config_path: Path, workspaces_root: Path
) -> ConfigService:
    return ConfigService(
        engine_config_path=engine_config_path,
        workspaces_root=workspaces_root,
    )


def _make_workspace_dir(workspaces_root: Path, workspace_id: str) -> Path:
    """在 ``workspaces_root`` 下建一个工作区目录,返工作区根。"""
    ws_root = workspaces_root / workspace_id
    ws_root.mkdir()
    return ws_root


def _write_project_yaml(ws_root: Path, content: str) -> Path:
    p = ws_root / "project.yaml"
    p.write_text(content, encoding="utf-8")
    return p


# ---------- 回退路径 ----------


class TestFallback:
    def test_workspace_dir_missing_returns_engine_defaults(
        self, service: ConfigService
    ) -> None:
        """工作区目录不存在 → 返引擎默认。"""
        cfg = service.load_workspace_config("not_exist")
        assert isinstance(cfg, WriterProfilesConfig)
        assert sorted(cfg.profiles.keys()) == [
            "cloud_premium",
            "cloud_quality",
            "local_qwen_32k",
        ]
        assert cfg.routing.default == "local_qwen_32k"

    def test_project_yaml_missing_returns_engine_defaults(
        self, service: ConfigService, workspaces_root: Path
    ) -> None:
        """工作区目录存在但无 ``project.yaml`` → 返引擎默认。"""
        _make_workspace_dir(workspaces_root, "ws_no_override")
        cfg = service.load_workspace_config("ws_no_override")
        assert cfg.routing.default == "local_qwen_32k"
        assert "cloud_quality" in cfg.profiles

    def test_project_yaml_empty_returns_engine_defaults(
        self, service: ConfigService, workspaces_root: Path
    ) -> None:
        """``project.yaml`` 空文件 → 返引擎默认。"""
        ws_root = _make_workspace_dir(workspaces_root, "ws_empty_yaml")
        _write_project_yaml(ws_root, "")
        cfg = service.load_workspace_config("ws_empty_yaml")
        assert cfg.routing.default == "local_qwen_32k"

    def test_project_yaml_comments_only_returns_engine_defaults(
        self, service: ConfigService, workspaces_root: Path
    ) -> None:
        """``project.yaml`` 只含注释 → 返引擎默认。"""
        ws_root = _make_workspace_dir(workspaces_root, "ws_comment_only")
        _write_project_yaml(ws_root, "# 仅注释,无任何键\n")
        cfg = service.load_workspace_config("ws_comment_only")
        assert cfg.routing.default == "local_qwen_32k"


# ---------- 合并路径(决策 A:dict-level merge by key)----------


class TestMerge:
    def test_override_default_only(
        self, service: ConfigService, workspaces_root: Path
    ) -> None:
        """覆盖 ``writer_routing.default``,profiles + rules + agent_routing 保留默认。

        对应 P3-B-02 plan 验收字面"fixture project.yaml 覆盖
        ``writer_routing.default`` 后,加载结果生效"。
        """
        ws_root = _make_workspace_dir(workspaces_root, "ws_default_override")
        _write_project_yaml(
            ws_root,
            "writer_routing:\n  default: cloud_quality\n",
        )
        cfg = service.load_workspace_config("ws_default_override")

        # default 覆盖生效
        assert cfg.routing.default == "cloud_quality"
        # profiles 引擎默认完整保留
        assert sorted(cfg.profiles.keys()) == [
            "cloud_premium",
            "cloud_quality",
            "local_qwen_32k",
        ]
        # rules 保留默认
        assert len(cfg.routing.rules) == 1
        assert cfg.routing.rules[0].profile == "cloud_premium"
        # agent_routing 保留默认 3 项
        assert cfg.routing.agent_routing == {
            "writer": "local_qwen_32k",
            "reviewer": "local_qwen_32k",
            "memory_extractor": "local_qwen_32k",
        }

    def test_override_replace_existing_profile(
        self, service: ConfigService, workspaces_root: Path
    ) -> None:
        """覆盖已有 profile_id → 该 profile 整体替换,其余保留。"""
        ws_root = _make_workspace_dir(workspaces_root, "ws_replace_profile")
        _write_project_yaml(
            ws_root,
            """\
writer_profiles:
  cloud_quality:
    provider: claude
    model: claude-sonnet-4-6
    context_window: 200000
    generation_params: {temperature: 0.95}
    cost_per_1k_input: 0.003
    cost_per_1k_output: 0.015
""",
        )
        cfg = service.load_workspace_config("ws_replace_profile")

        # cloud_quality 被替换:temperature 升到 0.95
        assert (
            cfg.profiles["cloud_quality"].generation_params["temperature"]
            == 0.95
        )
        # local_qwen_32k / cloud_premium 仍是引擎默认值
        assert (
            cfg.profiles["local_qwen_32k"].generation_params["temperature"]
            == 0.7
        )
        assert cfg.profiles["cloud_premium"].model == "claude-opus-4-7"

    def test_override_add_new_profile(
        self, service: ConfigService, workspaces_root: Path
    ) -> None:
        """工作区新增一个 profile,引擎默认完整保留。"""
        ws_root = _make_workspace_dir(workspaces_root, "ws_add_profile")
        _write_project_yaml(
            ws_root,
            """\
writer_profiles:
  custom_local:
    provider: ollama
    model: qwen-novel-large
    base_url: http://localhost:11434
    context_window: 32000
    generation_params: {temperature: 0.6}
    extra_request_kwargs: {think: false}
""",
        )
        cfg = service.load_workspace_config("ws_add_profile")

        # 新 profile 入列
        assert "custom_local" in cfg.profiles
        assert cfg.profiles["custom_local"].model == "qwen-novel-large"
        # 默认 3 个 profile 保留
        assert "local_qwen_32k" in cfg.profiles
        assert "cloud_quality" in cfg.profiles
        assert "cloud_premium" in cfg.profiles
        # routing 保留默认
        assert cfg.routing.default == "local_qwen_32k"

    def test_override_rules_replaces_entirely(
        self, service: ConfigService, workspaces_root: Path
    ) -> None:
        """工作区填 rules → 整段替换(列表顺序敏感,不部分合并)。"""
        ws_root = _make_workspace_dir(workspaces_root, "ws_rules_override")
        _write_project_yaml(
            ws_root,
            """\
writer_routing:
  rules:
    - condition: "tension_level >= 9"
      profile: cloud_premium
    - condition: "is_arc_finale == True"
      profile: cloud_quality
""",
        )
        cfg = service.load_workspace_config("ws_rules_override")

        assert len(cfg.routing.rules) == 2
        assert cfg.routing.rules[0].condition == "tension_level >= 9"
        assert cfg.routing.rules[1].profile == "cloud_quality"

    def test_override_agent_routing_partial(
        self, service: ConfigService, workspaces_root: Path
    ) -> None:
        """工作区只覆盖 ``agent_routing`` 部分 key,其余保留引擎默认。"""
        ws_root = _make_workspace_dir(workspaces_root, "ws_agent_partial")
        _write_project_yaml(
            ws_root,
            """\
writer_routing:
  agent_routing:
    reviewer: cloud_quality
""",
        )
        cfg = service.load_workspace_config("ws_agent_partial")

        # reviewer 被覆盖,writer / memory_extractor 保留默认
        assert cfg.routing.agent_routing == {
            "writer": "local_qwen_32k",
            "reviewer": "cloud_quality",
            "memory_extractor": "local_qwen_32k",
        }

    def test_module_level_load_workspace_config_smoke(
        self, monkeypatch: pytest.MonkeyPatch, tmp_path: Path
    ) -> None:
        """模块级 ``load_workspace_config(workspace_id)`` thin-wrapper 烟测试。

        验证调用走默认 ``settings.config_yaml_path`` / ``settings.data_dir``;
        通过 monkeypatch 改两处 settings 字段后,wrapper 行为与 ConfigService 等价。
        """
        # 准备临时引擎 config + 工作区
        engine_path = tmp_path / "engine.yaml"
        engine_path.write_text(_ENGINE_YAML, encoding="utf-8")
        ws_root = tmp_path / "ws_root"
        ws_root.mkdir()
        (ws_root / "smoke_ws").mkdir()
        (ws_root / "smoke_ws" / "project.yaml").write_text(
            "writer_routing:\n  default: cloud_premium\n",
            encoding="utf-8",
        )

        # monkeypatch settings(注:wrapper 实例化 ConfigService 时读 settings)
        from src.services import config_service as svc_module
        from src.config import settings

        monkeypatch.setattr(settings, "config_yaml_path", engine_path)
        monkeypatch.setattr(settings, "data_dir", ws_root)

        cfg = svc_module.load_workspace_config("smoke_ws")
        assert cfg.routing.default == "cloud_premium"


# ---------- 错误路径 ----------


class TestErrors:
    def test_engine_config_missing_raises(
        self, tmp_path: Path, workspaces_root: Path
    ) -> None:
        """引擎默认 ``config.yaml`` 不存在 → ``FileNotFoundError`` 透传。"""
        nonexistent = tmp_path / "no_such_config.yaml"
        svc = ConfigService(
            engine_config_path=nonexistent,
            workspaces_root=workspaces_root,
        )
        with pytest.raises(FileNotFoundError):
            svc.load_workspace_config("any_ws")

    def test_project_yaml_top_not_dict_raises(
        self, service: ConfigService, workspaces_root: Path
    ) -> None:
        """``project.yaml`` 顶层是 list / 标量 → ``ValueError``。"""
        ws_root = _make_workspace_dir(workspaces_root, "ws_top_list")
        _write_project_yaml(ws_root, "- a\n- b\n")
        with pytest.raises(ValueError, match="顶层应为映射"):
            service.load_workspace_config("ws_top_list")

    def test_writer_profiles_section_not_dict_raises(
        self, service: ConfigService, workspaces_root: Path
    ) -> None:
        """``writer_profiles`` 段是 list → ``ValueError``。"""
        ws_root = _make_workspace_dir(workspaces_root, "ws_profiles_list")
        _write_project_yaml(ws_root, "writer_profiles:\n  - a\n  - b\n")
        with pytest.raises(ValueError, match="writer_profiles 覆盖段应为 dict"):
            service.load_workspace_config("ws_profiles_list")

    def test_partial_profile_missing_required_fields_raises(
        self, service: ConfigService, workspaces_root: Path
    ) -> None:
        """工作区单 profile 半残必填字段(无 ``provider`` / ``model``)→ ValidationError。

        对应自决项 3:WriterProfile 内部不字段级合并;工作区想改某 profile
        的一个字段必须重写完整 profile。
        """
        ws_root = _make_workspace_dir(workspaces_root, "ws_partial")
        _write_project_yaml(
            ws_root,
            """\
writer_profiles:
  cloud_quality:
    generation_params: {temperature: 0.95}
""",
        )
        with pytest.raises(ValidationError):
            service.load_workspace_config("ws_partial")

    def test_override_default_references_unknown_profile_raises(
        self, service: ConfigService, workspaces_root: Path
    ) -> None:
        """覆盖后 ``routing.default`` 引用未定义 profile → ValidationError(交叉校验)。"""
        ws_root = _make_workspace_dir(workspaces_root, "ws_unknown_default")
        _write_project_yaml(
            ws_root,
            "writer_routing:\n  default: not_defined_profile\n",
        )
        with pytest.raises(ValidationError):
            service.load_workspace_config("ws_unknown_default")


# ---------- P6-B-12:save_workspace_config ----------


def _make_service_with_ws(
    engine_config_path: Path,
    workspaces_root: Path,
    *,
    ws_manager: _FakeWSManager | None = None,
) -> ConfigService:
    return ConfigService(
        engine_config_path=engine_config_path,
        workspaces_root=workspaces_root,
        ws_manager=ws_manager,
    )


class TestSaveWorkspaceConfig:
    """P6-B-12 PUT 端点服务层 5 类测试覆盖。"""

    # ---- happy ----

    def test_save_creates_project_yaml_and_returns_merged(
        self,
        engine_config_path: Path,
        workspaces_root: Path,
    ) -> None:
        """工作区目录在 + 无 project.yaml → 写 yaml + 返合并后配置。"""
        _make_workspace_dir(workspaces_root, "ws_new_save")
        ws_manager = _FakeWSManager()
        service = _make_service_with_ws(
            engine_config_path, workspaces_root, ws_manager=ws_manager
        )

        merged = service.save_workspace_config(
            "ws_new_save",
            {
                "writer_routing": {"default": "cloud_quality"},
            },
        )

        # yaml 落盘
        yaml_path = workspaces_root / "ws_new_save" / "project.yaml"
        assert yaml_path.is_file()
        # 合并后 default 切换
        assert merged.routing.default == "cloud_quality"
        # 引擎默认 profile 仍在
        assert set(merged.profiles.keys()) == {
            "cloud_premium",
            "cloud_quality",
            "local_qwen_32k",
        }
        # WS event 推送 1 次
        assert len(ws_manager.events) == 1
        evt = ws_manager.events[0]
        assert evt["channel"] == "workspace.ws_new_save.events"
        assert evt["event"] == "config_updated"
        assert evt["data"]["workspace_id"] == "ws_new_save"
        assert evt["data"]["default_profile"] == "cloud_quality"
        assert evt["data"]["profiles_count"] == 3

    def test_save_overrides_existing_yaml_with_backup(
        self,
        engine_config_path: Path,
        workspaces_root: Path,
    ) -> None:
        """已有 project.yaml → 备份到 .bak + 写新内容。"""
        ws_root = _make_workspace_dir(workspaces_root, "ws_overwrite")
        _write_project_yaml(
            ws_root,
            "writer_routing:\n  default: cloud_premium\n",
        )
        service = _make_service_with_ws(engine_config_path, workspaces_root)

        merged = service.save_workspace_config(
            "ws_overwrite",
            {"writer_routing": {"default": "cloud_quality"}},
        )

        assert merged.routing.default == "cloud_quality"
        # .bak 保留原文件内容
        bak_path = ws_root / "project.yaml.bak"
        assert bak_path.is_file()
        assert "cloud_premium" in bak_path.read_text(encoding="utf-8")
        # 新 yaml 是新内容
        new_yaml = (ws_root / "project.yaml").read_text(encoding="utf-8")
        assert "cloud_quality" in new_yaml
        assert "cloud_premium" not in new_yaml

    def test_save_adds_custom_profile_alongside_builtins(
        self,
        engine_config_path: Path,
        workspaces_root: Path,
    ) -> None:
        """工作区新增自定义 profile + 改 default 引用它 → 合并后 4 profile。"""
        _make_workspace_dir(workspaces_root, "ws_custom")
        service = _make_service_with_ws(engine_config_path, workspaces_root)

        merged = service.save_workspace_config(
            "ws_custom",
            {
                "writer_profiles": {
                    "qwen_creative": {
                        "provider": "ollama",
                        "model": "qwen-creative",
                        "base_url": "http://localhost:11434",
                        "context_window": 8000,
                        "generation_params": {"temperature": 0.9},
                        "extra_request_kwargs": {"think": False},
                        "cost_per_1k_input": 0.0,
                        "cost_per_1k_output": 0.0,
                        "notes": "高温度创作",
                    }
                },
                "writer_routing": {"default": "qwen_creative"},
            },
        )

        assert "qwen_creative" in merged.profiles
        assert merged.profiles["qwen_creative"].model == "qwen-creative"
        # 引擎默认 3 + 自定义 1 = 4
        assert len(merged.profiles) == 4
        assert merged.routing.default == "qwen_creative"

    def test_save_empty_dict_clears_overrides(
        self,
        engine_config_path: Path,
        workspaces_root: Path,
    ) -> None:
        """空 dict overrides → yaml 写空内容 → 合并等价引擎默认。"""
        ws_root = _make_workspace_dir(workspaces_root, "ws_empty_save")
        _write_project_yaml(
            ws_root,
            "writer_routing:\n  default: cloud_premium\n",
        )
        service = _make_service_with_ws(engine_config_path, workspaces_root)

        merged = service.save_workspace_config("ws_empty_save", {})

        # 默认回归引擎默认
        assert merged.routing.default == "local_qwen_32k"

    # ---- error ----

    def test_save_workspace_not_exist_raises_file_not_found(
        self,
        engine_config_path: Path,
        workspaces_root: Path,
    ) -> None:
        """工作区目录不存在 → FileNotFoundError(路由层翻 404 WORKSPACE_NOT_FOUND)。"""
        service = _make_service_with_ws(engine_config_path, workspaces_root)
        with pytest.raises(FileNotFoundError, match="工作区目录不存在"):
            service.save_workspace_config("ws_missing", {})

    def test_save_invalid_overrides_raises_validation_error(
        self,
        engine_config_path: Path,
        workspaces_root: Path,
    ) -> None:
        """overrides routing.default 引用未定义 profile → ValidationError + 不落盘。"""
        ws_root = _make_workspace_dir(workspaces_root, "ws_bad_save")
        service = _make_service_with_ws(engine_config_path, workspaces_root)

        with pytest.raises(ValidationError):
            service.save_workspace_config(
                "ws_bad_save",
                {"writer_routing": {"default": "nonexistent_profile"}},
            )

        # 失败后 project.yaml 不应创建
        assert not (ws_root / "project.yaml").exists()

    def test_save_profile_missing_required_fields_raises(
        self,
        engine_config_path: Path,
        workspaces_root: Path,
    ) -> None:
        """新 profile 缺必填字段 → ValidationError + 不落盘。"""
        ws_root = _make_workspace_dir(workspaces_root, "ws_bad_profile")
        service = _make_service_with_ws(engine_config_path, workspaces_root)

        with pytest.raises(ValidationError):
            service.save_workspace_config(
                "ws_bad_profile",
                {
                    "writer_profiles": {
                        "incomplete": {"provider": "ollama"}
                    }
                },
            )
        assert not (ws_root / "project.yaml").exists()

    # ---- gate ----

    def test_builtin_profile_ids_constant_exposes_four_ids(self) -> None:
        """``BUILTIN_PROFILE_IDS`` 含 4 内置 profile_id(供前端 UI 禁删按钮)。"""
        assert BUILTIN_PROFILE_IDS == frozenset(
            {
                "cloud_quality",
                "cloud_premium",
                "local_qwen_32k",
                "local_qwen_safe",
            }
        )

    def test_save_builtin_profile_override_is_allowed(
        self,
        engine_config_path: Path,
        workspaces_root: Path,
    ) -> None:
        """工作区可覆盖内置 profile_id(整体替换语义),后端不拒绝。"""
        _make_workspace_dir(workspaces_root, "ws_override_builtin")
        service = _make_service_with_ws(engine_config_path, workspaces_root)

        merged = service.save_workspace_config(
            "ws_override_builtin",
            {
                "writer_profiles": {
                    "cloud_quality": {
                        "provider": "claude",
                        "model": "claude-sonnet-4-6",
                        "context_window": 200000,
                        "generation_params": {"temperature": 0.95},
                        "cost_per_1k_input": 0.003,
                        "cost_per_1k_output": 0.015,
                        "notes": "工作区覆盖",
                    }
                }
            },
        )

        # 覆盖生效
        cq = merged.profiles["cloud_quality"]
        assert cq.generation_params["temperature"] == 0.95
        assert cq.notes == "工作区覆盖"

    # ---- rollback ----

    def test_save_validation_failure_keeps_original_yaml(
        self,
        engine_config_path: Path,
        workspaces_root: Path,
    ) -> None:
        """保存校验失败 → 原 project.yaml 不动(rollback 锚点)。"""
        ws_root = _make_workspace_dir(workspaces_root, "ws_rollback")
        original_content = "writer_routing:\n  default: cloud_premium\n"
        _write_project_yaml(ws_root, original_content)
        service = _make_service_with_ws(engine_config_path, workspaces_root)

        with pytest.raises(ValidationError):
            service.save_workspace_config(
                "ws_rollback",
                {"writer_routing": {"default": "nonexistent"}},
            )

        # 原文件内容未改
        assert (
            (ws_root / "project.yaml").read_text(encoding="utf-8")
            == original_content
        )

    # ---- WS event ----

    def test_save_ws_publish_failure_swallowed(
        self,
        engine_config_path: Path,
        workspaces_root: Path,
    ) -> None:
        """ws_manager.publish 抛错 → swallow + 不影响 save 成功返回。"""
        _make_workspace_dir(workspaces_root, "ws_ws_fail")
        ws_manager = _FakeWSManager(raise_exc=RuntimeError("ws broken"))
        service = _make_service_with_ws(
            engine_config_path, workspaces_root, ws_manager=ws_manager
        )

        merged = service.save_workspace_config(
            "ws_ws_fail",
            {"writer_routing": {"default": "cloud_quality"}},
        )

        # save 仍返合并配置
        assert merged.routing.default == "cloud_quality"
        # ws_manager 没记录(因为 publish 抛错)
        assert ws_manager.events == []

    def test_save_no_ws_manager_skips_publish_silently(
        self,
        engine_config_path: Path,
        workspaces_root: Path,
    ) -> None:
        """ws_manager=None → 不 publish,save 正常返回。"""
        _make_workspace_dir(workspaces_root, "ws_no_ws")
        service = _make_service_with_ws(
            engine_config_path, workspaces_root, ws_manager=None
        )
        merged = service.save_workspace_config(
            "ws_no_ws",
            {"writer_routing": {"default": "cloud_quality"}},
        )
        assert merged.routing.default == "cloud_quality"


# =============================================================================
# P6-B-13:reviewer 段加载 + 保存
# =============================================================================


class TestLoadReviewerConfig:
    """``ConfigService.load_reviewer_config`` 单元测试(P6-B-13)。"""

    # ---- happy ----

    def test_engine_defaults_when_no_workspace_dir(
        self, service: ConfigService
    ) -> None:
        """工作区目录不存在 → 引擎默认 reviewer 配置。"""
        cfg = service.load_reviewer_config("ws_not_exist")
        assert isinstance(cfg, ReviewerConfig)
        assert cfg.pass_threshold == 7.5
        assert cfg.revise_threshold == 6.5
        assert cfg.escalation_lower_bound == 6.0
        assert cfg.escalation_upper_bound == 8.0

    def test_engine_defaults_when_workspace_has_no_reviewer_section(
        self, service: ConfigService, workspaces_root: Path
    ) -> None:
        """工作区有 project.yaml 但无 reviewer 段 → 引擎默认。"""
        ws_root = _make_workspace_dir(workspaces_root, "ws_no_reviewer")
        _write_project_yaml(
            ws_root, "writer_routing:\n  default: cloud_quality\n"
        )
        cfg = service.load_reviewer_config("ws_no_reviewer")
        assert cfg.pass_threshold == 7.5
        assert cfg.escalation_upper_bound == 8.0

    def test_workspace_full_override(
        self, service: ConfigService, workspaces_root: Path
    ) -> None:
        """工作区 reviewer 段全字段覆盖 → 4 字段都生效。"""
        ws_root = _make_workspace_dir(workspaces_root, "ws_reviewer_full")
        _write_project_yaml(
            ws_root,
            "reviewer:\n"
            "  pass_threshold: 8.0\n"
            "  revise_threshold: 7.0\n"
            "  escalation_lower_bound: 5.5\n"
            "  escalation_upper_bound: 7.5\n",
        )
        cfg = service.load_reviewer_config("ws_reviewer_full")
        assert cfg.pass_threshold == 8.0
        assert cfg.revise_threshold == 7.0
        assert cfg.escalation_lower_bound == 5.5
        assert cfg.escalation_upper_bound == 7.5

    def test_workspace_partial_override_merges_field_level(
        self, service: ConfigService, workspaces_root: Path
    ) -> None:
        """工作区 reviewer 段只填部分字段 → 字段级合并(未填的保留引擎默认)。"""
        ws_root = _make_workspace_dir(workspaces_root, "ws_reviewer_partial")
        _write_project_yaml(
            ws_root,
            "reviewer:\n  pass_threshold: 9.0\n",
        )
        cfg = service.load_reviewer_config("ws_reviewer_partial")
        # 工作区覆盖
        assert cfg.pass_threshold == 9.0
        # 引擎默认保留
        assert cfg.revise_threshold == 6.5
        assert cfg.escalation_lower_bound == 6.0
        assert cfg.escalation_upper_bound == 8.0

    # ---- error ----

    def test_workspace_yaml_top_non_dict_raises(
        self, service: ConfigService, workspaces_root: Path
    ) -> None:
        """工作区 project.yaml 顶层非 dict → ValueError。"""
        ws_root = _make_workspace_dir(workspaces_root, "ws_top_bad")
        _write_project_yaml(ws_root, "- a\n- b\n")
        with pytest.raises(ValueError, match="project.yaml 顶层应为映射"):
            service.load_reviewer_config("ws_top_bad")

    def test_workspace_reviewer_section_non_dict_raises(
        self, service: ConfigService, workspaces_root: Path
    ) -> None:
        """工作区 reviewer 段非 dict → ValueError。"""
        ws_root = _make_workspace_dir(workspaces_root, "ws_section_bad")
        _write_project_yaml(ws_root, "reviewer:\n  - a\n  - b\n")
        with pytest.raises(ValueError, match="reviewer 段应为 dict"):
            service.load_reviewer_config("ws_section_bad")

    def test_workspace_invalid_threshold_ordering_raises(
        self, service: ConfigService, workspaces_root: Path
    ) -> None:
        """revise >= pass 触发交叉校验 → ValidationError。"""
        ws_root = _make_workspace_dir(workspaces_root, "ws_bad_order")
        _write_project_yaml(
            ws_root,
            "reviewer:\n  pass_threshold: 6.0\n  revise_threshold: 7.0\n",
        )
        with pytest.raises(ValidationError):
            service.load_reviewer_config("ws_bad_order")

    def test_workspace_escalation_invalid_ordering_raises(
        self, service: ConfigService, workspaces_root: Path
    ) -> None:
        """escalation_lower >= escalation_upper → ValidationError。"""
        ws_root = _make_workspace_dir(workspaces_root, "ws_bad_esc")
        _write_project_yaml(
            ws_root,
            "reviewer:\n  escalation_lower_bound: 8.0\n  escalation_upper_bound: 7.0\n",
        )
        with pytest.raises(ValidationError):
            service.load_reviewer_config("ws_bad_esc")

    # ---- gate(默认值对齐 ChapterCard + reviewer.py 模块级常量)----

    def test_reviewer_config_explicit_returns_none_when_no_workspace_dir(
        self, service: ConfigService
    ) -> None:
        """v1.0.x Commit D:工作区目录不存在 → ``load_reviewer_config_explicit`` 返 None。"""
        assert service.load_reviewer_config_explicit("ws_not_exist") is None

    def test_reviewer_config_explicit_returns_none_when_no_reviewer_section(
        self, service: ConfigService, workspaces_root: Path
    ) -> None:
        """v1.0.x Commit D:工作区 project.yaml 没有 reviewer 段 → 返 None
        (区分于 ``load_reviewer_config`` 在此场景返引擎默认)。"""
        ws_root = _make_workspace_dir(workspaces_root, "ws_explicit_no_section")
        _write_project_yaml(
            ws_root, "writer_routing:\n  default: cloud_quality\n"
        )
        assert (
            service.load_reviewer_config_explicit("ws_explicit_no_section")
            is None
        )

    def test_reviewer_config_explicit_returns_config_when_section_present(
        self, service: ConfigService, workspaces_root: Path
    ) -> None:
        """v1.0.x Commit D:工作区 project.yaml 显式有 reviewer 段 → 返字段级合并
        后的 ReviewerConfig(与 ``load_reviewer_config`` 同结果)。"""
        ws_root = _make_workspace_dir(workspaces_root, "ws_explicit_full")
        _write_project_yaml(
            ws_root,
            "reviewer:\n"
            "  pass_threshold: 8.5\n"
            "  revise_threshold: 7.0\n",
        )
        cfg = service.load_reviewer_config_explicit("ws_explicit_full")
        assert cfg is not None
        assert cfg.pass_threshold == 8.5
        assert cfg.revise_threshold == 7.0
        # 未填字段保留引擎默认(对齐 load_reviewer_config 合并语义)
        assert cfg.escalation_lower_bound == 6.0
        assert cfg.escalation_upper_bound == 8.0

    def test_reviewer_config_explicit_empty_yaml_returns_none(
        self, service: ConfigService, workspaces_root: Path
    ) -> None:
        """v1.0.x Commit D:project.yaml 内容为空(None)→ 返 None。"""
        ws_root = _make_workspace_dir(workspaces_root, "ws_explicit_empty")
        _write_project_yaml(ws_root, "")
        assert (
            service.load_reviewer_config_explicit("ws_explicit_empty") is None
        )

    def test_reviewer_config_explicit_top_non_dict_raises(
        self, service: ConfigService, workspaces_root: Path
    ) -> None:
        """v1.0.x Commit D:project.yaml 顶层非 dict 仍触发 ValueError(对齐
        load_reviewer_config 错误语义,不被静默 fallback)。"""
        ws_root = _make_workspace_dir(workspaces_root, "ws_explicit_top_bad")
        _write_project_yaml(ws_root, "- a\n- b\n")
        with pytest.raises(ValueError, match="project.yaml 顶层应为映射"):
            service.load_reviewer_config_explicit("ws_explicit_top_bad")

    def test_reviewer_config_defaults_match_runtime_sources(self) -> None:
        """ReviewerConfig() 4 字段默认对齐 ChapterCard 单章字段 + reviewer.py 常量。"""
        from src.core.agents.reviewer import (
            ESCALATION_LOWER_BOUND,
            ESCALATION_UPPER_BOUND,
        )
        from src.core.models.chapter_card import ChapterCard

        defaults = ReviewerConfig()
        chapter_card_fields = ChapterCard.model_fields
        assert (
            defaults.pass_threshold
            == chapter_card_fields["review_threshold"].default
        )
        assert (
            defaults.revise_threshold
            == chapter_card_fields["revise_threshold"].default
        )
        assert defaults.escalation_lower_bound == ESCALATION_LOWER_BOUND
        assert defaults.escalation_upper_bound == ESCALATION_UPPER_BOUND


class TestSaveReviewerConfig:
    """``ConfigService.save_workspace_config`` reviewer 段 5 类覆盖(P6-B-13)。"""

    # ---- happy ----

    def test_save_with_reviewer_section_persists_and_returns_merged(
        self,
        engine_config_path: Path,
        workspaces_root: Path,
    ) -> None:
        """overrides 含 reviewer 段 → yaml 落盘 + 重读 reviewer 配置生效。"""
        _make_workspace_dir(workspaces_root, "ws_save_reviewer")
        ws_manager = _FakeWSManager()
        service = _make_service_with_ws(
            engine_config_path, workspaces_root, ws_manager=ws_manager
        )

        service.save_workspace_config(
            "ws_save_reviewer",
            {
                "reviewer": {
                    "pass_threshold": 8.5,
                    "revise_threshold": 7.0,
                }
            },
        )

        # yaml 落盘含 reviewer 段
        yaml_path = workspaces_root / "ws_save_reviewer" / "project.yaml"
        content = yaml_path.read_text(encoding="utf-8")
        assert "reviewer:" in content
        assert "pass_threshold: 8.5" in content

        # 重读 reviewer 配置生效(字段级合并)
        reviewer_cfg = service.load_reviewer_config("ws_save_reviewer")
        assert reviewer_cfg.pass_threshold == 8.5
        assert reviewer_cfg.revise_threshold == 7.0
        # 未填字段保留引擎默认
        assert reviewer_cfg.escalation_lower_bound == 6.0
        assert reviewer_cfg.escalation_upper_bound == 8.0

        # WS event 发送
        assert len(ws_manager.events) == 1
        assert ws_manager.events[0]["event"] == "config_updated"

    def test_save_with_writer_and_reviewer_together(
        self,
        engine_config_path: Path,
        workspaces_root: Path,
    ) -> None:
        """同时覆盖 writer + reviewer 段 → 两段都落盘 + 各自生效。"""
        _make_workspace_dir(workspaces_root, "ws_combo")
        service = _make_service_with_ws(engine_config_path, workspaces_root)

        merged = service.save_workspace_config(
            "ws_combo",
            {
                "writer_routing": {"default": "cloud_quality"},
                "reviewer": {"pass_threshold": 9.0},
            },
        )
        assert merged.routing.default == "cloud_quality"
        reviewer_cfg = service.load_reviewer_config("ws_combo")
        assert reviewer_cfg.pass_threshold == 9.0

    # ---- error ----

    def test_save_reviewer_section_non_dict_raises(
        self,
        engine_config_path: Path,
        workspaces_root: Path,
    ) -> None:
        """overrides.reviewer 非 dict → ValueError + 不落盘。"""
        ws_root = _make_workspace_dir(workspaces_root, "ws_bad_section")
        service = _make_service_with_ws(engine_config_path, workspaces_root)
        with pytest.raises(ValueError, match="reviewer 覆盖段应为 dict"):
            service.save_workspace_config(
                "ws_bad_section",
                {"reviewer": ["pass_threshold", 7.0]},
            )
        assert not (ws_root / "project.yaml").exists()

    def test_save_reviewer_invalid_threshold_raises(
        self,
        engine_config_path: Path,
        workspaces_root: Path,
    ) -> None:
        """revise >= pass → ValidationError + 不落盘。"""
        ws_root = _make_workspace_dir(workspaces_root, "ws_bad_thr")
        service = _make_service_with_ws(engine_config_path, workspaces_root)
        with pytest.raises(ValidationError):
            service.save_workspace_config(
                "ws_bad_thr",
                {
                    "reviewer": {
                        "pass_threshold": 6.0,
                        "revise_threshold": 7.0,
                    }
                },
            )
        assert not (ws_root / "project.yaml").exists()

    # ---- gate(配合 writer 校验不被 reviewer 段污染) ----

    def test_save_reviewer_none_value_skips_merge(
        self,
        engine_config_path: Path,
        workspaces_root: Path,
    ) -> None:
        """overrides.reviewer=None → 等价于不传该段(跳过校验,不落 reviewer)。"""
        ws_root = _make_workspace_dir(workspaces_root, "ws_none_section")
        service = _make_service_with_ws(engine_config_path, workspaces_root)
        # to_yaml_dict 已经会过滤 None,但 service 层若直接收到 None 也应容忍
        service.save_workspace_config(
            "ws_none_section",
            {"reviewer": None, "writer_routing": {"default": "cloud_quality"}},
        )
        # reviewer=None 落盘后 reviewer 段实际是 null,load 时返默认
        # (yaml.safe_dump null 显式落盘 ≠ 不写段。但 to_yaml_dict 不会传 None,
        # 此处主要验证 service 不抛 ValueError)
        content = (ws_root / "project.yaml").read_text(encoding="utf-8")
        assert "writer_routing" in content

    # ---- rollback ----

    def test_save_invalid_reviewer_keeps_original_yaml(
        self,
        engine_config_path: Path,
        workspaces_root: Path,
    ) -> None:
        """reviewer 校验失败 → 原 project.yaml 不动(rollback 锚点)。"""
        ws_root = _make_workspace_dir(workspaces_root, "ws_reviewer_rollback")
        original = "writer_routing:\n  default: cloud_premium\n"
        _write_project_yaml(ws_root, original)
        service = _make_service_with_ws(engine_config_path, workspaces_root)

        with pytest.raises(ValidationError):
            service.save_workspace_config(
                "ws_reviewer_rollback",
                {
                    "reviewer": {
                        "pass_threshold": 5.0,
                        "revise_threshold": 7.0,
                    }
                },
            )
        # 原 yaml 内容未变
        assert (
            (ws_root / "project.yaml").read_text(encoding="utf-8") == original
        )

    # ---- WS event ----

    def test_save_reviewer_publishes_config_updated_event(
        self,
        engine_config_path: Path,
        workspaces_root: Path,
    ) -> None:
        """只改 reviewer 段也走 config_updated event(沿用 P6-B-12 同事件)。"""
        _make_workspace_dir(workspaces_root, "ws_reviewer_ws")
        ws_manager = _FakeWSManager()
        service = _make_service_with_ws(
            engine_config_path, workspaces_root, ws_manager=ws_manager
        )
        service.save_workspace_config(
            "ws_reviewer_ws",
            {"reviewer": {"pass_threshold": 8.0}},
        )
        assert len(ws_manager.events) == 1
        evt = ws_manager.events[0]
        assert evt["channel"] == "workspace.ws_reviewer_ws.events"
        assert evt["event"] == "config_updated"


# =============================================================================
# P6-B-14:retrieval 段加载 + 保存
# =============================================================================


class TestLoadRetrievalConfig:
    """``ConfigService.load_retrieval_config`` 单元测试(P6-B-14)。"""

    # ---- happy ----

    def test_engine_defaults_when_no_workspace_dir(
        self, service: ConfigService
    ) -> None:
        """工作区目录不存在 → 引擎默认 retrieval 配置。"""
        cfg = service.load_retrieval_config("ws_not_exist")
        assert isinstance(cfg, RetrievalConfig)
        assert cfg.top_k == 20
        assert cfg.min_similarity == 0.0

    def test_engine_defaults_when_workspace_has_no_retrieval_section(
        self, service: ConfigService, workspaces_root: Path
    ) -> None:
        """工作区有 project.yaml 但无 retrieval 段 → 引擎默认。"""
        ws_root = _make_workspace_dir(workspaces_root, "ws_no_retrieval")
        _write_project_yaml(
            ws_root, "writer_routing:\n  default: cloud_quality\n"
        )
        cfg = service.load_retrieval_config("ws_no_retrieval")
        assert cfg.top_k == 20
        assert cfg.min_similarity == 0.0

    def test_workspace_full_override(
        self, service: ConfigService, workspaces_root: Path
    ) -> None:
        """工作区 retrieval 段全字段覆盖 → 2 字段都生效。"""
        ws_root = _make_workspace_dir(workspaces_root, "ws_retrieval_full")
        _write_project_yaml(
            ws_root,
            "retrieval:\n  top_k: 30\n  min_similarity: 0.4\n",
        )
        cfg = service.load_retrieval_config("ws_retrieval_full")
        assert cfg.top_k == 30
        assert cfg.min_similarity == 0.4

    def test_workspace_partial_override_merges_field_level(
        self, service: ConfigService, workspaces_root: Path
    ) -> None:
        """工作区 retrieval 只填 top_k → min_similarity 保留引擎默认。"""
        ws_root = _make_workspace_dir(workspaces_root, "ws_retrieval_partial")
        _write_project_yaml(
            ws_root, "retrieval:\n  top_k: 50\n"
        )
        cfg = service.load_retrieval_config("ws_retrieval_partial")
        assert cfg.top_k == 50
        # 引擎默认保留
        assert cfg.min_similarity == 0.0

    # ---- error ----

    def test_workspace_yaml_top_non_dict_raises(
        self, service: ConfigService, workspaces_root: Path
    ) -> None:
        """工作区 project.yaml 顶层非 dict → ValueError。"""
        ws_root = _make_workspace_dir(workspaces_root, "ws_top_bad_r")
        _write_project_yaml(ws_root, "- a\n- b\n")
        with pytest.raises(ValueError, match="project.yaml 顶层应为映射"):
            service.load_retrieval_config("ws_top_bad_r")

    def test_workspace_retrieval_section_non_dict_raises(
        self, service: ConfigService, workspaces_root: Path
    ) -> None:
        """工作区 retrieval 段非 dict → ValueError。"""
        ws_root = _make_workspace_dir(workspaces_root, "ws_section_bad_r")
        _write_project_yaml(ws_root, "retrieval:\n  - a\n  - b\n")
        with pytest.raises(ValueError, match="retrieval 段应为 dict"):
            service.load_retrieval_config("ws_section_bad_r")

    def test_workspace_top_k_too_small_raises(
        self, service: ConfigService, workspaces_root: Path
    ) -> None:
        """top_k <= 0 → ValidationError(Pydantic ge=1 校验)。"""
        ws_root = _make_workspace_dir(workspaces_root, "ws_bad_topk")
        _write_project_yaml(
            ws_root, "retrieval:\n  top_k: 0\n"
        )
        with pytest.raises(ValidationError):
            service.load_retrieval_config("ws_bad_topk")

    def test_workspace_min_similarity_out_of_range_raises(
        self, service: ConfigService, workspaces_root: Path
    ) -> None:
        """min_similarity > 1.0 → ValidationError(Pydantic le=1.0 校验)。"""
        ws_root = _make_workspace_dir(workspaces_root, "ws_bad_sim")
        _write_project_yaml(
            ws_root, "retrieval:\n  min_similarity: 1.5\n"
        )
        with pytest.raises(ValidationError):
            service.load_retrieval_config("ws_bad_sim")

    # ---- gate(默认值对齐 retriever 模块级常量)----

    def test_retrieval_config_defaults_match_retriever_constants(self) -> None:
        """RetrievalConfig() 默认对齐 retriever.DEFAULT_TOP_K + min_similarity=0.0。"""
        from src.core.memory.retriever import DEFAULT_TOP_K

        defaults = RetrievalConfig()
        assert defaults.top_k == DEFAULT_TOP_K
        assert defaults.min_similarity == 0.0


class TestSaveRetrievalConfig:
    """``ConfigService.save_workspace_config`` retrieval 段 5 类覆盖(P6-B-14)。"""

    # ---- happy ----

    def test_save_with_retrieval_section_persists_and_returns_merged(
        self,
        engine_config_path: Path,
        workspaces_root: Path,
    ) -> None:
        """overrides 含 retrieval 段 → yaml 落盘 + 重读 retrieval 配置生效。"""
        _make_workspace_dir(workspaces_root, "ws_save_retrieval")
        ws_manager = _FakeWSManager()
        service = _make_service_with_ws(
            engine_config_path, workspaces_root, ws_manager=ws_manager
        )

        service.save_workspace_config(
            "ws_save_retrieval",
            {
                "retrieval": {
                    "top_k": 25,
                    "min_similarity": 0.3,
                }
            },
        )

        # yaml 落盘含 retrieval 段
        yaml_path = workspaces_root / "ws_save_retrieval" / "project.yaml"
        content = yaml_path.read_text(encoding="utf-8")
        assert "retrieval:" in content
        assert "top_k: 25" in content
        assert "min_similarity: 0.3" in content

        # 重读 retrieval 配置生效
        retrieval_cfg = service.load_retrieval_config("ws_save_retrieval")
        assert retrieval_cfg.top_k == 25
        assert retrieval_cfg.min_similarity == 0.3

        # WS event 发送
        assert len(ws_manager.events) == 1
        assert ws_manager.events[0]["event"] == "config_updated"

    def test_save_with_retrieval_partial_field(
        self,
        engine_config_path: Path,
        workspaces_root: Path,
    ) -> None:
        """overrides.retrieval 只填 top_k → 落盘后 min_similarity 仍走引擎默认。"""
        _make_workspace_dir(workspaces_root, "ws_retrieval_partial_save")
        service = _make_service_with_ws(engine_config_path, workspaces_root)

        service.save_workspace_config(
            "ws_retrieval_partial_save",
            {"retrieval": {"top_k": 15}},
        )
        retrieval_cfg = service.load_retrieval_config(
            "ws_retrieval_partial_save"
        )
        assert retrieval_cfg.top_k == 15
        assert retrieval_cfg.min_similarity == 0.0

    def test_save_with_writer_and_retrieval_together(
        self,
        engine_config_path: Path,
        workspaces_root: Path,
    ) -> None:
        """同时覆盖 writer + retrieval 段 → 两段都落盘 + 各自生效。"""
        _make_workspace_dir(workspaces_root, "ws_combo_r")
        service = _make_service_with_ws(engine_config_path, workspaces_root)

        merged = service.save_workspace_config(
            "ws_combo_r",
            {
                "writer_routing": {"default": "cloud_quality"},
                "retrieval": {"top_k": 40},
            },
        )
        assert merged.routing.default == "cloud_quality"
        retrieval_cfg = service.load_retrieval_config("ws_combo_r")
        assert retrieval_cfg.top_k == 40

    # ---- error ----

    def test_save_retrieval_section_non_dict_raises(
        self,
        engine_config_path: Path,
        workspaces_root: Path,
    ) -> None:
        """overrides.retrieval 非 dict → ValueError + 不落盘。"""
        ws_root = _make_workspace_dir(workspaces_root, "ws_bad_section_r")
        service = _make_service_with_ws(engine_config_path, workspaces_root)
        with pytest.raises(ValueError, match="retrieval 覆盖段应为 dict"):
            service.save_workspace_config(
                "ws_bad_section_r",
                {"retrieval": ["top_k", 20]},
            )
        assert not (ws_root / "project.yaml").exists()

    def test_save_retrieval_invalid_top_k_raises(
        self,
        engine_config_path: Path,
        workspaces_root: Path,
    ) -> None:
        """top_k <= 0 → ValidationError + 不落盘。"""
        ws_root = _make_workspace_dir(workspaces_root, "ws_bad_topk_save")
        service = _make_service_with_ws(engine_config_path, workspaces_root)
        with pytest.raises(ValidationError):
            service.save_workspace_config(
                "ws_bad_topk_save",
                {"retrieval": {"top_k": 0}},
            )
        assert not (ws_root / "project.yaml").exists()

    # ---- gate(None 视为不传该段) ----

    def test_save_retrieval_none_value_skips_merge(
        self,
        engine_config_path: Path,
        workspaces_root: Path,
    ) -> None:
        """overrides.retrieval=None → 等价于不传该段(跳过校验,不落 retrieval)。"""
        ws_root = _make_workspace_dir(workspaces_root, "ws_none_section_r")
        service = _make_service_with_ws(engine_config_path, workspaces_root)
        service.save_workspace_config(
            "ws_none_section_r",
            {"retrieval": None, "writer_routing": {"default": "cloud_quality"}},
        )
        content = (ws_root / "project.yaml").read_text(encoding="utf-8")
        assert "writer_routing" in content

    # ---- rollback ----

    def test_save_invalid_retrieval_keeps_original_yaml(
        self,
        engine_config_path: Path,
        workspaces_root: Path,
    ) -> None:
        """retrieval 校验失败 → 原 project.yaml 不动(rollback 锚点)。"""
        ws_root = _make_workspace_dir(
            workspaces_root, "ws_retrieval_rollback"
        )
        original = "writer_routing:\n  default: cloud_premium\n"
        _write_project_yaml(ws_root, original)
        service = _make_service_with_ws(engine_config_path, workspaces_root)

        with pytest.raises(ValidationError):
            service.save_workspace_config(
                "ws_retrieval_rollback",
                {"retrieval": {"top_k": -1}},
            )
        assert (
            (ws_root / "project.yaml").read_text(encoding="utf-8") == original
        )

    # ---- WS event ----

    def test_save_retrieval_publishes_config_updated_event(
        self,
        engine_config_path: Path,
        workspaces_root: Path,
    ) -> None:
        """只改 retrieval 段也走 config_updated event(沿用 P6-B-12 同事件)。"""
        _make_workspace_dir(workspaces_root, "ws_retrieval_ws")
        ws_manager = _FakeWSManager()
        service = _make_service_with_ws(
            engine_config_path, workspaces_root, ws_manager=ws_manager
        )
        service.save_workspace_config(
            "ws_retrieval_ws",
            {"retrieval": {"top_k": 10, "min_similarity": 0.5}},
        )
        assert len(ws_manager.events) == 1
        evt = ws_manager.events[0]
        assert evt["channel"] == "workspace.ws_retrieval_ws.events"
        assert evt["event"] == "config_updated"


# =============================================================================
# P6-B-15:cost_guard 段 — 加载 + 保存 + 5 类覆盖
# =============================================================================


class TestLoadCostGuardConfigForWorkspace:
    """``ConfigService.load_cost_guard_config_for_workspace`` 单元测试(P6-B-15)。"""

    # ---- happy ----

    def test_engine_defaults_when_no_workspace_dir(
        self, service: ConfigService
    ) -> None:
        """工作区目录不存在 → 引擎默认 cost_guard 配置(从 yaml 加载)。"""
        cfg = service.load_cost_guard_config_for_workspace("ws_not_exist_c")
        assert isinstance(cfg, CostGuardConfig)
        assert cfg.daily_usd_limit == 20.0
        assert cfg.monthly_usd_limit == 300.0
        assert cfg.per_workspace_daily_limit == 10.0
        assert cfg.action_on_exceed == "pause"
        assert cfg.notify_via == ["log", "websocket"]

    def test_engine_defaults_when_workspace_has_no_cost_guard_section(
        self, service: ConfigService, workspaces_root: Path
    ) -> None:
        """工作区有 project.yaml 但无 cost_guard 段 → 引擎默认。"""
        ws_root = _make_workspace_dir(workspaces_root, "ws_no_cost_guard")
        _write_project_yaml(
            ws_root, "writer_routing:\n  default: cloud_quality\n"
        )
        cfg = service.load_cost_guard_config_for_workspace("ws_no_cost_guard")
        assert cfg.daily_usd_limit == 20.0
        assert cfg.action_on_exceed == "pause"

    def test_workspace_full_override(
        self, service: ConfigService, workspaces_root: Path
    ) -> None:
        """工作区 cost_guard 段全字段覆盖 → 5 字段都生效。"""
        ws_root = _make_workspace_dir(workspaces_root, "ws_cost_guard_full")
        _write_project_yaml(
            ws_root,
            """\
cost_guard:
  daily_usd_limit: 50.0
  monthly_usd_limit: 500.0
  per_workspace_daily_limit: 25.0
  action_on_exceed: warn
  notify_via: [log]
""",
        )
        cfg = service.load_cost_guard_config_for_workspace("ws_cost_guard_full")
        assert cfg.daily_usd_limit == 50.0
        assert cfg.monthly_usd_limit == 500.0
        assert cfg.per_workspace_daily_limit == 25.0
        assert cfg.action_on_exceed == "warn"
        assert cfg.notify_via == ["log"]

    def test_workspace_partial_override_merges_field_level(
        self, service: ConfigService, workspaces_root: Path
    ) -> None:
        """工作区 cost_guard 只填 action_on_exceed → 其他字段保留引擎默认。"""
        ws_root = _make_workspace_dir(
            workspaces_root, "ws_cost_guard_partial"
        )
        _write_project_yaml(
            ws_root,
            "cost_guard:\n  action_on_exceed: continue\n",
        )
        cfg = service.load_cost_guard_config_for_workspace(
            "ws_cost_guard_partial"
        )
        assert cfg.action_on_exceed == "continue"
        # 引擎默认保留
        assert cfg.daily_usd_limit == 20.0
        assert cfg.monthly_usd_limit == 300.0
        assert cfg.notify_via == ["log", "websocket"]

    def test_workspace_partial_override_notify_via_empty_list(
        self, service: ConfigService, workspaces_root: Path
    ) -> None:
        """工作区 cost_guard 段 notify_via=[] → 显式禁用所有通知。"""
        ws_root = _make_workspace_dir(workspaces_root, "ws_cost_guard_empty")
        _write_project_yaml(
            ws_root,
            "cost_guard:\n  notify_via: []\n",
        )
        cfg = service.load_cost_guard_config_for_workspace(
            "ws_cost_guard_empty"
        )
        assert cfg.notify_via == []
        # 其他字段保留引擎默认
        assert cfg.daily_usd_limit == 20.0

    # ---- error ----

    def test_workspace_yaml_top_non_dict_raises(
        self, service: ConfigService, workspaces_root: Path
    ) -> None:
        """工作区 project.yaml 顶层非 dict → ValueError。"""
        ws_root = _make_workspace_dir(workspaces_root, "ws_top_bad_c")
        _write_project_yaml(ws_root, "- a\n- b\n")
        with pytest.raises(ValueError, match="project.yaml 顶层应为映射"):
            service.load_cost_guard_config_for_workspace("ws_top_bad_c")

    def test_workspace_cost_guard_section_non_dict_raises(
        self, service: ConfigService, workspaces_root: Path
    ) -> None:
        """工作区 cost_guard 段非 dict → ValueError。"""
        ws_root = _make_workspace_dir(workspaces_root, "ws_section_bad_c")
        _write_project_yaml(ws_root, "cost_guard:\n  - a\n  - b\n")
        with pytest.raises(ValueError, match="cost_guard 段应为 dict"):
            service.load_cost_guard_config_for_workspace("ws_section_bad_c")

    def test_workspace_negative_usd_limit_raises(
        self, service: ConfigService, workspaces_root: Path
    ) -> None:
        """daily_usd_limit < 0 → ValidationError(Pydantic ge=0 校验)。"""
        ws_root = _make_workspace_dir(workspaces_root, "ws_bad_usd")
        _write_project_yaml(
            ws_root, "cost_guard:\n  daily_usd_limit: -1.0\n"
        )
        with pytest.raises(ValidationError):
            service.load_cost_guard_config_for_workspace("ws_bad_usd")

    def test_workspace_invalid_action_on_exceed_raises(
        self, service: ConfigService, workspaces_root: Path
    ) -> None:
        """action_on_exceed 不在 Literal 三档 → ValidationError。"""
        ws_root = _make_workspace_dir(workspaces_root, "ws_bad_action")
        _write_project_yaml(
            ws_root, "cost_guard:\n  action_on_exceed: bogus\n"
        )
        with pytest.raises(ValidationError):
            service.load_cost_guard_config_for_workspace("ws_bad_action")

    # ---- gate(默认值 + 数据模型对账)----

    def test_cost_guard_config_defaults_match_cost_guard_module(self) -> None:
        """``CostGuardConfig()`` 默认对齐 OBSERVABILITY §3.4 字面默认值。"""
        defaults = CostGuardConfig()
        assert defaults.daily_usd_limit == 20.0
        assert defaults.monthly_usd_limit == 300.0
        assert defaults.per_workspace_daily_limit == 10.0
        assert defaults.action_on_exceed == "pause"
        assert defaults.notify_via == ["log", "websocket"]


class TestSaveCostGuardConfig:
    """``ConfigService.save_workspace_config`` cost_guard 段 5 类覆盖(P6-B-15)。"""

    # ---- happy ----

    def test_save_with_cost_guard_section_persists_and_returns_merged(
        self,
        engine_config_path: Path,
        workspaces_root: Path,
    ) -> None:
        """overrides 含 cost_guard 段 → yaml 落盘 + 重读 cost_guard 配置生效。"""
        _make_workspace_dir(workspaces_root, "ws_save_cost_guard")
        ws_manager = _FakeWSManager()
        service = _make_service_with_ws(
            engine_config_path, workspaces_root, ws_manager=ws_manager
        )

        service.save_workspace_config(
            "ws_save_cost_guard",
            {
                "cost_guard": {
                    "daily_usd_limit": 50.0,
                    "monthly_usd_limit": 600.0,
                    "per_workspace_daily_limit": 30.0,
                    "action_on_exceed": "warn",
                    "notify_via": ["log"],
                }
            },
        )

        # yaml 落盘含 cost_guard 段
        yaml_path = workspaces_root / "ws_save_cost_guard" / "project.yaml"
        content = yaml_path.read_text(encoding="utf-8")
        assert "cost_guard:" in content
        assert "daily_usd_limit: 50.0" in content
        assert "action_on_exceed: warn" in content

        # 重读 cost_guard 配置生效
        cfg = service.load_cost_guard_config_for_workspace(
            "ws_save_cost_guard"
        )
        assert cfg.daily_usd_limit == 50.0
        assert cfg.action_on_exceed == "warn"
        assert cfg.notify_via == ["log"]

        # WS event 发送
        assert len(ws_manager.events) == 1
        assert ws_manager.events[0]["event"] == "config_updated"

    def test_save_with_cost_guard_partial_field(
        self,
        engine_config_path: Path,
        workspaces_root: Path,
    ) -> None:
        """overrides.cost_guard 只填 action_on_exceed → 其余字段沿用引擎默认。"""
        _make_workspace_dir(workspaces_root, "ws_cost_guard_partial_save")
        service = _make_service_with_ws(engine_config_path, workspaces_root)

        service.save_workspace_config(
            "ws_cost_guard_partial_save",
            {"cost_guard": {"action_on_exceed": "continue"}},
        )
        cfg = service.load_cost_guard_config_for_workspace(
            "ws_cost_guard_partial_save"
        )
        assert cfg.action_on_exceed == "continue"
        assert cfg.daily_usd_limit == 20.0
        assert cfg.notify_via == ["log", "websocket"]

    def test_save_with_writer_and_cost_guard_together(
        self,
        engine_config_path: Path,
        workspaces_root: Path,
    ) -> None:
        """同时覆盖 writer + cost_guard 段 → 两段都落盘 + 各自生效。"""
        _make_workspace_dir(workspaces_root, "ws_combo_c")
        service = _make_service_with_ws(engine_config_path, workspaces_root)

        merged = service.save_workspace_config(
            "ws_combo_c",
            {
                "writer_routing": {"default": "cloud_quality"},
                "cost_guard": {"daily_usd_limit": 100.0},
            },
        )
        assert merged.routing.default == "cloud_quality"
        cfg = service.load_cost_guard_config_for_workspace("ws_combo_c")
        assert cfg.daily_usd_limit == 100.0

    # ---- error ----

    def test_save_cost_guard_section_non_dict_raises(
        self,
        engine_config_path: Path,
        workspaces_root: Path,
    ) -> None:
        """overrides.cost_guard 非 dict → ValueError + 不落盘。"""
        ws_root = _make_workspace_dir(workspaces_root, "ws_bad_section_c")
        service = _make_service_with_ws(engine_config_path, workspaces_root)
        with pytest.raises(ValueError, match="cost_guard 覆盖段应为 dict"):
            service.save_workspace_config(
                "ws_bad_section_c",
                {"cost_guard": ["daily_usd_limit", 20]},
            )
        assert not (ws_root / "project.yaml").exists()

    def test_save_cost_guard_invalid_action_raises(
        self,
        engine_config_path: Path,
        workspaces_root: Path,
    ) -> None:
        """action_on_exceed 非 Literal → ValidationError + 不落盘。"""
        ws_root = _make_workspace_dir(workspaces_root, "ws_bad_action_save")
        service = _make_service_with_ws(engine_config_path, workspaces_root)
        with pytest.raises(ValidationError):
            service.save_workspace_config(
                "ws_bad_action_save",
                {"cost_guard": {"action_on_exceed": "bogus"}},
            )
        assert not (ws_root / "project.yaml").exists()

    # ---- gate(None 视为不传该段) ----

    def test_save_cost_guard_none_value_skips_merge(
        self,
        engine_config_path: Path,
        workspaces_root: Path,
    ) -> None:
        """overrides.cost_guard=None → 等价于不传该段(跳过校验,不落 cost_guard)。"""
        ws_root = _make_workspace_dir(workspaces_root, "ws_none_section_c")
        service = _make_service_with_ws(engine_config_path, workspaces_root)
        service.save_workspace_config(
            "ws_none_section_c",
            {
                "cost_guard": None,
                "writer_routing": {"default": "cloud_quality"},
            },
        )
        content = (ws_root / "project.yaml").read_text(encoding="utf-8")
        assert "writer_routing" in content

    # ---- rollback ----

    def test_save_invalid_cost_guard_keeps_original_yaml(
        self,
        engine_config_path: Path,
        workspaces_root: Path,
    ) -> None:
        """cost_guard 校验失败 → 原 project.yaml 不动(rollback 锚点)。"""
        ws_root = _make_workspace_dir(
            workspaces_root, "ws_cost_guard_rollback"
        )
        original = "writer_routing:\n  default: cloud_premium\n"
        _write_project_yaml(ws_root, original)
        service = _make_service_with_ws(engine_config_path, workspaces_root)

        with pytest.raises(ValidationError):
            service.save_workspace_config(
                "ws_cost_guard_rollback",
                {"cost_guard": {"daily_usd_limit": -5.0}},
            )
        assert (
            (ws_root / "project.yaml").read_text(encoding="utf-8") == original
        )

    # ---- WS event ----

    def test_save_cost_guard_publishes_config_updated_event(
        self,
        engine_config_path: Path,
        workspaces_root: Path,
    ) -> None:
        """只改 cost_guard 段也走 config_updated event(沿用 P6-B-12 同事件)。"""
        _make_workspace_dir(workspaces_root, "ws_cost_guard_ws")
        ws_manager = _FakeWSManager()
        service = _make_service_with_ws(
            engine_config_path, workspaces_root, ws_manager=ws_manager
        )
        service.save_workspace_config(
            "ws_cost_guard_ws",
            {"cost_guard": {"daily_usd_limit": 15.0}},
        )
        assert len(ws_manager.events) == 1
        evt = ws_manager.events[0]
        assert evt["channel"] == "workspace.ws_cost_guard_ws.events"
        assert evt["event"] == "config_updated"


# =============================================================================
# v1.1 L1 Q16:generation 段测试(load_generation_config + save_generation_config)
# =============================================================================


class TestLoadGenerationConfig:
    """``ConfigService.load_generation_config`` 单元测试(v1.1 L1)。"""

    # ---- happy ----

    def test_engine_defaults_when_no_workspace_dir(
        self, service: ConfigService
    ) -> None:
        """工作区目录不存在 → 引擎 config.yaml generation 段(fixture 2200/3800/4)。"""
        cfg = service.load_generation_config("ws_not_exist")
        assert isinstance(cfg, GenerationConfig)
        assert cfg.word_count_min == 2200
        assert cfg.word_count_max == 3800
        assert cfg.max_rewrites == 4
        assert cfg.core_state_hard_enabled is True

    def test_core_state_hard_workspace_kill_switch_round_trip(
        self, service: ConfigService, workspaces_root: Path
    ) -> None:
        """核心状态 HARD 默认开启，工作区可显式关闭且不会被白名单丢弃。"""
        ws_root = _make_workspace_dir(workspaces_root, "ws_core_state_off")
        _write_project_yaml(
            ws_root, "generation:\n  core_state_hard_enabled: false\n"
        )
        cfg = service.load_generation_config("ws_core_state_off")
        assert cfg.core_state_hard_enabled is False
        assert cfg.word_count_min == 2200

    def test_engine_defaults_when_workspace_has_no_generation_section(
        self, service: ConfigService, workspaces_root: Path
    ) -> None:
        """工作区有 project.yaml 但无 generation 段 → 引擎默认。"""
        ws_root = _make_workspace_dir(workspaces_root, "ws_no_gen")
        _write_project_yaml(
            ws_root, "writer_routing:\n  default: cloud_quality\n"
        )
        cfg = service.load_generation_config("ws_no_gen")
        assert cfg.word_count_min == 2200
        assert cfg.word_count_max == 3800
        assert cfg.max_rewrites == 4

    def test_workspace_full_override(
        self, service: ConfigService, workspaces_root: Path
    ) -> None:
        """工作区 generation 段全字段覆盖 → 3 字段都生效。"""
        ws_root = _make_workspace_dir(workspaces_root, "ws_gen_full")
        _write_project_yaml(
            ws_root,
            "generation:\n"
            "  word_count_min: 1800\n"
            "  word_count_max: 3000\n"
            "  max_rewrites: 5\n",
        )
        cfg = service.load_generation_config("ws_gen_full")
        assert cfg.word_count_min == 1800
        assert cfg.word_count_max == 3000
        assert cfg.max_rewrites == 5

    def test_workspace_partial_override_merges_field_level(
        self, service: ConfigService, workspaces_root: Path
    ) -> None:
        """工作区 generation 段只填部分字段 → 字段级合并(未填的保留引擎默认)。"""
        ws_root = _make_workspace_dir(workspaces_root, "ws_gen_partial")
        _write_project_yaml(
            ws_root, "generation:\n  word_count_min: 2000\n"
        )
        cfg = service.load_generation_config("ws_gen_partial")
        # 工作区覆盖
        assert cfg.word_count_min == 2000
        # 引擎默认保留
        assert cfg.word_count_max == 3800
        assert cfg.max_rewrites == 4

    def test_legacy_batch_granularity_override_is_ignored(
        self, service: ConfigService, workspaces_root: Path
    ) -> None:
        """旧工作区可只读打开，但退役字段不再恢复任何批量能力。"""
        ws_root = _make_workspace_dir(workspaces_root, "ws_bg_legacy")
        _write_project_yaml(
            ws_root,
            "generation:\n"
            "  batch_granularity: beat\n"
            "  word_count_min: 2000\n",
        )
        cfg = service.load_generation_config("ws_bg_legacy")
        assert "batch_granularity" not in cfg.model_dump()
        assert cfg.word_count_min == 2000

    # ---- anti_tic_trim_enabled(v2 W3.5 wave-3,工作区级 UI 开关)----

    def test_anti_tic_trim_engine_default_off(
        self, service: ConfigService, workspaces_root: Path
    ) -> None:
        """无工作区覆盖 → anti_tic_trim_enabled 引擎默认 False(零回归)。"""
        ws_root = _make_workspace_dir(workspaces_root, "ws_att_default")
        _write_project_yaml(ws_root, "generation:\n  word_count_min: 2000\n")
        cfg = service.load_generation_config("ws_att_default")
        assert cfg.anti_tic_trim_enabled is False

    def test_anti_tic_trim_workspace_override_on(
        self, service: ConfigService, workspaces_root: Path
    ) -> None:
        """工作区覆盖 anti_tic_trim_enabled: true → 生效(字段级合并白名单含本字段)。

        防回退:本字段必须在 ``_merge_generation`` 白名单内,否则 project.yaml / UI
        写入读回被静默丢弃(同 w5-s10 progression/breakthrough 漏登旧坑 → UI 开关无效)。
        """
        ws_root = _make_workspace_dir(workspaces_root, "ws_att_on")
        _write_project_yaml(
            ws_root, "generation:\n  anti_tic_trim_enabled: true\n"
        )
        cfg = service.load_generation_config("ws_att_on")
        assert cfg.anti_tic_trim_enabled is True
        # 其余字段保留引擎默认(字段级合并未被冲掉)
        assert cfg.word_count_min == 2200

    # ---- error ----

    def test_workspace_yaml_top_non_dict_raises(
        self, service: ConfigService, workspaces_root: Path
    ) -> None:
        """工作区 project.yaml 顶层非 dict → ValueError。"""
        ws_root = _make_workspace_dir(workspaces_root, "ws_gen_top_bad")
        _write_project_yaml(ws_root, "- a\n- b\n")
        with pytest.raises(ValueError, match="project.yaml 顶层应为映射"):
            service.load_generation_config("ws_gen_top_bad")

    def test_workspace_generation_section_non_dict_raises(
        self, service: ConfigService, workspaces_root: Path
    ) -> None:
        """工作区 generation 段非 dict → ValueError。"""
        ws_root = _make_workspace_dir(workspaces_root, "ws_gen_section_bad")
        _write_project_yaml(ws_root, "generation:\n  - a\n  - b\n")
        with pytest.raises(ValueError, match="generation 段应为 dict"):
            service.load_generation_config("ws_gen_section_bad")

    def test_workspace_invalid_word_count_ordering_raises(
        self, service: ConfigService, workspaces_root: Path
    ) -> None:
        """word_count_min > word_count_max 触发交叉校验 → ValidationError。"""
        ws_root = _make_workspace_dir(workspaces_root, "ws_gen_bad_order")
        _write_project_yaml(
            ws_root,
            "generation:\n  word_count_min: 5000\n  word_count_max: 3000\n",
        )
        with pytest.raises(ValidationError):
            service.load_generation_config("ws_gen_bad_order")


class TestSaveGenerationConfig:
    """``ConfigService.save_generation_config`` 单元测试(v1.1 L1)。"""

    def test_save_creates_generation_section_and_returns_merged(
        self, engine_config_path: Path, workspaces_root: Path
    ) -> None:
        """工作区无 project.yaml → 写 generation 段 + 返合并后配置。"""
        _make_workspace_dir(workspaces_root, "ws_gen_save")
        ws_manager = _FakeWSManager()
        service = _make_service_with_ws(
            engine_config_path, workspaces_root, ws_manager=ws_manager
        )
        merged = service.save_generation_config(
            "ws_gen_save",
            {"word_count_min": 2000, "word_count_max": 3500, "max_rewrites": 2},
        )
        assert merged.word_count_min == 2000
        assert merged.word_count_max == 3500
        assert merged.max_rewrites == 2
        yaml_path = workspaces_root / "ws_gen_save" / "project.yaml"
        assert yaml_path.is_file()
        assert "generation:" in yaml_path.read_text(encoding="utf-8")
        # 走 config_updated event
        assert len(ws_manager.events) == 1
        assert ws_manager.events[0]["event"] == "config_updated"

    def test_save_core_state_hard_kill_switch(
        self, engine_config_path: Path, workspaces_root: Path
    ) -> None:
        """专属保存路径可关闭总开关并原样读回。"""
        _make_workspace_dir(workspaces_root, "ws_core_state_save_off")
        service = _make_service_with_ws(engine_config_path, workspaces_root)
        merged = service.save_generation_config(
            "ws_core_state_save_off", {"core_state_hard_enabled": False}
        )
        assert merged.core_state_hard_enabled is False
        assert (
            service.load_generation_config(
                "ws_core_state_save_off"
            ).core_state_hard_enabled
            is False
        )

    def test_save_preserves_other_sections(
        self, engine_config_path: Path, workspaces_root: Path
    ) -> None:
        """只改 generation 段不冲掉已有 reviewer / writer_routing 覆盖(L1 核心保证)。"""
        ws_root = _make_workspace_dir(workspaces_root, "ws_gen_preserve")
        _write_project_yaml(
            ws_root,
            "writer_routing:\n  default: cloud_quality\n"
            "reviewer:\n  pass_threshold: 9.0\n",
        )
        service = _make_service_with_ws(engine_config_path, workspaces_root)
        service.save_generation_config(
            "ws_gen_preserve", {"word_count_min": 2100}
        )
        # generation 段生效
        gen = service.load_generation_config("ws_gen_preserve")
        assert gen.word_count_min == 2100
        # 既有 reviewer / writer_routing 覆盖仍在
        reviewer = service.load_reviewer_config("ws_gen_preserve")
        assert reviewer.pass_threshold == 9.0
        merged = service.load_workspace_config("ws_gen_preserve")
        assert merged.routing.default == "cloud_quality"

    def test_save_invalid_ordering_raises_and_leaves_yaml_untouched(
        self, engine_config_path: Path, workspaces_root: Path
    ) -> None:
        """word_count_min > word_count_max → ValidationError + 原 project.yaml 不动。"""
        ws_root = _make_workspace_dir(workspaces_root, "ws_gen_save_bad")
        _write_project_yaml(ws_root, "reviewer:\n  pass_threshold: 8.0\n")
        original = (ws_root / "project.yaml").read_text(encoding="utf-8")
        service = _make_service_with_ws(engine_config_path, workspaces_root)
        with pytest.raises(ValidationError):
            service.save_generation_config(
                "ws_gen_save_bad",
                {"word_count_min": 5000, "word_count_max": 3000},
            )
        # 原文件未被破坏性写入
        assert (ws_root / "project.yaml").read_text(encoding="utf-8") == original

    def test_save_workspace_missing_raises_file_not_found(
        self, engine_config_path: Path, workspaces_root: Path
    ) -> None:
        """工作区目录不存在 → FileNotFoundError(路由翻 404)。"""
        service = _make_service_with_ws(engine_config_path, workspaces_root)
        with pytest.raises(FileNotFoundError):
            service.save_generation_config(
                "ws_gen_missing", {"word_count_min": 2000}
            )


# ===== W3.5 校准联动:trim 三态(override + 校准默认 + 生效值)全链路 =====

# 固定 deepseek engine config(writer→deepseek + overshoot/high 校准),测三态逻辑
# 不依赖真实 config.yaml 当前 routing 状态(稳定);真实 config.yaml 接线另见
# test_model_calibration.test_repo_config_yaml_has_deepseek_calibration。
_DEEPSEEK_ENGINE_YAML = """\
writer_profiles:
  cloud_deepseek_v4_pro:
    provider: openai_compatible
    model: deepseek-v4-pro
    base_url: https://dashscope.aliyuncs.com/compatible-mode/v1
    context_window: 1000000
    generation_params: {temperature: 0.85, max_tokens: 8000}
    extra_request_kwargs: {enable_thinking: false}
    cost_per_1k_input: 0.00042
    cost_per_1k_output: 0.00083

writer_routing:
  default: cloud_deepseek_v4_pro
  agent_routing:
    writer: cloud_deepseek_v4_pro

model_calibration:
  deepseek-v4-pro:
    calibrated_at: "2026-06-16"
    calibration_corpus: "test fixture"
    global:
      length_tendency: overshoot
      anti_stamp_strength: high
      recommended_temperature: 0.85
      thinking_default: false
      reviewer_threshold_adjustment: 0.0
    per_genre:
      introspection:
        chars_per_beat: 1534
        chars_per_chapter: 4602
        length_compliance: 0.667
        content_units_per_chapter: 3
        sample_chapters: 6

generation:
  word_count_min: 2500
  word_count_max: 4500
"""


class TestTrimCalibrationTristateW35:
    """W3.5 校准联动:word_count_trim / anti_tic_trim 三态(校准默认 + 工作区覆盖)。

    全链路(对齐 [[feedback_test_full_wiring_not_just_mechanism_ends]]):
    ``load_generation_override``(原始三态)→ ``_resolve_trim_calibration_defaults``
    (校准默认)→ ``load_generation_config_view``(生效值 = override ?? 校准默认)。
    """

    @pytest.fixture
    def deepseek_service(
        self, tmp_path: Path, workspaces_root: Path
    ) -> ConfigService:
        """engine config = 固定 deepseek(writer→deepseek + overshoot/high 校准)。"""
        p = tmp_path / "deepseek_config.yaml"
        p.write_text(_DEEPSEEK_ENGINE_YAML, encoding="utf-8")
        return ConfigService(
            engine_config_path=p, workspaces_root=workspaces_root
        )

    # ---- load_generation_override:原始三态(None / True / False)----

    def test_override_empty_when_no_project_yaml(
        self, service: ConfigService, workspaces_root: Path
    ) -> None:
        """无 project.yaml → 空 dict(消费方退校准默认)。"""
        _make_workspace_dir(workspaces_root, "ws_ov0")
        assert service.load_generation_override("ws_ov0") == {}

    def test_override_reads_explicit_true_false(
        self, service: ConfigService, workspaces_root: Path
    ) -> None:
        """project.yaml 显式 true/false → 原样读回(三态显式态,不塌成引擎默认)。"""
        ws = _make_workspace_dir(workspaces_root, "ws_ov1")
        _write_project_yaml(
            ws,
            "generation:\n"
            "  word_count_trim_enabled: true\n"
            "  anti_tic_trim_enabled: false\n",
        )
        ov = service.load_generation_override("ws_ov1")
        assert ov.get("word_count_trim_enabled") is True
        assert ov.get("anti_tic_trim_enabled") is False

    def test_override_omitted_field_is_none(
        self, service: ConfigService, workspaces_root: Path
    ) -> None:
        """generation 段有但 trim 字段省略 → .get 返 None(跟随校准态)。"""
        ws = _make_workspace_dir(workspaces_root, "ws_ov2")
        _write_project_yaml(ws, "generation:\n  word_count_max: 4500\n")
        ov = service.load_generation_override("ws_ov2")
        assert ov.get("word_count_trim_enabled") is None
        assert ov.get("anti_tic_trim_enabled") is None

    def test_override_malformed_returns_empty(
        self, service: ConfigService, workspaces_root: Path
    ) -> None:
        """generation 段非 dict → 空 dict(优雅退化,不抛、退校准默认关,不阻断生成)。"""
        ws = _make_workspace_dir(workspaces_root, "ws_ov3")
        _write_project_yaml(ws, "generation: not_a_dict\n")
        assert service.load_generation_override("ws_ov3") == {}

    # ---- _resolve_trim_calibration_defaults:writer 模型校准默认 ----

    def test_calibration_defaults_deepseek_both_on(
        self, deepseek_service: ConfigService, workspaces_root: Path
    ) -> None:
        """deepseek writer(overshoot/high)→ 两 trim 校准默认开。"""
        _make_workspace_dir(workspaces_root, "ws_ds")
        assert deepseek_service._resolve_trim_calibration_defaults("ws_ds") == {
            "word_count_trim": True,
            "anti_tic_trim": True,
        }

    def test_calibration_defaults_qwen_no_calibration_both_off(
        self, service: ConfigService, workspaces_root: Path
    ) -> None:
        """_ENGINE_YAML(writer→qwen-novel,无 calibration 段)→ 两 trim 校准默认关。"""
        _make_workspace_dir(workspaces_root, "ws_q")
        assert service._resolve_trim_calibration_defaults("ws_q") == {
            "word_count_trim": False,
            "anti_tic_trim": False,
        }

    # ---- load_generation_config_view:生效值 = override ?? 校准默认 ----

    def test_view_no_override_follows_calibration_deepseek(
        self, deepseek_service: ConfigService, workspaces_root: Path
    ) -> None:
        """deepseek + 无 override → enabled 生效 = 校准默认 True;override=None。"""
        _make_workspace_dir(workspaces_root, "ws_v1")
        view = deepseek_service.load_generation_config_view("ws_v1")
        assert view["word_count_trim_override"] is None
        assert view["word_count_trim_calibration_default"] is True
        assert view["word_count_trim_enabled"] is True  # 生效 = 校准默认
        assert view["anti_tic_trim_override"] is None
        assert view["anti_tic_trim_calibration_default"] is True
        assert view["anti_tic_trim_enabled"] is True

    def test_view_explicit_false_overrides_calibration(
        self, deepseek_service: ConfigService, workspaces_root: Path
    ) -> None:
        """工作区显式 False 覆盖 deepseek 校准 True → 生效 False(三态 override 优先)。"""
        ws = _make_workspace_dir(workspaces_root, "ws_v2")
        _write_project_yaml(
            ws, "generation:\n  word_count_trim_enabled: false\n"
        )
        view = deepseek_service.load_generation_config_view("ws_v2")
        assert view["word_count_trim_override"] is False
        assert view["word_count_trim_calibration_default"] is True  # 校准仍 True
        assert view["word_count_trim_enabled"] is False  # override 赢 → 生效 False

    def test_view_explicit_true_when_calibration_off(
        self, service: ConfigService, workspaces_root: Path
    ) -> None:
        """qwen(校准默认关)+ 工作区显式 True → 生效 True(override 优先,强制开)。"""
        ws = _make_workspace_dir(workspaces_root, "ws_v3")
        _write_project_yaml(ws, "generation:\n  anti_tic_trim_enabled: true\n")
        view = service.load_generation_config_view("ws_v3")
        assert view["anti_tic_trim_override"] is True
        assert view["anti_tic_trim_calibration_default"] is False  # qwen 无校准
        assert view["anti_tic_trim_enabled"] is True  # override 赢 → 生效 True
