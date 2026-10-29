"""工作区配置叠加服务(P3-B-02 引入,P6-B-12 扩写入路径,P6-B-13 扩 reviewer 段,P6-B-14 扩 retrieval 段,P6-B-15 扩 cost_guard 段)。

对齐 PROJECT_SPEC §10.1 三层配置加载顺序的层 1 → 层 2:

1. 引擎默认 ``config.yaml`` —— ``src.core.config_loader.load_writer_profiles_config``
2. 工作区覆盖 ``workspaces/<id>/project.yaml`` —— 本模块叠加 + 写入
3. 运行时覆盖(CLI / API 参数 / WriterProfileSelector)—— 留 P3-F-03

合并语义(P3-B-02 维护人 2026-05-09 拍 A)
==========================================

- ``writer_profiles``:**dict-level merge by key**。工作区写哪个 ``profile_id``
  即覆盖 / 新增哪个 profile,引擎默认其他 profile 保留。
- 单个 ``WriterProfile`` 内部:**整体替换,不字段级合并**。即工作区想改某个
  默认 profile 的 ``temperature``,必须重写完整 profile 字段集合(避免
  ``provider`` / ``cost_per_1k_*`` 等组合关系字段半残错配)。
- ``writer_routing.default``:单字段替换。
- ``writer_routing.rules``:整段替换(列表顺序敏感,不做部分合并)。
- ``writer_routing.agent_routing``:**dict-level merge by key**。工作区可单独
  覆盖某 agent 的 profile 绑定,其余 agent 保留引擎默认。
- ``reviewer``(P6-B-13):**字段级合并**。工作区填的字段覆盖,未填字段保留
  引擎默认;整段省略 → 走引擎默认。理由:reviewer 段只有 4 个 scalar 字段
  (无 dict / list 嵌套),字段级合并最自然 + 与 WriterProfile 整体替换有别
  (后者字段集多 + 组合关系强;reviewer 字段独立且语义清晰)。
- ``retrieval``(P6-B-14):**字段级合并**(同 reviewer)。retrieval 段只有 2
  个 scalar 字段(``top_k`` / ``min_similarity``),字段级合并最自然。
- ``cost_guard``(P6-B-15):**字段级合并**(同 reviewer / retrieval)。cost_guard
  段含 4 个 scalar 字段(``daily_usd_limit`` / ``monthly_usd_limit`` /
  ``per_workspace_daily_limit`` / ``action_on_exceed``)+ 1 个 list 字段
  (``notify_via``)。list 字段也走字段级 — 工作区给即整体替换,省略则保留
  引擎默认。

回退(fallback)语义
====================

- 工作区目录不存在 / ``project.yaml`` 不存在 → 返引擎默认配置(不抛错;
  "未提供覆盖" 等价于 "不覆盖")。
- 引擎默认 ``config.yaml`` 不存在 → ``FileNotFoundError`` 透传(由
  ``load_writer_profiles_config`` 抛出,fail-fast,不静默回退,因引擎默认是
  整套配置的兜底)。
- ``project.yaml`` 顶层非 dict / yaml 语法错 → 抛错。
- 合并后违反 ``WriterProfilesConfig`` 交叉校验(如覆盖后 ``default`` 引用
  未定义 profile)→ ``pydantic.ValidationError``。

P6-B-12 写入路径(``save_workspace_config``)
=============================================

- ``PUT /workspaces/{id}/config`` 调用入口;body 即工作区覆盖层 yaml dict 内容。
- **先内存校验后落盘**:把 overrides 与引擎默认合并跑一次,Pydantic /
  ``ValueError`` 失败则不写 yaml(原 ``project.yaml`` 不动),保证 yaml 落盘后
  下次 ``load_workspace_config`` 必合法。
- **P6-B-13 扩 reviewer 段校验**:若 overrides 含 ``reviewer:`` 段,额外跑
  ``_merge_reviewer`` 校验失败则不落盘(与 writer_profiles / writer_routing
  并列校验门);成功后 reviewer 段一并写入 ``project.yaml`` 顶层。
- **P6-B-14 扩 retrieval 段校验**:若 overrides 含 ``retrieval:`` 段,额外跑
  ``_merge_retrieval`` 校验失败则不落盘(与 reviewer 并列校验门);成功后
  retrieval 段一并写入 ``project.yaml`` 顶层。
- **P6-B-15 扩 cost_guard 段校验**:若 overrides 含 ``cost_guard:`` 段,额外
  跑 ``_merge_cost_guard`` 校验失败则不落盘(与 reviewer / retrieval 并列校验
  门);成功后 cost_guard 段一并写入 ``project.yaml`` 顶层。
- **写前备份原文件**:``project.yaml.bak`` 留作 IO 失败时的回滚锚点;成功后
  保留 .bak 供用户人工对照差异(7 天后由维护人手动清,本服务不自动清理)。
- **热加载语义**:写完 yaml 直接返回 ``load_workspace_config`` 重新读取后的
  ``WriterProfilesConfig``;``_make_router_factory`` 每次 LLM 调用重读 yaml,
  PUT 成功后下次任何 chapter:write / pipeline 调用即拿到新配置,**无需独立
  reload endpoint**(对齐 FRONTEND_SPEC §4.13 字面"保存后后端热加载"实际语义)。
- **WS event**:``workspace.{workspace_id}.events`` 频道发 ``config_updated``;
  publish 失败 swallow + structlog(对齐 ``BackupService._publish_workspace_event``
  同型)。
- **4 内置 profile 锁定**(``BUILTIN_PROFILE_IDS``):本服务**不**主动校验
  "工作区不可删 4 内置 ID"——dict-level merge by key 语义下,工作区根本无法
  通过 yaml 删除引擎默认的 profile(只能覆盖)。"内置不可删"约束**只在前端
  ConfigPage UI 层禁用删除按钮**(P6-F-04 自决项 10)。后端只暴露常量供 API
  schema / 前端类型生成对账,不做 yaml 落地校验。

不做什么
========

- ``WriterProfile`` 字段级深合并(自决项 3)。
- 运行时层 3 覆盖(留 P3-F-03)。
- 独立 reload endpoint(``_make_router_factory`` 每次重读 yaml 已天然热加载)。
"""

from __future__ import annotations

import shutil
from pathlib import Path
from typing import Any

import structlog
import yaml
from ruamel.yaml import YAML

from src.api.websocket.channels import workspace_events_channel
from src.config import settings
from src.core.config_loader import load_writer_profiles_config
from src.core.models.generation_config import GenerationConfig
from src.core.models.retrieval_config import RetrievalConfig
from src.core.models.reviewer_config import ReviewerConfig
from src.core.models.routing import (
    LLM_AS_JUDGE_AGENTS,
    RoutingConfig,
    WriterProfilesConfig,
    is_thinking_on_profile,
)
from src.core.models.writer_profile import WriterProfile
from src.core.workspace.workspace_paths import WorkspacePaths
from src.services.cost_guard import CostGuardConfig

_log = structlog.get_logger(__name__)

# 4 内置 profile_id(对齐 config.yaml writer_profiles 段 + plan §F-04 自决项 10)。
# 仅供前端 ConfigPage UI 禁用"删除按钮"对账 + API schema constants 暴露;
# 后端 yaml 合并语义本身保证内置 profile 无法被工作区删除(dict-level merge
# 是加法不是替换全集),所以不在 save_workspace_config 内做硬校验。
BUILTIN_PROFILE_IDS: frozenset[str] = frozenset(
    {
        "cloud_quality",
        "cloud_premium",
        "local_qwen_32k",
        "local_qwen_safe",
    }
)


