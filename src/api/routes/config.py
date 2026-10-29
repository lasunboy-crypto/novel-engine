"""工作区配置 GET / PUT 路由(P3-B-10 GET,P6-B-12 扩 PUT,P6-B-13 扩 reviewer,P6-B-14 扩 retrieval,P6-B-15 扩 cost_guard,P6-B-16 加 API key 独立路由)。

供 ConfigPage 拉合并后的 ``WriterProfilesConfig`` + 保存工作区覆盖层:

| 方法 | 路径 | 说明 |
|------|------|------|
| GET  | ``/workspaces/{workspace_id}/config`` | 工作区合并后配置(只读) |
| PUT  | ``/workspaces/{workspace_id}/config`` | 整体替换工作区覆盖层(P6-B-12) |
| PUT  | ``/workspaces/{workspace_id}/config/cost-guard`` | 局部更新成本守卫并保留其他覆盖 |
| GET  | ``/config/api-keys`` | 全 provider API key 脱敏状态列表(P6-B-16) |
| PUT  | ``/config/api-keys`` | 写入单 provider API key 到 .env(P6-B-16) |

URL 形态选 A(维护人 2026-05-09 拍板)
=====================================
- A(命中):嵌套子资源 ``/workspaces/{id}/config``,RESTful 标准范式;
  P6-B-12 PUT 顺接同 URL。
- B 落选:``/workspaces/{id}:config`` 自定义动词风格,但 ``:`` 在 spec 里
  专给写动作(``:archive`` / ``:clean`` / ``:purge``),GET / PUT 配置不属"动作"。
- C 落选:顶级 ``/config?workspace_id=...``,与"工作区子资源"语义割裂。

回退语义(透传 P3-B-02 ``ConfigService`` 行为)
==============================================
- 工作区目录不存在 / ``project.yaml`` 不存在 / yaml 空 → 返引擎默认配置(200,不抛 404);
  对齐"未提供覆盖等价于不覆盖"语义,前端不需特判。
- 引擎默认 ``config.yaml`` 不存在 → ``ConfigYamlNotFound`` (500),由全局
  异常翻译器兜底为 ``CONFIG_YAML_NOT_FOUND`` 错误码。
- ``project.yaml`` 顶层非 dict / 段落类型错 → ``ConfigYamlInvalid`` (422)。
- 合并后违反交叉校验(routing 引用未定义 profile)→ ``ConfigYamlInvalid`` (422)。

PUT 端点语义(P6-B-12)
========================
- **整体替换**:body 即工作区覆盖层 yaml 内容(``writer_profiles`` 段 +
  ``writer_routing`` 段),完整写入 ``project.yaml``,不与原文件做字段级 diff。
- **先校验后落盘**:body 与引擎默认合并跑一次,Pydantic / ``ValueError``
  失败 → 422 ``CONFIG_YAML_INVALID``(原 ``project.yaml`` 不动)。
- **工作区不存在** → 404 ``WORKSPACE_NOT_FOUND``。
- **热加载**:写完返回合并后完整 ``WorkspaceConfigOut``;``_make_router_factory``
  每次 LLM 调用重读 yaml,PUT 成功后下次任何 chapter:write / pipeline 调用即
  拿到新配置,**无需独立 reload endpoint**(对齐 FRONTEND_SPEC §4.13 实际语义)。
- **WS event**:``workspace.{workspace_id}.events`` 频道发 ``config_updated``
  (publish 失败 swallow,由 service 内层处理)。

无 ``X-Workspace-Id`` header 依赖
=================================
工作区 ID 直接走 URL path 参数(不像 chapters / characters 用 header),因
本端点字面就是"指定工作区的配置",path 参语义清晰且不与"当前活动工作区"
混淆。

P6-B-16 API key 端点(独立路由)
================================
``/config/api-keys`` 不是 workspace 子资源,因 API key 是**进程级凭证**
(全 workspace 共享 ``Settings.claude_api_key``)。挂在 ``api_keys_router``
下与 workspace 配置路径分离,前端 ConfigPage 可与 workspace 配置 PUT
并行调用而无相互影响。
- PUT body 校验失败 / provider 越界 → 422 ``API_KEY_PROVIDER_UNKNOWN``。
- .env 文件写入失败 → 500 ``API_KEY_WRITE_FAILED``。
- 成功后原地刷新 ``settings`` 单例 + WS publish ``api_key_updated`` 到
  ``system.alerts`` 频道 — 无需重启后端,下次任何 Claude 调用即用新 key。
"""

from __future__ import annotations

from typing import Any

import yaml
from fastapi import APIRouter, Body, Depends
from pydantic import ValidationError

