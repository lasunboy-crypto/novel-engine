"""成本守卫服务(P4-B-08)。

对齐 OBSERVABILITY §3.4(成本守卫配置)+ §3.3(单日 cost 超限阈值)+ §2.4
``cost_guard_triggered`` 事件 + API_CONTRACT §5.3 ``system.alerts`` 频道。

职责
====

1. **加载 ``cost_guard:`` 配置段**(``load_cost_guard_config``):从仓库根
   ``config.yaml``(或自定义路径)读 4 个字面字段 + ``notify_via`` 列表,
   yaml 缺失字段走 OBSERVABILITY §3.4 字面默认值。
2. **纯函数判定**(``evaluate_cost_guard``):接 caller 预读的成本数字
   (``daily_cost_usd`` / ``monthly_cost_usd`` / ``workspace_today_cost_usd``)
   + ``config`` → 返 ``CostGuardDecision``。判定函数无 IO,validator 同
   "预读数据接口"纪律(P4-B-07 沉淀)。
3. **副作用 helper**:``emit_cost_guard_event``(structlog ``cost_guard_triggered``
   + 可选 WS publish);``trigger_cost_guard_check``(组合 evaluate + emit,
   供 caller 一行调用)。

设计要点
========

- **不依赖持久层 / SQLAlchemy session**:本模块不查 metrics_daily 表。
  caller(orchestrator / 定时任务 / API 入口 gate)自己读 metrics 行,再调
  ``evaluate_cost_guard``。理由对齐 P4-B-07 ``validator_pipeline`` 预读
  数据接口:① 单测无需 in-memory SQLite;② ``src.services`` 不污染 caller
  的事务边界;③ 同一笔判定可被多个 caller 复用(章节启动 gate / 定时巡检 /
  API 调试端点)。
- **主流水线 gate 接入现状**(P4-B-08 留观于 v2 w1-s3 正式闭账,原字面
  "不集成 ChapterService / Orchestrator 入口 gate……主流水线 gate 接入由
  P4-acceptance(或独立后续任务)"已兑现,两层接入):
  ① **章级 gate**(P4-acceptance 接入):``ChapterService._run_gates`` —
  write / rewrite / revise 入口预读本工作区 ``metrics_daily`` 当日行调
  ``trigger_cost_guard_check``,``action=="pause"`` 抛
  ``CostGuardTriggered``(HTTP 402);
  ② **Run 级 gate**(v2 w1-s3 接入):``PipelineService._execute_loop``
  每章启动前调 ``_evaluate_run_cost_gate`` 同款预读判定,超限
  ``action=="pause"`` → run auto_pause(``pause_reason=
  "cost_guard_exceeded"``,可 :resume 续跑),**不走失败策略路由**(成本
  超限是 Run 级全局态,skip/retry 都是错误路由);章级 gate 抛
  ``CostGuardTriggered`` 时循环同语义特判兜底(两层行为一致)。
  遗留留观:跨工作区 daily / monthly 聚合维度两层均传 0.0,该两档限额
  实际不触发(``MetricsService`` 跨工作区聚合,P4-B-08 §8 留观,已登
  docs/v2_wishlist.md §"v2 w1-s3 期沉淀")。
- **workspace_id 透传纪律(P3-B-14 沉淀)**:``cost_guard_triggered`` event
  字段 ``workspace_id`` 由 caller 显式透传,本模块不读
  ``settings.data_dir`` / metrics 行字段自动猜。``workspace_id=None``
  表达"全工作区合计超限"(scope=daily / monthly 触发)。
- **WS publish helper 内联,不抽 ``persist_*_safely`` 公共模块**:本任务
  ``trigger_cost_guard_check`` 的 swallow 模式累计 6 处(P4-B-09 / P4-B-10
  第 7-8 处时再判定)— P4-B-07 / impl 文档接力沉淀。

字段映射(``CostGuardConfig`` ← config.yaml ``cost_guard:`` 段)
---------------------------------------------------------------

- ``daily_usd_limit``           ← float 默认 20.0
- ``monthly_usd_limit``         ← float 默认 300.0
- ``per_workspace_daily_limit`` ← float 默认 10.0
- ``action_on_exceed``          ← Literal["pause","warn","continue"] 默认 "pause"
- ``notify_via``                ← list[Literal["log","websocket"]] 默认 ["log","websocket"]
"""

from __future__ import annotations

from collections.abc import Sequence
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Literal

import structlog
import yaml
from pydantic import BaseModel, Field, field_validator

