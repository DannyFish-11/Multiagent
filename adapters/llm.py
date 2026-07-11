"""L0 适配层:LLMClient 协议 + vLLM OpenAI 兼容端点适配器。

核心逻辑只依赖 LLMClient 协议;对 vLLM/OpenAI 协议细节的所有知识收敛在本文件。
"""

from __future__ import annotations

import logging
from typing import Any, Protocol, runtime_checkable

import httpx

from core.errors import LayerError
from core.plugins import get_plugin, register
from core.schemas import Message

logger = logging.getLogger(__name__)


@runtime_checkable
class LLMClient(Protocol):
    async def chat(self, messages: list[Message], **kw) -> str: ...


class VLLMOpenAIAdapter:
    """通过 OpenAI 兼容 /chat/completions 调用 vLLM 上的 Gemma 4。"""

    def __init__(
        self,
        base_url: str,
        model: str,
        api_key: str = "EMPTY",
        timeout_s: float = 120.0,
        transport: httpx.AsyncBaseTransport | None = None,
    ) -> None:
        self.base_url = base_url.rstrip("/")
        self.model = model
        self._client = httpx.AsyncClient(
            base_url=self.base_url,
            headers={"Authorization": f"Bearer {api_key}"},
            timeout=timeout_s,
            transport=transport,
        )

    async def chat(self, messages: list[Message], **kw: Any) -> str:
        payload: dict[str, Any] = {
            "model": self.model,
            "messages": [m.model_dump() for m in messages],
        }
        payload.update(kw)
        try:
            resp = await self._client.post("/chat/completions", json=payload)
        except httpx.HTTPError as exc:
            raise LayerError("L0", "vllm", f"推理端点不可达 {self.base_url}: {exc}") from exc
        if resp.status_code != 200:
            raise LayerError("L0", "vllm", f"HTTP {resp.status_code}: {resp.text[:500]}")
        data = resp.json()
        try:
            content = data["choices"][0]["message"]["content"]
        except (KeyError, IndexError) as exc:
            raise LayerError("L0", "vllm", f"响应结构异常: {data}") from exc
        if content is None:
            raise LayerError("L0", "vllm", f"空回复: {data}")
        return content

    async def health(self) -> bool:
        try:
            resp = await self._client.get("/models")
        except httpx.HTTPError as exc:
            raise LayerError("L0", "vllm", f"健康检查失败 {self.base_url}/models: {exc}") from exc
        if resp.status_code != 200:
            raise LayerError("L0", "vllm", f"健康检查 HTTP {resp.status_code}")
        return True

    async def aclose(self) -> None:
        await self._client.aclose()


# ---------------------------------------------------------------- PHASE2.5 M-A

