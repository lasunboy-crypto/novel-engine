"""每次 LLM 调用的成本累加(P3-B-07 引入)。

把 ``LLMResponse.usage`` × ``WriterProfile.cost_per_1k_*`` 的成本累加到
``chapters.total_cost_usd``,把 ``input_tokens`` / ``output_tokens`` 按
``model_used`` 维度累加到 ``chapters.total_token_usage``(JSON dict)。

设计要点(P3-B-07 启动审议选项 A:ChapterService 包装 router)
==============================================================

1. **5 agent / Orchestrator / ``_prompt.py:resolve_routing`` 0 改动**:
   ChapterService 在 ``_orchestrate`` / ``revise`` 拿到 router 后再外包一层
   :class:`_CostTrackingRouter` —— 它的 ``route()`` 把 ``RoutedTarget.client``
   替换成 :class:`CostTrackingClient`(wraps inner ``complete`` + 算 cost +
   调 recorder callback)。``RoutedTarget.profile`` / ``complete_kwargs`` 字面
   不变,5 agent 拿到的 ``client`` duck-type 仍是 ``LLMClient``,
   ``client.complete(...)`` 调用形态保持不变。
2. **legacy 路径自动无 cost**:``router=None`` 时 ChapterService 不构造
   :class:`_CostTrackingRouter`,5 agent 拿原 ``self._client`` 调用,
   cost_tracker 完全不参与(legacy 是 P1/P2 测试兼容,无 profile 来源,
   无法算 cost,这与"维护人无 API key,P3 主链路全走 router"约束一致)。
3. **mock 路径自动 cost=0**:``mock_default`` profile 的 ``cost_per_1k_*=0.0``
   → :func:`compute_cost` 返 0.0;chapters 表 ``total_cost_usd`` 累加 0
   不前进,但 ``total_token_usage`` 仍按 ``model_used`` 维度记录调用次数 ×
   token 量(供诊断"mock 路径单章 token 量级"等场景)。维护人本机 P3
   默认 LLM_PROVIDER=mock,cost_usd 字段恒 0,与"维护人本机 cost 不可见"
   约束对齐。
4. **chapters 表写入边界**:每次 LLM 调用产一次短 session(由 caller
   注入的零参 ``session_factory()`` 开),读 chapter row → update 累加 →
   commit → close。**单次写**比积攒在内存里再一次性 commit 多了 N 次
   commit,但单章总 LLM 调用 3-5 次(write + reviewer + 0/1 reviser + 0/1
   extract),开销可忽略;换来"中途 raise 时已发生的 cost 不丢"+"实时
   可被前端 GET /chapters/{id} 看到累加进度"。
5. **chapter row 不存在时静默跳过**:理论上 ChapterService._orchestrate
   入口 ``_upsert_chapter_state`` 已建 row,但 :meth:`ChapterService.revise`
   未调入口 upsert(由 caller / orchestrator 流程上必先 write 才能 revise,
   chapters 行先于 revise 产生)。极端"已有 v1.md 但 chapters 行被外部删除
   后再 revise"场景下,recorder 只落 structlog warn 不抛(章节版本仍正常
   写新版本,只是 cost 累加丢这一次)。

不做什么(P3-B-07 范围外,留 P4 OBSERVABILITY)
-----------------------------------------------
- ``metrics`` 表(每次 LLM 调用一行明细)留 P4(P3-plan 决策 5 选 A);
  本任务只在 chapters 表上累加聚合值。
- LLM 调用本身的失败重试在 ``ClaudeClient`` / ``OllamaClient`` 内部做。
  **cost 写 SQLite 的 lock 重试**(v1.1 w4 B1 Y1 缺陷 C 双保险,推翻原"不重试"
  决策):cost UPDATE(``chapters`` 行)与 extract 阶段 persist 写并发会撞写锁。
  **真根因(2026-06-03 ch-15~18 实证更正初诊)非 ``SQLITE_BUSY_SNAPSHOT`` 瞬时
  竞争,而是 extract 长事务横跨 merger LLM 调用持写锁 40s+**——已由
  ``memory_extractor.extract`` 两段式重排治本(Phase 1 全 merge LLM 不写库 →
  Phase 2 集中 persist,写锁持有降到几十 ms)。本模块
  :func:`_record_chapter_cost_with_retry` 在 ``_COST_WRITE_LOCK`` 临界区内对 lock
  错误重试(每次新 session 重新 read-modify-write,无 lost-update),降级为
  **双保险**:cover 治本后残余的 Phase 2 集中写 + Chroma 写窗口(几十~几百 ms)
  内偶发竞争;预算耗尽才上抛(由 :class:`CostTrackingClient` swallow + warn)。
  详见 ``docs/acceptance/luo-clean-v2-digest.md`` Y1 缺陷 C 条目。
- ``token`` 维度的 prompt cache hit / miss 区分(Claude 4.5+ 才暴露);
  P3 阶段 ``LLMResponse.usage`` 只含 ``input_tokens`` / ``output_tokens``,
  本模块同步只累加这两维。
"""

