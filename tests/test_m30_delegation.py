"""M30 验收:作用域授权令牌(①)+ 来源可信闸(②)。

covers:令牌签发/验签/篡改/过期;allows/budget/expired 谓词;审批闸强制令牌
permissions/预算/时效(deny 优先于 level_override,安全工具也绕不过);预算累计;
provenance 闸(llm_output 被拒、erp_verified 放行、非受限动作不受影响);services 装配
令牌;doctor 预检。离线,复用 M5 Ed25519 身份签名,不引新依赖。
"""

from __future__ import annotations

import core.delegation as dele
from core.approval import ApprovalDenied, ApprovalQueue, Notifier
from core.audit import AuditLog
from core.config import ApprovalSettings, load_config
from core.identity import AgentIdentity


class FakeMemory:
    async def search(self, query, k=5):
        return []

    async def add(self, inp, meta=None):
        return "id"


def _identity(tmp_path):
    return AgentIdentity.load_or_create(tmp_path / "id")


def _queue(tmp_path, settings=None, token=None):
    settings = settings or ApprovalSettings(default_level="auto")
    return ApprovalQueue(settings, AuditLog(tmp_path / "a.jsonl"), Notifier(settings),
                         delegation=token)


# ---------------------------------------------------------------- 令牌本体

def test_issue_and_verify_roundtrip(tmp_path):
    ident = _identity(tmp_path)
    tok = dele.issue(ident, task="询价采购", permissions=["send_inquiry", "negotiate"],
                     max_budget_usd=5_000_000, ttl_s=7200, now=1000.0)
    assert tok.issuer == ident.agent_id and tok.agent_id == ident.agent_id
    assert tok.valid_until == 1000.0 + 7200
    assert dele.verify(tok, now=1000.0) is True                 # 签名有效 + 未过期


def test_tampered_token_fails_verify(tmp_path):
    tok = dele.issue(_identity(tmp_path), task="t", permissions=["a"], max_budget_usd=100)
    tok.max_budget_usd = 999999                                 # 篡改负载
    assert dele.verify(tok) is False                            # 签名与负载不符


def test_expired_token_fails_verify(tmp_path):
    tok = dele.issue(_identity(tmp_path), task="t", permissions=["a"], ttl_s=10, now=1000.0)
    assert dele.verify(tok, now=1005.0) is True
    assert dele.verify(tok, now=1011.0) is False                # 过期
    assert tok.expired(now=1011.0) is True


def test_token_predicates(tmp_path):
    tok = dele.issue(_identity(tmp_path), task="t", permissions=["web_*", "recall"],
                     max_budget_usd=100, now=1.0)
    assert tok.allows("web_search") and tok.allows("recall") and not tok.allows("payment")
    assert tok.budget_ok(50) and not tok.budget_ok(150)
    tok.spent_usd = 80
    assert tok.budget_ok(20) and not tok.budget_ok(21)
    assert tok.remaining_budget() == 20


# ---------------------------------------------------------------- 审批闸:令牌作用域

async def _run(queue, action, params, ran):
    async def _do():
        ran.append(action)
        return "ok"
    return await queue.gate(action=action, params=params, execute=_do,
                            level_override="auto")               # 模拟安全工具直放


async def test_action_in_permissions_executes(tmp_path):
    tok = dele.issue(_identity(tmp_path), task="t", permissions=["recall", "remember"])
    q = _queue(tmp_path, token=tok)
    ran = []
    assert await _run(q, "recall", {}, ran) == "ok" and ran == ["recall"]


async def test_action_outside_permissions_denied_even_when_safe(tmp_path):
    """治理刚性:令牌未许可的动作,即便调用方 override=auto 也被 deny。"""
    tok = dele.issue(_identity(tmp_path), task="t", permissions=["recall"])
    q = _queue(tmp_path, token=tok)
    ran = []
    try:
        await _run(q, "payment", {"amount_usd": 1}, ran)
        assert False, "应被拒"
    except ApprovalDenied as e:
        assert "许可范围" in str(e)
    assert ran == []                                            # execute 从未运行


async def test_expired_token_denies_at_gate(tmp_path):
    tok = dele.issue(_identity(tmp_path), task="t", permissions=["recall"], ttl_s=10, now=1.0)
    tok.valid_until = 1.0                                       # 立即过期(now>=valid_until)
    q = _queue(tmp_path, token=tok)
    ran = []
    try:
        await _run(q, "recall", {}, ran)
        assert False
    except ApprovalDenied as e:
        assert "过期" in str(e)
    assert ran == []


