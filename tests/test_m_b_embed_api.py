"""PHASE2.5 M-B 验收:嵌入 API 化(全部 mock)+ 维度守卫。"""

from __future__ import annotations

import json

import httpx
import pytest

from adapters.cost_ledger import CostLedger
from adapters.embedder import JinaAPIAdapter, UnsupportedModality
from adapters.vectordb import QdrantAdapter
from core.config import EmbedderSettings, VectorDBSettings
from core.errors import LayerError
from core.schemas import MultimodalInput
from tests.conftest import FIXTURES


def settings(dim=8, batch=3, retries=0, audio=False):
    return EmbedderSettings(
        backend="jina_api", model_name="jinaai/jina-embeddings-v5-omni-small",
        dim=dim, jina_api_key="test-key",
        api_batch_size=batch, api_max_retries=retries,
        api_retry_backoff_s=0.0, api_supports_audio=audio,
    )


def embed_response(n: int, dim: int = 8, tokens: int = 7) -> httpx.Response:
    return httpx.Response(200, json={
        "data": [{"embedding": [1.0] + [0.0] * (dim - 1), "index": i} for i in range(n)],
        "usage": {"total_tokens": tokens},
    })


# ---------------------------------------------------------------- 批量合并/拆分

async def test_batching_splits_by_limit():
    """7 条输入、批量上限 3 → 3 次请求(3+3+1),结果顺序完整。"""
    batches: list[int] = []

    def handler(request: httpx.Request) -> httpx.Response:
        body = json.loads(request.content)
        assert body["model"] == "jina-embeddings-v5-omni-small"  # API 模型名去掉 org 前缀
        batches.append(len(body["input"]))
        return embed_response(len(body["input"]))

    adapter = JinaAPIAdapter(settings(batch=3), transport=httpx.MockTransport(handler))
    vectors = await adapter.embed([MultimodalInput.text(f"t{i}") for i in range(7)])
    assert batches == [3, 3, 1]
    assert len(vectors) == 7 and all(len(v) == 8 for v in vectors)


# ---------------------------------------------------------------- 模态能力

async def test_audio_raises_unsupported_modality():
    """API 版暂不支持 audio:显式抛 UnsupportedModality,不静默降级。"""
    adapter = JinaAPIAdapter(settings(audio=False),
                             transport=httpx.MockTransport(lambda r: embed_response(1)))
    audio = MultimodalInput.from_file(FIXTURES / "tone_440hz.wav", "audio", "audio/wav")
    with pytest.raises(UnsupportedModality) as exc:
        await adapter.embed([audio])
    assert exc.value.modality == "audio" and exc.value.layer == "L1"


async def test_image_supported():
    def handler(request: httpx.Request) -> httpx.Response:
        body = json.loads(request.content)
        assert "image" in body["input"][0]
        return embed_response(1)

    adapter = JinaAPIAdapter(settings(), transport=httpx.MockTransport(handler))
    img = MultimodalInput.from_file(FIXTURES / "white_cat.png", "image", "image/png")
    assert len((await adapter.embed([img]))[0]) == 8


# ---------------------------------------------------------------- 重试与记账

async def test_retry_and_ledger(tmp_path):
    calls = {"n": 0}

    def handler(_r: httpx.Request) -> httpx.Response:
        calls["n"] += 1
        if calls["n"] == 1:
            return httpx.Response(429, text="rate limited")
        return embed_response(2, tokens=42)

    ledger = CostLedger({"jina-embeddings-v5-omni-small": {"input": 0.1}}, 10.0,
                        tmp_path / "ledger.json")
    adapter = JinaAPIAdapter(settings(retries=2), ledger=ledger,
                             transport=httpx.MockTransport(handler))
    await adapter.embed([MultimodalInput.text("a"), MultimodalInput.text("b")])
    assert calls["n"] == 2
    snap = ledger.snapshot()
    key = next(iter(snap["entries"]))
    assert snap["entries"][key]["prompt_tokens"] == 42


async def test_budget_gate_blocks_embedding(tmp_path):
    ledger = CostLedger({}, 0.0, tmp_path / "ledger.json")  # 预算为 0
    adapter = JinaAPIAdapter(settings(), ledger=ledger,
                             transport=httpx.MockTransport(lambda r: embed_response(1)))
    with pytest.raises(LayerError) as exc:
        await adapter.embed([MultimodalInput.text("a")])
    assert "日预算" in str(exc.value)


async def test_dim_mismatch_rejected_with_guidance():
    """验收⑦(adapter 侧):错误维度的嵌入响应被拦截且提示重算流程。"""
    adapter = JinaAPIAdapter(settings(dim=16),
                             transport=httpx.MockTransport(lambda r: embed_response(1, dim=8)))
    with pytest.raises(LayerError) as exc:
        await adapter.embed([MultimodalInput.text("a")])
    assert "维度" in str(exc.value) and "export" in str(exc.value)


# ---------------------------------------------------------------- Qdrant 维度守卫

async def test_qdrant_dimension_guard_points_to_m7(tmp_path):
    """验收⑦(存储侧):已有 collection 维度与新嵌入维度不一致 → 启动被拦,
    指引走 M7 export→import 重算,禁止静默建新库。"""
    vdb_cfg = VectorDBSettings(mode="local", path=str(tmp_path / "q"), collection="memories")
    db64 = QdrantAdapter(vdb_cfg, dim=64)
    await db64.ensure_collection()

    db32 = QdrantAdapter(vdb_cfg, dim=32, share_client_from=db64)
    with pytest.raises(LayerError) as exc:
        await db32.ensure_collection()
    msg = str(exc.value)
    assert "维度不一致" in msg and "memorypack export" in msg and "import" in msg
