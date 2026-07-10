"""收件驱动 worker(PHASE3 M11)——系统第一个非人类触发的任务源。

默认关(config.gmail_poll.enabled)。对打了特定标签的新邮件触发一次:
  阅读 → 按需入记忆(经既有 EmailMemoryIngest)→ 草拟回复(仅草稿,不发出)。
全程 source="email" 走审计;任何 confirm 级动作(send/delete)绝不自动执行——
草拟(draft)是 auto,发送(send)是 confirm,worker 只走到 draft 为止。
"""

from __future__ import annotations

import asyncio
import logging
from typing import Any

from core.approval import ApprovalQueue
from core.config import GmailPollSettings
from core.email_ingest import EmailMemoryIngest

logger = logging.getLogger(__name__)


class EmailWorker:
    def __init__(self, settings: GmailPollSettings, gmail, ingest: EmailMemoryIngest,
                 queue: ApprovalQueue, agent_id: str = "") -> None:
        self._settings = settings
        self._gmail = gmail          # adapters.gmail.GmailAdapter(鸭子类型:list_labeled/draft_reply)
        self._ingest = ingest
        self._queue = queue
        self._agent_id = agent_id
        self._seen: set[str] = set()
        self._task: asyncio.Task | None = None

    async def process_once(self) -> list[dict]:
        """扫一轮带标签的新邮件,处理并返回本轮生成的草稿摘要。"""
        drafts: list[dict] = []
        messages = await self._gmail.list_labeled(self._settings.label)
        for msg in messages:
            mid = msg["id"]
            if mid in self._seen:
                continue
            self._seen.add(mid)

            # 阅读 + 按需入记忆(auto)
            await self._queue.gate(
                action="gmail_read", params={"message_id": mid}, source="email",
                agent_id=self._agent_id, execute=lambda m=msg: _noop(m))
            await self._ingest.ingest(msg.get("body", ""), message_id=mid, session_id="email")

            # 草拟回复(draft 为 auto;绝不 send)
            async def _do_draft(m=msg):
                return await self._gmail.draft_reply(m["id"], _auto_reply_stub(m))

            draft = await self._queue.gate(
                action="gmail_draft", params={"message_id": mid}, source="email",
                agent_id=self._agent_id, execute=_do_draft)
            drafts.append({"message_id": mid, "draft": draft})
        return drafts

    async def _loop(self) -> None:
        while True:
            try:
                await self.process_once()
            except Exception:
                logger.exception("email worker 轮询失败")
            await asyncio.sleep(self._settings.interval_s)

    def start(self) -> None:
        if self._settings.enabled and self._task is None:
            self._task = asyncio.create_task(self._loop())

    async def stop(self) -> None:
        if self._task is not None:
            self._task.cancel()
            try:
                await self._task
            except asyncio.CancelledError:
                pass
            self._task = None


async def _noop(_msg: Any) -> str:
    return "read"


def _auto_reply_stub(msg: dict) -> str:
    return f"(自动草稿)已收到您关于「{msg.get('subject', '')[:40]}」的邮件,稍后回复。"
