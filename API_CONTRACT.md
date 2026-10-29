# API_CONTRACT.md
# API 契约与设计原则

> 版本: v1.0
> 关联: PROJECT_SPEC.md / FRONTEND_SPEC.md
> 原则: API 是前后端的唯一边界,设计要稳定、可演进、自描述。**具体接口由 FastAPI 自动生成 OpenAPI,本文档只定原则、命名规范、关键流程**。

---

## 1. 总体原则

1. **REST 优先,WebSocket 用于实时推送**。短轮询禁止。
2. **资源化命名**,动词放 HTTP 方法上(GET/POST/PUT/PATCH/DELETE)。
3. **不写 Action 风格 URL**(❌ `/api/createChapter`),除非是不可建模为资源的操作(如 `POST /api/chapters/{id}/regenerate`,这是允许的)。
4. **所有响应有统一错误格式**(见 §3)。
5. **所有写操作幂等性显式**:GET/PUT/DELETE 幂等,POST 默认非幂等(可带 idempotency_key 参数支持幂等)。
6. **版本化通过 URL 前缀**:`/api/v1/...`(v1 期间所有接口都在 v1 下,v2 出来再分)。
7. **分页统一**:`?page=1&page_size=20`,响应带 `total/page/page_size/items`。
8. **筛选与排序**:`?filter[status]=approved&sort=-created_at`(`-` 表降序)。
9. **字段裁剪**:大对象支持 `?fields=id,title,status` 减少传输。
10. **OpenAPI 即文档**,所有路由必须有 summary、description、response model。

---

## 2. URL 命名规范

```
/api/v1/<resource>                    集合
/api/v1/<resource>/{id}               单个
/api/v1/<resource>/{id}/<sub>         嵌套子资源
/api/v1/<resource>/{id}:action        动作(用冒号分隔,RPC 风格)
```

例:

```
GET    /api/v1/workspaces                       # 列出
POST   /api/v1/workspaces                       # 创建
GET    /api/v1/workspaces/{id}                  # 详情
PATCH  /api/v1/workspaces/{id}                  # 部分更新
DELETE /api/v1/workspaces/{id}                  # 删除
POST   /api/v1/workspaces/{id}:archive          # 归档(动作)
POST   /api/v1/workspaces/{id}:purge            # 彻底删除
GET    /api/v1/workspaces/{id}/characters       # 列出工作区下角色
POST   /api/v1/chapters/{id}:rewrite            # 整章重写
POST   /api/v1/chapters/{id}:revise             # 局部修订
```

---

## 3. 响应格式

### 3.1 成功响应

```json
{
  "data": { ... },                  // 资源对象 或 数组
  "meta": {                         // 可选,分页时必含
    "page": 1,
    "page_size": 20,
    "total": 142
  }
}
```

### 3.2 错误响应

```json
{
  "error": {
    "code": "WORKSPACE_NOT_FOUND",  // 机器可读
    "message": "工作区不存在",       // 人类可读
    "details": { ... },             // 可选,字段级错误等
    "trace_id": "v1c012-rewrite0",  // 可选,关联日志
    "request_id": "req_abc123"      // 必含,用于排障
  }
}
```

HTTP 状态码:
- 200/201 成功
- 400 请求参数错误
- 401 未授权(开启 auth 时)
- 403 操作不允许
- 404 资源不存在
- 409 状态冲突(如对已归档工作区做激活)
- 422 业务校验失败(Pydantic 校验失败)
- 429 频率限制
- 500 服务端错误
- 503 依赖服务不可用(如 Claude API 不通)

### 3.3 错误码命名

`<DOMAIN>_<REASON>`,大写下划线:

- `WORKSPACE_NOT_FOUND`
- `CHAPTER_INVALID_STATE`
- `WRITER_PROFILE_INVALID`
- `MEMORY_CONFLICT`
- `LLM_PROVIDER_UNAVAILABLE`
- `RATE_LIMIT_EXCEEDED`
- `COST_GUARD_TRIGGERED`

