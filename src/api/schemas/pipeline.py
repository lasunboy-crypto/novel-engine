"""流水线路由请求 / 响应 schema(P6-B-02)。

对应 ``POST /api/v1/pipeline:start`` / ``:pause`` / ``:resume`` / ``:stop``
+ ``GET /api/v1/pipeline/status`` 5 端点(对齐 API_CONTRACT §4.5 + plan §"P6
端点表汇总" 5-9)。

scope 4 档语义(对齐 plan §"P6-B-02" + 自决项 6):

- ``single`` = 单章(只跑 ``start_chapter_id``)
- ``range`` = 区间 ``[start_chapter_id, end_chapter_id]`` 包含两端
- ``volume`` = 整卷(对应 ``volume_id`` 下所有章节)
- ``until`` = 从 ``start_chapter_id`` 写到 ``end_chapter_id`` 为止(含);
  ``end_chapter_id`` 为空时默认到 ``start_chapter_id`` 所在卷末

字段越界 / 缺失 / 不合法 → ``PIPELINE_INVALID_SCOPE 422``(在 service 层做
跨字段校验;Pydantic 层只做类型与基础长度校验,避免 schema 与 service 双份
校验漂移)。
"""

from __future__ import annotations

from typing import Literal

from pydantic import BaseModel, ConfigDict, Field


# ---- 字面类型 ---------------------------------------------------------------


PipelineScope = Literal["single", "range", "volume", "until"]
"""scope 4 档(对齐 plan §"P6-B-02" + 自决项 6)。"""


PipelineState = Literal[
    "idle", "running", "paused", "interrupted", "stopped", "error"
]
"""pipeline 状态 6 档(v2 w1-s2 扩 ``interrupted``,对齐
``PIPELINE_STATE_VALUES`` + Alembic 0024)。

- ``idle``:无 active run(系统初始 / 上一轮 completed 后)
- ``running``:正在跑章节序列
- ``paused``:被 ``:pause`` 端点暂停 / 撞 invalidated gate 自动暂停 /
  孤儿章·rejected 卡按 stop 策略自动暂停(v2 w1-s1)
- ``interrupted``:进程重启时 run 中断且数据完整可恢复(v2 w1-s2)——
  **可 ``:resume`` 断点续跑**(区别于 ``error`` 死路);也挡新 ``:start``
  (须先 resume 续跑或 stop 放弃)
- ``stopped``:被 ``:stop`` 端点显式终止 / 全章节正常完成
- ``error``:运行时不可恢复错误 / 重启扫描发现行数据不完整(兜底)
"""


PipelineFailurePolicy = Literal["stop", "skip", "retry"]
"""失败策略 3 档(v2 w1-s1,对齐 v2_plan §4 w1-s1 + ``PIPELINE_FAILURE_POLICY_VALUES``)。

章级失败 = 孤儿章升 human_intervention / 写章异常 / 卡生成失败 / rejected 卡:

- ``stop``(默认,推荐):孤儿章·rejected 卡 → run 暂停(pause_reason=
  ``chapter_escalated`` / ``card_rejected``,人工解决后可 resume);
  写章异常·卡生成失败 → state=error。孤儿章需人工,跳过会让衔接接计划。
- ``skip``:失败章记入 ``skipped_chapter_ids`` 继续写下一章。
- ``retry``:同章重试 1 次,仍失败 → 按 ``stop`` 处置。
"""


# ---- 请求体 ------------------------------------------------------------------


