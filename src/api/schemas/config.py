"""工作区配置 GET / PUT 暴露 schema(P3-B-10 GET,P6-B-12 扩 PUT,P6-B-13 扩 reviewer 段,P6-B-14 扩 retrieval 段,P6-B-15 扩 cost_guard 段,P6-B-16 加 API key 独立路由 schema)。

P3-F-02 ConfigPage 简版只读展示当前工作区合并后的 ``WriterProfilesConfig``
(引擎默认 ``config.yaml`` ⊕ 工作区 ``project.yaml``)。P6-B-12 起新增 PUT 端点
对应 ConfigPage 完整版 CRUD —— body 即工作区覆盖层 yaml dict 内容。本模块定义
两组 schema:

- ``*Out``(GET 响应)— 暴露合并后的完整 ``WriterProfilesConfig`` 表面。
- ``*In``(PUT body)— 仅暴露工作区覆盖层(可缺省段,字面对齐 project.yaml
  文件结构)。``In`` 与 ``Out`` 分离避免"读写字段集偏移"导致前端无法直接
  ``PUT(GET 结果)``——P6-F-04 前端需自行拼装"工作区覆盖层"再 PUT。

字段暴露范围(P3-B-10 自决项 + P6-B-12 沿用)
==============================================
- ``WriterProfile`` 全字段暴露:``profile_id`` / ``provider`` / ``model`` /
  ``base_url`` / ``context_window`` / ``generation_params`` /
  ``extra_request_kwargs`` / ``cost_per_1k_input`` / ``cost_per_1k_output``
  / ``cost_currency`` / ``notes``。
- **无敏感数据**:Anthropic API key / Ollama 是否在线等运行时凭证 / 状态
  都不在 ``WriterProfile`` 字段范围,profile 数据本身公开;真正的 API key
  走 ``Settings`` env 变量(``src/config.py``),与本端点解耦。
- ``RoutingConfig`` 全字段暴露:``default`` / ``rules`` / ``agent_routing``。

P6-B-13 PUT body 扩 reviewer 段
================================
- 新 ``ReviewerConfigIn`` — 工作区级 reviewer 阈值默认(字段全可选,对齐
  P6-B-12 ``RoutingConfigIn`` 模式;省略 → 不覆盖)。
- ``WorkspaceConfigUpdateIn`` 顶层新增 ``reviewer: ReviewerConfigIn | None`` 段。
- ``to_yaml_dict()`` 序列化扩 reviewer 段(None 不落 yaml)。

P6-B-14 PUT body 扩 retrieval 段
================================
- 新 ``RetrievalConfigIn`` — 工作区级检索默认(字段全可选,对齐 P6-B-13
  ``ReviewerConfigIn`` 模式;省略 → 不覆盖)。
- ``WorkspaceConfigUpdateIn`` 顶层新增 ``retrieval: RetrievalConfigIn | None`` 段。
- ``to_yaml_dict()`` 序列化扩 retrieval 段(None 不落 yaml)。

P6-B-15 PUT body 扩 cost_guard 段
==================================
- 新 ``CostGuardConfigIn`` — 工作区级成本守卫默认(字段全可选,对齐 P6-B-14
  ``RetrievalConfigIn`` 模式;省略 → 不覆盖)。
- 新 ``CostGuardConfigOut`` — GET 响应合并视图;暴露 5 字段。
- ``WorkspaceConfigUpdateIn`` 顶层新增 ``cost_guard: CostGuardConfigIn | None`` 段。
- ``WorkspaceConfigOut`` 顶层新增 ``cost_guard: CostGuardConfigOut`` 必填段。
- ``to_yaml_dict()`` 序列化扩 cost_guard 段(None 不落 yaml)。
- ``CostGuardConfig`` 数据模型复用 ``src/services/cost_guard.py`` 既有实现
  (P4-B-08 引入)— 字段语义与默认值不重复定义。

P6-B-16 API key 独立路由 schema
================================
API key 与 ``writer_profiles`` / ``writer_routing`` / ``reviewer`` / ``retrieval``
/ ``cost_guard`` 五段**不混路由**:走独立 ``GET / PUT /api/v1/config/api-keys``
端点。理由(P6-B-12 docstring 字面"profile 数据公开 / 凭证解耦"沿用):

- profile 段公开数据,可整体 GET 给前端 ConfigPage 渲染;API key 是凭证,
  GET 必须脱敏 + PUT 走单独路径方便审计 + 不污染 workspace project.yaml。
- API key 是**进程级凭证**(全 workspace 共享 ``Settings.claude_api_key``),
  不是 workspace 子资源。URL 形态对齐:``/config/api-keys`` 顶层路径而非
  ``/workspaces/{id}/config/api-keys``。
- 存储介质:维护人 2026-05-17 拍板走 ``.env`` 文件(与 ``Settings`` env 变量
  同源),非 keyring 系统级凭证存储。``python-dotenv.set_key`` 保证其他 env
  变量、注释、顺序不丢。

字段暴露:
- ``ApiKeyIn``(PUT body):``provider`` Literal + ``api_key`` 非空 ``SecretStr``
  字符串(请求体明文,日志由 ``SecretStr`` 自动 ``***``)。
- ``ApiKeyStatusOut``(GET 响应单 provider):``provider`` + ``configured`` bool
  + ``masked_value`` ``str | None``(未配置时 None;已配置时 ``sk-ant-***`` 前
  8 字符 + 后缀)。
- ``ApiKeyListOut``(GET 响应):``api_keys: list[ApiKeyStatusOut]``。
"""

from __future__ import annotations

from typing import Any, Literal

from pydantic import BaseModel, ConfigDict, Field, field_validator, model_validator

from src.core.llm.openai_compatible_endpoint import (
    resolve_openai_compatible_endpoint,
)


class WriterProfileOut(BaseModel):
    """单个 ``WriterProfile`` 响应体。

    字段语义对齐 ``src/core/models/writer_profile.py:WriterProfile``;此处保留
    独立类型坐标避免 OpenAPI 把 core 内部模型与 API 表面绑定。
    """

    model_config = ConfigDict(extra="forbid")

    profile_id: str = Field(..., description="Profile 唯一 ID")
    provider: str = Field(
        ...,
        description=(
            "LLM provider 枚举:claude / ollama / mock(对齐 P3-B-01 实际取值集,"
            "PROJECT_SPEC §3.7 字面 openai_compatible 类 A 冲突已登记)"
        ),
    )
    model: str = Field(..., description="provider 视角下的模型名")
    base_url: str | None = Field(
        default=None,
        description="非默认 endpoint 时填(ollama 默认 http://localhost:11434);Claude 走 SDK 默认时为 None",
    )
    context_window: int = Field(..., description="模型上下文窗口长度(tokens)")
    generation_params: dict[str, Any] = Field(
        default_factory=dict,
        description="温度 / max_tokens / top_p / top_k 等生成参数",
    )
    extra_request_kwargs: dict[str, Any] = Field(
        default_factory=dict,
        description="provider 扩展字段(如 ollama think:false 顶层透传)",
    )
    cost_per_1k_input: float = Field(
        default=0.0, description="每 1k 输入 token 的配置估算"
    )
    cost_per_1k_output: float = Field(
        default=0.0, description="每 1k 输出 token 的配置估算"
    )
    cost_currency: str = Field(
        default="USD",
        pattern=r"^[A-Z]{3}$",
        description="配置估算币种；不代表 Provider 实际账单",
    )
    notes: str = Field(default="", description="自由文本说明,供 ConfigPage 展示")