错误码列表维护在 `src/api/errors.py`,前端 `web/src/api/errors.ts` 同步。

---

## 4. 核心资源与流程概览

下面只列**核心资源 + 关键流程**,具体接口见运行时 OpenAPI(`/api/v1/openapi.json`)。

### 4.1 资源清单

| 资源 | URL 前缀 | 说明 |
|------|---------|------|
| Workspaces | `/workspaces` | 工作区 |
| Outline | `/workspaces/{id}/outline` | 大纲(嵌套资源) |
| Volumes | `/workspaces/{id}/volumes` | 卷规划 |
| Characters | `/workspaces/{id}/characters` | 人设 |
| PlotThreads | `/workspaces/{id}/threads` | 伏笔 |
| Chapters | `/chapters` | 章节(顶级,id 含 workspace) |
| ChapterCards | `/chapters/{id}/card` | 章节卡 |
| CoreStateAudit | `/chapters/core-state` | 当前核心状态、书内时钟与候选/revision 审计 |
| Reviews | `/chapters/{id}/reviews` | 审稿记录 |
| Versions | `/chapters/{id}/versions` | 章节版本 |
| WriterProfiles | `/profiles/writer` | 写作 Profile |
| Pipeline | `/pipeline` | 运行控制 |
| Metrics | `/workspaces/{id}/metrics` | 指标 |
| Logs | `/logs` | 日志 |
| Config | `/config` | 配置 |
| Exports | `/workspaces/{id}/exports` | 导出 |

### 4.2 关键流程 1:创建新书并生成大纲

```
1. POST /api/v1/workspaces
   body: { display_name, genre, premise, total_volumes, chapters_per_volume, ... }
   → 201 Created, data: Workspace

2. POST /api/v1/workspaces/{id}/outline:generate
   body: { tension_template, ... }
   → 202 Accepted, data: { task_id }

3. WS 订阅 task.{task_id}.progress
   推送 stage: "world" | "characters" | "plot" | "volume_plans" | "mini_plan" | "done"

4. GET /api/v1/workspaces/{id}/outline
   → 200, data: { world, characters, plot, volume_plans, mini_plan }

5. (用户编辑后)
   PUT /api/v1/workspaces/{id}/outline
   或 POST /api/v1/workspaces/{id}/outline:regenerate-section
       body: { section: "characters", keep_others: true }

6. POST /api/v1/workspaces/{id}/outline:commit
   → 200, 工作区进入可写章节状态
```

### 4.3 关键流程 2:写一章

```
1. POST /api/v1/chapters/{id}/card:generate
   → 200, data: ChapterCard(草稿状态)
   高价值章的 `dramatic_source` 由服务端从当前 volume/beat/mini-plan typed source
   确定性冻结；普通/legacy 章为 null。客户端不得生成、改写或从自由文本推断该字段。

2. (审核章节卡,如果开启)
   GET  /api/v1/chapters/{id}/card
   PUT  /api/v1/chapters/{id}/card
   POST /api/v1/chapters/{id}/card:approve
   contract v1 卡在生成/批准时冻结当前核心状态 projection hash、合法迁移和
   post-v8 C2 typed 书内时间节点/anchor/closed relations/chain hash；来源漂移时批准失败。
   `exact`/`bounded_interval` 只保存原文明示数值，近似量不派生 bounds
   post-v8 A1 卡同时复核 `dramatic_source.source_hash` 与当前 outline；typed source
   缺失、漂移或被客户端丢弃时批准失败，且不发生 Provider 请求
   post-v8 C1 承重物件必须沿 plot→beat→mini-plan 逐对象同源；每个进入当前 beat 的
   物件至少有一条 `possession|control|access` 迁移。关系两端只认 stable ID，错
   source/hash/from/target 或缺迁移均在 Provider 前失败

3. POST /api/v1/chapters/{id}:write
   body: { writer_profile_override?: "cloud_premium" }
   → 202 Accepted, data: { task_id }

4. WS 订阅 chapter.{chapter_id}.events
   推送: state_transition / token_progress / review_done / verdict / ...

5. GET /api/v1/chapters/{id}
   → 200, 完整 Chapter 对象
   GET /api/v1/chapters/{id}/versions/{ver}
   → 某版本正文

6. approved final 与 ChapterSummary 形成后、ready 水位前，同事务提交核心候选和
   versioned typed 书内时钟 snapshot；卡外变化、证据/node/chain/hash 漂移或并发冲突
   不得 ready。Continue 只幂等补交
   已存在 final/summary 的缺失提交，不重放 Writer、Reviewer 或 Judge。
```