class ConfigService:
    """工作区配置叠加服务。

    实例化时绑定引擎默认 ``config.yaml`` 路径与 ``workspaces_root``;两个参数
    缺省都从 ``settings`` 推算,允许测试用 ``tmp_path`` 显式注入。

    ``ws_manager`` 默认 ``None``;P6-B-12 ``save_workspace_config`` 完成时若注入
    则 publish ``config_updated`` 事件到 ``workspace.{workspace_id}.events`` 频道,
    publish 失败 swallow + structlog,不阻塞 yaml 落盘的成功返回(对齐
    ``BackupService._publish_workspace_event`` 同型)。
    """

    def __init__(
        self,
        *,
        engine_config_path: Path | None = None,
        workspaces_root: Path | None = None,
        ws_manager: object | None = None,
    ) -> None:
        self._engine_config_path = (
            Path(engine_config_path)
            if engine_config_path is not None
            else settings.config_yaml_path
        )
        self._workspaces_root = (
            Path(workspaces_root)
            if workspaces_root is not None
            else settings.data_dir
        )
        self._ws_manager = ws_manager

    def load_workspace_config(self, workspace_id: str) -> WriterProfilesConfig:
        """加载并合并引擎默认 + 工作区覆盖,返合并后的 ``WriterProfilesConfig``。

        :param workspace_id: 工作区 ID;不存在或无 ``project.yaml`` 时返引擎默认。
        :return: 已通过交叉校验的 ``WriterProfilesConfig``。
        :raises FileNotFoundError: 引擎默认 ``config.yaml`` 不存在。
        :raises pydantic.ValidationError: 合并后违反 ``WriterProfilesConfig``
            交叉校验(如 ``routing.default`` 引用未定义 profile)。
        :raises ValueError: ``project.yaml`` 顶层非 dict / 关键段类型错。
        :raises yaml.YAMLError: ``project.yaml`` 语法错。
        """
        engine_config = load_writer_profiles_config(self._engine_config_path)

        paths = WorkspacePaths(
            workspace_id, workspaces_root=self._workspaces_root
        )
        if not paths.project_yaml.is_file():
            return engine_config

        with paths.project_yaml.open("r", encoding="utf-8") as fp:
            override_raw: Any = yaml.safe_load(fp)

        # 空 yaml 文件 / 只有注释 → 等价于无覆盖。
        if override_raw is None:
            return engine_config

        if not isinstance(override_raw, dict):
            raise ValueError(
                f"project.yaml 顶层应为映射(dict),收到 "
                f"{type(override_raw).__name__}。文件: {paths.project_yaml}"
            )

        return _merge_overrides(engine_config, override_raw)

    def save_workspace_config(
        self,
        workspace_id: str,
        overrides: dict[str, Any],
    ) -> WriterProfilesConfig:
        """落工作区 ``project.yaml`` + 重新合并并校验(P6-B-12 引入)。

        :param workspace_id: 工作区 ID;目录必须已存在(``WorkspaceService.create``
            落地后)。
        :param overrides: 工作区覆盖层 yaml dict 内容,字面就是 ``project.yaml``
            将要写入的内容(支持空 dict / 部分段);函数内会以 ``yaml.safe_dump``
            保序写入。
        :return: 合并后的最新 ``WriterProfilesConfig``(立即可用,等价于写完后
            再调一次 ``load_workspace_config``)。
        :raises FileNotFoundError: 工作区目录不存在(由路由层翻 ``WorkspaceNotFound``
            404,对齐既有 P1 模式)。
        :raises ValueError: ``overrides`` 与引擎默认合并后违反字段类型 / 交叉校验
            (由路由层翻 ``ConfigYamlInvalid`` 422,既有 GET 端点同模式)。
        :raises pydantic.ValidationError: 同上,Pydantic 字段校验失败。

        实施纪律(对齐模块 docstring §P6-B-12 写入路径):
        - 先在内存里跑一次 ``_merge_overrides`` 校验失败 → 不落盘 + 抛错。
        - 校验通过 → 备份原 ``project.yaml`` 到 ``project.yaml.bak``(IO 失败
          回滚锚点)→ 写新 yaml。
        - 写完读一次 ``load_workspace_config`` 二次确认(防 yaml dump round-trip
          后字段语义偏移,极端 case 仍能 fail-loud)。
        - 成功后 publish WS ``config_updated``(publish 失败 swallow + structlog)。
        """
        paths = WorkspacePaths(
            workspace_id, workspaces_root=self._workspaces_root
        )
        if not paths.root.is_dir():
            raise FileNotFoundError(
                f"工作区目录不存在: {workspace_id}(根目录: {paths.root})"
            )

        # 步骤 1:内存校验。失败抛错不落盘。
        engine_config = load_writer_profiles_config(self._engine_config_path)
        _merge_overrides(engine_config, overrides)  # 抛 ValueError / ValidationError

        # 步骤 1.1(P6-B-13):reviewer 段单独校验(若 overrides 含)。
        if "reviewer" in overrides:
            reviewer_override = overrides.get("reviewer")
            if reviewer_override is not None:
                if not isinstance(reviewer_override, dict):
                    raise ValueError(
                        f"reviewer 覆盖段应为 dict,收到 "
                        f"{type(reviewer_override).__name__}"
                    )
                engine_reviewer = _load_reviewer_section(
                    self._engine_config_path
                )
                _merge_reviewer(engine_reviewer, reviewer_override)

        # 步骤 1.2(P6-B-14):retrieval 段单独校验(若 overrides 含)。
        if "retrieval" in overrides:
            retrieval_override = overrides.get("retrieval")
            if retrieval_override is not None:
                if not isinstance(retrieval_override, dict):
                    raise ValueError(
                        f"retrieval 覆盖段应为 dict,收到 "
                        f"{type(retrieval_override).__name__}"
                    )
                engine_retrieval = _load_retrieval_section(
                    self._engine_config_path
                )
                _merge_retrieval(engine_retrieval, retrieval_override)

        # 步骤 1.3(P6-B-15):cost_guard 段单独校验(若 overrides 含)。
        if "cost_guard" in overrides:
            cost_guard_override = overrides.get("cost_guard")
            if cost_guard_override is not None:
                if not isinstance(cost_guard_override, dict):
                    raise ValueError(
                        f"cost_guard 覆盖段应为 dict,收到 "
                        f"{type(cost_guard_override).__name__}"
                    )
                engine_cost_guard = _load_cost_guard_section(
                    self._engine_config_path
                )
                _merge_cost_guard(engine_cost_guard, cost_guard_override)

        # 步骤 1.4(v1.1 L1):generation 段单独校验(若 overrides 含)。
        if "generation" in overrides:
            generation_override = overrides.get("generation")
            if generation_override is not None:
                if not isinstance(generation_override, dict):
                    raise ValueError(
                        f"generation 覆盖段应为 dict,收到 "
                        f"{type(generation_override).__name__}"
                    )
                engine_generation = _load_generation_section(
                    self._engine_config_path
                )
                _merge_generation(engine_generation, generation_override)

        # 步骤 2:写前备份 .bak(若原文件存在)。
        backup_path = paths.project_yaml.with_suffix(".yaml.bak")
        had_original = paths.project_yaml.exists()
        if had_original:
            shutil.copy2(paths.project_yaml, backup_path)

        # 步骤 3:写 yaml。IO 失败 → 还原备份。
        try:
            with paths.project_yaml.open("w", encoding="utf-8") as fp:
                yaml.safe_dump(
                    overrides,
                    fp,
                    allow_unicode=True,
                    sort_keys=False,
                    default_flow_style=False,
                )
        except OSError as exc:
            if had_original and backup_path.exists():
                shutil.copy2(backup_path, paths.project_yaml)
            _log.error(
                "config_yaml_write_failed",
                workspace_id=workspace_id,
                error_type=type(exc).__name__,
                error_message=str(exc),
            )
            raise

        # 步骤 4:re-roundtrip 验证落盘内容仍合法。
        merged = self.load_workspace_config(workspace_id)

        # 步骤 5:WS publish(失败 swallow)。
        self._publish_workspace_event(
            workspace_id=workspace_id,
            event="config_updated",
            data={
                "workspace_id": workspace_id,
                "default_profile": merged.routing.default,
                "profiles_count": len(merged.profiles),
            },
        )

        return merged

    def _read_overlay_raw(self, paths: WorkspacePaths) -> dict[str, Any]:
        """读现有 ``project.yaml`` 覆盖层原始 dict(无文件 / 空 → ``{}``)。

        与 ``load_workspace_config`` 区别:本方法**不与引擎默认合并、不校验**,
        只把磁盘上的覆盖层原样读回(给"读-改-存"路径保留未触及的段用)。

        :raises ValueError: ``project.yaml`` 顶层非 dict(与既有读路径同型 fail-loud)。
        :raises yaml.YAMLError: yaml 语法错。
        """
        if not paths.project_yaml.is_file():
            return {}
        with paths.project_yaml.open("r", encoding="utf-8") as fp:
            existing: Any = yaml.safe_load(fp)
        if existing is None:
            return {}
        if not isinstance(existing, dict):
            raise ValueError(
                f"project.yaml 顶层应为映射(dict),收到 "
                f"{type(existing).__name__}。文件: {paths.project_yaml}"
            )
        return existing

    def save_workspace_config_overlay(
        self,
        workspace_id: str,
        overrides: dict[str, Any],
        *,
        explicit_sections: set[str],
    ) -> WriterProfilesConfig:
        """统一 PUT ``/workspaces/{id}/config`` 写入口:写覆盖层但保留有专属端点的段。

        v1.1 w3-s8(ConfigPage round-trip 修复)。``save_workspace_config`` 是
        **整体替换**语义(body 即落盘内容,未在 body 的段被冲掉)。但 ``generation``
        段有专属端点 ``save_generation_config`` 做读-改-存管理(L1 侧边浮层),统一
        PUT **不是其权威**。ConfigPage 只发 ``writer_profiles`` + ``writer_routing``
        两段 → 旧路径会把 L1 设的 ``generation`` 段静默冲掉(数据丢失)。

        本方法在调 ``save_workspace_config`` 前,若 body **未显式提及** ``generation``
        段(``explicit_sections`` 不含 + ``overrides`` 不含),从现有 ``project.yaml``
        把 ``generation`` 段保留进 ``overrides``,避免误冲。语义边界:

        - body 给出 ``generation`` 值 → 用 body 的(``overrides`` 已含,不保留)。
        - body 显式 ``generation: null`` → 用户意图清空该段(``explicit_sections`` 含
          ``generation`` 但 ``overrides`` 不含)→ 不保留,清空。
        - body 完全不提 ``generation`` → 保留现有(ConfigPage 主场景)。

        其余 5 段(``writer_profiles`` / ``writer_routing`` / ``reviewer`` /
        ``retrieval`` / ``cost_guard``)无专属端点,统一 PUT 是其唯一权威,沿用
        整体替换语义(省略 = 清空),不在此保留范围。

        :param explicit_sections: body 显式提及的段名集合(典型 ``model_fields_set``);
            用于区分"显式 null 清空"与"省略保留"。
        """
        if (
            "generation" not in explicit_sections
            and "generation" not in overrides
        ):
            paths = WorkspacePaths(
                workspace_id, workspaces_root=self._workspaces_root
            )
            existing = self._read_overlay_raw(paths)
            existing_generation = existing.get("generation")
            if isinstance(existing_generation, dict) and existing_generation:
                overrides = {**overrides, "generation": existing_generation}
        return self.save_workspace_config(workspace_id, overrides)

    def save_engine_agent_routing(
        self, agent_routing: dict[str, str]
    ) -> WriterProfilesConfig:
        """写引擎默认 ``config.yaml`` 的 ``writer_routing.agent_routing`` 段(v1.1 w3-s2 H5)。

        全局层 agent → profile 绑定。与工作区覆盖(``save_workspace_config``)区分:
        本方法改的是**所有工作区共用的引擎默认**,影响面更大。

        语义:**整段替换** ``agent_routing`` 映射 —— 传入 dict 即新的完整 agent_routing;
        传入的 agent 用对应 profile,**未传入的 agent 从 config.yaml 中删除其覆盖行**
        (回退到 ``default`` 兜底);空 dict → 清空所有 agent 覆盖。

        注释保留:用 ``ruamel.yaml`` round-trip 读写,保留 18KB 手写注释(Q17 拍板)。
        **就地更新** ``agent_routing`` 子 key(改值保留行尾注释 / 新增 key 无注释 /
        删除 key 连带其行注释一起删),不整段替换映射对象(否则丢全部行尾注释)。

        校验门(失败不落盘,镜像 ``ModelRouter`` 运行时护栏):
        1. 引用的所有 ``profile_id`` 必须在 ``config.yaml`` ``writer_profiles`` 里声明
           (``WriterProfilesConfig`` 交叉校验)。
        2. LLM-as-judge agent(``LLM_AS_JUDGE_AGENTS``)绑定的 profile 必须 thinking-on
           (wave 1 G3/G20 治本 hard guard;切到 thinking-off 直接 422 拒绝,
           而非运行时才报错,体验更好)。

        热加载:写完 config.yaml 后下次任何 LLM 调用经 ``load_writer_profiles_config``
        重读引擎默认即生效,无需独立 reload 端点(对齐 ``save_workspace_config`` 语义)。

        :param agent_routing: 新的完整 agent → profile_id 映射。
        :return: 写盘后重读的最新引擎默认 ``WriterProfilesConfig``。
        :raises FileNotFoundError: ``config.yaml`` 不存在(由路由层翻 422)。
        :raises ValueError: 交叉校验 / hard guard 失败(由路由层翻 422)。
        """
        # 步骤 1:内存校验(失败抛 ValueError 不落盘)。
        engine_config = load_writer_profiles_config(self._engine_config_path)
        prospective = WriterProfilesConfig(
            profiles=engine_config.profiles,
            routing=RoutingConfig(
                default=engine_config.routing.default,
                rules=engine_config.routing.rules,
                agent_routing=agent_routing,
            ),
        )  # model_validator 跑交叉引用校验,引用未声明 profile → ValueError

        # 步骤 1.1:LLM-as-judge thinking hard guard(镜像 ModelRouter._enforce）。
        for agent, profile_id in agent_routing.items():
            if agent not in LLM_AS_JUDGE_AGENTS:
                continue
            profile = prospective.profiles[profile_id]  # 已交叉校验存在
            if not is_thinking_on_profile(profile):
                raise ValueError(
                    f"LLM-as-judge agent {agent!r} 绑定到 profile "
                    f"{profile_id!r} (provider={profile.provider}) thinking off。"
                    f"按 wave 1 G3/G20 治本要求,LLM-as-judge agents 必须用 "
                    f"thinking-on profile(extra_request_kwargs.think=True / "
                    f"enable_thinking=True);见 "
                    f"docs/decisions/v1_1_wave1_governance_invariants.md §2.4。"
                )

        # 步骤 2:ruamel round-trip 读 config.yaml(保注释)。
        yaml_rt = YAML()
        yaml_rt.preserve_quotes = True
        with self._engine_config_path.open("r", encoding="utf-8") as fp:
            doc = yaml_rt.load(fp)

        routing_block = doc.get("writer_routing")
        if routing_block is None:
            raise ValueError(
                f"config.yaml 缺 writer_routing 段,无法写 agent_routing。"
                f"文件: {self._engine_config_path}"
            )

        # 步骤 2.1:就地更新 agent_routing 子 key(保留未改 agent 的行尾注释）。
        existing = routing_block.get("agent_routing")
        if existing is None:
            routing_block["agent_routing"] = dict(agent_routing)
        else:
            for key in list(existing.keys()):
                if key not in agent_routing:
                    del existing[key]
            for key, value in agent_routing.items():
                existing[key] = value

        # 步骤 3:写前备份 .bak(IO 失败回滚锚点)。
        backup_path = self._engine_config_path.with_suffix(".yaml.bak")
        shutil.copy2(self._engine_config_path, backup_path)

        # 步骤 4:写 yaml。IO 失败 → 还原备份。
        try:
            with self._engine_config_path.open("w", encoding="utf-8") as fp:
                yaml_rt.dump(doc, fp)
        except OSError as exc:
            shutil.copy2(backup_path, self._engine_config_path)
            _log.error(
                "engine_config_yaml_write_failed",
                error_type=type(exc).__name__,
                error_message=str(exc),
            )
            raise

        # 步骤 5:re-roundtrip 二次确认落盘内容仍合法(fail-loud)。
        return load_writer_profiles_config(self._engine_config_path)

    def load_reviewer_config(self, workspace_id: str) -> ReviewerConfig:
        """加载工作区级 reviewer 阈值配置(P6-B-13)。

        合并语义:引擎默认 ``config.yaml`` 的 ``reviewer:`` 段 ⊕ 工作区
        ``project.yaml`` 的 ``reviewer:`` 段(字段级合并,工作区填的字段覆盖,
        未填字段保留引擎默认)。整段省略 → 走引擎默认;字段全省略 →
        ``ReviewerConfig`` 字段默认值(对齐 ChapterCard 单章字段 +
        ``reviewer.py`` ESCALATION_*_BOUND 模块级常量)。

        :param workspace_id: 工作区 ID;不存在或无 ``project.yaml`` /
            无 reviewer 段 → 返引擎默认。
        :return: 已通过交叉校验(``revise < pass`` /
            ``escalation_lower < escalation_upper``)的 ``ReviewerConfig``。
        :raises ValueError: ``project.yaml`` 顶层 / ``reviewer`` 段非 dict。
        :raises pydantic.ValidationError: 合并后违反 ``ReviewerConfig`` 字段
            范围或交叉校验。
        :raises yaml.YAMLError: ``project.yaml`` 语法错。

        **本任务范围**(启动指令字面接力 marker):本方法只暴露给 P6-F-08+
        前端 / 后续接力任务消费,**reviewer agent runtime 暂不接入**
        (``src/core/agents/reviewer.py`` 沿用 ChapterCard 单章字段)。
        """
        engine_reviewer = _load_reviewer_section(self._engine_config_path)

        paths = WorkspacePaths(
            workspace_id, workspaces_root=self._workspaces_root
        )
        if not paths.project_yaml.is_file():
            return engine_reviewer

        with paths.project_yaml.open("r", encoding="utf-8") as fp:
            override_raw: Any = yaml.safe_load(fp)

        if override_raw is None:
            return engine_reviewer

        if not isinstance(override_raw, dict):
            raise ValueError(
                f"project.yaml 顶层应为映射(dict),收到 "
                f"{type(override_raw).__name__}。文件: {paths.project_yaml}"
            )

        reviewer_override = override_raw.get("reviewer")
        if reviewer_override is None:
            return engine_reviewer

        if not isinstance(reviewer_override, dict):
            raise ValueError(
                f"project.yaml reviewer 段应为 dict,收到 "
                f"{type(reviewer_override).__name__}"
            )

        return _merge_reviewer(engine_reviewer, reviewer_override)

    def load_reviewer_config_explicit(
        self, workspace_id: str
    ) -> ReviewerConfig | None:
        """加载工作区级 reviewer 阈值配置(v1.0.x Commit D 引入)。

        与 ``load_reviewer_config`` 的区别:**显式区分"工作区是否设过 reviewer 段"**。
        工作区无 ``project.yaml`` 或 ``project.yaml`` 没有 ``reviewer`` 段时,
        返回 ``None``(而不是引擎默认 ReviewerConfig)。caller 可据此实现"工作区
        显式 → chapter_card 字段 → 引擎默认"三级 fallback。

        本 API 仅供 ``reviewer.py:_resolve_reviewer_thresholds`` 消费,既有
        caller(P6-F-08+ 前端配置页面 + load_workspace_config)仍走
        ``load_reviewer_config``(合并语义)。

        :param workspace_id: 工作区 ID
        :return: ``ReviewerConfig`` 表示工作区显式有 reviewer 段;``None`` 表示
            工作区没显式设(caller 应回退到 chapter_card 字段或引擎默认)。
        """
        paths = WorkspacePaths(
            workspace_id, workspaces_root=self._workspaces_root
        )
        if not paths.project_yaml.is_file():
            return None

        with paths.project_yaml.open("r", encoding="utf-8") as fp:
            override_raw: Any = yaml.safe_load(fp)

        if override_raw is None:
            return None

        if not isinstance(override_raw, dict):
            raise ValueError(
                f"project.yaml 顶层应为映射(dict),收到 "
                f"{type(override_raw).__name__}。文件: {paths.project_yaml}"
            )

        reviewer_override = override_raw.get("reviewer")
        if reviewer_override is None:
            return None

        if not isinstance(reviewer_override, dict):
            raise ValueError(
                f"project.yaml reviewer 段应为 dict,收到 "
                f"{type(reviewer_override).__name__}"
            )

        # 字段级合并:工作区填的字段覆盖,未填字段保留引擎默认(对齐
        # load_reviewer_config 语义,但只在工作区显式有 reviewer 段时触发)。
        engine_reviewer = _load_reviewer_section(self._engine_config_path)
        return _merge_reviewer(engine_reviewer, reviewer_override)

    def load_retrieval_config(self, workspace_id: str) -> RetrievalConfig:
        """加载工作区级 retrieval 检索配置(P6-B-14)。

        合并语义:引擎默认 ``config.yaml`` 的 ``retrieval:`` 段 ⊕ 工作区
        ``project.yaml`` 的 ``retrieval:`` 段(字段级合并,工作区填的字段覆盖,
        未填字段保留引擎默认)。整段省略 → 走引擎默认;字段全省略 →
        ``RetrievalConfig`` 字段默认值(``top_k=20`` / ``min_similarity=0.0``)。

        :param workspace_id: 工作区 ID;不存在或无 ``project.yaml`` /
            无 retrieval 段 → 返引擎默认。
        :return: 已通过字段范围校验的 ``RetrievalConfig``。
        :raises ValueError: ``project.yaml`` 顶层 / ``retrieval`` 段非 dict。
        :raises pydantic.ValidationError: 合并后违反 ``RetrievalConfig`` 字段
            范围(``top_k >= 1`` / ``0.0 <= min_similarity <= 1.0``)。
        :raises yaml.YAMLError: ``project.yaml`` 语法错。

        **本任务范围**(启动指令字面接力 marker):本方法只暴露给 P6-F-08+
        前端 / 后续接力任务消费,**retriever / writer caller 链暂不接入**
        (``src/core/memory/retriever.py`` 默认 ``DEFAULT_TOP_K=20`` 与
        ``src/core/agents/writer.py`` ``retriever_top_k=10`` 显式传参保持不变)。
        """
        engine_retrieval = _load_retrieval_section(self._engine_config_path)

        paths = WorkspacePaths(
            workspace_id, workspaces_root=self._workspaces_root
        )
        if not paths.project_yaml.is_file():
            return engine_retrieval

        with paths.project_yaml.open("r", encoding="utf-8") as fp:
            override_raw: Any = yaml.safe_load(fp)

        if override_raw is None:
            return engine_retrieval

        if not isinstance(override_raw, dict):
            raise ValueError(
                f"project.yaml 顶层应为映射(dict),收到 "
                f"{type(override_raw).__name__}。文件: {paths.project_yaml}"
            )

        retrieval_override = override_raw.get("retrieval")
        if retrieval_override is None:
            return engine_retrieval

        if not isinstance(retrieval_override, dict):
            raise ValueError(
                f"project.yaml retrieval 段应为 dict,收到 "
                f"{type(retrieval_override).__name__}"
            )

        return _merge_retrieval(engine_retrieval, retrieval_override)

    def load_generation_config(
        self,
        workspace_id: str,
        *,
        engine_base_override: GenerationConfig | None = None,
    ) -> GenerationConfig:
        """加载工作区级 generation 生成参数配置(v1.1 L1 Q16)。

        合并语义:引擎默认 ``config.yaml`` 的 ``generation:`` 段 ⊕ 工作区
        ``project.yaml`` 的 ``generation:`` 段(字段级合并,工作区填的字段覆盖,
        未填字段保留引擎默认)。整段省略 → 走引擎默认;字段全省略 →
        ``GenerationConfig`` 字段默认值(``word_count_min=2500`` /
        ``word_count_max=4000`` / ``max_rewrites=3``)。

        :param workspace_id: 工作区 ID;不存在或无 ``project.yaml`` /
            无 generation 段 → 返引擎默认(或 ``engine_base_override``)。
        :param engine_base_override: v1.1 wave 4 session 3 / I2.1 加。给定时
            **替代** ``config.yaml`` generation 段作合并基线——风格模板
            (``STYLE_GENERATION_DEFAULTS``)把所选模板的生成默认作为工作区级
            引擎基线注入,project.yaml L1 用户覆盖仍字段级优先(经 ``_merge_generation``)。
            ``None``(L1 既有路径 / 未选模板)时沿用 ``config.yaml`` 引擎默认,零回归。
        :return: 已通过交叉校验(``word_count_min <= word_count_max``)的
            ``GenerationConfig``。
        :raises ValueError: ``project.yaml`` 顶层 / ``generation`` 段非 dict。
        :raises pydantic.ValidationError: 合并后违反 ``GenerationConfig`` 字段
            范围或交叉校验。
        :raises yaml.YAMLError: ``project.yaml`` 语法错。

        **runtime 接入**(与 reviewer/retrieval 接力 marker 不同):本配置由
        ``ChapterCardService.derive_context`` 在新卡生成时读取作 word_count /
        max_rewrites 兜底(L1 本任务即接入);已生成卡走 G6.2 backfill 端点。
        """
        engine_generation = (
            engine_base_override
            if engine_base_override is not None
            else _load_generation_section(self._engine_config_path)
        )

        paths = WorkspacePaths(
            workspace_id, workspaces_root=self._workspaces_root
        )
        if not paths.project_yaml.is_file():
            return engine_generation

        with paths.project_yaml.open("r", encoding="utf-8") as fp:
            override_raw: Any = yaml.safe_load(fp)

        if override_raw is None:
            return engine_generation

        if not isinstance(override_raw, dict):
            raise ValueError(
                f"project.yaml 顶层应为映射(dict),收到 "
                f"{type(override_raw).__name__}。文件: {paths.project_yaml}"
            )

        generation_override = override_raw.get("generation")
        if generation_override is None:
            return engine_generation

        if not isinstance(generation_override, dict):
            raise ValueError(
                f"project.yaml generation 段应为 dict,收到 "
                f"{type(generation_override).__name__}"
            )

        return _merge_generation(engine_generation, generation_override)

    def load_generation_override(self, workspace_id: str) -> dict[str, Any]:
        """读工作区 ``project.yaml`` 的 ``generation:`` 段**原始 override** dict(不合并引擎默认)。

        W3.5 校准联动用:trim 三态解析(``ChapterService._resolve_anti_tic_trim`` /
        ``_resolve_word_count_trim``)需区分"用户**显式**设了 ``True``/``False``" vs
        "**未设**(``None``,跟随当前 writer 模型校准)"。``load_generation_config``
        返回的是合并后的 ``GenerationConfig``(字段已塌成具体 ``bool``,三态信息丢失);
        本方法返回 project.yaml 原始 override 段,消费方 ``.get(field)`` 拿到 ``None``
        (未设)/ ``True`` / ``False`` 三态。

        无 project.yaml / 顶层非 dict / 无 generation 段 / generation 段非 dict / 读
        失败 → **空 dict**(优雅退化,绝不抛 —— 三态解析读失败应退引擎默认关、不阻断
        生成;与 ``load_generation_config`` 配置展示/写盘路径的 fail-fast 语义有意不同)。

        :param workspace_id: 工作区 ID。
        :return: ``generation`` 段原始 dict(``{}`` 表示无覆盖 / 读失败)。
        """
        paths = WorkspacePaths(
            workspace_id, workspaces_root=self._workspaces_root
        )
        if not paths.project_yaml.is_file():
            return {}
        try:
            with paths.project_yaml.open("r", encoding="utf-8") as fp:
                override_raw: Any = yaml.safe_load(fp)
        except (yaml.YAMLError, OSError):  # noqa: BLE001 — 生成路径读失败退空(关),不阻断
            return {}
        if not isinstance(override_raw, dict):
            return {}
        generation_override = override_raw.get("generation")
        if not isinstance(generation_override, dict):
            return {}
        return generation_override

    def _resolve_trim_calibration_defaults(self, workspace_id: str) -> dict[str, bool]:
        """当前 workspace writer 模型校准:两个 trim 各是否**默认开**(三态 None 态取值)。

        keyed on **writer 模型**(对齐 ``OutlineService._resolve_length_calibration`` /
        ``ChapterService._calibration_default_trim``):``load_workspace_config`` →
        中央 ``build_model_router(...).select_profile("writer").model`` →
        ``load_model_calibration`` → ``GlobalCalibration.wants_*_trim()``(映射单一
        真相源)。活动 manifest 存在时不得在这个只读 UI 视图里退回 legacy route。

        供 GET /config/generation 三态视图显示「跟随模型(当前:开/关)」。无法解析(无
        routing / 模型无校准 / 任何异常)→ 全 ``False``(= 跟随时默认关,不阻断 GET)。
        """
        try:
            from src.core.routing.run_model_manifest_router import build_model_router

            config = self.load_workspace_config(workspace_id)
            writer_model = build_model_router(
                config,
                config_path=self._engine_config_path,
            ).select_profile("writer").model
            from src.core.config_loader import load_model_calibration

            # calibration 与 writer model **同源**(本 ConfigService 的 engine config):
            # 生产 = settings.config_yaml_path;测试用注入的 fixture engine config,writer
            # model 与 calibration 自洽 —— 不读全局真实 config.yaml(避免测试污染 + 第一性
            # 正确:同一份引擎配置既定义 routing 也定义 calibration)。
            cal = load_model_calibration(self._engine_config_path).get(writer_model)
        except Exception:  # noqa: BLE001 — 解析失败退全 False(跟随时默认关),不阻断 GET
            return {"word_count_trim": False, "anti_tic_trim": False}
        if cal is None:
            return {"word_count_trim": False, "anti_tic_trim": False}
        return {
            "word_count_trim": cal.global_.wants_word_count_trim(),
            "anti_tic_trim": cal.global_.wants_anti_tic_trim(),
        }

    def load_generation_config_view(self, workspace_id: str) -> dict[str, Any]:
        """GET/PUT /config/generation 的**三态视图** dict(``GenerationConfigOut`` 可直接 validate)。

        在 ``load_generation_config``(引擎默认 ⊕ 工作区覆盖)基础上,为两个 deepseek 专用
        trim 补 W3.5 校准联动三态字段:
        - ``*_override``:project.yaml 原始三态(``None``=跟随 / ``True`` / ``False``),
          来自 ``load_generation_override``;
        - ``*_calibration_default``:当前 writer 模型校准默认,来自
          ``_resolve_trim_calibration_defaults``;
        - ``*_enabled``:**生效值** = override 非 None 则取 override,否则取校准默认
          (覆盖 ``GenerationConfig`` 合并时塌成的 ``override??引擎默认 False`` —— 那个不含
          校准;本视图修正为含校准的真实生效值)。

        :raises: 同 ``load_generation_config``(project.yaml / generation 段非 dict →
            ``ValueError``;合并校验失败 → ``ValidationError``;yaml 语法错 → ``YAMLError``)。
            override / 校准解析均优雅退化(不抛)。
        :return: ``GenerationConfigOut.model_validate`` 可消费的 dict。
        """
        base: dict[str, Any] = self.load_generation_config(workspace_id).model_dump()
        override = self.load_generation_override(workspace_id)
        cal_defaults = self._resolve_trim_calibration_defaults(workspace_id)
        for trim in ("word_count_trim", "anti_tic_trim"):
            ov = override.get(f"{trim}_enabled")  # None / True / False(三态)
            cd = cal_defaults[trim]
            base[f"{trim}_enabled"] = ov if ov is not None else cd  # 生效值(三态修正)
            base[f"{trim}_override"] = ov
            base[f"{trim}_calibration_default"] = cd
        return base

    def save_generation_config(
        self,
        workspace_id: str,
        generation_override: dict[str, Any],
    ) -> GenerationConfig:
        """落工作区 ``generation:`` 段(只改该段,保留 project.yaml 其他覆盖)。

        v1.1 L1 专属端点用:侧边浮层只编辑 generation 段,服务端读现有
        ``project.yaml`` 覆盖层 → 替换 ``generation`` 键 → 整体存回
        (复用 ``save_workspace_config`` 的内存校验 + .bak 备份 + 二次确认 +
        WS publish + 缓存 invalidate),避免前端只发 generation 段冲掉
        reviewer / retrieval / cost_guard / routing 等其他覆盖。

        :param workspace_id: 工作区 ID;目录必须已存在。
        :param generation_override: ``generation`` 段 dict(字面就是要写入
            ``project.yaml`` 的 ``generation:`` 内容,函数内做字段范围 + 交叉校验)。
        :return: 合并引擎默认后的最新 ``GenerationConfig``。
        :raises FileNotFoundError: 工作区目录不存在(路由翻 404)。
        :raises ValueError / pydantic.ValidationError: 校验失败(路由翻 422,
            原 ``project.yaml`` 不动)。
        :raises yaml.YAMLError: 现有 ``project.yaml`` 语法错。
        """
        paths = WorkspacePaths(
            workspace_id, workspaces_root=self._workspaces_root
        )
        if not paths.root.is_dir():
            raise FileNotFoundError(
                f"工作区目录不存在: {workspace_id}(根目录: {paths.root})"
            )

        # 读现有 project.yaml 覆盖层(无则空 dict);只替换 generation 键。
        overlay = self._read_overlay_raw(paths)
        overlay["generation"] = generation_override
        self.save_workspace_config(workspace_id, overlay)
        return self.load_generation_config(workspace_id)

    def save_cost_guard_config(
        self,
        workspace_id: str,
        cost_guard_patch: dict[str, Any],
    ) -> CostGuardConfig:
        """局部更新工作区 ``cost_guard:`` 段并保留其他配置覆盖。

        ConfigPage 的成本上限表单只负责成本守卫，不能借统一配置 PUT 的
        整体替换语义清掉 writer / reviewer / retrieval / generation 等段。
        本入口读取现有覆盖层，在已有 ``cost_guard`` 子段上合并本次字段，
        再复用统一保存的校验、备份、事件与回读闭环。
        """

        paths = WorkspacePaths(
            workspace_id, workspaces_root=self._workspaces_root
        )
        if not paths.root.is_dir():
            raise FileNotFoundError(
                f"工作区目录不存在: {workspace_id}(根目录: {paths.root})"
            )

        overlay = self._read_overlay_raw(paths)
        existing = overlay.get("cost_guard")
        if existing is None:
            existing = {}
        if not isinstance(existing, dict):
            raise ValueError(
                f"project.yaml cost_guard 段应为 dict,收到 "
                f"{type(existing).__name__}"
            )
        overlay["cost_guard"] = {**existing, **cost_guard_patch}
        self.save_workspace_config(workspace_id, overlay)
        return self.load_cost_guard_config_for_workspace(workspace_id)

    def load_cost_guard_config_for_workspace(
        self, workspace_id: str
    ) -> CostGuardConfig:
        """加载工作区级 cost_guard 成本守卫配置(P6-B-15)。

        合并语义:引擎默认 ``config.yaml`` 的 ``cost_guard:`` 段 ⊕ 工作区
        ``project.yaml`` 的 ``cost_guard:`` 段(字段级合并,工作区填的字段
        覆盖,未填字段保留引擎默认)。整段省略 → 走引擎默认;字段全省略 →
        ``CostGuardConfig`` 字段默认值(对齐 OBSERVABILITY §3.4 字面默认值)。

        :param workspace_id: 工作区 ID;不存在或无 ``project.yaml`` /
            无 cost_guard 段 → 返引擎默认。
        :return: 已通过字段范围 + Literal 校验的 ``CostGuardConfig``。
        :raises ValueError: ``project.yaml`` 顶层 / ``cost_guard`` 段非 dict。
        :raises pydantic.ValidationError: 合并后违反 ``CostGuardConfig`` 字段
            范围 / Literal 校验(各 ``*_usd_limit >= 0.0`` /
            ``action_on_exceed`` 三档 / ``notify_via`` 两档)。
        :raises yaml.YAMLError: ``project.yaml`` 语法错。

        **本任务范围**(启动指令字面接力 marker):本方法只暴露给 P6-F-08+
        前端 / 后续接力任务消费,``deps.py`` ``_COST_GUARD_CONFIG`` 模块级
        单例 caller 链**暂不接入**(接入涉及单例 → 每工作区 lazy 缓存的改造,
        超 60 分钟范围,详 impl §6 留观项)。方法命名带 ``_for_workspace``
        后缀避免与模块函数 ``src.services.cost_guard.load_cost_guard_config``
        重名(后者签名是 ``path``,本方法签名是 ``workspace_id``,语义不同)。
        """
        engine_cost_guard = _load_cost_guard_section(self._engine_config_path)

        paths = WorkspacePaths(
            workspace_id, workspaces_root=self._workspaces_root
        )
        if not paths.project_yaml.is_file():
            return engine_cost_guard

        with paths.project_yaml.open("r", encoding="utf-8") as fp:
            override_raw: Any = yaml.safe_load(fp)

        if override_raw is None:
            return engine_cost_guard

        if not isinstance(override_raw, dict):
            raise ValueError(
                f"project.yaml 顶层应为映射(dict),收到 "
                f"{type(override_raw).__name__}。文件: {paths.project_yaml}"
            )

        cost_guard_override = override_raw.get("cost_guard")
        if cost_guard_override is None:
            return engine_cost_guard

        if not isinstance(cost_guard_override, dict):
            raise ValueError(
                f"project.yaml cost_guard 段应为 dict,收到 "
                f"{type(cost_guard_override).__name__}"
            )

        return _merge_cost_guard(engine_cost_guard, cost_guard_override)

    def _publish_workspace_event(
        self,
        *,
        workspace_id: str,
        event: str,
        data: dict[str, Any],
    ) -> None:
        """WS publish 到 ``workspace.{workspace_id}.events``;失败 swallow + log。

        对齐 ``BackupService._publish_workspace_event`` 同型(structlog ``event``
        首参冲突 → 业务 event 改名 ``ws_event``)。
        """
        if self._ws_manager is None:
            return
        try:
            self._ws_manager.publish(  # type: ignore[attr-defined]
                channel=workspace_events_channel(workspace_id),
                event=event,
                data=data,
            )
        except Exception as exc:  # noqa: BLE001
            _log.error(
                "config_event_notify_failed",
                workspace_id=workspace_id,
                ws_event=event,
                error_type=type(exc).__name__,
                error_message=str(exc),
            )