class RoutingRuleOut(BaseModel):
    """单条路由规则响应体。"""

    model_config = ConfigDict(extra="forbid")

    condition: str = Field(
        ...,
        description=(
            "布尔表达式字符串(受限 AST 白名单求值,见 src/core/routing/rule_eval.py)。"
            "示例:``is_arc_finale == True`` / ``tension_level >= 9``。"
        ),
    )
    profile: str = Field(..., description="命中本规则时使用的 profile_id")


class RoutingConfigOut(BaseModel):
    """路由配置响应体。

    对齐 ``src/core/models/routing.py:RoutingConfig`` 字段命名,仅作 API 表面
    复刻;同时把 dict 类型字段放成保序 dict(Pydantic v2 默认保 yaml 顺序)。
    """

    model_config = ConfigDict(extra="forbid")

    default: str = Field(
        ...,
        description="默认 profile_id;Writer 路由规则全部不命中时走此 profile",
    )
    rules: list[RoutingRuleOut] = Field(
        default_factory=list,
        description="Writer 路由规则列表,按顺序优先匹配",
    )
    agent_routing: dict[str, str] = Field(
        default_factory=dict,
        description=(
            "Agent 路由表 (P3-plan 决策 1 选 B,v1.1 wave 2 H3 补全清单)。"
            "key=agent_name,value=profile_id。完整 LLM-driven agent 清单:"
            "writer / reviewer / reviser / memory_extractor / continuity / "
            "reranker / reviewer_escalation / outline_builder / "
            "chapter_card_generator / mini_plan_generator / "
            "memory_health_checker / chapter_summary_extractor / "
            "continuity_validator / must_achieve_validator / "
            "beat_alignment_validator / must_vs_forbidden_validator。"
            "未填的 agent 走 default profile 兜底。"
            "纯规则 validator(style_drift / cross_chapter_voice)不在本表 "
            "— 它们不调 LLM。"
            "查询完整可路由 agent 清单走 GET /api/v1/config/routable-agents。"
        ),
    )


class ReviewerConfigOut(BaseModel):
    """合并后的 reviewer 阈值配置响应体(P6-B-13)。

    字段语义与默认值对齐 ``src/core/models/reviewer_config.py:ReviewerConfig``;
    API 表面复刻一份避免 OpenAPI 把 core 内部模型与 API 表面绑定(对齐
    ``RoutingConfigOut`` 同型自决项)。
    """

    model_config = ConfigDict(extra="forbid")

    pass_threshold: float = Field(
        ..., description="通过阈值;score_overall >= 即判 pass"
    )
    revise_threshold: float = Field(
        ..., description="revise 阈值;低于该值判 rewrite"
    )
    escalation_lower_bound: float = Field(
        ..., description="边界升 Claude 复审下界(闭区间)"
    )
    escalation_upper_bound: float = Field(
        ..., description="边界升 Claude 复审上界(闭区间)"
    )


class RetrievalConfigOut(BaseModel):
    """合并后的检索配置响应体(P6-B-14)。

    字段语义与默认值对齐 ``src/core/models/retrieval_config.py:RetrievalConfig``;
    API 表面复刻一份避免 OpenAPI 把 core 内部模型与 API 表面绑定(对齐
    ``RoutingConfigOut`` / ``ReviewerConfigOut`` 同型自决项)。
    """

    model_config = ConfigDict(extra="forbid")

    top_k: int = Field(
        ..., description="检索命中数上限;对齐 retriever DEFAULT_TOP_K 默认 20"
    )
    min_similarity: float = Field(
        ...,
        description=(
            "相似度下界(闭区间);0.0 = 不过滤(默认),> 0.0 时"
            "按 (1 - distance) >= min_similarity 过滤"
        ),
    )


class CostGuardConfigOut(BaseModel):
    """合并后的成本守卫配置响应体(P6-B-15)。

    字段语义与默认值对齐 ``src/services/cost_guard.py:CostGuardConfig``;API
    表面复刻一份避免 OpenAPI 把 service 内部模型与 API 表面绑定(对齐
    ``RoutingConfigOut`` / ``ReviewerConfigOut`` / ``RetrievalConfigOut`` 同型
    自决项)。
    """

    model_config = ConfigDict(extra="forbid")

    cost_currency: str = Field(
        ...,
        pattern=r"^[A-Z]{3}$",
        description="成本守卫阈值币种；不做汇率换算",
    )

    daily_usd_limit: float = Field(
        ...,
        description="全工作区合计单日成本上限(美元);默认 20.0",
    )
    monthly_usd_limit: float = Field(
        ...,
        description="全工作区合计单月成本上限(美元);默认 300.0",
    )
    per_workspace_daily_limit: float = Field(
        ...,
        description="单工作区单日成本上限(美元);默认 10.0",
    )
    action_on_exceed: Literal["pause", "warn", "continue"] = Field(
        ...,
        description=(
            "触发后行为 — pause | warn | continue;默认 pause。"
            "pause:推 system.alerts + caller 暂停新流水线;"
            "warn :推 system.alerts + 日志告警 + 继续;"
            "continue:仅日志(不推 WS,不暂停)"
        ),
    )
    notify_via: list[Literal["log", "websocket"]] = Field(
        ...,
        description="通知通道列表 — log | websocket;空列表禁用通知。默认 [log, websocket]",
    )


