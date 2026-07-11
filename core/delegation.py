"""Delegation Token(M30)——作用域授权令牌:给一次自主运行套上"临时工牌"。

我们原有的审批是**逐动作**分级(M9.2);令牌在其之上加一层**贯穿整个 run 的作用域**:
  - permissions:这次运行只能做这些动作(fnmatch 模式);范围外一律 deny;
  - max_budget_usd:累计花费上限(0=不限);超支 deny;
  - valid_until:时效(0=不过期);过期 deny;
  - transferable=False:不可转授权(签名绑定签发者身份,A2A 再委派须重签);
令牌由 AgentIdentity 的 Ed25519 私钥签名(复用 M5,不引新依赖),verify() 校验签名+时效。

治理刚性:令牌校验在审批闸里**先于** level_override 生效——安全工具(auto)也绕不过
过期/越权/超支的令牌(与"显式 deny 盖过 auto"同一条安全不变量)。默认关(需显式签发)。

已知边界(当前自签发、进程内使用是安全的;若将来令牌从**外部**传入需注意):
  - verify() 默认用令牌自带的 public_key 验签,只证明"被所附密钥签过",不证明"被可信签发者
    签过"。外部令牌必须显式传入**已知签发者**的 public_key(pinning),否则等于自证。
  - spent_usd 是进程内运行时累计(不进签名负载,以防篡改),**进程重启即归零**——长命/重启
    循环的 agent 每个进程周期可再花满 max_budget_usd。需跨进程额度请接 CostLedger 持久化。
"""

from __future__ import annotations

import time
from dataclasses import dataclass, field
from fnmatch import fnmatch

from core.identity import AgentIdentity, uuid7, verify_signature


@dataclass
class DelegationToken:
    token_id: str
    issuer: str                       # 签发者(agent_id;或人类审批者标识)
    task: str                         # 任务描述(审计可读)
    permissions: tuple[str, ...]      # 许可动作 fnmatch 模式;空=什么都不许
    max_budget_usd: float             # 累计花费上限;0=不限
    issued_at: float
    valid_until: float                # 过期时刻(epoch 秒);0=不过期
    transferable: bool                # 是否可转授权(A2A 再委派)
    agent_id: str                     # 绑定的 agent
    # 签名(不进签名负载):
    protected: str = ""
    signature: str = ""
    public_key: str = ""
    # 运行时累计已花(不进签名负载,不参与验签):
    spent_usd: float = field(default=0.0)

    def payload(self) -> dict:
        """进签名的确定性负载(不含签名与运行时 spent)。"""
        return {
            "token_id": self.token_id, "issuer": self.issuer, "task": self.task,
            "permissions": list(self.permissions), "max_budget_usd": self.max_budget_usd,
            "issued_at": self.issued_at, "valid_until": self.valid_until,
            "transferable": self.transferable, "agent_id": self.agent_id,
        }

    # ---- 校验谓词 ----

    def expired(self, now: float | None = None) -> bool:
        now = time.time() if now is None else now
        return self.valid_until != 0 and now >= self.valid_until

    def allows(self, action: str) -> bool:
        return any(fnmatch(action, p) for p in self.permissions)

    def budget_ok(self, amount: float) -> bool:
        if self.max_budget_usd <= 0:
            return True
        return (self.spent_usd + max(0.0, amount)) <= self.max_budget_usd + 1e-9

    def remaining_budget(self) -> float:
        return max(0.0, self.max_budget_usd - self.spent_usd) if self.max_budget_usd > 0 else float("inf")

    def public(self) -> dict:
        """脱敏视图(诊断/审计用):不含签名。"""
        return {**self.payload(), "spent_usd": round(self.spent_usd, 6),
                "remaining_usd": self.remaining_budget()}


def issue(identity: AgentIdentity, *, task: str, permissions, max_budget_usd: float = 0.0,
          ttl_s: float = 0.0, transferable: bool = False, issuer: str = "",
          now: float | None = None) -> DelegationToken:
    """由身份签发一张令牌(Ed25519 签名)。ttl_s=0 → 不过期;max_budget_usd=0 → 不限额。"""
    now = time.time() if now is None else now
    tok = DelegationToken(
        token_id=uuid7(), issuer=issuer or identity.agent_id, task=task,
        permissions=tuple(permissions), max_budget_usd=float(max_budget_usd),
        issued_at=now, valid_until=(now + ttl_s if ttl_s else 0.0),
        transferable=bool(transferable), agent_id=identity.agent_id)
    sig = identity.sign(tok.payload())
    tok.protected, tok.signature = sig["protected"], sig["signature"]
    tok.public_key = identity.public_key_b64()
    return tok


def verify(token: DelegationToken, *, public_key: str | None = None,
           now: float | None = None) -> bool:
    """验签 + 时效。签名无效或已过期均返回 False(不抛)。"""
    pub = public_key or token.public_key
    if not verify_signature(token.payload(), token.protected, token.signature, pub):
        return False
    return not token.expired(now)
