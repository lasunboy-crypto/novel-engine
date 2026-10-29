"""日志查询服务(P4-B-05)。

OBSERVABILITY §2.6 字面 ``GET /logs?...`` / ``GET /logs/trace/{id}`` 端点的
后端查询实现,以及 CLI ``novel logs`` 子命令复用的同一查询函数。

数据来源(目录约定,对齐 OBSERVABILITY §2.1)
=============================================

- 系统级:``<data_dir.parent>/data/system_logs/YYYY-MM-DD.jsonl``;不存在时
  按工作区根的同级 ``data/system_logs/`` 推算(具体路径由 ``data_dir`` 参数
  决定,详见 ``LogsService.__init__``)。
- 工作区维度:``<data_dir>/<workspace_id>/logs/YYYY-MM-DD.jsonl``;不存在时
  跳过(返空)。

**P4-B-05 范围**:logs API + CLI **查询接口框架**先于 jsonl 写入接通落地
(对齐 P4-B-04 ``system_session_factory`` 渐进 opt-in 节奏)。本任务交付:
读端 + Pydantic schemas + 单测覆盖;jsonl 写入接通(``configure_logging``
加 ``RotatingFileHandler``)留 P5+ 或 P4-acceptance 配套。jsonl 文件不存在
时,所有端点 200 + 空数组(对齐 metrics 端点"未启用时不报错"先例)。

错误语义
--------

- 目录 / 文件不存在 → 静默跳过(返空,不抛)。
- 单行 JSON 解析失败 → 跳过该行,**继续**处理后续行(失败 swallow + 不冒泡)。
  日志查询不能因为一行损坏 jsonl 阻断整体查询。
- ``timestamp`` 字段解析失败 → 该行跳过(无时间戳无法过滤 since/until)。
"""

from __future__ import annotations

import datetime as _dt
import json
from collections.abc import Iterator
from dataclasses import dataclass
from pathlib import Path
from typing import Any


@dataclass(frozen=True)
class LogQueryFilters:
    """``GET /logs`` 多字段过滤器。所有字段 None 表示不过滤。

    ``page`` 从 1 起;``page_size`` 上限 500(对齐 ``LogQueryOut`` schema 校验)。
    """

    workspace_id: str | None = None
    chapter_id: str | None = None
    trace_id: str | None = None
    request_id: str | None = None
    event: str | None = None
    level: str | None = None
    since: _dt.datetime | None = None
    until: _dt.datetime | None = None
    page: int = 1
    page_size: int = 50


class LogsService:
    """jsonl 日志读端服务(只读 + 失败 swallow)。

    单一构造参 ``data_dir`` = 工作区根目录(``settings.data_dir``)。系统级
    ``system_logs/`` 默认在 ``data_dir.parent / "data" / "system_logs"``;
    若 ``data_dir`` 自身已包含 ``system_logs`` 子目录(测试 fixture 直接落
    在 data_dir 下时),也会被识别。两条候选路径都尝试,谁先存在用谁。
    """

    def __init__(self, *, data_dir: Path) -> None:
        self._data_dir = Path(data_dir).resolve()

    # ---- 路径解析 ----------------------------------------------------------

    def _candidate_system_dirs(self) -> list[Path]:
        """系统级 jsonl 候选目录(双 fallback)。"""
        return [
            self._data_dir.parent / "data" / "system_logs",
            self._data_dir / "system_logs",
        ]

    def _workspace_logs_dir(self, workspace_id: str) -> Path:
        return self._data_dir / workspace_id / "logs"

    # ---- 公共查询接口 ------------------------------------------------------

    def query(self, filters: LogQueryFilters) -> tuple[list[dict[str, Any]], int]:
        """按 ``filters`` 加载所有候选 jsonl,过滤 + 排序 + 分页。

        返 ``(items, total)``。``items`` 为当前页结果(按 timestamp 升序);
        ``total`` 为应用筛选后未分页的总条数。
        """
        rows = list(self._iter_all_rows(filters.workspace_id))
        filtered = [r for r in rows if _matches(r, filters)]
        # 按 timestamp 升序(string ISO 8601 字典序与时间序一致)
        filtered.sort(key=lambda r: str(r.get("timestamp", "")))
        total = len(filtered)
        start = (filters.page - 1) * filters.page_size
        end = start + filters.page_size
        return filtered[start:end], total

    def list_by_trace(self, trace_id: str) -> list[dict[str, Any]]:
        """按 ``trace_id`` 字段精确匹配,返按 timestamp 升序的全部命中行。"""
        rows = [
            r
            for r in self._iter_all_rows(workspace_id=None)
            if r.get("trace_id") == trace_id
        ]
        rows.sort(key=lambda r: str(r.get("timestamp", "")))
        return rows

    def list_by_request(self, request_id: str) -> list[dict[str, Any]]:
        """按 ``request_id`` 字段精确匹配,返按 timestamp 升序的全部命中行。"""
        rows = [
            r
            for r in self._iter_all_rows(workspace_id=None)
            if r.get("request_id") == request_id
        ]
        rows.sort(key=lambda r: str(r.get("timestamp", "")))
        return rows

    def tail(self, n: int = 50) -> list[dict[str, Any]]:
        """读最近 ``n`` 行(按 timestamp 降序)。CLI ``--tail`` 复用。"""
        rows = list(self._iter_all_rows(workspace_id=None))
        rows.sort(key=lambda r: str(r.get("timestamp", "")), reverse=True)
        return rows[:n]

    # ---- 内部 IO -----------------------------------------------------------

    def _iter_all_rows(
        self, workspace_id: str | None
    ) -> Iterator[dict[str, Any]]:
        """遍历所有候选 jsonl 文件 + yield 解析成功的行。

        行解析失败 swallow(continue),不影响后续行。**目录去重**(
        P4-acceptance 步 ⑧ 接力 P4-F-04):``data_dir`` 下 ``_active`` /
        ``_pristine`` 等符号链接与真实工作区目录会被 ``iterdir()`` 当
        独立 child 各 yield 一次,导致同一份 jsonl 被读两次;通过
        ``resolve()`` 真实路径集合去重。
        """
        # 系统级
        seen_dirs: set[Path] = set()
        for sys_dir in self._candidate_system_dirs():
            try:
                real = sys_dir.resolve()
            except OSError:
                continue
            if real in seen_dirs:
                continue
            seen_dirs.add(real)
            yield from _read_jsonl_dir(sys_dir)

        # 工作区维度:① 显式指定 workspace_id 时只读该工作区;② 否则
        # 遍历 data_dir 下所有 <workspace_id>/logs/ 子目录(经 resolve()
        # 去重符号链接,避免 ``_active`` 双读 quirk)。
        if workspace_id is not None:
            yield from _read_jsonl_dir(self._workspace_logs_dir(workspace_id))
        elif self._data_dir.exists():
            for child in self._data_dir.iterdir():
                if child.is_dir() and (child / "logs").exists():
                    logs_dir = child / "logs"
                    try:
                        real = logs_dir.resolve()
                    except OSError:
                        continue
                    if real in seen_dirs:
                        continue
                    seen_dirs.add(real)
                    yield from _read_jsonl_dir(logs_dir)


