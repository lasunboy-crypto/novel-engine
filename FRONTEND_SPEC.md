# FRONTEND_SPEC.md
# 前端规格文档

> 版本: v1.0
> 关联: PROJECT_SPEC.md §2.3 / API_CONTRACT.md
> 原则: **功能优先,美化后置**。v1 不追求设计感,但所有功能必须可用、状态必须清晰、操作不能丢数据。

---

## 1. 技术栈(强约束)

- React 18 + TypeScript(strict mode)
- Vite 构建
- Tailwind CSS
- shadcn/ui(组件库,**不主动美化、保持默认风格**)
- TanStack Query(数据请求与缓存)
- React Router v6(路由)
- Zustand(全局状态:当前工作区、用户偏好)
- Recharts(图表)
- React Hook Form + Zod(表单与校验)
- date-fns(日期处理)
- `openapi-typescript`(从后端 OpenAPI 自动生成 TS 类型,**禁止手写后端字段类型**)

不引入:Redux、MUI、Ant Design、Chakra UI、复杂动画库。

---

## 2. 目录结构

```
web/
├── package.json
├── vite.config.ts
├── tsconfig.json
├── tailwind.config.ts
├── index.html
└── src/
    ├── main.tsx
    ├── App.tsx
    ├── router.tsx
    │
    ├── api/                  # API 客户端
    │   ├── client.ts         # axios + 拦截器
    │   ├── ws.ts             # WebSocket 客户端
    │   └── types.ts          # 从 OpenAPI 自动生成
    │
    ├── pages/                # 页面级组件,与路由对应
    │   ├── WorkspacesPage.tsx
    │   ├── NewBookPage.tsx
    │   ├── OutlinePage.tsx
    │   ├── CharactersPage.tsx
    │   ├── PlotThreadsPage.tsx
    │   ├── VolumePlanPage.tsx
    │   ├── ChaptersBoardPage.tsx
    │   ├── ChapterCardReviewPage.tsx
    │   ├── ChapterEditorPage.tsx
    │   ├── RunControlPage.tsx
    │   ├── MetricsPage.tsx
    │   ├── LogsPage.tsx
    │   ├── ConfigPage.tsx
    │   └── ExportPage.tsx
    │
    ├── components/           # 可复用组件
    │   ├── layout/           # AppShell / Sidebar / Header
    │   ├── ui/               # shadcn/ui 组件(Button/Input/Dialog 等)
    │   ├── domain/           # 业务组件
    │   │   ├── TensionCurveChart.tsx
    │   │   ├── ChapterCardForm.tsx
    │   │   ├── CharacterCard.tsx
    │   │   ├── ChapterStatusBadge.tsx
    │   │   ├── DiffViewer.tsx          # 章节版本对比
    │   │   ├── CostBadge.tsx
    │   │   └── WriterProfileSelector.tsx
    │   └── feedback/         # Toast / ConfirmDialog / UnsavedChangesGuard
    │
    ├── hooks/
    │   ├── useWorkspace.ts
    │   ├── useChapter.ts
    │   ├── useWebSocket.ts
    │   ├── useUnsavedChangesPrompt.ts  # 未保存提示
    │   └── useStreamingProgress.ts     # WS 写作进度
    │
    ├── stores/               # Zustand
    │   ├── workspaceStore.ts
    │   └── uiStore.ts
    │
    ├── lib/
    │   ├── format.ts         # 字数/时间/金额格式化
    │   └── validators.ts     # Zod schemas
    │
    └── styles/
        └── globals.css
```

---

## 3. 路由

