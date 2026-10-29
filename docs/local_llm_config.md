# 本地 LLM 配置(P3 阶段使用)

> P1/P2 阶段不引用此文件。

## 当前配置

- 母版模型:qwen3.5:35b
- 项目映射:qwen-novel(基于 qwen3.5:35b)
- Modelfile 草案位置:docs/modelfiles/qwen-novel.modelfile
- P3 启动时执行:ollama create qwen-novel -f docs/modelfiles/qwen-novel.modelfile

## 关键发现:Thinking Mode 必须关闭

Qwen 3.5 35B 是带 thinking mode 的推理模型。默认调用会输出大量思考过程(占满 token 上限),实际正文输出为空。

解决方案:API 调用顶层加 think:false 参数(注意不在 options 里)。

## 实测对比(2026-05-04 凌晨,模型 qwen-novel)

- 默认(think 开):400+ token 全部用于思考,content 为空,被截断
- think:false:140 token 完整产出 200 字小说,3.47 秒,正常 stop

## 验证过的请求模板

curl http://localhost:11434/api/chat 调用,JSON 主体:
- model: qwen-novel
- stream: true
- think: false  ← 关键参数
- options: { num_predict: 400, temperature: 0.7 }
- messages: [system, user]

## P3 实施要点

1. ModelRouter 实现 OpenAI 兼容 client 时,所有 Qwen 调用必须默认 think:false
2. 这个参数写进 config.yaml 的 writer_profiles.local_qwen 配置
3. 如果未来出现"输出空 / 速度慢 / token 浪费",第一件事检查 think 参数

## 全本地方案可行性

基于本次实测,Qwen 3.5 35B 在 think:false 模式下文笔可接受、速度可接受。P3 阶段可以以 qwen-novel 为主模型担任所有 agent,Claude 仅作为质量兜底备选。

## 补充澄清(2026-05-04)

### think 参数是调用层面的,不是模型层面的

同一个 qwen-novel 模型,**每次 API 调用可以独立决定是否 thinking**:

- 不传 think 字段 / `"think": true` → 走 thinking mode
- `"think": false` → 关闭 thinking,直接出文本

LLMClient 封装层暴露 `enable_thinking` 参数,默认 False,各 agent 按需打开。

### Reviewer/Reranker 升级路径(从快到慢)

1. **L1**:think:false + 普通 prompt(默认尝试)
2. **L2**:think:false + 强结构化 prompt(覆盖 80% 场景)
3. **L3**:think:true + 同 prompt(切换 1 行代码)
4. **L4**:Claude Sonnet API(极端兜底)

实施时按需逐级升级,大部分情况停在 L2。

