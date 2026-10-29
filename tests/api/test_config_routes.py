"""配置路由集成测试(P3-B-10 GET,P6-B-12 扩 PUT,P6-B-13 reviewer,P6-B-14 retrieval,P6-B-15 cost_guard)。

覆盖 ``GET / PUT /api/v1/workspaces/{workspace_id}/config`` 主路径与错误路径。

测试矩阵
========

主路径(GET — P3-B-10)
------------------------
- 工作区目录不存在 → 200 引擎默认配置(回退语义,对齐 P3-B-02)
- 工作区目录存在但无 ``project.yaml`` → 200 引擎默认配置
- 工作区有 ``project.yaml`` 覆盖 ``writer_routing.default`` → 200 合并后配置
- 工作区 ``project.yaml`` 覆盖单个 ``writer_profiles.<key>`` → 200 dict-merge 结果
- profile 全字段(provider / model / base_url / context_window / generation_params /
  extra_request_kwargs / cost_per_1k_input/output / notes)正确暴露
- routing.rules + agent_routing 正确暴露

错误路径(GET — P3-B-10)
-------------------------
- ``project.yaml`` 顶层非 dict → 422 ``CONFIG_YAML_INVALID``
- 合并后 routing.default 引用未定义 profile → 422 ``CONFIG_YAML_INVALID``
- 引擎默认 ``config.yaml`` 不存在 → 500 ``CONFIG_YAML_NOT_FOUND``

PUT 端点(P6-B-12)
-------------------
- happy:覆盖 default → 200 + yaml 落盘 + 返合并视图
- happy:新增自定义 profile + agent_routing 覆盖 → 200 + 合并字段对账
- error:body 字段 invalid(routing.default 引用未定义) → 422 CONFIG_YAML_INVALID
- error:工作区目录不存在 → 404 WORKSPACE_NOT_FOUND
- error:body 含 extra 字段 → 422 VALIDATION_FAILED(Pydantic extra forbid)
- gate:body 含空 dict → 等价"清空覆盖" 200
- gate:覆盖内置 profile 整体字段 → 200(自决项 — 内置不锁字段覆盖)
- rollback:body 校验失败时原 project.yaml 不动

reviewer 段(P6-B-13)
----------------------
- GET 响应 ``data.reviewer`` 暴露 4 字段(无工作区覆盖时为引擎默认)
- GET 字段级合并(工作区只填部分字段 → 其余沿用引擎默认)
- PUT 含 reviewer 段 → 200 + yaml 落盘 reviewer + GET 字段生效
- PUT reviewer 字段交叉校验失败 → 422 CONFIG_YAML_INVALID

retrieval 段(P6-B-14)
----------------------
- GET 响应 ``data.retrieval`` 暴露 2 字段(无工作区覆盖时为引擎默认)
- PUT 含 retrieval 段 → 200 + yaml 落盘 retrieval + GET 字段生效
- PUT retrieval 字段越界(top_k <= 0) → 422 CONFIG_YAML_INVALID
- PUT retrieval extra 字段 → 422 VALIDATION_FAILED

cost_guard 段(P6-B-15)
------------------------
- GET 响应 ``data.cost_guard`` 暴露 5 字段(无工作区覆盖时为引擎默认)
- GET 字段级合并(工作区只填部分字段 → 其余沿用引擎默认)
- PUT 含 cost_guard 段 → 200 + yaml 落盘 cost_guard + GET 字段生效
- PUT cost_guard 字段越界(daily_usd_limit < 0) → 422 VALIDATION_FAILED
- PUT cost_guard action_on_exceed 非 Literal → 422 VALIDATION_FAILED
- PUT cost_guard extra 字段 → 422 VALIDATION_FAILED

测试基础设施
============
- 复用 P3-B-02 已就位的 ``ConfigService(engine_config_path, workspaces_root)``
  注入路径,通过 ``app.dependency_overrides`` 替换 ``get_config_service``。
- ``create_app()`` 工厂保证中间件栈与生产一致(对齐 ``test_workspaces_routes`` 模式)。
"""

from __future__ import annotations

from collections.abc import Iterator
from pathlib import Path

import pytest
from fastapi.testclient import TestClient

from src.api.main import create_app
from src.api.routes.config import get_config_service
from src.services.config_service import ConfigService

# 引擎默认 yaml(对齐 ``tests/services/test_config_service.py:_ENGINE_YAML``)。
_ENGINE_YAML = """\
writer_profiles:
  local_qwen_32k:
    provider: ollama
    model: qwen-novel
    base_url: http://localhost:11434
    context_window: 24576
    generation_params: {temperature: 0.7}
    extra_request_kwargs: {think: false}
    notes: 本地 Qwen 32k 默认
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
  word_count_min: 2500
  word_count_max: 4000
  max_rewrites: 3
"""


# =============================================================================
# Fixtures
# =============================================================================


@pytest.fixture()
def workspaces_root(tmp_path: Path) -> Path:
    root = tmp_path / "workspaces"
    root.mkdir()
    return root


@pytest.fixture()
def engine_config_path(tmp_path: Path) -> Path:
    p = tmp_path / "config.yaml"
    p.write_text(_ENGINE_YAML, encoding="utf-8")
    return p


@pytest.fixture()
def config_service(
    engine_config_path: Path, workspaces_root: Path
) -> ConfigService:
    return ConfigService(
        engine_config_path=engine_config_path,
        workspaces_root=workspaces_root,
    )


@pytest.fixture()
def client(config_service: ConfigService) -> Iterator[TestClient]:
    app = create_app()
    app.dependency_overrides[get_config_service] = lambda: config_service
    with TestClient(app) as c:
        yield c
    app.dependency_overrides.clear()


def _make_workspace_dir(workspaces_root: Path, workspace_id: str) -> Path:
    ws_root = workspaces_root / workspace_id
    ws_root.mkdir()
    return ws_root


def _write_project_yaml(ws_root: Path, content: str) -> None:
    (ws_root / "project.yaml").write_text(content, encoding="utf-8")


# =============================================================================
# 主路径
# =============================================================================


def test_workspace_dir_missing_returns_engine_defaults(
    client: TestClient,
) -> None:
    """工作区目录不存在 → 200 引擎默认(回退语义)。"""
    resp = client.get("/api/v1/workspaces/not_exist/config")
    assert resp.status_code == 200
    body = resp.json()
    assert "data" in body and "error" not in body
    data = body["data"]
    assert data["workspace_id"] == "not_exist"
    assert sorted(data["profiles"].keys()) == [
        "cloud_premium",
        "cloud_quality",
        "local_qwen_32k",
    ]
    assert data["routing"]["default"] == "local_qwen_32k"


def test_project_yaml_missing_returns_engine_defaults(
    client: TestClient, workspaces_root: Path
) -> None:
    """工作区目录存在但无 project.yaml → 200 引擎默认。"""
    _make_workspace_dir(workspaces_root, "ws_no_override")
    resp = client.get("/api/v1/workspaces/ws_no_override/config")
    assert resp.status_code == 200
    data = resp.json()["data"]
    assert data["routing"]["default"] == "local_qwen_32k"
    assert "cloud_quality" in data["profiles"]


def test_workspace_override_default_routes_correctly(
    client: TestClient, workspaces_root: Path
) -> None:
    """``project.yaml`` 覆盖 ``writer_routing.default`` → 合并后 default 生效。"""
    ws_root = _make_workspace_dir(workspaces_root, "ws_override")
    _write_project_yaml(
        ws_root,
        "writer_routing:\n  default: cloud_quality\n",
    )
    resp = client.get("/api/v1/workspaces/ws_override/config")
    assert resp.status_code == 200
    data = resp.json()["data"]
    assert data["routing"]["default"] == "cloud_quality"
    # 其他段保留引擎默认
    assert sorted(data["profiles"].keys()) == [
        "cloud_premium",
        "cloud_quality",
        "local_qwen_32k",
    ]
    assert data["routing"]["rules"][0]["condition"] == "is_arc_finale == True"
    assert data["routing"]["rules"][0]["profile"] == "cloud_premium"


def test_writer_profile_full_fields_exposed(client: TestClient) -> None:
    """profile 全字段:provider / model / base_url / context_window /
    generation_params / extra_request_kwargs / cost_per_1k_* / notes 全部暴露。"""
    resp = client.get("/api/v1/workspaces/any_ws/config")
    assert resp.status_code == 200
    profile = resp.json()["data"]["profiles"]["local_qwen_32k"]
    assert profile["profile_id"] == "local_qwen_32k"
    assert profile["provider"] == "ollama"
    assert profile["model"] == "qwen-novel"
    assert profile["base_url"] == "http://localhost:11434"
    assert profile["context_window"] == 24576
    assert profile["generation_params"] == {"temperature": 0.7}
    assert profile["extra_request_kwargs"] == {"think": False}
    assert profile["cost_per_1k_input"] == 0.0
    assert profile["cost_per_1k_output"] == 0.0
    assert profile["notes"] == "本地 Qwen 32k 默认"


def test_routing_full_fields_exposed(client: TestClient) -> None:
    """routing 全字段:default / rules / agent_routing 全部暴露。"""
    resp = client.get("/api/v1/workspaces/any_ws/config")
    assert resp.status_code == 200
    routing = resp.json()["data"]["routing"]
    assert routing["default"] == "local_qwen_32k"
    assert len(routing["rules"]) == 1
    assert routing["rules"][0] == {
        "condition": "is_arc_finale == True",
        "profile": "cloud_premium",
    }
    assert routing["agent_routing"] == {
        "writer": "local_qwen_32k",
        "reviewer": "local_qwen_32k",
        "memory_extractor": "local_qwen_32k",
    }


