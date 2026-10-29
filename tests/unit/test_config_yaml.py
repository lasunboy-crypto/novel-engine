"""P3-B-01:``src/core/config_loader.py:load_writer_profiles_config`` 单元测试。

测试矩阵
========
- 仓库根真实 ``config.yaml`` 加载成功 + 字段对齐
- 自定义 yaml(临时文件)加载 + 字段断言
- 文件不存在 → FileNotFoundError
- yaml 顶层非 dict → ValueError
- writer_profiles 段非 dict → ValueError
- writer_profiles 内 profile 非 dict → ValueError
- 引用未定义 profile → ValidationError(交叉校验,沿用 WriterProfilesConfig 错误信息)
- yaml 字面 profile_id 与 dict key 不一致 → ValidationError
"""

from __future__ import annotations

from pathlib import Path

import pytest
from pydantic import ValidationError

from src.core.config_loader import load_writer_profiles_config


# ---------- 辅助:写临时 yaml ----------


def _write_yaml(tmp_path: Path, content: str) -> Path:
    p = tmp_path / "test_config.yaml"
    p.write_text(content, encoding="utf-8")
    return p


_MINIMAL_VALID_YAML = """
writer_profiles:
  local_qwen_32k:
    provider: ollama
    model: qwen-novel
    base_url: http://localhost:11434
    context_window: 24576
    extra_request_kwargs:
      think: false

writer_routing:
  default: local_qwen_32k
"""


# ---------- 真实仓库 config.yaml ----------


