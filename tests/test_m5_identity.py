"""Milestone 5 验收:Agent 身份 + A2A + 私有/共享记忆分区。"""

from __future__ import annotations

import uuid

import pytest
from fastapi.testclient import TestClient

from adapters.a2a import A2AClientAdapter, A2AServerAdapter, build_signed_card, verify_signed_card
from adapters.embedder import build_embedder
from adapters.memory import QdrantMemoryStore
from adapters.vectordb import QdrantAdapter
from core.identity import AgentIdentity, verify_envelope, verify_signature
from core.promotion import GraderPolicy, ManualPolicy
from core.schemas import MultimodalInput
from core.trust import TrustStore
from services.api import create_app
from tests.conftest import EchoMemoryLLM, ScriptedLLM, make_fake_config


def build_store_with_shared(cfg):
    embedder = build_embedder(cfg.embedder)
    db = QdrantAdapter(cfg.vectordb, dim=cfg.embedder.effective_dim)
    shared = QdrantAdapter(cfg.vectordb, dim=cfg.embedder.effective_dim,
                           collection=cfg.memory.shared_collection, share_client_from=db)
    return QdrantMemoryStore(embedder, ScriptedLLM(), db, cfg, shared_db=shared), db, shared


# ---------------------------------------------------------------- 5.1 身份

def test_identity_create_persist_and_key_perms(tmp_path):
    ident = AgentIdentity.load_or_create(tmp_path / "id")
    # UUID v7:版本位为 7
    assert uuid.UUID(ident.agent_id).version == 7
    # 私钥文件权限 600
    key_file = tmp_path / "id" / "agent_ed25519.key"
    assert key_file.exists()
    assert (key_file.stat().st_mode & 0o777) == 0o600
    # 重新加载 = 同一身份(agent_id 终身不变)
    again = AgentIdentity.load_or_create(tmp_path / "id")
    assert again.agent_id == ident.agent_id
    assert again.public_key_b64() == ident.public_key_b64()
    # profile 预留 payments 字段且为空(附录 A)
    assert again.profile["payments"] == []


def test_identity_rejects_loose_key_perms(tmp_path):
    import os

    ident = AgentIdentity.load_or_create(tmp_path / "id")
    os.chmod(tmp_path / "id" / "agent_ed25519.key", 0o644)
    with pytest.raises(Exception) as exc:
        AgentIdentity.load_or_create(tmp_path / "id")
    assert "权限" in str(exc.value)
    del ident


def test_sign_and_verify_roundtrip(tmp_path):
    ident = AgentIdentity.load_or_create(tmp_path / "id")
    payload = {"msg": "你好", "n": 42}
    env = ident.signed_envelope(payload)
    assert verify_envelope(env)
    # 篡改负载 → 验签失败
    env_bad = dict(env, payload={"msg": "被篡改", "n": 42})
    assert not verify_envelope(env_bad)
    # 错误公钥 → 验签失败
    other = AgentIdentity.load_or_create(tmp_path / "other")
    assert not verify_signature(payload, env["protected"], env["signature"],
                                other.public_key_b64())


def test_memory_namespace_bound_to_agent_id(tmp_path):
    ident = AgentIdentity.load_or_create(tmp_path / "id")
    assert ident.agent_id in ident.memory_namespace()


# ---------------------------------------------------------------- 5.2 A2A

def test_signed_agent_card_verify(tmp_path):
    ident = AgentIdentity.load_or_create(tmp_path / "id")
    signed = build_signed_card(ident, "http://localhost:8003")
    assert verify_signed_card(signed)
    assert set(signed["card"]["skills"]) == {"chat", "memory_search", "multimodal_recall"}
    assert signed["card"]["payments"] == []  # 附录 A 预留
    # 篡改 card → 验签失败
    tampered = {"card": dict(signed["card"], name="evil"), "signatures": signed["signatures"]}
    assert not verify_signed_card(tampered)