def test_workspace_override_single_profile_replaces_only_that_key(
    client: TestClient, workspaces_root: Path
) -> None:
    """工作区单个 profile 整体替换,其他保留(P3-B-02 dict-merge)。"""
    ws_root = _make_workspace_dir(workspaces_root, "ws_replace_qwen")
    _write_project_yaml(
        ws_root,
        """\
writer_profiles:
  local_qwen_32k:
    provider: ollama
    model: qwen2.5-32b-novel
    base_url: http://localhost:11434
    context_window: 32768
    generation_params: {temperature: 0.6}
    extra_request_kwargs: {think: false}
""",
    )
    resp = client.get("/api/v1/workspaces/ws_replace_qwen/config")
    assert resp.status_code == 200
    profiles = resp.json()["data"]["profiles"]
    # 替换的 profile
    assert profiles["local_qwen_32k"]["model"] == "qwen2.5-32b-novel"
    assert profiles["local_qwen_32k"]["context_window"] == 32768
    # 其他 profile 保留引擎默认
    assert profiles["cloud_quality"]["model"] == "claude-sonnet-4-6"
    assert profiles["cloud_premium"]["model"] == "claude-opus-4-7"


# =============================================================================
# 错误路径
# =============================================================================


def test_project_yaml_top_level_non_dict_returns_422(
    client: TestClient, workspaces_root: Path
) -> None:
    """``project.yaml`` 顶层非 dict → 422 CONFIG_YAML_INVALID。"""
    ws_root = _make_workspace_dir(workspaces_root, "ws_bad_yaml")
    _write_project_yaml(ws_root, "- not_a_dict\n")
    resp = client.get("/api/v1/workspaces/ws_bad_yaml/config")
    assert resp.status_code == 422
    body = resp.json()
    assert "error" in body and "data" not in body
    assert body["error"]["code"] == "CONFIG_YAML_INVALID"


def test_project_yaml_routing_default_unknown_profile_returns_422(
    client: TestClient, workspaces_root: Path
) -> None:
    """合并后 routing.default 引用未定义 profile → 422 CONFIG_YAML_INVALID。"""
    ws_root = _make_workspace_dir(workspaces_root, "ws_unknown_default")
    _write_project_yaml(
        ws_root,
        "writer_routing:\n  default: nonexistent_profile\n",
    )
    resp = client.get("/api/v1/workspaces/ws_unknown_default/config")
    assert resp.status_code == 422
    assert resp.json()["error"]["code"] == "CONFIG_YAML_INVALID"


def test_engine_config_yaml_missing_returns_500(
    tmp_path: Path, workspaces_root: Path
) -> None:
    """引擎默认 config.yaml 不存在 → 500 CONFIG_YAML_NOT_FOUND。"""
    missing_engine = tmp_path / "no_such_config.yaml"
    service = ConfigService(
        engine_config_path=missing_engine,
        workspaces_root=workspaces_root,
    )
    app = create_app()
    app.dependency_overrides[get_config_service] = lambda: service
    with TestClient(app) as c:
        resp = c.get("/api/v1/workspaces/any/config")
    app.dependency_overrides.clear()
    assert resp.status_code == 500
    assert resp.json()["error"]["code"] == "CONFIG_YAML_NOT_FOUND"


# =============================================================================
# PUT 端点(P6-B-12)
# =============================================================================


