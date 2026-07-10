"""MemoryAgent:对话循环 + 记忆读写编排(BUILD_SPEC M3-3)。

每轮:检索相关记忆 → 注入 system prompt → LLM 生成 → 异步写入新记忆。
图像输入走 L1 编码入库(caption=用户消息)并以 OpenAI 多模态格式送入 L0。
只依赖 LLMClient / MemoryStore 协议,不 import 任何 adapters 实现。
"""

from __future__ import annotations

import asyncio
import logging
import uuid
from typing import TYPE_CHECKING

from core.config import AppConfig
from core.prompts import memory_block
from core.schemas import ChatResponse, Message, MemoryHit, MultimodalInput

if TYPE_CHECKING:
    from adapters.llm import LLMClient
    from adapters.memory import MemoryStore

logger = logging.getLogger(__name__)


class MemoryAgent:
    def __init__(self, llm: "LLMClient", memory: "MemoryStore", config: AppConfig) -> None:
        self._llm = llm
        self._memory = memory
        self._config = config
        self._pending: set[asyncio.Task] = set()
        self.last_write_error = False  # 后台写失败的可观测标记(healthz/测试用)
        # M8 埋点:检索事件日志(可选注入;None 时不记录)
        self._retrieval_logger = None

    def set_retrieval_logger(self, logger_) -> None:
        self._retrieval_logger = logger_

    def _build_system_prompt(self, hits: list[MemoryHit]) -> str:
        return (
            f"{self._config.agent.system_prompt}\n\n"
            f"## 相关记忆\n{memory_block(hits)}"
        )

    async def _write_memories(
        self, user_message: str, session_id: str,
        image: MultimodalInput | None,
    ) -> None:
        meta = {"session_id": session_id}
        try:
            if image is not None:
                await self._memory.add(image, dict(meta, caption=user_message))
            await self._memory.add(MultimodalInput.text(user_message), meta)
        except Exception:
            logger.exception("memory write failed (session=%s)", session_id)
            raise

    async def _background_write(self, user_message: str, session_id: str,
                                image: MultimodalInput | None) -> None:
        """后台路径:异常已在 _write_memories 记录,此处吞掉,避免
        "Task exception was never retrieved" 且不让单次写失败拖垮事件循环。"""
        try:
            await self._write_memories(user_message, session_id, image)
        except Exception:
            self.last_write_error = True

    async def chat(
        self,
        message: str,
        session_id: str = "default",
        image: MultimodalInput | None = None,
        sync_memory_write: bool = False,
    ) -> ChatResponse:
        query = MultimodalInput.text(message)
        hits = await self._memory.search(query, k=self._config.agent.top_k)

        event_id = uuid.uuid4().hex
        if self._retrieval_logger is not None:
            from core.metabolism import RetrievalEvent

            self._retrieval_logger.log(RetrievalEvent(
                query=message, hit_ids=[h.id for h in hits], event_id=event_id))

        user_content: str | list[dict] = message
        if image is not None:
            user_content = [
                {"type": "text", "text": message},
                {"type": "image_url", "image_url": {
                    "url": f"data:{image.mime or 'image/png'};base64,{image.content}"}},
            ]

        reply = await self._llm.chat([
            Message(role="system", content=self._build_system_prompt(hits)),
            Message(role="user", content=user_content),
        ])

        if sync_memory_write:
            await self._write_memories(message, session_id, image)
        else:
            task = asyncio.create_task(self._background_write(message, session_id, image))
            self._pending.add(task)
            task.add_done_callback(self._pending.discard)

        return ChatResponse(reply=reply, session_id=session_id, memories_used=hits,
                            event_id=event_id)

    async def drain(self) -> None:
        """等待所有后台记忆写入完成(优雅停机/测试用)。"""
        if self._pending:
            await asyncio.gather(*list(self._pending), return_exceptions=True)