def load_workspace_config(workspace_id: str) -> WriterProfilesConfig:
    """模块级 thin-wrapper,等价于 ``ConfigService().load_workspace_config(workspace_id)``。

    P3-B-05 ``ModelRouter`` 与 P3-B-06 ``ChapterService`` 默认从此入口取配置;
    测试场景需注入 ``tmp_path`` 路径时改用 ``ConfigService(...)``。
    """
    return ConfigService().load_workspace_config(workspace_id)


def _merge_overrides(
    engine: WriterProfilesConfig, override_raw: dict[str, Any]
) -> WriterProfilesConfig:
    """合并引擎默认 + 工作区覆盖 dict,产出新 ``WriterProfilesConfig``。

    合并语义见模块 docstring §"合并语义"。本函数不直接落地校验,而是把合并
    后的字段交回 ``WriterProfilesConfig.model_validate`` 触发交叉校验。
    """
    profiles_override = override_raw.get("writer_profiles") or {}
    routing_override = override_raw.get("writer_routing") or {}

    if not isinstance(profiles_override, dict):
        raise ValueError(
            f"writer_profiles 覆盖段应为 dict,收到 "
            f"{type(profiles_override).__name__}"
        )
    if not isinstance(routing_override, dict):
        raise ValueError(
            f"writer_routing 覆盖段应为 dict,收到 "
            f"{type(routing_override).__name__}"
        )

    merged_profiles = _merge_profiles(engine.profiles, profiles_override)
    merged_routing = _merge_routing(engine.routing, routing_override)

    return WriterProfilesConfig(
        profiles=merged_profiles, routing=merged_routing
    )