from src.api.errors import (
    ApiKeyProviderUnknown,
    ApiKeyWriteFailed,
    ConfigYamlInvalid,
    ConfigYamlNotFound,
    WorkspaceNotFound,
)
from src.api.schemas.common import SuccessEnvelope
from src.api.schemas.config import (
    ApiKeyIn,
    ApiKeyListOut,
    ApiKeyStatusOut,
    CostGuardConfigIn,
    CostGuardConfigOut,
    EngineAgentRoutingUpdateIn,
    GenerationConfigIn,
    GenerationConfigOut,
    RetrievalConfigOut,
    ReviewerConfigOut,
    RoutableAgentOut,
    RoutableAgentsListOut,
    RoutableProfileMeta,
    RoutingConfigOut,
    RoutingRuleOut,
    WorkspaceConfigOut,
    WorkspaceConfigUpdateIn,
    WriterProfileOut,
)
from src.api.websocket.manager import manager as _ws_manager_singleton
from src.core.models.retrieval_config import RetrievalConfig
from src.core.models.reviewer_config import ReviewerConfig
from src.core.models.routing import WriterProfilesConfig
from src.services.api_key_service import (
    ApiKeyProviderUnknownError,
    ApiKeyService,
    ApiKeyWriteFailedError,
)
from src.services.config_service import ConfigService
from src.services.cost_guard import CostGuardConfig

router = APIRouter(prefix="/workspaces", tags=["config"])
api_keys_router = APIRouter(prefix="/config", tags=["config"])


def get_config_service() -> ConfigService:
    """依赖工厂:构造默认 ``ConfigService``(从 ``settings`` 推算路径)。

    P6-B-12 起注入全局 ``_ws_manager_singleton``,供 ``save_workspace_config``
    publish ``config_updated`` WS event;GET 路径不消费 ws_manager 但保留注入
    无副作用。单独抽出工厂便于测试通过 ``app.dependency_overrides`` 注入指向
    ``tmp_path`` + mock ws_manager 的 service。
    """

    return ConfigService(ws_manager=_ws_manager_singleton)


@router.get(
    "/{workspace_id}/config",
    response_model=SuccessEnvelope[WorkspaceConfigOut],
    summary="工作区合并后配置(只读)",
    description=(
        "返回引擎默认 config.yaml ⊕ 工作区 project.yaml 合并后的 "
        "writer_profiles + writer_routing。工作区目录 / project.yaml 缺失时"
        "回退到引擎默认(200)。供 P3-F-02 ConfigPage 简版只读展示。"
    ),
)
async def get_workspace_config(
    workspace_id: str,
    service: ConfigService = Depends(get_config_service),
) -> SuccessEnvelope[WorkspaceConfigOut]:
    try:
        merged = service.load_workspace_config(workspace_id)
        reviewer_merged = service.load_reviewer_config(workspace_id)
        retrieval_merged = service.load_retrieval_config(workspace_id)
        cost_guard_merged = service.load_cost_guard_config_for_workspace(
            workspace_id
        )
        generation_view = service.load_generation_config_view(workspace_id)
    except FileNotFoundError as exc:
        raise ConfigYamlNotFound(str(exc)) from exc
    except (ValueError, yaml.YAMLError) as exc:
        raise ConfigYamlInvalid(str(exc)) from exc
    except ValidationError as exc:
        raise ConfigYamlInvalid(str(exc)) from exc

    return SuccessEnvelope(
        data=_to_response(
            workspace_id,
            merged,
            reviewer_merged,
            retrieval_merged,
            cost_guard_merged,
            generation_view,
        ),
    )