def test_repo_root_config_yaml_loads(tmp_path: Path) -> None:
    """仓库根 ``config.yaml`` 必须能加载成功(P3 启动入口)。"""
    repo_root = Path(__file__).resolve().parent.parent.parent
    cfg_path = repo_root / "config.yaml"
    assert cfg_path.exists(), f"仓库根缺 config.yaml: {cfg_path}"

    cfg = load_writer_profiles_config(cfg_path)

    # 8 个示例 profile 全在
    # P7-B-06 起 +local_qwen_reviewer 低温审稿专用
    # v1.0.x post-wave5 H1 起 +cloud_qwen_max_thinking / cloud_qwen_max_standard
    # beat 批量章节卡 profile 已随能力退役；只保留仍可独立使用的标准/思考 profile。
    # v2 W3.5 任务A(2026-07-04):DeepSeek 转为 parked 历史实验资产,生产 agent_routing
    # 不再引用 cloud_deepseek_*;profile 仍需可解析,避免历史 acceptance / 旧测试断裂。
    expected_profile_ids = {
        "local_qwen_32k",
        "local_qwen_safe",
        "local_qwen_reviewer",
        "cloud_quality",
        "cloud_premium",
        "cloud_qwen_max_thinking",
        "cloud_qwen_max_standard",
        "cloud_qwen_max_medium_creative",
        "cloud_qwen_max_xhigh",
        "cloud_qwen_max_low_thinking",
        "cloud_qwen_max_low_standard",
        "cloud_deepseek_v4_pro",
        "cloud_deepseek_v4_pro_thinking",
        "mock_default",
    }
    assert set(cfg.profiles.keys()) == expected_profile_ids

    # v1.0.x wave 6 s2.5(2026-05-25):routing.default 切云端 dashscope
    # qwen3.8-max(原 local_qwen_32k);维护人 .env 配 DASHSCOPE_API_KEY 后跑
    # 云端实证。切回本地需同步改回 + 改 .env LLM_PROVIDER=ollama。
    assert cfg.routing.default == "cloud_qwen_max_medium_creative"

    # rules 默认置空(v1.1 wave 4 B1 2026-06-03 维护人拍板,对齐 PROJECT_SPEC §7):
    # 原 arc_finale + tension>=9 → cloud_premium 两条遗留规则已移除 — 维护人环境
    # 永久无 Claude key,这两条只靠 fallback 兜底且会被无效非空 CLAUDE_API_KEY
    # 误判可用而硬崩(B1 ch-3 S1 实证)。cloud_premium profile 定义保留(有 key
    # 的部署可自加规则启用)。详 PROJECT_SPEC §7 writer_routing 段说明。
    assert cfg.routing.rules == []

    # v1.0.x wave 6 s2.5:agent_routing 创作 + 审稿全切云端
    # v1.1 wave 2 H2(2026-05-28):拆 _maybe_fallback silent 升级 → 真混跑解锁
    # v1.1 wave 2 config-unify-cloud(2026-05-28):14 agent **全量切云端**,
    # 取消 4 个向量库相关 agent 的本地 ollama 路径(维护人动机 = 后续开发期
    # 行为统一可预测);切回本地见 config.yaml 上方主注释。
    # v2 W3.5 任务A(2026-07-04):生产 active routing 全量回 Qwen。DeepSeek
    # profile 保留 parked,但不得被生产 agent_routing 引用。
    assert cfg.routing.agent_routing["writer"] == "cloud_qwen_max_medium_creative"
    assert cfg.routing.agent_routing["reviewer"] == "cloud_qwen_max_thinking"
    # memory_extractor 与 Writer 同属中等思考/原 0.85 温度组。
    assert (
        cfg.routing.agent_routing["memory_extractor"]
        == "cloud_qwen_max_medium_creative"
    )
    # continuity/reranker/summary 使用 4K 低思考组。
    assert cfg.routing.agent_routing["continuity"] == "cloud_qwen_max_low_standard"
    assert cfg.routing.agent_routing["reranker"] == "cloud_qwen_max_low_standard"
    assert (
        cfg.routing.agent_routing["chapter_summary_extractor"]
        == "cloud_qwen_max_low_standard"
    )
    # must_achieve_validator 属 hard judge,任务 A 后仍必须走
    # cloud_qwen_max_thinking;完整 29 工种断言见
    # test_w3_5_task_a_production_routing_uses_qwen_only。
    assert (
        cfg.routing.agent_routing["must_achieve_validator"] == "cloud_qwen_max_thinking"
    )

    # ollama profile 字段断言
    qwen_32k = cfg.profiles["local_qwen_32k"]
    assert qwen_32k.provider == "ollama"
    assert qwen_32k.model == "qwen-novel"
    assert qwen_32k.extra_request_kwargs == {"think": False}
    assert qwen_32k.cost_per_1k_input == 0.0
    assert qwen_32k.cost_currency == "USD"

    # claude profile 字段断言
    cloud_premium = cfg.profiles["cloud_premium"]
    assert cloud_premium.provider == "claude"
    assert cloud_premium.model == "claude-opus-4-7"
    assert cloud_premium.cost_per_1k_input > 0
    assert cloud_premium.cost_currency == "USD"

    active_qwen_profiles = {
        profile_id
        for profile_id in expected_profile_ids
        if profile_id.startswith("cloud_qwen_max_")
    }
    assert active_qwen_profiles
    assert {
        cfg.profiles[profile_id].cost_currency
        for profile_id in active_qwen_profiles
    } == {"RMB"}

    # DeepSeek profile 字段 + 真实定价断言(profile 定义保留作 parked 历史实验资产,
    # 生产路由不得引用)。维护人 2026-06-15 确认百炼 RMB ¥3/¥6 每百万 → USD/1k
    # ÷7.2;原占位 0.004/0.012 高约 10x,锁定真实价防回退(影响 W3.5 大跑 cost_guard 准确度)。
    deepseek = cfg.profiles["cloud_deepseek_v4_pro"]
    assert deepseek.provider == "openai_compatible"
    assert deepseek.model == "deepseek-v4-pro"
    assert deepseek.cost_per_1k_input == 0.00042  # ¥3/百万 ÷7.2
    assert deepseek.cost_per_1k_output == 0.00083  # ¥6/百万 ÷7.2
    assert deepseek.cost_currency == "USD"
    # deepseek-v4-pro 是 reasoning 模型,必须显式关 thinking(对照实验单变量;正文不要思考链)
    assert deepseek.extra_request_kwargs == {"enable_thinking": False}
    assert "PARKED" in deepseek.notes
    # W3.5-s2:context_window 修正 131072(s1 占位错值)→ 1000000(百炼真实 1M,
    # 维护人 2026-06-16 控制台截图核实;输出上限 384K ≫ qwen 64K)。锁定防回退。
    assert deepseek.context_window == 1000000

    # W3.5-s2:cloud_deepseek_v4_pro_thinking 思考版 profile(镜像 cloud_qwen_max_thinking)
    # judge 硬护栏(router.py:231)要求 16 judge 走 thinking 版 → 必建此 profile。
    # 镜像方针:thinking on + 温度 0.7(qwen_thinking,非 standard 0.85);其余同 deepseek。
    deepseek_thinking = cfg.profiles["cloud_deepseek_v4_pro_thinking"]
    assert deepseek_thinking.provider == "openai_compatible"
    assert deepseek_thinking.model == "deepseek-v4-pro"  # 同模型,只换 thinking
    assert deepseek_thinking.extra_request_kwargs == {"enable_thinking": True}
    assert (
        deepseek_thinking.generation_params["temperature"] == 0.7
    )  # 镜像 qwen_thinking
    assert deepseek_thinking.context_window == 1000000  # 真实 1M
    assert deepseek_thinking.cost_per_1k_input == 0.00042  # 同模型同价
    assert deepseek_thinking.cost_per_1k_output == 0.00083
    assert "PARKED" in deepseek_thinking.notes

