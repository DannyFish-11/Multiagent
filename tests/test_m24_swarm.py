"""M24 验收:去中心化 swarm(成员手递手传任务,无中央调度器)。

覆盖:装配校验(空/重名/坏 entry/悬空 handoff);正常流转 intake→tech→summary 与
active/人设切换;转接链硬上限 loop_capped(防乒乓);转录合法(每个 tool_call 都有对应
结果,含"普通工具+转交"同批);普通工具经审批闸(deny 优雅回灌);services 按 autonomy=
swarm 装配、缺成员安全回落;doctor 预检。离线,ScriptedSwarmLLM 驱动。
"""

from __future__ import annotations

from core.config import ApprovalSettings, PolicyRule, load_config
from core.errors import LayerError
from core.schemas import MemoryHit
from core.swarm import SwarmAgent, SwarmMember, build_swarm
from core.tools import AssistantTurn, Tool, ToolCall, handoff_tool


class FakeMemory:
    def __init__(self):
        self.added = []

    async def search(self, query, k=5):
        return [MemoryHit(id="m1", score=0.9, content="用户上次报过登录问题")]

    async def add(self, inp, meta=None):
        self.added.append(inp.content)
        return "id1"


class ScriptedSwarmLLM:
    """按脚本逐步返回 AssistantTurn(实现 chat_tools);记录每次收到的 system + 全 messages。"""
    def __init__(self, turns):
        self._turns = list(turns)
        self._i = 0
        self.systems = []
        self.messages_seen = []

    async def chat_tools(self, messages, tools, **kw):
        self.systems.append(messages[0]["content"])
        self.messages_seen.append(list(messages))
        turn = self._turns[min(self._i, len(self._turns) - 1)]
        self._i += 1
        return turn


_THREE = {"entry": "intake", "members": [
    {"name": "intake", "prompt": "INTAKE_PERSONA", "handoffs": ["tech", "finance"]},
    {"name": "tech", "prompt": "TECH_PERSONA", "tools": ["recall"], "handoffs": ["summary"]},
    {"name": "finance", "prompt": "FIN_PERSONA", "handoffs": ["summary"]},
    {"name": "summary", "prompt": "SUMMARY_PERSONA"},
]}


def _cfg(**over):
    base = dict(agent={"autonomy": "swarm"}, swarm=_THREE,
                embedder={"backend": "fake"}, vectordb={"mode": "memory"})
    base.update(over)
    return load_config(**base)


# ---------------------------------------------------------------- 装配校验

def test_build_swarm_validates():
    def bad(swarm):
        try:
            build_swarm(load_config(swarm=swarm), object(), FakeMemory())
            return None
        except LayerError as e:
            return str(e)
    assert "为空" in bad({"members": []})
    assert "重复" in bad({"members": [{"name": "a"}, {"name": "a"}]})
    assert "entry" in bad({"entry": "x", "members": [{"name": "a"}]})
    assert "未定义" in bad({"members": [{"name": "a", "handoffs": ["ghost"]}]})


def test_build_swarm_assembles_handoff_tools():
    sw = build_swarm(_cfg(), object(), FakeMemory())
    intake = sw._members["intake"]
    names = {t.name for t in intake.tools}
    assert names == {"transfer_to_tech", "transfer_to_finance"}      # 按 handoffs 生成转交工具
    assert sw._members["tech"].tool("recall") is not None            # 私有工具也在
    assert sw._members["summary"].tools == []                        # 终点无工具


# ---------------------------------------------------------------- 正常流转

async def test_handoff_flow_intake_to_tech_to_summary():
    mem = FakeMemory()
    llm = ScriptedSwarmLLM([
        AssistantTurn(tool_calls=[ToolCall("c1", "transfer_to_tech", {"reason": "技术问题"})]),
        AssistantTurn(tool_calls=[ToolCall("c2", "transfer_to_summary", {"reason": "已排查"})]),
        AssistantTurn(content="最终答复:请重置密码。"),
    ])
    sw = build_swarm(_cfg(), llm, mem)
    resp = await sw.run("登录不了")
    assert resp.reply == "最终答复:请重置密码。"
    assert "loop_capped" not in resp.reply
    # 三步的 active 人设依次切换 intake→tech→summary
    assert "INTAKE_PERSONA" in llm.systems[0]
    assert "TECH_PERSONA" in llm.systems[1]
    assert "SUMMARY_PERSONA" in llm.systems[2]
    assert mem.added == ["登录不了"]                                  # 交互写入记忆


