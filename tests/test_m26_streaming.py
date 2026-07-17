"""M26 验收:流式输出(SSE /chat/stream)。

覆盖:OpenAICompatAdapter.chat_stream 解析 SSE 增量(MockTransport,零外呼);MemoryAgent
.chat_stream 事件序列(meta→token→done)+ 流末写记忆;不支持流式的 LLM 回落整段;
/chat/stream 端点在 chat 档真逐 token、在 tools 档整段一次性;流内出错发 error 事件。
"""

from __future__ import annotations

import json

import httpx

from core.agent import MemoryAgent
from core.config import LLMRoleSettings, load_config
from core.schemas import MemoryHit, Message


class FakeMemory:
    def __init__(self):
        self.added = []

    async def search(self, query, k=5):
        return [MemoryHit(id="m1", score=0.9, content="记忆X")]

    async def add(self, inp, meta=None):
        self.added.append(inp.content)
        return "id1"


# ---------------------------------------------------------------- adapter 层

async def test_adapter_chat_stream_parses_sse():
    from adapters.llm import OpenAICompatAdapter

    body = (b'data: {"choices":[{"delta":{"content":"\xe4\xbd\xa0\xe5\xa5\xbd"}}]}\n\n'
            b'data: {"choices":[{"delta":{"content":"\xef\xbc\x8c\xe4\xb8\x96\xe7\x95\x8c"}}]}\n\n'
            b'data: {"choices":[],"usage":{"prompt_tokens":5,"completion_tokens":3}}\n\n'
            b'data: [DONE]\n\n')

    def handler(req):
        b = json.loads(req.content)
        assert b["stream"] is True                                # 真发了 stream=true
        assert "stream_options" not in b                          # 默认不发(防不兼容网关 400)
        return httpx.Response(200, content=body)

    ad = OpenAICompatAdapter(LLMRoleSettings(base_url="http://x/v1", api_key="k", model="m"),
                             transport=httpx.MockTransport(handler))
    pieces = [p async for p in ad.chat_stream([Message(role="user", content="hi")])]
    assert pieces == ["你好", "，世界"]                           # 逐块增量,拼起来是完整回复


async def test_adapter_stream_usage_opt_in():
    """stream_usage=true 才发 stream_options.include_usage(默认关,兼容性优先)。"""
    from adapters.llm import OpenAICompatAdapter

    seen = {}

    def handler(req):
        seen["opts"] = json.loads(req.content).get("stream_options")
        return httpx.Response(200, content=b'data: {"choices":[{"delta":{"content":"a"}}]}\n\n'
                                             b'data: [DONE]\n\n')

    ad = OpenAICompatAdapter(
        LLMRoleSettings(base_url="http://x/v1", api_key="k", model="m", stream_usage=True),
        transport=httpx.MockTransport(handler))
    _ = [p async for p in ad.chat_stream([Message(role="user", content="hi")])]
    assert seen["opts"] == {"include_usage": True}


async def test_adapter_chat_stream_http_error_raises():
    from adapters.llm import OpenAICompatAdapter
    from core.errors import LayerError

    ad = OpenAICompatAdapter(LLMRoleSettings(base_url="http://x/v1", api_key="k", model="m"),
                             transport=httpx.MockTransport(lambda r: httpx.Response(500, text="boom")))
    try:
        async for _ in ad.chat_stream([Message(role="user", content="hi")]):
            pass
        assert False, "应抛 LayerError"
    except LayerError as e:
        assert "500" in str(e)


# ---------------------------------------------------------------- MemoryAgent 层

class StreamLLM:
    async def chat_stream(self, messages, **kw):
        for p in ("甲", "乙", "丙"):
            yield p

    async def chat(self, messages, **kw):
        return "甲乙丙"


async def test_memoryagent_chat_stream_events():
    mem = FakeMemory()
    agent = MemoryAgent(StreamLLM(), mem, load_config())
    events = [ev async for ev in agent.chat_stream("问题")]
    assert events[0]["type"] == "meta" and "event_id" in events[0]
    assert events[0]["memories_used"][0]["content"] == "记忆X"
    tokens = [e["text"] for e in events if e["type"] == "token"]
    assert tokens == ["甲", "乙", "丙"]                            # 真逐 token
    assert events[-1]["type"] == "done"
    assert mem.added == ["问题"]                                  # 流末写入记忆


class NonStreamLLM:
    async def chat(self, messages, **kw):
        return "整段回复"


async def test_memoryagent_chat_stream_fallback():
    """LLM 不支持流式 → 回落:一个 token 事件即整段回复。"""
    agent = MemoryAgent(NonStreamLLM(), FakeMemory(), load_config())
    events = [ev async for ev in agent.chat_stream("q")]
    tokens = [e["text"] for e in events if e["type"] == "token"]
    assert tokens == ["整段回复"]
    assert events[-1]["type"] == "done"