核心状态审计接口（均要求 `X-Workspace-Id`）：

```
GET  /api/v1/chapters/core-state
→ 200, data: {
     projection: {
       facts: [...],
       book_clock,                    # legacy v0 numeric fallback，原义不变
       book_time_contract_version,
       book_time: { current_node, latest_mainline_node_id,
                    latest_absolute_anchor, active_relation_chain,
                    relation_chain_hash } | null,
       projection_hash
     },
     items: [...]  # active/superseded/retracted revision、角色事实、未采纳候选
   }
```

审计资源只读。批准来源若有误，必须从对应章节执行既有 Rewind；对象、角色、时钟、
来源正文及后续派生结果按同一章节边界统一回退，不提供单条 revision 撤回。
`possession`、`control`、`access` 分别作为独立 `attribute` 返回，关系值为原子角色
stable ID；不得编码成复合字符串。历史 `holder` 只读兼容并可原样审计，新 PUT、
Card/Agent 输出与 revision 提交均拒绝写入。本卡不新增 endpoint 或请求动作。

ChapterCard 的 `book_clock_constraint` additive 暴露 `contract_version`、`kind`、
`node_id`、`node_chapter_id`、`anchor_node_id`、`relations`、`day_phase`、
`approximate_quantity`、`unanchored_reason`、`relation_chain_hash`。v0 响应继续保留旧字段且不回填
新字段；v1 的人类展示只使用 `time_gap` 原文与关系含义，不把内部 numeric projection 当成
正文格式要求。ordered/unanchored 的 `book_clock=null` 不等于丢失 anchor，权威状态在
`book_time`；不新增编辑、修锚或强制通过动作。

`generation.core_state_hard_enabled` 默认 `true`；PUT 工作区 generation 配置可设
`false`，只影响后续新生成/新批准卡，不回填、不删除既有审计历史。

### 4.4 关键流程 3:章节修订/重写

```
重写整章:
POST /api/v1/chapters/{id}:rewrite
body: { writer_profile_override?, reason? }
→ 202, task_id

局部修订:
POST /api/v1/chapters/{id}:revise
body: { issues: [ReviewIssue...], writer_profile_override? }
→ 202, task_id

手动编辑(保存为新版本):
POST /api/v1/chapters/{id}/versions
body: { content, notes }
→ 201, ChapterVersion
```

### 4.5 关键流程 4:批量运行

```
POST /api/v1/pipeline:start
body: {
  scope: "single" | "range" | "volume" | "until",
  start_chapter_id: "v1c012",
  end_chapter_id: "v1c020",     # range 用
  volume_id: "v2",              # volume 用
  writer_profile_override?: "..."
}
→ 202, data: { run_id }

POST /api/v1/pipeline:pause
POST /api/v1/pipeline:resume
POST /api/v1/pipeline:stop
GET  /api/v1/pipeline/status
WS 订阅 pipeline.events
```

### 4.6 关键流程 5:工作区生命周期

