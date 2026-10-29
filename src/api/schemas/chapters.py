"""章节路由请求/响应 schema(P1-B-19)。

涵盖 ``:write`` / ``:rewrite`` / ``:revise`` 三个动作端点的请求体与 result 段,
以及 ``GET /chapters/{id}/status`` 的响应体。

result 段设计原则(决策 6 拍板)
================================

``:write`` / ``:rewrite`` 跑完后 ``OrchestratorResult`` 体量大(每个版本含完整
正文,几千字 × N 版本),直接 dump 到 HTTP 响应里既浪费带宽又难序列化(Path
对象、datetime 等)。统一只回**摘要**:

- ``WriteResultSummary``:final_state / final_version_id / final_path /
  version_count / revise_count / rewrite_count / escalation_reason +
  ``review_summary``(每条带 ``revise_targets``,见 ``schemas/reviews.py``)
- 正文走 ``GET /chapters/{id}/versions/{version_id}`` 单独取
- ``:revise`` 的 result 段是 ``ChapterVersionMeta``(见 ``schemas/versions.py``),
  同样不含正文
"""

from __future__ import annotations

from typing import Any, Literal

from pydantic import BaseModel, ConfigDict, Field

from src.api.schemas.reviews import ReviewIssueIn, ReviewSummaryEntry


# ---- 请求体 ------------------------------------------------------------------


class ChapterWriteIn(BaseModel):
    """``POST /chapters/{chapter_id}:write`` 请求体(P3-B-11 引入,P3-F-03 类 C 顺延)。

    P1-P2 阶段 ``:write`` 端点空 body;P3-B-11 起 body 仅可选携带
    ``profile_override`` 字段(WriterProfileSelector 单次写作覆盖)。**全部
    字段都有默认值**——既有客户端继续发空 body 仍合法,后端按
    ``ChapterWriteIn()`` 默认实例处理(对齐 P1-F-14 ``useWriteChapterMutation``
    透传 ``undefined`` body 的行为)。
    """

    model_config = ConfigDict(extra="forbid")

    profile_override: str | None = Field(
        default=None,
        description=(
            "本次写作强制使用的 profile_id;``None`` 时走 RoutingConfig 规则 / "
            "agent_routing 默认。**单次生效不持久化**(对齐 P3-plan §F-2 字面)。"
            "非法 profile_id → 422 ``CHAPTER_INVALID_PROFILE``。"
        ),
    )


class ChapterRewriteIn(BaseModel):
    """``POST /chapters/{chapter_id}:rewrite`` 请求体。

    ``prior_rewrite_count`` 由调用方提供(P1 ChapterService 没 chapters 表可查
    "已用了几次重写"),对接 ``ChapterCard.max_rewrites`` 名额预算。**P2 弃用**:
    届时 service 自动从 chapters 表读,本字段可移除(v1 周期内只 deprecate 不移除,
    见 API_CONTRACT §10)。

    P3-B-11 加可选 ``profile_override`` 字段(P3-F-03 类 C 顺延,语义同
    ``ChapterWriteIn``)。
    """

    model_config = ConfigDict(extra="forbid")

    prior_rewrite_count: int = Field(
        default=0,
        ge=0,
        description=(
            "已用的重写次数,用于对接 ChapterCard.max_rewrites 名额预算。"
            "P2 service 自动从 chapters 表读时本字段会 deprecate。"
        ),
    )
    profile_override: str | None = Field(
        default=None,
        description=(
            "本次重写强制使用的 profile_id;``None`` 时走 RoutingConfig 规则。"
            "**单次生效不持久化**。非法 profile_id → 422 ``CHAPTER_INVALID_PROFILE``。"
        ),
    )


class ChapterReviseIn(BaseModel):
    """``POST /chapters/{chapter_id}:revise`` 请求体。

    ``issues`` 由调用方从最近一次 ``ReviewResult.revise_targets`` 取(P1 阶段
    review 没持久化,只能从最近一次 ``:write`` / ``:rewrite`` 响应的
    ``review_summary[].revise_targets`` 拿;P2 可加 ``GET /chapters/{id}/reviews``)。

    ``source_version_id`` 留空时取最新版本号(``next_version_id - 1``);显式指定
    可用于"基于历史版本拉新分支"的非主路径场景。
    """

    model_config = ConfigDict(extra="forbid")

    issues: list[ReviewIssueIn] = Field(
        ...,
        description=(
            "要修订的问题列表;通常来自最近一次 review_summary[].revise_targets。"
            "空列表时 Reviser 配合 prompt 兜底原样回原文(不推荐)。"
        ),
    )
    source_version_id: int | None = Field(
        default=None,
        ge=1,
        description="修订源版本号;留空取最新版本",
    )