from __future__ import annotations

import threading
import time
from collections.abc import Callable
from typing import Any

import structlog
from sqlalchemy.exc import OperationalError
from sqlalchemy.orm import Session

from src.core.db.models import ChapterORM
from src.core.llm.base import LLMClient, LLMResponse, Message, TokenUsage
from src.core.models.writer_profile import WriterProfile
from src.core.routing.router import ModelRouter, RoutedTarget

_log = structlog.get_logger(__name__)


_COST_WRITE_LOCK = threading.Lock()
"""进程内串行锁:序列化所有 chapter cost 累加写(Y1 治本,v1.1 w4 B1)。

**根因**:write 流程收尾期多个后台 agent(memory_extractor / character_disambiguator
/ *_merger / chapter_summary_extractor)各被 cost-tracking 包装,每次 LLM 调用结束
都触发本模块 recorder 对**同一 chapter row** ``SELECT…UPDATE``。多连接并发的
DEFERRED 读快照升级写在 WAL 下撞 ``SQLITE_BUSY_SNAPSHOT``(``busy_timeout`` 不
重试、立即抛 "database is locked"),且 read-modify-write 本身有 lost-update 竞态。

**为何用进程内锁而非 DB 层 BEGIN IMMEDIATE**:cost 写全部发生在单个 write 进程内
(driver / API 单 worker),进程内串行即可消除"并发"这一根因触发条件 → 无 snapshot
冲突、无 lost-update;相比全局 ``BEGIN IMMEDIATE``(会令 ``PRAGMA foreign_keys`` 在
事务内 no-op、破坏 FK 测试)零全局副作用。临界区是"读 row→累加→commit",仅几 ms,
串行化不影响吞吐。**边界(诚实标注)**:仅覆盖同进程;跨进程并发写同一 chapter row
不在此锁范围——但本地单机引擎不同进程写的是不同章(不同 row),WAL+busy_timeout 足够。

**缺陷 C 真根因 + 治本(v1.1 w4 B1 ch-15~18 实证,2026-06-03 更正初诊)**:本锁只
序列化 cost-cost 写,覆盖不到 cost 写 vs extract 阶段 persist 写的并发。**初诊误判
为 ``SQLITE_BUSY_SNAPSHOT`` 瞬时竞争 → retry-on-lock 兜底,但 B1 长跑 ch-15~18
retry 全失败**(每次 attempt 间隔 5s=busy_timeout 等满,印证是普通写锁长占用而非
SNAPSHOT 立即抛)。**真根因 = ``memory_extractor.extract`` 原"merge LLM 与 persist
交错"实现让 extract 长 session 首次 persist 即获写锁,之后 plot_thread_merger /
timeline_entry_merger 的 thinking LLM(各 ~20s)在持锁状态下进行 → 写锁被占 40s+,
cost 写整个 retry 窗口都抢不到。** 已由 ``memory_extractor.extract`` 两段式重排治本
(Phase 1 全 merge LLM 不写库 → rollback 隔断读快照 → Phase 2 集中 persist,写锁
持有降到几十 ms)。retry-on-lock 降级为双保险,cover 残余 Phase 2 + Chroma 写窗口。
"""

