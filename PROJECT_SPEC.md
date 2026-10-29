# PROJECT_SPEC.md
# 长篇小说创作引擎 - 项目规格文档

> 版本: v1.1
> 维护人: Feng
> 变更记录: v1.1 引入工作区(Workspace)、Writer Profile、API+Web 双前台架构、章节衔接桥、局部修订(revise)、角色 voice profile、记忆健康度、风格漂移检测、章节版本控制等核心机制。post-v6 起正式退休草稿池，新生产固定单稿；旧多稿数据只读兼容。

> 本文档是 Claude Code 开发本系统的最高优先级参考。任何代码实现与本文档冲突时,以本文档为准;若本文档存在歧义或缺失,先与维护人确认,不要自行脑补。

---

## 0. 一句话定义

**这是什么**:一套以"卷-章-节奏-记忆"四层结构为核心、人机协作的长篇小说生成引擎。输入一句话设定,产出可发布质量的长篇连载;通过外部记忆 + 节奏硬约束 + 多 agent 分工解决长文本下"记忆模糊、平铺燃文、大纲坍缩"三大问题。

**这不是什么**:不是聊天式的写作助手;不是一次性生成长文的工具;不是无人工介入的全自动流水线。

**v1 部署形态**:本地单机 FastAPI 服务 + Web 前端(React)+ CLI(辅助/调试)。后端默认 `127.0.0.1` 暴露,前端浏览器访问。

---

## 1. 核心设计原则

冲突时优先级从上到下:

1. **结构 > 文笔**。任何一章不达节奏/结构要求,文笔再好也必须修订或重写。
2. **外部记忆 > 模型记忆**。所有跨章信息必须落库,不依赖上下文窗口。
3. **硬约束 > 软提示**。能用代码校验的规则不要只放在提示词里。
4. **分工 > 全能**。每个 agent 只做一件事。
5. **可观测 > 速度**。宁可慢,不要黑盒。
6. **人工介入点必须显式**。
7. **混合模型,Profile 抽象**。Writer 可在云/本地间自由切换,代码不感知。
8. **前后端分离**。前端只展示与交互,所有业务在后端 service 层。
9. **工作区隔离**。一本书一个工作区,数据物理隔离,代码引擎共用。

---

## 2. 系统架构

### 2.1 顶层结构

```
┌──────────────────────────────────────────────┐
│  Web Frontend (React + Vite + Tailwind)      │
│  浏览器: http://127.0.0.1:5173                │
└─────────────┬────────────────────────────────┘
              │ HTTP/JSON + WebSocket
┌─────────────▼────────────────────────────────┐
│  FastAPI Backend (127.0.0.1:8000)            │
│  ├─ api/        REST + WebSocket             │
│  ├─ services/   业务编排层(API/CLI 共用)    │
│  └─ core/       Agent + Memory + Orchestrator│
└─────────────┬────────────────────────────────┘
              │
┌─────────────▼────────────────────────────────┐
│  Storage / Models                            │
│  ├─ SQLite(每工作区独立)                   │
│  ├─ Chroma(每工作区独立)                   │
│  ├─ Filesystem(章节、归档)                 │
│  ├─ Claude API(云端)                       │
│  └─ Local LLM(Qwen via OpenAI 兼容接口)    │
└──────────────────────────────────────────────┘

CLI(开发/调试) ──> services/(同后端共享)
```

### 2.2 模块清单

| 模块 | 职责 | 主要模型 | 阶段 |
|------|------|---------|------|
| WorkspaceManager | 工作区生命周期管理 | 无 | P1 |
| Orchestrator | 状态机驱动章节流水线 | 无 | P1 |
| OutlineBuilder | 一句话→世界观/人设/主线/卷规划 | Claude | P5 |
| ChapterCardGenerator | 生成章节卡(含 continuity_context) | Qwen | P5 |
| Retriever | 检索相关记忆(向量+重排) | Embedding+Qwen | P2 |
| Writer | 章节卡+记忆→正文 | Profile 决定 | P1 |
| Reviser | 局部修订 | 同 Writer Profile | P1 |
| Reviewer | 多维度评分,judge: pass/revise/rewrite | Qwen+Claude | P1/P4 |
| MemoryExtractor | 正文→结构化增量记忆 | Qwen | P2 |
| MemoryHealthChecker | 记忆库健康度审计 | Qwen | P4 |
| StyleAuditor | 风格漂移检测 | Qwen+Claude | P4接口/P7完整 |
| ModelRouter | 按 Profile 路由模型 | 无 | P3 |
| RuleValidator | 硬约束校验 | 无 | P4 |

