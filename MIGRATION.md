# 移植说明(MIGRATION.md)

把一台机器上正在跑的 **长篇小说创作引擎** 完整迁移到另一台机器,涵盖:**备
份清单 → 新机安装 → 代码迁移 → 数据导入 → 验证**。

> 适用场景:换电脑 / 从台式机迁到笔记本 / 从笔记本迁回台式机 / 备份做异地
> 容灾。本文档**不**包含线上服务器部署(v1 阶段仅本地单机,server 部署留
> v1.1)。
>
> 配套文档:`USER_GUIDE.md`(日常使用)/ `uninstall.sh`(完全卸载)/
> `docs/local_llm_config.md`(本地 LLM 配置)/ `docs/TROUBLESHOOTING.md`
> (故障排查)。

---

## §1 备份清单(老机器执行)

引擎所有**有价值的数据**集中在 4 个位置,逐项打包即可。

### 1.1 必备份(数据无法重建)

| 路径 | 内容 | 备注 |
|------|------|------|
| `workspaces/` | 全部工作区 — 每子目录含 `novel.db`(SQLite,章节 / 卡 / 卷 / 备份记录)+ `chroma/`(向量库,记忆嵌入)+ `meta.json`(schema 版本)+ `cards/` + `outline/` + `volumes/` + `backups/`(P6-B-07 自动备份 zip)+ `exports/` | **核心数据,丢失即丢全部小说** |
| `.env` | 环境变量 — `CLAUDE_API_KEY` / `LLM_PROVIDER` / `DATA_DIR` / `LLM_MOCK_VERDICT` 等 | 仅当你已从 `.env.example` 拷出 `.env` 并填了 API key 时需要 |
| `config.yaml` | 引擎默认配置 — Writer Profile / Reviewer 阈值 / 路由规则等(若维护人已修改) | 默认值在仓库中,**未改过可不带**(`git diff config.yaml` 看)|
| `prompts/*.md`(根目录 9 个 md 文件)| Writer / Reviewer / MemoryExtractor 等 prompt(若维护人已修改) | 默认值在仓库中,**未改过可不带**;`prompts/archive/` 是旧版本归档**不必带** |

### 1.2 不必备份(可重建 / 临时缓存)

- `.venv/`(uv 装的 Python 依赖,`uv sync` 重建)
- `web/node_modules/`(pnpm 装的前端依赖,`pnpm install` 重建)
- `__pycache__/` / `.pytest_cache/` / `.ruff_cache/` / `.mypy_cache/`(缓存)
- `web/dist/` / `web/.vite/`(前端构建产物)
- `archives/`(P1 期 workspace archive 模式遗留,P6+ 已切到 backups/ 模式)
- `.git/`(代码仓库历史,从 GitHub 重新 clone 即可)

### 1.3 打包命令

老机器仓库根目录执行:

```bash
# 一次打包(用日期作文件名,便于多次迁移区分)
tar -czf novel-engine-backup-$(date +%Y%m%d).tar.gz \
    workspaces/ \
    .env \
    config.yaml \
    prompts/

# 检查打包大小(workspaces/ 通常是大头 — chroma 向量库 + 备份 zip)
ls -lh novel-engine-backup-*.tar.gz
```

若 `.env` / `config.yaml` 任一不存在(未自定义过),`tar` 会报 warning 但
继续打包其余文件,不影响最终归档。把生成的 `.tar.gz` 复制到新机器(U
盘 / scp / 网盘均可)。

---

## §2 新机器安装步骤(零依赖到可启动)

新机器从零开始,装齐 4 个外部工具:`uv`(Python 包管理)+ `pnpm`(前端包
管理)+ `ollama`(本地 LLM 运行时)+ ollama 上的 `qwen-novel` 项目模型。

### 2.1 装 uv(Python 包管理器)

```bash
# macOS / Linux 通用
curl -LsSf https://astral.sh/uv/install.sh | sh

# 验证
uv --version    # 期望:uv 0.5.x 或更新
```

### 2.2 装 pnpm(前端包管理器,版本锁定 9.x)

```bash
# 需要先有 Node 18+,推荐用 nvm 管理:
# macOS: brew install nvm 并按 brew 提示加 shell 配置;
# Linux: curl -o- https://raw.githubusercontent.com/nvm-sh/nvm/v0.40.1/install.sh | bash
nvm install 20
nvm use 20

# 装 pnpm@9(本仓库 web/package.json 锁定 9.x)
npm install -g pnpm@9

# 验证
pnpm --version    # 期望:9.x
```

### 2.3 装 ollama + 拉模型(对齐 USER_GUIDE.md §1.3 + docs/local_llm_config.md)

```bash
# 1. 装 ollama(macOS / Linux 任选其一)
brew install ollama                              # macOS
curl -fsSL https://ollama.com/install.sh | sh    # Linux

# 2. 启动 ollama 服务(默认绑 127.0.0.1:11434)
ollama serve &                                   # 后台跑;或开新终端 ollama serve

# 3. 拉基础模型(母版,~22 GB,首次下载需稳定网络)
ollama pull qwen3.5:35b

# 4. 创建项目专用模型(基于仓库 docs/modelfiles/qwen-novel.modelfile)
# ⚠️ 必须先完成 §3 代码迁移让 docs/modelfiles/qwen-novel.modelfile 落地后再跑
# ollama create qwen-novel -f docs/modelfiles/qwen-novel.modelfile
```

**第 4 步留到 §3 代码迁移完成后**(此时仓库才存在,Modelfile 路径才有效)。
详细 thinking mode 参数与升级路径见 `docs/local_llm_config.md`。