class GenerationConfigOut(BaseModel):
    """合并后的生成参数配置响应体(v1.1 L1 Q16)。

    字段语义与默认值对齐 ``src/core/models/generation_config.py:GenerationConfig``;
    API 表面复刻一份避免 OpenAPI 把 core 内部模型与 API 表面绑定(对齐
    ``RetrievalConfigOut`` / ``ReviewerConfigOut`` 同型自决项)。
    """

    model_config = ConfigDict(extra="forbid")

    word_count_policy_version: Literal[0, 1, 2] = Field(
        0, description="篇幅规则：2 约数目标（min=max），1 范围要求，均不因字数拒稿或改写；0 为历史规则。",
    )
    word_count_min: int = Field(
        ..., description="新生成章节卡字数下限默认;对齐 ChapterCard 默认 2500"
    )
    word_count_max: int = Field(
        ..., description="新生成章节卡字数上限默认;对齐 ChapterCard 默认 4000"
    )
    max_rewrites: int = Field(
        ...,
        description="新生成章节卡整章重写次数上限默认;对齐 ChapterCard 默认 3",
    )
    plot_node_budget: int = Field(
        ...,
        description=(
            "新生成章节卡剧情节点预算默认(v1.1 wave 4 s3 / I2.1);对齐 "
            "ChapterCard 默认 3。风格模板作引擎基线注入,L1 用户覆盖优先。"
        ),
    )
    progression_consistency: Literal["auto", "on", "off"] = Field(
        ...,
        description=(
            "模块② 境界一致性(v1.1 wave 5 s10),工作区级开关。auto=按题材定"
            "(古典仙侠 ON / 爽文+热血 OFF / 其余 OFF);on/off 显式覆盖(仅 3 题材生效)。"
        ),
    )
    breakthrough_payoff: Literal["auto", "on", "off"] = Field(
        ...,
        description=(
            "模块① 升级可感化(v1.1 wave 5 s10),工作区级开关。auto=按题材定"
            "(古典仙侠+爽文+热血 ON / 其余 OFF);on/off 显式覆盖(仅 3 题材生效)。"
        ),
    )
    style_anchor_source: Literal["prev_chapter", "book_anchor"] = Field(
        ...,
        description=(
            "writer 风格基线段 + 跨章闸门统一的段均/句均数字锚源(v2 w2-s2 "
            "漂移治理注入端)。prev_chapter=锚前一章指纹(**默认**,v2 间隙 "
            "2026-06-13 起回退为默认);book_anchor=锚全书开篇基线(ch1-3 final "
            "指纹中位数,与趋势检测同锚;只换段均/句均两个数字,POV 判定不动)。"
            "回退原因:全书锚固定数字与'段长由章节张力驱动'规律系统性冲突,经 "
            "G17 在 rewrite 路径把高潮章强制往长(实证);全书级锚留 wave 4 以"
            "张力感知锚正确重新引入。"
        ),
    )
    anti_tic_trim_enabled: bool = Field(
        ...,
        description=(
            "anti-tic trim **生效值**(v2 W3.5 校准联动三态):= ``anti_tic_trim_override``"
            "(非 null 时)否则 ``anti_tic_trim_calibration_default``。即本工作区当前是否真会"
            "跑 anti-tic trim。开时 writer 出稿后加独立 LLM pass 清两类 deepseek 固有 tic"
            "(「不是A是B」+ 演后盖章),绝不动剧情/对白/细节,带三护栏(tic 密度 / 截断 / 越界)。"
        ),
    )
    anti_tic_trim_override: bool | None = Field(
        None,
        description=(
            "anti-tic trim 工作区**三态覆盖**(v2 W3.5 校准联动):``null``=跟随当前 writer "
            "模型校准(取值见 ``anti_tic_trim_calibration_default``)/ ``true``=强制开 / "
            "``false``=强制关。前端三态控件选中态来源(project.yaml generation 原始值)。"
        ),
    )
    anti_tic_trim_calibration_default: bool = Field(
        ...,
        description=(
            "anti-tic trim **校准默认**(v2 W3.5 校准联动):当前 workspace writer 模型校准 "
            "``anti_stamp_strength==high`` → ``true``(如 deepseek);未校准(qwen)→ "
            "``false``。override=null 时的实际取值;前端「跟随模型(当前:开/关)」的'当前'来源。"
        ),
    )
    word_count_trim_enabled: bool = Field(
        ...,
        description=(
            "超长 trim **生效值**(v2 W3.5 校准联动三态):= ``word_count_trim_override``"
            "(非 null 时)否则 ``word_count_trim_calibration_default``。开时 writer 出稿后"
            "(anti-tic 之后)若正文 > word_count_max,加独立 LLM 精简 pass 砍 must 外的水到"
            "≤上限 → 不进 rewrite 循环;bounded-retry 校验 + 弱软下限 2500 + 零 fail-open 兜底。"
        ),
    )
    word_count_trim_override: bool | None = Field(
        None,
        description=(
            "超长 trim 工作区**三态覆盖**(v2 W3.5 校准联动):``null``=跟随当前 writer 模型"
            "校准 / ``true``=强制开 / ``false``=强制关。前端三态控件选中态来源。"
        ),
    )
    word_count_trim_calibration_default: bool = Field(
        ...,
        description=(
            "超长 trim **校准默认**(v2 W3.5 校准联动):当前 workspace writer 模型校准 "
            "``length_tendency==overshoot`` → ``true``(如 deepseek);未校准(qwen)→ "
            "``false``。override=null 时的实际取值;前端「跟随模型(当前:开/关)」的'当前'来源。"
        ),
    )
    outline_autofix_enabled: bool = Field(
        default=True,
        description=(
            "大纲「名字写岔」确定性自动修工作区级开关(v2 W3.5-s4-step1,默认开)。"
            "开时 outline_consistency_validator 判出跨段硬伤后,若全部是可确定性修的 "
            "name_typo(多数票护栏过)→ 确定性归一 + 重验放行;含 substantive/ambiguous "
            "或修后仍不过 → 维持 HARD 拦(零 fail-open)。关 = v1 行为(任何硬伤 HARD 抛)。"
        ),
    )
    character_backfill_enabled: bool = Field(
        default=True,
        description=(
            "大纲角色完整性回填工作区级开关(v2 W3.5-s4-step1 机制1,默认开)。"
            "开时 build_outline 在 plot 后、volume_plans 前检出'plot 当真实角色引用、但 "
            "characters 没建卡'的指代悬空角色 → 补卡(fail-open + 去重 + 封顶 8),让 "
            "volume_plans 拿到完整角色表,从源头掐断张冠李戴混淆。关 = 跳过回填(机制1 前行为)。"
        ),
    )
    core_state_hard_enabled: bool = Field(
        default=True,
        description=(
            "全书核心状态与主时间线 HARD 总开关。True=新生成/新批准章节使用"
            "核心状态投影、迁移许可、同步提交和书内时钟；False=新卡退回 legacy v0，"
            "不回填、不删除既有审计历史。"
        ),
    )


class WorkspaceConfigOut(BaseModel):
    """``GET /api/v1/workspaces/{workspace_id}/config`` 成功响应 ``data`` 段。

    暴露当前工作区**合并后**的 ``WriterProfilesConfig`` + ``ReviewerConfig`` +
    ``RetrievalConfig`` + ``CostGuardConfig``;工作区目录或 ``project.yaml``
    不存在时回退到引擎默认(行为对齐 P3-B-02
    ``ConfigService.load_workspace_config`` fallback 语义)。
    """

    model_config = ConfigDict(extra="forbid")

    workspace_id: str = Field(
        ...,
        description="工作区 ID(回显 URL 路径参数,便于前端缓存键拼装)",
    )
    profiles: dict[str, WriterProfileOut] = Field(
        ...,
        description="profile 字典,key=profile_id",
    )
    routing: RoutingConfigOut = Field(..., description="路由配置")
    reviewer: ReviewerConfigOut = Field(
        ...,
        description=(
            "工作区级 reviewer 阈值配置(P6-B-13);引擎默认 + 工作区覆盖"
            "字段级合并后产出。"
        ),
    )
    retrieval: RetrievalConfigOut = Field(
        ...,
        description=(
            "工作区级检索配置(P6-B-14);引擎默认 + 工作区覆盖"
            "字段级合并后产出。"
        ),
    )
    cost_guard: CostGuardConfigOut = Field(
        ...,
        description=(
            "工作区级成本守卫配置(P6-B-15);引擎默认 + 工作区覆盖"
            "字段级合并后产出。"
        ),
    )
    generation: GenerationConfigOut = Field(
        ...,
        description=(
            "工作区级生成参数配置(v1.1 L1 Q16);引擎默认 + 工作区覆盖"
            "字段级合并后产出。"
        ),
    )