### 2.3 技术栈

**后端**:
- Python 3.11+,FastAPI + Uvicorn,SQLAlchemy 2.x + Alembic,Pydantic v2,structlog,pytest
- **不引入** LangChain / LangGraph

**前端**:
- React 18 + TypeScript,Vite,Tailwind + shadcn/ui,TanStack Query,React Router,Zustand,Recharts
- 详见 `FRONTEND_SPEC.md`

**存储**:SQLite(每工作区独立 .db),Chroma(每工作区独立目录),Qwen3-Embedding-0.6B 本地 embedding(1024 维)

**LLM**:Claude API(sonnet-4-6 / opus-4-7),本地通过任意 OpenAI 兼容接口(Qwen / DeepSeek / Llama)

### 2.4 目录结构

```
novel-engine/
├── PROJECT_SPEC.md / OBSERVABILITY.md / ROADMAP.md / FRONTEND_SPEC.md / API_CONTRACT.md
├── pyproject.toml / .env.example / config.yaml / README.md
│
├── workspaces/                # 所有工作区
│   ├── _active                # 软链接 → 当前激活工作区
│   ├── scifi_book_1/
│   │   ├── project.yaml       # 工作区配置覆盖
│   │   ├── meta.json          # 引擎版本/创建时间
│   │   ├── prompts/
│   │   │   └── style_guide.md
│   │   ├── data/
│   │   │   ├── novel.db
│   │   │   ├── chroma/
│   │   │   ├── chapters/
│   │   │   │   └── v1c012/    # 章节级版本目录
│   │   │   │       ├── v1.md
│   │   │   │       ├── v2.md
│   │   │   │       └── final.md
│   │   │   ├── cards/
│   │   │   ├── outline/
│   │   │   └── exports/
│   │   ├── archives/          # prompts/responses 归档
│   │   ├── backups/
│   │   └── logs/
│   └── xianxia_book_1/
│
├── archives/                  # 工作区归档(zip)
├── prompts/                   # 引擎默认提示词模板
├── src/
│   ├── core/                  # 业务核心(agents/memory/models/orchestrator/routing/llm/rules)
│   ├── services/              # API/CLI 共用服务层
│   ├── api/                   # FastAPI(routes/schemas/websocket/auth)
│   ├── cli/
│   ├── config.py
│   └── version.py
├── web/                       # 前端项目
├── alembic/                   # 数据库迁移
└── tests/
```

---

## 3. 数据模型

所有模型用 Pydantic v2 实现。标 [新增] 的是 v1.1 加的字段或类型。

### 3.1 Workspace [新增]

```python
class Workspace(BaseModel):
    workspace_id: str                  # 目录名
    display_name: str
    genre: str
    premise: str
    created_at: datetime
    last_active_at: datetime
    engine_version: str                # 创建时引擎版本
    status: Literal["active", "archived", "completed"]
    total_chapters: int                # 计划总章数
    completed_chapters: int
    config_overrides: dict
```

### 3.2 ChapterCard

