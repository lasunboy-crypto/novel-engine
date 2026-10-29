"""structlog 全局配置(P1-B-17)+ 章节级 contextvars 绑定 helper(P1-B-24)。

对齐 OBSERVABILITY §2.1 / §2.2 / §2.5:

- JSON Lines 输出(生产模式),console 彩色(开发模式)由 ``LOG_LEVEL=DEBUG``
  时切换。
- 必含字段:``timestamp`` / ``level`` / ``logger`` / ``event``;
  上下文字段(``request_id`` / ``trace_id`` / ``workspace_id`` 等)在调用点
  ``bind`` 进来。
- 敏感字段脱敏:``api_key`` / ``*_key`` / ``*_secret`` / ``*_token`` →
  ``***REDACTED***``(processor 级,所有 logger 共享)。

幂等性
======

``configure_logging()`` 直接调 ``structlog.configure``,后者本身幂等:重复调
会用最新一次的配置覆盖,内部不会泄漏 processor 链或 handler。这样 CLI
(P1-B-21)与 API(P1-B-17)可独立调用各自的 ``configure_logging()`` 而不
担心被对方污染。

P1-B-24:章节级 contextvars 绑定
================================

``bind_chapter_context(workspace_id, chapter_id, trace_id)`` 是上下文管理器,
在 ``with`` 块内所有 ``structlog`` 调用自动带上这三个字段(由 ``configure_logging``
注入的 ``merge_contextvars`` processor 完成合并),退出 ``with`` 时自动清理。

调用方:Orchestrator(``orchestrate_chapter`` 入口)/ ChapterService
(``revise`` 入口,不走 Orchestrator 但同样需要章节上下文)。Agent /
LLMClient 不感知 contextvars——它们的 ``_log.info(...)`` 自动继承当前
``with`` 块的字段,无需修改函数签名。

不在 ``configure_logging`` 内调用本 helper——后者是全局一次性配置,
``bind_chapter_context`` 是按章节闭环临时绑定。
"""

from __future__ import annotations

import json
import logging
import sys
from collections.abc import Iterator
from contextlib import contextmanager
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

import structlog

from src.config import settings

_SENSITIVE_KEY_SUFFIXES = ("_key", "_secret", "_token")
_SENSITIVE_KEY_NAMES = frozenset({"api_key", "authorization"})
_REDACTED = "***REDACTED***"


# =============================================================================
# P4-acceptance:JSONL 文件写入 handler(P4-B-05 留观项接通)
# =============================================================================


class WorkspaceAwareJSONLHandler(logging.Handler):
    """按 ``workspace_id`` 字段把 jsonl 行落到对应工作区 / 系统级目录。

    P4-B-05 设计的 ``LogsService.tail`` / ``GET /logs/trace/{id}`` 读端假设
    日志已落两份 jsonl(``src/services/logs_service.py:9-18`` 字面):

    - 系统级:``<data_dir>/_system_logs/YYYY-MM-DD.jsonl``
    - 工作区级:``<data_dir>/<workspace_id>/logs/YYYY-MM-DD.jsonl``

    写入端在 P4-B-05 字面"留 P5+ 或 P4-acceptance 配套"留观,本 handler
    在 P4-acceptance 范围内接通:emit 时按 record 携带的 ``workspace_id``
    字段分流;每日新文件按 UTC 日期 rotate(简版按日期分,无 size-based
    轮转,P5+ 量大时再升级 RotatingFileHandler);I/O 失败 swallow 避免日志
    handler 自身阻塞业务请求(对齐 ``*_safely`` 模式精神)。

    ``data_dir=None`` 时 emit 直接 return(测试 / 未注入路径不写文件)。
    """

    def __init__(self, data_dir: Path | None = None) -> None:
        super().__init__()
        self._data_dir = data_dir

    def emit(self, record: logging.LogRecord) -> None:
        if self._data_dir is None:
            return
        try:
            line = record.getMessage()
            # structlog JSONRenderer 已序列化为 JSON 字符串,这里只取 raw 字面
            # 写入文件;若上游用 ConsoleRenderer(DEBUG 模式),字符串非 JSON
            # 也照样写入(下游 logs_service ``_read_jsonl_dir`` 解析失败 swallow)。
            try:
                payload = json.loads(line)
            except (ValueError, TypeError):
                # 非 JSON 行(DEBUG console / 第三方库 stdlib log)落系统目录,
                # 不带 workspace 维度。
                payload = None

            workspace_id: str | None = None
            if isinstance(payload, dict):
                ws = payload.get("workspace_id")
                if isinstance(ws, str) and ws:
                    workspace_id = ws

            today = datetime.now(timezone.utc).date().isoformat()
            if workspace_id is not None:
                target_dir = self._data_dir / workspace_id / "logs"
            else:
                # ``LogsService._candidate_system_dirs`` 第 2 候选字面
                # ``<data_dir>/system_logs``;选用此候选避免动用项目根 data/
                # 子目录(``<data_dir.parent>/data/system_logs`` 在本仓库
                # 部署形态下 data/ 目录不存在,新建会改仓库结构)。
                target_dir = self._data_dir / "system_logs"
            target_dir.mkdir(parents=True, exist_ok=True)
            target_path = target_dir / f"{today}.jsonl"
            with target_path.open("a", encoding="utf-8") as fp:
                fp.write(line)
                if not line.endswith("\n"):
                    fp.write("\n")
        except Exception:  # noqa: BLE001
            # 日志 handler 失败不阻塞业务;swallow + 静默(避免 logger 内
            # 调 self.handleError 触发递归)。
            return


