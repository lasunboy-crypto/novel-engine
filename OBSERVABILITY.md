# OBSERVABILITY.md
# 日志、监控与可观测性规格

> 版本: v1.1
> 关联: PROJECT_SPEC.md / API_CONTRACT.md / FRONTEND_SPEC.md
> 原则: 系统可以慢、可以贵,但不能黑盒。任何一次产出异常都必须能在 5 分钟内定位到原因。
> v1.1 变更:增加 API 调用日志、WebSocket 事件、前端错误上报、记忆健康度追踪。

---

## 1. 三大可观测层

| 层 | 关注 | 形态 |
|---|------|------|
| Logs | 发生了什么 | structlog JSON,落盘 + 控制台 |
| Metrics | 系统跑得怎么样 | SQLite 聚合表 + API 暴露 + 前端图表 |
| Traces | 一次请求经过了哪些环节 | 章节 trace_id + API request_id 双轨 |

不引入 Prometheus/Grafana。MVP 用 SQLite + 前端图表足够。

---

## 2. 日志规范

### 2.1 技术选型

- 库:`structlog`
- 格式:JSON Lines(`.jsonl`),按天滚动
- 落盘:`workspaces/<name>/logs/YYYY-MM-DD.jsonl`(工作区维度) + `data/system_logs/YYYY-MM-DD.jsonl`(系统级)
- 控制台:开发模式彩色文本,生产模式 JSON
- 配置:`LOG_LEVEL` 环境变量(DEBUG/INFO/WARNING/ERROR)

### 2.2 必含字段

```json
{
  "timestamp": "2026-05-02T14:30:00.123Z",
  "level": "INFO",
  "logger": "agents.writer",
  "event": "chapter_draft_completed",
  "trace_id": "v1c012-rewrite0",
  "request_id": "req_abc123",
  "workspace_id": "scifi_book_1",
  "chapter_id": "v1c012",
  "module": "writer",
  "message": "人类可读描述"
}
```

### 2.3 trace_id 与 request_id

- **trace_id**:章节维度的追踪。格式 `{chapter_id}-{operation}{n}`,例:
  - `v1c012-rewrite0`、`v1c012-rewrite1`、`v1c012-revise0`
  - 一章从生成到 APPROVED 共享一个 trace_id 系列
- **request_id**:API 维度的追踪。格式 `req_<random12>`,每个 HTTP 请求一个
- 两者并存:API 触发的章节操作,日志同时带 trace_id 和 request_id;后台任务只有 trace_id

### 2.4 关键事件清单

#### 状态机事件
- `state_transition` - from_state, to_state, reason
- `human_intervention_required` - reason, chapter_id

#### LLM 调用事件
- `llm_request` - model, agent, input_tokens(预估), prompt_hash
- `llm_response` - model, agent, input_tokens, output_tokens, cost_usd, latency_ms, finish_reason
- `llm_retry` - attempt, error_type, error_message
- `llm_fallback` - from_model, to_model, reason
- `llm_streaming_chunk` - 仅 DEBUG 级,流式输出片段

#### Agent 事件
- `chapter_card_generated` - chapter_id, tension_level, must_achieve_count, plot_node_budget
- `retrieval_completed` - chapter_id, query_summary, candidates_count, after_rerank_count, top_similarity
- `chapter_draft_completed` - chapter_id, version_id, word_count, model_used
- `revision_completed` - chapter_id, version_id, revised_paragraphs_count
- `review_completed` - chapter_id, version_reviewed, score_overall, verdict, hard_constraints_passed
- `memory_extracted` - chapter_id, new_chars, new_threads, updated_chars, timeline_entries
- `memory_health_check` - workspace_id, severity, issues_count
- `style_audit` - workspace_id, drift_score, severity

#### 硬约束事件
- `validator_failed` - chapter_id, validator_name, details
- `validator_passed` - chapter_id, validator_name(DEBUG)
- `core_state_committed` - chapter_id, source_version_id, commit_hash,
  projection_after_hash, admitted_count, reaffirmed_count, soft_count,
  book_time_kind, book_time_node_id, book_time_relation_chain_hash
- `core_state_book_time_rewind_rebuilt` - workspace_id, target_chapter_seq,
  projection_hash, book_time_contract_version, book_time_relation_chain_hash
- contract v1 的投影/hash/证据漂移、HARD 密度超限和书内时钟 Judge 技术失败必须作为
  章节失败原因保留，不得记录成 validator pass 或 fail-open

