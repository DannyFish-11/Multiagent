"""Gmail 工具治理分级(M6.1,Omnigent function policy)。

read/search/summarize → allow;draft → allow(草稿不发出);
send/delete/archive → 每次 ask,无例外。
分级按工具名关键词判定,未识别的 Gmail 工具一律 ask(保守默认)。
"""

from __future__ import annotations

from typing import Any

READ_ONLY = ("read", "search", "get", "list", "summarize", "fetch", "query", "label")
DRAFT = ("draft",)
DANGEROUS = ("send", "delete", "archive", "trash", "remove", "batch_modify", "modify")


def classify(tool_name: str) -> str:
    """返回 allow | ask。顺序:危险词优先(send_draft 也须 ask)。"""
    name = tool_name.lower()
    if any(w in name for w in DANGEROUS):
        return "ask"
    if any(w in name for w in DRAFT):
        return "allow"
    if any(w in name for w in READ_ONLY):
        return "allow"
    return "ask"


def enforce(event: Any, **_: Any) -> dict[str, Any]:
    tool_name = str(getattr(event, "tool_name", "") or (event.get("tool_name", "") if isinstance(event, dict) else ""))
    if "gmail" not in tool_name.lower() and "mail" not in tool_name.lower():
        return {"action": "allow"}
    verdict = classify(tool_name)
    if verdict == "ask":
        return {"action": "ask", "reason": f"Gmail 危险操作 {tool_name} 须逐次人工确认(M6 治理分级,无例外)"}
    return {"action": "allow"}