# =============================================================================
# PUT body schema(P6-B-12)
# =============================================================================


class WriterProfileIn(BaseModel):
    """PUT body 内单个 ``WriterProfile`` 覆盖(P6-B-12)。

    字段语义对齐 ``src/core/models/writer_profile.py:WriterProfile``;字段集合
    与 ``WriterProfileOut`` 一致除 **不含 ``profile_id``**(由 ``writer_profiles``
    dict key 给出,body 内不必再重复)——对齐 ``ConfigService._merge_profiles``
    用 dict key 兜底 ``profile_id`` 的 yaml 字面习惯。
    """

    model_config = ConfigDict(extra="forbid")

    provider: Literal["claude", "ollama", "openai_compatible", "mock"] = Field(
        ...,
        description=(
            "LLM provider 枚举:claude / ollama / openai_compatible / mock"
            "(对齐 ``src/core/models/writer_profile.py:WriterProfile.provider``;"
            "v1.1 wave 3 H4 补 openai_compatible 闭合原 P3-B-01 类 A 冲突 — "
            "让 UI 能建 dashscope / DeepSeek / Moonshot 等兼容 provider 的 profile)。"
            "openai_compatible 时 ``base_url`` 必填"
        ),
    )
    model: str = Field(..., min_length=1, description="provider 视角下的模型名")
    base_url: str | None = Field(
        default=None,
        description="非默认 endpoint 时填(ollama 默认 http://localhost:11434);Claude 走 SDK 默认 endpoint 时为 None",
    )
    context_window: int = Field(
        ..., ge=1, description="模型上下文窗口长度(tokens)"
    )
    generation_params: dict[str, Any] = Field(
        default_factory=dict,
        description="温度 / max_tokens / top_p / top_k 等生成参数",
    )
    extra_request_kwargs: dict[str, Any] = Field(
        default_factory=dict,
        description="provider 扩展字段(如 ollama think:false 顶层透传)",
    )
    cost_per_1k_input: float = Field(
        default=0.0, ge=0.0, description="每 1k 输入 token 的配置估算"
    )
    cost_per_1k_output: float = Field(
        default=0.0, ge=0.0, description="每 1k 输出 token 的配置估算"
    )
    cost_currency: str = Field(
        default="USD",
        pattern=r"^[A-Z]{3}$",
        description="配置估算币种；不做汇率换算",
    )
    notes: str = Field(
        default="", description="自由文本说明,供 ConfigPage 展示"
    )

    @field_validator("cost_currency", mode="before")
    @classmethod
    def _normalize_cost_currency(cls, value: Any) -> Any:
        return value.strip().upper() if isinstance(value, str) else value

    @model_validator(mode="after")
    def _validate_base_url_not_empty(self) -> "WriterProfileIn":
        """对齐 ``WriterProfile._validate_provider_specific``。

        - 空字符串 base_url 非法(任何 provider)。
        - openai_compatible provider base_url 必填(无内置默认 endpoint)。
        """
        if self.base_url is not None and self.base_url.strip() == "":
            raise ValueError(
                "base_url 不允许为空字符串;若不需要覆盖默认 endpoint 请传 null"
            )
        if self.provider == "openai_compatible" and (
            self.base_url is None or self.base_url.strip() == ""
        ):
            raise ValueError(
                "provider=openai_compatible 时 base_url 必填(该 provider 无内置"
                "默认 endpoint;请显式声明,如 "
                "https://dashscope.aliyuncs.com/compatible-mode/v1)"
            )
        if self.provider == "openai_compatible" and self.base_url is not None:
            resolve_openai_compatible_endpoint(self.base_url)
        return self


class RoutingRuleIn(BaseModel):
    """PUT body 内单条路由规则覆盖(P6-B-12)。"""

    model_config = ConfigDict(extra="forbid")

    condition: str = Field(
        ...,
        min_length=1,
        description=(
            "布尔表达式字符串(受限 AST 白名单求值,见 src/core/routing/rule_eval.py)。"
            "示例:``is_arc_finale == True`` / ``tension_level >= 9``。"
        ),
    )
    profile: str = Field(
        ..., min_length=1, description="命中本规则时使用的 profile_id"
    )


class RoutingConfigIn(BaseModel):
    """PUT body 内 ``writer_routing`` 段覆盖(P6-B-12)。

    所有字段可选;省略 → 工作区不覆盖该子字段(对齐 ``_merge_routing``
    字段级覆盖语义)。
    """

    model_config = ConfigDict(extra="forbid")

    default: str | None = Field(
        default=None,
        description=(
            "默认 profile_id;省略 → 不覆盖引擎默认。"
            "非空时必须 ≥ 1 字符(空字符串通过 Pydantic 但合并后违反"
            "WriterProfilesConfig 交叉校验 → 422 ConfigYamlInvalid)"
        ),
    )
    rules: list[RoutingRuleIn] | None = Field(
        default=None,
        description="Writer 路由规则列表;省略 → 不覆盖引擎默认。空列表 → 工作区显式清空规则",
    )
    agent_routing: dict[str, str] | None = Field(
        default=None,
        description=(
            "Agent → profile 绑定 dict (PUT body 覆盖层语义)。"
            "省略 → 不覆盖引擎默认;空 dict → 显式清空工作区覆盖。"
            "key 取值范围对齐 ``AGENT_NAME`` Literal (29 个 LLM-driven agent,"
            "见 GET 响应 description 完整清单 / GET /api/v1/config/routable-agents 端点)。"
            "未列入 ``AGENT_NAME`` Literal 的 key 落库时会被 ``WriterProfilesConfig`` "
            "交叉校验拦截 (引用未声明 profile_id) 或路径走 default 兜底,"
            "**不会**抛 422 (保留向后兼容,允许 P5+ 新增 agent 时旧 yaml 仍可加载)。"
        ),
    )


class ReviewerConfigIn(BaseModel):
    """PUT body 内 ``reviewer`` 段覆盖(P6-B-13)。

    所有字段可选;省略 → 工作区不覆盖该子字段(对齐 P6-B-12 ``RoutingConfigIn``
    全可选模式)。整段省略 → 工作区不覆盖 reviewer 段(走引擎默认)。

    字段语义与交叉校验对齐 ``src/core/models/reviewer_config.py:ReviewerConfig``;
    合并后由 ``ConfigService.save_workspace_config`` 调 ``ReviewerConfig.model_validate``
    做最终交叉校验(``revise < pass`` / ``escalation_lower < escalation_upper``)。
    """

    model_config = ConfigDict(extra="forbid")

    pass_threshold: float | None = Field(
        default=None,
        ge=0.0,
        le=10.0,
        description=(
            "通过阈值;省略 → 不覆盖引擎默认(7.5)。"
            "``score_overall >= pass_threshold`` 即判 pass。"
        ),
    )
    revise_threshold: float | None = Field(
        default=None,
        ge=0.0,
        le=10.0,
        description=(
            "revise 阈值;省略 → 不覆盖引擎默认(6.5)。必须严格小于 pass_threshold。"
        ),
    )
    escalation_lower_bound: float | None = Field(
        default=None,
        ge=0.0,
        le=10.0,
        description="边界升 Claude 复审下界;省略 → 不覆盖引擎默认(6.0)。",
    )
    escalation_upper_bound: float | None = Field(
        default=None,
        ge=0.0,
        le=10.0,
        description=(
            "边界升 Claude 复审上界;省略 → 不覆盖引擎默认(8.0)。"
            "必须严格大于 escalation_lower_bound。"
        ),
    )


