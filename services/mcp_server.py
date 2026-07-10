"""MCP Server(官方 mcp Python SDK,stdio 传输)。

工具:memory_store / memory_search / memory_consolidate / memory_promote。
自 M5 起,所有工具响应均为身份签名信封(BUILD_SPEC PHASE2 5.1):
  {"payload": <结果>, "identity": {"agent_id", "public_key"}, "protected", "signature"}
验签用 core.identity.verify_envelope。

与 FastAPI 服务通过 core.factory.get_shared_memory_store 复用同一 MemoryStore:
同进程时为同一实例;独立进程时经同一 Qdrant collection(或 SimpleMem data_dir)
共享持久状态。

Omnigent(L4)通过 tools/mcp/memory.yaml 以 stdio 方式拉起本服务。
"""

from __future__ import annotations

import json
from typing import Any

from mcp.server.fastmcp import FastMCP

from core.factory import get_config, get_identity, get_shared_memory_store
from core.schemas import Modality, MultimodalInput

mcp = FastMCP("memory-agent")


def _signed(payload: Any) -> str:
    return json.dumps(get_identity().signed_envelope(payload), ensure_ascii=False)


@mcp.tool()
async def memory_store(content: str, modality: Modality = "text", meta: dict[str, Any] | None = None) -> str:
    """存入一条记忆。modality=text 时 content 为原文;image/audio 时为 base64。
    meta.visibility 可为 private(默认)/shared。

    返回签名信封 JSON,payload 为 {"id": "..."}。
    """
    store = get_shared_memory_store()
    mem_id = await store.add(MultimodalInput(type=modality, content=content), meta or {})
    return _signed({"id": mem_id})


@mcp.tool()
async def memory_search(query: str, k: int = 5) -> str:
    """按文本查询检索记忆。返回签名信封 JSON,payload 为命中数组
    (每项含 id/score/content/modality/meta)。"""
    store = get_shared_memory_store()
    hits = await store.search(MultimodalInput.text(query), k=k)
    return _signed([h.model_dump() for h in hits])


@mcp.tool()
async def memory_consolidate() -> str:
    """触发记忆整合(合并语义重复项)。返回签名信封 JSON,payload 为整合报告。"""
    store = get_shared_memory_store()
    report = await store.consolidate()
    return _signed(report.model_dump())


@mcp.tool()
async def memory_promote(memory_id: str) -> str:
    """把一条私有记忆上交到共享池(M5.3)。返回签名信封 JSON,payload 为
    {"shared_id": "..."}。上交决策应先经 PromotionPolicy(grader/manual)。"""
    from core.errors import LayerError

    store = get_shared_memory_store()
    if not hasattr(store, "promote"):
        raise LayerError("L2", "mcp", "当前 memory backend 不支持共享池上交")
    shared_id = await store.promote(memory_id)
    return _signed({"shared_id": shared_id})


def main() -> None:
    get_config()  # 提前加载配置,配置错误在启动时即暴露(fail-fast)
    get_identity()  # 身份加载失败也应在启动时暴露
    mcp.run(transport="stdio")


if __name__ == "__main__":
    main()
