"""M25 验收:中心调度 supervisor(协调者委派 worker、汇总结果;与 swarm 互补)。

覆盖:装配校验(空/重名 worker);协调者委派→worker 执行→取回结果→汇总的全流程;
worker 不写回长期记忆而协调者写(write_back);委派经审批闸(deny 真正拦住 worker 运行);
services 按 autonomy=supervisor 装配、缺 worker 安全回落;doctor 预检。离线,脚本 LLM 驱动。
"""

from __future__ import annotations

from core.config import ApprovalSettings, PolicyRule, load_config
from core.errors import LayerError
from core.schemas import MemoryHit
from core.supervisor import build_supervisor
from core.tool_agent import ToolAgent
from core.tools import AssistantTurn, ToolCall, delegate_tool


class FakeMemory:
    def __init__(self):
        self.added = []

    async def search(self, query, k=5):
        return [MemoryHit(id="m1", score=0.9, content="旧上下文")]

    async def add(self, inp, meta=None):
        self.added.append(inp.content)
        return "id1"


class ScriptedToolLLM:
    """按脚本逐步返回 AssistantTurn;记录每次的 system + 全 messages(供断言委派转录)。"""
    def __init__(self, turns):
        self._turns = list(turns)
        self._i = 0
        self.messages_seen = []

    async def chat_tools(self, messages, tools, **kw):
        self.messages_seen.append(list(messages))
        turn = self._turns[min(self._i, len(self._turns) - 1)]
        self._i += 1
        return turn


_TWO = {"prompt": "COORD_PERSONA", "workers": [
    {"name": "researcher", "prompt": "RESEARCH_PERSONA", "tools": ["recall"]},
    {"name": "writer", "prompt": "WRITE_PERSONA"},
]}


def _cfg(**over):
    base = dict(agent={"autonomy": "supervisor"}, supervisor=_TWO,
                embedder={"backend": "fake"}, vectordb={"mode": "memory"})
    base.update(over)
    return load_config(**base)


# ---------------------------------------------------------------- 装配校验

def test_build_supervisor_validates():
    def bad(sup):
        try:
            build_supervisor(load_config(supervisor=sup), object(), FakeMemory())
            return None
        except LayerError as e:
            return str(e)
    assert "为空" in bad({"workers": []})
    assert "重复" in bad({"workers": [{"name": "a"}, {"name": "a"}]})


def test_supervisor_tools_are_delegates():
    sup = build_supervisor(_cfg(), object(), FakeMemory())
    assert set(sup._tools) == {"delegate_to_researcher", "delegate_to_writer"}
    assert sup._persona == "COORD_PERSONA"


# ---------------------------------------------------------------- 委派全流程

async def test_supervisor_delegates_and_synthesizes():
    mem = FakeMemory()
    # 调用序列:协调者委派 researcher → researcher 直接回答 → 协调者汇总
    llm = ScriptedToolLLM([
        AssistantTurn(tool_calls=[ToolCall("d1", "delegate_to_researcher", {"task": "查 X"})]),
        AssistantTurn(content="研究结果:Y"),                          # worker 的回答
        AssistantTurn(content="综合答复:基于 Y 得出结论。"),          # 协调者汇总
    ])
    sup = build_supervisor(_cfg(), llm, mem)
    resp = await sup.run("帮我研究一下 X")
    assert resp.reply == "综合答复:基于 Y 得出结论。"
    # worker 结果回灌给协调者(出现在协调者第二次调用的 tool 消息里)
    final_msgs = llm.messages_seen[2]
    fed = [m for m in final_msgs if m.get("role") == "tool"][0]["content"]
    assert "研究结果:Y" in fed
    # write_back:只有协调者写回原始用户消息;worker 的子任务"查 X"不入库
    assert mem.added == ["帮我研究一下 X"]


# ---------------------------------------------------------------- 审批治理:委派可被 deny 拦住