@router.put(
    "/{workspace_id}/config",
    response_model=SuccessEnvelope[WorkspaceConfigOut],
    summary="保存工作区配置(整体替换 project.yaml 覆盖层,P6-B-12;w3-s8 保留 generation 段)",
    description=(
        "整体替换工作区 project.yaml 覆盖层(body 即 yaml 文件内容)。"
        "先与引擎默认合并校验合法 → 写 project.yaml(原文件备份到 .bak)→"
        "返合并后完整视图。下次任何 LLM 调用自动热加载新配置 + WS publish "
        "config_updated 事件到 workspace.{workspace_id}.events 频道。"
        "工作区不存在 → 404 WORKSPACE_NOT_FOUND;字段 / 交叉校验失败 → 422 "
        "CONFIG_YAML_INVALID(原 project.yaml 不动)。"
        "**v1.1 w3-s8**:generation 段有专属端点(PUT /config/generation,L1)"
        "做读-改-存;body 未提及 generation 时本端点**保留**现有该段,避免"
        "ConfigPage 只发 writer 两段时冲掉 L1 设的生成参数。显式 generation: null "
        "仍清空。其余 5 段无专属端点,沿用整体替换(省略 = 清空)。"
    ),
)
async def update_workspace_config(
    workspace_id: str,
    body: WorkspaceConfigUpdateIn = Body(...),
    service: ConfigService = Depends(get_config_service),
) -> SuccessEnvelope[WorkspaceConfigOut]:
    try:
        # v1.1 w3-s8:走 ``save_workspace_config_overlay`` 而非直接 ``save_workspace_config``
        # —— 后者整体替换会冲掉 L1 专属端点设的 generation 段(ConfigPage 只发 writer
        # 两段)。overlay 入口在 body 未显式提及 generation 时保留现有该段(详见服务方法
        # docstring)。``model_fields_set`` 区分"省略保留"与"显式 null 清空"。
        merged = service.save_workspace_config_overlay(
            workspace_id,
            body.to_yaml_dict(),
            explicit_sections=set(body.model_fields_set),
        )
        # P6-B-13/P6-B-14/P6-B-15:reviewer / retrieval / cost_guard 段已在
        # save_workspace_config 内 roundtrip 校验,此处只重读一次拼装响应
        # (load_*_config 已等价 fail-loud)。
        reviewer_merged = service.load_reviewer_config(workspace_id)
        retrieval_merged = service.load_retrieval_config(workspace_id)
        cost_guard_merged = service.load_cost_guard_config_for_workspace(
            workspace_id
        )
        generation_view = service.load_generation_config_view(workspace_id)
        # v1.1 wave 2 H6:走统一 invalidate 入口 ``reset_workspace_config_caches``
        # (P7-B-05 cost_guard reset 收编 + 未来扩展锚点)。该函数内部清:
        # ① cost_guard 工作区 lazy cache,让本次 PUT 的新值在下次
        #    ChapterService._run_gates 调用时即时生效(P7-B-05);
        # ② reviewer / retrieval 配置每次 caller 时由 ConfigService 实例化重读,
        #    无独立缓存,无需 invalidate(参 helper docstring 说明);
        # ③ writer_routing / agent_routing 由 ``_make_router_factory()`` 每次
        #    LLM 调用重读 yaml (P3-B-06),routing 改完立即生效。
        from src.api.deps import reset_workspace_config_caches

        reset_workspace_config_caches(workspace_id)
    except FileNotFoundError as exc:
        raise WorkspaceNotFound(str(exc)) from exc
    except (ValueError, yaml.YAMLError) as exc:
        raise ConfigYamlInvalid(str(exc)) from exc
    except ValidationError as exc:
        raise ConfigYamlInvalid(str(exc)) from exc

    return SuccessEnvelope(
        data=_to_response(
            workspace_id,
            merged,
            reviewer_merged,
            retrieval_merged,
            cost_guard_merged,
            generation_view,
        )
    )


@router.get(
    "/{workspace_id}/config/generation",
    response_model=SuccessEnvelope[GenerationConfigOut],
    summary="工作区生成参数配置(只读,v1.1 L1 Q16)",
    description=(
        "返回引擎默认 config.yaml ``generation:`` 段 ⊕ 工作区 project.yaml "
        "``generation:`` 段字段级合并后的生成参数(字数下限/上限 + 重写次数上限)。"
        "工作区目录 / project.yaml / generation 段缺失时回退引擎默认(200)。"
        "供 L1 章节卡页面侧边浮层只读展示 + 预填表单。"
    ),
)
async def get_generation_config(
    workspace_id: str,
    service: ConfigService = Depends(get_config_service),
) -> SuccessEnvelope[GenerationConfigOut]:
    try:
        view = service.load_generation_config_view(workspace_id)
    except FileNotFoundError as exc:
        raise ConfigYamlNotFound(str(exc)) from exc
    except (ValueError, yaml.YAMLError) as exc:
        raise ConfigYamlInvalid(str(exc)) from exc
    except ValidationError as exc:
        raise ConfigYamlInvalid(str(exc)) from exc

    return SuccessEnvelope(
        data=GenerationConfigOut.model_validate(view),
    )


@router.put(
    "/{workspace_id}/config/generation",
    response_model=SuccessEnvelope[GenerationConfigOut],
    summary="保存工作区生成参数配置(只改 generation 段,v1.1 L1 Q16)",
    description=(
        "只更新工作区 project.yaml 的 ``generation:`` 段(服务端读现有覆盖层 → "
        "替换 generation 键 → 整体存回),**保留 reviewer / retrieval / cost_guard "
        "/ writer 等其他覆盖段不动**(对齐 L1 侧边浮层只编辑生成参数的语义)。"
        "字段级合并:body 只填部分字段时其余沿用引擎默认。先合并校验合法 → 写盘"
        "(原文件备份 .bak)→ WS publish config_updated → 返合并后视图。"
        "工作区不存在 → 404 WORKSPACE_NOT_FOUND;字段 / 交叉校验失败 "
        "(word_count_min > word_count_max 等)→ 422 CONFIG_YAML_INVALID(原"
        " project.yaml 不动)。下次新生成章节卡自动用新默认作字数 / 重写次数兜底。"
    ),
)
async def update_generation_config(
    workspace_id: str,
    body: GenerationConfigIn = Body(...),
    service: ConfigService = Depends(get_config_service),
) -> SuccessEnvelope[GenerationConfigOut]:
    try:
        service.save_generation_config(workspace_id, body.to_override_dict())
        from src.api.deps import reset_workspace_config_caches

        reset_workspace_config_caches(workspace_id)
        # 校准联动:save 后重读三态视图(override + 校准默认 + 生效值),与 GET 同一构造。
        view = service.load_generation_config_view(workspace_id)
    except FileNotFoundError as exc:
        raise WorkspaceNotFound(str(exc)) from exc
    except (ValueError, yaml.YAMLError) as exc:
        raise ConfigYamlInvalid(str(exc)) from exc
    except ValidationError as exc:
        raise ConfigYamlInvalid(str(exc)) from exc

    return SuccessEnvelope(
        data=GenerationConfigOut.model_validate(view),
    )


