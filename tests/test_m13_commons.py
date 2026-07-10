"""PHASE4 M13 验收:群体资产库(统一信封 + 三级准入 + 采用/传播 + 观测)。"""

from __future__ import annotations

import asyncio
import base64
import io
import tarfile

import pytest

from core.approval import ApprovalQueue, Notifier
from core.audit import AuditLog
from core.commons import (
    CommonsStore,
    GraderAdmission,
    HumanAdmission,
    SandboxAdmission,
    build_envelope,
    read_skill_tar,
    verify_envelope_integrity,
)
from core.commons_metrics import CommonsMetrics
from core.config import ApprovalSettings, PolicyRule
from core.errors import LayerError
from core.identity import AgentIdentity
from tests.conftest import ScriptedLLM


def make_skill_tar(skill_md: str = "# SKILL\n何时用:测试。怎么用:调用。") -> str:
    buf = io.BytesIO()
    with tarfile.open(fileobj=buf, mode="w") as tar:
        data = skill_md.encode()
        info = tarfile.TarInfo("myskill/SKILL.md")
        info.size = len(data)
        tar.addfile(info, io.BytesIO(data))
    return base64.b64encode(buf.getvalue()).decode()


def grader_llm(accept=True):
    score = 0.9 if accept else 0.1
    return ScriptedLLM(replies=[f'{{"score": {score}, "reason": "r"}}'] * 20)


def make_store(tmp_path, policies, report_threshold=3):
    metrics = CommonsMetrics(tmp_path / "commons.json")
    audit = AuditLog(tmp_path / "audit.jsonl")
    return CommonsStore(metrics, audit, policies, report_threshold=report_threshold), audit


def approve_queue(tmp_path, auto=False):
    s = ApprovalSettings(audit_path=str(tmp_path / "aq.jsonl"),
                         default_level="auto" if auto else "confirm", timeout_s=2.0)
    return ApprovalQueue(s, AuditLog(s.audit_path), Notifier(s))


# ---------------------------------------------------------------- ① 信封与三类发布

async def test_publish_three_cargo_types(tmp_path):
    ident = AgentIdentity.load_or_create(tmp_path / "author")
    queue = approve_queue(tmp_path, auto=True)  # 便于 mcp 直通(仅本用例)

    async def pilot(env, payload):
        return [{"decision": "executed"}] * 3

    store, audit = make_store(tmp_path, {
        "memory": GraderAdmission(grader_llm(True)),
        "skill": SandboxAdmission(grader_llm(True), pilot, min_runs=3),
        "mcp_entry": HumanAdmission(queue),
    })

    mem = {"content": "巴黎是法国首都", "meta": {}}
    env_m = build_envelope(ident, "memory", mem)
    assert verify_envelope_integrity(env_m, mem)
    r = await store.publish(env_m, mem)
    assert r.status == "active" and r.review.policy == "grader"

    skill = make_skill_tar()
    env_s = build_envelope(ident, "skill", skill)
    r = await store.publish(env_s, skill)
    assert r.status == "active" and r.review.policy == "sandbox"

    mcp = {"name": "weather", "server_url": "http://x", "capabilities": "查天气",
           "credentials_note": "需 API key(不含本体)", "risk": "低"}
    env_c = build_envelope(ident, "mcp_entry", mcp)
    r = await store.publish(env_c, mcp)
    assert r.review.policy == "human"


async def test_bad_signature_or_hash_rejected(tmp_path):
    ident = AgentIdentity.load_or_create(tmp_path / "author")
    store, _ = make_store(tmp_path, {"memory": GraderAdmission(grader_llm(True))})
    mem = {"content": "x"}
    env = build_envelope(ident, "memory", mem)
    # 篡改 payload(hash 不符)
    with pytest.raises(LayerError):
        await store.publish(env, {"content": "被篡改"})
    # 篡改签名
    env2 = build_envelope(ident, "memory", mem)
    env2.signature = "AAAA"
    with pytest.raises(LayerError):
        await store.publish(env2, mem)


def test_skill_tar_requires_skill_md(tmp_path):
    buf = io.BytesIO()
    with tarfile.open(fileobj=buf, mode="w") as tar:
        data = b"x"
        info = tarfile.TarInfo("other.txt")
        info.size = 1
        tar.addfile(info, io.BytesIO(data))
    with pytest.raises(LayerError) as exc:
        read_skill_tar(base64.b64encode(buf.getvalue()).decode())
    assert "SKILL.md" in str(exc.value)


# ---------------------------------------------------------------- ② 三级准入(含负向)

async def test_memory_grader_rejects_low_score(tmp_path):
    ident = AgentIdentity.load_or_create(tmp_path / "a")
    store, _ = make_store(tmp_path, {"memory": GraderAdmission(grader_llm(False))})
    mem = {"content": "琐事"}
    env = build_envelope(ident, "memory", mem)
    r = await store.publish(env, mem)
    assert r.status == "deprecated" and r.review.verdict == "rejected"