# Y1 缺陷 C retry-on-lock 双保险参数(v1.1 w4 B1):治本后 cost 写撞锁窗口降到
# extract Phase 2 集中写 + Chroma 的几十~几百 ms,线性递增 backoff 累计上限
# 0.05*(1+…+9)≈2.25s 足够覆盖;仍耗尽则说明非瞬时竞争(真异常),上抛由
# CostTrackingClient swallow+warn。
_COST_WRITE_MAX_ATTEMPTS = 10
_COST_WRITE_RETRY_BASE_SECONDS = 0.05


CostRecorder = Callable[[WriterProfile, LLMResponse, float], None]
"""每次 LLM 调用后的 callback 签名。

参数:
    profile:本次调用对应的 ``WriterProfile``(由路由决定)
    response:LLM 返回的 ``LLMResponse``,含 ``usage`` + ``model_used``
    cost_usd:本次调用算出的 USD 成本(已通过 :func:`compute_cost` 计算)

实现方应**不抛异常**(失败请落 structlog;cost 写库故障不应让 LLM 调用整
体失败)。"""


def compute_cost(profile: WriterProfile, usage: TokenUsage) -> float:
    """按 1000 token 单价表算单次 LLM 调用的 USD 成本。

    公式:``(input_tokens / 1000) * cost_per_1k_input +
    (output_tokens / 1000) * cost_per_1k_output``。

    本地 ollama profile 配置 ``cost_per_1k_*=0.0`` → 返 0.0;mock profile
    同样 0.0 → 返 0.0。Claude 等云端 profile 走配置表给定单价。

    :param profile: WriterProfile;若 ``cost_per_1k_*`` 任一未配置(配置层
        已默认 0.0),按 0 计。
    :param usage: ``LLMResponse.usage``,``TokenUsage`` 含 ``input_tokens``
        与 ``output_tokens``。
    :return: USD 成本浮点值(可能为 0.0)。
    """
    return (
        usage.input_tokens / 1000.0 * profile.cost_per_1k_input
        + usage.output_tokens / 1000.0 * profile.cost_per_1k_output
    )


class CostTrackingClient(LLMClient):
    """LLMClient 装饰器:wraps inner client + profile + recorder。

    每次 :meth:`complete` 调用:

    1. 透传给 ``inner.complete(messages, **gen_params)`` 拿 ``LLMResponse``。
    2. 调 :func:`compute_cost` 算 cost。
    3. 调 ``recorder(profile, response, cost)``;recorder 写 chapters 表。
       recorder 抛异常时**不向上冒泡**(只记 structlog warn 兜底):cost 累
       加是辅助任务,LLM 响应已经拿到不该让章节流水线整体失败。
    4. 返 ``LLMResponse`` 给 caller(5 agent),agent 不感知 cost 路径。

    构造由 :class:`_CostTrackingRouter` 包装时一次性完成,5 agent 不主动
    构造本类。
    """

    def __init__(
        self,
        inner: LLMClient,
        profile: WriterProfile,
        recorder: CostRecorder,
    ) -> None:
        self._inner = inner
        self._profile = profile
        self._recorder = recorder

    def prepare_strict_json_schema(
        self,
        *,
        model: str,
        schema_name: str,
        schema: dict[str, Any],
        gen_params: dict[str, Any],
    ) -> dict[str, Any]:
        """透传 inner 的结构化输出能力，包装层不得静默降级请求合同。"""

        return self._inner.prepare_strict_json_schema(
            model=model,
            schema_name=schema_name,
            schema=schema,
            gen_params=gen_params,
        )

    def complete(
        self,
        messages: list[Message],
        **gen_params: Any,
    ) -> LLMResponse:
        response = self._inner.complete(messages, **gen_params)
        try:
            cost = compute_cost(self._profile, response.usage)
            self._recorder(self._profile, response, cost)
        except Exception as exc:  # noqa: BLE001 - 本层 swallow,LLM 响应已拿到
            _log.warning(
                "cost_tracker_recorder_failed",
                profile_id=self._profile.profile_id,
                model_used=response.model_used,
                error_type=type(exc).__name__,
                error_message=str(exc),
            )
        return response

    def __getattr__(self, name: str) -> Any:
        """Keep provider-specific non-observation capabilities visible.

        The central manifest client owns the exact bounded non-thinking wire
        used by word-count/anti-tic trim.  Cost tracking records the resulting
        response but must not hide that capability and make production fall
        back to legacy provider kwargs.
        """

        return getattr(self._inner, name)