# ---------- v2 W3.5 任务A:DeepSeek 生产路由回退契约 ----------


def test_w3_5_task_a_production_routing_uses_qwen_only() -> None:
    """29 个 active agent 全量 Qwen，并按真实负载分层思考/输入预算。"""
    repo_root = Path(__file__).resolve().parent.parent.parent
    cfg = load_writer_profiles_config(repo_root / "config.yaml")
    ar = cfg.routing.agent_routing

    medium_creative_agents = {
        "writer",
        "reviser",
        "memory_extractor",
    }
    low_standard_agents = {
        "continuity",
        "reranker",
        "chapter_summary_extractor",
    }
    xhigh_agents = {"mini_plan_generator", "outline_builder"}
    medium_semantic_agents = {
        "chapter_card_generator",
        "reviewer",
        "must_achieve_validator",
        "beat_alignment_validator",
        "must_vs_forbidden_validator",
        "plot_thread_merger",
        "character_disambiguator",
        "timeline_entry_merger",
        "mechanism_consistency_validator",
        "thread_closure_validator",
        "identity_coverage_validator",
        "setting_consistency_validator",
        "scene_anchor_validator",
        "volume_seam_replay_validator",
        "must_feasibility_validator",
        "outline_consistency_validator",
        "forbidden_crossing_validator",
    }
    low_semantic_agents = {
        "continuity_validator",
        "style_auditor",
        "cross_chapter_dedup_validator",
        "ending_tension_validator",
    }

    assert len(medium_creative_agents) == 3
    assert len(low_standard_agents) == 3
    assert len(xhigh_agents) == 2
    assert len(medium_semantic_agents) == 17
    assert len(low_semantic_agents) == 4
    assert cfg.routing.default == "cloud_qwen_max_medium_creative"

    expected_routes = {
        **{agent: "cloud_qwen_max_medium_creative" for agent in medium_creative_agents},
        **{agent: "cloud_qwen_max_low_standard" for agent in low_standard_agents},
        **{agent: "cloud_qwen_max_xhigh" for agent in xhigh_agents},
        **{agent: "cloud_qwen_max_thinking" for agent in medium_semantic_agents},
        **{agent: "cloud_qwen_max_low_thinking" for agent in low_semantic_agents},
    }
    assert ar == expected_routes

    active_profile_ids = set(expected_routes.values())
    assert len(active_profile_ids) == 5
    for profile_id in active_profile_ids:
        profile = cfg.profiles[profile_id]
        assert profile.model == "qwen3.8-max"
        assert profile.context_window == 1_000_000
        assert profile.cost_per_1k_input == 0.012
        assert profile.cost_per_1k_output == 0.036
        assert profile.extra_request_kwargs["enable_thinking"] is True
        assert profile.extra_request_kwargs["preserve_thinking"] is False
        assert "reasoning_effort" not in profile.extra_request_kwargs

    for profile_id in {"cloud_qwen_max_thinking", "cloud_qwen_max_medium_creative"}:
        profile = cfg.profiles[profile_id]
        assert profile.extra_request_kwargs["thinking_budget"] == 16_384
        assert profile.extra_request_kwargs["input_token_budget"] == 524_288
    assert (
        cfg.profiles["cloud_qwen_max_medium_creative"].generation_params["temperature"]
        == 0.85
    )
    assert (
        cfg.profiles["cloud_qwen_max_thinking"].generation_params["temperature"] == 0.7
    )

    xhigh = cfg.profiles["cloud_qwen_max_xhigh"]
    assert xhigh.extra_request_kwargs["thinking_budget"] == 32_768
    assert xhigh.extra_request_kwargs["input_token_budget"] == 524_288

    for profile_id in {"cloud_qwen_max_low_thinking", "cloud_qwen_max_low_standard"}:
        profile = cfg.profiles[profile_id]
        assert profile.extra_request_kwargs["thinking_budget"] == 4_096
        assert profile.extra_request_kwargs["input_token_budget"] == 131_072

    assert len(ar) == 29, f"agent_routing 应有 29 工种,实际 {len(ar)}"
    assert set(ar) == set(expected_routes)

    deepseek_count = sum(1 for v in ar.values() if v.startswith("cloud_deepseek"))
    qwen_count = sum(1 for v in ar.values() if v.startswith("cloud_qwen"))
    assert deepseek_count == 0, (
        f"生产 agent_routing 不得引用 cloud_deepseek_*;实际 {deepseek_count}"
    )
    assert qwen_count == 29, f"生产 agent_routing 应全量 Qwen,实际 {qwen_count}"

    # 兼容 profile 仍可解析，但不能成为当前 active route。
    standard_profile = cfg.profiles["cloud_qwen_max_standard"]
    assert standard_profile.extra_request_kwargs["enable_thinking"] is False
    assert standard_profile.extra_request_kwargs["preserve_thinking"] is False
    assert "cloud_qwen_max_standard" not in set(ar.values())

    deepseek_profiles = {
        "cloud_deepseek_v4_pro",
        "cloud_deepseek_v4_pro_thinking",
    }
    assert deepseek_profiles.issubset(cfg.profiles)
    for profile_id in deepseek_profiles:
        assert "PARKED" in cfg.profiles[profile_id].notes


