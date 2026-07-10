"""A2A 委托鉴权策略(M5.2,Omnigent function policy)。

来自未知 agent_id 的委托默认 ask(人工批准);白名单 agent_id 放行。
白名单的权威副本是记忆库中 kind=trust_whitelist 的记忆(core/trust.py,可检索
可审计);本策略进程无法直接查询记忆库,采用环境变量镜像 A2A_TRUSTED_AGENTS
(逗号分隔,由宿主启动时从 TrustStore.audit() 导出)。两者以记忆库为准。
"""

from __future__ import annotations

import os
from typing import Any


def enforce(event: Any, **_: Any) -> dict[str, Any]:
    tool_name = str(getattr(event, "tool_name", "") or (event.get("tool_name", "") if isinstance(event, dict) else ""))
    if not tool_name.lower().startswith(("a2a", "delegate")):
        return {"action": "allow"}
    args = getattr(event, "arguments", None) or (event.get("arguments") if isinstance(event, dict) else None) or {}
    from_agent = str(args.get("from_agent_id", ""))
    whitelist = {a.strip() for a in os.environ.get("A2A_TRUSTED_AGENTS", "").split(",") if a.strip()}
    if from_agent and from_agent in whitelist:
        return {"action": "allow"}
    return {
        "action": "ask",
        "reason": f"来自未知 agent_id={from_agent!r} 的 A2A 委托,默认需人工批准(M5 鉴权策略)",
    }
