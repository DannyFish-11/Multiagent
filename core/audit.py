"""全量审计日志(PHASE3 M9.2)。

每个动作(含 auto 级)记录 who / what / 参数摘要 / 结果 / 耗费,JSONL 落盘挂 volume。
并发安全:单 asyncio.Lock 串行化追加写(单进程内;多实例各写各的审计文件)。
"""

from __future__ import annotations

import asyncio
import json
import time
from pathlib import Path
from typing import Any


def _summarize(value: Any, limit: int = 200) -> Any:
    """参数摘要:截断长字符串、base64、避免把密钥/大 blob 写进审计。"""
    if isinstance(value, str):
        return value[:limit] + ("…" if len(value) > limit else "")
    if isinstance(value, dict):
        return {k: _summarize(v, limit) for k, v in value.items()}
    if isinstance(value, list):
        return [_summarize(v, limit) for v in value[:20]]
    return value


class AuditLog:
    def __init__(self, path: str | Path) -> None:
        self._path = Path(path)
        self._path.parent.mkdir(parents=True, exist_ok=True)
        self._lock = asyncio.Lock()

    async def record(self, *, action: str, level: str, decision: str,
                     agent_id: str = "", session_id: str = "", source: str = "unknown",
                     params: Any = None, result: Any = None,
                     cost_usd: float = 0.0, extra: dict | None = None) -> dict:
        # source 默认 "unknown"(非 "user"):忘传来源不应被静默标成"人类发起"这一特权值。
        entry = {
            "ts": time.time(),
            "agent_id": agent_id,
            "session_id": session_id,
            "source": source,          # user | email | web | timer …(支付来源检查依赖此字段)
            "action": action,
            "level": level,            # auto | confirm | deny
            "decision": decision,      # executed | approved | rejected | denied | timeout
            "params": _summarize(params),
            "result": _summarize(result),
            "cost_usd": round(float(cost_usd), 8),
        }
        if extra:
            entry.update(extra)
        async with self._lock:
            with self._path.open("a", encoding="utf-8") as f:
                f.write(json.dumps(entry, ensure_ascii=False) + "\n")
        return entry

    def read_all(self) -> list[dict]:
        if not self._path.exists():
            return []
        entries: list[dict] = []
        for line in self._path.read_text(encoding="utf-8").splitlines():
            if not line.strip():
                continue
            try:
                entries.append(json.loads(line))
            except json.JSONDecodeError:
                # 追加写非原子:进程在写一行途中被杀会留下半行坏记录。
                # 宁可少记一条,不可因此拒读全部(与 CostLedger 同一纪律)。
                continue
        return entries
