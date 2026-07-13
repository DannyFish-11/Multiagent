"""成本护栏(PHASE2.5 M-A):按日累计各端点 token 费用,超预算拒绝新请求。

设计要点:
- 单价表与日预算来自 config(budget.prices / budget.daily_usd)
- 账本持久化为 JSON 文件(挂 volume 后容器重建不清零当日用量)
- 放在 adapter 层,不依赖 Omnigent 在场(纯 Docker 模式的基础治理之一)
- LLM 与嵌入 API 共用同一账本实例(经 factory 单例注入)
"""

from __future__ import annotations

import json
import threading
import time
from pathlib import Path
from typing import Any

from core.errors import LayerError


def _today() -> str:
    return time.strftime("%Y-%m-%d", time.localtime())


class CostLedger:
    """按日 token 费用记账 + 预算闸门。

    prices:{model 名: {"input": 每百万输入 token 美元, "output": 每百万输出 token 美元}}。
    未配置单价的模型按 0 记费并在条目里标注 unpriced(不猜价格,不静默)。
    """

    def __init__(self, prices: dict[str, dict[str, float]] | None,
                 daily_budget_usd: float, path: str | Path) -> None:
        self._prices = prices or {}
        self._budget = daily_budget_usd
        self._path = Path(path)
        self._state: dict[str, Any] = {"date": _today(), "entries": {}, "total_usd": 0.0}
        self._lock = threading.Lock()  # M9.1 并发安全(跨线程/事件循环 record)
        self._load()

    # ---- 持久化 ----

    def _load(self) -> None:
        if self._path.exists():
            try:
                state = json.loads(self._path.read_text(encoding="utf-8"))
                if state.get("date") == _today():
                    self._state = state
            except (json.JSONDecodeError, OSError):
                pass  # 账本损坏时从零开始(宁可少记,不可拒读)

    def _save(self) -> None:
        self._path.parent.mkdir(parents=True, exist_ok=True)
        self._path.write_text(json.dumps(self._state, ensure_ascii=False, indent=2),
                              encoding="utf-8")

    def _rollover(self) -> None:
        if self._state.get("date") != _today():
            self._state = {"date": _today(), "entries": {}, "total_usd": 0.0}

    # ---- 记账 / 闸门 ----

    def record(self, endpoint: str, model: str,
               prompt_tokens: int, completion_tokens: int = 0,
               *, experiment_id: str = "", task_id: str = "",
               agent_id: str = "", purpose: str = "") -> float:
        """记录一次调用,返回本次费用(美元)。并发安全。

        M14.1 维度标签 {experiment_id, task_id, agent_id, purpose}:既有日预算
        逻辑不变,另按 experiment_id 独立累计(experiment_usd)。"""
        with self._lock:
            return self._record_locked(endpoint, model, prompt_tokens, completion_tokens,
                                       experiment_id, task_id, agent_id, purpose)

    def _record_locked(self, endpoint: str, model: str,
                       prompt_tokens: int, completion_tokens: int = 0,
                       experiment_id: str = "", task_id: str = "",
                       agent_id: str = "", purpose: str = "") -> float:
        self._rollover()
        # token 数按非负钳制:异常/伪造的负 usage 不得压低累计、腾出预算
        prompt_tokens = max(0, int(prompt_tokens or 0))
        completion_tokens = max(0, int(completion_tokens or 0))
        price = self._prices.get(model)
        cost = 0.0
        if price:
            cost = (prompt_tokens * float(price.get("input", 0.0))
                    + completion_tokens * float(price.get("output", 0.0))) / 1_000_000
        key = f"{endpoint}::{model}"
        entry = self._state["entries"].setdefault(
            key, {"prompt_tokens": 0, "completion_tokens": 0, "usd": 0.0,
                  "unpriced": price is None})
        entry["prompt_tokens"] += prompt_tokens
        entry["completion_tokens"] += completion_tokens
        entry["usd"] = round(entry["usd"] + cost, 8)
        self._state["total_usd"] = round(self._state["total_usd"] + cost, 8)
        # 维度累计(实验记账)
        if experiment_id:
            exp = self._state.setdefault("experiments", {}).setdefault(
                experiment_id, {"usd": 0.0, "by_agent": {}, "by_purpose": {}})
            exp["usd"] = round(exp["usd"] + cost, 8)
            if agent_id:
                exp["by_agent"][agent_id] = round(exp["by_agent"].get(agent_id, 0.0) + cost, 8)
            if purpose:
                exp["by_purpose"][purpose] = round(exp["by_purpose"].get(purpose, 0.0) + cost, 8)
        self._save()
        return cost

    def experiment_usd(self, experiment_id: str) -> float:
        with self._lock:
            self._rollover()
            return float(self._state.get("experiments", {}).get(experiment_id, {}).get("usd", 0.0))

    def experiment_snapshot(self, experiment_id: str) -> dict:
        with self._lock:
            return json.loads(json.dumps(
                self._state.get("experiments", {}).get(experiment_id, {})))

    def today_usd(self) -> float:
        self._rollover()
        return float(self._state["total_usd"])

    def check_budget(self) -> None:
        """超出日预算时抛错(在发起新请求之前调用)。并发安全。"""
        with self._lock:
            self._rollover()
            over = self._budget is not None and self._state["total_usd"] >= self._budget
            total, date = self._state["total_usd"], self._state["date"]
        if over:
            raise LayerError(
                "L0", "cost-ledger",
                f"已超出日预算:当日用量 ${total:.4f} >= "
                f"budget.daily_usd ${self._budget:.4f}(日期 {date});"
                "新请求被拒绝,提高预算或次日重试",
            )

    def snapshot(self) -> dict[str, Any]:
        self._rollover()
        return json.loads(json.dumps(self._state))