class OpenAICompatAdapter:
    """外部 OpenAI 兼容 API 适配器(多模态,含重试/备用端点/成本护栏)。

    - 端点三元组(base_url/api_key/model)全部来自 config/环境变量
    - 多模态输入:Message.content 允许 OpenAI parts 列表(text + image_url),
      与 MemoryAgent 构造的负载直接兼容
    - 单端点指数退避重试(max_retries);主端点连续失败 failover_threshold 次
      后切换到 fallbacks 中的下一个端点,切换写日志并在 last_meta 标注
      (LLMClient 协议返回 str,响应元数据经 adapter.last_meta 暴露)
    - 每次成功调用把 usage 记入 CostLedger;预算超限在发请求前拒绝
    """

    def __init__(self, role, ledger=None,
                 transport: httpx.AsyncBaseTransport | None = None) -> None:
        # role: core.config.LLMRoleSettings(鸭子类型,避免无谓的运行时依赖)
        endpoints = [
            {"base_url": role.base_url, "api_key": role.api_key, "model": role.model},
            *[{"base_url": f.base_url, "api_key": f.api_key, "model": f.model}
              for f in role.fallbacks],
        ]
        if not endpoints[0]["base_url"]:
            raise LayerError("L0", "openai-compat", "llm 角色未配置 base_url(停点:API 供应商与模型由人类指定)")
        self._endpoints = endpoints
        self._active = 0
        self._consecutive_failures = 0
        self._failover_threshold = max(1, role.failover_threshold)
        self._max_retries = max(0, role.max_retries)
        self._backoff_s = role.retry_backoff_s
        self._timeout_s = role.timeout_s
        self._ledger = ledger
        self._transport = transport
        self._clients: dict[int, httpx.AsyncClient] = {}
        self.last_meta: dict[str, Any] = {}

    @property
    def model(self) -> str:
        return self._endpoints[self._active]["model"]

    @property
    def active_endpoint(self) -> str:
        return self._endpoints[self._active]["base_url"]

    def _client(self, idx: int) -> httpx.AsyncClient:
        if idx not in self._clients:
            ep = self._endpoints[idx]
            self._clients[idx] = httpx.AsyncClient(
                base_url=ep["base_url"].rstrip("/"),
                headers={"Authorization": f"Bearer {ep['api_key'] or 'EMPTY'}"},
                timeout=self._timeout_s,
                transport=self._transport,
            )
        return self._clients[idx]

    async def _post_once(self, idx: int, payload: dict[str, Any]) -> dict[str, Any]:
        resp = await self._client(idx).post("/chat/completions", json=payload)
        if resp.status_code != 200:
            raise LayerError("L0", "openai-compat",
                             f"HTTP {resp.status_code} @ {self._endpoints[idx]['base_url']}: "
                             f"{resp.text[:300]}")
        return resp.json()

    async def _post_with_retry(self, idx: int, payload: dict[str, Any]) -> dict[str, Any]:
        import asyncio as _asyncio

        last_exc: Exception | None = None
        for attempt in range(self._max_retries + 1):
            try:
                return await self._post_once(idx, payload)
            except (httpx.HTTPError, LayerError) as exc:
                last_exc = exc
                if attempt < self._max_retries and self._backoff_s > 0:
                    await _asyncio.sleep(self._backoff_s * (2 ** attempt))
        raise last_exc  # type: ignore[misc]

    async def chat(self, messages: list[Message], **kw: Any) -> str:
        if self._ledger is not None:
            self._ledger.check_budget()  # 预算闸门:超限直接拒绝

        start_idx = self._active
        for offset in range(len(self._endpoints)):
            idx = (start_idx + offset) % len(self._endpoints)
            ep = self._endpoints[idx]
            payload: dict[str, Any] = {
                "model": ep["model"],
                "messages": [m.model_dump() for m in messages],
            }
            payload.update(kw)
            try:
                data = await self._post_with_retry(idx, payload)
            except (httpx.HTTPError, LayerError) as exc:
                self._consecutive_failures += 1
                logger.warning("LLM 端点失败 %s(连续第 %d 次): %s",
                               ep["base_url"], self._consecutive_failures, exc)
                if (self._consecutive_failures >= self._failover_threshold
                        and offset < len(self._endpoints) - 1):
                    nxt = self._endpoints[(idx + 1) % len(self._endpoints)]
                    logger.warning("failover:%s → %s", ep["base_url"], nxt["base_url"])
                    self._consecutive_failures = 0
                    continue
                raise LayerError("L0", "openai-compat",
                                 f"全部端点耗尽,最后错误: {exc}") from exc

            self._consecutive_failures = 0
            failover_happened = idx != start_idx
            if failover_happened:
                self._active = idx  # 粘住可用端点
            usage = data.get("usage") or {}
            if self._ledger is not None and usage:
                self._ledger.record(ep["base_url"], ep["model"],
                                    int(usage.get("prompt_tokens", 0)),
                                    int(usage.get("completion_tokens", 0)))
            self.last_meta = {
                "endpoint": ep["base_url"], "model": ep["model"],
                "usage": usage, "failover": failover_happened,
            }
            try:
                content = data["choices"][0]["message"]["content"]
            except (KeyError, IndexError) as exc:
                raise LayerError("L0", "openai-compat", f"响应结构异常: {data}") from exc
            if content is None:
                raise LayerError("L0", "openai-compat", f"空回复: {data}")
            return content
        raise LayerError("L0", "openai-compat", "没有可用端点")

    async def chat_tools(self, messages: list[dict], tools: list[dict], **kw: Any):
        """function-calling 一步:messages 为 OpenAI 格式 dict(含 tool/assistant.tool_calls),
        tools 为 function 规格。返回 AssistantTurn(content 或 tool_calls)。M22 工具循环用。"""
        import json

        from core.tools import AssistantTurn, ToolCall

        if self._ledger is not None:
            self._ledger.check_budget()
        idx = self._active
        ep = self._endpoints[idx]
        payload: dict[str, Any] = {"model": ep["model"], "messages": messages}
        if tools:
            payload["tools"] = tools
        payload.update(kw)
        data = await self._post_with_retry(idx, payload)
        try:
            msg = data["choices"][0]["message"]
        except (KeyError, IndexError) as exc:
            raise LayerError("L0", "openai-compat", f"响应结构异常: {data}") from exc
        usage = data.get("usage") or {}
        if self._ledger is not None and usage:
            self._ledger.record(ep["base_url"], ep["model"],
                                int(usage.get("prompt_tokens", 0)),
                                int(usage.get("completion_tokens", 0)))
        calls = []
        for tc in (msg.get("tool_calls") or []):
            fn = tc.get("function", {})
            raw = fn.get("arguments")
            try:
                args = raw if isinstance(raw, dict) else json.loads(raw or "{}")
            except (json.JSONDecodeError, TypeError):
                args = {}
            calls.append(ToolCall(id=tc.get("id", ""), name=fn.get("name", ""), arguments=args))
        return AssistantTurn(content=msg.get("content"), tool_calls=calls)

    async def health(self) -> bool:
        try:
            resp = await self._client(self._active).get("/models")
        except httpx.HTTPError as exc:
            raise LayerError("L0", "openai-compat",
                             f"健康检查失败 {self.active_endpoint}: {exc}") from exc
        if resp.status_code >= 500:
            raise LayerError("L0", "openai-compat", f"健康检查 HTTP {resp.status_code}")
        return True

    async def aclose(self) -> None:
        for client in self._clients.values():
            await client.aclose()