```
/                                  → 重定向到 /workspaces 或当前激活工作区
/workspaces                        → 工作区管理
/workspaces/new                    → 新书启动页
/w/:workspaceId/                   → 工作区主页(状态看板)
/w/:workspaceId/outline            → 大纲查看与编辑
/w/:workspaceId/characters         → 人设管理
/w/:workspaceId/threads            → 伏笔管理
/w/:workspaceId/volumes            → 卷规划与张力曲线
/w/:workspaceId/chapters           → 章节列表与状态看板
/w/:workspaceId/chapters/:id       → 章节阅读与编辑
/w/:workspaceId/cards/:id/review   → 章节卡审核
/w/:workspaceId/run                → 运行控制
/w/:workspaceId/metrics            → 指标
/w/:workspaceId/logs               → 日志
/w/:workspaceId/config             → 配置
/w/:workspaceId/export             → 导出与归档
```

未匹配路由 → 404 页。

---

## 4. v1 必做页面规格

每个页面给"必含元素 + 必含交互 + 数据来源"。

### 4.1 WorkspacesPage(工作区管理)

**必含元素**:
- 工作区列表(卡片或表格,默认表格):display_name、genre、status、completed/total 章数、最近活动时间
- "新建工作区"按钮 → 跳转 NewBookPage
- 每行操作:切换为激活、归档、清理、彻底删除、备份

**交互**:
- 切换激活:成功后路由跳到 `/w/:id/`
- 归档/清理/删除:必须 ConfirmDialog 二次确认,删除还要输入工作区名匹配
- 长操作(归档/备份)显示进度,期间禁用按钮

**数据**:`GET /api/workspaces`

### 4.2 NewBookPage(新书启动页)— 你最想要的那个

**布局**:左右双列。

**左侧表单**:
- 工作区标识(英文名,自动生成 + 可改)
- 显示名称
- 流派(下拉:玄幻/科幻/都市/历史/悬疑/言情/武侠/...,可自定义)
- 一句话设定(textarea,2-3 行)
- 目标卷数(默认 5)
- 每卷目标章数(默认 30)
- 单章字数范围(默认 2800-4500)
- 张力曲线模板(英雄之旅/平稳日常/强刺激爽文/自由)
- Writer Profile 默认选择(下拉,显示所有 profiles)
- 风格指南(可上传 .md 或在线编辑,**必填**)

**右侧预览/产出**:
- 顶部:进度条(idle / generating / ready / error)
- 生成结果分 tab 展示:
  - 世界观(可编辑 markdown)
  - 主线大纲(可编辑)
  - 核心人设(卡片列表,逐个可编辑)
  - 卷规划(每卷一张卡,显示 title + summary + 章数 + 张力曲线缩略图)
- 底部:"重新生成"(可选保留某些 tab) / "确认并保存" / "返回修改输入"

**交互**:
- "生成"按钮触发 `POST /api/outline/generate`,响应通过 WebSocket 推送进度(预计 1-3 分钟)
- 生成中:左侧表单禁用,右侧实时显示生成阶段("世界观生成中"→"人设生成中"→...)
- 任意 tab 可单独"重新生成此项",其他保留
- "确认并保存"前若有未保存修改,提示

**数据**:
- `POST /api/outline/generate`(异步,WebSocket 进度)
- `POST /api/workspaces/:id/commit-outline`(确认保存)

### 4.3 OutlinePage(大纲查看与编辑)

**必含**:
- 顶部 tabs:世界观 / 主线 / 卷规划
- 内容区可编辑(markdown 编辑器,左编辑右预览)
- "保存"按钮 + 自动保存提示
- 历史版本下拉(显示最近 10 次保存)

**数据**:`GET/PUT /api/workspaces/:id/outline`

### 4.4 CharactersPage(人设管理)

**必含**:
- 表格视图 + 卡片视图切换
- 每个角色显示:name、archetype、首次出场章节、最近更新章节
- 点击进入详情页(或抽屉),展示完整字段
- 详情页编辑模式:基础信息 / VoiceProfile / 状态 / 关系 / 秘密 各 tab
- "新增角色" / "删除角色"

**交互**:
- 删除前 ConfirmDialog,显示该角色被引用的章节数,引用 > 0 警告
- VoiceProfile 编辑使用专用表单(catchphrases 标签输入 / forbidden_phrases 标签输入)

