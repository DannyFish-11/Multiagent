"""收件驱动 worker(M11):对打了特定标签的新邮件触发一次:阅读 → 按需入记忆 → 草拟回复。

审批点(auto,零打扰,权限与 WebChat 同一引擎):
- gmail_read : 读该标签邮件
- gmail_draft: 起草回复草稿(绝不发送;draft 可视作 store 类)

红线:worker 绝不自动发送;发送永远是人类在 Gmail 里的显式动作。
"""

from __future__ import annotations

import asyncio
import logging
from collections import OrderedDict
from typing import Any

from core.approval import ApprovalQueue
from core.config import GmailPollSettings
from core.email_ingest import EmailMemoryIngest

logger = logging.getLogger(__name__)

# 去重窗口上限(先进先出逐出):长跑 worker 的 _seen 若无界,处理过的每封邮件 id
# 永久驻留内存。窗口内保证不重复处理;被逐出的老 id 若仍带标签最多重处理一次
# (与进程重启丢失 _seen 的既有语义同级,可接受)。
_SEEN_CAPACITY = 10_000


class EmailWorker:
    def __init__(self, settings: GmailPollSettings, gmail, ingest: EmailMemoryIngest,
                 queue: ApprovalQueue, agent_id: str = "",
                 seen_capacity: int = _SEEN_CAPACITY) -> None:
        self._settings = settings
        self._gmail = gmail          # adapters.gmail.GmailAdapter(鸭子类型:list_labeled/draft_reply)
        self._ingest = ingest
        self._queue = queue
        self._agent_id = agent_id
        self._seen: OrderedDict[str, None] = OrderedDict()
        self._seen_capacity = max(1, seen_capacity)
        self._task: asyncio.Task | None = None

    def _mark_seen(self, mid: str) -> None:
        self._seen[mid] = None
        self._seen.move_to_end(mid)
        while len(self._seen) > self._seen_capacity:
            self._seen.popitem(last=False)   # 逐出最老(防无界增长)

    async def process_once(self) -> list[dict]:
        """扫一轮带标签的新邮件,处理并返回本轮生成的草稿摘要。"""
        drafts: list[dict] = []
        messages = await self._gmail.list_labeled(self._settings.label)
        for msg in messages:
            mid = msg["id"]
            if mid in self._seen:
                continue

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
            # 处理全部成功才标记已见:中途失败(入记忆/草拟抛错)不标记,
            # 下一轮重试——否则异常邮件被静默永久跳过(草稿/记忆双双丢失)。
            self._mark_seen(mid)
        return drafts

    async def _loop(self) -> None:
        while True:
            try:
                await self.process_once()
            except Exception:
                logger.exception("email worker 轮询失败")
            await asyncio.sleep(max(1, self._settings.interval_s))

    def start(self) -> None:
        if self._task is None:
            self._task = asyncio.create_task(self._loop())

    async def stop(self) -> None:
        if self._task is not None:
            self._task.cancel()
            try:
                await self._task
            except asyncio.CancelledError:
                pass
            self._task = None


async def _noop(msg: dict) -> dict[str, Any]:
    return {"read": msg["id"]}


def _auto_reply_stub(msg: dict) -> str:
    return f"(草稿) 关于「{msg.get('subject', '(无主题)')}」的回复草稿"
