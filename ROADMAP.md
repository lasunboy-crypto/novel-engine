# ROADMAP.md
# 分阶段开发计划(双轨并行)

> 版本: v1.1
> 关联: PROJECT_SPEC / FRONTEND_SPEC / API_CONTRACT / OBSERVABILITY
> 原则: 每阶段后端 + 前端必须能联调跑通端到端流程,不允许"先把后端全做完再做前端"。

---

## 阶段总览

| 阶段 | 后端 | 前端 | 估时 |
|------|------|------|------|
| P1 | 最小闭环 + 工作区 + 章节版本 + 衔接桥 + revise | 工作区页 + 章节阅读编辑页 | 7-10 天 |
| P2 | 向量记忆库 + voice profile + 静态资产导入 | 人设页 + 伏笔页 | 6-8 天 |
| P3 | Writer Profile + 模型路由 | 配置页 + Profile 切换 | 3-5 天 |
| P4 | 完整可观测性 + 硬约束 + 记忆健康度 + 风格漂移接口 | 指标页 + 日志页 + 状态看板 | 6-8 天 |
| P5 | OutlineBuilder + 卷规划 + 章节卡生成 | **新书启动页** + 大纲页 + 张力曲线编辑 | 6-8 天 |
| P6 | 人工介入流程完善 + 导出 + 备份恢复 | 章节卡审核页 + 运行控制 + 导出归档 | 5-7 天 |
| P7 | 持续调优 + 风格漂移完整实现 | UI 美化 + 多读者视角(可选) | 持续 |

总计 v1 约 **33-46 天**(让 Claude Code 写大部分代码的前提下)。

### 当前 post-v8 串行改进状态（2026-08-27）

| 卡 | 状态 |
|---|---|
| A1 上游 typed 戏剧承诺 | DONE |
| C1 承重物件 identity + possession/control/access | DONE |
| C2 exact/bounded/ordered/unanchored 书内时间锚链 | DONE（Provider 0；无 DB/Alembic） |
| A2 Writer 输入显著性 | READY；下一唯一游标 |
| B0 Provider 0 去重 | QUEUED；不得提前启动 |

上述工程状态不等于 Provider、真人文学、生产或全新 12 章验收；第二阶段仍未授权。

---

## P1: 最小闭环 + 工作区基础

### P1 后端

**目标**:跑通"工作区 → 单章节 → 写 → 审 → 修订/重写 → 通过 → 落盘"全流程。文件式记忆,人工写章节卡。

**范围**:
- [ ] 项目初始化、目录结构、`.env.example`、基础 README
- [ ] `src/version.py`,工作区 meta.json 机制
- [ ] Pydantic 模型:Workspace、ChapterCard(含 ContinuityContext)、Chapter、ChapterVersion、ReviewResult、ReviewIssue
- [ ] `services/workspace_service.py`:create / list / switch / info / clean / archive / purge / backup
- [ ] `services/chapter_service.py`:write / rewrite / revise / 状态查询
- [ ] LLM client:`claude_client.py`(P3 加 openai_compatible)
- [ ] Writer agent(简版提示词)
- [ ] Reviser agent(局部修订)
- [ ] Reviewer agent(简版,但已经支持 revise/rewrite/pass 三档判决)
- [ ] 简易 Orchestrator + 状态机(支持 REVISING)
- [ ] 文件记忆:静态资产 markdown(world/characters)
- [ ] 章节版本控制(每次写作产生新 version)
- [ ] 衔接桥逻辑:从上一章正文自动抽取 continuity_context(用 Qwen 或简单规则,P1 简单规则即可)
- [ ] FastAPI 框架搭建,实现核心 API:
  - workspaces CRUD + lifecycle 动作
  - chapters write/rewrite/revise + 状态查询 + 版本管理
  - WebSocket 连接框架(基础事件推送)
- [ ] CLI 共用 services 层:`novel workspace ...`、`novel write-chapter ...`
- [ ] alembic 初始化

**范围外**:章节卡生成(本阶段人工)、向量记忆、Qwen、完整可观测、多 Profile、并行草稿、记忆健康、风格漂移

### P1 前端

**目标**:能在浏览器里管理工作区、查看章节、触发写作。

