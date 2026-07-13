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
import math
import time
import uuid
from dataclasses import dataclass, field
from fnmatch import fnmatch
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
                 notifier: "Notifier | None" = None, delegation=None) -> None:
        self._settings = settings
        self._audit = audit
        self._notifier = notifier
        self._pending: dict[str, PendingApproval] = {}
        self._lock = asyncio.Lock()
        self._delegation = delegation      # M30:作用域授权令牌(None=不启用作用域约束)

    def set_delegation(self, token) -> None:
        """设置/更换当前作用域授权令牌(M30)。"""
        self._delegation = token

    def classify(self, action: str, params: dict) -> tuple[str, str]:
        return evaluate(self._settings.policies, self._settings.default_level, action, params)

    # ---- M30:令牌作用域 + 来源可信 硬约束(先于 level_override,deny 优先) ----

    _AMOUNT_KEYS = ("amount_usd", "amount", "usd")

    @classmethod
    def _amount_raw(cls, params: dict):
        """取金额键的原始解析值 (float, 是否存在金额键)。存在但非有限/无法解析 → (nan, True)。"""
        for k in cls._AMOUNT_KEYS:
            if k not in params:
                continue
            v = params[k]
            if isinstance(v, bool):          # bool 是 int 子类,排除
                return math.nan, True
            if isinstance(v, (int, float)):
                return float(v), True
            if isinstance(v, str):
                try:
                    return float(v.strip()), True
                except ValueError:
                    return math.nan, True
            return math.nan, True
        return 0.0, False

    @classmethod
    def _amount(cls, params: dict) -> float:
        """用于预算记账的金额:只有**有限且非负**才计;其余(缺失/nan/inf/负)按 0。"""
        amt, present = cls._amount_raw(params)
        return amt if (present and math.isfinite(amt) and amt >= 0) else 0.0

    def _amount_deny(self, params: dict) -> str | None:
        """金额合法性硬闸(与令牌无关):任何带金额键的动作,非有限(nan/inf)或负数 → deny。
        堵住 amount_usd=nan/inf 同时绕过预算与数值策略(nan 比较恒 False)的漏洞。"""
        amt, present = self._amount_raw(params)
        if not present:
            return None
        if not math.isfinite(amt):
            return f"金额非法(非有限值 {params.get('amount_usd', params.get('amount'))!r})"
        if amt < 0:
            return f"金额非法(负数 {amt})"
        return None

    def _scope_deny(self, action: str, params: dict) -> str | None:
        """令牌作用域硬约束(不含预算——预算走原子预留)。expired/越权 → deny。"""
        tok = self._delegation
        if tok is None:
            return None
        if tok.expired():
            return f"授权令牌已过期(token={tok.token_id})"
        if not tok.allows(action):
            return f"动作 {action!r} 不在授权令牌许可范围 {list(tok.permissions)}"
        return None

    def _provenance_deny(self, action: str, params: dict) -> str | None:
        """来源可信闸。注意:_source 必须由**可信代码**注入——LLM 产生的工具参数在进入
        审批闸前已被 agent 剥除 _source(见 core.tools.sanitize_tool_args),因此 LLM 无法
        自证来源;pure-LLM 循环里受限动作若无可信上游注入 _source 将 fail-closed 被拒。"""
        pats = self._settings.require_verified_source
        if not pats or not any(fnmatch(action, p) for p in pats):
            return None
        src = params.get("_source", "")
        if src in self._settings.trusted_sources:
            return None
        return (f"动作 {action!r} 需可信数据来源(当前 _source={src or '缺失'!r};"
                "llm_output 等不可信来源被拒)")

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
        # M30:令牌越权/过期/金额非法 与 来源不可信 属**硬约束**,先于 level_override 判定,
        # deny 优先(安全工具的 auto 也绕不过——与"显式 deny 盖过 auto"同一不变量)。预算不在
        # 此处判(避免 check→execute 之间的 TOCTOU),而在放行后**原子预留**。
        hard_deny = (self._amount_deny(params) or self._scope_deny(action, params)
                     or self._provenance_deny(action, params))
        if hard_deny:
            level, reason = "deny", hard_deny
        elif level_override:
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

        # M30 预算:原子**预留**(check+扣款在同一把锁内,杜绝并发超支 TOCTOU);
        # 动作最终未成功(拒绝/超时/过期/执行异常)则在 finally 里退款。
        amt = self._amount(params)
        reserved = self._delegation is not None and amt > 0
        if reserved:
            async with self._lock:
                if not self._delegation.budget_ok(amt):
                    reserved = False
                    await _audit("denied")
                    raise ApprovalDenied(action, f"超授权预算(余额 "
                                         f"{self._delegation.remaining_budget():.2f},本次 {amt:.2f})")
                self._delegation.spent_usd += amt

        committed = False
        try:
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
                # M30:等待期间令牌可能过期——批准后、执行前**重新校验**授权(不用陈旧授权执行)
                if self._delegation is not None and self._delegation.expired():
                    await _audit("denied")
                    raise ApprovalDenied(action, "令牌在等待人工审批期间已过期")
                result = await execute()
                committed = True
                await _audit("approved", result=result)
                return result

            # auto
            result = await execute()
            committed = True
            await _audit("executed", result=result)
            return result
        finally:
            if reserved and not committed:               # 未成功 → 退款,不占用预算
                async with self._lock:
                    self._delegation.spent_usd -= amt


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
