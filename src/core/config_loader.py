"""引擎默认配置(``config.yaml``)加载器(P3-B-01 引入)。

职责
====
- 读取仓库根 ``config.yaml``(或自定义路径),把顶层 ``writer_profiles`` /
  ``writer_routing`` / ``agent_routing`` 三段映射到 ``WriterProfilesConfig``
  Pydantic 模型,获得 ``profiles`` dict + ``routing`` 对象。
- yaml 缺失字段 / 类型错 / routing 引用未定义 profile,本模块统一通过 Pydantic
  ``ValidationError`` 报错,caller 不需要处理 yaml 字符串细节。

不做什么
========
- 工作区 ``project.yaml`` 覆盖(留 P3-B-02 ``services/config_service.py``)。
- ``ModelRouter`` 实现(留 P3-B-05)。
- 运行时 Profile 切换(留 P3-F-03 + P3-B-09)。

设计决策
========
- yaml 顶层结构与 PROJECT_SPEC §7 字面对齐:``writer_profiles`` 是 dict,
  ``writer_routing`` 是包含 ``default`` / ``rules`` 的 dict;P3-plan 决策 1
  扩 ``agent_routing`` dict 并入 ``writer_routing`` 段以下(对应
  ``RoutingConfig.agent_routing`` 字段)。
- 文件不存在时 caller 决定是 fail-fast 还是 fallback,本模块抛 ``FileNotFoundError``,
  不静默回退默认值——避免"用户改了 yaml 但 typo 路径,系统跑默认配置"难排查。
"""

from __future__ import annotations

import copy
from pathlib import Path
from typing import Any

import yaml

from src.core.models.model_calibration import ModelCalibration
from src.core.models.run_model_manifest import RunModelManifestCatalog
from src.core.models.routing import RoutingConfig, WriterProfilesConfig
from src.core.models.writer_profile import WriterProfile


def load_writer_profiles_config(path: Path) -> WriterProfilesConfig:
    """从 yaml 文件加载并校验 ``WriterProfilesConfig``。

    :param path: yaml 文件路径(绝对或相对当前工作目录)。
    :return: 已校验的 ``WriterProfilesConfig``,profiles 字典 + routing 对象。
    :raises FileNotFoundError: 文件不存在(caller 显式处理,不静默回退)。
    :raises pydantic.ValidationError: yaml 字段类型错 / 缺失必填字段 / routing
        引用未定义 profile / profiles dict key 与 profile_id 不一致 等。
    :raises yaml.YAMLError: yaml 语法错。

    yaml 期望结构(对齐 PROJECT_SPEC §7 + P3-plan 决策 1)::

        writer_profiles:
          cloud_quality:
            provider: claude
            model: claude-sonnet-4-6
            context_window: 200000
            generation_params: {temperature: 0.85, max_tokens: 6000}
            cost_per_1k_input: 0.003
            cost_per_1k_output: 0.015
          local_qwen_32k:
            provider: ollama
            model: qwen-novel
            base_url: http://localhost:11434
            context_window: 24576
            generation_params: {temperature: 0.7, num_predict: 4096}
            extra_request_kwargs: {think: false}

        writer_routing:
          default: local_qwen_32k
          rules:
            - condition: "is_arc_finale == True"
              profile: cloud_premium
            - condition: "tension_level >= 9"
              profile: cloud_premium
          agent_routing:
            writer: local_qwen_32k
            reviewer: local_qwen_32k
            memory_extractor: local_qwen_32k
    """
    if not path.exists():
        raise FileNotFoundError(
            f"config.yaml 不存在: {path}。"
            "P3 阶段引擎默认 profile / routing 配置必须存在;"
            "若想跑默认 profile,复制仓库根 ``config.yaml`` 到指定路径。"
        )

    with path.open("r", encoding="utf-8") as fp:
        raw: dict[str, Any] = yaml.safe_load(fp) or {}

    if not isinstance(raw, dict):
        raise ValueError(
            f"config.yaml 顶层应为映射(dict),收到 {type(raw).__name__}。文件: {path}"
        )

    profiles_raw = raw.get("writer_profiles", {})
    routing_raw = raw.get("writer_routing", {})

    if not isinstance(profiles_raw, dict):
        raise ValueError(
            f"writer_profiles 段应为 dict,收到 {type(profiles_raw).__name__}"
        )
    if not isinstance(routing_raw, dict):
        raise ValueError(
            f"writer_routing 段应为 dict,收到 {type(routing_raw).__name__}"
        )

    # 把 profile_id 注入每个 profile dict(yaml 里 key 是 profile_id,
    # 但 WriterProfile 模型要求显式字段)。
    profiles: dict[str, WriterProfile] = {}
    for profile_id, profile_payload in profiles_raw.items():
        if not isinstance(profile_payload, dict):
            raise ValueError(
                f"writer_profiles[{profile_id!r}] 应为 dict,"
                f"收到 {type(profile_payload).__name__}"
            )
        # 若 yaml 字面已写 profile_id,优先用 yaml 字面值(交叉校验在
        # WriterProfilesConfig 落地);若没写,用 dict key 兜底。
        merged = {"profile_id": profile_id, **profile_payload}
        profiles[profile_id] = WriterProfile.model_validate(merged)

    routing = RoutingConfig.model_validate(routing_raw)

    return WriterProfilesConfig(profiles=profiles, routing=routing)


