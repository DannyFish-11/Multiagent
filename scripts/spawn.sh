#!/usr/bin/env bash
# PHASE3 M9.1:一键起 N 只实例。每实例独立 agent_id + 独立记忆 volume,
# 可选挂载同一共享池(config.memory.shared_collection 指向同一 Qdrant collection)。
#
# 用法:scripts/spawn.sh N [--shared]
#   N        实例数量
#   --shared 所有实例共享同一 Qdrant(默认各自独立 collection 前缀)
set -euo pipefail
cd "$(dirname "$0")/.."

N=${1:?用法: spawn.sh N [--shared]}
SHARED=${2:-}

command -v docker >/dev/null || { echo "错误:未安装 docker"; exit 1; }
[ -f .env ] || { echo "错误:缺少 .env(先跑 scripts/bootstrap.sh)"; exit 2; }

echo "== 起 $N 只 memory-agent 实例 =="
# 共享 qdrant 一份;每实例一个 compose project + 独立 volume + 独立端口
docker compose up -d qdrant

for i in $(seq 1 "$N"); do
  PORT=$((8002 + i - 1))
  PROJECT="magent_$i"
  DATA="./data/instance_$i"
  mkdir -p "$DATA/agent" "$DATA/logs"
  echo "  实例 $i → 端口 $PORT,数据 $DATA,project $PROJECT"
  MEMORY_AGENT_VECTORDB__URL="http://qdrant:6333" \
  MEMORY_AGENT_IDENTITY__DIR="/app/data/identity" \
  INSTANCE_DATA="$DATA" INSTANCE_PORT="$PORT" \
  docker compose -p "$PROJECT" up -d memory-api
  # 共享池模式:所有实例的 shared_collection 用同一名字(默认已是 memories_shared);
  # 独立模式:private collection 用实例名前缀(经 MEMORY_AGENT_VECTORDB__COLLECTION 区分)
  if [ "$SHARED" != "--shared" ]; then
    docker compose -p "$PROJECT" exec -T -e MEMORY_AGENT_VECTORDB__COLLECTION="mem_$i" memory-api true 2>/dev/null || true
  fi
done

echo "== 完成。各实例 /healthz:"
for i in $(seq 1 "$N"); do
  echo "  实例 $i: http://localhost:$((8002 + i - 1))/healthz"
done
