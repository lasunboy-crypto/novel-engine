"""ModelRouter(P3-B-05 引入)。

把 agent 调用映射到具体 ``WriterProfile`` + ``LLMClient``。包装三件事:

1. **Profile 选择**:Writer 走 ``RoutingConfig.rules`` 顺序匹配(规则字符串
   走 :func:`evaluate_rule` 受限 AST 求值);其他 agent 走 ``agent_routing``
   表;均回退到 ``default``。
2. **LLMClient 构造与缓存**:同一 ``(provider, model, base_url)`` 组合复用
   一个 client 实例,避免每次 agent 调用都走 ``factory.make_llm_client``
   (ClaudeClient SDK 内部维护 httpx 连接池;OllamaClient 维护 ``httpx.Client``;
   重复构造既浪费资源也丢复用机会)。
3. **``extra_request_kwargs`` 透传与 ``think`` 翻译**:把 ``WriterProfile``
   的 ``generation_params`` + ``extra_request_kwargs`` 合并成可直接 ``**``
   到 ``LLMClient.complete`` 的 dict;**仅 OllamaClient 路径**把 ``think``
   字段从 dict 中 pop 出来翻译为 ``enable_thinking`` 显式 keyword(对齐
   P3-B-04 impl §6 marker:避免 ``OllamaClient.complete`` 把 ``think``
   当 ``gen_params`` 误塞 ollama ``options`` dict)。

provider 派发(post-wave5 H1 routing completion 起按 ``profile.provider``)
==========================================================================

**Router 在 ``get_client`` 把 ``profile.provider`` 显式传给工厂**,工厂按该
参数派发 client 类(``MockLLMClient`` / ``ClaudeClient`` / ``OllamaClient``
/ ``OpenAICompatibleClient``)。``settings.llm_provider`` 全局开关在新路径
下退化为"工厂缺省 fallback"(cli/commands 等 bypass Router 路径用),不再
是 Router 的硬约束。

历史路径(P1-B-22 → post-wave5 H1 第一阶段)
--------------------------------------------

工厂只看 ``settings.llm_provider``,与 ``WriterProfile.provider`` 字面值解耦。
单一 provider 时 OK;多 provider 共存(本地 ollama + 云端 dashscope 同时
跑)时 break — Router 选中云端 profile 后,工厂仍按全局开关构造本地 client。

post-wave5 H1 routing completion 修法
---------------------------------------

工厂签名扩展为 ``make_llm_client(model, base_url, provider=None)``;Router
``get_client`` 显式传 ``provider=profile.provider``。**真正的多 provider
共存路径**由此打通 — luo3 工作区跑本地 qwen 同时 luo-cloud-test 跑云端
dashscope qwen3.8-max,共存零冲突,无需切 ``LLM_PROVIDER`` env。

provider-unavailable fallback(v1.1 wave 2 H2 重写自 P7-B-06.5)
================================================================

**``LLM_PROVIDER != "mock"`` 时**,Router 在 ``select_profile`` 末尾插入
provider-unavailable 守护:**命中 profile 的 provider 对应 API key 不可用**
(未配 key / 本地服务无需 key)时,fallback 到 ``writer_routing.default``
profile(若 default 也不可用,保留原 profile 让 client 自然抛错)。

**v1.1 wave 2 H2 之前(P7-B-06.5)** trigger 是 ``settings.llm_provider``
全局开关:profile.provider != settings 即 fallback。此约束让"真混跑
provider"(本地 ollama memory_extractor + 云端 dashscope writer 同进程)
不可达 — agent_routing 内本地 profile 总被 silent 升级到 default 云端。

**v1.1 wave 2 H2 重写** trigger 改为按 API key 可用性查询(详
:func:`_is_provider_usable`):

- 维护人本地用户场景(无 claude key + arc_finale rule 命中 cloud_premium):
  cloud_premium.provider=claude → key 未配 → 不可用 → fallback 到 default
  (local_qwen_32k 或当前 cloud_qwen_max_medium_creative,需配齐对应 key)
- 维护人云端用户场景(dashscope key 配齐 + memory_extractor=local_qwen_safe):
  local_qwen_safe.provider=ollama → 本地总可用 → **不 fallback,真走本地** ✅
  (旧逻辑下被 silent 升级到 default 云端,**真混跑解锁** = v1.1 wave 2 H2
  核心收益)

**override 路径不走 fallback**:维护人显式 ``force_profile``
(P3-B-11)即明确意图,失败由 LLMClient 报错让上层定位非法配置。

**mock 路径不走 fallback**:单测约定 profile.provider 字面与 settings
解耦(MockLLMClient 不消费 model)。
"""