async def test_transcript_valid_when_tool_and_handoff_in_same_turn():
    """同一轮里"普通工具 + 转交"并存:每个 tool_call 都要有对应 tool 结果(否则下一次
    chat_tools 转录非法)。"""
    mem = FakeMemory()
    llm = ScriptedSwarmLLM([
        AssistantTurn(tool_calls=[ToolCall("c1", "recall", {"query": "x"}),
                                  ToolCall("c2", "transfer_to_summary", {})]),
        AssistantTurn(content="好的。"),
    ])
    # intake 也带 recall 工具
    cfg = _cfg(swarm={"entry": "intake", "members": [
        {"name": "intake", "prompt": "I", "tools": ["recall"], "handoffs": ["summary"]},
        {"name": "summary", "prompt": "S"}]})
    sw = build_swarm(cfg, llm, mem)
    resp = await sw.run("q")
    assert resp.reply == "好的。"
    # 第二次调用(summary)看到的转录:assistant 的 2 个 tool_calls 各有 1 条 tool 结果
    msgs = llm.messages_seen[1]
    asst = [m for m in msgs if m.get("role") == "assistant" and m.get("tool_calls")][0]
    tool_results = [m for m in msgs if m.get("role") == "tool"]
    assert len(asst["tool_calls"]) == len(tool_results) == 2
    assert "S" in llm.systems[1]                                     # active 已切到 summary


# ---------------------------------------------------------------- 转接链上限(防乒乓)

async def test_handoff_chain_capped():
    mem = FakeMemory()
    # a↔b 互转;delegation_chain=2 → 第 3 次转交被拒,步数上限强制收尾
    cfg = _cfg(swarm={"entry": "a", "members": [
        {"name": "a", "prompt": "A", "handoffs": ["b"]},
        {"name": "b", "prompt": "B", "handoffs": ["a"]}]},
        loops={"per_point": {"delegation_chain": 2, "swarm_steps": 6}})
    llm = ScriptedSwarmLLM([
        AssistantTurn(tool_calls=[ToolCall("h", "transfer_to_b", {})]),   # a→b (#1)
        AssistantTurn(tool_calls=[ToolCall("h", "transfer_to_a", {})]),   # b→a (#2, 达上限)
        AssistantTurn(tool_calls=[ToolCall("h", "transfer_to_b", {})]),   # a→b 被拒
        AssistantTurn(tool_calls=[ToolCall("h", "transfer_to_b", {})]),   # 仍被拒
        AssistantTurn(tool_calls=[ToolCall("h", "transfer_to_b", {})]),
        AssistantTurn(tool_calls=[ToolCall("h", "transfer_to_b", {})]),
        AssistantTurn(content="收尾。"),
    ])
    sw = build_swarm(cfg, llm, mem)
    resp = await sw.run("go")
    assert "loop_capped" in resp.reply                                # 触顶不静默
    assert "a → b → a" in resp.reply                                  # 恰 2 次转交后不再前进


# ---------------------------------------------------------------- 审批治理复用

async def test_normal_tool_gated_by_approval(tmp_path):
    from core.approval import ApprovalQueue, Notifier
    from core.audit import AuditLog

    settings = ApprovalSettings(
        policies=[PolicyRule(action="danger", when={}, level="deny")], default_level="auto")
    approval = ApprovalQueue(settings, AuditLog(tmp_path / "a.jsonl"), Notifier(settings))
    ran = []

    async def _danger(args):
        ran.append(1)
        return "done"

    # 直接构造成员(带非 safe 危险工具),验证 swarm 里普通工具也过审批闸
    danger = Tool("danger", "危险", {"type": "object", "properties": {}}, _danger, action="danger")
    member = SwarmMember(name="solo", prompt="P", tools=[danger], handoffs=())
    llm = ScriptedSwarmLLM([AssistantTurn(tool_calls=[ToolCall("c", "danger", {})]),
                            AssistantTurn(content="没法做那个。")])
    sw = SwarmAgent(llm, FakeMemory(), load_config(), {"solo": member}, "solo", approval=approval)
    resp = await sw.run("干点危险的")
    assert ran == []                                                  # deny → 从未执行
    assert resp.reply == "没法做那个。"                                # 优雅回灌收尾


