"""支付适配层(PHASE3 M12)——解锁附录 A 的封印,带硬性笼子。

路径优先级:virtual_card(首选)→ x402(补充)。供应商由人类选定并开户(停点)。
硬性笼子(缺一不得上线,全部 config 化):单笔/日/月三层独立计数 + confirm 阈值
+ 可选商户白名单。AP2 留形:审计模拟 Intent/Cart 双记录结构。

来源检查在 core.payment_guard(仅人类会话可发起支付链);本 adapter 只管
"记账笼子 + 真实开卡/结算调用",不做审批(审批由 ApprovalQueue 承担)。
"""

from __future__ import annotations

import json
import math
import threading
import time
import uuid
from pathlib import Path

import httpx

from core.config import PaymentsSettings
from core.errors import LayerError
from core.payment_guard import assert_human_initiated


class PaymentDenied(LayerError):
    def __init__(self, reason: str) -> None:
        super().__init__("L12", "payments", f"支付被拒绝: {reason}")


class PaymentLedger:
    """单笔/日/月三层累计,并发安全,持久化挂 volume。"""

    def __init__(self, path: str | Path) -> None:
        self._path = Path(path)
        self._lock = threading.Lock()
        self._txns: list[dict] = []
        if self._path.exists():
            try:
                self._txns = json.loads(self._path.read_text(encoding="utf-8"))
            except (json.JSONDecodeError, OSError):
                self._txns = []

    def _save(self) -> None:
        self._path.parent.mkdir(parents=True, exist_ok=True)
        self._path.write_text(json.dumps(self._txns, ensure_ascii=False, indent=2),
                              encoding="utf-8")

    def _sum_since(self, since_ts: float) -> float:
        return sum(t["amount_usd"] for t in self._txns if t["ts"] >= since_ts)

    def day_total(self) -> float:
        return self._sum_since(time.time() - 86400)

    def month_total(self) -> float:
        return self._sum_since(time.time() - 30 * 86400)

    def reserve(self, amount: float, settings: PaymentsSettings) -> str:
        """三层笼子校验 + **原子预留**:在同一把锁内查限额并追加占位记录,金额立即计入
        日/月累计——杜绝 check→charge 之间的 TOCTOU 并发超支。返回预留 id。"""
        with self._lock:
            if amount > settings.per_tx_usd:
                raise PaymentDenied(f"单笔 ${amount:.2f} > 上限 ${settings.per_tx_usd:.2f}")
            if self.day_total() + amount > settings.daily_usd:
                raise PaymentDenied(
                    f"日累计将达 ${self.day_total() + amount:.2f} > 上限 ${settings.daily_usd:.2f}")
            if self.month_total() + amount > settings.monthly_usd:
                raise PaymentDenied(
                    f"月累计将达 ${self.month_total() + amount:.2f} > 上限 ${settings.monthly_usd:.2f}")
            rid = uuid.uuid4().hex
            self._txns.append({"ts": time.time(), "amount_usd": amount,
                               "_rid": rid, "_pending": True})
            self._save()
            return rid

    def finalize(self, rid: str, extra: dict) -> None:
        """预留兑现:把占位记录补全为正式交易。"""
        with self._lock:
            for t in self._txns:
                if t.get("_rid") == rid:
                    t.pop("_pending", None)
                    t.update(extra)
                    break
            self._save()

    def refund(self, rid: str) -> None:
        """预留回滚(支付失败时):移除占位,释放额度。"""
        with self._lock:
            self._txns = [t for t in self._txns if t.get("_rid") != rid]
            self._save()


