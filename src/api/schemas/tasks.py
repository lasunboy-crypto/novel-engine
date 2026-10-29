"""异步任务相关基础 schema(P1-B-17 + P1-B-26 类型化扩展)。

对齐 API_CONTRACT §6 异步任务模式:长操作(写作、导出、备份)统一返回
``task_id``,客户端通过 WS 订阅进度。本任务只落最小字段集,P1-B-19 章节路由
与 P1-B-20 WebSocket 接进来后再扩字段(``progress``、``result_url`` 等)。

P1 阶段任务可能同步执行,但接口形态遵循异步任务模式以降低 P2 后台 Worker
切换的成本(API_CONTRACT §6 注释)。

P1-B-26:章节路由 ``:write`` / ``:rewrite`` / ``:revise`` 引入 ``TaskInfo``
类型化子类(``TaskInfoChapterWrite`` / ``TaskInfoChapterRevise``),把 ``result``
字段从弱类型 ``dict[str, Any] | None`` 收紧到具体业务模型(``WriteResultSummary`` /
``ChapterVersionMeta``)。理由:基类 ``TaskInfo.result`` 弱类型导致这两个业务
模型无法进入 OpenAPI components/schemas,前端 ``pnpm gen-types`` 拿不到类型,
违反 CLAUDE.md 硬约束 5"禁止手写后端字段"(QUESTIONS.md Q1)。子类只覆盖
``result`` 字段类型,其余字段沿用基类;``task_registry`` 内部仍存基类
``TaskInfo``,路由层 ``model_validate`` 转子类后再包装到 ``SuccessEnvelope``。
"""

from __future__ import annotations

from datetime import datetime
from typing import Any, Literal

from pydantic import BaseModel, ConfigDict, Field

from src.api.schemas.chapters import WriteResultSummary
from src.api.schemas.versions import ChapterVersionMeta

TaskStatus = Literal[
    "pending", "running", "done", "failed", "interrupted", "cancelled"
]
"""任务状态机。

``interrupted`` 由 v1.1 wave 3 session 5(E3)新增 —— 进程重启后,落库的
``pending`` / ``running`` 僵尸行被 lifespan 启动扫描翻成本状态(对应 in-flight
BackgroundTask 已随进程死亡)。**与 ``failed`` 的区别**:``failed`` 是业务执行
失败(LLM 报错 / 工作区销毁等),``interrupted`` 是因后端重启而中断、并非任务
本身出错;前端据此显示"已中断"而非"失败",顶层状态机直接区分两者(维护人拍板,
2026-05-29,详 ``src/api/task_registry.py`` 模块 docstring)。

``cancelled`` 由 v1.1 wave 4 session 6(J1)新增 —— 用户经 ``POST
/tasks/{id}:cancel`` 主动取消、或 workspace destroy/purge/archive 级联取消正在
跑的任务,后台 worker 捕获 ``LLMCancelledError`` 翻成本状态。**与 ``interrupted``
的区别**:``interrupted`` 是被动(后端重启),``cancelled`` 是主动(用户意图);
**与 ``failed`` 的区别**:``cancelled`` 非任务出错。沿用 ``interrupted`` 顶层
状态档先例(维护人偏好顶层档比嵌套 error 字段更类型安全)。"""


class TaskInfo(BaseModel):
    """任务状态查询响应体(API_CONTRACT §6)。

    P1 阶段字段集合:

    - ``task_id``:任务唯一标识。
    - ``type``:任务类型(``chapter_write`` / ``chapter_rewrite`` /
      ``chapter_revise`` 等);P1 由 P1-B-19 路由层枚举,本任务不约束具体值。
    - ``status``:六档状态机(详 ``TaskStatus`` Literal)。
    - ``progress``:0-100 整数,P1 同步执行可只在 ``pending`` / ``done`` 间跳变。
    - ``result``:成功时的结果对象(章节版本元数据等),失败时为 ``None``。
    - ``error``:失败时的错误码 + 中文消息,成功时为 ``None``。
    - ``created_at`` / ``updated_at``:UTC ISO 8601。
    """

    model_config = ConfigDict(extra="forbid")

    task_id: str
    type: str
    status: TaskStatus
    progress: int = Field(default=0, ge=0, le=100)
    result: dict[str, Any] | None = None
    error: dict[str, Any] | None = None
    created_at: datetime
    updated_at: datetime


class TaskInfoChapterWrite(TaskInfo):
    """``:write`` / ``:rewrite`` / ``:re-review`` 路由专用 ``TaskInfo`` 子类(P1-B-26)。

    覆盖 ``type`` 字段为 ``Literal["chapter_write", "chapter_rewrite",
    "chapter_re_review"]``、``result`` 字段为 ``WriteResultSummary | None``。三条
    路由共用同一子类,因为 result 摘要类型完全一致(只是任务来源不同,由 ``type``
    区分)。``chapter_re_review``(v2 W3.5 UI 缺口3)= 人工改稿后单次重审,产出
    同一 ``WriteResultSummary``(含 final_state + escalation_reason)。

    OpenAPI 影响:``WriteResultSummary`` 进 components/schemas,前端
    ``pnpm gen-types`` 自动产出对应 TS 类型。
    """

    type: Literal[  # type: ignore[assignment]
        "chapter_write", "chapter_rewrite", "chapter_re_review"
    ]
    result: WriteResultSummary | None = None  # type: ignore[assignment]


class TaskInfoChapterRevise(TaskInfo):
    """``:revise`` 路由专用 ``TaskInfo`` 子类(P1-B-26)。

    覆盖 ``type`` 字段为 ``Literal["chapter_revise"]``、``result`` 字段为
    ``ChapterVersionMeta | None``。:revise 不走 Orchestrator,只产出新版本
    元数据(不含正文),所以 result 类型与 :write/:rewrite 不同。

    OpenAPI 影响:``ChapterVersionMeta`` 进 components/schemas。
    """

    type: Literal["chapter_revise"]  # type: ignore[assignment]
    result: ChapterVersionMeta | None = None  # type: ignore[assignment]