核心状态的完整 revision、有效期、逐字证据、候选准入结论和 Rewind 撤回元数据以 SQLite
`core_state_revisions`、`chapter_summaries.core_state_candidates`、
`chapters.core_state_commit_*` 为耐久审计源；日志只记 hash、计数和身份，不复制正文。
`GET /api/v1/chapters/core-state` 提供当前投影与历史审计视图。
承重物件的 `possession`、`control`、`access` 沿用同一 revision/event，不新增事件类型；
日志与审计必须保留独立 attribute、object stable ID、from/to stable ID、source/hash 和
revision 身份，legacy `holder` 只读可见但不得记为新 admission。
post-v8 C2 的完整 typed node、最近 absolute anchor、active relation chain 与 source-level
approximate quantity 只存既有 revision `value_json`/commit details；日志只记 kind、node/hash
身份，不复制自然语言链或正文。exact/bounds/closed relation/真正倒序冲突使用既有 validator
失败面；无法证明的时间留白只保留既有 soft warning，不新增 Agent、调用、retry 或事件类型。

#### API 事件 [新增]
- `api_request` - method, path, request_id, status_code, latency_ms, user_agent
- `api_error` - method, path, request_id, error_code, http_status
- `ws_connect` - client_id, channels(初始订阅)
- `ws_subscribe` - client_id, channels
- `ws_disconnect` - client_id, reason

#### 前端错误上报 [新增]
- `frontend_error` - error_type, message, stack(前 1000 字), url, user_action, browser

#### 系统事件
- `system_startup` - version, config_hash
- `config_reloaded` - changed_keys
- `database_migration` - workspace_id, from_version, to_version
- `workspace_lifecycle` - workspace_id, action(create/archive/clean/purge/backup)
- `cost_guard_triggered` - workspace_id, current_usd, limit_usd, action

### 2.5 敏感信息脱敏

必须脱敏:
- `api_key` / `*_key` / `*_secret` / `*_token` → `***REDACTED***`
- 完整 prompt → 落 `prompt_hash`(SHA256 前 12 位),内容存 `workspaces/<name>/archives/prompts/{hash}.txt`
- 完整正文 → 不落日志(已存 chapters/)
- 用户 IP / 浏览器 UA → API 日志保留前两段,后段脱敏(`192.168.***.***`)

### 2.6 日志查询(API + CLI)

API:

```
GET /api/v1/logs?workspace_id=&chapter_id=&trace_id=&request_id=&event=&level=&since=&until=&page=&page_size=
GET /api/v1/logs/trace/{trace_id}              # Trace 视图
WS  /api/v1/logs/stream                         # 实时日志流
```

CLI(沿用 v1.0):

```
novel logs --chapter v1c012
novel logs --trace v1c012-rewrite1
novel logs --request req_abc123
novel logs --event llm_response --since 1h
novel logs --level ERROR --since 24h
novel logs --tail
```

---

## 3. 指标体系

### 3.1 SQLite 指标表

每个工作区独立的 `novel.db` 中包含:

```sql
CREATE TABLE metrics_daily (
    date TEXT PRIMARY KEY,
    chapters_drafted INTEGER,
    chapters_approved INTEGER,
    chapters_rewritten INTEGER,
    chapters_revised INTEGER,                  -- [新增]
    chapters_escalated INTEGER,
    avg_review_score REAL,
    avg_rewrites_per_chapter REAL,
    avg_revises_per_chapter REAL,              -- [新增]
    total_input_tokens INTEGER,
    total_output_tokens INTEGER,
    total_cost_usd REAL,
    avg_chapter_latency_seconds REAL,
    qwen_calls INTEGER,
    claude_sonnet_calls INTEGER,
    claude_opus_calls INTEGER
);

CREATE TABLE metrics_per_chapter (
    chapter_id TEXT PRIMARY KEY,
    drafted_at TIMESTAMP,
    approved_at TIMESTAMP,
    total_latency_seconds REAL,
    rewrite_count INTEGER,
    revise_count INTEGER,                      -- [新增]
    parallel_drafts_used BOOLEAN,              -- [历史只读列;新生产恒为默认 false,API 不输出]
    final_score_overall REAL,
    final_score_plot_progression REAL,
    final_score_tension_match REAL,
    final_score_prose_quality REAL,
    final_score_consistency REAL,
    final_score_voice_consistency REAL,        -- [新增]
    final_score_hook_management REAL,
    word_count INTEGER,
    target_word_count_min INTEGER,
    target_word_count_max INTEGER,
    target_tension_level INTEGER,
    actual_tension_estimate REAL,
    total_tokens INTEGER,
    total_cost_usd REAL,
    writer_profile TEXT,                       -- [升级]使用的 profile id
    writer_model TEXT,
    writer_calls INTEGER,
    reviewer_calls INTEGER,
    reviser_calls INTEGER                      -- [新增]
);

-- [新增] 系统级指标(跨工作区)
CREATE TABLE system_metrics_daily (
    date TEXT PRIMARY KEY,
    api_requests INTEGER,
    api_errors INTEGER,
    avg_api_latency_ms REAL,
    ws_connections INTEGER,
    frontend_errors INTEGER,
    pipeline_runs INTEGER
);

-- [新增] 记忆健康度历史
CREATE TABLE memory_health_history (
    check_id TEXT PRIMARY KEY,
    checked_at TIMESTAMP,
    severity TEXT,                             -- ok/minor/warning/critical
    stale_threads_count INTEGER,
    inactive_characters_count INTEGER,
    conflicts_count INTEGER,
    report_json TEXT
);

-- [新增] 风格漂移历史
CREATE TABLE style_audit_history (
    audit_id TEXT PRIMARY KEY,
    audited_at TIMESTAMP,
    chapter_range_start TEXT,
    chapter_range_end TEXT,
    drift_score REAL,
    severity TEXT,
    report_json TEXT
);
```

### 3.2 API 暴露

```
GET /api/v1/workspaces/{id}/metrics/overview        # 概览卡片数据
GET /api/v1/workspaces/{id}/metrics/daily?since=&until=
GET /api/v1/workspaces/{id}/metrics/per-chapter?volume=&order=
GET /api/v1/workspaces/{id}/metrics/tension-curve   # 目标 vs 实际
GET /api/v1/workspaces/{id}/metrics/cost-breakdown  # 按模型分组
GET /api/v1/workspaces/{id}/metrics/memory-health
GET /api/v1/workspaces/{id}/metrics/style-audits
GET /api/v1/system/metrics                          # 系统级
```

### 3.3 关键阈值与告警

| 指标 | 阈值 | 告警方式 |
|------|------|---------|
| 单章 rewrite_count | ≥ 2 | 日志 WARNING + 前端徽章 |
| 单章 revise_count | ≥ 2 | 日志 WARNING |
| 连续 3 章 final_score_overall < 7.5 | 触发 | 日志 WARNING + WS 推送 system.alerts |
| 单日 total_cost_usd > daily_usd_limit | 触发 | 流水线自动暂停 + WS 推送 |
| 张力实际 vs 目标差 > 2 | 触发 | 日志 WARNING |
| 平均章节耗时 > 10 分钟 | 触发 | 日志 WARNING |
| 任一硬约束失败率 > 30% | 触发 | 日志 ERROR |
| 记忆健康度 critical | 触发 | 流水线自动暂停 + 强制人工介入 |
| 风格漂移 critical | 触发 | 流水线自动暂停 + 强制人工介入 |
| API 错误率 5min > 10% | 触发 | 日志 ERROR + WS 推送 |
| 前端错误 1h > 20 | 触发 | 日志 WARNING |

### 3.4 成本守卫配置

```yaml
cost_guard:
  daily_usd_limit: 20.0
  monthly_usd_limit: 300.0
  per_workspace_daily_limit: 10.0
  action_on_exceed: pause      # pause | warn | continue
  notify_via: [log, websocket]
```

---

## 4. 追踪(Trace)

### 4.1 章节级 Trace

API:`GET /api/v1/logs/trace/{trace_id}`,前端 LogsPage 提供"Trace 视图"。

输出示例:

```
[v1c012-rewrite0]  trace_id  workspace=scifi_book_1
14:30:01  state: PENDING_CARD → CARD_REVIEW
14:30:01  card generated (qwen-3.5, 1.2s, 1834 tokens)
14:32:15  card approved by user (request_id=req_a1b2c3)
14:32:15  state: CARD_REVIEW → PENDING_RETRIEVAL
14:32:16  retrieval: 20 candidates → 5 reranked (bge-m3 + qwen, 0.8s)
14:32:17  state: PENDING_RETRIEVAL → DRAFTING
14:32:17  llm_request (claude-sonnet-4-6, est. 3200 tokens in)
14:34:42  llm_response (3187 in / 4521 out, $0.082, 145.3s)
14:34:42  draft completed: 4203 words, version=1
14:34:42  state: DRAFTING → REVIEWING
14:34:43  llm_request (qwen-3.5)
14:35:11  llm_response (5102 in / 892 out, free, 28.4s)
14:35:11  validators: word_count ✓ must_achieve ✓ must_unresolved ✓ forbidden ✓ continuity ✓
14:35:11  review: score=7.1, verdict=revise (issues: 2)
14:35:11  state: REVIEWING → REVISING
14:35:12  llm_request (claude-sonnet-4-6, partial)
14:35:48  llm_response (2400 in / 1100 out, $0.024, 36.1s)
14:35:48  revision completed: version=2
14:35:48  state: REVISING → REVIEWING
14:35:49  ... (review 2)
14:36:15  review: score=8.4, verdict=pass
14:36:15  state: REVIEWING → PENDING_EXTRACTION
14:36:16  memory extracted: 0 new chars, 2 new threads, 1 updated, 3 timeline
14:36:38  state: PENDING_EXTRACTION → APPROVED

总耗时: 6min 37s
总成本: $0.106
最终版本: 2
重写: 0   修订: 1
```

### 4.2 API 级 Trace

`GET /api/v1/logs/trace/{request_id}` 输出该 request 触发的所有日志(可能包含一个 chapter trace)。

---

## 5. Prompt 与产出归档

```
workspaces/<name>/archives/
  v1c012/
    rewrite_0/
      chapter_card.json
      retrieved_memories.json
      writer_prompt.txt
      writer_response.txt
      chapter_content.md
      reviewer_prompt.txt
      reviewer_response.json
    revise_0/                          # [新增 v1.1]
      issues.json
      reviser_prompt.txt
      reviser_response.txt
      chapter_content.md
    parallel_drafts/                   # 历史只读目录;新生产不再创建或续写
    memory_extractor_prompt.txt
    memory_extractor_response.json
    final.md
    meta.json
```

可压缩:`POST /api/v1/workspaces/{id}/archives:compress?volume=1`。

---

## 6. 健康检查

CLI: `novel doctor`,API: `GET /api/v1/system/health`

检查项:

- [ ] 配置文件加载成功
- [ ] 当前激活工作区 SQLite 可读写
- [ ] 当前激活工作区 Chroma 可读写
- [ ] Qwen endpoint 可达
- [ ] Claude API 可达
- [ ] Embedding 模型可加载
- [ ] 日志目录可写
- [ ] 磁盘空间 > 1GB
- [ ] 引擎版本与工作区版本一致(或可迁移)
- [ ] 备份目录健康

输出 JSON 报告,前端 ConfigPage 展示。

---

## 7. 调试模式

`LOG_LEVEL=DEBUG` 时额外输出:

- LLM 调用完整 prompt(脱敏后)
- 状态机判定逻辑
- 检索的相似度分数列表
- validator 判定细节
- API 请求体(脱敏)
- WS 全部消息

DEBUG 模式仅本地调试用,不在生产长跑(磁盘大)。

---

## 8. 前端错误上报 [新增]

前端通过全局 ErrorBoundary + window.onerror + Promise rejection 捕获错误,POST 到 `/api/v1/system/frontend-errors`。

服务端落 `frontend_error` 事件日志。

---

## 9. 验收清单

- [ ] structlog 配置完成,所有模块统一 logger
- [ ] §2.4 所有事件有埋点
- [ ] 日志脱敏单测通过
- [ ] SQLite 指标表迁移完成(各工作区 + 系统级)
- [ ] 每章 APPROVED 后写入 metrics_per_chapter
- [ ] 每日聚合任务工作(APScheduler 或 cron)
- [ ] §2.6 API + CLI 命令实现并测试
- [ ] `novel doctor` / `GET /system/health` 实现
- [ ] 归档目录按 §5 落地
- [ ] 成本守卫超限时按配置行为执行
- [ ] WebSocket 系统告警频道工作
- [ ] 前端错误上报工作

2026-10-02 维护人批准：篇幅区间仍是写前正常任务，默认 2500–3500；采用 v1 后，
写后字数只用于统计。特别长的稿件在标题后附编辑提示，阈值为章节卡上限两倍；
提示不计入失败、改写、正文或记忆。查看与编辑导出提供提示，纯净导出可关闭。