from src.api.websocket.channels import system_alerts_channel
from src.api.websocket.manager import WebSocketManager

_log = structlog.get_logger(__name__)


CostGuardAction = Literal["pause", "warn", "continue"]
"""``action_on_exceed`` 字面字段三档(OBSERVABILITY §3.4)。"""

CostGuardNotifyChannel = Literal["log", "websocket"]
"""``notify_via`` 字面字段两档(OBSERVABILITY §3.4)。"""

CostGuardScope = Literal["daily", "monthly", "workspace_daily"]
"""判定命中维度 — 全工作区单日 / 全工作区单月 / 单工作区单日。"""


class CostGuardConfig(BaseModel):
    """``cost_guard:`` 配置段(OBSERVABILITY §3.4 字面字段)。

    Pydantic v2 model;字段默认值对齐 OBSERVABILITY §3.4 字面 yaml 示例。
    """

    model_config = {"extra": "forbid"}

    cost_currency: str = Field(
        default="USD",
        pattern=r"^[A-Z]{3}$",
        description=(
            "三项 legacy *_usd_limit 字段实际使用的币种；字段名为兼容保留，"
            "不做汇率换算"
        ),
    )

    daily_usd_limit: float = Field(
        default=20.0,
        ge=0.0,
        description="全工作区合计单日成本上限(美元)",
    )
    monthly_usd_limit: float = Field(
        default=300.0,
        ge=0.0,
        description="全工作区合计单月成本上限(美元)",
    )
    per_workspace_daily_limit: float = Field(
        default=10.0,
        ge=0.0,
        description="单工作区单日成本上限(美元)",
    )
    action_on_exceed: CostGuardAction = Field(
        default="pause",
        description="触发后行为 — pause | warn | continue(§3.4)",
    )
    notify_via: list[CostGuardNotifyChannel] = Field(
        default_factory=lambda: ["log", "websocket"],
        description="通知通道列表 — log | websocket(§3.4)",
    )

    @field_validator("cost_currency", mode="before")
    @classmethod
    def _normalize_cost_currency(cls, value: Any) -> Any:
        return value.strip().upper() if isinstance(value, str) else value


@dataclass(frozen=True)
class CostGuardDecision:
    """单次成本守卫判定结果。

    - ``triggered``:任一阈值超限 = True;全部低于 = False。
    - ``action``:沿用 config ``action_on_exceed``(可被 caller 调整)。
    - ``scope``:命中维度;``None`` = 未触发。
    - ``current_usd`` / ``limit_usd``:命中维度的当前累积 / 阈值,供
      structlog 与 WS payload 透传(对齐 OBSERVABILITY §2.4 字面字段)。
    - ``reason``:可读字符串(``"daily cost $25.30 > $20.00 limit"`` 等),
      WS data 字段含同源信息便于前端展示。
    """

    triggered: bool
    action: CostGuardAction
    scope: CostGuardScope | None
    current_usd: float
    limit_usd: float
    reason: str
    workspace_id: str | None = None
    currency: str = "USD"


# =============================================================================
# 配置加载
# =============================================================================


def load_cost_guard_config(path: Path | None = None) -> CostGuardConfig:
    """从 yaml 文件加载 ``cost_guard:`` 段。

    :param path: yaml 路径;``None`` → 走 ``settings.config_yaml_path``
        默认 ``config.yaml``(仓库根)。
    :return: 已校验的 ``CostGuardConfig``;yaml 无 ``cost_guard:`` 段或
        文件不存在时返字面默认配置(对齐 OBSERVABILITY §3.4 yaml 示例值)。

    yaml 期望结构(顶层 ``cost_guard:`` 段)::

        cost_guard:
          daily_usd_limit: 20.0
          monthly_usd_limit: 300.0
          per_workspace_daily_limit: 10.0
          action_on_exceed: pause
          notify_via: [log, websocket]

    与 ``config_loader.load_writer_profiles_config`` 区别:本函数对**文件
    不存在 / 段不存在**走默认值,而非 fail-fast。理由:cost guard 是辅助
    限流功能,默认值"全工作区 $20/天 + pause"是工程合理的安全值;让
    缺配置场景"自动走默认"而非"启动失败",对维护人体验更友好。yaml
    段类型错(``daily_usd_limit: "abc"``)仍按 Pydantic ``ValidationError``
    抛,不会静默吞。
    """
    if path is None:
        # 延迟 import 避开循环依赖(``src.config.settings`` 在 ``api.main`` import 链路中)
        from src.config import settings

        path = settings.config_yaml_path
    if not path.exists():
        return CostGuardConfig()
    with path.open("r", encoding="utf-8") as fp:
        raw: dict[str, Any] = yaml.safe_load(fp) or {}
    if not isinstance(raw, dict):
        return CostGuardConfig()
    section = raw.get("cost_guard")
    if not isinstance(section, dict):
        return CostGuardConfig()
    return CostGuardConfig.model_validate(section)