@router.put(
    "/{workspace_id}/config/cost-guard",
    response_model=SuccessEnvelope[CostGuardConfigOut],
    summary="局部更新工作区成本守卫配置",
    description=(
        "只合并工作区 project.yaml 的 cost_guard 段，保留 writer、reviewer、"
        "retrieval、generation 等其他覆盖。写入后立即清理工作区成本守卫缓存，"
        "下一次 Pipeline Continue 使用新上限。"
    ),
)
async def update_cost_guard_config(
    workspace_id: str,
    body: CostGuardConfigIn = Body(...),
    service: ConfigService = Depends(get_config_service),
) -> SuccessEnvelope[CostGuardConfigOut]:
    try:
        merged = service.save_cost_guard_config(
            workspace_id, body.to_override_dict()
        )
        from src.api.deps import reset_workspace_config_caches

        reset_workspace_config_caches(workspace_id)
    except FileNotFoundError as exc:
        raise WorkspaceNotFound(str(exc)) from exc
    except (ValueError, yaml.YAMLError) as exc:
        raise ConfigYamlInvalid(str(exc)) from exc
    except ValidationError as exc:
        raise ConfigYamlInvalid(str(exc)) from exc

    return SuccessEnvelope(
        data=CostGuardConfigOut.model_validate(merged.model_dump()),
    )


def _to_response(
    workspace_id: str,
    config: WriterProfilesConfig,
    reviewer: ReviewerConfig,
    retrieval: RetrievalConfig,
    cost_guard: CostGuardConfig,
    generation_view: dict[str, Any],
) -> WorkspaceConfigOut:
    """把 ``core`` 内部模型翻译到 API 表面 schema。

    P6-B-13 起加 ``reviewer: ReviewerConfig`` 参数;P6-B-14 起加
    ``retrieval: RetrievalConfig`` 参数;P6-B-15 起加
    ``cost_guard: CostGuardConfig`` 参数;v1.1 L1 起加
    ``generation: GenerationConfig`` 参数,在响应里暴露合并后的工作区级
    reviewer 阈值默认 + retrieval 检索默认 + cost_guard 成本守卫默认 +
    generation 生成参数默认。
    """

    profiles_out = {
        profile_id: WriterProfileOut.model_validate(profile.model_dump())
        for profile_id, profile in config.profiles.items()
    }
    routing_out = RoutingConfigOut(
        default=config.routing.default,
        rules=[
            RoutingRuleOut(condition=rule.condition, profile=rule.profile)
            for rule in config.routing.rules
        ],
        agent_routing=dict(config.routing.agent_routing),
    )
    reviewer_out = ReviewerConfigOut.model_validate(reviewer.model_dump())
    retrieval_out = RetrievalConfigOut.model_validate(retrieval.model_dump())
    cost_guard_out = CostGuardConfigOut.model_validate(cost_guard.model_dump())
    # generation_view = ConfigService.load_generation_config_view 产的三态视图 dict
    # (含 trim override + 校准默认 + 生效值);非裸 GenerationConfig.model_dump()——
    # 后者缺 W3.5 校准联动三态字段(*_calibration_default 必填)会 ValidationError。
    generation_out = GenerationConfigOut.model_validate(generation_view)
    return WorkspaceConfigOut(
        workspace_id=workspace_id,
        profiles=profiles_out,
        routing=routing_out,
        reviewer=reviewer_out,
        retrieval=retrieval_out,
        cost_guard=cost_guard_out,
        generation=generation_out,
    )


# =============================================================================
# API key 凭证管理(P6-B-16,独立路由)
# =============================================================================


def get_api_key_service() -> ApiKeyService:
    """依赖工厂:构造默认 ``ApiKeyService``(``.env`` 相对工作目录 + 全局 ws_manager)。

    单独抽出工厂便于测试通过 ``app.dependency_overrides`` 注入指向 tmp_path
    + mock ws_manager 的 service。
    """

    return ApiKeyService(ws_manager=_ws_manager_singleton)