class ChapterReReviewIn(BaseModel):
    """``POST /chapters/{chapter_id}:re-review`` 请求体(v2 W3.5 UI 缺口3)。

    对人工改稿(``human_intervention`` 章)的最新版本重跑写后审校。空 body 合法
    (既有前端发 undefined 仍按默认实例处理)。可选 ``profile_override`` 语义同
    ``ChapterWriteIn``。
    """

    model_config = ConfigDict(extra="forbid")

    profile_override: str | None = Field(
        default=None,
        description=(
            "本次重审强制使用的 profile_id;``None`` 时走 RoutingConfig 规则。"
            "**单次生效不持久化**。非法 profile_id → 422 ``CHAPTER_INVALID_PROFILE``。"
        ),
    )


# ---- 响应 data 段 ------------------------------------------------------------


class WriteResultSummary(BaseModel):
    """``:write`` / ``:rewrite`` 跑完的 result 段(决策 6 拍板:只放摘要)。

    所有字段来源都是 ``OrchestratorResult``,不含正文(正文走
    ``GET /versions/{id}`` 单独取)。
    """

    model_config = ConfigDict(extra="forbid")

    final_state: Literal[
        "drafting",
        "reviewing",
        "rewriting",
        "revising",
        "approved",
        "published",
        "human_intervention",
    ] = Field(..., description="最终状态机节点")
    final_version_id: int | None = Field(
        ..., description="标定为 final 的版本号;仅 final_state=approved 时非 None"
    )
    final_path: str | None = Field(
        ..., description="final.md 绝对路径;仅 final_state=approved 时非 None"
    )
    version_count: int = Field(..., ge=0, description="本次跑产出的版本数")
    revise_count: int = Field(..., ge=0, description="累计修订次数(含 initial)")
    rewrite_count: int = Field(..., ge=0, description="累计重写次数(含 initial)")
    escalation_reason: str | None = Field(
        ...,
        description=(
            "升人工原因;仅 final_state=human_intervention 时非 None。"
            "P1 取值:'rewrite_budget_exhausted' / 'reviewer_parse_failed_after_retry'。"
        ),
    )
    review_summary: list[ReviewSummaryEntry] = Field(
        ..., description="本次跑每次审稿的摘要(顺序与 reviews 一致)"
    )


# ---- 状态查询响应 ------------------------------------------------------------


_ChapterStatus8 = Literal[
    "drafting",
    "reviewing",
    "rewriting",
    "revising",
    "approved",
    "published",
    "human_intervention",
    "invalidated",
]
"""章节 status 8 档(P2-B-17 起 7 档,P6-B-01 扩 ``invalidated`` 第 8 档)。

对齐 PROJECT_SPEC §3.5 ``Chapter.status`` 与 ``ChapterState`` StrEnum。响应
schema 与 service 层 ``ChapterStateValue`` 保持单一真相源——这里用模块私有
alias,避免循环 import。``invalidated`` 由 ``ChapterRewindService.rewind``
强制写入(``chapter_id > target_id`` 的后续章节),``unblock`` 端点翻回
``drafting``。"""


_ChapterStatus7 = _ChapterStatus8
"""向后兼容旧名(P6-B-01 起即 8 档,旧名仍指向同一 Literal)。"""


class ChapterStatusOut(BaseModel):
    """``GET /chapters/{chapter_id}/status`` 响应 data 段。

    字段一一对应 ``services.chapter_service.ChapterStatusInfo``;P2-B-17 起
    ``status`` 升 7 档(``chapters`` 表行存在则读库,否则文件系统 fallback,
    详见 ``ChapterStatusInfo`` docstring)。
    """

    model_config = ConfigDict(extra="forbid")

    chapter_id: str
    status: _ChapterStatus7
    latest_version_id: int | None = Field(
        ..., description="最新版本号;无版本时为 None"
    )
    has_final: bool
    version_count: int = Field(..., ge=0)
    escalation_details: dict[str, Any] | None = Field(
        None,
        description=(
            "升 human_intervention 时的结构化详情(v1.1 acceptance 修复2 / "
            "Fix D 写前 must 可行性预检):{reason, checked_at_state, findings: "
            "[{must_text, confidence, evidence, suggestion}]};常态 None"
        ),
    )


# ---- 手动编辑正文(v1.1 acceptance 修复2,孤儿章人工解决流程) ----------------