```python
class ContinuityContext(BaseModel):    # [新增 A1] 章节衔接桥
    prev_chapter_tail: str             # 上一章末尾 500-800 字
    unfinished_actions: list[str]      # 上一章未完成动作
    time_gap: str                      # "立即续接" / "数分钟后" / "三日后"
    scene_continuation: Literal["same", "switch", "time_jump"]
    pov_change: bool
    pov_from: str | None
    pov_to: str

class CoreStateTransition(BaseModel):  # post-v6 Session 3/C
    state_key: str                     # object:<id>:<attribute> / character:<id>:<attribute>
    entity_type: Literal["object", "character"]
    entity_id: str
    attribute: str                     # 窄白名单；object 可用 possession/control/access
    from_value: str | None              # 三种关系值只能是角色 stable ID 或 null
    to_value: str                       # 三种关系值只能是角色 stable ID
    reason: str

class BookClockConstraint(BaseModel):  # post-v8 C2；versioned typed 书内时间合同
    contract_version: Literal[0, 1] = 0
    time_gap: str
    previous_interval: dict | None
    gap_interval: dict | None
    current_interval: dict | None
    kind: Literal["exact", "bounded_interval", "ordered_relative",
                  "genuinely_unanchored"] | None
    node_id: str                       # v1 由章/source/前投影确定性派生
    node_chapter_id: str
    anchor_node_id: str | None
    relations: list[Literal["same_instant", "after", "same_narrative_day",
                            "same_night", "next_narrative_day", "resume_anchor"]]
    day_phase: Literal["morning", "daytime", "afternoon", "night",
                       "around_midnight", "pre_dawn"] | None
    approximate_quantity: dict | None  # source-level；绝不派生 min/max
    unanchored_reason: str | None
    relation_chain_hash: str
    is_hard: bool                      # v1:前三类只硬化可证明的 typed facts
    soft_reason: str

class BookTimeProjection(BaseModel):
    contract_version: Literal[0, 1]
    current_node: dict | None
    latest_mainline_node_id: str | None
    latest_absolute_anchor: dict | None
    active_relation_chain: list[dict]
    relation_chain_hash: str

class VolumeDramaticPromise(BaseModel):  # post-v8 A1；只引用既有稳定 ID
    primary_thread_id: str | None       # 与 turning 至少一个非空
    primary_turning_id: str | None
    actor_id: str
    counterforce_id: str
    outcome: Literal["resolved", "explicitly_deferred"]
    deferred_to_volume_id: str | None
    deferred_to_thread_id: str | None

class LoadBearingObjectContract(BaseModel):  # post-v8 C1；plot 全书稳定物件目录
    object_id: str
    label: str
    causal_role: Literal["enter", "open", "close", "escape", "irreversible_action"]
    causal_requirement: str
    relation_attributes: list[Literal["possession", "control", "access"]]
    linked_turning_id: str | None       # 与 thread 至少一个非空
    linked_thread_id: str | None

class DramaticBeatContract(BaseModel):   # 高价值 beat/mini-plan 可选；普通留白为 null
    function: Literal["confrontation", "turning", "emotional_peak", "finale"]
    actor_id: str
    counterforce_id: str
    interaction_mode: Literal["direct", "indirect"]
    thread_id: str | None
    turning_id: str | None
    actor_goal: str
    counteraction: str
    choice: str
    consequence: str
    load_bearing_objects: list[LoadBearingObjectContract] = []  # 最多 2 个 plot 精确快照

class DramaticSourceBinding(BaseModel):  # ChapterCard 冻结 volume→beat→mini 同源快照
    volume_id: str
    beat_id: str
    promise: VolumeDramaticPromise
    beat_contract: DramaticBeatContract
    source_hash: str                     # canonical SHA-256

class ChapterCard(BaseModel):
    chapter_id: str
    workspace_id: str                  # [新增]
    volume_id: str
    chapter_number: int
    title: str

    # 节奏控制
    tension_level: int                 # 1-10
    word_count_min: int = 2800
    word_count_max: int = 4500
    plot_node_budget: int = 3

    # 内容契约
    must_achieve: list[str]
    must_unresolved: list[str]
    forbidden: list[str]
    pov_character: str
    setting: str
    dramatic_source: DramaticSourceBinding | None = None

    # 衔接(升级)
    continuity_context: ContinuityContext   # [升级 A1]
    next_chapter_seed: str

    # 全书核心状态与主时间线(post-v6 Session 3/C)
    core_state_contract_version: Literal[0, 1] = 0
    core_state_projection_hash: str = ""     # v1 必须是当前投影 SHA-256
    core_state_transitions: list[CoreStateTransition] = []
    book_clock_constraint: BookClockConstraint | None = None

    # 元数据
    arc_phase: Literal["opening", "rising", "climax", "falling", "resolution"]
    is_arc_finale: bool
    related_plot_threads: list[str]
    related_characters: list[str]

    # 审稿配置
    review_threshold: float = 7.5
    revise_threshold: float = 6.5      # [新增 A2]
    max_rewrites: int = 3
    max_revises: int = 2               # [新增 A2]
```

### 3.3 Character