class _CostTrackingRouter:
    """ModelRouter duck-type 包装(P3-B-07)。

    实现 :meth:`route` 方法签名与 :class:`ModelRouter.route` 一致,5 agent
    通过 ``_prompt.py:resolve_routing`` 调 ``router.route(agent_name, ...)``
    时拿到的 ``RoutedTarget.client`` 已经是 :class:`CostTrackingClient`。

    **不继承 ModelRouter**:只重写一个方法,继承反而要 super().__init__()
    复制 config + client_factory + 缓存,污染清晰度。Python duck-type 在此
    场景天然支持(``_prompt.py`` 实际只用 ``.route()``)。

    线程安全:与内层 router 一致(共享 ``_client_cache``);recorder 闭包
    持有 chapter_id 等 chapter-scoped 状态,本对象本身也是 chapter-scoped
    (每次 ``ChapterService._orchestrate`` / ``revise`` 重新构造)。
    """

    def __init__(self, inner: ModelRouter, recorder: CostRecorder) -> None:
        self._inner = inner
        self._recorder = recorder

    def has_agent_routing(self, agent_name: str) -> bool:
        """转发给内层 :meth:`ModelRouter.has_agent_routing`。

        Reviewer 升级路径(P3-B-08)需通过 router 检查"是否显式配置
        ``reviewer_escalation``";cost wrap 后调 ``router.has_agent_routing``
        会进本方法,内部透传给原 router,语义不变。
        """
        return self._inner.has_agent_routing(agent_name)

    def select_profile(
        self,
        agent_name: str,
        *,
        chapter_card: Any = None,
        override_profile_id: str | None = None,
    ) -> WriterProfile:
        """转发给内层 :meth:`ModelRouter.select_profile`(v2 W3.5-s7 端到端验收修复)。

        ``ChapterService._calibration_default_trim`` 用
        ``router.select_profile("writer").model`` 查 writer 模型校准决定 trim 三态默认。
        本 duck-type 包装原只重写 ``route`` + ``has_agent_routing``,**漏代理
        ``select_profile``** → 生成期(``_orchestrate`` 把 router cost-wrap 后)该调用抛
        ``AttributeError``,被 ``_calibration_default_trim`` 的 try/except 吞 → trim /
        anti-tic 校准联动**生产生成期静默退默认关**(curl + 单元测都走未 cost-wrap 的
        router,漏了这条生成期路径;W3.5-s7 端到端验收抓到)。透传内层即修复。
        """
        return self._inner.select_profile(
            agent_name,
            chapter_card=chapter_card,
            override_profile_id=override_profile_id,
        )

    def route(
        self,
        agent_name: str,
        *,
        chapter_card: Any = None,
        override_profile_id: str | None = None,
    ) -> RoutedTarget:
        target = self._inner.route(
            agent_name,
            chapter_card=chapter_card,
            override_profile_id=override_profile_id,
        )
        wrapped_client = CostTrackingClient(
            target.client, target.profile, self._recorder
        )
        return RoutedTarget(
            profile=target.profile,
            client=wrapped_client,
            complete_kwargs=target.complete_kwargs,
        )

    def __getattr__(self, name: str) -> Any:
        """透明保留内层 router 的非拦截能力（例如 Provider cap 预检）。"""

        return getattr(self._inner, name)


