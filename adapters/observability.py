"""M20 B:可观测性适配层(Langfuse via OpenTelemetry)。

红线:第三方(OTel / Langfuse)接触点全部收敛在本文件;core/services 不改签名。
业务代码零侵入——通过 instrument_* 在 adapter 工厂处**包裹**已装配对象,
enabled=false 时原样返回(零开销、零依赖),enabled=true 时返回发 span 的包装。

标准 OpenTelemetry 接入:span 用 GenAI 语义约定(gen_ai.*),Langfuse 原生理解
model/token 并自算成本;session/experiment/agent 维度经 trace_context 打标,
与 PHASE 4 实验记账对齐。未来换 Laminar 等 OTLP 后端只改端点,无需动业务码。

依赖为可选 extra(uv sync --extra observability);enabled=true 却未装依赖时
**fail-fast** 指明安装方式(不静默降级,符合全项目 L 层错误纪律)。
"""

from __future__ import annotations

import base64
import contextlib
import contextvars
import logging
import time
from typing import Any

from core.errors import LayerError

logger = logging.getLogger(__name__)

# 会话/实验维度标签(contextvar:跨 await 传播,零侵入)。
# 入口(如 /chat 处理器、ExperimentRunner)可用 trace_context(...) 打标;
# 未打标时相应属性省略,不影响 adapter 级 model/token/耗时/命中等埋点。
_trace_ctx: contextvars.ContextVar[dict[str, str]] = contextvars.ContextVar(
    "trace_ctx", default={})

_TRACER: Any = None
_INITED: bool = False


@contextlib.contextmanager
def trace_context(**tags: str):
    """在此上下文内产生的 span 都带上给定维度标签(session_id/experiment_id/agent_id)。"""
    clean = {k: str(v) for k, v in tags.items() if v}
    merged = {**_trace_ctx.get(), **clean}
    token = _trace_ctx.set(merged)
    try:
        yield
    finally:
        _trace_ctx.reset(token)


def _current_tags() -> dict[str, str]:
    return dict(_trace_ctx.get())


def init_tracing(cfg) -> Any:
    """装配全局 TracerProvider + OTLP exporter(幂等)。返回 tracer;禁用返回 None。

    enabled=true 但缺 OTel 依赖 → LayerError(fail-fast,指明 extra)。
    """
    global _TRACER, _INITED
    if not cfg.observability.enabled:
        return None
    if _INITED:
        return _TRACER

    try:
        from opentelemetry import trace
        from opentelemetry.exporter.otlp.proto.http.trace_exporter import OTLPSpanExporter
        from opentelemetry.sdk.resources import Resource
        from opentelemetry.sdk.trace import TracerProvider
        from opentelemetry.sdk.trace.export import BatchSpanProcessor
    except ImportError as exc:
        raise LayerError(
            "L-obs", "observability",
            "observability.enabled=true 需安装可观测性依赖:uv sync --extra observability",
        ) from exc

    obs = cfg.observability
    headers = {}
    if obs.public_key and obs.secret_key:
        auth = base64.b64encode(f"{obs.public_key}:{obs.secret_key}".encode()).decode()
        headers["Authorization"] = f"Basic {auth}"

    resource = Resource.create({"service.name": obs.service_name})
    provider = TracerProvider(resource=resource)
    provider.add_span_processor(BatchSpanProcessor(
        OTLPSpanExporter(endpoint=obs.otlp_endpoint, headers=headers)))
    # 只设一次全局 provider(避免测试/多次装配互相覆盖)
    if not isinstance(trace.get_tracer_provider(), TracerProvider):
        trace.set_tracer_provider(provider)
    _TRACER = trace.get_tracer("memory-agent")
    _INITED = True
    logger.info("observability on → OTLP %s (service=%s)", obs.otlp_endpoint, obs.service_name)
    return _TRACER


def _reset_for_test() -> None:
    """测试隔离用:清 tracer 缓存(不影响生产路径)。"""
    global _TRACER, _INITED
    _TRACER, _INITED = None, False


def _apply_tags(span, cfg) -> None:
    tags = _current_tags()
    if sid := tags.get("session_id"):
        span.set_attribute("langfuse.session.id", sid)
        span.set_attribute("session.id", sid)
    if eid := tags.get("experiment_id"):
        span.set_attribute("langfuse.trace.metadata.experiment_id", eid)
        span.set_attribute("experiment.id", eid)
    if aid := tags.get("agent_id"):
        span.set_attribute("langfuse.trace.metadata.agent_id", aid)
        span.set_attribute("agent.id", aid)


def _summary(text: Any, cfg) -> str:
    s = text if isinstance(text, str) else str(text)
    n = cfg.observability.io_summary_max_chars
    return s[:n]


