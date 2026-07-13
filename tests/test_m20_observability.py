"""M20 B 验收:可观测性埋点(OTel)。

- 关闭时(默认):instrument_* 直通、零依赖零开销 → 保证"克隆即测试全绿"。
- 开启时(需 observability extra,CI 无则跳过):adapter 包装发出带
  model/token/耗时/检索命中 + session/experiment 维度的 span。
"""

from __future__ import annotations

import pytest

from adapters.observability import (
    TracedEmbedder,
    TracedLLM,
    TracedMemory,
    _reset_for_test,
    instrument_embedder,
    instrument_llm,
    instrument_memory,
    trace_context,
)
from adapters.embedder import FakeDeterministicEmbedder
from adapters.llm import EchoLLM
from core.config import load_config
from core.schemas import Message, MultimodalInput


# ---------------------------------------------------------------- 关闭=零开销零依赖

def test_disabled_is_noop_and_zero_dependency():
    """默认 observability.enabled=false:instrument_* 原样返回,不触碰任何 OTel 依赖。"""
    cfg = load_config()
    assert cfg.observability.enabled is False
    sentinel = object()
    assert instrument_llm(sentinel, cfg) is sentinel
    assert instrument_embedder(sentinel, cfg) is sentinel
    assert instrument_memory(sentinel, cfg) is sentinel


def test_factory_returns_unwrapped_when_disabled():
    """工厂在关闭态返回未包装对象(不是 Traced* 包装)。"""
    from adapters.llm import build_llm_client

    cfg = load_config(llm={"mode": "echo"})
    client = build_llm_client(cfg)
    assert isinstance(client, EchoLLM)
    assert not isinstance(client, TracedLLM)


# ---------------------------------------------------------------- 开启=发 span(需 extra)

def _tracer_and_exporter():
    from opentelemetry.sdk.trace import TracerProvider
    from opentelemetry.sdk.trace.export import SimpleSpanProcessor
    from opentelemetry.sdk.trace.export.in_memory_span_exporter import InMemorySpanExporter

    exporter = InMemorySpanExporter()
    provider = TracerProvider()
    provider.add_span_processor(SimpleSpanProcessor(exporter))
    return provider.get_tracer("test"), exporter


class _FakeAPILLM:
    """带 model + last_meta.usage 的假 LLM(模拟 api 模式 adapter,用于验证 token 上报)。"""
    model = "deepseek-chat"
    last_meta = {"usage": {"prompt_tokens": 12, "completion_tokens": 8}}

    async def chat(self, messages, **kw):
        return "回答"


class _FakeHit:
    def __init__(self, hid):
        self.id = hid


class _FakeMemory:
    async def search(self, query, k=5, **kw):
        return [_FakeHit("m1"), _FakeHit("m2")]

    async def add(self, input, meta=None, **kw):
        return ["new-id"]


async def test_llm_span_carries_model_tokens_and_context_tags():
    pytest.importorskip("opentelemetry.sdk.trace")
    cfg = load_config()
    tracer, exporter = _tracer_and_exporter()
    llm = TracedLLM(_FakeAPILLM(), tracer, cfg)

    with trace_context(session_id="sess-1", experiment_id="exp-9", agent_id="agent-A"):
        out = await llm.chat([
            Message(role="system", content="## 相关记忆\n- 我的猫叫 Benjamin"),
            Message(role="user", content="我的猫叫什么"),
        ])
    assert out == "回答"

    (span,) = exporter.get_finished_spans()
    a = span.attributes
    assert span.name == "llm.chat"
    assert a["gen_ai.operation.name"] == "chat"
    assert a["gen_ai.request.model"] == "deepseek-chat"
    assert a["gen_ai.usage.input_tokens"] == 12
    assert a["gen_ai.usage.output_tokens"] == 8
    # 维度标签(与 PHASE 4 实验记账对齐)
    assert a["langfuse.session.id"] == "sess-1"
    assert a["experiment.id"] == "exp-9"
    assert a["agent.id"] == "agent-A"
    assert "gen_ai.prompt.summary" in a and "gen_ai.completion.summary" in a
    assert "latency_ms" in a


def test_traced_llm_does_not_fake_capabilities():
    """回归:TracedLLM 曾把 chat_tools/chat_stream 定义成实例方法,使 hasattr(wrapper,
    'chat_tools') 恒真——即便包的是 EchoLLM(无工具能力)。api 装配以此判断 autonomy,
    会把非 function-calling 模型误判为可用 → 运行时 500。包装后能力探测须与底层一致。"""
    pytest.importorskip("opentelemetry.sdk.trace")
    cfg = load_config()
    tracer, _ = _tracer_and_exporter()

    # 底层无 chat_tools/chat_stream:包装后也不得凭空出现
    bare = TracedLLM(EchoLLM(), tracer, cfg)
    assert not hasattr(bare, "chat_tools")
    assert not hasattr(bare, "chat_stream")

    # 底层有 chat_tools:包装后暴露且可调用
    class _FCLLM:
        model = "fc"
        async def chat(self, messages, **kw):
            return "x"
        async def chat_tools(self, messages, tools, **kw):
            return {"content": "tool-turn", "tool_calls": []}

    fc = TracedLLM(_FCLLM(), tracer, cfg)
    assert hasattr(fc, "chat_tools")


async def test_embedder_and_memory_spans():
    pytest.importorskip("opentelemetry.sdk.trace")
    cfg = load_config()
    tracer, exporter = _tracer_and_exporter()

    emb = TracedEmbedder(FakeDeterministicEmbedder(64), tracer, cfg)
    vecs = await emb.embed([MultimodalInput.text("你好世界")])
    assert len(vecs) == 1 and len(vecs[0]) == 64

    mem = TracedMemory(_FakeMemory(), tracer, cfg)
    hits = await mem.search(MultimodalInput.text("查询"), k=3)
    ids = await mem.add(MultimodalInput.text("记住这条"), {"session_id": "s"})
    assert len(hits) == 2 and ids == ["new-id"]

    names = [s.name for s in exporter.get_finished_spans()]
    assert names == ["embedder.embed", "memory.search", "memory.add"]
    by_name = {s.name: s.attributes for s in exporter.get_finished_spans()}
    assert by_name["embedder.embed"]["embedder.input_count"] == 1
    assert by_name["embedder.embed"]["embedder.dim"] == 64
    assert by_name["memory.search"]["memory.k"] == 3
    assert by_name["memory.search"]["memory.hit_count"] == 2
    assert by_name["memory.search"]["memory.hit_ids"] == "m1,m2"
    assert by_name["memory.add"]["memory.result_ids"] == "new-id"


async def test_instrument_wraps_when_enabled():
    pytest.importorskip("opentelemetry.sdk.trace")
    _reset_for_test()
    cfg = load_config(observability={"enabled": True,
                                     "otlp_endpoint": "http://localhost:3000/api/public/otel/v1/traces"})
    wrapped = instrument_llm(EchoLLM(), cfg)
    assert isinstance(wrapped, TracedLLM)
    # 透传:未显式包装的属性/方法仍可访问
    assert hasattr(wrapped, "health")
    _reset_for_test()