class PaymentsAdapter:
    def __init__(self, settings: PaymentsSettings, ledger: PaymentLedger | None = None,
                 transport: httpx.AsyncBaseTransport | None = None) -> None:
        self._settings = settings
        self._ledger = ledger or PaymentLedger(settings.ledger_path)
        self._transport = transport

    def _check_payee(self, payee: str) -> None:
        if self._settings.whitelist_enabled:
            if payee not in self._settings.payee_whitelist:
                raise PaymentDenied(f"商户白名单模式:{payee!r} 不在名单")

    async def pay(self, *, amount_usd: float, payee: str, purpose: str, source: str) -> dict:
        """执行一次支付(笼子校验在前)。source 必填:非人类来源(email/web)一律拒(硬红线,
        与审批级别正交)。金额须为有限正数。原子预留额度 → 真实结算 → 兑现;失败退款。"""
        assert_human_initiated(source)          # 硬红线:仅人类会话可发起支付链
        if not self._settings.enabled:
            raise PaymentDenied("支付能力未启用(附录 A 默认拒付;config.payments.enabled=false)")
        if self._settings.provider == "none":
            raise PaymentDenied("未配置支付供应商(停点:虚拟卡/x402 供应商由人类选定开户)")
        if not isinstance(amount_usd, (int, float)) or isinstance(amount_usd, bool) \
                or not math.isfinite(amount_usd) or amount_usd <= 0:
            raise PaymentDenied(f"金额非法(须为有限正数):{amount_usd!r}")
        self._check_payee(payee)
        rid = self._ledger.reserve(float(amount_usd), self._settings)   # 原子预留(闭 TOCTOU)

        try:
            if self._settings.provider == "virtual_card":
                result = await self._virtual_card_charge(amount_usd, payee)
            elif self._settings.provider == "x402":
                result = await self._x402_settle(amount_usd, payee)
            else:
                raise PaymentDenied(f"未知供应商: {self._settings.provider}")
        except Exception:
            self._ledger.refund(rid)            # 结算失败 → 释放预留额度
            raise

        # AP2 留形:意图与购物车分离记录(未来平移到 AP2 Mandate)
        intent = {"type": "intent", "amount_usd": amount_usd, "payee": payee,
                  "purpose": purpose, "ts": time.time()}
        cart = {"type": "cart", "amount_usd": amount_usd, "payee": payee, "ts": time.time()}
        extra = {"payee": payee, "purpose": purpose, "source": source,
                 "provider": self._settings.provider, "intent": intent, "cart": cart,
                 "result": result}
        self._ledger.finalize(rid, extra)
        return {"ts": time.time(), "amount_usd": amount_usd, **extra}

    async def _virtual_card_charge(self, amount: float, payee: str) -> dict:
        """单次限额虚拟卡:开卡(限额=amount)→ 返回卡号供付款 → 用完即焚。"""
        base = self._settings.provider_base_url
        if not base:
            raise PaymentDenied("虚拟卡服务未配置 provider_base_url")
        async with httpx.AsyncClient(timeout=30, transport=self._transport) as client:
            resp = await client.post(
                f"{base}/cards", headers={"Authorization": f"Bearer {self._settings.provider_api_key}"},
                json={"spend_limit_usd": amount, "single_use": True, "memo": payee})
            if resp.status_code >= 300:
                raise PaymentDenied(f"开卡失败 HTTP {resp.status_code}: {resp.text[:200]}")
            card = resp.json()
        return {"method": "virtual_card", "card_id": card.get("id"),
                "last4": card.get("last4"), "single_use": True}

    async def _x402_settle(self, amount: float, payee: str) -> dict:
        """x402 稳定币:专用小额热钱包结算(人类充值)。"""
        base = self._settings.provider_base_url
        if not base:
            raise PaymentDenied("x402 未配置 provider_base_url")
        async with httpx.AsyncClient(timeout=30, transport=self._transport) as client:
            resp = await client.post(
                f"{base}/settle", headers={"Authorization": f"Bearer {self._settings.provider_api_key}"},
                json={"amount_usd": amount, "payee": payee})
            if resp.status_code >= 300:
                raise PaymentDenied(f"结算失败 HTTP {resp.status_code}: {resp.text[:200]}")
            return {"method": "x402", **resp.json()}