def _merge_profiles(
    engine_profiles: dict[str, WriterProfile],
    override_payload: dict[str, Any],
) -> dict[str, WriterProfile]:
    """``writer_profiles`` dict-level merge by key。

    工作区 key 整体替换该 profile;未在工作区出现的 key 保留引擎默认。
    工作区单个 profile 必须包含 ``WriterProfile`` 全部必填字段(整体替换语义)。
    """
    merged: dict[str, WriterProfile] = dict(engine_profiles)

    for profile_id, payload in override_payload.items():
        if not isinstance(payload, dict):
            raise ValueError(
                f"writer_profiles[{profile_id!r}] 覆盖应为 dict,"
                f"收到 {type(payload).__name__}"
            )
        # yaml 字面允许省略 profile_id,用 dict key 兜底;若显式写则尊重
        # yaml 字面值,交叉校验在 ``WriterProfilesConfig`` 落地拒绝不一致。
        merged_payload = {"profile_id": profile_id, **payload}
        merged[profile_id] = WriterProfile.model_validate(merged_payload)

    return merged


def _load_reviewer_section(engine_config_path: Path) -> ReviewerConfig:
    """读引擎默认 ``config.yaml`` 的 ``reviewer:`` 段(P6-B-13)。

    yaml 不存在 / 无 reviewer 段 → ``ReviewerConfig()`` 字段默认(对齐
    ``CostGuardConfig.load_cost_guard_config`` 同型容忍模式)。reviewer 段
    类型错(非 dict)→ ``ValueError``,与 GET / PUT 路径错误码对齐。
    """
    if not engine_config_path.exists():
        return ReviewerConfig()
    with engine_config_path.open("r", encoding="utf-8") as fp:
        raw: Any = yaml.safe_load(fp)
    if not isinstance(raw, dict):
        return ReviewerConfig()
    section = raw.get("reviewer")
    if section is None:
        return ReviewerConfig()
    if not isinstance(section, dict):
        raise ValueError(
            f"config.yaml reviewer 段应为 dict,收到 {type(section).__name__}"
        )
    return ReviewerConfig.model_validate(section)