**数据**:`/api/workspaces/:id/characters/...`

### 4.5 PlotThreadsPage(伏笔管理)

**必含**:
- 表格,字段:title、status、planted_chapter、planned_payoff、actual_payoff、importance
- 状态筛选(planted/developing/resolved/abandoned)
- 重要性筛选(1-5)
- 进度条:本书伏笔已兑现/未兑现比例
- 点击进入详情/编辑

**数据**:`/api/workspaces/:id/threads/...`

### 4.6 VolumePlanPage(卷规划与张力曲线)

**必含**:
- 卷列表(顶部 tabs 或左侧导航)
- 选中某卷时:
  - 卷信息(title / summary / 章数)
  - **张力曲线图(Recharts 折线图)**,X 轴章节序号,Y 轴张力值 1-10
  - 曲线下方:每章一行(章号、tension、is_arc_finale 标记)
  - 拖拽曲线点修改某章 tension(或在表格行直接改)
  - 曲线模板下拉,可一键应用模板
  - "保存"按钮

**交互**:
- 修改后实时重绘曲线
- 离开页面前未保存提示

**数据**:`GET/PUT /api/workspaces/:id/volumes/:vid`

### 4.7 ChaptersBoardPage(章节列表与状态看板)

**必含**:
- 三种视图:列表 / 看板 / 时间线
- 看板视图:列 = 状态(drafting/reviewing/revising/rewriting/approved/intervention),卡片 = 章节
- 列表字段:chapter_id、title、status、当前版本号、最后审稿分、字数、写作 profile、累计成本、最后更新时间
- 筛选:卷、状态、是否需要人工介入
- 点击章节进入 ChapterEditorPage

**实时**:WebSocket 推送状态变化,看板实时更新

### 4.8 ChapterCardReviewPage(章节卡审核)

**必含**:
- 章节卡完整内容展示(只读 + 进入编辑模式)
- 关键字段高亮:tension_level / must_achieve / must_unresolved / forbidden / continuity_context
- 正文生产固定单稿；页面不得提供双稿/三稿策略切换、候选比较或多稿重启入口
- 衔接桥单独一块(显示上一章末尾原文 + unfinished_actions)
- contract v1 卡显示“核心状态与书内时钟审计”只读面板：当前值、来源章/版本、逐字
  证据、有效期、typed 时间 kind、原始自然语言、closed relation/day phase、最近明确锚
  是否保留、本章获准 `from → to` 迁移，以及未采纳/冲突候选理由。ordered/unanchored
  不显示累计分钟；“约一小时”等只提示近似量不生成分钟上下界
- 承重物件关系必须按 `possession`（物理持有）、`control`（决策/授权控制）、`access`
  （当前可操作目标）三个独立属性显示，关系值展示对应角色 stable ID；不得合并成
  holder 文本或从地点/正文推断。legacy `holder` 仅以只读历史标签展示并原样 round-trip
- 审计面板不提供单条撤回按钮；批准来源有误时，对象、角色和书内时钟都必须走对应
  章节 Rewind，避免正文与当前状态只回退一半
- 书内时间面板不提供编辑、修锚、分钟换算或强制通过控件；legacy v0 仍按既有
  numeric/unknown fallback 显示，v1 不从自然文本反向重建 node/relation
- `dramatic_source` 为服务端拥有的可空 typed source；本阶段不新增编辑器、徽章或人工
  填写入口。页面用 OpenAPI 生成类型原样保留并 round-trip 该字段，不得丢弃、改写或从
  `must_achieve` 等自然文本重建；C1 的承重物件 catalog 与关系声明同样只读保留，不新增
  人工编辑入口
- 操作按钮:批准 / 退回修改 / 编辑 / 强制使用某 profile

**数据**:`GET/PUT /api/chapters/:id/card`、`POST /api/chapters/:id/card/approve`、
`GET /api/chapters/core-state`

