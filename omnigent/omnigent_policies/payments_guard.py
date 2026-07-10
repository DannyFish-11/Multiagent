"""支付兜底拒绝策略(附录 A:默认拒付,未来显式解锁)。

任何涉及资金/钱包/支付关键词的工具调用一律 deny 并告警。
复查触发条件与试点路径记录于 docs/payments-assessment.md。
"""

from __future__ import annotations

import json
import re
from typing import Any

# 拉丁词按 token 精确匹配(避免 payload/repayment 一类误杀);中文按子串
PAYMENT_TOKENS = frozenset({
    "pay", "payment", "payments", "pays", "paypal", "wallet", "transfer",
    "checkout", "purchase", "refund", "crypto", "stablecoin", "usdc",
    "x402", "mandate",
})
PAYMENT_SUBSTRINGS = ("支付", "付款", "钱包", "转账", "购买", "退款")


def _find_keyword(haystack: str) -> str | None:
    tokens = set(re.split(r"[^a-z0-9]+", haystack))
    hit = next(iter(PAYMENT_TOKENS & tokens), None)
    if hit:
        return hit
    return next((kw for kw in PAYMENT_SUBSTRINGS if kw in haystack), None)


def enforce(event: Any, **_: Any) -> dict[str, Any]:
    tool_name = str(getattr(event, "tool_name", "") or (event.get("tool_name", "") if isinstance(event, dict) else ""))
    args = getattr(event, "arguments", None) or (event.get("arguments") if isinstance(event, dict) else None) or {}
    haystack = (tool_name + " " + json.dumps(args, ensure_ascii=False, default=str)).lower()
    hit = _find_keyword(haystack)
    if hit:
        return {
            "action": "deny",
            "reason": f"支付能力未启用:检测到关键词 {hit!r},按附录 A 默认拒付并告警"
                      "(解锁须满足 docs/payments-assessment.md 复查条件)",
        }
    return {"action": "allow"}