@api_keys_router.get(
    "/api-keys",
    response_model=SuccessEnvelope[ApiKeyListOut],
    summary="API key 脱敏状态列表(P6-B-16)",
    description=(
        "返回所有 KNOWN_PROVIDERS 的 API key 配置状态;每 provider 一项,"
        "已配置 / 未配置都列出。已配置项 masked_value 回显前 8 字符 + ``***`` "
        "后缀。未配置项 configured=False + masked_value=null。"
    ),
)
async def list_api_keys(
    service: ApiKeyService = Depends(get_api_key_service),
) -> SuccessEnvelope[ApiKeyListOut]:
    items = service.get_masked()
    api_keys = [ApiKeyStatusOut.model_validate(item) for item in items]
    return SuccessEnvelope(data=ApiKeyListOut(api_keys=api_keys))


@api_keys_router.put(
    "/api-keys",
    response_model=SuccessEnvelope[ApiKeyStatusOut],
    summary="写入单 provider API key 到 .env(P6-B-16)",
    description=(
        "PUT body ``{provider, api_key}`` → 写入 .env(保留其他变量 / 注释 / "
        "顺序)+ 原地刷新 Settings 单例 + WS publish ``api_key_updated`` 到 "
        "system.alerts 频道。下次任何 LLM 调用即用新 key,无需重启后端。"
        "返回该 provider 的脱敏状态(对齐 GET 单条元素结构)。"
        "provider 越界 → 422 API_KEY_PROVIDER_UNKNOWN;.env 写入失败 → 500 "
        "API_KEY_WRITE_FAILED。"
    ),
)
async def update_api_key(
    body: ApiKeyIn = Body(...),
    service: ApiKeyService = Depends(get_api_key_service),
) -> SuccessEnvelope[ApiKeyStatusOut]:
    try:
        status = service.put(body.provider, body.api_key)
    except ApiKeyProviderUnknownError as exc:
        raise ApiKeyProviderUnknown(str(exc)) from exc
    except ApiKeyWriteFailedError as exc:
        raise ApiKeyWriteFailed(str(exc)) from exc

    return SuccessEnvelope(data=ApiKeyStatusOut.model_validate(status))


# =============================================================================
# 可路由 agent 清单(v1.1 wave 2 H3,独立路由 /config/routable-agents)
# =============================================================================