```python
class VoiceProfile(BaseModel):         # [新增 A3]
    catchphrases: list[str] = []
    tone: str
    sentence_style: str
    self_address: str
    addresses_others: dict[str, str]
    forbidden_phrases: list[str] = []

class Character(BaseModel):
    character_id: str
    workspace_id: str                  # [新增]
    name: str
    aliases: list[str] = []
    archetype: str
    appearance: str
    personality: str
    background: str
    voice_profile: VoiceProfile        # [新增 A3]
    goals_long_term: list[str]
    goals_short_term: list[str]
    current_state: dict
    relationships: dict[str, str]
    abilities: list[str]
    secrets: list[str]
    first_appearance_chapter: str | None
    last_updated_chapter: str | None
```

### 3.4 PlotThread / TimelineEntry

(沿用 v1.0,各加 `workspace_id`)

### 3.5 Chapter

```python
class ChapterVersion(BaseModel):       # [新增 A7]
    version_id: int
    content: str
    word_count: int
    created_at: datetime
    creation_type: Literal["draft", "rewrite", "revise", "manual_edit"]
    model_used: str
    review_score: float | None
    notes: str

class Chapter(BaseModel):
    chapter_id: str
    workspace_id: str                  # [新增]
    card: ChapterCard
    current_version: int
    versions: list[ChapterVersion]     # [新增 A7]
    status: Literal[
        "drafting", "reviewing", "rewriting", "revising",
        "approved", "published", "human_intervention"
    ]
    rewrite_count: int = 0
    revise_count: int = 0              # [新增 A2]
    review_history: list[ReviewResult]
    created_at: datetime
    approved_at: datetime | None
    total_token_usage: dict
    total_cost_usd: float
```

### 3.6 ReviewResult(升级)

```python
class ReviewIssue(BaseModel):          # [新增 A2]
    paragraph_anchor: str              # 出问题段落前 30 字
    issue_type: str
    description: str
    suggestion: str

class ReviewResult(BaseModel):
    review_id: str
    chapter_id: str
    version_reviewed: int              # [新增]
    reviewed_at: datetime
    reviewer_model: str

    score_plot_progression: float
    score_tension_match: float
    score_prose_quality: float
    score_consistency: float
    score_hook_management: float
    score_voice_consistency: float     # [新增 A3]

    score_overall: float

    hard_constraints_passed: bool
    failed_constraints: list[str]

    issues: list[ReviewIssue]          # [升级 A2]
    suggestions: list[str]
    verdict: Literal["pass", "revise", "rewrite", "escalate_to_human"]
    revise_targets: list[ReviewIssue]  # [新增 A2]
```

### 3.7 WriterProfile [新增]

```python
class WriterProfile(BaseModel):
    profile_id: str
    provider: Literal["claude", "ollama", "openai_compatible", "mock"]
    model: str
    base_url: str | None
    context_window: int
    generation_params: dict
    extra_request_kwargs: dict = {}     # provider 扩展字段(ollama think 透传等)
    cost_per_1k_input: float = 0.0
    cost_per_1k_output: float = 0.0
    notes: str = ""
```

> **provider 枚举说明**(v1.1 w3-s8 F2 字面同步,对齐 `src/core/models/writer_profile.py`):
> `claude`(Anthropic SDK)/ `ollama`(P3-B-01,本地原生 API)/ `openai_compatible`
> (v1.0.x post-wave5 H1,覆盖 dashscope qwen / DeepSeek / Moonshot 等所有 OpenAI 兼容 API)
> / `mock`(无 API key 时默认,对齐维护人环境约束)。`extra_request_kwargs` 为
> P3-B-01 新增的 provider 扩展字段(原 spec 字面缺,本次一并补)。

### 3.8 StyleAuditReport [新增]

```python
class StyleAuditReport(BaseModel):
    audit_id: str
    workspace_id: str
    audited_chapter_range: tuple[str, str]
    audited_at: datetime
    metrics: dict                      # 句长均值/对话密度/修辞密度等
    drift_score: float
    drift_dimensions: list[str]
    severity: Literal["ok", "minor", "warning", "critical"]
    recommendations: list[str]
```

---

## 4. 核心规则与硬约束

### 4.1 节奏类(沿用 v1.0)

