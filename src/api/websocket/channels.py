"""WebSocket 标准频道命名(P1-B-20;P4-B-08 扩 ``system.alerts``)。

对齐 API_CONTRACT §5.3 标准频道表。**P1 只发 ``state_transition`` 事件**
(到 ``chapter.{chapter_id}.events`` 频道);P4-B-08 起新增 ``system.alerts``
推送 ``cost_guard_triggered`` / ``memory_health_critical`` 等系统级事件
(``pipeline.events`` / ``task.{id}.progress`` 多事件型仍留 P5+ 再做)。

本模块只提供频道名拼装函数,不持有状态。订阅 / 推送由 ``manager`` 模块负责。
"""

from __future__ import annotations

SYSTEM_ALERTS_CHANNEL: str = "system.alerts"
"""``system.alerts`` 字面频道字符串(API_CONTRACT §5.3 字面)。

跨工作区共享的全局频道,前端 / CLI 客户端订阅 ``"system.alerts"`` 即可接收
``cost_guard_triggered``(P4-B-08)/ ``memory_health_critical``(P4-B-09)等
推送事件。
"""


PIPELINE_EVENTS_CHANNEL: str = "pipeline.events"
"""``pipeline.events`` 字面频道字符串(API_CONTRACT §5.3 字面)。

跨工作区共享的全局频道(对齐 ``system.alerts`` 模式),P6-B-02 起承载流水线
``pipeline_started`` / ``pipeline_paused`` / ``pipeline_resumed`` /
``pipeline_stopped`` / ``chapter_started`` / ``chapter_finished`` 6 事件。
"""


def chapter_events_channel(chapter_id: str) -> str:
    """章节事件频道:``chapter.{chapter_id}.events``。

    P1 推 ``state_transition``;P2+ 扩 ``token_progress`` / ``review_done`` 等
    (API_CONTRACT §5.3)。
    """
    return f"chapter.{chapter_id}.events"


def chapter_card_events_channel(chapter_id: str) -> str:
    """章节卡事件频道:``chapter.{chapter_id}.card.events``(P5-B-07 引入)。

    与 ``chapter_events_channel`` **解耦的独立子频道**(决策 6=C 字面"章节卡
    生命周期独立于章节正文生命周期"):card_status 转换不与 chapter.status 7
    档共用 ``state_transition`` 事件,避免前端 ChapterEditorPage 与
    ChapterCardReviewPage 订阅同频道时事件类型混杂。

    P5-B-07 推 3 事件:

    - ``card_approved``:``POST card:approve`` 成功后(data 含 ``chapter_id``
      / ``card_status`` / ``card_approved_at``)
    - ``card_rejected``:``POST card:reject`` 成功后(data 含 ``chapter_id``
      / ``card_status`` / ``rejection_notes``)
    - ``card_status_changed``:``transition_card_status`` 通用状态变更
      (data 含 ``chapter_id`` / ``from_status`` / ``to_status``)

    前端 ChapterCardReviewPage 订阅 ``chapter.<id>.card.events`` 实时刷新
    状态徽章 + 退回原因展示。
    """
    return f"chapter.{chapter_id}.card.events"


def task_progress_channel(task_id: str) -> str:
    """任务进度频道:``task.{task_id}.progress``。

    P1 阶段任务同步执行,**不主动推送**;命名函数先落,P3 切后台 worker 后由
    worker 实现 ``stage`` / ``complete`` / ``error`` 事件推送(API_CONTRACT §5.3)。
    """
    return f"task.{task_id}.progress"


def system_alerts_channel() -> str:
    """系统告警频道:``system.alerts``(API_CONTRACT §5.3 字面常量)。

    P4-B-08 起由 cost_guard / threshold_alerts 模块用作 ``manager.publish``
    的 ``channel`` 入参。返回 ``SYSTEM_ALERTS_CHANNEL`` 常量,与
    ``chapter_events_channel`` / ``task_progress_channel`` 函数形态对齐
    (尽管无参数,统一函数形态便于未来按工作区拆频道时不破接口)。
    """
    return SYSTEM_ALERTS_CHANNEL


def pipeline_events_channel() -> str:
    """流水线事件频道:``pipeline.events``(API_CONTRACT §5.3 字面常量)。

    P6-B-02 起承载 ``pipeline_started`` / ``pipeline_paused`` /
    ``pipeline_resumed`` / ``pipeline_stopped`` / ``chapter_started`` /
    ``chapter_finished`` 6 事件;前端 RunControlPage 订阅本频道实时刷新
    流水线状态。无参数(跨工作区共享,对齐 ``system.alerts`` 模式);函数
    形态保留未来按工作区拆频道时不破接口。
    """
    return PIPELINE_EVENTS_CHANNEL


def workspace_events_channel(workspace_id: str) -> str:
    """工作区事件频道:``workspace.{workspace_id}.events``(P6-B-07 引入)。

    承载工作区级备份 / 恢复事件:``backup_created``(手动 / 自动备份完成 +
    ``data`` 含 ``backup_id`` / ``backup_type`` / ``file_path``)/
    ``restore_completed``(restore 完成 + ``data`` 含 ``backup_id`` /
    ``pre_restore_backup_id``)。与 ``chapter_events_channel(chapter_id)``
    同模式 per-workspace 拆频道(对齐"前端只订阅自己工作区"语义,跨工作区
    共享走 ``system.alerts`` 大频道)。
    """
    return f"workspace.{workspace_id}.events"


__all__ = [
    "PIPELINE_EVENTS_CHANNEL",
    "SYSTEM_ALERTS_CHANNEL",
    "chapter_card_events_channel",
    "chapter_events_channel",
    "pipeline_events_channel",
    "system_alerts_channel",
    "task_progress_channel",
    "workspace_events_channel",
]