async def test_budget_accumulates_and_caps(tmp_path):
    tok = dele.issue(_identity(tmp_path), task="t", permissions=["payment"], max_budget_usd=10.0)
    q = _queue(tmp_path, token=tok)
    ran = []
    assert await _run(q, "payment", {"amount_usd": 6}, ran) == "ok"   # 花 6
    assert tok.spent_usd == 6
    try:
        await _run(q, "payment", {"amount_usd": 6}, ran)              # 6+6>10 → 拒
        assert False
    except ApprovalDenied as e:
        assert "预算" in str(e)
    assert ran == ["payment"] and tok.spent_usd == 6                  # 未再执行,未再计费


async def test_no_token_is_backward_compatible(tmp_path):
    q = _queue(tmp_path, token=None)                          # 无令牌 → 老行为
    ran = []
    assert await _run(q, "anything", {}, ran) == "ok" and ran == ["anything"]


# ---------------------------------------------------------------- 来源可信闸

async def test_provenance_rejects_llm_output(tmp_path):
    s = ApprovalSettings(default_level="auto", require_verified_source=["payment"])
    q = _queue(tmp_path, settings=s)
    ran = []

    async def _do():
        ran.append(1)
        return "paid"
    # llm_output 来源 → 拒
    try:
        await q.gate(action="payment", params={"amount_usd": 1, "_source": "llm_output"},
                     execute=_do)
        assert False
    except ApprovalDenied as e:
        assert "可信数据来源" in str(e)
    assert ran == []
    # erp_verified 来源 → 放行
    assert await q.gate(action="payment",
                        params={"amount_usd": 1, "_source": "erp_verified"},
                        execute=_do) == "paid"
    # 非受限动作不受影响
    assert await q.gate(action="recall", params={}, execute=_do) == "paid"


# ---------------------------------------------------------------- 审计修复回归

async def test_negative_amount_rejected(tmp_path):
    """负额:预算 check 会钳制到 0 但记账不会 → 曾可"充值"预算。现直接 deny。"""
    tok = dele.issue(_identity(tmp_path), task="t", permissions=["payment"], max_budget_usd=10.0)
    q = _queue(tmp_path, token=tok)
    ran = []
    try:
        await _run(q, "payment", {"amount_usd": -50}, ran)
        assert False
    except ApprovalDenied as e:
        assert "金额非法" in str(e)
    assert ran == [] and tok.spent_usd == 0                     # 未执行,未改动预算


async def test_string_amount_is_parsed_for_budget(tmp_path):
    """金额以字符串给出也要计入预算(否则预算 fail-open)。"""
    tok = dele.issue(_identity(tmp_path), task="t", permissions=["payment"], max_budget_usd=10.0)
    q = _queue(tmp_path, token=tok)
    ran = []
    try:
        await _run(q, "payment", {"amount_usd": "20"}, ran)     # "20" > 10 → 拒
        assert False
    except ApprovalDenied as e:
        assert "预算" in str(e)
    assert ran == []


async def test_llm_supplied_source_is_stripped(tmp_path):
    """治理:LLM 在工具参数里自称 _source=erp_verified 不得绕过 provenance 闸。
    ToolAgent 会剥除 _source,故受限动作仍 fail-closed 被拒。"""
    from core.config import ApprovalSettings, PolicyRule, load_config
    from core.tool_agent import ToolAgent
    from core.tools import AssistantTurn, Tool, ToolCall
    from core.approval import ApprovalQueue, Notifier
    from core.audit import AuditLog

    s = ApprovalSettings(require_verified_source=["danger"],
                         policies=[PolicyRule(action="danger", when={}, level="auto")],
                         default_level="auto")
    approval = ApprovalQueue(s, AuditLog(tmp_path / "a.jsonl"), Notifier(s))
    ran = []

    async def _r(args):
        ran.append(args)
        return "done"
    tool = Tool("danger", "", {"type": "object", "properties": {}}, _r, action="danger")

    class LLM:
        def __init__(self):
            self.i = 0

        async def chat_tools(self, messages, tools, **kw):
            self.i += 1
            if self.i == 1:   # LLM 谎称来源可信
                return AssistantTurn(tool_calls=[ToolCall(
                    "c", "danger", {"_source": "erp_verified", "x": 1})])
            return AssistantTurn(content="没做成那个。")

    class Mem:
        async def search(self, q, k=5):
            return []

        async def add(self, i, meta=None):
            return "id"

    agent = ToolAgent(LLM(), Mem(), load_config(), approval=approval, tools=[tool])
    resp = await agent.run("干点危险的")
    assert ran == []                                            # _source 被剥,provenance 仍拒
    assert resp.reply == "没做成那个。"