- [TASK] 篇幅按章节卡 word_count_min/max 起草（默认 2500–3500 系统字符），与其他写作任务同权重。
  2026-10-02 维护人批准：word_count_policy_version=1 不按字数拒稿、扣分、压缩或扩写；
  v0 只兼容冻结的历史活动与旧判决。事实、剧情边界和连续性仍按 HARD 执行。
- [HARD] must_achieve 全部达成
- [HARD] must_unresolved 全部保留未解决
- [HARD] forbidden 不出现
- [SOFT] plot_node_budget 不超
- [HARD] 副本类剧情 ≥ 3 章
- [SOFT] 张力曲线起伏

### 4.2 一致性类

- [HARD] 出场角色必须在人设卡中存在
- [HARD] 死亡/重大状态变化必须前 ≥ 2 章铺垫
- [HARD] 主时间线不得真正倒序；post-v8 C2 只按 frozen typed facts 判定，无法证明的
  自然时间留白不得伪判
- [SOFT] 行为符合 personality
- **[新增 HARD]** 角色对白不出现该角色 voice_profile.forbidden_phrases
- **[新增 SOFT]** 对白体现 voice_profile 至少一项特征
- **[HARD, contract v1]** Writer、Setting Judge 与写后提交必须共用章节卡冻结的
  核心状态投影和约束 ID；卡片未声明 `from_value → to_value` 的承重客观状态不得改变
- **[HARD, contract v1]** 核心变化只有在本章已批准正文存在可定位逐字证据后才成为
  当前事实；ChapterSummaryExtractor 只有候选提名权，含混、非客观、短期或非承重候选
  保持 SOFT，富记忆不得覆盖核心账
- **[HARD]** 每个 `state_key` 同时最多一条 active revision；Rewind 撤回目标章及后续
  revision 并恢复仍有效前驱，Pause/Stop 不改账
- **[HARD, post-v8 A1]** UI `mystery` 与中文悬疑/推理共用 canonical genre 身份；
  新生成的悬疑/推理题材每卷必须有唯一 `dramatic_promise`，卷末恰好一个 finale beat 以同一
  actor/counterforce/thread/turning 明确 `resolved` 或 `explicitly_deferred`。递延必须
  指向后续既有 volume/thread；不得把未解决伪写为解决
- **[HARD, post-v8 A1]** 高价值 beat 可携带 `DramaticBeatContract`；承担它的
  mini-plan 恰好一章逐字段同源，并闭合角色/thread 引用。`indirect` 反动作合法但必须有
  可见后果；普通独处、治愈、休整与留白不强制合同
- **[HARD, post-v8 A1]** ChapterCard 由代码冻结 source binding/hash；批准、checkpoint/
  Continue、显式 Revise 与 Writer Provider 前都要对当前 volume/beat/mini-plan 复核。
  legacy 缺字段可读，但当前 typed source 存在时不得降级或静默丢失
- **[HARD, post-v8 A1]** 稳定 ID、resolved/deferred 与机器判定只存在于 typed 字段；
  `must_achieve`、summary、relationship 等自然文本不得承载机器前缀、opaque token、
  位置协议或伪结构化 marker，也不得用关键词解析恢复状态
- **[HARD, post-v8 C1]** `PlotOutput.load_bearing_objects` 只收真正决定 turning/beat/finale
  可执行性的物件，按因果作用而非出现频率选择；搪瓷杯、沙粒、普通工具与可互换耗材
  保持 clean control。高价值 beat 与指定 mini-plan 只能逐对象复制 plot catalog 的完整
  typed 快照，ChapterCard source hash 覆盖该快照；alias、错 turning/thread 或来源漂移
  必须在正文 Provider 前失败
- **[HARD, post-v8 C1]** 承重物件关系只分为 `possession`（物理持有）、`control`
  （决策/授权控制）与 `access`（当前可操作目标）。关系两端只认原子 stable ID，不得把
  三义塞入复合字符串、自由文本 marker、关键词或位置协议；`holder` 仅供 legacy 存储
  读取，新 Card、Summary 与 revision 不得写入
- **[HARD, post-v8 C1]** contract v1 中，每个进入当前 beat 的承重物件至少冻结一条
  获准关系迁移；迁移必须命中该物件声明的 relation attribute，`to_value` 必须是本章
  `related_characters`。同章默认最多建立一个此前未登记的承重物件身份；本章经来源
  验真的 plot→beat→mini 明确要求两个时，可首次登记这两个 ID，仍受两条迁移容量限制。
  首次登记不代表故事中首次获得，保持关系不要求另演交接；自然正文证据仍须验真。
  错 `from`、错 target、错 source/hash、并发 revision 或缺 exact evidence 均不得提交，checkpoint 与
  Rewind 复用同一 CoreState 事务边界

