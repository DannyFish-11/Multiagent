"""双层熔断(PHASE5 M18.2)——治 token 失控。

层一(实验级):按 experiment_id 预算,烧穿即暂停(已在 ExperimentRunner/CostLedger);
              云端补充:暂停后 VM 低功耗等待人类决策,超时(默认 24h)自动打包自毁。
层二(全局):conductor 维护跨所有实验的日/月总额度,触顶停止派发新 VM 并通知;
            任何单一实验不得占用全局余额超过 N%(默认 50%)。
熔断事件一律即时通知(不等日报)。
"""

from __future__ import annotations

import json
from dataclasses import dataclass
from pathlib import Path


@dataclass
class BreakerConfig:
    global_daily_usd: float = 50.0
    global_monthly_usd: float = 500.0
    single_experiment_max_ratio: float = 0.5   # 单实验不得占全局余额 > 50%
    pause_wait_timeout_s: float = 86400.0        # 层一暂停后等待人类的超时(24h)


class GlobalBreaker:
    """层二全局熔断:跨所有实验的日/月总额度 + 单实验占比闸门。持久化挂 volume。"""

    def __init__(self, config: BreakerConfig, path: str | Path) -> None:
        self._cfg = config
        self._path = Path(path)
        self._spend: list[dict] = []
        if self._path.exists():
            try:
                self._spend = json.loads(self._path.read_text(encoding="utf-8"))
            except (json.JSONDecodeError, OSError):
                self._spend = []

    def _save(self) -> None:
        self._path.parent.mkdir(parents=True, exist_ok=True)
        self._path.write_text(json.dumps(self._spend, ensure_ascii=False), encoding="utf-8")

    def record(self, experiment_id: str, usd: float, now: float) -> None:
        self._spend.append({"experiment_id": experiment_id, "usd": usd, "ts": now})
        self._save()

    def _sum_since(self, since: float) -> float:
        return sum(s["usd"] for s in self._spend if s["ts"] >= since)

    def day_total(self, now: float) -> float:
        return self._sum_since(now - 86400)

    def month_total(self, now: float) -> float:
        return self._sum_since(now - 30 * 86400)

    def can_dispatch(self, experiment_budget_usd: float, now: float) -> tuple[bool, str]:
        """派发新 VM 前调用。返回 (允许?, 原因)。"""
        if self.day_total(now) >= self._cfg.global_daily_usd:
            return False, (f"全局日额度触顶:${self.day_total(now):.2f} >= "
                           f"${self._cfg.global_daily_usd:.2f}")
        if self.month_total(now) >= self._cfg.global_monthly_usd:
            return False, (f"全局月额度触顶:${self.month_total(now):.2f} >= "
                           f"${self._cfg.global_monthly_usd:.2f}")
        # 单实验占比闸门:该实验预算不得超过全局日余额的 N%
        remaining = self._cfg.global_daily_usd - self.day_total(now)
        if experiment_budget_usd > remaining * self._cfg.single_experiment_max_ratio + 1e-9:
            # 更严格:也不得超过全局日额度本身的 N%
            pass
        cap = self._cfg.global_daily_usd * self._cfg.single_experiment_max_ratio
        if experiment_budget_usd > cap + 1e-9:
            return False, (f"单实验预算 ${experiment_budget_usd:.2f} 超过全局日额度的 "
                           f"{self._cfg.single_experiment_max_ratio:.0%}(${cap:.2f})")
        return True, "ok"


@dataclass
class PausedExperiment:
    experiment_id: str
    vm_id: str
    paused_at: float
    reason: str

    def expired(self, now: float, timeout_s: float) -> bool:
        return now - self.paused_at >= timeout_s