# 全系统可路由的 LLM-driven agent 元信息表(中文 display_name + description)。
#
# **修改本表时同步**:① ``src/core/models/routing.py:AGENT_NAME`` Literal
# ② ``src/api/schemas/config.py:RoutingConfigOut.agent_routing`` description。
# 三处保持枚举一致。**纯规则 validator 不在此表**(style_drift /
# cross_chapter_voice)— 它们不调 LLM,UI 切换无意义。
# v1.1 w4-s5 followup(2026-06-01):本表补齐 A3 mergers(plot_thread_merger /
# character_disambiguator / timeline_entry_merger)+ ``style_auditor``;v1.1 w6-s1
# 补 mechanism_consistency_validator + thread_closure_validator;v1.1 acceptance
# 修复2 补 volume_seam_replay_validator(修复1 漏登)+ must_feasibility_validator,
# 与 ``AGENT_NAME`` Literal 完整对齐(29 个 LLM-driven agent,含 reviewer_escalation
# 设计保留项)。
_ROUTABLE_AGENTS_METADATA: tuple[tuple[str, str, str], ...] = (
    # (agent_name, display_name, description)
    # ---- P1-P2 ----
    ("writer", "正文写作", "章节正文生成,核心创作 agent"),
    ("reviewer", "审稿评分", "章节正文 6 维度打分 + verdict 判定 (pass / revise / rewrite)"),
    ("reviser", "局部修订", "针对 reviewer revise 反馈做小幅修订 (不重写整章)"),
    ("memory_extractor", "记忆抽取", "章节通过后从正文抽取结构化记忆 (人物 / 时间线 / 设定)"),
    ("continuity", "衔接桥抽取", "为下一章准备前章末尾 800 字衔接桥 (含场景 / 情绪 / 视角延续)"),
    # ---- P3 ----
    ("reranker", "检索精排", "向量召回 top_20 精排到 top_5,提升上下文召回精度"),
    (
        "reviewer_escalation",
        "审稿边界升级",
        "首审分数落入模糊区 [escalation_lower, escalation_upper] 时升 Claude 复审 (默认 disable)",
    ),
    # ---- P4-P5 ----
    ("outline_builder", "大纲生成", "整套大纲生成 — 世界 / 角色 / 情节 / 卷-章双层"),
    ("chapter_card_generator", "章节卡设计", "单章 must_achieve / scene / 衔接桥 / 节奏 / 张力等结构化设计"),
    (
        "mini_plan_generator",
        "卷内节奏分配",
        "卷级 mini_plan 一行事件生成 (字面留观:实际行为受 outline_builder fallback 控制,v1.1 G3 后续按 stage 派发)",
    ),
    (
        "memory_health_checker",
        "记忆健康检查",
        "工作区记忆健康度 LLM 判定 — stale / inactive / orphan 三类 issue 检测",
    ),
    # ---- v1.0.x wave 4 ----
    (
        "chapter_summary_extractor",
        "章节摘要抽取",
        "章节通过后抽取 1-2 段摘要落 ChapterSummaryORM,供下游 derive_context 拉长程记忆",
    ),
    # ---- v1.1 wave 1(LLM-driven validator 族) ----
    (
        "continuity_validator",
        "衔接桥校验 (LLM)",
        "对照前章衔接桥与当前章正文,LLM-as-judge 判定 4 类断裂 (场景 / 情绪 / 视角 / 关键道具)",
    ),
    (
        "must_achieve_validator",
        "must_achieve 命中校验 (LLM)",
        "逐条比对 chapter_card.must_achieve 与正文,LLM-as-judge 给出每条命中 / 未命中判定",
    ),
    (
        "beat_alignment_validator",
        "Beat 对齐校验 (LLM)",
        "v1.1 G3 — mini_plan one_line_event 是否对齐 beat.summary + 触发 forbidden_advance",
    ),
    (
        "must_vs_forbidden_validator",
        "must vs forbidden 互斥校验 (LLM)",
        "v1.1 G20 — chapter_card.must_achieve vs forbidden 笛卡尔积矛盾判定 (含同主题信息密度互斥)",
    ),
    # ---- v1.1 wave 2 session 5(A3 治本:Memory Extractor LLM 兜底合并族)----
    (
        "plot_thread_merger",
        "伏笔线索合并 (LLM)",
        "v1.1 A3 — 本章抽取的 plot_thread 与工作区已有 plot_thread 语义合并判定 (同伏笔不同表述/简称别名)",
    ),
    (
        "character_disambiguator",
        "角色消歧 (LLM)",
        "v1.1 A3 — 描述性指代 (灰袍人/胖官员) vs 真名披露场景的角色身份消歧 (规则版 0.85 相似度漏抓的兜底)",
    ),
    (
        "timeline_entry_merger",
        "时间线条目合并 (LLM)",
        "v1.1 A3 — 跨章回忆/倒叙场景同事件 timeline_entry 语义去重合并 (预防性兜底)",
    ),
    # ---- v1.1 wave 4 session 5(I2.2:StyleAuditor 落地)----
    (
        "style_auditor",
        "风格模板审计 (LLM)",
        "v1.1 I2.2 — 章节产出 vs 工作区所选写作风格模板期望 (修辞/对白/调性) 的偏移度审计 (独立 /style/audit 端点)",
    ),
    # ---- v1.1 wave 6 session 1(A1 + A4:outline 生成期内容护栏)----
    (
        "mechanism_consistency_validator",
        "大纲机制一致性 (LLM)",
        "v1.1 A1 — 大纲生成期 world 段高概念词是否配可执行规则 + 是否给两个互斥答案的自检 (真矛盾 HARD retry / 缺规则 WARN)",
    ),
    (
        "thread_closure_validator",
        "伏笔收尾 (LLM)",
        "v1.1 A4 — 全书 long_thread 在结局区是否收口的批量判定 (closed / 有交代开放护 C1 / 埋了不收 WARN)",
    ),
    # ---- v1.1 wave 6 session 2(A3:角色双界身份完整性)----
    (
        "identity_coverage_validator",
        "身份覆盖校验 (LLM)",
        "v1.1 A3 — 多情境身份角色 (双界/卧底/易容) 每个身份侧面在全书 mini_plan 事件中是否至少交代一次的批量判定 (未覆盖 WARN)",
    ),
    # ---- v1.1 wave 6 session 3(B1 + B2:下游设定/场景一致性)----
    (
        "setting_consistency_validator",
        "设定一致性校验 (LLM)",
        "v1.1 B1 — 审稿期本章正文是否违反设定契约 (world 不变规则 + 出场角色固定身份) 的判定 (绣春刀穿帮/肉体在不在/身份写串,冲突才报 WARN,护留白)",
    ),
    (
        "scene_anchor_validator",
        "双时空场景锚点复核 (LLM)",
        "v1.1 B2 — extract 后醒来点≠入梦点且无位移交代时,复核是否真硬跳 (肉体瞬移) 还是剧情自然差异的判定 (WARN)",
    ),
    # ---- v1.1 wave 6 session 4(B4:正文跨章去重 / B6:终局张力曲线)----
    (
        "cross_chapter_dedup_validator",
        "正文跨章去重判定 (LLM)",
        "v1.1 B4 — 审稿期代码相似度初筛命中的跨章重复片段是注水复读还是有意回扣 (母题呼应/章末钩子) 的判定 (破空套路/玉佩逐句重写,WARN,护有意手法)",
    ),
    (
        "ending_tension_validator",
        "终局张力曲线判定 (LLM)",
        "v1.1 B6 — 仅全书终章触发,判结局是否与前文张力断裂 (强行宣告岁月静好) 还是自然收束的判定 (WARN,护平静收尾本身不是 bug)",
    ),
    # ---- v1.1 acceptance 修复1 + 修复2(2026-06-10;修复1 当时漏登本表,修复2 一并补)----
    (
        "volume_seam_replay_validator",
        "卷界事件重演判定 (LLM)",
        "v1.1 acceptance 修复1 — outline:commit 期判新卷首章是否重演前卷末章已完结事件 (must 永不可达成死锁拦截)",
    ),
    (
        "must_feasibility_validator",
        "写前 must 可行性预检 (LLM)",
        "v1.1 acceptance 修复2 / Fix D — 写前逐条判 must_achieve 是否已被前文消费 (高置信不可达 → 提前升人工,省 4 版重写)",
    ),
    # ---- v2 W3.5-S3(大纲跨段一致性校验)----
    (
        "outline_consistency_validator",
        "大纲跨段一致性校验 (LLM)",
        "v2 W3.5-S3 — 大纲 4 段生成后跨段判名字/称谓矛盾 + 角色混淆/张冠李戴 (HARD 阻断) + 卷规划覆盖 (WARN);deepseek 生成 + qwen 校验组合拳",
    ),
    # ---- v2 W3.5 补强3(正文级 forbidden 越界下游硬闸)----
    (
        "forbidden_crossing_validator",
        "正文 forbidden 越界判定 (LLM)",
        "v2 W3.5 补强3 — 写后读正文 vs 卡 forbidden 语义比对,判本章是否真实发生被禁的事 (尤其隐式进入类越界);越界确凿 HARD 覆盖 verdict=rewrite → reviser 删越界重写;qwen 严格第二双眼",
    ),
)