# ---- 模块级 helper ---------------------------------------------------------


def _read_jsonl_dir(directory: Path) -> Iterator[dict[str, Any]]:
    """读目录下所有 ``*.jsonl`` 文件,yield 每行解析成功的 dict。

    ``directory`` 不存在 → 直接 return(无 yield)。单行 JSON 解析失败
    swallow + continue。文件 IO 失败 swallow + continue 下一文件。
    """
    if not directory.exists() or not directory.is_dir():
        return
    for path in sorted(directory.glob("*.jsonl")):
        try:
            with path.open("r", encoding="utf-8") as fh:
                for raw in fh:
                    line = raw.strip()
                    if not line:
                        continue
                    try:
                        obj = json.loads(line)
                    except json.JSONDecodeError:
                        continue
                    if isinstance(obj, dict):
                        yield obj
        except OSError:
            continue


def _parse_iso(value: Any) -> _dt.datetime | None:
    """ISO 8601 string → datetime(失败返 None)。

    ``Z`` 后缀(``2026-05-10T14:30:00.123Z``)需要先替换为 ``+00:00`` 才能
    被 ``datetime.fromisoformat`` 识别(<Python 3.11 的兼容性同步)。
    """
    if isinstance(value, _dt.datetime):
        return value
    if not isinstance(value, str):
        return None
    text = value.replace("Z", "+00:00") if value.endswith("Z") else value
    try:
        return _dt.datetime.fromisoformat(text)
    except ValueError:
        return None


def _matches(row: dict[str, Any], filters: LogQueryFilters) -> bool:
    """按 ``filters`` 字段做 AND 过滤。任一不匹配返 False。"""
    if filters.workspace_id and row.get("workspace_id") != filters.workspace_id:
        return False
    if filters.chapter_id and row.get("chapter_id") != filters.chapter_id:
        return False
    if filters.trace_id and row.get("trace_id") != filters.trace_id:
        return False
    if filters.request_id and row.get("request_id") != filters.request_id:
        return False
    if filters.event and row.get("event") != filters.event:
        return False
    if filters.level and str(row.get("level", "")).upper() != filters.level.upper():
        return False
    if filters.since is not None or filters.until is not None:
        ts = _parse_iso(row.get("timestamp"))
        if ts is None:
            return False
        # 比较前对齐 timezone:naive datetime 视作 UTC,避免 since/until 不同
        # 时区时 < / > 抛 TypeError。
        if ts.tzinfo is None:
            ts = ts.replace(tzinfo=_dt.timezone.utc)
        if filters.since is not None:
            since = filters.since
            if since.tzinfo is None:
                since = since.replace(tzinfo=_dt.timezone.utc)
            if ts < since:
                return False
        if filters.until is not None:
            until = filters.until
            if until.tzinfo is None:
                until = until.replace(tzinfo=_dt.timezone.utc)
            if ts > until:
                return False
    return True


def classify_trace_id(value: str) -> str:
    """按 OBSERVABILITY §2.3 字面格式分流。

    ``req_<hex12>`` → ``"request_id"``;否则 → ``"trace_id"``。供
    ``GET /logs/trace/{id}`` 路由智能识别(§2.6 单 path 同时承载两种语义,
    §4.2 字面"GET /logs/trace/{request_id}" 是 §2.6 的复用扩展表达)。
    """
    return "request_id" if value.startswith("req_") else "trace_id"