async def test_a2a_unknown_agent_requires_approval(tmp_path):
    cfg = make_fake_config(tmp_path)
    store, _, _ = build_store_with_shared(cfg)
    ident = AgentIdentity.load_or_create(tmp_path / "id")
    server = A2AServerAdapter(ident, store, TrustStore(store), "http://localhost:8003")

    resp = await server.handle_task("memory_search", {"query": "任何"}, from_agent_id="stranger-001")
    assert verify_envelope(resp)
    assert resp["payload"]["status"] == "approval_required"


async def test_a2a_whitelisted_agent_gets_shared_memories_only(tmp_path):
    cfg = make_fake_config(tmp_path)
    store, _, _ = build_store_with_shared(cfg)
    ident = AgentIdentity.load_or_create(tmp_path / "id")
    trust = TrustStore(store)
    server = A2AServerAdapter(ident, store, trust, "http://localhost:8003")

    await store.add(MultimodalInput.text("私有:用户的猫叫 Benjamin"), {})
    await store.add(MultimodalInput.text("共享:白色的猫是常见宠物"), {"visibility": "shared"})
    peer = AgentIdentity.load_or_create(tmp_path / "peer")
    await trust.trust(peer.agent_id)
    # 白名单是记忆:可检索审计
    assert await trust.is_trusted(peer.agent_id)
    audit = await trust.audit()
    assert any(e["agent_id"] == peer.agent_id for e in audit)

    resp = await server.handle_task("memory_search", {"query": "白色的猫"},
                                    from_agent_id=peer.agent_id)
    assert verify_envelope(resp)
    assert resp["payload"]["status"] == "ok"
    contents = [h["content"] for h in resp["payload"]["hits"]]
    assert any("共享" in c for c in contents)
    assert not any("私有" in c for c in contents), "私有记忆不得外泄给委托方"


def test_a2a_client_verifies_card_via_http(tmp_path):
    cfg = make_fake_config(tmp_path)
    store, _, _ = build_store_with_shared(cfg)
    app = create_app(cfg, llm=EchoMemoryLLM(), memory=store, skip_dependency_checks=True)
    with TestClient(app) as client:
        signed = client.get("/identity/card").json()
        assert verify_signed_card(signed)


# ---------------------------------------------------------------- 5.3 分区与上交

async def test_visibility_default_private_and_shared_pool(tmp_path):
    cfg = make_fake_config(tmp_path)
    store, db, shared = build_store_with_shared(cfg)
    await store.add(MultimodalInput.text("默认私有的记忆"), {})
    await store.add(MultimodalInput.text("显式共享的记忆"), {"visibility": "shared"})
    assert await db.count() == 1
    assert await shared.count() == 1
    # 检索合并两池
    hits = await store.search(MultimodalInput.text("记忆"), k=5)
    visibilities = {h.meta.get("visibility") for h in hits}
    assert visibilities == {"private", "shared"}


async def test_promote_copies_to_shared_pool(tmp_path):
    cfg = make_fake_config(tmp_path)
    store, _, shared = build_store_with_shared(cfg)
    mem_id = await store.add(MultimodalInput.text("值得共享的客观事实"), {})
    shared_id = await store.promote(mem_id)
    assert shared_id
    assert await shared.count() == 1
    points = await shared.get([shared_id])
    assert points[0]["payload"]["meta"]["promoted_from"] == mem_id


async def test_grader_policy(tmp_path):
    llm = ScriptedLLM(replies=['{"score": 0.9, "reason": "客观知识"}',
                               '{"score": 0.2, "reason": "私人琐事"}'])
    policy = GraderPolicy(llm, threshold=0.7)
    assert await policy.decide("巴黎是法国首都", {}) == "promote"
    assert await policy.decide("我今天午饭吃了面", {}) == "reject"


async def test_manual_policy_queues(tmp_path):
    policy = ManualPolicy()
    assert await policy.decide("任意内容", {"a": 1}) == "pending"
    assert policy.queue == [{"content": "任意内容", "meta": {"a": 1}}]