class ChapterContentUpdateIn(BaseModel):
    """``PUT /chapters/{chapter_id}/content`` 请求体(v1.1 acceptance 修复2)。

    孤儿章人工解决流程:维护人在 ChapterEditorPage 编辑正文后保存。仅
    ``human_intervention`` 状态章节可调(防误改 approved 章),其余状态 409
    ``CHAPTER_INVALID_STATE``。保存 = 落新版本(版本号自增,旧版保留)+ 切
    final 指针;批准走既有 ``:force-pass``。
    """

    model_config = ConfigDict(extra="forbid")

    content: str = Field(
        ...,
        min_length=1,
        description="人工编辑后的完整章节正文(Markdown 纯文本)",
    )


class ChapterContentUpdateOut(BaseModel):
    """``PUT /chapters/{chapter_id}/content`` 响应 data 段。"""

    model_config = ConfigDict(extra="forbid")

    chapter_id: str
    version_id: int = Field(..., description="新落盘的版本号")
    word_count: int = Field(..., ge=0, description="新正文字数(len 口径)")


# ---- 列表查询响应(P1-B-25) -------------------------------------------------


class ChapterListItem(BaseModel):
    """``GET /chapters`` 列表项响应 data 段单条(P1-B-25 引入,P2-B-17 升 7 档)。

    字段并集对齐 P1-F-10(工作区主页看板:总章数 / 已完成 / 进行中 / 最近修改)
    + P1-F-11(章节列表行:chapter_id / title / state / latest_version)。

    字段名按 ``ChapterCard`` 字面对齐:``title`` / ``chapter_number``——P1-F-10
    任务模板字面 ``display_name`` / ``order_index`` 在规格源 ``ChapterCard`` 实际
    是 ``title`` / ``chapter_number``,本任务按规格源走(类 A 冲突)。

    一一对应 ``services.chapter_service.ChapterListEntry``,路由层 ``model_validate``
    转换,与 ``ChapterStatusInfo`` / ``ChapterStatusOut`` 套路一致。``status``
    P2-B-17 升 7 档(对齐 PROJECT_SPEC ``Chapter.status``);前端 P2-F-08
    据此合并 ``LiveChapterStateBadge`` / ``ChapterStateBadge``。
    """

    model_config = ConfigDict(extra="forbid")

    chapter_id: str
    title: str = Field(..., description="章节标题(取自 ChapterCard.title)")
    chapter_number: int = Field(
        ..., ge=1, description="卷内章序号(取自 ChapterCard.chapter_number)"
    )
    volume_id: str
    status: _ChapterStatus7
    latest_version_id: int | None = Field(
        ..., description="最新版本号;无版本时为 None"
    )
    has_final: bool
    version_count: int = Field(..., ge=0)
    last_modified_at: str = Field(
        ..., description="章节卡文件 mtime,ISO 8601 UTC"
    )


# ---- 章节人工介入(P6-B-01)-------------------------------------------------


class ChapterRewindIn(BaseModel):
    """``POST /chapters/{chapter_id}:rewind`` 请求体(P6-B-01,空 body)。

    空 schema(``extra="forbid"`` 仍生效):rewind 触发条件全由 path 参数
    与 ``X-Workspace-Id`` header 决定,无需 body 字段;保留 schema 类型
    位以备 P6+ 扩 ``reason`` / ``preserve_versions: bool`` 等审计字段。
    """

    model_config = ConfigDict(extra="forbid")


class ChapterForcePassIn(BaseModel):
    """``POST /chapters/{chapter_id}:force-pass`` 请求体(P6-B-01)。

    ``reason`` 强制非空(``min_length=1``,自决项 1 对齐 PROJECT_SPEC §5.4
    字面"破坏性操作要 user 显式留痕"):force-pass 跳过 Reviewer 是显著
    决策点,留理由便于事后追溯。前端 ChapterEditorPage / RunControlPage 必须
    弹 textarea 收集理由。
    """

    model_config = ConfigDict(extra="forbid")

    reason: str = Field(
        ...,
        min_length=1,
        description=(
            "force-pass 触发理由(必填,前端 textarea 收集;持久化到 "
            "``chapters.force_pass_reason`` Text 列)"
        ),
    )


class ChapterUnblockIn(BaseModel):
    """``POST /chapters/{chapter_id}:unblock`` 请求体(P6-B-01,空 body)。"""

    model_config = ConfigDict(extra="forbid")


class WorkspaceUnblockAllIn(BaseModel):
    """``POST /workspaces/{workspace_id}:unblock-all`` 请求体(P6-B-01,空 body)。"""

    model_config = ConfigDict(extra="forbid")


class WorkspaceResetStaleIn(BaseModel):
    """``POST /workspaces/{workspace_id}:reset-stale`` 请求体(v2 w1-s2,空 body)。"""

    model_config = ConfigDict(extra="forbid")