# ---------------------------------------------------------------- 端点层

def _parse_sse(text: str) -> list[dict]:
    return [json.loads(line[5:].strip())
            for line in text.splitlines() if line.startswith("data:")]


def test_endpoint_streams_chat_path():
    from fastapi.testclient import TestClient

    from services.api import create_app

    cfg = load_config(agent={"autonomy": "chat"}, embedder={"backend": "fake"},
                      vectordb={"mode": "memory"})
    app = create_app(cfg, llm=StreamLLM(), memory=FakeMemory())
    with TestClient(app) as c:
        assert type(app.state.agent).__name__ == "MemoryAgent"
        r = c.post("/chat/stream", json={"message": "你好", "session_id": "s"})
        assert r.status_code == 200
        assert r.headers["content-type"].startswith("text/event-stream")
        assert r.headers.get("x-accel-buffering") == "no"          # 反代不缓冲(逐 token 生效)
        evs = _parse_sse(r.text)
        assert evs[0]["type"] == "meta"
        assert [e["text"] for e in evs if e["type"] == "token"] == ["甲", "乙", "丙"]
        assert evs[-1]["type"] == "done"


def test_endpoint_fallback_for_tool_agent():
    """tools 档(ToolAgent 无 chat_stream)→ 端点整段一次性给出(meta+token+done)。"""
    from fastapi.testclient import TestClient

    from services.api import create_app
    from core.tools import AssistantTurn

    class ToolLLM:
        async def chat_tools(self, messages, tools, **kw):
            return AssistantTurn(content="工具档最终答复")

    cfg = load_config(agent={"autonomy": "tools"}, embedder={"backend": "fake"},
                      vectordb={"mode": "memory"})
    app = create_app(cfg, llm=ToolLLM(), memory=FakeMemory())
    with TestClient(app) as c:
        assert type(app.state.agent).__name__ == "ToolAgent"
        r = c.post("/chat/stream", json={"message": "q", "session_id": "s"})
        evs = _parse_sse(r.text)
        assert [e["type"] for e in evs] == ["meta", "token", "done"]
        assert evs[1]["text"] == "工具档最终答复"


def test_endpoint_emits_error_event_on_failure():
    from fastapi.testclient import TestClient

    from services.api import create_app

    class BoomLLM:
        async def chat_stream(self, messages, **kw):
            raise RuntimeError("kaboom")
            yield  # pragma: no cover - 使其成为异步生成器

    cfg = load_config(agent={"autonomy": "chat"}, embedder={"backend": "fake"},
                      vectordb={"mode": "memory"})
    app = create_app(cfg, llm=BoomLLM(), memory=FakeMemory())
    with TestClient(app) as c:
        r = c.post("/chat/stream", json={"message": "q", "session_id": "s"})
        evs = _parse_sse(r.text)
        assert any(e["type"] == "error" and "kaboom" in e["message"] for e in evs)


async def test_adapter_stream_duplicate_usage_blocks_billed_once(tmp_path):
    """回归:部分 OpenAI 兼容网关在同一流里重复回传 usage 块。

    修复前:每个 usage 块都 ledger.record → 同一笔 token 被计多次(双重记账,
    日预算被虚耗)。修复后:同一流只按首个 usage 块记账一次;last_meta 仍取末块。
    """
    from adapters.cost_ledger import CostLedger
    from adapters.llm import OpenAICompatAdapter

    usage1 = {"prompt_tokens": 10, "completion_tokens": 4}
    usage2 = {"prompt_tokens": 10, "completion_tokens": 4}   # 网关重复回传同一累计值
    body = (b'data: {"choices":[{"delta":{"content":"a"}}]}\n\n'
            + f'data: {json.dumps({"choices": [], "usage": usage1})}\n\n'.encode()
            + f'data: {json.dumps({"choices": [], "usage": usage2})}\n\n'.encode()
            + b'data: [DONE]\n\n')

    ledger = CostLedger({}, 100.0, tmp_path / "ledger.json")
    ad = OpenAICompatAdapter(
        LLMRoleSettings(base_url="http://x/v1", api_key="k", model="m", stream_usage=True),
        ledger=ledger, transport=httpx.MockTransport(lambda r: httpx.Response(200, content=body)))
    pieces = [p async for p in ad.chat_stream([Message(role="user", content="hi")])]

    assert pieces == ["a"]
    entry = ledger.snapshot()["entries"]["http://x/v1::m"]
    assert entry["prompt_tokens"] == 10        # 记账一次,而非 20
    assert entry["completion_tokens"] == 4     # 而非 8
    assert ad.last_meta["usage"] == usage2     # 元数据取末块(累计终值)
