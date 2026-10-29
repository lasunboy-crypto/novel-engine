#!/usr/bin/env bash
# dev.sh — 一键启动本地开发环境:后端 FastAPI(:8000)+ 前端 Vite(:5173)
#
#   用法：在项目根目录运行   ./dev.sh
#   关闭：按一次 Ctrl-C，前后端一起退出
#
# 纯 shell，不引入任何新依赖；后端走 uv、前端走 pnpm（已是项目技术栈）。

# 切到脚本所在目录（= 项目根），这样从任何地方调用都能正确启动
cd "$(dirname "$0")" || exit 1

# 前端依赖首次未安装则自动安装一次
if [ ! -d web/node_modules ]; then
  echo "[dev] 前端依赖未安装，正在 pnpm install（首次较慢，装完自动继续）..."
  ( cd web && pnpm install ) || { echo "[dev] pnpm install 失败，请手动在 web/ 下跑 pnpm install"; exit 1; }
fi

# Ctrl-C / 退出时，连同两个子进程一起干净关闭（kill 0 = 杀掉本进程组）
trap 'echo; echo "[dev] 正在关闭前后端..."; kill 0' EXIT

echo "=================================================="
echo "  后端 API   ->  http://127.0.0.1:8000   (接口文档 /docs)"
echo "  前端页面   ->  http://127.0.0.1:5173   <- 浏览器打开这个"
echo "  按一次 Ctrl-C 同时关闭前后端"
echo "=================================================="
echo ""

# 后端（FastAPI + 热重载）
uv run uvicorn src.api.main:app --host 127.0.0.1 --port 8000 --reload &

# 前端（Vite 开发服务器，热更新）
( cd web && pnpm dev ) &

# 等待两个子进程；任一退出或收到 Ctrl-C 都会触发上面的 trap 一起收尾
wait