def _merge_reviewer(
    engine: ReviewerConfig, override_payload: dict[str, Any]
) -> ReviewerConfig:
    """``reviewer`` 字段级合并(P6-B-13)。

    工作区填的字段覆盖,未填字段保留引擎默认;``None`` 值视为"省略不覆盖"
    (对齐 ``RoutingConfigIn`` 字段级合并语义)。合并后调
    ``ReviewerConfig.model_validate`` 触发字段范围 + 交叉校验
    (``revise < pass`` / ``escalation_lower < escalation_upper``)。
    """
    base = engine.model_dump()
    for field in (
        "pass_threshold",
        "revise_threshold",
        "escalation_lower_bound",
        "escalation_upper_bound",
    ):
        if field in override_payload and override_payload[field] is not None:
            base[field] = override_payload[field]
    return ReviewerConfig.model_validate(base)


def _load_retrieval_section(engine_config_path: Path) -> RetrievalConfig:
    """读引擎默认 ``config.yaml`` 的 ``retrieval:`` 段(P6-B-14)。

    yaml 不存在 / 无 retrieval 段 → ``RetrievalConfig()`` 字段默认(对齐
    ``_load_reviewer_section`` 同型容忍模式)。retrieval 段类型错(非 dict)
    → ``ValueError``,与 GET / PUT 路径错误码对齐。
    """
    if not engine_config_path.exists():
        return RetrievalConfig()
    with engine_config_path.open("r", encoding="utf-8") as fp:
        raw: Any = yaml.safe_load(fp)
    if not isinstance(raw, dict):
        return RetrievalConfig()
    section = raw.get("retrieval")
    if section is None:
        return RetrievalConfig()
    if not isinstance(section, dict):
        raise ValueError(
            f"config.yaml retrieval 段应为 dict,收到 {type(section).__name__}"
        )
    return RetrievalConfig.model_validate(section)