class ChapterRewindOut(BaseModel):
    """``rewind`` 端点响应 data 段。"""

    model_config = ConfigDict(extra="forbid")

    chapter_id: str = Field(..., description="被 rewind 的目标章节 ID")
    from_status: _ChapterStatus8 = Field(..., description="rewind 前的 status")
    to_status: Literal["drafting"] = Field(
        ..., description="rewind 后固定 drafting(回到未写状态)"
    )
    invalidated_chapter_ids: list[str] = Field(
        ...,
        description="被翻成 invalidated 的后续章节 chapter_id 列表(已排序)",
    )
    vector_revoked_count: int = Field(
        ..., ge=0, description="Chroma 软删除条目数"
    )
    extracted_revoked_count: int = Field(
        ...,
        ge=0,
        description="SQLite extracted_* 三表合计 revoked_at 翻 now 行数",
    )


class ChapterForcePassOut(BaseModel):
    """``force-pass`` 端点响应 data 段。"""

    model_config = ConfigDict(extra="forbid")

    chapter_id: str
    from_status: _ChapterStatus8
    to_status: Literal["approved"] = Field(..., description="固定 approved")
    force_pass_at: str = Field(..., description="ISO 8601 UTC")
    force_pass_reason: str


class ChapterUnblockOut(BaseModel):
    """``unblock`` 端点响应 data 段(单章)。"""

    model_config = ConfigDict(extra="forbid")

    chapter_id: str
    from_status: Literal["invalidated"] = Field(..., description="固定 invalidated")
    to_status: Literal["drafting"] = Field(..., description="固定 drafting")


class WorkspaceUnblockAllOut(BaseModel):
    """``unblock-all`` 端点响应 data 段(workspace 批量)。"""

    model_config = ConfigDict(extra="forbid")

    workspace_id: str
    unblocked_chapter_ids: list[str] = Field(
        ..., description="本次被 unblock 的章节 chapter_id 列表(已排序)"
    )
    unblocked_count: int = Field(..., ge=0)


class WorkspaceResetStaleOut(BaseModel):
    """``reset-stale`` 端点响应 data 段(workspace 批量,v2 w1-s2 中断章自愈)。"""

    model_config = ConfigDict(extra="forbid")

    workspace_id: str
    reset_chapter_ids: list[str] = Field(
        ...,
        description=(
            "本次被重置回 drafting 的章节 chapter_id 列表(已排序);空 = "
            "无滞留非终态章(幂等)或对账跳过(看 skipped_reason)"
        ),
    )
    reset_count: int = Field(..., ge=0)
    skipped_reason: Literal["active_pipeline_run", "active_tasks"] | None = (
        Field(
            None,
            description=(
                "非 None = 本次未执行扫描:``active_pipeline_run``(pipeline "
                "循环正在写章)/ ``active_tasks``(chapter:write 等后台任务"
                "在写)— 防误杀,等任务结束后重试"
            ),
        )
    )


class RefreshContinuityItem(BaseModel):
    """``refresh-continuity-all`` 单章结果项(v1.1 w3-s7 E6)。"""

    model_config = ConfigDict(extra="forbid")

    chapter_id: str
    chapter_number: int = Field(..., ge=1)
    outcome: Literal["refreshed", "unchanged", "skipped_first"] = Field(
        ...,
        description=(
            "refreshed=continuity_context 真被重抽覆盖并写回卡 JSON;"
            "unchanged=重抽返回同值(上一章未 approved / 找不到 / LLM 抽提 "
            "fail-open 降级返原值);skipped_first=首章无上一章不处理。"
            "(JSON 损坏 / schema 不符的坏卡按 list_chapters 惯例 warn-skip,"
            "不入 items 也不计入 total_scanned)"
        ),
    )


class RefreshContinuityAllOut(BaseModel):
    """``POST /chapters:refresh-continuity-all`` 响应 data 段(workspace 批量,
    v1.1 w3-s7 E6)。

    一键重抽既有章节卡的 ``continuity_context``,治批量生成阶段遗留的 tail 占位
    字符串。``refreshed_count`` 是真被覆盖的章数;其余 outcome 见 ``items``。
    """

    model_config = ConfigDict(extra="forbid")

    workspace_id: str
    total_scanned: int = Field(..., ge=0, description="扫到的章节卡文件总数(= items 长度)")
    refreshed_count: int = Field(
        ..., ge=0, description="continuity_context 真被刷新覆盖的章数"
    )
    items: list[RefreshContinuityItem] = Field(
        ..., description="逐章结果,按 chapter_number 升序"
    )
