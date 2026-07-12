#!/usr/bin/env bash
# ============================================================
#  memory-agent 一键运行(Linux / macOS)—— 双击本文件即可(或 ./run.sh)。
#  首次会自动装好运行环境(uv),然后起服务并打开浏览器聊天页。
#  零配置即 demo 档(零 key / 零 GPU);想用真实大模型请先放一个 .env。
# ============================================================
set -e
cd "$(dirname "$0")"

if ! command -v uv >/dev/null 2>&1; then
  echo "[1/2] 正在安装运行环境 uv ..."
  curl -LsSf https://astral.sh/uv/install.sh | sh
  export PATH="$HOME/.local/bin:$HOME/.cargo/bin:$PATH"
fi

echo "[2/2] 启动 memory-agent(首次会自动装依赖,稍候)..."
exec uv run python -m core.launch "$@"