### 4.3 衔接类 [新增]

- **[HARD]** 旧卡 continuity_context.unfinished_actions 非空时，本章前 200 字必须解决或显式延续。新叙事卡显式保存 immediate_action_indices 时，只对来源验真的即时子集执行此要求；长期未决完整保留为背景，不要求逐章重演。索引须与前章已验真账本的 is_immediate 逐条一致，缺记录或来源漂移不得当作空集放行。
- **[HARD]** scene_continuation = "same" 时,前 100 字不允许地点变更
- **[SOFT]** legacy v0、真正未锚定片段，以及无法由 frozen typed facts 证明矛盾的
  含混/对白/梦境/回忆/插叙时间表述只作语义提示
- **[HARD, post-v8 C2 clock contract v1]** 每章书内时间只属于 `exact`、
  `bounded_interval`、`ordered_relative`、`genuinely_unanchored` 之一。`exact`
  只来自原文明示唯一点或明确数值时长；`bounded_interval` 只来自原文明示闭区间；
  “同夜稍后”“次日清晨”“数日后”“约一小时后”“当夜”均保存原文、唯一 anchor、
  closed relation/day phase，其中近似量不得派生分钟上下界或 numeric HARD
- **[HARD, post-v8 C2 clock contract v1]** 只有 frozen facts 能直接证明的 exact 冲突、
  明示 bounds 越界、`AFTER`/同日/同夜/次日/恢复锚违反和真正主线倒序才可拒绝。
  Writer、continuity Q3 与 Setting Judge 共用同一约束 ID/source/hash；Writer 只看到
  原始自然语言和关系含义，不显示累计分钟、不要求正文写时间戳
- **[HARD, post-v8 C2 persistence]** ordered/unanchored 节点不得清空最近 absolute anchor
  或既有关系链；v1 typed snapshot/chain 进入既有 `value_json`、projection/commit hash、
  checkpoint details 与 Rewind 事务。v0 lazy 读取不 backfill、不猜 relation、旧 canonical
  hash 字节不变；本合同不新增 DB 表/列/Alembic

核心状态 HARD 胶囊目标上限为 8 组/1000 字；超过 12 组或 1500 字时在 Writer 调用前
显式失败，禁止静默截断或把审计 hash/source 挤入正文提示。工作区
`generation.core_state_hard_enabled=false` 只让后续新卡回到 contract v0，不删除历史账。

### 4.4 审稿评分(升级)

```
score_overall =
    0.25 * score_plot_progression +
    0.20 * score_tension_match +
    0.20 * score_consistency +
    0.15 * score_voice_consistency +    # [新增]
    0.10 * score_prose_quality +
    0.10 * score_hook_management
```

判决规则(升级):

- 任一硬约束失败 → `rewrite`
- 综合分 < revise_threshold(6.5)→ `rewrite`
- 综合分 ∈ [revise_threshold, review_threshold) → `revise`
- 综合分 ≥ review_threshold(7.5)→ `pass`
- rewrite_count ≥ max_rewrites 或 revise_count ≥ max_revises → `escalate_to_human`

---

## 5. 提示词原型

(沿用 v1.0 的 outline_builder / chapter_card / writer / reviewer / memory_extractor / style_guide)

新增:

- `prompts/reviser.md`:输入正文 + ReviewIssue 列表 → 修订后正文(只改指定段落)
- `prompts/memory_health_checker.md`:输入工作区当前 PlotThread + Character → 健康度报告
- `prompts/style_auditor.md`:输入 style_guide + 最近 N 章正文 → StyleAuditReport

---

## 6. 状态机(升级)

```
新增状态: REVISING

新增/修改转换:
  REVIEWING --[verdict=revise, revise_count<max]--> REVISING
  REVIEWING --[verdict=revise, revise_count>=max]--> REWRITING
  REVISING --[done]--> REVIEWING
```

