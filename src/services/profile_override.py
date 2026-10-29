"""单次写作 Profile 覆盖(P3-B-11 引入,P3-F-03 类 C 顺延)。

把 ``ChapterService.write`` / ``rewrite`` 入口的 ``profile_override`` 字符串
(由 ``:write`` / ``:rewrite`` 请求体 ``profile_override`` 字段透传)翻译为
router 层的 ``override_profile_id`` 参数,让本次写作的所有 agent 强制走
指定 profile。

设计要点(P3-F-03 启动审议决策 1 选 A:沿用 P3-B-07 wrap 模式)
================================================================

1. **5 agent / Orchestrator / ``_prompt.py`` 0 改动**:duck-type wrap,
   5 agent 通过 ``_prompt.py:resolve_routing`` 调
   ``router.route(agent_name, ...)`` 时,本 wrap 把内层
   ``inner.route(agent_name, override_profile_id=self._profile_override)``
   注入,raw :class:`ModelRouter.select_profile` 拿到 ``override_profile_id``
   后跳过 routing 规则 / agent_routing,直接 ``_lookup_profile(profile_id)``
   命中 override profile(P3-B-05 已预留 ``override_profile_id`` 参数)。
2. **wrap 顺序**:``_CostTrackingRouter`` (outer) → ``_ProfileOverrideRouter``
   (mid) → :class:`ModelRouter` (inner)。cost wrap 在外才能拿到已 override
   的 profile 计费——``RoutedTarget.profile`` 是 override profile,cost
   wrap 按其 ``cost_per_1k_*`` 计费,语义自洽。``ChapterService._orchestrate``
   构造时:::

       router = self._router_factory(workspace_id)
       router = wrap_router_with_profile_override(router, profile_override)
       if session_factory_in_position:
           router = wrap_router_with_cost_tracking(router, ...)

3. **校验时机**::class:`_ProfileOverrideRouter.__init__` 调
   :meth:`ModelRouter.has_profile` 前置校验——失败抛
   :class:`ProfileNotFoundError`,API 层 ``errors.py`` 翻译为 422
   ``CHAPTER_INVALID_PROFILE``。前置抛而非走到 LLM 调用时才抛 ``KeyError``,
   trace 干净 + 错误信息可定位非法 profile_id。
4. **profile_override=None 短路**::func:`wrap_router_with_profile_override`
   ``profile_override is None`` 时**返原 router 不 wrap**,避免空覆盖也叠
   一层无意义 wrap(同 P3-B-07 cost wrap 在 router=None 时不构造的设计)。

不做什么(P3-B-11 范围外)
--------------------------
- 持久化 ``profile_override`` 到 chapters / chapter_card(单次覆盖语义,
  P3-plan §F-2 字面"覆盖只对单次生效不持久化"),持久化归 P6 ConfigPage
  CRUD。
- :revise 路径加 ``profile_override``(P3-F-03 启动审议决策 3 选 B,留 P6
  完整 CRUD 时再评估;Reviser agent 一般用稳定 profile)。
"""

from __future__ import annotations

from typing import Any

from src.core.models.writer_profile import WriterProfile
from src.core.routing.router import ModelRouter, RoutedTarget


class ProfileNotFoundError(ValueError):
    """前置校验:``profile_override`` 引用的 ``profile_id`` 未在 config 声明。

    :class:`_ProfileOverrideRouter.__init__` 调
    :meth:`ModelRouter.has_profile` 校验失败时抛。API 层 ``errors.py``
    翻译为 422 ``CHAPTER_INVALID_PROFILE``,前端可据此引导用户从下拉里
    重选合法 profile。
    """


class WriterProfileNotFoundError(ValueError):
    """前置校验:章节卡 ``force_profile`` 字段引用的 ``profile_id`` 未在 config
    声明(P6-B-09 引入)。

    与 :class:`ProfileNotFoundError` 区分:后者是单次 request body
    ``profile_override`` 越界(transient,前端引导改下拉重发);本异常是章节卡
    持久化字段越界(用户必须先编辑章节卡 force_profile 字段才能再次 :write)。
    API 层 ``errors.py`` 翻译为 422 ``WRITER_PROFILE_NOT_FOUND``,details 含
    ``profile_id``(越界的具体 profile_id)+ ``known_profiles``(WriterRouter
    当前声明的全部 profile_id 列表,前端可据此提示用户)。

    抛点:::class:`src.services.chapter_service.ChapterService._orchestrate`
    在 wrap profile_override 之前 — 当 ``card.force_profile`` 非空且
    :meth:`ModelRouter.has_profile` 返 ``False`` 时显式抛(不依赖
    :class:`_ProfileOverrideRouter` wrap 入口抛的 :class:`ProfileNotFoundError`,
    避免与 request-body ``profile_override`` 越界翻译表混淆)。
    """


