"""群体资产库 Commons(PHASE4 M13):统一信封 + 三种货物 + 三级准入。

三类资产:memory(经验)/ skill(程序性知识,tar+SKILL.md)/ mcp_entry(工具地址与说明)。
一个统一信封 CommonsEnvelope,三级差异化准入(危险性递增):
  memory    → GraderPolicy(LLM 评分)
  skill     → SandboxPolicy(grader 初筛 → 试点隔离运行 ≥N → 审计复核 → 入池)
  mcp_entry → HumanPolicy(永远人工批准,ApprovalQueue confirm;采用时本地再 confirm 双闸)

红线:统一信封,复用既有货物格式,不自造 schema。stats 由服务端维护,不信客户端上报。
采用是拉取制:资产库从不推送。
"""

from __future__ import annotations

import base64
import hashlib
import io
import json
import tarfile
import time
from typing import TYPE_CHECKING, Any, Literal

from pydantic import BaseModel, Field

from core.audit import AuditLog
from core.commons_metrics import CommonsMetrics
from core.errors import LayerError
from core.identity import AgentIdentity, uuid7, verify_signature

if TYPE_CHECKING:
    from core.approval import ApprovalQueue

CargoType = Literal["memory", "skill", "mcp_entry"]
Status = Literal["active", "deprecated", "revoked"]


def canonical_hash(payload: Any) -> str:
    blob = json.dumps(payload, sort_keys=True, ensure_ascii=False, separators=(",", ":"),
                      default=str).encode("utf-8")
    return hashlib.sha256(blob).hexdigest()


class ReviewRecord(BaseModel):
    policy: Literal["grader", "sandbox", "human"]
    verdict: Literal["pending", "approved", "rejected"] = "pending"
    reviewer: str = ""          # agent_id | "human"
    notes: str = ""
    reviewed_at: float | None = None


class EntryStats(BaseModel):
    adoptions: int = 0
    references: int = 0
    reports: int = 0


class CommonsEnvelope(BaseModel):
    entry_id: str = Field(default_factory=uuid7)
    type: CargoType
    version: str = "1.0.0"
    author: str                 # agent_id
    signature: str = ""         # 作者对 content_hash 的 Ed25519 签名(分离签名 protected/signature)
    protected: str = ""
    author_public_key: str = ""
    content_hash: str = ""
    created_at: float = Field(default_factory=time.time)
    updated_at: float = Field(default_factory=time.time)
    review: ReviewRecord
    stats: EntryStats = Field(default_factory=EntryStats)
    status: Status = "active"


def build_envelope(identity: AgentIdentity, cargo_type: CargoType, payload: Any,
                   version: str = "1.0.0") -> CommonsEnvelope:
    """作者侧构造签名信封(对 payload 内容哈希签名)。"""
    ch = canonical_hash(payload)
    sig = identity.sign({"content_hash": ch})
    policy = {"memory": "grader", "skill": "sandbox", "mcp_entry": "human"}[cargo_type]
    return CommonsEnvelope(
        type=cargo_type, author=identity.agent_id, version=version,
        signature=sig["signature"], protected=sig["protected"],
        author_public_key=identity.public_key_b64(), content_hash=ch,
        review=ReviewRecord(policy=policy),  # type: ignore[arg-type]
    )


def verify_envelope_integrity(env: CommonsEnvelope, payload: Any) -> bool:
    """入池前校验:content_hash 与 payload 一致,且作者签名有效。"""
    if canonical_hash(payload) != env.content_hash:
        return False
    return verify_signature({"content_hash": env.content_hash}, env.protected,
                            env.signature, env.author_public_key)


# ---------------------------------------------------------------- skill 货物

def read_skill_tar(payload_b64: str) -> dict[str, bytes]:
    """解包 skill tar,返回 {文件名: 内容};必须含 SKILL.md(业界惯例,不自造 schema)。"""
    raw = base64.b64decode(payload_b64)
    files: dict[str, bytes] = {}
    with tarfile.open(fileobj=io.BytesIO(raw)) as tar:
        for m in tar.getmembers():
            if m.isfile():
                f = tar.extractfile(m)
                if f:
                    files[m.name.split("/")[-1]] = f.read()
    if not any(name == "SKILL.md" for name in files):
        raise LayerError("L13", "commons-skill", "skill tar 必须包含 SKILL.md")
    return files


# ---------------------------------------------------------------- 三级准入政策

class AdmissionContext(BaseModel):
    model_config = {"arbitrary_types_allowed": True}
    envelope: CommonsEnvelope
    payload: Any


class GraderAdmission:
    """memory:LLM 评分 ≥ 阈值即入池。"""

    def __init__(self, llm, threshold: float = 0.7) -> None:
        self._llm = llm
        self._threshold = threshold

    async def review(self, ctx: AdmissionContext) -> ReviewRecord:
        from core.promotion import GraderPolicy

        policy = GraderPolicy(self._llm, self._threshold)
        content = json.dumps(ctx.payload, ensure_ascii=False)[:2000]
        decision = await policy.decide(content, {})
        return ReviewRecord(
            policy="grader", verdict="approved" if decision == "promote" else "rejected",
            reviewer="grader", notes=getattr(policy, "last_reason", ""), reviewed_at=time.time())