# ---------- 临时 yaml ----------


def test_minimal_yaml_loads(tmp_path: Path) -> None:
    """最小合法 yaml(1 profile + 1 default,无 rules / agent_routing)。"""
    cfg_path = _write_yaml(tmp_path, _MINIMAL_VALID_YAML)
    cfg = load_writer_profiles_config(cfg_path)
    assert "local_qwen_32k" in cfg.profiles
    assert cfg.routing.default == "local_qwen_32k"
    assert cfg.routing.rules == []
    assert cfg.routing.agent_routing == {}


# ---------- 错误路径 ----------


def test_file_not_found(tmp_path: Path) -> None:
    """yaml 文件不存在 → FileNotFoundError(不静默回退)。"""
    cfg_path = tmp_path / "nope.yaml"
    with pytest.raises(FileNotFoundError, match="config.yaml 不存在"):
        load_writer_profiles_config(cfg_path)


def test_yaml_top_level_not_mapping(tmp_path: Path) -> None:
    """yaml 顶层是 list / 标量 → ValueError。"""
    cfg_path = _write_yaml(tmp_path, "- foo\n- bar\n")
    with pytest.raises(ValueError, match="顶层应为映射"):
        load_writer_profiles_config(cfg_path)


def test_writer_profiles_non_dict(tmp_path: Path) -> None:
    cfg_path = _write_yaml(
        tmp_path,
        "writer_profiles: [not, a, dict]\nwriter_routing:\n  default: x\n",
    )
    with pytest.raises(ValueError, match="writer_profiles 段应为 dict"):
        load_writer_profiles_config(cfg_path)


def test_profile_payload_non_dict(tmp_path: Path) -> None:
    cfg_path = _write_yaml(
        tmp_path,
        "writer_profiles:\n  bad: 'not a dict'\nwriter_routing:\n  default: bad\n",
    )
    with pytest.raises(ValueError, match=r"writer_profiles\['bad'\] 应为 dict"):
        load_writer_profiles_config(cfg_path)