@api_keys_router.get(
    "/routable-agents",
    response_model=SuccessEnvelope[RoutableAgentsListOut],
    summary="可路由 agent 清单 (v1.1 wave 2 H3)",
    description=(
        "返回全系统 LLM-driven 可路由 agent 完整清单 (27 个,含 reviewer_escalation "
        "设计保留项)。每项含 agent_name / display_name (中文) / description / "
        "current_profile (当前生效 profile_id) / default_profile (兜底)。"
        "可选 query 参数 ``workspace_id`` — 给定时 current_profile 反映该工作区"
        "合并后的字段值;省略时反映引擎默认 config.yaml (全局视图)。"
        "前端 H4/H5 Settings 页面用本端点画 agent 切换器 dropdown,用户改完"
        "走 PUT /workspaces/{id}/config 的 agent_routing 段覆盖。"
        "**纯规则 validator** (style_drift / cross_chapter_voice) 不在本端点 "
        "— 它们不调 LLM,UI 切换无意义。"
    ),
)
async def list_routable_agents(
    workspace_id: str | None = None,
    service: ConfigService = Depends(get_config_service),
) -> SuccessEnvelope[RoutableAgentsListOut]:
    """列出全系统可路由 agent + 当前生效 profile。

    ``workspace_id`` 给定时走工作区合并视图(``ConfigService.load_workspace_config``);
    省略 / None 时走引擎默认 ``config.yaml`` 的全局视图(对齐 fallback 语义)。

    yaml 加载失败(``config.yaml`` 不存在 / 解析错 / 交叉校验失败)→ 422
    ``ConfigYamlInvalid``(对齐 ``get_workspace_config`` 错误处置)。
    """
    try:
        if workspace_id is not None and workspace_id.strip():
            merged = service.load_workspace_config(workspace_id)
        else:
            # 引擎默认视图 — 用 ConfigService 内部走 load_writer_profiles_config
            # 的等价路径(workspace_id="" 触发 fallback,但 PROJECT_SPEC 字面"未
            # 提供覆盖等价于不覆盖" — 走"不存在工作区"分支即拿引擎默认)。
            from src.config import settings as _settings
            from src.core.config_loader import load_writer_profiles_config

            merged = load_writer_profiles_config(_settings.config_yaml_path)
        # 在 try 内构建响应 —— _build_routable_agents_list 读 model_calibration 段,
        # 段脏时 load_model_calibration 抛 ValidationError,与上方 config 加载错误
        # 同样翻 422 ConfigYamlInvalid(对齐"不吞坏 config",避免漏成 500)。
        result = _build_routable_agents_list(merged)
    except FileNotFoundError as exc:
        raise ConfigYamlNotFound(str(exc)) from exc
    except (ValueError, yaml.YAMLError) as exc:
        raise ConfigYamlInvalid(str(exc)) from exc
    except ValidationError as exc:
        raise ConfigYamlInvalid(str(exc)) from exc

    return SuccessEnvelope(data=result)