class EchoLLM:
    """离线 demo 档(M20 A1):不做真实推理,仅回显"检索到的记忆 + 用户问题"。

    用途:零 key / 零 GPU / 零 docker 验证"存入→检索→注入 prompt→复述"这条
    记忆闭环链路是否打通。**不代表模型智力**;检索质量受 fake 哈希嵌入限制而退化。
    与真实 LLMClient 同协议(async chat),可被 MemoryAgent 直接使用。
    """

    async def chat(self, messages: list[Message], **kw: Any) -> str:
        system = next((m for m in messages if m.role == "system"), None)
        user = next((m for m in messages if m.role == "user"), None)
        mem = ""
        if system is not None:
            text = system.content if isinstance(system.content, str) else str(system.content)
            if "## 相关记忆" in text:
                mem = text.split("## 相关记忆", 1)[1].strip()
        return (
            "【echo demo 模式 · 非真实推理】\n"
            f"你问:{_plain_text(user.content) if user else ''}\n"
            f"我检索到的相关记忆:\n{mem or '(无相关记忆)'}"
        )

    async def health(self) -> bool:
        return True


def _plain_text(content: Any) -> str:
    """从 Message.content(str 或 OpenAI 多模态 parts 列表)取纯文本。"""
    if isinstance(content, str):
        return content
    if isinstance(content, list):
        return " ".join(
            part.get("text", "") for part in content
            if isinstance(part, dict) and part.get("type") == "text"
        )
    return str(content)


# ---------------------------------------------------------------- 插件登记(M21)
# 内置 LLM 后端按名字注册到插件表;build_llm_client 只按 config.llm.mode 取名解析,
# 第三方 LLM(entry point "llm:xxx" 或 @register("llm","xxx"))掉进来即可用。

@register("llm", "echo")
def _llm_echo(config, role, ledger):
    return EchoLLM()


@register("llm", "api")
def _llm_api(config, role, ledger):
    role_cfg = config.llm.memory if role == "memory" else config.llm.chat
    # memory 角色未配置时回落到 chat 角色(允许同一端点)
    if role == "memory" and not role_cfg.base_url:
        role_cfg = config.llm.chat
    return OpenAICompatAdapter(role_cfg, ledger=ledger)


@register("llm", "local")
def _llm_local(config, role, ledger):
    return VLLMOpenAIAdapter(
        base_url=config.llm.base_url, model=config.llm.model,
        api_key=config.llm.api_key, timeout_s=config.llm.timeout_s,
    )


@register("llm", "litellm")
def _llm_litellm(config, role, ledger):
    # 名字常驻注册;LiteLLMAdapter 及其 litellm 依赖仅在真正用到时 lazy import
    from adapters.llm_litellm import LiteLLMAdapter

    role_cfg = config.llm.memory if role == "memory" else config.llm.chat
    if role == "memory" and not role_cfg.model:
        role_cfg = config.llm.chat
    return LiteLLMAdapter(role_cfg, ledger=ledger)


def build_llm_client(config, role: str = "chat", ledger=None):
    """按 config.llm.mode 从插件表取 LLM 后端(local/api/echo/litellm/第三方…)。

    core/services 只调用不选型;新增后端只需注册一个名字,此处零改动。
    M20 B:返回前经 instrument_llm 包裹(observability.enabled=false 时原样返回)。
    """
    from adapters.observability import instrument_llm

    client = get_plugin("llm", config.llm.mode)(config, role, ledger)
    return instrument_llm(client, config)


def build_ledger(config):
    """CostLedger 单例构造(LLM 与嵌入共用)。"""
    from adapters.cost_ledger import CostLedger

    return CostLedger(config.budget.prices, config.budget.daily_usd,
                      config.budget.ledger_path)


class ConcurrencyLimitedLLM:
    """LLMClient 装饰器(M9.1):信号量限制并发 LLM 调用,防 API 限流雪崩。

    包裹任意 LLMClient;不改被包裹者签名(adapter 红线:core 只见 LLMClient 协议)。
    """

    def __init__(self, inner, max_concurrent: int) -> None:
        import asyncio as _asyncio

        self._inner = inner
        self._sem = _asyncio.Semaphore(max(1, max_concurrent))

    async def chat(self, messages, **kw):
        async with self._sem:
            return await self._inner.chat(messages, **kw)

    def __getattr__(self, name):  # 透传 health / aclose / model / last_meta 等
        return getattr(self._inner, name)