正文生产固定单稿:
- 新建大纲、章节卡、首稿、rewrite、revise 与 checkpoint/Continue 均只走一条正文链
- Writer 每轮至多调用一次,Reviewer 只审核当前唯一稿件
- 旧 workspace 已持久化的 `draft_strategy=single/parallel_2/parallel_3` 只在磁盘读取层校验并折叠为单稿；不得重新扇出 Provider 调用
- 新 API、Prompt、动态 JSON Schema 与 UI 不暴露或接受 `draft_strategy`

---

## 7. Writer Profile 与模型路由

```yaml
writer_profiles:
  cloud_quality:
    provider: claude
    model: claude-sonnet-4-6
    context_window: 200000
    generation_params: { temperature: 0.85, max_tokens: 6000 }
    cost_per_1k_input: 0.003
    cost_per_1k_output: 0.015

  cloud_premium:
    provider: claude
    model: claude-opus-4-7
    context_window: 200000

  local_qwen_32k:
    provider: openai_compatible
    base_url: http://localhost:11434/v1
    model: qwen3.5:latest
    context_window: 32000

  local_qwen_safe:
    provider: openai_compatible
    base_url: http://localhost:11434/v1
    model: qwen3.5:latest
    context_window: 16000

writer_routing:
  default: cloud_quality
  rules: []   # 默认无 Writer 特殊路由

# 工作区可在 project.yaml 覆盖整个 writer_routing
# 有 Claude API key 的部署可按需自加 cloud_premium 规则
# (如 is_arc_finale==true / tension_level>=9 → cloud_premium)
```

> **§7 writer_routing.rules 默认置空(v1.1 wave 4 B1,2026-06-03 维护人拍板)**:
> 原默认两条规则 `is_arc_finale==true → cloud_premium` / `tension_level>=9 →
> cloud_premium`(finale/高张力章升级 Claude opus)已移除。**为何**:本项目维护人
> 环境**永久无 Claude API key**(仅 dashscope),这两条规则命中时只能靠 Router
> fallback 兜底,而 `.env` 中一个无效非空 `CLAUDE_API_KEY`(401)会让
> `_is_provider_usable("claude")` 误判可用 → 不降级 → 硬崩(B1 ch-3 S1 实证)。
> 移除后引擎默认全程走 dashscope,任何书的 finale/高张力章不再撞此崩溃。
> `cloud_premium` profile 定义保留 — 有 Claude key 的部署可在 config.yaml /
> project.yaml 自加上述规则启用。

其他 agent(ChapterCard、Reviewer、MemoryExtractor、Reranker)路由配置见 v1.0 §7。

---

## 8. 错误处理与人工介入

(沿用 v1.0,新增介入点)

- 记忆健康度 critical → 人工裁决
- 风格漂移 critical → 人工裁决
- 工作区 engine_version 不兼容 → 强制迁移确认

---

## 9. 工作区生命周期

### 9.1 状态

```
created → active → completed → archived
                           ↘ purged(物理删除)
```

### 9.2 命令(CLI 与 API 对等)

```
novel workspace create <name> --premise "..." --genre <genre>
novel workspace list
novel workspace switch <name>
novel workspace info <name>
novel workspace archive <name>            # 打包到 archives/<name>.zip
novel workspace clean <name>              # 清理运行时数据,保留正文+大纲+人设
novel workspace purge <name>              # 彻底删除(二次确认)
novel workspace export <name> --format <md|epub|docx|chaptered_md>
novel workspace backup <name>
novel workspace restore <name> --from <backup_id>
```

### 9.3 默认策略

- **完成后归档而非删除**:status=completed 不自动删数据
- **自动备份**:每章 APPROVED 时自动 zip 备份关键数据(novel.db + outline + cards)→ backups/,保留最近 30 份
- **purge 二次确认**:CLI 输入工作区名确认;API 传 confirm=<workspace_id>

### 9.4 数据导出

- `md`:全书拼接成单一 markdown
- `chaptered_md`:每章一个 .md
- `epub`:标准 EPUB 3.0
- `docx`:Word 文档

附带 `meta.json`:作者、字数、卷章数、创作时间、引擎版本。

---

## 10. 配置与密钥

### 10.1 三层配置

1. 引擎默认:`config.yaml`
2. 工作区覆盖:`workspaces/<name>/project.yaml`
3. 运行时覆盖:CLI 参数 / API 参数