**范围**:
- [ ] Vite + React + TS + Tailwind + shadcn 初始化
- [ ] OpenAPI 类型生成脚本
- [ ] 路由框架、AppShell、全局 Toast、ConfirmDialog、UnsavedChangesGuard
- [ ] WebSocket 客户端(连接 + 心跳 + 断线重连)
- [ ] WorkspacesPage(完整功能:列表 / 切换 / 创建简版表单 / 归档 / 清理 / 删除 / 备份)
- [ ] 工作区主页(简版状态看板:总章数 / 已完成 / 进行中)
- [ ] ChaptersBoardPage(列表视图)
- [ ] ChapterEditorPage(完整三栏:章节卡 / 正文 / 审稿历史 + 版本切换 + 触发重写/修订)
- [ ] 章节实时进度(WS 推送 token 增长)

### P1 验收

- [ ] 浏览器创建工作区,导入预制 world/characters md
- [ ] 浏览器手动上传第 1 章章节卡 JSON
- [ ] 浏览器点"开始写作",实时看到 token 进度
- [ ] 写完自动审稿,verdict=revise 时自动修订,verdict=rewrite 时自动重写
- [ ] 浏览器查看版本对比(v1 vs v2)
- [ ] 浏览器手动改章节卡某字段重新写,验证生效
- [ ] CLI 等价操作可用
- [ ] 工作区归档/清理/删除 / 备份/恢复全工作

---

## P2: 向量记忆库 + 角色声音

### P2 后端

**目标**:替换文件记忆为 Chroma + SQLite 双层,引入 voice profile。

**范围**:
- [ ] 完整 Pydantic 模型:Character(含 VoiceProfile)、PlotThread、TimelineEntry
- [ ] SQLite schema 迁移(各模型对应表)
- [ ] `memory/store.py`(SQLite 封装,CRUD)
- [ ] `memory/vector.py`(Chroma 封装,向量化与检索)
- [ ] Qwen3-Embedding-0.6B 集成
- [ ] `memory/retriever.py`:章节卡 → query → top_20(P3 引入重排)
- [ ] MemoryExtractor agent(自动运行)
- [ ] Writer 改造:接收检索记忆 + 当前章节出场角色的 voice_profile
- [ ] Reviewer 增加 score_voice_consistency 维度
- [ ] 静态资产导入工具:`novel workspace import-assets <md_dir>`
- [ ] API:characters / threads / timeline CRUD
- [ ] 衔接桥升级:用 Qwen 从上一章自动抽取 continuity_context(取代 P1 的简单规则)

### P2 前端

**目标**:完整人设和伏笔管理。

**范围**:
- [ ] CharactersPage(表格/卡片视图、详情抽屉、VoiceProfile 编辑器)
- [ ] PlotThreadsPage(表格、状态筛选、详情)
- [ ] 章节编辑器右栏增加"当前章节出场角色 voice 校验"提示

### P2 验收

- [ ] 导入静态资产,自动入 SQLite + Chroma
- [ ] 写完一章自动抽取记忆,新人物/伏笔/时间线入库
- [ ] 写下一章时检索结果包含上一章相关信息
- [ ] 连续写 3 章,人物名字、关系、伏笔无矛盾
- [ ] 人物对白若违反 voice forbidden_phrases,审稿能检出
- [ ] 前端可编辑人设和伏笔,改动反映到下次写作

---

## P3: Writer Profile + 模型路由

### P3 后端

**目标**:接 Qwen,实现 Profile 抽象；正文生产固定单稿。

**范围**:
- [ ] `llm/openai_compatible_client.py`(通用客户端)
- [ ] WriterProfile 数据模型 + config 加载
- [ ] `routing/router.py`:按 writer_routing 规则匹配 profile
- [ ] 所有 agent 改造为通过 Router 调用
- [ ] Reviewer 升级:Qwen 主审 + 边界(6-8 分)升级 Claude 复审
- [ ] Reranker 实现(20 → 5)
- [x] post-v6 退休草稿池；Writer 每轮一次、Reviewer 只审当前唯一稿
- [ ] 成本核算:实时累计到 SQLite

### P3 前端

**目标**:可视化 Profile,可临时切换。

**范围**:
- [ ] ConfigPage 简版:Profile 列表(只读 v1)+ Routing 规则展示
- [ ] WriterProfileSelector 组件,在 ChapterEditor 和 RunControl 中可临时覆盖
- [x] 章节卡与前端不再生成、编辑或展示多稿策略

### P3 验收

