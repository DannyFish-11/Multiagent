"""M29 验收:agent 编排层可观测性(TracedAgent 发 span)。

用 FakeTracer(不依赖 OTel 是否安装)直接验证包装器逻辑:run/chat/chat_stream 各开一个
agent span;chat_stream 把 M28 的 step 事件记为 span event(编排时间线);enabled=false
时 instrument_agent 原样返回(零开销);chat_stream 事件本身透传不变。
"""

from __future__ import annotations

import contextlib

from adapters.observability import TracedAgent, instrument_agent
from core.config import load_config


class FakeSpan:
    def __init__(self, name):
        self.name = name
        self.attrs = {}
        self.events = []

    def set_attribute(self, k, v):
        self.attrs[k] = v

    def add_event(self, name, attrs=None):
        self.events.append((name, attrs or {}))


class FakeTracer:
    def __init__(self):
        self.spans = []

    @contextlib.contextmanager
    def start_as_current_span(self, name):
        sp = FakeSpan(name)
        self.spans.append(sp)
        yield sp


class FakeResp:
    def __init__(self, reply):
        self.reply = reply


class FakeAgent:
    """最小 inner agent:run/chat 返回定值;chat_stream 吐 meta/step/token/done。"""
    def __init__(self):
        self.ran = []

    async def run(self, message, session_id="default"):
        self.ran.append(("run", message, session_id))
        return FakeResp("答复R")

    async def chat(self, message, session_id="default", image=None, sync_memory_write=True):
        self.ran.append(("chat", message, session_id))
        return FakeResp("答复C")

    async def chat_stream(self, message, session_id="default", image=None):
        yield {"type": "meta", "event_id": "e1", "memories_used": []}
        yield {"type": "step", "kind": "tool", "name": "recall", "status": "start"}
        yield {"type": "step", "kind": "tool", "name": "recall", "status": "done"}
        yield {"type": "step", "kind": "handoff", "name": "tech", "status": "done"}
        yield {"type": "token", "text": "最终"}
        yield {"type": "done", "event_id": "e1"}


def _traced():
    tr = FakeTracer()
    return TracedAgent(FakeAgent(), tr, load_config()), tr


# ---------------------------------------------------------------- run / chat

async def test_run_opens_agent_span():
    agent, tr = _traced()
    resp = await agent.run("问题", session_id="s1")
    assert resp.reply == "答复R"                       # 透传返回不变
    assert len(tr.spans) == 1 and tr.spans[0].name == "agent.run"
    assert tr.spans[0].attrs["agent.type"] == "FakeAgent"
    assert tr.spans[0].attrs["session.id"] == "s1"
    assert "答复R" in tr.spans[0].attrs["gen_ai.completion.summary"]


async def test_chat_opens_agent_span():
    agent, tr = _traced()
    resp = await agent.chat("问题", session_id="s2")
    assert resp.reply == "答复C"
    assert tr.spans[0].name == "agent.chat" and tr.spans[0].attrs["session.id"] == "s2"


# ---------------------------------------------------------------- chat_stream + step events

async def test_chat_stream_records_steps_as_span_events_and_passes_through():
    agent, tr = _traced()
    evs = [ev async for ev in agent.chat_stream("q", session_id="s3")]
    # 事件透传不变(与 inner 一致)
    assert [e["type"] for e in evs] == ["meta", "step", "step", "step", "token", "done"]
    span = tr.spans[0]
    assert span.name == "agent.stream"
    # 3 个 step 事件被记为 span event
    step_events = [e for e in span.events if e[0] == "step"]
    assert len(step_events) == 3
    assert span.attrs["agent.step_count"] == 3
    assert span.attrs["gen_ai.completion.length"] == len("最终")
    # handoff 那条也在(kind=handoff)
    assert any(a.get("kind") == "handoff" and a.get("name") == "tech" for _, a in step_events)


# ---------------------------------------------------------------- 开关

def test_instrument_agent_noop_when_disabled():
    inner = FakeAgent()
    cfg = load_config()                                # observability.enabled 默认 false
    assert instrument_agent(inner, cfg) is inner       # 原样返回,零开销


def test_instrument_agent_wraps_when_enabled(monkeypatch):
    import adapters.observability as obs

    inner = FakeAgent()
    cfg = load_config(observability={"enabled": True})
    monkeypatch.setattr(obs, "init_tracing", lambda c: FakeTracer())   # 免真 OTel 依赖
    wrapped = obs.instrument_agent(inner, cfg)
    assert isinstance(wrapped, TracedAgent) and wrapped._inner is inner