def _redact_sensitive(
    _logger: Any, _method_name: str, event_dict: dict[str, Any]
) -> dict[str, Any]:
    """OBSERVABILITY §2.5 敏感字段脱敏 processor。

    匹配规则:
    - key 完整等于 ``api_key`` / ``authorization``(case-insensitive)。
    - key 以 ``_key`` / ``_secret`` / ``_token`` 结尾(case-insensitive)。

    匹配到的字段值原地替换为 ``***REDACTED***``。仅遍历顶层 key,不递归——
    避免误改嵌套字典里的非敏感字段;调用方若需对嵌套结构脱敏,先在调用点处理。
    """

    for key in list(event_dict.keys()):
        lower = key.lower()
        if lower in _SENSITIVE_KEY_NAMES or lower.endswith(_SENSITIVE_KEY_SUFFIXES):
            event_dict[key] = _REDACTED
    return event_dict


def configure_logging() -> None:
    """全局配置 structlog + 标准库 logging。幂等;CLI / API 可各自独立调用。

    ``LOG_LEVEL`` 取自 ``settings.log_level``(.env / 环境变量)。
    DEBUG 走 console renderer(彩色易读),其他走 JSONRenderer(jsonl 友好)。
    """

    level_name = settings.log_level.upper()
    level = getattr(logging, level_name, logging.INFO)

    # 标准库 logging 桥接:第三方库(uvicorn / fastapi)的日志也走 structlog
    # 输出格式,统一一份 jsonl 流。
    logging.basicConfig(
        format="%(message)s",
        stream=sys.stdout,
        level=level,
        force=True,
    )

    # P4-acceptance:在 stdout handler 之外并行挂 jsonl 文件 handler,把
    # structlog 输出按 workspace_id 字段分流落两份 jsonl 文件(供
    # ``LogsService.tail`` / ``GET /logs/trace/{id}`` 读端消费)。
    # 失败不阻塞 — handler.emit 内 swallow,且 basicConfig force=True 已
    # 清空 root.handlers,这里追加不重复挂载。
    root_logger = logging.getLogger()
    has_jsonl = any(
        isinstance(h, WorkspaceAwareJSONLHandler) for h in root_logger.handlers
    )
    if not has_jsonl:
        jsonl_handler = WorkspaceAwareJSONLHandler(
            data_dir=settings.data_dir if settings.data_dir.exists() else None
        )
        jsonl_handler.setLevel(level)
        root_logger.addHandler(jsonl_handler)

    shared_processors: list[Any] = [
        structlog.contextvars.merge_contextvars,
        structlog.stdlib.add_log_level,
        structlog.stdlib.add_logger_name,
        structlog.processors.TimeStamper(fmt="iso", utc=True),
        _redact_sensitive,
        structlog.processors.StackInfoRenderer(),
        structlog.processors.format_exc_info,
    ]

    if level <= logging.DEBUG:
        renderer: Any = structlog.dev.ConsoleRenderer(colors=True)
    else:
        renderer = structlog.processors.JSONRenderer()

    structlog.configure(
        processors=[*shared_processors, renderer],
        wrapper_class=structlog.make_filtering_bound_logger(level),
        context_class=dict,
        # 用 stdlib LoggerFactory:底层 logger 有 ``.name`` 属性,
        # ``stdlib.add_logger_name`` processor 才能工作;同时与上面
        # ``logging.basicConfig`` 配合让第三方库(uvicorn / fastapi)的 stdlib
        # 日志也走同一份格式输出。
        logger_factory=structlog.stdlib.LoggerFactory(),
        # P1-B-24 改 ``cache_logger_on_first_use=False``:每次 ``_log.info(...)``
        # 重新从当前 config 解析 processor 链。原 ``True`` 在多次
        # ``configure_logging()`` 调用(API + CLI test 各调一次)后,会让
        # 早先 bind 的 logger 持着旧 list 引用,``structlog.testing.capture_logs``
        # 只能改最新 list,**测试隔离失败**。P1 规模(单用户、低吞吐)下
        # 缓存优化的性能差异不可感知;P3 高 QPS 场景再评估是否需要细分
        # 测试 vs 生产配置。
        cache_logger_on_first_use=False,
    )