def _merge_retrieval(
    engine: RetrievalConfig, override_payload: dict[str, Any]
) -> RetrievalConfig:
    """``retrieval`` 字段级合并(P6-B-14)。

    工作区填的字段覆盖,未填字段保留引擎默认;``None`` 值视为"省略不覆盖"
    (对齐 ``_merge_reviewer`` 同型字段级合并语义)。合并后调
    ``RetrievalConfig.model_validate`` 触发字段范围校验
    (``top_k >= 1`` / ``0.0 <= min_similarity <= 1.0``)。
    """
    base = engine.model_dump()
    for field in ("top_k", "min_similarity"):
        if field in override_payload and override_payload[field] is not None:
            base[field] = override_payload[field]
    return RetrievalConfig.model_validate(base)


def _load_generation_section(engine_config_path: Path) -> GenerationConfig:
    """读引擎默认 ``config.yaml`` 的 ``generation:`` 段(v1.1 L1)。

    yaml 不存在 / 无 generation 段 → ``GenerationConfig()`` 字段默认(对齐
    ``_load_retrieval_section`` 同型容忍模式)。generation 段类型错(非 dict)
    → ``ValueError``,与 GET / PUT 路径错误码对齐。
    """
    if not engine_config_path.exists():
        return GenerationConfig()
    with engine_config_path.open("r", encoding="utf-8") as fp:
        raw: Any = yaml.safe_load(fp)
    if not isinstance(raw, dict):
        return GenerationConfig()
    section = raw.get("generation")
    if section is None:
        return GenerationConfig()
    if not isinstance(section, dict):
        raise ValueError(
            f"config.yaml generation 段应为 dict,收到 {type(section).__name__}"
        )
    return GenerationConfig.model_validate(section)


