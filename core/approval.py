"""审批中枢 ApprovalQueue(PHASE3 M9.2)——无 Omnigent 形态下所有危险动作的守门人。

动作分三级(policy_engine 按 config 声明式判级):
  auto    → 直接执行
  confirm → 入待批队列,阻塞至人类 approve / reject 或超时取消
  deny    → 直接拒绝并告警

用法(工具适配器包裹一次危险动作):
    result = await queue.gate(
        action="gmail_send", params={...}, source="user",
        agent_id=..., session_id=...,
        execute=lambda: real_send(...),
    )

所有动作(含 auto)全量审计。confirm 通知经 config(webhook / 邮件,最简形态)。
"""

from __future__ import annotations

import asyncio
import time
import uuid
from dataclasses import dataclass, field
from typing import Any, Awaitable, Callable

from core.audit import AuditLog
from core.config import ApprovalSettings
from core.errors import LayerError
from core.policy_engine import evaluate


class ApprovalDenied(LayerError):
    def __init__(self, action: str, reason: str) -> None:
        super().__init__("L9", "approval", f"动作被拒绝 [{action}]: {reason}")


class ApprovalTimeout(LayerError):
    def __init__(self, action: str, timeout_s: float) -> None:
        super().__init__("L9", "approval", f"动作 [{action}] 等待人工批准超时({timeout_s}s),已取消")


@dataclass
class PendingApproval:
    id: str
    action: str
    params: dict
    source: str
    agent_id: str
    session_id: str
    reason: str
    created_at: float = field(default_factory=time.time)
    _event: asyncio.Event = field(default_factory=asyncio.Event, repr=False)
    _decision: str = ""  # approved | rejected

    def public(self) -> dict[str, Any]:
        from core.audit import _summarize

        return {
            "id": self.id, "action": self.action, "params": _summarize(self.params),
            "source": self.source, "agent_id": self.agent_id, "session_id": self.session_id,
            "reason": self.reason, "created_at": self.created_at,
        }


class ApprovalQueue:
    def __init__(self, settings: ApprovalSettings, audit: AuditLog,
                 notifier: "Notifier | None" = None) -> None:
        self._settings = settings
        self._audit = audit
        self._notifier = notifier
        self._pending: dict[str, PendingApproval] = {}
        self._lock = asyncio.Lock()

    def classify(self, action: str, params: dict) -> tuple[str, str]:
        return evaluate(self._settings.policies, self._settings.default_level, action, params)

    def list_pending(self) -> list[dict]:
        return [p.public() for p in self._pending.values()]

    async def resolve(self, approval_id: str, approved: bool) -> bool:
        async with self._lock:
            pa = self._pending.get(approval_id)
            if pa is None:
                return False
            pa._decision = "approved" if approved else "rejected"
            pa._event.set()
        return True

    async def gate(self, *, action: str, params: dict,
                   execute: Callable[[], Awaitable[Any]],
                   source: str = "user", agent_id: str = "", session_id: str = "",
                   level_override: str | None = None) -> Any:
        """按级别放行/入队/拒绝一次危险动作。返回 execute() 结果(auto/approved)。"""
        if level_override:
            # 调用方给的 override(如安全工具的 auto)不得盖过**显式 deny 策略**:
            # 仍走一次分级,命中 deny 则 deny 优先(防工具自升级绕过治理)。
            classified, creason = self.classify(action, params)
            level, reason = (("deny", creason) if classified == "deny"
                             else (level_override, "调用方指定级别"))
        else:
            level, reason = self.classify(action, params)

        async def _audit(decision: str, result: Any = None, cost: float = 0.0) -> None:
            await self._audit.record(
                action=action, level=level, decision=decision, source=source,
                agent_id=agent_id, session_id=session_id, params=params,
                result=result, cost_usd=cost, extra={"reason": reason},
            )

        if level == "deny":
            await _audit("denied")
            if self._notifier:
                await self._notifier.notify(f"[DENY] {action}: {reason}")
            raise ApprovalDenied(action, reason)

        if level == "confirm":
            pa = PendingApproval(id=uuid.uuid4().hex, action=action, params=params,
                                 source=source, agent_id=agent_id, session_id=session_id,
                                 reason=reason)
            async with self._lock:
                self._pending[pa.id] = pa
            if self._notifier:
                await self._notifier.notify(
                    f"[CONFIRM] 待批准动作 {action}(id={pa.id},来源={source}):{reason}")
            try:
                await asyncio.wait_for(pa._event.wait(), timeout=self._settings.timeout_s)
            except asyncio.TimeoutError:
                async with self._lock:
                    self._pending.pop(pa.id, None)
                await _audit("timeout")
                raise ApprovalTimeout(action, self._settings.timeout_s) from None
            async with self._lock:
                self._pending.pop(pa.id, None)
            if pa._decision != "approved":
                await _audit("rejected")
                raise ApprovalDenied(action, "人工拒绝")
            # 批准 → 执行
            result = await execute()
            await _audit("approved", result=result)
            return result

        # auto
        result = await execute()
        await _audit("executed", result=result)
        return result


class Notifier:
    """最简通知:webhook POST 或(留形)邮件。config.approval.notify 选择。"""

    def __init__(self, settings: ApprovalSettings) -> None:
        self._settings = settings

    async def notify(self, message: str) -> None:
        if self._settings.notify == "webhook" and self._settings.webhook_url:
            import httpx

            try:
                async with httpx.AsyncClient(timeout=10) as client:
                    await client.post(self._settings.webhook_url, json={"text": message})
            except httpx.HTTPError:
                pass  # 通知失败不阻断主流程(审批本身已入队/已审计)
        # notify == "email":沿用 M11 Gmail 适配器,由宿主装配时注入;此处留形