```
POST /api/v1/workspaces/{id}:archive       → 归档(打包)
POST /api/v1/workspaces/{id}:clean         → 清理运行时数据
POST /api/v1/workspaces/{id}:purge
  body: { confirm: "<workspace_id>" }      → 彻底删除(必须传名字匹配)
POST /api/v1/workspaces/{id}:backup        → 立即备份
POST /api/v1/workspaces/{id}/exports
  body: { format: "epub" | "md" | "docx" | "chaptered_md", scope: "full" | "volume" | "range", ... }
  → 202, task_id, 完成后 GET /api/v1/workspaces/{id}/exports/{export_id}/download
```

---

## 5. WebSocket 协议

### 5.1 连接

`ws://127.0.0.1:8000/api/v1/ws`

连接后发送订阅消息:

```json
{ "type": "subscribe", "channels": ["pipeline.events", "chapter.v1c012.events"] }
```

允许多次订阅/取消订阅:

```json
{ "type": "unsubscribe", "channels": ["chapter.v1c012.events"] }
```

### 5.2 消息格式

服务端推送统一格式:

```json
{
  "channel": "chapter.v1c012.events",
  "event": "state_transition",
  "timestamp": "2026-05-02T14:30:00.123Z",
  "data": { ... }
}
```

### 5.3 标准频道

| 频道 | 事件 | 数据 |
|------|------|------|
| `pipeline.events` | started/paused/resumed/stopped | { run_id, scope, ... } |
| `pipeline.events` | chapter_started/chapter_finished | { chapter_id, status, score? } |
| `chapter.{id}.events` | state_transition | { from, to } |
| `chapter.{id}.events` | token_progress | { input, output, current_str_preview } |
| `chapter.{id}.events` | review_done | { review_id, verdict, score } |
| `task.{id}.progress` | stage | { stage, progress: 0-1 } |
| `task.{id}.progress` | complete | { result_id } |
| `task.{id}.progress` | error | { error: ApiError } |
| `system.alerts` | cost_guard_triggered | { current_usd, limit_usd } |
| `system.alerts` | memory_health_critical | { workspace_id, issues } |

### 5.4 心跳

服务端每 30s 发 `{ "type": "ping" }`,客户端回 `{ "type": "pong" }`。两次未回 → 服务端关连接。

---

## 6. 异步任务模式

长操作(大纲生成、写作、导出、备份)统一返回 task_id,客户端通过 WS 订阅进度。

任务状态查询:

```
GET /api/v1/tasks/{task_id}
→ 200, data: { task_id, type, status: "pending|running|done|failed", progress, result_url?, error? }
```

任务结果(如导出文件)通过专用 URL 下载:

```
GET /api/v1/workspaces/{id}/exports/{export_id}/download
→ 200, 文件流
```

---

## 7. 认证

### 7.1 默认模式

- `API_AUTH_ENABLED=false`(默认)→ 无认证
- 仅允许绑定 `127.0.0.1`,启动时校验

### 7.2 启用 Token 认证

- `API_AUTH_ENABLED=true` + `API_AUTH_TOKEN=xxx`
- 客户端在 Header 加 `Authorization: Bearer xxx`
- WebSocket 连接 URL 带 `?token=xxx`

### 7.3 不实现的

- v1 不实现多用户、不实现 OAuth、不实现 RBAC
- 这些都是 v2+ 的事

---

## 8. 限流与成本守卫

### 8.1 接口限流

写作类接口(`:write`/`:rewrite`/`:revise`/`outline:generate`)设全局并发上限:

- 同一时间只允许 1 个章节在写作中(避免并发污染共享记忆库)
- 每章每轮固定单稿，Writer 至多调用一次；旧多稿卡恢复也只占用并执行这一条章节链
- `ChapterCard` 新请求不得携带已退休的 `draft_strategy`；旧磁盘 JSON 仅由历史读取兼容层折叠
- `ChapterCard.dramatic_source` 是可空、服务端拥有的 typed source binding。PUT 客户端
  必须原样 round-trip；服务端在 approve/write/rewrite/revise/Continue 前逐字段复核，
  不接受自由文本 marker、关键词解析或位置协议替代 typed 字段