class RetrievalConfigIn(BaseModel):
    """PUT body 内 ``retrieval`` 段覆盖(P6-B-14)。

    所有字段可选;省略 → 工作区不覆盖该子字段(对齐 P6-B-13 ``ReviewerConfigIn``
    全可选模式)。整段省略 → 工作区不覆盖 retrieval 段(走引擎默认)。

    字段语义与默认值对齐 ``src/core/models/retrieval_config.py:RetrievalConfig``;
    合并后由 ``ConfigService.save_workspace_config`` 调
    ``RetrievalConfig.model_validate`` 做最终字段范围校验
    (``top_k >= 1`` / ``0.0 <= min_similarity <= 1.0``)。
    """

    model_config = ConfigDict(extra="forbid")

    top_k: int | None = Field(
        default=None,
        ge=1,
        description=(
            "检索命中数上限;省略 → 不覆盖引擎默认(20)。"
            "对齐 retriever DEFAULT_TOP_K 默认 20。"
        ),
    )
    min_similarity: float | None = Field(
        default=None,
        ge=0.0,
        le=1.0,
        description=(
            "相似度下界(闭区间);省略 → 不覆盖引擎默认(0.0 = 不过滤)。"
            "> 0.0 时 caller 按 (1 - distance) >= min_similarity 过滤命中。"
        ),
    )


class CostGuardConfigIn(BaseModel):
    """PUT body 内 ``cost_guard`` 段覆盖(P6-B-15)。

    所有字段可选;省略 → 工作区不覆盖该子字段(对齐 P6-B-14 ``RetrievalConfigIn``
    全可选模式)。整段省略 → 工作区不覆盖 cost_guard 段(走引擎默认)。

    字段语义与默认值对齐 ``src/services/cost_guard.py:CostGuardConfig``;
    合并后由 ``ConfigService.save_workspace_config`` 调
    ``CostGuardConfig.model_validate`` 做最终字段范围校验
    (各 ``*_usd_limit`` ``>= 0.0`` / ``action_on_exceed`` Literal /
    ``notify_via`` 列表元素 Literal)。
    """

    model_config = ConfigDict(extra="forbid")

    cost_currency: str | None = Field(
        default=None,
        pattern=r"^[A-Z]{3}$",
        description="成本守卫阈值币种；省略则不覆盖",
    )

    daily_usd_limit: float | None = Field(
        default=None,
        ge=0.0,
        description=(
            "全工作区合计单日成本上限(美元);省略 → 不覆盖引擎默认(20.0)。"
        ),
    )
    monthly_usd_limit: float | None = Field(
        default=None,
        ge=0.0,
        description=(
            "全工作区合计单月成本上限(美元);省略 → 不覆盖引擎默认(300.0)。"
        ),
    )
    per_workspace_daily_limit: float | None = Field(
        default=None,
        ge=0.0,
        description=(
            "单工作区单日成本上限(美元);省略 → 不覆盖引擎默认(10.0)。"
        ),
    )
    action_on_exceed: Literal["pause", "warn", "continue"] | None = Field(
        default=None,
        description=(
            "触发后行为 — pause | warn | continue;省略 → 不覆盖引擎默认(pause)。"
        ),
    )
    notify_via: list[Literal["log", "websocket"]] | None = Field(
        default=None,
        description=(
            "通知通道列表 — log | websocket;省略 → 不覆盖引擎默认 [log, websocket]。"
            "空列表 → 显式禁用所有通知。"
        ),
    )

    @field_validator("cost_currency", mode="before")
    @classmethod
    def _normalize_cost_currency(cls, value: Any) -> Any:
        return value.strip().upper() if isinstance(value, str) else value

    def to_override_dict(self) -> dict[str, Any]:
        """序列化为 ``cost_guard`` 局部覆盖段(None 字段不落盘)。"""

        return self.model_dump(exclude_none=True)