async def test_denied_handoff_does_not_switch(tmp_path):
    """治理真正生效:对 handoff 动作的 deny 策略必须**拦住转交本身**(不只是审计),
    active 不切换,拒绝回灌,当前成员自行收尾。"""
    from core.approval import ApprovalQueue, Notifier
    from core.audit import AuditLog

    settings = ApprovalSettings(
        policies=[PolicyRule(action="handoff:b", when={}, level="deny")], default_level="auto")
    approval = ApprovalQueue(settings, AuditLog(tmp_path / "a.jsonl"), Notifier(settings))
    cfg = _cfg(swarm={"entry": "a", "members": [
        {"name": "a", "prompt": "A_PERSONA", "handoffs": ["b"]},
        {"name": "b", "prompt": "B_PERSONA"}]})
    llm = ScriptedSwarmLLM([
        AssistantTurn(tool_calls=[ToolCall("c", "transfer_to_b", {})]),   # 被 deny
        AssistantTurn(content="我这边直接处理了。"),                        # a 自行收尾
    ])
    from core.swarm import build_swarm as _bs
    sw = _bs(cfg, llm, FakeMemory(), approval=approval)
    resp = await sw.run("go")
    assert resp.reply == "我这边直接处理了。"
    assert "A_PERSONA" in llm.systems[1]                              # 仍是 a(未切到 b)
    fed = [m for m in llm.messages_seen[1] if m.get("role") == "tool"][0]["content"]
    assert "被拒" in fed


def test_self_handoff_rejected():
    def bad():
        try:
            build_swarm(load_config(swarm={"members": [{"name": "a", "handoffs": ["a"]}]}),
                        object(), FakeMemory())
            return None
        except LayerError as e:
            return str(e)
    assert "自身" in bad()


async def test_handoff_to_unknown_member_is_rejected():
    """防御:成员的转交工具指向不存在成员时,回灌错误、不切换、不崩。"""
    ghost = handoff_tool("ghost")                                     # 目标不在 members
    member = SwarmMember(name="solo", prompt="P", tools=[ghost], handoffs=("ghost",))
    llm = ScriptedSwarmLLM([AssistantTurn(tool_calls=[ToolCall("c", "transfer_to_ghost", {})]),
                            AssistantTurn(content="改为自行处理。")])
    sw = SwarmAgent(llm, FakeMemory(), load_config(), {"solo": member}, "solo")
    resp = await sw.run("q")
    assert resp.reply == "改为自行处理。"
    fed = [m for m in llm.messages_seen[1] if m.get("role") == "tool"][0]["content"]
    assert "不存在" in fed


# ---------------------------------------------------------------- 安全须知(注入防御)

async def test_member_prompt_has_injection_defense():
    sw = build_swarm(_cfg(), ScriptedSwarmLLM([AssistantTurn(content="x")]), FakeMemory())
    sp = sw._system_prompt(sw._members["tech"], [])
    assert "不可信数据" in sp and "TECH_PERSONA" in sp


# ---------------------------------------------------------------- services 装配

def test_services_wires_swarm_when_autonomy_swarm():
    from fastapi.testclient import TestClient

    from services.api import create_app

    llm = ScriptedSwarmLLM([
        AssistantTurn(tool_calls=[ToolCall("c1", "transfer_to_summary", {})]),
        AssistantTurn(content="搞定。"),
    ])
    cfg = _cfg(swarm={"entry": "intake", "members": [
        {"name": "intake", "prompt": "I", "handoffs": ["summary"]},
        {"name": "summary", "prompt": "S"}]})
    app = create_app(cfg, llm=llm, memory=FakeMemory())
    with TestClient(app) as c:
        assert type(app.state.agent).__name__ == "SwarmAgent"
        r = c.post("/chat", json={"message": "你好", "session_id": "s"})
        assert r.status_code == 200 and "搞定" in r.json()["reply"]


def test_swarm_without_members_falls_back():
    from fastapi.testclient import TestClient

    from services.api import create_app

    # autonomy=swarm 但无成员 → 安全回落 MemoryAgent(不崩)
    cfg = load_config(agent={"autonomy": "swarm"}, embedder={"backend": "fake"},
                      vectordb={"mode": "memory"}, llm={"mode": "echo"})
    app = create_app(cfg, memory=FakeMemory())
    with TestClient(app) as c:
        assert type(app.state.agent).__name__ == "MemoryAgent"
        assert c.get("/healthz").status_code == 200


# ---------------------------------------------------------------- doctor

def test_doctor_flags_swarm_misconfig():
    from core.doctor import run_doctor

    checks = run_doctor(load_config(agent={"autonomy": "swarm"},
                                    llm={"mode": "api", "chat": {"base_url": "http://x",
                                                                 "model": "gpt"}}))
    assert [c for c in checks if "swarm" in c.title and c.level == "fail"]   # 空成员 → fail


def test_doctor_ok_for_valid_swarm():
    from core.doctor import run_doctor

    checks = run_doctor(_cfg(llm={"mode": "api", "chat": {"base_url": "http://x", "model": "gpt"}}))
    line = [c for c in checks if "autonomy=swarm" in c.title]
    assert line and line[0].level == "ok"
