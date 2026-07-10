"""PHASE3 M12 验收:支付解锁 + 硬性笼子 + 来源检查 + 支付记忆。"""

from __future__ import annotations

import httpx
import pytest

from adapters.payments import PaymentDenied, PaymentLedger, PaymentsAdapter
from core.approval import ApprovalDenied, ApprovalQueue, Notifier
from core.audit import AuditLog
from core.config import ApprovalSettings, PaymentsSettings, PolicyRule
from core.payment_guard import PaymentSourceDenied, assert_human_initiated


def settings(**kw):
    base = dict(enabled=True, provider="virtual_card",
                provider_base_url="http://vcard", provider_api_key="k",
                per_tx_usd=10.0, daily_usd=20.0, monthly_usd=100.0,
                confirm_threshold_usd=1.0)
    base.update(kw)
    return PaymentsSettings(**base)


def card_ok(request):
    return httpx.Response(200, json={"id": "card_1", "last4": "4242"})


def adapter(tmp_path, s=None, handler=card_ok):
    s = s or settings()
    ledger = PaymentLedger(tmp_path / "pay.json")
    return PaymentsAdapter(s, ledger=ledger, transport=httpx.MockTransport(handler)), ledger


# ---------------------------------------------------------------- 基本支付

async def test_virtual_card_payment_records_memory_structure(tmp_path):
    pay, ledger = adapter(tmp_path)
    txn = await pay.pay(amount_usd=5.0, payee="shop.com", purpose="买书")
    assert txn["result"]["method"] == "virtual_card"
    assert txn["result"]["single_use"] is True
    # AP2 留形:Intent/Cart 双记录
    assert txn["intent"]["type"] == "intent" and txn["cart"]["type"] == "cart"
    assert ledger.day_total() == 5.0


async def test_disabled_is_default_deny(tmp_path):
    pay, _ = adapter(tmp_path, settings(enabled=False))
    with pytest.raises(PaymentDenied) as exc:
        await pay.pay(amount_usd=1.0, payee="x", purpose="y")
    assert "附录 A" in str(exc.value)


# ---------------------------------------------------------------- 三层笼子

async def test_per_tx_cap(tmp_path):
    pay, _ = adapter(tmp_path, settings(per_tx_usd=10.0))
    with pytest.raises(PaymentDenied) as exc:
        await pay.pay(amount_usd=10.01, payee="x", purpose="y")
    assert "单笔" in str(exc.value)


async def test_daily_cap(tmp_path):
    pay, _ = adapter(tmp_path, settings(daily_usd=20.0))
    await pay.pay(amount_usd=8.0, payee="x", purpose="a")
    await pay.pay(amount_usd=8.0, payee="x", purpose="b")
    with pytest.raises(PaymentDenied) as exc:
        await pay.pay(amount_usd=8.0, payee="x", purpose="c")  # 累计 24 > 20
    assert "日累计" in str(exc.value)


async def test_monthly_cap(tmp_path):
    pay, ledger = adapter(tmp_path, settings(daily_usd=1000.0, monthly_usd=15.0))
    await pay.pay(amount_usd=10.0, payee="x", purpose="a")
    with pytest.raises(PaymentDenied) as exc:
        await pay.pay(amount_usd=6.0, payee="x", purpose="b")  # 月累计 16 > 15
    assert "月累计" in str(exc.value)


async def test_payee_whitelist(tmp_path):
    pay, _ = adapter(tmp_path, settings(whitelist_enabled=True, payee_whitelist=["trusted.com"]))
    await pay.pay(amount_usd=1.0, payee="trusted.com", purpose="ok")
    with pytest.raises(PaymentDenied) as exc:
        await pay.pay(amount_usd=1.0, payee="random.com", purpose="no")
    assert "白名单" in str(exc.value)


def test_ledger_persists(tmp_path):
    l1 = PaymentLedger(tmp_path / "p.json")
    l1.record({"ts": __import__("time").time(), "amount_usd": 7.0, "payee": "x"})
    l2 = PaymentLedger(tmp_path / "p.json")
    assert l2.day_total() == 7.0


# ---------------------------------------------------------------- 来源检查(硬红线)

def test_source_guard_only_human():
    assert_human_initiated("user")  # 放行
    for bad in ("email", "web", "timer"):
        with pytest.raises(PaymentSourceDenied):
            assert_human_initiated(bad)


async def test_payment_never_triggered_by_email_or_web(tmp_path):
    """负向:邮件驱动/网页内容发起的支付链一律被来源检查拒绝(即便金额低于阈值)。"""
    pay, _ = adapter(tmp_path)

    async def attempt(source: str):
        assert_human_initiated(source)  # 支付链第一道闸:来源检查
        return await pay.pay(amount_usd=0.5, payee="x", purpose="tiny")

    # 人类会话:通过
    assert (await attempt("user"))["amount_usd"] == 0.5
    # 邮件 / 网页:即便 $0.5 < confirm 阈值,也在来源检查处被拒
    for bad in ("email", "web"):
        with pytest.raises(PaymentSourceDenied):
            await attempt(bad)


# ---------------------------------------------------------------- 与审批中枢组合

async def test_confirm_threshold_via_approval(tmp_path):
    """≥ confirm_threshold 走 confirm;低于则 auto —— 由声明式 policy 表达。"""
    s = ApprovalSettings(
        audit_path=str(tmp_path / "a.jsonl"), default_level="auto",
        policies=[PolicyRule(action="payment", when={"amount_usd__gte": 1.0}, level="confirm"),
                  PolicyRule(action="payment", when={}, level="auto")])
    q = ApprovalQueue(s, AuditLog(s.audit_path), Notifier(s))
    assert q.classify("payment", {"amount_usd": 5.0})[0] == "confirm"
    assert q.classify("payment", {"amount_usd": 0.5})[0] == "auto"


async def test_over_budget_payment_denied_by_policy(tmp_path):
    s = ApprovalSettings(
        audit_path=str(tmp_path / "a.jsonl"), default_level="auto",
        policies=[PolicyRule(action="payment", when={"amount_usd__gte": 10.0}, level="deny",
                             reason="超单笔上限")])
    audit = AuditLog(s.audit_path)
    q = ApprovalQueue(s, audit, Notifier(s))
    with pytest.raises(ApprovalDenied):
        await q.gate(action="payment", params={"amount_usd": 50.0},
                     source="user", execute=lambda: _ok())
    assert audit.read_all()[-1]["decision"] == "denied"


async def _ok():
    return {"ok": True}
