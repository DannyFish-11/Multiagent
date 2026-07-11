"""L3 Memory-Agent FastAPI 服务(:8002,BUILD_SPEC M3)。

路由:/chat /memory/add /memory/search /memory/consolidate /healthz。
启动时对 L0/L1/L2 做依赖健康检查,任何一层不可达立即失败并指明层号。
"""

from __future__ import annotations

import contextlib
import logging

from fastapi import FastAPI, Request
from fastapi.responses import HTMLResponse, JSONResponse

from adapters.a2a import build_signed_card
from adapters.embedder import RemoteEmbedderAdapter, build_embedder
from adapters.llm import build_ledger, build_llm_client
from core.agent import MemoryAgent
from core.identity import AgentIdentity
from core.metabolism import RetrievalLogger
from core.config import AppConfig, load_config
from core.errors import LayerError
from core.factory import build_memory_store
from core.schemas import (
    ChatRequest,
    ChatResponse,
    ConsolidationReport,
    FeedbackRequest,
    HealthReport,
    MemoryAddRequest,
    MemoryAddResponse,
    MemoryHit,
    MemorySearchRequest,
    MultimodalInput,
    PromoteRequest,
)

logger = logging.getLogger(__name__)


def create_app(
    config: AppConfig | None = None,
    llm=None,
    memory=None,
    skip_dependency_checks: bool = False,
) -> FastAPI:
    cfg = config or load_config()

    @contextlib.asynccontextmanager
    async def lifespan(app: FastAPI):
        _ledger = build_ledger(cfg)
        _llm = llm or build_llm_client(cfg, role="chat", ledger=_ledger)
        if cfg.embedder.backend == "remote":
            _embedder = RemoteEmbedderAdapter(cfg.embedder.base_url, cfg.embedder.effective_dim)
        else:
            _embedder = build_embedder(cfg.embedder, ledger=_ledger)

        # fail-fast 依赖健康检查(BUILD_SPEC §0.2-5)
        if not skip_dependency_checks:
            if hasattr(_llm, "health"):
                await _llm.health()
            if hasattr(_embedder, "health"):
                await _embedder.health()

        _memory = memory if memory is not None else build_memory_store(cfg, embedder=_embedder, llm=_llm)
        app.state.embedder = _embedder
        app.state.ledger = _ledger
        app.state.llm = _llm
        app.state.memory = _memory
        # M5 身份(终身不变;记忆库随 agent_id 绑定)
        app.state.identity = AgentIdentity.load_or_create(cfg.identity.dir)
        app.state.signed_card = build_signed_card(app.state.identity, cfg.a2a.base_url)
        # M8 埋点
        app.state.retrieval_logger = RetrievalLogger(cfg.metabolism.events_path)
        # M9 审批中枢(无 Omnigent 形态的危险动作守门人;工具循环的每次工具调用经此闸)
        from core.approval import ApprovalQueue, Notifier
        from core.audit import AuditLog

        app.state.audit = AuditLog(cfg.approval.audit_path)
        app.state.approvals = ApprovalQueue(
            cfg.approval, app.state.audit, Notifier(cfg.approval))
        # 装配 agent(需 function-calling 模型):autonomy=supervisor → 中心调度委派(M25);
        # swarm → 去中心化多成员(M24);tools → 会用工具的单 agent(M22);否则 → 回落记忆问答。
        _fc = hasattr(_llm, "chat_tools")
        if cfg.agent.autonomy == "supervisor" and _fc and cfg.supervisor.workers:
            from adapters.web import WebAdapter
            from core.supervisor import build_supervisor

            app.state.agent = build_supervisor(
                cfg, _llm, _memory, WebAdapter(cfg.web), approval=app.state.approvals)
        elif cfg.agent.autonomy == "swarm" and _fc and cfg.swarm.members:
            from adapters.web import WebAdapter
            from core.swarm import build_swarm

            app.state.agent = build_swarm(
                cfg, _llm, _memory, WebAdapter(cfg.web), approval=app.state.approvals)
        elif cfg.agent.autonomy == "tools" and _fc:
            from adapters.web import WebAdapter
            from core.tool_agent import ToolAgent
            from core.tools import build_toolbox

            app.state.agent = ToolAgent(
                _llm, _memory, cfg, approval=app.state.approvals,
                tools=build_toolbox(cfg, _memory, WebAdapter(cfg.web)))
        else:
            app.state.agent = MemoryAgent(_llm, _memory, cfg)
        if hasattr(app.state.agent, "set_retrieval_logger"):
            app.state.agent.set_retrieval_logger(app.state.retrieval_logger)
        # M29:可观测性开启时把 agent 编排层也包成发 span 的 TracedAgent(关闭原样返回,零开销);
        # 内部 LLM/嵌入/记忆调用已各自埋点,自动挂到 agent span 下形成执行树。
        from adapters.observability import instrument_agent
        app.state.agent = instrument_agent(app.state.agent, cfg)
        logger.info("L3 api ready: memory.backend=%s autonomy=%s agent_id=%s",
                    cfg.memory.backend, cfg.agent.autonomy, app.state.identity.agent_id)
        yield
        await app.state.agent.drain()

    app = FastAPI(title="memory-agent L3 api", lifespan=lifespan)

    @app.exception_handler(LayerError)
    async def layer_error_handler(_req: Request, exc: LayerError):
        return JSONResponse(status_code=502, content={"error": str(exc), "layer": exc.layer})

    @app.get("/healthz", response_model=HealthReport)
    async def healthz():
        """逐项报告三依赖连通状态:LLM 端点(L0)/ 嵌入端点(L1)/ Qdrant(L2)。"""
        layers: dict[str, str] = {"L3": "ok"}
        status = "ok"

        async def _probe_embedder() -> None:
            if hasattr(app.state.embedder, "health"):
                await app.state.embedder.health()
            else:  # 本地/假后端:跑一次探针嵌入
                await app.state.embedder.embed([MultimodalInput.text("healthz probe")])

        async def _probe_qdrant() -> None:
            mem = app.state.memory
            db = getattr(mem, "_db", None)
            if db is not None and hasattr(db, "health"):
                await db.health()

        checks = (("L0", getattr(app.state.llm, "health", None)),
                  ("L1", _probe_embedder), ("L2", _probe_qdrant))
        for name, probe in checks:
            try:
                if probe is not None:
                    await probe()
                layers[name] = "ok"
            except LayerError as exc:
                layers[name] = str(exc)
                status = "degraded"
        return HealthReport(status=status, layers=layers)

    @app.get("/", response_class=HTMLResponse)
    @app.get("/ui", response_class=HTMLResponse)
    async def web_ui():
        """内置浏览器聊天界面(打开首页即对话,无需命令行)。"""
        from services.webui import CHAT_HTML

        return CHAT_HTML

    @app.get("/config")
    async def show_config():
        """当前生效配置(密钥自动脱敏);排障用,不泄露 key。"""
        from core.cli import _redact

        return _redact(cfg.model_dump())

    @app.get("/plugins")
    async def show_plugins():
        """列出所有已注册插件(内置 + 第三方 entry_points 发现)。"""
        import adapters.cloud  # noqa: F401 - 触发 cloud_provider 内置注册
        import core.experiment  # noqa: F401 - 触发 task_source 内置注册
        import core.harness  # noqa: F401 - 触发 profile 内置注册
        from core.plugins import REGISTRY

        return REGISTRY.snapshot()

    @app.post("/chat", response_model=ChatResponse)
    async def chat(req: ChatRequest):
        image = None
        if req.image_base64:
            image = MultimodalInput(type="image", content=req.image_base64, mime=req.image_mime)
        return await app.state.agent.chat(
            req.message, session_id=req.session_id, image=image, sync_memory_write=True,
        )

    @app.post("/chat/stream")
    async def chat_stream(req: ChatRequest):
        """流式对话(M26,SSE text/event-stream):事件 data: {type: meta|token|done|error}。
        chat 档(MemoryAgent)真逐 token;tools/swarm/supervisor 档整段一次性给出(仍可用)。"""
        import json as _json

        from fastapi.responses import StreamingResponse

        image = None
        if req.image_base64:
            image = MultimodalInput(type="image", content=req.image_base64, mime=req.image_mime)
        agent = app.state.agent

        async def _events():
            def _sse(ev: dict) -> str:
                return f"data: {_json.dumps(ev, ensure_ascii=False)}\n\n"
            try:
                if hasattr(agent, "chat_stream"):
                    async for ev in agent.chat_stream(req.message, session_id=req.session_id,
                                                      image=image):
                        yield _sse(ev)
                else:                                  # 工具/多 agent 档:整段一次性
                    resp = await agent.chat(req.message, session_id=req.session_id,
                                            image=image, sync_memory_write=True)
                    yield _sse({"type": "meta", "event_id": resp.event_id,
                                "memories_used": [h.model_dump() for h in resp.memories_used]})
                    yield _sse({"type": "token", "text": resp.reply})
                    yield _sse({"type": "done", "event_id": resp.event_id})
            except Exception as exc:                   # 流内出错:发 error 事件而非静默断流
                logger.exception("chat_stream 失败")
                yield _sse({"type": "error", "message": str(exc)})

        # 反缓冲头:否则 nginx 等反代默认 proxy_buffering on 会把整条流缓成一坨,
        # 直到 done 才吐,令逐 token 失效。X-Accel-Buffering=no 显式关 nginx 缓冲。
        return StreamingResponse(_events(), media_type="text/event-stream", headers={
            "Cache-Control": "no-cache", "Connection": "keep-alive", "X-Accel-Buffering": "no"})

    @app.post("/memory/add", response_model=MemoryAddResponse)
    async def memory_add(req: MemoryAddRequest):
        mem_id = await app.state.memory.add(req.input, req.meta)
        return MemoryAddResponse(ids=[mem_id])

    @app.post("/memory/search", response_model=list[MemoryHit])
    async def memory_search(req: MemorySearchRequest):
        return await app.state.memory.search(req.query, k=req.k)

    @app.post("/memory/consolidate", response_model=ConsolidationReport)
    async def memory_consolidate():
        return await app.state.memory.consolidate()

    # ---- PHASE 2 ----

    @app.get("/identity/card")
    async def identity_card():
        """签名 Agent Card(M5.2;A2AClientAdapter.fetch_and_verify_card 消费)。"""
        return app.state.signed_card

    @app.post("/memory/promote")
    async def memory_promote(req: PromoteRequest):
        """上交一条私有记忆到共享池(M5.3)。策略决策由调用方先行完成。"""
        if not hasattr(app.state.memory, "promote"):
            return JSONResponse(status_code=400, content={"error": "当前后端不支持共享池"})
        shared_id = await app.state.memory.promote(req.memory_id)
        return {"shared_id": shared_id}

    @app.post("/feedback")
    async def feedback(req: FeedbackRequest):
        """M8 用户显式反馈(👍/👎),写入检索事件日志供代谢实验回放。"""
        ok = app.state.retrieval_logger.set_feedback(
            req.event_id, req.feedback, req.adopted_memory_ids)
        return {"recorded": ok}

    # ---- PHASE 3 审批中枢(M9.2) ----

    @app.get("/approvals")
    async def list_approvals():
        """列出待批准动作队列。"""
        return {"pending": app.state.approvals.list_pending()}

    @app.post("/approvals/{approval_id}/approve")
    async def approve(approval_id: str):
        ok = await app.state.approvals.resolve(approval_id, approved=True)
        return JSONResponse(status_code=200 if ok else 404, content={"resolved": ok})

    @app.post("/approvals/{approval_id}/reject")
    async def reject(approval_id: str):
        ok = await app.state.approvals.resolve(approval_id, approved=False)
        return JSONResponse(status_code=200 if ok else 404, content={"resolved": ok})

    @app.get("/audit")
    async def audit_tail(limit: int = 50):
        """审计日志尾部(运维/验收用)。"""
        entries = app.state.audit.read_all()
        return {"entries": entries[-limit:], "total": len(entries)}

    return app


def main() -> None:
    import uvicorn

    cfg = load_config()
    uvicorn.run(create_app(cfg), host=cfg.services.api_host, port=cfg.services.api_port)


if __name__ == "__main__":
    main()