def _build_routable_agents_list(
    merged: WriterProfilesConfig,
) -> RoutableAgentsListOut:
    """从合并后 ``WriterProfilesConfig`` 构建 routable-agents 响应(GET / PUT 共用)。

    W3.5-s5 扩(plan §4):
    - 每 agent 标 ``is_llm_judge``(``LLM_AS_JUDGE_AGENTS`` 成员)→ 前端 judge
      硬护栏 UI 拦截;
    - 每 profile 暴露 ``profile_meta``(model / thinking_on / calibrated /
      calibrated_at)→ 前端校准状态显示 + 未校准警告 + judge thinking 判定。

    校准按 profile 的**模型名** key(``model_calibration`` 段,W3.5-s2),与生成期
    ``outline_service._resolve_length_calibration`` 同一真相源(``config.yaml`` 引擎层)。
    校准段缺失 → 空 dict(优雅,所有模型 ``calibrated=False``);段存在但脏 →
    ``load_model_calibration`` fail-fast 抛 ``ValidationError``(caller try 块翻 422,
    对齐 ``_resolve_length_calibration`` 不吞坏 config 的纪律)。
    """
    from src.config import settings as _settings
    from src.core.config_loader import load_model_calibration
    from src.core.models.routing import (
        LLM_AS_JUDGE_AGENTS,
        is_thinking_on_profile,
    )

    default_profile = merged.routing.default
    agent_routing = merged.routing.agent_routing
    available_profiles = sorted(merged.profiles.keys())

    calibrations = load_model_calibration(_settings.config_yaml_path)
    profile_meta = {
        profile_id: RoutableProfileMeta(
            model=profile.model,
            thinking_on=is_thinking_on_profile(profile),
            calibrated=profile.model in calibrations,
            calibrated_at=(
                calibrations[profile.model].calibrated_at
                if profile.model in calibrations
                else None
            ),
        )
        for profile_id, profile in merged.profiles.items()
    }

    agents = [
        RoutableAgentOut(
            agent_name=agent_name,
            display_name=display_name,
            description=description,
            llm_driven=True,
            is_llm_judge=agent_name in LLM_AS_JUDGE_AGENTS,
            current_profile=agent_routing.get(agent_name, default_profile),
            default_profile=default_profile,
        )
        for agent_name, display_name, description in _ROUTABLE_AGENTS_METADATA
    ]
    return RoutableAgentsListOut(
        agents=agents,
        available_profiles=available_profiles,
        profile_meta=profile_meta,
    )


@api_keys_router.put(
    "/agent-routing",
    response_model=SuccessEnvelope[RoutableAgentsListOut],
    summary="保存引擎默认 agent routing(全局层,v1.1 wave 3 H5)",
    description=(
        "整段替换引擎默认 config.yaml 的 writer_routing.agent_routing 段(全局层,"
        "所有工作区共用)。请求体 agent_routing 即新的完整映射:列出的 agent 用"
        "对应 profile,未列出的 agent 回退到 writer_routing.default 兜底;空 dict "
        "清空所有覆盖。ruamel.yaml round-trip 写入保留 config.yaml 手写注释。"
        "校验失败(引用未声明 profile / LLM-judge agent 切到 thinking-off)→ 422 "
        "CONFIG_YAML_INVALID,不落盘。返回写盘后刷新的全局 routable-agents 视图。"
        "工作区级覆盖仍走 PUT /workspaces/{id}/config 的 agent_routing 段。"
    ),
)
async def update_engine_agent_routing(
    body: EngineAgentRoutingUpdateIn = Body(...),
    service: ConfigService = Depends(get_config_service),
) -> SuccessEnvelope[RoutableAgentsListOut]:
    """写引擎默认 agent_routing + 返回刷新后的全局视图。

    错误映射对齐 ``get_workspace_config``:config.yaml 缺失 → 422 ConfigYamlNotFound;
    交叉校验 / hard guard / yaml 结构异常 → 422 ConfigYamlInvalid。
    """
    try:
        merged = service.save_engine_agent_routing(body.agent_routing)
        result = _build_routable_agents_list(merged)
    except FileNotFoundError as exc:
        raise ConfigYamlNotFound(str(exc)) from exc
    except (ValueError, yaml.YAMLError) as exc:
        raise ConfigYamlInvalid(str(exc)) from exc
    except ValidationError as exc:
        raise ConfigYamlInvalid(str(exc)) from exc

    return SuccessEnvelope(data=result)
