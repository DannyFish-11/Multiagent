"""行动记忆闭环(M6.2)。

每次工具执行生成 ActionMemory(做了什么/结果/用户反馈)入私有记忆;
行动前检索相关 ActionMemory 注入决策上下文("上次你嫌这家吵"式经验延续)。
"""

from __future__ import annotations

import time
from typing import TYPE_CHECKING

from core.schemas import MemoryHit, MultimodalInput

if TYPE_CHECKING:
    from adapters.memory import MemoryStore

ACTION_KIND = "action"


class ActionRecorder:
    def __init__(self, memory: "MemoryStore") -> None:
        self._memory = memory

    async def record(self, action: str, result: str, feedback: str | None = None,
                     tool: str = "", session_id: str = "default") -> str:
        parts = [f"行动:{action}", f"结果:{result}"]
        if feedback:
            parts.append(f"用户反馈:{feedback}")
        return await self._memory.add(
            MultimodalInput.text(";".join(parts)),
            {"kind": ACTION_KIND, "tool": tool, "session_id": session_id,
             "visibility": "private", "executed_at": time.time()},
        )

    async def recall_relevant(self, intent: str, k: int = 3) -> list[MemoryHit]:
        """行动前调用:检索与当前意图相关的历史行动经验。"""
        hits = await self._memory.search(MultimodalInput.text(intent), k=max(k * 3, 9))
        return [h for h in hits if h.meta.get("kind") == ACTION_KIND][:k]

    @staticmethod
    def context_block(hits: list[MemoryHit]) -> str:
        if not hits:
            return ""
        lines = "\n".join(f"- {h.content}" for h in hits)
        return f"\n## 相关行动经验\n{lines}"