async def test_skill_sandbox_requires_pilot_runs(tmp_path):
    ident = AgentIdentity.load_or_create(tmp_path / "a")

    async def pilot_too_few(env, payload):
        return [{"decision": "executed"}]  # 仅 1 次 < 3

    store, _ = make_store(tmp_path, {
        "skill": SandboxAdmission(grader_llm(True), pilot_too_few, min_runs=3)})
    skill = make_skill_tar()
    env = build_envelope(ident, "skill", skill)
    r = await store.publish(env, skill)
    assert r.review.verdict == "rejected" and "试点" in r.review.notes


async def test_skill_sandbox_rejects_anomaly(tmp_path):
    ident = AgentIdentity.load_or_create(tmp_path / "a")

    async def pilot_anomaly(env, payload):
        return [{"decision": "executed"}, {"decision": "denied"}, {"decision": "executed"}]

    store, _ = make_store(tmp_path, {
        "skill": SandboxAdmission(grader_llm(True), pilot_anomaly, min_runs=3)})
    skill = make_skill_tar()
    env = build_envelope(ident, "skill", skill)
    r = await store.publish(env, skill)
    assert r.review.verdict == "rejected" and "异常" in r.review.notes


async def test_mcp_entry_cannot_bypass_human(tmp_path):
    """硬负向①:任何路径(grader 满分、多数联名)都不能使 mcp_entry 绕过人工批准。"""
    ident = AgentIdentity.load_or_create(tmp_path / "a")
    # 即便 policy 表把 commons_publish_mcp_entry 配成 auto,HumanAdmission 用
    # level_override=confirm 强制人工,超时(无人批)→ 拒绝
    s = ApprovalSettings(audit_path=str(tmp_path / "aq.jsonl"), default_level="auto",
                         timeout_s=0.2,
                         policies=[PolicyRule(action="commons_publish_mcp_entry", when={},
                                              level="auto")])  # 恶意/误配为 auto
    queue = ApprovalQueue(s, AuditLog(s.audit_path), Notifier(s))
    store, _ = make_store(tmp_path, {"mcp_entry": HumanAdmission(queue)})
    mcp = {"name": "evil", "server_url": "http://evil"}
    env = build_envelope(ident, "mcp_entry", mcp)
    r = await store.publish(env, mcp)
    assert r.review.verdict == "rejected"  # 无人批准 → 超时拒绝,未入池
    assert store.browse("mcp_entry") == []


async def test_mcp_entry_admitted_only_with_human_approval(tmp_path):
    ident = AgentIdentity.load_or_create(tmp_path / "a")
    queue = approve_queue(tmp_path, auto=False)
    store, _ = make_store(tmp_path, {"mcp_entry": HumanAdmission(queue)})
    mcp = {"name": "ok", "server_url": "http://ok"}
    env = build_envelope(ident, "mcp_entry", mcp)

    async def publish():
        return await store.publish(env, mcp)

    task = asyncio.create_task(publish())
    await asyncio.sleep(0.05)
    pend = queue.list_pending()
    assert len(pend) == 1  # 人工待批
    await queue.resolve(pend[0]["id"], approved=True)
    r = await task
    assert r.status == "active" and r.review.verdict == "approved"


# ---------------------------------------------------------------- ② 负向:未过沙箱不得采用

async def test_skill_not_adoptable_before_sandbox(tmp_path):
    """硬负向②:未过沙箱期的 skill 不得被非试点实例采用。"""
    ident = AgentIdentity.load_or_create(tmp_path / "a")

    async def pilot_fail(env, payload):
        return []  # 沙箱未通过

    store, _ = make_store(tmp_path, {
        "skill": SandboxAdmission(grader_llm(True), pilot_fail, min_runs=3)})
    skill = make_skill_tar()
    env = build_envelope(ident, "skill", skill)
    published = await store.publish(env, skill)
    assert published.status != "active"
    # 非试点实例采用应失败(非 active)
    with pytest.raises(LayerError):
        await store.adopt(published.entry_id, "instance-B")


# ---------------------------------------------------------------- ③ 采用 + ActionMemory/审计

async def test_adopt_records_audit(tmp_path):
    ident = AgentIdentity.load_or_create(tmp_path / "a")

    async def pilot(env, payload):
        return [{"decision": "executed"}] * 3

    store, audit = make_store(tmp_path, {
        "skill": SandboxAdmission(grader_llm(True), pilot, min_runs=3)})
    skill = make_skill_tar()
    env = build_envelope(ident, "skill", skill)
    r = await store.publish(env, skill)
    adopted = await store.adopt(r.entry_id, "instance-B")
    assert adopted["type"] == "skill"
    # B 的采用出现在审计
    assert any(e["action"] == "commons_adopt" and e["agent_id"] == "instance-B"
               for e in audit.read_all())


# ---------------------------------------------------------------- ④ revoke 传播

