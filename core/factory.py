"""按配置装配各层的工厂。FastAPI 服务与 MCP server 通过它复用同一 MemoryStore 实例。"""

from __future__ import annotations

from adapters.embedder import Embedder, build_embedder
from adapters.llm import LLMClient, build_ledger, build_llm_client
from adapters.memory import MemoryStore, QdrantMemoryStore, SimpleMemAdapter
from adapters.vectordb import QdrantAdapter
from core.agent import MemoryAgent
from core.config import AppConfig, load_config
from core.plugins import get_plugin, register

_singletons: dict[str, object] = {}


def get_config() -> AppConfig:
    if "config" not in _singletons:
        _singletons["config"] = load_config()
    return _singletons["config"]  # type: ignore[return-value]


def get_ledger(config: AppConfig):
    """CostLedger 进程级单例(LLM 与嵌入 API 共用一本账)。"""
    if "ledger" not in _singletons:
        _singletons["ledger"] = build_ledger(config)
    return _singletons["ledger"]


def build_llm(config: AppConfig, role: str = "chat") -> LLMClient:
    """选型逻辑在 adapters.llm.build_llm_client(mode=local|api,双角色);
    外层套并发信号量(M9.1 防 API 限流雪崩)。"""
    from adapters.llm import ConcurrencyLimitedLLM

    inner = build_llm_client(config, role=role, ledger=get_ledger(config))
    return ConcurrencyLimitedLLM(inner, config.concurrency.max_concurrent_llm_calls)


# ---------------------------------------------------------------- 记忆后端插件(M21)

@register("memory", "simplemem")
def _mem_simplemem(config, embedder=None, llm=None):
    return SimpleMemAdapter(config)


@register("memory", "qdrant")
def _mem_qdrant(config, embedder=None, llm=None):
    db = QdrantAdapter(config.vectordb, dim=config.embedder.effective_dim)
    shared_db = QdrantAdapter(
        config.vectordb, dim=config.embedder.effective_dim,
        collection=config.memory.shared_collection, share_client_from=db,
    )
    return QdrantMemoryStore(embedder, llm, db, config, shared_db=shared_db)


def build_memory_store(config: AppConfig, embedder: Embedder | None = None,
                       llm: LLMClient | None = None) -> MemoryStore:
    """按 config.memory.backend 从插件表取记忆后端(qdrant/simplemem/第三方)。

    qdrant 需要嵌入器 + 记忆抽取 LLM(未显式传入则在此按 config 装配)。
    """
    backend = config.memory.backend
    if backend == "qdrant":
        embedder = embedder or build_embedder(config.embedder, ledger=get_ledger(config))
        llm = llm or build_llm(config, role="memory")
    return get_plugin("memory", backend)(config, embedder, llm)


def get_shared_memory_store(config: AppConfig | None = None) -> MemoryStore:
    """进程级单例:API 与 MCP server 同进程时复用同一实例;跨进程时经由同一
    Qdrant collection / SimpleMem data_dir 共享状态。"""
    if "memory_store" not in _singletons:
        _singletons["memory_store"] = build_memory_store(config or get_config())
    return _singletons["memory_store"]  # type: ignore[return-value]


def build_agent(config: AppConfig, llm: LLMClient | None = None,
                memory: MemoryStore | None = None) -> MemoryAgent:
    llm = llm or build_llm(config)
    memory = memory or get_shared_memory_store(config)
    return MemoryAgent(llm, memory, config)


def get_identity(config: AppConfig | None = None):
    """进程级单例身份(M5)。身份终身不变;换脑不换身份。"""
    if "identity" not in _singletons:
        from core.identity import AgentIdentity

        cfg = config or get_config()
        _singletons["identity"] = AgentIdentity.load_or_create(cfg.identity.dir)
    return _singletons["identity"]
