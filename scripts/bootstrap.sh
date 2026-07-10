#!/usr/bin/env bash
# PHASE2.5 一键部署:检查环境 → 准备 .env → up → 冒烟验收
set -euo pipefail
cd "$(dirname "$0")/.."

echo "== memory-agent bootstrap =="

# 1) docker / compose 版本检查
command -v docker >/dev/null || { echo "错误:未安装 docker"; exit 1; }
docker compose version >/dev/null 2>&1 || { echo "错误:需要 Docker Compose v2(docker compose)"; exit 1; }
docker info >/dev/null 2>&1 || { echo "错误:docker daemon 未运行"; exit 1; }

# 2) .env 准备
if [ ! -f .env ]; then
  cp .env.example .env
  echo "已生成 .env(自 .env.example)。请填写以下密钥后重新运行本脚本:"
  echo "  - MEMORY_AGENT_LLM__CHAT__BASE_URL / API_KEY / MODEL"
  echo "  - MEMORY_AGENT_EMBEDDER__JINA_API_KEY"
  exit 2
fi
for key in MEMORY_AGENT_LLM__CHAT__BASE_URL MEMORY_AGENT_LLM__CHAT__API_KEY MEMORY_AGENT_EMBEDDER__JINA_API_KEY; do
  if ! grep -qE "^${key}=.+" .env; then
    echo "错误:.env 缺少 ${key}(停点:API 供应商与模型由人类指定)"; exit 2
  fi
done

# 3) 起服务
mkdir -p data/agent data/qdrant logs exports reports
docker compose up -d --build
echo "等待 memory-api 就绪..."
for i in $(seq 1 60); do
  if curl -sf http://localhost:8002/healthz >/dev/null 2>&1; then break; fi
  sleep 2
done

# 4) 冒烟验收
./scripts/verify_25.sh
