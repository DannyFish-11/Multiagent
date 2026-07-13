"""PHASE3 M9 验收:审批中枢 + 分级 + 审计 + 并发安全。"""

from __future__ import annotations

import asyncio

import pytest

from adapters.cost_ledger import CostLedger
from core.approval import ApprovalDenied, ApprovalQueue, ApprovalTimeout, Notifier
from core.audit import AuditLog
from core.config import ApprovalSettings, PolicyRule


def make_queue(tmp_path, policies=(), default="confirm", timeout=5.0):
    settings = ApprovalSettings(
        timeout_s=timeout, audit_path=str(tmp_path / "audit.jsonl"),
        default_level=default, policies=list(policies))
    audit = AuditLog(settings.audit_path)
    return ApprovalQueue(settings, audit, Notifier(settings)), audit


async def _exec(val="done"):
    return val


# ---------------------------------------------------------------- 分级

async def test_auto_executes_and_audits(tmp_path):
    q, audit = make_queue(tmp_path, policies=[
        PolicyRule(action="web_fetch", when={}, level="auto")])
    result = await q.gate(action="web_fetch", params={"url": "x"},
                          execute=lambda: _exec("page"))
    assert result == "page"
    entries = audit.read_all()
    assert entries[-1]["decision"] == "executed" and entries[-1]["level"] == "auto"


async def test_deny_raises_and_audits(tmp_path):
    q, audit = make_queue(tmp_path, policies=[
        PolicyRule(action="pay*", when={"amount_usd__gte": 1000}, level="deny",
                   reason="超大额禁止")])
    with pytest.raises(ApprovalDenied):
        await q.gate(action="pay_card", params={"amount_usd": 5000}, execute=lambda: _exec())
    assert audit.read_all()[-1]["decision"] == "denied"


async def test_confirm_blocks_until_approved(tmp_path):
    q, audit = make_queue(tmp_path, policies=[
        PolicyRule(action="gmail_send", when={}, level="confirm")], timeout=5.0)

    task = asyncio.create_task(
        q.gate(action="gmail_send", params={"to": "a@b.com"}, execute=lambda: _exec("sent")))
    await asyncio.sleep(0.05)
    pending = q.list_pending()
    assert len(pending) == 1 and pending[0]["action"] == "gmail_send"

    assert await q.resolve(pending[0]["id"], approved=True)
    assert await task == "sent"
    assert audit.read_all()[-1]["decision"] == "approved"


async def test_confirm_rejected(tmp_path):
    q, _ = make_queue(tmp_path, default="confirm", timeout=5.0)
    task = asyncio.create_task(
        q.gate(action="gmail_send", params={}, execute=lambda: _exec()))
    await asyncio.sleep(0.05)
    pid = q.list_pending()[0]["id"]
    await q.resolve(pid, approved=False)
    with pytest.raises(ApprovalDenied):
        await task


async def test_confirm_timeout_cancels(tmp_path):
    q, audit = make_queue(tmp_path, default="confirm", timeout=0.2)
    with pytest.raises(ApprovalTimeout):
        await q.gate(action="gmail_send", params={}, execute=lambda: _exec())
    assert audit.read_all()[-1]["decision"] == "timeout"
    assert q.list_pending() == []  # 超时后出队


# ---------------------------------------------------------------- 声明式规则

async def test_declarative_first_match_wins(tmp_path):
    q, _ = make_queue(tmp_path, policies=[
        PolicyRule(action="web_*", when={"url__regex": r"evil\.com"}, level="deny"),
        PolicyRule(action="web_fetch", when={}, level="auto"),
        PolicyRule(action="web_submit", when={}, level="confirm"),
    ])
    assert q.classify("web_fetch", {"url": "http://good.com"})[0] == "auto"
    assert q.classify("web_fetch", {"url": "http://evil.com/x"})[0] == "deny"
    assert q.classify("web_submit", {"url": "http://good.com"})[0] == "confirm"


async def test_nonfinite_or_negative_amount_hard_denied(tmp_path):
    """回归:amount_usd=nan/inf/负数 曾同时绕过预算记账与数值策略(nan 比较恒 False),
    须被金额硬闸拒绝——即便 policy 表把该动作配成 auto。"""
    q, audit = make_queue(tmp_path, policies=[
        PolicyRule(action="pay", when={}, level="auto")])  # 恶意/误配为 auto
    for bad in (float("nan"), float("inf"), float("-inf"), -1.0, "nan", "not-a-number", True):
        with pytest.raises(ApprovalDenied):
            await q.gate(action="pay", params={"amount_usd": bad},
                         source="user", execute=lambda: _exec())
        assert audit.read_all()[-1]["decision"] == "denied"
    # 合法正常金额仍放行(auto)
    assert await q.gate(action="pay", params={"amount_usd": 3.0},
                        source="user", execute=lambda: _exec("ok")) == "ok"


