"""群体资产库 metrics 原语(补 M13 缺口,PHASE4)。

M15 C 臂"无准入、仅靠 stats 自然筛选"需要:引用(cite)、举报(report)、
降级复审(demote)三类计数,以及基于计数的存活/扩散度量。持久化 JSONL 挂 volume。

一条共享池条目的生命指标:
  cites   —— 被几个实例采用/引用(扩散度)
  reports —— 被举报次数(负信号)
  demoted —— 是否已降级(复审判定为坏品后移出主池)
  first_seen / last_activity —— 存活时长计算
"""

from __future__ import annotations

import json
import threading
import time
from pathlib import Path
from typing import Any


class CommonsMetrics:
    def __init__(self, path: str | Path) -> None:
        self._path = Path(path)
        self._lock = threading.Lock()
        self._items: dict[str, dict[str, Any]] = {}
        if self._path.exists():
            try:
                self._items = json.loads(self._path.read_text(encoding="utf-8"))
            except (json.JSONDecodeError, OSError):
                self._items = {}

    def _save(self) -> None:
        self._path.parent.mkdir(parents=True, exist_ok=True)
        self._path.write_text(json.dumps(self._items, ensure_ascii=False, indent=2),
                              encoding="utf-8")

    def _entry(self, item_id: str) -> dict[str, Any]:
        return self._items.setdefault(item_id, {
            "cites": 0, "reports": 0, "demoted": False,
            "adopters": [], "reporters": [], "first_seen": time.time(),
            "last_activity": time.time(),
        })

    def register(self, item_id: str) -> None:
        with self._lock:
            self._entry(item_id)
            self._save()

    def cite(self, item_id: str, by_agent: str) -> None:
        with self._lock:
            e = self._entry(item_id)
            if by_agent not in e["adopters"]:
                e["adopters"].append(by_agent)
            e["cites"] += 1
            e["last_activity"] = time.time()
            self._save()

    def report(self, item_id: str, by_agent: str, reason: str = "") -> int:
        with self._lock:
            e = self._entry(item_id)
            reporters = e.setdefault("reporters", [])   # 去重上报者:单实例连报不叠加
            if by_agent not in reporters:
                reporters.append(by_agent)
            e["reports"] = len(reporters)               # reports = 不同上报者数
            e["last_activity"] = time.time()
            self._save()
            return e["reports"]

    def demote(self, item_id: str) -> None:
        with self._lock:
            e = self._entry(item_id)
            e["demoted"] = True
            e["last_activity"] = time.time()
            self._save()

    def should_demote(self, item_id: str, report_threshold: int = 3) -> bool:
        """降级复审判据:举报数达阈值且引用寥寥。"""
        e = self._items.get(item_id)
        if not e or e["demoted"]:
            return False
        distinct_reports = len(e.get("reporters", []))   # 按不同上报者计,防单实例刷举报
        return distinct_reports >= report_threshold and distinct_reports > len(e["adopters"])

    def spread(self, item_id: str) -> int:
        e = self._items.get(item_id, {})
        return len(e.get("adopters", []))

    def survival_seconds(self, item_id: str, until: float) -> float:
        e = self._items.get(item_id)
        if not e:
            return 0.0
        return max(0.0, until - e["first_seen"])

    def snapshot(self) -> dict[str, Any]:
        with self._lock:
            return json.loads(json.dumps(self._items))