class _ProfileOverrideRouter:
    """ModelRouter duck-type wrap(P3-B-11)。

    与 :class:`src.services.cost_tracker._CostTrackingRouter` 同形态(P3-B-07
    wrap 模式 Rule of Three 第 3 处独立使用证据)。:meth:`route` /
    :meth:`select_profile` 把 caller 调用透传给内层 router,但**强制注入**
    ``override_profile_id=self._profile_override`` —— :class:`ModelRouter`
    内部 ``override_profile_id is not None`` 时短路 routing 规则 /
    agent_routing,直接 ``_lookup_profile(profile_id)`` 命中 override profile
    (P3-B-05 引入 ``override_profile_id`` 参数,本任务首次启用)。

    **不继承 ModelRouter**:只重写 :meth:`route` / :meth:`select_profile` /
    :meth:`has_agent_routing` 三方法,继承会污染 ``_config`` / ``_client_cache``
    字段语义。Python duck-type 在此场景天然支持(``_prompt.py`` 实际只用
    ``router.route()`` 接口,P3-B-08 ``reviewer.py`` 用 ``router.has_agent_routing``)。

    线程安全:与内层 router 一致(共享 ``_client_cache`` 等状态);本对象本身
    chapter-scoped(每次 ``ChapterService._orchestrate`` 构造)。
    """

    def __init__(self, inner: ModelRouter, profile_override: str) -> None:
        if not inner.has_profile(profile_override):
            raise ProfileNotFoundError(
                f"profile_id={profile_override!r} 未在 writer_profiles 声明"
            )
        self._inner = inner
        self._profile_override = profile_override

    def route(
        self,
        agent_name: str,
        *,
        chapter_card: Any = None,
        override_profile_id: str | None = None,
    ) -> RoutedTarget:
        # caller 传入的 override_profile_id 优先(实际场景不会发生:5 agent /
        # orchestrator / cost wrap 都不传);未传时强制注入
        # self._profile_override。
        effective = (
            override_profile_id
            if override_profile_id is not None
            else self._profile_override
        )
        return self._inner.route(
            agent_name,
            chapter_card=chapter_card,
            override_profile_id=effective,
        )

    def select_profile(
        self,
        agent_name: str,
        *,
        chapter_card: Any = None,
        override_profile_id: str | None = None,
    ) -> WriterProfile:
        effective = (
            override_profile_id
            if override_profile_id is not None
            else self._profile_override
        )
        return self._inner.select_profile(
            agent_name,
            chapter_card=chapter_card,
            override_profile_id=effective,
        )

    def has_agent_routing(self, agent_name: str) -> bool:
        """转发给内层 :meth:`ModelRouter.has_agent_routing`(P3-B-08 用)。

        ``agent_routing`` 由 raw config 决定,``profile_override`` 不影响
        Reviewer 升级路径(``reviewer_escalation``)的 disable / enable
        判定——边界审稿仍按 config 走升级判定,但升级目标 profile 也会被
        本 wrap 强制 override(语义自洽:override 只决定"用哪个 profile",
        不决定"是否升级")。
        """
        return self._inner.has_agent_routing(agent_name)

    def __getattr__(self, name: str) -> Any:
        """透明保留内层 router 的非拦截能力（例如 Provider cap 预检）。"""

        return getattr(self._inner, name)


def wrap_router_with_profile_override(
    router: ModelRouter,
    profile_override: str | None,
) -> ModelRouter | _ProfileOverrideRouter:
    """便捷构造器:``profile_override`` 非空时 wrap,否则返原 router。

    供 ``ChapterService._orchestrate`` 在 cost wrap **之前**调用——wrap
    顺序见模块 docstring §2。返回类型联合 :class:`ModelRouter` /
    :class:`_ProfileOverrideRouter`,但调用方可像 ``ModelRouter`` 一样
    传给 ``orchestrate_chapter(router=...)``(duck-type)。

    :param router: 原 ``ModelRouter``。**必须是 raw ModelRouter**,因为
        ``_ProfileOverrideRouter.__init__`` 要调 :meth:`ModelRouter.has_profile`
        前置校验;若已被其他 wrap 包过,wrap 类未必转发本方法。
    :param profile_override: ``profile_id`` 字符串,``None`` 时直接返
        ``router`` 不 wrap。``profile_id`` 不在 ``writer_profiles`` 声明时
        构造抛 :class:`ProfileNotFoundError`。
    """
    manifest_validator = getattr(router, "validate_profile_override", None)
    if callable(manifest_validator):
        # An active RunModelManifest owns the whole call graph.  Its persisted
        # token is accepted as a resume assertion, while every ordinary
        # profile override is an attempted single-agent escape and fails here.
        manifest_validator(profile_override)
        return router
    if profile_override is None:
        return router
    return _ProfileOverrideRouter(router, profile_override)