def make_chapter_recorder(
    *,
    workspace_id: str,
    chapter_id: str,
    session_factory: Callable[[], Session],
) -> CostRecorder:
    """构造 chapter-scoped 的 cost recorder 闭包。

    每次 LLM 调用:打开短 session → 读 ``ChapterORM`` row(``chapter_id``
    定位)→ 累加 ``total_cost_usd`` + ``total_token_usage[model_used]`` →
    commit → close。

    chapter row 不存在场景(理论上不会:_orchestrate 入口 upsert 已建;
    revise 路径前序 write 已建)只落 structlog warn,不抛——保持 cost
    累加是辅助任务的 fail-soft 性质。

    ``total_token_usage`` 累加规则
    --------------------------------
    JSON dict,key = ``model_used`` 字符串(``"mock-default"`` /
    ``"claude-sonnet-4-6"`` / ``"qwen-novel"`` 等),value = ``{"input_tokens":
    int, "output_tokens": int}``。同 model 多次累加在同一 key 下,不同 model
    各自分桶。**重新赋值整个 dict**(SQLAlchemy JSON 列默认非 mutable-tracked,
    in-place ``dict[k] = v`` 不触发 dirty 标记;单测会发现 commit 后值不变)。

    :param workspace_id: 工作区 ID,recorder 内部校验 chapter row 的
        ``workspace_id`` 与之一致(防御:chapter_id 跨工作区碰撞)。
    :param chapter_id: 章节 ID,recorder 写库的目标 row。
    :param session_factory: 零参 session 工厂(``ChapterService`` 把"按
        workspace_id 开 session"包成零参再注入)。每次调用产新 session,
        recorder 内部 ``close()``。
    :return: ``CostRecorder``,签名 ``(profile, response, cost) -> None``。
    """

    def _recorder(
        profile: WriterProfile,
        response: LLMResponse,
        cost: float,
    ) -> None:
        # 进程内串行:消除并发 read-modify-write 同行的 SNAPSHOT 冲突 + lost-update。
        # 锁内再对 cost-vs-extract 并发的瞬时 lock 错误重试(缺陷 C 治本)。
        with _COST_WRITE_LOCK:
            _record_chapter_cost_with_retry(
                workspace_id=workspace_id,
                chapter_id=chapter_id,
                session_factory=session_factory,
                profile=profile,
                response=response,
                cost=cost,
            )

    return _recorder


def _is_sqlite_lock_error(exc: OperationalError) -> bool:
    """判断是否 SQLite 写锁错误(``database is locked`` / ``busy``)。

    ``SQLITE_BUSY`` / ``SQLITE_BUSY_SNAPSHOT`` 经 SQLAlchemy 包成
    ``OperationalError``,消息含 "database is locked" / "database is busy"。
    只对这类瞬时锁竞争重试;其他 ``OperationalError``(schema / 约束等真错误)
    立即上抛,不被重试掩盖。
    """
    msg = str(exc).lower()
    return "database is locked" in msg or "database is busy" in msg