- [ ] ChapterCard / Reviewer / MemoryExtractor / Reranker 跑在本地 Qwen
- [ ] Writer 默认 Claude,arc_finale/tension≥9 自动切 Opus
- [ ] 单章 Claude 成本下降 ≥ 60%(对比 P2)
- [ ] Qwen JSON 失败率 < 5%,失败时自动降级 Claude
- [x] 新生产入口无法产生或接受多稿；旧多稿 workspace 可读但仅单路恢复
- [ ] 配置切换:project.yaml 设 default=local_qwen_32k 后,所有写作走本地

---

## P4: 完整可观测性 + 硬约束 + 健康检查

### P4 后端

**目标**:OBSERVABILITY 文档全部落地,validator 完整。

**范围**:
- [ ] structlog 配置,所有模块统一 logger
- [ ] OBSERVABILITY §2.4 所有事件埋点(含 API/WS/前端错误)
- [ ] 日志脱敏 + prompt hash 机制
- [ ] metrics 表迁移(工作区 + 系统级 + 健康度 + 风格漂移)
- [ ] 完整状态机持久化,断点续跑
- [ ] `rules/validators.py`:全部 [HARD] 规则(must_achieve/must_unresolved/forbidden/角色存在/时间线/死亡铺垫/voice forbidden/衔接桥)
- [ ] 归档目录按规范落地(含 revise_n；历史 parallel 归档只读保留)
- [ ] CLI/API:logs / stats / trace / doctor / metrics
- [ ] 成本守卫
- [ ] 阈值告警(WS system.alerts)
- [ ] MemoryHealthChecker agent
- [ ] StyleAuditor 接口预留(P7 完整实现)
- [ ] 前端错误上报接口
- [ ] API 限流 + cost guard

### P4 前端

**目标**:完整观测前端。

**范围**:
- [ ] MetricsPage(概览 + 各类图表 + 时间筛选)
- [ ] LogsPage(筛选 + 详情抽屉 + Trace 视图 + 实时模式)
- [ ] ChaptersBoardPage 升级:看板视图,WS 实时刷新
- [ ] 工作区主页升级:显示告警徽章、健康度评分
- [ ] 全局 ErrorBoundary + 错误上报

### P4 验收

- [ ] 写一章,所有日志带 trace_id + request_id,Trace 视图完整还原
- [ ] 故意构造违反 must_achieve 的草稿,validator 检出并触发 rewrite
- [ ] 故意构造时间倒流,validator 检出
- [ ] kill 进程,resume 从断点恢复
- [ ] doctor 全绿
- [ ] 前端 Metrics/Logs 页可用
- [ ] 故意触发前端错误,服务端能收到上报
- [ ] 成本超限自动暂停流水线 + WS 告警
- [ ] MemoryHealthChecker 跑出报告

---

## P5: 大纲构建 + 卷规划(你最期待的页面)

### P5 后端

**目标**:从一句话到完整大纲与张力曲线,自动生成章节卡。

**范围**:
- [ ] OutlineBuilder agent(对应提示词)
- [ ] 卷规划数据模型:Volume / Arc / TensionCurve
- [ ] 张力曲线模板系统(英雄之旅/平稳日常/强刺激爽文等)
- [ ] ChapterCardGenerator agent(替代 P1 人工卡)
- [ ] 张力曲线 [HARD] 校验(副本横跨规则)
- [ ] 异步任务系统:大纲生成走 task_id + WS 进度
- [ ] API:`outline:generate`、`outline:regenerate-section`、`outline:commit`、`volumes` CRUD

### P5 前端

**重点阶段,你最想要的网页**。

**范围**:
- [ ] **NewBookPage**(完整双窗口):
  - 左侧表单(所有预设选项)
  - 右侧分 tab 展示生成结果
  - WS 进度条(world → characters → plot → volume_plans)
  - 单 tab 重新生成
  - 确认提交
- [ ] OutlinePage(三 tab + markdown 编辑 + 历史版本)
- [ ] **VolumePlanPage**(张力曲线编辑器,Recharts + 拖拽 / 表格双向编辑 + 模板应用)
- [ ] 章节卡审核机制:前 3 章 + 每卷首章默认进入 CARD_REVIEW

### P5 验收

- [ ] 输入"一个废柴炼丹师在末法时代重生为顶级仙帝",前端实时看生成进度
- [ ] 生成出世界观 / 5-10 个核心人设 / 主线 / 第 1 卷张力曲线
- [ ] 在网页上拖拽张力曲线某章,数值更新
- [ ] 不满意人设 tab,单独重新生成,其他保留
- [ ] 确认后自动生成第 1-5 章卡片,前 3 章进入审核
- [ ] 全自动跑前 5 章,产出连贯