class PipelineStartIn(BaseModel):
    """``POST /api/v1/pipeline:start`` 请求体(对齐 API_CONTRACT §4.5 字面)。

    跨字段约束在 service 层 ``_validate_scope_params`` 校验(scope 与 4 字段
    的存在性映射):

    - ``single``:``start_chapter_id`` 必填
    - ``range``:``start_chapter_id`` + ``end_chapter_id`` 必填
    - ``volume``:``volume_id`` 必填(``start_chapter_id`` / ``end_chapter_id``
      忽略)
    - ``until``:``start_chapter_id`` 必填,``end_chapter_id`` 可选(空则到
      所在卷末,自决项 6)

    违反 → ``PIPELINE_INVALID_SCOPE 422``。
    """

    model_config = ConfigDict(extra="forbid")

    scope: PipelineScope = Field(..., description="scope 4 档,见 PipelineScope")
    start_chapter_id: str | None = Field(
        default=None,
        min_length=1,
        description="起始章节 ID(single / range / until 必填,volume 忽略)",
    )
    end_chapter_id: str | None = Field(
        default=None,
        min_length=1,
        description="结束章节 ID(range 必填;until 可选;single / volume 忽略)",
    )
    volume_id: str | None = Field(
        default=None,
        min_length=1,
        description="卷 ID(volume 必填;其他 scope 忽略)",
    )
    profile_override: str | None = Field(
        default=None,
        min_length=1,
        description=(
            "本次流水线强制使用的 writer profile_id;``None`` 时走默认 routing。"
            "单次生效不持久化(对齐 P3-B-11 ``chapter:write`` profile_override 模式)。"
        ),
    )
    failure_policy: PipelineFailurePolicy = Field(
        default="stop",
        description=(
            "章级失败策略(v2 w1-s1):``stop``(默认,孤儿章暂停可人工解决后"
            "续跑)/ ``skip``(失败章记入 skipped_chapter_ids 继续)/ "
            "``retry``(同章重试 1 次后按 stop)。见 PipelineFailurePolicy。"
        ),
    )
    provider_call_cap: int = Field(
        default=1000,
        ge=1,
        description=(
            "本次 run 的真实 provider 物理调用 hard cap；计数跨 pause/resume"
            "与进程重启累计，达到后下一次网络调用前停止。"
        ),
    )


class PipelinePauseIn(BaseModel):
    """``POST /api/v1/pipeline:pause`` 请求体(空 schema,保留扩展位)。"""

    model_config = ConfigDict(extra="forbid")


class PipelineResumeIn(BaseModel):
    """``POST /api/v1/pipeline:resume`` 请求体(空 schema)。"""

    model_config = ConfigDict(extra="forbid")


class PipelineStopIn(BaseModel):
    """``POST /api/v1/pipeline:stop`` 请求体(空 schema)。"""

    model_config = ConfigDict(extra="forbid")


# ---- 响应体 ------------------------------------------------------------------


class PipelineStartOut(BaseModel):
    """``:start`` 端点响应 data 段(返 202 + ``{run_id}``)。"""

    model_config = ConfigDict(extra="forbid")

    run_id: str = Field(..., description="本次 run 的唯一 ID(``run_<16hex>``)")
    workspace_id: str
    state: PipelineState = Field(..., description="启动后立即为 ``running``")
    scope: PipelineScope
    chapter_ids: list[str] = Field(
        ...,
        description="本次 run 解析后的章节序列(按写作顺序);scope=single 长度 1",
    )
    failure_policy: PipelineFailurePolicy = Field(
        ..., description="本次 run 生效的章级失败策略(v2 w1-s1)"
    )
    provider_call_cap: int


class ConfiguredCostEstimateOut(BaseModel):
    """按原币种相加的本地配置估算；不同币种永不合并。"""

    model_config = ConfigDict(extra="forbid")

    currency: str = Field(..., pattern=r"^[A-Z]{3}$")
    amount: float = Field(..., ge=0.0)


class ActualBilledCostOut(BaseModel):
    """Provider 实际账单占位；未导入时不由 usage 反推。"""

    model_config = ConfigDict(extra="forbid")

    status: Literal["not_recorded"] = "not_recorded"
    amount: None = None
    currency: None = None


class AttemptUsageCostOut(BaseModel):
    """物理 Provider attempt 的权威 usage/配置估算投影。"""

    model_config = ConfigDict(extra="forbid")

    usage_known_attempts: int = Field(..., ge=0)
    usage_unknown_attempts: int = Field(..., ge=0)
    legacy_unclassified_attempts: int = Field(..., ge=0)
    input_tokens_known: int = Field(..., ge=0)
    output_tokens_known: int = Field(..., ge=0)
    provider_request_id_count: int = Field(..., ge=0)
    response_status_counts: dict[str, int]
    configured_cost_basis_counts: dict[str, int]
    configured_estimates: list[ConfiguredCostEstimateOut]
    actual_billed: ActualBilledCostOut


