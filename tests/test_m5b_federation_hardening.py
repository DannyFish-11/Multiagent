"""外层联邦/信任/令牌硬化验收(对外审计 #22/#24/#25/#26)。

- #22 入站 A2A 签名意图路由点亮 + 冒名防护(未签名请求声称可信 agent_id 不得通过)
- #24 信任白名单在无 dump_all 后端上的**精确** targeted 查找(不因相似度沾边误信任)
- #25 委派令牌**签发者绑定**(verify_issued_by pin 到已知身份,拒绝自证/错发者)
- #26 审批闸/审计 source 默认改 "unknown"(忘传来源不再被静默标成特权 "user")
"""

from __future__ import annotations

from fastapi.testclient import TestClient

from adapters.a2a import A2AClientAdapter
from core.audit import AuditLog
from core.config import ApprovalSettings, PolicyRule
from core.approval import ApprovalQueue, Notifier
from core.delegation import issue, verify, verify_issued_by
from core.identity import AgentIdentity, verify_envelope
from core.schemas import MemoryHit, MultimodalInput
from core.trust import TrustStore
from services.api import create_app
from tests.conftest import EchoMemoryLLM, make_fake_config
from tests.test_m5_identity import build_store_with_shared


async def _ok():
    return {"ok": True}


# ---------------------------------------------------------------- #22 入站 A2A 路由

async def _seed(store):
    await store.add(MultimodalInput.text("共享:白色的猫是常见宠物"), {"visibility": "shared"})
    await store.add(MultimodalInput.text("私有:用户的猫叫 Benjamin"), {})


async def test_a2a_inbound_trusted_gets_shared_only(tmp_path):
    cfg = make_fake_config(tmp_path)
    store, _, _ = build_store_with_shared(cfg)
    await _seed(store)
    peer = AgentIdentity.load_or_create(tmp_path / "peer")
    await TrustStore(store).trust(peer.agent_id)
    app = create_app(cfg, llm=EchoMemoryLLM(), memory=store, skip_dependency_checks=True)
    with TestClient(app) as client:
        env = A2AClientAdapter(peer).build_delegation("memory_search", {"query": "白色的猫"})
        r = client.post("/a2a/tasks", json=env)
        assert r.status_code == 200
        body = r.json()
        assert verify_envelope(body) and body["payload"]["status"] == "ok"
        contents = [h["content"] for h in body["payload"]["hits"]]
        assert any("共享" in c for c in contents)
        assert not any("私有" in c for c in contents)   # 私有绝不外泄


async def test_a2a_inbound_untrusted_needs_approval(tmp_path):
    cfg = make_fake_config(tmp_path)
    store, _, _ = build_store_with_shared(cfg)
    await _seed(store)
    stranger = AgentIdentity.load_or_create(tmp_path / "stranger")   # 未加白
    app = create_app(cfg, llm=EchoMemoryLLM(), memory=store, skip_dependency_checks=True)
    with TestClient(app) as client:
        env = A2AClientAdapter(stranger).build_delegation("memory_search", {"query": "x"})
        body = client.post("/a2a/tasks", json=env).json()
        assert body["payload"]["status"] == "approval_required"


async def test_a2a_inbound_unsigned_spoof_rejected(tmp_path):
    """冒名防护:未签名请求即便声称一个**可信** agent_id,也不得被认证放行。"""
    cfg = make_fake_config(tmp_path)
    store, _, _ = build_store_with_shared(cfg)
    await _seed(store)
    peer = AgentIdentity.load_or_create(tmp_path / "peer")
    await TrustStore(store).trust(peer.agent_id)         # peer 是可信的
    app = create_app(cfg, llm=EchoMemoryLLM(), memory=store, skip_dependency_checks=True)
    with TestClient(app) as client:
        # 裸 payload,无 signature/identity,却声称自己是可信的 peer
        spoof = {"skill": "memory_search", "params": {"query": "白色的猫"},
                 "from_agent_id": peer.agent_id}
        body = client.post("/a2a/tasks", json=spoof).json()
        assert body["payload"]["status"] == "approval_required"   # 明文声称无效


# ---------------------------------------------------------------- #24 信任精确查找

class _FakeMemNoDumpAll:
    """无 dump_all 的最简后端:search 返回全部条目(模拟召回),验精确匹配逻辑。"""
    def __init__(self):
        self._items: list[tuple[str, str, dict]] = []

    async def add(self, inp, meta=None):
        mid = f"m{len(self._items)}"
        self._items.append((mid, inp.content, meta or {}))
        return mid

    async def search(self, query, k=5):
        return [MemoryHit(id=i, score=1.0, content=c, meta=m)
                for i, c, m in self._items][:k]


async def test_trust_precise_on_backend_without_dump_all():
    mem = _FakeMemNoDumpAll()
    assert not hasattr(mem, "dump_all")
    ts = TrustStore(mem)
    await ts.trust("agent-A")
    assert await ts.is_trusted("agent-A") is True
    assert await ts.is_trusted("agent-B") is False   # 精确等值:不因召回/相似沾边误信任
    assert await ts.is_trusted("") is False


# ---------------------------------------------------------------- #25 令牌签发者绑定

def test_delegation_issuer_binding(tmp_path):
    alice = AgentIdentity.load_or_create(tmp_path / "alice")
    bob = AgentIdentity.load_or_create(tmp_path / "bob")
    tok = issue(alice, task="t", permissions=["*"], max_budget_usd=10.0)
    assert verify(tok) is True                         # 裸 verify 用令牌自带公钥 → 自证通过(弱点)
    assert verify_issued_by(tok, alice) is True        # pin 到真实签发者 alice → 通过
    assert verify_issued_by(tok, bob) is False         # pin 到 bob → 拒(非其签发)


def test_delegation_forged_issuer_field_rejected(tmp_path):
    """攻击者用自己的密钥签令牌、把 issuer 字段写成 alice:裸 verify 自证通过,
    verify_issued_by(alice) 因公钥不匹配而拒绝。"""
    alice = AgentIdentity.load_or_create(tmp_path / "alice")
    bob = AgentIdentity.load_or_create(tmp_path / "bob")
    forged = issue(bob, task="t", permissions=["*"], max_budget_usd=10.0,
                   issuer=alice.agent_id)              # bob 签,却声称 issuer=alice
    assert verify(forged) is True                      # 自证(用 bob 自带公钥)
    assert verify_issued_by(forged, alice) is False    # pin alice:公钥是 bob 的 → 拒


# ---------------------------------------------------------------- #26 source 诚实默认

async def test_gate_source_defaults_to_unknown(tmp_path):
    s = ApprovalSettings(audit_path=str(tmp_path / "a.jsonl"), default_level="auto",
                         policies=[PolicyRule(action="noop", when={}, level="auto")])
    audit = AuditLog(s.audit_path)
    q = ApprovalQueue(s, audit, Notifier(s))
    await q.gate(action="noop", params={}, execute=_ok)      # 不传 source
    assert audit.read_all()[-1]["source"] == "unknown"       # 非 "user"


async def test_audit_source_defaults_to_unknown(tmp_path):
    audit = AuditLog(tmp_path / "a.jsonl")
    await audit.record(action="x", level="auto", decision="executed")   # 不传 source
    assert audit.read_all()[-1]["source"] == "unknown"
