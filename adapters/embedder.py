"""L1 适配层:Embedder 协议 + 各嵌入后端。

后端:
- JinaV5OmniAdapter  : 本地加载 jinaai/jina-embeddings-v5-omni-{small|nano}(目标机器)
- RemoteEmbedderAdapter: HTTP 调本项目 L1 服务 POST /embed(L3 使用)
- JinaAPIAdapter     : Jina 云 API 回退(需人类提供 key —— BUILD_SPEC M2 回退方案)
- FakeDeterministicEmbedder: 确定性哈希嵌入,仅测试/CI;必须在 config 显式选择,
  绝不自动回退(BUILD_SPEC 禁止静默替换指定模型)。
"""

from __future__ import annotations

import hashlib
import math
from typing import Protocol, runtime_checkable

import httpx

from core.config import EmbedderSettings
from core.errors import LayerError
from core.plugins import get_plugin, register
from core.schemas import MultimodalInput


@runtime_checkable
class Embedder(Protocol):
    async def embed(self, inputs: list[MultimodalInput]) -> list[list[float]]: ...

    @property
    def dim(self) -> int: ...


class UnsupportedModality(LayerError):
    """API 后端不支持的模态:显式拒绝而非静默降级(PHASE2.5 M-B)。"""

    def __init__(self, modality: str, backend: str) -> None:
        super().__init__("L1", backend,
                         f"该后端暂不支持 {modality} 模态;能力差异见 README(不做静默降级)")
        self.modality = modality


def truncate_and_normalize(vec: list[float], target_dim: int | None) -> list[float]:
    """Matryoshka 截断到 target_dim 后 L2 归一化;target_dim 为 None 时仅归一化。"""
    if target_dim is not None:
        if target_dim > len(vec):
            raise LayerError("L1", "embedder", f"matryoshka_dim={target_dim} 大于模型输出维度 {len(vec)}")
        vec = vec[:target_dim]
    norm = math.sqrt(sum(x * x for x in vec))
    if norm == 0:
        return vec
    return [x / norm for x in vec]


class FakeDeterministicEmbedder:
    """字符 3-gram 哈希投影。同一输入永远得到同一向量;词面重叠的文本向量相近。

    仅用于测试本项目自身的存储/检索/编排逻辑,不具备语义/跨模态能力。
    """

    def __init__(self, dim: int = 64) -> None:
        self._dim = dim

    @property
    def dim(self) -> int:
        return self._dim

    def _embed_bytes(self, data: bytes) -> list[float]:
        vec = [0.0] * self._dim
        n = 3
        if len(data) < n:
            data = data + b"\x00" * (n - len(data))
        for i in range(len(data) - n + 1):
            gram = data[i : i + n]
            h = int.from_bytes(hashlib.blake2b(gram, digest_size=8).digest(), "big")
            vec[h % self._dim] += 1.0 if (h >> 63) & 1 else -1.0  # signed hashing
        return truncate_and_normalize(vec, None)

    async def embed(self, inputs: list[MultimodalInput]) -> list[list[float]]:
        return [self._embed_bytes(item.raw_bytes()) for item in inputs]