def _merge_generation(
    engine: GenerationConfig, override_payload: dict[str, Any]
) -> GenerationConfig:
    """``generation`` 字段级合并(v1.1 L1)。

    工作区填的字段覆盖,未填字段保留引擎默认;``None`` 值视为"省略不覆盖"
    (对齐 ``_merge_reviewer`` / ``_merge_retrieval`` 同型字段级合并语义)。
    合并后调 ``GenerationConfig.model_validate`` 触发字段范围 + 交叉校验
    (``word_count_min <= word_count_max``)。
    """
    base = engine.model_dump()
    if "batch_granularity" in override_payload:
        _log.warning(
            "generation_batch_granularity_retired_ignored",
            requested_value=override_payload.get("batch_granularity"),
            reason="chapter cards are generated one at a time after rich memory succeeds",
        )
    # 白名单需随 GenerationConfig 字段演进同步登记(v2 w2-s2 修):w5-s10 的
    # progression_consistency / breakthrough_payoff 曾漏登 → project.yaml /
    # L1 UI 写入这两字段后读回被本函数静默丢弃(UI 开关无效,恒走引擎默认
    # auto)— 既有缺陷,w2-s2 加 style_anchor_source 时一并补登 + 防回退测。
    for field in (
        "word_count_min",
        "word_count_max",
        "word_count_policy_version",
        "max_rewrites",
        "plot_node_budget",
        "core_state_hard_enabled",
        "progression_consistency",
        "breakthrough_payoff",
        "style_anchor_source",
        "anti_tic_trim_enabled",  # v2 W3.5 wave-3:漏登则 UI 开关静默失效(同 w5-s10 旧坑)
        "word_count_trim_enabled",  # v2 W3.5-s3rev:漏登则 UI 开关静默失效(同上)
        "outline_autofix_enabled",  # v2 W3.5-s4-step1:漏登则工作区级 kill-switch 失效(同上)
        "character_backfill_enabled",  # v2 W3.5-s4-step1 机制1:漏登则工作区级开关失效(同上)
    ):
        if field in override_payload and override_payload[field] is not None:
            base[field] = override_payload[field]
    if (engine.word_count_policy_version == 2
            and override_payload.get("word_count_policy_version") is None
            and any(override_payload.get(key) is not None for key in ("word_count_min", "word_count_max"))):
        base["word_count_policy_version"] = 1
        legacy = GenerationConfig()
        for key in ("word_count_min", "word_count_max"):
            if override_payload.get(key) is None:
                base[key] = getattr(legacy, key)
    return GenerationConfig.model_validate(base)