def test_predicate_operators(tmp_path):
    from core.policy_engine import evaluate

    rules = [
        PolicyRule(action="pay", when={"amount_usd__gte": 1.0}, level="confirm"),
        PolicyRule(action="pay", when={}, level="auto"),
    ]
    assert evaluate(rules, "deny", "pay", {"amount_usd": 5.0})[0] == "confirm"
    assert evaluate(rules, "deny", "pay", {"amount_usd": 0.5})[0] == "auto"


# ---------------------------------------------------------------- 并发

async def test_concurrent_confirms_isolated(tmp_path):
    """多个并发 confirm 各自独立入队、按 id 精确解析,互不串扰。"""
    q, _ = make_queue(tmp_path, default="confirm", timeout=5.0)
    tasks = [asyncio.create_task(
        q.gate(action="act", params={"i": i}, session_id=f"s{i}", execute=lambda i=i: _exec(i)))
        for i in range(10)]
    await asyncio.sleep(0.1)
    pending = q.list_pending()
    assert len(pending) == 10
    # 只批准偶数 id 对应的项
    by_session = {p["params"]["i"]: p["id"] for p in pending}
    for i in range(10):
        await q.resolve(by_session[i], approved=(i % 2 == 0))
    results = await asyncio.gather(*tasks, return_exceptions=True)
    for i, r in enumerate(results):
        if i % 2 == 0:
            assert r == i
        else:
            assert isinstance(r, ApprovalDenied)


def test_cost_ledger_thread_safe(tmp_path):
    """CostLedger 并发 record 无丢失(M9.1 并发安全)。"""
    import threading

    ledger = CostLedger({"m": {"input": 1.0, "output": 0.0}}, 1e9, tmp_path / "l.json")

    def worker():
        for _ in range(200):
            ledger.record("ep", "m", 1000)

    threads = [threading.Thread(target=worker) for _ in range(8)]
    for t in threads:
        t.start()
    for t in threads:
        t.join()
    # 8*200*1000 tokens * $1/1e6 = $1.6
    assert abs(ledger.today_usd() - 1.6) < 1e-6


async def test_audit_records_who_what_result(tmp_path):
    q, audit = make_queue(tmp_path, policies=[
        PolicyRule(action="web_fetch", when={}, level="auto")])
    await q.gate(action="web_fetch", params={"url": "http://x"}, source="user",
                 agent_id="agent-1", session_id="sess-1", execute=lambda: _exec("ok"))
    e = audit.read_all()[-1]
    assert e["agent_id"] == "agent-1" and e["session_id"] == "sess-1"
    assert e["source"] == "user" and e["action"] == "web_fetch"
    assert e["result"] == "ok"


# ---------------------------------------------------------------- API 端点

async def test_approvals_endpoints(tmp_path):
    """GET /approvals + approve 端点(单事件循环,ASGITransport)。"""
    import httpx
    from asgi_lifespan import LifespanManager

    from services.api import create_app
    from tests.conftest import EchoMemoryLLM, make_fake_config
    from adapters.embedder import build_embedder
    from adapters.memory import QdrantMemoryStore
    from adapters.vectordb import QdrantAdapter

    cfg = make_fake_config(tmp_path)
    cfg.approval.audit_path = str(tmp_path / "audit.jsonl")
    cfg.approval.default_level = "confirm"
    cfg.approval.timeout_s = 10.0
    embedder = build_embedder(cfg.embedder)
    db = QdrantAdapter(cfg.vectordb, dim=cfg.embedder.effective_dim)
    store = QdrantMemoryStore(embedder, EchoMemoryLLM(), db, cfg)
    app = create_app(cfg, llm=EchoMemoryLLM(), memory=store, skip_dependency_checks=True)

    async with LifespanManager(app):
        transport = httpx.ASGITransport(app=app)
        async with httpx.AsyncClient(transport=transport, base_url="http://t") as client:
            assert (await client.get("/approvals")).json()["pending"] == []

            # 在同一事件循环里驱动一个 confirm 动作入队
            async def _exec():
                return "sent"
            gate_task = asyncio.create_task(
                app.state.approvals.gate(action="gmail_send", params={"to": "a@b.com"},
                                         execute=_exec))
            for _ in range(50):
                pend = (await client.get("/approvals")).json()["pending"]
                if pend:
                    break
                await asyncio.sleep(0.02)
            assert pend and pend[0]["action"] == "gmail_send"

            r = await client.post(f"/approvals/{pend[0]['id']}/approve")
            assert r.json()["resolved"] is True
            assert await gate_task == "sent"

            audit = (await client.get("/audit")).json()
            assert any(e["decision"] == "approved" for e in audit["entries"])
            assert (await client.post("/approvals/nonexistent/approve")).status_code == 404
