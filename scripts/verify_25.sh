#!/usr/bin/env bash
# PHASE2.5 M-D 冒烟验收(在已 up 的部署上执行,消耗少量真实 API 配额)
set -euo pipefail
API=${API:-http://localhost:8002}
PASS=0; FAIL=0
ok()   { echo "  ✅ $1"; PASS=$((PASS+1)); }
bad()  { echo "  ❌ $1"; FAIL=$((FAIL+1)); }

echo "== verify_25 @ $API =="

# ① /healthz 三依赖全绿
H=$(curl -sf "$API/healthz")
if echo "$H" | grep -q '"status":"ok"' \
   && echo "$H" | python3 -c "import json,sys; d=json.load(sys.stdin); assert all(d['layers'][k]=='ok' for k in ('L0','L1','L2')), d" ; then
  ok "①healthz 三依赖全绿"
else
  bad "①healthz: $H"
fi

# ② 端到端对话:两轮会话,第二轮命中第一轮事实
SID="verify25-$(date +%s)"
curl -sf "$API/chat" -H 'Content-Type: application/json' \
  -d "{\"message\":\"记住:我的猫叫 Benjamin,全身白色\",\"session_id\":\"$SID\"}" >/dev/null
R2=$(curl -sf "$API/chat" -H 'Content-Type: application/json' \
  -d "{\"message\":\"我的猫叫什么名字?\",\"session_id\":\"$SID\"}")
echo "$R2" | grep -q "Benjamin" && ok "②记忆闭环(两轮对话)" || bad "②回答未命中: $R2"

# ③ 多模态:测试图片入记忆,新会话文本召回
IMG=$(python3 - <<'PY'
import base64, io
try:
    from PIL import Image, ImageDraw
    img = Image.new("RGB", (64, 64), (250, 250, 250))
    buf = io.BytesIO(); img.save(buf, "PNG")
    print(base64.b64encode(buf.getvalue()).decode())
except ImportError:  # 宿主机无 PIL 时用最小 1x1 PNG
    print("iVBORw0KGgoAAAANSUhEUgAAAAEAAAABCAYAAAAfFcSJAAAADUlEQVR42mP8/5+hHgAHggJ/PchI7wAAAABJRU5ErkJggg==")
PY
)
curl -sf "$API/chat" -H 'Content-Type: application/json' \
  -d "{\"message\":\"这是我的白色猫 Benjamin 的照片\",\"session_id\":\"$SID-img\",\"image_base64\":\"$IMG\"}" >/dev/null
R3=$(curl -sf "$API/memory/search" -H 'Content-Type: application/json' \
  -d '{"query":{"type":"text","content":"白色的猫的照片"},"k":5}')
echo "$R3" | grep -q '"modality":"image"' && ok "③多模态图片记忆召回" || bad "③未召回图片记忆: $R3"

# ④ 拔备胎测试(需 .env 配置 FALLBACKS 后,临时把主端点改错并重启 memory-api)
#    自动化脚本仅校验日志中已有 failover 记录或提示人工步骤
if docker compose logs memory-api 2>/dev/null | grep -q "failover:"; then
  ok "④备胎切换日志在案"
else
  echo "  ⚠️ ④拔备胎:人工步骤 —— 临时把 MEMORY_AGENT_LLM__CHAT__BASE_URL 配错并 docker compose restart memory-api,请求应自动落到 fallback 且日志出现 'failover:'"
fi

# ⑤ 预算测试:budget.daily_usd=0.01 时请求被拒且报错含当日用量
R5=$(MEMORY_AGENT_BUDGET__DAILY_USD=0.01 docker compose exec -T \
  -e MEMORY_AGENT_BUDGET__DAILY_USD=0.01 memory-api python - <<'PY' 2>&1 || true
import asyncio
from core.config import load_config
from adapters.llm import build_llm_client, build_ledger
from core.schemas import Message
cfg = load_config()
cfg.budget.daily_usd = 0.01
ledger = build_ledger(cfg)
ledger.record("verify", "manual", 10_000_000, 10_000_000)  # 人为灌满
llm = build_llm_client(cfg, ledger=ledger)
try:
    asyncio.run(llm.chat([Message(role="user", content="hi")]))
    print("NOT-BLOCKED")
except Exception as e:
    print(f"BLOCKED: {e}")
PY
)
echo "$R5" | grep -q "BLOCKED" && echo "$R5" | grep -q "日预算" && ok "⑤预算超限拒绝且含用量" || bad "⑤预算测试: $R5"

# ⑥ 重建测试:down && up 后 ② 的记忆仍在(volume 生效)
docker compose down >/dev/null 2>&1 && docker compose up -d >/dev/null 2>&1
for i in $(seq 1 60); do curl -sf "$API/healthz" >/dev/null 2>&1 && break; sleep 2; done
R6=$(curl -sf "$API/memory/search" -H 'Content-Type: application/json' \
  -d '{"query":{"type":"text","content":"我的猫叫什么"},"k":5}')
echo "$R6" | grep -q "Benjamin" && ok "⑥容器重建后记忆无损" || bad "⑥重建后记忆丢失: $R6"

# ⑦ 维度守卫:错误维度配置下启动被拦且提示重算流程
R7=$(docker compose exec -T -e MEMORY_AGENT_EMBEDDER__DIM=123 memory-api python - <<'PY' 2>&1 || true
import asyncio
from core.config import load_config
from adapters.vectordb import QdrantAdapter
cfg = load_config()
db = QdrantAdapter(cfg.vectordb, dim=123)
try:
    asyncio.run(db.ensure_collection())
    print("NOT-GUARDED")
except Exception as e:
    print(f"GUARDED: {e}")
PY
)
echo "$R7" | grep -q "GUARDED" && echo "$R7" | grep -q "memorypack export" && ok "⑦维度守卫拦截并指引 M7" || bad "⑦维度守卫: $R7"

echo "== 结果:$PASS 通过 / $FAIL 失败 =="
[ "$FAIL" -eq 0 ]