# =============================================================================
# P7-B-05:工作区级 cost_guard 配置 lazy 缓存
# =============================================================================


_WORKSPACE_COST_GUARD_CACHE: dict[str, CostGuardConfig] = {}
"""P7-B-05 模块级 lazy 缓存。每工作区首次访问时加载 ``ConfigService.
load_cost_guard_config_for_workspace`` + 缓存,后续 dict 取 O(1)。

进程级单例(对齐 ``api/deps.py:_COST_GUARD_CONFIG`` 既有模式),无锁 — 写入
路径走 ``PUT /workspaces/{id}/config`` 时 caller 必须显式调
``reset_workspace_cost_guard_config_cache(workspace_id)`` 让本次写入立即生效;
否则下次 ``get_workspace_cost_guard_config`` 仍读旧缓存值。配置变更
高频度低(用户在 ConfigPage 手动改),不做 mtime / inotify 监听复杂化。
"""


def get_workspace_cost_guard_config(
    workspace_id: str,
    *,
    default: CostGuardConfig | None = None,
) -> CostGuardConfig:
    """P7-B-05:取工作区级 ``CostGuardConfig``,lazy 缓存。

    优先级:**工作区 project.yaml cost_guard 段 > default(注入的引擎默认 /
    单例)> 字面默认 ``CostGuardConfig()``**。

    - 工作区 project.yaml 含 ``cost_guard:`` 段(非空) → 调
      ``ConfigService.load_cost_guard_config_for_workspace`` 加载合并(P6-B-15
      字段级合并语义)+ 落缓存,后续 dict 取 O(1)。
    - 工作区 project.yaml 无 cost_guard 段 / 无 project.yaml / 无目录 → 返
      ``default``(若给定;通常是 ``deps.py:_COST_GUARD_CONFIG`` 单例)或
      ``CostGuardConfig()`` 字面默认。**不落缓存** — 让 default 配置变更时不
      要被缓存吃住(单测注入 default 在每个 case 改变,这条很重要)。
    - 加载失败 → swallow + 落 ``default`` / 字面默认到缓存。

    替代 ``api/deps.py:_COST_GUARD_CONFIG`` 单例直用模式 — caller 链
    (``ChapterService._run_gates``)改走本函数后,工作区级 cost_guard 覆盖
    (P6-B-15 落地的 yaml 字段)即时生效;无工作区覆盖时 P1-P6 行为字面不变。
    """
    cached = _WORKSPACE_COST_GUARD_CACHE.get(workspace_id)
    if cached is not None:
        return cached
    # 先判断工作区是否显式有 cost_guard 覆盖 — 没有则回退 default,保持 P1-P6
    # 既有"注入单例直用"行为不变(避免引擎 yaml 默认值反向覆盖 caller 注入)。
    if not _workspace_has_cost_guard_override(workspace_id):
        return default if default is not None else CostGuardConfig()
    try:
        # 延迟 import 规避 ``src.services.config_service`` 顶层引用 ``cost_guard``
        # (config_service 顶层 ``from src.services.cost_guard import CostGuardConfig``)
        # 的反向循环;函数体内 import 在运行时模块已初始化,无 ImportError。
        from src.services.config_service import ConfigService

        cfg = ConfigService().load_cost_guard_config_for_workspace(workspace_id)
    except Exception as exc:  # noqa: BLE001 — swallow per *_safely 先例
        _log.warning(
            "workspace_cost_guard_config_load_failed",
            workspace_id=workspace_id,
            error_type=type(exc).__name__,
            error_message=str(exc),
        )
        cfg = default if default is not None else CostGuardConfig()
    _WORKSPACE_COST_GUARD_CACHE[workspace_id] = cfg
    return cfg


