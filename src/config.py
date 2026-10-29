"""应用配置(pydantic-settings),P1 仅加载 .env。

模块底部实例化 settings 单例,供全项目导入使用:
    from src.config import settings

import 时即触发字段校验与跨字段安全校验。当 API_HOST=0.0.0.0 但
API_AUTH_ENABLED=false 时,import 立即抛 ValidationError,防止裸跑公网。
"""

from pathlib import Path
from typing import Literal

from pydantic import Field, SecretStr, model_validator
from pydantic_settings import BaseSettings, SettingsConfigDict


class Settings(BaseSettings):
    """从环境变量与 .env 加载的应用配置。

    pydantic-settings 默认大小写不敏感,环境变量 API_HOST 自动映射到字段 api_host。
    .env 文件路径相对当前工作目录;不存在时静默跳过,完全靠环境变量也能工作。
    """

    model_config = SettingsConfigDict(
        env_file=".env",
        env_file_encoding="utf-8",
        case_sensitive=False,
        extra="ignore",
    )

    claude_api_key: SecretStr = Field(
        default=SecretStr(""),
        description="Anthropic API key,SecretStr 防止误打日志泄露。",
    )

    dashscope_api_key: SecretStr = Field(
        default=SecretStr(""),
        description=(
            "阿里云 DashScope API key,SecretStr 防止误打日志泄露(v1.0.x"
            " post-wave5 H1 引入)。WriterProfile.base_url 指向 dashscope"
            " endpoint(``https://dashscope.aliyuncs.com/compatible-mode/v1``)"
            "时必填,否则 OpenAICompatibleClient.__init__ 抛 ValueError。"
        ),
    )

    deepseek_api_key: SecretStr = Field(
        default=SecretStr(""),
        description=(
            "DeepSeek API key,SecretStr 防止误打日志泄露(v1.1 wave 2 H2 引入)。"
            "WriterProfile.base_url 指向 deepseek endpoint(``https://api.deepseek.com``)"
            "时必填。同走 OpenAICompatibleClient 通配路径。"
        ),
    )

    tokenhub_api_key: SecretStr = Field(
        default=SecretStr(""),
        description=(
            "腾讯云 TokenHub API key。只允许精确官方 HTTPS host "
            "tokenhub.tencentmaas.com 通过 endpoint authority 读取；不得发送到"
            "其它兼容 host。SecretStr 防止日志或配置序列化泄露。"
        ),
    )

    alibaba_model_studio_workspace_id: str = Field(
        default="",
        pattern=r"^(?:|[a-z0-9](?:[a-z0-9-]{0,61}[a-z0-9])?)$",
        description=(
            "阿里云百炼华北2北京业务空间的非密钥 DNS label，仅供显式 "
            "workspace-specific endpoint 使用；S2-1 qwen3.8-27b 公共 "
            "DashScope 路由不读取此字段，也不能替代 DASHSCOPE_API_KEY。"
        ),
    )

    openai_compatible_api_key: SecretStr = Field(
        default=SecretStr(""),
        description=(
            "OpenAI 兼容协议通配 API key,SecretStr 防止误打日志泄露(v1.1"
            " wave 2 H2 引入)。覆盖未单独列字段的 OpenAI 兼容 provider"
            "(典型:Moonshot / 智谱 GLM / OpenAI 官方 / 任何兼容服务)。"
            "WriterProfile.base_url 指向相应 endpoint 时,OpenAICompatibleClient"
            " 按规范 hostname registry 精确匹配 provider 专字段(dashscope_api_key"
            "/ deepseek_api_key / tokenhub_api_key 等);未匹配的合法 endpoint"
            "只使用本字段。"
        ),
    )

    api_host: str = Field(
        default="127.0.0.1",
        description="FastAPI 绑定 host;0.0.0.0 必须配合 api_auth_enabled=true。",
    )
    api_port: int = Field(default=8000, ge=1, le=65535)
    api_auth_enabled: bool = Field(default=False)
    api_auth_token: SecretStr = Field(default=SecretStr(""))

    data_dir: Path = Field(
        default=Path("./workspaces"),
        description="工作区数据根目录,每个工作区一个子目录。",
    )

    prompts_dir: Path = Field(
        default=Path("prompts"),
        description=(
            "提示词模板根目录,默认仓库根 prompts/。P3 引入工作区级覆盖时,"
            "WorkspacePaths.prompts_dir 优先,本字段做兜底。"
        ),
    )

    config_yaml_path: Path = Field(
        default=Path("config.yaml"),
        description=(
            "引擎默认 ``config.yaml`` 路径(P3-B-01 引入)。文件含顶层 "
            "``writer_profiles`` / ``writer_routing`` 两段,由 "
            "``src.core.config_loader.load_writer_profiles_config`` 加载。"
            "P3-B-02 工作区 ``project.yaml`` 覆盖在 service 层叠加。"
        ),
    )

    log_level: str = Field(default="INFO")

    llm_provider: Literal["claude", "ollama", "openai_compatible", "mock"] = Field(
        default="mock",
        description=(
            "LLM provider 选择(**v1.1 wave 2 H2 起语义降级**)。"
            "默认 mock(对齐 CLAUDE.md §维护人环境约束:维护人无 Claude API"
            " key,P1-P2 默认走 mock 不调真 API)。\n\n"
            "**v1.1 wave 2 H2 之前(P3-B-01 ~ post-wave5 H1)**:本字段作"
            "**运行时全局开关**,Router 用它判 provider-mismatch fallback,"
            "工厂用它派发 client 类型 — 让 真混跑 provider(同进程不同"
            " agent 走不同 provider)不可达。\n\n"
            "**v1.1 wave 2 H2 起语义降级**:Router 不再用本字段做 fallback"
            "(改按 API key 可用性查询,详 src/core/routing/router.py:"
            "_is_provider_usable);工厂仍按 profile.provider 派发(post-"
            "wave5 H1 routing completion 已建)。本字段仅剩 2 用途:\n"
            "  ① mock 路径短路(本字段=mock 时 Router 跳过所有 fallback,"
            "MockLLMClient 不消费 model 字段)\n"
            "  ② 工厂 bypass router 路径的缺省 fallback(CLI / 启动期 deps"
            " bootstrap 等极端兜底)\n\n"
            "**真混跑配置方式**:在 config.yaml writer_profiles 内不同"
            " profile 设不同 provider,agent_routing 段把不同 agent 指向"
            "不同 profile 即可(本字段值无关)。各 provider 的 API key 在"
            " Settings 对应字段(claude_api_key / dashscope_api_key 等),"
            " 通过 UI 上 PUT /api/v1/config/api-keys 输入或 .env 静态配置。"
        ),
    )

    post_review_gate_max_concurrency: int = Field(
        default=4,
        ge=1,
        le=8,
        description=(
            "写后独立 LLM 闸(must_achieve / continuity 各 Q / setting / "
            "dedup / ending_tension)并发判定上限(v2 间隙 A1,2026-06-13)。"
            "判定层并发跑、合并层仍按原链顺序串行重放,verdict 语义与串行"
            "完全一致(防回退测 test_post_review_gate_parallelism 锁死)。"
            "**1 = 关闭并发退回旧串行链**(运维回退开关;环境变量 "
            "POST_REVIEW_GATE_MAX_CONCURRENCY=1)。上限 8 防 provider "
            "突发 QPS 限流；429 时 "
            "validator 自身 fail-open 放行 + warn 日志,不阻断审稿)。"
        ),
    )

    mini_plan_beat_alignment_max_concurrency: int = Field(
        default=4,
        ge=1,
        le=4,
        description=(
            "mini-plan Beat 对齐 Judge 的卷内并发上限。只在同一卷内按最多 "
            "4 章组成严格波次；当前波次全部完成并按章号稳定合并后，才会启动"
            "下一波。卷与卷仍保持串行，下一卷继续读取上一卷已验收的末 5 章。"
            "1 = 关闭并发并恢复逐章 fail-fast；环境变量 "
            "MINI_PLAN_BEAT_ALIGNMENT_MAX_CONCURRENCY=1。"
        ),
    )

    chapter_card_semantic_gate_max_concurrency: int = Field(
        default=2,
        ge=1,
        le=2,
        description=(
            "ChapterCard 写前 setting Judge 与 must-vs-forbidden Judge 的并发"
            "上限。2 = 两路只读快照并行、全部结束后仍按 setting → contract "
            "固定顺序合并；1 = 恢复旧串行 fail-fast。环境变量 "
            "CHAPTER_CARD_SEMANTIC_GATE_MAX_CONCURRENCY=1。"
        ),
    )

    outline_builder_max_retry_attempts: int = Field(
        default=3,
        ge=1,
        le=10,
        description=(
            "OutlineBuilder 单段 LLM 调用失败时的自动 retry 次数(含首次,>=1)。"
            "v1.0.x wave 5 session 2 引入:luo3 实测 qwen-novel 复杂 JSON schema "
            "任务失败率 ~50%(6 次重试 5 次 fail);通用 retry-with-feedback "
            "helper 把 ValidationError / JSONDecodeError / post_validate 错"
            "转中文反馈追加进下次 prompt,默认 N=3 时成功率 1-0.5^3≈87.5%。"
            "上限 10 防止极端配置下 outline 生成卡死(本地 qwen-novel 每段 ~30-60s,"
            "N=10 一段最坏 5-10 分钟)。"
        ),
    )

    continuity_strategy: Literal["rule", "llm", "rule_plus_llm"] = Field(
        default="rule",
        description=(
            "衔接桥抽取策略(P2-B-12 引入,v1.0.x wave 5 session 3 默认由"
            "``llm`` 改 ``rule``;v1.1 K1 加 ``rule_plus_llm`` 第三档)。\n\n"
            "``rule`` 走 P1 简单字符串切片(``extract_from_prev_text``,取上一章"
            "末尾 800 字字面作 ``prev_chapter_tail`` + 启发式扫 "
            "``unfinished_actions``,其余沿用 ``card.continuity_context`` 手填值)。\n\n"
            "``llm`` 走 LLM 抽取(``extract_continuity_with_llm``,完整 "
            "``ContinuityContext`` 7 字段全部由 LLM 输出 JSON 给出,含 "
            "``prev_chapter_tail`` —— LLM 会把上一章末尾改写/截断后塞回)。\n\n"
            "``rule_plus_llm``(v1.1 K1)叠加模式:rule **确定性**保住"
            "``prev_chapter_tail``(字面 800 字,LLM 永不覆盖)+ ``unfinished_actions``"
            "打底 + ``pov_from`` / ``pov_to`` / ``pov_change``(卡字面对比);LLM **只增强**"
            "``time_gap`` + ``scene_continuation`` 两个需语义理解、Writer 确实消费的软"
            "字段(逐字段降级:LLM 失败/字段非法时退回 rule/卡值,字面 tail 与 pov_* "
            "永远保底)。比 ``llm`` 全有或全无更稳 —— 字面真值不丢,只补语义。\n\n"
            "默认 ``rule`` 的动机:本地 qwen-novel 路径下 LLM 二次抽提(luo3 实测)"
            "质量不稳,会把上一章末尾稀释/改写后塞回 tail,Writer 实际拿到 paraphrase "
            "而非字面 500-800 字 → 衔接生硬;rule 路径直接给字面真值,信号最强。"
            "云端 LLM 接入(v1.1 H 类)后,用户可在 SettingsPage 切 ``rule_plus_llm`` "
            "(推荐:保字面 tail + 智能补软字段)或 ``llm``(全 LLM 抽提)。"
        ),
    )

    @model_validator(mode="after")
    def _validate_public_host_requires_auth(self) -> "Settings":
        """绑公网必须开鉴权,否则拒绝启动。"""
        if self.api_host == "0.0.0.0" and not self.api_auth_enabled:
            raise ValueError(
                "API_HOST=0.0.0.0 暴露在公网,必须同时设置 API_AUTH_ENABLED=true "
                "并提供 API_AUTH_TOKEN。详见 PROJECT_SPEC §10.3。"
            )
        if self.api_auth_enabled and not self.api_auth_token.get_secret_value():
            raise ValueError(
                "API_AUTH_ENABLED=true 时必须提供非空的 API_AUTH_TOKEN。"
            )
        if self.alibaba_model_studio_workspace_id in {"trial", "token-plan"}:
            raise ValueError(
                "ALIBABA_MODEL_STUDIO_WORKSPACE_ID 不得使用阿里共享/试用保留域名"
            )
        return self

    @model_validator(mode="after")
    def _validate_log_level(self) -> "Settings":
        allowed = {"DEBUG", "INFO", "WARNING", "ERROR"}
        if self.log_level.upper() not in allowed:
            raise ValueError(
                f"LOG_LEVEL 必须是 {sorted(allowed)} 之一,收到 {self.log_level!r}"
            )
        return self


settings = Settings()