---

## §3 代码迁移(从 GitHub 拉仓库 + 装依赖)

新机器仓库目录(例 `~/code/novel-engine`)执行:

```bash
# 1. 克隆仓库(替换为实际仓库 URL)
git clone <repo-url> novel-engine
cd novel-engine

# 2. 装后端 Python 依赖(uv 自动读 pyproject.toml + uv.lock 还原确切版本)
uv sync

# 3. 装前端依赖
cd web && pnpm install && cd ..

# 4. 完成 §2.3 第 4 步(此时 docs/modelfiles/qwen-novel.modelfile 已存在)
ollama create qwen-novel -f docs/modelfiles/qwen-novel.modelfile
ollama list      # 期望看到 qwen3.5:35b 母版 + qwen-novel 项目映射两行
```

到这一步,代码 + 外部工具就位。**还差数据**(workspaces / .env /
config.yaml / prompts 改动)。

---

## §4 数据导入(还原工作区 + schema 升级)

新机器仓库根目录执行:

### 4.1 还原备份

```bash
# 把老机器的 tar.gz 复制到仓库根目录后:
tar -xzf novel-engine-backup-*.tar.gz

# 验证:
ls workspaces/                # 应见所有老工作区子目录
ls -la .env config.yaml       # 任一不存在不报错,有就是有
ls prompts/ | head            # 应见 writer.md / reviewer.md 等 prompt md 文件
```

### 4.2 schema 升级(让代码版本与 DB schema 对齐)

老机器迁过来的 `workspaces/<id>/novel.db` 可能比新代码 schema 旧(例如老
机器停在 P5 期、新机器代码已到 P7 期)。**必须显式触发数据迁移机制**(P6-
B-08):

```bash
# 1. 启动后端
uv run uvicorn src.api.main:app --host 127.0.0.1 --port 8000

# 2. 另开终端,先查 schema 版本差(对每个工作区跑一次)
curl http://127.0.0.1:8000/api/v1/workspaces/<workspace_id>/version-status

# 期望返:
# {
#   "from_schema_version": "0005_volumes_table",  # 老 head
#   "to_schema_version":   "0009_pipeline_runs",  # 新 head
#   "is_compatible": false,
#   "migration_required": true,
#   ...
# }

# 3. 若 migration_required=true,触发 :migrate 端点
# (会自动 BackupService 备份 + alembic upgrade head)
curl -X POST http://127.0.0.1:8000/api/v1/workspaces/<workspace_id>:migrate \
     -H "Content-Type: application/json" \
     -d '{"confirm": "<workspace_id>"}'

# CLI 等价(P7-B-04 落地):
uv run novel workspace migrate <workspace_id> --confirm <workspace_id>
```

⚠️ **关键约束**:`POST :migrate` 在升级前**自动备份**(`workspaces/<id>/
backups/pre_migration_<timestamp>.zip`),失败可 `POST :restore` 一键回滚
(P6-B-07 + P7-F-01 落地)。

### 4.3 多工作区批量升级(若有 10+ 工作区)

```bash
# 用 CLI 写个循环:
for ws in $(ls workspaces/); do
    [ "$ws" = "_active" ] && continue   # 跳过软链
    echo "迁移 $ws ..."
    uv run novel workspace migrate "$ws" --confirm "$ws"
done
```

---

## §5 验证(确认迁移成功)

### 5.1 后端单元测试(确认代码 + 依赖装齐)

```bash
uv run pytest tests/
# 期望:2102 passed / 7 deselected(P7-B-05 基线;deselect 7 是 --integration
# 标记的真 LLM 测试默认 skip,不依赖迁移数据)
```

### 5.2 后端 + 前端 + 浏览器走查(端到端确认数据可见)

```bash
# 终端 1:后端
uv run uvicorn src.api.main:app --host 127.0.0.1 --port 8000

# 终端 2:前端
cd web && pnpm dev
# 期望:http://127.0.0.1:5173 起服 + 控制台无 fetch / CORS 错

# 浏览器:打开 http://127.0.0.1:5173
# 1. 选一个老机器迁过来的工作区
# 2. 进 ChaptersBoardPage 看历史章节加载(显数 / 状态 / 标题)
# 3. 进 ChapterEditorPage 打开任一历史章节看正文渲染
# 4. 进 ExportPage 看历史导出记录加载
# 上述 4 步全显数无 toast 报错 = 迁移成功
```

### 5.3 故障排查

迁移过程报错时,按下表速查:

| 现象 | 看 |
|------|---|
| 后端启动报 `RuntimeError: API binds 0.0.0.0` | `docs/TROUBLESHOOTING.md §1.1` |
| `pytest` 报 fixture 找不到 | `docs/TROUBLESHOOTING.md §1.3`(默认不依赖 DATA_DIR) |
| 后端报 `workspace_version_mismatch` | `docs/TROUBLESHOOTING.md §2.3`(再跑一次 :migrate) |
| 浏览器章节列表加载报"no such column" | `docs/TROUBLESHOOTING.md §5.3`(schema 未升级) |
| 前端报"调用 LLM 失败 — Connection refused on 11434" | `docs/TROUBLESHOOTING.md §3.2`(ollama 未起 / qwen-novel 未创建) |
| `ollama list` 不见 `qwen-novel` | 回 §2.3 第 4 步 / §3 第 4 步重跑 |

迁移完成后,老机器可执行 `bash uninstall.sh`(交互式确认)完全清理。