async def test_delegation_denied_worker_never_runs(tmp_path):
    from core.approval import ApprovalQueue, Notifier
    from core.audit import AuditLog

    settings = ApprovalSettings(
        policies=[PolicyRule(action="delegate:researcher", when={}, level="deny")],
        default_level="auto")
    approval = ApprovalQueue(settings, AuditLog(tmp_path / "a.jsonl"), Notifier(settings))
    ran = []

    async def fake_worker(task):
        ran.append(task)                                             # 不应运行(deny 拦在闸前)
        return "worker 结果"
    deleg = delegate_tool("researcher", fake_worker)
    llm = ScriptedToolLLM([
        AssistantTurn(tool_calls=[ToolCall("c", "delegate_to_researcher", {"task": "t"})]),
        AssistantTurn(content="那我自己处理。"),
    ])
    sup = ToolAgent(llm, FakeMemory(), load_config(), approval=approval,
                    tools=[deleg], persona="协调者")
    resp = await sup.run("go")
    assert ran == []                                                 # 运行 worker 是 execute 回调,被 deny → 从未跑
    assert resp.reply == "那我自己处理。"


# ---------------------------------------------------------------- services 装配

def test_services_wires_supervisor():
    from fastapi.testclient import TestClient

    from services.api import create_app

    llm = ScriptedToolLLM([
        AssistantTurn(tool_calls=[ToolCall("d", "delegate_to_writer", {"task": "写"})]),
        AssistantTurn(content="草稿完成。"),
        AssistantTurn(content="最终:草稿完成。"),
    ])
    app = create_app(_cfg(), llm=llm, memory=FakeMemory())
    with TestClient(app) as c:
        agent = app.state.agent
        assert type(agent).__name__ == "ToolAgent"                  # supervisor 即 ToolAgent
        assert any(n.startswith("delegate_to_") for n in agent._tools)   # 工具是委派工具
        r = c.post("/chat", json={"message": "写点东西", "session_id": "s"})
        assert r.status_code == 200 and "最终" in r.json()["reply"]


def test_supervisor_without_workers_falls_back():
    from fastapi.testclient import TestClient

    from services.api import create_app

    cfg = load_config(agent={"autonomy": "supervisor"}, embedder={"backend": "fake"},
                      vectordb={"mode": "memory"}, llm={"mode": "echo"})
    app = create_app(cfg, memory=FakeMemory())
    with TestClient(app) as c:
        assert type(app.state.agent).__name__ == "MemoryAgent"      # 缺 worker → 安全回落
        assert c.get("/healthz").status_code == 200


# ---------------------------------------------------------------- doctor

def test_doctor_flags_empty_workers():
    from core.doctor import run_doctor

    checks = run_doctor(load_config(agent={"autonomy": "supervisor"},
                                    llm={"mode": "api", "chat": {"base_url": "http://x",
                                                                 "model": "gpt"}}))
    assert [c for c in checks if "supervisor" in c.title and c.level == "fail"]


def test_doctor_ok_for_valid_supervisor():
    from core.doctor import run_doctor

    checks = run_doctor(_cfg(llm={"mode": "api", "chat": {"base_url": "http://x", "model": "gpt"}}))
    line = [c for c in checks if "autonomy=supervisor" in c.title]
    assert line and line[0].level == "ok"


# ---------------------------------------------------------------- worker 不写回(直接验证)

async def test_worker_does_not_write_back():
    mem = FakeMemory()
    worker = ToolAgent(ScriptedToolLLM([AssistantTurn(content="done")]), mem, load_config(),
                       persona="W", write_back=False)
    await worker.run("子任务")
    assert mem.added == []                                           # write_back=False → 不自动入库


async def test_write_back_only_governs_auto_write_not_remember_tool():
    """语义明确:write_back=False 只关**自动**写回;若显式给 worker 装了 remember 工具,
    它仍会照常写(算子有意授予即尊重)。"""
    from core.tools import remember_tool

    mem = FakeMemory()
    llm = ScriptedToolLLM([
        AssistantTurn(tool_calls=[ToolCall("c", "remember", {"text": "要点 Z"})]),
        AssistantTurn(content="记好了。"),
    ])
    worker = ToolAgent(llm, mem, load_config(), tools=[remember_tool(mem)],
                       persona="W", write_back=False)
    await worker.run("请记住要点 Z")
    assert mem.added == ["要点 Z"]           # 显式 remember 照写;但子任务本身未被自动写回