class JinaV5OmniAdapter:
    """本地加载 jina-embeddings-v5-omni(retrieval 任务适配器)。

    实际模型 API 以 HuggingFace 仓库 README/源码为准;加载与编码的全部第三方
    接触点收敛在此类。无 GPU 时可用 CPU(nano 档),device 由 config 控制。
    """

    def __init__(self, settings: EmbedderSettings) -> None:
        self._settings = settings
        self._dim = settings.effective_dim
        self._model = None

    @property
    def dim(self) -> int:
        return self._dim

    def _load(self):
        if self._model is not None:
            return self._model
        try:
            import torch
            from transformers import AutoModel
        except ImportError as exc:
            raise LayerError(
                "L1", "jina-v5-omni",
                "缺少本地嵌入依赖,安装 extras: uv sync --extra local-embed",
            ) from exc
        device = self._settings.device
        if device == "auto":
            device = "cuda" if torch.cuda.is_available() else "cpu"
        try:
            self._model = AutoModel.from_pretrained(
                self._settings.model_name, trust_remote_code=True
            ).to(device).eval()
        except Exception as exc:
            raise LayerError(
                "L1", "jina-v5-omni",
                f"加载 {self._settings.model_name} 失败(检查 HF 可达性/模型名/显存): {exc}",
            ) from exc
        return self._model

    def _encode(self, item: MultimodalInput) -> list[float]:
        import io

        model = self._load()
        # jina v5-omni 系列暴露按模态的 encode_* 方法(以模型仓库实际代码为准,
        # 此处按能力探测调用,探测不到即报 L1 错误而非静默降级)。
        if item.type == "text":
            for name in ("encode_text", "encode"):
                fn = getattr(model, name, None)
                if fn is not None:
                    out = fn([item.content], task="retrieval")
                    return list(map(float, out[0]))
        elif item.type == "image":
            from PIL import Image

            img = Image.open(io.BytesIO(item.raw_bytes())).convert("RGB")
            fn = getattr(model, "encode_image", None)
            if fn is not None:
                out = fn([img], task="retrieval")
                return list(map(float, out[0]))
        elif item.type == "audio":
            fn = getattr(model, "encode_audio", None)
            if fn is not None:
                out = fn([item.raw_bytes()], task="retrieval")
                return list(map(float, out[0]))
        raise LayerError(
            "L1", "jina-v5-omni",
            f"模型 {self._settings.model_name} 未暴露 {item.type} 模态的 encode 方法,"
            "请核对模型仓库实际 API 并更新本适配器",
        )

    async def embed(self, inputs: list[MultimodalInput]) -> list[list[float]]:
        import asyncio

        def _run() -> list[list[float]]:
            return [
                truncate_and_normalize(self._encode(i), self._settings.matryoshka_dim)
                for i in inputs
            ]

        return await asyncio.to_thread(_run)


class RemoteEmbedderAdapter:
    """HTTP 调用本项目 L1 嵌入服务(POST /embed)。L3 通过它复用 L1。"""

    def __init__(self, base_url: str, dim: int, timeout_s: float = 60.0,
                 transport: httpx.AsyncBaseTransport | None = None) -> None:
        self.base_url = base_url.rstrip("/")
        self._dim = dim
        self._client = httpx.AsyncClient(base_url=self.base_url, timeout=timeout_s, transport=transport)

    @property
    def dim(self) -> int:
        return self._dim

    async def embed(self, inputs: list[MultimodalInput]) -> list[list[float]]:
        try:
            resp = await self._client.post(
                "/embed", json={"inputs": [i.model_dump() for i in inputs]}
            )
        except httpx.HTTPError as exc:
            raise LayerError("L1", "embed-service", f"嵌入服务不可达 {self.base_url}: {exc}") from exc
        if resp.status_code != 200:
            raise LayerError("L1", "embed-service", f"HTTP {resp.status_code}: {resp.text[:500]}")
        data = resp.json()
        if data.get("dim") != self._dim:
            raise LayerError(
                "L1", "embed-service",
                f"维度不一致:服务返回 {data.get('dim')},config 期望 {self._dim}",
            )
        return data["vectors"]

    async def health(self) -> bool:
        try:
            resp = await self._client.get("/healthz")
        except httpx.HTTPError as exc:
            raise LayerError("L1", "embed-service", f"健康检查失败 {self.base_url}: {exc}") from exc
        if resp.status_code != 200:
            raise LayerError("L1", "embed-service", f"健康检查 HTTP {resp.status_code}")
        return True

    async def aclose(self) -> None:
        await self._client.aclose()