加载顺序 1 → 2 → 3,后者覆盖前者。

### 10.2 .env

```
CLAUDE_API_KEY=sk-ant-...
QWEN_BASE_URL=http://localhost:11434/v1
LOG_LEVEL=INFO
DATA_DIR=./workspaces
API_HOST=127.0.0.1
API_PORT=8000
API_AUTH_ENABLED=false                  # 监听公网必须改 true
API_AUTH_TOKEN=
```

### 10.3 API 安全

- 默认绑定 `127.0.0.1`,**绝不**默认绑 `0.0.0.0`
- 公网访问需显式 `--host 0.0.0.0` 且 `API_AUTH_ENABLED=true`,否则启动时报错拒绝运行

---

## 11. 数据迁移与备份

### 11.1 版本对齐

- `src/version.py` 定义引擎版本
- 工作区 `meta.json` 记录创建时引擎版本
- 启动时比对,工作区版本 < 引擎版本触发自动迁移

### 11.2 迁移流程

- SQLite schema 用 alembic 管理
- 破坏性变更必须有迁移脚本 + 回滚脚本
- 迁移前自动备份(backups/pre_migration_*.zip)

### 11.3 备份策略

- **自动**:每章 APPROVED 后增量备份(只备 db + outline,向量库可重建)
- **手动**:`novel workspace backup <name>` 全量
- **保留**:最近 30 份自动备份 + 全部手动备份

---

## 12. 验收标准

通用(每阶段必查):

- [ ] 新增代码 pytest 通过
- [ ] 数据模型字段无遗漏
- [ ] 硬约束有 validator + 单测
- [ ] 日志符合 OBSERVABILITY.md
- [ ] API 接口 OpenAPI 自动生成可用
- [ ] 前端 TypeScript 类型从 OpenAPI 同步
- [ ] README 更新

阶段性验收见 ROADMAP.md。

---

## 13. Claude Code 协作约定

1. 每次新会话先读 PROJECT_SPEC / OBSERVABILITY / ROADMAP / FRONTEND_SPEC / API_CONTRACT
2. 不引入本文档技术栈以外的依赖,先写 QUESTIONS.md
3. 不修改本文档,有疑问写 QUESTIONS.md
4. 不跨阶段实现
5. 每完成一个模块,写 ≤ 1 页实现说明:`docs/impl/<module>.md`
6. 数据库 schema 变更必须用 alembic 迁移
7. 提示词修改保留旧版本(`prompts/archive/<name>_v<n>.md`)
8. **后端服务必须通过 services/ 层暴露,不允许 API 直接调 core/**
9. **前端不得直接读数据库或文件系统,所有数据通过 API**
10. **每个 API 接口必须有 Pydantic 请求/响应 schema,禁止裸 dict**

---

## 14. 未来扩展(v2+ 备忘)

明确登记但**不进 v1 范围**:

- B1. 多读者视角校验(reader persona agent)
- B2. 同人/番外支持(工作区下分主线/番外/同人)
- B3. 角色关系图谱可视化(D3 force layout)
- B4. 多人协作模式
- B5. 自动发布到公众号/X(独立项目,API 对接)
- B6. TTS 听书音频生成
- B7. 读者反馈闭环

v1 周期内一律拒绝实现。

---

## 15. 术语表

- **工作区(Workspace)**:一本书的独立数据+配置容器,系统级单一引擎服务多工作区
- **Writer Profile**:写作模型配置组,可云可本地
- **衔接桥(Continuity Context)**:章节卡中负责保证前后章衔接的子结构
- **历史草稿池(Draft Pool)**:已退休的并行多稿机制；旧 workspace/归档只读保留，新生产不可触发
- **修订(Revise)**:对正文局部段落的精修,与"重写(Rewrite)"区分
- **声音档案(Voice Profile)**:角色对白与语言风格的规范
- **风格漂移(Style Drift)**:长篇连载中文笔/节奏/调性偏离作者初衷
- **记忆健康度(Memory Health)**:记忆库内伏笔、人物状态等数据的一致性与活跃度
- **卷(Volume)/弧(Arc)/副本(Dungeon)/节点(Plot Node)/伏笔(Plot Thread)/章节卡(Chapter Card)/静态资产(Static Asset)/增量记忆(Incremental Memory)**:沿用 v1.0