class SandboxAdmission:
    """skill:grader 初筛 → 试点实例隔离采用并运行 ≥N 次 → 审计复核无异常 → 入池。

    run_pilot 回调由装配层注入(在隔离环境跑试点,返回运行审计条目)。
    未过沙箱期的 skill status 保持 pending,browse 不可见、非试点不得采用。
    """

    def __init__(self, llm, run_pilot, min_runs: int = 3, threshold: float = 0.7) -> None:
        self._grader = GraderAdmission(llm, threshold)
        self._run_pilot = run_pilot   # async (envelope, payload) -> list[audit_entry]
        self._min_runs = min_runs

    async def review(self, ctx: AdmissionContext) -> ReviewRecord:
        pre = await self._grader.review(ctx)
        if pre.verdict != "approved":
            return ReviewRecord(policy="sandbox", verdict="rejected",
                                reviewer="grader", notes="初筛未过", reviewed_at=time.time())
        runs = await self._run_pilot(ctx.envelope, ctx.payload)
        if len(runs) < self._min_runs:
            return ReviewRecord(policy="sandbox", verdict="rejected", reviewer="sandbox",
                                notes=f"试点运行 {len(runs)} < {self._min_runs}", reviewed_at=time.time())
        # 审计复核:任一运行异常即拒
        anomalies = [r for r in runs if r.get("decision") not in ("executed", "approved")]
        if anomalies:
            return ReviewRecord(policy="sandbox", verdict="rejected", reviewer="sandbox",
                                notes=f"{len(anomalies)} 次运行异常", reviewed_at=time.time())
        return ReviewRecord(policy="sandbox", verdict="approved", reviewer="sandbox",
                            notes=f"试点 {len(runs)} 次无异常", reviewed_at=time.time())


class HumanAdmission:
    """mcp_entry:永远人工批准,无自动通道(走 ApprovalQueue confirm)。

    grader 打满分、多数实例联名都不能绕过(硬负向测试覆盖)。
    """

    def __init__(self, queue: "ApprovalQueue") -> None:
        self._queue = queue

    async def review(self, ctx: AdmissionContext) -> ReviewRecord:
        from core.approval import ApprovalDenied, ApprovalTimeout

        async def _admit():
            return "admitted"

        try:
            # level_override=confirm:强制人工,无论 policy 表如何配置
            await self._queue.gate(
                action="commons_publish_mcp_entry",
                params={"entry_id": ctx.envelope.entry_id, "name": _mcp_name(ctx.payload)},
                source="commons", execute=_admit, level_override="confirm")
        except (ApprovalDenied, ApprovalTimeout) as exc:
            return ReviewRecord(policy="human", verdict="rejected", reviewer="human",
                                notes=str(exc), reviewed_at=time.time())
        return ReviewRecord(policy="human", verdict="approved", reviewer="human",
                            reviewed_at=time.time())


def _mcp_name(payload: Any) -> str:
    return payload.get("name", "") if isinstance(payload, dict) else ""


# ---------------------------------------------------------------- 资产库 + 采用/传播