# ------------------------------------------------------------------ 包装器

class _TracedBase:
    """__getattr__ 透传:保持底层对象的完整协议(health/model/aclose/…)。"""

    def __init__(self, inner, tracer, cfg) -> None:
        self._inner = inner
        self._tracer = tracer
        self._cfg = cfg

    def __getattr__(self, name):  # 未显式包装的方法/属性一律透传
        return getattr(self._inner, name)


class TracedLLM(_TracedBase):
    async def chat(self, messages, **kw) -> str:
        with self._tracer.start_as_current_span("llm.chat") as span:
            _apply_tags(span, self._cfg)
            model = getattr(self._inner, "model", "") or ""
            span.set_attribute("gen_ai.system", "openai")
            span.set_attribute("gen_ai.operation.name", "chat")
            if model:
                span.set_attribute("gen_ai.request.model", str(model))
            span.set_attribute("gen_ai.prompt.summary", _summary(messages, self._cfg))
            t0 = time.perf_counter()
            try:
                out = await self._inner.chat(messages, **kw)
            except Exception as exc:
                span.set_attribute("error", True)
                span.set_attribute("error.message", str(exc)[:512])
                raise
            span.set_attribute("latency_ms", round((time.perf_counter() - t0) * 1000, 3))
            span.set_attribute("gen_ai.completion.summary", _summary(out, self._cfg))
            usage = (getattr(self._inner, "last_meta", {}) or {}).get("usage") or {}
            if usage:
                span.set_attribute("gen_ai.usage.input_tokens", int(usage.get("prompt_tokens", 0)))
                span.set_attribute("gen_ai.usage.output_tokens",
                                   int(usage.get("completion_tokens", 0)))
            return out


class TracedEmbedder(_TracedBase):
    @property
    def dim(self) -> int:
        return self._inner.dim

    async def embed(self, inputs):
        with self._tracer.start_as_current_span("embedder.embed") as span:
            _apply_tags(span, self._cfg)
            span.set_attribute("embedder.input_count", len(inputs))
            span.set_attribute("embedder.dim", int(getattr(self._inner, "dim", 0) or 0))
            t0 = time.perf_counter()
            try:
                out = await self._inner.embed(inputs)
            except Exception as exc:
                span.set_attribute("error", True)
                span.set_attribute("error.message", str(exc)[:512])
                raise
            span.set_attribute("latency_ms", round((time.perf_counter() - t0) * 1000, 3))
            return out


class TracedMemory(_TracedBase):
    async def search(self, query, k: int = 5, **kw):
        with self._tracer.start_as_current_span("memory.search") as span:
            _apply_tags(span, self._cfg)
            span.set_attribute("memory.k", int(k))
            span.set_attribute("memory.query.summary", _summary(query, self._cfg))
            t0 = time.perf_counter()
            try:
                hits = await self._inner.search(query, k=k, **kw)
            except Exception as exc:
                span.set_attribute("error", True)
                span.set_attribute("error.message", str(exc)[:512])
                raise
            span.set_attribute("latency_ms", round((time.perf_counter() - t0) * 1000, 3))
            span.set_attribute("memory.hit_count", len(hits))
            span.set_attribute("memory.hit_ids",
                               ",".join(str(getattr(h, "id", "")) for h in hits))
            return hits

    async def add(self, input, meta=None, **kw):  # noqa: A002 - 对齐底层签名
        with self._tracer.start_as_current_span("memory.add") as span:
            _apply_tags(span, self._cfg)
            span.set_attribute("memory.input_type", str(getattr(input, "type", "")))
            t0 = time.perf_counter()
            try:
                ids = await self._inner.add(input, meta, **kw)
            except Exception as exc:
                span.set_attribute("error", True)
                span.set_attribute("error.message", str(exc)[:512])
                raise
            span.set_attribute("latency_ms", round((time.perf_counter() - t0) * 1000, 3))
            span.set_attribute("memory.result_ids", ",".join(map(str, ids or [])))
            return ids


# ------------------------------------------------------------------ 装配入口

def instrument_llm(obj, cfg):
    """enabled=false → 原样返回(零开销);enabled=true → 包裹为发 span 的 TracedLLM。"""
    if not cfg.observability.enabled:
        return obj
    tracer = init_tracing(cfg)
    return TracedLLM(obj, tracer, cfg) if tracer is not None else obj


def instrument_embedder(obj, cfg):
    if not cfg.observability.enabled:
        return obj
    tracer = init_tracing(cfg)
    return TracedEmbedder(obj, tracer, cfg) if tracer is not None else obj


def instrument_memory(obj, cfg):
    if not cfg.observability.enabled:
        return obj
    tracer = init_tracing(cfg)
    return TracedMemory(obj, tracer, cfg) if tracer is not None else obj
