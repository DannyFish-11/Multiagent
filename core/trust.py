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

    def _has_dump_all(self) -> bool:
        return hasattr(self._memory, "dump_all")

    async def _scan_entries(self) -> list[dict]:
        """枚举白名单记忆。dump_all 后端全量精确;否则退化为**尽力**向量检索(仅 audit 用,
        受 k 上限约束可能漏,不用于 is_trusted 判定——见 is_trusted 的精确 targeted 查找)。"""
        if self._has_dump_all():
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
        """精确判定 agent_id 是否在白名单。

        dump_all 后端:全量精确扫描 + meta 精确等值匹配。无 dump_all 后端:用**该 agent 条目的
        原文**做 targeted 检索(query==存储文本,最大化召回该条),再按 `trusted_agent_id` meta
        精确匹配——不再靠通用短语的相似度排序(那会漏排在 k 之外的条目)。始终精确等值判定,
        绝不因相似度沾边而误信任(误判方向也 fail-closed:漏召回 → 视为未信任 → 需人工批准)。"""
        if not agent_id:
            return False
        if self._has_dump_all():
            return any(e["agent_id"] == agent_id for e in await self._scan_entries())
        hits = await self._memory.search(
            MultimodalInput.text(self._entry_text(agent_id)), k=50)
        return any(h.meta.get("kind") == TRUST_KIND
                   and h.meta.get("trusted_agent_id") == agent_id for h in hits)

    async def audit(self) -> list[dict]:
        """列出全部白名单记忆(可审计)。"""
        return await self._scan_entries()