from __future__ import annotations

from collections.abc import Callable
from threading import Lock
from typing import Any, NamedTuple, Protocol, runtime_checkable

from src.core.llm.base import LLMClient
from src.core.llm.factory import make_llm_client
from src.core.llm.ollama_client import OllamaClient
from src.core.llm.openai_compatible_endpoint import (
    resolve_openai_compatible_endpoint,
)
from src.core.models.routing import (
    LLM_AS_JUDGE_AGENTS,
    WriterProfilesConfig,
    is_thinking_on_profile,
)
from src.core.models.writer_profile import WriterProfile
from src.core.routing.rule_eval import evaluate_rule


@runtime_checkable
class ProviderAttemptCapacity(Protocol):
    """Router 的可选只读能力：并发批能否容纳最坏物理 Provider 尝试数。

    hard cap 的最终所有权仍在 AttemptLedger 原子 ``begin``；本协议只让并发
    调度器在创建 future 之前选择并行或固定串行。任何 router wrapper 都应通过
    通用 delegation 保留这类非拦截能力。
    """

    def can_afford_provider_attempts(self, required_attempts: int) -> bool:
        """返回当前 attempt scope 是否至少还容得下 ``required_attempts``。"""


def _is_provider_usable(provider: str, base_url: str | None = None) -> bool:
    """判 provider 是否运行时可用(v1.1 wave 2 H2 引入)。

    用于 :meth:`ModelRouter._maybe_fallback_for_unavailable_provider` 取代
    旧版 ``settings.llm_provider`` 全局开关判断,**解锁真混跑 provider**
    (同进程 agent_routing 不同 agent 走不同 provider)。

    语义
    ----
    - ``mock`` / ``ollama``:本地路径,总可用(连不通时 client 自然报错给
      上层定位)。ollama 是本地 HTTP 端点,不需要 API key
    - ``claude``:需 ``settings.claude_api_key`` 非空
    - ``openai_compatible``:先按 profile ``base_url`` 的规范 hostname
      解析唯一 credential authority，再检查该唯一 key。官方 endpoint 只认
      专用 key；未知兼容 endpoint 只认 generic key；URL 含糊时不可用
    - 未知 provider:返 ``True``,让 ``LLMClient`` 工厂自然抛"unknown
      provider"错(由 :func:`src.core.llm.factory.make_llm_client` 处理),
      避免本函数变 fail-closed 阻塞未来新 provider 接入

    与 ``ApiKeyService`` 的关系
    ----------------------------
    本函数直接读 ``settings.<provider>_api_key``,与 ``ApiKeyService.put``
    原地刷新 settings 字段路径自然同步 — 用户在 UI 上 PUT 新 key 后立即
    被本函数感知,无需 cache invalidate。
    """
    from src.config import settings

    if provider in ("mock", "ollama"):
        return True
    if provider == "claude":
        return bool(settings.claude_api_key.get_secret_value())
    if provider == "openai_compatible":
        if base_url is None:
            return False
        try:
            endpoint = resolve_openai_compatible_endpoint(base_url)
        except ValueError:
            return False
        api_key = getattr(settings, endpoint.settings_field)
        return bool(api_key.get_secret_value())
    return True