class CommonsStore:
    """群体资产库服务端:统一信封入池、三级准入、采用登记、举报降级、撤销传播。

    stats 服务端维护(不信客户端上报)。采用为拉取制。
    """

    def __init__(self, metrics: CommonsMetrics, audit: AuditLog,
                 policies: dict[str, Any], report_threshold: int = 3) -> None:
        self._metrics = metrics
        self._audit = audit
        self._policies = policies       # {"memory": GraderAdmission, "skill": Sandbox, "mcp_entry": Human}
        self._report_threshold = report_threshold
        self._entries: dict[str, dict] = {}      # entry_id -> {envelope, payload}
        self._adoptions: dict[str, set[str]] = {}  # entry_id -> {agent_id}

    async def publish(self, env: CommonsEnvelope, payload: Any) -> CommonsEnvelope:
        # 入池前校验:验签失败 / hash 不符一律拒收
        if not verify_envelope_integrity(env, payload):
            await self._audit.record(action="commons_publish", level="deny", decision="denied",
                                     agent_id=env.author, params={"type": env.type},
                                     extra={"reason": "验签或 hash 校验失败"})
            raise LayerError("L13", "commons", "信封验签或 content_hash 校验失败,拒收")
        if env.type == "skill":
            read_skill_tar(payload)  # 结构校验(须含 SKILL.md)

        policy = self._policies.get(env.type)
        if policy is None:
            raise LayerError("L13", "commons", f"未配置 {env.type} 的准入政策")
        review = await policy.review(AdmissionContext(envelope=env, payload=payload))
        env.review = review
        env.status = "active" if review.verdict == "approved" else "deprecated"
        env.updated_at = time.time()
        self._entries[env.entry_id] = {"envelope": env, "payload": payload}
        self._metrics.register(env.entry_id)
        await self._audit.record(
            action=f"commons_publish_{env.type}", level="auto",
            decision=review.verdict, agent_id=env.author,
            params={"entry_id": env.entry_id, "policy": review.policy}, extra={"notes": review.notes})
        return env

    def browse(self, cargo_type: str | None = None) -> list[CommonsEnvelope]:
        """浏览可采用资产:仅 status=active 可见(revoked/deprecated 不可见)。"""
        out = []
        for rec in self._entries.values():
            env = rec["envelope"]
            if env.status != "active":
                continue
            if cargo_type and env.type != cargo_type:
                continue
            out.append(env)
        return out

    def get(self, entry_id: str) -> dict | None:
        return self._entries.get(entry_id)

    async def adopt(self, entry_id: str, agent_id: str,
                    local_confirm=None) -> dict:
        """采用(拉取制)。skill 须已过沙箱(active);mcp_entry 采用时本地再 confirm(双闸)。"""
        rec = self._entries.get(entry_id)
        if rec is None or rec["envelope"].status != "active":
            raise LayerError("L13", "commons", f"条目不可采用(不存在或非 active): {entry_id}")
        env = rec["envelope"]

        # mcp_entry 双闸:采用时本地再走一次 confirm
        if env.type == "mcp_entry":
            if local_confirm is None:
                raise LayerError("L13", "commons", "mcp_entry 采用须提供本地 confirm 闸门(双闸)")
            await local_confirm(env, rec["payload"])

        self._adoptions.setdefault(entry_id, set()).add(agent_id)
        self._metrics.cite(entry_id, agent_id)
        env.stats.adoptions = len(self._adoptions[entry_id])
        await self._audit.record(action="commons_adopt", level="auto", decision="executed",
                                 agent_id=agent_id, params={"entry_id": entry_id, "type": env.type})
        return {"entry_id": entry_id, "type": env.type, "payload": rec["payload"]}

    async def report(self, entry_id: str, agent_id: str, reason: str = "") -> dict:
        """举报:reports 达阈值自动降级为待人工复审。"""
        rec = self._entries.get(entry_id)
        if rec is None:
            raise LayerError("L13", "commons", f"条目不存在: {entry_id}")
        n = self._metrics.report(entry_id, agent_id, reason)
        rec["envelope"].stats.reports = n
        demoted = False
        if n >= self._report_threshold:
            rec["envelope"].status = "deprecated"  # 降级待人工复审(browse 不再可见)
            self._metrics.demote(entry_id)
            demoted = True
        await self._audit.record(action="commons_report", level="auto",
                                 decision="demoted" if demoted else "recorded",
                                 agent_id=agent_id, params={"entry_id": entry_id},
                                 extra={"reason": reason, "reports": n})
        return {"entry_id": entry_id, "reports": n, "demoted": demoted}

    async def revoke(self, entry_id: str, by: str, is_human: bool = False) -> list[dict]:
        """撤销(人类,或作者本人)。已采用实例收到通知(skill 自动禁用待人工确认、
        mcp 立即移除)。返回通知列表。"""
        rec = self._entries.get(entry_id)
        if rec is None:
            raise LayerError("L13", "commons", f"条目不存在: {entry_id}")
        env = rec["envelope"]
        if not is_human and by != env.author:
            raise LayerError("L13", "commons", "revoke 仅限人类或作者本人")
        env.status = "revoked"
        env.updated_at = time.time()
        notifications = []
        for agent_id in self._adoptions.get(entry_id, set()):
            action = {"skill": "disable_pending_human", "mcp_entry": "unmount_immediately",
                      "memory": "flag"}[env.type]
            notifications.append({"agent_id": agent_id, "entry_id": entry_id,
                                  "type": env.type, "action": action})
        await self._audit.record(action="commons_revoke", level="auto", decision="revoked",
                                 agent_id=by, params={"entry_id": entry_id, "type": env.type},
                                 extra={"is_human": is_human, "notified": len(notifications)})
        return notifications

    # ---- 观测(M13.5 仪表原语) ----

    def metrics_report(self) -> dict:
        by_type: dict[str, dict] = {}
        for rec in self._entries.values():
            env = rec["envelope"]
            t = by_type.setdefault(env.type, {"total": 0, "active": 0, "revoked": 0,
                                              "approved": 0})
            t["total"] += 1
            t[env.status] = t.get(env.status, 0) + 1
            if env.review.verdict == "approved":
                t["approved"] += 1
        entries = []
        for eid, rec in self._entries.items():
            env = rec["envelope"]
            entries.append({
                "entry_id": eid, "type": env.type, "status": env.status,
                "author": env.author, "adoptions": env.stats.adoptions,
                "references": self._metrics.spread(eid), "reports": env.stats.reports,
                "verdict": env.review.verdict, "created_at": env.created_at})
        rates = {}
        for t, c in by_type.items():
            rates[t] = {"pass_rate": c["approved"] / c["total"] if c["total"] else 0.0,
                        "revoke_rate": c.get("revoked", 0) / c["total"] if c["total"] else 0.0}
        return {"by_type": by_type, "rates": rates, "entries": entries,
                "commons_metrics": self._metrics.snapshot()}