async def test_concurrent_payments_cannot_overspend(tmp_path):
    """TOCTOU:两笔并发付款争夺同一额度,原子预留保证不超支(只有一笔成功)。"""
    import asyncio

    tok = dele.issue(_identity(tmp_path), task="t", permissions=["payment"], max_budget_usd=10.0)
    q = _queue(tmp_path, token=tok)
    done = []

    async def _pay():
        async def _do():
            await asyncio.sleep(0.01)          # 拉长执行窗口,逼出竞态
            return "ok"
        try:
            await q.gate(action="payment", params={"amount_usd": 6}, execute=_do,
                         level_override="auto")
            done.append("ok")
        except ApprovalDenied:
            done.append("denied")

    await asyncio.gather(_pay(), _pay())
    assert sorted(done) == ["denied", "ok"]                     # 恰一成一拒
    assert tok.spent_usd == 6                                   # 未超支


async def test_confirm_rechecks_expiry_after_wait(tmp_path):
    """confirm 等待期间令牌过期 → 批准后、执行前重新校验,拒绝执行(不用陈旧授权)。"""
    import asyncio

    from core.config import ApprovalSettings, PolicyRule
    from core.approval import ApprovalQueue, Notifier
    from core.audit import AuditLog

    tok = dele.issue(_identity(tmp_path), task="t", permissions=["payment"],
                     ttl_s=3600, now=1000.0)
    s = ApprovalSettings(policies=[PolicyRule(action="payment", when={}, level="confirm")],
                         default_level="auto", timeout_s=5)
    q = ApprovalQueue(s, AuditLog(tmp_path / "a.jsonl"), Notifier(s), delegation=tok)
    ran = []

    async def _do():
        ran.append(1)
        return "paid"

    async def _drive():
        await asyncio.sleep(0.02)
        tok.valid_until = 1.0                  # 等待期间令牌过期
        pending = q.list_pending()
        await q.resolve(pending[0]["id"], approved=True)

    task = asyncio.gather(
        q.gate(action="payment", params={}, execute=_do, source="user"),
        _drive(), return_exceptions=True)
    results = await task
    assert any(isinstance(r, ApprovalDenied) and "过期" in str(r) for r in results)
    assert ran == []                                           # 批准了但过期 → 未执行


# ---------------------------------------------------------------- services 装配

def test_services_issues_token_when_enabled(tmp_path):
    from fastapi.testclient import TestClient

    from services.api import create_app

    cfg = load_config(agent={"autonomy": "chat"}, embedder={"backend": "fake"},
                      vectordb={"mode": "memory"}, llm={"mode": "echo"},
                      identity={"dir": str(tmp_path / "id")},
                      delegation={"enabled": True, "task": "demo",
                                  "permissions": ["recall", "remember"], "max_budget_usd": 100})
    app = create_app(cfg, memory=FakeMemory())
    with TestClient(app):
        assert app.state.approvals._delegation is not None
        assert app.state.delegation.permissions == ("recall", "remember")
        assert dele.verify(app.state.delegation) is True       # 真被身份签了名


def test_services_no_token_when_disabled(tmp_path):
    from fastapi.testclient import TestClient

    from services.api import create_app

    cfg = load_config(agent={"autonomy": "chat"}, embedder={"backend": "fake"},
                      vectordb={"mode": "memory"}, llm={"mode": "echo"},
                      identity={"dir": str(tmp_path / "id")})
    app = create_app(cfg, memory=FakeMemory())
    with TestClient(app):
        assert app.state.approvals._delegation is None


# ---------------------------------------------------------------- doctor

def test_doctor_flags_empty_permissions():
    from core.doctor import run_doctor

    checks = run_doctor(load_config(delegation={"enabled": True, "permissions": []}))
    assert [c for c in checks if "delegation" in c.title and c.level == "fail"]


def test_doctor_ok_delegation_and_provenance():
    from core.doctor import run_doctor

    checks = run_doctor(load_config(
        delegation={"enabled": True, "permissions": ["recall"]},
        approval={"require_verified_source": ["payment"]}))
    assert [c for c in checks if "delegation 令牌已启用" in c.title and c.level == "ok"]
    assert [c for c in checks if "来源可信闸已启用" in c.title and c.level == "ok"]