生成参数面板提供“核心状态与书内时钟 HARD”总开关，默认开。关闭须明确说明只让后续
新卡回到 legacy v0，既有 revision/audit 不删除。

### 4.9 ChapterEditorPage(章节阅读与编辑)— 重点页面

**布局**:三栏(可折叠成两栏 / 一栏)。

**左栏 - 章节卡**(可折叠):
- 章节卡只读视图
- 关键字段对比当前正文的达成情况(must_achieve 逐项打勾 / 未达成红色)

**中栏 - 正文**:
- markdown 编辑器(默认只读,点"编辑"切到可编辑)
- 顶部:标题、字数、当前版本、状态、模型、累计成本
- 版本切换下拉
- 操作按钮:重写整章 / 局部修订 / 手动编辑后保存为新版本
- 局部修订:点击 Reviewer issue,正文中对应段落高亮,弹出修订建议,确认后调用 reviser

**右栏 - 审稿与日志**:
- 审稿历史(每次评审一张卡,展示六维分数 + verdict + issues)
- 当前版本的硬约束校验结果(逐项)
- 时间线(本章所有事件)

**实时**:章节正在写作时,中栏顶部显示"正在写作 ...",字数实时增长(WS 推送)

**未保存提示**:手动编辑模式下离开必须 prompt

**数据**:多个 API 组合,见 API_CONTRACT.md

### 4.10 RunControlPage(运行控制)

**必含**:
- 当前流水线状态(运行中/暂停/空闲)
- 启动控制:从某章开始 / 写 N 章 / 写到某章 / 写完整卷
- Writer Profile 临时覆盖(本次运行内有效)
- 暂停 / 继续 / 终止 按钮
- 实时输出窗口(WS 推送当前章节状态、正在调用什么模型、token 进度)
- 异常时高亮 + 跳转链接到对应章节人工介入页

### 4.11 MetricsPage(指标)

**必含**:
- 概览卡片:总章数、平均分、总成本、本月成本、平均章节耗时
- 图表:
  - 每章综合分折线
  - 每章成本柱状图
  - 张力目标值 vs 实际值(双折线)
  - 重写次数分布
- 时间范围筛选(7天/30天/全部)
- 模型分布饼图(按模型分组的调用次数与成本)

**数据**:`/api/workspaces/:id/metrics/...`

### 4.12 LogsPage(日志)

**必含**:
- 筛选:章节 / trace_id / event 类型 / 级别 / 时间范围
- 表格 + 详情抽屉
- "实时模式":开启后追新日志(WS)
- "Trace 视图":输入 chapter_id 或 trace_id,展示完整时间线(对应 OBSERVABILITY.md §4)

### 4.13 ConfigPage(配置)

**必含**:
- 三 tab:引擎默认 / 工作区覆盖 / 临时运行时
- 各 tab 内分组:
  - Writer Profiles 列表(可新增/编辑/删除)
  - Writer Routing 规则编辑(条件 + profile 映射)
  - Reviewer 配置(阈值)
  - 检索配置(top_k)
  - 成本守卫(daily/monthly limit)
  - API key 管理(脱敏显示,只能更新不能查看完整值)
- 保存后调用 `config_reloaded` 接口,后端热加载

### 4.14 ExportPage(导出与归档)

**必含**:
- 导出格式选择(md/chaptered_md/epub/docx)
- 导出范围(全书 / 指定卷 / 指定章节区间)
- 导出选项(是否包含元数据、是否包含审稿记录)
- "立即导出"按钮 → 下载文件
- 历史导出列表(最近 10 次,可重新下载)
- "归档工作区" / "清理工作区" / "彻底删除工作区"(带详细说明每个操作的影响)

---

## 5. 通用 UX 规则

### 5.1 未保存修改保护

- 所有"可编辑表单/markdown"页面,使用 `useUnsavedChangesPrompt` hook
- 路由切换、刷新、关闭浏览器时,有未保存修改必须弹原生 confirm