class GenerationConfigIn(BaseModel):
    """专属端点 ``PUT /workspaces/{id}/config/generation`` body(v1.1 L1 Q16)。

    所有字段可选;省略 → 不覆盖该子字段(对齐 ``RetrievalConfigIn`` 全可选模式)。
    侧边浮层通常 3 字段全发;但全可选语义让"只调一项"也能正常合并(缺失字段
    由 ``GenerationConfig`` 引擎默认填补)。

    字段语义与交叉校验对齐 ``src/core/models/generation_config.py:GenerationConfig``;
    合并后由 ``ConfigService.save_generation_config`` 调
    ``GenerationConfig.model_validate`` 做最终字段范围 + 交叉校验
    (``word_count_min <= word_count_max``)。
    """

    model_config = ConfigDict(extra="forbid")

    word_count_policy_version: Literal[0, 1, 2] | None = Field(
        None, description="篇幅规则：2 约数目标（min=max），1 范围要求；省略时保留当前配置。",
    )
    word_count_min: int | None = Field(
        default=None,
        ge=1,
        description="新生成章节卡字数下限默认;省略 → 不覆盖引擎默认(2500)。",
    )
    word_count_max: int | None = Field(
        default=None,
        ge=1,
        description="新生成章节卡字数上限默认;省略 → 不覆盖引擎默认(4000)。",
    )
    max_rewrites: int | None = Field(
        default=None,
        ge=0,
        description=(
            "新生成章节卡整章重写次数上限默认;省略 → 不覆盖引擎默认(3)。"
        ),
    )
    plot_node_budget: int | None = Field(
        default=None,
        ge=1,
        description=(
            "新生成章节卡剧情节点预算默认(v1.1 wave 4 s3 / I2.1);省略 → "
            "不覆盖(引擎默认 3 或风格模板基线)。"
        ),
    )
    progression_consistency: Literal["auto", "on", "off"] | None = Field(
        default=None,
        description=(
            "模块② 境界一致性开关(v1.1 wave 5 s10);省略 → 不覆盖引擎默认(auto)。"
            "auto=按题材定 / on / off(显式覆盖仅 3 题材生效)。"
        ),
    )
    breakthrough_payoff: Literal["auto", "on", "off"] | None = Field(
        default=None,
        description=(
            "模块① 升级可感化开关(v1.1 wave 5 s10);省略 → 不覆盖引擎默认(auto)。"
            "auto=按题材定 / on / off(显式覆盖仅 3 题材生效)。"
        ),
    )
    style_anchor_source: Literal["prev_chapter", "book_anchor"] | None = Field(
        default=None,
        description=(
            "writer 风格基线段数字锚源(v2 w2-s2);省略 → 不覆盖引擎默认"
            "(prev_chapter,v2 间隙 2026-06-13 起从 book_anchor 回退为默认)。"
            "book_anchor=显式锚全书开篇基线(全书级锚留 wave 4 以张力感知锚重做)。"
        ),
    )
    anti_tic_trim_enabled: bool | None = Field(
        default=None,
        description=(
            "anti-tic trim 返工 pass 开关(v2 W3.5 wave-3);省略 → 不覆盖引擎默认(False=关)。"
            "True=writer 出稿后清两类 deepseek 固有 tic(带三护栏,绝不动剧情/对白/细节);"
            "False=engine 级 kill-switch 完全不调用(零回归)。"
        ),
    )
    word_count_trim_enabled: bool | None = Field(
        default=None,
        description=(
            "超长 trim 返工 pass 开关(v2 W3.5-s3rev);省略 → 不覆盖引擎默认(False=关)。"
            "True=writer 出稿后若超 word_count_max,看真正文 trim 精简到 ≤上限(bounded-retry "
            "校验 + 弱软下限 2500 + 零 fail-open 兜底)→ 不进 rewrite 循环;False=完全不调用(零回归)。"
        ),
    )
    outline_autofix_enabled: bool | None = Field(
        default=None,
        description=(
            "大纲「名字写岔」确定性自动修开关(v2 W3.5-s4-step1);省略 → 不覆盖引擎默认"
            "(True=开)。True=全 name_typo 冲突走多数票护栏确定性归一 + 重验放行;"
            "False=任何跨段硬伤一律 HARD 抛(v1 行为)。"
        ),
    )
    character_backfill_enabled: bool | None = Field(
        default=None,
        description=(
            "大纲角色完整性回填开关(v2 W3.5-s4-step1 机制1);省略 → 不覆盖引擎默认"
            "(True=开)。True=plot 后/volume_plans 前补全指代悬空角色卡(fail-open);"
            "False=跳过回填(机制1 前行为)。"
        ),
    )
    core_state_hard_enabled: bool | None = Field(
        default=None,
        description=(
            "全书核心状态与主时间线 HARD 总开关；省略 → 不覆盖引擎默认(True)。"
            "关闭只影响后续新卡/新批准路径，既有 revision 保留可审计。"
        ),
    )

    def to_override_dict(self) -> dict[str, Any]:
        """序列化为 ``generation`` 覆盖段 dict(None 字段不落,对齐 to_yaml_dict 模式)。"""
        return self.model_dump(exclude_none=True)


class WorkspaceConfigUpdateIn(BaseModel):
    """``PUT /api/v1/workspaces/{workspace_id}/config`` body(P6-B-12,P6-B-13 扩 reviewer,P6-B-14 扩 retrieval,P6-B-15 扩 cost_guard)。

    字面对齐 ``workspaces/<id>/project.yaml`` 文件结构(顶层 ``writer_profiles``
    + ``writer_routing`` + ``reviewer`` + ``retrieval`` + ``cost_guard`` 五段)。
    本端点语义是 **整体替换工作区覆盖层** —— body 内的 dict 内容会以
    ``yaml.safe_dump`` 完整写入 ``project.yaml``,**不与原 project.yaml 做字段级
    diff 合并**。

    省略所有段都合法(等价于"清空这 5 段工作区覆盖,完全走引擎默认")。

    **v1.1 w3-s8 例外 —— generation 段**:``generation`` 段有专属端点
    ``PUT /workspaces/{id}/config/generation``(L1)做读-改-存,统一 PUT **不是其
    权威**。路由层走 ``ConfigService.save_workspace_config_overlay``:body 未显式
    提及 ``generation`` 时**保留**现有该段(避免 ConfigPage 只发 writer 两段时
    冲掉 L1 设的生成参数);显式 ``generation: null`` 仍清空;显式给值则替换。
    其余 5 段无专属端点,沿用整体替换(省略 = 清空)。

    与 ``WorkspaceConfigOut`` 的区别(P6-B-12 自决项 2)
    ==================================================
    ``Out`` 暴露**合并后**完整视图(引擎默认 ⊕ 工作区覆盖),前端直接 PUT
    ``Out`` 会把引擎默认整体灌进 project.yaml,污染工作区覆盖层语义。本 ``In``
    schema 强制前端自行拼装"工作区覆盖层"(P6-F-04 实现细节:前端在 ConfigPage
    维护两份 state — engineDefault + workspaceOverride,PUT 时只发后者)。
    """

    model_config = ConfigDict(extra="forbid")

    writer_profiles: dict[str, WriterProfileIn] | None = Field(
        default=None,
        description=(
            "工作区覆盖的 profile 字典(可新增 / 覆盖引擎默认 profile)。"
            "省略 → 不覆盖任何 profile;空 dict → 显式覆盖为空。"
            "**注意**:dict-level merge by key 语义下,工作区无法'删除'引擎"
            "默认 profile,只能'覆盖'。"
        ),
    )
    writer_routing: RoutingConfigIn | None = Field(
        default=None,
        description="工作区覆盖的 routing 段。省略 → 不覆盖。",
    )
    reviewer: ReviewerConfigIn | None = Field(
        default=None,
        description=(
            "工作区覆盖的 reviewer 阈值段(P6-B-13)。省略 → 不覆盖,走引擎默认。"
            "字段级合并 — 工作区只填部分字段时其余沿用引擎默认。"
        ),
    )
    retrieval: RetrievalConfigIn | None = Field(
        default=None,
        description=(
            "工作区覆盖的 retrieval 检索段(P6-B-14)。省略 → 不覆盖,走引擎默认。"
            "字段级合并 — 工作区只填部分字段时其余沿用引擎默认。"
        ),
    )
    cost_guard: CostGuardConfigIn | None = Field(
        default=None,
        description=(
            "工作区覆盖的 cost_guard 成本守卫段(P6-B-15)。省略 → 不覆盖,走引擎默认。"
            "字段级合并 — 工作区只填部分字段时其余沿用引擎默认。"
        ),
    )
    generation: GenerationConfigIn | None = Field(
        default=None,
        description=(
            "工作区覆盖的 generation 生成参数段(v1.1 L1 Q16)。省略 → 不覆盖,走引擎默认。"
            "字段级合并 — 工作区只填部分字段时其余沿用引擎默认。"
            "UI 主路径走专属端点 PUT /workspaces/{id}/config/generation(只改该段不冲掉"
            "其他覆盖);本统一 PUT 段供 API client 整体替换时一并 round-trip。"
        ),
    )

    def to_yaml_dict(self) -> dict[str, Any]:
        """转成 yaml 写入用 dict(只含 body 内显式给出的段,None 段不落 yaml)。

        Pydantic v2 ``model_dump(exclude_none=True)`` 会把 ``writer_profiles=None``
        排除掉,但子段(``writer_routing.default=None`` 等)的 None 也会被排除,
        与"省略 default 不覆盖"语义一致。
        """
        result: dict[str, Any] = {}
        if self.writer_profiles is not None:
            result["writer_profiles"] = {
                profile_id: profile.model_dump(exclude_none=False)
                for profile_id, profile in self.writer_profiles.items()
            }
        if self.writer_routing is not None:
            routing_dump = self.writer_routing.model_dump(exclude_none=True)
            result["writer_routing"] = routing_dump
        if self.reviewer is not None:
            reviewer_dump = self.reviewer.model_dump(exclude_none=True)
            result["reviewer"] = reviewer_dump
        if self.retrieval is not None:
            retrieval_dump = self.retrieval.model_dump(exclude_none=True)
            result["retrieval"] = retrieval_dump
        if self.cost_guard is not None:
            cost_guard_dump = self.cost_guard.model_dump(exclude_none=True)
            result["cost_guard"] = cost_guard_dump
        if self.generation is not None:
            generation_dump = self.generation.model_dump(exclude_none=True)
            result["generation"] = generation_dump
        return result