---

## P6: 人工介入完善 + 导出 + 备份

### P6 后端

**范围**:
- [ ] ChapterCard 编辑/批准/退回
- [ ] 强制通过 / rewind 章节(后续章节标废 + 记忆库回退)
- [ ] 流水线 pause/resume/stop + scope 选择(single/range/volume/until)
- [ ] 导出引擎(md/chaptered_md/epub/docx)
- [ ] 备份/恢复完整实现
- [ ] 数据迁移机制(version 比对 + 自动迁移)
- [ ] QUESTIONS.md / TROUBLESHOOTING.md 工作流

### P6 前端

**范围**:
- [ ] ChapterCardReviewPage(完整功能)
- [ ] RunControlPage(完整启动选项 + 实时输出 + 暂停继续 + 异常跳转)
- [ ] ExportPage(格式选择 + 范围 + 历史导出 + 归档清理删除)
- [ ] ConfigPage 完整(三层配置 + Profile 增删改 + Routing 规则编辑 + API key 管理)

### P6 验收

- [ ] 用户全程从浏览器:创建工作区 → 审核大纲 → 审核前 3 章卡 → 自动跑 → 中途 pause → 修改章节卡 → resume → rewind 重写 → 继续 → 导出 epub
- [ ] CLI 等价路径全工作
- [ ] 新用户照 README 30 分钟内跑出第 1 章

---

## P7: 持续调优 + 美化迭代

不设固定 deadline。关注:

- 长程一致性(30 章后)
- 张力实际 vs 设计的吻合度
- Reviewer 判断准确度(对照人工)
- 提示词版本迭代(prompts/archive)
- StyleAuditor 完整实现
- UI 美化(色板 / 字号 / 间距 / 暗色模式 / 动效)
- v2 wishlist 中的项,**仍然按需评估,不自动进入 v1**

---

## 跨阶段约束

### 每阶段开始

1. Claude Code 读 5 份核心文档
2. 写 `docs/plan/p<n>_plan.md`(后端/前端各自任务拆解)
3. 维护人确认计划再编码

### 每阶段结束

1. 运行该阶段验收
2. 维护人主观验收
3. 写 `docs/impl/p<n>_backend.md` / `docs/impl/p<n>_frontend.md`
4. git tag `v0.<n>.0`
5. 进入下阶段

### 双轨协同纪律

- **后端 schema 改动 → 立刻 OpenAPI 重生 → 前端 gen-types**(同会话内完成,不留隔夜)
- 同一阶段内,后端先做 1-2 个核心接口,前端立刻接;不要后端把接口全做完前端才动
- 跨阶段绝对禁止
- 任何"顺手做"必须先写 QUESTIONS.md

---

## 风险与备案

| 风险 | 应对 |
|------|------|
| Qwen JSON 不稳定 | P3 降级 Claude;严重时 outlines/instructor 库 |
| 16k 不够 | Reviewer 分段;MemoryExtractor 分段;Writer 长期用 Claude |
| 检索精度下降 | P7 引入混合检索(BM25 + 向量) |
| 成本超预期 | 调 max_tokens、Sonnet 替 Opus、压 prompt |
| 维护人审核瓶颈 | P6 后期逐步关闭审核(前 3 → 前 1 → 仅卷首) |
| 前后端字段不一致 | 强制 OpenAPI 自动生成,禁止手写 |
| 长流程中断丢数据 | P4 状态持久化 + 自动备份 |
| 工作区数据迁移失败 | 迁移前强制全量备份,有回滚 |

---

## v1 完成定义

满足以下全部即视为 v1 发布:

- [ ] P1-P6 全部验收通过
- [ ] 用一句话设定 → 完整 30 章产出,质量主观验收"可发布"
- [ ] 总成本符合预期(每章 < $0.5)
- [ ] 系统连续运行 7 天无人值守(每天写 3-5 章),无数据丢失
- [ ] 前端覆盖 FRONTEND_SPEC §4 所有 v1 必做页面
- [ ] CLI 与 API 功能完全等价
- [ ] 文档完整(README / TROUBLESHOOTING / impl 实现说明)

v1 后即可开始第二本书的实战,P7 调优与第二本书并行推进。

2026-10-02 篇幅规则修正已获维护人批准：章节卡保留 2500–3500 正常任务，v1 写后字数只计量，不作 HARD。