class RoutedTarget(NamedTuple):
    """路由结果三元组,供 agent 直接消费。

    使用范式(P3-B-06 agent 改造后)::

        target = router.route("writer", chapter_card=card)
        response = target.client.complete(messages, **target.complete_kwargs)

    ``complete_kwargs`` 已包含 ``profile.generation_params`` + 翻译后的
    ``extra_request_kwargs``;**不含** ``model``——client 构造时已绑定
    ``profile.model``,``LLMClient.complete`` 内部默认会用。
    """

    profile: WriterProfile
    client: LLMClient
    complete_kwargs: dict[str, Any]


class ModelRouter:
    """模型路由器(P3-B-05 引入)。

    Args:
        config: 已通过 ``WriterProfilesConfig`` 交叉校验的 profile + routing
            配置。通常由 ``services.config_service.load_workspace_config``
            合并引擎默认 + 工作区 ``project.yaml`` 后产出。
        client_factory: ``LLMClient`` 工厂函数(签名
            ``(model: str, base_url: str | None) -> LLMClient``)。默认走
            ``src.core.llm.factory.make_llm_client``;测试可注入 stub
            观察缓存命中。
    """

    def __init__(
        self,
        config: WriterProfilesConfig,
        *,
        client_factory: Callable[..., LLMClient] | None = None,
    ) -> None:
        self._config = config
        self._client_factory = client_factory or make_llm_client
        # 缓存 key:``(provider, model, base_url)``。``provider`` 取自
        # ``WriterProfile.provider`` 字面值,与全局 ``settings.llm_provider``
        # 解耦——同一进程不同 profile 有不同 ``provider`` 字面时,缓存按
        # profile 字面区分,避免误共用。
        self._client_cache: dict[
            tuple[str, str, str | None], LLMClient
        ] = {}
        # post-v6 B:同一个 Router 首次被多个安全并行 Judge 命中时，必须只构造
        # 一个共享 client。锁只覆盖缓存 miss + factory，不覆盖 Provider 调用。
        self._client_cache_lock = Lock()

    # ------------------------------------------------------------------
    # Profile 选择
    # ------------------------------------------------------------------

    def select_profile(
        self,
        agent_name: str,
        *,
        chapter_card: Any = None,
        override_profile_id: str | None = None,
    ) -> WriterProfile:
        """按 agent 名(+ 可选 ChapterCard 上下文)返回应使用的 profile。

        :param agent_name: agent 名(``writer`` / ``reviewer`` /
            ``memory_extractor`` / ``continuity`` / ``reranker`` /
            ``reviser`` / ``reviewer_escalation``)。``writer`` 触发
            ``RoutingConfig.rules`` 规则匹配;其他 agent 直接走
            ``agent_routing``。
        :param chapter_card: 仅 Writer 路径用,通常是 ``ChapterCard``
            实例(走 ``model_dump()`` 取字段 dict)或字段 dict 本身。
            其他 agent 路径忽略此参数。
        :param override_profile_id: 单次写作覆盖(对齐 P3-F-03
            WriterProfileSelector 用例)。非 ``None`` 时跳过规则匹配 /
            ``agent_routing``,直接命中此 profile。**override 路径不走
            provider-mismatch fallback**——显式指定即明确意图,失败由
            ``LLMClient`` 自然抛错,便于上层定位非法 profile。
        :return: 命中的 ``WriterProfile`` 实例。
        :raises KeyError: ``override_profile_id`` 引用未声明 profile,或
            agent_routing / rules 引用未声明 profile(后者本应在
            ``WriterProfilesConfig`` 交叉校验时拦下,留兜底)。
        :raises RuleEvalError: ``writer`` 路径下某条规则求值出错。
        """
        if override_profile_id is not None:
            profile = self._lookup_profile(override_profile_id)
            self._assert_llm_judge_thinking_on(agent_name, profile)
            return profile

        if agent_name == "writer":
            profile_id = self._select_writer_profile_id(chapter_card)
        else:
            profile_id = self._config.routing.agent_routing.get(
                agent_name, self._config.routing.default
            )
        profile = self._lookup_profile(profile_id)
        profile = self._maybe_fallback_for_unavailable_provider(profile)
        self._assert_llm_judge_thinking_on(agent_name, profile)
        return profile

    def _assert_llm_judge_thinking_on(
        self, agent_name: str, profile: WriterProfile
    ) -> None:
        """LLM-as-judge profile hard guard(v1.1 wave 2 临时会话 2026-05-28 立)。

        4 个 LLM-as-judge agent(``must_vs_forbidden_validator`` /
        ``beat_alignment_validator`` / ``must_achieve_validator`` /
        ``continuity_validator``)resolve 出的 profile 必须 thinking-on,否则
        wave 1 治理(G3 / G20 等)失效。

        判定 thinking-on 逻辑见 :func:`is_thinking_on_profile`(mock / claude
        always pass;ollama 看 ``think``;openai_compatible 看 ``enable_thinking``)。

        覆盖路径:
        - 普通 ``agent_routing`` resolve 路径
        - ``override_profile_id`` 显式覆盖路径(防 maintainer 误 override 到非
          thinking-on profile)
        - ``writer_routing.rules`` 写规则匹配路径(writer agent 不在
          ``LLM_AS_JUDGE_AGENTS`` 集合,不触发本 guard;留观未来扩展)
        - ``_maybe_fallback_for_unavailable_provider`` fallback 后路径(若
          fallback 让 LLM-as-judge agent 落到非 thinking-on default,本 guard
          仍截获)

        :param agent_name: ModelRouter.select_profile 收到的 agent 名
        :param profile: 已经过 fallback 的最终 profile
        :raises ValueError: agent ∈ LLM_AS_JUDGE_AGENTS 但 profile 不 thinking-on
        """
        if agent_name not in LLM_AS_JUDGE_AGENTS:
            return
        if is_thinking_on_profile(profile):
            return
        raise ValueError(
            f"LLM-as-judge agent {agent_name!r} resolved to profile "
            f"{profile.profile_id!r} (provider={profile.provider}) "
            f"with thinking off. Per wave 1 G3/G20 治本要求,LLM-as-judge "
            f"agents 必须使用 thinking-on profile "
            f"(extra_request_kwargs.think=True / enable_thinking=True). "
            f"修法: agent_routing[{agent_name!r}] 显式声明 thinking-on profile, "
            f"或 default profile 改 thinking-on; 见 "
            f"docs/decisions/v1_1_wave1_governance_invariants.md §2.4."
        )

    def _maybe_fallback_for_unavailable_provider(
        self, profile: WriterProfile
    ) -> WriterProfile:
        """provider 不可用时 fallback 到 default profile(v1.1 wave 2 H2 重写)。

        **trigger 改自 P7-B-06.5**:旧版按 ``settings.llm_provider`` 全局开关
        判 mismatch(profile.provider != settings 即 fallback);新版按 API
        key 可用性查询(详 :func:`_is_provider_usable`)。**核心收益**:解锁
        "真混跑 provider" — agent_routing 内 ``memory_extractor:
        local_qwen_safe (ollama)`` + ``writer: cloud_qwen_max_medium_creative
        (openai_compatible)`` 同进程共存,memory_extractor 不再被 silent
        升级到云端 default(旧逻辑下 ``settings.llm_provider=openai_compatible``
        ≠ ollama → silent fallback)。

        protective 场景仍覆盖(对齐 P7-B-06.5 引入动机):

        - 维护人无 claude key + ``is_arc_finale=True`` rule 命中
          ``cloud_premium`` → cloud_premium.provider=claude → key 未配 →
          fallback 到 default(若 default 可用),避免 ``ClaudeClient.__init__``
          因无 API key 抛错卡流水线
        - default 也不可用 → 保留原 profile 让 ``LLMClient`` 自然报错
          (避免无限循环 + 错误信息可定位)

        **mock 路径不 fallback**:单测约定 profile.provider 字面与 settings
        解耦,MockLLMClient 不消费 model 字段。

        **override 路径不进入本方法**:维护人显式指定 profile_override
        意图明确,失败由 LLMClient 报错让上层定位非法配置。
        """
        from src.config import settings

        if settings.llm_provider == "mock":
            return profile
        if _is_provider_usable(profile.provider, profile.base_url):
            return profile
        default_profile = self._lookup_profile(self._config.routing.default)
        if _is_provider_usable(
            default_profile.provider, default_profile.base_url
        ):
            return default_profile
        return profile

    # 旧名 alias 保留 1 个 release 防外部依赖(P7-B-06.5 引入名,v1.1 wave 2
    # H2 重写后逻辑变 — 同方法体两名都指最新行为)
    _maybe_fallback_for_provider_mismatch = (
        _maybe_fallback_for_unavailable_provider
    )

    def _select_writer_profile_id(self, chapter_card: Any) -> str:
        """Writer 路径:规则顺序匹配,无命中则按 agent_routing 兜底。"""
        if chapter_card is not None:
            context = self._build_rule_context(chapter_card)
            for rule in self._config.routing.rules:
                if evaluate_rule(rule.condition, context):
                    return rule.profile
        # 无 ChapterCard 上下文(如 Writer 起步章 / 测试场景)或所有规则不
        # 命中:走 ``agent_routing.writer`` 显式声明,缺省回退 ``default``。
        return self._config.routing.agent_routing.get(
            "writer", self._config.routing.default
        )

    @staticmethod
    def _build_rule_context(chapter_card: Any) -> dict[str, Any]:
        """ChapterCard → 规则求值上下文 dict。

        支持三种入参形态:
        - Pydantic 模型(``ChapterCard``):``model_dump()`` 取全部字段。
        - 普通 dict:直接复制(测试常用)。
        - 其他对象:走 ``vars()`` 兜底(实践中不常用,但避免硬抛便于调试)。
        """
        if hasattr(chapter_card, "model_dump"):
            return chapter_card.model_dump()
        if isinstance(chapter_card, dict):
            return dict(chapter_card)
        return dict(vars(chapter_card))

    def has_agent_routing(self, agent_name: str) -> bool:
        """是否在 ``agent_routing`` 表中**显式声明**本 agent。

        :meth:`select_profile` 对未配置 agent 走 ``default`` 兜底,无法
        区分"未配置"与"显式配置成 default"。本方法严格按 ``agent_routing``
        字典键存在性返 bool,供需要"配置开启"语义的调用方使用。

        典型场景:Reviewer 升级路径(P3-plan 决策 4 选 A,默认 disable)。
        Reviewer 在 ``score_overall ∈ [6.0, 8.0]`` 边界仅当配置文件显式列
        ``agent_routing.reviewer_escalation: <profile>`` 才触发升 Claude
        复审;不显式配置时跳过升级,即使 ``default`` 是 cloud_quality 也不
        误触发(避免维护人本机无 API key 时启动报错)。

        :param agent_name: agent 名(如 ``reviewer_escalation``)。
        :return: ``True`` 当且仅当 ``agent_routing`` 字典含此键。
        """
        return agent_name in self._config.routing.agent_routing

    def has_profile(self, profile_id: str) -> bool:
        """是否在 ``writer_profiles`` 字典中声明本 profile(P3-B-11 引入)。

        与 :meth:`has_agent_routing` 语义对称:后者查 ``agent_routing`` 键
        存在性,本方法查 ``profiles`` 键存在性。供前置校验场景(P3-B-11
        ``_ProfileOverrideRouter.__init__`` 校验 ``profile_override`` 已声明)
        使用,避免拖到 :meth:`select_profile` / :meth:`route` 时才抛
        ``KeyError`` 兜底——前置抛 ``ProfileNotFoundError`` 让 API 层翻译为
        422 ``CHAPTER_INVALID_PROFILE``,前端可定位非法 profile_id。

        :param profile_id: profile 字符串。
        :return: ``True`` 当且仅当 ``writer_profiles`` 字典含此键。
        """
        return profile_id in self._config.profiles

    def _lookup_profile(self, profile_id: str) -> WriterProfile:
        try:
            return self._config.profiles[profile_id]
        except KeyError as exc:
            raise KeyError(
                f"profile_id={profile_id!r} 未在 writer_profiles 声明; "
                f"已声明: {sorted(self._config.profiles.keys())}"
            ) from exc

    # ------------------------------------------------------------------
    # LLMClient 构造与缓存
    # ------------------------------------------------------------------

    def get_client(self, profile: WriterProfile) -> LLMClient:
        """按 ``(provider, model, base_url)`` 缓存返 LLMClient 实例。

        首次命中触发 ``client_factory`` 构造;后续相同三元组直接返缓存
        实例。**post-wave5 H1 routing completion 起**,显式传
        ``provider=profile.provider`` 给工厂,工厂按该参数派发 client 类
        (而非 ``settings.llm_provider`` 全局开关)。这让 Router 真正实
        现"多 provider 共存"路径 — 同进程内不同 profile 可绑不同 provider
        的 client。
        """
        key = (profile.provider, profile.model, profile.base_url)
        if key not in self._client_cache:
            with self._client_cache_lock:
                if key not in self._client_cache:
                    self._client_cache[key] = self._client_factory(
                        model=profile.model,
                        base_url=profile.base_url,
                        provider=profile.provider,
                    )
        return self._client_cache[key]

    # ------------------------------------------------------------------
    # complete_kwargs 构造(含 think 翻译)
    # ------------------------------------------------------------------

    @staticmethod
    def build_complete_kwargs(
        profile: WriterProfile, client: LLMClient
    ) -> dict[str, Any]:
        """合并 ``generation_params`` + ``extra_request_kwargs``,按需翻译 ``think``。

        合并规则:
        1. 起点:``profile.generation_params`` 浅拷贝。
        2. 叠加 ``profile.extra_request_kwargs`` 浅拷贝(后者覆盖同名键,
           但 P3-B-01 约定二者键集互不重叠——前者是生成参数,后者是
           provider 扩展)。
        3. **仅 ``isinstance(client, OllamaClient)`` 时**:把 ``think``
           键 ``pop`` 出来,翻译为 ``enable_thinking`` 显式 keyword(对齐
           ``OllamaClient.complete`` 签名)。其他 client 类型(MockLLMClient
           / ClaudeClient)``think`` 仍留在 dict 里——MockLLMClient 透传
           忽略未识别 kwargs;ClaudeClient 路径配置上约定 ``extra_request_kwargs``
           为空 dict,理论上不会出现 ``think``。
        """
        merged: dict[str, Any] = dict(profile.generation_params)
        merged.update(profile.extra_request_kwargs)
        if isinstance(client, OllamaClient) and "think" in merged:
            merged["enable_thinking"] = bool(merged.pop("think"))
        return merged

    # ------------------------------------------------------------------
    # 主入口:route
    # ------------------------------------------------------------------

    def route(
        self,
        agent_name: str,
        *,
        chapter_card: Any = None,
        override_profile_id: str | None = None,
    ) -> RoutedTarget:
        """一次性返回 ``(profile, client, complete_kwargs)`` 三元组。

        参数语义同 :meth:`select_profile`。返回的 ``complete_kwargs`` 已可直
        接 ``**`` 到 ``client.complete(messages, **kwargs)``。
        """
        profile = self.select_profile(
            agent_name,
            chapter_card=chapter_card,
            override_profile_id=override_profile_id,
        )
        client = self.get_client(profile)
        complete_kwargs = self.build_complete_kwargs(profile, client)
        return RoutedTarget(
            profile=profile,
            client=client,
            complete_kwargs=complete_kwargs,
        )
