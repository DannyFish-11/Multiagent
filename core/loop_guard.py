"""循环硬上限(PHASE4 M14.1)。

审计所有"重试/迭代直到满意"的位置,每处强制 max_iterations。触顶记录
loop_capped 事件(经 AuditLog,若提供)而非静默停止。

用法:
    guard = LoopGuard("vote_rounds", limit=3, audit=..., on_cap=...)
    while not satisfied:
        await guard.tick()   # 触顶抛 LoopCapped(默认)或记录后返回 False
"""

from __future__ import annotations

from typing import TYPE_CHECKING

from core.errors import LayerError

if TYPE_CHECKING:
    from core.audit import AuditLog


class LoopCapped(LayerError):
    def __init__(self, point: str, limit: int) -> None:
        super().__init__("L14", "loop-guard",
                         f"循环 [{point}] 触及硬上限 {limit} 次仍未满足退出条件,已强制终止")
        self.point = point
        self.limit = limit


class LoopGuard:
    def __init__(self, point: str, limit: int, *, audit: "AuditLog | None" = None,
                 agent_id: str = "", session_id: str = "", raise_on_cap: bool = True) -> None:
        self.point = point
        self.limit = max(1, limit)
        self.count = 0
        self._audit = audit
        self._agent_id = agent_id
        self._session_id = session_id
        self._raise = raise_on_cap

    @property
    def capped(self) -> bool:
        return self.count >= self.limit

    async def tick(self) -> bool:
        """进入下一轮前调用。未触顶 → True 并计数;触顶 → 记 loop_capped 事件,
        raise_on_cap 时抛 LoopCapped,否则返回 False。"""
        if self.count >= self.limit:
            if self._audit is not None:
                await self._audit.record(
                    action=f"loop:{self.point}", level="auto", decision="loop_capped",
                    agent_id=self._agent_id, session_id=self._session_id,
                    params={"limit": self.limit}, extra={"point": self.point})
            if self._raise:
                raise LoopCapped(self.point, self.limit)
            return False
        self.count += 1
        return True