def test_routing_default_references_undefined_profile(tmp_path: Path) -> None:
    cfg_path = _write_yaml(
        tmp_path,
        """
writer_profiles:
  p1:
    provider: ollama
    model: qwen-novel
    context_window: 16000

writer_routing:
  default: nonexistent
""",
    )
    with pytest.raises(ValidationError, match="writer_routing.default"):
        load_writer_profiles_config(cfg_path)


def test_yaml_explicit_profile_id_mismatch_dict_key(tmp_path: Path) -> None:
    """yaml dict key 是 ``p1``,但 profile 字面写 ``profile_id: p2`` → 拒绝。"""
    cfg_path = _write_yaml(
        tmp_path,
        """
writer_profiles:
  p1:
    profile_id: p2
    provider: ollama
    model: qwen-novel
    context_window: 16000

writer_routing:
  default: p1
""",
    )
    with pytest.raises(ValidationError, match="profile_id"):
        load_writer_profiles_config(cfg_path)


def test_empty_yaml(tmp_path: Path) -> None:
    """完全空 yaml(注释 + 空白)→ writer_profiles 缺失,WriterProfilesConfig 校验失败。"""
    cfg_path = _write_yaml(tmp_path, "# 空配置\n")
    with pytest.raises(ValidationError):
        load_writer_profiles_config(cfg_path)


# =============================================================================
# v1.1 wave 2 H3:AGENT_NAME Literal 完整性
# =============================================================================


def test_agent_name_literal_contains_all_router_callers() -> None:
    """AGENT_NAME Literal 字面必须覆盖 ``src/core/agents/*.py`` 内所有真走 ``router.route``
    的 agent。

    防御目标:wave 1+ 新加 LLM-driven agent / wave 1+ validator 时遗漏更新 Literal
    导致前端 UI 拿不到完整可路由 agent 清单(v1.1 wave 2 H3 根因)。

    方法:从源文件 grep ``agent_name=`` / ``agent_name = "...``"`` 字面收集"实际
    被路由的 agent"集合,与 AGENT_NAME Literal 取值集合对比。

    例外清单(显式排除)
    ---------------------
    - 纯规则 validator(``style_drift_validator`` / ``cross_chapter_voice_validator``)
      在 ``src/core/rules/`` 下,不调 LLM,不在 ``agents/`` 目录
    - (v1.1 w4-s5 / I2.2 起 ``style_auditor`` 已落地为真 LLM agent,走
      ``resolve_routing(agent_name="style_auditor")`` 且在 AGENT_NAME 内,不再排除)
    """
    import re
    from typing import get_args

    from src.core.models.routing import AGENT_NAME

    literal_set = set(get_args(AGENT_NAME))

    agents_dir = (
        Path(__file__).resolve().parent.parent.parent / "src" / "core" / "agents"
    )
    assert agents_dir.exists(), f"agents 目录不存在: {agents_dir}"

    # 收集所有 router.route("...") 调用 + agent_name="..." 字面常量
    pattern_route = re.compile(r"""router\.route\(\s*["']([a-z_]+)["']""")
    pattern_agent_name = re.compile(r"""agent_name\s*=\s*["']([a-z_]+)["']""")
    pattern_agent_name_const = re.compile(
        r"""^_(?:AGENT_NAME|OUTLINE_AGENT_NAME)\s*=\s*["']([a-z_]+)["']""",
        re.MULTILINE,
    )

    detected: set[str] = set()
    for py_file in agents_dir.glob("*.py"):
        text = py_file.read_text(encoding="utf-8")
        detected.update(pattern_route.findall(text))
        detected.update(pattern_agent_name.findall(text))
        detected.update(pattern_agent_name_const.findall(text))

    # detected 应是 literal_set 的子集(literal_set 允许更宽,含 reviewer_escalation
    # 等设计保留项;detected 是从代码字面捞的实际 caller)。
    missing = detected - literal_set
    assert not missing, (
        f"以下 agent 在代码中走 router 但未列入 AGENT_NAME Literal: {missing}。"
        f"修补:src/core/models/routing.py:AGENT_NAME"
    )