def load_run_model_manifest_catalog(path: Path) -> RunModelManifestCatalog:
    """Load the optional central single-model activity selection.

    The section lives only in the engine config.  Workspace ``project.yaml``
    overlays intentionally cannot replace it, so an individual workspace or
    agent route cannot escape the activity-wide authority.  Missing section is
    backward-compatible and means no manifest lock is active.
    """

    if not path.exists():
        raise FileNotFoundError(f"config.yaml 不存在: {path}")
    with path.open("r", encoding="utf-8") as fp:
        raw: Any = yaml.safe_load(fp) or {}
    if not isinstance(raw, dict):
        raise ValueError(
            f"config.yaml 顶层应为映射(dict),收到 {type(raw).__name__}。文件: {path}"
        )
    section = raw.get("run_model_manifest")
    if section is None:
        return RunModelManifestCatalog()
    if not isinstance(section, dict):
        raise ValueError("run_model_manifest 段必须是 dict")
    return RunModelManifestCatalog.model_validate(copy.deepcopy(section))


def load_model_calibration(path: Path) -> dict[str, ModelCalibration]:
    """从 yaml 文件加载顶层 ``model_calibration:`` 段(W3.5-s2 引入)。

    :param path: yaml 文件路径(同 ``load_writer_profiles_config``,通常仓库根 config.yaml)。
    :return: ``{模型名: ModelCalibration}`` dict;**段缺失返空 dict**(优雅,非报错)。
    :raises FileNotFoundError: 文件不存在(同 profiles loader,不静默回退)。
    :raises pydantic.ValidationError: ``model_calibration`` 段存在但字段类型错 / 缺必填 /
        题材原型 key 未知 等(present-but-malformed = fail-fast)。
    :raises ValueError: ``model_calibration`` 段非 dict / 某模型条目非 dict。

    设计:**段缺失 = 优雅空**(对齐 plan §4.1 "未校准警告"语义 —— 大多数旧工作区 / 旧
    config 无本段,消费方〔s3 拆章 / s5 路由〕按 model id 未命中时**警告**而非崩溃);
    但**段存在则严格校验**(脏数据 fail-fast,避免拆章读到坏 chars_per_beat 静默产错章数)。

    yaml 期望结构(plan §3.3,**keyed by 模型名**非 profile id)::

        model_calibration:
          deepseek-v4-pro:
            calibrated_at: "2026-06-16"
            calibration_corpus: "heal-off-v1 + luo-accept-v2 modelab_drafts"
            global:
              length_tendency: overshoot
              anti_stamp_strength: high
              recommended_temperature: 0.85
              thinking_default: false
              reviewer_threshold_adjustment: 0.0
            per_genre:
              introspection: {chars_per_beat: 1534, chars_per_chapter: 4602, ...}
              action:        {chars_per_beat: 1788, chars_per_chapter: 5364, ...}
    """
    if not path.exists():
        raise FileNotFoundError(
            f"config.yaml 不存在: {path}。load_model_calibration 与 "
            "load_writer_profiles_config 同源,不静默回退默认。"
        )

    with path.open("r", encoding="utf-8") as fp:
        raw: dict[str, Any] = yaml.safe_load(fp) or {}

    if not isinstance(raw, dict):
        raise ValueError(
            f"config.yaml 顶层应为映射(dict),收到 {type(raw).__name__}。文件: {path}"
        )

    calibration_raw = raw.get("model_calibration")
    if calibration_raw is None:
        return {}  # 段缺失 = 优雅空,消费方按 model 未命中警告

    if not isinstance(calibration_raw, dict):
        raise ValueError(
            f"model_calibration 段应为 dict,收到 {type(calibration_raw).__name__}"
        )

    calibrations: dict[str, ModelCalibration] = {}
    for model_name, payload in calibration_raw.items():
        if not isinstance(payload, dict):
            raise ValueError(
                f"model_calibration[{model_name!r}] 应为 dict,"
                f"收到 {type(payload).__name__}"
            )
        # by_alias:yaml 字面 'global' → 模型 global_ 字段(alias='global')。
        calibrations[model_name] = ModelCalibration.model_validate(payload)

    manifest_section = raw.get("run_model_manifest") or {}
    active = manifest_section.get("active") if isinstance(manifest_section, dict) else None
    manifests = manifest_section.get("manifests", {}) if isinstance(manifest_section, dict) else {}
    selected = manifests.get(active, {}) if isinstance(manifests, dict) else {}
    if not isinstance(selected, dict):
        selected = {}
    if selected.get("pipeline_contract") == "creative_delta_v6":
        from src.core.models.calibration_binding import writer_calibration_contract_hash
        from src.core.models.run_model_manifest import RunModelManifest

        manifest = RunModelManifest.model_validate(selected)
        calibration = calibrations.get(manifest.exact_model_id)
        if calibration is not None and calibration.writer_contract_hash != writer_calibration_contract_hash(manifest):
            # Existing consumers consistently see "uncalibrated": UI, outline
            # admission and optional trim all share this loader. Keep the old
            # source YAML untouched; never synthesize observations or relax the
            # formal outline's pre-Provider calibration gate.
            calibrations.pop(manifest.exact_model_id)
    return calibrations