class PipelineStatusOut(BaseModel):
    """``GET /api/v1/pipeline/status`` 响应 data 段。

    无 active run 时返 ``state=idle`` + 其余字段空(``run_id=None`` /
    ``chapter_ids=[]`` / ``progress=0.0`` 等)。
    """

    model_config = ConfigDict(extra="forbid")

    run_id: str | None = Field(
        ..., description="active run 的 run_id;无 active run 时 None"
    )
    workspace_id: str
    state: PipelineState
    scope: PipelineScope | None = Field(
        ..., description="当前 run 的 scope;无 active run 时 None"
    )
    chapter_ids: list[str] = Field(
        ...,
        description="本次 run 解析后的章节序列(已排序);无 active run 时 []",
    )
    current_chapter: str | None = Field(
        ..., description="当前正在写的章节 ID;暂停 / 完成时为最后处理的章节"
    )
    progress: float = Field(
        ...,
        ge=0.0,
        le=1.0,
        description="完成进度 0.0-1.0(completed_chapter_ids 数 / chapter_ids 数)",
    )
    completed_chapter_ids: list[str] = Field(
        ..., description="已成功完成的章节 ID 列表(按完成顺序)"
    )
    profile_override: str | None = Field(
        ..., description="本次 run 的 profile_override;空表示走默认"
    )
    pause_reason: str | None = Field(
        ...,
        description=(
            "暂停原因:``user_request`` / ``chapter_invalidated`` / "
            "``chapter_escalated``(孤儿章,v2 w1-s1)/ ``card_rejected``"
            "(rejected 卡,v2 w1-s1)/ ``cost_guard_exceeded``(Run 级"
            "成本护栏,v2 w1-s3;次日成本日清零 / 调高限额后可 :resume)"
            "/ ``completed`` / None"
        ),
    )
    error_details: dict | None = Field(
        ...,
        description=(
            "error / interrupted 状态的详情 dict。error:``error_type`` / "
            "``message`` / ``chapter_id``;interrupted(v2 w1-s2/s3)按 "
            "``reason`` 区分:``engine_restarted_mid_run``(重启改判)/ "
            "``chapter_watchdog_timeout``(章级看门狗超时,v2 w1-s3,含 "
            "``chapter_id`` / ``elapsed_seconds`` / ``timeout_seconds``)/ "
            "``pipeline_loop_conflict``(旧循环未退出占用,v2 w1-s3)"
        ),
    )
    failure_policy: PipelineFailurePolicy = Field(
        ..., description="本次 run 生效的章级失败策略(v2 w1-s1);idle 时默认 stop"
    )
    skipped_chapter_ids: list[str] = Field(
        ...,
        description=(
            "skip 策略下被跳过的失败章 ID 列表(v2 w1-s1;选章时 "
            "completed ∪ skipped 都不再选)"
        ),
    )
    provider_call_cap: int = Field(..., ge=1)
    provider_call_count: int = Field(..., ge=0)
    transport_retry_count: int = Field(..., ge=0)
    pipeline_attempt_count: int = Field(..., ge=0)
    upstream_generation_attempt_count: int = Field(..., ge=0)
    attempt_usage_cost: AttemptUsageCostOut


class PipelinePauseOut(BaseModel):
    """``:pause`` / ``:resume`` / ``:stop`` 端点响应 data 段(共用结构)。

    与 ``PipelineStatusOut`` 字段一致(返回操作后最新 snapshot,前端不必再 GET
    status)。
    """

    model_config = ConfigDict(extra="forbid")

    run_id: str | None
    workspace_id: str
    state: PipelineState
    scope: PipelineScope | None
    chapter_ids: list[str]
    current_chapter: str | None
    progress: float = Field(..., ge=0.0, le=1.0)
    completed_chapter_ids: list[str]
    profile_override: str | None
    pause_reason: str | None
    error_details: dict | None
    failure_policy: PipelineFailurePolicy
    skipped_chapter_ids: list[str]
    provider_call_cap: int
    provider_call_count: int
    transport_retry_count: int
    pipeline_attempt_count: int
    upstream_generation_attempt_count: int
    attempt_usage_cost: AttemptUsageCostOut


PipelineResumeOut = PipelinePauseOut
"""``:resume`` 端点响应 data 段(与 PipelinePauseOut 完全同形)。"""


PipelineStopOut = PipelinePauseOut
"""``:stop`` 端点响应 data 段(与 PipelinePauseOut 完全同形)。"""


__all__ = [
    "ActualBilledCostOut",
    "AttemptUsageCostOut",
    "ConfiguredCostEstimateOut",
    "PipelineFailurePolicy",
    "PipelinePauseIn",
    "PipelinePauseOut",
    "PipelineResumeIn",
    "PipelineResumeOut",
    "PipelineScope",
    "PipelineStartIn",
    "PipelineStartOut",
    "PipelineState",
    "PipelineStatusOut",
    "PipelineStopIn",
    "PipelineStopOut",
]