def _workspace_has_cost_guard_override(workspace_id: str) -> bool:
    """检测工作区 project.yaml 是否显式含非空 ``cost_guard:`` 段(P7-B-05)。

    工作区目录不存在 / project.yaml 不存在 / 顶层非 dict / cost_guard 段缺失 /
    段值为 None(yaml ``cost_guard:`` 写成空)→ 返 ``False``。yaml 语法错 /
    IO 错也吞掉返 ``False``(``ConfigService`` 加载时会同样路径再抛错并被
    上层 swallow + log)。

    本检测是 lazy cache 是否启用工作区覆盖路径的 gate;返 ``False`` 时回退到
    caller 注入的 default 单例,保持 P1-P6 行为字面不变。
    """
    try:
        # 延迟 import 规避顶层循环(WorkspacePaths 仅本函数用)。
        from src.config import settings
        from src.core.workspace.workspace_paths import WorkspacePaths

        paths = WorkspacePaths(workspace_id, workspaces_root=settings.data_dir)
        if not paths.project_yaml.is_file():
            return False
        with paths.project_yaml.open("r", encoding="utf-8") as fp:
            raw = yaml.safe_load(fp)
    except Exception:  # noqa: BLE001
        return False
    if not isinstance(raw, dict):
        return False
    section = raw.get("cost_guard")
    return isinstance(section, dict) and bool(section)


def reset_workspace_cost_guard_config_cache(
    workspace_id: str | None = None,
) -> None:
    """P7-B-05:清 workspace cost_guard 配置 lazy 缓存。

    :param workspace_id: 给定时清单工作区;``None`` 时清全部(单测 /
        ``PUT /workspaces/{id}/config`` 写入回调用)。
    """
    if workspace_id is None:
        _WORKSPACE_COST_GUARD_CACHE.clear()
    else:
        _WORKSPACE_COST_GUARD_CACHE.pop(workspace_id, None)


# =============================================================================
# 纯判定函数(无 IO)
# =============================================================================


def evaluate_cost_guard(
    *,
    config: CostGuardConfig,
    daily_cost_usd: float = 0.0,
    monthly_cost_usd: float = 0.0,
    workspace_today_cost_usd: float = 0.0,
    workspace_id: str | None = None,
) -> CostGuardDecision:
    """成本守卫判定函数(纯函数,无副作用)。

    按"单工作区单日 → 全工作区单日 → 全工作区单月"优先级判断超限维度,
    任一命中即返 ``triggered=True``;否则返 ``triggered=False`` +
    ``scope=None``。

    :param config: ``CostGuardConfig`` 配置实例(``load_cost_guard_config``
        产出)。
    :param daily_cost_usd: 全工作区当日合计成本(由 caller 跨工作区聚合
        ``metrics_daily.total_cost_usd`` 当日列)。
    :param monthly_cost_usd: 全工作区当月合计成本(同上,30 天聚合)。
    :param workspace_today_cost_usd: 单工作区当日合计成本
        (``metrics_daily.total_cost_usd`` 当日单行)。
    :param workspace_id: 触发时透传到 ``CostGuardDecision.workspace_id``;
        ``None`` 表达"全工作区合计"维度触发。
    :return: ``CostGuardDecision``。
    """
    # 单工作区单日优先(粒度最细,通常先命中)
    if (
        workspace_id is not None
        and workspace_today_cost_usd > config.per_workspace_daily_limit
    ):
        return CostGuardDecision(
            triggered=True,
            action=config.action_on_exceed,
            scope="workspace_daily",
            current_usd=workspace_today_cost_usd,
            limit_usd=config.per_workspace_daily_limit,
            reason=(
                f"workspace {workspace_id} today cost "
                f"{config.cost_currency} {workspace_today_cost_usd:.4f} > "
                f"{config.cost_currency} "
                f"{config.per_workspace_daily_limit:.4f} limit"
            ),
            workspace_id=workspace_id,
            currency=config.cost_currency,
        )

    if daily_cost_usd > config.daily_usd_limit:
        return CostGuardDecision(
            triggered=True,
            action=config.action_on_exceed,
            scope="daily",
            current_usd=daily_cost_usd,
            limit_usd=config.daily_usd_limit,
            reason=(
                f"daily cost {config.cost_currency} {daily_cost_usd:.4f} > "
                f"{config.cost_currency} {config.daily_usd_limit:.4f} limit"
            ),
            workspace_id=workspace_id,
            currency=config.cost_currency,
        )

    if monthly_cost_usd > config.monthly_usd_limit:
        return CostGuardDecision(
            triggered=True,
            action=config.action_on_exceed,
            scope="monthly",
            current_usd=monthly_cost_usd,
            limit_usd=config.monthly_usd_limit,
            reason=(
                f"monthly cost {config.cost_currency} {monthly_cost_usd:.4f} > "
                f"{config.cost_currency} {config.monthly_usd_limit:.4f} limit"
            ),
            workspace_id=workspace_id,
            currency=config.cost_currency,
        )

    return CostGuardDecision(
        triggered=False,
        action=config.action_on_exceed,
        scope=None,
        current_usd=0.0,
        limit_usd=0.0,
        reason="under all limits",
        workspace_id=workspace_id,
        currency=config.cost_currency,
    )


