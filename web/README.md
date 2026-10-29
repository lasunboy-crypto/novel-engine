# Novel Engine 前端

React + TypeScript + Vite。安装与后端连接方法见 [项目首页](../README.md)。

```bash
pnpm install --frozen-lockfile
pnpm dev --host 127.0.0.1
pnpm test
pnpm lint
pnpm build
```

API 类型来自 OpenAPI；修改接口后，在后端运行时执行 `pnpm gen-types`。