### 5.2 长操作反馈

- > 1s 操作显示加载态(按钮 disabled + spinner)
- > 5s 操作进度通过 WebSocket 推送,前端展示阶段
- 写作类操作展示实时 token 增长

### 5.3 错误处理

- API 错误统一通过 axios 拦截器捕获,显示 Toast
- 5xx 错误同时记录到前端错误日志(POST /api/frontend-errors,见 OBSERVABILITY)
- 网络断开:全局横幅提示 + 自动重试

### 5.4 二次确认

破坏性操作必须 ConfirmDialog:
- 删除工作区(输入名称匹配才能确认)
- 删除人设/伏笔(显示引用计数)
- 强制通过审稿
- 回退章节
- 终止运行

### 5.5 实时数据

- WebSocket 连接在 AppShell 建立,全局可用
- 订阅事件:章节状态变化、token 进度、流水线状态、错误告警
- 断线自动重连(指数退避)

### 5.6 加载与骨架屏

- 所有列表/详情页用 shadcn 的 Skeleton 占位
- 不允许出现空白闪烁

### 5.7 响应式

- v1 主要面向桌面浏览器(≥ 1280px 宽)
- 移动端不要求完美,但**不允许破版**(布局完整可读即可)

---

## 6. 状态管理

### 6.1 服务端状态(用 TanStack Query)

- 所有从后端拉取的数据
- 默认配置:staleTime 30s,失败重试 1 次
- 章节列表、指标等数据 WS 通知后调 `queryClient.invalidateQueries`

### 6.2 客户端状态(用 Zustand)

- `workspaceStore`:当前激活工作区 id、显示名
- `uiStore`:侧边栏折叠、主题(预留)、语言

### 6.3 表单状态(用 React Hook Form)

所有表单用 RHF + Zod schema。Zod schema 与后端 Pydantic 字段对齐(可半自动生成)。

---

## 7. 与后端的契约

### 7.1 类型同步

- 后端 FastAPI 自动生成 OpenAPI(`/openapi.json`)
- 前端 build 前运行 `pnpm gen-types`(执行 `openapi-typescript http://localhost:8000/openapi.json -o src/api/types.ts`)
- 所有 API 调用使用生成的类型

### 7.2 字段不一致处理

- 字段名变化:不允许前端"猜",必须后端先发版,前端再升级类型
- 字段缺失:前端类型严格匹配,缺失字段访问时报错

### 7.3 WebSocket 协议

详见 `API_CONTRACT.md §5`

---

## 8. 测试要求

- 关键页面有 Vitest + Testing Library 单测(NewBookPage / ChapterEditorPage / VolumePlanPage)
- API 客户端有 mock 测试
- v1 不要求 E2E,P7 阶段可加 Playwright

---

## 9. 验收清单

每阶段验收前端工作时:

- [ ] OpenAPI 类型已同步,无 `any` 类型
- [ ] 所有可编辑页面有未保存提示
- [ ] 所有破坏性操作有二次确认
- [ ] WebSocket 断线重连工作正常
- [ ] 长操作有进度反馈
- [ ] 错误 Toast 与前端错误上报工作
- [ ] 桌面浏览器 1280-1920px 正常
- [ ] 加载状态有 Skeleton,无空白闪烁

---

## 10. 美化迭代(P7+)

v1 完成后再考虑:

- 设计系统精修(色板、字号阶梯、间距系统)
- 暗色模式
- 动效(Framer Motion)
- 自定义图标
- 移动端响应式优化
- 多语言

v1 期间任何"美化"提议都拒绝,记到 `docs/v2_wishlist.md`。

2026-10-02 维护人批准：篇幅区间仍是写前正常任务，默认 2500–3500；采用 v1 后，
写后字数只用于统计。特别长的稿件在标题后附编辑提示，阈值为章节卡上限两倍；
提示不计入失败、改写、正文或记忆。查看与编辑导出提供提示，纯净导出可关闭。
