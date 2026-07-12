"""M28 验收:流式进度事件(step)覆盖工具/多 agent 模式。

covers:ToolAgent.chat_stream 发 tool 的 start/done 步骤 + token + done;run() 与
chat_stream 结果一致(同一 _stream primitive,无重复循环);supervisor 委派显示 delegate
步骤;swarm 显示 handoff 步骤;/chat/stream 端点在 tools 档吐出 step 事件。
"""

from __future__ import annotations

import json

from core.config import load_config
from core.schemas import MemoryHit
from core.tool_agent import ToolAgent
from core.tools import AssistantTurn, Tool, ToolCall, recall_tool


class FakeMemory:
    def __init__(self):
        self.added = []

    async def search(self, query, k=5):
        return [MemoryHit(id="m1", score=0.9, content="记忆X")]

    async def add(self, inp, meta=None):
        self.added.append(inp.content)
        return "id1"


class ScriptedToolLLM:
    def __init__(self, turns):
        self._turns = list(turns)
        self._i = 0

    async def chat_tools(self, messages, tools, **kw):
        turn = self._turns[min(self._i, len(self._turns) - 1)]
        self._i += 1
        return turn


class ScriptedSwarmLLM(ScriptedToolLLM):
    pass


def _parse_sse(text):
    return [json.loads(ln[5:].strip()) for ln in text.splitlines() if ln.startswith("data:")]


# ---------------------------------------------------------------- ToolAgent

async def test_toolagent_chat_stream_step_events():
    mem = FakeMemory()
    llm = ScriptedToolLLM([
        AssistantTurn(tool_calls=[ToolCall("c", "recall", {"query": "x"})]),
        AssistantTurn(content="答案"),
    ])
    agent = ToolAgent(llm, mem, load_config(), tools=[recall_tool(mem)])
    evs = [ev async for ev in agent.chat_stream("q")]
    assert evs[0]["type"] == "meta" and isinstance(evs[0]["memories_used"], list)
    steps = [e for e in evs if e["type"] == "step"]
    assert any(e["name"] == "recall" and e["status"] == "start" for e in steps)
    assert any(e["name"] == "recall" and e["status"] == "done" for e in steps)
    tok = [e for e in evs if e["type"] == "token"][0]
    assert tok["text"] == "答案"
    assert evs[-1]["type"] == "done"


async def test_run_matches_chat_stream_reply():
    """run() 与 chat_stream() 走同一 _stream:最终 reply 一致(重构无回归)。"""
    def mk():
        return ScriptedToolLLM([
            AssistantTurn(tool_calls=[ToolCall("c", "recall", {"query": "x"})]),
            AssistantTurn(content="最终结论"),
        ])
    mem1 = FakeMemory()
    resp = await ToolAgent(mk(), mem1, load_config(), tools=[recall_tool(mem1)]).run("q")
    mem2 = FakeMemory()
    toks = [e["text"] async for e in
            ToolAgent(mk(), mem2, load_config(), tools=[recall_tool(mem2)]).chat_stream("q")
            if e["type"] == "token"]
    assert resp.reply == "".join(toks) == "最终结论"


# ---------------------------------------------------------------- supervisor(delegate 步骤)

async def test_supervisor_stream_shows_delegate_step():
    from core.supervisor import build_supervisor

    llm = ScriptedToolLLM([
        AssistantTurn(tool_calls=[ToolCall("d", "delegate_to_writer", {"task": "写"})]),
        AssistantTurn(content="worker 完成"),         # worker 的回答
        AssistantTurn(content="汇总答复"),             # 协调者汇总
    ])
    cfg = load_config(agent={"autonomy": "supervisor"}, embedder={"backend": "fake"},
                      vectordb={"mode": "memory"},
                      supervisor={"workers": [{"name": "writer", "prompt": "W"}]})
    sup = build_supervisor(cfg, llm, FakeMemory())
    evs = [ev async for ev in sup.chat_stream("q")]
    steps = [e for e in evs if e["type"] == "step"]
    assert any(e["kind"] == "delegate" and e["name"] == "delegate_to_writer" for e in steps)
    assert [e for e in evs if e["type"] == "token"][0]["text"] == "汇总答复"


# ---------------------------------------------------------------- swarm(handoff 步骤)

async def test_swarm_stream_shows_handoff_step():
    from core.swarm import build_swarm

    llm = ScriptedSwarmLLM([
        AssistantTurn(tool_calls=[ToolCall("h", "transfer_to_summary", {})]),
        AssistantTurn(content="最终"),
    ])
    cfg = load_config(agent={"autonomy": "swarm"}, embedder={"backend": "fake"},
                      vectordb={"mode": "memory"},
                      swarm={"entry": "intake", "members": [
                          {"name": "intake", "prompt": "I", "handoffs": ["summary"]},
                          {"name": "summary", "prompt": "S"}]})
    sw = build_swarm(cfg, llm, FakeMemory())
    evs = [ev async for ev in sw.chat_stream("q")]
    steps = [e for e in evs if e["type"] == "step"]
    assert any(e["kind"] == "handoff" and e["name"] == "summary" for e in steps)


# ---------------------------------------------------------------- 端点

def test_endpoint_streams_step_events_for_tools():
    from fastapi.testclient import TestClient

    from services.api import create_app

    class ToolLLM:
        def __init__(self):
            self.i = 0

        async def chat_tools(self, messages, tools, **kw):
            self.i += 1
            if self.i == 1:
                return AssistantTurn(tool_calls=[ToolCall("c", "recall", {"query": "x"})])
            return AssistantTurn(content="最终答复")

    cfg = load_config(agent={"autonomy": "tools", "tools": ["recall"]},
                      embedder={"backend": "fake"}, vectordb={"mode": "memory"})
    app = create_app(cfg, llm=ToolLLM(), memory=FakeMemory())
    with TestClient(app) as c:
        r = c.post("/chat/stream", json={"message": "q", "session_id": "s"})
        evs = _parse_sse(r.text)
        assert any(e["type"] == "step" and e["name"] == "recall" for e in evs)
        assert any(e["type"] == "token" and "最终答复" in e["text"] for e in evs)
        assert evs[-1]["type"] == "done"


async def test_toolagent_logs_retrieval_event():
    """M8 回归:autonomy=tools(默认)也要记检索事件(供 /feedback + 代谢),不能只有 chat 档记。"""
    logged = []

    class Logger:
        def log(self, ev):
            logged.append(ev)

    agent = ToolAgent(ScriptedToolLLM([AssistantTurn(content="hi")]), FakeMemory(),
                      load_config(), tools=[])
    agent.set_retrieval_logger(Logger())
    resp = await agent.run("我的猫叫什么")
    assert len(logged) == 1
    assert logged[0].query == "我的猫叫什么" and logged[0].event_id == resp.event_id
    assert logged[0].hit_ids == ["m1"]                 # 命中记忆 id 记入事件


def test_unused_tool_import_guard():
    """占位:确保 Tool 导入被使用(避免 F401);同时校验 spec 不含内部字段。"""
    t = Tool("x", "d", {"type": "object", "properties": {}}, None)
    assert "handoff_to" not in t.spec()["function"]
