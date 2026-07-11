"""LiteLLM 适配器(M21):一个 OpenAI 格式调 100+ 家 LLM。

装了可选依赖(`uv sync --extra litellm`)后,`llm.mode=litellm` 即用 OpenAI / Claude /
Gemini / Bedrock / Ollama / vLLM / DeepSeek / … 任一供应商——"随便拉一个 LLM 过来就能用"。

配置(复用 llm.chat 角色三元组):
- `model`:litellm 的 provider/model 写法,如 `gpt-4o` · `anthropic/claude-3-5-sonnet`
  · `deepseek/deepseek-chat` · `ollama/llama3` · `openai/自建兼容模型`
- `base_url`(可选)→ api_base(自建/兼容端点)
- `api_key`(可选)→ 该 provider 的 key;留空则 litellm 读对应 provider 的环境变量

红线:litellm 只在本文件接触(lazy import);usage 记入 CostLedger,预算闸在前。
"""

from __future__ import annotations

from typing import Any

from core.errors import LayerError
from core.schemas import Message


def _get(obj: Any, key: str, default: Any = 0) -> Any:
    if obj is None:
        return default
    if isinstance(obj, dict):
        return obj.get(key, default)
    return getattr(obj, key, default)


class LiteLLMAdapter:
    """与 LLMClient 协议一致(async chat);model 决定走哪家,零改业务码。"""

    def __init__(self, role, ledger=None) -> None:
        if not role.model:
            raise LayerError(
                "L0", "litellm",
                "llm.chat.model 未配置:litellm 需 provider/model(如 anthropic/claude-3-5-sonnet)")
        self._model = role.model
        self._api_key = role.api_key or None
        self._api_base = role.base_url or None
        self._timeout = role.timeout_s
        self._ledger = ledger
        self.last_meta: dict[str, Any] = {}

    @property
    def model(self) -> str:
        return self._model

    async def chat(self, messages: list[Message], **kw: Any) -> str:
        if self._ledger is not None:
            self._ledger.check_budget()      # 预算闸门:超限直接拒绝
        try:
            import litellm
        except ImportError as exc:
            raise LayerError("L0", "litellm",
                             "缺 litellm 依赖:uv sync --extra litellm") from exc
        try:
            resp = await litellm.acompletion(
                model=self._model,
                messages=[m.model_dump() for m in messages],
                api_key=self._api_key, api_base=self._api_base,
                timeout=self._timeout, **kw)
        except Exception as exc:
            raise LayerError("L0", "litellm", f"调用失败({self._model}): {exc}") from exc

        choices = _get(resp, "choices", None)
        if not choices:
            raise LayerError("L0", "litellm", f"响应无 choices: {resp!r}"[:300])
        msg = _get(choices[0], "message", None)
        content = _get(msg, "content", None)
        if content is None:
            raise LayerError("L0", "litellm", f"空回复: {resp!r}"[:300])

        usage = _get(resp, "usage", None)
        pt, ct = int(_get(usage, "prompt_tokens", 0)), int(_get(usage, "completion_tokens", 0))
        if self._ledger is not None and (pt or ct):
            self._ledger.record(self._api_base or "litellm", self._model, pt, ct)
        self.last_meta = {"model": self._model,
                          "usage": {"prompt_tokens": pt, "completion_tokens": ct}}
        return content

    async def chat_tools(self, messages: list[dict], tools: list[dict], **kw: Any):
        """function-calling 一步(M22 工具循环用)。返回 AssistantTurn。"""
        import json

        from core.tools import AssistantTurn, ToolCall

        if self._ledger is not None:
            self._ledger.check_budget()
        try:
            import litellm
        except ImportError as exc:
            raise LayerError("L0", "litellm",
                             "缺 litellm 依赖:uv sync --extra litellm") from exc
        try:
            resp = await litellm.acompletion(
                model=self._model, messages=messages, tools=tools or None,
                api_key=self._api_key, api_base=self._api_base, timeout=self._timeout, **kw)
        except Exception as exc:
            raise LayerError("L0", "litellm", f"调用失败({self._model}): {exc}") from exc
        choices = _get(resp, "choices", None)
        if not choices:
            raise LayerError("L0", "litellm", f"响应无 choices: {resp!r}"[:300])
        msg = _get(choices[0], "message", None)
        usage = _get(resp, "usage", None)
        pt, ct = int(_get(usage, "prompt_tokens", 0)), int(_get(usage, "completion_tokens", 0))
        if self._ledger is not None and (pt or ct):
            self._ledger.record(self._api_base or "litellm", self._model, pt, ct)
        calls = []
        for tc in (_get(msg, "tool_calls", None) or []):
            fn = _get(tc, "function", {})
            raw = _get(fn, "arguments", "{}")
            try:
                args = raw if isinstance(raw, dict) else json.loads(raw or "{}")
            except (json.JSONDecodeError, TypeError):
                args = {}
            calls.append(ToolCall(id=_get(tc, "id", ""), name=_get(fn, "name", ""),
                                  arguments=args))
        return AssistantTurn(content=_get(msg, "content", None), tool_calls=calls)

    async def health(self) -> bool:
        # litellm 无统一 health 探针;真正的可达性在首次 chat 时以 L0 错误暴露(不静默)
        return True
