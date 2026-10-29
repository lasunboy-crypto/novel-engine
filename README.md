# Novel Engine · 长篇小说创作引擎

一个在本机运行的中文长篇小说创作工具，把创作意图、世界观、人物、大纲、章节卡、正文、审核与跨章记忆串在同一个工作区里。

**FastAPI + React + CLI · SQLite + Chroma · MIT**

适合愿意参与规划、审稿和修改的作者，以及研究长文本生成流程的开发者。项目仍在持续开发，已有真实新书规划和连续章节的流程验证；自动审核通过不等于文学质量合格，也不代表已证明能稳定生成整部可发表长篇。

## 能做什么

- 从创作偏好和故事设定整理规划，生成世界观、人物、分卷大纲和逐章计划。
- 用章节卡约定剧情目标、禁区、节奏和上一章的衔接，再生成正文。
- 通过审核、修订、重写和人工介入处理问题，保留章节版本。
- 记录人物状态、伏笔、时间线、摘要和核心事实，供后续章节检索。
- 查看运行进度、请求用量和配置价格估算，暂停或从检查点继续。
- 导出 Markdown、分章 Markdown、EPUB 和 DOCX。
- 通过网页操作，也可以使用 `novel` 命令行工具。

## 先在本机启动

需要 **Python 3.11、uv、Node.js 22.12+ 和 pnpm**。Python 大模型与向量库依赖较多，首次安装需要下载；涉及嵌入模型的功能还可能下载 `Qwen/Qwen3-Embedding-0.6B`。macOS 是本项目的主要开发环境，其他系统尚未做完整验证。

先下载项目：

```bash
git clone https://github.com/lasunboy-crypto/novel-engine.git
cd novel-engine
```

在仓库根目录执行：

```bash
uv sync --locked
cp .env.example .env
CONFIG_YAML_PATH=config/public_mock.yaml LLM_PROVIDER=mock uv run uvicorn src.api.main:app --host 127.0.0.1 --port 8000
```

这里显式选择仓库提供的模拟配置，生成和审核使用固定测试响应，不会调用付费小说模型。模拟模式用于了解界面与开发联调，不能用来评价创作效果；它不保证所有真实写作场景都能用模拟数据走完。

再打开一个终端，在仓库根目录执行：

```bash
cd web
pnpm install --frozen-lockfile
pnpm dev --host 127.0.0.1
```

浏览器访问 [本机网页](http://127.0.0.1:5173)，接口文档在 [本机 API 文档](http://127.0.0.1:8000/docs)。两个终端各按 `Ctrl+C` 即可停止。

首次使用会看到空工作区。创建新书后，按“规划 → 大纲确认 → 章节卡 → 运行 → 审核/修订 → 导出”操作。详细步骤见 [入门指南](docs/open-source/getting-started.md)。

> `LLM_PROVIDER=mock` 不是所有路由的总开关；务必同时使用上面的 `CONFIG_YAML_PATH=config/public_mock.yaml`。根目录 `config.yaml` 保留了现有完整配置和历史模型参数，初次启动不要直接拿它当模拟配置。

## 接入真实模型

实现包含 Ollama、Anthropic 和 OpenAI 兼容接口适配；实际可用性取决于供应商、模型、结构化输出和配置。先阅读 [模型配置](docs/open-source/model-configuration.md)，再在独立的新工作区小规模试写。

密钥填入本机 `.env` 或产品配置页。真实生成会把参与该次请求的故事材料发送给所选服务，产生的费用以供应商账单为准。项目中的型号、容量和配置价格都是配置快照，使用前自行核对供应商当前信息。

## 数据与运行边界

- 面向本地单机使用，默认只监听 `127.0.0.1`；远程访问必须按接口要求启用鉴权。
- 小说、数据库、向量索引、备份与日志保存在本机工作区；升级与维护前先备份。
- 公开仓库提供程序、提示词、配置样例、可移植测试与技术说明；不包含维护人的小说工作区、密钥、模型原始响应、私人实验记录或开发 Git 历史。
- 已知限制包括模型可能漏写剧情、误判事实、产生重复或在重写时损伤已通过内容。长篇连续性与最终发表质量仍需要作者通读。

## 开发与测试

```bash
uv sync --locked
uv run pytest -q
cd web
pnpm install --frozen-lockfile
pnpm test
pnpm lint
pnpm build
```

默认排除需要外部服务的 `integration` 测试。公开快照不打包依赖私人原始样本和历史提交的实验测试；发布验证范围见 [公开发布说明](PUBLIC_RELEASE.md)。测试通过只证明相应工程行为。

```bash
uv run novel --help
```

目录：`src/` 是后端、创作流程与 CLI，`web/` 是前端，`prompts/` 是提示词与版本归档，`alembic/` 是数据库迁移，`tests/` 是自动化测试。

## 文档与参与

- [入门指南](docs/open-source/getting-started.md) · [模型配置](docs/open-source/model-configuration.md)
- [静态资产导入](docs/open-source/asset-import.md)
- [架构规格](PROJECT_SPEC.md) · [API 契约](API_CONTRACT.md) · [前端规格](FRONTEND_SPEC.md) · [可观测性](OBSERVABILITY.md) · [路线图](ROADMAP.md)
- [贡献指南](CONTRIBUTING.md) · [安全问题](SECURITY.md)

欢迎提交可复现的 Bug、使用反馈与小范围改进。技术规格保留了开发阶段的设计背景；第一次使用请从本 README 和入门指南开始。

代码与随附项目文档采用 [MIT 许可证](LICENSE)。第三方依赖、模型权重和外部素材遵循各自许可；本许可证不替你决定输入素材或生成小说的权利归属。