# =============================================================================
# API key 凭证管理 schema(P6-B-16,独立路由 /config/api-keys)
# =============================================================================


_KNOWN_API_KEY_PROVIDERS = (
    "claude",
    "dashscope",
    "deepseek",
    "openai_compatible",
)
"""当前支持 API key 写入的 provider 枚举。

与 ``ApiKeyService.KNOWN_PROVIDERS`` 字面对齐 — 修改时两处同步改。
**v1.0.x post-wave5 H1 起追加 ``dashscope``**(阿里云通义千问云端 API,
覆盖 qwen3.8-max 等);**v1.1 wave 2 H2 起追加 ``deepseek`` +
``openai_compatible`` 通配**(后者覆盖 Moonshot / 智谱 GLM / OpenAI 官方
等所有未单独列字段的兼容 provider)。ollama 不走 API key(本地 HTTP 端点),
不进本枚举。
"""


class ApiKeyIn(BaseModel):
    """``PUT /api/v1/config/api-keys`` body(P6-B-16)。

    单次 PUT 只写一个 provider 的 key;批量更新走多次 PUT(对齐 RESTful
    单资源语义 + 失败原子性 — 任一 provider 写入失败不影响其他)。

    ``api_key`` 字段是明文 ``str`` 而非 ``SecretStr`` —— Pydantic v2 ``SecretStr``
    在 JSON body 反序列化时会包成 ``SecretStr`` 实例但 ``model_dump_json``
    会输出 ``"**********"``,与 PUT body 语义"用户主动 PUT 明文"不符。
    日志保护改在 ``ApiKeyService`` 内部脱敏(``_mask`` 函数),不依赖
    Pydantic 字段类型。``min_length=8`` 避免误传空值或测试残留(真 ``sk-ant-``
    / ``sk-`` key 至少 32+ 字符,8 是宽松下限)。
    """

    model_config = ConfigDict(extra="forbid")

    provider: Literal[
        "claude",
        "dashscope",
        "deepseek",
        "openai_compatible",
    ] = Field(
        ...,
        description=(
            "API key provider 枚举;当前支持 claude / dashscope / deepseek"
            " / openai_compatible 通配(对齐 _KNOWN_API_KEY_PROVIDERS)。"
            "ollama 不走 API key(本地 HTTP 端点),不在本枚举。"
        ),
    )
    api_key: str = Field(
        ...,
        min_length=8,
        description=(
            "API key 明文。Claude 形如 ``sk-ant-`` 开头长字符串;"
            "dashscope 形如 ``sk-`` 开头 32 字符。"
            "服务端写入 ``.env`` 后立即原地刷新 ``Settings`` 单例,"
            "下次任何 LLM 调用即用新 key。日志侧由 ``ApiKeyService._mask`` 脱敏。"
        ),
    )


class ApiKeyStatusOut(BaseModel):
    """单 provider 的 API key 配置状态(GET 响应内一项)。

    ``configured=False`` + ``masked_value=None`` 表 ``Settings`` 字段为空字符串
    (未配置);``configured=True`` + ``masked_value="sk-ant-***"`` 表已配置
    + 脱敏展示前 8 字符。脱敏规则对齐 ``OBSERVABILITY §3.3`` 字面 "API key
    必须脱敏 ``***REDACTED***``" — 本端点用前缀回显 + ``***`` 后缀替代纯
    ``***REDACTED***`` 是为前端能视觉对账 "key 是否写对"(只回显前 8 字符
    安全:Anthropic key 前缀均为 ``sk-ant-`` + 1 字符版本号,无独立熵)。
    """

    model_config = ConfigDict(extra="forbid")

    provider: str = Field(
        ...,
        description="API key provider 名;对齐 _KNOWN_API_KEY_PROVIDERS 枚举",
    )
    configured: bool = Field(
        ...,
        description="是否已配置 — Settings 字段非空字符串即 True",
    )
    masked_value: str | None = Field(
        default=None,
        description=(
            "脱敏值,configured=False 时为 None;True 时为 ``<前 8 字符>***`` "
            "(示例 ``sk-ant-***``)。前端展示用,任何场景不应反向解析。"
        ),
    )


class ApiKeyListOut(BaseModel):
    """``GET /api/v1/config/api-keys`` 响应。

    返回所有 ``_KNOWN_API_KEY_PROVIDERS`` 的状态(已配置 / 未配置都列出),
    便于前端 ConfigPage 渲染完整列表。
    """

    model_config = ConfigDict(extra="forbid")

    api_keys: list[ApiKeyStatusOut] = Field(
        ...,
        description="API key 状态列表;每 provider 一项,已配置 / 未配置都列出",
    )


# =============================================================================
# 可路由 agent 清单 schema(v1.1 wave 2 H3,独立路由 /config/routable-agents)
# =============================================================================