@contextmanager
def bind_workspace_context(workspace_id: str) -> Iterator[None]:
    """工作区级 contextvars 绑定(v2 间隙 A3②,2026-06-13)。

    ``bind_chapter_context`` 的无章号变体,供**非章级** LLM 链路入口使用:
    outline 生成 / 单段重生 / commit gates(volume_seam judge)/ 章节卡
    单卡与批量生成。这些链路此前零绑定 → ``llm_request`` / ``llm_response``
    事件缺 ``workspace_id`` 字段,被 ``WorkspaceRoutingHandler`` 分流到
    ``<data_dir>/system_logs/`` 而非工作区日志 → 单看工作区日志少计
    writer/reviewer/卡链 ~86 次调用/书(质量闸成本审计 §7.2 工程债;
    cost_tracker DB 落账无此缺口,纯日志归属问题)。

    嵌套语义与 ``bind_chapter_context`` 一致(``bound_contextvars`` 退出
    只 unbind 自己设置的 key):若外层已有章级绑定,本 helper 不应再嵌套
    (章级三字段已含 workspace_id);它只用于"无章号可绑"的链路。
    """
    with structlog.contextvars.bound_contextvars(workspace_id=workspace_id):
        yield


@contextmanager
def bind_chapter_context(
    *,
    workspace_id: str,
    chapter_id: str,
    trace_id: str,
) -> Iterator[None]:
    """章节级 contextvars 绑定(OBSERVABILITY §2.2 必含字段)。

    ``with`` 块内所有 structlog 调用自动带上 ``workspace_id`` / ``chapter_id``
    / ``trace_id`` 三个字段(由 ``configure_logging`` 注入的
    ``merge_contextvars`` processor 自动合并到 event_dict);退出 ``with``
    时由 ``structlog.contextvars.bound_contextvars`` 负责清理,异常路径
    也会清理(``contextlib`` ExitStack 兜)。

    嵌套语义:本 helper 用 ``bound_contextvars`` 而非 ``bind_contextvars``,
    退出时只 unbind 自己设置的三个 key,不清空整个 contextvar 状态。
    P1 不出现 chapter 上下文嵌套(write 一次只跑一章),但保留嵌套友好性。

    Args:
        workspace_id: 当前操作所属工作区 ID。
        chapter_id: 当前操作所属章节 ID(如 ``"ch-0001"``)。
        trace_id: 章节级 trace,格式 ``{chapter_id}-{operation}{n}``,
            由 ``src.core.orchestrator.trace.make_trace_id`` 生成。
    """
    with structlog.contextvars.bound_contextvars(
        workspace_id=workspace_id,
        chapter_id=chapter_id,
        trace_id=trace_id,
    ):
        yield