async def test_revoke_propagates_to_adopters(tmp_path):
    """硬负向③:revoke 一条被两实例采用的 mcp_entry,两实例挂载须移除。"""
    ident = AgentIdentity.load_or_create(tmp_path / "a")
    queue = approve_queue(tmp_path, auto=False)
    store, _ = make_store(tmp_path, {"mcp_entry": HumanAdmission(queue)})
    mcp = {"name": "svc", "server_url": "http://svc"}
    env = build_envelope(ident, "mcp_entry", mcp)

    pub = asyncio.create_task(store.publish(env, mcp))
    await asyncio.sleep(0.05)
    await queue.resolve(queue.list_pending()[0]["id"], approved=True)
    r = await pub

    async def local_confirm(env_, payload_):
        return True  # 采用侧本地 confirm 通过(双闸另有专测)

    await store.adopt(r.entry_id, "inst-A", local_confirm=local_confirm)
    await store.adopt(r.entry_id, "inst-B", local_confirm=local_confirm)

    notifications = await store.revoke(r.entry_id, by="human", is_human=True)
    assert {n["agent_id"] for n in notifications} == {"inst-A", "inst-B"}
    assert all(n["action"] == "unmount_immediately" for n in notifications)
    # 撤销后不可见、不可再采用
    assert store.browse("mcp_entry") == []
    with pytest.raises(LayerError):
        await store.adopt(r.entry_id, "inst-C", local_confirm=local_confirm)


async def test_mcp_adopt_double_gate(tmp_path):
    """mcp_entry 采用须本地再 confirm(双闸):缺本地闸门则拒绝采用。"""
    ident = AgentIdentity.load_or_create(tmp_path / "a")
    queue = approve_queue(tmp_path, auto=False)
    store, _ = make_store(tmp_path, {"mcp_entry": HumanAdmission(queue)})
    mcp = {"name": "svc", "server_url": "http://svc"}
    env = build_envelope(ident, "mcp_entry", mcp)
    pub = asyncio.create_task(store.publish(env, mcp))
    await asyncio.sleep(0.05)
    await queue.resolve(queue.list_pending()[0]["id"], approved=True)
    r = await pub
    with pytest.raises(LayerError) as exc:
        await store.adopt(r.entry_id, "inst-A")  # 未提供 local_confirm
    assert "双闸" in str(exc.value)


# ---------------------------------------------------------------- report 降级

async def test_report_threshold_demotes(tmp_path):
    ident = AgentIdentity.load_or_create(tmp_path / "a")
    store, _ = make_store(tmp_path, {"memory": GraderAdmission(grader_llm(True))},
                          report_threshold=3)
    mem = {"content": "可疑信息"}
    env = build_envelope(ident, "memory", mem)
    r = await store.publish(env, mem)
    for i in range(3):
        res = await store.report(r.entry_id, f"reporter-{i}", "错误")
    assert res["demoted"] is True
    assert store.browse("memory") == []  # 降级后不可见


async def test_widely_adopted_entry_resists_report_brigading(tmp_path):
    """采用感知降级:被多实例采用的条目不因少量举报(≤采用者数)被打压下架。"""
    ident = AgentIdentity.load_or_create(tmp_path / "a")
    store, _ = make_store(tmp_path, {"memory": GraderAdmission(grader_llm(True))},
                          report_threshold=3)
    mem = {"content": "被广泛采用的有用记忆"}
    env = build_envelope(ident, "memory", mem)
    r = await store.publish(env, mem)
    # 5 个真实实例采用
    for i in range(5):
        await store.adopt(r.entry_id, f"adopter-{i}")
    # 单一实例连报 3 次达阈值,但 reports(3) 不超过采用者(5) → 不降级
    for _ in range(3):
        res = await store.report(r.entry_id, "attacker", "恶意举报")
    assert res["demoted"] is False
    assert len(store.browse("memory")) == 1  # 仍在池中可见
    # 举报累计超过采用者数(6 > 5)后才降级
    for _ in range(3):
        res = await store.report(r.entry_id, "attacker", "恶意举报")
    assert res["demoted"] is True
    assert store.browse("memory") == []


# ---------------------------------------------------------------- ⑤ metrics 端点

async def test_metrics_report_and_purification_primitive(tmp_path):
    ident = AgentIdentity.load_or_create(tmp_path / "a")
    store, _ = make_store(tmp_path, {"memory": GraderAdmission(grader_llm(True))})
    for i in range(3):
        mem = {"content": f"事实{i}"}
        env = build_envelope(ident, "memory", mem)
        pub = await store.publish(env, mem)
        if i == 0:
            await store.adopt(pub.entry_id, "b1")
            await store.adopt(pub.entry_id, "b2")
    report = store.metrics_report()
    assert report["by_type"]["memory"]["total"] == 3
    assert "pass_rate" in report["rates"]["memory"]
    # 采用曲线原语:有 entry 的 references/adoptions
    top = max(report["entries"], key=lambda e: e["references"])
    assert top["references"] == 2