# ---------------------------------------------------------------- 5.2 SDK 全链路冒烟(离线)

async def test_a2a_sdk_end_to_end_smoke(tmp_path):
    """经 a2a-sdk 1.0.3 真实 JSONRPC 栈完成一次委托:
    well-known 卡片可取且含签名;白名单 agent 的 memory_search 委托返回可验签的
    共享记忆;未知 agent 得到 approval_required。"""
    import httpx
    pytest.importorskip("a2a")

    cfg = make_fake_config(tmp_path)
    store, _, _ = build_store_with_shared(cfg)
    ident = AgentIdentity.load_or_create(tmp_path / "id")
    trust = TrustStore(store)
    server = A2AServerAdapter(ident, store, trust, "http://localhost:8003")

    await store.add(MultimodalInput.text("共享:白色的猫是常见宠物"), {"visibility": "shared"})
    peer = AgentIdentity.load_or_create(tmp_path / "peer")
    await trust.trust(peer.agent_id)

    app = server.build_app()
    transport = httpx.ASGITransport(app=app)

    # Agent Card(A2A well-known 路径)
    async with httpx.AsyncClient(transport=transport, base_url="http://a2a") as client:
        card_resp = await client.get("/.well-known/agent-card.json")
        assert card_resp.status_code == 200
        card = card_resp.json()
        assert card["name"] == "memory-agent"
        assert card["signatures"], "well-known 卡片必须携带签名"

    # 客户端适配器发起委托(白名单 peer)→ ok + 只含共享记忆 + 响应可验签
    peer_client = A2AClientAdapter(peer)
    envelope = await peer_client.delegate(
        "http://a2a/", "memory_search", {"query": "白色的猫"}, transport=transport)
    assert verify_envelope(envelope)
    assert envelope["payload"]["status"] == "ok"
    assert envelope["payload"]["hits"], "共享记忆应命中"
    assert all(h["meta"].get("visibility") == "shared" for h in envelope["payload"]["hits"])

    # 未知 agent → approval_required
    stranger = AgentIdentity.load_or_create(tmp_path / "stranger")
    stranger_client = A2AClientAdapter(stranger)
    envelope = await stranger_client.delegate(
        "http://a2a/", "memory_search", {"query": "白色的猫"}, transport=transport)
    assert envelope["payload"]["status"] == "approval_required"


def test_identity_rejects_swapped_private_key(tmp_path):
    """identity.json 公钥与私钥不匹配(密钥被替换)必须拒绝加载。"""
    from cryptography.hazmat.primitives import serialization
    from cryptography.hazmat.primitives.asymmetric.ed25519 import Ed25519PrivateKey

    AgentIdentity.load_or_create(tmp_path / "id")
    rogue = Ed25519PrivateKey.generate().private_bytes(
        encoding=serialization.Encoding.PEM,
        format=serialization.PrivateFormat.PKCS8,
        encryption_algorithm=serialization.NoEncryption(),
    )
    (tmp_path / "id" / "agent_ed25519.key").write_bytes(rogue)
    with pytest.raises(Exception) as exc:
        AgentIdentity.load_or_create(tmp_path / "id")
    assert "不匹配" in str(exc.value)


async def test_promote_via_api(tmp_path):
    """/memory/promote 端点:私有记忆经 API 上交共享池。"""
    cfg = make_fake_config(tmp_path)
    store, _, shared = build_store_with_shared(cfg)
    app = create_app(cfg, llm=EchoMemoryLLM(), memory=store, skip_dependency_checks=True)
    with TestClient(app) as client:
        add = client.post("/memory/add", json={
            "input": {"type": "text", "content": "值得共享的客观事实"}, "meta": {}})
        mem_id = add.json()["ids"][0]
        resp = client.post("/memory/promote", json={"memory_id": mem_id})
        assert resp.status_code == 200 and resp.json()["shared_id"]
    assert await shared.count() == 1