class JinaAPIAdapter:
    """Jina 云 API 回退(BUILD_SPEC M2:本地加载失败超过 2 次迭代时启用,key 须由人类提供)。"""

    API_URL = "https://api.jina.ai/v1/embeddings"

    def __init__(self, settings: EmbedderSettings, ledger=None,
                 transport: httpx.AsyncBaseTransport | None = None) -> None:
        if not settings.jina_api_key:
            raise LayerError(
                "L1", "jina-api",
                "backend=jina_api 需要 MEMORY_AGENT_EMBEDDER__JINA_API_KEY(停点:向人类索取)",
            )
        self._settings = settings
        self._dim = settings.effective_dim
        self._ledger = ledger  # 用量并入 CostLedger(M-A/M-B 共用)
        self._client = httpx.AsyncClient(
            headers={"Authorization": f"Bearer {settings.jina_api_key}"},
            timeout=60.0, transport=transport,
        )

    @property
    def dim(self) -> int:
        return self._dim

    def _to_api_input(self, item: MultimodalInput) -> dict:
        if item.type == "text":
            return {"text": item.content}
        if item.type == "image":
            return {"image": item.content}
        # audio/video:API 版能力以 Jina 文档为准;不支持时显式拒绝,不静默降级
        if not self._settings.api_supports_audio:
            raise UnsupportedModality(item.type, "jina-api")
        return {"audio": item.content}

    async def _post_with_retry(self, payload: dict) -> dict:
        import asyncio as _asyncio

        last_exc: Exception | None = None
        for attempt in range(self._settings.api_max_retries + 1):
            try:
                resp = await self._client.post(self.API_URL, json=payload)
                if resp.status_code != 200:
                    raise LayerError("L1", "jina-api",
                                     f"HTTP {resp.status_code}: {resp.text[:500]}")
                return resp.json()
            except (httpx.HTTPError, LayerError) as exc:
                if isinstance(exc, UnsupportedModality):
                    raise
                last_exc = exc
                if attempt < self._settings.api_max_retries and self._settings.api_retry_backoff_s > 0:
                    await _asyncio.sleep(self._settings.api_retry_backoff_s * (2 ** attempt))
        raise last_exc  # type: ignore[misc]

    async def embed(self, inputs: list[MultimodalInput]) -> list[list[float]]:
        if self._ledger is not None:
            self._ledger.check_budget()
        api_inputs = [self._to_api_input(i) for i in inputs]
        model = self._settings.model_name.split("/")[-1]
        vectors: list[list[float]] = []
        batch_size = max(1, self._settings.api_batch_size)
        for start in range(0, len(api_inputs), batch_size):  # 批量请求合并 + 上限拆分
            batch = api_inputs[start:start + batch_size]
            payload = {"model": model, "task": "retrieval.query", "input": batch}
            if self._settings.matryoshka_dim:
                payload["dimensions"] = self._settings.matryoshka_dim
            data = await self._post_with_retry(payload)
            usage = data.get("usage") or {}
            if self._ledger is not None and usage:
                self._ledger.record(self.API_URL, model,
                                    int(usage.get("total_tokens",
                                                  usage.get("prompt_tokens", 0))))
            vectors.extend(truncate_and_normalize(d["embedding"], None) for d in data["data"])
        for v in vectors:
            if len(v) != self._dim:
                raise LayerError(
                    "L1", "jina-api",
                    f"API 返回维度 {len(v)} != config 期望 {self._dim}"
                    "(dimensions 参数可能未生效,检查模型名与 matryoshka_dim;"
                    "若确认换模型,走 M7 export→import 重算流程)",
                )
        return vectors


# ---------------------------------------------------------------- 插件登记(M21)
register("embedder", "local")(lambda s, ledger=None: JinaV5OmniAdapter(s))
register("embedder", "remote")(lambda s, ledger=None: RemoteEmbedderAdapter(s.base_url, s.effective_dim))
register("embedder", "jina_api")(lambda s, ledger=None: JinaAPIAdapter(s, ledger=ledger))
register("embedder", "fake")(lambda s, ledger=None: FakeDeterministicEmbedder(s.effective_dim))


def build_embedder(settings: EmbedderSettings, ledger=None) -> Embedder:
    """按 settings.backend 从插件表取嵌入后端(不存在任何自动回退;缺名即报错列出可用)。"""
    return get_plugin("embedder", settings.backend)(settings, ledger=ledger)