def _load_cost_guard_section(engine_config_path: Path) -> CostGuardConfig:
    """读引擎默认 ``config.yaml`` 的 ``cost_guard:`` 段(P6-B-15)。

    yaml 不存在 / 无 cost_guard 段 → ``CostGuardConfig()`` 字段默认(对齐
    ``_load_retrieval_section`` 同型容忍模式 + ``cost_guard.load_cost_guard_config``
    既有 fallback 语义)。cost_guard 段类型错(非 dict)→ ``ValueError``,与
    GET / PUT 路径错误码对齐。
    """
    if not engine_config_path.exists():
        return CostGuardConfig()
    with engine_config_path.open("r", encoding="utf-8") as fp:
        raw: Any = yaml.safe_load(fp)
    if not isinstance(raw, dict):
        return CostGuardConfig()
    section = raw.get("cost_guard")
    if section is None:
        return CostGuardConfig()
    if not isinstance(section, dict):
        raise ValueError(
            f"config.yaml cost_guard 段应为 dict,收到 {type(section).__name__}"
        )
    return CostGuardConfig.model_validate(section)


def _merge_cost_guard(
    engine: CostGuardConfig, override_payload: dict[str, Any]
) -> CostGuardConfig:
    """``cost_guard`` 字段级合并(P6-B-15)。

    工作区填的字段覆盖,未填字段保留引擎默认;``None`` 值视为"省略不覆盖"
    (对齐 ``_merge_reviewer`` / ``_merge_retrieval`` 同型字段级合并语义)。
    ``notify_via`` list 字段也走字段级 — 工作区给即整体替换(空列表 → 显式
    禁用所有通知),省略 / None 则保留引擎默认。合并后调
    ``CostGuardConfig.model_validate`` 触发字段范围 + Literal 校验。
    """
    base = engine.model_dump()
    for field in (
        "cost_currency",
        "daily_usd_limit",
        "monthly_usd_limit",
        "per_workspace_daily_limit",
        "action_on_exceed",
        "notify_via",
    ):
        if field in override_payload and override_payload[field] is not None:
            base[field] = override_payload[field]
    return CostGuardConfig.model_validate(base)


def _merge_routing(
    engine_routing: RoutingConfig,
    override_payload: dict[str, Any],
) -> RoutingConfig:
    """``writer_routing`` 字段级合并。

    - ``default``:单字段替换(工作区填即覆盖)。
    - ``rules``:整段替换(列表顺序敏感)。
    - ``agent_routing``:dict-level merge by key(工作区可单独覆盖某 agent)。
    """
    base = engine_routing.model_dump()

    if "default" in override_payload:
        base["default"] = override_payload["default"]

    if "rules" in override_payload:
        rules_override = override_payload["rules"]
        if rules_override is not None and not isinstance(rules_override, list):
            raise ValueError(
                f"writer_routing.rules 覆盖应为 list,收到 "
                f"{type(rules_override).__name__}"
            )
        base["rules"] = rules_override or []

    if "agent_routing" in override_payload:
        ar_override = override_payload["agent_routing"] or {}
        if not isinstance(ar_override, dict):
            raise ValueError(
                f"writer_routing.agent_routing 覆盖应为 dict,收到 "
                f"{type(ar_override).__name__}"
            )
        merged_agent_routing = dict(base.get("agent_routing") or {})
        merged_agent_routing.update(ar_override)
        base["agent_routing"] = merged_agent_routing

    return RoutingConfig.model_validate(base)
