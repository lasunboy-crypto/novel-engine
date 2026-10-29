# 模型配置

## 从模拟配置开始

程序根据 profile（模型及参数配置）与路由表选择实际客户端。`LLM_PROVIDER` 不是统一覆盖所有 Agent 的总开关。初次使用请同时指定：

```bash
CONFIG_YAML_PATH=config/public_mock.yaml LLM_PROVIDER=mock uv run uvicorn src.api.main:app --host 127.0.0.1 --port 8000
```

`config/public_mock.yaml` 只声明一个模拟 profile，默认路由和所有未单独指定的 Agent 都使用它。已有工作区可能包含自己的 `project.yaml` 覆盖；演示请新建工作区，不要直接套在旧书上。

## 使用真实模型

根目录 `config.yaml` 提供完整 profile 示例，`config/` 中还有实验模型配置；它们是开发快照，不能当成当前供应商能力和价格承诺。

使用前请完成以下配置：

1. 把准备使用的配置复制为本机文件，例如 `config/local.yaml`；该文件已忽略，不要提交密钥。
2. 在 `writer_profiles` 中填写供应商实际支持的 `provider`、`model`、`base_url`、上下文容量、生成参数和价格。
3. 将 `writer_routing.default`、条件规则及 `agent_routing` 指向你选定的 profile。检查所有审核、摘要和记忆 Agent 的路由，不能只修改 Writer。
4. 按 `.env.example` 在本机 `.env` 填对应凭据，设置 `CONFIG_YAML_PATH=config/local.yaml` 和相应的 `LLM_PROVIDER`。
5. 使用独立新工作区和少量章节验证结构化输出、正文长度、审核、记忆与费用，再决定是否扩展。

适配器包括 `mock`、`ollama`、`claude` 与 `openai_compatible`。不同模型的思考参数、结构化输出和 token 上限并不通用；程序对部分语义审核要求相应的思考配置，不建议仅替换模型名后直接长篇运行。

供应商专用 key 会按受支持的精确端点选择；其他合法兼容服务使用 `OPENAI_COMPATIBLE_API_KEY`。不要把真实密钥写进 YAML 或修改代码来绕过端点校验。

本地 Ollama 示例可参考 [本地模型说明](../local_llm_config.md) 和 `docs/modelfiles/`；文档中的特定模型版本需要自行核对与安装。

## 数据、费用与恢复

真实云端调用会发送所需的故事设定、人物、上下文、正文或审核材料。调用数不是“一章一次”：规划、写作、审核、记忆以及必要重试可能分别产生请求。界面的配置估算与供应商账单不是同一口径。

先设置可承担的运行范围和预算，保留工作区备份。请求失败后从产品页面确认状态与继续位置；不要手改数据库或模型生成物冒充正常通过。
