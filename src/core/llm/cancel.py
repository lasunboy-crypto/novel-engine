"""LLM 调用取消信号(v1.1 wave 4 session 6 = J1)。

## 为何走 contextvar 而非显式参数透传

J1 plan-kickoff(候选 §J1 line 694/699)字面建议"``LLMClient.complete()`` 加
显式 ``cancel_event`` 参数 + 10+ caller 逐层透传"。w4-s6 实施期维护人拍板**翻案
为 contextvar 复用**(决策 1,详 ``docs/plan/v1_1_candidates.md`` §J1 实际实施
决策段):

- 显式透传要改 ``reviewer`` / ``must_achieve_validator`` / ``plot_thread_merger``
  / ``character_disambiguator`` / ``timeline_entry_merger`` 等 **20+ agent 函数
  签名 —— 这些正是 wave 1 治理 172 测覆盖的文件** → 主动引入治理回归面。
- contextvar 方案 **0 改动这 20+ agent**:在后台任务入口(``_run_*_task``)绑定
  cancel ``threading.Event``,3 个 client 从 contextvar 读 → 治理零回归 by
  construction。
- 复用代码既有模式:3 个 client 已从 ``structlog.contextvars`` 读 ``workspace_id``
  (见 ``claude_client.py`` 入口),本模块是同套路延伸。

## 取消信号真源 vs 传递

- **真源**:``threading.Event`` 存 ``src/api/task_registry.py`` 的进程级 dict
  (``task_id -> Event``,与 ``_destroyed_workspaces`` 同模式)。cancel 请求线程
  调 ``task_registry.cancel_task(task_id)`` → ``event.set()``。
- **传递**:后台 worker 线程入口 ``with bind_cancel_event(...)`` 把该 Event 绑进
  本模块 ContextVar;client 调 ``raise_if_cancelled()`` 读 ContextVar 查
  ``event.is_set()``。
- cancel 请求线程与 worker 线程是**不同线程 / 不同 context**,故必须经 dict 真源
  共享 Event 引用;ContextVar 只负责"把 Event 从 worker 入口递到深层 client",
  不跨线程传播。

## 中断语义("尽力中断")

cancel 是 chunk 间 / 调用前的**协作式检查**,非抢占式:

- ``OpenAICompatibleClient``(SSE 流式,dashscope qwen 真实云路径):每个 SSE
  chunk 间查一次 → 真中断正在跑的调用、立即省 token。
- ``ClaudeClient`` / ``OllamaClient``(单次阻塞调用):仅阻塞调用前 pre-flight
  查一次 → 命中多 agent 流水线**串行调用间隙**中断(claude 维护人无 key 跑
  mock、ollama P3 本地非当前热路径,真 chunk 中断推 P3,详候选 §J1 留观债)。

## 线程池 reset 必要性

Starlette ``BackgroundTasks`` 把 sync 函数派发到 anyio threadpool,**线程会被
跨任务复用**。若只 set 不 reset,上一个任务的 cancel Event 会泄漏给复用同线程
的下一个任务 → 误中断。故 ``bind_cancel_event`` 用 contextmanager,``finally``
``reset`` token(对齐 ``bound_contextvars`` 退出清理语义)。
"""

from __future__ import annotations

import threading
from collections.abc import Iterator
from contextlib import contextmanager
from contextvars import ContextVar, Token


class LLMCancelledError(BaseException):
    """LLM 调用被用户取消信号中断(``cancel_event.is_set()`` 为真)。

    **为何继承 ``BaseException`` 而非 ``Exception``**(对齐 ``asyncio.CancelledError``
    Py3.8+ 的同款设计):取消信号**必须穿透**沿途所有 ``except Exception``。本项目
    热路径(``chapter_orchestrator`` / validators / extractors)有大量 fail-open /
    swallow 的 ``except Exception``(单个 agent / validator LLM 报错时 fail-open 不
    中断章节)—— 若取消异常被这些块吞掉,流水线会**继续往下跑下一个 agent**,
    cancel 在流水线中途根本不生效。继承 ``BaseException`` 让取消信号绕过所有
    ``except Exception``,直达后台任务入口的 ``bind_cancel_event`` 包装层翻
    ``status="cancelled"``。``with`` / ``finally`` 清理(DB session、httpx 连接)
    在 ``BaseException`` 传播时仍正常执行,无资源泄漏。

    与 ``failed``(业务失败)/ ``interrupted``(后端重启中断)显式区分。不可重试
    (``_retry`` 的 ``except Exception`` 天然不捕获本异常)。

    **测试注意**:断言取消用 ``pytest.raises(LLMCancelledError)``,不能用
    ``pytest.raises(Exception)``(BaseException 子类不被 Exception 捕获)。
    """


# worker 线程入口绑定的 cancel Event;None → 未绑定(单测直接调 client /
# 同步执行路径未经后台任务入口),此时 ``raise_if_cancelled`` 静默放过。
_cancel_event_var: ContextVar[threading.Event | None] = ContextVar(
    "llm_cancel_event", default=None
)


def current_cancel_event() -> threading.Event | None:
    """读当前 context 绑定的 cancel Event;未绑定 → None。"""
    return _cancel_event_var.get()


def is_cancelled() -> bool:
    """当前 context 是否已收到取消信号。未绑定 cancel Event → False。"""
    event = _cancel_event_var.get()
    return event is not None and event.is_set()


def raise_if_cancelled() -> None:
    """若当前 context 已收到取消信号,抛 ``LLMCancelledError``。

    ``event.is_set()`` 是 atomic bool 读(非网络 / 非锁操作),O(1) 开销;
    client 每个 SSE chunk 间调用无性能问题,无需采样降频(决策 3)。
    """
    if is_cancelled():
        raise LLMCancelledError("LLM 调用被取消信号中断")


@contextmanager
def bound_cancel_event(event: threading.Event | None) -> Iterator[None]:
    """把 cancel Event 绑进 context(``with`` 块内 client 可见),退出自动 reset。

    ``event=None`` 也照常 set/reset(语义透明:显式清空当前 context 的 cancel
    Event,防线程池复用泄漏)。
    """
    token: Token[threading.Event | None] = _cancel_event_var.set(event)
    try:
        yield
    finally:
        _cancel_event_var.reset(token)


__all__ = [
    "LLMCancelledError",
    "bound_cancel_event",
    "current_cancel_event",
    "is_cancelled",
    "raise_if_cancelled",
]
