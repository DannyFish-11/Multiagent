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

    def global_cap_hit(self, now: float) -> tuple[bool, str]:
        """层二全局熔断:日/月总额度触顶 → 应停止一切派发(halt-all)。返回 (触顶?, 原因)。"""
        if self.day_total(now) >= self._cfg.global_daily_usd:
            return True, (f"全局日额度触顶:${self.day_total(now):.2f} >= "
                          f"${self._cfg.global_daily_usd:.2f}")
        if self.month_total(now) >= self._cfg.global_monthly_usd:
            return True, (f"全局月额度触顶:${self.month_total(now):.2f} >= "
                          f"${self._cfg.global_monthly_usd:.2f}")
        return False, ""

    def exceeds_structural_cap(self, experiment_budget_usd: float) -> tuple[bool, str]:
        """预算是否超过'全局日额度 × N%'这一**固定**上限。

        超过则该实验在任何余额下都永不可派发(结构性拒绝),调用方应将其置终态而非
        无限排队。返回 (超限?, 原因)。
        """
        total_cap = self._cfg.global_daily_usd * self._cfg.single_experiment_max_ratio
        if experiment_budget_usd > total_cap + 1e-9:
            return True, (f"单实验预算 ${experiment_budget_usd:.2f} 超过全局日额度的 "
                          f"{self._cfg.single_experiment_max_ratio:.0%}(${total_cap:.2f}),结构性拒绝")
        return False, ""

    def experiment_within_ratio(self, experiment_budget_usd: float,
                                now: float) -> tuple[bool, str]:
        """单实验占比闸门:预算不得超过全局日余额的 N%,亦不得超过日额度本身的 N%。

        这是**实验专属、永久性**的判定(实验预算固定,不因等待而变小);不满足时
        只应跳过该实验,不得据此停派其余队列。返回 (通过?, 原因)。
        """
        ratio = self._cfg.single_experiment_max_ratio
        remaining = max(self._cfg.global_daily_usd - self.day_total(now), 0.0)
        remaining_cap = remaining * ratio
        if experiment_budget_usd > remaining_cap + 1e-9:
            return False, (f"单实验预算 ${experiment_budget_usd:.2f} 超过日余额的 "
                           f"{ratio:.0%}(${remaining_cap:.2f})")
        total_cap = self._cfg.global_daily_usd * ratio
        if experiment_budget_usd > total_cap + 1e-9:
            return False, (f"单实验预算 ${experiment_budget_usd:.2f} 超过全局日额度的 "
                           f"{ratio:.0%}(${total_cap:.2f})")
        return True, "ok"

    def can_dispatch(self, experiment_budget_usd: float, now: float) -> tuple[bool, str]:
        """派发新 VM 前的综合判定(全局熔断 + 单实验占比)。返回 (允许?, 原因)。

        注:conductor 派发循环应分别调用 global_cap_hit / experiment_within_ratio,
        以区分"halt-all"(全局触顶,break)与"skip-this"(单实验超限,continue),
        避免大预算实验永久堵塞队列头。本方法仅供单点判定/单元测试便捷使用。
        """
        hit, why = self.global_cap_hit(now)
        if hit:
            return False, why
        return self.experiment_within_ratio(experiment_budget_usd, now)


@dataclass
class PausedExperiment:
    experiment_id: str
    vm_id: str
    paused_at: float
    reason: str

    def expired(self, now: float, timeout_s: float) -> bool:
        return now - self.paused_at >= timeout_s