def _record_chapter_cost_with_retry(
    *,
    workspace_id: str,
    chapter_id: str,
    session_factory: Callable[[], Session],
    profile: WriterProfile,
    response: LLMResponse,
    cost: float,
) -> None:
    """cost 写 + SQLite 锁错误重试(Y1 缺陷 C 治本,在 ``_COST_WRITE_LOCK`` 内调用)。

    每次重试都新开 session 重新 ``get → 累加 → commit``,读到的是最新行值,故
    **无 lost-update**(retry 不是简单重放旧值);extract 阶段的并发 persist 写
    落定后,重试即成功。重试 ``_COST_WRITE_MAX_ATTEMPTS`` 次仍撞锁才上抛(由
    :class:`CostTrackingClient` swallow + warn);非锁类错误立即上抛。
    """
    for attempt in range(1, _COST_WRITE_MAX_ATTEMPTS + 1):
        try:
            _record_chapter_cost_once(
                workspace_id=workspace_id,
                chapter_id=chapter_id,
                session_factory=session_factory,
                profile=profile,
                response=response,
                cost=cost,
            )
            if attempt > 1:
                _log.info(
                    "cost_tracker_write_succeeded_after_retry",
                    workspace_id=workspace_id,
                    chapter_id=chapter_id,
                    attempts=attempt,
                )
            return
        except OperationalError as exc:
            if (
                not _is_sqlite_lock_error(exc)
                or attempt == _COST_WRITE_MAX_ATTEMPTS
            ):
                raise
            _log.debug(
                "cost_tracker_write_lock_retry",
                workspace_id=workspace_id,
                chapter_id=chapter_id,
                attempt=attempt,
                max_attempts=_COST_WRITE_MAX_ATTEMPTS,
            )
            time.sleep(_COST_WRITE_RETRY_BASE_SECONDS * attempt)


def _record_chapter_cost_once(
    *,
    workspace_id: str,
    chapter_id: str,
    session_factory: Callable[[], Session],
    profile: WriterProfile,
    response: LLMResponse,
    cost: float,
) -> None:
    """单次 cost 累加写(在 ``_COST_WRITE_LOCK`` 临界区内调用)。"""
    session: Session = session_factory()
    try:
        row = session.get(ChapterORM, chapter_id)
        if row is None:
            _log.warning(
                "cost_tracker_chapter_row_missing",
                workspace_id=workspace_id,
                chapter_id=chapter_id,
                profile_id=profile.profile_id,
                model_used=response.model_used,
            )
            return
        if row.workspace_id != workspace_id:
            _log.warning(
                "cost_tracker_workspace_mismatch",
                workspace_id=workspace_id,
                chapter_id=chapter_id,
                row_workspace_id=row.workspace_id,
            )
            return

        # 该列的历史公开合同明确是 USD。非 USD profile 的配置估算由
        # AttemptLedger 按原币种权威保存，不能再伪装成美元写进章节投影。
        if profile.cost_currency == "USD":
            row.total_cost_usd = float(row.total_cost_usd) + float(cost)

        existing = dict(row.total_token_usage or {})
        bucket = dict(existing.get(response.model_used, {}))
        bucket["input_tokens"] = int(bucket.get("input_tokens", 0)) + int(
            response.usage.input_tokens
        )
        bucket["output_tokens"] = int(bucket.get("output_tokens", 0)) + int(
            response.usage.output_tokens
        )
        existing[response.model_used] = bucket
        row.total_token_usage = existing

        session.commit()
    except Exception:
        session.rollback()
        raise
    finally:
        session.close()


def wrap_router_with_cost_tracking(
    router: ModelRouter,
    *,
    workspace_id: str,
    chapter_id: str,
    session_factory: Callable[[], Session],
) -> _CostTrackingRouter:
    """便捷构造器:从 ``(router, workspace_id, chapter_id, session_factory)``
    一次性产 cost-tracking router。

    供 ``ChapterService._orchestrate`` / ``revise`` 调用。返回类型注解为
    ``_CostTrackingRouter``,但调用方可像 ``ModelRouter`` 一样直接传给
    ``orchestrate_chapter(router=...)`` / ``revise_chapter(router=...)``
    (duck-type)。

    若上游希望禁用 cost tracking(测试 / 特殊场景),不调本函数即可,直接
    传原 ``ModelRouter``。
    """
    recorder = make_chapter_recorder(
        workspace_id=workspace_id,
        chapter_id=chapter_id,
        session_factory=session_factory,
    )
    return _CostTrackingRouter(router, recorder)
