"""PHASE4 M15.1 + M13 metrics 原语:VotePolicy + commons 引用/举报/降级。

注:M15 完整对照实验(grader vs vote vs 自然筛选,出 reports/m15_governance.md)
是人类停点 + 真花钱,不在此测试;这里只验证已实装的机制本身。
"""

from __future__ import annotations

from core.commons_metrics import CommonsMetrics
from core.promotion import VotePolicy
from tests.conftest import ScriptedLLM


class StubVoter:
    def __init__(self, agent_id, accept, reason="r"):
        self.agent_id = agent_id
        self._accept = accept
        self._reason = reason

    async def vote(self, content, meta):
        return self._accept, self._reason


# ---------------------------------------------------------------- VotePolicy

async def test_vote_simple_majority():
    voters = [StubVoter("a1", True), StubVoter("a2", True), StubVoter("a3", False)]
    policy = VotePolicy(voters, rule="simple_majority")
    assert await policy.decide("客观事实", {}) == "promote"  # 2/3 accept
    # 记录每张选票(谁、投了什么、理由)
    assert len(policy.last_ballots) == 3
    assert {b["agent_id"] for b in policy.last_ballots} == {"a1", "a2", "a3"}


async def test_vote_tie_rejects():
    voters = [StubVoter("a1", True), StubVoter("a2", False)]
    assert await VotePolicy(voters, rule="simple_majority").decide("x", {}) == "reject"


async def test_vote_supermajority():
    voters = [StubVoter(f"a{i}", i < 2) for i in range(3)]  # 2/3 accept
    # 简单多数通过,超多数(0.66*3=1.98 → 需 ≥2)也通过
    assert await VotePolicy(voters, rule="supermajority", supermajority_ratio=0.66).decide("x", {}) == "promote"
    # 提高门槛到 0.9(需 ≥2.7 → 3 票)则不通过
    assert await VotePolicy(voters, rule="supermajority", supermajority_ratio=0.9).decide("x", {}) == "reject"


async def test_vote_audited(tmp_path):
    from core.audit import AuditLog

    audit = AuditLog(tmp_path / "audit.jsonl")
    voters = [StubVoter("a1", True, "有用"), StubVoter("a2", False, "过时")]
    await VotePolicy(voters, audit=audit).decide("候选记忆", {})
    entries = audit.read_all()
    votes = [e for e in entries if e["action"] == "vote"]
    assert len(votes) == 2
    assert {e["decision"] for e in votes} == {"accept", "reject"}
    assert any(e.get("reason") == "过时" for e in votes)


async def test_llm_voter_parses_json():
    from core.promotion import LLMVoter

    voter = LLMVoter(ScriptedLLM(replies=['{"accept": true, "reason": "客观知识"}']), agent_id="a1")
    accept, reason = await voter.vote("巴黎是法国首都", {})
    assert accept is True and reason == "客观知识"


async def test_vote_empty_electorate_fails_closed():
    """回归:空投票团在 supermajority/weighted 下不得 fail-open。

    修复前:n=0 时 accepts(0) >= n*ratio(0) 恒真 → 空投票团裁决 promote,
    准入闸形同虚设。修复后:无票可记一律 reject(与 default_level=confirm
    同一条"无凭据时保守"不变量)。
    """
    for rule in ("supermajority", "weighted", "simple_majority"):
        assert await VotePolicy([], rule=rule).decide("x", {}) == "reject"


# ---------------------------------------------------------------- commons metrics(M13 原语)

def test_commons_cite_report_demote(tmp_path):
    m = CommonsMetrics(tmp_path / "commons.json")
    m.register("item1")
    m.cite("item1", "agentA")
    m.cite("item1", "agentB")
    m.cite("item1", "agentA")  # 同一实例重复引用不重复计入 adopters
    assert m.spread("item1") == 2  # 扩散度 = 采用的不同实例数
    assert m.snapshot()["item1"]["cites"] == 3

    # 举报 → 降级判据(按不同上报者计数,防单实例刷举报)
    for r in ("rc1", "rc2", "rc3"):
        m.report("item1", r, "错误信息")
    # cites 采用者 2 个,3 个不同上报者 → reports(3) > adopters(2) 且达阈值
    assert m.should_demote("item1", report_threshold=3) is True
    m.demote("item1")
    assert m.snapshot()["item1"]["demoted"] is True
    assert m.should_demote("item1") is False  # 已降级不重复


def test_commons_survival_and_persistence(tmp_path):
    import time

    m = CommonsMetrics(tmp_path / "c.json")
    m.register("x")
    now = time.time()
    assert m.survival_seconds("x", now + 10) >= 10 - 1
    # 持久化(挂 volume 场景)
    m2 = CommonsMetrics(tmp_path / "c.json")
    assert "x" in m2.snapshot()


def test_commons_natural_selection_signal(tmp_path):
    """C 臂'自然筛选'依据:高举报低引用条目应被降级,高引用条目存活。"""
    m = CommonsMetrics(tmp_path / "c.json")
    # 坏品:无人采用,多个不同上报者举报
    m.register("bad")
    for r in ("rv1", "rv2", "rv3", "rv4"):
        m.report("bad", r)
    # 良品:多实例采用,零举报
    m.register("good")
    for a in ("a1", "a2", "a3"):
        m.cite("good", a)
    assert m.should_demote("bad") is True
    assert m.should_demote("good") is False
    assert m.spread("good") == 3 and m.spread("bad") == 0