class TestUpdateWorkspaceConfig:
    """``PUT /api/v1/workspaces/{workspace_id}/config`` 集成测试。"""

    def test_put_overrides_default_persists_and_returns_merged(
        self, client: TestClient, workspaces_root: Path
    ) -> None:
        """PUT 覆盖 default → 200 + yaml 落盘 + 返合并视图(default 切换)。"""
        _make_workspace_dir(workspaces_root, "ws_put_default")

        resp = client.put(
            "/api/v1/workspaces/ws_put_default/config",
            json={"writer_routing": {"default": "cloud_quality"}},
        )

        assert resp.status_code == 200, resp.text
        data = resp.json()["data"]
        assert data["routing"]["default"] == "cloud_quality"
        # yaml 落盘
        yaml_file = workspaces_root / "ws_put_default" / "project.yaml"
        assert yaml_file.is_file()
        content = yaml_file.read_text(encoding="utf-8")
        assert "cloud_quality" in content

    def test_put_adds_custom_profile_and_routes_to_it(
        self, client: TestClient, workspaces_root: Path
    ) -> None:
        """PUT 新增自定义 profile + 改 default 指向 → 200 + 4 profile 合并。"""
        _make_workspace_dir(workspaces_root, "ws_put_custom")

        resp = client.put(
            "/api/v1/workspaces/ws_put_custom/config",
            json={
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
                "writer_routing": {
                    "default": "qwen_creative",
                    "agent_routing": {"writer": "qwen_creative"},
                },
            },
        )

        assert resp.status_code == 200, resp.text
        data = resp.json()["data"]
        # 4 profile(引擎 3 + 自定义 1)
        assert "qwen_creative" in data["profiles"]
        assert data["profiles"]["qwen_creative"]["model"] == "qwen-creative"
        assert data["routing"]["default"] == "qwen_creative"
        assert data["routing"]["agent_routing"]["writer"] == "qwen_creative"

    def test_put_openai_compatible_profile_accepted(
        self, client: TestClient, workspaces_root: Path
    ) -> None:
        """PUT openai_compatible profile(含 base_url)→ 200(v1.1 wave 3 H4
        补枚举后 UI 可建 dashscope / DeepSeek 等兼容 provider profile)。"""
        _make_workspace_dir(workspaces_root, "ws_put_oai")

        resp = client.put(
            "/api/v1/workspaces/ws_put_oai/config",
            json={
                "writer_profiles": {
                    "cloud_deepseek": {
                        "provider": "openai_compatible",
                        "model": "deepseek-chat",
                        "base_url": "https://api.deepseek.com/v1",
                        "context_window": 64000,
                        "cost_currency": "RMB",
                    }
                },
            },
        )

        assert resp.status_code == 200, resp.text
        data = resp.json()["data"]
        assert data["profiles"]["cloud_deepseek"]["provider"] == "openai_compatible"
        assert (
            data["profiles"]["cloud_deepseek"]["base_url"]
            == "https://api.deepseek.com/v1"
        )
        assert data["profiles"]["cloud_deepseek"]["cost_currency"] == "RMB"

    def test_put_openai_compatible_without_base_url_returns_422(
        self, client: TestClient, workspaces_root: Path
    ) -> None:
        """openai_compatible profile 缺 base_url → 422 VALIDATION_FAILED
        (WriterProfileIn 镜像核心模型 base_url 必填校验)。"""
        _make_workspace_dir(workspaces_root, "ws_put_oai_nobase")

        resp = client.put(
            "/api/v1/workspaces/ws_put_oai_nobase/config",
            json={
                "writer_profiles": {
                    "cloud_deepseek": {
                        "provider": "openai_compatible",
                        "model": "deepseek-chat",
                        "context_window": 64000,
                    }
                },
            },
        )

        assert resp.status_code == 422
        assert resp.json()["error"]["code"] == "VALIDATION_FAILED"

    @pytest.mark.parametrize(
        "base_url",
        [
            "https://user@example.invalid/v1",
            "https://example.invalid/v1?query=1",
            "http://api.deepseek.com/v1",
            "https://api.deepseek.com:8443/v1",
        ],
    )
    def test_put_openai_compatible_ambiguous_base_url_returns_422(
        self,
        client: TestClient,
        workspaces_root: Path,
        base_url: str,
    ) -> None:
        _make_workspace_dir(workspaces_root, "ws_put_oai_badbase")

        resp = client.put(
            "/api/v1/workspaces/ws_put_oai_badbase/config",
            json={
                "writer_profiles": {
                    "custom": {
                        "provider": "openai_compatible",
                        "model": "custom-model",
                        "base_url": base_url,
                        "context_window": 64000,
                    }
                },
            },
        )

        assert resp.status_code == 422
        assert resp.json()["error"]["code"] == "VALIDATION_FAILED"

    def test_put_invalid_default_returns_422(
        self, client: TestClient, workspaces_root: Path
    ) -> None:
        """body routing.default 引用未定义 profile → 422 CONFIG_YAML_INVALID。"""
        _make_workspace_dir(workspaces_root, "ws_put_invalid")

        resp = client.put(
            "/api/v1/workspaces/ws_put_invalid/config",
            json={"writer_routing": {"default": "nonexistent_profile"}},
        )

        assert resp.status_code == 422
        assert resp.json()["error"]["code"] == "CONFIG_YAML_INVALID"

    def test_put_workspace_not_found_returns_404(
        self, client: TestClient
    ) -> None:
        """工作区目录不存在 → 404 WORKSPACE_NOT_FOUND。"""
        resp = client.put(
            "/api/v1/workspaces/ws_missing_for_put/config",
            json={"writer_routing": {"default": "cloud_quality"}},
        )

        assert resp.status_code == 404
        assert resp.json()["error"]["code"] == "WORKSPACE_NOT_FOUND"

    def test_put_body_extra_field_returns_422(
        self, client: TestClient, workspaces_root: Path
    ) -> None:
        """body 含 schema 未声明字段 → 422 VALIDATION_FAILED(extra=forbid)。"""
        _make_workspace_dir(workspaces_root, "ws_put_extra")

        resp = client.put(
            "/api/v1/workspaces/ws_put_extra/config",
            json={"unknown_top_level_field": {}},
        )

        assert resp.status_code == 422
        assert resp.json()["error"]["code"] == "VALIDATION_FAILED"

    def test_put_empty_body_clears_overrides(
        self, client: TestClient, workspaces_root: Path
    ) -> None:
        """body 空 dict → 等价"清空工作区覆盖" 200 + 合并回归引擎默认。"""
        ws_root = _make_workspace_dir(workspaces_root, "ws_put_empty")
        _write_project_yaml(
            ws_root, "writer_routing:\n  default: cloud_premium\n"
        )

        resp = client.put(
            "/api/v1/workspaces/ws_put_empty/config", json={}
        )

        assert resp.status_code == 200
        data = resp.json()["data"]
        # 回归引擎默认
        assert data["routing"]["default"] == "local_qwen_32k"

    def test_put_rollback_keeps_original_yaml_on_validation_failure(
        self, client: TestClient, workspaces_root: Path
    ) -> None:
        """body 校验失败时原 project.yaml 内容不变。"""
        ws_root = _make_workspace_dir(workspaces_root, "ws_put_rollback")
        original = "writer_routing:\n  default: cloud_premium\n"
        _write_project_yaml(ws_root, original)

        resp = client.put(
            "/api/v1/workspaces/ws_put_rollback/config",
            json={"writer_routing": {"default": "nonexistent"}},
        )

        assert resp.status_code == 422
        # 原文件未改
        assert (
            (ws_root / "project.yaml").read_text(encoding="utf-8") == original
        )

    def test_put_overrides_builtin_profile_field_succeeds(
        self, client: TestClient, workspaces_root: Path
    ) -> None:
        """工作区覆盖内置 profile 整体字段 → 200(自决项:内置不锁字段覆盖)。"""
        _make_workspace_dir(workspaces_root, "ws_put_builtin")

        resp = client.put(
            "/api/v1/workspaces/ws_put_builtin/config",
            json={
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

        assert resp.status_code == 200
        cq = resp.json()["data"]["profiles"]["cloud_quality"]
        assert cq["generation_params"]["temperature"] == 0.95
        assert cq["notes"] == "工作区覆盖"

    def test_put_writes_yaml_backup_when_overwriting(
        self, client: TestClient, workspaces_root: Path
    ) -> None:
        """已有 project.yaml → 备份到 .bak + 写新内容。"""
        ws_root = _make_workspace_dir(workspaces_root, "ws_put_bak")
        original = "writer_routing:\n  default: cloud_premium\n"
        _write_project_yaml(ws_root, original)

        resp = client.put(
            "/api/v1/workspaces/ws_put_bak/config",
            json={"writer_routing": {"default": "cloud_quality"}},
        )

        assert resp.status_code == 200
        # .bak 留存原内容
        bak = ws_root / "project.yaml.bak"
        assert bak.is_file()
        assert "cloud_premium" in bak.read_text(encoding="utf-8")
        # 新 yaml 是新内容
        new_yaml = (ws_root / "project.yaml").read_text(encoding="utf-8")
        assert "cloud_quality" in new_yaml


# =============================================================================
# P6-B-13:reviewer 段 GET / PUT 集成
# =============================================================================


class TestReviewerConfigEndpoint:
    """``reviewer`` 段在 GET / PUT 路径下的字段级合并 + 校验(P6-B-13)。"""

    def test_get_returns_reviewer_engine_defaults_when_no_override(
        self, client: TestClient
    ) -> None:
        """GET 无工作区覆盖 → reviewer 段为引擎默认 4 字段。"""
        resp = client.get("/api/v1/workspaces/ws_no_override/config")
        assert resp.status_code == 200
        reviewer = resp.json()["data"]["reviewer"]
        assert reviewer == {
            "pass_threshold": 7.5,
            "revise_threshold": 6.5,
            "escalation_lower_bound": 6.0,
            "escalation_upper_bound": 8.0,
        }

    def test_get_returns_reviewer_field_level_merge(
        self, client: TestClient, workspaces_root: Path
    ) -> None:
        """GET 工作区只填 pass_threshold → 其余字段沿用引擎默认。"""
        ws_root = _make_workspace_dir(workspaces_root, "ws_reviewer_partial")
        _write_project_yaml(
            ws_root, "reviewer:\n  pass_threshold: 9.0\n"
        )
        resp = client.get(
            "/api/v1/workspaces/ws_reviewer_partial/config"
        )
        assert resp.status_code == 200
        reviewer = resp.json()["data"]["reviewer"]
        assert reviewer["pass_threshold"] == 9.0
        assert reviewer["revise_threshold"] == 6.5
        assert reviewer["escalation_upper_bound"] == 8.0

    def test_put_reviewer_full_override_persists_and_returns_merged(
        self, client: TestClient, workspaces_root: Path
    ) -> None:
        """PUT 完整 reviewer 段 → 200 + yaml 落盘 + 返合并视图。"""
        _make_workspace_dir(workspaces_root, "ws_put_reviewer")

        resp = client.put(
            "/api/v1/workspaces/ws_put_reviewer/config",
            json={
                "reviewer": {
                    "pass_threshold": 8.0,
                    "revise_threshold": 7.0,
                    "escalation_lower_bound": 5.5,
                    "escalation_upper_bound": 7.5,
                }
            },
        )
        assert resp.status_code == 200, resp.text
        reviewer = resp.json()["data"]["reviewer"]
        assert reviewer["pass_threshold"] == 8.0
        assert reviewer["revise_threshold"] == 7.0
        # yaml 落盘
        yaml_path = (
            workspaces_root / "ws_put_reviewer" / "project.yaml"
        )
        content = yaml_path.read_text(encoding="utf-8")
        assert "reviewer:" in content
        assert "pass_threshold: 8.0" in content

    def test_put_reviewer_partial_override_merges_field_level(
        self, client: TestClient, workspaces_root: Path
    ) -> None:
        """PUT 只填 pass_threshold → 字段级合并(其余沿用引擎默认)。"""
        _make_workspace_dir(workspaces_root, "ws_put_reviewer_partial")
        resp = client.put(
            "/api/v1/workspaces/ws_put_reviewer_partial/config",
            json={"reviewer": {"pass_threshold": 9.0}},
        )
        assert resp.status_code == 200, resp.text
        reviewer = resp.json()["data"]["reviewer"]
        assert reviewer["pass_threshold"] == 9.0
        assert reviewer["revise_threshold"] == 6.5

    def test_put_reviewer_invalid_threshold_ordering_returns_422(
        self, client: TestClient, workspaces_root: Path
    ) -> None:
        """revise >= pass → 422 CONFIG_YAML_INVALID。"""
        ws_root = _make_workspace_dir(workspaces_root, "ws_put_bad_thr")
        resp = client.put(
            "/api/v1/workspaces/ws_put_bad_thr/config",
            json={
                "reviewer": {
                    "pass_threshold": 6.0,
                    "revise_threshold": 7.0,
                }
            },
        )
        assert resp.status_code == 422
        assert resp.json()["error"]["code"] == "CONFIG_YAML_INVALID"
        # 失败时 project.yaml 不应创建
        assert not (ws_root / "project.yaml").exists()

    def test_put_reviewer_extra_field_returns_422(
        self, client: TestClient, workspaces_root: Path
    ) -> None:
        """reviewer 段含 extra 字段 → 422 VALIDATION_FAILED(extra=forbid)。"""
        _make_workspace_dir(workspaces_root, "ws_put_reviewer_extra")
        resp = client.put(
            "/api/v1/workspaces/ws_put_reviewer_extra/config",
            json={
                "reviewer": {
                    "pass_threshold": 8.0,
                    "unknown_field": 1.0,
                }
            },
        )
        assert resp.status_code == 422
        assert resp.json()["error"]["code"] == "VALIDATION_FAILED"


# =============================================================================
# P6-B-14:retrieval 段 GET / PUT 集成
# =============================================================================


class TestRetrievalConfigEndpoint:
    """``retrieval`` 段在 GET / PUT 路径下的字段级合并 + 校验(P6-B-14)。"""

    def test_get_returns_retrieval_engine_defaults_when_no_override(
        self, client: TestClient
    ) -> None:
        """GET 无工作区覆盖 → retrieval 段为引擎默认 2 字段。"""
        resp = client.get("/api/v1/workspaces/ws_no_override_r/config")
        assert resp.status_code == 200
        retrieval = resp.json()["data"]["retrieval"]
        assert retrieval == {"top_k": 20, "min_similarity": 0.0}

    def test_get_returns_retrieval_field_level_merge(
        self, client: TestClient, workspaces_root: Path
    ) -> None:
        """GET 工作区只填 top_k → min_similarity 沿用引擎默认。"""
        ws_root = _make_workspace_dir(workspaces_root, "ws_retrieval_partial")
        _write_project_yaml(ws_root, "retrieval:\n  top_k: 40\n")
        resp = client.get(
            "/api/v1/workspaces/ws_retrieval_partial/config"
        )
        assert resp.status_code == 200
        retrieval = resp.json()["data"]["retrieval"]
        assert retrieval["top_k"] == 40
        assert retrieval["min_similarity"] == 0.0

    def test_put_retrieval_full_override_persists_and_returns_merged(
        self, client: TestClient, workspaces_root: Path
    ) -> None:
        """PUT 完整 retrieval 段 → 200 + yaml 落盘 + 返合并视图。"""
        _make_workspace_dir(workspaces_root, "ws_put_retrieval")

        resp = client.put(
            "/api/v1/workspaces/ws_put_retrieval/config",
            json={
                "retrieval": {
                    "top_k": 25,
                    "min_similarity": 0.4,
                }
            },
        )
        assert resp.status_code == 200, resp.text
        retrieval = resp.json()["data"]["retrieval"]
        assert retrieval["top_k"] == 25
        assert retrieval["min_similarity"] == 0.4
        yaml_path = (
            workspaces_root / "ws_put_retrieval" / "project.yaml"
        )
        content = yaml_path.read_text(encoding="utf-8")
        assert "retrieval:" in content
        assert "top_k: 25" in content
        assert "min_similarity: 0.4" in content

    def test_put_retrieval_invalid_top_k_returns_422(
        self, client: TestClient, workspaces_root: Path
    ) -> None:
        """top_k <= 0 → 422 VALIDATION_FAILED(Pydantic ge=1 在 body schema 层先拦)。"""
        ws_root = _make_workspace_dir(workspaces_root, "ws_put_bad_topk")
        resp = client.put(
            "/api/v1/workspaces/ws_put_bad_topk/config",
            json={"retrieval": {"top_k": 0}},
        )
        assert resp.status_code == 422
        # body 层 Pydantic 字段校验先于 service 层语义校验,所以是 VALIDATION_FAILED
        assert resp.json()["error"]["code"] == "VALIDATION_FAILED"
        assert not (ws_root / "project.yaml").exists()

    def test_put_retrieval_min_similarity_out_of_range_returns_422(
        self, client: TestClient, workspaces_root: Path
    ) -> None:
        """min_similarity > 1.0 → 422 VALIDATION_FAILED。"""
        ws_root = _make_workspace_dir(workspaces_root, "ws_put_bad_sim")
        resp = client.put(
            "/api/v1/workspaces/ws_put_bad_sim/config",
            json={"retrieval": {"min_similarity": 1.5}},
        )
        assert resp.status_code == 422
        assert resp.json()["error"]["code"] == "VALIDATION_FAILED"
        assert not (ws_root / "project.yaml").exists()

    def test_put_retrieval_extra_field_returns_422(
        self, client: TestClient, workspaces_root: Path
    ) -> None:
        """retrieval 段含 extra 字段 → 422 VALIDATION_FAILED(extra=forbid)。"""
        _make_workspace_dir(workspaces_root, "ws_put_retrieval_extra")
        resp = client.put(
            "/api/v1/workspaces/ws_put_retrieval_extra/config",
            json={
                "retrieval": {
                    "top_k": 20,
                    "unknown_field": 1.0,
                }
            },
        )
        assert resp.status_code == 422
        assert resp.json()["error"]["code"] == "VALIDATION_FAILED"


# =============================================================================
# P6-B-15:cost_guard 段 GET / PUT 集成
# =============================================================================


class TestCostGuardConfigEndpoint:
    """``cost_guard`` 段在 GET / PUT 路径下的字段级合并 + 校验(P6-B-15)。"""

    def test_get_returns_cost_guard_engine_defaults_when_no_override(
        self, client: TestClient
    ) -> None:
        """GET 无工作区覆盖 → cost_guard 段为引擎默认 6 字段。"""
        resp = client.get("/api/v1/workspaces/ws_no_override_c/config")
        assert resp.status_code == 200
        cost_guard = resp.json()["data"]["cost_guard"]
        assert cost_guard == {
            "cost_currency": "USD",
            "daily_usd_limit": 20.0,
            "monthly_usd_limit": 300.0,
            "per_workspace_daily_limit": 10.0,
            "action_on_exceed": "pause",
            "notify_via": ["log", "websocket"],
        }

    def test_get_returns_cost_guard_field_level_merge(
        self, client: TestClient, workspaces_root: Path
    ) -> None:
        """GET 工作区只填 action_on_exceed → 其余字段沿用引擎默认。"""
        ws_root = _make_workspace_dir(
            workspaces_root, "ws_cost_guard_partial"
        )
        _write_project_yaml(
            ws_root, "cost_guard:\n  action_on_exceed: warn\n"
        )
        resp = client.get(
            "/api/v1/workspaces/ws_cost_guard_partial/config"
        )
        assert resp.status_code == 200
        cost_guard = resp.json()["data"]["cost_guard"]
        assert cost_guard["action_on_exceed"] == "warn"
        assert cost_guard["daily_usd_limit"] == 20.0
        assert cost_guard["notify_via"] == ["log", "websocket"]

    def test_put_cost_guard_full_override_persists_and_returns_merged(
        self, client: TestClient, workspaces_root: Path
    ) -> None:
        """PUT 完整 cost_guard 段 → 200 + yaml 落盘 + 返合并视图。"""
        _make_workspace_dir(workspaces_root, "ws_put_cost_guard")

        resp = client.put(
            "/api/v1/workspaces/ws_put_cost_guard/config",
            json={
                "cost_guard": {
                    "cost_currency": "rmb",
                    "daily_usd_limit": 50.0,
                    "monthly_usd_limit": 600.0,
                    "per_workspace_daily_limit": 25.0,
                    "action_on_exceed": "warn",
                    "notify_via": ["log"],
                }
            },
        )
        assert resp.status_code == 200, resp.text
        cost_guard = resp.json()["data"]["cost_guard"]
        assert cost_guard["cost_currency"] == "RMB"
        assert cost_guard["daily_usd_limit"] == 50.0
        assert cost_guard["action_on_exceed"] == "warn"
        assert cost_guard["notify_via"] == ["log"]
        yaml_path = (
            workspaces_root / "ws_put_cost_guard" / "project.yaml"
        )
        content = yaml_path.read_text(encoding="utf-8")
        assert "cost_guard:" in content
        assert "cost_currency: RMB" in content
        assert "daily_usd_limit: 50.0" in content
        assert "action_on_exceed: warn" in content

    def test_put_cost_guard_partial_override_merges_field_level(
        self, client: TestClient, workspaces_root: Path
    ) -> None:
        """PUT 只填 action_on_exceed → 字段级合并(其余沿用引擎默认)。"""
        _make_workspace_dir(workspaces_root, "ws_put_cost_guard_partial")
        resp = client.put(
            "/api/v1/workspaces/ws_put_cost_guard_partial/config",
            json={"cost_guard": {"action_on_exceed": "continue"}},
        )
        assert resp.status_code == 200, resp.text
        cost_guard = resp.json()["data"]["cost_guard"]
        assert cost_guard["action_on_exceed"] == "continue"
        assert cost_guard["daily_usd_limit"] == 20.0

    def test_put_cost_guard_subresource_preserves_other_overrides(
        self, client: TestClient, workspaces_root: Path
    ) -> None:
        """独立成本端点局部合并 cost_guard，其他 project.yaml 段原样保留。"""
        ws_root = _make_workspace_dir(workspaces_root, "ws_cost_guard_safe")
        _write_project_yaml(
            ws_root,
            """\
retrieval:
  top_k: 7
generation:
  word_count_min: 3000
cost_guard:
  action_on_exceed: warn
  monthly_usd_limit: 700.0
""",
        )

        resp = client.put(
            "/api/v1/workspaces/ws_cost_guard_safe/config/cost-guard",
            json={"per_workspace_daily_limit": 80.0},
        )

        assert resp.status_code == 200, resp.text
        cost_guard = resp.json()["data"]
        assert cost_guard["per_workspace_daily_limit"] == 80.0
        assert cost_guard["action_on_exceed"] == "warn"
        assert cost_guard["monthly_usd_limit"] == 700.0
        content = (ws_root / "project.yaml").read_text(encoding="utf-8")
        assert "top_k: 7" in content
        assert "word_count_min: 3000" in content
        assert "per_workspace_daily_limit: 80.0" in content

    def test_put_cost_guard_negative_limit_returns_422(
        self, client: TestClient, workspaces_root: Path
    ) -> None:
        """daily_usd_limit < 0 → 422 VALIDATION_FAILED(Pydantic ge=0)。"""
        ws_root = _make_workspace_dir(workspaces_root, "ws_put_bad_usd")
        resp = client.put(
            "/api/v1/workspaces/ws_put_bad_usd/config",
            json={"cost_guard": {"daily_usd_limit": -1.0}},
        )
        assert resp.status_code == 422
        assert resp.json()["error"]["code"] == "VALIDATION_FAILED"
        assert not (ws_root / "project.yaml").exists()

    def test_put_cost_guard_invalid_action_returns_422(
        self, client: TestClient, workspaces_root: Path
    ) -> None:
        """action_on_exceed 非 Literal 三档 → 422 VALIDATION_FAILED。"""
        ws_root = _make_workspace_dir(workspaces_root, "ws_put_bad_action")
        resp = client.put(
            "/api/v1/workspaces/ws_put_bad_action/config",
            json={"cost_guard": {"action_on_exceed": "bogus"}},
        )
        assert resp.status_code == 422
        assert resp.json()["error"]["code"] == "VALIDATION_FAILED"
        assert not (ws_root / "project.yaml").exists()

    def test_put_cost_guard_extra_field_returns_422(
        self, client: TestClient, workspaces_root: Path
    ) -> None:
        """cost_guard 段含 extra 字段 → 422 VALIDATION_FAILED(extra=forbid)。"""
        _make_workspace_dir(workspaces_root, "ws_put_cost_guard_extra")
        resp = client.put(
            "/api/v1/workspaces/ws_put_cost_guard_extra/config",
            json={
                "cost_guard": {
                    "daily_usd_limit": 25.0,
                    "unknown_field": 1.0,
                }
            },
        )
        assert resp.status_code == 422
        assert resp.json()["error"]["code"] == "VALIDATION_FAILED"


# =============================================================================
# 可路由 agent 清单(v1.1 wave 2 H3)
# =============================================================================


class TestRoutableAgentsEndpoint:
    """``GET /api/v1/config/routable-agents`` 集成测试(v1.1 wave 2 H3)。

    覆盖:
    - workspace_id 省略 → 引擎默认视图(current_profile = default)
    - workspace_id 给定 → 工作区合并视图(current_profile 反映工作区 agent_routing 覆盖)
    - 全 22 个 LLM-driven agent 都在响应中(对齐 AGENT_NAME Literal 清单)
    - available_profiles 列出 sorted 全部已声明 profile
    - 引擎 config.yaml 损坏 → 422
    """

    def _patch_engine_config_path(
        self,
        client: TestClient,
        engine_config_path: Path,
        workspaces_root: Path,
    ) -> None:
        """让 ``/config/routable-agents`` 端点内部走 ``load_writer_profiles_config(settings.config_yaml_path)``
        时读测试 fixture 的 engine_config_path,而不是真实生产 config.yaml。

        通过 monkeypatch ``src.config.settings.config_yaml_path`` 实现。
        """
        from src.config import settings

        self._original_path = settings.config_yaml_path
        settings.config_yaml_path = engine_config_path

    def _restore_engine_config_path(self) -> None:
        from src.config import settings

        settings.config_yaml_path = self._original_path

    def test_list_returns_engine_default_view_when_workspace_id_omitted(
        self,
        client: TestClient,
        engine_config_path: Path,
        workspaces_root: Path,
    ) -> None:
        """workspace_id 省略 → current_profile 全部等于 default(local_qwen_32k 除已显式
        覆盖的 writer/reviewer/memory_extractor 外)。"""
        self._patch_engine_config_path(
            client, engine_config_path, workspaces_root
        )
        try:
            resp = client.get("/api/v1/config/routable-agents")
        finally:
            self._restore_engine_config_path()

        assert resp.status_code == 200, resp.json()
        data = resp.json()["data"]
        agent_map = {a["agent_name"]: a for a in data["agents"]}

        # 默认覆盖的 3 个 agent 走 agent_routing 字段
        assert agent_map["writer"]["current_profile"] == "local_qwen_32k"
        assert agent_map["reviewer"]["current_profile"] == "local_qwen_32k"
        assert (
            agent_map["memory_extractor"]["current_profile"] == "local_qwen_32k"
        )
        # 未覆盖的 agent 走 default 兜底
        assert agent_map["reviser"]["current_profile"] == "local_qwen_32k"
        assert (
            agent_map["beat_alignment_validator"]["current_profile"]
            == "local_qwen_32k"
        )
        # default_profile 字段反映 routing.default
        assert agent_map["writer"]["default_profile"] == "local_qwen_32k"

    def test_list_returns_workspace_view_when_workspace_id_given(
        self,
        client: TestClient,
        engine_config_path: Path,
        workspaces_root: Path,
    ) -> None:
        """workspace_id 给定 → current_profile 反映工作区合并视图。

        工作区 project.yaml 覆盖 writer → cloud_quality,reviewer 不动 → 仍走引擎
        默认 local_qwen_32k。
        """
        ws_root = _make_workspace_dir(workspaces_root, "ws_routable_override")
        _write_project_yaml(
            ws_root,
            """\
writer_routing:
  agent_routing:
    writer: cloud_quality
""",
        )
        resp = client.get(
            "/api/v1/config/routable-agents?workspace_id=ws_routable_override"
        )
        assert resp.status_code == 200, resp.json()
        agent_map = {a["agent_name"]: a for a in resp.json()["data"]["agents"]}

        # writer 被工作区覆盖
        assert agent_map["writer"]["current_profile"] == "cloud_quality"
        # reviewer 没被覆盖 → 走引擎默认 agent_routing(local_qwen_32k)
        assert agent_map["reviewer"]["current_profile"] == "local_qwen_32k"

    def test_list_includes_all_25_routable_agents(
        self,
        client: TestClient,
        engine_config_path: Path,
        workspaces_root: Path,
    ) -> None:
        """响应必含全 31 个 LLM-driven agent(对齐 AGENT_NAME Literal;W3.5-S3 +1 outline_consistency;W3.5 补强3 +1 forbidden_crossing)。"""
        self._patch_engine_config_path(
            client, engine_config_path, workspaces_root
        )
        try:
            resp = client.get("/api/v1/config/routable-agents")
        finally:
            self._restore_engine_config_path()

        assert resp.status_code == 200
        agent_names = {a["agent_name"] for a in resp.json()["data"]["agents"]}
        expected = {
            # P1-P2
            "writer",
            "reviewer",
            "reviser",
            "memory_extractor",
            "continuity",
            # P3
            "reranker",
            "reviewer_escalation",
            # P4-P5
            "outline_builder",
            "chapter_card_generator",
            "mini_plan_generator",
            "memory_health_checker",
            # v1.0.x wave 4
            "chapter_summary_extractor",
            # v1.1 wave 1
            "continuity_validator",
            "must_achieve_validator",
            "beat_alignment_validator",
            "must_vs_forbidden_validator",
            # v1.1 wave 2 session 5(A3 mergers)
            "plot_thread_merger",
            "character_disambiguator",
            "timeline_entry_merger",
            # v1.1 wave 4 session 5(I2.2 StyleAuditor)
            "style_auditor",
            # v1.1 wave 6 session 1(A1 + A4 内容护栏)
            "mechanism_consistency_validator",
            "thread_closure_validator",
            # v1.1 wave 6 session 2(A3 身份覆盖)
            "identity_coverage_validator",
            # v1.1 wave 6 session 3(B1 设定一致性 + B2 场景锚点)
            "setting_consistency_validator",
            "scene_anchor_validator",
            # v1.1 wave 6 session 4(B4 正文跨章去重 + B6 终局张力曲线)
            "cross_chapter_dedup_validator",
            "ending_tension_validator",
            # v1.1 acceptance 修复1 + 修复2(修复1 漏登元数据表,修复2 一并补)
            "volume_seam_replay_validator",
            "must_feasibility_validator",
            # v2 W3.5-S3(大纲跨段一致性校验,qwen)
            "outline_consistency_validator",
            # v2 W3.5 补强3(正文级 forbidden 越界下游硬闸,qwen)
            "forbidden_crossing_validator",
        }
        assert agent_names == expected, (
            f"missing={expected - agent_names}, extra={agent_names - expected}"
        )

    def test_list_available_profiles_sorted_all_declared(
        self,
        client: TestClient,
        engine_config_path: Path,
        workspaces_root: Path,
    ) -> None:
        """available_profiles 字段列出引擎已声明 profile 清单(sorted)。"""
        self._patch_engine_config_path(
            client, engine_config_path, workspaces_root
        )
        try:
            resp = client.get("/api/v1/config/routable-agents")
        finally:
            self._restore_engine_config_path()

        assert resp.status_code == 200
        data = resp.json()["data"]
        assert data["available_profiles"] == [
            "cloud_premium",
            "cloud_quality",
            "local_qwen_32k",
        ]

    def test_list_returns_metadata_chinese_display_name(
        self,
        client: TestClient,
        engine_config_path: Path,
        workspaces_root: Path,
    ) -> None:
        """每条 agent 都有中文 display_name + description(前端 UI label / tooltip)。"""
        self._patch_engine_config_path(
            client, engine_config_path, workspaces_root
        )
        try:
            resp = client.get("/api/v1/config/routable-agents")
        finally:
            self._restore_engine_config_path()

        agent_map = {a["agent_name"]: a for a in resp.json()["data"]["agents"]}
        # 抽查关键字段:writer 中文名"正文写作"+ reviewer "审稿评分"
        assert agent_map["writer"]["display_name"] == "正文写作"
        assert agent_map["reviewer"]["display_name"] == "审稿评分"
        # llm_driven 全 True(纯规则 validator 不在本端点)
        assert all(a["llm_driven"] for a in resp.json()["data"]["agents"])

    def test_list_empty_workspace_id_treated_as_engine_default(
        self,
        client: TestClient,
        engine_config_path: Path,
        workspaces_root: Path,
    ) -> None:
        """workspace_id="" 空串 → 走引擎默认视图(对齐 docstring 字面)。"""
        self._patch_engine_config_path(
            client, engine_config_path, workspaces_root
        )
        try:
            resp = client.get("/api/v1/config/routable-agents?workspace_id=")
        finally:
            self._restore_engine_config_path()

        assert resp.status_code == 200
        agent_map = {a["agent_name"]: a for a in resp.json()["data"]["agents"]}
        # 走引擎默认 agent_routing → writer 走 local_qwen_32k(不走 cloud_quality)
        assert agent_map["writer"]["current_profile"] == "local_qwen_32k"

    # ---- W3.5-s5:is_llm_judge + profile_meta(judge UI 拦截 + 校准状态后端契约)----

    def test_list_is_llm_judge_mirrors_frozenset(
        self,
        client: TestClient,
        engine_config_path: Path,
        workspaces_root: Path,
    ) -> None:
        """W3.5-s5:``is_llm_judge=True`` 的 agent 集合**精确等于** ``LLM_AS_JUDGE_AGENTS``。

        前端据此前置拦截"把 judge 绑 thinking-off profile"。本镜像测防后端
        frozenset 演进(加/删 judge)而响应字段漂移 → UI 拦截误判(漏拦/误拦)。
        额外锚定易混对:``continuity``(抽取,非 judge)vs ``continuity_validator``
        (治理 judge);``reviewer`` / ``style_auditor`` 是打分非治理 judge。
        """
        from src.core.models.routing import LLM_AS_JUDGE_AGENTS

        self._patch_engine_config_path(
            client, engine_config_path, workspaces_root
        )
        try:
            resp = client.get("/api/v1/config/routable-agents")
        finally:
            self._restore_engine_config_path()

        assert resp.status_code == 200, resp.text
        agents = resp.json()["data"]["agents"]
        judge_set = {a["agent_name"] for a in agents if a["is_llm_judge"]}
        assert judge_set == set(LLM_AS_JUDGE_AGENTS), (
            f"missing={set(LLM_AS_JUDGE_AGENTS) - judge_set}, "
            f"extra={judge_set - set(LLM_AS_JUDGE_AGENTS)}"
        )
        amap = {a["agent_name"]: a for a in agents}
        # 易混对:抽取型 continuity 非 judge / 治理型 continuity_validator 是 judge
        assert amap["continuity"]["is_llm_judge"] is False
        assert amap["continuity_validator"]["is_llm_judge"] is True
        # 打分型(reviewer / style_auditor)非治理 judge;writer 非 judge
        assert amap["reviewer"]["is_llm_judge"] is False
        assert amap["style_auditor"]["is_llm_judge"] is False
        assert amap["writer"]["is_llm_judge"] is False

    def test_list_profile_meta_thinking_and_uncalibrated(
        self,
        client: TestClient,
        engine_config_path: Path,
        workspaces_root: Path,
    ) -> None:
        """W3.5-s5:``profile_meta`` 暴露 model / thinking_on / calibrated / calibrated_at。

        本 fixture(``_ENGINE_YAML``)无 ``model_calibration`` 段 → 所有模型
        ``calibrated=False`` / ``calibrated_at=None``(优雅空,= 未校准警告路径);
        thinking_on 反映 provider(ollama think:false → False;claude → True)。
        key 集合必须 == ``available_profiles``(前端两处一致消费)。
        """
        self._patch_engine_config_path(
            client, engine_config_path, workspaces_root
        )
        try:
            resp = client.get("/api/v1/config/routable-agents")
        finally:
            self._restore_engine_config_path()

        assert resp.status_code == 200, resp.text
        data = resp.json()["data"]
        meta = data["profile_meta"]
        assert set(meta.keys()) == set(data["available_profiles"])
        # thinking_on:ollama think:false → False;claude(恒 pass)→ True
        assert meta["local_qwen_32k"]["thinking_on"] is False
        assert meta["cloud_quality"]["thinking_on"] is True
        assert meta["cloud_premium"]["thinking_on"] is True
        # model 透传
        assert meta["local_qwen_32k"]["model"] == "qwen-novel"
        assert meta["cloud_quality"]["model"] == "claude-sonnet-4-6"
        # 无 model_calibration 段 → 全部未校准(优雅空)
        assert all(not m["calibrated"] for m in meta.values())
        assert all(m["calibrated_at"] is None for m in meta.values())


# 带逐行注释的引擎 yaml(测 ruamel round-trip 保注释)。
_COMMENTED_ENGINE_YAML = """\
# 引擎默认配置头注释(顶层注释保留测试)
writer_profiles:
  local_qwen_32k:
    provider: ollama
    model: qwen-novel
    base_url: http://localhost:11434
    context_window: 24576
    generation_params: {temperature: 0.7}
    extra_request_kwargs: {think: false}
    notes: 本地 Qwen 32k
  cloud_quality:
    provider: claude
    model: claude-sonnet-4-6
    context_window: 200000
    generation_params: {temperature: 0.85}
    cost_per_1k_input: 0.003
    cost_per_1k_output: 0.015

writer_routing:
  default: local_qwen_32k
  agent_routing:
    writer: local_qwen_32k       # writer 行尾注释保留测试
    reviewer: local_qwen_32k     # reviewer 行尾注释保留测试
"""


class TestUpdateEngineAgentRouting:
    """``PUT /api/v1/config/agent-routing`` 集成测试(v1.1 wave 3 H5,路 B 全局可改)。

    覆盖:
    - happy:整段替换 agent_routing → 200 + config.yaml 落盘 + 返刷新全局视图
    - 整段替换语义:未列出的 agent 从 yaml 删除 → 回退 default
    - 空 dict → 清空所有覆盖
    - ruamel round-trip 保留逐行 + 顶层注释
    - 引用未声明 profile → 422 CONFIG_YAML_INVALID(不落盘)
    - LLM-judge agent 切 thinking-off profile → 422(hard guard,不落盘)
    - LLM-judge agent 切 thinking-on(claude)profile → 200
    - 写前备份 config.yaml.bak
    - body extra 字段 → 422 VALIDATION_FAILED
    """

    def test_put_persists_and_returns_refreshed_view(
        self, client: TestClient, engine_config_path: Path
    ) -> None:
        """PUT 整段替换 → 200,response 反映新绑定 + config.yaml 落盘。"""
        resp = client.put(
            "/api/v1/config/agent-routing",
            json={
                "agent_routing": {
                    "writer": "cloud_quality",
                    "reviewer": "cloud_quality",
                }
            },
        )
        assert resp.status_code == 200, resp.text
        agent_map = {
            a["agent_name"]: a for a in resp.json()["data"]["agents"]
        }
        assert agent_map["writer"]["current_profile"] == "cloud_quality"
        assert agent_map["reviewer"]["current_profile"] == "cloud_quality"
        # memory_extractor 原在引擎默认 agent_routing,整段替换后被删 → 回退 default
        assert (
            agent_map["memory_extractor"]["current_profile"] == "local_qwen_32k"
        )
        # 落盘
        content = engine_config_path.read_text(encoding="utf-8")
        assert "cloud_quality" in content

    def test_put_empty_dict_clears_all_overrides(
        self, client: TestClient
    ) -> None:
        """PUT 空 dict → 清空所有 agent 覆盖,全部走 default。"""
        resp = client.put(
            "/api/v1/config/agent-routing", json={"agent_routing": {}}
        )
        assert resp.status_code == 200, resp.text
        agent_map = {
            a["agent_name"]: a for a in resp.json()["data"]["agents"]
        }
        assert agent_map["writer"]["current_profile"] == "local_qwen_32k"
        assert agent_map["reviewer"]["current_profile"] == "local_qwen_32k"

    def test_put_invalid_profile_ref_returns_422(
        self, client: TestClient, engine_config_path: Path
    ) -> None:
        """引用未声明 profile → 422 CONFIG_YAML_INVALID,原 config.yaml 不动。"""
        original = engine_config_path.read_text(encoding="utf-8")
        resp = client.put(
            "/api/v1/config/agent-routing",
            json={"agent_routing": {"writer": "no_such_profile"}},
        )
        assert resp.status_code == 422, resp.text
        assert resp.json()["error"]["code"] == "CONFIG_YAML_INVALID"
        # 不落盘
        assert engine_config_path.read_text(encoding="utf-8") == original

    def test_put_llm_judge_thinking_off_returns_422(
        self, client: TestClient, engine_config_path: Path
    ) -> None:
        """LLM-judge agent 绑 thinking-off profile → 422(hard guard),不落盘。"""
        original = engine_config_path.read_text(encoding="utf-8")
        resp = client.put(
            "/api/v1/config/agent-routing",
            # local_qwen_32k = ollama think:false → thinking off
            json={"agent_routing": {"must_achieve_validator": "local_qwen_32k"}},
        )
        assert resp.status_code == 422, resp.text
        assert resp.json()["error"]["code"] == "CONFIG_YAML_INVALID"
        assert engine_config_path.read_text(encoding="utf-8") == original

    def test_put_llm_judge_thinking_on_accepted(
        self, client: TestClient
    ) -> None:
        """LLM-judge agent 绑 thinking-on(claude)profile → 200。"""
        resp = client.put(
            "/api/v1/config/agent-routing",
            # cloud_quality = claude → thinking on(always pass)
            json={"agent_routing": {"must_achieve_validator": "cloud_quality"}},
        )
        assert resp.status_code == 200, resp.text
        agent_map = {
            a["agent_name"]: a for a in resp.json()["data"]["agents"]
        }
        assert (
            agent_map["must_achieve_validator"]["current_profile"]
            == "cloud_quality"
        )

    def test_put_writes_backup(
        self, client: TestClient, engine_config_path: Path
    ) -> None:
        """写前备份 config.yaml.bak。"""
        resp = client.put(
            "/api/v1/config/agent-routing",
            json={"agent_routing": {"writer": "cloud_quality"}},
        )
        assert resp.status_code == 200, resp.text
        backup = engine_config_path.with_suffix(".yaml.bak")
        assert backup.is_file()

    def test_put_body_extra_field_returns_422(
        self, client: TestClient
    ) -> None:
        """body 含 extra 字段 → 422 VALIDATION_FAILED(Pydantic extra forbid)。"""
        resp = client.put(
            "/api/v1/config/agent-routing",
            json={"agent_routing": {}, "unexpected": 1},
        )
        assert resp.status_code == 422, resp.text

    def test_save_preserves_inline_and_top_comments(
        self, tmp_path: Path
    ) -> None:
        """ruamel round-trip 保留逐行 + 顶层注释(service 层直测,Q17 路 B 核心)。"""
        cfg = tmp_path / "config.yaml"
        cfg.write_text(_COMMENTED_ENGINE_YAML, encoding="utf-8")
        ws_root = tmp_path / "ws"
        ws_root.mkdir()
        service = ConfigService(
            engine_config_path=cfg, workspaces_root=ws_root
        )

        # 改 writer 的值,reviewer 不动
        merged = service.save_engine_agent_routing(
            {"writer": "cloud_quality", "reviewer": "local_qwen_32k"}
        )
        assert merged.routing.agent_routing["writer"] == "cloud_quality"

        content = cfg.read_text(encoding="utf-8")
        # 顶层注释保留
        assert "# 引擎默认配置头注释(顶层注释保留测试)" in content
        # 未改的 reviewer 行尾注释保留
        assert "# reviewer 行尾注释保留测试" in content
        # 改了值的 writer 行尾注释仍保留(ruamel 注释绑 key)
        assert "# writer 行尾注释保留测试" in content
        # writer 的值已更新
        assert "writer: cloud_quality" in content


# =============================================================================
# v1.1 L1 Q16:generation 段 — 统一 GET + 专属 GET/PUT 端点集成
# =============================================================================


class TestGenerationConfigEndpoint:
    """``generation`` 段在统一 GET + 专属 GET/PUT 端点的合并 + 校验(v1.1 L1)。"""

    def test_unified_get_exposes_generation_engine_defaults(
        self, client: TestClient
    ) -> None:
        """统一 /config GET 暴露 generation 段引擎默认(2500/4000/3)。"""
        resp = client.get("/api/v1/workspaces/ws_no_override/config")
        assert resp.status_code == 200
        gen = resp.json()["data"]["generation"]
        assert gen == {
            "word_count_min": 2500,
            "word_count_max": 4000,
            "word_count_policy_version": 0,
            "max_rewrites": 3,
            "plot_node_budget": 3,
            "progression_consistency": "auto",
            "breakthrough_payoff": "auto",
            "style_anchor_source": "prev_chapter",  # v2 间隙回退引擎默认
            # v2 W3.5 校准联动三态:fixture engine config(_ENGINE_YAML)无 model_calibration
            # 段 → calibration_default=False;无工作区 override(None)→ enabled 生效值 = False。
            "anti_tic_trim_enabled": False,
            "anti_tic_trim_override": None,
            "anti_tic_trim_calibration_default": False,
            "word_count_trim_enabled": False,
            "word_count_trim_override": None,
            "word_count_trim_calibration_default": False,
            "outline_autofix_enabled": True,  # v2 W3.5-s4-step1 默认开
            "character_backfill_enabled": True,  # v2 W3.5-s4-step1 机制1 默认开
            "core_state_hard_enabled": True,
        }

    def test_dedicated_get_returns_engine_defaults_when_no_override(
        self, client: TestClient
    ) -> None:
        """专属 GET 无工作区覆盖 → 引擎默认。"""
        resp = client.get(
            "/api/v1/workspaces/ws_no_override/config/generation"
        )
        assert resp.status_code == 200
        assert resp.json()["data"] == {
            "word_count_min": 2500,
            "word_count_max": 4000,
            "word_count_policy_version": 0,
            "max_rewrites": 3,
            "plot_node_budget": 3,
            "progression_consistency": "auto",
            "breakthrough_payoff": "auto",
            "style_anchor_source": "prev_chapter",  # v2 间隙回退引擎默认
            # v2 W3.5 校准联动三态:fixture engine config(_ENGINE_YAML)无 model_calibration
            # 段 → calibration_default=False;无工作区 override(None)→ enabled 生效值 = False。
            "anti_tic_trim_enabled": False,
            "anti_tic_trim_override": None,
            "anti_tic_trim_calibration_default": False,
            "word_count_trim_enabled": False,
            "word_count_trim_override": None,
            "word_count_trim_calibration_default": False,
            "outline_autofix_enabled": True,  # v2 W3.5-s4-step1 默认开
            "character_backfill_enabled": True,  # v2 W3.5-s4-step1 机制1 默认开
            "core_state_hard_enabled": True,
        }

    def test_dedicated_get_field_level_merge(
        self, client: TestClient, workspaces_root: Path
    ) -> None:
        """专属 GET 工作区只填 word_count_min → 其余沿用引擎默认。"""
        ws_root = _make_workspace_dir(workspaces_root, "ws_gen_partial")
        _write_project_yaml(ws_root, "generation:\n  word_count_min: 2000\n")
        resp = client.get(
            "/api/v1/workspaces/ws_gen_partial/config/generation"
        )
        assert resp.status_code == 200
        data = resp.json()["data"]
        assert data["word_count_min"] == 2000
        assert data["word_count_max"] == 4000
        assert data["max_rewrites"] == 3

    def test_dedicated_get_calibration_default_on_for_deepseek_writer(
        self, workspaces_root: Path, tmp_path: Path
    ) -> None:
        """W3.5 校准联动接线(切 model → 校准全联动,plan §6-s5 护栏①):engine config
        writer→deepseek(overshoot/high)+ 无工作区 override → GET 暴露 calibration_default
        =True + override=None + enabled 生效=True(切 deepseek 自动开 trim,HTTP 端到端透传)。"""
        import yaml as _yaml

        ds_yaml = _yaml.safe_dump(
            {
                "writer_profiles": {
                    "cloud_deepseek_v4_pro": {
                        "provider": "openai_compatible",
                        "model": "deepseek-v4-pro",
                        "base_url": "https://dashscope.aliyuncs.com/compatible-mode/v1",
                        "context_window": 1000000,
                        "generation_params": {"temperature": 0.85, "max_tokens": 8000},
                        "extra_request_kwargs": {"enable_thinking": False},
                        "cost_per_1k_input": 0.00042,
                        "cost_per_1k_output": 0.00083,
                    }
                },
                "writer_routing": {
                    "default": "cloud_deepseek_v4_pro",
                    "agent_routing": {"writer": "cloud_deepseek_v4_pro"},
                },
                "model_calibration": {
                    "deepseek-v4-pro": {
                        "calibrated_at": "2026-06-16",
                        "calibration_corpus": "test",
                        "global": {
                            "length_tendency": "overshoot",
                            "anti_stamp_strength": "high",
                            "recommended_temperature": 0.85,
                            "thinking_default": False,
                            "reviewer_threshold_adjustment": 0.0,
                        },
                        "per_genre": {
                            "introspection": {
                                "chars_per_beat": 1534,
                                "chars_per_chapter": 4602,
                                "length_compliance": 0.667,
                                "content_units_per_chapter": 3,
                                "sample_chapters": 6,
                            }
                        },
                    }
                },
            },
            allow_unicode=True,
        )
        ds_cfg = tmp_path / "ds_config.yaml"
        ds_cfg.write_text(ds_yaml, encoding="utf-8")
        _make_workspace_dir(workspaces_root, "ws_ds_api")
        svc = ConfigService(
            engine_config_path=ds_cfg, workspaces_root=workspaces_root
        )
        app = create_app()
        app.dependency_overrides[get_config_service] = lambda: svc
        with TestClient(app) as test_client:
            resp = test_client.get(
                "/api/v1/workspaces/ws_ds_api/config/generation"
            )
        assert resp.status_code == 200
        data = resp.json()["data"]
        # 切 deepseek + 无工作区 override → 两 trim 校准默认开,生效=校准默认
        assert data["word_count_trim_override"] is None
        assert data["word_count_trim_calibration_default"] is True
        assert data["word_count_trim_enabled"] is True
        assert data["anti_tic_trim_override"] is None
        assert data["anti_tic_trim_calibration_default"] is True
        assert data["anti_tic_trim_enabled"] is True

    def test_dedicated_put_full_override_persists_and_returns_merged(
        self, client: TestClient, workspaces_root: Path
    ) -> None:
        """专属 PUT 完整 generation 段 → 200 + yaml 落盘 + 返合并视图。"""
        _make_workspace_dir(workspaces_root, "ws_put_gen")
        resp = client.put(
            "/api/v1/workspaces/ws_put_gen/config/generation",
            json={
                "word_count_min": 1800,
                "word_count_max": 3200,
                "max_rewrites": 5,
            },
        )
        assert resp.status_code == 200, resp.text
        data = resp.json()["data"]
        assert data["word_count_min"] == 1800
        assert data["word_count_max"] == 3200
        assert data["max_rewrites"] == 5
        content = (
            workspaces_root / "ws_put_gen" / "project.yaml"
        ).read_text(encoding="utf-8")
        assert "generation:" in content
        assert "word_count_min: 1800" in content

    def test_dedicated_put_preserves_other_sections(
        self, client: TestClient, workspaces_root: Path
    ) -> None:
        """专属 PUT 只改 generation 段不冲掉已有 reviewer 覆盖(L1 核心保证)。"""
        ws_root = _make_workspace_dir(workspaces_root, "ws_gen_preserve")
        _write_project_yaml(ws_root, "reviewer:\n  pass_threshold: 9.0\n")
        resp = client.put(
            "/api/v1/workspaces/ws_gen_preserve/config/generation",
            json={"word_count_min": 2100},
        )
        assert resp.status_code == 200, resp.text
        # generation 段生效 + reviewer 覆盖仍在(查统一 GET)
        merged = client.get(
            "/api/v1/workspaces/ws_gen_preserve/config"
        ).json()["data"]
        assert merged["generation"]["word_count_min"] == 2100
        assert merged["reviewer"]["pass_threshold"] == 9.0

    def test_dedicated_put_partial_override_merges_field_level(
        self, client: TestClient, workspaces_root: Path
    ) -> None:
        """专属 PUT 只填 max_rewrites → 字段级合并(字数沿用引擎默认)。"""
        _make_workspace_dir(workspaces_root, "ws_put_gen_partial")
        resp = client.put(
            "/api/v1/workspaces/ws_put_gen_partial/config/generation",
            json={"max_rewrites": 6},
        )
        assert resp.status_code == 200, resp.text
        data = resp.json()["data"]
        assert data["max_rewrites"] == 6
        assert data["word_count_min"] == 2500
        assert data["word_count_max"] == 4000

    def test_dedicated_put_can_disable_core_state_hard_without_losing_audit(
        self, client: TestClient, workspaces_root: Path
    ) -> None:
        """工作区总开关可持久化关闭；默认仍为开。"""
        _make_workspace_dir(workspaces_root, "ws_core_state_off")
        resp = client.put(
            "/api/v1/workspaces/ws_core_state_off/config/generation",
            json={"core_state_hard_enabled": False},
        )
        assert resp.status_code == 200, resp.text
        assert resp.json()["data"]["core_state_hard_enabled"] is False
        content = (
            workspaces_root / "ws_core_state_off" / "project.yaml"
        ).read_text(encoding="utf-8")
        assert "core_state_hard_enabled: false" in content

    def test_dedicated_put_invalid_ordering_returns_422(
        self, client: TestClient, workspaces_root: Path
    ) -> None:
        """word_count_min > word_count_max → 422 CONFIG_YAML_INVALID + 不落盘。"""
        ws_root = _make_workspace_dir(workspaces_root, "ws_put_gen_bad")
        resp = client.put(
            "/api/v1/workspaces/ws_put_gen_bad/config/generation",
            json={"word_count_min": 5000, "word_count_max": 3000},
        )
        assert resp.status_code == 422
        assert resp.json()["error"]["code"] == "CONFIG_YAML_INVALID"
        assert not (ws_root / "project.yaml").exists()

    def test_dedicated_put_extra_field_returns_422(
        self, client: TestClient, workspaces_root: Path
    ) -> None:
        """generation body 含 extra 字段 → 422 VALIDATION_FAILED(extra=forbid)。"""
        _make_workspace_dir(workspaces_root, "ws_put_gen_extra")
        resp = client.put(
            "/api/v1/workspaces/ws_put_gen_extra/config/generation",
            json={"word_count_min": 2000, "unknown": 1},
        )
        assert resp.status_code == 422
        assert resp.json()["error"]["code"] == "VALIDATION_FAILED"

    def test_dedicated_put_workspace_not_found_returns_404(
        self, client: TestClient
    ) -> None:
        """工作区目录不存在 → 404 WORKSPACE_NOT_FOUND。"""
        resp = client.put(
            "/api/v1/workspaces/ws_gen_missing/config/generation",
            json={"word_count_min": 2000},
        )
        assert resp.status_code == 404
        assert resp.json()["error"]["code"] == "WORKSPACE_NOT_FOUND"


# =============================================================================
# v1.1 w3-s8:统一 PUT round-trip 修复 —— 不冲掉 L1 专属端点设的 generation 段
# =============================================================================


class TestUnifiedPutPreservesGeneration:
    """``PUT /config`` 统一端点保留 generation 段(w3-s8 ConfigPage round-trip)。

    对称于 ``TestGenerationConfigEndpoint.test_dedicated_put_preserves_other_sections``
    (专属 PUT 不冲掉 reviewer);本类验反向 —— 统一 PUT 不冲掉 generation。
    """

    def test_unified_put_writer_only_preserves_generation(
        self, client: TestClient, workspaces_root: Path
    ) -> None:
        """核心修复:先 L1 设 generation,再统一 PUT 只发 writer 段 → generation 保留。

        复现旧 bug 场景:ConfigPage 存 writer profile 时把 L1 设的生成参数静默冲掉。
        """
        _make_workspace_dir(workspaces_root, "ws_unified_keep_gen")
        # L1 专属端点先设 generation 覆盖
        client.put(
            "/api/v1/workspaces/ws_unified_keep_gen/config/generation",
            json={"word_count_min": 3000, "max_rewrites": 5},
        )
        # ConfigPage 走统一 PUT 只发 writer_routing(不提 generation)
        resp = client.put(
            "/api/v1/workspaces/ws_unified_keep_gen/config",
            json={"writer_routing": {"default": "cloud_quality"}},
        )
        assert resp.status_code == 200, resp.text
        merged = resp.json()["data"]
        # writer 段切换生效
        assert merged["routing"]["default"] == "cloud_quality"
        # generation 段被保留(旧 bug 下会回引擎默认 2500/3)
        assert merged["generation"]["word_count_min"] == 3000
        assert merged["generation"]["max_rewrites"] == 5
        # 落盘 yaml 仍含 generation 段
        content = (
            workspaces_root / "ws_unified_keep_gen" / "project.yaml"
        ).read_text(encoding="utf-8")
        assert "generation:" in content
        assert "word_count_min: 3000" in content

    def test_unified_put_explicit_generation_replaces(
        self, client: TestClient, workspaces_root: Path
    ) -> None:
        """统一 PUT body 显式给 generation 值 → 替换生效(API client 整体 round-trip)。"""
        _make_workspace_dir(workspaces_root, "ws_unified_gen_explicit")
        client.put(
            "/api/v1/workspaces/ws_unified_gen_explicit/config/generation",
            json={"word_count_min": 3000},
        )
        resp = client.put(
            "/api/v1/workspaces/ws_unified_gen_explicit/config",
            json={
                "writer_routing": {"default": "cloud_quality"},
                "generation": {"word_count_min": 2200},
            },
        )
        assert resp.status_code == 200, resp.text
        assert resp.json()["data"]["generation"]["word_count_min"] == 2200

    def test_unified_put_explicit_null_generation_clears(
        self, client: TestClient, workspaces_root: Path
    ) -> None:
        """统一 PUT body 显式 ``generation: null`` → 清空该段回引擎默认(2500)。"""
        _make_workspace_dir(workspaces_root, "ws_unified_gen_clear")
        client.put(
            "/api/v1/workspaces/ws_unified_gen_clear/config/generation",
            json={"word_count_min": 3000},
        )
        resp = client.put(
            "/api/v1/workspaces/ws_unified_gen_clear/config",
            json={
                "writer_routing": {"default": "cloud_quality"},
                "generation": None,
            },
        )
        assert resp.status_code == 200, resp.text
        # 显式 null → 清空 → 回引擎默认
        assert resp.json()["data"]["generation"]["word_count_min"] == 2500


# =============================================================================
# W3.5-s5:routable-agents profile_meta 校准状态(带 model_calibration 段的引擎 yaml)
# =============================================================================
#
# 注:``_build_routable_agents_list`` 的校准读 ``settings.config_yaml_path``(引擎层,
# 与生成期 ``outline_service._resolve_length_calibration`` 同源),与 profiles 的来源
# 须为同一文件才贴生产 —— 故本类 fixture 同时把 config_service 与 settings.config_yaml_path
# 指向含 model_calibration 段的同一 tmp 文件。
_CALIB_ENGINE_YAML = """\
writer_profiles:
  local_qwen_32k:
    provider: ollama
    model: qwen-novel
    base_url: http://localhost:11434
    context_window: 24576
    generation_params: {temperature: 0.7}
    extra_request_kwargs: {think: false}
  cloud_deepseek_v4_pro:
    provider: openai_compatible
    model: deepseek-v4-pro
    base_url: https://example/v1
    context_window: 1000000
    generation_params: {temperature: 0.85}
    extra_request_kwargs: {enable_thinking: false}
  cloud_deepseek_v4_pro_thinking:
    provider: openai_compatible
    model: deepseek-v4-pro
    base_url: https://example/v1
    context_window: 1000000
    generation_params: {temperature: 0.7}
    extra_request_kwargs: {enable_thinking: true}

writer_routing:
  default: local_qwen_32k
  agent_routing:
    writer: cloud_deepseek_v4_pro

model_calibration:
  deepseek-v4-pro:
    calibrated_at: "2026-06-16"
    calibration_corpus: "test corpus"
    global:
      length_tendency: overshoot
      anti_stamp_strength: high
      recommended_temperature: 0.85
      thinking_default: false
      reviewer_threshold_adjustment: 0.0
    per_genre:
      action:
        chars_per_beat: 1788
        chars_per_chapter: 5364
        length_compliance: 0.333
        content_units_per_chapter: 3.0
        sample_chapters: 6
"""


class TestRoutableAgentsCalibrationMetaW3S5:
    """W3.5-s5:``profile_meta`` 校准状态 + openai_compatible thinking 暴露。

    覆盖前端"校准状态显示 / 未校准警告"(plan §4.1 ②)+ "judge 硬护栏 UI 拦截"
    (thinking_on 对 openai_compatible provider)的**后端契约**:
    - 已校准模型(``model_calibration`` 段含)→ ``calibrated=True`` + ``calibrated_at``;
      同模型多 profile(standard/thinking)共享同一校准(keyed by 模型名);
    - 未校准模型(qwen)→ ``calibrated=False`` / ``calibrated_at=None``;
    - openai_compatible ``enable_thinking`` 真实反映到 ``thinking_on``;
    - ``model_calibration`` 段脏(未知题材原型)→ 422 ``CONFIG_YAML_INVALID``
      (load_model_calibration fail-fast,不吞坏 config;护 try 块内构建)。
    """

    @pytest.fixture()
    def calib_client(self, tmp_path: Path) -> Iterator[TestClient]:
        cfg = tmp_path / "config.yaml"
        cfg.write_text(_CALIB_ENGINE_YAML, encoding="utf-8")
        ws_root = tmp_path / "workspaces"
        ws_root.mkdir()

        from src.config import settings

        original = settings.config_yaml_path
        settings.config_yaml_path = cfg
        svc = ConfigService(engine_config_path=cfg, workspaces_root=ws_root)
        app = create_app()
        app.dependency_overrides[get_config_service] = lambda: svc
        try:
            with TestClient(app) as c:
                yield c
        finally:
            app.dependency_overrides.clear()
            settings.config_yaml_path = original

    def test_calibrated_model_exposes_calibrated_at(
        self, calib_client: TestClient
    ) -> None:
        """已校准模型 → calibrated=True + calibrated_at;同模型多 profile 共享校准。"""
        resp = calib_client.get("/api/v1/config/routable-agents")
        assert resp.status_code == 200, resp.text
        meta = resp.json()["data"]["profile_meta"]
        # deepseek standard + thinking 共享同模型校准(keyed by 模型名)
        assert meta["cloud_deepseek_v4_pro"]["calibrated"] is True
        assert meta["cloud_deepseek_v4_pro"]["calibrated_at"] == "2026-06-16"
        assert meta["cloud_deepseek_v4_pro_thinking"]["calibrated"] is True
        assert (
            meta["cloud_deepseek_v4_pro_thinking"]["calibrated_at"] == "2026-06-16"
        )
        # qwen 未校准
        assert meta["local_qwen_32k"]["calibrated"] is False
        assert meta["local_qwen_32k"]["calibrated_at"] is None

    def test_openai_compatible_thinking_on_reflected(
        self, calib_client: TestClient
    ) -> None:
        """openai_compatible enable_thinking 真实反映到 thinking_on(judge 拦截依据)。"""
        resp = calib_client.get("/api/v1/config/routable-agents")
        assert resp.status_code == 200, resp.text
        meta = resp.json()["data"]["profile_meta"]
        assert meta["cloud_deepseek_v4_pro"]["thinking_on"] is False
        assert meta["cloud_deepseek_v4_pro_thinking"]["thinking_on"] is True

    def test_malformed_calibration_section_returns_422(
        self, calib_client: TestClient, tmp_path: Path
    ) -> None:
        """model_calibration 段脏(未知题材原型)→ 422,不漏成 500(护 try 块内构建)。"""
        cfg = tmp_path / "config.yaml"
        bad = _CALIB_ENGINE_YAML.replace("      action:", "      bogus_archetype:")
        cfg.write_text(bad, encoding="utf-8")
        resp = calib_client.get("/api/v1/config/routable-agents")
        assert resp.status_code == 422, resp.text
        assert resp.json()["error"]["code"] == "CONFIG_YAML_INVALID"