- OpenAPI 必须暴露 `VolumeDramaticPromise`、`LoadBearingObjectContract`、`DramaticBeatContract`、
  `DramaticSourceBinding` 的 closed object schema；前端类型只能由 OpenAPI 重生
- 超出返回 429 + 错误码 `PIPELINE_BUSY`

### 8.2 成本守卫

每次 LLM 调用前检查累计成本,超过 `cost_guard.daily_usd_limit` 触发:

- 默认行为:返回 402 + 错误码 `COST_GUARD_TRIGGERED`,流水线自动暂停
- 用户可在 ConfigPage 调阈值或设置 `action_on_exceed: warn`

---

## 9. 字段命名约定

- 全部 snake_case(JSON 字段)
- 时间字段后缀 `_at`,值 ISO 8601 UTC(`2026-05-02T14:30:00Z`)
- 时长字段后缀 `_seconds` 或 `_ms`
- 金额字段后缀 `_usd`,精度小数点后 4 位
- ID 字段无前缀,统一 `id`、`workspace_id`、`chapter_id`(不要 `workspaceId` 或 `workspaceID`)
- 布尔字段用 `is_/has_/should_` 前缀(`is_arc_finale`、`has_unsaved_changes`)

---

## 10. 版本演进规则

- v1 周期内,**字段只能增不能删**
- 删除字段要走"deprecated → v2 移除"流程
- 字段语义变更必须出 v2 接口,v1 保留至 v2 稳定 3 个月后移除
- 错误码只增不删

---

## 11. Claude Code 实施要点

1. **先定 Pydantic schema 再写路由**。schema 在 `src/api/schemas/` 下按资源分文件。
2. **路由薄,业务在 service**。路由代码 ≤ 30 行/接口,只做参数校验 + 调 service + 包装响应。
3. **不允许在路由里写 SQL 或调 LLM**。这些必须在 service 或更下层。
4. **所有路由都要写 summary + description + response_model**(FastAPI 装饰器)。
5. **生成 TypeScript 类型**:`pnpm gen-types` 是前端 build 的前置步骤,后端 schema 改动后必须重新生成。
6. **错误统一**:不在路由里 raise HTTPException 写死字符串,而是 raise 自定义 `ApiError` 子类(如 `WorkspaceNotFound`),由全局异常处理器转成 §3.2 格式。
7. **WebSocket 不传业务数据**,只通知"有事发生",前端收到通知后再用 REST 拉详情。例外:token 进度、流式日志这种高频小数据可直接 WS 传。

---

## 12. 验收清单

- [ ] OpenAPI 在 `/api/v1/openapi.json` 可访问,Swagger UI 在 `/docs` 可用
- [ ] 所有路由有 summary/description/response_model
- [ ] 所有错误走统一格式,有 trace_id/request_id
- [ ] WebSocket 心跳工作正常,断线重连可恢复订阅
- [ ] 限流与成本守卫工作正常
- [ ] TypeScript 类型从 OpenAPI 自动生成,无手写
- [ ] CLI 与 API 共用 service 层,功能等价

## 2026-10-02 篇幅策略与编辑导出

维护人批准的规则变更：ChapterCard 与 generation 增加 word_count_policy_version（0/1）。
旧卡缺省 0 保留历史合同；新配置 1，篇幅仍是正常写作要求，写后不按字数阻断。
既有 chapter-cards:reset-word-count 可显式传该版本，省略保持卡片原值；范围及版本一同回显。
ExportCreateIn.include_length_notes 默认 true，为超出要求上限两倍的章节标题附纯程序编辑提示；
false 是纯净导出。原始标题、正文和记忆不变，旧 manifest 没有该字段时不追补批注。