# =============================================================================
# 副作用 helper(structlog + WS publish)
# =============================================================================


def emit_cost_guard_event(
    decision: CostGuardDecision,
    *,
    notify_via: Sequence[CostGuardNotifyChannel] = ("log", "websocket"),
    ws_manager: WebSocketManager | None = None,
) -> None:
    """触发时发 structlog + WS 推送(``triggered=False`` 时静默返回)。

    OBSERVABILITY §2.4 字面 ``cost_guard_triggered`` 字段
    ``workspace_id`` / ``current_usd`` / ``limit_usd`` / ``action``;本函数
    扩充 ``scope`` 与 ``reason`` 透传给 LogsPage / WS 客户端定位维度。

    ``notify_via`` 控制通知通道;``"log"`` 不在表里 → 不 emit structlog
    (但仍走默认 INFO log 路径;P4 简版无独立"静音"模式,保留向后兼容)。
    ``"websocket"`` 不在表里 → 不 WS publish。

    ``ws_manager=None`` → 不 WS publish(测试 / 无 WS 上下文场景);生产
    入口传 ``src.api.websocket.manager.manager`` 单例。
    """
    if not decision.triggered:
        return

    if "log" in notify_via:
        _log.warning(
            "cost_guard_triggered",
            workspace_id=decision.workspace_id,
            current_usd=decision.current_usd,
            limit_usd=decision.limit_usd,
            action=decision.action,
            scope=decision.scope,
            reason=decision.reason,
            currency=decision.currency,
        )

    if "websocket" in notify_via and ws_manager is not None:
        # API_CONTRACT §5.3 字面 ``system.alerts`` data 字段 ``{current_usd,
        # limit_usd}``;扩 ``workspace_id`` / ``action`` / ``scope`` /
        # ``reason`` 与 structlog 字段同源便于前端定位。
        try:
            ws_manager.publish(
                channel=system_alerts_channel(),
                event="cost_guard_triggered",
                data={
                    "workspace_id": decision.workspace_id,
                    "current_usd": decision.current_usd,
                    "limit_usd": decision.limit_usd,
                    "action": decision.action,
                    "scope": decision.scope,
                    "reason": decision.reason,
                    "currency": decision.currency,
                },
            )
        except Exception as exc:  # noqa: BLE001 — swallow per *_safely 先例
            _log.error(
                "cost_guard_notify_failed",
                stage="ws_publish",
                error_type=type(exc).__name__,
                error_message=str(exc),
            )


def trigger_cost_guard_check(
    *,
    config: CostGuardConfig,
    daily_cost_usd: float = 0.0,
    monthly_cost_usd: float = 0.0,
    workspace_today_cost_usd: float = 0.0,
    workspace_id: str | None = None,
    ws_manager: WebSocketManager | None = None,
) -> CostGuardDecision:
    """组合 ``evaluate_cost_guard`` + ``emit_cost_guard_event`` 一行调用。

    供 caller(orchestrator 入口 gate / 定时巡检 / API 端点)一次性 evaluate
    并触发通知。``notify_via`` 直接取 ``config.notify_via``(由
    ``load_cost_guard_config`` 装载)。
    """
    decision = evaluate_cost_guard(
        config=config,
        daily_cost_usd=daily_cost_usd,
        monthly_cost_usd=monthly_cost_usd,
        workspace_today_cost_usd=workspace_today_cost_usd,
        workspace_id=workspace_id,
    )
    emit_cost_guard_event(
        decision,
        notify_via=tuple(config.notify_via),
        ws_manager=ws_manager,
    )
    return decision


__all__ = [
    "CostGuardAction",
    "CostGuardConfig",
    "CostGuardDecision",
    "CostGuardNotifyChannel",
    "CostGuardScope",
    "emit_cost_guard_event",
    "evaluate_cost_guard",
    "get_workspace_cost_guard_config",
    "load_cost_guard_config",
    "reset_workspace_cost_guard_config_cache",
    "trigger_cost_guard_check",
]
