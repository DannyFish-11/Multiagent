#!/usr/bin/env bash
# PHASE3 M9-M12 冒烟验收(在已 up 的部署上执行;危险能力需真实 key/开户)
set -euo pipefail
API=${API:-http://localhost:8002}
PASS=0; FAIL=0
ok()  { echo "  ✅ $1"; PASS=$((PASS+1)); }
bad() { echo "  ❌ $1"; FAIL=$((FAIL+1)); }

echo "== verify_3 @ $API =="

# M9 ① 审批队列端点在线
if curl -sf "$API/approvals" | grep -q '"pending"'; then ok "M9 审批队列端点"; else bad "M9 /approvals"; fi
# M9 ② 审计日志端点在线
if curl -sf "$API/audit" | grep -q '"entries"'; then ok "M9 审计端点"; else bad "M9 /audit"; fi
# M9 ③ 并发:同时发 8 路对话不 5xx
CODES=$(for i in $(seq 1 8); do
  curl -s -o /dev/null -w "%{http_code}\n" "$API/chat" -H 'Content-Type: application/json' \
    -d "{\"message\":\"并发测试$i\",\"session_id\":\"c$i\"}" &
done; wait)
echo "$CODES" | grep -qv 200 && bad "M9 并发出现非 200: $CODES" || ok "M9 八路并发全 200"

# M10 搜索→抓取→回答带来源(需 config.web 配好搜索 key)
echo "  ⚠️ M10 上网:需 config.web.search_api_key;人工验证 搜索→抓取→回答带来源、"
echo "     页面注入指令不执行且未入队、黑名单域名被 deny 并留审计"

# M11 邮件:需 Gmail OAuth。人工:贴标签邮件 60s 内被处理、产出草稿、无 confirm 自动执行
echo "  ⚠️ M11 邮件:需 Gmail MCP OAuth;开 gmail_poll.enabled 后人工验证标签驱动草稿+审计"

# M12 支付来源检查(不需真实开户即可验证硬红线):邮件/网页来源支付被拒
R=$(docker compose exec -T memory-api python - <<'PY' 2>&1 || true
from core.payment_guard import assert_human_initiated, PaymentSourceDenied
bad = 0
for src in ("email", "web", "timer"):
    try:
        assert_human_initiated(src); print(f"LEAK:{src}"); bad += 1
    except PaymentSourceDenied:
        pass
assert_human_initiated("user")  # 人类放行
print("OK" if bad == 0 else "FAIL")
PY
)
echo "$R" | grep -q "^OK$" && ok "M12 支付来源检查(仅人类会话可发起)" || bad "M12 来源检查: $R"

# M12 支付笼子(单笔上限,不需真实开户)
R=$(docker compose exec -T memory-api python - <<'PY' 2>&1 || true
from adapters.payments import PaymentLedger, PaymentDenied
from core.config import PaymentsSettings
import tempfile, os
s = PaymentsSettings(enabled=True, provider="virtual_card", per_tx_usd=10, daily_usd=20, monthly_usd=100)
l = PaymentLedger(os.path.join(tempfile.mkdtemp(), "p.json"))
try:
    l.check_caps(10.01, s); print("LEAK")
except PaymentDenied:
    print("CAPPED")
PY
)
echo "$R" | grep -q "CAPPED" && ok "M12 单笔上限笼子" || bad "M12 笼子: $R"

echo "== 结果:$PASS 通过 / $FAIL 失败(⚠️ 项需人工/真实 key) =="
[ "$FAIL" -eq 0 ]
