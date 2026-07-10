"""A2A 信任白名单(M5.2):白名单本身是记忆,可被检索与审计。

- 未知 agent_id 的委托默认需人工批准(Omnigent 策略层同规则,见
  omnigent/omnigent_policies/a2a_trust.py)
- trust(agent_id) 写入一条 kind=trust_whitelist 的私有记忆
- is_trusted() 通过检索该类记忆判定
"""

from __future__ import annotations

from typing import TYPE_CHECKING

from core.schemas import MultimodalInput

if TYPE_CHECKING:
    from adapters.memory import MemoryStore

TRUST_KIND = "trust_whitelist"


class TrustStore:
    def __init__(self, memory: "MemoryStore") -> None:
        self._memory = memory

    @staticmethod
    def _entry_text(agent_id: str) -> str:
        return f"信任白名单:允许来自 agent {agent_id} 的 A2A 任务委托"

    async def trust(self, agent_id: str, note: str = "") -> str:
        text = self._entry_text(agent_id) + (f"({note})" if note else "")
        return await self._memory.add(
            MultimodalInput.text(text),
            {"kind": TRUST_KIND, "trusted_agent_id": agent_id, "visibility": "private"},
        )

    async def _scan_entries(self) -> list[dict]:
        """白名单判定必须精确:后端支持 dump_all 时全量扫描;否则退化为向量检索。"""
        if hasattr(self._memory, "dump_all"):
            points = await self._memory.dump_all()
            return [
                {"memory_id": p["id"],
                 "agent_id": p["payload"].get("meta", {}).get("trusted_agent_id"),
                 "content": p["payload"].get("content", "")}
                for p in points
                if p["payload"].get("meta", {}).get("kind") == TRUST_KIND
            ]
        hits = await self._memory.search(MultimodalInput.text("信任白名单 A2A 任务委托"), k=50)
        return [
            {"memory_id": h.id, "agent_id": h.meta.get("trusted_agent_id"), "content": h.content}
            for h in hits if h.meta.get("kind") == TRUST_KIND
        ]

    async def is_trusted(self, agent_id: str) -> bool:
        return any(e["agent_id"] == agent_id for e in await self._scan_entries())

    async def audit(self) -> list[dict]:
        """列出全部白名单记忆(可审计)。"""
        return await self._scan_entries()