class RoutableAgentOut(BaseModel):
    """单个可路由 agent 的元信息(GET /config/routable-agents 响应单项)。

    供前端 H4/H5 Settings 页面画 agent 切换器 dropdown 用 — 每个 agent 一行,
    展示 ``display_name`` + ``description`` + 当前 profile (``current_profile``),
    用户在 UI 上改 profile 后 PUT 到 ``/workspaces/{id}/config`` 的 ``agent_routing``
    覆盖层。

    ``llm_driven=True`` 表该 agent 真调 LLM (可路由切换 provider / model);
    纯规则 validator(``style_drift_validator`` / ``cross_chapter_voice_validator``)
    不在本端点返回 — 它们不调 LLM,UI 切换无意义。

    本 schema 不暴露 ``router=None`` legacy 字段或 ``ESCALATION_*_BOUND``
    阈值等内部行为参数 — 这些走 ``ReviewerConfig`` / 各 agent 自己的配置段
    (与 routing 解耦)。
    """

    model_config = ConfigDict(extra="forbid")

    agent_name: str = Field(
        ...,
        description=(
            "Agent 名 (对齐 ``AGENT_NAME`` Literal + ``config.yaml`` "
            "``agent_routing.<agent_name>`` 字段)"
        ),
    )
    display_name: str = Field(
        ...,
        description="中文显示名,前端 UI label 用",
    )
    description: str = Field(
        ...,
        description="中文一句话职责说明,前端 UI tooltip / 帮助文案用",
    )
    llm_driven: bool = Field(
        ...,
        description=(
            "是否真调 LLM (本端点只返 True 的 agent;预留字段供前端区分,"
            "未来加纯规则 validator 列表时复用同 schema)"
        ),
    )
    is_llm_judge: bool = Field(
        ...,
        description=(
            "是否 LLM-as-judge 治理 agent(对齐 ``LLM_AS_JUDGE_AGENTS`` frozenset,"
            "W3.5-s5 引入)。True 表该 agent 绑定的 profile **必须 thinking-on**"
            "(wave 1 G3/G20 hard guard);前端据此**前置拦截**用户把 judge 绑到 "
            "thinking-off profile(``RoutableProfileMeta.thinking_on=False``),"
            "给友好提示而非直撞后端 422 ``CONFIG_YAML_INVALID``。"
        ),
    )
    current_profile: str = Field(
        ...,
        description=(
            "当前生效的 profile_id (引擎默认 + 可选 workspace_id 工作区覆盖"
            "合并后的字段值);若 agent_routing 未配置则等于 ``default_profile``"
        ),
    )
    default_profile: str = Field(
        ...,
        description=(
            "兜底 profile_id (引擎默认 ``writer_routing.default``);"
            "工作区可在 UI 上覆盖 ``agent_routing.<agent_name>`` 走自定义 profile,"
            "未覆盖时 ``current_profile`` 等于本字段"
        ),
    )


class RoutableProfileMeta(BaseModel):
    """单个 profile 的元信息(``GET /config/routable-agents`` 响应,W3.5-s5 引入)。

    供前端设置页 agent → profile 切换器做两件事(plan §4.1):

    ① **judge 硬护栏 UI 拦截**:``thinking_on=False`` 的 profile 不能绑到
       LLM-as-judge agent(``RoutableAgentOut.is_llm_judge=True``);前端前置
       拦截 + 友好提示,避免直撞后端 ``_assert_llm_judge_thinking_on`` 的 422。
    ② **校准状态显示 + 未校准警告**:``calibrated`` / ``calibrated_at`` 反映该
       profile 的**模型**(非 profile id —— standard/thinking 共享同模型校准)是否
       有 stored 校准(W3.5-s2 ``model_calibration`` 段,keyed by 模型名);writer
       绑到 ``calibrated=False`` 模型时前端明确提示正式大纲生成会在 Provider
       请求前被阻断，必须先完成该模型校准。

    **校准为引擎级**(读引擎默认 ``config.yaml`` 的 ``model_calibration`` 段);
    与生成期 ``outline_service._resolve_length_calibration`` 同一真相源
    (按 ``router.select_profile("writer").model`` 解析),保证 UI 显示与实际
    拆章行为一致。
    """

    model_config = ConfigDict(extra="forbid")

    model: str = Field(
        ...,
        description=(
            "profile 底层模型名(如 ``deepseek-v4-pro`` / ``qwen3.8-max``);"
            "校准按模型名 key(非 profile id)"
        ),
    )
    thinking_on: bool = Field(
        ...,
        description=(
            "是否 thinking-on(``is_thinking_on_profile`` 判定:mock/claude 恒 True,"
            "ollama 看 think,openai_compatible 看 enable_thinking)。LLM-as-judge "
            "agent 必须绑 thinking-on profile,否则后端 hard guard 422。"
        ),
    )
    calibrated: bool = Field(
        ...,
        description=(
            "该 profile 的**模型**是否有 stored 校准(引擎 ``model_calibration`` 段"
            "含此模型名)。False = 未校准 → 正式大纲生成会在任何 Provider 请求前"
            "阻断，禁止静默跳过长度感知拆章。"
        ),
    )
    calibrated_at: str | None = Field(
        None,
        description=(
            "校准产出日期(``model_calibration[model].calibrated_at``,ISO 8601);"
            "``calibrated=False`` 时为 None。前端校准徽章展示用。"
        ),
    )


class RoutableAgentsListOut(BaseModel):
    """``GET /api/v1/config/routable-agents`` 响应。

    返回全系统可路由的 LLM-driven agent 完整清单(对齐 ``AGENT_NAME`` Literal,
    含 ``reviewer_escalation`` 设计保留项)。

    可选 query 参数 ``workspace_id`` —— 给定时 ``current_profile`` 字段反映该
    工作区合并后的字段值(走 ``ConfigService.load_workspace_config`` 路径);
    省略时 ``current_profile`` 走引擎默认 ``config.yaml``,等同于"全局默认视图"。
    """

    model_config = ConfigDict(extra="forbid")

    agents: list[RoutableAgentOut] = Field(
        ...,
        description="可路由 agent 清单;每项含 agent_name / display_name / description / current_profile / default_profile / is_llm_judge",
    )
    available_profiles: list[str] = Field(
        ...,
        description=(
            "当前 ``WriterProfilesConfig`` 已声明的 profile_id 列表 "
            "(前端切换器 dropdown 选项来源);UI 不应允许用户填本列表外的值"
        ),
    )
    profile_meta: dict[str, RoutableProfileMeta] = Field(
        ...,
        description=(
            "profile_id → 元信息(model / thinking_on / calibrated / calibrated_at,"
            "W3.5-s5 引入)。key 集合 == ``available_profiles``。前端用于:judge 硬"
            "护栏 UI 拦截(``thinking_on``)+ 校准状态显示/未校准警告"
            "(``calibrated`` / ``calibrated_at``)。"
        ),
    )


class EngineAgentRoutingUpdateIn(BaseModel):
    """``PUT /api/v1/config/agent-routing`` 请求体(v1.1 wave 3 H5)。

    写引擎默认 ``config.yaml`` 的 ``writer_routing.agent_routing`` 段(全局层,
    所有工作区共用)。与工作区覆盖 ``PUT /workspaces/{id}/config`` 区分:本端点
    改的是引擎默认,影响所有工作区。

    **整段替换语义**:``agent_routing`` 即新的完整映射 —— 列出的 agent 用对应
    profile,未列出的 agent 从 config.yaml 删除其覆盖(回退到 ``default`` 兜底);
    空 dict → 清空所有 agent 覆盖。

    校验(后端 ``ConfigService.save_engine_agent_routing``,失败 422):
    ① 引用的 profile_id 必须已在 ``writer_profiles`` 声明;
    ② LLM-as-judge agent 绑定的 profile 必须 thinking-on(wave 1 G3/G20 hard guard)。
    """

    model_config = ConfigDict(extra="forbid")

    agent_routing: dict[str, str] = Field(
        ...,
        description=(
            "新的完整 agent → profile_id 映射(整段替换)。key 对齐 ``AGENT_NAME`` "
            "Literal;value 必须是 ``available_profiles`` 内的 profile_id。"
            "空 dict 表清空所有 agent 覆盖,全部走 ``writer_routing.default``。"
        ),
    )
