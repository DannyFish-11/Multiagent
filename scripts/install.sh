#!/usr/bin/env sh
# memory-agent 一键引导:检查环境 → 首次运行向导 → 起 Docker 全栈 → 等健康检查。
# 用法:克隆仓库后在项目根执行 `./scripts/install.sh`(或 make quickstart)。
set -eu

cd "$(CDPATH= cd -- "$(dirname -- "$0")/.." && pwd)"

say() { printf '\033[1m%s\033[0m\n' "$*"; }
die() { printf '\033[31m%s\033[0m\n' "$*" >&2; exit 1; }

say "== memory-agent 一键引导 =="

command -v docker >/dev/null 2>&1 || die "未找到 docker。请先装 Docker Desktop(Win/macOS)或 Docker Engine(Linux)。"
docker compose version >/dev/null 2>&1 || die "未找到 'docker compose'(v2)。请升级 Docker。"

# 依赖(向导用 uv 跑;没有 uv 也能继续,用系统 python)
RUNNER="uv run python"
command -v uv >/dev/null 2>&1 || RUNNER="python3"

if [ ! -f .env ]; then
  say "-- 未发现 .env,进入首次运行向导 --"
  $RUNNER scripts/setup.py || die "向导未完成。"
else
  say "-- 已存在 .env,跳过向导(改配置重跑:$RUNNER scripts/setup.py)--"
fi

say "-- 构建并启动 Docker 全栈(qdrant + memory-api)--"
docker compose up -d --build

say "-- 等待 /healthz 通过(最多 ~90s)--"
i=0
until curl -fs http://localhost:8002/healthz >/dev/null 2>&1; do
  i=$((i + 1))
  [ "$i" -gt 45 ] && die "健康检查超时。看日志:docker compose logs memory-api"
  sleep 2
done

say ""
say "✅ 起来了!L3 API 在 http://localhost:8002"
say "   和它对话:      make chat"
say "   看健康:        curl localhost:8002/healthz"
say "   停:            docker compose down"
